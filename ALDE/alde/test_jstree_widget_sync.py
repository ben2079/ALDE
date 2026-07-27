import os
import socket
import threading
import time
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from alde.agents_db import (
    AgentDbSocketRepository,
    AgentDbSocketServerService,
    load_agentsdb_runtime_config_from_env,
    _AgentDbSocketRequestHandler,
    _AgentDbSocketTCPServer,
)
from alde.jstree_widget import JsonTreeWidget, TreeDataPersistenceService


class _FakeRepository:
    _OBJECT_COLLECTION_MAP: dict[str, str] = {}

    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.object_records: dict[str, dict[str, dict[str, object]]] = {}

    @contextmanager
    def deferred_write_queue(self):
        yield

    def upsert_object(self, object_name: str, object_id: str, object_payload: dict[str, object]) -> dict[str, object]:
        payload = dict(object_payload)
        self.records[object_id] = payload
        collection = self.object_records.setdefault(str(object_name), {})
        collection[str(object_id)] = dict(payload)
        return dict(payload)

    def delete_object(self, object_name: str, object_id: str) -> bool:
        self.records.pop(str(object_id), None)
        collection = self.object_records.setdefault(str(object_name), {})
        deleted = str(object_id) in collection
        collection.pop(str(object_id), None)
        return deleted

    def load_object(self, object_name: str, object_id: str) -> dict[str, object] | None:
        payload = self.records.get(object_id)
        if not isinstance(payload, dict):
            payload = self.object_records.get(str(object_name), {}).get(str(object_id))
        return dict(payload) if isinstance(payload, dict) else None

    def load_objects(self, object_name: str, object_filter: dict[str, object] | None = None, limit: int = 50) -> list[dict[str, object]]:
        collection = self.object_records.get(str(object_name), {})
        filter_payload = dict(object_filter or {})
        loaded_payload_list: list[dict[str, object]] = []
        for object_payload in collection.values():
            if not isinstance(object_payload, dict):
                continue
            if any(object_payload.get(key) != value for key, value in filter_payload.items()):
                continue
            loaded_payload_list.append(dict(object_payload))
            if len(loaded_payload_list) >= max(1, int(limit)):
                break
        return loaded_payload_list


def _load_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return int(probe_socket.getsockname()[1])


def _start_agentsdb_socket_server(tmp_path: Path) -> tuple[_AgentDbSocketTCPServer, threading.Thread, int]:
    port = _load_free_local_port()
    server = _AgentDbSocketTCPServer(
        ("127.0.0.1", port),
        _AgentDbSocketRequestHandler,
        AgentDbSocketServerService(
            backend_uri="agentsmem://local",
            default_database_name="alde_knowledge",
            memory_image_path=str(tmp_path / "agentsdb-stream.json"),
        ),
    )
    server_thread = threading.Thread(target=server.serve_forever, name=f"agentsdb-stream-test:{port}", daemon=True)
    server_thread.start()
    return server, server_thread, port


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def tree_persistence_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_IDE_TREE_MEMORY_ONLY", "0")


def test_tree_data_persistence_service_writes_stream_head_and_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "1")
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    backend_name, target = service.save_data(
        {"PROJECTS": {"Demo": {"path": "/tmp/demo"}}},
        change_event={"action": "upsert", "section_name": "PROJECTS", "item_key": "Demo"},
    )

    assert backend_name == "agents_db"
    assert target == service._tree_object_id()

    stream_head = repository.records[service._tree_stream_head_object_id()]
    event_id = str(stream_head.get("event_id") or "")
    tree_record = repository.records[service._tree_object_id()]
    stream_event = repository.records[service._tree_stream_event_object_id(event_id)]

    assert event_id
    assert tree_record["last_stream_event_id"] == event_id
    assert tree_record["content_sha256"] == stream_head["tree_hash"]
    assert stream_event["change"]["section_name"] == "PROJECTS"
    assert stream_event["change"]["item_key"] == "Demo"


def test_tree_data_persistence_service_load_live_update_uses_stream_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "1")
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    service.save_data(
        {"PROJECTS": {"Remote": {"path": "/tmp/remote"}}},
        change_event={"action": "bootstrap", "origin": "json"},
    )
    previous_cursor = {"event_id": "stale", "updated_at": "", "tree_hash": ""}

    live_payload, live_cursor = service.load_live_update(previous_cursor=previous_cursor)
    assert isinstance(live_payload, dict)
    assert live_payload["PROJECTS"]["Remote"]["path"] == "/tmp/remote"
    assert isinstance(live_cursor, dict)
    assert live_cursor["event_id"] == service.load_last_stream_cursor()["event_id"]

    unchanged_payload, unchanged_cursor = service.load_live_update(previous_cursor=live_cursor)
    assert unchanged_payload is None
    assert unchanged_cursor == live_cursor


