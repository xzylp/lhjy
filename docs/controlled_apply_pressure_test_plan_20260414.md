# apply=true 受控真实压测方案

> 更新日期：2026-04-15
> 目标：在不突破电子围栏的前提下，完成一次可回溯的 `apply=true` 小额真实闭环压测。

---

## 1. 当前真实状态

- 已完成闭环：
  - `真实行情 -> runtime -> discussion -> execution-intents -> dispatch preview`
  - Windows `18791` 资产与执行桥已接通
  - 飞书长连接、重要消息、监督催办已联通
- 尚未完成：
  - `apply=true` 真实下单闭环证据
  - `真实报单 -> gateway claim -> receipt -> 对账 -> 飞书执行回执` 的压测留档

---

## 2. 当前代码侧真实电子围栏

## 2.1 运行时配置默认值

来自 [`runtime_config.py`](/srv/projects/ashare-system-v2/src/ashare_system/runtime_config.py)：

- `max_hold_count=5`
- `max_total_position=0.8`
- `equity_position_limit=0.2`
- `max_single_position=0.25`
- `max_single_amount=50000`
- `minimum_total_invested_amount=100000`
- `reverse_repo_reserved_amount=70000`
- `reverse_repo_target_ratio=0.7`
- `emergency_stop=false`

## 2.2 execution_precheck 真实阻断项

来自 [`system_api.py`](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py) 的 `_build_execution_precheck()`：

- `risk_gate_reject`
- `audit_gate_hold`
- `balance_unavailable`
- `market_price_unavailable`
- `total_position_limit_reached`
- `max_hold_count_reached`
- `single_position_limit_reached`
- `cash_unavailable`
- `budget_below_min_lot`
- `order_lot_insufficient`
- `cash_exceeded`
- `single_amount_exceeded`
- `emergency_stop_active`
- `trading_session_closed`
- `market_snapshot_fetch_failed`
- `market_snapshot_unavailable`
- `market_snapshot_stale`
- `limit_up_locked`
- `limit_down_locked`
- `price_deviation_exceeded`
- `guard_reject`

## 2.3 apply=true 的额外提交闸门

来自 [`system_api.py`](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py) 的 `_build_execution_dispatch_receipts()`：

- Windows 执行桥状态缺失时阻断
- `windows_execution_gateway.reachable=false` 时阻断
- `qmt_connected=false` 或 `qmt_vm.reachable=false` 时阻断
- `emergency_stop=true` 时阻断
- `run_mode=live` 但 `live_trade_enabled=false` 时阻断

结论：
- 当前系统不是“apply=true 就直接裸下单”。
- 但只要以上条件满足，`apply=true` 就会进入真实执行链，因此压测前必须把参数、白名单和回滚步骤先写死。
- 额外注意：当前 Windows gateway worker 本身不依据 `paper/live` 自动短路；因此 `apply=true` 必须继续通过外层脚本确认、交易时段校验和 readiness 检查来控制，不能把 `paper` 误当成天然保险丝。

---

## 3. 当前与项目目标的关键差异

你当前的项目目标口径里，团队希望：

- 目标仓位可推进到 5 成
- 未满 5 成仓时，agent 有义务持续发现机会并打满目标仓位
- 满仓前可持续提案，满仓后也可做替换仓位和日内 T

但当前代码默认口径仍偏“测试期保守值”：

- 股票仓位上限还是 `equity_position_limit=20%`
- 逆回购保留金额还是 `70000`
- 股票测试预算还是 `30000`

这意味着：

- 当前代码更适合做“小额真实压测”
- 还不适合直接按你的“5 成仓交易台纪律”进入正式实盘

因此建议分两阶段：

1. 第一阶段：按现有 20% 股票仓位上限，完成一次真实 `apply=true` 小额闭环压测。
2. 第二阶段：在压测闭环稳定后，再把 `equity_position_limit`、逆回购保留金额、股票预算等参数逐步切向你的 5 成仓目标。

---

## 4. 本次建议压测目标

本轮只验证“真实执行链稳定性”，不验证收益最大化。

建议目标：

- 只做 1 笔或 1 只标的
- 单笔金额控制在 `10000 ~ 21000`
- 不突破当前 `equity_position_limit=20%`
- 不修改 `max_single_amount=50000`
- 保持 `reverse_repo_reserved_amount=70000`
- 只在交易时段、桥接状态 `healthy/connected` 时触发

## 4.1 基于 2026-04-15 实数的首轮建议口径

截至 `2026-04-15 10:12`，控制面真实状态已经推进到：

- discussion cycle: `round_1_running`
- execution precheck: `approved=3 blocked=0`
- execution intents: `ready intents=3 blocked=0`
- dispatch preview: `preview_count=3 blocked=0`

当前三条真实 preview intents 为：

1. `000001.SZ 平安银行` `qty=1800` `price=11.17` `estimated_value=20106.0`
2. `000002.SZ 万 科Ａ` `qty=5000` `price=4.02` `estimated_value=20100.0`
3. `000004.SZ *ST国华` `qty=6100` `price=3.30` `estimated_value=20130.0`

