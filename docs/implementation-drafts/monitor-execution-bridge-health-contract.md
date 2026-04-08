# Monitor Execution Bridge Health Contract (v1)

## 目标

在现有 `exit snapshot` 之外，为 Windows 执行面补一份稳定可读的健康快照与趋势摘要。

当前这里的 `execution_bridge_health` 指：

- `Windows Execution Gateway`
- `QMT VM`

主链后续可以直接把这份结构接到：

- `review-board`
- `postclose-master`

而不需要自己再做 `gateway/qmt` 字段拼装。

## 读取入口

- `MonitorStateService.get_latest_execution_bridge_health()`
- `MonitorStateService.get_execution_bridge_health_trend_summary()`
- `MonitorStateService.get_state()["latest_execution_bridge_health"]`
- `MonitorStateService.get_state()["execution_bridge_health_trend_summary"]`
- `build_execution_bridge_health_ingress_payload(...)`
- `build_execution_bridge_health_client_template(...)`
- `get_execution_bridge_health_latest_descriptor()`
- `build_execution_bridge_health_deployment_contract_sample(...)`

## ingress helper（给 Windows Execution Gateway 直接发 POST）

helper 入口：

```python
from ashare_system.monitor.persistence import build_execution_bridge_health_ingress_payload
```

helper 输出结构（可直接 POST 到 `/system/monitor/execution-bridge-health`）：

```python
{
    "trigger": str,  # 对应 ExecutionBridgeHealthIngressInput.trigger，默认 windows_gateway
    "health": {...},  # 对应 ExecutionBridgeHealthIngressInput.health
}
```

health 顶层必需字段（helper 保证稳定给出）：

- `version`
- `checked_at`
- `reported_at`
- `source_id`
- `deployment_role`
- `bridge_path`
- `overall_status`
- `gateway_online`
- `qmt_connected`
- `account_id`
- `session_fresh_seconds`
- `attention_components`
- `attention_component_keys`
- `last_poll_at`
- `last_receipt_at`
- `last_error`
- `windows_execution_gateway`
- `qmt_vm`
- `component_health`
- `summary_lines`
- `updated_at`

source 标识字段：

- `reported_at`：Windows 侧实际上报时间（ISO8601 字符串）
- `source_id`：上报来源实例 ID（例如 `windows-vm-a`）
- `deployment_role`：部署角色（例如 `primary_gateway` / `backup_gateway`）
- `bridge_path`：桥接路径（例如 `linux_openclaw -> windows_gateway -> qmt_vm`）

空值约定：

- helper 在缺省场景下始终补齐所有必需字段，不会缺字段。
- 未提供 source 标识字段时，统一空字符串 `""`。
- legacy 调用仅给 `gateway_online/qmt_connected` 也可用，helper 会自动补齐其余字段空值。
- `reported_at` 的 helper 回退顺序是：
  - `health.reported_at`
  - helper 参数 `reported_at`
  - `health.updated_at`
  - `health.last_poll_at`
  - `""`

最小样例：

```python
payload = build_execution_bridge_health_ingress_payload(
    source_id="windows-vm-a",
    deployment_role="primary_gateway",
    bridge_path="linux_openclaw -> windows_gateway -> qmt_vm",
    health={
        "reported_at": "2026-04-08T09:30:00+08:00",
        "gateway_online": True,
        "qmt_connected": True,
    },
)
```

Linux 主控当前真实处理关系：

```text
POST /system/monitor/execution-bridge-health
  body.trigger -> ExecutionBridgeHealthIngressInput.trigger
  body.health -> ExecutionBridgeHealthIngressInput.health
  -> MonitorStateService.save_execution_bridge_health(body.health, trigger=body.trigger)
```

## client template（给 Windows Gateway 直接抄）

当前 persistence 层还提供：

- `build_execution_bridge_health_client_template(...)`

它返回的是“可直接抄”的客户端模板，重点字段包括：

