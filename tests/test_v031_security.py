"""v0.3.1 安全加固测试：覆盖可信度筛选、裸可执行文件拦截、URL 强制校验与链接模式。"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import (
    CatalogEntry,
    GalgamePatcher,
    OnlinePatchCandidate,
    PatchCatalog,
    PatchFileInspection,
    UntrustedSourceError,
    compute_sha256,
    crawl_github_patch_releases,
    crawl_vndb_patches,
    downloader,
    inspect_patch_file,
    is_direct_patch_url,
    is_executable_url,
    scrape_patch_links_from_page,
)
from galgame_patcher.cli import main as cli_main


class TestV031Security(unittest.TestCase):
    """验证 v0.3.1 安全加固的各个关键防线。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "Game.exe").write_bytes(b"ORIG_EXE")
        self.patcher = GalgamePatcher()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rule1_models_and_exports(self) -> None:
        err = UntrustedSourceError("https://evil.test/p.zip", "缺少哈希")
        msg = str(err)
        self.assertIn("拒绝从未经审核的远程地址直接安装补丁", msg)
        self.assertIn("https://evil.test/p.zip", msg)
        self.assertIn("--allow-untrusted-url", msg)
        self.assertIn("expected_sha256", msg)

        cand = OnlinePatchCandidate(
            title="Test",
            url="https://example.com/a.zip",
            source_site="Test",
        )
        self.assertFalse(cand.trusted)
        self.assertEqual(cand.trust_reason, "来源未经审核")
        self.assertFalse(cand.bare_executable)

    def test_rule2_executable_urls_rejected_from_direct_download(self) -> None:
        self.assertFalse(is_direct_patch_url("https://x.com/a.exe"))
        self.assertFalse(is_direct_patch_url("https://x.com/a.dll"))
        self.assertTrue(is_direct_patch_url("https://x.com/a.zip"))
        self.assertTrue(is_direct_patch_url("https://x.com/a.7z"))
        self.assertTrue(is_executable_url("https://x.com/a.DLL"))
        self.assertTrue(is_executable_url("https://x.com/dir/run%20me.Ps1"))
        self.assertFalse(is_executable_url("https://x.com/a.7z"))

        html_page = (
            '<html><body>'
            '<a href="https://x.com/malware.exe">patch installer</a>'
            '<a href="https://nekonyansoft.com/dl/patch.zip">official zip</a>'
            '</body></html>'
        )
        with mock.patch("galgame_patcher.downloader.fetch_text", return_value=html_page):
            items = scrape_patch_links_from_page("https://example.com/list.html")

        self.assertEqual(len(items), 2)
        exe_item = next(i for i in items if i.url.endswith("malware.exe"))
        zip_item = next(i for i in items if i.url.endswith("patch.zip"))
        self.assertTrue(exe_item.bare_executable)
        self.assertFalse(exe_item.is_direct_download)
        self.assertFalse(exe_item.trusted)
        self.assertFalse(zip_item.bare_executable)
        self.assertTrue(zip_item.is_direct_download)
        self.assertTrue(zip_item.trusted)

    def test_remote_install_refuses_bare_executable_before_downloading(self) -> None:
        with mock.patch("galgame_patcher.downloader.open_url_request") as open_request:
            with self.assertRaisesRegex(ValueError, "裸可执行文件"):
                self.patcher.apply(
                    "https://example.com/patch.exe",
                    game_dir=self.game_dir,
                    expected_sha256="a" * 64,
                )
        open_request.assert_not_called()

    def test_downloader_rejects_executable_response_before_reading(self) -> None:
        cases = (
            ("https://example.com/patch.exe", {}),
            ("https://example.com/download", {
                "Content-Disposition": "attachment; filename=patch.cmd",
            }),
        )
        for final_url, headers in cases:
            with self.subTest(final_url=final_url, headers=headers):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = final_url
                response.info.return_value = headers
                with mock.patch(
                    "galgame_patcher.downloader.open_url_request", return_value=response
                ):
                    with self.assertRaisesRegex(ValueError, "裸可执行文件"):
                        downloader.download_patch(
                            "https://example.com/download", self.root, filename="patch.zip"
                        )
                response.read.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [self.game_dir])

    def test_verify_file_compares_expected_sha256(self) -> None:
        patch = self.root / "patch.xp3"
        patch.write_bytes(b"test patch contents")
        args = ["verify-file", str(patch), "--sha256"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli_main(args + [compute_sha256(patch).upper()]), 0)
        self.assertIn("SHA256 与预期值一致", output.getvalue())

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli_main(args + ["0" * 64]), 1)
            self.assertEqual(cli_main(args + ["not-a-digest"]), 1)
        self.assertIn("SHA256 不匹配", errors.getvalue())
        self.assertIn("64 位十六进制值", errors.getvalue())

    def test_remote_cache_separates_same_named_files_and_rejects_short_downloads(self) -> None:
        first_url = "https://example.com/first/patch.zip"
        second_url = "https://example.com/second/patch.zip"

        def response_for(url: str, data: bytes, declared_size: int) -> mock.MagicMock:
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = url
            response.info.return_value = {"Content-Length": str(declared_size)}
            response.read.side_effect = [data, b""]
            return response

        responses = [response_for(first_url, b"first", 5), response_for(second_url, b"second", 6)]
        with mock.patch(
            "galgame_patcher.downloader.tempfile.gettempdir", return_value=str(self.root)
        ), mock.patch("galgame_patcher.downloader.open_url_request", side_effect=responses):
            first = downloader.download_patch(
                first_url, expected_sha256=hashlib.sha256(b"first").hexdigest()
            )
            second = downloader.download_patch(
                second_url, expected_sha256=hashlib.sha256(b"second").hexdigest()
            )

        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(second.read_bytes(), b"second")

        with mock.patch(
            "galgame_patcher.downloader.open_url_request",
            return_value=response_for(second_url, b"truncated", 20),
        ):
            with self.assertRaisesRegex(ValueError, "下载大小不符"):
                downloader.download_patch(second_url, dest_dir=second.parent)
        self.assertEqual(second.read_bytes(), b"second")
        self.assertEqual(list(second.parent.iterdir()), [second])

    def test_rule3_github_and_vndb_trust_filtering(self) -> None:
        fake_search_resp = {
            "items": [
                {
                    "full_name": "lowstar/tiny-repo",
                    "html_url": "https://github.com/lowstar/tiny-repo",
                    "description": "Low star repo",
                    "stargazers_count": 3,
                    "owner": {"login": "lowstar"},
                },
                {
                    "full_name": "trusted-dev/good-patch",
                    "html_url": "https://github.com/trusted-dev/good-patch",
                    "description": "Popular patch repo",
                    "stargazers_count": 50,
                    "owner": {"login": "trusted-dev"},
                },
            ]
        }
        fake_release_resp = {
            "tag_name": "v1.0",
            "assets": [
                {
                    "name": "setup.exe",
                    "browser_download_url": "https://github.com/trusted-dev/good-patch/releases/download/v1.0/setup.exe",
                },
                {
                    "name": "patch.zip",
                    "browser_download_url": "https://github.com/trusted-dev/good-patch/releases/download/v1.0/patch.zip",
                },
            ],
        }

        def _fake_fetch_json(url: str, **kwargs: object) -> dict[str, object]:
            if "/search/repositories" in url:
                return fake_search_resp
            if "/releases/latest" in url:
                return fake_release_resp
            raise AssertionError(f"Unexpected URL: {url}")

        with mock.patch("galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json):
            cands = crawl_github_patch_releases("sample")

        urls = [c.url for c in cands]
        # 1. star 数 3 的仓库不出现
        self.assertFalse(any("lowstar/tiny-repo" in u for u in urls))
        # 2. setup.exe 资产不出现在任何候选里
        self.assertFalse(any(u.endswith("setup.exe") for u in urls))
        # 3. 50 stars 仓库出现且默认未审核
        repo_home = next(c for c in cands if c.url == "https://github.com/trusted-dev/good-patch")
        self.assertFalse(repo_home.trusted)
        self.assertIn("50 stars", repo_home.trust_reason)
        self.assertIn("50 stars", repo_home.title)
        # 4. patch.zip 资产出现且 is_direct_download=True
        zip_asset = next(c for c in cands if c.url.endswith("patch.zip"))
        self.assertTrue(zip_asset.is_direct_download)
        self.assertFalse(zip_asset.trusted)

        # 5. 将 trusted-dev 加入白名单后，同 owner 仓库 trusted=True
        with mock.patch("galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json), mock.patch.object(
            downloader, "_TRUSTED_GITHUB_OWNERS", ("trusted-dev",)
        ):
            trusted_cands = crawl_github_patch_releases("sample")
        self.assertTrue(all(c.trusted for c in trusted_cands))
        self.assertTrue(all("白名单" in c.trust_reason for c in trusted_cands))

        # 6. 测试 VNDB 可信度三档分类与 bare_executable 标记
        vndb_resp = {
            "results": [
                {
                    "id": "r1",
                    "title": "Official Patch",
                    "official": True,
                    "languages": [{"lang": "en"}],
                    "extlinks": [
                        {"url": "https://cdn.nekonyansoft.com/p.7z", "label": "Official CDN"},
                        {"url": "https://mirror.example.org/official.exe", "label": "Mirror EXE"},
                    ],
                },
                {
                    "id": "r2",
                    "title": "Community Patch",
                    "official": False,
                    "languages": [{"lang": "zh-Hans"}],
                    "extlinks": [
                        {"url": "https://community.example.org/chs.zip", "label": "Forum"},
                    ],
                },
            ]
        }
        with mock.patch("galgame_patcher.downloader.fetch_json", return_value=vndb_resp):
            vndb_cands = crawl_vndb_patches("sample")

        c_official_cdn = next(c for c in vndb_cands if "nekonyansoft.com" in c.url)
        c_official_other = next(c for c in vndb_cands if "mirror.example.org" in c.url)
        c_community = next(c for c in vndb_cands if "community.example.org" in c.url)
        self.assertTrue(c_official_cdn.trusted)
        self.assertFalse(c_official_other.trusted)
        self.assertTrue(c_official_other.bare_executable)
        self.assertFalse(c_community.trusted)

    def test_rule4_apply_blocks_untrusted_url_and_inspect_patch_file(self) -> None:
        local_zip = self.root / "p.zip"
        with zipfile.ZipFile(local_zip, "w") as zf:
            zf.writestr("patch.xp3", b"ZIP_DATA")
        zip_sha = compute_sha256(local_zip)

        # 1) 不带 expected_sha256 调用 apply(url) -> 抛 UntrustedSourceError，且不触发网络请求
        with mock.patch("galgame_patcher.patcher.download_patch") as mocked_dl:
            with self.assertRaises(UntrustedSourceError):
                self.patcher.apply("https://example.com/p.zip", game_dir=self.game_dir)
            mocked_dl.assert_not_called()

        # 2) 传入 allow_untrusted_url=True -> 进入下载流程
        with mock.patch(
            "galgame_patcher.patcher.download_patch", return_value=local_zip
        ) as mocked_dl:
            res = self.patcher.apply(
                "https://example.com/p.zip",
                game_dir=self.game_dir,
                allow_untrusted_url=True,
            )
            mocked_dl.assert_called_once()
            self.assertTrue(res.success)

        # 3) 传入 expected_sha256 -> 进入下载流程
        with mock.patch(
            "galgame_patcher.patcher.download_patch", return_value=local_zip
        ) as mocked_dl:
            res2 = self.patcher.apply(
                "https://example.com/p.zip",
                game_dir=self.game_dir,
                expected_sha256=zip_sha,
            )
            mocked_dl.assert_called_once()
            self.assertTrue(res2.success)

        # 4) catalog 条目无 expected_sha256 默认也被拦，传 allow_untrusted_url=True 放行
        cat = PatchCatalog(
            [
                CatalogEntry(
                    id="no_hash",
                    name="NoHashGame",
                    patch_url="https://example.com/p.zip",
                )
            ]
        )
        with mock.patch("galgame_patcher.patcher.download_patch") as mocked_dl:
            with self.assertRaises(UntrustedSourceError):
                self.patcher.apply_from_catalog(
                    "no_hash", catalog=cat, game_dir=self.game_dir
                )
            mocked_dl.assert_not_called()

        # 5) inspect_patch_file 检查 .zip 与 .exe
        local_exe = self.root / "mod_loader.exe"
        local_exe.write_bytes(b"MZ" + b"\x00" * 100)

        zip_info = inspect_patch_file(local_zip)
        exe_info = inspect_patch_file(local_exe)
        self.assertIsInstance(zip_info, PatchFileInspection)
        self.assertTrue(zip_info.is_archive)
        self.assertFalse(zip_info.is_bare_executable)
        self.assertIn("压缩包", zip_info.describe())
        self.assertIn(zip_sha, zip_info.describe())

        self.assertFalse(exe_info.is_archive)
        self.assertTrue(exe_info.is_bare_executable)
        self.assertIn("裸可执行文件", exe_info.describe())

    def test_rule5_cli_search_links_only_verify_file_and_gui_import(self) -> None:
        fake_cands = [
            OnlinePatchCandidate(
                title="Official Patch",
                url="https://nekonyansoft.com/patch.zip",
                source_site="VNDB",
                is_direct_download=True,
                language="en",
                official=True,
                trusted=True,
                trust_reason="VNDB 标记为官方发布，且域名命中发行商白名单",
                bare_executable=False,
            ),
            OnlinePatchCandidate(
                title="ThirdPartyTool (20 stars)",
                url="https://github.com/user/repo/installer.exe",
                source_site="GitHub",
                is_direct_download=False,
                trusted=False,
                trust_reason="GitHub 第三方仓库（20 stars），发布者未经审核",
                bare_executable=True,
            ),
        ]

        out_buf = io.StringIO()
        with mock.patch(
            "galgame_patcher.cli.discover_patches_online", return_value=fake_cands
        ), mock.patch("galgame_patcher.patcher.download_patch") as mocked_dl:
            with redirect_stdout(out_buf):
                code = cli_main(["search", "TestVN"])
            mocked_dl.assert_not_called()

        text = out_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("不下载任何文件", text)
        self.assertIn("[白名单]", text)
        self.assertIn("[未审核]", text)
        self.assertIn("该链接指向可执行文件", text)

        # --json 与 --open
        json_buf = io.StringIO()
        with mock.patch(
            "galgame_patcher.cli.discover_patches_online", return_value=fake_cands
        ), mock.patch("galgame_patcher.cli.webbrowser.open") as mocked_open:
            with redirect_stdout(json_buf):
                code_json = cli_main(["search", "TestVN", "--json", "--open", "1"])
        self.assertEqual(code_json, 0)
        mocked_open.assert_called_once_with("https://nekonyansoft.com/patch.zip")
        parsed = json.loads(json_buf.getvalue())
        self.assertEqual(len(parsed), 2)
        self.assertTrue(parsed[0]["trusted"])
        self.assertTrue(parsed[1]["bare_executable"])

        # verify-file 命令对可执行文件给出醒目警告
        exe_file = self.root / "patcher.exe"
        exe_file.write_bytes(b"MZ123")
        vf_buf = io.StringIO()
        with redirect_stdout(vf_buf):
            code_vf = cli_main(["verify-file", str(exe_file)])
        self.assertEqual(code_vf, 0)
        self.assertIn("请确认来源可信", vf_buf.getvalue())

        # CLI apply 传无哈希 URL 返回 1 且不下载
        err_buf = io.StringIO()
        with mock.patch("galgame_patcher.patcher.download_patch") as mocked_dl:
            with redirect_stderr(err_buf):
                code_apply = cli_main(
                    ["apply", "https://example.com/bad.zip", "--game-dir", str(self.game_dir)]
                )
            mocked_dl.assert_not_called()
        self.assertEqual(code_apply, 1)
        self.assertIn("--allow-untrusted-url", err_buf.getvalue())

        # GUI 导入与结构检查（无 tkinter 时允许跳过）
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("当前 Python 无 tkinter，跳过 GUI 导入验证")

        from galgame_patcher import gui

        self.assertTrue(hasattr(gui, "GalgamePatcherGUI"))
        # 确认源码中没有“填入选中链接”按钮与 _use_selected
        gui_source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertNotIn("填入选中链接", gui_source)
        self.assertNotIn("_use_selected", gui_source)


if __name__ == "__main__":
    unittest.main()
