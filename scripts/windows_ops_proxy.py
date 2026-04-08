from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProxyContext:
    def __init__(self, project_dir: Path, bind_host: str, port: int, service_port: int) -> None:
        self.project_dir = project_dir
        self.bind_host = bind_host
        self.port = port
        self.service_port = service_port
        self.state_dir = self.project_dir / ".ashare_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_dir = self.project_dir / "scripts"
        self.logs_dir = self.project_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = self.state_dir / "ops_proxy_token.txt"
        self.manifest_file = self.state_dir / "ops_proxy_endpoints.json"
        self.allowed_logs = {
            "startup.log": self.logs_dir / "startup.log",
            "api_service.log": self.logs_dir / "api_service.log",
            "api_service.err": self.logs_dir / "api_service.err",
            "scheduler.log": self.logs_dir / "scheduler.log",
            "scheduler.err": self.logs_dir / "scheduler.err",
        }
        self.token = self._ensure_token()
        self._write_manifest()

    def _ensure_token(self) -> str:
        if self.token_file.exists():
            token = self.token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(32)
        self.token_file.write_text(token + "\n", encoding="utf-8")
        return token

    def _write_manifest(self) -> None:
        script = self.scripts_dir / "write_ops_proxy_endpoints.ps1"
        if script.exists():
            self._run_powershell(
                [
                    "-File",
                    str(script),
                    "-ProjectDir",
                    str(self.project_dir),
                    "-Port",
                    str(self.port),
                    "-BindHost",
                    self.bind_host,
                    "-TokenFile",
                    str(self.token_file),
                ],
                check=False,
            )
            return

        payload = {
            "generated_at": self._now_text(),
            "project_dir": str(self.project_dir),
            "port": self.port,
            "bind_host": self.bind_host,
            "preferred_wsl_url": f"http://127.0.0.1:{self.port}",
            "candidate_urls": [f"http://127.0.0.1:{self.port}"],
            "token_file": str(self.token_file),
        }
        self.manifest_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _now_text() -> str:
        import datetime as _dt

        return _dt.datetime.now().isoformat(timespec="seconds")

    def _base_subprocess_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(self.project_dir),
            "text": True,
            "capture_output": True,
        }
        if CREATE_NO_WINDOW:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        return kwargs

    def _run_powershell(self, extra_args: list[str], check: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *extra_args]
        return subprocess.run(cmd, check=check, timeout=timeout, **self._base_subprocess_kwargs())

    def _popen_powershell(self, extra_args: list[str]) -> subprocess.Popen[str]:
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *extra_args]
        kwargs = self._base_subprocess_kwargs()
        kwargs.pop("capture_output", None)
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        return subprocess.Popen(cmd, **kwargs)

    def get_status(self) -> dict[str, Any]:
        script = self.scripts_dir / "windows_service.ps1"
        result = self._run_powershell(
            [
                "-File",
                str(script),
                "-Action",
                "status",
                "-ProjectDir",
                str(self.project_dir),
                "-Port",
                str(self.service_port),
                "-Json",
            ],
            timeout=20,
        )
        payload = self._completed_payload(result)
        if result.returncode == 0:
            payload["service"] = self._parse_json(result.stdout)
        else:
            payload["service"] = None
        payload["proxy"] = self.proxy_metadata()
        return payload

    def run_service_action(
        self,
        action: str,
        *,
        bind_host: str | None = None,
        port: int | None = None,
        no_scheduler: bool = False,
    ) -> dict[str, Any]:
        script = self.scripts_dir / "windows_service.ps1"
        args = [
            "-File",
            str(script),
            "-Action",
            action,
            "-ProjectDir",
            str(self.project_dir),
            "-BindHost",
            bind_host or "0.0.0.0",
            "-Port",
            str(port or self.service_port),
            "-Json",
        ]
        if no_scheduler:
            args.append("-NoScheduler")
        result = self._run_powershell(args, timeout=180)
        payload = self._completed_payload(result)
        payload["service"] = self._parse_json(result.stdout)
        return payload

    def run_watchdog_action(
        self,
        action: str,
        *,
        no_scheduler: bool = False,
        keep_services_alive: bool = False,
    ) -> dict[str, Any]:
        if action == "start":
            script = self.scripts_dir / "start_unattended.ps1"
            args = [
                "-File",
                str(script),
                "-Port",
                str(self.service_port),
            ]
            if no_scheduler:
                args.append("-NoScheduler")
            proc = self._popen_powershell(args)
            return {
                "ok": True,
                "action": action,
                "watchdog_pid": proc.pid,
                "updated_at": self._now_text(),
                "proxy": self.proxy_metadata(),
            }

        script = self.scripts_dir / "stop_unattended.ps1"
        args = [
            "-File",
            str(script),
            "-ProjectDir",
            str(self.project_dir),
            "-Port",
            str(self.service_port),
            "-Json",
        ]
        if keep_services_alive:
            args.append("-KeepServicesAlive")
        result = self._run_powershell(args, timeout=120)
        payload = self._completed_payload(result)
        payload["watchdog"] = self._parse_json(result.stdout)
        return payload

    def run_tests(self, test_args: list[str]) -> dict[str, Any]:
        script = self.scripts_dir / "run_tests.ps1"
        result = self._run_powershell(
            [
                "-File",
                str(script),
                *test_args,
            ],
            timeout=3600,
        )
        return self._completed_payload(result)

    def run_healthcheck(self) -> dict[str, Any]:
        script = self.scripts_dir / "health_check.ps1"
        result = self._run_powershell(["-File", str(script)], timeout=120)
        return self._completed_payload(result)

    def tail_log(self, name: str, lines: int) -> dict[str, Any]:
        path = self.allowed_logs.get(name)
        if path is None:
            raise ValueError(f"unsupported log file: {name}")
        if not path.exists():
            return {
                "ok": True,
                "name": name,
                "path": str(path),
                "lines": [],
                "exists": False,
                "updated_at": self._now_text(),
            }
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return {
            "ok": True,
            "name": name,
            "path": str(path),
            "lines": content[-max(1, min(lines, 500)):],
            "exists": True,
            "updated_at": self._now_text(),
        }

    def proxy_metadata(self) -> dict[str, Any]:
        return {
            "service": "windows-ops-proxy",
            "bind_host": self.bind_host,
            "port": self.port,
            "service_port": self.service_port,
            "project_dir": str(self.project_dir),
            "manifest_file": str(self.manifest_file),
            "token_file": str(self.token_file),
            "updated_at": self._now_text(),
        }

    @staticmethod
    def _completed_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "updated_at": ProxyContext._now_text(),
        }

    @staticmethod
    def _parse_json(text: str) -> Any:
        stripped = (text or "").strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None


