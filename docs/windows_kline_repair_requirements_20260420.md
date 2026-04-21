# Windows 侧 K 线链路修复要求

更新时间：2026-04-20 08:35 +08:00

## 1. 问题结论

截至 `2026-04-20 08:35 CST`，A 股系统的行情链路不是整体不可用，而是 Windows QMT 网关内部的 K 线 RPC 单点故障。

已经确认：

- `GET /qmt/quote/universe` 正常
- `GET /qmt/quote/tick` 正常
- `GET /qmt/quote/instruments` 正常
- `GET /qmt/quote/kline` 全部失败
- Linux 主控 `127.0.0.1:18793` 的 `kline` 失败，本质上是上游 Windows 网关 `192.168.122.66:18791` 返回 `502 qmt rpc failed`
- 直接带鉴权访问 Windows 网关，`kline` 仍失败，说明不是 Linux 主控参数拼装问题，不是鉴权问题，不是网络连通性问题

结论明确：

**故障点在 Windows `18791` 网关内部调用 QMT 的 K 线能力时失败。**

## 2. 今日实测证据

实测时间：`2026-04-20 08:33 - 08:35 CST`

直接访问 Windows 网关：

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/tick?codes=601778.SH"
```

结果：

- `200 OK`
- 返回正常 tick 数据

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/kline?codes=601778.SH&period=1d&count=60"
```

结果：

- `502 Bad Gateway`
- 返回：

```json
{"ok":false,"message":"qmt rpc failed","error":""}
```

进一步验证：

- 单票失败
- 多票失败
- `1d` 失败
- `5m` 失败

说明不是单个 symbol 脏数据，也不是某个周期不支持，而是整个 kline RPC 能力都坏了。

Linux 主控日志也一致：

```text
GET /qmt/quote/kline -> 504 Gateway Timeout
upstream error: 502 - {"ok":false,"message":"qmt rpc failed","error":""}
falling back to windows_proxy
GET http://192.168.122.66:18791/qmt/quote/kline -> 502 Bad Gateway
```

## 3. 对系统的实际影响

这个问题会直接影响：

- `/runtime/data/health` 的 `kline_freshness`
- 盘前数据新鲜度检查
- 因子计算中依赖 `get_bars()` 的部分
- runtime 选股、factor monitor、playbook 计算
- 盘前/盘中任何依赖日线或分钟线的策略逻辑

当前系统虽然还能运行，但属于：

- 服务正常
- 账户正常
- 桥接正常
- **K 线数据不可用**

因此不能视为完整可交易态。

## 4. Windows 侧必须完成的修复项

### P0-1. 修复 `/qmt/quote/kline` 的底层 RPC

要求：

- 恢复以下接口可用：
  - `GET /qmt/quote/kline?codes=601778.SH&period=1d&count=60`
  - `GET /qmt/quote/kline?codes=601778.SH&period=5m&count=10`
  - `GET /qmt/quote/kline?codes=601778.SH,600666.SH&period=1d&count=60`
- 返回 `200 OK`
- `data` 中必须有 K 线数组或映射，字段至少包含：
  - `open`
  - `high`
  - `low`
  - `close`
  - `volume`
  - `amount`
  - `preClose`
  - `trade_time`

### P0-2. 不允许继续返回空错误

现在接口只返回：

```json
{"ok":false,"message":"qmt rpc failed","error":""}
```

这不可排障。必须改成可诊断错误，至少返回：

- 调用的底层函数名
- 原始异常字符串
- 参数摘要
- QMT 连接状态
- 使用的 Python 运行时
- 是否命中重试
- 上游脚本路径

建议返回结构：

```json
{
  "ok": false,
  "message": "qmt rpc failed",
  "error": "xtdata.get_market_data_ex raised TypeError: ...",
  "diagnostics": {
    "handler": "qmt_rpc_py36.py",
    "method": "get_market_data_ex",
    "codes_count": 1,
    "period": "1d",
    "count": 60,
    "start_time": "",
    "end_time": "",
    "dividend_type": "",
    "qmt_connected": true,
    "python_runtime": "3.6.x"
  }
}
```

### P0-3. 为 `kline` 单独打日志

必须在 Windows 侧日志里明确记录：

- 收到的请求参数
- 调用的 QMT 方法
- 失败前后的异常栈
- QMT 返回值类型
- 是否为空 dataframe / 空 dict / 抛异常
- 重试次数

