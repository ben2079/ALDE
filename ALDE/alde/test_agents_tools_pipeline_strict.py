from __future__ import annotations

import json
from pathlib import Path

from alde import agents_tools as tools_mod


def test_request_object_resolution_service_strict_mode_blocks_inline_and_file_imports(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_PIPELINE_STRICT", "1")
    monkeypatch.setenv(
        "AI_IDE_AGENTS_DB_SOURCES",
        json.dumps(
            {
                "strict": True,
                "allowlist": {
                    "import_sources": ["profile_id"],
                },
            }
        ),
    )

    inline_result = tools_mod.REQUEST_OBJECT_RESOLUTION_SERVICE.build_result_from_request(
        {
            "source": "text",
            "value": {"profile_id": "profile-inline"},
        },
        obj_name="profiles",
    )
    assert inline_result is None

    profile_file_path = tmp_path / "profile.json"
    profile_file_path.write_text('{"profile_id": "profile-file"}', encoding="utf-8")
    file_result = tools_mod.REQUEST_OBJECT_RESOLUTION_SERVICE.build_result_from_request(
        {
            "source": "file",
            "value": str(profile_file_path),
        },
        obj_name="profiles",
    )
    assert file_result is None


def test_request_object_resolution_service_strict_mode_allows_db_store_imports(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_IDE_AGENTS_DB_PIPELINE_STRICT", "1")
    monkeypatch.setenv(
        "AI_IDE_AGENTS_DB_SOURCES",
        json.dumps(
            {
                "strict": True,
                "allowlist": {
                    "import_sources": ["profile_id"],
                },
            }
        ),
    )

    expected_payload = {
        "correlation_id": "profile-db-1",
        "profile": {"name": "Ada"},
    }

    monkeypatch.setattr(
        tools_mod.DOCUMENT_REPOSITORY,
        "get_document",
        lambda correlation_id, db_path=None, obj_name=None: expected_payload if correlation_id == "profile-db-1" else None,
    )

    loaded_result = tools_mod.REQUEST_OBJECT_RESOLUTION_SERVICE.build_result_from_request(
        {
            "source": "profile_id",
            "value": "profile-db-1",
        },
        obj_name="profiles",
    )
    assert loaded_result == expected_payload

    blocked_result = tools_mod.REQUEST_OBJECT_RESOLUTION_SERVICE.build_result_from_request(
        {
            "source": "correlation_id",
            "value": "profile-db-1",
        },
        obj_name="profiles",
    )
    assert blocked_result is None
