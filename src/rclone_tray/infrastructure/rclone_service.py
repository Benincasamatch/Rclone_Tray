"""Rclone Service - rclone 进程管理

职责：
- 启动 / 停止 / 重启 rclone 进程
- 查询状态（PID、ExitCode）
- 不包含业务编排逻辑

生命周期：
    [start] → 启动子进程 → [running]
    [stop]  → 终止子进程 → [stopped]
    [restart] → stop → start
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rclone_tray.infrastructure.logger import get_logger

logger = get_logger(__name__)


class RcloneService:
    """rclone 进程管理服务。

    管理单个 rclone mount 进程的完整生命周期。
    每个 Profile 可以有多个挂载目标，每个目标可能对应一个进程。
    """

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[str]] = {}  # mount_point -> process
        self._exit_codes: dict[str, Optional[int]] = {}

        # 注册进程清理
        atexit.register(self._cleanup_all)

    # ── 属性 ──────────────────────────────────────────────

    @property
    def active_mount_points(self) -> list[str]:
        """返回当前正在运行的挂载点列表。"""
        return list(self._processes.keys())

    @property
    def is_any_running(self) -> bool:
        """是否有任何进程在运行。"""
        return any(self._is_process_alive(p) for p in self._processes.values())

    def get_pid(self, mount_point: str) -> Optional[int]:
        """获取指定挂载点的进程 PID。"""
        proc = self._processes.get(mount_point)
        if proc and self._is_process_alive(proc):
            return proc.pid
        return None

    def get_pids(self) -> list[int]:
        """获取所有活跃进程的 PID 列表。"""
        return [
            proc.pid for proc in self._processes.values()
            if self._is_process_alive(proc)
        ]

    def get_exit_code(self, mount_point: str) -> Optional[int]:
        """获取指定挂载点的退出码。"""
        proc = self._processes.get(mount_point)
        if proc and proc.poll() is not None:
            return proc.returncode
        return self._exit_codes.get(mount_point)

    def get_all_status(self) -> dict[str, dict]:
        """获取所有进程的详细状态。"""
        status: dict[str, dict] = {}
        for mp, proc in list(self._processes.items()):
            alive = self._is_process_alive(proc)
            status[mp] = {
                "pid": proc.pid if alive else None,
                "running": alive,
                "exit_code": proc.returncode if not alive else None,
                "mount_point": mp,
            }
        return status

    # ── 核心操作 ──────────────────────────────────────────

    def start(
        self,
        mount_point: str,
        remote: str,
        rclone_path: str = "rclone",
        extra_args: Optional[list[str]] = None,
        flags: Optional[list[str]] | None = None,
    ) -> None:
        """启动 rclone mount 进程。

        Args:
            mount_point: 本地挂载点（如 Z:）
            remote: 远程路径（如 remote:path）
            rclone_path: rclone 可执行文件路径
            extra_args: 额外参数
            flags: 挂载标志参数

        Raises:
            RcloneNotFoundError: rclone 未找到
            RcloneAlreadyRunningError: 该挂载点已有进程在运行
            RcloneStartError: 启动失败
        """
        if mount_point in self._processes:
            proc = self._processes[mount_point]
            if self._is_process_alive(proc):
                raise RcloneAlreadyRunningError(
                    f"挂载点 {mount_point} 已有进程在运行 (PID: {proc.pid})"
                )
            # 进程已死，清理旧记录
            self._cleanup_mount_point(mount_point)

        # 查找 rclone 可执行文件
        rclone_exe = self._find_rclone(rclone_path)
        if not rclone_exe:
            raise RcloneNotFoundError(
                f"未找到 rclone: {rclone_path}。请检查路径或下载 rclone。"
            )

        # 构建命令
        cmd = [str(rclone_exe), "mount", remote, mount_point]
        if extra_args:
            cmd.extend(extra_args)
        if flags:
            cmd.extend(flags)

        # 常用默认参数
        default_flags = ["--vfs-cache-mode", "writes", "--daemon"]
        for flag in default_flags:
            if flag not in cmd:
                cmd.append(flag)

        logger.info("启动 rclone: %s", " ".join(cmd))
        logger.debug("完整命令: %s", cmd)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self._processes[mount_point] = proc
            self._exit_codes[mount_point] = None
            logger.info("rclone 已启动 (PID: %d, 挂载点: %s)", proc.pid, mount_point)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RcloneStartError(f"启动 rclone 失败: {exc}") from exc

    def stop(self, mount_point: str, timeout: float = 10.0) -> bool:
        """停止指定挂载点的 rclone 进程。

        Returns:
            进程是否已成功停止
        """
        proc = self._processes.get(mount_point)
        if not proc:
            logger.warning("挂载点 %s 无进程可停止", mount_point)
            return True

        if not self._is_process_alive(proc):
            self._cleanup_mount_point(mount_point)
            return True

        logger.info("停止 rclone (PID: %d, 挂载点: %s)", proc.pid, mount_point)

        try:
            # Windows 使用 terminate
            if sys.platform == "win32":
                proc.terminate()
            else:
                os.kill(proc.pid, signal.SIGTERM)

            try:
                proc.wait(timeout=timeout)
                logger.info("rclone 已停止 (挂载点: %s)", mount_point)
            except subprocess.TimeoutExpired:
                logger.warning("rclone 未在 %ss 内响应，强制终止", timeout)
                proc.kill()
                proc.wait(timeout=5.0)

            self._exit_codes[mount_point] = proc.returncode
            self._cleanup_mount_point(mount_point)
            return True

        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("停止 rclone 失败: %s", exc)
            return False

    def stop_all(self, timeout: float = 10.0) -> None:
        """停止所有 rclone 进程。"""
        for mp in list(self._processes.keys()):
            self.stop(mp, timeout)

    def restart(self, mount_point: str, **start_kwargs: str) -> None:
        """重启指定挂载点的 rclone 进程。"""
        self.stop(mount_point)
        self.start(mount_point, **start_kwargs)

    # ── 自动检测 ──────────────────────────────────────────

    @staticmethod
    def auto_detect_rclone() -> Optional[str]:
        """自动检测系统中的 rclone。"""
        # 常见安装位置
        candidates = [
            "rclone",
            "rclone.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "rclone" / "rclone.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "rclone" / "rclone.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "rclone" / "rclone.exe",
            Path.home() / "AppData" / "Local" / "rclone" / "rclone.exe",
            Path.home() / "rclone.exe",
        ]

        for candidate in candidates:
            rclone_path = RcloneService._find_rclone(str(candidate))
            if rclone_path:
                return str(rclone_path)

        return None

    @staticmethod
    def check_rclone_version(rclone_path: str) -> Optional[str]:
        """检查 rclone 版本。"""
        try:
            result = subprocess.run(
                [rclone_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                first_line = result.stdout.splitlines()[0] if result.stdout else ""
                return first_line.strip()
            return None
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("检查 rclone 版本失败: %s", exc)
            return None

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _find_rclone(path: str) -> Optional[Path]:
        """查找 rclone 可执行文件。"""
        path_obj = Path(path)
        if path_obj.is_file():
            return path_obj.resolve()

        # 尝试 PATH 环境变量
        which_cmd = "where" if sys.platform == "win32" else "which"
        try:
            result = subprocess.run(
                [which_cmd, path if path != "rclone" else "rclone"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                found = result.stdout.strip().splitlines()[0]
                return Path(found).resolve()
        except (OSError, subprocess.TimeoutExpired):
            pass

        return None

    @staticmethod
    def _is_process_alive(proc: Optional[subprocess.Popen]) -> bool:
        """检查进程是否存活。"""
        if proc is None:
            return False
        return proc.poll() is None

    def _cleanup_mount_point(self, mount_point: str) -> None:
        """清理已终止的进程记录。"""
        proc = self._processes.pop(mount_point, None)
        if proc and proc.returncode is not None:
            self._exit_codes[mount_point] = proc.returncode

    def _cleanup_all(self) -> None:
        """程序退出时清理所有进程。"""
        for mp in list(self._processes.keys()):
            try:
                self.stop(mp, timeout=3.0)
            except Exception as exc:
                logger.debug("清理进程 %s 时出错: %s", mp, exc)


# ── 异常定义 ─────────────────────────────────────────────


class RcloneError(Exception):
    """rclone 服务基础异常。"""


class RcloneNotFoundError(RcloneError):
    """rclone 可执行文件未找到。"""


class RcloneAlreadyRunningError(RcloneError):
    """rclone 已在运行。"""


class RcloneStartError(RcloneError):
    """rclone 启动失败。"""