- `method`
- `path`
- `content_type`
- `request_body`
- `minimal_request_body`
- `top_level_health_defaults`
- `latest_read_descriptor`
- `source_value_suggestions`

其中最小 request body 模板当前是：

```python
{
    "trigger": "windows_gateway",
    "health": {
        "reported_at": "",
        "source_id": "windows-vm-a",
        "deployment_role": "primary_gateway",
        "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
        "gateway_online": False,
        "qmt_connected": False,
    },
}
```

`top_level_health_defaults` 直接对应当前 health 顶层默认字段，Windows Gateway 不需要再翻文档手工补空值。

## latest descriptor（给 Linux 主控直接抄）

当前 persistence 层还提供：

- `get_execution_bridge_health_latest_descriptor()`

它返回 Linux 主控推荐读取键描述，重点是两组：

- `latest_execution_bridge_health.recommended_fields`
- `execution_bridge_health_trend_summary.recommended_fields`

当前推荐读取键：

```python
{
    "latest_execution_bridge_health": {
        "source_id": "latest_execution_bridge_health.health.source_id",
        "deployment_role": "latest_execution_bridge_health.health.deployment_role",
        "bridge_path": "latest_execution_bridge_health.health.bridge_path",
        "overall_status": "latest_execution_bridge_health.health.overall_status",
    },
    "execution_bridge_health_trend_summary": {
        "latest_source_id": "execution_bridge_health_trend_summary.latest_source_id",
        "latest_deployment_role": "execution_bridge_health_trend_summary.latest_deployment_role",
        "latest_bridge_path": "execution_bridge_health_trend_summary.latest_bridge_path",
        "trend_status": "execution_bridge_health_trend_summary.trend_status",
    },
}
```

这样 Linux/OpenClaw 不需要再自己猜 latest 读取键。

## 统一 deployment contract sample helper

当前 persistence 层还提供：

- `build_execution_bridge_health_deployment_contract_sample(...)`

用于一次性给出统一部署样本，直接覆盖 4 类信息：

1. Windows Gateway 最小 POST body 示例  
   字段：`windows_gateway_minimal_post_body`
2. Linux latest/trend 读取示例  
   字段：`linux_latest_trend_read_example`
3. primary/backup gateway 取值样例  
   字段：`gateway_role_samples`
4. 最小 HTTP/curl 风格消费样例（同时附原始请求结构）  
   字段：`http_curl_samples`、`raw_request_samples`

最小示例：

```python
from ashare_system.monitor.persistence import build_execution_bridge_health_deployment_contract_sample

sample = build_execution_bridge_health_deployment_contract_sample(
    api_base_url="http://127.0.0.1:8100",
)
```

返回样本关键结构：

```python
{
    "windows_gateway_minimal_post_body": {...},
    "linux_latest_trend_read_example": {...},
    "gateway_role_samples": {
        "primary_gateway": {
            "source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
        },
        "backup_gateway": {
            "source_id": "windows-vm-b",
            "deployment_role": "backup_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway_backup -> qmt_vm",
        },
    },
    "http_curl_samples": {
        "primary_gateway": "curl -X POST ...",
        "backup_gateway": "curl -X POST ...",
    },
    "raw_request_samples": {
        "primary_gateway": {...},
        "backup_gateway": {...},
    },
}
```

## source / deployment_role / bridge_path 取值建议

当前 helper 给出的建议值包括：

- Linux/OpenClaw：
  - `source_id = "linux-openclaw-main"`
  - `deployment_role = "linux_control_plane"`
- Windows Gateway 主：
  - `source_id = "windows-vm-a"`
  - `deployment_role = "primary_gateway"`
  - `bridge_path = "linux_openclaw -> windows_gateway -> qmt_vm"`
- Windows Gateway 备：
  - `source_id = "windows-vm-b"`
  - `deployment_role = "backup_gateway"`
  - `bridge_path = "linux_openclaw -> windows_gateway_backup -> qmt_vm"`

