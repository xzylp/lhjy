# ashare-system-v2 专家复核现状包

> 编写时间：2026-04-20  
> 编写目的：结合 2026-04-20 全天在线联调、实盘链路排查、当日代码整改结果，给外部专家一份可直接复核的项目现状说明。  
> 本文定位：不是宣传稿，而是“当前项目相对于最初设计目标，已经做到什么、仍缺什么、哪些有真实证据、哪些只是逻辑补齐”的审查底稿。

---

## 1. 项目原始目标

按项目当前 README 与设计要求，这个项目最初要做的不是“固定模板选股器”，而是：

```text
市场变化
-> Agent 感知市场并形成 market_hypothesis
-> Agent 自主组织 playbooks / factors / weights / constraints
-> runtime 作为工具层返回候选与证据
-> discussion / supervision 收敛提案
-> execution precheck / intent / dispatch
-> Windows Gateway / QMT
-> receipt / reconciliation / learning / governance
```

更具体地说，项目要求至少满足 5 件事：

1. Agent 不是被动填表，而是真能理解市场、组织战法、调用 runtime。
2. 盘中不仅能看持仓，还要能盯异动、抓机会、做快决策。
3. Linux 控制面、Windows Gateway、QMT、回执、对账之间要形成真实执行闭环。
4. 飞书/控制台不是死脚本问答器，而是项目的真实控制入口。
5. 所有关键动作都要留痕，可追溯，可复盘，可治理。

---

## 2. 本次复核范围

本次现状包只基于今天的真实联调和当日修复，不把“未来计划”冒充“已经达成”。

纳入范围：

- Linux 控制面在线状态
- Go/QMT 数据平面与真实账户链路
- Windows 执行桥 `poll -> claim -> submit -> receipt -> health`
- discussion / execution / pending / receipt 主链状态
- 盘中巡视、快路径、机会票注入、快速风控审批链
- 今日针对真实问题完成的代码修复

不冒充已经完成的范围：

- 多日稳定实盘收益证明
- Agent 完全自主理解市场并稳定贴合热点主线的长期证据
- 飞书自然语言入口的长期稳定实测
- 多个交易日、多个市场阶段下的盘中快链稳定性

---

## 3. 今日真实证据

### 3.1 控制面在线

今日实测：

- `http://127.0.0.1:8100/health` 返回 `status=ok`
- `http://127.0.0.1:8100/system/health` 返回 `status=ok`
- 当前运行模式为 `live`

结论：

- Linux 控制面今天是在线的，不是离线 mock 状态。

### 3.2 Go/QMT 本地桥在线

今日实测：

- `http://127.0.0.1:18793/health` 返回 `status=ok`
- `http://127.0.0.1:18793/qmt/ping` 返回：
  - `ok=true`
  - `trader_connected=true`
  - `account_id=8890130545`

结论：

- Go 数据平面在线
- QMT 交易连接在线
- Windows 本地 Python/QMT 桥不是纸面配置，而是真连接状态

### 3.3 Windows 执行桥今日已恢复闭环

今日联调时，Linux 控制面直接读到：

- 最新 receipt：
  - `intent_id=intent-2026-04-20-000008.SZ`
  - `reported_at=2026-04-20T15:45:25+08:00`
  - `status=submitted`
  - `broker_order_id=1082234806`
  - `gateway_source_id=windows-vm-a`
  - `bridge_path=linux_openclaw -> windows_gateway -> qmt_vm`

同时：

- `monitor_state.json` 中
  - `latest_execution_bridge_health.health.reported_at=2026-04-20T15:52:33+08:00`
  - `overall_status=healthy`
  - `gateway_status=healthy`
  - `qmt_status=healthy`

再同时：

- `/system/execution/gateway/intents/pending?account_id=8890130545&limit=10`
  - 返回 `0` 条

这三者合起来说明：

1. Linux 控制面今天确实生成并派发了 execution intent
2. Windows worker 今天确实 claim 并处理了 intent
3. receipt 今天确实成功回写到 Linux
4. bridge health 今天确实持续刷新
5. 当前 pending 队列已被消费清空

