from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import json
import os
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from ALDE.alde.mcp_net_server import McpHttpRequestHandler as NetworkMcpHttpRequestHandler
from ALDE.alde.webplayer_mcp_server import McpHttpRequestHandler, TidalApiService, WebPlayerMcpRequestService


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
    assert "webplayer_favorite_current_track" in names
    assert "webplayer_volume_adjust" in names
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


def test_favorite_tool_exposes_wait_for_player_parameter() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])

    favorite_definition = None
    for tool_definition in tools:
        function_definition = tool_definition.get("function") or {}
        if function_definition.get("name") == "webplayer_favorite_current_track":
            favorite_definition = function_definition
            break

    assert favorite_definition is not None
    properties = (favorite_definition.get("parameters") or {}).get("properties") or {}
    assert "playback_backend" in properties
    assert "wait_for_player_s" in properties
    assert "track_id" in properties
    assert "country_code" in properties
    assert "cdp_port" not in properties
    assert "cdp_click_timeout_s" not in properties


def test_volume_tool_exposes_volume_target_parameter() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])

    volume_definition = None
    for tool_definition in tools:
        function_definition = tool_definition.get("function") or {}
        if function_definition.get("name") == "webplayer_volume_adjust":
            volume_definition = function_definition
            break

    assert volume_definition is not None
    properties = (volume_definition.get("parameters") or {}).get("properties") or {}
    playback_backend = properties.get("playback_backend") or {}
    assert playback_backend.get("default") == "browser"
    assert playback_backend.get("enum") == ["browser", "api_only", "api"]
    volume_target = properties.get("volume_target") or {}
    assert volume_target.get("default") == "browser"
    assert volume_target.get("enum") == ["browser", "system"]


def test_search_tool_exposes_api_only_country_code_parameter() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])

    search_definition = None
    for tool_definition in tools:
        function_definition = tool_definition.get("function") or {}
        if function_definition.get("name") == "webplayer_search":
            search_definition = function_definition
            break

    assert search_definition is not None
    properties = (search_definition.get("parameters") or {}).get("properties") or {}
    playback_backend = properties.get("playback_backend") or {}
    assert playback_backend.get("default") == "browser"
    assert playback_backend.get("enum") == ["browser", "api_only", "api"]
    country_code = properties.get("country_code") or {}
    assert country_code.get("default") == "DE"


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


def test_tidal_api_request_merges_credentials_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("WEBPLAYER_MCP_TIDAL_AUTHORIZATION", "Bearer env-token")
    monkeypatch.setenv("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN", "env-x-token")

    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_request",
        arguments={
            "url": "https://tidal.com/v1/ping",
            "headers": {"Authorization": "Bearer explicit-token", "X-Test": "1"},
        },
    )

    assert request_object.headers["Authorization"] == "Bearer explicit-token"
    assert request_object.headers["x-tidal-token"] == "env-x-token"
    assert request_object.headers["X-Test"] == "1"


def test_tidal_api_request_uses_default_public_token_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_MCP_TIDAL_AUTHORIZATION", raising=False)
    monkeypatch.delenv("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN", raising=False)

    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_request",
        arguments={
            "url": "https://tidal.com/v1/ping",
        },
    )

    assert request_object.headers["x-tidal-token"] == "CzET4vdadNUFQ5JU"


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


def test_tools_call_rejects_browser_control_in_api_only_mode() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_play",
                "arguments": {"playback_backend": "api_only"},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "unsupported_api_only" in result_payload.get("stdout", "")
    assert "playback_backend=api_only" in result_payload.get("stdout", "")
    assert "object_name=webplayer_play" in result_payload.get("stdout", "")


def test_tools_call_rejects_api_only_favorite_without_track_id() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_favorite_current_track",
                "arguments": {"playback_backend": "api_only"},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_track_id_api_only" in result_payload.get("stdout", "")


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
    assert payload.get("result", {}).get("protocolVersion") == "2026-07-28"
    capabilities = payload.get("result", {}).get("capabilities", {})
    assert "tools" in capabilities
    assert "prompts" in capabilities


