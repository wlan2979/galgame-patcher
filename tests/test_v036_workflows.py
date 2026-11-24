"""回归测试：元数据请求、发行商来源、安装历史和失败诊断。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from galgame_patcher import downloader
from galgame_patcher.cli import main
from galgame_patcher.locator import ManualLocator
from galgame_patcher.patcher import GalgamePatcher


class _Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class MetadataPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        for name, value in (("time", self.clock), ("_next_request", {}), ("_robots_cache", {})):
            patcher = mock.patch.object(downloader, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_robots_and_retry_after_share_host_budget(self):
        url = "https://example.test/patch"
        calls = []

        def open_request(target, **_kwargs):
            calls.append((target, self.clock.now))
            if target.endswith("robots.txt"):
                return io.BytesIO(b"User-agent: *\nDisallow: /private\n")
            if len(calls) == 2:
                raise HTTPError(url, 429, "Too Many Requests", {"Retry-After": "2"}, io.BytesIO())
            return io.BytesIO(b"patches")

        with mock.patch.object(downloader, "open_url_request", side_effect=open_request):
            self.assertEqual(downloader.fetch_text(url, timeout=6), "patches")
            with self.assertRaisesRegex(ValueError, "禁止自动访问"):
                downloader.fetch_text("https://example.test/private/patch", timeout=6)
        self.assertEqual(calls, [("https://example.test/robots.txt", 0.0), (url, 1.0), (url, 3.0)])

    def test_vndb_post_retry_is_bounded_and_paced(self):
        url = "https://api.vndb.org/kana/release"
        calls = []

        def open_request(target, **kwargs):
            calls.append((self.clock.now, kwargs["timeout"]))
            raise HTTPError(target, 503, "Unavailable", {}, io.BytesIO())

        with mock.patch.object(downloader, "open_url_request", side_effect=open_request):
            with self.assertRaises(HTTPError):
                downloader.fetch_json(url, method="POST", payload={}, timeout=10)
        self.assertEqual(calls, [(0.0, 3.0), (1.6, 3.0), (3.2, 3.0)])

    def test_kagura_returns_known_dlc_link_when_robots_unavailable(self):
        failures = []
        with mock.patch.object(downloader, "fetch_text", side_effect=ValueError("robots.txt HTTP 403")):
            candidates = downloader.crawl_kagura_patches(
                "Summer Memories+ 去遮挡补丁", on_failure=lambda *event: failures.append(event)
            )
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].manual_install)
        self.assertTrue(candidates[0].url.endswith("/summer-memories-expansion-dlc-patch/"))
        self.assertIn("403", failures[0][1])

    def test_kagura_accepts_only_publisher_product_pages(self):
        html = """
            <a href="/product/fixture-game-steam-patch/">Fixture Game Steam Patch</a>
            <a href="/product/fixture-game-steam-patch?ref=2">Fixture Game Steam Patch</a>
            <a href="/product/another-fixture-game2-patch/">Another Fixture Game2 Patch</a>
            <a href="https://example.test/product/fixture-game-patch/">Fixture Game Patch</a>
            <a href="/download/fixture-game.zip">Fixture Game Patch</a>
        """
        with mock.patch.object(downloader, "fetch_text", return_value=html):
            candidates = downloader.crawl_kagura_patches("Fixture Game")
        self.assertEqual(
            [item.url for item in candidates],
            ["https://www.kaguragames.com/product/fixture-game-steam-patch/"],
        )

    def test_senren_release_page_remains_available_when_robots_blocks_search(self):
        failures = []
        with mock.patch.object(downloader, "fetch_text", side_effect=ValueError("robots.txt HTTP 403")):
            candidates = downloader.crawl_yuzusoft_patches(
                "千恋万花", on_failure=lambda *event: failures.append(event)
            )
            self.assertEqual(downloader.crawl_yuzusoft_patches("Other Game"), [])
        self.assertEqual(candidates[0].url, "https://www.yuzu-soft.com/products/senren/download.html")
        self.assertTrue(candidates[0].manual_install)
        self.assertIn("403", failures[0][1])


class RollbackAndDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.game = self.root / "game"
        self.game.mkdir()
        self.backup = self.game / ".galpatch_backup"
        (self.game / "undo.json").write_text("vanilla", encoding="utf-8")
        self.patcher = GalgamePatcher()

    def _install(self, name, files, **options):
        patch = self.root / name
        patch.mkdir()
        for filename, content in files.items():
            (patch / filename).write_text(content, encoding="utf-8")
        self.assertTrue(self.patcher.apply(patch, game_dir=self.game, **options).success)

    def test_undo_and_restore_to_record_preserve_prior_patches(self):
        self._install("patch1", {"undo.json": "first", "older.xp3": "older"})
        first_id = self.patcher.rollback_history(self.game)[0][0]
        self._install("patch2", {"undo.json": "second", "recent.xp3": "newer"})
        self._install("patch3", {"undo.json": "third"})
        self._install("patch4", {"undo.json": "fourth"})
        self.assertEqual(len(self.patcher.rollback_history(self.game)), 4)
        self.assertTrue(self.patcher.rollback(self.game, mode="keep-patches").success)
        self.assertEqual((self.game / "undo.json").read_text(), "third")

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["rollback", str(self.game), "--history"]), 0)
            self.assertIn(first_id, output.getvalue())
            self.assertEqual(main(["rollback", str(self.game), "--to", first_id]), 0)
        self.assertEqual((self.game / "undo.json").read_text(), "first")
        self.assertTrue((self.game / "older.xp3").exists())
        self.assertFalse((self.game / "recent.xp3").exists())
        self.assertTrue(self.patcher.rollback(self.game, mode="keep-patches").success)
        self.assertEqual((self.game / "undo.json").read_text(), "vanilla")
        self.assertFalse((self.game / "older.xp3").exists())
        self.assertEqual(self.patcher.rollback_history(self.game), [])

    def test_corrupt_manifest_blocks_apply_and_warns_during_scan(self):
        self._install("patch1", {"undo.json": "patched"})
        manifest = self.backup / "manifest.json"
        manifest.write_text("{", encoding="utf-8")
        patch = self.root / "undo.json"
        patch.write_text("replacement", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["apply", str(patch), "--game-dir", str(self.game), "--verbose"]), 1)
        self.assertIn("无法读取备份清单", errors.getvalue())
        self.assertIn("Traceback", errors.getvalue())
        self.assertEqual((self.game / "undo.json").read_text(), "patched")
        self.assertEqual(manifest.read_text(), "{")

        with mock.patch.dict(os.environ, {"GALGAME_PATCHER_APPDATA": str(self.root)}):
            ManualLocator().add_game(self.game, name="Fixture")
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["scan", "--only-installed", "--include-manual", "--verbose"]), 0)
        self.assertIn("补丁状态待确认", output.getvalue())
        self.assertIn("无法确认补丁状态", errors.getvalue())

    def test_legacy_full_rollback_reports_missing_file(self):
        self._install("patch1", {"undo.json": "patched"})
        manifest = self.backup / "manifest.json"
        data = json.loads(manifest.read_text())
        del data["latest_install"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.patcher.rollback_history(self.game), [])
        original = self.backup / "files" / "undo.json"
        original.unlink()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["rollback", str(self.game)]), 1)
        self.assertIn("undo.json: FileNotFoundError", errors.getvalue())
        self.assertTrue(manifest.exists())
        original.write_text("vanilla", encoding="utf-8")
        self.assertTrue(self.patcher.rollback(self.game).success)
        self.assertEqual((self.game / "undo.json").read_text(), "vanilla")

    def test_install_without_backup_stops_history_but_keeps_original(self):
        self._install("patch1", {"undo.json": "patched"})
        self._install("patch2", {"undo.json": "untracked"}, backup=False)
        self.assertEqual(self.patcher.rollback_history(self.game), [])
        with self.assertRaisesRegex(ValueError, "没有可撤回"):
            self.patcher.rollback(self.game, mode="keep-patches")
        self.assertEqual((self.game / "undo.json").read_text(), "untracked")
        self.assertTrue(self.patcher.rollback(self.game).success)
        self.assertEqual((self.game / "undo.json").read_text(), "vanilla")
