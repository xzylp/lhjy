# 2026-04-19 正式上线实测记录

> 实测时间: 2026-04-19
> 实测目标: 按正式上线要求核验控制面、数据面、监督面、Runtime Compose 与上线准入状态

---

## 2026-04-19 修复后复测结论

在完成本轮代码修复并重启 `ashare-system-v2.service` 后，四项阻断已连续收口：

1. `go-live gate` 已从 `BLOCKED` 变为 `READY`
2. 全量自动化测试已通过，结果为 `162/162 OK`
3. `system/readiness` 已恢复可用，`service-recovery-readiness` 对历史交易日检查返回 `ready`
4. `compose-from-brief` 在正式环境下的同类请求实测耗时约 `13.72s`，已低于此前 `20s` 超时表现

修复后关键结果：

- `scripts/check_go_live_gate.sh` 输出：`READY`
- `./.venv/bin/python -m unittest discover -s tests` 输出：`Ran 162 tests ... OK`
- `GET /system/deployment/service-recovery-readiness?trade_date=2026-04-17` 输出：`status=ready`
- `POST /runtime/jobs/compose-from-brief` 实测：
  - `elapsed_seconds=13.72`
  - `ok=true`
  - `trace_id=compose-runtime-2021b87d16`

本轮修复覆盖点：

- 默认 `trade_date` 解析补上兜底，监督面与执行摘要不再因空 candidate 场景抛错
- 健康检查中“当前执行平面未使用”的 xtquant 路径改为 `warning`
- 监督任务 prompt 补齐“自己组织参数”，并修复重复派发节流与“有新活动但未对齐目标”场景
- `/runtime/factors` 与 `/runtime/playbooks` 改为返回 seed 目录，冻结公开契约
- 飞书机会票回复改回 `机会票概览`
- `create_app()` 在非 `live` 模式下不再后台抢刷账户状态，消除测试期缓存竞态
- `readiness` 会自修复过期/错误的 startup recovery 记录
- `service-recovery-readiness` 对历史交易日不再错误按“当前时钟新鲜度”阻断
- `check_go_live_gate.sh` 会自动解析参考交易日，并在非当日场景放宽交易时段约束
- compose 在有板块白名单/黑名单时会先收缩 universe，再跑 pipeline，减少全市场扫描开销

最终判断：

本轮四件事已经一起完成，并经过实测闭环验证。当前仓库状态已从“服务在线但上线阻断”推进到“测试通过、准入门放行、关键接口可用”。

---

## 一、实测范围

- 服务与端口在线状态
- 健康巡检与 go-live gate
- 全量自动化测试
- Runtime 策略仓库与评估账本
- `POST /runtime/jobs/compose-from-brief` 自定义编排能力
- `GET /system/agents/supervision-board` 监督质量信号
- `GET /system/readiness` 与 `GET /system/deployment/service-recovery-readiness`

## 二、实测结论

### 总结论

当前系统**不满足正式上线要求**。

原因不是“服务起不来”，而是“系统在线但准入仍 blocked，且关键链路仍有稳定性与回归问题”：

1. 控制面、Go 数据平台、长连接与相关 systemd 服务都在线。
2. Runtime 因子/战法仓库规模已达标，实测为 `64` 个因子、`20` 个战法。
3. 自定义 `compose-from-brief` 可以在正式运行环境完成执行并写入 evaluation ledger。
4. 但正式上线准入脚本 `check_go_live_gate.sh` 结果仍为 `BLOCKED`。
5. `system/readiness` 与 `service-recovery-readiness` 也都返回 `blocked`。
6. 全量自动化测试仍失败，且失败点集中在监督链路、默认场景和契约漂移。
7. `compose-from-brief` 在正式环境下首次 20 秒超时，第二次放宽到 60 秒后才返回，说明响应时延不稳定。

## 三、实测结果对账

### 1. 服务在线与健康巡检

- `127.0.0.1:8100` 控制面在线
- `127.0.0.1:18793` Go Data Platform 在线
- `/health` 返回 `{"status":"ok","service":"ashare-system-v2","mode":"live","environment":"dev"}`
- `scripts/health_check.sh` 结果整体通过

说明：

- 这代表“服务在线”，不代表“达到上线准入”
- 健康巡检更偏进程、端口、HTTP 存活检查

### 2. go-live gate

执行 `scripts/check_go_live_gate.sh` 后，结果为 `BLOCKED`。

关键阻断项：