def test_jsonrpc_initialize_negotiates_webplayer_ui_extension() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {}}},
            },
        }
    )

    result = payload["result"]
    assert result["protocolVersion"] == "2026-07-28"
    assert result["capabilities"]["extensions"]["io.modelcontextprotocol/ui"]["resourceUri"] == (
        service._UI_RESOURCE_URI
    )


def test_jsonrpc_webplayer_resources_use_negotiated_ui_state() -> None:
    service = WebPlayerMcpRequestService()
    service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"extensions": {"io.modelcontextprotocol/ui": {}}}},
        }
    )

    payload = service.dispatch_object({"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}})
    resources = payload["result"]["resources"]
    assert resources[0]["uri"] == service._UI_RESOURCE_URI


def test_jsonrpc_webplayer_resource_is_mcp_app_mini_controls() -> None:
    service = WebPlayerMcpRequestService()
    service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"extensions": {"io.modelcontextprotocol/ui": {}}}},
        }
    )

    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": service._UI_RESOURCE_URI},
        }
    )
    content = payload["result"]["contents"][0]
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert "window.mcp.callTool" in content["text"]
    assert "webplayer_favorite_current_track" in content["text"]
    assert "id=\"favoriteButton\"" in content["text"]
    assert 'id="actionFeedback"' in content["text"]
    assert 'role="status"' in content["text"]
    assert 'aria-live="polite"' in content["text"]
    assert 'aria-atomic="true"' in content["text"]
    assert "danger.is-active" in content["text"]
    assert 'class="favorite-icon"' in content["text"]
    assert ".favorite-icon path" in content["text"]
    assert ".icon-button.danger.is-active .favorite-icon path" in content["text"]
    assert "id=\"playToggleButton\"" in content["text"]
    assert '"wait_for_player_s":2' in content["text"]
    assert "webplayer_volume_adjust" in content["text"]
    assert "volumeModeBrowser" in content["text"]
    assert "volumeModeSystem" in content["text"]
    assert "id=\"metaQuality\"" in content["text"]
    assert "id=\"metaBitrate\"" in content["text"]
    assert "webplayer_backward" in content["text"]
    assert "<svg" in content["text"]
    assert "marquee-track" in content["text"]
    assert content["text"].index('data-tool="webplayer_favorite_current_track"') < content["text"].index('id="playToggleButton"')
    assert 'data-tool="webplayer_stop"' not in content["text"]
    assert "JSON.stringify(result)" not in content["text"]


