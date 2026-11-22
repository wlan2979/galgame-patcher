"""使用 PyInstaller 将 galgame-patcher 打包为 Windows 单文件可执行程序 (.exe)。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """生成入口包装脚本并调用 PyInstaller 打包单文件 GUI 可执行程序。"""
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
    ]

    for optional_pkg in ("py7zr", "pyzipper"):
        if importlib.util.find_spec(optional_pkg) is not None:
            command.extend(["--collect-all", optional_pkg])

    command.append(str(entry_script))

    subprocess.run(command, cwd=project_root, check=True)
    exe_path = project_root / "dist" / "galgame-patcher-windows-x64.exe"
    print(f"Build succeeded: {exe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
