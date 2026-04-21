from os import getenv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .data.storage import ensure_storage_layout


RunMode = Literal["dry-run", "paper", "live"]


def _load_dotenv() -> None:
    """自动加载项目根目录的 .env 文件"""
    try:
        from dotenv import load_dotenv
        here = Path(__file__).parent
        for _ in range(5):
            env_file = here / ".env"
            if env_file.exists():
                load_dotenv(env_file, override=False)
                return
            here = here.parent
    except ImportError:
        pass


_load_dotenv()

def _env(name: str, default: str) -> str:
    return getenv(name, default)


def _env_int(name: str, default: int) -> int:
    val = getenv(name)
    return int(val) if val else default


def _env_float(name: str, default: float) -> float:
    val = getenv(name)
    return float(val) if val else default


def _env_bool(name: str, default: bool = False) -> bool:
    val = getenv(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


def _env_path(name: str, default: str) -> Path:
    return Path(getenv(name, default))


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(path: Path, base: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()


class XtQuantSettings(BaseModel):
    """QMT 交易终端配置"""
    root: Path = Field(default_factory=lambda: _env_path("ASHARE_XTQUANT_ROOT", r"D:\国金证券QMT交易端"))
    service_root: Path = Field(default_factory=lambda: _env_path("ASHARE_XTQUANTSERVICE_ROOT", r"D:\Coding\lhjy\xtquantservice"))
    userdata: Path = Field(default_factory=lambda: _env_path("ASHARE_XTQUANT_USERDATA", r"D:\国金证券QMT交易端\userdata_mini"))
    market_host: str = Field(default_factory=lambda: _env("ASHARE_XTQUANT_MARKET_HOST", "localhost"))
    market_port: int | None = Field(default_factory=lambda: _env_int("ASHARE_XTQUANT_MARKET_PORT", 0) or None)
    session_id: int = Field(default_factory=lambda: _env_int("ASHARE_XTQUANT_SESSION_ID", 8890130545))
    account_id: str = Field(default_factory=lambda: _env("ASHARE_XTQUANT_ACCOUNT_ID", "8890130545"))
    account_type: str = Field(default_factory=lambda: _env("ASHARE_XTQUANT_ACCOUNT_TYPE", "STOCK"))
    # QMT 自动启动
    exe_path: Path = Field(default_factory=lambda: _env_path("ASHARE_QMT_EXE", r"D:\国金证券QMT交易端\bin.x64\XtMiniQmt.exe"))
    password: str = Field(default_factory=lambda: _env("ASHARE_QMT_PASSWORD", ""))
    auto_start: bool = Field(default_factory=lambda: _env_bool("ASHARE_QMT_AUTO_START", True))
    startup_wait_sec: int = Field(default_factory=lambda: _env_int("ASHARE_QMT_STARTUP_WAIT_SEC", 30))


class ServiceSettings(BaseModel):
    """服务端口与探针配置"""
    host: str = Field(default_factory=lambda: _env("ASHARE_SERVICE_HOST", "127.0.0.1"))
    port: int = Field(default_factory=lambda: _env_int("ASHARE_SERVICE_PORT", 8100))
    probe_timeout_sec: float = Field(default_factory=lambda: _env_float("ASHARE_SERVICE_PROBE_TIMEOUT_SEC", 3.0))
    public_base_url: str = Field(default_factory=lambda: _env("ASHARE_PUBLIC_BASE_URL", ""))


class NotifySettings(BaseModel):
    """通知配置"""
    feishu_app_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_APP_ID", ""))
    feishu_app_secret: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_APP_SECRET", ""))
    feishu_chat_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_CHAT_ID", ""))
    feishu_important_chat_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_IMPORTANT_CHAT_ID", ""))
    feishu_supervision_chat_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_SUPERVISION_CHAT_ID", ""))
    feishu_verification_token: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_VERIFICATION_TOKEN", ""))
    feishu_control_plane_base_url: str = Field(
        default_factory=lambda: _env("ASHARE_FEISHU_CONTROL_PLANE_BASE_URL", "")
    )
    feishu_bot_role: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_BOT_ROLE", "main"))
    feishu_bot_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_BOT_ID", ""))
    feishu_bot_name: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_BOT_NAME", ""))
    feishu_supervision_app_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_SUPERVISION_APP_ID", ""))
    feishu_supervision_app_secret: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_SUPERVISION_APP_SECRET", ""))
    feishu_supervision_bot_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_SUPERVISION_BOT_ID", ""))
    feishu_supervision_bot_name: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_SUPERVISION_BOT_NAME", ""))
    feishu_execution_app_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_EXECUTION_APP_ID", ""))
    feishu_execution_app_secret: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_EXECUTION_APP_SECRET", ""))
    feishu_execution_bot_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_EXECUTION_BOT_ID", ""))
    feishu_execution_bot_name: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_EXECUTION_BOT_NAME", ""))
    alerts_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_ALERTS_ENABLED"))

    def get_feishu_bot_config(self, role: str = "main") -> dict[str, str]:
        resolved_role = str(role or "main").strip().lower()
        if resolved_role in {"supervision", "督办", "monitor"}:
            return {
                "role": "supervision",
                "app_id": self.feishu_supervision_app_id,
                "app_secret": self.feishu_supervision_app_secret,
                "bot_id": self.feishu_supervision_bot_id,
                "bot_name": self.feishu_supervision_bot_name or "Hermes督办",
                "chat_id": self.feishu_supervision_chat_id or self.feishu_chat_id,
                "important_chat_id": self.feishu_supervision_chat_id or self.feishu_important_chat_id or self.feishu_chat_id,
                "supervision_chat_id": self.feishu_supervision_chat_id or self.feishu_chat_id,
            }
        if resolved_role in {"execution", "回执", "trade"}:
            return {
                "role": "execution",
                "app_id": self.feishu_execution_app_id,
                "app_secret": self.feishu_execution_app_secret,
                "bot_id": self.feishu_execution_bot_id,
                "bot_name": self.feishu_execution_bot_name or "Hermes回执",
                "chat_id": self.feishu_chat_id,
                "important_chat_id": self.feishu_important_chat_id or self.feishu_chat_id,
                "supervision_chat_id": self.feishu_supervision_chat_id or self.feishu_chat_id,
            }
        return {
            "role": "main",
            "app_id": self.feishu_app_id,
            "app_secret": self.feishu_app_secret,
            "bot_id": self.feishu_bot_id,
            "bot_name": self.feishu_bot_name or "Hermes主控",
            "chat_id": self.feishu_chat_id,
            "important_chat_id": self.feishu_important_chat_id or self.feishu_chat_id,
            "supervision_chat_id": self.feishu_supervision_chat_id or self.feishu_chat_id,
        }

    def list_feishu_bot_configs(self) -> list[dict[str, str]]:
        configs = [
            self.get_feishu_bot_config("main"),
            self.get_feishu_bot_config("supervision"),
            self.get_feishu_bot_config("execution"),
        ]
        deduped: list[dict[str, str]] = []
        seen_roles: set[str] = set()
        for item in configs:
            role = str(item.get("role") or "").strip()
            if not role or role in seen_roles:
                continue
            seen_roles.add(role)
            deduped.append(item)
        return deduped


class WindowsGatewaySettings(BaseModel):
    """Windows HTTP 交易桥配置"""
    base_url: str = Field(default_factory=lambda: _env("ASHARE_WINDOWS_GATEWAY_BASE_URL", ""))
    token: str = Field(default_factory=lambda: _env("ASHARE_WINDOWS_GATEWAY_TOKEN", ""))
    token_file: str = Field(default_factory=lambda: _env("ASHARE_WINDOWS_GATEWAY_TOKEN_FILE", ""))
    timeout_sec: float = Field(default_factory=lambda: _env_float("ASHARE_WINDOWS_GATEWAY_TIMEOUT_SEC", 10.0))


class GoPlatformSettings(BaseModel):
    """Linux 本地 Go 并发数据平台配置"""
    base_url: str = Field(default_factory=lambda: _env("ASHARE_GO_PLATFORM_BASE_URL", "http://127.0.0.1:18793"))
    timeout_sec: float = Field(default_factory=lambda: _env_float("ASHARE_GO_PLATFORM_TIMEOUT_SEC", 15.0))
    enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_GO_PLATFORM_ENABLED", False))


class HermesSettings(BaseModel):
    """Hermes 控制台模型与路由配置"""

    default_model: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MODEL_DEFAULT", "MiniMax-M2.7"))
    fast_model: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MODEL_FAST", "MiniMax-M2.7"))
    deep_model: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MODEL_DEEP", "gpt-5.4"))
    execution_guard_model: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MODEL_EXECUTION", "gpt-5.4"))
    minimax_provider_name: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MINIMAX_PROVIDER_NAME", "MiniMax"))
    minimax_model: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MINIMAX_MODEL", "MiniMax-M2.7"))
    minimax_base_url: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MINIMAX_BASE_URL", ""))
    minimax_api_key: str = Field(default_factory=lambda: _env("ASHARE_HERMES_MINIMAX_API_KEY", ""))
    compat_provider_name: str = Field(default_factory=lambda: _env("ASHARE_HERMES_COMPAT_PROVIDER_NAME", "Chunfeng"))
    compat_base_url: str = Field(default_factory=lambda: _env("ASHARE_HERMES_COMPAT_BASE_URL", ""))
    compat_api_key: str = Field(default_factory=lambda: _env("ASHARE_HERMES_COMPAT_API_KEY", ""))
    compat_model: str = Field(default_factory=lambda: _env("ASHARE_HERMES_COMPAT_MODEL", "gpt-5.4"))
    routing_policy: str = Field(default_factory=lambda: _env("ASHARE_HERMES_ROUTING_POLICY", "half_minimax_half_compat"))
    fast_roles: tuple[str, ...] = ("runtime_scout", "cron_intraday_watch", "cron_position_watch", "execution_operator")
    deep_roles: tuple[str, ...] = ("event_researcher", "strategy_analyst", "risk_gate", "audit_recorder")
    escalation_risk_levels: tuple[str, ...] = ("high", "critical")


class AppSettings(BaseModel):
    """全局应用配置"""
    app_name: str = "ashare-system-v2"
    environment: str = Field(default_factory=lambda: _env("ASHARE_ENV", "dev"))
    run_mode: RunMode = Field(default_factory=lambda: _env("ASHARE_RUN_MODE", "dry-run"))
    live_trade_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_LIVE_ENABLE", False))
    execution_mode: str = Field(default_factory=lambda: _env("ASHARE_EXECUTION_MODE", "xtquant"))
    execution_plane: str = Field(default_factory=lambda: _env("ASHARE_EXECUTION_PLANE", "local_xtquant"))
    market_mode: str = Field(default_factory=lambda: _env("ASHARE_MARKET_MODE", "xtquant"))
    allowed_markets: tuple[str, ...] = ("SH", "SZ")
    execution_submit_retry_attempts: int = Field(default_factory=lambda: _env_int("ASHARE_EXECUTION_RETRY_ATTEMPTS", 1))
    execution_submit_retry_backoff_ms: int = Field(default_factory=lambda: _env_int("ASHARE_EXECUTION_RETRY_BACKOFF_MS", 0))

    # 路径
    workspace: Path = Field(default_factory=lambda: _env_path("ASHARE_WORKSPACE", str(_default_project_root())))
    storage_root: Path = Field(default_factory=lambda: _env_path("ASHARE_STORAGE_ROOT", ".ashare_state"))
    logs_dir: Path = Field(default_factory=lambda: _env_path("ASHARE_LOGS_DIR", "logs"))

    # 子配置
    xtquant: XtQuantSettings = Field(default_factory=XtQuantSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    windows_gateway: WindowsGatewaySettings = Field(default_factory=WindowsGatewaySettings)
    go_platform: GoPlatformSettings = Field(default_factory=GoPlatformSettings)
    hermes: HermesSettings = Field(default_factory=HermesSettings)

    # 策略参数
    strategy_name: str = Field(default_factory=lambda: _env("ASHARE_STRATEGY_NAME", "ashare-system-v2"))
    minimum_confidence: float = Field(default_factory=lambda: _env_float("ASHARE_MIN_CONFIDENCE", 0.55))

    @property
    def control_plane_db_path(self) -> Path:
        return self.storage_root / "db" / "control_plane.sqlite3"


def load_settings() -> AppSettings:
    settings = AppSettings()
    project_root = _default_project_root()
    settings.workspace = _resolve_path(settings.workspace, project_root)
    settings.storage_root = _resolve_path(settings.storage_root, settings.workspace)
    settings.logs_dir = _resolve_path(settings.logs_dir, settings.workspace)
    ensure_storage_layout(settings.storage_root)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings
