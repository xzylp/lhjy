# Windows K 线代理并发性能整改要求

文档日期：2026-04-20  
Linux 主控项目目录：`/srv/projects/ashare-system-v2`  
Windows 网关目标：`192.168.122.66:18791`  
内部 quote bridge：`127.0.0.1:18793`

## 1. 本次文档目的

本轮不是再确认 K 线“能不能返回”，而是确认：

- K 线链路在 `market_data3(v3)` 改造后，功能已经恢复
- 但 Windows 代理口 `18791` 在高并发下出现明显尾延迟恶化
- 当前问题不是“数据错误”，而是“并发性能与排队能力不足”

因此本要求文档的目标是：

- 让 Windows 侧针对 `18791 -> 18793 -> QMT` 链路做并发性能排查
- 明确必须提交的日志、压测结果和代码改造点
- 明确验收标准，避免只回“接口已通”

## 2. Linux 侧已完成的真实验证

验证时间：2026-04-20 晚间  
验证主机：当前 Linux 主控机  
当前在线事实：

- `http://127.0.0.1:18793/health` 正常
- `http://192.168.122.66:18791/health` 正常
- `http://127.0.0.1:8100/runtime/data/health` 返回 `healthy`
- `gateway_health.base_url = http://192.168.122.66:18791`
- `kline_freshness.status = fresh`

说明：

- Linux 主控实际使用的代理口是 `192.168.122.66:18791`
- 不是 Linux 本机的 `127.0.0.1:18791`
- 所以 Windows 侧需要按这个真实入口排查，不要只看 Windows 本机回环

## 3. 已确认通过的功能项

以下请求，Linux 侧都已实测成功：

### 3.1 直连 quote bridge `18793`

- `GET /qmt/quote/kline?codes=600000.SH&period=1m&count=20`
- `GET /qmt/quote/kline?codes=600000.SH&period=1d&count=20`
- `GET /qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1`
- `GET /qmt/quote/kline?codes=000001.SZ&period=5m&count=10`
- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-20`
- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-17`

### 3.2 通过 Windows 代理 `192.168.122.66:18791`

- `GET /qmt/quote/kline?codes=600000.SH&period=1m&count=20`
- `GET /qmt/quote/kline?codes=600000.SH&period=1d&count=20`
- `GET /qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1`
- `GET /qmt/quote/kline?codes=000001.SZ&period=5m&count=10`
- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-20`
- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-17`

### 3.3 已确认的底层实现事实

无论走 `18793` 还是 `18791`，返回的 `diagnostics` 都显示：

- `method = xtdata.get_client().get_market_data3(v3)`
- `python_runtime = Python 3.11.0`

这说明：

- 功能链路已经切到了 `market_data3(v3)`
- 当前问题不再是旧 `pandas/py36` 回退
- 当前问题聚焦在代理层或并发调度层

## 4. 当前真实问题

### 4.1 常规请求性能正常

Linux 侧对 `192.168.122.66:18791` 的小样本压测结果：

- `1m`：10 次，平均 `21.02ms`，P50 `18.84ms`，P95 `30.43ms`
- `1d`：10 次，平均 `23.20ms`，P50 `22.02ms`，P95 `31.76ms`

说明常规串行或轻量请求下，链路已经可用。

### 4.2 高并发下尾延迟严重恶化

Linux 侧对 `192.168.122.66:18791` 的并发梯度压测结果如下：

#### 1 worker / 20 次

- `avg = 43.18ms`
- `P95 = 47.19ms`
- `max = 502.17ms`

#### 2 workers / 20 次

- `avg = 39.36ms`
- `P95 = 51.81ms`
- `max = 54.86ms`

#### 4 workers / 20 次

- `avg = 67.15ms`
- `P95 = 85.55ms`
- `max = 87.69ms`

#### 8 workers / 40 次

- `avg = 1922.12ms`
- `P50 = 277.84ms`
- `P95 = 7865.79ms`
- `max = 8541.22ms`
- `error_count = 0`
- `all_ok = true`
- `all_count_20 = true`

### 4.3 问题性质判断

这说明当前问题是：

- 在高并发下，数据没有错
- 请求也没有失败
- 但存在严重排队或阻塞，导致尾延迟飙升到秒级

所以这是典型的并发处理退化问题，不是功能性故障。

## 5. Windows 侧重点排查范围

请 Windows 侧不要泛泛排查，要直接围绕以下四个点给结论。

### P0-1. 排查 `18791` 代理是否存在串行化

必须确认：

- `18791` 的 `kline` 请求是否经过全局互斥锁
- 是否所有 `/qmt/quote/kline` 请求被单 worker 串行处理
- 是否有单请求队列或 channel 容量过小导致堆积
- 是否存在“同一时刻只能发一个上游请求”的保护逻辑

需要给出的证据：

- `go_manager/proxy.go` 或等价实现的实际处理路径
- `kline` handler 是否有 `mutex`、单消费者 goroutine、串行 channel、同步等待逻辑
- 请求进入时间、发往 `18793` 时间、收到上游响应时间、返回客户端时间

### P0-2. 排查 `18791 -> 18793` 是否有连接复用或客户端实现问题

必须确认：

