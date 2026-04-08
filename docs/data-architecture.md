# ashare-system-v2 数据底座总方案

> 版本: v0.9 草案
> 更新时间: 2026-04-05
> 目标: 为选股、盯盘、实盘执行、多 Agent 讨论提供统一的数据发现、标准化、持久化与服务底座。

## 1. 设计目标

本系统的数据层必须同时满足 3 个目标：

1. 数据要足够全。
   - 能持续发现影响股市与个股的行情、指数、板块、公告、新闻、政策、情绪变化。
2. 数据要能服务分析。
   - 既能支撑技术分析、量化因子、风控，也能支撑盘中跟踪与复盘。
3. 数据要能服务 Agent。
   - 不是把原始数据堆给 Agent，而是把标准化后的 dossier、事件摘要、证据链、冲突点提供给不同角色。

当前系统已经具备部分行情与状态存储能力，但距离“完整数据底座”还有明显缺口：

- 有 XtQuant 行情入口，但缺统一历史库。
- 有研究状态存储，但缺自动化、多源事件发现。
- 有候选股 dossier 预计算，但缺完整的原始层 / 标准层 / 特征层分层。
- 有监控状态和讨论状态，但缺统一的 serving 层输出给 Agent。

## 2. 总体架构

```text
外部数据源
  -> ingest 采集层
  -> raw 原始层
  -> normalized 标准层
  -> feature 特征层
  -> serving 服务层
  -> runtime / monitor / discussion / agent dossier / notify
```

五层职责固定如下：

### 2.1 ingest 采集层

负责主动发现和拉取数据，不负责分析。

采集对象：

- 个股实时行情
- 指数 / 大盘实时行情
- 板块 / 情绪 / 涨跌停结构
- 个股 K 线
- 公司公告
- 新闻快讯
- 宏观 / 政策 / 行业事件
- 账户与执行数据

### 2.2 raw 原始层

保留原始载荷，便于追溯、复盘和重新清洗。

原则：

- 不丢字段
- 保留源头时间戳
- 保留 source、symbol、request_id、fetched_at
- 不在 raw 层做复杂业务判断

### 2.3 normalized 标准层

将不同来源的数据清洗为统一结构。

统一项：

- symbol
- name
- trade_date
- event_at
- source
- source_type
- category
- severity
- freshness_seconds
- dedupe_key
- payload

### 2.4 feature 特征层

为技术分析、量化模型、风险模型与 Agent 讨论生成可消费特征。

典型产物：

- 量价指标
- 趋势与波动指标
- 板块强弱与市场情绪
- 事件影响评分
- 公告重要性评分
- 事件与价格共振判断
- 候选股综合 dossier

### 2.5 serving 服务层

向系统其他模块提供稳定接口。

消费方：

- runtime pipeline
- monitor watcher
- discussion cycle
- audit
- notify
- main / ashare / 子 Agent

## 3. 数据域划分

系统数据按 6 个域管理。

### 3.1 市场行情域

包含：

- 个股实时快照
- Tick / 分钟 / 日线
- 买一卖一 / 成交量 / 昨收 / 涨跌幅

当前主来源：

- XtQuant / xtdata

目标状态：

- 盘中有实时缓存
- 盘后有日线归档
- 候选池和执行池可按 symbol 快速检索最近行情

### 3.2 指数与市场结构域

包含：

- 上证、深证、创业板、沪深 300、中证 500 等指数
- 涨跌停数量
- 连板高度
- 市场成交额
- 板块轮动
- 强势 / 弱势情绪结构

当前状态：

- 代码支持指数行情读取
- 尚未形成项目内独立的历史指数 / 市场结构库

目标状态：

- 指数快照按固定周期落盘
- 市场结构指标形成独立特征集
- 可作为 risk / strategy / audit 共用上下文

### 3.3 事件研究域

包含：

- 公司公告
- 财报 / 业绩预告
- 回购 / 减持 / 监管 / 停复牌
- 新闻快讯
- 行业 / 政策 / 宏观事件

当前状态：

- 已有 `news` / `announcements` 状态存储
- 当前主要依赖接口写入
- 缺自动发现和多源抓取

目标状态：

- 自动采集 + 手工补录双通道
- 去重、分类、影响评分、时间衰减
- 每只候选股可回溯最近重要事件

### 3.4 账户执行域

包含：

- 资产
- 持仓
- 委托
- 成交
- 撤单
- 执行前检查
- 执行后对账

目标：

- 为 executor / audit / notify / learning 提供统一事实源