## deployment contract sample（给 Windows / Linux 双端直接抄）

当前 persistence 层还提供：

- `build_execution_bridge_health_deployment_contract_sample(...)`

它把现有三个 helper 的稳定口径收口成一份部署样例：

- `build_execution_bridge_health_ingress_payload(...)`
- `build_execution_bridge_health_client_template(...)`
- `get_execution_bridge_health_latest_descriptor()`

当前返回结构分四组：

- `request_samples`
- `read_samples`
- `http_samples`
- `source_value_samples`

其中 `request_samples` 当前最小是：

```python
{
    "windows_gateway_minimal_post_body": {
        "trigger": "windows_gateway",
        "health": {
            "reported_at": "",
            "source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "gateway_online": False,
            "qmt_connected": False,
        },
    },
    "windows_gateway_primary_post_body": {...},
    "windows_gateway_backup_post_body": {...},
}
```

主备样例直接对应当前建议值：

- `windows_gateway_primary_post_body.health.source_id = "windows-vm-a"`
- `windows_gateway_primary_post_body.health.deployment_role = "primary_gateway"`
- `windows_gateway_primary_post_body.health.bridge_path = "linux_openclaw -> windows_gateway -> qmt_vm"`
- `windows_gateway_backup_post_body.health.source_id = "windows-vm-b"`
- `windows_gateway_backup_post_body.health.deployment_role = "backup_gateway"`
- `windows_gateway_backup_post_body.health.bridge_path = "linux_openclaw -> windows_gateway_backup -> qmt_vm"`

Linux 侧读取样例当前是：

```python
{
    "linux_latest_read_example": {
        "root_key": "latest_execution_bridge_health",
        "recommended_fields": {
            "source_id": "latest_execution_bridge_health.health.source_id",
            "deployment_role": "latest_execution_bridge_health.health.deployment_role",
            "bridge_path": "latest_execution_bridge_health.health.bridge_path",
            "overall_status": "latest_execution_bridge_health.health.overall_status",
        },
        "example_values": {
            "reported_at": "2026-04-08T14:35:00+08:00",
            "source_id": "windows-vm-a",
            "deployment_role": "primary_gateway",
            "bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "overall_status": "healthy",
        },
    },
    "linux_trend_read_example": {
        "root_key": "execution_bridge_health_trend_summary",
        "recommended_fields": {
            "latest_source_id": "execution_bridge_health_trend_summary.latest_source_id",
            "latest_deployment_role": "execution_bridge_health_trend_summary.latest_deployment_role",
            "latest_bridge_path": "execution_bridge_health_trend_summary.latest_bridge_path",
            "trend_status": "execution_bridge_health_trend_summary.trend_status",
        },
        "example_values": {
            "latest_source_id": "windows-vm-a",
            "latest_deployment_role": "primary_gateway",
            "latest_bridge_path": "linux_openclaw -> windows_gateway -> qmt_vm",
            "trend_status": "stable",
        },
    },
}
```

`http_samples` 当前给出最小 POST 消费样例：

```python
{
    "windows_gateway_post": {
        "method": "POST",
        "path": "/system/monitor/execution-bridge-health",
        "content_type": "application/json",
        "body": { ...windows_gateway_minimal_post_body... },
    },
    "curl_post_example": "curl -X POST \"http://127.0.0.1:8100/system/monitor/execution-bridge-health\" ...",
}
```

这意味着：

- Windows Gateway 可以直接抄 `request_samples.windows_gateway_minimal_post_body`
- 主备部署切换时只需要切换到 `windows_gateway_primary_post_body` 或 `windows_gateway_backup_post_body`
- Linux/OpenClaw 可以直接抄 `read_samples.linux_latest_read_example` 与 `read_samples.linux_trend_read_example`
- 若要给联调同学一个最小 HTTP 示例，直接发 `http_samples.curl_post_example`

## latest_execution_bridge_health

返回结构：

