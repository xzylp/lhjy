# Windows 网关延迟问题回复

更新时间：2026-04-16 22:05 +08:00

对应反馈文件：

- `windows_gateway_latency_feedback_20260416.md`

## 1. 本次 Windows 侧结论

本次对照 Linux 反馈后，Windows 侧确认存在一层实现问题，且已修复：

1. `asset` 与 `positions` 原先被错误地绑在一起查询
   - `/qmt/account/asset` 实际同时执行了 `asset + positions`
   - `/qmt/account/positions` 实际也同时执行了 `asset + positions`

2. `orders` 与 `trades` 原先也被错误地绑在一起查询
   - `/qmt/account/orders` 实际同时执行了 `orders + trades`
   - `/qmt/account/trades` 实际也同时执行了 `orders + trades`

这会直接放大单接口耗时，并把慢查询互相拖累。

另外，Windows 侧已补一层明确的“禁代理”处理：

- Go 网关出站 HTTP 显式禁用系统/环境代理
- `gateway_worker` 显式禁用 `urllib` 代理
- 兼容保留的 `windows_ops_proxy` 也显式禁用 `urllib` 代理

因此当前项目内程序不会再依赖 Astar 之类的系统代理去中转内网/本地请求。

## 2. 已完成修复

### 2.1 接口拆分修复

已修复文件：

- `C:\Users\yxzzh\Desktop\XTQ\scripts\qmt_rpc_py36.py`

修复内容：

- `asset` 只查 `query_stock_asset`
- `positions` 只查 `query_stock_positions`
- `orders` 只查 `query_stock_orders`
- `trades` 只查 `query_stock_trades`
- `order_status` 不再顺带把 `orders + trades` 一起查

### 2.2 代理绕行修复

已修复文件：

- `C:\Users\yxzzh\Desktop\XTQ\go_manager\main.go`
- `C:\Users\yxzzh\Desktop\XTQ\go_manager\proxy.go`
- `C:\Users\yxzzh\Desktop\XTQ\ashare_system\windows_execution_gateway_worker.py`
- `C:\Users\yxzzh\Desktop\XTQ\ashare_system\windows_ops_proxy.py`

修复内容：

- Go `http.Client` 使用显式 `Proxy: nil`
- Python `urllib` 使用 `ProxyHandler({})`

### 2.3 生效情况

- Go 网关已重启生效
- `gateway_worker` 已重新拉起，当前状态文件已刷新到 22:00 左右

## 3. Windows 本机对比结果

以下为 2026-04-16 晚间 Windows 本机实测。

### 3.1 Go 网关对外入口 `18791`

| 接口 | 结果 | 耗时 |
|---|---:|---:|
| `/qmt/account/asset` | 成功 | `4524.54ms` |
| `/qmt/account/positions` | 成功 | `654.95ms` |
| `/qmt/account/orders` | 成功 | `1130.81ms` |

### 3.2 Trade Bridge 直连 `18792`

| 接口 | 结果 | 耗时 |
|---|---:|---:|
| `/qmt/account/asset` | 成功 | `4550.07ms` |
| `/qmt/account/orders` | 成功 | `700.90ms` |
| `/qmt/account/trades` | 超时失败 | `70034.36ms` |

补充样本：

- `/qmt/account/positions` 在修复后已不再强制绑定 `asset`
- 当前 `positions` 在 Windows 本机已可落到亚秒级样本（`18791` 实测约 `655ms`）

## 4. 与 Linux 反馈的对比结论

### 4.1 已确认是 Windows 侧实现问题，并已修复的部分

Linux 反馈中，`positions` 被观测到进入 `4s` 级别。

Windows 本次排查后确认，之前代码确实把：

- `positions` 绑上了 `asset`
- `orders` 绑上了 `trades`

这属于 Windows 侧实现问题，不是 Linux 调度问题。

该问题现已修复。

### 4.2 当前已可排除不是 Go 外层代理导致的部分

从本机直连对比看：

- `asset`
  - `18791` 约 `4525ms`
  - `18792` 约 `4550ms`
  - 两层几乎一致，说明慢点不在 Go 外层转发

- `orders`
  - `18791` 约 `1131ms`
  - `18792` 约 `701ms`
  - Go 外层增加的只是小量转发开销，不是主慢点

因此：

- Go 网关本身不是当前主要延迟来源
- `asset` 的慢点更靠近 Trade Bridge / XtQuant / QMT

### 4.3 当前仍明显慢的部分

`/qmt/account/trades` 目前在 Windows 本机直连 `18792` 时，仍然超过 `70s` 超时。

这说明：

- 即使绕过 Linux
- 即使绕过 Go 外层网关
- 即使拆掉 `orders + trades` 的错误耦合

`trades` 这一条仍然是 Windows / XtQuant / QMT 下游链路本身慢。

## 5. 当前判断

截至 2026-04-16 22:05，当前分层判断如下：

1. Linux 侧此前确实存在控制面异步调度问题
   - 这一层由 Linux 继续修是对的

2. Windows 侧也确实存在实现问题
   - 主要是接口查询错误耦合
   - 这层已修复

3. 修复后剩余最慢点主要集中在下游查询本身
   - `asset` 仍稳定在 `4.5s` 左右
   - `trades` 仍可能超过 `70s`

因此当前剩余瓶颈更像：

- `query_stock_asset`
- `query_stock_trades`
- 以及更下游的 XtQuant / QMT 查询链本身

## 6. 对 Linux 侧的建议结论

可以据此调整 Linux 侧判断：

### 已可确认修掉的 Windows 问题

- `positions` 不应再默认背上 `asset` 的耗时
- `orders` 不应再默认背上 `trades` 的耗时

### 仍应继续隔离出在线链路的项目

- `trades`
- `asset`

尤其：

- `trades` 不适合继续留在在线健康检查链路
- `asset` 仍建议做缓存/降频

## 7. 结论摘要

一句话结论：

- Windows 侧确认有问题，已修一层
- Go 外层代理不是当前主要慢点
- 当前真正剩余的慢点主要在 `asset` 和尤其 `trades` 的下游 QMT 查询链
- 同时已明确禁用代理，当前本程序不会通过 Astar 等代理绕行外网再回中国
