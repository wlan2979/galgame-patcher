"""核心数据模型与常见 Galgame 引擎补丁文件常量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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

COMMON_ARCHIVE_EXTENSIONS: tuple[str, ...] = (
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tar.xz",
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


@dataclass
class CatalogEntry:
    """描述补丁源清单中的单款游戏及其远程补丁配置。"""

    id: str
    name: str
    steam_app_id: Optional[int] = None
    install_dir_name: Optional[str] = None
    executable_name: Optional[str] = None
    patch_url: str = ""
    patch_version: str = "1.0"
    expected_sha256: Optional[str] = None
    archive_password: Optional[str] = None
    target_subdir: str = ""
    description: str = ""

    def to_game_profile(self) -> GameProfile:
        """转换为用于目录定位与打补丁的 GameProfile 对象。"""
        return GameProfile(
            name=self.name,
            steam_app_id=self.steam_app_id,
            install_dir_name=self.install_dir_name,
            executable_name=self.executable_name,
            target_subdir=self.target_subdir,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogEntry:
        """从字典反序列化构造 CatalogEntry。"""
        raw_app_id = data.get("steam_app_id")
        app_id = int(raw_app_id) if raw_app_id not in (None, "") else None

        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            steam_app_id=app_id,
            install_dir_name=(
                str(data["install_dir_name"])
                if data.get("install_dir_name") is not None
                else None
            ),
            executable_name=(
                str(data["executable_name"])
                if data.get("executable_name") is not None
                else None
            ),
            patch_url=str(data.get("patch_url") or ""),
            patch_version=str(data.get("patch_version") or "1.0"),
            expected_sha256=(
                str(data["expected_sha256"])
                if data.get("expected_sha256")
                else None
            ),
            archive_password=(
                str(data["archive_password"])
                if data.get("archive_password")
                else None
            ),
            target_subdir=str(data.get("target_subdir") or ""),
            description=str(data.get("description") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可保存为 JSON 的字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "steam_app_id": self.steam_app_id,
            "install_dir_name": self.install_dir_name,
            "executable_name": self.executable_name,
            "patch_url": self.patch_url,
            "patch_version": self.patch_version,
            "expected_sha256": self.expected_sha256,
            "archive_password": self.archive_password,
            "target_subdir": self.target_subdir,
            "description": self.description,
        }


@dataclass
class InstalledGameStatus:
    """记录清单中的游戏在本机的安装与补丁状态。"""

    entry: CatalogEntry
    installed: bool
    game_dir: Optional[Path] = None
    patched: bool = False
