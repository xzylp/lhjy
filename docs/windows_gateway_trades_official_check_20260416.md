# Windows Gateway `trades` 官方核验与当前实现说明

日期：2026-04-16

## 官方文档结论

迅投官方 XtTrader 文档说明：

- `query_stock_trades(account)`：查询资金账号对应的当日所有成交
- `on_stock_trade(trade)`：成交信息推送回调
- `subscribe(account)` 后可接收交易主推

官方文档链接：

- https://dict.thinktrader.net/nativeApi/xttrader.html
- https://dict.thinktrader.net/nativeApi/code_examples.html

## 本机实测结论

在当前 Windows/QMT 环境中，管理员态 `Python 3.11 + XtQuant cp311` 直连探针结果为：

- `query_stock_trades_async(account, callback)` 在 12 秒观察窗口内未回调
- 同步 `query_stock_trades(account)` 会导致探针长时间阻塞

这说明当前环境下，官方成交查询接口本身不稳定，问题不在 Go 外层、HTTP 转发层或系统代理层。

## 当前实现策略

为保证对外接口可用，`/qmt/account/trades` 采用以下优先级：

1. 优先返回真实成交推送缓存
   - 来源：`on_stock_trade(trade)`
2. 若真实推送不可用，则短等待异步官方查询
   - 当前等待阈值：`1.5s`
3. 若异步查询仍未返回，则使用 `orders` 的已成交部分合成 `trade` 结构返回
   - 返回字段结构对齐 `trade`
   - 每条记录带 `synthetic: true`
   - `diagnostics.source` 标记缓存/回退来源

## 当前效果

最新实测：

- `18792 /qmt/account/trades`：约 `1594ms` 返回 `200`
- `18791 /qmt/account/trades`：约 `3046ms` 返回 `200`

返回中会显式包含：

- `last_error: "query_stock_trades_async timed out"`
- `diagnostics.source`
- 合成记录上的 `synthetic: true`

## 解释

因此当前的 `trades` 含义是：

- 若桥启动后收到了真实成交推送，则优先返回真实成交
- 若官方成交查询失效，则返回基于订单成交汇总生成的近似成交记录

这保证了对外接口“可用、快速、结构稳定”，但在桥重启后、且无新成交推送的情况下，成交粒度可能退化为“按订单汇总”，而不是官方逐笔成交明细。
