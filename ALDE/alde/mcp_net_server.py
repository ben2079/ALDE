"""Network MCP server for TCP (jsonl) and HTTP transports.

This keeps MCP request semantics identical to the stdio server by delegating
all method dispatch to MCP_REQUEST_SERVICE.
"""

from __future__ import annotations

import argparse
import errno
import json
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    from .mcp_server import MCP_REQUEST_SERVICE  # type: ignore
except Exception:
    from alde.mcp_server import MCP_REQUEST_SERVICE  # type: ignore


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")


class McpTcpRequestHandler(socketserver.StreamRequestHandler):
    """JSON-line MCP handler over TCP."""

    def handle(self) -> None:
        while True:
            raw_line = self.rfile.readline()
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                request_payload = json.loads(line)
            except Exception:
                self.wfile.write(_json_bytes({"error": "invalid_json"}))
                continue

            response_payload = MCP_REQUEST_SERVICE.dispatch_object(request_payload)
            self.wfile.write(_json_bytes(response_payload))


class McpTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class McpHttpRequestHandler(BaseHTTPRequestHandler):
    """HTTP MCP handler.

    - POST /mcp  with JSON body {method, params}
    - GET  /health for liveness
    """

    server_version = "alde-mcp-http/1.0"

    def _write_json_response(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._write_json_response({"ok": True, "server": "alde-mcp-http"}, status=200)
            return
        self._write_json_response({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in {"", "/mcp"}:
            self._write_json_response({"error": "not_found"}, status=404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            content_length = 0

        raw_body = self.rfile.read(max(content_length, 0)) if content_length > 0 else b""
        if not raw_body:
            self._write_json_response({"error": "invalid_json"}, status=400)
            return

        try:
            request_payload = json.loads(raw_body.decode("utf-8", errors="replace"))
        except Exception:
            self._write_json_response({"error": "invalid_json"}, status=400)
            return

        response_payload = MCP_REQUEST_SERVICE.dispatch_object(request_payload)
        status_code = 200 if "result" in response_payload else 400
        self._write_json_response(response_payload, status=status_code)

    def log_message(self, _format: str, *_args: object) -> None:  # noqa: A003
        # Keep output quiet in normal runtime; callers can add logging externally.
        return


class McpNetworkServerService:
    def _create_server(self, server_factory: Any, *, host: str, port: int) -> Any:
        try:
            return server_factory((host, port), McpTcpRequestHandler if server_factory is McpTcpServer else McpHttpRequestHandler)
        except OSError as exc:
            if host not in {"0.0.0.0", "::", ""} and exc.errno in {errno.EADDRNOTAVAIL, errno.EADDRINUSE}:
                try:
                    return server_factory(("0.0.0.0", port), McpTcpRequestHandler if server_factory is McpTcpServer else McpHttpRequestHandler)
                except OSError:
                    raise
            raise

    def run_tcp_server(self, *, host: str, port: int) -> None:
        with self._create_server(McpTcpServer, host=host, port=port) as server:
            server.serve_forever()

    def run_http_server(self, *, host: str, port: int) -> None:
        with self._create_server(ThreadingHTTPServer, host=host, port=port) as server:
            server.serve_forever()


MCP_NETWORK_SERVER_SERVICE = McpNetworkServerService()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MCP server over TCP or HTTP")
    parser.add_argument(
        "--transport",
        choices=["tcp", "http"],
        default="tcp",
        help="Network transport mode",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: 8765 for tcp, 8766 for http)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    transport = str(args.transport or "tcp").strip().lower()
    host = str(args.host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(args.port) if args.port is not None else (8765 if transport == "tcp" else 8766)

    if transport == "http":
        MCP_NETWORK_SERVER_SERVICE.run_http_server(host=host, port=port)
        return

    MCP_NETWORK_SERVER_SERVICE.run_tcp_server(host=host, port=port)


if __name__ == "__main__":
    main()
