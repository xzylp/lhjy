# ashare-system-v2 Linux 侧多 Agent 并发数据面 Go 平台改造方案与交付要求（方案 B）

更新时间：2026-04-16

## 1. 文档用途

本文档用于向 Gemini 或其他研发代理下发正式任务，要求其基于当前项目现状，输出一份可实施、可验收、可渐进接入的技术方案。

本任务统一限定为“方案 B”，并且边界必须严格按以下口径理解：

- Windows 侧 Go 网关已经写好，视为既成事实与既有基础设施
- 本次不要求重新设计或重写 Windows 侧 Go 网关
- 本次要求设计的是 Linux 侧统一 Go 数据平台
- 该 Linux Go 平台需要同时负责：
  - 对外：并发请求 Windows `18791` 数据面入口
  - 对内：并发改造项目内部服务之间的请求与调度
- 目标是在 Linux 侧建立统一并发请求平台，而不是重复做一份 Windows 网关方案

不接受以下方向的输出：

- 只写 Windows 侧增强
- 把主要篇幅放在 Windows 侧如何重写
- 不设计 Linux 本地统一 Go 数据平台
- 只覆盖对外请求，不覆盖项目内部服务请求

## 2. 背景资料

请严格以下列文档为事实依据，不要脱离项目实际，不要虚构现有能力：

- `/srv/projects/ashare-system-v2/docs/XTQ_Windows_Go_技术实现说明_20260416.md`
- `/srv/projects/ashare-system-v2/docs/XTQ_Windows_Go_接口说明_20260416.md`

## 3. 项目背景与已知前提

当前 Windows 侧 QMT 网关已经完成 Go 化并发改造，并且已经可以作为正式数据出口使用。本次工作目标不是修改 Windows 侧业务协议，也不是重新实现 Windows 数据面，而是在 Linux 侧新增统一 Go 并发平台，承接：

- 发往 Windows 侧真实数据面的并发请求
- 项目内部服务之间的并发请求、聚合、缓存、调度与观测

### 3.1 Windows 侧当前统一入口

- 正式入口：`http://192.168.122.66:18791`
- 本机回环：`http://127.0.0.1:18791`
- 管理 UI：`http://127.0.0.1:18889`
- 鉴权 Header：`X-Ashare-Token`
- Token 文件位置保持不变
- endpoints 文件位置保持不变
- `/status` 返回结构兼容原约定

### 3.2 Windows Go 网关当前内部调用链（既成事实，不是本次主改造对象）

- 外部请求 -> Go Proxy (`18791`)
- 行情类请求 -> `scripts/qmt_rpc_py36.py` -> QMT Python 3.6 运行时
- 账户/交易类请求 -> Trade Bridge (`127.0.0.1:18792`) -> XtQuant / QMT
- 状态类请求 -> 本地状态文件 + Trade Bridge 健康检查

### 3.3 Windows Go 网关当前已具备能力

- 三层 lane 优先级调度：
  - `quote`
  - `account_fast`
  - `trade_slow`
- 每条 lane 已支持：
  - `workers`
  - `queue`
  - `queue_timeout_ms`
  - `retries`
- 全局支持：
  - `retry_backoff_ms`
- 高负载时支持先排队，超时后返回 `429`
- `/qmt/trade/order` 默认不自动重试，避免重复下单
- `/health` 不参与慢交易队列竞争
- 程序与状态数据固定保存在项目目录

### 3.4 Windows Go 网关当前实测结果

已知文档事实包括：

- Go Proxy 已接管 `18791`
- `/health` 正常
- `/status` 正常并兼容原结构
- `quote/instruments` 正常
- `account/asset` 正常
- 在 5 个慢 `orders` 并发场景下，`/health` 仍约 `68ms` 返回，`/asset` 仍可完成

### 3.5 当前系统总体定位

方案设计必须严格符合以下口径：

- Agent 是大脑，负责提出问题、组织参数、决定何时拉数据、何时复扫、何时调权重
- 程序是工具底座、数据底座、执行器、监督器、电子围栏
- Windows Go 网关是“外部真实数据面入口”，本次作为既有前提直接复用
- Linux 本地必须使用 Go 设计一个“统一并发请求平台”，同时覆盖：
  - 对 Windows 数据面的请求
  - 对项目内部各服务接口的请求
- 整个系统不是固定问答机器人，也不是固定打分器
- 必须从真实量化团队、真实盘中盯盘、真实多 Agent 并发研究与协作的视角设计

## 4. 正式任务

请输出一份完整的《Linux 侧 Go 并发数据平台技术方案》，覆盖：

