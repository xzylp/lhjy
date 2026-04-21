# Windows 日线历史补数修复答复

更新时间：2026-04-20 09:25 +08:00

## 1. 结论

本次已完成 Windows 侧日线历史补数修复，`/qmt/quote/kline` 的 `1d` 路径已恢复到最近交易日，且 `end_time` 分支已修复。

当前本机 `127.0.0.1:18791` 实测结果：

- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1`
  - 最新 `trade_time = 2026-04-17 00:00:00`
- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=60`
  - 最后一根 bar 为 `2026-04-17 00:00:00`
- `GET /qmt/quote/kline?codes=000001.SZ&period=5m&count=10`
  - 最新分钟线为 `2026-04-17 15:00:00`
- `GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-20`
  - 正常返回，不再出现 `TypeError("'NoneType' object is not iterable")`
- `GET /qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1`
  - 5 只样本票最新 `trade_time` 均为 `2026-04-17 00:00:00`

因此这次不是只修复“接口能通”，而是把全市场样本票 `1d` 历史补到了最近交易日。

## 2. 根因

本次剩余问题的根因有两部分：

1. 旧逻辑只在 `get_market_data()` 返回空结果时才触发 `download_history_data()`。
2. 对于 `1d` 场景，如果本地已有旧缓存但停留在 `2026-04-13`，旧逻辑会直接把旧数据返回，不会判断“数据虽有但已过期”。
3. `end_time` 直接透传 `2026-04-20` 这种带横线格式给 `xtdata.get_market_data()`，与当前 QMT Python 3.6 运行时要求的 `YYYYMMDD` 不兼容，因此触发：

```text
TypeError("'NoneType' object is not iterable")
```

也就是说，问题不在 Linux，不在外层网关，而在 Windows 内部 `qmt_rpc_py36.py` 的日线补数触发条件和时间参数兼容处理。

## 3. 已完成修复

修改文件：

- `scripts/qmt_rpc_py36.py`

核心修复点：

1. 增加时间规范化逻辑，把 `start_time/end_time` 统一转换为 QMT 可接受的格式。
   - `1d` 使用 `YYYYMMDD`
2. 增加最近应达交易日判断。
   - 当前时间为 `2026-04-20 09:xx CST`
   - 因尚未收盘，日线应至少补到最近已完成交易日 `2026-04-17`
3. 增加日线“旧数据也补数”的逻辑。
   - 不再只在“空数据”时下载
   - 当最新 bar 早于应达交易日时，也会触发 `download_history_data()`
4. 增加日线补数诊断字段和日志字段。
5. 修复 `end_time` 分支。
   - `2026-04-20` 现会规范为 `20260420`
   - 不再触发 `NoneType` 异常

## 4. 新增诊断字段

当前 `diagnostics` 已补充：

- `normalized_start_time`
- `normalized_end_time`
- `latest_trade_time`
- `latest_trade_time_by_code`
- `expected_trade_date`
- `history_missing`
- `daily_download_attempted`
- `daily_download_result`
- `cache_hit`
- `cache_source`

示例：

```json
{
  "latest_trade_time": "2026-04-17 00:00:00",
  "expected_trade_date": "20260417",
  "history_missing": false,
  "daily_download_attempted": true,
  "daily_download_result": "success",
  "cache_hit": false,
  "cache_source": "qmt_download"
}
```

## 5. 本机验收结果

验收时间：2026-04-20 09:22 - 09:23 CST

### 5.1 单票 `1d count=1`

请求：

```bash
GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1
```

结果：

- `200 OK`
- 最新 `trade_time = 2026-04-17 00:00:00`
- 首次命中旧缓存时，自动触发日线补数
- `daily_download_attempted = true`
- `daily_download_result = success`

### 5.2 单票 `1d count=60`

请求：

```bash
GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=60
```

结果：

- `200 OK`
- 末尾 bar 已更新到 `2026-04-17 00:00:00`
- 当前缓存已新鲜，`daily_download_attempted = false`

### 5.3 单票 `5m count=10`

请求：

```bash
GET /qmt/quote/kline?codes=000001.SZ&period=5m&count=10
```

结果：

- `200 OK`
- 返回 `2026-04-17 14:15:00 ~ 15:00:00`

### 5.4 `end_time` 分支

请求：

```bash
GET /qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-20
```

结果：

- `200 OK`
- `normalized_end_time = 20260420`
- 返回最新已完成交易日 `2026-04-17 00:00:00`
- 不再出现 `TypeError("'NoneType' object is not iterable")`

### 5.5 多票样本 `1d count=1`

请求：

```bash
GET /qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1
```

结果：

- `200 OK`
- 5 只样本票最新 `trade_time` 全部达到 `2026-04-17 00:00:00`

