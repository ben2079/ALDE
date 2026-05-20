from __future__ import annotations

# Maintainer contact: see repository README.

from pathlib import Path
from importlib.metadata import metadata
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import sys
import json
import os
import atexit
import hashlib
import time
from types import SimpleNamespace
from typing import Any
from datetime import datetime
from pyexpat import model


_THIS_MODULE = sys.modules.get(__name__)
if _THIS_MODULE is not None:
    if __name__.startswith("ALDE_Projekt.ALDE.alde"):
        sys.modules.setdefault("alde.agents_factory", _THIS_MODULE)
    elif __name__.startswith("alde."):
        sys.modules.setdefault("ALDE_Projekt.ALDE.alde.agents_factory", _THIS_MODULE)

try:
    from .agents_config import (
        build_agent_handoff,
        get_agent_config,
        get_agent_workflow_config,
        get_default_job_name,
        get_handoff_route_contract,
        get_job_config,
        get_specialized_system_prompt,
        get_workflow_config,
        normalize_action_request_name,
        normalize_agent_label,
        normalize_tool_name,
        prepare_incoming_handoff,
        validate_handoff_for_target
    )  # type: ignore
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from alde.agents_config import (
            build_agent_handoff,
            get_agent_config,
            get_agent_workflow_config,
            get_default_job_name,
            get_handoff_route_contract,
            get_job_config,
            get_specialized_system_prompt,
            get_workflow_config,
            normalize_action_request_name,
            normalize_agent_label,
            normalize_tool_name,
            prepare_incoming_handoff,
            validate_handoff_for_target
            )  # type: ignore
    else:
        raise

try:
    from .chat_runtime import ChatCom, ChatCompletion
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        import os
        import sys

        _pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _pkg_parent not in sys.path:
            sys.path.insert(0, _pkg_parent)
        from alde.chat_runtime import ChatCom, ChatCompletion
    else:
        raise
try:
    from .agents_tools import (  # type: ignore
        DOCUMENT_OBJECT_SERVICE,
        DOCUMENT_REPOSITORY,
        REQUEST_OBJECT_RESOLUTION_SERVICE,
        UNIFIED_TOOLS,
        function_dispatcher,
        get_agent_tools,
        get_tool_spec,
        memorydb,
        md_to_pdf,
        vectordb,
        write_document,
    )
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from alde.agents_tools import (  # type: ignore
            DOCUMENT_OBJECT_SERVICE,
            DOCUMENT_REPOSITORY,
            REQUEST_OBJECT_RESOLUTION_SERVICE,
            UNIFIED_TOOLS,
            function_dispatcher,
            get_agent_tools,
            get_tool_spec,
            memorydb,
            md_to_pdf,
            vectordb,
            write_document,
        )
    else:
        raise
if __name__ == '__main__':
    _script_dir = Path(__file__).parent
    _parent_dir = _script_dir.parent
    if str(_parent_dir) not in sys.path:

        sys.path.insert(0, str(_parent_dir))
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))
try:
    from .get_path import GetPath  # type: ignore
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from alde.get_path import GetPath  # type: ignore
    else:
        raise

#try:
    # ChatComE is the agent-capable chat client that accepts (_messages, tools)
    # and returns a full OpenAI response object via ._response().
  #  from .chat_completion import ChatComE as ChatCom, ChatHistory
#except ImportError:  # allow running directly from the repository root


_MAX_TOOL_DEPTH = 50
_TOOL_CACHE: dict[str, str] = {}
_WORKFLOW_SESSION_CACHE: dict[str, dict[str, Any]] = {}
_MODEL = "gpt-4.1-mini-2025-04-14"
model = _MODEL


def _default_cover_letter_output_dir() -> str:
    try:
        base_dir = GetPath()._parent(parg=f"{__file__}")
        if isinstance(base_dir, str) and base_dir.strip():
            return os.path.abspath(
                os.path.join(base_dir, "AppData", "VSM_4_Data", "cover_letters")
            )
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Cover_letters")


# NOTE: This must be a real dict at runtime; tool-call dispatch reads from it.
try:
    from . import agents_registry as _agents_registry  # type: ignore
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from alde import agents_registry as _agents_registry  # type: ignore
    else:
        raise
AGENTS_REGISTRY: dict[str, dict] = getattr(_agents_registry, "AGENTS_REGISTRY", {}) or {}

# Defer importing ChatHistory to runtime to avoid circular imports
_initial_history_length = 0

# Lazy accessor for the ChatHistory singleton to prevent circular imports.
_history_instance: Any | None = None


