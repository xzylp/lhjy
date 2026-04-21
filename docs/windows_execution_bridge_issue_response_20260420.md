# Windows 执行桥问题答复

更新时间：2026-04-20 15:35 +08:00

## 1. 结论

本次问题不是“网络偶发波动”，而是 Windows 执行桥本身存在三处明确故障：

1. `gateway_worker` 在 2026-04-18 启动后因状态文件写入失败崩溃，导致今天没有持续常驻。
2. `claim` 与 `receipt` 的 payload 结构偏离原稳定运行包兼容格式，Linux 控制面返回 `HTTP 422`，闭环被打断。
3. worker 的 submit 路径仍在直连 `XtQuantTrader`，未复用当前稳定在线的本地 `18792` 交易桥，因此出现 `XtQuantTrader.connect()=-1`。

其中：

- `claim` 与 `receipt` 协议问题已修复。
- 状态文件写入崩溃问题已修复。
- worker 执行路径已改为优先复用本地 `18792` 交易桥。

## 2. 现场证据

### 2.1 worker 并未持续常驻

本机进程检查时，仅看到：

- `XtMiniQmt.exe`
- `pythonw.exe`（对应 `18792` 交易桥）

未看到常驻 `windows_execution_gateway_worker` 进程。

### 2.2 原始日志明确显示两类故障

`logs/gateway_worker.log` 中原始错误包括：

```text
claim intent failed: HTTP Error 422: Unprocessable Entity
submit receipt failed: HTTP Error 422: Unprocessable Entity
PermissionError: [WinError 5] 拒绝访问 ... gateway_worker_status.json
```

这说明：

- `claim/receipt` 闭环不是没跑，而是协议不兼容被 Linux 控制面拒绝
- worker 随后又因状态文件写入失败直接退出

## 3. 已完成修复

修改文件：

- `ashare_system/state_store.py`
- `ashare_system/windows_execution_gateway_worker.py`

### 3.1 修复状态文件写入

在 `state_store.py` 中增加 Windows 兼容回退：

- 仍优先使用临时文件 + `os.replace`
- 若目标状态文件被占用导致 `WinError 5/32`
- 则回退为原位写入，避免 worker 直接崩溃

这修复了 `gateway_worker_status.json` 造成的常驻中断。

### 3.2 修复 poll/claim/receipt 协议

在 `windows_execution_gateway_worker.py` 中恢复为稳定包兼容语义：

- `poll pending` 增加查询参数
  - `gateway_source_id`
  - `deployment_role`
  - `limit`
  - `account_id`
- `claim` payload 补回
  - `gateway_source_id`
  - `deployment_role`
  - `bridge_path`
  - `claimed_at`
- `receipt` 改为结构化字段，不再直接塞裸 `result`
  - `receipt_id`
  - `intent_id`
  - `intent_version`
  - `gateway_source_id`
  - `deployment_role`
  - `bridge_path`
  - `reported_at`
  - `submitted_at`
  - `status`
  - `broker_order_id`
  - `broker_session_id`
  - `exchange_order_id`
  - `error_code`
  - `error_message`
  - `order`
  - `fills`
  - `latency_ms`
  - `raw_payload`
  - `summary_lines`

同时修复了执行顺序：

- 只有 `claim_status == claimed` 才会继续 submit
- 不再出现“claim 失败仍继续 submit”的错误路径

### 3.3 修复 submit 执行路径

worker 现已改为：

- 优先调用本地 `18792` 交易桥：
  - `POST /qmt/trade/order`
- 仅当交易桥不可达时，才回退旧的直连 `XtQuantClientAdapter`

这样可直接复用：

- 已登录的 QMT 会话
- 已建立的 trader 连接
- 已验证可用的执行面

并避免再次撞上 `XtQuantTrader.connect()=-1` 的老问题。

### 3.4 增强日志

当前 worker 日志已增加：

- `claim intent ok ... payload=... response=...`
- `submit via trade bridge ...`
- `post receipt ok ... response=...`
- `poll/claim/receipt` 失败时的 HTTP 状态码与响应 body

## 4. 修复后验收结果

### 4.1 状态文件恢复正常

当前 `.ashare_state/gateway_worker_status.json` 已恢复为正常 JSON，可读出：

```json
{
  "last_poll_at": "2026-04-20T15:28:16+08:00",
  "last_receipt_at": "2026-04-20T15:28:30+08:00",
  "last_health_report_at": "2026-04-20T15:28:36+08:00",
  "last_intent_id": "intent-2026-04-20-000002.SZ",
  "control_plane": {
    "reachable": true
  }
}
```

说明：

- worker 已不再因状态文件写入失败崩溃
- 控制面可达状态与最新时间戳已能写回本地

### 4.2 `claim` 已恢复成功

实测 `--once` 运行日志中已出现：

```text
claim intent ok intent_id=intent-2026-04-20-000002.SZ ...
response={"ok": true, "claim_status": "claimed", ...}
```

说明 `HTTP 422` 的 claim 协议错误已修复。

### 4.3 `receipt` 已恢复成功

同一次联调中已出现：

```text
post receipt ok intent_id=intent-2026-04-20-000002.SZ status=failed response={"ok": true, "stored": true, ...}
```

说明 `receipt` 也已被 Linux 控制面正式接收并写入，不再是 422。

### 4.4 本地交易桥可用

本机直连 `127.0.0.1:18792/qmt/ping` 验证结果：

- `ok = true`
- `trading_status.trader_connected = true`
- `account_id = 8890130545`

说明新的 submit 首选执行面是在线的。

## 5. 当前剩余状态

本次修复已经把执行桥的三个核心阻塞点拆开并修掉了前两类：

- worker 不再因状态文件崩溃
- `claim` 恢复
- `receipt` 恢复
- health 本地状态恢复

当前剩余需要注意的一点是：

- 我这次没有直接将常驻 worker 自动重新拉起为长期运行

原因是：

- 常驻 worker 一旦启动，将开始持续消费 Linux 控制面的真实执行 intent
- 这属于真实交易动作，不适合在未再次确认当前 pending 状态的情况下静默开启

## 6. 建议的最后一步

在你确认允许恢复常驻执行后，执行：

1. 启动 `windows_execution_gateway_worker` 常驻进程
2. 观察新的：
   - `latest_execution_bridge_health.reported_at`
   - `latest_execution_gateway_receipt`
   - `pending -> claim -> receipt`
3. 用今天或下一笔真实 intent 验证 submit 是否走 `18792` 成功落单

## 7. 对需求稿的回复要点

本次 Windows 执行桥问题的明确答复是：

- 不是 Linux 到 QMT 的网络问题
- 不是券商整体不可用
- 根因是 Windows worker 已崩溃，且 claim/receipt 协议与当前 Linux 控制面不兼容
- 协议问题已修复，状态写入崩溃已修复，submit 也已切换为优先走稳定在线的本地 `18792` 交易桥
- 当前已经拿到新的 `claim` 成功与 `receipt` 成功证据

## 8. 文件位置

本地答复稿：

- `C:\Users\yxzzh\Desktop\XTQ\docs\windows_execution_bridge_issue_response_20260420.md`

共享答复稿：

- `Z:\srv\projects\ashare-system-v2\docs\windows_execution_bridge_issue_response_20260420.md`
