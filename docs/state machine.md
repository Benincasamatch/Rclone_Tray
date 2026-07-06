| 当前状态       | 事件          | 下一状态     |
| ---------- | ----------- | -------- |
| configured | start       | starting |
| starting   | success     | mounted  |
| mounted    | crash       | restart  |
| restart    | success     | mounted  |
| restart    | retry limit | error    |


