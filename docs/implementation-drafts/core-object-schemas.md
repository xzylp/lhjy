# ashare-system-v2 核心对象 Schema 草案

> 状态: Draft  
> 版本: v0.1  
> 日期: 2026-04-04

本文档定义 3 个优先级最高的核心对象：

- `candidate_case`
- `param_change_event`
- `agent_score_state`

## 1. candidate_case

```json
{
  "case_id": "case-20260404-600519-SH",
  "symbol": "600519.SH",
  "name": "贵州茅台",
  "pool_membership": {
    "base_pool": true,
    "focus_pool": true,
    "execution_pool": false,
    "watchlist": true
  },
  "runtime_snapshot": {
    "rank": 1,
    "selection_score": 85.6,
    "action": "BUY",
    "score_breakdown": {
      "momentum": 0.82,
      "liquidity": 0.76,
      "price_bias": 0.68
    }
  },
  "opinions": [
    {
      "round": 1,
      "agent_id": "ashare-research",
      "stance": "support",
      "confidence": "high",
      "reasons": ["业绩确定性高"],
      "evidence_refs": ["research-summary:600519.SH"]
    },
    {
      "round": 1,
      "agent_id": "ashare-risk",
      "stance": "watch",
      "confidence": "medium",
      "reasons": ["当前仍为 paper 模式"],
      "evidence_refs": ["system-config", "system-audits"]
    }
  ],
  "round_1_summary": {
    "support_points": [],
    "oppose_points": [],
    "evidence_gaps": [],
    "questions_for_round_2": []
  },
  "round_2_summary": {
    "resolved_points": [],
    "remaining_disputes": []
  },
  "risk_gate": "limit",
  "audit_gate": "clear",
  "final_status": "watchlist",
  "selected_reason": null,
  "rejected_reason": null,
  "intraday_state": "observing",
  "updated_at": "2026-04-04T15:00:00+08:00"
}
```

字段要求：

- `case_id`
  - 全局唯一
- `pool_membership`
  - 反映在各层股票池中的归属
- `opinions`
  - 保存每轮各 Agent 的原始观点
- `risk_gate`
  - `allow / limit / reject`
- `audit_gate`
  - `clear / hold`
- `final_status`
  - `selected / watchlist / rejected`

## 2. param_change_event

```json
{
  "event_id": "param-20260404-watchlist-capacity-001",
  "event_type": "param_change",
  "param_key": "watchlist_capacity",
  "scope": "strategy",
  "old_value": 8,
  "new_value": 5,
  "allowed_range": [3, 12],
  "effective_period": "next_trading_day",
  "effective_from": "2026-04-07",
  "effective_to": null,
  "source_layer": "agent_adjusted_params",
  "proposed_by": "user",
  "structured_by": "ashare",
  "evaluated_by": ["ashare-strategy", "ashare-risk"],
  "approved_by": "ashare-audit",
  "written_by": "ashare",
  "reason": "近期轮动加快，收缩 watchlist 提高聚焦度",
  "status": "approved",
  "created_at": "2026-04-04T15:05:00+08:00"
}
```

状态建议：

- `proposed`
- `evaluating`
- `approved`
- `rejected`
- `effective`
- `expired`
- `revoked`

## 3. agent_score_state

```json
{
  "agent_id": "ashare-strategy",
  "score_date": "2026-04-04",
  "old_score": 10.0,
  "result_score_delta": 1.2,
  "learning_score_delta": 0.4,
  "governance_score_delta": 0.0,
  "new_score": 11.6,
  "weight_bucket": "normal",
  "weight_value": 1.0,
  "governance_state": "normal_mode",
  "cases_evaluated": [
    {
      "symbol": "600519.SH",
      "stance": "support",
      "next_day_close_outcome": "correct",
      "delta": 0.6
    }
  ],
  "learning_updates": [
    {
      "proposal_id": "learn-20260404-001",
      "adopted": true,
      "verified_effective": true,
      "delta": 0.4
    }
  ],
  "updated_at": "2026-04-05T15:10:00+08:00"
}
```

权重桶建议：

- `high_credit`
- `normal`
- `low_credit`
- `weak_credit`
- `suspended`

治理状态建议：

- `normal_mode`
- `learning_mode`
- `suspended`
- `recovered_low_credit`
