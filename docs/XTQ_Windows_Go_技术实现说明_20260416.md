# XTQ Windows Go 网关技术实现说明

更新时间：2026-04-16 13:00 +08:00

## 1. 改造目标

本次改造目标不是改协议，而是替换 Windows 侧外部入口的数据面实现：

- 保持 Linux -> Windows 的接口约定不变
- 保持 Windows -> QMT / Trade Bridge 的业务能力不变
- 解决单慢请求拖死整个 `18791` 的问题
- 增加排队、优先级、重试与配置化并发
- 保证程序与运行时数据固定保存在项目目录

## 2. 当前组件结构

### 2.1 对外入口

Go Manager 内嵌 Go Proxy，统一对外监听：

- `0.0.0.0:18791`

管理 UI 独立监听：

- `127.0.0.1:18889`

### 2.2 内部调用链

- 外部请求 -> Go Proxy (`18791`)
- 行情类请求 -> `scripts/qmt_rpc_py36.py` -> QMT Python 3.6 运行时
- 账户/交易类请求 -> Trade Bridge (`127.0.0.1:18792`) -> XtQuant / QMT
- 状态类请求 -> 本地状态文件 + Trade Bridge 健康检查

### 2.3 项目内固定路径

- 项目根目录：`C:\Users\yxzzh\Desktop\XTQ`
- 状态目录：`C:\Users\yxzzh\Desktop\XTQ\.ashare_state`
- 日志目录：`C:\Users\yxzzh\Desktop\XTQ\logs`
- 运行目录：`C:\Users\yxzzh\Desktop\XTQ\runtime`
- Go 构建缓存：`C:\Users\yxzzh\Desktop\XTQ\go_manager\.gocache`
- Go 模块缓存：`C:\Users\yxzzh\Desktop\XTQ\go_manager\.gomodcache`

## 3. 关键实现点

### 3.1 端口切换

旧版 Python `windows_ops_proxy.py` 原本占用 `18791`。
目前已切换为 Go Proxy 接管 `18791`，同时保持：

- Token 文件不变
- endpoints 文件不变
- `/status` 返回结构兼容

### 3.2 状态兼容

Go Proxy 不再通过 HTTP 回调旧版 `/status`，而是本地组装兼容结构，避免自调用递归。

兼容状态来源：

- `qmt_watchdog_status.json`
- `gateway_worker_status.json`
- Trade Bridge `/health`
- 项目配置与日志路径

### 3.3 三层优先级调度

当前实现使用三条独立 lane：

1. `quote`
   - 高优先级
   - 用于行情与只读市场数据
2. `account_fast`
   - 中优先级
   - 用于资产、持仓等快查询
3. `trade_slow`
   - 低优先级
   - 用于委托、成交、下单、查单等可能慢阻塞请求

这样做的结果是：

- 慢 `orders/trades` 不会挤占行情池
- 慢 `orders/trades` 不会完全堵死 `asset/positions`
- `/health` 无需进入慢交易调度

### 3.4 排队机制

每条 lane 均包含：

- `workers` 工位池
- `queue` 排队槽位
- `queue_timeout_ms` 排队超时

调度顺序：

1. 请求先尝试进入队列
2. 队列内等待空闲工位
3. 获得工位后执行下游调用
4. 超过排队时间后返回 `429`

与旧实现区别：

- 旧实现：池满即立刻 `429`
- 新实现：先排队，再在超时点快速失败

### 3.5 重试策略

每条 lane 支持独立 `retries` 配置。

当前策略：

- 行情 lane：允许重试
- 账户快查 lane：允许重试
- 慢交易 lane：默认不重试
- `/qmt/trade/order`：强制不自动重试，避免重复下单

重试触发条件：

- 上游连接错误
- `502 / 503 / 504`
- 5xx 错误
- 部分 `ok=false` 失败响应

退避参数：

- `retry_backoff_ms = 350`

## 4. 配置化实现

配置文件：

- `C:\Users\yxzzh\Desktop\XTQ\config\windows_gateway_config.json`

新增配置段：

```json
"go_proxy": {
  "quote": {
    "workers": 8,
    "queue": 32,
    "queue_timeout_ms": 4000,
    "retries": 1
  },
  "account_fast": {
    "workers": 6,
    "queue": 24,
    "queue_timeout_ms": 15000,
    "retries": 1
  },
  "trade_slow": {
    "workers": 4,
    "queue": 12,
    "queue_timeout_ms": 15000,
    "retries": 0
  },
  "retry_backoff_ms": 350
}
```

如果配置缺失，Go 代码会自动补默认值。

## 5. 代码位置

核心代码：

- `C:\Users\yxzzh\Desktop\XTQ\go_manager\main.go`
- `C:\Users\yxzzh\Desktop\XTQ\go_manager\proxy.go`
- `C:\Users\yxzzh\Desktop\XTQ\go_manager\web\index.html`
- `C:\Users\yxzzh\Desktop\XTQ\start_xtq_go_manager.bat`

本次实现主要内容：

- 新增 `go_proxy` 配置段解析与默认值处理
- 将固定信号量改为 lane 调度模型
- 引入排队槽位与排队超时
- 引入 lane 级失败重试
- 保持 `/status` 兼容结构
- 启动脚本默认启用 Go Proxy 并监听 `18791`

## 6. 实测结论

2026-04-16 已完成本机验证：

- Go Proxy 已接管 `18791`
- Go Manager UI 正常监听 `18889`
- `/health` 正常
- `/status` 正常并兼容原结构
- `quote/instruments` 正常返回样例证券
- `account/asset` 正常返回实盘资产数据
- 在 5 个慢 `orders` 并发场景下，`/health` 仍约 68ms 返回，`/asset` 仍可正常完成

## 7. 风险与边界

仍需明确的运行边界：

- 当前是“受控并发”，不是无限并发
- 持续超载时，请求会先排队，超过队列等待时间后返回 `429`
- `trade_slow` 默认不自动重试，是为了避免重复下单与重复交易风险
- Linux 控制面网络抖动仍会影响 `gateway_worker` 上报与轮询，但不影响本地 `18791` 对外健康响应

## 8. 结论

本次 Go 化后的 Windows 外部入口已经从“单慢请求可拖死全局”升级为：

- 接口兼容
- 多并发接入
- 受控并发执行
- 分层优先级
- 排队等待
- 可配置并发
- 有限重试
- 项目内固定落盘

当前实现适合作为 Linux 侧继续对接的正式 Windows 数据面入口。
