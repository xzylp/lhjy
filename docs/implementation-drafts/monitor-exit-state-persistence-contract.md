# Monitor Exit Snapshot State Persistence Contract (v1)

## 目标

把 `market_watcher` 产生的 `latest exit snapshot` 稳定写入 monitor state，供主链读取，不改变 `check_once()` 返回类型。

## 写入入口

- 生产者：`MarketWatcher.check_once()`
- 持久化方法：`MonitorStateService.save_exit_snapshot(snapshot, trigger="market_watcher")`

## StateStore 键

- `latest_exit_snapshot`
- `exit_snapshot_history`

### latest_exit_snapshot 结构

```json
{
  "snapshot_id": "exit-snapshot-YYYYMMDDHHMMSS",
  "generated_at": "ISO8601",
  "trigger": "market_watcher",
  "snapshot": {
    "version": "v1",
    "checked_at": 0.0,
    "signal_count": 0,
    "watched_symbols": [],
    "by_symbol": [],
    "by_reason": [],
    "by_severity": [],
    "by_tag": [],
    "summary_lines": ["当前无退出监控信号。"],
    "items": []
  }
}
```

### exit_snapshot_history 结构

```json
[
  {
    "snapshot_id": "exit-snapshot-YYYYMMDDHHMMSS",
    "generated_at": "ISO8601",
    "trigger": "market_watcher",
    "signal_count": 0,
    "checked_at": 0.0
  }
]
```

## 主链读取建议

- 优先读取 `MonitorStateService.get_state()["latest_exit_snapshot"]`
- 聚合展示读取：
  - `latest_exit_snapshot.snapshot.signal_count`
  - `latest_exit_snapshot.snapshot.by_reason`
  - `latest_exit_snapshot.snapshot.summary_lines`
