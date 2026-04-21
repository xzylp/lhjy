# 2026-04-20 实盘跟盘主要问题记录

## 一句话结论

今天系统并不是完全“没跑起来”，而是出现了三类问题同时叠加：

1. 真实下单链有一条已经打通，但只是在绕开原桥接问题后打通。
2. 讨论与选股主链仍然带有明显的“基础样本池占位”残留，导致界面和结果都像没真正贴合市场。
3. 控制面状态存储膨胀，已经开始反向拖慢 API 与 gateway 相关轻接口。

---

## 今日已确认的真实事实

### 1. 真实交易并非完全失败

今天已经确认至少有一笔真实买单成功报出到券商侧：

- 标的：`000002.SZ`
- 数量：`5100`
- 价格：`3.92`
- `order_id`：`1082219891`
- 状态：`ACCEPTED`

券商侧通过 `http://127.0.0.1:18793/qmt/account/orders` 能查到该订单，说明：

- QMT 侧真实报单能力存在
- Go 数据平面与券商连接正常
- “今天完全不能报单”这个判断不成立

这也反过来说明，后续未成交或旧通道未通，不是单纯“券商挂了”或“网络不通”。

### 2. 讨论链今天确实在运行，但输出质量不达标

从日志与状态文件看，今天这些任务在持续执行：

- 持仓快巡视
- 持仓深巡视
- 盯盘巡检
- 微观巡检
- 研究/风控/审计自动写回
- discussion cycle 刷新

也就是“调度器没工作”这个判断也不成立。真实问题是：

- 跑了，但输出没有形成贴近市场热点的结果
- 跑了，但很多结论仍是泛化占位文案
- 跑了，但控制面和执行桥的状态回写没有形成稳定闭环

---

## 今日主要问题

## 问题 1：原 Windows bridge 通道没有形成稳定闭环

今天最核心的问题仍是：

- `pending execution intent`
- `claim`
- `receipt`

这条 Windows bridge 主链没有稳定跑通。

已经看到的硬证据：

- `monitor_state.json` 中 `latest_execution_bridge_health` 仍停留在 `2026-04-18T07:46:25+08:00`
- 今天 Linux 控制面没有收到新的桥健康上报
- `meeting_state.json` 中存在 `pending_execution_intents`
- 但桥接回执和健康状态没有同步更新到今天

这说明：

- 不是 Linux 没产生 intent
- 而是 Windows 侧 worker 轮询、认领、回执至少有一段没有持续工作

这个问题已另写专项文档：

- [windows_execution_bridge_issue_20260420.md](/srv/projects/ashare-system-v2/docs/windows_execution_bridge_issue_20260420.md)

## 问题 2：选股主链仍被“基础样本池”逻辑强烈主导

今天的 `candidate_cases` 和 `discussion cycle` 显示，很多票虽然已经经过多轮 opinion 写回，但界面与摘要仍然保留以下风格的文案：

> 进入基础样本池，等待 Agent 组织 compose 参数发起深度扫描

这反映出两个层面的问题：

- 显示层还在暴露 runtime 粗筛文案
- 真实 strategy/research 结论没有完整接管最终展示口径

同时，`execution_pool` 的形成仍然高度依赖已有候选池，不是彻底由 Agent 在更大空间里自由重组。

实际表现就是：

- 改了策略，仍然容易看到老的那批基础样本
- 即使 strategy 已经写回新 stance，页面上看起来还是像旧逻辑
- 用户主观感受会是“换了策略但没换票”

## 问题 3：`selected_reason` / `headline_reason` 有明显显示偏差

今天实测中已经确认：

- 某些 case 实际已经有新的 `ashare-strategy` opinion
- 但展示出来的理由仍然优先落回 `runtime_snapshot.summary`

结果是：

- 用户看到的不是最新讨论结论
- 而是最早的基础样本池粗筛摘要

这会直接造成误判：

- 看起来像 compose 没生效
- 看起来像 Agent 没在重新组织理由
- 实际上是显示优先级有问题

