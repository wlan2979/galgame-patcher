"""统一网络请求、远程补丁流式下载、HEAD 链接探活与网页补丁爬虫模块（P2-2）。"""

from __future__ import annotations

import hashlib
import html.parser
import json
import logging
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional, Sequence, Union

from ._version import __version__
from .models import COMMON_ARCHIVE_EXTENSIONS, COMMON_PATCH_EXTENSIONS, OnlinePatchCandidate

logger = logging.getLogger("galgame_patcher")

ProgressCallback = Callable[[int, Optional[int]], None]

DEFAULT_TIMEOUT = 30.0
_CHUNK_SIZE = 64 * 1024
_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\r\n]+)")
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";\r\n]+)"?')

_DIRECT_DOWNLOAD_SUFFIXES: tuple[str, ...] = (
    COMMON_ARCHIVE_EXTENSIONS
    + tuple(ext for ext in COMMON_PATCH_EXTENSIONS if ext not in (".txt", ".json", ".ini", ".cfg"))
)


def is_url(source: Union[str, Path]) -> bool:
    """判断输入源是否为 HTTP/HTTPS/FILE URL。"""
    if isinstance(source, Path):
        return False
    lowered = str(source).strip().lower()
    return lowered.startswith(("http://", "https://", "file://"))


def is_direct_patch_url(url: str) -> bool:
    """判断某个 URL 路径是否直接指向压缩包或补丁文件。"""
    parsed = urllib.parse.urlparse(url)
    path_lower = urllib.parse.unquote(parsed.path).lower()
    return any(path_lower.endswith(ext) for ext in _DIRECT_DOWNLOAD_SUFFIXES)


def _build_opener(proxies: Optional[dict[str, str]] = None) -> urllib.request.OpenerDirector:
    """构建统一遵循系统或自定义代理配置的 URL Opener（P2-2）。"""
    proxy_handler = (
        urllib.request.ProxyHandler(proxies)
        if proxies is not None
        else urllib.request.ProxyHandler()
    )
    return urllib.request.build_opener(proxy_handler)


def open_url_request(
    url: str,
    *,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    proxies: Optional[dict[str, str]] = None,
):
    """统一发起网络请求，使用相同的 User-Agent、代理和超时设置（P2-2）。"""
    merged_headers = {
        "User-Agent": f"galgame-patcher/{__version__}",
    }
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=data,
        headers=merged_headers,
        method=method,
    )
    if url.strip().lower().startswith("file://"):
        return urllib.request.urlopen(request, timeout=timeout)
    opener = _build_opener(proxies=proxies)
    return opener.open(request, timeout=timeout)


def fetch_text(
    url: str,
    *,
    timeout: float = 15.0,
    proxies: Optional[dict[str, str]] = None,
) -> str:
    """通过统一网络配置拉取远程文本内容。"""
    with open_url_request(url, timeout=timeout, proxies=proxies) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    proxies: Optional[dict[str, str]] = None,
) -> Any:
    """通过统一网络配置发送 GET/POST 请求并解析返回的 JSON。"""
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data_bytes: Optional[bytes] = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    with open_url_request(
        url,
        method=method,
        data=data_bytes,
        headers=req_headers,
        timeout=timeout,
        proxies=proxies,
    ) as response:
        raw_text = response.read().decode("utf-8", errors="replace")
    return json.loads(raw_text)


