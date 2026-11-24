"""galgame-patcher 命令行与桌面 GUI 启动入口。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
import webbrowser
from pathlib import Path
from typing import Optional, Sequence

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
from .models import GameRunningError, PatchResult
from .patcher import GalgamePatcher, compute_sha256, inspect_patch_file


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

    # catalog 子命令（P2-2: update / list / verify）
    catalog_parser = subparsers.add_parser(
        "catalog", help="管理补丁源清单（远程更新、列表查看、链接探活）"
    )
    catalog_sub = catalog_parser.add_subparsers(
        dest="catalog_action", required=True
    )
    cat_update = catalog_sub.add_parser("update", help="从远程 URL 拉取并增量合并更新清单")
    cat_update.add_argument("url", help="远程清单 JSON URL")
    cat_update.add_argument("--catalog", help="要更新的本地清单文件路径（默认 %APPDATA%）")

    cat_list = catalog_sub.add_parser("list", help="列出清单中的所有游戏条目")
    cat_list.add_argument("--catalog", help="清单文件路径或远程 URL")

    cat_verify = catalog_sub.add_parser("verify", help="对清单中的每个补丁链接发起 HEAD 请求探活")
    cat_verify.add_argument("--catalog", help="清单文件路径或远程 URL")

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
    search_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="以 JSON 格式输出搜索结果",
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
    auto_parser.add_argument("--password", help="覆盖清单中的压缩包解压密码")
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
        "--mode", choices=("full", "keep-patches"),
        help="full 完整还原；keep-patches 仅撤回最近一次安装",
    )
    history_group = rollback_parser.add_mutually_exclusive_group()
    history_group.add_argument("--history", action="store_true", help="列出安装记录和时间")
    history_group.add_argument("--to", metavar="记录ID", help="仅撤回指定安装记录之后的安装")

    for command_parser in (apply_parser, scan_parser, auto_parser, locate_parser, rollback_parser):
        command_parser.add_argument("--verbose", action="store_true", help="显示诊断详情")

    # hash 子命令
    hash_parser = subparsers.add_parser("hash", help="计算补丁文件的 SHA256 校验值")
    hash_parser.add_argument("file", help="待计算哈希的文件路径")

    return parser


def _report_patch_result(result: PatchResult, *, show_paths: bool = False) -> int:
    print(result.message, file=sys.stdout if result.success else sys.stderr)
    if result.failed_reasons:
        print(result.failure_details(), file=sys.stderr)
    if show_paths:
        print(f"游戏目录: {result.game_dir}")
        if result.backup_dir:
            print(f"备份目录: {result.backup_dir}")
    return 0 if result.success else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行主入口，返回退出状态码。"""
    if argv is None and len(sys.argv) == 1 and getattr(sys, "frozen", False):
        from .gui import launch_gui

        return launch_gui()

    parser = build_parser()
    args = parser.parse_args(argv)
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
                    print(
                        "错误: 当前 Python 未包含 tkinter，无法启动 GUI。\n"
                        "  Debian/Ubuntu: sudo apt install python3-tk\n"
                        "  其他情况请改用带 Tk 支持的 Python，或直接使用 Release 中的 .exe。\n"
                        "  命令行功能 (apply/scan/auto/locate/rollback/hash) 不受影响。",
                        file=sys.stderr,
                    )
                    return 1
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
            return _report_patch_result(result, show_paths=True)

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
                print("未扫描到符合条件的游戏。")
                return 0
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
                print(
                    f"[{state} / {patch_state}] {item.entry.name} "
                    f"(id={item.entry.id}, app_id={app_id_text}) -> {dir_text}"
                )
            return 0

        if args.command == "add-game":
            manual = ManualLocator()
            reg = manual.add_game(
                args.path,
                name=args.name,
                executable=args.executable,
            )
            print(
                f"已登记游戏: {reg['name']} "
                f"(可执行文件: {reg.get('executable') or '无'}) -> {reg['dir']}"
            )
            return 0

        if args.command == "catalog":
            catalog = PatchCatalog.load(getattr(args, "catalog", None))
            if args.catalog_action == "list":
                print(f"清单版本: v{catalog.version} (共 {len(catalog.entries)} 条)")
                for e in catalog.entries:
                    print(
                        f"- {e.id}: {e.name} (v{e.patch_version}, "
                        f"source={e.source}, last_verified={e.last_verified or '-'}) -> {e.patch_url or '未配置URL'}"
                    )
                return 0

            if args.catalog_action == "update":
                updated = catalog.update_from_remote(
                    args.url,
                    target_path=args.catalog,
                )
                if updated:
                    print(
                        f"清单已更新至 v{catalog.version}（共 {len(catalog.entries)} 个条目）。"
                    )
                else:
                    print(f"本地清单已是最新版本 (v{catalog.version})，无需更新。")
                return 0

            if args.catalog_action == "verify":
                report = catalog.verify_links()
                if args.catalog:
                    catalog.save_to_file(args.catalog)
                all_ok = True
                for entry_id, (ok, detail) in report.items():
                    mark = "有效" if ok else "失效"
                    if not ok:
                        all_ok = False
                    print(f"[{mark}] {entry_id}: {detail}")
                return 0 if all_ok else 1

        if args.command == "search":
            failures: list[tuple[str, str]] = []
            candidates = discover_patches_online(
                args.query, on_failure=lambda source, detail: failures.append((source, detail))
            )
            if failures:
                sources = "、".join(dict.fromkeys(source for source, _ in failures))
                print(f"部分搜索来源未完成：{sources}。结果可能不完整。", file=sys.stderr)
                if args.verbose:
                    for source, detail in failures:
                        print(f"- {source}：{detail}", file=sys.stderr)
            if args.open_index is not None:
                if args.open_index < 1 or args.open_index > len(candidates):
                    print(
                        f"错误: --open 序号超出范围（共 {len(candidates)} 条，指定了 {args.open_index}）",
                        file=sys.stderr,
                    )
                    return 1
                target_cand = candidates[args.open_index - 1]
                webbrowser.open(target_cand.url)
                if not args.json_output:
                    print(
                        f"已在系统浏览器中打开第 {args.open_index} 条链接: {target_cand.url}"
                    )

            if args.json_output:
                payload = [
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
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0

            if not candidates:
                print(
                    f"未搜索到与 {args.query!r} 相关的在线补丁（本命令只列出链接，不下载任何文件）。"
                )
                return 0

            print(
                f"共找到 {len(candidates)} 条补丁候选（本命令只列出链接，不下载任何文件）：\n"
            )
            for idx, cand in enumerate(candidates, start=1):
                trust_tag = "白名单" if cand.trusted else "未审核"
                kind = "直链包" if cand.is_direct_download else "发布页"
                if cand.manual_install:
                    kind = "手动安装"
                lang = f" [{cand.language}]" if cand.language else ""
                print(
                    f"{idx}. [{trust_tag}] [{kind} | {cand.source_site}]{lang} {cand.title}"
                )
                print(f"   URL: {cand.url}")
                if cand.description:
                    print(f"   {cand.description}")
                print(f"   说明: {cand.trust_reason}")
                if cand.bare_executable:
                    print("   ⚠ 该链接指向可执行文件，下载后请务必核对哈希再安装")

            print(
                "\n以上链接均来自第三方，本工具不保证其安全性。\n"
                "请在浏览器查阅发行版和安装方式。标为手动安装的补丁请按发布者的说明操作。\n"
                "使用本工具安装本地补丁前，可用 verify-file --sha256 核对文件。"
            )
            return 0

        if args.command == "sites":
            if args.init_config:
                cfg_path = save_user_sources()
                print(f"已初始化自定义补丁来源配置文件: {cfg_path}")

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
                    return 1
                chosen = portals[args.open_index - 1]
                webbrowser.open(chosen["url"])
                print(f"已在浏览器中打开: {chosen['name']} -> {chosen['url']}")
                return 0

            print("发行商与社区补丁资源导航\n")
            for idx, site in enumerate(portals, start=1):
                print(f"{idx}. [{site['mode']}] {site['name']}")
                print(f"   URL: {site['url']}")
                print(f"   说明: {site['desc']}")

            print(f"\n自定义站点与白名单配置文件: {get_user_sources_path()}")
            print("可使用 `galpatch sites --open <序号>` 在浏览器中打开对应站点，或 `--init-config` 生成配置模板。")
            return 0

        if args.command == "verify-file":
            inspection = inspect_patch_file(args.path)
            print(inspection.describe())
            if inspection.is_bare_executable:
                print(
                    "\n⚠ 警告：该文件是可执行文件，安装后会被游戏直接加载，请确认来源可信！"
                )
            if args.sha256 is not None:
                expected = args.sha256.strip().lower()
                if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                    raise ValueError("预期 SHA256 必须为 64 位十六进制值")
                if inspection.sha256 != expected:
                    raise ValueError(f"SHA256 不匹配，预期 {expected}")
                print("SHA256 与预期值一致")
            return 0

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
            return _report_patch_result(result, show_paths=True)

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
            print(found_dir)
            return 0

        if args.command == "rollback":
            patcher = GalgamePatcher()
            if args.history:
                history = patcher.rollback_history(args.game_dir)
                for record_id, applied_at in history:
                    print(f"{record_id}  {applied_at}")
                if not history:
                    print("没有可单独撤回的安装记录。")
                return 0
            if args.to is not None and args.mode == "full":
                parser.error("--to 不能与 --mode full 同用")
            mode = "keep-patches" if args.to is not None else (args.mode or "full")
            return _report_patch_result(patcher.rollback(args.game_dir, mode=mode, to=args.to))

        if args.command == "hash":
            print(compute_sha256(args.file))
            return 0

    except Exception as exc:
        print(f"错误 ({args.command}): {exc}", file=sys.stderr)
        if verbose:
            traceback.print_exc()
        return 2 if isinstance(exc, GameRunningError) else 1
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            logger.setLevel(previous_log_level)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
