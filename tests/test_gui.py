"""问题 8 测试：GUI 纯逻辑单测（无 tkinter 依赖）与主窗口启动冒烟测试（无显示环境自动 skip）。"""

from __future__ import annotations

import gc
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galgame_patcher.gui_logic import (
    build_default_search_query,
    compute_download_progress,
    filter_game_statuses,
    format_apply_confirm_message,
    format_candidate_detail,
    format_candidate_row,
    format_game_status_text,
    format_install_state_summary,
    format_patch_exception,
    format_search_summary,
    format_transfer_speed,
    summarize_error_status,
    validate_patch_task_inputs,
)
from galgame_patcher.models import (
    CatalogEntry,
    ConcurrencyError,
    GameRunningError,
    InstalledGameStatus,
    InstallStatus,
    OnlinePatchCandidate,
)


class TestGuiPureLogic(unittest.TestCase):
    """无需 tkinter 与显示环境即可运行的 GUI 纯逻辑单元测试。"""

    def test_format_speed_and_download_progress(self) -> None:
        self.assertEqual(format_transfer_speed(512), "512 B/s")
        self.assertEqual(format_transfer_speed(2048), "2.0 KB/s")
        self.assertEqual(format_transfer_speed(3 * 1024 * 1024), "3.00 MB/s")
        self.assertEqual(format_transfer_speed(-10), "0 B/s")

        pct, msg = compute_download_progress(500, 1000, 1.0)
        self.assertAlmostEqual(pct, 50.0)
        self.assertIn("50.0%", msg)
        self.assertIn("500/1000 字节", msg)

        # 下载满时封顶 99.0%（直到写盘与校验完成才置 100%）
        pct_cap, _ = compute_download_progress(2000, 1000, 0.0)
        self.assertEqual(pct_cap, 99.0)

        # 未知总体积时进度条固定显示 50.0
        pct_unknown, msg_unknown = compute_download_progress(1024, None, 2.0)
        self.assertEqual(pct_unknown, 50.0)
        self.assertIn("1024 字节", msg_unknown)
        self.assertIn("512 B/s", msg_unknown)

    def test_filter_game_statuses_and_status_labels(self) -> None:
        statuses = [
            InstalledGameStatus(
                entry=CatalogEntry(
                    id="senren",
                    name="Senren * Banka",
                    steam_app_id=1144400,
                    install_dir_name="SenrenBanka",
                ),
                installed=True,
                game_dir=Path("/games/SenrenBanka"),
                patched=True,
            ),
            InstalledGameStatus(
                entry=CatalogEntry(
                    id="riddle",
                    name="Riddle Joker",
                    steam_app_id=1277930,
                    install_dir_name="RiddleJoker",
                ),
                installed=True,
                game_dir=Path("/games/RiddleJoker"),
                patched=False,
            ),
            InstalledGameStatus(
                entry=CatalogEntry(id="broken", name="Broken Manifest"),
                installed=True,
                game_dir=Path("/games/Broken"),
                patched=None,
            ),
            InstalledGameStatus(
                entry=CatalogEntry(id="uninstalled", name="Future VN"),
                installed=False,
                game_dir=None,
                patched=False,
            ),
        ]

        self.assertEqual(format_game_status_text(statuses[0]), "已安装 / 已打补丁")
        self.assertEqual(format_game_status_text(statuses[1]), "已安装 / 未打补丁")
        self.assertEqual(format_game_status_text(statuses[2]), "已安装 / 补丁状态待确认")
        self.assertEqual(format_game_status_text(statuses[3]), "未安装")

        all_rows = filter_game_statuses(statuses)
        self.assertEqual([idx for idx, _, _ in all_rows], [0, 1, 2, 3])

        installed_only = filter_game_statuses(statuses, only_installed=True)
        self.assertEqual([idx for idx, _, _ in installed_only], [0, 1, 2])

        by_appid = filter_game_statuses(statuses, keyword="1277930")
        self.assertEqual([idx for idx, _, _ in by_appid], [1])

        by_case_insensitive_name = filter_game_statuses(statuses, keyword="  SENREN ")
        self.assertEqual([idx for idx, _, _ in by_case_insensitive_name], [0])

        by_dir = filter_game_statuses(statuses, keyword="broken")
        self.assertEqual([idx for idx, _, _ in by_dir], [2])

    def test_error_and_exception_formatting(self) -> None:
        self.assertEqual(summarize_error_status("第一行错误\n第二行详情"), "错误: 第一行错误")
        self.assertEqual(summarize_error_status(""), "错误: 未知错误")

        kind, title, detail = format_patch_exception(
            GameRunningError(["data.xp3"]), operation="apply"
        )
        self.assertEqual(kind, "warning")
        self.assertIn("游戏正在运行", title)
        self.assertIn("data.xp3", detail)

        kind_rb, title_rb, _ = format_patch_exception(
            GameRunningError(["data.xp3"]), operation="rollback"
        )
        self.assertEqual(kind_rb, "warning")
        self.assertEqual(title_rb, "文件被占用")

        kind_cc, title_cc, detail_cc = format_patch_exception(
            ConcurrencyError(pid=999), operation="apply"
        )
        self.assertEqual(kind_cc, "warning")
        self.assertIn("另一个实例", title_cc)
        self.assertIn("999", detail_cc)

        kind_err, title_err, detail_err = format_patch_exception(
            RuntimeError("磁盘已满"), operation="apply"
        )
        self.assertEqual(kind_err, "error")
        self.assertEqual(title_err, "操作失败")
        self.assertEqual(detail_err, "磁盘已满")

    def test_search_summary_candidate_and_confirm_helpers(self) -> None:
        msg, logs, warn = format_search_summary(
            "TestVN", 0, [("GitHub", "HTTP 403 限流"), ("VNDB", "请求超时")]
        )
        self.assertIn("找到 0 条", msg)
        self.assertIn("GitHub、VNDB", msg)
        self.assertEqual(len(logs), 2)
        self.assertIsNotNone(warn)
        assert warn is not None
        self.assertIn("GitHub、VNDB", warn)

        msg_ok, logs_ok, warn_ok = format_search_summary("TestVN", 2, [])
        self.assertIn("找到 2 条", msg_ok)
        self.assertEqual(logs_ok, [])
        self.assertIsNone(warn_ok)

        trusted_cand = OnlinePatchCandidate(
            title="Official Patch",
            url="https://nekonyansoft.com/p.zip",
            source_site="NekoNyan",
            is_direct_download=True,
            language="en",
            trusted=True,
            trust_reason="发行商白名单",
            description="Steam 版",
        )
        self.assertEqual(
            format_candidate_row(trusted_cand),
            (
                "✔ 白名单",
                "Official Patch",
                "NekoNyan",
                "直链包",
                "en",
                "https://nekonyansoft.com/p.zip",
            ),
        )
        self.assertIn("Steam 版", format_candidate_detail(trusted_cand))
        self.assertIn("发行商白名单", format_candidate_detail(trusted_cand))

        exe_cand = OnlinePatchCandidate(
            title="Setup",
            url="https://example.com/setup.exe",
            source_site="Web",
            manual_install=True,
            bare_executable=True,
        )
        self.assertEqual(format_candidate_row(exe_cand)[3], "手动安装")
        self.assertIn("可执行文件", format_candidate_detail(exe_cand))

        self.assertEqual(
            build_default_search_query(
                CatalogEntry(id="example_game", name="示例"), "/games/MyVN"
            ),
            "MyVN",
        )
        self.assertEqual(
            build_default_search_query(
                CatalogEntry(id="real", name="Real Name", install_dir_name="RealDir"),
                "/games/Other",
            ),
            "RealDir",
        )

        # 输入校验
        self.assertIsNotNone(validate_patch_task_inputs("", "/p.zip"))
        self.assertIsNotNone(validate_patch_task_inputs("/game", ""))
        url_warn = validate_patch_task_inputs("/game", "https://example.com/p.zip")
        self.assertIsNotNone(url_warn)
        assert url_warn is not None
        self.assertIn("URL", url_warn[0])
        self.assertIsNone(validate_patch_task_inputs("/game", "/local/p.zip"))

        confirm_no_hash = format_apply_confirm_message(
            "/game", backup=False, expected_sha256=None
        )
        self.assertIn("取消勾选自动备份", confirm_no_hash)
        self.assertIn("未填写发布者的 SHA256", confirm_no_hash)

        status = InstallStatus(
            game_dir=Path("/game"),
            state="incomplete",
            latest_install="install-abc",
            applied_at="2026-01-01T00:00:00+00:00",
            backup_size_bytes=4096,
        )
        summary = format_install_state_summary(status)
        self.assertIn("上次安装未完成", summary)
        self.assertIn("4.00 KB", summary)
        self.assertIn("恢复未完成安装", summary)


