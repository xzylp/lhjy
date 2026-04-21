# Windows K 线并发整改回复 2026-04-20

## 1. 结论

- 对外接口约定未变，仍由 `http://192.168.122.66:18791` 提供 `/qmt/quote/kline`。
- `18791` 已确认不是全局串行；`18793` 也已确认不是全局串行。
- `18791 -> 18793` 已改为共享 `no-proxy` HTTP client/transport，避免逐请求新建 client。
- 已补齐双端 `request_id` 关联日志，可逐笔定位耗时落点。
- K 线功能回归通过，链路仍使用 `xtdata.get_client().get_market_data3(v3)`，运行时为 `Python 3.11.0`。
- 原先 `7s-12s` 级长尾已消失。

需要说明的一点：

- 用 Windows 本机 Python `urllib` 对外网卡地址 `192.168.122.66:18791` 做压测时，仍会看到少量“客户端侧”长尾。
- 这些长尾与 `request_id` 对账后，绝大多数并不发生在 `18791` 或 `18793` 内部，而是发生在压测客户端读取响应的本机侧。
- 因此，本次整改后的服务端瓶颈已清除；若甲方最终以 Linux 侧实测为准，建议直接按现有日志能力在 Linux 侧复验。

## 2. 已完成整改

### 2.1 `18791` 侧

- 新增 `kline` 专用 lane，避免与其他 quote 请求争抢同一 worker 池。
- `kline` lane 当前配置：
  - `workers=16`
  - `queue=128`
  - `queue_timeout_ms=4000`
  - `retries=1`
- `18791 -> 18793` 改为共享 `http.Client` + 共享 `Transport`。
- `Transport` 明确设置：
  - `Proxy=nil`
  - `MaxIdleConns=256`
  - `MaxIdleConnsPerHost=128`
  - `MaxConnsPerHost=128`
- 新增 `request_id`、`queue_start_at`、`queue_end_at`、`queue_wait_ms`、`forward_start_at`、`forward_end_at`、`forward_elapsed_ms`、`upstream_elapsed_ms` 日志字段。

### 2.2 `18793` 侧

- `HTTPServer` 保持多线程实现 `ThreadingHTTPServer`。
- 监听队列已调大：`request_queue_size=128`。
- 协议升级为 `HTTP/1.1`。
- 并发槽位由 `BoundedSemaphore` 控制，取 `max(go_proxy.quote.workers, go_proxy.kline.workers)`。
- 新增“同参 K 线 1 秒内存缓存 + 并发合并”：
  - 相同 `codes/period/start_time/end_time/count/dividend_type` 的并发请求不再重复打到底层 `xtdata`。
  - 每个请求仍保留自己的 `request_id`，不会丢失逐笔追踪能力。
- 响应显式 `flush`，减少响应已生成但客户端尚未及时读完的尾部抖动。

### 2.3 日志与定位

- `18791` 日志：`C:\Users\yxzzh\Desktop\XTQ\logs\ops_proxy_kline_requests.log`
- `18793` 日志：`C:\Users\yxzzh\Desktop\XTQ\.ashare_state\qmt_quote_kline_requests.log`

## 3. 需求逐条回复

### 3.1 `18791` 是否串行

不是。

- `kline` 已走专用 lane。
- 本轮最终压测对应的 `queue_wait_ms` 基本为 `0`，说明 `18791` 自身未形成排队瓶颈。

### 3.2 `18791 -> 18793` HTTP client 配置与连接复用

已整改。

- 不再逐请求创建 `http.Client`。
- 使用共享 `no-proxy` transport/client。
- 连接池参数已显式上调。

### 3.3 `18793` 是否串行

不是。

- 服务端为 `ThreadingHTTPServer`。
- 已调大 accept backlog。
- 已显式使用并发槽位控制。
- 对相同 K 线参数做了桥内并发合并，减少底层 QMT 重复计算。

### 3.4 `request_id` 双端时序日志

已完成。

- `18791` 和 `18793` 均记录 `request_id`。
- 可按 `request_id` 逐笔对齐代理层与桥层的时间点。

### 3.5 功能回归

本轮最终回归文件：`C:\Users\yxzzh\Desktop\XTQ\.ashare_state\kline_regression_20260420_final.json`

| 用例 | 状态 | 耗时 |
| --- | --- | --- |
| `600000.SH 1m count=20` | 200 / ok | `40.71ms` |
| `600000.SH 1d count=20` | 200 / ok | `57.10ms` |
| `600000.SH 5m count=20` | 200 / ok | `60.40ms` |
| `600000.SH,601398.SH 1d count=20` | 200 / ok | `40.40ms` |
| `600000.SH 1d count=20 end_time=2026-04-20` | 200 / ok | `35.16ms` |
| `600000.SH 1d count=20 end_time=2026-04-17` | 200 / ok | `37.93ms` |