### 3.5 讨论治理域

包含：

- candidate cases
- discussion cycles
- round opinions
- meeting notes
- audit records
- agent score states

目标：

- 把讨论过程结构化保存，而不是只保留自然语言

### 3.6 学习反馈域

包含：

- 次日涨跌结果
- 选中 / 落选后的结果回写
- agent 学分变化
- 策略采纳与效果回写

目标：

- 形成“讨论 -> 决策 -> 执行 -> 结果 -> 学习”的闭环

## 4. 数据源优先级

为避免混乱，数据源按优先级接入。

### 4.1 一级数据源

用于生产主链，优先级最高。

- XtQuant 行情与账户数据
- 交易所 / 券商可验证公告源
- 官方或高可信新闻源

### 4.2 二级数据源

用于补充和交叉验证。

- 第三方财经快讯
- 行业资讯
- 板块 / 情绪聚合源

### 4.3 三级数据源

仅用于参考，不直接作为执行依据。

- 网络公开舆情
- 搜索热度
- 社交讨论热词

规则：

- risk / audit 在引用三级数据时，必须看到一级或二级数据的支撑，不能单独放行执行。

## 5. 项目目录规划

所有数据都应放在项目目录，不放系统盘散路径。

建议在 `ASHARE_STORAGE_ROOT` 下分层：

```text
.ashare_state/
  raw/
    market/
    index/
    announcements/
    news/
    policy/
  normalized/
    market/
    events/
    market_structure/
  features/
    factors/
    sentiment/
    dossiers/
  serving/
    latest_market_snapshot.json
    latest_market_structure.json
    latest_event_summary.json
    latest_dossier_pack.json
  cache/
  runtime_state.json
  research_state.json
  meeting_state.json
  monitor_state.json
  candidate_cases.json
  discussion_cycles.json
  agent_score_states.json
  audits.json
```

说明：

- `raw/` 保存原始数据
- `normalized/` 保存标准化结果
- `features/` 保存可复用的特征和 dossier
- `serving/` 保存系统当前对外服务的最新版本
- 现有 `cache/` 保留，用于轻量本地缓存

## 6. 刷新频率与保留策略

默认策略必须可通过自然语言动态调整，不写死到流程里。

### 6.1 盘中刷新

- candidate pool 行情: 300s
- focus pool 行情: 60s
- execution pool 行情: 30s
- 竞价阶段 heartbeat: 300s
- 正常 heartbeat: 600s
- 重大事件拉取: 60s 到 300s，视数据源而定

### 6.2 保留策略

- 实时 serving 快照: 仅保留最新
- heartbeat / pool snapshot 历史: 保留 200 条
- dossier pack: 保留最近 5 个交易日
- archive: 保留最近 20 个交易日
- raw 新闻 / 公告: 按交易日归档，后续可扩展为更长周期

### 6.3 原则

- TTL 应与监控节奏保持一致，避免每次都触发重复拉取
- 是否刷新由 “是否过期 + 是否签名变化 + 是否达到最小轮询间隔” 共同决定

## 6A. 历史市场数据存储策略

历史市场数据必须是正式资产，不是临时缓存。

### 6A.1 必存对象

系统至少要保存以下历史数据：

- 核心指数历史
  - 上证指数
  - 深证成指
  - 创业板指
  - 沪深 300
  - 中证 500
  - 中证 1000
- 市场结构历史
  - 涨停数
  - 跌停数
  - 连板高度
  - 两市成交额
  - 板块强度排名
  - 情绪阶段标签
- 候选池个股历史
  - 日线
  - 分钟线
  - 盘中关键快照
- 执行池个股历史
  - 更高频的分钟线与盘中快照

### 6A.2 时间粒度

建议按 4 档保存：

- `1d`
  - 用于中短线趋势、结构、回测和 Agent 复盘
- `60m`
  - 用于波段节奏和日内结构判断
- `5m`
  - 用于盘中观察、候选晋级、风控和复核
- `snapshot`
  - 用于关键时间点事实记录

原则：

- 不是所有股票都保存同样高频的数据
- 高频粒度优先给 `focus_pool` 和 `execution_pool`
- 全市场范围主要保存 `1d` 和核心指数 / 市场结构

### 6A.3 存储目录建议

建议在 `ASHARE_STORAGE_ROOT` 下新增：

```text
.ashare_state/
  raw/
    market/
      symbol/
        1d/
        60m/
        5m/
      index/
        1d/
        60m/
        5m/
      structure/
        1d/
        intraday/
  normalized/
    market/
      symbol/
      index/
      structure/
  features/
    market_context/
    symbol_context/
```

