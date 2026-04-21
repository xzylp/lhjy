"""FastAPI 应用工厂"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .container import (
    get_audit_store,
    get_agent_score_service,
    get_candidate_case_service,
    get_catalog_service,
    get_document_index_service,
    get_discussion_state_store,
    get_discussion_cycle_service,
    get_execution_adapter,
    get_execution_gateway_state_store,
    get_feishu_longconn_state_store,
    get_history_store,
    get_message_dispatcher,
    get_market_adapter,
    get_meeting_state_store,
    get_monitor_state_service,
    get_position_watch_state_store,
    get_parameter_service,
    get_research_state_store,
    get_runtime_config_manager,
    get_runtime_state_store,
    get_settings,
    get_strategy_registry,
)
from .monitor.stock_pool import StockPoolManager
from .monitor.alert_engine import AlertEngine
from .precompute import DossierPrecomputeService
from .account_state import AccountStateService
from .strategy.screener import StockScreener
from .startup_recovery import StartupRecoveryService
from .infra.state_migration import migrate_legacy_state_files


def create_app() -> FastAPI:
    """创建 all-in-one FastAPI 应用"""
    settings = get_settings()
    migrate_legacy_state_files(settings.storage_root)
    execution_adapter = get_execution_adapter()
    market_adapter = get_market_adapter()
    audit_store = get_audit_store()
    runtime_state_store = get_runtime_state_store()
    research_state_store = get_research_state_store()
    meeting_state_store = get_meeting_state_store()
    discussion_state_store = get_discussion_state_store()
    execution_gateway_state_store = get_execution_gateway_state_store()
    position_watch_state_store = get_position_watch_state_store()
    parameter_service = get_parameter_service()
    candidate_case_service = get_candidate_case_service()
    discussion_cycle_service = get_discussion_cycle_service()
    agent_score_service = get_agent_score_service()
    monitor_state_service = get_monitor_state_service()
    feishu_longconn_state_store = get_feishu_longconn_state_store()
    catalog_service = get_catalog_service()
    document_index_service = get_document_index_service()
    history_store = get_history_store()
    message_dispatcher = get_message_dispatcher()
    document_index_service.index_workspace_documents(settings.workspace)
    startup_recovery_service = StartupRecoveryService(execution_adapter, meeting_state_store)
    account_state_service = AccountStateService(
        settings,
        execution_adapter,
        meeting_state_store,
        config_mgr=get_runtime_config_manager(),
        parameter_service=parameter_service,
    )

    async def _run_startup_housekeeping() -> None:
        if settings.run_mode != "live":
            return
        try:
            account_state_payload = await asyncio.to_thread(
                account_state_service.snapshot,
                settings.xtquant.account_id,
                True,
                include_trades=False,
            )
            if audit_store:
                audit_store.append(
                    category="execution",
                    message="服务启动完成账户状态刷新",
                    payload={
                        "account_id": settings.xtquant.account_id,
                        "status": account_state_payload.get("status"),
                        "verified": account_state_payload.get("verified"),
                        "config_match": account_state_payload.get("config_match"),
                    },
                )
        except Exception as exc:
            if audit_store:
                audit_store.append(
                    category="execution",
                    message="服务启动账户状态刷新失败",
                    payload={"error": str(exc)},
                )

        try:
            startup_payload = await asyncio.to_thread(
                startup_recovery_service.recover,
                settings.xtquant.account_id,
                True,
            )
            if audit_store:
                audit_store.append(
                    category="execution",
                    message="服务启动完成执行恢复扫描",
                    payload={
                        "account_id": settings.xtquant.account_id,
                        "status": startup_payload.get("status"),
                        "order_count": startup_payload.get("order_count", 0),
                        "pending_count": startup_payload.get("pending_count", 0),
                        "orphan_count": startup_payload.get("orphan_count", 0),
                    },
                )
        except Exception as exc:
            if audit_store:
                audit_store.append(
                    category="execution",
                    message="服务启动恢复扫描失败",
                    payload={"error": str(exc)},
                )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        startup_task = asyncio.create_task(_run_startup_housekeeping(), name="startup-housekeeping")
        _app.state.startup_housekeeping_task = startup_task
        if audit_store:
            audit_store.append(
                category="execution",
                message="服务启动后台恢复任务已提交",
                payload={"account_id": settings.xtquant.account_id},
            )
        yield
        if not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await startup_task

    # 运行时动态配置
    config_mgr = get_runtime_config_manager()
    dossier_precompute_service = DossierPrecomputeService(
        settings=settings,
        market_adapter=market_adapter,
        research_state_store=research_state_store,
        runtime_state_store=runtime_state_store,
        candidate_case_service=candidate_case_service,
        config_mgr=config_mgr,
    )

    app = FastAPI(
        title="ashare-system-v2",
        version="0.2.0",
        description="A股多Agent自动化量化交易系统",
        lifespan=lifespan,
    )

    # 系统路由 (含运行时配置 API)
    from .apps.system_api import build_router as build_system_router
    app.include_router(
        build_system_router(
            settings=settings,
            config_mgr=config_mgr,
            audit_store=audit_store,
            runtime_state_store=runtime_state_store,
            research_state_store=research_state_store,
            meeting_state_store=meeting_state_store,
            discussion_state_store=discussion_state_store,
            execution_gateway_state_store=execution_gateway_state_store,
            position_watch_state_store=position_watch_state_store,
            parameter_service=parameter_service,
            candidate_case_service=candidate_case_service,
            discussion_cycle_service=discussion_cycle_service,
            agent_score_service=agent_score_service,
            monitor_state_service=monitor_state_service,
            feishu_longconn_state_store=feishu_longconn_state_store,
            message_dispatcher=message_dispatcher,
            market_adapter=market_adapter,
            execution_adapter=execution_adapter,
            dossier_precompute_service=dossier_precompute_service,
        )
    )

    # 行情路由
    from .apps.market_api import build_router as build_market_router
    app.include_router(build_market_router(market_adapter, getattr(market_adapter, "mode", settings.market_mode)))

    # 交易执行路由
    from .apps.execution_api import build_router as build_execution_router
    app.include_router(
        build_execution_router(
            execution_adapter,
            getattr(execution_adapter, "mode", settings.execution_mode),
            dispatcher=message_dispatcher,
            meeting_state_store=meeting_state_store,
            execution_plane=settings.execution_plane,
        )
    )

    # 报告路由
    from .apps.report_api import build_router as build_report_router
    app.include_router(build_report_router(settings.logs_dir / "reports"))

    # 数据 serving 路由
    from .apps.data_api import build_router as build_data_router
    app.include_router(build_data_router(settings))

    # 监控路由
    from .apps.monitor_api import build_router as build_monitor_router
    pool_mgr = StockPoolManager()
    alert_engine = AlertEngine()
    app.include_router(
        build_monitor_router(
            pool_mgr,
            alert_engine,
            monitor_state_service,
            discussion_cycle_service=discussion_cycle_service,
            settings=settings,
        )
    )

    # 策略路由
    from .apps.strategy_api import build_router as build_strategy_router
    strategy_registry = get_strategy_registry()
    screener = StockScreener()
    app.include_router(build_strategy_router(settings, strategy_registry, screener))

    # 运行时路由
    from .apps.runtime_api import build_router as build_runtime_router
    app.include_router(
        build_runtime_router(
            settings=settings,
            market_adapter=market_adapter,
            execution_adapter=execution_adapter,
            strategy_registry=strategy_registry,
            screener=screener,
            pool_mgr=pool_mgr,
            audit_store=audit_store,
            runtime_state_store=runtime_state_store,
            meeting_state_store=meeting_state_store,
            research_state_store=research_state_store,
            config_mgr=config_mgr,
            parameter_service=parameter_service,
            candidate_case_service=candidate_case_service,
            monitor_state_service=monitor_state_service,
            message_dispatcher=message_dispatcher,
            dossier_precompute_service=dossier_precompute_service,
        )
    )

    # 研究路由
    from .apps.research_api import build_router as build_research_router
    app.include_router(
        build_research_router(
            settings=settings,
            audit_store=audit_store,
            research_state_store=research_state_store,
        )
    )

    # 检索路由
    from .apps.search_api import build_router as build_search_router
    app.include_router(
        build_search_router(
            document_index=document_index_service,
            catalog_service=catalog_service,
            history_store=history_store,
            runtime_state_store=runtime_state_store,
        )
    )

    # Hermes 通用控制平台路由
    from .apps.hermes_api import build_router as build_hermes_router
    app.include_router(
        build_hermes_router(
            settings=settings,
            audit_store=audit_store,
            runtime_state_store=runtime_state_store,
            research_state_store=research_state_store,
            meeting_state_store=meeting_state_store,
            agent_score_service=agent_score_service,
            monitor_state_service=monitor_state_service,
        )
    )

    # 仪表盘路由 (SPA)
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    import os

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)

    @app.get("/dashboard/{full_path:path}")
    async def dashboard_spa(full_path: str):
        # 1. 尝试直接查找静态文件 (assets, favicon 等)
        file_path = os.path.join(static_dir, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        
        # 2. 如果文件不存在，且不带后缀，则可能是 SPA 路由，返回 index.html
        index_file = os.path.join(static_dir, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        
        return {"detail": "Dashboard frontend not built. Please run 'npm run build' in web directory."}

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": settings.app_name,
            "mode": settings.run_mode,
            "environment": settings.environment,
        }

    return app
