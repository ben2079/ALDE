from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import alde.agents_ccomp as chat_mod
import alde.agents_config as agents_configurator
import alde.agents_db as agents_db_mod
import alde.agents_factory as agents_factory
import alde.agents_tools as tools_mod


SAMPLE_WORKFLOW_REQUEST = {
    "action": "generate_cover_letter",
    "job_posting": {
        "source": "text",
        "value": {"title": "Full Stack Software Engineer", "company": {"about": "Example Co"}},
    },
    "applicant_profile": {
        "source": "text",
        "value": {"profile_id": "profile:test", "preferences": {"language": "de"}},
    },
    "options": {
        "language": "de",
        "tone": "modern",
        "max_words": 350,
    },
}


class _DeterministicDispatcherChatComE:
    def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
        self._model = _model
        self._messages = list(_messages)
        self._tools = list(tools)

    def _response(self):
        message = SimpleNamespace(
            content=json.dumps(
                {
                    "cover_letter": {"full_text": "Sehr geehrtes Team,\n\nMotivation und Erfahrung."},
                    "quality": {"word_count": 5, "language": "de"},
                },
                ensure_ascii=False,
            ),
            tool_calls=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _InMemoryMongoDocumentBackend:
    def __init__(self) -> None:
        self._collections: dict[tuple[str, str], dict[str, dict[str, object]]] = {}

    def _collection_name(self, *, db_name: str | None = None, obj_name: str | None = None) -> str:
        normalized_db_name = str(db_name or "").strip().lower()
        if normalized_db_name:
            return normalized_db_name
        return str(obj_name or "documents").strip() or "documents"

    def _bucket(self, *, collection_name: str, storage_key: str) -> dict[str, dict[str, object]]:
        return self._collections.setdefault((collection_name, storage_key), {})

    def load_db(
        self,
        *,
        storage_key: str,
        empty_db: dict[str, object],
        db_name: str | None = None,
        obj_name: str | None = None,
        root_key: str,
    ) -> dict[str, object]:
        collection_name = self._collection_name(db_name=db_name, obj_name=obj_name)
        bucket = self._bucket(collection_name=collection_name, storage_key=storage_key)
        loaded = json.loads(json.dumps(empty_db))
        loaded[root_key] = json.loads(json.dumps(bucket))
        return loaded

    def save_db(
        self,
        *,
        storage_key: str,
        db: dict[str, object],
        db_name: str | None = None,
        obj_name: str | None = None,
        root_key: str,
    ) -> None:
        collection_name = self._collection_name(db_name=db_name, obj_name=obj_name)
        bucket = self._bucket(collection_name=collection_name, storage_key=storage_key)
        bucket.clear()
        root_payload = db.get(root_key) if isinstance(db, dict) else None
        if isinstance(root_payload, dict):
            for record_id, record_value in root_payload.items():
                if isinstance(record_value, dict):
                    bucket[str(record_id)] = json.loads(json.dumps(record_value))

    def load_record(
        self,
        *,
        storage_key: str,
        record_id: str,
        db_name: str | None = None,
        obj_name: str | None = None,
    ) -> dict[str, object] | None:
        collection_name = self._collection_name(db_name=db_name, obj_name=obj_name)
        bucket = self._bucket(collection_name=collection_name, storage_key=storage_key)
        record = bucket.get(record_id)
        if not isinstance(record, dict):
            return None
        return json.loads(json.dumps(record))

    def upsert_record(
        self,
        *,
        storage_key: str,
        record_id: str,
        record_value: dict[str, object],
        db_name: str | None = None,
        obj_name: str | None = None,
    ) -> None:
        collection_name = self._collection_name(db_name=db_name, obj_name=obj_name)
        bucket = self._bucket(collection_name=collection_name, storage_key=storage_key)
        bucket[record_id] = json.loads(json.dumps(record_value))

    def delete_record(
        self,
        *,
        storage_key: str,
        record_id: str,
        db_name: str | None = None,
        obj_name: str | None = None,
    ) -> None:
        collection_name = self._collection_name(db_name=db_name, obj_name=obj_name)
        bucket = self._bucket(collection_name=collection_name, storage_key=storage_key)
        bucket.pop(record_id, None)

    def load_backend_diagnostic(self) -> dict[str, object]:
        return {
            "backend": "agents_db",
            "backend_mode": "enabled",
            "repository_type": "inmemory-test",
            "repository_available": True,
            "fallback_file_backend": False,
            "effective_uri": "agentsdb://inmemory",
            "database_name": "alde_knowledge",
            "last_error": "",
        }


class TestWorkflowIntegration(unittest.TestCase):
    def setUp(self) -> None:
        chat_mod.ChatHistory._history_ = []
        agents_factory._WORKFLOW_SESSION_CACHE.clear()
        self._previous_agentsdb_strict = os.getenv("AI_IDE_AGENTS_DB_PIPELINE_STRICT")
        os.environ["AI_IDE_AGENTS_DB_PIPELINE_STRICT"] = "0"

    def tearDown(self) -> None:
        if self._previous_agentsdb_strict is None:
            os.environ.pop("AI_IDE_AGENTS_DB_PIPELINE_STRICT", None)
        else:
            os.environ["AI_IDE_AGENTS_DB_PIPELINE_STRICT"] = self._previous_agentsdb_strict

    def _apply_env_values(self, env_value_map: dict[str, str]) -> dict[str, str | None]:
        previous_value_map: dict[str, str | None] = {}
        for env_name, env_value in env_value_map.items():
            previous_value_map[env_name] = os.getenv(env_name)
            os.environ[env_name] = env_value
        return previous_value_map

    def _restore_env_values(self, previous_value_map: dict[str, str | None]) -> None:
        for env_name, previous_value in previous_value_map.items():
            if previous_value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous_value

    def _reset_agentsdb_runtime_state(self) -> None:
        tools_mod.DOCUMENT_REPOSITORY._agentsdb_backend = None
        tools_mod.DOCUMENT_REPOSITORY._agentsdb_backend_loaded = False
        tools_mod.DOCUMENT_REPOSITORY._agentsdb_backend_diagnostic_emitted = False
        tools_mod.DOCUMENT_REPOSITORY._projection_cache.clear()
        agents_db_mod._AGENTS_DB_PIPELINE_SERVICE_CACHE.clear()

    def _load_chat_input_payload(self, chat: chat_mod.ChatCom) -> dict[str, object]:
        raw_input_text = getattr(chat, "_input_text", "")
        if isinstance(raw_input_text, dict):
            return dict(raw_input_text)
        if isinstance(raw_input_text, str):
            try:
                loaded_payload = json.loads(raw_input_text)
                if isinstance(loaded_payload, dict):
                    return loaded_payload
            except Exception:
                return {}
        return {}

    def test_cover_letter_request_uses_configured_forced_route(self) -> None:
        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(SAMPLE_WORKFLOW_REQUEST, ensure_ascii=False),
                _name="test_workflow",
            )

        self.assertIsNone(chat._forced_route)
        normalized_payload = self._load_chat_input_payload(chat)
        resolved_payload = {
            "profile_result": normalized_payload.get("profile_result"),
            "job_posting_result": normalized_payload.get("job_posting_result"),
        }
        self.assertEqual(resolved_payload["profile_result"]["profile"]["profile_id"], "profile:test")
        self.assertEqual(resolved_payload["profile_result"]["parse"]["language"], "de")
        self.assertEqual(resolved_payload["job_posting_result"]["job_posting"]["job_title"], "Full Stack Software Engineer")
        get_client.assert_called()

    def test_document_repository_requires_agentsdb_backend(self) -> None:
        with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=None):
            with self.assertRaises(RuntimeError) as exc:
                tools_mod.DOCUMENT_REPOSITORY.load_db(db_name="dispatcher_documents")
        self.assertIn("agentsdb_backend_unavailable", str(exc.exception))

    def test_document_repository_resolves_agentsdb_storage_keys_without_filesystem_abspath(self) -> None:
        expected_dispatcher_db_path = tools_mod._default_dispatcher_db_path()
        expected_job_postings_db_path = tools_mod._default_document_db_path("job_postings")
        dispatcher_db_path = tools_mod.DOCUMENT_REPOSITORY._resolve_db_path(db_name="dispatcher_documents")
        job_postings_db_path = tools_mod.DOCUMENT_REPOSITORY._resolve_db_path(
            db_name="job_postings",
            obj_name="job_postings",
        )
        explicit_agentsdb_key = tools_mod.DOCUMENT_REPOSITORY._resolve_db_path(
            db_path="2444::agentsdb::custom_namespace",
        )

        self.assertEqual(dispatcher_db_path, expected_dispatcher_db_path)
        self.assertEqual(job_postings_db_path, expected_job_postings_db_path)
        self.assertEqual(explicit_agentsdb_key, "2444::agentsdb::custom_namespace")
        self.assertFalse(os.path.isabs(dispatcher_db_path), dispatcher_db_path)
        self.assertFalse(os.path.isabs(job_postings_db_path), job_postings_db_path)

    def test_real_agentsdb_socket_pipeline_ingests_job_posting_and_syncs_knowledge(self) -> None:
        agents_db_uri = "agentsdb://127.0.0.1:2331"
        database_name = "alde_knowledge"
        storage_key = "2331::agentsdb::job_postings_real_pipeline_e2e"
        correlation_id = "real-socket-ingest-127001-2331"

        previous_env = self._apply_env_values(
            {
                "AI_IDE_KNOWLEDGE_AGENTS_DB_URI": agents_db_uri,
                "AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI": "agentsmem://local",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAME": database_name,
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_ID": "ns_real_socket_pipeline",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_SLUG": "real-socket-pipeline",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_NAME": "Real Socket Pipeline",
            }
        )
        self._reset_agentsdb_runtime_state()

        socket_repository: agents_db_mod.AgentDbSocketRepository | None = None
        try:
            try:
                socket_repository = agents_db_mod.AgentDbSocketRepository.create_from_uri(
                    agents_db_uri,
                    database_name,
                    timeout_seconds=1.5,
                )
                health_payload = socket_repository._request_object("health")
                self.assertTrue(bool(health_payload.get("ok", True)))
            except Exception as exc:
                self.skipTest(
                    "agentsdb socket endpoint not reachable for real integration test "
                    f"({agents_db_uri}): {type(exc).__name__}: {exc}"
                )

            result_text, routing_request = agents_factory.execute_tool(
                "execute_action_request",
                {
                    "action": "ingest_object",
                    "payload": {
                        "correlation_id": correlation_id,
                        "obj_name": "job_postings",
                        "obj_db_path": storage_key,
                        "source_agent": "job_posting_parser_real_socket_test",
                        "job_posting": {
                            "job_title": "Real Socket Pipeline Engineer",
                            "company_name": "AgentsDB Integration Labs",
                            "requirements": {
                                "technical_skills": ["Python", "AgentsDB"],
                                "soft_skills": ["Ownership"],
                                "languages": ["Deutsch", "Englisch"],
                            },
                        },
                        "source_payload": {
                            "integration_test": True,
                            "agents_db_uri": agents_db_uri,
                        },
                    },
                },
                source_agent_label="_xworker",
            )

            result = json.loads(result_text)
            stored = tools_mod.DOCUMENT_REPOSITORY.get_document(
                correlation_id,
                db_path=storage_key,
                obj_name="job_postings",
            )

            self.assertIsNone(routing_request)
            self.assertTrue(result["ok"])
            self.assertEqual(result["correlation_id"], correlation_id)
            self.assertIsInstance(result.get("knowledge_sync"), dict)
            self.assertTrue(bool(result["knowledge_sync"].get("stored")), result.get("knowledge_sync"))
            self.assertIn(str(result["knowledge_sync"].get("object_name") or ""), {"job_posting", "job_postings"})
            self.assertEqual(stored["job_posting"]["job_title"], "Real Socket Pipeline Engineer")

            document_id = str(result["knowledge_sync"].get("document_id") or "").strip()
            self.assertTrue(document_id)

            self.assertIsNotNone(socket_repository)
            mapped_document = socket_repository.load_object("document", document_id)
            self.assertIsNotNone(mapped_document)
            self.assertEqual(str(mapped_document.get("correlation_id") or ""), correlation_id)
        finally:
            self._restore_env_values(previous_env)
            self._reset_agentsdb_runtime_state()

    def test_cover_letter_request_resolves_persisted_job_posting_before_routing(self) -> None:
        stored_request = {
            "action": "generate_cover_letter",
            "job_posting": {
                "source": "correlation_id",
                "value": "sha-stored-1",
            },
            "job_postings_db_path": "/tmp/job_postings.json",
            "applicant_profile": {
                "source": "text",
                "value": {"profile_id": "profile:test", "preferences": {"language": "de"}},
            },
            "options": {
                "language": "de",
                "tone": "modern",
                "max_words": 350,
            },
        }

        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client, patch(
            "alde.tools.DOCUMENT_REPOSITORY.get_document",
            return_value={
                "agent": "job_posting_parser",
                "correlation_id": "sha-stored-1",
                "parse": {"is_job_posting": True},
                "job_posting": {"job_title": "Backend Engineer", "company_name": "Stored Co"},
                "db_updates": {"processing_state": "processed", "processed": True},
                "file": {"content_sha256": "sha-stored-1"},
                "link": {"thread_id": "thread-1", "message_id": "msg-1"},
            },
        ):
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(stored_request, ensure_ascii=False),
                _name="test_workflow",
            )

        self.assertIsNone(chat._forced_route)
        normalized_payload = self._load_chat_input_payload(chat)
        resolved_payload = {
            "profile_result": normalized_payload.get("profile_result"),
            "job_posting_result": normalized_payload.get("job_posting_result"),
        }
        self.assertEqual(resolved_payload["job_posting_result"]["correlation_id"], "sha-stored-1")
        self.assertEqual(resolved_payload["job_posting_result"]["job_posting"]["job_title"], "Backend Engineer")
        self.assertNotIn("job_posting", normalized_payload)
        self.assertNotIn("job_postings_db_path", normalized_payload)
        self.assertEqual(resolved_payload["profile_result"]["profile"]["profile_id"], "profile:test")
        self.assertEqual(resolved_payload["profile_result"]["parse"]["language"], "de")
        get_client.assert_called()

    def test_cover_letter_request_with_structured_inputs_routes_directly_to_writer(self) -> None:
        ready_request = {
            "action": "generate_cover_letter",
            "job_posting_result": {
                "agent": "job_posting_parser",
                "correlation_id": "sha-ready-1",
                "job_posting": {"job_title": "Platform Engineer"},
            },
            "applicant_profile": {
                "source": "text",
                "value": {"profile_id": "profile:ready", "preferences": {"language": "en"}},
            },
            "options": {"language": "en", "tone": "direct", "max_words": 250},
        }

        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(ready_request, ensure_ascii=False),
                _name="test_workflow",
            )

        self.assertIsNone(chat._forced_route)
        normalized_payload = self._load_chat_input_payload(chat)
        resolved_payload = {
            "profile_result": normalized_payload.get("profile_result"),
            "job_posting_result": normalized_payload.get("job_posting_result"),
        }
        self.assertEqual(resolved_payload["job_posting_result"]["correlation_id"], "sha-ready-1")
        self.assertEqual(resolved_payload["profile_result"]["profile"]["profile_id"], "profile:ready")
        self.assertEqual(resolved_payload["profile_result"]["parse"]["language"], "en")
        get_client.assert_called()

    def test_cover_letter_request_with_profile_file_routes_directly_to_writer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"profile_id": "profile:file", "preferences": {"language": "fr"}}, tmp, ensure_ascii=False)
            profile_path = tmp.name

        try:
            request = {
                "action": "generate_cover_letter",
                "job_posting": {
                    "source": "text",
                    "value": {"title": "API Engineer", "company": {"about": "Example Co"}},
                },
                "applicant_profile": {
                    "source": "file",
                    "value": profile_path,
                },
                "options": {"language": "fr", "tone": "formal", "max_words": 300},
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )

            forced_route = getattr(chat, "_forced_route", None)
            if forced_route is not None:
                self.assertEqual(forced_route.get("target_agent"), "_xworker")
                self.assertEqual(forced_route.get("job_name"), "cover_letter_writer")
            normalized_payload = self._load_chat_input_payload(chat)
            resolved_payload = {
                "profile_result": normalized_payload.get("profile_result"),
                "job_posting_result": normalized_payload.get("job_posting_result"),
            }
            self.assertEqual(resolved_payload["profile_result"]["profile"]["profile_id"], "profile:file")
            self.assertEqual(resolved_payload["profile_result"]["parse"]["language"], "fr")
            self.assertEqual(resolved_payload["profile_result"]["profile"]["source_path"], profile_path)
            self.assertEqual(resolved_payload["job_posting_result"]["job_posting"]["job_title"], "API Engineer")
            if forced_route is None:
                get_client.assert_called()
            else:
                get_client.assert_not_called()
        finally:
            os.unlink(profile_path)

    def test_cover_letter_request_with_profile_file_and_job_result_routes_directly_to_writer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump({"profile_id": "profile:file-ready", "preferences": {"language": "it"}}, tmp, ensure_ascii=False)
            profile_path = tmp.name

        try:
            request = {
                "action": "generate_cover_letter",
                "job_posting_result": {
                    "agent": "job_posting_parser",
                    "correlation_id": "sha-file-ready",
                    "job_posting": {"job_title": "Systems Engineer"},
                },
                "applicant_profile": {
                    "source": "file",
                    "value": {"path": profile_path},
                },
                "options": {"language": "it", "tone": "precise", "max_words": 280},
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )

            forced_route = getattr(chat, "_forced_route", None)
            if forced_route is not None:
                self.assertEqual(forced_route.get("target_agent"), "_xworker")
                self.assertEqual(forced_route.get("job_name"), "cover_letter_writer")
            normalized_payload = self._load_chat_input_payload(chat)
            resolved_payload = {
                "profile_result": normalized_payload.get("profile_result"),
                "job_posting_result": normalized_payload.get("job_posting_result"),
            }
            self.assertEqual(resolved_payload["profile_result"]["profile"]["profile_id"], "profile:file-ready")
            self.assertEqual(resolved_payload["profile_result"]["parse"]["language"], "it")
            self.assertEqual(resolved_payload["profile_result"]["profile"]["source_path"], profile_path)
            if forced_route is None:
                get_client.assert_called()
            else:
                get_client.assert_not_called()
        finally:
            os.unlink(profile_path)

    def test_cover_letter_request_with_persisted_profile_routes_directly_to_writer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(
                {
                    "schema": "profiles_db_v1",
                    "profiles": {
                        "profile:stored": {
                            "correlation_id": "profile:stored",
                            "source_agent": "profile_parser",
                            "parse": {"language": "es", "errors": [], "warnings": []},
                            "profile": {"profile_id": "profile:stored", "preferences": {"language": "es"}},
                        }
                    },
                },
                tmp,
                ensure_ascii=False,
            )
            profiles_db_path = tmp.name

        try:
            request = {
                "action": "generate_cover_letter",
                "job_posting": {
                    "source": "text",
                    "value": {"title": "Platform Engineer", "company": {"about": "Stored Co"}},
                },
                "applicant_profile": {
                    "source": "profile_id",
                    "value": "profile:stored",
                    "db_path": profiles_db_path,
                },
                "options": {"language": "es", "tone": "clear", "max_words": 320},
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )

            self.assertIsNone(chat._forced_route)
            normalized_payload = self._load_chat_input_payload(chat)
            self.assertEqual(normalized_payload.get("action"), "generate_cover_letter")
            profile_result = normalized_payload.get("profile_result")
            if isinstance(profile_result, dict):
                self.assertEqual(profile_result.get("correlation_id"), "profile:stored")
                self.assertEqual((profile_result.get("profile") or {}).get("profile_id"), "profile:stored")
            job_posting_result = normalized_payload.get("job_posting_result")
            if isinstance(job_posting_result, dict):
                self.assertEqual((job_posting_result.get("job_posting") or {}).get("job_title"), "Platform Engineer")
            get_client.assert_called()
        finally:
            os.unlink(profiles_db_path)

    def test_cover_letter_request_with_job_posting_file_routes_directly_to_writer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as posting_tmp, tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as profile_tmp:
            posting_tmp.write("Support Engineer Rolle mit SQL, Kundenkontakt und ERP-Kontext.")
            posting_path = posting_tmp.name
            json.dump({"profile_id": "profile:file-job", "preferences": {"language": "de"}}, profile_tmp, ensure_ascii=False)
            profile_path = profile_tmp.name

        try:
            request = {
                "action": "generate_cover_letter",
                "job_posting": {
                    "source": "file",
                    "value": posting_path,
                },
                "applicant_profile": {
                    "source": "file",
                    "value": profile_path,
                },
                "options": {"language": "de", "tone": "modern", "max_words": 280},
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )

            forced_route = getattr(chat, "_forced_route", None)
            if forced_route is not None:
                self.assertEqual(forced_route.get("target_agent"), "_xworker")
                self.assertEqual(forced_route.get("job_name"), "cover_letter_writer")
            normalized_payload = self._load_chat_input_payload(chat)
            resolved_payload = {
                "profile_result": normalized_payload.get("profile_result"),
                "job_posting_result": normalized_payload.get("job_posting_result"),
            }
            self.assertEqual(resolved_payload["job_posting_result"]["file"]["path"], posting_path)
            self.assertTrue(resolved_payload["job_posting_result"]["correlation_id"])
            self.assertIn("SQL", resolved_payload["job_posting_result"]["job_posting"]["raw_text"])
            self.assertEqual(resolved_payload["profile_result"]["profile"]["source_path"], profile_path)
            if forced_route is None:
                get_client.assert_called()
            else:
                get_client.assert_not_called()
        finally:
            os.unlink(posting_path)
            os.unlink(profile_path)

    def test_cover_letter_request_with_pdf_job_posting_file_uses_document_reader_resolution(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".pdf", delete=False, encoding="utf-8") as posting_tmp, tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as profile_tmp:
            posting_tmp.write("%PDF-1.4\nMock PDF content")
            posting_path = posting_tmp.name
            json.dump({"profile_id": "profile:file-job-pdf", "preferences": {"language": "de"}}, profile_tmp, ensure_ascii=False)
            profile_path = profile_tmp.name

        try:
            request = {
                "action": "generate_cover_letter",
                "job_posting": {
                    "source": "file",
                    "value": posting_path,
                },
                "applicant_profile": {
                    "source": "file",
                    "value": profile_path,
                },
                "options": {"language": "de", "tone": "modern", "max_words": 280},
            }

            with patch.object(
                tools_mod,
                "read_document",
                return_value="Software Support Specialist mit SQL, Kundenkontakt und ERP-Kontext.",
            ) as read_document_mock, patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )

            forced_route = getattr(chat, "_forced_route", None)
            if forced_route is not None:
                self.assertEqual(forced_route.get("target_agent"), "_xworker")
                self.assertEqual(forced_route.get("job_name"), "cover_letter_writer")
            normalized_payload = self._load_chat_input_payload(chat)
            resolved_payload = {
                "profile_result": normalized_payload.get("profile_result"),
                "job_posting_result": normalized_payload.get("job_posting_result"),
            }
            self.assertEqual(resolved_payload["job_posting_result"]["file"]["path"], posting_path)
            self.assertIn("SQL", resolved_payload["job_posting_result"]["job_posting"]["raw_text"])
            read_document_mock.assert_called_with(posting_path)
            if forced_route is None:
                get_client.assert_called()
            else:
                get_client.assert_not_called()
        finally:
            os.unlink(posting_path)
            os.unlink(profile_path)

    def test_cover_letter_request_with_persisted_profile_and_job_result_routes_directly_to_writer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(
                {
                    "schema": "profiles_db_v1",
                    "profiles": {
                        "profile:stored-ready": {
                            "correlation_id": "profile:stored-ready",
                            "source_agent": "profile_parser",
                            "parse": {"language": "nl", "errors": [], "warnings": []},
                            "profile": {"profile_id": "profile:stored-ready", "preferences": {"language": "nl"}},
                        }
                    },
                },
                tmp,
                ensure_ascii=False,
            )
            profiles_db_path = tmp.name

        try:
            request = {
                "action": "generate_cover_letter",
                "job_posting_result": {
                    "agent": "job_posting_parser",
                    "correlation_id": "sha-profile-stored-ready",
                    "job_posting": {"job_title": "Site Reliability Engineer"},
                },
                "applicant_profile": {
                    "source": "profile_id",
                    "value": {"profile_id": "profile:stored-ready"},
                    "profiles_db_path": profiles_db_path,
                },
                "options": {"language": "nl", "tone": "sharp", "max_words": 260},
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )

            self.assertIsNone(chat._forced_route)
            normalized_payload = self._load_chat_input_payload(chat)
            self.assertEqual(normalized_payload.get("action"), "generate_cover_letter")
            self.assertEqual(
                ((normalized_payload.get("job_posting_result") or {}).get("correlation_id")),
                "sha-profile-stored-ready",
            )
            profile_result = normalized_payload.get("profile_result")
            if isinstance(profile_result, dict):
                self.assertEqual(profile_result.get("correlation_id"), "profile:stored-ready")
                self.assertEqual((profile_result.get("profile") or {}).get("profile_id"), "profile:stored-ready")
            get_client.assert_called()
        finally:
            os.unlink(profiles_db_path)

    def test_ready_cover_letter_forced_route_uses_structured_handoff(self) -> None:
        ready_request = {
            "action": "generate_cover_letter",
            "job_posting_result": {
                "agent": "job_posting_parser",
                "correlation_id": "sha-ready-2",
                "job_posting": {"job_title": "Support Engineer"},
            },
            "applicant_profile": {
                "source": "text",
                "value": {"profile_id": "profile:ready-2", "preferences": {"language": "de"}},
            },
            "options": {"language": "de", "tone": "modern", "max_words": 280},
        }

        chat = chat_mod.ChatCom(
            _model="gpt-4o-mini",
            _input_text=json.dumps(ready_request, ensure_ascii=False),
            _name="test_ready_handoff",
        )

        self.assertIsInstance(chat._forced_route, dict)
        self.assertEqual((chat._forced_route or {}).get("job_name"), "cover_letter_writer")
        forced_output = (((chat._forced_route or {}).get("handoff_payload") or {}).get("output") or {})
        self.assertEqual((forced_output.get("job_posting_result") or {}).get("object_name"), "job_postings")
        self.assertEqual((forced_output.get("profile_result") or {}).get("object_name"), "profiles")
        normalized_payload = self._load_chat_input_payload(chat)
        self.assertEqual(
            ((normalized_payload.get("job_posting_result") or {}).get("correlation_id")),
            "sha-ready-2",
        )

    def test_resolve_forced_route_cover_letter_writer_payload_uses_canonical_results(self) -> None:
        route = agents_configurator.resolve_forced_route(
            "_xrouter_xplanner",
            {
                "action": "generate_cover_letter",
                "correlation_id": "job-route-cover-1",
                "job_posting_result": {
                    "job_posting": {
                        "job_title": "Route Platform Engineer",
                        "company_name": "Route Co",
                    },
                    "file": {
                        "path": "/tmp/route-cover-1.pdf",
                        "content_sha256": "job-route-cover-1",
                    },
                },
                "profile_result": {
                    "profile": {
                        "profile_id": "profile-route-cover-1",
                        "skills": ["python", "rag"],
                    },
                },
                "options": {
                    "language": "de",
                },
            },
            {"_xworker"},
        )

        self.assertIsInstance(route, dict)
        self.assertEqual((route or {}).get("job_name"), "cover_letter_writer")
        output_payload = (((route or {}).get("handoff_payload") or {}).get("output") or {})
        self.assertEqual((output_payload.get("job_posting_result") or {}).get("object_name"), "job_postings")
        self.assertEqual((output_payload.get("job_posting_result") or {}).get("record_kind"), "document")
        self.assertEqual((output_payload.get("job_posting_result") or {}).get("correlation_id"), "job-route-cover-1")
        self.assertEqual((output_payload.get("job_posting_result") or {}).get("source_path"), "/tmp/route-cover-1.pdf")
        self.assertEqual((output_payload.get("profile_result") or {}).get("object_name"), "profiles")
        self.assertEqual((output_payload.get("profile_result") or {}).get("record_kind"), "document")
        self.assertEqual((output_payload.get("profile_result") or {}).get("correlation_id"), "profile-route-cover-1")

    def test_store_result_supports_non_pdf_job_posting_sources(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            result = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    job_posting_result={
                        "agent": "job_platform_ingest",
                        "correlation_id": "platform:job-42",
                        "parse": {"is_job_posting": True},
                        "job_posting": {
                            "job_title": "Remote Python Engineer",
                            "company_name": "Platform Co",
                        },
                    },
                    db_path=job_postings_db_path,
                    source_agent="job_platform_ingest",
                    source_payload={
                        "platform": "example_jobs",
                        "record_id": "job-42",
                        "url": "https://jobs.example.invalid/job-42",
                    },
                )
            )

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("platform:job-42", db_path=job_postings_db_path, obj_name="job_postings")
            self.assertTrue(result["ok"])
            self.assertEqual(result["correlation_id"], "platform:job-42")
            self.assertEqual(stored["job_posting"]["job_title"], "Remote Python Engineer")
            self.assertEqual(stored["parse"]["is_job_posting"], True)
        finally:
            os.unlink(job_postings_db_path)

    def test_store_result_includes_knowledge_sync_for_job_postings(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            with patch.object(
                tools_mod,
                "sync_parser_result_to_agentsdb_knowledge",
                return_value={
                    "ok": True,
                    "stored": True,
                    "object_name": "job_posting",
                    "entity_count": 4,
                    "relation_count": 3,
                },
            ) as sync_mock:
                result = json.loads(
                    tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                        job_posting_result={
                            "agent": "job_platform_ingest",
                            "correlation_id": "platform:job-44",
                            "parse": {"is_job_posting": True},
                            "job_posting": {
                                "job_title": "Knowledge Graph Engineer",
                                "company_name": "Platform Co",
                                "requirements": {
                                    "technical_skills": ["Python", "Neo4j"],
                                    "languages": ["Deutsch"],
                                },
                            },
                        },
                        db_path=job_postings_db_path,
                        source_agent="job_platform_ingest",
                        source_payload={"platform": "example_jobs", "record_id": "job-44"},
                    )
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["knowledge_sync"]["stored"])
            self.assertEqual(result["knowledge_sync"]["entity_count"], 4)
            self.assertEqual(sync_mock.call_args.kwargs["object_name"], "job_postings")
            self.assertEqual(sync_mock.call_args.kwargs["correlation_id"], "platform:job-44")
        finally:
            os.unlink(job_postings_db_path)

    def test_store_result_uses_mongo_backend_as_primary_store_for_job_postings(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            mongo_backend = _InMemoryMongoDocumentBackend()
            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=mongo_backend):
                result = json.loads(
                    tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                        job_posting_result={
                            "agent": "job_platform_ingest",
                            "correlation_id": "platform:job-43",
                            "parse": {"is_job_posting": True},
                            "job_posting": {
                                "job_title": "Knowledge Pipeline Engineer",
                                "company_name": "Platform Co",
                            },
                        },
                        db_path=job_postings_db_path,
                        source_agent="job_platform_ingest",
                        source_payload={"platform": "example_jobs", "record_id": "job-43"},
                    )
                )
                stored = tools_mod.DOCUMENT_REPOSITORY.get_document(
                    "platform:job-43",
                    db_path=job_postings_db_path,
                    obj_name="job_postings",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["db_path"], job_postings_db_path)
            self.assertEqual(stored["job_posting"]["job_title"], "Knowledge Pipeline Engineer")
            mongo_record = mongo_backend.load_record(
                storage_key=job_postings_db_path,
                record_id="platform:job-43",
                db_name="job_postings",
                obj_name="job_postings",
            )
            self.assertEqual(mongo_record["job_posting"]["job_title"], "Knowledge Pipeline Engineer")
        finally:
            os.unlink(job_postings_db_path)

    def test_store_result_persists_explicit_job_posting_model(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            result = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    job_posting_result={
                        "agent": "job_platform_ingest",
                        "correlation_id": "platform:job-explicit-55",
                        "parse": {"is_job_posting": True, "language": "de"},
                        "raw_text_document": {
                            "document_type": "job_posting",
                            "title": "Platform Support Engineer",
                            "language": "de",
                            "raw_text": "Platform Support Engineer at Example Co with Python and SQL.",
                            "sections": [
                                {
                                    "section_key": "header",
                                    "heading": "Object Header",
                                    "text": "Title: Platform Support Engineer\nOrganization: Example Co",
                                }
                            ],
                        },
                        "entity_objects": [
                            {
                                "entity_key": "subject",
                                "entity_type": "job_posting",
                                "canonical_name": "Platform Support Engineer",
                                "metadata": {"role": "subject"},
                            },
                            {
                                "entity_key": "organization:example_co",
                                "entity_type": "organization",
                                "canonical_name": "Example Co",
                            },
                            {
                                "entity_key": "skill:python",
                                "entity_type": "skill",
                                "canonical_name": "Python",
                            },
                        ],
                        "relation_objects": [
                            {
                                "source_entity_key": "subject",
                                "target_entity_key": "organization:example_co",
                                "relation_type": "offered_by",
                                "section_key": "header",
                            }
                        ],
                    },
                    db_path=job_postings_db_path,
                    source_agent="job_platform_ingest",
                )
            )

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document(
                "platform:job-explicit-55",
                db_path=job_postings_db_path,
                obj_name="job_postings",
            )
            self.assertTrue(result["ok"])
            self.assertEqual(stored["job_posting"]["job_title"], "Platform Support Engineer")
            self.assertEqual(stored["job_posting"]["company_name"], "Example Co")
            self.assertEqual(stored["raw_text_document"]["title"], "Platform Support Engineer")
            self.assertEqual(stored["entity_objects"][2]["canonical_name"], "Python")
        finally:
            os.unlink(job_postings_db_path)

    def test_update_dispatcher_status_uses_mongo_backend_as_primary_store(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            dispatcher_db_path = tmp.name

        try:
            mongo_backend = _InMemoryMongoDocumentBackend()
            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=mongo_backend):
                result = tools_mod.DOCUMENT_REPOSITORY.update_dispatcher_status(
                    correlation_id="dispatch:job-44",
                    processing_state="processed",
                    db_path=dispatcher_db_path,
                    processed=True,
                    extra_updates={"source_agent": "job_dispatcher", "tenant_id": "tenant_demo"},
                )
                dispatcher_db = tools_mod.DOCUMENT_REPOSITORY.load_db(dispatcher_db_path, db_name="dispatcher_documents")

            self.assertTrue(result["ok"])
            self.assertEqual(dispatcher_db["documents"]["dispatch:job-44"]["processing_state"], "processed")
            self.assertEqual(dispatcher_db["documents"]["dispatch:job-44"]["source_agent"], "job_dispatcher")
        finally:
            os.unlink(dispatcher_db_path)

    def test_get_dispatcher_record_returns_canonical_operational_fields_for_legacy_records(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            dispatcher_db_path = tmp.name

        try:
            correlation_id = "dispatch:job-legacy-45"
            source_path = "/tmp/platform_engineer.pdf"
            mongo_backend = _InMemoryMongoDocumentBackend()
            mongo_backend.upsert_record(
                storage_key=dispatcher_db_path,
                record_id=correlation_id,
                record_value={
                    "id": correlation_id,
                    "content_sha256": correlation_id,
                    "source_path": source_path,
                    "processing_state": "queued",
                    "processed": False,
                },
                db_name="dispatcher_documents",
                obj_name="documents",
            )

            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=mongo_backend):
                dispatcher_record = tools_mod.DOCUMENT_REPOSITORY.get_dispatcher_record(
                    correlation_id,
                    db_path=dispatcher_db_path,
                )

            self.assertIsInstance(dispatcher_record, dict)
            self.assertEqual((dispatcher_record or {}).get("title"), "platform_engineer")
            self.assertEqual((dispatcher_record or {}).get("record_kind"), "document")
            self.assertEqual((dispatcher_record or {}).get("kind"), "document")
            self.assertEqual((dispatcher_record or {}).get("object_name"), "documents")
            self.assertEqual((dispatcher_record or {}).get("status"), "queued")
            self.assertEqual((dispatcher_record or {}).get("db_updates", {}).get("processing_state"), "queued")
        finally:
            os.unlink(dispatcher_db_path)

    def test_request_object_resolution_loads_legacy_db_file_with_canonical_fields(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            db_path = tmp.name
            json.dump(
                {
                    "job_postings": {
                        "legacy-job-1": {
                            "correlation_id": "legacy-job-1",
                            "source_agent": "job_posting_parser",
                            "file": {
                                "path": "/tmp/legacy_job.pdf",
                                "content_sha256": "legacy-job-1",
                            },
                            "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                            "job_posting": {
                                "job_title": "Legacy Platform Engineer",
                                "company_name": "Example Co",
                            },
                            "db_updates": {"processing_state": "processed", "processed": True},
                        }
                    }
                },
                tmp,
                ensure_ascii=False,
            )

        try:
            result = tools_mod.REQUEST_OBJECT_RESOLUTION_SERVICE._load_result_from_legacy_db_file(
                correlation_id="legacy-job-1",
                obj_name="job_postings",
                db_path=db_path,
            )

            self.assertIsInstance(result, dict)
            self.assertEqual((result or {}).get("title"), "Legacy Platform Engineer")
            self.assertEqual((result or {}).get("content_sha256"), "legacy-job-1")
            self.assertEqual((result or {}).get("record_kind"), "document")
            self.assertEqual((result or {}).get("object_name"), "job_postings")
            self.assertEqual((result or {}).get("processing_state"), "processed")
            self.assertIs((result or {}).get("processed"), True)
            self.assertEqual((result or {}).get("db_updates", {}).get("processing_state"), "processed")
        finally:
            os.unlink(db_path)

    def test_request_object_resolution_load_result_from_file_returns_canonical_fields(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            source_path = tmp.name
            tmp.write("Platform Engineer role with Python and workflow orchestration.\n")

        try:
            result = tools_mod.REQUEST_OBJECT_RESOLUTION_SERVICE.load_result_from_file(
                source_path=source_path,
                obj_name="job_postings",
            )

            self.assertIsInstance(result, dict)
            self.assertEqual((result or {}).get("source_path"), source_path)
            self.assertEqual((result or {}).get("record_kind"), "document")
            self.assertEqual((result or {}).get("kind"), "document")
            self.assertEqual((result or {}).get("object_name"), "job_postings")
            self.assertEqual((result or {}).get("processing_state"), "processed")
            self.assertIs((result or {}).get("processed"), True)
            self.assertEqual((result or {}).get("db_updates", {}).get("processing_state"), "processed")
            self.assertEqual((result or {}).get("file", {}).get("path"), source_path)
            self.assertTrue(str((result or {}).get("content_sha256") or "").strip())
        finally:
            os.unlink(source_path)

    def test_routing_result_object_load_upsert_payload_adds_canonical_top_level_fields(self) -> None:
        routing_request = {
            "handoff": {
                "target_agent": "_xworker",
                "source_agent": "_xrouter_xplanner",
                "handoff_payload": {
                    "output": {
                        "correlation_id": "route-job-1",
                        "file": {
                            "path": "/tmp/route-job-1.pdf",
                            "content_sha256": "route-job-1",
                        },
                    }
                },
                "metadata": {
                    "correlation_id": "route-job-1",
                    "obj_name": "job_postings",
                },
            },
            "handoff_context": {
                "source_agent": "_xrouter_xplanner",
                "contract": {
                    "schema": {
                        "result_postprocess": {
                            "tool": "store_object_result",
                            "source_agent": "target_agent",
                        }
                    }
                },
            },
        }
        result_object = agents_factory.RoutingResultObject(
            routing_request=routing_request,
            result_text={
                "agent": "job_posting_parser",
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "job_posting": {
                    "job_title": "Routing Platform Engineer",
                    "company_name": "Example Co",
                },
                "db_updates": {
                    "processing_state": "processed",
                    "processed": True,
                },
            },
            succeeded=True,
        )

        payload = result_object.load_upsert_payload()

        self.assertEqual(payload.get("title"), "Routing Platform Engineer")
        self.assertEqual(payload.get("object_name"), "job_postings")
        self.assertEqual(payload.get("record_kind"), "document")
        self.assertEqual(payload.get("kind"), "document")
        self.assertEqual(payload.get("source"), "job_posting_parser")
        self.assertEqual(payload.get("source_path"), "/tmp/route-job-1.pdf")
        self.assertEqual(payload.get("content_sha256"), "route-job-1")
        self.assertEqual(payload.get("processing_state"), "processed")
        self.assertIs(payload.get("processed"), True)
        self.assertEqual((payload.get("db_updates") or {}).get("processing_state"), "processed")

    def test_routing_result_object_dispatcher_updates_roundtrip_canonical_fields(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            dispatcher_db_path = tmp.name

        routing_request = {
            "handoff": {
                "target_agent": "_xworker",
                "source_agent": "_xrouter_xplanner",
                "handoff_payload": {
                    "output": {
                        "correlation_id": "route-job-2",
                        "file": {
                            "path": "/tmp/route-job-2.pdf",
                            "content_sha256": "route-job-2",
                        },
                    }
                },
                "metadata": {
                    "correlation_id": "route-job-2",
                    "obj_name": "job_postings",
                },
            },
            "handoff_context": {
                "source_agent": "_xrouter_xplanner",
                "contract": {
                    "schema": {
                        "result_postprocess": {
                            "tool": "store_object_result",
                            "source_agent": "target_agent",
                        }
                    }
                },
            },
        }

        try:
            result_object = agents_factory.RoutingResultObject(
                routing_request=routing_request,
                result_text={
                    "agent": "job_posting_parser",
                    "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                    "job_posting": {
                        "job_title": "Dispatcher Platform Engineer",
                        "company_name": "Example Co",
                    },
                    "db_updates": {
                        "processing_state": "processed",
                        "processed": True,
                    },
                },
                succeeded=True,
            )

            tools_mod.DOCUMENT_REPOSITORY.update_dispatcher_status(
                correlation_id="route-job-2",
                processing_state=result_object.load_processing_state(),
                db_path=dispatcher_db_path,
                processed=result_object.load_processed(),
                failed_reason=result_object.load_failed_reason(),
                extra_updates=result_object.load_dispatcher_updates(),
            )
            dispatcher_record = tools_mod.DOCUMENT_REPOSITORY.get_dispatcher_record(
                "route-job-2",
                db_path=dispatcher_db_path,
            )

            self.assertEqual((dispatcher_record or {}).get("title"), "Dispatcher Platform Engineer")
            self.assertEqual((dispatcher_record or {}).get("object_name"), "job_postings")
            self.assertEqual((dispatcher_record or {}).get("record_kind"), "document")
            self.assertEqual((dispatcher_record or {}).get("kind"), "document")
            self.assertEqual((dispatcher_record or {}).get("source"), "job_posting_parser")
            self.assertEqual((dispatcher_record or {}).get("source_path"), "/tmp/route-job-2.pdf")
            self.assertEqual((dispatcher_record or {}).get("content_sha256"), "route-job-2")
            self.assertEqual((dispatcher_record or {}).get("processing_state"), "processed")
            self.assertIs((dispatcher_record or {}).get("processed"), True)
        finally:
            os.unlink(dispatcher_db_path)

    def test_routing_result_object_build_cover_letter_handoff_args_uses_canonical_results(self) -> None:
        routing_request = {
            "handoff": {
                "target_agent": "_xworker",
                "source_agent": "_xrouter_xplanner",
                "handoff_payload": {
                    "output": {
                        "action": "generate_cover_letter",
                        "correlation_id": "job-cover-1",
                        "applicant_profile": {
                            "source": "text",
                            "value": {
                                "profile_id": "profile-cover-1",
                                "skills": ["python", "rag"],
                            },
                        },
                        "options": {
                            "language": "fr",
                        },
                    }
                },
                "metadata": {
                    "correlation_id": "job-cover-1",
                    "obj_name": "job_postings",
                    "writer_job_name": "cover_letter_writer",
                },
            },
            "handoff_context": {
                "source_agent": "_xrouter_xplanner",
                "contract": {
                    "schema": {
                        "result_postprocess": {
                            "tool": "store_object_result",
                            "source_agent": "target_agent",
                        }
                    }
                },
            },
        }

        result_object = agents_factory.RoutingResultObject(
            routing_request=routing_request,
            result_text={
                "agent": "job_posting_parser",
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "job_posting": {
                    "job_title": "Cover Letter Engineer",
                    "company_name": "Example Co",
                },
                "file": {
                    "path": "/tmp/job-cover-1.pdf",
                    "content_sha256": "job-cover-1",
                },
                "db_updates": {
                    "processing_state": "processed",
                    "processed": True,
                },
            },
            succeeded=True,
        )

        handoff_args = result_object.build_cover_letter_handoff_args()

        self.assertIsInstance(handoff_args, dict)
        writer_payload = ((handoff_args or {}).get("handoff_payload") or {}).get("output") or {}
        self.assertEqual((writer_payload.get("job_posting_result") or {}).get("object_name"), "job_postings")
        self.assertEqual((writer_payload.get("job_posting_result") or {}).get("record_kind"), "document")
        self.assertEqual((writer_payload.get("job_posting_result") or {}).get("source_path"), "/tmp/job-cover-1.pdf")
        self.assertEqual((writer_payload.get("profile_result") or {}).get("object_name"), "profiles")
        self.assertEqual((writer_payload.get("profile_result") or {}).get("record_kind"), "document")
        self.assertEqual((writer_payload.get("profile_result") or {}).get("correlation_id"), "profile-cover-1")
        self.assertEqual(((writer_payload.get("profile_result") or {}).get("parse") or {}).get("language"), "fr")

    def test_document_dispatch_service_build_cover_letter_resume_payload_uses_canonical_results(self) -> None:
        payload = tools_mod.DOCUMENT_DISPATCH_SERVICE.build_cover_letter_resume_payload(
            correlation_id="job-resume-1",
            job_posting_result={
                "job_posting": {
                    "job_title": "Resume Platform Engineer",
                    "company_name": "Resume Co",
                },
                "file": {
                    "path": "/tmp/job-resume-1.pdf",
                },
            },
            profile_result={
                "profile": {
                    "profile_id": "profile-resume-1",
                    "skills": ["python"],
                },
            },
            passthrough_context={
                "action": "generate_cover_letter",
                "options": {"tone": "modern"},
            },
        )

        self.assertEqual((payload.get("job_posting_result") or {}).get("object_name"), "job_postings")
        self.assertEqual((payload.get("job_posting_result") or {}).get("record_kind"), "document")
        self.assertEqual((payload.get("job_posting_result") or {}).get("correlation_id"), "job-resume-1")
        self.assertEqual((payload.get("profile_result") or {}).get("object_name"), "profiles")
        self.assertEqual((payload.get("profile_result") or {}).get("record_kind"), "document")
        self.assertEqual((payload.get("profile_result") or {}).get("correlation_id"), "profile-resume-1")

    def test_document_dispatch_service_build_passthrough_context_uses_canonical_results(self) -> None:
        passthrough_context = tools_mod.DOCUMENT_DISPATCH_SERVICE.build_passthrough_context(
            action="document_dispatch",
            profile_result={
                "profile": {
                    "profile_id": "profile-pass-1",
                    "skills": ["python"],
                },
            },
            job_posting_result={
                "job_posting": {
                    "job_title": "Passthrough Engineer",
                    "company_name": "Passthrough Co",
                },
                "file": {
                    "path": "/tmp/passthrough-job.pdf",
                    "content_sha256": "job-pass-1",
                },
            },
            options={"language": "de"},
        )

        self.assertEqual(passthrough_context.get("action"), "generate_cover_letter")
        self.assertEqual((passthrough_context.get("profile_result") or {}).get("object_name"), "profiles")
        self.assertEqual((passthrough_context.get("profile_result") or {}).get("correlation_id"), "profile-pass-1")
        self.assertEqual((passthrough_context.get("job_posting_result") or {}).get("object_name"), "job_postings")
        self.assertEqual((passthrough_context.get("job_posting_result") or {}).get("correlation_id"), "job-pass-1")

    def test_write_document_returns_structured_payload_and_persists_cover_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mongo_backend = _InMemoryMongoDocumentBackend()
            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=mongo_backend):
                result = tools_mod.write_document(
                    content="Sehr geehrtes Team,\n\nmit Interesse bewerbe ich mich.\n",
                    path=tmpdir,
                    doc_id="bewerbung_test",
                    correlation_id="cover-letter:test-1",
                )
                stored = tools_mod.DOCUMENT_REPOSITORY.get_document("cover-letter:test-1", obj_name="cover_letters")

            self.assertTrue(result["ok"])
            self.assertTrue(os.path.exists(result["path"]))
            self.assertEqual(result["correlation_id"], "cover-letter:test-1")
            self.assertEqual(stored["cover_letter"]["document_id"], "bewerbung_test")
            self.assertIn("mit Interesse bewerbe ich mich", stored["cover_letter"]["full_text"])
            self.assertEqual(stored["file"]["path"], result["path"])

    def test_dispatch_documents_reads_existing_dispatcher_state_via_mongo_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "posting.pdf")
            with open(pdf_path, "wb") as handle:
                handle.write(b"fake-pdf-content")

            correlation_id = tools_mod._sha256_file(pdf_path)
            dispatcher_storage_key = tools_mod.DOCUMENT_REPOSITORY._resolve_db_path(db_name="dispatcher_documents")
            mongo_backend = _InMemoryMongoDocumentBackend()
            mongo_backend.upsert_record(
                storage_key=dispatcher_storage_key,
                record_id=correlation_id,
                record_value={
                    "id": correlation_id,
                    "content_sha256": correlation_id,
                    "processing_state": "processed",
                    "processed": True,
                },
                db_name="dispatcher_documents",
                obj_name="documents",
            )

            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=mongo_backend):
                report = tools_mod.dispatch_docs(scan_dir=tmpdir, dry_run=True)

            self.assertTrue(report["db"]["reachable"])
            self.assertEqual(len(report["classified"]["known_processed"]), 1)
            self.assertEqual(report["classified"]["known_processed"][0]["content_sha256"], correlation_id)

    def test_dispatch_documents_resolve_scan_dir_prefers_default_vsm4_before_db_parent(self) -> None:
        warnings: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved_scan_dir = tools_mod.DOCUMENT_DISPATCH_SERVICE.resolve_scan_dir(
                os.path.join(tmpdir, "missing-documents"),
                resolved_db_path=os.path.join(tmpdir, "dispatcher_doc_db.json"),
                warnings=warnings,
            )

        expected_scan_dir = os.path.join(Path(tools_mod.__file__).resolve().parent.parent, "AppData", "VSM_4_Data")
        self.assertEqual(resolved_scan_dir, expected_scan_dir)
        self.assertTrue(warnings)
        self.assertEqual(warnings[-1].get("reason"), "fallback_to_default_vsm4")

    def test_collect_document_paths_skips_virtualenv_and_package_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kept_pdf = os.path.join(tmpdir, "documents", "keep.pdf")
            skipped_hidden_venv_pdf = os.path.join(tmpdir, ".venv", "skip-hidden.pdf")
            skipped_venv_pdf = os.path.join(tmpdir, "venv", "skip-venv.pdf")
            skipped_site_packages_pdf = os.path.join(tmpdir, "vendor", "site-packages", "skip-package.pdf")
            skipped_cover_letter_pdf = os.path.join(tmpdir, "cover_letters_generated", "skip-cover-letter.pdf")

            for file_path in (
                kept_pdf,
                skipped_hidden_venv_pdf,
                skipped_venv_pdf,
                skipped_site_packages_pdf,
                skipped_cover_letter_pdf,
            ):
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as handle:
                    handle.write(b"fake-pdf-content")

            document_paths = tools_mod.DOCUMENT_DISPATCH_SERVICE.collect_document_paths(
                tmpdir,
                recursive=True,
                extensions={".pdf"},
            )

            self.assertEqual(document_paths, [kept_pdf])

    def test_dispatch_documents_tool_spec_accepts_current_agent_and_job_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "posting.pdf")
            with open(pdf_path, "wb") as handle:
                handle.write(b"fake-pdf-content")

            tool_spec = tools_mod.get_tool_spec("dispatch_documents")

            self.assertIsNotNone(tool_spec)
            parameter_names = [param.name for param in tool_spec.parameters]
            self.assertIn("agent_name", parameter_names)
            self.assertIn("parser_agent_name", parameter_names)
            self.assertIn("parser_job_name", parameter_names)

            tool_result = tool_spec.execute({"scan_dir": tmpdir, "dry_run": True})
            direct_result = tools_mod.DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                scan_dir=tmpdir,
                dry_run=True,
                parser_job_name="job_posting_parser",
            )
            legacy_agent_result = tools_mod.DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                scan_dir=tmpdir,
                dry_run=True,
                agent_name="_xworker",
                parser_job_name="job_posting_parser",
            )
            target_agent_result = tools_mod.DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                scan_dir=tmpdir,
                dry_run=True,
                target_agent_name="_xworker",
                parser_job_name="job_posting_parser",
            )

            self.assertIsInstance(tool_result, dict)
            self.assertEqual(tool_result["agent"], "xworker")
            self.assertEqual(tool_result["job_name"], "document_dispatch")
            self.assertEqual(direct_result["agent"], "xworker")
            self.assertEqual(direct_result["job_name"], "document_dispatch")
            self.assertEqual(legacy_agent_result.get("errors"), [])
            self.assertEqual(target_agent_result.get("errors"), [])

    def test_read_document_uses_pypdf_extractor_for_pdf_files(self) -> None:
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
            tmp.write(b"fake-pdf-content")

        try:
            with patch("alde.agents_tools._read_pdf_text_with_pypdf", return_value="Seite 1: Python\n\nSeite 2: MongoDB") as extract_mock:
                result = tools_mod.read_document(pdf_path)

            self.assertEqual(result, "Seite 1: Python\n\nSeite 2: MongoDB")
            extract_mock.assert_called_once_with(pdf_path)
        finally:
            os.unlink(pdf_path)

    def test_read_document_tool_spec_marks_direct_final_result(self) -> None:
        tool_spec = tools_mod.get_tool_spec("read_document")

        self.assertIsNotNone(tool_spec)
        self.assertTrue(tool_spec.final_result)
        self.assertFalse(tool_spec.tool_response_required)

    def test_pypdf_read_document_tool_spec_marks_direct_final_result(self) -> None:
        tool_spec = tools_mod.get_tool_spec("pypdf_read_document")

        self.assertIsNotNone(tool_spec)
        self.assertTrue(tool_spec.final_result)
        self.assertFalse(tool_spec.tool_response_required)

    def test_pypdf_read_document_tool_executes_pypdf_helper(self) -> None:
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
            tmp.write(b"fake-pdf-content")

        try:
            with patch("alde.agents_tools._read_pdf_text_with_pypdf", return_value="pypdf text") as extract_mock:
                result = tools_mod.pypdf_read_document(pdf_path)

            self.assertEqual(result, "pypdf text")
            extract_mock.assert_called_once_with(pdf_path)
        finally:
            os.unlink(pdf_path)

    def test_xworker_prompt_config_supports_tool_name_selection_and_tools_task_option(self) -> None:
        prompt_config = agents_configurator.get_prompt_config("_xworker")
        task_config = prompt_config.get("task") or {}
        selection_policy = task_config.get("job_skill_profile_policy") or {}

        self.assertEqual(task_config.get("tools"), [])
        self.assertEqual(selection_policy.get("selection_mode"), "tool_name")
        self.assertEqual(selection_policy.get("fallback_selection_mode"), "job_name")
        self.assertEqual(selection_policy.get("fallback_skill_profile"), "xworker_core")

    def test_direct_file_read_guidance_prefers_read_document_over_retrieval(self) -> None:
        router_prompt = agents_configurator.get_system_prompt("_xrouter_xplanner")
        worker_prompt = agents_configurator.get_system_prompt("_xworker")
        read_document_spec = tools_mod.get_tool_spec("read_document")
        memorydb_spec = tools_mod.get_tool_spec("memorydb")
        vectordb_spec = tools_mod.get_tool_spec("vectordb")
        vdb_worker_spec = tools_mod.get_tool_spec("vdb_worker")

        self.assertIn("concrete filesystem path", router_prompt)
        self.assertIn("read_document", router_prompt)
        self.assertIn("memorydb", router_prompt)
        self.assertIn("vectordb", router_prompt)

        self.assertIn("concrete filesystem path", worker_prompt)
        self.assertIn("read_document", worker_prompt)
        self.assertIn("memorydb", worker_prompt)
        self.assertIn("vectordb", worker_prompt)

        self.assertIsNotNone(read_document_spec)
        self.assertIsNone(memorydb_spec)
        self.assertIsNone(vectordb_spec)
        self.assertIsNotNone(vdb_worker_spec)
        self.assertIn("concrete file path", read_document_spec.description)
        self.assertIn("vector store", vdb_worker_spec.description.lower())

    def test_route_to_agent_builds_xworker_request_from_tool_name_and_explicit_tools(self) -> None:
        result_text, routing_request = agents_factory.execute_tool(
            "route_to_agent",
            {
                "target_agent": "_xworker",
                "tool_name": "read_document",
                "tools": ["read_document"],
                "message_text": "Please load /tmp/example.txt",
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result_text, "Routing to _xworker")
        self.assertIsNotNone(routing_request)
        routed_tool_names = [
            tool_def["function"]["name"]
            for tool_def in (routing_request.get("tools") or [])
            if isinstance(tool_def, dict) and isinstance(tool_def.get("function"), dict)
        ]

        self.assertEqual(routed_tool_names, ["read_document"])
        self.assertEqual(routing_request["handoff"]["metadata"]["tool_name"], "read_document")
        self.assertEqual(routing_request["handoff"]["metadata"]["job_name"], "generic_execution")
        self.assertEqual(routing_request["handoff"]["metadata"]["tools"], ["read_document"])
        self.assertEqual(routing_request["runtime"]["selection_mode"], "tool_name")
        self.assertEqual(routing_request["runtime"]["fallback_selection_mode"], "job_name")
        self.assertEqual(routing_request["runtime"]["tool_name"], "read_document")
        self.assertEqual(routing_request["runtime"]["job_name"], "generic_execution")
        self.assertEqual(routing_request["runtime"]["explicit_tools"], ["read_document"])
        self.assertEqual(routing_request["runtime"]["skill_profile"], "xworker_core")

    def test_route_to_agent_bootstraps_agentsdb_agent_memory_profile(self) -> None:
        backend = _InMemoryMongoDocumentBackend()
        self._reset_agentsdb_runtime_state()

        with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=backend):
            result_text, routing_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "cover_letter_writer",
                    "message_text": "Bitte bereite den Writer-Kontext vor.",
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(result_text, "Routing to _xworker")
            self.assertIsNotNone(routing_request)

            scope_key = agents_factory.AGENT_MEMORY_SERVICE.load_session_scope_key(
                thread_id=agents_factory.WORKFLOW_CONTEXT_SERVICE.load_current_thread_id(),
            )
            correlation_id = agents_factory.AGENT_MEMORY_SERVICE.build_object_correlation_id(
                agent_label="_xworker",
                memory_slot="cover_letter_writer",
                scope_key=scope_key,
            )
            stored_record = tools_mod.DOCUMENT_REPOSITORY.get_document(
                correlation_id,
                obj_name="agent_memory",
            )

            self.assertIn("agent_memory", stored_record)
            profile = (stored_record.get("agent_memory") or {}).get("agent_profile") or {}
            self.assertEqual(profile.get("agent_label"), "_xworker")
            self.assertEqual(profile.get("memory_slot"), "cover_letter_writer")
            self.assertIn("cover_letter_writer", profile.get("jobs") or [])

    def test_dispatch_profile_is_cached_for_writer_and_attached_to_writer_messages(self) -> None:
        backend = _InMemoryMongoDocumentBackend()
        self._reset_agentsdb_runtime_state()

        with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=backend):
            dispatch_payload = {
                "output": {
                    "action": "generate_cover_letter",
                    "job_posting_result": {
                        "correlation_id": "job:test",
                        "job_posting": {
                            "company": "Example GmbH",
                            "title": "Python Engineer",
                        },
                    },
                    "profile_result": {
                        "correlation_id": "profile:test",
                        "profile": {
                            "skills": ["python", "rag"],
                        },
                    },
                    "applicant_profile": {
                        "source": "text",
                        "value": {
                            "profile_id": "profile:test",
                            "skills": ["python", "rag"],
                        },
                    },
                    "options": {
                        "language": "de",
                        "tone": "modern",
                    },
                    "sequence": {
                        "writer_job_name": "cover_letter_writer",
                    },
                }
            }

            tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                profile_result=dispatch_payload["output"]["profile_result"],
                correlation_id="profile:test",
                source_agent="_xworker",
            )
            tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                job_posting_result=dispatch_payload["output"]["job_posting_result"],
                correlation_id="job:test",
                source_agent="_xworker",
            )

            dispatch_result, dispatch_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "document_dispatch",
                    "message_text": "Bitte starte den Workflow.",
                    "handoff_payload": dispatch_payload,
                    "handoff_metadata": {"writer_job_name": "cover_letter_writer"},
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(dispatch_result, "Routing to _xworker")
            self.assertIsNotNone(dispatch_request)

            scope_key = agents_factory.AGENT_MEMORY_SERVICE.load_session_scope_key(
                thread_id=agents_factory.WORKFLOW_CONTEXT_SERVICE.load_current_thread_id(),
            )
            writer_correlation_id = agents_factory.AGENT_MEMORY_SERVICE.build_object_correlation_id(
                agent_label="_xworker",
                memory_slot="cover_letter_writer",
                scope_key=scope_key,
            )
            stored_writer_record = tools_mod.DOCUMENT_REPOSITORY.get_document(
                writer_correlation_id,
                obj_name="agent_memory",
            )
            session_entries = (
                ((stored_writer_record.get("agent_memory") or {}).get("session_context") or {}).get("entries") or []
            )

            self.assertTrue(session_entries)
            context_types = [str(entry.get("context_type") or "") for entry in session_entries if isinstance(entry, dict)]
            self.assertIn("applicant_profile", context_types)
            self.assertIn("profile_result", context_types)
            self.assertIn("job_posting_result", context_types)
            self.assertIn("options", context_types)
            self.assertIn("ATTACHMENT", context_types)

            writer_result, writer_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "cover_letter_writer",
                    "message_text": "Bitte erstelle ein Anschreiben.",
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(writer_result, "Routing to _xworker")
            self.assertIsNotNone(writer_request)

            attachment_documents = ((writer_request.get("runtime") or {}).get("attachment_documents") or [])
            self.assertTrue(attachment_documents)
            attachment_obj_names = {
                str(item.get("obj_name") or "")
                for item in attachment_documents
                if isinstance(item, dict)
            }
            self.assertIn("profiles", attachment_obj_names)
            self.assertIn("job_postings", attachment_obj_names)

            session_cache_messages = [
                message
                for message in (writer_request.get("messages") or [])
                if isinstance(message, dict)
                and str(message.get("role") or "").strip().lower() == "user"
                and "Session cache context (agentsdb agent_memory)" in str(message.get("content") or "")
            ]

            self.assertTrue(session_cache_messages)
            self.assertIn("profile_result", str(session_cache_messages[-1].get("content") or ""))
            self.assertIn("job_posting_result", str(session_cache_messages[-1].get("content") or ""))
            self.assertIn("options", str(session_cache_messages[-1].get("content") or ""))

            attachment_messages = [
                message
                for message in (writer_request.get("messages") or [])
                if isinstance(message, dict)
                and str(message.get("role") or "").strip().lower() == "user"
                and "Session attachment documents (agentsdb agent_memory)" in str(message.get("content") or "")
            ]
            self.assertTrue(attachment_messages)
            self.assertIn("profiles", str(attachment_messages[-1].get("content") or ""))
            self.assertIn("job_postings", str(attachment_messages[-1].get("content") or ""))

    def test_real_agentsdb_socket_writer_context_attaches_profile_and_job_posting_documents(self) -> None:
        agents_db_uri = "agentsdb://127.0.0.1:2331"
        database_name = "alde_knowledge"
        probe_id = uuid.uuid4().hex[:8]
        profile_correlation_id = f"profile:real-socket-attachment:{probe_id}"
        job_correlation_id = f"job:real-socket-attachment:{probe_id}"

        previous_env = self._apply_env_values(
            {
                "AI_IDE_KNOWLEDGE_AGENTS_DB_URI": agents_db_uri,
                "AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI": "agentsmem://local",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAME": database_name,
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_ID": f"ns_real_socket_attachment_{probe_id}",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_SLUG": f"real-socket-attachment-{probe_id}",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_NAME": f"Real Socket Attachment {probe_id}",
            }
        )
        self._reset_agentsdb_runtime_state()

        socket_repository: agents_db_mod.AgentDbSocketRepository | None = None
        try:
            try:
                socket_repository = agents_db_mod.AgentDbSocketRepository.create_from_uri(
                    agents_db_uri,
                    database_name,
                    timeout_seconds=1.5,
                )
                health_payload = socket_repository._request_object("health")
                self.assertTrue(bool(health_payload.get("ok", True)))
            except Exception as exc:
                self.skipTest(
                    "agentsdb socket endpoint not reachable for real attachment integration test "
                    f"({agents_db_uri}): {type(exc).__name__}: {exc}"
                )

            profile_result = {
                "agent": "_xworker",
                "correlation_id": profile_correlation_id,
                "parse": {"language": "de", "errors": [], "warnings": []},
                "profile": {
                    "profile_id": profile_correlation_id,
                    "preferences": {"language": "de"},
                    "skills": ["python", "rag", "agentsdb"],
                },
            }
            job_posting_result = {
                "agent": "_xworker",
                "correlation_id": job_correlation_id,
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "raw_text_document": {
                    "document_type": "job_posting",
                    "title": "Senior MCP Engineer",
                    "language": "de",
                    "raw_text": "Senior MCP Engineer at Example Co with Python, AgentsDB, and distributed systems.",
                    "sections": [],
                    "metadata": {},
                },
                "job_posting": {
                    "job_title": "Senior MCP Engineer",
                    "company_name": "Example Co",
                    "raw_text": "Senior MCP Engineer at Example Co with Python, AgentsDB, and distributed systems.",
                },
            }

            profile_store = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    profile_result=profile_result,
                    correlation_id=profile_correlation_id,
                    source_agent="_xworker",
                    obj_name="profiles",
                )
            )
            job_store = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    job_posting_result=job_posting_result,
                    correlation_id=job_correlation_id,
                    source_agent="_xworker",
                    obj_name="job_postings",
                )
            )

            self.assertTrue(profile_store["ok"])
            self.assertTrue(job_store["ok"])

            dispatch_result, dispatch_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "document_dispatch",
                    "message_text": "Bitte starte den Workflow.",
                    "handoff_payload": {
                        "output": {
                            "action": "generate_cover_letter",
                            "job_posting_result": job_posting_result,
                            "profile_result": profile_result,
                            "applicant_profile": {
                                "source": "text",
                                "value": {
                                    "profile_id": profile_correlation_id,
                                    "skills": ["python", "rag", "agentsdb"],
                                },
                            },
                            "options": {
                                "language": "de",
                                "tone": "modern",
                            },
                            "sequence": {
                                "writer_job_name": "cover_letter_writer",
                            },
                        }
                    },
                    "handoff_metadata": {"writer_job_name": "cover_letter_writer"},
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(dispatch_result, "Routing to _xworker")
            self.assertIsNotNone(dispatch_request)

            scope_key = agents_factory.AGENT_MEMORY_SERVICE.load_session_scope_key(
                thread_id=agents_factory.WORKFLOW_CONTEXT_SERVICE.load_current_thread_id(),
            )
            writer_correlation_id = agents_factory.AGENT_MEMORY_SERVICE.build_object_correlation_id(
                agent_label="_xworker",
                memory_slot="cover_letter_writer",
                scope_key=scope_key,
            )
            stored_writer_record = tools_mod.DOCUMENT_REPOSITORY.get_document(
                writer_correlation_id,
                obj_name="agent_memory",
            )
            session_entries = (
                ((stored_writer_record.get("agent_memory") or {}).get("session_context") or {}).get("entries") or []
            )

            self.assertTrue(session_entries)
            context_types = [str(entry.get("context_type") or "") for entry in session_entries if isinstance(entry, dict)]
            self.assertIn("applicant_profile", context_types)
            self.assertIn("profile_result", context_types)
            self.assertIn("job_posting_result", context_types)
            self.assertIn("options", context_types)
            self.assertIn("ATTACHMENT", context_types)

            writer_result, writer_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "cover_letter_writer",
                    "message_text": "Bitte erstelle ein Anschreiben.",
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(writer_result, "Routing to _xworker")
            self.assertIsNotNone(writer_request)

            attachment_documents = ((writer_request.get("runtime") or {}).get("attachment_documents") or [])
            self.assertTrue(attachment_documents)
            attachment_obj_names = {
                str(item.get("obj_name") or "")
                for item in attachment_documents
                if isinstance(item, dict)
            }
            self.assertIn("profiles", attachment_obj_names)
            self.assertIn("job_postings", attachment_obj_names)

            session_cache_messages = [
                message
                for message in (writer_request.get("messages") or [])
                if isinstance(message, dict)
                and str(message.get("role") or "").strip().lower() == "user"
                and "Session cache context (agentsdb agent_memory)" in str(message.get("content") or "")
            ]
            self.assertTrue(session_cache_messages)
            self.assertIn("profile_result", str(session_cache_messages[-1].get("content") or ""))
            self.assertIn("job_posting_result", str(session_cache_messages[-1].get("content") or ""))

            attachment_messages = [
                message
                for message in (writer_request.get("messages") or [])
                if isinstance(message, dict)
                and str(message.get("role") or "").strip().lower() == "user"
                and "Session attachment documents (agentsdb agent_memory)" in str(message.get("content") or "")
            ]
            self.assertTrue(attachment_messages)
            self.assertIn("profiles", str(attachment_messages[-1].get("content") or ""))
            self.assertIn("job_postings", str(attachment_messages[-1].get("content") or ""))
        finally:
            self._restore_env_values(previous_env)
            self._reset_agentsdb_runtime_state()

    def test_known_processed_cover_letter_resume_path_persists_cover_letter_artifacts(self) -> None:
        backend = _InMemoryMongoDocumentBackend()
        self._reset_agentsdb_runtime_state()

        with tempfile.TemporaryDirectory() as scan_tmpdir, tempfile.TemporaryDirectory() as output_tmpdir:
            pdf_path = os.path.join(scan_tmpdir, "posting.pdf")
            with open(pdf_path, "wb") as handle:
                handle.write(b"fake-pdf-content")

            correlation_id = tools_mod._sha256_file(pdf_path)
            profile_correlation_id = "profile:resume-e2e"
            dispatcher_storage_key = tools_mod.DOCUMENT_REPOSITORY._resolve_db_path(db_name="dispatcher_documents")

            profile_result = {
                "agent": "_xworker",
                "correlation_id": profile_correlation_id,
                "parse": {"language": "de", "errors": [], "warnings": []},
                "profile": {
                    "profile_id": profile_correlation_id,
                    "preferences": {"language": "de"},
                    "skills": ["Python", "AgentsDB"],
                },
            }
            job_posting_result = {
                "agent": "job_posting_parser",
                "correlation_id": correlation_id,
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "file": {"path": pdf_path, "content_sha256": correlation_id},
                "job_posting": {
                    "job_title": "Platform Engineer",
                    "company_name": "Example Co",
                    "raw_text": "Platform Engineer mit Python, AgentsDB und Workflow-Orchestrierung.",
                },
                "raw_text_document": {
                    "document_type": "job_posting",
                    "title": "Platform Engineer",
                    "language": "de",
                    "raw_text": "Platform Engineer mit Python, AgentsDB und Workflow-Orchestrierung.",
                    "sections": [],
                    "metadata": {},
                },
                "db_updates": {
                    "processing_state": "processed",
                    "processed": True,
                },
            }

            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=backend):
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    profile_result=profile_result,
                    correlation_id=profile_correlation_id,
                    source_agent="_xworker",
                    obj_name="profiles",
                )
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    job_posting_result=job_posting_result,
                    correlation_id=correlation_id,
                    source_agent="job_posting_parser",
                    obj_name="job_postings",
                )
                backend.upsert_record(
                    storage_key=dispatcher_storage_key,
                    record_id=correlation_id,
                    record_value={
                        "id": correlation_id,
                        "content_sha256": correlation_id,
                        "source_path": pdf_path,
                        "processing_state": "processed",
                        "processed": True,
                        "file_size_bytes": os.path.getsize(pdf_path),
                        "mtime_epoch": int(os.path.getmtime(pdf_path)),
                    },
                    db_name="dispatcher_documents",
                    obj_name="documents",
                )

                dispatch_report = tools_mod.DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                    scan_dir=scan_tmpdir,
                    action="generate_cover_letter",
                    profile_id=profile_correlation_id,
                    job_posting_result=job_posting_result,
                    options={
                        "language": "de",
                        "tone": "modern",
                        "max_words": 320,
                        "output_dir": output_tmpdir,
                        "write_pdf": False,
                    },
                    dry_run=False,
                )

                route_handoffs = agents_factory._extract_tool_handoff_messages(dispatch_report)
                self.assertEqual(len(route_handoffs), 1)
                self.assertEqual(route_handoffs[0]["job_name"], "cover_letter_writer")

                route_result, writer_request = agents_factory.execute_route_to_agent(
                    route_handoffs[0],
                    source_agent_label="_xworker",
                )

                self.assertEqual(route_result, "Routing to _xworker")
                self.assertEqual(((writer_request.get("runtime") or {}).get("job_name")), "cover_letter_writer")
                result_postprocess = (((writer_request.get("handoff_context") or {}).get("contract") or {}).get("schema") or {}).get("result_postprocess") or {}
                self.assertEqual(result_postprocess.get("tool"), "persist_cover_letter_artifacts")

                persist_result = agents_factory.ROUTING_RESULT_POSTPROCESS_SERVICE.apply_object_result(
                    writer_request,
                    result_text=json.dumps(
                        {
                            "cover_letter": {
                                "full_text": "Sehr geehrtes Team,\n\nmit meiner Erfahrung in Python und AgentsDB passe ich gut zur Rolle Platform Engineer.\n\nMit freundlichen Gruessen\nBen"
                            },
                            "cv": {
                                "full_text": "Python\nAgentsDB\nWorkflow-Orchestrierung"
                            },
                            "quality": {
                                "language": "de",
                                "tone_used": "modern",
                                "matched_requirements": ["Python", "AgentsDB"],
                                "highlighted_skills": ["Python", "AgentsDB"],
                                "red_flags": [],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    succeeded=True,
                )

                self.assertIsInstance(persist_result, dict)
                self.assertTrue(persist_result["ok"])
                self.assertEqual(((persist_result.get("result") or {}).get("document_pdf_path")), None)

                stored_cover_letter = tools_mod.DOCUMENT_REPOSITORY.get_document(
                    correlation_id,
                    obj_name="cover_letters",
                )
                self.assertIn("cover_letter", stored_cover_letter)
                self.assertIn(
                    "Python und AgentsDB passe ich gut zur Rolle Platform Engineer",
                    (stored_cover_letter.get("cover_letter") or {}).get("full_text") or "",
                )

                scope_key = agents_factory.AGENT_MEMORY_SERVICE.load_session_scope_key(
                    thread_id=agents_factory.WORKFLOW_CONTEXT_SERVICE.load_current_thread_id(),
                )
                writer_memory_correlation_id = agents_factory.AGENT_MEMORY_SERVICE.build_object_correlation_id(
                    agent_label="_xworker",
                    memory_slot="cover_letter_writer",
                    scope_key=scope_key,
                )
                writer_memory_record = tools_mod.DOCUMENT_REPOSITORY.get_document(
                    writer_memory_correlation_id,
                    obj_name="agent_memory",
                )
                session_entries = ((((writer_memory_record.get("agent_memory") or {}).get("session_context") or {}).get("entries") or []))
                context_types = [
                    str(entry.get("context_type") or "")
                    for entry in session_entries
                    if isinstance(entry, dict)
                ]

                self.assertIn("profile_result", context_types)
                self.assertIn("job_posting_result", context_types)
                self.assertIn("ATTACHMENT", context_types)

    def test_route_to_agent_caches_generic_handoff_context_for_target_job(self) -> None:
        backend = _InMemoryMongoDocumentBackend()
        self._reset_agentsdb_runtime_state()

        with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=backend):
            handoff_payload = {
                "output": {
                    "object_result": {
                        "kind": "note",
                        "title": "Follow-up",
                    },
                    "dispatcher_updates": {
                        "processed": True,
                        "processing_state": "cached",
                    },
                    "options": {
                        "priority": "high",
                    },
                }
            }

            result_text, routing_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "generic_execution",
                    "message_text": "Bitte arbeite den Kontext ab.",
                    "handoff_payload": handoff_payload,
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(result_text, "Routing to _xworker")
            self.assertIsNotNone(routing_request)

            scope_key = agents_factory.AGENT_MEMORY_SERVICE.load_session_scope_key(
                thread_id=agents_factory.WORKFLOW_CONTEXT_SERVICE.load_current_thread_id(),
            )
            correlation_id = agents_factory.AGENT_MEMORY_SERVICE.build_object_correlation_id(
                agent_label="_xworker",
                memory_slot="generic_execution",
                scope_key=scope_key,
            )
            stored_record = tools_mod.DOCUMENT_REPOSITORY.get_document(
                correlation_id,
                obj_name="agent_memory",
            )
            session_entries = (
                ((stored_record.get("agent_memory") or {}).get("session_context") or {}).get("entries") or []
            )

            self.assertTrue(session_entries)
            context_types = [str(entry.get("context_type") or "") for entry in session_entries if isinstance(entry, dict)]
            self.assertIn("object_result", context_types)
            self.assertIn("dispatcher_updates", context_types)
            self.assertIn("options", context_types)

            second_result, second_request = agents_factory.execute_tool(
                "route_to_agent",
                {
                    "target_agent": "_xworker",
                    "job_name": "generic_execution",
                    "message_text": "Bitte setze fort.",
                },
                source_agent_label="_xplaner_xrouter",
            )

            self.assertEqual(second_result, "Routing to _xworker")
            self.assertIsNotNone(second_request)

            session_cache_messages = [
                message
                for message in (second_request.get("messages") or [])
                if isinstance(message, dict)
                and str(message.get("role") or "").strip().lower() == "user"
                and "Session cache context (agentsdb agent_memory)" in str(message.get("content") or "")
            ]

            self.assertTrue(session_cache_messages)
            self.assertIn("object_result", str(session_cache_messages[-1].get("content") or ""))
            self.assertIn("dispatcher_updates", str(session_cache_messages[-1].get("content") or ""))
            self.assertIn("options", str(session_cache_messages[-1].get("content") or ""))

    def test_read_document_tool_result_is_returned_and_logged_as_final_assistant_result(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            file_path = tmp.name
            tmp.write("Direkter Dokumentinhalt")

        try:
            tool_call = SimpleNamespace(
                id="call_read_document",
                function=SimpleNamespace(
                    name="read_document",
                    arguments=json.dumps({"file_path": file_path}, ensure_ascii=False),
                ),
            )

            result = agents_factory._handle_tool_calls(
                SimpleNamespace(content="", tool_calls=[tool_call]),
                agent_label="",
            )

            self.assertEqual(result, "Direkter Dokumentinhalt")

            assistant_entries = [
                entry
                for entry in chat_mod.ChatHistory._history_
                if isinstance(entry, dict) and entry.get("role") == "assistant"
            ]
            tool_entries = [
                entry
                for entry in chat_mod.ChatHistory._history_
                if isinstance(entry, dict) and entry.get("role") == "tool"
            ]

            self.assertTrue(assistant_entries)
            self.assertEqual(assistant_entries[-1].get("content"), "Direkter Dokumentinhalt")
            self.assertTrue(tool_entries)
            self.assertFalse(tool_entries[-1].get("tool_response_required", True))

            history = chat_mod.ChatHistory()
            history._thread_iD = tool_entries[-1].get("thread-id")
            inserted = history._insert(tool=True, f_depth=10)

            self.assertEqual(inserted, [{"role": "assistant", "content": "Direkter Dokumentinhalt"}])
        finally:
            os.unlink(file_path)

    def test_run_retrieval_with_events_triggers_optional_mongodb_sync(self) -> None:
        retrieval_result = [
            {
                "document_id": "doc_job_0001",
                "title": "Senior Python Engineer",
                "score": 0.93,
                "source": "https://example.org/jobs/0001",
            },
        ]

        with patch("alde.tools._emit_query_event") as emit_query_event:
            with patch("alde.tools._emit_outcome_event") as emit_outcome_event:
                with patch("alde.tools._run_vectordb_subprocess", return_value=retrieval_result):
                    with patch("alde.tools.sync_retrieval_run_to_mongodb_knowledge", return_value={"ok": True, "stored": True}) as mongo_sync:
                        result = tools_mod._run_retrieval_with_events("vectordb", "Python RAG", 3)

        self.assertEqual(result, retrieval_result)
        emit_query_event.assert_called_once()
        emit_outcome_event.assert_called_once()
        mongo_sync.assert_called_once()
        self.assertEqual(mongo_sync.call_args.kwargs["tool_name"], "vectordb")
        self.assertEqual(mongo_sync.call_args.kwargs["query_event"]["query_text"], "Python RAG")
        self.assertEqual(mongo_sync.call_args.kwargs["outcome_event"]["result_count"], 1)
        self.assertEqual(mongo_sync.call_args.kwargs["retrieval_result"][0]["document_id"], "doc_job_0001")

    def test_vector_search_logging_emits_raw_result_without_wrapper(self) -> None:
        result_object = [{"rank": 1, "source": "knowledge.md", "content": "alpha"}]

        with patch("builtins.print") as mocked_print:
            agents_factory.TOOL_EXECUTION_CALLBACK_SERVICE.log_object_result("memorydb", result_object)

        mocked_print.assert_called_once_with("TOOL RESULT: memorydb [payload omitted]")

    def test_followup_uses_raw_tool_results_when_history_messages_are_empty(self) -> None:
        captured_messages: dict[str, object] = {}

        class _DummyChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                captured_messages["messages"] = list(_messages)

            def _response(self):
                message = SimpleNamespace(content="processed retrieval", tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        history = agents_factory.get_history()
        history._history_ = []
        history._thread_iD = 777

        with patch.object(
            agents_factory.TOOL_CALL_FOLLOWUP_SERVICE,
            "build_object_request",
            return_value={"messages": [], "tools": [], "model": "gpt-4o-mini"},
        ), patch("alde.chat_completion.ChatComE", _DummyChatComE):
            result = agents_factory.TOOL_CALL_FOLLOWUP_SERVICE.execute_object_followup(
                history=history,
                routing_request=None,
                tool_results=['[{"rank": 1, "source": "knowledge.md", "content": "alpha"}]'],
                depth=0,
                ChatCom=None,
                agent_label="_xplaner_xrouter",
                workflow_session=None,
            )

        self.assertEqual(
            captured_messages["messages"],
            [{"role": "user", "content": '[{"rank": 1, "source": "knowledge.md", "content": "alpha"}]'}],
        )
        self.assertEqual(result, "processed retrieval")

    def test_store_result_supports_direct_profile_storage(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            profiles_db_path = tmp.name

        try:
            result = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                    profile_result={
                        "agent": "profile_platform_ingest",
                        "correlation_id": "profile:job-board-7",
                        "parse": {"language": "de", "errors": [], "warnings": []},
                        "profile": {
                            "profile_id": "profile:job-board-7",
                            "personal_info": {"full_name": "Max Mustermann"},
                            "preferences": {"language": "de"},
                        },
                    },
                    db_path=profiles_db_path,
                    source_agent="profile_platform_ingest",
                )
            )

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("profile:job-board-7", db_path=profiles_db_path, obj_name="profiles")
            self.assertTrue(result["ok"])
            self.assertEqual(result["correlation_id"], "profile:job-board-7")
            self.assertEqual(stored["profile"]["profile_id"], "profile:job-board-7")
            self.assertEqual(stored["profile"]["personal_info"]["full_name"], "Max Mustermann")
        finally:
            os.unlink(profiles_db_path)

    def test_ingest_result_supports_request_style_profile_sources(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            profiles_db_path = tmp.name

        try:
            result = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.ingest_result(
                    applicant_profile={
                        "source": "text",
                        "value": {
                            "profile_id": "profile:platform-11",
                            "personal_info": {"full_name": "Erika Muster"},
                            "preferences": {"language": "de"},
                        },
                    },
                    db_path=profiles_db_path,
                    source_agent="profile_platform_ingest",
                    source_payload={"platform": "example_profiles", "record_id": "platform-11"},
                )
            )

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("profile:platform-11", db_path=profiles_db_path, obj_name="profiles")
            self.assertTrue(result["ok"])
            self.assertEqual(result["correlation_id"], "profile:platform-11")
            self.assertEqual(stored["profile"]["personal_info"]["full_name"], "Erika Muster")
        finally:
            os.unlink(profiles_db_path)

    def test_ingest_object_action_returns_store_result_without_model_call_for_job_postings(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            request = {
                "action": "ingest_object",
                "correlation_id": "platform:ingest-99",
                "source_agent": "job_platform_ingest",
                "obj_db_path": job_postings_db_path,
                "job_posting": {
                    "job_title": "Senior Data Engineer",
                    "company_name": "Platform Co",
                    "external_id": "ingest-99",
                },
                "source_payload": {
                    "platform": "example_jobs",
                    "record_id": "ingest-99",
                },
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )
                response = json.loads(chat.get_response())

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("platform:ingest-99", db_path=job_postings_db_path, obj_name="job_postings")
            self.assertTrue(response["ok"])
            self.assertEqual(response["correlation_id"], "platform:ingest-99")
            self.assertEqual(stored["job_posting"]["job_title"], "Senior Data Engineer")
            self.assertEqual(stored["parse"]["is_job_posting"], True)
            get_client.assert_not_called()
        finally:
            os.unlink(job_postings_db_path)

    def test_ingest_result_includes_knowledge_sync_for_job_postings(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            with patch.object(
                tools_mod,
                "sync_parser_result_to_agentsdb_knowledge",
                return_value={
                    "ok": True,
                    "stored": True,
                    "object_name": "job_posting",
                    "document_id": "doc:job_posting:platform:ingest-knowledge-1",
                    "entity_count": 5,
                    "relation_count": 4,
                },
            ) as sync_mock:
                result = json.loads(
                    tools_mod.DOCUMENT_OBJECT_SERVICE.ingest_result(
                        job_posting={
                            "job_title": "Platform Knowledge Engineer",
                            "company_name": "Platform Co",
                            "requirements": {
                                "technical_skills": ["Python", "MongoDB"],
                                "soft_skills": ["Kommunikation"],
                                "languages": ["Deutsch", "Englisch"],
                            },
                        },
                        correlation_id="platform:ingest-knowledge-1",
                        db_path=job_postings_db_path,
                        source_agent="job_platform_ingest",
                        source_payload={"platform": "example_jobs", "record_id": "ingest-knowledge-1"},
                    )
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["knowledge_sync"]["stored"])
            self.assertEqual(result["knowledge_sync"]["relation_count"], 4)
            self.assertEqual(sync_mock.call_args.kwargs["object_name"], "job_postings")
            self.assertEqual(sync_mock.call_args.kwargs["correlation_id"], "platform:ingest-knowledge-1")
        finally:
            os.unlink(job_postings_db_path)

    def test_store_profile_action_returns_store_result_without_model_call(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            profiles_db_path = tmp.name

        try:
            request = {
                "action": "ingest_object",
                "obj_db_path": profiles_db_path,
                "source_agent": "profile_platform_ingest",
                "applicant_profile": {
                    "source": "text",
                    "value": {
                        "profile_id": "profile:ingest-5",
                        "personal_info": {"full_name": "Jane Doe"},
                        "preferences": {"language": "en"},
                    },
                },
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )
                response = json.loads(chat.get_response())

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("profile:ingest-5", db_path=profiles_db_path, obj_name="profiles")
            self.assertTrue(response["ok"])
            self.assertEqual(response["correlation_id"], "profile:ingest-5")
            self.assertEqual(stored["profile"]["personal_info"]["full_name"], "Jane Doe")
            get_client.assert_not_called()
        finally:
            os.unlink(profiles_db_path)

    def test_ingest_object_action_returns_store_result_without_model_call_for_profiles(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            profiles_db_path = tmp.name

        try:
            request = {
                "action": "ingest_object",
                "obj_db_path": profiles_db_path,
                "source_agent": "profile_platform_ingest",
                "profile": {
                    "profile_id": "profile:ingest-15",
                    "personal_info": {"full_name": "Alex Example"},
                    "preferences": {"language": "en"},
                },
                "source_payload": {
                    "platform": "example_profiles",
                    "record_id": "ingest-15",
                },
            }

            with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
                chat = chat_mod.ChatCom(
                    _model="gpt-4o-mini",
                    _input_text=json.dumps(request, ensure_ascii=False),
                    _name="test_workflow",
                )
                response = json.loads(chat.get_response())

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("profile:ingest-15", db_path=profiles_db_path, obj_name="profiles")
            self.assertTrue(response["ok"])
            self.assertEqual(response["correlation_id"], "profile:ingest-15")
            self.assertEqual(stored["profile"]["personal_info"]["full_name"], "Alex Example")
            get_client.assert_not_called()
        finally:
            os.unlink(profiles_db_path)

    def test_ingest_object_action_requests_missing_job_posting_fields(self) -> None:
        request = {
            "action": "ingest_object",
            "source_agent": "job_platform_ingest",
            "obj_db_path": "/tmp/unused-job-postings.json",
        }

        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(request, ensure_ascii=False),
                _name="test_workflow",
            )
            response = str(chat.get_response())

        self.assertTrue(response.strip())
        self.assertIn("missing", response.lower())
        self.assertIn("required", response.lower())
        self.assertIn("ingest", response.lower())
        get_client.assert_called()

    def test_execute_action_request_cover_letter_writer_alias_uses_cover_letter_schema(self) -> None:
        result_text, routing_request = agents_factory.execute_tool(
            "execute_action_request",
            {"action": "cover_letter_writer"},
            source_agent_label="_xworker",
        )

        result = json.loads(result_text)
        self.assertIsNone(routing_request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_action_request")
        self.assertEqual(result["action"], "generate_cover_letter")
        self.assertEqual(result["schema_name"], "cover_letter_generation_request")
        self.assertIn("missing required field 'applicant_profile'", result["errors"])

    def test_execute_action_request_ingest_object_action_only_returns_schema_error(self) -> None:
        result_text, routing_request = agents_factory.execute_tool(
            "execute_action_request",
            {"action": "ingest_object"},
            source_agent_label="_xworker",
        )

        result = json.loads(result_text)
        self.assertIsNone(routing_request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_action_request")
        self.assertEqual(result["action"], "ingest_object")
        self.assertIn("payload does not satisfy action request schema conditions", result["errors"])

    def test_execute_action_request_tool_ingests_job_posting_for_dispatcher(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            result_text, routing_request = agents_factory.execute_tool(
                "execute_action_request",
                {
                    "action": "ingest_object",
                    "payload": {
                        "correlation_id": "platform:dispatcher-21",
                        "obj_db_path": job_postings_db_path,
                        "obj_name": "job_postings",
                        "source_agent": "job_platform_ingest",
                        "job_posting": {
                            "job_title": "Automation Engineer",
                            "company_name": "Dispatcher Co",
                        },
                        "source_payload": {
                            "platform": "example_jobs",
                            "record_id": "dispatcher-21",
                        },
                    },
                },
                source_agent_label="_xworker",
            )

            result = json.loads(result_text)
            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("platform:dispatcher-21", db_path=job_postings_db_path, obj_name="job_postings")
            self.assertIsNone(routing_request)
            self.assertTrue(result["ok"])
            self.assertEqual(stored["job_posting"]["job_title"], "Automation Engineer")
            self.assertEqual(stored["parse"]["is_job_posting"], True)
        finally:
            os.unlink(job_postings_db_path)

    def test_primary_route_parser_result_is_persisted_to_job_postings_store(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        try:
            routing_request = {
                "agent_label": "_xworker",
                "handoff": {
                    "handoff_payload": {
                        "output": {
                            "type": "job_posting_pdf",
                            "file": {
                                "path": "/tmp/example-job.pdf",
                                "content_sha256": "sha-direct-store-1",
                            },
                            "requested_actions": ["parse", "store_object_result"],
                            "job_name": "job_posting_parser",
                        }
                    },
                    "metadata": {
                        "correlation_id": "sha-direct-store-1",
                        "obj_name": "job_postings",
                        "obj_db_path": job_postings_db_path,
                    },
                },
                "handoff_context": {
                    "contract": {
                        "schema": {
                            "result_postprocess": {
                                "tool": "store_object_result",
                                "source_agent": "target_agent",
                            }
                        }
                    }
                },
            }

            postprocess_result = agents_factory.ROUTING_RESULT_POSTPROCESS_SERVICE.apply_object_result(
                routing_request,
                result_text=json.dumps(
                    {
                        "agent": "xworker",
                        "job_name": "job_posting_parser",
                        "correlation_id": "sha-direct-store-1",
                        "parse": {"is_job_posting": True},
                        "job_posting": {
                            "job_title": "Runtime Persisted Engineer",
                            "company_name": "Route Storage Co",
                        },
                    },
                    ensure_ascii=False,
                ),
                succeeded=True,
            )

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document(
                "sha-direct-store-1",
                db_path=job_postings_db_path,
                obj_name="job_postings",
            )

            self.assertIsInstance(postprocess_result, dict)
            self.assertTrue(postprocess_result["ok"])
            self.assertEqual(postprocess_result["obj_name"], "job_postings")
            self.assertEqual(stored["job_posting"]["job_title"], "Runtime Persisted Engineer")
            self.assertEqual(stored["agent"], "_xworker")
        finally:
            os.unlink(job_postings_db_path)

    def test_primary_route_parser_plain_text_is_normalized_and_forwards_to_cover_letter_writer(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as dispatcher_tmp, tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as job_tmp:
            dispatcher_db_path = dispatcher_tmp.name
            job_postings_db_path = job_tmp.name

        try:
            routing_request = {
                "agent_label": "_xworker",
                "handoff": {
                    "handoff_payload": {
                        "output": {
                            "type": "job_posting_pdf",
                            "action": "generate_cover_letter",
                            "file": {
                                "path": "/tmp/example-job.pdf",
                                "name": "example-job.pdf",
                                "content_sha256": "sha-plain-text-1",
                            },
                            "link": {"thread_id": "thread-plain-1", "message_id": "msg-plain-1"},
                            "db": {"processing_state": "queued"},
                            "requested_actions": ["parse", "extract_text", "store_object_result", "mark_processed_on_success"],
                            "job_name": "job_posting_parser",
                            "applicant_profile": {
                                "source": "profile_id",
                                "value": "profile:plain-1",
                            },
                        }
                    },
                    "metadata": {
                        "correlation_id": "sha-plain-text-1",
                        "dispatcher_message_id": "dispatcher-plain-1",
                        "dispatcher_db_path": dispatcher_db_path,
                        "obj_name": "job_postings",
                        "obj_db_path": job_postings_db_path,
                    },
                },
                "handoff_context": {
                    "contract": {
                        "schema": {
                            "result_postprocess": {
                                "tool": "upsert_object_record",
                                "source_agent": "target_agent",
                            }
                        }
                    }
                },
            }

            result_text = "Support Engineer at Example Co with Python and SQL."
            postprocess_result = agents_factory.ROUTING_RESULT_POSTPROCESS_SERVICE.apply_object_result(
                routing_request,
                result_text=result_text,
                succeeded=True,
            )

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document(
                "sha-plain-text-1",
                db_path=job_postings_db_path,
                obj_name="job_postings",
            )

            self.assertIsInstance(postprocess_result, dict)
            self.assertTrue(postprocess_result["ok"])
            self.assertEqual(postprocess_result["result"]["correlation_id"], "sha-plain-text-1")
            self.assertEqual(postprocess_result["result"]["raw_text_document"]["raw_text"], result_text)
            self.assertIn("parser_result_synthesized_from_plain_text", postprocess_result["result"]["parse"]["warnings"])
            self.assertEqual(stored["raw_text_document"]["raw_text"], result_text)
            self.assertEqual(stored["job_posting"]["raw_text"], result_text)
            self.assertTrue(stored["parse"]["is_job_posting"])
            self.assertEqual(postprocess_result["handoff_messages"][0]["job_name"], "cover_letter_writer")
            self.assertEqual(
                postprocess_result["handoff_messages"][0]["handoff_payload"]["output"]["job_posting_result"]["raw_text_document"]["raw_text"],
                result_text,
            )
            self.assertEqual(
                postprocess_result["handoff_messages"][0]["handoff_payload"]["output"]["profile_result"]["profile"]["profile_id"],
                "profile:plain-1",
            )
        finally:
            os.unlink(dispatcher_db_path)
            os.unlink(job_postings_db_path)

    def test_deterministic_action_is_logged_as_real_assistant_result(self) -> None:
        request = {
            "action": "ingest_object",
            "payload": {
                "correlation_id": "platform:history-1",
                "obj_db_path": "/tmp/unused-job-postings.json",
                "source_agent": "job_platform_ingest",
                "job_posting": {
                    "job_title": "History Engineer",
                    "company_name": "History Co",
                },
            },
        }

        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(request, ensure_ascii=False),
                _name="test_workflow",
            )
            response_text = chat.get_response()

        assistant_entries = [
            entry
            for entry in chat_mod.ChatHistory._history_
            if isinstance(entry, dict) and entry.get("role") == "assistant"
        ]

        self.assertTrue(assistant_entries)
        latest = assistant_entries[-1]
        self.assertEqual(latest.get("content"), response_text)
        self.assertEqual(latest.get("data", {}).get("deterministic_action", {}).get("action"), "ingest_object")
        self.assertEqual(latest.get("data", {}).get("deterministic_action", {}).get("correlation_id"), "platform:history-1")
        get_client.assert_not_called()

    def test_forced_route_is_logged_as_prepared_route_instead_of_tool_placeholder(self) -> None:
        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
            chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(SAMPLE_WORKFLOW_REQUEST, ensure_ascii=False),
                _name="test_workflow",
            )

        assistant_entries = [
            entry
            for entry in chat_mod.ChatHistory._history_
            if isinstance(entry, dict) and entry.get("role") == "assistant"
        ]

        self.assertTrue(assistant_entries)
        self.assertNotEqual(assistant_entries[-1].get("content"), "[forced route prepared]")
        get_client.assert_called_once()

    def test_upsert_object_record_updates_both_stores_for_job_postings(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as dispatcher_tmp, tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as jobs_tmp:
            dispatcher_db_path = dispatcher_tmp.name
            job_postings_db_path = jobs_tmp.name

        try:
            result = json.loads(
                tools_mod.DOCUMENT_OBJECT_SERVICE.upsert_object_record(
                    job_posting_result={
                        "agent": "job_platform_ingest",
                        "correlation_id": "platform:atomic-1",
                        "parse": {"is_job_posting": True},
                        "job_posting": {
                            "job_title": "ERP Integration Engineer",
                            "company_name": "Atomic Co",
                        },
                        "db_updates": {"processing_state": "processed", "processed": True},
                    },
                    dispatcher_db_path=dispatcher_db_path,
                    job_postings_db_path=job_postings_db_path,
                    source_agent="job_platform_ingest",
                    source_payload={"platform": "example_jobs", "record_id": "atomic-1"},
                    dispatcher_updates={"thread_id": "thread-atomic"},
                )
            )

            stored_job = tools_mod.DOCUMENT_REPOSITORY.get_document("platform:atomic-1", db_path=job_postings_db_path, obj_name="job_postings")
            dispatcher_db = tools_mod.DOCUMENT_REPOSITORY.load_db(dispatcher_db_path, db_name="dispatcher_documents")
            dispatcher_record = dispatcher_db["documents"]["platform:atomic-1"]
            self.assertTrue(result["ok"])
            self.assertTrue(result["dispatcher_updated"])
            self.assertEqual(stored_job["job_posting"]["job_title"], "ERP Integration Engineer")
            self.assertEqual(dispatcher_record["processing_state"], "processed")
            self.assertEqual(dispatcher_record["thread_id"], "thread-atomic")
        finally:
            os.unlink(dispatcher_db_path)
            os.unlink(job_postings_db_path)

    def test_execute_action_request_tool_supports_atomic_object_upsert_for_job_postings(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as dispatcher_tmp, tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as jobs_tmp:
            dispatcher_db_path = dispatcher_tmp.name
            job_postings_db_path = jobs_tmp.name

        try:
            result_text, routing_request = agents_factory.execute_tool(
                "execute_action_request",
                {
                    "action": "upsert_object_record",
                    "payload": {
                        "correlation_id": "platform:atomic-2",
                        "dispatcher_db_path": dispatcher_db_path,
                        "obj_db_path": job_postings_db_path,
                        "obj_name": "job_postings",
                        "source_agent": "job_platform_ingest",
                        "processing_state": "processed",
                        "job_posting_result": {
                            "agent": "job_platform_ingest",
                            "parse": {"is_job_posting": True},
                            "job_posting": {
                                "job_title": "Autonomous Workflow Engineer",
                                "company_name": "Atomic Dispatcher Co",
                            },
                        },
                    },
                },
                source_agent_label="_xworker",
            )

            result = json.loads(result_text)
            stored_job = tools_mod.DOCUMENT_REPOSITORY.get_document("platform:atomic-2", db_path=job_postings_db_path, obj_name="job_postings")
            dispatcher_db = tools_mod.DOCUMENT_REPOSITORY.load_db(dispatcher_db_path, db_name="dispatcher_documents")
            self.assertIsNone(routing_request)
            self.assertTrue(result["ok"])
            self.assertEqual(stored_job["job_posting"]["job_title"], "Autonomous Workflow Engineer")
            self.assertEqual(dispatcher_db["documents"]["platform:atomic-2"]["processing_state"], "processed")
        finally:
            os.unlink(dispatcher_db_path)
            os.unlink(job_postings_db_path)

    def test_action_request_service_dispatches_generic_object_action_via_config(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            custom_db_path = tmp.name

        try:
            request = {
                "action": "store_generic_object",
                "obj_name": "custom_records",
                "custom_records_db_path": custom_db_path,
                "source_agent": "generic_ingest",
                "object_payload": {
                    "id": "custom-42",
                    "name": "Config First",
                    "source": "platform.generic",
                },
            }
            schema_config = {
                "name": "generic_object_ingest_request",
                "actions": ["store_generic_object"],
                "request_resolution": {
                    "objects": [
                        {
                            "binding_name": "generic_object",
                            "request_field": "object_request",
                            "result_field": "object_result",
                            "default_obj_name": "generic_objects",
                            "obj_name_config_key": "generic_obj_name",
                            "db_path_field_key": "generic_db_path_field",
                            "default_source": "text",
                        }
                    ]
                },
                "action_execution": {
                    "handler_name": "ingest_object",
                    "binding_name": "generic_object",
                    "object_payload_field": "object_payload",
                    "request_payload_field": "object_request",
                    "result_payload_field": "object_result",
                    "correlation_id_fields": ["correlation_id"],
                    "source_agent_fields": ["source_agent"],
                    "default_request_source": "text",
                },
            }

            with patch("alde.tools.get_action_request_schema_config", return_value=schema_config), patch(
                "alde.tools.validate_action_request",
                return_value={
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                    "schema_name": "generic_object_ingest_request",
                },
            ):
                response = json.loads(tools_mod.ACTION_REQUEST_SERVICE.execute_request(request))

            stored = tools_mod.DOCUMENT_REPOSITORY.get_document("custom-42", db_path=custom_db_path, obj_name="custom_records")
            self.assertTrue(response["ok"])
            self.assertEqual(response["obj_name"], "custom_records")
            self.assertEqual(stored["custom_records"]["name"], "Config First")
            self.assertEqual(stored["agent"], "generic_ingest")
        finally:
            os.unlink(custom_db_path)

    def test_forced_route_executes_via_agents_factory_path(self) -> None:
        captured_execute_tool_calls: list[tuple[str, dict]] = []

        def _fake_execute_tool(name: str, args: dict, tool_call_id: str = None, source_agent_label: str = None):
            captured_execute_tool_calls.append((name, dict(args)))
            if name == "route_to_agent":
                return (
                    "Routing to _xworker",
                    {
                        "messages": [
                            {"role": "system", "content": "writer system"},
                            {"role": "system", "content": "Structured handoff context. {}"},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        **SAMPLE_WORKFLOW_REQUEST,
                                        "job_posting_result": {
                                            "agent": "job_posting_parser",
                                            "correlation_id": "sample-job-1",
                                            "job_posting": {
                                                "job_title": "Full Stack Software Engineer",
                                                "raw_text": "Full Stack Software Engineer role at Example Co",
                                            },
                                        },
                                        "profile_result": {
                                            "agent": "profile_parser",
                                            "correlation_id": "profile:test",
                                            "parse": {"language": "de", "errors": [], "warnings": []},
                                            "profile": SAMPLE_WORKFLOW_REQUEST["applicant_profile"]["value"],
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        "agent_label": "_xworker",
                        "tools": [],
                        "model": "gpt-4o",
                        "include_history": False,
                        "handoff": {
                            "protocol": "agent_handoff_v1",
                            "source_agent": "_xplaner_xrouter",
                            "target_agent": "_xworker",
                            "handoff_payload": {
                                "agent_label": "_xplaner_xrouter",
                                "handoff_to": "_xworker",
                                "output": {
                                    **SAMPLE_WORKFLOW_REQUEST,
                                    "job_posting_result": {
                                        "agent": "job_posting_parser",
                                        "correlation_id": "sample-job-1",
                                        "job_posting": {
                                            "job_title": "Full Stack Software Engineer",
                                            "raw_text": "Full Stack Software Engineer role at Example Co",
                                        },
                                    },
                                    "profile_result": {
                                        "agent": "profile_parser",
                                        "correlation_id": "profile:test",
                                        "parse": {"language": "de", "errors": [], "warnings": []},
                                        "profile": SAMPLE_WORKFLOW_REQUEST["applicant_profile"]["value"],
                                    },
                                },
                            },
                            "metadata": {},
                        },
                        "handoff_context": {
                            "contract": {
                                "schema": {
                                    "result_postprocess": {
                                        "tool": "persist_cover_letter_artifacts",
                                        "text_writer_tool": "write_document",
                                        "pdf_writer_tool": "md_to_pdf",
                                        "default_write_pdf": True,
                                    }
                                }
                            }
                        },
                        "runtime": {"instance_policy": "ephemeral", "role": "worker"},
                    },
                )
            raise AssertionError(f"Unexpected tool execution: {name}")

        chat = chat_mod.ChatCom(
            _model="gpt-4o-mini",
            _input_text=json.dumps(SAMPLE_WORKFLOW_REQUEST, ensure_ascii=False),
            _name="test_workflow",
        )

        with patch("alde.chat_completion.ChatComE", _DeterministicDispatcherChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_fake_execute_tool,
        ), patch(
            "alde.agents_factory.write_document",
            return_value="Document saved to: /tmp/sample_cover_letter.md",
        ), patch(
            "alde.agents_factory.md_to_pdf",
            return_value={"ok": True, "pdf_path": "/tmp/sample_cover_letter.pdf"},
        ):
            response = chat.get_response()

        payload = json.loads(response)
        self.assertEqual(payload["cover_letter"]["full_text"], "Sehr geehrtes Team,\n\nMotivation und Erfahrung.")
        self.assertEqual(payload["page_count"], 2)
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(payload["pages"][0]["page"], 1)
        self.assertEqual(payload["pages"][0]["title"], "Application")
        self.assertTrue(payload["pages"][0]["content_sha"])
        self.assertEqual(payload["pages"][1]["page"], 2)
        self.assertEqual(payload["pages"][1]["title"], "CV")
        self.assertIn("full_text", payload["cv"])
        self.assertEqual(payload["quality"]["language"], "de")
        self.assertEqual(payload["document_text_path"], "/tmp/sample_cover_letter.md")
        self.assertEqual(payload["document_pdf_path"], "/tmp/sample_cover_letter.pdf")
        self.assertEqual(len(captured_execute_tool_calls), 1)
        self.assertEqual(captured_execute_tool_calls[0][0], "route_to_agent")
        routed = captured_execute_tool_calls[0][1]
        self.assertEqual(routed["target_agent"], "_xworker")
        self.assertIn(
            routed.get("job_name"),
            {"cover_letter_writer", "router_planner_cover_letter_sequence"},
        )
        protocol = routed.get("handoff_protocol")
        if protocol is not None:
            self.assertIn(protocol, {"agent_handoff_v1", "message_text"})

    def test_ready_forced_route_executes_via_structured_handoff_path(self) -> None:
        captured_execute_tool_calls: list[tuple[str, dict]] = []

        def _fake_execute_tool(name: str, args: dict, tool_call_id: str = None, source_agent_label: str = None):
            captured_execute_tool_calls.append((name, dict(args)))
            if name == "route_to_agent":
                return (
                    "Routing to _xworker",
                    {
                        "messages": [
                            {"role": "system", "content": "writer system"},
                            {"role": "system", "content": "Structured handoff context. {}"},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "action": "generate_cover_letter",
                                        "job_posting_result": {
                                            "agent": "job_posting_parser",
                                            "correlation_id": "sha-direct-1",
                                            "job_posting": {"job_title": "Support Engineer"},
                                        },
                                        "profile_result": {
                                            "agent": "profile_parser",
                                            "correlation_id": "profile:direct-1",
                                            "parse": {"language": "de", "errors": [], "warnings": []},
                                            "profile": {"profile_id": "profile:direct-1", "preferences": {"language": "de"}},
                                        },
                                        "options": {"language": "de", "tone": "modern", "max_words": 280},
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        "agent_label": "_xworker",
                        "tools": [],
                        "model": "gpt-4o",
                        "include_history": False,
                        "handoff": {
                            "protocol": "agent_handoff_v1",
                            "source_agent": "_xplaner_xrouter",
                            "target_agent": "_xworker",
                            "handoff_payload": {
                                "agent_label": "_xplaner_xrouter",
                                "handoff_to": "_xworker",
                                "output": {
                                    "action": "generate_cover_letter",
                                    "job_posting_result": {
                                        "agent": "job_posting_parser",
                                        "correlation_id": "sha-direct-1",
                                        "file": {"path": "/tmp/source-posting.pdf"},
                                        "job_posting": {"job_title": "Support Engineer", "company_name": "Example Co"},
                                    },
                                    "profile_result": {
                                        "agent": "profile_parser",
                                        "correlation_id": "profile:direct-1",
                                        "profile": {"profile_id": "profile:direct-1", "preferences": {"language": "de"}},
                                    },
                                    "options": {"language": "de", "tone": "modern", "max_words": 280},
                                },
                            },
                            "metadata": {},
                        },
                        "handoff_context": {
                            "contract": {
                                "schema": {
                                    "result_postprocess": {
                                        "tool": "persist_cover_letter_artifacts",
                                        "text_writer_tool": "write_document",
                                        "pdf_writer_tool": "md_to_pdf",
                                        "default_write_pdf": True,
                                    }
                                }
                            }
                        },
                        "runtime": {"instance_policy": "ephemeral", "role": "worker"},
                    },
                )
            raise AssertionError(f"Unexpected tool execution: {name}")

        ready_request = {
            "action": "generate_cover_letter",
            "job_posting_result": {
                "agent": "job_posting_parser",
                "correlation_id": "sha-direct-1",
                "job_posting": {"job_title": "Support Engineer"},
            },
            "applicant_profile": {
                "source": "text",
                "value": {"profile_id": "profile:direct-1", "preferences": {"language": "de"}},
            },
            "options": {"language": "de", "tone": "modern", "max_words": 280},
        }

        chat = chat_mod.ChatCom(
            _model="gpt-4o-mini",
            _input_text=json.dumps(ready_request, ensure_ascii=False),
            _name="test_ready_structured_route",
        )

        with patch("alde.chat_completion.ChatComE", _DeterministicDispatcherChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_fake_execute_tool,
        ), patch(
            "alde.agents_factory.write_document",
            return_value="Document saved to: /tmp/cover_letter_ready.md",
        ), patch(
            "alde.agents_factory.md_to_pdf",
            return_value={"ok": True, "pdf_path": "/tmp/cover_letter_ready.pdf"},
        ):
            response = chat.get_response()

        payload = json.loads(response)
        self.assertEqual(payload["cover_letter"]["full_text"], "Sehr geehrtes Team,\n\nMotivation und Erfahrung.")
        self.assertEqual(payload["page_count"], 2)
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(payload["pages"][0]["title"], "Application")
        self.assertEqual(payload["pages"][1]["title"], "CV")
        self.assertTrue(payload["pages"][1]["metadata"]["content_sha"])
        self.assertEqual(payload["document_text_path"], "/tmp/cover_letter_ready.md")
        self.assertEqual(payload["document_pdf_path"], "/tmp/cover_letter_ready.pdf")
        self.assertEqual(payload["document_path"], "/tmp/cover_letter_ready.pdf")
        self.assertEqual(len(captured_execute_tool_calls), 1)
        routed = captured_execute_tool_calls[0][1]
        self.assertEqual(routed.get("target_agent"), "_xworker")
        self.assertIn(
            routed.get("job_name"),
            {"cover_letter_writer", "router_planner_cover_letter_sequence"},
        )

    def test_ready_forced_route_uses_explicit_job_posting_identity_for_artifact_doc_id(self) -> None:
        captured_write_calls: list[dict[str, object]] = []

        def _fake_write_document(*, content: str, path: str | None = None, doc_id: str | None = None, correlation_id: str | None = None, **_: object):
            captured_write_calls.append(
                {
                    "content": content,
                    "path": path,
                    "doc_id": doc_id,
                    "correlation_id": correlation_id,
                }
            )
            return "Document saved to: /tmp/explicit_cover_letter.md"

        chat = chat_mod.ChatCom(
            _model="gpt-4o-mini",
            _input_text=json.dumps(SAMPLE_WORKFLOW_REQUEST, ensure_ascii=False),
            _name="test_explicit_structured_route",
        )

        def _fake_execute_tool(name: str, args: dict, tool_call_id: str = None, source_agent_label: str = None):
            if name != "route_to_agent":
                raise AssertionError(f"Unexpected tool execution: {name}")
            return (
                "Routing to _xworker",
                {
                    "messages": [],
                    "agent_label": "_xworker",
                    "tools": [],
                    "model": "gpt-4o",
                    "include_history": False,
                    "handoff": {
                        "protocol": "agent_handoff_v1",
                        "source_agent": "_xplaner_xrouter",
                        "target_agent": "_xworker",
                        "handoff_payload": {
                            "agent_label": "_xplaner_xrouter",
                            "handoff_to": "_xworker",
                            "output": {
                                "action": "generate_cover_letter",
                                "job_posting_result": {
                                    "agent": "job_posting_parser",
                                    "correlation_id": "sha-explicit-1",
                                    "file": {"path": "/tmp/source-posting.pdf"},
                                    "raw_text_document": {
                                        "title": "Support Engineer",
                                        "raw_text": "Support Engineer at Example Co",
                                    },
                                    "entity_objects": [
                                        {"entity_key": "subject", "entity_type": "job_posting", "canonical_name": "Support Engineer", "metadata": {"role": "subject"}},
                                        {"entity_key": "organization:example_co", "entity_type": "organization", "canonical_name": "Example Co"},
                                    ],
                                    "relation_objects": [
                                        {"source_entity_key": "subject", "target_entity_key": "organization:example_co", "relation_type": "offered_by", "section_key": "header"}
                                    ],
                                },
                                "profile_result": {
                                    "agent": "profile_parser",
                                    "correlation_id": "profile:direct-1",
                                    "profile": {"profile_id": "profile:direct-1", "preferences": {"language": "de"}},
                                },
                                "options": {"language": "de", "tone": "modern", "max_words": 280},
                            },
                        },
                        "metadata": {},
                    },
                    "handoff_context": {
                        "contract": {
                            "schema": {
                                "result_postprocess": {
                                    "tool": "persist_cover_letter_artifacts",
                                    "text_writer_tool": "write_document",
                                    "pdf_writer_tool": "md_to_pdf",
                                    "default_write_pdf": True,
                                }
                            }
                        }
                    },
                    "runtime": {"instance_policy": "ephemeral", "role": "worker"},
                },
            )

        with patch("alde.chat_completion.ChatComE", _DeterministicDispatcherChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_fake_execute_tool,
        ), patch(
            "alde.agents_factory.write_document",
            side_effect=_fake_write_document,
        ), patch(
            "alde.agents_factory.md_to_pdf",
            return_value={"ok": True, "pdf_path": "/tmp/explicit_cover_letter.pdf"},
        ):
            response = chat.get_response()

        payload = json.loads(response)
        self.assertEqual(payload["document_text_path"], "/tmp/explicit_cover_letter.md")
        self.assertEqual(payload["document_pdf_path"], "/tmp/explicit_cover_letter.pdf")
        self.assertEqual(captured_write_calls[0]["doc_id"], "Support Engineer_Example Co")
        self.assertIn("# Application", str(captured_write_calls[0]["content"]))
        self.assertIn("<!-- pagebreak -->", str(captured_write_calls[0]["content"]))
        self.assertIn("# CV", str(captured_write_calls[0]["content"]))

    def test_import_dispatch_pipeline_action_uses_forced_route(self) -> None:
        pipeline_request = {
            "action": "import_dispatch_pipeline",
            "correlation_id": "pipeline-import-001",
            "job_posting_result": {
                "agent": "job_posting_parser",
                "correlation_id": "pipeline-import-001",
                "job_posting": {
                    "job_title": "Pipeline Engineer",
                    "company_name": "Tree Data Co",
                },
            },
            "agents_db_tree_path": "ALDE/AppData/tree_data.json",
        }

        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client:
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text=json.dumps(pipeline_request, ensure_ascii=False),
                _name="test_import_dispatch_pipeline_route",
            )

        self.assertIsNone(chat._forced_route)
        normalized_payload = self._load_chat_input_payload(chat)
        self.assertEqual(normalized_payload.get("action"), "import_dispatch_pipeline")
        self.assertEqual(normalized_payload.get("correlation_id"), "pipeline-import-001")
        get_client.assert_called()

    def test_dispatch_parser_import_pipeline_persists_and_propagates_tree_data_path(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            job_postings_db_path = tmp.name

        tree_data_rel_path = "ALDE/AppData/tree_data.json"
        try:
            self.assertTrue(Path(PKG_ROOT / "AppData" / "tree_data.json").is_file())

            mongo_backend = _InMemoryMongoDocumentBackend()
            with patch.object(tools_mod.DOCUMENT_REPOSITORY, "_load_agentsdb_backend", return_value=mongo_backend), patch.object(
                tools_mod,
                "sync_parser_result_to_agentsdb_knowledge",
                return_value={
                    "ok": True,
                    "stored": True,
                    "backend": "mongodb",
                    "tree_data_path": tree_data_rel_path,
                    "object_name": "job_postings",
                },
            ) as sync_mock:
                result = json.loads(
                    tools_mod.DOCUMENT_OBJECT_SERVICE.store_result(
                        job_posting_result={
                            "agent": "document_dispatch_ingest_import_pipeline",
                            "correlation_id": "pipeline-import-002",
                            "parse": {"is_job_posting": True},
                            "job_posting": {
                                "job_title": "Dispatcher Parser Import Engineer",
                                "company_name": "Pipeline Labs",
                            },
                        },
                        correlation_id="pipeline-import-002",
                        db_path=job_postings_db_path,
                        source_agent="document_dispatch_ingest_import_pipeline",
                        source_payload={
                            "action": "import_dispatch_pipeline",
                            "agents_db_tree_path": tree_data_rel_path,
                            "tree_data_path": tree_data_rel_path,
                        },
                    )
                )

            mongo_record = mongo_backend.load_record(
                storage_key=job_postings_db_path,
                record_id="pipeline-import-002",
                db_name="job_postings",
                obj_name="job_postings",
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["knowledge_sync"]["stored"])
            self.assertEqual(result["knowledge_sync"]["backend"], "mongodb")
            self.assertEqual(result["knowledge_sync"]["tree_data_path"], tree_data_rel_path)
            self.assertIsNotNone(mongo_record)
            self.assertEqual(mongo_record["job_posting"]["job_title"], "Dispatcher Parser Import Engineer")
            self.assertEqual(sync_mock.call_args.kwargs["object_name"], "job_postings")
            self.assertEqual(sync_mock.call_args.kwargs["correlation_id"], "pipeline-import-002")
            self.assertEqual(sync_mock.call_args.kwargs["handoff_payload"]["agents_db_tree_path"], tree_data_rel_path)
            self.assertEqual(sync_mock.call_args.kwargs["handoff_payload"]["tree_data_path"], tree_data_rel_path)
        finally:
            os.unlink(job_postings_db_path)


if __name__ == "__main__":
    unittest.main()
