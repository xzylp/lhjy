# 2026-04-21 正式改造方案：从讨论型控制面到可执行交易主链

## 1. 文档目的

这份文档不是再补一份任务清单，而是把当前系统的正式改造思路讲清楚：

1. 现在到底卡在哪里。
2. 为什么不能继续靠堆模块或堆 Agent 解决。
3. 设计上应该怎么重构主链。
4. 每一类改造的目标、理由、边界和验收标准是什么。

本文直接承接：

- [live_runtime_diagnosis_20260421.md](/srv/projects/ashare-system-v2/docs/live_runtime_diagnosis_20260421.md)

核心判断只有一句：

```text
当前系统不是“不会运行”，而是“运行了很多旁路动作，但缺少一条被服务端强约束的交易执行主链”。
```

---

## 2. 现状判断

结合 2026-04-21 盘中实测，当前系统已经具备以下能力：

- 市场数据、账户数据、QMT 桥接读链路在线。
- 候选生成、讨论状态机、执行预检、intent 生成已经存在。
- 持仓巡视、部分卖出快路径、做T信号生成器已经存在。
- Hermes 控制面、飞书机器人、监督面板已经能展示运行态。

但真正缺失的是“主链收口”：

```text
市场感知
-> 形成执行目标
-> 讨论收敛
-> 风控审查
-> 生成 intent
-> 自动派发
-> 网关回执
-> 成交复核
-> 归因学习
```

当前大量模块都在这个链条周边工作，但没有被强制编排成一条稳定闭环。

所以问题不在于：

- 模块太少
- 机器人太少
- 面板不够多

问题在于：

- 关键状态没有被服务端约束成必须推进的业务链
- 盘中没有持续推进未闭合主链
- 持仓、新开仓、换仓三种动作没有被拆成独立责任链

---

## 3. 改造总原则

本轮正式改造遵循五条原则。

### 3.1 服务端硬约束优先于 Prompt 自觉

凡是影响真实交易的关键纪律，不允许只停留在：

- 提示词建议
- Agent 自我约束
- 面板展示提醒

必须落入服务端主链。

适用范围包括：

- regime 对执行预算的影响
- 因子有效性对 compose 权重的影响
- 讨论结果对 dispatch 的触发
- 风控和审计结论对是否可执行的影响

理由：

- 实盘链路不能依赖 Agent “记得先看一眼”。
- 只读展示不等于真正接入执行。

### 3.2 业务链优先于模块堆叠

后续设计不再按“模块看起来都存在”验收，而按业务链是否闭合验收。

优先级从高到低：

1. 真实执行主链
2. 持仓管理主链
3. 盘中可观测主链
4. 归因与学习主链
5. 展示与问答体验

理由：

- 没有主链，模块越多越容易互相掩盖问题。

### 3.3 持续推进优于单点触发

盘中链路不能只在 `09:30` 触发一次，也不能只在 `finalize_cycle()` 时才允许派发。

必须改成：

- 阶段性收口
- 连续检查
- 超时推进
- 失败重试

理由：

- 超短交易窗口是连续竞争，不是单次击发。

### 3.4 风险分级优于一刀切冻结

保留 `regime == chaos` 这类弱市识别能力，但禁止继续使用：

- `chaos -> FREEZE_ALL`

必须改成：

- 冻结弱候选
- 降低预算
- 提高确认门槛
- 保留强势例外

理由：

- 弱市不是零机会，真正强票往往出现在分歧和混乱里。

### 3.5 链路可追责优于“看起来很忙”

后续所有主链都必须能回答：

- 谁提案
- 谁放行
- 谁阻断
- 卡在哪一步
- 为什么没继续推进
- 最终有没有成交
- 结果归因到谁

理由：

- 没有 trace，就没有可验证的自治。

---

## 4. 目标架构

目标架构不是“更多 Agent”，而是“更清晰的主链分层”。

### 4.1 四层结构

#### 第一层：感知层

负责形成结构化市场输入。

包括：

- 市场阶段与 `regime`
- 热点板块链
- 持仓异动
- 候选池变化
- 因子有效性快照
- Windows/QMT 执行链健康状态

要求：

- 感知层产物必须结构化，不得只存在于自然语言摘要。
- 每个关键字段都要带 `generated_at` 和 `freshness`。

#### 第二层：决策层

负责在约束内组织方案。

包括：

- Agent 市场假设
- playbook 选择
- factor 选择与加权
- 持仓动作判断
- 新开仓 / 做T / 换仓的优先级安排

要求：

- Agent 有自主空间，但自主必须发生在受约束的集合内。
- 不允许跳过感知层直接盲 compose。

#### 第三层：执行层

负责把“可讨论的结论”变成“可派发的动作”。

包括：

- execution pool
- precheck
- intent
- dispatch
- gateway receipt
- fill / cancel / reject

要求：

- 执行层必须成为独立主链，不再被动依赖讨论是否有人点 finalize。

#### 第四层：归因层

负责把结果回写到系统，不让 Elo、学习、评估成为孤岛。

包括：

- 成交后风险复核
- 策略归因
- Agent 贡献归因
- 因子表现归因
- 参数回顾

要求：

- 每日收盘后必须能生成结构化归因，不允许只留日志。

---

## 5. 三条核心业务主线

这是本次改造的核心。后续所有调度、面板、机器人、任务卡都围绕这三条链来设计。

### 5.1 新开仓主链

目标：

- 盘前形成预案
- 盘中发现机会
- 在时效内完成收敛和派发

标准链路：

