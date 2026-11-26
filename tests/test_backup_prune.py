"""问题 1 测试：备份体积统计、galpatch backup prune 与安装后自动清理。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional

from galgame_patcher import (
    GalgamePatcher,
    PatchResult,
    format_size_bytes,
    parse_size_bytes,
)
from galgame_patcher.cli import main as cli_main


class TestBackupSizeAndPrune(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir()
        (self.game_dir / "data.xp3").write_bytes(b"ORIG_" + b"0" * 1024)
        self.backup_root = self.game_dir / GalgamePatcher.BACKUP_FOLDER_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _apply_version(
        self,
        patcher: GalgamePatcher,
        tag: str,
        payload: bytes,
        extra_files: Optional[dict[str, bytes]] = None,
    ) -> PatchResult:
        patch_dir = self.root / f"patch_{tag}"
        patch_dir.mkdir(exist_ok=True)
        (patch_dir / "data.xp3").write_bytes(payload)
        for name, data in (extra_files or {}).items():
            (patch_dir / name).write_bytes(data)
        res = patcher.apply(patch_dir, game_dir=self.game_dir)
        self.assertTrue(res.success, res.message)
        return res

    def test_parse_and_format_size_bytes(self) -> None:
        self.assertEqual(parse_size_bytes("2G"), 2 * 1024 ** 3)
        self.assertEqual(parse_size_bytes("512M"), 512 * 1024 ** 2)
        self.assertEqual(parse_size_bytes("10K"), 10 * 1024)
        self.assertEqual(parse_size_bytes("1.5KB"), int(1.5 * 1024))
        self.assertEqual(parse_size_bytes("256"), 256)
        self.assertEqual(parse_size_bytes(0), 0)
        with self.assertRaises(ValueError):
            parse_size_bytes("-5M")
        with self.assertRaises(ValueError):
            parse_size_bytes("abc")
        self.assertEqual(format_size_bytes(0), "0 B")
        self.assertEqual(format_size_bytes(2048), "2.00 KB")
        self.assertEqual(format_size_bytes(3 * 1024 ** 2), "3.00 MB")

    def test_status_reports_backup_size_human_and_json(self) -> None:
        patcher = GalgamePatcher(max_backup_history=None, max_backup_size_bytes=None)
        initial_status = patcher.install_status(self.game_dir)
        self.assertEqual(initial_status.backup_size_bytes, 0)

        self._apply_version(patcher, "v1", b"V1_" + b"1" * 2048)
        self._apply_version(patcher, "v2", b"V2_" + b"2" * 2048)

        status = patcher.install_status(self.game_dir)
        self.assertGreater(status.backup_size_bytes, 3000)
        self.assertEqual(status.snapshot_count, 2)

        json_buf = io.StringIO()
        with redirect_stdout(json_buf):
            code = cli_main(["status", str(self.game_dir), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(json_buf.getvalue())
        self.assertEqual(data["backup_size_bytes"], status.backup_size_bytes)
        self.assertIn("KB", data["backup_size_human"])

        human_buf = io.StringIO()
        with redirect_stdout(human_buf):
            code = cli_main(["status", str(self.game_dir)])
        self.assertEqual(code, 0)
        self.assertIn("备份体积:", human_buf.getvalue())

    def test_prune_keeps_recent_n_and_baseline_preserving_rollbacks(self) -> None:
        patcher = GalgamePatcher(max_backup_history=None, max_backup_size_bytes=None)
        for i in range(1, 6):
            self._apply_version(
                patcher,
                f"v{i}",
                f"V{i}_".encode() + bytes([64 + i]) * 2048,
                extra_files={f"extra_{i}.dat": f"EXTRA_{i}".encode()},
            )

        history_before = patcher.rollback_history(self.game_dir)
        self.assertEqual(len(history_before), 5)

        # CLI galpatch backup prune --keep 3 --max-size 2G
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(
                ["backup", "prune", str(self.game_dir), "--keep", "3", "--max-size", "2G"]
            )
        self.assertEqual(code, 0)
        self.assertIn("已清理 2 个历史快照", out.getvalue())

        history_after = patcher.rollback_history(self.game_dir)
        self.assertEqual(len(history_after), 3)
        self.assertEqual(
            [item[0] for item in history_after],
            [item[0] for item in history_before[:3]],
        )
        # 磁盘上只剩 3 个 install-* 快照 + 原始基线 files/
        disk_snaps = list(self.backup_root.glob("install-*"))
        self.assertEqual(len(disk_snaps), 3)
        self.assertTrue((self.backup_root / "files" / "data.xp3").is_file())

        # 逐次撤回最近 3 次（v5 -> v4 -> v3 -> v2）均应成功
        self.assertTrue(patcher.rollback(self.game_dir, mode="keep-patches").success)
        self.assertTrue((self.game_dir / "data.xp3").read_bytes().startswith(b"V4_"))
        self.assertTrue(patcher.rollback(self.game_dir, mode="keep-patches").success)
        self.assertTrue((self.game_dir / "data.xp3").read_bytes().startswith(b"V3_"))
        self.assertTrue(patcher.rollback(self.game_dir, mode="keep-patches").success)
        self.assertTrue((self.game_dir / "data.xp3").read_bytes().startswith(b"V2_"))

        # 3 次都撤回后，不再有逐次记录，但仍可完整回滚至首次安装前原始基线
        status = patcher.install_status(self.game_dir)
        self.assertEqual(status.state, "installed_no_history")
        self.assertEqual(patcher.rollback_history(self.game_dir), [])
        full_rb = patcher.rollback(self.game_dir, mode="full")
        self.assertTrue(full_rb.success, full_rb.message)
        self.assertEqual((self.game_dir / "data.xp3").read_bytes(), b"ORIG_" + b"0" * 1024)
        self.assertFalse((self.game_dir / "extra_1.dat").exists())
        self.assertFalse((self.game_dir / "extra_2.dat").exists())

    def test_prune_respects_max_size_and_never_deletes_baseline(self) -> None:
        patcher = GalgamePatcher(max_backup_history=None, max_backup_size_bytes=None)
        for i in range(1, 5):
            self._apply_version(patcher, f"s{i}", b"S" * 4096)

        # 上限设得很小（100 B），即使原始基线超过 100 B 也绝不能删除原始基线
        result = patcher.prune_backups(self.game_dir, keep=10, max_size_bytes=100)
        self.assertEqual(len(result.removed_snapshots), 4)
        self.assertEqual(result.kept_snapshots, [])
        self.assertTrue(result.baseline_preserved)
        self.assertIn("保留原始基线", result.message)
        self.assertEqual(list(self.backup_root.glob("install-*")), [])
        self.assertTrue((self.backup_root / "files" / "data.xp3").is_file())

        # 完整回滚依然恢复到原始基线
        rb = patcher.rollback(self.game_dir, mode="full")
        self.assertTrue(rb.success, rb.message)
        self.assertEqual((self.game_dir / "data.xp3").read_bytes(), b"ORIG_" + b"0" * 1024)

    def test_prune_dry_run_and_orphan_cleanup_and_in_progress_guard(self) -> None:
        patcher = GalgamePatcher(max_backup_history=None, max_backup_size_bytes=None)
        self._apply_version(patcher, "a1", b"A1" * 1024)
        self._apply_version(patcher, "a2", b"A2" * 1024)

        orphan = self.backup_root / "install-orphan123"
        orphan.mkdir()
        (orphan / "junk.bin").write_bytes(b"JUNK" * 512)

        dry = patcher.prune_backups(self.game_dir, keep=1, dry_run=True)
        self.assertTrue(dry.dry_run)
        self.assertIn("install-orphan123", dry.removed_snapshots)
        self.assertEqual(len(dry.removed_snapshots), 2)
        self.assertTrue(orphan.is_dir())

        real = patcher.prune_backups(self.game_dir, keep=1, dry_run=False)
        self.assertFalse(orphan.exists())
        self.assertEqual(len(real.kept_snapshots), 1)

        # 未完成安装状态拒绝清理
        manifest_path = self.backup_root / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["status"] = "in_progress"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未完成"):
            patcher.prune_backups(self.game_dir, keep=1)

    def test_apply_auto_prunes_excess_snapshots_and_notifies(self) -> None:
        patcher = GalgamePatcher(max_backup_history=3, max_backup_size_bytes=2 * 1024 ** 3)
        for i in range(1, 4):
            res = self._apply_version(patcher, f"auto{i}", f"AUTO{i}_".encode() * 200)
            self.assertEqual(res.pruned_snapshots, [])  # type: ignore[attr-defined]

        # 第 4 次安装超过 keep=3，应自动清理 1 个最旧快照并在消息里提示
        res4 = self._apply_version(patcher, "auto4", b"AUTO4_" * 200)
        self.assertEqual(len(res4.pruned_snapshots), 1)  # type: ignore[attr-defined]
        self.assertGreater(res4.pruned_bytes, 0)  # type: ignore[attr-defined]
        self.assertIn("自动清理了 1 个超出上限的旧快照", res4.message)  # type: ignore[attr-defined]
        self.assertEqual(len(patcher.rollback_history(self.game_dir)), 3)
        # 完整回滚依然能还原原始基线
        self.assertTrue(patcher.rollback(self.game_dir, mode="full").success)
        self.assertEqual((self.game_dir / "data.xp3").read_bytes(), b"ORIG_" + b"0" * 1024)

    def test_cli_backup_prune_json_and_errors(self) -> None:
        patcher = GalgamePatcher(max_backup_history=None, max_backup_size_bytes=None)
        for i in range(1, 5):
            self._apply_version(patcher, f"cli{i}", b"C" * 1024)

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(
                ["backup", "prune", str(self.game_dir), "--keep", "2", "--json"]
            )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(len(payload["removed_snapshots"]), 2)
        self.assertEqual(len(payload["kept_snapshots"]), 2)
        self.assertTrue(payload["baseline_preserved"])

        err = io.StringIO()
        with redirect_stderr(err):
            bad_code = cli_main(
                ["backup", "prune", str(self.game_dir), "--max-size", "invalid"]
            )
        self.assertEqual(bad_code, 1)
        self.assertIn("无效的体积格式", err.getvalue())


if __name__ == "__main__":
    unittest.main()
