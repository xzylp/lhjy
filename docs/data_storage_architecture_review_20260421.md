# 数据存储架构方案审核意见

> 审核对象：[data_storage_architecture_plan_20260421.md](data_storage_architecture_plan_20260421.md)
> 审核时间：2026-04-21
> 审核结论：**方向正确，技术选型合理，但落地路径需要补充关键细节才能避免"搬家搬一半"的风险。**

---

## 一、总体评价

### ✅ 做对的部分

1. **诊断准确**。JSON 状态文件不适合长期数据资产，这是当前系统最大的数据瓶颈。`StateStore` 的 `fcntl` 文件锁 + 全量 `json.loads` / `json.dumps` 模式在数据量增长后会成为性能杀手。
2. **技术选型克制**。没有上 ClickHouse / Kafka / ES，选 `SQLite + Parquet + DuckDB` 是对单机量化系统的正确判断。
3. **三层数据架构（原始→特征→摘要）** 逻辑清晰，尤其"Agent 优先读摘要层"是对 compose 链效率的正确优化。
4. **增量补全 + 幂等设计** 从一开始就提出来，避免了后续补数逻辑的反复重做。
5. **分层目录** 把 `db/lake/state/cache/serving` 职责划清，比现在 `storage_root` 下平铺好得多。

### ⚠️ 需要补强的部分

方案覆盖了"应该怎么组织数据"，但对以下关键问题缺少回答：

---

## 二、关键问题逐项分析

### 问题 1：迁移期间的双写与回退机制缺失

方案说"不建议一次性大迁移，建议三步走"，但三步之间缺少**双写期**设计：

- 第一步建完 SQLite 主库后，`StateStore` JSON 和 SQLite 之间怎么共存？
- 如果 SQLite 出问题，能不能一键退回到 JSON？
- 讨论/执行主链的数据是"搬到 SQLite 后删 JSON"还是"双写一段时间"？

**建议**：增加明确的双写策略：

```text
阶段 A：只写 JSON（现状）
阶段 B：写 JSON + 写 SQLite，读仍走 JSON（双写验证期，≥ 5 个交易日）
阶段 C：写 JSON + 写 SQLite，读切 SQLite（灰度切换期）
阶段 D：只写 SQLite，JSON 归档不再更新（正式迁移完成）
```

每个阶段需要一个**一键回退开关**（环境变量即可），不需要重启就能切回。

---

### 问题 2：SQLite 并发写入在当前架构下是否安全

当前系统是单进程 FastAPI（uvicorn），但 `scheduler.py` 的盘中任务可能产生并发写入：

- 持仓巡视任务写 `execution_traces`
- 机会扫描任务写 `discussion_traces`
- 对账任务写 `gateway_receipts`
- 盘中快退审批写 `agent_actions`

SQLite 在 WAL 模式下支持 1 writer + N readers，但多 writer 会互相阻塞。

**建议**：

1. 明确要求 SQLite 开启 WAL 模式（`PRAGMA journal_mode=WAL`）
2. 所有写操作走统一的 `ControlPlaneDB` 单例，内部用写锁串行化
3. 方案中增加一节"并发安全设计"，说明写入模式

---

### 问题 3：Parquet + DuckDB 的依赖引入成本被低估

当前 `pyproject.toml` 的依赖中**没有 `pyarrow` 和 `duckdb`**。引入它们意味着：

- `pyarrow` 安装包较大（~200MB），在部分 Linux 环境编译耗时
- `duckdb` 与 `pandas` 的版本兼容性需要测试
- 回测引擎 `backtest/engine.py` 已经用了 `pandas`，但生产链路的 `scheduler.py` 中很多地方直接用 dict，引入 Parquet 会在数据格式之间产生转换成本

**建议**：

1. 在方案中增加"依赖影响评估"一节
2. 明确 `pyarrow` 和 `duckdb` 只在盘后任务和研究查询中使用，不进入盘中热路径
3. 盘中热路径（巡视、扫描）继续用内存 dict / cache，不直接读 Parquet

---

### 问题 4：现有 `StateStore` 的哪些 key 迁移、哪些保留，缺少具体清单

