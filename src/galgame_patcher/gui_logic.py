"""GUI 可测试纯逻辑模块（无 tkinter 依赖）：结果过滤、进度计算、状态与错误文案格式化。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from .downloader import is_url
from .models import (
    CatalogEntry,
    ConcurrencyError,
    GameRunningError,
    InstalledGameStatus,
    InstallStatus,
    OnlinePatchCandidate,
    format_size_bytes,
)


def format_transfer_speed(bytes_per_sec: float) -> str:
    """将字节每秒速率格式化为可读速度字符串。"""
    rate = max(0.0, float(bytes_per_sec))
    if rate >= 1024 * 1024:
        return f"{rate / (1024 * 1024):.2f} MB/s"
    if rate >= 1024:
        return f"{rate / 1024:.1f} KB/s"
    return f"{rate:.0f} B/s"


def compute_download_progress(
    downloaded: int, total: Optional[int], elapsed_seconds: float
) -> tuple[float, str]:
    """计算下载进度条百分比与状态提示文案。"""
    safe_downloaded = max(0, int(downloaded))
    elapsed = max(0.001, float(elapsed_seconds))
    speed_str = format_transfer_speed(safe_downloaded / elapsed)
    if total is not None and total > 0:
        pct = min(99.0, safe_downloaded * 100.0 / total)
        msg = f"正在下载补丁: {pct:.1f}% ({safe_downloaded}/{total} 字节, {speed_str})"
        return pct, msg
    msg = f"正在下载补丁: {safe_downloaded} 字节 ({speed_str})"
    return 50.0, msg


def format_game_status_text(status: InstalledGameStatus) -> str:
    """将游戏安装与补丁状态格式化为左侧列表状态列文案。"""
    if not status.installed:
        return "未安装"
    if status.patched is None:
        return "已安装 / 补丁状态待确认"
    if status.patched:
        return "已安装 / 已打补丁"
    return "已安装 / 未打补丁"


def filter_game_statuses(
    statuses: Sequence[InstalledGameStatus],
    *,
    keyword: str = "",
    only_installed: bool = False,
) -> list[tuple[int, InstalledGameStatus, str]]:
    """按安装筛选与关键字过滤游戏状态列表，返回 (原索引, 状态对象, 状态文本) 列表。"""
    kw = keyword.strip().lower()
    matched: list[tuple[int, InstalledGameStatus, str]] = []
    for idx, status in enumerate(statuses):
        if only_installed and not status.installed:
            continue
        entry = status.entry
        if kw:
            haystack = (
                f"{entry.name} {entry.id} {entry.steam_app_id or ''} "
                f"{entry.install_dir_name or ''} {status.game_dir or ''}"
            ).lower()
            if kw not in haystack:
                continue
        matched.append((idx, status, format_game_status_text(status)))
    return matched


def format_install_state_summary(status: InstallStatus) -> str:
    """将当前游戏目录的 InstallStatus 格式化为右侧详情面板摘要文案。"""
    text = f"补丁状态: {status.state_label}"
    if status.latest_install:
        applied = f"（{status.applied_at}）" if status.applied_at else ""
        text += f" — {status.latest_install}{applied}"
    if status.backup_size_bytes > 0:
        text += f"（备份体积 {format_size_bytes(status.backup_size_bytes)}）"
    if status.state == "incomplete":
        text += "，可点击「恢复未完成安装」回滚残留改动"
    return text


def build_default_search_query(
    selected_entry: Optional[CatalogEntry], game_dir: str
) -> str:
    """根据当前选中的清单条目或游戏目录推断在线搜索框默认关键词。"""
    if selected_entry is not None and selected_entry.id != "example_game":
        preferred = (selected_entry.install_dir_name or selected_entry.name or "").strip()
        if preferred:
            return preferred
    cleaned_dir = game_dir.strip()
    if cleaned_dir:
        return Path(cleaned_dir).name
    return ""


def format_candidate_row(
    cand: OnlinePatchCandidate,
) -> tuple[str, str, str, str, str, str]:
    """将在线候选条目格式化为搜索结果对话框表格行数据。"""
    trust_text = "✔ 白名单" if cand.trusted else "未审核"
    kind_text = "直链包" if cand.is_direct_download else "发布网页"
    if cand.manual_install:
        kind_text = "手动安装"
    return (
        trust_text,
        cand.title,
        cand.source_site,
        kind_text,
        cand.language or "-",
        cand.url,
    )


def format_candidate_detail(cand: OnlinePatchCandidate) -> str:
    """生成在线候选结果底部说明或可执行文件警告文案。"""
    if cand.bare_executable:
        return "⚠ 该链接指向可执行文件，请谨慎下载"
    return "\n".join(filter(None, (cand.description, cand.trust_reason)))


def summarize_error_status(err_msg: str) -> str:
    """提取多行错误消息的首行作为状态栏单行摘要。"""
    summary = err_msg.splitlines()[0].strip() if err_msg and err_msg.strip() else "未知错误"
    return f"错误: {summary}"


def format_patch_exception(
    exc: Exception, *, operation: str = "apply"
) -> tuple[str, str, str]:
    """将安装/回滚异常映射为 (队列事件类型, 弹窗标题, 错误正文)。"""
    is_rollback = operation == "rollback"
    if isinstance(exc, GameRunningError):
        title = "文件被占用" if is_rollback else "游戏正在运行或文件被占用"
        return ("warning", title, str(exc))
    if isinstance(exc, ConcurrencyError):
        title = "并发冲突" if is_rollback else "另一个实例正在运行"
        return ("warning", title, str(exc))
    return ("error", "操作失败", str(exc))


def format_search_summary(
    query: str,
    candidate_count: int,
    failures: Sequence[tuple[str, str]],
) -> tuple[str, list[str], Optional[str]]:
    """生成在线搜索完成后的状态摘要、失败日志列表以及无结果时的弹窗提示。"""
    msg = f"在线搜索 '{query}' 完成：找到 {candidate_count} 条候选补丁。"
    sources = "、".join(dict.fromkeys(source for source, _ in failures))
    if sources:
        msg += f" 来源未完成：{sources}。"
    failure_logs = [f"来源未完成：{source}（{detail}）" for source, detail in failures]
    warning_dialog: Optional[str] = None
    if failures and candidate_count == 0:
        warning_dialog = f"暂无候选结果。以下来源未完成搜索：{sources}。详情见运行日志。"
    return msg, failure_logs, warning_dialog


def validate_patch_task_inputs(
    game_dir: str, patch_source: str
) -> Optional[tuple[str, str]]:
    """校验 GUI 安装/预览任务输入，不合法时返回 (警告标题, 提示文案)，合法返回 None。"""
    if not game_dir.strip():
        return ("缺少参数", "请先指定或扫描得到游戏安装目录。")
    cleaned_source = patch_source.strip()
    if not cleaned_source:
        return ("缺少补丁源", "请先通过『选择补丁文件...』导入本地补丁文件。")
    if is_url(cleaned_source) or cleaned_source.lower().startswith(("http://", "https://")):
        return (
            "已禁止从 URL 直接安装",
            "出于安全考虑，本工具不再支持从 URL 直接安装补丁。\n"
            "请用『自动上网搜索』拿到链接，在浏览器下载后，\n"
            "用『选择补丁文件...』导入本地文件。",
        )
    return None


def format_apply_confirm_message(
    game_dir: str, *, backup: bool, expected_sha256: Optional[str]
) -> str:
    """生成正式安装前的二次确认提示文案。"""
    backup_note = "已备份至 .galpatch_backup" if backup else "注意：已取消勾选自动备份"
    hash_note = (
        ""
        if (expected_sha256 or "").strip()
        else "\n未填写发布者的 SHA256，请确认文件来自可信来源。"
    )
    return (
        f"即将把补丁安装到游戏目录:\n{game_dir}\n\n"
        f"将覆盖同名文件（{backup_note}）。{hash_note}\n确定继续吗？"
    )
