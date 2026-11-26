"""问题 7 测试：补丁中大小写同名文件静默覆盖检测（Windows 报错、非 Windows 警告）。"""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from galgame_patcher import GalgamePatcher
from galgame_patcher.archive import check_case_conflicts, find_case_conflicts


class TestCaseConflictDetection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir()
        (self.game_dir / "orig.xp3").write_bytes(b"ORIG")
        self.patcher = GalgamePatcher()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_find_and_check_case_conflicts_by_platform(self) -> None:
        conflicts = find_case_conflicts(
            ["data.bin", "Data.bin", "sub/a.xp3", "other/A.xp3", "dir/Voice.xp3", "dir/voice.xp3"]
        )
        self.assertEqual(len(conflicts), 2)
        self.assertIn(["data.bin", "Data.bin"], conflicts)
        self.assertIn(["dir/Voice.xp3", "dir/voice.xp3"], conflicts)

        # Windows 抛出 ValueError 并列出冲突路径
        with self.assertRaises(ValueError) as ctx:
            check_case_conflicts(["data.bin", "Data.bin"], platform="win32")
        self.assertIn("data.bin", str(ctx.exception))
        self.assertIn("Data.bin", str(ctx.exception))

        # 非 Windows 只打警告，不抛异常
        with self.assertLogs("galgame_patcher", level="WARNING") as logs:
            detected = check_case_conflicts(["data.bin", "Data.bin"], platform="linux")
        self.assertEqual(detected, [["data.bin", "Data.bin"]])
        joined_logs = "\n".join(logs.output)
        self.assertIn("data.bin", joined_logs)
        self.assertIn("Data.bin", joined_logs)

        # 不同目录或不同文件名不误报
        self.assertEqual(
            find_case_conflicts(["Data/a.bin", "data/b.bin", "sub1/x.xp3", "sub2/X.xp3"]),
            [],
        )

    def test_zip_with_case_conflicting_entries_blocked_on_windows(self) -> None:
        conflict_zip = self.root / "conflict.zip"
        with zipfile.ZipFile(conflict_zip, "w") as zf:
            zf.writestr("data.bin", b"LOWER")
            zf.writestr("Data.bin", b"UPPER")

        with mock.patch("galgame_patcher.archive.sys.platform", "win32"), mock.patch(
            "galgame_patcher.patcher.sys.platform", "win32"
        ):
            with self.assertRaises(ValueError) as ctx:
                self.patcher.apply(conflict_zip, game_dir=self.game_dir)

        msg = str(ctx.exception)
        self.assertIn("data.bin", msg)
        self.assertIn("Data.bin", msg)
        self.assertFalse((self.game_dir / "data.bin").exists())
        self.assertFalse((self.game_dir / "Data.bin").exists())

    def test_tar_with_case_conflicting_entries_blocked_on_windows(self) -> None:
        conflict_tar = self.root / "conflict.tar"
        with tarfile.open(conflict_tar, "w") as tf:
            for name, payload in (("sub/data.bin", b"LOWER"), ("sub/Data.bin", b"UPPER")):
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))

        with mock.patch("galgame_patcher.archive.sys.platform", "win32"), mock.patch(
            "galgame_patcher.patcher.sys.platform", "win32"
        ):
            with self.assertRaises(ValueError) as ctx:
                self.patcher.apply(conflict_tar, game_dir=self.game_dir)
        self.assertIn("sub/data.bin", str(ctx.exception))
        self.assertIn("sub/Data.bin", str(ctx.exception))

    def test_collect_patch_files_detects_case_conflict_on_windows_and_warns_on_posix(self) -> None:
        patch_dir = self.root / "patch_dir"
        patch_dir.mkdir()
        src_a = self.root / "src_a.bin"
        src_b = self.root / "src_b.bin"
        src_a.write_bytes(b"A")
        src_b.write_bytes(b"B")

        fake_pairs = [
            (src_a, self.game_dir / "data.bin"),
            (src_b, self.game_dir / "Data.bin"),
        ]

        with mock.patch.object(
            self.patcher, "_walk_source_dir", return_value=(fake_pairs, [])
        ):
            # Windows 报错
            with mock.patch("galgame_patcher.patcher.sys.platform", "win32"):
                with self.assertRaises(ValueError) as ctx:
                    self.patcher._collect_patch_files(
                        source_path=patch_dir,
                        dest_root=self.game_dir,
                        temp_dir=self.root / "tmp",
                        strip_single_top_dir=True,
                        password=None,
                        password_candidates=None,
                    )
            self.assertIn("data.bin", str(ctx.exception))
            self.assertIn("Data.bin", str(ctx.exception))

            # 非 Windows（如 Linux）仅记录警告并返回原列表
            with mock.patch("galgame_patcher.patcher.sys.platform", "linux"):
                with self.assertLogs("galgame_patcher", level="WARNING") as logs:
                    pairs, skipped = self.patcher._collect_patch_files(
                        source_path=patch_dir,
                        dest_root=self.game_dir,
                        temp_dir=self.root / "tmp",
                        strip_single_top_dir=True,
                        password=None,
                        password_candidates=None,
                    )
            self.assertEqual(len(pairs), 2)
            self.assertEqual(skipped, [])
            self.assertIn("data.bin", "\n".join(logs.output))
            self.assertIn("Data.bin", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
