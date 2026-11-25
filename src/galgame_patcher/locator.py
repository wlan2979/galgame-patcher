"""自动扫描并定位手工登记目录、Steam 库及本地非 Steam Galgame 安装目录（P2-1）。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence, Union

from .models import GALGAME_ARCHIVE_INDICATORS, GameProfile, get_appdata_dir

logger = logging.getLogger("galgame_patcher")

PathLike = Union[str, os.PathLike, Path]
ScanProgressCallback = Callable[[Path, int], None]

_VDF_PATH_PATTERN = re.compile(r'"path"\s+"([^"]+)"', re.IGNORECASE)
_ACF_INSTALLDIR_PATTERN = re.compile(r'"installdir"\s+"([^"]+)"', re.IGNORECASE)
_ACF_APPID_PATTERN = re.compile(r'"appid"\s+"(\d+)"', re.IGNORECASE)
_ACF_NAME_PATTERN = re.compile(r'"name"\s+"([^"]+)"', re.IGNORECASE)

_CHARACTERISTIC_GALGAME_SUBDIRS: frozenset[str] = frozenset(
    {
        "script",
        "scripts",
        "data",
        "gamedata",
        "sound",
        "bgm",
        "voice",
        "movie",
        "video",
        "cg",
        "archive",
    }
)

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "windows",
        "$recycle.bin",
        "system volume information",
        "programdata",
        "appdata",
        "node_modules",
        ".git",
        "__pycache__",
        ".galpatch_backup",
        "venv",
        ".venv",
        "recovery",
        "perflogs",
    }
)


def is_likely_galgame_dir(directory: Path) -> bool:
    """判断某个目录是否具有典型 Galgame 目录特征（含引擎封包文件，或含 .exe + 特征子目录）。"""
    if not directory.is_dir():
        return False
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.debug("无法检查游戏目录 %s: %s", directory, exc)
        return False

    has_exe = False
    has_characteristic_subdir = False

    for item in entries:
        try:
            if item.is_file():
                suffix = item.suffix.lower()
                if suffix in GALGAME_ARCHIVE_INDICATORS:
                    return True
                if suffix == ".exe":
                    has_exe = True
            elif item.is_dir():
                if item.name.lower() in _CHARACTERISTIC_GALGAME_SUBDIRS:
                    has_characteristic_subdir = True
                    # 检查第一层特征子目录下是否有引擎封包文件
                    try:
                        for sub in item.iterdir():
                            if (
                                sub.is_file()
                                and sub.suffix.lower() in GALGAME_ARCHIVE_INDICATORS
                            ):
                                return True
                    except OSError:
                        pass
        except OSError:
            continue

    return has_exe and has_characteristic_subdir


def infer_largest_executable(directory: Path) -> Optional[str]:
    """在指定目录顶层查找体积最大的 .exe 文件名。"""
    if not directory.is_dir():
        return None
    largest_name: Optional[str] = None
    largest_size = -1
    try:
        for item in directory.iterdir():
            if item.is_file() and item.suffix.lower() == ".exe":
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                if size > largest_size:
                    largest_size = size
                    largest_name = item.name
    except OSError:
        return None
    return largest_name


class ManualLocator:
    """管理用户手工登记在 %APPDATA%\\galgame-patcher\\games.json 中的游戏目录（P2-1）。"""

    def __init__(self, config_path: Optional[PathLike] = None) -> None:
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else get_appdata_dir() / "games.json"
        )

    def list_games(self) -> list[dict[str, Any]]:
        """读取所有已登记的游戏列表。"""
        if not self.config_path.is_file():
            return []
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw_list = data.get("games", [])
            elif isinstance(data, list):
                raw_list = data
            else:
                raise ValueError("根节点应为列表或对象")
            if not isinstance(raw_list, list):
                raise ValueError("games 应为列表")
            return [item for item in raw_list if isinstance(item, dict)]
        except (OSError, ValueError) as exc:
            logger.warning("无法读取手工登记清单 %s: %s", self.config_path, exc)
            return []

    def save_games(self, games: Sequence[dict[str, Any]]) -> None:
        """保存已登记的游戏列表到 games.json。"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "games": list(games)}
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_game(
        self,
        path: PathLike,
        *,
        name: Optional[str] = None,
        executable: Optional[str] = None,
    ) -> dict[str, Any]:
        """登记一个本地游戏目录，自动推断游戏名与主程序文件名。"""
        game_dir = Path(path).resolve()
        if not game_dir.is_dir():
            raise FileNotFoundError(f"要登记的游戏目录不存在: {game_dir}")

        inferred_name = (name or "").strip() or game_dir.name
        inferred_exe = (executable or "").strip() or infer_largest_executable(game_dir)

        entry: dict[str, Any] = {
            "name": inferred_name,
            "dir": str(game_dir),
            "executable": inferred_exe,
        }

        existing = self.list_games()
        updated: list[dict[str, Any]] = []
        replaced = False
        for item in existing:
            item_dir = str(item.get("dir") or "")
            if (
                Path(item_dir).resolve() == game_dir
                if item_dir
                else False
            ) or str(item.get("name") or "").lower() == inferred_name.lower():
                if not replaced:
                    updated.append(entry)
                    replaced = True
            else:
                updated.append(item)

        if not replaced:
            updated.append(entry)

        self.save_games(updated)
        return entry

    def find_by_name(
        self, name: str, executable_name: Optional[str] = None
    ) -> Optional[Path]:
        """在手工登记列表中按名称或目录名匹配游戏路径。"""
        if not name or not name.strip():
            return None
        target = name.strip()
        target_lower = target.lower()

        exact: list[Path] = []
        casefold: list[Path] = []
        substring: list[Path] = []

        for item in self.list_games():
            raw_dir = item.get("dir")
            if not raw_dir:
                continue
            game_dir = Path(str(raw_dir))
            if not game_dir.is_dir():
                continue
            if executable_name and not (game_dir / executable_name).is_file():
                continue

            item_name = str(item.get("name") or game_dir.name)
            for candidate_str in (item_name, game_dir.name):
                c_lower = candidate_str.lower()
                if candidate_str == target:
                    exact.append(game_dir.resolve())
                    break
                if c_lower == target_lower:
                    casefold.append(game_dir.resolve())
                    break
                if target_lower in c_lower:
                    substring.append(game_dir.resolve())
                    break

        for bucket in (exact, casefold, substring):
            if bucket:
                return bucket[0]
        return None