## 6. 日志验收

`quote_rpc.log` 当前可直接看出：

- 请求是否带 `end_time`
- 规范化后的 `normalized_end_time`
- 预期交易日 `expected_trade_date`
- 是否触发日线补数
- 补数是否成功
- 最终最新 bar 时间

示例日志：

```text
2026-04-20 09:22:11,709 INFO kline success codes=000001.SZ period=1d bars=1 download_attempted=True daily_download_attempted=True daily_download_result=success retry_hit=True latest_trade_time="2026-04-17 00:00:00" expected_trade_date=20260417 cache_source=qmt_download
2026-04-20 09:22:27,733 INFO kline request codes=000001.SZ period=1d source_period=1d count=1 start_time= end_time=2026-04-20 normalized_start_time= normalized_end_time=20260420 expected_trade_date=20260417 dividend_type=none runtime=Python 3.6.8
2026-04-20 09:22:28,782 INFO kline success codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ period=1d bars=5 download_attempted=False daily_download_attempted=False daily_download_result=not_needed retry_hit=False latest_trade_time={"000001.SZ": "2026-04-17 00:00:00", "000002.SZ": "2026-04-17 00:00:00", "000004.SZ": "2026-04-17 00:00:00", "000006.SZ": "2026-04-17 00:00:00", "000007.SZ": "2026-04-17 00:00:00"} expected_trade_date=20260417 cache_source=local_cache
```

## 7. 对需求项的逐条回复

### P0-1 全市场样本票 `1d` 补到最近交易日

已完成。

当前样本票不再停留在 `2026-04-13`，已达到 `2026-04-17`。

### P0-2 `1d` 与 `5m` 补数策略对齐

已完成。

`1d` 现在不再只在空数据时补数；当检测到日线历史过旧时，也会主动触发回补。

### P0-3 `end_time` 分支修复

已完成。

带 `end_time=2026-04-20` 的请求已经恢复正常，不再抛 `NoneType`。

### P0-4 日线补数状态可诊断

已完成。

`diagnostics` 与 `quote_rpc.log` 已可明确看出：

- 最新 bar 时间
- 目标交易日
- 是否缺历史
- 是否尝试日线补数
- 补数结果
- 命中缓存还是下载结果

## 8. 当前状态

截至 `2026-04-20 09:25 CST`，Windows 侧日线历史补数问题已修复并恢复。  
Linux 侧后续复核时，`/runtime/data/health` 中的 `kline_freshness.status` 应不再停留在 `stale`。

## 9. Linux 主控复测闭环

复测时间：`2026-04-20 09:30 - 09:34 CST`

### 9.1 `/runtime/data/health`

Linux 主控实测：

```json
{
  "available": true,
  "status": "healthy",
  "gateway_health": {
    "available": true,
    "status": "healthy",
    "status_code": 200
  },
  "kline_freshness": {
    "available": true,
    "latest_trade_time": "2026-04-17 00:00:00",
    "lag_hours": 81.5464,
    "expected_trade_date": "2026-04-17",
    "status": "fresh"
  },
  "universe_coverage": {
    "available": true,
    "status": "healthy",
    "count": 5505
  }
}
```

结论：

- Linux 主控已经识别最近应完成交易日为 `2026-04-17`
- 虽然当前时间是 `2026-04-20` 盘中，`lag_hours` 很大，但不会再被误判为 `stale`
- Windows 修复结果已经真实传导到 Linux 健康检查链路

### 9.2 `/system/deployment/service-recovery-readiness`

Linux 主控实测：

```json
{
  "status": "ready",
  "summary_lines": [
    "服务恢复检查: status=ready trade_date=2026-04-20 account=8890130545。",
    "workspace_age=12.513793 latest_signal_age=12.513805 execution_plane=windows_gateway。",
    "longconn=connected readiness=degraded bridge=ok supervision_items=7。"
  ],
  "failed_checks": []
}
```

结论：

- 服务恢复检查已恢复为 `ready`
- Linux 主控、Windows 网关、飞书长连接、监督链路当前均可用

### 9.3 `go-live gate`

Linux 主控实测：

```text
[go-live-gate] BLOCKED trade_date=2026-04-20 account_id=8890130545
- 准入1_linux_services: OK
- 准入2_windows_bridge: OK
- 准入3_apply_closed_loop: NO
- 准入4_agent_chain: OK
- 准入5_feishu_delivery: OK
```

结论：

- 当前唯一未通过项是 `准入3_apply_closed_loop`
- 该项含义是：今天尚未形成真实派发成功回执
- 这不再是数据链损坏，也不是 Windows 日线历史补数故障
- 数据链、服务链、Agent 链路当前已经恢复，剩余阻断项属于真实执行闭环尚未发生
