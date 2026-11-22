"""GalgamePatcher 核心补丁安装、SHA256 校验、备份回滚与 CLI 测试。"""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from galgame_patcher import (
    GalgamePatcher,
    GameProfile,
    SteamLocator,
    apply_patch,
    compute_sha256,
    rollback_patch,
)
from galgame_patcher.cli import main as cli_main


class TestGalgamePatcher(unittest.TestCase):
    """覆盖单文件补丁、压缩包剥离顶层目录、校验、回滚与 CLI 流程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.steam_root = self.root / "Steam"
        self.game_dir = self.steam_root / "steamapps" / "common" / "MyVisualNovel"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "Game.exe").write_bytes(b"ORIGINAL_EXE")
        (self.game_dir / "patch.xp3").write_bytes(b"OLD_XP3_DATA")

        acf_content = '''
"AppState"
{
    "appid"        "223344"
    "installdir"   "MyVisualNovel"
}
'''
        (self.steam_root / "steamapps" / "appmanifest_223344.acf").write_text(
            acf_content, encoding="utf-8"
        )

        self.locator = SteamLocator(steam_root=self.steam_root)
        self.patcher = GalgamePatcher(locator=self.locator)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_single_file_patch_backup_and_rollback(self) -> None:
        patch_file = self.root / "patch.xp3"
        patch_file.write_bytes(b"NEW_UNCENSORED_XP3")

        result = self.patcher.apply(patch_file, game_dir=self.game_dir)
        self.assertTrue(result.success)
        self.assertEqual(result.installed_files, ["patch.xp3"])
        self.assertEqual(result.backed_up_files, ["patch.xp3"])
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"NEW_UNCENSORED_XP3")

        # 确认备份存在，再回滚恢复原始数据
        self.assertIsNotNone(result.backup_dir)
        assert result.backup_dir is not None
        self.assertTrue((result.backup_dir / "manifest.json").is_file())

        rb = self.patcher.rollback(self.game_dir)
        self.assertTrue(rb.success)
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"OLD_XP3_DATA")
        self.assertFalse((self.game_dir / GalgamePatcher.BACKUP_FOLDER_NAME).exists())

    def test_zip_patch_strips_single_top_dir_and_removes_new_files_on_rollback(self) -> None:
        zip_path = self.root / "chs_patch_v1.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("PatchReleaseDir/patch.xp3", b"ZIP_XP3_DATA")
            zf.writestr("PatchReleaseDir/chs/scenario.ks", b"TEXT_KS")

        result = self.patcher.apply(zip_path, game_dir=self.game_dir)
        self.assertTrue(result.success)
        self.assertEqual(sorted(result.installed_files), ["chs/scenario.ks", "patch.xp3"])
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"ZIP_XP3_DATA")
        self.assertEqual((self.game_dir / "chs" / "scenario.ks").read_bytes(), b"TEXT_KS")

        rb = rollback_patch(self.game_dir)
        self.assertTrue(rb.success)
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"OLD_XP3_DATA")
        self.assertFalse((self.game_dir / "chs" / "scenario.ks").exists())
        self.assertFalse((self.game_dir / "chs").exists())

    def test_tar_gz_and_zip_slip_protection(self) -> None:
        tar_path = self.root / "patch.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            data = b"TAR_DATA"
            info = tarfile.TarInfo("data.arc")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        result = self.patcher.apply(tar_path, game_dir=self.game_dir, target_subdir="data")
        self.assertTrue(result.success)
        self.assertEqual((self.game_dir / "data" / "data.arc").read_bytes(), b"TAR_DATA")

        evil_zip = self.root / "evil.zip"
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("../escape.txt", b"BAD")

        with self.assertRaises(ValueError):
            self.patcher.apply(evil_zip, game_dir=self.game_dir)

    def test_sha256_verification(self) -> None:
        patch_file = self.root / "data.pck"
        patch_file.write_bytes(b"PCK_CONTENT_123")
        expected = compute_sha256(patch_file)

        with self.assertRaises(ValueError):
            self.patcher.apply(
                patch_file,
                game_dir=self.game_dir,
                expected_sha256="0" * 64,
            )

        res = self.patcher.apply(
            patch_file,
            game_dir=self.game_dir,
            expected_sha256=expected.upper(),
        )
        self.assertTrue(res.success)
        self.assertEqual((self.game_dir / "data.pck").read_bytes(), b"PCK_CONTENT_123")

    def test_auto_locate_with_profile_and_apply_patch_helper(self) -> None:
        patch_dir = self.root / "patch_folder"
        patch_dir.mkdir()
        (patch_dir / "voice.ypf").write_bytes(b"VOICE_DATA")

        profile = GameProfile(
            name="MyVisualNovel",
            steam_app_id=223344,
            executable_name="Game.exe",
        )
        res = apply_patch(
            patch_dir,
            profile=profile,
            steam_root=self.steam_root,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.game_dir, self.game_dir.resolve())
        self.assertEqual((self.game_dir / "voice.ypf").read_bytes(), b"VOICE_DATA")

    def test_cli_end_to_end(self) -> None:
        patch_file = self.root / "patch.xp3"
        patch_file.write_bytes(b"CLI_XP3_PATCH")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            code_hash = cli_main(["hash", str(patch_file)])
        self.assertEqual(code_hash, 0)
        file_hash = stdout_buf.getvalue().strip()
        self.assertEqual(file_hash, compute_sha256(patch_file))

        locate_buf = io.StringIO()
        with redirect_stdout(locate_buf):
            code_locate = cli_main(
                ["locate", "--app-id", "223344", "--steam-root", str(self.steam_root)]
            )
        self.assertEqual(code_locate, 0)
        self.assertEqual(Path(locate_buf.getvalue().strip()), self.game_dir.resolve())

        with redirect_stdout(io.StringIO()):
            code_apply = cli_main(
                [
                    "apply",
                    str(patch_file),
                    "--app-id",
                    "223344",
                    "--steam-root",
                    str(self.steam_root),
                    "--sha256",
                    file_hash,
                ]
            )
        self.assertEqual(code_apply, 0)
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"CLI_XP3_PATCH")

        with redirect_stdout(io.StringIO()):
            code_rollback = cli_main(["rollback", str(self.game_dir)])
        self.assertEqual(code_rollback, 0)
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"OLD_XP3_DATA")


if __name__ == "__main__":
    unittest.main()
