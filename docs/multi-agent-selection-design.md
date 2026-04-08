# ashare-system-v2 多 Agent 选股协作设计稿

> 状态: Draft  
> 版本: v0.9  
> 日期: 2026-04-04

## 1. 目标

本文档定义 `ashare-system-v2` 在 OpenClaw 体系下的多 Agent 选股协作机制，用于实现：

- 主程序先筛出 `10-20` 只候选股票
- 多 Agent 围绕候选股票进行 `2` 轮讨论
- 形成完整的入选、观察、落选理由
- 从中收敛出最终 `1-3` 只执行候选
- 将讨论过程、证据链、风控限制、审议结论对用户可解释展示

本文档只定义协作机制、消息结构、收敛规则和展示模型，不直接约束具体代码实现。

实现草案配套文件：

- [param-registry.draft.yaml](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/param-registry.draft.yaml)
- [state-machine-transition-table.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/state-machine-transition-table.md)
- [core-object-schemas.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/core-object-schemas.md)
- [minimal-rollout-plan.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/minimal-rollout-plan.md)

## 2. 设计原则

### 2.1 总体原则

- `main` 仍是唯一前台入口
- `ashare` 是量化中台主持人和消息总线，不直接替代叶子 Agent 做专业判断
- 多 Agent 协作要真实利用各自职责，而不是伪装成一条串行审批链
- 所有讨论都必须可追溯、可审计、可复盘
- 最终结果必须让用户看见“为什么入选、为什么落选、谁支持、谁反对、证据是什么”
- 每个交易日必须生成股票池，股票池不可缺席
- 所有阈值默认视为动态变量，不视为永久写死常量

### 2.2 技术原则

- 子代理之间不直接自由点对点聊天，统一通过 `ashare` 中转和归档
- 讨论轮次固定为 `2` 轮，避免无限讨论
- `risk` 保留明确风控否决权
- `audit` 参与讨论，但主要负责质量监督，不直接主导排名
- `executor` 只接收已通过讨论收敛、风控和审议的最终执行候选

## 3. 角色定义

### 3.1 主干角色

| Agent | 定位 | 作用 |
|------|------|------|
| `main` | 唯一前台 | 接收用户请求、转发给 `ashare`、统一对外回复 |
| `ashare` | 讨论主持人 | 管理候选池、分发讨论任务、归档观点、收敛结果 |

### 3.2 量化子代理

| Agent | 定位 | 核心职责 |
|------|------|----------|
| `ashare-runtime` | 候选输入层 | 提供主程序筛出的 `10-20` 只候选及基础数据包 |
| `ashare-research` | 研究层 | 给出新闻、公告、基本面、事件、情绪观点 |
| `ashare-strategy` | 策略层 | 给出排序、策略适配、打分依据、观察名单 |
| `ashare-risk` | 风控层 | 给出 `ALLOW / LIMIT / REJECT`、仓位与执行限制 |
| `ashare-audit` | 审议层 | 检查讨论充分性、证据质量、逻辑一致性 |
| `ashare-executor` | 执行层 | 只接收最终 `1-3` 只执行候选并负责执行回执 |

## 4. 输出目标

系统应输出两层结果：

### 4.1 候选评选结果

- 覆盖 `10-20` 只候选股票
- 每只股票必须有：
  - 当前状态：`selected` / `watchlist` / `rejected`
  - 研究理由
  - 策略理由
  - 风控意见
  - 审议备注
  - 关键数据证据

### 4.2 最终执行结果

- 只保留最终 `1-3` 只股票进入执行准备
- 每只股票必须有：
  - 为什么最终胜出
  - 关键支持证据
  - 关键反对意见是否已解决
  - 当前风控条件
- 是否允许进入执行层

### 4.3 股票池要求

系统每天至少维护三层股票池：

- `base_pool`
  - 当日基础候选池，必须存在
- `focus_pool`
  - 多 Agent 讨论后重点关注池
- `execution_pool`
  - 最终 `1-3` 只执行池

其中：

- `base_pool` 是日常运行必选项
- `focus_pool` 和 `execution_pool` 是在 `base_pool` 基础上继续收敛

## 5. 协作流程

### 5.1 总体流程

```text
main
  -> ashare
  -> ashare-runtime           # 生成候选池与基础数据包
  -> Round 1:
       ashare-research
       ashare-strategy
       ashare-risk
       ashare-audit
  -> ashare 汇总第一轮讨论卡
  -> Round 2:
       ashare-research
       ashare-strategy
       ashare-risk
       ashare-audit
  -> ashare 收敛最终评选结果
  -> ashare-executor          # 仅针对最终 1-3 只且已满足执行前提
  -> main
```

### 5.2 Phase 0: 候选池生成

由 `ashare-runtime` 提供统一候选包。

候选包至少包含：

- `symbol`
- `name`
- `rank`
- `selection_score`
- `action`
- `score_breakdown`
- `summary`
- `market_snapshot`
- `runtime_report_ref`

### 5.3 Round 1: 独立陈述

第一轮不做相互反驳，各代理独立对全部候选给出本视角判断。

#### `ashare-research`

逐只输出：

- 研究支持点
- 研究反对点
- 事件驱动或公告摘要
- 情绪和置信度

#### `ashare-strategy`

逐只输出：

- 排名依据
- 因子解释
- 为什么适合当前策略
- 为什么进优先观察或淘汰

#### `ashare-risk`

逐只输出：

- 当前可执行性态度
- 单票/总仓/行业暴露限制
- 是否受 `paper`、账户、执行链路影响
- 初步 `ALLOW / LIMIT / REJECT`

#### `ashare-audit`

第一轮不盖章，只做质量观察：

- 证据是否足够
- 是否有明显逻辑跳跃
- 哪些股票需要在第二轮重点解释

### 5.4 Round 1 汇总

由 `ashare` 为每只股票生成一张讨论卡：

- `support_points`
- `oppose_points`
- `evidence_gaps`
- `current_status`
  - `keep`
  - `watch`
  - `drop`
- `questions_for_round_2`

### 5.5 Round 2: 交叉质询

第二轮只讨论以下对象：

- 排名前列票
- 争议较大的票
- 证据不充分但仍有潜力的票

第二轮不再扩展新候选，只围绕已有候选澄清分歧。

#### 交叉讨论规则

- `research` 回应策略和风险提出的证据问题
- `strategy` 回应研究和风险提出的排序质疑
- `risk` 对争议票给出最终执行态度
- `audit` 检查关键反对意见是否被正面回应

### 5.6 最终收敛

由 `ashare` 生成两份结果：

- `review_list_10_20`
- `execution_shortlist_1_3`

并给出每只股票的最终状态：

- `selected`
- `watchlist`
- `rejected`

### 5.7 执行门

只有同时满足以下条件的股票才允许进入 `ashare-executor`：

- `risk` 最终态度不是 `REJECT`
- `audit` 未触发严重阻断
- 执行链路健康
- 当前运行模式允许执行

## 6. 状态机设计

每只股票在讨论过程中的状态建议统一为：

