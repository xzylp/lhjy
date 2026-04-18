# 可视化控制平台需求文档

> 适用对象：Gemini 前端/产品实现
> 项目：`ashare-system-v2`
> 文档版本：2026-04-16

## 1. 项目目标

构建一个 Web 可视化控制平台，作为 `ashare-system-v2` 的统一前台。

平台定位不是普通后台管理页，而是一套真正可用于量化交易团队协作的控制台：

- Agent 的指挥大厅
- 程序工具底座的可视化入口
- 飞书消息与人机交互的总控台
- 监督、风控、执行、学习的统一观察面

核心原则：

- Agent 是大脑
- 程序是手脚、监督系统和电子围栏
- 前台优先展示真实结构化数据，不自行编造行情结论
- 前台不是静态看板，而是“可操作、可追踪、可解释”的工作平台

## 2. 产品原则

### 2.1 角色关系

- Agent 负责发现机会、组织参数、调用 runtime、提出提案、讨论、学习
- 程序负责提供数据、执行、留痕、监督、风控围栏
- 可视化平台负责把这一切以高信息密度、可追踪、可交互的方式展现出来

### 2.2 展示原则

- 优先展示真实结构化接口数据
- 所有关键结论都要尽量附带来源或对应数据入口
- 所有关键卡片要体现“当前结论 + 下一步动作”
- 不允许把 mock 展示成 live

### 2.3 交互原则

- 所有页面必须支持从摘要 drill-down 到详情
- 所有高风险动作必须二次确认
- 所有真实交易相关按钮必须显式区分 `preview` 与 `apply`
- 不允许把平台做成仅能浏览、不能工作推进的展示页

## 3. 必须实现的功能模块

### 3.1 全局总览页

目标：一屏看到当前系统核心运行状态。

必须展示：

- 当前 `trade_date`
- `run_mode`
- `market_adapter`
- `execution_adapter`
- `feishu_longconn` 状态
- 讨论状态
- 执行状态
- 监督状态
- 研究状态
- 今日 `selected/watchlist/rejected` 数量
- 当前仓位、预算、风险约束、阻断数
- 最近飞书消息状态与回执状态

建议布局：

- 顶部全局状态条
- 中部四象限卡片：讨论 / 执行 / 风控 / 监督
- 底部简报流：飞书、执行、研究、催办

### 3.2 Agent 工作台

目标：观察每个 Agent 是否在持续有效工作。

必须展示：

- `ashare`
- `ashare-runtime`
- `ashare-research`
- `ashare-strategy`
- `ashare-risk`
- `ashare-audit`
- `ashare-executor`

每个 Agent 必须展示：

- `status`
- `activity_label`
- 最近活动时间
- 当前工作痕迹
- 是否被催办
- 最近评分与权重

必须支持：

- 查看某个 agent 的上下文入口
- 查看某个 agent 的最近动作摘要
- 查看与该 agent 相关的数据入口

### 3.3 机会票与讨论页

目标：把候选收敛过程做成真正的讨论台，而不是简单股票列表。

必须展示：

- `selected`
- `watchlist`
- `rejected`

每只票必须展示：

- `symbol`
- `name`
- 当前讨论定位
- `selected_reason`
- `risk_gate`
- `audit_gate`
- 研究摘要
- 替换候选关系

必须支持：

- 查看 `discussion context`
- 查看 `client brief`
- 查看 `meeting context`
- 从列表进入逐票详情

### 3.4 逐票详情页

目标：这是平台重点页面，必须做成“交易台复核卡”。

每只票必须展示：

- 基础信息
- 最新可得市场快照
- 研究摘要
- 持仓状态
- 执行预检状态
- 日内/尾盘信号
- `trade_advice`
- `trigger_conditions`
- `risk_notes`

必须支持：

- 查看该票当前讨论定位
- 查看当前执行阻断
- 查看是否适合替换/做 T/继续持有
- 查看数据来源

禁止：

- 做成普通行情页
- 做成只有价格和 K 线的股票详情页

### 3.5 执行控制页

目标：统一观察和操作执行链路。

必须展示：

- `execution_precheck`
- `execution_intents`
- `dispatch preview`
- `submitted`
- `blocked`

每条 intent 必须展示：

- `symbol`
- `quantity`
- `price`
- `blockers`
- `next action`

必须明确区分：

- `preview`
- `apply`
- `queued_for_gateway`
- `submitted`

必须要求：

- 所有真实执行动作都必须二次确认

### 3.6 风控与电子围栏页

目标：把系统真正控住了什么，清楚呈现出来。

必须展示：

- 当前有效风险参数
- 总仓位
- 单票金额
- 持仓数量
- 日损限制
- 排除方向

必须展示：

- 当前阻断原因分布
- 哪些标的被挡住
- 为什么被挡住
- 建议动作是什么

必须支持查看：

- `selection_preference` 命中情况
- 如银行、白酒等方向排除命中

