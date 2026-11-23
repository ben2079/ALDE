from __future__ import annotations

import gzip
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import ALDE.alde.webplayer_mcp_server as webplayer_module
from ALDE.alde.webplayer_mcp_server import McpHttpRequestHandler


class _DummyHttpHandler:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

    _load_normalized_request_path = staticmethod(McpHttpRequestHandler._load_normalized_request_path)
    _load_request_header_value = McpHttpRequestHandler._load_request_header_value
    _has_streamable_http_headers = McpHttpRequestHandler._has_streamable_http_headers
    _load_accept_header_values = McpHttpRequestHandler._load_accept_header_values
    _wants_sse_stream = McpHttpRequestHandler._wants_sse_stream
    _load_sse_bootstrap_body = staticmethod(McpHttpRequestHandler._load_sse_bootstrap_body)
    _decode_sentinel_header_value = staticmethod(McpHttpRequestHandler._decode_sentinel_header_value)
    _load_local_origin_hostnames = staticmethod(McpHttpRequestHandler._load_local_origin_hostnames)
    _load_request_origin_hostnames = McpHttpRequestHandler._load_request_origin_hostnames
    _origin_matches_allowlist_entry = staticmethod(McpHttpRequestHandler._origin_matches_allowlist_entry)
    _origin_is_allowed = McpHttpRequestHandler._origin_is_allowed
    _load_jsonrpc_error_payload = staticmethod(McpHttpRequestHandler._load_jsonrpc_error_payload)
    _load_streamable_http_validation_error = McpHttpRequestHandler._load_streamable_http_validation_error
    _accepts_gzip = McpHttpRequestHandler._accepts_gzip


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        ("", False),
        ("identity", False),
        ("gzip", True),
        ("br, GZip; q=0.5", True),
        ("gzip;q=0", False),
        ("gzip;q=invalid", False),
        ("gzip;q=2", False),
    ],
)
def test_scope_compression_respects_client_encoding(encoding, expected) -> None:
    assert _DummyHttpHandler({"Accept-Encoding": encoding})._accepts_gzip() is expected


def test_streamable_validation_rejects_forbidden_origin(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_MCP_ALLOWED_ORIGINS", raising=False)
    handler = _DummyHttpHandler(
        {
            "Origin": "https://example.com",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "prompts/list",
        }
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "prompts/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            }
        },
    }

    validation_error = handler._load_streamable_http_validation_error(payload)
    assert validation_error is not None
    error_payload, status = validation_error
    assert status == 403
    assert ((error_payload.get("error") or {}).get("code")) == -32010


def test_streamable_validation_accepts_same_host_origin_by_default(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_MCP_ALLOWED_ORIGINS", raising=False)
    handler = _DummyHttpHandler(
        {
            "Host": "192.168.0.48:8766",
            "Origin": "http://192.168.0.48",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "prompts/list",
        }
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "prompts/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            }
        },
    }

    validation_error = handler._load_streamable_http_validation_error(payload)
    assert validation_error is None


def test_streamable_validation_accepts_ide_webview_origin_by_default(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_MCP_ALLOWED_ORIGINS", raising=False)
    handler = _DummyHttpHandler(
        {
            "Origin": "vscode-webview://8f4c3d00-0c58-4876-8bf7-8e1f3a7ad9f9",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "prompts/list",
        }
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "prompts/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            }
        },
    }

    validation_error = handler._load_streamable_http_validation_error(payload)
    assert validation_error is None


