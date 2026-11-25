"""针对代码审计 16 项问题的回归测试。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import PatchCatalog, SteamLocator, extract_archive
from galgame_patcher import downloader
from galgame_patcher.archive import _try_extract_with_7z
from galgame_patcher.cli import main as cli_main
from galgame_patcher.locator import ManualLocator
from galgame_patcher.patcher import GalgamePatcher


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class AuditFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.appdata_dir = self.root / "appdata"
        self.appdata_dir.mkdir()
        self._old_appdata_env = os.environ.get("GALGAME_PATCHER_APPDATA")
        os.environ["GALGAME_PATCHER_APPDATA"] = str(self.appdata_dir)

        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir()
        (self.game_dir / "Game.exe").write_bytes(b"ORIG_EXE")
        self.patcher = GalgamePatcher()

    def tearDown(self) -> None:
        if self._old_appdata_env is None:
            os.environ.pop("GALGAME_PATCHER_APPDATA", None)
        else:
            os.environ["GALGAME_PATCHER_APPDATA"] = self._old_appdata_env
        self._tmp.cleanup()

    # 1. 外部 7z 返回码 1（有文件未解压）必须算失败
    def test_external_7z_returncode_1_is_failure(self) -> None:
        dummy = self.root / "partial.7z"
        dummy.write_bytes(b"7z")
        proc = __import__("subprocess").CompletedProcess(
            args=["7z"], returncode=1, stdout="", stderr="ERROR: Data Error in setup.exe"
        )
        with mock.patch.dict(sys.modules, {"py7zr": None}), mock.patch(
            "galgame_patcher.archive._find_7z_executable", return_value="7z"
        ), mock.patch(
            "galgame_patcher.archive._find_bsdtar_executable", return_value=None
        ), mock.patch("galgame_patcher.archive.subprocess.run", return_value=proc):
            with self.assertRaises(ValueError) as ctx:
                extract_archive(dummy, self.root / "out7z")

        self.assertIn("returncode=1", str(ctx.exception))

    def test_try_extract_with_7z_rejects_returncode_1(self) -> None:
        proc = __import__("subprocess").CompletedProcess(
            args=["7z"], returncode=1, stdout="Unsupported Method", stderr=""
        )
        with mock.patch("galgame_patcher.archive._run_external", return_value=proc):
            ok, detail = _try_extract_with_7z(
                "7z", Path("a.7z"), self.root / "out", [None]
            )
        self.assertFalse(ok)
        self.assertIn("returncode=1", detail)

    # 3. 结构错误但仍是合法 JSON 的清单不能崩 GUI 启动
    def test_catalog_rejects_non_list_games_and_default_falls_back(self) -> None:
        storage = PatchCatalog.default_storage_path()
        storage.parent.mkdir(parents=True, exist_ok=True)
        storage.write_text(
            json.dumps({"version": 1, "games": {"evil": 1}}), encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            PatchCatalog.from_dict({"version": 1, "games": {"evil": 1}})
        with self.assertRaises(ValueError):
            PatchCatalog.from_dict({"version": 1, "games": [1, "two"]})

        catalog = PatchCatalog.default()
        self.assertEqual([entry.id for entry in catalog.entries], ["example_game"])

    # 7. py7zr 只解压一次（solid 压缩包不再逐成员重复解码）
    def test_py7zr_extracts_archive_in_one_pass(self) -> None:
        calls: list[object] = []

        class FakeSevenZip:
            def __init__(self, _path: object, mode: str = "r", password: object = None):
                self.password = password

            def __enter__(self) -> FakeSevenZip:
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def getnames(self) -> list[str]:
                return ["top/a.xp3", "top/b.xp3"]

            def list(self) -> list[object]:
                return []

            def extract(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("不应逐成员调用 extract()")

            def extractall(self, path: object) -> None:
                calls.append(path)
                target = Path(path) / "top"
                target.mkdir(parents=True, exist_ok=True)
                (target / "a.xp3").write_bytes(b"A")
                (target / "b.xp3").write_bytes(b"B")

        fake_module = types.ModuleType("py7zr")
        fake_module.SevenZipFile = FakeSevenZip  # type: ignore[attr-defined]
        archive_path = self.root / "patch.7z"
        archive_path.write_bytes(b"7z")
        out_dir = self.root / "out_py7zr"

        with mock.patch.dict(sys.modules, {"py7zr": fake_module}):
            extract_archive(archive_path, out_dir)

        self.assertEqual(len(calls), 1)
        self.assertEqual((out_dir / "top" / "a.xp3").read_bytes(), b"A")
        self.assertEqual((out_dir / "top" / "b.xp3").read_bytes(), b"B")

    # 9. 导出清单默认不带明文密码
    def test_catalog_export_can_omit_archive_password(self) -> None:
        catalog = PatchCatalog.from_dict(
            {
                "version": 1,
                "games": [
                    {
                        "id": "g1",
                        "name": "Game One",
                        "archive_password": "s3cret-pass",
                    }
                ],
            }
        )
        out_path = self.root / "export.json"
        catalog.save_to_file(out_path, include_passwords=False)
        raw = out_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret-pass", raw)
        self.assertNotIn("archive_password", raw)

        catalog.save_to_file(out_path)
        self.assertIn("s3cret-pass", out_path.read_text(encoding="utf-8"))

    # 10. HEAD 405 时用 Range GET 复检
    def test_check_url_head_falls_back_to_range_get(self) -> None:
        calls: list[str] = []

        def _open(url: str, **kwargs: object):
            method = str(kwargs.get("method") or "GET")
            calls.append(method)
            if method == "HEAD":
                raise urllib.error.HTTPError(url, 405, "Method Not Allowed", {}, io.BytesIO())
            return _FakeResponse(206)

        with mock.patch.object(downloader, "open_url_request", side_effect=_open):
            ok, detail = downloader.check_url_head("https://example.com/patch.zip")

        self.assertTrue(ok)
        self.assertEqual(detail, "HTTP 206")
        self.assertEqual(calls, ["HEAD", "GET"])

    def test_check_url_head_does_not_retry_plain_404(self) -> None:
        calls: list[str] = []

        def _open(url: str, **kwargs: object):
            calls.append(str(kwargs.get("method") or "GET"))
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())

        with mock.patch.object(downloader, "open_url_request", side_effect=_open):
            ok, detail = downloader.check_url_head("https://example.com/missing.zip")

        self.assertFalse(ok)
        self.assertEqual(detail, "HTTP 404")
        self.assertEqual(calls, ["HEAD"])

    # 14. 默认下载缓存会清理过期目录
    def test_download_cache_prunes_old_entries(self) -> None:
        cache_root = self.root / "cache"
        old_dir = cache_root / "old"
        fresh_dir = cache_root / "fresh"
        keep_dir = cache_root / "keep"
        for item in (old_dir, fresh_dir, keep_dir):
            item.mkdir(parents=True)
            (item / "patch.zip").write_bytes(b"x")

        old_time = 30 * 24 * 60 * 60
        os.utime(old_dir, (0, old_time))
        # 最近访问过的目录保留
        downloader._cleanup_download_cache(cache_root, keep=keep_dir)

        self.assertFalse(old_dir.exists())
        self.assertTrue(fresh_dir.exists())
        self.assertTrue(keep_dir.exists())

    # 16. 回滚不能删除安装前就存在的空目录，但应删除补丁新建的目录
    def _build_patch_dir(self) -> Path:
        patch_src = self.root / "patchsrc" / "newdir"
        patch_src.mkdir(parents=True)
        (patch_src / "x.xp3").write_bytes(b"PATCH_X")
        return self.root / "patchsrc"

    def test_full_rollback_keeps_preexisting_empty_dir(self) -> None:
        (self.game_dir / "mods").mkdir()
        patch_src = self._build_patch_dir()

        result = self.patcher.apply(patch_src, game_dir=self.game_dir)
        self.assertTrue(result.success, result.message)
        self.assertTrue((self.game_dir / "newdir" / "x.xp3").is_file())

        rolled = self.patcher.rollback(self.game_dir, mode="full")
        self.assertTrue(rolled.success, rolled.message)
        self.assertTrue((self.game_dir / "mods").is_dir())
        self.assertFalse((self.game_dir / "newdir").exists())

    def test_undo_latest_install_keeps_preexisting_empty_dir(self) -> None:
        (self.game_dir / "mods").mkdir()
        patch_src = self._build_patch_dir()

        result = self.patcher.apply(patch_src, game_dir=self.game_dir)
        self.assertTrue(result.success, result.message)

        undone = self.patcher.rollback(self.game_dir, mode="keep-patches")
        self.assertTrue(undone.success, undone.message)
        self.assertTrue((self.game_dir / "mods").is_dir())
        self.assertFalse((self.game_dir / "newdir").exists())

    # 6. 深度扫描只执行一次
    def test_scan_games_runs_deep_scan_once(self) -> None:
        entries = [
            {"id": f"g{i}", "name": f"Game {i}", "install_dir_name": f"Missing{i}"}
            for i in range(3)
        ]
        catalog = PatchCatalog.from_dict({"version": 1, "games": entries})
        locator = SteamLocator(
            steam_root=self.root / "no-steam",
            manual_locator=ManualLocator(),
        )
        candidate = self.root / "FoundGame"
        candidate.mkdir()
        deep_calls: list[object] = []

        def _fake_deep_scan(*, roots=None):
            deep_calls.append(roots)
            return [candidate]

        with mock.patch(
            "galgame_patcher.catalog.deep_scan_games", side_effect=_fake_deep_scan
        ), mock.patch(
            "galgame_patcher.locator.iter_deep_scan_candidates",
            side_effect=AssertionError("不应重复扫描磁盘"),
        ):
            statuses = catalog.scan_games(
                locator=locator, deep_scan=True, deep_scan_roots=[self.root]
            )

        self.assertEqual(len(deep_calls), 1)
        self.assertTrue(
            any(status.installed and status.game_dir == candidate.resolve() for status in statuses)
        )

    # 8. catalog verify 使用 URL 清单时不能把结果写回 https:\... 路径
    def test_catalog_verify_does_not_save_remote_catalog(self) -> None:
        catalog = PatchCatalog(entries=[], version=1)
        catalog.verify_links = mock.Mock(return_value={})  # type: ignore[method-assign]
        catalog.save_to_file = mock.Mock()  # type: ignore[method-assign]

        with mock.patch(
            "galgame_patcher.cli.PatchCatalog.load", return_value=catalog
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli_main(
                ["catalog", "verify", "--catalog", "https://example.com/catalog.json"]
            )

        self.assertEqual(code, 0)
        catalog.save_to_file.assert_not_called()

    # 5. 安装进行中关闭窗口应等待而不是直接销毁
    def test_gui_close_waits_for_active_install(self) -> None:
        try:
            from galgame_patcher import gui
        except ImportError:
            self.skipTest("当前 Python 未包含 tkinter，跳过 GUI 测试")

        app = gui.GalgamePatcherGUI.__new__(gui.GalgamePatcherGUI)
        app.root = mock.Mock()
        app._closing = False
        app._install_active = True
        app._active_thread = mock.Mock()
        app._active_thread.is_alive.return_value = True
        app._poll_after_id = None
        app._cancel_scan_flag = False
        app._set_status = mock.Mock()
        app._save_gui_config = mock.Mock()

        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            app._on_close()

        app.root.destroy.assert_not_called()
        app.root.after.assert_called_once_with(200, app._close_when_idle)

    # 15. 补丁 SHA256 在后台线程计算，界面线程只处理结果
    def test_gui_inspects_patch_offscreen(self) -> None:
        try:
            from galgame_patcher import gui
        except ImportError:
            self.skipTest("当前 Python 未包含 tkinter，跳过 GUI 测试")

        app = gui.GalgamePatcherGUI.__new__(gui.GalgamePatcherGUI)
        app._ui_queue = queue.Queue()
        app._inspecting = False
        app._inspected_key = None
        app._set_inspect_text = mock.Mock()

        patch = self.root / "big_patch.zip"
        patch.write_bytes(b"patch-bytes")
        on_ready = mock.Mock()

        app._inspect_patch_async(
            str(patch), ask_exec_confirm=True, on_ready=on_ready
        )
        self.assertTrue(app._inspecting)

        kind, inspection, ask_confirm, callback = app._ui_queue.get(timeout=5)
        self.assertEqual(kind, "inspect_done")
        self.assertEqual(
            inspection.sha256, hashlib.sha256(b"patch-bytes").hexdigest()
        )
        self.assertTrue(ask_confirm)
        self.assertIs(callback, on_ready)
        # 回调必须等 UI 线程处理队列后才会执行
        on_ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