这意味着此前一直卡住的执行桥闭环，今天已经从“设计存在”变成了“有当天真实链路证据”。

### 3.4 QMT 订单侧也能对上

今日通过 `http://127.0.0.1:18793/qmt/account/orders?account_id=8890130545` 读取订单列表，可见：

- `order_id=1082234806`
- `stock_code=000008.SZ`
- `strategy_name=windows_gateway`
- `order_remark=intent-2026-04-20-00000`

说明：

- receipt 里的 `broker_order_id` 不是假值
- Windows Gateway 的 submit 已经进入 QMT 订单侧

需要专家注意的一点：

- 当前订单接口里该笔单的 `order_status=57`
- `status_msg` 为券商原始返回片段
- 今天只证明“桥接与报单到 QMT 订单侧”成立，不在本文中强行把该状态解释为“最终成交”或“最终失败”

### 3.5 当前准入依然不是 fully ready

今日同时检查到：

- `/system/readiness?account_id=8890130545`
  - `status=degraded`
- `/system/deployment/service-recovery-readiness?...`
  - `status=ready`
- `/system/deployment/controlled-apply-readiness?...`
  - `status=blocked`

当前 `controlled-apply` 阻断原因不是桥坏，而是策略/风控门槛问题：

- `equity_position_limit`
  - 当前实际口径：`0.4`
  - 受控 apply 准入门槛：`allowed<=0.3`
- 同时账户侧存在：
  - `pending=2`
  - `warning=1`
  - `stale=1`

结论：

- “执行桥坏了”这个问题今天已基本排除
- 当前剩余阻断，更像是受控 apply 规则口径与当前运营口径不一致，以及历史挂单/未决订单收口问题

---

## 4. 今天明确修掉了什么

以下内容不是“计划”，而是今天已经落到代码里的修复。

### 4.1 状态文件拆分

今天已经补齐：

- `discussion_state.json`
- `execution_gateway_state.json`
- 历史 `meeting_state.json` 自动迁移
- discussion context 盘后裁剪归档

修复目的：

- 避免 `meeting_state.json` 巨大化后拖死轻接口
- 让 discussion 状态与 execution gateway 状态解耦

影响判断：

- 这是控制面从“堆状态”走向“可持续运行”的基础修复
- 但“现网体积是否已稳定小于 5MB、轻接口是否稳定 <200ms”今天未在文档里虚报完成，仍需再测

### 4.2 `headline_reason` 主链统一

今天已经修复：

- `CandidateCaseService._pick_reason()`
- `resolve_headline_reason()`
- `round_summarizer`
- `system_api`

实际解决的问题：

- 之前页面与接口长期显示“进入基础样本池，等待进一步筛选”这类粗筛占位文案
- 现在讨论结论优先，不再被 runtime summary 覆盖

这类问题虽然不是策略逻辑错误，但它直接影响你对系统是否真的在思考的判断，因此必须算关键修复。

### 4.3 `go_platform` 执行模式误判

今天已经修复：

- `execution_mode=go_platform` 但 `execution_plane` 仍落在默认值时，自动纠偏
- healthcheck 新增 `go_platform /health` 探活
- 未识别 execution mode 时给出独立告警，而不是混成 `execution_adapter_unavailable`

这类修复的意义是：

- 现在控制面对于“执行面在线但口径误判”的问题更容易被及时识别

### 4.4 盘中巡视链补到执行主链

今天已经接上：

- `intraday_signal`
- `action_suggestions`
- `fast_track_review`
- `fast_track_exit()`

当前含义：

- 盘中巡视不再只是写一条 opinion 或状态
- 卖出信号现在可以进入真实执行意图链
- 风险代理可以走单独快审，不再强制等完整两轮讨论

### 4.5 盘中新机会注入候选池

今天补上：

- `fast_opportunity_scan` 增加 5m 量比、板块联动判断
- 满足：
  - 涨幅 > 5%
  - 5m 量比 >= 2
  - 板块联动
  的标的，可注入 `candidate_cases`

