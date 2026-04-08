"""DI 容器 — 全局单例管理"""

from functools import lru_cache

from .settings import AppSettings, load_settings


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return load_settings()


@lru_cache(maxsize=1)
def get_runtime_config_manager():
    from .runtime_config import RuntimeConfigManager

    settings = get_settings()
    return RuntimeConfigManager(settings.storage_root / "runtime_config.json")


@lru_cache(maxsize=1)
def get_execution_adapter():
    from .infra.adapters import build_execution_adapter
    settings = get_settings()
    return build_execution_adapter(settings.execution_mode, settings)


@lru_cache(maxsize=1)
def get_market_adapter():
    from .infra.market_adapter import build_market_adapter
    settings = get_settings()
    return build_market_adapter(settings.market_mode, settings)


@lru_cache(maxsize=1)
def get_audit_store():
    from .infra.audit_store import AuditStore
    settings = get_settings()
    return AuditStore(settings.storage_root / "audits.json")


@lru_cache(maxsize=1)
def get_runtime_state_store():
    from .infra.audit_store import StateStore
    settings = get_settings()
    return StateStore(settings.storage_root / "runtime_state.json")


@lru_cache(maxsize=1)
def get_research_state_store():
    from .infra.audit_store import StateStore
    settings = get_settings()
    return StateStore(settings.storage_root / "research_state.json")


@lru_cache(maxsize=1)
def get_meeting_state_store():
    from .infra.audit_store import StateStore
    settings = get_settings()
    return StateStore(settings.storage_root / "meeting_state.json")


@lru_cache(maxsize=1)
def get_monitor_state_store():
    from .infra.audit_store import StateStore

    settings = get_settings()
    return StateStore(settings.storage_root / "monitor_state.json")


@lru_cache(maxsize=1)
def get_monitor_state_service():
    from .data.archive import DataArchiveStore
    from .monitor.persistence import MonitorStateService

    settings = get_settings()
    return MonitorStateService(
        state_store=get_monitor_state_store(),
        config_mgr=get_runtime_config_manager(),
        archive_store=DataArchiveStore(settings.storage_root),
    )


@lru_cache(maxsize=1)
def get_feishu_notifier():
    from .notify.feishu import FeishuNotifier

    settings = get_settings()
    return FeishuNotifier(
        settings.notify.feishu_app_id,
        settings.notify.feishu_app_secret,
        settings.notify.feishu_chat_id,
    )


@lru_cache(maxsize=1)
def get_message_dispatcher():
    from .notify.dispatcher import MessageDispatcher

    return MessageDispatcher(get_feishu_notifier())


@lru_cache(maxsize=1)
def get_qmt_launcher():
    from .infra.qmt_launcher import QMTLauncher
    settings = get_settings()
    return QMTLauncher(settings.xtquant)


@lru_cache(maxsize=1)
def get_parameter_registry():
    from .governance.param_registry import ParameterRegistry
    return ParameterRegistry()


@lru_cache(maxsize=1)
def get_parameter_service():
    from .governance.param_service import ParameterService
    from .governance.param_store import ParameterEventStore

    settings = get_settings()
    return ParameterService(
        registry=get_parameter_registry(),
        store=ParameterEventStore(settings.storage_root / "param_change_events.json"),
    )


@lru_cache(maxsize=1)
def get_candidate_case_service():
    from .discussion.candidate_case import CandidateCaseService

    settings = get_settings()
    return CandidateCaseService(settings.storage_root / "candidate_cases.json")


@lru_cache(maxsize=1)
def get_discussion_cycle_service():
    from .discussion.discussion_service import DiscussionCycleService

    settings = get_settings()
    return DiscussionCycleService(
        settings.storage_root / "discussion_cycles.json",
        candidate_case_service=get_candidate_case_service(),
        parameter_service=get_parameter_service(),
    )


@lru_cache(maxsize=1)
def get_agent_score_service():
    from .learning.score_state import AgentScoreService

    settings = get_settings()
    return AgentScoreService(settings.storage_root / "agent_score_states.json")


@lru_cache(maxsize=1)
def get_strategy_registry():
    """创建并注册所有策略"""
    from .strategy.registry import StrategyRegistry
    from .strategy.momentum_strategy import MomentumStrategy
    from .strategy.reversion_strategy import ReversionStrategy
    from .strategy.breakout_strategy import BreakoutStrategy
    registry = StrategyRegistry()
    registry.register(MomentumStrategy())
    registry.register(ReversionStrategy())
    registry.register(BreakoutStrategy())
    return registry


def reset_container() -> None:
    get_settings.cache_clear()
    get_execution_adapter.cache_clear()
    get_market_adapter.cache_clear()
    get_runtime_config_manager.cache_clear()
    get_audit_store.cache_clear()
    get_runtime_state_store.cache_clear()
    get_research_state_store.cache_clear()
    get_meeting_state_store.cache_clear()
    get_monitor_state_store.cache_clear()
    get_monitor_state_service.cache_clear()
    get_feishu_notifier.cache_clear()
    get_message_dispatcher.cache_clear()
    get_qmt_launcher.cache_clear()
    get_parameter_registry.cache_clear()
    get_parameter_service.cache_clear()
    get_candidate_case_service.cache_clear()
    get_discussion_cycle_service.cache_clear()
    get_agent_score_service.cache_clear()
    get_strategy_registry.cache_clear()
