"""Quick health check for local MCP server transports.

- Reads alde/mcp_servers.json
- Selects configured default server (prefers local-tcp)
- Supports stdio, tcp, and http entries
- Sends initialize and tools/list and prints basic status
- Falls back from local-tcp to local-http when needed
- Emits a machine-readable summary line for control-plane ingestion
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict

CONFIG_PATH = Path(__file__).with_name("mcp_servers.json")
DEFAULT_TIMEOUT = float(os.getenv("ALDE_MCP_HEALTH_TIMEOUT", "20"))
DEFAULT_SERVER_NAME = os.getenv("ALDE_MCP_DEFAULT_SERVER", "local-tcp")
DEFAULT_FALLBACK_ORDER = os.getenv("ALDE_MCP_FALLBACK_ORDER", "local-http")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_true(value: str | None, *, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return bool(default)
    return normalized in {"1", "true", "yes", "on"}


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(float(item) for item in values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    clamped_percentile = max(0.0, min(100.0, float(percentile)))
    position = (clamped_percentile / 100.0) * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - lower_index
    return sorted_values[lower_index] + (sorted_values[upper_index] - sorted_values[lower_index]) * fraction


def _load_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_line(proc: subprocess.Popen, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    deadline = time.monotonic() + max(float(timeout), 0.1)
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed pipe")
        stripped_line = line.strip()
        if not stripped_line:
            continue
        try:
            return json.loads(stripped_line)
        except Exception:
            # Some modules emit bootstrap logs on stdout; skip until JSON arrives.
            continue
    raise TimeoutError("Timed out waiting for MCP server response")


class McpHealthProbeService:
    def __init__(self, *, config_path: Path, timeout_seconds: float) -> None:
        self._config_path = config_path
        self._timeout_seconds = max(float(timeout_seconds), 1.0)
        # Launch from repository root so module paths like ALDE.alde.* resolve.
        self._server_cwd = Path(__file__).resolve().parents[2]

    def load_default_server_name(self, config_payload: Dict[str, Any]) -> str:
        configured_name = str(config_payload.get("default_server") or "").strip()
        if configured_name:
            return configured_name
        return DEFAULT_SERVER_NAME

    def load_server_map(self) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
        config_payload = _load_config(self._config_path)
        server_payload = config_payload.get("servers")
        if not isinstance(server_payload, dict) or not server_payload:
            raise RuntimeError("No servers configured in mcp_servers.json")

        configured_fallback_order: list[str] = []
        raw_fallback_order = config_payload.get("fallback_order")
        if isinstance(raw_fallback_order, list):
            configured_fallback_order = [
                str(item).strip()
                for item in raw_fallback_order
                if str(item).strip()
            ]
        elif isinstance(raw_fallback_order, str) and raw_fallback_order.strip():
            configured_fallback_order = [
                str(item).strip()
                for item in raw_fallback_order.split(",")
                if str(item).strip()
            ]

        normalized_server_map: dict[str, dict[str, Any]] = {
            str(server_name): dict(server_config)
            for server_name, server_config in server_payload.items()
            if isinstance(server_config, dict)
        }
        if not normalized_server_map:
            raise RuntimeError("No valid servers configured in mcp_servers.json")

        preferred_name = self.load_default_server_name(config_payload)
        if preferred_name in normalized_server_map:
            return normalized_server_map, preferred_name, configured_fallback_order

        for fallback_name in ("local-tcp", "local-http", "local-stdio"):
            if fallback_name in normalized_server_map:
                return normalized_server_map, fallback_name, configured_fallback_order

        first_name = next(iter(normalized_server_map.keys()))
        return normalized_server_map, first_name, configured_fallback_order

    def load_probe_order(
        self,
        server_map: dict[str, dict[str, Any]],
        selected_server_name: str,
        configured_fallback_order: list[str] | None = None,
    ) -> list[str]:
        probe_order: list[str] = []

        def _append(server_name: str) -> None:
            normalized_server_name = str(server_name or "").strip()
            if not normalized_server_name:
                return
            if normalized_server_name not in server_map:
                return
            if normalized_server_name in probe_order:
                return
            probe_order.append(normalized_server_name)

        _append(selected_server_name)

        fallback_order = [
            str(item).strip()
            for item in str(DEFAULT_FALLBACK_ORDER or "").split(",")
            if str(item).strip()
        ]

        if configured_fallback_order:
            fallback_order = [
                str(item).strip()
                for item in configured_fallback_order
                if str(item).strip()
            ]

        selected_transport = str((server_map.get(selected_server_name) or {}).get("type") or "").strip().lower()
        if selected_server_name == "local-tcp" or selected_transport == "tcp":
            _append("local-http")

        for fallback_server_name in fallback_order:
            _append(fallback_server_name)

        if _is_true(os.getenv("ALDE_MCP_ENABLE_STDIO_FALLBACK", "0"), default=False):
            _append("local-stdio")

        return probe_order

    def _launch_process(self, server: Dict[str, Any]) -> subprocess.Popen | None:
        command = str(server.get("command") or "").strip()
        if not command:
            return None
        args = [str(argument) for argument in (server.get("args") or [])]
        cmd = [command, *args]
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(self._server_cwd),
        )

    def _stop_process(self, proc: subprocess.Popen | None) -> str:
        if proc is None:
            return ""
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        except Exception:
            pass

        if proc.stdout is None:
            return ""
        try:
            remaining_output = proc.stdout.read()
        except Exception:
            remaining_output = ""
        return str(remaining_output or "").strip()

    def _send_stdio_request(self, proc: subprocess.Popen, payload: Dict[str, Any]) -> Dict[str, Any]:
        if proc.stdin is None:
            raise RuntimeError("stdio MCP process has no stdin pipe")
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        return _read_line(proc, timeout=self._timeout_seconds)

    def _send_tcp_request(self, host: str, port: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        with socket.create_connection((host, port), timeout=2.0) as connection:
            connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            response_bytes = b""
            while b"\n" not in response_bytes:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response_bytes += chunk
        if not response_bytes:
            raise RuntimeError("TCP MCP returned no response")
        response_line = response_bytes.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        return json.loads(response_line)

    def _send_http_request(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = response.read().decode("utf-8", errors="replace")
        return json.loads(body)

    def _retry_request(self, request_callable: Callable[[], Dict[str, Any]]) -> tuple[Dict[str, Any], float]:
        deadline = time.monotonic() + self._timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            request_start = time.perf_counter()
            try:
                response_payload = request_callable()
                latency_ms = (time.perf_counter() - request_start) * 1000.0
                return response_payload, latency_ms
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        if last_error is not None:
            raise last_error
        raise TimeoutError("MCP request timed out")

    def _probe_stdio(self, server: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], str, float, float]:
        proc = self._launch_process(server)
        if proc is None:
            raise RuntimeError("stdio server requires command + args")
        init_response: Dict[str, Any] = {}
        list_response: Dict[str, Any] = {}
        init_latency_ms = 0.0
        list_latency_ms = 0.0
        try:
            init_start = time.perf_counter()
            init_response = self._send_stdio_request(proc, {"method": "initialize", "params": {}})
            init_latency_ms = (time.perf_counter() - init_start) * 1000.0

            list_start = time.perf_counter()
            list_response = self._send_stdio_request(proc, {"method": "tools/list", "params": {}})
            list_latency_ms = (time.perf_counter() - list_start) * 1000.0
        finally:
            startup_output = self._stop_process(proc)
        return init_response, list_response, startup_output, init_latency_ms, list_latency_ms

    def _probe_tcp(self, server: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], str, float, float]:
        host = str(server.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(server.get("port") or 8765)
        proc = self._launch_process(server)
        init_response: Dict[str, Any] = {}
        list_response: Dict[str, Any] = {}
        init_latency_ms = 0.0
        list_latency_ms = 0.0
        try:
            init_response, init_latency_ms = self._retry_request(
                lambda: self._send_tcp_request(host, port, {"method": "initialize", "params": {}})
            )
            list_response, list_latency_ms = self._retry_request(
                lambda: self._send_tcp_request(host, port, {"method": "tools/list", "params": {}})
            )
        finally:
            startup_output = self._stop_process(proc)
        return init_response, list_response, startup_output, init_latency_ms, list_latency_ms

    def _probe_http(self, server: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], str, float, float]:
        url = str(server.get("url") or "").strip()
        if not url:
            raise RuntimeError("http server requires url")
        proc = self._launch_process(server)
        init_response: Dict[str, Any] = {}
        list_response: Dict[str, Any] = {}
        init_latency_ms = 0.0
        list_latency_ms = 0.0
        try:
            init_response, init_latency_ms = self._retry_request(
                lambda: self._send_http_request(url, {"method": "initialize", "params": {}})
            )
            list_response, list_latency_ms = self._retry_request(
                lambda: self._send_http_request(url, {"method": "tools/list", "params": {}})
            )
        finally:
            startup_output = self._stop_process(proc)
        return init_response, list_response, startup_output, init_latency_ms, list_latency_ms

    def _call_stdio(self, server: Dict[str, Any], payload: Dict[str, Any]) -> tuple[Dict[str, Any], str, float]:
        proc = self._launch_process(server)
        if proc is None:
            raise RuntimeError("stdio server requires command + args")
        startup_output = ""
        try:
            self._send_stdio_request(proc, {"method": "initialize", "params": {}})
            response_start = time.perf_counter()
            response_payload = self._send_stdio_request(proc, payload)
            response_latency_ms = (time.perf_counter() - response_start) * 1000.0
        finally:
            startup_output = self._stop_process(proc)
        return response_payload, startup_output, response_latency_ms

    def _call_tcp(self, server: Dict[str, Any], payload: Dict[str, Any]) -> tuple[Dict[str, Any], str, float]:
        host = str(server.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(server.get("port") or 8765)
        proc = self._launch_process(server)
        startup_output = ""
        try:
            self._retry_request(
                lambda: self._send_tcp_request(host, port, {"method": "initialize", "params": {}})
            )
            response_payload, response_latency_ms = self._retry_request(
                lambda: self._send_tcp_request(host, port, payload)
            )
        finally:
            startup_output = self._stop_process(proc)
        return response_payload, startup_output, response_latency_ms

    def _call_http(self, server: Dict[str, Any], payload: Dict[str, Any]) -> tuple[Dict[str, Any], str, float]:
        url = str(server.get("url") or "").strip()
        if not url:
            raise RuntimeError("http server requires url")
        proc = self._launch_process(server)
        startup_output = ""
        try:
            self._retry_request(
                lambda: self._send_http_request(url, {"method": "initialize", "params": {}})
            )
            response_payload, response_latency_ms = self._retry_request(
                lambda: self._send_http_request(url, payload)
            )
        finally:
            startup_output = self._stop_process(proc)
        return response_payload, startup_output, response_latency_ms

    def execute_request(self, payload: Dict[str, Any], *, server_name: str | None = None) -> Dict[str, Any]:
        server_map, selected_server_name, configured_fallback_order = self.load_server_map()
        resolved_server_name = str(server_name or "").strip() or selected_server_name
        probe_order = self.load_probe_order(
            server_map,
            resolved_server_name,
            configured_fallback_order=configured_fallback_order,
        )
        attempts: list[dict[str, Any]] = []

        for candidate_server_name in probe_order:
            server = dict(server_map.get(candidate_server_name) or {})
            transport = str(server.get("type") or "").strip().lower() or "unknown"
            started_at = _utc_now_iso()
            start_perf = time.perf_counter()
            startup_log = ""
            try:
                if transport == "stdio":
                    response_payload, startup_log, response_latency_ms = self._call_stdio(server, payload)
                elif transport == "tcp":
                    response_payload, startup_log, response_latency_ms = self._call_tcp(server, payload)
                elif transport == "http":
                    response_payload, startup_log, response_latency_ms = self._call_http(server, payload)
                else:
                    raise RuntimeError(f"Unsupported MCP transport: {transport}")

                attempts.append(
                    {
                        "server_name": candidate_server_name,
                        "transport": transport,
                        "started_at": started_at,
                        "latency_ms": round((time.perf_counter() - start_perf) * 1000.0, 3),
                        "response_latency_ms": round(response_latency_ms, 3),
                        "ok": True,
                        "timed_out": False,
                        "error": "",
                        "startup_log": startup_log[:400],
                    }
                )
                return {
                    "ok": True,
                    "selected_server": resolved_server_name,
                    "active_server": candidate_server_name,
                    "active_transport": transport,
                    "fallback_used": candidate_server_name != resolved_server_name,
                    "response": response_payload,
                    "attempts": attempts,
                }
            except Exception as exc:
                attempts.append(
                    {
                        "server_name": candidate_server_name,
                        "transport": transport,
                        "started_at": started_at,
                        "latency_ms": round((time.perf_counter() - start_perf) * 1000.0, 3),
                        "response_latency_ms": 0.0,
                        "ok": False,
                        "timed_out": self._is_timeout_error(exc),
                        "error": f"{type(exc).__name__}: {exc}",
                        "startup_log": startup_log[:400],
                    }
                )

        return {
            "ok": False,
            "selected_server": resolved_server_name,
            "active_server": "",
            "active_transport": "",
            "fallback_used": False,
            "response": {},
            "attempts": attempts,
            "error": str((attempts[-1] if attempts else {}).get("error") or "mcp_request_failed"),
        }

    def execute_tools_call(self, tool_name: str, arguments_payload: Dict[str, Any] | None = None, *, server_name: str | None = None) -> Dict[str, Any]:
        return self.execute_request(
            {
                "method": "tools/call",
                "params": {
                    "name": str(tool_name or "").strip(),
                    "arguments": dict(arguments_payload or {}),
                },
            },
            server_name=server_name,
        )

    def _is_timeout_error(self, error_object: Exception) -> bool:
        if isinstance(error_object, (TimeoutError, socket.timeout, subprocess.TimeoutExpired)):
            return True
        normalized_error = str(error_object).strip().lower()
        return "timeout" in normalized_error or "timed out" in normalized_error

    def _probe_server(self, server_name: str, server: Dict[str, Any]) -> dict[str, Any]:
        started_at = _utc_now_iso()
        start_perf = time.perf_counter()
        transport = str(server.get("type") or "").strip().lower() or "unknown"
        startup_log = ""

        try:
            if transport == "stdio":
                init_response, list_response, startup_log, init_latency_ms, tools_list_latency_ms = self._probe_stdio(server)
            elif transport == "tcp":
                init_response, list_response, startup_log, init_latency_ms, tools_list_latency_ms = self._probe_tcp(server)
            elif transport == "http":
                init_response, list_response, startup_log, init_latency_ms, tools_list_latency_ms = self._probe_http(server)
            else:
                raise RuntimeError(f"Unsupported MCP transport: {transport}")

            tool_count = len(list_response.get("result", {}).get("tools", [])) if "result" in list_response else 0
            success = "result" in init_response and "result" in list_response
            error_text = "" if success else "missing result payload"
            return {
                "server_name": str(server_name),
                "transport": transport,
                "started_at": started_at,
                "latency_ms": round((time.perf_counter() - start_perf) * 1000.0, 3),
                "initialize_latency_ms": round(init_latency_ms, 3),
                "tools_list_latency_ms": round(tools_list_latency_ms, 3),
                "ok": bool(success),
                "timed_out": False,
                "error": error_text,
                "tool_count": int(tool_count),
                "startup_log": startup_log[:400],
            }
        except Exception as exc:
            return {
                "server_name": str(server_name),
                "transport": transport,
                "started_at": started_at,
                "latency_ms": round((time.perf_counter() - start_perf) * 1000.0, 3),
                "initialize_latency_ms": 0.0,
                "tools_list_latency_ms": 0.0,
                "ok": False,
                "timed_out": self._is_timeout_error(exc),
                "error": f"{type(exc).__name__}: {exc}",
                "tool_count": 0,
                "startup_log": startup_log[:400],
            }

    def _build_transport_metrics(self, attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        metric_map: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            transport = str(attempt.get("transport") or "unknown")
            metric_bucket = metric_map.setdefault(
                transport,
                {
                    "attempt_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "timeout_count": 0,
                    "latency_values": [],
                },
            )
            metric_bucket["attempt_count"] += 1
            if bool(attempt.get("ok")):
                metric_bucket["success_count"] += 1
            else:
                metric_bucket["failure_count"] += 1
            if bool(attempt.get("timed_out")):
                metric_bucket["timeout_count"] += 1
            metric_bucket["latency_values"].append(_safe_float(attempt.get("latency_ms")))

        normalized_metric_map: dict[str, dict[str, Any]] = {}
        for transport, metric_bucket in metric_map.items():
            attempt_count = max(int(metric_bucket.get("attempt_count") or 0), 1)
            latency_values = [float(item) for item in (metric_bucket.get("latency_values") or []) if float(item) >= 0.0]
            normalized_metric_map[transport] = {
                "attempt_count": int(metric_bucket.get("attempt_count") or 0),
                "success_count": int(metric_bucket.get("success_count") or 0),
                "failure_count": int(metric_bucket.get("failure_count") or 0),
                "timeout_count": int(metric_bucket.get("timeout_count") or 0),
                "error_rate": round(float(metric_bucket.get("failure_count") or 0) / attempt_count, 4),
                "timeout_rate": round(float(metric_bucket.get("timeout_count") or 0) / attempt_count, 4),
                "p50_latency_ms": round(_percentile(latency_values, 50), 3),
                "p95_latency_ms": round(_percentile(latency_values, 95), 3),
                "avg_latency_ms": round(sum(latency_values) / len(latency_values), 3) if latency_values else 0.0,
            }

        return normalized_metric_map

    def _build_overall_metrics(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        attempt_count = len(attempts)
        if attempt_count <= 0:
            return {
                "attempt_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "timeout_count": 0,
                "error_rate": 0.0,
                "timeout_rate": 0.0,
                "p50_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
                "avg_latency_ms": 0.0,
            }

        success_count = sum(1 for attempt in attempts if bool(attempt.get("ok")))
        failure_count = attempt_count - success_count
        timeout_count = sum(1 for attempt in attempts if bool(attempt.get("timed_out")))
        latency_values = [_safe_float(attempt.get("latency_ms")) for attempt in attempts]

        return {
            "attempt_count": int(attempt_count),
            "success_count": int(success_count),
            "failure_count": int(failure_count),
            "timeout_count": int(timeout_count),
            "error_rate": round(float(failure_count) / float(attempt_count), 4),
            "timeout_rate": round(float(timeout_count) / float(attempt_count), 4),
            "p50_latency_ms": round(_percentile(latency_values, 50), 3),
            "p95_latency_ms": round(_percentile(latency_values, 95), 3),
            "avg_latency_ms": round(sum(latency_values) / len(latency_values), 3) if latency_values else 0.0,
        }

    def probe_with_fallback(self) -> dict[str, Any]:
        server_map, selected_server_name, configured_fallback_order = self.load_server_map()
        probe_order = self.load_probe_order(
            server_map,
            selected_server_name,
            configured_fallback_order=configured_fallback_order,
        )
        selected_transport = str((server_map.get(selected_server_name) or {}).get("type") or "").strip().lower() or "unknown"

        attempts: list[dict[str, Any]] = []
        successful_attempt: dict[str, Any] | None = None

        for server_name in probe_order:
            server_payload = dict(server_map.get(server_name) or {})
            attempt = self._probe_server(server_name, server_payload)
            attempt["attempt_index"] = len(attempts) + 1
            attempts.append(attempt)
            if bool(attempt.get("ok")):
                successful_attempt = dict(attempt)
                break

        transport_metrics = self._build_transport_metrics(attempts)
        overall_metrics = self._build_overall_metrics(attempts)
        fallback_used = bool(
            successful_attempt
            and str(successful_attempt.get("server_name") or "") != str(selected_server_name or "")
        )

        failure_error = ""
        if not successful_attempt and attempts:
            failure_error = str(attempts[-1].get("error") or "mcp_probe_failed")

        return {
            "ok": bool(successful_attempt),
            "timestamp": _utc_now_iso(),
            "selected_server": str(selected_server_name),
            "selected_transport": selected_transport,
            "active_server": str((successful_attempt or {}).get("server_name") or ""),
            "active_transport": str((successful_attempt or {}).get("transport") or ""),
            "fallback_used": bool(fallback_used),
            "attempt_count": len(attempts),
            "attempts": attempts,
            "tool_count": int((successful_attempt or {}).get("tool_count") or 0),
            "probe_metrics": {
                "overall_metrics": overall_metrics,
                "transport_metrics": transport_metrics,
            },
            "error": failure_error,
        }


def main() -> int:
    probe_payload: dict[str, Any]
    try:
        probe_service = McpHealthProbeService(
            config_path=CONFIG_PATH,
            timeout_seconds=DEFAULT_TIMEOUT,
        )
        probe_payload = probe_service.probe_with_fallback()
    except Exception as exc:
        probe_payload = {
            "ok": False,
            "timestamp": _utc_now_iso(),
            "selected_server": "",
            "selected_transport": "",
            "active_server": "",
            "active_transport": "",
            "fallback_used": False,
            "attempt_count": 0,
            "attempts": [],
            "tool_count": 0,
            "probe_metrics": {
                "overall_metrics": {
                    "attempt_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "timeout_count": 0,
                    "error_rate": 1.0,
                    "timeout_rate": 0.0,
                    "p50_latency_ms": 0.0,
                    "p95_latency_ms": 0.0,
                    "avg_latency_ms": 0.0,
                },
                "transport_metrics": {},
            },
            "error": f"{type(exc).__name__}: {exc}",
        }

    print(
        "Probing MCP default "
        f"{probe_payload.get('selected_server') or 'unknown'} "
        f"(transport={probe_payload.get('selected_transport') or 'unknown'})"
    )
    for attempt in probe_payload.get("attempts") or []:
        print(
            "attempt "
            f"{attempt.get('attempt_index')} "
            f"server={attempt.get('server_name')} "
            f"transport={attempt.get('transport')} "
            f"ok={attempt.get('ok')} "
            f"timed_out={attempt.get('timed_out')} "
            f"latency_ms={attempt.get('latency_ms')} "
            f"error={attempt.get('error') or '-'}"
        )

    if probe_payload.get("ok"):
        fallback_note = " (fallback)" if probe_payload.get("fallback_used") else ""
        print(
            "active -> "
            f"{probe_payload.get('active_server') or 'unknown'} "
            f"transport={probe_payload.get('active_transport') or 'unknown'}"
            f"{fallback_note}"
        )
        print(f"tools/list -> {probe_payload.get('tool_count')} tools")
    else:
        print(f"MCP health probe failed: {probe_payload.get('error') or 'unknown error'}")

    print("MCP_PROBE_JSON=" + json.dumps(probe_payload, ensure_ascii=True, separators=(",", ":")))
    return 0 if probe_payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())


def call_mcp_tool(
    tool_name: str,
    arguments_payload: Dict[str, Any] | None = None,
    *,
    config_path: Path = CONFIG_PATH,
    timeout_seconds: float = DEFAULT_TIMEOUT,
    server_name: str | None = None,
) -> Dict[str, Any]:
    probe_service = McpHealthProbeService(
        config_path=config_path,
        timeout_seconds=timeout_seconds,
    )
    return probe_service.execute_tools_call(
        tool_name,
        arguments_payload,
        server_name=server_name,
    )
