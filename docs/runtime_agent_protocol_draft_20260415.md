# Agent -> Runtime 参数化调用协议草案

> 日期：2026-04-15
> 状态：Draft v1
> 目标：在不推倒现有主程序骨架的前提下，把 runtime 从“固定流水线选股器”升级为“可由 Agent 组织参数并调用的策略工具”

---

## 一、协议定位

本协议解决两个核心问题：

1. Agent 如何把自己的市场判断、策略意图、参数组合、风险边界，结构化地表达给 runtime。
2. runtime 如何把候选、证据、过滤原因、评分拆解、执行建议，结构化地返回给 Agent。

协议主张：

- 程序继续做底座和工具。
- Agent 负责提出交易假设和参数组织。
- runtime 不再默认替 Agent 决策，而是负责计算、筛选、解释和回传证据。

---

## 二、设计原则

### 1. 向后兼容

第一版协议不能直接推翻现有 `POST /runtime/jobs/pipeline`。

落地策略应为：

- 保留现有 `RuntimeJobRequest`
- 新增 Agent 专用请求模型
- 在 runtime 内做兼容适配
- 允许旧入口继续跑默认流水线

### 2. 先表达意图，再表达参数

Agent 发起 runtime 调用时，不应只传“给我跑一下”。

必须尽量带上：

- 当前市场判断
- 目标机会类型
- 本次策略意图
- 采用的因子和战法
- 约束边界
- 输出偏好

### 3. 结果必须可解释

runtime 返回的不只是候选名单，而应回答：

- 为什么这些票入选
- 为什么那些票被过滤
- 本次战法是否适配当前市场
- 哪些因子贡献最大
- 哪些风险项需要进一步质询

### 4. 先工具化，再进化

第一版协议优先保证：

- 能表达
- 能执行
- 能解释
- 能记录

后续再逐步加：

- 微回测
- 多战法对比
- 自适应权重建议
- 成败归因反馈

---

## 三、调用总览

建议引入新的 Agent 专用入口：

- `POST /runtime/jobs/compose`

并保留现有入口：

- `POST /runtime/jobs/pipeline`
- `POST /runtime/jobs/intraday`
- `POST /runtime/jobs/autotrade`

其中：

- `pipeline` 继续服务默认流水线和兼容旧调用
- `compose` 负责承接 Agent 参数化调用

---

## 四、请求协议

## 4.1 顶层结构

```json
{
  "request_id": "agent-runtime-20260415-001",
  "agent": {
    "agent_id": "ashare-strategy",
    "role": "strategy",
    "session_id": "cycle-20260415-round1",
    "proposal_id": "proposal-heat-rotation-001"
  },
  "intent": {
    "mode": "opportunity_scan",
    "objective": "寻找盘中最强方向中的首板后转强与趋势加速机会",
    "market_hypothesis": "指数震荡偏强，机器人与电力设备板块热度抬升，适合做板块共振+趋势加速",
    "trade_horizon": "intraday_to_overnight"
  },
  "universe": {
    "scope": "a-share",
    "symbol_pool": [],
    "sector_whitelist": ["机器人", "电力设备"],
    "sector_blacklist": ["银行"],
    "source": "full_market"
  },
  "strategy": {
    "playbooks": [
      {
        "id": "sector_resonance",
        "weight": 0.35,
        "params": {
          "min_sector_heat": 0.72,
          "min_leader_strength": 0.65
        }
      },
      {
        "id": "trend_acceleration",
        "weight": 0.40,
        "params": {
          "min_breakout_pct": 0.025,
          "max_pullback_pct": 0.04
        }
      },
      {
        "id": "weak_to_strong_intraday",
        "weight": 0.25,
        "params": {
          "min_turnover_ratio": 1.8,
          "min_volume_ratio": 1.5
        }
      }
    ],
    "factors": [
      {
        "id": "momentum_slope",
        "group": "momentum",
        "weight": 0.18,
        "params": {
          "window": 20
        }
      },
      {
        "id": "main_fund_inflow",
        "group": "capital_flow",
        "weight": 0.15,
        "params": {
          "window": 5
        }
      },
      {
        "id": "sector_heat_score",
        "group": "sentiment",
        "weight": 0.20,
        "params": {}
      },
      {
        "id": "breakout_quality",
        "group": "technical",
        "weight": 0.22,
        "params": {
          "window": 30
        }
      },
      {
        "id": "news_catalyst_score",
        "group": "event",
        "weight": 0.10,
        "params": {
          "lookback_hours": 12
        }
      },
      {
        "id": "liquidity_risk_penalty",
        "group": "risk",
        "weight": -0.15,
        "params": {}
      }
    ],
    "ranking": {
      "primary_score": "composite_score",
      "secondary_keys": ["sector_heat_score", "main_fund_inflow", "breakout_quality"]
    }
  },
  "constraints": {
    "hard_filters": {
      "exclude_st": true,
      "exclude_halt": true,
      "exclude_limit_up_unbuyable": true,
      "exclude_limit_down": true
    },
    "user_preferences": {
      "excluded_theme_keywords": ["银行"],
      "max_single_amount": 20000,
      "equity_position_limit": 0.3
    },
    "position_rules": {
      "allow_replace_position": true,
      "allow_intraday_t": true,
      "allow_overnight": true
    },
    "risk_rules": {
      "daily_loss_limit": 0.05,
      "max_drawdown_tolerance": 0.03
    }
  },
  "market_context": {
    "session_phase": "intraday",
    "market_regime": "range_to_risk_on",
    "focus_topics": ["机器人", "电力设备", "电网"],
    "holding_symbols": ["600166.SH"],
    "cash_available": 30308.56
  },
  "output": {
    "max_candidates": 12,
    "include_filtered_reasons": true,
    "include_score_breakdown": true,
    "include_evidence": true,
    "include_counter_evidence": true,
    "return_mode": "proposal_ready"
  }
}
```