def test_tree_data_persistence_service_normalizes_agentdb_socket_uri_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = TreeDataPersistenceService(tmp_path)
    runtime_config = SimpleNamespace(
        agents_db_uri="agentdb::localhost:::",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    socket_calls: list[tuple[str, str]] = []
    knowledge_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "alde.jstree_widget.load_agentsdb_runtime_config_from_env",
        lambda: runtime_config,
    )
    monkeypatch.setattr(
        "alde.jstree_widget.AgentDbSocketRepository",
        SimpleNamespace(
            create_from_uri=lambda uri, database_name: socket_calls.append((uri, database_name)) or {"uri": uri, "database_name": database_name}
        ),
    )
    monkeypatch.setattr(
        "alde.jstree_widget.KnowledgeRepository",
        SimpleNamespace(
            create_from_uri=lambda uri, database_name: knowledge_calls.append((uri, database_name)) or {"uri": uri, "database_name": database_name}
        ),
    )

    _, repository = service._load_agentsdb_repository()

    assert runtime_config.agents_db_uri == "agentsdb://127.0.0.1:2331"
    assert socket_calls == [("agentsdb://127.0.0.1:2331", "alde_knowledge")]
    assert knowledge_calls == []
    assert repository == {"uri": "agentsdb://127.0.0.1:2331", "database_name": "alde_knowledge"}


def test_tree_data_persistence_service_normalizes_malformed_agentsdb_ipv6_loopback_uri(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = TreeDataPersistenceService(tmp_path)
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://:::1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    socket_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "alde.jstree_widget.load_agentsdb_runtime_config_from_env",
        lambda: runtime_config,
    )
    monkeypatch.setattr(
        "alde.jstree_widget.AgentDbSocketRepository",
        SimpleNamespace(
            create_from_uri=lambda uri, database_name: socket_calls.append((uri, database_name)) or {"uri": uri, "database_name": database_name}
        ),
    )

    _, repository = service._load_agentsdb_repository()

    assert runtime_config.agents_db_uri == "agentsdb://127.0.0.1:2331"
    assert socket_calls == [("agentsdb://127.0.0.1:2331", "alde_knowledge")]
    assert repository == {"uri": "agentsdb://127.0.0.1:2331", "database_name": "alde_knowledge"}


def test_load_agentsdb_runtime_config_from_env_honors_connection_config_database_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "agentsdb_connection.json"
    config_path.write_text(
        json.dumps(
            {
                "agents_db_uri": "agentsdb://:::1:2331",
                "backend_uri": "agentsmem://local",
                "database_name": "custom_review_db",
                "namespace_id": "ns_review",
                "index_backend": "faiss",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_IDE_KNOWLEDGE_AGENTS_DB_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("AI_IDE_KNOWLEDGE_AGENTS_DB_URI", raising=False)
    monkeypatch.delenv("AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI", raising=False)
    monkeypatch.delenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAME", raising=False)
    monkeypatch.delenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_ID", raising=False)
    monkeypatch.setattr("alde.agents_db._AGENTSDB_CONNECTION_CONFIG_CACHE", None, raising=False)

    runtime_config = load_agentsdb_runtime_config_from_env()

    assert runtime_config is not None
    assert runtime_config.agents_db_uri == "agentsdb://127.0.0.1:2331"
    assert runtime_config.database_name == "custom_review_db"
    assert runtime_config.namespace_id == "ns_review"


def test_tree_data_persistence_service_defaults_to_memory_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (None, None))

    backend_name, target = service.save_data(
        {"PROJECTS": {"Demo": {"path": "/tmp/demo"}}},
        change_event={"action": "upsert", "section_name": "PROJECTS", "item_key": "Demo"},
    )
    loaded_data, loaded_backend, source = service.load_data()

    assert backend_name in {"agents_db", "memory", "json"}
    assert target == "inmemory"
    assert loaded_backend == "memory"
    assert source == "inmemory"
    assert loaded_data["PROJECTS"]["Demo"]["path"] == "/tmp/demo"
    assert service.live_sync_enabled() is False
    assert service.supports_push_stream() is False


def test_tree_data_persistence_service_projects_gui_env_json_into_env_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_IDE_TREE_MEMORY_ONLY", "0")
    service = TreeDataPersistenceService(tmp_path)
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (None, None))

    gui_env_path = tmp_path / "gui_env.json"
    gui_env_path.write_text(
        json.dumps(
            {
                "format": "ai_ide_gui_env_v1",
                "env": {
                    "AI_IDE_CONTROL_PLANE_REFRESH_MS": "0",
                    "AI_IDE_AGENTS_DB_TREE_POLL_MS": "5000",
                },
            }
        ),
        encoding="utf-8",
    )

    loaded_data, backend_name, source = service.load_data()

    assert backend_name in {"agents_db", "memory", "json"}
    assert source == "inmemory"
    assert loaded_data["ENV"]["gui_env.json"]["format"] == "ai_ide_gui_env_v1"
    assert loaded_data["ENV"]["gui_env.json"]["env"]["AI_IDE_CONTROL_PLANE_REFRESH_MS"] == "0"
    assert loaded_data["ENV"]["gui_env.json"]["env"]["AI_IDE_AGENTS_DB_TREE_POLL_MS"] == "5000"


def test_tree_data_persistence_service_loads_projection_sources_from_env_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_IDE_TREE_MEMORY_ONLY", "0")
    monkeypatch.setenv("AI_IDE_TREE_SECTION_ALLOWLIST", "DATABASES")

    source_payload_path = tmp_path / "tree_projection_sources.json"
    local_projection_path = tmp_path / "custom_projection.json"
    local_projection_path.write_text(json.dumps({"name": "custom-db", "ok": True}), encoding="utf-8")
    source_payload_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "section": "DATABASES",
                        "key": "custom_projection",
                        "kind": "json_file",
                        "file_path": str(local_projection_path),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_IDE_TREE_PROJECTION_SOURCES_PATH", str(source_payload_path))

    service = TreeDataPersistenceService(tmp_path)
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (None, None))

    loaded_data, backend_name, source = service.load_data()

    assert backend_name in {"agents_db", "memory", "json"}
    assert source == "inmemory"
    assert loaded_data["DATABASES"]["custom_projection"]["name"] == "custom-db"
    assert loaded_data["DATABASES"]["custom_projection"]["ok"] is True


