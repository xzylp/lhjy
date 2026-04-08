# ashare-system-v2 状态转移表草案

> 状态: Draft  
> 版本: v0.1  
> 日期: 2026-04-04

本文档把主设计稿中的总状态机拆成可实现的转移表。

## 1. Pool State

| From | Event | Guard | To | Owner |
|------|------|------|------|------|
| `day_open` | scheduler start | trading_day = true | `base_pool_ready` | `ashare-runtime` |
| `base_pool_ready` | round-1 dispatch | pool_count >= 10 | `focus_pool_building` | `ashare` |
| `focus_pool_building` | round-1 summary done | discussion available | `focus_pool_ready` | `ashare` |
| `focus_pool_ready` | round-2 summary done | final review complete | `execution_pool_building` | `ashare` |
| `execution_pool_building` | risk + audit clear | selected_count in [1,3] | `execution_pool_ready` | `ashare` |
| `execution_pool_building` | risk reject or audit hold | selected_count = 0 or blocked | `execution_pool_blocked` | `ashare-risk` / `ashare-audit` |

## 2. Discussion State

| From | Event | Guard | To | Owner |
|------|------|------|------|------|
| `idle` | pool ready | base_pool exists | `round_1_running` | `ashare` |
| `round_1_running` | all opinions received | research + strategy + risk + audit present | `round_1_summarized` | `ashare` |
| `round_1_summarized` | round-2 trigger | disputes or top candidates exist | `round_2_running` | `ashare` |
| `round_2_running` | all replies received | key disputes answered | `final_review_ready` | `ashare` |
| `final_review_ready` | final merge | risk != reject and audit != hold | `final_selection_ready` | `ashare` |
| `final_review_ready` | blocking outcome | reject or hold | `final_selection_blocked` | `ashare-risk` / `ashare-audit` |

## 3. Parameter State

| From | Event | Guard | To | Owner |
|------|------|------|------|------|
| `param_idle` | natural-language request | request parseable | `param_proposal_structured` | `ashare` |
| `param_proposal_structured` | evaluation start | scope valid | `param_evaluating` | `ashare` |
| `param_evaluating` | audit approve | range valid and no hard-rule conflict | `param_approved` | `ashare-audit` |
| `param_evaluating` | audit reject | invalid range or blocked | `param_rejected` | `ashare-audit` |
| `param_approved` | write current value | persistence ok | `param_effective` | `ashare` |
| `param_effective` | period expires | effective_period elapsed | `param_expired` | runtime |
| `param_effective` | explicit revoke | approved revoke event | `param_revoked` | `ashare-audit` |

## 4. Intraday State

| From | Event | Guard | To | Owner |
|------|------|------|------|------|
| `observing` | monitor event | trigger_level in {medium, high} | `intraday_review` | `ashare-runtime` |
| `intraday_review` | structure + liquidity + risk pass | all true | `t_ready` | `ashare-strategy` + `ashare-risk` |
| `intraday_review` | block decision | any hard fail | `intraday_blocked` | `ashare-risk` |
| `t_ready` | order intent accepted | executor healthy | `t_executing` | `ashare-executor` |
| `t_executing` | take profit / stop / tail exit | exit rule met | `t_closed` | `ashare-executor` |
| `t_executing` | execution fail | broker/runtime error | `execution_blocked` | `ashare-executor` |

## 5. Score State

| From | Event | Guard | To | Owner |
|------|------|------|------|------|
| `market_close` | settlement batch start | trading_day ended | `next_day_validation_pending` | scheduler |
| `next_day_validation_pending` | next-day close | next close data ready | `next_day_close_verified` | scheduler |
| `next_day_close_verified` | score calc | opinions + outcomes present | `score_delta_calculated` | learning module |
| `score_delta_calculated` | persist score | score within [0,20] | `score_updated` | learning module |
| `score_updated` | governance check | score = 0 | `agent_suspended` | `ashare` + `ashare-audit` |
| `score_updated` | governance check | score in [1,9] | `learning_mode` | learning module |
| `score_updated` | governance check | score >= 10 | `normal_mode` | learning module |
| `agent_suspended` | verified recovery | approved learning improvement | `recovered_low_credit` | `ashare-audit` |

## 6. Cross-Domain Triggers

| Trigger Source | Event | Target Domain | Expected Effect |
|------|------|------|------|
| Parameter | `param_effective` | Pool / Discussion / Intraday | New parameter values take effect |
| Discussion | `final_selection_ready` | Intraday | Selected and watchlist names enter active monitoring |
| Score | `score_updated` | Discussion | Agent weights change for next round |
| Score | `agent_suspended` | Discussion / Param | Suspended agent loses key voting rights |
| Intraday | `intraday_blocked` | Audit | Record mid-session block event |
