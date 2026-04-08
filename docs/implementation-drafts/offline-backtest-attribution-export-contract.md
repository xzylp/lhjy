# Offline Backtest Attribution 导出契约

## 目标

固化 `ashare_system.backtest.attribution` 的离线导出字段，供后续 `system API`、`report` 或文档层直接消费。

本契约只描述离线回测统计输出，不描述线上事实成交后的归因闭环。

## 边界

- 该模块是 `offline_backtest attribution`
  - 输入是离线 `trade list`，或由 `backtest.engine.BacktestResult` 适配出的最小成交样本。
  - 输出是离线统计、对比视图和 review 友好的摘要。
- 该模块不是 `learning attribution`
  - 不负责线上成交事实落库。
  - 不负责执行后复盘闭环、学习标签沉淀或治理链路归因。
  - 不应替代 `ashare_system.learning.attribution`。

## 顶层返回

`BacktestAttributionReport`

关键字段：

- `available`
  - 是否有匹配样本。
- `attribution_scope`
  - 固定为 `offline_backtest`。
- `semantics_note`
  - 明确说明“这是离线回测 attribution，不代表线上真实成交后的事实归因”。
- `filters`
  - 当前筛选条件，支持：
    - `playbook`
    - `regime`
    - `exit_reason`
- `trade_count`
- `win_rate`
- `avg_return_pct`
- `total_return_pct`
- `overview`
- `by_playbook`
- `by_regime`
- `by_exit_reason`
- `weakest_buckets`
- `compare_views`
- `selected_weakest_bucket`
- `selected_compare_view`
- `export_payload`
- `items`
- `summary_lines`

## overview

`overview` 用于 API 或报告层直接展示全局摘要。

建议字段：

- `attribution_scope`
- `semantics_note`
- `trade_count`
- `win_rate`
- `avg_return_pct`
- `total_return_pct`
- `filters`
- `weakest_bucket_count`
- `compare_view_dimensions`
- `selected_weakest_bucket`
- `selected_compare_view`

## 聚合桶

`by_playbook / by_regime / by_exit_reason` 使用统一桶结构：

- `key`
- `trade_count`
- `win_count`
- `loss_count`
- `flat_count`
- `win_rate`
- `avg_return_pct`
- `total_return_pct`
- `sample_symbols`

## weakest_buckets

`weakest_buckets` 用于快速识别每个维度当前最弱分桶。

统一字段：

- `dimension`
  - `playbook` / `regime` / `exit_reason`
- `key`
- `trade_count`
- `win_rate`
- `avg_return_pct`
- `total_return_pct`
- `sample_symbols`

排序约定：

- 默认按 `avg_return_pct` 从弱到强排序。
- 返回值可直接用于 review 页的“弱点清单”或风险提示区。
- 可通过构建参数直接指定：
  - `weakest_bucket_dimension`
  - `weakest_bucket_sort_by`
  - `weakest_bucket_sort_order`

若上层只想直接拿某一个维度的最弱桶，优先消费：

- `selected_weakest_bucket`

## compare_views

`compare_views` 用于最小对比展示，面向后续 API 或报告层的维度对比卡片。

当前支持：

- `compare_views.playbook`
- `compare_views.regime`
- `compare_views.exit_reason`

每个 compare view 建议字段：

- `dimension`
- `bucket_count`
- `compared_keys`
- `best_bucket`
- `weakest_bucket`
- `spread_return_pct`
- `buckets`

可通过构建参数直接指定：

- `compare_view_dimension`
- `compare_bucket_sort_by`
- `compare_bucket_sort_order`

若上层只想直接拿某一个维度的对比视图，优先消费：

- `selected_compare_view`

用途：

- `best_bucket` / `weakest_bucket`
  - 直接渲染“最好 vs 最弱”对比。
- `spread_return_pct`
  - 用于快速判断维度分化强弱。
- `buckets`
  - 作为明细列表或图表数据源。

## export_payload

`export_payload` 是面向后续接入层的稳定导出包，建议至少包含：

- `overview`
- `summary_lines`
- `by_playbook`
- `by_regime`
- `by_exit_reason`
- `weakest_buckets`
- `selected_weakest_bucket`
- `compare_views`
- `selected_compare_view`
- `items`

推荐消费方式：

1. `system API`
   - 直接透传 `export_payload` 或其子集。
   - 避免在 API 层重复拼装 `weakest_buckets / compare_views`。
   - 若只需单一维度输出，直接使用 `selected_weakest_bucket / selected_compare_view`，不再二次筛整包。
2. `report` 层
   - `summary_lines` 用于文字摘要。
   - `weakest_buckets` 用于“弱点观察”。
   - `compare_views` 用于对比卡片或表格。
3. 文档层
   - 直接引用 `overview` 和 `compare_views` 字段，减少重复解释。

## 与 playbook runner 的关系

- `backtest.playbook_runner`
  - 负责过滤样本、接底层 `backtest.engine` 输出、返回最小回测结果。
- `backtest.attribution`
  - 负责按 `playbook / regime / exit_reason` 生成聚合、弱点分桶和对比视图。

两者都属于离线回测层，不应混入线上学习闭环语义。