```text
市场感知
-> 候选发现
-> discussion 收敛
-> execution precheck
-> intent
-> auto dispatch
-> gateway receipt
-> fill / reject
```

当前问题：

- `09:30` 开盘执行为空壳。
- `intent` 生成后不自动派发。
- `chaos` 时直接冻结全部候选。

改造目标：

- 开盘与盘中候选不再悬空。

### 5.2 持仓管理主链

目标：

- 对已有持仓做实时节奏管理，而不是只等尾盘总结。

动作类型明确拆开：

- `protect_sell`
- `high_sell`
- `low_buy_recover`
- `reduce_on_weakness`
- `hold_with_watch`

标准链路：

```text
持仓巡视
-> 动作判断
-> 风险复核
-> 执行动作
-> 回执
-> 持仓状态更新
```

当前问题：

- 卖出快路径能走。
- 做T 低吸和回补更多停留在信号与候选注入层。
- 没有稳定形成“判断 -> 放行 -> 派发 -> 回执”的闭环。

改造目标：

- 持仓管理成为一等公民，而不是开新仓链的附属品。

### 5.3 换仓替换主链

目标：

- 在仓位有限时，对“保留旧票还是切换到新票”形成独立判断。

标准链路：

```text
旧持仓质量评估
-> 新候选对比
-> 机会成本比较
-> replacement_review
-> 先出后进 / 分步替换
-> 回执与复核
```

当前问题：

- 设计里已有 `replacement_review`，但没有真正成为独立主链。
- 当前很多换仓判断混在持仓巡视、监督提示和 runtime advisory 中。

改造目标：

- 换仓逻辑从“附属建议”升级成“正式执行链”。

---

## 6. Agent 角色边界

后续不再把 Agent 定义成“自由发挥的全能操盘手”，而是定义成“在约束内组织最优方案的决策单元”。

### 6.1 Agent 应负责什么

- 理解当前市场状态
- 形成市场假设
- 在允许的 playbook/factor 集合中组织提案
- 提供比较理由、替代方案和主次优先级
- 对分歧结果给出解释

### 6.2 Agent 不应负责什么

- 绕过执行纪律直接下单
- 无视 regime 与风控边界自由选因子
- 把提示词当成执行约束的唯一来源
- 用“表述丰富”代替结构化结论

### 6.3 设计理由

- 真正可用的自治不是“无限自由”，而是“在清晰边界内稳定产出”。
- 用户关心的是实盘结果和链路可解释性，不是 Agent 的语言表现。

---

## 7. 关键设计改造

### 7.1 感知层强制注入 compose

当前问题：

- `/runtime/capabilities` 和因子有效性更多停留在展示层。
- Agent 可以看到，但程序不强制消费。

改造要求：

- compose 请求构建阶段，必须注入：
  - 最新 `regime`
  - 因子有效性快照
  - 持仓上下文
  - 热点板块链
  - 执行预算状态

执行语义：

- 不支持 monitor 的因子标记为“需事后归因”。
- 低有效性因子不再一刀切乘 `0.3`，改为连续降权。
- 若 Agent 选择的因子与当前市场状态严重不一致，必须返回警告或触发重编排。

设计理由：

- 不让 Agent 盲选。
- 不让无效因子在执行链里与有效因子同权竞争。

### 7.2 `chaos` 从全冻结改成分级风控

当前问题：

- `regime == chaos` 直接触发 `FREEZE_ALL`。

改造要求：

- `chaos` 只代表“进入高风险弱市”，不代表“所有交易行为一律停止”。

建议语义：

- `freeze_weak_candidates`
- `shrink_total_budget`
- `allow_only_verified_strength`
- `replacement_review_first`

设计理由：

- `chaos` 的判定本身合理，问题出在执行策略过粗。
- 实盘里弱市也有强势龙头、逆势票和持仓修复动作。

### 7.3 `finalize` 从唯一闸门改成阶段性收口

当前问题：

- 只要不进入 `finalize_cycle()`，intent 就可能永远不派发。

改造要求：

- 当满足以下条件时，允许自动进入派发态：
  - `round_summarized`
  - `approved_count > 0`
  - `session_open = true`
  - 无新增硬阻断

额外要求：

- 若策略/风控/审计超时，系统必须给出明确降级策略。
- 降级动作要可配置为：
  - 允许继续
  - 降低仓位继续
  - 阻断并报警

设计理由：

- 盘中系统不能无限等待完美讨论。

### 7.4 开盘执行与盘中执行改成连续窗口

当前问题：

- 只有名义上的 `09:30 开盘执行`，且实际为空壳。

改造要求：

- 新增连续推进窗口：
  - `09:25-09:30` 预装填
  - `09:30-10:30` 高频推进
  - `10:30-11:30` 常规推进
  - `13:00-14:30` 午后推进
  - `14:30-14:57` 尾盘收口

推进动作包括：

- 刷 execution pool
- 刷 precheck
- 对未派发 intent 自动补派发
- 对 gateway 未回执动作跟踪重试
- 对异常动作写明卡点

设计理由：

- 超短执行依赖连续窗口，不是一次性任务。

### 7.5 持仓做T与换仓正式纳入执行主链

当前问题：

- 设计已存在，但链路分散，未形成稳定闭环。

改造要求：

- 给 `day_trading` 和 `replacement_review` 建独立执行卡。
- 每个动作必须有：
  - 来源
  - 理由
  - 风控意见
  - 执行结果
  - 成交后复核

设计理由：

- 做T 和换仓不是附属能力，而是超短系统核心。

### 7.6 监督系统从“催办”升级成“强制主线推进器”

