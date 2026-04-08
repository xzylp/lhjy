# ashare-system-v2 项目结构设计

> 基于 v1 骨架，按十二大子系统重新组织目录。
> 硬性约束: Python 文件 ≤ 300 行，每层文件夹 ≤ 8 个文件。

---

## 顶层目录

```
ashare-system-v2/
├── src/
│   └── ashare_system/        # 主包
├── tests/                    # 测试
├── scripts/                  # 运维脚本 (.sh)
├── docs/                     # 正式文档
├── discuss/                  # 讨论/评审文档
├── logs/                     # 日志输出 (gitignore)
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 核心源码结构 (src/ashare_system/)

```
ashare_system/
│
├── main.py                   # 入口 (简洁)
├── run.py                    # 启动器
├── app.py                    # FastAPI 实例
├── settings.py               # 全局配置
├── container.py              # DI 容器
├── contracts.py              # 公共数据契约 (Pydantic)
├── scheduler.py              # 全局调度器
│
├── ai/                       # [2] AI 模型体系
│   ├── __init__.py
│   ├── contracts.py          # 模型输入输出契约
│   ├── registry.py           # 模型注册表 (统一管理)
│   ├── trainer.py            # 通用训练框架
│   ├── xgb_scorer.py         # XGBoost 选股评分
│   ├── tf_sequence.py        # Transformer 序列预测
│   ├── lstm_trend.py         # LSTM 趋势预测 (Attention)
│   └── nlp_sentiment.py      # NLP 新闻情感分析
│
├── ai_advanced/              # [2] AI 进阶模型 (Phase 6)
│   ├── __init__.py
│   ├── hft_signal.py         # 高频盘口信号模型
│   ├── ppo_position.py       # PPO 强化学习仓位优化
│   ├── ensemble.py           # 多模型 Stacking 集成
│   └── model_eval.py         # 模型评估 pipeline (AUC/IC)
│
├── factors/                  # [3] 因子引擎体系
│   ├── __init__.py
│   ├── engine.py             # 因子批量计算引擎
│   ├── registry.py           # 因子注册表 (声明式)
│   ├── validator.py          # 因子 IC/IR 有效性验证
│   ├── pipeline.py           # 去极值/标准化/中性化
│   ├── selector.py           # Top-N 因子筛选
│   ├── base/                 # 基础因子 (量价/技术/财务)
│   │   ├── __init__.py
│   │   ├── price_volume.py   # 量价因子
│   │   ├── technical.py      # 技术指标因子
│   │   ├── financial.py      # 财务比率因子
│   │   └── momentum.py       # 动量反转因子
│   ├── micro/                # 微观结构因子
│   │   ├── __init__.py
│   │   ├── orderbook.py      # 盘口深度因子
│   │   └── tick_features.py  # Tick 级特征因子
│   ├── behavior/             # 行为金融因子
│   │   ├── __init__.py
│   │   ├── herd.py           # 羊群效应因子
│   │   └── overreaction.py   # 过度反应因子
│   ├── macro/                # 宏观因子
│   │   ├── __init__.py
│   │   └── indicators.py     # 利率/汇率/PMI/社融
│   ├── alt/                  # 另类数据因子
│   │   ├── __init__.py
│   │   └── sentiment.py      # 舆情/搜索热度
│   └── chain/                # 产业链因子
│       ├── __init__.py
│       └── supply_demand.py  # 上下游供需因子
│
├── monitor/                  # [4] 实时监控系统
│   ├── __init__.py
│   ├── alert_engine.py       # 即时预警引擎
│   ├── market_watcher.py     # 全天盯盘服务
│   ├── dragon_tiger.py       # 龙虎榜分析器
│   ├── limit_analyzer.py     # 涨停数据分析
│   ├── hot_money.py          # 游资战法识别
│   ├── stock_pool.py         # 预选股池生成
│   └── dashboard.py          # 监控仪表盘
│
├── sentiment/                # [5] 情绪周期系统
│   ├── __init__.py
│   ├── cycle.py              # 四阶段框架 (冰点/回暖/主升/高潮)
│   ├── indicators.py         # 情绪量化指标
│   ├── turning_point.py      # 情绪拐点识别
│   ├── position_map.py       # 周期 → 仓位映射
│   └── calculator.py         # 每日情绪自动计算
│
├── data/                     # [6] 数据系统
│   ├── __init__.py
│   ├── fetcher.py            # 统一数据获取 (AkShare)
│   ├── kline.py              # K线数据 (日线/分钟线)
│   ├── special.py            # 涨跌停/龙虎榜/资金流向
│   ├── cleaner.py            # 数据清洗 pipeline
│   ├── repair.py             # 数据修复
│   ├── validator.py          # 数据完整性验证
│   └── cache.py              # 本地缓存管理
│
├── backtest/                 # [7] 回测系统
│   ├── __init__.py
│   ├── engine.py             # 回测引擎核心
│   ├── portfolio.py          # 组合回测
│   ├── optimizer.py          # 无限迭代优化器
│   ├── slippage.py           # 滑点 + 手续费模型
│   ├── metrics.py            # 风险指标 (夏普/回撤/卡尔玛)
│   └── curve.py              # 资金曲线生成
│
├── strategy/                 # [8] 策略 + 交易系统
│   ├── __init__.py
│   ├── registry.py           # 策略注册表 (插件化)
│   ├── screener.py           # 选股漏斗 (v1 升级)
│   ├── buy_decision.py       # 买入决策引擎
│   ├── sell_decision.py      # 卖出决策引擎
│   ├── day_trading.py        # 日内做T系统
│   ├── position_mgr.py      # 仓位管理 (凯利公式)
│   └── golden_combo.py       # 黄金策略组合
│
├── risk/                     # [9] 风控体系
│   ├── __init__.py
│   ├── rules.py              # 6大风控规则引擎
│   ├── guard.py              # 执行守卫 (拦截层)
│   ├── emergency.py          # 紧急预案 (熔断/强平)
│   └── emotion_shield.py     # 情绪保护机制
│
├── report/                   # [10] 报告系统
│   ├── __init__.py
│   ├── daily.py              # 日终复盘报告
│   ├── realtime.py           # 实时交易报告
│   ├── strategy_report.py    # 策略优化报告
│   └── generator.py          # 报告模板引擎
│
├── notify/                   # [11] 通信系统
│   ├── __init__.py
│   ├── feishu.py             # 飞书 Webhook 推送
│   ├── dispatcher.py         # 消息分发 (路由+限流)
│   └── templates.py          # 消息模板
│
├── learning/                 # [12] 学习系统
│   ├── __init__.py
│   ├── continuous.py         # 持续学习框架
│   ├── self_evolve.py        # 策略自进化
│   └── trade_review.py       # 交易复盘分析
│
├── apps/                     # API 路由层 (v1 保留)
│   ├── __init__.py
│   ├── execution_api.py      # 交易执行 API
│   ├── market_api.py         # 行情数据 API
│   ├── strategy_api.py       # 策略管理 API
│   ├── monitor_api.py        # 监控查询 API
│   ├── report_api.py         # 报告查询 API
│   └── system_api.py         # 系统管理 API
│
└── infra/                    # 基础设施层 (v1 迁移)
    ├── __init__.py
    ├── state.py              # 状态持久化
    ├── audit_store.py        # 审计日志
    ├── adapters.py           # XtQuant 适配器
    ├── healthcheck.py        # 健康检查
    └── xtquant_runtime.py    # QMT 运行时
