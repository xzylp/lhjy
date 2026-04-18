# XTQ Windows Go 接口说明

更新时间：2026-04-16 13:00 +08:00

## 1. 对外访问约定

当前 Windows QMT 网关对外统一入口：

- `base_url`: `http://192.168.122.66:18791`
- 本机回环：`http://127.0.0.1:18791`
- 管理 UI：`http://127.0.0.1:18889`
- 鉴权 Header：`X-Ashare-Token`
- Token 文件：`C:\Users\yxzzh\Desktop\XTQ\.ashare_state\ops_proxy_token.txt`
- 端点发现文件：`C:\Users\yxzzh\Desktop\XTQ\.ashare_state\ops_proxy_endpoints.json`

说明：

- Linux 侧原有调用约定未改变。
- 接口路径、请求方法、Header 名、Token 文件位置均保持不变。
- `/status` 已保持原约定的数据结构兼容。

## 2. 已开放接口

### 2.1 健康与状态

- `GET /health`
- `GET /status`
- `GET /logs/tail?name=startup|gateway|qmt_watchdog`
- `POST /actions/watchdog`
- `POST /actions/gateway`
- `POST /actions/qmt`
- `GET|POST /actions/healthcheck`

### 2.2 行情接口

- `GET /qmt/ping`
- `GET /qmt/quote/tick?codes=...`
- `GET /qmt/quote/kline?codes=...&period=...&start_time=...&end_time=...&count=...&dividend_type=...`
- `GET /qmt/quote/instruments?codes=...`
- `GET /qmt/quote/universe?scope=...&a_share_only=...`
- `GET /qmt/quote/sectors`
- `GET /qmt/quote/sector-members?sector=...`

### 2.3 账户与交易接口

- `GET /qmt/account/asset`
- `GET /qmt/account/positions`
- `GET /qmt/account/orders`
- `GET /qmt/account/trades`
- `POST /qmt/trade/order`
- `POST /qmt/trade/order_status`

## 3. 返回约定

### 3.1 `/health`

返回字段：

- `status`
- `service`
- `bind_host`
- `port`
- `service_port`
- `project_dir`
- `updated_at`

### 3.2 `/status`

兼容原需求结构，包含：

- `ok`
- `updated_at`
- `proxy`
- `qmt`
- `trade_bridge`
- `gateway_worker`
- `control_plane`
- `processes`
- `logs`

### 3.3 错误与过载保护

正常情况下不改变原协议。

高负载下新增以下运行时行为：

- 请求会先进入队列等待，不再因为单个慢请求拖死整个服务。
- 队列等待超时后，服务返回 `429`。
- 这属于过载保护策略，不属于接口协议变更。

## 4. 当前调度策略

Go 数据面已启用三层优先级与独立并发池：

- `quote` 高优先级：行情类请求
- `account_fast` 中优先级：`/qmt/account/asset`、`/qmt/account/positions`
- `trade_slow` 低优先级：`/qmt/account/orders`、`/qmt/account/trades`、下单、查单

当前项目内配置位于：

- `C:\Users\yxzzh\Desktop\XTQ\config\windows_gateway_config.json`

当前默认值：

- `quote`: workers=8, queue=32, queue_timeout_ms=4000, retries=1
- `account_fast`: workers=6, queue=24, queue_timeout_ms=15000, retries=1
- `trade_slow`: workers=4, queue=12, queue_timeout_ms=15000, retries=0
- `retry_backoff_ms`: 350

说明：

- 下单接口不自动重试，避免重复成交。
- 行情与账户快查支持失败重试。
- `/health` 不参与慢交易队列竞争。

## 5. 2026-04-16 实测结果

### 5.1 基础连通性

已验证：

- `GET /health` 返回正常，端口为 `18791`
- `GET /status` 返回正常，结构兼容原约定
- `GET /qmt/quote/instruments?codes=600000.SH,300750.SZ` 返回正常
- `GET /qmt/account/asset` 返回正常

实测行情样本：

- `600000.SH -> 浦发银行`
- `300750.SZ -> 宁德时代`

实测资产样本：

- `account_id = 8890130545`
- `total_asset = 101096.9`

### 5.2 并发与优先级验证

在同时发起 5 个 `/qmt/account/orders` 慢请求 2 秒后：

- 5 个请求均处于 `Running`
- 未出现“额外请求立即 429”
- 同时 `GET /health` 约 68ms 返回
- 同时 `GET /qmt/account/asset` 正常返回，约 5467ms 完成

结论：

- 慢交易查询已进入排队机制，而非立即拒绝
- 健康检查未被慢交易拖住
- 账户快查可与慢交易查询并行，不再被同一低优先级池完全堵死

## 6. 启动方式

项目内手动启动脚本：

- `C:\Users\yxzzh\Desktop\XTQ\start_xtq_go_manager.bat`

该脚本会默认：

- 启动 Go Manager
- 启用 Go 数据面代理
- 监听正式端口 `18791`

## 7. 结论

截至 2026-04-16，本次改造后的 Windows 侧已经满足：

- 对外继续提供原约定接口
- 与 QMT 保持可通讯
- 支持多并发外部请求接入
- 内部按优先级进行受控并发、排队与重试
- 程序与状态数据固定保存在项目目录
