# Windows K线链路修复答复

更新时间：2026-04-20 09:08 +08:00

## 1. 结论

`GET /qmt/quote/kline` 已恢复可用，已按需求完成以下三类验收：

- 单票日线：`GET /qmt/quote/kline?codes=601778.SH&period=1d&count=60`
- 单票 5 分钟线：`GET /qmt/quote/kline?codes=601778.SH&period=5m&count=10`
- 多票日线：`GET /qmt/quote/kline?codes=601778.SH,600666.SH&period=1d&count=60`

当前本机 `127.0.0.1:18791` 实测均返回 `200 OK`，`data` 直接返回 bar 数组映射，bar 字段包含：

- `open`
- `high`
- `low`
- `close`
- `volume`
- `amount`
- `preClose`
- `trade_time`

同时已补充独立日志入口：

- `GET /logs/tail?name=quote_rpc&lines=...`

## 2. 根因定位

本次故障不在 Linux，也不在外层 HTTP 转发，而在 Windows `18791` 内部调用 QMT K线 RPC 的运行时选择错误。

实际根因如下：

1. `kline` 路径被当前运行环境拉到了项目内的 Python 3.11。
2. QMT 自带 `runtime/qmt_bin_x64/Lib/site-packages` 中的 `numpy/pandas/pytz` 为 Python 3.6 时代的包。
3. `xtdata.get_market_data()` 在取 K 线时会进入 `pandas/numpy` 路径，导致在 Python 3.11 下出现兼容性失败。
4. `tick` 之所以还能成功，是因为没有走到同一条 `pandas/numpy` 依赖路径，所以表现为“tick 正常、kline 全挂”。

直接复核结果：

- `runtime/qmt_python311/python.exe` 执行 `scripts/qmt_rpc_py36.py kline` 失败
- `runtime/qmt_python36/pythonw.exe` 执行同一脚本成功

因此修复方案不是改外部接口，而是把 K线 RPC 恢复到兼容的 Python 3.6 运行时。

## 3. 已完成修复

### 3.1 运行时修复

- `scripts/qmt_rpc_py36.py` 对应的调用运行时改为优先使用 `runtime/qmt_python36/pythonw.exe`
- 保留 `18792` 交易桥的 Python 3.11 运行环境，不影响交易侧既有修复

### 3.2 返回结构修复

`kline` 成功时现在返回：

- `ok`
- `codes`
- `period`
- `source_period`
- `download_attempted`
- `data`
- `bars`
- `raw_data`
- `diagnostics`

其中：

- `data` 为直接可消费的 bar 数组映射
- `bars` 与 `data` 保持一致，兼容原有内部使用
- `raw_data` 保留原始结构，便于排障

### 3.3 错误可诊断化

失败时不再返回空 `error`，而是返回：

- `error`: 原始异常摘要
- `diagnostics`: 方法名、参数摘要、QMT 连接状态、Python 运行时、是否命中重试、是否触发下载
- `traceback`: 原始 Python traceback

### 3.4 独立日志

已新增并对外暴露 `quote_rpc` 日志：

- 本地日志文件：`C:\Users\yxzzh\Desktop\XTQ\logs\quote_rpc.log`
- 对外日志接口：`GET /logs/tail?name=quote_rpc&lines=...`

日志会记录：

- 请求参数
- 使用的 QMT 方法
- Python 运行时
- 是否触发下载/重试
- 成功条数
- 异常堆栈

## 4. 修改文件

- `scripts/qmt_rpc_py36.py`
- `ashare_system/windows_ops_proxy.py`
- `go_manager/main.go`
- `go_manager/proxy.go`

本次修改点分别为：

- `qmt_rpc_py36.py`：新增 `quote_rpc` 独立日志、K线诊断字段、失败 traceback、成功时 `data=bars`
- `windows_ops_proxy.py`：K线 RPC 改为优先走 `runtime/qmt_python36/pythonw.exe`，并暴露 `quote_rpc` 日志
- `go_manager/main.go`：补充 `quote_rpc` 日志查询入口
- `go_manager/proxy.go`：K线 RPC 优先 Python 3.6，且当子进程失败但已写出 JSON 结果时，优先透传真实诊断而不是吞掉错误

## 5. 本机验收结果

验收时间：2026-04-20 09:01 - 09:03 CST

### 5.1 单票日线

请求：

```bash
GET /qmt/quote/kline?codes=601778.SH&period=1d&count=60
```

结果：

- `200 OK`
- `ok = true`
- `codes = ["601778.SH"]`
- `period = "1d"`
- `diagnostics.python_runtime = "Python 3.6.8"`
- `diagnostics.qmt_connected = true`

### 5.2 单票 5 分钟线

请求：

```bash
GET /qmt/quote/kline?codes=601778.SH&period=5m&count=10
```

结果：

- `200 OK`
- `ok = true`
- `codes = ["601778.SH"]`
- `period = "5m"`
- `diagnostics.python_runtime = "Python 3.6.8"`
- `diagnostics.qmt_connected = true`
- 当地实测返回 10 根 bar

### 5.3 多票日线

请求：

```bash
GET /qmt/quote/kline?codes=601778.SH,600666.SH&period=1d&count=60
```

结果：

- `200 OK`
- `ok = true`
- `codes = ["601778.SH", "600666.SH"]`
- `period = "1d"`
- `diagnostics.codes_count = 2`
- `diagnostics.python_runtime = "Python 3.6.8"`
- `diagnostics.qmt_connected = true`

## 6. 数据结构对照

当前成功返回中，`data` 结构示意如下：

```json
{
  "ok": true,
  "data": {
    "601778.SH": [
      {
        "open": 6.98,
        "high": 6.99,
        "low": 6.94,
        "close": 6.95,
        "volume": 282061,
        "amount": 196579104.0,
        "preClose": null,
        "trade_time": "2026-04-17 14:15:00"
      }
    ]
  },
  "diagnostics": {
    "handler": "qmt_rpc_py36.py",
    "method": "xtdata.get_market_data",
    "codes_count": 1,
    "period": "5m",
    "count": 10,
    "qmt_connected": true,
    "python_runtime": "Python 3.6.8",
    "retry_hit": false,
    "download_attempted": false
  }
}
```

满足需求稿提出的字段规范，不再需要从 `raw_data` 二次展开后才能使用。

## 7. 日志验收

本机已验证：

```bash
GET /logs/tail?name=quote_rpc&lines=5
```

接口返回正常，可直接看到最近 K线请求日志，例如：

- `kline request codes=601778.SH period=1d ... runtime=Python 3.6.8`
- `kline success codes=601778.SH period=1d bars=60 download_attempted=False retry_hit=False`

## 8. 对需求项的逐条回复

### P0-1 修复 `/qmt/quote/kline`

已完成。

- 单票 `1d` 已恢复
- 单票 `5m` 已恢复
- 多票 `1d` 已恢复
- `data` 直接返回 bar 数组映射

### P0-2 不再返回空错误

已完成。

失败时将返回真实 `error`、`diagnostics` 与 `traceback`，不再是空字符串。

### P0-3 单独日志

已完成。

- 新增 `quote_rpc.log`
- 新增 `/logs/tail?name=quote_rpc`

## 9. 当前状态

截至 `2026-04-20 09:08 CST`，Windows 侧 K线链路已恢复到可联调用状态，且已补齐诊断与日志能力。  
Linux 侧后续只需按既有外部约定继续调用，无需改接口协议。
