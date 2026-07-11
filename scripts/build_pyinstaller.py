"""PyInstaller 构建脚本

用于开发阶段的快速打包。

用法:
    python scripts/build_pyinstaller.py

输出:
    dist/rclone-tray/
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
SPEC_NAME = PROJECT_ROOT / "rclone-tray.spec"


def main() -> None:
    # 确保在项目根目录
    original_cwd = Path.cwd()
    try:
        # 切换到项目根目录
        __import__("os").chdir(str(PROJECT_ROOT))

        # 构建参数
        # 使用 os.sep 确保路径分隔符正确
        import os
        # PyInstaller 6.x 要求 --add-data 使用等号格式：--add-data=SOURCE:DEST
        data_path = f"src{os.sep}rclone_tray{os.sep}resources{os.sep}icons:resources{os.sep}icons"
        
        args = [
            sys.executable or "python",
            "-m",
            "PyInstaller",
            "--name=RcloneTray",
            "--onefile",           # 单文件 exe
            "--windowed",          # 无控制台窗口（关键：防止命令行窗口闪烁）
            "--noconfirm",
            "--clean",
            "--distpath", str(DIST_DIR),
            "--workpath", str(BUILD_DIR),
            "--icon=NONE",         # TODO: 添加图标文件
            # 包含资源文件（注意：Linux 使用:分隔，Windows 使用;分隔）
            f"--add-data={data_path}",
            # 隐藏导入
            "--hidden-import", "PySide6.QtCore",
            "--hidden-import", "PySide6.QtGui",
            "--hidden-import", "PySide6.QtWidgets",
            "--hidden-import", "tomli_w",
            # 使用 .pyw 入口文件（Windows 无控制台模式）
            str(PROJECT_ROOT / "src" / "rclone_tray.pyw"),
        ]

        print("=" * 60)
        print("Building Rclone Tray with PyInstaller...")
        print("=" * 60)
        print(f"Command: {' '.join(args)}")
        print()

        result = subprocess.run(args, check=True)

        if result.returncode == 0:
            print()
            print("=" * 60)
            print("✅ Build successful!")
            print(f"   Output: {DIST_DIR / 'RcloneTray.exe'}")
            print("=" * 60)

    except subprocess.CalledProcessError as exc:
        print(f"❌ Build failed with exit code {exc.returncode}")
        sys.exit(1)
    finally:
        __import__("os").chdir(str(original_cwd))


if __name__ == "__main__":
    main()