当前问题：

- supervision 能识别谁没产物，但不负责推动主链闭合。

改造要求：

- 若存在可执行机会但策略席位超时，自动生成推进任务。
- 若风险席位未给结构化结论，在时限内触发升级或默认策略。
- 若审计席位未完成，不得无边界阻塞交易主链。

设计理由：

- 监督的价值不在提醒，而在保障业务链继续向前。

### 7.7 Elo 与归因系统接入真实结果

当前问题：

- Elo 算法存在，但自动喂数不足。

改造要求：

- 在 discussion 终态、风控终态、审计终态和成交结果落地后，自动生成归因输入。
- 将 Agent 观点与最终结果、收益、风险偏差进行结构化对比。

设计理由：

- 不接结果的 Elo 只是孤立算法，不会改善系统。

---

## 8. 控制台与机器人改造方向

### 8.1 Hermes 控制台从接口聚合页改成业务作战台

面板核心不再展示“有哪些接口有值”，而展示：

- 当前市场状态
- 当前主线任务
- 新开仓链进度
- 持仓链进度
- 换仓链进度
- 每只票卡在哪一关
- 当前 dispatch/receipt/fill
- 哪个 Agent 超时
- 哪个动作是自动触发

设计理由：

- 用户要看到的是全流程，不是接口拼接。

### 8.2 飞书机器人回归“双模”

机器人需要同时支持：

1. 业务定向回答
2. 基础通用问答

设计原则：

- 命中交易/系统语义时，走控制面业务链。
- 未命中固定业务语义时，退回基础 Agent 能力。
- 不允许每条自由问答都强行封装成业务卡。

设计理由：

- 机器人是入口，不是只有剧本的 FAQ 机。

### 8.3 推送精简但关键节点必须保留

建议保留的主动推送：

- 主链进入执行态
- 风控阻断
- 派发失败
- 网关异常
- 成交回执
- 持仓异常波动触发的动作建议

建议取消的冗余推送：

- 普通入池通知
- 无动作价值的重复状态卡

设计理由：

- 重要事件推送，普通状态放面板。

---

## 9. 验收标准

本次改造验收，不按“代码改了多少”评估，只按下面几条业务标准评估。

### 9.1 主链闭合验收

必须至少满足：

- 新开仓链能在交易时段完成 `candidate -> precheck -> intent -> dispatch -> receipt`
- 持仓链能完成 `watch -> action -> dispatch -> receipt`
- 换仓链能完成 `replacement_review -> 出旧进新 -> 回执`

### 9.2 状态一致性验收

必须满足：

- `mainline_stage` 不再在盘中误判成 `postclose_learning`
- 每个面板关键状态都带 `generated_at`
- 候选、预检、intent、dispatch、receipt 状态能在一个视图内对齐

### 9.3 风控语义验收

必须满足：

- `chaos` 不再一刀切 `FREEZE_ALL`
- risk/audit 结论真正进入执行链
- regime 能影响预算、仓位、允许动作类型

### 9.4 Agent 自治验收

必须满足：

- Agent 不能跳过感知层盲 compose
- 因子与战法选择有服务端约束
- 每次 compose 都能回答“为什么选这些因子、为什么不用别的”

### 9.5 可追责验收

必须满足：

- 任意一只票都能追到：
  - 来源
  - 讨论
  - 放行
  - 派发
  - 回执
  - 成交
  - 归因

---

## 10. 实施优先级

### P0：必须先做

- 开盘执行空壳改成真实主链推进器
- `finalize` 唯一闸门改成阶段性收口
- `chaos -> FREEZE_ALL` 改为分级风控
- 持仓/做T/换仓正式纳入执行链
- `mainline_stage` 盘中误判修复

### P1：紧接着做

- 连续执行窗口调度
- 执行链统一追踪视图
- 监督系统升级为主线推进器
- 飞书机器人双模问答
- 控制台改成业务作战台
- 建立控制面数据库与索引层：
  - 引入 SQLite 主库
  - 建立 dataset catalog / partitions / ingestion runs
  - 建立文档与讨论的全文检索
- 建立本地历史底座第一阶段：
  - 先日线全量 + 日增量
  - 再分钟线近端窗口
  - 同步产出股性画像和摘要层

### P2：随后做

- Elo 自动喂数与归因闭环
- 因子有效性更精细的连续加权
- regime 分层回测与参数校准
- 更完整的跨市场、另类数据和成长质量补强
- Hermes 模型路由正式化：
  - 先把模型槽位从展示改成可执行策略
  - 再加岗位到模型的路由规则
  - 最后加“按风险升级模型”

---

## 10.1 新增专项任务编排

根据最新讨论，以下三条主线正式并入改造清单，不再作为可选增强项。

### A 线：数据库与索引层

目标：

- 建立正式的数据资产目录、结构化检索与事务型控制面存储，结束“主要靠散落 JSON 文件扩张”的状态。

技术基线：

- `SQLite`：控制面主数据库，负责索引、trace、任务 run、结构化状态与全文检索索引
- `Parquet + DuckDB`：历史行情与研究底座，负责日线、分钟线、因子值、行为样本等分析型数据
- `SQLite FTS5`：纪要、审计、讨论、文档全文检索
- `cache / serving`：仅保留为热缓存层，不再承担长期事实源角色

目录基线：

```text
storage_root/
  db/
    control_plane.sqlite3
  lake/
    bars_1d/
    bars_1m/
    bars_5m/
    factor_values/
    behavior_samples/
  state/
    runtime_state.json
    research_state.json
    discussion_state.json
    execution_gateway_state.json
  cache/
  serving/
  learning/
  reports/
```

