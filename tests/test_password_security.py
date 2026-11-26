"""问题 2 测试：解压密码默认不明文落盘、catalog export --include-passwords 与 GUI 不回填。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import CatalogEntry, InstalledGameStatus, PatchCatalog
from galgame_patcher.cli import main as cli_main


class TestPasswordPersistenceSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_entry_to_dict_and_catalog_save_default_omit_passwords(self) -> None:
        entry = CatalogEntry(
            id="secret_game",
            name="Secret Game",
            archive_password="top-secret-password",
        )
        default_dict = entry.to_dict()
        self.assertNotIn("archive_password", default_dict)

        explicit_dict = entry.to_dict(include_password=True)
        self.assertEqual(explicit_dict["archive_password"], "top-secret-password")

        catalog = PatchCatalog(entries=[entry])
        target = self.root / "catalog.json"
        catalog.save_to_file(target)
        saved_text = target.read_text(encoding="utf-8")
        self.assertNotIn("top-secret-password", saved_text)
        self.assertNotIn("archive_password", saved_text)

        target_with_pw = self.root / "catalog_with_pw.json"
        catalog.save_to_file(target_with_pw, include_passwords=True)
        self.assertIn("top-secret-password", target_with_pw.read_text(encoding="utf-8"))

    def test_cli_catalog_export_default_vs_include_passwords(self) -> None:
        src = self.root / "source_catalog.json"
        src.write_text(
            json.dumps(
                {
                    "version": 1,
                    "games": [
                        {
                            "id": "g1",
                            "name": "Game One",
                            "archive_password": "plain-password-123",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        # 默认 export 不含密码
        out_safe = self.root / "exported_safe.json"
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = cli_main(["catalog", "export", str(out_safe), "--catalog", str(src)])
        self.assertEqual(code, 0)
        safe_raw = out_safe.read_text(encoding="utf-8")
        self.assertNotIn("plain-password-123", safe_raw)
        self.assertNotIn("archive_password", safe_raw)
        self.assertIn("不含明文解压密码", stdout_buf.getvalue())

        # 显式 --include-passwords 才导出明文密码，并打印安全警告
        out_pw = self.root / "exported_with_pw.json"
        stdout_pw = io.StringIO()
        stderr_pw = io.StringIO()
        with redirect_stdout(stdout_pw), redirect_stderr(stderr_pw):
            code_pw = cli_main(
                [
                    "catalog",
                    "export",
                    str(out_pw),
                    "--catalog",
                    str(src),
                    "--include-passwords",
                ]
            )
        self.assertEqual(code_pw, 0)
        self.assertIn("plain-password-123", out_pw.read_text(encoding="utf-8"))
        self.assertIn("明文解压密码", stderr_pw.getvalue())

    def test_cli_catalog_add_does_not_persist_plaintext_password(self) -> None:
        cat_path = self.root / "local_catalog.json"
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            code = cli_main(
                [
                    "catalog",
                    "add",
                    "--catalog",
                    str(cat_path),
                    "--id",
                    "g2",
                    "--name",
                    "Game Two",
                    "--password",
                    "should-not-write",
                ]
            )
        self.assertEqual(code, 0)
        self.assertNotIn("should-not-write", cat_path.read_text(encoding="utf-8"))
        self.assertIn("不会将解压密码明文落盘", err_buf.getvalue())

    def test_gui_row_selection_does_not_autofill_catalog_password(self) -> None:
        try:
            from galgame_patcher import gui
        except ImportError:
            self.skipTest("当前 Python 未包含 tkinter，跳过 GUI 测试")

        app = gui.GalgamePatcherGUI.__new__(gui.GalgamePatcherGUI)
        app._statuses = [
            InstalledGameStatus(
                entry=CatalogEntry(
                    id="g_pw",
                    name="Encrypted Game",
                    archive_password="leaked-password",
                    expected_sha256="a" * 64,
                    target_subdir="sub",
                ),
                installed=True,
                game_dir=self.root,
                patched=False,
            )
        ]
        app.tree = mock.Mock()
        app.tree.selection.return_value = ("0",)
        app.detail_name_var = mock.Mock()
        app.detail_exe_var = mock.Mock()
        app.detail_desc_var = mock.Mock()
        app.game_dir_var = mock.Mock()
        app.patch_source_var = mock.Mock()
        app.password_var = mock.Mock()
        app.sha256_var = mock.Mock()
        app.target_subdir_var = mock.Mock()
        app._gui_config = {}
        app._inspected_key = ("old", "key")
        app._set_inspect_text = mock.Mock()
        app._append_log = mock.Mock()

        app._on_select_row(None)

        app.password_var.set.assert_called_once_with("")
        app._append_log.assert_called_once()
        self.assertIn("未自动回填", app._append_log.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
