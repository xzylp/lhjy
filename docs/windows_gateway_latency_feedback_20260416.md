# Windows 网关 / QMT 接口耗时反馈

更新时间：2026-04-16 20:30 +08:00

目的：

- 给 Windows 侧排查 `18791 -> QMT / Trade Bridge` 的真实慢点
- 区分 “Linux 代码调度问题” 和 “Windows / QMT 查询本身慢”
- 便于在 Windows 侧逐条复现与比对

---

## 1. 结论摘要

当前已经确认：

1. Linux 侧此前存在一层代码问题：
   - `readiness / account-state` 等重接口在 `async def` 中直接执行同步重逻辑，曾导致整个控制面事件循环被拖住。
   - 这一层已经部分修正。
   - 证据：修正后 `GET /system/operations/components` 在混合压测下已恢复正常，不再跟着一起卡死。

2. 但目前仍存在一层明确的上游慢查询问题：
   - `GET /qmt/account/orders`
   - `GET /qmt/account/trades`
   - 这两个接口在 Linux 侧通过 Go 平台和 fallback 到 Windows 直连后都超时。

3. `GET /qmt/account/asset` 也偏慢，但能返回。
4. `GET /qmt/account/positions` 相对正常。

因此当前主判断是：

- “控制面整体被拖死”这层，Linux 代码有问题，已在修。
- “账户状态 / readiness 仍然慢”这层，主要是 Windows 网关 / QMT 某些账户查询接口本身慢，尤其 `orders/trades`。

---

## 2026-04-16 22:10 +08:00 增量结论

本轮继续联调后，结论进一步收敛：

1. Linux 控制面侧的“假阻断”已经基本拆净。
   - `live + go_platform` 之前被旧 `xtquant` 健康检查口径误判为 `invalid`
   - 现在已改正，`/system/healthcheck` 返回 `ok=true`
   - `readiness` 也已从 `blocked` 收敛为 `degraded`

2. 启动阶段已经自动刷新账户快照。
   - 服务重启后 `account_access/account_identity` 现可直接恢复为 `ok`
   - 不再因为历史缓存过期导致 `readiness` 误判

3. 未决订单巡检与处置已验证不是当前主阻断。
   - 实时处置结果：
     - `pending_order_inspection = clear`
     - `pending_order_remediation = no_action`
   - 之前 readiness 中看到的 `timed out / error` 属于旧缓存脏状态，不是当前事实

4. 当前唯一剩余的真实慢点已收敛到：
   - `POST /system/execution-reconciliation/run`
   - 根因仍是：
     - `GET /qmt/account/trades`
   - 在 readiness 中对应表现为：
     - `execution_reconciliation = warning`
     - `detail = windows_gateway_timeout: /qmt/account/trades | elapsed>10.0s`

5. 业务判断：
   - 这条慢链主要影响：
     - 成交回执
     - 对账
     - 巡检闭环
   - 当前不应再把整套交易控制面判成 `blocked`
   - 更合理口径是：
     - `readiness = degraded`
     - 允许继续观察与人工联调

---

## 2. 关键接口实测耗时

测试环境：

- Linux Go 统一入口：`http://127.0.0.1:18793`
- Linux Python 控制面：`http://127.0.0.1:8100`
- Windows Go 网关：`http://192.168.122.66:18791`
- 账户：`8890130545`

### 2.1 直接从 Linux 适配层测执行接口

实测脚本是直接调用 Linux 当前 `execution_adapter`：

- `get_balance(account_id)`
- `get_positions(account_id)`
- `get_orders(account_id)`
- `get_trades(account_id)`

结果：

| 查询项 | 结果 | 耗时 |
|---|---:|---:|
| `balance` | 成功 | `5114.77ms` |
| `positions` | 成功 | `622.32ms` |
| `orders` | 超时失败 | `25026.86ms` |
| `trades` | 超时失败 | `25014.70ms` |

对应 Linux 侧日志：

```text
go_platform_timeout | GET /qmt/account/orders | elapsed=15015.35ms | error=timed out
go_platform_exec_fallback | GET /qmt/account/orders | error=go_platform_timeout: /qmt/account/orders | elapsed 15015ms | falling back to windows_proxy
go_platform_exec_fallback_failed | GET /qmt/account/orders | error=windows_gateway_timeout: /qmt/account/orders | elapsed>10.0s

go_platform_error | GET /qmt/account/trades | error=go_platform_overloaded: /qmt/account/trades | trade queue timeout
go_platform_exec_fallback | GET /qmt/account/trades | error=go_platform_overloaded: /qmt/account/trades | trade queue timeout | falling back to windows_proxy
go_platform_exec_fallback_failed | GET /qmt/account/trades | error=windows_gateway_timeout: /qmt/account/trades | elapsed>10.0s
```

解释：

- Go 平台第一段等待约 15 秒
- fallback 到 Windows 直连后又等 10 秒
- 所以最终 `orders/trades` 的单次业务耗时会接近 25 秒

这已经明显超出在线控制面/健康检查的可接受范围。

---

### 2.2 直接请求 Windows 网关资产接口

接口：

