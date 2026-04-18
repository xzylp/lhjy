# 飞书三权与机器人主线操作手册

## 目标

这份手册只回答 4 件事：

1. 飞书里到底能做什么
2. 该发给哪个入口
3. 机器人会怎么回
4. 联调失败时先查哪里

主原则不变：

- Agent 是大脑，负责分析、提案、讨论和决策
- 程序是手脚、监督器、电子围栏和留痕系统
- 飞书是人机协同前台，不是另一套业务逻辑

## 三权定义

### 1. 知情权

作用：

- 看当前盘面、讨论、执行、评分、监督、研究摘要
- 接收重要通知，例如执行回执、讨论结论、盘后学习摘要

主要入口：

- `GET /system/feishu/rights`
- `GET /system/feishu/briefing`
- `POST /system/feishu/briefing/notify`

适合的飞书问法：

- `现在状态怎么样`
- `今天推荐什么`
- `有没有执行回执`
- `各 agent 评分多少`

### 2. 调参权

作用：

- 用自然语言直接改参数
- 支持先预览再落地
- 复杂治理仍可转成正式 proposal

主要入口：

- `POST /system/feishu/adjustments/natural-language`
- `POST /system/adjustments/natural-language`
- `POST /system/params/proposals`

适合的飞书指令：

- `把测试仓位改到三成`
- `今天先不买白酒股`
- `逆回购保留改成 0`
- `只预览，不落地，把单票金额上限改到 2 万`

### 3. 询问权

作用：

- 按问题意图组合状态、讨论、执行、参数、研究、风控和个股分析
- 不再限制成固定 5 类死主题

主入口：

- `POST /system/feishu/ask`

支持的问题方向：

- `status`
- `discussion`
- `execution`
- `params`
- `scores`
- `supervision`
- `research`
- `risk`
- `holding_review`
- `day_trading`
- `position`
- `opportunity`
- `replacement`
- `symbol_analysis`

适合的飞书问法：

- `现在各 agent 忙不忙`
- `最近参数提案有哪些`
- `当前有哪些风控阻断`
- `现在有哪些机会票`
- `当前仓位为什么这样配`
- `帮我分析一下金龙羽`

## 飞书前台的 3 个核心入口

### 入口 A：问答主入口

- 路径：`/system/feishu/ask`
- 用途：状态问答、个股分析、持仓复核、执行回执、参数和评分查询
- 适合机器人日常回复

### 入口 B：事件回调入口

- 路径：`/system/feishu/events`
- 用途：接收群聊消息、@机器人消息、催办回执、链接预览请求
- 这是长连接 worker 和飞书后台打通的关键入口

### 入口 C：自然语言调参入口

- 路径：`/system/feishu/adjustments/natural-language`
- 用途：把飞书里的口语化调参直接翻成程序参数动作

## 机器人应该承担的工作

机器人负责：

- 自动发送重要消息
- 自动催办超时或活动迟滞的 agent
- 自动接收“已收到催办”“已处理”这类回执并回写监督状态
- 自动回复状态、执行、参数、监督、个股分析等问题

机器人不负责：

- 自己编盘面结论
- 自己拍板实盘交易
- 脱离程序围栏直接下单
- 替代 runtime、discussion、execution 的正式留痕链

## 主线流程对应的飞书动作

### 盘前

推荐动作：

- 读 `/system/feishu/briefing`
- 读 `/system/monitoring/cadence`
- 读 `/system/workspace-context`

飞书里适合问：

- `今天状态怎样`
- `今天有哪些待处理阻断`

### 盘中发现

推荐动作：

- 问 `/system/feishu/ask`
- 运行 `runtime/jobs/pipeline` 或 `runtime/jobs/intraday`
- 结合研究、策略、风险反馈形成提案

飞书里适合问：

- `现在有哪些机会票`
- `有没有替换建议`
- `帮我分析一下某某股票`

### 讨论收敛

推荐动作：

- 读 `/system/discussions/client-brief`
- 读 `/system/discussions/meeting-context`
- 必要时发起自然语言调参或正式 proposal

飞书里适合问：

- `今天推荐什么`
- `当前仓位为什么这样配`

### 执行预演与派发

推荐动作：

- 读 `/system/discussions/execution-precheck`
- 读 `/system/discussions/execution-intents`
- 读 `/system/discussions/execution-dispatch/latest`

飞书里适合问：

- `有没有执行回执`
- `现在能不能下`
- `当前有哪些风控阻断`

### 监督催办

推荐动作：

- 读 `/system/agents/supervision-board`
- 机器人自动发催办
- 收到回执后回写 ack

飞书里适合发：

- `研究已收到催办`
- `风控已处理`
- `审计转入复核`

## 常用联调命令

### 1. 看飞书三权总览

```bash
curl -sS "http://127.0.0.1:8100/system/feishu/rights"
```

### 2. 看当前飞书简报

```bash
curl -sS "http://127.0.0.1:8100/system/feishu/briefing"
```

### 3. 测试飞书问答

```bash
curl -sS -X POST "http://127.0.0.1:8100/system/feishu/ask" \
  -H "Content-Type: application/json" \
  -d '{"question":"帮我分析一下金龙羽"}'
```

### 4. 测试自然语言调参

```bash
curl -sS -X POST "http://127.0.0.1:8100/system/feishu/adjustments/natural-language" \
  -H "Content-Type: application/json" \
  -d '{"instruction":"把测试仓位改到三成，只预览"}'
```

### 5. 看监督板

```bash
curl -sS "http://127.0.0.1:8100/system/agents/supervision-board"
```

### 6. 看飞书长连接状态

```bash
curl -sS "http://127.0.0.1:8100/system/feishu/longconn/status"
```

## 飞书后台最少要确认的配置

### 事件订阅

- 使用长连接
- 已订阅 `im.message.receive_v1`
- 机器人在群里被 @ 或接收消息后，控制面能收到事件

### 回调配置

- 使用长连接
- 已订阅 `url.preview.get`
- 链接预览需要和控制面的 `/system/feishu/events` 配合

### 长连接状态

至少满足：

- `status=connected`
- `pid_alive=true`
- `is_fresh=true`

## 失败时先查哪里

### 机器人不回复

先查：

1. `/system/feishu/longconn/status`
2. `systemctl --user status ashare-feishu-longconn.service`
3. `/system/feishu/events/config`

### 调参失败

先查：

1. `/system/feishu/adjustments/natural-language`
2. `/system/adjustments/natural-language`
3. 当前消息是否真的表达了明确参数动作

### 问答只会回固定帮助词

先查：

1. 控制面是否已重启到最新代码
2. `/system/feishu/ask` 是否已支持当前问题意图
3. 飞书长连接进程是否还是旧版本代码

### 催办不准或老催 runtime

先查：

1. `/system/agents/supervision-board`
2. 是否已经切到“按活动痕迹催办”
3. 最近是否有研究、调参、风控预检、审计复核等真实动作留痕

## 推荐值班口径

人默认只接重要消息：

- 今日战果
- 真实买卖回执
- 重大风控阻断
- 关键调参结果

机器人默认承担：

- 日常催办
- 状态摘要
- 问答分流
- 调参入口承接

## 当前结论

飞书不是外挂通知工具，而是量化交易台的人机协同前台。

它的职责已经明确分成三层：

- 知情权：看状态和摘要
- 调参权：改参数和治理偏好
- 询问权：问事实、问结论、问单票

而真正的正式动作，仍然要落回程序接口、监督链和电子围栏里完成。
