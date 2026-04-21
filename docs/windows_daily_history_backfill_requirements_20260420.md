# Windows 侧全市场日线历史补数修复要求

更新时间：2026-04-20 09:15 +08:00

## 1. 当前结论

Windows 侧上一轮修复已经让 `/qmt/quote/kline` 接口恢复可用，但问题 **没有彻底结束**。

当前真实剩余问题是：

- 接口已经能返回 `200 OK`
- 候选票如 `601778.SH` 的日线可以返回较新的历史
- 但对全市场基础采样票，如 `000001.SZ`、`000002.SZ`、`000004.SZ`、`000006.SZ`、`000007.SZ`
- `GET /qmt/quote/kline?period=1d&count=1` 仍只返回到 `2026-04-13`

这会直接导致 Linux 侧：

- `/runtime/data/health`
- `kline_freshness`

继续判定为：

- `status = stale`

所以当前状态不是“接口还挂着”，而是：

**接口通了，但全市场日线历史数据没有补到最近交易日。**

## 2. Linux 侧实测证据

实测时间：`2026-04-20 09:08 - 09:13 CST`

Linux 主控当前返回：

```json
{
  "available": true,
  "status": "degraded",
  "gateway_health": {
    "status": "healthy"
  },
  "kline_freshness": {
    "available": true,
    "sample_symbol_count": 5,
    "bar_count": 5,
    "latest_trade_time": "2026-04-13 00:00:00",
    "status": "stale"
  }
}
```

说明：

- 网关可达
- K 线接口可调用
- 但返回的最新日线时间仍旧过老

## 3. 直接复核结果

### 3.1 基础样本票 `1d count=1`

直接访问 Windows 网关：

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1"
```

实测结果：

- `200 OK`
- 但 5 只票全部只返回：
  - `trade_time = 2026-04-13 00:00:00`

这说明问题不是 Linux 缓存，而是 Windows 侧真实返回的数据就停在 `2026-04-13`。

### 3.2 单票 `000001.SZ` 的 `1d count=60`

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/kline?codes=000001.SZ&period=1d&count=60"
```

实测结果：

- 返回大量历史 bars
- 但最后一根仍停在：
  - `2026-04-13 00:00:00`

说明：

- 不是 `count=1` 特有问题
- 而是该 symbol 的日线历史本身没有补到最近交易日

### 3.3 单票 `000001.SZ` 的 `5m count=10`

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/kline?codes=000001.SZ&period=5m&count=10"
```

实测结果：

- 返回的是 `2026-04-17 14:15:00 ~ 15:00:00`
- 且 `diagnostics.download_attempted = true`
- `diagnostics.retry_hit = true`

说明：

- 分钟线数据可以通过下载或补数拿到较新结果
- 但日线数据没有触发同样的补数闭环

### 3.4 `end_time` 参数仍异常

以下请求：

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-20"
```

以及：

```bash
curl -H "X-Ashare-Token: <token>" \
"http://192.168.122.66:18791/qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-17"
```

当前返回：

```json
{
  "ok": false,
  "message": "qmt rpc failed",
  "error": "TypeError(\"'NoneType' object is not iterable\",)"
}
```

并附带 traceback，异常点位于：

- `xtdata.get_market_data(...)`

说明：

- 当前 `end_time` 分支还不稳定
- 日线补数逻辑不能依赖这个分支继续带病运行

## 4. 对系统的实际影响

这个问题会继续影响：

- Linux `/runtime/data/health`
- 盘前数据新鲜度判断
- 依赖通用日线样本的监控与健康检查
- 全市场横向筛选、基础行为画像、通用因子扫描

当前系统状态因此仍然是：

- Windows `kline` 接口：已恢复
- Linux 主控：已恢复
- 全市场 `1d` 历史：仍不完整

所以不能把当前状态定义为“数据链彻底修复完成”。

## 5. Windows 侧必须完成的修复项

### P0-1. 全市场样本票 `1d` 历史必须补到最近交易日

要求：

- 对任意普通 A 股样本票，如：
  - `000001.SZ`
  - `000002.SZ`
  - `000004.SZ`
  - `000006.SZ`
  - `000007.SZ`
- `GET /qmt/quote/kline?period=1d&count=1`
- 返回的最新 `trade_time` 必须达到最近交易日

以当前时点为例，至少不应停留在：

- `2026-04-13`

### P0-2. 日线与分钟线补数策略必须对齐

当前现象是：

- `5m` 可以通过下载 / 重试拿到较新结果
- `1d` 没有做到同样的闭环

要求：

- 明确 `1d` 数据在缺失或过旧时是否触发下载
- 明确是否存在单独的历史缓存目录
- 明确为什么 `5m` 能补、`1d` 不能补
- 修复后保证 `1d` 与 `5m` 都具备一致的“缺失即补数”能力

