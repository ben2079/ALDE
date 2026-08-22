from __future__ import annotations

from ALDE.alde.webplayer_mcp_server import WebPlayerMcpRequestService


def test_webplayer_prompts_list_contains_strategy_prompts() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "prompts/list", "params": {}})
    prompts = payload.get("result", {}).get("prompts", [])
    names = {item.get("name") for item in prompts}

    assert "webplayer_operator" in names
    assert "webplayer_strategy_overview" in names
    assert "webplayer_strategy_ifaai_generalized" in names


def test_webplayer_ifaai_prompt_contains_governance_and_provisional_status() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "webplayer_strategy_ifaai_generalized",
                "arguments": {
                    "player_selector": "chromium",
                    "risk_tier": "high",
                    "compliance_mode": "strict",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "Generalized agentic governance profile" in text
    assert "risk_tier_raw=high" in text
    assert "risk_tier_effective=high" in text
    assert "compliance_mode_raw=strict" in text
    assert "compliance_mode_effective=strict" in text
    assert "profile_source=provisional_baseline" in text


def test_webplayer_overview_prompt_mentions_agentsdb_mcp() -> None:
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
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "AgentsDB MCP" in text
    assert "agentsdb/mcp" in text
    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "webplayer_library_play" in text
    assert "webplayer_open_playback_target" in text
    assert "tidal_api_request" in text


def test_webplayer_operator_prompt_prefers_current_playback_flow() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "webplayer_operator",
                "arguments": {
                    "player_selector": "chromium",
                    "search_query": "dark techno EBM",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "webplayer_library_play" in text
    assert "webplayer_open_playback_target" in text
    assert "webplayer_favorite_current_track" in text
    assert "webplayer_volume_adjust" in text
    assert "tidal_api_track_manifest" in text
