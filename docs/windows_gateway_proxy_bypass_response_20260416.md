# Windows Gateway 代理绕开与性能回复

日期：2026-04-16

## 结论

有改善，但主要改善点不是“绕开 Astar 代理”本身，而是以下两项一起生效后带来的结果：

1. Windows 外层请求已经显式绕开系统代理，不再经美国链路回绕。
2. `18792` 交易桥已切换为 `Python 3.11`，并改为复用常驻 XtQuant Trader 会话，不再对 `asset/positions/orders` 每次重连 QMT。

当前结论是：

- 代理绕开已生效。
- 之前的大头时延主要不在代理，而在旧版桥接实现与下游 QMT 查询方式。
- 修复后，对外核心接口已经恢复到毫秒级。
- 剩余未完全解决的是 `trades`，根因仍在 QMT 成交查询本身，不是 Go 外层代理或系统代理转发。

## 当前运行形态

- 对外网关：`http://192.168.122.66:18791`
- 本机 UI：`http://127.0.0.1:18889`
- 交易桥：`127.0.0.1:18792`
- 交易桥 Python：`C:\Users\yxzzh\Desktop\XTQ\runtime\qmt_python311\pythonw.exe`
- XtQuant 来源：`C:\Users\yxzzh\Desktop\XTQ\runtime\qmt_bin_x64\Lib\site-packages\xtquant`

## 修复前后对比

修复前典型实测：

- `18791 /qmt/ping` 约 `29702ms`
- `18791 /qmt/account/asset` 约 `4524ms`
- `18791 /qmt/account/positions` 约 `655ms`
- `18791 /qmt/account/orders` 约 `1131ms`
- `18791 /qmt/account/trades` 超时

修复后最新实测：

- `18791 /qmt/ping` 约 `105ms`
- `18791 /qmt/account/asset` 约 `6.63ms`
- `18791 /qmt/account/positions` 约 `7.36ms`
- `18791 /qmt/account/orders` 约 `17.75ms`
- `18791 /status` 约 `15.89ms`
- `18791 /qmt/account/trades` 约 `20.15s` 后失败返回

直连交易桥 `18792` 的最新实测：

- `18792 /qmt/ping` 约 `446.95ms`
- `18792 /qmt/account/asset` 约 `113.43ms`
- `18792 /qmt/account/positions` 约 `44.40ms`
- `18792 /qmt/account/orders` 约 `8.19ms`
- `18792 /qmt/account/trades` 约 `20.12s` 后失败返回

## 对问题定位的判断

现在可以排除的部分：

- 不是 Astar 代理绕路导致的主要瓶颈。
- 不是 Go 外层 `18791` 的转发层导致的主要瓶颈。
- 不是旧版 Python 3.6 运行时兼容问题导致的核心接口慢。

现在仍然存在的问题：

- `trades` 查询在 QMT 成交查询侧仍不稳定。
- 但当前已将它收敛为约 `20s` 快速失败，不再像之前一样拖到约 `60s-70s`。

## 已完成的实现调整

- `18792` 交易桥从 `Python 3.6` 切换到 `Python 3.11`
- 交易桥运行时固定到项目内入口 `runtime/qmt_python311`
- XtQuant 改为优先加载 `runtime/qmt_bin_x64/Lib/site-packages`
- `asset/positions/orders` 改为复用常驻 Trader 会话
- Go 外层 `18791 /qmt/ping` 改为优先转发到 `18792 /qmt/ping`
- Go/Python 内部 HTTP 已显式禁用系统代理

## 接口可用性结论

按照当前对外约定，以下接口已经可正常对外提供：

- `GET /health`
- `GET /status`
- `GET /qmt/ping`
- `GET /qmt/account/asset`
- `GET /qmt/account/positions`
- `GET /qmt/account/orders`
- `POST /qmt/trade/order`
- `POST /qmt/trade/order_status`

当前仍需单独说明的接口：

- `GET /qmt/account/trades`
  - 已从长时间卡死优化为约 `20s` 内返回失败
  - 但仍未达到稳定成功
  - 根因判断偏向下游 QMT 成交查询能力，而非 Windows 外层代理层
