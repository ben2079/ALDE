"""  """
from __future__ import annotations

"""
Clean, import-safe implementation of ClosableTextEdit, JsonTreeWidget and
JsonHighlighter. This file replaces a broken version that caused import
failures due to stray top-level code and invalid references to `self`.

Features preserved:
- ClosableTextEdit: QTextEdit with a small toolbar button to load ChatHistory
- JsonTreeWidget: QTreeWidget that can display JSON/Python data structures
- JsonHighlighter: lightweight QSyntaxHighlighter for JSON-like text

This module intentionally keeps runtime behavior minimal so importing it
won't trigger heavy work. UI interactions (e.g. loading history) assume
`ChatHistory._history_` may exist in `chat_completion` module; if not,
buttons will show a placeholder message.
"""

from typing import Any, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone, timedelta
import hashlib
import importlib
import importlib.util
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot, QSize
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPixmap,
    QTextCharFormat,
    QSyntaxHighlighter,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QLabel,
    QToolButton,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QDockWidget,
    QMessageBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QSizePolicy,
)

try:
    from .agents_db import AgentDbSocketRepository, KnowledgeRepository, UiAgentDbSocketRepository, load_agentsdb_runtime_config_from_env, normalize_agentsdb_socket_uri, sync_parser_result_to_agentsdb_knowledge  # type: ignore
except Exception:
    try:
        from alde.agents_db import AgentDbSocketRepository, KnowledgeRepository, UiAgentDbSocketRepository, load_agentsdb_runtime_config_from_env, normalize_agentsdb_socket_uri, sync_parser_result_to_agentsdb_knowledge  # type: ignore
    except Exception:
        AgentDbSocketRepository = None  # type: ignore[assignment]
        KnowledgeRepository = None  # type: ignore[assignment]
        UiAgentDbSocketRepository = None  # type: ignore[assignment]
        load_agentsdb_runtime_config_from_env = None  # type: ignore[assignment]
        normalize_agentsdb_socket_uri = None  # type: ignore[assignment]
        sync_parser_result_to_agentsdb_knowledge = None  # type: ignore[assignment]

# Try to import ChatHistory if available; keep optional.
try:
    from .chat_completion import ChatHistory  # type: ignore
except Exception:  # allow running as script from repo root
    try:
        from alde.chat_completion import ChatHistory  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        ChatHistory = None  # type: ignore

try:
    from .ui_glyphs import (
        DROPDOWN_COLLAPSED_GLYPH,
        DROPDOWN_COLLAPSED_PREFIX,
        DROPDOWN_EXPANDED_GLYPH,
        DROPDOWN_EXPANDED_PREFIX,
        dropdown_prefix,
    )
except Exception:
    try:
        from alde.ui_glyphs import (
            DROPDOWN_COLLAPSED_GLYPH,
            DROPDOWN_COLLAPSED_PREFIX,
            DROPDOWN_EXPANDED_GLYPH,
            DROPDOWN_EXPANDED_PREFIX,
            dropdown_prefix,
        )
    except Exception:  # pragma: no cover - keep imports resilient in direct-script mode
        DROPDOWN_EXPANDED_GLYPH = "▾"
        DROPDOWN_COLLAPSED_GLYPH = "▸"
        DROPDOWN_EXPANDED_PREFIX = "▾ "
        DROPDOWN_COLLAPSED_PREFIX = "▸ "

        def dropdown_prefix(expanded: bool) -> str:
            return DROPDOWN_EXPANDED_PREFIX if bool(expanded) else DROPDOWN_COLLAPSED_PREFIX


def _load_optional_mongo_client_class() -> Any | None:
    try:
        pymongo_module = importlib.import_module("pymongo")
    except Exception:
        return None
    return getattr(pymongo_module, "MongoClient", None)


class _TreePushStreamBridge(QObject):
    update_received = Signal(object, object)
    stream_error = Signal(str)


