"""核心数据模型与常见 Galgame 引擎补丁文件常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

COMMON_PATCH_EXTENSIONS: tuple[str, ...] = (
    ".xp3",
    ".arc",
    ".pck",
    ".pac",
    ".ypf",
    ".int",
    ".bin",
    ".dat",
    ".afs",
    ".cpk",
    ".rpa",
    ".dll",
    ".exe",
    ".ini",
    ".cfg",
    ".json",
    ".txt",
    ".ks",
    ".scn",
    ".psb",
)


@dataclass
class GameProfile:
    """描述目标 Galgame 的定位信息与补丁安装配置。"""

    name: str
    steam_app_id: Optional[int] = None
    install_dir_name: Optional[str] = None
    executable_name: Optional[str] = None
    target_subdir: str = ""


@dataclass
class PatchResult:
    """记录一次打补丁或回滚操作的结果。"""

    success: bool
    game_dir: Path
    installed_files: list[str] = field(default_factory=list)
    backed_up_files: list[str] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    message: str = ""
