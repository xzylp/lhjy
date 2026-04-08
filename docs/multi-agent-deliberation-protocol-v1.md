# 多 Agent 讨论协议 v1

> 更新时间: 2026-04-06
> 目标: 让 `ashare` 体系从“多角色表态”升级为“多视角推理、互相质疑、补证修正、最后收敛”的真实讨论流程。

## 1. 设计目标

当前系统已经具备：

- 候选池落库
- 两轮讨论状态机
- `research / strategy / risk / audit` 四角色
- `reply-pack / final-brief / client-brief`
- 统一 `agent-packets / workspace_context / discussion_context`

但当前流程仍偏向：

- 各角色快速提交 opinion
- 主持人收集后直接刷新
- 两轮更像“二次表态”，不是“二次推理”

本协议要解决的问题：

1. 让每个 Agent 先独立形成判断，而不是复读已有结论。
2. 让第二轮围绕争议点展开，而不是全量重评。
3. 让 Agent 必须回应别人的质疑，并允许修正自己。
4. 让用户能看到入选、落选、争议与证据，而不是只看最终票。
5. 让次日结算和积分真正能回溯到“谁判断得好，谁判断得差”。

## 2. 核心原则

### 2.1 独立判断优先

Round 1 中，`research / strategy / risk / audit` 必须基于统一 packet 独立生成判断，不先依赖其他角色结论。

### 2.2 争议驱动而非重复表态

Round 2 只讨论争议 case，不做全量重评。争议来源包括：

- 立场冲突
- 风控 `limit / reject`
- 审计 `question / limit / reject`
- 关键证据缺口
- 候选排序理由不充分

### 2.3 回应必须显式

第二轮不允许简单复制第一轮观点。必须说明：

- 回应了谁
- 回应了什么问题
- 是否改判
- 改判依据是什么

### 2.4 补证优先于停摆

当 packet 证据不足时，Agent 不应只说“数据不足”，而应：

1. 说明缺什么
2. 补读内部 serving / runtime / research / monitor 数据
3. 必要时使用允许的外部工具补检事实源
4. 再输出修正后的判断

### 2.5 收敛看论证质量，不看简单票数

最终不是“4 票里 3 票支持就通过”，而是综合：

- 证据完整度
- 推理一致性
- 对反对意见的回应质量
- 风控可执行性
- 审计证据链完整度

## 3. 角色定位

### 3.1 `ashare`

定位：主持人和收敛器，不直接替代专业判断。

职责：

- 发起讨论
- 分发统一上下文
- 汇总第一轮争议点
- 指定第二轮问题
- 收敛为最终推荐或阻断

### 3.2 `ashare-research`

定位：事件和逻辑催化分析。

重点：

- 新闻、公告、政策、行业催化
- 情绪与主题一致性
- 公司与板块叙事是否成立

### 3.3 `ashare-strategy`

定位：排序与胜出逻辑。

重点：

- 候选股相对强弱
- 排名依据
- 与其他候选相比为什么更优或更差

### 3.4 `ashare-risk`

定位：执行前约束和风险条件。

重点：

- 波动、流动性、仓位、执行可行性
- 是否允许进入执行池
- 如果限制，解除条件是什么

### 3.5 `ashare-audit`

定位：讨论质量与证据链复核，不负责最终排名。

重点：

- 有没有证据断层
- 有没有关键质疑未回应
- 有没有结论和证据不一致

## 4. 讨论阶段

## 4.1 Phase 0: 候选准备

输入：

- runtime 候选池，建议 10-20 只，后续可放大至 30 只
- `workspace_context`
- `discussion_context`
- `agent-packets`

输出：

- `base_pool`
- `focus_pool`
- `discussion_seed`

主持人要求：

- 如果 dossier 已 fresh，优先复用
- 若 packet 缺核心证据，先做补证，不直接开会

## 4.2 Phase 1: Round 1 独立分析

特点：

- 四个角色并行独立输出
- 不要求先阅读其他人观点

每个 Agent 至少输出：

- 自己支持或反对的核心 thesis
- 2 条以上关键证据
- 至少 1 条证据缺口或不确定项
- 对该票的当前 stance

Round 1 目标：

- 形成每只股票的支持点、反对点、证据缺口
- 不要求此轮完全收敛

## 4.3 Phase 2: 冲突提炼

由 `ashare` 主持人执行。

对每只 case 提炼：

- `support_points`
- `oppose_points`
- `risk_constraints`
- `audit_questions`
- `evidence_gaps`
- `needs_round_2`

进入 Round 2 的条件：

- stance 冲突
- `risk in {limit, reject}`
- `audit in {question, limit, reject}`
- top 候选缺核心证据
- 策略说明不足以解释其相对胜出

## 4.4 Phase 3: Round 2 争议讨论

特点：

- 只针对争议 case
- 每个 Agent 必须阅读其他角色 Round 1 的相关观点

第二轮必须回答：

1. 你回应的是谁的质疑？
2. 你是否接受了对方的部分观点？
3. 你是否补到了新证据？
4. 你的 stance 是否变化？
5. 剩余分歧是什么？

禁止：

- 原样重复第一轮 reasons
- 不引用任何其他角色就直接重新下结论

## 4.5 Phase 4: Finalize 收敛

由 `ashare` 汇总，`audit` 只做讨论质量复核，不拥有绝对排名权。

收敛输出包括：

- `selected`
- `watchlist`
- `rejected`
- `key_disputes`
- `why_selected`

## 5. 委派模板落点

为避免主持人 `ashare` 在运行时把任务写成“泛泛要求 JSON 数组”，正式委派模板统一维护在：

