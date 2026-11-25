"""第二轮审计修复的测试：清单信任、资源上限、状态一致性与检索匹配。"""

from __future__ import annotations

import functools
import hashlib
import http.server
import io
import json
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import GalgamePatcher, PatchCatalog
from galgame_patcher.archive import (
    _precheck_7z_limits,
    extract_archive,
)
from galgame_patcher.cli import main as cli_main
from galgame_patcher.downloader import (
    crawl_07th_mod_patches,
    crawl_github_patch_releases,
    download_patch,
    fetch_bytes,
)


class _SilentHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class _StreamingHandler(http.server.BaseHTTPRequestHandler):
    """无限输出流的服务端，用于验证下载体积上限。"""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        chunk = b"A" * (64 * 1024)
        try:
            for _ in range(100_000):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionAbortedError, OSError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


class _DeclaredLargeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(10 * 1024 * 1024))
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class TestCatalogTrust(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.web_dir = self.root / "web"
        self.web_dir.mkdir()
        self.local_path = self.root / "catalog.json"
        self.local_path.write_text(
            json.dumps({"version": 1, "games": []}), encoding="utf-8"
        )
        self.remote_path = self.web_dir / "remote_catalog.json"
        self._write_remote(
            {
                "version": 2,
                "games": [
                    {
                        "id": "remote_game",
                        "name": "Remote Game",
                        "patch_url": "https://example.com/patch.zip",
                        "expected_sha256": "a" * 64,
                    }
                ],
            }
        )
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(_SilentHTTPHandler, directory=str(self.web_dir)),
        )
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/remote_catalog.json"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _write_remote(self, payload: dict[str, object]) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.remote_path.write_bytes(raw)
        return raw

    def _run(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(
                [
                    "catalog",
                    "update",
                    self.url,
                    "--catalog",
                    str(self.local_path),
                    *extra,
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def test_update_requires_trust_flag_or_pin(self) -> None:
        original = self.local_path.read_text(encoding="utf-8")
        code, _out, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn("未经校验", err)
        self.assertEqual(self.local_path.read_text(encoding="utf-8"), original)

    def test_update_rejects_wrong_pin(self) -> None:
        code, _out, err = self._run("--trust-catalog-sha256", "b" * 64)
        self.assertEqual(code, 1)
        self.assertIn("SHA256 校验失败", err)

    def test_update_with_correct_pin_prints_changes(self) -> None:
        digest = hashlib.sha256(self.remote_path.read_bytes()).hexdigest()
        code, out, _err = self._run("--trust-catalog-sha256", digest)
        self.assertEqual(code, 0)
        self.assertIn("远程清单 SHA256", out)
        self.assertIn(digest, out)
        self.assertIn("[新增] remote_game", out)
        self.assertIn("https://example.com/patch.zip", out)
        saved = PatchCatalog.from_file(self.local_path)
        self.assertEqual(saved.version, 2)
        self.assertEqual([entry.id for entry in saved.entries], ["remote_game"])

    def test_update_with_allow_untrusted_warns(self) -> None:
        code, _out, err = self._run("--allow-untrusted-catalog")
        self.assertEqual(code, 0)
        self.assertIn("警告", err)
        self.assertEqual(
            PatchCatalog.from_file(self.local_path).version,
            2,
        )

    def test_update_rejects_invalid_remote_catalog(self) -> None:
        self._write_remote(
            {
                "version": 3,
                "games": [
                    {
                        "id": "evil",
                        "name": "Evil Game",
                        "target_subdir": "../outside",
                    }
                ],
            }
        )
        code, _out, err = self._run("--allow-untrusted-catalog")
        self.assertEqual(code, 1)
        self.assertIn("未通过校验", err)
        self.assertEqual(
            PatchCatalog.from_file(self.local_path).version,
            1,
        )

    def test_library_update_requires_trust(self) -> None:
        catalog = PatchCatalog.from_file(self.local_path)
        with self.assertRaisesRegex(ValueError, "未经校验"):
            catalog.update_from_remote(self.url, target_path=self.local_path)


class TestDownloadLimits(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _serve(self, handler: type[http.server.BaseHTTPRequestHandler]) -> tuple[http.server.ThreadingHTTPServer, str]:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/patch.zip"
        return server, url

    def test_streaming_download_stops_at_limit(self) -> None:
        server, url = self._serve(_StreamingHandler)
        dest = self.root / "downloads"
        try:
            with self.assertRaisesRegex(ValueError, "超过上限"):
                download_patch(url, dest_dir=dest, max_bytes=512 * 1024, timeout=10)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(list(dest.glob("*.part")), [])

    def test_content_length_over_limit_rejected_before_read(self) -> None:
        server, url = self._serve(_DeclaredLargeHandler)
        try:
            with self.assertRaisesRegex(ValueError, "声明大小"):
                download_patch(url, dest_dir=self.root / "dl", max_bytes=1024, timeout=10)
        finally:
            server.shutdown()
            server.server_close()

    def test_metadata_response_is_capped(self) -> None:
        server, url = self._serve(_StreamingHandler)
        try:
            with self.assertRaisesRegex(ValueError, "超过"):
                fetch_bytes(url, timeout=10, max_bytes=1024)
        finally:
            server.shutdown()
            server.server_close()


class TestArchiveLimits(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zip_ratio_limit(self) -> None:
        archive = self.root / "bomb.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("zeros.bin", b"\x00" * (2 * 1024 * 1024))
        out = self.root / "out_ratio"
        with mock.patch("galgame_patcher.archive.MAX_COMPRESSION_RATIO", 50):
            with self.assertRaisesRegex(ValueError, "压缩炸弹"):
                extract_archive(archive, out)
        self.assertEqual(list(out.rglob("*")), [])

    def test_zip_absolute_limit(self) -> None:
        archive = self.root / "big.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("big.bin", b"x" * 2048)
        out = self.root / "out_abs"
        with mock.patch("galgame_patcher.archive.MAX_EXTRACTED_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "超过上限"):
                extract_archive(archive, out)
        self.assertEqual(list(out.rglob("*")), [])

    def test_tar_absolute_limit(self) -> None:
        payload = self.root / "big.bin"
        payload.write_bytes(b"x" * 2048)
        archive = self.root / "big.tar"
        with tarfile.open(archive, "w") as tf:
            tf.add(payload, arcname="big.bin")
        with mock.patch("galgame_patcher.archive.MAX_EXTRACTED_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "超过上限"):
                extract_archive(archive, self.root / "out_tar")

    def test_external_7z_precheck_reads_sizes(self) -> None:
        archive = self.root / "dummy.7z"
        archive.write_bytes(b"7z")
        proc = subprocess.CompletedProcess(
            args=["7z"], returncode=0, stdout="Path = big.bin\nSize = 2048\n", stderr=""
        )
        with mock.patch(
            "galgame_patcher.archive._run_external", return_value=proc
        ), mock.patch("galgame_patcher.archive.MAX_EXTRACTED_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "超过上限"):
                _precheck_7z_limits("7z", archive)


class TestSkippedAndStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir()
        (self.game_dir / "a.xp3").write_bytes(b"ORIG_A")
        self.patch_dir = self.root / "patch"
        self.patch_dir.mkdir()
        (self.patch_dir / "a.xp3").write_bytes(b"PATCH_A")
        self.patcher = GalgamePatcher()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reserved_backup_dir_files_are_reported(self) -> None:
        (self.patch_dir / "normal.xp3").write_bytes(b"NORMAL")
        nested = self.patch_dir / "sub" / ".galpatch_backup"
        nested.mkdir(parents=True)
        (nested / "x.txt").write_text("x", encoding="utf-8")
        backup_dir = self.patch_dir / ".galpatch_backup"
        backup_dir.mkdir()
        (backup_dir / "manifest.json").write_text("{}", encoding="utf-8")

        result = self.patcher.apply(
            patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
        )

        self.assertTrue(result.success, result.message)
        self.assertEqual(sorted(result.skipped_files), [
            ".galpatch_backup/manifest.json",
            "sub/.galpatch_backup/x.txt",
        ])
        self.assertIn("已跳过", result.message)
        self.assertTrue((self.game_dir / "normal.xp3").is_file())
        self.assertFalse((self.game_dir / ".galpatch_backup" / "x.txt").exists())
        manifest = json.loads(
            (self.game_dir / ".galpatch_backup" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["status"], "committed")

    def test_no_backup_install_reports_no_history(self) -> None:
        self.assertTrue(
            self.patcher.apply(
                patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
            ).success
        )
        self.assertEqual(
            self.patcher.install_status(self.game_dir).state, "installed"
        )

        self.assertTrue(
            self.patcher.apply(
                patch_source=str(self.patch_dir),
                game_dir=str(self.game_dir),
                backup=False,
            ).success
        )
        status = self.patcher.install_status(self.game_dir)
        self.assertEqual(status.state, "installed_no_history")
        self.assertIn("不支持逐次撤回", status.state_label)
        self.assertEqual(status.history, [])

        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli_main(["status", str(self.game_dir)]), 0)
        self.assertIn("不支持逐次撤回", out.getvalue())

    def test_keep_patches_without_records_fails_cleanly(self) -> None:
        self.assertTrue(
            self.patcher.apply(
                patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
            ).success
        )
        self.assertTrue(self.patcher.rollback(self.game_dir).success)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = cli_main(
                ["rollback", str(self.game_dir), "--mode", "keep-patches"]
            )
        self.assertEqual(code, 1)
        self.assertIn("没有可撤回的逐次安装记录", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TestSearchFixes(unittest.TestCase):
    def test_07th_two_char_chinese_query_matches_english_repo(self) -> None:
        repos = [
            {
                "full_name": "07th-mod/higurashi-console-arcs",
                "html_url": "https://github.com/07th-mod/higurashi-console-arcs",
                "description": "",
                "stargazers_count": 5,
            },
            {
                "full_name": "07th-mod/unrelated-tool",
                "html_url": "https://github.com/07th-mod/unrelated-tool",
                "description": "build helper",
                "stargazers_count": 5,
            },
        ]
        with mock.patch(
            "galgame_patcher.downloader.fetch_json", return_value=repos
        ):
            candidates = crawl_07th_mod_patches("寒蝉")
        urls = [candidate.url for candidate in candidates]
        self.assertIn("https://github.com/07th-mod/higurashi-console-arcs", urls)
        self.assertNotIn("https://github.com/07th-mod/unrelated-tool", urls)

    def test_github_default_star_threshold_keeps_small_repos(self) -> None:
        search_payload = {
            "items": [
                {
                    "full_name": "small-dev/patch-repo",
                    "html_url": "https://github.com/small-dev/patch-repo",
                    "description": "Patch repo",
                    "stargazers_count": 3,
                    "owner": {"login": "small-dev"},
                }
            ]
        }
        release_payload = {"tag_name": "", "assets": []}

        def _fake_fetch_json(url: str, **_kwargs: object) -> dict[str, object]:
            if "/search/repositories" in url:
                return search_payload
            if "/releases/latest" in url:
                return release_payload
            raise AssertionError(url)

        with mock.patch(
            "galgame_patcher.downloader.fetch_json", side_effect=_fake_fetch_json
        ):
            candidates = crawl_github_patch_releases("sample")
        self.assertTrue(
            any("small-dev/patch-repo" in candidate.url for candidate in candidates)
        )


if __name__ == "__main__":
    unittest.main()
