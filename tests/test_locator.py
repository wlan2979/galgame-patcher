"""SteamLocator 与游戏目录自动定位测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from galgame_patcher import GameProfile, SteamLocator, locate_game


class TestSteamLocator(unittest.TestCase):
    """测试模拟 Steam 目录结构下的多种游戏定位方式。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.steam_root = self.root / "Steam"
        self.secondary_lib = self.root / "D_SteamLibrary"

        (self.steam_root / "steamapps" / "common").mkdir(parents=True)
        (self.secondary_lib / "steamapps" / "common").mkdir(parents=True)

        # 写入包含两个库路径的 libraryfolders.vdf（含 Windows 双反斜杠转义格式）
        escaped_steam = str(self.steam_root).replace("\\", "\\\\")
        escaped_secondary = str(self.secondary_lib).replace("\\", "\\\\")
        vdf_content = f'''
"libraryfolders"
{{
    "0"
    {{
        "path"        "{escaped_steam}"
    }}
    "1"
    {{
        "path"        "{escaped_secondary}"
    }}
}}
'''
        (self.steam_root / "steamapps" / "libraryfolders.vdf").write_text(
            vdf_content, encoding="utf-8"
        )

        # 在第二个 Steam 库中创建模拟游戏与 appmanifest
        self.game_dir = self.secondary_lib / "steamapps" / "common" / "TestGalgame"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "Game.exe").write_bytes(b"MZ")

        acf_content = '''
"AppState"
{
    "appid"        "114514"
    "name"         "Test Galgame HD"
    "installdir"   "TestGalgame"
}
'''
        (
            self.secondary_lib / "steamapps" / "appmanifest_114514.acf"
        ).write_text(acf_content, encoding="utf-8")

        self.locator = SteamLocator(steam_root=self.steam_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_get_library_folders(self) -> None:
        libraries = self.locator.get_library_folders()
        self.assertEqual(
            libraries,
            [self.steam_root.resolve(), self.secondary_lib.resolve()],
        )

    def test_find_by_app_id(self) -> None:
        found = self.locator.find_by_app_id(114514, executable_name="Game.exe")
        self.assertEqual(found, self.game_dir.resolve())

    def test_find_by_name_variants(self) -> None:
        # 精确匹配
        self.assertEqual(
            self.locator.find_by_name("TestGalgame"),
            self.game_dir.resolve(),
        )
        # 大小写不敏感匹配
        self.assertEqual(
            self.locator.find_by_name("testgalgame"),
            self.game_dir.resolve(),
        )
        # 子串匹配
        self.assertEqual(
            self.locator.find_by_name("Galgame", executable_name="Game.exe"),
            self.game_dir.resolve(),
        )
        # 可执行文件校验失败时返回 None
        self.assertIsNone(
            self.locator.find_by_name("TestGalgame", executable_name="NotExists.exe")
        )

    def test_locate_with_profile_and_missing_error(self) -> None:
        profile = GameProfile(
            name="测试作品",
            steam_app_id=114514,
            install_dir_name="TestGalgame",
            executable_name="Game.exe",
        )
        found = locate_game(profile=profile, steam_root=self.steam_root)
        self.assertEqual(found, self.game_dir.resolve())

        with self.assertRaises(FileNotFoundError):
            self.locator.locate(app_id=999999, game_name="NonExistentGalgame")


if __name__ == "__main__":
    unittest.main()