基于当前受控围栏，首轮真实 `apply=true` 建议固定为：

- `max_apply_intents=1`
- `allowed_symbols=000001.SZ`
- 只允许 `intent-2026-04-15-000001.SZ`
- 单次只做 `apply=true` 一笔
- 仍保留 `require_live=true`
- 仍要求 `require_trading_session=true`

选择 `000001.SZ` 作为首轮白名单的原因不是“它一定最好”，而是：

- 它当前就是 `first_intent`
- 预演金额 `20106.0` 落在本轮建议上限内
- 不需要临时改动 `max_single_amount / stock_test_budget / reverse_repo_reserved_amount`
- 这样可以把第一轮目标收束为“验证真实执行链”，而不是同时验证多标的并发策略

若你后续更想测别的标的，也应保持同一原则：

- 只改白名单 symbol，不同时放开 `max_apply_intents`
- 不同时放开多只票
- 不在第一轮同时做换仓 / 日内 T / 自动逆回购回补

---

## 5. 压测前准入清单

- [ ] `ashare-system-v2.service` 在线
- [ ] `ashare-system-v2-scheduler.service` 在线
- [x] `ashare-feishu-longconn.service` 在线
- [ ] `/system/feishu/longconn/status` 返回 `status=connected` 且 `is_fresh=true`
- [ ] `/monitor/state` 中 execution bridge 为 `healthy`
- [ ] `/system/account-state` 可返回真实资产
- [ ] `/system/discussions/execution-precheck` 返回至少 1 条 `approved`
- [ ] 目标标的不在涨停/跌停/快照过期/价格偏离过大状态
- [ ] `emergency_stop=false`
- [ ] 若 `run_mode=live`，则 `live_trade_enabled=true`

## 5.1 已程序化的准入入口

控制面已新增只读接口：

- `GET /system/deployment/controlled-apply-readiness`

该接口会统一回看并判定：

- `run_mode/live_trade_enabled`
- Windows 执行桥与 QMT reachability
- 飞书长连接 freshness
- 交易时段要求
- discussion cycle 是否存在
- execution precheck 是否至少有 1 条 approved
- execution intents 是否至少有 1 条 intent
- 单次 apply 的 intent 数量上限
- symbol 白名单
- `emergency_stop`
- `equity_position_limit / max_single_amount / reverse_repo_reserved_amount / stock_test_budget_amount` 受控阈值

当前仓库脚本 [`scripts/check_apply_pressure_readiness.sh`](/srv/projects/ashare-system-v2/scripts/check_apply_pressure_readiness.sh) 已直接消费这个接口，而不是在 shell 中重复拼接判断。

可通过环境变量覆盖本轮压测口径：

- `ASHARE_APPLY_READY_MAX_INTENTS`
- `ASHARE_APPLY_READY_ALLOWED_SYMBOLS`
- `ASHARE_APPLY_READY_REQUIRE_LIVE`
- `ASHARE_APPLY_READY_REQUIRE_TRADING_SESSION`
- `ASHARE_APPLY_READY_MAX_EQUITY_POSITION_LIMIT`
- `ASHARE_APPLY_READY_MAX_SINGLE_AMOUNT`
- `ASHARE_APPLY_READY_MIN_REVERSE_REPO_RESERVED_AMOUNT`
- `ASHARE_APPLY_READY_MAX_STOCK_BUDGET_AMOUNT`
- `ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY`
- `ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS`

其中新增围栏含义：

- `ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY`
  - 控制当日最多允许多少次真实 `apply` 提交
  - 当前按 `execution_dispatch_history` 中的 `queued_count + submitted_count` 计数
  - 适合首轮压测限制为 `1`
- `ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS`
  - 控制禁止真实 `apply` 的时段，格式为 `HH:MM-HH:MM,HH:MM-HH:MM`
  - 例如 `09:25-09:35,14:55-15:00`
  - 适合屏蔽集合竞价后刚开盘抖动段与尾盘临近收盘段

---

## 6. 建议压测步骤

## 第一步：冻结压测范围

- 只选 1 只股票
- 只允许 1 条 execution intent 进入 `apply=true`
- 先确认 `execution-precheck` 中该票是 `approved=true`
- 记录压测时间、标的、数量、价格、账户状态截图

建议本轮直接固定为：

- `trade_date=2026-04-15`
- `account_id=8890130545`
- `intent_id=intent-2026-04-15-000001.SZ`
- `symbol=000001.SZ`
- `quantity=1800`
- `price=11.17`

## 第二步：做最终预演

- 若想按正式压测窗口顺序执行，优先使用主线脚本：

```bash
bash scripts/run_go_live_pressure_sequence.sh \
  --trade-date 2026-04-15 \
  --account-id 8890130545 \
  --intent-id intent-2026-04-15-000001.SZ \
  --allowed-symbol 000001.SZ
```

它会先执行 `check_go_live_gate.sh`，再进入单票 preview 主线。

- 先调一次：

