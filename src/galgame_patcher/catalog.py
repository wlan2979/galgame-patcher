"""游戏补丁源清单（PatchCatalog）、远程版本化更新、链接探活与本机游戏自动扫描（P2-2）。"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from .downloader import check_url_head, fetch_json
from .locator import ManualLocator, SteamLocator, deep_scan_games
from .models import (
    CatalogEntry,
    InstalledGameStatus,
    get_appdata_dir,
)

logger = logging.getLogger("galgame_patcher")

# 内置清单仅提供一个格式示例，不收录任何具体作品或补丁链接。
DEFAULT_CATALOG_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "example_game",
        "name": "示例游戏（请导入清单或使用自动搜索）",
        "steam_app_id": None,
        "install_dir_name": "ExampleGame",
        "executable_name": "Game.exe",
        "patch_url": "",
        "patch_version": "1.0",
        "description": "格式示例条目。请只填写官方发布或已获授权的补丁链接。",
        "last_verified": "",
        "source": "builtin",
    },
]


def _is_game_patched(game_dir: Path) -> Optional[bool]:
    """检查补丁清单，无法确认状态时返回 None。"""
    manifest_path = game_dir / ".galpatch_backup" / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("备份清单根节点应为对象")
        if data.get("status") == "in_progress":
            logger.warning("检测到未完成的补丁安装，请检查备份清单: %s", manifest_path)
            return None
        return data.get("status") != "rolled_back"
    except (OSError, ValueError) as exc:
        logger.warning("无法确认补丁状态，请检查备份清单 %s: %s", manifest_path, exc)
        return None


class PatchCatalog:
    """管理游戏补丁源清单、远程增量合并更新、链接校验及本机已安装游戏扫描。"""

    def __init__(
        self,
        entries: Optional[Sequence[CatalogEntry]] = None,
        *,
        version: int = 1,
    ) -> None:
        self.entries: list[CatalogEntry] = list(entries or ())
        self.version: int = int(version)

    @staticmethod
    def default_storage_path() -> Path:
        """返回 %APPDATA%\\galgame-patcher\\catalog.json 默认持久化路径。"""
        return get_appdata_dir() / "catalog.json"

    @classmethod
    def default(cls) -> PatchCatalog:
        """加载用户本地已更新的持久化清单，若不存在则加载内置默认清单模板。"""
        storage = cls.default_storage_path()
        if storage.is_file():
            try:
                return cls.from_file(storage)
            except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
                logger.warning("无法加载本地补丁清单 %s，已使用内置清单: %s", storage, exc)
        return cls.from_dict({"version": 1, "games": DEFAULT_CATALOG_ENTRIES}, default_source="builtin")

    @classmethod
    def from_dict(
        cls,
        data: Union[dict[str, Any], list[dict[str, Any]]],
        *,
        default_source: str = "builtin",
    ) -> PatchCatalog:
        """从 JSON 解析出的字典或列表构造清单。"""
        version = 1
        if isinstance(data, dict):
            raw_version = data.get("version")
            if raw_version is not None:
                version = int(raw_version)
            if "games" in data:
                raw_entries = data["games"]
            elif "entries" in data:
                raw_entries = data["entries"]
            else:
                raw_entries = []
        elif isinstance(data, list):
            raw_entries = data
        else:
            raise ValueError("清单 JSON 格式无效，应为对象或条目列表")

        if not isinstance(raw_entries, list) or not all(
            isinstance(item, dict) for item in raw_entries
        ):
            raise ValueError('清单 JSON 格式无效，games/entries 应为条目对象列表')

        entries = [
            CatalogEntry.from_dict(item, default_source=default_source)
            for item in raw_entries
        ]
        return cls(entries=entries, version=version)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> PatchCatalog:
        """从本地 JSON 文件加载清单。"""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"清单文件不存在: {file_path}")
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return cls.from_dict(data, default_source=str(file_path))

    @classmethod
    def from_url(cls, url: str, timeout: float = 15.0) -> PatchCatalog:
        """通过 downloader.py 的统一网络配置从远程 URL 加载清单 JSON（P2-2）。"""
        data = fetch_json(url, timeout=timeout)
        return cls.from_dict(data, default_source=url)

    @classmethod
    def load(cls, source: Optional[Union[str, Path]] = None) -> PatchCatalog:
        """根据路径或 URL 自动加载清单，未指定时返回默认清单。"""
        if source is None or str(source).strip() == "":
            return cls.default()
        source_str = str(source).strip()
        if source_str.lower().startswith(("http://", "https://", "file://")):
            return cls.from_url(source_str)
        return cls.from_file(source_str)

    def save_to_file(
        self,
        path: Optional[Union[str, Path]] = None,
        *,
        include_passwords: bool = True,
    ) -> Path:
        """将当前清单保存为 JSON 文件；导出分享时可关闭明文解压密码。"""
        target = Path(path) if path is not None else self.default_storage_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "games": [
                entry.to_dict(include_password=include_passwords)
                for entry in self.entries
            ],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def update_from_remote(
        self,
        url: str,
        *,
        target_path: Optional[Union[str, Path]] = None,
        timeout: float = 15.0,
    ) -> bool:
        """从远程 URL 拉取新版清单并增量合并到本地 catalog.json，保留 .bak 备份（P2-2）。

        - 校验远程 version 字段比当前 version 新才执行覆盖合并
        - 合并策略：同 id 的条目以远程为准，本地独有条目保留
        - 写入 %APPDATA%\\galgame-patcher\\catalog.json，并将合并前版本备份为 catalog.json.bak
        """
        remote_catalog = self.from_url(url, timeout=timeout)
        if remote_catalog.version <= self.version:
            return False

        storage_path = (
            Path(target_path)
            if target_path is not None
            else self.default_storage_path()
        )
        storage_path.parent.mkdir(parents=True, exist_ok=True)

        # 保存合并前的版本到 catalog.json.bak
        bak_path = storage_path.with_name(storage_path.name + ".bak")
        if storage_path.is_file():
            shutil.copy2(storage_path, bak_path)
        else:
            self.save_to_file(bak_path)

        remote_by_id: dict[str, CatalogEntry] = {}
        for entry in remote_catalog.entries:
            entry.source = url
            remote_by_id[entry.id] = entry

        merged: list[CatalogEntry] = []
        seen_ids: set[str] = set()

        # 保留现有顺序中的条目（若远程有同 id 则替换为远程版本，否则保留本地独有条目）
        for local_entry in self.entries:
            if local_entry.id in remote_by_id:
                merged.append(remote_by_id[local_entry.id])
                seen_ids.add(local_entry.id)
            else:
                merged.append(local_entry)
                seen_ids.add(local_entry.id)

        # 追加远程新增条目
        for remote_entry in remote_catalog.entries:
            if remote_entry.id not in seen_ids:
                merged.append(remote_entry)
                seen_ids.add(remote_entry.id)

        self.entries = merged
        self.version = remote_catalog.version
        self.save_to_file(storage_path)
        return True

    _SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

    def validate(self) -> tuple[list[str], list[str]]:
        """检查清单条目，返回 (错误列表, 警告列表)；错误会阻止保存。"""
        errors: list[str] = []
        warnings: list[str] = []
        seen_ids: set[str] = set()

        for index, entry in enumerate(self.entries, start=1):
            label = f"条目 #{index} [{entry.id.strip() or '无 id'}]"
            entry_id = entry.id.strip()
            if not entry_id:
                errors.append(f"{label}: id 不能为空")
            elif entry_id in seen_ids:
                errors.append(f"{label}: id 与其他条目重复")
            else:
                seen_ids.add(entry_id)

            if not entry.name.strip():
                errors.append(f"{label}: name 不能为空")

            if entry.steam_app_id is not None and entry.steam_app_id <= 0:
                errors.append(f"{label}: steam_app_id 必须为正整数")

            for field_name, value in (
                ("install_dir_name", entry.install_dir_name),
                ("executable_name", entry.executable_name),
            ):
                if value and ("/" in value or "\\" in value):
                    errors.append(f"{label}: {field_name} 不能包含路径分隔符")

            subdir = entry.target_subdir.strip()
            if subdir:
                sub_path = Path(subdir)
                if (
                    sub_path.is_absolute()
                    or sub_path.drive
                    or ".." in sub_path.parts
                ):
                    errors.append(
                        f"{label}: target_subdir 必须是游戏目录内的相对路径"
                    )

            url = entry.patch_url.strip()
            if url:
                if not url.lower().startswith(("http://", "https://")):
                    errors.append(f"{label}: patch_url 必须是 http(s) 链接")
                elif not entry.expected_sha256:
                    warnings.append(
                        f"{label}: 未配置 expected_sha256，远程补丁内容无法核验"
                    )

            if entry.expected_sha256 and not self._SHA256_PATTERN.match(
                entry.expected_sha256.strip()
            ):
                errors.append(
                    f"{label}: expected_sha256 必须是 64 位十六进制字符串"
                )

        return errors, warnings

    def verify_links(
        self, *, timeout: float = 10.0
    ) -> dict[str, tuple[bool, str]]:
        """对清单中的每个条目发起 HEAD 请求探活，更新 last_verified 并标记失效链接（P2-2）。"""
        today_iso = datetime.now(timezone.utc).date().isoformat()
        report: dict[str, tuple[bool, str]] = {}
        for entry in self.entries:
            if not entry.patch_url or not entry.patch_url.strip():
                report[entry.id] = (False, "未配置补丁链接")
                continue
            ok, detail = check_url_head(entry.patch_url, timeout=timeout)
            if ok:
                entry.last_verified = today_iso
            report[entry.id] = (ok, detail)
        return report

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
        include_manual: bool = False,
        deep_scan: bool = False,
        deep_scan_roots: Optional[Sequence[Union[str, Path]]] = None,
    ) -> list[InstalledGameStatus]:
        """扫描本机已安装的清单游戏、手工登记游戏以及可选的全盘非 Steam 候选游戏（P2-1）。"""
        active_locator = locator or SteamLocator()
        results: list[InstalledGameStatus] = []
        seen_dirs: set[Path] = set()

        # 深度扫描整个磁盘很慢，只做一次，所有条目共用结果（原先每个条目扫一遍）。
        deep_candidates: Optional[list[Path]] = None
        if deep_scan:
            deep_candidates = list(deep_scan_games(roots=deep_scan_roots))

        for entry in self.entries:
            game_dir: Optional[Path] = None
            installed = False
            patched = False

            try:
                game_dir = active_locator.locate(
                    entry.to_game_profile(),
                    include_manual=include_manual,
                    deep_scan=False,
                    deep_candidates=deep_candidates,
                ).resolve()
                installed = True
                seen_dirs.add(game_dir)
                patched = _is_game_patched(game_dir)
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

        # 追加 ManualLocator 中已登记但不在清单中的本地游戏
        if include_manual:
            manual_loc = active_locator.manual_locator or ManualLocator()
            for item in manual_loc.list_games():
                raw_dir = item.get("dir")
                if not raw_dir:
                    continue
                gdir = Path(str(raw_dir))
                if not gdir.is_dir():
                    continue
                resolved = gdir.resolve()
                if resolved in seen_dirs:
                    continue
                seen_dirs.add(resolved)
                gname = str(item.get("name") or resolved.name)
                exe_name = item.get("executable")
                patched = _is_game_patched(resolved)
                results.append(
                    InstalledGameStatus(
                        entry=CatalogEntry(
                            id=f"manual_{resolved.name.lower()}",
                            name=gname,
                            install_dir_name=resolved.name,
                            executable_name=str(exe_name) if exe_name else None,
                            source="manual",
                        ),
                        installed=True,
                        game_dir=resolved,
                        patched=patched,
                    )
                )

        # 追加深度扫描发现的非 Steam 本地 Galgame
        if deep_candidates is not None:
            for candidate_dir in deep_candidates:
                resolved = candidate_dir.resolve()
                if resolved in seen_dirs:
                    continue
                seen_dirs.add(resolved)
                patched = _is_game_patched(resolved)
                results.append(
                    InstalledGameStatus(
                        entry=CatalogEntry(
                            id=f"deep_{resolved.name.lower()}",
                            name=resolved.name,
                            install_dir_name=resolved.name,
                            source="deep_scan",
                        ),
                        installed=True,
                        game_dir=resolved,
                        patched=patched,
                    )
                )

        return results