def test_webplayer_http_fallback_serves_mini_controls_html() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), McpHttpRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        port = int(server.server_address[1])
        for path in ("/ui/webplayer/mini-controls", "/ui/webplayer/mini-controls.html"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as response:
                body = response.read().decode("utf-8")
                content_type = response.headers.get_content_type()
                cache_control = str(response.headers.get("Cache-Control") or "")
                pragma = str(response.headers.get("Pragma") or "")

            assert response.status == 200
            assert content_type == "text/html"
            assert "no-store" in cache_control
            assert "no-cache" in cache_control
            assert pragma == "no-cache"
            assert "WebPlayer Mini Controls" in body
            assert "<svg" in body
            assert "marquee-track" in body
            assert "webplayer_favorite_current_track" in body
            assert "id=\"favoriteButton\"" in body
            assert 'id="actionFeedback"' in body
            assert 'role="status"' in body
            assert 'aria-live="polite"' in body
            assert 'aria-atomic="true"' in body
            assert 'id="status"' not in body
            feedback_style = body.split(".action-feedback {", 1)[1].split("}", 1)[0]
            assert "position: absolute;" in feedback_style
            assert "pointer-events: none;" in feedback_style
            assert "danger.is-active" in body
            assert 'class="favorite-icon"' in body
            assert 'data-ansi-background=":[\\|/]:"' in body
            assert "%3A%5B%5C%7C%2F%5D%3A" in body
            panel_style = body.split(".panel {", 1)[1].split("}", 1)[0]
            assert "background-repeat: repeat, no-repeat;" in panel_style
            assert "width='63' height='26'" in panel_style
            assert "letter-spacing='0'" in panel_style
            assert "background-size: 63px 26px, 100% 100%;" in panel_style
            icon_button_style = body.split(".icon-button {", 1)[1].split("}", 1)[0]
            assert "border: 0;" in icon_button_style
            assert "background: transparent;" in icon_button_style
            assert "box-shadow: none;" in icon_button_style
            inactive_heart_style = body.split(".favorite-icon path {", 1)[1].split("}", 1)[0]
            active_heart_style = body.split(
                ".icon-button.danger.is-active .favorite-icon path {",
                1,
            )[1].split("}", 1)[0]
            assert "fill: transparent;" in inactive_heart_style
            assert "fill: currentColor;" in active_heart_style
            assert "id=\"playToggleButton\"" in body
            assert body.index('data-tool="webplayer_favorite_current_track"') < body.index('id="playToggleButton"')
            assert 'data-tool="webplayer_stop"' not in body
            assert '"wait_for_player_s":2' in body
            assert "webplayer_volume_adjust" in body
            assert "volumeModeBrowser" in body
            assert "volumeModeSystem" in body
            assert "id=\"metaTitle\"" in body
            assert "id=\"metaArtist\"" in body
            assert "id=\"metaQuality\"" in body
            assert "id=\"metaBitrate\"" in body
            assert "id=\"albumArtwork\"" in body
            artwork_style = body.split(".album-artwork {", 1)[1].split("}", 1)[0]
            assert "width: 80px;" in artwork_style
            assert "height: 80px;" in artwork_style
            assert "flex: 0 0 80px;" in artwork_style
            assert "id=\"metaBpm\"" in body
            assert "id=\"metaKey\"" in body
            assert "Bit/Hz: none" in body
            assert "parseSynchronizedLyrics" in body
            assert "lyrics_subtitles_base64" in body
            assert "window.setInterval(updateLyricsMarquee, 500)" in body
            assert "updateFavoriteButtonTarget" in body
            assert 'setStatus("Adding to favorites…", "loading", 0)' in body
            assert 'actionError ? "error" : "success"' in body
            assert "actionFeedbackEl.hidden = false" in body
            assert "actionFeedbackEl.hidden = true" in body
            assert "`Favorite failed: ${actionError}`" in body
            assert "`Favorite failed: ${errorMessage}`" in body
            assert 'playback_backend: "api_only"' in body
            assert "track_id: normalizedTrackId" in body
            assert "hasBitDepthSampleRate" in body
            assert 'metaTitleEl.textContent = `Title: ${title || "—"}`' in body
            assert 'metaArtistEl.textContent = `Artist: ${artist || "—"}`' in body
            update_now_playing_script = body.split("function updateNowPlaying", 1)[1].split(
                "function summarizeAction",
                1,
            )[0]
            assert "const trackId =" in update_now_playing_script
            assert "const countryCode =" in update_now_playing_script
            assert "JSON.stringify(result)" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_cors_allows_local_development_ports_for_default_origins() -> None:
    allowed_origins = {"http://localhost", "http://127.0.0.1"}
    handlers = (McpHttpRequestHandler, NetworkMcpHttpRequestHandler)

    for handler in handlers:
        assert handler._origin_matches_allowlist("http://localhost:3000", allowed_origins)  # noqa: SLF001
        assert handler._origin_matches_allowlist("http://127.0.0.1:5173", allowed_origins)  # noqa: SLF001
        assert not handler._origin_matches_allowlist("http://localhost.example:3000", allowed_origins)  # noqa: SLF001
        assert not handler._origin_matches_allowlist("http://localhost:bad", allowed_origins)  # noqa: SLF001


def test_favorite_command_reports_missing_optional_dependencies_and_has_one_header_builder() -> None:
    service = WebPlayerMcpRequestService()
    favorite_command = service.player_service._load_favorite_current_track_command(  # noqa: SLF001
        player_selector="chromium",
        arguments={"wait_for_player_s": 2},
    )
    script = favorite_command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]

    assert "error=missing_python_dependency:websockets" in script
    assert "error=missing_python_dependency:cryptography" in script
    assert script.count("def tidal_headers(") == 0
    assert "def tidal_headers_final(" in script


