负责设计模块。
# Profile Manager

职责
------
负责管理所有 Profile 提供统一profile入口

负责
------
新增
删除
修改
切换
导入
导出

依赖
------
Config Service

对外提供
------
LoadProfiles()
SaveProfile()
SwitchProfile()

不会负责
------
启动 rclone
更新 UI

---
# Rclone Service

职责
------
管理整个挂载生命周期

负责
------
## 控制能力
调用 rclone
停止 rclone
重启 rclone
查询 rclone 状态
获取 PID
获取 ExitCode

## 查询能力
CheckStatus
生命周期状态
挂载信息（当前profile、挂载点等）

## 事件能力
生命周期出现变化通知
异常通知

依赖
------
Process Manager
Profile Manager

---
# Watchdog

职责
------
监控 rclone

负责
------
Crash Detection
Restart
Retry Count

不会负责
------
UI

---
# Tray
职责:
提供系统托盘入口，并负责托盘 UI 的展示与交互。

## 对外提供
托盘菜单
图标状态
托盘入口
## 主动做：
订阅 Mount Manager 的状态变化事件，并更新托盘 UI。
## 不做：
业务判断
rclone 调用
Watchdog 控制
生命周期管理

---
# Controller
## 负责：
接收 UI 请求
协调多个 Manager
管理整个应用流程
处理业务逻辑
发布应用事件

