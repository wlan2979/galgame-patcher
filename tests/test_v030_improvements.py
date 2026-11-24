"""v0.3.0 全面改进测试：覆盖 P0-1~P0-3、P1-1~P1-3、P2-1~P2-2、P3-1~P3-3 与网页补丁自动爬取。"""

from __future__ import annotations

import functools
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import (
    ConcurrencyError,
    GalgamePatcher,
    GameRunningError,
    ManualLocator,
    PatchCatalog,
    SteamLocator,
    deep_scan_games,
    discover_patches_online,
    extract_archive,
    locate_game,
)
from galgame_patcher.cli import main as cli_main


class _SilentHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class TestV030Improvements(unittest.TestCase):
    """逐项验证改进提示词中的 P0 / P1 / P2 / P3 需求及在线补丁爬取。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.appdata_dir = self.root / "appdata"
        self.appdata_dir.mkdir(parents=True)
        self._old_appdata_env = os.environ.get("GALGAME_PATCHER_APPDATA")
        os.environ["GALGAME_PATCHER_APPDATA"] = str(self.appdata_dir)

        self.game_dir = self.root / "GameDir"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "Game.exe").write_bytes(b"ORIG_EXE")
        (self.game_dir / "a.xp3").write_bytes(b"ORIG_A")
        (self.game_dir / "b.xp3").write_bytes(b"ORIG_B")
        (self.game_dir / "c.xp3").write_bytes(b"ORIG_C")

        self.patcher = GalgamePatcher()

    def tearDown(self) -> None:
        if self._old_appdata_env is None:
            os.environ.pop("GALGAME_PATCHER_APPDATA", None)
        else:
            os.environ["GALGAME_PATCHER_APPDATA"] = self._old_appdata_env
        self._tmp.cleanup()

    def test_p0_1_transactional_install_auto_rollback_on_midway_failure(self) -> None:
        """P0-1: 第 3 个文件写入抛 PermissionError 时自动回滚已覆盖/已新增文件，manifest 记为 rolled_back。"""
        patch_dir = self.root / "patch_p0_1"
        patch_dir.mkdir()
        (patch_dir / "01_new.xp3").write_bytes(b"NEW_01")
        (patch_dir / "a.xp3").write_bytes(b"PATCH_A")
        (patch_dir / "b.xp3").write_bytes(b"PATCH_B")
        (patch_dir / "c.xp3").write_bytes(b"PATCH_C")

        real_copy2 = shutil.copy2
        dest_copy_count = 0

        def _flaky_copy2(src: object, dst: object, *args: object, **kwargs: object) -> object:
            nonlocal dest_copy_count
            dst_path = Path(str(dst)).resolve()
            # 仅统计向 game_dir 正式目标位置（非 .galpatch_backup）的写入次数
            if GalgamePatcher.BACKUP_FOLDER_NAME not in dst_path.parts:
                dest_copy_count += 1
                if dest_copy_count == 3:
                    raise PermissionError("模拟第 3 个目标文件被锁导致写入失败")
            return real_copy2(src, dst, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch("galgame_patcher.patcher.shutil.copy2", side_effect=_flaky_copy2):
            with self.assertRaises(PermissionError) as ctx:
                self.patcher.apply(patch_dir, game_dir=self.game_dir)

        # ① 异常信息包含自动回滚提示
        self.assertIn("已自动回滚，游戏目录未受影响", str(ctx.exception))
        # ② 所有原有文件恢复为原始内容
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertEqual((self.game_dir / "b.xp3").read_bytes(), b"ORIG_B")
        self.assertEqual((self.game_dir / "c.xp3").read_bytes(), b"ORIG_C")
        # ③ 本轮新增文件已被清理
        self.assertFalse((self.game_dir / "01_new.xp3").exists())
        # ④ manifest 状态为 rolled_back
        manifest_path = self.game_dir / GalgamePatcher.BACKUP_FOLDER_NAME / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("status"), "rolled_back")

        # ⑤ 模拟上次进程崩溃留下 status="in_progress" 的残留 manifest，rollback() 应识别并还原
        backup_files_dir = self.game_dir / GalgamePatcher.BACKUP_FOLDER_NAME / "files"
        backup_files_dir.mkdir(parents=True, exist_ok=True)
        (backup_files_dir / "a.xp3").write_bytes(b"ORIG_A")
        (self.game_dir / "a.xp3").write_bytes(b"CORRUPTED_BY_CRASH")
        (self.game_dir / "crash_new.xp3").write_bytes(b"LEFTOVER_NEW")
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "in_progress",
                    "backed_up_files": ["a.xp3"],
                    "newly_added_files": ["crash_new.xp3"],
                }
            ),
            encoding="utf-8",
        )
        rb_res = self.patcher.rollback(self.game_dir)
        self.assertTrue(rb_res.success)
        self.assertIn("检测到上次打补丁未完成，已按残留记录还原", rb_res.message)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertFalse((self.game_dir / "crash_new.xp3").exists())

    def test_failed_second_patch_preserves_previous_install_and_backup(self) -> None:
        first = self.root / "first"
        first.mkdir()
        (first / "a.xp3").write_bytes(b"FIRST_A")
        (first / "addon.xp3").write_bytes(b"FIRST_ADDON")
        self.assertTrue(self.patcher.apply(first, game_dir=self.game_dir).success)
        manifest = self.game_dir / ".galpatch_backup" / "manifest.json"
        previous_manifest = manifest.read_bytes()

        second = self.root / "second"
        second.mkdir()
        (second / "a.xp3").write_bytes(b"SECOND_A")
        (second / "addon.xp3").write_bytes(b"SECOND_ADDON")
        (second / "b.xp3").write_bytes(b"SECOND_B")
        real_copy2 = shutil.copy2

        def fail_last_write(src: Path, dst: Path) -> object:
            if src == second / "b.xp3":
                raise OSError("模拟第二次安装写入失败")
            return real_copy2(src, dst)

        with mock.patch("galgame_patcher.patcher.shutil.copy2", side_effect=fail_last_write):
            with self.assertRaisesRegex(OSError, "已自动回滚"):
                self.patcher.apply(second, game_dir=self.game_dir)

        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"FIRST_A")
        self.assertEqual((self.game_dir / "addon.xp3").read_bytes(), b"FIRST_ADDON")
        self.assertEqual((self.game_dir / "b.xp3").read_bytes(), b"ORIG_B")
        self.assertEqual(manifest.read_bytes(), previous_manifest)
        self.assertTrue(self.patcher.rollback(self.game_dir).success)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertFalse((self.game_dir / "addon.xp3").exists())

    def test_failed_automatic_restore_keeps_recovery_manifest(self) -> None:
        source = self.root / "a.xp3"
        source.write_bytes(b"NEW_A")
        target = (self.game_dir / "a.xp3").resolve()
        real_copy2 = shutil.copy2

        def fail_writes_to_game(src: Path, dst: Path) -> object:
            if Path(dst).resolve() == target:
                raise PermissionError("模拟安装和自动还原时目标不可写")
            return real_copy2(src, dst)

        with mock.patch("galgame_patcher.patcher.shutil.copy2", side_effect=fail_writes_to_game):
            with self.assertRaisesRegex(PermissionError, "自动回滚未完成，备份已保留"):
                self.patcher.apply(source, game_dir=self.game_dir)

        manifest = self.game_dir / ".galpatch_backup" / "manifest.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "in_progress")
        with self.assertRaisesRegex(ValueError, "请先回滚"):
            self.patcher.apply(source, game_dir=self.game_dir)
        self.assertTrue(self.patcher.rollback(self.game_dir).success)

    def test_install_without_backup_does_not_discard_previous_backup(self) -> None:
        first = self.root / "a.xp3"
        first.write_bytes(b"FIRST_A")
        second = self.root / "b.xp3"
        second.write_bytes(b"SECOND_B")
        self.assertTrue(self.patcher.apply(first, game_dir=self.game_dir).success)
        self.assertTrue(self.patcher.apply(second, game_dir=self.game_dir, backup=False).success)

        self.assertTrue(self.patcher.rollback(self.game_dir).success)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertEqual((self.game_dir / "b.xp3").read_bytes(), b"SECOND_B")

    def test_p0_2_locked_file_precheck_and_cli_exit_code_2(self) -> None:
        """P0-2: 目标文件被占用时抛 GameRunningError，游戏目录零改动，CLI 返回退出码 2。"""
        patch_file = self.root / "a.xp3"
        patch_file.write_bytes(b"NEW_A")

        with mock.patch("galgame_patcher.patcher._is_file_locked", return_value=True):
            with self.assertRaises(GameRunningError) as ctx:
                self.patcher.apply(patch_file, game_dir=self.game_dir)
            self.assertIn("无法以可写方式打开以下文件", str(ctx.exception))
            self.assertIn("a.xp3", str(ctx.exception))
            self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")

            # CLI 捕获 GameRunningError 后打印到 stderr 并返回退出码 2
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                code = cli_main(
                    ["apply", str(patch_file), "--game-dir", str(self.game_dir)]
                )
            self.assertEqual(code, 2)
            self.assertIn("检查文件权限", stderr_buf.getvalue())

            # 使用 --force-locked 可跳过占用检查
            with redirect_stdout(io.StringIO()):
                code_forced = cli_main(
                    [
                        "apply",
                        str(patch_file),
                        "--game-dir",
                        str(self.game_dir),
                        "--force-locked",
                    ]
                )
            self.assertEqual(code_forced, 0)
            self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"NEW_A")

    def test_p0_3_rollback_keeps_backup_on_partial_failure(self) -> None:
        """P0-3: rollback 还原第 2 个文件抛 OSError 时保留 .galpatch_backup 且返回 failed_files。"""
        patch_dir = self.root / "patch_p0_3"
        patch_dir.mkdir()
        (patch_dir / "a.xp3").write_bytes(b"PATCHED_A")
        (patch_dir / "b.xp3").write_bytes(b"PATCHED_B")

        apply_res = self.patcher.apply(patch_dir, game_dir=self.game_dir)
        self.assertTrue(apply_res.success)

        real_copy2 = shutil.copy2
        call_idx = 0

        def _fail_second_restore(src: object, dst: object, *args: object, **kwargs: object) -> object:
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise OSError("模拟还原第 2 个文件时磁盘空间不足")
            return real_copy2(src, dst, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch("galgame_patcher.patcher.shutil.copy2", side_effect=_fail_second_restore):
            rb_res = self.patcher.rollback(self.game_dir)

        self.assertFalse(rb_res.success)
        self.assertTrue((self.game_dir / GalgamePatcher.BACKUP_FOLDER_NAME).is_dir())
        self.assertIn("b.xp3", rb_res.failed_files)
        self.assertIn("磁盘空间不足", rb_res.failed_reasons.get("b.xp3", ""))
        self.assertIn("备份已保留，修复问题后可重试回滚", rb_res.message)

    def test_p1_1_archive_distinguishes_io_error_from_password_error(self) -> None:
        """P1-1: py7zr 抛 PermissionError 时立刻中止，不继续尝试后续密码，且错误信息不含“密码”。"""
        dummy_7z = self.root / "dummy.7z"
        dummy_7z.write_bytes(b"7z_placeholder")
        out_dir = self.root / "out_7z"

        call_count = 0

        class _FakeSevenZipFile:
            def __init__(self, *args: object, **kwargs: object) -> None:
                nonlocal call_count
                call_count += 1
                raise PermissionError("磁盘写入权限被拒绝")

            def __enter__(self) -> _FakeSevenZipFile:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        fake_py7zr = types.ModuleType("py7zr")
        fake_py7zr.SevenZipFile = _FakeSevenZipFile  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, {"py7zr": fake_py7zr}):
            with self.assertRaises(ValueError) as ctx:
                extract_archive(
                    dummy_7z,
                    out_dir,
                    password="pw1",
                    password_candidates=["pw2", "pw3"],
                )

        self.assertEqual(call_count, 1)
        self.assertNotIn("密码", str(ctx.exception))
        self.assertIn("PermissionError", str(ctx.exception))

    def test_p1_2_path_traversal_blocked_before_write_for_zip_and_7z(self) -> None:
        """P1-2: 包含 ../../evil.txt 的 zip 与 7z 均在写盘前抛 ValueError，外部目录不生成 evil.txt。"""
        extract_target = self.root / "sub" / "extract_here"
        extract_target.mkdir(parents=True)
        evil_outside = self.root / "evil.txt"

        evil_zip = self.root / "evil.zip"
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("../../evil.txt", b"EVIL_ZIP")

        with self.assertRaises(ValueError):
            extract_archive(evil_zip, extract_target)
        self.assertFalse(evil_outside.exists())

        # 构造带 "../../evil.txt" 成员名的模拟 7z
        evil_7z = self.root / "evil.7z"
        evil_7z.write_bytes(b"7z")

        class _EvilSevenZip:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> _EvilSevenZip:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def getnames(self) -> list[str]:
                return ["../../evil.txt"]

            def extract(self, *args: object, **kwargs: object) -> None:
                evil_outside.write_bytes(b"SHOULD_NEVER_BE_CALLED")

        fake_py7zr = types.ModuleType("py7zr")
        fake_py7zr.SevenZipFile = _EvilSevenZip  # type: ignore[attr-defined]

        with mock.patch.dict(sys.modules, {"py7zr": fake_py7zr}):
            with self.assertRaises(ValueError):
                extract_archive(evil_7z, extract_target)
        self.assertFalse(evil_outside.exists())

    def test_p1_3_external_extractor_returncode_and_stderr_summary(self) -> None:
        """P1-3: 外部 7z 返回 returncode=2 且 stderr 含 Wrong password 时异常包含该摘要，无密码时省略 -p。"""
        dummy_7z = self.root / "ext.7z"
        dummy_7z.write_bytes(b"7z")
        out_dir = self.root / "out_ext"
        recorded_cmds: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded_cmds.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=2,
                stdout="",
                stderr="ERROR: Wrong password : secret.xp3",
            )

        with mock.patch.dict(sys.modules, {"py7zr": None}), mock.patch(
            "galgame_patcher.archive._find_7z_executable", return_value="7z"
        ), mock.patch(
            "galgame_patcher.archive._find_bsdtar_executable", return_value=None
        ), mock.patch(
            "galgame_patcher.archive.subprocess.run", side_effect=_fake_run
        ):
            with self.assertRaises(ValueError) as ctx:
                extract_archive(dummy_7z, out_dir)

        self.assertIn("Wrong password", str(ctx.exception))
        self.assertEqual(len(recorded_cmds), 1)
        # 无密码时不应出现 "-p" 参数
        self.assertFalse(any(arg.startswith("-p") for arg in recorded_cmds[0]))

    def test_p2_1_manual_locator_and_deep_scan_non_steam(self) -> None:
        """P2-1: ManualLocator 手工登记与非 Steam 深度扫描均能定位含 Game.exe + data.xp3 的目录。"""
        non_steam_root = self.root / "NonSteamGames"
        fake_vn = non_steam_root / "DlsiteGalgame"
        fake_vn.mkdir(parents=True)
        (fake_vn / "SmallLauncher.exe").write_bytes(b"12")
        (fake_vn / "MainEngine.exe").write_bytes(b"1234567890")
        (fake_vn / "data.xp3").write_bytes(b"XP3_ARCHIVE")

        # 1) 测试深度扫描发现非 Steam 游戏
        candidates = deep_scan_games(roots=[non_steam_root])
        self.assertEqual(candidates, [fake_vn.resolve()])

        found_deep = locate_game(
            game_name="DlsiteGalgame",
            deep_scan=True,
            deep_scan_roots=[non_steam_root],
        )
        self.assertEqual(found_deep, fake_vn.resolve())

        # 2) 测试 CLI add-game 与 ManualLocator 自动推断目录名与最大 .exe
        with redirect_stdout(io.StringIO()):
            code_add = cli_main(["add-game", str(fake_vn)])
        self.assertEqual(code_add, 0)

        manual = ManualLocator()
        games = manual.list_games()
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["name"], "DlsiteGalgame")
        self.assertEqual(games[0]["executable"], "MainEngine.exe")

        found_manual = locate_game(
            game_name="DlsiteGalgame",
            executable_name="MainEngine.exe",
        )
        self.assertEqual(found_manual, fake_vn.resolve())

    def test_p2_2_catalog_remote_update_merge_bak_and_verify(self) -> None:
        """P2-2: update_from_remote 增量合并新版本清单并生成 .bak，verify_links 探活更新 last_verified。"""
        web_dir = self.root / "web_p2_2"
        web_dir.mkdir()
        (web_dir / "valid_patch.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        local_cat_path = self.appdata_dir / "galgame-patcher" / "catalog.json"
        local_v1 = {
            "version": 1,
            "games": [
                {
                    "id": "local_custom",
                    "name": "本地独有条目",
                    "patch_version": "1.0",
                    "patch_url": "",
                },
                {
                    "id": "shared_game",
                    "name": "共享游戏旧版",
                    "patch_version": "1.0",
                    "patch_url": "",
                },
            ],
        }
        local_cat_path.parent.mkdir(parents=True, exist_ok=True)
        local_cat_path.write_text(
            json.dumps(local_v1, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        original_raw = local_cat_path.read_text(encoding="utf-8")

        handler = functools.partial(_SilentHTTPHandler, directory=str(web_dir))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            base_url = f"http://127.0.0.1:{port}"
            remote_v2 = {
                "version": 2,
                "games": [
                    {
                        "id": "shared_game",
                        "name": "共享游戏新版",
                        "patch_version": "2.0",
                        "patch_url": f"{base_url}/valid_patch.zip",
                    },
                    {
                        "id": "remote_new",
                        "name": "远程新增游戏",
                        "patch_version": "1.1",
                        "patch_url": f"{base_url}/not_found_404.zip",
                    },
                ],
            }
            (web_dir / "remote_catalog.json").write_text(
                json.dumps(remote_v2, ensure_ascii=False), encoding="utf-8"
            )

            catalog = PatchCatalog.from_file(local_cat_path)
            updated = catalog.update_from_remote(
                f"{base_url}/remote_catalog.json",
                target_path=local_cat_path,
            )
            self.assertTrue(updated)
            self.assertEqual(catalog.version, 2)

            ids = [e.id for e in catalog.entries]
            self.assertEqual(ids, ["local_custom", "shared_game", "remote_new"])
            shared_entry = catalog.find_entry("shared_game")
            assert shared_entry is not None
            self.assertEqual(shared_entry.patch_version, "2.0")
            self.assertEqual(shared_entry.source, f"{base_url}/remote_catalog.json")

            # 校验 .bak 内容为合并前的 v1 内容
            bak_path = local_cat_path.with_name("catalog.json.bak")
            self.assertTrue(bak_path.is_file())
            self.assertEqual(bak_path.read_text(encoding="utf-8"), original_raw)

            # 校验 verify_links 对每个条目发起 HEAD 请求
            report = catalog.verify_links()
            self.assertTrue(report["shared_game"][0])
            self.assertTrue(bool(shared_entry.last_verified))
            self.assertFalse(report["remote_new"][0])
        finally:
            server.shutdown()
            server.server_close()

    def test_p3_2_post_install_sha256_verification_and_dry_run(self) -> None:
        """P3-2: dry_run 仅预览不写盘；compute_sha256 返回错误值时触发自动回滚且 success=False。"""
        patch_dir = self.root / "patch_p3_2"
        patch_dir.mkdir()
        (patch_dir / "a.xp3").write_bytes(b"DRY_AND_VERIFY_A")
        (patch_dir / "added.xp3").write_bytes(b"DRY_AND_VERIFY_ADDED")

        # 1) Dry-Run 模式不改动磁盘文件
        dry_res = self.patcher.apply(patch_dir, game_dir=self.game_dir, dry_run=True)
        self.assertTrue(dry_res.success)
        self.assertFalse(dry_res.verified)
        self.assertIn("仅预览 Dry-Run", dry_res.message)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertFalse((self.game_dir / "added.xp3").exists())

        # 2) Mock compute_sha256 返回错误哈希值，触发写后校验失败与自动回滚
        with mock.patch(
            "galgame_patcher.patcher.compute_sha256", return_value="0" * 64
        ):
            bad_res = self.patcher.apply(patch_dir, game_dir=self.game_dir)

        self.assertFalse(bad_res.success)
        self.assertFalse(bad_res.verified)
        self.assertIn("a.xp3", bad_res.failed_files)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"ORIG_A")
        self.assertFalse((self.game_dir / "added.xp3").exists())

    def test_p3_3_concurrency_lock_and_stale_pid_cleanup(self) -> None:
        """P3-3: .lock 中为当前活动 PID 时抛 ConcurrencyError；为不存在的陈旧 PID 时自动清理并继续。"""
        lock_dir = self.game_dir / GalgamePatcher.BACKUP_FOLDER_NAME
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / ".lock"

        patch_file = self.root / "a.xp3"
        patch_file.write_bytes(b"LOCK_TEST_A")

        # 1) 写入当前进程 PID，第二次实例应抛 ConcurrencyError
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        with self.assertRaises(ConcurrencyError) as ctx:
            self.patcher.apply(patch_file, game_dir=self.game_dir)
        self.assertIn(str(os.getpid()), str(ctx.exception))

        # 2) 写入一个不存在的陈旧 PID，应自动清理锁并完成补丁安装
        lock_file.write_text("99999991", encoding="utf-8")
        with mock.patch("galgame_patcher.patcher._is_pid_running", return_value=False):
            res = self.patcher.apply(patch_file, game_dir=self.game_dir)
        self.assertTrue(res.success)
        self.assertEqual((self.game_dir / "a.xp3").read_bytes(), b"LOCK_TEST_A")
        self.assertFalse(lock_file.exists())

    def test_online_patch_scraper_and_cli_search(self) -> None:
        """测试自动从补丁发布网页爬取 .zip / .7z 补丁下载直链。"""
        web_dir = self.root / "web_scraper"
        web_dir.mkdir()
        (web_dir / "index.html").write_text(
            """
            <html><body>
              <a href="/files/official_patch_v1.2.zip">Official Patch v1.2 (ZIP)</a>
              <a href="/files/voice_pack.7z">Voice Restoration Pack (7z)</a>
              <a href="/about.html">About Us</a>
            </body></html>
            """,
            encoding="utf-8",
        )

        handler = functools.partial(_SilentHTTPHandler, directory=str(web_dir))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            page_url = f"http://127.0.0.1:{port}/index.html"
            candidates = discover_patches_online(page_url)
            self.assertEqual(len(candidates), 2)
            self.assertTrue(all(c.is_direct_download for c in candidates))
            urls = [c.url for c in candidates]
            self.assertIn(f"http://127.0.0.1:{port}/files/official_patch_v1.2.zip", urls)
            self.assertIn(f"http://127.0.0.1:{port}/files/voice_pack.7z", urls)

            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                code = cli_main(["search", page_url])
            self.assertEqual(code, 0)
            self.assertIn("official_patch_v1.2.zip", out_buf.getvalue())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