class OpsHandler(BaseHTTPRequestHandler):
    server_version = "AshareWindowsOpsProxy/0.1"

    @property
    def context(self) -> ProxyContext:
        return self.server.context  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok", **self.context.proxy_metadata()})
            return
        if not self._authorize():
            return
        if parsed.path == "/status":
            self._write_json(HTTPStatus.OK, self.context.get_status())
            return
        if parsed.path == "/logs/tail":
            query = parse_qs(parsed.query)
            name = (query.get("name") or ["startup.log"])[0]
            try:
                lines = int((query.get("lines") or ["80"])[0])
            except ValueError:
                lines = 80
            try:
                payload = self.context.tail_log(name, lines)
            except ValueError as exc:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            self._write_json(HTTPStatus.OK, payload)
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        payload = self._read_json_body()
        if parsed.path == "/actions/service":
            action = str(payload.get("action") or "status").strip().lower()
            if action not in {"start", "stop", "restart", "status"}:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "unsupported service action"})
                return
            result = self.context.run_service_action(
                action,
                bind_host=str(payload.get("bind_host") or "0.0.0.0"),
                port=int(payload.get("port") or self.context.service_port),
                no_scheduler=bool(payload.get("no_scheduler", False)),
            )
            self._write_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST, result)
            return
        if parsed.path == "/actions/watchdog":
            action = str(payload.get("action") or "").strip().lower()
            if action not in {"start", "stop"}:
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "unsupported watchdog action"})
                return
            result = self.context.run_watchdog_action(
                action,
                no_scheduler=bool(payload.get("no_scheduler", False)),
                keep_services_alive=bool(payload.get("keep_services_alive", False)),
            )
            self._write_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST, result)
            return
        if parsed.path == "/actions/tests":
            args = payload.get("args") or []
            if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
                self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "args must be string list"})
                return
            result = self.context.run_tests(args)
            self._write_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST, result)
            return
        if parsed.path == "/actions/healthcheck":
            result = self.context.run_healthcheck()
            self._write_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST, result)
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorize(self) -> bool:
        header = self.headers.get("X-Ashare-Token", "").strip()
        if header and secrets.compare_digest(header, self.context.token):
            return True
        self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ashare-system-v2 Windows ops proxy")
    parser.add_argument("--host", default=os.environ.get("ASHARE_OPS_PROXY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASHARE_OPS_PROXY_PORT", "18791")))
    parser.add_argument("--service-port", type=int, default=int(os.environ.get("ASHARE_SERVICE_PORT", "8100")))
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    return parser.parse_args()


def main() -> int:
    if os.name != "nt":
        print("windows_ops_proxy.py must run on Windows", file=sys.stderr)
        return 1

    args = parse_args()
    context = ProxyContext(
        project_dir=Path(args.project_dir).resolve(),
        bind_host=args.host,
        port=args.port,
        service_port=args.service_port,
    )
    server = ThreadingHTTPServer((args.host, args.port), OpsHandler)
    server.context = context  # type: ignore[attr-defined]
    print(
        json.dumps(
            {
                "status": "ok",
                "service": "windows-ops-proxy",
                "host": args.host,
                "port": args.port,
                "service_port": args.service_port,
                "manifest_file": str(context.manifest_file),
                "token_file": str(context.token_file),
            },
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
