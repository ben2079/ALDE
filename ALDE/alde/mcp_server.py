"""
Minimal MCP stdio server exposing the unified tool registry.

Methods handled:
- initialize
- tools/list
- tools/call

Transport: stdio (one JSON object per line).
"""

import json
import os
from pathlib import Path
import hashlib
import sys
from typing import Any, Dict

try:
    from .agents_factory import execute_tool  # type: ignore
    from .agents_tools import get_tool_registry  # type: ignore
except Exception:
    try:
        from alde.agents_factory import execute_tool  # type: ignore
        from alde.agents_tools import get_tool_registry  # type: ignore
    except Exception:
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        from alde.agents_factory import execute_tool  # type: ignore
        from alde.agents_tools import get_tool_registry  # type: ignore


class McpRequestService:
    """Domain service for MCP request dispatch independent of transport."""

    _TRACKED_PROMPT_SOURCES: tuple[tuple[str, str], ...] = (
        ("agents.md", "AGENTS.md"),
        ("skills.md", "SKILLS.md"),
        ("instructs.md", "INSTRUCTS.md"),
        ("instructions.md", "INSTRUCTIONS.md"),
        ("prompts.md", "PROMPTS.md"),
        ("ifaai_guideline.md", "IFAAI_GUIDELINE.md"),
    )
    _SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-03-26")
    _UI_EXTENSION_NAME = "io.modelcontextprotocol/ui"
    _UI_RESOURCE_URI = "ui://alde/operator-console.html"
    _UI_RESOURCE_MIME_TYPE = "text/html"

    def __init__(self) -> None:
        self._ui_enabled = False

    def _workspace_root_path(self) -> Path:
        # mcp_server.py lives in ALDE/alde; the workspace root is two levels above ALDE.
        return Path(__file__).resolve().parents[2]

    def _stable_short_hash(self, file_path: Path) -> str:
        try:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            return digest[:16]
        except Exception:
            return ""

    def _load_tracked_prompt_source_status(self) -> list[dict[str, Any]]:
        workspace_root = self._workspace_root_path()
        statuses: list[dict[str, Any]] = []
        for source_name, relative_path in self._TRACKED_PROMPT_SOURCES:
            target_path = workspace_root / relative_path
            exists = target_path.is_file()
            stat_payload = target_path.stat() if exists else None
            statuses.append(
                {
                    "source": source_name,
                    "path": relative_path,
                    "exists": exists,
                    "size_bytes": int(stat_payload.st_size) if stat_payload else 0,
                    "mtime_epoch": float(stat_payload.st_mtime) if stat_payload else 0.0,
                    "sha256_16": self._stable_short_hash(target_path) if exists else "",
                }
            )
        return statuses

    def _load_prompt_fragments(self) -> dict[str, dict[str, Any]]:
        try:
            from .agents_runtime import PROMPT_FRAGMENTS  # type: ignore
        except Exception:
            from alde.agents_runtime import PROMPT_FRAGMENTS  # type: ignore
        return dict(PROMPT_FRAGMENTS or {})

    def _load_agent_skills(self) -> dict[str, dict[str, Any]]:
        try:
            from .agents_runtime import AGENT_SKILLS  # type: ignore
        except Exception:
            from alde.agents_runtime import AGENT_SKILLS  # type: ignore
        return dict(AGENT_SKILLS or {})

    def _build_tracked_prompt_sources_text(self) -> str:
        statuses = self._load_tracked_prompt_source_status()
        lines = [
            "Tracked prompt/customization sources:",
            "- exists=true means source is available for propagation in strategy synthesis.",
        ]
        for status in statuses:
            lines.append(
                "- {path}: exists={exists}, size_bytes={size}, sha256_16={sha}".format(
                    path=status.get("path") or "",
                    exists=bool(status.get("exists")),
                    size=int(status.get("size_bytes") or 0),
                    sha=status.get("sha256_16") or "",
                )
            )
        return "\n".join(lines).strip()

    def _build_server_strategy_overview_text(self) -> str:
        tracked_status = self._load_tracked_prompt_source_status()
        available_sources = [entry.get("path") for entry in tracked_status if entry.get("exists")]
        prompt_fragments = self._load_prompt_fragments()
        agent_skills = self._load_agent_skills()
        lines = [
            "MCP server strategy for prompt/skill propagation:",
            "1) Track workspace customization files (AGENTS/skills/instructs/prompts variants) as versioned sources.",
            "2) Resolve server-side skill profile policy from AGENT_SKILLS + PROMPT_FRAGMENTS.",
            "3) Expose deterministic prompts via prompts/list + prompts/get before tools/call execution.",
            "4) Treat the AgentsDB MCP layer as a first-class runtime integration point for graph, workflow, and relation-view operations.",
            "5) Use source hashes for client cache invalidation and reproducible prompt replay.",
            "",
            "Runtime facts:",
            "- available_tracked_sources={count}".format(count=len(available_sources)),
            "- tracked_source_paths={paths}".format(paths=", ".join(str(path) for path in available_sources) if available_sources else "none"),
            "- skill_profiles={count}".format(count=len(agent_skills)),
            "- prompt_fragments={count}".format(count=len(prompt_fragments)),
            "- agentsdb_mcp_reference=agentsdb/mcp",
        ]
        return "\n".join(lines).strip()

    def _build_skill_strategy_text(
        self,
        *,
        skill_profile: str,
        tool_name: str,
        job_name: str,
    ) -> str:
        skills = self._load_agent_skills()
        fragments = self._load_prompt_fragments()
        selected_profile = str(skill_profile or "").strip() or "xworker_core"
        profile = dict(skills.get(selected_profile) or {})
        fragment_names = [str(name) for name in list(profile.get("prompt_fragments") or []) if str(name).strip()]

        lines = [
            "Skill propagation strategy payload:",
            "- skill_profile={value}".format(value=selected_profile),
            "- tool_name={value}".format(value=str(tool_name or "").strip() or ""),
            "- job_name={value}".format(value=str(job_name or "").strip() or ""),
            "- role={value}".format(value=str(profile.get("role") or "")),
            "- profile_description={value}".format(value=str(profile.get("description") or "")),
            "",
            "Prompt fragment expansion order:",
        ]

        if not fragment_names:
            lines.append("- none (profile not found or has no prompt fragments)")
        for fragment_name in fragment_names:
            fragment = dict(fragments.get(fragment_name) or {})
            fragment_text = str(fragment.get("text") or "").strip()
            lines.append("- {name}".format(name=fragment_name))
            if fragment_text:
                lines.append("  text: {value}".format(value=fragment_text))
            instruction_list = [str(item) for item in list(fragment.get("instructions") or []) if str(item).strip()]
            for instruction in instruction_list:
                lines.append("  instruction: {value}".format(value=instruction))

        lines.append("")
        lines.append("Execution protocol:")
        lines.append("- Call initialize once per session.")
        lines.append("- Call prompts/get for selected skill_profile before tools/call.")
        lines.append("- Execute tools/call with explicit arguments; do not infer missing required fields.")
        return "\n".join(lines).strip()

    def _build_agentsdb_mcp_operator_text(self, *, context: str, tool_name: str) -> str:
        selected_context = str(context or "").strip() or "general_agentsdb_mcp_operation"
        selected_tool_name = str(tool_name or "").strip() or "relation_graph_view"
        return "\n".join(
            [
                "AgentsDB MCP operator prompt:",
                f"- context={selected_context}",
                f"- tool_name={selected_tool_name}",
                "- Use the AgentsDB MCP layer as the primary endpoint for graph, workflow, and relation-view operations.",
                "- Prefer deterministic tool selection and explicit parameters over free-form assumptions.",
                "- When a request touches graph topology, workflow state, or relation navigation, invoke the appropriate AgentsDB-backed tool and report the resulting structure clearly.",
                "- If the task is ambiguous, ask for clarification before mutating or summarizing state.",
                "- Reference agentsdb/mcp as the transport and capability context when composing the execution plan.",
            ]
        ).strip()

    def _build_ifaai_generalized_strategy_text(
        self,
        *,
        skill_profile: str,
        tool_name: str,
        job_name: str,
        risk_tier: str,
        compliance_mode: str,
    ) -> str:
        selected_risk_tier = str(risk_tier or "").strip().lower() or "standard"
        if selected_risk_tier not in {"low", "standard", "high", "critical"}:
            selected_risk_tier = "standard"

        selected_compliance_mode = str(compliance_mode or "").strip().lower() or "balanced"
        if selected_compliance_mode not in {"strict", "balanced", "adaptive"}:
            selected_compliance_mode = "balanced"

        selected_skill_profile = str(skill_profile or "").strip() or "xworker_core"
        selected_tool_name = str(tool_name or "").strip()
        selected_job_name = str(job_name or "").strip()

        risk_gate_map = {
            "low": "Require input and output schema checks plus deterministic tool argument validation.",
            "standard": "Require schema checks, provenance tags, and post-execution validation against requested intent.",
            "high": "Require pre-execution policy checks, bounded tool scopes, and explicit uncertainty disclosure.",
            "critical": "Require policy checks, human approval gate, immutable audit logs, and fail-closed execution.",
        }
        compliance_control_map = {
            "strict": "Default deny unknown tools, require full audit metadata, and block execution on missing constraints.",
            "balanced": "Allow known tools with mandatory traceability and deterministic fallback rules.",
            "adaptive": "Allow context-specific tool adaptation while preserving auditability and reversible decisions.",
        }

        tracked_status = self._load_tracked_prompt_source_status()
        guideline_status = next(
            (entry for entry in tracked_status if str(entry.get("path") or "") == "IFAAI_GUIDELINE.md"),
            {},
        )
        has_official_guideline = bool(guideline_status.get("exists"))
        strategy_profile_source = "workspace_guideline" if has_official_guideline else "provisional_baseline"
        normative_status = "normative_reference_present" if has_official_guideline else "non_authoritative_fallback"

        lines = [
            "Generalized agentic governance profile (IFAAI-aligned baseline):",
            "- profile_name=ifaai_generalized_v1",
            "- profile_source={value}".format(value=strategy_profile_source),
            "- normative_status={value}".format(value=normative_status),
            "- skill_profile={value}".format(value=selected_skill_profile),
            "- tool_name={value}".format(value=selected_tool_name),
            "- job_name={value}".format(value=selected_job_name),
            "- risk_tier={value}".format(value=selected_risk_tier),
            "- compliance_mode={value}".format(value=selected_compliance_mode),
            "",
            "Core principles:",
            "- Human oversight: define escalation and approval boundaries for impactful actions.",
            "- Safety and robustness: enforce bounded execution, deterministic fallbacks, and failure containment.",
            "- Transparency: attach prompt sources, policy decisions, and tool traces to each execution.",
            "- Accountability: maintain immutable audit events per route, tool call, and result mutation.",
            "- Data governance: minimize payload scope, redact sensitive fields, and preserve purpose limitation.",
            "- Interoperability: keep MCP contracts stable and versioned for cross-agent reproducibility.",
            "",
            "Operational controls:",
            "- Risk gate: {value}".format(value=risk_gate_map[selected_risk_tier]),
            "- Compliance control: {value}".format(value=compliance_control_map[selected_compliance_mode]),
            "- Prompt provenance: include tracked source hashes from tracked_prompt_sources_status.",
            "- Skill propagation: resolve from AGENT_SKILLS and expand PROMPT_FRAGMENTS before tools/call.",
            "- Execution invariants: validate required arguments and forbid silent parameter invention.",
            "- Guideline anchor: use IFAAI_GUIDELINE.md when present; otherwise treat this profile as provisional.",
            "",
            "Lifecycle checkpoints:",
            "- Plan: classify intent, risk tier, and allowed tool surface.",
            "- Preflight: run prompts/get, policy gates, and argument-schema checks.",
            "- Execute: call tools deterministically and capture route/tool telemetry.",
            "- Verify: compare outputs to requested intent and record uncertainty.",
            "- Learn: update prompt/skill strategy with auditable, source-grounded changes.",
        ]
        return "\n".join(lines).strip()

    def safe_serialize_object(self, object_payload: Any) -> Any:
        """Best-effort serialization; fallback to string for non-serializable objects."""
        try:
            json.dumps(object_payload)
            return object_payload
        except Exception:
            try:
                return str(object_payload)
            except Exception:
                return "<unserializable>"

    @staticmethod
    def _load_csv_values(raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            return [str(item).strip() for item in raw_value if str(item).strip()]
        return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]

    def _load_ui_extension_requested(self, params: Dict[str, Any]) -> bool:
        capabilities = params.get("capabilities")
        if isinstance(capabilities, dict):
            extensions = capabilities.get("extensions")
            if isinstance(extensions, dict) and self._UI_EXTENSION_NAME in extensions:
                return True
            experimental = capabilities.get("experimental")
            if isinstance(experimental, dict) and self._UI_EXTENSION_NAME in experimental:
                return True
        meta = params.get("_meta")
        if isinstance(meta, dict):
            extensions = self._load_csv_values(meta.get("io.modelcontextprotocol/extensions"))
            if self._UI_EXTENSION_NAME in extensions:
                return True
        return str(os.getenv("ALDE_MCP_UI_EXTENSION_DEFAULT") or "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _load_server_capabilities_payload(self, *, ui_enabled: bool = False) -> dict[str, Any]:
        capabilities: dict[str, Any] = {"tools": {}, "prompts": {}}
        if ui_enabled:
            capabilities["resources"] = {"listChanged": False}
            capabilities["extensions"] = {self._UI_EXTENSION_NAME: {"resourceUri": self._UI_RESOURCE_URI}}
        return capabilities

    def _load_requested_protocol_version(self, params: Dict[str, Any]) -> str:
        requested_version = str(params.get("protocolVersion") or "").strip()
        if not requested_version:
            return "2025-03-26"
        if requested_version not in self._SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(f"Unsupported protocol version: {requested_version}")
        return requested_version

    def load_initialize_result(self, params: Dict[str, Any]) -> dict[str, Any]:
        return {
            "result": {
                "protocolVersion": self._load_requested_protocol_version(params),
                "serverInfo": {"name": "alde-local-mcp"},
                "capabilities": self._load_server_capabilities_payload(
                    ui_enabled=self._load_ui_extension_requested(params)
                ),
            }
        }

    def load_tools_list_result(self, _params: Dict[str, Any]) -> dict[str, Any]:
        tool_registry = get_tool_registry()
        tools = [self.safe_serialize_object(spec) for _name, spec in tool_registry.items()]
        return {"result": {"tools": tools}}

    def _load_mcp_tool_definitions(self, *, ui_enabled: bool) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for spec in get_tool_registry().values():
            function = dict((spec or {}).get("function") or {})
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            definition: dict[str, Any] = {
                "name": name,
                "title": name,
                "description": str(function.get("description") or ""),
                "inputSchema": function.get("parameters")
                if isinstance(function.get("parameters"), dict)
                else {"type": "object", "additionalProperties": False},
            }
            if ui_enabled:
                definition["_meta"] = {"ui": {"resourceUri": self._UI_RESOURCE_URI}}
            definitions.append(definition)
        return definitions

    def _load_ui_resource_markup(self) -> str:
        return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ALDE MCP Operator Console</title>
<style>body{font:14px system-ui,sans-serif;margin:12px;background:#f8fafc;color:#0f172a}
.panel{border:1px solid #cbd5e1;border-radius:12px;padding:12px;background:#fff}
button{margin-right:8px;border:1px solid #94a3b8;border-radius:8px;padding:6px 10px;cursor:pointer}
#log{margin-top:10px;white-space:pre-wrap;max-height:220px;overflow:auto}</style></head>
<body><div class="panel"><button id="prompts">prompts/list</button><button id="tools">tools/list</button>
<pre id="log">Ready.</pre></div><script>
const log=document.getElementById("log");
async function request(method){if(!window.mcp||typeof window.mcp.request!=="function"){log.textContent="Host bridge unavailable (window.mcp.request missing).";return}
try{log.textContent=JSON.stringify(await window.mcp.request({method,params:{}}),null,2)}catch(error){log.textContent=String(error)}}
document.getElementById("prompts").onclick=()=>request("prompts/list");
document.getElementById("tools").onclick=()=>request("tools/list");
</script></body></html>"""

    def load_resources_list_result(self, params: Dict[str, Any]) -> dict[str, Any]:
        if not self._load_ui_extension_requested(params):
            return {"result": {"resources": []}}
        return {"result": {"resources": [{
            "uri": self._UI_RESOURCE_URI,
            "name": "alde-operator-console",
            "title": "ALDE MCP Operator Console",
            "description": "Compact MCP app UI for prompt and tool introspection.",
            "mimeType": self._UI_RESOURCE_MIME_TYPE,
        }]}}

    def load_resources_read_result(self, params: Dict[str, Any]) -> dict[str, Any]:
        if not self._load_ui_extension_requested(params):
            return {"error": "ui_extension_not_enabled"}
        if str(params.get("uri") or "").strip() != self._UI_RESOURCE_URI:
            return {"error": "resource_not_found"}
        return {"result": {"contents": [{
            "uri": self._UI_RESOURCE_URI,
            "mimeType": self._UI_RESOURCE_MIME_TYPE,
            "text": self._load_ui_resource_markup(),
        }]}}

    def load_tools_call_result(self, params: Dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not name:
            return {"error": "Missing tool name"}

        arguments_payload = params.get("arguments") or {}
        if isinstance(arguments_payload, str):
            try:
                arguments_payload = json.loads(arguments_payload)
            except Exception:
                arguments_payload = {}

        try:
            result, route_req = execute_tool(name, arguments_payload, params.get("id"))
            payload: Dict[str, Any] = {"content": self.safe_serialize_object(result)}
            if route_req:
                payload["route_request"] = self.safe_serialize_object(route_req)
            return {"result": payload}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"tool_error: {exc}"}

    def load_prompts_list_result(self, _params: Dict[str, Any]) -> dict[str, Any]:
        prompts = [
            {
                "name": "tracked_prompt_sources_status",
                "description": "Status snapshot of tracked AGENTS/skills/instructs/prompts source files used for strategy propagation.",
                "arguments": [],
            },
            {
                "name": "server_strategy_overview",
                "description": "Server-level strategy overview for propagating prompt and skill guidance via MCP.",
                "arguments": [],
            },
            {
                "name": "server_strategy_for_skill",
                "description": "Expanded prompt fragment strategy for a selected skill profile and optional tool/job selectors.",
                "arguments": [
                    {"name": "skill_profile", "required": False, "description": "Skill profile key, e.g. xworker_core."},
                    {"name": "tool_name", "required": False, "description": "Optional routed tool name."},
                    {"name": "job_name", "required": False, "description": "Optional routed job name."},
                ],
            },
            {
                "name": "server_strategy_ifaai_generalized",
                "description": "Generalized governance strategy profile aligned for international agentic AI operations.",
                "arguments": [
                    {"name": "skill_profile", "required": False, "description": "Skill profile key for propagation context."},
                    {"name": "tool_name", "required": False, "description": "Optional tool selector for operational scoping."},
                    {"name": "job_name", "required": False, "description": "Optional job selector for operational scoping."},
                    {"name": "risk_tier", "required": False, "description": "low|standard|high|critical"},
                    {"name": "compliance_mode", "required": False, "description": "strict|balanced|adaptive"},
                ],
            },
            {
                "name": "agentsdb_mcp_operator",
                "description": "Operational prompt for using the AgentsDB MCP layer for graph, workflow, and relation-view tasks.",
                "arguments": [
                    {"name": "context", "required": False, "description": "Optional task context or user intent."},
                    {"name": "tool_name", "required": False, "description": "Optional AgentsDB-backed tool such as relation_graph_view or workflow_diagram."},
                ],
            },
        ]
        return {"result": {"prompts": prompts}}

    def load_prompts_get_result(self, params: Dict[str, Any]) -> dict[str, Any]:
        prompt_name = str(params.get("name") or "").strip()
        arguments_payload = params.get("arguments") or {}
        if not isinstance(arguments_payload, dict):
            arguments_payload = {}

        prompt_text = ""
        if prompt_name == "tracked_prompt_sources_status":
            prompt_text = self._build_tracked_prompt_sources_text()
        elif prompt_name == "server_strategy_overview":
            prompt_text = self._build_server_strategy_overview_text()
        elif prompt_name == "server_strategy_for_skill":
            prompt_text = self._build_skill_strategy_text(
                skill_profile=str(arguments_payload.get("skill_profile") or ""),
                tool_name=str(arguments_payload.get("tool_name") or ""),
                job_name=str(arguments_payload.get("job_name") or ""),
            )
        elif prompt_name == "server_strategy_ifaai_generalized":
            prompt_text = self._build_ifaai_generalized_strategy_text(
                skill_profile=str(arguments_payload.get("skill_profile") or ""),
                tool_name=str(arguments_payload.get("tool_name") or ""),
                job_name=str(arguments_payload.get("job_name") or ""),
                risk_tier=str(arguments_payload.get("risk_tier") or ""),
                compliance_mode=str(arguments_payload.get("compliance_mode") or ""),
            )
        elif prompt_name == "agentsdb_mcp_operator":
            prompt_text = self._build_agentsdb_mcp_operator_text(
                context=str(arguments_payload.get("context") or ""),
                tool_name=str(arguments_payload.get("tool_name") or ""),
            )
        else:
            return {"error": "prompt_not_found"}

        return {
            "result": {
                "description": "Server prompt payload for MCP strategy propagation.",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": prompt_text,
                        },
                    }
                ],
            }
        }

    @staticmethod
    def _load_jsonrpc_error(*, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _dispatch_jsonrpc_object(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        method = str(payload.get("method") or "").strip()
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        requested_ui_enabled = self._load_ui_extension_requested(params)
        if method == "initialize":
            try:
                result = self.load_initialize_result(params).get("result") or {}
            except ValueError as exc:
                return self._load_jsonrpc_error(
                    request_id=request_id,
                    code=-32602,
                    message=str(exc),
                )
            self._ui_enabled = requested_ui_enabled
            result["supportedVersions"] = list(self._SUPPORTED_PROTOCOL_VERSIONS)
        elif method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": list(self._SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": self._load_server_capabilities_payload(ui_enabled=self._ui_enabled or requested_ui_enabled),
            }
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": self._load_mcp_tool_definitions(ui_enabled=self._ui_enabled or requested_ui_enabled),
            }
        elif method == "resources/list":
            result = self.load_resources_list_result(
                {"_meta": {"io.modelcontextprotocol/extensions": self._UI_EXTENSION_NAME}}
                if self._ui_enabled or requested_ui_enabled
                else params
            ).get("result") or {}
            result["resultType"] = "complete"
        elif method == "resources/read":
            legacy = self.load_resources_read_result(
                {**params, "_meta": {"io.modelcontextprotocol/extensions": self._UI_EXTENSION_NAME}}
                if self._ui_enabled or requested_ui_enabled
                else params
            )
            if "error" in legacy:
                return self._load_jsonrpc_error(
                    request_id=request_id,
                    code=-32001,
                    message=str(legacy["error"]),
                )
            else:
                result = legacy.get("result") or {}
                result["resultType"] = "complete"
        elif method == "tools/call":
            legacy = self.load_tools_call_result(params)
            if "error" in legacy:
                return self._load_jsonrpc_error(
                    request_id=request_id,
                    code=-32602,
                    message=str(legacy["error"]),
                )
            result = legacy.get("result") or {}
        elif method == "prompts/list":
            result = self.load_prompts_list_result(params).get("result") or {}
            result["resultType"] = "complete"
        elif method == "prompts/get":
            legacy = self.load_prompts_get_result(params)
            if "error" in legacy:
                return self._load_jsonrpc_error(
                    request_id=request_id,
                    code=-32602,
                    message=str(legacy["error"]),
                )
            result = legacy.get("result") or {}
        else:
            return self._load_jsonrpc_error(
                request_id=request_id,
                code=-32601,
                message=f"Method not found: {method}",
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def dispatch_object(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"error": "invalid_json"}

        if payload.get("jsonrpc") == "2.0":
            return self._dispatch_jsonrpc_object(payload)

        method = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}


        handlers = {
            "initialize": self.load_initialize_result,
            "resources/list": self.load_resources_list_result,
            "resources/read": self.load_resources_read_result,
            "tools/list": self.load_tools_list_result,
            "tools/call": self.load_tools_call_result,
            "prompts/list": self.load_prompts_list_result,
            "prompts/get": self.load_prompts_get_result,
        }

        handler = handlers.get(str(method or ""))
        if not handler:
            return {"error": "method_not_implemented"}
        return handler(params)


MCP_REQUEST_SERVICE = McpRequestService()


def _response(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            _response({"error": "invalid_json"})
            continue

        _response(MCP_REQUEST_SERVICE.dispatch_object(payload))


if __name__ == "__main__":
    main()