### P0-3. `end_time` 分支必须修复

当前：

- `end_time=2026-04-20`
- `end_time=2026-04-17`

都会触发：

- `TypeError("'NoneType' object is not iterable")`

要求：

- `/qmt/quote/kline` 带 `end_time` 时不能再抛这个异常
- 至少要做到：
  - 返回正常结果
  - 或返回可诊断、可解释的失败原因

### P0-4. 日线补数状态必须可诊断

当前虽然有 `quote_rpc.log`，但还看不出：

- 为什么 `000001.SZ` 的 `1d` 只到 `2026-04-13`
- 是否尝试过下载
- 下载调用了什么
- 下载失败还是根本没走下载

要求在日志和返回的 `diagnostics` 中增加：

- `latest_trade_time`
- `expected_trade_date`
- `history_missing`
- `daily_download_attempted`
- `daily_download_result`
- `cache_hit`
- `cache_source`

建议结构：

```json
{
  "diagnostics": {
    "handler": "qmt_rpc_py36.py",
    "method": "xtdata.get_market_data",
    "period": "1d",
    "codes_count": 1,
    "latest_trade_time": "2026-04-13 00:00:00",
    "expected_trade_date": "2026-04-17",
    "history_missing": true,
    "daily_download_attempted": true,
    "daily_download_result": "success",
    "cache_hit": false,
    "cache_source": "qmt_download"
  }
}
```

## 6. Windows 侧优先排查点

### 6.1 为什么 `1d` 不补数、`5m` 会补数

重点查：

- `qmt_rpc_py36.py` 内部 `period=1d` 分支
- 是否对日线和分钟线用了不同下载逻辑
- 是否只有分钟线触发 `download_history_data`
- 是否日线默认只读本地缓存，不主动更新

### 6.2 `xtdata.get_market_data()` 对日线的缓存来源

请确认：

- 当前日线数据到底来自：
  - 本地 QMT 历史缓存
  - 在线下载结果
  - 内部聚合缓存
- 为什么 `000001.SZ` 这种基础票还停留在 `2026-04-13`

### 6.3 `end_time` 参数兼容性

当前 traceback 已表明：

- `xtdata.get_market_data(...)`
- 在 `end_time` 场景下返回了 `None`
- 上层代码没有做兼容处理

请确认：

- `end_time` 的格式要求
- 是否必须使用 `YYYYMMDD`
- 是否必须传 `start_time`
- 是否 `count + end_time` 在当前 xtquant 版本本来就不兼容

### 6.4 是否存在“只对候选票补历史”的隐藏逻辑

当前表现很像：

- 热门票 / 候选票历史较新
- 基础样本票历史偏旧

请确认是否存在：

- 仅对最近访问票下载历史
- 仅对候选池下载历史
- 没有对全市场基准样本做统一补数

如果存在，需要改成：

- 全市场健康检查样本也具备同样的日线新鲜度保障

## 7. 要求补充的交付结果

### 7.1 本机直测结果

请提供以下请求的真实返回：

```bash
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=000001.SZ&period=1d&count=1"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=000001.SZ&period=1d&count=60"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=000001.SZ&period=5m&count=10"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=000001.SZ&period=1d&count=1&end_time=2026-04-20"
curl -H "X-Ashare-Token: <token>" "http://127.0.0.1:18791/qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1"
```

### 7.2 日线补数日志

请提供 `quote_rpc.log` 中对应时间段的原始日志，必须能看到：

- 是否尝试补数
- 是否命中缓存
- 最后一根 bar 的 `trade_time`

### 7.3 代码级修复说明

请说明：

- 哪个文件改了
- 哪个函数改了
- 修复的是下载逻辑、缓存逻辑、参数兼容、还是 `end_time` 分支

## 8. 验收标准

### 验收 1：基础样本票 `1d` 必须新鲜

以下请求返回的最新 `trade_time` 不能再停留在 `2026-04-13`：

```bash
GET /qmt/quote/kline?codes=000001.SZ,000002.SZ,000004.SZ,000006.SZ,000007.SZ&period=1d&count=1
```

### 验收 2：`end_time` 不再抛 `NoneType`

以下请求不能再返回：

- `TypeError("'NoneType' object is not iterable")`

### 验收 3：Linux 侧健康恢复

Linux 复核时应满足：

- `/runtime/data/health`
- `kline_freshness.status != stale`

### 验收 4：日志可诊断

必须能从日志中看出：

- 为什么日线旧
- 是否尝试补数
- 补数是否成功

## 9. 当前判断

当前最可能的真实问题，不在 Linux，而在 Windows 侧以下之一：

- 日线 `1d` 分支没有触发补数
- 日线缓存仍然停留在旧数据
- `end_time` 参数分支本身仍有兼容性 bug
- 当前修复只解决了“接口调用成功”，没有解决“全市场日线历史更新完整”
