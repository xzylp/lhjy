"""健康检查"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import build_execution_adapter
from .market_adapter import build_market_adapter
from ..settings import AppSettings
from ..logging_config import get_logger

logger = get_logger("healthcheck")


@dataclass
class HealthcheckResult:
    ok: bool
    checks: list[dict[str, str]] = field(default_factory=list)


class EnvironmentHealthcheck:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def run(self) -> HealthcheckResult:
        checks: list[dict[str, str]] = []
        checks.append(self._path_check("workspace", self.settings.workspace))
        checks.append(self._path_check("storage_root", self.settings.storage_root))
        checks.append(self._path_check("xtquant_root", self.settings.xtquant.root))
        checks.append(self._path_check("xtquantservice_root", self.settings.xtquant.service_root))

        if self.settings.run_mode == "live":
            checks.append(
                self._value_check(
                    "live_trade_enabled",
                    self.settings.live_trade_enabled,
                    str(self.settings.live_trade_enabled).lower(),
                )
            )
            checks.append(self._value_check("execution_mode", self.settings.execution_mode == "xtquant", self.settings.execution_mode))
            checks.append(self._value_check("market_mode", self.settings.market_mode == "xtquant", self.settings.market_mode))
            checks.append(self._adapter_mode_check("execution_adapter_mode", build_execution_adapter, expected="xtquant"))
            checks.append(self._adapter_mode_check("market_adapter_mode", build_market_adapter, expected="xtquant"))

        ok = all(item["status"] == "ok" for item in checks)
        return HealthcheckResult(ok=ok, checks=checks)

    def _path_check(self, name: str, path: Path) -> dict[str, str]:
        return {
            "name": name,
            "status": "ok" if path.exists() else "missing",
            "detail": str(path),
        }

    def _value_check(self, name: str, passed: bool, detail: str) -> dict[str, str]:
        return {
            "name": name,
            "status": "ok" if passed else "invalid",
            "detail": detail,
        }

    def _adapter_mode_check(self, name: str, builder, expected: str) -> dict[str, str]:
        try:
            adapter = builder(expected, self.settings)
            actual = getattr(adapter, "mode", expected)
            passed = actual == expected
            return self._value_check(name, passed, actual)
        except Exception as exc:
            return {
                "name": name,
                "status": "invalid",
                "detail": str(exc),
            }


def run_healthcheck(settings: AppSettings) -> None:
    result = EnvironmentHealthcheck(settings).run()
    print(json.dumps({"ok": result.ok, "checks": result.checks}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.ok else 1)