def test_tree_data_persistence_service_env_projection_parses_json_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_IDE_TREE_MEMORY_ONLY", "0")
    monkeypatch.setenv("AI_IDE_TREE_SECTION_ALLOWLIST", "ENV")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "AI_IDE_TREE_PROJECTION_SOURCES_JSON={\"sources\":[{\"section\":\"DATABASES\",\"key\":\"k\",\"kind\":\"json_file\",\"file_path\":\"AppData/x.json\"}] }\n",
        encoding="utf-8",
    )

    service = TreeDataPersistenceService(tmp_path)
    env_projection = service._load_env_projection_object(env_path)

    assert isinstance(env_projection, dict)
    sections = env_projection["sections"]
    assert isinstance(sections["General"]["AI_IDE_TREE_PROJECTION_SOURCES_JSON"]["value"], dict)
    assert sections["General"]["AI_IDE_TREE_PROJECTION_SOURCES_JSON"]["value"]["sources"][0]["section"] == "DATABASES"


def test_tree_data_persistence_service_loads_agentsdb_query_source_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_IDE_TREE_MEMORY_ONLY", "0")
    monkeypatch.setenv("AI_IDE_TREE_SECTION_ALLOWLIST", "DATABASES")
    monkeypatch.setenv(
        "AI_IDE_TREE_PROJECTION_SOURCES_JSON",
        json.dumps(
            {
                "sources": [
                    {
                        "section": "DATABASES",
                        "key": "documents_recent",
                        "kind": "agentsdb_query",
                        "object_name": "document",
                        "filter": {"namespace_id": "ns_alde_default"},
                        "fields": ["_id", "title", "updated_at"],
                        "limit": 10,
                    }
                ]
            }
        ),
    )

    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "namespace_id": "ns_alde_default",
                "title": "Doc One",
                "updated_at": "2026-05-16T00:00:00+00:00",
                "content": "A",
            },
            "doc-2": {
                "_id": "doc-2",
                "namespace_id": "ns_other",
                "title": "Doc Two",
                "updated_at": "2026-05-17T00:00:00+00:00",
                "content": "B",
            },
        }
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    loaded_data, backend_name, _source = service.load_data()

    query_payload = loaded_data["DATABASES"]["documents_recent"]
    assert backend_name in {"agents_db", "memory", "json"}
    assert query_payload["_meta"]["source_of_truth"] == "agentsdb_query"
    assert query_payload["_meta"]["record_count"] == 1
    assert query_payload["records"][0]["_id"] == "doc-1"
    assert query_payload["records"][0]["title"] == "Doc One"
    assert "content" not in query_payload["records"][0]


def test_tree_data_persistence_service_strict_agentsdb_sources_use_db_only_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_IDE_TREE_MEMORY_ONLY", "0")
    monkeypatch.setenv("AI_IDE_TREE_SECTION_ALLOWLIST", "DATABASES")
    monkeypatch.setenv("AI_IDE_AGENTS_DB_PIPELINE_STRICT", "1")

    local_projection_path = tmp_path / "custom_projection.json"
    local_projection_path.write_text(json.dumps({"name": "local", "ok": True}), encoding="utf-8")

    monkeypatch.setenv(
        "AI_IDE_AGENTS_DB_SOURCES",
        json.dumps(
            {
                "strict": True,
                "sources": [
                    {
                        "section": "DATABASES",
                        "key": "local_projection",
                        "kind": "json_file",
                        "file_path": str(local_projection_path),
                    },
                    {
                        "section": "DATABASES",
                        "key": "documents_recent",
                        "kind": "agentsdb_query",
                        "object_name": "document",
                        "filter": {"namespace_id": "ns_alde_default"},
                        "fields": ["_id", "title", "notes", "content"],
                        "limit": 10,
                    },
                ],
                "allowlist": {
                    "fields": {
                        "document": ["_id", "title", "notes"],
                    }
                },
            }
        ),
    )

    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "namespace_id": "ns_alde_default",
                "title": "Doc One",
                "notes": "only-allowlisted",
                "content": "must-be-filtered",
            },
        }
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    loaded_data, backend_name, _source = service.load_data()

    assert backend_name in {"agents_db", "memory", "json"}
    assert "local_projection" not in loaded_data["DATABASES"]
    records = loaded_data["DATABASES"]["documents_recent"]["records"]
    assert records[0]["_id"] == "doc-1"
    assert records[0]["title"] == "Doc One"
    assert records[0]["notes"] == "only-allowlisted"
    assert "content" not in records[0]