```text
candidate
  -> under_review
  -> selected
  -> watchlist
  -> rejected
```

附加门控状态：

```text
execution_gate
  -> allow
  -> limit
  -> reject
```

附加审议状态：

```text
audit_gate
  -> clear
  -> hold
```

### 6.1 最终执行判断

建议采用：

```text
if risk == reject:
    execution = blocked
elif audit == hold:
    execution = blocked
elif runtime/executor unhealthy:
    execution = blocked
else:
    execution = ready
```

## 7. 消息结构

### 7.1 基础对象

每条讨论消息建议统一结构：

```json
{
  "type": "agent_opinion",
  "round": 1,
  "symbol": "600519.SH",
  "agent_id": "ashare-research",
  "stance": "support",
  "confidence": "high",
  "reasons": [
    "业绩确定性高",
    "事件催化清晰"
  ],
  "evidence": [
    {
      "kind": "research_event",
      "source": "news|announcement|runtime|system",
      "ref": "event-or-report-id",
      "summary": "简要证据"
    }
  ],
  "questions_to_others": [
    "当前估值是否已透支动量逻辑？"
  ],
  "blocking_flags": []
}
```

### 7.2 候选包

```json
{
  "type": "candidate_packet",
  "generated_at": "<ISO8601>",
  "source": "ashare-runtime",
  "candidates": [
    {
      "symbol": "600519.SH",
      "rank": 1,
      "selection_score": 85.6,
      "action": "BUY",
      "score_breakdown": {
        "momentum": 0.82,
        "liquidity": 0.76,
        "price_bias": 0.68
      },
      "summary": "量价动能偏强"
    }
  ]
}
```

### 7.3 讨论卡

由 `ashare` 汇总：

```json
{
  "type": "candidate_case",
  "symbol": "600519.SH",
  "round_1_summary": {
    "support_points": [],
    "oppose_points": [],
    "evidence_gaps": [],
    "questions_for_round_2": []
  },
  "round_2_summary": {
    "resolved_points": [],
    "remaining_disputes": []
  },
  "final_status": "selected",
  "risk_gate": "limit",
  "audit_gate": "clear"
}
```

### 7.4 最终结果

```json
{
  "type": "final_selection",
  "generated_at": "<ISO8601>",
  "review_list_10_20": [],
  "execution_shortlist_1_3": [],
  "rejected_list": []
}
```

## 8. 轮次规则

### 8.1 固定轮次

- 总讨论轮次固定 `2` 轮
- 不允许自动开启第 `3` 轮
- 第二轮结束后必须进入收敛

### 8.2 第二轮范围限制

第二轮只讨论：

- `strategy` 排名前列的票
- `risk` 有明显限制但未彻底否决的票
- `audit` 标记证据不足但可补的票

## 9. 收敛规则

### 9.1 候选池收敛

全部 `10-20` 只都保留最终状态和理由。

分类规则建议：

- `selected`
  - 综合观点正向
  - 风险可控
  - 证据足够
- `watchlist`
  - 有亮点，但仍有争议或条件待满足
- `rejected`
  - 核心逻辑不成立
  - 风险明确不可接受
  - 证据不足且第二轮未补足

### 9.2 最终执行池收敛

最终执行池只取 `1-3` 只。

建议条件：

- `strategy` 排名前列
- `research` 没有重大负面结论
- `risk != REJECT`
- `audit != HOLD`
- 当前执行条件满足

## 10. 风控与审议权责

### 10.1 `risk` 权限

`risk` 拥有明确风控否决权。

可执行动作：

- `ALLOW`
- `LIMIT`
- `REJECT`

其中：

- `REJECT` 直接阻断执行
- `LIMIT` 允许保留在评选结果里，但不一定允许真实执行

### 10.2 `audit` 权限

`audit` 参与讨论，但不主导选股结果。

建议权限边界：

- 可以标记证据不足
- 可以要求补充说明
- 可以在严重断链时触发 `HOLD`
- 不直接改策略排名
- 不直接替代 `risk` 做放行

## 11. 阻断规则

建议只保留三类强阻断：

- `RISK_REJECT`
- `AUDIT_HOLD`
- `EXECUTION_UNAVAILABLE`

除此之外，默认优先降级为：

- `watchlist`

而不是轻易全盘阻断。

## 12. 用户可见结果模型

### 12.1 候选评选表

面向用户展示 `10-20` 只候选：

| 字段 | 说明 |
|------|------|
| 股票 | 标的代码与名称 |
| 当前结论 | selected / watchlist / rejected |
| 研究理由 | 研究支持与反对点 |
| 策略理由 | 排序与因子解释 |
| 风控意见 | allow / limit / reject 与条件 |
| 审议备注 | 证据是否充分、是否有未解争议 |
| 数据支撑 | 关键证据摘要 |

### 12.2 最终执行表

面向用户展示最终 `1-3` 只：

| 字段 | 说明 |
|------|------|
| 股票 | 最终胜出的标的 |
| 胜出原因 | 多 Agent 综合结论 |
| 关键证据 | 核心数据和事件支撑 |
| 已解决争议 | 第二轮被解决的关键问题 |
| 风控条件 | 仓位、模式、执行限制 |
| 执行状态 | ready / blocked |

### 12.3 落选说明

对落选股票展示：

- 为什么落选
- 是谁提出了关键反对
- 对应证据或规则
- 是否进入后续观察名单

## 13. 与现有架构的关系

本方案不要求当前就引入 OpenClaw Teams。

推荐先保持现有主干：

```text
main -> ashare -> 子代理
```

只是把 `ashare` 的职责从“简单路由器”升级为“讨论主持人 + 结果收敛器”。

Teams 可以作为后续增强层使用，但不是本版方案的前置依赖。

## 14. 本版建议固定结论

- 最终执行只取 `1-3` 只
- 候选保留 `10-20` 只完整评选记录
- 总讨论轮次固定 `2` 轮
- `risk` 有明确否决权
- `audit` 参与讨论，但主要做质量监督，不直接主导结果
- 所有讨论通过 `ashare` 汇总，不做自由点对点乱聊

## 15. Agent 学分与裁撤机制

### 15.1 目标

多 Agent 讨论机制如果只有过程、没有后验奖惩，就无法形成真实学习闭环。

因此系统需要为量化子代理引入统一的学分治理机制，用于：

- 根据实际市场结果评估 Agent 判断质量
- 将“讨论质量”转化为长期信用
- 对持续低质量 Agent 触发降权、待重训、待裁撤
- 为后续自我学习和策略改进提供量化依据

### 15.2 学分账户

建议每个量化子代理持有独立学分账户：

- `ashare-research`
- `ashare-strategy`
- `ashare-risk`
- `ashare-audit`

当前不建议把 `runtime` 和 `executor` 纳入同一套方向性学分口径，因为它们更偏运行与执行层，不直接承担选股判断。

### 15.3 基础分与上下限

- 基础分：`10`
- 最高分：`20`
- 最低分：`0`

当学分降到 `0` 时，系统必须明确通知：

