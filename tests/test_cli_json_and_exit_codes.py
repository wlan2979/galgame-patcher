"""问题 6 测试：全 CLI --json 输出、统一 _emit() 与退出码规范（0/1/2/3/4/5）。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import ConcurrencyError, GameRunningError, compute_sha256
from galgame_patcher.cli import (
    EXIT_CONCURRENCY_CONFLICT,
    EXIT_ERROR,
    EXIT_GAME_RUNNING,
    EXIT_OK,
    EXIT_UNTRUSTED_SOURCE,
    EXIT_VERIFICATION_FAILED,
)
from galgame_patcher.cli import main as cli_main


class TestCliJsonAndExitCodes(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_appdata = os.environ.get("GALGAME_PATCHER_APPDATA")
        os.environ["GALGAME_PATCHER_APPDATA"] = str(self.root / "appdata")

        self.steam_root = self.root / "Steam"
        self.game_dir = self.steam_root / "steamapps" / "common" / "JsonVN"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "JsonVN.exe").write_bytes(b"ORIG_EXE")
        (self.game_dir / "data.xp3").write_bytes(b"ORIG_DATA")
        (self.steam_root / "steamapps" / "appmanifest_12345.acf").write_text(
            '"AppState"\n{\n    "appid" "12345"\n    "installdir" "JsonVN"\n}\n',
            encoding="utf-8",
        )

        self.patch_file = self.root / "data.xp3"
        self.patch_file.write_bytes(b"PATCHED_DATA")

        self.catalog_path = self.root / "catalog.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "games": [
                        {
                            "id": "json_vn",
                            "name": "Json VN",
                            "steam_app_id": 12345,
                            "install_dir_name": "JsonVN",
                            "archive_password": "never-leak-in-list",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self._old_appdata is None:
            os.environ.pop("GALGAME_PATCHER_APPDATA", None)
        else:
            os.environ["GALGAME_PATCHER_APPDATA"] = self._old_appdata
        self._tmp.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_all_core_commands_support_json_output(self) -> None:
        # locate --json
        code, out, _ = self._run(
            ["locate", "--app-id", "12345", "--steam-root", str(self.steam_root), "--json"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(Path(json.loads(out)["game_dir"]), self.game_dir.resolve())

        # scan --json
        code, out, _ = self._run(
            [
                "scan",
                "--catalog",
                str(self.catalog_path),
                "--steam-root",
                str(self.steam_root),
                "--only-installed",
                "--json",
            ]
        )
        self.assertEqual(code, EXIT_OK)
        scan_data = json.loads(out)
        self.assertEqual(len(scan_data), 1)
        self.assertEqual(scan_data[0]["id"], "json_vn")
        self.assertTrue(scan_data[0]["installed"])

        # catalog list --json（且不泄露明文密码）
        code, out, _ = self._run(
            ["catalog", "list", "--catalog", str(self.catalog_path), "--json"]
        )
        self.assertEqual(code, EXIT_OK)
        list_data = json.loads(out)
        self.assertEqual(list_data["count"], 1)
        self.assertNotIn("archive_password", list_data["games"][0])
        self.assertNotIn("never-leak-in-list", out)

        # hash --json & verify-file --json
        code, out, _ = self._run(["hash", str(self.patch_file), "--json"])
        self.assertEqual(code, EXIT_OK)
        digest = compute_sha256(self.patch_file)
        self.assertEqual(json.loads(out)["sha256"], digest)

        code, out, _ = self._run(
            ["verify-file", str(self.patch_file), "--sha256", digest, "--json"]
        )
        self.assertEqual(code, EXIT_OK)
        vf_data = json.loads(out)
        self.assertEqual(vf_data["sha256"], digest)
        self.assertTrue(vf_data["sha256_matched"])

        # apply --json
        code, out, _ = self._run(
            [
                "apply",
                str(self.patch_file),
                "--game-dir",
                str(self.game_dir),
                "--sha256",
                digest,
                "--json",
            ]
        )
        self.assertEqual(code, EXIT_OK)
        apply_data = json.loads(out)
        self.assertTrue(apply_data["success"])
        self.assertTrue(apply_data["verified"])
        self.assertEqual(apply_data["installed_files"], ["data.xp3"])

        # rollback --history --json & rollback --json
        code, out, _ = self._run(["rollback", str(self.game_dir), "--history", "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(json.loads(out)["history"]), 1)

        code, out, _ = self._run(["rollback", str(self.game_dir), "--json"])
        self.assertEqual(code, EXIT_OK)
        rb_data = json.loads(out)
        self.assertTrue(rb_data["success"])
        self.assertEqual((self.game_dir / "data.xp3").read_bytes(), b"ORIG_DATA")

    def test_exit_codes_for_each_failure_category(self) -> None:
        # 1: 通用失败（不存在目录）
        code, out, _err = self._run(["status", str(self.root / "missing"), "--json"])
        self.assertEqual(code, EXIT_ERROR)
        err_payload = json.loads(out)
        self.assertEqual(err_payload["exit_code"], EXIT_ERROR)
        self.assertFalse(err_payload["ok"])

        # 2: 游戏运行中（GameRunningError），即便 stderr 有未提供 SHA256 警告，stdout 仍是纯净 JSON
        with mock.patch(
            "galgame_patcher.patcher.GalgamePatcher.apply",
            side_effect=GameRunningError(["data.xp3"]),
        ):
            code, out, _err = self._run(
                ["apply", str(self.patch_file), "--game-dir", str(self.game_dir), "--json"]
            )
        self.assertEqual(code, EXIT_GAME_RUNNING)
        self.assertEqual(json.loads(out)["exit_code"], EXIT_GAME_RUNNING)

        # 3: 校验失败（SHA256 不匹配 & 写后校验失败）
        code, out, _err = self._run(
            [
                "apply",
                str(self.patch_file),
                "--game-dir",
                str(self.game_dir),
                "--sha256",
                "0" * 64,
                "--json",
            ]
        )
        self.assertEqual(code, EXIT_VERIFICATION_FAILED)
        self.assertEqual(json.loads(out)["exit_code"], EXIT_VERIFICATION_FAILED)

        with mock.patch("galgame_patcher.patcher.compute_sha256", return_value="0" * 64):
            code, out, _err = self._run(
                [
                    "apply",
                    str(self.patch_file),
                    "--game-dir",
                    str(self.game_dir),
                    "--json",
                ]
            )
        self.assertEqual(code, EXIT_VERIFICATION_FAILED)
        post_data = json.loads(out)
        self.assertFalse(post_data["success"])
        self.assertTrue(post_data["verification_failed"])

        # 4: 并发锁冲突（ConcurrencyError）
        with mock.patch(
            "galgame_patcher.patcher.GalgamePatcher.apply",
            side_effect=ConcurrencyError(pid=4321),
        ):
            code, out, _err = self._run(
                ["apply", str(self.patch_file), "--game-dir", str(self.game_dir), "--json"]
            )
        self.assertEqual(code, EXIT_CONCURRENCY_CONFLICT)
        self.assertEqual(json.loads(out)["exit_code"], EXIT_CONCURRENCY_CONFLICT)

        # 5: 未信任来源拦截（UntrustedSourceError）
        code, out, _err = self._run(
            ["apply", "https://example.com/untrusted.zip", "--game-dir", str(self.game_dir), "--json"]
        )
        self.assertEqual(code, EXIT_UNTRUSTED_SOURCE)
        self.assertEqual(json.loads(out)["exit_code"], EXIT_UNTRUSTED_SOURCE)


if __name__ == "__main__":
    unittest.main()
