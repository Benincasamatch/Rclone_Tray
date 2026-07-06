"""Nuitka 构建脚本

用于正式发布的 Nuitka 编译打包。
生成单文件 exe，体积更小、启动更快。

用法:
    python scripts/build_nuitka.py

前置条件:
    pip install nuitka

输出:
    dist/rclone-tray.exe
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


def main() -> None:
    original_cwd = Path.cwd()

    try:
        __import__("os").chdir(str(PROJECT_ROOT))

        args = [
            sys.executable or "python",
            "-m",
            "nuitka",
            # 输出配置
            "--output-dir=" + str(DIST_DIR),
            # 模块配置
            "--module",
            # 单文件模式
            "--onefile",
            # 无控制台
            "--windows-disable-console",
            # 插件
            "--enable-plugin=pyqt6",
            "--enable-plugin=pyside6",
            # 包含 PySide6 必要组件
            "--include-package=PySide6",
            # 包含数据文件
            f"--include-data-dir={PROJECT_ROOT / 'src' / 'rclone_tray' / 'resources'}=rclone_tray/resources",
            # 公司/版本信息
            "--windows-company-name=Benincasamatch",
            f"--windows-product-version=0.1.0",
            "--windows-file-description=Rclone Tray",
            # 排除不需要的
            "--nofollow-import-to=tkinter",
            "--nofollow-import-to=test",
            "--nofollow-import-to=pytest",
            # 优化
            "--remove-output",
            # 输入
            str(PROJECT_ROOT / "src" / "rclone_tray" / "__main__.py"),
        ]

        print("=" * 60)
        print("Building Rclone Tray with Nuitka...")
        print("=" * 60)
        print(f"Command: {' '.join(args)}")
        print()

        result = subprocess.run(args, check=True)

        if result.returncode == 0:
            print()
            print("=" * 60)
            print("✅ Build successful!")
            exe_path = DIST_DIR / "rclone-tray.exe"
            if exe_path.exists():
                print(f"   Output: {exe_path}")
            print("=" * 60)

    except subprocess.CalledProcessError as exc:
        print(f"❌ Build failed with exit code {exc.returncode}")
        sys.exit(1)
    finally:
        __import__("os").chdir(str(original_cwd))


if __name__ == "__main__":
    main()
