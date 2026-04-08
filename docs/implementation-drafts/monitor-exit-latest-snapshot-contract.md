# monitor exit latest snapshot 契约

## 目标

`market_watcher` 对外提供一个稳定可读的最新退出监控快照，供主链或审计链直接读取，而不需要依赖 `check_once()` 的返回值。

同时该快照会写入 monitor state 层，供后续 Main 或归档链读取。

读取入口：

- `MarketWatcher.get_latest_exit_snapshot()`
- `MonitorStateService.get_latest_exit_snapshot()`

这两个入口即使在尚未执行过 `check_once()` 时，也会返回固定结构的空快照。

## 字段契约

### 内存 latest snapshot

返回对象为：

```python
{
    "version": "v1",
    "checked_at": float,
    "signal_count": int,
    "watched_symbols": list[str],
    "by_symbol": list[{"key": str, "count": int}],
    "by_reason": list[{"key": str, "count": int}],
    "by_severity": list[{"key": str, "count": int}],
    "by_tag": list[{"key": str, "count": int}],
    "summary_lines": list[str],
    "items": list[dict],
}
```

字段说明：

- `version`
  当前快照契约版本，固定为 `v1`。
- `checked_at`
  最近一次 `check_once()` 完成时间，Unix 时间戳；未执行前为 `0.0`。
- `signal_count`
  最近一次退出监控信号数量。
- `watched_symbols`
  最近一次检查时 watcher 的标的集合，已排序。
- `by_symbol`
  按标的聚合的计数桶，元素结构为 `{"key": 标的代码, "count": 数量}`。
- `by_reason`
  按退出原因聚合的计数桶，`reason` 命名与现有 `tail_market` / `exit_engine` 对齐，例如 `board_break`、`sector_retreat`、`time_stop`。
- `by_severity`
  按严重级别聚合的计数桶，当前级别集合为 `IMMEDIATE`、`HIGH`、`NORMAL`。
- `by_tag`
  按审计标签聚合的计数桶，优先复用已有标签，例如 `board_break`、`micro_rebound_failed`、`sector_retreat`、`negative_alert`、`intraday_fade`。
- `summary_lines`
  面向日志、通知或页面摘要的简短文本列表。
- `items`
  最近一次退出监控的完整明细，直接来自 exit monitor summary。

### monitor state 持久化记录

`MonitorStateService.get_latest_exit_snapshot()` 与 `get_state()["latest_exit_snapshot"]` 的持久化结构为：

```python
{
    "snapshot_id": str,
    "generated_at": str,
    "trigger": str,
    "snapshot": {
        "version": "v1",
        "checked_at": float,
        "signal_count": int,
        "watched_symbols": list[str],
        "by_symbol": list[{"key": str, "count": int}],
        "by_reason": list[{"key": str, "count": int}],
        "by_severity": list[{"key": str, "count": int}],
        "by_tag": list[{"key": str, "count": int}],
        "summary_lines": list[str],
        "items": list[dict],
    },
}
```

字段说明：

- `snapshot_id`
  monitor state 层的持久化记录 ID；未持久化前为空字符串。
- `generated_at`
  monitor state 写入时间；未持久化前为空字符串。
- `trigger`
  触发来源，当前默认是 `market_watcher`；未持久化前为空字符串。
- `snapshot`
  具体的 latest exit snapshot，字段契约与内存态一致。

## 空快照约定

在以下场景返回空快照，而不是缺字段：

- `MarketWatcher` 刚初始化，尚未执行 `check_once()`
- 最近一次检查没有退出信号
- watcher 未注入 `exit_monitor`
- monitor state 中尚未持久化任何 exit snapshot

空快照示例：

```python
{
    "version": "v1",
    "checked_at": 0.0,
    "signal_count": 0,
    "watched_symbols": [],
    "by_symbol": [],
    "by_reason": [],
    "by_severity": [],
    "by_tag": [],
    "summary_lines": ["当前无退出监控信号。"],
    "items": [],
}
```

## 读取建议

主链读取时优先使用：

```python
snapshot = watcher.get_latest_exit_snapshot()
```

如果需要从 monitor state 直接读取持久化后的最新结果，使用：

```python
latest_exit = state_service.get_latest_exit_snapshot()
snapshot = latest_exit["snapshot"]
```

如果需要完整明细可直接看 `snapshot["items"]`；如果只做聚合展示或审计摘要，优先读：

- `snapshot["by_symbol"]`
- `snapshot["by_reason"]`
- `snapshot["by_severity"]`
- `snapshot["by_tag"]`
- `snapshot["summary_lines"]`
