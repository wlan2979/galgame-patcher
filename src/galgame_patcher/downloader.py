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
from .models import (
    BARE_EXECUTABLE_SUFFIXES,
    COMMON_ARCHIVE_EXTENSIONS,
    OnlinePatchCandidate,
    get_appdata_dir,
)

logger = logging.getLogger("galgame_patcher")

ProgressCallback = Callable[[int, Optional[int]], None]

DEFAULT_TIMEOUT = 30.0
_CHUNK_SIZE = 64 * 1024
_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')([^;\r\n]+)")
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";\r\n]+)"?')

# 仅将压缩包视为“直链包”；裸可执行文件或散装文件不参与直链白名单。
_DIRECT_DOWNLOAD_SUFFIXES: tuple[str, ...] = COMMON_ARCHIVE_EXTENSIONS

# 这些格式一旦被复制进游戏目录就会被执行，是恶意软件最直接的载体，
# 永远不视为可自动下载安装的补丁。
_BARE_EXECUTABLE_SUFFIXES: tuple[str, ...] = BARE_EXECUTABLE_SUFFIXES

# 官方发行商及公开百科/开源组织补丁域名白名单。
# 白名单只是「最低层来源过滤」，不代表工具担保内容安全。
_TRUSTED_PATCH_DOMAINS: tuple[str, ...] = (
    "nekonyansoft.com",
    "jastusa.com",
    "mangagamer.com",
    "sekaiproject.com",
    "frontwing.com",
    "yuzu-soft.com",
    "yuzu-soft.jp",
    "shiravune.com",
    "kaguragames.com",
    "denpasoft.com",
    "johren.net",
    "07th-mod.com",
    "pcgamingwiki.com",
)

# 可信 GitHub 仓库 owner 白名单。默认空 —— 需要用户自行确认后追加，
# 可直接在 %APPDATA%\galgame-patcher\sources.json 的 trusted_github_owners 中配置。
_TRUSTED_GITHUB_OWNERS: tuple[str, ...] = ()

# GitHub 仓库搜索的最低 star 门槛
DEFAULT_MIN_GITHUB_STARS = 10

# 官方与开源补丁网站快捷导航（用于 CLI sites 与 GUI 官方站导航窗口）
OFFICIAL_PATCH_PORTALS: tuple[dict[str, str], ...] = (
    {
        "name": "VNDB（视觉小说补丁数据库）",
        "url": "https://vndb.org/r",
        "mode": "内置API搜索",
        "desc": "涵盖大多数作品的官方补丁与社区补丁发布索引",
    },
    {
        "name": "柚子社 Yuzusoft 官方支持页",
        "url": "https://www.yuzu-soft.com/support.html",
        "mode": "内置搜索/直链",
        "desc": "Yuzusoft 历代作品官方修正补丁下载与支持汇总",
    },
    {
        "name": "NekoNyan 官方补丁站",
        "url": "https://nekonyansoft.com/shop/patches",
        "mode": "内置搜索/导航",
        "desc": "NekoNyan 发行作品的官方 18+ / 恢复补丁下载页",
    },
    {
        "name": "PCGamingWiki（修复与补丁百科）",
        "url": "https://www.pcgamingwiki.com/",
        "mode": "内置API搜索",
        "desc": "PC 游戏与视觉小说官方更新、兼容性修复指引",
    },
    {
        "name": "07th-Mod（寒蝉/海猫开源语音补丁）",
        "url": "https://07th-mod.com/",
        "mode": "内置搜索/导航",
        "desc": "07th Expansion 作品的高清立绘与全语音开源补丁项目",
    },
    {
        "name": "Shiravune（白舟）官方网站",
        "url": "https://www.shiravune.com/",
        "mode": "浏览器打开",
        "desc": "Shiravune 发行作品官方补丁页（站内 Patches 栏目）",
    },
    {
        "name": "Kagura Games 官方站",
        "url": "https://www.kaguragames.com/",
        "mode": "浏览器打开",
        "desc": "Kagura Games 官方免费补丁页面（站内 Patches 栏目）",
    },
    {
        "name": "Denpasoft 官方站",
        "url": "https://denpasoft.com/",
        "mode": "浏览器打开",
        "desc": "Denpasoft 官方未删减补丁发布商店",
    },
    {
        "name": "Johren 官方平台",
        "url": "https://www.johren.net/",
        "mode": "浏览器打开",
        "desc": "Johren 官方正版平台与未删减版本",
    },
    {
        "name": "JAST USA 官方站",
        "url": "https://jastusa.com/",
        "mode": "浏览器打开",
        "desc": "JAST USA 官方补丁与正版发行站",
    },
    {
        "name": "MangaGamer 官方站",
        "url": "https://www.mangagamer.com/",
        "mode": "浏览器打开",
        "desc": "MangaGamer 官方支持与正版补丁发布站",
    },
    {
        "name": "Sekai Project 官方站",
        "url": "https://sekaiproject.com/",
        "mode": "浏览器打开",
        "desc": "Sekai Project 官方补丁公告与支持站",
    },
)