---

## 4.2 字段定义

### agent

标识谁发起了本次调用，便于：

- 审计
- 监督
- 归因
- 复盘

关键字段：

- `agent_id`
- `role`
- `session_id`
- `proposal_id`

### intent

表达这次 runtime 调用想解决什么问题，而不是只表达“跑一下”。

建议支持的 `mode`：

- `opportunity_scan`：全市场机会扫描
- `theme_scan`：主题 / 板块扫描
- `holding_recheck`：持仓复核
- `replacement_scan`：替换仓扫描
- `intraday_t_scan`：持仓日内 T 机会扫描
- `overnight_scan`：隔夜候选扫描
- `counter_evidence_scan`：针对既有提案的反证扫描

### universe

表达本次扫描范围。

建议支持：

- `scope`: `main-board | a-share | custom | holdings | watchlist`
- `symbol_pool`
- `sector_whitelist`
- `sector_blacklist`
- `source`

### strategy

这是协议核心，表达 Agent 组织好的“战法 + 因子 + 排序逻辑”。

其中：

- `playbooks` 对应战法插件层
- `factors` 对应因子仓库
- `ranking` 对应结果排序规则

这里不应只支持“调用已有插件”，还应支持“引用仓库版本”和“提交学习产物”两类扩展场景：

- 调用场景：Agent 指定 `playbook_id/factor_id + version`
- 学习场景：Agent 提交 `learned_template`、`learned_combo` 或新的权重模板进入仓库候选区

也就是说，runtime 的策略层最终要支持：

- 从正式仓库读取
- 从实验仓库读取
- 从学习仓库读取候选模板
- 把新学习成果写回仓库等待评估

### constraints

表达程序电子围栏与用户偏好的组合约束。

这里必须和战法解耦，不能把用户偏好写死进战法。

### market_context

表达 Agent 当前对市场的理解，以及当前账户和持仓背景。

其作用是让 runtime 不只是机械算分，而是结合使用场景输出。

### output

控制返回力度。

不同使用场景的返回深度不同：

- 快速盘中扫描
- 提案前证据汇总
- 盘后复盘记录

### learned_asset_options

这是 `compose` 协议里专门给 Agent 的“学习产物消费开关”。

原则不是“仓库里有 active 就全部吃进去”，而是：

- Agent 先判断当前市场假设、主题方向、战法组合是否真的贴合某类历史学习产物
- 只有确认“值得借用既有学习成果”时，才开启自动吸附
- 自动吸附始终是显式开关，不是默认隐式行为

建议字段：