def test_tree_data_persistence_service_memory_only_materializes_agentsdb_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        }
    }
    upsert_calls: list[tuple[str, str]] = []
    repository.upsert_object = lambda object_name, object_id, object_payload: upsert_calls.append((str(object_name), str(object_id))) or dict(object_payload)  # type: ignore[method-assign]
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    loaded_data, backend_name, source = service.load_data()

    assert backend_name == "agents_db_repository"
    assert source == "alde_knowledge"
    assert loaded_data["DATABASES"]["agentsdb_repository"]["alde_knowledge"]["documents"]["doc-1"]["title"] == "Doc One"
    assert upsert_calls == []
    assert service.live_sync_enabled() is True
    assert service.supports_push_stream() is False


def test_tree_data_persistence_service_applies_agentsdb_repository_edit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "metadata": {
                    "status": "draft",
                },
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        }
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    synced = service.apply_agentsdb_repository_edit(
        section_name="DATABASES",
        path_segments=["agentsdb_repository", "alde_knowledge", "documents", "doc-1", "title"],
        key_name="title",
        value="Doc Updated",
    )

    assert synced is True
    assert repository.object_records["document"]["doc-1"]["title"] == "Doc Updated"

    nested_synced = service.apply_agentsdb_repository_edit(
        section_name="DATABASES",
        path_segments=["agentsdb_repository", "alde_knowledge", "documents", "doc-1", "metadata", "status"],
        key_name="status",
        value="published",
    )

    assert nested_synced is True
    assert repository.object_records["document"]["doc-1"]["metadata"]["status"] == "published"


def test_tree_data_persistence_service_deletes_agentsdb_repository_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "metadata": {
                    "status": "draft",
                },
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        }
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    removed_field = service.delete_agentsdb_repository_path(
        section_name="DATABASES",
        path_segments=["agentsdb_repository", "alde_knowledge", "documents", "doc-1", "metadata", "status"],
    )
    assert removed_field is True
    assert "status" not in repository.object_records["document"]["doc-1"]["metadata"]

    removed_record = service.delete_agentsdb_repository_path(
        section_name="DATABASES",
        path_segments=["agentsdb_repository", "alde_knowledge", "documents", "doc-1"],
    )
    assert removed_record is True
    assert "doc-1" not in repository.object_records["document"]


def test_tree_data_persistence_service_creates_agentsdb_repository_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    created = service.create_agentsdb_repository_record(
        section_name="DATABASES",
        path_segments=["agentsdb_repository", "alde_knowledge", "documents"],
        record_id="doc-2",
        record_payload={"title": "Doc Two"},
    )

    assert created is True
    assert repository.object_records["document"]["doc-2"]["_id"] == "doc-2"
    assert repository.object_records["document"]["doc-2"]["title"] == "Doc Two"
    assert repository.object_records["document"]["doc-2"]["updated_at"]

    duplicate = service.create_agentsdb_repository_record(
        section_name="DATABASES",
        path_segments=["agentsdb_repository", "alde_knowledge", "documents"],
        record_id="doc-2",
    )

    assert duplicate is False


def test_tree_data_persistence_service_projects_repository_keys_into_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "1")
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
        "entity": "entities",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "document_type": "note",
                "namespace_id": "ns_alde_default",
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        },
        "entity": {
            "ent-1": {
                "_id": "ent-1",
                "canonical_name": "Python",
                "entity_type": "skill",
                "updated_at": "2026-05-15T00:00:01+00:00",
            }
        },
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    loaded_data, backend_name, source = service.load_data()

    repository_payload = loaded_data["DATABASES"]["agentsdb_repository"]
    assert backend_name == "agents_db_repository"
    assert source == "alde_knowledge"
    assert repository_payload["alde_knowledge"]["documents"]["doc-1"]["title"] == "Doc One"
    assert repository_payload["alde_knowledge"]["entities"]["ent-1"]["canonical_name"] == "Python"


def test_tree_data_persistence_service_live_sync_polls_repository_projection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "1")
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        }
    }
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    initial_payload, initial_cursor = service.load_live_update(previous_cursor=None)
    assert isinstance(initial_payload, dict)
    assert isinstance(initial_cursor, dict)
    assert initial_payload["DATABASES"]["agentsdb_repository"]["alde_knowledge"]["documents"]["doc-1"]["title"] == "Doc One"

    unchanged_payload, unchanged_cursor = service.load_live_update(previous_cursor=initial_cursor)
    assert unchanged_payload is None
    assert unchanged_cursor == initial_cursor

    repository.object_records["document"]["doc-2"] = {
        "_id": "doc-2",
        "title": "Doc Two",
        "updated_at": "2026-05-15T00:00:02+00:00",
    }

    updated_payload, updated_cursor = service.load_live_update(previous_cursor=initial_cursor)
    assert isinstance(updated_payload, dict)
    assert isinstance(updated_cursor, dict)
    assert updated_payload["DATABASES"]["agentsdb_repository"]["alde_knowledge"]["documents"]["doc-2"]["title"] == "Doc Two"
    assert updated_cursor["tree_hash"] != initial_cursor["tree_hash"]