## 问题 4：控制面状态文件过大，已经反噬接口响应

今天确认：

- `/srv/data/ashare-state/meeting_state.json` 已膨胀到约 `136 MB`
- 其中最大块来自 `latest_discussion_context` 与 `discussion_context:2026-04-20`
- API 轻接口请求已出现卡住现象
- `serve` 主线程出现 `locks_lock_inode_wait`

这说明当前状态存储存在架构性问题：

- 轻量 gateway 状态
- 大体积 discussion context
- 其他执行/讨论快照

都被塞进同一个大 JSON 文件，并通过同一把文件锁访问。

直接后果：

- 本来很轻的 `/execution/gateway/*` 也会跟着卡
- scheduler 持锁时，serve 侧轻接口也会被拖住
- 秒级巡视与执行协同会越来越不稳定

## 问题 5：盯盘链在“有动作”和“有效动作”之间还有距离

日志能证明盯盘任务在跑，但用户感受到的主要问题仍然成立：

- 没有形成足够强的盘中机会捕捉
- 没有体现出“快人一步执行”的超短优势
- 对已有持仓的做 T、减仓、卖出建议不够敏感
- 对热门板块和涨停基因的贴合度不够

本质上，这不是“定时器没挂”，而是：

- 盯盘输出还偏巡检型
- 交易动作联动还不够强
- 市场感知与执行决策的耦合度还不够高

---

## 今日定位出的 Linux 侧问题

这些问题属于项目自身实现，不应甩锅给 Windows：

### 1. `go_platform` 曾被错误判成不可直连执行

今天已确认原有执行分支里，`go_platform` 没被当作可执行模式处理，导致本应可走的真实执行链被误判成：

- `execution_adapter_unavailable`

这属于 Linux 控制面代码问题，不是 Windows 网络问题。

### 2. gateway 状态复用 `meeting_state.json` 是错误设计

当前 `execution gateway` 的待执行、回执、历史记录，不应该与大体量 `discussion_context` 共用同一主状态文件。

这不是偶发卡顿，而是结构性隐患。

### 3. compose 结果的展示接管不彻底

今天能看到 strategy 已写回真实 opinion，但最终展示层没有完整反映出来，导致用户误以为策略根本没切换。

---

## 今日定位出的 Windows 侧问题

Windows 侧专项问题单独见：

- [windows_execution_bridge_issue_20260420.md](/srv/projects/ashare-system-v2/docs/windows_execution_bridge_issue_20260420.md)

这里只保留总判断：

- 当前 Windows bridge 不是“偶发慢”，而是“状态回写链长期不新鲜”
- 不是网络不通
- 是 worker 轮询/claim/receipt/health 上报链至少有一段没有稳定工作

---

## 对今天整体运行状态的判断

今天系统状态可以定义为：

- 真实报单能力：部分可用
- 讨论链：运行中，但表达与接管不彻底
- 盯盘链：运行中，但交易动作价值不足
- 执行桥：不稳定，且 Windows 侧专项问题明显
- 控制面：已出现状态文件膨胀与锁竞争

因此今天的问题不是“系统完全没启动”，而是：

- 真实能力存在
- 但主链闭环不稳
- 且界面、状态、执行桥之间存在明显错位

---

## 结论

今天的主要问题可以归并为四条主线：

1. Windows bridge 没有稳定形成 `pending -> claim -> receipt -> health` 的持续闭环。
2. 选股与讨论链虽然在跑，但仍残留“基础样本池主导”的旧痕迹，导致结果看起来不像真正理解市场。
3. 控制面把大体量 discussion context 和 gateway 状态混存，已经造成文件锁等待和接口卡顿。
4. 盯盘与盘中执行更像“有巡视”，还不像“有超短交易优势”。

下一步应分两条线推进：

- Windows 侧先把桥接闭环修稳
- Linux 侧把状态存储拆分、展示接管修正、盘中策略联动继续强化
