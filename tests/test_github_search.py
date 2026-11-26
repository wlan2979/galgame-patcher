"""问题 3 测试：GitHub 搜索 Token 鉴权、403 限流恢复时间提示与本地缓存（全 Mock）。"""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock

from galgame_patcher import (
    GitHubRateLimitError,
    clear_github_search_cache,
    crawl_07th_mod_patches,
    crawl_github_patch_releases,
    discover_patches_online,
    downloader,
    get_github_token,
)


def _http_error(
    url: str, code: int, headers_dict: dict[str, str]
) -> urllib.error.HTTPError:
    msg = Message()
    for k, v in headers_dict.items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, "Forbidden", msg, io.BytesIO(b""))


class TestGitHubSearchAuthAndCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "GALGAME_PATCHER_APPDATA": str(self.root / "appdata"),
            },
            clear=False,
        )
        self._env_patch.start()
        self._old_gh = os.environ.pop("GITHUB_TOKEN", None)
        self._old_gp_gh = os.environ.pop("GALGAME_PATCHER_GITHUB_TOKEN", None)

    def tearDown(self) -> None:
        if self._old_gh is not None:
            os.environ["GITHUB_TOKEN"] = self._old_gh
        else:
            os.environ.pop("GITHUB_TOKEN", None)
        if self._old_gp_gh is not None:
            os.environ["GALGAME_PATCHER_GITHUB_TOKEN"] = self._old_gp_gh
        else:
            os.environ.pop("GALGAME_PATCHER_GITHUB_TOKEN", None)
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_token_env_precedence_and_authorization_header(self) -> None:
        self.assertIsNone(get_github_token())

        captured_headers: list[dict[str, str]] = []

        def _fake_fetch_json(url: str, **kwargs: Any) -> Any:
            captured_headers.append(dict(kwargs.get("headers") or {}))
            if "/search/repositories" in url:
                return {
                    "items": [
                        {
                            "full_name": "dev/vn-patch",
                            "html_url": "https://github.com/dev/vn-patch",
                            "description": "patch",
                            "stargazers_count": 10,
                            "owner": {"login": "dev"},
                        }
                    ]
                }
            if "/releases/latest" in url:
                return {"tag_name": "v1.0", "assets": []}
            return []

        # 仅设置 GITHUB_TOKEN
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_from_github_token"}), mock.patch(
            "galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json
        ):
            self.assertEqual(get_github_token(), "ghp_from_github_token")
            crawl_github_patch_releases("token-test-1", use_cache=False)

        self.assertEqual(len(captured_headers), 2)
        self.assertEqual(
            captured_headers[0].get("Authorization"), "Bearer ghp_from_github_token"
        )
        self.assertEqual(
            captured_headers[1].get("Authorization"), "Bearer ghp_from_github_token"
        )

        # 同时设置时 GALGAME_PATCHER_GITHUB_TOKEN 优先
        captured_headers.clear()
        with mock.patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "ghp_lower",
                "GALGAME_PATCHER_GITHUB_TOKEN": "ghp_higher",
            },
        ), mock.patch("galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json):
            self.assertEqual(get_github_token(), "ghp_higher")
            crawl_github_patch_releases("token-test-2", use_cache=False)
            crawl_07th_mod_patches("Higurashi")

        self.assertTrue(
            all(h.get("Authorization") == "Bearer ghp_higher" for h in captured_headers)
        )

        # 未设置 Token 时不发 Authorization 头
        captured_headers.clear()
        with mock.patch("galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json):
            crawl_github_patch_releases("token-test-3", use_cache=False)
        self.assertTrue(all("Authorization" not in h for h in captured_headers))

    def test_403_rate_limit_reports_reset_time_on_search_and_discover(self) -> None:
        reset_ts = int(time.time()) + 900
        expected_time_str = datetime.fromtimestamp(reset_ts).strftime("%Y-%m-%d %H:%M:%S")
        err = _http_error(
            "https://api.github.com/search/repositories?q=test",
            403,
            {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_ts),
            },
        )

        with mock.patch("galgame_patcher.downloader.fetch_json", side_effect=err):
            with self.assertRaises(GitHubRateLimitError) as ctx:
                crawl_github_patch_releases("rate-limited-game", use_cache=False)

        message = str(ctx.exception)
        self.assertIn("403", message)
        self.assertIn(expected_time_str, message)
        self.assertIn("GITHUB_TOKEN", message)

        # 通过 discover_patches_online 汇总时，on_failure 收到清晰恢复时间
        failures: list[tuple[str, str]] = []
        with mock.patch.object(downloader, "crawl_vndb_patches", return_value=[]), mock.patch.object(
            downloader, "crawl_yuzusoft_patches", return_value=[]
        ), mock.patch.object(
            downloader, "crawl_nekonyan_patches", return_value=[]
        ), mock.patch.object(
            downloader, "crawl_kagura_patches", return_value=[]
        ), mock.patch.object(
            downloader, "crawl_pcgamingwiki_patches", return_value=[]
        ), mock.patch.object(
            downloader, "crawl_07th_mod_patches", return_value=[]
        ), mock.patch(
            "galgame_patcher.downloader.fetch_json", side_effect=err
        ):
            discover_patches_online(
                "rate-limited-game",
                on_failure=lambda src, detail: failures.append((src, detail)),
            )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "GitHub")
        self.assertIn(expected_time_str, failures[0][1])

    def test_403_rate_limit_during_releases_stops_further_requests_and_skips_cache(self) -> None:
        reset_ts = int(time.time()) + 600
        expected_time_str = datetime.fromtimestamp(reset_ts).strftime("%Y-%m-%d %H:%M:%S")
        release_calls: list[str] = []

        def _fake_fetch_json(url: str, **_kwargs: Any) -> Any:
            if "/search/repositories" in url:
                return {
                    "items": [
                        {
                            "full_name": "dev/repo1",
                            "html_url": "https://github.com/dev/repo1",
                            "description": "r1",
                            "stargazers_count": 20,
                            "owner": {"login": "dev"},
                        },
                        {
                            "full_name": "dev/repo2",
                            "html_url": "https://github.com/dev/repo2",
                            "description": "r2",
                            "stargazers_count": 30,
                            "owner": {"login": "dev"},
                        },
                    ]
                }
            release_calls.append(url)
            raise _http_error(
                url,
                403,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_ts)},
            )

        failures: list[tuple[str, str]] = []
        with mock.patch("galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json):
            cands = crawl_github_patch_releases(
                "partial-limit",
                on_failure=lambda src, detail: failures.append((src, detail)),
            )

        # 第一个仓库触发 403 后立即停止，不再继续请求第二个仓库的 release
        self.assertEqual(len(release_calls), 1)
        self.assertEqual(len(cands), 2)
        self.assertEqual(len(failures), 1)
        self.assertIn(expected_time_str, failures[0][1])
        # 不完整结果不应写入缓存
        self.assertEqual(clear_github_search_cache(), 0)

    def test_local_cache_hits_expires_and_never_stores_token(self) -> None:
        calls: list[str] = []

        def _fake_fetch_json(url: str, **_kwargs: Any) -> Any:
            calls.append(url)
            if "/search/repositories" in url:
                return {
                    "items": [
                        {
                            "full_name": "cache-owner/my-patch",
                            "html_url": "https://github.com/cache-owner/my-patch",
                            "description": "Cached patch",
                            "stargazers_count": 15,
                            "owner": {"login": "cache-owner"},
                        }
                    ]
                }
            return {
                "tag_name": "v1.2",
                "assets": [
                    {
                        "name": "patch.zip",
                        "browser_download_url": "https://github.com/cache-owner/my-patch/releases/download/v1.2/patch.zip",
                    }
                ],
            }

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_secret_never_cache"}), mock.patch(
            "galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json
        ):
            first = crawl_github_patch_releases("cached-game", cache_ttl=300)
            second = crawl_github_patch_releases("cached-game", cache_ttl=300)

        self.assertEqual(len(calls), 2)
        self.assertEqual([c.url for c in first], [c.url for c in second])

        # 确认缓存文件已落盘且不含 token
        cache_dir = self.root / "appdata" / "galgame-patcher" / "cache" / "github_search"
        cache_files = list(cache_dir.glob("*.json"))
        self.assertEqual(len(cache_files), 1)
        self.assertNotIn("ghp_secret_never_cache", cache_files[0].read_text(encoding="utf-8"))

        # 缓存过期后重新请求
        with mock.patch(
            "galgame_patcher.downloader.time.time", return_value=time.time() + 1000
        ), mock.patch("galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json):
            crawl_github_patch_releases("cached-game", cache_ttl=300)
        self.assertEqual(len(calls), 4)

        self.assertEqual(clear_github_search_cache(), 1)
        self.assertEqual(clear_github_search_cache(), 0)


if __name__ == "__main__":
    unittest.main()
