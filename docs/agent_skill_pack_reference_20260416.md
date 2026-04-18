# Agent 补充 Skill 包参考

> 日期：2026-04-16
> 定位：这不是主程序新架构，也不是新的主决策引擎，而是 Hermes / Agent 可挂载的辅助技能包。

## 基本原则

- 主判断来自本项目主链：
  - runtime compose
  - discussion
  - risk gate
  - execution
- skill 包只做三件事：
  - 补充
  - 对照
  - 交叉验证

## 使用边界

- skill 不能直接替代主程序事实链。
- skill 不能直接产出最终执行结论。
- skill 不能绕开风控和留痕。
- 若 skill 与 agent 自组组合冲突，必须显式报告差异与采纳理由。

## 建议挂载的 10 类 Skill

### 1. Policy-Monitor

- 用途：抓政策、监管、行业新闻并提炼市场影响。
- 适合：政策催化、监管变化、题材扩散。

### 2. Stock-Analyst

- 用途：做个股快速体检。
- 适合：候选池外陌生股票、主控快速问答、与主链逐票分析对照。

### 3. Daily-Trade-Review

- 用途：盘后复盘和归因。
- 适合：盘后学习、错失机会总结、行为偏差修复。

### 4. Quant-KB

- 用途：量化战法和指标知识补充。
- 适合：给 agent 提供战法灵感和参数参考。

### 5. Stock-Watcher

- 用途：监控自选股异动、突破和预警。
- 适合：盯盘、持仓异动提醒、机会票二次触发。

### 6. A-Shares-Data

- 用途：补充 A 股基础、财务、历史数据。
- 适合：排雷、历史对照、非主链数据补丁。

### 7. Report-Extractor

- 用途：提炼研报、财报、公告。
- 适合：研究证据补丁、公告摘要、快速读研报。

### 8. Risk-Alert-System

- 用途：监控回撤、利空和市场异常。
- 适合：持仓风险提醒、盘中安全提醒。

### 9. Backtest-Engine

- 用途：对 agent 自组组合做最小回测和对照验证。
- 适合：参数对比、战法对照、盘后学习反馈。

### 10. Skill-Vetter

- 用途：审计外挂 skill 的权限和数据边界。
- 适合：安全检查、权限治理、外挂能力准入。

## 推荐用法

### 方法一：主链先行

1. 先由 agent 用本项目主链组织组合。
2. 再调用 skill 做补充和交叉验证。
3. 最终输出“主方案 / skill 参考方案 / 差异原因”。

### 方法二：候选池外快检

1. 主控先调用主程序接口做 ad-hoc 体检。
2. 再用 `Stock-Analyst / Policy-Monitor / Report-Extractor` 做补丁。
3. 若结论仍成立，再升级成 opportunity_ticket 或正式讨论。

### 方法三：盘后自进化

1. 主链输出当日 compose、discussion、execution、归因结果。
2. 用 `Daily-Trade-Review / Backtest-Engine / Quant-KB` 做复盘和策略对照。
3. 再沉淀成参数提案、learned asset 候选或 prompt patch 候选。

## 最重要的纪律

- skill 是外挂工具箱，不是主厨。
- 主厨仍然是 agent。
- 菜谱和后厨仍然是本项目程序。