def iter_deep_scan_candidates(
    roots: Optional[Sequence[PathLike]] = None,
    *,
    max_depth: int = 3,
    progress_callback: Optional[ScanProgressCallback] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Iterator[Path]:
    """可中断的生成器：遍历指定根目录（默认 C:/D:/E:，深度 <= 3）发现非 Steam Galgame 目录（P2-1）。"""
    if roots is not None:
        search_roots = []
        for path in map(Path, roots):
            if path.is_dir():
                search_roots.append(path)
            else:
                logger.warning("深度扫描目录不存在或不可访问: %s", path)
    elif sys.platform == "win32":
        search_roots = [
            Path(f"{drive}:\\")
            for drive in ("C", "D", "E")
            if Path(f"{drive}:\\").is_dir()
        ]
    else:
        search_roots = [Path.home()]

    seen: set[Path] = set()
    scanned_count = 0

    def _walk(current: Path, depth: int) -> Iterator[Path]:
        nonlocal scanned_count
        if cancel_check is not None and cancel_check():
            return

        scanned_count += 1
        if progress_callback is not None:
            progress_callback(current, scanned_count)

        if is_likely_galgame_dir(current):
            try:
                resolved = current.resolve()
            except OSError:
                resolved = current
            if resolved not in seen:
                seen.add(resolved)
                yield resolved
            return

        if depth >= max_depth:
            return

        try:
            children = sorted(
                (p for p in current.iterdir() if p.is_dir() and not p.is_symlink()),
                key=lambda p: p.name.lower(),
            )
        except OSError as exc:
            logger.log(logging.WARNING if depth == 0 else logging.DEBUG,
                       "无法扫描目录 %s: %s", current, exc)
            return

        for child in children:
            if cancel_check is not None and cancel_check():
                return
            if child.name.lower() in _SKIP_DIR_NAMES or child.name.startswith("."):
                continue
            yield from _walk(child, depth + 1)

    for root in search_roots:
        if cancel_check is not None and cancel_check():
            return
        yield from _walk(root, 0)


def deep_scan_games(
    roots: Optional[Sequence[PathLike]] = None,
    *,
    max_depth: int = 3,
    progress_callback: Optional[ScanProgressCallback] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[Path]:
    """执行非 Steam 深度扫描，返回去重并按目录名排序的候选 Galgame 目录列表。"""
    candidates = list(
        iter_deep_scan_candidates(
            roots=roots,
            max_depth=max_depth,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
    )
    return sorted(candidates, key=lambda p: p.name.lower())


class SteamLocator:
    """用于自动发现手工登记目录、Steam 多库路径及非 Steam 深度扫描目录的定位器。"""

    def __init__(
        self,
        steam_root: Optional[PathLike] = None,
        extra_search_dirs: Optional[Sequence[PathLike]] = None,
        manual_locator: Optional[ManualLocator] = None,
        deep_scan: bool = False,
        deep_scan_roots: Optional[Sequence[PathLike]] = None,
    ) -> None:
        """初始化定位器，支持组合 ManualLocator、Steam 库与可选深度扫描。"""
        self._explicit_steam_root = Path(steam_root) if steam_root is not None else None
        self.extra_search_dirs: list[Path] = [
            Path(p) for p in (extra_search_dirs or ())
        ]
        self.manual_locator = manual_locator or ManualLocator()
        self.deep_scan = deep_scan
        self.deep_scan_roots: Optional[list[Path]] = (
            [Path(p) for p in deep_scan_roots] if deep_scan_roots is not None else None
        )
        if self._explicit_steam_root is not None and not self._explicit_steam_root.is_dir():
            logger.warning("指定的 Steam 目录不存在或不可访问: %s", self._explicit_steam_root)

    def detect_steam_root(self) -> Optional[Path]:
        """自动探测本地 Steam 根目录（优先指定路径，其次 Windows 注册表与默认路径）。"""
        if self._explicit_steam_root is not None:
            return (
                self._explicit_steam_root
                if self._explicit_steam_root.is_dir()
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
            except OSError as exc:
                logger.warning("无法读取 Steam 库配置 %s: %s", vdf_path, exc)

        return libraries

    def list_installed_steam_manifests(self) -> list[dict[str, Any]]:
        """扫描所有 Steam 库中的 appmanifest_*.acf，返回本机已安装的 Steam 游戏列表。"""
        discovered: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for library in self.get_library_folders():
            steamapps_dir = library / "steamapps"
            if not steamapps_dir.is_dir():
                continue
            try:
                acf_files = sorted(steamapps_dir.glob("appmanifest_*.acf"))
            except OSError as exc:
                logger.warning("无法列出 Steam 游戏清单 %s: %s", steamapps_dir, exc)
                continue
            for manifest_path in acf_files:
                try:
                    content = manifest_path.read_text(encoding="utf-8", errors="ignore")
                except OSError as exc:
                    logger.warning("无法读取 Steam 游戏清单 %s: %s", manifest_path, exc)
                    continue
                id_match = _ACF_APPID_PATTERN.search(content)
                dir_match = _ACF_INSTALLDIR_PATTERN.search(content)
                name_match = _ACF_NAME_PATTERN.search(content)
                if not id_match or not dir_match:
                    logger.warning("Steam 游戏清单缺少 App ID 或安装目录: %s", manifest_path)
                    continue
                app_id = int(id_match.group(1))
                if app_id in seen_ids:
                    continue
                install_dir_name = dir_match.group(1).strip()
                game_dir = steamapps_dir / "common" / install_dir_name
                if not game_dir.is_dir():
                    continue
                seen_ids.add(app_id)
                discovered.append(
                    {
                        "steam_app_id": app_id,
                        "name": (
                            name_match.group(1).strip()
                            if name_match
                            else install_dir_name
                        ),
                        "install_dir_name": install_dir_name,
                        "game_dir": game_dir.resolve(),
                    }
                )
        return discovered

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
            except OSError as exc:
                logger.warning("无法读取 Steam 游戏清单 %s: %s", manifest_path, exc)
                continue

            match = _ACF_INSTALLDIR_PATTERN.search(content)
            if not match:
                logger.warning("Steam 游戏清单缺少安装目录: %s", manifest_path)
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
            except OSError as exc:
                logger.warning("无法列出游戏搜索目录 %s: %s", root, exc)
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
        include_manual: bool = True,
        deep_scan: Optional[bool] = None,
        deep_scan_roots: Optional[Sequence[PathLike]] = None,
        deep_candidates: Optional[Sequence[Path]] = None,
    ) -> Path:
        """按 ManualLocator -> SteamLocator -> 深度扫描候选 的顺序定位游戏目录（P2-1）。

        deep_candidates 允许调用方复用一次深度扫描的结果，避免批量定位时重复扫描磁盘。
        """
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

        # 1. ManualLocator 手工登记目录查找
        if include_manual and self.manual_locator is not None:
            for candidate_name in (resolved_install_dir, resolved_game_name):
                if candidate_name:
                    manual_found = self.manual_locator.find_by_name(
                        candidate_name, executable_name=resolved_exe
                    )
                    if manual_found is not None:
                        return manual_found

        # 2. SteamLocator 查找 (AppID -> install_dir_name -> game_name)
        if resolved_app_id is not None:
            found = self.find_by_app_id(resolved_app_id, executable_name=resolved_exe)
            if found is not None:
                return found

        for candidate_name in (resolved_install_dir, resolved_game_name):
            if candidate_name:
                found = self.find_by_name(candidate_name, executable_name=resolved_exe)
                if found is not None:
                    return found

        # 3. 深度扫描候选目录查找
        use_deep = self.deep_scan if deep_scan is None else deep_scan
        roots_to_use = (
            deep_scan_roots
            if deep_scan_roots is not None
            else self.deep_scan_roots
        )
        if deep_candidates is not None:
            candidates: Sequence[Path] = deep_candidates
        elif use_deep or roots_to_use is not None:
            candidates = list(iter_deep_scan_candidates(roots=roots_to_use))
        else:
            candidates = ()
        if candidates:
            for candidate_dir in candidates:
                if not self._is_valid_game_dir(candidate_dir, resolved_exe):
                    continue
                for candidate_name in (resolved_install_dir, resolved_game_name):
                    if not candidate_name:
                        continue
                    if (
                        candidate_dir.name.lower() == candidate_name.lower()
                        or candidate_name.lower() in candidate_dir.name.lower()
                    ):
                        return candidate_dir

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
    manual_locator: Optional[ManualLocator] = None,
    deep_scan: bool = False,
    deep_scan_roots: Optional[Sequence[PathLike]] = None,
) -> Path:
    """快速定位 Galgame 安装目录的便捷函数。"""
    locator = SteamLocator(
        steam_root=steam_root,
        extra_search_dirs=extra_search_dirs,
        manual_locator=manual_locator,
        deep_scan=deep_scan,
        deep_scan_roots=deep_scan_roots,
    )
    return locator.locate(
        profile=profile,
        app_id=app_id,
        game_name=game_name,
        install_dir_name=install_dir_name,
        executable_name=executable_name,
    )
