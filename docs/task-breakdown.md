# ashare-system-v2 任务拆解

> 6 Phase 路线图，细化到文件级别，含验收标准。
> 每个任务标注优先级 (P0/P1/P2) 和预估文件数。

---

## Phase 1: 基座层

> 目标：项目骨架 + 配置 + 基础设施 + 数据层 + 脚本，系统可启动。

### 1.1 项目骨架搭建

| 任务 | 说明 |
|------|------|
| 删除 v1 代码 | 清空 `src/ashare_system/` 下所有 v1 文件 (保留 `__init__.py`) |
| 删除 egg-info | 删除 `src/ashare_system.egg-info/` 和 `src/ashare_system_v2.egg-info/` |
| 创建目录结构 | 按 v2 架构创建全部子目录 + `__init__.py` |
| 更新 pyproject.toml | 入口点、依赖列表更新 |
| 创建 .gitignore | 忽略 logs/, .venv/, __pycache__/, .ashare_state/ |

**验收：** `uv sync` 成功，`import ashare_system` 不报错

### 1.2 核心配置层 (7 文件)

| 文件 | 内容 | 来源 |
|------|------|------|
| `main.py` | 入口 (简洁，仅调用 run) | 重写 |
| `run.py` | 启动器 (Uvicorn 启动 FastAPI) | 重写 |
| `app.py` | FastAPI 实例 + 路由挂载 | v1 简化 |
| `settings.py` | Pydantic BaseSettings，v2 配置项 | v1 改造 |
| `contracts.py` | 公共数据契约 (保留交易类，新增因子/情绪) | v1 扩展 |
| `container.py` | DI 容器 (适配 v2 模块) | v1 改造 |
| `scheduler.py` | 调度器骨架 (盘前/盘中/盘后时间表) | 新建 |

**验收：** `from ashare_system.settings import load_settings` 可执行

### 1.3 基础设施层 infra/ (6 文件)

| 文件 | 内容 | 来源 |
|------|------|------|
| `infra/__init__.py` | 包初始化 | 新建 |
| `infra/adapters.py` | XtQuant 执行/行情适配器 | v1 迁移 |
| `infra/xtquant_runtime.py` | QMT 运行时加载 | v1 迁移 |
| `infra/filters.py` | A股代码过滤 | v1 迁移 |
| `infra/state.py` | 状态持久化 | v1 迁移 |
| `infra/audit_store.py` | 审计日志 | v1 迁移 |
| `infra/healthcheck.py` | 健康检查 | v1 迁移 |

**验收：** `from ashare_system.infra.adapters import MockExecutionAdapter` 可执行

### 1.4 数据层 data/ (7 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `data/__init__.py` | 包初始化 | P0 |
| `data/contracts.py` | 数据层契约 (DataQuality, CleanedBar 等) | P0 |
| `data/fetcher.py` | 统一数据获取，失败返回空 | P0 |
| `data/kline.py` | K线数据获取 (日线/分钟线) | P0 |
| `data/cleaner.py` | 数据清洗 pipeline (过滤→修复→标准化) | P0 |
| `data/validator.py` | 数据完整性验证 | P0 |
| `data/cache.py` | 本地缓存管理 | P1 |

**验收：** 数据获取 + 清洗 pipeline 可运行，失败时返回空而非假数据

### 1.5 日志系统

| 文件 | 内容 |
|------|------|
| `logging_config.py` | Logger 配置，输出到 `logs/` 目录 |

**验收：** 日志文件正确写入 `logs/` 目录

### 1.6 脚本 scripts/ (4 文件)

| 文件 | 内容 |
|------|------|
| `scripts/start.sh` | 一键启动 FastAPI 服务 |
| `scripts/stop.sh` | 一键停止 |
| `scripts/run_tests.sh` | 运行测试 |
| `scripts/health_check.sh` | 健康巡检 |

**验收：** `bash scripts/start.sh` 可启动服务，`/health` 端点返回 ok

### Phase 1 总验收标准

- [ ] `uv sync` 依赖安装成功
- [ ] `bash scripts/start.sh` 启动 FastAPI 服务
- [ ] `curl localhost:8100/health` 返回 ok
- [ ] `bash scripts/run_tests.sh` 基础测试通过
- [ ] 所有文件 ≤ 300 行，每目录 ≤ 8 文件

---

## Phase 2: 因子引擎

> 目标：500+ 因子可注册、计算、验证、筛选。

