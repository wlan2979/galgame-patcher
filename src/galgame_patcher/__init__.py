"""galgame-patcher：一键定位并给对应 Galgame 自动打补丁的 Python 库与桌面工具。"""

from __future__ import annotations

from .archive import extract_archive, is_supported_archive
from ._version import __version__
from .catalog import PatchCatalog
from .downloader import download_patch
from .locator import SteamLocator, locate_game
from .models import (
    COMMON_ARCHIVE_EXTENSIONS,
    COMMON_PATCH_EXTENSIONS,
    CatalogEntry,
    GameProfile,
    InstalledGameStatus,
    PatchResult,
)
from .patcher import GalgamePatcher, apply_patch, compute_sha256, rollback_patch


def launch_gui() -> int:
    """启动桌面 GUI（惰性导入 tkinter，缺失时不影响库与 CLI 的其余功能）。"""
    from .gui import launch_gui as _launch_gui

    return _launch_gui()

__all__ = [
    "__version__",
    "COMMON_PATCH_EXTENSIONS",
    "COMMON_ARCHIVE_EXTENSIONS",
    "GameProfile",
    "PatchResult",
    "CatalogEntry",
    "InstalledGameStatus",
    "SteamLocator",
    "PatchCatalog",
    "GalgamePatcher",
    "locate_game",
    "apply_patch",
    "rollback_patch",
    "compute_sha256",
    "download_patch",
    "extract_archive",
    "is_supported_archive",
    "launch_gui",
]