def test_tree_data_persistence_service_disables_push_stream_for_repository_projection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "1")
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.subscribe_tree_stream = lambda *args, **kwargs: iter(())  # type: ignore[attr-defined]
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    assert service.supports_push_stream() is False


def test_tree_data_persistence_service_supports_push_stream_for_repository_projection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.subscribe_repository_stream = lambda **kwargs: iter(())  # type: ignore[attr-defined]
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    assert service.supports_push_stream() is True


def test_tree_data_persistence_service_stream_live_updates_uses_repository_push_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        }
    }

    def subscribe_repository_stream(**kwargs):
        repository.object_records["document"]["doc-2"] = {
            "_id": "doc-2",
            "title": "Doc Two",
            "updated_at": "2026-05-15T00:00:01+00:00",
        }
        yield {
            "ok": True,
            "stream": "repository_update",
            "database_name": "alde_knowledge",
            "object_name": "document",
            "object_id": "doc-2",
            "stream_cursor": {
                "event_id": "evt-push-1",
                "updated_at": "2026-05-15T00:00:01+00:00",
            },
        }

    repository.subscribe_repository_stream = subscribe_repository_stream  # type: ignore[attr-defined]
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    loaded_data, stream_cursor = next(iter(service.stream_live_updates(previous_cursor=None)))

    assert loaded_data["DATABASES"]["agentsdb_repository"]["alde_knowledge"]["documents"]["doc-2"]["title"] == "Doc Two"
    assert isinstance(stream_cursor, dict)
    assert stream_cursor["event_id"] == "evt-push-1"
    assert stream_cursor["tree_hash"]


