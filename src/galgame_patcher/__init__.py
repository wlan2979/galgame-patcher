"""galgame-patcher：一键定位并给对应 Galgame 自动打补丁的 Python 库与桌面工具。"""

from __future__ import annotations

from .archive import extract_archive, is_supported_archive
from .catalog import PatchCatalog
from .downloader import download_patch
from .gui import launch_gui
from .locator import SteamLocator, locate_game
from .models import (
    COMMON_ARCHIVE_EXTENSIONS,
    COMMON_ARCHIVE_PASSWORDS,
    COMMON_PATCH_EXTENSIONS,
    CatalogEntry,
    GameProfile,
    InstalledGameStatus,
    PatchResult,
)
from .patcher import GalgamePatcher, apply_patch, compute_sha256, rollback_patch

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "COMMON_PATCH_EXTENSIONS",
    "COMMON_ARCHIVE_EXTENSIONS",
    "COMMON_ARCHIVE_PASSWORDS",
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