```bash
bash scripts/run_controlled_single_apply.sh \
  --trade-date 2026-04-15 \
  --account-id 8890130545 \
  --intent-id intent-2026-04-15-000001.SZ \
  --allowed-symbol 000001.SZ
```

- 确认：
  - `preview_count=1`
  - `blocked_count=0`
  - `status=preview`
  - 已自动留档 `before_preview / after_preview` 证据
  - 已自动打印 `preview_snapshot`，包含：
    - `dispatch_status`
    - `latest_dispatch_status`
    - `latest_receipt_status`
    - `account_total_asset/account_cash`

## 第三步：执行真实 apply

- 若已进入真实压测窗口，可直接走：

```bash
bash scripts/run_go_live_pressure_sequence.sh \
  --trade-date 2026-04-15 \
  --account-id 8890130545 \
  --intent-id intent-2026-04-15-000001.SZ \
  --allowed-symbol 000001.SZ \
  --apply --confirm APPLY
```

- 再调一次：

```bash
bash scripts/run_controlled_single_apply.sh \
  --trade-date 2026-04-15 \
  --account-id 8890130545 \
  --intent-id intent-2026-04-15-000001.SZ \
  --allowed-symbol 000001.SZ \
  --apply --confirm APPLY

默认脚本口径已进一步收紧为：

- `max_apply_intents=1`
- `max_apply_submissions_per_day=1`
- `blocked_time_windows=09:25-09:35,14:55-15:00`

如需临时放宽，可在执行前覆盖环境变量：

```bash
export ASHARE_APPLY_READY_MAX_SUBMISSIONS_PER_DAY=2
export ASHARE_APPLY_READY_BLOCKED_TIME_WINDOWS="09:25-09:32,14:57-15:00"
```
```

- 期望结果不是立即 `submitted`，而是按当前 Windows gateway 架构先出现：
  - `queued_for_gateway`
  - 后续再由 Windows worker claim / receipt 回写
- 当前脚本还会自动做这些动作：
  - 留档 `before_apply / after_apply` 证据
  - 抓取 `/system/discussions/execution-dispatch/latest`
  - 抓取 `/system/execution/gateway/receipts/latest`
  - 抓取 `/system/account-state`
  - 打印 `apply_snapshot`，便于快速核对派发、回执和账户状态是否同步推进

## 第四步：核对回执

- 检查：
  - `/system/execution/gateway/intents/pending`
  - `/system/execution/gateway/receipts/latest`
  - `/system/discussions/execution-intents`
  - `/system/account-state`
- 确认：
  - intent 状态推进
  - receipt 已落库
  - 飞书已收到执行回执或阻断回执
  - `run_controlled_single_apply.sh` 已输出的 `apply_snapshot` 与证据目录内容一致

## 第五步：人工对账

- 对照 QMT / Windows 网关侧：
  - 委托编号
  - 提交时间
  - 报单状态
  - 是否成交
  - 实际成交价/量

---

## 7. 压测成功标准

- `apply=true` 请求返回非伪造结果
- 真实产生 `queued_for_gateway` 或 `submitted`
- Windows worker 真实 claim 该 intent
- receipt 真实回写到控制面
- 飞书收到执行回执
- 控制面与 Windows/QMT 对账一致
- 未突破股票测试预算、单票金额、总仓位、逆回购保留约束

---

## 8. 压测失败后的回滚动作

- 若执行桥不可达：
  - 停止继续 `apply=true`
  - 核查 Windows gateway、QMT VM、`execution_bridge_health`
- 若 receipt 未回写：
  - 先查 gateway pending / claim 状态
  - 再查 Windows worker 日志
- 若飞书未通知：
  - 查 `ashare-feishu-longconn.service`
  - 查 `/system/feishu/longconn/status`
  - 查控制面执行回执通知链
- 若出现异常连续报单：
  - 立即打开 `emergency_stop`
  - 暂停新的 `apply=true`

---

## 9. 压测前建议先定死的参数

建议先用这一版做首轮真实压测：

- `max_apply_intents=1`
- `allowed_symbols=000001.SZ`
- `max_hold_count=5`
- `equity_position_limit=0.2`
- `max_single_position=0.25`
- `max_single_amount=50000`
- `reverse_repo_reserved_amount=70000`
- `minimum_total_invested_amount=100000`
- `emergency_stop=false`

不建议在首轮压测时同时做这些动作：

- 直接把股票仓位放宽到 50%
- 同时放开多只标的
- 同时测替换仓位、日内 T、自动逆回购回补
- 同时切换主用 agent 编排链

---

## 10. 本轮建议结论

当前最稳的路线不是“直接按最终实盘纪律上 5 成仓”，而是：

1. 先按现有 20% 股票仓位上限做一次 `apply=true` 小额真实闭环压测。
2. 首轮白名单建议固定为 `000001.SZ`，只放行 `intent-2026-04-15-000001.SZ`。
2. 压测通过后，再单独推进“5 成仓纪律”和更主动的 agent 交易台策略。

这条路线更符合当前代码与联调状态，也更容易定位故障来源。
