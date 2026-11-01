# rclone_tray

Windows 本地 rclone 管理工具：主窗口与系统托盘共享配置、进程状态和操作结果。保留真实 rclone 调用，不是演示界面。

## 使用构建文件

运行 `dist\rclone_tray.exe`。单文件、无控制台，内置 rclone v1.74.0；无需安装 Python。挂载需要 [WinFsp](https://winfsp.dev/rel/)，安装驱动需 Windows 授权。

- 手动运行显示挂载首页；关闭窗口隐藏至托盘，不停止挂载。
- 托盘“打开挂载管理”唤起主窗口；“退出”确认后停止本应用的 GUI/挂载并取消延迟任务。
- 不接管或终止其他应用启动的 rclone。
- 请从正常用户会话启动。Windows 提权和普通用户会话的盘符可见性不同；管理员启动创建的盘符不一定能在普通资源管理器中看到。

## 功能与页面

| 页面 | 操作 |
| --- | --- |
| 挂载，默认首页 | 搜索名称/remote/位置、状态筛选、添加/编辑/删除配置、逐项与全部启停、打开位置、查看日志 |
| rclone GUI | 服务就绪状态、启动/停止、打开当前网页、免认证或用户名密码、随应用启动 |
| 设置 | 可执行文件与 rclone.conf 路径、环境检测、系统自启、开机自动挂载和延迟、主题、减少动态效果 |
| 关于与诊断 | 版本、WinFsp、配置/日志位置、复制诊断、官方帮助、退出 |
| 托盘 | 快捷启停、挂载子菜单、打开配置、启动偏好、主窗口与退出 |

新增/编辑挂载独立保存，保存不自动启动。运行中的连接参数须先停止后修改；普通主题或显示名修改不会丢失进程管理关系。删除只删除挂载配置，不删除远端文件。

界面遵循《Tern · Stencil 设计参考》：深色玻璃主题为默认，浅色与跟随系统为派生配色；侧栏导航、内容面板、右侧详情与底部状态条，4px 间距体系与紫色强调色。半透明由应用内环境渐变与面板叠加模拟（Tkinter 无背景模糊），窄窗口自动折叠导航与详情栏，“减少动态效果”让所有过渡立即完成。

目录挂载接受普通空目录或父目录存在的尚未创建子目录；不接受含文件的目录、链接或重解析点。普通空目录在挂载前仅用非递归 `rmdir` 移除，停止后恢复。网络模式只能使用明确盘符；空卷标使用按挂载 ID 生成的唯一共享名。

## 配置与安全

应用配置：`%APPDATA%\rclone_tray\config.json`；运行日志与缓存分别在该目录的 `logs\`、`cache\`。

- 配置 schema 2，旧版字段和挂载 ID 保留；保存使用原子替换。
- 配置损坏不会被重置或覆盖；修复原文件后点“重新载入配置”。
- 未保存修改在取消、导航、隐藏和退出前确认；保存失败保留输入。
- 外部文件变化或保存版本冲突拒绝覆盖；重新载入后可显式保留输入并更新版本。
- GUI 密码使用 Windows DPAPI 加密，受 Windows 用户/环境绑定，不能把密文复制到另一用户后期待直接解密。旧明文在明确保存时迁移。
- GUI 认证通过网页输入；URL 不包含用户名或密码，进程输出在日志落盘前脱敏。

rclone 可执行文件优先级：明确设置 → 打包 EXE 旁的 `rclone.exe` 或 `rclone\rclone.exe` → 内置 → PATH → 常见目录。明确设置无效不会偷偷使用另一个程序。

现代 rclone GUI 由内置资源提供；v1.74 以下使用 `rcd --rc-web-gui`，首次运行需要下载其官方 WebUI，网络受限可能就绪超时，日志会保留原因；不假报启动成功。

系统自启注册表项位于 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，命令带 `--autostart`。开机自动挂载只在此启动来源执行，且全局策略与逐项开关同时允许；手动打开不意外挂载。GUI 自动启动表达“随应用启动”。

## 源码运行

支持本次构建使用的 Python 3.12。项目内放置 `rclone\rclone.exe`，第三方二进制不纳入版本控制。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

`--show` 显式显示主窗口；`--autostart` 是系统登录启动标记。隔离验证可设 `RCLONE_TRAY_DATA_DIR`，应用配置、日志、缓存转到该目录；`RCLONE_CONFIG` 或设置中的 `rclone_conf` 可指定独立 rclone 配置。

## 构建与回归

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean rclone_tray.spec
.\.venv\Scripts\python.exe tools\capture_ui.py            # 隔离沙箱逐页截图到 docs\evidence\（加 --light 为浅色）
```

产物：`dist\rclone_tray.exe`。打包包括 rclone、图标、文档和项目许可证。配置仍保存在用户目录，不写到 EXE 解包目录。

若运行环境的 `TEMP`/`TMP` 不可写，PyInstaller 单文件解包会在进入应用前报错；请修复目录权限，或从将 `TEMP`/`TMP` 指向可写目录的进程启动。应用不能在 bootloader 失败前修改该环境。

## 文档

- [重建计划](docs/项目计划.md)
- [交互规格](docs/交互规格.md)
- [架构决策](docs/架构决策.md)
- [开发日志](devlog/2026-10-31-重建实施.md) · [界面重构日志](devlog/2026-11-01-界面重构.md)
- [验收记录与截图](docs/验收记录.md)
- rclone 官方文档：<https://rclone.org/docs/>