```python
{
    "health_id": str,
    "generated_at": str,
    "trigger": str,
    "health": {
        "version": "v1",
        "checked_at": float,
        "reported_at": str,
        "source_id": str,
        "deployment_role": str,
        "bridge_path": str,
        "overall_status": "unknown|healthy|degraded|down",
        "gateway_online": bool,
        "qmt_connected": bool,
        "account_id": str,
        "session_fresh_seconds": int,
        "attention_components": list[str],
        "attention_component_keys": list[str],
        "last_poll_at": str,
        "last_receipt_at": str,
        "last_error": str,
        "windows_execution_gateway": {
            "key": "windows_execution_gateway",
            "label": "Windows Execution Gateway",
            "status": "unknown|healthy|degraded|down",
            "reachable": bool,
            "latency_ms": float,
            "staleness_seconds": float,
            "error_count": int,
            "success_count": int,
            "last_ok_at": str,
            "last_error_at": str,
            "detail": str,
            "tags": list[str],
        },
        "qmt_vm": {
            "key": "qmt_vm",
            "label": "QMT VM",
            "status": "unknown|healthy|degraded|down",
            "reachable": bool,
            "latency_ms": float,
            "staleness_seconds": float,
            "error_count": int,
            "success_count": int,
            "last_ok_at": str,
            "last_error_at": str,
            "detail": str,
            "tags": list[str],
        },
        "component_health": [
            {
                "key": str,
                "label": str,
                "status": str,
                "reachable": bool,
                "latency_ms": float,
                "staleness_seconds": float,
                "error_count": int,
                "detail": str,
                "tags": list[str],
            },
            ...
        ],
        "summary_lines": list[str],
        "updated_at": str,
    },
}
```

说明：

- `overall_status` 是给 review-board / postclose-master 直接读的总体状态。
- `reported_at/source_id/deployment_role/bridge_path` 用于远端（Windows Execution Gateway / QMT VM）上报来源与部署路径追踪。
- `gateway_online` / `qmt_connected` 保留为兼容布尔摘要。
- `attention_components` 是当前窗口内需要重点提示给 Main 的组件标签列表。
- `attention_component_keys` 是给程序侧直连使用的稳定 key（`windows_execution_gateway` / `qmt_vm`）。
- `component_health` 是统一组件列表，Main 不必再从两个命名字段二选一组装。
- 若调用方只传 legacy 布尔字段，当前实现也会自动补出 `windows_execution_gateway` / `qmt_vm` 两个组件快照。
- legacy 调用未传远端上报字段时，`reported_at/source_id/deployment_role/bridge_path` 固定为空字符串，不会缺字段。

## execution_bridge_health_history

`MonitorStateService.get_state()["execution_bridge_health_history"]` 当前记录最近 20 条摘要，底层最多保留 200 条。

历史项结构：

```python
{
    "health_id": str,
    "generated_at": str,
    "trigger": str,
    "checked_at": float,
    "reported_at": str,
    "source_id": str,
    "deployment_role": str,
    "bridge_path": str,
    "overall_status": str,
    "gateway_online": bool,
    "qmt_connected": bool,
    "session_fresh_seconds": int,
    "last_error": str,
    "attention_components": list[str],
    "windows_execution_gateway": {
        "status": str,
        "reachable": bool,
        "latency_ms": float,
        "staleness_seconds": float,
        "error_count": int,
    },
    "qmt_vm": {
        "status": str,
        "reachable": bool,
        "latency_ms": float,
        "staleness_seconds": float,
        "error_count": int,
    },
}
```

## execution_bridge_health_trend_summary

返回结构：