设计边界：

- 当前阶段不引入 Kafka、Elasticsearch、ClickHouse、TimescaleDB 这类重基础设施
- 以单机正式可用、目录清晰、快速检索、低运维成本为优先目标

顺序任务：

1. 建立 `SQLite` 控制面主库。
2. 建立数据目录与分区登记表：
   - `dataset_catalog`
   - `dataset_partitions`
   - `ingestion_runs`
3. 建立控制面结构化表：
   - `execution_traces`
   - `discussion_traces`
   - `gateway_receipts`
   - `agent_actions`
4. 建立 `documents + FTS5` 全文检索。
5. 把后续历史底座、讨论归档、执行 trace 逐步挂到统一索引层。

交付标准：

- 程序可以不靠遍历目录就知道某类数据放在哪里
- 关键数据集都有 freshness / source / run_id / partition 记录
- 文档、纪要、审计、讨论可以全文检索
- 控制面结构化数据不再主要依赖 JSON 状态文件堆积

### B 线：本地历史底座

目标：

- 让程序与 Agent 都能稳定拿到历史上下文，而不是依赖临时现拉实时接口。

数据分层：

1. 原始层
   - `1d` 全历史
   - `1m/5m` 近端窗口
2. 特征层
   - 涨停触板率、封板率、炸板率、回封率、次日溢价、波动结构、板块跟随性等
3. 摘要层
   - 供 runtime / Hermes / Agent 直接消费的短摘要，不要求 Agent 直接吃长 K 线数组

重点数据集：

- `bars_1d`
- `bars_1m`
- `bars_5m`
- `factor_values`
- `behavior_samples`
- `stock_behavior_profiles`

顺序任务：

1. 建立正式历史数据存储结构：
   - `db/`
   - `lake/`
   - `state/`
   - `cache/`
2. 先完成日线全量与日增量补齐。
3. 再完成分钟线近端窗口落盘。
4. 基于历史原始层计算股性画像。
5. 输出给 Agent 可直接消费的摘要层。
6. 将 freshness、partition、run_id 纳入统一索引。

调度节奏：

- 收盘后：补 `1d` 日线并刷新最新交易日
- 夜间：修补 `1m/5m` 缺口并计算特征层
- 盘前：做 freshness 检查，必要时补跑增量

交付标准：

- 重点标的可本地直接读到日线历史
- 近端分钟线可本地直接读到
- 股性画像与摘要层可供 runtime / Hermes 消费
- 每日补数具备幂等、可追溯、可重跑

### C 线：Hermes 模型路由

目标：

- 让 Hermes 从“展示模型槽位”升级为“按岗位和风险级别分配智能”的正式编排层。

顺序任务：

1. 把模型槽位从页面展示别名改成可执行策略配置。
2. 建立岗位到模型槽位的路由规则。
3. 建立任务类型到模型槽位的覆盖规则。
4. 引入按风险升级模型机制：
   - 高仓位影响
   - 多角色意见冲突
   - `regime=chaos/defensive`
   - 大额换仓
   - 异常成交或执行失败
5. 让 Hermes 主控、研究、风险、执行等岗位走不同智能档位。

交付标准：

- 同一平台内不同岗位可使用不同模型策略
- 关键任务可自动升级更强模型
- 普通问答与状态巡检继续走快模型
- 模型选择过程可追溯，可解释

### 10.2 推荐实施顺序

推荐按以下顺序推进：

1. 先做本地历史底座
   - 先完成数据库与索引层
   - 先日线全量 + 日增量
   - 再分钟线近端窗口
   - 同步产出股性画像和摘要层
2. 再做 Hermes 模型路由
   - 先把模型槽位从展示改成可执行策略
   - 再加岗位到模型的路由规则
   - 最后加“按风险升级模型”

设计理由：

- 数据库与索引层先完成，历史底座才不会继续变成新的散乱文件堆。
- 历史底座完成后，Hermes 和 Agent 才有稳定的历史上下文可读。
- 模型路由后完成，才能真正让更强模型消费更好的数据，而不是空耗在缺历史证据的任务上。

---

## 10.3 代码实施任务单

以下实施单直接挂在总改造清单下，按代码落地顺序拆分。

### T1：数据库与索引层

#### T1-1 建立控制面主库

目标：

- 在 `storage_root/db/control_plane.sqlite3` 建立正式主库，承接控制面结构化存储。

建议新增文件：

- `src/ashare_system/data/control_db.py`
- `src/ashare_system/data/schema.sql`
- `src/ashare_system/data/migrations.py`

建议改动文件：

- [settings.py](/srv/projects/ashare-system-v2/src/ashare_system/settings.py)
- [container.py](/srv/projects/ashare-system-v2/src/ashare_system/container.py)

核心任务：

1. 增加 `control_db_path` 配置。
2. 在启动阶段初始化主库与 schema 版本。
3. 提供最小数据库访问封装，避免业务层到处直连 SQL。

验收：

- 服务启动后主库自动初始化
- schema version 可见
- 不影响现有 JSON 状态读写

#### T1-2 建立数据目录与分区索引

目标：

- 为后续历史底座提供正式索引层。

建议新增表：

- `dataset_catalog`
- `dataset_partitions`
- `ingestion_runs`

建议新增文件：

- `src/ashare_system/data/catalog_service.py`

建议改动文件：

- [precompute.py](/srv/projects/ashare-system-v2/src/ashare_system/precompute.py)
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)

核心任务：