- `auto_apply_active`
- `max_auto_apply`
- `preferred_tags`
- `blocked_asset_ids`

推荐语义：

- `auto_apply_active=false`
  仅消费本次请求里显式写入的 learned asset
- `auto_apply_active=true`
  允许 runtime 在 `active` 资产中按匹配度自动挑选少量资产参与本次 compose
- `max_auto_apply`
  控制最多吸附几个，避免一轮 compose 同时叠太多历史偏置
- `preferred_tags`
  用于声明本轮更偏向哪类学习产物，例如 `trend`、`rotation`、`intraday-t`
- `blocked_asset_ids`
  用于明确排除已过期、已失效或本轮不想引用的资产

使用纪律：

- `draft/review_required` 资产只允许注册、评审、回看，不得进入主链加权
- `active` 资产才允许显式引用或自动吸附
- 自动吸附开启后，Agent 必须在讨论里解释：
  - 为什么现在开启
  - 吸附了哪些资产
  - 这些资产改变了哪些排序或偏置
  - 是否引入了新的风险或过拟合嫌疑

应允许按需裁剪。

---

## 五、返回协议

## 5.1 顶层结构

```json
{
  "job_id": "runtime-a1b2c3d4e5",
  "status": "completed",
  "request_id": "agent-runtime-20260415-001",
  "generated_at": "2026-04-15T10:28:00+08:00",
  "agent": {
    "agent_id": "ashare-strategy",
    "role": "strategy"
  },
  "intent": {
    "mode": "opportunity_scan",
    "objective": "寻找盘中最强方向中的首板后转强与趋势加速机会"
  },
  "market_summary": {
    "market_regime": "range_to_risk_on",
    "hot_sectors": ["机器人", "电力设备"],
    "risk_flags": ["指数未放量突破"]
  },
  "candidates": [
    {
      "symbol": "002882.SZ",
      "name": "金龙羽",
      "rank": 1,
      "action_hint": "BUY_CANDIDATE",
      "composite_score": 78.6,
      "playbook_fit": {
        "sector_resonance": 0.81,
        "trend_acceleration": 0.75,
        "weak_to_strong_intraday": 0.68
      },
      "factor_scores": {
        "momentum_slope": 0.72,
        "main_fund_inflow": 0.69,
        "sector_heat_score": 0.88,
        "breakout_quality": 0.77,
        "news_catalyst_score": 0.41,
        "liquidity_risk_penalty": -0.09
      },
      "evidence": [
        "所属主题热度处于当日高位",
        "量价结构显示放量突破",
        "资金因子与技术因子同步转强"
      ],
      "counter_evidence": [
        "事件催化强度一般",
        "若板块热度回落则强度持续性存疑"
      ],
      "risk_flags": [
        "波动偏大"
      ],
      "positioning_hint": {
        "suggested_role": "frontline_candidate",
        "max_suggested_amount": 20000
      }
    }
  ],
  "filtered_out": [
    {
      "symbol": "601398.SH",
      "name": "工商银行",
      "stage": "user_preferences",
      "reason": "命中 excluded_theme_keywords=银行"
    }
  ],
  "explanations": {
    "strategy_summary": "板块共振与趋势加速是本轮主导逻辑，资金与情绪因子贡献最高",
    "weight_summary": [
      "sector_heat_score 对排序贡献最高",
      "breakout_quality 与 main_fund_inflow 提供二次确认"
    ],
    "constraint_summary": [
      "已过滤银行方向",
      "单票金额按 20000 元约束"
    ]
  },
  "proposal_packet": {
    "selected_symbols": ["002882.SZ", "600166.SH"],
    "watchlist_symbols": ["002263.SZ"],
    "discussion_focus": [
      "热度持续性",
      "是否属于板块核心",
      "是否优于现有持仓"
    ]
  },
  "evaluation_trace": {
    "stored": true,
    "trace_id": "eval-20260415-001"
  }
}
```

---

## 5.2 返回字段分层

### 第一层：作业元数据

- `job_id`
- `status`
- `request_id`
- `generated_at`

### 第二层：市场摘要

用于快速告诉 Agent：

- 现在是不是适合这套战法
- 哪些板块热
- 哪些系统性风险需要注意

### 第三层：候选结果

