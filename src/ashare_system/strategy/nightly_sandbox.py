"""夜间沙盘推演引擎。

23:00 自动执行，从 T 日 finalize 结果中提取 watchlist，
模拟参数调整后的讨论结果，输出次日优先标的。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..contracts import SandboxResult
from ..logging_config import get_logger

logger = get_logger("strategy.nightly_sandbox")

# 沙盘模拟的最大 watchlist 标的数
MAX_SIMULATION_TARGETS = 5


class NightlySandbox:
    """夜间沙盘推演引擎。

    工作流：
    1. 从 T 日 finalize_bundle 中提取 watchlist 前 N 名
    2. 对每支票读取 attribution 报告的 parameter_hints
    3. 模拟"如果按 hint 建议调参，讨论结果是否会改变"
    4. 输出 tomorrow_priority 标记
    """

    def __init__(
        self,
        storage_root: Path,
        now_factory: Callable[[], datetime] | None = None,
        replay_packet_builder: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._storage_root = storage_root
        self._output_dir = storage_root / "nightly_deliberation"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._now_factory = now_factory or datetime.now
        self._replay_packet_builder = replay_packet_builder

    def run_simulation(
        self,
        *,
        trade_date: str,
        finalize_bundle: dict[str, Any],
        attribution_report: dict[str, Any],
        parameter_hints: list[dict[str, Any]] | None = None,
    ) -> SandboxResult:
        """执行夜间沙盘推演。

        Args:
            trade_date: 当日交易日
            finalize_bundle: T 日的 discussion finalize 结果
            attribution_report: T 日的归因报告
            parameter_hints: T 日的参数调优建议

        Returns:
            SandboxResult，包含次日优先标的和模拟日志

        TODO:
            1. 实现 _extract_watchlist() — 从 finalize_bundle 提取 watchlist 排名
            2. 实现 _simulate_param_adjustment() — 模拟参数调整后的影响
            3. 实现 _replay_discussion() — 回放讨论流程看结果是否改变
            4. 集成 discussion_service.build_openclaw_replay_proposal_packet()
        """
        now = self._now_factory()
        simulation_log: list[str] = []
        tomorrow_priorities: list[str] = []
        missed_opportunities: list[dict] = []

        # Step 1: 提取 watchlist
        watchlist = self._extract_watchlist(finalize_bundle)
        targets = watchlist[:MAX_SIMULATION_TARGETS]
        simulation_log.append(f"沙盘推演启动: trade_date={trade_date}, watchlist={len(watchlist)}, 模拟目标={len(targets)}")

        if not targets:
            simulation_log.append("watchlist 为空，跳过沙盘推演。")
            result = SandboxResult(
                trade_date=trade_date,
                generated_at=now.isoformat(),
                simulation_log=simulation_log,
                summary_lines=["watchlist 为空，无需推演。"],
            )
            self._save_result(trade_date, result)
            return result

        # Step 2: 对每个目标进行模拟
        for symbol_data in targets:
            symbol = str(symbol_data.get("symbol", ""))
            if not symbol:
                continue

            # 模拟参数调整
            adj_result = self._simulate_param_adjustment(
                symbol=symbol,
                symbol_data=symbol_data,
                attribution_report=attribution_report,
                parameter_hints=parameter_hints or [],
            )
            simulation_log.append(f"  [{symbol}] 参数模拟: {adj_result.get('summary', '无变化')}")
            replay_summary = self._build_replay_summary(symbol)
            if replay_summary:
                simulation_log.append(f"  [{symbol}] 讨论回放: {replay_summary}")

            # 判断是否应该进入次日优先列表
            if adj_result.get("should_promote", False):
                tomorrow_priorities.append(symbol)
                missed_opportunities.append({
                    "symbol": symbol,
                    "reason": adj_result.get("reason", ""),
                    "simulated_score_delta": adj_result.get("score_delta", 0.0),
                })
                simulation_log.append(f"  [{symbol}] → 加入次日优先列表")

        # Step 3: 生成结果
        summary_lines = [
            f"沙盘推演完成: 模拟 {len(targets)} 支，次日优先 {len(tomorrow_priorities)} 支。",
        ]
        if tomorrow_priorities:
            summary_lines.append(f"次日优先标的: {', '.join(tomorrow_priorities)}")

        result = SandboxResult(
            trade_date=trade_date,
            generated_at=now.isoformat(),
            tomorrow_priorities=tomorrow_priorities,
            missed_opportunities=missed_opportunities,
            simulation_log=simulation_log,
            summary_lines=summary_lines,
        )
        self._save_result(trade_date, result)
        logger.info("夜间沙盘推演完成: trade_date=%s priorities=%s", trade_date, tomorrow_priorities)
        return result

    def load_result(self, trade_date: str) -> SandboxResult | None:
        """加载指定交易日的沙盘推演结果。"""
        path = self._output_dir / f"{trade_date}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SandboxResult.model_validate(data)
        except Exception:
            logger.warning("沙盘结果加载失败: %s", path)
            return None

    def get_tomorrow_priorities(self, trade_date: str) -> list[str]:
        """获取指定日期的次日优先标的列表（供盘前买入决策使用）。"""
        result = self.load_result(trade_date)
        return result.tomorrow_priorities if result else []

    def _extract_watchlist(self, finalize_bundle: dict[str, Any]) -> list[dict]:
        """从 finalize_bundle 提取 watchlist 标的。

        TODO: 解析 finalize_bundle 的 reply_pack/reason_board，
              提取 final_status == 'watchlist' 的标的并按评分排序
        """
        reply_pack = finalize_bundle.get("reply_pack", {})
        watchlist_items: list[dict] = []
        for symbol, item in reply_pack.items() if isinstance(reply_pack, dict) else []:
            if isinstance(item, dict) and item.get("final_status") in {"watchlist", "selected"}:
                watchlist_items.append({"symbol": symbol, **item})
        if watchlist_items:
            return sorted(watchlist_items, key=lambda x: float(x.get("score", x.get("selection_score", 0.0)) or 0.0), reverse=True)

        for item in list(finalize_bundle.get("watchlist") or []):
            if isinstance(item, dict) and item.get("symbol"):
                watchlist_items.append(dict(item))
        for item in list(finalize_bundle.get("selected") or []):
            if isinstance(item, dict) and item.get("symbol"):
                candidate = dict(item)
                candidate.setdefault("final_status", "selected")
                watchlist_items.append(candidate)
        return sorted(
            watchlist_items,
            key=lambda x: float(x.get("score", x.get("selection_score", 0.0)) or 0.0),
            reverse=True,
        )

    def _simulate_param_adjustment(
        self,
        *,
        symbol: str,
        symbol_data: dict,
        attribution_report: dict[str, Any],
        parameter_hints: list[dict],
    ) -> dict[str, Any]:
        """模拟参数调整对该标的的影响。

        TODO:
            1. 从 parameter_hints 中找到与该标的相关的建议
            2. 模拟调整后的评分变化
            3. 回放讨论逻辑判断是否会从 watchlist → selected
        """
        hints = [item for item in parameter_hints if self._hint_matches_symbol(item, symbol=symbol)]
        report_items = list(attribution_report.get("items") or [])
        symbol_report_items = [item for item in report_items if str(item.get("symbol") or "") == symbol]
        base_score = float(symbol_data.get("score", symbol_data.get("selection_score", 0.0)) or 0.0)
        score_delta = 0.0
        reasons: list[str] = []

        for hint in hints:
            action = str(hint.get("action") or hint.get("suggested_action") or "").lower()
            weight = float(hint.get("weight", hint.get("confidence", 0.0)) or 0.0)
            if action in {"relax", "boost", "promote", "increase"}:
                score_delta += max(weight, 0.5)
                reasons.append(str(hint.get("reason") or hint.get("summary") or "参数提示偏正向"))
            elif action in {"tighten", "reduce", "suppress", "block"}:
                score_delta -= max(weight, 0.5)
                reasons.append(str(hint.get("reason") or hint.get("summary") or "参数提示偏负向"))

        avg_next_day_close_pct = 0.0
        if symbol_report_items:
            avg_next_day_close_pct = sum(
                float(item.get("next_day_close_pct", 0.0) or 0.0)
                for item in symbol_report_items
            ) / len(symbol_report_items)
            if avg_next_day_close_pct <= -0.02:
                score_delta += 1.5
                reasons.append(f"历史归因显示该票次日均收益 {avg_next_day_close_pct:.2%}，值得前置复盘")
            elif avg_next_day_close_pct >= 0.02:
                score_delta -= 0.5
                reasons.append(f"历史归因显示该票次日表现尚可 {avg_next_day_close_pct:.2%}")

        should_promote = base_score + score_delta >= max(base_score + 1.0, 2.0) and score_delta > 0
        return {
            "summary": f"hint={len(hints)} score_delta={score_delta:+.2f}",
            "should_promote": should_promote,
            "reason": "；".join(reasons[:3]),
            "score_delta": round(score_delta, 4),
        }

    def _build_replay_summary(self, symbol: str) -> str:
        if self._replay_packet_builder is None:
            return ""
        try:
            payload = self._replay_packet_builder(symbol)
        except Exception as exc:
            logger.warning("构建 replay packet 失败: symbol=%s error=%s", symbol, exc)
            return "回放构建失败"
        summary_lines = list(payload.get("summary_lines") or payload.get("replay_packet", {}).get("summary_lines") or [])
        if not summary_lines:
            contradiction_lines = list(payload.get("contradiction_summary_lines") or [])
            summary_lines = contradiction_lines
        return summary_lines[0] if summary_lines else ""

    @staticmethod
    def _hint_matches_symbol(hint: dict[str, Any], *, symbol: str) -> bool:
        if str(hint.get("symbol") or "") == symbol:
            return True
        symbols = hint.get("symbols")
        if isinstance(symbols, list):
            return symbol in {str(item) for item in symbols}
        scope = str(hint.get("scope") or "")
        target = str(hint.get("target") or "")
        return symbol in {scope, target}

    def _save_result(self, trade_date: str, result: SandboxResult) -> None:
        """保存推演结果到 JSON 文件。"""
        path = self._output_dir / f"{trade_date}.json"
        path.write_text(
            json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
