"""基于 Tkinter / ttk 的 Galgame 一键补丁桌面图形界面（P3-1 重构版）。"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

from ._version import __version__
from .catalog import PatchCatalog, _is_game_patched
from .downloader import (
    OFFICIAL_PATCH_PORTALS,
    discover_patches_online,
    get_user_sources_path,
    is_url,
    load_user_sources,
    save_user_sources,
)
from .gui_logic import (
    build_default_search_query,
    compute_download_progress,
    filter_game_statuses,
    format_apply_confirm_message,
    format_candidate_detail,
    format_candidate_row,
    format_install_state_summary,
    format_patch_exception,
    format_search_summary,
    format_transfer_speed,
    summarize_error_status,
    validate_patch_task_inputs,
)
from .locator import ManualLocator, SteamLocator
from .models import (
    CatalogEntry,
    ConcurrencyError,
    GameRunningError,
    InstalledGameStatus,
    OnlinePatchCandidate,
    PatchFileInspection,
    get_appdata_dir,
)
from .patcher import GalgamePatcher, inspect_patch_file

logger = logging.getLogger("galgame_patcher")


def _setup_gui_file_logger() -> Path:
    """配置写入 %APPDATA%\\galgame-patcher\\logs\\gui.log 的文件日志处理器（P3-1 #8）。"""
    log_dir = get_appdata_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "gui.log"

    has_file_handler = any(
        isinstance(h, logging.FileHandler)
        and getattr(h, "baseFilename", None) == str(log_file.resolve())
        for h in logger.handlers
    )
    if not has_file_handler:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(fh)
        logger.setLevel(logging.DEBUG)
    return log_file