### 6A.4 保存范围控制

为了避免数据量失控，保存范围分级：

- `tier_1`
  - 核心指数、市场结构
  - 长期保留
- `tier_2`
  - 当前候选池 30 只
  - 保留近 20 个交易日高价值数据
- `tier_3`
  - 历史曾进入 focus / execution 的股票
  - 保留用于复盘与学习

### 6A.5 保留周期建议

- 指数日线: 长期保留
- 指数分钟线: 保留 60 个交易日
- 市场结构日度摘要: 长期保留
- 市场结构盘中快照: 保留 20 个交易日
- 候选池日线: 保留 60 个交易日
- 候选池 5m / 60m: 保留 20 个交易日
- 执行池盘中关键快照: 保留 60 个交易日

### 6A.6 当前实现改造原则

现有 `cache/*.json` 只能作为轻缓存，不应承担正式历史库职责。

后续应将：

- `cache/` 保留为短 TTL 缓存层
- 历史行情正式写入 `raw/market` 与 `normalized/market`
- dossier 和 serving 从历史库派生，不直接依赖 cache

## 6B. 数据新鲜度策略

新鲜度不是附加字段，而是决策门槛。

### 6B.1 新鲜度定义

每份数据都必须带：

- `fetched_at`
- `source_at`
- `generated_at`
- `expires_at`
- `staleness_level`

其中：

- `source_at`
  - 数据源自身时间
- `fetched_at`
  - 本系统抓到数据的时间
- `generated_at`
  - 本系统完成标准化或 dossier 生成的时间
- `expires_at`
  - 当前数据何时失效

### 6B.2 新鲜度分级

建议统一使用：

- `fresh`
  - 可直接用于实时分析和执行
- `warm`
  - 可用于讨论和观察，但不应直接驱动执行
- `stale`
  - 仅可用于参考，不能作为执行依据
- `expired`
  - 不可用，必须刷新

### 6B.3 建议阈值

- execution pool 实时快照
  - `fresh <= 30s`
- focus pool 实时快照
  - `fresh <= 60s`
- candidate pool 实时快照
  - `fresh <= 300s`
- 大盘 / 指数实时快照
  - `fresh <= 60s`
- 新闻快讯
  - `fresh <= 300s`
- 公告类事件
  - 按事件发生后持续有效，但需标注是否已过交易窗口
- dossier
  - 与 candidate poll 周期一致，默认 `<= 300s`

### 6B.4 决策门槛

不同消费方对新鲜度要求不同：

- `executor`
  - 只接受 `fresh`
- `risk`
  - `fresh` 或 `warm`
- `strategy`
  - `fresh` 或 `warm`
- `research`
  - 可接受 `warm`
- `audit`
  - 可接受 `warm`，但必须明确标出时效

### 6B.5 刷新触发条件

数据刷新应由以下条件共同决定：

- 已过 `expires_at`
- 候选池签名变化
- 进入新交易阶段
  - 竞价
  - 开盘
  - 午后
  - 临近收盘
- 出现重大新闻 / 公告事件
- Agent 或用户主动要求强制刷新

### 6B.6 freshness 作为 Agent 输入

Agent dossier 中必须显式提供：

- `market_snapshot_staleness`
- `event_summary_staleness`
- `dossier_staleness`

这样 Agent 讨论时知道自己基于的是实时材料还是延迟材料。

## 6C. 历史走势与实时走势的联动逻辑

历史数据不是为了画图，而是为了给“当下”提供参照系。

每次候选分析都必须至少计算 3 个维度：

### 6C.1 个股 vs 大盘

- 个股相对指数强弱
- 市场弱时是否逆势
- 市场强时是否跟随

### 6C.2 个股 vs 板块

- 个股是否强于所属板块
- 是否是板块龙头、跟风或掉队

### 6C.3 个股 vs 事件

- 价格先动还是事件先出
- 事件出现后量价是否确认
- 是全市场驱动、板块驱动还是单股驱动

这三类关系都应进入 `symbol_context` 和 `decision_dossier`。

## 7. 标准化模型

### 7.1 行情快照模型

字段建议：

- symbol
- name
- source
- snapshot_at
- last_price
- bid_price
- ask_price
- pre_close
- change_pct
- volume
- amount
- turnover_rate
- market_phase

### 7.2 事件模型

字段建议：

