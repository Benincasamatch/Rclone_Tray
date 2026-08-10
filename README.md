# rclone_tray

Windows 系统托盘程序，用于管理 [rclone](https://rclone.org/) 的运行状态。

## 功能

- 自动搜索 `rclone.exe` 路径并显示版本
- 托盘管理 `rclone gui` 服务进程（Web GUI），一键在浏览器打开
- 管理多个 `rclone mount` 挂载进程（盘符 / 网络驱动器）
- 开机自启（注册表）与开机自动挂载
- 提供 rclone config 管理入口

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> 挂载功能需要先安装 [WinFsp](https://winfsp.dev/)。

## 运行

```powershell
python main.py
```

> 也可以直接运行打包产物 `dist\rclone_tray.exe`（已内置 rclone）。

## 打包

```powershell
pip install pyinstaller
python -m PyInstaller --noconfirm --clean rclone_tray.spec
# 产物：dist\rclone_tray.exe（单文件、无控制台、内置 rclone.exe）
```

- 项目内 `rclone\` 文件夹放置 rclone.exe（程序自动优先使用）
- 打包时通过 `rclone_tray.spec` 将 `rclone\`、`docs\` 一并打入 exe
- 如需在 exe 旁覆盖 rclone，将新的 `rclone\rclone.exe` 放在 exe 同目录即可

> `rclone\rclone.exe` 为第三方二进制（约 75MB），已通过 `.gitignore` 排除在版本控制之外，需自行从 [rclone 官网](https://rclone.org/downloads/) 下载后放入该目录。

## 配置

配置文件位于 `%APPDATA%\rclone_tray\config.json`，也可通过托盘「设置...」界面修改。

## 文档

- 制作流程与设计：`docs/项目计划.md`
- rclone 官方文档：<https://rclone.org/docs/>
