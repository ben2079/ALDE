from __future__ import annotations

import json
from pathlib import Path

from alde.ai_ide_v1756 import persist_connection_selection_to_config, probe_mcp_endpoint_for_uri


class TreeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def add_to_section(self, section_name: str, key: str, value: dict[str, object]) -> None:
        self.calls.append((section_name, key, value))

    def remove_from_section(self, section_name: str, item_name: str) -> bool:
        return False


def test_persist_connection_selection_updates_config_and_tree(tmp_path: Path) -> None:
    config_path = tmp_path / "agentsdb_connection.json"
    tree_store = TreeRecorder()

    payload = persist_connection_selection_to_config(
        "agentsdb://example.test:2331",
        config_path=config_path,
        connection_name="demo_conn",
        tree_store=tree_store,
    )

    assert payload["agents_db_uri"] == "agentsdb://example.test:2331"
    assert json.loads(config_path.read_text(encoding="utf-8"))["agents_db_uri"] == "agentsdb://example.test:2331"
    assert tree_store.calls[0][0] == "DATABASES"
    assert tree_store.calls[0][1] == "demo_conn"
    assert tree_store.calls[0][2]["uri"] == "agentsdb://example.test:2331"


def test_probe_mcp_endpoint_posts_initialize_to_mcp_path() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_request(endpoint_url: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((endpoint_url, payload))
        return {"result": {"protocolVersion": "2025-03-26"}}

    result = probe_mcp_endpoint_for_uri("http://example.test:8766", request_func=fake_request)

    assert result["ok"] is True
    assert result["status_text"] == "Connection established / Health check ok"
    assert calls[0][0] == "http://example.test:8766/mcp"
    assert calls[0][1]["method"] == "initialize"
