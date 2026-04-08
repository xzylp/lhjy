# 子系统规格与通信矩阵

> 本文档补充 [technical-manual.md](technical-manual.md)，聚焦 `ashare-system-v2` 的子系统职责、OpenClaw 对接面和当前正式通信链路。

## 一、Strategy

**目录:** `src/ashare_system/strategy/`

**职责:**

- 选股漏斗
- 买卖决策
- 仓位规划
- 多策略组合

**OpenClaw 消费方:**

- `ashare-strategy`
- `ashare-risk`

**核心接口:**

- `GET /strategy/strategies`
- `POST /strategy/screen`

## 二、Risk

**目录:** `src/ashare_system/risk/`

**职责:**

- 规则校验
- 执行守卫
- 情绪保护
- 风险拦截

**OpenClaw 消费方:**

- `ashare-risk`
- `ashare-executor`

## 三、Execution

**相关目录:** `src/ashare_system/apps/execution_api.py`, `src/ashare_system/infra/`

**职责:**

- 账户余额读取
- 持仓读取
- 订单与成交查询
- 已放行执行意图下单

**核心接口:**

- `GET /execution/health`
- `GET /execution/balance/{account_id}`
- `GET /execution/positions/{account_id}`
- `GET /execution/orders/{account_id}`
- `GET /execution/trades/{account_id}`
- `POST /execution/orders`

## 四、Runtime

**相关目录:** `src/ashare_system/apps/runtime_api.py`, `src/ashare_system/report/`, `src/ashare_system/scheduler.py`

**职责:**

- 运行健康探测
- pipeline 选股任务
- intraday 任务
- 自动交易任务入口
- 最新运行报告输出

**OpenClaw 消费方:**

- `ashare-runtime`
- `ashare-strategy`
- `ashare-risk`
- `ashare-audit`

**核心接口:**

- `GET /runtime/health`
- `POST /runtime/jobs/pipeline`
- `POST /runtime/jobs/intraday`
- `POST /runtime/jobs/autotrade`
- `GET /runtime/reports/latest`
- `GET /system/reports/runtime`

## 五、Research

**相关目录:** `src/ashare_system/apps/research_api.py`

**职责:**

- 研究同步
- 新闻入库
- 公告入库
- 研究摘要输出

**OpenClaw 消费方:**

- `ashare-research`
- `ashare-strategy`
- `ashare-audit`

**核心接口:**

- `GET /research/health`
- `POST /research/sync`
- `POST /research/events/news`
- `POST /research/events/announcements`
- `GET /research/summary`
- `GET /system/research/summary`

## 六、System / Governance / Audit

**相关目录:** `src/ashare_system/apps/system_api.py`, `src/ashare_system/infra/state.py`, `src/ashare_system/infra/audit_store.py`

**职责:**

- 系统概览
- 运行组件状态
- 审计记录
- 会议纪要
- 配置读取
- 盘后主报告输出

**核心接口:**

- `GET /system/health`
- `GET /system/home`
- `GET /system/overview`
- `GET /system/operations/components`
- `GET /system/audits`
- `GET /system/audits/by-decision`
- `GET /system/audits/by-experiment`
- `GET /system/research/summary`
- `GET /system/reports/runtime`
- `GET /system/reports/postclose-master`
- `GET /system/reports/postclose-master-template`
- `POST /system/meetings/record`
- `GET /system/meetings/latest`
- `GET /system/config`
- `GET /system/params`
- `POST /system/params/proposals`
- `GET /system/params/proposals`
- `GET /system/cases`
- `GET /system/cases/{case_id}`
- `POST /system/discussions/opinions/batch`
- `POST /system/cases/{case_id}/rebuild`
- `POST /system/cases/rebuild`
- `GET /system/discussions/summary`
- `GET /system/discussions/agent-packets`
- `GET /system/discussions/reason-board`
- `GET /system/discussions/reply-pack`
- `GET /system/discussions/final-brief`
- `GET /system/discussions/client-brief`
- `GET /system/discussions/meeting-context`
- `GET /system/discussions/cycles`
- `GET /system/discussions/cycles/{trade_date}`
- `POST /system/discussions/cycles/bootstrap`
- `POST /system/discussions/cycles/{trade_date}/rounds/{round}/start`
- `POST /system/discussions/cycles/{trade_date}/refresh`
- `POST /system/discussions/cycles/{trade_date}/finalize`
- `GET /system/discussions/execution-precheck`
- `GET /system/discussions/execution-intents`
- `POST /system/discussions/execution-intents/dispatch`
- `GET /system/discussions/execution-dispatch/latest`
- `GET /system/agent-scores`
- `POST /system/agent-scores/settlements`

