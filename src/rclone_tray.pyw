"""Rclone Tray Windows 启动入口（无控制台窗口）

此文件用于 Windows 系统下直接双击启动，不会显示命令行窗口。
使用 .pyw 扩展名确保 Python 以无控制台模式运行。

用法:
    双击 rclone_tray.pyw 启动应用
    或在命令行运行：pythonw.exe rclone_tray.pyw
"""

from rclone_tray.main import run

if __name__ == "__main__":
    run()