- 该 Agent 进入 `待裁撤 / 待重训` 状态
- 该状态必须写入学习与审计链路
- 该 Agent 在后续讨论中的关键意见权需要被暂停或显著降权

### 15.3.1 学分与权重映射

学分不仅是历史记录，也直接决定该 Agent 在讨论和最终收敛中的权重。

本版建议采用分段权重：

| 学分区间 | 信用状态 | 建议权重 |
|------|------|---------|
| `16-20` | 高信用 | `1.2 - 1.5` |
| `10-15` | 正常 | `1.0` |
| `4-9` | 低信用 | `0.6 - 0.8` |
| `1-3` | 弱信用 | `0.2 - 0.4` |
| `0` | 待裁撤 / 待重训 | `0` |

说明：

- 高分 Agent 在争议票和最终 shortlist 收敛中有更高影响力
- 低分 Agent 仍可发言，但更偏参考意见
- `0` 分 Agent 不参与关键表决

### 15.4 结算时点

本版建议采用固定结算时点：

- 以 **下一交易日收盘** 为准

原因：

- 避免盘中波动过早评价 Agent
- 给讨论结论足够的验证窗口
- 便于统一形成可复盘的日结批次

### 15.5 评分对象

评分对象不是单条消息，而是 Agent 对股票评定池和最终执行候选的判断质量。

建议分两层：

#### A. 候选池评定层

面向 `10-20` 只候选股票：

- 某 Agent 对某股票给出 `support / watch / oppose`
- 次日收盘后，根据实际涨跌和相对表现做后验评价

#### B. 最终执行层

面向最终 `1-3` 只：

- 判断最终执行 shortlist 是否优于未入选或落选标的
- 用于提高对“最终决策能力”的权重考核

### 15.6 记分建议

本版先定义原则，不锁死绝对数值。

#### `research` / `strategy`

更偏方向性与排序质量：

- `support` 且次日收盘表现验证正确：加分
- `support` 且次日收盘表现明显错误：扣分
- `oppose` 且次日收盘表现验证正确：加分
- `oppose` 且次日收盘表现明显错误：扣分
- `watch` 采用低权重加减分

#### `risk`

不能只按涨跌评分，而应按“风险规避与错误拦截质量”评分：

- 正确阻止高风险或不满足条件的标的：加分
- 错误放行明显不该执行的标的：重扣
- 在 `paper`、账户异常、规则冲突时正确限制执行：加分

#### `audit`

不能按涨跌直接打分，而应按“过程质量监督”评分：

- 正确发现证据断裂、逻辑矛盾：加分
- 错误放过明显流程缺陷：扣分
- 无意义地频繁阻断正常流程：扣分

### 15.7 学分状态分层

建议定义四段治理状态：

#### 高信用状态

- `score >= 16`
- 权重高于默认值
- 其有效方法可进入学习参考池

#### 正常状态

- `score >= 10`
- 正常参与讨论与表决

#### 低信用状态

- `score <= 9`
- 权重下降
- 发言仍可保留，但必须附更强证据
- 自动进入自学习补强模式

#### 待裁撤 / 待重训状态

- `score = 0`
- 系统明确发出内部通知
- 暂停关键结论权或转为旁听状态
- 进入学习与复盘队列

### 15.8 到 0 分后的系统动作

当 Agent 学分到 `0` 时，建议触发以下动作：

1. 写入 `audit` 与学习记录
2. 在 `ashare` 侧标记为 `suspended`
3. 暂停其关键投票权
4. 后续讨论中仅保留参考发言，不能主导关键结论
5. 进入待重训或替换流程

### 15.8.1 恢复路径

`0` 分代表进入治理态，不代表立即物理删除。

建议恢复路径：

1. 进入 `suspended` 状态
2. 开始自学习和复盘
3. 提交新的可验证策略建议、证据模板或判断修正方案
4. 被系统采纳并在后续验证中证明有效
5. 恢复为低信用状态并重新获得有限权重

恢复后的起始分建议：

- `2-4`

避免一次恢复就回到正常信用状态。

### 15.9 审计与学习链路要求

每次学分结算都应形成可追溯记录，至少包含：

- 结算日期
- 评估股票池
- Agent 当日 stance
- 次日收盘验证结果
- 本次加减分
- 当前累计分
- 是否触发低信用或待裁撤状态

### 15.9.1 自学习机制

低分 Agent 不应只是被动扣分，而应进入主动补强模式。

建议的学习来源分为两类：

#### A. 内部来源

- 历史成功案例
- 高分 Agent 的有效证据结构
- 历史复盘记录
- 已被审计确认过的高质量讨论样本

#### B. 外部来源

- 公开策略框架
- 公开研究方法
- 公开市场经验与建议

但外部来源必须满足：

- 可追溯
- 可结构化
- 不能直接当作最终结论
- 必须先转化成内部可验证规则

### 15.9.2 学习加分机制

学习加分不能因为 Agent 自称“学会了”就直接发放。

建议采用四步闭环：

1. Agent 提交新的可验证策略建议、证据模板或风险规则
2. `audit` 或学习模块记录为候选改进项
3. 该改进被流程采纳
4. 后续实际结果验证该改进有效

只有完成上述闭环，才允许给予学习加分。

### 15.9.3 学习加分用途

学习加分用于奖励：

- 能识别自身错误模式
- 能吸收更优方法
- 能提出被采纳且有效的改进

它的作用是让低分 Agent 有恢复路径，而不是只存在淘汰机制。

### 15.10 本版暂不细化的点

以下内容保留到实现阶段再定：

- 不同市场环境下的加减分斜率
- 单只股票与组合层表现的权重占比
- 是否引入滚动窗口衰减
- 是否按行业、风格、策略类别分开计分
- 学习加分与结果加分的精确比例

## 16. 学分结算公式框架

### 16.1 目标

学分结算需要同时满足：

- 能反映 Agent 对候选判断的实际质量
- 能区分不同角色的职责差异
- 能把“讨论正确”与“学习进步”分开结算
- 公式先足够稳定，再逐步细化

因此本版先定义公式框架，不把所有参数一次锁死。

### 16.2 统一结算结构

建议每个 Agent 在每个交易日形成一条结算记录：

```text
daily_score_delta
  = result_score_delta
  + learning_score_delta
  + governance_score_delta
```

其中：

- `result_score_delta`: 基于候选判断和次日收盘验证的结果分
- `learning_score_delta`: 基于被采纳且验证有效的学习改进
- `governance_score_delta`: 基于流程质量、异常行为、无效阻断等治理项

### 16.3 `research` / `strategy` 结果分

对 `research` 和 `strategy`，建议按单股票逐只结算，再汇总为日分。

单股票建议公式：

```text
symbol_result_score
  = stance_weight
  * outcome_weight
  * confidence_weight
  * bucket_weight
```

建议解释：

- `stance_weight`
  - `support = +1`
  - `oppose = -1`
  - `watch = +/-0.5`

- `outcome_weight`
  - 次日收盘验证正确：正值
  - 次日收盘验证错误：负值
  - 波动不明显：接近 0