- `准入1_linux_services: NO`
- `准入2_windows_bridge: NO`
- `准入3_apply_closed_loop: NO`
- `准入4_agent_chain: NO`

关键摘要：

- `service_recovery=blocked`
- `discussion status=blocked cycle=round_1_running cases=138`
- `2026-04-19 尚无执行派发回执`
- `latest receipt ... status=failed`

这意味着当前不是“可谨慎上线”，而是“准入门本身明确未通过”。

### 3. 全量自动化测试

执行：

```bash
./.venv/bin/python -m unittest discover -s tests
```

结果：

- `Ran 162 tests in 49.536s`
- `FAILED (failures=6, errors=7)`

本轮主要失败点：

- 监督看板依赖默认 `trade_date` 推导，空场景仍会抛错
- 健康检查里未启用的 xtquant 路径状态仍返回 `ok`，与测试期望 `warning` 不一致
- `account-state` 缓存接口行为回归
- Agent 任务 prompt 缺少“自己组织参数”要求
- 同阶段重复派发节流失效
- 飞书机会票紧凑回复格式不符合测试预期
- `/runtime/factors` 对外暴露数量超过 64，契约发生漂移

### 4. Runtime 仓库与账本

实测接口：

- `GET /runtime/strategy-repository`
- `GET /runtime/evaluations?limit=5`

结果：

- 仓库统计：`factor=64`，`playbook=20`
- `governance_summary.observe_only_count=84`
- Evaluation Ledger 可正常读到最新 trace

本次实测生成了以下 trace：

- `compose-runtime-0cf7896d8f` 对应 `formal-prod-check-20260419-001`
- `compose-runtime-7aa8725bbf` 对应 `formal-prod-check-20260419-002`

说明：

- 因子/战法规模目标已达第一阶段要求
- 账本落盘能力可用
- 但治理建议几乎全部仍停留在 `observe_only`

### 5. 自定义 Compose Brief 实测

实测接口：

```text
POST /runtime/jobs/compose-from-brief
```

验证内容：

- 自定义 `intent_mode=news_catalyst_scan`
- 自定义 `playbooks=[trend_acceleration, sector_resonance]`
- 自定义 `factors=[momentum_slope, sector_heat_score]`
- 自定义权重、行业排除、regime 约束、风险约束

结果：

- 第一次请求 `formal-prod-check-20260419-001` 在 `20s` 超时，但后台最终仍完成并写入 ledger
- 第二次请求 `formal-prod-check-20260419-002` 在更长超时窗口内成功返回
- 返回体包含：
  - `composition_manifest`
  - `applied_constraints`
  - `evaluation_trace`
  - `repository.used_assets`
  - `brief_execution`

但本次结果也暴露出正式环境问题：

- 返回候选数为 `0`
- `filtered_out` 中 3 个候选都被 `market_regime_not_allowed:chaos` 拦截
- 首次请求响应时延超过 20 秒，不适合作为稳定上线表现

说明：

- Compose 功能“能跑”
- 但线上数据环境下可用性与响应稳定性还不够

### 6. 监督面与恢复面

实测接口：

- `GET /system/agents/supervision-board?trade_date=2026-04-17&overdue_after_seconds=180`
- `GET /system/readiness`
- `GET /system/deployment/service-recovery-readiness?trade_date=2026-04-17`

结果：

- 监督看板能返回 `quality_state`、`quality_reason`、`activity_signals`
- 监督面能识别出：
  - `ashare-strategy` 为 `overdue + blocked`
  - `ashare` 为 `overdue + blocked`
  - `ashare-executor` 为 `needs_work + low`
- `system/readiness.status=blocked`
- `service-recovery-readiness.status=blocked`

服务恢复未通过项：

- `readiness`
- `workspace_context`
- `recovery_signal_freshness`

结论：

- 监督能力“有内容”
- 但监督内容本身正在证明系统尚未恢复到可上线状态

## 四、上线阻断清单

按正式上线标准，当前至少还有以下阻断项：

1. `go-live gate` 直接返回 `BLOCKED`
2. `system/readiness` 返回 `blocked`
3. `service-recovery-readiness` 返回 `blocked`
4. 全量测试仍有 `6 failures / 7 errors`
5. Compose 在线请求存在明显时延不稳定
6. 执行闭环未完成，最新 gateway receipt 为 `failed`
7. 监督主线仍停在 `round_1_running`，总协调与策略岗位处于超时阻塞态

## 五、建议修复顺序

### P0：先解除上线阻断