_YUZUSOFT_ALIASES: dict[str, tuple[str, ...]] = {
    "senren": ("千恋", "万花", "senren", "banka"),
    "riddle": ("riddle", "joker", "谜题"),
    "sabbat": ("sabbat", "witch", "魔女", "夜宴"),
    "stella": ("stella", "cafe", "喫茶", "星光"),
    "ten_sz": ("tenshin", "天使", "騒々", "reboot"),
    "tenshin": ("tenshin", "天使", "騒々"),
    "lllj": ("limelight", "lemonade", "lllj", "ライムライト"),
    "amairo": ("amairo", "isle", "天色"),
    "dracu": ("dracu", "riot"),
    "noble": ("noble", "works"),
    "bra": ("braban", "ぶらばん"),
    "parquete": ("parquete", "パルフェ"),
}

_07TH_KEYWORDS: tuple[str, ...] = (
    "07th",
    "higurashi",
    "umineko",
    "ciconia",
    "ryukishi",
    "寒蝉",
    "海猫",
    "白鹳",
)


def get_user_sources_path() -> Path:
    """返回用户自定义补丁站点与白名单配置文件路径（%APPDATA%\\galgame-patcher\\sources.json）。"""
    return get_appdata_dir() / "sources.json"


def load_user_sources() -> dict[str, list[str]]:
    """读取用户自定义的检索页面列表、信任域名与 GitHub owner 白名单。"""
    default_data: dict[str, list[str]] = {
        "extra_pages": [],
        "trusted_domains": [],
        "trusted_github_owners": [],
    }
    path = get_user_sources_path()
    if not path.is_file():
        return default_data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_data
        for key in ("extra_pages", "trusted_domains", "trusted_github_owners"):
            val = raw.get(key)
            if isinstance(val, list):
                default_data[key] = [str(item).strip() for item in val if str(item).strip()]
        return default_data
    except (OSError, json.JSONDecodeError):
        return default_data


def save_user_sources(
    *,
    extra_pages: Optional[Sequence[str]] = None,
    trusted_domains: Optional[Sequence[str]] = None,
    trusted_github_owners: Optional[Sequence[str]] = None,
) -> Path:
    """保存或初始化用户自定义站点配置 sources.json。"""
    current = load_user_sources()
    if extra_pages is not None:
        current["extra_pages"] = [str(x).strip() for x in extra_pages if str(x).strip()]
    if trusted_domains is not None:
        current["trusted_domains"] = [
            str(x).strip().lower() for x in trusted_domains if str(x).strip()
        ]
    if trusted_github_owners is not None:
        current["trusted_github_owners"] = [
            str(x).strip() for x in trusted_github_owners if str(x).strip()
        ]

    path = get_user_sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, **current}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def is_url(source: Union[str, Path]) -> bool:
    """判断输入源是否为 HTTP/HTTPS/FILE URL。"""
    if isinstance(source, Path):
        return False
    lowered = str(source).strip().lower()
    return lowered.startswith(("http://", "https://", "file://"))