```

---

## 测试目录结构

```
tests/
├── test_ai/
│   ├── test_xgb_scorer.py
│   ├── test_ensemble.py
│   └── test_model_registry.py
├── test_factors/
│   ├── test_engine.py
│   ├── test_pipeline.py
│   └── test_base_factors.py
├── test_backtest/
│   ├── test_engine.py
│   └── test_metrics.py
├── test_strategy/
│   ├── test_screener.py
│   ├── test_buy_decision.py
│   └── test_position_mgr.py
├── test_risk/
│   └── test_rules.py
├── test_data/
│   ├── test_fetcher.py
│   └── test_cleaner.py
├── test_sentiment/
│   └── test_cycle.py
├── test_notify/
│   └── test_feishu.py
└── test_apps/
    ├── test_execution_api.py
    └── test_market_api.py
```

---

## 脚本目录

```
scripts/
├── start.sh                  # 一键启动全栈
├── stop.sh                   # 一键停止
├── train_models.sh           # AI 模型训练
├── run_backtest.sh           # 执行回测
├── compute_factors.sh        # 因子批量计算
├── run_tests.sh              # 运行测试
├── daily_pipeline.sh         # 盘后日常 pipeline
└── health_check.sh           # 健康巡检
```

---

## v1 → v2 文件映射 (迁移指引)

| v1 文件 | v2 去向 | 说明 |
|---------|---------|------|
| `screener.py` | `strategy/screener.py` | 升级为可插拔漏斗 |
| `scoring.py` | `ai/xgb_scorer.py` | 规则评分 → ML 评分 |
| `risk.py` | `risk/rules.py` | 拆分为规则 + 守卫 + 应急 |
| `execution_guard.py` | `risk/guard.py` | 迁移 |
| `runtime_engine.py` | `strategy/buy_decision.py` | 评分决策拆分为买/卖 |
| `indicators.py` | `factors/base/technical.py` | 扩展为因子引擎 |
| `research_service.py` | `data/fetcher.py` | 数据获取统一化 |
| `scheduler.py` | `scheduler.py` (根级) | 保留，扩展调度任务 |
| `state.py` | `infra/state.py` | 迁移到基础设施层 |
| `adapters.py` | `infra/adapters.py` | 迁移到基础设施层 |
| `audit_store.py` | `infra/audit_store.py` | 迁移到基础设施层 |
| `apps/*.py` | `apps/*.py` | 重命名，扩展新路由 |
| `settings.py` | `settings.py` (根级) | 保留 |
| `container.py` | `container.py` (根级) | 保留，扩展注入项 |
| `contracts.py` | `contracts.py` (根级) | 保留，扩展数据契约 |

### 可移除的 v1 文件 (v2 不再需要)

| 文件 | 原因 |
|------|------|
| `openclaw_bundle.py` | v2 不再依赖 OpenClaw 协议 |
| `openclaw_consistency.py` | 同上 |
| `decision_protocol.py` | 由 `strategy/buy_decision.py` 替代 |
| `leader_selector.py` | Agent 投票机制由 AI 集成替代 |
| `theme_engine.py` | 终端主题，非核心 |
| `layout.py` | 终端布局，非核心 |
| `paper_validation_*.py` | 验证 CLI，由回测系统替代 |
| `deployment_smoke.py` | 由 `scripts/health_check.sh` 替代 |
| `recovery_drill*.py` | 可精简合并到 `infra/` |

---

## 依赖规则 (模块间依赖方向)

```
apps/ ──→ strategy/ ──→ ai/
  │          │            │
  │          ▼            ▼
  │       risk/ ←── sentiment/
  │          │
  ▼          ▼
infra/ ←── data/ ←── factors/
  │
  ▼
notify/ ←── report/ ←── backtest/
```

**核心原则:**
- `data/` 和 `infra/` 是最底层，不依赖业务模块
- `ai/`、`factors/`、`sentiment/` 是中间层，只依赖 `data/`
- `strategy/`、`risk/` 是业务层，编排中间层
- `apps/` 是最外层 API，调用业务层
- `notify/` 和 `report/` 是输出层，任何层可调用
- **禁止循环依赖**
