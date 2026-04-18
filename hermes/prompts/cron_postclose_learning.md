# 盘后学习 cron 模板

你在执行 A 股交易台的盘后学习任务。

任务目标：

1. 汇总当日 runtime、research、meeting、audit、postclose 报告。
2. 识别：
   - `missed_opportunities`
   - `late_discoveries`
   - `risk_overblocking`
   - `unsupported_promotions`
   - `proposal_candidates`
3. 生成简明学习摘要，供次日治理与 prompt patch 使用。

输出要求：

- 先列事实，再列归因，再列改进建议。
- 缺少关键报告时要明确写“证据不足”。
- 若没有值得上报的增量，只输出 `[SILENT]`。