def _enable_windows_dpi_awareness() -> None:
    """在 Windows 下开启高 DPI 适配，避免界面模糊。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _format_speed(bytes_per_sec: float) -> str:
    return format_transfer_speed(bytes_per_sec)


class GalgamePatcherGUI:
    """Galgame 补丁安装器桌面可视化窗口（左右分栏、防抖搜索、Queue 异步通信、在线爬虫）。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.log_file_path = _setup_gui_file_logger()
        self.gui_config_path = get_appdata_dir() / "gui.json"
        self._gui_config = self._load_gui_config()

        self.root.title(f"Galgame 一键补丁工具 v{__version__}")
        initial_geometry = str(self._gui_config.get("geometry") or "1020x680")
        try:
            self.root.geometry(initial_geometry)
        except tk.TclError:
            self.root.geometry("1020x680")
        self.root.minsize(880, 580)

        self.manual_locator = ManualLocator()
        self.locator = SteamLocator(manual_locator=self.manual_locator)
        self.patcher = GalgamePatcher(locator=self.locator)
        self.catalog = PatchCatalog.default()
        self._statuses: list[InstalledGameStatus] = []
        self._filtered_indices: list[int] = []
        self._busy = False
        self._inspecting = False
        self._install_active = False
        self._closing = False
        self._cancel_scan_flag = False
        self._search_after_id: Optional[str] = None
        self._poll_after_id: Optional[str] = None
        self._ui_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._active_thread: Optional[threading.Thread] = None
        self._confirmed_exec_key: Optional[tuple[str, str]] = None
        self._inspected_key: Optional[tuple[str, str]] = None

        self.search_var = tk.StringVar()
        self.only_installed_var = tk.BooleanVar(value=False)
        self.include_manual_var = tk.BooleanVar(value=True)
        self.deep_scan_var = tk.BooleanVar(value=False)
        self.backup_var = tk.BooleanVar(value=True)
        self.undo_latest_var = tk.BooleanVar(value=False)

        self.detail_name_var = tk.StringVar(value="（请在左侧选择游戏或手动指定路径）")
        self.detail_exe_var = tk.StringVar(value="-")
        self.detail_desc_var = tk.StringVar(value="-")
        self.detail_state_var = tk.StringVar(value="补丁状态: -")
        self.game_dir_var = tk.StringVar(
            value=str(self._gui_config.get("last_game_dir") or "")
        )
        self.patch_source_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.sha256_var = tk.StringVar()
        self.target_subdir_var = tk.StringVar()
        self.status_var = tk.StringVar(
            value="就绪：支持扫描 Steam / 本地游戏、自动上网爬取补丁或按 Ctrl+V 粘贴补丁路径。"
        )
        self._build_ui()
        # 手动改游戏目录时清空上一条清单条目遗留的目标子目录，并刷新补丁状态。
        self.game_dir_var.trace_add("write", self._on_game_dir_var_changed)
        self._refresh_install_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-v>", self._on_paste_shortcut)
        self.root.bind("<Control-V>", self._on_paste_shortcut)
        self._poll_after_id = self.root.after(100, self._poll_queue)
        self.scan_games_async()

    def _load_gui_config(self) -> dict[str, Any]:
        if not self.gui_config_path.is_file():
            return {}
        try:
            data = json.loads(self.gui_config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_gui_config(self) -> None:
        try:
            payload = {
                "geometry": self.root.geometry(),
                "last_game_dir": self.game_dir_var.get().strip(),
                "last_patch_dir": self._gui_config.get("last_patch_dir") or "",
            }
            self.gui_config_path.parent.mkdir(parents=True, exist_ok=True)
            self.gui_config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("保存 GUI 配置失败: %s", exc)

    def _on_game_dir_var_changed(self, *_args: object) -> None:
        if self.target_subdir_var.get():
            self.target_subdir_var.set("")
        if hasattr(self, "btn_recover"):
            self._refresh_install_state()

    def _on_close(self) -> None:
        """关闭窗口；若正在写补丁则等待写完，避免留下半截文件。"""
        if self._closing:
            return
        if self._install_active and not messagebox.askyesno(
            "退出确认",
            "补丁操作仍在进行。现在退出会中断写入并可能损坏游戏文件。\n"
            "确定要退出吗？程序会等当前操作完成后再关闭。",
        ):
            return

        self._closing = True
        if self._poll_after_id is not None:
            self.root.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self._cancel_scan_flag = True

        if self._install_active and self._active_thread is not None and self._active_thread.is_alive():
            self._set_status("正在等待当前补丁操作结束，完成后自动关闭…", is_error=True)
            self.root.after(200, self._close_when_idle)
            return
        self._save_gui_config()
        self.root.destroy()

    def _close_when_idle(self) -> None:
        if self._install_active and self._active_thread is not None and self._active_thread.is_alive():
            self.root.after(200, self._close_when_idle)
            return
        self._save_gui_config()
        self.root.destroy()

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 顶部工具栏与搜索框
        toolbar = ttk.Frame(main_frame)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(toolbar, text="搜索:").pack(side=tk.LEFT, padx=(0, 4))
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=22)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 10))
        self.search_entry.bind("<KeyRelease>", self._on_search_key_release)

        self.btn_scan = ttk.Button(
            toolbar, text="扫描本机游戏", command=self.scan_games_async
        )
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_add_game = ttk.Button(
            toolbar, text="登记游戏目录...", command=self.register_manual_game
        )
        self.btn_add_game.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_import_catalog = ttk.Button(
            toolbar, text="导入清单...", command=self.import_catalog
        )
        self.btn_import_catalog.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_export_catalog = ttk.Button(
            toolbar, text="导出清单...", command=self.export_catalog
        )
        self.btn_export_catalog.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Checkbutton(
            toolbar,
            text="仅已安装",
            variable=self.only_installed_var,
            command=self._refresh_tree,
        ).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Checkbutton(
            toolbar,
            text="含手工登记",
            variable=self.include_manual_var,
            command=self.scan_games_async,
        ).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Checkbutton(
            toolbar,
            text="深度扫描非Steam",
            variable=self.deep_scan_var,
            command=self.scan_games_async,
        ).pack(side=tk.LEFT)

        # 中部左右分栏 PanedWindow（P3-1 #1）
        paned = ttk.PanedWindow(main_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        # 左侧：游戏列表（名称 / 状态 / 补丁版本）
        left_frame = ttk.LabelFrame(paned, text="游戏库列表", padding=6)
        paned.add(left_frame, weight=2)

        columns = ("name", "status", "version")
        self.tree = ttk.Treeview(
            left_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("name", text="名称")
        self.tree.heading("status", text="状态")
        self.tree.heading("version", text="补丁版本")

        self.tree.column("name", width=210, anchor=tk.W)
        self.tree.column("status", width=115, anchor=tk.CENTER)
        self.tree.column("version", width=75, anchor=tk.CENTER)

        y_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_row)

        # 右侧：详情与操作面板
        right_frame = ttk.LabelFrame(paned, text="游戏详情与补丁操作", padding=10)
        paned.add(right_frame, weight=3)
        right_frame.columnconfigure(1, weight=1)

        ttk.Label(right_frame, text="当前游戏:").grid(
            row=0, column=0, sticky=tk.W, pady=3, padx=(0, 8)
        )
        ttk.Label(
            right_frame,
            textvariable=self.detail_name_var,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=1, columnspan=2, sticky=tk.W, pady=3)

        ttk.Label(right_frame, text="主程序文件:").grid(
            row=1, column=0, sticky=tk.W, pady=3, padx=(0, 8)
        )
        ttk.Label(right_frame, textvariable=self.detail_exe_var).grid(
            row=1, column=1, columnspan=2, sticky=tk.W, pady=3
        )

        ttk.Label(right_frame, text="补丁说明:").grid(
            row=2, column=0, sticky=tk.W, pady=3, padx=(0, 8)
        )
        desc_frame = ttk.Frame(right_frame)
        desc_frame.grid(row=2, column=1, columnspan=2, sticky=tk.W, pady=3)
        ttk.Label(
            desc_frame, textvariable=self.detail_desc_var, wraplength=420
        ).pack(anchor=tk.W)
        ttk.Label(
            desc_frame,
            textvariable=self.detail_state_var,
            foreground="#1b5e20",
            wraplength=420,
        ).pack(anchor=tk.W, pady=(2, 0))

        ttk.Separator(right_frame, orient=tk.HORIZONTAL).grid(
            row=3, column=0, columnspan=3, sticky=tk.EW, pady=8
        )

        ttk.Label(right_frame, text="游戏安装目录:").grid(
            row=4, column=0, sticky=tk.W, pady=4, padx=(0, 8)
        )
        ttk.Entry(right_frame, textvariable=self.game_dir_var).grid(
            row=4, column=1, sticky=tk.EW, pady=4
        )
        ttk.Button(right_frame, text="浏览目录...", command=self.browse_game_dir).grid(
            row=4, column=2, sticky=tk.W, padx=(6, 0), pady=4
        )

        ttk.Label(right_frame, text="本地补丁路径:").grid(
            row=5, column=0, sticky=tk.W, pady=4, padx=(0, 8)
        )
        ttk.Entry(right_frame, textvariable=self.patch_source_var).grid(
            row=5, column=1, sticky=tk.EW, pady=4
        )
        patch_btn_box = ttk.Frame(right_frame)
        patch_btn_box.grid(row=5, column=2, sticky=tk.W, padx=(6, 0), pady=4)
        ttk.Button(
            patch_btn_box, text="选择补丁文件...", command=self.browse_patch_file
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            patch_btn_box, text="粘贴路径", command=self.paste_patch_path_from_clipboard
        ).pack(side=tk.LEFT)

        # 在线自动爬取补丁按钮行（只提供浏览器检索与链接复制，不自动下载）
        crawl_row = ttk.Frame(right_frame)
        crawl_row.grid(row=6, column=1, columnspan=2, sticky=tk.W, pady=(2, 6))
        self.btn_crawl = ttk.Button(
            crawl_row,
            text="上网搜索补丁 (VNDB/官方站/GitHub)...",
            command=self.on_search_online_clicked,
        )
        self.btn_crawl.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_portals = ttk.Button(
            crawl_row,
            text="补丁站点导航...",
            command=self.open_portals_dialog,
        )
        self.btn_portals.pack(side=tk.LEFT)

        # 本地补丁文件核验预览区（只读，显示体积与 SHA256）
        ttk.Label(right_frame, text="文件核验预览:").grid(
            row=7, column=0, sticky=tk.NW, pady=4, padx=(0, 8)
        )
        self.inspect_text = tk.Text(
            right_frame, height=5, state=tk.DISABLED, wrap=tk.WORD, bg="#f5f5f5"
        )
        self.inspect_text.grid(row=7, column=1, columnspan=2, sticky=tk.EW, pady=4)
        self._set_inspect_text("尚未导入本地补丁文件。请先通过“选择补丁文件...”导入并核对 SHA256。")

        options_row = ttk.Frame(right_frame)
        options_row.grid(row=8, column=0, columnspan=3, sticky=tk.EW, pady=4)
        options_row.columnconfigure(1, weight=1)
        options_row.columnconfigure(3, weight=1)

        ttk.Label(options_row, text="解压密码 (可选):").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6)
        )
        ttk.Entry(options_row, textvariable=self.password_var, width=16).grid(
            row=0, column=1, sticky=tk.EW, padx=(0, 10)
        )

        ttk.Label(options_row, text="预期 SHA256 (可选):").grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6)
        )
        ttk.Entry(options_row, textvariable=self.sha256_var).grid(
            row=0, column=3, sticky=tk.EW
        )

        ttk.Label(options_row, text="目标子目录 (可选，留空=根目录):").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 6), pady=(6, 0)
        )
        ttk.Entry(options_row, textvariable=self.target_subdir_var).grid(
            row=1, column=1, columnspan=3, sticky=tk.EW, pady=(6, 0)
        )

        backup_options = ttk.Frame(right_frame)
        backup_options.grid(row=9, column=0, columnspan=3, sticky=tk.W, pady=4)
        ttk.Checkbutton(
            backup_options, text="安装时保留备份", variable=self.backup_var
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            backup_options, text="回滚时仅撤回最近一次安装", variable=self.undo_latest_var
        ).pack(side=tk.LEFT)

        # 动作按钮栏
        action_bar = ttk.Frame(right_frame)
        action_bar.grid(row=10, column=0, columnspan=3, sticky=tk.W, pady=(10, 4))

        self.btn_dry_run = ttk.Button(
            action_bar, text="仅预览 (Dry-Run)", command=self.on_dry_run_clicked
        )
        self.btn_dry_run.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_apply = ttk.Button(
            action_bar, text="一键安装补丁", command=self.on_apply_clicked
        )
        self.btn_apply.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_rollback = ttk.Button(
            action_bar, text="回滚补丁", command=self.on_rollback_clicked
        )
        self.btn_rollback.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_recover = ttk.Button(
            action_bar,
            text="恢复未完成安装",
            command=self.on_recover_clicked,
            state=tk.DISABLED,
        )
        self.btn_recover.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_open_dir = ttk.Button(
            action_bar, text="打开游戏目录", command=self.open_game_dir
        )
        self.btn_open_dir.pack(side=tk.LEFT)

        # 日志文本框（嵌入右侧面板底部）
        ttk.Label(right_frame, text="运行日志:").grid(
            row=11, column=0, columnspan=3, sticky=tk.W, pady=(8, 2)
        )
        self.log_text = tk.Text(
            right_frame, height=5, state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.grid(row=12, column=0, columnspan=3, sticky=tk.NSEW, pady=(0, 2))
        right_frame.rowconfigure(12, weight=1)

        # 底部状态栏与进度条（P3-1 #5 & #8）
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.X, expand=False)

        self.progress = ttk.Progressbar(
            bottom_frame, orient=tk.HORIZONTAL, mode="determinate", maximum=100
        )
        self.progress.pack(fill=tk.X, pady=(0, 4))

        self.status_label = tk.Label(
            bottom_frame,
            textvariable=self.status_var,
            anchor=tk.W,
            fg="#1b5e20",
        )
        self.status_label.pack(fill=tk.X)

    def _set_inspect_text(self, text: str) -> None:
        self.inspect_text.configure(state=tk.NORMAL)
        self.inspect_text.delete("1.0", tk.END)
        self.inspect_text.insert("1.0", text)
        self.inspect_text.configure(state=tk.DISABLED)

    def _inspect_patch_async(
        self,
        raw_path: str,
        *,
        ask_exec_confirm: bool,
        on_ready: Callable[[bool], None],
    ) -> None:
        """在后台线程计算 SHA256，避免大补丁文件卡住界面；结果经队列回到 UI 线程。"""
        target = Path(raw_path)
        if target.is_dir():
            self._set_inspect_text(
                f"目录: {target.resolve()}\n类型: 补丁文件夹（安装时将递归写入目录下所有文件）"
            )
            self._inspected_key = (str(target.resolve()), "DIR")
            on_ready(True)
            return

        self._inspecting = True
        self._inspected_key = None
        self._set_inspect_text(f"正在读取并计算 SHA256: {target}\n大文件可能需要几秒钟…")

        def _worker() -> None:
            try:
                inspection = inspect_patch_file(target)
            except Exception as exc:
                self._ui_queue.put(("inspect_failed", str(exc), on_ready))
            else:
                self._ui_queue.put(
                    ("inspect_done", inspection, ask_exec_confirm, on_ready)
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_inspection(
        self, inspection: PatchFileInspection, *, ask_exec_confirm: bool
    ) -> bool:
        """展示核验详情；裸可执行文件在继续前需要用户二次确认。"""
        self._set_inspect_text(inspection.describe())
        key = (str(inspection.path), inspection.sha256)
        self._inspected_key = key

        if inspection.is_bare_executable and ask_exec_confirm:
            if self._confirmed_exec_key == key:
                return True
            ok = messagebox.askyesno(
                "可执行文件安全确认",
                "该文件是可执行文件，安装后会被游戏加载。\n"
                "确认来源可信再继续。\n\n"
                f"SHA256: {inspection.sha256}\n\n"
                "是否继续？",
            )
            if not ok:
                return False
            self._confirmed_exec_key = key
        return True

    def _set_status(self, text: str, *, is_error: bool = False) -> None:
        self.status_var.set(text)
        self.status_label.configure(fg="#c62828" if is_error else "#1b5e20")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (
            self.btn_scan,
            self.btn_add_game,
            self.btn_import_catalog,
            self.btn_export_catalog,
            self.btn_crawl,
            self.btn_portals,
            self.btn_dry_run,
            self.btn_apply,
            self.btn_rollback,
        ):
            btn.configure(state=state)
        # 恢复按钮只在「上次安装未完成」且空闲时可用，由状态刷新统一决定。
        self._refresh_install_state()

    def _poll_queue(self) -> None:
        """轮询后台事件，记录单个事件的错误并继续处理其余事件。"""
        self._poll_after_id = None
        try:
            while not self._closing:
                try:
                    event = self._ui_queue.get_nowait()
                except queue.Empty:
                    break

                kind = None
                try:
                    kind = event[0]
                    self._handle_queue_event(kind, event)
                except Exception:
                    logger.exception("处理后台界面事件失败，事件类型=%r", kind)
                    if not self._closing:
                        try:
                            self._set_status("界面更新失败，详情见 GUI 日志。", is_error=True)
                        except Exception:
                            logger.exception("无法显示后台界面事件的错误提示")
        finally:
            if not self._closing:
                try:
                    if self.root.winfo_exists():
                        self._poll_after_id = self.root.after(100, self._poll_queue)
                except tk.TclError:
                    logger.debug("Tk 窗口已不可用，停止后台队列轮询", exc_info=True)

    def _handle_queue_event(self, kind: str, event: tuple[Any, ...]) -> None:
        if kind in ("scan_done", "search_done", "success", "warning", "error"):
            self._set_busy(False)

        if kind == "inspect_done":
            self._inspecting = False
            inspection, ask_confirm, on_ready = event[1], event[2], event[3]
            try:
                ready = self._finish_inspection(
                    inspection, ask_exec_confirm=ask_confirm
                )
            except Exception:
                logger.exception("处理补丁文件核验结果失败")
                self._set_status("处理补丁文件核验结果失败，详情见日志。", is_error=True)
                on_ready(False)
                return
            if not ready:
                self._set_status("已取消导入未确认的可执行文件。", is_error=True)
            on_ready(ready)
            return
        if kind == "inspect_failed":
            self._inspecting = False
            self._inspected_key = None
            detail = str(event[1])
            self._set_inspect_text(f"无法读取本地文件信息: {detail}")
            self._set_status(
                f"无法读取补丁文件: {detail.splitlines()[0]}", is_error=True
            )
            event[2](False)
            return

        if kind == "progress":
            pct, msg = event[1], event[2]
            self.progress.configure(value=pct)
            self._set_status(msg, is_error=False)
        elif kind == "log":
            self._append_log(str(event[1]))
        elif kind == "scan_done":
            statuses, msg = event[1], event[2]
            self._statuses = statuses
            self.progress.configure(value=100)
            self._refresh_tree()
            self._set_status(msg, is_error=False)
            self._append_log(msg)
        elif kind == "search_done":
            candidates, query, failures = event[1], event[2], event[3]
            self.progress.configure(value=100)
            msg, failure_logs, warning_dialog = format_search_summary(
                query, len(candidates), failures
            )
            self._set_status(msg, is_error=bool(failures))
            self._append_log(msg)
            for line in failure_logs:
                self._append_log(line)
            if warning_dialog is not None:
                messagebox.showwarning("搜索结果不完整", warning_dialog)
            else:
                self._show_online_candidates_dialog(query, candidates)
        elif kind == "success":
            msg, rescan = event[1], event[2]
            self.progress.configure(value=100)
            self._set_status(msg, is_error=False)
            self._append_log(msg)
            messagebox.showinfo("操作完成", msg)
            if rescan and not self._closing:
                self.scan_games_async()
        elif kind == "warning":
            title, msg = event[1], event[2]
            self._set_status(f"提示: {msg.splitlines()[0]}", is_error=True)
            self._append_log(f"警告: {msg}")
            messagebox.showwarning(title, msg)
        elif kind == "error":
            err_msg = str(event[1])
            self._set_status(summarize_error_status(err_msg), is_error=True)
            self._append_log(f"错误: {err_msg}")
            messagebox.showerror("操作失败", err_msg)
        else:
            logger.warning("忽略未知的后台界面事件类型 %r", kind)

    def _on_search_key_release(self, _event: object) -> None:
        """顶部搜索框 200ms 防抖实时过滤（P3-1 #2）。"""
        if self._search_after_id is not None:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.root.after(200, self._refresh_tree)

    def _on_paste_shortcut(self, event: object) -> Optional[str]:
        """在非输入框焦点下按 Ctrl+V 直接把剪贴板路径粘贴到补丁源（P3-1 #3）。"""
        focused = self.root.focus_get()
        if isinstance(focused, (ttk.Entry, tk.Entry, tk.Text)):
            return None
        self.paste_patch_path_from_clipboard()
        return "break"

    def paste_patch_path_from_clipboard(self) -> None:
        """从系统剪贴板读取本地补丁路径并填入输入框（拒绝远程 URL）。"""
        try:
            raw = self.root.clipboard_get().strip().strip('"').strip("'")
        except tk.TclError:
            messagebox.showwarning("剪贴板为空", "剪贴板中没有可用的文本路径。")
            return
        if not raw:
            return
        if is_url(raw) or raw.lower().startswith(("http://", "https://")):
            messagebox.showwarning(
                "不支持远程 URL",
                "出于安全考虑，本工具不再支持从 URL 直接安装补丁。\n"
                "请先在浏览器下载到本地，再导入本地文件。",
            )
            return
        self.patch_source_var.set(raw)
        self.sha256_var.set("")
        if Path(raw).exists():
            def _on_inspected(confirmed: bool) -> None:
                if not confirmed:
                    self.patch_source_var.set("")

            self._inspect_patch_async(raw, ask_exec_confirm=True, on_ready=_on_inspected)
            self._set_status(f"已从剪贴板填入补丁路径，正在校验: {raw}", is_error=False)
        else:
            self._set_status(f"已从剪贴板填入补丁路径（文件不存在）: {raw}", is_error=True)

    def scan_games_async(self) -> None:
        """在后台线程扫描 Steam 库、手工登记库及可选的深度扫描目录（P3-1 #4）。"""
        if self._busy:
            return
        self._set_busy(True)
        self.progress.configure(value=10)
        include_manual = self.include_manual_var.get()
        deep_scan = self.deep_scan_var.get()
        self._set_status("正在后台扫描本机游戏目录，请稍候...", is_error=False)

        def _worker() -> None:
            warnings = set()
            worker_id = threading.get_ident()
            ui_queue = self._ui_queue

            class ScanWarnings(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    if record.thread == worker_id:
                        message = record.getMessage()
                        if message not in warnings:
                            warnings.add(message)
                            ui_queue.put(("log", f"扫描提示: {message}"))

            handler = ScanWarnings(logging.WARNING)
            logger.addHandler(handler)
            try:
                statuses = self.catalog.scan_games(
                    locator=self.locator,
                    only_installed=False,
                    include_manual=include_manual,
                    deep_scan=deep_scan,
                )
                # 同时将本机 Steam 库中已安装的其余游戏合并进候选视图，方便直接点选并在线搜索补丁
                known_dirs = {
                    s.game_dir.resolve() for s in statuses if s.game_dir is not None
                }
                for steam_item in self.locator.list_installed_steam_manifests():
                    gdir = Path(steam_item["game_dir"]).resolve()
                    if gdir in known_dirs:
                        continue
                    known_dirs.add(gdir)
                    patched = _is_game_patched(gdir)
                    statuses.append(
                        InstalledGameStatus(
                            entry=CatalogEntry(
                                id=f"steam_{steam_item['steam_app_id']}",
                                name=str(steam_item["name"]),
                                steam_app_id=int(steam_item["steam_app_id"]),
                                install_dir_name=str(steam_item["install_dir_name"]),
                                source="steam_local",
                            ),
                            installed=True,
                            game_dir=gdir,
                            patched=patched,
                        )
                    )
                installed_count = sum(1 for s in statuses if s.installed)
                msg = (
                    f"扫描完成：共 {len(statuses)} 款游戏条目，"
                    f"本机已安装 {installed_count} 款。"
                )
                if warnings:
                    msg += f"有 {len(warnings)} 条提示，详见运行日志。"
                self._ui_queue.put(("scan_done", statuses, msg))
            except Exception as exc:
                logger.exception("扫描游戏失败")
                self._ui_queue.put(("error", str(exc)))
            finally:
                logger.removeHandler(handler)

        t = threading.Thread(target=_worker, daemon=True)
        self._active_thread = t
        t.start()

    def _refresh_tree(self) -> None:
        self._search_after_id = None
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        rows = filter_game_statuses(
            self._statuses,
            keyword=self.search_var.get(),
            only_installed=self.only_installed_var.get(),
        )
        self._filtered_indices = []
        for idx, status, state_text in rows:
            entry = status.entry
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    entry.name,
                    state_text,
                    entry.patch_version,
                ),
            )
            self._filtered_indices.append(idx)

    def _on_select_row(self, _event: object) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        if idx >= len(self._statuses):
            return
        status = self._statuses[idx]
        entry = status.entry

        self.detail_name_var.set(entry.name)
        self.detail_exe_var.set(entry.executable_name or "-")
        self.detail_desc_var.set(entry.description or f"来源: {entry.source}")
        if status.game_dir:
            self.game_dir_var.set(str(status.game_dir))
            self._gui_config["last_game_dir"] = str(status.game_dir)
        # GUI 侧不再把清单的远程 URL 填进待安装输入框
        self.patch_source_var.set("")
        self._inspected_key = None
        self._set_inspect_text("尚未导入本地补丁文件。请先通过“选择补丁文件...”导入并核对 SHA256。")
        # 出于安全考虑，不自动从清单回填明文解压密码，切换条目时清空输入框。
        self.password_var.set("")
        if entry.archive_password:
            self._append_log(
                f"提示：清单条目「{entry.name}」包含密码字段，出于安全考虑未自动回填，请在“解压密码”栏手动输入。"
            )
        self.sha256_var.set(entry.expected_sha256 or "")
        self.target_subdir_var.set(entry.target_subdir or "")

    def _refresh_install_state(self) -> None:
        """同步显示当前游戏目录的补丁状态，并控制「恢复未完成安装」按钮是否可用。"""
        game_dir = self.game_dir_var.get().strip()
        if not game_dir or not Path(game_dir).is_dir():
            self.detail_state_var.set("补丁状态: -")
            self.btn_recover.configure(state=tk.DISABLED)
            return
        try:
            status = self.patcher.install_status(game_dir)
        except Exception as exc:
            logger.warning("读取补丁安装状态失败: %s", exc)
            self.detail_state_var.set(f"补丁状态: 读取失败（{exc}）")
            self.btn_recover.configure(state=tk.DISABLED)
            return

        self.detail_state_var.set(format_install_state_summary(status))
        self.btn_recover.configure(
            state=(
                tk.NORMAL
                if status.state == "incomplete" and not self._busy
                else tk.DISABLED
            )
        )

    def register_manual_game(self) -> None:
        """手工选择并登记非 Steam 游戏目录到 %APPDATA%\\galgame-patcher\\games.json（P2-1）。"""
        initial_dir = self.game_dir_var.get().strip() or str(
            self._gui_config.get("last_game_dir") or ""
        )
        directory = filedialog.askdirectory(
            title="选择要登记到手工库的 Galgame 目录",
            initialdir=initial_dir if initial_dir and Path(initial_dir).is_dir() else None,
        )
        if not directory:
            return
        try:
            reg = self.manual_locator.add_game(directory)
            self.game_dir_var.set(str(reg["dir"]))
            self._gui_config["last_game_dir"] = str(reg["dir"])
            self._save_gui_config()
            self._append_log(
                f"已登记本地游戏: {reg['name']} (主程序: {reg.get('executable') or '无'}) -> {reg['dir']}"
            )
            self.scan_games_async()
        except Exception as exc:
            logger.exception("登记本地游戏失败")
            self._set_status(f"登记失败: {exc}", is_error=True)
            messagebox.showerror("登记失败", str(exc))

    def import_catalog(self) -> None:
        path = filedialog.askopenfilename(
            title="导入游戏补丁清单 JSON",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            self.catalog = PatchCatalog.from_file(path)
            self._append_log(f"已导入清单: {path}")
            self.scan_games_async()
        except Exception as exc:
            logger.exception("导入清单失败")
            self._set_status(f"导入清单失败: {exc}", is_error=True)
            messagebox.showerror("导入失败", str(exc))

    def export_catalog(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出补丁清单 JSON",
            defaultextension=".json",
            initialfile="galgame_catalog.json",
            filetypes=[("JSON 文件", "*.json")],
        )
        if not path:
            return
        try:
            self.catalog.save_to_file(path)
            self._append_log(f"已导出清单到（不含明文解压密码）: {path}")
            messagebox.showinfo(
                "导出成功",
                f"已导出清单到:\n{path}\n\n出于安全考虑，导出文件不包含明文解压密码。",
            )
        except Exception as exc:
            logger.exception("导出清单失败")
            self._set_status(f"导出失败: {exc}", is_error=True)
            messagebox.showerror("导出失败", str(exc))

    def browse_game_dir(self) -> None:
        initial = self.game_dir_var.get().strip() or str(
            self._gui_config.get("last_game_dir") or ""
        )
        directory = filedialog.askdirectory(
            title="选择 Galgame 安装根目录",
            initialdir=initial if initial and Path(initial).is_dir() else None,
        )
        if directory:
            self.game_dir_var.set(directory)
            self._gui_config["last_game_dir"] = directory
            self._save_gui_config()

    def browse_patch_file(self) -> None:
        initial = str(self._gui_config.get("last_patch_dir") or "")
        path = filedialog.askopenfilename(
            title="选择补丁文件或压缩包",
            initialdir=initial if initial and Path(initial).is_dir() else None,
            filetypes=[
                (
                    "常见补丁与压缩包",
                    "*.zip *.7z *.rar *.tar.gz *.tgz *.xp3 *.arc *.pck *.pac *.ypf *.rpa",
                ),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self._confirmed_exec_key = None
            self._set_status(f"正在校验 {Path(path).name}…", is_error=False)

            def _on_inspected(confirmed: bool) -> None:
                if not confirmed:
                    self.patch_source_var.set("")
                    self._inspected_key = None
                    return
                self.patch_source_var.set(path)
                self.sha256_var.set("")
                self._gui_config["last_patch_dir"] = str(Path(path).parent)
                self._save_gui_config()
                self._set_status(
                    f"已检查本地补丁文件并显示 SHA256: {Path(path).name}",
                    is_error=False,
                )

            self._inspect_patch_async(path, ask_exec_confirm=True, on_ready=_on_inspected)

    def open_game_dir(self) -> None:
        game_dir = self.game_dir_var.get().strip()
        if not game_dir or not Path(game_dir).is_dir():
            messagebox.showwarning("提示", "请先填写或选择一个有效的游戏安装目录。")
            return
        if sys.platform == "win32":
            startfile = os.startfile
            startfile(game_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", game_dir])
        else:
            subprocess.Popen(["xdg-open", game_dir])

    def on_search_online_clicked(self) -> None:
        """自动在 VNDB / GitHub Releases / 指定网页爬取对应游戏的补丁链接。"""
        if self._busy:
            return
        selected = self.tree.selection()
        selected_entry: Optional[CatalogEntry] = None
        if selected:
            idx = int(selected[0])
            if idx < len(self._statuses):
                selected_entry = self._statuses[idx].entry
        default_query = build_default_search_query(
            selected_entry, self.game_dir_var.get()
        )

        query = self._prompt_search_query(default_query)
        if not query:
            return

        self._set_busy(True)
        self.progress.configure(value=20)
        self._set_status(f"正在从 VNDB / 官方补丁站 / PCGW / GitHub 检索 '{query}' 的链接...", is_error=False)

        def _worker() -> None:
            try:
                failures: list[tuple[str, str]] = []
                candidates = discover_patches_online(
                    query, on_failure=lambda source, detail: failures.append((source, detail))
                )
                self._ui_queue.put(("search_done", candidates, query, failures))
            except Exception as exc:
                logger.exception("在线搜索补丁失败")
                self._ui_queue.put(("error", f"在线搜索补丁失败: {exc}"))

        t = threading.Thread(target=_worker, daemon=True)
        self._active_thread = t
        t.start()

    def _prompt_search_query(self, initial: str) -> str:
        dialog = tk.Toplevel(self.root)
        dialog.title("自动上网搜索补丁")
        dialog.geometry("460x160")
        dialog.transient(self.root)
        dialog.grab_set()

        result_holder = {"value": ""}
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="输入游戏名称（英文/日文/中文）或要爬取的补丁网页 URL：",
        ).pack(anchor=tk.W, pady=(0, 6))
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(frame, textvariable=var)
        entry.pack(fill=tk.X, pady=(0, 12))
        entry.focus_set()

        btn_row = ttk.Frame(frame)
        btn_row.pack(anchor=tk.E)

        def _ok(*_args: object) -> None:
            result_holder["value"] = var.get().strip()
            dialog.destroy()

        entry.bind("<Return>", _ok)
        ttk.Button(btn_row, text="开始爬取", command=_ok).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="取消", command=dialog.destroy).pack(side=tk.LEFT)

        self.root.wait_window(dialog)
        return result_holder["value"]

    def _show_online_candidates_dialog(
        self, query: str, candidates: list[OnlinePatchCandidate]
    ) -> None:
        if not candidates:
            messagebox.showinfo(
                "未找到结果",
                f"未在公开数据库搜索到与 '{query}' 相关的补丁链接，请尝试使用游戏的英文/罗马音原名搜索。",
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"在线补丁搜索结果（仅作参考链接） - {query} (共 {len(candidates)} 条)")
        dialog.geometry("940x480")
        dialog.transient(self.root)

        top_warn = tk.Label(
            dialog,
            text=(
                "搜索结果为第三方链接，本工具不下载也不保证其安全。"
                "双击查阅来源页面，按对应发行版的说明安装。"
            ),
            fg="#8b0000",
            anchor=tk.W,
            padx=10,
            pady=6,
        )
        top_warn.pack(fill=tk.X)

        frame = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        frame.pack(fill=tk.BOTH, expand=True)

        cols = ("trusted", "title", "source", "type", "lang", "url")
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tree.heading("trusted", text="白名单")
        tree.heading("title", text="标题")
        tree.heading("source", text="来源")
        tree.heading("type", text="类型")
        tree.heading("lang", text="语言")
        tree.heading("url", text="网址")

        tree.column("trusted", width=85, anchor=tk.CENTER)
        tree.column("title", width=230, anchor=tk.W)
        tree.column("source", width=105, anchor=tk.CENTER)
        tree.column("type", width=75, anchor=tk.CENTER)
        tree.column("lang", width=75, anchor=tk.CENTER)
        tree.column("url", width=320, anchor=tk.W)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for idx, cand in enumerate(candidates):
            tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=format_candidate_row(cand),
            )

        btn_bar = ttk.Frame(dialog, padding=(10, 4, 10, 4))
        btn_bar.pack(fill=tk.X)

        exec_warn_var = tk.StringVar(value="")
        exec_warn_label = tk.Label(
            dialog,
            textvariable=exec_warn_var,
            fg="#c62828",
            anchor=tk.W,
            padx=10,
            justify=tk.LEFT,
            wraplength=880,
        )
        exec_warn_label.pack(fill=tk.X, pady=(0, 8))

        def _on_select_cand(_event: object) -> None:
            sel = tree.selection()
            if not sel:
                exec_warn_var.set("")
                return
            cand = candidates[int(sel[0])]
            exec_warn_var.set(format_candidate_detail(cand))

        def _open_browser(_event: object = None) -> None:
            sel = tree.selection()
            if not sel:
                return
            cand = candidates[int(sel[0])]
            webbrowser.open(cand.url)

        def _copy_link() -> None:
            sel = tree.selection()
            if not sel:
                return
            cand = candidates[int(sel[0])]
            self.root.clipboard_clear()
            self.root.clipboard_append(cand.url)
            self._set_status(f"已复制链接到剪贴板: {cand.url}", is_error=False)

        tree.bind("<<TreeviewSelect>>", _on_select_cand)
        tree.bind("<Double-1>", _open_browser)
        ttk.Button(btn_bar, text="在浏览器打开该页", command=_open_browser).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_bar, text="复制链接", command=_copy_link).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_bar, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def open_portals_dialog(self) -> None:
        """展示发行商和社区资源及自定义站点配置入口。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("补丁站点导航")
        dialog.geometry("820x440")
        dialog.transient(self.root)

        tip = ttk.Label(
            dialog,
            text="双击或点击“在浏览器打开”查阅发行商或社区的说明。部分站点可能限制自动请求。",
        )
        tip.pack(anchor=tk.W, padx=10, pady=(8, 4))

        frame = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        frame.pack(fill=tk.BOTH, expand=True)

        cols = ("name", "mode", "url", "desc")
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        tree.heading("name", text="网站名称")
        tree.heading("mode", text="集成方式")
        tree.heading("url", text="网址")
        tree.heading("desc", text="说明")

        tree.column("name", width=190, anchor=tk.W)
        tree.column("mode", width=100, anchor=tk.CENTER)
        tree.column("url", width=230, anchor=tk.W)
        tree.column("desc", width=260, anchor=tk.W)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        portals = list(OFFICIAL_PATCH_PORTALS)
        for idx, page_url in enumerate(load_user_sources().get("extra_pages", []), start=1):
            portals.append(
                {
                    "name": f"自定义站点 #{idx}",
                    "url": page_url,
                    "mode": "自定义检索",
                    "desc": "来自 sources.json extra_pages",
                }
            )

        for idx, item in enumerate(portals):
            tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(item["name"], item["mode"], item["url"], item["desc"]),
            )

        btn_bar = ttk.Frame(dialog, padding=(10, 4, 10, 10))
        btn_bar.pack(fill=tk.X)

        def _open_selected(_event: object = None) -> None:
            sel = tree.selection()
            if not sel:
                return
            webbrowser.open(portals[int(sel[0])]["url"])

        def _open_sources_config() -> None:
            cfg_path = get_user_sources_path()
            if not cfg_path.is_file():
                save_user_sources()
            if sys.platform == "win32":
                startfile = os.startfile
                startfile(str(cfg_path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(cfg_path)])
            else:
                subprocess.Popen(["xdg-open", str(cfg_path)])

        tree.bind("<Double-1>", _open_selected)
        ttk.Button(btn_bar, text="在浏览器打开", command=_open_selected).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(
            btn_bar,
            text="编辑自定义站点配置 (sources.json)",
            command=_open_sources_config,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_bar, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def _run_patch_task(self, *, dry_run: bool) -> None:
        if self._busy or self._inspecting:
            return
        game_dir = self.game_dir_var.get().strip()
        patch_source = self.patch_source_var.get().strip()
        validation_warning = validate_patch_task_inputs(game_dir, patch_source)
        if validation_warning is not None:
            messagebox.showwarning(*validation_warning)
            return

        password = self.password_var.get().strip() or None
        expected_sha256 = self.sha256_var.get().strip() or None
        target_subdir = self.target_subdir_var.get().strip()
        backup = self.backup_var.get()

        def _after_inspection(confirmed: bool) -> None:
            if not confirmed:
                return
            # 危险操作二次确认（P3-1 #6）
            if not dry_run:
                confirm_msg = format_apply_confirm_message(
                    game_dir, backup=backup, expected_sha256=expected_sha256
                )
                if not messagebox.askyesno("确认安装补丁", confirm_msg):
                    return
            self._start_patch_task(
                dry_run=dry_run,
                game_dir=game_dir,
                patch_source=patch_source,
                password=password,
                expected_sha256=expected_sha256,
                target_subdir=target_subdir,
                backup=backup,
            )

        # SHA256 在后台线程计算，避免大文件冻结界面；完成后再弹确认框。
        self._set_status("正在校验补丁文件并计算 SHA256…", is_error=False)
        self._inspect_patch_async(
            patch_source,
            ask_exec_confirm=not dry_run,
            on_ready=_after_inspection,
        )

    def _start_patch_task(
        self,
        *,
        dry_run: bool,
        game_dir: str,
        patch_source: str,
        password: Optional[str],
        expected_sha256: Optional[str],
        target_subdir: str,
        backup: bool,
    ) -> None:
        if self._busy:
            self._set_status("已有操作正在进行，请稍后再试。", is_error=True)
            return
        self._set_busy(True)
        self._install_active = True
        self.progress.configure(value=0)
        start_msg = "正在预览补丁变更..." if dry_run else "正在安装补丁，请稍候..."
        self._set_status(start_msg, is_error=False)
        self._append_log(start_msg)

        start_time = time.monotonic()

        def _progress_cb(downloaded: int, total: Optional[int]) -> None:
            pct, msg = compute_download_progress(
                downloaded, total, time.monotonic() - start_time
            )
            self._ui_queue.put(("progress", pct, msg))

        def _worker() -> None:
            try:
                result = self.patcher.apply(
                    patch_source=patch_source,
                    game_dir=game_dir,
                    target_subdir=target_subdir,
                    backup=backup,
                    expected_sha256=expected_sha256,
                    password=password,
                    progress_callback=_progress_cb,
                    dry_run=dry_run,
                )
                if not result.success:
                    self._ui_queue.put(("error", f"{result.message}\n{result.failure_details()}".rstrip()))
                else:
                    self._ui_queue.put(("success", result.message, not dry_run))
            except Exception as exc:
                kind, title, detail = format_patch_exception(exc, operation="apply")
                if isinstance(exc, (GameRunningError, ConcurrencyError)):
                    logger.warning("补丁操作受阻 (%s): %s", type(exc).__name__, exc)
                    self._ui_queue.put((kind, title, detail))
                else:
                    logger.exception("补丁操作异常")
                    self._ui_queue.put((kind, detail))
            finally:
                self._install_active = False

        t = threading.Thread(target=_worker, daemon=True)
        self._active_thread = t
        t.start()

    def on_dry_run_clicked(self) -> None:
        """执行仅预览 Dry-Run 模式（P3-2 #3）。"""
        self._run_patch_task(dry_run=True)

    def on_apply_clicked(self) -> None:
        """执行正式补丁安装。"""
        self._run_patch_task(dry_run=False)

    def on_rollback_clicked(self) -> None:
        if self._busy:
            return
        game_dir = self.game_dir_var.get().strip()
        if not game_dir:
            messagebox.showwarning("缺少参数", "请先指定需要还原的游戏目录。")
            return

        mode = "keep-patches" if self.undo_latest_var.get() else "full"
        if mode == "keep-patches":
            try:
                history = self.patcher.rollback_history(game_dir)
            except Exception as exc:
                messagebox.showerror("无法读取安装记录", str(exc))
                return
            if not history:
                messagebox.showwarning(
                    "无法逐次撤回",
                    "当前游戏目录没有可逐次撤回的安装记录。\n"
                    "请取消勾选「回滚时仅撤回最近一次安装」，改用完整回滚。",
                )
                return
        action = "撤回最近一次补丁安装" if mode == "keep-patches" else "完整还原备份"
        if not messagebox.askyesno("确认回滚", f"确定要对以下游戏{action}吗？\n{game_dir}"):
            return

        self._start_rollback_task(
            game_dir, mode=mode, status_text="正在还原备份文件，请稍候..."
        )

    def on_recover_clicked(self) -> None:
        """恢复上次中断的安装：完整回滚残留改动。"""
        if self._busy:
            return
        game_dir = self.game_dir_var.get().strip()
        if not game_dir:
            messagebox.showwarning("缺少参数", "请先指定需要恢复的游戏目录。")
            return
        if not messagebox.askyesno(
            "确认恢复",
            f"将完整回滚以下游戏目录中上次未完成的补丁安装：\n{game_dir}\n\n"
            "已经写入的新增文件会被删除，被覆盖的文件会从备份还原。",
        ):
            return

        self._start_rollback_task(
            game_dir,
            mode="full",
            status_text="正在恢复未完成的安装，请稍候...",
            recover=True,
        )

    def _start_rollback_task(
        self,
        game_dir: str,
        *,
        mode: str,
        status_text: str,
        recover: bool = False,
    ) -> None:
        self._set_busy(True)
        self._install_active = True
        self.progress.configure(value=10)
        self._set_status(status_text, is_error=False)

        def _worker() -> None:
            try:
                if recover:
                    result = self.patcher.recover_incomplete_install(game_dir)
                else:
                    result = self.patcher.rollback(game_dir, mode=mode)
                if result.success:
                    self._ui_queue.put(("success", result.message, True))
                else:
                    self._ui_queue.put(("error", f"{result.message}\n{result.failure_details()}".rstrip()))
            except Exception as exc:
                kind, title, detail = format_patch_exception(exc, operation="rollback")
                if isinstance(exc, (GameRunningError, ConcurrencyError)):
                    logger.warning("回滚受阻 (%s): %s", type(exc).__name__, exc)
                    self._ui_queue.put((kind, title, detail))
                else:
                    logger.exception("回滚异常")
                    self._ui_queue.put((kind, detail))
            finally:
                self._install_active = False

        t = threading.Thread(target=_worker, daemon=True)
        self._active_thread = t
        t.start()


def launch_gui() -> int:
    """启动 Galgame 一键补丁工具图形界面。"""
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    GalgamePatcherGUI(root)
    root.mainloop()
    return 0