### 2.1 因子框架 (6 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `factors/__init__.py` | 包初始化 | P0 |
| `factors/engine.py` | 因子批量计算引擎 (增量更新) | P0 |
| `factors/registry.py` | 因子注册表 (声明式 + 自动发现) | P0 |
| `factors/validator.py` | 因子 IC/IR 有效性验证 | P0 |
| `factors/pipeline.py` | Winsorize + Z-Score + 中性化 | P0 |
| `factors/selector.py` | Top-N 因子筛选 | P1 |

### 2.2 基础因子 base/ (5 文件, 150+ 因子)

| 文件 | 因子类别 | 数量 |
|------|----------|------|
| `base/__init__.py` | 包初始化 | — |
| `base/price_volume.py` | 量价因子 (换手率/量比/量价背离) | ~40 |
| `base/technical.py` | 技术指标 (MA/MACD/RSI/ATR/KDJ/BOLL/OBV/VWAP) | ~50 |
| `base/financial.py` | 财务比率 (PE/PB/ROE/营收增速) | ~30 |
| `base/momentum.py` | 动量反转 (N日涨幅/相对强度) | ~30 |

### 2.3 其他因子子目录 (Phase 2 骨架, Phase 4+ 填充)

| 子目录 | 文件 | 优先级 |
|--------|------|--------|
| `micro/` | `orderbook.py`, `tick_features.py` | P1 |
| `behavior/` | `herd.py`, `overreaction.py` | P1 |
| `macro/` | `indicators.py` | P1 |
| `alt/` | `sentiment.py` | P2 |
| `chain/` | `supply_demand.py` | P2 |

### Phase 2 验收标准

- [ ] 150+ 基础因子可注册并计算
- [ ] 因子 pipeline (去极值→标准化→中性化) 正确运行
- [ ] IC/IR 验证可输出因子有效性报告
- [ ] `bash scripts/compute_factors.sh` 可执行因子批量计算

---

## Phase 3: 策略 + 风控 + 回测

> 目标：选股漏斗可运行，风控拦截生效，单策略回测可执行。

### 3.1 策略层 strategy/ (8 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `strategy/__init__.py` | 包初始化 | P0 |
| `strategy/registry.py` | 策略注册表 (插件化) | P0 |
| `strategy/screener.py` | 选股漏斗 (环境→板块→因子→AI→风控) | P0 |
| `strategy/buy_decision.py` | 买入决策 (评分→排序→仓位→下单) | P0 |
| `strategy/sell_decision.py` | 卖出决策 (ATR止损+移动止损+时间止损) | P0 |
| `strategy/day_trading.py` | 日内做T | P1 |
| `strategy/position_mgr.py` | 凯利公式仓位管理 | P0 |
| `strategy/golden_combo.py` | 黄金策略组合 | P1 |

### 3.2 风控层 risk/ (5 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `risk/__init__.py` | 包初始化 | P0 |
| `risk/rules.py` | 6 大风控规则引擎 | P0 |
| `risk/guard.py` | 执行守卫 (拦截层) | P0 |
| `risk/emergency.py` | 紧急预案 (熔断/强平) | P1 |
| `risk/emotion_shield.py` | 情绪保护机制 | P1 |

### 3.3 回测层 backtest/ (7 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `backtest/__init__.py` | 包初始化 | P0 |
| `backtest/engine.py` | 回测引擎核心 (事件驱动) | P0 |
| `backtest/portfolio.py` | 组合回测 | P0 |
| `backtest/optimizer.py` | 无限迭代优化器 | P1 |
| `backtest/slippage.py` | 滑点 + 手续费模型 | P0 |
| `backtest/metrics.py` | 夏普/回撤/卡尔玛 | P0 |
| `backtest/curve.py` | 资金曲线生成 | P1 |

### Phase 3 验收标准

- [ ] 选股漏斗可从全 A 股中筛选候选
- [ ] 凯利公式仓位计算输出合理
- [ ] ATR 止损止盈正确触发
- [ ] 风控规则可拦截违规交易
- [ ] 单策略回测可输出夏普比率和最大回撤
- [ ] `bash scripts/run_backtest.sh` 可执行

---

## Phase 4: AI 模型 + 情绪周期

> 目标：XGBoost 可训练推理，情绪阶段可自动判定。

### 4.1 AI 模型 ai/ (8 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `ai/__init__.py` | 包初始化 | P0 |
| `ai/contracts.py` | 模型输入输出契约 | P0 |
| `ai/registry.py` | 模型注册表 | P0 |
| `ai/trainer.py` | 通用训练框架 | P0 |
| `ai/xgb_scorer.py` | XGBoost 选股评分 | P0 |
| `ai/tf_sequence.py` | Transformer 序列预测 | P1 |
| `ai/lstm_trend.py` | LSTM 趋势预测 | P1 |
| `ai/nlp_sentiment.py` | NLP 情感分析 | P1 |