方案说"state/ 只存运行态快照，不再继续承载长期资产"，但没有列出具体哪些 key 属于"长期资产"应该迁走。

从 `container.py` 和代码审查来看，当前有以下 StateStore 文件：

| 文件 | 现有 key 举例 | 属性 |
|------|-------------|------|
| `runtime_state.json` | `latest_runtime_report`, `latest_runtime_context` | 运行态 ✅ 保留 |
| `research_state.json` | `latest_dossier_pack`, `stock_behavior_profile_history`, `news`, `announcements` | **混合**：profile history 和 news 应迁走 |
| `discussion_state.json` | 讨论相关状态 | 运行态 ✅ 保留 |
| `execution_gateway_state.json` | intent/receipt | **混合**：历史 receipt 应迁走 |
| `position_watch_state.json` | 巡视运行态 | 运行态 ✅ 保留 |
| `monitor_state.json` | 监控运行态 | 运行态 ✅ 保留 |
| `candidate_cases.json` | 讨论候选案例全量 | **长期资产**：应迁走 |
| `discussion_cycles.json` | 讨论轮次记录 | **长期资产**：应迁走 |
| `agent_score_states.json` | Agent 治理分数 | **长期资产**：应迁走 |
| `param_change_events.json` | 参数变更事件流 | **长期资产**：应迁走 |

**建议**：在方案中补充"迁移清单"章节，逐文件列出 `保留 / 迁移 / 双写` 策略。

---

### 问题 5：`stock_behavior_profile_history` 的膨胀问题需要在迁移前解决

从 `precompute.py` L567-570 看，每次画像刷新都 `append` 到 `stock_behavior_profile_history`，只按交易日数做粗粒度保留。如果每天 30 只候选票、保留 20 天，这个 JSON 数组会达到 600 条 × 每条含完整 profile dict ≈ 数 MB。

**当前的 `StateStore._save()` 是全量 `json.dumps` + 全量写入**，这意味着每次保存 `research_state.json` 都要序列化这个庞然大物。

**建议**：

1. 这是迁移到 SQLite/Parquet 的最优先候选——`stock_behavior_profiles` 表 + `behavior_samples` Parquet
2. 迁移后 `research_state.json` 中只保留 `latest_stock_behavior_profiles`（最新快照），history 全部进 SQLite

---

### 问题 6：FTS5 全文检索的 schema 和写入时机未定义

方案提到用 SQLite FTS5 检索讨论纪要、审计复盘等文本，但未说明：

- 讨论纪要从哪个字段提取（`CandidateOpinion.reasons`? `thesis`? `key_evidence`?）
- 写入时机是实时跟随讨论流程还是盘后批量归档
- 检索时的分词策略（中文需要 `jieba` 或 `simple` tokenizer）

**建议**：

1. FTS5 的中文支持需要外部 tokenizer，建议评估 `simple` tokenizer + 空格分词预处理 vs `jieba` 分词
2. 写入时机建议跟随 `finalize_cycle()`，在讨论定稿时一次性写入 FTS 索引
3. 先确定最小可用字段集，不要试图索引所有文本

---

### 问题 7：serving 层与 lake 层的关系不清晰

当前 `serving/` 下有 `latest_stock_behavior_profiles.json`（见 `precompute.py` L574），方案中 serving 层仍然保留但说"只负责快读"。

问题是：`serving` 和 `cache` 的边界在哪里？

- `serving/latest_stock_behavior_profiles.json` 是 cache 还是 serving？
- `lake/behavior_samples/` 写入后，`serving/` 的内容从哪里来？是 lake 的投影还是独立生成的？

**建议**：

1. `serving/` 定义为"对外 API 直接读取的预计算产物"
2. `cache/` 定义为"盘中热数据的临时加速层，随时可丢弃重建"
3. `serving/` 的数据来源必须可追溯到 `lake/` 或 `db/`，不能有只存在于 serving 的唯一数据

---

## 三、缺少但应该补充的内容

### 3.1 备份策略

SQLite 作为控制面主库，一旦损坏就是全系统状态丢失。方案中应增加：

- 每日盘后自动备份 `control_plane.sqlite3`（`sqlite3 .backup` 命令）
- 保留最近 7 天备份
- Parquet 文件天然按 trade_date 分区，本身就是增量备份结构

