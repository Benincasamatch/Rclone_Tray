# rclone_tray

Windows 本地 rclone 管理工具：主窗口与系统托盘共享配置、进程状态和操作结果。保留真实 rclone 调用，不是演示界面。

## 使用构建文件

当前版本为 Rust + Win32，无需 Python。发布产物为 `dist\rclone_tray_setup.exe` 安装程序与便携版 `dist\rclone_tray.exe`，rclone 位于同目录的 `rclone\rclone.exe`。安装程序按当前用户安装到 `%LOCALAPPDATA%\Programs\rclone_tray`，写入开始菜单项与卸载入口，不申请管理员权限。挂载需要 [WinFsp](https://winfsp.dev/rel/)，安装驱动需 Windows 授权。

- 手动运行显示挂载首页；关闭窗口隐藏至托盘，不停止挂载。
- 托盘“打开挂载管理”唤起主窗口；“退出”确认后停止本应用的 GUI/挂载并取消延迟任务。
- 不接管或终止其他应用启动的 rclone。
- 应用被强制结束（崩溃、任务管理器结束进程）时，本应用启动的 rclone 子进程可能继续运行；重新打开后应用不会接管它们。请在任务管理器中结束对应 `rclone.exe`，或在重启后恢复。正常退出（托盘“退出”）会停止本应用的全部进程。
- 请从正常用户会话启动。Windows 提权和普通用户会话的盘符可见性不同；管理员启动创建的盘符不一定能在普通资源管理器中看到。

## 功能与页面

| 页面 | 操作 |
| --- | --- |
| 挂载，默认首页 | 搜索名称/remote/位置、状态筛选、添加/编辑/删除配置、逐项与全部启停、打开位置、查看日志 |
| rclone GUI | 服务就绪状态、启动/停止、打开当前网页、用户名密码认证（默认）或显式免认证、随应用启动 |
| 设置 | 可执行文件与 rclone.conf 路径、环境检测、系统自启、开机自动挂载和延迟、主题、减少动态效果 |
| 关于与诊断 | 版本与构建信息、检查 GitHub Releases 更新、一键打开下载链接、WinFsp、配置/日志位置、复制诊断、官方帮助、退出 |
| 托盘 | 快捷启停、挂载子菜单、打开配置、启动偏好、主窗口与退出 |

新增/编辑挂载独立保存，保存不自动启动。运行中的连接参数须先停止后修改；普通主题或显示名修改不会丢失进程管理关系。删除只删除挂载配置，不删除远端文件。

界面遵循《Tern · Stencil 设计参考》：深色默认、紫色强调、统一线性图标与焦点状态、可折叠侧栏、右侧详情和固定底部状态条。窄窗口将详情堆叠到列表下方，操作换行并可滚动。Windows DWM 为标题栏提供可选材质和圆角，内容区用不同亮度的实色层次保证可读性，不是全窗口背景模糊；「减少动态与材质效果」关闭原生转场与标题栏材质。浅色与跟随系统保留相同布局。

目录挂载接受普通空目录或父目录存在的尚未创建子目录；不接受含文件的目录、链接或重解析点。普通空目录在挂载前仅用非递归 `rmdir` 移除，停止后恢复。网络模式只能使用明确盘符；空卷标使用按挂载 ID 生成的唯一共享名。

## 配置与安全

应用配置：`%APPDATA%\rclone_tray\config.json`；运行日志与缓存分别在该目录的 `logs\`、`cache\`。

- 配置 schema 2，旧版字段和挂载 ID 保留；保存使用原子替换。
- 配置损坏不会被重置或覆盖；修复原文件后点“重新载入配置”。
- 未保存修改在取消、导航、隐藏和退出前确认；保存失败保留输入。
- 外部文件变化或保存版本冲突拒绝覆盖；重新载入后可显式保留输入并更新版本。
- GUI 密码使用 Windows DPAPI 加密，受 Windows 用户/环境绑定，不能把密文复制到另一用户后期待直接解密。旧明文在明确保存时迁移。
- GUI 认证通过网页输入；URL 不包含用户名或密码，进程输出在日志落盘前脱敏。
- GUI 默认要求认证：未填写用户名和密码时不会启动服务。显式勾选“免认证”会以 `--no-auth` 开放本机 5572 端口，本机任何进程都能调用 rclone RC API（等同本机命令执行权限），仅在完全受控环境使用。

rclone 可执行文件优先级：明确设置 → 打包 EXE 旁的 `rclone.exe` 或 `rclone\rclone.exe` → 内置 → PATH → 常见目录。明确设置无效不会偷偷使用另一个程序。自动发现只影响本次运行，解析到的路径不会写回配置文件（单文件版内置路径位于临时解包目录，下次启动即失效）。

现代 rclone GUI 由内置资源提供；v1.74 以下使用 `rcd --rc-web-gui`，首次运行需要下载其官方 WebUI，网络受限可能就绪超时，日志会保留原因；不假报启动成功。

系统自启注册表项位于 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，命令带 `--autostart`。开机自动挂载只在此启动来源执行，且全局策略与逐项开关同时允许；手动打开不意外挂载。GUI 自动启动表达“随应用启动”。

## 版本、更新与安装

- 版本号为语义化版本（`x.y.z`），由 `Cargo.toml` 决定；构建时嵌入 git commit、分支、构建时间与未提交修改标记，显示在“关于与诊断”和复制出的诊断信息中。
- 应用启动后在后台请求 GitHub Releases，忽略草稿与预发布，按语义化版本比较。发现更高版本时发送一次托盘气泡提醒，点击气泡进入“关于与诊断”；“关于与诊断”显示当前版本、最新版本、发布时间、更新说明和资产大小。
- “检查更新”可手动重试；“打开下载页面”优先打开发布中的 `rclone_tray_setup.exe`，没有安装包时打开该版本发布页。更新模块只打开链接，不下载、不替换正在运行的 exe。
- 离线或 API 限流只记录在“关于与诊断”，启动检查不会弹错误气泡；手动检查会显示具体原因。

## 安装与升级

1. 运行 `dist\rclone_tray_setup.exe`，按向导安装；已安装时同一安装程序覆盖升级，保留 `%APPDATA%\rclone_tray` 中的配置与日志。
2. 托盘退出旧实例后再运行安装程序；安装程序通过应用互斥体检测运行中的实例，存在时要求先关闭再继续（静默安装在同样条件下直接退出，返回码 1）。
3. 卸载通过“设置 → 应用”或开始菜单卸载项完成，不删除用户配置。

## 源码运行

使用 Rust Windows GNU 工具链；构建脚本优先使用 `E:\tools\rust`，否则从 PATH 查找 cargo。项目内需要官方 `rclone\rclone.exe`。

构建期用 binutils 的 `windres` 把 `assets/app.rc` 编译进 EXE，为资源管理器、开始菜单和快捷方式提供文件图标；找不到时构建会报错，可用 `WINDRES` 环境变量指定。托盘与窗口图标仍由同一份 `assets/icon.ico` 在运行时创建。

```powershell
.\tools\build.ps1       # 构建 exe 与安装程序；运行中的 EXE 须先从托盘退出
.\target\release\rclone_tray.exe --show
```

构建安装程序需要 Inno Setup 6：`winget install JRSoftware.InnoSetup`，或设置 `INNO_SETUP_ISCC` 指向 `ISCC.exe`。脚本从 `Cargo.toml` 读取版本并产出 `dist\rclone_tray_setup.exe`、对应 `.sha256` 与 `dist\build-manifest.json`。

`--show` 显式显示主窗口；`--autostart` 是系统登录启动标记。隔离验证可设 `RCLONE_TRAY_DATA_DIR`，应用配置、日志、缓存转到该目录；`RCLONE_CONFIG` 或设置中的 `rclone_conf` 可指定独立 rclone 配置。

工作区若被打了 `Mandatory Label\Low` 完整性标签（`icacls <目录>` 可见），该目录下启动的 EXE 会以 Low 完整性运行，无法写 `%APPDATA%`，应用报 `创建临时文件失败（系统错误 5）`、挂载返回 `c0000022`。修复：`powershell -ExecutionPolicy Bypass -File tools\fix-integrity-label.ps1 -Root <工作区目录>`（检测到 Low 时才请求管理员权限，把标签改回 Medium）。

## 构建与回归

```powershell
.\tools\build.ps1 -Test
.\.venv\Scripts\python.exe tools\capture_ui.py --exe dist\rclone_tray.exe
.\.venv\Scripts\python.exe tools\capture_ui.py --exe dist\rclone_tray.exe --light
```

`tools/capture_ui.py` 使用 ctypes + Pillow 驱动真实原生窗口，逐页截图并检查搜索、键盘、主题草稿、窄窗口、编辑取消与退出；临时数据在结束后删除，不启动挂载或 GUI。需要解锁的交互桌面和匹配的进程完整性级别；低完整性工具进程无法切前台时，可通过 Windows 授权终端运行该脚本。应用运行不依赖 Python。截图与结果在 `docs/evidence/native-stencil-*.png`、`native-stencil-checks.json` 及对应浅色文件，最近一次更新模块烟测为 `native-2.1-update-about*.png`；产物身份见 `dist/build-manifest.json`。

应用本身不依赖 Python；仅 `tools/capture_ui.py` 验收截图工具需要 Python 与 Pillow（`python -m pip install -r requirements.txt`）。旧 Python 实现与 PyInstaller 配置已移除，历史验收截图与开发日志保留用于追踪。

## 文档

- [重建计划](docs/项目计划.md)
- [交互规格](docs/交互规格.md)
- [架构决策](docs/架构决策.md)
- [开发日志](devlog/2026-10-31-重建实施.md) · [界面重构日志](devlog/2026-11-01-界面重构.md) · [版本控制与更新](devlog/2026-11-02-版本控制与更新.md)
- [版本与发布](docs/版本与发布.md)
- [验收记录与截图](docs/验收记录.md)
- rclone 官方文档：<https://rclone.org/docs/>
