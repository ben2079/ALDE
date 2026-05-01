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

from typing import Any
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import Qt, QTimer, Slot, QSize
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
    from .agents_db import AgentDbSocketRepository, KnowledgeRepository, load_agentsdb_runtime_config_from_env, sync_parser_result_to_agentsdb_knowledge  # type: ignore
except Exception:
    try:
        from alde.agents_db import AgentDbSocketRepository, KnowledgeRepository, load_agentsdb_runtime_config_from_env, sync_parser_result_to_agentsdb_knowledge  # type: ignore
    except Exception:
        AgentDbSocketRepository = None  # type: ignore[assignment]
        KnowledgeRepository = None  # type: ignore[assignment]
        load_agentsdb_runtime_config_from_env = None  # type: ignore[assignment]
        sync_parser_result_to_agentsdb_knowledge = None  # type: ignore[assignment]

# Try to import ChatHistory if available; keep optional.
try:
    from .chat_completion import ChatHistory  # type: ignore
except Exception:  # allow running as script from repo root
    try:
        from alde.chat_completion import ChatHistory  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        ChatHistory = None  # type: ignore


def _load_optional_mongo_client_class() -> Any | None:
    try:
        pymongo_module = importlib.import_module("pymongo")
    except Exception:
        return None
    return getattr(pymongo_module, "MongoClient", None)


