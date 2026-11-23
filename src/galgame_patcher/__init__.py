"""galgame-patcher：一键定位、自动爬取并给对应 Galgame 安装/还原补丁的 Python 库与桌面工具。"""

from __future__ import annotations

from ._version import __version__
from .archive import extract_archive, is_supported_archive
from .catalog import PatchCatalog
from .downloader import (
    check_url_head,
    crawl_github_patch_releases,
    crawl_vndb_patches,
    discover_patches_online,
    download_patch,
    is_direct_patch_url,
    is_executable_url,
    scrape_patch_links_from_page,
)
from .locator import (
    ManualLocator,
    SteamLocator,
    deep_scan_games,
    is_likely_galgame_dir,
    iter_deep_scan_candidates,
    locate_game,
)
from .models import (
    BARE_EXECUTABLE_SUFFIXES,
    COMMON_ARCHIVE_EXTENSIONS,
    COMMON_PATCH_EXTENSIONS,
    GALGAME_ARCHIVE_INDICATORS,
    CatalogEntry,
    ConcurrencyError,
    GameProfile,
    GameRunningError,
    InstalledGameStatus,
    OnlinePatchCandidate,
    PatchFileInspection,
    PatchResult,
    UntrustedSourceError,
    get_appdata_dir,
)
from .patcher import (
    GalgamePatcher,
    apply_patch,
    compute_sha256,
    inspect_patch_file,
    rollback_patch,
)


def launch_gui() -> int:
    """启动桌面 GUI（惰性导入 tkinter，缺失时不影响库与 CLI 的其余功能）。"""
    from .gui import launch_gui as _launch_gui

    return _launch_gui()


__all__ = [
    "__version__",
    "COMMON_PATCH_EXTENSIONS",
    "GALGAME_ARCHIVE_INDICATORS",
    "COMMON_ARCHIVE_EXTENSIONS",
    "BARE_EXECUTABLE_SUFFIXES",
    "GameRunningError",
    "ConcurrencyError",
    "UntrustedSourceError",
    "GameProfile",
    "PatchResult",
    "PatchFileInspection",
    "CatalogEntry",
    "InstalledGameStatus",
    "OnlinePatchCandidate",
    "get_appdata_dir",
    "ManualLocator",
    "SteamLocator",
    "is_likely_galgame_dir",
    "iter_deep_scan_candidates",
    "deep_scan_games",
    "PatchCatalog",
    "GalgamePatcher",
    "locate_game",
    "apply_patch",
    "rollback_patch",
    "compute_sha256",
    "inspect_patch_file",
    "download_patch",
    "is_direct_patch_url",
    "is_executable_url",
    "check_url_head",
    "discover_patches_online",
    "crawl_vndb_patches",
    "crawl_github_patch_releases",
    "scrape_patch_links_from_page",
    "extract_archive",
    "is_supported_archive",
    "launch_gui",
]
