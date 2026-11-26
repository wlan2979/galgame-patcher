"""安装状态查询 / 中断恢复与清单编辑工具的测试。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import GalgamePatcher, PatchCatalog
from galgame_patcher.cli import main as cli_main
from galgame_patcher.models import CatalogEntry


class TestInstallStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir()
        (self.game_dir / "a.xp3").write_bytes(b"ORIG_A")
        self.patch_dir = self.root / "patch"
        self.patch_dir.mkdir()
        (self.patch_dir / "a.xp3").write_bytes(b"PATCH_A")
        (self.patch_dir / "new.dat").write_bytes(b"NEW_FILE")
        self.patcher = GalgamePatcher()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _apply(self) -> None:
        result = self.patcher.apply(
            patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
        )
        self.assertTrue(result.success, result.message)

    def _mark_incomplete(self) -> None:
        manifest_path = self.game_dir / ".galpatch_backup" / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["status"] = "in_progress"
        manifest_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def test_status_lifecycle(self) -> None:
        status = self.patcher.install_status(self.game_dir)
        self.assertEqual(status.state, "not_installed")
        self.assertEqual(status.state_label, "未安装补丁")
        self.assertIsNone(status.manifest_path)

        self._apply()
        status = self.patcher.install_status(self.game_dir)
        self.assertEqual(status.state, "installed")
        self.assertEqual(status.state_label, "已安装补丁")
        self.assertIsNotNone(status.manifest_path)
        self.assertTrue(status.latest_install.startswith("install-"))
        self.assertTrue(status.applied_at)
        self.assertEqual(status.backed_up_files, 1)
        self.assertEqual(status.newly_added_files, 1)
        self.assertEqual(len(status.history), 1)
        self.assertEqual(status.history[0][0], status.latest_install)
        self.assertEqual(status.to_dict()["state"], "installed")

        self._mark_incomplete()
        status = self.patcher.install_status(self.game_dir)
        self.assertEqual(status.state, "incomplete")
        self.assertEqual(status.state_label, "上次安装未完成")

    def test_incomplete_manifest_blocks_apply_with_recovery_hint(self) -> None:
        self._apply()
        self._mark_incomplete()
        with self.assertRaisesRegex(ValueError, "先回滚"):
            self.patcher.apply(
                patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
            )

    def test_recover_incomplete_install_restores_original_files(self) -> None:
        self._apply()
        self._mark_incomplete()

        result = self.patcher.recover_incomplete_install(self.game_dir)

        self.assertTrue(result.success, result.message)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertFalse((self.game_dir / "new.dat").exists())
        self.assertEqual(
            self.patcher.install_status(self.game_dir).state, "not_installed"
        )

    def test_recover_requires_incomplete_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "没有需要恢复"):
            self.patcher.recover_incomplete_install(self.game_dir)

        self._apply()
        with self.assertRaisesRegex(ValueError, "没有需要恢复"):
            self.patcher.recover_incomplete_install(self.game_dir)
        self.assertTrue((self.game_dir / "new.dat").exists())

    def test_status_on_missing_directory(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.patcher.install_status(self.root / "missing")

    def test_invalid_manifest_reports_invalid_state(self) -> None:
        backup_root = self.game_dir / ".galpatch_backup"
        backup_root.mkdir()
        (backup_root / "manifest.json").write_text("{not json", encoding="utf-8")
        status = self.patcher.install_status(self.game_dir)
        self.assertEqual(status.state, "invalid")
        self.assertIn("备份清单", status.message)


class TestStatusAndRecoverCli(unittest.TestCase):
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
        self.assertTrue(
            self.patcher.apply(
                patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
            ).success
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_status_json_and_human_output(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(["status", str(self.game_dir), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["state"], "installed")
        self.assertEqual(payload["state_label"], "已安装补丁")
        self.assertEqual(payload["backed_up_files"], 1)

        human = io.StringIO()
        with redirect_stdout(human):
            code = cli_main(["status", str(self.game_dir)])
        self.assertEqual(code, 0)
        self.assertIn("已安装补丁", human.getvalue())
        self.assertIn("最近安装", human.getvalue())

    def test_status_missing_dir_returns_error(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = cli_main(["status", str(self.root / "missing")])
        self.assertEqual(code, 1)
        self.assertIn("游戏目录不存在", err.getvalue())

    def test_recover_without_yes_requires_interactive_stdin(self) -> None:
        manifest_path = self.game_dir / ".galpatch_backup" / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["status"] = "in_progress"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        with mock.patch("sys.stdin.isatty", return_value=False):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = cli_main(["recover", str(self.game_dir)])
        self.assertEqual(code, 1)
        self.assertIn("--yes", err.getvalue())
        self.assertEqual(
            (self.game_dir / "a.xp3").read_bytes(), b"PATCH_A"
        )

    def test_recover_refuses_invalid_manifest(self) -> None:
        backup_root = self.game_dir / ".galpatch_backup"
        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text("{broken", encoding="utf-8")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = cli_main(["recover", str(self.game_dir), "--yes"])
        self.assertEqual(code, 1)
        self.assertIn("备份清单损坏", err.getvalue())

    def test_recover_yes_rolls_back_incomplete_install(self) -> None:
        manifest_path = self.game_dir / ".galpatch_backup" / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["status"] = "in_progress"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(["recover", str(self.game_dir), "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("已成功回滚补丁", out.getvalue())
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")

        with redirect_stdout(io.StringIO()) as again:
            code = cli_main(["recover", str(self.game_dir), "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("无需恢复", again.getvalue())


class TestCatalogValidate(unittest.TestCase):
    def test_valid_catalog_has_no_errors(self) -> None:
        catalog = PatchCatalog(
            entries=[
                CatalogEntry(
                    id="good",
                    name="Good Game",
                    steam_app_id=123,
                    patch_url="https://example.com/p.zip",
                    expected_sha256="a" * 64,
                    target_subdir="mods",
                )
            ]
        )
        errors, warnings = catalog.validate()
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_invalid_catalog_reports_all_problems(self) -> None:
        catalog = PatchCatalog(
            entries=[
                CatalogEntry(
                    id="",
                    name="",
                    steam_app_id=0,
                    install_dir_name="bad/name",
                    patch_url="ftp://example.com/p.zip",
                    expected_sha256="xyz",
                    target_subdir="../outside",
                ),
                CatalogEntry(id="dup", name="First"),
                CatalogEntry(
                    id="dup",
                    name="Second",
                    patch_url="https://example.com/p.zip",
                ),
            ]
        )
        errors, warnings = catalog.validate()
        joined = "\n".join(errors)
        self.assertIn("id 不能为空", joined)
        self.assertIn("id 与其他条目重复", joined)
        self.assertIn("name 不能为空", joined)
        self.assertIn("steam_app_id 必须为正整数", joined)
        self.assertIn("install_dir_name 不能包含路径分隔符", joined)
        self.assertIn("patch_url 必须是 http(s) 链接", joined)
        self.assertIn("expected_sha256 必须是 64 位十六进制字符串", joined)
        self.assertIn("target_subdir 必须是游戏目录内的相对路径", joined)
        self.assertEqual(len(warnings), 1)
        self.assertIn("expected_sha256", warnings[0])

    def test_cli_validate_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            bad = Path(tmp) / "bad.json"
            good.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "games": [
                            {
                                "id": "g",
                                "name": "Game",
                                "patch_url": "https://example.com/p.zip",
                                "expected_sha256": "b" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bad.write_text(
                json.dumps({"version": 1, "games": [{"id": "g", "name": ""}]}),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["catalog", "validate", "--catalog", str(good)]), 0)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli_main(["catalog", "validate", "--catalog", str(bad)]), 3)
            self.assertIn("name 不能为空", err.getvalue())


class TestCatalogEditCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.catalog_path = self.root / "catalog.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, *extra: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli_main(
                [
                    "catalog",
                    "add",
                    "--catalog",
                    str(self.catalog_path),
                    *extra,
                ]
            )

    def test_add_creates_entry_and_upsert_preserves_fields(self) -> None:
        code = self._add(
            "--id",
            "game_one",
            "--name",
            "Game One",
            "--url",
            "https://example.com/p.zip",
            "--sha256",
            "a" * 64,
            "--app-id",
            "123",
        )
        self.assertEqual(code, 0)
        catalog = PatchCatalog.from_file(self.catalog_path)
        self.assertEqual(len(catalog.entries), 1)
        entry = catalog.entries[0]
        self.assertEqual(entry.id, "game_one")
        self.assertEqual(entry.steam_app_id, 123)
        self.assertEqual(entry.source, "user")

        code = self._add("--id", "game_one", "--name", "Game One Renamed")
        self.assertEqual(code, 0)
        catalog = PatchCatalog.from_file(self.catalog_path)
        self.assertEqual(len(catalog.entries), 1)
        entry = catalog.entries[0]
        self.assertEqual(entry.name, "Game One Renamed")
        self.assertEqual(entry.steam_app_id, 123)
        self.assertEqual(entry.expected_sha256, "a" * 64)

    def test_add_rejects_invalid_sha256_without_saving(self) -> None:
        code = self._add(
            "--id",
            "game_one",
            "--name",
            "Game One",
            "--url",
            "https://example.com/p.zip",
            "--sha256",
            "not-a-hash",
        )
        self.assertEqual(code, 3)
        self.assertFalse(self.catalog_path.exists())

    def test_add_warns_when_remote_patch_has_no_sha256(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = cli_main(
                [
                    "catalog",
                    "add",
                    "--catalog",
                    str(self.catalog_path),
                    "--id",
                    "game_one",
                    "--name",
                    "Game One",
                    "--url",
                    "https://example.com/p.zip",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("expected_sha256", err.getvalue())

    def test_add_rejects_remote_catalog_target(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = cli_main(
                [
                    "catalog",
                    "add",
                    "--catalog",
                    "https://example.com/catalog.json",
                    "--id",
                    "g",
                    "--name",
                    "Game",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("只支持本地清单文件", err.getvalue())

    def test_remove_entry(self) -> None:
        self.assertEqual(self._add("--id", "one", "--name", "One"), 0)
        self.assertEqual(self._add("--id", "two", "--name", "Two"), 0)

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(
                [
                    "catalog",
                    "remove",
                    "one",
                    "--catalog",
                    str(self.catalog_path),
                ]
            )
        self.assertEqual(code, 0)
        catalog = PatchCatalog.from_file(self.catalog_path)
        self.assertEqual([entry.id for entry in catalog.entries], ["two"])

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli_main(
                [
                    "catalog",
                    "remove",
                    "missing",
                    "--catalog",
                    str(self.catalog_path),
                ]
            )
        self.assertEqual(code, 1)


class TestGuiInstallState(unittest.TestCase):
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

    def _build_app(self, game_dir: Path) -> tuple[object, mock.Mock, mock.Mock]:
        try:
            from galgame_patcher import gui
        except ImportError:
            self.skipTest("当前 Python 未包含 tkinter，跳过 GUI 测试")
        app = gui.GalgamePatcherGUI.__new__(gui.GalgamePatcherGUI)
        app.patcher = self.patcher
        app._busy = False
        app.game_dir_var = mock.Mock()
        app.game_dir_var.get.return_value = str(game_dir)
        app.detail_state_var = mock.Mock()
        app.btn_recover = mock.Mock()
        return app, app.detail_state_var, app.btn_recover

    def test_refresh_shows_installed_and_disables_recover(self) -> None:
        gui_module = __import__("galgame_patcher.gui", fromlist=["tk"])
        self.assertTrue(
            self.patcher.apply(
                patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
            ).success
        )
        app, state_var, recover_btn = self._build_app(self.game_dir)

        app._refresh_install_state()

        text = state_var.set.call_args[0][0]
        self.assertIn("已安装补丁", text)
        self.assertEqual(recover_btn.configure.call_args.kwargs["state"], gui_module.tk.DISABLED)

    def test_refresh_enables_recover_for_incomplete_install(self) -> None:
        gui_module = __import__("galgame_patcher.gui", fromlist=["tk"])
        self.assertTrue(
            self.patcher.apply(
                patch_source=str(self.patch_dir), game_dir=str(self.game_dir)
            ).success
        )
        manifest_path = self.game_dir / ".galpatch_backup" / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["status"] = "in_progress"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        app, state_var, recover_btn = self._build_app(self.game_dir)
        app._refresh_install_state()
        text = state_var.set.call_args[0][0]
        self.assertIn("上次安装未完成", text)
        self.assertEqual(recover_btn.configure.call_args.kwargs["state"], gui_module.tk.NORMAL)

        app._busy = True
        app._refresh_install_state()
        self.assertEqual(recover_btn.configure.call_args.kwargs["state"], gui_module.tk.DISABLED)

    def test_refresh_handles_missing_directory(self) -> None:
        gui_module = __import__("galgame_patcher.gui", fromlist=["tk"])
        app, state_var, recover_btn = self._build_app(self.root / "missing")
        app._refresh_install_state()
        state_var.set.assert_called_once_with("补丁状态: -")
        self.assertEqual(recover_btn.configure.call_args.kwargs["state"], gui_module.tk.DISABLED)


if __name__ == "__main__":
    unittest.main()