- `GET http://192.168.122.66:18791/qmt/account/asset`

结果：

- 可成功返回真实账户资产
- 返回账户：`8890130545`
- 最近一次成功返回资产：
  - `cash = 85347.4`
  - `market_value = 15867.0`
  - `total_asset = 101214.4`

说明：

- 资产接口不是完全不可用
- 但 Linux 侧多次实测中，资产接口响应大约在 `4s ~ 5s` 级别

---

### 2.3 混合压测结果

并发请求集合：

- `/system/operations/components`
- `/system/readiness?account_id=8890130545`
- `/qmt/account/asset`
- `/qmt/account/positions`

每个接口并发 3 次，总共 12 个请求。

#### 修复前

- `system_components` 超时
- `system_readiness` 超时
- `qmt_asset` 成功但约 `5s`
- `qmt_positions` 成功

这说明当时 Linux 控制面存在“慢请求拖死整个事件循环”的问题。

#### 修复部分调度问题后

最新混合压测结果：

| 接口 | 结果 | 最大耗时 |
|---|---:|---:|
| `system_components` | 3/3 成功 | `12.42ms` |
| `qmt_asset` | 3/3 成功 | `4005.56ms` |
| `qmt_positions` | 3/3 成功 | `4026.19ms` |
| `system_readiness` | 3/3 超时 | `~8011ms` |

解释：

- `system_components` 已恢复，证明 Linux 事件循环阻塞问题已拆掉一大块。
- `system_readiness` 仍慢，说明它内部业务依赖的查询仍然过重。
- 进一步追查发现，`readiness` 依赖的账户状态链仍会被 `balance / positions / orders / trades` 慢查询拖住。

---

## 3. 当前 Linux 侧判断

### 3.1 已确认属于 Linux 代码层的问题

- 重接口原本在异步路由里直接跑同步重逻辑
- 会导致：
  - `readiness`
  - `account-state`
  - `controlled-apply-readiness`
  - `service-recovery-readiness`
  把整个控制面事件循环拖住

这层已经开始修：

- 重路由转入线程池
- `readiness` 改成优先读缓存
- `pending_order_inspection` 改成优先读缓存

### 3.2 已确认更像 Windows / QMT 侧的问题

以下接口本身明显慢或超时：

- `/qmt/account/orders`
- `/qmt/account/trades`
- `/qmt/account/asset` 偏慢

特别是 `orders/trades`：

- 经 Go 平台调用慢
- fallback 到 Windows 直连仍慢
- 因此不太像 Linux Go 平台转发导致
- 更像 Windows 网关内部 Trade Bridge / XtQuant / QMT 查询链本身慢

---

## 4. 对交易实时性的实际影响

### 4.1 主要影响

当前慢点主要会影响：

- `/system/readiness`
- `/system/account-state`
- 健康检查 / 监督面板
- 订单巡检 / 回执 / 对账类接口

### 4.2 相对不直接影响

当前最慢的是：

- `orders`
- `trades`

这两条更偏：

- 回执
- 巡检
- 对账
- 健康检查

它们不是最核心的“盘口实时行情链”。

### 4.3 仍需关注

`balance` 当前也有 `~5s` 级延迟，这会影响：

- 执行前检查
- 仓位预算计算
- apply 前确认

所以不能完全忽略。

---

## 5. 建议 Windows 侧重点核查

请优先在 Windows 侧单独测以下接口的真实耗时，并确认是不是 QMT / Trade Bridge 本身就慢：

### 必测

1. `GET /qmt/account/asset`
2. `GET /qmt/account/positions`
3. `GET /qmt/account/orders`
4. `GET /qmt/account/trades`

### 建议记录

- 单次耗时
- 并发 3 次时耗时
- 是否进入队列等待
- 是否超时
- Go 网关日志中分配到哪条 lane
- Trade Bridge / XtQuant / QMT 内部实际耗时

### 重点怀疑点

1. `trade_slow` lane 本身队列等待过长
2. Trade Bridge 查询 `orders/trades` 本来就慢
3. XtQuant / QMT 在 `query_stock_orders / query_stock_trades` 上阻塞
4. Windows 网关对 `orders/trades` 没有单独优化，导致超时累积

---

## 6. 当前 Linux 侧后续动作

Linux 侧准备继续做的，不影响业务主逻辑：

1. 把 `readiness` 彻底改成轻检查，只读缓存
2. 把 `orders/trades` 从在线健康检查链路彻底移出
3. 把实时交易主链只保留：
   - `balance`
   - `positions`
4. 单独继续跟踪 `balance` 5 秒级延迟

当前不会改：

- 选股逻辑
- 讨论逻辑
- 风控规则
- 执行规则
- 仓位与参数业务语义

---

## 7. 一句话结论

当前最慢的不是“账户状态整体”，而是：

- `orders`
- `trades`

它们主要影响巡检、回执、对账与健康检查。

另外：

- `balance` 也偏慢，约 `5s`
- `positions` 相对正常

所以建议 Windows 侧优先核查：

- `orders/trades` 是否是 QMT / Trade Bridge 本身慢
- `asset` 为什么稳定在 `4s~5s`