### 3.7 调参与治理页

目标：让人和 Agent 都能高效消费参数体系。

必须实现：

- 自然语言调参输入框
- 预览模式
- 落地模式
- 最近参数提案列表
- 参数变更历史
- 当前生效参数

每项参数最好展示：

- 来源
- 当前值
- 状态
- 审批情况

### 3.8 飞书消息中心

目标：统一观察对外沟通链。

必须展示：

- 最近重要消息
- 最近催办消息
- 最近问答回复
- 最近执行回执
- 飞书长连接状态
- 最近 `reply_card / reply_lines` 样例

必须支持：

- 预览卡片消息 JSON
- 预览卡片渲染结果

### 3.9 盘后学习与评估页

目标：让学习闭环可见。

必须展示：

- `learned_assets`
- `evaluation_ledger`
- `outcome`
- `attribution`
- `learning_bridge`

每个学习产物至少展示：

- 状态
- advice
- 是否 active
- `discussion/risk/audit` 绑定

必须支持查看：

- 为什么被提升
- 为什么被观察
- 为什么被回滚

### 3.10 系统运维页

目标：统一观察系统可用性。

必须展示：

- 组件健康状态
- service endpoint
- scheduler 状态
- control plane 状态
- windows gateway 状态
- feishu longconn 状态
- 最近错误
- degraded reason

## 4. 交互要求

- 所有页面必须支持从摘要进入详情
- 所有关键卡片必须提供数据来源或对应接口入口
- 所有关键卡片必须展示下一步动作
- 所有高风险动作必须弹确认框
- 所有真实交易操作必须明确标注 `preview` 与 `apply`

## 5. 前端实现要求

### 5.1 技术栈建议

- React
- TypeScript
- Vite
- TanStack Query
- Zustand 或 Context 做轻状态
- ECharts 负责图表

### 5.2 UI 风格要求

- 专业交易台风格
- 不要做成普通 SaaS 后台
- 信息密度高，但分区清晰
- 优先卡片布局 + 侧边详情抽屉
- 桌面优先，同时兼容移动端查看

### 5.3 视觉要求

- 浅色中性底
- 使用红/绿/橙/蓝作为状态色
- 不要紫色 AI 风格
- 不要依赖过度动画
- 重点是信息效率和可扫描性

## 6. 数据接入要求

优先复用现有后端接口，不允许另造一套假数据模型。

建议优先接入：

- `/system/feishu/briefing`
- `/system/feishu/rights`
- `/system/feishu/ask`
- `/system/agents/supervision-board`
- `/system/discussions/client-brief`
- `/system/discussions/execution-precheck`
- `/system/discussions/execution-dispatch/latest`
- `/system/research/summary`
- `/system/account-state`
- `/system/params`
- `/system/params/proposals`
- `/runtime/strategy-repository`
- `/runtime/evaluations`
- `/runtime/learned-assets/*`

## 7. 页面与路由建议

建议至少包含以下路由：

- `/`
- `/overview`
- `/agents`
- `/discussion`
- `/discussion/:symbol`
- `/execution`
- `/risk`
- `/governance`
- `/feishu`
- `/learning`
- `/operations`

## 8. 组件设计要求

建议优先沉淀以下通用组件：

- `StatusBadge`
- `MetricCard`
- `AgentCard`
- `DiscussionCaseCard`
- `SymbolTradeAdviceCard`
- `ExecutionIntentTable`
- `RiskBlockerCard`
- `FeishuMessagePreview`
- `JsonPreviewDrawer`
- `DataSourceLinks`

## 9. Gemini 第一阶段交付要求

第一阶段请至少交付：

1. 页面信息架构图
2. 路由设计
3. 组件树设计
4. API 映射表
5. 关键页面高保真原型
6. 前端代码骨架
7. 至少 4 个页面可运行：
   - 全局总览
   - Agent 工作台
   - 机会票/讨论页
   - 逐票详情页

## 10. 代码质量要求

- TypeScript 严格类型
- 所有 API 响应定义类型
- 组件按业务分层
- 不允许一个页面塞进一个超大文件
- 所有关键页面必须实现 `loading / empty / error` 三态
- 所有关键卡片应支持后续接入飞书卡片 JSON 预览

## 11. 明确禁止事项

- 不要把平台做成普通 CRUD 后台
- 不要引入大量无关功能
- 不要虚构实时行情
- 不要绕开现有后端接口另造一套假数据模型
- 不要把 Agent 限制成固定 FAQ 机器人

## 12. 实现重点提醒

Gemini 在实现时需要始终记住：

- 平台服务的是“Agent 驱动的量化交易团队”
- 程序提供的是能力底座、监督和围栏
- 平台要展示的是“工作过程、决策依据、执行状态、风险与学习闭环”
- 不要把系统做成只会显示数值的面板
- 不要把它做成没有交易台感的通用管理站