def test_agentsdb_socket_repository_subscribe_tree_stream_receives_push_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "1")
    socket_server, server_thread, port = _start_agentsdb_socket_server(tmp_path)
    try:
        agents_db_uri = f"agentsdb://127.0.0.1:{port}"
        repository = AgentDbSocketRepository(agents_db_uri, database_name="alde_knowledge", timeout_seconds=1.0)
        persistence_service = TreeDataPersistenceService(tmp_path)
        runtime_config = SimpleNamespace(
            agents_db_uri=agents_db_uri,
            database_name="alde_knowledge",
            namespace_id="ns_alde_default",
        )
        monkeypatch.setattr(persistence_service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

        persistence_service.save_data(
            {"PROJECTS": {"Initial": {"path": "/tmp/initial"}}},
            change_event={"action": "bootstrap", "origin": "test"},
        )
        initial_cursor = persistence_service.load_last_stream_cursor()

        stop_event = threading.Event()
        received_messages: list[dict[str, object]] = []

        def consume_stream() -> None:
            for response_payload in repository.subscribe_tree_stream(
                persistence_service._tree_object_id(),
                last_event_id=str((initial_cursor or {}).get("event_id") or "") or None,
                stop_event=stop_event,
                heartbeat_seconds=1.0,
            ):
                received_messages.append(response_payload)
                break

        consumer_thread = threading.Thread(target=consume_stream, name="tree-stream-consumer", daemon=True)
        consumer_thread.start()

        persistence_service.save_data(
            {
                "PROJECTS": {
                    "Initial": {"path": "/tmp/initial"},
                    "Pushed": {"path": "/tmp/pushed"},
                }
            },
            change_event={"action": "upsert", "section_name": "PROJECTS", "item_key": "Pushed"},
        )

        deadline = time.time() + 3.0
        while time.time() < deadline and not received_messages:
            time.sleep(0.05)

        stop_event.set()
        consumer_thread.join(timeout=2.0)

        assert received_messages
        received_payload = received_messages[0]
        assert received_payload["tree_data"]["PROJECTS"]["Pushed"]["path"] == "/tmp/pushed"
        assert received_payload["change"]["item_key"] == "Pushed"
        assert received_payload["stream_cursor"]["event_id"]
    finally:
        socket_server.shutdown()
        socket_server.server_close()
        server_thread.join(timeout=2.0)


def test_agentsdb_socket_repository_normalizes_malformed_ipv6_loopback_uri() -> None:
    repository = AgentDbSocketRepository("agentsdb://:::1:2331", database_name="alde_knowledge", timeout_seconds=1.0)

    assert repository._agents_db_uri == "agentsdb://127.0.0.1:2331"
    assert repository._host == "127.0.0.1"
    assert repository._port == 2331


def test_agentsdb_socket_repository_subscribe_repository_stream_receives_push_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    socket_server, server_thread, port = _start_agentsdb_socket_server(tmp_path)
    try:
        agents_db_uri = f"agentsdb://127.0.0.1:{port}"
        repository = AgentDbSocketRepository(agents_db_uri, database_name="alde_knowledge", timeout_seconds=1.0)

        stop_event = threading.Event()
        received_messages: list[dict[str, object]] = []

        def consume_stream() -> None:
            for response_payload in repository.subscribe_repository_stream(
                stop_event=stop_event,
                heartbeat_seconds=1.0,
            ):
                received_messages.append(response_payload)
                break

        consumer_thread = threading.Thread(target=consume_stream, name="repository-stream-consumer", daemon=True)
        consumer_thread.start()

        repository.upsert_object(
            "document",
            "doc-1",
            {
                "_id": "doc-1",
                "title": "Doc One",
                "updated_at": "2026-05-15T00:00:00+00:00",
            },
        )

        deadline = time.time() + 3.0
        while time.time() < deadline and not received_messages:
            time.sleep(0.05)

        stop_event.set()
        consumer_thread.join(timeout=2.0)

        assert received_messages
        received_payload = received_messages[0]
        assert received_payload["object_name"] == "document"
        assert received_payload["object_id"] == "doc-1"
        assert received_payload["change"]["action"] == "upsert"
        assert received_payload["stream_cursor"]["event_id"]
    finally:
        socket_server.shutdown()
        socket_server.server_close()
        server_thread.join(timeout=2.0)


def test_agentsdb_socket_repository_subscribe_repository_stream_include_meta_yields_subscription_ack(
    tmp_path: Path,
) -> None:
    socket_server, server_thread, port = _start_agentsdb_socket_server(tmp_path)
    try:
        agents_db_uri = f"agentsdb://127.0.0.1:{port}"
        repository = AgentDbSocketRepository(agents_db_uri, database_name="alde_knowledge", timeout_seconds=1.0)

        stop_event = threading.Event()
        received_messages: list[dict[str, object]] = []

        def consume_stream() -> None:
            for response_payload in repository.subscribe_repository_stream(
                stop_event=stop_event,
                heartbeat_seconds=1.0,
                include_meta=True,
            ):
                received_messages.append(response_payload)
                break

        consumer_thread = threading.Thread(target=consume_stream, name="repository-stream-meta-consumer", daemon=True)
        consumer_thread.start()

        deadline = time.time() + 3.0
        while time.time() < deadline and not received_messages:
            time.sleep(0.05)

        stop_event.set()
        consumer_thread.join(timeout=2.0)

        assert received_messages
        received_payload = received_messages[0]
        assert received_payload["subscribed"] is True
        assert received_payload["stream"] == "repository_subscription"
    finally:
        socket_server.shutdown()
        socket_server.server_close()
        server_thread.join(timeout=2.0)


def test_agentsdb_repository_push_stream_reports_subscription_status_to_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    service = TreeDataPersistenceService(tmp_path)
    repository = _FakeRepository()
    repository._OBJECT_COLLECTION_MAP = {
        "document": "documents",
    }
    repository.object_records = {
        "document": {
            "doc-1": {
                "_id": "doc-1",
                "title": "Doc One",
                "updated_at": "2026-05-15T00:00:00+00:00",
            }
        }
    }

    def subscribe_repository_stream(**kwargs):
        assert kwargs["include_meta"] is True
        yield {
            "ok": True,
            "subscribed": True,
            "stream": "repository_subscription",
            "database_name": "alde_knowledge",
        }
        repository.object_records["document"]["doc-2"] = {
            "_id": "doc-2",
            "title": "Doc Two",
            "updated_at": "2026-05-15T00:00:01+00:00",
        }
        yield {
            "ok": True,
            "stream": "repository_update",
            "database_name": "alde_knowledge",
            "object_name": "document",
            "object_id": "doc-2",
            "stream_cursor": {
                "event_id": "evt-push-1",
                "updated_at": "2026-05-15T00:00:01+00:00",
            },
        }

    repository.subscribe_repository_stream = subscribe_repository_stream  # type: ignore[attr-defined]
    runtime_config = SimpleNamespace(
        agents_db_uri="agentsdb://127.0.0.1:2331",
        database_name="alde_knowledge",
        namespace_id="ns_alde_default",
    )
    monkeypatch.setattr(service, "_load_agentsdb_repository", lambda: (runtime_config, repository))

    status_messages: list[dict[str, object]] = []
    loaded_data, stream_cursor = next(
        iter(service.stream_live_updates(previous_cursor=None, status_callback=status_messages.append))
    )

    assert status_messages == [
        {
            "ok": True,
            "subscribed": True,
            "stream": "repository_subscription",
            "database_name": "alde_knowledge",
        }
    ]
    assert loaded_data["DATABASES"]["agentsdb_repository"]["alde_knowledge"]["documents"]["doc-2"]["title"] == "Doc Two"
    assert isinstance(stream_cursor, dict)
    assert stream_cursor["event_id"] == "evt-push-1"


def test_agentsdb_socket_repository_query_returns_multimodel_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AI_IDE_TREE_MEMORY_ONLY", raising=False)
    socket_server, server_thread, port = _start_agentsdb_socket_server(tmp_path)
    try:
        agents_db_uri = f"agentsdb://127.0.0.1:{port}"
        repository = AgentDbSocketRepository(agents_db_uri, database_name="alde_knowledge", timeout_seconds=1.0)

        repository.upsert_object(
            "document",
            "doc-query-1",
            {
                "_id": "doc-query-1",
                "namespace_id": "ns_alde_default",
                "title": "Python Retrieval Guide",
                "blocks": [
                    {
                        "block_id": "blk-query-1",
                        "heading": "Overview",
                        "content": "Python retrieval pipelines blend lexical and graph evidence.",
                    }
                ],
            },
        )
        repository.upsert_object(
            "entity",
            "ent-query-python",
            {
                "_id": "ent-query-python",
                "id": "ent-query-python",
                "namespace_id": "ns_alde_default",
                "canonical_name": "Python",
                "entity_type": "skill",
                "summary": "Python powers the retrieval pipeline.",
            },
        )
        repository.upsert_object(
            "relation",
            "rel-query-python",
            {
                "_id": "rel-query-python",
                "id": "rel-query-python",
                "namespace_id": "ns_alde_default",
                "relation_type": "supports_retrieval",
                "source_entity_id": "ent-query-python",
                "target_entity_id": "doc-query-1",
                "metadata": {
                    "relation_description": "Python supports lexical retrieval and graph enrichment.",
                },
            },
        )

        result_payload = repository.query(
            "python retrieval",
            owner_types=["block", "entity", "relation"],
            namespace_id="ns_alde_default",
            limit=6,
            use_vector=False,
        )

        owner_type_set = {str(chunk.get("owner_type") or "") for chunk in result_payload.get("chunks") or []}
        assert result_payload["ok"] is True
        assert result_payload["used_vector_search"] is False
        assert {"block", "entity", "relation"}.issubset(owner_type_set)

        alias_result_payload = repository._request_object(
            "query",
            {
                "query": "python retrieval",
                "owner_types": "relations",
                "namespace_id": "ns_alde_default",
                "limit": 6,
                "used_vector_search": "false",
            },
        )

        assert alias_result_payload["ok"] is True
        assert alias_result_payload["owner_types"] == ["relation"]
        assert alias_result_payload["request"]["use_vector"] is False
    finally:
        socket_server.shutdown()
        socket_server.server_close()
        server_thread.join(timeout=2.0)


def test_json_tree_widget_pulls_remote_agentsdb_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_SYNC", "0")
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))

    widget = JsonTreeWidget()
    service = TreeDataPersistenceService(tmp_path)
    monkeypatch.setattr(service, "live_sync_enabled", lambda: True, raising=False)
    monkeypatch.setattr(service, "load_last_stream_cursor", lambda: None, raising=False)
    monkeypatch.setattr(
        service,
        "load_live_update",
        lambda previous_cursor=None: (
            {"PROJECTS": {"Remote": {"path": "/tmp/remote"}}},
            {"event_id": "evt-1", "updated_at": "2026-05-15T00:00:00+00:00", "tree_hash": "hash-1"},
        ),
        raising=False,
    )
    widget._persistence_service = service

    widget._poll_live_tree_updates()

    assert widget._data["PROJECTS"]["Remote"]["path"] == "/tmp/remote"
    assert widget._live_sync_cursor["event_id"] == "evt-1"

    widget.deleteLater()