每个候选至少应包含：

- 标的身份
- 综合评分
- 战法适配度
- 因子得分
- 正向证据
- 反向证据
- 风险标签
- 仓位提示

### 第四层：过滤结果

Agent 必须知道哪些票被过滤掉，以及在哪一层被过滤：

- 硬过滤
- 用户偏好
- 风控约束
- 战法不匹配
- 因子不达标

### 第五层：解释摘要

返回面向讨论与提案，不是面向程序。

这部分要帮助 Agent 后续：

- 向其他 Agent 发起提案
- 接受质询
- 形成简洁对外说明

### 第六层：评估追踪

把本次调用接入评估与学习链。

---

## 六、与当前实现的兼容映射

当前 `RuntimeJobRequest` 很轻：

```python
class RuntimeJobRequest(BaseModel):
    symbols: list[str] = []
    universe_scope: str = "main-board"
    max_candidates: int | None = None
    auto_trade: bool = False
    account_id: str = "8890130545"
```

因此第一阶段不建议直接删除它，而应新增一个更完整的请求模型，例如：

- `AgentRuntimeComposeRequest`

兼容策略：

### 阶段 1：协议先落文档与模型

- 新增 `AgentRuntimeComposeRequest`
- 新增 `AgentRuntimeComposeResponse`
- 新增 `POST /runtime/jobs/compose`
- 内部仍可复用现有 pipeline 的部分能力

### 阶段 2：最小可用适配

先把以下字段映射到现有逻辑：

- `universe.scope -> universe_scope`
- `universe.symbol_pool -> symbols`
- `output.max_candidates -> max_candidates`
- `constraints.user_preferences.excluded_theme_keywords -> runtime_config / parameter_service`

### 阶段 3：逐步接入原子库

再逐步接入：

- 因子仓库
- 战法插件
- 评分拆解
- 过滤链路
- 评估追踪

---

## 七、实现拆分建议

建议按以下顺序落地：

### 第一步：协议模型

- `contracts/runtime_compose.py`
- 定义 `AgentRuntimeComposeRequest`
- 定义 `AgentRuntimeComposeResponse`
- 定义 `PlaybookSpec / FactorSpec / ConstraintSpec / CandidateExplanation`

### 第二步：编排器

- `runtime/strategy_composer.py`
- 负责解析 Agent 请求
- 负责参数校验
- 负责把请求拆给因子层 / 战法层 / 过滤层

### 第三步：因子与战法注册表

- `runtime/factor_registry.py`
- `runtime/playbook_registry.py`
- `runtime/strategy_repository.py`

用于做：

- 能力声明
- 参数校验
- 可用性查询
- 版本查询
- 状态管理
- 学习产物入库
- 实验/正式/归档分区管理

### 第三步补充：学习产物沉淀机制

为了支持 Agent 自己学习出战法，仓库层应再补一条正式链路：

- `runtime/learned_strategy_store.py`

负责记录：

- Agent 提交的新战法模板
- Agent 提交的新因子组合
- Agent 提交的新权重模板
- 对应的来源证据、评估结果、审批状态

建议状态至少包含：

- `draft`
- `experimental`
- `review_required`
- `active`
- `rejected`
- `archived`

### 第四步：路由与落盘

- `apps/runtime_api.py`
- 新增 `/runtime/jobs/compose`
- 落审计、监督、评估 trace

### 第五步：Agent 消费说明面

- `/system/agents/capability-map`
- `/system/workflow/mainline`
- Hermes / OpenClaw prompt

都要同步告诉 Agent：

- 可用哪些因子
- 可用哪些战法
- 参数如何组织
- 返回结构怎么看

---

## 八、当前阶段完成定义

当以下四项完成时，可视为协议层第一阶段完成：

- 有正式文档
- 有请求响应模型
- 有 `/runtime/jobs/compose` 路由
- 有最小兼容实现与示例回包

在此之前，不应直接把 runtime 继续堆成更多硬编码规则。

进一步完成定义：

- 有仓库级注册与查询能力
- 有实验/学习/正式分区
- 有 Agent 学习产物入库与审核链

做到这一步，runtime 才真正具备“随时增加新策略、因子调优、沉淀 Agent 新战法”的仓库形态。