def _get_url_path_lower(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.unquote(parsed.path).lower()


def is_executable_url(url: str) -> bool:
    """判断 URL 路径是否以裸可执行文件/脚本后缀结尾。"""
    path_lower = _get_url_path_lower(url)
    return any(path_lower.endswith(ext) for ext in _BARE_EXECUTABLE_SUFFIXES)


def is_direct_patch_url(url: str) -> bool:
    """判断某个 URL 路径是否直接指向补丁压缩包（不含裸可执行文件）。"""
    path_lower = _get_url_path_lower(url)
    return any(path_lower.endswith(ext) for ext in _DIRECT_DOWNLOAD_SUFFIXES)


def _is_trusted_domain(url: str) -> bool:
    """判断 URL 主机名是否命中内置官方或用户自定义域名白名单。"""
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return False
    host_lower = host.lower()
    user_domains = load_user_sources().get("trusted_domains", [])
    all_domains = list(_TRUSTED_PATCH_DOMAINS) + [d.lower() for d in user_domains]
    return any(
        host_lower == domain or host_lower.endswith("." + domain)
        for domain in all_domains
    )


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
        domain_trusted = _is_trusted_domain(full_url)
        results.append(
            OnlinePatchCandidate(
                title=title,
                url=full_url,
                source_site=urllib.parse.urlparse(page_url).netloc or "WebPage",
                is_direct_download=direct,
                description=f"从页面 {page_url} 提取",
                trusted=domain_trusted,
                trust_reason=(
                    "命中官方发行商域名白名单"
                    if domain_trusted
                    else "从网页提取，发布者未知"
                ),
                bare_executable=is_executable_url(full_url),
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
                domain_trusted = _is_trusted_domain(url)
                vndb_trusted = bool(official and domain_trusted)
                if vndb_trusted:
                    reason = "VNDB 标记为官方发布，且域名命中发行商白名单"
                elif official:
                    reason = "VNDB 标记为官方，但域名不在发行商白名单内"
                else:
                    reason = "VNDB 记录的社区补丁，发布者未经审核"

                candidates.append(
                    OnlinePatchCandidate(
                        title=f"{title} ({label})",
                        url=url,
                        source_site="VNDB",
                        is_direct_download=is_direct_patch_url(url),
                        language=lang_str,
                        official=official,
                        description=f"VNDB Release {rel_id} ({'官方补丁' if official else '社区补丁'})",
                        trusted=vndb_trusted,
                        trust_reason=reason,
                        bare_executable=is_executable_url(url),
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
                        trusted=False,
                        trust_reason="VNDB 详情页，需自行判断发布方",
                        bare_executable=False,
                    )
                )

    return candidates


def crawl_github_patch_releases(
    game_query: str,
    *,
    min_stars: int = DEFAULT_MIN_GITHUB_STARS,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """通过 GitHub 公开 API 搜索游戏相关开源补丁仓库及其 Release 压缩包链接。"""
    query = game_query.strip()
    if not query:
        return []

    encoded_q = urllib.parse.quote(f"{query} patch")
    search_url = f"https://api.github.com/search/repositories?q={encoded_q}&per_page=10"
    data = fetch_json(search_url, timeout=timeout, proxies=proxies)
    repos = data.get("items", []) if isinstance(data, dict) else []

    candidates: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()
    user_owners = load_user_sources().get("trusted_github_owners", [])
    trusted_owners_lower = {
        o.lower() for o in (list(_TRUSTED_GITHUB_OWNERS) + list(user_owners))
    }
    dropped_low_stars = 0
    skipped_executables = 0

    for repo in repos:
        if not isinstance(repo, dict):
            continue
        full_name = str(repo.get("full_name") or "")
        html_url = str(repo.get("html_url") or "")
        desc = str(repo.get("description") or "")
        if not full_name:
            continue

        stars = int(repo.get("stargazers_count") or 0)
        if stars < min_stars:
            logger.debug(
                "已跳过低 star GitHub 仓库 %s (%d stars < %d)",
                full_name,
                stars,
                min_stars,
            )
            dropped_low_stars += 1
            continue

        owner_obj = repo.get("owner")
        owner = (
            str(owner_obj.get("login") or "")
            if isinstance(owner_obj, dict)
            else ""
        )
        if not owner and "/" in full_name:
            owner = full_name.split("/", 1)[0]

        owner_trusted = owner.lower() in trusted_owners_lower
        trust_reason = (
            "owner 在本地可信白名单内"
            if owner_trusted
            else f"GitHub 第三方仓库（{stars} stars），发布者未经审核"
        )

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
                if not dl_url:
                    continue
                if is_executable_url(dl_url):
                    logger.warning("已跳过可执行文件资产: %s", dl_url)
                    skipped_executables += 1
                    continue
                if dl_url not in seen_urls and is_direct_patch_url(dl_url):
                    seen_urls.add(dl_url)
                    candidates.append(
                        OnlinePatchCandidate(
                            title=f"{full_name} - {asset_name}",
                            url=dl_url,
                            source_site="GitHub Releases",
                            is_direct_download=True,
                            version=tag_name,
                            description=desc or f"GitHub Release ({full_name})",
                            trusted=owner_trusted,
                            trust_reason=trust_reason,
                            bare_executable=False,
                        )
                    )

        if html_url and html_url not in seen_urls:
            seen_urls.add(html_url)
            candidates.append(
                OnlinePatchCandidate(
                    title=f"{full_name} ({stars} stars)",
                    url=html_url,
                    source_site="GitHub",
                    is_direct_download=False,
                    description=desc or "GitHub 补丁项目主页",
                    trusted=owner_trusted,
                    trust_reason=trust_reason,
                    bare_executable=is_executable_url(html_url),
                )
            )

    logger.info(
        "GitHub 搜索汇总：丢弃低 star 仓库 %d 个，跳过可执行文件资产 %d 个",
        dropped_low_stars,
        skipped_executables,
    )
    return candidates


def _tokenize_query(query: str) -> list[str]:
    """将游戏名称拆分为用于匹配页面标题和锚点 slug 的小写词元列表。"""
    tokens = [t for t in re.split(r"[\s*＊_\-:：/()\[\]]+", query.lower()) if t]
    return tokens


def crawl_yuzusoft_patches(
    game_query: str = "",
    *,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """从柚子社官方网站 (yuzu-soft.com/support.html) 检索官方修正补丁与支持页链接。"""
    support_url = "https://www.yuzu-soft.com/support.html"
    html_text = fetch_text(support_url, timeout=timeout, proxies=proxies)
    parser = _AnchorExtractor()
    parser.feed(html_text)

    query_lower = game_query.strip().lower()
    tokens = _tokenize_query(game_query)
    is_broad = (not query_lower) or any(
        k in query_lower for k in ("yuzu", "柚子", "ゆず")
    )

    candidates: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()
    matched_subpages: list[str] = []

    for raw_href, label in parser.links:
        full_url = urllib.parse.urljoin(support_url, raw_href)
        url_lower = full_url.lower()
        combined = f"{label} {url_lower}".lower()

        if is_direct_patch_url(full_url):
            if is_broad or any(tok in combined for tok in tokens):
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    domain_ok = _is_trusted_domain(full_url)
                    candidates.append(
                        OnlinePatchCandidate(
                            title=label or PurePosixPath(urllib.parse.urlparse(full_url).path).name,
                            url=full_url,
                            source_site="Yuzusoft",
                            is_direct_download=True,
                            official=True,
                            description="柚子社官方支持页提供的修正补丁压缩包",
                            trusted=domain_ok,
                            trust_reason=(
                                "柚子社官方支持页直链（域名命中官方白名单）"
                                if domain_ok
                                else "从柚子社官方支持页提取的外链"
                            ),
                            bare_executable=is_executable_url(full_url),
                        )
                    )
            continue

        if "/products/" in url_lower and ("sup" in url_lower or "support" in url_lower):
            hit = is_broad
            if not hit:
                hit = any(tok in combined for tok in tokens)
            if not hit:
                for slug_key, alias_words in _YUZUSOFT_ALIASES.items():
                    if slug_key in url_lower and any(
                        alias in query_lower for alias in alias_words
                    ):
                        hit = True
                        break
            if hit and full_url not in matched_subpages:
                matched_subpages.append(full_url)

    # 对命中的作品支持子页（最多展开前 3 个，防止泛搜请求风暴）继续抓取具体补丁链接
    for subpage_url in matched_subpages[:3]:
        if subpage_url not in seen_urls:
            seen_urls.add(subpage_url)
            candidates.append(
                OnlinePatchCandidate(
                    title=f"Yuzusoft 支持页 ({PurePosixPath(urllib.parse.urlparse(subpage_url).path).parent.name})",
                    url=subpage_url,
                    source_site="Yuzusoft",
                    is_direct_download=False,
                    official=True,
                    description="柚子社对应作品官方支持页面",
                    trusted=_is_trusted_domain(subpage_url),
                    trust_reason="柚子社官方支持页（域名命中官方白名单）",
                    bare_executable=False,
                )
            )
        try:
            sub_items = scrape_patch_links_from_page(
                subpage_url, timeout=timeout, proxies=proxies
            )
            for item in sub_items:
                if item.url not in seen_urls and (
                    item.is_direct_download or "patch" in item.title.lower() or "パッチ" in item.title
                ):
                    seen_urls.add(item.url)
                    item.source_site = "Yuzusoft"
                    item.official = True
                    candidates.append(item)
        except Exception as exc:
            logger.debug("抓取柚子社子页 %s 失败: %s", subpage_url, exc)

    return candidates


def crawl_nekonyan_patches(
    game_query: str,
    *,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """从 NekoNyan 官方补丁汇总页 (nekonyansoft.com/shop/patches) 检索对应作品补丁入口。"""
    base_page = "https://nekonyansoft.com/shop/patches"
    html_text = fetch_text(base_page, timeout=timeout, proxies=proxies)
    parser = _AnchorExtractor()
    parser.feed(html_text)

    query_lower = game_query.strip().lower()
    tokens = [t for t in _tokenize_query(game_query) if len(t) >= 2]
    is_broad = "nekonyan" in query_lower

    candidates: list[OnlinePatchCandidate] = []
    seen_urls: set[str] = set()

    for raw_href, label in parser.links:
        if not raw_href.startswith("#") or len(raw_href) <= 1:
            continue
        title = label or raw_href[1:]
        haystack = f"{title} {raw_href}".lower()
        if not is_broad:
            if not tokens or not any(tok in haystack for tok in tokens):
                continue

        full_url = f"{base_page}{raw_href}"
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        candidates.append(
            OnlinePatchCandidate(
                title=title,
                url=full_url,
                source_site="NekoNyan",
                is_direct_download=False,
                official=True,
                description="NekoNyan 官方补丁页面（点击在浏览器直达该游戏下载卡片）",
                trusted=True,
                trust_reason="NekoNyan 官方补丁页（域名命中发行商白名单）",
                bare_executable=False,
            )
        )
        if len(candidates) >= 15:
            break

    return candidates


def crawl_pcgamingwiki_patches(
    game_query: str,
    *,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """通过 PCGamingWiki MediaWiki 公开 API 检索游戏修复与官方补丁百科页面。"""
    query = game_query.strip()
    if not query:
        return []

    encoded = urllib.parse.quote(query)
    api_url = (
        f"https://www.pcgamingwiki.com/w/api.php"
        f"?action=query&list=search&srsearch={encoded}&srlimit=5&format=json"
    )
    data = fetch_json(api_url, timeout=timeout, proxies=proxies)
    if not isinstance(data, dict):
        return []

    search_items = data.get("query", {}).get("search", [])
    if not isinstance(search_items, list):
        return []

    candidates: list[OnlinePatchCandidate] = []
    for item in search_items:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        title = str(item["title"]).strip()
        slug = urllib.parse.quote(title.replace(" ", "_"), safe="/():*'!~-")
        url_str = f"https://www.pcgamingwiki.com/wiki/{slug}"
        candidates.append(
            OnlinePatchCandidate(
                title=f"{title} - PCGamingWiki 修复与补丁参考",
                url=url_str,
                source_site="PCGamingWiki",
                is_direct_download=False,
                official=False,
                description="PCGamingWiki 百科词条（含官方补丁/兼容性修复说明与外链）",
                trusted=_is_trusted_domain(url_str),
                trust_reason="PCGamingWiki 公开百科页面（域名命中白名单）",
                bare_executable=False,
            )
        )
    return candidates


def crawl_07th_mod_patches(
    game_query: str,
    *,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """针对寒蝉/海猫等 07th 作品检索 07th-Mod 官方与 GitHub 开源补丁仓库。"""
    query_lower = game_query.strip().lower()
    if not any(kw in query_lower for kw in _07TH_KEYWORDS):
        return []

    candidates: list[OnlinePatchCandidate] = [
        OnlinePatchCandidate(
            title="07th-Mod 官方补丁站 (寒蝉/海猫语音与立绘增强)",
            url="https://07th-mod.com/",
            source_site="07th-Mod",
            is_direct_download=False,
            official=True,
            description="07th-Mod 官方网站与安装向导",
            trusted=_is_trusted_domain("https://07th-mod.com/"),
            trust_reason="07th-Mod 官方网站（域名命中白名单）",
            bare_executable=False,
        )
    ]

    user_owners = load_user_sources().get("trusted_github_owners", [])
    owner_trusted = "07th-mod" in {
        o.lower() for o in (list(_TRUSTED_GITHUB_OWNERS) + list(user_owners))
    }
    trust_reason = (
        "owner 在本地可信白名单内"
        if owner_trusted
        else "07th-Mod GitHub 开源仓库，默认未加入个人 owner 白名单"
    )

    try:
        repos = fetch_json(
            "https://api.github.com/orgs/07th-mod/repos?per_page=15&sort=updated",
            timeout=timeout,
            proxies=proxies,
        )
        if isinstance(repos, list):
            tokens = _tokenize_query(game_query)
            for repo in repos:
                if not isinstance(repo, dict):
                    continue
                full_name = str(repo.get("full_name") or "")
                html_url = str(repo.get("html_url") or "")
                desc = str(repo.get("description") or "")
                stars = int(repo.get("stargazers_count") or 0)
                haystack = f"{full_name} {desc}".lower()
                if "07th" not in query_lower and not any(t in haystack for t in tokens if len(t) >= 3):
                    continue
                if html_url:
                    candidates.append(
                        OnlinePatchCandidate(
                            title=f"{full_name} ({stars} stars)",
                            url=html_url,
                            source_site="07th-Mod (GitHub)",
                            is_direct_download=False,
                            description=desc or "07th-Mod 官方开源仓库",
                            trusted=owner_trusted,
                            trust_reason=trust_reason,
                            bare_executable=False,
                        )
                    )
    except Exception as exc:
        logger.debug("检索 07th-Mod GitHub 仓库失败: %s", exc)

    return candidates


def discover_patches_online(
    query_or_url: str,
    *,
    extra_page_urls: Optional[Sequence[str]] = None,
    timeout: float = 12.0,
    proxies: Optional[dict[str, str]] = None,
) -> list[OnlinePatchCandidate]:
    """自动从公开网站检索对应游戏的补丁候选列表（只返回链接信息，不参与自动下载）。"""
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
        parsed_target = urllib.parse.urlparse(target)
        host_lower = (parsed_target.hostname or "").lower()
        if (host_lower == "yuzu-soft.com" or host_lower.endswith(".yuzu-soft.com")) and parsed_target.path in ("", "/", "/index.html"):
            try:
                _extend(crawl_yuzusoft_patches("", timeout=timeout, proxies=proxies))
            except Exception as exc:
                logger.warning("爬取 yuzu-soft.com 支持页失败: %s", exc)
            return results

        try:
            _extend(scrape_patch_links_from_page(target, timeout=timeout, proxies=proxies))
        except Exception as exc:
            logger.warning("爬取页面 %s 失败: %s", target, exc)
        return results

    configured_pages = list(extra_page_urls or ()) + load_user_sources().get("extra_pages", [])
    for page_url in configured_pages:
        try:
            _extend(
                scrape_patch_links_from_page(
                    page_url, keyword=target, timeout=timeout, proxies=proxies
                )
            )
        except Exception as exc:
            logger.debug("爬取自定义页面 %s 失败: %s", page_url, exc)

    for crawler in (
        crawl_vndb_patches,
        crawl_yuzusoft_patches,
        crawl_nekonyan_patches,
        crawl_pcgamingwiki_patches,
        crawl_07th_mod_patches,
        crawl_github_patch_releases,
    ):
        try:
            _extend(crawler(target, timeout=timeout, proxies=proxies))
        except Exception as exc:
            logger.debug("来源检索 %s 失败: %s", crawler.__name__, exc)

    results.sort(
        key=lambda c: (
            not c.trusted,
            c.bare_executable,
            not c.is_direct_download,
            not c.official,
        )
    )
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
