from __future__ import annotations

from ALDE.alde.mcp_server import McpRequestService


def test_initialize_exposes_prompt_capability() -> None:
    service = McpRequestService()
    payload = service.dispatch_object({"method": "initialize", "params": {}})
    capabilities = payload.get("result", {}).get("capabilities", {})
    assert "tools" in capabilities
    assert "prompts" in capabilities


def test_prompts_list_contains_strategy_prompts() -> None:
    service = McpRequestService()
    payload = service.dispatch_object({"method": "prompts/list", "params": {}})
    prompt_items = payload.get("result", {}).get("prompts", [])
    prompt_names = {item.get("name") for item in prompt_items}

    assert "tracked_prompt_sources_status" in prompt_names
    assert "server_strategy_overview" in prompt_names
    assert "server_strategy_for_skill" in prompt_names
    assert "server_strategy_ifaai_generalized" in prompt_names
    assert "agentsdb_mcp_operator" in prompt_names


def test_prompts_get_tracked_sources_mentions_agents_md() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "tracked_prompt_sources_status",
            },
        }
        
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "AGENTS.md" in text
    assert "exists=" in text


def test_prompts_get_skill_strategy_expands_fragment_instructions() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "server_strategy_for_skill",
                "arguments": {
                    "skill_profile": "xworker_mcp_webplayer_operator",
                    "tool_name": "webplayer_play",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "mcp_webplayer_call_templates" in text
    assert "Target MCP endpoint" in text
    assert "prompts/list" in text
    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "tidal_api_track_manifest" in text


def test_prompts_get_server_strategy_overview_mentions_agentsdb_mcp() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {"name": "server_strategy_overview"},
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "AgentsDB MCP" in text
    assert "agentsdb/mcp" in text


def test_prompts_get_agentsdb_mcp_operator_contains_operational_guidance() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "agentsdb_mcp_operator",
                "arguments": {
                    "context": "relation_graph_review",
                    "tool_name": "workflow_diagram",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "AgentsDB MCP operator prompt" in text
    assert "context=relation_graph_review" in text
    assert "tool_name=workflow_diagram" in text
    assert "agentsdb/mcp" in text


def test_prompts_get_unknown_prompt_returns_error() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "does_not_exist",
            },
        }
    )

    assert payload.get("error") == "prompt_not_found"


def test_prompts_get_ifaai_generalized_includes_governance_principles() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "server_strategy_ifaai_generalized",
                "arguments": {
                    "skill_profile": "xworker_core",
                    "tool_name": "route_to_agent",
                    "job_name": "interactive_planning",
                    "risk_tier": "high",
                    "compliance_mode": "strict",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "IFAAI-aligned baseline" in text
    assert "risk_tier=high" in text
    assert "compliance_mode=strict" in text
    assert "Core principles" in text
    assert "Human oversight" in text
    assert "normative_status=" in text


def test_prompts_get_ifaai_generalized_normalizes_invalid_modes() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "server_strategy_ifaai_generalized",
                "arguments": {
                    "risk_tier": "unsupported",
                    "compliance_mode": "custom",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "risk_tier=standard" in text
    assert "compliance_mode=balanced" in text


def test_prompts_get_ifaai_generalized_marks_provisional_without_guideline_file() -> None:
    service = McpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "server_strategy_ifaai_generalized",
                "arguments": {},
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    text = ((messages[0] or {}).get("content") or {}).get("text", "") if messages else ""

    assert "profile_source=provisional_baseline" in text
    assert "normative_status=non_authoritative_fallback" in text
