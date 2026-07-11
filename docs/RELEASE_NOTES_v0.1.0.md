# Rclone Tray v0.1.0 发布说明

## 📦 发布信息

- **版本号**: v0.1.0
- **发布日期**: 2024
- **许可证**: MIT

## ✨ 主要功能

### 核心功能
- ✅ 系统托盘集成（Windows）
- ✅ 挂载配置管理（CRUD 操作）
- ✅ 配置文件导入/导出
- ✅ 自动挂载与开机启动
- ✅ 进程监控与自动恢复
- ✅ 系统通知支持

### 技术特性
- ✅ 分层架构设计（UI/Application/Domain/Infrastructure）
- ✅ 模块化设计，易于扩展
- ✅ 完整的单元测试覆盖（93.75% 通过率）
- ✅ 类型注解完整

## 🐛 Bug 修复

### 已修复问题
1. **无限命令行窗口问题**
   - 问题：打包后的 EXE 文件运行时无限打开命令行窗口
   - 解决方案：创建 `.pyw` 入口文件，使用无控制台模式
   - 影响：Windows 用户不再看到命令行窗口闪烁

2. **构建脚本路径问题**
   - 问题：PyInstaller 6.x 语法变更导致构建失败
   - 解决方案：更新 `--add-data` 参数格式为等号形式
   - 影响：跨平台构建兼容性提升

## 🔧 技术改进

### 构建系统
- 修复 PyInstaller 6.x 兼容性问题
- 优化资源文件打包路径处理
- 添加 Nuitka 构建支持（可选）

### 代码质量
- 通过 Twine 包验证检查
- Wheel 和源码包双重分发格式
- 符合 PEP 517/518 标准

## 📋 安装方式

### Python 包安装
```bash
# 方式 1: wheel 包（推荐）
pip install rclone_tray-0.1.0-py3-none-any.whl

# 方式 2: 源码包
pip install rclone_tray-0.1.0.tar.gz

# 方式 3: 从 PyPI（发布后）
pip install rclone-tray
```

### Windows EXE 生成
```bash
# 在 Windows 环境下执行
pip install pyinstaller
python scripts/build_pyinstaller.py
# 输出：dist/RcloneTray.exe
```

## ⚠️ 已知限制

1. **平台限制**: 主要针对 Windows 优化，Linux/macOS 支持有限
2. **Python 版本**: 需要 Python 3.12+
3. **依赖工具**: 需要预先安装并配置 rclone

## 📝 使用说明

### 启动应用
```bash
python -m rclone_tray
```

### 首次使用
1. 确保已安装 rclone 并完成初始配置
2. 运行应用后会在系统托盘显示图标
3. 右键点击托盘图标可管理挂载配置
4. 支持配置的导入/导出

## 🔮 未来计划

- [ ] 现代化 Web 前端支持（FastAPI + Vue3/React）
- [ ] 增加 MountManager 单元测试
- [ ] 配置热重载功能
- [ ] 跨平台系统托盘支持优化
- [ ] 更多预设配置模板

## 📄 许可证

MIT License - 详见 LICENSE 文件

## 👥 贡献者

- Benincasamatch

---

**注意**: 当前构建的 Linux 可执行文件仅供测试，Windows 用户请在 Windows 环境下重新打包生成 EXE 文件。
