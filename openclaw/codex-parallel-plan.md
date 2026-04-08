# Codex 并行执行方案

> 项目：`ashare-system-v2`
> 日期：2026-04-07
> 目标：把当前剩余的 `M6 / T12` 改造任务拆成可并行推进的多 Codex 工作包，同时尽量避免文件冲突。

## 1. 当前完成度判断

当前主链已经完成：

- `M0` 数据契约扩展
- `M1` 市场状态机与板块联动
- `M2` 战法路由与买入决策
- `M3` 个股股性画像与龙头排序最小实现
- `M4` 战法化退出引擎最小实现
- `M5` OpenClaw 协议层与 dossier/runtime API

当前未完全收口的部分主要集中在 `M6 / T12`：

- 股性画像产线化
- 退出引擎继续盘手化
- 学习治理闭环补观察窗口与高风险审批
- 专门复盘视图
- 最终总回归与文档收口

## 2. 完成定义

建议把“本轮改造完成”定义为以下 5 条全部达成：

1. `StockBehaviorProfile` 从运行时轻量推断推进到稳定产线或缓存产物。
2. `tail_market / ExitEngine` 继续补强，具备更真实的快进快出判断。
3. 参数治理闭环补齐观察窗口、rollback 后再评估和高风险审批入口。
4. 系统补出按标的/按原因的复盘视图。
5. 跑通 focused regression，并完成任务板与执行记录收口。

## 3. 并行拓扑

建议采用：

- `Codex-A`：股性画像产线
- `Codex-B`：退出引擎盘手化
- `Codex-C`：学习治理闭环
- `Codex-Main`：集成与文档

不建议超过 4 个并行写主线。

原因：

- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py) 写入冲突概率很高
- [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py) 与 [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py) 都是高冲突文件
- worker 太多后，集成成本会明显高于编码收益

## 4. 工作包拆分

### 4.1 Codex-A：股性画像产线

目标：

- 把股性画像从运行时轻推断推进到稳定日更/缓存产物
- 让 runtime / dossier / precompute 优先复用画像产物
- 降低 fallback 占比

允许修改：

- [stock_profile.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/stock_profile.py)
- [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)
- [test_precompute_contexts.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_precompute_contexts.py)
- 可新增与画像产线直接相关的测试文件

禁止修改：

- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
- `src/ashare_system/governance/*`

交付物：

- 画像缓存或产线入口
- dossier/runtime 复用真实画像产物
- 更清晰的 fallback 标记
- 测试补齐

