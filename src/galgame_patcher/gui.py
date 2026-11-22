"""基于 Tkinter / ttk 的 Galgame 一键补丁桌面图形界面。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .catalog import PatchCatalog
from .locator import SteamLocator
from .models import InstalledGameStatus
from .patcher import GalgamePatcher


def _enable_windows_dpi_awareness() -> None:
    """在 Windows 下开启高 DPI 适配，避免界面模糊。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class GalgamePatcherGUI:
    """Galgame 补丁安装器桌面可视化窗口。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Galgame 一键补丁工具 v0.2.0")
        self.root.geometry("940x660")
        self.root.minsize(820, 560)

        self.locator = SteamLocator()
        self.patcher = GalgamePatcher(locator=self.locator)
        self.catalog = PatchCatalog.default()
        self._statuses: list[InstalledGameStatus] = []
        self._busy = False

        self.only_installed_var = tk.BooleanVar(value=False)
        self.backup_var = tk.BooleanVar(value=True)
        self.game_dir_var = tk.StringVar()
        self.patch_source_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.sha256_var = tk.StringVar()
        self.target_subdir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪：可点击上方“扫描本地 Steam 游戏”或手动选择游戏目录与补丁。")

        self._build_ui()
        self.scan_games()

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 顶部工具栏
        toolbar = ttk.Frame(main_frame)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        self.btn_scan = ttk.Button(
            toolbar, text="扫描本地 Steam 游戏", command=self.scan_games
        )
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_import_catalog = ttk.Button(
            toolbar, text="导入清单 JSON...", command=self.import_catalog
        )
        self.btn_import_catalog.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_export_catalog = ttk.Button(
            toolbar, text="导出示例清单...", command=self.export_catalog
        )
        self.btn_export_catalog.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Checkbutton(
            toolbar,
            text="仅显示本机已安装游戏",
            variable=self.only_installed_var,
            command=self._refresh_tree,
        ).pack(side=tk.LEFT)

        # 中部游戏列表
        list_frame = ttk.LabelFrame(main_frame, text="游戏清单与本机安装状态", padding=6)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        columns = ("name", "app_id", "installed", "patched", "version", "game_dir")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("name", text="游戏名称")
        self.tree.heading("app_id", text="Steam AppID")
        self.tree.heading("installed", text="安装状态")
        self.tree.heading("patched", text="补丁状态")
        self.tree.heading("version", text="版本")
        self.tree.heading("game_dir", text="游戏路径")

        self.tree.column("name", width=220, anchor=tk.W)
        self.tree.column("app_id", width=95, anchor=tk.CENTER)
        self.tree.column("installed", width=85, anchor=tk.CENTER)
        self.tree.column("patched", width=85, anchor=tk.CENTER)
        self.tree.column("version", width=65, anchor=tk.CENTER)
        self.tree.column("game_dir", width=330, anchor=tk.W)

        y_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_row)

        # 下方操作表单
        form_frame = ttk.LabelFrame(main_frame, text="补丁安装与还原配置", padding=10)
        form_frame.pack(fill=tk.X, pady=(0, 8))
        form_frame.columnconfigure(1, weight=1)

        ttk.Label(form_frame, text="游戏安装目录:").grid(
            row=0, column=0, sticky=tk.W, pady=4, padx=(0, 8)
        )
        ttk.Entry(form_frame, textvariable=self.game_dir_var).grid(
            row=0, column=1, sticky=tk.EW, pady=4
        )
        ttk.Button(form_frame, text="浏览目录...", command=self.browse_game_dir).grid(
            row=0, column=2, sticky=tk.W, padx=(6, 0), pady=4
        )

        ttk.Label(form_frame, text="补丁文件 / 下载链接:").grid(
            row=1, column=0, sticky=tk.W, pady=4, padx=(0, 8)
        )
        ttk.Entry(form_frame, textvariable=self.patch_source_var).grid(
            row=1, column=1, sticky=tk.EW, pady=4
        )
        ttk.Button(
            form_frame, text="选择本地补丁...", command=self.browse_patch_file
        ).grid(row=1, column=2, sticky=tk.W, padx=(6, 0), pady=4)

        options_row = ttk.Frame(form_frame)
        options_row.grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=4)
        options_row.columnconfigure(1, weight=1)
        options_row.columnconfigure(3, weight=1)

        ttk.Label(options_row, text="解压密码 (留空自动尝试):").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6)
        )
        ttk.Entry(options_row, textvariable=self.password_var, width=20).grid(
            row=0, column=1, sticky=tk.EW, padx=(0, 12)
        )

        ttk.Label(options_row, text="预期 SHA256 (可选):").grid(
            row=0, column=2, sticky=tk.W, padx=(0, 6)
        )
        ttk.Entry(options_row, textvariable=self.sha256_var).grid(
            row=0, column=3, sticky=tk.EW, padx=(0, 12)
        )

        ttk.Checkbutton(
            options_row, text="覆盖前自动备份原文件", variable=self.backup_var
        ).grid(row=0, column=4, sticky=tk.W)

        # 动作按钮栏
        action_bar = ttk.Frame(form_frame)
        action_bar.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        self.btn_apply = ttk.Button(
            action_bar, text="一键安装补丁", command=self.on_apply_clicked
        )
        self.btn_apply.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_rollback = ttk.Button(
            action_bar, text="一键卸载补丁 (还原备份)", command=self.on_rollback_clicked
        )
        self.btn_rollback.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_open_dir = ttk.Button(
            action_bar, text="打开游戏文件夹", command=self.open_game_dir
        )
        self.btn_open_dir.pack(side=tk.LEFT)

        # 底部状态与日志
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.BOTH, expand=False)

        self.progress = ttk.Progressbar(
            bottom_frame, orient=tk.HORIZONTAL, mode="determinate", maximum=100
        )
        self.progress.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(bottom_frame, textvariable=self.status_var).pack(
            anchor=tk.W, pady=(0, 4)
        )

        self.log_text = tk.Text(
            bottom_frame, height=6, state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

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
            self.btn_import_catalog,
            self.btn_export_catalog,
            self.btn_apply,
            self.btn_rollback,
        ):
            btn.configure(state=state)

    def scan_games(self) -> None:
        """扫描本机 Steam 库中的清单游戏并更新列表。"""
        self._statuses = self.catalog.scan_games(locator=self.locator)
        self._refresh_tree()
        installed_count = sum(1 for s in self._statuses if s.installed)
        msg = f"扫描完成：清单共 {len(self._statuses)} 款游戏，本机已安装 {installed_count} 款。"
        self.status_var.set(msg)
        self._append_log(msg)

    def _refresh_tree(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        only_installed = self.only_installed_var.get()
        for idx, status in enumerate(self._statuses):
            if only_installed and not status.installed:
                continue
            entry = status.entry
            app_id_text = str(entry.steam_app_id) if entry.steam_app_id else "-"
            installed_text = "已安装" if status.installed else "未检测到"
            patched_text = (
                "已打补丁" if status.patched else ("未打补丁" if status.installed else "-")
            )
            dir_text = str(status.game_dir) if status.game_dir else ""
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    entry.name,
                    app_id_text,
                    installed_text,
                    patched_text,
                    entry.patch_version,
                    dir_text,
                ),
            )

    def _on_select_row(self, _event: object) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        status = self._statuses[idx]
        entry = status.entry

        if status.game_dir:
            self.game_dir_var.set(str(status.game_dir))
        self.patch_source_var.set(entry.patch_url or "")
        self.password_var.set(entry.archive_password or "")
        self.sha256_var.set(entry.expected_sha256 or "")
        self.target_subdir_var.set(entry.target_subdir or "")

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
            self.scan_games()
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def export_catalog(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出示例补丁清单 JSON",
            defaultextension=".json",
            initialfile="galgame_catalog.json",
            filetypes=[("JSON 文件", "*.json")],
        )
        if not path:
            return
        try:
            self.catalog.save_to_file(path)
            self._append_log(f"已导出清单模板到: {path}")
            messagebox.showinfo("导出成功", f"已导出清单到:\n{path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def browse_game_dir(self) -> None:
        directory = filedialog.askdirectory(title="选择 Galgame 安装根目录")
        if directory:
            self.game_dir_var.set(directory)

    def browse_patch_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择补丁文件或压缩包",
            filetypes=[
                (
                    "常见补丁与压缩包",
                    "*.zip *.7z *.rar *.tar.gz *.tgz *.xp3 *.arc *.pck *.pac *.ypf *.rpa",
                ),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.patch_source_var.set(path)
            self.sha256_var.set("")

    def open_game_dir(self) -> None:
        game_dir = self.game_dir_var.get().strip()
        if not game_dir or not Path(game_dir).is_dir():
            messagebox.showwarning("提示", "请先填写或选择一个有效的游戏安装目录。")
            return
        if sys.platform == "win32":
            os.startfile(game_dir)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", game_dir])
        else:
            subprocess.Popen(["xdg-open", game_dir])

    def _run_background(
        self,
        worker: Callable[[], str],
        *,
        start_msg: str,
    ) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.progress.configure(value=0)
        self.status_var.set(start_msg)
        self._append_log(start_msg)

        def _task() -> None:
            try:
                result_msg = worker()
                self.root.after(0, lambda: self._on_worker_success(result_msg))
            except Exception as exc:
                err_msg = str(exc)
                self.root.after(0, lambda: self._on_worker_error(err_msg))

        threading.Thread(target=_task, daemon=True).start()

    def _on_worker_success(self, msg: str) -> None:
        self.progress.configure(value=100)
        self._set_busy(False)
        self.status_var.set(msg)
        self._append_log(msg)
        self.scan_games()
        messagebox.showinfo("操作完成", msg)

    def _on_worker_error(self, err_msg: str) -> None:
        self._set_busy(False)
        self.status_var.set(f"操作失败: {err_msg}")
        self._append_log(f"错误: {err_msg}")
        messagebox.showerror("操作失败", err_msg)

    def on_apply_clicked(self) -> None:
        game_dir = self.game_dir_var.get().strip()
        patch_source = self.patch_source_var.get().strip()
        if not game_dir:
            messagebox.showwarning("缺少参数", "请先指定或扫描得到游戏安装目录。")
            return
        if not patch_source:
            messagebox.showwarning(
                "缺少补丁源", "请先选择本地补丁文件，或填写补丁下载链接 (URL)。"
            )
            return

        password = self.password_var.get().strip() or None
        expected_sha256 = self.sha256_var.get().strip() or None
        target_subdir = self.target_subdir_var.get().strip()
        backup = self.backup_var.get()

        def _progress_cb(downloaded: int, total: Optional[int]) -> None:
            if total and total > 0:
                pct = min(99.0, downloaded * 100.0 / total)
                self.root.after(
                    0,
                    lambda p=pct, d=downloaded, t=total: (
                        self.progress.configure(value=p),
                        self.status_var.set(f"正在下载补丁: {p:.1f}% ({d}/{t} 字节)"),
                    ),
                )

        def _worker() -> str:
            result = self.patcher.apply(
                patch_source=patch_source,
                game_dir=game_dir,
                target_subdir=target_subdir,
                backup=backup,
                expected_sha256=expected_sha256,
                password=password,
                progress_callback=_progress_cb,
            )
            return result.message

        self._run_background(_worker, start_msg="正在安装补丁，请稍候...")

    def on_rollback_clicked(self) -> None:
        game_dir = self.game_dir_var.get().strip()
        if not game_dir:
            messagebox.showwarning("缺少参数", "请先指定需要还原的游戏目录。")
            return

        if not messagebox.askyesno(
            "确认卸载补丁",
            f"确定要从备份还原以下游戏目录吗？\n{game_dir}",
        ):
            return

        def _worker() -> str:
            result = self.patcher.rollback(game_dir)
            return result.message

        self._run_background(_worker, start_msg="正在还原备份文件，请稍候...")


def launch_gui() -> int:
    """启动 Galgame 一键补丁工具图形界面。"""
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    GalgamePatcherGUI(root)
    root.mainloop()
    return 0