def test_jsonrpc_tools_call_returns_spec_content_blocks() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "webplayer_search",
                "arguments": {},
            },
        }
    )

    result = payload["result"]
    content = result["content"]
    assert isinstance(content, list)
    assert content
    assert content[0]["type"] == "text"
    assert isinstance(content[0]["text"], str)
    assert isinstance(result["structuredContent"], dict)
    assert result["isError"] is True


def test_webplayer_favorite_and_volume_commands_are_built() -> None:
    service = WebPlayerMcpRequestService()
    supported_actions = service.player_service.load_supported_actions()
    assert "webplayer_favorite_current_track" in supported_actions
    assert "webplayer_volume_adjust" in supported_actions

    favorite_command = service.player_service._load_favorite_current_track_command(  # noqa: SLF001
        player_selector="chromium",
        arguments={"wait_for_player_s": 2},
    )
    assert "api.tidal.com/v1/sessions" in favorite_command
    assert "api.tidal.com/v1/search?" in favorite_command
    assert "listen.tidal.com/v1/search?" in favorite_command
    assert "countryCode" in favorite_command
    assert "userCollectionTracks/me/relationships/items" in favorite_command
    assert "Object.keys(localStorage)" in favorite_command
    assert "refreshToken" in favorite_command
    assert "clientSecret" in favorite_command
    assert "oauth2/token" in favorite_command
    assert "tidal_headers_final" in favorite_command
    assert "tidal_public_headers" in favorite_command
    assert "headers = tidal_headers_final(access_token)" in favorite_command
    assert '"Be" "arer " + access_token' in favorite_command
    assert '"metadata", "xesam:url"' in favorite_command
    assert "mpris:trackid" in favorite_command
    assert 're.search(r"/track/([0-9]+)", url_value)' in favorite_command
    assert "def load_track_id_from_cache" in favorite_command
    assert '"Cache" / "Cache_Data"' in favorite_command
    assert 'emit(f"track_id_source={track_id_source or \'unknown\'}")' in favorite_command
    assert "token_browser_user_mismatch" in favorite_command
    assert "favorite_state=liked" in favorite_command
    assert "favorite_backend=api" in favorite_command
    assert "favorite_backend=cdp" in favorite_command
    assert "_run_cdp_favorite_click" in favorite_command
    assert "load_stream_quality_via_relationship_items" in favorite_command
    assert "stream_quality_source=" in favorite_command
    assert 'emit(f"quality={quality_value}")' in favorite_command
    assert 'emit(f"bitrate={bitrate_value}")' in favorite_command
    assert "openapi_tracks_mediaTags_via_relationship_items" in favorite_command
    assert '"page[size]": 100' in favorite_command
    assert '"filter[id]": track_id' not in favorite_command
    assert 'button[data-test="footer-favorite-button"]' in favorite_command
    assert 'button[data-test="add-to-favorites-button"]' in favorite_command
    assert "state.textContent" in favorite_command
    assert "aus meiner musik entfernen" in favorite_command
    assert "remove from my collection" in favorite_command
    assert 'button[aria-label*="Add to playlist" i]' not in favorite_command
    assert "load_access_token_for_browser_user" in favorite_command
    assert "token_user_alignment=browser_user_match" in favorite_command
    assert "load_browser_favorites_snapshot" in favorite_command
    assert "update_session_environment" in favorite_command
    assert "favorite_verify_source=" in favorite_command
    assert "favorite_verified=" in favorite_command
    assert "favorite_verify_openapi_status=" in favorite_command
    assert "favorite_verify_ids_status=" in favorite_command
    assert "account_alignment_required=" in favorite_command
    assert "favorite_target_collection_user_id=" in favorite_command
    assert "if not token_browser_user_mismatch and track_id in browser_favorite_track_ids" in favorite_command
    assert 'emit("favorite_state=unknown")' not in favorite_command
    assert 'emit(f"favorite_result=token_user_{favorite_result}")' not in favorite_command
    assert "openapi_userCollectionTracks_me" in favorite_command
    assert "openapi_write_conflict" in favorite_command
    assert "v1_favorites_ids" in favorite_command
    assert "WEBPLAYER_TOKEN_USER_ID" in favorite_command
    assert "token_user_env_file=" in favorite_command
    assert "session-env" in favorite_command
    assert "TidalOAuthTokenService" in favorite_command
    assert "TIDAL_API_TOKEN_FILE" in favorite_command
    assert 'source_profile.parent / "Local State"' in favorite_command
    assert 'b"AuthDB/" in child.read_bytes()' not in favorite_command
    assert 'subprocess.run(["playerctl", "-l"]' in favorite_command
    assert "CzET4vdadNUFQ5JU" in favorite_command
    assert "WEBPLAYER_CDP_PORT=" in favorite_command
    assert "WEBPLAYER_CDP_CLICK_TIMEOUT_S=" in favorite_command

    volume_command = service.player_service._load_volume_adjust_command(  # noqa: SLF001
        player_selector="chromium",
        arguments={"delta_percent": 5},
    )
    assert "pactl set-sink-input-volume" in volume_command
    assert "pactl set-sink-volume @DEFAULT_SINK@" in volume_command
    assert "volume_target=browser" in volume_command
    assert "LC_ALL=C pactl list sink-inputs" in volume_command
    assert 'application\\.name = "Chromium"' in volume_command
    assert "volume_adjustment" in volume_command
    assert "sink_input_id" in volume_command
    assert "pactl_missing" in volume_command
    assert "delta_percent=5" in volume_command