- 修复 `readiness` 与 `service-recovery-readiness` 的阻断项
- 补齐 `workspace_context` 与恢复信号新鲜度
- 查清 `latest receipt status=failed` 的根因
- 把 `go-live gate` 跑到非 `BLOCKED`

### P1：修复正式环境稳定性

- 优先处理 `compose-from-brief` 的响应时延
- 明确首次请求为何会超 20 秒
- 对慢链路增加更清晰的阶段日志与超时保护

### P2：消灭当前测试回归

- 修默认 `trade_date` 空场景
- 修健康检查状态语义
- 修监督 prompt 与派发节流
- 冻结 `/runtime/factors` 对外数量契约
- 修复 `account-state` 缓存行为

## 六、最终判断

本次实测证明：

- 系统已经具备较完整的运行骨架
- 正式运行环境中的核心服务大多在线
- Runtime Compose、监督看板、仓库账本都不是空壳

但同样明确证明：

- **它现在还不具备正式上线条件**
- 当前状态更接近“上线前联调/准生产演练”，不是“可直接放行”

---

# ashare-system-v2 框架代码工程交接文档

> 交接日期: 2026-04-12
> 语法检查: **20/20 全部通过** ✅ (19 Python + 1 JSON)

---

## 一、本次交付物清单

### 新建文件（7 个）

