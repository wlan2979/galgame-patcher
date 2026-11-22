"""自动扫描并定位 Steam 库及本地 Galgame 安装目录。"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional, Sequence, Union

from .models import GameProfile

PathLike = Union[str, os.PathLike, Path]

_VDF_PATH_PATTERN = re.compile(r'"path"\s+"([^"]+)"', re.IGNORECASE)
_ACF_INSTALLDIR_PATTERN = re.compile(r'"installdir"\s+"([^"]+)"', re.IGNORECASE)


class SteamLocator:
    """用于自动发现 Steam 安装目录、多库路径及目标 Galgame 目录的定位器。"""

    def __init__(
        self,
        steam_root: Optional[PathLike] = None,
        extra_search_dirs: Optional[Sequence[PathLike]] = None,
    ) -> None:
        """初始化定位器，可指定自定义 Steam 根目录和额外搜索目录。"""
        self._explicit_steam_root = Path(steam_root) if steam_root is not None else None
        self.extra_search_dirs: list[Path] = [
            Path(p) for p in (extra_search_dirs or ())
        ]

    def detect_steam_root(self) -> Optional[Path]:
        """自动探测本地 Steam 根目录（优先指定路径，其次 Windows 注册表与默认路径）。"""
        if self._explicit_steam_root is not None:
            return (
                self._explicit_steam_root
                if self._explicit_steam_root.exists()
                else None
            )

        candidates: list[Path] = []

        if sys.platform == "win32":
            try:
                import winreg

                registry_locations = (
                    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\WOW6432Node\Valve\Steam",
                        "InstallPath",
                    ),
                    (
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Valve\Steam",
                        "InstallPath",
                    ),
                )
                for hkey, subkey, value_name in registry_locations:
                    try:
                        with winreg.OpenKey(hkey, subkey) as key:
                            value, _ = winreg.QueryValueEx(key, value_name)
                            if value:
                                candidates.append(Path(str(value)))
                    except OSError:
                        continue
            except ImportError:
                pass

            for env_var in ("ProgramFiles(x86)", "ProgramFiles"):
                base = os.environ.get(env_var)
                if base:
                    candidates.append(Path(base) / "Steam")
        else:
            home = Path.home()
            candidates.extend(
                [
                    home / ".local" / "share" / "Steam",
                    home / ".steam" / "steam",
                    home / "Library" / "Application Support" / "Steam",
                ]
            )

        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return None

    def get_library_folders(self) -> list[Path]:
        """解析 libraryfolders.vdf，返回所有有效的 Steam 库根目录列表。"""
        steam_root = self.detect_steam_root()
        if steam_root is None:
            return []

        libraries: list[Path] = []
        seen: set[Path] = set()

        def _add_if_valid(path: Path) -> None:
            resolved = path.resolve()
            if resolved.is_dir() and resolved not in seen:
                seen.add(resolved)
                libraries.append(resolved)

        _add_if_valid(steam_root)

        vdf_path = steam_root / "steamapps" / "libraryfolders.vdf"
        if vdf_path.is_file():
            try:
                content = vdf_path.read_text(encoding="utf-8", errors="ignore")
                for match in _VDF_PATH_PATTERN.finditer(content):
                    raw_path = match.group(1).replace("\\\\", "\\")
                    _add_if_valid(Path(raw_path))
            except OSError:
                pass

        return libraries

    def find_by_app_id(
        self, app_id: int, executable_name: Optional[str] = None
    ) -> Optional[Path]:
        """根据 Steam AppID 读取 appmanifest_<app_id>.acf 并定位游戏目录。"""
        manifest_name = f"appmanifest_{app_id}.acf"
        for library in self.get_library_folders():
            steamapps_dir = library / "steamapps"
            manifest_path = steamapps_dir / manifest_name
            if not manifest_path.is_file():
                continue

            try:
                content = manifest_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            match = _ACF_INSTALLDIR_PATTERN.search(content)
            if not match:
                continue

            install_dir_name = match.group(1).strip()
            game_dir = steamapps_dir / "common" / install_dir_name
            if self._is_valid_game_dir(game_dir, executable_name):
                return game_dir
        return None

    def find_by_name(
        self, name: str, executable_name: Optional[str] = None
    ) -> Optional[Path]:
        """按目录名或游戏名称在 Steam 库 common 目录及额外搜索目录中匹配游戏路径。"""
        if not name or not name.strip():
            return None

        target = name.strip()
        target_lower = target.lower()
        search_roots = self._collect_search_roots()

        exact_matches: list[Path] = []
        casefold_matches: list[Path] = []
        substring_matches: list[Path] = []

        for root in search_roots:
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue

            for entry in entries:
                if not entry.is_dir():
                    continue
                if not self._is_valid_game_dir(entry, executable_name):
                    continue

                entry_name = entry.name
                entry_lower = entry_name.lower()
                if entry_name == target:
                    exact_matches.append(entry)
                elif entry_lower == target_lower:
                    casefold_matches.append(entry)
                elif target_lower in entry_lower:
                    substring_matches.append(entry)

        for bucket in (exact_matches, casefold_matches, substring_matches):
            if bucket:
                return bucket[0]
        return None

    def locate(
        self,
        profile: Optional[GameProfile] = None,
        *,
        app_id: Optional[int] = None,
        game_name: Optional[str] = None,
        install_dir_name: Optional[str] = None,
        executable_name: Optional[str] = None,
    ) -> Path:
        """按 AppID -> install_dir_name -> 游戏名的优先级定位游戏安装目录，未找到则抛异常。"""
        resolved_app_id = app_id if app_id is not None else (
            profile.steam_app_id if profile else None
        )
        resolved_install_dir = install_dir_name or (
            profile.install_dir_name if profile else None
        )
        resolved_game_name = game_name or (profile.name if profile else None)
        resolved_exe = executable_name or (
            profile.executable_name if profile else None
        )

        if resolved_app_id is not None:
            found = self.find_by_app_id(resolved_app_id, executable_name=resolved_exe)
            if found is not None:
                return found

        for candidate_name in (resolved_install_dir, resolved_game_name):
            if candidate_name:
                found = self.find_by_name(candidate_name, executable_name=resolved_exe)
                if found is not None:
                    return found

        hints: list[str] = []
        if resolved_app_id is not None:
            hints.append(f"app_id={resolved_app_id}")
        if resolved_install_dir:
            hints.append(f"install_dir_name={resolved_install_dir!r}")
        if resolved_game_name:
            hints.append(f"game_name={resolved_game_name!r}")
        hint_text = ", ".join(hints) if hints else "未提供定位条件"
        raise FileNotFoundError(f"未能定位到游戏安装目录（{hint_text}）")

    def _collect_search_roots(self) -> list[Path]:
        """收集 Steam common 目录和额外自定义父目录。"""
        roots: list[Path] = []
        seen: set[Path] = set()

        for library in self.get_library_folders():
            common_dir = library / "steamapps" / "common"
            if common_dir.is_dir():
                resolved = common_dir.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    roots.append(resolved)

        for extra in self.extra_search_dirs:
            if extra.is_dir():
                resolved = extra.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    roots.append(resolved)

        return roots

    @staticmethod
    def _is_valid_game_dir(
        game_dir: Path, executable_name: Optional[str] = None
    ) -> bool:
        """校验目录存在且包含指定主程序（如果提供了 executable_name）。"""
        if not game_dir.is_dir():
            return False
        if executable_name:
            return (game_dir / executable_name).is_file()
        return True


def locate_game(
    profile: Optional[GameProfile] = None,
    *,
    app_id: Optional[int] = None,
    game_name: Optional[str] = None,
    install_dir_name: Optional[str] = None,
    executable_name: Optional[str] = None,
    steam_root: Optional[PathLike] = None,
    extra_search_dirs: Optional[Sequence[PathLike]] = None,
) -> Path:
    """快速定位 Galgame 安装目录的便捷函数。"""
    locator = SteamLocator(
        steam_root=steam_root,
        extra_search_dirs=extra_search_dirs,
    )
    return locator.locate(
        profile=profile,
        app_id=app_id,
        game_name=game_name,
        install_dir_name=install_dir_name,
        executable_name=executable_name,
    )