- 到 `18793` 的 HTTP client 是否复用连接
- 是否每次请求都新建 transport / client
- 是否存在过短连接、禁 keep-alive、代理转发缓冲问题
- 是否请求体/响应体读取方式导致阻塞

需要给出的证据：

- HTTP client 初始化代码
- transport 配置
- keep-alive / idle conn / max idle conn / per host 限制
- 并发时连接池行为说明

### P0-3. 排查 `18793` quote bridge 是否对 K 线内部串行化

虽然当前 `18793` 单次很快，但仍要确认：

- `qmt_rpc_py36.py` 当前虽然名字未改，但实际是不是 `Python 3.11`
- `kline` 处理函数内部是否有全局锁
- `market_data3(v3)` 外层有没有人为做单 flight 合并或串行排队
- 同时多个 `kline` 请求是否会被 Python 层串行消费

需要给出的证据：

- Python 处理入口日志
- 每个请求唯一 request_id
- `request_received_at`
- `market_data3_start_at`
- `market_data3_end_at`
- `response_sent_at`

### P0-4. 排查是否存在 QMT 侧限流或同步阻塞

如果上面两层都没有串行化，则要继续确认：

- `get_market_data3(v3)` 是否本身对同一 client 有串行锁
- 是否多请求并发时，QMT client 只能顺序出数
- 是否 quote bridge 用了单 client 并在 client 层阻塞

需要给出的证据：

- 同一时间 8 并发请求时，每个请求的 `market_data3` 调用开始/结束时间
- 如果开始时间呈串行排布，那就是内部串行化
- 如果开始时间并行、结束时间严重拉开，则说明上游本身存在处理瓶颈

## 6. 必须补充的日志字段

Windows 侧要对 `18791` 和 `18793` 两端都加日志，至少包含：

### 6.1 代理层 `18791`

- `request_id`
- `path`
- `codes`
- `period`
- `count`
- `request_received_at`
- `forward_start_at`
- `forward_end_at`
- `response_sent_at`
- `elapsed_ms`
- `upstream_elapsed_ms`
- `status_code`

### 6.2 quote bridge `18793`

- `request_id`
- `codes`
- `period`
- `count`
- `request_received_at`
- `market_data3_start_at`
- `market_data3_end_at`
- `decode_start_at`
- `decode_end_at`
- `response_sent_at`
- `elapsed_ms`
- `bars_count`
- `python_runtime`
- `method`

要求：

- 代理层与 quote bridge 的同一请求必须使用同一个 `request_id`
- 这样 Linux 侧才能对齐一条慢请求到底卡在代理还是卡在 bridge

## 7. Windows 侧必须提交的压测报告

整改后必须重新提交以下压测，不接受只给“成功返回”。

### 7.1 功能回归

必须覆盖：

- 单票 `1m`
- 单票 `1d`
- 单票 `5m`
- 多票 `1d`
- `end_time=2026-04-20`
- `end_time=2026-04-17`

### 7.2 并发压测

至少覆盖：

- `1 worker / 20 次`
- `2 workers / 20 次`
- `4 workers / 20 次`
- `8 workers / 40 次`

每组都必须给：

- 平均
- P50
- P95
- P99
- 最大值
- 错误数
- 返回 bars 完整率

### 7.3 慢请求明细

至少提供：

- 最慢 10 个请求的 `request_id`
- 每个请求在 `18791` 和 `18793` 两端的分段耗时

## 8. 验收标准

本轮整改验收标准如下。

### 功能验收

以下必须全部成立：

- `kline` 单票、多票、`5m`、`1d`、`end_time` 全部成功
- 返回结构不变
- `diagnostics.method = xtdata.get_client().get_market_data3(v3)`
- `diagnostics.python_runtime = Python 3.11.0`

### 性能验收

最低标准：

- `2 workers / 20 次` 下，P95 不高于 `80ms`
- `4 workers / 20 次` 下，P95 不高于 `120ms`
- `8 workers / 40 次` 下，P95 不高于 `500ms`
- `8 workers / 40 次` 下，P99 不高于 `1000ms`
- 不允许再出现 `7s-8s` 级尾延迟

### 稳定性验收

- 错误数必须为 `0`
- 所有请求返回 bars 数必须正确
- 压测结束后 `health` 仍正常
- 不能把数据面打到 `degraded`

## 9. 当前 Linux 侧结论

截至 2026-04-20 本轮验证，Linux 侧结论如下：

- K 线功能链路已经恢复，且真实运行在 `market_data3(v3) + Python 3.11`
- 日常轻负载性能已进入几十毫秒量级
- 当前未完成项不在“能不能取到数据”，而在“高并发下代理层尾延迟过大”

换句话说：

- 功能修复已完成
- 并发性能整改未完成

## 10. Windows 侧回复要求

Windows 侧回复时，不接受只回：

- “已经优化”
- “已经改成并发”
- “接口现在很快”

必须逐条回复：

1. `18791` 是否存在串行化，证据是什么  
2. `18791 -> 18793` 的 HTTP client 配置是什么  
3. `18793` 内部是否存在串行化，证据是什么  
4. 并发压测四组结果是什么  
5. 最慢 10 个请求卡在代理还是 bridge  
6. 是否达到本文件第 8 节的验收标准