- `confidence_weight`
  - `high > medium > low`

- `bucket_weight`
  - 最终 `1-3` 只权重大于普通 `10-20` 候选

### 16.4 `risk` 结果分

`risk` 不适合只按涨跌评分，应采用“执行风险是否被正确控制”的结果分。

建议拆成三类：

- 正确 `REJECT` 高风险或不合规标的：加分
- 错误 `ALLOW` 高风险标的：重扣
- 正确 `LIMIT` paper / 仓位 / 模式异常：加分

建议核心判据：

- 是否避免了错误执行
- 是否识别出 `paper`、账户异常、板块超限、单票超限
- 是否给出可执行、可落地的限制条件

### 16.5 `audit` 结果分

`audit` 的结果分应基于“过程监督是否有效”，而不是次日涨跌本身。

建议加分项：

- 正确发现证据断链
- 正确指出关键逻辑矛盾
- 正确拦下未充分讨论的高争议票

建议扣分项：

- 放过明显流程缺陷
- 反复触发无意义 `HOLD`
- 在证据充分时频繁过度阻断

### 16.6 学习分

学习分建议单独结算，不与结果分混淆。

建议公式：

```text
learning_score_delta
  = adopted_weight
  * verified_effect_weight
```

其中：

- `adopted_weight`
  - 改进建议是否被流程采纳

- `verified_effect_weight`
  - 采纳后是否确实改善了判断质量

### 16.7 治理分

治理分用于约束不良协作行为。

建议扣分项：

- 无依据强结论
- 重复输出空洞理由
- 不回应第二轮关键质疑
- 大量复制旧理由、不形成新证据
- 低分状态下仍反复无证据强判断

### 16.8 学分更新

每日收盘结算后：

```text
new_score = clamp(0, 20, old_score + daily_score_delta)
```

其中：

- `clamp` 保证学分不低于 `0`，不高于 `20`

### 16.9 权重使用位置

学分映射出的权重建议只作用于以下场景：

- `ashare` 在争议票收敛时的综合加权
- 多 Agent 对同一股票意见冲突时的优先级排序
- 低分 Agent 是否需要更强证据才能保留 `selected` 倾向

不建议直接作用于：

- 原始运行分数
- 执行成交结果
- 原始市场数据

## 17. 策略与因子体系

### 17.1 目标

多 Agent 讨论不能脱离策略和因子，否则只能停留在“会说理由”，无法形成稳定选股逻辑。

因此需要给 `strategy` 一套明确可解释的策略与因子框架，供 `research`、`risk`、`audit` 围绕同一逻辑进行讨论。

### 17.2 建议的主策略层

本版建议先保留 5 个主策略层：

- `momentum`
  - 强调趋势、量价、强者恒强
- `reversion`
  - 强调回撤修复、偏离回归
- `breakout`
  - 强调形态突破、放量确认
- `event-driven`
  - 强调公告、新闻、主题催化
- `sector-theme-rotation`
  - 强调板块轮动、主题轮动、资金切换与阶段性主线

### 17.3 策略职责分工

- `strategy`
  - 决定当前股票更匹配哪类主策略
- `research`
  - 为 `event-driven`、`sector-theme-rotation` 和基本面逻辑提供证据
- `risk`
  - 检查该策略在当前市场阶段是否放大风险
- `audit`
  - 检查“策略归因”和“证据归因”是否一致

### 17.4 因子层建议

本版建议先按 6 类因子组织：

- `momentum_factors`
  - 涨幅、趋势延续、量价共振
- `liquidity_factors`
  - 成交额、换手、成交稳定性
- `quality_factors`
  - 基本面稳健度、盈利质量、公告质量
- `event_factors`
  - 新闻、公告、主题催化、情绪强度
- `rotation_factors`
  - 板块强弱切换、主题热度、资金迁移、相对强度扩散
- `risk_factors`
  - 波动、回撤、集中度、板块暴露

### 17.5 因子到讨论的映射

建议映射如下：

- `research`
  - 重点解释 `quality_factors`、`event_factors`、`rotation_factors`
- `strategy`
  - 重点解释 `momentum_factors`、`liquidity_factors`、`rotation_factors`
- `risk`
  - 重点解释 `risk_factors`
- `audit`
  - 检查因子解释是否和最终结论一致

### 17.5.1 板块 / 主题轮动的讨论要求

若某只股票的核心逻辑来自板块或主题轮动，则讨论中至少要说明：

- 当前主线板块或主题是什么
- 该股票在板块中的位置：龙头 / 跟随 / 补涨 / 观察
- 板块热度是增强、分化还是退潮
- 该轮动逻辑是否有新闻、资金、量价或公告支持

不能只写“题材强”或“板块热”这种空泛表述。

### 17.5.2 市场环境分层

为了让策略切换有统一口径，本版建议先定义 4 类市场环境：

- `trend_market`
  - 趋势明确、主线清晰、强势股延续性较好
- `rotation_market`
  - 主线快速切换、板块轮动频繁、强弱切换明显
- `range_market`
  - 缺乏明确主线、震荡为主、持续性较弱
- `risk_off_market`
  - 风险偏好下降、回撤扩大、执行风险明显上升

### 17.5.2.1 市场环境判定指标

本版先定义一套可落地的判定指标框架，后续实现时可映射到具体接口和因子。

建议至少观测以下维度：

- 主线延续性
  - 强势票次日延续比例
  - 前一日领涨股隔日表现
- 板块轮动速度
  - 近 3 个交易日主导板块切换频率
- 风险偏好
  - 高波动票与高换手票的承接强弱
- 回撤压力
  - 候选池平均回撤、盘中炸板/跳水频率
- 宽度与扩散
  - 强势股是单点集中还是板块扩散

### 17.5.2.2 市场环境默认参数

本版以下数值均为默认参数，不是永久写死规则。

后续应统一纳入动态参数层，允许按市场环境、复盘结果和自然语言调优进行调整。

- `trend_market`
  - 强势股次日延续比例默认参考 `>= 55%`
  - 主线板块连续 `2-3` 日占优
  - 候选池平均回撤默认参考 `<= 3%`
  - 炸板或冲高回落占比默认参考不高于候选池的 `25%`

- `rotation_market`
  - 近 `3` 个交易日主导板块切换默认参考 `>= 2` 次
  - 强势股次日延续比例默认参考在 `35% - 55%`
  - 新热点接力速度快，但单一主线持续性不足
  - 板块间相对强度切换明显

- `range_market`
  - 强势股次日延续比例默认参考 `< 35%`
  - 候选池轮动弱、持续性差
  - 盘中冲高回落频繁，候选池平均回撤默认参考在 `3% - 4.5%`
  - 无单一板块连续 `2` 日稳定占优

- `risk_off_market`
  - 候选池平均回撤默认参考 `>= 4.5%`
  - 高波动标的承接明显变差
  - 炸板、跳水、尾盘失稳比例显著升高
  - 执行风险和尾盘不确定性上升
  - `risk` 或执行链路已出现明显风险提示

