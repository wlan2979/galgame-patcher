"""Galgame 补丁事务化安装、占用检测、并发锁、安装后校验与安全回滚核心逻辑。"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence, Union

from .archive import extract_archive, is_supported_archive
from .catalog import PatchCatalog
from .downloader import ProgressCallback, download_patch, is_url
from .locator import SteamLocator
from .models import (
    BARE_EXECUTABLE_SUFFIXES,
    CatalogEntry,
    ConcurrencyError,
    GameProfile,
    GameRunningError,
    PatchFileInspection,
    PatchResult,
    UntrustedSourceError,
)

logger = logging.getLogger("galgame_patcher")

_CHUNK_SIZE = 1024 * 1024


def _raw_sha256(file_path: Union[str, Path]) -> str:
    """内部使用的真实文件 SHA256 计算函数。"""
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


def compute_sha256(file_path: Union[str, Path]) -> str:
    """分块计算指定文件的 SHA256 十六进制摘要（小写）。"""
    return _raw_sha256(file_path)


def inspect_patch_file(path: Union[str, Path]) -> PatchFileInspection:
    """检查本地补丁文件的体积、修改时间、类型标记与 SHA256 摘要，供安装前人工核对。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"补丁文件不存在: {target}")

    st = target.stat()
    modified_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
    return PatchFileInspection(
        path=target.resolve(),
        size=st.st_size,
        sha256=compute_sha256(target),
        modified_at=modified_at,
        is_archive=is_supported_archive(target),
        is_bare_executable=target.suffix.lower() in BARE_EXECUTABLE_SUFFIXES,
    )