def check_url_head(
    url: str,
    *,
    timeout: float = 10.0,
    proxies: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    """对目标 URL 发起 HEAD 请求探活（用于 catalog verify，P2-2）。"""
    if not url or not url.strip():
        return False, "空链接"

    cleaned = url.strip()
    if cleaned.lower().startswith("file://"):
        try:
            with open_url_request(cleaned, timeout=timeout) as _:
                return True, "OK (file://)"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    try:
        with open_url_request(
            cleaned, method="HEAD", timeout=timeout, proxies=proxies
        ) as response:
            status = getattr(response, "status", 200) or 200
            if 200 <= int(status) < 400:
                return True, f"HTTP {status}"
            return False, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class _AnchorExtractor(html.parser.HTMLParser):
    """从 HTML 页面中提取所有 <a href="..."> 链接与锚文本。"""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            for attr_name, attr_val in attrs:
                if attr_name.lower() == "href" and attr_val:
                    self._current_href = attr_val.strip()
                    self._text_parts = []
                    break

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href is not None:
            label = " ".join("".join(self._text_parts).split())
            self.links.append((self._current_href, label))
            self._current_href = None
            self._text_parts = []


def scrape_patch_links_from_page(
    page_url: str,
    *,
    keyword: str = "",
    timeout: float = 15.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """从任意网页爬取补丁下载链接（自动提取 .zip/.7z/.rar/.xp3/.pck 等直链）。"""
    html_text = fetch_text(page_url, timeout=timeout, proxies=proxies)
    parser = _AnchorExtractor()
    parser.feed(html_text)

    keyword_lower = keyword.strip().lower()
    results: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()

    for raw_href, label in parser.links:
        full_url = urllib.parse.urljoin(page_url, raw_href)
        if full_url in seen_urls:
            continue
        direct = is_direct_patch_url(full_url)
        combined_text = f"{label} {full_url}".lower()
        if not direct and "patch" not in combined_text and "补丁" not in combined_text:
            continue
        if keyword_lower and keyword_lower not in combined_text and not direct:
            continue

        seen_urls.add(full_url)
        title = label or PurePosixPath(urllib.parse.urlparse(full_url).path).name or full_url
        results.append(
            OnlinePatchCandidate(
                title=title,
                url=full_url,
                source_site=urllib.parse.urlparse(page_url).netloc or "WebPage",
                is_direct_download=direct,
                description=f"从页面 {page_url} 提取",
            )
        )
    return results


def crawl_vndb_patches(
    game_query: str,
    *,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """通过 VNDB 官方公开 API (v2 kana) 自动检索对应游戏的补丁发布记录与下载链接。"""
    query = game_query.strip()
    if not query:
        return []

    payload = {
        "filters": ["and", ["search", "=", query], ["patch", "=", True]],
        "fields": (
            "id, title, alttitle, languages.lang, patch, freeware, "
            "official, extlinks.url, extlinks.label, extlinks.name"
        ),
        "results": 15,
    }
    data = fetch_json(
        "https://api.vndb.org/kana/release",
        method="POST",
        payload=payload,
        timeout=timeout,
        proxies=proxies,
    )
    items = data.get("results", []) if isinstance(data, dict) else []
    candidates: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            continue
        rel_id = str(item.get("id") or "")
        title = str(item.get("title") or item.get("alttitle") or query)
        official = bool(item.get("official"))
        langs = [
            str(lang_obj.get("lang"))
            for lang_obj in (item.get("languages") or [])
            if isinstance(lang_obj, dict) and lang_obj.get("lang")
        ]
        lang_str = ",".join(langs)
        extlinks = item.get("extlinks") or []

        if extlinks:
            for link in extlinks:
                if not isinstance(link, dict) or not link.get("url"):
                    continue
                url = str(link["url"]).strip()
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                label = str(link.get("label") or link.get("name") or "VNDB External")
                candidates.append(
                    OnlinePatchCandidate(
                        title=f"{title} ({label})",
                        url=url,
                        source_site="VNDB",
                        is_direct_download=is_direct_patch_url(url),
                        language=lang_str,
                        official=official,
                        description=f"VNDB Release {rel_id} ({'官方补丁' if official else '社区补丁'})",
                    )
                )
        elif rel_id:
            vndb_url = f"https://vndb.org/{rel_id}"
            if vndb_url not in seen_urls:
                seen_urls.add(vndb_url)
                candidates.append(
                    OnlinePatchCandidate(
                        title=title,
                        url=vndb_url,
                        source_site="VNDB",
                        is_direct_download=False,
                        language=lang_str,
                        official=official,
                        description=f"VNDB Release {rel_id} 详情页",
                    )
                )

    return candidates


def crawl_github_patch_releases(
    game_query: str,
    *,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """通过 GitHub 公开 API 搜索游戏相关开源补丁仓库及其 Release 补丁直链。"""
    query = game_query.strip()
    if not query:
        return []

    encoded_q = urllib.parse.quote(f"{query} patch")
    search_url = f"https://api.github.com/search/repositories?q={encoded_q}&per_page=5"
    data = fetch_json(search_url, timeout=timeout, proxies=proxies)
    repos = data.get("items", []) if isinstance(data, dict) else []

    candidates: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()

    for repo in repos[:3]:
        if not isinstance(repo, dict):
            continue
        full_name = str(repo.get("full_name") or "")
        html_url = str(repo.get("html_url") or "")
        desc = str(repo.get("description") or "")
        if not full_name:
            continue

        rel_api = f"https://api.github.com/repos/{full_name}/releases/latest"
        try:
            rel_data = fetch_json(rel_api, timeout=timeout, proxies=proxies)
        except Exception:
            rel_data = None

        if isinstance(rel_data, dict):
            tag_name = str(rel_data.get("tag_name") or "")
            for asset in rel_data.get("assets") or []:
                if not isinstance(asset, dict):
                    continue
                dl_url = str(asset.get("browser_download_url") or "")
                asset_name = str(asset.get("name") or "")
                if dl_url and dl_url not in seen_urls and is_direct_patch_url(dl_url):
                    seen_urls.add(dl_url)
                    candidates.append(
                        OnlinePatchCandidate(
                            title=f"{full_name} - {asset_name}",
                            url=dl_url,
                            source_site="GitHub Releases",
                            is_direct_download=True,
                            version=tag_name,
                            description=desc or f"GitHub Release ({full_name})",
                        )
                    )

        if html_url and html_url not in seen_urls:
            seen_urls.add(html_url)
            candidates.append(
                OnlinePatchCandidate(
                    title=full_name,
                    url=html_url,
                    source_site="GitHub",
                    is_direct_download=False,
                    description=desc or "GitHub 补丁项目主页",
                )
            )

    return candidates


def discover_patches_online(
    query_or_url: str,
    *,
    extra_page_urls: Optional[Sequence[str]] = None,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """自动从网站爬取对应游戏的补丁候选列表（支持直接爬网页 URL、VNDB 补丁库与 GitHub Releases）。"""
    target = query_or_url.strip()
    if not target:
        return []

    results: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()

    def _extend(items: Sequence[OnlinePatchCandidate]) -> None:
        for item in items:
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                results.append(item)

    if is_url(target):
        try:
            _extend(scrape_patch_links_from_page(target, timeout=timeout, proxies=proxies))
        except Exception as exc:
            logger.warning("爬取页面 %s 失败: %s", target, exc)
        return results

    for page_url in extra_page_urls or ():
        try:
            _extend(
                scrape_patch_links_from_page(
                    page_url, keyword=target, timeout=timeout, proxies=proxies
                )
            )
        except Exception as exc:
            logger.debug("爬取自定义页面 %s 失败: %s", page_url, exc)

    try:
        _extend(crawl_vndb_patches(target, timeout=timeout, proxies=proxies))
    except Exception as exc:
        logger.debug("VNDB 补丁搜索失败: %s", exc)

    try:
        _extend(crawl_github_patch_releases(target, timeout=timeout, proxies=proxies))
    except Exception as exc:
        logger.debug("GitHub 补丁搜索失败: %s", exc)

    # 优先将可直接下载的直链与官方补丁排在前面
    results.sort(key=lambda c: (not c.is_direct_download, not c.official))
    return results


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
    timeout: float = DEFAULT_TIMEOUT,
    proxies: Optional[dict[str, str]] = None,
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

    part_path: Optional[Path] = None
    try:
        with open_url_request(url, timeout=timeout, proxies=proxies) as response:
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