def test_webplayer_track_metadata_helpers_normalize_quality_and_audio_format() -> None:
    service = WebPlayerMcpRequestService()
    helper_namespace: dict[str, object] = {}
    exec(service.player_service._load_track_metadata_helpers_script(), helper_namespace)  # noqa: S102, SLF001

    normalize_quality = helper_namespace["normalize_stream_quality"]
    load_audio_format = helper_namespace["load_audio_format_reference"]
    load_artwork_url = helper_namespace["load_artwork_url"]

    assert callable(normalize_quality)
    assert normalize_quality(["LOSSLESS", "HIRES_LOSSLESS"]) == "HI_RES_LOSSLESS"
    assert normalize_quality("HI_RES") == "HI_RES_LOSSLESS"
    assert normalize_quality("MASTER") == "HI_RES_LOSSLESS"
    assert normalize_quality("unknown") == "none"
    assert callable(load_audio_format)
    assert load_audio_format("LOSSLESS") == ("16", "44.1", "16/44.1 kHz")
    assert load_audio_format("HIRES_LOSSLESS") == ("24", "192", "24/192 kHz")
    assert callable(load_artwork_url)
    assert load_artwork_url("903a2cb9-f702-43cf-a643-eb659fc381e8") == (
        "https://resources.tidal.com/images/903a2cb9/f702/43cf/a643/eb659fc381e8/320x320.jpg"
    )


