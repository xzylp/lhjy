# Offline Backtest Metrics 导出契约

## 目标

定义 `ashare_system.backtest.metrics` 在离线回测场景下的维度指标输出，供后续 API、报告或文档层直接消费。

本契约只覆盖 `offline_backtest metrics`，不涉及线上事实归因，也不替代 `learning attribution`。

## 边界

- 该模块用于离线回测绩效拆分。
- 关注维度：
  - `playbook`
  - `regime`
  - `exit_reason`
- 输出适用于：
  - 回测 review
  - 报告摘要
  - API 透传
- 不适用于：
  - 线上成交事实归因
  - 学习闭环结论沉淀

## 顶层对象

`BacktestMetrics`

关键字段：

- `metrics_scope`
  - 固定为 `offline_backtest_metrics`
- `semantics_note`
  - 明确说明“这是 offline_backtest metrics，不代表线上真实成交后的事实归因”
- `total_return`
- `annual_return`
- `sharpe_ratio`
- `max_drawdown`
- `calmar_ratio`
- `win_rate`
- `profit_loss_ratio`
- `total_trades`
- `winning_trades`
- `losing_trades`
- `by_playbook_metrics`
- `by_regime_metrics`
- `by_exit_reason_metrics`
- `export_payload`

兼容字段仍保留：

- `win_rate_by_playbook`
- `avg_return_by_regime`
- `exit_reason_distribution`
- `calmar_by_playbook`

## 统一维度指标

`by_playbook_metrics / by_regime_metrics / by_exit_reason_metrics` 使用统一结构：

- `key`
- `trade_count`
- `winning_trades`
- `losing_trades`
- `flat_trades`
- `win_rate`
- `avg_return_pct`
- `total_return_pct`
- `profit_loss_ratio`
- `annual_return`
- `max_drawdown`
- `calmar_ratio`
- `sample_symbols`

说明：

- `by_exit_reason_metrics` 额外包含 `ratio`
- 这是离线回测维度绩效，不是线上归因标签

## export_payload

推荐直接透传 `metrics.export_payload`，结构至少包含：

- `metrics_scope`
- `semantics_note`
- `overview`
- `by_playbook`
- `by_regime`
- `by_exit_reason`

其中 `overview` 建议字段：

- `total_trades`
- `winning_trades`
- `losing_trades`
- `win_rate`
- `total_return`
- `annual_return`
- `sharpe_ratio`
- `max_drawdown`
- `calmar_ratio`

## API-ready 最小示例

```json
{
  "metrics_scope": "offline_backtest_metrics",
  "semantics_note": "这是 offline_backtest metrics，用于离线回测绩效拆分，不代表线上真实成交后的事实归因。",
  "overview": {
    "total_trades": 3,
    "winning_trades": 2,
    "losing_trades": 1,
    "win_rate": 0.666667,
    "total_return": 0.02,
    "annual_return": 2.474294,
    "sharpe_ratio": 4.941492,
    "max_drawdown": -0.019417,
    "calmar_ratio": 127.430473
  },
  "by_playbook": [
    {
      "key": "leader_chase",
      "trade_count": 2,
      "winning_trades": 1,
      "losing_trades": 1,
      "flat_trades": 0,
      "win_rate": 0.5,
      "avg_return_pct": 0.015,
      "total_return_pct": 0.03,
      "profit_loss_ratio": 2.5,
      "annual_return": 40.233839,
      "max_drawdown": -0.02,
      "calmar_ratio": 2011.691965,
      "sample_symbols": ["600519.SH", "000001.SZ"]
    }
  ],
  "by_regime": [
    {
      "key": "trend",
      "trade_count": 2,
      "winning_trades": 1,
      "losing_trades": 1,
      "flat_trades": 0,
      "win_rate": 0.5,
      "avg_return_pct": 0.015,
      "total_return_pct": 0.03,
      "profit_loss_ratio": 2.5,
      "annual_return": 40.233839,
      "max_drawdown": -0.02,
      "calmar_ratio": 2011.691965,
      "sample_symbols": ["600519.SH", "000001.SZ"]
    }
  ],
  "by_exit_reason": [
    {
      "key": "time_stop",
      "trade_count": 2,
      "winning_trades": 2,
      "losing_trades": 0,
      "flat_trades": 0,
      "win_rate": 1.0,
      "avg_return_pct": 0.03,
      "total_return_pct": 0.06,
      "profit_loss_ratio": 0.0,
      "annual_return": 1528.588986,
      "max_drawdown": 0.0,
      "calmar_ratio": 0.0,
      "sample_symbols": ["600519.SH", "600036.SH"],
      "ratio": 0.666667
    }
  ]
}
```

## 消费建议

1. API 层优先透传 `export_payload`，不要重复拼装维度字段。
2. 报告层优先用：
   - `overview` 做总览
   - `by_playbook / by_regime / by_exit_reason` 做拆分表
3. 若后续需要和 attribution 联动展示，应在 UI 或 API 聚合层组合，不要把 metrics 和 attribution 混成同一语义对象。
