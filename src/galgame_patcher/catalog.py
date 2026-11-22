"""游戏补丁源清单（PatchCatalog）与本机已安装 Galgame 自动扫描。"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from ._version import __version__
from .locator import SteamLocator
from .models import CatalogEntry, InstalledGameStatus

# 内置清单仅提供一个格式示例，不收录任何具体作品或补丁链接。
# 请通过 PatchCatalog.load(<本地 JSON 路径或 URL>) 加载你自己维护的清单，
# 且只收录官方发布或已获授权的补丁。
DEFAULT_CATALOG_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "example_game",
        "name": "示例游戏（请导入你自己的清单）",
        "steam_app_id": None,
        "install_dir_name": "ExampleGame",
        "executable_name": "Game.exe",
        "patch_url": "",
        "patch_version": "1.0",
        "description": "格式示例条目。请只填写官方发布或已获授权的补丁链接。",
    },
]


class PatchCatalog:
    """管理游戏补丁源清单并自动扫描本地已安装的 Steam Galgame。"""

    def __init__(self, entries: Optional[Sequence[CatalogEntry]] = None) -> None:
        self.entries: list[CatalogEntry] = list(entries or ())

    @classmethod
    def default(cls) -> PatchCatalog:
        """加载内置默认游戏补丁配置模板清单。"""
        return cls.from_dict(DEFAULT_CATALOG_ENTRIES)

    @classmethod
    def from_dict(
        cls, data: Union[dict[str, Any], list[dict[str, Any]]]
    ) -> PatchCatalog:
        """从 JSON 解析出的字典或列表构造清单。"""
        if isinstance(data, dict):
            raw_entries = data.get("games") or data.get("entries") or []
        elif isinstance(data, list):
            raw_entries = data
        else:
            raise ValueError("清单 JSON 格式无效，应为对象或条目列表")

        entries = [CatalogEntry.from_dict(item) for item in raw_entries]
        return cls(entries=entries)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> PatchCatalog:
        """从本地 JSON 文件加载清单。"""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"清单文件不存在: {file_path}")
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_url(cls, url: str, timeout: float = 15.0) -> PatchCatalog:
        """从远程 HTTP/HTTPS/FILE URL 加载清单 JSON。"""
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"galgame-patcher/{__version__}"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8")
        data = json.loads(raw_text)
        return cls.from_dict(data)

    @classmethod
    def load(cls, source: Optional[Union[str, Path]] = None) -> PatchCatalog:
        """根据路径或 URL 自动加载清单，未指定时返回默认内置清单。"""
        if source is None or str(source).strip() == "":
            return cls.default()
        source_str = str(source).strip()
        if source_str.lower().startswith(("http://", "https://", "file://")):
            return cls.from_url(source_str)
        return cls.from_file(source_str)

    def save_to_file(self, path: Union[str, Path]) -> None:
        """将当前清单保存为 JSON 文件（可用于生成/编辑自定义清单）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "games": [entry.to_dict() for entry in self.entries],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def find_entry(self, query: Union[str, int]) -> Optional[CatalogEntry]:
        """按 Steam AppID、条目 id、游戏名称或目录名查找清单条目。"""
        query_str = str(query).strip()
        if not query_str:
            return None

        if isinstance(query, int) or query_str.isdigit():
            target_app_id = int(query_str)
            for entry in self.entries:
                if entry.steam_app_id == target_app_id:
                    return entry

        query_lower = query_str.lower()
        for entry in self.entries:
            if entry.id.lower() == query_lower:
                return entry

        for entry in self.entries:
            if entry.name.lower() == query_lower or (
                entry.install_dir_name
                and entry.install_dir_name.lower() == query_lower
            ):
                return entry

        for entry in self.entries:
            if query_lower in entry.name.lower() or (
                entry.install_dir_name
                and query_lower in entry.install_dir_name.lower()
            ):
                return entry

        return None

    def scan_games(
        self,
        locator: Optional[SteamLocator] = None,
        *,
        only_installed: bool = False,
    ) -> list[InstalledGameStatus]:
        """扫描本机已安装的清单游戏及其当前补丁状态。"""
        active_locator = locator or SteamLocator()
        results: list[InstalledGameStatus] = []

        for entry in self.entries:
            game_dir: Optional[Path] = None
            installed = False
            patched = False

            try:
                game_dir = active_locator.locate(entry.to_game_profile()).resolve()
                installed = True
                manifest_path = game_dir / ".galpatch_backup" / "manifest.json"
                patched = manifest_path.is_file()
            except FileNotFoundError:
                installed = False

            if only_installed and not installed:
                continue

            results.append(
                InstalledGameStatus(
                    entry=entry,
                    installed=installed,
                    game_dir=game_dir,
                    patched=patched,
                )
            )

        return results