def test_webplayer_now_playing_command_exposes_rich_track_metadata() -> None:
    service = WebPlayerMcpRequestService()
    now_playing_command = service.player_service._load_now_playing_command(  # noqa: SLF001
        player_selector="chromium"
    )

    assert "load_stream_quality_via_relationship_items" in now_playing_command
    assert "stream_quality_source=" in now_playing_command
    assert 'emit(f"quality={quality_value}")' in now_playing_command
    assert 'emit(f"bitrate={bitrate_value}")' in now_playing_command
    assert "load_quality_bitrate_reference" in now_playing_command
    assert "openapi_tracks_mediaTags_via_relationship_items" in now_playing_command
    assert "normalize_stream_quality" in now_playing_command
    assert "HI_RES_LOSSLESS" in now_playing_command
    assert "load_tidal_track_metadata" in now_playing_command
    assert "load_playback_context_metadata" in now_playing_command
    assert "load_track_id_from_public_search" in now_playing_command
    assert 'track_id_source = "public_search"' in now_playing_command
    assert 'emit(f"track_id_source={track_id_source or \'unknown\'}")' in now_playing_command
    assert "albums.coverArt,lyrics" in now_playing_command
    assert "/playbackinfopostpaywall?" in now_playing_command
    assert "audioSamplingRate" in now_playing_command
    assert "lyrics_text_base64" in now_playing_command
    assert "lyrics_subtitles_base64" in now_playing_command
    assert 'emit(f"bit_depth={bit_depth}")' in now_playing_command
    assert 'emit(f"sample_rate_khz={sample_rate_khz}")' in now_playing_command
    assert 'emit(f"artwork_url={artwork_url}")' in now_playing_command
    assert 'emit(f"country_code={country_code}")' in now_playing_command
    assert '"bpm",' in now_playing_command
    assert '"musical_key",' in now_playing_command
    assert '"api_relationship_items"' in now_playing_command

    script = now_playing_command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    compile(script, "<webplayer_now_playing>", "exec")


def test_webplayer_api_only_commands_are_built() -> None:
    service = WebPlayerMcpRequestService()

    unsupported_play_command = service.player_service.load_object_command(  # noqa: SLF001
        object_name="webplayer_play",
        query=None,
        player_selector="chromium",
        arguments={"playback_backend": "api_only"},
    )
    assert "unsupported_api_only" in unsupported_play_command
    assert "object_name=webplayer_play" in unsupported_play_command

    search_command = service.player_service.load_object_command(  # noqa: SLF001
        object_name="webplayer_search",
        query="Muse",
        player_selector="chromium",
        arguments={"playback_backend": "api_only", "query": "Muse"},
    )
    assert "listen.tidal.com/v1/search" in search_command
    assert "search_backend=api_only" in search_command
    assert "WEBPLAYER_SEARCH_QUERY=" in search_command
    assert "WEBPLAYER_COUNTRY_CODE=" in search_command
    assert "xdg-open" not in search_command

    favorite_command = service.player_service.load_object_command(  # noqa: SLF001
        object_name="webplayer_favorite_current_track",
        query=None,
        player_selector="chromium",
        arguments={"playback_backend": "api_only", "track_id": "12345", "country_code": "DE"},
    )
    assert "userCollectionTracks/me/relationships/items" in favorite_command
    assert "favorite_backend=api_only" in favorite_command
    assert "load_stream_quality_via_relationship_items" in favorite_command
    assert "stream_quality_source=" in favorite_command
    assert 'emit(f"quality={quality_value}")' in favorite_command
    assert 'emit(f"bitrate={bitrate_value}")' in favorite_command
    assert "openapi_tracks_mediaTags_via_relationship_items" in favorite_command
    assert "WEBPLAYER_TRACK_ID=" in favorite_command
    assert "WEBPLAYER_COUNTRY_CODE=" in favorite_command
    assert "WEBPLAYER_CDP_PORT=9222" in favorite_command
    assert "WEBPLAYER_CDP_TOKEN_TIMEOUT_S=45" in favorite_command
    assert "class TidalBrowserAccessTokenService" in favorite_command
    assert "except ImportError:" in favorite_command
    assert 'self.last_error = "cdp_websockets_missing"' in favorite_command
    assert "Network.requestWillBeSentExtraInfo" in favorite_command
    assert "/json/new?" in favorite_command
    assert "/json/close/" in favorite_command
    assert '"source": "browser_cdp_network"' in favorite_command
    assert "tempfile.mkstemp" in favorite_command
    assert "os.fchmod(file_descriptor, 0o600)" in favorite_command
    assert "error=playerctl_missing" not in favorite_command

    script = favorite_command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    compile(script, "<webplayer_favorite_api_only>", "exec")

    browser_favorite_command = service.player_service.load_object_command(  # noqa: SLF001
        object_name="webplayer_favorite_current_track",
        query=None,
        player_selector="chromium",
        arguments={"playback_backend": "browser"},
    )
    assert '"google-chrome"' in browser_favorite_command
    assert '"google-chrome-stable"' in browser_favorite_command


