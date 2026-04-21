"""启动器 — CLI 入口，含 QMT 自动启动"""

from __future__ import annotations

import argparse
import threading

import uvicorn

from .feishu_longconn import run_feishu_long_connection
from .container import (
    get_candidate_case_service,
    get_market_adapter,
    get_research_state_store,
    get_runtime_config_manager,
    get_runtime_state_store,
)
from .logging_config import setup_logging, get_logger
from .precompute import DossierPrecomputeService
from .settings import load_settings

logger = get_logger("run")


def _start_qmt_if_needed(settings) -> bool:
    """在 xtquant 模式下自动启动 QMT"""
    if settings.execution_mode != "xtquant" and settings.market_mode != "xtquant":
        return True
    if not settings.xtquant.auto_start:
        return True
    from .infra.qmt_launcher import QMTLauncher
    launcher = QMTLauncher(settings.xtquant)
    ok = launcher.ensure_running()
    if ok:
        logger.info("QMT 就绪")
        # 后台守护线程
        t = threading.Thread(target=launcher.watchdog_loop, daemon=True)
        t.start()
    else:
        if settings.run_mode == "live":
            raise RuntimeError("live 模式下 QMT 启动失败，禁止继续启动服务")
        logger.warning("QMT 启动失败，将以 mock-fallback 模式运行")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="ashare-system-v2 启动器")
    parser.add_argument(
        "command",
        choices=["serve", "healthcheck", "scheduler", "refresh-profiles", "feishu-longconn"],
        default="serve",
        nargs="?",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--trade-date", default=None)
    parser.add_argument("--source", default="candidate_pool")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--bot-role", default=None)
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.logs_dir)
    logger.info("ashare-system-v2 启动 (mode=%s, env=%s)", settings.run_mode, settings.environment)

    if args.command == "healthcheck":
        from .infra.healthcheck import run_healthcheck
        run_healthcheck(settings)
        return

    if args.command == "scheduler":
        from .scheduler import run_scheduler
        run_scheduler(dry_run=args.dry_run or settings.run_mode == "dry-run")
        return

    if args.command == "refresh-profiles":
        symbols = list(args.symbols or [])
        precompute_service = DossierPrecomputeService(
            settings=settings,
            market_adapter=get_market_adapter(),
            research_state_store=get_research_state_store(),
            runtime_state_store=get_runtime_state_store(),
            candidate_case_service=get_candidate_case_service(),
            config_mgr=get_runtime_config_manager(),
        )
        result = precompute_service.refresh_behavior_profiles(
            trade_date=args.trade_date,
            symbols=symbols,
            source=args.source,
            limit=args.limit,
            force=args.force,
            trigger="cli",
        )
        if result.get("ok"):
            logger.info(
                "画像刷新完成: trade_date=%s symbol_count=%s profile_count=%s coverage=%s",
                result.get("trade_date"),
                result.get("symbol_count"),
                result.get("profile_count"),
                result.get("coverage_ratio"),
            )
        else:
            logger.warning(
                "画像刷新未执行: reason=%s trade_date=%s",
                result.get("reason"),
                result.get("trade_date"),
            )
        return

    if args.command == "feishu-longconn":
        run_feishu_long_connection(settings, bot_role=args.bot_role)
        return

    # serve 模式: 先启动 QMT，再启动 FastAPI
    _start_qmt_if_needed(settings)

    host = args.host or settings.service.host
    port = args.port or settings.service.port
    logger.info("FastAPI 服务启动: http://%s:%d", host, port)

    uvicorn.run(
        "ashare_system.app:create_app",
        factory=True,
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
