# Windows Gateway 行情接口补充需求

> 日期：2026-04-12
> 目标：让 Linux 控制面把 `market_mode` 从 `mock` 切到 `windows_proxy`，使 `runtime -> discussion -> execution` 主流程使用真实行情，而不是模拟行情。

## 一、当前现状

当前 Windows 侧 `18791` 已验证可用的接口包括：

- `GET /health`
- `GET /qmt/account/asset`
- `GET /qmt/account/positions`
- `GET /qmt/account/orders`
- `GET /qmt/account/trades`
- `POST /qmt/trade/order`
- `POST /qmt/trade/order_status`
- `GET /qmt/quote/tick`
- `GET /qmt/quote/kline`

其中：

- 交易链已可用，Linux 控制面已切到 `execution_mode=windows_proxy`
- 行情链仍未切换，原因不是价格接口缺失，而是还缺少 `runtime` 真正依赖的股票池、板块、证券名称等元数据接口

当前 Linux 控制面真实状态：

- `execution_adapter = windows_proxy`
- `market_adapter = mock`

所以现在的真实阻塞点是：

- Windows 侧还没有满足 `MarketDataAdapter` 全量能力的行情元数据接口

## 二、Linux 侧最小能力要求

Linux 侧 `runtime/scheduler/precompute/sentiment` 当前依赖以下市场能力：

1. 实时快照
2. K 线
3. 主板股票池
4. 全 A 股票池
5. 板块列表
6. 板块成分股
7. 证券名称
8. 指数快照

目前第 1、2 项已有接口，第 3 到第 7 项缺失，第 8 项可复用 tick 接口。

## 三、需要 Windows 侧补充的接口

### 3.1 证券基础信息

#### 接口

`GET /qmt/quote/instruments?codes=600000.SH,000001.SZ`

#### 作用

返回证券名称、市场、是否主板、证券类型，供 Linux 侧：

- `get_symbol_name()`
- 主板过滤
- A 股过滤

#### 最小返回结构

```json
{
  "ok": true,
  "codes": ["600000.SH", "000001.SZ"],
  "data": {
    "600000.SH": {
      "symbol": "600000.SH",
      "name": "浦发银行",
      "market": "SH",
      "security_type": "stock",
      "board": "main"
    },
    "000001.SZ": {
      "symbol": "000001.SZ",
      "name": "平安银行",
      "market": "SZ",
      "security_type": "stock",
      "board": "main"
    }
  }
}
```

#### 字段要求

- `symbol`: 标准证券代码，如 `600000.SH`
- `name`: 中文简称
- `market`: `SH` / `SZ` / `BJ`
- `security_type`: 至少区分 `stock` / `index` / `fund` / `bond`
- `board`: 至少区分 `main` / `gem` / `star` / `beijing`

### 3.2 全 A 股票池

#### 接口

`GET /qmt/quote/universe?a_share_only=true`

#### 作用

供 Linux 侧：

- `get_a_share_universe()`
- sentiment 计算
- runtime 默认 universe

#### 最小返回结构

```json
{
  "ok": true,
  "scope": "a_share",
  "symbols": [
    "600000.SH",
    "600519.SH",
    "000001.SZ"
  ]
}
```

#### 要求

- 返回全部 A 股证券代码
- 代码格式统一为 `<6位代码>.<SH|SZ|BJ>`
- 不要混入指数、ETF、可转债

### 3.3 主板股票池

#### 接口

`GET /qmt/quote/universe?scope=main_board`

#### 作用

供 Linux 侧：

- `get_main_board_universe()`
- runtime 默认 `main-board` universe_scope

#### 最小返回结构

```json
{
  "ok": true,
  "scope": "main_board",
  "symbols": [
    "600000.SH",
    "600519.SH",
    "000001.SZ"
  ]
}
```

#### 要求

- 只返回主板股票
- 不包含创业板、科创板、北交所

### 3.4 板块列表