def test_browser_access_token_service_parses_and_stores_validated_token(tmp_path: Path) -> None:
    service = WebPlayerMcpRequestService()
    namespace = {
        "base64": base64,
        "binascii": binascii,
        "json": json,
        "os": os,
        "Path": Path,
        "tempfile": tempfile,
        "time": time,
    }
    exec(  # noqa: S102
        service.player_service._load_browser_access_token_service_script(),  # noqa: SLF001
        namespace,
    )
    token_service_class = namespace["TidalBrowserAccessTokenService"]
    expires_at = int(time.time()) + 3600
    payload_segment = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    access_token = f"header.{payload_segment}.signature"

    request_event = {
        "method": "Network.requestWillBeSentExtraInfo",
        "params": {"headers": {"authorization": f"Bearer {access_token}"}},
    }
    assert token_service_class._load_bearer_token(request_event) == access_token
    assert token_service_class._load_bearer_token(
        {"method": "Network.requestWillBeSentExtraInfo", "params": {"headers": {}}}
    ) == ""

    token_path = tmp_path / "tidal-token.json"
    token_service = token_service_class(
        debug_port=9222,
        timeout_s=45,
        token_path=token_path,
        public_web_token="public-token",
        user_agent="test-agent",
    )
    token_service._store_access_token(
        access_token,
        {"sessionId": "session-1", "userId": "user-1"},
    )

    stored_payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert stored_payload["access_token"] == access_token
    assert stored_payload["expires_at"] == expires_at
    assert stored_payload["session_id"] == "session-1"
    assert stored_payload["source"] == "browser_cdp_network"
    assert stored_payload["user_id"] == "user-1"
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_webplayer_library_tracks_use_current_route_and_existing_cdp_tab() -> None:
    service = WebPlayerMcpRequestService()
    library_command = service.player_service.load_object_command(  # noqa: SLF001
        object_name="webplayer_library_play",
        query=None,
        player_selector="chromium",
        arguments={"section": "my_collection_tracks", "cdp_port": 9222},
    )

    assert "https://tidal.com/my-collection/tracks" in library_command
    assert "my-collections/tracks" not in library_command
    assert "CDP_NAVIGATE_URL=" in library_command
    assert "CDP_PORT=9222 python3 - <<'PY'" in library_command
    assert "'method': 'Page.navigate'" in library_command
    assert "cdp_navigated=true" in library_command
    assert "cdp_navigate_exit_code=$?" in library_command
    assert 'button[data-test*="play" i]' in library_command


def test_webplayer_browser_fallback_uses_requested_cdp_port() -> None:
    service = WebPlayerMcpRequestService()
    search_play_command = service.player_service.load_object_command(  # noqa: SLF001
        object_name="webplayer_search_play",
        query="Muse",
        player_selector="chromium",
        arguments={"cdp_port": 9333},
    )

    assert "CDP_PORT=9333 python3 - <<'PY'" in search_play_command
    assert "--remote-debugging-port=9333" in search_play_command


