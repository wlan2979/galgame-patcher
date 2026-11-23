"""核心数据模型、异常定义与常见 Galgame 引擎补丁文件常量。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

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

# 用于非 Steam 深度扫描识别 Galgame 目录的专用资源后缀（排除通用的 .exe/.dll/.txt/.json 等）
GALGAME_ARCHIVE_INDICATORS: tuple[str, ...] = (
    ".xp3",
    ".arc",
    ".pck",
    ".pac",
    ".ypf",
    ".int",
    ".afs",
    ".cpk",
    ".rpa",
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


def get_appdata_dir() -> Path:
    """返回 galgame-patcher 的用户配置与日志目录（支持环境变量覆盖以便测试隔离）。"""
    override = os.environ.get("GALGAME_PATCHER_APPDATA")
    if override:
        base = Path(override)
    else:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (Path.home() / ".config")
    target = base / "galgame-patcher"
    target.mkdir(parents=True, exist_ok=True)
    return target


class GameRunningError(RuntimeError):
    """当目标文件被正在运行的游戏或其他进程锁定时抛出（P0-2）。"""

    def __init__(self, locked_files: Sequence[str]) -> None:
        self.locked_files = list(locked_files)
        joined = "\n  ".join(self.locked_files)
        message = (
            "以下文件正被其他程序占用，请先完全退出游戏（含 Steam 后台进程）：\n"
            f"  {joined}"
        )
        super().__init__(message)


class ConcurrencyError(RuntimeError):
    """当另一个 galgame-patcher 实例正在操作同一游戏目录时抛出（P3-3）。"""

    def __init__(self, pid: Optional[int], started_at: str = "") -> None:
        self.pid = pid
        self.started_at = started_at
        pid_text = f"PID {pid}" if pid is not None else "未知 PID"
        extra = f"，启动时间 {started_at}" if started_at else ""
        super().__init__(
            f"另一个 galgame-patcher 实例正在处理该目录（{pid_text}{extra}），请等待其完成"
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
    """记录一次打补丁、预览或回滚操作的结果。"""

    success: bool
    game_dir: Path
    installed_files: list[str] = field(default_factory=list)
    backed_up_files: list[str] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    message: str = ""
    failed_files: list[str] = field(default_factory=list)
    failed_reasons: dict[str, str] = field(default_factory=dict)
    verified: bool = False


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
    last_verified: str = ""
    source: str = "builtin"

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
    def from_dict(cls, data: dict[str, Any], default_source: str = "builtin") -> CatalogEntry:
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
            last_verified=str(data.get("last_verified") or ""),
            source=str(data.get("source") or default_source),
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
            "last_verified": self.last_verified,
            "source": self.source,
        }


@dataclass
class InstalledGameStatus:
    """记录清单中的游戏在本机的安装与补丁状态。"""

    entry: CatalogEntry
    installed: bool
    game_dir: Optional[Path] = None
    patched: bool = False


@dataclass
class OnlinePatchCandidate:
    """表示从公开补丁网站/数据库爬取到的补丁候选结果。"""

    title: str
    url: str
    source_site: str
    is_direct_download: bool = False
    version: str = ""
    language: str = ""
    official: bool = False
    description: str = ""
