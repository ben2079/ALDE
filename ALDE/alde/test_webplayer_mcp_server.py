from __future__ import annotations

import json

from ALDE.alde.webplayer_mcp_server import TidalApiService, WebPlayerMcpRequestService


def test_tools_list_contains_required_webplayer_tools() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])
    names = {(item.get("function") or {}).get("name") for item in tools}

    assert "webplayer_play" in names
    assert "webplayer_stop" in names
    assert "webplayer_forward" in names
    assert "webplayer_backward" in names
    assert "webplayer_now_playing" in names
    assert "webplayer_search" in names
    assert "webplayer_search_play" in names
    assert "webplayer_playlist_play" in names
    assert "webplayer_library_play" in names
    assert "webplayer_open_playback_target" in names
    assert "tidal_api_request" in names
    assert "tidal_api_track" in names
    assert "tidal_api_track_manifest" in names
    assert "tidal_api_widevine" in names


def test_search_play_tool_exposes_cdp_autoclick_parameters() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])

    search_play_definition = None
    for tool_definition in tools:
        function_definition = tool_definition.get("function") or {}
        if function_definition.get("name") == "webplayer_search_play":
            search_play_definition = function_definition
            break

    assert search_play_definition is not None
    properties = (search_play_definition.get("parameters") or {}).get("properties") or {}
    assert "cdp_autoclick" in properties
    assert "cdp_port" in properties
    assert "cdp_click_timeout_s" in properties


def test_tools_call_routes_tidal_api_tool_to_tidal_service() -> None:
    class TidalApiServiceStub:
        def __init__(self) -> None:
            self.called_object_name = ""
            self.called_arguments: dict[str, object] = {}

        def dispatch_object(self, *, object_name: str, arguments: dict[str, object]) -> dict[str, object]:
            self.called_object_name = object_name
            self.called_arguments = arguments
            return {
                "ok": True,
                "object_name": object_name,
                "request_url": arguments.get("url"),
            }

    tidal_api_service_stub = TidalApiServiceStub()
    service = WebPlayerMcpRequestService(tidal_api_service=tidal_api_service_stub)

    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "tidal_api_request",
                "arguments": {
                    "method": "GET",
                    "url": "https://tidal.com/v1/ping",
                },
            },
        }
    )

    result_payload = json.loads(payload.get("result", {}).get("content", "{}"))
    assert tidal_api_service_stub.called_object_name == "tidal_api_request"
    assert tidal_api_service_stub.called_arguments["url"] == "https://tidal.com/v1/ping"
    assert result_payload["ok"] is True
    assert result_payload["object_name"] == "tidal_api_request"


def test_tidal_api_request_rejects_non_tidal_host() -> None:
    service = TidalApiService()
    payload = service.dispatch_object(
        object_name="tidal_api_request",
        arguments={
            "url": "https://example.com/v1/ping",
        },
    )

    assert payload["ok"] is False
    assert payload["error"] == "invalid_host_non_tidal"


def test_tidal_api_track_manifest_builds_expected_query() -> None:
    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_track_manifest",
        arguments={
            "track_id": "1758221",
            "adaptive": False,
            "formats": "EMBEDDED",
            "manifest_type": "FULL",
            "uri_scheme": "HTTPS",
            "usage": "STREAM",
        },
    )

    assert request_object.method == "GET"
    assert request_object.object_name == "tidal_api_track_manifest"
    assert request_object.url.startswith("https://openapi.tidal.com/v2/trackManifests/1758221?")
    assert "adaptive=false" in request_object.url
    assert "formats=EMBEDDED" in request_object.url
    assert "manifestType=FULL" in request_object.url
    assert "uriScheme=HTTPS" in request_object.url
    assert "usage=STREAM" in request_object.url


def test_tools_call_rejects_missing_search_query() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_search",
                "arguments": {},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_query" in result_payload.get("stdout", "")


def test_tools_call_rejects_missing_search_play_query() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_search_play",
                "arguments": {},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_query" in result_payload.get("stdout", "")


def test_tools_call_rejects_missing_playlist_target() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_playlist_play",
                "arguments": {},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_playlist_target" in result_payload.get("stdout", "")


def test_tools_call_rejects_invalid_library_section() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_library_play",
                "arguments": {"section": "does_not_exist"},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "invalid_library_section" in result_payload.get("stdout", "")


def test_initialize_protocol_version() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "initialize", "params": {}})
    assert payload.get("result", {}).get("protocolVersion") == "2025-03-26"
    capabilities = payload.get("result", {}).get("capabilities", {})
    assert "tools" in capabilities
    assert "prompts" in capabilities


def test_prompts_list_contains_webplayer_operator() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "prompts/list", "params": {}})
    prompts = payload.get("result", {}).get("prompts", [])
    names = {item.get("name") for item in prompts}
    assert "webplayer_operator" in names


def test_prompts_get_returns_text_message() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "webplayer_operator",
                "arguments": {
                    "player_selector": "chromium",
                    "search_query": "meshuggah",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    assert len(messages) == 1
    text = ((messages[0] or {}).get("content") or {}).get("text", "")
    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "webplayer_library_play" in text
    assert "webplayer_open_playback_target" in text
    assert "tidal_api_request" in text
    assert "meshuggah" in text


def test_strategy_overview_mentions_current_playback_tools() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "webplayer_strategy_overview",
                "arguments": {"player_selector": "chromium"},
            },
        }
    )

    messages = payload.get("result", {}).get("messages", [])
    assert len(messages) == 1
    text = ((messages[0] or {}).get("content") or {}).get("text", "")
    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "webplayer_library_play" in text
    assert "tidal_api_track_manifest" in text