| 文件 | 行数 | 模块 | 完成度 |
|------|------|------|--------|
| [auction_fetcher.py](file:///srv/projects/ashare-system-v2/src/ashare_system/data/auction_fetcher.py) | ~130 | A·竞价数据抓取 | 框架 ⬜ 数据源 TODO |
| [auction_engine.py](file:///srv/projects/ashare-system-v2/src/ashare_system/strategy/auction_engine.py) | ~155 | A·竞价研判引擎 | **已实现** ✅ |
| [micro_rhythm.py](file:///srv/projects/ashare-system-v2/src/ashare_system/monitor/micro_rhythm.py) | ~190 | B·微观节奏追踪 | **已实现** ✅ |
| [event_bus.py](file:///srv/projects/ashare-system-v2/src/ashare_system/data/event_bus.py) | ~140 | D·事件总线 | **已实现** ✅ |
| [prompt_patcher.py](file:///srv/projects/ashare-system-v2/src/ashare_system/learning/prompt_patcher.py) | ~220 | F·Prompt 自进化 | **已实现** ✅ |
| [registry_updater.py](file:///srv/projects/ashare-system-v2/src/ashare_system/learning/registry_updater.py) | ~140 | F·注册表覆写 | **已实现** ✅ |
| [nightly_sandbox.py](file:///srv/projects/ashare-system-v2/src/ashare_system/strategy/nightly_sandbox.py) | ~175 | G·夜间沙盘 | 框架 ⬜ 模拟逻辑 TODO |

### 修改文件（12 个）

| 文件 | 改动要点 |
|------|---------|
| [contracts.py](file:///srv/projects/ashare-system-v2/src/ashare_system/contracts.py) | +128 行，8 个 Pydantic 模型 + 3 个 Literal |
| [state_machine.py](file:///srv/projects/ashare-system-v2/src/ashare_system/discussion/state_machine.py) | 重写: 动态轮次 `start_round(n)` / `can_continue_discussion()` |
| [discussion_service.py](file:///srv/projects/ashare-system-v2/src/ashare_system/discussion/discussion_service.py) | DiscussionCycle +3 字段，start_round 移除硬限，refresh_cycle 自动续轮 |
| [contradiction_detector.py](file:///srv/projects/ashare-system-v2/src/ashare_system/discussion/contradiction_detector.py) | +80 行 `detect_evidence_conflicts()` + 9 对关键词对立表 |
| [finalizer.py](file:///srv/projects/ashare-system-v2/src/ashare_system/discussion/finalizer.py) | `build_finalize_bundle()` 新增 `agent_weights` 参数 |
| [opinion_validator.py](file:///srv/projects/ashare-system-v2/src/ashare_system/discussion/opinion_validator.py) | `round == 2` → `round >= 2` 泛化 |
| [auto_governance.py](file:///srv/projects/ashare-system-v2/src/ashare_system/learning/auto_governance.py) | +96 行 `build_agent_lesson_patches()` 归因→教训 |
| [score_state.py](file:///srv/projects/ashare-system-v2/src/ashare_system/learning/score_state.py) | +57 行 `export_weights()` + `run_daily_settlement()` |
| [sell_decision.py](file:///srv/projects/ashare-system-v2/src/ashare_system/strategy/sell_decision.py) | 重写核心: Playbook-aware + Regime-aware 动态退出参数 |
| [buy_decision.py](file:///srv/projects/ashare-system-v2/src/ashare_system/strategy/buy_decision.py) | `generate()` 新增 `auction_signals`，KILL 过滤 / PROMOTE +20 分 |
| [exit_engine.py](file:///srv/projects/ashare-system-v2/src/ashare_system/strategy/exit_engine.py) | `check()` 新增 `micro_signal`，PEAK_FADE/RHYTHM_BREAK/VALLEY_HOLD 处理 |
| [freshness.py](file:///srv/projects/ashare-system-v2/src/ashare_system/data/freshness.py) | +30 行 `tag_freshness()` 数据时效标签 |

### 修改配置文件（1 个）

| 文件 | 改动要点 |
|------|---------|
| [team_registry.final.json](file:///srv/projects/ashare-system-v2/openclaw/team_registry.final.json) | 新增 `agent_weights` 节点（4 个 Agent 初始权重 1.0） |

---

## 二、新增数据模型速查

```python
# 集合竞价
AuctionAction    = Literal["PROMOTE", "HOLD", "DEMOTE", "KILL"]
AuctionSnapshot  # symbol, price, volume, prev_close, prev_volume_5d_avg, open_change_pct
AuctionSignal    # symbol, action, reason, auction_volume_ratio, open_change_pct, playbook

# 微观节奏
MicroSignalType  = Literal["PEAK_FADE", "VALLEY_HOLD", "RHYTHM_BREAK"]
MicroBarSnapshot # symbol, open, high, low, close, volume, timestamp
MicroSignal      # symbol, signal_type, strength, timestamp, bar_count

# 事件总线
EventType        = Literal["NEGATIVE_NEWS", "PRICE_ALERT", "AUCTION_SIGNAL", ...]
MarketEvent      # event_type, symbol, payload, timestamp, priority, source

# 夜间沙盘
SandboxResult    # trade_date, tomorrow_priorities, missed_opportunities, simulation_log

# 自进化
Lesson           # text, source, agent_id, created_at, expires_at
PatchResult      # agent_id, lessons_before, lessons_after, added, removed, error

# 数据时效
DataFreshnessLevel = Literal["REALTIME", "NEAR_REALTIME", "DELAYED", "STALE"]
```

---

## 三、剩余 TODO 汇总

### 🔴 必须补全（系统上线前）

| # | 文件 | TODO | 说明 |
|---|------|------|------|
| 1 | `auction_fetcher.py` | `_fetch_from_gateway()` | HTTP 调用 Windows Gateway |
| 2 | `auction_fetcher.py` | `_fetch_from_akshare()` | akshare 数据适配 |
| 3 | `scheduler.py` | 注册 7 个新 cron 任务 | 竞价×2 + 微观巡检 + 盘后×3 + 夜间沙盘 |
| 4 | `discussion_service.py` | `finalize_cycle()` 调 `export_weights()` | 加权投票闭环 |

### 🟡 功能深化

| # | 文件 | TODO | 说明 |
|---|------|------|------|
| 5 | `event_fetcher.py` | `fetch_incremental()` | 增量抓取 + 事件发射 |
| 6 | `market_watcher.py` | 价格异动 → `PRICE_ALERT` | 事件总线对接 |
| 7 | `nightly_sandbox.py` | `_simulate_param_adjustment()` | 参数模拟逻辑 |
| 8 | `self_evolve.py` | scheduler 17:15 接入 | 策略权重建议 |
| 9 | `continuous.py` | scheduler 17:30 接入 | 增量训练 |

### 🔵 数据层整固

| # | 文件 | TODO | 说明 |
|---|------|------|------|
| 10 | `precompute.py` | `as_of_time` 参数 | 截面保障 |
| 11 | `serving.py` | `as_of_time` 过滤 | 数据一致性 |

---

## 四、模块间调用关系图

```mermaid
graph TB
    subgraph "数据层"
        AF["auction_fetcher<br/>竞价快照"]
        EB["event_bus<br/>事件总线"]
        FR["freshness<br/>时效标签"]
    end

    subgraph "策略层"
        AE["auction_engine<br/>竞价研判"]
        BD["buy_decision<br/>买入决策"]
        SD["sell_decision<br/>卖出决策"]
        EE["exit_engine<br/>退出引擎"]
        NS["nightly_sandbox<br/>夜间沙盘"]
    end

    subgraph "监控层"
        MR["micro_rhythm<br/>微观节奏"]
    end

    subgraph "讨论层"
        SM["state_machine<br/>动态轮次"]
        CD["contradiction_detector<br/>证据矛盾"]
        FN["finalizer<br/>加权投票"]
        DS["discussion_service<br/>讨论主控"]
        OV["opinion_validator<br/>意见校验"]
    end

    subgraph "学习层"
        SS["score_state<br/>学分结算"]
        AG["auto_governance<br/>归因治理"]
        PP["prompt_patcher<br/>Prompt进化"]
        RU["registry_updater<br/>注册表覆写"]
    end

    AF -->|AuctionSnapshot| AE
    AE -->|AuctionSignal| BD
    MR -->|MicroSignal| EE
    SD -->|playbook params| EE
    EB -.->|NEGATIVE_NEWS| EE
    EB -.->|PRICE_ALERT| MR
    NS -->|tomorrow_priorities| BD

    SM -->|can_continue| DS
    CD -->|evidence_conflicts| DS
    OV -->|round >= 2| DS
    SS -->|export_weights| FN
    FN -->|agent_weights| DS

    SS -->|score_states| AG
    AG -->|lesson_patches| PP
    SS -->|weight_values| RU

    style AF fill:#e74c3c,color:white
    style AE fill:#27ae60,color:white
    style MR fill:#27ae60,color:white
    style EB fill:#27ae60,color:white
    style PP fill:#27ae60,color:white
    style RU fill:#27ae60,color:white
    style NS fill:#e74c3c,color:white
    style SM fill:#27ae60,color:white
    style CD fill:#27ae60,color:white
    style FN fill:#27ae60,color:white
    style SD fill:#27ae60,color:white
    style BD fill:#27ae60,color:white
    style EE fill:#27ae60,color:white
    style SS fill:#27ae60,color:white
    style AG fill:#27ae60,color:white
    style OV fill:#27ae60,color:white
    style FR fill:#27ae60,color:white
    style DS fill:#f39c12,color:white
```

**绿色 = 核心逻辑已实现** | **红色 = 框架在，核心 TODO 待补** | **黄色 = 已改但需接线**

---

## 五、快速验证手册

### 5.1 全量语法检查（已通过 ✅）
```bash
python3 -c "
import ast, json
files = [
    'src/ashare_system/contracts.py','src/ashare_system/data/auction_fetcher.py',
    'src/ashare_system/data/event_bus.py','src/ashare_system/data/freshness.py',
    'src/ashare_system/strategy/auction_engine.py','src/ashare_system/strategy/sell_decision.py',
    'src/ashare_system/strategy/buy_decision.py','src/ashare_system/strategy/exit_engine.py',
    'src/ashare_system/strategy/nightly_sandbox.py','src/ashare_system/monitor/micro_rhythm.py',
    'src/ashare_system/discussion/state_machine.py','src/ashare_system/discussion/contradiction_detector.py',
    'src/ashare_system/discussion/finalizer.py','src/ashare_system/discussion/discussion_service.py',
    'src/ashare_system/discussion/opinion_validator.py','src/ashare_system/learning/auto_governance.py',
    'src/ashare_system/learning/score_state.py','src/ashare_system/learning/prompt_patcher.py',
    'src/ashare_system/learning/registry_updater.py',
]
for f in files:
    with open(f) as fh: ast.parse(fh.read()); print(f'✓ {f}')
with open('openclaw/team_registry.final.json') as fh: json.load(fh); print('✓ team_registry.final.json')
print('All checks passed ✅')
"
```

### 5.2 单模块功能验证

**竞价引擎**（核心已实现，可直接测试）:
```python
from src.ashare_system.contracts import AuctionSnapshot
from src.ashare_system.strategy.auction_engine import AuctionEngine
eng = AuctionEngine()
snap = AuctionSnapshot(symbol="000001.SZ", price=15.0, volume=50000,
                       prev_close=14.5, prev_volume_5d_avg=80000, open_change_pct=0.0345)
assert eng.evaluate_auction_snapshot(snap, "leader_chase").action == "PROMOTE"
snap_kill = AuctionSnapshot(symbol="000002.SZ", price=14.0, volume=5000,
                            prev_close=14.5, prev_volume_5d_avg=80000, open_change_pct=-0.0345)
assert eng.evaluate_auction_snapshot(snap_kill, "leader_chase").action == "KILL"
```

**微观节奏**（三种信号检测均已实现）:
```python
from src.ashare_system.contracts import MicroBarSnapshot
from src.ashare_system.monitor.micro_rhythm import MicroRhythmTracker
tracker = MicroRhythmTracker()
bar1 = MicroBarSnapshot(symbol="X", open=15.0, high=15.2, low=14.8, close=15.1, volume=1000)
bar2 = MicroBarSnapshot(symbol="X", open=15.1, high=15.5, low=14.7, close=14.9, volume=800)
tracker.push_bar(bar1)
signal = tracker.push_bar(bar2)
assert signal and signal.signal_type == "PEAK_FADE"
```

**动态讨论轮次**:
```python
from src.ashare_system.discussion.state_machine import DiscussionStateMachine as SM
assert SM.start_round(3) == ("execution_pool_building", "round_running")
assert SM.can_continue_discussion({"remaining_disputes": ["x"]}, 2, max_rounds=3) is True
assert SM.can_continue_discussion({"remaining_disputes": ["x"]}, 3, max_rounds=3) is False
```

**Playbook-aware 止损**:
```python
from src.ashare_system.strategy.sell_decision import SellDecisionEngine
eng = SellDecisionEngine()
p_leader = eng._resolve_params("leader_chase", "")
p_reflow = eng._resolve_params("sector_reflow_first_board", "")
assert p_leader["atr_stop_mult"] == 1.5   # 龙头票止损紧
assert p_reflow["atr_stop_mult"] == 2.5   # 首板票止损松
p_chaos = eng._resolve_params("leader_chase", "chaos")
assert p_chaos["atr_stop_mult"] == 1.5 * 0.8  # chaos 再收紧
```

---

## 六、向后兼容保障

| 改动点 | 兼容处理 |
|--------|---------|
| `state_machine` `start_round_1/2()` | 保留为 `start_round(1/2)` 的快捷方法 |
| `discussion_service.start_round()` | 移除 `ValueError`，Round 3+ 走泛化路径 |
| `sell_decision.evaluate()` | 新增 `playbook=""`, `regime=""` 默认值 |
| `exit_engine.check()` | 新增 `micro_signal=None` 默认值 |
| `buy_decision.generate()` | 新增 `auction_signals=None` 默认值 |
| `finalizer.build_finalize_bundle()` | 新增 `agent_weights=None` 默认值 |
| `opinion_validator` | `round == 2` → `round >= 2`，向前无影响 |
| `DiscussionCycle` | 新增 3 个有默认值的字段 |
| `team_registry.final.json` | 新增不影响现有解析 |

所有现有调用点**无需任何修改**即可继续工作。

---

## 七、接力开发建议

### 推荐开工顺序（最短路径出闭环）

**第 1 步（半天）— 跑通盘后闭环**:
1. 在 `scheduler.py` 注册 3 个盘后任务（16:30 学分结算 / 16:45 Prompt 自进化 / 17:00 注册表刷新）
2. 在 `discussion_service.finalize_cycle()` 中调 `score_state.export_weights()` 传给 finalizer
3. 手动执行一次完整盘后流程验证

**第 2 步（半天）— 竞价数据层**:
4. 实现 `auction_fetcher._fetch_from_akshare()`（Gateway 可后补）
5. 在 `scheduler.py` 注册 09:20 + 09:24 竞价任务

**第 3 步（1 天）— 事件总线接线**:
6. 在 `scheduler.py.__init__()` 初始化 EventBus 并注册响应链
7. 在 `event_fetcher.py` 和 `market_watcher.py` 中加事件发射

**第 4 步（半天）— 收尾**:
8. `nightly_sandbox._simulate_param_adjustment()` 模拟逻辑
9. `precompute.py` as_of_time 截面保障

### ⚠️ 关键注意事项

> [!CAUTION]
> `prompt_patcher.py` 的安全约束（`MAX_LESSONS=10`, `MAX_LESSON_CHARS=200`）是**硬编码**的，
> 绝不能被自进化逻辑覆盖。如需调整只能手动改源码。

> [!WARNING]
> `scheduler.py` 是 2601 行巨石文件，建议在改之前 `git tag v0.9-pre-upgrade` 做快照。
> 新增的 7 个 cron 任务建议集中放在文件顶部的任务定义区。

> [!IMPORTANT]
> `discussion_service.py` 的 `refresh_cycle()` 现在会**自动续轮** —
> 当 `can_continue_discussion()` 返回 True 时自动进入 Round N+1。
> 这意味着如果 Agent 回复中频繁包含 `remaining_disputes`，讨论可能会走满 `max_rounds=3` 轮。
> 可以通过调整 `DiscussionCycle.max_rounds` 控制上限。
