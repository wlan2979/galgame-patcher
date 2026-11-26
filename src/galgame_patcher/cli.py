"""galgame-patcher 命令行与桌面 GUI 启动入口。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional, Union

from ._version import __version__
from .catalog import PatchCatalog
from .downloader import (
    OFFICIAL_PATCH_PORTALS,
    discover_patches_online,
    get_user_sources_path,
    is_url,
    load_user_sources,
    save_user_sources,
)
from .locator import ManualLocator, SteamLocator
from .models import (
    CatalogEntry,
    ConcurrencyError,
    GameRunningError,
    PatchResult,
    UntrustedSourceError,
    VerificationError,
    format_size_bytes,
    parse_size_bytes,
)
from .patcher import GalgamePatcher, compute_sha256, inspect_patch_file

# 统一 CLI 退出码规范
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GAME_RUNNING = 2
EXIT_VERIFICATION_FAILED = 3
EXIT_CONCURRENCY_CONFLICT = 4
EXIT_UNTRUSTED_SOURCE = 5


def _emit(
    *,
    json_output: bool,
    data: Any,
    text: Union[str, Sequence[str]],
    stream: Any = None,
) -> None:
    """统一 CLI 输出入口：开启 --json 时输出结构化 JSON，否则输出人类可读文本。"""
    target = stream if stream is not None else sys.stdout
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2), file=target)
        return
    if isinstance(text, str):
        if text:
            print(text, file=target)
    else:
        for line in text:
            print(line, file=target)


def _exit_code_for_exception(exc: BaseException) -> int:
    """根据异常类型返回约定的 CLI 退出码。"""
    if isinstance(exc, GameRunningError):
        return EXIT_GAME_RUNNING
    if isinstance(exc, VerificationError):
        return EXIT_VERIFICATION_FAILED
    if isinstance(exc, ConcurrencyError):
        return EXIT_CONCURRENCY_CONFLICT
    if isinstance(exc, UntrustedSourceError):
        return EXIT_UNTRUSTED_SOURCE
    return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="galgame-patcher",
        description="一键定位、自动爬取并给对应 Galgame 安装/还原补丁的命令行与桌面工具",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # apply 子命令
    apply_parser = subparsers.add_parser(
        "apply", help="安装本地补丁或远程 URL 补丁到指定/自动定位的游戏目录"
    )
    apply_parser.add_argument(
        "patch_source", help="补丁文件路径、文件夹路径、压缩包或下载链接 (URL)"
    )
    apply_parser.add_argument("--game-dir", help="直接指定游戏安装目录")
    apply_parser.add_argument("--app-id", type=int, help="通过 Steam AppID 自动定位游戏")
    apply_parser.add_argument("--game-name", help="通过游戏目录名称自动定位游戏")
    apply_parser.add_argument("--steam-root", help="自定义 Steam 根目录路径")
    apply_parser.add_argument("--target-subdir", default="", help="补丁释放到游戏目录下的子目录")
    apply_parser.add_argument("--sha256", dest="expected_sha256", help="补丁文件预期 SHA256 值")
    apply_parser.add_argument("--require-sha256", action="store_true", help="要求提供预期 SHA256")
    apply_parser.add_argument("--password", help="加密压缩包的解压密码")
    apply_parser.add_argument(
        "--try-password",
        dest="password_candidates",
        action="append",
        help="附加尝试的候选解压密码（可多次指定）",
    )
    apply_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="本轮成功后不保留新增备份，已有备份继续保留",
    )
    apply_parser.add_argument(
        "--no-strip",
        action="store_true",
        help="解压压缩包时不剥离单层顶层套壳目录",
    )
    apply_parser.add_argument(
        "--force-locked",
        action="store_true",
        help="跳过目标文件占用检查。游戏运行时覆盖文件可能损坏安装。",
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将覆盖或新增的文件列表，不实际写入磁盘（P3-2）",
    )
    apply_parser.add_argument(
        "--allow-untrusted-url",
        action="store_true",
        help="【高风险】允许从未经审核且无 SHA256 校验的远程 URL 下载并安装补丁",
    )

    # scan 子命令
    scan_parser = subparsers.add_parser(
        "scan", help="扫描本机已安装的清单 Galgame、手工登记游戏与非 Steam 游戏"
    )
    scan_parser.add_argument("--catalog", help="自定义清单 JSON 文件路径或远程 URL")
    scan_parser.add_argument(
        "--only-installed",
        action="store_true",
        help="仅显示本机已检测到的已安装游戏",
    )
    scan_parser.add_argument(
        "--include-manual",
        action="store_true",
        help="包含通过 add-game 手工登记的游戏目录（P2-1）",
    )
    scan_parser.add_argument(
        "--deep",
        action="store_true",
        help="深度扫描非 Steam 常见安装位置（P2-1）",
    )
    scan_parser.add_argument(
        "--deep-root",
        dest="deep_roots",
        action="append",
        help="指定深度扫描根目录（可多次指定，默认 C:/D:/E:）",
    )
    scan_parser.add_argument("--steam-root", help="自定义 Steam 根目录路径")

    # add-game 子命令（P2-1）
    add_game_parser = subparsers.add_parser(
        "add-game", help="将非 Steam 游戏目录手工登记到本地配置库"
    )
    add_game_parser.add_argument("path", help="游戏安装根目录路径")
    add_game_parser.add_argument("--name", help="自定义游戏名称（默认取目录名）")
    add_game_parser.add_argument(
        "--executable", help="主程序文件名（默认取目录下体积最大的 .exe）"
    )

    # catalog 子命令（P2-2: update / list / verify / add / remove / validate / export）
    catalog_parser = subparsers.add_parser(
        "catalog", help="管理补丁源清单（编辑条目、校验、导出、远程更新、列表查看、链接探活）"
    )
    catalog_sub = catalog_parser.add_subparsers(
        dest="catalog_action", required=True
    )
    cat_update = catalog_sub.add_parser("update", help="从远程 URL 拉取并增量合并更新清单")
    cat_update.add_argument("url", help="远程清单 JSON URL")
    cat_update.add_argument("--catalog", help="要更新的本地清单文件路径（默认 %APPDATA%）")
    trust_group = cat_update.add_mutually_exclusive_group()
    trust_group.add_argument(
        "--trust-catalog-sha256",
        metavar="SHA256",
        help="锁定远程清单文件本身的 SHA256（推荐，从可信渠道取得）",
    )
    trust_group.add_argument(
        "--allow-untrusted-catalog",
        action="store_true",
        help="【高风险】不锁定清单文件，直接信任远程清单提供的链接与哈希",
    )

    cat_list = catalog_sub.add_parser("list", help="列出清单中的所有游戏条目")
    cat_list.add_argument("--catalog", help="清单文件路径或远程 URL")

    cat_verify = catalog_sub.add_parser("verify", help="对清单中的每个补丁链接发起 HEAD 请求探活")
    cat_verify.add_argument("--catalog", help="清单文件路径或远程 URL")

    cat_add = catalog_sub.add_parser("add", help="新增或更新清单条目（按 id 覆盖）")
    cat_add.add_argument("--id", required=True, help="条目唯一 ID，例如 game_name")
    cat_add.add_argument("--name", required=True, help="游戏名称")
    cat_add.add_argument("--url", help="补丁下载链接（http/https）")
    cat_add.add_argument("--sha256", help="补丁文件预期 SHA256（64 位十六进制）")
    cat_add.add_argument(
        "--password",
        help="（已停用明文落盘）本地清单默认不保存明文密码，请在 apply/auto 时传 --password",
    )
    cat_add.add_argument("--app-id", type=int, help="Steam AppID")
    cat_add.add_argument("--install-dir", help="Steam 安装目录名")
    cat_add.add_argument("--executable", help="游戏主程序文件名")
    cat_add.add_argument("--subdir", dest="target_subdir", help="补丁写入的子目录")
    cat_add.add_argument("--patch-version", help="补丁版本号（默认 1.0）")
    cat_add.add_argument("--description", help="补丁说明")
    cat_add.add_argument(
        "--catalog", help="要编辑的本地清单文件路径（默认 %APPDATA%\\galgame-patcher\\catalog.json）"
    )

    cat_remove = catalog_sub.add_parser("remove", help="按 id 删除清单条目")
    cat_remove.add_argument("entry_id", help="要删除的条目 ID")
    cat_remove.add_argument("--catalog", help="要编辑的本地清单文件路径")

    cat_validate = catalog_sub.add_parser("validate", help="检查清单格式与条目字段")
    cat_validate.add_argument("--catalog", help="清单文件路径或远程 URL")

    cat_export = catalog_sub.add_parser(
        "export", help="将清单导出到指定 JSON 文件（默认不含明文解压密码）"
    )
    cat_export.add_argument("output", help="导出目标 JSON 文件路径")
    cat_export.add_argument("--catalog", help="源清单文件路径或远程 URL")
    cat_export.add_argument(
        "--include-passwords",
        action="store_true",
        help="【安全注意】显式导出条目的明文解压密码字段",
    )

    # search 子命令（只列出链接，不下载文件）
    search_parser = subparsers.add_parser(
        "search",
        help="从 VNDB / GitHub Releases / 指定网页检索补丁链接（只列出链接，不下载文件）",
    )
    search_parser.add_argument("query", help="游戏名称（英文/日文/中文）或要爬取的补丁网页 URL")
    search_parser.add_argument(
        "--open",
        dest="open_index",
        type=int,
        metavar="N",
        help="直接在系统浏览器中打开第 N 条搜索结果的链接",
    )
    search_parser.add_argument("--verbose", action="store_true", help="显示各来源失败详情")

    # sites 子命令（补丁站点导航与自定义来源配置）
    sites_parser = subparsers.add_parser(
        "sites", help="列出发行商、社区补丁站点和自定义来源配置文件路径"
    )
    sites_parser.add_argument(
        "--open",
        dest="open_index",
        type=int,
        metavar="N",
        help="直接在系统浏览器中打开第 N 个网站",
    )
    sites_parser.add_argument(
        "--init-config",
        action="store_true",
        help="生成或刷新用户自定义补丁站点配置文件 (%APPDATA%\\galgame-patcher\\sources.json)",
    )

    # verify-file 子命令（安装前本地文件安全核验）
    verify_file_parser = subparsers.add_parser(
        "verify-file",
        help="检查本地补丁文件大小、修改时间、类型与 SHA256（安装前核对）",
    )
    verify_file_parser.add_argument("path", help="待检查的本地补丁文件路径")
    verify_file_parser.add_argument("--sha256", help="从可信发布页取得的预期 SHA256")

    # auto 子命令
    auto_parser = subparsers.add_parser(
        "auto", help="根据清单自动定位游戏、下载并安装对应补丁"
    )
    auto_parser.add_argument("game", help="清单中的游戏 ID、名称或 Steam AppID")
    auto_parser.add_argument("--catalog", help="自定义清单 JSON 文件路径或远程 URL")
    auto_parser.add_argument("--game-dir", help="手动覆盖目标游戏目录")
    auto_parser.add_argument("--steam-root", help="自定义 Steam 根目录路径")
    auto_parser.add_argument(
        "--password",
        help="加密压缩包的解压密码（本地清单默认不保存明文密码，安装加密包时通过此参数提供）",
    )
    auto_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="本轮成功后不保留新增备份，已有备份继续保留",
    )
    auto_parser.add_argument(
        "--force-locked",
        action="store_true",
        help="跳过目标文件占用检查。游戏运行时覆盖文件可能损坏安装。",
    )
    auto_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览变更而不实际写入磁盘",
    )
    auto_parser.add_argument(
        "--allow-untrusted-url",
        action="store_true",
        help="【高风险】允许清单中未配置 expected_sha256 的条目从远程下载安装",
    )

    # gui 子命令
    subparsers.add_parser("gui", help="启动可视化桌面图形界面")

    # locate 子命令
    locate_parser = subparsers.add_parser("locate", help="自动定位游戏安装目录")
    locate_parser.add_argument("--app-id", type=int, help="Steam AppID")
    locate_parser.add_argument("--game-name", help="游戏目录或名称")
    locate_parser.add_argument("--steam-root", help="自定义 Steam 根目录路径")
    locate_parser.add_argument(
        "--deep", action="store_true", help="启用深度扫描定位非 Steam 游戏"
    )

    # rollback 子命令
    rollback_parser = subparsers.add_parser("rollback", help="从备份一键还原游戏目录")
    rollback_parser.add_argument("game_dir", help="需要还原的游戏安装目录")
    rollback_parser.add_argument(
        "--mode",
        choices=("full", "keep-patches"),
        help="full 完整还原；keep-patches 仅撤回最近一次安装",
    )
    history_group = rollback_parser.add_mutually_exclusive_group()
    history_group.add_argument("--history", action="store_true", help="列出安装记录和时间")
    history_group.add_argument("--to", metavar="记录ID", help="仅撤回指定安装记录之后的安装")

    # status 子命令
    status_parser = subparsers.add_parser("status", help="查看游戏目录当前的补丁安装状态")
    status_parser.add_argument("game_dir", help="游戏安装目录")

    # recover 子命令
    recover_parser = subparsers.add_parser(
        "recover", help="恢复上次中断的补丁安装（完整回滚残留改动）"
    )
    recover_parser.add_argument("game_dir", help="游戏安装目录")
    recover_parser.add_argument("--yes", action="store_true", help="跳过交互确认")

    # backup 子命令
    backup_parser = subparsers.add_parser(
        "backup", help="管理游戏目录的补丁备份（清理多余历史快照）"
    )
    backup_sub = backup_parser.add_subparsers(dest="backup_action", required=True)
    backup_prune = backup_sub.add_parser(
        "prune",
        aliases=["clean"],
        help="清理超出数量或体积上限的历史快照（始终保留首次安装前的原始基线）",
    )
    backup_prune.add_argument("game_dir", help="游戏安装目录")
    backup_prune.add_argument(
        "--keep",
        type=int,
        default=3,
        help="保留最近 N 次安装快照（默认 3；0 表示仅保留原始基线）",
    )
    backup_prune.add_argument(
        "--max-size",
        default="2G",
        help="备份总体积上限（默认 2G，例如 500M、2G；设为 none 不按体积裁剪）",
    )
    backup_prune.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将清理的快照与可释放空间，不实际删除",
    )

    # hash 子命令
    hash_parser = subparsers.add_parser("hash", help="计算补丁文件的 SHA256 校验值")
    hash_parser.add_argument("file", help="待计算哈希的文件路径")

    # 统一为所有非 GUI 命令挂载 --json 参数
    for json_parser in (
        apply_parser,
        scan_parser,
        add_game_parser,
        cat_update,
        cat_list,
        cat_verify,
        cat_add,
        cat_remove,
        cat_validate,
        cat_export,
        search_parser,
        sites_parser,
        verify_file_parser,
        auto_parser,
        locate_parser,
        rollback_parser,
        status_parser,
        recover_parser,
        backup_prune,
        hash_parser,
    ):
        json_parser.add_argument(
            "--json",
            dest="json_output",
            action="store_true",
            help="以 JSON 格式输出结果",
        )

    for command_parser in (
        apply_parser,
        scan_parser,
        auto_parser,
        locate_parser,
        rollback_parser,
        status_parser,
        recover_parser,
        backup_prune,
    ):
        command_parser.add_argument("--verbose", action="store_true", help="显示诊断详情")

    return parser


def _report_patch_result(
    result: PatchResult, *, json_output: bool = False, show_paths: bool = False
) -> int:
    exit_code = EXIT_OK
    if not result.success:
        exit_code = (
            EXIT_VERIFICATION_FAILED if result.verification_failed else EXIT_ERROR
        )

    if json_output:
        _emit(json_output=True, data=result.to_dict(), text="", stream=sys.stdout)
        return exit_code

    lines: list[str] = [result.message]
    if result.failed_reasons:
        lines.append(result.failure_details())
    _emit(
        json_output=False,
        data=None,
        text=lines,
        stream=sys.stdout if result.success else sys.stderr,
    )
    if show_paths:
        path_lines = [f"游戏目录: {result.game_dir}"]
        if result.backup_dir:
            path_lines.append(f"备份目录: {result.backup_dir}")
        _emit(json_output=False, data=None, text=path_lines, stream=sys.stdout)
    return exit_code


def _editable_catalog(source: Optional[str]) -> tuple[PatchCatalog, Path]:
    """加载可编辑的本地清单；未指定路径且默认文件不存在时从空白清单开始。"""
    if source and is_url(source):
        raise ValueError("catalog add/remove 只支持本地清单文件，不能直接编辑远程 URL")
    path = Path(source) if source else PatchCatalog.default_storage_path()
    if path.is_file():
        return PatchCatalog.from_file(path), path
    # 显式指定的新路径也允许从空白清单开始，方便首次创建。
    return PatchCatalog(entries=[]), path


def _collect_catalog_update_changes(
    before: dict[str, dict[str, object]], catalog: PatchCatalog
) -> list[str]:
    """逐条收集远程更新带来的新增与变更，提示用户核对链接和哈希来源。"""
    changes: list[str] = []
    for entry in catalog.entries:
        old = before.get(entry.id)
        if old is None:
            changes.append(
                f"[新增] {entry.id}: {entry.name} -> "
                f"{entry.patch_url or '未配置 URL'} "
                f"(SHA256: {'已配置' if entry.expected_sha256 else '未配置'})"
            )
            continue
        old_url = str(old.get("patch_url") or "")
        old_hash = str(old.get("expected_sha256") or "")
        if (
            old.get("name") == entry.name
            and old_url == entry.patch_url
            and old_hash == (entry.expected_sha256 or "")
        ):
            continue
        changes.append(
            f"[更新] {entry.id}: {entry.name}\n"
            f"    URL: {old_url or '-'} -> {entry.patch_url or '-'}\n"
            f"    SHA256: {old_hash or '-'} -> {entry.expected_sha256 or '-'}"
        )
    return changes


def _handle_catalog_edit(args: argparse.Namespace, *, json_output: bool) -> int:
    """处理 catalog add / remove / validate。"""
    if args.catalog_action == "validate":
        catalog = PatchCatalog.load(args.catalog)
        errors, warnings = catalog.validate()
        if json_output:
            _emit(
                json_output=True,
                data={
                    "valid": not errors,
                    "count": len(catalog.entries),
                    "errors": errors,
                    "warnings": warnings,
                },
                text="",
            )
            return EXIT_OK if not errors else EXIT_VERIFICATION_FAILED

        if warnings:
            _emit(
                json_output=False,
                data=None,
                text=[f"警告: {warning}" for warning in warnings],
            )
        if errors:
            _emit(
                json_output=False,
                data=None,
                text=[f"错误: {error}" for error in errors],
                stream=sys.stderr,
            )
        _emit(
            json_output=False,
            data=None,
            text=(
                f"清单共 {len(catalog.entries)} 条："
                f"{len(errors)} 个错误，{len(warnings)} 个警告。"
            ),
        )
        return EXIT_OK if not errors else EXIT_VERIFICATION_FAILED

    catalog, path = _editable_catalog(args.catalog)

    if args.catalog_action == "remove":
        entry_id = args.entry_id.strip()
        remaining = [entry for entry in catalog.entries if entry.id != entry_id]
        if len(remaining) == len(catalog.entries):
            _emit(
                json_output=json_output,
                data={"ok": False, "removed": False, "id": entry_id, "error": f"未找到条目: {entry_id}"},
                text=f"未找到条目: {entry_id}",
                stream=sys.stdout if json_output else sys.stderr,
            )
            return EXIT_ERROR
        catalog.entries = remaining
        saved = catalog.save_to_file(path)
        _emit(
            json_output=json_output,
            data={
                "ok": True,
                "removed": True,
                "id": entry_id,
                "path": str(saved),
                "remaining": len(remaining),
            },
            text=f"已删除条目 {entry_id}，清单已保存: {saved}（剩余 {len(remaining)} 条）",
        )
        return EXIT_OK

    entry_id = args.id.strip()
    entry = next((item for item in catalog.entries if item.id == entry_id), None)
    if entry is None:
        entry = CatalogEntry(id=entry_id, name=args.name, source="user")
        catalog.entries.append(entry)
    entry.name = args.name
    optional_fields = {
        "patch_url": args.url,
        "expected_sha256": args.sha256,
        "steam_app_id": args.app_id,
        "install_dir_name": args.install_dir,
        "executable_name": args.executable,
        "target_subdir": args.target_subdir,
        "patch_version": args.patch_version,
        "description": args.description,
    }
    for field_name, value in optional_fields.items():
        if value is not None:
            setattr(entry, field_name, value)

    if args.password is not None:
        print(
            "警告：出于安全考虑，本地清单不会将解压密码明文落盘。"
            "安装加密补丁时请使用 --password 参数临时提供密码。",
            file=sys.stderr,
        )

    errors, warnings = catalog.validate()
    if errors:
        if json_output:
            _emit(
                json_output=True,
                data={
                    "ok": False,
                    "saved": False,
                    "errors": errors,
                    "warnings": warnings,
                },
                text="",
            )
        else:
            err_lines = [f"错误: {error}" for error in errors]
            err_lines.append("清单未保存，请修正上述错误后重试。")
            _emit(json_output=False, data=None, text=err_lines, stream=sys.stderr)
        return EXIT_VERIFICATION_FAILED

    saved = catalog.save_to_file(path)
    if json_output:
        _emit(
            json_output=True,
            data={
                "ok": True,
                "saved": True,
                "path": str(saved),
                "count": len(catalog.entries),
                "entry": entry.to_dict(include_password=False),
                "warnings": warnings,
            },
            text="",
        )
    else:
        _emit(
            json_output=False,
            data=None,
            text=f"已保存清单: {saved}（共 {len(catalog.entries)} 条）",
        )
        if warnings:
            _emit(
                json_output=False,
                data=None,
                text=[f"警告: {warning}" for warning in warnings],
                stream=sys.stderr,
            )
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行主入口，按统一退出码规范返回状态码。"""
    if argv is None and len(sys.argv) == 1 and getattr(sys, "frozen", False):
        from .gui import launch_gui

        return launch_gui()

    parser = build_parser()
    args = parser.parse_args(argv)
    json_output = bool(getattr(args, "json_output", False))
    verbose = args.command != "search" and getattr(args, "verbose", False)
    logger = logging.getLogger("galgame_patcher")
    previous_log_level = logger.level
    handler = None
    if verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

    try:
        if args.command == "gui":
            try:
                from .gui import launch_gui
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.split(".")[0] in ("tkinter", "_tkinter"):
                    _emit(
                        json_output=False,
                        data=None,
                        text=(
                            "错误: 当前 Python 未包含 tkinter，无法启动 GUI。\n"
                            "  Debian/Ubuntu: sudo apt install python3-tk\n"
                            "  其他情况请改用带 Tk 支持的 Python，或直接使用 Release 中的 .exe。\n"
                            "  命令行功能 (apply/scan/auto/locate/rollback/hash) 不受影响。"
                        ),
                        stream=sys.stderr,
                    )
                    return EXIT_ERROR
                raise

            return launch_gui()

        if args.command == "apply":
            if not args.game_dir and args.app_id is None and not args.game_name:
                parser.error("请至少提供 --game-dir、--app-id 或 --game-name 之一")

            if not (args.expected_sha256 or "").strip():
                if args.require_sha256:
                    raise ValueError("--require-sha256 需要同时提供 --sha256")
                if not args.dry_run and not is_url(args.patch_source):
                    print(
                        "警告：安装本地补丁前未提供预期 SHA256，请确认文件来自可信发布者。",
                        file=sys.stderr,
                    )

            locator = SteamLocator(steam_root=args.steam_root)
            patcher = GalgamePatcher(locator=locator)
            result = patcher.apply(
                patch_source=args.patch_source,
                game_dir=args.game_dir,
                app_id=args.app_id,
                game_name=args.game_name,
                target_subdir=args.target_subdir,
                backup=not args.no_backup,
                expected_sha256=args.expected_sha256,
                strip_single_top_dir=not args.no_strip,
                password=args.password,
                password_candidates=args.password_candidates,
                force_locked=args.force_locked,
                dry_run=args.dry_run,
                allow_untrusted_url=args.allow_untrusted_url,
            )
            return _report_patch_result(
                result, json_output=json_output, show_paths=True
            )

        if args.command == "scan":
            catalog = PatchCatalog.load(args.catalog)
            locator = SteamLocator(
                steam_root=args.steam_root,
                deep_scan=args.deep,
                deep_scan_roots=args.deep_roots,
            )
            statuses = catalog.scan_games(
                locator=locator,
                only_installed=args.only_installed,
                include_manual=args.include_manual,
                deep_scan=args.deep,
                deep_scan_roots=args.deep_roots,
            )
            if not statuses:
                _emit(
                    json_output=json_output,
                    data=[],
                    text="未扫描到符合条件的游戏。",
                )
                return EXIT_OK
            scan_lines: list[str] = []
            for item in statuses:
                state = "已安装" if item.installed else "未安装"
                patch_state = "已打补丁" if item.patched else "未打补丁"
                if item.patched is None:
                    patch_state = "补丁状态待确认"
                app_id_text = (
                    str(item.entry.steam_app_id)
                    if item.entry.steam_app_id is not None
                    else "-"
                )
                dir_text = str(item.game_dir) if item.game_dir else "-"
                scan_lines.append(
                    f"[{state} / {patch_state}] {item.entry.name} "
                    f"(id={item.entry.id}, app_id={app_id_text}) -> {dir_text}"
                )
            _emit(
                json_output=json_output,
                data=[item.to_dict() for item in statuses],
                text=scan_lines,
            )
            return EXIT_OK

        if args.command == "add-game":
            manual = ManualLocator()
            reg = manual.add_game(
                args.path,
                name=args.name,
                executable=args.executable,
            )
            _emit(
                json_output=json_output,
                data=reg,
                text=(
                    f"已登记游戏: {reg['name']} "
                    f"(可执行文件: {reg.get('executable') or '无'}) -> {reg['dir']}"
                ),
            )
            return EXIT_OK

        if args.command == "catalog":
            if args.catalog_action in ("add", "remove", "validate"):
                return _handle_catalog_edit(args, json_output=json_output)

            catalog = PatchCatalog.load(getattr(args, "catalog", None))
            if args.catalog_action == "list":
                list_lines = [
                    f"清单版本: v{catalog.version} (共 {len(catalog.entries)} 条)"
                ]
                for e in catalog.entries:
                    list_lines.append(
                        f"- {e.id}: {e.name} (v{e.patch_version}, "
                        f"source={e.source}, last_verified={e.last_verified or '-'}) -> {e.patch_url or '未配置URL'}"
                    )
                _emit(
                    json_output=json_output,
                    data={
                        "version": catalog.version,
                        "count": len(catalog.entries),
                        "games": [
                            e.to_dict(include_password=False) for e in catalog.entries
                        ],
                    },
                    text=list_lines,
                )
                return EXIT_OK

            if args.catalog_action == "export":
                if args.include_passwords:
                    print(
                        "警告：本次导出包含明文解压密码，请妥善保管并勿直接分享。",
                        file=sys.stderr,
                    )
                saved = catalog.save_to_file(
                    args.output, include_passwords=args.include_passwords
                )
                suffix = (
                    "（含明文解压密码）"
                    if args.include_passwords
                    else "（不含明文解压密码）"
                )
                _emit(
                    json_output=json_output,
                    data={
                        "path": str(saved),
                        "count": len(catalog.entries),
                        "include_passwords": bool(args.include_passwords),
                    },
                    text=f"已导出清单: {saved}（共 {len(catalog.entries)} 条）{suffix}",
                )
                return EXIT_OK

            if args.catalog_action == "update":
                before = {
                    entry.id: entry.to_dict(include_password=False)
                    for entry in catalog.entries
                }
                if args.allow_untrusted_catalog:
                    print(
                        "警告：正在信任未经哈希锁定的远程清单。"
                        "清单作者可替换补丁链接与预期 SHA256，请自行确认来源。",
                        file=sys.stderr,
                    )
                updated = catalog.update_from_remote(
                    args.url,
                    target_path=args.catalog,
                    expected_sha256=args.trust_catalog_sha256,
                    allow_untrusted=args.allow_untrusted_catalog,
                )
                changes = _collect_catalog_update_changes(before, catalog) if updated else []
                update_lines: list[str] = []
                if updated:
                    update_lines.append(
                        f"清单已更新至 v{catalog.version}（共 {len(catalog.entries)} 个条目）。"
                    )
                    if catalog.remote_sha256:
                        update_lines.append(f"远程清单 SHA256: {catalog.remote_sha256}")
                    if changes:
                        update_lines.append("本次清单变更（请核对链接与哈希来源）:")
                        update_lines.extend(f"  {line}" for line in changes)
                else:
                    update_lines.append(
                        f"本地清单已是最新版本 (v{catalog.version})，无需更新。"
                    )
                _emit(
                    json_output=json_output,
                    data={
                        "updated": updated,
                        "version": catalog.version,
                        "count": len(catalog.entries),
                        "remote_sha256": catalog.remote_sha256 or None,
                        "changes": changes,
                    },
                    text=update_lines,
                )
                return EXIT_OK

            if args.catalog_action == "verify":
                report = catalog.verify_links()
                if args.catalog and not is_url(args.catalog):
                    catalog.save_to_file(args.catalog)
                elif not args.catalog:
                    catalog.save_to_file()
                else:
                    print(
                        "提示：清单来自 URL，验证结果不会写回远程地址。",
                        file=sys.stderr,
                    )
                all_ok = True
                verify_lines: list[str] = []
                verify_data: dict[str, dict[str, Any]] = {}
                for entry_id, (ok, detail) in report.items():
                    mark = "有效" if ok else "失效"
                    if not ok:
                        all_ok = False
                    verify_lines.append(f"[{mark}] {entry_id}: {detail}")
                    verify_data[entry_id] = {"ok": ok, "detail": detail}
                _emit(
                    json_output=json_output,
                    data={"all_ok": all_ok, "results": verify_data},
                    text=verify_lines,
                )
                return EXIT_OK if all_ok else EXIT_ERROR

        if args.command == "search":
            failures: list[tuple[str, str]] = []
            candidates = discover_patches_online(
                args.query,
                on_failure=lambda source, detail: failures.append((source, detail)),
            )
            if failures:
                sources = "、".join(dict.fromkeys(source for source, _ in failures))
                print(
                    f"部分搜索来源未完成：{sources}。结果可能不完整。",
                    file=sys.stderr,
                )
                if args.verbose:
                    for source, detail in failures:
                        print(f"- {source}：{detail}", file=sys.stderr)
            if args.open_index is not None:
                if args.open_index < 1 or args.open_index > len(candidates):
                    print(
                        f"错误: --open 序号超出范围（共 {len(candidates)} 条，指定了 {args.open_index}）",
                        file=sys.stderr,
                    )
                    return EXIT_ERROR
                target_cand = candidates[args.open_index - 1]
                webbrowser.open(target_cand.url)
                if not json_output:
                    print(
                        f"已在系统浏览器中打开第 {args.open_index} 条链接: {target_cand.url}"
                    )

            search_payload = [
                {
                    "trusted": c.trusted,
                    "trust_reason": c.trust_reason,
                    "bare_executable": c.bare_executable,
                    "manual_install": c.manual_install,
                    "url": c.url,
                    "title": c.title,
                    "source_site": c.source_site,
                    "is_direct_download": c.is_direct_download,
                    "version": c.version,
                    "language": c.language,
                    "official": c.official,
                    "description": c.description,
                }
                for c in candidates
            ]
            if not candidates:
                _emit(
                    json_output=json_output,
                    data=search_payload,
                    text=f"未搜索到与 {args.query!r} 相关的在线补丁（本命令只列出链接，不下载任何文件）。",
                )
                return EXIT_OK

            search_lines = [
                f"共找到 {len(candidates)} 条补丁候选（本命令只列出链接，不下载任何文件）：\n"
            ]
            for idx, cand in enumerate(candidates, start=1):
                trust_tag = "白名单" if cand.trusted else "未审核"
                kind = "直链包" if cand.is_direct_download else "发布页"
                if cand.manual_install:
                    kind = "手动安装"
                lang = f" [{cand.language}]" if cand.language else ""
                search_lines.append(
                    f"{idx}. [{trust_tag}] [{kind} | {cand.source_site}]{lang} {cand.title}"
                )
                search_lines.append(f"   URL: {cand.url}")
                if cand.description:
                    search_lines.append(f"   {cand.description}")
                search_lines.append(f"   说明: {cand.trust_reason}")
                if cand.bare_executable:
                    search_lines.append(
                        "   ⚠ 该链接指向可执行文件，下载后请务必核对哈希再安装"
                    )
            search_lines.append(
                "\n以上链接均来自第三方，本工具不保证其安全性。\n"
                "请在浏览器查阅发行版和安装方式。标为手动安装的补丁请按发布者的说明操作。\n"
                "使用本工具安装本地补丁前，可用 verify-file --sha256 核对文件。"
            )
            _emit(json_output=json_output, data=search_payload, text=search_lines)
            return EXIT_OK

        if args.command == "sites":
            cfg_initialized: Optional[Path] = None
            if args.init_config:
                cfg_initialized = save_user_sources()
                if not json_output:
                    print(f"已初始化自定义补丁来源配置文件: {cfg_initialized}")

            portals = list(OFFICIAL_PATCH_PORTALS)
            user_cfg = load_user_sources()
            for idx, page_url in enumerate(user_cfg.get("extra_pages", []), start=1):
                portals.append(
                    {
                        "name": f"自定义站点 #{idx}",
                        "url": page_url,
                        "mode": "自定义检索",
                        "desc": "来自用户 sources.json extra_pages",
                    }
                )

            if args.open_index is not None:
                if args.open_index < 1 or args.open_index > len(portals):
                    print(
                        f"错误: --open 序号超出范围（共 {len(portals)} 个站点，指定了 {args.open_index}）",
                        file=sys.stderr,
                    )
                    return EXIT_ERROR
                chosen = portals[args.open_index - 1]
                webbrowser.open(chosen["url"])
                _emit(
                    json_output=json_output,
                    data={"opened": chosen},
                    text=f"已在浏览器中打开: {chosen['name']} -> {chosen['url']}",
                )
                return EXIT_OK

            site_lines = ["发行商与社区补丁资源导航\n"]
            for idx, site in enumerate(portals, start=1):
                site_lines.append(f"{idx}. [{site['mode']}] {site['name']}")
                site_lines.append(f"   URL: {site['url']}")
                site_lines.append(f"   说明: {site['desc']}")
            site_lines.append(f"\n自定义站点与白名单配置文件: {get_user_sources_path()}")
            site_lines.append(
                "可使用 `galpatch sites --open <序号>` 在浏览器中打开对应站点，或 `--init-config` 生成配置模板。"
            )
            _emit(
                json_output=json_output,
                data={
                    "config_path": str(get_user_sources_path()),
                    "initialized": str(cfg_initialized) if cfg_initialized else None,
                    "sites": portals,
                },
                text=site_lines,
            )
            return EXIT_OK

        if args.command == "verify-file":
            inspection = inspect_patch_file(args.path)
            verify_data_out = inspection.to_dict()
            vf_lines = [inspection.describe()]
            if inspection.is_bare_executable:
                vf_lines.append(
                    "\n⚠ 警告：该文件是可执行文件，安装后会被游戏直接加载，请确认来源可信！"
                )
            if inspection.rar_supported is False:
                print(
                    "\n⚠ 警告：当前环境无法直接解压 .rar 补丁，请先安装 7-Zip 或 WinRAR，或手动解压后再安装。",
                    file=sys.stderr,
                )
            if args.sha256 is not None:
                expected = args.sha256.strip().lower()
                if len(expected) != 64 or any(
                    c not in "0123456789abcdef" for c in expected
                ):
                    raise ValueError("预期 SHA256 必须为 64 位十六进制值")
                if inspection.sha256 != expected:
                    raise VerificationError(f"SHA256 不匹配，预期 {expected}")
                verify_data_out["sha256_matched"] = True
                vf_lines.append("SHA256 与预期值一致")
            _emit(json_output=json_output, data=verify_data_out, text=vf_lines)
            return EXIT_OK

        if args.command == "auto":
            catalog = PatchCatalog.load(args.catalog)
            locator = SteamLocator(steam_root=args.steam_root)
            patcher = GalgamePatcher(locator=locator)
            result = patcher.apply_from_catalog(
                args.game,
                catalog=catalog,
                game_dir=args.game_dir,
                backup=not args.no_backup,
                password=args.password,
                force_locked=args.force_locked,
                dry_run=args.dry_run,
                allow_untrusted_url=args.allow_untrusted_url,
            )
            return _report_patch_result(
                result, json_output=json_output, show_paths=True
            )

        if args.command == "locate":
            if args.app_id is None and not args.game_name:
                parser.error("请至少提供 --app-id 或 --game-name 之一")

            locator = SteamLocator(
                steam_root=args.steam_root,
                deep_scan=args.deep,
            )
            found_dir = locator.locate(
                app_id=args.app_id,
                game_name=args.game_name,
                deep_scan=args.deep,
            )
            _emit(
                json_output=json_output,
                data={"game_dir": str(found_dir)},
                text=str(found_dir),
            )
            return EXIT_OK

        if args.command == "rollback":
            patcher = GalgamePatcher()
            if args.history:
                history = patcher.rollback_history(args.game_dir)
                history_lines = (
                    [f"{record_id}  {applied_at}" for record_id, applied_at in history]
                    if history
                    else ["没有可单独撤回的安装记录。"]
                )
                _emit(
                    json_output=json_output,
                    data={
                        "game_dir": str(Path(args.game_dir).resolve()),
                        "history": [
                            {"id": record_id, "applied_at": applied_at}
                            for record_id, applied_at in history
                        ],
                    },
                    text=history_lines,
                )
                return EXIT_OK
            if args.to is not None and args.mode == "full":
                parser.error("--to 不能与 --mode full 同用")
            mode = "keep-patches" if args.to is not None else (args.mode or "full")
            if mode == "keep-patches":
                history = patcher.rollback_history(args.game_dir)
                if not history:
                    msg = "没有可撤回的逐次安装记录。如需完整还原，请使用 --mode full。"
                    _emit(
                        json_output=json_output,
                        data={
                            "success": False,
                            "game_dir": str(Path(args.game_dir).resolve()),
                            "message": msg,
                        },
                        text=msg,
                        stream=sys.stdout if json_output else sys.stderr,
                    )
                    return EXIT_ERROR
            return _report_patch_result(
                patcher.rollback(args.game_dir, mode=mode, to=args.to),
                json_output=json_output,
            )

        if args.command == "status":
            status = GalgamePatcher().install_status(args.game_dir)
            status_lines = [
                f"游戏目录: {status.game_dir}",
                f"状态: {status.state_label}",
            ]
            if status.manifest_path:
                status_lines.append(f"备份清单: {status.manifest_path}")
            if status.latest_install:
                applied = f"，安装时间 {status.applied_at}" if status.applied_at else ""
                status_lines.append(f"最近安装: {status.latest_install}{applied}")
            if status.state in (
                "installed",
                "installed_no_history",
                "incomplete",
            ):
                status_lines.append(
                    f"备份内容: 原文件 {status.backed_up_files} 个，"
                    f"新增文件 {status.newly_added_files} 个，"
                    f"新建目录 {status.created_dirs} 个"
                )
            status_lines.append(
                f"备份体积: {format_size_bytes(status.backup_size_bytes)}"
                f" ({status.backup_size_bytes} 字节)"
            )
            if status.history:
                status_lines.append("安装历史（最近在前）:")
                for record_id, applied_at in status.history:
                    status_lines.append(f"  {record_id}  {applied_at or '-'}")
            if status.message:
                status_lines.append(f"说明: {status.message}")
            if status.state == "incomplete":
                status_lines.append("可执行 galpatch recover <游戏目录> 回滚残留改动。")
            _emit(
                json_output=json_output,
                data=status.to_dict(),
                text=status_lines,
            )
            return EXIT_OK

        if args.command == "backup":
            if args.backup_action in ("prune", "clean"):
                raw_max = str(args.max_size).strip() if args.max_size is not None else ""
                max_size_bytes = (
                    None
                    if raw_max.lower() in ("", "none", "unlimited")
                    else parse_size_bytes(raw_max)
                )
                prune_result = GalgamePatcher().prune_backups(
                    args.game_dir,
                    keep=args.keep,
                    max_size_bytes=max_size_bytes,
                    dry_run=args.dry_run,
                )
                _emit(
                    json_output=json_output,
                    data=prune_result.to_dict(),
                    text=prune_result.message,
                )
                return EXIT_OK

        if args.command == "recover":
            patcher = GalgamePatcher()
            status = patcher.install_status(args.game_dir)
            if status.state == "invalid":
                msg = f"备份清单损坏，无法自动恢复: {status.manifest_path}\n{status.message}"
                _emit(
                    json_output=json_output,
                    data={
                        "success": False,
                        "game_dir": str(status.game_dir),
                        "state": status.state,
                        "message": msg,
                    },
                    text=msg,
                    stream=sys.stdout if json_output else sys.stderr,
                )
                return EXIT_ERROR
            if status.state != "incomplete":
                msg = f"当前状态为「{status.state_label}」，无需恢复。"
                _emit(
                    json_output=json_output,
                    data={
                        "success": True,
                        "recovered": False,
                        "game_dir": str(status.game_dir),
                        "state": status.state,
                        "message": msg,
                    },
                    text=msg,
                )
                return EXIT_OK
            if not args.yes:
                if not sys.stdin.isatty():
                    raise ValueError("非交互环境无法确认，请加 --yes 后重试")
                try:
                    answer = input(
                        f"将完整回滚 {status.game_dir} 中未完成的安装，是否继续? [y/N] "
                    )
                except EOFError as exc:
                    raise ValueError("未收到确认输入，请加 --yes 后重试") from exc
                if answer.strip().lower() not in ("y", "yes"):
                    _emit(
                        json_output=json_output,
                        data={"success": True, "recovered": False, "cancelled": True, "message": "已取消。"},
                        text="已取消。",
                    )
                    return EXIT_OK
            return _report_patch_result(
                patcher.recover_incomplete_install(args.game_dir),
                json_output=json_output,
                show_paths=True,
            )

        if args.command == "hash":
            digest = compute_sha256(args.file)
            _emit(
                json_output=json_output,
                data={"file": str(Path(args.file)), "sha256": digest},
                text=digest,
            )
            return EXIT_OK

    except Exception as exc:
        code = _exit_code_for_exception(exc)
        if json_output:
            _emit(
                json_output=True,
                data={
                    "ok": False,
                    "command": args.command,
                    "exit_code": code,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                text="",
                stream=sys.stdout,
            )
        _emit(
            json_output=False,
            data=None,
            text=f"错误 ({args.command}): {exc}",
            stream=sys.stderr,
        )
        if verbose:
            traceback.print_exc()
        return code
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            logger.setLevel(previous_log_level)

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