class AgentRuntimeConfigService:
    def load_object_config(self, agent_name: str | None) -> dict[str, Any]:
        if not agent_name:
            return {}

        agent_label = normalize_agent_label(agent_name)
        config = deepcopy(get_agent_config(agent_label) or AGENTS_REGISTRY.get(agent_label) or {})
        raw_tools = list(config.get("tools") or [])
        routing_policy = dict(config.get("routing_policy") or {})
        can_route = bool(routing_policy.get("can_route"))

        if not can_route:
            raw_tools = [
                tool_name
                for tool_name in raw_tools
                if not (isinstance(tool_name, str) and normalize_tool_name(tool_name) == "route_to_agent")
            ]

        config["agent_label"] = agent_label
        config["tools"] = raw_tools
        config["routing_policy"] = routing_policy
        return config

    def load_object_tools(self, agent_name: str | None) -> list[dict[str, Any]]:
        config = self.load_object_config(agent_name)
        return get_agent_tools(config.get("tools") or [])

    def load_object_attachment_entries(
        self,
        *,
        agent_name: str | None,
        job_name: str | None = None,
        tool_name: str | None = None,
        scope_key: str | None = None,
        thread_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not agent_name:
            return []
        resolved_memory_slot = AGENT_MEMORY_SERVICE.load_amemo_slot(
            job_name=job_name,
            tool_name=tool_name,
        )
        resolved_scope_key = AGENT_MEMORY_SERVICE.load_session_scope_key(
            scope_key=scope_key,
            thread_id=thread_id,
        )
        return OBJECT_MEMORY_ATTACHMENT_SERVICE.load_object_attachment_entries(
            agent_label=agent_name,
            memory_slot=resolved_memory_slot,
            scope_key=resolved_scope_key,
        )

    def load_object_attachment_documents(
        self,
        *,
        agent_name: str | None,
        job_name: str | None = None,
        tool_name: str | None = None,
        scope_key: str | None = None,
        thread_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not agent_name:
            return []
        resolved_memory_slot = AGENT_MEMORY_SERVICE.load_amemo_slot(
            job_name=job_name,
            tool_name=tool_name,
        )
        resolved_scope_key = AGENT_MEMORY_SERVICE.load_session_scope_key(
            scope_key=scope_key,
            thread_id=thread_id,
        )
        return OBJECT_MEMORY_ATTACHMENT_SERVICE.load_object_attachment_documents(
            agent_label=agent_name,
            memory_slot=resolved_memory_slot,
            scope_key=resolved_scope_key,
        )

    def load_object_attachment_message(
        self,
        *,
        agent_name: str | None,
        job_name: str | None = None,
        tool_name: str | None = None,
        scope_key: str | None = None,
        thread_id: int | None = None,
    ) -> dict[str, str] | None:
        if not agent_name:
            return None
        resolved_memory_slot = AGENT_MEMORY_SERVICE.load_amemo_slot(
            job_name=job_name,
            tool_name=tool_name,
        )
        resolved_scope_key = AGENT_MEMORY_SERVICE.load_session_scope_key(
            scope_key=scope_key,
            thread_id=thread_id,
        )
        return OBJECT_MEMORY_ATTACHMENT_SERVICE.load_attachment_context_message(
            agent_label=agent_name,
            memory_slot=resolved_memory_slot,
            scope_key=resolved_scope_key,
        )


AGENT_RUNTIME_CONFIG_SERVICE = AgentRuntimeConfigService()


def _get_runtime_agent_config(agent_name: str | None) -> dict[str, Any]:
    return AGENT_RUNTIME_CONFIG_SERVICE.load_object_config(agent_name)


def get_agent_runtime_tools(agent_name: str | None) -> list[dict[str, Any]]:
    return AGENT_RUNTIME_CONFIG_SERVICE.load_object_tools(agent_name)


class AgentRuntimePolicyObject:
    def __init__(self, agent_name: str | None, config: dict[str, Any] | None) -> None:
        self.agent_name = agent_name
        self.config = dict(config or {})

    def load_can_route(self) -> bool:
        return bool((self.config.get("routing_policy") or {}).get("can_route"))

    def load_runtime_metadata(self) -> dict[str, Any]:
        if not self.config:
            return {}
        return {
            "agent_label": self.config.get("agent_label") or normalize_agent_label(self.agent_name or ""),
            "canonical_name": self.config.get("canonical_name") or "",
            "role": self.config.get("role") or "worker",
            "skill_profile": self.config.get("skill_profile") or "",
            "instance_policy": self.config.get("instance_policy") or "ephemeral",
            "routing_policy": deepcopy(self.config.get("routing_policy") or {}),
            "history_policy": deepcopy(self.config.get("history_policy") or {}),
        }

    def load_history_policy(self) -> dict[str, Any]:
        policy = dict(self.config.get("history_policy") or {})
        followup_depth = policy.get("followup_history_depth", 15)
        routed_depth = policy.get("routed_history_depth", 0)
        include_routed_history = bool(policy.get("include_routed_history", False))

        try:
            followup_depth = max(0, int(followup_depth))
        except Exception:
            followup_depth = 15
        try:
            routed_depth = max(0, int(routed_depth))
        except Exception:
            routed_depth = 0

        return {
            "followup_history_depth": followup_depth,
            "include_routed_history": include_routed_history,
            "routed_history_depth": routed_depth,
        }


class AgentRuntimePolicyService:
    def load_object_policy(self, agent_name: str | None) -> AgentRuntimePolicyObject:
        return AgentRuntimePolicyObject(agent_name, _get_runtime_agent_config(agent_name))

    def load_allowed_tool_names(self, agent_name: str | None) -> set[str]:
        allowed: set[str] = set()
        for tool_def in get_agent_runtime_tools(agent_name):
            function_def = tool_def.get("function") if isinstance(tool_def, dict) else None
            if not isinstance(function_def, dict):
                continue
            tool_name = function_def.get("name")
            if isinstance(tool_name, str) and tool_name:
                allowed.add(normalize_tool_name(tool_name))
        return allowed

    def load_tool_allowed(self, agent_name: str | None, tool_name: str) -> bool:
        normalized_name = normalize_tool_name(tool_name)
        if normalized_name == "vectordb_tool":
            normalized_name = "vectordb"
        if not agent_name:
            return True
        return normalized_name in self.load_allowed_tool_names(agent_name)

    def load_can_route(self, agent_name: str | None) -> bool:
        return self.load_object_policy(agent_name).load_can_route()

    def load_runtime_metadata(self, agent_name: str | None) -> dict[str, Any]:
        return self.load_object_policy(agent_name).load_runtime_metadata()

    def load_history_policy(self, agent_name: str | None) -> dict[str, Any]:
        return self.load_object_policy(agent_name).load_history_policy()


AGENT_RUNTIME_POLICY_SERVICE = AgentRuntimePolicyService()


def _get_allowed_tool_names(agent_name: str | None) -> set[str]:
    return AGENT_RUNTIME_POLICY_SERVICE.load_allowed_tool_names(agent_name)


def _is_tool_allowed_for_agent(agent_name: str | None, tool_name: str) -> bool:
    return AGENT_RUNTIME_POLICY_SERVICE.load_tool_allowed(agent_name, tool_name)


def _agent_can_route(agent_name: str | None) -> bool:
    return AGENT_RUNTIME_POLICY_SERVICE.load_can_route(agent_name)


def _agent_runtime_metadata(agent_name: str | None) -> dict[str, Any]:
    return AGENT_RUNTIME_POLICY_SERVICE.load_runtime_metadata(agent_name)


def _agent_history_policy(agent_name: str | None) -> dict[str, Any]:
    return AGENT_RUNTIME_POLICY_SERVICE.load_history_policy(agent_name)


class WorkflowSnapshotService:
    def load_actor(self, workflow_session: dict[str, Any], current_state: str) -> dict[str, Any]:
        workflow_name = str(workflow_session.get("workflow_name") or "").strip()
        workflow_config = get_workflow_config(workflow_name) if workflow_name else {}
        if not workflow_config:
            workflow_config = get_agent_workflow_config(str(workflow_session.get("agent_label") or "")) or {}
        state_config = ((workflow_config.get("states") or {}).get(current_state) or {}) if current_state else {}
        actor_config = state_config.get("actor") if isinstance(state_config.get("actor"), dict) else {}
        return {
            "kind": actor_config.get("kind"),
            "name": actor_config.get("name"),
        }

    def load_event_data(
        self,
        workflow_session: dict[str, Any],
        *,
        event_kind: str | None = None,
        event_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_event = deepcopy(workflow_session.get("last_event") or {})
        effective_event_payload = deepcopy(payload if payload is not None else (last_event.get("payload") or {}))
        nested_event_payload = effective_event_payload.get("payload") if isinstance(effective_event_payload.get("payload"), dict) else {}
        effective_event_kind = event_kind or last_event.get("kind")
        effective_event_name = event_name or last_event.get("name")
        effective_tool_name = str(effective_event_payload.get("tool_name") or effective_event_name or "").strip() or None
        return {
            "last_event": last_event,
            "payload": effective_event_payload,
            "nested_payload": nested_event_payload,
            "kind": effective_event_kind,
            "name": effective_event_name,
            "tool_name": effective_tool_name,
        }

    def load_snapshot_actor(
        self,
        actor: dict[str, Any],
        *,
        event_kind: str | None,
        event_name: str | None,
        tool_name: str | None,
    ) -> dict[str, Any]:
        snapshot_actor_kind = actor.get("kind")
        snapshot_actor_name = actor.get("name")
        if event_kind == "state" and event_name in {"tool_complete", "tool_failed"} and tool_name:
            snapshot_actor_kind = "tool"
            snapshot_actor_name = tool_name
        elif tool_name and snapshot_actor_kind == "state" and str(snapshot_actor_name or "") in {"workflow_complete", "workflow_failed"}:
            snapshot_actor_kind = "tool"
            snapshot_actor_name = tool_name
        return {
            "kind": snapshot_actor_kind,
            "name": snapshot_actor_name,
        }

    def build_snapshot(
        self,
        workflow_session: dict[str, Any],
        *,
        phase: str,
        event_kind: str | None = None,
        event_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow_name = str(workflow_session.get("workflow_name") or "")
        current_state = str(workflow_session.get("current_state") or "")
        actor = self.load_actor(workflow_session, current_state)
        event_data = self.load_event_data(
            workflow_session,
            event_kind=event_kind,
            event_name=event_name,
            payload=payload,
        )
        snapshot_actor = self.load_snapshot_actor(
            actor,
            event_kind=event_data.get("kind"),
            event_name=event_data.get("name"),
            tool_name=event_data.get("tool_name"),
        )
        return {
            "last_event": event_data.get("last_event") or {},
            "snapshot": {
                "phase": phase,
                "workflow_name": workflow_name,
                "agent_label": workflow_session.get("agent_label"),
                "current_state": current_state,
                "terminal": bool(workflow_session.get("terminal")),
                "actor": snapshot_actor,
                "event": {
                    "kind": event_data.get("kind"),
                    "name": event_data.get("name"),
                    "tool_name": event_data.get("tool_name"),
                    "action": event_data["payload"].get("action") or event_data["nested_payload"].get("action"),
                    "target_agent": event_data["payload"].get("target_agent") or event_data["nested_payload"].get("target_agent"),
                    "correlation_id": event_data["payload"].get("correlation_id") or event_data["nested_payload"].get("correlation_id"),
                },
            },
        }

    def build_workflow_data(
        self,
        workflow_session: dict[str, Any] | None,
        *,
        phase: str,
        event_kind: str | None = None,
        event_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return WORKFLOW_STATUS_PROJECTION_SERVICE.build_object_data(
            workflow_session,
            phase=phase,
            event_kind=event_kind,
            event_name=event_name,
            payload=payload,
        )


WORKFLOW_SNAPSHOT_SERVICE = WorkflowSnapshotService()


class WorkflowStatusProjectionService:
    def build_object_data(
        self,
        workflow_session: dict[str, Any] | None,
        *,
        phase: str,
        event_kind: str | None = None,
        event_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not workflow_session:
            return None

        workflow_name = str(workflow_session.get("workflow_name") or "")
        current_state = str(workflow_session.get("current_state") or "")
        snapshot_data = WORKFLOW_SNAPSHOT_SERVICE.build_snapshot(
            workflow_session,
            phase=phase,
            event_kind=event_kind,
            event_name=event_name,
            payload=payload,
        )
        workflow_data = {
            "phase": phase,
            "workflow_name": workflow_name,
            "agent_label": workflow_session.get("agent_label"),
            "scope_key": workflow_session.get("scope_key"),
            "runtime": deepcopy(workflow_session.get("runtime") or {}),
            "current_state": current_state,
            "terminal": bool(workflow_session.get("terminal")),
            "history": list(workflow_session.get("history") or []),
            "retry": deepcopy(workflow_session.get("retry") or {}),
            "last_event": snapshot_data.get("last_event") or {},
            "last_transition": deepcopy(workflow_session.get("last_transition") or {}),
            "snapshot": snapshot_data.get("snapshot") or {},
        }
        if event_kind or event_name or payload is not None:
            workflow_data["event"] = {
                "kind": event_kind,
                "name": event_name,
                "payload": deepcopy(payload or {}),
            }
        return {"workflow": workflow_data}


WORKFLOW_STATUS_PROJECTION_SERVICE = WorkflowStatusProjectionService()


class WorkflowRetryStatusService:
    FAILURE_EVENTS = {"tool_failed", "model_failed", "routed_agent_failed"}
    COMPLETION_EVENTS = {"followup_complete", "routed_agent_complete"}

    def load_retry_state(self, retry_status: dict[str, Any], retry_policy: dict[str, Any]) -> dict[str, Any]:
        max_attempts = int(retry_status.get("max_attempts") or retry_policy.get("max_attempts") or 0)
        backoff_seconds = list(retry_status.get("backoff_seconds") or retry_policy.get("backoff_seconds") or [])
        attempt_count = int(retry_status.get("attempt_count") or 0)
        retry_history = list(retry_status.get("history") or [])
        return {
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "backoff_seconds": backoff_seconds,
            "history": retry_history,
            "last_failure": retry_status.get("last_failure"),
            "next_delay_seconds": int(retry_status.get("next_delay_seconds") or 0),
            "exhausted": bool(retry_status.get("exhausted")),
        }

    def build_history_entry(
        self,
        *,
        attempt_count: int,
        event_name: str,
        next_state: str,
        delay_seconds: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "attempt": attempt_count,
            "event": event_name,
            "state": next_state,
            "delay_seconds": delay_seconds,
            "payload": deepcopy(payload),
        }

    def build_status(
        self,
        retry_state: dict[str, Any],
        *,
        next_delay_seconds: int,
        exhausted: bool,
        last_failure: Any,
    ) -> dict[str, Any]:
        attempt_count = int(retry_state.get("attempt_count") or 0)
        max_attempts = int(retry_state.get("max_attempts") or 0)
        return {
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "remaining_attempts": max(max_attempts - attempt_count, 0) if max_attempts else 0,
            "next_delay_seconds": next_delay_seconds,
            "backoff_seconds": list(retry_state.get("backoff_seconds") or []),
            "exhausted": exhausted,
            "last_failure": deepcopy(last_failure),
            "history": list(retry_state.get("history") or []),
        }

    def update_status(
        self,
        retry_status: dict[str, Any],
        retry_policy: dict[str, Any],
        *,
        event_name: str,
        payload: dict[str, Any],
        next_state: str,
    ) -> dict[str, Any]:
        retry_state = self.load_retry_state(retry_status, retry_policy)

        if event_name in self.FAILURE_EVENTS:
            retry_state["attempt_count"] = int(retry_state.get("attempt_count") or 0) + 1
            backoff_seconds = list(retry_state.get("backoff_seconds") or [])
            delay_index = min(max(int(retry_state["attempt_count"]) - 1, 0), max(len(backoff_seconds) - 1, 0)) if backoff_seconds else 0
            next_delay_seconds = backoff_seconds[delay_index] if backoff_seconds else 0
            exhausted = bool(retry_state.get("max_attempts") and int(retry_state["attempt_count"]) >= int(retry_state["max_attempts"]))
            retry_state["history"] = list(retry_state.get("history") or []) + [
                self.build_history_entry(
                    attempt_count=int(retry_state["attempt_count"]),
                    event_name=event_name,
                    next_state=next_state,
                    delay_seconds=next_delay_seconds,
                    payload=payload,
                )
            ]
            return self.build_status(
                retry_state,
                next_delay_seconds=next_delay_seconds,
                exhausted=exhausted,
                last_failure=payload,
            )

        if event_name == "retry_requested":
            retry_state["history"] = list(retry_state.get("history") or []) + [
                self.build_history_entry(
                    attempt_count=int(retry_state.get("attempt_count") or 0),
                    event_name=event_name,
                    next_state=next_state,
                    delay_seconds=0,
                    payload=payload,
                )
            ]
            return self.build_status(
                retry_state,
                next_delay_seconds=0,
                exhausted=bool(retry_state.get("max_attempts") and int(retry_state.get("attempt_count") or 0) >= int(retry_state.get("max_attempts") or 0)),
                last_failure=retry_state.get("last_failure"),
            )

        if event_name in self.COMPLETION_EVENTS:
            return self.build_status(
                retry_state,
                next_delay_seconds=0,
                exhausted=False,
                last_failure=retry_state.get("last_failure"),
            )

        return self.build_status(
            retry_state,
            next_delay_seconds=int(retry_state.get("next_delay_seconds") or 0),
            exhausted=bool(retry_state.get("exhausted")),
            last_failure=retry_state.get("last_failure"),
        )


WORKFLOW_RETRY_STATUS_SERVICE = WorkflowRetryStatusService()


class WorkflowContextService:
    def load_history(self) -> Any:
        return HISTORY_ACCESS_SERVICE.load_history()

    def load_current_thread_id(self) -> int | None:
        try:
            return getattr(self.load_history(), "_thread_iD", None)
        except Exception:
            return None

    def load_runtime_metadata(self, agent_name: str | None) -> dict[str, Any]:
        return AGENT_RUNTIME_POLICY_SERVICE.load_runtime_metadata(agent_name)

    def build_scope_key(
        self,
        agent_name: str | None,
        workflow_name: str,
        *,
        thread_id: int | None = None,
    ) -> str | None:
        runtime = self.load_runtime_metadata(agent_name)
        instance_policy = str(runtime.get("instance_policy") or "ephemeral")
        if instance_policy == "ephemeral":
            return None

        thread_token = thread_id if thread_id is not None else self.load_current_thread_id()
        agent_label = str(runtime.get("agent_label") or normalize_agent_label(agent_name or ""))

        if instance_policy == "session_scoped":
            return f"session:{thread_token}:{agent_label}"
        if instance_policy == "workflow_scoped":
            return f"workflow:{thread_token}:{workflow_name or agent_label}:{agent_label}"
        if instance_policy == "service_scoped":
            return f"service:{workflow_name or agent_label}:{agent_label}"
        return None


WORKFLOW_CONTEXT_SERVICE = WorkflowContextService()


class WorkflowSessionService:
    def load_current_thread_id(self) -> int | None:
        return WORKFLOW_CONTEXT_SERVICE.load_current_thread_id()

    def build_scope_key(
        self,
        agent_name: str | None,
        workflow_name: str,
        *,
        thread_id: int | None = None,
    ) -> str | None:
        return WORKFLOW_CONTEXT_SERVICE.build_scope_key(
            agent_name,
            workflow_name,
            thread_id=thread_id,
        )

    def persist_session(
        self,
        workflow_session: dict[str, Any] | None,
        *,
        thread_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not workflow_session:
            return None

        scope_key = str(workflow_session.get("scope_key") or "")
        if scope_key:
            _WORKFLOW_SESSION_CACHE[scope_key] = deepcopy(workflow_session)
        return workflow_session

    def normalize_retry_policy(self, workflow_config: dict[str, Any]) -> dict[str, Any]:
        policy = workflow_config.get("retry_policy") or {}
        backoff_seconds = [int(value) for value in (policy.get("backoff_seconds") or [])]
        max_attempts = int(policy.get("max_attempts") or len(backoff_seconds) or 0)
        return {
            "max_attempts": max_attempts,
            "backoff_seconds": backoff_seconds,
        }

    def build_history_data(
        self,
        workflow_session: dict[str, Any] | None,
        *,
        phase: str,
        event_kind: str | None = None,
        event_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return WORKFLOW_STATUS_PROJECTION_SERVICE.build_object_data(
            workflow_session,
            phase=phase,
            event_kind=event_kind,
            event_name=event_name,
            payload=payload,
        )

    def list_history_entries(
        self,
        *,
        agent_label: str | None = None,
        workflow_name: str | None = None,
        thread_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return WORKFLOW_HISTORY_QUERY_SERVICE.list_object_entries(
            agent_label=agent_label,
            workflow_name=workflow_name,
            thread_id=thread_id,
            limit=limit,
        )

    def load_latest_status(
        self,
        *,
        agent_label: str | None = None,
        workflow_name: str | None = None,
        thread_id: int | None = None,
    ) -> dict[str, Any] | None:
        return WORKFLOW_HISTORY_QUERY_SERVICE.load_latest_object_status(
            agent_label=agent_label,
            workflow_name=workflow_name,
            thread_id=thread_id,
        )

    def create_session(
        self,
        agent_name: str | None,
        *,
        thread_id: int | None = None,
        routing_request: dict[str, Any] | None = None,
        workflow_name: str | None = None,
    ) -> dict[str, Any] | None:
        if not agent_name:
            return None

        workflow_config: dict[str, Any] = {}
        resolved_workflow_name = str(workflow_name or "").strip()

        if not resolved_workflow_name and routing_request is not None:
            resolved_workflow_name = AGENT_EXECUTION_SELECTION_SERVICE.load_workflow_name(
                normalize_agent_label(agent_name),
                routing_request,
            )

        if resolved_workflow_name:
            workflow_config = get_workflow_config(resolved_workflow_name)
        if not workflow_config:
            workflow_config = get_agent_workflow_config(agent_name)
        if not workflow_config:
            return None

        states = workflow_config.get("states") or {}
        entry_state = str(workflow_config.get("entry_state") or "")
        if not entry_state or entry_state not in states:
            return None

        retry_policy = self.normalize_retry_policy(workflow_config)
        runtime_metadata = WORKFLOW_CONTEXT_SERVICE.load_runtime_metadata(agent_name)
        workflow_name = str(workflow_config.get("name") or resolved_workflow_name or "").strip()
        scope_key = self.build_scope_key(agent_name, workflow_name, thread_id=thread_id)
        cached_session = _WORKFLOW_SESSION_CACHE.get(scope_key) if scope_key else None
        if cached_session and not bool(cached_session.get("terminal")):
            return deepcopy(cached_session)

        session = {
            "workflow_name": workflow_name,
            "agent_label": normalize_agent_label(agent_name),
            "runtime": runtime_metadata,
            "scope_key": scope_key,
            "current_state": entry_state,
            "terminal": bool((states.get(entry_state) or {}).get("terminal", False)),
            "history": [entry_state],
            "retry": {
                "attempt_count": 0,
                "max_attempts": retry_policy.get("max_attempts", 0),
                "remaining_attempts": retry_policy.get("max_attempts", 0),
                "next_delay_seconds": 0,
                "backoff_seconds": list(retry_policy.get("backoff_seconds") or []),
                "exhausted": False,
                "last_failure": None,
                "history": [],
            },
        }
        return self.persist_session(session, thread_id=thread_id)

    def update_retry_status(
        self,
        workflow_session: dict[str, Any],
        workflow_config: dict[str, Any],
        *,
        event_name: str,
        payload: dict[str, Any],
        next_state: str,
    ) -> dict[str, Any]:
        return WORKFLOW_RETRY_STATUS_SERVICE.update_status(
            dict(workflow_session.get("retry") or {}),
            self.normalize_retry_policy(workflow_config),
            event_name=event_name,
            payload=payload,
            next_state=next_state,
        )

    def advance_session(
        self,
        workflow_session: dict[str, Any] | None,
        *,
        event_kind: str,
        event_name: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not workflow_session:
            return None

        workflow_name = str(workflow_session.get("workflow_name") or "").strip()
        workflow_config = get_workflow_config(workflow_name) if workflow_name else {}
        if not workflow_config:
            workflow_config = get_agent_workflow_config(str(workflow_session.get("agent_label") or ""))
        if not workflow_config:
            return workflow_session

        current_state = str(workflow_session.get("current_state") or "")
        payload = payload or {}
        transition = WORKFLOW_TRANSITION_SERVICE.load_matching_transition(
            workflow_config,
            current_state=current_state,
            event_kind=event_kind,
            event_name=event_name,
            payload=payload,
        )
        if transition:
            next_state = str(transition.get("to") or "")
            if not next_state:
                return workflow_session
            updated = WORKFLOW_TRANSITION_SERVICE.build_updated_session(
                workflow_session,
                workflow_config,
                transition=transition,
                event_kind=event_kind,
                event_name=event_name,
                payload=payload,
                retry_status=self.update_retry_status(
                    workflow_session,
                    workflow_config,
                    event_name=event_name,
                    payload=payload,
                    next_state=next_state,
                ),
            )
            if updated is not None:
                return self.persist_session(updated)

        return self.persist_session(workflow_session)


WORKFLOW_SESSION_SERVICE = WorkflowSessionService()


class WorkflowHistoryQueryService:
    def list_object_entries(
        self,
        *,
        agent_label: str | None = None,
        workflow_name: str | None = None,
        thread_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        history = WORKFLOW_CONTEXT_SERVICE.load_history()
        items: list[dict[str, Any]] = []
        for entry in reversed(list(history._history_ or [])):
            if not isinstance(entry, dict):
                continue
            data = entry.get("data") or {}
            workflow = data.get("workflow") if isinstance(data, dict) else None
            if not isinstance(workflow, dict):
                continue
            if agent_label and workflow.get("agent_label") != normalize_agent_label(agent_label):
                continue
            if workflow_name and workflow.get("workflow_name") != workflow_name:
                continue
            if thread_id is not None and entry.get("thread-id") != thread_id:
                continue
            items.append(
                {
                    "message_id": entry.get("message-id"),
                    "role": entry.get("role"),
                    "assistant_name": entry.get("assistant-name"),
                    "thread_id": entry.get("thread-id"),
                    "thread_name": entry.get("thread-name"),
                    "time": entry.get("time"),
                    "workflow": deepcopy(workflow),
                }
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    def load_latest_object_status(
        self,
        *,
        agent_label: str | None = None,
        workflow_name: str | None = None,
        thread_id: int | None = None,
    ) -> dict[str, Any] | None:
        entries = self.list_object_entries(
            agent_label=agent_label,
            workflow_name=workflow_name,
            thread_id=thread_id,
            limit=1,
        )
        return entries[0] if entries else None


WORKFLOW_HISTORY_QUERY_SERVICE = WorkflowHistoryQueryService()


class WorkflowHistoryLogService:
    def log_depth_warning(self, history: Any, warning: str) -> None:
        history._log(_role='assistant', _content=warning,
                     _name='dispatcher_agent', _thread_name='chat', _obj='tool_call')

    def log_tool_call_start(self, history: Any, *, agent_msg: Any, agent_label: str, workflow_session: dict[str, Any] | None) -> None:
        history._log(
            _role='assistant', _content=agent_msg.content,
            _name=agent_label, _thread_name='chat', _obj='tool_call',
            _tool_calls=agent_msg.tool_calls,
            _data=_workflow_history_data(workflow_session, phase='tool_call_start')
        )

    def log_tool_result(
        self,
        history: Any,
        *,
        tool_content: str,
        agent_label: str,
        tool_call_id: str | None,
        tool_name: str,
        workflow_session: dict[str, Any] | None,
        event_kind: str,
        event_name: str,
        payload: dict[str, Any] | None,
        tool_response_required: bool = True,
    ) -> None:
        history._log(
            _role='tool',
            _content=tool_content,
            _obj='tool_call',
            _thread_name='chat',
            _name=agent_label,
            _tool_call_id=tool_call_id,
            _name_tool=tool_name,
            _tool_response_required=tool_response_required,
            _data=_workflow_history_data(
                workflow_session,
                phase='tool_result',
                event_kind=event_kind,
                event_name=event_name,
                payload=payload,
            )
        )

    def log_model_failure(
        self,
        history: Any,
        *,
        err: str,
        agent_label: str,
        workflow_session: dict[str, Any] | None,
    ) -> None:
        history._log(_role='assistant', _content=err,
                     _name=agent_label or '_xplaner_xrouter',
                     _thread_name='tool_call', _obj='chat',
                     _data=_workflow_history_data(
                         workflow_session,
                         phase='model_failure',
                         event_kind='state',
                         event_name='model_failed',
                         payload={'error': str(err.removeprefix('Follow-up model call failed: ').strip() or err)},
                     ))

    def log_assistant_response(
        self,
        history: Any,
        *,
        text: str,
        response_agent_label: str,
        workflow_session: dict[str, Any] | None,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        history._log(_role='assistant', _content=text,
                     _name=response_agent_label, _thread_name='tool_call', _obj='chat',
                     _data=_workflow_history_data(
                         workflow_session,
                         phase='assistant_response',
                         event_kind='state',
                         event_name=event_name,
                         payload=payload,
                     ))


WORKFLOW_HISTORY_LOG_SERVICE = WorkflowHistoryLogService()


# AgentMemoryService has been migrated to agents_db (module-context alignment).
# Re-exported here to preserve the existing public API.
try:
    from .agents_db import (  # type: ignore
        ObjectMemoryAttachmentService,
        AgentMemoryAttachmentService,
        AgentMemoryService,
        OBJECT_MEMORY_ATTACHMENT_SERVICE,
        AGENT_MEMORY_ATTACHMENT_SERVICE,
        AGENT_MEMORY_SERVICE,
    )
except ImportError:
    from alde.agents_db import (  # type: ignore
        ObjectMemoryAttachmentService,
        AgentMemoryAttachmentService,
        AgentMemoryService,
        OBJECT_MEMORY_ATTACHMENT_SERVICE,
        AGENT_MEMORY_ATTACHMENT_SERVICE,
        AGENT_MEMORY_SERVICE,
    )


def _current_thread_id() -> int | None:
    return WORKFLOW_SESSION_SERVICE.load_current_thread_id()


def _workflow_session_scope_key(
    agent_name: str | None,
    workflow_name: str,
    *,
    thread_id: int | None = None,
) -> str | None:
    return WORKFLOW_SESSION_SERVICE.build_scope_key(agent_name, workflow_name, thread_id=thread_id)


def _persist_workflow_session(
    workflow_session: dict[str, Any] | None,
    *,
    thread_id: int | None = None,
) -> dict[str, Any] | None:
    return WORKFLOW_SESSION_SERVICE.persist_session(workflow_session, thread_id=thread_id)


def _normalize_retry_policy(workflow_config: dict[str, Any]) -> dict[str, Any]:
    return WORKFLOW_SESSION_SERVICE.normalize_retry_policy(workflow_config)


def _workflow_history_data(
    workflow_session: dict[str, Any] | None,
    *,
    phase: str,
    event_kind: str | None = None,
    event_name: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return WORKFLOW_SESSION_SERVICE.build_history_data(
        workflow_session,
        phase=phase,
        event_kind=event_kind,
        event_name=event_name,
        payload=payload,
    )


def get_workflow_history_entries(
    *,
    agent_label: str | None = None,
    workflow_name: str | None = None,
    thread_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return WORKFLOW_HISTORY_QUERY_SERVICE.list_object_entries(
        agent_label=agent_label,
        workflow_name=workflow_name,
        thread_id=thread_id,
        limit=limit,
    )


def get_latest_workflow_status(
    *,
    agent_label: str | None = None,
    workflow_name: str | None = None,
    thread_id: int | None = None,
) -> dict[str, Any] | None:
    return WORKFLOW_HISTORY_QUERY_SERVICE.load_latest_object_status(
        agent_label=agent_label,
        workflow_name=workflow_name,
        thread_id=thread_id,
    )


def _create_workflow_session(
    agent_name: str | None,
    *,
    thread_id: int | None = None,
    routing_request: dict[str, Any] | None = None,
    workflow_name: str | None = None,
) -> dict[str, Any] | None:
    return WORKFLOW_SESSION_SERVICE.create_session(
        agent_name,
        thread_id=thread_id,
        routing_request=routing_request,
        workflow_name=workflow_name,
    )


def _update_workflow_retry_status(
    workflow_session: dict[str, Any],
    workflow_config: dict[str, Any],
    *,
    event_name: str,
    payload: dict[str, Any],
    next_state: str,
) -> dict[str, Any]:
    return WORKFLOW_SESSION_SERVICE.update_retry_status(
        workflow_session,
        workflow_config,
        event_name=event_name,
        payload=payload,
        next_state=next_state,
    )


class WorkflowTransitionMatchService:
    def state_matches(self, source: Any, current_state: str) -> bool:
        if source in (None, "", "*"):
            return True
        if isinstance(source, (list, tuple, set)):
            return current_state in {str(item) for item in source}
        return str(source) == current_state

    def payload_value(self, payload: dict[str, Any], key: str) -> Any:
        current: Any = payload
        for segment in str(key).split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(segment)
        return current

    def condition_matches(self, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            if "all" in expected:
                return all(self.condition_matches(actual, item) for item in (expected.get("all") or []))
            if "any" in expected:
                return any(self.condition_matches(actual, item) for item in (expected.get("any") or []))
            if "not" in expected:
                return not self.condition_matches(actual, expected.get("not"))
            if "eq" in expected:
                return actual == expected.get("eq")
            if "in" in expected:
                options = expected.get("in") or []
                return actual in options
            if "not_in" in expected:
                options = expected.get("not_in") or []
                return actual not in options
            if "exists" in expected:
                return (actual is not None) == bool(expected.get("exists"))
            if "truthy" in expected:
                return bool(actual) == bool(expected.get("truthy"))
            if "contains" in expected:
                needle = expected.get("contains")
                if isinstance(actual, str):
                    return str(needle) in actual
                if isinstance(actual, (list, tuple, set)):
                    return needle in actual
                if isinstance(actual, dict):
                    return needle in actual.values() or needle in actual.keys()
                return False
        return actual == expected

    def conditions_match(self, payload: dict[str, Any], conditions: Any) -> bool:
        if not conditions:
            return True
        if isinstance(conditions, dict):
            if "all" in conditions:
                return all(self.conditions_match(payload, item) for item in (conditions.get("all") or []))
            if "any" in conditions:
                return any(self.conditions_match(payload, item) for item in (conditions.get("any") or []))
            if "not" in conditions:
                return not self.conditions_match(payload, conditions.get("not"))
            for key, expected in conditions.items():
                actual = self.payload_value(payload, str(key))
                if not self.condition_matches(actual, expected):
                    return False
            return True
        return bool(conditions)

    def event_name_matches(self, event_kind: str, expected_name: Any, actual_name: str) -> bool:
        if expected_name in (None, "", "*"):
            return True
        if isinstance(expected_name, (list, tuple, set)):
            return any(self.event_name_matches(event_kind, item, actual_name) for item in expected_name)
        if event_kind == "tool":
            return normalize_tool_name(str(expected_name)) == normalize_tool_name(actual_name)
        return str(expected_name) == actual_name

    def message_indicates_failure(self, value: Any) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        failure_prefixes = (
            "error",
            "failed",
            "follow-up model call failed",
            "routing failed",
            "unknown tool",
            "unknown target agent",
            "no user question available",
            "api call error",
        )
        return text.startswith(failure_prefixes)

    def transition_matches(
        self,
        transition: dict[str, Any],
        current_state: str,
        event_kind: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> bool:
        if not self.state_matches(transition.get("from"), current_state):
            return False

        event = transition.get("on") or {}
        if str(event.get("kind") or "") != event_kind:
            return False
        if not self.event_name_matches(event_kind, event.get("name"), event_name):
            return False

        return self.conditions_match(payload, event.get("conditions") or {})


WORKFLOW_TRANSITION_MATCH_SERVICE = WorkflowTransitionMatchService()


class WorkflowTransitionService:
    def load_matching_transition(
        self,
        workflow_config: dict[str, Any],
        *,
        current_state: str,
        event_kind: str,
        event_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        for transition in (workflow_config.get("transitions") or []):
            if not isinstance(transition, dict):
                continue
            if _workflow_transition_matches(transition, current_state, event_kind, event_name, payload):
                return transition
        return None

    def build_updated_session(
        self,
        workflow_session: dict[str, Any],
        workflow_config: dict[str, Any],
        *,
        transition: dict[str, Any],
        event_kind: str,
        event_name: str,
        payload: dict[str, Any],
        retry_status: dict[str, Any],
    ) -> dict[str, Any] | None:
        next_state = str(transition.get("to") or "")
        if not next_state:
            return None

        states = workflow_config.get("states") or {}
        state_config = states.get(next_state) or {}
        updated = dict(workflow_session)
        updated["current_state"] = next_state
        updated["terminal"] = bool(state_config.get("terminal", False))
        updated["last_event"] = {
            "kind": event_kind,
            "name": normalize_tool_name(event_name) if event_kind == "tool" else event_name,
            "payload": payload,
        }
        updated["last_transition"] = deepcopy(transition)
        updated["history"] = list(workflow_session.get("history") or []) + [next_state]
        updated["retry"] = retry_status
        return updated


WORKFLOW_TRANSITION_SERVICE = WorkflowTransitionService()


def _workflow_state_matches(source: Any, current_state: str) -> bool:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.state_matches(source, current_state)


def _workflow_payload_value(payload: dict[str, Any], key: str) -> Any:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.payload_value(payload, key)


def _workflow_condition_matches(actual: Any, expected: Any) -> bool:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.condition_matches(actual, expected)


def _workflow_conditions_match(payload: dict[str, Any], conditions: Any) -> bool:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.conditions_match(payload, conditions)


def _workflow_event_name_matches(event_kind: str, expected_name: Any, actual_name: str) -> bool:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.event_name_matches(event_kind, expected_name, actual_name)


def _message_indicates_failure(value: Any) -> bool:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.message_indicates_failure(value)


def _workflow_transition_matches(
    transition: dict[str, Any],
    current_state: str,
    event_kind: str,
    event_name: str,
    payload: dict[str, Any],
) -> bool:
    return WORKFLOW_TRANSITION_MATCH_SERVICE.transition_matches(
        transition,
        current_state,
        event_kind,
        event_name,
        payload,
    )


def _advance_workflow_session(
    workflow_session: dict[str, Any] | None,
    *,
    event_kind: str,
    event_name: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return WORKFLOW_SESSION_SERVICE.advance_session(
        workflow_session,
        event_kind=event_kind,
        event_name=event_name,
        payload=payload,
    )


class HistoryAccessService:
    def load_history(self) -> Any:
        global _history_instance
        if _history_instance is None:
            try:
                from .chat_completion import ChatHistory as _chat_history_class
            except Exception:
                import os
                import sys

                _pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if _pkg_parent not in sys.path:
                    sys.path.insert(0, _pkg_parent)
                from alde.chat_completion import ChatHistory as _chat_history_class
            _history_instance = _chat_history_class()
        return _history_instance

    def load_latest_user_message(self, default: str = "") -> str:
        try:
            history = self.load_history()
            for entry in reversed(history._history_):
                if isinstance(entry, dict) and entry.get("role") == "user":
                    content = entry.get("content") or ""
                    if isinstance(content, str) and content.strip():
                        return content
        except Exception as exc:
            print(f"[DEBUG] could not read last user message: {exc}")
        return default


HISTORY_ACCESS_SERVICE = HistoryAccessService()


def get_history() -> Any:
    return HISTORY_ACCESS_SERVICE.load_history()


def _latest_user_message(default: str = "") -> str:
    return HISTORY_ACCESS_SERVICE.load_latest_user_message(default)

# Flush on exit only when new entries were added
# def _cleanup_on_exit():
# if len(ChatHistory._history_) > _initial_history_length:
# history._flush()
# atexit.register(_cleanup_on_exit)

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ParamSpec:
    """Parameter specification for tool functions."""
    name: str
    type: str = "string"  # string, number, boolean, array, object
    description: str = ""
    required: bool = False
    enum: list | None = None
    items: dict | None = None
    default: any = None

    def to_python_type(self) -> str:
        """Convert JSON schema type to Python type hint."""
        type_map = {
            "string": "str",
            "number": "float",
            "integer": "int",
            "boolean": "bool",
            "array": "list",
            "object": "dict"
        }
        py_type = type_map.get(self.type, "Any")
        if not self.required:
            py_type = f"{py_type} | None"
        return py_type

    def to_tool_property(self) -> dict:
        """Convert to OpenAI tool parameter property."""
        prop = {"type": self.type, "description": self.description}
        if self.enum:
            prop["enum"] = self.enum #:list
        if self.items:
            prop["items"] = self.items #:dict
        return prop

@dataclass
class ToolSpec:
    """Complete tool specification - single source of truth."""
    name: str
    description: str
    parameters: list[ParamSpec] = field(default_factory=list)
    implementation: Callable | None = None  # Optional: actual function reference

    # Callbacks bound to this tool
    on_call: Callable[[str, dict], None] | None = None  # Called before execution
    on_result: Callable[[str, str], None] | None = None  # Called after execution

    def to_tool_definition(self) -> dict:
        """Generate OpenAI-compatible tool definition."""
        properties = {}
        required = []

        for param in self.parameters:
            properties[param.name] = param.to_tool_property()
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
        }

    def execute(self, args: dict, tool_call_id: str = None) -> str:
        """Execute this tool with logging callbacks."""
        # Call on_call callback if registered
        if self.on_call:
            try:
                self.on_call(self.name, args)
            except Exception as e:
                print(f"on_call error: {e}")

        # Execute the tool
        result = ""
        try:
            if self.implementation:
                # Build kwargs from args
                kwargs = {}
                for p in self.parameters:
                    if p.name in args:
                        kwargs[p.name] = args[p.name]
                    elif p.default is not None:
                        kwargs[p.name] = p.default
                    elif not p.required:
                        kwargs[p.name] = None
                result = self.implementation(**kwargs)
            else:
                result = f"Tool '{self.name}' has no implementation"
        except Exception as e:
            result = f"Tool execution error: {e}"

        # Call on_result callback if registered
        if self.on_result:
            try:
                self.on_result(self.name, result, tool_call_id)
            except Exception as e:
                print(f"on_result error: {e}")

        return result

    def to_function_signature(self) -> str:
        """Generate Python function signature string."""
        params = []
        for p in self.parameters:
            if p.required:
                params.append(f"{p.name}: {p.to_python_type()}")
            else:
                default = f'"{p.default}"' if isinstance(p.default, str) else p.default
                params.append(f"{p.name}: {p.to_python_type()} = {default}")
        return f"def {self.name}({', '.join(params)}) -> str:"

    def to_function_stub(self) -> str:
        """Generate complete Python function stub."""
        sig = self.to_function_signature()
        # Prevent accidental triple-quote termination in generated source.
        safe_desc = (self.description or "").replace('"""', r'\"\"\"')
        docstring = f'    """{safe_desc}"""'
        body = f'    return f"{self.name} executed with params: {{{", ".join(p.name for p in self.parameters)}}}"'
        return f"{sig}\n{docstring}\n{body}"

    def compile_stub(
        self,
        *,
        attach_as_implementation: bool = True,
        globals_dict: dict | None = None,
    ) -> Callable:
        """Compile `to_function_stub()` into a real Python function.

        Uses `exec()` on the generated source code and returns the created
        callable. By default it also assigns it to `self.implementation`.

        Security note: only do this for trusted ToolSpec inputs.
        """

        import keyword
        import re

        def _is_identifier(name: str) -> bool:
            return bool(re.fullmatch(r"[A-Za-z_]\w*", name)) and not keyword.iskeyword(name)

        if not _is_identifier(self.name):
            raise ValueError(f"Tool name is not a valid Python identifier: {self.name!r}")
        for p in self.parameters:
            if not _is_identifier(p.name):
                raise ValueError(f"Param name is not a valid Python identifier: {p.name!r}")

        src = self.to_function_stub()

        ns: dict = {}
        if globals_dict is None:
            # Minimal, but still functional default. (We keep builtins so the
            # function can execute normally.)
            ns["__builtins__"] = __builtins__
        else:
            ns.update(globals_dict)
            ns.setdefault("__builtins__", __builtins__)

        exec(src, ns, ns)
        fn = ns.get(self.name)
        if not callable(fn):
            raise RuntimeError(f"Stub did not define a callable named {self.name!r}")

        if attach_as_implementation:
            self.implementation = fn
        return fn

class AgentBootstrapPromptService:
    def load_triage_messages(self) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are an triage agent. "
                    "1. Ask clarifying questions if information is missing. "
                    "2. If you can answer the question independently, do so. "
                    "3. If a specialized agent is clearly better suited, "
                    "   chose a suited tool or call the function route_to_agent with the appropriate 'target_agent'."
                ),
            }
        ]


AGENT_BOOTSTRAP_PROMPT_SERVICE = AgentBootstrapPromptService()

TRIAGE_BOOTSTRAP_MESSAGES = AGENT_BOOTSTRAP_PROMPT_SERVICE.load_triage_messages()

# Tools that require special handling in dispatcher
_SPECIAL_HANDLED_TOOLS = ['vectordb_tool', 'route_to_agent']

# ============================================================================
# Define ALL tools using the unified factory
# ============================================================================
class ToolRegistryBootstrapService:
    def bind_unified_object_callbacks(self) -> None:
        TOOL_REGISTRY_BINDING_SERVICE.bind_object_callbacks(UNIFIED_TOOLS)


TOOL_REGISTRY_BOOTSTRAP_SERVICE = ToolRegistryBootstrapService()

# ============================================================================
# Default Logging Callbacks - bound to each tool
# ============================================================================
class ToolExecutionCallbackService:
    RAW_VECTOR_SEARCH_TOOL_NAMES = {'memorydb', 'vectordb', 'vectordb_tool', 'VectorDB'}

    def is_raw_vector_search_tool(self, tool_name: str) -> bool:
        return str(tool_name or '').strip() in self.RAW_VECTOR_SEARCH_TOOL_NAMES

    def log_object_call(self, tool_name: str, args: dict[str, Any]) -> None:
        print(f"TOOL CALL: {tool_name} with args: {list(args.keys())}")

    def log_object_result(self, tool_name: str, result: Any, tool_call_id: str = None) -> None:
        try:
            preview = result if isinstance(result, str) else str(result)
        except Exception:
            preview = "[unprintable result]"
        if self.is_raw_vector_search_tool(tool_name):
            print(f"TOOL RESULT: {tool_name} [payload omitted]")
            return
        print(f"TOOL RESULT: {tool_name} -> {preview[:100]}...")
        # Do not log tool role directly into history here to avoid invalid
        # message sequences for OpenAI (tool messages must follow assistant
        # messages with matching tool_calls). Pairing is handled in _handle_tool_calls.


TOOL_EXECUTION_CALLBACK_SERVICE = ToolExecutionCallbackService()


def _default_on_call(tool_name: str, args: dict) -> None:
    """Default callback: log tool call."""
    TOOL_EXECUTION_CALLBACK_SERVICE.log_object_call(tool_name, args)


def _default_on_result(tool_name: str, result: str, tool_call_id: str = None) -> None:
    """Default callback: log tool result to history."""
    TOOL_EXECUTION_CALLBACK_SERVICE.log_object_result(tool_name, result, tool_call_id)


class ToolRegistryBindingService:
    def bind_object_callbacks(self, specs: list[Any]) -> None:
        for spec in specs:
            spec.on_call = _default_on_call
            spec.on_result = _default_on_result


TOOL_REGISTRY_BINDING_SERVICE = ToolRegistryBindingService()

# Bind callbacks to all tools
TOOL_REGISTRY_BOOTSTRAP_SERVICE.bind_unified_object_callbacks()

# ============================================================================
# Special Tool Handlers (vectordb, route_to_agent)
# ============================================================================
def execute_vectordb(args: dict, tool_call_id: str = None) -> tuple[str, dict | None]:
    """Execute vectordb_tool with caching."""
    return VECTOR_SEARCH_DISPATCHER.dispatch_object(args, tool_call_id)


class VectorSearchDispatcher:
    def resolve_object_name(self, args: dict[str, Any]) -> str:
        return str(args.get('vector_tools', 'VectorDB') or 'VectorDB')

    def dispatch_object(self, args: dict[str, Any], tool_call_id: str = None) -> tuple[str, dict | None]:
        query = (args.get('Query') or args.get('query') or '').strip()
        object_name = self.resolve_object_name(args)
        cache_key = f"{object_name}:{query.lower()}"

        if cache_key in _TOOL_CACHE:
            result = _TOOL_CACHE[cache_key]
        else:
            if object_name == 'VectorDB':
                result = vectordb(query, k=3)
            else:
                result = memorydb(query, k=3)
            _TOOL_CACHE[cache_key] = result

        _default_on_result(object_name, result, tool_call_id)
        return result, None


VECTOR_SEARCH_DISPATCHER = VectorSearchDispatcher()


class AgentExecutionSelectionService:
    VALID_SELECTION_MODES = {"job_name", "tool_name"}

    def load_selection_policy(self, agent_config: dict[str, Any] | None) -> dict[str, str]:
        config = dict(agent_config or {})
        policy = dict(config.get("skill_profile_loading") or {})
        selection_mode = str(policy.get("mode") or "job_name").strip() or "job_name"
        if selection_mode not in self.VALID_SELECTION_MODES:
            selection_mode = "job_name"

        fallback_selection_mode = str(policy.get("fallback_selection_mode") or "").strip()
        if fallback_selection_mode and fallback_selection_mode not in self.VALID_SELECTION_MODES:
            fallback_selection_mode = ""

        fallback_skill_profile = str(
            policy.get("fallback_skill_profile")
            or config.get("skill_profile")
            or ""
        ).strip()

        return {
            "selection_mode": selection_mode,
            "fallback_selection_mode": fallback_selection_mode,
            "fallback_skill_profile": fallback_skill_profile,
        }

    def load_job_name(self, routing_request: dict[str, Any] | None) -> str:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_job_name(routing_request)

    def load_tool_name(self, routing_request: dict[str, Any] | None) -> str:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_tool_name(routing_request)

    def load_workflow_name(self, agent_label: str, routing_request: dict[str, Any] | None) -> str:
        selected_job_name = self.load_job_name(routing_request)
        if selected_job_name:
            selected_job_config = get_job_config(selected_job_name)
            selected_job_workflow_name = str(selected_job_config.get("workflow_name") or "").strip()
            if selected_job_workflow_name:
                return selected_job_workflow_name

        selected_contract = ROUTING_HANDOFF_VIEW_SERVICE.load_contract(routing_request)
        selected_contract_workflow_name = str(selected_contract.get("workflow_name") or "").strip()
        if selected_contract_workflow_name:
            return selected_contract_workflow_name

        handoff_payload = ROUTING_HANDOFF_VIEW_SERVICE.load_payload(routing_request)
        metadata = ROUTING_HANDOFF_VIEW_SERVICE.load_metadata(routing_request)
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        for candidate in (
            metadata.get("workflow_name"),
            output_payload.get("workflow_name"),
            handoff_payload.get("workflow_name"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        target_config = _get_runtime_agent_config(agent_label)
        return str(target_config.get("workflow_name") or (target_config.get("workflow") or {}).get("definition") or "").strip()

    def load_job_default_tool_names(self, agent_label: str, routing_request: dict[str, Any] | None) -> list[str]:
        selected_job_name = self.load_job_name(routing_request)
        if not selected_job_name:
            return []

        selected_job_config = get_job_config(selected_job_name)
        selected_runtime_agent = normalize_agent_label(str(selected_job_config.get("runtime_agent") or ""))
        resolved_agent_label = normalize_agent_label(agent_label)
        if selected_runtime_agent and resolved_agent_label and selected_runtime_agent != resolved_agent_label:
            return []

        raw_default_tools = selected_job_config.get("default_tool_names")
        if not isinstance(raw_default_tools, list):
            raw_default_tools = selected_job_config.get("default_tools")
        if not isinstance(raw_default_tools, list):
            return []

        resolved_default_tools: list[str] = []
        for raw_tool_name in raw_default_tools:
            if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
                continue
            normalized_tool_name = normalize_tool_name(raw_tool_name)
            if normalized_tool_name:
                resolved_default_tools.append(normalized_tool_name)
        return resolved_default_tools

    def load_selection_value(self, routing_request: dict[str, Any] | None, selection_mode: str) -> str:
        if selection_mode == "tool_name":
            return self.load_tool_name(routing_request)
        return self.load_job_name(routing_request)

    def load_skill_profile_name(
        self,
        agent_config: dict[str, Any] | None,
        routing_request: dict[str, Any] | None,
    ) -> str:
        config = dict(agent_config or {})
        selection_policy = self.load_selection_policy(config)

        selection_mode = selection_policy.get("selection_mode") or "job_name"
        selected_value = self.load_selection_value(routing_request, selection_mode)
        selection_map_name = "tool_skill_profiles" if selection_mode == "tool_name" else "job_skill_profiles"
        selection_map = dict(config.get(selection_map_name) or {})
        resolved_skill_profile = str(selection_map.get(selected_value) or "").strip() if selected_value else ""

        fallback_selection_mode = selection_policy.get("fallback_selection_mode") or ""
        if not resolved_skill_profile and fallback_selection_mode:
            fallback_value = self.load_selection_value(routing_request, fallback_selection_mode)
            fallback_map_name = "tool_skill_profiles" if fallback_selection_mode == "tool_name" else "job_skill_profiles"
            fallback_map = dict(config.get(fallback_map_name) or {})
            resolved_skill_profile = str(fallback_map.get(fallback_value) or "").strip() if fallback_value else ""

        if resolved_skill_profile:
            return resolved_skill_profile
        return str(selection_policy.get("fallback_skill_profile") or config.get("skill_profile") or "").strip()

    def load_requested_tool_names(
        self,
        agent_label: str,
        routing_request: dict[str, Any] | None,
        explicit_tools: list[Any] | None = None,
    ) -> list[str]:
        requested_tool_names: list[str] = []
        if isinstance(explicit_tools, list):
            for raw_value in explicit_tools:
                if not isinstance(raw_value, str):
                    continue
                normalized_tool_name = normalize_tool_name(raw_value)
                if normalized_tool_name:
                    requested_tool_names.append(normalized_tool_name)

        if not requested_tool_names:
            selected_tool_name = self.load_tool_name(routing_request)
            if selected_tool_name:
                requested_tool_names.append(normalize_tool_name(selected_tool_name))

        if not requested_tool_names:
            requested_tool_names.extend(
                self.load_job_default_tool_names(
                    agent_label,
                    routing_request,
                )
            )

        unique_tool_names: list[str] = []
        seen_tool_names: set[str] = set()
        for tool_name in requested_tool_names:
            normalized_tool_name = normalize_tool_name(tool_name)
            if not normalized_tool_name or normalized_tool_name in seen_tool_names:
                continue
            unique_tool_names.append(normalized_tool_name)
            seen_tool_names.add(normalized_tool_name)

        if not unique_tool_names:
            return []

        allowed_tool_names = _get_allowed_tool_names(agent_label)
        disallowed_tool_names = [tool_name for tool_name in unique_tool_names if tool_name not in allowed_tool_names]
        if disallowed_tool_names:
            raise ValueError(
                "explicit tools are not allowed for {agent}: {tools}".format(
                    agent=agent_label,
                    tools=", ".join(disallowed_tool_names),
                )
            )
        return unique_tool_names

    def load_tool_definitions(
        self,
        agent_label: str,
        routing_request: dict[str, Any] | None,
        explicit_tools: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        requested_tool_names = self.load_requested_tool_names(
            agent_label,
            routing_request,
            explicit_tools=explicit_tools,
        )
        if not requested_tool_names:
            selected_job_name = self.load_job_name(routing_request)
            selected_job_config = get_job_config(selected_job_name) if selected_job_name else {}
            if bool(selected_job_config.get("disable_runtime_tools")):
                return []
            return get_agent_runtime_tools(agent_label)
        return get_agent_tools(requested_tool_names)

    def load_system_text(
        self,
        agent_label: str,
        agent_config: dict[str, Any] | None,
        routing_request: dict[str, Any] | None,
    ) -> str:
        config = dict(agent_config or {})
        resolved_system_text = str(config.get("system") or "")
        selected_job_name = self.load_job_name(routing_request)
        if normalize_agent_label(agent_label) == "_xworker" and selected_job_name:
            specialized_system_text = get_specialized_system_prompt("xworker", selected_job_name)
            if specialized_system_text:
                return specialized_system_text
        return resolved_system_text

    def load_runtime_metadata(
        self,
        agent_label: str,
        agent_config: dict[str, Any] | None,
        routing_request: dict[str, Any] | None,
        explicit_tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        runtime_metadata = _agent_runtime_metadata(agent_label)
        selection_policy = self.load_selection_policy(agent_config)
        runtime_metadata.update(
            {
                "selection_mode": selection_policy.get("selection_mode") or "job_name",
                "fallback_selection_mode": selection_policy.get("fallback_selection_mode") or "",
                "skill_profile": self.load_skill_profile_name(agent_config, routing_request),
                "job_name": self.load_job_name(routing_request),
                "tool_name": self.load_tool_name(routing_request),
                "explicit_tools": self.load_requested_tool_names(
                    agent_label,
                    routing_request,
                    explicit_tools=explicit_tools,
                ),
            }
        )

        handoff_metadata = ROUTING_HANDOFF_VIEW_SERVICE.load_metadata(routing_request)
        session_cache_scope_key = str(handoff_metadata.get("session_cache_scope_key") or "").strip() or None
        attachment_documents = AGENT_RUNTIME_CONFIG_SERVICE.load_object_attachment_documents(
            agent_name=agent_label,
            job_name=str(runtime_metadata.get("job_name") or "").strip() or None,
            tool_name=str(runtime_metadata.get("tool_name") or "").strip() or None,
            scope_key=session_cache_scope_key,
            thread_id=WORKFLOW_CONTEXT_SERVICE.load_current_thread_id(),
        )
        if attachment_documents:
            runtime_metadata["attachment_documents"] = attachment_documents
            runtime_metadata["attachment_count"] = len(attachment_documents)
        return runtime_metadata


AGENT_EXECUTION_SELECTION_SERVICE = AgentExecutionSelectionService()




class AgentRoutingDispatcher:
    def _load_canonical_job_name(self, job_name: str | None) -> str | None:
        normalized_job_name = str(job_name or "").strip()
        if not normalized_job_name:
            return None

        canonical_aliases = {
            "agentdb_operation": "adb_operation",
            "agentsdb_operation": "adb_operation",
            "dispatch_document": "document_dispatch",
            "repo_knowledge_worker": "adb_worker",
            "repo_knowledge_query": "adb_query",
        }
        candidate_job_names: list[str] = [normalized_job_name]
        alias_job_name = str(canonical_aliases.get(normalized_job_name) or "").strip()
        if alias_job_name and alias_job_name not in candidate_job_names:
            candidate_job_names.append(alias_job_name)

        for candidate_job_name in candidate_job_names:
            if get_job_config(candidate_job_name):
                return candidate_job_name
        return normalized_job_name

    def _load_target_job_context(
        self,
        raw_target: str | None,
        job_name: str | None,
    ) -> tuple[str | None, str | None, bool]:
        normalized_target = normalize_agent_label(str(raw_target or "").strip())
        if not normalized_target or normalized_target in AGENTS_REGISTRY:
            return None, None, False

        target_job_name = self._load_canonical_job_name(normalized_target)
        if not target_job_name:
            return None, None, False

        target_job_config = get_job_config(target_job_name)
        target_runtime_agent = normalize_agent_label(str(target_job_config.get("runtime_agent") or ""))
        if not target_runtime_agent or target_runtime_agent not in AGENTS_REGISTRY:
            return None, None, False

        normalized_job_name = str(job_name or "").strip()
        if normalized_job_name and normalized_job_name != target_job_name:
            return None, None, False

        return target_runtime_agent, target_job_name, True

    def _load_route_defaults(self, job_name: str | None) -> dict[str, Any]:
        normalized_job_name = str(job_name or "").strip()
        if not normalized_job_name:
            return {}
        job_config = get_job_config(normalized_job_name)
        route_defaults = job_config.get("route_defaults") if isinstance(job_config, dict) else None
        return deepcopy(route_defaults) if isinstance(route_defaults, dict) else {}

    def _build_sequence_handoff_payload(
        self,
        *,
        args: dict[str, Any],
        route_defaults: dict[str, Any],
        route_job_name: str,
    ) -> dict[str, Any]:
        sequence_payload = route_defaults.get("sequence_payload") if isinstance(route_defaults.get("sequence_payload"), dict) else {}
        if not sequence_payload:
            return {}

        output_payload = deepcopy(sequence_payload)
        passthrough_keys = (
            "action",
            "profile_id",
            "applicant_profile",
            "job_posting",
            "job_posting_result",
            "profile_result",
            "options",
        )
        for key in passthrough_keys:
            if key in args and args.get(key) is not None:
                output_payload[key] = deepcopy(args.get(key))
        profile_id = str(output_payload.get("profile_id") or "").strip()
        if (
            profile_id
            and not isinstance(output_payload.get("applicant_profile"), dict)
            and not isinstance(output_payload.get("profile_result"), dict)
        ):
            output_payload["applicant_profile"] = {
                "source": "profile_id",
                "value": profile_id,
            }
        if not str(output_payload.get("action") or "").strip():
            output_payload["action"] = "generate_cover_letter"

        handoff_payload: dict[str, Any] = {"output": output_payload}
        if route_job_name:
            handoff_payload["job_name"] = route_job_name
        return handoff_payload

    def _payload_path_exists(self, payload: dict[str, Any], key_path: str) -> bool:
        current: Any = payload
        for segment in str(key_path or "").split("."):
            if not segment:
                continue
            if not isinstance(current, dict) or segment not in current:
                return False
            current = current.get(segment)
        return current is not None

    def _load_route_guard_config(self, *, target: str, job_name: str | None) -> dict[str, Any]:
        normalized_job_name = str(job_name or "").strip()
        if normalize_agent_label(target) != "_xworker" or not normalized_job_name:
            return {}
        job_config = get_job_config(normalized_job_name)
        workflow_name = str(job_config.get("workflow_name") or "").strip()
        if not workflow_name:
            return {}
        workflow_config = get_workflow_config(workflow_name)
        route_guard = workflow_config.get("route_guard") if isinstance(workflow_config, dict) else None
        return dict(route_guard) if isinstance(route_guard, dict) else {}

    def resolve_object_name(self, args: dict[str, Any]) -> str:
        agent_response = args.get('agent_response')
        handoff_payload = args.get('handoff_payload')

        object_name = args.get('target_agent', '')
        if not object_name and isinstance(agent_response, dict):
            object_name = agent_response.get('handoff_to', '')
        if not object_name and isinstance(handoff_payload, dict):
            object_name = handoff_payload.get('handoff_to', '')
        return normalize_agent_label(str(object_name or "")) if object_name else ""

    def dispatch_object(
        self,
        object_name: str,
        args: dict,
        tool_call_id: str = None,
        *,
        source_agent_label: str | None = None,
    ) -> tuple[str, dict | None]:
        source_agent_label = normalize_agent_label(source_agent_label or "") if source_agent_label else None

        allow_internal_handoff = bool(args.get("allow_internal_handoff"))
        agent_response = args.get('agent_response')
        handoff_payload = args.get('handoff_payload')
        handoff_protocol = str(args.get('handoff_protocol') or args.get('protocol') or '').strip() or None
        handoff_metadata = args.get('handoff_metadata') if isinstance(args.get('handoff_metadata'), dict) else None
        nested_job_name = None
        nested_tool_name = None
        if isinstance(agent_response, dict):
            nested_job_name = str(agent_response.get('job_name') or '').strip() or None
            output_payload = agent_response.get('output') if isinstance(agent_response.get('output'), dict) else {}
            nested_tool_name = str(agent_response.get('tool_name') or output_payload.get('tool_name') or '').strip() or None
        if nested_job_name is None and isinstance(handoff_payload, dict):
            nested_job_name = str(handoff_payload.get('job_name') or '').strip() or None
        if nested_tool_name is None and isinstance(handoff_payload, dict):
            output_payload = handoff_payload.get('output') if isinstance(handoff_payload.get('output'), dict) else {}
            nested_tool_name = str(handoff_payload.get('tool_name') or output_payload.get('tool_name') or '').strip() or None
        job_name = str(args.get('job_name') or '').strip() or nested_job_name or None
        canonical_job_name = self._load_canonical_job_name(job_name)
        if canonical_job_name:
            job_name = canonical_job_name
            args['job_name'] = canonical_job_name
        explicit_tools = [
            str(value).strip()
            for value in (args.get('tools') or [])
            if isinstance(value, str) and str(value).strip()
        ] if isinstance(args.get('tools'), list) else []
        tool_name = normalize_tool_name(
            str(args.get('tool_name') or nested_tool_name or (explicit_tools[0] if len(explicit_tools) == 1 else '')).strip()
        ) if str(args.get('tool_name') or nested_tool_name or (explicit_tools[0] if len(explicit_tools) == 1 else '')).strip() else None
        handoff_id = str(args.get('handoff_id') or '').strip() or None

        raw_target_value = str(args.get('target_agent') or object_name or '').strip()
        resolved_target_agent, resolved_target_job_name, target_was_job_name = self._load_target_job_context(
            raw_target_value,
            job_name,
        )
        if resolved_target_job_name and not job_name:
            job_name = resolved_target_job_name
            args['job_name'] = resolved_target_job_name

        route_defaults = self._load_route_defaults(job_name)
        job_config = get_job_config(str(job_name or "").strip()) if str(job_name or "").strip() else {}
        job_runtime_agent = normalize_agent_label(str((job_config or {}).get("runtime_agent") or "").strip()) if isinstance(job_config, dict) else ""
        default_target_agent = normalize_agent_label(str(route_defaults.get("target_agent") or job_runtime_agent or "").strip()) if (route_defaults or job_runtime_agent) else ""
        if (not str(args.get("target_agent") or "").strip() or target_was_job_name) and default_target_agent:
            args["target_agent"] = default_target_agent
        elif target_was_job_name and resolved_target_agent:
            args["target_agent"] = resolved_target_agent

        default_handoff_metadata = route_defaults.get("handoff_metadata") if isinstance(route_defaults.get("handoff_metadata"), dict) else {}
        if default_handoff_metadata:
            merged_handoff_metadata = deepcopy(default_handoff_metadata)
            if isinstance(handoff_metadata, dict):
                merged_handoff_metadata.update(deepcopy(handoff_metadata))
            handoff_metadata = merged_handoff_metadata
            args["handoff_metadata"] = deepcopy(handoff_metadata)

        route_job_name = str(route_defaults.get("job_name") or "").strip() if route_defaults else ""
        if route_job_name and job_name:
            if str(args.get("job_name") or "").strip() == str(job_name).strip():
                job_name = route_job_name
                args["job_name"] = job_name

        if route_defaults and agent_response is None and handoff_payload is None:
            route_handoff_payload = self._build_sequence_handoff_payload(
                args=args,
                route_defaults=route_defaults,
                route_job_name=str(job_name or "").strip(),
            )
            if route_handoff_payload:
                handoff_payload = route_handoff_payload
                args["handoff_payload"] = deepcopy(route_handoff_payload)

        target = normalize_agent_label(
            str(args.get('target_agent') or object_name or '').strip()
        ) if str(args.get('target_agent') or object_name or '').strip() else ""

        denied_result = AGENT_ROUTING_REQUEST_SERVICE.load_denied_result(
            target=target,
            source_agent_label=source_agent_label,
            allow_internal_handoff=allow_internal_handoff,
        )
        if denied_result:
            _default_on_result('route_to_agent', denied_result, tool_call_id)
            return denied_result, None

        if target == '_xworker' and not job_name and tool_name:
            job_name = str(get_default_job_name(target) or '').strip() or 'generic_execution'

        if target == '_xworker' and not job_name and not tool_name:
            result = "Invalid route_to_agent payload for _xworker: missing required job_name or tool_name"
            _default_on_result('route_to_agent', result, tool_call_id)
            return result, None

        route_guard_config = self._load_route_guard_config(target=target, job_name=job_name)
        user_question_preview = str(args.get('user_question') or args.get('message_text') or '').strip().lower()
        dispatch_keywords = [
            str(keyword).strip().lower()
            for keyword in (route_guard_config.get("dispatch_keywords") or ["dispatch", "dispatcher"])
            if str(keyword).strip()
        ]
        dispatch_context_requested = bool(
            user_question_preview
            and any(keyword in user_question_preview for keyword in dispatch_keywords)
        )
        requires_structured_handoff = bool(route_guard_config.get("require_structured_handoff_for_dispatch"))
        if requires_structured_handoff and dispatch_context_requested and agent_response is None and handoff_payload is None:
            result = (
                "Invalid route_to_agent payload for parser dispatch: "
                "missing structured handoff_payload/agent_response. "
                "Use dispatch_documents handoff (agent_handoff_v1) with metadata paths."
            )
            _default_on_result('route_to_agent', result, tool_call_id)
            return result, None

        required_route_paths = [
            str(path).strip()
            for path in (route_guard_config.get("required_route_payload_paths") or [])
            if str(path).strip()
        ]
        if required_route_paths and (agent_response is not None or handoff_payload is not None):
            route_payload = {
                "agent_response": agent_response if isinstance(agent_response, dict) else {},
                "handoff_payload": handoff_payload if isinstance(handoff_payload, dict) else {},
                "handoff_metadata": handoff_metadata if isinstance(handoff_metadata, dict) else {},
            }
            missing_paths = [
                key_path
                for key_path in required_route_paths
                if not self._payload_path_exists(route_payload, key_path)
            ]
            if missing_paths:
                result = (
                    "Invalid route_to_agent payload for parser dispatch: "
                    f"missing required handoff paths: {', '.join(missing_paths)}"
                )
                _default_on_result('route_to_agent', result, tool_call_id)
                return result, None

        user_question = AGENT_ROUTING_REQUEST_SERVICE.load_user_question(
            args,
            agent_response=agent_response,
            handoff_payload=handoff_payload,
        )
        if not user_question and agent_response is None and handoff_payload is None:
            result = (f"Unknown target agent: {target}" if target not in AGENTS_REGISTRY
                      else "No user question available for routing")
            _default_on_result('route_to_agent', result, tool_call_id)
            return result, None

        try:
            routing_request = AGENT_ROUTING_REQUEST_SERVICE.build_object_request(
                target=target,
                user_question=user_question,
                agent_response=agent_response,
                handoff_payload=handoff_payload,
                handoff_protocol=handoff_protocol,
                handoff_metadata=handoff_metadata,
                job_name=job_name,
                tool_name=tool_name,
                tools=explicit_tools,
                handoff_id=handoff_id,
                source_agent_label=source_agent_label,
            )
        except Exception as exc:
            result = f"Invalid handoff payload for {target or 'unknown target'}: {type(exc).__name__}: {exc}"
            _default_on_result('route_to_agent', result, tool_call_id)
            return result, None

        result = f"Routing to {target}"
        _default_on_result('route_to_agent', result, tool_call_id)
        return result, routing_request


class AgentRoutingRequestService:
    def load_user_question(
        self,
        args: dict[str, Any],
        *,
        agent_response: Any,
        handoff_payload: Any,
    ) -> str:
        return (
            args.get('user_question', '')
            or args.get('message_text', '')
            or _latest_user_message('')
        )

    def load_denied_result(
        self,
        *,
        target: str,
        source_agent_label: str | None,
        allow_internal_handoff: bool = False,
    ) -> str | None:
        internal_self_route = bool(
            allow_internal_handoff
            and source_agent_label
            and target
            and target == source_agent_label
        )

        if source_agent_label and not _agent_can_route(source_agent_label):
            if not internal_self_route:
                source_config = _get_runtime_agent_config(source_agent_label)
                role_name = source_config.get("role") or "worker"
                return f"Routing denied for {source_agent_label}: role '{role_name}' cannot delegate"

        source_config = _get_runtime_agent_config(source_agent_label)
        source_handoff_policy = dict(source_config.get("handoff_policy") or {})
        allowed_targets = [normalize_agent_label(str(value)) for value in (source_handoff_policy.get("allowed_targets") or []) if str(value).strip()]
        if target and allowed_targets and target != source_agent_label and target not in set(allowed_targets):
            return f"Routing denied for {source_agent_label}: target '{target}' is not allowed by handoff_policy.allowed_targets"

        if target not in AGENTS_REGISTRY:
            return f"Unknown target agent: {target}"

        target_config = _get_runtime_agent_config(target)
        target_handoff_policy = dict(target_config.get("handoff_policy") or {})
        allowed_sources = [normalize_agent_label(str(value)) for value in (target_handoff_policy.get("allowed_sources") or []) if str(value).strip()]
        if source_agent_label and allowed_sources and source_agent_label != target and source_agent_label not in set(allowed_sources):
            return f"Routing denied for {target}: source '{source_agent_label}' is not allowed by handoff_policy.allowed_sources"
        return None

    def build_object_request(
        self,
        *,
        target: str,
        user_question: str,
        agent_response: Any,
        handoff_payload: Any,
        handoff_protocol: str | None,
        handoff_metadata: dict[str, Any] | None,
        job_name: str | None,
        tool_name: str | None,
        tools: list[str] | None,
        handoff_id: str | None,
        source_agent_label: str | None,
    ) -> dict[str, Any]:
        source_config = _get_runtime_agent_config(source_agent_label)
        source_handoff_policy = dict(source_config.get("handoff_policy") or {})
        resolved_handoff_metadata = dict(handoff_metadata or {})
        if job_name and not str(resolved_handoff_metadata.get("job_name") or "").strip():
            resolved_handoff_metadata["job_name"] = job_name
        if tool_name and not str(resolved_handoff_metadata.get("tool_name") or "").strip():
            resolved_handoff_metadata["tool_name"] = normalize_tool_name(tool_name)
        if tools and "tools" not in resolved_handoff_metadata:
            resolved_handoff_metadata["tools"] = [normalize_tool_name(value) for value in tools if str(value).strip()]
        if handoff_id and not str(resolved_handoff_metadata.get("handoff_id") or "").strip():
            resolved_handoff_metadata["handoff_id"] = handoff_id

        initial_handoff_payload = agent_response if isinstance(agent_response, dict) else handoff_payload if isinstance(handoff_payload, dict) else None
        handoff_contract = get_handoff_route_contract(
            source_agent_label,
            target,
            protocol=handoff_protocol,
            handoff_payload=initial_handoff_payload,
            handoff_metadata=resolved_handoff_metadata,
        )
        requested_protocol = str(handoff_protocol or "").strip()
        handoff_schema = dict(handoff_contract.get("schema") or {})
        schema_protocol = str(handoff_schema.get("protocol") or "").strip()
        schema_required_payload_any = [
            str(value).strip()
            for value in (handoff_schema.get("required_payload_any") or [])
            if str(value).strip()
        ]
        if schema_protocol and requested_protocol and requested_protocol != schema_protocol:
            requested_protocol = schema_protocol

        resolved_protocol = requested_protocol or str(handoff_contract.get("protocol") or "").strip() or (
            "agent_handoff_v1"
            if agent_response is not None or handoff_payload is not None
            else str(source_handoff_policy.get("default_protocol") or "message_text")
        )

        resolved_agent_response = agent_response
        resolved_handoff_payload = handoff_payload
        if resolved_protocol == "message_text" and schema_required_payload_any:
            resolved_protocol = "agent_handoff_v1"
        if (
            resolved_protocol == "agent_handoff_v1"
            and resolved_agent_response is None
            and resolved_handoff_payload is None
            and str(user_question or "").strip()
        ):
            resolved_handoff_payload = {"msg": str(user_question).strip()}

        handoff = build_agent_handoff(
            source_agent_label=source_agent_label,
            target_agent=target,
            protocol=resolved_protocol,
            message_text=user_question,
            agent_response=resolved_agent_response,
            handoff_payload=resolved_handoff_payload,
            handoff_metadata=resolved_handoff_metadata,
        )

        handoff_report = validate_handoff_for_target(
            target,
            handoff,
            source_agent_label=source_agent_label,
        )
        if not handoff_report.get("valid"):
            raise ValueError(
                "Invalid handoff payload for {target}: {errors}".format(
                    target=target,
                    errors="; ".join(str(error) for error in (handoff_report.get("errors") or [])),
                )
            )

        prepared_handoff = prepare_incoming_handoff(
            target,
            handoff,
            source_agent_label=source_agent_label,
        )
        target_config = _get_runtime_agent_config(target)
        routing_request_view = {
            "agent_label": target,
            "handoff": handoff,
            "handoff_context": prepared_handoff,
        }
        resolved_system_text = AGENT_EXECUTION_SELECTION_SERVICE.load_system_text(
            target,
            target_config,
            routing_request_view,
        )
        resolved_tools = AGENT_EXECUTION_SELECTION_SERVICE.load_tool_definitions(
            target,
            routing_request_view,
            explicit_tools=tools,
        )
        runtime_metadata = AGENT_EXECUTION_SELECTION_SERVICE.load_runtime_metadata(
            target,
            target_config,
            routing_request_view,
            explicit_tools=tools,
        )
        current_thread_id = WORKFLOW_CONTEXT_SERVICE.load_current_thread_id()
        memory_slot = AGENT_MEMORY_SERVICE.load_amemo_slot(
            job_name=str(runtime_metadata.get("job_name") or "").strip() or job_name,
            tool_name=str(runtime_metadata.get("tool_name") or "").strip() or tool_name,
        )
        session_cache_scope_key = AGENT_MEMORY_SERVICE.load_session_scope_key(
            scope_key=str(resolved_handoff_metadata.get("session_cache_scope_key") or "").strip() or None,
            thread_id=current_thread_id,
        )
        scoped_attachment_documents = AGENT_RUNTIME_CONFIG_SERVICE.load_object_attachment_documents(
            agent_name=target,
            job_name=memory_slot,
            tool_name=str(runtime_metadata.get("tool_name") or "").strip() or None,
            scope_key=session_cache_scope_key,
            thread_id=current_thread_id,
        )
        if scoped_attachment_documents:
            runtime_metadata["attachment_documents"] = scoped_attachment_documents
            runtime_metadata["attachment_count"] = len(scoped_attachment_documents)

        AGENT_MEMORY_SERVICE.ensure_amemo(
            agent_label=target,
            memory_slot=memory_slot,
            scope_key=session_cache_scope_key,
            runtime_metadata=runtime_metadata,
            system_prompt=resolved_system_text,
            source_agent_label=source_agent_label,
        )
        cache_source_payload = (
            handoff_payload
            if isinstance(handoff_payload, dict)
            else (handoff.get("handoff_payload") if isinstance(handoff, dict) else None)
        )
        cache_source_metadata = (
            resolved_handoff_metadata
            if isinstance(resolved_handoff_metadata, dict)
            else (handoff.get("metadata") if isinstance(handoff, dict) else None)
        )
        AGENT_MEMORY_SERVICE.cache_dispatch_profile_context(
            target_agent_label=target,
            target_memory_slot=memory_slot,
            source_agent_label=source_agent_label,
            handoff_payload=cache_source_payload,
            handoff_metadata=cache_source_metadata,
            thread_id=current_thread_id,
            runtime_metadata=runtime_metadata,
            system_prompt=resolved_system_text,
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": resolved_system_text }]
        if prepared_handoff.get("system_context"):
            messages.append({"role": "system", "content": str(prepared_handoff.get("system_context") or "")})
        messages.append({
            "role": "user",
            "content": str(prepared_handoff.get("user_message") or handoff.get("message_text") or ""),
        })
        session_cache_message = AGENT_MEMORY_SERVICE.load_session_cache_message(
            agent_label=target,
            memory_slot=memory_slot,
            scope_key=session_cache_scope_key,
        )
        if session_cache_message is not None:
            messages.append(session_cache_message)
        attachment_context_message = AGENT_RUNTIME_CONFIG_SERVICE.load_object_attachment_message(
            agent_name=target,
            job_name=memory_slot,
            tool_name=str(runtime_metadata.get("tool_name") or "").strip() or None,
            scope_key=session_cache_scope_key,
            thread_id=current_thread_id,
        )
        if attachment_context_message is not None:
            messages.append(attachment_context_message)
        target_history_policy = _agent_history_policy(target)
        return {
            'messages': messages,
            'agent_label': target,
            'tools': resolved_tools,
            'model': target_config.get("model") or model,
            'include_history': bool(target_history_policy.get("include_routed_history")),
            'history_depth': int(target_history_policy.get("routed_history_depth") or 0),
            'handoff': handoff,
            'handoff_context': prepared_handoff,
            'runtime': runtime_metadata,
        }


AGENT_ROUTING_REQUEST_SERVICE = AgentRoutingRequestService()


class RoutingRequestViewService:
    def load_request(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        return routing_request if isinstance(routing_request, dict) else {}

    def has_object_agent(self, routing_request: dict[str, Any] | None) -> bool:
        return bool(str(self.load_request(routing_request).get("agent_label") or "").strip())

    def load_agent_label(self, routing_request: dict[str, Any] | None, fallback: str = "") -> str:
        return str(self.load_request(routing_request).get("agent_label") or fallback or "").strip() or fallback

    def load_messages(self, routing_request: dict[str, Any] | None) -> list[Any]:
        messages = self.load_request(routing_request).get("messages") or []
        if not isinstance(messages, list):
            messages = [messages]
        return list(messages)

    def load_include_history(self, routing_request: dict[str, Any] | None) -> bool:
        return bool(self.load_request(routing_request).get("include_history"))

    def load_history_depth(self, routing_request: dict[str, Any] | None, default: int = 15) -> int:
        try:
            return max(0, int(self.load_request(routing_request).get("history_depth") or default))
        except Exception:
            return default

    def load_tools(self, routing_request: dict[str, Any] | None) -> list[Any]:
        tools = self.load_request(routing_request).get("tools") or []
        if not isinstance(tools, list):
            tools = [tools]
        return list(tools)

    def load_model(self, routing_request: dict[str, Any] | None, fallback_model: Any) -> Any:
        return self.load_request(routing_request).get("model") or fallback_model

    def load_handoff(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_handoff(routing_request)

    def load_handoff_context(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_handoff_context(routing_request)

    def load_contract(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_contract(routing_request)

    def load_result_postprocess(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_result_postprocess(routing_request)

    def load_target_agent(self, routing_request: dict[str, Any] | None) -> str:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_target_agent(routing_request)

    def load_source_agent(self, routing_request: dict[str, Any] | None) -> str:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_source_agent(routing_request)


ROUTING_REQUEST_VIEW_SERVICE = RoutingRequestViewService()


class RoutingHandoffViewService:
    def load_handoff(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        request = routing_request if isinstance(routing_request, dict) else {}
        handoff = request.get("handoff")
        return handoff if isinstance(handoff, dict) else {}

    def load_handoff_context(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        request = routing_request if isinstance(routing_request, dict) else {}
        handoff_context = request.get("handoff_context")
        return handoff_context if isinstance(handoff_context, dict) else {}

    def load_contract(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        contract = self.load_handoff_context(routing_request).get("contract")
        return contract if isinstance(contract, dict) else {}

    def load_result_postprocess(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        schema = self.load_contract(routing_request).get("schema")
        if not isinstance(schema, dict):
            return {}
        result_postprocess = schema.get("result_postprocess")
        return result_postprocess if isinstance(result_postprocess, dict) else {}

    def load_payload(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        handoff_payload = self.load_handoff(routing_request).get("handoff_payload")
        return handoff_payload if isinstance(handoff_payload, dict) else {}

    def load_metadata(self, routing_request: dict[str, Any] | None) -> dict[str, Any]:
        metadata = self.load_handoff(routing_request).get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    def load_target_agent(self, routing_request: dict[str, Any] | None) -> str:
        request = routing_request if isinstance(routing_request, dict) else {}
        handoff = self.load_handoff(request)
        return normalize_agent_label(str(request.get("agent_label") or handoff.get("target_agent") or ""))

    def load_source_agent(self, routing_request: dict[str, Any] | None) -> str:
        handoff = self.load_handoff(routing_request)
        handoff_context = self.load_handoff_context(routing_request)
        return normalize_agent_label(str(handoff.get("source_agent") or handoff_context.get("source_agent") or ""))

    def load_correlation_id(self, routing_request: dict[str, Any] | None) -> str:
        metadata = self.load_metadata(routing_request)
        handoff_payload = self.load_payload(routing_request)
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        return str(
            metadata.get("correlation_id")
            or output_payload.get("correlation_id")
            or handoff_payload.get("correlation_id")
            or ""
        ).strip()

    def load_job_name(self, routing_request: dict[str, Any] | None) -> str:
        metadata = self.load_metadata(routing_request)
        handoff_payload = self.load_payload(routing_request)
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        for candidate in (
            metadata.get("job_name"),
            output_payload.get("job_name"),
            handoff_payload.get("job_name"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    def load_tool_name(self, routing_request: dict[str, Any] | None) -> str:
        metadata = self.load_metadata(routing_request)
        handoff_payload = self.load_payload(routing_request)
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        for candidate in (
            metadata.get("tool_name"),
            metadata.get("tool"),
            output_payload.get("tool_name"),
            output_payload.get("tool"),
            handoff_payload.get("tool_name"),
            handoff_payload.get("tool"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return normalize_tool_name(candidate.strip())
        return ""

    def load_object_name(self, routing_request: dict[str, Any] | None) -> str:
        metadata = self.load_metadata(routing_request)
        handoff_payload = self.load_payload(routing_request)
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        for candidate in (
            metadata.get("obj_name"),
            output_payload.get("obj_name"),
            handoff_payload.get("obj_name"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if isinstance(metadata.get("job_postings_db_path"), str) and str(metadata.get("job_postings_db_path") or "").strip():
            return "job_postings"
        if isinstance(metadata.get("profiles_db_path"), str) and str(metadata.get("profiles_db_path") or "").strip():
            return "profiles"
        job_name = self.load_job_name(routing_request)
        job_config = get_job_config(job_name)
        default_object_name = str(job_config.get("default_object_name") or "").strip()
        if default_object_name:
            return default_object_name
        if isinstance(output_payload.get("job_posting_result"), dict) or isinstance(output_payload.get("job_posting"), dict):
            return "job_postings"
        if isinstance(output_payload.get("profile_result"), dict) or isinstance(output_payload.get("profile"), dict):
            return "profiles"
        return "documents"

    def load_object_db_path(self, routing_request: dict[str, Any] | None, *, resolved_obj_name: str | None = None) -> str:
        metadata = self.load_metadata(routing_request)
        obj_name = str(resolved_obj_name or self.load_object_name(routing_request) or "documents").strip() or "documents"
        candidate_keys = ["obj_db_path", f"{obj_name}_db_path", "job_postings_db_path", "profiles_db_path"]
        seen: set[str] = set()
        for key in candidate_keys:
            if key in seen:
                continue
            seen.add(key)
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def load_dispatcher_paths(self, routing_request: dict[str, Any] | None) -> tuple[str, str]:
        metadata = self.load_metadata(routing_request)
        resolved_obj_name = self.load_object_name(routing_request)
        return (
            str(metadata.get("dispatcher_db_path") or "").strip(),
            self.load_object_db_path(routing_request, resolved_obj_name=resolved_obj_name),
        )


ROUTING_HANDOFF_VIEW_SERVICE = RoutingHandoffViewService()


AGENT_ROUTING_DISPATCHER = AgentRoutingDispatcher()


def execute_route_to_agent(
    args: dict,
    tool_call_id: str = None,
    *,
    source_agent_label: str | None = None,
) -> tuple[str, dict | None]:
    """Execute route_to_agent and return routing request."""
    target_object_name = AGENT_ROUTING_DISPATCHER.resolve_object_name(args)
    return AGENT_ROUTING_DISPATCHER.dispatch_object(
        target_object_name,
        args,
        tool_call_id,
        source_agent_label=source_agent_label,
    )


def execute_forced_route(args: dict, *, ChatCom=None, origin_agent_label: str = "_xplaner_xrouter") -> str:
    resolved_args = dict(args or {})
    requested_origin_agent = normalize_agent_label(
        str(
            resolved_args.pop("_origin_agent_label", "")
            or resolved_args.pop("source_agent_label", "")
            or origin_agent_label
            or "_xplaner_xrouter"
        )
    ) or "_xplaner_xrouter"
    return FORCED_ROUTE_DISPATCHER.dispatch_object(
        resolved_args,
        ChatCom=ChatCom,
        origin_agent_label=requested_origin_agent,
    )


class ForcedRouteDispatcher:
    def dispatch_object(self, args: dict[str, Any], *, ChatCom=None, origin_agent_label: str = "_xplaner_xrouter") -> str:
        tool_call = SimpleNamespace(
            id="forced_route_1",
            function=SimpleNamespace(
                name="route_to_agent",
                arguments=json.dumps(args or {}, ensure_ascii=False),
            ),
        )
        forced_message = SimpleNamespace(content="", tool_calls=[tool_call])
        result = _handle_tool_calls(
            forced_message,
            depth=0,
            ChatCom=ChatCom,
            agent_label=origin_agent_label or "_xplaner_xrouter",
        )
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception:
            return str(result)


FORCED_ROUTE_DISPATCHER = ForcedRouteDispatcher()


class ToolExecutionDispatcher:
    def dispatch_object(
        self,
        object_name: str,
        args: dict,
        tool_call_id: str = None,
        *,
        source_agent_label: str | None = None,
    ) -> tuple[str, dict | None]:
        if object_name == 'vectordb_tool':
            return execute_vectordb(args, tool_call_id)
        if object_name == 'route_to_agent':
            source_label = normalize_agent_label(source_agent_label or "") if source_agent_label else ""
            raw_route_target = str(
                args.get("target_agent")
                or ((args.get("agent_response") or {}).get("handoff_to") if isinstance(args.get("agent_response"), dict) else "")
                or ((args.get("handoff_payload") or {}).get("handoff_to") if isinstance(args.get("handoff_payload"), dict) else "")
                or ""
            )
            route_target = normalize_agent_label(raw_route_target)
            resolved_route_target, _, target_was_job_name = AGENT_ROUTING_DISPATCHER._load_target_job_context(
                raw_route_target,
                args.get("job_name"),
            )
            if target_was_job_name and resolved_route_target:
                route_target = resolved_route_target
            allow_internal_handoff = bool(args.get("allow_internal_handoff"))
            internal_self_route = bool(allow_internal_handoff and source_label and route_target == source_label)
            if source_agent_label and not _is_tool_allowed_for_agent(source_agent_label, object_name) and not internal_self_route:
                return f"Tool '{object_name}' is not allowed for agent {normalize_agent_label(source_agent_label)}", None
            return execute_route_to_agent(args, tool_call_id, source_agent_label=source_agent_label)

        if source_agent_label and not _is_tool_allowed_for_agent(source_agent_label, object_name):
            return f"Tool '{object_name}' is not allowed for agent {normalize_agent_label(source_agent_label)}", None

        spec = get_tool_spec(object_name)
        if spec:
            result = spec.execute(args, tool_call_id)
            return result, None

        return f"Unknown tool: {object_name}", None


TOOL_EXECUTION_DISPATCHER = ToolExecutionDispatcher()

# ============================================================================
# Unified Tool Execution
# ============================================================================
def execute_tool(
    name: str,
    args: dict,
    tool_call_id: str = None,
    *,
    source_agent_label: str | None = None,
) -> tuple[str, dict | None]:
    """
    Execute a tool by name. Returns (result, optional_routing_request).
    Logging is handled by the tool's bound callbacks.
    """
    return TOOL_EXECUTION_DISPATCHER.dispatch_object(
        name,
        args,
        tool_call_id,
        source_agent_label=source_agent_label,
    )

# ============================================================================
# Serialize Tool Calls for Logging
# ============================================================================
class ToolCallSerializationService:
    def serialize_object(self, tool_calls: Any) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            try:
                tool_call_id = getattr(tool_call, 'id', '')
                function_name = ''
                function_arguments = '{}'
                if hasattr(tool_call, 'function'):
                    function_name = getattr(tool_call.function, 'name', '')
                    function_arguments = getattr(tool_call.function, 'arguments', '{}')
                elif isinstance(tool_call, dict):
                    function_data = tool_call.get('function', {})
                    function_name = function_data.get('name', '')
                    function_arguments = function_data.get('arguments', '{}')
                    tool_call_id = tool_call.get('id', tool_call_id)
                serialized.append({
                    'id': tool_call_id,
                    'type': 'function',
                    'function': {'name': str(function_name), 'arguments': str(function_arguments)}
                })
            except Exception:
                serialized.append({
                    'id': '',
                    'type': 'function',
                    'function': {'name': '', 'arguments': '{}'}
                })
        return serialized


TOOL_CALL_SERIALIZATION_SERVICE = ToolCallSerializationService()


def serialize_tool_calls(tool_calls):
        return TOOL_CALL_SERIALIZATION_SERVICE.serialize_object(tool_calls)


class RoutingResultPayloadService:
    def load_payload_object(
        self,
        payload_value: Any,
        *,
        fallback_value: Any = None,
    ) -> "RoutingPayloadObject":
        return RoutingPayloadObject(
            payload_value,
            fallback_value=fallback_value,
        )

    def load_document_artifact_object(
        self,
        *,
        result_postprocess: dict[str, Any] | None = None,
        parsed_result: dict[str, Any] | None = None,
        handoff_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        fallback_result_text: Any = None,
    ) -> "RoutingDocumentArtifactObject":
        return RoutingDocumentArtifactObject(
            result_postprocess=result_postprocess,
            parsed_result=parsed_result,
            handoff_payload=handoff_payload,
            metadata=metadata,
            fallback_result_text=fallback_result_text,
        )

    def resolve_handoff_job_name(
        self,
        *,
        handoff_item: dict[str, Any],
        handoff_payload: dict[str, Any],
        handoff_metadata: dict[str, Any],
    ) -> str | None:
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        for candidate in (
            handoff_item.get("job_name"),
            handoff_payload.get("job_name"),
            output_payload.get("job_name"),
            handoff_metadata.get("job_name"),
            handoff_metadata.get("parser_job_name"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return str(candidate).strip()

        requested_actions = output_payload.get("requested_actions") if isinstance(output_payload.get("requested_actions"), list) else []
        if any(str(action).strip().lower() == "parse" for action in requested_actions):
            return "job_posting_parser"
        return None

    def resolve_handoff_tool_name(
        self,
        *,
        handoff_item: dict[str, Any],
        handoff_payload: dict[str, Any],
        handoff_metadata: dict[str, Any],
    ) -> str | None:
        output_payload = handoff_payload.get("output") if isinstance(handoff_payload.get("output"), dict) else {}
        for candidate in (
            handoff_item.get("tool_name"),
            handoff_payload.get("tool_name"),
            output_payload.get("tool_name"),
            handoff_metadata.get("tool_name"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return normalize_tool_name(str(candidate).strip())
        return None

    def extract_tool_handoff_messages(self, result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict):
            return []

        raw_messages = result.get("handoff_messages") or []
        if not isinstance(raw_messages, list):
            return []

        handoff_messages: list[dict[str, Any]] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            target_agent = normalize_agent_label(str(item.get("target_agent") or item.get("handoff_to") or ""))
            if not target_agent:
                continue
            handoff_payload = deepcopy(item.get("handoff_payload") or {}) if isinstance(item.get("handoff_payload"), dict) else item.get("handoff_payload")
            raw_handoff_metadata = (
                item.get("handoff_metadata")
                if isinstance(item.get("handoff_metadata"), dict)
                else item.get("metadata")
                if isinstance(item.get("metadata"), dict)
                else {}
            )
            handoff_metadata = deepcopy(raw_handoff_metadata)
            resolved_job_name = self.resolve_handoff_job_name(
                handoff_item=item,
                handoff_payload=handoff_payload if isinstance(handoff_payload, dict) else {},
                handoff_metadata=handoff_metadata,
            )
            resolved_tool_name = self.resolve_handoff_tool_name(
                handoff_item=item,
                handoff_payload=handoff_payload if isinstance(handoff_payload, dict) else {},
                handoff_metadata=handoff_metadata,
            )
            route_args: dict[str, Any] = {
                "target_agent": target_agent,
                "handoff_protocol": str(item.get("handoff_protocol") or item.get("protocol") or "").strip() or None,
                "allow_internal_handoff": True,
                "message_text": item.get("message_text"),
                "handoff_payload": handoff_payload,
                "handoff_metadata": handoff_metadata,
            }
            if resolved_job_name:
                route_args["job_name"] = resolved_job_name
            if resolved_tool_name:
                route_args["tool_name"] = resolved_tool_name
            handoff_messages.append(
                route_args
            )
        return handoff_messages

    def parse_json_object(self, value: Any) -> dict[str, Any]:
        return self.load_payload_object(value).load_json_object()

    def build_result_text(self, payload: dict[str, Any], fallback: Any) -> str:
        return self.load_payload_object(payload, fallback_value=fallback).load_result_text()

    def extract_saved_document_path(self, write_result: Any) -> str:
        return self.load_payload_object(write_result).load_saved_document_path()

    def derive_document_output_dir(
        self,
        parsed_result: dict[str, Any],
        handoff_payload: dict[str, Any],
        metadata: dict[str, Any],
    ) -> str:
        return self.load_document_artifact_object(
            parsed_result=parsed_result,
            handoff_payload=handoff_payload,
            metadata=metadata,
        ).load_output_dir()

    def derive_document_doc_id(
        self,
        parsed_result: dict[str, Any],
        handoff_payload: dict[str, Any],
        correlation_id: str,
    ) -> str:
        return self.load_document_artifact_object(
            parsed_result=parsed_result,
            handoff_payload=handoff_payload,
        ).load_doc_id(correlation_id=correlation_id)


class RoutingPayloadObject:
    def __init__(
        self,
        payload_value: Any,
        *,
        fallback_value: Any = None,
    ) -> None:
        self.payload_value = payload_value
        self.fallback_value = fallback_value

    def load_json_object(self) -> dict[str, Any]:
        if isinstance(self.payload_value, dict):
            return deepcopy(self.payload_value)
        if not isinstance(self.payload_value, str):
            return {}
        text = self.payload_value.strip()
        if not text:
            return {}

        candidates: list[str] = [text]

        if text.startswith("```"):
            fence_lines = text.splitlines()
            if fence_lines:
                body_lines = fence_lines[1:]
                if body_lines and body_lines[-1].strip().startswith("```"):
                    body_lines = body_lines[:-1]
                fenced_candidate = "\n".join(body_lines).strip()
                if fenced_candidate:
                    candidates.insert(0, fenced_candidate)

        json_start = text.find("{")
        json_end = text.rfind("}")
        if json_start != -1 and json_end > json_start:
            sliced_candidate = text[json_start:json_end + 1].strip()
            if sliced_candidate and sliced_candidate not in candidates:
                candidates.insert(0, sliced_candidate)

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def load_result_text(self) -> str:
        payload = self.load_json_object()
        if not payload:
            if isinstance(self.fallback_value, str):
                return self.fallback_value
            try:
                return json.dumps(self.fallback_value, ensure_ascii=False)
            except Exception:
                return str(self.fallback_value)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(self.fallback_value)

    def load_saved_document_path(self) -> str:
        if isinstance(self.payload_value, str):
            return str(self.payload_value).split(": ", 1)[-1].strip()
        if isinstance(self.payload_value, dict):
            for key in ("path", "file_path", "document_path", "md_path"):
                value = self.payload_value.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return str(self.payload_value or "").strip()


class RoutingDocumentArtifactObject:
    PAGE_BREAK_MARKER = "<!-- pagebreak -->"

    def __init__(
        self,
        *,
        result_postprocess: dict[str, Any] | None = None,
        parsed_result: dict[str, Any] | None = None,
        handoff_payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        fallback_result_text: Any = None,
    ) -> None:
        self.result_postprocess = deepcopy(result_postprocess or {})
        self.parsed_result = parsed_result if isinstance(parsed_result, dict) else {}
        self.handoff_payload = deepcopy(handoff_payload or {})
        self.metadata = deepcopy(metadata or {})
        self.fallback_result_text = fallback_result_text
        self.output_payload = self.handoff_payload.get("output") if isinstance(self.handoff_payload.get("output"), dict) else {}
        self.document = self.load_document()

    def load_document(self) -> dict[str, Any]:
        document = self.parsed_result.get("document") if isinstance(self.parsed_result.get("document"), dict) else {}
        if document:
            return document
        cover_letter = self.parsed_result.get("cover_letter") if isinstance(self.parsed_result.get("cover_letter"), dict) else {}
        if cover_letter:
            document = deepcopy(cover_letter)
            self.parsed_result.setdefault("document", deepcopy(cover_letter))
        return document

    def extract_text_blocks(
        self,
        value: Any,
        *,
        preferred_keys: tuple[str, ...] = (),
    ) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, list):
            blocks: list[str] = []
            for item in value:
                blocks.extend(self.extract_text_blocks(item, preferred_keys=preferred_keys))
            return blocks
        if isinstance(value, dict):
            blocks: list[str] = []
            ordered_keys = [key for key in preferred_keys if key in value]
            ordered_keys.extend([key for key in value.keys() if key not in ordered_keys])
            for key in ordered_keys:
                blocks.extend(self.extract_text_blocks(value.get(key), preferred_keys=preferred_keys))
            return blocks
        text = str(value).strip()
        return [text] if text else []

    def deduplicate_text_blocks(
        self,
        blocks: list[str],
        *,
        max_items: int = 12,
    ) -> list[str]:
        deduplicated: list[str] = []
        seen: set[str] = set()
        for block in blocks:
            text = str(block or "").strip()
            if not text:
                continue
            key = " ".join(text.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            deduplicated.append(text)
            if len(deduplicated) >= max_items:
                break
        return deduplicated

    def load_profile_result(self) -> dict[str, Any]:
        profile_result = self.output_payload.get("profile_result")
        return profile_result if isinstance(profile_result, dict) else {}

    def load_profile(self) -> dict[str, Any]:
        profile = self.load_profile_result().get("profile")
        return profile if isinstance(profile, dict) else {}

    def load_profile_name(self) -> str:
        profile = self.load_profile()
        personal_info = profile.get("personal_info") if isinstance(profile.get("personal_info"), dict) else {}
        for candidate in (
            personal_info.get("full_name"),
            personal_info.get("name"),
            profile.get("full_name"),
            profile.get("name"),
            profile.get("profile_id"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    def load_profile_contact_lines(self) -> list[str]:
        profile = self.load_profile()
        personal_info = profile.get("personal_info") if isinstance(profile.get("personal_info"), dict) else {}
        lines: list[str] = []
        for label, value in (
            ("Email", personal_info.get("email") or profile.get("email")),
            ("Phone", personal_info.get("phone") or profile.get("phone")),
            ("Location", personal_info.get("location") or profile.get("location")),
            ("LinkedIn", personal_info.get("linkedin") or profile.get("linkedin")),
        ):
            if isinstance(value, str) and value.strip():
                lines.append(f"{label}: {value.strip()}")
        return lines

    def load_profile_skill_blocks(self) -> list[str]:
        profile = self.load_profile()
        blocks: list[str] = []
        for key in ("skills", "technical_skills", "core_skills", "key_skills", "competencies"):
            blocks.extend(self.extract_text_blocks(profile.get(key)))
        return self.deduplicate_text_blocks(blocks, max_items=12)

    def load_profile_experience_blocks(self) -> list[str]:
        profile = self.load_profile()
        blocks: list[str] = []
        for key in ("experience", "work_experience", "projects", "achievements"):
            blocks.extend(
                self.extract_text_blocks(
                    profile.get(key),
                    preferred_keys=(
                        "role",
                        "title",
                        "company",
                        "summary",
                        "description",
                        "text",
                        "value",
                    ),
                )
            )
        return self.deduplicate_text_blocks(blocks, max_items=8)

    def load_profile_education_blocks(self) -> list[str]:
        profile = self.load_profile()
        blocks = self.extract_text_blocks(
            profile.get("education"),
            preferred_keys=("degree", "institution", "field", "summary", "text", "value"),
        )
        return self.deduplicate_text_blocks(blocks, max_items=6)

    def load_profile_language_blocks(self) -> list[str]:
        profile = self.load_profile()
        blocks = self.extract_text_blocks(
            profile.get("languages"),
            preferred_keys=("name", "level", "text", "value"),
        )
        return self.deduplicate_text_blocks(blocks, max_items=6)

    def load_body_blocks(self) -> list[str]:
        body_value = self.document.get("body")
        return self.extract_text_blocks(
            body_value,
            preferred_keys=(
                "opening",
                "motivation",
                "experience",
                "fit",
                "paragraphs",
                "closing",
                "text",
                "value",
            ),
        )

    def load_job_requirement_blocks(self) -> list[str]:
        job_posting_result = self.load_job_posting_result()
        job_posting = job_posting_result.get("job_posting") if isinstance(job_posting_result.get("job_posting"), dict) else {}
        requirements = job_posting.get("requirements") if isinstance(job_posting.get("requirements"), dict) else {}

        blocks: list[str] = []
        for key in ("technical_skills", "soft_skills", "languages", "experience_description", "education"):
            blocks.extend(self.extract_text_blocks(requirements.get(key)))
        blocks.extend(self.extract_text_blocks(job_posting.get("responsibilities")))
        return self.deduplicate_text_blocks(blocks, max_items=12)

    def load_matched_skill_blocks(self) -> list[str]:
        profile_skills = self.load_profile_skill_blocks()
        if not profile_skills:
            return []

        job_requirements = self.load_job_requirement_blocks()
        if not job_requirements:
            return profile_skills[:6]

        matched: list[str] = []
        requirement_keys = [str(requirement).lower() for requirement in job_requirements]
        for skill in profile_skills:
            skill_key = str(skill).lower()
            if any(skill_key in requirement_key or requirement_key in skill_key for requirement_key in requirement_keys):
                matched.append(skill)
        matched = self.deduplicate_text_blocks(matched, max_items=8)
        return matched or profile_skills[:6]

    def build_document_full_text(self) -> str:
        header = self.document.get("header") if isinstance(self.document.get("header"), dict) else {}
        subject = str(
            header.get("subject")
            or ((self.document.get("job_posting") or {}).get("job_title") if isinstance(self.document.get("job_posting"), dict) else "")
            or ""
        ).strip()
        salutation_blocks = self.extract_text_blocks(
            self.document.get("salutation") or self.document.get("introduction"),
            preferred_keys=("first_sentence", "follow_up", "text", "value"),
        )
        conclusion_blocks = self.extract_text_blocks(
            self.document.get("conclusion"),
            preferred_keys=("final_sentence", "closing", "text", "value"),
        )
        sign_off_blocks = self.extract_text_blocks(
            self.document.get("sign_off") or self.document.get("signature") or self.document.get("closing"),
            preferred_keys=("closing", "applicant_name", "name", "text", "value"),
        )
        body_blocks = self.load_body_blocks()

        lines: list[str] = []
        if subject:
            lines.extend([subject, ""])
        for block in salutation_blocks:
            lines.extend([block, ""])
        for block in body_blocks:
            lines.extend([block, ""])
        for block in conclusion_blocks:
            lines.extend([block, ""])
        if sign_off_blocks:
            lines.append("\n".join(sign_off_blocks).strip())

        return "\n".join(lines).strip()

    def should_replace_full_text(self, full_text: str) -> bool:
        normalized = str(full_text or "").strip()
        if not normalized:
            return False
        if normalized.startswith("{") or normalized.startswith("["):
            return True
        if "\n\n{" in normalized or "\n\n[" in normalized:
            return True
        return False

    def load_application_full_text(self) -> str:
        full_text = str(self.document.get("full_text") or "").strip()
        if full_text and not self.should_replace_full_text(full_text):
            return full_text

        synthesized_text = self.build_document_full_text()
        if not synthesized_text and full_text:
            return full_text
        if not synthesized_text:
            return ""

        self.document["full_text"] = synthesized_text
        if isinstance(self.parsed_result.get("document"), dict):
            self.parsed_result["document"]["full_text"] = synthesized_text
        if isinstance(self.parsed_result.get("cover_letter"), dict):
            self.parsed_result["cover_letter"]["full_text"] = synthesized_text
        return synthesized_text

    def load_cv_payload(self) -> dict[str, Any]:
        cv_payload = self.parsed_result.get("cv") if isinstance(self.parsed_result.get("cv"), dict) else {}
        if cv_payload:
            return cv_payload
        resume_payload = self.parsed_result.get("resume") if isinstance(self.parsed_result.get("resume"), dict) else {}
        return resume_payload

    def build_cv_full_text(self) -> str:
        profile = self.load_profile()
        profile_name = self.load_profile_name()
        title, company = self.load_job_posting_identity()
        target_role = " bei ".join(part for part in (title, company) if part)

        cv_payload = self.load_cv_payload()
        summary_text = str(cv_payload.get("summary") or profile.get("summary") or "").strip()
        matched_skills = self.load_matched_skill_blocks()
        profile_skills = self.load_profile_skill_blocks()
        experience_blocks = self.load_profile_experience_blocks()
        education_blocks = self.load_profile_education_blocks()
        language_blocks = self.load_profile_language_blocks()
        contact_lines = self.load_profile_contact_lines()

        lines: list[str] = []
        if profile_name:
            lines.append(profile_name)
        lines.extend(contact_lines)
        if lines:
            lines.append("")

        if target_role:
            lines.extend(["## Target Role", target_role, ""])

        if summary_text:
            lines.extend(["## Profile Summary", summary_text, ""])

        if matched_skills:
            lines.append("## Job Fit")
            lines.extend(f"- {block}" for block in matched_skills)
            lines.append("")

        if profile_skills:
            lines.append("## Skills")
            lines.extend(f"- {block}" for block in profile_skills)
            lines.append("")

        if experience_blocks:
            lines.append("## Experience")
            lines.extend(f"- {block}" for block in experience_blocks)
            lines.append("")

        if education_blocks:
            lines.append("## Education")
            lines.extend(f"- {block}" for block in education_blocks)
            lines.append("")

        if language_blocks:
            lines.append("## Languages")
            lines.extend(f"- {block}" for block in language_blocks)
            lines.append("")

        if not lines:
            lines.extend([
                "## Target Role",
                target_role or "Job-specific CV profile",
                "",
                "## Skills",
                "- No structured profile skills provided.",
            ])

        return "\n".join(lines).strip()

    def load_cv_full_text(self) -> str:
        cv_payload = self.load_cv_payload()
        cv_full_text = str(cv_payload.get("full_text") or "").strip()
        if cv_full_text and not self.should_replace_full_text(cv_full_text):
            return cv_full_text

        synthesized_cv = self.build_cv_full_text()
        if isinstance(self.parsed_result.get("cv"), dict):
            self.parsed_result["cv"]["full_text"] = synthesized_cv
        elif synthesized_cv:
            self.parsed_result["cv"] = {"full_text": synthesized_cv}
        return synthesized_cv

    def load_page_signature(self, *, page_kind: str) -> str:
        if page_kind == "application":
            signature_blocks = self.extract_text_blocks(
                self.document.get("signature") or self.document.get("sign_off") or self.document.get("closing"),
                preferred_keys=("closing", "applicant_name", "name", "text", "value"),
            )
            if signature_blocks:
                return "\n".join(signature_blocks).strip()
        cv_payload = self.load_cv_payload()
        signature = str(cv_payload.get("signature") or self.load_profile_name()).strip()
        return signature

    def build_page_entry(
        self,
        *,
        page: int,
        title: str,
        page_content: str,
        signature: str,
    ) -> dict[str, Any]:
        normalized_content = str(page_content or "").strip()
        content_sha = hashlib.sha256(normalized_content.encode("utf-8", "ignore")).hexdigest()
        metadata = {
            "page": int(page),
            "title": title,
            "titel": title,
            "signature": signature,
            "content_sha": content_sha,
            "content_sha256": content_sha,
        }
        return {
            "page": int(page),
            "title": title,
            "titel": title,
            "signature": signature,
            "page_content": normalized_content,
            "content_sha": content_sha,
            "content_sha256": content_sha,
            "metadata": metadata,
        }

    def load_pages(self) -> list[dict[str, Any]]:
        existing_pages = self.parsed_result.get("pages") if isinstance(self.parsed_result.get("pages"), list) else []
        existing_page_1 = existing_pages[0] if len(existing_pages) > 0 and isinstance(existing_pages[0], dict) else {}
        existing_page_2 = existing_pages[1] if len(existing_pages) > 1 and isinstance(existing_pages[1], dict) else {}

        application_text = str(existing_page_1.get("page_content") or "").strip() or self.load_application_full_text()
        cv_text = str(existing_page_2.get("page_content") or "").strip() or self.load_cv_full_text()

        page_1_title = str(existing_page_1.get("title") or existing_page_1.get("titel") or "Application").strip() or "Application"
        page_2_title = str(existing_page_2.get("title") or existing_page_2.get("titel") or "CV").strip() or "CV"

        pages = [
            self.build_page_entry(
                page=1,
                title=page_1_title,
                page_content=application_text,
                signature=self.load_page_signature(page_kind="application"),
            ),
            self.build_page_entry(
                page=2,
                title=page_2_title,
                page_content=cv_text,
                signature=self.load_page_signature(page_kind="cv"),
            ),
        ]

        if isinstance(self.parsed_result.get("cover_letter"), dict):
            self.parsed_result["cover_letter"]["full_text"] = application_text
        elif application_text:
            self.parsed_result["cover_letter"] = {"full_text": application_text}

        if isinstance(self.parsed_result.get("cv"), dict):
            self.parsed_result["cv"]["full_text"] = cv_text
        elif cv_text:
            self.parsed_result["cv"] = {"full_text": cv_text}

        return pages

    def serialize_pages_to_markdown(self, pages: list[dict[str, Any]]) -> str:
        markdown_lines: list[str] = []
        for index, page in enumerate(pages):
            title = str(page.get("title") or page.get("titel") or "").strip()
            content = str(page.get("page_content") or "").strip()
            if index > 0:
                markdown_lines.extend(["", self.PAGE_BREAK_MARKER, ""])
            if title:
                markdown_lines.extend([f"# {title}", ""])
            if content:
                markdown_lines.append(content)
        return "\n".join(markdown_lines).strip()

    def load_full_text(self) -> str:
        pages = self.load_pages()
        markdown_text = self.serialize_pages_to_markdown(pages)
        if not markdown_text:
            return ""

        self.parsed_result["pages"] = pages
        self.parsed_result["page_count"] = len(pages)
        self.parsed_result["document"] = self.parsed_result.get("document") if isinstance(self.parsed_result.get("document"), dict) else {}
        self.parsed_result["document"]["full_text"] = markdown_text
        self.parsed_result["document"]["page_break_marker"] = self.PAGE_BREAK_MARKER
        self.document = self.parsed_result["document"]
        return markdown_text

    def load_correlation_id(self) -> str:
        correlation = self.parsed_result.get("correlation") if isinstance(self.parsed_result.get("correlation"), dict) else {}
        return str(
            correlation.get("correlation_id")
            or ((self.output_payload.get("job_posting_result") or {}).get("correlation_id") if isinstance(self.output_payload.get("job_posting_result"), dict) else "")
            or self.metadata.get("correlation_id")
            or ""
        ).strip()

    def load_output_dir(self) -> str:
        for candidate in (
            (((self.output_payload.get("options") or {}).get("output_dir")) if isinstance(self.output_payload.get("options"), dict) else None),
            self.metadata.get("output_dir"),
            self.parsed_result.get("output_dir"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return os.path.abspath(os.path.expanduser(candidate.strip()))

        return _default_cover_letter_output_dir()

    def load_job_posting_result(self) -> dict[str, Any]:
        job_posting_result = self.output_payload.get("job_posting_result")
        return job_posting_result if isinstance(job_posting_result, dict) else {}

    def load_job_posting_entity_name(
        self,
        *,
        entity_key: str | None = None,
        entity_type: str | None = None,
        role: str | None = None,
    ) -> str:
        job_posting_result = self.load_job_posting_result()
        entity_payloads = job_posting_result.get("entity_objects")
        if not isinstance(entity_payloads, list):
            return ""
        for entity_payload in entity_payloads:
            if not isinstance(entity_payload, dict):
                continue
            metadata = entity_payload.get("metadata") if isinstance(entity_payload.get("metadata"), dict) else {}
            if entity_key and str(entity_payload.get("entity_key") or entity_payload.get("seed_key") or "").strip() != entity_key:
                continue
            if entity_type and str(entity_payload.get("entity_type") or entity_payload.get("type_key") or "").strip() != entity_type:
                continue
            if role and str(metadata.get("role") or "").strip() != role:
                continue
            for candidate in (
                entity_payload.get("canonical_name"),
                entity_payload.get("name"),
                entity_payload.get("title"),
                entity_payload.get("mention_text"),
            ):
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return ""

    def load_job_posting_identity(self) -> tuple[str, str]:
        job_posting_result = self.load_job_posting_result()
        job_posting = job_posting_result.get("job_posting") if isinstance(job_posting_result.get("job_posting"), dict) else {}
        raw_text_document = job_posting_result.get("raw_text_document") if isinstance(job_posting_result.get("raw_text_document"), dict) else {}
        title = str(
            job_posting.get("job_title")
            or self.load_job_posting_entity_name(entity_key="subject")
            or self.load_job_posting_entity_name(entity_type="job_posting")
            or self.load_job_posting_entity_name(role="subject")
            or raw_text_document.get("title")
            or ""
        ).strip()
        company = str(
            job_posting.get("company_name")
            or self.load_job_posting_entity_name(entity_type="organization")
            or ""
        ).strip()
        return title, company

    def load_doc_id(self, *, correlation_id: str | None = None) -> str:
        title, company = self.load_job_posting_identity()
        if title or company:
            return "_".join(part for part in (title, company) if part)
        if correlation_id:
            return correlation_id
        if self.load_correlation_id():
            return self.load_correlation_id()
        return "document"

    def load_write_pdf(self) -> bool:
        options = self.output_payload.get("options") if isinstance(self.output_payload.get("options"), dict) else {}
        if isinstance(options.get("write_pdf"), bool):
            return bool(options.get("write_pdf"))
        return bool(self.result_postprocess.get("default_write_pdf", True))

    def load_pdf_title(self) -> str | None:
        header = self.document.get("header") if isinstance(self.document.get("header"), dict) else {}
        for candidate in (
            header.get("subject"),
            (self.parsed_result.get("pages") or [{}])[0].get("title") if isinstance(self.parsed_result.get("pages"), list) and self.parsed_result.get("pages") else None,
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def load_pdf_author(self) -> str | None:
        author = self.load_profile_name()
        return author or None

    def build_result_payload(
        self,
        *,
        document_text_path: str,
        document_pdf_path: str | None,
    ) -> dict[str, Any]:
        self.parsed_result["document_text_path"] = document_text_path or None
        self.parsed_result["document_pdf_path"] = document_pdf_path
        self.parsed_result["document_path"] = document_pdf_path or document_text_path or None
        return {
            "ok": True,
            "result": self.parsed_result,
            "result_text": _result_text_from_payload(self.parsed_result, self.fallback_result_text),
        }


ROUTING_RESULT_PAYLOAD_SERVICE = RoutingResultPayloadService()


def _extract_tool_handoff_messages(result: Any) -> list[dict[str, Any]]:
    return ROUTING_RESULT_PAYLOAD_SERVICE.extract_tool_handoff_messages(result)


def _parse_json_object(value: Any) -> dict[str, Any]:
    return ROUTING_RESULT_PAYLOAD_SERVICE.parse_json_object(value)


def _result_text_from_payload(payload: dict[str, Any], fallback: Any) -> str:
    return ROUTING_RESULT_PAYLOAD_SERVICE.build_result_text(payload, fallback)


def _extract_saved_document_path(write_result: Any) -> str:
    return ROUTING_RESULT_PAYLOAD_SERVICE.extract_saved_document_path(write_result)


def _derive_document_output_dir(parsed_result: dict[str, Any], handoff_payload: dict[str, Any], metadata: dict[str, Any]) -> str:
    return ROUTING_RESULT_PAYLOAD_SERVICE.derive_document_output_dir(parsed_result, handoff_payload, metadata)


def _derive_document_doc_id(parsed_result: dict[str, Any], handoff_payload: dict[str, Any], correlation_id: str) -> str:
    return ROUTING_RESULT_PAYLOAD_SERVICE.derive_document_doc_id(parsed_result, handoff_payload, correlation_id)


def _persist_document_artifacts(
    *,
    result_postprocess: dict[str, Any],
    parsed_result: dict[str, Any],
    handoff_payload: dict[str, Any],
    metadata: dict[str, Any],
    fallback_result_text: Any,
) -> dict[str, Any] | None:
    return ROUTING_RESULT_POSTPROCESS_SERVICE.persist_object_artifacts(
        result_postprocess=result_postprocess,
        parsed_result=parsed_result,
        handoff_payload=handoff_payload,
        metadata=metadata,
        fallback_result_text=fallback_result_text,
    )


class RoutingResultObject:
    def __init__(
        self,
        *,
        routing_request: dict[str, Any] | None,
        result_text: Any,
        succeeded: bool,
    ) -> None:
        self.request = ROUTING_REQUEST_VIEW_SERVICE.load_request(routing_request)
        self.result_text = result_text
        self.succeeded = succeeded
        self.result_postprocess = ROUTING_HANDOFF_VIEW_SERVICE.load_result_postprocess(self.request)
        self.object_name = normalize_tool_name(str(self.result_postprocess.get("tool") or ""))
        self.handoff_payload = ROUTING_HANDOFF_VIEW_SERVICE.load_payload(self.request)
        self.metadata = ROUTING_HANDOFF_VIEW_SERVICE.load_metadata(self.request)
        self.target_agent_label = ROUTING_HANDOFF_VIEW_SERVICE.load_target_agent(self.request)
        self.source_agent_label = ROUTING_HANDOFF_VIEW_SERVICE.load_source_agent(self.request)
        self.correlation_id = ROUTING_HANDOFF_VIEW_SERVICE.load_correlation_id(self.request)
        self.obj_name = ROUTING_HANDOFF_VIEW_SERVICE.load_object_name(self.request)
        self.dispatcher_db_path, self.obj_db_path = ROUTING_HANDOFF_VIEW_SERVICE.load_dispatcher_paths(self.request)
        self.parsed_result = self.load_parsed_result(result_text)

    def load_output_payload(self) -> dict[str, Any]:
        output_payload = self.handoff_payload.get("output")
        return output_payload if isinstance(output_payload, dict) else {}

    def load_output_action(self) -> str:
        output_payload = self.load_output_payload()
        return normalize_action_request_name(str(output_payload.get("action") or ""))

    def load_job_name(self) -> str:
        return ROUTING_HANDOFF_VIEW_SERVICE.load_job_name(self.request)

    def load_plain_text_fallback_result(self, result_text: Any) -> dict[str, Any]:
        if not isinstance(result_text, str):
            return {}

        raw_text = result_text.strip()
        if not raw_text or self.load_job_name() != "job_posting_parser":
            return {}

        output_payload = self.load_output_payload()
        file_payload = output_payload.get("file") if isinstance(output_payload.get("file"), dict) else {}
        fallback_result = REQUEST_OBJECT_RESOLUTION_SERVICE.build_inline_result(
            raw_text,
            obj_name=self.obj_name,
            source_path=str(file_payload.get("path") or "").strip() or None,
        )
        if not isinstance(fallback_result, dict) or not fallback_result:
            return {}

        if self.correlation_id:
            fallback_result["correlation_id"] = self.correlation_id
        if self.target_agent_label:
            fallback_result["agent"] = self.target_agent_label

        for key in ("file", "link"):
            value = output_payload.get(key)
            if isinstance(value, dict) and value:
                fallback_result.setdefault(key, deepcopy(value))

        job_name = self.load_job_name()
        if job_name:
            fallback_result.setdefault("job_name", job_name)

        parse_payload = fallback_result.get("parse")
        if isinstance(parse_payload, dict):
            warnings = parse_payload.get("warnings")
            if not isinstance(warnings, list):
                warnings = []
                parse_payload["warnings"] = warnings
            if "parser_result_synthesized_from_plain_text" not in warnings:
                warnings.append("parser_result_synthesized_from_plain_text")

        return fallback_result

    def load_parsed_result(self, result_text: Any) -> dict[str, Any]:
        parsed_result = _parse_json_object(result_text)
        if parsed_result:
            return parsed_result
        return self.load_plain_text_fallback_result(result_text)

    def load_cover_letter_profile_result(self) -> dict[str, Any]:
        output_payload = self.load_output_payload()
        profile_result = output_payload.get("profile_result")
        if isinstance(profile_result, dict) and profile_result:
            embedded_profile = profile_result.get("profile") if isinstance(profile_result.get("profile"), dict) else {}
            profile_source_path = next(
                (
                    str(candidate).strip()
                    for candidate in (
                        profile_result.get("source_path"),
                        profile_result.get("path"),
                        ((profile_result.get("file") or {}).get("path") if isinstance(profile_result.get("file"), dict) else None),
                    )
                    if isinstance(candidate, str) and candidate.strip()
                ),
                None,
            )
            return REQUEST_OBJECT_RESOLUTION_SERVICE._augment_canonical_result_payload(
                deepcopy(profile_result),
                obj_name="profiles",
                correlation_id=str(
                    profile_result.get("correlation_id")
                    or embedded_profile.get("profile_id")
                    or embedded_profile.get("id")
                    or ""
                ).strip() or None,
                source_path=profile_source_path,
            )

        applicant_profile = output_payload.get("applicant_profile")
        if not isinstance(applicant_profile, dict) or not applicant_profile:
            return {}

        profile_payload: dict[str, Any] = {}
        source_value = applicant_profile.get("value")
        if isinstance(source_value, dict) and source_value:
            profile_payload = deepcopy(source_value)
        elif "source" not in applicant_profile and "value" not in applicant_profile:
            profile_payload = deepcopy(applicant_profile)

        correlation_id = ""
        if profile_payload:
            correlation_id = str(
                profile_payload.get("profile_id")
                or profile_payload.get("id")
                or output_payload.get("correlation_id")
                or self.correlation_id
                or ""
            ).strip()
        else:
            source_name = str(applicant_profile.get("source") or "").strip().lower()
            if source_name == "profile_id":
                correlation_id = str(applicant_profile.get("value") or "").strip()
                if correlation_id:
                    profile_payload = {"profile_id": correlation_id}

        if not profile_payload:
            return {}

        options = output_payload.get("options") if isinstance(output_payload.get("options"), dict) else {}
        preferences = profile_payload.get("preferences") if isinstance(profile_payload.get("preferences"), dict) else {}
        language = str(options.get("language") or preferences.get("language") or "de").strip() or "de"

        result: dict[str, Any] = {
            "agent": "xworker",
            "parse": {"language": language, "errors": [], "warnings": []},
            "profile": profile_payload,
        }
        if correlation_id:
            result["correlation_id"] = correlation_id
        inline_profile_source_path = next(
            (
                str(candidate).strip()
                for candidate in (
                    profile_payload.get("source_path") if isinstance(profile_payload, dict) else None,
                    result.get("source_path"),
                )
                if isinstance(candidate, str) and candidate.strip()
            ),
            None,
        )
        return REQUEST_OBJECT_RESOLUTION_SERVICE._augment_canonical_result_payload(
            result,
            obj_name="profiles",
            correlation_id=correlation_id or None,
            source_path=inline_profile_source_path,
        )

    def load_cover_letter_job_posting_result(self) -> dict[str, Any]:
        if not isinstance(self.parsed_result, dict) or not self.parsed_result:
            return {}
        correlation_id = str(
            self.parsed_result.get("correlation_id")
            or self.correlation_id
            or ((self.parsed_result.get("file") or {}).get("content_sha256") if isinstance(self.parsed_result.get("file"), dict) else "")
            or ""
        ).strip()
        return REQUEST_OBJECT_RESOLUTION_SERVICE._augment_canonical_result_payload(
            deepcopy(self.parsed_result),
            obj_name=str(self.parsed_result.get("object_name") or self.obj_name or "job_postings"),
            correlation_id=correlation_id or None,
            source_path=self.load_source_path(),
        )

    def load_writer_job_name(self) -> str:
        return str(self.metadata.get("writer_job_name") or "cover_letter_writer").strip() or "cover_letter_writer"

    def build_cover_letter_handoff_args(self) -> dict[str, Any] | None:
        if self.load_output_action() != "generate_cover_letter":
            return None
        if not self.succeeded or not self.load_processed() or not self.parsed_result:
            return None

        job_posting_result = self.load_cover_letter_job_posting_result()
        if not job_posting_result:
            return None
        profile_result = self.load_cover_letter_profile_result()
        if not profile_result:
            return None

        writer_job_name = self.load_writer_job_name()
        correlation_id = str(
            job_posting_result.get("correlation_id")
            or self.correlation_id
            or ((job_posting_result.get("file") or {}).get("content_sha256") if isinstance(job_posting_result.get("file"), dict) else "")
            or ""
        ).strip()
        if not correlation_id:
            return None

        output_payload = self.load_output_payload()
        writer_payload: dict[str, Any] = {
            "type": "cover_letter_request",
            "action": "generate_cover_letter",
            "job_name": writer_job_name,
            "correlation_id": correlation_id,
            "job_posting_result": deepcopy(job_posting_result),
            "profile_result": profile_result,
            "options": deepcopy(output_payload.get("options") or {}),
        }
        writer_payload["job_posting_result"].setdefault("correlation_id", correlation_id)

        for key in ("applicant_profile", "cover_letter_context", "source_document"):
            value = output_payload.get(key)
            if value is not None:
                writer_payload[key] = deepcopy(value)

        handoff_metadata: dict[str, Any] = {
            "correlation_id": correlation_id,
            "job_name": writer_job_name,
        }
        for key in (
            "sequence_name",
            "parser_job_name",
            "writer_job_name",
            "dispatcher_message_id",
            "dispatcher_db_path",
            "obj_name",
            "obj_db_path",
        ):
            value = self.metadata.get(key)
            if value not in (None, "", [], {}):
                handoff_metadata[key] = deepcopy(value)

        return {
            "target_agent": self.target_agent_label or "_xworker",
            "job_name": writer_job_name,
            "handoff_protocol": "agent_handoff_v1",
            "allow_internal_handoff": True,
            "handoff_payload": {
                "agent_label": self.target_agent_label or "_xworker",
                "handoff_to": self.target_agent_label or "_xworker",
                "output": writer_payload,
            },
            "handoff_metadata": handoff_metadata,
        }

    def load_valid_request(self) -> bool:
        return bool(self.request)

    def load_db_updates(self) -> dict[str, Any]:
        parsed_result = self.parsed_result if isinstance(self.parsed_result, dict) else {}
        file_payload = parsed_result.get("file") if isinstance(parsed_result.get("file"), dict) else {}
        db_updates = parsed_result.get("db_updates") if isinstance(parsed_result.get("db_updates"), dict) else {}
        effective_updates = dict(db_updates)

        correlation_id = str(
            parsed_result.get("correlation_id")
            or self.correlation_id
            or effective_updates.get("correlation_id")
            or ""
        ).strip()
        content_sha256 = str(
            parsed_result.get("content_sha256")
            or file_payload.get("content_sha256")
            or effective_updates.get("content_sha256")
            or correlation_id
            or ""
        ).strip()
        processing_state = str(
            parsed_result.get("processing_state")
            or parsed_result.get("status")
            or effective_updates.get("processing_state")
            or ("processed" if self.succeeded else "failed")
        ).strip() or ("processed" if self.succeeded else "failed")
        processed_value = parsed_result.get("processed")
        if isinstance(processed_value, bool):
            processed = processed_value
        elif isinstance(effective_updates.get("processed"), bool):
            processed = bool(effective_updates.get("processed"))
        else:
            processed = processing_state == "processed"
        failed_reason = str(
            parsed_result.get("failed_reason")
            or parsed_result.get("last_error")
            or effective_updates.get("failed_reason")
            or ""
        ).strip() or None

        if correlation_id:
            effective_updates["correlation_id"] = correlation_id
        if content_sha256:
            effective_updates["content_sha256"] = content_sha256
        effective_updates["processing_state"] = processing_state
        effective_updates["processed"] = processed
        effective_updates["failed_reason"] = failed_reason

        existing_record_id = str(
            effective_updates.get("existing_record_id")
            or parsed_result.get("id")
            or ""
        ).strip()
        if existing_record_id:
            effective_updates["existing_record_id"] = existing_record_id
        return effective_updates

    def load_processing_state(self) -> str:
        db_updates = self.load_db_updates()
        return str(db_updates.get("processing_state") or ("processed" if self.succeeded else "failed")).strip() or ("processed" if self.succeeded else "failed")

    def load_processed(self) -> bool:
        db_updates = self.load_db_updates()
        processed = db_updates.get("processed")
        if isinstance(processed, bool):
            return processed
        return self.load_processing_state() == "processed"

    def load_failed_reason(self) -> str | None:
        db_updates = self.load_db_updates()
        failed_reason = str(db_updates.get("failed_reason") or "").strip() or None
        if not self.succeeded and not failed_reason:
            return str(self.result_text or "").strip() or "routed_agent_failed"
        return failed_reason

    def load_canonical_record(self) -> dict[str, Any]:
        processing_state = self.load_processing_state()
        processed = self.load_processed()
        failed_reason = self.load_failed_reason()
        db_updates = self.load_db_updates()
        if self.parsed_result:
            partial_record: dict[str, Any] = dict(self.parsed_result)
        else:
            partial_record = {
                "correlation_id": self.correlation_id,
                "db_updates": {
                    "correlation_id": self.correlation_id,
                    "processing_state": processing_state,
                    "processed": processed,
                    "failed_reason": failed_reason,
                },
            }

        output_payload = self.load_output_payload()
        output_file_payload = output_payload.get("file") if isinstance(output_payload.get("file"), dict) else {}
        if not isinstance(partial_record.get("file"), dict):
            partial_record["file"] = {}
        if isinstance(output_file_payload, dict) and output_file_payload:
            file_payload = dict(partial_record.get("file") or {})
            for key, value in output_file_payload.items():
                file_payload.setdefault(str(key), deepcopy(value))
            partial_record["file"] = file_payload

        correlation_id = str(
            partial_record.get("correlation_id")
            or self.correlation_id
            or db_updates.get("correlation_id")
            or ((partial_record.get("file") or {}).get("content_sha256") if isinstance(partial_record.get("file"), dict) else "")
            or "result"
        ).strip() or "result"
        source_agent = str(
            partial_record.get("source")
            or partial_record.get("source_agent")
            or partial_record.get("agent")
            or self.load_source_agent()
            or self.target_agent_label
            or self.source_agent_label
            or ""
        ).strip() or None
        return DOCUMENT_REPOSITORY._normalize_operational_record(
            record=partial_record,
            correlation_id=correlation_id,
            object_name=str(partial_record.get("object_name") or self.obj_name or "documents"),
            source_agent=source_agent,
            source_path=self.load_source_path(),
            title=str(partial_record.get("title") or "").strip() or None,
            content_sha256=str(
                partial_record.get("content_sha256")
                or db_updates.get("content_sha256")
                or ((partial_record.get("file") or {}).get("content_sha256") if isinstance(partial_record.get("file"), dict) else "")
                or correlation_id
            ).strip() or correlation_id,
            processing_state=processing_state,
            processed=processed,
            failed_reason=failed_reason,
        )

    def load_dispatcher_updates(self) -> dict[str, Any]:
        normalized_record = self.load_canonical_record()
        db_updates = self.load_db_updates()
        dispatcher_updates: dict[str, Any] = {
            "id": normalized_record.get("id"),
            "correlation_id": normalized_record.get("correlation_id"),
            "source": normalized_record.get("source"),
            "source_agent": normalized_record.get("source_agent"),
            "source_path": normalized_record.get("source_path"),
            "path": normalized_record.get("path"),
            "title": normalized_record.get("title"),
            "record_kind": normalized_record.get("record_kind"),
            "kind": normalized_record.get("kind"),
            "object_name": normalized_record.get("object_name"),
            "content_sha256": normalized_record.get("content_sha256"),
            "status": normalized_record.get("status"),
            "processing_state": normalized_record.get("processing_state"),
            "processed": normalized_record.get("processed"),
            "failed_reason": normalized_record.get("failed_reason"),
        }
        if isinstance(normalized_record.get("file"), dict) and normalized_record.get("file"):
            dispatcher_updates["file"] = deepcopy(normalized_record.get("file") or {})
        if isinstance(normalized_record.get("link"), dict) and normalized_record.get("link"):
            dispatcher_updates["link"] = deepcopy(normalized_record.get("link") or {})
        if isinstance(db_updates.get("existing_record_id"), str) and db_updates.get("existing_record_id"):
            dispatcher_updates["id"] = db_updates.get("existing_record_id")
        return dispatcher_updates

    def load_source_path(self) -> str | None:
        parsed_result = self.parsed_result if isinstance(self.parsed_result, dict) else {}
        file_payload = parsed_result.get("file") if isinstance(parsed_result.get("file"), dict) else {}
        output_payload = self.load_output_payload()
        output_file_payload = output_payload.get("file") if isinstance(output_payload.get("file"), dict) else {}
        for candidate in (
            parsed_result.get("source_path"),
            parsed_result.get("path"),
            file_payload.get("path"),
            file_payload.get("source_uri"),
            output_file_payload.get("path"),
            output_file_payload.get("source_uri"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def load_upsert_payload(self) -> dict[str, Any]:
        processing_state = self.load_processing_state()
        processed = self.load_processed()
        failed_reason = self.load_failed_reason()
        if self.parsed_result:
            upsert_payload = dict(self.parsed_result)
        else:
            upsert_payload = {
            "correlation_id": self.correlation_id,
            "db_updates": {
                "correlation_id": self.correlation_id,
                "processing_state": processing_state,
                "processed": processed,
                "failed_reason": failed_reason,
            },
        }
        normalized_record = self.load_canonical_record()

        upsert_payload.update(
            {
                "id": normalized_record.get("id"),
                "correlation_id": normalized_record.get("correlation_id"),
                "source": normalized_record.get("source"),
                "source_agent": normalized_record.get("source_agent"),
                "source_path": normalized_record.get("source_path"),
                "path": normalized_record.get("path"),
                "title": normalized_record.get("title"),
                "record_kind": normalized_record.get("record_kind"),
                "kind": normalized_record.get("kind"),
                "object_name": normalized_record.get("object_name"),
                "content_sha256": normalized_record.get("content_sha256"),
                "status": normalized_record.get("status"),
                "processing_state": normalized_record.get("processing_state"),
                "processed": normalized_record.get("processed"),
                "failed_reason": normalized_record.get("failed_reason"),
                "db_updates": DOCUMENT_REPOSITORY._build_legacy_db_updates(record=normalized_record),
            }
        )
        if isinstance(normalized_record.get("file"), dict) and normalized_record.get("file"):
            upsert_payload["file"] = deepcopy(normalized_record.get("file") or {})
        if failed_reason and not isinstance(upsert_payload.get("error"), str):
            upsert_payload["error"] = failed_reason
        return upsert_payload

    def load_source_agent(self) -> str:
        postprocess_source_agent = str(self.result_postprocess.get("source_agent") or "target_agent").strip().lower()
        if postprocess_source_agent == "source_agent":
            return self.source_agent_label
        if postprocess_source_agent and postprocess_source_agent not in {"target_agent", "source_agent"}:
            return normalize_agent_label(postprocess_source_agent)
        return self.target_agent_label


class RoutingResultPostprocessService:
    def load_object_result(
        self,
        routing_request: dict[str, Any] | None,
        *,
        result_text: Any,
        succeeded: bool,
    ) -> RoutingResultObject:
        return RoutingResultObject(
            routing_request=routing_request,
            result_text=result_text,
            succeeded=succeeded,
        )

    def persist_object_artifacts(
        self,
        *,
        result_postprocess: dict[str, Any],
        parsed_result: dict[str, Any],
        handoff_payload: dict[str, Any],
        metadata: dict[str, Any],
        fallback_result_text: Any,
    ) -> dict[str, Any] | None:
        artifact_object = ROUTING_RESULT_PAYLOAD_SERVICE.load_document_artifact_object(
            result_postprocess=result_postprocess,
            parsed_result=parsed_result,
            handoff_payload=handoff_payload,
            metadata=metadata,
            fallback_result_text=fallback_result_text,
        )
        full_text = artifact_object.load_full_text()
        if not full_text:
            return None

        output_dir = artifact_object.load_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        correlation_id = artifact_object.load_correlation_id()
        doc_id = artifact_object.load_doc_id(correlation_id=correlation_id)
        write_result = write_document(content=full_text, path=output_dir, doc_id=doc_id, correlation_id=correlation_id)
        document_text_path = _extract_saved_document_path(write_result)
        document_pdf_path: str | None = None

        if artifact_object.load_write_pdf() and document_text_path:
            pdf_path = os.path.splitext(document_text_path)[0] + ".pdf"
            pdf_result = md_to_pdf(
                md_path=document_text_path,
                pdf_path=pdf_path,
                title=artifact_object.load_pdf_title(),
                author=artifact_object.load_pdf_author(),
            )
            if isinstance(pdf_result, dict) and isinstance(pdf_result.get("pdf_path"), str):
                document_pdf_path = str(pdf_result.get("pdf_path") or "").strip() or None

        return artifact_object.build_result_payload(
            document_text_path=document_text_path,
            document_pdf_path=document_pdf_path,
        )

    def apply_object_result(
        self,
        routing_request: dict[str, Any] | None,
        *,
        result_text: Any,
        succeeded: bool,
    ) -> dict[str, Any] | None:
        result_object = self.load_object_result(
            routing_request,
            result_text=result_text,
            succeeded=succeeded,
        )
        if not result_object.load_valid_request():
            return None

        if result_object.object_name == "persist_document_artifacts":
            return self.persist_object_artifacts(
                result_postprocess=result_object.result_postprocess,
                parsed_result=result_object.parsed_result,
                handoff_payload=result_object.handoff_payload,
                metadata=result_object.metadata,
                fallback_result_text=result_text,
            )

        if result_object.object_name == "store_object_result":
            result = DOCUMENT_OBJECT_SERVICE.store_result(
                object_result=result_object.load_upsert_payload(),
                correlation_id=result_object.correlation_id or None,
                db_path=result_object.obj_db_path or None,
                obj_name=result_object.obj_name,
                source_agent=result_object.load_source_agent(),
                source_payload=result_object.handoff_payload,
            )
            parsed_store = _parse_json_object(result)
            if parsed_store:
                parsed_store.setdefault("result", result_object.parsed_result)
                parsed_store.setdefault("result_text", _result_text_from_payload(result_object.parsed_result, result_text))
                return parsed_store
            return {"ok": False, "raw_result": str(result)}

        if result_object.object_name not in {"upsert_object_record", "upsert_dispatcher_job_record"}:
            return None

        if not result_object.correlation_id or not result_object.dispatcher_db_path or not result_object.obj_db_path:
            return None

        result = DOCUMENT_OBJECT_SERVICE.upsert_object_record(
            object_result=result_object.load_upsert_payload(),
            correlation_id=result_object.correlation_id,
            dispatcher_db_path=result_object.dispatcher_db_path,
            obj_db_path=result_object.obj_db_path,
            obj_name=result_object.obj_name,
            processing_state=result_object.load_processing_state(),
            processed=result_object.load_processed(),
            failed_reason=result_object.load_failed_reason(),
            source_agent=result_object.load_source_agent(),
            source_payload=result_object.handoff_payload,
            dispatcher_updates=result_object.load_dispatcher_updates() or None,
        )
        if isinstance(result, dict):
            result["result"] = result_object.parsed_result
            result["result_text"] = _result_text_from_payload(result_object.parsed_result, result_text)
            cover_letter_handoff = result_object.build_cover_letter_handoff_args()
            if cover_letter_handoff:
                result["handoff_messages"] = [cover_letter_handoff]
            return result
        parsed_upsert = _parse_json_object(result)
        if parsed_upsert:
            parsed_upsert.setdefault("result", result_object.parsed_result)
            parsed_upsert.setdefault("result_text", _result_text_from_payload(result_object.parsed_result, result_text))
            cover_letter_handoff = result_object.build_cover_letter_handoff_args()
            if cover_letter_handoff:
                parsed_upsert["handoff_messages"] = [cover_letter_handoff]
            return parsed_upsert
        return {"ok": False, "raw_result": str(result)}


ROUTING_RESULT_POSTPROCESS_SERVICE = RoutingResultPostprocessService()


def _execute_router_branch_process_object_call(result_queue: Any, branch_payload: dict[str, Any]) -> None:
    branch_index = int(branch_payload.get("branch_index") or 0)
    started = time.perf_counter()
    try:
        tool_call = SimpleNamespace(
            id=str(branch_payload.get("tool_call_id") or f"parallel_route_group_branch_{branch_index}"),
            function=SimpleNamespace(
                name=str(branch_payload.get("tool_name") or "route_to_agent"),
                arguments=str(branch_payload.get("tool_arguments") or "{}"),
            ),
        )
        depth = int(branch_payload.get("depth") or 0)
        agent_label = str(branch_payload.get("agent_label") or "")
        result = _handle_tool_calls(
            SimpleNamespace(content="", tool_calls=[tool_call]),
            depth=depth + 1,
            ChatCom=None,
            agent_label=agent_label,
        )
        result_queue.put(
            {
                "branch_index": branch_index,
                "status": "completed",
                "timed_out": False,
                "duration_ms": round(max(time.perf_counter() - started, 0.0) * 1000.0, 2),
                "result": result,
                "error": "",
            }
        )
    except Exception as exc:
        result_queue.put(
            {
                "branch_index": branch_index,
                "status": "failed",
                "timed_out": False,
                "duration_ms": round(max(time.perf_counter() - started, 0.0) * 1000.0, 2),
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )


class ToolCallExecutionService:
    _HANDOFF_PARALLEL_ENABLED_ENV = "ALDE_HANDOFF_PARALLEL_ENABLED"
    _HANDOFF_PARALLEL_WORKERS_ENV = "ALDE_HANDOFF_PARALLEL_WORKERS"
    _ROUTER_BRANCH_PARALLEL_ENABLED_ENV = "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED"
    _ROUTER_BRANCH_PARALLEL_WORKERS_ENV = "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS"
    _ROUTER_BRANCH_TIMEOUT_SECONDS_ENV = "ALDE_ROUTER_BRANCH_TIMEOUT_SECONDS"
    _ROUTER_BRANCH_HARD_TIMEOUT_ENABLED_ENV = "ALDE_ROUTER_BRANCH_HARD_TIMEOUT_ENABLED"

    def __init__(self) -> None:
        self._router_parallel_status_lock = Lock()
        self._router_parallel_status = {
            "total_parallel_runs": 0,
            "total_parallel_branches": 0,
            "total_completed_branches": 0,
            "total_failed_branches": 0,
            "total_timeout_branches": 0,
            "total_partial_fail_runs": 0,
            "total_hard_timeout_runs": 0,
            "last_run_at": "",
            "last_run_duration_ms": 0.0,
            "last_run_branch_count": 0,
            "last_run_partial_fail": False,
            "last_run_hard_timeout_enabled": False,
            "last_run_status_counts": {"completed": 0, "failed": 0, "timeout": 0},
            "last_run_worker_count": 1,
            "last_run_timeout_seconds": 0.0,
        }

    def sanitize_object(self, value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, item in value.items():
                safe_key = key if isinstance(key, (str, int, float, bool)) or key is None else str(key)
                safe[str(safe_key)] = self.sanitize_object(item)
            return safe
        if isinstance(value, list):
            return [self.sanitize_object(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        try:
            return str(value)
        except Exception:
            return "[unserializable]"

    def render_object_result(self, value: Any) -> str:
        try:
            if isinstance(value, (dict, list)):
                return json.dumps(self.sanitize_object(value), ensure_ascii=False)
            return str(value)
        except Exception:
            return "[unprintable tool result]"

    def load_handoff_parallel_object_config(self, object_name: str) -> dict[str, Any]:
        normalized_object_name = str(object_name or "").strip() or "tool_handoff"
        parallel_enabled = self._load_object_bool_from_env(
            self._HANDOFF_PARALLEL_ENABLED_ENV,
            default=False,
        )
        max_parallel_workers = self._load_object_int_from_env(
            self._HANDOFF_PARALLEL_WORKERS_ENV,
            default=4,
            minimum=1,
            maximum=16,
        )
        return {
            "object_name": normalized_object_name,
            "parallel_enabled": parallel_enabled,
            "max_parallel_workers": max_parallel_workers,
            "worker_count": max_parallel_workers if parallel_enabled else 1,
        }

    def load_router_branch_parallel_object_config(self, object_name: str) -> dict[str, Any]:
        normalized_object_name = str(object_name or "").strip() or "router_branch"
        parallel_enabled = self._load_object_bool_from_env(
            self._ROUTER_BRANCH_PARALLEL_ENABLED_ENV,
            default=False,
        )
        max_parallel_workers = self._load_object_int_from_env(
            self._ROUTER_BRANCH_PARALLEL_WORKERS_ENV,
            default=4,
            minimum=1,
            maximum=16,
        )
        timeout_seconds = self._load_object_float_from_env(
            self._ROUTER_BRANCH_TIMEOUT_SECONDS_ENV,
            default=30.0,
            minimum=0.1,
            maximum=600.0,
        )
        hard_timeout_enabled = self._load_object_bool_from_env(
            self._ROUTER_BRANCH_HARD_TIMEOUT_ENABLED_ENV,
            default=False,
        )
        return {
            "object_name": normalized_object_name,
            "parallel_enabled": parallel_enabled,
            "max_parallel_workers": max_parallel_workers,
            "worker_count": max_parallel_workers if parallel_enabled else 1,
            "timeout_seconds": timeout_seconds,
            "hard_timeout_enabled": hard_timeout_enabled,
        }

    @staticmethod
    def _load_object_bool_from_env(env_name: str, *, default: bool) -> bool:
        raw_value = str(os.getenv(env_name, "1" if default else "0") or "").strip().lower()
        if raw_value in {"1", "true", "yes", "on"}:
            return True
        if raw_value in {"0", "false", "no", "off"}:
            return False
        return bool(default)

    @staticmethod
    def _load_object_int_from_env(
        env_name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            loaded_value = int(str(os.getenv(env_name, str(default)) or str(default)).strip())
        except Exception:
            loaded_value = int(default)
        return max(minimum, min(maximum, loaded_value))

    @staticmethod
    def _load_object_float_from_env(
        env_name: str,
        *,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            loaded_value = float(str(os.getenv(env_name, str(default)) or str(default)).strip())
        except Exception:
            loaded_value = float(default)
        return max(minimum, min(maximum, loaded_value))

    def build_handoff_object_tool_call(
        self,
        *,
        parent_tool_call_id: str,
        handoff_index: int,
        handoff_args: dict[str, Any],
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"{parent_tool_call_id}_handoff_{handoff_index}",
            function=SimpleNamespace(
                name="route_to_agent",
                arguments=json.dumps(handoff_args, ensure_ascii=False),
            ),
        )

    def execute_handoff_object_message(
        self,
        *,
        parent_tool_call_id: str,
        handoff_index: int,
        handoff_args: dict[str, Any],
        depth: int,
        ChatCom,
        agent_label: str,
    ) -> Any:
        synthetic_tool_call = self.build_handoff_object_tool_call(
            parent_tool_call_id=parent_tool_call_id,
            handoff_index=handoff_index,
            handoff_args=handoff_args,
        )
        return _handle_tool_calls(
            SimpleNamespace(content="", tool_calls=[synthetic_tool_call]),
            depth=depth + 1,
            ChatCom=ChatCom,
            agent_label=agent_label,
        )

    def execute_handoff_object_messages(
        self,
        *,
        parent_tool_call_id: str,
        handoff_messages: list[dict[str, Any]],
        depth: int,
        ChatCom,
        agent_label: str,
    ) -> list[Any]:
        handoff_items = [
            (index, dict(handoff_args or {}))
            for index, handoff_args in enumerate(handoff_messages, start=1)
        ]
        if not handoff_items:
            return []

        config = self.load_handoff_parallel_object_config("tool_handoff")
        worker_count = min(
            int(config.get("worker_count") or 1),
            len(handoff_items),
        )
        if worker_count <= 1:
            return [
                self.execute_handoff_object_message(
                    parent_tool_call_id=parent_tool_call_id,
                    handoff_index=handoff_index,
                    handoff_args=handoff_args,
                    depth=depth,
                    ChatCom=ChatCom,
                    agent_label=agent_label,
                )
                for handoff_index, handoff_args in handoff_items
            ]

        futures = []
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="alde-handoff") as executor:
            for handoff_index, handoff_args in handoff_items:
                futures.append(
                    executor.submit(
                        self.execute_handoff_object_message,
                        parent_tool_call_id=parent_tool_call_id,
                        handoff_index=handoff_index,
                        handoff_args=handoff_args,
                        depth=depth,
                        ChatCom=ChatCom,
                        agent_label=agent_label,
                    )
                )

            ordered_results: list[Any] = []
            for future in futures:
                try:
                    ordered_results.append(future.result())
                except Exception as exc:
                    ordered_results.append(f"Handoff execution failed: {exc}")
        return ordered_results

    def _load_tool_call_object_name(self, tool_call: Any) -> str:
        tool_function = getattr(tool_call, "function", None)
        return normalize_tool_name(str(getattr(tool_function, "name", "") or ""))

    def should_execute_router_parallel_object_calls(
        self,
        *,
        agent_msg: Any,
        agent_label: str,
    ) -> bool:
        config = self.load_router_branch_parallel_object_config("router_branch")
        if not bool(config.get("parallel_enabled")):
            return False
        if not _agent_can_route(agent_label):
            return False

        tool_calls = list(getattr(agent_msg, "tool_calls", []) or [])
        if len(tool_calls) < 2:
            return False

        return all(
            self._load_tool_call_object_name(tool_call) == "route_to_agent"
            for tool_call in tool_calls
        )

    def build_router_branch_object_tool_call(
        self,
        *,
        parent_tool_call_id: str,
        branch_index: int,
        tool_call: Any,
    ) -> SimpleNamespace:
        tool_function = getattr(tool_call, "function", None)
        tool_name = str(getattr(tool_function, "name", "") or "")
        tool_arguments = str(getattr(tool_function, "arguments", "{}") or "{}")
        return SimpleNamespace(
            id=f"{parent_tool_call_id}_branch_{branch_index}",
            function=SimpleNamespace(
                name=tool_name,
                arguments=tool_arguments,
            ),
        )

    def execute_router_branch_object_call(
        self,
        *,
        branch_tool_call: Any,
        branch_index: int,
        depth: int,
        ChatCom,
        agent_label: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            branch_result = _handle_tool_calls(
                SimpleNamespace(content="", tool_calls=[branch_tool_call]),
                depth=depth + 1,
                ChatCom=ChatCom,
                agent_label=agent_label,
            )
            duration_seconds = max(time.perf_counter() - started, 0.0)
            duration_ms = round(duration_seconds * 1000.0, 2)
            timed_out = bool(timeout_seconds > 0.0 and duration_seconds > timeout_seconds)
            if timed_out:
                return {
                    "branch_index": int(branch_index),
                    "status": "timeout",
                    "timed_out": True,
                    "duration_ms": duration_ms,
                    "result": branch_result,
                    "error": f"branch exceeded timeout ({duration_seconds:.2f}s > {timeout_seconds:.2f}s)",
                }
            return {
                "branch_index": int(branch_index),
                "status": "completed",
                "timed_out": False,
                "duration_ms": duration_ms,
                "result": branch_result,
                "error": "",
            }
        except Exception as exc:
            duration_seconds = max(time.perf_counter() - started, 0.0)
            return {
                "branch_index": int(branch_index),
                "status": "failed",
                "timed_out": False,
                "duration_ms": round(duration_seconds * 1000.0, 2),
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _project_router_branch_object_result(self, branch_outcome: dict[str, Any]) -> Any:
        if not isinstance(branch_outcome, dict):
            return {
                "status": "failed",
                "error": "invalid branch outcome",
            }
        status = str(branch_outcome.get("status") or "").strip().lower()
        if status == "completed":
            return branch_outcome.get("result")
        return {
            "branch_index": int(branch_outcome.get("branch_index") or 0),
            "status": status or "failed",
            "timed_out": bool(branch_outcome.get("timed_out")),
            "duration_ms": float(branch_outcome.get("duration_ms") or 0.0),
            "error": str(branch_outcome.get("error") or "").strip(),
        }

    def _execute_router_branch_object_calls_hard_timeout(
        self,
        *,
        branch_calls: list[Any],
        depth: int,
        agent_label: str,
        worker_count: int,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        import multiprocessing
        import queue as std_queue

        preferred_start_method = "spawn" if os.name == "nt" else "fork"
        try:
            process_context = multiprocessing.get_context(preferred_start_method)
        except Exception:
            process_context = multiprocessing.get_context("spawn")
        result_queue: Any = process_context.Queue()
        pending_items = list(enumerate(branch_calls, start=1))
        active_items: dict[int, dict[str, Any]] = {}
        outcomes_by_index: dict[int, dict[str, Any]] = {}

        def _start_object_items() -> None:
            while len(active_items) < worker_count and pending_items:
                branch_index, branch_tool_call = pending_items.pop(0)
                tool_function = getattr(branch_tool_call, "function", None)
                branch_payload = {
                    "branch_index": int(branch_index),
                    "tool_call_id": str(getattr(branch_tool_call, "id", "") or f"parallel_route_group_branch_{branch_index}"),
                    "tool_name": str(getattr(tool_function, "name", "") or "route_to_agent"),
                    "tool_arguments": str(getattr(tool_function, "arguments", "{}") or "{}"),
                    "depth": int(depth),
                    "agent_label": str(agent_label or ""),
                }
                process = process_context.Process(
                    target=_execute_router_branch_process_object_call,
                    args=(result_queue, branch_payload),
                    daemon=True,
                )
                process.start()
                active_items[int(branch_index)] = {
                    "process": process,
                    "started_at": time.perf_counter(),
                }

        _start_object_items()

        while active_items:
            received_message = None
            try:
                received_message = result_queue.get(timeout=0.02)
            except std_queue.Empty:
                received_message = None
            except Exception:
                received_message = None

            if isinstance(received_message, dict):
                branch_index = int(received_message.get("branch_index") or 0)
                active_state = active_items.pop(branch_index, None)
                if active_state is not None:
                    process = active_state.get("process")
                    if process is not None:
                        try:
                            process.join(timeout=0.05)
                        except Exception:
                            pass
                    if not received_message.get("duration_ms"):
                        started_at = float(active_state.get("started_at") or 0.0)
                        received_message["duration_ms"] = round(max(time.perf_counter() - started_at, 0.0) * 1000.0, 2)

                if branch_index <= 0:
                    branch_index = len(outcomes_by_index) + 1
                    received_message["branch_index"] = branch_index
                received_message.setdefault("status", "failed")
                received_message["timed_out"] = bool(received_message.get("timed_out"))
                outcomes_by_index[branch_index] = dict(received_message)
                _start_object_items()
                continue

            now = time.perf_counter()
            timed_out_branch_indexes: list[int] = []
            exited_branch_indexes: list[int] = []

            for branch_index, active_state in list(active_items.items()):
                process = active_state.get("process")
                started_at = float(active_state.get("started_at") or 0.0)
                elapsed_seconds = max(now - started_at, 0.0)

                if timeout_seconds > 0.0 and elapsed_seconds > timeout_seconds:
                    timed_out_branch_indexes.append(branch_index)
                    continue

                if process is not None and not process.is_alive() and process.exitcode is not None:
                    exited_branch_indexes.append(branch_index)

            for branch_index in timed_out_branch_indexes:
                active_state = active_items.pop(branch_index, None)
                if active_state is None:
                    continue
                process = active_state.get("process")
                if process is not None and process.is_alive():
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    try:
                        process.join(timeout=0.5)
                    except Exception:
                        pass

                started_at = float(active_state.get("started_at") or 0.0)
                elapsed_seconds = max(time.perf_counter() - started_at, 0.0)
                outcomes_by_index[branch_index] = {
                    "branch_index": int(branch_index),
                    "status": "timeout",
                    "timed_out": True,
                    "duration_ms": round(elapsed_seconds * 1000.0, 2),
                    "result": None,
                    "error": f"branch exceeded hard timeout ({elapsed_seconds:.2f}s > {timeout_seconds:.2f}s)",
                }
                _start_object_items()

            for branch_index in exited_branch_indexes:
                active_state = active_items.pop(branch_index, None)
                if active_state is None:
                    continue
                process = active_state.get("process")
                exit_code = None
                if process is not None:
                    exit_code = process.exitcode
                    try:
                        process.join(timeout=0.05)
                    except Exception:
                        pass

                started_at = float(active_state.get("started_at") or 0.0)
                elapsed_seconds = max(time.perf_counter() - started_at, 0.0)
                outcomes_by_index[branch_index] = {
                    "branch_index": int(branch_index),
                    "status": "failed",
                    "timed_out": False,
                    "duration_ms": round(elapsed_seconds * 1000.0, 2),
                    "result": None,
                    "error": f"branch process exited without result (code {exit_code})",
                }
                _start_object_items()

        outcomes: list[dict[str, Any]] = []
        for branch_index in range(1, len(branch_calls) + 1):
            outcomes.append(
                dict(
                    outcomes_by_index.get(
                        branch_index,
                        {
                            "branch_index": int(branch_index),
                            "status": "failed",
                            "timed_out": False,
                            "duration_ms": 0.0,
                            "result": None,
                            "error": "branch result missing",
                        },
                    )
                )
            )

        try:
            result_queue.close()
        except Exception:
            pass
        try:
            result_queue.join_thread()
        except Exception:
            pass

        return outcomes

    def execute_router_branch_object_calls(
        self,
        *,
        tool_calls: list[Any],
        depth: int,
        ChatCom,
        agent_label: str,
    ) -> dict[str, Any]:
        execution_started = time.perf_counter()
        branch_calls: list[Any] = [
            self.build_router_branch_object_tool_call(
                parent_tool_call_id="parallel_route_group",
                branch_index=branch_index,
                tool_call=tool_call,
            )
            for branch_index, tool_call in enumerate(tool_calls, start=1)
        ]
        if not branch_calls:
            return {
                "outcomes": [],
                "results": [],
                "status_counts": {"completed": 0, "failed": 0, "timeout": 0},
                "partial_fail": False,
                "branch_count": 0,
                "worker_count": 1,
                "timeout_seconds": 0.0,
                "duration_ms": 0.0,
            }

        config = self.load_router_branch_parallel_object_config("router_branch")
        worker_count = min(
            int(config.get("worker_count") or 1),
            len(branch_calls),
        )
        timeout_seconds = float(config.get("timeout_seconds") or 0.0)
        hard_timeout_enabled = bool(config.get("hard_timeout_enabled"))
        if worker_count <= 1:
            branch_outcomes = [
                self.execute_router_branch_object_call(
                    branch_tool_call=branch_tool_call,
                    branch_index=branch_index,
                    depth=depth,
                    ChatCom=ChatCom,
                    agent_label=agent_label,
                    timeout_seconds=timeout_seconds,
                )
                for branch_index, branch_tool_call in enumerate(branch_calls, start=1)
            ]
        elif hard_timeout_enabled:
            branch_outcomes = self._execute_router_branch_object_calls_hard_timeout(
                branch_calls=branch_calls,
                depth=depth,
                agent_label=agent_label,
                worker_count=worker_count,
                timeout_seconds=timeout_seconds,
            )
        else:
            futures = []
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="alde-router-branch") as executor:
                for branch_index, branch_tool_call in enumerate(branch_calls, start=1):
                    futures.append(
                        executor.submit(
                            self.execute_router_branch_object_call,
                            branch_tool_call=branch_tool_call,
                            branch_index=branch_index,
                            depth=depth,
                            ChatCom=ChatCom,
                            agent_label=agent_label,
                            timeout_seconds=timeout_seconds,
                        )
                    )

                branch_outcomes = []
                for future_index, future in enumerate(futures, start=1):
                    try:
                        branch_outcomes.append(dict(future.result() or {}))
                    except Exception as exc:
                        branch_outcomes.append(
                            {
                                "branch_index": int(future_index),
                                "status": "failed",
                                "timed_out": False,
                                "duration_ms": 0.0,
                                "result": None,
                                "error": f"Router branch execution failed: {exc}",
                            }
                        )

        status_counts = {
            "completed": 0,
            "failed": 0,
            "timeout": 0,
        }
        for branch_outcome in branch_outcomes:
            status_name = str(branch_outcome.get("status") or "failed").strip().lower()
            if status_name not in status_counts:
                status_name = "failed"
            status_counts[status_name] += 1

        projected_results = [
            self._project_router_branch_object_result(branch_outcome)
            for branch_outcome in branch_outcomes
        ]
        partial_fail = bool(status_counts["failed"] or status_counts["timeout"])
        return {
            "outcomes": branch_outcomes,
            "results": projected_results,
            "status_counts": status_counts,
            "partial_fail": partial_fail,
            "branch_count": len(branch_outcomes),
            "worker_count": worker_count,
            "timeout_seconds": timeout_seconds,
            "hard_timeout_enabled": hard_timeout_enabled,
            "duration_ms": round(max(time.perf_counter() - execution_started, 0.0) * 1000.0, 2),
        }

    def record_router_parallel_runtime_status(self, branch_execution: dict[str, Any]) -> None:
        branch_count = int(branch_execution.get("branch_count") or 0)
        status_counts = dict(branch_execution.get("status_counts") or {})
        with self._router_parallel_status_lock:
            self._router_parallel_status["total_parallel_runs"] = int(self._router_parallel_status.get("total_parallel_runs") or 0) + 1
            self._router_parallel_status["total_parallel_branches"] = int(self._router_parallel_status.get("total_parallel_branches") or 0) + branch_count
            self._router_parallel_status["total_completed_branches"] = int(self._router_parallel_status.get("total_completed_branches") or 0) + int(status_counts.get("completed") or 0)
            self._router_parallel_status["total_failed_branches"] = int(self._router_parallel_status.get("total_failed_branches") or 0) + int(status_counts.get("failed") or 0)
            self._router_parallel_status["total_timeout_branches"] = int(self._router_parallel_status.get("total_timeout_branches") or 0) + int(status_counts.get("timeout") or 0)
            if bool(branch_execution.get("partial_fail")):
                self._router_parallel_status["total_partial_fail_runs"] = int(self._router_parallel_status.get("total_partial_fail_runs") or 0) + 1
            if bool(branch_execution.get("hard_timeout_enabled")):
                self._router_parallel_status["total_hard_timeout_runs"] = int(self._router_parallel_status.get("total_hard_timeout_runs") or 0) + 1
            self._router_parallel_status["last_run_at"] = datetime.now().isoformat(timespec="seconds")
            self._router_parallel_status["last_run_duration_ms"] = float(branch_execution.get("duration_ms") or 0.0)
            self._router_parallel_status["last_run_branch_count"] = branch_count
            self._router_parallel_status["last_run_partial_fail"] = bool(branch_execution.get("partial_fail"))
            self._router_parallel_status["last_run_hard_timeout_enabled"] = bool(branch_execution.get("hard_timeout_enabled"))
            self._router_parallel_status["last_run_status_counts"] = {
                "completed": int(status_counts.get("completed") or 0),
                "failed": int(status_counts.get("failed") or 0),
                "timeout": int(status_counts.get("timeout") or 0),
            }
            self._router_parallel_status["last_run_worker_count"] = int(branch_execution.get("worker_count") or 1)
            self._router_parallel_status["last_run_timeout_seconds"] = float(branch_execution.get("timeout_seconds") or 0.0)

    def load_router_parallel_runtime_status(self) -> dict[str, Any]:
        config = self.load_router_branch_parallel_object_config("router_branch")
        with self._router_parallel_status_lock:
            status = dict(self._router_parallel_status)
        status["config"] = {
            "parallel_enabled": bool(config.get("parallel_enabled")),
            "configured_worker_count": int(config.get("worker_count") or 1),
            "configured_timeout_seconds": float(config.get("timeout_seconds") or 0.0),
            "hard_timeout_enabled": bool(config.get("hard_timeout_enabled")),
            "max_parallel_workers": int(config.get("max_parallel_workers") or 1),
        }
        return status

    def process_object_calls(
        self,
        *,
        agent_msg: Any,
        depth: int,
        ChatCom=None,
        agent_label: str,
        workflow_session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        history = get_history()
        routing_request: dict[str, Any] | None = None
        tool_results: list[str] = []
        terminal_tool_result: str | None = None
        terminal_tool_name: str | None = None

        WORKFLOW_HISTORY_LOG_SERVICE.log_tool_call_start(
            history,
            agent_msg=agent_msg,
            agent_label=agent_label,
            workflow_session=workflow_session,
        )

        if self.should_execute_router_parallel_object_calls(
            agent_msg=agent_msg,
            agent_label=agent_label,
        ):
            route_tool_spec = get_tool_spec("route_to_agent")
            route_tool_response_required = bool(getattr(route_tool_spec, "tool_response_required", True))
            branch_execution = self.execute_router_branch_object_calls(
                tool_calls=list(agent_msg.tool_calls),
                depth=depth,
                ChatCom=ChatCom,
                agent_label=agent_label,
            )
            branch_results = list(branch_execution.get("results") or [])
            branch_outcomes = list(branch_execution.get("outcomes") or [])
            branch_status_counts = dict(branch_execution.get("status_counts") or {})
            timeout_branch_count = int(branch_status_counts.get("timeout") or 0)
            failed_branch_count = int(branch_status_counts.get("failed") or 0)
            completed_branch_count = int(branch_status_counts.get("completed") or 0)
            partial_fail = bool(branch_execution.get("partial_fail"))
            self.record_router_parallel_runtime_status(branch_execution)
            merged_result = {
                "mode": "router_parallel_branches",
                "branch_count": int(branch_execution.get("branch_count") or len(branch_results)),
                "branch_results": branch_results,
                "branch_outcomes": branch_outcomes,
                "branch_status_counts": branch_status_counts,
                "partial_fail": partial_fail,
                "timeout_branch_count": timeout_branch_count,
                "failed_branch_count": failed_branch_count,
                "completed_branch_count": completed_branch_count,
                "configured_worker_count": int(branch_execution.get("worker_count") or 1),
                "configured_timeout_seconds": float(branch_execution.get("timeout_seconds") or 0.0),
                "hard_timeout_enabled": bool(branch_execution.get("hard_timeout_enabled")),
                "run_duration_ms": float(branch_execution.get("duration_ms") or 0.0),
            }
            tool_content = self.render_object_result(merged_result)
            tool_results.append(tool_content)

            branch_payload = {
                "parallel_branches": int(branch_execution.get("branch_count") or len(branch_results)),
                "branch_mode": "router_parallel_branches",
                "partial_fail": partial_fail,
                "timeout_branch_count": timeout_branch_count,
                "failed_branch_count": failed_branch_count,
                "completed_branch_count": completed_branch_count,
                "hard_timeout_enabled": bool(branch_execution.get("hard_timeout_enabled")),
            }
            branch_failed = partial_fail
            if branch_failed:
                workflow_session = _advance_workflow_session(
                    workflow_session,
                    event_kind="state",
                    event_name="tool_failed",
                    payload={
                        "tool_name": "route_to_agent",
                        "result": tool_content,
                        **branch_payload,
                    },
                )
                workflow_history_event_kind = "state"
                workflow_history_event_name = "tool_failed"
                workflow_history_payload = {
                    "tool_name": "route_to_agent",
                    "result": tool_content,
                    **branch_payload,
                }
            else:
                workflow_session = _advance_workflow_session(
                    workflow_session,
                    event_kind="tool",
                    event_name="route_to_agent",
                    payload=branch_payload,
                )
                workflow_session = _advance_workflow_session(
                    workflow_session,
                    event_kind="state",
                    event_name="tool_complete",
                    payload={
                        "tool_name": "route_to_agent",
                        "result": tool_content,
                        **branch_payload,
                    },
                )
                workflow_history_event_kind = "tool"
                workflow_history_event_name = "route_to_agent"
                workflow_history_payload = branch_payload

            terminal_tool_result = tool_content
            terminal_tool_name = "route_to_agent"

            WORKFLOW_HISTORY_LOG_SERVICE.log_tool_result(
                history,
                tool_content=tool_content,
                agent_label=agent_label,
                tool_call_id="parallel_route_group",
                tool_name="route_to_agent",
                workflow_session=workflow_session,
                event_kind=workflow_history_event_kind,
                event_name=workflow_history_event_name,
                payload=workflow_history_payload,
                tool_response_required=route_tool_response_required,
            )

            return {
                "workflow_session": workflow_session,
                "routing_request": routing_request,
                "tool_results": tool_results,
                "terminal_tool_result": terminal_tool_result,
                "terminal_tool_name": terminal_tool_name,
            }

        for tool_call in agent_msg.tool_calls:
            tool_name = tool_call.function.name
            tool_spec = get_tool_spec(tool_name)
            tool_response_required = bool(getattr(tool_spec, 'tool_response_required', True))
            direct_final_result = bool(getattr(tool_spec, 'final_result', False))
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except Exception:
                args = {}

            result, request = execute_tool(tool_name, args, tool_call.id, source_agent_label=agent_label)

            auto_handoff_results: list[Any] = []
            auto_handoff_messages = _extract_tool_handoff_messages(result)
            if auto_handoff_messages and request is None:
                auto_handoff_results = self.execute_handoff_object_messages(
                    parent_tool_call_id=str(getattr(tool_call, "id", "call")),
                    handoff_messages=auto_handoff_messages,
                    depth=depth,
                    ChatCom=ChatCom,
                    agent_label=agent_label,
                )
                if isinstance(result, dict):
                    result = dict(result)
                    result["handoff_results"] = auto_handoff_results
                refreshed_workflow_session = _create_workflow_session(agent_label)
                if refreshed_workflow_session is not None:
                    workflow_session = refreshed_workflow_session

            tool_content = self.render_object_result(result)
            tool_results.append(tool_content)
            tool_failed = _message_indicates_failure(tool_content) and request is None
            recoverable_direct_failure = bool(tool_failed and direct_final_result)

            if tool_failed and not recoverable_direct_failure:
                workflow_session = _advance_workflow_session(
                    workflow_session,
                    event_kind="state",
                    event_name="tool_failed",
                    payload={
                        "tool_name": normalize_tool_name(str(tool_name)),
                        "result": tool_content,
                    },
                )
                workflow_history_event_kind = 'state'
                workflow_history_event_name = 'tool_failed'
                workflow_history_payload: dict[str, Any] = {
                    'tool_name': normalize_tool_name(str(tool_name)),
                    'result': tool_content,
                }
            else:
                workflow_session = _advance_workflow_session(
                    workflow_session,
                    event_kind="tool",
                    event_name=str(tool_name),
                    payload=args,
                )
                if not recoverable_direct_failure:
                    workflow_session = _advance_workflow_session(
                        workflow_session,
                        event_kind="state",
                        event_name="tool_complete",
                        payload={
                            **({"tool_name": normalize_tool_name(str(tool_name)), "result": tool_content}),
                            **(args if isinstance(args, dict) else {}),
                        },
                    )
                workflow_history_event_kind = 'tool'
                workflow_history_event_name = str(tool_name)
                if recoverable_direct_failure:
                    workflow_history_payload = {
                        **(args if isinstance(args, dict) else {}),
                        "recoverable_failure": True,
                        "result": tool_content,
                    }
                else:
                    workflow_history_payload = args

            if workflow_session and workflow_session.get("terminal") and not recoverable_direct_failure and request is None:
                terminal_tool_result = tool_content
                terminal_tool_name = normalize_tool_name(str(tool_name))
            elif direct_final_result and request is None and not tool_failed:
                terminal_tool_result = tool_content
                terminal_tool_name = normalize_tool_name(str(tool_name))

            WORKFLOW_HISTORY_LOG_SERVICE.log_tool_result(
                history,
                tool_content=tool_content,
                agent_label=agent_label,
                tool_call_id=getattr(tool_call, 'id', None),
                tool_name=tool_name,
                workflow_session=workflow_session,
                event_kind=workflow_history_event_kind,
                event_name=workflow_history_event_name,
                payload=workflow_history_payload,
                tool_response_required=tool_response_required,
            )
            if isinstance(request, dict) and request.get('messages') is not None:
                routing_request = request

        return {
            'workflow_session': workflow_session,
            'routing_request': routing_request,
            'tool_results': tool_results,
            'terminal_tool_result': terminal_tool_result,
            'terminal_tool_name': terminal_tool_name,
        }


TOOL_CALL_EXECUTION_SERVICE = ToolCallExecutionService()


def get_router_parallel_runtime_status() -> dict[str, Any]:
    return TOOL_CALL_EXECUTION_SERVICE.load_router_parallel_runtime_status()


class ToolCallFollowupService:
    def _is_routing_status_result(self, tool_results: list[str]) -> bool:
        if len(tool_results) != 1:
            return False
        text = str(tool_results[0] or '').strip().lower()
        return text.startswith('routing to ')

    def ensure_object_followup_messages(
        self,
        *,
        followup_messages: list[dict[str, Any]],
        tool_results: list[str],
    ) -> list[dict[str, Any]]:
        if self._is_routing_status_result(tool_results):
            return followup_messages

        raw_tool_results = "\n".join(
            str(tool_result)
            for tool_result in tool_results
            if str(tool_result or '').strip()
        ).strip()
        if not raw_tool_results:
            return followup_messages

        has_tool_message = any(
            isinstance(message, dict) and str(message.get('role') or '').strip().lower() == 'tool'
            for message in followup_messages
        )
        if has_tool_message:
            return followup_messages

        if followup_messages:
            latest_content = str((followup_messages[-1] or {}).get('content') or '')
            if raw_tool_results and raw_tool_results in latest_content:
                return followup_messages

        return list(followup_messages) + [{"role": "user", "content": raw_tool_results}]

    def build_object_request(
        self,
        *,
        history: Any,
        routing_request: dict[str, Any] | None,
        agent_label: str,
    ) -> dict[str, Any]:
        if routing_request is not None:
            followup_messages = ROUTING_REQUEST_VIEW_SERVICE.load_messages(routing_request)
            if ROUTING_REQUEST_VIEW_SERVICE.load_include_history(routing_request):
                followup_messages.extend(
                    history._insert(
                        tool=True,
                        f_depth=ROUTING_REQUEST_VIEW_SERVICE.load_history_depth(routing_request, default=15),
                    )
                )
            followup_tools = ROUTING_REQUEST_VIEW_SERVICE.load_tools(routing_request)
            followup_model = ROUTING_REQUEST_VIEW_SERVICE.load_model(routing_request, model)
            return {
                'messages': followup_messages,
                'tools': followup_tools,
                'model': followup_model,
            }

        current_agent_config = _get_runtime_agent_config(agent_label)
        current_history_policy = _agent_history_policy(agent_label)
        followup_tools = get_agent_runtime_tools(agent_label)
        followup_model = current_agent_config.get('model') or model
        followup_messages = history._insert(
            tool=True,
            f_depth=int(current_history_policy.get('followup_history_depth') or 15),
        )
        sys_text = current_agent_config.get('system')
        if sys_text and (not followup_messages or followup_messages[0].get('role') != 'system'):
            followup_messages = [{"role": "system", "content": sys_text}] + followup_messages
        return {
            'messages': followup_messages,
            'tools': followup_tools,
            'model': followup_model,
        }

    def execute_object_followup(
        self,
        *,
        history: Any,
        routing_request: dict[str, Any] | None,
        tool_results: list[str],
        depth: int,
        ChatCom=None,
        agent_label: str,
        workflow_session: dict[str, Any] | None,
    ) -> Any:
        try:
            from . import ChatComE  # type: ignore
        except Exception:
            import os
            import sys

            _pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _pkg_parent not in sys.path:
                sys.path.insert(0, _pkg_parent)
            from alde.chat_completion import ChatComE, ChatCompletion  # type: ignore

        request = self.build_object_request(
            history=history,
            routing_request=routing_request,
            agent_label=agent_label,
        )
        followup_tools = list(request.get('tools') or [])
        selected_job_name = ROUTING_HANDOFF_VIEW_SERVICE.load_job_name(routing_request)
        if (
            selected_job_name == 'job_posting_parser'
            and tool_results
            and not self._is_routing_status_result(tool_results)
        ):
            followup_tools = []

        followup_messages = self.ensure_object_followup_messages(
            followup_messages=list(request.get('messages') or []),
            tool_results=tool_results,
        )
        if not followup_messages:
            followup_messages = [{"role": "user", "content": ""}]

        c = ChatComE(
            _model=request.get('model') or model,
            _messages=followup_messages,
            tools=followup_tools,
            tool_choice='auto'
        )

        try:
            resp = c._response()
        except Exception as exc:
            err = f"Follow-up model call failed: {exc}"
            _maybe_apply_routing_result_postprocess(
                routing_request,
                result_text=err,
                succeeded=False,
            )
            workflow_session = _advance_workflow_session(
                workflow_session,
                event_kind='state',
                event_name='model_failed',
                payload={'error': str(exc)},
            )
            WORKFLOW_HISTORY_LOG_SERVICE.log_model_failure(
                history,
                err=err,
                agent_label=agent_label,
                workflow_session=workflow_session,
            )
            return err

        return ASSISTANT_RESPONSE_SERVICE.handle_object_response(
            resp=resp,
            routing_request=routing_request,
            history=history,
            depth=depth,
            ChatCom=ChatCom,
            agent_label=agent_label,
            workflow_session=workflow_session,
            tool_results=tool_results,
        )


class AssistantResponseService:
    def resolve_agent_label(self, routing_request: dict[str, Any] | None, agent_label: str) -> str:
        return ROUTING_REQUEST_VIEW_SERVICE.load_agent_label(routing_request, fallback=agent_label) or '_xplaner_xrouter'

    def resolve_object_label(self, routing_request: dict[str, Any] | None, agent_label: str) -> str:
        return self.resolve_agent_label(routing_request, agent_label)

    def _latest_tool_failure(self, tool_results: list[str]) -> str | None:
        for tool_result in reversed(tool_results or []):
            text = str(tool_result or '').strip()
            if text and _message_indicates_failure(text):
                return text
        return None

    def _response_acknowledges_failure(self, text: str) -> bool:
        normalized = str(text or '').strip().lower()
        if not normalized:
            return False
        if _message_indicates_failure(normalized):
            return True

        failure_hints = (
            'error',
            'fehler',
            'nicht gefunden',
            'missing',
            'invalid',
            'dateipfad',
            'path',
            'unable',
            'cannot',
            "can't",
            'konnte nicht',
            'uebergib',
            'provide',
            'required',
            'erforderlich',
        )
        return any(hint in normalized for hint in failure_hints)

    def _coerce_text_against_tool_failures(self, *, text: str, tool_results: list[str]) -> str:
        latest_failure = self._latest_tool_failure(tool_results)
        if not latest_failure:
            return text
        if self._response_acknowledges_failure(text):
            return text
        return latest_failure

    def _apply_postprocess_handoffs(
        self,
        *,
        postprocess_result: dict[str, Any] | None,
        depth: int,
        ChatCom,
        response_agent_label: str,
    ) -> str | None:
        if not isinstance(postprocess_result, dict):
            return None

        handoff_messages = postprocess_result.get("handoff_messages") or []
        if not isinstance(handoff_messages, list) or not handoff_messages:
            return None

        handoff_results = TOOL_CALL_EXECUTION_SERVICE.execute_handoff_object_messages(
            parent_tool_call_id=f"postprocess_{normalize_agent_label(response_agent_label or '') or 'agent'}",
            handoff_messages=[dict(item or {}) for item in handoff_messages if isinstance(item, dict)],
            depth=depth,
            ChatCom=ChatCom,
            agent_label=response_agent_label,
        )
        postprocess_result["handoff_results"] = handoff_results

        for handoff_result in reversed(handoff_results):
            text_value = str(handoff_result or "").strip()
            if text_value:
                return text_value
        return None

    def _handle_nested_object_calls(
        self,
        *,
        message: Any,
        routing_request: dict[str, Any] | None,
        history: Any,
        depth: int,
        ChatCom,
        response_agent_label: str,
        workflow_session: dict[str, Any] | None,
    ) -> Any:
        if not getattr(message, 'tool_calls', None):
            return None

        next_workflow_session = workflow_session
        if not next_workflow_session or normalize_agent_label(str(next_workflow_session.get("agent_label") or "")) != normalize_agent_label(response_agent_label or ""):
            next_workflow_session = _create_workflow_session(
                response_agent_label,
                routing_request=routing_request,
            )
        rec = _handle_tool_calls(
            message,
            depth + 1,
            ChatCom=ChatCom,
            agent_label=response_agent_label,
            workflow_session=next_workflow_session,
            routing_request=routing_request,
        )
        if rec is None or not str(rec).strip():
            return None

        routed_event_name = 'routed_agent_failed' if _message_indicates_failure(rec) else 'routed_agent_complete'
        postprocess_result = _maybe_apply_routing_result_postprocess(
            routing_request,
            result_text=rec,
            succeeded=routed_event_name == 'routed_agent_complete',
        )
        if isinstance(postprocess_result, dict) and isinstance(postprocess_result.get('result_text'), str):
            rec = postprocess_result.get('result_text')
        chained_result = self._apply_postprocess_handoffs(
            postprocess_result=postprocess_result if isinstance(postprocess_result, dict) else None,
            depth=depth,
            ChatCom=ChatCom,
            response_agent_label=response_agent_label,
        )
        if isinstance(chained_result, str) and chained_result.strip():
            rec = chained_result
        _advance_workflow_session(
            workflow_session,
            event_kind='state',
            event_name=routed_event_name,
            payload={
                'target_agent': response_agent_label,
                'result': str(rec),
            },
        )
        return rec

    def _handle_text_response(
        self,
        *,
        text: str,
        routing_request: dict[str, Any] | None,
        history: Any,
        response_agent_label: str,
        workflow_session: dict[str, Any] | None,
        tool_results: list[str],
    ) -> str:
        text = self._coerce_text_against_tool_failures(
            text=text,
            tool_results=tool_results,
        )
        completion_event_name = 'followup_complete'
        completion_payload: dict[str, Any] = {}
        if ROUTING_REQUEST_VIEW_SERVICE.has_object_agent(routing_request):
            completion_event_name = 'routed_agent_failed' if _message_indicates_failure(text) else 'routed_agent_complete'
            postprocess_result = _maybe_apply_routing_result_postprocess(
                routing_request,
                result_text=text,
                succeeded=completion_event_name == 'routed_agent_complete',
            )
            if isinstance(postprocess_result, dict) and isinstance(postprocess_result.get('result_text'), str):
                text = postprocess_result.get('result_text')
            chained_result = self._apply_postprocess_handoffs(
                postprocess_result=postprocess_result if isinstance(postprocess_result, dict) else None,
                depth=0,
                ChatCom=None,
                response_agent_label=response_agent_label,
            )
            if isinstance(chained_result, str) and chained_result.strip():
                text = chained_result
            completion_payload = {
                'target_agent': response_agent_label,
                'result': text,
            }
        workflow_session = _advance_workflow_session(
            workflow_session,
            event_kind='state',
            event_name=completion_event_name,
            payload=completion_payload,
        )
        WORKFLOW_HISTORY_LOG_SERVICE.log_assistant_response(
            history,
            text=text,
            response_agent_label=response_agent_label,
            workflow_session=workflow_session,
            event_name=completion_event_name,
            payload=completion_payload,
        )
        return text

    def handle_assistant_response(
        self,
        *,
        resp: Any,
        routing_request: dict[str, Any] | None,
        history: Any,
        depth: int,
        ChatCom,
        agent_label: str,
        workflow_session: dict[str, Any] | None,
        tool_results: list[str],
    ) -> Any:
        response_agent_label = self.resolve_object_label(routing_request, agent_label)

        if getattr(resp, 'choices', None):
            message = resp.choices[0].message
            nested_result = self._handle_nested_object_calls(
                message=message,
                routing_request=routing_request,
                history=history,
                depth=depth,
                ChatCom=ChatCom,
                response_agent_label=response_agent_label,
                workflow_session=workflow_session,
            )
            if nested_result is not None:
                return nested_result

            text = (getattr(message, 'content', '') or '').strip()
            if text:
                return self._handle_text_response(
                    text=text,
                    routing_request=routing_request,
                    history=history,
                    response_agent_label=response_agent_label,
                    workflow_session=workflow_session,
                    tool_results=tool_results,
                )

        return "\n".join(tool_results).strip() or None

    def handle_object_response(
        self,
        *,
        resp: Any,
        routing_request: dict[str, Any] | None,
        history: Any,
        depth: int,
        ChatCom,
        agent_label: str,
        workflow_session: dict[str, Any] | None,
        tool_results: list[str],
    ) -> Any:
        return self.handle_assistant_response(
            resp=resp,
            routing_request=routing_request,
            history=history,
            depth=depth,
            ChatCom=ChatCom,
            agent_label=agent_label,
            workflow_session=workflow_session,
            tool_results=tool_results,
        )


ASSISTANT_RESPONSE_SERVICE = AssistantResponseService()


TOOL_CALL_FOLLOWUP_SERVICE = ToolCallFollowupService()


def _maybe_apply_routing_result_postprocess(
    routing_request: dict[str, Any] | None,
    *,
    result_text: Any,
    succeeded: bool,
) -> dict[str, Any] | None:
    return ROUTING_RESULT_POSTPROCESS_SERVICE.apply_object_result(
        routing_request,
        result_text=result_text,
        succeeded=succeeded,
    )

# ============================================================================
# Handle Tool Calls - uses execute_tool with bound callbacks
# ============================================================================

def _handle_tool_calls(agent_msg, depth: int = 0,
                        ChatCom = None,
                       agent_label: str ="",
                       workflow_session: dict[str, Any] | None = None,
                       routing_request: dict[str, Any] | None = None) -> Any:
    """Execute tool calls and continue the conversation."""
    # Ensure we have a ChatHistory instance available (lazy import)
    history = get_history()
    if depth >= _MAX_TOOL_DEPTH:
        warning = "Aborting: tool-call depth exceeded."
        WORKFLOW_HISTORY_LOG_SERVICE.log_depth_warning(history, warning)
        return warning

    if not hasattr(agent_msg, 'tool_calls') or not agent_msg.tool_calls:
        return getattr(agent_msg, 'content', None) or None
    if workflow_session is None:
        workflow_session = _create_workflow_session(agent_label)
    execution_result = TOOL_CALL_EXECUTION_SERVICE.process_object_calls(
        agent_msg=agent_msg,
        depth=depth,
        ChatCom=ChatCom,
        agent_label=agent_label,
        workflow_session=workflow_session,
    )
    workflow_session = execution_result.get('workflow_session')
    next_routing_request = execution_result.get('routing_request')
    if next_routing_request is None:
        next_routing_request = routing_request
    tool_results = list(execution_result.get('tool_results') or [])
    terminal_tool_result = execution_result.get('terminal_tool_result')
    terminal_tool_name = execution_result.get('terminal_tool_name')
    agent_label = agent_label or '_xplaner_xrouter'

    if terminal_tool_result is not None and next_routing_request is None:
        WORKFLOW_HISTORY_LOG_SERVICE.log_assistant_response(
            history,
            text=str(terminal_tool_result),
            response_agent_label=agent_label,
            workflow_session=workflow_session,
            event_name='tool_result_final',
            payload={
                'direct_tool_result': True,
                'tool_name': terminal_tool_name,
                'result': str(terminal_tool_result),
            },
        )
        return terminal_tool_result

    return TOOL_CALL_FOLLOWUP_SERVICE.execute_object_followup(
        history=history,
        routing_request=next_routing_request,
        tool_results=tool_results,
        depth=depth,
        ChatCom=ChatCom,
        agent_label=agent_label,
        workflow_session=workflow_session,
    )