### 4.2 情绪周期 sentiment/ (6 文件)

| 文件 | 内容 | 优先级 |
|------|------|--------|
| `sentiment/__init__.py` | 包初始化 | P0 |
| `sentiment/cycle.py` | 四阶段框架 | P0 |
| `sentiment/indicators.py` | 情绪量化指标 | P0 |
| `sentiment/turning_point.py` | 拐点识别 | P1 |
| `sentiment/position_map.py` | 周期→仓位映射 | P0 |
| `sentiment/calculator.py` | 每日情绪自动计算 | P0 |

### Phase 4 验收标准

- [ ] XGBoost 模型可训练，AUC > 0.80
- [ ] 模型注册表可管理多个模型版本
- [ ] 情绪阶段判定输出正确 (冰点/回暖/主升/高潮)
- [ ] 情绪→仓位映射生效
- [ ] `bash scripts/train_models.sh` 可执行

---

## Phase 5: 监控 + 通信 + 报告 + API + 调度

> 目标：全流程可运行，飞书推送，API 可访问。

### 5.1 监控 monitor/ (8 文件)

| 文件 | 优先级 |
|------|--------|
| `monitor/__init__.py` | P0 |
| `monitor/alert_engine.py` | P0 |
| `monitor/market_watcher.py` | P0 |
| `monitor/dragon_tiger.py` | P1 |
| `monitor/limit_analyzer.py` | P1 |
| `monitor/hot_money.py` | P2 |
| `monitor/stock_pool.py` | P0 |
| `monitor/dashboard.py` | P2 |

### 5.2 通信 notify/ (4 文件)

| 文件 | 优先级 |
|------|--------|
| `notify/__init__.py` | P0 |
| `notify/feishu.py` | P0 |
| `notify/dispatcher.py` | P0 |
| `notify/templates.py` | P1 |

### 5.3 报告 report/ (5 文件)

| 文件 | 优先级 |
|------|--------|
| `report/__init__.py` | P0 |
| `report/daily.py` | P0 |
| `report/realtime.py` | P1 |
| `report/strategy_report.py` | P1 |
| `report/generator.py` | P0 |

### 5.4 API 路由 apps/ (7 文件)

| 文件 | 优先级 |
|------|--------|
| `apps/__init__.py` | P0 |
| `apps/execution_api.py` | P0 |
| `apps/market_api.py` | P0 |
| `apps/strategy_api.py` | P1 |
| `apps/monitor_api.py` | P1 |
| `apps/report_api.py` | P1 |
| `apps/system_api.py` | P0 |

### 5.5 调度器完善

- `scheduler.py` 填充盘前/盘中/盘后完整时间表

### Phase 5 验收标准

- [ ] 飞书 Webhook 推送成功
- [ ] 所有 API 端点可访问
- [ ] 调度器按时间表触发任务
- [ ] 日终复盘报告可生成
- [ ] 预选股池每日刷新

---

## Phase 6: 进阶 AI + 学习系统

> 目标：多模型集成，策略自进化闭环。

### 6.1 进阶 AI ai_advanced/ (5 文件)

| 文件 | 优先级 |
|------|--------|
| `ai_advanced/__init__.py` | P2 |
| `ai_advanced/hft_signal.py` | P2 |
| `ai_advanced/ppo_position.py` | P2 |
| `ai_advanced/ensemble.py` | P0 |
| `ai_advanced/model_eval.py` | P1 |

### 6.2 学习系统 learning/ (4 文件)

| 文件 | 优先级 |
|------|--------|
| `learning/__init__.py` | P1 |
| `learning/continuous.py` | P1 |
| `learning/self_evolve.py` | P2 |
| `learning/trade_review.py` | P2 |

### Phase 6 验收标准

- [ ] 多模型 Stacking 集成可运行
- [ ] 模型评估 pipeline 输出 AUC/IC
- [ ] 自进化闭环: reviewer→brain/data/risk 反馈链路通畅
- [ ] 持续学习框架可增量训练

---

## 文件统计

| Phase | 新建文件数 | P0 文件 | P1 文件 | P2 文件 |
|-------|-----------|---------|---------|---------|
| 1 | ~25 | 20 | 5 | 0 |
| 2 | ~20 | 12 | 5 | 3 |
| 3 | ~20 | 14 | 6 | 0 |
| 4 | ~14 | 10 | 4 | 0 |
| 5 | ~24 | 14 | 7 | 3 |
| 6 | ~9 | 1 | 4 | 4 |
| **合计** | **~112** | **71** | **31** | **10** |