- event_id
- source
- source_type
- symbol
- name
- title
- summary
- category
- sentiment
- severity
- event_at
- fetched_at
- freshness_seconds
- dedupe_key
- evidence_url
- raw_payload_ref

### 7.3 指数 / 市场结构模型

字段建议：

- snapshot_id
- snapshot_at
- index_symbol
- index_name
- last_price
- change_pct
- volume
- amount
- limit_up_count
- limit_down_count
-连板_highest
- broad_breadth
- market_sentiment_label

注：

- 字段名应使用 ASCII，实际实现时把 `连板_highest` 统一改为 `limit_up_streak_highest`

## 8. Agent Dossier 设计

这是本方案的核心输出，不是可选项。

每个候选股必须能生成标准 dossier，供 `ashare-research`、`ashare-strategy`、`ashare-risk`、`ashare-audit` 共用。

### 8.1 dossier 必含字段

- symbol
- name
- trade_date
- rank
- selection_score
- final_status
- risk_gate
- audit_gate
- market_snapshot
- daily_bar
- intraday_summary
- sector_summary
- market_context
- recent_events
- announcement_summary
- positive_points
- negative_points
- open_questions
- evidence_chain
- generated_at
- expires_at

### 8.2 子 Agent 使用方式

- `ashare-research`
  - 关注 `recent_events`、`announcement_summary`、`market_context`
- `ashare-strategy`
  - 关注 `market_snapshot`、`daily_bar`、`intraday_summary`、技术特征
- `ashare-risk`
  - 关注 `negative_points`、波动、流动性、市场环境、仓位约束
- `ashare-audit`
  - 关注 `evidence_chain`、多角色结论是否一致、是否缺关键证据

### 8.3 输出原则

- Agent 讨论不直接读取原始 JSON 文件
- Agent 使用 serving 层或 dossier API
- 所有结论必须能回溯到证据链

## 9. API 与服务边界建议

建议新增数据服务边界，而不是把所有逻辑塞进现有 API。

### 9.1 建议新增读取接口

- `GET /data/market/snapshots/latest`
- `GET /data/index/snapshots/latest`
- `GET /data/events/latest`
- `GET /data/events/by-symbol/{symbol}`
- `GET /data/dossiers/latest`
- `GET /data/dossiers/{trade_date}/{symbol}`
- `GET /data/market-structure/latest`

### 9.2 建议新增写入 / 刷新接口

- `POST /data/ingest/events/news`
- `POST /data/ingest/events/announcements`
- `POST /data/ingest/market/refresh`
- `POST /data/ingest/index/refresh`
- `POST /data/dossiers/precompute`

原则：

- ingest 与 serving 分开
- 讨论系统只读 dossier，不直接碰采集细节

## 10. 与现有系统的衔接

当前已有能力可直接纳入本方案：

- `[src/ashare_system/infra/market_adapter.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/infra/market_adapter.py)` 作为行情入口
- `[src/ashare_system/data/cache.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/data/cache.py)` 作为轻缓存
- `[src/ashare_system/precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)` 作为 dossier 预计算起点
- `[src/ashare_system/apps/research_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/research_api.py)` 作为研究事件写入口
- `[src/ashare_system/monitor/persistence.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/monitor/persistence.py)` 作为盯盘快照持久化基础

现阶段不要推翻已有框架，只做以下收敛：

1. 把原始数据、标准化数据、特征数据和 serving 数据分层。
2. 把 `research_state.json` 从“临时研究状态”扩展为“事件研究域入口状态”。
3. 把 `precompute.py` 产出的 dossier 变成正式 serving 资产，而不是临时附属产物。
4. 把 monitor / discussion / notify 的数据读取统一收口到 serving 层。

## 11. 存储引擎演进策略

数据量上来之后，单纯 JSON 文件会出现 4 个问题：

- 单文件越来越大，读写变慢
- 并发写入容易冲突
- 按时间、symbol、trade_date 检索效率差
- 后续做复盘、统计、学习时聚合成本高

所以现在设计数据结构时，必须默认未来会“轻量数据库化”，但现阶段不强行上重型库。

### 11.1 当前建议

采用分阶段混合存储：

- `JSON`
  - 适合最新状态、配置、会议、审计、serving 快照
- `分文件归档`
  - 适合 raw 事件、原始行情批次、按交易日切分的数据
- `轻量数据库`
  - 适合历史行情、事件索引、因子结果、复盘检索

### 11.2 推荐演进路线

第一阶段：

