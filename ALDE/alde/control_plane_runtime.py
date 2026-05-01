from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import importlib
import json
import os
import socket
import sys
import uuid
from urllib.parse import urlparse


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _candidate_path in (_REPO_ROOT, _PACKAGE_ROOT):
    _candidate_text = str(_candidate_path)
    if _candidate_text not in sys.path:
        sys.path.insert(0, _candidate_text)

_THIS_MODULE = sys.modules.get(__name__)
if _THIS_MODULE is not None:
    if __name__.startswith("ALDE_Projekt.ALDE.alde"):
        sys.modules.setdefault("alde.control_plane_runtime", _THIS_MODULE)
    elif __name__.startswith("alde."):
        sys.modules.setdefault("ALDE_Projekt.ALDE.alde.control_plane_runtime", _THIS_MODULE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _json_safe_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        if isinstance(value, (dict, list, tuple)):
            try:
                return json.loads(json.dumps(str(value), ensure_ascii=False))
            except Exception:
                return str(value)
        return value


def _normalized_projection_value(value: Any, *, default: str = "unknown") -> str:
    raw_value = _safe_str(value).lower()
    if not raw_value:
        return default
    normalized = "".join(character if character.isalnum() else "_" for character in raw_value).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized or default


def _count_projection_values(recent_items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in recent_items:
        if not isinstance(item, dict):
            continue
        normalized_value = _normalized_projection_value(item.get(key), default="")
        if not normalized_value:
            continue
        counts[normalized_value] = counts.get(normalized_value, 0) + 1
    return dict(sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])))


