"""远程补丁下载、进度回调与本地缓存校验模块。"""

from __future__ import annotations

import hashlib
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Union

from ._version import __version__

ProgressCallback = Callable[[int, Optional[int]], None]

_CHUNK_SIZE = 64 * 1024
_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\r\n]+)")
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";\r\n]+)"?')


def is_url(source: Union[str, Path]) -> bool:
    """判断输入源是否为 HTTP/HTTPS/FILE URL。"""
    if isinstance(source, Path):
        return False
    lowered = str(source).strip().lower()
    return lowered.startswith(("http://", "https://", "file://"))


def _compute_file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def _infer_filename(url: str, content_disposition: Optional[str]) -> str:
    if content_disposition:
        star_match = _FILENAME_STAR_RE.search(content_disposition)
        if star_match:
            decoded = urllib.parse.unquote(star_match.group(1).strip())
            clean = Path(decoded).name
            if clean:
                return clean

        plain_match = _FILENAME_RE.search(content_disposition)
        if plain_match:
            clean = Path(plain_match.group(1).strip()).name
            if clean:
                return clean

    parsed = urllib.parse.urlparse(url)
    url_name = urllib.parse.unquote(PurePosixPath(parsed.path).name)
    if url_name:
        return Path(url_name).name

    return "downloaded_patch"


def download_patch(
    url: str,
    dest_dir: Optional[Union[str, Path]] = None,
    *,
    filename: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
    timeout: float = 30.0,
) -> Path:
    """从远程 URL 流式下载补丁到本地缓存目录，支持哈希校验与缓存复用。"""
    cache_dir = (
        Path(dest_dir)
        if dest_dir is not None
        else Path(tempfile.gettempdir()) / "galgame_patcher_cache"
    )
    cache_dir.mkdir(parents=True, exist_ok=True)

    normalized_expected = (
        expected_sha256.strip().lower() if expected_sha256 else None
    )

    if filename:
        initial_target = cache_dir / Path(filename).name
        if initial_target.is_file() and normalized_expected:
            if _compute_file_sha256(initial_target) == normalized_expected:
                size = initial_target.stat().st_size
                if progress_callback:
                    progress_callback(size, size)
                return initial_target

    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"galgame-patcher/{__version__}"},
    )

    part_path: Optional[Path] = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = response.info()
            resolved_name = (
                Path(filename).name
                if filename
                else _infer_filename(url, headers.get("Content-Disposition"))
            )
            target_path = cache_dir / resolved_name

            if target_path.is_file() and normalized_expected:
                if _compute_file_sha256(target_path) == normalized_expected:
                    size = target_path.stat().st_size
                    if progress_callback:
                        progress_callback(size, size)
                    return target_path

            content_length_raw = headers.get("Content-Length")
            total_bytes = (
                int(content_length_raw)
                if content_length_raw and content_length_raw.isdigit()
                else None
            )

            part_path = cache_dir / f"{resolved_name}.part"
            digest = hashlib.sha256()
            downloaded = 0

            with part_path.open("wb") as fh:
                while True:
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_bytes)

            if normalized_expected:
                actual_hash = digest.hexdigest().lower()
                if actual_hash != normalized_expected:
                    raise ValueError(
                        f"下载补丁 SHA256 校验失败: 预期 {normalized_expected}，实际 {actual_hash}"
                    )

            if target_path.exists():
                target_path.unlink()
            part_path.replace(target_path)
            part_path = None
            return target_path
    finally:
        if part_path is not None and part_path.exists():
            part_path.unlink(missing_ok=True)
