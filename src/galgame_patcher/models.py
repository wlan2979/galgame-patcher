"""核心数据模型、异常定义与常见 Galgame 引擎补丁文件常量。"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

_SIZE_SPEC_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?)\s*$",
    re.IGNORECASE,
)
_SIZE_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "mib": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
    "gib": 1024 ** 3,
    "t": 1024 ** 4,
    "tb": 1024 ** 4,
    "tib": 1024 ** 4,
}


def parse_size_bytes(raw: Union[str, int]) -> int:
    """将体积字符串（如 '2G'、'500M'、'1024K'、'1.5GB'）或整数解析为字节数。"""
    if isinstance(raw, bool):
        raise ValueError(f"无效的体积大小: {raw!r}")
    if isinstance(raw, int):
        if raw < 0:
            raise ValueError(f"体积上限不能为负数: {raw}")
        return raw
    text = str(raw).strip()
    match = _SIZE_SPEC_RE.match(text)
    if not match:
        raise ValueError(
            f"无效的体积格式: {raw!r}（示例: 500M、2G、1024K、1048576）"
        )
    value = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = _SIZE_UNIT_MULTIPLIERS.get(unit)
    if multiplier is None:
        raise ValueError(f"不支持的体积单位: {raw!r}")
    result = int(value * multiplier)
    if result < 0:
        raise ValueError(f"体积上限不能为负数: {raw!r}")
    return result


def format_size_bytes(size_bytes: int) -> str:
    """将字节数格式化为人类可读体积字符串。"""
    size = max(0, int(size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{size} B"

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

# 这些格式一旦被复制进游戏目录就会被执行，是恶意软件最直接的载体，
# 永远不视为可自动下载安装的补丁。
BARE_EXECUTABLE_SUFFIXES: tuple[str, ...] = (
    ".exe",
    ".dll",
    ".msi",
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".js",
    ".scr",
    ".com",
    ".jar",
)


def file_sha256(
    file_path: Union[str, Path], *, chunk_size: int = 1024 * 1024
) -> str:
    """分块计算文件 SHA256 十六进制摘要（小写），文件不存在时抛出 FileNotFoundError。"""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"无法计算 SHA256，文件不存在: {path}")

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


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
            "无法以可写方式打开以下文件。请先退出游戏并检查文件权限、只读属性和杀毒软件：\n"
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


class UntrustedSourceError(RuntimeError):
    """拒绝从未经审核且未锁定哈希的远程 URL 直接安装补丁时抛出。"""

    def __init__(self, url: str, reason: str = "") -> None:
        self.url = url
        self.reason = reason
        reason_line = f"\n原因: {reason}" if reason else ""
        message = (
            f"拒绝从未经审核的远程地址直接安装补丁: {url}{reason_line}\n"
            "安全风险: 远程补丁可能包含恶意程序，工具无法验证发布者身份。\n"
            "请任选以下安全方式处理:\n"
            "  ① 在浏览器中自行下载补丁，再用本地文件路径安装（推荐）\n"
            "  ② 在清单中为该条目配置 expected_sha256，锁定文件内容后再安装\n"
            "  ③ 明确知晓风险时，命令行加 --allow-untrusted-url 参数"
        )
        super().__init__(message)


class VerificationError(ValueError):
    """当文件或清单的 SHA256 哈希校验或完整性校验失败时抛出。"""


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
    skipped_files: list[str] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    message: str = ""
    failed_files: list[str] = field(default_factory=list)
    failed_reasons: dict[str, str] = field(default_factory=dict)
    verified: bool = False
    verification_failed: bool = False
    pruned_snapshots: list[str] = field(default_factory=list)
    pruned_bytes: int = 0

    def failure_details(self) -> str:
        return "\n".join(f"  {path}: {reason}" for path, reason in self.failed_reasons.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "game_dir": str(self.game_dir),
            "installed_files": list(self.installed_files),
            "backed_up_files": list(self.backed_up_files),
            "skipped_files": list(self.skipped_files),
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "message": self.message,
            "failed_files": list(self.failed_files),
            "failed_reasons": dict(self.failed_reasons),
            "verified": self.verified,
            "verification_failed": self.verification_failed,
            "pruned_snapshots": list(self.pruned_snapshots),
            "pruned_bytes": self.pruned_bytes,
        }


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

    def to_dict(self, *, include_password: bool = False) -> dict[str, Any]:
        """序列化为可保存为 JSON 的字典；默认不输出明文解压密码。"""
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "steam_app_id": self.steam_app_id,
            "install_dir_name": self.install_dir_name,
            "executable_name": self.executable_name,
            "patch_url": self.patch_url,
            "patch_version": self.patch_version,
            "expected_sha256": self.expected_sha256,
            "target_subdir": self.target_subdir,
            "description": self.description,
            "last_verified": self.last_verified,
            "source": self.source,
        }
        if include_password:
            payload["archive_password"] = self.archive_password
        return payload


@dataclass
class InstalledGameStatus:
    """记录清单中的游戏在本机的安装与补丁状态。"""

    entry: CatalogEntry
    installed: bool
    game_dir: Optional[Path] = None
    patched: Optional[bool] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry.id,
            "name": self.entry.name,
            "steam_app_id": self.entry.steam_app_id,
            "install_dir_name": self.entry.install_dir_name,
            "executable_name": self.entry.executable_name,
            "patch_version": self.entry.patch_version,
            "source": self.entry.source,
            "installed": self.installed,
            "game_dir": str(self.game_dir) if self.game_dir else None,
            "patched": self.patched,
        }


INSTALL_STATE_LABELS: dict[str, str] = {
    "not_installed": "未安装补丁",
    "installed": "已安装补丁",
    "installed_no_history": "已安装补丁（不支持逐次撤回）",
    "incomplete": "上次安装未完成",
    "rolled_back": "已回滚",
    "invalid": "备份清单损坏",
}


@dataclass
class InstallStatus:
    """描述某个游戏目录当前的补丁安装状态，供状态查询与中断恢复使用。"""

    game_dir: Path
    state: str
    manifest_path: Optional[Path] = None
    latest_install: str = ""
    applied_at: str = ""
    backed_up_files: int = 0
    newly_added_files: int = 0
    created_dirs: int = 0
    history: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""
    backup_size_bytes: int = 0
    baseline_size_bytes: int = 0
    snapshot_count: int = 0

    @property
    def state_label(self) -> str:
        return INSTALL_STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_dir": str(self.game_dir),
            "state": self.state,
            "state_label": self.state_label,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
            "latest_install": self.latest_install,
            "applied_at": self.applied_at,
            "backed_up_files": self.backed_up_files,
            "newly_added_files": self.newly_added_files,
            "created_dirs": self.created_dirs,
            "backup_size_bytes": self.backup_size_bytes,
            "backup_size_human": format_size_bytes(self.backup_size_bytes),
            "baseline_size_bytes": self.baseline_size_bytes,
            "snapshot_count": self.snapshot_count,
            "history": [
                {"id": record_id, "applied_at": applied_at}
                for record_id, applied_at in self.history
            ],
            "message": self.message,
        }


@dataclass
class BackupPruneResult:
    """记录一次备份快照清理的结果。"""

    game_dir: Path
    removed_snapshots: list[str] = field(default_factory=list)
    kept_snapshots: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    remaining_bytes: int = 0
    baseline_preserved: bool = False
    baseline_bytes: int = 0
    dry_run: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_dir": str(self.game_dir),
            "removed_snapshots": list(self.removed_snapshots),
            "kept_snapshots": list(self.kept_snapshots),
            "freed_bytes": self.freed_bytes,
            "freed_human": format_size_bytes(self.freed_bytes),
            "remaining_bytes": self.remaining_bytes,
            "remaining_human": format_size_bytes(self.remaining_bytes),
            "baseline_preserved": self.baseline_preserved,
            "baseline_bytes": self.baseline_bytes,
            "baseline_human": format_size_bytes(self.baseline_bytes),
            "dry_run": self.dry_run,
            "message": self.message,
        }


@dataclass
class PatchFileInspection:
    """本地补丁文件核验结果，用于在安装前向用户展示体积、时间、压缩包支持度与 SHA256 摘要。"""

    path: Path
    size: int
    sha256: str
    modified_at: str
    is_archive: bool
    is_bare_executable: bool
    archive_format: str = ""
    rar_supported: Optional[bool] = None
    rar_support_detail: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size": self.size,
            "size_human": format_size_bytes(self.size),
            "sha256": self.sha256,
            "modified_at": self.modified_at,
            "is_archive": self.is_archive,
            "is_bare_executable": self.is_bare_executable,
            "archive_format": self.archive_format,
            "rar_supported": self.rar_supported,
            "rar_support_detail": self.rar_support_detail,
            "warnings": list(self.warnings),
        }

    def describe(self) -> str:
        """输出包含文件类型、大小、修改时间、SHA256 与前置提示的多行可读摘要。"""
        if self.is_bare_executable:
            kind = "裸可执行文件"
        elif self.is_archive:
            kind = "压缩包"
        else:
            kind = "补丁文件"

        size_mb = self.size / (1024 * 1024)
        lines = [
            f"文件: {self.path}",
            f"类型: {kind}",
            f"大小: {self.size} 字节 ({size_mb:.2f} MB)",
            f"修改时间: {self.modified_at}",
            f"SHA256: {self.sha256}",
        ]
        if self.rar_support_detail:
            lines.append(f"RAR 支持: {self.rar_support_detail}")
        for warning in self.warnings:
            lines.append(f"提示: {warning}")
        return "\n".join(lines)


@dataclass
class OnlinePatchCandidate:
    """表示从公开补丁网站/数据库爬取到的补丁候选结果。

    安全说明：爬取结果一律只作为「链接」展示，不参与自动下载安装。
    """

    title: str
    url: str
    source_site: str
    is_direct_download: bool = False
    version: str = ""
    language: str = ""
    official: bool = False
    description: str = ""
    trusted: bool = False
    trust_reason: str = "来源未经审核"
    bare_executable: bool = False
    manual_install: bool = False
