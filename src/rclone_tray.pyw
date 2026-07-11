"""rclone_tray.pyw - Windows 无控制台模式入口文件

使用 .pyw 扩展名确保 Python 以无控制台模式 (pythonw.exe) 运行，
避免打包后的 EXE 文件显示命令行窗口。
"""

from rclone_tray.main import run

if __name__ == "__main__":
    run()
