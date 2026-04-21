# 2026-04-20 Windows 执行桥专项问题单

## 目标

本文件只讨论 Windows bridge 链路问题，不混入选股、Agent、展示层问题。

核心判断：

- 这不是“网络不通”问题
- 这是 Windows 执行桥链路没有稳定完成 `poll -> claim -> submit -> receipt -> health` 的闭环问题

---

## 现象

今天 Linux 控制面出现了以下现象：

1. 已经生成真实 `execution intent`
2. 原桥接链没有持续消费这些 intent
3. Windows bridge 健康状态没有更新到今天
4. Linux 侧能直接经 Go/QMT 查询到账户、持仓、订单，说明数据平面是通的

---

## 关键证据

## 证据 1：不是券商或 QMT 完全不可用

今天已经有真实订单报出到券商：

- 标的：`000002.SZ`
- 数量：`5100`
- 价格：`3.92`
- `order_id`：`1082219891`
- 状态：`ACCEPTED`

并且 Linux 侧可正常访问：

- `http://127.0.0.1:18793/qmt/account/asset`
- `http://127.0.0.1:18793/qmt/account/positions`
- `http://127.0.0.1:18793/qmt/account/orders`
- `http://127.0.0.1:18793/qmt/quote/*`

结论：

- Go 数据平面与 QMT 的访问是通的
- 券商报单链不是整体瘫痪
- “原桥接失败”不能解释成网络问题

## 证据 2：桥健康回写已经明显陈旧

`/srv/data/ashare-state/monitor_state.json` 中：

- `latest_execution_bridge_health.reported_at = 2026-04-18T07:46:25+08:00`

今天是 `2026-04-20`，说明：

- Linux 控制面两天都没有收到新的桥健康上报
- 至少健康回写链已经不工作，或没有持续工作

## 证据 3：Linux 侧已经产出待执行意图

今天 `meeting_state.json` 中能确认存在：

- `pending_execution_intents`
- `latest_execution_gateway_receipt`
- `execution_gateway_receipt_history`

这说明 Linux 侧并非没有派发意图。

问题在于：

- Windows worker 没有稳定把这些意图消费并回写到今天的新状态

## 证据 4：桥接 worker 当下没有形成可见活动痕迹

今天 Linux 侧观察到的结果是：

- 没有今天的新 bridge health
- 没有持续更新的 claim/receipt 活动证据
- 旧的失败或陈旧状态仍残留

说明至少有一种情况成立：

1. Windows worker 没启动
2. Windows worker 启动了，但没有轮询到 Linux `/system/execution/gateway/intents/pending`
3. 轮询到了，但 claim 失败
4. claim 成功了，但 submit 或 receipt 回写失败
5. 健康上报脚本没有工作

---

## 当前最可能的真实问题段

按概率排序，当前优先怀疑这几段：

## 1. Windows worker 没有持续运行

需要确认：

- 是否有常驻进程持续执行 `windows_execution_gateway_worker`
- 是否因为登录会话中断、计划任务失败、服务退出而停止
- 是否只是手工运行过一次，而不是长期驻留

## 2. Windows worker 配置的控制面地址或 token 不正确

需要确认：

- `control_plane_base_url` 是否仍指向当前正式外网地址
- token 是否过期、未加载或路径错误
- `pending_path` / `claim_path` / `receipt_path` 是否与 Linux 控制面一致

## 3. worker 拉取成功，但 receipt 没回写

需要确认：

- claim 成功后，真实下单异常有没有被吞掉
- `post_receipt` 是否超时、报 4xx/5xx、被异常吞没
- receipt payload 是否不满足 Linux 侧校验要求

## 4. 健康上报脚本单独失效

即使 worker 偶尔工作，如果 health 上报链坏了，Linux 侧也会一直认为桥状态不新鲜。

需要确认：

- bridge health 是否有独立脚本或独立计划任务
- 该任务是否仍在按分钟级运行
- 上报地址、token、桥标识是否与当前部署一致

---

## 需要 Windows 侧逐项核查的内容

## 一、进程与任务

请核查：

- `windows_execution_gateway_worker` 是否常驻
- 是否配置成 Windows 服务、计划任务或守护进程
- 最近一次启动时间、退出时间、退出码
- 是否存在多个 worker 并发导致 claim 冲突

## 二、控制面连通性

请在 Windows 侧直接验证：

- 能否访问 Linux 控制面 `/system/execution/gateway/intents/pending`
- 能否正常 POST `/system/execution/gateway/intents/claim`
- 能否正常 POST `/system/execution/gateway/receipts`
- 能否正常上报 execution bridge health

需要记录：

- 请求 URL
- HTTP 状态码
- 返回体
- 超时耗时

## 三、QMT 提交链

请核查：

- worker 真正调用的下单接口是哪一层
- 从 worker 到 QMT 的 submit 是否有异常日志
- 下单失败时是否生成 failed receipt 回写 Linux
- 是否存在“下单失败但没有回写 receipt”的情况

## 四、日志留痕

Windows 侧需要提供以下日志片段：

- worker 启动日志
- 最近一次 `poll pending intents` 日志
- 最近一次 `claim intent` 日志
- 最近一次真实 `submit order` 日志
- 最近一次 `post receipt` 日志
- 最近一次 `bridge health report` 日志

---

## Linux 侧已经确认的非 Windows 网络问题

以下事项今天已经确认，不应再作为排查方向：

1. 不是 Linux 到 Go/QMT 的网络不通
2. 不是券商完全拒绝报单
3. 不是 Linux 完全没有生成 intent

所以请 Windows 侧不要把问题泛化成“网络偶发波动”。

当前需要回答的是：

- 为什么今天没有新的 health
- 为什么 intent 没有稳定被 claim
- 为什么 receipt 没有稳定回写

---

## Windows 侧修复完成后的验收标准

必须同时满足以下条件，才算桥接修好：

1. Linux 侧 `latest_execution_bridge_health.reported_at` 更新为当天实时值
2. Linux 侧 `/system/execution/gateway/intents/pending` 能看到 intent 数量随 claim 变化
3. claim 后，intent 状态从 `approved` 进入 `claimed`
4. 下单后，无论成功还是失败，都必须回写 receipt
5. Linux 侧 `latest_execution_gateway_receipt` 更新时间进入当天
6. 至少完成一次真实 `pending -> claim -> receipt` 闭环留痕

如果没有 receipt，只说“Windows 已经在跑”，不算修复完成。

---

## 结论

今天 Windows 执行桥的问题，不是抽象意义上的“有点不稳定”，而是：

- 健康上报陈旧
- claim/receipt 闭环没有形成今天的稳定证据
- Linux 已能证明 QMT 与数据平面本身可用

因此 Windows 侧本次整改目标必须是：

- 恢复常驻 worker
- 恢复 claim 与 receipt 回写
- 恢复 bridge health 持续上报
- 提供完整日志证据

只有这四项都到位，Linux 侧才能重新把 Windows bridge 视为正式执行主链。