### 17.5.3 策略适用环境

建议按市场环境优先匹配策略：

- `trend_market`
  - 优先：`momentum`、`breakout`
- `rotation_market`
  - 优先：`sector-theme-rotation`、`event-driven`
- `range_market`
  - 优先：`reversion`
- `risk_off_market`
  - 优先降级整体激进策略权重，保守处理或不进入执行

### 17.5.4 策略切换条件

本版先定义定性切换规则：

- 当主线延续性增强、龙头与跟随共振明显时：
  - 从 `reversion` 向 `momentum / breakout` 倾斜

- 当热点快速切换、板块强度轮动加快时：
  - 从单纯 `momentum` 向 `sector-theme-rotation` 倾斜

- 当市场缺乏延续、强势股频繁回落时：
  - 降低 `momentum / breakout` 权重，提升 `reversion`

- 当系统进入明显 `risk_off_market`：
  - `risk` 有权要求整体降级，不以策略偏好强行保留执行倾向

### 17.5.5 切换权责

策略切换不应由单一角色独断。

建议职责如下：

- `runtime`
  - 提供市场阶段和候选结构变化的底层数据
- `strategy`
  - 提出主策略切换建议
- `research`
  - 解释是否有事件、主题或基本面支撑
- `risk`
  - 判断切换后是否放大组合风险
- `audit`
  - 检查切换逻辑是否自洽

### 17.6 用户展示建议

因子不必全部原始暴露给用户，但应至少展示：

- 主策略归属
- 关键因子摘要
- 最重要的 2-4 个支持因子
- 最重要的 1-3 个风险因子

## 18. 盘中日内 T 状态机

### 18.1 定位

日内 T 不应直接复用隔日选股流程，而应作为单独状态机处理。

原因：

- 时间尺度不同
- 风险暴露更快
- 盘中信息噪声更大
- 执行与风控的实时性要求更高

### 18.1.1 适用范围

本版确认：

- 日内 T 不只面向最终 `1-3` 只执行 shortlist
- 主程序筛出的 `10-20` 只候选都可以进入盘中 T 观察范围

但“可观察”不等于“可立即执行”，仍需经过盘中结构判断和即时风控门控。

### 18.2 建议状态机

```text
intraday_candidate
  -> observing
  -> t_ready
  -> t_executing
  -> t_closed
  -> archived
```

附加阻断状态：

```text
intraday_blocked
  -> risk_blocked
  -> liquidity_blocked
  -> execution_blocked
```

### 18.3 盘中 T 的角色分工

- `runtime`
  - 提供盘中快照、实时信号、基础异动和候选池状态刷新
- `strategy`
  - 判断是否出现可做 T 的结构
- `risk`
  - 判断是否满足日内仓位、回撤、成交、价格区间约束
- `audit`
  - 主要负责留痕，不宜过度干预盘中节奏
- `executor`
  - 只处理已通过盘中风控的 T 意图

### 18.3.1 盘中 T 限流原则

由于 `10-20` 只候选都可进入盘中 T 观察范围，本版建议增加限流机制：

- 所有候选都可进入 `observing`
- 只有触发盘中信号的个股才进入 `t_ready` 评估
- 同一时刻进入高优先级盘中复议的股票数量应受限
- 若盘中信号过多，优先处理：
  - 原本 `selected` 的票
  - 量价结构最明确的票
  - 风险暴露变化最大的票

### 18.4 日内 T 与隔日选股的区别

- 隔日选股：
  - 更强调候选质量与次日验证
- 日内 T：
  - 更强调盘中波动结构、成交承接和即时风控

### 18.4.1 日内 T 与候选池关系

建议将盘中候选分为两层：

- `base_candidates`
  - 即主程序筛出的 `10-20` 只候选
- `intraday_focus_list`
  - 在盘中因异动、结构或轮动强化而进入重点观察的子集

这样既保留全量候选盘中机会，也避免所有候选同时进入高强度复议。

### 18.5 学分口径

本版建议：

- 日内 T 与普通候选池分开结算
- 不直接混入隔日选股学分
- 后续可单独形成 `intraday_score`

### 18.5.1 日内 T 触发门槛

虽然全部候选都可进入盘中 T 观察，但真正进入盘中 T 执行准备的票，建议至少满足：

- 盘中结构明确，不是随机噪声
- 成交承接和流动性满足要求
- `risk` 未触发盘中阻断
- 执行链路健康

### 18.5.1.1 日内 T 最低流动性门槛

本版建议至少设置以下最低门槛草案：

- 当前成交额默认参考不低于该标的近 `5` 个交易日同时间段均值的 `1.2x`
- 当前换手强度不低于候选池中位水平
- 盘口承接评分不低于候选池平均水平
- 若盘口连续空档、滑点风险明显放大，则不进入 `t_ready`

后续实现时建议映射到：

- 最低成交额
- 最低换手
- 最低盘口承接评分

### 18.5.1.2 日内 T 最低波动门槛

盘中 T 需要“有波动但不失控”。

建议草案：

- 波动过小：
  - 日内振幅默认参考 `< 1.5%`，不足以覆盖交易成本和执行意义，不做 T
- 波动过大：
  - 日内振幅默认参考 `> 8%`，容易进入情绪噪声和失控区间，不做 T

因此盘中 T 更适合：

- 结构清晰
- 振幅大致处于默认参考 `1.5% - 8%`
- 成交承接稳定

的候选。

### 18.5.2 日内 T 开仓触发条件

本版建议把盘中 T 的开仓触发收紧为“结构 + 流动性 + 风控”三类条件同时成立。

建议条件：

- 结构条件
  - 出现明确突破、回踩承接、分时转强或二次放量确认
- 流动性条件
  - 成交额、换手、盘口承接满足最低要求
- 风控条件
  - `risk` 未触发盘中阻断
  - 当前仓位、单票限制、行业暴露允许

若三类条件中任一不满足，不进入盘中 T 执行准备。

### 18.5.3 日内 T 减仓条件

建议出现以下任一情况时，优先考虑减仓而不是继续加仓：

- 原始上涨结构被破坏
- 放量后承接不足
- 板块轮动明显转弱
- 风险暴露快速上升
- 执行端或账户状态出现异常

### 18.5.4 日内 T 退出条件

建议盘中 T 的退出条件至少包括：

- 达到预设目标后分批退出
- 触发止损或结构失效
- 盘中风险等级提升至不可接受
- 盯盘复议结果变为 `block`
- 接近尾盘且不满足继续持有条件

### 18.5.4.1 止盈止损草案

本版先采用相对保守的百分比草案：

- 止盈
  - 浮盈达到默认参考 `+2.5%` 时，允许首次分批兑现
  - 浮盈达到默认参考 `+4.0%` 且结构开始走弱时，优先大幅兑现
  - 若板块和结构仍强，可保留小部分观察仓，但不宜恋战

- 止损
  - 浮亏达到默认参考 `-1.5%` 时，进入强制减仓或退出评估
  - 浮亏达到默认参考 `-2.0%` 或结构明显失效时，优先退出
  - 不允许因“可能会反弹”而无限拖延止损