1. Linux 本地 Go 并发请求平台如何设计
2. Linux -> Windows `18791` 请求链如何通过 Go 平台完成并发化、缓存化、调度化
3. 项目内部服务请求如何通过 Go 平台完成并发改写与统一治理
4. 如何让主程序以渐进方式接入，而不是推翻现有 `ashare-system-v2` 架构
5. 如何支持多 Agent 同时拉真实数据、盘中监控、并发研究、并发问询、并发策略扫描
6. 如何兼顾性能、稳定性、幂等、安全、风控和可观测性

请注意：

- 本任务明确采用“方案 B：Windows 侧既有 Go 网关 + Linux 侧统一 Go 数据平台”
- Windows 侧只需要作为现状与边界纳入分析，不作为主要研发对象
- 必须把 Linux 本地 Go 并发请求平台作为正式主角来设计
- Linux Go 平台必须覆盖两类请求：
  - 对外请求：Linux -> Windows `18791`
  - 对内请求：项目内部服务之间的接口调用
- 目标不是单点加速，而是在 Linux 侧建立统一并发请求平台

## 5. 核心目标

本次方案必须服务于以下目标：

- 多 Agent 并发取数，不互相拖死
- 行情、账户快查、慢交易查询彼此隔离
- Linux 本地新增 Go 并发请求平台，作为程序内部统一数据面客户端与统一内部服务请求层
- 为 Hermes / OpenClaw / 主控 Agent / 子 Agent 提供稳定、高吞吐、低阻塞的数据底座
- 保持现有主程序框架不推倒重来，只增强调用链稳定性、并发性、监督性、可观测性
- 保持现有 Windows 对外协议不变
- 强化交易接口幂等保护与风控边界

## 6. 输出文档必须覆盖的章节

请至少包含以下 13 个章节，缺一不可。

### 6.1 目标与边界

需要说明：

- 本次改造解决什么问题
- 不解决什么问题
- 与现有 Python 主程序、Windows Go 网关、QMT、Trade Bridge 的边界

### 6.2 当前现状分析

需要基于两份文档总结：

- Windows 侧已具备哪些能力
- Linux 侧当前直接请求 Windows 与内部服务时还存在哪些瓶颈
- 特别分析多 Agent 同时请求下可能出现的问题：
  - 连接复用不足
  - 慢接口拖累快接口
  - 重复请求过多
  - 无本地缓存
  - 无请求合并
  - 无统一超时与熔断
  - 无优先级继承
  - 无租户/Agent 隔离
  - 观测不足

### 6.3 总体架构方案

必须说明：

- Agent
- 主程序
- Linux Go 并发请求平台
- Windows Go 网关
- QMT / Trade Bridge
- 每一层职责
- 同步调用、异步调用、缓存命中、失败降级路径

### 6.4 Linux 本地 Go 并发请求平台设计

这一章必须重点展开，至少包含：

- 部署位置建议
- 进程形态建议：
  - 独立 daemon
  - sidecar
  - 内嵌服务
  - CLI wrapper
  - 明确推荐一种主方案，其他作为备选
- 对主程序暴露方式：
  - HTTP
  - Unix socket
  - gRPC
  - 本地队列
  - 推荐一种主方案，其他作为备选
- 内部调度模型：
  - lane 划分
  - 优先级模型
  - 并发池模型
  - 排队模型
  - 超时模型
  - 重试模型
  - 熔断/限流模型
- 请求分类建议：
  - `quote`
  - `account_fast`
  - `trade_slow`
  - `health`
  - `metadata`
  - `bulk_snapshot`
  - `watch_poll`
- 是否支持 singleflight / 请求合并
- 是否支持短时缓存：
  - `tick`
  - `kline`
  - `instruments`
  - `sectors`
  - `positions`
  - `asset`
- 是否支持批量聚合请求
- 如何支持 agent 级调用画像与隔离：
  - `agent_id`
  - `session_id`
  - `request_purpose`
  - `priority_hint`
  - `deadline_hint`
- 如何同时覆盖两类请求：
  - Linux -> Windows `18791`
  - Linux 内部服务 -> 内部服务

### 6.5 并发与调度策略

请给出明确建议值，不要空泛。至少包括：

- lane 数量建议
- 每个 lane 的 workers 建议
- queue 建议
- queue_timeout_ms 建议
- retries 建议
- retry_backoff_ms 建议
- 连接池大小建议
- keepalive 建议
- 最大同时下游连接数建议
- 盘前、盘中、盘后、夜间学习的差异化配置建议

### 6.6 请求协议增强建议

要求：

- 不破坏现有 Windows 对外协议
- Linux 本地内部协议可以增强

请设计：