- [openclaw-subagent-delegation-templates.md](openclaw-subagent-delegation-templates.md)

使用要求：

- Round 1 任务用该文档中的并行模板。
- Round 2 任务必须带上 `controversy_summary_lines / round_2_guidance / substantive_gap_case_ids`。
- 若 `finalize` 返回 `discussion_not_ready`，优先回看该文档中的“主持人自检清单”，而不是把问题归因到网关或接口故障。
- `why_not_selected`
- `execution_candidates`

## 5. Opinion 结构升级

当前 opinion 结构保留：

- `case_id`
- `round`
- `agent_id`
- `stance`
- `confidence`
- `reasons`
- `evidence_refs`

建议新增扩展字段：

```json
{
  "case_id": "case-20260406-600519-SH",
  "round": 2,
  "agent_id": "ashare-strategy",
  "stance": "support",
  "confidence": "high",
  "reasons": ["相对强弱仍领先", "研究补充的催化与趋势一致"],
  "evidence_refs": ["packet:dossier:600519.SH", "event:announcement:123"],
  "thesis": "该票在争议组中仍是最优胜出者",
  "key_evidence": ["排名前2", "近3日相对基准强于候选池均值"],
  "evidence_gaps": ["尚缺盘中承接确认"],
  "questions_to_others": ["risk 是否接受在仓位减半条件下放行"],
  "challenged_by": ["ashare-risk", "ashare-audit"],
  "challenged_points": ["波动偏大", "证据链不足"],
  "previous_stance": "watch",
  "changed": true,
  "changed_because": ["研究已补到公告催化", "审计缺口已部分关闭"],
  "resolved_questions": ["催化真实性已确认"],
  "remaining_disputes": ["仓位上限仍需限制"]
}
```

说明：

- 第一阶段至少要有 `thesis / key_evidence / evidence_gaps`
- 第二阶段至少要有 `challenged_by / changed / changed_because / resolved_questions / remaining_disputes`

## 6. 主持人输出结构

`ashare` 每轮不只汇总 opinion，还要生成中间结构：

```json
{
  "trade_date": "2026-04-06",
  "round": 1,
  "case_id": "case-20260406-600519-SH",
  "discussion_brief": {
    "support_points": [],
    "oppose_points": [],
    "risk_constraints": [],
    "audit_questions": [],
    "evidence_gaps": [],
    "needs_round_2": true
  }
}
```

Round 2 后生成：

```json
{
  "case_id": "case-20260406-600519-SH",
  "resolved_points": [],
  "unresolved_points": [],
  "persuasion_summary": [
    "research 补充公告催化后，说服了 strategy 从 watch 改为 support",
    "risk 接受减仓条件，但仍要求限额"
  ],
  "final_recommendation_reason": "在争议票中证据最完整且可控"
}
```

## 7. Audit 的权力边界

`ashare-audit` 参与两轮，但默认不应成为“最后拍板排名者”。

建议规则：

- `audit` 有权指出：
  - 证据不完整
  - 关键质疑未回应
  - 逻辑断裂
- `audit` 无权单独决定：
  - 哪只票一定排第 1
  - 哪只票一定胜过另一只票

也就是说：

- `audit` 负责讨论质量与可复核性
- `strategy` 负责排序
- `risk` 负责可执行性
- `research` 负责催化与逻辑
- `ashare` 负责主持收敛

## 8. 用户可见输出

客户端最终不只看“今天买什么”，而是至少看到：

### 8.1 候选池结果

- 候选总数
- 每只股票中文名 + 代码
- 入选 / 观察 / 淘汰分组

### 8.2 每只股票原因

对于每只候选至少展示：

- 入选或落选结论
- 核心支持理由
- 核心反对理由
- 风控状态
- 审计状态
- 关键数据支撑

### 8.3 争议与收敛

- 谁反对过
- 谁被说服了
- 第二轮解决了哪些问题
- 最终为什么它胜出

## 9. 次日反馈与积分

积分不是简单按表态次数给，而要回溯到判断质量。

基本规则：

- 每个 Agent 基础分 10 分
- 上限 20 分
- 下限 0 分
- 0 分时明确进入待淘汰或停用名单

结算视角：

- 次日收盘表现
- 是否支持了优胜票
- 是否错杀了优胜票
- 是否放行了明显差票
- 是否提出了有价值的风险警示

加分来源：

- 正确支持优胜票
- 正确反对劣质票
- 第二轮中通过补证修正错误判断
- 提出被采纳的高质量新策略或建议

减分来源：

- 无依据强结论
- 第二轮不回应质疑
- 重复复读第一轮
- 明显错误且拒绝修正

## 10. 第一版落地建议

先不推翻大框架，按最小增量实现：

### P1

- 扩展 opinion 模型，加入 `thesis / key_evidence / evidence_gaps / challenged_by / changed_because`
- `ashare` 在 Round 1 后自动提炼争议点

### P2

- Round 2 强制“引用他人观点并回应”
- 未回应质疑则该票不能进入 finalize ready

### P3

- `client_brief / reply-pack / final-brief` 增加：
  - `persuasion_summary`
  - `selected_vs_rejected_reasons`
  - `why_selected`
  - `why_not_selected`

### P4

- 将次日结算与积分系统绑定到扩展 opinion 结构
- 把“是否修正错误判断”纳入积分

## 11. 结论

这份协议的重点不是“让会议变慢”，而是：

- 让 Agent 必须独立判断
- 必须互相质疑
- 必须补证
- 必须允许修正
- 最终让收敛结论真正具备说服力与可解释性

只有这样，系统的优势才会从“多 Agent 轮流说话”变成“多 Agent 协作推理后产出更强结论”。
