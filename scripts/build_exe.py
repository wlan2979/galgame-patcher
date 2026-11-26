"""使用 PyInstaller 将 galgame-patcher 打包为 Windows 单文件可执行程序 (.exe)。"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional


def find_7z_binaries(
    project_root: Path, *, explicit_path: Optional[str] = None
) -> list[Path]:
    """查找可随 Windows 独立版打包的 7za.exe / 7z.exe（及同目录配套 DLL）。"""
    candidates: list[Path] = []

    raw_explicit = (explicit_path or os.environ.get("GALGAME_PATCHER_7Z_PATH") or "").strip()
    if raw_explicit:
        candidates.append(Path(raw_explicit))

    candidates.extend(
        [
            project_root / "vendor" / "7zip" / "7za.exe",
            project_root / "vendor" / "7zip" / "7z.exe",
        ]
    )

    for cmd in ("7za", "7z"):
        found = shutil.which(cmd)
        if found:
            candidates.append(Path(found))

    candidates.extend(
        [
            Path(r"C:\Program Files\7-Zip\7za.exe"),
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7za.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            bundled = [candidate]
            # 若使用 7z.exe（或含配套编解码器 DLL），一并打包同目录的 7z.dll / 7zxa.dll
            for dll_name in ("7z.dll", "7zxa.dll"):
                dll_path = candidate.parent / dll_name
                if dll_path.is_file():
                    bundled.append(dll_path)
            return bundled
    return []


def build_pyinstaller_command(
    project_root: Path,
    entry_script: Path,
    *,
    seven_zip_path: Optional[str] = None,
    require_7z: bool = False,
) -> tuple[list[str], list[Path]]:
    """构建 PyInstaller 命令行并返回 (命令参数列表, 已发现的 7z 二进制列表)。"""
    sep = os.pathsep
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "galgame-patcher-windows-x64",
        "--paths",
        str(project_root / "src"),
        "--hidden-import",
        "galgame_patcher.gui",
        "--add-data",
        f"LICENSE{sep}.",
        "--add-data",
        f"NOTICE{sep}.",
    ]

    for optional_pkg in ("py7zr", "pyzipper"):
        if importlib.util.find_spec(optional_pkg) is not None:
            command.extend(["--collect-all", optional_pkg])

    seven_z_bins = find_7z_binaries(project_root, explicit_path=seven_zip_path)
    if seven_z_bins:
        for bin_path in seven_z_bins:
            command.extend(["--add-binary", f"{bin_path}{sep}bin"])
        print(
            "Bundling 7-Zip binaries for .rar/.7z support: "
            + ", ".join(str(p) for p in seven_z_bins)
        )
    else:
        warning_msg = (
            "WARNING: 未找到 7za.exe 或 7z.exe，打包出的独立版将无法内置解压 .rar 压缩包，"
            "用户需本机安装 7-Zip 或 WinRAR。"
            "可通过 --seven-zip <路径> 或设置环境变量 GALGAME_PATCHER_7Z_PATH 指定 7za.exe/7z.exe。"
        )
        print(warning_msg, file=sys.stderr)
        if require_7z:
            raise FileNotFoundError(warning_msg)

    command.append(str(entry_script))
    return command, seven_z_bins


def main(argv: Optional[Sequence[str]] = None) -> int:
    """生成入口包装脚本并调用 PyInstaller 打包单文件 GUI 可执行程序。"""
    parser = argparse.ArgumentParser(description="打包 galgame-patcher Windows 单文件可执行程序")
    parser.add_argument(
        "--seven-zip",
        help="显式指定要打包进去的 7za.exe 或 7z.exe 路径（也可通过 GALGAME_PATCHER_7Z_PATH 指定）",
    )
    parser.add_argument(
        "--require-7z",
        action="store_true",
        help="未找到 7za.exe/7z.exe 时直接失败退出（适用于正式 Release 构建）",
    )
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    build_dir = project_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    entry_script = build_dir / "pyinstaller_entry.py"
    entry_script.write_text(
        "import sys\n"
        "from galgame_patcher.cli import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )

    command, _ = build_pyinstaller_command(
        project_root,
        entry_script,
        seven_zip_path=args.seven_zip,
        require_7z=args.require_7z,
    )

    subprocess.run(command, cwd=project_root, check=True)
    exe_path = project_root / "dist" / "galgame-patcher-windows-x64.exe"
    print(f"Build succeeded: {exe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