def test_json_tree_widget_uses_poll_when_push_runtime_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    call_order: list[str] = []
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_AUTO_SYNC_RUNTIME", "1")
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_PUSH_RUNTIME", "0")
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))
    monkeypatch.setattr(TreeDataPersistenceService, "live_sync_enabled", lambda self: True)
    monkeypatch.setattr(TreeDataPersistenceService, "supports_push_stream", lambda self: True)
    monkeypatch.setattr(TreeDataPersistenceService, "push_stream_enabled", lambda self: True)
    monkeypatch.setattr(JsonTreeWidget, "_start_live_push_stream", lambda self: call_order.append("push") or True)
    monkeypatch.setattr(JsonTreeWidget, "_start_live_sync_timer", lambda self: call_order.append("poll"))

    widget = JsonTreeWidget()

    assert call_order == ["poll"]
    widget.deleteLater()


def test_json_tree_widget_defaults_to_manual_sync_runtime(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    call_order: list[str] = []
    monkeypatch.delenv("AI_IDE_AGENTS_DB_TREE_AUTO_SYNC_RUNTIME", raising=False)
    monkeypatch.delenv("AI_IDE_AGENTS_DB_TREE_PUSH_RUNTIME", raising=False)
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))
    monkeypatch.setattr(TreeDataPersistenceService, "live_sync_enabled", lambda self: True)
    monkeypatch.setattr(TreeDataPersistenceService, "supports_push_stream", lambda self: True)
    monkeypatch.setattr(TreeDataPersistenceService, "push_stream_enabled", lambda self: True)
    monkeypatch.setattr(JsonTreeWidget, "_start_live_push_stream", lambda self: call_order.append("push") or True)
    monkeypatch.setattr(JsonTreeWidget, "_start_live_sync_timer", lambda self: call_order.append("poll"))

    widget = JsonTreeWidget()

    assert call_order == []
    diagnostic = widget.load_live_sync_diagnostic()
    assert diagnostic["transport"] == "manual"
    assert diagnostic["connection_state"] == "manual_waiting"
    assert diagnostic["auto_sync_enabled"] is False

    widget.deleteLater()


def test_json_tree_widget_uses_compact_tree_spacing(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))
    monkeypatch.setattr(TreeDataPersistenceService, "live_sync_enabled", lambda self: False)

    widget = JsonTreeWidget()

    assert widget.indentation() == JsonTreeWidget._TREE_INDENTATION
    assert widget.iconSize() == JsonTreeWidget._TREE_ICON_SIZE

    widget.deleteLater()