def test_browser_access_token_service_processes_queued_cdp_header(tmp_path: Path) -> None:
    service = WebPlayerMcpRequestService()

    class FakeWebSocket:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.messages: list[str] = []
            self.sent_methods: list[str] = []

        async def send(self, raw_payload: str) -> None:
            payload = json.loads(raw_payload)
            method = str(payload["method"])
            self.sent_methods.append(method)
            if method == "Page.close":
                return
            if method == "Page.navigate":
                self.messages.append(
                    json.dumps(
                        {
                            "method": "Network.requestWillBeSentExtraInfo",
                            "params": {
                                "headers": {
                                    "Authorization": f"Bearer {self.access_token}",
                                }
                            },
                        }
                    )
                )
            self.messages.append(json.dumps({"id": payload["id"], "result": {}}))

        async def recv(self) -> str:
            return self.messages.pop(0)

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeWebsockets:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        def connect(self, *_args: object, **_kwargs: object) -> FakeConnection:
            return FakeConnection(self.websocket)

    namespace = {
        "asyncio": asyncio,
        "base64": base64,
        "binascii": binascii,
        "json": json,
        "os": os,
        "Path": Path,
        "tempfile": tempfile,
        "time": time,
        "WebSocketException": RuntimeError,
    }
    exec(  # noqa: S102
        service.player_service._load_browser_access_token_service_script(),  # noqa: SLF001
        namespace,
    )
    token_service_class = namespace["TidalBrowserAccessTokenService"]
    access_token = "captured-browser-access-token-with-sufficient-length"
    fake_websocket = FakeWebSocket(access_token)
    namespace["websockets"] = FakeWebsockets(fake_websocket)
    token_service = token_service_class(
        debug_port=9222,
        timeout_s=5,
        token_path=tmp_path / "tidal-token.json",
        public_web_token="public-token",
        user_agent="test-agent",
    )
    token_service._create_page_target = lambda: {
        "id": "target-1",
        "webSocketDebuggerUrl": "ws://test-target",
    }
    token_service._load_session_payload = lambda token: (
        {"sessionId": "session-1", "userId": "user-1"}
        if token == access_token
        else {}
    )

    captured_token, session_payload = asyncio.run(token_service._capture_access_token())

    assert captured_token == access_token
    assert session_payload["userId"] == "user-1"
    assert fake_websocket.sent_methods == [
        "Network.enable",
        "Page.enable",
        "Page.navigate",
        "Page.close",
    ]


def test_webplayer_favorite_command_embedded_python_script_compiles() -> None:
    service = WebPlayerMcpRequestService()
    favorite_command = service.player_service._load_favorite_current_track_command(  # noqa: SLF001
        player_selector="chromium",
        arguments={"wait_for_player_s": 2},
    )
    script = favorite_command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    compile(script, "<webplayer_favorite_current_track>", "exec")


def test_webplayer_favorite_command_does_not_pass_check_to_popen() -> None:
    service = WebPlayerMcpRequestService()
    favorite_command = service.player_service._load_favorite_current_track_command(  # noqa: SLF001
        player_selector="chromium",
        arguments={"wait_for_player_s": 2},
    )
    script = favorite_command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    module = ast.parse(script)
    popen_calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
    ]
    assert popen_calls
    for call in popen_calls:
        assert all(keyword.arg != "check" for keyword in call.keywords if keyword.arg is not None)


def test_webplayer_favorite_uses_long_running_timeout_override() -> None:
    class CommandServiceStub:
        def __init__(self) -> None:
            self.command_timeout_s: int | None = None

        def run_object(self, *, object_name: str, command: str, command_timeout_s: int | None = None):  # noqa: ANN001
            self.command_timeout_s = command_timeout_s

            class Result:
                def to_payload(self) -> dict[str, object]:
                    return {"object_name": object_name, "ok": True, "stdout": "", "stderr": ""}

            return Result()

    command_service = CommandServiceStub()
    service = WebPlayerMcpRequestService()
    service.player_service.command_service = command_service
    service.player_service.dispatch_object(
        object_name="webplayer_favorite_current_track",
        player_selector="chromium",
        arguments={"wait_for_player_s": 2, "command_timeout_s": 90},
    )
    assert command_service.command_timeout_s == 90


def test_jsonrpc_webplayer_rejects_unsupported_protocol_version() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "9999-01-01"},
        }
    )

    assert payload["error"]["code"] == -32602


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
