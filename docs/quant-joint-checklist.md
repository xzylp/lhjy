# Quant 联调检查清单

> Updated: 2026-04-06

## 目标

用一套固定步骤验证 `quant -> ashare -> ashare-system-v2 -> 通知摘要` 是否通畅，避免每次靠记忆手敲接口。

本清单只做只读或预演检查，不提交真实订单。

## 一键检查

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
./scripts/smoke_quant_joint.sh 10 8890130545
```

## 检查顺序

1. `openclaw --profile quant status --all`
2. `./scripts/ashare_api.sh probe`
3. `GET /health`
4. `GET /runtime/health`
5. `POST /runtime/jobs/pipeline`
6. `GET /system/discussions/execution-precheck`
7. `GET /system/discussions/execution-intents`
8. `POST /system/discussions/execution-intents/dispatch` with `apply=false`
9. `GET /system/discussions/execution-dispatch/latest`
10. `GET /system/discussions/client-brief`
11. `GET /system/discussions/final-brief`

## 通过标准

- `probe` 能返回动态可达的 WSL 基址，不写死单一宿主 IP。
- `/health` 返回 `status=ok`。
- `/runtime/health` 返回 `run_mode=live` 且执行适配器、行情适配器状态正常。
- `pipeline` 能生成当日候选，`trade_date` 可解析。
- `execution-precheck` 有结构化字段：
  - `status`
  - `approved_count`
  - `blocked_count`
  - `primary_recommended_next_action`
- `execution-intents` 返回 `items[].intent_id`。
- `dispatch apply=false` 返回：
  - `status=preview`
  - `summary_notification.dispatched=true`
- `execution-dispatch/latest` 可回看最近一次预演回执。
- `client-brief` 包含：
  - `execution_dispatch_status`
  - `execution_dispatch_submitted_count`
  - `execution_dispatch_preview_count`
  - `execution_dispatch_blocked_count`
  - `execution_dispatch_lines`
- 用户可见列表保留 `代码 + 中文名`，不能退化成纯代码。

## 常见异常定位

### 1. `probe` 找不到 8100

- 先看 Windows 侧服务是否已启动。
- 再看 `.ashare_state/service_endpoints.json` 是否已刷新。
- 最后看 WSL 当前可达宿主 IP 是否变化，避免沿用旧会话里的历史地址。

### 2. `execution-intents` 为空

- 先确认当日讨论是否已有 `selected` 标的。
- 再确认执行预检是否因仓位、预算、交易时段阻断。
- 这通常不是 OpenClaw 会话故障。

### 3. `dispatch` 没有摘要通知

- 检查返回中的 `summary_notification.reason`。
- 当前设计下：
  - `preview / blocked` 会发标准摘要
  - `submitted 且无 blocker` 走逐笔交易通知，避免重复刷屏

### 4. PowerShell 输出中文乱码

- 当前已知是终端编码问题。
- 以仓库状态文件、接口 JSON、quant 日志和飞书实际消息为准，不按 PowerShell 控制台乱码判定数据损坏。

## 人工抽查

必要时补查：

```bash
journalctl --user -u openclaw-gateway-quant.service -n 80 --no-pager
openclaw --profile quant status --all
./scripts/ashare_api.sh GET "/system/discussions/execution-dispatch/latest?trade_date=2026-04-06"
```