### 18.5.4.2 尾盘退出规则

尾盘阶段建议比盘中更保守。

草案规则：

- 默认参考 `14:30` 之后原则上不再新开盘中 T 仓位
- 默认参考 `14:45` 之后若结构转弱，优先平掉日内 T 仓位
- 默认参考 `14:55` 前未满足隔夜条件的 T 仓位应基本完成退出
- 若尾盘承接不足、结构走弱、执行不确定性上升：
  - 优先平掉日内 T 仓位
- 若尾盘仍强、风险受控、允许隔夜持有：
  - 才允许转入隔夜持有逻辑

本版默认倾向：

- 日内 T 优先日内闭环
- 不默认把 T 仓位自动转为隔夜仓

### 18.5.5 日内 T 禁止条件

本版建议以下情形直接禁止进入盘中 T：

- `paper` / 执行模式不允许
- 流动性明显不足
- 风险端给出盘中 `REJECT`
- 候选缺乏可解释结构，只是随机噪声波动
- 同一标的盘中已触发过高频反复进出，接近失控

## 19. 盯盘与盘中复议机制

### 19.1 目标

盯盘机制用于在盘中发现“候选状态变化”，并决定是否触发二次复议。

它的作用不是重新跑完整个讨论流程，而是在必要时对重点票做快速重审。

本版同时确认：

- 盯盘范围覆盖全部 `10-20` 只候选
- 但盘中高强度复议只针对触发事件的子集

### 19.2 触发条件建议

建议盘中以下信号触发盯盘事件：

- 突发放量
- 快速拉升或快速跳水
- 涨停/炸板/跌停相关异动
- 公告、新闻、监管事件实时出现
- 执行链路或账户状态变化

### 19.3 盯盘事件结构

建议统一为：

```json
{
  "type": "monitor_event",
  "symbol": "600519.SH",
  "event_kind": "volume_spike|price_break|news_update|risk_change",
  "timestamp": "<ISO8601>",
  "snapshot": {},
  "trigger_level": "low|medium|high"
}
```

### 19.4 复议分级

建议不要所有盯盘事件都触发完整复议，采用三档：

- `low`
  - 只记录，不复议
- `medium`
  - 由 `strategy` 或 `risk` 单点复核
- `high`
  - 触发盘中二次复议

### 19.4.1 候选范围与优先级

由于全部候选都可触发盯盘事件，建议盘中按优先级处理：

1. 原始 `selected` 票
2. 轮动加强、结构强化的 `watchlist` 票
3. 原本被边缘保留但突然出现强异动的票

不建议对全部候选一视同仁并发处理。

### 19.5 盘中二次复议流程

高等级事件建议流程：

```text
monitor_event
  -> ashare-runtime 更新快照
  -> ashare-strategy 判断结构是否变化
  -> ashare-risk 判断是否需要降级/阻断
  -> ashare 汇总为 intraday_review
  -> 若仍可执行，再进入 executor
```

补充规则：

- 若该股票原本不在最终 `1-3` shortlist，但盘中复议后结构和风控条件显著改善，也允许其进入盘中 `t_ready` 路径。

### 19.5.1 盘中复议的决策边界

盘中复议的目标是快速重判，不是重新召开完整两轮讨论。

因此建议限制为：

- 只围绕触发事件的股票
- 只重判当前是否继续、降级、阻断或进入 `t_ready`
- 不在盘中扩展全量候选重新排序
- 不在盘中大幅改写前一日完整策略结论

### 19.6 二次复议结果

盘中复议建议只允许输出以下结果：

- `maintain`
  - 维持原结论
- `downgrade`
  - 从 `selected` 降为 `watchlist`
- `block`
  - 暂停执行或撤销执行准备
- `t_ready`
  - 满足日内 T 条件，进入日内 T 流程

### 19.7 审计要求

盘中复议也必须留痕，但不要求像首轮讨论那样完整展开。

建议至少保留：

- 触发事件
- 复议参与 Agent
- 最终处理动作
- 风控依据

## 20. Watchlist 容量与排序规则

### 20.1 容量目标

`watchlist` 不能无限膨胀，否则会让讨论和盯盘失去重点。

本版建议：

- `watchlist` 默认容量上限参考为 `5-8` 只

### 20.2 容量约束逻辑

若落入 `watchlist` 的股票过多，则优先保留：

- 争议大但潜力高的票
- 轮动逻辑增强中的票
- 盘中可能进入 `intraday_focus_list` 的票

优先剔除：

- 证据薄弱但又无明显增量催化的票
- 结构和风险都一般、且缺乏轮动弹性的票

### 20.3 排序规则

`watchlist` 建议按以下顺序综合排序：

1. 结构改善潜力
2. 轮动强化可能性
3. 研究增量催化
4. 风控可接受度

### 20.4 用户展示

用户侧应明确区分：

- `selected`
  - 当前优先执行或重点关注
- `watchlist`
  - 暂不执行，但值得继续观察
- `rejected`
  - 当前不建议继续投入注意力

## 21. 动态参数层设计

### 21.1 目标

为了满足“每天必须有股票池”和“参数不能写死”的原则，系统需要统一的动态参数层。

动态参数层的职责是：

- 保存系统默认参数
- 保存当前生效参数
- 保存参数可调整范围
- 保存最近一次调整原因和来源

### 21.2 参数分层

建议分成三层：

#### A. `system_defaults`

系统默认值，仅作为冷启动参考。

#### B. `market_regime_params`

根据当前市场环境动态加载的参数组。

例如：

- `t_min_amplitude`
- `t_max_amplitude`
- `risk_off_drawdown_threshold`
- `watchlist_capacity`
- `tail_open_cutoff`
- `tail_exit_cutoff`

#### C. `agent_adjusted_params`

由 Agent 基于自然语言调优或复盘结果提出，并经审议后临时或阶段性生效的参数。

### 21.3 参数对象建议

每个动态参数建议统一结构：

```json
{
  "param_key": "t_min_amplitude",
  "default_value": 0.015,
  "current_value": 0.018,
  "allowed_range": [0.01, 0.03],
  "scope": "intraday",
  "effective_from": "2026-04-04",
  "effective_to": null,
  "last_adjust_reason": "rotation_market 下振幅要求上调",
  "updated_by": "ashare-risk",
  "approved_by": "ashare-audit"
}
```

### 21.3.1 参数字段规范

建议正式字段如下：

- `param_key`
  - 参数唯一标识
- `scope`
  - `global / market / runtime / strategy / risk / intraday / execution`
- `default_value`
  - 系统默认值
- `current_value`
  - 当前生效值
- `allowed_range`
  - 可调整范围
- `value_type`
  - `number / percent / integer / enum / time`
- `effective_from`
  - 生效起点
- `effective_to`
  - 生效终点，可为空
- `effective_period`
  - `today_session / next_trading_day / until_revoked`
- `source_layer`
  - `system_defaults / market_regime_params / agent_adjusted_params`