这一步的意义非常关键：

- 盘中链终于不再只盯持仓
- 系统开始具备“发现新机会并把它送进讨论链”的能力

但要诚实说明：

- 今天只把逻辑主链补齐了
- 还没有形成“多个交易日稳定抓到市场主线龙头”的长期证据

### 4.6 Windows 执行桥恢复

今天结合 Windows 侧修复与 Linux 侧验证，已经拿到：

- claim 成功证据
- receipt 成功证据
- bridge health 当天刷新证据
- QMT 订单侧落单证据

这是今天最重要的真实进展，因为它直接决定这个项目是不是还停留在“模拟控制面”。

---

## 5. 相对于最初目标，当前完成度判断

下面按“原始设计要求”给出当前判断。

### 5.1 要求一：Agent 成为交易团队的大脑

当前结论：`部分达成，仍未达到你最初设想`

已达到：

- Agent 相关链路、compose 结构、讨论/监督/治理骨架已经存在
- runtime 可以承接 `playbooks / factors / weights / constraints`
- 因子监控、风控、讨论、执行接口都已连成主线

仍未充分证明：

- Agent 是否真的能持续先感知市场，再组织因子与战法
- Agent 是否稳定贴合当日热门板块，而不是抽象地从预设候选里挑几只票
- Agent 是否已经具备稳定的“主线理解 -> 机会解释 -> 执行决策”能力

专家需要重点审视的问题：

- Agent 选因子和战法，到底是“自主推理”，还是“在预设集合里做受约束选择”
- 当前 compose 结果是否真正被市场状态驱动，而不只是被已有候选排序驱动

### 5.2 要求二：盘中要快，要能盯异动、抓机会、做动作

当前结论：`从骨架不足推进到主链可跑，但缺实战连续证据`

已达到：

- 秒级 `fast_position_watch`
- 30 秒级 `position_watch`
- 盘中快退审批链已补齐
- 盘中新机会可注入候选池

仍未充分证明：

- 今天并没有形成多笔成功的盘中自动止盈/止损/做T 收口证据
- 还没有证明它在真实热点主升行情里能“快人一步”
- 还没有证明多持仓、多机会并发时不会顾此失彼

专家需要重点审视的问题：

- 当前盘中触发阈值是否足够敏感，是否能在窗口稍纵即逝时抢到动作
- 机会扫描逻辑是否足够贴近 A 股超短主线，而不是只做表面异动统计

### 5.3 要求三：执行必须真闭环

当前结论：`今天拿到了真实闭环证据，这是当前项目最扎实的一块`

已真实验证：

- Linux 控制面派发 intent
- Windows worker claim
- submit 进入 QMT
- receipt 回写 Linux
- health 实时回写
- pending 清空

仍需继续确认：

- 多笔并发时的稳定性
- 异常单、拒单、撤单时的收口一致性
- stale pending order 的自动治理与对账闭环

专家需要重点审视的问题：

- 当前 bridge 闭环是否已经足够稳定，能否作为正式执行主链长期依赖

### 5.4 要求四：飞书/控制台要成为真实控制入口

当前结论：`控制台比飞书更接近真实控制入口，飞书入口仍需专家重点看`

已达到：

- Linux 控制面、dashboard、runtime/system 接口、Hermes 页面已具备真实消费业务数据的能力

仍未充分证明：

- 飞书接待口是否已经摆脱“帮助页/固定剧本器”倾向
- 是否已经足够自然地承接调参、问状态、问持仓、问候选、问执行
- 是否能让用户真正把它当作控制入口而非说明书入口

专家需要重点审视的问题：

- 飞书自然语言入口是否已经完成产品化，而不是半成品问答壳

### 5.5 要求五：所有关键动作可追溯、可治理

当前结论：`大部分主链已经可追溯，但治理成熟度仍未收口`

已达到：

- discussion / execution / receipt / bridge health / supervision / readiness 都有留痕
- 因子有效性、Elo、对账、治理接口都在主线里

仍未充分证明：

