# Windows Gateway 行情接口补齐答复稿
> 日期：2026-04-12
> 对应需求文档：`windows_gateway_market_api_requirements_20260412.md`

针对需求文档中提出的 Windows 侧行情元数据接口补齐事项，现答复如下：

Windows `18791` 行情代理接口已按要求完成补齐并完成本机验证，当前已具备支撑 Linux 侧将 `market_mode` 从 `mock` 切换为 `windows_proxy` 的能力。现有实现已覆盖需求文档中列出的 P0 / P1 接口，并对返回结构、代码规范、中文字段可读性及 K 线周期支持做了统一处理。

## 一、完成情况

### 1. 已补齐并开放的接口

- `GET /qmt/quote/instruments?codes=600000.SH,000001.SZ`
- `GET /qmt/quote/universe?a_share_only=true`
- `GET /qmt/quote/universe?scope=main_board`
- `GET /qmt/quote/sectors`
- `GET /qmt/quote/sector-members?sector=银行`

### 2. 已有接口保持可用

- `GET /health`
- `GET /qmt/account/asset`
- `GET /qmt/account/positions`
- `GET /qmt/account/orders`
- `GET /qmt/account/trades`
- `POST /qmt/trade/order`
- `POST /qmt/trade/order_status`
- `GET /qmt/quote/tick`
- `GET /qmt/quote/kline`

## 二、与需求文档的一致性说明

### 1. 证券代码规范

已统一使用以下格式返回证券代码：

- `600000.SH`
- `000001.SZ`
- `430047.BJ`

不会返回以下非目标格式：

- `SH600000`
- `600000`
- `000001.XSHE`

### 2. 证券基础信息接口

`/qmt/quote/instruments` 已按要求返回以下字段：

- `symbol`
- `name`
- `market`
- `security_type`
- `board`

其中：

- `market` 已区分 `SH` / `SZ` / `BJ`
- `security_type` 已区分 `stock` / `index` / `fund` / `bond`
- `board` 已区分 `main` / `gem` / `star` / `beijing`

### 3. 股票池接口

`/qmt/quote/universe?a_share_only=true`：

- 返回全量 A 股证券代码
- 不混入指数、ETF、可转债

`/qmt/quote/universe?scope=main_board`：

- 仅返回主板股票
- 不包含创业板、科创板、北交所

### 4. 板块接口

`/qmt/quote/sectors`：

- 已返回可直接用于查询成分股的行业/概念板块名称
- 已过滤纯数字项、指数风格桶、市场桶等无意义分类

`/qmt/quote/sector-members?sector=...`：

- 可按 `/qmt/quote/sectors` 返回的板块名查询成分股
- 返回值为标准证券代码数组

### 5. Tick / K 线接口

`/qmt/quote/tick` 已保持以下字段稳定可用：

- `lastPrice`
- `lastClose`
- `volume`
- `bidPrice[0]`
- `askPrice[0]`

`/qmt/quote/kline` 已保持以下字段稳定可用：

- `open`
- `high`
- `low`
- `close`
- `volume`
- `amount`
- `preClose`
- `trade_time`

已支持的周期：

- `1m`
- `5m`
- `15m`
- `60m`
- `1d`

## 三、修正项说明

本次补齐过程中，同时完成了以下修正：

- 修复了证券中文名称乱码问题
- 修复了板块中文名称乱码问题
- 对板块清单进行了可用性过滤，确保 Linux 侧可直接消费
- 对 `15m`、`60m` K 线进行了聚合补齐

## 四、当前对外访问方式

当前服务监听在：

- `0.0.0.0:18791`

局域网对外访问地址为：

- `http://192.168.122.66:18791`

鉴权方式：

- Header：`X-Ashare-Token`

token 文件路径：

- `C:\Users\yxzzh\Desktop\XTQ\.ashare_state\ops_proxy_token.txt`

如需联调，可直接按上述地址和鉴权头进行请求。

## 五、已完成的本机验证

已完成以下实测验证：

- `/qmt/quote/instruments?codes=600000.SH,300750.SZ`
- `/qmt/quote/universe?a_share_only=true`
- `/qmt/quote/universe?scope=main_board`
- `/qmt/quote/sectors`
- `/qmt/quote/sector-members?sector=银行`
- `/qmt/quote/sector-members?sector=白酒`
- `/qmt/quote/sector-members?sector=算力`
- `/qmt/quote/sector-members?sector=机器人`
- `/qmt/quote/sector-members?sector=人工智能`
- `/qmt/quote/kline?codes=600000.SH&period=15m&count=3`
- `/qmt/quote/kline?codes=600000.SH&period=60m&count=3`

验证结果均符合当前需求范围。

## 六、结论

Windows `18791` 行情接口补齐工作已完成，需求文档中要求的市场元数据接口已实现，数据结构与代码规范已对齐，对外接口已可用于 Linux 侧 `windows_proxy market adapter` 的真实接线验证。

如 Linux 侧开始联调，可直接进入以下阶段：

1. 接入 `WindowsProxyMarketDataAdapter`
2. 将 `market_mode` 切换为 `windows_proxy`
3. 重启 `serve + scheduler`
4. 验证 `runtime/discussion/execution` 全链路真实行情流转
