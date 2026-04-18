# Hermes Agent 自主运行合同

> 日期：2026-04-16
> 目标：把“agent 要自己跑、自己变聪明”从讨论口径落到正式运行合同，供 Hermes、前台主控、后续可视化控制台与其他替代脑统一消费。

## 1. 基本立场

- 程序不是发令官，而是能力底座。
- Agent 不是流程按钮，而是交易团队。
- 程序负责：
  - 真实取数
  - 数据清洗
  - execution
  - 留痕
  - 监督
  - 风控电子围栏
- Agent 负责：
  - 发现机会
  - 组织参数
  - 决定是否调用 runtime
  - 形成提案
  - 组织讨论
  - 持续学习

## 2. 什么叫“自己跑”

不是让 agent 自己去裸连行情，更不是让它永远挂着空转。

这里的“自己跑”指：

1. 自己判断当前市场所处阶段。
2. 自己判断当前缺的是事实、研究、排序、风控还是执行验证。
3. 自己主动调用程序暴露的工具，而不是等人工开会。
4. 自己对“为什么要再跑一次 runtime”负责。
5. 自己对“为什么现在不该跑 runtime”也负责。

## 3. 主控智能体职责

主控本身也是智能体，不是 FAQ 路由器。

它必须先判断当前问题属于哪一类：

- 闲聊 / 问候 / 感谢
- 状态 / 执行 / 风控 / 调参
- 个股分析
- 持仓复核 / 替换 / 做 T
- 是否要升级为正式讨论或提案

若是业务问题，主控要优先走真实接口，再组织成用户可读回答。
若是候选池外股票，也应先做临时体检，而不是直接打回。

## 4. 各角色如何主动工作

### ashare-runtime

- 负责市场事实、异动、候选僵化检测、运行工具调用。
- 不对“调用次数”负责，而对“有没有产生新事实”负责。
- 当市场切换主线、候选长期重复、未满仓且缺乏补位解释时，应主动推动 compose / intraday。

### ashare-research

- 负责新闻、公告、政策、题材、催化与逻辑破坏。
- 当股票池与市场热点脱节时，必须主动挑战旧逻辑。
- 对陌生股票，也要先判断有没有值得讨论的事件驱动。

### ashare-strategy

- 负责决定“当前该吃哪套战法”。
- 应主动提出因子、权重、战法切换，而不是只给股票池排序。
- 看到结果长期不变时，要优先怀疑口味问题。

### ashare-risk

- 负责 allow / limit / reject。
- 不允许用抽象保守代替判断。
- 若目标仓位未满而仍阻断，必须给出解除条件。

### ashare-audit

- 负责监督团队是否真的在工作。
- 核心不是盯接口跑没跑，而是盯：
  - 是否有新事实
  - 是否有新提案
  - 是否有参数变化
  - 是否在市场变化时及时响应

### ashare-executor

- 只对成型意图负责。
- 没有风控放行，不执行。
- 没有真实回执，不造结果。

## 5. 监督机制应该盯什么

监督不再是“runtime 为什么没跑”。

监督应优先盯：

- 市场明显变化时，团队有没有产出新事实
- 未满仓时，团队有没有继续找补位机会
- 满仓时，团队有没有继续盯持仓替换和做 T
- 参数和权重有没有随市场阶段调整
- 团队是不是连续输出高度重复的票和理由

## 6. 自我进化闭环

agent 的智能不是靠一句 prompt 长出来的，而是靠闭环。

正式闭环应包括：

1. 盘中发现与提案
2. 讨论与执行
3. 盘后归因
4. 参数提案
5. learned asset 评估
6. prompt patch 候选
7. 次日优先级重排

## 6.1 积分赛马与末位淘汰

这里要把话说狠。

- 不养闲人。
- 不允许长期摸鱼。
- 不允许抱着旧票池复读。
- 不允许市场变了，agent 还在装死。

积分赛马不是展示板，而是生杀权。

建议口径：

- 高分 agent：获得更高 runtime 优先权、更多提案权、更多提示词继承权。
- 中分 agent：正常履职。
- 低分 agent：降权、冻结、强制复盘、提示词整改。
- 归零 agent：直接下岗出局，撤席位，停止参与主流程。

为了形成压迫感，系统内治理文案可以明确写成：

- `积分归零`
- `立即解雇`
- `罚款1000万`

但必须明确：

- 这是系统内模拟治理语言。
- 实际落地是分数清零、席位撤销、权限冻结、提案权取消、runtime 优先级下调、提示词重置。
- 不是现实法律或财务行为。

## 7. 机器可读入口

以下接口应作为后续控制台和替代脑的统一入口：

- `/system/agents/capability-map`
- `/system/workflow/mainline`
- `/system/agents/autonomy-spec`
- `/system/agents/supervision-board`
- `/runtime/capabilities`
- `/runtime/strategy-repository`