def _build_recent_item_summary(recent_items: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_items = [dict(item) for item in recent_items if isinstance(item, dict)]
    status_counts = _count_projection_values(normalized_items, "status")
    source_counts = _count_projection_values(normalized_items, "source")
    audit_type_counts = _count_projection_values(normalized_items, "audit_type")
    action_group_counts = _count_projection_values(normalized_items, "action_group")
    latest_item = dict(normalized_items[0]) if normalized_items else {}
    return {
        "count": len(normalized_items),
        "status_counts": status_counts,
        "source_counts": source_counts,
        "audit_type_counts": audit_type_counts,
        "action_group_counts": action_group_counts,
        "latest_item": latest_item,
        "failure_count": int(status_counts.get("fail") or 0),
        "pass_count": int(status_counts.get("pass") or 0),
        "info_count": int(status_counts.get("info") or 0),
    }


def _build_recent_item_filters(recent_items: list[dict[str, Any]]) -> dict[str, list[str]]:
    summary = _build_recent_item_summary(recent_items)
    return {
        "statuses": sorted(summary["status_counts"].keys()),
        "sources": sorted(summary["source_counts"].keys()),
        "audit_types": sorted(summary["audit_type_counts"].keys()),
        "action_groups": sorted(summary["action_group_counts"].keys()),
    }


def _build_recent_projection_item(
    *,
    timestamp: str | None,
    title: str,
    summary: str,
    source: str,
    status: str = "info",
    audit_type: str | None = None,
    action_group: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": _safe_str(timestamp) or _utc_now_iso(),
        "title": _safe_str(title) or "activity",
        "summary": _safe_str(summary) or _safe_str(title) or "activity",
        "source": _safe_str(source) or "control_plane",
        "status": _safe_str(status).lower() or "info",
        "audit_type": _normalized_projection_value(audit_type or title or "activity", default="activity"),
        "action_group": _normalized_projection_value(action_group or source or "control_plane", default="control_plane"),
        "metadata": _json_safe_copy(metadata or {}),
    }


def _build_projection_snapshot(
    *,
    snapshot_kind: str,
    healthy: bool,
    alerts: list[str] | None = None,
    summary_metrics: dict[str, Any] | None = None,
    recent_items: list[dict[str, Any]] | None = None,
    detail_rows: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    normalized_alerts = [str(item) for item in (alerts or []) if str(item)]
    normalized_recent_items = [dict(item) for item in (recent_items or []) if isinstance(item, dict)]
    normalized_detail_rows = [dict(item) for item in (detail_rows or []) if isinstance(item, dict)]
    recent_item_summary = _build_recent_item_summary(normalized_recent_items)
    recent_item_filters = _build_recent_item_filters(normalized_recent_items)
    return {
        "generated_at": _safe_str(generated_at) or _utc_now_iso(),
        "snapshot_kind": _safe_str(snapshot_kind) or "control_plane",
        "healthy": bool(healthy),
        "alerts": normalized_alerts,
        "attention_count": len(normalized_alerts),
        "summary_metrics": dict(summary_metrics or {}),
        "recent_items": normalized_recent_items,
        "recent_item_count": len(normalized_recent_items),
        "recent_item_summary": recent_item_summary,
        "recent_item_filters": recent_item_filters,
        "detail_rows": normalized_detail_rows,
        **extra,
    }


def _module_candidates(module_name: str) -> list[str]:
    candidates: list[str] = []
    if __package__:
        candidates.append(f"{__package__}.{module_name}")
    candidates.extend([f"alde.{module_name}", f"ALDE_Projekt.ALDE.alde.{module_name}", f"ALDE.alde.{module_name}", module_name])
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate_name = _safe_str(candidate)
        if not candidate_name or candidate_name in seen:
            continue
        seen.add(candidate_name)
        deduped.append(candidate_name)
    return deduped


def _load_symbol(module_name: str, symbol_name: str) -> Any:
    last_error: Exception | None = None
    for candidate in _module_candidates(module_name):
        try:
            module = importlib.import_module(candidate)
            return getattr(module, symbol_name)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ImportError(f"Unable to load {symbol_name} from {module_name}")


class RuntimeProjectionService:
    FAILURE_EVENT_NAMES = {"tool_failed", "model_failed", "routed_agent_failed", "workflow_failed"}
    COMPLETION_EVENT_NAMES = {"tool_complete", "followup_complete", "routed_agent_complete", "workflow_complete"}
    RETRY_EVENT_NAMES = {"retry_requested", "retry_exhausted"}

    def load_generated_root(self, base_dir: str | None = None) -> Path:
        return Path(base_dir) if base_dir else Path(__file__).resolve().parents[1] / "AppData" / "generated"

    def load_learning_entries(self, base_dir: str | None = None) -> list[dict[str, Any]]:
        target_path = self.load_generated_root(base_dir) / "learning_events.jsonl"
        if not target_path.is_file():
            return []

        entries: list[dict[str, Any]] = []
        try:
            with open(target_path, "r", encoding="utf-8") as event_file:
                for raw_line in event_file:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict):
                        entries.append(entry)
        except OSError:
            return []
        return entries

    def load_history_entries(self, history_entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if isinstance(history_entries, list):
            return self._normalize_history_entries(history_entries)

        try:
            ChatHistory = _load_symbol("agents_ccomp", "ChatHistory")
        except Exception:
            return []

        try:
            loaded_history = getattr(ChatHistory, "_history_", None) or ChatHistory._load()
        except Exception:
            return []
        return self._normalize_history_entries(loaded_history)

    def load_stored_runtime_events(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            load_runtime_events = _load_symbol("agents_event_store", "load_runtime_events")
        except Exception:
            return []

        try:
            loaded_events = load_runtime_events(base_dir=base_dir, session_id=session_id)
        except Exception:
            return []

        return [event_object for event_object in loaded_events if isinstance(event_object, dict)]

    def _normalize_history_entries(self, raw_entries: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(raw_entries, list):
            return normalized
        for item in raw_entries:
            if isinstance(item, dict):
                normalized.append(item)
                continue
            if isinstance(item, list):
                normalized.extend(entry for entry in item if isinstance(entry, dict))
        return normalized

    def _history_value(self, history_entry: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in history_entry and history_entry.get(key) not in (None, ""):
                return history_entry.get(key)
        return None

    def _workflow_data(self, history_entry: dict[str, Any]) -> dict[str, Any]:
        data_object = history_entry.get("data") if isinstance(history_entry.get("data"), dict) else {}
        return data_object.get("workflow") if isinstance(data_object.get("workflow"), dict) else {}

    def _workflow_snapshot(self, workflow_object: dict[str, Any]) -> dict[str, Any]:
        return workflow_object.get("snapshot") if isinstance(workflow_object.get("snapshot"), dict) else {}

    def _effective_event(self, workflow_object: dict[str, Any], snapshot_object: dict[str, Any]) -> dict[str, Any]:
        workflow_event = workflow_object.get("event") if isinstance(workflow_object.get("event"), dict) else {}
        snapshot_event = snapshot_object.get("event") if isinstance(snapshot_object.get("event"), dict) else {}
        payload = workflow_event.get("payload") if isinstance(workflow_event.get("payload"), dict) else {}
        snapshot_payload = snapshot_event.get("payload") if isinstance(snapshot_event.get("payload"), dict) else {}
        return {
            "kind": workflow_event.get("kind") or snapshot_event.get("kind"),
            "name": workflow_event.get("name") or snapshot_event.get("name"),
            "payload": dict(payload or snapshot_payload or {}),
            "tool_name": payload.get("tool_name") or snapshot_payload.get("tool_name") or snapshot_event.get("tool_name"),
            "target_agent": payload.get("target_agent") or snapshot_payload.get("target_agent") or snapshot_event.get("target_agent"),
            "correlation_id": payload.get("correlation_id") or snapshot_payload.get("correlation_id") or snapshot_event.get("correlation_id"),
            "action": payload.get("action") or snapshot_payload.get("action") or snapshot_event.get("action"),
        }

    def _event_family(self, *, event_kind: str | None, event_name: str | None, target_agent: str | None) -> str:
        normalized_name = _safe_str(event_name)
        if normalized_name in self.RETRY_EVENT_NAMES:
            return "retry"
        if normalized_name in self.FAILURE_EVENT_NAMES:
            return "failure"
        if normalized_name in self.COMPLETION_EVENT_NAMES:
            return "completion"
        if target_agent:
            return "handoff"
        if _safe_str(event_kind) == "tool":
            return "tool"
        return "state"

    def _event_status(self, *, event_family: str, event_name: str | None, phase: str, role: str) -> str:
        normalized_name = _safe_str(event_name)
        normalized_phase = _safe_str(phase)
        if event_family == "failure":
            return "failed"
        if event_family == "completion":
            return "completed"
        if event_family == "retry" and normalized_name == "retry_requested":
            return "scheduled"
        if event_family == "retry" and normalized_name == "retry_exhausted":
            return "exhausted"
        if role == "tool":
            return "completed"
        if normalized_phase == "tool_call_start":
            return "requested"
        if normalized_phase == "tool_result":
            return "completed"
        return "active"

    def _build_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        event_id: str,
        timestamp: str,
        session_id: str | None = None,
        correlation_id: str | None = None,
        agent_label: str | None = None,
        workflow_name: str | None = None,
    ) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "event_id": event_id,
            "timestamp": timestamp,
            "payload": dict(payload or {}),
            "session_id": _safe_str(session_id) or None,
            "correlation_id": _safe_str(correlation_id) or None,
            "agent_label": _safe_str(agent_label) or None,
            "workflow_name": _safe_str(workflow_name) or None,
        }

    def _project_learning_entry(
        self,
        learning_entry: dict[str, Any],
        *,
        query_context_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        event_type = _safe_str(learning_entry.get("event_type"))
        payload = learning_entry.get("payload") if isinstance(learning_entry.get("payload"), dict) else {}
        timestamp = _safe_str(payload.get("timestamp") or learning_entry.get("timestamp")) or _utc_now_iso()

        if event_type == "query":
            event_id = _safe_str(payload.get("event_id")) or f"query:{uuid.uuid4()}"
            metadata = dict(payload)
            tool_name = _safe_str(metadata.pop("tool", metadata.pop("tool_name", "")))
            query_text = _safe_str(metadata.pop("query_text", ""))
            event_object = self._build_event(
                event_type="query",
                event_id=event_id,
                timestamp=timestamp,
                session_id=_safe_str(payload.get("session_id")) or None,
                correlation_id=event_id,
                agent_label=_safe_str(payload.get("agent")) or None,
                workflow_name=_safe_str(payload.get("workflow_name")) or None,
                payload={
                    "query_text": query_text,
                    "tool_name": tool_name,
                    "metadata": metadata,
                },
            )
            query_context_by_id[event_id] = {
                "session_id": event_object.get("session_id"),
                "agent_label": event_object.get("agent_label"),
                "correlation_id": event_object.get("correlation_id"),
            }
            return [event_object]

        if event_type == "outcome":
            query_event_id = _safe_str(payload.get("query_event_id"))
            query_context = dict(query_context_by_id.get(query_event_id) or {})
            metadata = dict(payload)
            tool_name = _safe_str(metadata.pop("tool", metadata.pop("tool_name", "")))
            success = bool(metadata.get("success"))
            return [
                self._build_event(
                    event_type="outcome",
                    event_id=_safe_str(payload.get("event_id")) or f"outcome:{uuid.uuid4()}",
                    timestamp=timestamp,
                    session_id=_safe_str(payload.get("session_id") or query_context.get("session_id")) or None,
                    correlation_id=query_event_id or _safe_str(query_context.get("correlation_id")) or None,
                    agent_label=_safe_str(payload.get("agent") or query_context.get("agent_label")) or None,
                    workflow_name=_safe_str(payload.get("workflow_name")) or None,
                    payload={
                        "query_event_id": query_event_id,
                        "tool_name": tool_name,
                        "success": success,
                        "latency_ms": payload.get("latency_ms"),
                        "reward": payload.get("reward"),
                        "result_count": payload.get("result_count"),
                        "metadata": metadata,
                    },
                )
            ]

        return []

    def _project_history_entry(self, history_entry: dict[str, Any]) -> list[dict[str, Any]]:
        workflow_object = self._workflow_data(history_entry)
        if not workflow_object:
            return []

        snapshot_object = self._workflow_snapshot(workflow_object)
        actor_object = snapshot_object.get("actor") if isinstance(snapshot_object.get("actor"), dict) else {}
        retry_object = workflow_object.get("retry") if isinstance(workflow_object.get("retry"), dict) else {}
        effective_event = self._effective_event(workflow_object, snapshot_object)
        workflow_payload = effective_event.get("payload") if isinstance(effective_event.get("payload"), dict) else {}
        phase = _safe_str(workflow_object.get("phase") or self._history_value(history_entry, "thread-name", "thread_name")) or "history"
        timestamp = _safe_str(self._history_value(history_entry, "time", "timestamp", "date")) or _utc_now_iso()
        message_id = _safe_str(self._history_value(history_entry, "message-id", "message_id")) or str(uuid.uuid4())
        session_id = _safe_str(workflow_object.get("scope_key") or self._history_value(history_entry, "thread-id", "thread_id")) or None
        agent_label = _safe_str(
            workflow_object.get("agent_label")
            or self._history_value(history_entry, "assistant-name", "assistant_name", "name")
        ) or None
        workflow_name = _safe_str(workflow_object.get("workflow_name")) or None
        event_kind = _safe_str(effective_event.get("kind")) or None
        event_name = _safe_str(effective_event.get("name")) or None
        target_agent = _safe_str(effective_event.get("target_agent") or workflow_payload.get("target_agent")) or None
        correlation_id = _safe_str(effective_event.get("correlation_id") or workflow_payload.get("correlation_id")) or None
        action_name = _safe_str(effective_event.get("action") or workflow_payload.get("action")) or None
        role = _safe_str(history_entry.get("role"))
        event_family = self._event_family(event_kind=event_kind, event_name=event_name, target_agent=target_agent)
        event_status = self._event_status(event_family=event_family, event_name=event_name, phase=phase, role=role)

        projected_events: list[dict[str, Any]] = []

        current_state = _safe_str(workflow_object.get("current_state"))
        if current_state:
            projected_events.append(
                self._build_event(
                    event_type="workflow_state",
                    event_id=f"history:{message_id}:workflow_state",
                    timestamp=timestamp,
                    session_id=session_id,
                    correlation_id=correlation_id or event_name,
                    agent_label=agent_label,
                    workflow_name=workflow_name,
                    payload={
                        "state_name": current_state,
                        "state_phase": phase,
                        "metadata": {
                            "source": "chat_history",
                            "event_kind": event_kind,
                            "event_name": event_name,
                            "event_payload": dict(workflow_payload),
                            "thread_id": self._history_value(history_entry, "thread-id", "thread_id"),
                            "thread_name": self._history_value(history_entry, "thread-name", "thread_name"),
                            "terminal": bool(workflow_object.get("terminal")),
                            "event_family": event_family,
                            "event_status": event_status,
                            "actor_kind": actor_object.get("kind"),
                            "actor_name": actor_object.get("name"),
                            "tool_name": effective_event.get("tool_name"),
                            "target_agent": target_agent,
                            "correlation_id": correlation_id,
                            "action": action_name,
                            "retry": dict(retry_object),
                        },
                    },
                )
            )

        tool_calls = history_entry.get("tool_calls") if isinstance(history_entry.get("tool_calls"), list) else []
        for index, tool_call in enumerate(tool_calls):
            function_object = tool_call.get("function") if isinstance(tool_call, dict) and isinstance(tool_call.get("function"), dict) else {}
            tool_name = _safe_str(function_object.get("name") or (tool_call.get("name") if isinstance(tool_call, dict) else "")) or "unknown_tool"
            tool_call_id = _safe_str(tool_call.get("id") if isinstance(tool_call, dict) else "") or str(index)
            projected_events.append(
                self._build_event(
                    event_type="tool_call",
                    event_id=f"history:{message_id}:tool_call:{tool_call_id}",
                    timestamp=timestamp,
                    session_id=session_id,
                    correlation_id=tool_call_id,
                    agent_label=agent_label,
                    workflow_name=workflow_name,
                    payload={
                        "tool_name": tool_name,
                        "phase": phase or "tool_call_start",
                        "metadata": {
                            "source": "chat_history",
                            "event_family": "tool",
                            "event_status": "requested",
                            "thread_id": self._history_value(history_entry, "thread-id", "thread_id"),
                            "thread_name": self._history_value(history_entry, "thread-name", "thread_name"),
                            "event_name": event_name,
                            "target_agent": target_agent,
                        },
                    },
                )
            )

        tool_name = _safe_str(history_entry.get("name"))
        tool_call_id = _safe_str(history_entry.get("tool_call_id")) or None
        if role == "tool" and tool_name:
            tool_event_family = "failure" if event_name == "tool_failed" else "completion"
            tool_event_status = "failed" if event_name == "tool_failed" else "completed"
            projected_events.append(
                self._build_event(
                    event_type="tool_call",
                    event_id=f"history:{message_id}:tool_result:{tool_call_id or tool_name}",
                    timestamp=timestamp,
                    session_id=session_id,
                    correlation_id=tool_call_id,
                    agent_label=agent_label,
                    workflow_name=workflow_name,
                    payload={
                        "tool_name": tool_name,
                        "phase": phase or "tool_result",
                        "metadata": {
                            "source": "chat_history",
                            "event_family": tool_event_family,
                            "event_status": tool_event_status,
                            "event_kind": event_kind,
                            "event_name": event_name,
                            "target_agent": target_agent,
                        },
                    },
                )
            )

        if target_agent:
            source_agent = _safe_str(workflow_payload.get("source_agent") or agent_label)
            protocol = _safe_str(workflow_payload.get("protocol") or action_name) or "workflow_history"
            handoff_status = "completed" if event_name == "routed_agent_complete" else "failed" if event_name == "routed_agent_failed" else "requested"
            projected_events.append(
                self._build_event(
                    event_type="agent_handoff",
                    event_id=f"history:{message_id}:agent_handoff:{target_agent}",
                    timestamp=timestamp,
                    session_id=session_id,
                    correlation_id=correlation_id or event_name,
                    agent_label=agent_label,
                    workflow_name=workflow_name,
                    payload={
                        "source_agent": source_agent,
                        "target_agent": target_agent,
                        "protocol": protocol,
                        "metadata": {
                            "source": "chat_history",
                            "event_kind": event_kind,
                            "event_name": event_name,
                            "event_family": "handoff",
                            "event_status": handoff_status,
                            "payload": dict(workflow_payload),
                        },
                    },
                )
            )

        return projected_events

    def load_runtime_events(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        query_context_by_id: dict[str, dict[str, Any]] = {}
        combined_events: list[dict[str, Any]] = list(
            self.load_stored_runtime_events(base_dir=base_dir, session_id=session_id)
        )
        for learning_entry in self.load_learning_entries(base_dir=base_dir):
            combined_events.extend(
                self._project_learning_entry(learning_entry, query_context_by_id=query_context_by_id)
            )
        for history_entry in self.load_history_entries(history_entries):
            combined_events.extend(self._project_history_entry(history_entry))

        unique_events: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for event_object in combined_events:
            if not isinstance(event_object, dict):
                continue
            event_session_id = _safe_str(event_object.get("session_id"))
            if session_id and event_session_id != _safe_str(session_id):
                continue
            event_id = _safe_str(event_object.get("event_id")) or json.dumps(event_object, ensure_ascii=False, sort_keys=True)
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            unique_events.append(event_object)
        return unique_events

    def _normalize_trace_kind(self, *, role: str, tool_calls: list[dict[str, Any]], handoff_object: dict[str, Any] | None) -> str:
        if role == "tool":
            return "tool_result"
        if tool_calls:
            return "assistant_tool_call"
        if handoff_object:
            return "agent_handoff"
        return role or "message"

    def _normalize_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        normalized_calls: list[dict[str, Any]] = []
        if not isinstance(tool_calls, list):
            return normalized_calls
        for tool_call in tool_calls:
            if isinstance(tool_call, dict):
                normalized_calls.append(_json_safe_copy(tool_call))
        return normalized_calls

    def _build_trace_summary(
        self,
        *,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]],
        tool_name: str,
        handoff_object: dict[str, Any] | None,
    ) -> str:
        if tool_calls:
            tool_names = [
                _safe_str(((tool_call.get("function") or {}).get("name") if isinstance(tool_call.get("function"), dict) else tool_call.get("name")))
                for tool_call in tool_calls
            ]
            compact_names = ", ".join(name for name in tool_names if name) or "tool"
            return f"assistant tool_calls: {compact_names}"
        if role == "tool":
            return f"tool result: {tool_name or 'unknown'}"
        if handoff_object:
            return (
                "handoff:"
                f"{_safe_str(handoff_object.get('source_agent')) or 'unknown'}"
                f"->{_safe_str(handoff_object.get('target_agent')) or 'unknown'}"
            )
        if content:
            compact = " ".join(content.split())
            return compact[:120] + ("..." if len(compact) > 120 else "")
        return role or "message"

    def _build_trace_entry(self, history_entry: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(history_entry, dict):
            return None

        workflow_object = self._workflow_data(history_entry)
        snapshot_object = self._workflow_snapshot(workflow_object)
        effective_event = self._effective_event(workflow_object, snapshot_object)
        workflow_payload = effective_event.get("payload") if isinstance(effective_event.get("payload"), dict) else {}
        role = _safe_str(history_entry.get("role")) or "message"
        tool_calls = self._normalize_tool_calls(history_entry.get("tool_calls"))
        timestamp = _safe_str(self._history_value(history_entry, "time", "timestamp", "date")) or _utc_now_iso()
        message_id = _safe_str(self._history_value(history_entry, "message-id", "message_id")) or str(uuid.uuid4())
        thread_id = self._history_value(history_entry, "thread-id", "thread_id")
        thread_name = self._history_value(history_entry, "thread-name", "thread_name")
        session_id = _safe_str(workflow_object.get("scope_key") or thread_id) or None
        assistant_name = _safe_str(self._history_value(history_entry, "assistant-name", "assistant_name")) or None
        agent_label = _safe_str(workflow_object.get("agent_label") or assistant_name or history_entry.get("name")) or None
        workflow_name = _safe_str(workflow_object.get("workflow_name")) or None
        tool_name = _safe_str(history_entry.get("name") or effective_event.get("tool_name")) or None
        target_agent = _safe_str(effective_event.get("target_agent") or workflow_payload.get("target_agent")) or None
        source_agent = _safe_str(workflow_payload.get("source_agent") or agent_label) or None
        protocol = _safe_str(workflow_payload.get("protocol") or effective_event.get("action")) or None
        content_value = history_entry.get("content")
        if content_value is None:
            content = ""
        elif isinstance(content_value, str):
            content = content_value
        else:
            content = json.dumps(_json_safe_copy(content_value), ensure_ascii=False)

        handoff_object: dict[str, Any] | None = None
        if target_agent:
            handoff_object = {
                "source_agent": source_agent,
                "target_agent": target_agent,
                "protocol": protocol or "workflow_history",
                "correlation_id": _safe_str(effective_event.get("correlation_id") or workflow_payload.get("correlation_id")) or None,
                "payload": _json_safe_copy(workflow_payload),
            }

        trace_kind = self._normalize_trace_kind(role=role, tool_calls=tool_calls, handoff_object=handoff_object)
        return {
            "trace_id": f"trace:{message_id}",
            "message_id": message_id,
            "timestamp": timestamp,
            "trace_kind": trace_kind,
            "summary": self._build_trace_summary(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_name=tool_name or "",
                handoff_object=handoff_object,
            ),
            "role": role,
            "content": content,
            "thread_id": thread_id,
            "thread_name": thread_name,
            "session_id": session_id,
            "assistant_name": assistant_name,
            "agent_label": agent_label,
            "workflow_name": workflow_name,
            "tool_name": tool_name,
            "tool_call_id": _safe_str(history_entry.get("tool_call_id")) or None,
            "tool_calls": tool_calls,
            "handoff": handoff_object,
            "workflow": _json_safe_copy(workflow_object),
            "workflow_snapshot": _json_safe_copy(snapshot_object),
            "workflow_payload": _json_safe_copy(workflow_payload),
            "data": _json_safe_copy(history_entry.get("data") if isinstance(history_entry.get("data"), dict) else {}),
        }

    def load_history_trace(
        self,
        *,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        trace_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        trace_entries: list[dict[str, Any]] = []
        for history_entry in self.load_history_entries(history_entries):
            trace_entry = self._build_trace_entry(history_entry)
            if not isinstance(trace_entry, dict):
                continue
            if session_id and _safe_str(trace_entry.get("session_id")) != _safe_str(session_id):
                continue
            trace_entries.append(trace_entry)

        ordered_trace = sorted(
            trace_entries,
            key=lambda trace_entry: (
                _safe_str(trace_entry.get("timestamp")),
                _safe_str(trace_entry.get("message_id")),
            ),
        )
        if trace_limit is not None and trace_limit >= 0:
            ordered_trace = ordered_trace[-trace_limit:]
        return ordered_trace


class RuntimeMetricsService:
    def __init__(self, projection_service: RuntimeProjectionService) -> None:
        self._projection_service = projection_service

    def summarize_events(self, runtime_events: list[dict[str, Any]], *, session_id: str | None = None) -> dict[str, Any]:
        event_type_counts: dict[str, int] = {}
        tool_name_counts: dict[str, int] = {}
        handoff_target_counts: dict[str, int] = {}
        agent_label_counts: dict[str, int] = {}
        workflow_name_counts: dict[str, int] = {}
        session_event_counts: dict[str, int] = {}
        event_family_counts: dict[str, int] = {}
        event_status_counts: dict[str, int] = {}
        latency_values: list[float] = []
        reward_values: list[float] = []
        success_count = 0
        failure_count = 0
        latest_status_by_session: dict[str, tuple[str, tuple[str, str]]] = {}

        for runtime_event in runtime_events:
            event_type = _safe_str(runtime_event.get("event_type")) or "unknown"
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1

            event_session_id = _safe_str(runtime_event.get("session_id"))
            if event_session_id:
                session_event_counts[event_session_id] = session_event_counts.get(event_session_id, 0) + 1

            agent_label = _safe_str(runtime_event.get("agent_label"))
            if agent_label:
                agent_label_counts[agent_label] = agent_label_counts.get(agent_label, 0) + 1

            workflow_name = _safe_str(runtime_event.get("workflow_name"))
            if workflow_name:
                workflow_name_counts[workflow_name] = workflow_name_counts.get(workflow_name, 0) + 1

            payload = runtime_event.get("payload") if isinstance(runtime_event.get("payload"), dict) else {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            tool_name = _safe_str(payload.get("tool_name") or metadata.get("tool_name"))
            if tool_name:
                tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1

            event_family = _safe_str(metadata.get("event_family"))
            if event_family:
                event_family_counts[event_family] = event_family_counts.get(event_family, 0) + 1

            event_status = _safe_str(metadata.get("event_status"))
            if event_status:
                event_status_counts[event_status] = event_status_counts.get(event_status, 0) + 1
                if event_session_id:
                    current_order = (
                        _safe_str(runtime_event.get("timestamp")),
                        _safe_str(runtime_event.get("event_id")),
                    )
                    previous_status, previous_order = latest_status_by_session.get(event_session_id, ("", ("", "")))
                    if current_order >= previous_order:
                        latest_status_by_session[event_session_id] = (event_status, current_order)

            target_agent = _safe_str(payload.get("target_agent") or metadata.get("target_agent"))
            if event_type == "agent_handoff" and target_agent:
                handoff_target_counts[target_agent] = handoff_target_counts.get(target_agent, 0) + 1

            latency_ms = payload.get("latency_ms")
            if isinstance(latency_ms, (int, float)) and latency_ms >= 0:
                latency_values.append(float(latency_ms))

            reward_value = payload.get("reward")
            if isinstance(reward_value, (int, float)):
                reward_values.append(float(reward_value))

            if event_type == "outcome":
                if bool(payload.get("success")):
                    success_count += 1
                else:
                    failure_count += 1

        average_latency_ms = round(sum(latency_values) / len(latency_values), 2) if latency_values else 0.0
        average_reward = round(sum(reward_values) / len(reward_values), 4) if reward_values else 0.0
        active_session_count = sum(
            1
            for status, _order in latest_status_by_session.values()
            if status not in {"completed", "failed", "exhausted"}
        )
        return {
            "event_count": len(runtime_events),
            "session_id": session_id,
            "session_count": len(session_event_counts),
            "active_session_count": active_session_count,
            "event_type_counts": event_type_counts,
            "tool_name_counts": tool_name_counts,
            "handoff_target_counts": handoff_target_counts,
            "agent_label_counts": agent_label_counts,
            "workflow_name_counts": workflow_name_counts,
            "session_event_counts": session_event_counts,
            "event_family_counts": event_family_counts,
            "event_status_counts": event_status_counts,
            "success_count": success_count,
            "failure_count": failure_count,
            "average_latency_ms": average_latency_ms,
            "average_reward": average_reward,
        }

    def load_runtime_metrics(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        runtime_events = self._projection_service.load_runtime_events(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
        )
        return self.summarize_events(runtime_events, session_id=session_id)


class RuntimeViewService:
    def __init__(self, projection_service: RuntimeProjectionService, metrics_service: RuntimeMetricsService) -> None:
        self._projection_service = projection_service
        self._metrics_service = metrics_service

    def sort_events(self, runtime_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            [event_object for event_object in runtime_events if isinstance(event_object, dict)],
            key=lambda event_object: (
                _safe_str(event_object.get("timestamp")),
                _safe_str(event_object.get("event_id")),
            ),
        )

    def _build_summary(self, runtime_event: dict[str, Any], metadata: dict[str, Any]) -> str:
        event_type = _safe_str(runtime_event.get("event_type"))
        payload = runtime_event.get("payload") if isinstance(runtime_event.get("payload"), dict) else {}
        if event_type == "query":
            return f"query:{_safe_str(payload.get('tool_name')) or 'unknown'}"
        if event_type == "outcome":
            status = "ok" if payload.get("success") else "failed"
            return f"outcome:{_safe_str(payload.get('tool_name')) or 'unknown'}:{status}"
        if event_type == "tool_call":
            return f"tool:{_safe_str(payload.get('tool_name')) or 'unknown'}:{_safe_str(payload.get('phase')) or 'n/a'}"
        if event_type == "agent_handoff":
            return "handoff:" + (
                f"{_safe_str(payload.get('source_agent')) or 'unknown'}->{_safe_str(payload.get('target_agent')) or 'unknown'}:{_safe_str(metadata.get('event_status')) or 'requested'}"
            )
        if event_type == "workflow_state":
            return f"state:{_safe_str(payload.get('state_name')) or 'n/a'}:{_safe_str(metadata.get('event_name')) or _safe_str(metadata.get('event_family')) or _safe_str(payload.get('state_phase')) or 'n/a'}"
        return event_type or "event"

    def build_timeline_entry(self, runtime_event: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(runtime_event, dict):
            return None
        payload = runtime_event.get("payload") if isinstance(runtime_event.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return {
            "timestamp": runtime_event.get("timestamp"),
            "event_id": runtime_event.get("event_id"),
            "event_type": runtime_event.get("event_type"),
            "session_id": runtime_event.get("session_id"),
            "agent_label": runtime_event.get("agent_label"),
            "workflow_name": runtime_event.get("workflow_name"),
            "status": metadata.get("event_status"),
            "family": metadata.get("event_family") or runtime_event.get("event_type"),
            "summary": self._build_summary(runtime_event, metadata),
            "tool_name": payload.get("tool_name"),
            "state_name": payload.get("state_name"),
            "target_agent": payload.get("target_agent"),
        }

    def _build_session_summary(self, session_id: str, runtime_events: list[dict[str, Any]]) -> dict[str, Any]:
        ordered_events = self.sort_events(runtime_events)
        workflow_state_events = [
            event_object for event_object in ordered_events if _safe_str(event_object.get("event_type")) == "workflow_state"
        ]
        handoff_events = [
            event_object for event_object in ordered_events if _safe_str(event_object.get("event_type")) == "agent_handoff"
        ]
        retry_events = [
            event_object
            for event_object in workflow_state_events
            if isinstance(event_object.get("payload"), dict)
            and isinstance(event_object["payload"].get("metadata"), dict)
            and _safe_str(event_object["payload"]["metadata"].get("event_family")) == "retry"
        ]
        failure_events = [
            event_object
            for event_object in ordered_events
            if (
                _safe_str(event_object.get("event_type")) == "outcome"
                and isinstance(event_object.get("payload"), dict)
                and not bool(event_object["payload"].get("success"))
            )
            or (
                isinstance(event_object.get("payload"), dict)
                and isinstance(event_object["payload"].get("metadata"), dict)
                and _safe_str(event_object["payload"]["metadata"].get("event_status")) == "failed"
            )
        ]
        metrics = self._metrics_service.summarize_events(ordered_events, session_id=session_id)
        agent_labels = sorted({_safe_str(event_object.get("agent_label")) for event_object in ordered_events if _safe_str(event_object.get("agent_label"))})
        workflow_names = sorted({_safe_str(event_object.get("workflow_name")) for event_object in ordered_events if _safe_str(event_object.get("workflow_name"))})
        latest_workflow_state = self.build_timeline_entry(workflow_state_events[-1] if workflow_state_events else None)
        latest_handoff = self.build_timeline_entry(handoff_events[-1] if handoff_events else None)
        return {
            "session_id": session_id,
            "event_count": len(ordered_events),
            "agent_labels": agent_labels,
            "workflow_names": workflow_names,
            "first_timestamp": ordered_events[0].get("timestamp") if ordered_events else None,
            "last_timestamp": ordered_events[-1].get("timestamp") if ordered_events else None,
            "latest_workflow_state": latest_workflow_state,
            "latest_handoff": latest_handoff,
            "retry": {
                "requested_count": sum(
                    1
                    for event_object in retry_events
                    if _safe_str(((event_object.get("payload") or {}).get("metadata") or {}).get("event_name")) == "retry_requested"
                ),
                "exhausted_count": sum(
                    1
                    for event_object in retry_events
                    if _safe_str(((event_object.get("payload") or {}).get("metadata") or {}).get("event_name")) == "retry_exhausted"
                ),
            },
            "handoffs": {
                "count": len(handoff_events),
                "completed_count": sum(
                    1
                    for event_object in handoff_events
                    if _safe_str(((event_object.get("payload") or {}).get("metadata") or {}).get("event_status")) == "completed"
                ),
                "failed_count": sum(
                    1
                    for event_object in handoff_events
                    if _safe_str(((event_object.get("payload") or {}).get("metadata") or {}).get("event_status")) == "failed"
                ),
            },
            "failure_count": len(failure_events),
            "metrics": metrics,
        }

    def load_runtime_view(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        event_limit: int | None = None,
        trace_limit: int | None = None,
    ) -> dict[str, Any]:
        runtime_events = self._projection_service.load_runtime_events(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
        )
        trace_entries = self._projection_service.load_history_trace(
            session_id=session_id,
            history_entries=history_entries,
            trace_limit=trace_limit,
        )
        ordered_events = self.sort_events(runtime_events)
        if event_limit is not None and event_limit >= 0:
            ordered_events = ordered_events[-event_limit:]

        grouped_events: dict[str, list[dict[str, Any]]] = {}
        for event_object in ordered_events:
            group_session_id = _safe_str(event_object.get("session_id")) or "unknown"
            grouped_events.setdefault(group_session_id, []).append(event_object)

        session_views = [
            self._build_session_summary(group_session_id, grouped_events[group_session_id])
            for group_session_id in sorted(grouped_events)
        ]

        return {
            "generated_at": _utc_now_iso(),
            "session_count": len(session_views),
            "event_count": len(ordered_events),
            "metrics": self._metrics_service.load_runtime_metrics(
                base_dir=base_dir,
                session_id=session_id,
                history_entries=history_entries,
            ),
            "sessions": session_views,
            "events": [self.build_timeline_entry(event_object) for event_object in ordered_events if isinstance(event_object, dict)],
            "trace_count": len(trace_entries),
            "trace": trace_entries,
        }

    def export_runtime_view(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        event_limit: int | None = None,
        trace_limit: int | None = None,
    ) -> str:
        target_root = self._projection_service.load_generated_root(base_dir)
        target_root.mkdir(parents=True, exist_ok=True)
        target_path = target_root / (f"runtime_view_{session_id}.json" if session_id else "runtime_view_latest.json")
        view_object = self.load_runtime_view(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )
        with open(target_path, "w", encoding="utf-8") as view_file:
            json.dump(view_object, view_file, ensure_ascii=False, indent=2)
        return str(target_path)


class WorkflowValidationService:
    def load_report(self) -> dict[str, Any]:
        try:
            validate_runtime_contracts = _load_symbol("agents_config", "validate_runtime_contracts")
            report = validate_runtime_contracts()
        except Exception as exc:
            return {
                "valid": False,
                "workflow_count": 0,
                "valid_count": 0,
                "invalid_count": 1,
                "mapping_errors": [f"workflow validation unavailable: {type(exc).__name__}: {exc}"],
                "workflows": [],
                "errors": [f"workflow validation unavailable: {type(exc).__name__}: {exc}"],
            }

        flattened_errors = list(report.get("errors") or report.get("mapping_errors") or [])
        return {**report, "errors": flattened_errors}

    def load_workflow_report(self, workflow_name: str) -> dict[str, Any]:
        report = self.load_report()
        for workflow_report in report.get("workflows") or []:
            if _safe_str((workflow_report or {}).get("name")) == _safe_str(workflow_name):
                return {**workflow_report, "errors": list((workflow_report or {}).get("errors") or [])}
        return {
            "name": workflow_name,
            "valid": False,
            "errors": [f"workflow '{workflow_name}' is not defined in WORKFLOW_CONFIGS"],
            "warnings": [],
            "stats": {},
        }


class WorkflowStatusService:
    def __init__(self, validation_service: WorkflowValidationService) -> None:
        self._validation_service = validation_service

    def _load_tool_snapshot_config(self, actor_name: str) -> dict[str, Any]:
        if not actor_name:
            return {}
        try:
            get_tool_config = _load_symbol("agents_config", "get_tool_config")
        except Exception:
            return {}
        try:
            return dict((get_tool_config(actor_name) or {}).get("snapshot_view") or {})
        except Exception:
            return {}

    def format_snapshot_view(self, workflow: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(workflow, dict):
            return None

        snapshot = workflow.get("snapshot") if isinstance(workflow.get("snapshot"), dict) else {}
        actor = snapshot.get("actor") if isinstance(snapshot.get("actor"), dict) else {}
        event = snapshot.get("event") if isinstance(snapshot.get("event"), dict) else {}
        workflow_name = _safe_str(snapshot.get("workflow_name") or workflow.get("workflow_name"))
        current_state = _safe_str(snapshot.get("current_state") or workflow.get("current_state"))
        actor_name = _safe_str(actor.get("name"))
        event_name = _safe_str(event.get("name"))

        if not any((workflow_name, current_state, actor_name, event_name)):
            return None

        tool_snapshot_config = self._load_tool_snapshot_config(actor_name)
        if tool_snapshot_config:
            action = _safe_str(event.get("action")) or None
            correlation_id = _safe_str(event.get("correlation_id")) or None
            summary_fields = [
                _safe_str(value)
                for value in (tool_snapshot_config.get("summary_fields") or [])
                if _safe_str(value)
            ]
            summary_values = [
                _safe_str(event.get(field_name))
                for field_name in summary_fields
                if event.get(field_name) not in (None, "", [], {})
            ]
            return {
                "kind": _safe_str(tool_snapshot_config.get("kind")) or "tool_action",
                "title": _safe_str(tool_snapshot_config.get("title")) or current_state or actor_name,
                "summary": " | ".join(summary_values) if summary_values else current_state or actor_name,
                "workflow_name": workflow_name,
                "state": current_state,
                "actor_name": actor_name,
                "action": action,
                "correlation_id": correlation_id,
                "event_name": event_name or None,
            }

        return {
            "kind": "workflow_state",
            "title": current_state or workflow_name or actor_name or event_name,
            "summary": actor_name or event_name or workflow_name,
            "workflow_name": workflow_name or None,
            "state": current_state or None,
            "actor_name": actor_name or None,
            "action": event.get("action"),
            "correlation_id": event.get("correlation_id"),
            "event_name": event_name or None,
        }

    def enrich_status_entry(self, entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return entry
        enriched = dict(entry)
        workflow = dict(enriched.get("workflow") or {})
        snapshot_view = self.format_snapshot_view(workflow)
        if snapshot_view is not None:
            workflow["snapshot_view"] = snapshot_view
        enriched["workflow"] = workflow
        return enriched

    def load_status_view(
        self,
        *,
        target_agent: str | None = None,
        workflow_name: str | None = None,
        thread_id: int | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        try:
            get_latest_workflow_status = _load_symbol("agents_factory", "get_latest_workflow_status")
            get_workflow_history_entries = _load_symbol("agents_factory", "get_workflow_history_entries")
        except Exception as exc:
            validation = (
                self._validation_service.load_workflow_report(workflow_name)
                if workflow_name
                else self._validation_service.load_report()
            )
            return {
                "latest": None,
                "items": [],
                "validation": validation,
                "error": f"workflow status unavailable: {type(exc).__name__}: {exc}",
                "workflow_name": workflow_name,
                "agent_label": target_agent,
            }

        try:
            items = [
                self.enrich_status_entry(item)
                for item in get_workflow_history_entries(
                    agent_label=target_agent,
                    workflow_name=workflow_name,
                    thread_id=thread_id,
                    limit=limit,
                )
            ]
            latest = self.enrich_status_entry(
                get_latest_workflow_status(
                    agent_label=target_agent,
                    workflow_name=workflow_name,
                    thread_id=thread_id,
                )
            )
            validation = (
                self._validation_service.load_workflow_report(workflow_name)
                if workflow_name
                else self._validation_service.load_report()
            )
            return {
                "latest": latest,
                "items": items,
                "validation": validation,
                "workflow_name": workflow_name,
                "agent_label": target_agent,
            }
        except Exception as exc:
            validation = (
                self._validation_service.load_workflow_report(workflow_name)
                if workflow_name
                else self._validation_service.load_report()
            )
            return {
                "latest": None,
                "items": [],
                "validation": validation,
                "error": f"workflow status unavailable: {type(exc).__name__}: {exc}",
                "workflow_name": workflow_name,
                "agent_label": target_agent,
            }


class QueueHealthService:
    def load_queue_health(self) -> tuple[str, bool]:
        backend = _safe_str(os.getenv("ALDE_WEB_QUEUE_BACKEND", "inmemory")).lower() or "inmemory"
        if backend != "rq":
            return backend, True

        redis_url = _safe_str(os.getenv("ALDE_WEB_REDIS_URL", "redis://localhost:6379/0"))
        try:
            redis_module = importlib.import_module("redis")
            redis_connection = redis_module.Redis.from_url(redis_url)
            redis_connection.ping()
            return "rq", True
        except Exception:
            return "rq", False


class RuntimeObservabilityService:
    def __init__(
        self,
        view_service: RuntimeViewService,
        validation_service: WorkflowValidationService,
        queue_health_service: QueueHealthService,
    ) -> None:
        self._view_service = view_service
        self._validation_service = validation_service
        self._queue_health_service = queue_health_service

    def load_router_parallel_status(self) -> dict[str, Any]:
        try:
            load_router_parallel_runtime_status = _load_symbol("agents_factory", "get_router_parallel_runtime_status")
        except Exception:
            return {
                "available": False,
                "config": {
                    "parallel_enabled": False,
                    "configured_worker_count": 1,
                    "configured_timeout_seconds": 30.0,
                    "max_parallel_workers": 1,
                },
                "total_parallel_runs": 0,
                "total_parallel_branches": 0,
                "total_completed_branches": 0,
                "total_failed_branches": 0,
                "total_timeout_branches": 0,
                "total_partial_fail_runs": 0,
            }

        try:
            status = load_router_parallel_runtime_status()
        except Exception as exc:
            return {
                "available": False,
                "error": f"router parallel status unavailable: {type(exc).__name__}: {exc}",
                "config": {
                    "parallel_enabled": False,
                    "configured_worker_count": 1,
                    "configured_timeout_seconds": 30.0,
                    "max_parallel_workers": 1,
                },
                "total_parallel_runs": 0,
                "total_parallel_branches": 0,
                "total_completed_branches": 0,
                "total_failed_branches": 0,
                "total_timeout_branches": 0,
                "total_partial_fail_runs": 0,
            }

        normalized_status = dict(status if isinstance(status, dict) else {})
        normalized_status.setdefault("config", {})
        normalized_status["available"] = True
        return normalized_status

    def load_snapshot(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        event_limit: int | None = None,
        trace_limit: int | None = None,
    ) -> dict[str, Any]:
        runtime_view = self._view_service.load_runtime_view(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )
        validation_report = self._validation_service.load_report()
        queue_backend, queue_healthy = self._queue_health_service.load_queue_health()
        session_views = [session_view for session_view in (runtime_view.get("sessions") or []) if isinstance(session_view, dict)]
        latest_sessions = sorted(
            session_views,
            key=lambda session_view: (
                _safe_str(session_view.get("last_timestamp")),
                _safe_str(session_view.get("session_id")),
            ),
            reverse=True,
        )[:5]
        metrics = runtime_view.get("metrics") if isinstance(runtime_view.get("metrics"), dict) else {}
        router_parallel_status = self.load_router_parallel_status()
        router_parallel_config = (
            router_parallel_status.get("config")
            if isinstance(router_parallel_status.get("config"), dict)
            else {}
        )

        return {
            "generated_at": runtime_view.get("generated_at") or _utc_now_iso(),
            "session_id": session_id,
            "healthy": bool(validation_report.get("valid") and queue_healthy),
            "queue_backend": queue_backend,
            "queue_healthy": bool(queue_healthy),
            "session_count": runtime_view.get("session_count", 0),
            "event_count": runtime_view.get("event_count", 0),
            "trace_count": runtime_view.get("trace_count", 0),
            "active_session_count": int(metrics.get("active_session_count") or 0),
            "validation": validation_report,
            "metrics": metrics,
            "router_parallel_status": router_parallel_status,
            "router_parallel_enabled": bool(router_parallel_config.get("parallel_enabled")),
            "router_parallel_hard_timeout_enabled": bool(router_parallel_config.get("hard_timeout_enabled")),
            "router_parallel_total_runs": int(router_parallel_status.get("total_parallel_runs") or 0),
            "router_parallel_timeout_branches": int(router_parallel_status.get("total_timeout_branches") or 0),
            "router_parallel_partial_fail_runs": int(router_parallel_status.get("total_partial_fail_runs") or 0),
            "router_parallel_hard_timeout_runs": int(router_parallel_status.get("total_hard_timeout_runs") or 0),
            "session_ids": [
                _safe_str(session_view.get("session_id"))
                for session_view in session_views
                if _safe_str(session_view.get("session_id"))
            ],
            "latest_sessions": [
                {
                    "session_id": session_view.get("session_id"),
                    "last_timestamp": session_view.get("last_timestamp"),
                    "failure_count": session_view.get("failure_count", 0),
                    "latest_workflow_state": session_view.get("latest_workflow_state"),
                    "latest_handoff": session_view.get("latest_handoff"),
                }
                for session_view in latest_sessions
            ],
        }


class OperatorStatusService:
    def __init__(
        self,
        validation_service: WorkflowValidationService,
        queue_health_service: QueueHealthService,
    ) -> None:
        self._validation_service = validation_service
        self._queue_health_service = queue_health_service

    def load_dispatcher_status(self) -> dict[str, Any]:
        try:
            document_dispatch_service = _load_symbol("tools", "DOCUMENT_DISPATCH_SERVICE")
            default_dispatcher_db_path = _load_symbol("tools", "_default_dispatcher_db_path")
            dispatcher_db_path = str(default_dispatcher_db_path() or "")
            dispatcher_error = document_dispatch_service.check_dispatcher_access(
                resolved_db_path=dispatcher_db_path
            )
        except Exception as exc:
            dispatcher_db_path = ""
            dispatcher_error = f"{type(exc).__name__}: {exc}"

        return {
            "dispatcher_db_path": dispatcher_db_path,
            "dispatcher_healthy": dispatcher_error in (None, ""),
            "dispatcher_error": dispatcher_error,
        }

    def load_agentsdb_status(self) -> dict[str, Any]:
        try:
            load_runtime_config = _load_symbol("agents_db", "load_agentsdb_runtime_config_from_env")
            socket_repository_class = _load_symbol("agents_db", "AgentDbSocketRepository")
        except Exception as exc:
            return {
                "agentsdb_uri": "",
                "agentsdb_database_name": "",
                "agentsdb_endpoint": "",
                "agentsdb_healthy": False,
                "agentsdb_error": f"{type(exc).__name__}: {exc}",
                "agentsdb_detail": "agents_db module unavailable",
            }

        runtime_config = load_runtime_config()
        if runtime_config is None:
            return {
                "agentsdb_uri": "",
                "agentsdb_database_name": "",
                "agentsdb_endpoint": "",
                "agentsdb_healthy": None,
                "agentsdb_error": "",
                "agentsdb_detail": "not configured",
            }

        agentsdb_uri = _safe_str(getattr(runtime_config, "agents_db_uri", ""))
        database_name = _safe_str(getattr(runtime_config, "database_name", "alde_knowledge")) or "alde_knowledge"
        if not agentsdb_uri:
            return {
                "agentsdb_uri": "",
                "agentsdb_database_name": database_name,
                "agentsdb_endpoint": "",
                "agentsdb_healthy": None,
                "agentsdb_error": "",
                "agentsdb_detail": "not configured",
            }

        normalized_uri = agentsdb_uri.lower()
        if not normalized_uri.startswith("agentsdb://"):
            backend_name = normalized_uri.split("://", 1)[0] if "://" in normalized_uri else "backend"
            return {
                "agentsdb_uri": agentsdb_uri,
                "agentsdb_database_name": database_name,
                "agentsdb_endpoint": "",
                "agentsdb_healthy": None,
                "agentsdb_error": "",
                "agentsdb_detail": f"{backend_name} backend configured",
            }

        parsed_uri = urlparse(agentsdb_uri)
        host = _safe_str(parsed_uri.hostname) or "127.0.0.1"
        port = int(parsed_uri.port or 2331)
        endpoint = f"{host}:{port}"

        try:
            repository = socket_repository_class.create_from_uri(
                agentsdb_uri,
                database_name,
                timeout_seconds=2.0,
            )
            health_payload = repository._request_object("health")
            healthy = bool(health_payload.get("ok"))
            response_backend = _safe_str(health_payload.get("storage_backend") or health_payload.get("backend") or "socket")
            resolved_database_name = _safe_str(health_payload.get("database_name") or database_name) or database_name
            return {
                "agentsdb_uri": agentsdb_uri,
                "agentsdb_database_name": resolved_database_name,
                "agentsdb_endpoint": endpoint,
                "agentsdb_healthy": healthy,
                "agentsdb_error": "" if healthy else _safe_str(health_payload.get("error") or "health response not ok"),
                "agentsdb_detail": f"{response_backend} @ {endpoint}",
            }
        except (socket.timeout, OSError, Exception) as exc:
            return {
                "agentsdb_uri": agentsdb_uri,
                "agentsdb_database_name": database_name,
                "agentsdb_endpoint": endpoint,
                "agentsdb_healthy": False,
                "agentsdb_error": f"{type(exc).__name__}: {exc}",
                "agentsdb_detail": f"socket @ {endpoint}",
            }

    def load_mcp_config_path(self) -> Path:
        return Path(__file__).with_name("mcp_servers.json")

    def _build_service_row(
        self,
        *,
        title: str,
        state: str,
        detail: str,
        note: str = "",
    ) -> dict[str, Any]:
        normalized_state = _safe_str(state).lower() or "unknown"
        return {
            "title": title,
            "state": normalized_state,
            "detail": detail,
            "note": note,
            "healthy": normalized_state == "pass",
        }

    def _load_numeric_value(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    def _load_percentile_value(self, values: list[float], percentile: float) -> float:
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

    def _build_transport_metrics_from_attempts(self, attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        transport_metrics: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            transport_name = _safe_str(attempt.get("transport") or "unknown") or "unknown"
            metric_payload = transport_metrics.setdefault(
                transport_name,
                {
                    "attempt_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "timeout_count": 0,
                    "latency_values": [],
                },
            )
            metric_payload["attempt_count"] += 1
            if bool(attempt.get("ok")):
                metric_payload["success_count"] += 1
            else:
                metric_payload["failure_count"] += 1
            if bool(attempt.get("timed_out")):
                metric_payload["timeout_count"] += 1
            metric_payload["latency_values"].append(self._load_numeric_value(attempt.get("latency_ms")))

        normalized_transport_metrics: dict[str, dict[str, Any]] = {}
        for transport_name, metric_payload in transport_metrics.items():
            attempt_count = max(int(metric_payload.get("attempt_count") or 0), 1)
            latency_values = [
                self._load_numeric_value(item)
                for item in (metric_payload.get("latency_values") or [])
                if self._load_numeric_value(item) >= 0.0
            ]
            normalized_transport_metrics[transport_name] = {
                "attempt_count": int(metric_payload.get("attempt_count") or 0),
                "success_count": int(metric_payload.get("success_count") or 0),
                "failure_count": int(metric_payload.get("failure_count") or 0),
                "timeout_count": int(metric_payload.get("timeout_count") or 0),
                "error_rate": round(float(metric_payload.get("failure_count") or 0) / float(attempt_count), 4),
                "timeout_rate": round(float(metric_payload.get("timeout_count") or 0) / float(attempt_count), 4),
                "p50_latency_ms": round(self._load_percentile_value(latency_values, 50), 3),
                "p95_latency_ms": round(self._load_percentile_value(latency_values, 95), 3),
                "avg_latency_ms": round(sum(latency_values) / len(latency_values), 3) if latency_values else 0.0,
            }
        return normalized_transport_metrics

    def _build_overall_metrics_from_attempts(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
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

        success_count = sum(1 for attempt in attempts if bool((attempt or {}).get("ok")))
        failure_count = attempt_count - success_count
        timeout_count = sum(1 for attempt in attempts if bool((attempt or {}).get("timed_out")))
        latency_values = [self._load_numeric_value((attempt or {}).get("latency_ms")) for attempt in attempts]

        return {
            "attempt_count": int(attempt_count),
            "success_count": int(success_count),
            "failure_count": int(failure_count),
            "timeout_count": int(timeout_count),
            "error_rate": round(float(failure_count) / float(attempt_count), 4),
            "timeout_rate": round(float(timeout_count) / float(attempt_count), 4),
            "p50_latency_ms": round(self._load_percentile_value(latency_values, 50), 3),
            "p95_latency_ms": round(self._load_percentile_value(latency_values, 95), 3),
            "avg_latency_ms": round(sum(latency_values) / len(latency_values), 3) if latency_values else 0.0,
        }

    def _load_mcp_probe_metrics(self, normalized_probe: dict[str, Any]) -> dict[str, Any]:
        probe_metrics = normalized_probe.get("probe_metrics") if isinstance(normalized_probe.get("probe_metrics"), dict) else {}
        overall_metrics = probe_metrics.get("overall_metrics") if isinstance(probe_metrics.get("overall_metrics"), dict) else {}
        transport_metrics = probe_metrics.get("transport_metrics") if isinstance(probe_metrics.get("transport_metrics"), dict) else {}
        if overall_metrics and transport_metrics:
            return {
                "overall_metrics": dict(overall_metrics),
                "transport_metrics": dict(transport_metrics),
            }

        attempts = [attempt for attempt in (normalized_probe.get("attempts") or []) if isinstance(attempt, dict)]
        return {
            "overall_metrics": self._build_overall_metrics_from_attempts(attempts),
            "transport_metrics": self._build_transport_metrics_from_attempts(attempts),
        }

    def _infer_recent_action_status(self, title: str, summary: str) -> str:
        combined_text = f"{title} {summary}".lower()
        if any(token in combined_text for token in ("fail", "error", "missing", "unreachable", "degraded", "locked")):
            return "fail"
        if any(token in combined_text for token in ("pass", "ready", "healthy", "completed", "ok")):
            return "pass"
        return "info"

    def _infer_recent_action_type(
        self,
        title: str,
        summary: str,
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        combined_text = " ".join(
            [title, summary, source, _safe_str((metadata or {}).get("raw"))]
        ).lower()
        if any(token in combined_text for token in ("repair", "backup", "restore")):
            return "repair"
        if any(token in combined_text for token in ("export", "snapshot exported")):
            return "export"
        if any(token in combined_text for token in ("refresh", "refreshed", "reload")):
            return "refresh"
        if any(token in combined_text for token in ("validation", "contract")):
            return "validation"
        if any(token in combined_text for token in ("probe", "health", "check")):
            return "probe"
        return "action"

    def _infer_recent_action_group(
        self,
        title: str,
        summary: str,
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        combined_text = " ".join(
            [title, summary, source, _safe_str((metadata or {}).get("raw"))]
        ).lower()
        if "agentsdb" in combined_text:
            return "agentsdb"
        if any(token in combined_text for token in ("queue", "rq", "redis")):
            return "queue"
        if "dispatcher" in combined_text:
            return "dispatcher"
        if "mcp" in combined_text:
            return "mcp"
        if any(token in combined_text for token in ("workflow", "validation", "contract")):
            return "workflow_validation"
        if any(token in combined_text for token in ("runtime", "trace", "snapshot", "observability")):
            return "runtime"
        return _normalized_projection_value(source or "operator", default="operator")

    def _normalize_recent_action_item(self, item: Any) -> dict[str, Any] | None:
        if isinstance(item, dict):
            timestamp = _safe_str(item.get("timestamp") or item.get("created_at") or item.get("occurred_at"))
            title = _safe_str(item.get("title") or item.get("event_type") or item.get("message") or item.get("summary"))
            source = _safe_str(item.get("source") or item.get("tenant_id") or "desktop_operator")
            detail_object = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            metadata_object = detail_object or dict(item)
            summary = _safe_str(item.get("summary"))
            if not summary and detail_object:
                summary = ", ".join(
                    f"{key}={_safe_str(value)}"
                    for key, value in detail_object.items()
                    if _safe_str(key) and _safe_str(value)
                )
            summary = summary or title
            status = _safe_str(item.get("status") or item.get("state")) or self._infer_recent_action_status(title, summary)
            audit_type = _safe_str(item.get("audit_type") or item.get("action_type") or metadata_object.get("audit_type")) or self._infer_recent_action_type(
                title,
                summary,
                source=source,
                metadata=metadata_object,
            )
            action_group = _safe_str(item.get("action_group") or item.get("family") or metadata_object.get("action_group")) or self._infer_recent_action_group(
                title,
                summary,
                source=source,
                metadata=metadata_object,
            )
            return _build_recent_projection_item(
                timestamp=timestamp,
                title=title or "operator.action",
                summary=summary,
                source=source,
                status=status,
                audit_type=audit_type,
                action_group=action_group,
                metadata=metadata_object,
            )

        if isinstance(item, str):
            raw_value = item.strip()
            if not raw_value:
                return None
            timestamp, separator, message = raw_value.partition(" | ")
            summary = message if separator else raw_value
            title = summary.split(":", 1)[0].strip() or "operator.action"
            return _build_recent_projection_item(
                timestamp=timestamp if separator else "",
                title=title,
                summary=summary,
                source="desktop_operator",
                status=self._infer_recent_action_status(title, summary),
                audit_type=self._infer_recent_action_type(title, summary, source="desktop_operator", metadata={"raw": raw_value}),
                action_group=self._infer_recent_action_group(title, summary, source="desktop_operator", metadata={"raw": raw_value}),
                metadata={"raw": raw_value},
            )

        return None

    def load_recent_action_items(
        self,
        *,
        recent_action_entries: list[Any] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        normalized_items = [
            normalized_item
            for normalized_item in (
                self._normalize_recent_action_item(item)
                for item in (recent_action_entries or [])
            )
            if isinstance(normalized_item, dict)
        ]
        return list(reversed(normalized_items[-max(0, int(limit)):]))

    def load_snapshot(
        self,
        *,
        mcp_probe: dict[str, Any] | None = None,
        recent_action_entries: list[Any] | None = None,
    ) -> dict[str, Any]:
        queue_backend, queue_healthy = self._queue_health_service.load_queue_health()
        agentsdb_status = self.load_agentsdb_status()
        validation_report = self._validation_service.load_report()
        dispatcher_status = self.load_dispatcher_status()
        mcp_config_path = self.load_mcp_config_path()

        normalized_probe = dict(mcp_probe or {})
        if not normalized_probe:
            normalized_probe = {
                "ok": None,
                "returncode": None,
                "stdout": "",
                "stderr": "probe not executed yet" if mcp_config_path.is_file() else "mcp_servers.json not found",
            }

        mcp_probe_metrics = self._load_mcp_probe_metrics(normalized_probe)
        mcp_overall_metrics = dict(mcp_probe_metrics.get("overall_metrics") or {})
        mcp_transport_metrics = dict(mcp_probe_metrics.get("transport_metrics") or {})
        mcp_active_server = _safe_str(normalized_probe.get("active_server") or normalized_probe.get("selected_server"))
        mcp_active_transport = _safe_str(normalized_probe.get("active_transport") or normalized_probe.get("selected_transport"))
        mcp_fallback_used = bool(normalized_probe.get("fallback_used"))

        validation_errors = [
            str(item)
            for item in (validation_report.get("errors") or validation_report.get("mapping_errors") or [])
            if str(item)
        ]
        dispatcher_error = str(dispatcher_status.get("dispatcher_error") or "").strip()
        dispatcher_note = dispatcher_error or "Dispatcher projection ready"
        agentsdb_healthy = agentsdb_status.get("agentsdb_healthy")
        if agentsdb_healthy is None:
            agentsdb_state = "not-run"
        elif bool(agentsdb_healthy):
            agentsdb_state = "pass"
        else:
            agentsdb_state = "fail"
        agentsdb_error = _safe_str(agentsdb_status.get("agentsdb_error"))
        agentsdb_note = agentsdb_error or "AgentsDB endpoint reachable"
        agentsdb_detail = _safe_str(agentsdb_status.get("agentsdb_detail")) or _safe_str(agentsdb_status.get("agentsdb_uri")) or "not configured"
        mcp_stdout = _safe_str(normalized_probe.get("stdout"))
        mcp_stderr = _safe_str(normalized_probe.get("stderr"))
        mcp_latency_p95 = self._load_numeric_value(mcp_overall_metrics.get("p95_latency_ms"))
        mcp_error_rate = self._load_numeric_value(mcp_overall_metrics.get("error_rate"))
        mcp_timeout_rate = self._load_numeric_value(mcp_overall_metrics.get("timeout_rate"))
        mcp_probe_message = (mcp_stderr or mcp_stdout or "No MCP probe output available.")[:220]
        if mcp_active_transport:
            mcp_probe_message = (
                f"transport={mcp_active_transport} "
                f"p95={round(mcp_latency_p95, 3)}ms "
                f"err={round(mcp_error_rate, 4)} "
                f"timeout={round(mcp_timeout_rate, 4)}"
            )
            if mcp_fallback_used:
                mcp_probe_message = f"fallback active; {mcp_probe_message}"
        validation_summary = (
            f"{int(validation_report.get('valid_count') or 0)} valid / {int(validation_report.get('invalid_count') or 0)} invalid"
            if validation_report else "No validation report available"
        )
        if normalized_probe.get("ok") is None:
            mcp_state = "not-run"
        elif bool(normalized_probe.get("ok")):
            mcp_state = "pass"
        else:
            mcp_state = "fail"

        service_rows = [
            self._build_service_row(
                title="Queue",
                state="pass" if queue_healthy else "fail",
                detail=f"backend={queue_backend or 'n/a'}",
            ),
            self._build_service_row(
                title="AgentsDB",
                state=agentsdb_state,
                detail=agentsdb_detail,
                note=agentsdb_note,
            ),
            self._build_service_row(
                title="Dispatcher",
                state="pass" if dispatcher_status.get("dispatcher_healthy") else "fail",
                detail=Path(str(dispatcher_status.get("dispatcher_db_path") or "n/a")).name,
                note=dispatcher_note,
            ),
            self._build_service_row(
                title="MCP",
                state=mcp_state,
                detail=(
                    f"{mcp_active_transport or 'unknown'} @ {mcp_active_server or 'n/a'}"
                    if mcp_config_path.is_file()
                    else "config missing"
                ),
                note=mcp_probe_message,
            ),
            self._build_service_row(
                title="Workflow Validation",
                state="pass" if validation_report.get("valid") else "fail",
                detail=validation_summary,
                note=f"{len(validation_errors)} issues projected" if validation_errors else "No active validation errors",
            ),
        ]

        alerts: list[str] = []
        if not queue_healthy:
            alerts.append("Queue backend is unreachable.")
        if agentsdb_healthy is False:
            alerts.append(agentsdb_error or "AgentsDB endpoint is unreachable.")
        if not bool(dispatcher_status.get("dispatcher_healthy")):
            alerts.append(dispatcher_note)
        if not mcp_config_path.is_file():
            alerts.append("mcp_servers.json is missing.")
        elif normalized_probe.get("ok") is False:
            alerts.append(mcp_probe_message)
        if validation_errors:
            alerts.extend(validation_errors[:3])

        healthy_service_count = sum(1 for row in service_rows if bool(row.get("healthy")))
        recent_actions = self.load_recent_action_items(recent_action_entries=recent_action_entries, limit=12)
        recent_action_summary = _build_recent_item_summary(recent_actions)
        recent_action_filters = _build_recent_item_filters(recent_actions)
        summary_metrics = {
            "service_count": len(service_rows),
            "healthy_service_count": healthy_service_count,
            "attention_count": len(alerts),
            "validation_issue_count": len(validation_errors),
            "agentsdb_pass": 1 if agentsdb_healthy is True else 0,
            "agentsdb_monitored": 1 if agentsdb_healthy is not None else 0,
            "recent_fail_count": int(recent_action_summary["status_counts"].get("fail") or 0),
            "recent_pass_count": int(recent_action_summary["status_counts"].get("pass") or 0),
            "mcp_probe_attempt_count": int(mcp_overall_metrics.get("attempt_count") or 0),
            "mcp_fallback_used": 1 if mcp_fallback_used else 0,
            "mcp_error_rate": round(self._load_numeric_value(mcp_overall_metrics.get("error_rate")), 4),
            "mcp_timeout_rate": round(self._load_numeric_value(mcp_overall_metrics.get("timeout_rate")), 4),
            "mcp_latency_p50_ms": round(self._load_numeric_value(mcp_overall_metrics.get("p50_latency_ms")), 3),
            "mcp_latency_p95_ms": round(self._load_numeric_value(mcp_overall_metrics.get("p95_latency_ms")), 3),
            "mcp_active_transport": mcp_active_transport or "",
        }

        return _build_projection_snapshot(
            snapshot_kind="operator",
            healthy=bool(
                queue_healthy
                and agentsdb_healthy is not False
                and dispatcher_status.get("dispatcher_healthy")
                and validation_report.get("valid")
                and normalized_probe.get("ok") is not False
            ),
            alerts=alerts,
            summary_metrics=summary_metrics,
            recent_items=recent_actions,
            detail_rows=service_rows,
            queue_backend=queue_backend,
            queue_healthy=bool(queue_healthy),
            agentsdb_uri=agentsdb_status.get("agentsdb_uri"),
            agentsdb_database_name=agentsdb_status.get("agentsdb_database_name"),
            agentsdb_endpoint=agentsdb_status.get("agentsdb_endpoint"),
            agentsdb_healthy=agentsdb_healthy,
            agentsdb_error=agentsdb_error,
            agentsdb_detail=agentsdb_detail,
            dispatcher_db_path=dispatcher_status.get("dispatcher_db_path"),
            dispatcher_healthy=dispatcher_status.get("dispatcher_healthy"),
            dispatcher_error=dispatcher_status.get("dispatcher_error"),
            workflow_validation=validation_report,
            validation_issue_count=len(validation_errors),
            validation_errors=validation_errors[:6],
            mcp_config_path=str(mcp_config_path),
            mcp_config_present=mcp_config_path.is_file(),
            mcp_probe=normalized_probe,
            mcp_active_server=mcp_active_server,
            mcp_active_transport=mcp_active_transport,
            mcp_fallback_used=mcp_fallback_used,
            mcp_probe_metrics=mcp_probe_metrics,
            mcp_overall_metrics=mcp_overall_metrics,
            mcp_transport_metrics=mcp_transport_metrics,
            service_count=len(service_rows),
            healthy_service_count=healthy_service_count,
            service_rows=service_rows,
            recent_actions=recent_actions,
            audit_summary=recent_action_summary,
            recent_action_filters=recent_action_filters,
        )


class DesktopMonitoringSnapshotService:
    def __init__(
        self,
        view_service: RuntimeViewService,
        observability_service: RuntimeObservabilityService,
    ) -> None:
        self._view_service = view_service
        self._observability_service = observability_service

    def load_snapshot(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        event_limit: int | None = None,
        trace_limit: int | None = None,
    ) -> dict[str, Any]:
        runtime_view = self._view_service.load_runtime_view(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )
        observability = self._observability_service.load_snapshot(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )

        sessions = [session_view for session_view in (runtime_view.get("sessions") or []) if isinstance(session_view, dict)]
        session_views_by_id = {
            _safe_str(session_view.get("session_id")): session_view
            for session_view in sessions
            if _safe_str(session_view.get("session_id"))
        }
        metrics = runtime_view.get("metrics") if isinstance(runtime_view.get("metrics"), dict) else {}
        events = runtime_view.get("events") if isinstance(runtime_view.get("events"), list) else []
        trace_entries = runtime_view.get("trace") if isinstance(runtime_view.get("trace"), list) else []
        latest_session_summaries = [
            session_summary
            for session_summary in (observability.get("latest_sessions") or [])
            if isinstance(session_summary, dict)
        ]
        latest_session_id = _safe_str((latest_session_summaries[0] if latest_session_summaries else {}).get("session_id"))
        latest_session = dict(session_views_by_id.get(latest_session_id) or (sessions[-1] if sessions else {}))

        trace_agents = sorted(
            {
                _safe_str(trace_entry.get("agent_label") or trace_entry.get("assistant_name"))
                for trace_entry in trace_entries
                if isinstance(trace_entry, dict)
                and _safe_str(trace_entry.get("agent_label") or trace_entry.get("assistant_name"))
            }
        )
        trace_workflows = sorted(
            {
                _safe_str(trace_entry.get("workflow_name"))
                for trace_entry in trace_entries
                if isinstance(trace_entry, dict) and _safe_str(trace_entry.get("workflow_name"))
            }
        )
        trace_tools = sorted(
            {
                _safe_str(tool_call.get("name") or ((tool_call.get("function") or {}).get("name")))
                for trace_entry in trace_entries
                if isinstance(trace_entry, dict)
                for tool_call in (trace_entry.get("tool_calls") or [])
                if isinstance(tool_call, dict)
                and _safe_str(tool_call.get("name") or ((tool_call.get("function") or {}).get("name")))
            }
        )
        trace_handoffs = sorted(
            {
                (
                    f"{_safe_str((trace_entry.get('handoff') or {}).get('source_agent'))}"
                    f"->{_safe_str((trace_entry.get('handoff') or {}).get('target_agent'))}"
                )
                for trace_entry in trace_entries
                if isinstance(trace_entry, dict)
                and isinstance(trace_entry.get("handoff"), dict)
                and _safe_str((trace_entry.get("handoff") or {}).get("target_agent"))
            }
        )

        alerts: list[str] = []
        failure_count = int(metrics.get("failure_count") or 0)
        if not sessions:
            alerts.append("No projected runtime sessions yet. Execute a workflow to populate observability.")
        if failure_count > 0:
            alerts.append(f"{failure_count} failed runtime outcomes recorded in the current projection.")
        average_latency_ms = float(metrics.get("average_latency_ms") or 0.0)
        if average_latency_ms >= 1500:
            alerts.append(f"Average latency is elevated at {average_latency_ms:.0f} ms.")
        if not bool(observability.get("queue_healthy")):
            alerts.append(
                f"Queue backend '{_safe_str(observability.get('queue_backend')) or 'unknown'}' is unavailable."
            )
        validation = observability.get("validation") if isinstance(observability.get("validation"), dict) else {}
        validation_errors = [str(item) for item in (validation.get("errors") or []) if str(item)]
        router_parallel_status = (
            observability.get("router_parallel_status")
            if isinstance(observability.get("router_parallel_status"), dict)
            else {}
        )
        router_parallel_timeout_branches = int(router_parallel_status.get("total_timeout_branches") or 0)
        router_parallel_partial_fail_runs = int(router_parallel_status.get("total_partial_fail_runs") or 0)
        router_parallel_total_runs = int(router_parallel_status.get("total_parallel_runs") or 0)
        router_parallel_hard_timeout_runs = int(router_parallel_status.get("total_hard_timeout_runs") or 0)
        router_parallel_config = (
            router_parallel_status.get("config")
            if isinstance(router_parallel_status.get("config"), dict)
            else {}
        )
        if validation_errors:
            alerts.append(f"{len(validation_errors)} runtime contract validation issues projected.")
        if router_parallel_timeout_branches > 0:
            alerts.append(
                f"Router parallel branches reported {router_parallel_timeout_branches} timeout classifications."
            )
        if router_parallel_partial_fail_runs > 0:
            alerts.append(
                f"Router parallel mode recorded {router_parallel_partial_fail_runs} partial-fail runs."
            )
        if router_parallel_hard_timeout_runs > 0:
            alerts.append(
                f"Router parallel hard-timeout mode used in {router_parallel_hard_timeout_runs} runs."
            )
        recent_items = [
            _build_recent_projection_item(
                timestamp=_safe_str(event_object.get("timestamp")),
                title=_safe_str(event_object.get("event_type")) or "runtime.event",
                summary=_safe_str(event_object.get("summary")) or _safe_str(event_object.get("event_type")) or "runtime.event",
                source="runtime_monitoring",
                status=_safe_str(event_object.get("status")) or "info",
                audit_type=_safe_str(event_object.get("event_type")) or "runtime_event",
                action_group="runtime_monitoring",
                metadata=event_object,
            )
            for event_object in list(events[-12:])
            if isinstance(event_object, dict)
        ]
        summary_metrics = {
            "session_count": int(runtime_view.get("session_count") or 0),
            "event_count": int(runtime_view.get("event_count") or 0),
            "trace_count": int(runtime_view.get("trace_count") or len(trace_entries) or 0),
            "success_count": int(metrics.get("success_count") or 0),
            "failure_count": failure_count,
            "average_latency_ms": average_latency_ms,
            "active_session_count": int(observability.get("active_session_count") or 0),
            "validation_issue_count": len(validation_errors),
            "router_parallel_runs": router_parallel_total_runs,
            "router_parallel_timeout_branches": router_parallel_timeout_branches,
            "router_parallel_partial_fail_runs": router_parallel_partial_fail_runs,
            "router_parallel_hard_timeout_runs": router_parallel_hard_timeout_runs,
            "router_parallel_hard_timeout_enabled": bool(router_parallel_config.get("hard_timeout_enabled")),
        }

        return _build_projection_snapshot(
            snapshot_kind="monitoring",
            healthy=bool(observability.get("healthy")),
            alerts=alerts,
            summary_metrics=summary_metrics,
            recent_items=recent_items,
            detail_rows=list(latest_session_summaries),
            generated_at=_safe_str(runtime_view.get("generated_at")) or _utc_now_iso(),
            queue_backend=_safe_str(observability.get("queue_backend")) or "inmemory",
            queue_healthy=bool(observability.get("queue_healthy")),
            validation=validation,
            validation_issue_count=len(validation_errors),
            active_session_count=int(observability.get("active_session_count") or 0),
            router_parallel_status=router_parallel_status,
            router_parallel_enabled=bool(router_parallel_config.get("parallel_enabled")),
            latest_sessions=latest_session_summaries,
            observability=observability,
            session_count=int(runtime_view.get("session_count") or 0),
            event_count=int(runtime_view.get("event_count") or 0),
            trace_count=int(runtime_view.get("trace_count") or len(trace_entries) or 0),
            success_count=int(metrics.get("success_count") or 0),
            failure_count=failure_count,
            average_latency_ms=average_latency_ms,
            latest_session=latest_session,
            events=list(events[-12:]),
            trace=list(trace_entries[-24:]),
            trace_filter_options={
                "agents": trace_agents,
                "workflows": trace_workflows,
                "tools": trace_tools,
                "handoffs": trace_handoffs,
            },
        )


class ControlPlaneSnapshotExportService:
    def __init__(
        self,
        projection_service: RuntimeProjectionService,
        view_service: RuntimeViewService,
        monitoring_service: DesktopMonitoringSnapshotService,
        operator_service: OperatorStatusService,
    ) -> None:
        self._projection_service = projection_service
        self._view_service = view_service
        self._monitoring_service = monitoring_service
        self._operator_service = operator_service

    def _write_snapshot_file(
        self,
        *,
        base_dir: str | None,
        file_name: str,
        payload: dict[str, Any],
    ) -> str:
        target_root = self._projection_service.load_generated_root(base_dir)
        target_root.mkdir(parents=True, exist_ok=True)
        target_path = target_root / file_name
        with open(target_path, "w", encoding="utf-8") as snapshot_file:
            json.dump(_json_safe_copy(payload), snapshot_file, ensure_ascii=False, indent=2)
        return str(target_path)

    def export_desktop_monitoring_snapshot(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        event_limit: int | None = None,
        trace_limit: int | None = None,
    ) -> str:
        snapshot = self._monitoring_service.load_snapshot(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )
        file_name = f"monitoring_snapshot_{session_id}.json" if session_id else "monitoring_snapshot_latest.json"
        return self._write_snapshot_file(base_dir=base_dir, file_name=file_name, payload=snapshot)

    def export_operator_status_snapshot(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        mcp_probe: dict[str, Any] | None = None,
        recent_action_entries: list[Any] | None = None,
    ) -> str:
        snapshot = self._operator_service.load_snapshot(
            mcp_probe=mcp_probe,
            recent_action_entries=recent_action_entries,
        )
        file_name = f"operator_snapshot_{session_id}.json" if session_id else "operator_snapshot_latest.json"
        return self._write_snapshot_file(base_dir=base_dir, file_name=file_name, payload=snapshot)

    def export_control_plane_snapshot(
        self,
        *,
        base_dir: str | None = None,
        session_id: str | None = None,
        history_entries: list[dict[str, Any]] | None = None,
        event_limit: int | None = None,
        trace_limit: int | None = None,
        mcp_probe: dict[str, Any] | None = None,
        recent_action_entries: list[Any] | None = None,
    ) -> str:
        runtime_view = self._view_service.load_runtime_view(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )
        monitoring_snapshot = self._monitoring_service.load_snapshot(
            base_dir=base_dir,
            session_id=session_id,
            history_entries=history_entries,
            event_limit=event_limit,
            trace_limit=trace_limit,
        )
        operator_snapshot = self._operator_service.load_snapshot(
            mcp_probe=mcp_probe,
            recent_action_entries=recent_action_entries,
        )

        merged_recent_items = [
            dict(item)
            for item in [
                *(monitoring_snapshot.get("recent_items") or []),
                *(operator_snapshot.get("recent_items") or []),
            ]
            if isinstance(item, dict)
        ]
        merged_recent_items = sorted(
            merged_recent_items,
            key=lambda item: (_safe_str(item.get("timestamp")), _safe_str(item.get("title"))),
            reverse=True,
        )[:12]

        combined_alerts: list[str] = []
        for alert in [*(monitoring_snapshot.get("alerts") or []), *(operator_snapshot.get("alerts") or [])]:
            alert_text = _safe_str(alert)
            if alert_text and alert_text not in combined_alerts:
                combined_alerts.append(alert_text)

        detail_rows = [
            {
                "title": "Monitoring",
                "state": "pass" if bool(monitoring_snapshot.get("healthy")) else "fail",
                "detail": f"sessions={int(monitoring_snapshot.get('session_count') or 0)} events={int(monitoring_snapshot.get('event_count') or 0)}",
                "note": f"alerts={int(monitoring_snapshot.get('attention_count') or 0)} recent={int(monitoring_snapshot.get('recent_item_count') or 0)}",
                "healthy": bool(monitoring_snapshot.get("healthy")),
            },
            {
                "title": "Operator",
                "state": "pass" if bool(operator_snapshot.get("healthy")) else "fail",
                "detail": f"services={int(operator_snapshot.get('service_count') or 0)} healthy={int(operator_snapshot.get('healthy_service_count') or 0)}",
                "note": f"alerts={int(operator_snapshot.get('attention_count') or 0)} recent={int(operator_snapshot.get('recent_item_count') or 0)}",
                "healthy": bool(operator_snapshot.get("healthy")),
            },
        ]
        bundle = _build_projection_snapshot(
            snapshot_kind="control_plane_bundle",
            healthy=bool(monitoring_snapshot.get("healthy") and operator_snapshot.get("healthy")),
            alerts=combined_alerts[:12],
            summary_metrics={
                "session_count": int(runtime_view.get("session_count") or 0),
                "event_count": int(runtime_view.get("event_count") or 0),
                "trace_count": int(runtime_view.get("trace_count") or 0),
                "monitoring_attention_count": int(monitoring_snapshot.get("attention_count") or 0),
                "operator_attention_count": int(operator_snapshot.get("attention_count") or 0),
                "monitoring_recent_item_count": int(monitoring_snapshot.get("recent_item_count") or 0),
                "operator_recent_item_count": int(operator_snapshot.get("recent_item_count") or 0),
            },
            recent_items=merged_recent_items,
            detail_rows=detail_rows,
            runtime_view=runtime_view,
            monitoring=monitoring_snapshot,
            operator=operator_snapshot,
        )
        file_name = f"control_plane_snapshot_{session_id}.json" if session_id else "control_plane_snapshot_latest.json"
        return self._write_snapshot_file(base_dir=base_dir, file_name=file_name, payload=bundle)


RUNTIME_PROJECTION_SERVICE = RuntimeProjectionService()
RUNTIME_METRICS_SERVICE = RuntimeMetricsService(RUNTIME_PROJECTION_SERVICE)
RUNTIME_VIEW_SERVICE = RuntimeViewService(RUNTIME_PROJECTION_SERVICE, RUNTIME_METRICS_SERVICE)
WORKFLOW_VALIDATION_SERVICE = WorkflowValidationService()
WORKFLOW_STATUS_SERVICE = WorkflowStatusService(WORKFLOW_VALIDATION_SERVICE)
QUEUE_HEALTH_SERVICE = QueueHealthService()
OPERATOR_STATUS_SERVICE = OperatorStatusService(
    WORKFLOW_VALIDATION_SERVICE,
    QUEUE_HEALTH_SERVICE,
)
RUNTIME_OBSERVABILITY_SERVICE = RuntimeObservabilityService(
    RUNTIME_VIEW_SERVICE,
    WORKFLOW_VALIDATION_SERVICE,
    QUEUE_HEALTH_SERVICE,
)
DESKTOP_MONITORING_SNAPSHOT_SERVICE = DesktopMonitoringSnapshotService(
    RUNTIME_VIEW_SERVICE,
    RUNTIME_OBSERVABILITY_SERVICE,
)
CONTROL_PLANE_SNAPSHOT_EXPORT_SERVICE = ControlPlaneSnapshotExportService(
    RUNTIME_PROJECTION_SERVICE,
    RUNTIME_VIEW_SERVICE,
    DESKTOP_MONITORING_SNAPSHOT_SERVICE,
    OPERATOR_STATUS_SERVICE,
)


def load_runtime_view(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    event_limit: int | None = None,
    trace_limit: int | None = None,
) -> dict[str, Any]:
    return RUNTIME_VIEW_SERVICE.load_runtime_view(
        base_dir=base_dir,
        session_id=session_id,
        history_entries=history_entries,
        event_limit=event_limit,
        trace_limit=trace_limit,
    )


def load_runtime_trace(
    *,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    trace_limit: int | None = None,
) -> list[dict[str, Any]]:
    return RUNTIME_PROJECTION_SERVICE.load_history_trace(
        session_id=session_id,
        history_entries=history_entries,
        trace_limit=trace_limit,
    )


def export_runtime_view(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    event_limit: int | None = None,
    trace_limit: int | None = None,
) -> str:
    return RUNTIME_VIEW_SERVICE.export_runtime_view(
        base_dir=base_dir,
        session_id=session_id,
        history_entries=history_entries,
        event_limit=event_limit,
        trace_limit=trace_limit,
    )


def export_desktop_monitoring_snapshot(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    event_limit: int | None = None,
    trace_limit: int | None = None,
) -> str:
    return CONTROL_PLANE_SNAPSHOT_EXPORT_SERVICE.export_desktop_monitoring_snapshot(
        base_dir=base_dir,
        session_id=session_id,
        history_entries=history_entries,
        event_limit=event_limit,
        trace_limit=trace_limit,
    )


def export_operator_status_snapshot(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    mcp_probe: dict[str, Any] | None = None,
    recent_action_entries: list[Any] | None = None,
) -> str:
    return CONTROL_PLANE_SNAPSHOT_EXPORT_SERVICE.export_operator_status_snapshot(
        base_dir=base_dir,
        session_id=session_id,
        mcp_probe=mcp_probe,
        recent_action_entries=recent_action_entries,
    )


def export_control_plane_snapshot(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    event_limit: int | None = None,
    trace_limit: int | None = None,
    mcp_probe: dict[str, Any] | None = None,
    recent_action_entries: list[Any] | None = None,
) -> str:
    return CONTROL_PLANE_SNAPSHOT_EXPORT_SERVICE.export_control_plane_snapshot(
        base_dir=base_dir,
        session_id=session_id,
        history_entries=history_entries,
        event_limit=event_limit,
        trace_limit=trace_limit,
        mcp_probe=mcp_probe,
        recent_action_entries=recent_action_entries,
    )


def load_runtime_observability_snapshot(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    event_limit: int | None = None,
    trace_limit: int | None = None,
) -> dict[str, Any]:
    return RUNTIME_OBSERVABILITY_SERVICE.load_snapshot(
        base_dir=base_dir,
        session_id=session_id,
        history_entries=history_entries,
        event_limit=event_limit,
        trace_limit=trace_limit,
    )


def load_operator_status_snapshot(
    *,
    mcp_probe: dict[str, Any] | None = None,
    recent_action_entries: list[Any] | None = None,
) -> dict[str, Any]:
    return OPERATOR_STATUS_SERVICE.load_snapshot(
        mcp_probe=mcp_probe,
        recent_action_entries=recent_action_entries,
    )


def load_desktop_monitoring_snapshot(
    *,
    base_dir: str | None = None,
    session_id: str | None = None,
    history_entries: list[dict[str, Any]] | None = None,
    event_limit: int | None = None,
    trace_limit: int | None = None,
) -> dict[str, Any]:
    return DESKTOP_MONITORING_SNAPSHOT_SERVICE.load_snapshot(
        base_dir=base_dir,
        session_id=session_id,
        history_entries=history_entries,
        event_limit=event_limit,
        trace_limit=trace_limit,
    )


def get_workflow_validation_report() -> dict[str, Any]:
    return WORKFLOW_VALIDATION_SERVICE.load_report()


def get_workflow_status_view(
    *,
    target_agent: str | None = None,
    workflow_name: str | None = None,
    thread_id: int | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    return WORKFLOW_STATUS_SERVICE.load_status_view(
        target_agent=target_agent,
        workflow_name=workflow_name,
        thread_id=thread_id,
        limit=limit,
    )


def _enrich_workflow_status_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    return WORKFLOW_STATUS_SERVICE.enrich_status_entry(entry)


def get_queue_health() -> tuple[str, bool]:
    return QUEUE_HEALTH_SERVICE.load_queue_health()


__all__ = [
    "_enrich_workflow_status_entry",
    "export_control_plane_snapshot",
    "export_desktop_monitoring_snapshot",
    "export_operator_status_snapshot",
    "export_runtime_view",
    "get_queue_health",
    "get_workflow_status_view",
    "get_workflow_validation_report",
    "load_desktop_monitoring_snapshot",
    "load_operator_status_snapshot",
    "load_runtime_observability_snapshot",
    "load_runtime_trace",
    "load_runtime_view",
]