## API-ready 最小示例

下面示例展示后续 `system API` 可直接透传的最小结构：

```json
{
  "overview": {
    "attribution_scope": "offline_backtest",
    "semantics_note": "这是离线回测 attribution，用于样本统计与策略复盘，不代表线上真实成交后的事实归因。",
    "trade_count": 3,
    "win_rate": 0.666667,
    "avg_return_pct": 0.013333,
    "total_return_pct": 0.04,
    "filters": {
      "weakest_bucket_dimension": "regime",
      "compare_view_dimension": "playbook",
      "weakest_bucket_sort_by": "key",
      "weakest_bucket_sort_order": "desc",
      "compare_bucket_sort_by": "avg_return_pct",
      "compare_bucket_sort_order": "desc"
    },
    "weakest_bucket_count": 3,
    "compare_view_dimensions": ["playbook", "regime", "exit_reason"],
    "selected_weakest_bucket": {
      "dimension": "regime",
      "key": "range",
      "trade_count": 1,
      "win_rate": 1.0,
      "avg_return_pct": 0.01,
      "total_return_pct": 0.01,
      "sample_symbols": ["600036.SH"]
    },
    "selected_compare_view": {
      "dimension": "playbook",
      "bucket_count": 2,
      "compared_keys": ["leader_chase", "divergence_reseal"],
      "best_bucket": {
        "key": "leader_chase",
        "trade_count": 2,
        "win_count": 1,
        "loss_count": 1,
        "flat_count": 0,
        "win_rate": 0.5,
        "avg_return_pct": 0.015,
        "total_return_pct": 0.03,
        "sample_symbols": ["600519.SH", "000001.SZ"]
      },
      "weakest_bucket": {
        "key": "divergence_reseal",
        "trade_count": 1,
        "win_count": 1,
        "loss_count": 0,
        "flat_count": 0,
        "win_rate": 1.0,
        "avg_return_pct": 0.01,
        "total_return_pct": 0.01,
        "sample_symbols": ["600036.SH"]
      },
      "spread_return_pct": 0.005,
      "buckets": [
        {
          "key": "leader_chase",
          "trade_count": 2,
          "win_count": 1,
          "loss_count": 1,
          "flat_count": 0,
          "win_rate": 0.5,
          "avg_return_pct": 0.015,
          "total_return_pct": 0.03,
          "sample_symbols": ["600519.SH", "000001.SZ"]
        },
        {
          "key": "divergence_reseal",
          "trade_count": 1,
          "win_count": 1,
          "loss_count": 0,
          "flat_count": 0,
          "win_rate": 1.0,
          "avg_return_pct": 0.01,
          "total_return_pct": 0.01,
          "sample_symbols": ["600036.SH"]
        }
      ]
    }
  },
  "summary_lines": [
    "离线回测样本 3 笔，胜率 66.7%，平均收益 1.33%。",
    "战法维度样本最多的是 leader_chase，共 2 笔。",
    "市场状态中表现最弱的是 range。",
    "退出原因中样本最多的是 time_stop，共 2 笔。",
    "当前最弱分桶是 exit_reason:board_break，平均收益 -2.00%。",
    "已选最弱分桶 regime:range。",
    "已选对比视图 playbook，分桶数 2。"
  ],
  "selected_weakest_bucket": {
    "dimension": "regime",
    "key": "range",
    "trade_count": 1,
    "win_rate": 1.0,
    "avg_return_pct": 0.01,
    "total_return_pct": 0.01,
    "sample_symbols": ["600036.SH"]
  },
  "selected_compare_view": {
    "dimension": "playbook",
    "bucket_count": 2,
    "compared_keys": ["leader_chase", "divergence_reseal"],
    "best_bucket": {
      "key": "leader_chase",
      "trade_count": 2,
      "win_count": 1,
      "loss_count": 1,
      "flat_count": 0,
      "win_rate": 0.5,
      "avg_return_pct": 0.015,
      "total_return_pct": 0.03,
      "sample_symbols": ["600519.SH", "000001.SZ"]
    },
    "weakest_bucket": {
      "key": "divergence_reseal",
      "trade_count": 1,
      "win_count": 1,
      "loss_count": 0,
      "flat_count": 0,
      "win_rate": 1.0,
      "avg_return_pct": 0.01,
      "total_return_pct": 0.01,
      "sample_symbols": ["600036.SH"]
    },
    "spread_return_pct": 0.005,
    "buckets": [
      {
        "key": "leader_chase",
        "trade_count": 2,
        "win_count": 1,
        "loss_count": 1,
        "flat_count": 0,
        "win_rate": 0.5,
        "avg_return_pct": 0.015,
        "total_return_pct": 0.03,
        "sample_symbols": ["600519.SH", "000001.SZ"]
      },
      {
        "key": "divergence_reseal",
        "trade_count": 1,
        "win_count": 1,
        "loss_count": 0,
        "flat_count": 0,
        "win_rate": 1.0,
        "avg_return_pct": 0.01,
        "total_return_pct": 0.01,
        "sample_symbols": ["600036.SH"]
      }
    ]
  }
}
```
