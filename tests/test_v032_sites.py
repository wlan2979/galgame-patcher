"""v0.3.2 多站点检索源扩展测试：Yuzusoft、NekoNyan、PCGamingWiki、07th-Mod 与自定义配置。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import (
    crawl_07th_mod_patches,
    crawl_github_patch_releases,
    crawl_nekonyan_patches,
    crawl_pcgamingwiki_patches,
    crawl_yuzusoft_patches,
    discover_patches_online,
    get_user_sources_path,
    load_user_sources,
    save_user_sources,
)
from galgame_patcher.cli import main as cli_main


class TestV032Sites(unittest.TestCase):
    """覆盖新增官方补丁源检索、自定义站点配置及 sites 命令。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_appdata = os.environ.get("GALGAME_PATCHER_APPDATA")
        os.environ["GALGAME_PATCHER_APPDATA"] = str(self.root / "appdata")

    def tearDown(self) -> None:
        if self._old_appdata is None:
            os.environ.pop("GALGAME_PATCHER_APPDATA", None)
        else:
            os.environ["GALGAME_PATCHER_APPDATA"] = self._old_appdata
        self._tmp.cleanup()

    def test_yuzusoft_crawler_and_root_url_dispatch(self) -> None:
        download_page = "https://www.yuzu-soft.com/products/senren/download.html"
        support_html = """
        <html><body>
          <a href="http://yuzu-soft.jp/bin/p_tenshin_savefix.zip">修正パッチ(p_tenshin_savefix.zip)</a>
          <a href="https://www.yuzu-soft.com/products/senren/senren_sup.html">千恋</a>
          <a href="https://www.yuzu-soft.com/products/riddle/riddle_sup.html">Riddle</a>
        </body></html>
        """
        senren_sup_html = """
        <html><body>
          <a href="https://yuzu-soft.jp/bin/sen_patch_1_12.zip">千恋＊万花修正パッチ</a>
          <a href="bin/sen_wall.zip">壁纸</a>
          <a href="bin/sen_sys_voice.zip">系统语音</a>
          <a href="bin/twitter.zip">头像</a>
        </body></html>
        """

        def _fake_fetch_text(url: str, **kwargs: object) -> str:
            if url.endswith("/support.html"):
                return support_html
            if url.split("?", 1)[0].endswith(("/senren_sup.html", "/download.html")):
                return senren_sup_html
            if url.endswith("/riddle_sup.html"):
                raise TimeoutError
            raise AssertionError(f"Unexpected URL: {url}")

        with mock.patch("galgame_patcher.downloader.fetch_text", side_effect=_fake_fetch_text):
            cands = crawl_yuzusoft_patches("千恋")

        urls = [c.url for c in cands]
        self.assertIn("https://www.yuzu-soft.com/products/senren/senren_sup.html", urls)
        self.assertIn(download_page, urls)
        self.assertFalse(any(c.is_direct_download for c in cands))
        installer = next(c for c in cands if c.url == download_page)
        self.assertTrue(installer.trusted)
        self.assertTrue(installer.manual_install)
        self.assertIn("Steam 版兼容性未确认", installer.description)

        with mock.patch("galgame_patcher.downloader.fetch_text", side_effect=_fake_fetch_text):
            direct_cands = discover_patches_online(download_page + "?ref=test")
        self.assertEqual(direct_cands, [installer])

        with mock.patch("galgame_patcher.downloader.fetch_text") as mocked_fetch:
            archive_cands = discover_patches_online("https://yuzu-soft.jp/bin/sen_patch_1_12.zip")
        mocked_fetch.assert_not_called()
        self.assertEqual(archive_cands, [installer])

        # 直接把 https://www.yuzu-soft.com 传给 discover_patches_online 应自动解析 support.html
        on_failure = mock.Mock()
        with mock.patch("galgame_patcher.downloader.fetch_text", side_effect=_fake_fetch_text):
            root_cands = discover_patches_online("https://www.yuzu-soft.com", on_failure=on_failure)
        self.assertTrue(any("p_tenshin_savefix.zip" in c.url for c in root_cands))
        on_failure.assert_called_once_with("柚子社作品页", "请求超时")

    def test_nekonyan_pcgamingwiki_and_07th_mod_crawlers(self) -> None:
        nn_html = """
        <html><body>
          <a href="#aokana-extra1-18-steam-patch">Aokana Extra1 18+ Steam Patch</a>
          <a href="#aokana-perfect-edition-18-steam-patch">Aokana Perfect Edition 18+ Steam Patch</a>
          <a href="#aokana-extra2-18-steam-patch">Aokana Extra2 18+ Steam Patch</a>
          <a href="#makinglovers-first-blush-18-steam-patch">Making*Lovers 18+ Steam Patch</a>
        </body></html>
        """
        with mock.patch("galgame_patcher.downloader.fetch_text", return_value=nn_html):
            nn_cands = crawl_nekonyan_patches("Aokana Extra 2")
        self.assertEqual(len(nn_cands), 1)
        self.assertEqual(
            nn_cands[0].url,
            "https://patches.nekonyansoft.com/#aokana-extra2-18-steam-patch",
        )
        self.assertTrue(nn_cands[0].trusted)
        self.assertIn("Steam", nn_cands[0].description)

        pcgw_resp = {
            "query": {
                "search": [
                    {"title": "Senren * Banka"},
                ]
            }
        }
        with mock.patch("galgame_patcher.downloader.fetch_json", return_value=pcgw_resp):
            pcgw_cands = crawl_pcgamingwiki_patches("Senren Banka")
        self.assertEqual(len(pcgw_cands), 1)
        self.assertIn("pcgamingwiki.com/wiki/Senren", pcgw_cands[0].url)
        self.assertTrue(pcgw_cands[0].trusted)

        # 07th-Mod：不相关关键词直接跳过，不发网络请求
        with mock.patch("galgame_patcher.downloader.fetch_json") as mocked_json:
            self.assertEqual(crawl_07th_mod_patches("SomeOtherGame"), [])
            mocked_json.assert_not_called()

        org_repos = [
            {
                "full_name": "07th-mod/higurashi-console-arcs",
                "html_url": "https://github.com/07th-mod/higurashi-console-arcs",
                "description": "Higurashi console voice and graphics mod",
                "stargazers_count": 180,
            }
        ]
        with mock.patch("galgame_patcher.downloader.fetch_json", return_value=org_repos):
            mod_cands = crawl_07th_mod_patches("Higurashi")
        self.assertTrue(any(c.url == "https://07th-mod.com/" and c.trusted for c in mod_cands))
        self.assertFalse(any(c.official for c in mod_cands))
        self.assertTrue(
            any("higurashi-console-arcs" in c.url and not c.trusted for c in mod_cands)
        )

    def test_user_sources_json_custom_pages_and_allowlists(self) -> None:
        cfg_path = save_user_sources(
            extra_pages=["https://custom-fan.example.org/patches.html"],
            trusted_domains=["custom-publisher.org"],
            trusted_github_owners=["my-trusted-org"],
        )
        self.assertEqual(cfg_path, get_user_sources_path())
        loaded = load_user_sources()
        self.assertEqual(loaded["extra_pages"], ["https://custom-fan.example.org/patches.html"])
        self.assertEqual(loaded["trusted_domains"], ["custom-publisher.org"])
        self.assertEqual(loaded["trusted_github_owners"], ["my-trusted-org"])

        fake_search = {
            "items": [
                {
                    "full_name": "my-trusted-org/vn-fix",
                    "html_url": "https://github.com/my-trusted-org/vn-fix",
                    "description": "VN fix",
                    "stargazers_count": 25,
                    "owner": {"login": "my-trusted-org"},
                }
            ]
        }
        with mock.patch(
            "galgame_patcher.downloader.fetch_json",
            side_effect=[fake_search, {}],
        ):
            gh_cands = crawl_github_patch_releases("vn")
        self.assertEqual(len(gh_cands), 1)
        self.assertTrue(gh_cands[0].trusted)

        # CLI sites 命令与 --init-config / --open
        out_buf = io.StringIO()
        with redirect_stdout(out_buf), mock.patch("galgame_patcher.cli.webbrowser.open") as mocked_open:
            code_list = cli_main(["sites"])
            code_open = cli_main(["sites", "--open", "2"])
        self.assertEqual(code_list, 0)
        self.assertEqual(code_open, 0)
        self.assertIn("yuzu-soft.com", out_buf.getvalue())
        self.assertIn("custom-fan.example.org", out_buf.getvalue())
        mocked_open.assert_called_once_with("https://www.yuzu-soft.com/support.html")


if __name__ == "__main__":
    unittest.main()
