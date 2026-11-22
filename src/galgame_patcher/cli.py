"""galgame-patcher 命令行与桌面 GUI 启动入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .catalog import PatchCatalog
from .locator import SteamLocator
from .patcher import GalgamePatcher, compute_sha256


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="galgame-patcher",
        description="一键定位并给对应 Galgame 自动打补丁的命令行与桌面工具",
    )
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
        help="覆盖文件前不进行自动备份",
    )
    apply_parser.add_argument(
        "--no-strip",
        action="store_true",
        help="解压压缩包时不剥离单层顶层套壳目录",
    )

    # scan 子命令
    scan_parser = subparsers.add_parser(
        "scan", help="扫描本机已安装的清单 Galgame 与补丁状态"
    )
    scan_parser.add_argument("--catalog", help="自定义清单 JSON 文件路径或远程 URL")
    scan_parser.add_argument(
        "--only-installed",
        action="store_true",
        help="仅显示本机已检测到的已安装游戏",
    )
    scan_parser.add_argument("--steam-root", help="自定义 Steam 根目录路径")

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
        help="覆盖文件前不进行自动备份",
    )

    # gui 子命令
    subparsers.add_parser("gui", help="启动可视化桌面图形界面")

    # locate 子命令
    locate_parser = subparsers.add_parser("locate", help="自动定位游戏安装目录")
    locate_parser.add_argument("--app-id", type=int, help="Steam AppID")
    locate_parser.add_argument("--game-name", help="游戏目录或名称")
    locate_parser.add_argument("--steam-root", help="自定义 Steam 根目录路径")

    # rollback 子命令
    rollback_parser = subparsers.add_parser("rollback", help="从备份一键还原游戏目录")
    rollback_parser.add_argument("game_dir", help="需要还原的游戏安装目录")

    # hash 子命令
    hash_parser = subparsers.add_parser("hash", help="计算补丁文件的 SHA256 校验值")
    hash_parser.add_argument("file", help="待计算哈希的文件路径")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行主入口，返回退出状态码。"""
    if argv is None and len(sys.argv) == 1 and getattr(sys, "frozen", False):
        from .gui import launch_gui

        return launch_gui()

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "gui":
            from .gui import launch_gui

            return launch_gui()

        if args.command == "apply":
            if not args.game_dir and args.app_id is None and not args.game_name:
                parser.error("请至少提供 --game-dir、--app-id 或 --game-name 之一")

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
            )
            print(result.message)
            print(f"游戏目录: {result.game_dir}")
            if result.backup_dir:
                print(f"备份目录: {result.backup_dir}")
            return 0

        if args.command == "scan":
            catalog = PatchCatalog.load(args.catalog)
            locator = SteamLocator(steam_root=args.steam_root)
            statuses = catalog.scan_games(
                locator=locator,
                only_installed=args.only_installed,
            )
            if not statuses:
                print("未扫描到符合条件的游戏。")
                return 0
            for item in statuses:
                state = "已安装" if item.installed else "未安装"
                patch_state = "已打补丁" if item.patched else "未打补丁"
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
            )
            print(result.message)
            print(f"游戏目录: {result.game_dir}")
            if result.backup_dir:
                print(f"备份目录: {result.backup_dir}")
            return 0

        if args.command == "locate":
            if args.app_id is None and not args.game_name:
                parser.error("请至少提供 --app-id 或 --game-name 之一")

            locator = SteamLocator(steam_root=args.steam_root)
            found_dir = locator.locate(app_id=args.app_id, game_name=args.game_name)
            print(found_dir)
            return 0

        if args.command == "rollback":
            patcher = GalgamePatcher()
            result = patcher.rollback(Path(args.game_dir))
            print(result.message)
            return 0

        if args.command == "hash":
            print(compute_sha256(args.file))
            return 0

    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