```python
{
    "available": bool,
    "recent_limit": int,
    "snapshot_count": int,
    "latest_reported_at": str,
    "latest_source_id": str,
    "latest_deployment_role": str,
    "latest_bridge_path": str,
    "latest_overall_status": str,
    "overall_status_series": list[str],
    "overall_status_counts": {
        "healthy": int,
        "degraded": int,
        "down": int,
        "unknown": int,
    },
    "trend_status": "unknown|stable|degrading|critical",
    "gateway_online_ratio": float,
    "qmt_connected_ratio": float,
    "latest_gateway_online": bool,
    "latest_qmt_connected": bool,
    "latest_gateway_status": str,
    "latest_qmt_vm_status": str,
    "latest_session_fresh_seconds": int,
    "last_error_count": int,
    "attention_snapshot_count": int,
    "attention_ratio": float,
    "latest_attention_components": list[str],
    "latest_attention_component_keys": list[str],
    "windows_execution_gateway": {
        "key": str,
        "label": str,
        "latest_status": str,
        "latest_reachable": bool,
        "status_series": list[str],
        "reachable_ratio": float,
        "avg_latency_ms": float,
        "max_latency_ms": float,
        "max_staleness_seconds": float,
        "error_count_total": int,
        "attention_count": int,
        "summary_lines": list[str],
    },
    "qmt_vm": {
        "key": str,
        "label": str,
        "latest_status": str,
        "latest_reachable": bool,
        "status_series": list[str],
        "reachable_ratio": float,
        "avg_latency_ms": float,
        "max_latency_ms": float,
        "max_staleness_seconds": float,
        "error_count_total": int,
        "attention_count": int,
        "summary_lines": list[str],
    },
    "component_trends": [windows_execution_gateway, qmt_vm],
    "health_trend_snapshot": {
        "latest_reported_at": str,
        "latest_source_id": str,
        "latest_deployment_role": str,
        "latest_bridge_path": str,
        "latest_overall_status": str,
        "trend_status": str,
        "attention_ratio": float,
        "latest_gateway_status": str,
        "latest_qmt_vm_status": str,
    },
    "summary_lines": list[str],
}
```

说明：

- 顶层 `summary_lines` 适合直接进 review-board / postclose-master 摘要区。
- `windows_execution_gateway` / `qmt_vm` 子结构适合渲染组件级健康卡片。
- `component_trends` 适合前端直接循环渲染，不必硬编码两个字段名。
- `health_trend_snapshot` 适合 postclose-master 取一屏概览。
- `trend_status` 当前是基于最近窗口状态分数聚合的粗粒度趋势，不代表完整因果诊断。

## 空结构约定

没有任何历史记录时：

- `get_latest_execution_bridge_health()` 返回固定空结构，而不是缺字段。
- `get_execution_bridge_health_trend_summary()` 返回 `available=False` 与固定子结构。
- `reported_at/source_id/deployment_role/bridge_path` 以及 `latest_*` 对应字段统一返回空字符串 `""`，Linux 主控不要自行猜测默认来源。

## 当前接入建议

当前推荐由 Windows 执行面轮询或回执同步链直接调用：

```python
state_service.save_execution_bridge_health(health_payload, trigger="windows_gateway")
```

Main 侧消费时优先读取：

```python
state = state_service.get_state()
latest = state["latest_execution_bridge_health"]
trend = state["execution_bridge_health_trend_summary"]
```

Linux 主控当前推荐消费口径：

- 当前来源识别直接读：
  - `latest["health"]["reported_at"]`
  - `latest["health"]["source_id"]`
  - `latest["health"]["deployment_role"]`
  - `latest["health"]["bridge_path"]`
- 趋势摘要直接读：
  - `trend["latest_reported_at"]`
  - `trend["latest_source_id"]`
  - `trend["latest_deployment_role"]`
  - `trend["latest_bridge_path"]`
- 若只想取一屏概要，直接使用：
  - `trend["health_trend_snapshot"]`

这样可以直接拿到：

- 最近一条健康上报来自哪台 Windows VM / 哪条执行桥
- 当前总体状态
- Gateway / QMT VM 两个组件的最新状态
- 最近窗口趋势摘要
- 可直接展示给 review-board / postclose-master 的 `summary_lines`
