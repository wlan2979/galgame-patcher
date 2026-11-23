"""多格式归档解压模块，支持 .zip / .7z / .rar / .tar* 及安全路径前置校验。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

from .models import COMMON_ARCHIVE_EXTENSIONS

logger = logging.getLogger("galgame_patcher")

_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")


def is_supported_archive(path: Path) -> bool:
    """判断路径文件名是否为受支持的补丁压缩包格式。"""
    lower_name = path.name.lower()
    return any(lower_name.endswith(suffix) for suffix in COMMON_ARCHIVE_EXTENSIONS)


def _is_zip_archive(path: Path) -> bool:
    return path.suffix.lower() == ".zip"


def _is_7z_archive(path: Path) -> bool:
    return path.suffix.lower() == ".7z"


def _is_rar_archive(path: Path) -> bool:
    return path.suffix.lower() == ".rar"


def _is_tar_archive(path: Path) -> bool:
    lower_name = path.name.lower()
    return any(lower_name.endswith(suffix) for suffix in _TAR_SUFFIXES)


def resolve_safe_member_path(base_dir: Path, member_name: str) -> Path:
    """校验归档成员路径，防止路径遍历（Zip Slip）越界写入。"""
    normalized = member_name.replace("\\", "/")
    pure = PurePosixPath(normalized.lstrip("/"))
    if (
        member_name.startswith(("/", "\\"))
        or ".." in pure.parts
        or (len(pure.parts) > 0 and ":" in pure.parts[0])
    ):
        raise ValueError(f"检测到不安全的压缩包路径: {member_name!r}")

    resolved_base = base_dir.resolve()
    target = (resolved_base / Path(*pure.parts)).resolve()
    try:
        target.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"压缩包成员路径越界: {member_name!r}") from exc
    return target


def build_password_list(
    password: Optional[str] = None,
    password_candidates: Optional[Sequence[str]] = None,
) -> list[Optional[str]]:
    """构建去重后的压缩包密码尝试顺序列表（仅包含用户明确提供的密码）。"""
    ordered: list[Optional[str]] = []
    seen: set[Optional[str]] = set()

    def _push(item: Optional[str]) -> None:
        if item not in seen:
            seen.add(item)
            ordered.append(item)

    if password is not None and password != "":
        _push(password)
        _push(None)
    else:
        _push(None)

    for candidate in password_candidates or ():
        if candidate:
            _push(candidate)

    return ordered


def _clear_directory(directory: Path) -> None:
    """清空解压目标目录内容，避免密码重试残留文件。"""
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        return
    for item in list(directory.iterdir()):
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def _verify_extracted_tree(extract_dir: Path) -> None:
    """校验解压后的目录树，确保不存在指向外部的符号链接或路径越界。"""
    resolved_base = extract_dir.resolve()
    for item in extract_dir.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"压缩包包含不安全的符号链接: {item.name!r}")
        try:
            item.resolve().relative_to(resolved_base)
        except ValueError as exc:
            raise ValueError(f"解压文件路径越界: {item}") from exc


def _run_external(
    command: list[str],
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    """静默执行外部解压 CLI 命令，禁用 stdin 并设置超时防止挂死（P1-3）。"""
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "ignore",
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(command, **kwargs)  # type: ignore[arg-type]
    except subprocess.TimeoutExpired as exc:
        raise ValueError("解压超时，压缩包可能过大或已损坏") from exc


def _summarize_process_output(proc: subprocess.CompletedProcess[str]) -> str:
    combined = "\n".join(
        part.strip() for part in (proc.stderr or "", proc.stdout or "") if part and part.strip()
    )
    if not combined:
        return f"returncode={proc.returncode}"
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    summary = " | ".join(lines[-3:])
    return f"returncode={proc.returncode}: {summary[:300]}"


def _find_first_executable(candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute():
            if path.is_file():
                return str(path)
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _find_7z_executable() -> Optional[str]:
    return _find_first_executable(
        (
            "7z",
            "7za",
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        )
    )


def _find_unrar_executable() -> Optional[str]:
    return _find_first_executable(
        (
            "unrar",
            "rar",
            r"C:\Program Files\WinRAR\UnRAR.exe",
            r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
            r"C:\Program Files\WinRAR\WinRAR.exe",
        )
    )


def _find_bsdtar_executable() -> Optional[str]:
    if sys.platform == "win32":
        system_tar = Path(r"C:\Windows\system32\tar.exe")
        if system_tar.is_file():
            return str(system_tar)
    return _find_first_executable(("bsdtar", "tar"))


def _is_password_related_exception(exc: Exception) -> bool:
    """判断异常是否属于密码错误或加密校验失败（P1-1）。"""
    if isinstance(exc, (OSError, shutil.Error)):
        return False
    cls_name = type(exc).__name__.lower()
    mod_name = (type(exc).__module__ or "").lower()
    msg = str(exc).lower()
    password_tokens = (
        "password",
        "wrongpassword",
        "crc",
        "bad7zfile",
        "badzipfile",
        "corrupt",
        "encrypted",
        "lzmaerror",
        "decryption",
    )
    if any(token in cls_name for token in password_tokens):
        return True
    if ("py7zr" in mod_name or "rarfile" in mod_name) and any(
        token in msg for token in ("password", "crc", "corrupt", "encrypted", "bad")
    ):
        return True
    if any(token in msg for token in ("password", "encrypted", "wrong password", "crc failed")):
        return True
    return False


def _extract_zip_with_zipfile(
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> None:
    with zipfile.ZipFile(archive_path, "r") as zf:
        file_infos = [
            info
            for info in zf.infolist()
            if info.filename and not info.filename.endswith(("/", "\\"))
        ]
        # 前置校验所有成员路径（P1-2）
        for info in file_infos:
            resolve_safe_member_path(extract_dir, info.filename)

        is_encrypted = any(info.flag_bits & 0x1 for info in file_infos)

        if not is_encrypted:
            for info in file_infos:
                target = resolve_safe_member_path(extract_dir, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            return

        passwords_to_try = [pwd for pwd in password_list if pwd is not None]
        if not passwords_to_try:
            raise ValueError(
                "ZIP 压缩包已加密，请提供解压密码"
                "（命令行 --password，或在 GUI 中填写“解压密码”）"
            )

        last_error: Optional[Exception] = None
        for pwd in passwords_to_try:
            _clear_directory(extract_dir)
            try:
                pwd_bytes = pwd.encode("utf-8")
                for info in file_infos:
                    target = resolve_safe_member_path(extract_dir, info.filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r", pwd=pwd_bytes) as src, target.open(
                        "wb"
                    ) as dst:
                        shutil.copyfileobj(src, dst)
                logger.debug("ZIP 密码验证通过")
                return
            except (RuntimeError, zipfile.BadZipFile) as exc:
                logger.debug("ZIP 尝试密码失败: %s: %s", type(exc).__name__, exc)
                last_error = exc
                continue
            except NotImplementedError:
                if _try_extract_aes_zip(archive_path, extract_dir, password_list):
                    return
                raise ValueError(
                    "ZIP 使用了标准库不支持的加密方式（如 AES），"
                    "请安装 7-Zip 或 pyzipper 后重试"
                )

        logger.error("ZIP 已尝试 %d 个密码均失败: %s", len(passwords_to_try), last_error)
        raise ValueError(
            f"ZIP 压缩包解压失败，已尝试 {len(passwords_to_try)} 个密码均失败 "
            f"({type(last_error).__name__}: {last_error})"
        )


def _try_extract_aes_zip(
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> bool:
    try:
        import pyzipper  # type: ignore[import-not-found]

        with pyzipper.AESZipFile(archive_path, "r") as zf:
            file_infos = [
                info
                for info in zf.infolist()
                if info.filename and not info.filename.endswith(("/", "\\"))
            ]
            for info in file_infos:
                resolve_safe_member_path(extract_dir, info.filename)
            for pwd in password_list:
                if pwd is None:
                    continue
                _clear_directory(extract_dir)
                try:
                    zf.setpassword(pwd.encode("utf-8"))
                    for info in file_infos:
                        target = resolve_safe_member_path(extract_dir, info.filename)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info, "r") as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                    return True
                except (RuntimeError, zipfile.BadZipFile) as exc:
                    logger.debug("AES ZIP 尝试密码失败: %s", exc)
                    continue
    except ImportError:
        pass

    seven_z = _find_7z_executable()
    if seven_z:
        ok, _ = _try_extract_with_7z(seven_z, archive_path, extract_dir, password_list)
        return ok
    return False


def _try_extract_with_7z(
    seven_z_exe: str,
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
    timeout: float = 300.0,
) -> tuple[bool, str]:
    """调用外部 7z CLI 解压（P1-3：无密码时省略 -p，返回码 0/1 视为成功，收集 stderr 摘要）。"""
    last_detail = ""
    for pwd in password_list:
        _clear_directory(extract_dir)
        cmd = [
            seven_z_exe,
            "x",
            "-y",
        ]
        if pwd is not None:
            cmd.append(f"-p{pwd}")
        cmd.extend([f"-o{extract_dir}", str(archive_path)])
        proc = _run_external(cmd, timeout=timeout)
        if proc.returncode in (0, 1):
            logger.debug("7z CLI 解压成功 (returncode=%d)", proc.returncode)
            return True, ""
        last_detail = _summarize_process_output(proc)
        logger.debug("7z CLI 解压尝试失败: %s", last_detail)
    return False, last_detail


def _try_extract_with_unrar(
    unrar_exe: str,
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
    timeout: float = 300.0,
) -> tuple[bool, str]:
    """调用外部 UnRAR CLI 解压（P1-3：无密码时使用 -p-，收集 stderr 摘要）。"""
    last_detail = ""
    for pwd in password_list:
        _clear_directory(extract_dir)
        pwd_arg = f"-p{pwd}" if pwd is not None else "-p-"
        proc = _run_external(
            [
                unrar_exe,
                "x",
                "-o+",
                "-y",
                pwd_arg,
                str(archive_path),
                f"{extract_dir}\\" if sys.platform == "win32" else f"{extract_dir}/",
            ],
            timeout=timeout,
        )
        if proc.returncode == 0:
            logger.debug("UnRAR CLI 解压成功")
            return True, ""
        last_detail = _summarize_process_output(proc)
        logger.debug("UnRAR CLI 解压尝试失败: %s", last_detail)
    return False, last_detail


def _extract_tar(archive_path: Path, extract_dir: Path) -> None:
    with tarfile.open(archive_path, "r:*") as tf:
        members = tf.getmembers()
        for member in members:
            if member.isdir():
                continue
            if member.issym() or member.islnk() or not member.isfile():
                raise ValueError(f"压缩包包含不支持的特殊文件类型: {member.name!r}")
            resolve_safe_member_path(extract_dir, member.name)

        for member in members:
            if member.isdir():
                continue
            target = resolve_safe_member_path(extract_dir, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted, target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)


def _precheck_archive_members_with_bsdtar(
    bsdtar_exe: str, archive_path: Path, extract_dir: Path
) -> None:
    """在使用 bsdtar 解压前先列出归档成员并校验路径，防止路径穿越写盘（P1-2）。"""
    proc = _run_external([bsdtar_exe, "-tf", str(archive_path)])
    if proc.returncode == 0 and proc.stdout:
        for line in proc.stdout.splitlines():
            entry = line.strip()
            if entry:
                resolve_safe_member_path(extract_dir, entry)


def _extract_7z(
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> None:
    password_errors: list[Exception] = []
    cli_errors: list[str] = []

    # 1. 优先尝试 py7zr（P1-1 异常分流 + P1-2 逐成员安全落盘）
    try:
        import py7zr  # type: ignore[import-not-found]

        for pwd in password_list:
            _clear_directory(extract_dir)
            try:
                with py7zr.SevenZipFile(
                    archive_path, mode="r", password=pwd
                ) as sz:
                    member_names = list(sz.getnames())
                    for name in member_names:
                        resolve_safe_member_path(extract_dir, name)

                    with tempfile.TemporaryDirectory(prefix="py7zr_stage_") as stage_str:
                        stage_dir = Path(stage_str)
                        for name in member_names:
                            target = resolve_safe_member_path(extract_dir, name)
                            sz.extract(path=stage_dir, targets=[name])
                            staged_item = stage_dir / Path(*PurePosixPath(name.replace("\\", "/").lstrip("/")).parts)
                            if staged_item.is_file():
                                target.parent.mkdir(parents=True, exist_ok=True)
                                shutil.move(str(staged_item), str(target))
                            if hasattr(sz, "reset"):
                                sz.reset()
                logger.debug("py7zr 解压成功")
                return
            except ValueError:
                raise
            except (OSError, shutil.Error) as exc:
                logger.error("py7zr 解压遇到系统/IO错误: %s", exc, exc_info=True)
                raise ValueError(f"解压 .7z 时发生系统或磁盘错误 ({type(exc).__name__}): {exc}") from exc
            except Exception as exc:
                if _is_password_related_exception(exc):
                    logger.debug("py7zr 密码尝试 %r 失败: %s: %s", pwd, type(exc).__name__, exc)
                    password_errors.append(exc)
                    continue
                logger.error("py7zr 解压发生未预期异常", exc_info=True)
                raise ValueError(f"解压 .7z 失败 ({type(exc).__name__}): {exc}") from exc
    except ImportError:
        pass

    # 2. 尝试外部 7z CLI
    seven_z = _find_7z_executable()
    if seven_z:
        ok, detail = _try_extract_with_7z(
            seven_z, archive_path, extract_dir, password_list
        )
        if ok:
            return
        if detail:
            cli_errors.append(detail)

    # 3. 回退到系统内置 bsdtar（支持无密码 .7z，解压前预检成员路径）
    bsdtar = _find_bsdtar_executable()
    if bsdtar and not password_errors:
        _precheck_archive_members_with_bsdtar(bsdtar, archive_path, extract_dir)
        _clear_directory(extract_dir)
        proc = _run_external([bsdtar, "-xf", str(archive_path), "-C", str(extract_dir)])
        if proc.returncode == 0:
            return
        cli_errors.append(_summarize_process_output(proc))

    details: list[str] = []
    if password_errors:
        last_exc = password_errors[-1]
        details.append(
            f"已尝试 {len(password_errors)} 个密码均失败 "
            f"({type(last_exc).__name__}: {last_exc})"
        )
    if cli_errors:
        details.append("; ".join(cli_errors))
    detail_suffix = f"（{'; '.join(details)}）" if details else ""
    logger.error("无法解压 .7z 压缩包: %s", detail_suffix)
    raise ValueError(
        f"无法解压 .7z 压缩包：请确认解压密码正确或压缩包未损坏{detail_suffix}"
    )


def _extract_rar(
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> None:
    password_errors: list[Exception] = []
    cli_errors: list[str] = []

    # 1. 尝试 rarfile 库（P1-1 异常分流 + P1-2 rf.open 逐成员安全落盘，不调用 extractall）
    try:
        import rarfile  # type: ignore[import-not-found]

        with rarfile.RarFile(archive_path) as rf:
            members = [m for m in rf.infolist() if not m.is_dir()]
            for member in members:
                resolve_safe_member_path(extract_dir, member.filename)

            for pwd in password_list:
                _clear_directory(extract_dir)
                try:
                    for member in members:
                        target = resolve_safe_member_path(extract_dir, member.filename)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with rf.open(member, pwd=pwd) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                    logger.debug("rarfile 解压成功")
                    return
                except ValueError:
                    raise
                except (OSError, shutil.Error) as exc:
                    logger.error("rarfile 解压遇到系统/IO错误: %s", exc, exc_info=True)
                    raise ValueError(
                        f"解压 .rar 时发生系统或磁盘错误 ({type(exc).__name__}): {exc}"
                    ) from exc
                except Exception as exc:
                    if _is_password_related_exception(exc):
                        logger.debug(
                            "rarfile 密码尝试 %r 失败: %s: %s",
                            pwd,
                            type(exc).__name__,
                            exc,
                        )
                        password_errors.append(exc)
                        continue
                    logger.error("rarfile 解压发生未预期异常", exc_info=True)
                    raise ValueError(
                        f"解压 .rar 失败 ({type(exc).__name__}): {exc}"
                    ) from exc
    except ImportError:
        pass

    # 2. 尝试 7z / UnRAR CLI
    seven_z = _find_7z_executable()
    if seven_z:
        ok, detail = _try_extract_with_7z(
            seven_z, archive_path, extract_dir, password_list
        )
        if ok:
            return
        if detail:
            cli_errors.append(detail)

    unrar_exe = _find_unrar_executable()
    if unrar_exe:
        ok, detail = _try_extract_with_unrar(
            unrar_exe, archive_path, extract_dir, password_list
        )
        if ok:
            return
        if detail:
            cli_errors.append(detail)

    # 3. 回退到系统 bsdtar（部分无密码 RAR）
    bsdtar = _find_bsdtar_executable()
    if bsdtar and not password_errors:
        _precheck_archive_members_with_bsdtar(bsdtar, archive_path, extract_dir)
        _clear_directory(extract_dir)
        proc = _run_external([bsdtar, "-xf", str(archive_path), "-C", str(extract_dir)])
        if proc.returncode == 0:
            return
        cli_errors.append(_summarize_process_output(proc))

    details: list[str] = []
    if password_errors:
        last_exc = password_errors[-1]
        details.append(
            f"已尝试 {len(password_errors)} 个密码均失败 "
            f"({type(last_exc).__name__}: {last_exc})"
        )
    if cli_errors:
        details.append("; ".join(cli_errors))
    detail_suffix = f"（{'; '.join(details)}）" if details else ""
    logger.error("无法解压 .rar 压缩包: %s", detail_suffix)
    raise ValueError(
        f"无法解压 .rar 压缩包：请确认解压密码正确或安装 WinRAR / 7-Zip{detail_suffix}"
    )


def extract_archive(
    archive_path: Path,
    extract_dir: Path,
    *,
    password: Optional[str] = None,
    password_candidates: Optional[Sequence[str]] = None,
) -> None:
    """安全解压 .zip / .7z / .rar / .tar* 归档文件并支持用户指定候选密码尝试。"""
    extract_dir.mkdir(parents=True, exist_ok=True)
    password_list = build_password_list(
        password=password,
        password_candidates=password_candidates,
    )

    if _is_zip_archive(archive_path):
        _extract_zip_with_zipfile(archive_path, extract_dir, password_list)
    elif _is_tar_archive(archive_path):
        _extract_tar(archive_path, extract_dir)
    elif _is_7z_archive(archive_path):
        _extract_7z(archive_path, extract_dir, password_list)
    elif _is_rar_archive(archive_path):
        _extract_rar(archive_path, extract_dir, password_list)
    else:
        raise ValueError(f"不支持的压缩包格式: {archive_path.name}")

    _verify_extracted_tree(extract_dir)