1. 定义数据集元信息。
2. 每次写入历史数据时登记：
   - `dataset_name`
   - `trade_date`
   - `path`
   - `row_count`
   - `source`
   - `freshness_status`
3. 为增量补数任务写 `run_id` 和状态。

验收：

- 任意数据集都能先查 catalog 再定位文件
- 补数任务可追溯

#### T1-3 建立控制面 trace 表

目标：

- 把讨论、执行、回执、Agent 动作从松散状态文件中逐步结构化。

建议新增表：

- `discussion_traces`
- `execution_traces`
- `gateway_receipts`
- `agent_actions`

建议新增文件：

- `src/ashare_system/data/trace_repo.py`

建议改动文件：

- [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)
- [execution_gateway.py](/srv/projects/ashare-system-v2/src/ashare_system/execution_gateway.py)
- [discussion_service.py](/srv/projects/ashare-system-v2/src/ashare_system/discussion/discussion_service.py)

核心任务：

1. 记录 `case_id / symbol / stage / actor / status / created_at`。
2. 把关键派发与回执同步写入 SQLite。
3. 为主控面板提供统一 trace 读取能力。

验收：

- 单只票的讨论到执行链可回放
- 面板不必跨多个 JSON 拼事实

#### T1-4 建立全文检索

目标：

- 支持纪要、审计、评审、讨论摘要的快速检索。

建议新增表：

- `documents`
- `documents_fts`

建议新增文件：

- `src/ashare_system/data/document_index.py`

建议新增接口：

- `/system/search/documents`
- `/system/search/discussions`

验收：

- 可按关键词检索历史纪要与讨论
- Hermes 和 Agent 可以调用统一检索口

### T2：本地历史底座

#### T2-1 目录重整

目标：

- 在 `storage_root` 下建立正式数据分层目录。

建议目录：

- `db/`
- `lake/`
- `state/`
- `cache/`

建议改动文件：

- [settings.py](/srv/projects/ashare-system-v2/src/ashare_system/settings.py)
- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)

验收：

- 启动时自动创建目录
- 旧路径仍可兼容读取，避免一次性破坏现有运行

#### T2-2 日线全量与日增量

目标：

- 建立 `1d` 本地历史事实源。

建议新增文件：

- `src/ashare_system/data/history_ingest.py`
- `src/ashare_system/data/history_store.py`

建议改动文件：

- [precompute.py](/srv/projects/ashare-system-v2/src/ashare_system/precompute.py)
- [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py)

核心任务：

1. 从 QMT / Go 平台读取 `1d` 历史。
2. 写入 `lake/bars_1d/trade_date=YYYY-MM-DD/*.parquet`。
3. 每日执行增量补齐。
4. 把最新分区登记到 `dataset_partitions`。

验收：

- 重点股票可本地读到完整日线
- 非交易日和交易日都能校验 freshness

#### T2-3 分钟线近端窗口

目标：

- 建立盘中与近端研究需要的分钟线窗口历史。

建议数据集：

- `bars_1m`
- `bars_5m`

核心任务：

1. 保留近 60-120 个交易日分钟线窗口。
2. 夜间补缺。
3. 盘前做 freshness 检查。

验收：

- 指定 symbol 可快速读取最近分钟线窗口
- 缺口可自动识别并补数

#### T2-4 股性画像与摘要层

目标：

- 把历史底座变成 Agent 真正可读的能力，而不是只存原始 bar。

建议新增表：

- `stock_behavior_profiles`

建议新增数据集：

- `behavior_samples`

建议改动文件：

- [precompute.py](/srv/projects/ashare-system-v2/src/ashare_system/precompute.py)

核心任务：

1. 从历史 K 线和行为样本生成结构化股性画像。
2. 输出短摘要层供 runtime / Hermes 使用。
3. 记录 `sample_days / zt_sample_days / source / profile_trade_date`。

验收：

- 每只重点标的都可输出画像与摘要
- Agent 不必直接读长 K 线数组

#### T2-5 历史读取接口

目标：

- 给程序和 Agent 提供正式历史读取通道。

建议新增接口：

- `/runtime/history/behavior-profile`
- `/runtime/history/stock-summary`
- `/runtime/history/bars`
- `/runtime/history/factor-values`

建议改动文件：