- 保留现有 `StateStore + JSON`
- 同时把新增历史数据按目录分片
- 文件命名时就预留主键维度
  - `trade_date`
  - `symbol`
  - `period`
  - `source`

第二阶段：

- 引入 SQLite 或 DuckDB
- 文件层继续保留 raw 归档
- 数据库承接 normalized / feature / index 查询

第三阶段：

- serving 层统一从数据库 + 最新缓存拼装
- Agent 和量化都不再直接读取文件

### 11.3 轻量数据库选型建议

#### SQLite

优点：

- 嵌入式，部署简单
- 单机项目非常适合
- 适合结构化查询、主键检索、时间范围检索

适合：

- 事件索引
- symbol / trade_date / category 查询
- 讨论记录与执行记录索引

#### DuckDB

优点：

- 对分析型查询更强
- 做因子、历史回放、批量统计、复盘更方便

适合：

- 历史行情分析
- 大盘 / 板块 / 个股联表分析
- 学习反馈统计

#### 结论

对本项目建议采用：

- `SQLite` 作为事务型轻量索引库
- `DuckDB` 作为分析型历史库

如果要控制复杂度，第一步先只上 `SQLite`，等历史量明显上来再补 `DuckDB`。

### 11.4 现在就要预留的表结构思路

即便当前先用文件，也要按未来表结构设计字段。

建议核心表模型如下：

- `market_bars`
  - `symbol`
  - `name`
  - `period`
  - `trade_time`
  - `open/high/low/close`
  - `volume/amount`
  - `source`
  - `fetched_at`
- `market_snapshots`
  - `symbol`
  - `snapshot_at`
  - `last_price`
  - `bid_price`
  - `ask_price`
  - `pre_close`
  - `volume`
  - `staleness_level`
- `index_snapshots`
  - `index_symbol`
  - `snapshot_at`
  - `last_price`
  - `change_pct`
  - `amount`
- `market_structure_snapshots`
  - `snapshot_at`
  - `limit_up_count`
  - `limit_down_count`
  - `turnover_total`
  - `sentiment_label`
- `event_records`
  - `event_id`
  - `symbol`
  - `category`
  - `source`
  - `event_at`
  - `severity`
  - `dedupe_key`
  - `title`
  - `summary`
- `dossier_records`
  - `trade_date`
  - `symbol`
  - `generated_at`
  - `expires_at`
  - `signature`
  - `payload_json`

### 11.5 文件结构必须兼容数据库迁移

从现在开始，所有新增文件都遵守以下原则：

- 一条记录必须有稳定主键
- 必须有 `symbol` / `trade_date` / `timestamp` 之一
- 必须有 `source`
- 必须有时间字段
- 不把展示文本当成唯一数据载体
- 不把大对象无限堆进一个总文件

### 11.6 哪些数据优先数据库化

优先级建议：

1. 历史 K 线与指数数据
2. 事件记录与事件索引
3. 市场结构快照
4. dossier 历史
5. 学习反馈与绩效回写

以下数据可以继续保留 JSON：

- 最新运行状态
- 最新会议纪要
- 最新执行摘要
- 参数配置
- 少量审计与启动状态

## 12. 分阶段落地顺序

### 阶段 A: 数据地图与目录收口

- 定义目录结构
- 定义标准模型
- 固化保留与刷新策略

### 阶段 B: 事件发现能力

- 新闻与公告统一 ingestion
- 多源去重
- 事件分类与重要性评分

### 阶段 C: 指数与市场结构能力

- 建立大盘 / 指数 / 情绪快照
- 建立市场结构 serving 层

### 阶段 D: dossier 正式化

- 把现有 precompute 升级为正式 dossier pipeline
- 增加市场环境、事件摘要、证据链字段

### 阶段 E: Agent 接入

- `ashare` 统一调用 dossier / market context / event summary
- 子 Agent 仅消费结构化材料

### 阶段 F: 学习闭环

- 次日结果回写
- 学分系统与策略采纳记录联动

## 13. 当前阶段结论

本项目后续实现必须遵循一个原则：

> 数据不是附属品，而是多 Agent 协作、风控审计、执行与学习的共同底座。

若没有统一的数据发现、标准化、特征生成与 dossier 服务层：

- 技术分析会变薄
- 量化策略会缺支撑
- Agent 讨论会空转
- audit 无法真正审计
- learning 无法闭环

因此后续代码推进，优先级必须调整为：

1. 先补数据底座
2. 再补 dossier / serving
3. 再增强 Agent 协作细节
