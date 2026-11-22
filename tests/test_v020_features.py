"""v0.2.0 新增功能测试：加密归档、密码自动尝试、.7z、在线下载、清单扫描、CLI 与 GUI。"""

from __future__ import annotations

import functools
import http.server
import io
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path

from galgame_patcher import (
    GalgamePatcher,
    PatchCatalog,
    SteamLocator,
    compute_sha256,
    download_patch,
)
from galgame_patcher.archive import build_password_list, extract_archive
from galgame_patcher.cli import main as cli_main


def _crc32_step(crc: int, byte_val: int) -> int:
    return zlib.crc32(bytes([byte_val]), crc ^ 0xFFFFFFFF) ^ 0xFFFFFFFF


def _build_encrypted_zip(
    zip_path: Path, filename: str, plaintext: bytes, password: str
) -> None:
    """使用标准 PKZIP ZipCrypto 加密生成测试用加密 .zip 文件。"""
    key0 = 0x12345678
    key1 = 0x23456789
    key2 = 0x34567890

    def update_keys(b: int) -> None:
        nonlocal key0, key1, key2
        key0 = _crc32_step(key0, b)
        key1 = ((key1 + (key0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        key2 = _crc32_step(key2, (key1 >> 24) & 0xFF)

    def encrypt_byte(b: int) -> int:
        temp = (key2 | 2) & 0xFFFF
        k = ((temp * (temp ^ 1)) >> 8) & 0xFF
        c = b ^ k
        update_keys(b)
        return c

    for pb in password.encode("utf-8"):
        update_keys(pb)

    file_crc = zlib.crc32(plaintext) & 0xFFFFFFFF
    header = bytearray([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, (file_crc >> 24) & 0xFF])
    encrypted_payload = bytes(encrypt_byte(b) for b in bytes(header) + plaintext)

    name_bytes = filename.encode("utf-8")
    comp_size = len(encrypted_payload)
    uncomp_size = len(plaintext)
    flags = 0x0001  # 加密标志位

    local_header = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        flags,
        0,  # STORE
        0,
        0,
        file_crc,
        comp_size,
        uncomp_size,
        len(name_bytes),
        0,
    )
    local_offset = 0
    central_offset = len(local_header) + len(name_bytes) + comp_size

    central_header = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        flags,
        0,
        0,
        0,
        file_crc,
        comp_size,
        uncomp_size,
        len(name_bytes),
        0,
        0,
        0,
        0,
        0,
        local_offset,
    )
    central_size = len(central_header) + len(name_bytes)

    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        1,
        1,
        central_size,
        central_offset,
        0,
    )

    zip_path.write_bytes(
        local_header + name_bytes + encrypted_payload + central_header + name_bytes + eocd
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class TestV020Features(unittest.TestCase):
    """覆盖加密压缩包、.7z、在线下载、清单扫描、新 CLI 命令与 GUI 导入。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.steam_root = self.root / "Steam"
        self.game_dir = self.steam_root / "steamapps" / "common" / "SenrenBanka"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "SenrenBanka.exe").write_bytes(b"ORIG_EXE")
        (self.game_dir / "patch.xp3").write_bytes(b"ORIG_XP3")

        acf_content = '''
"AppState"
{
    "appid"        "1144400"
    "installdir"   "SenrenBanka"
}
'''
        (self.steam_root / "steamapps" / "appmanifest_1144400.acf").write_text(
            acf_content, encoding="utf-8"
        )

        self.locator = SteamLocator(steam_root=self.steam_root)
        self.patcher = GalgamePatcher(locator=self.locator)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_password_list_ordering_and_encrypted_zip_auto_password(self) -> None:
        candidates = build_password_list(password="custom_pw", password_candidates=["extra"])
        self.assertEqual(candidates[0], "custom_pw")
        self.assertIn("galgame", candidates)
        self.assertIn("extra", candidates)

        # 使用内置常见密码 "galgame" 加密，不显式传入密码时应能自动轮询解出
        enc_zip = self.root / "encrypted_patch.zip"
        _build_encrypted_zip(
            enc_zip,
            "PatchTopDir/patch.xp3",
            b"DECRYPTED_XP3_CONTENT",
            password="galgame",
        )

        res = self.patcher.apply(enc_zip, game_dir=self.game_dir)
        self.assertTrue(res.success)
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"DECRYPTED_XP3_CONTENT")

        # 使用非常见密码加密时，显式传入正确密码可通过，不传应报错
        custom_zip = self.root / "secret_patch.zip"
        _build_encrypted_zip(
            custom_zip,
            "voice.xp3",
            b"SECRET_VOICE",
            password="my_unique_secret_999",
        )

        with self.assertRaises(ValueError):
            extract_archive(custom_zip, self.root / "out_fail")

        res2 = self.patcher.apply(
            custom_zip,
            game_dir=self.game_dir,
            password="my_unique_secret_999",
        )
        self.assertTrue(res2.success)
        self.assertEqual((self.game_dir / "voice.xp3").read_bytes(), b"SECRET_VOICE")

    def test_7z_archive_patch_if_bsdtar_or_7z_available(self) -> None:
        # Windows 11/Server 内置 bsdtar 支持生成与解压 .7z
        tar_exe = (
            r"C:\Windows\system32\tar.exe"
            if sys.platform == "win32"
            else shutil.which("bsdtar")
        )
        if not tar_exe or not Path(tar_exe).exists():
            self.skipTest("当前环境无 bsdtar，跳过真实 .7z 生成测试")

        src_stage = self.root / "stage7z" / "WrapperDir"
        src_stage.mkdir(parents=True)
        (src_stage / "patch.xp3").write_bytes(b"7Z_PATCH_DATA")

        archive_7z = self.root / "patch.7z"
        proc = subprocess.run(
            [tar_exe, "-a", "-cf", str(archive_7z), "-C", str(self.root / "stage7z"), "WrapperDir"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not archive_7z.is_file():
            self.skipTest("当前 bsdtar 不支持打包 .7z 格式")

        res = self.patcher.apply(archive_7z, game_dir=self.game_dir)
        self.assertTrue(res.success)
        self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"7Z_PATCH_DATA")

    def test_http_catalog_scan_download_and_cli_auto(self) -> None:
        web_dir = self.root / "web"
        web_dir.mkdir()

        patch_zip = web_dir / "senren_chs_v2.zip"
        with zipfile.ZipFile(patch_zip, "w") as zf:
            zf.writestr("SenrenPatch/patch.xp3", b"ONLINE_PATCH_V2")

        patch_sha = compute_sha256(patch_zip)
        catalog_path = web_dir / "catalog.json"
        catalog_data = {
            "version": 1,
            "games": [
                {
                    "id": "senren_banka",
                    "name": "千恋＊万花",
                    "steam_app_id": 1144400,
                    "install_dir_name": "SenrenBanka",
                    "executable_name": "SenrenBanka.exe",
                    "patch_url": "",
                    "patch_version": "2.0",
                    "expected_sha256": patch_sha,
                },
                {
                    "id": "not_installed_game",
                    "name": "不存在的测试游戏",
                    "steam_app_id": 999888,
                    "patch_url": "",
                },
            ],
        }

        handler = functools.partial(_QuietHandler, directory=str(web_dir))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            base_url = f"http://127.0.0.1:{port}"
            catalog_data["games"][0]["patch_url"] = f"{base_url}/senren_chs_v2.zip"
            catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")

            # 1) 从 URL 加载清单并扫描本地游戏
            catalog = PatchCatalog.load(f"{base_url}/catalog.json")
            statuses = catalog.scan_games(locator=self.locator, only_installed=True)
            self.assertEqual(len(statuses), 1)
            self.assertEqual(statuses[0].entry.id, "senren_banka")
            self.assertTrue(statuses[0].installed)
            self.assertFalse(statuses[0].patched)

            # 2) 测试下载缓存与进度回调
            progress_events: list[tuple[int, int | None]] = []
            cache_dir = self.root / "dl_cache"
            downloaded = download_patch(
                f"{base_url}/senren_chs_v2.zip",
                dest_dir=cache_dir,
                expected_sha256=patch_sha,
                progress_callback=lambda d, t: progress_events.append((d, t)),
            )
            self.assertTrue(downloaded.is_file())
            self.assertTrue(len(progress_events) >= 1)

            # 3) 测试 CLI scan 与 auto 子命令
            scan_out = io.StringIO()
            with redirect_stdout(scan_out):
                code_scan = cli_main(
                    [
                        "scan",
                        "--catalog",
                        f"{base_url}/catalog.json",
                        "--only-installed",
                        "--steam-root",
                        str(self.steam_root),
                    ]
                )
            self.assertEqual(code_scan, 0)
            self.assertIn("千恋＊万花", scan_out.getvalue())

            with redirect_stdout(io.StringIO()):
                code_auto = cli_main(
                    [
                        "auto",
                        "senren_banka",
                        "--catalog",
                        f"{base_url}/catalog.json",
                        "--steam-root",
                        str(self.steam_root),
                    ]
                )
            self.assertEqual(code_auto, 0)
            self.assertEqual((self.game_dir / "patch.xp3").read_bytes(), b"ONLINE_PATCH_V2")

            # 打补丁后再次扫描应显示 patched=True
            statuses_after = catalog.scan_games(locator=self.locator, only_installed=True)
            self.assertTrue(statuses_after[0].patched)
        finally:
            server.shutdown()
            server.server_close()

    def test_gui_module_importable_and_instantiable(self) -> None:
        from galgame_patcher import gui

        self.assertTrue(callable(gui.launch_gui))
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            app = gui.GalgamePatcherGUI(root)
            self.assertIsNotNone(app.tree)
            root.destroy()
        except Exception as exc:
            # 无图形显示环境的 Linux CI 容器上允许跳过窗口实例化
            if sys.platform.startswith("linux"):
                self.skipTest(f"无图形显示环境，跳过 Tk 窗口测试: {exc}")
            raise


if __name__ == "__main__":
    unittest.main()