def test_json_tree_widget_context_adds_agentsdb_repository_record(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setattr(
        TreeDataPersistenceService,
        "load_data",
        lambda self: (
            {
                "DATABASES": {
                    "agentsdb_repository": {
                        "alde_knowledge": {
                            "documents": {},
                        }
                    }
                }
            },
            "agents_db_repository",
            "alde_knowledge",
        ),
    )
    monkeypatch.setattr(TreeDataPersistenceService, "live_sync_enabled", lambda self: False)

    widget = JsonTreeWidget()
    collection_item = widget._root_sections["DATABASES"].child(0).child(0).child(0)
    create_calls: list[tuple[str | None, list[str], str]] = []
    reload_calls: list[bool] = []

    monkeypatch.setattr(
        widget._persistence_service,
        "resolve_agentsdb_repository_collection_binding",
        lambda section_name, path_segments: {
            "collection_name": "documents",
            "object_name": "document",
        }
        if section_name == "DATABASES" and list(path_segments) == ["agentsdb_repository", "alde_knowledge", "documents"]
        else None,
        raising=False,
    )
    monkeypatch.setattr(
        widget._persistence_service,
        "create_agentsdb_repository_record",
        lambda section_name, path_segments, record_id: create_calls.append((section_name, list(path_segments), record_id)) or True,
        raising=False,
    )
    monkeypatch.setattr(widget, "_reload_tree_from_persistence", lambda log_message=False: reload_calls.append(bool(log_message)), raising=False)
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText", lambda *args, **kwargs: ("doc-3", True))
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)

    widget._context_add_agentsdb_repository_record(collection_item, "DATABASES")

    assert create_calls == [
        ("DATABASES", ["agentsdb_repository", "alde_knowledge", "documents"], "doc-3")
    ]
    assert reload_calls == [False]

    widget.deleteLater()


def test_json_tree_widget_live_sync_diagnostic_tracks_backoff_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_PUSH_BACKOFF_BASE_SECONDS", "0.5")
    monkeypatch.setenv("AI_IDE_AGENTS_DB_TREE_PUSH_BACKOFF_MAX_SECONDS", "2.0")
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))
    monkeypatch.setattr(TreeDataPersistenceService, "live_sync_enabled", lambda self: True)
    monkeypatch.setattr(TreeDataPersistenceService, "supports_push_stream", lambda self: False)
    monkeypatch.setattr(TreeDataPersistenceService, "push_stream_enabled", lambda self: True)

    widget = JsonTreeWidget()

    assert widget._compute_push_stream_backoff_seconds(1) == 0.5
    assert widget._compute_push_stream_backoff_seconds(2) == 1.0
    assert widget._compute_push_stream_backoff_seconds(4) == 2.0

    widget._record_live_sync_failure(
        "socket down",
        reconnect_attempts=3,
        backoff_seconds=2.0,
    )
    widget._record_live_sync_cursor(
        {"event_id": "evt-42", "updated_at": "2026-05-15T00:00:00+00:00"},
        update_received_at=True,
    )

    diagnostic = widget.load_live_sync_diagnostic()
    assert diagnostic["last_error"] == "socket down"
    assert diagnostic["reconnect_attempts"] == 3
    assert diagnostic["backoff_seconds"] == 2.0
    assert diagnostic["last_event_id"] == "evt-42"
    assert diagnostic["last_event_at"] == "2026-05-15T00:00:00+00:00"
    assert diagnostic["last_update_at"]

    widget.deleteLater()


def test_json_tree_widget_manual_sync_reloads_tree_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))
    widget = JsonTreeWidget()

    reload_calls: list[bool] = []
    monkeypatch.setattr(widget, "_reload_tree_from_persistence", lambda log_message=False: reload_calls.append(bool(log_message)), raising=False)

    assert widget.run_manual_sync(source_label="test_manual") is True
    assert reload_calls == [False]

    diagnostic = widget.load_live_sync_diagnostic()
    assert diagnostic["transport"] == "manual"
    assert diagnostic["connection_state"] == "manual"
    assert diagnostic["last_update_at"]

    widget.deleteLater()


def test_json_tree_widget_marks_push_stream_connected_on_subscription_status(
    monkeypatch: pytest.MonkeyPatch,
    qapp: QApplication,
) -> None:
    monkeypatch.setattr(TreeDataPersistenceService, "load_data", lambda self: ({}, "json", "stub"))
    monkeypatch.setattr(TreeDataPersistenceService, "live_sync_enabled", lambda self: True)
    monkeypatch.setattr(TreeDataPersistenceService, "supports_push_stream", lambda self: False)
    monkeypatch.setattr(TreeDataPersistenceService, "push_stream_enabled", lambda self: True)

    widget = JsonTreeWidget()
    widget._handle_push_stream_status_payload({"ok": True, "subscribed": True, "stream": "repository_subscription"})

    diagnostic = widget.load_live_sync_diagnostic()
    assert diagnostic["transport"] == "push"
    assert diagnostic["connection_state"] == "connected"
    assert diagnostic["reconnect_attempts"] == 0
    assert diagnostic["backoff_seconds"] == 0.0

    widget.deleteLater()