建议回归：

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
PYTHONPATH=src pytest tests/test_precompute_contexts.py
```

### 4.2 Codex-B：退出引擎盘手化

目标：

- 强化 `tail_market` 快撤判断
- 扩展 1m/5m 微结构
- 加强板块同步弱化、个股快速失真退出
- 更明确地利用入场上下文

允许修改：

- [exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/exit_engine.py)
- [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
- [test_phase_exit_engine.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase_exit_engine.py)
- [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py)

禁止修改：

- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- `src/ashare_system/governance/*`
- [stock_profile.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/stock_profile.py)

交付物：

- 更真实的分时快撤规则
- 更细的板块联动弱化退出
- 入场后股性容忍度与退出节奏联动
- 测试补齐

建议回归：

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
PYTHONPATH=src pytest tests/test_phase_exit_engine.py tests/test_phase1.py
```

### 4.3 Codex-C：学习治理闭环

目标：

- 补 proposal 的观察窗口管理
- 补 rollback 后二次效果追踪
- 补高风险 rollback 的人工审批/放行接口
- 补按标的/按原因的复盘视图

允许修改：

- [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)
- [param_service.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_service.py)
- [param_store.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/governance/param_store.py)
- [attribution.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/learning/attribution.py)
- [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py)

禁止修改：

- [scheduler.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/scheduler.py)
- [stock_profile.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/strategy/stock_profile.py)
- [precompute.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/precompute.py)

交付物：

- 观察窗口与效果跟踪增强
- rollback 后再评估链
- 高风险审批接口
- 复盘查询接口
- 测试补齐

建议回归：

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
PYTHONPATH=src pytest tests/test_system_governance.py
```

### 4.4 Codex-Main：集成与收口

目标：

- 不抢功能开发
- 只负责收结果、跑回归、修小集成问题、更新文档

允许修改：

- 小型集成冲突涉及的必要文件
- [quant-full-v1-execution-taskboard.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/quant-full-v1-execution-taskboard.md)
- [执行记录-codex.md](/mnt/d/Coding/lhjy/ashare-system-v2/discuss/执行记录-codex.md)

职责：

1. 合并 `A/B/C` 的成果
2. 跑 focused regression
3. 必要时修小型集成问题
4. 更新任务板和执行记录

建议回归：

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py
```

## 5. 推荐目录隔离

不要让多个 Codex 共用同一个工作目录。

推荐使用 `git worktree`：

```bash
cd /mnt/d/Coding/lhjy/ashare-system-v2
git worktree add ../ashare-system-v2-a
git worktree add ../ashare-system-v2-b
git worktree add ../ashare-system-v2-c
git worktree add ../ashare-system-v2-main
```

若当前仓库状态不适合 `worktree`，至少复制 4 份目录，避免文件互相覆盖。

## 6. 推荐合并顺序

建议合并顺序：

1. `Codex-C`
2. `Codex-A`
3. `Codex-B`
4. `Codex-Main`

原因：

- `Codex-C` 独占 [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py)，冲突风险最高
- `Codex-A` 和 `Codex-B` 写入面相对独立
- `Codex-Main` 最后做收口，避免反复改文档和总测试

## 7. 各 Codex 启动提示词

### 7.1 Codex-A 提示词

```text
你负责 ashare-system-v2 的“股性画像产线化”。
只允许修改：
- src/ashare_system/strategy/stock_profile.py
- src/ashare_system/precompute.py
- tests/test_precompute_contexts.py
- 可新增与该主题直接相关的测试文件

不要修改：
- src/ashare_system/apps/system_api.py
- src/ashare_system/scheduler.py
- src/ashare_system/governance/*

目标：
1. 把 StockBehaviorProfile 从运行时启发式推断推进到稳定日更/缓存产物
2. precompute/runtime/dossier 优先复用画像产物
3. 降低 fallback 占比
4. 补测试并给出回归命令

要求：
- 用 apply_patch 改文件
- 不做与本任务无关的重构
- 最后列出改动文件、行为变化、测试结果
```

### 7.2 Codex-B 提示词

```text
你负责 ashare-system-v2 的“退出引擎盘手化增强”。
只允许修改：
- src/ashare_system/strategy/exit_engine.py
- src/ashare_system/scheduler.py
- tests/test_phase_exit_engine.py
- tests/test_phase1.py

不要修改：
- src/ashare_system/apps/system_api.py
- src/ashare_system/governance/*
- src/ashare_system/strategy/stock_profile.py

目标：
1. 补更真实的 1m/5m 微结构快撤
2. 强化板块同步弱化和个股快速失真退出
3. 引入更明确的入场后上下文约束
4. 保持现有 tail_market 主链兼容

要求：
- 用 apply_patch 改文件
- 补最小但有效的测试
- 最后列出改动文件、行为变化、测试结果
```

### 7.3 Codex-C 提示词

```text
你负责 ashare-system-v2 的“学习治理闭环增强”。
只允许修改：
- src/ashare_system/apps/system_api.py
- src/ashare_system/governance/param_service.py
- src/ashare_system/governance/param_store.py
- src/ashare_system/learning/attribution.py
- tests/test_system_governance.py

不要修改：
- src/ashare_system/scheduler.py
- src/ashare_system/strategy/stock_profile.py
- src/ashare_system/precompute.py

目标：
1. 给 parameter proposal/rollback 增加观察窗口管理
2. 增加 rollback 后二次效果追踪
3. 增加高风险 rollback 的人工审批/放行接口
4. 增加按标的/按原因的复盘视图
5. 保持当前 proposal/effects/rollback-preview/rollback-apply 兼容

要求：
- 用 apply_patch 改文件
- 不另起平行治理系统
- 最后列出改动文件、接口变化、测试结果
```

### 7.4 Codex-Main 提示词

```text
你是集成 Codex，不负责大功能开发。
职责：
1. 接收 A/B/C 产出并合并
2. 跑 focused regression
3. 必要时修小型集成问题
4. 更新 discuss/quant-full-v1-execution-taskboard.md
5. 更新 discuss/执行记录-codex.md

总回归命令：
PYTHONPATH=src pytest tests/test_precompute_contexts.py tests/test_phase_strategy_runtime_api.py tests/test_phase_exit_engine.py tests/test_phase1.py tests/test_system_governance.py
```

## 8. 并行期间的硬约束

1. [system_api.py](/mnt/d/Coding/lhjy/ashare-system-v2/src/ashare_system/apps/system_api.py) 必须单人独占。
2. [test_phase1.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_phase1.py) 和 [test_system_governance.py](/mnt/d/Coding/lhjy/ashare-system-v2/tests/test_system_governance.py) 不允许多人同时修改。
3. 文档统一由 `Codex-Main` 修改。
4. 每个 worker 只能在自己负责的测试范围内补测试。
5. 合并前先跑该 worker 自己的最小回归，不要把未测代码直接交给 `Main`。

## 9. 当前建议执行顺序

第一阶段并行启动：

- 启动 `Codex-C`
- 启动 `Codex-A`
- 启动 `Codex-B`

第二阶段收口：

- `Codex-Main` 依次合并并跑回归
- 若有冲突，优先保留：
  - `system_api.py` 以 `Codex-C` 为主
  - `precompute.py / stock_profile.py` 以 `Codex-A` 为主
  - `scheduler.py / exit_engine.py` 以 `Codex-B` 为主

## 10. 本文档用途

这份文档用于：

- 后续手工开多个 Codex 会话时直接粘贴提示词
- 控制并行边界，减少文件冲突
- 明确“改造完成”前还剩哪些任务
- 给最终集成留出清晰的合并顺序
