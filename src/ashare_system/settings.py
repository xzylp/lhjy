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


class NotifySettings(BaseModel):
    """通知配置"""
    feishu_app_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_APP_ID", ""))
    feishu_app_secret: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_APP_SECRET", ""))
    feishu_chat_id: str = Field(default_factory=lambda: _env("ASHARE_FEISHU_CHAT_ID", ""))
    alerts_enabled: bool = Field(default_factory=lambda: _env_bool("ASHARE_ALERTS_ENABLED"))


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

    # 策略参数
    strategy_name: str = Field(default_factory=lambda: _env("ASHARE_STRATEGY_NAME", "ashare-system-v2"))
    minimum_confidence: float = Field(default_factory=lambda: _env_float("ASHARE_MIN_CONFIDENCE", 0.55))


def load_settings() -> AppSettings:
    settings = AppSettings()
    project_root = _default_project_root()
    settings.workspace = _resolve_path(settings.workspace, project_root)
    settings.storage_root = _resolve_path(settings.storage_root, settings.workspace)
    settings.logs_dir = _resolve_path(settings.logs_dir, settings.workspace)
    ensure_storage_layout(settings.storage_root)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    return settings
