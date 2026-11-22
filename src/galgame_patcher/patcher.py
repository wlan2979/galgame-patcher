"""Galgame 补丁安装、在线下载、校验、自动备份与一键回滚核心逻辑。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Sequence, Union

from .archive import extract_archive, is_supported_archive
from .catalog import PatchCatalog
from .downloader import ProgressCallback, download_patch, is_url
from .locator import SteamLocator
from .models import CatalogEntry, GameProfile, PatchResult

_CHUNK_SIZE = 1024 * 1024


def compute_sha256(file_path: Union[str, Path]) -> str:
    """分块计算指定文件的 SHA256 十六进制摘要（小写）。"""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"无法计算 SHA256，文件不存在: {path}")

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


class GalgamePatcher:
    """Galgame 补丁安装器，支持自动定位、在线下载、密码解压、备份与回滚。"""

    BACKUP_FOLDER_NAME = ".galpatch_backup"

    def __init__(self, locator: Optional[SteamLocator] = None) -> None:
        """初始化补丁安装器，可注入自定义 SteamLocator 实例。"""
        self.locator = locator or SteamLocator()

    def apply(
        self,
        patch_source: Union[str, Path],
        game_dir: Optional[Union[str, Path]] = None,
        *,
        profile: Optional[GameProfile] = None,
        app_id: Optional[int] = None,
        game_name: Optional[str] = None,
        target_subdir: str = "",
        backup: bool = True,
        expected_sha256: Optional[str] = None,
        strip_single_top_dir: bool = True,
        password: Optional[str] = None,
        password_candidates: Optional[Sequence[str]] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> PatchResult:
        """将本地文件/目录/压缩包或远程 URL 补丁一键安装到目标 Galgame 目录。"""
        if is_url(patch_source):
            source_path = download_patch(
                str(patch_source),
                expected_sha256=expected_sha256,
                progress_callback=progress_callback,
            )
        else:
            source_path = Path(patch_source)
            if not source_path.exists():
                raise FileNotFoundError(f"补丁源路径不存在: {source_path}")

        if expected_sha256 is not None:
            if not source_path.is_file():
                raise ValueError("仅支持对单个补丁文件或压缩包进行 SHA256 校验")
            actual_hash = compute_sha256(source_path)
            expected_normalized = expected_sha256.strip().lower()
            if actual_hash != expected_normalized:
                raise ValueError(
                    f"补丁 SHA256 校验失败: 预期 {expected_normalized}，实际 {actual_hash}"
                )

        resolved_game_dir = self._resolve_game_dir(
            game_dir=game_dir,
            profile=profile,
            app_id=app_id,
            game_name=game_name,
        )

        resolved_subdir = target_subdir or (profile.target_subdir if profile else "")
        dest_root = self._resolve_dest_root(resolved_game_dir, resolved_subdir)
        dest_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="galpatch_") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            file_pairs = self._collect_patch_files(
                source_path=source_path,
                dest_root=dest_root,
                temp_dir=temp_dir,
                strip_single_top_dir=strip_single_top_dir,
                password=password,
                password_candidates=password_candidates,
            )

            return self._install_files(
                file_pairs=file_pairs,
                game_dir=resolved_game_dir,
                backup=backup,
            )

    def apply_from_catalog(
        self,
        entry_or_query: Union[CatalogEntry, str, int],
        *,
        catalog: Optional[PatchCatalog] = None,
        game_dir: Optional[Union[str, Path]] = None,
        backup: bool = True,
        password: Optional[str] = None,
        password_candidates: Optional[Sequence[str]] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> PatchResult:
        """从补丁源清单中检索游戏条目，自动下载并安装对应补丁。"""
        if isinstance(entry_or_query, CatalogEntry):
            entry = entry_or_query
        else:
            active_catalog = catalog or PatchCatalog.default()
            found = active_catalog.find_entry(entry_or_query)
            if found is None:
                raise LookupError(f"清单中未找到匹配的游戏条目: {entry_or_query!r}")
            entry = found

        if not entry.patch_url or not entry.patch_url.strip():
            raise ValueError(
                f"游戏 '{entry.name}' 尚未配置补丁下载链接 (patch_url)，"
                "请导入包含有效链接的自定义清单 JSON 或手动选择本地补丁文件。"
            )

        return self.apply(
            patch_source=entry.patch_url,
            game_dir=game_dir,
            profile=entry.to_game_profile(),
            target_subdir=entry.target_subdir,
            backup=backup,
            expected_sha256=entry.expected_sha256,
            password=password if password is not None else entry.archive_password,
            password_candidates=password_candidates,
            progress_callback=progress_callback,
        )

    def rollback(self, game_dir: Union[str, Path]) -> PatchResult:
        """根据备份清单将游戏目录还原至打补丁前的状态并清理备份。"""
        resolved_game_dir = Path(game_dir).resolve()
        if not resolved_game_dir.is_dir():
            raise FileNotFoundError(f"游戏目录不存在: {resolved_game_dir}")

        backup_root = resolved_game_dir / self.BACKUP_FOLDER_NAME
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"未找到补丁备份清单: {manifest_path}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backed_up_files: list[str] = manifest.get("backed_up_files", [])
        newly_added_files: list[str] = manifest.get("newly_added_files", [])
        backup_files_root = backup_root / "files"

        restored: list[str] = []
        for rel_posix in backed_up_files:
            rel_path = Path(rel_posix)
            backup_file = backup_files_root / rel_path
            target_file = resolved_game_dir / rel_path
            if backup_file.is_file():
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_file, target_file)
                restored.append(rel_posix)

        for rel_posix in newly_added_files:
            target_file = resolved_game_dir / Path(rel_posix)
            if target_file.is_file():
                target_file.unlink()
                self._cleanup_empty_parents(target_file.parent, resolved_game_dir)

        shutil.rmtree(backup_root, ignore_errors=True)

        return PatchResult(
            success=True,
            game_dir=resolved_game_dir,
            installed_files=restored,
            backed_up_files=[],
            backup_dir=None,
            message=(
                f"已成功回滚补丁：还原 {len(restored)} 个原文件，"
                f"移除 {len(newly_added_files)} 个新增文件。"
            ),
        )

    def _resolve_game_dir(
        self,
        *,
        game_dir: Optional[Union[str, Path]],
        profile: Optional[GameProfile],
        app_id: Optional[int],
        game_name: Optional[str],
    ) -> Path:
        if game_dir is not None:
            resolved = Path(game_dir).resolve()
            if not resolved.is_dir():
                raise FileNotFoundError(f"指定的游戏目录不存在: {resolved}")
            return resolved

        return self.locator.locate(
            profile=profile,
            app_id=app_id,
            game_name=game_name,
        ).resolve()

    @staticmethod
    def _resolve_dest_root(game_dir: Path, target_subdir: str) -> Path:
        cleaned = target_subdir.strip()
        if not cleaned:
            return game_dir

        dest_root = (game_dir / cleaned).resolve()
        try:
            dest_root.relative_to(game_dir)
        except ValueError as exc:
            raise ValueError(f"目标子目录越界: {target_subdir!r}") from exc
        return dest_root

    def _collect_patch_files(
        self,
        *,
        source_path: Path,
        dest_root: Path,
        temp_dir: Path,
        strip_single_top_dir: bool,
        password: Optional[str],
        password_candidates: Optional[Sequence[str]],
    ) -> list[tuple[Path, Path]]:
        """将各种补丁源解析为 (源文件, 目标路径) 映射列表。"""
        if source_path.is_dir():
            return self._walk_source_dir(source_path, dest_root)

        if is_supported_archive(source_path):
            extract_archive(
                source_path,
                temp_dir,
                password=password,
                password_candidates=password_candidates,
            )
            effective_root = temp_dir
            if strip_single_top_dir:
                entries = list(temp_dir.iterdir())
                if len(entries) == 1 and entries[0].is_dir():
                    top_dir = entries[0]
                    if not (dest_root / top_dir.name).exists():
                        effective_root = top_dir
            return self._walk_source_dir(effective_root, dest_root)

        return [(source_path, dest_root / source_path.name)]

    def _walk_source_dir(
        self, source_dir: Path, dest_root: Path
    ) -> list[tuple[Path, Path]]:
        pairs: list[tuple[Path, Path]] = []
        for item in sorted(source_dir.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(source_dir)
            if self.BACKUP_FOLDER_NAME in rel.parts:
                continue
            pairs.append((item, dest_root / rel))
        return pairs

    def _install_files(
        self,
        *,
        file_pairs: list[tuple[Path, Path]],
        game_dir: Path,
        backup: bool,
    ) -> PatchResult:
        backup_root = game_dir / self.BACKUP_FOLDER_NAME
        backup_files_root = backup_root / "files"
        manifest_path = backup_root / "manifest.json"

        existing_backed_up: set[str] = set()
        existing_newly_added: set[str] = set()
        if backup and manifest_path.is_file():
            try:
                old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing_backed_up.update(old_manifest.get("backed_up_files", []))
                existing_newly_added.update(old_manifest.get("newly_added_files", []))
            except (OSError, json.JSONDecodeError):
                pass

        installed_files: list[str] = []
        backed_up_this_run: list[str] = []

        for src_file, dst_file in file_pairs:
            rel_posix = dst_file.resolve().relative_to(game_dir).as_posix()

            if backup:
                if dst_file.exists():
                    # 若某文件是先前打补丁时新增的，二次覆盖时不将其当作原始游戏文件备份
                    if rel_posix not in existing_newly_added:
                        backup_target = backup_files_root / Path(rel_posix)
                        if not backup_target.exists():
                            backup_target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(dst_file, backup_target)
                        existing_backed_up.add(rel_posix)
                        backed_up_this_run.append(rel_posix)
                else:
                    if rel_posix not in existing_backed_up:
                        existing_newly_added.add(rel_posix)

            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            installed_files.append(rel_posix)

        used_backup_dir: Optional[Path] = None
        if backup:
            backup_root.mkdir(parents=True, exist_ok=True)
            manifest_data = {
                "version": 1,
                "backed_up_files": sorted(existing_backed_up),
                "newly_added_files": sorted(existing_newly_added),
            }
            manifest_path.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            used_backup_dir = backup_root

        return PatchResult(
            success=True,
            game_dir=game_dir,
            installed_files=installed_files,
            backed_up_files=backed_up_this_run,
            backup_dir=used_backup_dir,
            message=(
                f"补丁安装成功：写入 {len(installed_files)} 个文件，"
                f"备份 {len(backed_up_this_run)} 个已有文件。"
            ),
        )

    @staticmethod
    def _cleanup_empty_parents(directory: Path, stop_at: Path) -> None:
        current = directory.resolve()
        boundary = stop_at.resolve()
        while current != boundary:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def apply_patch(
    patch_source: Union[str, Path],
    game_dir: Optional[Union[str, Path]] = None,
    *,
    profile: Optional[GameProfile] = None,
    app_id: Optional[int] = None,
    game_name: Optional[str] = None,
    target_subdir: str = "",
    backup: bool = True,
    expected_sha256: Optional[str] = None,
    strip_single_top_dir: bool = True,
    password: Optional[str] = None,
    password_candidates: Optional[Sequence[str]] = None,
    progress_callback: Optional[ProgressCallback] = None,
    steam_root: Optional[Union[str, Path]] = None,
) -> PatchResult:
    """一键安装 Galgame 补丁的便捷函数。"""
    locator = SteamLocator(steam_root=steam_root)
    patcher = GalgamePatcher(locator=locator)
    return patcher.apply(
        patch_source=patch_source,
        game_dir=game_dir,
        profile=profile,
        app_id=app_id,
        game_name=game_name,
        target_subdir=target_subdir,
        backup=backup,
        expected_sha256=expected_sha256,
        strip_single_top_dir=strip_single_top_dir,
        password=password,
        password_candidates=password_candidates,
        progress_callback=progress_callback,
    )


def rollback_patch(game_dir: Union[str, Path]) -> PatchResult:
    """一键回滚指定游戏目录补丁的便捷函数。"""
    patcher = GalgamePatcher()
    return patcher.rollback(game_dir=game_dir)