- `last_adjust_reason`
  - 最近一次调整原因
- `updated_by`
  - 提案或写入来源
- `approved_by`
  - 审批来源
- `version`
  - 参数版本号

### 21.3.2 参数分类建议

建议至少分为以下几类：

- 股票池类
  - `base_pool_capacity`
  - `watchlist_capacity`
- 市场环境类
  - `trend_follow_through_ratio`
  - `rotation_switch_frequency`
  - `risk_off_drawdown_threshold`
- 策略类
  - `momentum_weight`
  - `reversion_weight`
  - `breakout_weight`
  - `event_driven_weight`
  - `sector_theme_rotation_weight`
- 盘中 T 类
  - `t_min_amplitude`
  - `t_max_amplitude`
  - `t_take_profit_1`
  - `t_take_profit_2`
  - `t_stop_loss_soft`
  - `t_stop_loss_hard`
  - `tail_open_cutoff`
  - `tail_exit_cutoff`
- 风控类
  - `max_total_position`
  - `max_single_position`
  - `daily_loss_limit`
  - `sector_exposure_limit`

### 21.4 动态参数与股票池关系

动态参数层可以调整：

- `base_pool` 容量
- `focus_pool` 收敛规则
- `watchlist` 容量
- 盘中 T 触发门槛
- 止盈止损草案参数

但不能破坏以下硬约束：

- 每日必须生成 `base_pool`
- 最终 `execution_pool` 仍只取 `1-3` 只
- `risk` 否决权保留
- `audit` 留痕要求保留

### 21.5 参数优先级与覆盖顺序

同一参数在多层同时存在时，建议采用以下覆盖顺序：

```text
agent_adjusted_params
  > market_regime_params
  > system_defaults
```

说明：

- `system_defaults`
  - 仅提供冷启动基线
- `market_regime_params`
  - 提供当前市场环境下的推荐值
- `agent_adjusted_params`
  - 仅在经审议批准后，覆盖前两层

### 21.6 参数回退规则

参数并非永久上调或下调。

建议回退规则：

- `today_session`
  - 会话结束自动失效，回退到下一层
- `next_trading_day`
  - 次一交易日结束后自动失效
- `until_revoked`
  - 保持有效，直到新的审批变更覆盖或显式撤销

### 21.7 冲突处理原则

若多个参数提案在同一周期冲突，建议按以下顺序处理：

1. `risk` 明确禁止项优先
2. `audit` 驳回项优先
3. 更短生效周期的盘中参数不得覆盖硬风控项
4. 同类参数只保留最新且已审批通过的一版

## 22. 自然语言参数调优机制

### 22.1 目标

允许用户或 Agent 通过自然语言提出参数调整建议，但不能绕过审议流程直接生效。

### 22.2 输入示例

例如：

- “今天轮动很快，降低 breakout 权重，提高主题轮动权重”
- “把 watchlist 从 8 只收缩到 5 只”
- “最近尾盘风险高，把日内 T 新开仓时间提前”
- “把 risk_off 条件调严格一点”

### 22.3 内部流程

建议流程：

1. 用户或 Agent 提出自然语言调整请求
2. `ashare` 解析为结构化参数提案
3. `strategy` 说明策略收益影响
4. `risk` 说明风险影响
5. `audit` 检查是否允许生效并留痕
6. 写入动态参数层
7. 在指定生效周期内启用

### 22.4 可调与不可调

允许动态调整的内容：

- 股票池容量
- 策略权重
- 因子权重
- 市场环境阈值
- 盘中 T 门槛
- 止盈止损参数
- 尾盘时间参数

不应被自然语言随意改动的内容：

- `main -> ashare -> 子代理` 主架构
- 2 轮讨论机制
- `risk` 否决权
- `audit` 审议留痕要求
- 每日必须生成股票池

### 22.5 生效周期

建议参数调整支持以下生效周期：

- `today_session`
- `next_trading_day`
- `until_revoked`

默认建议：

- 盘中参数优先 `today_session`
- 结构性参数优先 `next_trading_day`

## 23. 参数提案与审批权限矩阵

### 23.1 角色权限原则

参数调整必须区分：

- 谁可以提案
- 谁可以评估
- 谁可以批准
- 谁负责写入生效

### 23.2 权限矩阵

| 参数类型 | 可提案 | 必评估 | 必审批 | 写入执行 |
|------|------|------|------|---------|
| 股票池容量 | `ashare` / 用户 | `strategy` + `risk` | `audit` | `ashare` |
| 市场环境阈值 | `strategy` / `risk` | `strategy` + `risk` | `audit` | `ashare` |
| 策略权重 | `strategy` | `risk` | `audit` | `ashare` |
| 因子权重 | `strategy` / `research` | `risk` | `audit` | `ashare` |
| 盘中 T 门槛 | `strategy` / `risk` | `risk` | `audit` | `ashare` |
| 止盈止损参数 | `strategy` / `risk` | `risk` | `audit` | `ashare` |
| 尾盘时间参数 | `risk` | `strategy` + `risk` | `audit` | `ashare` |
| 硬风控上限 | `risk` | `risk` | `audit` | `ashare` |

### 23.3 用户输入权限

用户可以通过自然语言提出调整建议，但默认不直接获得写入权。

用户输入应被视为：

- 参数变更请求
- 而不是已批准的参数变更

### 23.4 `ashare` 的角色

`ashare` 在参数体系中的角色是：

- 统一接收自然语言请求
- 生成结构化参数提案
- 组织评估链路
- 在审批通过后写入生效

`ashare` 不应绕过 `risk` 与 `audit` 直接改写关键参数。

## 24. 参数变更审计事件

### 24.1 最小审计要求

每一次参数变更都必须形成独立审计事件。

最小要求：

- 参数名
- 调整前值
- 调整后值
- 调整范围是否合法
- 提案人
- 评估人
- 审批人
- 生效周期
- 调整原因
- 调整时间

### 24.2 审计事件结构建议

```json
{
  "event_type": "param_change",
  "param_key": "watchlist_capacity",
  "old_value": 8,
  "new_value": 5,
  "allowed_range": [3, 12],
  "proposed_by": "user",
  "evaluated_by": ["ashare-strategy", "ashare-risk"],
  "approved_by": "ashare-audit",
  "written_by": "ashare",
  "effective_period": "next_trading_day",
  "reason": "近期轮动加快，收缩 watchlist 提高聚焦度",
  "timestamp": "2026-04-04T15:05:00+08:00"
}
```

### 24.3 必须拒绝的变更

以下类型的变更建议直接拒绝：

- 越过 `allowed_range`
- 绕过 `risk` 或 `audit`
- 试图修改硬架构原则
- 试图取消每日股票池生成
- 试图取消关键留痕

## 25. 参数生效顺序与运行时读取

### 25.1 运行时读取顺序

建议运行时每次读取参数时遵循：

1. 先读 `agent_adjusted_params`
2. 若无，再读 `market_regime_params`
3. 若无，再回落 `system_defaults`

### 25.2 运行时缓存建议

