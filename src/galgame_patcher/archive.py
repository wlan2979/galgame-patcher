"""多格式归档解压模块，支持 .zip / .7z / .rar / .tar* 与密码自动尝试。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

from .models import COMMON_ARCHIVE_EXTENSIONS, COMMON_ARCHIVE_PASSWORDS

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
    normalized = member_name.replace("\\", "/").lstrip("/")
    pure = PurePosixPath(normalized)
    if member_name.startswith(("/", "\\")) or ".." in pure.parts:
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
    """构建去重后的压缩包密码尝试顺序列表。"""
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

    for common in COMMON_ARCHIVE_PASSWORDS:
        if common:
            _push(common)

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


def _run_external(command: list[str]) -> subprocess.CompletedProcess[str]:
    """静默执行外部解压 CLI 命令（在 Windows GUI 模式下不弹出黑框）。"""
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "ignore",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, **kwargs)  # type: ignore[arg-type]


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
            raise ValueError("ZIP 压缩包已加密，但未提供可用解压密码")

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
                return
            except (RuntimeError, zipfile.BadZipFile) as exc:
                last_error = exc
                continue
            except NotImplementedError:
                # 可能是 AES 加密的 ZIP，尝试 pyzipper 或外部 7z
                if _try_extract_aes_zip(archive_path, extract_dir, password_list):
                    return
                raise ValueError(
                    "ZIP 使用了标准库不支持的加密方式（如 AES），"
                    "请安装 7-Zip 或 pyzipper 后重试"
                )

        raise ValueError(f"ZIP 压缩包解压失败，密码不正确或压缩包已损坏: {last_error}")


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
                except (RuntimeError, zipfile.BadZipFile):
                    continue
    except ImportError:
        pass

    seven_z = _find_7z_executable()
    if seven_z:
        return _try_extract_with_7z(seven_z, archive_path, extract_dir, password_list)
    return False


def _try_extract_with_7z(
    seven_z_exe: str,
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> bool:
    for pwd in password_list:
        _clear_directory(extract_dir)
        pwd_arg = f"-p{pwd}" if pwd is not None else "-p"
        proc = _run_external(
            [
                seven_z_exe,
                "x",
                "-y",
                pwd_arg,
                f"-o{extract_dir}",
                str(archive_path),
            ]
        )
        if proc.returncode == 0:
            return True
    return False


def _try_extract_with_unrar(
    unrar_exe: str,
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> bool:
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
            ]
        )
        if proc.returncode == 0:
            return True
    return False


def _extract_tar(archive_path: Path, extract_dir: Path) -> None:
    with tarfile.open(archive_path, "r:*") as tf:
        for member in tf.getmembers():
            if member.isdir():
                continue
            if member.issym() or member.islnk() or not member.isfile():
                raise ValueError(f"压缩包包含不支持的特殊文件类型: {member.name!r}")
            target = resolve_safe_member_path(extract_dir, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted, target.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)


def _extract_7z(
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> None:
    # 1. 优先尝试 py7zr（适合打包成单文件 .exe 后无外部依赖运行）
    try:
        import py7zr  # type: ignore[import-not-found]

        for pwd in password_list:
            _clear_directory(extract_dir)
            try:
                with py7zr.SevenZipFile(
                    archive_path, mode="r", password=pwd
                ) as sz:
                    for name in sz.getnames():
                        resolve_safe_member_path(extract_dir, name)
                    sz.extractall(path=extract_dir)
                return
            except Exception:
                continue
    except ImportError:
        pass

    # 2. 尝试外部 7z CLI
    seven_z = _find_7z_executable()
    if seven_z and _try_extract_with_7z(
        seven_z, archive_path, extract_dir, password_list
    ):
        return

    # 3. 回退到系统内置 bsdtar（支持无密码 .7z）
    bsdtar = _find_bsdtar_executable()
    if bsdtar:
        _clear_directory(extract_dir)
        proc = _run_external([bsdtar, "-xf", str(archive_path), "-C", str(extract_dir)])
        if proc.returncode == 0:
            return

    raise ValueError(
        "无法解压 .7z 压缩包：请确认解压密码正确，"
        "或安装 py7zr / 7-Zip 后重试"
    )


def _extract_rar(
    archive_path: Path,
    extract_dir: Path,
    password_list: list[Optional[str]],
) -> None:
    # 1. 优先尝试 7z / UnRAR CLI
    seven_z = _find_7z_executable()
    if seven_z and _try_extract_with_7z(
        seven_z, archive_path, extract_dir, password_list
    ):
        return

    unrar_exe = _find_unrar_executable()
    if unrar_exe and _try_extract_with_unrar(
        unrar_exe, archive_path, extract_dir, password_list
    ):
        return

    # 2. 尝试 rarfile 库
    try:
        import rarfile  # type: ignore[import-not-found]

        with rarfile.RarFile(archive_path) as rf:
            for member in rf.infolist():
                if not member.is_dir():
                    resolve_safe_member_path(extract_dir, member.filename)
            for pwd in password_list:
                _clear_directory(extract_dir)
                try:
                    rf.extractall(path=extract_dir, pwd=pwd)
                    return
                except Exception:
                    continue
    except ImportError:
        pass

    # 3. 回退到系统 bsdtar（部分无密码 RAR）
    bsdtar = _find_bsdtar_executable()
    if bsdtar:
        _clear_directory(extract_dir)
        proc = _run_external([bsdtar, "-xf", str(archive_path), "-C", str(extract_dir)])
        if proc.returncode == 0:
            return

    raise ValueError(
        "无法解压 .rar 压缩包：请确认解压密码正确，"
        "或安装 WinRAR (UnRAR) / 7-Zip 后重试"
    )


def extract_archive(
    archive_path: Path,
    extract_dir: Path,
    *,
    password: Optional[str] = None,
    password_candidates: Optional[Sequence[str]] = None,
) -> None:
    """安全解压 .zip / .7z / .rar / .tar* 归档文件并支持密码自动轮询尝试。"""
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
