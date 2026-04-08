# ashare-system-v2 最小落地实施方案

> 状态: Draft  
> 版本: v0.1  
> 日期: 2026-04-04

## 1. 目标

本文档定义从当前设计稿进入程序实现的最小闭环路线。

原则：

- 不重写大框架
- 先打通最小可用闭环
- 先落对象、状态、参数，再扩展多策略和盘中细节
- 每一步都能验证

## 2. 最小闭环范围

第一阶段只落 5 个核心能力：

1. 每日 `base_pool`
2. `candidate_case` 持久化
3. 两轮讨论结果归档
4. `param_change_event` 动态参数变更链
5. `agent_score_state` 次日结算链

暂不在第一阶段落地：

- 全量因子体系
- 全量盘中 T 执行
- 自由策略切换引擎
- 完整前端展示

## 3. 依赖锚点

实施必须以前面 3 份草案为准：

- [param-registry.draft.yaml](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/param-registry.draft.yaml)
- [state-machine-transition-table.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/state-machine-transition-table.md)
- [core-object-schemas.md](/mnt/d/Coding/lhjy/ashare-system-v2/docs/implementation-drafts/core-object-schemas.md)

## 4. 推荐实施顺序

### Phase A: 对象与存储层

先落 3 个对象：

- `candidate_case`
- `param_change_event`
- `agent_score_state`

建议先用现有状态目录或本地 JSON/审计存储实现，不急着先上复杂数据库。

验收：

- 可以创建、读取、更新这 3 类对象
- 每类对象都有统一 ID 和时间戳

### Phase B: 参数层

目标：

- 加载 `system_defaults`
- 支持 `market_regime_params`
- 支持 `agent_adjusted_params`
- 实现参数覆盖顺序和回退

最低要求：

- 能读取一个参数的 `default_value/current_value/allowed_range`
- 能写入一条已审批参数变更
- 能在 `today_session / next_trading_day / until_revoked` 三种周期下生效

验收：

- 一条自然语言参数变更请求能落成 `param_change_event`
- 参数更新后可被读取并影响运行

### Phase C: 股票池与讨论链

目标：

- 每日必须生成 `base_pool`
- 基于 `base_pool` 生成 `candidate_case`
- 能归档 round 1 / round 2 的 Agent 观点
- 能把每只股票收敛成：
  - `selected`
  - `watchlist`
  - `rejected`

最低要求：

- 不要求一开始就让所有 Agent 自动自由协作
- 可以先由 `ashare` 按顺序拉取 4 个核心意见：
  - `research`
  - `strategy`
  - `risk`
  - `audit`

验收：

- `candidate_case` 能完整保存两轮观点和最终状态

### Phase D: 学分结算链

目标：

- 在下一交易日收盘后计算 `agent_score_state`
- 更新学分
- 映射权重桶
- 低分进入学习状态

最低要求：

- 先只对 `research`、`strategy`、`risk`、`audit` 做结算
- `runtime` 和 `executor` 先不纳入同口径

验收：

- 至少能完成一次日结算
- 至少能把某个 Agent 从 `10` 分更新到新分数

### Phase E: 参数调优闭环

目标：

- 用户或 Agent 提自然语言调参
- `ashare` 结构化
- `risk` / `audit` 审批
- 参数生效
- 后续在学分结算中体现影响

验收：

- 一条参数调整请求完整走完：
  - request
  - proposal
  - approve/reject
  - effective
  - audit record

## 5. 推荐代码落点

优先推荐新增或扩展以下模块：

- `src/ashare_system/learning/`
  - `score_state.py`
  - `settlement.py`
  - `improvement.py`

- `src/ashare_system/governance/`
  - `param_registry.py`
  - `param_store.py`
  - `param_service.py`

- `src/ashare_system/discussion/`
  - `candidate_case.py`
  - `discussion_service.py`
  - `state_machine.py`

- `src/ashare_system/apps/`
  - 增加参数查询、候选 case、评分状态读取接口

## 6. API 最小建议

第一阶段建议补的最小 API：

- `GET /system/params`
- `POST /system/params/proposals`
- `GET /system/params/proposals`
- `GET /system/cases`
- `GET /system/cases/{case_id}`
- `GET /system/agent-scores`

## 7. 第一批验收用例

建议先做 5 个用例：

1. 生成 `base_pool`
2. 写入 1 个 `candidate_case`
3. 写入 1 条 `param_change_event`
4. 完成 1 次 round 1 + round 2 结果归档
5. 完成 1 次次日收盘学分结算

## 8. 风险控制

第一阶段要避免的坑：

- 一开始就引入过多数据库复杂度
- 直接做全量盘中 T 自动执行
- 没有对象模型就开始写 prompt 和接口耦合逻辑
- 参数可调但没有审批和留痕

## 9. 进入代码前的完成标志

只有以下条件满足，才建议正式进入代码阶段：

- 参数注册表草案稳定
- 核心 schema 稳定
- 总状态机不再大改
- `risk` 和 `audit` 的权责边界稳定

## 10. 下一步建议

建议下一步直接做两件事：

1. 产出一份“文件级实施清单”
2. 确定最小第一批代码修改范围

这样就可以从文档阶段切到实际开发阶段。