- 统一请求头
- `trace_id`
- `agent_id`
- `session_id`
- `request_class`
- `priority_hint`
- `freshness_hint`
- `idempotency_key`
- `timeout_ms`
- `source`
- `scenario`
- 是否支持批量接口包装
- 是否支持结果中的：
  - `queue_wait_ms`
  - `upstream_cost_ms`
  - `cache_hit`
  - `lane`

### 6.7 数据一致性与风控边界

必须说明：

- 哪些接口允许缓存，哪些不允许
- 哪些接口允许重试，哪些严禁自动重试
- 下单、撤单、查单如何避免重复提交
- 账户资产、持仓、订单、成交之间如何做读一致性设计
- 如何防止 Agent 因瞬时脏数据做出错误判断
- 如何设计“只读数据面”和“交易动作面”的硬隔离

### 6.8 可观测性与运维方案

必须包含：

- 结构化日志
- 指标埋点
- trace
- lane 级监控
- queue 深度
- `429` 次数
- 各接口 `P50/P95/P99`
- cache hit ratio
- merge hit ratio
- upstream error ratio
- per-agent 调用统计
- 慢请求审计
- 告警建议
- 管理面或控制台建议

### 6.9 故障与降级策略

必须覆盖：

- Windows 网关短暂不可达
- QMT 卡死
- Trade Bridge 超时
- 单 lane 打满
- 全局超载
- Linux 本地 Go 服务重启
- 缓存失效
- 重复拉同一批 symbols
- 返回 `429/5xx` 时的主程序降级策略
- 盘中与盘后差异化降级策略

### 6.10 与现有 ashare-system-v2 的接线方案

必须明确到模块级别：

- 哪些 Python 模块需要改
- 哪些适配器保留
- 哪些地方新增 Go client adapter
- 哪些内部服务请求需要先纳入 Go 平台
- 主程序如何逐步切流
- 是否先保留旧 Python 直连链路作为回退
- 如何做灰度迁移
- 如何避免一次性大改

### 6.11 分阶段实施计划

建议拆成：

- `P0` 方案落地准备
- `P1` Linux Go 并发客户端最小版
- `P2` 缓存与 singleflight
- `P3` 监控与管理面
- `P4` 批量聚合与多 Agent 优化
- `P5` 灰度切流与回退

每阶段必须写：

- 目标
- 产出物
- 风险
- 验收标准

### 6.12 交付清单

请给出明确文件级交付物建议，例如：

- 设计文档
- 接口契约文档
- 配置样例
- Go 代码目录结构
- Python 适配层改造点
- 部署脚本
- `systemd` 或 `supervisor` 文件
- 压测脚本
- 监控面板定义
- 回滚方案

### 6.13 验收标准

必须量化，至少包括：

- 5 个 Agent 并发取数
- 10 个 Agent 并发盯盘
- 慢 `orders/trades` 不拖死 `health/asset`
- 高频行情请求下 `P95`
- 账户快查 `P95`
- `429` 出现条件
- 下单接口不重复
- 缓存命中效果
- 队列等待可观测
- 故障切回旧链路可用
- 真实联调与压测要求

## 7. 额外必须附带的两部分

### 7.1 推荐目录结构

请给出 Linux Go 并发平台建议的目录树。

### 7.2 建议默认配置

请给出一份可直接落地的 JSON 或 YAML 配置样例，至少包括：

- lane 配置
- timeout
- retries
- backoff
- cache TTL
- merge 策略
- metrics
- upstream 配置
- fallback 配置

## 8. 输出要求

请严格遵守以下要求：

- 全文使用中文
- 不写宣传口号
- 站在架构师 + 量化系统工程师视角
- 不能只写概念，必须落到结构、模块、配置、接口、流程、验收
- 不要脱离现有项目实际，必须兼容当前 `ashare-system-v2`
- 不要推翻现有框架，强调渐进式增强
- 重点服务“多 Agent 同时拉真实数据”的场景
- 必须特别体现：
  - 优先级调度
  - 请求合并
  - 短时缓存
  - 连接复用
  - 可观测性
  - 灰度切流
  - 风控隔离
  - 交易接口幂等保护

## 9. 注意事项

- 不要把系统写成固定 FAQ 机器人
- 不要把 Agent 写成被动调用者
- 要体现 Agent 主动调用工具、程序提供底座服务的关系
- 方案必须兼容现有 Windows Go 网关事实，不要假设它不存在
- Windows 侧不是本次主要研发对象，不要把主要篇幅花在 Windows 重写上
- Linux 本地 Go 并发平台是本次方案主体，必须展开
- 必须同时覆盖对外请求和项目内部服务请求
- 输出内容要达到“可以直接作为研发实施蓝图”的程度

## 10. 建议给 Gemini 的输出标题

《ashare-system-v2 Linux 侧多 Agent 并发数据面 Go 平台改造方案与交付要求（方案 B）》