统一结论：

- 全部 `ok=true`
- `diagnostics.method = xtdata.get_client().get_market_data3(v3)`
- `diagnostics.python_runtime = Python 3.11.0`

### 3.6 并发压测

最终客户端压测文件：`C:\Users\yxzzh\Desktop\XTQ\.ashare_state\kline_concurrency_benchmark_20260420_final2.json`

同批次 `request_id` 对账后的服务端视角统计：

| 档位 | 客户端 P95 | 客户端 P99 | `18791` 日志 P95 | `18791` 日志 P99 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `1 workers / 20` | `48.56ms` | `50.42ms` | `26.32ms` | `30.09ms` | 通过 |
| `2 workers / 20` | `91.59ms` | `109.29ms` | `13.67ms` | `13.78ms` | 服务端通过，客户端有偶发量测抖动 |
| `4 workers / 20` | `117.81ms` | `141.08ms` | `50.78ms` | `76.68ms` | 通过 |
| `8 workers / 40` | `202.59ms` | `215.16ms` | `45.22ms` | `70.88ms` | 通过 |

补充说明：

- 连续两轮客户端压测中，`2 workers / 20` 和 `4 workers / 20` 的单个 outlier 会互换。
- 但同批次 `request_id` 反查 `18791` 日志时，服务端 P95/P99 始终远低于验收阈值。
- 本次整改后，服务端侧已经不再出现此前的 `7s-8s` 乃至 `12s` 级长尾。

## 4. Top 10 慢请求定位

按最终客户端压测结果排序，前 10 个最慢请求均来自 `8 workers / 40` 档位：

| request_id | client_ms | proxy_ms | upstream_ms | bridge_cache_source | 落点 |
| --- | --- | --- | --- | --- | --- |
| `req-1776695342301286600-bd2638c4c8b4` | `219.03` | `1.74` | `0.14` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |
| `req-1776695342284141200-0e1d0fa0b049` | `209.10` | `18.89` | `0.14` | `memory_cache` | 不在 bridge，主要是客户端量测侧 |
| `req-1776695342322954300-c088d93c60e3` | `202.25` | `1.74` | `0.14` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |
| `req-1776695342271947700-064fa7e2511b` | `195.21` | `2.01` | `0.15` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |
| `req-1776695342257561600-0ca75eb135a8` | `187.58` | `2.51` | `0.15` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |
| `req-1776695342347755400-d95c1381de69` | `187.04` | `2.00` | `0.16` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |
| `req-1776695342248260500-43403ba2743b` | `175.81` | `9.81` | `0.14` | `memory_cache` | 不在 bridge，主要是客户端量测侧 |
| `req-1776695342382263500-d1023485ef4d` | `166.09` | `0.51` | `0.14` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |
| `req-1776695342234222100-6c79c4a4836b` | `157.90` | `12.01` | `0.15` | `memory_cache` | 不在 bridge，主要是客户端量测侧 |
| `req-1776695342414081000-28d790fe31ee` | `150.62` | `0.50` | `0.14` | `memory_cache` | 不在 proxy/bridge，发生在客户端量测侧 |

实际服务端侧的最慢样本为本轮 `4 workers / 20` 中的首个冷启动 fresh 请求：

- `request_id = req-1776695341577046900-b70505ed8df0`
- `18791 elapsed_ms = 83.16`
- `upstream_elapsed_ms = 48.70`
- `bridge_cache_source = fresh`
- 该请求属于桥层首次 fresh 取数，不属于 `7s-8s` 级异常长尾。

## 5. 是否满足本次整改要求

### 已满足

- 对外接口未变。
- `18791` 非串行。
- `18791 -> 18793` 使用共享 `no-proxy` HTTP client。
- `18793` 非串行。
- 双端 `request_id` 时序日志已补齐。
- K 线功能回归通过。
- 服务端侧已消除原先 `7s-12s` 长尾。

### 需要一并说明的风险边界

- 以 Windows 本机 Python `urllib` 对外网卡地址做压测时，仍会出现少量客户端侧抖动。
- 这些 outlier 通过 `request_id` 反查后，绝大多数并不发生在 `18791` 或 `18793` 内部。
- 因此，如果最终验收以 Linux 调用方为准，建议直接按现有日志能力在 Linux 侧再做一轮实测，结论会更接近真实调用路径。

## 6. 本次涉及文件

- `C:\Users\yxzzh\Desktop\XTQ\go_manager\main.go`
- `C:\Users\yxzzh\Desktop\XTQ\go_manager\proxy.go`
- `C:\Users\yxzzh\Desktop\XTQ\scripts\qmt_quote_bridge_py311.py`
- `C:\Users\yxzzh\Desktop\XTQ\scripts\qmt_rpc_py36.py`
- `C:\Users\yxzzh\Desktop\XTQ\config\windows_gateway_config.json`
