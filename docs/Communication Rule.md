```
禁止：UI ─────► ServiceUI ─────► ManagerManager ─► ManagerService ─► UIService ─► Service
```

全部禁止。

允许：

```
UI ↓CoordinatorCoordinator ↓ServiceService ↓CoordinatorCoordinator ↓UI
```

任何通信都经过 Coordinator。
> **Coordinator 负责"协调"，不负责"实现"。**