class TreeDataPersistenceService:
    """Persist tree data either in AgentDB or in a local JSON fallback file."""

    _ENV_ASSIGNMENT_PATTERN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    _ENV_OBJECT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    _AI_IDE_SECTION_NAME_ORDER: tuple[str, ...] = (
        "PROJECTS",
        "RUNTIME",
        "ENV",
        "TEMPLATES",
        "DATABASES",
        "CHAT_HISTORY",
        "DOCUMENTS",
        "RUNTIME_VIEWS",
        "DISPATCHER_DB",
        "GENERATED_DATA",
        "HISTORY",
    )
    _PROJECTION_SOURCE_OBJECT_DEFINITION: tuple[dict[str, Any], ...] = (
        {"section": "ENV", "key": ".env", "kind": "env_file", "file_name": ".env"},
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

    def __init__(self, app_data_dir: Path) -> None:
        self._app_data_dir = app_data_dir
        self._json_path = app_data_dir / "tree_data.json"
        self._mongo_client: Any | None = None
        self._mongo_collection: Any | None = None
        self._mongo_disabled = False
        self._storage_config_cache: dict[str, Any] | None = None

    def _agentsdb_strict_mode(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_STRICT", "1")).strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _agentsdb_tree_sync_enabled(self) -> bool:
        value = str(os.getenv("AI_IDE_AGENTS_DB_TREE_SYNC", "0")).strip().lower()
        return value in {"1", "true", "yes", "on"}

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

    def _load_agentsdb_repository(self) -> tuple[Any, Any] | tuple[None, None]:
        if not callable(load_agentsdb_runtime_config_from_env):
            return None, None
        runtime_config = load_agentsdb_runtime_config_from_env()
        if runtime_config is None:
            return None, None
        uri = str(getattr(runtime_config, "agents_db_uri", "") or "").strip()
        database_name = str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge"
        if not uri:
            return None, None
        if uri.lower().startswith("agentsdb://"):
            if AgentDbSocketRepository is None:
                return None, None
            return runtime_config, AgentDbSocketRepository.create_from_uri(uri, database_name)
        if KnowledgeRepository is None:
            return None, None
        return runtime_config, KnowledgeRepository.create_from_uri(uri, database_name)

    def _tree_object_id(self) -> str:
        configured_id = str(os.getenv("AI_IDE_TREE_AGENTS_DB_OBJECT_ID", "")).strip()
        return configured_id or "tree_widget:tree_data"

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

        for section_name in self._AI_IDE_SECTION_NAME_ORDER:
            normalized_data.setdefault(section_name, {})

        return normalized_data

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

    def _env_projection_path_candidates(self, app_data_dir_list: list[Path]) -> list[Path]:
        source_file = Path(__file__).resolve()
        candidate_path_list: list[Path] = [
            source_file.parents[1] / ".env",
            source_file.parents[2] / ".env",
            source_file.with_suffix(".env"),
        ]

        for app_data_dir in app_data_dir_list:
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
            line_list = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

        section_payload_map: dict[str, dict[str, dict[str, Any]]] = {}

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
                        "value": str(disabled_assignment_match.group(2) or ""),
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
                "value": str(assignment_match.group(2) or ""),
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
    ) -> tuple[Any | None, str, str | None]:
        source_kind = str(source_object.get("kind") or "").strip().lower()
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
            configured_path = Path(configured_file_name).expanduser()

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
            file_name = str(source_object.get("file_name") or "").strip()
            if not file_name:
                return None, "", None
            for app_data_dir in app_data_dir_list:
                file_path = app_data_dir / file_name
                payload = self._load_json_projection_object(file_path)
                if payload is not None:
                    return payload, str(file_path), self._mtime_iso(file_path)
            return None, "", None

        if source_kind == "directory_index":
            dir_name = str(source_object.get("dir_name") or "").strip()
            if not dir_name:
                return None, "", None
            pattern = str(source_object.get("pattern") or "*").strip() or "*"
            for app_data_dir in app_data_dir_list:
                directory_path = app_data_dir / dir_name
                payload = self._load_directory_file_index(directory_path, pattern=pattern)
                if payload is not None:
                    return payload, str(directory_path), self._mtime_iso(directory_path)
            return None, "", None

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
            payload, uri, updated_at = self._load_projection_payload_from_local_source(source_object, app_data_dir_list)
            local_loaded = True
            local_payload = payload
            local_uri = str(uri or "")
            local_updated_at = self._timestamp_from_iso(updated_at)
            return local_payload, local_uri, local_updated_at

        if conflict_policy == "agentsdb_strict":
            if agentsdb_payload is None:
                return None, "", False
            return agentsdb_payload, agentsdb_uri, True

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
            for section_name in self._AI_IDE_SECTION_NAME_ORDER
        }

        app_data_dir_list = self._projection_app_data_dir_list()
        for source_object in self._PROJECTION_SOURCE_OBJECT_DEFINITION:
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
        return self._apply_tree_storage_projection_policy(merged_data)

    def _apply_tree_storage_projection_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._normalize_tree_data_structure(data)
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

    def _agentsdb_tree_payload(self, *, runtime_config: Any, repository: Any | None, data: dict[str, Any]) -> dict[str, Any]:
        normalized_data = self._merge_local_projection_sections(
            self._normalize_tree_data_structure(data),
            runtime_config=runtime_config,
            repository=repository,
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        namespace_id = str(getattr(runtime_config, "namespace_id", "") or "ns_alde_default").strip() or "ns_alde_default"
        return {
            "namespace_id": namespace_id,
            "title": "AI IDE Source Tree",
            "summary": "AgentsDB stores the complete AI IDE tree as source-of-truth for UI projections.",
            "document_type": "ai_ide_tree",
            "source_uri": "alde://ai_ide/tree",
            "tree_data": normalized_data,
            "agentsdb_tree": self._agentsdb_tree_projection(runtime_config=runtime_config, repository=repository),
            "projection_contract": {
                "source_of_truth": "agents_db",
                "consumer": "ai_ide",
                "section_order": list(self._AI_IDE_SECTION_NAME_ORDER),
                "projection_conflict_policy": self._projection_conflict_policy(),
            },
            "updated_at": timestamp,
            "created_at": timestamp,
        }

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
        runtime_config, repository = self._load_agentsdb_repository()
        tree_sync_enabled = self._agentsdb_tree_sync_enabled()
        if repository is not None and not tree_sync_enabled:
            self._purge_agentsdb_tree_object(repository)

        if repository is not None and tree_sync_enabled:
            try:
                record = repository.load_object("document", self._tree_object_id())
                tree_payload = record.get("tree_data") if isinstance(record, dict) else None
                if not isinstance(tree_payload, dict) and isinstance(record, dict):
                    tree_payload = record.get("data")
                if isinstance(tree_payload, dict):
                    previous_tree_hash = self._tree_data_content_hash(tree_payload)
                    normalized_tree_payload = self._merge_local_projection_sections(
                        self._normalize_tree_data_structure(tree_payload),
                        runtime_config=runtime_config,
                        repository=repository,
                    )
                    if previous_tree_hash != self._tree_data_content_hash(normalized_tree_payload):
                        try:
                            repository.upsert_object(
                                "document",
                                self._tree_object_id(),
                                self._agentsdb_tree_payload(
                                    runtime_config=runtime_config,
                                    repository=repository,
                                    data=normalized_tree_payload,
                                ),
                            )
                        except Exception:
                            pass
                    return normalized_tree_payload, "agents_db", self._tree_object_id()
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
                    normalized_payload = self._merge_local_projection_sections(
                        self._normalize_tree_data_structure(payload),
                        runtime_config=runtime_config,
                        repository=repository,
                    )
                    if tree_sync_enabled and repository is not None and runtime_config is not None:
                        try:
                            repository.upsert_object(
                                "document",
                                self._tree_object_id(),
                                self._agentsdb_tree_payload(runtime_config=runtime_config, repository=repository, data=normalized_payload),
                            )
                        except Exception:
                            pass
                    return normalized_payload, "mongodb", self._mongo_target_label()
            except Exception as exc:
                print(f"[WARNING] MongoDB tree load failed, falling back to JSON: {exc}")

        if self._json_path.exists():
            with open(self._json_path, "r", encoding="utf-8") as data_file:
                loaded_data = json.load(data_file)
            if isinstance(loaded_data, dict):
                normalized_loaded_data = self._merge_local_projection_sections(
                    self._normalize_tree_data_structure(loaded_data),
                    runtime_config=runtime_config,
                    repository=repository,
                )
                if tree_sync_enabled and repository is not None and runtime_config is not None:
                    try:
                        repository.upsert_object(
                            "document",
                            self._tree_object_id(),
                            self._agentsdb_tree_payload(runtime_config=runtime_config, repository=repository, data=normalized_loaded_data),
                        )
                    except Exception:
                        pass
                return normalized_loaded_data, "json", str(self._json_path)
        if tree_sync_enabled and self._agentsdb_strict_mode():
            raise RuntimeError("agents_db tree object not found and strict mode is enabled")
        return self._merge_local_projection_sections(
            self._normalize_tree_data_structure({}),
            runtime_config=runtime_config,
            repository=repository,
        ), "json", str(self._json_path)

    def save_data(self, data: dict[str, Any]) -> tuple[str, str]:
        runtime_config, repository = self._load_agentsdb_repository()
        tree_sync_enabled = self._agentsdb_tree_sync_enabled()
        normalized_data = self._merge_local_projection_sections(
            self._normalize_tree_data_structure(data),
            runtime_config=runtime_config,
            repository=repository,
        )
        if repository is not None and runtime_config is not None:
            if tree_sync_enabled:
                try:
                    repository.upsert_object(
                        "document",
                        self._tree_object_id(),
                        self._agentsdb_tree_payload(runtime_config=runtime_config, repository=repository, data=normalized_data),
                    )
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
                return "mongodb", self._target_label()
            except Exception as exc:
                print(f"[WARNING] MongoDB tree save failed, falling back to JSON: {exc}")

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
QScrollBar:vertical   { width: 4px;  }
QScrollBar:horizontal { height:50px;  }

/* grow a bit + colour when mouse enters the bar itself          */
QScrollBar:vertical:hover   { width: 4px; }
QScrollBar:horizontal:hover { height:50px; }

/* ----- handle (the draggable knob) --------------------------- */
QScrollBar::handle {
    background: rgba(120,120,120,0.0);   /* transparent while idle  */
    border-radius: 4px;
    min-width: 4px;
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

    p = Path(__file__).with_name("symbols") / s
    if p.is_file():
        return QIcon(str(p))
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
        self._bg_color = "#1D1D1D"
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
        toolbar_layout.setContentsMargins(4, 2, 4, 2)
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
            f"QWidget#JsonTreeWidgetWithToolbar {{ background: transparent; border-radius: 14px; }}"
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


# ------------------------- JsonTreeWidget -------------------------------
class JsonTreeWidget(QTreeWidget):
    _DEFAULT_ROOT_SECTION_LAYOUT: tuple[tuple[str, bool], ...] = (
        ("PROJECTS", False),
        ("RUNTIME", True),
        ("ENV", True),
        ("TEMPLATES", True),
        ("DATABASES", True),
        ("CHAT_HISTORY", True),
        ("DOCUMENTS", True),
        ("RUNTIME_VIEWS", True),
        ("DISPATCHER_DB", True),
        ("GENERATED_DATA", True),
        ("HISTORY", True),
    )
    _SMALL_FONT_SECTION_NAMES: set[str] = {"PROJECTS", "CHAT_HISTORY", "HISTORY"}
    _HISTORY_SECTION_NAMES: set[str] = {"CHAT_HISTORY", "HISTORY"}

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def sizeHint(self) -> QSize:
        return QSize(200, 160)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setAnimated(True)

        # Guards / caches for persistence.
        self._initializing = True
        self._item_last_text: dict[QTreeWidgetItem, str] = {}
        self._last_saved_hash: str | None = None
        app_data_dir = Path(__file__).parent.parent / "AppData"
        self._persistence_service = TreeDataPersistenceService(app_data_dir)
        
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

        self._style_template = """
               QTreeWidget, QTreeView {{
                   background-color:{bg_color};
                   color:{text_color};
                   font-family:'Fira Code', monospace;
                   border: 1px solid #303030;
                   border-top: none;
                   border-bottom-left-radius: 14px;
                   border-bottom-right-radius: 14px;
                   padding: 4px 0px 6px 0px;
                   outline: none;
               }}
               QTreeWidget::item, QTreeView::item {{
                   padding: 2px;
                   background-color:{bg_color};
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
        self._text_color = "#d4d4d4"
        self._bg_color = "#1D1D1D"
        self._accent_color = "#3a5fff"

        # Typography
        self._section_header_font_size = 10
        self._section_item_font_size_small = 10
        self._apply_stylesheet()

        # NOTE: itemChanged is connected after initial population to avoid
        # triggering persistence while icons/fonts are being applied.
        
        # Enable context menu
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        
        # Initialize default root sections
        self._initialize_root_sections()

        # Connect signal for handling item edits (after initial load).
        self.itemExpanded.connect(self._on_item_expanded)
        self.itemChanged.connect(self._on_item_changed)
        self._remember_tree_texts()
        self._initializing = False

    def _should_lazy_load_children(self, section_name: str | None, value: Any) -> bool:
        section_upper = (section_name or "").upper()
        return section_upper in self._HISTORY_SECTION_NAMES and isinstance(value, (dict, list, tuple)) and bool(value)

    def _add_lazy_placeholder(self, item: QTreeWidgetItem, value: Any, section_name: str | None) -> None:
        placeholder = QTreeWidgetItem(["..."])
        placeholder.setFlags(placeholder.flags() & ~Qt.ItemIsEditable)
        item.addChild(placeholder)
        self._lazy_children[item] = (value, section_name)

    @Slot(QTreeWidgetItem)
    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        lazy_payload = self._lazy_children.pop(item, None)
        if lazy_payload is None:
            return

        value, section_name = lazy_payload
        item.takeChildren()

        if isinstance(value, dict):
            for key, child_value in value.items():
                item.addChild(self._build_item(key, child_value, section_name=section_name))
        elif isinstance(value, (list, tuple)):
            for index, child_value in enumerate(value):
                item.addChild(self._build_item(index, child_value, section_name=section_name))

        self._remember_item_texts_recursive(item)

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
        # Never override the section header icons.
        if item in self._root_sections.values():
            return

        base = _icon(self._item_base_icon_name(item))
        if base.isNull():
            return

        size = 18
        # Neutral icon tint.
        ico = _tinted_icon(base, color=self._text_color, size=size)

        # HISTORY root items: show date badge instead of dot marker
        badge = self._item_badge.get(item, "")
        if badge:
            ico = _icon_with_badge_text(
                ico,
                text=badge,
                badge_color=self._accent_color,
                text_color="#111111",
                size=size,
            )
        else:
            ico = _icon_with_marker(ico, marker_color=self._accent_color, size=size)
        item.setIcon(0, ico)

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
        self._update_root_section_header_styles()
        self._update_root_section_icons()
        self._refresh_item_icons_recursive()
    
    def _apply_stylesheet(self) -> None:
        """Apply the current colors to the QTreeWidget stylesheet."""
        self.setStyleSheet(
            self._style_template.format(
                text_color=self._text_color,
                bg_color=self._bg_color,
                branch_color=self._branch_color,
            )
            + SCROLLBAR_HOVER_ONLY_DARK
        )

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
        root_item = self._build_item("root", data, section_name=None)
        # move children of artificial root to top level
        while root_item.childCount():
            self.addTopLevelItem(root_item.takeChild(0))
        self.expandToDepth(0)

    def _build_item(self, key: str | int, value: Any, *, section_name: str | None = None) -> QTreeWidgetItem:
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
                label = f"{key_label} {{...}}"

            item = QTreeWidgetItem([label])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self._item_kind[item] = "dict"
            if self._should_lazy_load_children(section_name, value):
                self._add_lazy_placeholder(item, value, section_name)
            else:
                for k, v in value.items():
                    item.addChild(self._build_item(k, v, section_name=section_name))
        elif isinstance(value, (list, tuple)):
            item = QTreeWidgetItem([f"{key_label} [{len(value)}]"])
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            self._item_kind[item] = "list"
            if self._should_lazy_load_children(section_name, value):
                self._add_lazy_placeholder(item, value, section_name)
            else:
                for i, v in enumerate(value):
                    item.addChild(self._build_item(i, v, section_name=section_name))
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

        # Update early so repeated non-text itemChanged events don't re-enter.
        self._item_last_text[item] = new_text
        
        # Check if this item belongs to a section
        section_name = self._item_to_section.get(item)
        if section_name is None:
            # Try to find section by walking up the tree
            parent = item.parent()
            while parent is not None:
                for sect_name, sect_item in self._root_sections.items():
                    if sect_item == parent:
                        section_name = sect_name
                        break
                if section_name:
                    break
                parent = parent.parent()
        
        if section_name is None or section_name not in self._data:
            return

        def extract_key(text: str) -> str:
            if ": " in text:
                return text.split(": ", 1)[0].strip()
            if text.endswith(" {...}"):
                return text[:-5].strip()
            if text.endswith(" [") and text.rsplit(" ", 1)[-1].endswith("]"):
                # fallback, although current formatter won't hit this
                return text.rsplit(" ", 1)[0].strip()
            if " [" in text and text.endswith("]"):
                return text.rsplit(" ", 1)[0].strip()
            return text.strip()

        def coerce_key(segment: str, container: Any) -> Any:
            if isinstance(container, (list, tuple)) and segment.startswith("[") and segment.endswith("]"):
                inner = segment[1:-1]
                if inner.isdigit():
                    return int(inner)
            return segment

        def parse_value(text: str) -> Any:
            try:
                return json.loads(text)
            except Exception:
                return text

        # Build the path from root to the edited item (skip section root)
        path_segments: list[str] = []
        current = item
        section_root = self._root_sections.get(section_name)
        
        while current is not None and current != section_root:
            path_segments.append(extract_key(current.text(column)))
            current = current.parent()
        path_segments.reverse()
        
        if not path_segments:
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

        if ": " not in new_text:
            return

        key_part, value_part = new_text.split(": ", 1)
        key_part = key_part.strip()
        value_part = value_part.strip()
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
        
        # Persist changes to disk after successful edit
        self._save_data()
    
    def _initialize_root_sections(self) -> None:
        """Initialize default root sections like VS Code Explorer."""
        for section_name, collapsed in self._DEFAULT_ROOT_SECTION_LAYOUT:
            self._add_root_section(section_name, collapsed=collapsed)
            self._data[section_name] = {}
        
        # Load previously saved data if available
        self._load_data()

        # Ensure root headers match current theme settings.
        self._update_root_section_header_styles()
        self._update_section_item_font_sizes()

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

        # Accent-colored root icon + accent marker
        icon_name = self._root_section_icon_name(name)
        if icon_name:
            base = _icon(icon_name)
            if not base.isNull():
                ico = _icon_with_marker(_tinted_icon(base, color=self._accent_color, size=16), marker_color=self._accent_color, size=16)
                section.setIcon(0, ico)
        
        self.addTopLevelItem(section)
        self._root_sections[name] = section
        
        if not collapsed:
            section.setExpanded(True)
        
        return section

    def _update_root_section_icons(self) -> None:
        for name, section in self._root_sections.items():
            icon_name = self._root_section_icon_name(name)
            if not icon_name:
                continue
            base = _icon(icon_name)
            if base.isNull():
                continue
            ico = _icon_with_marker(_tinted_icon(base, color=self._accent_color, size=16), marker_color=self._accent_color, size=16)
            section.setIcon(0, ico)
    
    def add_to_section(self, section_name: str, key: str, value: Any, *, persist: bool = True) -> None:
        """Add data to a specific section (e.g., 'PROJECTS', 'DATABASES').

        Set persist=False for derived/ephemeral views (e.g. ChatHistory preview)
        to avoid bloating AppData/tree_data.json.
        """
        section = self._root_sections.get(section_name)
        if section is None:
            section = self._add_root_section(section_name)
            self._data[section_name] = {}
        
        # Store the data in our internal structure
        if section_name not in self._data:
            self._data[section_name] = {}
        self._data[section_name][key] = value
        
        item = self._build_item(key, value, section_name=section_name)
        section.addChild(item)

        # Remember baseline text for edit detection.
        self._remember_item_texts_recursive(item)

        # Ensure consistent font sizing for sections that use smaller typography.
        if section_name.upper() in self._SMALL_FONT_SECTION_NAMES:
            self._update_section_item_font_sizes()

        if section_name.upper() in self._HISTORY_SECTION_NAMES:
            # Force history semantics for icon selection.
            self._item_kind[item] = "history"
            badge = self._extract_history_badge(value)
            if badge:
                self._item_badge[item] = badge
            self._apply_item_icon(item)
        
        # Track this item's section and key
        self._item_to_section[item] = section_name
        self._item_to_key[item] = key
        
        section.setExpanded(True)
        
        # Save after adding (unless this is a derived/ephemeral view)
        if persist:
            self._save_data()
    
    def remove_from_section(self, section_name: str, item_name: str) -> bool:
        """Remove an item from a section by name."""
        section = self._root_sections.get(section_name)
        if section is None:
            return False
        
        for i in range(section.childCount()):
            child = section.child(i)
            if child and item_name in child.text(0):
                section.removeChild(child)
                
                # Remove from data structure
                if section_name in self._data and item_name in self._data[section_name]:
                    del self._data[section_name][item_name]
                
                # Remove from tracking dicts
                if child in self._item_to_section:
                    del self._item_to_section[child]
                if child in self._item_to_key:
                    del self._item_to_key[child]

                if child in self._item_last_text:
                    del self._item_last_text[child]
                
                self._save_data()
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
    
    def _save_data(self) -> None:
        """Save the current data structure to the configured storage backend."""
        try:
            try:
                env_path = self._persistence_service.persist_env_projection_from_tree_data(self._data)
                if env_path is not None:
                    print(f"[INFO] ENV projection written to {env_path}")
            except Exception as env_exc:
                print(f"[WARNING] Could not write ENV projection: {env_exc}")

            payload = json.dumps(self._data, ensure_ascii=False, sort_keys=True)
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if self._last_saved_hash == payload_hash:
                return

            backend_name, target = self._persistence_service.save_data(self._data)
            self._last_saved_hash = payload_hash
            print(f"[INFO] Tree data saved to {backend_name}:{target}")
        except Exception as e:
            print(f"[WARNING] Could not save tree data: {e}")
    
    def _load_data(self) -> None:
        """Load the data structure from the configured storage backend."""
        try:
            loaded_data, backend_name, source = self._persistence_service.load_data()
            if isinstance(loaded_data, dict) and loaded_data:

                # Restore internal data first so edits persist correctly.
                for section_name, section_data in loaded_data.items():
                    if isinstance(section_data, dict):
                        self._data[section_name] = section_data

                # Snapshot hash so we don't immediately re-save identical content.
                payload = json.dumps(self._data, ensure_ascii=False, sort_keys=True)
                self._last_saved_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    
                # Restore all sections
                self.blockSignals(True)
                for section_name, section_data in loaded_data.items():
                    if section_name not in self._root_sections:
                        self._add_root_section(section_name)
                    
                    if isinstance(section_data, dict):
                        for key, value in section_data.items():
                            # Don't call add_to_section during load to avoid saving again
                            section = self._root_sections.get(section_name)
                            if section:
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
                self.blockSignals(False)
                print(f"[INFO] Tree data loaded from {backend_name}:{source}")
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

        self.add_to_section(section_name, key_name, imported_data)
        if callable(sync_parser_result_to_agentsdb_knowledge):
            try:
                serialized_payload = json.dumps(imported_data, ensure_ascii=False, default=str)
            except Exception:
                serialized_payload = str(imported_data)
            sync_parser_result_to_agentsdb_knowledge(
                object_name="documents",
                correlation_id=f"tree-import:{hashlib.sha256(f'{file_path}:{key_name}:{section_name}'.encode('utf-8')).hexdigest()[:24]}",
                result_payload={
                    "agent": "data_tree_import",
                    "file": {
                        "source_path": str(file_path),
                        "name": Path(file_path).name,
                        "import_format": str(import_format),
                    },
                    "parse": {
                        "raw_text": serialized_payload,
                    },
                    "document": {
                        "title": str(key_name),
                        "summary": f"Data tree import from {Path(file_path).name}",
                        "section": str(section_name),
                        "import_format": str(import_format),
                        "payload": imported_data,
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
            
            # Rename
            rename_action = QAction("✏️ Rename", self)
            rename_action.triggered.connect(lambda: self.editItem(item, 0))
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
    
    def _context_add_item(self, section_name: str) -> None:
        """Add new item to section via context menu."""
        from PySide6.QtWidgets import QInputDialog
        
        key, ok = QInputDialog.getText(self, "New Item", f"Enter name for new item in {section_name}:")
        if ok and key:
            self.add_to_section(section_name, key, {"value": ""})
    
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
                self._save_data()
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
        self.add_to_section("CHAT_HISTORY", "Chat History", history, persist=True)


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
QScrollBar:vertical   { width: 4px;  }
QScrollBar:horizontal { height:50px;  }

/* grow a bit + colour when mouse enters the bar itself          */
QScrollBar:vertical:hover   { width: 4px; }
QScrollBar:horizontal:hover { height:50px; }

/* ----- handle (the draggable knob) --------------------------- */
QScrollBar::handle {
    background: rgba(120,120,120,0.0);   /* transparent while idle  */
    border-radius: 4px;
    min-width: 4px;
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
