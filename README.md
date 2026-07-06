<div align="center">

# 🚀 Rclone Tray

**一个用于管理 rclone 挂载的 Windows 系统托盘工具**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](https://microsoft.com/windows)

</div>

---

## 📖 简介

Rclone Tray 是一个 Windows 系统托盘应用程序，提供图形界面来管理 `rclone mount` 的挂载操作。它支持多个配置（Profile）、自动崩溃恢复、系统托盘菜单、Windows 通知等功能，让 rclone 挂载管理变得更简单。

## ✨ 特性

- 🖥️ **系统托盘** - 最小化到托盘，后台运行
- 📂 **多配置管理** - 创建、编辑、切换多个挂载配置
- 🎯 **多目标挂载** - 一个配置可挂载多个远程路径
- 🔄 **自动恢复** - 监控 rclone 进程，崩溃后自动重启
- 🔔 **Windows 通知** - 挂载成功/失败/异常即时通知
- 🚀 **开机自启** - 支持设置开机自动启动
- 📋 **日志查看** - 运行日志和调试日志分离
- 🌙 **主题切换** - 支持浅色/深色主题（预留）
- 🌐 **多语言** - 支持中文/English（预留）

## 🏗 架构

```
┌─────────────────────────────────────────┐
│           Presentation Layer            │
│  ┌────────┐ ┌──────────┐ ┌──────────┐  │
│  │  Tray  │ │MainWindow│ │ Dialogs  │  │
│  └────┬───┘ └────┬─────┘ └────┬─────┘  │
└───────┴──────────┴────────────┴─────────┘
                    │
        ┌───────────┴───────────┐
        │ ApplicationCoordinator │
        └───────────┬───────────┘
                    │
    ┌───────────────┴───────────────┐
    │         Domain Layer          │
    │  ┌────────────┐ ┌───────────┐ │
    │  │  Profile   │ │   Mount   │ │
    │  │  Manager   │ │  Manager  │ │
    │  └────────────┘ └───────────┘ │
    └───────────────┬───────────────┘
                    │
    ┌───────────────┴───────────────┐
    │      Infrastructure Layer      │
    │  ┌──────┐ ┌──────┐ ┌───────┐  │
    │  │Config│ │Rclone│ │Watch- │  │
    │  │Service│ │Service│ │dog   │  │
    │  └──────┘ └──────┘ └───────┘  │
    │  ┌──────┐ ┌──────┐ ┌───────┐  │
    │  │Logger│ │Notify│ │System │  │
    │  └──────┘ └──────┘ └───────┘  │
    └───────────────────────────────┘
```

## 📦 安装

### 前置要求

- [Python 3.12+](https://python.org)
- [rclone](https://rclone.org/downloads/)

### 从源码运行

```bash
# 克隆仓库
git clone https://github.com/Benincasamatch/Rclone_Tray.git
cd Rclone_Tray

# 安装依赖
pip install -r requirements.txt

# 运行
python -m rclone_tray
```

### 打包为 exe

**开发阶段（PyInstaller）:**

```bash
python scripts/build_pyinstaller.py
```

**正式发布（Nuitka）:**

```bash
python scripts/build_nuitka.py
```

## 🚀 使用

1. 首次启动会自动检测 rclone
2. 创建配置 - 填写远程地址和挂载位置
3. 点击"启动"开始挂载
4. 最小化到托盘，后台运行

### 托盘菜单

- 状态显示（已挂载/未挂载/异常）
- 启动/停止/重启服务
- 快速切换配置
- 打开主窗口/设置/日志

## 🗺 开发计划

- [x] 基础框架搭建
- [x] 配置文件管理
- [x] rclone 进程管理
- [x] Watchdog 自动恢复
- [x] 系统托盘
- [x] 主窗口 UI
- [ ] 国际化（i18n）
- [ ] 主题切换
- [ ] WebDAV 备份与恢复
- [ ] 自动更新
- [ ] 安装向导（首次启动）

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

[MIT](LICENSE) © 2026 Benincasamatch
