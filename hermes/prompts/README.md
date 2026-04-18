# Hermes 提示词模板

这些文件不是给最终用户直接看的闲聊提示词，而是给 Hermes 主代理在两类场景下复用：

1. `delegate_task` 时，把对应角色模板内容和当次任务上下文一起传给子代理。
2. `cron` 建任务时，把巡检或学习模板作为任务提示词主体。

## 角色模板

- `runtime_scout.md`
- `event_researcher.md`
- `strategy_analyst.md`
- `risk_gate.md`
- `audit_recorder.md`
- `execution_operator.md`

## cron 模板

- `cron_intraday_watch.md`
- `cron_position_watch.md`
- `cron_postclose_learning.md`
- `cron_nightly_sandbox.md`

## 使用原则

- 子代理没有父会话记忆，使用前必须把 `trade_date`、目标仓位、持仓摘要、当前候选、争议点、输出格式一起传入。
- 角色模板本身只定义职责、边界和输出格式，不替代真实上下文。
- 程序是工具库、执行器、监督器和电子围栏；agent 是主厨团队。模板应鼓励 agent 主动找机会、提方案、做替换判断和盘后进化，而不是把系统降级成固定问答机。
- 不要把交互压成固定五类 FAQ。若用户问的是机会票、活跃度、为什么没满仓、该不该换仓、该不该调参，也应按真实业务意图组织子代理。
- 若用户点名分析一只并不在当前讨论链内的股票，主代理仍可先调用系统工具完成临时体检，再决定是否需要派发给研究/策略/风控进一步讨论。
- 主代理可自然闲聊，但只要问题落到股票、仓位、执行、风控、研究、调参、监督，就应优先组织真实接口和角色模板，而不是靠记忆编答。
- 若任务需要真实系统数据，统一通过 `/srv/projects/ashare-system-v2/scripts/ashare_api.sh` 访问。
- 若需要知道“能读什么接口、机器人面板怎么排、主线流程怎么跑”，优先读取：
  - `/system/agents/capability-map`
  - `/system/robot/console-layout`
  - `/system/workflow/mainline`