为了避免盘中频繁抖动，建议：

- 盘中参数按短周期缓存
- 结构性参数按会话或交易日缓存
- 一次审批通过后，不应在毫秒级被频繁覆盖

### 25.3 参数变更广播

参数变更生效后，建议至少广播给：

- `ashare`
- `ashare-strategy`
- `ashare-risk`
- `ashare-runtime`
- `ashare-audit`

必要时再由 `ashare` 控制是否影响 `executor`

## 26. 参数注册表草案

### 26.1 目标

为了让动态参数层可直接落地为配置表或数据库表，本版补充一份最小可用的参数注册表草案。

目标是回答：

- 当前系统至少需要哪些参数
- 这些参数属于哪个 scope
- 默认值、动态范围、审批角色分别是什么

### 26.2 核心参数注册表

| param_key | scope | default_value | allowed_range | effective_period 默认建议 | 提案主角色 | 审批角色 |
|------|------|------|------|------|------|------|
| `base_pool_capacity` | `runtime` | `20` | `10-50` | `next_trading_day` | `ashare` | `ashare-audit` |
| `focus_pool_capacity` | `strategy` | `10` | `5-20` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `watchlist_capacity` | `strategy` | `8` | `3-12` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `execution_pool_capacity` | `execution` | `3` | `1-3` | `until_revoked` | `ashare-risk` | `ashare-audit` |
| `trend_follow_through_ratio` | `market` | `0.55` | `0.40-0.75` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `rotation_switch_frequency` | `market` | `2` | `1-5` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `risk_off_drawdown_threshold` | `risk` | `0.045` | `0.02-0.08` | `next_trading_day` | `ashare-risk` | `ashare-audit` |
| `momentum_weight` | `strategy` | `0.25` | `0.00-0.60` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `reversion_weight` | `strategy` | `0.20` | `0.00-0.60` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `breakout_weight` | `strategy` | `0.20` | `0.00-0.60` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `event_driven_weight` | `strategy` | `0.15` | `0.00-0.50` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `sector_theme_rotation_weight` | `strategy` | `0.20` | `0.00-0.60` | `next_trading_day` | `ashare-strategy` | `ashare-audit` |
| `t_min_amplitude` | `intraday` | `0.015` | `0.005-0.04` | `today_session` | `ashare-strategy` | `ashare-audit` |
| `t_max_amplitude` | `intraday` | `0.08` | `0.04-0.15` | `today_session` | `ashare-risk` | `ashare-audit` |
| `t_take_profit_1` | `intraday` | `0.025` | `0.01-0.05` | `today_session` | `ashare-strategy` | `ashare-audit` |
| `t_take_profit_2` | `intraday` | `0.04` | `0.02-0.08` | `today_session` | `ashare-strategy` | `ashare-audit` |
| `t_stop_loss_soft` | `intraday` | `-0.015` | `-0.05--0.005` | `today_session` | `ashare-risk` | `ashare-audit` |
| `t_stop_loss_hard` | `intraday` | `-0.02` | `-0.08--0.01` | `today_session` | `ashare-risk` | `ashare-audit` |
| `tail_open_cutoff` | `intraday` | `14:30` | `14:00-14:50` | `today_session` | `ashare-risk` | `ashare-audit` |
| `tail_exit_cutoff` | `intraday` | `14:55` | `14:30-14:59` | `today_session` | `ashare-risk` | `ashare-audit` |
| `max_total_position` | `risk` | `0.80` | `0.20-1.00` | `until_revoked` | `ashare-risk` | `ashare-audit` |
| `max_single_position` | `risk` | `0.25` | `0.05-0.40` | `until_revoked` | `ashare-risk` | `ashare-audit` |
| `daily_loss_limit` | `risk` | `0.05` | `0.01-0.10` | `until_revoked` | `ashare-risk` | `ashare-audit` |
| `sector_exposure_limit` | `risk` | `0.40` | `0.10-0.60` | `until_revoked` | `ashare-risk` | `ashare-audit` |

### 26.3 说明

- `execution_pool_capacity` 的上限固定为 `3`
- 盘中参数默认使用 `today_session`
- 结构性参数默认使用 `next_trading_day`
- 硬风控参数默认使用 `until_revoked`

## 27. 总状态机

### 27.1 目标

本节把前文三个关键链路统一为一张生命周期总状态机：

- 股票池生成链
- 多 Agent 讨论链
- 参数调优链
- 学分结算链

### 27.2 日级总流程

```text
trading_day_start
  -> build_base_pool
  -> round_1_discussion
  -> round_1_summary
  -> round_2_discussion
  -> final_selection
  -> intraday_monitoring
  -> intraday_recheck / intraday_t
  -> trading_day_close
  -> score_settlement
  -> learning_update
  -> next_day_prep
```

### 27.3 详细状态机

```text
[A] Pool Lifecycle
day_open
  -> base_pool_ready
  -> focus_pool_ready
  -> execution_pool_ready

[B] Discussion Lifecycle
execution_pool_ready
  -> round_1_running
  -> round_1_summarized
  -> round_2_running
  -> final_review_ready
  -> final_selection_ready

[C] Parameter Lifecycle
param_request_received
  -> param_proposal_structured
  -> param_evaluating
  -> param_approved / param_rejected
  -> param_effective
  -> param_expired / param_revoked

[D] Intraday Lifecycle
market_open
  -> observing
  -> monitor_event_triggered
  -> intraday_review
  -> t_ready
  -> t_executing
  -> t_closed / intraday_blocked

[E] Score Lifecycle
market_close
  -> next_day_validation_pending
  -> next_day_close_verified
  -> score_delta_calculated
  -> score_updated
  -> learning_mode_check
  -> suspended / recovered / normal
```

### 27.4 跨链路触发关系

建议明确以下跨链路触发：

- `final_selection_ready`
  - 触发 `intraday_monitoring`
- `param_effective`
  - 影响后续 `focus_pool_ready`、`execution_pool_ready`、`t_ready`
- `score_updated`
  - 影响下一交易日多 Agent 收敛权重
- `suspended`
  - 影响对应 Agent 是否保留关键表决权

### 27.5 异常状态

建议统一保留以下异常状态：

- `data_unavailable`
- `risk_blocked`
- `audit_hold`
- `execution_unavailable`
- `param_rejected`
- `agent_suspended`

### 27.6 实现建议

若后续转程序实现，建议至少拆成以下四类状态表：

- `pool_state`
- `discussion_state`
- `param_state`
- `agent_score_state`

## 28. 待确认问题

以下问题留待审议后最终定稿：

1. 市场环境判定阈值是否还要继续细化到按接口字段直接落库
2. `audit HOLD` 的触发门槛是否只限“严重证据断裂”
3. 最终 `1-3` 只是否允许全部来自同一行业
4. 用户端是否展示完整讨论轮次，还是只展示压缩后的结论版
5. 是否引入滚动衰减
6. `risk`、`audit` 的记分权重是否独立于 `research`、`strategy`
7. 日内 T 的止盈止损百分比是否需要按不同策略分别配置