## 七、Market / Data / Factors

**相关目录:**

- `src/ashare_system/data/`
- `src/ashare_system/factors/`
- `src/ashare_system/apps/market_api.py`

**职责:**

- 股票池与板块
- snapshot 与 bars
- 因子计算与筛选
- 数据清洗与缓存

**核心接口:**

- `GET /market/health`
- `GET /market/universe`
- `GET /market/snapshots`
- `GET /market/bars`
- `GET /market/sectors`

## 八、Notify / Report / Learning

**相关目录:**

- `src/ashare_system/notify/`
- `src/ashare_system/report/`
- `src/ashare_system/learning/`

**职责:**

- 飞书 Open API 推送
- 运行报告与盘后主报告
- 学习与复盘基础能力

**说明:**

- 用户入口在 OpenClaw，不在 `notify/`。
- `notify/` 负责程序主动输出，不承担前台路由。

## 九、当前正式通信链路

```text
Feishu / WebChat
  -> OpenClaw Gateway
  -> main
  -> ashare
  -> 指定量化子角色
  -> ashare-system-v2 FastAPI
  -> QMT / XtQuant / state / audits / reports
  -> ashare 汇总
  -> main 对外回复
```

## 十、当前通信约束

- 飞书与 webchat 都先进入 `main`。
- `main` 不直接做量化子任务。
- 股票与量化问题统一由 `main` 转给 `ashare`。
- `ashare` 收到量化任务后必须继续分发到正确子角色。
- `ashare-runtime` 负责“跑和读”，不负责放行与下单。
- `ashare` 自己只可写 discussion / governance 接口，不能越权替代运行、研究、策略、执行子角色。
- Round 2 只处理 `round_2_target_case_ids` 指向的争议票，不做全量复评。
- Round 2 是否完成不只看四个子代理都发言，还要看每条 round 2 opinion 是否包含对争议的结构化回应；缺少实质回应时，cycle 会停留在 `round_2_running`。
- `ashare-risk` 是执行前闸门。
- `ashare-executor` 只能处理已放行执行意图。
- `POST /system/discussions/cycles/{trade_date}/finalize` 响应应直接提供 `client_brief`，用于主调度与通知复用。
- 若 `finalize` 返回 `finalize_skipped=true` 且原因是 `discussion_not_ready`，说明仍需补齐二轮回应，不应解释为网关、FastAPI 或执行器故障。
- 执行回执统一由 `execution-intents/dispatch` 生成，并可通过 `execution-dispatch/latest` 回读。
- `ashare-audit` 只读、复核、归纳，不负责改写系统状态，也不单独主导最终排名。

## 十一、WSL 与 Windows 直连要求

- Windows 服务地址按 manifest 动态暴露，不再写死单一 IP。
- WSL 侧优先探测 manifest 中的 `preferred_wsl_url`，应以当前发布的 WSL 可达地址为准，不假定固定为 `127.0.0.1:8100`。
- 若不可达，则依次尝试 manifest 中的其他候选地址，以及 `/etc/resolv.conf` / 默认网关 / PowerShell 发现地址。
- OpenClaw 与人工排障统一使用 `scripts/ashare_api.sh`，不再通过 PowerShell 中转。

## 十二、检查与启动

### 12.1 启动

- Windows: `scripts/start_unattended.ps1`
- WSL: `scripts/start_openclaw_gateway.sh`

### 12.2 探测

```bash
./scripts/ashare_api.sh probe
./scripts/ashare_api.sh GET /health
./scripts/ashare_api.sh GET /runtime/health
curl http://127.0.0.1:18789/health
```

### 12.3 目标结果

- gateway 健康
- WSL 能直接访问 Windows 上的 `ashare-system-v2`
- `main -> ashare -> 子团队` 路由链不依赖旧通信兜底逻辑，且默认通过新增会话活动验证闭环，不清理历史会话
