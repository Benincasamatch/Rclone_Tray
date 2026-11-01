"""rclone executable discovery, validated version output, and WinFsp evidence."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

SEARCH_DIRS = [
    r"%ProgramFiles%\rclone",
    r"%ProgramFiles(x86)%\rclone",
    r"%LocalAppData%\rclone",
    r"%LocalAppData%\Programs\rclone",
    r"%USERPROFILE%\rclone",
    r"C:\rclone",
    r"C:\tools\rclone",
    r"%USERPROFILE%\scoop\apps\rclone\current",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
]


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _script_dir() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return str(Path(__file__).resolve().parent.parent)


def _bundle_dir() -> str:
    return str(getattr(sys, "_MEIPASS", _script_dir()))


def _candidates() -> list[str]:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.extend([str(Path(_script_dir()) / "rclone.exe"),
                           str(Path(_script_dir()) / "rclone" / "rclone.exe")])
    candidates.extend([str(Path(_bundle_dir()) / "rclone" / "rclone.exe"),
                       str(Path(_bundle_dir()) / "rclone.exe")])
    on_path = shutil.which("rclone")
    if on_path:
        candidates.append(on_path)
    candidates.extend(str(Path(expand(directory)) / "rclone.exe") for directory in SEARCH_DIRS)
    return list(dict.fromkeys(candidates))


def find_rclone(preferred: str = "") -> Optional[str]:
    """Explicit invalid paths never silently select a different executable."""
    if preferred:
        path = Path(expand(preferred))
        return str(path.resolve()) if path.is_file() else None
    for candidate in _candidates():
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
    return None


def parse_version(text: str) -> Optional[str]:
    match = re.search(r"(?m)^rclone v(\d+\.\d+(?:\.\d+)?)(?:[-+][^\s]+)?\s*$", text)
    return match.group(1) if match else None


def version_tuple(version: Optional[str]) -> tuple[int, int]:
    if not version:
        return (0, 0)
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_version(exe: str) -> Optional[str]:
    """Only a real, successful rclone version response is accepted."""
    if not exe or not Path(exe).is_file():
        return None
    try:
        result = subprocess.run([exe, "version"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15,
                                creationflags=no_window_flag(), shell=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(result.stdout) if result.returncode == 0 else None


def winfsp_installed() -> bool:
    """Look for the native WinFsp runtime; a guessed install directory is not evidence."""
    if os.name != "nt":
        return False
    import winreg
    roots: list[Path] = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WinFsp", 0,
                                winreg.KEY_READ | view) as key:
                directory, kind = winreg.QueryValueEx(key, "InstallDir")
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and directory:
                    roots.append(Path(expand(directory)))
        except OSError:
            pass
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / "WinFsp")
    runtime = "winfsp-x64.dll" if sys.maxsize > 2**32 else "winfsp-x86.dll"
    return any((root / "bin" / runtime).is_file() for root in roots)