- 这些治理结果是否真正反向改变 Agent 行为
- Elo、评分、治理是否已经对实际决策产生稳定的正反馈

---

## 6. 仍然没有完成的部分

以下内容请专家直接按“真实缺口”审视，不要按“代码接口存在”判完成。

### 6.1 Agent 对市场主线的理解仍缺强证据

这是当前最接近你原始设想、但也最没法靠接口自证的部分。

当前真实问题不是“没有 Agent”，而是：

- Agent 是否真的先理解市场，再组合工具？
- 还是仍然更多依赖已有候选/已有排序/已有约束？
- 为什么之前会长期出现“不像热点主线票”的候选？

今天的修复更多是：

- 把感知、讨论、执行、巡视链补通

但这并不自动等于：

- Agent 已经像成熟超短交易员一样理解市场情绪、板块扩散和龙头辨识

### 6.2 盘中实战质量还缺连续样本

今天修的是：

- 主链能跑
- 桥已恢复
- 快路径已接通

但还缺：

- 真实多日、多票、多场景下的盘中成功样本
- 包括：
  - 快速止损
  - 冲高保护
  - 做T
  - 盘中新机会注入后进入讨论并形成执行

### 6.3 受控 apply 规则口径仍不一致

今天已经看到一个非常现实的问题：

- 当前运营口径把仓位上限调到 `0.4`
- 但 `controlled-apply` 仍按 `0.3` 在拦

这不是桥问题，也不是行情问题，而是：

- 运营参数
- 受控准入策略
- 实际执行口径

三者还没有完全统一

### 6.4 历史挂单/未决订单治理仍需继续做实

今天 readiness 仍显示：

- `pending=2`
- `warning=1`
- `stale=1`

说明：

- 即便桥通了，历史遗留订单治理和对账仍然不能掉以轻心

---

## 7. 对专家最有价值的审查问题

建议专家不要只看“代码多不多”，而重点回答下面几个问题：

1. 当前 Agent 主线，到底是“有框架的自主决策”，还是“被 runtime 和预设候选强引导的半自主决策”？
2. 当前盘中链，对超短交易最重要的“快”和“贴热点主线”，够不够？
3. 当前桥接执行链，是否已经可以从“调试态闭环”进入“正式主链闭环”？
4. 当前治理与监督，是否真的反向影响 Agent 行为，还是主要停留在留痕层？
5. 相对于最初设想，这个项目现在最缺的是：
   - 市场理解
   - 盘中速度
   - 执行稳定性
   - 治理闭环
   - 入口产品化
   哪一项？

---

## 8. 当前结论

一句话总结：

```text
这个项目今天已经从“概念上像一个 Agent 交易控制面”，推进到了“控制面、执行桥、盘中巡视、讨论与治理主链都开始形成真实运行证据”的阶段；
但距离你最初设想的“Agent 真正理解市场、主动组织战法、稳定贴合主线、持续秒级实战”的目标，仍然有明显差距。
```

更直白地说：

- 最薄弱、最致命的一环，今天是执行桥，已经拿到当天真实闭环证据。
- 当前最应该请专家继续盯的，不再是“桥通没通”，而是：
  - Agent 是否真的懂市场
  - 盘中链是否真的够快、够准、够贴主线
  - 运营口径、准入口径、执行口径是否统一

---

## 9. 相关材料

建议专家配合以下文档一起看：

- [README.md](/srv/projects/ashare-system-v2/README.md)
- [task_live_fix_20260420.md](/srv/projects/ashare-system-v2/task_live_fix_20260420.md)
- [today_live_tracking_issues_20260420.md](/srv/projects/ashare-system-v2/docs/today_live_tracking_issues_20260420.md)
- [windows_execution_bridge_issue_20260420.md](/srv/projects/ashare-system-v2/docs/windows_execution_bridge_issue_20260420.md)
- [windows_execution_bridge_issue_response_20260420.md](/srv/projects/ashare-system-v2/docs/windows_execution_bridge_issue_response_20260420.md)
- [expert_review_20260419.md](/srv/projects/ashare-system-v2/docs/expert_review_20260419.md)