class TestGuiWindowSmoke(unittest.TestCase):
    """主窗口启动冒烟测试：在 Windows 或 Linux Xvfb 环境下真实创建主窗口，无显示环境自动 skip。"""

    def test_main_window_startup_filter_and_clean_shutdown(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"当前 Python 未包含 tkinter，跳过 GUI 冒烟测试: {exc}")

        from galgame_patcher import gui

        with tempfile.TemporaryDirectory() as tmp:
            old_appdata = os.environ.get("GALGAME_PATCHER_APPDATA")
            os.environ["GALGAME_PATCHER_APPDATA"] = str(Path(tmp) / "appdata")
            root = None
            app = None
            try:
                try:
                    root = tk.Tk()
                except tk.TclError as exc:
                    self.skipTest(f"无图形显示环境（未启动 Xvfb / 无 DISPLAY），跳过 GUI 冒烟测试: {exc}")

                root.withdraw()
                with mock.patch.object(gui.GalgamePatcherGUI, "scan_games_async"):
                    app = gui.GalgamePatcherGUI(root)

                root.update_idletasks()
                self.assertIn("Galgame", root.title())
                self.assertIsNotNone(app.tree)
                self.assertIsNotNone(app.btn_apply)
                self.assertIsNotNone(app.btn_rollback)
                self.assertIsNotNone(app.btn_recover)

                # 注入示例列表并验证实时过滤与树形视图渲染
                app._statuses = [
                    InstalledGameStatus(
                        entry=CatalogEntry(id="g1", name="Alpha Novel"),
                        installed=True,
                        game_dir=Path(tmp),
                        patched=False,
                    ),
                    InstalledGameStatus(
                        entry=CatalogEntry(id="g2", name="Beta Novel"),
                        installed=False,
                    ),
                ]
                app.only_installed_var.set(True)
                app._refresh_tree()
                self.assertEqual(app._filtered_indices, [0])
                self.assertEqual(len(app.tree.get_children()), 1)

                app.search_var.set("beta")
                app.only_installed_var.set(False)
                app._refresh_tree()
                self.assertEqual(app._filtered_indices, [1])
            finally:
                if app is not None:
                    app._on_close()
                elif root is not None:
                    try:
                        if root.winfo_exists():
                            root.destroy()
                    except tk.TclError:
                        pass
                app = None
                root = None
                gc.collect()
                logger = logging.getLogger("galgame_patcher")
                for handler in list(logger.handlers):
                    if isinstance(handler, logging.FileHandler):
                        handler.close()
                        logger.removeHandler(handler)
                if old_appdata is None:
                    os.environ.pop("GALGAME_PATCHER_APPDATA", None)
                else:
                    os.environ["GALGAME_PATCHER_APPDATA"] = old_appdata


if __name__ == "__main__":
    unittest.main()