- [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- [data_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/data_api.py)

验收：

- Hermes / runtime 能直接消费历史摘要与研究数据

### T3：Hermes 模型路由

#### T3-1 模型槽位从展示改成策略配置

目标：

- 让 `HERMES_MODEL_SLOTS` 不再只是页面展示项。

建议改动文件：

- [hermes_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/hermes_api.py)

建议新增文件：

- `src/ashare_system/hermes/model_router.py`
- `src/ashare_system/hermes/model_policy.py`

核心任务：

1. 给模型槽位增加：
   - `capability_level`
   - `allowed_roles`
   - `allowed_tasks`
   - `cost_tier`
2. 提供统一路由函数，而不是页面硬编码别名。

验收：

- 模型槽位可被程序真实选择
- 控制台显示与程序执行逻辑一致

#### T3-2 岗位路由规则

目标：

- 不同岗位走不同智能档位。

建议岗位：

- `hermes_master`
- `strategy_analyst`
- `risk_gate`
- `execution_operator`
- `event_researcher`
- `runtime_scout`

核心任务：

1. 建立岗位到模型槽位映射。
2. 为闲聊/快巡检保留快模型。
3. 为研究/策略/风险保留主模型或强模型。

验收：

- 同一平台内不同岗位能稳定使用不同模型策略

#### T3-3 按风险升级模型

目标：

- 把强模型用在真正高价值任务，而不是全平台无差别启用。

核心任务：

1. 设计升级触发条件：
   - 高仓位影响
   - `regime=chaos/defensive`
   - 多角色冲突
   - 大额换仓
   - 派发失败 / 异常成交
2. 在 Hermes 主控链路中引入升级判断。
3. 记录“为什么升级到更强模型”的 trace。

验收：

- 关键高风险任务会自动切换更强模型
- 普通任务不被强模型拖慢

#### T3-4 面板与问答联动

目标：

- 让模型路由的结果在 Hermes 页面和问答链里可见、可解释。

建议改动文件：

- [hermes_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/hermes_api.py)
- Hermes 前端展示页

核心任务：

1. 页面显示岗位默认模型与升级规则摘要。
2. 会话记录中保留 `model_id / profile_id / route_reason`。
3. 问答链支持通用回答与业务链并存。

验收：

- 你能在页面和日志里看到模型为什么这么选

### T4：统一验收顺序

推荐按以下顺序验收：

1. `T1` 数据库与索引层先通过
2. `T2` 历史底座再通过
3. `T3` Hermes 模型路由最后通过

原因：

- 没有 `T1`，`T2` 很容易继续变成散乱文件堆。
- 没有 `T2`，`T3` 就只是把更强模型接到缺历史上下文的系统上。

---

## 10.4 总任务清单

下面把整份总方案统一收口成可执行任务清单。默认状态均为 `pending`，后续可直接在本节逐项标注 `in_progress / done / blocked`。

### P0 主链任务

#### R0-1 开盘执行主链落地

- 状态：`completed`
- 目标：把 `09:30` 开盘执行从空壳改成真实执行推进器
- 依赖：无
- 关键模块：
  - [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py)
  - [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)
- 完成定义：
  - 可自动拉取 precheck / intent / dispatch
  - 成功与失败都留痕
- 2026-04-21 注脚：
  - `scheduler.py` 已把开盘执行从空壳推进为真实 control-plane 调用链。
  - `system_api.py` 已补 execution chain prepare / progress / dispatch 入口。

#### R0-2 阶段性自动派发替代 finalize 单点闸门

- 状态：`completed`
- 目标：让 `round_summarized + approved + session_open` 可进入自动派发态
- 依赖：R0-1
- 关键模块：
  - [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [discussion_service.py](/srv/projects/ashare-system-v2/src/ashare_system/discussion/discussion_service.py)
- 完成定义：
  - intent 不再长时间悬空
  - 派发失败有明确原因
- 2026-04-21 注脚：
  - `refresh_discussion_cycle()` 已在满足条件时自动准备 precheck / intent，并按 trigger 推进派发。

#### R0-3 `chaos` 分级风控替代 `FREEZE_ALL`

- 状态：`completed`
- 目标：把 `chaos` 从全冻结改成风险收缩
- 依赖：无
- 关键模块：
  - [intraday_ranker.py](/srv/projects/ashare-system-v2/src/ashare_system/monitor/intraday_ranker.py)
  - [regime_detector.py](/srv/projects/ashare-system-v2/src/ashare_system/market/regime_detector.py)
- 完成定义：
  - 弱票冻结
  - 强票保留例外
  - 总预算可收缩
- 2026-04-21 注脚：
  - `intraday_ranker.py` 已改为弱候选 `FREEZE`、强候选 `DOWNGRADE`，不再全局 `FREEZE_ALL`。

#### R0-4 持仓/做T/换仓进入正式执行链

- 状态：`completed`
- 目标：让持仓卖出、做T低吸、换仓替换都形成稳定闭环
- 依赖：R0-2
- 关键模块：
  - [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py)
  - [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - [runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py)
- 完成定义：
  - `watch -> action -> precheck -> dispatch -> receipt` 跑通
- 2026-04-21 注脚：
  - `scheduler.py` 已增加 `execution.window:advance`，执行窗口可持续推进而不只依赖 finalize。

#### R0-5 `mainline_stage` 盘中误判修复

- 状态：`completed`
- 目标：盘中不再误显示为 `postclose_learning`
- 依赖：无
- 关键模块：
  - [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)
- 完成定义：
  - 盘中主线与 `phase_code` 一致
  - 盘后产物不会反向污染盘中主线
- 2026-04-21 注脚：
  - `system_api.py` 已限制 nightly / review_board 仅在 trade_date 与时段匹配时影响主线判断。

### P1 平台与数据任务

#### R1-1 连续执行窗口调度

- 状态：`completed`
- 目标：把盘中执行从单点触发改成连续推进
- 依赖：R0-1, R0-2
- 关键模块：
  - [scheduler.py](/srv/projects/ashare-system-v2/src/ashare_system/scheduler.py)
- 完成定义：
  - `09:25-14:57` 分阶段推进执行窗口
- 2026-04-21 注脚：
  - `scheduler.py` 已落地 `execution.window:advance`、`position.watch:fast_realtime`、`position.watch:check_realtime`、`execution.bridge_guardian:check`、`monitor.market_watcher:check_micro`。
  - 盘中主链已从“单点开盘动作”升级为“连续窗口推进”。

#### R1-2 执行链统一追踪视图

- 状态：`completed`
- 目标：统一展示 `candidate -> precheck -> intent -> dispatch -> receipt -> fill`
- 依赖：R0-2
- 关键模块：
  - [system_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/system_api.py)
  - Hermes 面板相关接口
- 完成定义：
  - 单只票全链路可视
- 2026-04-21 注脚：
  - 已新增 `/system/dashboard/execution-mainline` 与 `/system/execution/mainline-trace`。
  - 新视图把 `candidate -> precheck -> intent -> receipt -> fill -> attribution` 串成单条主链，支持按 `symbol/trace_id` 过滤。
  - 独立测试：`tests/test_dashboard_mission_control.py` 已新增该链路视图验证并通过。

#### R1-3 监督系统升级为主线推进器

- 状态：`completed`
- 目标：让 supervision 从催办变成强制推进
- 依赖：R0-2, R0-4
- 关键模块：
  - [supervision_tasks.py](/srv/projects/ashare-system-v2/src/ashare_system/supervision_tasks.py)
  - [supervision_state.py](/srv/projects/ashare-system-v2/src/ashare_system/supervision_state.py)
- 完成定义：
  - 超时席位会触发推进或升级策略
- 2026-04-21 注脚：
  - `scheduler.py` 的 `supervision.agent:check` 已会生成 `recommended_tasks`、自动催办并记录 `dispatch_key`。
  - `supervision_tasks.py` 已支持按主线阶段编排任务、同步完成态、按真实活动信号判定 completion。
  - `supervision_state.py` 已把 `attention / notify / escalate` 收口成结构化监督语义，而非纯提醒。

#### R1-4 飞书机器人双模问答

- 状态：`completed`
- 目标：业务命中走控制面，未命中回退基础 Agent 能力
- 依赖：无
- 关键模块：
  - 飞书长连接服务
  - Hermes / system 问答路由
- 完成定义：
  - 不再把所有自由问答都强封装成业务卡
- 2026-04-21 注脚：
  - `system_api.py` 已按 `casual_chat / open_chat / execution / supervision / status` 多路径分流。
  - 本轮已修复 `/system/feishu/ask` 对同一问题重复求解的问题，避免同一条消息跑两遍问答链路。
  - 轻量与自由问答默认 `prefer_plain_text=true`，闲聊不会再被强制封成业务卡。

#### R1-5 控制台改成业务作战台

- 状态：`completed`
- 目标：展示主线、卡点、执行态，而不是接口拼接
- 依赖：R1-2, R1-3
- 关键模块：
  - Hermes 前端
  - [hermes_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/hermes_api.py)
- 完成定义：
  - 用户能直接看懂全流程当前卡点
- 2026-04-21 注脚：
  - 当前控制台数据面已由 `/system/dashboard/mission-control`、`/system/dashboard/opportunity-flow`、`/system/dashboard/execution-mainline`、`/system/agents/supervision-board` 组成。
  - Hermes 前台不再只是接口目录，而是市场 / 讨论 / 执行 / 监督 / 历史底座的业务作战台。

#### R1-6 控制面数据库与索引层

- 状态：`completed`
- 目标：建立 SQLite 主库与统一索引层
- 依赖：无
- 对应实施单：T1-1, T1-2, T1-3, T1-4
- 完成定义：
  - `control_plane.sqlite3`
  - `dataset_catalog / dataset_partitions / ingestion_runs`
  - `documents + FTS5`
- 2026-04-21 注脚：
  - 已新增 `data/control_db.py`、`data/schema.sql`、`data/migrations.py`、`data/catalog_service.py`、`data/document_index.py`。
  - 已新增 `/system/search/documents` 与 `/system/search/catalog`。
  - `/system/search/catalog` 已补充 `capabilities` 与 `latest_history_runtime`，不再只回 dataset 索引。
  - 独立测试：`tests/test_control_plane_data_layer.py` 通过。

#### R1-7 本地历史底座第一阶段

- 状态：`completed`
- 目标：建立日线、分钟线、股性画像与摘要层
- 依赖：R1-6
- 对应实施单：T2-1, T2-2, T2-3, T2-4, T2-5
- 完成定义：
  - 日线全量 + 日增量
  - 分钟线近端窗口
  - 股性画像与摘要可读
- 2026-04-21 注脚：
  - 已新增 `data/history_store.py`、`data/history_ingest.py`。
  - 已新增 `/runtime/history/capabilities`、`/runtime/history/ingest/*`、`/runtime/history/behavior-profile`、`/runtime/history/stock-summary`。
  - 已补齐 `/runtime/history/bars`、`/runtime/history/bars/latest`、`/runtime/history/partitions`、`/runtime/history/search-symbol`、`/runtime/history/context`、`/runtime/history/regime-memory`。
  - 已补齐 `/runtime/history/ingest/daily-backfill`，可按批次执行全市场日线历史回填，不再局限于示例样本。
  - `scheduler.py` 已新增正式任务：`history.ingest:daily`、`history.ingest:minute`、`history.ingest:behavior_profiles`。
  - 已修复同一交易日批量入湖文件覆盖问题，现按批次来源生成独立 parquet 文件。
  - 已修复历史日线按“入湖日期”错误分区的问题，当前改为按每根 bar 的真实 `trade_time/trade_date` 分区落湖，历史查询语义与真实交易日一致。
  - `/system/dashboard/mission-control` 与总控台首页已接入历史底座透视，能直接看到最近入湖状态、能力和最新作业。
  - 分钟线入湖已改为动态组合选池：优先覆盖持仓、候选池、runtime top picks、盘中异动机会、预检股票，再用主板池补足。
  - Hermes 办公台 `/system/hermes/integrations/ashare/overview` 与飞书简报 `/system/feishu/briefing` 已带入历史底座摘要与 `/system/search/catalog` 数据引用。
  - compose 主链已新增 `history_context` 服务端注入，Agent 不再只能“自己想起来再查历史”，而是在 compose request 中直接拿到历史摘要。
  - 真实导入实测：
    - `bars_1d` 已对 600 只主板股票写入 3 个 parquet 分片，总计 65,600 行。
    - `bars_1m` 已对 20 只股票写入 4,800 行分钟线窗口。
    - `stock_behavior_profiles` 已对 43 只股票完成入库。
  - 2026-04-21 新增实测：
    - 已按真实交易日分区对全 A 股 5,505 只股票写入 480 根日线窗口，总计 2,546,880 行、21,284 个分区。
    - 当前控制库 `control_plane.sqlite3` 中 `bars_1d` 分区范围为 `2024-04-25 ~ 2026-04-21`。
  - 当前环境若无 `pyarrow/fastparquet/duckdb`，会真实回落到 `jsonl` 落盘并明确暴露能力状态，不伪装成 parquet 已启用。
  - 独立测试：`tests/test_control_plane_data_layer.py` 通过。

### P2 学习与智能编排任务

#### R2-1 Elo 自动喂数与归因闭环

- 状态：`completed`
- 目标：把讨论终态、执行结果和收益风险自动写入评分系统
- 依赖：R1-2
- 完成定义：
  - Elo 不再是孤立算法
- 2026-04-21 注脚：
  - `discussion_service.py` 的 `finalize_cycle()` 已自动调用 `_auto_settle_discussion_agents()`。
  - `evaluation_ledger.py` 已在后验结果回写时自动构建 `agent_score_settlement`。
  - `score_state.py` 的 `run_daily_settlement()` 已统一落地 pairwise rating 与学分状态更新。

#### R2-2 因子连续加权与有效性精细化

- 状态：`completed`
- 目标：把因子有效性从粗糙规则升级为连续加权
- 依赖：R1-7
- 完成定义：
  - 无效因子不再与有效因子同权
- 2026-04-21 注脚：
  - `compose_factor_policy.py` 已按 `mean_rank_ic + p_value` 连续调权，不再固定乘 `0.3`。
  - `unsupported_for_monitor` 因子不再伪装为“已验证有效”，而是显式进入 outcome attribution 事后归因链。
  - `runtime_api.py` 已把因子守门接入 compose 主链与自动派发审查链。

#### R2-3 regime 分层回测与参数校准

- 状态：`completed`
- 目标：校准 `chaos/defensive/trend/rotation` 下的执行口径
- 依赖：R1-7
- 完成定义：
  - 关键参数不再纯拍脑袋
- 2026-04-21 注脚：
  - `backtest/walk_forward.py` 已提供 regime conditional walk-forward 验证器。
  - `strategy_composer.py` 已把 `composite_adjustment_multiplier` 从默认拍脑袋值切到账本样本自动估算；缺样本时保守回退到 `4.0`，不再沿用 `10.0`。
  - `system_api.py` 已提供 `offline backtest metrics` 的 regime review 汇总，能暴露最弱 regime 桶并驱动参数复核。
  - 真实边界：当前是“样本驱动 + 保守回退”的线上治理，不是全自动在线重估所有 regime 参数。

#### R2-4 跨市场/另类数据/成长质量补强

- 状态：`completed`
- 目标：补当前因子维度空白
- 依赖：R1-7
- 完成定义：
  - 四大缺口有明确补齐路径
- 2026-04-21 注脚：
  - `factor_registry.py` 已补齐首批真实因子组：
    - `chip_distribution`
    - `macro_environment`
    - `cross_market`
    - `alternative_data`
    - `growth_quality`
  - `factor_monitor.py` 已把无法做横截面 IC 的组标记为 `unsupported_for_monitor`，并要求走后验归因，而不是继续同权盲用。
  - 当前完成的是“第一阶段补强与主链接入”，后续仍可继续加深样本质量与覆盖范围。

#### R2-5 Hermes 模型路由正式化

- 状态：`completed`
- 目标：按岗位与风险级别分配模型能力
- 依赖：R1-7
- 对应实施单：T3-1, T3-2, T3-3, T3-4
- 完成定义：
  - 模型槽位从展示变成可执行策略
  - 关键任务可升级更强模型
- 2026-04-21 注脚：
  - 已新增 `hermes/model_policy.py` 与 `hermes/model_router.py`。
  - `hermes_api.py` 的 `/system/hermes/models` 已切到真实槽位配置，新增 `/system/hermes/models/resolve`。
  - 独立测试：`tests/test_hermes_model_router.py` 通过。

### 建议执行顺序

1. 先完成 `R0-1` 到 `R0-5`
2. 再完成 `R1-6` 与 `R1-7`
3. 然后完成 `R1-1` 到 `R1-5`
4. 最后推进 `R2-1` 到 `R2-5`

这样排的原因：

- 先把交易主链修通，否则后续数据和智能编排没有落点。
- 再把数据库与历史底座打牢，否则后续 Agent 能力提升缺稳定证据。
- 最后再做学习和模型路由，避免把更强模型接到仍然混乱的底座上。

---

## 11. 最终结论

本轮改造的核心不是继续加功能，而是改变系统的组织方式：

```text
从：
Agent 在旁边提建议，程序偶尔执行

改成：
程序拥有硬主链，Agent 为主链提供受约束的高质量决策
```

这也是本项目从“复杂控制面”走向“可稳定实盘系统”的必要条件。

如果后续按这份方案落地，系统的评价标准也应该同步改变：

- 不再看它“会不会说”
- 不再看它“模块多不多”
- 而看它能不能在交易时段持续、稳定、可追责地推进真实交易主链