def _is_file_locked(path: Path) -> bool:
    """检测目标文件是否正被其他进程占用（P0-2）。"""
    if not path.is_file():
        return False

    if sys.platform == "win32":
        try:
            with path.open("r+b"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False

    try:
        import fcntl

        with path.open("r+b") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (PermissionError, BlockingIOError):
                return True
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return False
    except PermissionError:
        return True
    except OSError:
        return False


def _is_pid_running(pid: int) -> bool:
    """检测给定 PID 的进程是否仍在运行（P3-3）。"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if sys.platform == "win32":
        process_query_limited_info = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_info, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return False
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _parse_lock_content(raw_text: str) -> tuple[Optional[int], str]:
    """解析 .lock 文件中的 PID 与启动时间（兼容 JSON 与纯数字格式）。"""
    text = raw_text.strip()
    if not text:
        return None, ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, int):
            return parsed, ""
        if isinstance(parsed, dict):
            raw_pid = parsed.get("pid")
            pid = int(raw_pid) if raw_pid is not None else None
            started = str(parsed.get("started_at") or "")
            return pid, started
    except (ValueError, json.JSONDecodeError):
        pass

    match = re.search(r"\d+", text)
    if match:
        return int(match.group(0)), ""
    return None, ""


def _write_manifest_atomic(manifest_path: Path, data: dict[str, Any]) -> None:
    """通过写临时文件 + os.replace 原子更新 manifest.json（P0-1）。"""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_path, manifest_path)


class GalgamePatcher:
    """Galgame 补丁安装器，支持自动定位、在线下载、事务化备份与安全回滚。"""

    BACKUP_FOLDER_NAME = ".galpatch_backup"

    def __init__(self, locator: Optional[SteamLocator] = None) -> None:
        """初始化补丁安装器，可注入自定义 SteamLocator 实例。"""
        self.locator = locator or SteamLocator()

    @contextmanager
    def _directory_lock(self, game_dir: Path) -> Iterator[Path]:
        """在 game_dir/.galpatch_backup/.lock 上创建排他锁，自动清理陈旧锁（P3-3）。"""
        backup_root = game_dir / self.BACKUP_FOLDER_NAME
        backup_root.mkdir(parents=True, exist_ok=True)
        lock_path = backup_root / ".lock"

        acquired_fd: Optional[int] = None
        for _ in range(2):
            try:
                acquired_fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                break
            except FileExistsError:
                try:
                    raw = lock_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    raw = ""
                pid, started_at = _parse_lock_content(raw)
                if pid is not None and _is_pid_running(pid):
                    raise ConcurrencyError(pid=pid, started_at=started_at)
                logger.info("清理已退出的陈旧进程锁 (PID=%s): %s", pid, lock_path)
                try:
                    lock_path.unlink()
                except OSError:
                    raise ConcurrencyError(pid=pid, started_at=started_at)

        if acquired_fd is None:
            raise ConcurrencyError(pid=None)

        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )
            os.write(acquired_fd, payload.encode("utf-8"))
        finally:
            os.close(acquired_fd)

        try:
            yield lock_path
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                if backup_root.is_dir() and not any(backup_root.iterdir()):
                    backup_root.rmdir()
            except OSError:
                pass

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
        force_locked: bool = False,
        dry_run: bool = False,
        allow_untrusted_url: bool = False,
        source_origin: str = "manual",
    ) -> PatchResult:
        """将本地文件/目录/压缩包或受校验的远程 URL 补丁安装到目标 Galgame 目录。"""
        if is_url(patch_source):
            if expected_sha256 and expected_sha256.strip():
                pass
            elif not allow_untrusted_url:
                raise UntrustedSourceError(
                    str(patch_source),
                    "该链接未提供 expected_sha256，工具无法确认下载到的文件是否被篡改",
                )
            else:
                logger.warning(
                    "用户显式允许从未经审核的远程地址安装 (source_origin=%s): %s",
                    source_origin,
                    patch_source,
                )

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
            actual_hash = _raw_sha256(source_path)
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
        if not dry_run:
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
                force_locked=force_locked,
                dry_run=dry_run,
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
        force_locked: bool = False,
        dry_run: bool = False,
        allow_untrusted_url: bool = False,
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
            force_locked=force_locked,
            dry_run=dry_run,
            allow_untrusted_url=(
                entry.expected_sha256 is None and allow_untrusted_url
            ),
            source_origin="catalog",
        )

    def rollback(
        self, game_dir: Union[str, Path], *, mode: str = "full", to: Optional[str] = None
    ) -> PatchResult:
        """完整还原，或撤回安装至指定记录（默认撤回最近一次）。"""
        if mode not in ("full", "keep-patches"):
            raise ValueError(f"未知的回滚模式: {mode}")
        if to is not None and mode != "keep-patches":
            raise ValueError("指定安装记录时需要使用 keep-patches 模式")
        resolved_game_dir = Path(game_dir).resolve()
        if not resolved_game_dir.is_dir():
            raise FileNotFoundError(f"游戏目录不存在: {resolved_game_dir}")

        backup_root = resolved_game_dir / self.BACKUP_FOLDER_NAME
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"未找到补丁备份清单: {manifest_path}")
        with self._directory_lock(resolved_game_dir):
            if not manifest_path.is_file():
                raise FileNotFoundError(f"未找到补丁备份清单: {manifest_path}")
            manifest = self._read_backup_manifest(manifest_path)
            status = manifest.get("status", "committed")
            if mode == "keep-patches":
                if status == "in_progress":
                    raise ValueError("检测到未完成的安装，请先执行完整回滚")
                if to is None:
                    return self._undo_latest_install(resolved_game_dir, backup_root, manifest)
                history = self._read_rollback_history(backup_root, manifest)
                ids = [record_id for record_id, _ in history]
                if to not in ids:
                    raise ValueError(f"找不到安装记录 {to}，请用 rollback --history 查看")
                if to == ids[0]:
                    return PatchResult(success=True, game_dir=resolved_game_dir,
                                       message=f"已经处于安装记录 {to}")
                for _ in range(ids.index(to)):
                    result = self._undo_latest_install(resolved_game_dir, backup_root, manifest)
                    if not result.success:
                        return result
                    manifest = self._read_backup_manifest(manifest_path)
                result.message = f"已恢复至安装记录 {to}。"
                return result
            backed_up_files: list[str] = manifest.get("backed_up_files", [])
            newly_added_files: list[str] = manifest.get("newly_added_files", [])
            backup_files_root = backup_root / "files"

            restored: list[str] = []
            removed_new: list[str] = []
            failed_files: list[str] = []
            failed_reasons: dict[str, str] = {}

            for rel_posix in backed_up_files:
                rel_path = Path(rel_posix)
                try:
                    backup_file = self._restore_path(backup_files_root, rel_path)
                    target_file = self._restore_game_path(resolved_game_dir, rel_path)
                    if not backup_file.is_file():
                        raise FileNotFoundError(f"备份文件缺失: {backup_file}")
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup_file, target_file)
                    restored.append(rel_posix)
                except (OSError, ValueError) as exc:
                    failed_files.append(rel_posix)
                    failed_reasons[rel_posix] = f"{type(exc).__name__}: {exc}"

            for rel_posix in newly_added_files:
                try:
                    target_file = self._restore_game_path(resolved_game_dir, Path(rel_posix))
                    if not target_file.exists():
                        continue
                    target_file.unlink()
                    removed_new.append(rel_posix)
                    self._cleanup_empty_parents(target_file.parent, resolved_game_dir)
                except (OSError, ValueError) as exc:
                    failed_files.append(rel_posix)
                    failed_reasons[rel_posix] = f"{type(exc).__name__}: {exc}"

            in_progress_prefix = (
                "检测到上次打补丁未完成，已按残留记录还原。"
                if status == "in_progress"
                else ""
            )

            if failed_files:
                return PatchResult(
                    success=False,
                    game_dir=resolved_game_dir,
                    installed_files=restored,
                    backed_up_files=[],
                    backup_dir=backup_root,
                    message=(
                        f"回滚部分失败：{len(failed_files)} 个文件处理出错。"
                        "备份已保留，修复问题后可重试回滚。"
                    ),
                    failed_files=failed_files,
                    failed_reasons=failed_reasons,
                    verified=False,
                )

            try:
                for snapshot in backup_root.glob("install-*"):
                    if snapshot.is_dir() and not snapshot.is_symlink():
                        shutil.rmtree(snapshot)
                if backup_files_root.exists():
                    shutil.rmtree(backup_files_root)
                if manifest_path.exists():
                    manifest_path.unlink()
            except OSError as exc:
                logger.warning("清理备份内容失败: %s", exc)

        return PatchResult(
            success=True,
            game_dir=resolved_game_dir,
            installed_files=restored,
            backed_up_files=[],
            backup_dir=None,
            message=(
                f"{in_progress_prefix}已成功回滚补丁：还原 {len(restored)} 个原文件，"
                f"移除 {len(removed_new)} 个新增文件。"
            ),
            failed_files=[],
            failed_reasons={},
            verified=True,
        )

    @staticmethod
    def _read_backup_manifest(manifest_path: Path) -> dict:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取备份清单 {manifest_path}，已保留备份: {exc}") from exc
        if not isinstance(manifest, dict) or any(
            not isinstance(manifest.get(key), list)
            or not all(isinstance(item, str) for item in manifest[key])
            for key in ("backed_up_files", "newly_added_files")
        ):
            raise ValueError(f"备份清单格式无效，已保留备份: {manifest_path}")
        return manifest

    @staticmethod
    def _restore_path(root: Path, relative: Path) -> Path:
        if relative.anchor or ".." in relative.parts or not relative.parts:
            raise ValueError(f"备份记录包含越界路径: {relative}")
        target = root.absolute() / relative
        if target.resolve() != target:
            raise ValueError(f"备份记录包含越界路径: {relative}")
        return target

    def _restore_game_path(self, game_dir: Path, relative: Path) -> Path:
        if relative.parts and relative.parts[0].casefold() == self.BACKUP_FOLDER_NAME:
            raise ValueError(f"回滚不能写入备份目录: {relative}")
        return self._restore_path(game_dir, relative)

    def rollback_history(self, game_dir: Union[str, Path]) -> list[tuple[str, str]]:
        """列出有快照的安装记录，最近一次在前。"""
        game_dir = Path(game_dir).resolve()
        if not game_dir.is_dir():
            raise FileNotFoundError(f"游戏目录不存在: {game_dir}")
        backup_root = game_dir / self.BACKUP_FOLDER_NAME
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.is_file():
            return []
        with self._directory_lock(game_dir):
            if not manifest_path.is_file():
                return []
            return self._read_rollback_history(backup_root, self._read_backup_manifest(manifest_path))

    def _read_rollback_history(self, backup_root: Path, manifest: dict) -> list[tuple[str, str]]:
        if manifest.get("status") == "in_progress":
            raise ValueError("检测到未完成的安装，请先执行完整回滚")
        history = []
        seen = set()
        while manifest.get("status") == "committed" and manifest.get("latest_install") is not None:
            snapshot_dir, journal = self._read_install_journal(backup_root, manifest)
            if snapshot_dir.name in seen:
                raise ValueError("安装记录出现重复引用")
            seen.add(snapshot_dir.name)
            history.append((snapshot_dir.name, journal["applied_at"]))
            manifest = journal["previous_manifest"] or {}
        return history

    def _read_install_journal(self, backup_root: Path, manifest: dict) -> tuple[Path, dict]:
        latest = manifest.get("latest_install")
        if manifest.get("status") != "committed" or not isinstance(latest, str):
            raise ValueError("没有可撤回的最近一次安装记录。旧备份仍可使用完整回滚。")
        if not latest.startswith("install-") or Path(latest).name != latest:
            raise ValueError("最近一次安装记录中的目录无效")
        snapshot_dir = self._restore_path(backup_root, Path(latest))
        journal = json.loads((snapshot_dir / "undo.json").read_text(encoding="utf-8"))
        if (
            not isinstance(journal, dict)
            or not isinstance(journal.get("applied_at"), str)
            or not isinstance(journal.get("prior_contents"), dict)
            or "previous_manifest" not in journal
            or (journal["previous_manifest"] is not None and not isinstance(journal["previous_manifest"], dict))
        ):
            raise ValueError("最近一次安装记录的格式无效")
        return snapshot_dir, journal

    def _undo_latest_install(
        self, game_dir: Path, backup_root: Path, manifest: dict
    ) -> PatchResult:
        snapshot_dir, journal = self._read_install_journal(backup_root, manifest)
        changes = journal["prior_contents"]
        previous = journal["previous_manifest"]

        plan = []
        for name, saved in reversed(list(changes.items())):
            relative = Path(name)
            target = self._restore_game_path(game_dir, relative)
            if saved is not None and (
                not isinstance(saved, str)
                or Path(saved) not in (Path("files") / relative, Path(snapshot_dir.name, "files") / relative)
            ):
                raise ValueError(f"安装记录中的备份路径无效: {name}")
            snapshot = self._restore_path(backup_root, Path(saved)) if saved is not None else None
            if snapshot is not None and not snapshot.is_file():
                raise FileNotFoundError(f"缺少最近一次安装的备份: {name}")
            plan.append((name, target, snapshot))

        restored = []
        failures = {}
        for name, target, snapshot in plan:
            try:
                if snapshot is None:
                    target.unlink(missing_ok=True)
                    self._cleanup_empty_parents(target.parent, game_dir)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snapshot, target)
                    restored.append(name)
            except OSError as exc:
                failures[name] = str(exc)

        if failures:
            return PatchResult(
                success=False, game_dir=game_dir, installed_files=restored,
                backed_up_files=[], backup_dir=backup_root,
                message="撤回最近一次安装时部分文件处理失败，已保留记录，可重试。",
                failed_files=list(failures), failed_reasons=failures,
            )

        manifest_path = backup_root / "manifest.json"
        if previous is None:
            manifest_path.unlink()
        else:
            _write_manifest_atomic(manifest_path, previous)
        try:
            shutil.rmtree(snapshot_dir)
            if previous is None and (backup_root / "files").exists():
                shutil.rmtree(backup_root / "files")
        except OSError as exc:
            logger.warning("清理已撤回的安装备份失败: %s", exc)
        if previous is None:
            summary = "已恢复至首次跟踪安装前的状态。"
        else:
            summary = "较早的安装记录已保留。"
        return PatchResult(
            success=True, game_dir=game_dir, installed_files=restored,
            backed_up_files=[], backup_dir=backup_root if previous else None,
            message=f"已撤回最近一次安装（{journal['applied_at']}）。{summary}",
            verified=True,
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
        backup: bool = True,
        force_locked: bool = False,
        dry_run: bool = False,
    ) -> PatchResult:
        """事务化安装补丁文件，支持前置占用检测、并发锁、写后校验与异常自动回滚。"""
        # 1. Dry-run 仅预览模式（不写盘，P3-2）
        if dry_run:
            will_overwrite: list[str] = []
            will_add: list[str] = []
            for _src_file, dst_file in file_pairs:
                rel_posix = dst_file.resolve().relative_to(game_dir).as_posix()
                if dst_file.exists():
                    will_overwrite.append(rel_posix)
                else:
                    will_add.append(rel_posix)

            preview_msg = (
                f"[仅预览 Dry-Run] 共 {len(file_pairs)} 个文件："
                f"将覆盖 {len(will_overwrite)} 个已有文件"
                f"（{', '.join(will_overwrite) if will_overwrite else '无'}），"
                f"将新增 {len(will_add)} 个文件"
                f"（{', '.join(will_add) if will_add else '无'}）。"
            )
            return PatchResult(
                success=True,
                game_dir=game_dir,
                installed_files=will_overwrite + will_add,
                backed_up_files=will_overwrite,
                backup_dir=None,
                message=preview_msg,
                verified=False,
            )

        with self._directory_lock(game_dir):
            # 2. 前置文件占用检测（P0-2）
            if not force_locked:
                locked_files: list[str] = []
                for _src_file, dst_file in file_pairs:
                    if dst_file.exists() and _is_file_locked(dst_file):
                        rel_posix = dst_file.resolve().relative_to(game_dir).as_posix()
                        locked_files.append(rel_posix)
                if locked_files:
                    raise GameRunningError(locked_files)

            backup_root = game_dir / self.BACKUP_FOLDER_NAME
            backup_files_root = backup_root / "files"
            manifest_path = backup_root / "manifest.json"

            existing_backed_up: set[str] = set()
            existing_newly_added: set[str] = set()
            previous_manifest = None
            if manifest_path.is_file():
                old_manifest = self._read_backup_manifest(manifest_path)
                if old_manifest.get("status") == "in_progress":
                    raise ValueError("上次安装未完成，请先回滚后重试")
                if old_manifest and old_manifest.get("status") != "rolled_back":
                    previous_manifest = old_manifest
                    existing_backed_up.update(old_manifest.get("backed_up_files", []))
                    existing_newly_added.update(old_manifest.get("newly_added_files", []))

            installed_files: list[str] = []
            backed_up_this_run: list[str] = []
            prior_contents: dict[str, Optional[Path]] = {}
            snapshot_dir: Optional[Path] = None

            def _sync_manifest(status: str, latest_install: Optional[str] = None) -> None:
                payload = {
                    "version": 1,
                    "status": status,
                    "backed_up_files": sorted(existing_backed_up),
                    "newly_added_files": sorted(existing_newly_added),
                }
                if latest_install is not None:
                    payload["latest_install"] = latest_install
                _write_manifest_atomic(manifest_path, payload)

            _sync_manifest("in_progress")

            def _rollback_current_transaction() -> str:
                failures = []
                for rel_posix, snapshot in reversed(prior_contents.items()):
                    target_file = game_dir / Path(rel_posix)
                    try:
                        if snapshot is None:
                            target_file.unlink(missing_ok=True)
                            self._cleanup_empty_parents(target_file.parent, game_dir)
                        else:
                            target_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(snapshot, target_file)
                    except OSError as exc:
                        failures.append(f"{rel_posix} ({exc})")

                if failures:
                    return f"自动回滚未完成，备份已保留：{', '.join(failures)}"

                try:
                    if previous_manifest is None:
                        existing_backed_up.clear()
                        existing_newly_added.clear()
                        _sync_manifest("rolled_back")
                    else:
                        _write_manifest_atomic(manifest_path, previous_manifest)
                except OSError as exc:
                    return f"文件已还原，但备份清单恢复失败：{exc}"

                if snapshot_dir is not None:
                    shutil.rmtree(snapshot_dir, ignore_errors=True)
                return "已自动回滚，游戏目录未受影响"

            try:
                for src_file, dst_file in file_pairs:
                    rel_posix = dst_file.resolve().relative_to(game_dir).as_posix()
                    expected_file_hash = _raw_sha256(src_file)

                    if dst_file.exists():
                        if rel_posix in existing_backed_up or rel_posix in existing_newly_added:
                            if snapshot_dir is None:
                                snapshot_dir = Path(
                                    tempfile.mkdtemp(prefix="install-", dir=backup_root)
                                )
                            snapshot = snapshot_dir / "files" / rel_posix
                            snapshot.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(dst_file, snapshot)
                        else:
                            snapshot = backup_files_root / rel_posix
                            snapshot.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(dst_file, snapshot)
                            existing_backed_up.add(rel_posix)
                            backed_up_this_run.append(rel_posix)
                    else:
                        snapshot = None
                        if rel_posix not in existing_backed_up:
                            existing_newly_added.add(rel_posix)

                    _sync_manifest("in_progress")
                    prior_contents[rel_posix] = snapshot
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    installed_files.append(rel_posix)
                    _sync_manifest("in_progress")

                    # 安装后 SHA256 校验（P3-2）
                    written_hash = compute_sha256(dst_file)
                    if written_hash != expected_file_hash:
                        reason = (
                            f"写入后 SHA256 校验不一致: 预期 {expected_file_hash}，"
                            f"实际 {written_hash}"
                        )
                        rollback_note = _rollback_current_transaction()
                        return PatchResult(
                            success=False,
                            game_dir=game_dir,
                            installed_files=[],
                            backed_up_files=[],
                            backup_dir=backup_root,
                            message=f"安装后文件校验失败 ({rel_posix})，{rollback_note}。",
                            failed_files=[rel_posix],
                            failed_reasons={rel_posix: reason},
                            verified=False,
                        )

                if backup:
                    if snapshot_dir is None:
                        snapshot_dir = Path(tempfile.mkdtemp(prefix="install-", dir=backup_root))
                    _write_manifest_atomic(
                        snapshot_dir / "undo.json",
                        {
                            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "previous_manifest": previous_manifest,
                            "prior_contents": {
                                name: snapshot.relative_to(backup_root).as_posix() if snapshot else None
                                for name, snapshot in prior_contents.items()
                            },
                        },
                    )
                    _sync_manifest("committed", snapshot_dir.name)
                elif previous_manifest is not None:
                    retained = {key: value for key, value in previous_manifest.items() if key != "latest_install"}
                    _write_manifest_atomic(manifest_path, retained)
                else:
                    manifest_path.unlink()

            except BaseException as exc:
                note = f"{exc}（{_rollback_current_transaction()}）"
                if isinstance(exc, PermissionError):
                    raise PermissionError(note) from exc
                if isinstance(exc, OSError):
                    raise OSError(note) from exc
                if isinstance(exc, Exception):
                    try:
                        raise type(exc)(note) from exc
                    except TypeError:
                        raise RuntimeError(note) from exc
                raise

            if snapshot_dir is not None and not backup:
                shutil.rmtree(snapshot_dir, ignore_errors=True)

            if backup:
                used_backup_dir: Optional[Path] = backup_root
            else:
                try:
                    for rel_posix in backed_up_this_run:
                        snapshot = backup_files_root / rel_posix
                        snapshot.unlink(missing_ok=True)
                        self._cleanup_empty_parents(snapshot.parent, backup_root)
                except Exception as exc:
                    logger.warning("清理临时事务备份失败: %s", exc)
                used_backup_dir = None

            return PatchResult(
                success=True,
                game_dir=game_dir,
                installed_files=installed_files,
                backed_up_files=backed_up_this_run,
                backup_dir=used_backup_dir,
                message=(
                    f"补丁安装成功：写入并校验 {len(installed_files)} 个文件，"
                    f"备份 {len(backed_up_this_run)} 个已有文件。"
                ),
                verified=True,
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
    force_locked: bool = False,
    dry_run: bool = False,
    allow_untrusted_url: bool = False,
    source_origin: str = "manual",
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
        force_locked=force_locked,
        dry_run=dry_run,
        allow_untrusted_url=allow_untrusted_url,
        source_origin=source_origin,
    )


def rollback_patch(
    game_dir: Union[str, Path], *, mode: str = "full", to: Optional[str] = None
) -> PatchResult:
    """一键回滚指定游戏目录补丁的便捷函数。"""
    patcher = GalgamePatcher()
    return patcher.rollback(game_dir=game_dir, mode=mode, to=to)
