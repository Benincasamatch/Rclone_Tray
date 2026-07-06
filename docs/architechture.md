# 目标

1. 高内聚、低耦合
2. UI 与业务逻辑分离
3. 易于扩展多个配置
4. 易于替换 rclone 实现
5. 易于单元测试
6. 支持长期维护
7. 支持未来增加插件

所有设计都不能违反这里。

细化目标
[[module design]]
[[sequence design]]
[[interface design]]
[[state machine]]

框架：
[[Logical Architecture]]

---
# 逻辑结构
ui layer:主窗口、设置、托盘、通知
application layer：协调业务流程、处理命令
Domain Layer：配置、挂载、状态、规则
 Infrastructure Layer ： rclone、文件系统、日志、系统API

---
# 职责划分
每个模块回答三个问题：

```
负责什么？
不能负责什么？
依赖谁？
```

### UI Layer

负责：
- 显示界面
- 收集用户输入
- 展示状态
不能：
- 启动 rclone
- 修改配置文件
- 操作进程
只能调用：
Application Layer

---

### Application Layer
负责：
- 执行用户命令
- 编排流程
- 调用各个服务
不能：
- 绘制界面
- 直接操作控件

---

### Domain Layer
负责：
- Profile
- Mount
- Config
- State
- Validation
不能：
- 调系统 API

---

### Infrastructure Layer
负责：
- 启动进程
- 读写配置
- Windows API
- 日志
- 通知
不能：
- 写业务逻辑

---
# 核心模块
Profile Manager
    管理多个配置

Mount Manager
    管理挂载生命周期

Process Watchdog
    监控 rclone

Config Service
    配置读写

Tray Service
    托盘

Notification Service
    Windows 通知

Log Service
    日志

Update Service（预留）

Theme Service

Language Service
注意：这些是**业务模块**，不是文件夹。

---
# 数据流
```
用户点击

↓

UI

↓

Application

↓

Mount Manager

↓

Process Watchdog

↓

rmount.exe

↓

状态返回

↓

UI刷新
```

---
# 事件流
```rmount 崩溃

↓

Watchdog

↓

Restart

↓

更新状态

↓

Notification

↓

刷新 UI
```
---
# 目录结构
```
app/  
  
ui/  
  
application/  
  
domain/  
  
infrastructure/  
  
resources/  
  
tests/  
  
docs/
```