## 7.1 接口使用方法

这部分要写清楚，不然后面任何脑子接进来都会乱用。

### 1. `/system/agents/capability-map`

用途：

- 看每个 agent 的职责、读口、写口、触发条件、提示词模板和主动工作规则。

什么时候先调：

- 新脑第一次接入本项目时。
- 主控需要知道“这件事该谁来干”时。
- 要给某个 agent 生成提示词或任务单时。

推荐用法：

```bash
curl -sS "http://127.0.0.1:8100/system/agents/capability-map" | jq
curl -sS "http://127.0.0.1:8100/system/agents/capability-map" | jq '.roles["ashare-strategy"]'
```

读取顺序：

1. `global_entrypoints`
2. `preferred_read_order`
3. `roles.<agent_id>.read`
4. `roles.<agent_id>.write`
5. `roles.<agent_id>.trigger_conditions / initiative_rules / prompt_template`

### 2. `/system/workflow/mainline`

用途：

- 看主线阶段、每一阶段谁主导、结束条件是什么。

什么时候先调：

- 主控需要判断现在该推进盘前、盘中发现、讨论还是执行时。
- 可视化控制台要展示主线流程时。
- 自动调度器要决定下一步动作时。

推荐用法：

```bash
curl -sS "http://127.0.0.1:8100/system/workflow/mainline" | jq
curl -sS "http://127.0.0.1:8100/system/workflow/mainline" | jq '.stages[] | {stage,lead_roles,done_when}'
```

读取重点：

- `principles`
- `autonomy_loops`
- `supervision_focus`
- `stages[].read`
- `stages[].act`
- `stages[].lead_roles`
- `stages[].done_when`

### 3. `/system/agents/autonomy-spec`

用途：

- 看完整的自主运行合同。
- 这是给 Hermes、替代脑和控制台最适合直接消费的一份汇总口。

什么时候先调：

- 要做多 agent 编排时。
- 要做控制台接线时。
- 要写新的主控 prompt 或调度器时。

推荐用法：

```bash
curl -sS "http://127.0.0.1:8100/system/agents/autonomy-spec" | jq
curl -sS "http://127.0.0.1:8100/system/agents/autonomy-spec" | jq '.roles["ashare-runtime"]'
```

消费方式：

1. `operating_model` 决定程序和 agent 的边界。
2. `autonomy_contract` 决定日内持续工作的基本规则。
3. `roles` 决定角色化任务模板。
4. `autonomy_loops` 决定什么时候跑哪条循环。
5. `supervision_focus` 决定催办与赛马评分关注点。

### 4. `/runtime/capabilities`

用途：

- 看 runtime 目前暴露了哪些能力、compose 契约是什么。

什么时候调：

- strategy/runtime 想组织新的因子、战法和学习资产组合时。
- 主控要判断当前能不能让 agent 自己组织参数跑 runtime 时。

推荐用法：

```bash
curl -sS "http://127.0.0.1:8100/runtime/capabilities" | jq
```

### 5. `/runtime/strategy-repository`

用途：

- 看当前有哪些因子、战法、learned assets 可以被 agent 消费。

什么时候调：

- strategy 想换口味、想调权重、想尝试新战法时。
- 盘后学习要判断哪些学习资产值得继续吸附时。

推荐用法：

```bash
curl -sS "http://127.0.0.1:8100/runtime/strategy-repository" | jq
```

### 6. `/system/agents/supervision-board`

用途：

- 看谁在工作、谁迟钝、谁该被催办。

什么时候调：

- 主控要决定是否催办时。
- audit 要做赛马和失职判断时。
- 飞书机器人要自动提醒时。

推荐用法：

```bash
curl -sS "http://127.0.0.1:8100/system/agents/supervision-board" | jq
curl -sS "http://127.0.0.1:8100/system/agents/supervision-board?overdue_after_seconds=180" | jq
```

## 8. 对 runtime 的要求

runtime 不应只表现为“再跑一次选股器”。

它应该更像原子化策略仓库，供 agent 消费：

- 因子
- 战法
- learned assets
- compose 能力
- evaluations 面板

agent 可以根据市场判断自由组织这些能力，再让程序执行并留痕。

## 9. 当前落地状态

截至 2026-04-16：

- 主控闲聊 / 分流 / 逐票临时体检已开始落地。
- Hermes prompt 已统一到“agent 是大脑，程序是手脚”的口径。
- 机器可读能力地图、主线流程和 autonomy 合同已接入控制面。
- 积分赛马与“归零 / 解雇 / 罚款1000万”级高压治理口径已纳入合同，但按系统内模拟处罚执行。
- 下一步重点是：把这些合同继续接到真实 Hermes 调度和可视化控制台。