### 3.2 容量规划

方案应给出粗略的容量预估：

| 数据集 | 单日增量 | 1 年总量 | 格式 |
|--------|---------|---------|------|
| `bars_1d` 全市场（~5000 只） | ~1 MB Parquet | ~250 MB | Parquet |
| `bars_5m` 全市场 | ~50 MB Parquet | ~12 GB | Parquet |
| `bars_1m` 全市场 | ~250 MB Parquet | ~60 GB | Parquet |
| `control_plane.sqlite3` | ~100 KB | ~25 MB | SQLite |
| `behavior_samples` | ~500 KB | ~120 MB | Parquet |

这些数字直接决定了保留策略的合理性——1m 全市场 60GB/年 在单机上是否可接受？

### 3.3 降级方案

如果 DuckDB/Parquet 在某些环境无法安装（如 Windows QMT 侧、嵌入式环境），系统应该能降级到"只用 SQLite + JSON"运行。方案应明确：

- DuckDB 是可选依赖（`extras_require`），不是强制依赖
- 研究查询接口在无 DuckDB 时返回 `501 Not Implemented` 或 fallback 到 SQLite 查询

---

## 四、推荐修改后的迁移路线

```text
第一步（本周）：基础设施准备
  ├── 建 db/control_plane.sqlite3（WAL 模式）
  ├── 建 dataset_catalog / dataset_partitions / ingestion_runs 表
  ├── 建 ControlPlaneDB 单例（写锁串行化）
  ├── 不改任何现有读写路径
  └── 验收：SQLite 正常创建 + 读写测试通过

第二步（下周）：最痛点数据迁移
  ├── stock_behavior_profiles → SQLite 表
  ├── stock_behavior_profile_history → SQLite 表（按 trade_date 索引）
  ├── candidate_cases → SQLite 表
  ├── discussion_cycles → SQLite 表
  ├── agent_score_states → SQLite 表
  ├── 双写期：JSON 继续写，SQLite 同步写，读仍走 JSON
  └── 验收：双写 5 天后对比 JSON 和 SQLite 数据一致性

第三步（第三周）：切换读路径
  ├── 上述数据的读路径切到 SQLite
  ├── JSON 文件保留但不再被主链读取
  ├── 增加一键回退开关
  └── 验收：切换后 API 响应时间无劣化、数据正确

第四步（后续）：历史行情 lake 化
  ├── 加 pyarrow / duckdb 为可选依赖
  ├── 日线 Parquet 写入 + 盘后增量任务
  ├── 分钟线按保留策略写入
  ├── 研究查询接口上线
  └── 验收：DuckDB 可查日线全历史

第五步（后续）：检索与 Agent 消费
  ├── FTS5 索引建设（讨论纪要 + 审计 + 复盘）
  ├── 摘要层 API 上线
  └── compose 链接入历史摘要
```

---

## 五、审核结论

| 项目 | 评价 |
|------|------|
| 技术选型 | ✅ 合理，`SQLite + Parquet + DuckDB` 是单机量化最优组合 |
| 分层架构 | ✅ 清晰，`db/lake/state/cache/serving` 职责划分正确 |
| 三层数据模型 | ✅ 原始→特征→摘要的分层对 Agent 消费友好 |
| 迁移策略 | ⚠️ 缺双写期和回退机制，需补充 |
| 并发安全 | ⚠️ 未讨论 SQLite 写入模式，需补充 WAL + 写锁设计 |
| 依赖管理 | ⚠️ pyarrow/duckdb 未在 pyproject.toml 中声明，需评估引入成本 |
| 迁移清单 | ❌ 缺少逐文件的 保留/迁移 清单 |
| 容量规划 | ❌ 缺少容量预估，1m 全市场可能不适合无条件全量存 |
| 备份策略 | ❌ 未提及 |
| 降级方案 | ❌ 未提及 |
| FTS5 细节 | ⚠️ 中文分词、写入时机、schema 未定义 |

**最终建议**：方案可以批准启动第一步（建 SQLite 主库 + 目录），但在启动第二步（数据迁移）前，必须补充**双写策略、迁移清单、并发安全设计**三份补充文档。
