# 2026-04-21 数据存储与检索架构方案

## 1. 文档目标

这份文档解决三个问题：

1. 历史数据如何长期保存，避免继续散落成大量 JSON 和零散缓存。
2. 如何在保证结构清晰的前提下，让程序和 Agent 都能快速检索。
3. 如何把“本地历史底座”正式接入后续改造清单，而不是停留在概念层。

本文重点覆盖：

- 历史 K 线与分钟线
- 股性画像与摘要层
- 讨论、审计、执行 trace 的结构化沉淀
- Hermes / Agent 可消费的数据读取方式

---

## 2. 当前现状与问题

从仓库当前实现看，系统已经有不少本地落盘能力，但主要以 `JSON 状态文件 + serving 产物 + cache` 为主：

- `StateStore / AuditStore` 管大量运行态与审计状态  
  见：[audit_store.py](/srv/projects/ashare-system-v2/src/ashare_system/infra/audit_store.py)
- `storage_root` 下已有：
  - `runtime_state.json`
  - `research_state.json`
  - `discussion_state.json`
  - `execution_gateway_state.json`
  - 各类 `learning/*.json`
  - `cache/kline`
  见：[container.py](/srv/projects/ashare-system-v2/src/ashare_system/container.py)
- 股性画像已经有雏形，且存在历史记录概念：
  - `latest_stock_behavior_profiles`
  - `stock_behavior_profile_history`
  见：[precompute.py](/srv/projects/ashare-system-v2/src/ashare_system/precompute.py#L952)
- runtime 侧已经有 `KlineCache`
  见：[runtime_api.py](/srv/projects/ashare-system-v2/src/ashare_system/apps/runtime_api.py#L147)

这说明系统并不是完全没有本地数据基础，而是存在以下结构性问题：

### 2.1 状态文件适合轻状态，不适合长期数据资产

JSON 状态文件适合：

- 当前运行状态
- 当前讨论状态
- 当前执行状态
- 少量学习快照

但不适合长期承载：

- 日线全历史
- 分钟线窗口历史
- 因子快照
- 股性原始特征
- 大量 trace 记录
- 文本检索与归档

### 2.2 缺少统一目录与索引层

当前即使有数据，也缺少统一的：

- dataset 定义
- 分区登记
- freshness 登记
- schema 版本
- run_id / 来源 / 校验

结果就是：

- 文件会越来越多
- 人和程序都难以快速知道“该读哪份数据”

### 2.3 Agent 缺少结构化历史读取通道

Agent 目前更多消费实时接口和摘要产物，但对“历史走法、股性、同类样本、板块行为习惯”的读取通道还不够正式。

结果是：

- Agent 对过去的理解不稳定
- 很多历史认知只能临时现算
- 一旦实时桥抖动，历史上下文就容易断层

---

## 3. 设计目标

本次数据存储改造的目标不是“换个数据库”，而是建立正式的数据资产底座。

### 3.1 核心目标

1. 让历史数据长期可积累、可增量补全、可追溯。
2. 让程序和 Agent 都能快速拿到结构化历史上下文。
3. 让本地数据组织方式保持清晰，而不是继续堆目录和 JSON。
4. 支持后续：
   - 股性研究
   - 因子归因
   - regime 研究
   - 换仓比较
   - 盘后学习
   - Hermes 深度问答

### 3.2 不追求的目标

当前阶段不追求：

- 分布式数据库
- 多机实时写入
- 超大规模 OLAP 集群
- 引入 Kafka / Elasticsearch / ClickHouse 这类重基础设施

本项目当前更适合：

- 单机正式存储
- 轻运维
- 结构化检索
- 高可读目录

---

## 4. 推荐技术组合

本项目最适合采用 `分层组合`，而不是单一技术硬扛全部场景。

## 4.1 SQLite：控制面主数据库

适合存储：

- 元数据
- 索引
- 运行 trace
- 任务 run 记录
- 讨论/执行主链记录
- 股性画像结果
- freshness 状态
- 文档索引

选择理由：

- 单机部署足够稳
- Python 原生支持成熟
- 事务、索引、迁移、备份都简单
- 比散落 JSON 强很多
- 不会引入过高运维复杂度

### 推荐存储内容

- `symbols`
- `trading_calendar`
- `dataset_catalog`
- `dataset_partitions`
- `ingestion_runs`
- `stock_behavior_profiles`
- `factor_effectiveness_snapshots`
- `market_regime_snapshots`
- `discussion_traces`
- `execution_traces`
- `gateway_receipts`
- `agent_actions`
- `documents`

## 4.2 Parquet + DuckDB：历史行情与研究底座

适合存储：

- `1d` 历史 K 线全量
- `1m/5m` 近端分钟线窗口
- 因子原始值快照
- 盘后原始特征样本
- 回测明细

选择理由：

- 列式格式适合历史分析
- 本地单机查询快
- 适合按交易日、周期分区
- 适合大批量追加与盘后分析
- DuckDB 非常适合本地研究查询

### 推荐存储内容

- `bars_1d`
- `bars_1m`
- `bars_5m`
- `factor_values`
- `behavior_samples`
- `backtest_trades`

## 4.3 SQLite FTS5：文本全文检索

适合存储与检索：

- 讨论纪要
- 审计复盘
- 风控说明
- 机器人摘要
- 专家评审意见
- 文档说明书

选择理由：

- 足够轻
- 本地即用
- 支持全文检索
- 便于 Agent 和 Hermes 快速回查

## 4.4 热缓存层：仅做盘中快读

继续保留：

- 内存缓存
- 本地热点 cache
- serving 快照

但职责要收紧：

- 只负责快读
- 不再承担长期事实源角色

---

## 5. 分层数据架构

建议把 `storage_root` 重新组织为以下结构。

```text
storage_root/
  db/
    control_plane.sqlite3
  lake/
    bars_1d/
      trade_date=YYYY-MM-DD/
    bars_1m/
      trade_date=YYYY-MM-DD/
    bars_5m/
      trade_date=YYYY-MM-DD/
    factor_values/
      trade_date=YYYY-MM-DD/
    behavior_samples/
      trade_date=YYYY-MM-DD/
  state/
    runtime_state.json
    research_state.json
    discussion_state.json
    execution_gateway_state.json
  cache/
    kline/
    runtime/
    market/
  serving/
  learning/
  reports/
```

### 5.1 各层职责

#### `db/`

只存结构化、可索引、需要事务保障的数据。

#### `lake/`

只存中大体量的历史原始与研究型数据。

#### `state/`

只存运行态快照，不再继续承载长期资产。

#### `cache/`

只存热点数据和临时加速数据。

---

## 6. 历史底座设计

本地历史底座不应只等于“多存几根 K 线”，而应拆成三层。

## 6.1 原始层

直接保存原始市场历史。

### 范围

- 日线全历史
- 1m / 5m 近端窗口历史
- 必要的成交量、成交额、涨跌停信息

### 建议保留策略

- `1d`：尽量全历史
- `1m / 5m`：近 60-120 个交易日
- tick：只保留关键窗口或摘要，不建议长期全量存

## 6.2 特征层

把原始历史转成程序可用特征。

### 重点特征

- 触板率
- 封板率
- 炸板率
- 回封率
- 次日溢价
- 3 日延续
- 回撤修复
- 波动结构
- 量价风格
- 板块跟随性
- 龙头属性

## 6.3 摘要层

这是给 Agent 和 Hermes 直接消费的一层。

示例：

- `近 120 日 8 次触板，封板率 62.5%，次日平均溢价 1.8%`
- `更偏分歧回封型，不是一字加速型`
- `高波动高换手，适合超短，持股天数不宜长`

设计原则：

- Agent 优先读摘要层
- 程序计算和研究读原始层与特征层

---

## 7. 增量补全机制

历史底座必须支持日增量，且要可追溯。

## 7.1 调度建议

### 收盘后

- 补 `1d` 日线
- 校验最新交易日
- 更新股性原始样本

### 夜间

- 修补 `1m / 5m` 缺口
- 计算特征层
- 刷新画像层

### 盘前

- 做 freshness 检查
- 若缺失，则补跑增量

## 7.2 幂等要求

所有补数任务必须满足：

- 同一天可重复执行
- 重复执行不会写脏
- 失败可重跑

## 7.3 运行登记

建议建表：

- `ingestion_runs`

字段建议：

- `run_id`
- `dataset_name`
- `period`
- `trade_date`
- `status`
- `row_count`
- `symbol_count`
- `started_at`
- `finished_at`
- `source`
- `error_message`

---

## 8. 目录与索引设计

这是整个架构里最关键的一层。

没有统一索引，Parquet 文件最终也会变成另一种形式的“散乱目录”。

## 8.1 dataset catalog

建议建表：

- `dataset_catalog`

字段建议：

- `dataset_name`
- `description`
- `storage_kind`
- `retention_policy`
- `owner_module`
- `primary_keys`
- `partition_keys`
- `version`

## 8.2 dataset partitions

建议建表：

- `dataset_partitions`

字段建议：

- `dataset_name`
- `period`
- `trade_date`
- `path`
- `row_count`
- `symbol_count`
- `min_time`
- `max_time`
- `source`
- `checksum`
- `created_at`
- `freshness_status`

这样做的好处：

- 先查索引，再读数据
- Agent 和程序都能快速定位历史分区
- 便于做 freshness、缺失诊断与审计

---

## 9. 面向程序与 Agent 的读取接口

不能让 Agent 直接扫文件系统。

建议统一提供三类读取接口。

## 9.1 快摘要接口

适合 Agent 在盘中快速理解。

示例：

- `/runtime/history/behavior-profile?symbol=...`
- `/runtime/history/stock-summary?symbol=...`
- `/runtime/history/regime-memory?trade_date=...`

输出特点：

- 短
- 结构化
- 可直接进入 prompt / compose

## 9.2 研究查询接口

适合盘后研究和深度分析。

示例：

- `/runtime/history/bars`
- `/runtime/history/factor-values`
- `/runtime/history/behavior-samples`

## 9.3 检索接口

适合 Hermes / 人工 / Agent 查询历史纪要和文档。

示例：

- `/system/search/documents?q=...`
- `/system/search/discussions?q=...`

---

## 10. 推荐迁移策略

当前不建议一次性大迁移，建议三步走。

## 10.1 第一步：建立正式目录结构与 SQLite 主库

动作：

- 新建 `db/` 与 `lake/`
- 建 `control_plane.sqlite3`
- 先落 `dataset_catalog / dataset_partitions / ingestion_runs`

目标：

- 先把索引层搭起来

## 10.2 第二步：把历史 K 线和股性样本沉入 lake

动作：

- 日线全量写 Parquet
- 分钟线近端窗口写 Parquet
- 股性原始样本写 Parquet
- `stock_behavior_profiles` 结果写 SQLite

目标：

- 建立历史底座

## 10.3 第三步：把检索与 Agent 读取切到正式接口

动作：

- 为 Hermes / runtime 提供历史摘要接口
- 增加文档全文检索
- 逐步减少“直接扫 JSON 文件”的路径

目标：

- 让历史数据成为可消费能力

---

## 11. 不建议当前采用的技术

当前阶段不建议上：

- Elasticsearch / OpenSearch
- Kafka
- ClickHouse
- TimescaleDB
- MongoDB

原因：

- 运维复杂度高
- 与当前单机部署不匹配
- 对核心问题帮助不如 `SQLite + Parquet + DuckDB`

---

## 12. 验收标准

本次数据架构改造完成后，至少应满足以下标准。

### 12.1 历史底座验收

- 日线全历史可本地查询
- 分钟线近端窗口可本地查询
- 每日可增量补全
- freshness 可见

### 12.2 股性画像验收

- 每只重点股票可输出结构化股性画像
- Agent 可直接读取摘要层
- 画像刷新可追溯到原始历史数据

### 12.3 检索验收

- 人和 Agent 都能快速查：
  - 历史讨论
  - 历史审计
  - 历史复盘
  - 某只票的历史摘要

### 12.4 目录治理验收

- 任一数据集都能回答：
  - 放在哪里
  - 最新分区到哪天
  - 谁写的
  - 是否新鲜

---

## 13. 最终判断

对本项目来说，最合适的正式方案不是“只上一个数据库”，而是：

```text
SQLite 管控制面索引与事务型数据
+ Parquet + DuckDB 管历史行情与研究数据
+ SQLite FTS5 管全文检索
+ cache 只做热数据
```

这套方案的价值不在“技术名词更多”，而在于它同时满足：

- 单机正式可用
- 目录清晰
- 快速检索
- 便于增量补数
- 便于 Agent 消费
- 便于后续长期演进

