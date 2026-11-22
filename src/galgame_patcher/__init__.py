"""galgame-patcher：一键定位并给对应 Galgame 自动打补丁的 Python 库。"""

from __future__ import annotations

from .locator import SteamLocator, locate_game
from .models import COMMON_PATCH_EXTENSIONS, GameProfile, PatchResult
from .patcher import GalgamePatcher, apply_patch, compute_sha256, rollback_patch

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "COMMON_PATCH_EXTENSIONS",
    "GameProfile",
    "PatchResult",
    "SteamLocator",
    "GalgamePatcher",
    "locate_game",
    "apply_patch",
    "rollback_patch",
    "compute_sha256",
]