class TreeDataPersistenceService:
    """Persist tree data either in AgentDB or in a local JSON fallback file."""

    _ENV_ASSIGNMENT_PATTERN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    _ENV_OBJECT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _MCP_SECTION_NAME = "MCP"
    _AGENTSDB_REPOSITORY_SECTION_NAME = "DATABASES"
    _AGENTSDB_REPOSITORY_SECTION_KEY = "agentsdb_repository"
    _TREE_AGENTSDB_URI_PATTERN = re.compile(
        r"^(?:agents?db)(?:://|::)?(?P<host>\[[^\]]+\]|[A-Za-z0-9._-]+)?(?::(?P<port>\d+))?(?::*)?$",
        re.IGNORECASE,
    )

    _AI_IDE_SECTION_NAME_ORDER: tuple[str, ...] = (
        "RUNTIME",
        "CHAT_HISTORY",
        "DATABASES",
        "MCP",
        "ENV",
    )
    _AI_IDE_SECTION_NAME_ORDER_ENV_NAME = "AI_IDE_SECTION_NAME_ORDER"
    _PROJECTION_SOURCE_OBJECT_DEFINITION: tuple[dict[str, Any], ...] = (
        {"section": "ENV", "key": ".env.json", "kind": "env_file", "file_name": ".env.json"},
        {"section": "ENV", "key": "gui_env.json", "kind": "json_file", "file_name": "gui_env.json"},
        {"section": "RUNTIME_VIEWS", "key": "runtime_tabs", "kind": "json_file", "file_name": "control_plane_runtime_tabs.json"},
        {"section": "DISPATCHER_DB", "key": "dispatcher_doc_db", "kind": "json_file", "file_name": "dispatcher_doc_db.json"},
        {"section": "DATABASES", "key": "agentsdb_connection", "kind": "json_file", "file_name": "agentsdb_connection.json"},
        {"section": "DOCUMENTS", "key": "job_postings_db", "kind": "json_file", "file_name": "job_postings_db.json"},
        {"section": "DOCUMENTS", "key": "profiles_db", "kind": "json_file", "file_name": "profiles_db.json"},
        {"section": "DOCUMENTS", "key": "candidate_profiles_db", "kind": "json_file", "file_name": "candidate_profiles_db.json"},
        {"section": "TEMPLATES", "key": "template_files", "kind": "directory_index", "dir_name": "templates", "pattern": "*.json"},
        {"section": "GENERATED_DATA", "key": "generated_files", "kind": "directory_index", "dir_name": "generated", "pattern": "*"},
        {"section": "CHAT_HISTORY", "key": "chat_history", "kind": "chat_history"},
    )
    _PROJECTION_SOURCES_ENV_NAME = "AI_IDE_TREE_PROJECTION_SOURCES_JSON"
    _PROJECTION_SOURCES_PATH_ENV_NAME = "AI_IDE_TREE_PROJECTION_SOURCES_PATH"
    _PROJECTION_APPEND_SOURCES_ENV_NAME = "AI_IDE_TREE_PROJECTION_APPEND_SOURCES_JSON"
    _PROJECTION_APPEND_SOURCES_PATH_ENV_NAME = "AI_IDE_TREE_PROJECTION_APPEND_SOURCES_PATH"
    _AGENTS_DB_SOURCES_ENV_NAME = "AI_IDE_AGENTS_DB_SOURCES"
    _AGENTS_DB_SOURCES_JSON_ENV_NAME = "AI_IDE_AGENTS_DB_SOURCES_JSON"
    _DEFAULT_SECTION_ALLOWLIST: tuple[str, ...] = (
        "RUNTIME",
        "CHAT_HISTORY",
        "DATABASES",
        "ENV",
        "MCP"
    )
    _DEFAULT_HISTORY_RETENTION_DAYS = 28
    _DEFAULT_HISTORY_MAX_ITEMS = 2000
    _HISTORY_SECTION_NAMES: tuple[str, ...] = ("CHAT_HISTORY", "HISTORY")

    def __init__(self, app_data_dir: Path) -> None:
        self._app_data_dir = app_data_dir
        self._json_path = app_data_dir / "tree_data.json"
        self._mongo_client: Any | None = None
        self._mongo_collection: Any | None = None
        self._mongo_disabled = False
        self._storage_config_cache: dict[str, Any] | None = None
        self._last_stream_cursor: dict[str, Any] | None = None
        self._inmemory_tree_data: dict[str, Any] = self._normalize_tree_data_structure({})
        self._inmemory_tree_hash: str | None = self._tree_data_content_hash(self._inmemory_tree_data)

    def memory_only_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_TREE_MEMORY_ONLY", "1")).strip().lower()
        if not value:
            value = "1"
        return value in {"1", "true", "yes", "on", "memory", "inmemory"}

    def _agentsdb_strict_mode(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_STRICT", "1")).strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _agentsdb_pipeline_strict_mode(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_PIPELINE_STRICT", "1")).strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _parse_bool_value(self, value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on", "strict"}

    def _agentsdb_sources_config_payload(self) -> tuple[dict[str, Any] | list[Any] | None, bool]:
        raw_payload = ""
        for env_name in (self._AGENTS_DB_SOURCES_ENV_NAME, self._AGENTS_DB_SOURCES_JSON_ENV_NAME):
            candidate = str(os.getenv(env_name, "") or "").strip()
            if candidate:
                raw_payload = candidate
                break
        if not raw_payload:
            return None, False

        try:
            parsed_payload = json.loads(raw_payload)
        except Exception:
            return None, True

        if isinstance(parsed_payload, (dict, list)):
            return parsed_payload, True
        return None, True

    def _agentsdb_sources_field_allowlist(self) -> dict[str, set[str]]:
        parsed_payload, _has_config = self._agentsdb_sources_config_payload()
        if not isinstance(parsed_payload, dict):
            return {}

        allowlist_payload = parsed_payload.get("allowlist") if isinstance(parsed_payload.get("allowlist"), Mapping) else {}
        field_map_payload: Any = (
            allowlist_payload.get("fields")
            if isinstance(allowlist_payload.get("fields"), Mapping)
            else parsed_payload.get("allowlist_fields")
        )
        if not isinstance(field_map_payload, Mapping):
            return {}

        field_allowlist_map: dict[str, set[str]] = {}
        for object_name, field_values in field_map_payload.items():
            normalized_object_name = str(object_name or "").strip().lower()
            if not normalized_object_name:
                continue
            if isinstance(field_values, (str, bytes)):
                normalized_fields = {str(field_values).strip()} if str(field_values).strip() else set()
            elif isinstance(field_values, Sequence):
                normalized_fields = {
                    str(field_name).strip()
                    for field_name in field_values
                    if str(field_name).strip()
                }
            else:
                normalized_fields = set()
            if normalized_fields:
                field_allowlist_map[normalized_object_name] = normalized_fields
        return field_allowlist_map

    def _default_strict_projection_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "section": "DATABASES",
                "key": "agents_documents_recent",
                "kind": "agentsdb_query",
                "object_name": "document",
                "fields": ["_id", "title", "updated_at", "source_uri", "notes"],
                "limit": 200,
            },
            {
                "section": "DATABASES",
                "key": "agents_entities_recent",
                "kind": "agentsdb_query",
                "object_name": "entity",
                "fields": ["_id", "entity_id", "entity_type", "updated_at", "notes"],
                "limit": 200,
            },
            {
                "section": "DATABASES",
                "key": "agents_relations_recent",
                "kind": "agentsdb_query",
                "object_name": "relation",
                "fields": ["_id", "source_entity_id", "target_entity_id", "relation_type", "updated_at", "notes"],
                "limit": 200,
            },
        ]

    def _default_strict_field_allowlist(self) -> dict[str, set[str]]:
        return {
            "document": {"_id", "title", "updated_at", "source_uri", "notes"},
            "entity": {"_id", "entity_id", "entity_type", "updated_at", "notes"},
            "relation": {"_id", "source_entity_id", "target_entity_id", "relation_type", "updated_at", "notes"},
        }

    def _projection_db_only_strict_mode(self) -> bool:
        parsed_payload, has_config = self._agentsdb_sources_config_payload()
        if not self._agentsdb_pipeline_strict_mode():
            return False
        if isinstance(parsed_payload, dict) and "strict" in parsed_payload:
            return self._parse_bool_value(parsed_payload.get("strict"), default=True)
        return has_config

    def _strict_projection_sources_override(self) -> list[dict[str, Any]] | None:
        parsed_payload, has_config = self._agentsdb_sources_config_payload()
        if not has_config:
            return None
        loaded_sources = self._load_projection_sources_payload(parsed_payload)
        if isinstance(loaded_sources, list) and loaded_sources:
            return loaded_sources
        if self._projection_db_only_strict_mode():
            return self._default_strict_projection_sources()
        return None

    def _strict_projection_field_allowlist(self) -> dict[str, set[str]]:
        configured_allowlist = self._agentsdb_sources_field_allowlist()
        if configured_allowlist:
            return configured_allowlist
        if self._projection_db_only_strict_mode():
            return self._default_strict_field_allowlist()
        return {}

    def _agentsdb_tree_sync_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_SYNC", "")).strip().lower()
        if not value:
            value = str(self._load_storage_config().get("agentsdb_tree_sync", "1")).strip().lower()
        if not value:
            value = "1"
        return value in {"1", "true", "yes", "on"}

    def live_sync_enabled(self) -> bool:
        if self.memory_only_enabled():
            return self.uses_repository_projection()
        return self._agentsdb_tree_sync_enabled()

    def live_sync_interval_ms(self) -> int:
        raw_value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_POLL_MS", "5000") or "5000").strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = 5000
        return max(2000, resolved_value)

    def push_stream_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_PUSH_STREAM", "")).strip().lower()
        if not value:
            value = str(self._load_storage_config().get("agentsdb_tree_push_stream", "1")).strip().lower()
        if not value:
            value = "1"
        return value in {"1", "true", "yes", "on"}

    def supports_push_stream(self) -> bool:
        if not self.live_sync_enabled() or not self.push_stream_enabled():
            return False
        runtime_config, repository = self._load_agentsdb_repository()
        if self._should_project_agentsdb_repository(repository):
            return runtime_config is not None and repository is not None and callable(getattr(repository, "subscribe_repository_stream", None))
        return runtime_config is not None and repository is not None and callable(getattr(repository, "subscribe_tree_stream", None))

    def uses_repository_projection(self) -> bool:
        _runtime_config, repository = self._load_agentsdb_repository()
        return self._should_project_agentsdb_repository(repository)

    def live_sync_backend_name(self) -> str:
        return "agents_db_repository" if self.uses_repository_projection() else "agents_db_live"

    def live_sync_source_label(self) -> str:
        return "agents_db_repository" if self.uses_repository_projection() else "agents_db"

    def live_sync_source(self) -> str:
        runtime_config, repository = self._load_agentsdb_repository()
        if self._should_project_agentsdb_repository(repository):
            return self._agentsdb_repository_source(runtime_config)
        if self.memory_only_enabled():
            return "inmemory"
        return self._tree_object_id()

    def _memory_view_data(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._strip_derived_agentsdb_repository_sections(
            self._normalize_tree_data_structure(data)
        )
        return self._apply_tree_storage_projection_policy(normalized_data)

    def _store_inmemory_tree_data(
        self,
        data: dict[str, Any],
        *,
        change_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_data = self._memory_view_data(data)
        self._inmemory_tree_data = normalized_data
        self._inmemory_tree_hash = self._tree_data_content_hash(normalized_data)
        stream_cursor = self._build_tree_stream_cursor(
            normalized_data,
            change_event or {"action": "memory_update", "origin": "tree_widget"},
        )
        self._last_stream_cursor = self._normalize_tree_stream_cursor(stream_cursor)
        return dict(normalized_data)

    def _normalize_projection_conflict_policy_value(self, value: Any) -> str | None:
        policy_value = str(value or "").strip().lower()
        if not policy_value:
            return None
        alias_map = {
            "agents_db_first": "agentsdb_first",
            "agentsdb": "agentsdb_first",
            "db_first": "agentsdb_first",
            "local": "local_first",
            "local_db_first": "local_first",
            "newest": "newest_wins",
            "latest": "newest_wins",
            "strict": "agentsdb_strict",
            "agents_db_strict": "agentsdb_strict",
        }
        resolved_policy = alias_map.get(policy_value, policy_value)
        if resolved_policy not in {"agentsdb_first", "local_first", "newest_wins", "agentsdb_strict"}:
            return None
        return resolved_policy

    def _projection_conflict_policy(self) -> str:
        if self._projection_db_only_strict_mode():
            return "agentsdb_strict"
        default_policy = "newest_wins"
        configured_policy = str(os.getenv("AI_IDE_AGENTS_DB_PROJECTION_CONFLICT_POLICY", "")).strip().lower()
        if not configured_policy:
            configured_policy = str(self._load_storage_config().get("projection_conflict_policy", "")).strip().lower()
        if not configured_policy:
            return default_policy
        normalized_policy = self._normalize_projection_conflict_policy_value(configured_policy)
        return normalized_policy or default_policy

    def _timestamp_from_iso(self, value: Any) -> datetime | None:
        timestamp = str(value or "").strip()
        if not timestamp:
            return None
        if timestamp.endswith("Z"):
            timestamp = f"{timestamp[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(timestamp)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _mtime_iso(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            if not path.exists():
                return None
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except Exception:
            return None

    def _chat_history_source_path(self) -> Path | None:
        if ChatHistory is None:
            return None
        for attribute_name in ("_HISTORY_PATH", "_LAST_READ_HISTORY_PATH", "_FINAL_PATH"):
            try:
                raw_path = str(getattr(ChatHistory, attribute_name, "") or "").strip()
            except Exception:
                raw_path = ""
            if raw_path:
                path = Path(raw_path)
                if path.exists() and path.is_file():
                    return path
        return None

    def _tree_data_content_hash(self, data: Any) -> str:
        normalized_data = self._normalize_tree_data_structure(data)
        safe_data = self._json_safe_projection_data(normalized_data)
        serialized_data = json.dumps(safe_data, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(serialized_data.encode("utf-8")).hexdigest()

    def _normalize_tree_agentsdb_uri(self, uri: Any) -> str:
        if callable(normalize_agentsdb_socket_uri):
            return normalize_agentsdb_socket_uri(uri)
        normalized_uri = str(uri or "").strip()
        if not normalized_uri:
            return "agentsdb://127.0.0.1:2331"

        loose_match = self._TREE_AGENTSDB_URI_PATTERN.match(normalized_uri)
        if loose_match is not None:
            resolved_host = str(loose_match.group("host") or "").strip().strip("[]").lower()
            resolved_port_text = str(loose_match.group("port") or "").strip()
            try:
                resolved_port = int(resolved_port_text or 2331)
            except Exception:
                resolved_port = 2331
            if resolved_host in {"", "localhost", "127.0.0.1", "::1"}:
                resolved_host = "127.0.0.1"
            return f"agentsdb://{resolved_host or '127.0.0.1'}:{resolved_port}"

        parsed_uri = urlparse(normalized_uri)
        if str(parsed_uri.scheme or "").strip().lower() != "agentsdb":
            return normalized_uri
        resolved_host = str(parsed_uri.hostname or "").strip().lower()
        if resolved_host in {"", "localhost", "127.0.0.1"}:
            resolved_port = int(parsed_uri.port or 2331)
            return f"agentsdb://127.0.0.1:{resolved_port}"
        return normalized_uri

    def _load_agentsdb_repository(self) -> tuple[Any, Any] | tuple[None, None]:
        if not callable(load_agentsdb_runtime_config_from_env):
            return None, None
        runtime_config = load_agentsdb_runtime_config_from_env()
        if runtime_config is None:
            return None, None
        uri = self._normalize_tree_agentsdb_uri(getattr(runtime_config, "agents_db_uri", "") or "")
        if uri and hasattr(runtime_config, "agents_db_uri"):
            try:
                runtime_config.agents_db_uri = uri
            except Exception:
                pass
        database_name = str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge"
        if not uri:
            return None, None
        if uri.lower().startswith("agentsdb://"):
            repository_class = UiAgentDbSocketRepository or AgentDbSocketRepository
            if repository_class is None:
                return None, None
            return runtime_config, repository_class.create_from_uri(uri, database_name)
        if KnowledgeRepository is None:
            return None, None
        return runtime_config, KnowledgeRepository.create_from_uri(uri, database_name)

    def _tree_object_id(self) -> str:
        configured_id = str(os.getenv("AI_IDE_TREE_AGENTS_DB_OBJECT_ID", "")).strip()
        return configured_id or "tree_widget:tree_data"

    def _tree_stream_head_object_id(self) -> str:
        return f"{self._tree_object_id()}:stream:head"

    def _tree_stream_event_object_id(self, event_id: str) -> str:
        normalized_event_id = str(event_id or "").strip() or "snapshot"
        return f"{self._tree_object_id()}:stream:event:{normalized_event_id}"

    def _normalize_tree_stream_cursor(self, cursor: Any) -> dict[str, Any] | None:
        if not isinstance(cursor, dict):
            return None
        event_id = str(cursor.get("event_id") or "").strip()
        updated_at = str(cursor.get("updated_at") or cursor.get("created_at") or "").strip()
        tree_hash = str(cursor.get("tree_hash") or cursor.get("content_sha256") or "").strip()
        if not event_id and not updated_at and not tree_hash:
            return None
        return {
            "event_id": event_id,
            "updated_at": updated_at,
            "tree_hash": tree_hash,
        }

    def load_last_stream_cursor(self) -> dict[str, Any] | None:
        if not isinstance(self._last_stream_cursor, dict):
            return None
        return dict(self._last_stream_cursor)

    def _load_tree_record_from_agentsdb(self, repository: Any | None) -> dict[str, Any] | None:
        if repository is None:
            return None
        try:
            record = repository.load_object("document", self._tree_object_id())
        except Exception:
            return None
        return record if isinstance(record, dict) else None

    def _extract_tree_payload_from_record(self, record: Any) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            return None
        tree_payload = record.get("tree_data")
        if not isinstance(tree_payload, dict):
            tree_payload = record.get("data")
        return tree_payload if isinstance(tree_payload, dict) else None

    def _tree_stream_cursor_from_tree_record(self, record: Any) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            return None
        embedded_cursor = self._normalize_tree_stream_cursor(record.get("stream_cursor"))
        if embedded_cursor is not None:
            return embedded_cursor
        tree_payload = self._extract_tree_payload_from_record(record)
        tree_hash = str(record.get("content_sha256") or "").strip()
        if not tree_hash and isinstance(tree_payload, dict):
            tree_hash = self._tree_data_content_hash(tree_payload)
        return self._normalize_tree_stream_cursor(
            {
                "event_id": str(record.get("last_stream_event_id") or "").strip(),
                "updated_at": str(record.get("updated_at") or record.get("created_at") or "").strip(),
                "tree_hash": tree_hash,
            }
        )

    def _load_tree_stream_head(self, repository: Any | None) -> dict[str, Any] | None:
        if repository is None:
            return None
        try:
            head_payload = repository.load_object("document", self._tree_stream_head_object_id())
        except Exception:
            return None
        return head_payload if isinstance(head_payload, dict) else None

    def _load_tree_stream_cursor_from_agentsdb(self, repository: Any | None) -> dict[str, Any] | None:
        head_payload = self._load_tree_stream_head(repository)
        normalized_head_cursor = self._normalize_tree_stream_cursor(head_payload)
        if normalized_head_cursor is not None:
            return normalized_head_cursor
        return self._tree_stream_cursor_from_tree_record(self._load_tree_record_from_agentsdb(repository))

    def _purge_agentsdb_tree_object(self, repository: Any | None) -> bool:
        if repository is None:
            return False
        object_id = self._tree_object_id()
        try:
            delete_object = getattr(repository, "delete_object", None)
            if callable(delete_object):
                return bool(delete_object("document", object_id))
        except Exception:
            return False

        try:
            load_collection = getattr(repository, "load_collection", None)
            collection = load_collection("document") if callable(load_collection) else None
            if isinstance(collection, dict) and object_id in collection:
                collection.pop(object_id, None)
                flush_image = getattr(repository, "_flush_image", None)
                if callable(flush_image):
                    flush_image()
                return True
        except Exception:
            return False

        return False

    def _normalize_tree_data_structure(self, data: Any) -> dict[str, Any]:
        normalized_data: dict[str, Any] = {}
        if isinstance(data, dict):
            for section_name, section_payload in data.items():
                normalized_section_name = str(section_name or "").strip().upper()
                if not normalized_section_name:
                    continue
                normalized_data[normalized_section_name] = (
                    dict(section_payload) if isinstance(section_payload, dict) else {}
                )

        for section_name in self._resolved_ai_ide_section_name_order():
            normalized_data.setdefault(section_name, {})

        return normalized_data

    def _resolved_ai_ide_section_name_order(self) -> tuple[str, ...]:
        default_order = tuple(str(name).strip().upper() for name in self._AI_IDE_SECTION_NAME_ORDER if str(name).strip())
        raw_value = str(os.getenv(self._AI_IDE_SECTION_NAME_ORDER_ENV_NAME, "") or "").strip()
        if not raw_value:
            return default_order

        tokens: list[str] = []
        parsed_from_json = False
        if raw_value.startswith("["):
            try:
                parsed_payload = json.loads(raw_value)
            except Exception:
                parsed_payload = None
            if isinstance(parsed_payload, list):
                parsed_from_json = True
                tokens = [str(item or "").strip() for item in parsed_payload]

        if not parsed_from_json:
            tokens = [part.strip() for part in re.split(r"[,;|\s]+", raw_value) if part.strip()]

        ordered_unique: list[str] = []
        for token in tokens:
            normalized_token = str(token or "").strip().upper()
            if not normalized_token or normalized_token in ordered_unique:
                continue
            ordered_unique.append(normalized_token)

        if not ordered_unique:
            return default_order

        for default_section in default_order:
            if default_section not in ordered_unique:
                ordered_unique.append(default_section)
        return tuple(ordered_unique)

    def _tree_section_allowlist(self) -> set[str]:
        raw_value = str(os.getenv("AI_IDE_TREE_SECTION_ALLOWLIST", "")).strip()
        if raw_value:
            candidates = re.split(r"[\s,]+", raw_value)
            return {item.strip().upper() for item in candidates if item.strip()}
        return set(self._DEFAULT_SECTION_ALLOWLIST)

    def _history_retention_days(self) -> int:
        raw_value = str(os.getenv("AI_IDE_TREE_HISTORY_DAYS", str(self._DEFAULT_HISTORY_RETENTION_DAYS))).strip()
        try:
            return max(0, int(raw_value))
        except Exception:
            return self._DEFAULT_HISTORY_RETENTION_DAYS

    def _history_max_entries(self) -> int:
        raw_value = str(os.getenv("AI_IDE_TREE_HISTORY_MAX_ITEMS", str(self._DEFAULT_HISTORY_MAX_ITEMS))).strip()
        try:
            return max(0, int(raw_value))
        except Exception:
            return self._DEFAULT_HISTORY_MAX_ITEMS

    def _parse_history_timestamp(self, entry: Mapping[str, Any]) -> datetime | None:
        for key in ("timestamp", "updated_at", "created_at", "time", "date"):
            value = entry.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                try:
                    return datetime.fromtimestamp(float(value), timezone.utc)
                except Exception:
                    continue
            if not isinstance(value, str):
                value = str(value)
            text = value.strip()
            if not text:
                continue
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except Exception:
                parsed = None
            if parsed is None:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except Exception:
                        continue
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        return None

    def _trim_history_entries(self, entries: list[Any]) -> list[Any]:
        retention_days = self._history_retention_days()
        max_entries = self._history_max_entries()
        cutoff = None
        if retention_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        filtered: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                filtered.append(entry)
                continue
            if cutoff is None:
                filtered.append(entry)
                continue
            timestamp = self._parse_history_timestamp(entry)
            if timestamp is None or timestamp >= cutoff:
                filtered.append(entry)

        if max_entries > 0 and len(filtered) > max_entries:
            filtered = filtered[-max_entries:]
        return filtered

    def _trim_history_section(self, section_payload: dict[str, Any]) -> dict[str, Any]:
        trimmed_payload: dict[str, Any] = {}
        for key, value in section_payload.items():
            if isinstance(value, list):
                trimmed_payload[key] = self._trim_history_entries(value)
            else:
                trimmed_payload[key] = value
        return trimmed_payload

    def _prune_projects_section(self, section_payload: dict[str, Any]) -> dict[str, Any]:
        storage_payload = section_payload.get("tree_widget_storage")
        if isinstance(storage_payload, dict):
            return {"tree_widget_storage": dict(storage_payload)}
        return {"tree_widget_storage": {}}

    def _filter_tree_sections(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._normalize_tree_data_structure(data)
        allowed_sections = self._tree_section_allowlist()
        filtered: dict[str, Any] = {}
        ordered_section_names: list[str] = []

        for section_name in self._resolved_ai_ide_section_name_order():
            if section_name in normalized_data and section_name in allowed_sections and section_name not in ordered_section_names:
                ordered_section_names.append(section_name)

        for section_name in normalized_data.keys():
            if section_name in allowed_sections and section_name not in ordered_section_names:
                ordered_section_names.append(section_name)

        for section_name in ordered_section_names:
            section_payload = normalized_data.get(section_name)
            payload = section_payload if isinstance(section_payload, dict) else {}
            if section_name == "PROJECTS":
                payload = self._prune_projects_section(payload)
            if section_name in self._HISTORY_SECTION_NAMES:
                payload = self._trim_history_section(payload)
            filtered[section_name] = dict(payload)

        for section_name in ordered_section_names:
            filtered.setdefault(section_name, {})

        for section_name in sorted(allowed_sections):
            filtered.setdefault(section_name, {})

        return filtered

    def _projection_app_data_dir_list(self) -> list[Path]:
        candidate_dir_list: list[Path] = []
        workspace_app_data_dir = self._app_data_dir.parent.parent / "AppData"
        seen_path_set: set[str] = set()
        for candidate_dir in (workspace_app_data_dir, self._app_data_dir):
            try:
                resolved_dir = str(candidate_dir.resolve())
            except Exception:
                resolved_dir = str(candidate_dir)
            if resolved_dir in seen_path_set:
                continue
            seen_path_set.add(resolved_dir)
            if candidate_dir.is_dir():
                candidate_dir_list.append(candidate_dir)
        return candidate_dir_list

    def _projection_source_object_id(self, source_key: str) -> str:
        normalized_source_key = str(source_key or "projection").strip().lower()
        return f"ai_ide_projection:{normalized_source_key}"

    def _normalize_projection_source_object(self, source_object: Any) -> dict[str, Any] | None:
        if not isinstance(source_object, Mapping):
            return None

        section_name = str(source_object.get("section") or "").strip().upper()
        source_key = str(source_object.get("key") or "").strip()
        source_kind = str(source_object.get("kind") or "").strip().lower()
        if not section_name or not source_key or not source_kind:
            return None

        normalized_source: dict[str, Any] = {
            "section": section_name,
            "key": source_key,
            "kind": source_kind,
        }
        for optional_field in (
            "file_name",
            "file_path",
            "dir_name",
            "dir_path",
            "pattern",
            "object_name",
            "collection_name",
            "source_uri",
            "query",
            "filter",
            "fields",
            "sort_by",
            "limit",
            "max_entries",
        ):
            if optional_field in source_object:
                normalized_source[optional_field] = source_object.get(optional_field)
        return normalized_source

    def _load_projection_sources_payload(self, payload: Any) -> list[dict[str, Any]] | None:
        raw_sources: Any = None
        if isinstance(payload, list):
            raw_sources = payload
        elif isinstance(payload, Mapping):
            raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            return None

        normalized_sources: list[dict[str, Any]] = []
        for item in raw_sources:
            normalized_item = self._normalize_projection_source_object(item)
            if normalized_item is not None:
                normalized_sources.append(normalized_item)
        return normalized_sources

    def _projection_sources_path_candidates(self, configured_path: str) -> list[Path]:
        if not configured_path:
            return []
        raw_path = Path(configured_path).expanduser()
        if raw_path.is_absolute():
            return [raw_path]

        candidate_path_list = [
            (self._app_data_dir.parent / raw_path),
            (self._app_data_dir.parent.parent / raw_path),
            raw_path,
        ]
        unique_candidate_list: list[Path] = []
        seen_set: set[str] = set()
        for candidate_path in candidate_path_list:
            try:
                normalized_path = str(candidate_path.resolve())
            except Exception:
                normalized_path = str(candidate_path)
            if normalized_path in seen_set:
                continue
            seen_set.add(normalized_path)
            unique_candidate_list.append(candidate_path)
        return unique_candidate_list

    def _load_projection_sources_from_env(
        self,
        *,
        inline_env_name: str,
        path_env_name: str,
    ) -> list[dict[str, Any]] | None:
        inline_payload = str(os.getenv(inline_env_name, "") or "").strip()
        if inline_payload:
            try:
                parsed_payload = json.loads(inline_payload)
            except Exception:
                parsed_payload = None
            loaded_sources = self._load_projection_sources_payload(parsed_payload)
            if loaded_sources is not None:
                return loaded_sources

        configured_path = str(os.getenv(path_env_name, "") or "").strip()
        if not configured_path:
            return None
        for candidate_path in self._projection_sources_path_candidates(configured_path):
            if not candidate_path.exists() or not candidate_path.is_file():
                continue
            try:
                parsed_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            loaded_sources = self._load_projection_sources_payload(parsed_payload)
            if loaded_sources is not None:
                return loaded_sources
        return None

    def _projection_source_object_definition(self) -> tuple[dict[str, Any], ...]:
        default_sources = [
            dict(source_object)
            for source_object in self._PROJECTION_SOURCE_OBJECT_DEFINITION
            if isinstance(source_object, Mapping)
        ]

        strict_override_sources = self._strict_projection_sources_override()
        if isinstance(strict_override_sources, list):
            effective_sources = strict_override_sources
        else:
            effective_sources = None

        override_sources = self._load_projection_sources_from_env(
            inline_env_name=self._PROJECTION_SOURCES_ENV_NAME,
            path_env_name=self._PROJECTION_SOURCES_PATH_ENV_NAME,
        )
        append_sources = self._load_projection_sources_from_env(
            inline_env_name=self._PROJECTION_APPEND_SOURCES_ENV_NAME,
            path_env_name=self._PROJECTION_APPEND_SOURCES_PATH_ENV_NAME,
        )

        if effective_sources is None:
            effective_sources = list(override_sources) if isinstance(override_sources, list) else default_sources
            if isinstance(append_sources, list) and append_sources:
                effective_sources.extend(append_sources)

        # Keep deterministic order while allowing later entries to override section/key duplicates.
        deduped_source_map: dict[tuple[str, str], dict[str, Any]] = {}
        deduped_order: list[tuple[str, str]] = []
        for source_object in effective_sources:
            normalized_source = self._normalize_projection_source_object(source_object)
            if normalized_source is None:
                continue
            section_key = (
                str(normalized_source.get("section") or "").strip().upper(),
                str(normalized_source.get("key") or "").strip(),
            )
            if not section_key[0] or not section_key[1]:
                continue
            if section_key not in deduped_source_map:
                deduped_order.append(section_key)
            deduped_source_map[section_key] = normalized_source

        return tuple(deduped_source_map[key] for key in deduped_order if key in deduped_source_map)

    def _json_safe_projection_data(self, data: Any) -> Any:
        try:
            return json.loads(json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            return {
                "_meta": {
                    "serialized_as": "string",
                },
                "value": str(data),
            }

    def _agentsdb_projection_payload(
        self,
        *,
        runtime_config: Any,
        section_name: str,
        source_key: str,
        source_uri: str,
        data: Any,
    ) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).isoformat()
        namespace_id = str(getattr(runtime_config, "namespace_id", "") or "ns_alde_default").strip() or "ns_alde_default"
        safe_data = self._json_safe_projection_data(data)
        serialized_data = json.dumps(safe_data, ensure_ascii=False, sort_keys=True)
        content_sha256 = hashlib.sha256(serialized_data.encode("utf-8")).hexdigest()
        return {
            "namespace_id": namespace_id,
            "title": f"AI IDE Projection: {source_key}",
            "summary": "Snapshot of existing AI IDE databases continued in agents_db.",
            "document_type": "ai_ide_projection",
            "source_uri": str(source_uri or f"alde://ai_ide/projection/{source_key}"),
            "section_name": str(section_name or "").upper(),
            "source_key": str(source_key or "").strip(),
            "data": safe_data,
            "content_sha256": content_sha256,
            "updated_at": timestamp,
            "created_at": timestamp,
        }

    def _load_projection_record_from_agentsdb(self, repository: Any | None, source_key: str) -> dict[str, Any] | None:
        if repository is None:
            return None
        try:
            record = repository.load_object("document", self._projection_source_object_id(source_key))
        except Exception:
            return None
        if not isinstance(record, dict):
            return None
        return record

    def _load_projection_payload_from_agentsdb(self, repository: Any | None, source_key: str) -> Any | None:
        record = self._load_projection_record_from_agentsdb(repository, source_key)
        if not isinstance(record, dict):
            return None
        return record.get("data")

    def _agentsdb_repository_object_name_map(self, repository: Any | None) -> dict[str, str]:
        object_collection_map = getattr(repository, "_OBJECT_COLLECTION_MAP", None)
        if not isinstance(object_collection_map, dict):
            return {}
        return {
            str(collection_name or "").strip(): str(object_name or "").strip().lower()
            for object_name, collection_name in object_collection_map.items()
            if str(collection_name or "").strip() and str(object_name or "").strip()
        }

    @staticmethod
    def _coerce_tree_path_segment(segment: Any, container: Any) -> Any:
        if isinstance(container, (list, tuple)):
            if isinstance(segment, int):
                return segment
            normalized_segment = str(segment or "").strip()
            if normalized_segment.startswith("[") and normalized_segment.endswith("]"):
                normalized_segment = normalized_segment[1:-1].strip()
            if normalized_segment.isdigit():
                return int(normalized_segment)
        return segment

    def _resolve_agentsdb_repository_collection_context(
        self,
        *,
        section_name: str | None,
        path_segments: Sequence[Any],
    ) -> dict[str, Any] | None:
        if str(section_name or "").strip().upper() != self._AGENTSDB_REPOSITORY_SECTION_NAME:
            return None

        normalized_path = [segment for segment in path_segments if str(segment or "").strip()]
        if len(normalized_path) < 3:
            return None
        if str(normalized_path[0] or "").strip() != self._AGENTSDB_REPOSITORY_SECTION_KEY:
            return None

        runtime_config, repository = self._load_agentsdb_repository()
        if runtime_config is None or repository is None or not self._should_project_agentsdb_repository(repository):
            return None

        database_name = self._agentsdb_repository_source(runtime_config)
        if str(normalized_path[1] or "").strip() != database_name:
            return None

        collection_name = str(normalized_path[2] or "").strip()
        object_name = self._agentsdb_repository_object_name_map(repository).get(collection_name)
        if not object_name:
            return None

        return {
            "runtime_config": runtime_config,
            "repository": repository,
            "database_name": database_name,
            "collection_name": collection_name,
            "object_name": object_name,
            "path_segments": list(normalized_path),
        }

    def resolve_agentsdb_repository_collection_binding(
        self,
        *,
        section_name: str | None,
        path_segments: Sequence[Any],
    ) -> dict[str, Any] | None:
        collection_context = self._resolve_agentsdb_repository_collection_context(
            section_name=section_name,
            path_segments=path_segments,
        )
        if collection_context is None:
            return None
        if len(list(collection_context.get("path_segments") or [])) != 3:
            return None
        return collection_context

    def resolve_agentsdb_repository_binding(
        self,
        *,
        section_name: str | None,
        path_segments: Sequence[Any],
    ) -> dict[str, Any] | None:
        collection_context = self._resolve_agentsdb_repository_collection_context(
            section_name=section_name,
            path_segments=path_segments,
        )
        if collection_context is None:
            return None
        normalized_path = list(collection_context.get("path_segments") or [])
        if len(normalized_path) < 4:
            return None

        record_id = str(normalized_path[3] or "").strip()
        if not record_id:
            return None

        return {
            "runtime_config": collection_context.get("runtime_config"),
            "repository": collection_context.get("repository"),
            "database_name": collection_context.get("database_name"),
            "collection_name": collection_context.get("collection_name"),
            "object_name": collection_context.get("object_name"),
            "record_id": record_id,
            "field_path": list(normalized_path[4:]),
        }

    def create_agentsdb_repository_record(
        self,
        *,
        section_name: str | None,
        path_segments: Sequence[Any],
        record_id: str,
        record_payload: Mapping[str, Any] | None = None,
    ) -> bool:
        binding = self.resolve_agentsdb_repository_collection_binding(
            section_name=section_name,
            path_segments=path_segments,
        )
        if binding is None:
            return False

        repository = binding.get("repository")
        object_name = str(binding.get("object_name") or "").strip()
        normalized_record_id = str(record_id or "").strip()
        payload = dict(record_payload or {})
        payload_record_id = str(payload.get("_id") or "").strip()
        if not normalized_record_id:
            normalized_record_id = payload_record_id
        if repository is None or not object_name or not normalized_record_id:
            return False

        try:
            existing_payload = repository.load_object(object_name, normalized_record_id)
        except Exception:
            existing_payload = None
        if isinstance(existing_payload, dict):
            return False

        payload["_id"] = normalized_record_id
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

        try:
            repository.upsert_object(object_name, normalized_record_id, payload)
        except Exception:
            return False
        return True

    def apply_agentsdb_repository_edit(
        self,
        *,
        section_name: str | None,
        path_segments: Sequence[Any],
        key_name: str,
        value: Any,
    ) -> bool:
        binding = self.resolve_agentsdb_repository_binding(section_name=section_name, path_segments=path_segments)
        if binding is None:
            return False

        repository = binding.get("repository")
        object_name = str(binding.get("object_name") or "").strip()
        record_id = str(binding.get("record_id") or "").strip()
        field_path = list(binding.get("field_path") or [])
        if repository is None or not object_name or not record_id or not field_path:
            return False

        try:
            record_payload = repository.load_object(object_name, record_id)
        except Exception:
            return False
        if not isinstance(record_payload, dict):
            return False

        parent_container: Any = record_payload
        for segment in field_path[:-1]:
            key_obj = self._coerce_tree_path_segment(segment, parent_container)
            try:
                parent_container = parent_container[key_obj]
            except (KeyError, IndexError, TypeError):
                return False

        last_segment = field_path[-1]
        key_obj = self._coerce_tree_path_segment(last_segment, parent_container)
        normalized_key_name = str(key_name or "").strip() or str(last_segment)

        if isinstance(parent_container, dict):
            if normalized_key_name != str(last_segment):
                try:
                    parent_container[normalized_key_name] = parent_container.pop(key_obj)
                except Exception:
                    parent_container[normalized_key_name] = value
                key_obj = normalized_key_name
            parent_container[key_obj] = value
        elif isinstance(parent_container, list):
            if not isinstance(key_obj, int) or not (0 <= key_obj < len(parent_container)):
                return False
            parent_container[key_obj] = value
        else:
            return False

        new_record_id = record_id
        if len(field_path) == 1 and str(field_path[0] or "").strip() == "_id":
            candidate_record_id = str(record_payload.get("_id") or "").strip()
            if candidate_record_id:
                new_record_id = candidate_record_id

        try:
            repository.upsert_object(object_name, new_record_id, record_payload)
            if new_record_id != record_id:
                delete_object = getattr(repository, "delete_object", None)
                if callable(delete_object):
                    delete_object(object_name, record_id)
        except Exception:
            return False
        return True

    def delete_agentsdb_repository_path(
        self,
        *,
        section_name: str | None,
        path_segments: Sequence[Any],
    ) -> bool:
        binding = self.resolve_agentsdb_repository_binding(section_name=section_name, path_segments=path_segments)
        if binding is None:
            return False

        repository = binding.get("repository")
        object_name = str(binding.get("object_name") or "").strip()
        record_id = str(binding.get("record_id") or "").strip()
        field_path = list(binding.get("field_path") or [])
        if repository is None or not object_name or not record_id:
            return False

        if not field_path:
            delete_object = getattr(repository, "delete_object", None)
            if not callable(delete_object):
                return False
            try:
                return bool(delete_object(object_name, record_id))
            except Exception:
                return False

        try:
            record_payload = repository.load_object(object_name, record_id)
        except Exception:
            return False
        if not isinstance(record_payload, dict):
            return False

        parent_container: Any = record_payload
        for segment in field_path[:-1]:
            key_obj = self._coerce_tree_path_segment(segment, parent_container)
            try:
                parent_container = parent_container[key_obj]
            except (KeyError, IndexError, TypeError):
                return False

        last_segment = field_path[-1]
        key_obj = self._coerce_tree_path_segment(last_segment, parent_container)

        removed = False
        if isinstance(parent_container, dict):
            if str(last_segment or "").strip() == "_id" and len(field_path) == 1:
                delete_object = getattr(repository, "delete_object", None)
                if not callable(delete_object):
                    return False
                try:
                    return bool(delete_object(object_name, record_id))
                except Exception:
                    return False
            removed = key_obj in parent_container
            if removed:
                parent_container.pop(key_obj, None)
        elif isinstance(parent_container, list):
            if isinstance(key_obj, int) and 0 <= key_obj < len(parent_container):
                parent_container.pop(key_obj)
                removed = True
        if not removed:
            return False

        try:
            repository.upsert_object(object_name, record_id, record_payload)
        except Exception:
            return False
        return True

    def _upsert_projection_payload_to_agentsdb(
        self,
        *,
        repository: Any | None,
        runtime_config: Any | None,
        section_name: str,
        source_key: str,
        source_uri: str,
        data: Any,
    ) -> None:
        if self.memory_only_enabled():
            return
        if repository is None or runtime_config is None:
            return

        projection_id = self._projection_source_object_id(source_key)
        projection_payload = self._agentsdb_projection_payload(
            runtime_config=runtime_config,
            section_name=section_name,
            source_key=source_key,
            source_uri=source_uri,
            data=data,
        )
        try:
            existing_payload = repository.load_object("document", projection_id)
        except Exception:
            existing_payload = None
        if isinstance(existing_payload, dict) and str(existing_payload.get("content_sha256") or "") == str(projection_payload.get("content_sha256") or ""):
            return
        try:
            repository.upsert_object("document", projection_id, projection_payload)
        except Exception:
            pass

    def _load_json_projection_object(self, file_path: Path, *, max_bytes: int = 2_000_000) -> Any | None:
        if not file_path.is_file():
            return None
        try:
            file_size = int(file_path.stat().st_size)
        except Exception:
            return None

        if file_size > max_bytes:
            return {
                "_meta": {
                    "path": str(file_path),
                    "truncated": True,
                    "size_bytes": file_size,
                }
            }

        try:
            with open(file_path, "r", encoding="utf-8") as file_handle:
                return json.load(file_handle)
        except Exception:
            return None

    def _load_directory_file_index(self, directory_path: Path, *, pattern: str = "*", max_entries: int = 250) -> dict[str, Any] | None:
        if not directory_path.is_dir():
            return None

        entry_list: list[dict[str, Any]] = []
        try:
            for file_path in sorted(directory_path.glob(pattern)):
                if not file_path.is_file():
                    continue
                file_size = int(file_path.stat().st_size)
                entry_list.append(
                    {
                        "name": file_path.name,
                        "size_bytes": file_size,
                    }
                )
                if len(entry_list) >= max(1, int(max_entries)):
                    break
        except Exception:
            return None

        return {
            "root_path": str(directory_path),
            "entry_count": len(entry_list),
            "entries": entry_list,
        }

    def _expand_projection_path_candidates(self, configured_path: str, app_data_dir_list: list[Path]) -> list[Path]:
        normalized_path = str(configured_path or "").strip()
        if not normalized_path:
            return []
        raw_path = Path(normalized_path).expanduser()
        candidate_path_list: list[Path] = []
        if raw_path.is_absolute():
            candidate_path_list.append(raw_path)
        else:
            for app_data_dir in app_data_dir_list:
                candidate_path_list.append(app_data_dir / raw_path)
                candidate_path_list.append(app_data_dir.parent / raw_path)
                candidate_path_list.append(app_data_dir.parent.parent / raw_path)
            candidate_path_list.append(raw_path)

        unique_candidate_list: list[Path] = []
        seen_set: set[str] = set()
        for candidate_path in candidate_path_list:
            try:
                normalized_candidate = str(candidate_path.resolve())
            except Exception:
                normalized_candidate = str(candidate_path)
            if normalized_candidate in seen_set:
                continue
            seen_set.add(normalized_candidate)
            unique_candidate_list.append(candidate_path)
        return unique_candidate_list

    def _agentsdb_query_projection_payload(
        self,
        source_object: Mapping[str, Any],
        *,
        runtime_config: Any | None,
        repository: Any | None,
    ) -> tuple[Any | None, str, str | None]:
        if repository is None:
            return None, "", None

        query_payload = source_object.get("query") if isinstance(source_object.get("query"), Mapping) else {}
        object_name = str(
            source_object.get("object_name")
            or source_object.get("collection_name")
            or query_payload.get("object_name")
            or query_payload.get("collection_name")
            or ""
        ).strip().lower()
        if not object_name:
            return None, "", None

        raw_limit = source_object.get("limit", query_payload.get("limit", 50))
        try:
            resolved_limit = max(1, min(int(raw_limit), 10000))
        except Exception:
            resolved_limit = 50

        filter_payload = source_object.get("filter", query_payload.get("filter"))
        if not isinstance(filter_payload, Mapping):
            filter_payload = {}
        normalized_filter = dict(filter_payload)

        try:
            load_objects = getattr(repository, "load_objects", None)
            if not callable(load_objects):
                return None, "", None
            raw_record_list = load_objects(object_name, object_filter=normalized_filter, limit=resolved_limit)
        except Exception:
            return None, "", None
        if not isinstance(raw_record_list, list):
            raw_record_list = []

        strict_mode = self._projection_db_only_strict_mode()
        field_allowlist_map = self._strict_projection_field_allowlist()
        allowed_field_set = field_allowlist_map.get(object_name, set())

        fields_payload = source_object.get("fields", query_payload.get("fields"))
        selected_field_list = [
            str(field_name).strip()
            for field_name in (fields_payload if isinstance(fields_payload, Sequence) and not isinstance(fields_payload, (str, bytes)) else [])
            if str(field_name).strip()
        ]

        if strict_mode:
            if selected_field_list:
                selected_field_list = [field_name for field_name in selected_field_list if field_name in allowed_field_set]
            else:
                selected_field_list = sorted(allowed_field_set)
            if not selected_field_list:
                selected_field_list = ["_id"]

        projected_record_list: list[dict[str, Any]] = []
        latest_updated_at = ""
        for record in raw_record_list:
            if not isinstance(record, Mapping):
                continue
            record_payload = dict(record)
            if selected_field_list:
                selected_payload = {
                    field_name: self._json_safe_projection_data(record_payload.get(field_name))
                    for field_name in selected_field_list
                }
                if "_id" not in selected_payload and "_id" in record_payload:
                    selected_payload["_id"] = self._json_safe_projection_data(record_payload.get("_id"))
                record_payload = selected_payload
            else:
                record_payload = self._json_safe_projection_data(record_payload)
            projected_record_list.append(record_payload)

            updated_at = str(record.get("updated_at") or record.get("created_at") or "").strip()
            if updated_at and updated_at > latest_updated_at:
                latest_updated_at = updated_at

        database_name = self._agentsdb_repository_source(runtime_config)
        source_uri = str(source_object.get("source_uri") or "").strip() or f"alde://agentsdb/query/{database_name}/{object_name}"

        return {
            "_meta": {
                "source_of_truth": "agentsdb_query",
                "database_name": database_name,
                "object_name": object_name,
                "limit": resolved_limit,
                "filter": normalized_filter,
                "record_count": len(projected_record_list),
                "fields": selected_field_list,
                "latest_updated_at": latest_updated_at,
            },
            "records": projected_record_list,
        }, source_uri, latest_updated_at or None

    def _env_projection_path_candidates(self, app_data_dir_list: list[Path]) -> list[Path]:
        source_file = Path(__file__).resolve()
        candidate_path_list: list[Path] = [
            source_file.parents[1] / ".env.json",
            source_file.parents[2] / ".env.json",
            source_file.with_suffix(".env.json"),
            source_file.parents[1] / ".env",
            source_file.parents[2] / ".env",
            source_file.with_suffix(".env"),
        ]

        for app_data_dir in app_data_dir_list:
            candidate_path_list.append(app_data_dir.parent / ".env.json")
            candidate_path_list.append(app_data_dir.parent.parent / ".env.json")
            candidate_path_list.append(app_data_dir.parent / ".env")
            candidate_path_list.append(app_data_dir.parent.parent / ".env")

        unique_candidate_path_list: list[Path] = []
        seen_path_set: set[str] = set()
        for candidate_path in candidate_path_list:
            expanded_candidate_path = candidate_path.expanduser()
            try:
                normalized_candidate_path = str(expanded_candidate_path.resolve())
            except Exception:
                normalized_candidate_path = str(expanded_candidate_path)
            if normalized_candidate_path in seen_path_set:
                continue
            seen_path_set.add(normalized_candidate_path)
            unique_candidate_path_list.append(expanded_candidate_path)

        return unique_candidate_path_list

    def _load_env_projection_object(self, env_path: Path) -> dict[str, Any] | None:
        if not env_path.is_file():
            return None

        try:
            env_file_text = env_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        if str(env_path.suffix or "").strip().lower() == ".json":
            try:
                json_payload = json.loads(env_file_text)
            except Exception:
                return None

            section_payload_map: dict[str, dict[str, dict[str, Any]]] = {}

            sections_payload = json_payload.get("sections") if isinstance(json_payload, Mapping) else None
            if isinstance(sections_payload, Mapping):
                for raw_section_name, raw_section_payload in sections_payload.items():
                    section_name = str(raw_section_name or "").strip() or "General"
                    section_payload_map.setdefault(section_name, {})
                    if not isinstance(raw_section_payload, Mapping):
                        continue
                    for raw_variable_name, raw_variable_payload in raw_section_payload.items():
                        variable_name = str(raw_variable_name or "").strip()
                        if not self._ENV_OBJECT_NAME_PATTERN.match(variable_name):
                            continue
                        if isinstance(raw_variable_payload, Mapping):
                            section_payload_map[section_name][variable_name] = {
                                "value": raw_variable_payload.get("value", ""),
                                "enabled": bool(raw_variable_payload.get("enabled", True)),
                            }
                        else:
                            section_payload_map[section_name][variable_name] = {
                                "value": raw_variable_payload,
                                "enabled": True,
                            }
            else:
                env_payload = json_payload.get("env") if isinstance(json_payload, Mapping) and isinstance(json_payload.get("env"), Mapping) else json_payload
                if not isinstance(env_payload, Mapping):
                    return None
                section_payload_map["General"] = {}
                for raw_variable_name, raw_value in env_payload.items():
                    variable_name = str(raw_variable_name or "").strip()
                    if not self._ENV_OBJECT_NAME_PATTERN.match(variable_name):
                        continue
                    section_payload_map["General"][variable_name] = {
                        "value": raw_value,
                        "enabled": True,
                    }

            variable_count = 0
            enabled_count = 0
            for section_payload in section_payload_map.values():
                for field_payload in section_payload.values():
                    variable_count += 1
                    if bool(field_payload.get("enabled")):
                        enabled_count += 1

            return {
                "_meta": {
                    "source_path": str(env_path),
                    "section_count": len(section_payload_map),
                    "variable_count": variable_count,
                    "enabled_count": enabled_count,
                    "disabled_count": max(0, variable_count - enabled_count),
                },
                "sections": section_payload_map,
            }

        line_list = env_file_text.splitlines()

        section_payload_map: dict[str, dict[str, dict[str, Any]]] = {}

        def parse_env_value(raw_value: Any) -> Any:
            value_text = str(raw_value or "")
            stripped_value = value_text.strip()
            if not stripped_value:
                return ""

            candidate_payload_list = [stripped_value]
            if len(stripped_value) >= 2 and stripped_value[0] == stripped_value[-1] and stripped_value[0] in {"'", '"'}:
                candidate_payload_list.append(stripped_value[1:-1].strip())

            for candidate_payload in candidate_payload_list:
                if not candidate_payload:
                    continue
                if not (
                    (candidate_payload.startswith("{") and candidate_payload.endswith("}"))
                    or (candidate_payload.startswith("[") and candidate_payload.endswith("]"))
                ):
                    continue
                try:
                    parsed_payload = json.loads(candidate_payload)
                except Exception:
                    continue
                if isinstance(parsed_payload, (dict, list)):
                    return parsed_payload
            return value_text

        def ensure_section_payload(section_name: str) -> str:
            normalized_section_name = str(section_name or "").strip() or "General"
            if normalized_section_name not in section_payload_map:
                section_payload_map[normalized_section_name] = {}
            return normalized_section_name

        current_section_name = ensure_section_payload("General")

        for raw_line in line_list:
            stripped_line = str(raw_line or "").strip()
            if not stripped_line:
                continue

            if stripped_line.startswith("#"):
                comment_payload = stripped_line[1:].strip()
                if not comment_payload:
                    continue

                disabled_assignment_match = self._ENV_ASSIGNMENT_PATTERN.match(comment_payload)
                if disabled_assignment_match is not None:
                    variable_name = str(disabled_assignment_match.group(1) or "").strip()
                    if not variable_name:
                        continue
                    section_payload_map[current_section_name][variable_name] = {
                        "value": parse_env_value(disabled_assignment_match.group(2)),
                        "enabled": False,
                    }
                    continue

                # Treat comment lines without key/value assignment as section headers.
                if "=" not in comment_payload:
                    current_section_name = ensure_section_payload(comment_payload)
                continue

            assignment_match = self._ENV_ASSIGNMENT_PATTERN.match(stripped_line)
            if assignment_match is None:
                continue

            variable_name = str(assignment_match.group(1) or "").strip()
            if not variable_name:
                continue

            section_payload_map[current_section_name][variable_name] = {
                "value": parse_env_value(assignment_match.group(2)),
                "enabled": True,
            }

        variable_count = 0
        enabled_count = 0
        for section_payload in section_payload_map.values():
            for field_payload in section_payload.values():
                variable_count += 1
                if bool(field_payload.get("enabled")):
                    enabled_count += 1

        return {
            "_meta": {
                "source_path": str(env_path),
                "section_count": len(section_payload_map),
                "variable_count": variable_count,
                "enabled_count": enabled_count,
                "disabled_count": max(0, variable_count - enabled_count),
            },
            "sections": section_payload_map,
        }

    @staticmethod
    def _env_projection_payload_from_tree_data(data: dict[str, Any]) -> dict[str, Any] | None:
        env_section_payload = data.get("ENV") if isinstance(data, dict) else None
        if not isinstance(env_section_payload, dict):
            return None

        preferred_payload = env_section_payload.get(".env.json")
        if not isinstance(preferred_payload, dict) or not isinstance(preferred_payload.get("sections"), dict):
            preferred_payload = env_section_payload.get(".env")
        if isinstance(preferred_payload, dict) and isinstance(preferred_payload.get("sections"), dict):
            return preferred_payload

        for payload in env_section_payload.values():
            if isinstance(payload, dict) and isinstance(payload.get("sections"), dict):
                return payload
        return None

    def _resolve_env_projection_write_path(self, env_projection_payload: dict[str, Any]) -> Path | None:
        meta_payload = env_projection_payload.get("_meta") if isinstance(env_projection_payload, dict) else None
        configured_source_path = str((meta_payload or {}).get("source_path") or "").strip()
        if configured_source_path:
            return Path(configured_source_path).expanduser()

        app_data_dir_list = self._projection_app_data_dir_list()
        candidate_path_list = self._env_projection_path_candidates(app_data_dir_list)
        for candidate_path in candidate_path_list:
            if candidate_path.exists() and candidate_path.is_file():
                return candidate_path
        if candidate_path_list:
            return candidate_path_list[0]
        return None

    @staticmethod
    def _serialize_env_projection_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list, tuple)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        return str(value)

    def _serialize_env_projection_sections(
        self,
        sections_payload: dict[str, Any],
    ) -> tuple[str, int, int, int]:
        section_block_list: list[list[str]] = []
        section_count = 0
        variable_count = 0
        enabled_count = 0

        for raw_section_name, section_payload in sections_payload.items():
            section_name = str(raw_section_name or "").strip() or "General"
            section_variable_payload = section_payload if isinstance(section_payload, dict) else {}

            section_line_list: list[str] = []
            include_header = section_name.lower() != "general"
            if include_header:
                section_line_list.append(f"# {section_name}")

            section_variable_count = 0
            for raw_variable_name, raw_variable_payload in section_variable_payload.items():
                variable_name = str(raw_variable_name or "").strip()
                if not self._ENV_OBJECT_NAME_PATTERN.match(variable_name):
                    continue

                if isinstance(raw_variable_payload, dict):
                    value_payload = raw_variable_payload.get("value", "")
                    enabled_payload = bool(raw_variable_payload.get("enabled", True))
                else:
                    value_payload = raw_variable_payload
                    enabled_payload = True

                value_text = self._serialize_env_projection_value(value_payload)
                line_prefix = "" if enabled_payload else "# "
                section_line_list.append(f"{line_prefix}{variable_name}={value_text}")
                section_variable_count += 1
                variable_count += 1
                if enabled_payload:
                    enabled_count += 1

            if section_variable_count <= 0:
                continue
            section_count += 1
            section_block_list.append(section_line_list)

        serialized_line_list: list[str] = []
        for block_index, section_block in enumerate(section_block_list):
            if block_index > 0:
                serialized_line_list.append("")
            serialized_line_list.extend(section_block)

        serialized_payload = "\n".join(serialized_line_list).rstrip()
        if serialized_payload:
            serialized_payload += "\n"
        return serialized_payload, section_count, variable_count, enabled_count

    def persist_env_projection_from_tree_data(self, data: dict[str, Any]) -> Path | None:
        env_projection_payload = self._env_projection_payload_from_tree_data(data)
        if not isinstance(env_projection_payload, dict):
            return None

        sections_payload = env_projection_payload.get("sections")
        if not isinstance(sections_payload, dict):
            return None

        env_path = self._resolve_env_projection_write_path(env_projection_payload)
        if env_path is None:
            return None

        if str(env_path.suffix or "").strip().lower() == ".json":
            normalized_sections: dict[str, dict[str, dict[str, Any]]] = {}
            env_payload: dict[str, Any] = {}
            section_count = 0
            variable_count = 0
            enabled_count = 0

            for raw_section_name, section_payload in sections_payload.items():
                section_name = str(raw_section_name or "").strip() or "General"
                if not isinstance(section_payload, Mapping):
                    continue
                normalized_sections.setdefault(section_name, {})
                section_variable_count = 0

                for raw_variable_name, raw_variable_payload in section_payload.items():
                    variable_name = str(raw_variable_name or "").strip()
                    if not self._ENV_OBJECT_NAME_PATTERN.match(variable_name):
                        continue
                    if isinstance(raw_variable_payload, Mapping):
                        value_payload = raw_variable_payload.get("value", "")
                        enabled_payload = bool(raw_variable_payload.get("enabled", True))
                    else:
                        value_payload = raw_variable_payload
                        enabled_payload = True

                    normalized_sections[section_name][variable_name] = {
                        "value": value_payload,
                        "enabled": enabled_payload,
                    }
                    section_variable_count += 1
                    variable_count += 1
                    if enabled_payload:
                        enabled_count += 1
                        env_payload[variable_name] = value_payload

                if section_variable_count > 0:
                    section_count += 1

            serialized_payload = json.dumps(
                {
                    "format": "alde_env_json_v1",
                    "env": env_payload,
                    "sections": normalized_sections,
                },
                indent=2,
                ensure_ascii=False,
            ).rstrip() + "\n"
        else:
            serialized_payload, section_count, variable_count, enabled_count = self._serialize_env_projection_sections(sections_payload)

        existing_payload: str | None = None
        if env_path.exists() and env_path.is_file():
            try:
                existing_payload = env_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                existing_payload = None

        if existing_payload != serialized_payload:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(serialized_payload, encoding="utf-8")

        meta_payload = env_projection_payload.get("_meta")
        if not isinstance(meta_payload, dict):
            meta_payload = {}
            env_projection_payload["_meta"] = meta_payload
        meta_payload["source_path"] = str(env_path)
        meta_payload["section_count"] = section_count
        meta_payload["variable_count"] = variable_count
        meta_payload["enabled_count"] = enabled_count
        meta_payload["disabled_count"] = max(0, variable_count - enabled_count)

        return env_path

    def _load_projection_payload_from_local_source(
        self,
        source_object: dict[str, Any],
        app_data_dir_list: list[Path],
        *,
        runtime_config: Any | None = None,
        repository: Any | None = None,
    ) -> tuple[Any | None, str, str | None]:
        source_kind = str(source_object.get("kind") or "").strip().lower()
        if self._projection_db_only_strict_mode() and source_kind != "agentsdb_query":
            return None, "", None
        if source_kind == "chat_history":
            if ChatHistory is None:
                return None, "", None
            try:
                history_payload = ChatHistory._load()
                serialized_history = json.dumps(history_payload, ensure_ascii=False)
                history_path = self._chat_history_source_path()
                history_uri = str(history_path) if history_path is not None else "alde://chat/history"
                history_mtime = self._mtime_iso(history_path)
                if len(serialized_history) > 2_000_000:
                    return {
                        "_meta": {
                            "truncated": True,
                            "char_count": len(serialized_history),
                        }
                    }, history_uri, history_mtime
                return history_payload, history_uri, history_mtime
            except Exception:
                return None, "", None

        if source_kind == "env_file":
            configured_file_name = str(source_object.get("file_name") or ".env").strip() or ".env"
            configured_file_path = str(source_object.get("file_path") or "").strip()
            configured_path = Path(configured_file_path or configured_file_name).expanduser()

            candidate_path_list: list[Path] = []
            if configured_path.is_absolute():
                candidate_path_list.append(configured_path)

            for env_candidate_path in self._env_projection_path_candidates(app_data_dir_list):
                if configured_path.is_absolute() or env_candidate_path.name == configured_path.name:
                    candidate_path_list.append(env_candidate_path)

            seen_path_set: set[str] = set()
            for candidate_path in candidate_path_list:
                expanded_candidate_path = candidate_path.expanduser()
                try:
                    normalized_candidate_path = str(expanded_candidate_path.resolve())
                except Exception:
                    normalized_candidate_path = str(expanded_candidate_path)
                if normalized_candidate_path in seen_path_set:
                    continue
                seen_path_set.add(normalized_candidate_path)

                payload = self._load_env_projection_object(expanded_candidate_path)
                if payload is not None:
                    return payload, str(expanded_candidate_path), self._mtime_iso(expanded_candidate_path)

            return None, "", None

        if source_kind == "json_file":
            configured_file_path = str(source_object.get("file_path") or "").strip()
            file_name = str(source_object.get("file_name") or "").strip()
            path_hint = configured_file_path or file_name
            if not path_hint:
                return None, "", None
            for file_path in self._expand_projection_path_candidates(path_hint, app_data_dir_list):
                payload = self._load_json_projection_object(file_path)
                if payload is not None:
                    return payload, str(file_path), self._mtime_iso(file_path)
            return None, "", None

        if source_kind == "directory_index":
            configured_dir_path = str(source_object.get("dir_path") or "").strip()
            dir_name = str(source_object.get("dir_name") or "").strip()
            path_hint = configured_dir_path or dir_name
            if not path_hint:
                return None, "", None
            pattern = str(source_object.get("pattern") or "*").strip() or "*"
            max_entries_value = source_object.get("max_entries")
            try:
                max_entries = max(1, min(int(max_entries_value), 5000)) if max_entries_value is not None else 250
            except Exception:
                max_entries = 250
            for directory_path in self._expand_projection_path_candidates(path_hint, app_data_dir_list):
                payload = self._load_directory_file_index(directory_path, pattern=pattern, max_entries=max_entries)
                if payload is not None:
                    return payload, str(directory_path), self._mtime_iso(directory_path)
            return None, "", None

        if source_kind == "agentsdb_query":
            return self._agentsdb_query_projection_payload(
                source_object,
                runtime_config=runtime_config,
                repository=repository,
            )

        return None, "", None

    def _resolve_projection_payload(
        self,
        *,
        source_object: dict[str, Any],
        source_key: str,
        section_name: str,
        app_data_dir_list: list[Path],
        repository: Any | None,
        runtime_config: Any | None,
    ) -> tuple[Any | None, str, bool]:
        conflict_policy = self._projection_conflict_policy()
        source_kind = str(source_object.get("kind") or "").strip().lower()
        agentsdb_record = self._load_projection_record_from_agentsdb(repository, source_key)
        agentsdb_payload = agentsdb_record.get("data") if isinstance(agentsdb_record, dict) else None
        agentsdb_uri = str((agentsdb_record or {}).get("source_uri") or f"alde://ai_ide/projection/{source_key}")
        agentsdb_updated_at = self._timestamp_from_iso((agentsdb_record or {}).get("updated_at") or (agentsdb_record or {}).get("created_at"))

        local_loaded = False
        local_payload: Any | None = None
        local_uri = ""
        local_updated_at: datetime | None = None

        def load_local_once() -> tuple[Any | None, str, datetime | None]:
            nonlocal local_loaded, local_payload, local_uri, local_updated_at
            if local_loaded:
                return local_payload, local_uri, local_updated_at
            payload, uri, updated_at = self._load_projection_payload_from_local_source(
                source_object,
                app_data_dir_list,
                runtime_config=runtime_config,
                repository=repository,
            )
            local_loaded = True
            local_payload = payload
            local_uri = str(uri or "")
            local_updated_at = self._timestamp_from_iso(updated_at)
            return local_payload, local_uri, local_updated_at

        if conflict_policy == "agentsdb_strict":
            if agentsdb_payload is not None:
                return agentsdb_payload, agentsdb_uri, True
            if source_kind == "agentsdb_query":
                local_payload, local_uri, _ = load_local_once()
                if local_payload is None:
                    return None, "", False
                self._upsert_projection_payload_to_agentsdb(
                    repository=repository,
                    runtime_config=runtime_config,
                    section_name=section_name,
                    source_key=source_key,
                    source_uri=local_uri,
                    data=local_payload,
                )
                return local_payload, local_uri or agentsdb_uri, True
            return None, "", False

        if conflict_policy == "agentsdb_first":
            if agentsdb_payload is not None:
                return agentsdb_payload, agentsdb_uri, True
            local_payload, local_uri, _ = load_local_once()
            if local_payload is None:
                return None, "", False
            self._upsert_projection_payload_to_agentsdb(
                repository=repository,
                runtime_config=runtime_config,
                section_name=section_name,
                source_key=source_key,
                source_uri=local_uri,
                data=local_payload,
            )
            return local_payload, local_uri, False

        if conflict_policy == "local_first":
            local_payload, local_uri, _ = load_local_once()
            if local_payload is not None:
                self._upsert_projection_payload_to_agentsdb(
                    repository=repository,
                    runtime_config=runtime_config,
                    section_name=section_name,
                    source_key=source_key,
                    source_uri=local_uri,
                    data=local_payload,
                )
                return local_payload, local_uri, False
            if agentsdb_payload is None:
                return None, "", False
            return agentsdb_payload, agentsdb_uri, True

        # newest_wins
        local_payload, local_uri, local_timestamp = load_local_once()
        if agentsdb_payload is None and local_payload is None:
            return None, "", False
        if agentsdb_payload is None:
            self._upsert_projection_payload_to_agentsdb(
                repository=repository,
                runtime_config=runtime_config,
                section_name=section_name,
                source_key=source_key,
                source_uri=local_uri,
                data=local_payload,
            )
            return local_payload, local_uri, False
        if local_payload is None:
            return agentsdb_payload, agentsdb_uri, True

        local_is_newer = False
        if local_timestamp is not None and agentsdb_updated_at is not None:
            local_is_newer = local_timestamp > agentsdb_updated_at
        elif local_timestamp is not None and agentsdb_updated_at is None:
            local_is_newer = True

        if local_is_newer:
            self._upsert_projection_payload_to_agentsdb(
                repository=repository,
                runtime_config=runtime_config,
                section_name=section_name,
                source_key=source_key,
                source_uri=local_uri,
                data=local_payload,
            )
            return local_payload, local_uri, False
        return agentsdb_payload, agentsdb_uri, True

    def _build_local_projection_sections(
        self,
        *,
        runtime_config: Any | None = None,
        repository: Any | None = None,
    ) -> dict[str, dict[str, Any]]:
        projection_sections: dict[str, dict[str, Any]] = {
            section_name: {}
            for section_name in self._resolved_ai_ide_section_name_order()
        }

        app_data_dir_list = self._projection_app_data_dir_list()
        for source_object in self._projection_source_object_definition():
            section_name = str(source_object.get("section") or "").strip().upper()
            source_key = str(source_object.get("key") or "").strip()
            if not section_name or not source_key:
                continue
            if section_name not in projection_sections:
                projection_sections[section_name] = {}

            payload, _, _ = self._resolve_projection_payload(
                source_object=source_object,
                source_key=source_key,
                section_name=section_name,
                app_data_dir_list=app_data_dir_list,
                repository=repository,
                runtime_config=runtime_config,
            )

            if payload is None:
                continue

            projection_sections[section_name][source_key] = payload

        return projection_sections

    def _merge_local_projection_sections(
        self,
        data: dict[str, Any],
        *,
        runtime_config: Any | None = None,
        repository: Any | None = None,
    ) -> dict[str, Any]:
        merged_data = self._normalize_tree_data_structure(data)
        local_projection_sections = self._build_local_projection_sections(
            runtime_config=runtime_config,
            repository=repository,
        )
        for section_name, section_payload in local_projection_sections.items():
            if not section_payload:
                continue
            target_section_payload = merged_data.setdefault(section_name, {})
            if not isinstance(target_section_payload, dict):
                target_section_payload = {}
                merged_data[section_name] = target_section_payload
            for projection_key, projection_value in section_payload.items():
                target_section_payload[projection_key] = projection_value
        merged_data[self._MCP_SECTION_NAME] = self._load_mcp_projection_section_from_env(merged_data)
        return self._apply_tree_storage_projection_policy(merged_data)

    def _load_mcp_projection_section_from_env(self, data: dict[str, Any]) -> dict[str, Any]:
        env_projection_payload = self._env_projection_payload_from_tree_data(data)
        if not isinstance(env_projection_payload, dict):
            return {}

        section_payload_map = env_projection_payload.get("sections")
        if not isinstance(section_payload_map, dict):
            return {}

        mcp_payload = section_payload_map.get("mcp")
        if not isinstance(mcp_payload, dict):
            for section_name, section_payload in section_payload_map.items():
                if str(section_name or "").strip().lower() == "mcp" and isinstance(section_payload, dict):
                    mcp_payload = section_payload
                    break

        if not isinstance(mcp_payload, dict):
            return {}
        return {"mcp": dict(mcp_payload)}

    def _apply_tree_storage_projection_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._filter_tree_sections(data)
        if "PROJECTS" not in normalized_data:
            return normalized_data
        projects_payload = normalized_data.get("PROJECTS")
        if not isinstance(projects_payload, dict):
            projects_payload = {}
            normalized_data["PROJECTS"] = projects_payload

        storage_payload = projects_payload.get("tree_widget_storage")
        if not isinstance(storage_payload, dict):
            storage_payload = {}
            projects_payload["tree_widget_storage"] = storage_payload

        stored_policy = self._normalize_projection_conflict_policy_value(storage_payload.get("projection_conflict_policy"))
        if stored_policy is None:
            storage_payload["projection_conflict_policy"] = self._projection_conflict_policy()
        else:
            storage_payload["projection_conflict_policy"] = stored_policy
        return normalized_data

    def _agentsdb_repository_projection_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_REPOSITORY_VIEW", "")).strip().lower()
        if not value:
            value = str(self._load_storage_config().get("agentsdb_repository_view", "1")).strip().lower()
        if not value:
            value = "1"
        return value in {"1", "true", "yes", "on"}

    def _agentsdb_repository_projection_limit(self) -> int:
        raw_value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_REPOSITORY_LIMIT", "")).strip()
        if not raw_value:
            raw_value = str(self._load_storage_config().get("agentsdb_repository_limit", "2000")).strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = 2000
        return max(1, min(resolved_value, 10000))

    def _should_project_agentsdb_repository(self, repository: Any | None) -> bool:
        object_collection_map = getattr(repository, "_OBJECT_COLLECTION_MAP", None)
        return self._agentsdb_repository_projection_enabled() and isinstance(object_collection_map, dict) and bool(object_collection_map)

    def _agentsdb_repository_source(self, runtime_config: Any | None) -> str:
        return str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge"

    def _compact_agentsdb_repository_value(self, value: Any, *, depth: int = 0) -> Any:
        if depth >= 2:
            if isinstance(value, dict):
                return {
                    "_meta": {
                        "kind": "dict",
                        "key_count": len(value),
                    }
                }
            if isinstance(value, (list, tuple)):
                return {
                    "_meta": {
                        "kind": "list",
                        "length": len(value),
                    }
                }

        if isinstance(value, dict):
            item_list = list(value.items())
            projected_value: dict[str, Any] = {}
            for index, (child_key, child_value) in enumerate(item_list):
                if index >= 24:
                    projected_value["_meta"] = {
                        "kind": "dict",
                        "truncated": True,
                        "visible_key_count": 24,
                        "total_key_count": len(item_list),
                    }
                    break
                projected_value[str(child_key)] = self._compact_agentsdb_repository_value(child_value, depth=depth + 1)
            return projected_value

        if isinstance(value, (list, tuple)):
            sequence = list(value)
            if sequence and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in sequence) and len(sequence) > 16:
                return {
                    "_meta": {
                        "kind": "numeric_list",
                        "length": len(sequence),
                        "preview": [float(sequence[index]) for index in range(min(4, len(sequence)))],
                    }
                }
            projected_list = [
                self._compact_agentsdb_repository_value(item, depth=depth + 1)
                for item in sequence[:12]
            ]
            if len(sequence) > len(projected_list):
                return {
                    "_meta": {
                        "kind": "list",
                        "length": len(sequence),
                        "sample_size": len(projected_list),
                    },
                    "items": projected_list,
                }
            return projected_list

        if isinstance(value, str) and len(value) > 320:
            return {
                "_meta": {
                    "kind": "string",
                    "length": len(value),
                    "preview": value[:320],
                }
            }

        return self._json_safe_projection_data(value)

    def _agentsdb_repository_record_projection(self, record: dict[str, Any]) -> dict[str, Any]:
        projected_record: dict[str, Any] = {}
        for key_name, value in sorted(dict(record).items(), key=lambda item: str(item[0])):
            projected_record[str(key_name)] = self._compact_agentsdb_repository_value(value)
        return projected_record

    def _agentsdb_repository_collection_view(
        self,
        repository: Any | None,
        *,
        object_name: str,
        collection_name: str,
    ) -> dict[str, Any]:
        record_limit = self._agentsdb_repository_projection_limit()
        record_payload_list: list[dict[str, Any]] = []
        try:
            load_objects = getattr(repository, "load_objects", None)
            if callable(load_objects):
                raw_result = load_objects(object_name, limit=record_limit)
                if isinstance(raw_result, list):
                    record_payload_list = [dict(item) for item in raw_result if isinstance(item, dict)]
        except Exception:
            record_payload_list = []

        collection_payload: dict[str, Any] = {
            "_meta": {
                "object_name": object_name,
                "collection_name": collection_name,
                "record_count_visible": 0,
                "record_limit": record_limit,
                "truncated": False,
                "latest_updated_at": "",
            }
        }
        latest_updated_at = ""
        for record in sorted(record_payload_list, key=lambda item: str(item.get("_id") or item.get("id") or "")):
            record_id = str(record.get("_id") or record.get("id") or "").strip()
            if not record_id:
                record_id = f"{object_name}:{len(collection_payload)}"
            collection_payload[record_id] = self._agentsdb_repository_record_projection(record)
            updated_at = str(record.get("updated_at") or record.get("created_at") or "").strip()
            if updated_at and updated_at > latest_updated_at:
                latest_updated_at = updated_at

        collection_meta = collection_payload["_meta"]
        if isinstance(collection_meta, dict):
            collection_meta["record_count_visible"] = max(0, len(collection_payload) - 1)
            collection_meta["truncated"] = len(record_payload_list) >= record_limit
            collection_meta["latest_updated_at"] = latest_updated_at
        return collection_payload

    def _agentsdb_repository_view_payload(
        self,
        *,
        runtime_config: Any,
        repository: Any | None,
    ) -> dict[str, Any]:
        database_name = self._agentsdb_repository_source(runtime_config)
        agents_db_uri = self._normalize_tree_agentsdb_uri(getattr(runtime_config, "agents_db_uri", "") or "")
        object_collection_map = getattr(repository, "_OBJECT_COLLECTION_MAP", None)
        collection_payload_map: dict[str, Any] = {}
        latest_updated_at = ""
        visible_record_count = 0

        if isinstance(object_collection_map, dict):
            for object_name, collection_name in sorted(object_collection_map.items(), key=lambda item: str(item[1] or item[0])):
                normalized_object_name = str(object_name or "").strip().lower()
                normalized_collection_name = str(collection_name or "").strip()
                if not normalized_object_name or not normalized_collection_name:
                    continue
                collection_payload = self._agentsdb_repository_collection_view(
                    repository,
                    object_name=normalized_object_name,
                    collection_name=normalized_collection_name,
                )
                collection_payload_map[normalized_collection_name] = collection_payload
                collection_meta = collection_payload.get("_meta") if isinstance(collection_payload, dict) else None
                if isinstance(collection_meta, dict):
                    visible_record_count += int(collection_meta.get("record_count_visible") or 0)
                    collection_updated_at = str(collection_meta.get("latest_updated_at") or "").strip()
                    if collection_updated_at and collection_updated_at > latest_updated_at:
                        latest_updated_at = collection_updated_at

        return {
            "_meta": {
                "source_of_truth": "agentsdb_repository",
                "agents_db_uri": agents_db_uri,
                "database_name": database_name,
                "collection_count": len(collection_payload_map),
                "record_count_visible": visible_record_count,
                "record_limit_per_collection": self._agentsdb_repository_projection_limit(),
                "latest_updated_at": latest_updated_at,
            },
            database_name: collection_payload_map,
        }

    def _strip_derived_agentsdb_repository_sections(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._normalize_tree_data_structure(data)
        section_payload = normalized_data.get(self._AGENTSDB_REPOSITORY_SECTION_NAME)
        if not isinstance(section_payload, dict):
            normalized_data[self._MCP_SECTION_NAME] = {}
            return normalized_data
        stripped_section_payload = dict(section_payload)
        stripped_section_payload.pop(self._AGENTSDB_REPOSITORY_SECTION_KEY, None)
        normalized_data[self._AGENTSDB_REPOSITORY_SECTION_NAME] = stripped_section_payload
        normalized_data[self._MCP_SECTION_NAME] = {}
        return normalized_data

    def _overlay_agentsdb_repository_sections(
        self,
        data: dict[str, Any],
        *,
        runtime_config: Any | None = None,
        repository: Any | None = None,
    ) -> dict[str, Any]:
        normalized_data = self._normalize_tree_data_structure(data)
        if runtime_config is None or not self._should_project_agentsdb_repository(repository):
            return normalized_data

        section_payload = normalized_data.get(self._AGENTSDB_REPOSITORY_SECTION_NAME)
        if not isinstance(section_payload, dict):
            section_payload = {}
        section_payload = dict(section_payload)
        section_payload[self._AGENTSDB_REPOSITORY_SECTION_KEY] = self._agentsdb_repository_view_payload(
            runtime_config=runtime_config,
            repository=repository,
        )
        normalized_data[self._AGENTSDB_REPOSITORY_SECTION_NAME] = section_payload
        return normalized_data

    def _resolve_tree_view_data(
        self,
        data: dict[str, Any],
        *,
        runtime_config: Any | None = None,
        repository: Any | None = None,
    ) -> dict[str, Any]:
        normalized_data = self._strip_derived_agentsdb_repository_sections(self._normalize_tree_data_structure(data))
        normalized_data = self._merge_local_projection_sections(
            normalized_data,
            runtime_config=runtime_config,
            repository=repository,
        )
        normalized_data = self._overlay_agentsdb_repository_sections(
            normalized_data,
            runtime_config=runtime_config,
            repository=repository,
        )
        return self._apply_tree_storage_projection_policy(normalized_data)

    def _build_repository_projection_cursor(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._normalize_tree_data_structure(data)
        repository_section = normalized_data.get(self._AGENTSDB_REPOSITORY_SECTION_NAME)
        repository_payload = repository_section.get(self._AGENTSDB_REPOSITORY_SECTION_KEY) if isinstance(repository_section, dict) else None
        repository_meta = repository_payload.get("_meta") if isinstance(repository_payload, dict) else None
        updated_at = str((repository_meta or {}).get("latest_updated_at") or "").strip()
        tree_hash = self._tree_data_content_hash(normalized_data)
        return {
            "event_id": tree_hash[:32],
            "updated_at": updated_at,
            "tree_hash": tree_hash,
        }

    def _agentsdb_collection_projection(self, repository: Any | None) -> list[dict[str, Any]]:
        if repository is None:
            return []

        object_collection_map = getattr(repository, "_OBJECT_COLLECTION_MAP", None)
        if not isinstance(object_collection_map, dict):
            return []

        collection_projection: list[dict[str, Any]] = []
        for object_name, collection_name in object_collection_map.items():
            normalized_object_name = str(object_name or "").strip().lower()
            normalized_collection_name = str(collection_name or "").strip()
            if not normalized_object_name or not normalized_collection_name:
                continue

            projection_record: dict[str, Any] = {
                "name": normalized_collection_name,
                "object_name": normalized_object_name,
            }
            try:
                load_objects = getattr(repository, "load_objects", None)
                if callable(load_objects):
                    projection_record["has_records"] = bool(load_objects(normalized_object_name, limit=1))
            except Exception:
                pass
            collection_projection.append(projection_record)

        collection_projection.sort(key=lambda item: str(item.get("name") or ""))
        return collection_projection

    def _agentsdb_tree_projection(self, *, runtime_config: Any, repository: Any | None) -> dict[str, Any]:
        database_name = str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge"
        return {
            "schema": "agentsdb_tree_v1",
            "db": [
                {
                    "name": database_name,
                    "coll": self._agentsdb_collection_projection(repository),
                }
            ],
        }

    def _build_tree_stream_cursor(self, data: dict[str, Any], change_event: Any) -> dict[str, Any]:
        safe_change_event = self._json_safe_projection_data(
            change_event if isinstance(change_event, dict) else {"action": "snapshot", "origin": "tree_widget"}
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        tree_hash = self._tree_data_content_hash(data)
        serialized_seed = json.dumps(
            {
                "timestamp": timestamp,
                "tree_hash": tree_hash,
                "change": safe_change_event,
                "nonce": time.time_ns(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        event_id = hashlib.sha256(serialized_seed.encode("utf-8")).hexdigest()[:32]
        return {
            "event_id": event_id,
            "updated_at": timestamp,
            "tree_hash": tree_hash,
            "change": safe_change_event,
        }

    def _agentsdb_tree_stream_event_payload(
        self,
        *,
        runtime_config: Any,
        stream_cursor: dict[str, Any],
    ) -> dict[str, Any]:
        namespace_id = str(getattr(runtime_config, "namespace_id", "") or "ns_alde_default").strip() or "ns_alde_default"
        updated_at = str(stream_cursor.get("updated_at") or datetime.now(timezone.utc).isoformat())
        safe_change_event = self._json_safe_projection_data(stream_cursor.get("change") or {"action": "snapshot", "origin": "tree_widget"})
        return {
            "namespace_id": namespace_id,
            "title": "AI IDE Tree Stream Event",
            "summary": "Append-only change stream event for the AI IDE tree widget.",
            "document_type": "ai_ide_tree_stream_event",
            "source_uri": "alde://ai_ide/tree/stream",
            "tree_object_id": self._tree_object_id(),
            "event_id": str(stream_cursor.get("event_id") or "").strip(),
            "tree_hash": str(stream_cursor.get("tree_hash") or "").strip(),
            "change": safe_change_event,
            "updated_at": updated_at,
            "created_at": updated_at,
        }

    def _agentsdb_tree_stream_head_payload(
        self,
        *,
        runtime_config: Any,
        stream_cursor: dict[str, Any],
    ) -> dict[str, Any]:
        namespace_id = str(getattr(runtime_config, "namespace_id", "") or "ns_alde_default").strip() or "ns_alde_default"
        updated_at = str(stream_cursor.get("updated_at") or datetime.now(timezone.utc).isoformat())
        safe_change_event = self._json_safe_projection_data(stream_cursor.get("change") or {"action": "snapshot", "origin": "tree_widget"})
        return {
            "namespace_id": namespace_id,
            "title": "AI IDE Tree Stream Head",
            "summary": "Latest live cursor for the AI IDE tree widget change stream.",
            "document_type": "ai_ide_tree_stream_head",
            "source_uri": "alde://ai_ide/tree/stream/head",
            "tree_object_id": self._tree_object_id(),
            "event_id": str(stream_cursor.get("event_id") or "").strip(),
            "tree_hash": str(stream_cursor.get("tree_hash") or "").strip(),
            "change": safe_change_event,
            "updated_at": updated_at,
            "created_at": updated_at,
        }

    def _agentsdb_tree_payload(
        self,
        *,
        runtime_config: Any,
        repository: Any | None,
        data: dict[str, Any],
        stream_cursor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_data = self._strip_derived_agentsdb_repository_sections(
            self._normalize_tree_data_structure(data)
        )
        normalized_data = self._merge_local_projection_sections(
            base_data,
            runtime_config=runtime_config,
            repository=repository,
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        namespace_id = str(getattr(runtime_config, "namespace_id", "") or "ns_alde_default").strip() or "ns_alde_default"
        content_sha256 = self._tree_data_content_hash(normalized_data)
        payload = {
            "namespace_id": namespace_id,
            "title": "AI IDE Source Tree",
            "summary": "AgentsDB stores the complete AI IDE tree as source-of-truth for UI projections.",
            "document_type": "ai_ide_tree",
            "source_uri": "alde://ai_ide/tree",
            "tree_data": normalized_data,
            "content_sha256": content_sha256,
            "agentsdb_tree": self._agentsdb_tree_projection(runtime_config=runtime_config, repository=repository),
            "projection_contract": {
                "source_of_truth": "agents_db",
                "consumer": "ai_ide",
                "section_order": list(self._resolved_ai_ide_section_name_order()),
                "projection_conflict_policy": self._projection_conflict_policy(),
            },
            "updated_at": timestamp,
            "created_at": timestamp,
        }
        normalized_stream_cursor = self._normalize_tree_stream_cursor(stream_cursor)
        if normalized_stream_cursor is not None:
            payload["last_stream_event_id"] = str(normalized_stream_cursor.get("event_id") or "").strip() or None
            payload["stream_cursor"] = normalized_stream_cursor
        return payload

    def _upsert_tree_payload_to_agentsdb(
        self,
        *,
        repository: Any | None,
        runtime_config: Any | None,
        data: dict[str, Any],
        change_event: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if repository is None or runtime_config is None:
            return None
        normalized_data = self._merge_local_projection_sections(
            self._normalize_tree_data_structure(data),
            runtime_config=runtime_config,
            repository=repository,
        )
        stream_cursor = self._build_tree_stream_cursor(normalized_data, change_event)
        tree_payload = self._agentsdb_tree_payload(
            runtime_config=runtime_config,
            repository=repository,
            data=normalized_data,
            stream_cursor=stream_cursor,
        )
        stream_event_payload = self._agentsdb_tree_stream_event_payload(
            runtime_config=runtime_config,
            stream_cursor=stream_cursor,
        )
        stream_head_payload = self._agentsdb_tree_stream_head_payload(
            runtime_config=runtime_config,
            stream_cursor=stream_cursor,
        )

        flush_context = getattr(repository, "deferred_write_queue", None)
        if not callable(flush_context):
            flush_context = getattr(repository, "deferred_flush", None)

        if callable(flush_context):
            with flush_context():
                repository.upsert_object("document", self._tree_stream_event_object_id(str(stream_cursor.get("event_id") or "")), stream_event_payload)
                repository.upsert_object("document", self._tree_stream_head_object_id(), stream_head_payload)
                repository.upsert_object("document", self._tree_object_id(), tree_payload)
        else:
            repository.upsert_object("document", self._tree_stream_event_object_id(str(stream_cursor.get("event_id") or "")), stream_event_payload)
            repository.upsert_object("document", self._tree_stream_head_object_id(), stream_head_payload)
            repository.upsert_object("document", self._tree_object_id(), tree_payload)

        self._last_stream_cursor = self._normalize_tree_stream_cursor(stream_cursor)
        return self.load_last_stream_cursor()

    def _load_tree_seed_payload(
        self,
        *,
        runtime_config: Any | None,
        repository: Any | None,
    ) -> tuple[dict[str, Any], str, str]:
        tree_sync_enabled = self._agentsdb_tree_sync_enabled()
        if repository is not None and tree_sync_enabled:
            try:
                record = self._load_tree_record_from_agentsdb(repository)
                tree_payload = self._extract_tree_payload_from_record(record)
                if isinstance(tree_payload, dict):
                    self._last_stream_cursor = self._load_tree_stream_cursor_from_agentsdb(repository) or self._tree_stream_cursor_from_tree_record(record)
                    return self._normalize_tree_data_structure(tree_payload), "agents_db", self._tree_object_id()
            except Exception as exc:
                if self._agentsdb_strict_mode():
                    raise RuntimeError(f"agents_db tree load failed: {exc}") from exc
                print(f"[WARNING] agents_db tree load failed, trying legacy backends: {exc}")

        mongo_collection = self._get_collection()
        if mongo_collection is not None:
            try:
                document = mongo_collection.find_one({"_id": self._document_key()})
                payload = document.get("data") if isinstance(document, dict) else None
                if isinstance(payload, dict):
                    return self._normalize_tree_data_structure(payload), "mongodb", self._target_label()
            except Exception as exc:
                print(f"[WARNING] MongoDB tree load failed, falling back to JSON: {exc}")

        if self._json_path.exists():
            with open(self._json_path, "r", encoding="utf-8") as data_file:
                loaded_data = json.load(data_file)
            if isinstance(loaded_data, dict):
                return self._normalize_tree_data_structure(loaded_data), "json", str(self._json_path)

        return self._normalize_tree_data_structure({}), "empty", str(self._json_path)

    def load_live_update(
        self,
        previous_cursor: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not self.live_sync_enabled():
            return None, self.load_last_stream_cursor() or self._normalize_tree_stream_cursor(previous_cursor)

        runtime_config, repository = self._load_agentsdb_repository()
        if repository is None:
            return None, self.load_last_stream_cursor() or self._normalize_tree_stream_cursor(previous_cursor)

        if self._should_project_agentsdb_repository(repository):
            seed_data, _backend_name, _source = self._load_tree_seed_payload(
                runtime_config=runtime_config,
                repository=repository,
            )
            resolved_tree_payload = self._resolve_tree_view_data(
                seed_data,
                runtime_config=runtime_config,
                repository=repository,
            )
            current_cursor = self._build_repository_projection_cursor(resolved_tree_payload)
            normalized_previous_cursor = self._normalize_tree_stream_cursor(previous_cursor)
            if normalized_previous_cursor == current_cursor:
                self._last_stream_cursor = dict(current_cursor)
                return None, self.load_last_stream_cursor()
            self._last_stream_cursor = dict(current_cursor)
            return resolved_tree_payload, self.load_last_stream_cursor()

        current_cursor = self._load_tree_stream_cursor_from_agentsdb(repository)
        normalized_previous_cursor = self._normalize_tree_stream_cursor(previous_cursor)
        if current_cursor is not None and normalized_previous_cursor == current_cursor:
            self._last_stream_cursor = dict(current_cursor)
            return None, self.load_last_stream_cursor()

        tree_record = self._load_tree_record_from_agentsdb(repository)
        tree_payload = self._extract_tree_payload_from_record(tree_record)
        if not isinstance(tree_payload, dict):
            if current_cursor is not None:
                self._last_stream_cursor = dict(current_cursor)
            return None, self.load_last_stream_cursor()

        normalized_tree_payload = self._merge_local_projection_sections(
            self._normalize_tree_data_structure(tree_payload),
            runtime_config=runtime_config,
            repository=repository,
        )
        resolved_cursor = current_cursor or self._tree_stream_cursor_from_tree_record(tree_record)
        self._last_stream_cursor = self._normalize_tree_stream_cursor(resolved_cursor)
        return normalized_tree_payload, self.load_last_stream_cursor()

    def stream_live_updates(
        self,
        previous_cursor: dict[str, Any] | None = None,
        *,
        stop_event: threading.Event | None = None,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Iterable[tuple[dict[str, Any], dict[str, Any] | None]]:
        if not self.supports_push_stream():
            return

        runtime_config, repository = self._load_agentsdb_repository()
        subscribe_tree_stream = getattr(repository, "subscribe_tree_stream", None) if repository is not None else None
        subscribe_repository_stream = getattr(repository, "subscribe_repository_stream", None) if repository is not None else None
        if runtime_config is None or repository is None:
            return

        normalized_previous_cursor = self._normalize_tree_stream_cursor(previous_cursor)
        last_event_id = str((normalized_previous_cursor or {}).get("event_id") or "").strip() or None

        if self._should_project_agentsdb_repository(repository):
            if not callable(subscribe_repository_stream):
                return
            object_name_list = sorted(
                {
                    str(object_name or "").strip().lower()
                    for object_name in getattr(repository, "_OBJECT_COLLECTION_MAP", {}).keys()
                    if str(object_name or "").strip()
                }
            )
            for response_payload in subscribe_repository_stream(
                last_event_id=last_event_id,
                stop_event=stop_event,
                object_names=object_name_list,
                include_meta=True,
            ):
                if isinstance(response_payload, dict) and (
                    bool(response_payload.get("subscribed")) or bool(response_payload.get("heartbeat"))
                ):
                    if callable(status_callback):
                        status_callback(dict(response_payload))
                    continue
                normalized_tree_payload = self._resolve_tree_view_data(
                    self._inmemory_tree_data,
                    runtime_config=runtime_config,
                    repository=repository,
                )
                self._inmemory_tree_data = self._strip_derived_agentsdb_repository_sections(normalized_tree_payload)
                projection_cursor = self._build_repository_projection_cursor(normalized_tree_payload)
                event_cursor = self._normalize_tree_stream_cursor(
                    response_payload.get("stream_cursor") if isinstance(response_payload, dict) else None
                )
                stream_cursor = {
                    "event_id": str((event_cursor or {}).get("event_id") or projection_cursor.get("event_id") or "").strip(),
                    "updated_at": str((event_cursor or {}).get("updated_at") or projection_cursor.get("updated_at") or "").strip(),
                    "tree_hash": str(projection_cursor.get("tree_hash") or "").strip(),
                }
                normalized_stream_cursor = self._normalize_tree_stream_cursor(stream_cursor)
                if normalized_stream_cursor is not None:
                    self._last_stream_cursor = dict(normalized_stream_cursor)
                    last_event_id = str(normalized_stream_cursor.get("event_id") or "").strip() or last_event_id
                yield normalized_tree_payload, self.load_last_stream_cursor() or normalized_stream_cursor
            return

        if not callable(subscribe_tree_stream):
            return

        for response_payload in subscribe_tree_stream(
            self._tree_object_id(),
            last_event_id=last_event_id,
            stop_event=stop_event,
            include_meta=True,
        ):
            if isinstance(response_payload, dict) and (
                bool(response_payload.get("subscribed")) or bool(response_payload.get("heartbeat"))
            ):
                if callable(status_callback):
                    status_callback(dict(response_payload))
                continue
            tree_payload = response_payload.get("tree_data") if isinstance(response_payload, dict) else None
            if not isinstance(tree_payload, dict):
                continue
            normalized_tree_payload = self._merge_local_projection_sections(
                self._normalize_tree_data_structure(tree_payload),
                runtime_config=runtime_config,
                repository=repository,
            )
            stream_cursor = self._normalize_tree_stream_cursor(
                response_payload.get("stream_cursor") if isinstance(response_payload, dict) else None
            )
            if stream_cursor is None:
                stream_cursor = self._normalize_tree_stream_cursor(
                    {
                        "event_id": response_payload.get("event_id") if isinstance(response_payload, dict) else None,
                        "updated_at": response_payload.get("updated_at") if isinstance(response_payload, dict) else None,
                        "tree_hash": response_payload.get("tree_hash") if isinstance(response_payload, dict) else None,
                    }
                )
            if stream_cursor is not None:
                self._last_stream_cursor = dict(stream_cursor)
                last_event_id = str(stream_cursor.get("event_id") or "").strip() or last_event_id
            yield normalized_tree_payload, self.load_last_stream_cursor() or stream_cursor

    def _load_storage_config(self) -> dict[str, Any]:
        if self._storage_config_cache is not None:
            return self._storage_config_cache
        if not self._json_path.exists():
            self._storage_config_cache = {}
            return self._storage_config_cache
        try:
            with open(self._json_path, "r", encoding="utf-8") as config_file:
                payload = json.load(config_file)
            projects_payload = payload.get("PROJECTS") if isinstance(payload, dict) else None
            storage_payload = projects_payload.get("tree_widget_storage") if isinstance(projects_payload, dict) else None
            self._storage_config_cache = storage_payload if isinstance(storage_payload, dict) else {}
        except Exception:
            self._storage_config_cache = {}
        return self._storage_config_cache

    def _storage_backend(self) -> str:
        # JSON-first by default; backend persistence is opt-in via env or config.
        configured_backend = str(os.getenv("ALDE_TREE_STORAGE_BACKEND", "")).strip().lower()
        if not configured_backend:
            configured_backend = str(self._load_storage_config().get("backend", "json")).strip().lower()
        return configured_backend or "json"

    def _mongo_uri(self) -> str:
        uri = str(os.getenv("ALDE_TREE_MONGO_URI", "")).strip()
        if not uri:
            uri = str(self._load_storage_config().get("mongo_uri", "")).strip()
        if uri:
            return uri

        # Prefer AgentsDB backend URI when it points to a Mongo-compatible endpoint.
        agentsdb_backend_uri = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI", "")).strip()
        if agentsdb_backend_uri:
            parsed_backend_uri = urlparse(agentsdb_backend_uri)
            if parsed_backend_uri.scheme in {"mongodb", "mongodb+srv"}:
                return agentsdb_backend_uri
            
    def _database_name(self) -> str:
        configured = str(os.getenv("ALDE_TREE_AGENT_DB", "")).strip()
        if not configured:
            configured = str(self._load_storage_config().get("mongo_database", "")).strip()
        if configured:
            return configured

        knowledge_db = str(os.getenv("AI_IDE_KNOWLEDGE_AGENT_DB", "")).strip()
        if not knowledge_db:
            knowledge_db = str(os.getenv("AI_IDE_KNOWLEDGE_AGENT_DB", "")).strip()
        return knowledge_db or "alde_tree_data"

    def _collection_name(self) -> str:
        configured = str(os.getenv("ALDE_TREE_COLLECTION", "")).strip()
        if not configured:
            configured = str(self._load_storage_config().get("collection", "")).strip()
        return configured or "tree_widget"

    def _document_key(self) -> str:
        configured = str(os.getenv("ALDE_TREE_DOCUMENT_KEY", "")).strip()
        if not configured:
            configured = str(self._load_storage_config().get("document_key", "")).strip()
        return configured or "tree_data"

    def _target_label(self) -> str:
        return f"{self._database_name()}.{self._collection_name()}/{self._document_key()}"

    def _get_collection(self) -> Any | None:
        if self._mongo_disabled:
            return None
        if self._mongo_collection is not None:
            return self._mongo_collection
        if self._storage_backend() != "mongodb":
            self._mongo_disabled = True
            return None

        mongo_client_class = _load_optional_mongo_client_class()
        if mongo_client_class is None:
            self._mongo_disabled = True
            return None

        try:
            self._mongo_client = mongo_client_class(self._mongo_uri(), serverSelectionTimeoutMS=1500)
            database = self._mongo_client[self._database_name()]
            self._mongo_collection = database[self._collection_name()]
            return self._mongo_collection
        except Exception:
            self._mongo_disabled = True
            return None

    def load_data(self) -> tuple[dict[str, Any], str, str]:
        if self.memory_only_enabled():
            runtime_config, repository = self._load_agentsdb_repository()
            loaded_data = self._resolve_tree_view_data(
                self._inmemory_tree_data,
                runtime_config=runtime_config,
                repository=repository,
            )
            self._inmemory_tree_data = self._strip_derived_agentsdb_repository_sections(loaded_data)
            self._inmemory_tree_hash = self._tree_data_content_hash(self._inmemory_tree_data)
            if repository is not None and self._should_project_agentsdb_repository(repository):
                self._last_stream_cursor = self._build_repository_projection_cursor(loaded_data)
                return loaded_data, "agents_db_repository", self._agentsdb_repository_source(runtime_config)
            return loaded_data, "memory", "inmemory"

        runtime_config, repository = self._load_agentsdb_repository()
        tree_sync_enabled = self._agentsdb_tree_sync_enabled()
        self._last_stream_cursor = None
        if repository is not None and not tree_sync_enabled:
            self._purge_agentsdb_tree_object(repository)

        seed_data, seed_backend, seed_source = self._load_tree_seed_payload(
            runtime_config=runtime_config,
            repository=repository,
        )
        normalized_loaded_data = self._resolve_tree_view_data(
            seed_data,
            runtime_config=runtime_config,
            repository=repository,
        )
        persistable_payload = self._strip_derived_agentsdb_repository_sections(normalized_loaded_data)

        if repository is not None and self._should_project_agentsdb_repository(repository):
            self._last_stream_cursor = self._build_repository_projection_cursor(normalized_loaded_data)
            return normalized_loaded_data, "agents_db_repository", self._agentsdb_repository_source(runtime_config)

        if seed_backend == "agents_db":
            previous_tree_hash = self._tree_data_content_hash(seed_data)
            if previous_tree_hash != self._tree_data_content_hash(persistable_payload):
                try:
                    stream_cursor = self._upsert_tree_payload_to_agentsdb(
                        repository=repository,
                        runtime_config=runtime_config,
                        data=persistable_payload,
                        change_event={
                            "action": "projection_merge",
                            "origin": "tree_widget_load",
                            "source_backend": "agents_db",
                        },
                    )
                    if stream_cursor is not None:
                        self._last_stream_cursor = stream_cursor
                except Exception:
                    pass
            return normalized_loaded_data, "agents_db", seed_source

        if seed_backend == "mongodb":
            if tree_sync_enabled and repository is not None and runtime_config is not None:
                try:
                    stream_cursor = self._upsert_tree_payload_to_agentsdb(
                        repository=repository,
                        runtime_config=runtime_config,
                        data=persistable_payload,
                        change_event={
                            "action": "bootstrap",
                            "origin": "mongodb",
                            "source_backend": "mongodb",
                        },
                    )
                    if stream_cursor is not None:
                        self._last_stream_cursor = stream_cursor
                except Exception:
                    pass
            return normalized_loaded_data, "mongodb", seed_source

        if seed_backend == "json":
            if tree_sync_enabled and repository is not None and runtime_config is not None:
                try:
                    stream_cursor = self._upsert_tree_payload_to_agentsdb(
                        repository=repository,
                        runtime_config=runtime_config,
                        data=persistable_payload,
                        change_event={
                            "action": "bootstrap",
                            "origin": "json",
                            "source_backend": "json",
                        },
                    )
                    if stream_cursor is not None:
                        self._last_stream_cursor = stream_cursor
                except Exception:
                    pass
            return normalized_loaded_data, "json", seed_source

        empty_payload = normalized_loaded_data
        if tree_sync_enabled and repository is not None and runtime_config is not None:
            try:
                stream_cursor = self._upsert_tree_payload_to_agentsdb(
                    repository=repository,
                    runtime_config=runtime_config,
                    data=persistable_payload,
                    change_event={
                        "action": "bootstrap",
                        "origin": "tree_widget",
                        "reason": "empty_tree_bootstrap",
                    },
                )
                if stream_cursor is not None:
                    self._last_stream_cursor = stream_cursor
                return empty_payload, "agents_db", self._tree_object_id()
            except Exception as exc:
                if self._agentsdb_strict_mode():
                    raise RuntimeError(f"agents_db tree bootstrap failed: {exc}") from exc
                print(f"[WARNING] agents_db tree bootstrap failed, falling back to JSON: {exc}")
        return empty_payload, "json", str(self._json_path)

    def save_data(self, data: dict[str, Any], *, change_event: dict[str, Any] | None = None) -> tuple[str, str]:
        if self.memory_only_enabled():
            self._store_inmemory_tree_data(
                self._strip_derived_agentsdb_repository_sections(data),
                change_event=change_event,
            )
            return "memory", "inmemory"

        runtime_config, repository = self._load_agentsdb_repository()
        tree_sync_enabled = self._agentsdb_tree_sync_enabled()
        persistable_input = self._strip_derived_agentsdb_repository_sections(data)
        normalized_data = self._merge_local_projection_sections(
            self._normalize_tree_data_structure(persistable_input),
            runtime_config=runtime_config,
            repository=repository,
        )
        if repository is not None and runtime_config is not None:
            if tree_sync_enabled:
                try:
                    stream_cursor = self._upsert_tree_payload_to_agentsdb(
                        repository=repository,
                        runtime_config=runtime_config,
                        data=normalized_data,
                        change_event=change_event or {"action": "snapshot", "origin": "tree_widget"},
                    )
                    if stream_cursor is not None:
                        self._last_stream_cursor = stream_cursor
                    return "agents_db", self._tree_object_id()
                except Exception as exc:
                    if self._agentsdb_strict_mode():
                        raise RuntimeError(f"agents_db tree save failed: {exc}") from exc
                    print(f"[WARNING] agents_db tree save failed, trying legacy backends: {exc}")
            else:
                self._purge_agentsdb_tree_object(repository)

        _collection = self._get_collection()
        if _collection is not None:
            try:
                _collection.upsert_object(
                    {"_id": self._document_key()},
                    {
                        "$set": {
                            "data": normalized_data,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    upsert=True,
                )
                return "agentsdb", self._target_label()
            except Exception as exc:
                print(f"[WARNING] agentsdb tree save failed, falling back to JSON: {exc}")

        if tree_sync_enabled and self._agentsdb_strict_mode():
            raise RuntimeError("agents_db tree persistence required but unavailable")

        self._app_data_dir.mkdir(exist_ok=True)
        with open(self._json_path, "w", encoding="utf-8") as data_file:
            json.dump(normalized_data, data_file, indent=2, ensure_ascii=False)
        return "json", str(self._json_path)


# --------------------- Helper: icon loader (minimal) ---------------------
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication


def _tinted_icon(base: QIcon, *, color: QColor | str, size: int = 16) -> QIcon:
    """Return a tinted variant of `base`.

    This is used to colorize monochrome SVG icons to match the current accent.
    """
    if base.isNull() or QApplication.instance() is None:
        return base

    qcolor = QColor(color) if isinstance(color, str) else color
    pm = base.pixmap(size, size)
    if pm.isNull():
        return base

    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, pm)
    # colorize while keeping alpha from source icon
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(out.rect(), qcolor)
    p.end()
    return QIcon(out)


def _icon_with_marker(base: QIcon, *, marker_color: QColor | str, size: int = 16) -> QIcon:
    """Overlay a small accent marker ("fmarker") onto an icon."""
    if base.isNull() or QApplication.instance() is None:
        return base

    qcolor = QColor(marker_color) if isinstance(marker_color, str) else marker_color
    pm = base.pixmap(size, size)
    if pm.isNull():
        return base

    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, pm)

    # Bottom-right dot marker
    r = max(3, size // 5)
    margin = max(1, size // 12)
    cx = size - margin - r
    cy = size - margin - r
    p.setPen(Qt.NoPen)
    p.setBrush(qcolor)
    p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
    p.end()
    return QIcon(out)


def _icon_with_badge_text(
    base: QIcon,
    *,
    text: str,
    badge_color: QColor | str,
    text_color: QColor | str = "#ffffff",
    size: int = 18,
) -> QIcon:
    """Overlay a small date badge onto an icon.

    Used for HISTORY root items (e.g. "Chat History") to show the last entry date.
    """
    if base.isNull() or QApplication.instance() is None:
        return base

    t = (text or "").strip()
    if not t:
        return base

    badge_qcolor = QColor(badge_color) if isinstance(badge_color, str) else badge_color
    text_qcolor = QColor(text_color) if isinstance(text_color, str) else text_color

    pm = base.pixmap(size, size)
    if pm.isNull():
        return base

    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, pm)

    # Badge geometry (bottom-right)
    font = QFont("Fira Code", max(6, size // 3))
    font.setBold(True)
    p.setFont(font)
    metrics = p.fontMetrics()
    pad_x = max(2, size // 10)
    pad_y = max(1, size // 12)
    text_w = metrics.horizontalAdvance(t)
    text_h = metrics.height()
    badge_w = min(size, text_w + 2 * pad_x)
    badge_h = min(size, text_h + 2 * pad_y)
    x = size - badge_w
    y = size - badge_h

    # badge background
    p.setPen(Qt.NoPen)
    p.setBrush(badge_qcolor)
    radius = max(2, badge_h // 3)
    p.drawRoundedRect(x, y, badge_w, badge_h, radius, radius)

    # badge text
    p.setPen(text_qcolor)
    p.drawText(x, y, badge_w, badge_h, Qt.AlignCenter, t)
    p.end()
    return QIcon(out)



SCROLLBAR_HOVER_ONLY_DARK = """
/* ==== generic dark style – hide until mouse-over, no arrows ==== */

/* --- shared  -------------------------------------------------- */
QScrollBar:horizontal, QScrollBar:vertical {
    background: transparent;          /* nothing until hover        */
    margin: 0px;                      /* no outer gaps              */
    border: none;
}

/* size while idle (almost invisible but still receives hover)   */
QScrollBar:vertical   { width: 6px;  }
QScrollBar:horizontal { height:50px;  }

/* grow a bit + colour when mouse enters the bar itself          */
QScrollBar:vertical:hover   { width: 6px; }
QScrollBar:horizontal:hover { height:50px; }

/* ----- handle (the draggable knob) --------------------------- */
QScrollBar::handle {
    background: rgba(120,120,120,0.0);   /* transparent while idle  */
    border-radius: 4px;
    min-width: 6px;
    min-height: 600px;
}
QScrollBar::handle:hover {
    background: rgba(120,120,120,0.6);   /* show on hover           */
}

/* ----- remove arrows & useless areas ------------------------- */
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;  border: none;  width:0px; height:0px;
}
"""

_LOCAL_ICON_CACHE: dict[str, QIcon | None] = {}

def _icon(name: str) -> QIcon:
    """Import-safe icon loader.

    Supports:
    - Local icons in the `symbols/` folder (current behavior)
    - Optional http(s) URLs (downloaded + cached on disk)

    Returns an empty QIcon before QApplication exists.
    """

    if QApplication.instance() is None:
        return QIcon()

    s = (name or "").strip()
    if not s:
        return QIcon()

    if s.startswith("http://") or s.startswith("https://"):
        return _icon_from_url(s)

    if s in _LOCAL_ICON_CACHE:
        cached_icon = _LOCAL_ICON_CACHE.get(s)
        return QIcon(cached_icon) if isinstance(cached_icon, QIcon) else QIcon()

    p = Path(__file__).with_name("symbols") / s
    if p.is_file():
        icon = QIcon(str(p))
        _LOCAL_ICON_CACHE[s] = icon
        return QIcon(icon)

    _LOCAL_ICON_CACHE[s] = None
    return QIcon()


def _icon_cache_dir() -> Path:
    # Allow override for privacy/offline control.
    override = os.environ.get("AI_IDE_ICON_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).parent.parent / "AppData" / "icon_cache"


def _icon_from_url(url: str, *, timeout_s: float = 3.0) -> QIcon:
    """Download an icon from the web and cache it locally.

    Notes:
    - This is best-effort: network failures simply return an empty icon.
    - Use only URLs you have rights to use (e.g. Material icons are Apache-2.0).
    """
    if QApplication.instance() is None:
        return QIcon()

    u = (url or "").strip()
    if not u:
        return QIcon()

    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        return QIcon()

    cache_dir = _icon_cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return QIcon()

    key = hashlib.sha256(u.encode("utf-8")).hexdigest()
    for ext in (".svg", ".png", ".jpg", ".jpeg"):
        candidate = cache_dir / f"{key}{ext}"
        if candidate.is_file():
            return QIcon(str(candidate))

    try:
        req = Request(u, headers={"User-Agent": "alde/1.0"})
        with urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except Exception:
        return QIcon()

    if not data:
        return QIcon()

    # Pick a file extension Qt can understand.
    ext = ".svg" if u.lower().endswith(".svg") else ""
    if "image/svg" in ctype:
        ext = ".svg"
    elif "image/png" in ctype:
        ext = ".png"
    elif "image/jpeg" in ctype or "image/jpg" in ctype:
        ext = ".jpg"
    else:
        head = data.lstrip()[:200].lower()
        if head.startswith(b"<svg") or b"<svg" in head or b"image/svg+xml" in head:
            ext = ".svg"
        elif data.startswith(b"\x89PNG"):
            ext = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"

    if not ext:
        return QIcon()

    path = cache_dir / f"{key}{ext}"
    tmp = cache_dir / f"{key}{ext}.tmp"
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return QIcon()

    return QIcon(str(path))


# ------------------------- JsonTreeWidgetWithToolbar -------------------------------
class JsonTreeWidgetWithToolbar(QWidget):
    """Wrapper widget that contains toolbar buttons above the JsonTreeWidget."""

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def sizeHint(self) -> QSize:
        return QSize(220, 180)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("JsonTreeWidgetWithToolbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        
        # Create main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Create toolbar frame
        toolbar = QFrame(self)
        self._toolbar = toolbar
        toolbar.setFixedHeight(28)
        toolbar.setObjectName("JsonTreeToolbar")
        self._bg_color = "#0b0b0b"
        self._accent_color = "#3a5fff"
        self._toolbar_style_template = """
            QFrame#JsonTreeToolbar {{
                background: {bg};
                border: 1px solid #303030;
                border-bottom: none;
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }}
            QToolButton {{
                background: transparent;
                border: none;
                border-radius: 3px;
                padding: 2px;
                max-width: 24px;
                max-height: 24px;
                color: #E3E3DED6;
            }}
            QToolButton:hover {{
                background: rgba(255, 255, 255, 0.10);
                border: none;
            }}
            QToolButton:pressed {{
                background: rgba(255, 255, 255, 0.16);
                border: none;
            }}
        """
        self._apply_toolbar_style(toolbar)
        self._apply_wrapper_style()
        
        # Create toolbar layout
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 2, 8, 2)
        toolbar_layout.setSpacing(2)
        
        # Create tree widget
        self.tree = JsonTreeWidget(self)
        self.tree.setMinimumSize(0, 0)
        self.tree.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        
        # Create buttons
        self._btn_load_history = QToolButton(toolbar)
        icon_hist = _icon("load_content.svg")
        if not icon_hist.isNull():
            self._btn_load_history.setIcon(icon_hist)
        else:
            self._btn_load_history.setText("📂")
        self._btn_load_history.setToolTip("Load project history")
        self._btn_load_history.setFixedSize(26, 26)
        self._btn_load_history.clicked.connect(self.tree._show_history_tree)
        
        self._btn_collapse_all = QToolButton(toolbar)
        icon_collapse = _icon("expansion_panels.svg")
        if not icon_collapse.isNull():
            self._btn_collapse_all.setIcon(icon_collapse)
        else:
            self._btn_collapse_all.setText("⬇")
        self._btn_collapse_all.setToolTip("Collapse all items")
        self._btn_collapse_all.setFixedSize(26, 26)
        self._btn_collapse_all.clicked.connect(self.tree.collapseAll)
        
        self._btn_add_project = QToolButton(toolbar)
        icon_add = _icon("deployed_code.svg")
        if not icon_add.isNull():
            self._btn_add_project.setIcon(icon_add)
        else:
            self._btn_add_project.setText("➕")
        self._btn_add_project.setToolTip("Add project root")
        self._btn_add_project.setFixedSize(26, 26)
        self._btn_add_project.clicked.connect(self.tree._add_project_root)
        
        self._btn_import_json = QToolButton(toolbar)
        # General import entry point for JSON/YAML/TOML/Python data files.
        icon_import = _icon("open_file.svg")
        if not icon_import.isNull():
            self._btn_import_json.setIcon(icon_import)
        else:
            self._btn_import_json.setText("📥")
        self._btn_import_json.setToolTip("Import file")
        self._btn_import_json.setFixedSize(26, 26)
        self._btn_import_json.clicked.connect(self.tree._import_data_file_dialog)
        
        self._btn_export_json = QToolButton(toolbar)
        icon_export = _icon("file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg")
        if not icon_export.isNull():
            self._btn_export_json.setIcon(icon_export)
        else:
            self._btn_export_json.setText("📤")
        self._btn_export_json.setToolTip("Export to JSON file")
        self._btn_export_json.setFixedSize(26, 26)
        self._btn_export_json.clicked.connect(self.tree._export_json_file)
        
        self._btn_templates = QToolButton(toolbar)
        icon_template = _icon("schema_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg")
        if not icon_template.isNull():
            self._btn_templates.setIcon(icon_template)
        else:
            self._btn_templates.setText("📋")
        self._btn_templates.setToolTip("Load template")
        self._btn_templates.setFixedSize(26, 26)
        self._btn_templates.clicked.connect(self.tree._load_template)
        
        # Add buttons to toolbar
        toolbar_layout.addWidget(self._btn_load_history)
        toolbar_layout.addWidget(self._btn_collapse_all)
        toolbar_layout.addWidget(self._btn_add_project)
        toolbar_layout.addWidget(self._btn_import_json)
        toolbar_layout.addWidget(self._btn_export_json)
        toolbar_layout.addWidget(self._btn_templates)
        toolbar_layout.addStretch()
        
        # Add widgets to main layout
        layout.addWidget(toolbar)
        layout.addWidget(self.tree)

    def set_accent_color(self, color: QColor | str) -> None:
        """Update accent-dependent colors (toolbar + root icons)."""
        if isinstance(color, QColor):
            color_str = color.name(QColor.HexRgb)
        else:
            color_str = str(color).strip() or self._accent_color

        if color_str == self._accent_color:
            return
        self._accent_color = color_str
        self._apply_toolbar_style(self._toolbar)
        self.tree.set_accent_color(color_str)
    
    def _apply_wrapper_style(self) -> None:
        self.setStyleSheet(
            f"QWidget#JsonTreeWidgetWithToolbar {{ background: {self._bg_color}; border-radius: 14px; }}"
        )

    def _apply_toolbar_style(self, toolbar: QFrame) -> None:
        toolbar.setStyleSheet(
            self._toolbar_style_template.format(bg=self._bg_color, accent=self._accent_color)
        )

    def set_text_color(self, color: QColor | str) -> None:
        self.tree.set_text_color(color)

    def set_background_color(self, color: QColor | str) -> None:
        self._bg_color = color.name(QColor.HexRgb) if isinstance(color, QColor) else str(color)
        self._apply_toolbar_style(self._toolbar)
        self._apply_wrapper_style()
        self.tree.set_background_color(color)

    def set_background_color(self, color: QColor | str) -> None:
        """Set tree and toolbar background to match outer widgets."""
        if isinstance(color, str):
            color = color.strip()
            if not color:
                return
            color_str = color
        elif isinstance(color, QColor):
            color_str = color.name(
                QColor.HexArgb if color.alpha() < 255 else QColor.HexRgb
            )
        else:
            return

        if color_str == self._bg_color:
            return

        self._bg_color = color_str
        self._apply_wrapper_style()
        # toolbar is first child; safe to re-apply via findChild
        toolbar = self.findChild(QFrame, "JsonTreeToolbar")
        if toolbar:
            self._apply_toolbar_style(toolbar)
        self.tree.set_background_color(color_str)

    def set_text_color(self, color: QColor | str) -> None:
        """Forward text color to inner tree."""
        self.tree.set_text_color(color)
    
    # Expose tree methods for convenience
    def add_to_section(self, section_name: str, key: str, value: Any, *, persist: bool = True) -> None:
        self.tree.add_to_section(section_name, key, value, persist=persist)
    
    def remove_from_section(self, section_name: str, item_name: str) -> bool:
        return self.tree.remove_from_section(section_name, item_name)
    
    def set_json(self, data: Any) -> None:
        self.tree.set_json(data)

    def load_live_sync_diagnostic(self) -> dict[str, Any]:
        return self.tree.load_live_sync_diagnostic()

    def run_manual_sync(self, *, source_label: str = "manual_sync") -> bool:
        return bool(self.tree.run_manual_sync(source_label=source_label))


# ------------------------- JsonTreeWidget -------------------------------
class JsonTreeWidget(QTreeWidget):
    _DEFAULT_ROOT_SECTION_LAYOUT: tuple[tuple[str, bool], ...] = (
        ("RUNTIME", True),
        ("CHAT_HISTORY", True),
        ("DATABASES", True),
        ("MCP", True),
        ("ENV", True),
    )
    _SMALL_FONT_SECTION_NAMES: set[str] = {"PROJECTS", "CHAT_HISTORY"}
    _HISTORY_SECTION_NAMES: set[str] = {"CHAT_HISTORY", "HISTORY"}
    _HIDDEN_ROOT_SECTION_NAMES: set[str] = {"PROJECTS"}
    _TREE_ICON_SIZE = QSize(20, 20)
    # Block-based layout: hierarchy is conveyed by grouped chips, not indentation.
    _TREE_INDENTATION = 0
    _initial_load_async_result_ready = Signal(object)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def sizeHint(self) -> QSize:
        return QSize(200, 160)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.setFrameShape(QFrame.NoFrame)
        self.setHeaderHidden(True)
        self.viewport().setAutoFillBackground(True)
        # Tree labels can have small vertical offsets between semantic groups.
        self.setUniformRowHeights(False)
        self.setAnimated(True)
        self.setIconSize(self._TREE_ICON_SIZE)
        # Keep labels readable in narrow docks by reducing the default branch gap.
        self.setIndentation(self._TREE_INDENTATION)

        # Guards / caches for persistence.
        self._initializing = True
        self._item_last_text: dict[QTreeWidgetItem, str] = {}
        self._last_saved_hash: str | None = None
        self._live_sync_cursor: dict[str, Any] | None = None
        self._live_sync_timer: QTimer | None = None
        self._live_sync_poll_in_flight = False
        self._live_sync_diagnostic_lock = threading.RLock()
        self._live_sync_diagnostic: dict[str, Any] = {}
        self._last_live_sync_log_key: str = ""
        self._last_live_sync_log_at: float = 0.0
        self._live_stream_bridge: _TreePushStreamBridge | None = None
        self._live_stream_thread: threading.Thread | None = None
        self._live_stream_stop_event: threading.Event | None = None
        self._push_update_timer: QTimer | None = None
        self._push_update_pending: tuple[Any, Any] | None = None
        self._push_update_apply_in_flight = False
        self._initial_load_inflight = False
        app_data_dir = Path(__file__).parent.parent / "AppData"
        self._persistence_service = TreeDataPersistenceService(app_data_dir)
        self._initial_load_async_enabled = self._initial_tree_load_async_enabled()
        self._initialize_live_sync_diagnostic()
        
        # Store data for each section separately
        self._data: dict[str, dict[str, Any]] = {}
        
        # Store multiple root categories (like VS Code sections)
        self._root_sections: dict[str, QTreeWidgetItem] = {}
        
        # Track which items belong to which section
        self._item_to_section: dict[QTreeWidgetItem, str] = {}
        self._item_to_key: dict[QTreeWidgetItem, str] = {}
        self._item_kind: dict[QTreeWidgetItem, str] = {}
        self._item_badge: dict[QTreeWidgetItem, str] = {}
        self._lazy_children: dict[QTreeWidgetItem, tuple[Any, str | None]] = {}
        self._linked_root_expand_sync_in_flight = False

        self._style_template = """
               QTreeWidget, QTreeView, QAbstractItemView, QAbstractScrollArea {{
                   background: {bg_color};
                   background-color:{bg_color};
                   color:{text_color};
                   font-family:'Fira Code', monospace;
                   border: 1px solid {frame_color};
                   border-top: none;
                   border-top-left-radius: 0px;
                   border-top-right-radius: 0px;
                   border-bottom-left-radius: 14px;
                   border-bottom-right-radius: 14px;
                   padding: 4px 0px 6px 0px;
                   outline: none;
               }}
               QTreeWidget::viewport, QTreeView::viewport {{
                   background: {bg_color};
                   background-color:{bg_color};
                   border: none;
               }}
               QTreeWidget::corner, QTreeView::corner {{
                   background: {bg_color};
                   border: none;
               }}
               QTreeWidget::item, QTreeView::item {{
                   margin: 0px;
                   padding: 0px;
                   color:{text_color};
                   border: none;
                   background: transparent;
               }}
               QTreeWidget::item:selected,
               QTreeView::item:selected,
               QTreeWidget::item:selected:active,
               QTreeView::item:selected:active,
               QTreeWidget::item:selected:!active,
               QTreeView::item:selected:!active {{
                   color:{text_color};
                   border: none;
                   background: transparent;
               }}
               QTreeWidget::branch, QTreeView::branch {{ color:{branch_color}; }}
               QTreeWidget::branch:has-children:!adjoins-item,
               QTreeView::branch:has-children:!adjoins-item {{
                   background-color:{bg_color};
               }}
               QTreeWidget::branch:closed:has-children:!adjoins-item,
               QTreeView::branch:closed:has-children:!adjoins-item {{
                   background-color:{bg_color};
               }}
               QTreeWidget::branch:open:has-children:!adjoins-item,
               QTreeView::branch:open:has-children:!adjoins-item {{
                   background-color:{bg_color};
               }}
               """
        self._branch_color = "#2d8cf0"
        self._text_color = "#E3E3DE"
        self._bg_color = "#0b0b0b"
        self._accent_color = "#3a5fff"
        self._frame_color = "#303030"
        self._muted_color = "#9a9a95"

        # Typography
        self._section_header_font_size = 10
        self._section_item_font_size_small = 10
        self._apply_stylesheet()

        # NOTE: itemChanged is connected after initial population to avoid
        # triggering persistence while icons/fonts are being applied.
        
        # Enable context menu
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._initial_load_async_result_ready.connect(self._handle_initial_tree_load_result)
        
        # Initialize default root sections
        self._initialize_root_sections()

        # Connect signal for handling item edits (after initial load).
        self.setExpandsOnDoubleClick(False)
        self.itemClicked.connect(self._on_item_single_clicked)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.itemExpanded.connect(self._on_item_expanded)
        self.itemCollapsed.connect(self._on_item_collapsed)
        self.itemChanged.connect(self._on_item_changed)
        self._remember_tree_texts()
        self._initializing = False
        self._update_live_sync_cursor()
        self.destroyed.connect(self._handle_widget_destroyed)
        if self._initial_load_async_enabled:
            self._start_initial_tree_load_async()
        self._start_live_sync_transport()

    def _initial_tree_load_async_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_TREE_INITIAL_LOAD_ASYNC", "1") or "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _start_initial_tree_load_async(self) -> None:
        if self._initial_load_inflight:
            return
        self._initial_load_inflight = True

        def _worker() -> None:
            try:
                loaded_data, backend_name, source = self._persistence_service.load_data()
                payload = {
                    "ok": True,
                    "loaded_data": loaded_data,
                    "backend_name": str(backend_name or "memory"),
                    "source": str(source or ""),
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            try:
                self._initial_load_async_result_ready.emit(payload)
            except RuntimeError:
                return

        threading.Thread(target=_worker, name="tree-initial-load", daemon=True).start()

    @Slot(object)
    def _handle_initial_tree_load_result(self, payload: object) -> None:
        self._initial_load_inflight = False
        if not isinstance(payload, dict):
            return

        if not bool(payload.get("ok")):
            error_text = str(payload.get("error") or "").strip()
            if error_text:
                print(f"[INFO] Could not load tree data (async init): {error_text}")
            return

        loaded_data = payload.get("loaded_data")
        if not isinstance(loaded_data, dict):
            return

        backend_name = str(payload.get("backend_name") or "memory")
        source = str(payload.get("source") or "")
        self._apply_loaded_tree_data(
            loaded_data,
            backend_name=backend_name,
            source=source,
            log_message=backend_name != "memory",
        )

    def _initialize_live_sync_diagnostic(self) -> None:
        self._live_sync_diagnostic = {
            "enabled": bool(self._persistence_service.live_sync_enabled()),
            "auto_sync_enabled": False,
            "push_enabled": bool(self._persistence_service.push_stream_enabled()),
            "push_supported": False,
            "transport": "disabled",
            "connection_state": "idle",
            "reconnect_attempts": 0,
            "backoff_seconds": 0.0,
            "error_count": 0,
            "last_error": "",
            "last_error_at": "",
            "last_event_id": "",
            "last_event_at": "",
            "last_update_at": "",
            "tree_object_id": self._persistence_service._tree_object_id(),
        }

    def _live_sync_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _push_stream_base_backoff_seconds(self) -> float:
        raw_value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_PUSH_BACKOFF_BASE_SECONDS", "0.5") or "0.5").strip()
        try:
            resolved_value = float(raw_value)
        except Exception:
            resolved_value = 0.5
        return max(0.1, resolved_value)

    def _push_stream_max_backoff_seconds(self) -> float:
        raw_value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_PUSH_BACKOFF_MAX_SECONDS", "8.0") or "8.0").strip()
        try:
            resolved_value = float(raw_value)
        except Exception:
            resolved_value = 8.0
        return max(self._push_stream_base_backoff_seconds(), resolved_value)

    def _compute_push_stream_backoff_seconds(self, reconnect_attempts: int) -> float:
        normalized_attempts = max(1, int(reconnect_attempts or 1))
        base_delay = self._push_stream_base_backoff_seconds()
        max_delay = self._push_stream_max_backoff_seconds()
        return min(max_delay, base_delay * (2 ** max(0, normalized_attempts - 1)))

    def _push_stream_coalesce_interval_ms(self) -> int:
        raw_value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_PUSH_COALESCE_MS", "200") or "200").strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = 200
        return max(0, min(resolved_value, 5000))

    def _push_stream_runtime_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_PUSH_RUNTIME", "0") or "0").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _auto_sync_runtime_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_AUTO_SYNC_RUNTIME", "0") or "0").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _ensure_push_update_timer(self) -> QTimer:
        if isinstance(self._push_update_timer, QTimer):
            return self._push_update_timer
        timer = QTimer(self)
        timer.setObjectName("JsonTreeWidgetPushUpdateCoalesceTimer")
        timer.setSingleShot(True)
        timer.timeout.connect(self._drain_push_stream_update)
        self._push_update_timer = timer
        return timer

    def _schedule_push_stream_update(self, loaded_data: Any, stream_cursor: Any) -> None:
        self._push_update_pending = (loaded_data, stream_cursor)
        timer = self._ensure_push_update_timer()
        if not timer.isActive():
            timer.start(self._push_stream_coalesce_interval_ms())

    def _set_live_sync_diagnostic(self, **updates: Any) -> None:
        with self._live_sync_diagnostic_lock:
            self._live_sync_diagnostic.update(updates)

    def _record_live_sync_cursor(self, stream_cursor: Any, *, update_received_at: bool = False) -> None:
        if not isinstance(stream_cursor, dict):
            return
        diagnostic_updates: dict[str, Any] = {}
        event_id = str(stream_cursor.get("event_id") or "").strip()
        updated_at = str(stream_cursor.get("updated_at") or "").strip()
        if event_id:
            diagnostic_updates["last_event_id"] = event_id
        if updated_at:
            diagnostic_updates["last_event_at"] = updated_at
        if update_received_at:
            diagnostic_updates["last_update_at"] = self._live_sync_now_iso()
        if diagnostic_updates:
            self._set_live_sync_diagnostic(**diagnostic_updates)

    def _record_live_sync_failure(
        self,
        error_text: str,
        *,
        reconnect_attempts: int,
        backoff_seconds: float,
        connection_state: str = "backoff",
    ) -> None:
        with self._live_sync_diagnostic_lock:
            error_count = int(self._live_sync_diagnostic.get("error_count") or 0) + 1
            self._live_sync_diagnostic.update(
                {
                    "transport": "push",
                    "connection_state": connection_state,
                    "reconnect_attempts": max(0, int(reconnect_attempts or 0)),
                    "backoff_seconds": max(0.0, float(backoff_seconds or 0.0)),
                    "error_count": error_count,
                    "last_error": str(error_text or "").strip(),
                    "last_error_at": self._live_sync_now_iso(),
                }
            )

    def load_live_sync_diagnostic(self) -> dict[str, Any]:
        with self._live_sync_diagnostic_lock:
            return dict(self._live_sync_diagnostic)

    @classmethod
    def _is_hidden_root_section_name(cls, section_name: str | None) -> bool:
        return str(section_name or "").strip().upper() in cls._HIDDEN_ROOT_SECTION_NAMES

    def _reset_tree_view_state(self, *, expanded_sections: dict[str, bool] | None = None) -> None:
        self.clear()
        self._root_sections = {}
        self._item_to_section = {}
        self._item_to_key = {}
        self._item_kind = {}
        self._item_badge = {}
        self._lazy_children = {}
        self._item_last_text = {}
        self._data = {}

        allowed_sections = self._persistence_service._tree_section_allowlist()
        for section_name, collapsed in self._resolved_root_section_layout():
            if section_name not in allowed_sections:
                continue
            self._data[section_name] = {}
            if self._is_hidden_root_section_name(section_name):
                continue
            section = self._add_root_section(section_name, collapsed=collapsed)
            if isinstance(expanded_sections, dict) and section_name in expanded_sections:
                section.setExpanded(bool(expanded_sections.get(section_name)))

    def _apply_loaded_tree_data(
        self,
        loaded_data: dict[str, Any],
        *,
        backend_name: str,
        source: str,
        log_message: bool = True,
    ) -> None:
        normalized_loaded_data = self._persistence_service._filter_tree_sections(loaded_data)
        payload = json.dumps(normalized_loaded_data, ensure_ascii=False, sort_keys=True)
        self._last_saved_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        expanded_sections = {
            section_name: section.isExpanded()
            for section_name, section in self._root_sections.items()
            if section is not None
        }

        self._initializing = True
        self.blockSignals(True)
        try:
            self._reset_tree_view_state(expanded_sections=expanded_sections)
            allowed_sections = self._persistence_service._tree_section_allowlist()
            for section_name, section_data in normalized_loaded_data.items():
                if section_name not in allowed_sections:
                    continue
                if not isinstance(section_data, dict):
                    section_data = {}
                self._data[section_name] = dict(section_data)
                if self._is_hidden_root_section_name(section_name):
                    continue
                if section_name not in self._root_sections:
                    section = self._add_root_section(section_name)
                    if section_name in expanded_sections:
                        section.setExpanded(bool(expanded_sections.get(section_name)))
                section = self._root_sections.get(section_name)
                if section is None:
                    continue

                for key, value in section_data.items():
                    item = self._build_item(key, value, section_name=section_name)
                    section.addChild(item)
                    self._item_to_section[item] = section_name
                    self._item_to_key[item] = key
                    self._remember_item_texts_recursive(item)
                    if section_name.upper() in self._HISTORY_SECTION_NAMES:
                        self._item_kind[item] = "history"
                        badge = self._extract_history_badge(value)
                        if badge:
                            self._item_badge[item] = badge
                        self._apply_item_icon(item)
        finally:
            self.blockSignals(False)
            self._update_root_section_header_styles()
            self._update_section_item_font_sizes()
            self._remember_tree_texts()
            self._apply_board_card_item_widgets()
            self._initializing = False

        self._update_live_sync_cursor()
        if log_message:
            print(f"[INFO] Tree data loaded from {backend_name}:{source}")

    def _update_live_sync_cursor(self) -> None:
        stream_cursor = self._persistence_service.load_last_stream_cursor()
        if isinstance(stream_cursor, dict):
            self._live_sync_cursor = dict(stream_cursor)
            self._record_live_sync_cursor(stream_cursor)

    @Slot(object)
    def _handle_widget_destroyed(self, _obj: object | None = None) -> None:
        self._set_live_sync_diagnostic(connection_state="stopped", backoff_seconds=0.0)
        self._stop_live_sync_transport()

    def _stop_live_sync_transport(self) -> None:
        if isinstance(self._live_sync_timer, QTimer):
            self._live_sync_timer.stop()
            self._live_sync_timer.deleteLater()
            self._live_sync_timer = None
        if isinstance(self._push_update_timer, QTimer):
            self._push_update_timer.stop()
            self._push_update_timer.deleteLater()
            self._push_update_timer = None
        self._push_update_pending = None
        self._push_update_apply_in_flight = False
        stop_event = self._live_stream_stop_event
        if isinstance(stop_event, threading.Event):
            stop_event.set()

    def _start_live_sync_transport(self) -> None:
        if not self._persistence_service.live_sync_enabled():
            self._set_live_sync_diagnostic(
                enabled=False,
                auto_sync_enabled=False,
                transport="disabled",
                connection_state="disabled",
            )
            return

        auto_sync_enabled = self._auto_sync_runtime_enabled()
        if not auto_sync_enabled:
            self._stop_live_sync_transport()
            self._set_live_sync_diagnostic(
                enabled=True,
                auto_sync_enabled=False,
                push_enabled=False,
                transport="manual",
                connection_state="manual_waiting",
                reconnect_attempts=0,
                backoff_seconds=0.0,
            )
            return

        push_enabled = bool(self._persistence_service.push_stream_enabled()) and self._push_stream_runtime_enabled()
        self._set_live_sync_diagnostic(enabled=True, auto_sync_enabled=True, push_enabled=push_enabled)
        if push_enabled and self._start_live_push_stream():
            return
        self._start_live_sync_timer()

    def _start_live_push_stream(self) -> bool:
        if not self._persistence_service.supports_push_stream():
            self._set_live_sync_diagnostic(push_supported=False)
            return False

        self._stop_live_sync_transport()
        self._set_live_sync_diagnostic(
            push_supported=True,
            transport="push",
            connection_state="connecting",
            reconnect_attempts=0,
            backoff_seconds=0.0,
        )
        self._live_stream_bridge = _TreePushStreamBridge(self)
        self._live_stream_bridge.update_received.connect(self._handle_push_stream_update, Qt.QueuedConnection)
        self._live_stream_bridge.stream_error.connect(self._handle_push_stream_error, Qt.QueuedConnection)
        stop_event = threading.Event()
        self._live_stream_stop_event = stop_event

        def worker() -> None:
            last_cursor = dict(self._live_sync_cursor) if isinstance(self._live_sync_cursor, dict) else None
            reconnect_attempts = 0
            while not stop_event.is_set():
                delivered_update = False
                self._set_live_sync_diagnostic(
                    transport="push",
                    connection_state="connecting" if reconnect_attempts == 0 else "reconnecting",
                    reconnect_attempts=reconnect_attempts,
                    backoff_seconds=0.0,
                )
                try:
                    for loaded_data, stream_cursor in self._persistence_service.stream_live_updates(
                        previous_cursor=last_cursor,
                        stop_event=stop_event,
                        status_callback=self._handle_push_stream_status_payload,
                    ):
                        if stop_event.is_set():
                            break
                        delivered_update = True
                        reconnect_attempts = 0
                        if isinstance(stream_cursor, dict):
                            last_cursor = dict(stream_cursor)
                        self._set_live_sync_diagnostic(
                            transport="push",
                            connection_state="connected",
                            reconnect_attempts=0,
                            backoff_seconds=0.0,
                        )
                        if self._live_stream_bridge is not None:
                            self._live_stream_bridge.update_received.emit(loaded_data, stream_cursor)
                    if stop_event.is_set():
                        break
                    if not delivered_update:
                        reconnect_attempts += 1
                        backoff_seconds = self._compute_push_stream_backoff_seconds(reconnect_attempts)
                        self._record_live_sync_failure(
                            "tree_push_stream_closed",
                            reconnect_attempts=reconnect_attempts,
                            backoff_seconds=backoff_seconds,
                        )
                        if self._live_stream_bridge is not None:
                            self._live_stream_bridge.stream_error.emit(
                                f"tree_push_stream_closed; retry in {backoff_seconds:.1f}s"
                            )
                        stop_event.wait(backoff_seconds)
                except Exception as exc:
                    if stop_event.is_set():
                        break
                    reconnect_attempts += 1
                    backoff_seconds = self._compute_push_stream_backoff_seconds(reconnect_attempts)
                    self._record_live_sync_failure(
                        str(exc),
                        reconnect_attempts=reconnect_attempts,
                        backoff_seconds=backoff_seconds,
                    )
                    if self._live_stream_bridge is not None:
                        self._live_stream_bridge.stream_error.emit(
                            f"{exc}; retry in {backoff_seconds:.1f}s"
                        )
                    stop_event.wait(backoff_seconds)

        self._live_stream_thread = threading.Thread(
            target=worker,
            name="alde-tree-push-stream",
            daemon=True,
        )
        self._live_stream_thread.start()
        return True

    def _start_live_sync_timer(self) -> None:
        if not self._persistence_service.live_sync_enabled():
            return
        if isinstance(self._live_sync_timer, QTimer):
            self._live_sync_timer.stop()
            self._live_sync_timer.deleteLater()
        self._set_live_sync_diagnostic(
            transport="poll",
            connection_state="polling",
            backoff_seconds=0.0,
            reconnect_attempts=0,
            push_supported=bool(self._persistence_service.supports_push_stream()),
        )
        self._live_sync_timer = QTimer(self)
        self._live_sync_timer.setObjectName("JsonTreeWidgetLiveSyncPollTimer")
        self._live_sync_timer.setInterval(self._persistence_service.live_sync_interval_ms())
        self._live_sync_timer.timeout.connect(self._poll_live_tree_updates)
        self._live_sync_timer.start()

    def _live_sync_log_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_TREE_LIVE_SYNC_LOG", "1") or "1").strip().lower()
        return value not in {"0", "false", "no", "off", "quiet"}

    def _live_sync_log_interval_seconds(self) -> float:
        raw_value = str(os.getenv("AI_IDE_TREE_LIVE_SYNC_LOG_INTERVAL_SECONDS", "5.0") or "5.0").strip()
        try:
            return max(0.0, float(raw_value))
        except Exception:
            return 5.0

    def _emit_live_sync_log(self, *, source_label: str, source: str, payload_hash: str) -> None:
        if not self._live_sync_log_enabled():
            return
        log_key = f"{source_label}:{source}:{payload_hash}"
        current_time = time.monotonic()
        interval_seconds = self._live_sync_log_interval_seconds()
        if log_key == self._last_live_sync_log_key and current_time - self._last_live_sync_log_at < interval_seconds:
            return
        self._last_live_sync_log_key = log_key
        self._last_live_sync_log_at = current_time
        print(f"[INFO] Tree data live-synced from {source_label}:{source}")

    def _consume_live_tree_update(
        self,
        loaded_data: Any,
        stream_cursor: Any,
        *,
        backend_name: str,
        source_label: str,
        source: str,
    ) -> None:
        if isinstance(stream_cursor, dict):
            self._live_sync_cursor = dict(stream_cursor)
            self._record_live_sync_cursor(stream_cursor, update_received_at=True)
        if not isinstance(loaded_data, dict):
            return

        is_push_source = str(source_label or "").strip().lower().endswith("_push") or source_label == "agents_db_push"

        self._set_live_sync_diagnostic(
            transport="push" if is_push_source else "poll",
            connection_state="connected" if is_push_source else "polling",
            reconnect_attempts=0,
            backoff_seconds=0.0,
        )

        normalized_loaded_data = self._persistence_service._filter_tree_sections(loaded_data)
        payload_hash = hashlib.sha256(
            json.dumps(normalized_loaded_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if payload_hash == self._last_saved_hash:
            return

        self._apply_loaded_tree_data(
            normalized_loaded_data,
            backend_name=backend_name,
            source=source,
            log_message=False,
        )
        self._emit_live_sync_log(
            source_label=source_label,
            source=source,
            payload_hash=payload_hash,
        )

    @Slot()
    def _drain_push_stream_update(self) -> None:
        if self._push_update_apply_in_flight:
            timer = self._ensure_push_update_timer()
            if not timer.isActive():
                timer.start(self._push_stream_coalesce_interval_ms())
            return

        pending_payload = self._push_update_pending
        self._push_update_pending = None
        if pending_payload is None:
            return

        self._push_update_apply_in_flight = True
        try:
            loaded_data, stream_cursor = pending_payload
            repository_push_enabled = self._persistence_service.uses_repository_projection()
            self._consume_live_tree_update(
                loaded_data,
                stream_cursor,
                backend_name="agents_db_repository_push" if repository_push_enabled else "agents_db_push",
                source_label="agents_db_repository_push" if repository_push_enabled else "agents_db_push",
                source=self._persistence_service.live_sync_source(),
            )
        finally:
            self._push_update_apply_in_flight = False

        if self._push_update_pending is not None:
            timer = self._ensure_push_update_timer()
            if not timer.isActive():
                timer.start(self._push_stream_coalesce_interval_ms())

    @Slot(object, object)
    def _handle_push_stream_update(self, loaded_data: Any, stream_cursor: Any) -> None:
        self._schedule_push_stream_update(loaded_data, stream_cursor)

    def _handle_push_stream_status_payload(self, status_payload: dict[str, Any]) -> None:
        if not isinstance(status_payload, dict):
            return
        if not bool(status_payload.get("subscribed")) and not bool(status_payload.get("heartbeat")):
            return
        self._set_live_sync_diagnostic(
            transport="push",
            connection_state="connected",
            reconnect_attempts=0,
            backoff_seconds=0.0,
        )

    @Slot(str)
    def _handle_push_stream_error(self, error_text: str) -> None:
        print(f"[WARNING] Tree push stream reconnecting after error: {error_text}")

    @Slot()
    def _poll_live_tree_updates(self) -> None:
        if self._initializing or self._live_sync_poll_in_flight:
            return
        if not self._persistence_service.live_sync_enabled():
            return

        self._live_sync_poll_in_flight = True
        try:
            loaded_data, stream_cursor = self._persistence_service.load_live_update(previous_cursor=self._live_sync_cursor)
            self._consume_live_tree_update(
                loaded_data,
                stream_cursor,
                backend_name=self._persistence_service.live_sync_backend_name(),
                source_label=self._persistence_service.live_sync_source_label(),
                source=self._persistence_service.live_sync_source(),
            )
        except Exception as exc:
            print(f"[WARNING] Could not live-sync tree data: {exc}")
        finally:
            self._live_sync_poll_in_flight = False

    def run_manual_sync(self, *, source_label: str = "manual_sync") -> bool:
        normalized_source = str(source_label or "manual_sync").strip() or "manual_sync"
        self._set_live_sync_diagnostic(
            auto_sync_enabled=self._auto_sync_runtime_enabled(),
            push_enabled=False,
            transport="manual",
            connection_state="syncing",
            reconnect_attempts=0,
            backoff_seconds=0.0,
        )
        try:
            self._reload_tree_from_persistence(log_message=False)
            self._set_live_sync_diagnostic(
                transport="manual",
                connection_state="manual",
                reconnect_attempts=0,
                backoff_seconds=0.0,
                last_error="",
                last_error_at="",
                last_update_at=self._live_sync_now_iso(),
            )
            payload_hash = str(self._last_saved_hash or "").strip()
            if payload_hash:
                self._emit_live_sync_log(
                    source_label=normalized_source,
                    source=self._persistence_service.live_sync_source(),
                    payload_hash=payload_hash,
                )
            return True
        except Exception as exc:
            with self._live_sync_diagnostic_lock:
                error_count = int(self._live_sync_diagnostic.get("error_count") or 0) + 1
                self._live_sync_diagnostic.update(
                    {
                        "auto_sync_enabled": self._auto_sync_runtime_enabled(),
                        "push_enabled": False,
                        "transport": "manual",
                        "connection_state": "error",
                        "reconnect_attempts": 0,
                        "backoff_seconds": 0.0,
                        "error_count": error_count,
                        "last_error": str(exc),
                        "last_error_at": self._live_sync_now_iso(),
                    }
                )
            print(f"[WARNING] Could not manually sync tree data: {exc}")
            return False

    def _should_lazy_load_children(self, section_name: str | None, value: Any) -> bool:
        section_upper = (section_name or "").upper()
        if not isinstance(value, (dict, list, tuple)) or not value:
            return False
        if section_upper in self._HISTORY_SECTION_NAMES:
            return True

        raw_threshold = str(os.getenv("AI_IDE_TREE_LAZY_CHILDREN_THRESHOLD", "24") or "24").strip()
        try:
            lazy_threshold = int(raw_threshold)
        except Exception:
            lazy_threshold = 24
        lazy_threshold = max(4, min(lazy_threshold, 500))

        child_count = len(value)
        if section_upper in {"DATABASES", "PROJECTS", "RUNTIME", "ENV", "MCP"} and child_count >= lazy_threshold:
            return True
        return False

    def _add_lazy_placeholder(self, item: QTreeWidgetItem, value: Any, section_name: str | None) -> None:
        placeholder = QTreeWidgetItem(["..."])
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemIsEditable)
        item.addChild(placeholder)
        self._lazy_children[item] = (value, section_name)

    @Slot(QTreeWidgetItem)
    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        self._sync_linked_root_section_expansion(item, expanded=True)
        lazy_payload = self._lazy_children.pop(item, None)
        if lazy_payload is None:
            self._apply_board_card_item_widgets()
            return

        value, section_name = lazy_payload
        item.takeChildren()
        parent_item_key = self._extract_item_key_from_text(item.text(0))

        if isinstance(value, dict):
            for key, child_value in value.items():
                item.addChild(
                    self._build_item(
                        key,
                        child_value,
                        section_name=section_name,
                        parent_key=parent_item_key,
                    )
                )
        elif isinstance(value, (list, tuple)):
            for index, child_value in enumerate(value):
                item.addChild(
                    self._build_item(
                        index,
                        child_value,
                        section_name=section_name,
                        parent_key=parent_item_key,
                    )
                )

        self._remember_item_texts_recursive(item)
        self._apply_board_card_item_widgets()

    @Slot(QTreeWidgetItem)
    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        self._sync_linked_root_section_expansion(item, expanded=False)
        self._apply_board_card_item_widgets()

    def _sync_linked_root_section_expansion(self, item: QTreeWidgetItem, *, expanded: bool) -> None:
        if self._linked_root_expand_sync_in_flight:
            return
        section_name = self._resolve_section_name_for_item(item)
        if section_name not in {"ENV", "MCP"}:
            return

        linked_section_name = "MCP" if section_name == "ENV" else "ENV"
        linked_item = self._root_sections.get(linked_section_name)
        if linked_item is None or linked_item is item or bool(linked_item.isExpanded()) == bool(expanded):
            return

        self._linked_root_expand_sync_in_flight = True
        try:
            linked_item.setExpanded(bool(expanded))
        finally:
            self._linked_root_expand_sync_in_flight = False

    def _remember_tree_texts(self) -> None:
        for section in self._root_sections.values():
            if section is None:
                continue
            self._remember_item_texts_recursive(section)

    def _remember_item_texts_recursive(self, item: QTreeWidgetItem) -> None:
        self._item_last_text[item] = item.text(0).strip()
        for i in range(item.childCount()):
            child = item.child(i)
            if child is not None:
                self._remember_item_texts_recursive(child)

    def _item_depth(self, item: QTreeWidgetItem) -> int:
        """Depth below a section header.

        Section header children => depth 1.
        """
        depth = 0
        cur = item
        while cur is not None:
            parent = cur.parent()
            if parent is None:
                break
            depth += 1
            if parent in self._root_sections.values():
                break
            cur = parent
        return max(depth, 0)

    @staticmethod
    def _root_section_icon_name(section_name: str) -> str | None:
        return {
            "PROJECTS": "deployed_code.svg",
            "RUNTIME": "deployed_code.svg",
            "ENV": "variable_add_26dp_E3E3E3_FILL0_wght600_GRAD0_opsz24.svg",
            "MCP": "network_node_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg",
            "TEMPLATES": "schema_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg",
            "DATABASES": "database_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg",
            "CHAT_HISTORY": "load_content.svg",
            "DOCUMENTS": "open_file.svg",
            "RUNTIME_VIEWS": "expansion_panels.svg",
            "DISPATCHER_DB": "database_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg",
            "GENERATED_DATA": "file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg",
            "HISTORY": "load_content.svg",
        }.get(str(section_name or "").strip().upper())

    def _item_base_icon_name(self, item: QTreeWidgetItem) -> str:
        kind = self._item_kind.get(item, "")
        depth = self._item_depth(item)
        section_name = (self._resolve_section_name_for_item(item) or "").upper()
        item_key = self._extract_item_key_from_text(item.text(0)).strip().lower()

        if section_name == "ENV" and item_key == "mcp":
            return "network_node_24dp_E3E3E3_FILL0_wght400_GRAD0_opsz24.svg"

        # Special-case: items directly under the HISTORY section should use a history icon.
        try:
            parent = item.parent()
            history_roots = {self._root_sections.get(name) for name in self._HISTORY_SECTION_NAMES}
            if parent is not None and parent in history_roots:
                return "load_content.svg"
        except Exception:
            pass

        # Containers
        if kind == "dict":
            return "explorer.svg" if depth <= 1 else "expansion_panels.svg"
        if kind == "list":
            return "compare_arrows_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg"

        # Leaves (value types)
        if kind == "bool":
            return "check_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg"
        if kind == "null":
            return "close.svg"
        if kind == "num":
            return "analyse.svg"
        if kind == "str":
            return "html_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg"

        # Fallback
        return "open_file.svg" if depth <= 1 else "menu_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg"

    def _apply_item_icon(self, item: QTreeWidgetItem) -> None:
        # Tree view no longer uses item icons; keep the label-only grouped layout.
        item.setData(0, Qt.DecorationRole, None)

    @staticmethod
    def _format_date_badge(date_str: str) -> str:
        s = (date_str or "").strip()
        if not s:
            return ""

        # Accept compact numeric formats often found in persisted history.
        # Examples:
        # - ddmmyy    => 080120
        # - ddmmyyyy  => 08012026
        # - yyyymmdd  => 20260108
        digits = "".join(ch for ch in s if ch.isdigit())
        if digits:
            # If the value came from an int, leading zeros are lost (e.g. 080120 -> 80120).
            # Pad short sequences back to ddmmyy.
            if len(digits) in (4, 5):
                digits = digits.zfill(6)
            if len(digits) == 6:  # ddmmyy
                dd, mm = digits[:2], digits[2:4]
            elif len(digits) == 8:
                # Prefer yyyymmdd when it looks like a year prefix.
                if digits.startswith(("19", "20")):
                    dd, mm = digits[6:8], digits[4:6]
                else:  # ddmmyyyy
                    dd, mm = digits[:2], digits[2:4]
            elif len(digits) >= 10 and digits.startswith(("19", "20")):
                # e.g. 2026-01-08T... -> take leading yyyymmdd
                dd, mm = digits[6:8], digits[4:6]
            else:
                dd = mm = ""

            month = {
                "01": "Jan",
                "02": "Feb",
                "03": "Mar",
                "04": "Apr",
                "05": "May",
                "06": "Jun",
                "07": "Jul",
                "08": "Aug",
                "09": "Sep",
                "10": "Oct",
                "11": "Nov",
                "12": "Dec",
            }.get(mm, "")
            if dd and month:
                return f"{dd} {month}"

        # common format in this repo: dd.mm.yyyy -> show 'dd Mon'
        if len(s) >= 10 and s[2] == "." and s[5] == ".":
            dd = s[:2]
            mm = s[3:5]
            month = {
                "01": "Jan",
                "02": "Feb",
                "03": "Mar",
                "04": "Apr",
                "05": "May",
                "06": "Jun",
                "07": "Jul",
                "08": "Aug",
                "09": "Sep",
                "10": "Oct",
                "11": "Nov",
                "12": "Dec",
            }.get(mm, "")
            if month:
                return f"{dd} {month}"
            return dd

        # fallback: keep it short
        return s[:6]

    @staticmethod
    def _extract_history_badge(value: Any) -> str:
        """Try to extract a human-readable date badge from a history payload."""
        # expected shapes:
        # - list[dict] with dicts containing 'date'
        # - list[list[dict]] (some versions store sessions)
        # - dict with nested lists

        def _iter_entries(obj: Any):
            if isinstance(obj, list):
                for x in obj:
                    yield x
            elif isinstance(obj, dict):
                # try common containers
                for k in ("history", "messages", "items", "data"):
                    cand = obj.get(k)
                    if isinstance(cand, list):
                        for x in cand:
                            yield x

        last_date: str | None = None
        try:
            # Walk from the end so we pick the latest date.
            if isinstance(value, list):
                candidates = list(value)
            else:
                candidates = list(_iter_entries(value))

            for entry in reversed(candidates):
                if isinstance(entry, dict):
                    d = entry.get("date")
                    if d is not None:
                        ds = str(d).strip()
                        if ds:
                            last_date = ds
                        break
                elif isinstance(entry, list):
                    for sub in reversed(entry):
                        if isinstance(sub, dict):
                            d = sub.get("date")
                            if d is not None:
                                ds = str(d).strip()
                                if ds:
                                    last_date = ds
                                break
                    if last_date:
                        break
        except Exception:
            last_date = None

        if not last_date:
            return ""
        return JsonTreeWidget._format_date_badge(last_date)

    def _refresh_item_icons_recursive(self, parent: QTreeWidgetItem | None = None) -> None:
        if parent is None:
            for i in range(self.topLevelItemCount()):
                top = self.topLevelItem(i)
                if top is not None:
                    self._refresh_item_icons_recursive(top)
            return

        self._apply_item_icon(parent)
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child is not None:
                self._refresh_item_icons_recursive(child)

    def set_accent_color(self, color: QColor | str) -> None:
        if isinstance(color, QColor):
            color_str = color.name(QColor.HexRgb)
        else:
            color_str = str(color).strip()
        if not color_str or color_str == self._accent_color:
            return
        self._accent_color = color_str
        self._apply_stylesheet()
        self._update_root_section_header_styles()
        self._update_root_section_icons()
        self._refresh_item_icons_recursive()

    @staticmethod
    def _qss_rgba(color_value: QColor | str, alpha: int, *, fallback: str) -> str:
        color = QColor(color_value) if isinstance(color_value, QColor) else QColor(str(color_value or ""))
        if not color.isValid():
            return fallback
        alpha_clamped = max(0, min(255, int(alpha)))
        return f"rgba({color.red()},{color.green()},{color.blue()},{alpha_clamped})"
    
    def _apply_stylesheet(self) -> None:
        """Apply the current colors to the QTreeWidget stylesheet."""
        scrollbar_bg_override = (
            "QTreeWidget QScrollBar:horizontal,"
            "QTreeWidget QScrollBar:vertical,"
            "QTreeView QScrollBar:horizontal,"
            "QTreeView QScrollBar:vertical,"
            "QAbstractScrollArea QScrollBar:horizontal,"
            "QAbstractScrollArea QScrollBar:vertical {"
            f"background: {self._bg_color};"
            f"background-color: {self._bg_color};"
            "}"
        )
        self.setStyleSheet(
            self._style_template.format(
                text_color=self._text_color,
                bg_color=self._bg_color,
                branch_color=self._branch_color,
                frame_color=self._frame_color,
                item_frame_color=self._qss_rgba(self._muted_color, 170, fallback="rgba(154,154,149,170)"),
                item_bg_color=self._qss_rgba(self._bg_color, 34, fallback="rgba(11,11,11,34)"),
                item_selected_frame_color=self._qss_rgba(self._accent_color, 214, fallback="rgba(58,95,255,214)"),
                item_selected_bg_color=self._qss_rgba(self._accent_color, 56, fallback="rgba(58,95,255,56)"),
            )
            + SCROLLBAR_HOVER_ONLY_DARK
            + scrollbar_bg_override
        )
        self._apply_board_card_item_widgets()

    def set_text_color(self, color: QColor | str) -> None:
        """Expose text color change so other widgets can match their palette."""
        if isinstance(color, str):
            color = color.strip()
            if not color:
                return
            color_str = color
        elif isinstance(color, QColor):
            color_str = color.name(
                QColor.HexArgb if color.alpha() < 255 else QColor.HexRgb
            )
        else:
            return

        if color_str == self._text_color:
            return

        self._text_color = color_str
        self._apply_stylesheet()
        self._update_root_section_header_styles()
        self._refresh_item_icons_recursive()

    def set_background_color(self, color: QColor | str) -> None:
        """Expose background change so parents can sync with chat prompt area."""
        if isinstance(color, str):
            color = color.strip()
            if not color:
                return
            color_str = color
        elif isinstance(color, QColor):
            color_str = color.name(
                QColor.HexArgb if color.alpha() < 255 else QColor.HexRgb
            )
        else:
            return

        if color_str == self._bg_color:
            return

        self._bg_color = color_str
        self._apply_stylesheet()
    

      
    def set_json(self, data: Any) -> None:
        """Rebuild tree from `data` and collapse to top level."""
        self._data = data  # Store for save logic
        self.clear()
        root_item = self._build_item("root", data, section_name=None, parent_key=None)
        # move children of artificial root to top level
        while root_item.childCount():
            self.addTopLevelItem(root_item.takeChild(0))
        self.expandToDepth(0)
        self._apply_board_card_item_widgets()

    def _build_item(
        self,
        key: str | int,
        value: Any,
        *,
        section_name: str | None = None,
        parent_key: str | None = None,
    ) -> QTreeWidgetItem:
        section_upper = (section_name or "").upper()

        idx: int | None = None
        if isinstance(key, int):
            idx = key
            # HISTORY requested: no brackets around numeric indices.
            key_label = str(key) if section_upper in self._HISTORY_SECTION_NAMES else f"[{key}]"
        else:
            key_label = str(key)
            # Backward-compat: older persisted data may contain bracketed indices as strings.
            # e.g. "[1428]" -> "1428"
            if section_upper in self._HISTORY_SECTION_NAMES:
                k = key_label.strip()
                if k.startswith("[") and k.endswith("]"):
                    inner = k[1:-1].strip()
                    if inner.isdigit():
                        key_label = inner

        is_env_records_row = (
            section_upper == "ENV"
            and isinstance(key, int)
            and str(parent_key or "").strip().lower() == "records"
            and isinstance(value, Mapping)
        )
        if is_env_records_row:
            record_id = ""
            for id_field in ("_id", "id", "entity_id", "canonical_name", "title"):
                candidate_id = str(value.get(id_field) or "").strip()
                if candidate_id:
                    record_id = candidate_id
                    break
            key_label = f"[{int(key) + 1:02d}]"
            if record_id:
                key_label = f"{key_label}: {json.dumps(record_id, ensure_ascii=False)}"

        if isinstance(value, dict):
            if section_upper in self._HISTORY_SECTION_NAMES:
                # Requested: remove brackets/dots/braces. Keep it compact.
                d = value.get("date")
                badge = self._format_date_badge(str(d)) if d is not None else ""
                role = value.get("role")
                role_str = str(role).strip() if isinstance(role, str) else ""
                parts = [key_label]
                if badge:
                    parts.append(badge)
                if role_str:
                    parts.append(role_str)
                label = " ".join(p for p in parts if p)
            else:
                label = key_label if is_env_records_row else f"{key_label} {{...}}"

            item = QTreeWidgetItem([label])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self._item_kind[item] = "dict"
            if self._should_lazy_load_children(section_name, value):
                self._add_lazy_placeholder(item, value, section_name)
            else:
                for k, v in value.items():
                    item.addChild(
                        self._build_item(
                            k,
                            v,
                            section_name=section_name,
                            parent_key=str(key),
                        )
                    )
        elif isinstance(value, (list, tuple)):
            item = QTreeWidgetItem([f"{key_label} [{len(value)}]"])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self._item_kind[item] = "list"
            if self._should_lazy_load_children(section_name, value):
                self._add_lazy_placeholder(item, value, section_name)
            else:
                for i, v in enumerate(value):
                    item.addChild(
                        self._build_item(
                            i,
                            v,
                            section_name=section_name,
                            parent_key=str(key),
                        )
                    )
        else:
            text = json.dumps(value, ensure_ascii=False)
            item = QTreeWidgetItem([f"{key_label}: {text}"])
            item.setFlags(item.flags() | Qt.ItemIsEditable)

            if value is None:
                self._item_kind[item] = "null"
            elif isinstance(value, bool):
                self._item_kind[item] = "bool"
            elif isinstance(value, (int, float)):
                self._item_kind[item] = "num"
            elif isinstance(value, str):
                self._item_kind[item] = "str"
            else:
                self._item_kind[item] = "other"

        if section_upper in self._SMALL_FONT_SECTION_NAMES:
            f = item.font(0)
            f.setPointSize(self._section_item_font_size_small)
            item.setFont(0, f)

        self._apply_item_icon(item)
        return item

    @staticmethod
    def _extract_item_key_from_text(text: str) -> str:
        if ": " in text:
            return text.split(": ", 1)[0].strip()
        if text.endswith(" {...}"):
            return text[:-5].strip()
        if text.endswith(" [") and text.rsplit(" ", 1)[-1].endswith("]"):
            return text.rsplit(" ", 1)[0].strip()
        if " [" in text and text.endswith("]"):
            return text.rsplit(" ", 1)[0].strip()
        return text.strip()

    def _resolve_section_name_for_item(self, item: QTreeWidgetItem | None) -> str | None:
        if item is None:
            return None
        section_name = self._item_to_section.get(item)
        if section_name is not None:
            return section_name
        parent = item.parent()
        while parent is not None:
            for candidate_section_name, section_item in self._root_sections.items():
                if section_item == parent:
                    return candidate_section_name
            parent = parent.parent()
        return None

    def _item_path_segments(self, item: QTreeWidgetItem, column: int = 0, *, section_name: str | None = None) -> list[str]:
        resolved_section_name = section_name or self._resolve_section_name_for_item(item)
        if resolved_section_name is None:
            return []

        path_segments: list[str] = []
        current = item
        section_root = self._root_sections.get(resolved_section_name)
        while current is not None and current != section_root:
            path_segments.append(self._extract_item_key_from_text(current.text(column)))
            current = current.parent()
        path_segments.reverse()
        return path_segments

    def _reload_tree_from_persistence(self, *, log_message: bool = False) -> None:
        loaded_data, backend_name, source = self._persistence_service.load_data()
        if isinstance(loaded_data, dict):
            self._apply_loaded_tree_data(
                loaded_data,
                backend_name=backend_name,
                source=source,
                log_message=log_message,
            )

    def _is_agentsdb_repository_root_item(self, *, section_name: str | None, item_key: str | None) -> bool:
        return (
            str(section_name or "").strip().upper() == self._persistence_service._AGENTSDB_REPOSITORY_SECTION_NAME
            and str(item_key or "").strip() == self._persistence_service._AGENTSDB_REPOSITORY_SECTION_KEY
        )

    @Slot(QTreeWidgetItem, int)
    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle item edits by syncing changes back into the stored data."""
        # Block recursion
        if self.signalsBlocked():
            return

        # Ignore startup / programmatic updates that don't represent a real text edit.
        if self._initializing:
            return
            
        new_text = item.text(column).strip()
        if not new_text:
            return

        last_text = self._item_last_text.get(item)
        if last_text is not None and last_text == new_text:
            return

        original_item_key = ""
        if isinstance(last_text, str) and last_text.strip():
            original_item_key = self._extract_item_key_from_text(last_text)

        # Update early so repeated non-text itemChanged events don't re-enter.
        self._item_last_text[item] = new_text
        
        # Check if this item belongs to a section
        section_name = self._resolve_section_name_for_item(item)
        
        if section_name is None or section_name not in self._data:
            return

        def coerce_key(segment: str, container: Any) -> Any:
            return self._persistence_service._coerce_tree_path_segment(segment, container)

        def parse_value(text: str) -> Any:
            try:
                return json.loads(text)
            except Exception:
                return text

        # Build the path from root to the edited item (skip section root)
        path_segments = self._item_path_segments(item, column, section_name=section_name)
        if path_segments and original_item_key:
            path_segments[-1] = original_item_key

        item_kind = str(self._item_kind.get(item, "")).strip().lower()
        is_scalar_item = item_kind not in {"dict", "list"}

        if not path_segments:
            return

        repository_binding = self._persistence_service.resolve_agentsdb_repository_binding(
            section_name=section_name,
            path_segments=path_segments,
        )

        if repository_binding is not None and ": " not in new_text and not is_scalar_item:
            self._reload_tree_from_persistence(log_message=False)
            return

        # Walk the stored data to reach the parent container of the edited key
        # Start from the section data
        parent_container: Any = self._data[section_name]
        for segment in path_segments[:-1]:
            key_obj = coerce_key(segment, parent_container)
            try:
                parent_container = parent_container[key_obj]
            except (KeyError, IndexError, TypeError):
                return

        last_segment = path_segments[-1]
        key_obj = coerce_key(last_segment, parent_container)

        if ": " in new_text:
            key_part, value_part = new_text.split(": ", 1)
            key_part = key_part.strip()
            value_part = value_part.strip()
        elif is_scalar_item:
            key_part = str(last_segment).strip() or original_item_key
            value_part = new_text.strip()
        else:
            return

        parsed_value = parse_value(value_part)

        if isinstance(parent_container, dict):
            # Handle key rename if needed
            if key_part != last_segment:
                try:
                    parent_container[key_part] = parent_container.pop(key_obj)
                except KeyError:
                    parent_container[key_part] = parsed_value
                key_obj = key_part
                last_segment = key_part
                self._item_to_key[item] = str(key_part)
            parent_container[key_obj] = parsed_value
        elif isinstance(parent_container, list):
            if not isinstance(key_obj, int) or not (0 <= key_obj < len(parent_container)):
                return
            parent_container[key_obj] = parsed_value
            key_part = last_segment  # keep list index label
        else:
            return

        canonical_text = f"{key_part}: {json.dumps(parsed_value, ensure_ascii=False)}"
        if canonical_text != item.text(column):
            self.blockSignals(True)
            item.setText(column, canonical_text)
            self.blockSignals(False)
            self._item_last_text[item] = canonical_text.strip()

        if repository_binding is not None:
            synced = False
            try:
                synced = self._persistence_service.apply_agentsdb_repository_edit(
                    section_name=section_name,
                    path_segments=path_segments,
                    key_name=str(key_part),
                    value=parsed_value,
                )
            except Exception as sync_exc:
                print(f"[WARNING] Could not sync AgentDB repository edit: {sync_exc}")
            if not synced:
                self._reload_tree_from_persistence(log_message=False)
                return
            payload = json.dumps(self._data, ensure_ascii=False, sort_keys=True)
            self._last_saved_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            return
        
        # Persist changes to disk after successful edit
        self._save_data(
            change_event={
                "action": "edit",
                "origin": "tree_widget",
                "section_name": section_name,
                "path": [str(segment) for segment in path_segments],
                "key": str(key_part),
            }
        )
    
    def _initialize_root_sections(self) -> None:
        """Initialize default root sections like VS Code Explorer."""
        allowed_sections = self._persistence_service._tree_section_allowlist()
        for section_name, collapsed in self._resolved_root_section_layout():
            if section_name not in allowed_sections:
                continue
            self._add_root_section(section_name, collapsed=collapsed)
            self._data[section_name] = {}
        
        # Load previously saved data if available.
        # This can be expensive when repository projection is active, so keep
        # startup responsive by loading asynchronously unless explicitly disabled.
        if not self._initial_load_async_enabled:
            self._load_data()

        # Ensure root headers match current theme settings.
        self._update_root_section_header_styles()
        self._update_section_item_font_sizes()
        self._apply_board_card_item_widgets()

    def _resolved_root_section_layout(self) -> tuple[tuple[str, bool], ...]:
        collapse_defaults = {
            str(section_name).strip().upper(): bool(collapsed)
            for section_name, collapsed in self._DEFAULT_ROOT_SECTION_LAYOUT
            if str(section_name).strip()
        }
        section_order = self._persistence_service._resolved_ai_ide_section_name_order()
        return tuple((section_name, collapse_defaults.get(section_name, True)) for section_name in section_order)

    def _update_section_item_font_sizes(self) -> None:
        """Apply per-section font sizing to already-built items."""

        def apply_recursive(parent: QTreeWidgetItem, point_size: int) -> None:
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child is None:
                    continue
                f = child.font(0)
                f.setPointSize(point_size)
                child.setFont(0, f)
                apply_recursive(child, point_size)

        for section_name in sorted(self._SMALL_FONT_SECTION_NAMES):
            root = self._root_sections.get(section_name)
            if root is not None:
                apply_recursive(root, self._section_item_font_size_small)

    def _update_root_section_header_styles(self) -> None:
        for section in self._root_sections.values():
            if section is None:
                continue
            font = section.font(0)
            font.setBold(True)
            font.setPointSize(self._section_header_font_size)
            section.setFont(0, font)
            section.setForeground(0, QColor(self._accent_color))

    @staticmethod
    def _board_card_normalize_label(raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return ""
        for prefix in (DROPDOWN_EXPANDED_PREFIX, DROPDOWN_COLLAPSED_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if text.endswith(" {...}"):
            text = text[:-5].strip()
        if " [" in text and text.endswith("]"):
            text = text.rsplit(" ", 1)[0].strip()
        return text.strip()

    @staticmethod
    def _board_card_keyword_group(title_key: str) -> str | None:
        text = str(title_key or "").strip().lower()
        if not text:
            return None

        keyword_groups: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("alerts", ("error", "warn", "warning", "failed", "failure", "timeout", "retry", "critical", "panic")),
            ("security", ("token", "secret", "credential", "password", "apikey", "api_key", "auth", "private key")),
            ("integration", ("mcp", "server", "socket", "endpoint", "http", "https", "api", "tool", "bridge")),
            ("data", ("database", "db", "collection", "record", "vector", "index", "table", "document")),
            ("runtime", ("runtime", "worker", "queue", "job", "dispatcher", "session", "event", "stream")),
            ("workspace", ("project", "workspace", "repo", "file", "path", "module", "template")),
        )
        for category, keywords in keyword_groups:
            if any(keyword in text for keyword in keywords):
                return category
        return None

    @staticmethod
    def _board_card_env_mcp_group(section_name: str, title_key: str) -> str | None:
        normalized_section = str(section_name or "").strip().upper()
        text = str(title_key or "").strip().lower()
        if not normalized_section:
            return None

        if normalized_section == "ENV":
            if any(token in text for token in ("error", "warn", "failed", "timeout", "invalid")):
                return "alerts"
            if any(token in text for token in ("token", "secret", "password", "apikey", "api_key", "auth", "private", "credential")):
                return "security"
            if any(token in text for token in ("url", "uri", "endpoint", "host", "port", "socket")):
                return "integration"
            if any(token in text for token in ("path", "dir", "directory", "file", "workspace", "repo", "home")):
                return "workspace"
            if any(token in text for token in ("runtime", "worker", "queue", "model", "provider", "thread", "cache")):
                return "runtime"
            return "neutral"

        if normalized_section == "MCP":
            if any(token in text for token in ("error", "warn", "failed", "timeout", "retry", "unavailable")):
                return "alerts"
            if any(token in text for token in ("token", "secret", "apikey", "api_key", "auth", "credential", "key")):
                return "security"
            if any(token in text for token in ("server", "endpoint", "host", "port", "url", "uri", "socket", "transport", "bridge")):
                return "integration"
            if any(token in text for token in ("tool", "runtime", "worker", "event", "stream", "sync")):
                return "runtime"
            if any(token in text for token in ("path", "file", "workspace", "repo", "module")):
                return "workspace"
            return "integration"

        return None

    def _board_card_category(self, section_title: str, *, item: QTreeWidgetItem | None = None) -> str:
        title_key = str(section_title or "").strip().lower()
        if not title_key and isinstance(item, QTreeWidgetItem):
            title_key = str(item.text(0) or "").strip().lower()

        section_name = str(self._resolve_section_name_for_item(item) or "").strip().upper()
        if not section_name and isinstance(item, QTreeWidgetItem) and item.parent() is None:
            section_name = str(self._board_card_normalize_label(item.text(0)) or "").strip().upper()
        section_palette: dict[str, str] = {
            "PROJECTS": "workspace",
            "RUNTIME": "runtime",
            "DATABASES": "data",
            "CHAT_HISTORY": "history",
            "HISTORY": "history",
        }
        if section_name in {"ENV", "MCP"}:
            env_mcp_group = self._board_card_env_mcp_group(section_name, title_key)
            if env_mcp_group:
                return env_mcp_group
        if section_name in section_palette:
            return section_palette[section_name]

        item_kind = ""
        if isinstance(item, QTreeWidgetItem):
            item_kind = str(self._item_kind.get(item, "")).strip().lower()
        if item_kind in {"dict", "list"}:
            return "container"

        keyword_group = self._board_card_keyword_group(title_key)
        if keyword_group:
            return keyword_group

        if "/" in title_key or "\\" in title_key:
            return "workspace"
        if title_key.endswith((".py", ".json", ".md", ".toml", ".yaml", ".yml", ".txt")):
            return "workspace"
        if "history" in title_key or "chat" in title_key:
            return "history"
        return "neutral"

    @staticmethod
    def _board_card_palette(category: str) -> dict[str, str]:
        palettes: dict[str, dict[str, str]] = {
            "workspace": {
                "label_fg": "#d8ecff",
                "label_bg": "rgba(66, 120, 168, 0.24)",
                "label_border": "rgba(66, 120, 168, 0.62)",
            },
            "runtime": {
                "label_fg": "#d6ffe1",
                "label_bg": "rgba(56, 150, 99, 0.24)",
                "label_border": "rgba(56, 150, 99, 0.64)",
            },
            "data": {
                "label_fg": "#ffe6cf",
                "label_bg": "rgba(178, 118, 68, 0.24)",
                "label_border": "rgba(178, 118, 68, 0.62)",
            },
            "integration": {
                "label_fg": "#d0f5ff",
                "label_bg": "rgba(47, 149, 168, 0.24)",
                "label_border": "rgba(47, 149, 168, 0.60)",
            },
            "history": {
                "label_fg": "#f0dfff",
                "label_bg": "rgba(120, 102, 181, 0.24)",
                "label_border": "rgba(120, 102, 181, 0.60)",
            },
            "security": {
                "label_fg": "#ffe8e6",
                "label_bg": "rgba(178, 48, 68, 0.34)",
                "label_border": "rgba(227, 96, 116, 0.88)",
            },
            "alerts": {
                "label_fg": "#fff3d8",
                "label_bg": "rgba(212, 128, 18, 0.36)",
                "label_border": "rgba(255, 175, 64, 0.86)",
            },
            "container": {
                "label_fg": "#dbe4e7",
                "label_bg": "rgba(105, 120, 127, 0.20)",
                "label_border": "rgba(124, 141, 149, 0.54)",
            },
            "neutral": {
                "label_fg": "#dce3e7",
                "label_bg": "rgba(84, 96, 104, 0.20)",
                "label_border": "rgba(113, 127, 136, 0.52)",
            },
        }
        return palettes.get(str(category or "").strip().lower(), palettes["neutral"])

    @staticmethod
    def _board_card_group_top_margin(current_category: str, previous_category: str | None) -> int:
        normalized_current = str(current_category or "").strip().lower()
        normalized_previous = str(previous_category or "").strip().lower()
        if not normalized_current or not normalized_previous:
            return 0
        if normalized_current != normalized_previous:
            return 6
        return 0

    def _apply_board_card_label_style(
        self,
        label_widget: QLabel,
        category: str,
        *,
        font_size_px: int,
    ) -> None:
        palette = self._board_card_palette(category)
        label_widget.setStyleSheet(
            (
                f"color: {palette['label_fg']};"
                "font-weight: 700;"
                f"font-size: {max(9, int(font_size_px))}px;"
                f"background: {palette['label_bg']};"
                f"border: 1px solid {palette['label_border']};"
                "border-radius: 6px;"
                "padding: 2px 9px 3px 9px;"
            )
        )

    def _apply_board_card_marker_style(self, marker_widget: QLabel, marker_text: str) -> None:
        glyph = str(marker_text or "").strip()
        if glyph == DROPDOWN_EXPANDED_GLYPH:
            marker_color = self._accent_color
        elif glyph == DROPDOWN_COLLAPSED_GLYPH:
            marker_color = self._muted_color
        else:
            marker_color = "transparent"
        marker_widget.setStyleSheet(
            (
                f"color: {marker_color};"
                "font-weight: 700;"
                "font-size: 14px;"
                "background: transparent;"
                "border: none;"
                "padding: 0px;"
            )
        )

    def _remove_board_card_item_widget(self, item: QTreeWidgetItem) -> None:
        existing_widget = self.itemWidget(item, 0)
        if isinstance(existing_widget, QWidget) and bool(existing_widget.property("board_card_widget")):
            self.removeItemWidget(item, 0)
            existing_widget.deleteLater()
        item.setSizeHint(0, QSize())

    def _iter_item_subtree(self, parent_item: QTreeWidgetItem):
        yield parent_item
        for child_index in range(parent_item.childCount()):
            child_item = parent_item.child(child_index)
            if isinstance(child_item, QTreeWidgetItem):
                yield from self._iter_item_subtree(child_item)

    def _iter_grouped_child_rows(self, parent_item: QTreeWidgetItem):
        previous_child_category: str | None = None
        for child_index in range(parent_item.childCount()):
            child_item = parent_item.child(child_index)
            if not isinstance(child_item, QTreeWidgetItem):
                continue

            normalized_label = self._board_card_normalize_label(child_item.text(0))
            item_category = self._board_card_category(normalized_label, item=child_item)
            row_margin_top = self._board_card_group_top_margin(item_category, previous_child_category)
            previous_child_category = item_category

            yield child_item, normalized_label, item_category, row_margin_top
            yield from self._iter_grouped_child_rows(child_item)

    @staticmethod
    def _board_card_dropdown_marker(item: QTreeWidgetItem) -> str:
        if item.childCount() <= 0:
            return ""
        return dropdown_prefix(item.isExpanded())

    def _apply_board_card_item_row(
        self,
        item: QTreeWidgetItem,
        *,
        normalized_label: str,
        item_category: str,
        row_margin_top: int,
    ) -> None:
        container_widget = self.itemWidget(item, 0)
        if not isinstance(container_widget, QWidget) or not bool(container_widget.property("board_card_widget")):
            container_widget = QWidget(self)
            container_widget.setProperty("board_card_widget", True)
            container_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            container_layout = QHBoxLayout(container_widget)
            container_layout.setContentsMargins(0, row_margin_top, 0, 0)
            container_layout.setSpacing(0)

            marker_widget = QLabel("", container_widget)
            marker_widget.setObjectName("jsonTreeBoardCardMarker")
            marker_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            marker_widget.setAlignment(Qt.AlignCenter)
            marker_widget.setFixedWidth(13)
            self._apply_board_card_marker_style(marker_widget, "")

            label_widget = QLabel(normalized_label, container_widget)
            label_widget.setObjectName("jsonTreeBoardCardLabel")
            label_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            label_widget.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            label_widget.setWordWrap(False)

            container_layout.addWidget(marker_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
            container_layout.addSpacing(3)
            container_layout.addWidget(label_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
            container_layout.addStretch(1)
            self.setItemWidget(item, 0, container_widget)
        else:
            container_layout = container_widget.layout()
            if isinstance(container_layout, QHBoxLayout):
                container_layout.setContentsMargins(0, row_margin_top, 0, 0)
            marker_widget = container_widget.findChild(QLabel, "jsonTreeBoardCardMarker")
            if not isinstance(marker_widget, QLabel):
                marker_widget = QLabel("", container_widget)
                marker_widget.setObjectName("jsonTreeBoardCardMarker")
                marker_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                marker_widget.setAlignment(Qt.AlignCenter)
                marker_widget.setFixedWidth(13)
                self._apply_board_card_marker_style(marker_widget, "")
                if isinstance(container_layout, QHBoxLayout):
                    container_layout.insertWidget(0, marker_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
                    container_layout.insertSpacing(1, 3)
            label_widget = container_widget.findChild(QLabel, "jsonTreeBoardCardLabel")
            if not isinstance(label_widget, QLabel):
                label_widget = QLabel(normalized_label, container_widget)
                label_widget.setObjectName("jsonTreeBoardCardLabel")
                label_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                label_widget.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                label_widget.setWordWrap(False)
                if isinstance(container_layout, QHBoxLayout):
                    insert_index = max(0, container_layout.count() - 1)
                    container_layout.insertWidget(insert_index, label_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)

        marker = self._board_card_dropdown_marker(item).strip()
        marker_widget.setText(marker)
        self._apply_board_card_marker_style(marker_widget, marker)
        label_widget.setText(normalized_label)
        label_widget.setToolTip(str(item.toolTip(0) or normalized_label))
        self._apply_board_card_label_style(
            label_widget,
            item_category,
            font_size_px=11,
        )
        desired_height = max(23, int(label_widget.sizeHint().height()) + row_margin_top)
        item.setSizeHint(0, QSize(0, desired_height))

    def _apply_board_card_item_widgets(self) -> None:
        previous_top_level_category: str | None = None

        for top_level_index in range(self.topLevelItemCount()):
            top_item = self.topLevelItem(top_level_index)
            if not isinstance(top_item, QTreeWidgetItem):
                continue

            top_label = self._board_card_normalize_label(top_item.text(0))
            top_category = self._board_card_category(top_label, item=top_item)
            top_margin = 0
            top_margin = self._board_card_group_top_margin(top_category, previous_top_level_category)
            previous_top_level_category = top_category

            self._apply_board_card_item_row(
                top_item,
                normalized_label=top_label,
                item_category=top_category,
                row_margin_top=top_margin,
            )

            for child_item, child_label, child_category, child_margin in self._iter_grouped_child_rows(top_item):
                self._apply_board_card_item_row(
                    child_item,
                    normalized_label=child_label,
                    item_category=child_category,
                    row_margin_top=child_margin,
                )
    
    def _add_root_section(self, name: str, collapsed: bool = False) -> QTreeWidgetItem:
        """Add a new root section (like 'PROJECTS' or 'DATABASES' in VS Code)."""
        if name in self._root_sections:
            return self._root_sections[name]
        
        section = QTreeWidgetItem([name.upper()])
        section.setFlags(section.flags() | Qt.ItemIsEditable)
        
        # Style for section headers (bold, slightly different color)
        font = section.font(0)
        font.setBold(True)
        font.setPointSize(self._section_header_font_size)
        section.setFont(0, font)
        section.setForeground(0, QColor(self._accent_color))

        # Root headers stay text-only to keep compact grouped blocks.
        section.setData(0, Qt.DecorationRole, None)
        
        self.addTopLevelItem(section)
        self._root_sections[name] = section
        
        if not collapsed:
            section.setExpanded(True)
        
        return section

    def _update_root_section_icons(self) -> None:
        for section in self._root_sections.values():
            section.setData(0, Qt.DecorationRole, None)
    
    def add_to_section(self, section_name: str, key: str, value: Any, *, persist: bool = True) -> None:
        """Add data to a specific section (e.g., 'PROJECTS', 'DATABASES').

        Set persist=False for derived/ephemeral views (e.g. ChatHistory preview)
        to avoid bloating AppData/tree_data.json.
        """
        normalized_section_name = str(section_name or "").strip().upper()
        if not normalized_section_name:
            return

        allowed_sections = self._persistence_service._tree_section_allowlist()
        if normalized_section_name not in allowed_sections:
            return

        section = self._root_sections.get(normalized_section_name)
        if section is None:
            if self._is_hidden_root_section_name(normalized_section_name):
                section = None
            else:
                section = self._add_root_section(normalized_section_name)
            self._data[normalized_section_name] = {}
        
        # Store the data in our internal structure
        if normalized_section_name not in self._data:
            self._data[normalized_section_name] = {}
        self._data[normalized_section_name][key] = value

        if section is None:
            if persist:
                self._save_data(
                    change_event={
                        "action": "upsert",
                        "origin": "tree_widget",
                        "section_name": normalized_section_name,
                        "item_key": str(key),
                    }
                )
            return
        
        item = self._build_item(key, value, section_name=normalized_section_name)
        section.addChild(item)

        # Remember baseline text for edit detection.
        self._remember_item_texts_recursive(item)

        # Ensure consistent font sizing for sections that use smaller typography.
        if normalized_section_name in self._SMALL_FONT_SECTION_NAMES:
            self._update_section_item_font_sizes()

        if normalized_section_name in self._HISTORY_SECTION_NAMES:
            # Force history semantics for icon selection.
            self._item_kind[item] = "history"
            badge = self._extract_history_badge(value)
            if badge:
                self._item_badge[item] = badge
            self._apply_item_icon(item)
        
        # Track this item's section and key
        self._item_to_section[item] = normalized_section_name
        self._item_to_key[item] = key
        
        section.setExpanded(True)
        self._apply_board_card_item_widgets()
        
        # Save after adding (unless this is a derived/ephemeral view)
        if persist:
            self._save_data(
                change_event={
                    "action": "upsert",
                    "origin": "tree_widget",
                    "section_name": normalized_section_name,
                    "item_key": str(key),
                }
            )
    
    def remove_from_section(self, section_name: str, item_name: str) -> bool:
        """Remove an item from a section by name."""
        normalized_section_name = str(section_name or "").strip().upper()
        normalized_item_name = str(item_name or "").strip()
        if not normalized_section_name or not normalized_item_name:
            return False

        if self._is_agentsdb_repository_root_item(section_name=normalized_section_name, item_key=normalized_item_name):
            return False

        if self._is_hidden_root_section_name(normalized_section_name):
            section_data = self._data.get(normalized_section_name)
            if isinstance(section_data, dict) and normalized_item_name in section_data:
                del section_data[normalized_item_name]
                self._save_data(
                    change_event={
                        "action": "delete",
                        "origin": "tree_widget",
                        "section_name": normalized_section_name,
                        "item_key": normalized_item_name,
                    }
                )
                return True
            return False

        section = self._root_sections.get(normalized_section_name)
        if section is None:
            return False
        
        for i in range(section.childCount()):
            child = section.child(i)
            if child and normalized_item_name in child.text(0):
                section.removeChild(child)
                
                # Remove from data structure
                if normalized_section_name in self._data and normalized_item_name in self._data[normalized_section_name]:
                    del self._data[normalized_section_name][normalized_item_name]
                
                # Remove from tracking dicts
                if child in self._item_to_section:
                    del self._item_to_section[child]
                if child in self._item_to_key:
                    del self._item_to_key[child]

                if child in self._item_last_text:
                    del self._item_last_text[child]

                self._apply_board_card_item_widgets()
                
                self._save_data(
                    change_event={
                        "action": "delete",
                        "origin": "tree_widget",
                        "section_name": normalized_section_name,
                        "item_key": normalized_item_name,
                    }
                )
                return True
        return False
    
    @Slot(bool)
    def _add_project_root(self, checked: bool = False) -> None:
        """Add a new project root to the PROJECTS section."""
        from PySide6.QtWidgets import QInputDialog
        
        name, ok = QInputDialog.getText(
            self, 
            "New Project", 
            "Enter project name:",
            text="New Project"
        )
        
        if ok and name:
            project_data = {
                "name": name,
                "path": "",
                "files": [],
                "settings": {}
            }
            self.add_to_section("PROJECTS", name, project_data)
    
    @Slot(bool)
    def _add_database_root(self, checked: bool = False) -> None:
        """Add a new database connection to the DATABASES section."""
        from PySide6.QtWidgets import QInputDialog
        
        name, ok = QInputDialog.getText(
            self, 
            "New Database Connection", 
            "Enter connection name:",
            text="New Connection"
        )
        
        if ok and name:
            db_data = {
                "name": name,
                "type": "PostgreSQL",
                "host": "localhost",
                "port": 5432,
                "database": "",
                "username": ""
            }
            self.add_to_section("DATABASES", name, db_data)
    
    def _save_data(self, *, change_event: dict[str, Any] | None = None) -> None:
        """Save the current data structure to the configured storage backend."""
        try:
            payload = json.dumps(self._data, ensure_ascii=False, sort_keys=True)
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if self._last_saved_hash == payload_hash:
                return

            try:
                env_path = self._persistence_service.persist_env_projection_from_tree_data(self._data)
                if env_path is not None:
                    print(f"[INFO] ENV projection written to {env_path}")
            except Exception as env_exc:
                print(f"[WARNING] Could not write ENV projection: {env_exc}")

            backend_name, target = self._persistence_service.save_data(self._data, change_event=change_event)
            self._last_saved_hash = payload_hash
            self._update_live_sync_cursor()
            if backend_name != "memory":
                print(f"[INFO] Tree data saved to {backend_name}:{target}")
        except Exception as e:
            print(f"[WARNING] Could not save tree data: {e}")
    
    def _load_data(self) -> None:
        """Load the data structure from the configured storage backend."""
        try:
            loaded_data, backend_name, source = self._persistence_service.load_data()
            if isinstance(loaded_data, dict):
                self._apply_loaded_tree_data(
                    loaded_data,
                    backend_name=backend_name,
                    source=source,
                    log_message=backend_name != "memory",
                )
        except Exception as e:
            print(f"[INFO] Could not load tree data (this is normal on first run): {e}")
    
    @Slot(bool)
    def _import_data_file_dialog(
        self,
        checked: bool = False,
        *,
        preset_format: str | None = None,
    ) -> None:
        """Import structured data from Python/JSON/YAML/TOML files."""
        from PySide6.QtWidgets import QFileDialog, QInputDialog

        _ = checked

        supported_formats = ["Python", "JSON", "YAML", "TOML"]
        selected_format = str(preset_format or "").strip().upper()
        if selected_format not in {fmt.upper() for fmt in supported_formats}:
            selected_format, ok = QInputDialog.getItem(
                self,
                "Import Format",
                "Choose file type:",
                supported_formats,
                0,
                False,
            )
            if not ok or not selected_format:
                return

        selected_format = str(selected_format).strip().upper()
        file_filters = {
            "PYTHON": "Python Files (*.py);;All Files (*)",
            "JSON": "JSON Files (*.json);;All Files (*)",
            "YAML": "YAML Files (*.yaml *.yml);;All Files (*)",
            "TOML": "TOML Files (*.toml);;All Files (*)",
        }

        file_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            f"Import {selected_format} File",
            str(Path.home()),
            file_filters.get(selected_format, "All Files (*)"),
        )
        if not file_path:
            return

        try:
            imported_data = self._load_import_payload(file_path, selected_format)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Import Error",
                f"Failed to import {selected_format} file:\n{exc}",
            )
            return

        self._commit_imported_data(file_path, imported_data, selected_format)

    def _load_import_payload(self, file_path: str, import_format: str) -> Any:
        fmt = str(import_format or "").strip().upper()
        if fmt == "JSON":
            with open(file_path, "r", encoding="utf-8") as handle:
                return json.load(handle)

        if fmt == "YAML":
            try:
                import yaml  # type: ignore
            except Exception as exc:
                raise RuntimeError("YAML import requires the PyYAML package.") from exc
            with open(file_path, "r", encoding="utf-8") as handle:
                return yaml.safe_load(handle)

        if fmt == "TOML":
            try:
                import tomllib  # Python 3.11+
            except Exception as exc:
                raise RuntimeError("TOML import is not available in this Python runtime.") from exc
            with open(file_path, "rb") as handle:
                return tomllib.load(handle)

        if fmt == "PYTHON":
            with open(file_path, "r", encoding="utf-8") as handle:
                return self._parse_python_import_payload(handle.read(), file_path)

        raise ValueError(f"Unsupported import format: {import_format}")

    def _parse_python_import_payload(self, source: str, file_path: str = "") -> Any:
        """Parse Python files that contain literal data or literal assignments."""
        import ast

        payload = str(source or "").strip()
        if not payload:
            raise ValueError("Python file is empty.")

        try:
            return ast.literal_eval(payload)
        except Exception:
            pass

        module = ast.parse(payload, filename=file_path or "<python-import>")
        assignment_total = 0
        literal_assignments: dict[str, Any] = {}
        for node in module.body:
            value_node = None
            target_name = ""
            if isinstance(node, ast.Assign):
                value_node = node.value
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    target_name = str(node.targets[0].id or "").strip()
            elif isinstance(node, ast.AnnAssign):
                value_node = node.value
                if isinstance(node.target, ast.Name):
                    target_name = str(node.target.id or "").strip()

            if value_node is None:
                continue
            assignment_total += 1

            try:
                literal_value = ast.literal_eval(value_node)
                if target_name:
                    literal_assignments[target_name] = literal_value
                elif assignment_total == 1:
                    # Keep backward compatibility for anonymous single-value imports.
                    return literal_value
            except Exception:
                continue

        # If the file contains runtime expressions (e.g. function calls/comprehensions),
        # project the executed module so the tree can represent the full config surface.
        has_non_literal_assignments = assignment_total > len(literal_assignments)
        if file_path and has_non_literal_assignments:
            module_projection = self._load_python_module_projection(file_path)
            if module_projection:
                return module_projection

        if literal_assignments:
            if len(literal_assignments) == 1:
                return next(iter(literal_assignments.values()))
            return literal_assignments

        raise ValueError(
            "Python import supports literal values (dict/list/etc.) or assignments to literal values."
        )

    def _load_python_module_projection(self, file_path: str) -> dict[str, Any]:
        source_path = Path(file_path).expanduser().resolve()
        if not source_path.is_file():
            return {}

        module_name = f"data_tree_projection_{source_path.stem}_{hashlib.sha256(str(source_path).encode('utf-8')).hexdigest()[:12]}"
        spec = importlib.util.spec_from_file_location(module_name, str(source_path))
        if spec is None or spec.loader is None:
            return {}

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        projected_symbols: dict[str, Any] = {}
        for symbol_name, symbol_value in vars(module).items():
            normalized_name = str(symbol_name or "").strip()
            if not normalized_name or normalized_name.startswith("__"):
                continue
            if not normalized_name.lstrip("_").isupper():
                continue

            normalized_value = self._normalize_python_projection_value(symbol_value)
            projected_symbols[normalized_name] = normalized_value

        return {
            "module_path": str(source_path),
            "module_name": source_path.stem,
            "symbol_count": len(projected_symbols),
            "symbols": projected_symbols,
        }

    def _normalize_python_projection_value(self, value: Any, *, _depth: int = 0) -> Any:
        if _depth > 12:
            return "<max_depth_reached>"

        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return {
                str(key): self._normalize_python_projection_value(item, _depth=_depth + 1)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple)):
            return [self._normalize_python_projection_value(item, _depth=_depth + 1) for item in value]

        if isinstance(value, set):
            normalized_items = [self._normalize_python_projection_value(item, _depth=_depth + 1) for item in value]
            return sorted(normalized_items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))

        if callable(value):
            return f"<callable:{getattr(value, '__name__', type(value).__name__)}>"

        module_name = getattr(value, "__module__", "")
        class_name = type(value).__name__
        if module_name:
            return f"<{module_name}.{class_name}>"
        return f"<{class_name}>"

    def _commit_imported_data(self, file_path: str, imported_data: Any, import_format: str) -> None:
        from PySide6.QtWidgets import QInputDialog

        sections = list(self._root_sections.keys())
        if not sections:
            QMessageBox.warning(self, "Import Error", "No target section available.")
            return

        section_name, ok = QInputDialog.getItem(
            self,
            "Select Section",
            "Add imported data to section:",
            sections,
            0,
            False,
        )
        if not ok or not section_name:
            return

        file_name = Path(file_path).stem
        key_name, ok = QInputDialog.getText(
            self,
            "Item Name",
            "Enter name for imported data:",
            text=file_name,
        )
        if not ok or not key_name:
            return

        normalized_imported_data = imported_data
        if isinstance(imported_data, str):
            stripped_payload = imported_data.strip()
            candidate_payload_list = [stripped_payload]
            if len(stripped_payload) >= 2 and stripped_payload[0] == stripped_payload[-1] and stripped_payload[0] in {"'", '"'}:
                candidate_payload_list.append(stripped_payload[1:-1].strip())
            for candidate_payload in candidate_payload_list:
                if not candidate_payload:
                    continue
                if not (
                    (candidate_payload.startswith("{") and candidate_payload.endswith("}"))
                    or (candidate_payload.startswith("[") and candidate_payload.endswith("]"))
                ):
                    continue
                try:
                    parsed_payload = json.loads(candidate_payload)
                except Exception:
                    continue
                if isinstance(parsed_payload, (dict, list)):
                    normalized_imported_data = parsed_payload
                    break

        self.add_to_section(section_name, key_name, normalized_imported_data)
        if callable(sync_parser_result_to_agentsdb_knowledge):
            try:
                serialized_payload = json.dumps(normalized_imported_data, ensure_ascii=False, default=str)
            except Exception:
                serialized_payload = str(normalized_imported_data)
            sync_parser_result_to_agentsdb_knowledge(
                object_name="documents",
                correlation_id=f"tree-import:{hashlib.sha256(f'{file_path}:{key_name}:{section_name}'.encode('utf-8')).hexdigest()[:24]}",
                result_payload={
                    "agent": "data_tree_import",
                    "source": "data_tree_import",
                    "source_path": str(file_path),
                    "title": str(key_name),
                    "record_kind": "document",
                    "kind": "document",
                    "object_name": "documents",
                    "file": {
                        "source_path": str(file_path),
                        "path": str(file_path),
                        "name": Path(file_path).name,
                        "import_format": str(import_format),
                    },
                    "parse": {
                        "raw_text": serialized_payload,
                    },
                    "content_sha256": f"tree-import:{hashlib.sha256(f'{file_path}:{key_name}:{section_name}'.encode('utf-8')).hexdigest()[:24]}",
                    "status": "processed",
                    "processing_state": "processed",
                    "processed": True,
                    "failed_reason": None,
                    "document": {
                        "title": str(key_name),
                        "summary": f"Data tree import from {Path(file_path).name}",
                        "section": str(section_name),
                        "import_format": str(import_format),
                        "payload": normalized_imported_data,
                    },
                    "db_updates": {
                        "processing_state": "processed",
                        "processed": True,
                    },
                },
                handoff_metadata={"source_agent": "data_tree_import"},
                handoff_payload={"agent_label": "data_tree_import", "source": "data_tree"},
            )
            self._save_data()
        QMessageBox.information(
            self,
            "Import Success",
            f"{import_format} data imported to {section_name}/{key_name}",
        )

    @Slot(bool)
    def _import_json_file(self, checked: bool = False) -> None:
        """Backward-compatible JSON import entry point."""
        self._import_data_file_dialog(checked, preset_format="JSON")
    
    @Slot(bool)
    def _export_json_file(self, checked: bool = False) -> None:
        """Export current data to a JSON file with section selection."""
        from PySide6.QtWidgets import QFileDialog, QDialog, QVBoxLayout, QCheckBox, QPushButton, QDialogButtonBox
        
        # Create dialog for section selection
        dialog = QDialog(self)
        dialog.setWindowTitle("Export Sections")
        dialog.setMinimumWidth(300)
        
        layout = QVBoxLayout(dialog)
        layout.addWidget(QMessageBox().information(None, "Info", "Select sections to export:") or QWidget())
        
        # Create checkboxes for each section
        checkboxes = {}
        for section_name in self._root_sections.keys():
            item_count = len(self._data.get(section_name, {}))
            cb = QCheckBox(f"{section_name} ({item_count} items)")
            cb.setChecked(True)
            checkboxes[section_name] = cb
            layout.addWidget(cb)
        
        # Add buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() != QDialog.Accepted:
            return
        
        # Get selected sections
        selected_sections = {
            name: self._data[name]
            for name, cb in checkboxes.items()
            if cb.isChecked() and name in self._data
        }
        
        if not selected_sections:
            QMessageBox.warning(self, "Export", "No sections selected")
            return
        
        # File dialog
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export JSON File",
            str(Path.home() / "tree_export.json"),
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(selected_sections, f, indent=2, ensure_ascii=False)
            
            section_names = ", ".join(selected_sections.keys())
            QMessageBox.information(
                self,
                "Export Success",
                f"Sections exported: {section_names}\nTo: {file_path}"
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "Export Error",
                f"Failed to export JSON file:\n{e}"
            )
    
    @Slot(bool)
    def _load_template(self, checked: bool = False) -> None:
        """Load predefined templates or custom configurations."""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QDialogButtonBox, QFileDialog, QPushButton
        
        # Create dialog
        dialog = QDialog(self)
        dialog.setWindowTitle("Load Template or Configuration")
        dialog.setMinimumSize(400, 300)
        
        layout = QVBoxLayout(dialog)
        
        # List of built-in templates
        list_widget = QListWidget()
        templates = self._get_builtin_templates()
        for name in templates.keys():
            list_widget.addItem(name)
        layout.addWidget(list_widget)
        
        # Custom file button
        custom_btn = QPushButton("Load from file...")
        custom_btn.clicked.connect(lambda: dialog.done(2))  # Custom result code
        layout.addWidget(custom_btn)
        
        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        result = dialog.exec()
        
        if result == QDialog.Accepted:
            # Load selected template
            selected_item = list_widget.currentItem()
            if selected_item:
                template_name = selected_item.text()
                template_data = templates.get(template_name)
                if template_data:
                    self._apply_template(template_data, template_name)
        elif result == 2:
            # Load from custom file
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Load Configuration File",
                str(Path.home()),
                "JSON Files (*.json);;All Files (*)"
            )
            if file_path:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                    self._apply_template(config_data, Path(file_path).stem)
                except Exception as e:
                    QMessageBox.critical(self, "Load Error", f"Failed to load file:\n{e}")
    
    def _get_builtin_templates(self) -> dict:
        """Return built-in templates and user-saved templates."""
        templates = {
            "Python Web Project": {
                "PROJECTS": {
                    "WebApp": {
                        "name": "Python Web Application",
                        "path": "",
                        "files": ["app.py", "requirements.txt", "config.py"],
                        "settings": {
                            "framework": "Flask",
                            "python_version": "3.11",
                            "debug": True
                        }
                    }
                },
                "DATABASES": {
                    "PostgreSQL-Dev": {
                        "type": "PostgreSQL",
                        "host": "localhost",
                        "port": 5432,
                        "database": "webapp_dev",
                        "username": "dev_user"
                    }
                }
            },
            "Data Science Project": {
                "PROJECTS": {
                    "DataAnalysis": {
                        "name": "Data Analysis Project",
                        "path": "",
                        "files": ["analysis.ipynb", "data_processing.py", "requirements.txt"],
                        "settings": {
                            "python_version": "3.11",
                            "libraries": ["pandas", "numpy", "matplotlib", "scikit-learn"]
                        }
                    }
                }
            },
            "Microservices Setup": {
                "PROJECTS": {
                    "API-Gateway": {
                        "name": "API Gateway Service",
                        "path": "",
                        "port": 8000,
                        "type": "gateway"
                    },
                    "Auth-Service": {
                        "name": "Authentication Service",
                        "path": "",
                        "port": 8001,
                        "type": "microservice"
                    },
                    "Data-Service": {
                        "name": "Data Processing Service",
                        "path": "",
                        "port": 8002,
                        "type": "microservice"
                    }
                },
                "DATABASES": {
                    "Redis-Cache": {
                        "type": "Redis",
                        "host": "localhost",
                        "port": 6379
                    },
                    "MongoDB-Main": {
                        "type": "MongoDB",
                        "host": "localhost",
                        "port": 27017,
                        "database": "microservices"
                    }
                }
            },
            "Empty Workspace": {
                "PROJECTS": {},
                "RUNTIME": {},
                "ENV": {},
                "MCP": {},
                "TEMPLATES": {},
                "DATABASES": {},
                "CHAT_HISTORY": {},
                "DOCUMENTS": {},
                "RUNTIME_VIEWS": {},
                "DISPATCHER_DB": {},
                "GENERATED_DATA": {},
                "HISTORY": {}
            }
        }
        
        # Load user-saved templates
        try:
            templates_dir = Path(__file__).parent.parent / "AppData" / "templates"
            if templates_dir.exists():
                for template_file in templates_dir.glob("*.json"):
                    try:
                        with open(template_file, 'r', encoding='utf-8') as f:
                            template_data = json.load(f)
                        template_name = f"📁 {template_file.stem}"
                        templates[template_name] = template_data
                    except Exception as e:
                        print(f"[WARNING] Could not load template {template_file}: {e}")
        except Exception as e:
            print(f"[WARNING] Could not scan templates directory: {e}")
        
        return templates
    
    def _apply_template(self, template_data: dict, template_name: str) -> None:
        """Apply template data to the tree."""
        from PySide6.QtWidgets import QInputDialog
        
        # Ask if user wants to replace or merge
        options = ["Merge with existing", "Replace all"]
        choice, ok = QInputDialog.getItem(
            self,
            "Apply Template",
            f"How to apply template '{template_name}'?",
            options,
            0,
            False
        )
        
        if not ok:
            return
        
        if choice == "Replace all":
            # Clear all sections
            for section_name in list(self._root_sections.keys()):
                section = self._root_sections[section_name]
                while section.childCount() > 0:
                    section.removeChild(section.child(0))
                self._data[section_name] = {}
        
        # Apply template data
        for section_name, section_data in template_data.items():
            if section_name not in self._root_sections:
                self._add_root_section(section_name)
            
            if isinstance(section_data, dict):
                for key, value in section_data.items():
                    self.add_to_section(section_name, key, value)
        
        QMessageBox.information(
            self,
            "Template Applied",
            f"Template '{template_name}' has been applied successfully"
        )
    
    @Slot(bool)
    def _save_as_template(self, checked: bool = False) -> None:
        """Save current workspace as a reusable template."""
        from PySide6.QtWidgets import QInputDialog, QFileDialog
        
        # Ask for template name
        template_name, ok = QInputDialog.getText(
            self,
            "Save as Template",
            "Enter template name:",
            text="My Custom Template"
        )
        
        if not ok or not template_name:
            return
        
        # Choose what to include
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QCheckBox, QDialogButtonBox
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Sections to Include")
        dialog.setMinimumWidth(300)
        
        layout = QVBoxLayout(dialog)
        
        checkboxes = {}
        for section_name in self._root_sections.keys():
            item_count = len(self._data.get(section_name, {}))
            cb = QCheckBox(f"{section_name} ({item_count} items)")
            cb.setChecked(True)
            checkboxes[section_name] = cb
            layout.addWidget(cb)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        if dialog.exec() != QDialog.Accepted:
            return
        
        # Build template data
        template_data = {
            name: self._data.get(name, {})
            for name, cb in checkboxes.items()
            if cb.isChecked()
        }
        
        # Save to templates directory
        try:
            templates_dir = Path(__file__).parent.parent / "AppData" / "templates"
            templates_dir.mkdir(exist_ok=True)
            
            safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in template_name)
            template_file = templates_dir / f"{safe_name}.json"
            
            with open(template_file, 'w', encoding='utf-8') as f:
                json.dump(template_data, f, indent=2, ensure_ascii=False)
            
            QMessageBox.information(
                self,
                "Template Saved",
                f"Template '{template_name}' saved to:\n{template_file}\n\nYou can now load it from the templates menu."
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "Save Error",
                f"Failed to save template:\n{e}"
            )
    
    @Slot(object)
    def _show_context_menu(self, position) -> None:
        """Show context menu for tree items."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        
        item = self.itemAt(position)
        if not item:
            return
        
        menu = QMenu(self)
        
        # Check if this is a section header or a regular item
        is_section = item in self._root_sections.values()
        
        if is_section:
            # Section header menu
            section_name = None
            for name, sect_item in self._root_sections.items():
                if sect_item == item:
                    section_name = name
                    break
            
            if section_name:
                # Add item to section
                add_action = QAction(f"➕ Add item to {section_name}", self)
                add_action.triggered.connect(lambda: self._context_add_item(section_name))
                menu.addAction(add_action)
                
                menu.addSeparator()
                
                # Export section
                export_action = QAction(f"📤 Export {section_name}", self)
                export_action.triggered.connect(lambda: self._context_export_section(section_name))
                menu.addAction(export_action)
                
                # Import to section
                import_action = QAction(f"📥 Import to {section_name}", self)
                import_action.triggered.connect(lambda: self._context_import_to_section(section_name))
                menu.addAction(import_action)
                
                menu.addSeparator()
                
                # Clear section
                clear_action = QAction(f"🗑 Clear {section_name}", self)
                clear_action.triggered.connect(lambda: self._context_clear_section(section_name))
                menu.addAction(clear_action)
        else:
            # Regular item menu
            section_name = self._item_to_section.get(item)
            item_key = self._item_to_key.get(item)
            resolved_section_name = section_name or self._resolve_section_name_for_item(item)
            path_segments = self._item_path_segments(item, 0, section_name=resolved_section_name)
            collection_binding = self._persistence_service.resolve_agentsdb_repository_collection_binding(
                section_name=resolved_section_name,
                path_segments=path_segments,
            )
            repository_binding = self._persistence_service.resolve_agentsdb_repository_binding(
                section_name=resolved_section_name,
                path_segments=path_segments,
            )

            if collection_binding is not None and repository_binding is None:
                add_record_action = QAction("➕ Add record", self)
                add_record_action.triggered.connect(lambda: self._context_add_agentsdb_repository_record(item, resolved_section_name))
                menu.addAction(add_record_action)
                menu.addSeparator()
            
            # Rename
            rename_action = QAction("✏️ Edit value", self)
            rename_action.triggered.connect(lambda: self._context_edit_item(item))
            menu.addAction(rename_action)
            
            # Duplicate
            duplicate_action = QAction("📋 Duplicate", self)
            duplicate_action.triggered.connect(lambda: self._context_duplicate_item(item, section_name, item_key))
            menu.addAction(duplicate_action)
            
            menu.addSeparator()
            
            # Export item
            export_action = QAction("📤 Export item", self)
            export_action.triggered.connect(lambda: self._context_export_item(item, section_name, item_key))
            menu.addAction(export_action)
            
            # Copy as JSON
            copy_action = QAction("📄 Copy as JSON", self)
            copy_action.triggered.connect(lambda: self._context_copy_json(item, section_name, item_key))
            menu.addAction(copy_action)
            
            menu.addSeparator()
            
            # Delete
            delete_action = QAction("🗑 Delete", self)
            delete_action.triggered.connect(lambda: self._context_delete_item(item, section_name, item_key))
            menu.addAction(delete_action)
        
        menu.exec(self.viewport().mapToGlobal(position))

    @Slot(QTreeWidgetItem, int)
    def _on_item_single_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not isinstance(item, QTreeWidgetItem):
            return
        if item.childCount() <= 0:
            return
        item.setExpanded(not item.isExpanded())

    @Slot(QTreeWidgetItem, int)
    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not isinstance(item, QTreeWidgetItem):
            return
        if item in self._root_sections.values():
            return
        self._context_edit_item(item)

    def _context_edit_item(self, item: QTreeWidgetItem) -> None:
        from PySide6.QtWidgets import QInputDialog

        if not isinstance(item, QTreeWidgetItem):
            return
        if item in self._root_sections.values():
            return

        item_kind = str(self._item_kind.get(item, "")).strip().lower()
        if item_kind in {"dict", "list"}:
            return

        item_text = str(item.text(0) or "").strip()
        if not item_text:
            return

        key_part = self._extract_item_key_from_text(item_text)
        if ": " in item_text:
            current_value = item_text.split(": ", 1)[1].strip()
        else:
            current_value = ""

        edited_value, ok = QInputDialog.getText(
            self,
            "Edit Value",
            f"Enter value for {key_part}:",
            text=current_value,
        )
        if not ok:
            return

        updated_text = f"{key_part}: {edited_value.strip()}"
        if updated_text == item_text:
            return

        item.setText(0, updated_text)
    
    def _context_add_item(self, section_name: str) -> None:
        """Add new item to section via context menu."""
        from PySide6.QtWidgets import QInputDialog
        
        key, ok = QInputDialog.getText(self, "New Item", f"Enter name for new item in {section_name}:")
        if ok and key:
            self.add_to_section(section_name, key, {"value": ""})

    def _context_add_agentsdb_repository_record(self, item: QTreeWidgetItem, section_name: str | None) -> None:
        """Create a new AgentDB record below a repository collection node."""
        from PySide6.QtWidgets import QInputDialog

        resolved_section_name = section_name or self._resolve_section_name_for_item(item)
        path_segments = self._item_path_segments(item, 0, section_name=resolved_section_name)
        collection_binding = self._persistence_service.resolve_agentsdb_repository_collection_binding(
            section_name=resolved_section_name,
            path_segments=path_segments,
        )
        if collection_binding is None:
            return

        collection_name = str(collection_binding.get("collection_name") or path_segments[-1] or "record").strip()
        record_id, ok = QInputDialog.getText(
            self,
            "New AgentDB Record",
            f"Enter record id for {collection_name}:",
        )
        normalized_record_id = str(record_id or "").strip()
        if not ok or not normalized_record_id:
            return

        created = self._persistence_service.create_agentsdb_repository_record(
            section_name=resolved_section_name,
            path_segments=path_segments,
            record_id=normalized_record_id,
        )
        if created:
            self._reload_tree_from_persistence(log_message=False)
            QMessageBox.information(self, "Created", f"'{normalized_record_id}' has been added to {collection_name}")
        else:
            QMessageBox.warning(self, "Create Error", f"Could not create '{normalized_record_id}' in {collection_name}")
    
    def _context_export_section(self, section_name: str) -> None:
        """Export single section."""
        from PySide6.QtWidgets import QFileDialog
        
        if section_name not in self._data:
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export {section_name}",
            str(Path.home() / f"{section_name.lower()}_export.json"),
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self._data[section_name], f, indent=2, ensure_ascii=False)
            QMessageBox.information(self, "Export Success", f"{section_name} exported to:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export:\n{e}")
    
    def _context_import_to_section(self, section_name: str) -> None:
        """Import JSON to specific section."""
        from PySide6.QtWidgets import QFileDialog, QInputDialog
        
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            f"Import to {section_name}",
            str(Path.home()),
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            key, ok = QInputDialog.getText(
                self,
                "Item Name",
                "Enter name for imported data:",
                text=Path(file_path).stem
            )
            
            if ok and key:
                self.add_to_section(section_name, key, data)
                QMessageBox.information(self, "Import Success", f"Data imported to {section_name}/{key}")
        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Failed to import:\n{e}")
    
    def _context_clear_section(self, section_name: str) -> None:
        """Clear all items in section."""
        reply = QMessageBox.question(
            self,
            "Clear Section",
            f"Delete all items in {section_name}?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            section = self._root_sections.get(section_name)
            if section:
                while section.childCount() > 0:
                    section.removeChild(section.child(0))
                self._data[section_name] = {}
                self._apply_board_card_item_widgets()
                self._save_data(
                    change_event={
                        "action": "clear_section",
                        "origin": "tree_widget",
                        "section_name": section_name,
                    }
                )
                QMessageBox.information(self, "Cleared", f"{section_name} has been cleared")
    
    def _context_duplicate_item(self, item: QTreeWidgetItem, section_name: str, item_key: str) -> None:
        """Duplicate an item."""
        from PySide6.QtWidgets import QInputDialog
        
        if not section_name or not item_key:
            return
        
        original_data = self._data.get(section_name, {}).get(item_key)
        if original_data is None:
            return
        
        new_key, ok = QInputDialog.getText(
            self,
            "Duplicate Item",
            "Enter name for duplicated item:",
            text=f"{item_key}_copy"
        )
        
        if ok and new_key:
            import copy
            self.add_to_section(section_name, new_key, copy.deepcopy(original_data))
            QMessageBox.information(self, "Duplicated", f"Item duplicated as {new_key}")
    
    def _context_export_item(self, item: QTreeWidgetItem, section_name: str, item_key: str) -> None:
        """Export single item to JSON file."""
        from PySide6.QtWidgets import QFileDialog
        
        if not section_name or not item_key:
            return
        
        item_data = self._data.get(section_name, {}).get(item_key)
        if item_data is None:
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Item",
            str(Path.home() / f"{item_key}.json"),
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(item_data, f, indent=2, ensure_ascii=False)
            QMessageBox.information(self, "Export Success", f"Item exported to:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export:\n{e}")
    
    def _context_copy_json(self, item: QTreeWidgetItem, section_name: str, item_key: str) -> None:
        """Copy item as JSON to clipboard."""
        from PySide6.QtWidgets import QApplication
        
        if not section_name or not item_key:
            return
        
        item_data = self._data.get(section_name, {}).get(item_key)
        if item_data is None:
            return
        
        try:
            json_str = json.dumps(item_data, indent=2, ensure_ascii=False)
            clipboard = QApplication.clipboard()
            clipboard.setText(json_str)
            QMessageBox.information(self, "Copied", "JSON copied to clipboard")
        except Exception as e:
            QMessageBox.warning(self, "Copy Error", f"Failed to copy:\n{e}")
    
    def _context_delete_item(self, item: QTreeWidgetItem, section_name: str, item_key: str) -> None:
        """Delete an item."""
        resolved_section_name = section_name or self._resolve_section_name_for_item(item)
        path_segments = self._item_path_segments(item, 0, section_name=resolved_section_name)
        repository_binding = self._persistence_service.resolve_agentsdb_repository_binding(
            section_name=resolved_section_name,
            path_segments=path_segments,
        )
        if repository_binding is not None:
            target_label = str(path_segments[-1] or item.text(0) or "item").strip()
            reply = QMessageBox.question(
                self,
                "Delete Item",
                f"Delete '{target_label}' from AgentDB?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                deleted = self._persistence_service.delete_agentsdb_repository_path(
                    section_name=resolved_section_name,
                    path_segments=path_segments,
                )
                if deleted:
                    self._reload_tree_from_persistence(log_message=False)
                    QMessageBox.information(self, "Deleted", f"'{target_label}' has been deleted")
                else:
                    QMessageBox.warning(self, "Delete Error", f"Could not delete '{target_label}' from AgentDB")
            return

        if self._is_agentsdb_repository_root_item(section_name=resolved_section_name, item_key=item_key):
            QMessageBox.information(self, "Delete Item", "The AgentDB repository view is derived from the live database and cannot be removed from the tree.")
            return

        if not section_name or not item_key:
            return
        
        reply = QMessageBox.question(
            self,
            "Delete Item",
            f"Delete '{item_key}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.remove_from_section(section_name, item_key)
            QMessageBox.information(self, "Deleted", f"'{item_key}' has been deleted")
    
    @Slot(bool)
    def _show_history_tree(self, checked: bool = False) -> None:
        if ChatHistory is None:
            QMessageBox.information(self, "History", "ChatHistory not available")
            return

        allowed_sections = self._persistence_service._tree_section_allowlist()
        target_section = None
        for candidate_section in ("CHAT_HISTORY", "HISTORY"):
            if candidate_section in allowed_sections:
                target_section = candidate_section
                break
        if target_section is None:
            QMessageBox.information(
                self,
                "History",
                "Chat history section is not enabled in AI_IDE_TREE_SECTION_ALLOWLIST",
            )
            return

        try:
            history = ChatHistory._load()
        except Exception as e:
            QMessageBox.warning(self, "History", f"Could not load history: {e}")
            return
        # Restore original behavior: show the full history structure.
        # Persist chat history in the configured tree storage backend.
        try:
            self.remove_from_section("CHAT_HISTORY", "Chat History")
        except Exception:
            pass
        try:
            self.remove_from_section("HISTORY", "Chat History")
        except Exception:
            pass
        self.add_to_section(target_section, "Chat History", history, persist=True)


# ------------------------- JsonHighlighter ------------------------------
from PySide6.QtCore import QRegularExpression

# ─────────────────────── JsonHighlighter ───────────────────────
class JsonHighlighter(QSyntaxHighlighter):
    """
    Tiny JSON syntax highlighter (dark theme) used by ClosableTextEdit but
    can be reused on every QTextDocument.
    """

    def __init__(self, doc):
        super().__init__(doc)

        mono = QFont("Fira Code", 10)
        mono.setStyleHint(QFont.Monospace)

        def _fmt(color: str) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setFont(mono)
            f.setForeground(QColor(color))
            return f

        self._fmt_string = _fmt("#ce9178")
        self._fmt_number = _fmt("#b5cea8")
        self._fmt_bool   = _fmt("#4fc1ff")
        self._fmt_null   = _fmt("#c586c0")
        self._fmt_key    = _fmt("#569cd6")

        # regular expressions ---------------------------------------------
        self._rx_string = QRegularExpression(r'"([^"\\]|\\.)*"')
        self._rx_number = QRegularExpression(
            r"\b-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?\b")
        self._rx_bool   = QRegularExpression(r"\b(true|false)\b")
        self._rx_null   = QRegularExpression(r"\bnull\b")
        self._rx_key    = QRegularExpression(r'"([^"\\]|\\.)*"\s*:')

    # noinspection PyPep8Naming
    def highlightBlock(self, text: str) -> None:         # noqa: N802
        """Apply colour formats for each token type."""
        def _apply(fmt: QTextCharFormat, rx: QRegularExpression):
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)

        _apply(self._fmt_string, self._rx_string)
        _apply(self._fmt_number, self._rx_number)
        _apply(self._fmt_bool,   self._rx_bool)
        _apply(self._fmt_null,   self._rx_null)
        _apply(self._fmt_key,    self._rx_key)




# ─── constants.py  (new helper file – may also live at the top of the module)
SCROLLBAR_HOVER_ONLY_DARK = """
/* ==== generic dark style – hide until mouse-over, no arrows ==== */

/* --- shared  -------------------------------------------------- */
QScrollBar:horizontal, QScrollBar:vertical {
    background: transparent;          /* nothing until hover        */
    margin: 0px;                      /* no outer gaps              */
    border: none;
}

/* size while idle (almost invisible but still receives hover)   */
QScrollBar:vertical   { width: 6px;  }
QScrollBar:horizontal { height:50px;  }

/* grow a bit + colour when mouse enters the bar itself          */
QScrollBar:vertical:hover   { width: 6px; }
QScrollBar:horizontal:hover { height:50px; }

/* ----- handle (the draggable knob) --------------------------- */
QScrollBar::handle {
    background: rgba(120,120,120,0.0);   /* transparent while idle  */
    border-radius: 4px;
    min-width: 6px;
    min-height: 600px;
}
QScrollBar::handle:hover {
    background: rgba(120,120,120,0.6);   /* show on hover           */
}

/* ----- remove arrows & useless areas ------------------------- */
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;  border: none;  width:0px; height:0px;
}
"""