日志文件建议能从以下入口直接看到最近 200 行：

- `/logs/tail?name=gateway`
- 或新增 `/logs/tail?name=quote_rpc`

## 5. Windows 侧优先排查点

请按下面顺序排查，不要泛泛看网络。

### 5.1 先确认 `kline` 走的是哪段代码

根据现有文档，行情链路应为：

- `18791 Go Proxy`
- `scripts/qmt_rpc_py36.py`
- QMT Python 3.6 运行时

请确认：

- 当前线上 `18791` 的 `/qmt/quote/kline` 是否真的走到 `qmt_rpc_py36.py`
- 还是已经切到别的 Python / 桥接实现
- `tick` 与 `kline` 是否走了不同的底层函数

### 5.2 检查底层是否调用了错误的 xtquant / xtdata 方法

重点看：

- `xtdata.get_market_data_ex(...)`
- `xtdata.get_market_data(...)`
- 参数名是否兼容当前 QMT / xtquant 版本
- `period` 是否需要映射
- `count/start_time/end_time/dividend_type` 是否有一项触发异常
- 单票、分钟线、日线是否都在同一函数里失败

### 5.3 检查运行时是否只剩 tick 可用、bars 不可用

当前 `/qmt/ping` 显示：

- `market_probe.connected = true`

但这只能证明行情探针活着，不能证明 `bars/kline` 能取到。

所以请单独在 Windows 本机直接执行：

- 一次 tick 拉取
- 一次单票 `1d`
- 一次单票 `5m`
- 一次多票 `1d`

确认是哪一种返回异常。

### 5.4 检查 QMT / Python 运行时兼容性

怀疑点包括：

- QMT 升级后 `get_market_data_ex` 行为变了
- Python 3.6 运行时的 xtquant 包损坏或版本不一致
- 运行时路径错了，tick 用的是一个能力，kline 用的是另一个失效能力
- QMT 已登录，但历史 / K 线模块没准备好
- K 线接口需要先下载 / 同步历史数据，而当前脚本没有处理

### 5.5 检查 Go Proxy 对 `kline` 的超时 / 重试包装

Linux 侧看到的是：

- `18793` 返回 `504`
- 内层是 `18791` 返回 `502`

这说明：

- Go Proxy 自己没有卡死
- 而是内部拿到了明确失败

请确认：

- quote lane 是否对 `kline` 做了错误重试
- 是否吞掉了原始错误
- 是否把非超时错误统一压扁成 `qmt rpc failed`

## 6. 要求补充的本机排障结果

Windows 侧修复时，请一次性交付这些结果。

### 6.1 本机直测结果

提供以下命令结果截图或原始返回：

```bash
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/tick?codes=601778.SH"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=601778.SH&period=1d&count=60"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=601778.SH&period=5m&count=10"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=601778.SH,600666.SH&period=1d&count=60"
```

### 6.2 原始异常日志

必须给出 `kline` 失败时的完整错误，不接受只有：

- `qmt rpc failed`

必须看到：

- Python traceback
- xtdata / xtquant 抛出的真实异常
- 参数明细

### 6.3 代码级修复说明

要说明改了什么：

- 哪个文件
- 哪个函数
- 修复的是方法名、参数、兼容逻辑、超时、还是运行时路径

## 7. 修复验收标准

### 验收 1：接口恢复

以下请求必须全部 `200 OK`：

- 单票 `1d`
- 单票 `5m`
- 多票 `1d`

### 验收 2：字段完整

每根 bar 至少有：

- `open`
- `high`
- `low`
- `close`
- `volume`
- `amount`
- `preClose`
- `trade_time`

### 验收 3：Linux 侧恢复

Linux 侧复核时应满足：

- `/runtime/data/health` 中 `kline_freshness.status != degraded`
- 盘前数据新鲜度不再因 `kline` 报错
- `market_adapter.get_bars()` 可正常返回数据

### 验收 4：错误可诊断

即使未来再次失败，也必须返回可定位信息，不能再是空 `error`。

## 8. 当前判断

当前最可能的真实问题，不在 Linux，而在 Windows 侧以下之一：

- `qmt_rpc_py36.py` 内部的 K 线函数失效
- `xtdata.get_market_data_ex` 调用参数已与当前环境不兼容
- K 线能力依赖的历史数据 / 运行时未准备好
- Go Proxy 把底层异常吞掉，只暴露成 `qmt rpc failed`
