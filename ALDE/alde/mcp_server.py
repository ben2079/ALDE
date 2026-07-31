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

    def load_initialize_result(self, _params: Dict[str, Any]) -> dict[str, Any]:
        return {
            "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "alde-local-mcp"},
                "capabilities": {
                    "tools": {},
                    "prompts": {},
                },
            }
        }

    def load_tools_list_result(self, _params: Dict[str, Any]) -> dict[str, Any]:
        tool_registry = get_tool_registry()
        tools = [self.safe_serialize_object(spec) for _name, spec in tool_registry.items()]
        return {"result": {"tools": tools}}

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

    def dispatch_object(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"error": "invalid_json"}

        method = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}


        handlers = {
            "initialize": self.load_initialize_result,
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