def test_origin_policy_accepts_loopback_localhost_subdomains(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_MCP_ALLOWED_ORIGINS", raising=False)
    handler = _DummyHttpHandler({})

    assert handler._origin_is_allowed("http://mcp-app.localhost:4300") is True
    assert handler._origin_is_allowed("https://example.com") is False


def test_has_streamable_http_headers_detects_mcp_headers() -> None:
    handler = _DummyHttpHandler({"Mcp-Method": "prompts/list"})
    assert handler._has_streamable_http_headers() is True


def test_has_streamable_http_headers_is_false_without_mcp_headers() -> None:
    handler = _DummyHttpHandler({"Content-Type": "application/json"})
    assert handler._has_streamable_http_headers() is False


def test_wants_sse_stream_detects_event_stream_accept_header() -> None:
    handler = _DummyHttpHandler({"Accept": "application/json, text/event-stream"})
    assert handler._wants_sse_stream() is True


def test_load_sse_bootstrap_body_points_to_mcp_endpoint() -> None:
    sse_body = _DummyHttpHandler._load_sse_bootstrap_body("mcp")
    assert "event: endpoint" in sse_body
    assert "data: /mcp" in sse_body


def test_audio_scope_endpoint_returns_uncached_pcm_payload(monkeypatch) -> None:
    class ScopeServiceStub:
        raise_error = False
        requests: list[object] = []

        def load_scope_payload(self, request=None) -> dict[str, object]:
            self.requests.append(request)
            if self.raise_error:
                raise RuntimeError("capture reader failed")
            return {
                "ok": True,
                "status": "ready",
                "sample_rate_hz": 48000,
                "channels": [{"index": 0, "samples": [0.25, -0.25]}],
            }

    monkeypatch.setattr(webplayer_module, "PLAYBACK_SCOPE_SERVICE", ScopeServiceStub())
    server = ThreadingHTTPServer(("127.0.0.1", 0), McpHttpRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scope_urls = (
            f"http://127.0.0.1:{server.server_port}/ui/Mpc.App.ui/scope",
            f"http://127.0.0.1:{server.server_port}/ui/WebMpc.app.ui/scope",
        )
        for scope_url in scope_urls:
            with urlopen(
                f"{scope_url}?channels=in1%2Cin2%2Cout1%2Cout2%2Cout3%2Cout4&window_ms=20&points=512",
                timeout=2,
            ) as response:
                payload = json.loads(response.read())
                assert response.status == 200
                assert response.headers.get("Cache-Control") == "no-store"
                assert payload["channels"][0]["samples"] == [0.25, -0.25]
        assert webplayer_module.PLAYBACK_SCOPE_SERVICE.requests[-1] == {
            "channels": ["in1,in2,out1,out2,out3,out4"],
            "window_ms": ["20"],
            "points": ["512"],
        }
        webplayer_module.PLAYBACK_SCOPE_SERVICE.raise_error = True
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/ui/Mpc.App.ui/scope", timeout=2)
        except HTTPError as error:
            assert error.code == 503
            assert error.headers.get("Cache-Control") == "no-store"
            assert json.loads(error.read()) == {
                "ok": False,
                "status": "unavailable",
                "error": "scope_request_failed",
            }
        else:
            raise AssertionError("scope service errors must return a JSON 503 response")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_audio_scope_endpoint_compresses_only_when_accepted(monkeypatch) -> None:
    payload = {
        "ok": True,
        "status": "ready",
        "sample_rate_hz": 48000,
        "channels": [
            {"index": index, "samples": [0.125, -0.125] * 1024}
            for index in range(4)
        ],
    }

    class ScopeServiceStub:
        def load_scope_payload(self, request=None):
            return payload

    monkeypatch.setattr(webplayer_module, "PLAYBACK_SCOPE_SERVICE", ScopeServiceStub())
    server = ThreadingHTTPServer(("127.0.0.1", 0), McpHttpRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/ui/Mpc.App.ui/scope"
        for encoding, compressed in (("gzip", True), ("identity", False), ("gzip;q=0", False)):
            with urlopen(Request(url, headers={"Accept-Encoding": encoding}), timeout=2) as response:
                body = response.read()
                assert response.status == 200
                assert response.headers["Vary"] == "Accept-Encoding"
                assert response.headers["Cache-Control"] == "no-store"
                assert int(response.headers["Content-Length"]) == len(body)
                assert (response.headers.get("Content-Encoding") == "gzip") is compressed
                decoded = gzip.decompress(body) if compressed else body
                assert json.loads(decoded) == payload
                if compressed:
                    assert len(body) < len(decoded) / 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
