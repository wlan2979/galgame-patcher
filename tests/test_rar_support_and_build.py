"""问题 5 测试：7za.exe 随包查找、build_exe 打包与缺失警告、verify-file/inspect RAR 前置检测。"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from galgame_patcher import detect_rar_support, inspect_patch_file
from galgame_patcher.archive import _find_7z_executable
from galgame_patcher.cli import main as cli_main


def _load_build_exe_module() -> object:
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "build_exe.py"
    spec = importlib.util.spec_from_file_location("build_exe_under_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRarSupportAndPackaging(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_find_7z_executable_prefers_meipass_bundled_7za(self) -> None:
        meipass = self.root / "meipass"
        bin_dir = meipass / "bin"
        bin_dir.mkdir(parents=True)
        exe_name = "7za.exe" if sys.platform == "win32" else "7za"
        bundled_7za = bin_dir / exe_name
        bundled_7za.write_bytes(b"FAKE_7ZA")

        with mock.patch.dict(os.environ, {"GALGAME_PATCHER_7Z_PATH": ""}), mock.patch.object(
            sys, "_MEIPASS", str(meipass), create=True
        ):
            found = _find_7z_executable()
        self.assertEqual(found, str(bundled_7za))

    def test_detect_rar_support_and_inspect_rar_warnings(self) -> None:
        rar_file = self.root / "patch.rar"
        rar_file.write_bytes(b"Rar!\x1a\x07\x00dummy")

        # 1) 完全没有 RAR 解压后端
        with mock.patch(
            "galgame_patcher.archive._find_7z_executable", return_value=None
        ), mock.patch(
            "galgame_patcher.archive._find_unrar_executable", return_value=None
        ), mock.patch(
            "galgame_patcher.archive._find_bsdtar_executable", return_value=None
        ):
            supported, backend, detail = detect_rar_support()
            self.assertFalse(supported)
            self.assertEqual(backend, "")
            self.assertIn("7-Zip", detail)

            inspection = inspect_patch_file(rar_file)
            self.assertTrue(inspection.is_archive)
            self.assertEqual(inspection.archive_format, ".rar")
            self.assertFalse(inspection.rar_supported)
            self.assertTrue(inspection.warnings)
            self.assertIn("RAR 支持:", inspection.describe())

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli_main(["verify-file", str(rar_file)])
            self.assertEqual(code, 0)
            self.assertIn("RAR 支持:", out.getvalue())
            self.assertIn("无法直接解压 .rar", err.getvalue())

        # 2) 检测到 7-Zip 时正常提示支持
        with mock.patch(
            "galgame_patcher.archive._find_7z_executable", return_value="/fake/7za.exe"
        ):
            supported, backend, detail = detect_rar_support()
            self.assertTrue(supported)
            self.assertEqual(backend, "7z")
            self.assertIn("7za.exe", detail)

            inspection_ok = inspect_patch_file(rar_file)
            self.assertTrue(inspection_ok.rar_supported)
            self.assertEqual(inspection_ok.warnings, [])

        # 3) 仅检测到 bsdtar 时提示部分支持
        with mock.patch(
            "galgame_patcher.archive._find_7z_executable", return_value=None
        ), mock.patch(
            "galgame_patcher.archive._find_unrar_executable", return_value=None
        ), mock.patch(
            "galgame_patcher.archive._find_bsdtar_executable", return_value="/usr/bin/bsdtar"
        ):
            supported, backend, detail = detect_rar_support()
            self.assertTrue(supported)
            self.assertIn("bsdtar", backend)
            self.assertIn("仅支持部分无密码 RAR", detail)

    def test_build_exe_bundles_7za_and_warns_when_missing(self) -> None:
        build_exe = _load_build_exe_module()
        fake_root = self.root / "proj"
        vendor_dir = fake_root / "vendor" / "7zip"
        vendor_dir.mkdir(parents=True)
        entry = fake_root / "entry.py"
        entry.write_text("", encoding="utf-8")

        fake_7za = vendor_dir / "7za.exe"
        fake_7za.write_bytes(b"MZ7ZA")
        fake_dll = vendor_dir / "7z.dll"
        fake_dll.write_bytes(b"MZDLL")

        out_ok = io.StringIO()
        with redirect_stdout(out_ok):
            cmd, bins = build_exe.build_pyinstaller_command(fake_root, entry)  # type: ignore[attr-defined]
        self.assertEqual(bins, [fake_7za, fake_dll])
        self.assertIn(f"{fake_7za}{os.pathsep}bin", cmd)
        self.assertIn(f"{fake_dll}{os.pathsep}bin", cmd)

        # 未找到 7za/7z 时必须输出明确警告而不是静默跳过
        empty_root = self.root / "empty_proj"
        empty_root.mkdir()
        err_buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GALGAME_PATCHER_7Z_PATH": ""}), mock.patch.object(
            build_exe, "find_7z_binaries", return_value=[]
        ), redirect_stderr(err_buf):
            cmd_missing, bins_missing = build_exe.build_pyinstaller_command(empty_root, entry)  # type: ignore[attr-defined]
        self.assertEqual(bins_missing, [])
        self.assertNotIn("--add-binary", cmd_missing)
        self.assertIn("WARNING", err_buf.getvalue())
        self.assertIn("7za.exe", err_buf.getvalue())

        # require_7z=True 时直接报错
        with mock.patch.object(build_exe, "find_7z_binaries", return_value=[]), redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(FileNotFoundError):
                build_exe.build_pyinstaller_command(empty_root, entry, require_7z=True)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