#### 接口

`GET /qmt/quote/sectors`

#### 作用

供 Linux 侧：

- `get_sectors()`
- runtime 生成 `sector_profile`
- discussion 汇总板块联动

#### 最小返回结构

```json
{
  "ok": true,
  "sectors": [
    "银行",
    "白酒",
    "算力",
    "机器人"
  ]
}
```

#### 要求

- 返回可用于取成分股的板块名
- 优先行业/概念板块，避免混入无意义系统分类

### 3.5 板块成分股

#### 接口

`GET /qmt/quote/sector-members?sector=银行`

#### 作用

供 Linux 侧：

- `get_sector_symbols(sector_name)`
- runtime 识别候选票所属板块

#### 最小返回结构

```json
{
  "ok": true,
  "sector": "银行",
  "symbols": [
    "600000.SH",
    "600036.SH",
    "601398.SH"
  ]
}
```

#### 要求

- 只返回证券代码数组即可
- 板块名必须与 `/qmt/quote/sectors` 返回值一致

## 四、已有接口映射要求

### 4.1 Tick 快照

已有：

`GET /qmt/quote/tick?codes=...`

Linux 侧映射需要保证以下字段稳定可用：

- `lastPrice`
- `lastClose`
- `volume`
- `bidPrice[0]`
- `askPrice[0]`

### 4.2 K 线

已有：

`GET /qmt/quote/kline?codes=...&period=1m&count=...`

Linux 侧映射需要保证以下字段稳定可用：

- `open`
- `high`
- `low`
- `close`
- `volume`
- `amount`
- `preClose`
- `trade_time` 或等价时间索引

#### period 要求

至少支持：

- `1m`
- `5m`
- `15m`
- `60m`
- `1d`

## 五、优先级

### P0

- `/qmt/quote/instruments`
- `/qmt/quote/universe?scope=main_board`
- `/qmt/quote/universe?a_share_only=true`

说明：

- 这三项补完后，Linux 侧已可切走 `mock`，跑出真实 `runtime pipeline`

### P1

- `/qmt/quote/sectors`
- `/qmt/quote/sector-members`

说明：

- 这两项补完后，`sector_profile`、板块联动、discussion 板块摘要会更完整

## 六、兼容与返回规范

所有新增接口统一要求：

- 除 `/health` 外都要求 `X-Ashare-Token`
- 返回 JSON
- 使用 `ok: true/false`
- 失败时至少返回以下之一：
  - `last_error`
  - `message`
  - `error`
  - `detail`

代码格式统一要求：

- `600000.SH`
- `000001.SZ`
- `430047.BJ`

不要返回：

- `SH600000`
- `600000`
- `000001.XSHE`

## 七、Linux 侧对接计划

Windows 侧接口补齐后，Linux 侧将执行以下动作：

1. 新增 `WindowsProxyMarketDataAdapter`
2. `tick/kline` 直接走 `18791`
3. `market_mode` 切到 `windows_proxy`
4. 重启 `serve + scheduler`
5. 验证：
   - `/system/operations/components`
   - `/runtime/jobs/pipeline`
   - `/system/discussions/summary`
   - `/system/discussions/execution-intents`

## 八、验收标准

满足以下条件即可视为 Windows 行情接口补齐完成：

1. Linux 控制面 `market_adapter` 不再是 `mock`
2. `POST /runtime/jobs/pipeline` 返回的 `market_mode` 为 `windows_proxy`
3. `runtime report` 中 `top_picks` 的价格、量能来自真实行情
4. `candidate cases` 能按真实行情生成
5. `discussion summary / execution intents` 不再依赖 mock 行情

## 九、当前结论

当前不是交易链堵塞，也不是 OpenClaw 堵塞。

当前真实阻塞点是：

- Windows `18791` 还缺少 Linux runtime 所需的行情元数据接口

本文件就是 Windows 侧的补充实现清单，补完即可继续下一轮真实联调。
