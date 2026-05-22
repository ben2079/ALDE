from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import alde.repo_code_splitter as repo_code_splitter_mod
from alde import agents_db as agents_db_mod
from alde.agents_db import AgentDbInMemoryRepository, AgentDbSocketRepository, AgentDbSocketServerService, KnowledgeObjectService
from alde.agents_tools import get_tool_spec, repo_knowledge_worker, repo_knowledge_query
from alde.repo_code_splitter import (
    LoadContextPromptParser,
    RepoIndexService,
    RepoModuleParser,
    PythonCodeSplitter,
    _build_default_runtime_config,
    _normalize_repo_runtime_config,
    load_context,
    load_repo_context_for_ide_agent,
)


class _FakeEmbeddingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def process_object(self, object_name: str, obj: dict, *, owner_id: str) -> dict[str, bool]:
        _ = obj
        self.calls.append((object_name, owner_id))
        return {"stored": True}


class TestRepoCodeSplitter(unittest.TestCase):
    def test_agentdb_inmemory_repository_deferred_flush_batches_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = str(Path(tmp_dir) / "agentsdb_batch_image.json")
            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()

            flush_calls: list[str] = []
            original_flush = repo._flush_image

            def tracked_flush() -> None:
                flush_calls.append("flush")
                original_flush()

            repo._flush_image = tracked_flush  # type: ignore[method-assign]

            with repo.deferred_flush():
                repo.upsert_object("document", "doc:1", {"id": "doc:1", "tenant_id": "tenant_default", "namespace_id": "ns_repo_knowledge", "document_type": "repo_source", "title": "Doc 1", "source_uri": "file://doc1", "content_sha256": "sha1"})
                repo.upsert_object("document", "doc:2", {"id": "doc:2", "tenant_id": "tenant_default", "namespace_id": "ns_repo_knowledge", "document_type": "repo_source", "title": "Doc 2", "source_uri": "file://doc2", "content_sha256": "sha2"})
                self.assertEqual(flush_calls, [])

            self.assertEqual(len(flush_calls), 1)

    def test_agentdb_socket_repository_deferred_queue_splits_apply_operations(self) -> None:
        repo = AgentDbSocketRepository("agentsdb://127.0.0.1:2331")
        repo._apply_operations_batch_size = 2

        batch_lengths: list[int] = []

        def fake_request_object(action_name: str, action_payload: dict[str, object] | None = None) -> dict[str, object]:
            self.assertEqual(action_name, "apply_operations")
            operations = list((action_payload or {}).get("operations") or [])
            batch_lengths.append(len(operations))
            return {
                "ok": True,
                "applied": len(operations),
                "deleted": sum(1 for operation in operations if isinstance(operation, dict) and operation.get("action") == "delete"),
                "results": [],
            }

        repo._request_object = fake_request_object  # type: ignore[method-assign]

        with repo.deferred_write_queue():
            repo.upsert_object("document", "doc:1", {"id": "doc:1"})
            repo.upsert_object("document", "doc:2", {"id": "doc:2"})
            repo.upsert_object("document", "doc:3", {"id": "doc:3"})
            repo.delete_object("document", "doc:2")
            repo.delete_object("document", "doc:3")

        self.assertEqual(batch_lengths, [2, 2, 1])

    def test_agentdb_socket_server_apply_operations_uses_deferred_flush(self) -> None:
        class _FakeRepository:
            def __init__(self) -> None:
                self.deferred_flush_entries = 0
                self.calls: list[tuple[str, str]] = []

            @contextmanager
            def deferred_flush(self):
                self.deferred_flush_entries += 1
                yield

            def upsert_object(self, object_name: str, object_id: str, object_payload: dict[str, object]) -> dict[str, object]:
                _ = object_payload
                self.calls.append(("upsert", f"{object_name}:{object_id}"))
                return {"_id": object_id}

            def delete_object(self, object_name: str, object_id: str) -> bool:
                self.calls.append(("delete", f"{object_name}:{object_id}"))
                return True

        service = AgentDbSocketServerService(backend_uri="agentsmem://local")
        repository = _FakeRepository()
        service._repository_cache["alde_knowledge"] = repository

        response = service.dispatch_object(
            "apply_operations",
            {
                "operations": [
                    {"action": "upsert", "object_name": "document", "object_id": "doc:1", "object_payload": {"id": "doc:1"}},
                    {"action": "delete", "object_name": "document", "object_id": "doc:1"},
                ]
            },
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["applied"], 2)
        self.assertEqual(repository.deferred_flush_entries, 1)
        self.assertEqual(repository.calls, [("upsert", "document:doc:1"), ("delete", "document:doc:1")])

    def test_agentdb_socket_server_load_repository_treats_agentsmem_as_inmemory(self) -> None:
        service = AgentDbSocketServerService(backend_uri="agentsmem://local")

        repository = service.load_repository("alde_knowledge")

        self.assertIsInstance(repository, AgentDbInMemoryRepository)

    def test_agentdb_inmemory_repository_concurrent_flush_uses_unique_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = str(Path(tmp_dir) / "agentsdb_concurrent_flush.json")
            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()

            barrier = threading.Barrier(2)
            original_replace = agents_db_mod.os.replace
            replace_sources: list[str] = []
            replace_errors: list[BaseException] = []

            def gated_replace(src: str, dst: str) -> None:
                replace_sources.append(str(src))
                try:
                    barrier.wait(timeout=2.0)
                except BaseException as exc:  # pragma: no cover - defensive guard
                    replace_errors.append(exc)
                    raise
                original_replace(src, dst)

            def flush_repo() -> None:
                repo._flush_image()

            with patch("alde.agents_db.os.replace", side_effect=gated_replace):
                threads = [threading.Thread(target=flush_repo) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(replace_errors, [])
            self.assertEqual(len(replace_sources), 2)
            self.assertNotEqual(replace_sources[0], replace_sources[1])

    def test_agentdb_socket_repository_includes_server_detail_in_runtime_error(self) -> None:
        repo = AgentDbSocketRepository("agentsdb://127.0.0.1:2331")

        def fake_send_request_bytes(request_payload: dict[str, object]) -> bytes:
            _ = request_payload
            return b'{"ok":false,"error":"agents_db_socket_request_failed","detail":"boom"}\n'

        repo._send_request_bytes = fake_send_request_bytes  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError) as raised:
            repo.ensure_index_objects()

        self.assertEqual(str(raised.exception), "agents_db_socket_request_failed: boom")

    def test_agentdb_inmemory_repository_request_and_query_use_explicit_repo_query_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "mymodule.py").write_text(
                "import os\n\nclass Finder:\n    def find(self):\n        return os.getcwd()\n",
                encoding="utf-8",
            )
            image_path = str(root / "agentsdb_repo_query_image.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            embedding_service = _FakeEmbeddingService()
            index_service = RepoIndexService(
                repo,
                knowledge_service,
                embedding_service,
                workers=1,
                runtime_config=_build_default_runtime_config(),
            )
            index_service.index_repo_object(str(root), extensions=(".py",))
            repo._flush_image()

            request_payload = repo.request("Finder class", use_vector=False)

            self.assertEqual(request_payload["target_agent"], "_xworker")
            self.assertEqual(request_payload["job_name"], "adb_query")
            self.assertEqual(request_payload["tool_name"], "repo_knowledge_query")
            self.assertEqual(request_payload["namespace_id"], "ns_repo_knowledge")
            self.assertEqual(request_payload["image_path"], image_path)

            result = repo.query("Finder class", use_vector=False)

            self.assertTrue(result["ok"], msg=result.get("error"))
            self.assertEqual(result["job_name"], "adb_query")
            self.assertEqual(result["request"]["job_name"], "adb_query")
            self.assertGreaterEqual(result["total"], 1)
            self.assertIn("block", {chunk.get("owner_type") for chunk in result["chunks"]})
            self.assertIn("mymodule.py", {chunk.get("source_path") for chunk in result["chunks"]})

    def test_agentdb_socket_repository_request_and_query_use_explicit_repo_query_job(self) -> None:
        repo = AgentDbSocketRepository("agentsdb://127.0.0.1:2331")
        captured_calls: list[tuple[str, dict[str, object]]] = []
        document_payload = {
            "_id": "doc:repo_source:test",
            "namespace_id": "ns_repo_knowledge",
            "blocks": [
                {
                    "block_id": "blk:repo:test:1",
                    "heading": "Finder class",
                    "content": "class Finder:\n    def find(self):\n        return os.getcwd()",
                    "block_kind": "class",
                    "metadata": {"source_path": "mymodule.py", "kind": "class"},
                }
            ],
        }
        entity_payload = {
            "_id": "ent:repo:test:finder",
            "namespace_id": "ns_repo_knowledge",
            "entity_type": "class",
            "canonical_name": "Finder",
            "summary": "Finder class",
            "metadata": {"source_path": "mymodule.py"},
        }

        def fake_request_object(action_name: str, action_payload: dict[str, object] | None = None) -> dict[str, object]:
            captured_calls.append((action_name, dict(action_payload or {})))
            self.assertEqual(action_name, "load_objects")
            object_name = str((action_payload or {}).get("object_name") or "")
            if object_name == "document":
                return {"ok": True, "object_payload_list": [document_payload]}
            if object_name == "entity":
                return {"ok": True, "object_payload_list": [entity_payload]}
            if object_name in {"embedding", "relation"}:
                return {"ok": True, "object_payload_list": []}
            return {"ok": True, "object_payload_list": []}

        repo._request_object = fake_request_object  # type: ignore[method-assign]

        request_payload = repo.request("Finder class", use_vector=False)

        self.assertEqual(request_payload["target_agent"], "_xworker")
        self.assertEqual(request_payload["job_name"], "adb_query")
        self.assertEqual(request_payload["tool_name"], "repo_knowledge_query")

        result = repo.query("Finder class", use_vector=False)

        self.assertTrue(result["ok"], msg=result.get("error"))
        self.assertEqual(result["job_name"], "adb_query")
        self.assertEqual(result["request"]["target_agent"], "_xworker")
        self.assertGreaterEqual(result["total"], 1)
        self.assertIn("mymodule.py", {chunk.get("source_path") for chunk in result["chunks"]})
        self.assertEqual([call[0] for call in captured_calls], ["load_objects", "load_objects"])

    def test_adb_operation_tool_executes_generic_agentdb_operations(self) -> None:
        spec = get_tool_spec("agentdb_operation")
        self.assertIsNotNone(spec)

        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = str(Path(tmp_dir) / "agentsdb_operation_image.json")
            base_args = {
                "agents_db_uri": "agentsmem://local",
                "backend_uri": "agentsmem://local",
                "database_name": "adb_operation_test",
                "memory_image_path": image_path,
            }

            health_payload = json.loads(spec.execute({"operation": "health", **base_args}))
            self.assertTrue(health_payload["ok"])
            self.assertEqual(health_payload["operation"], "health")

            ensure_payload = json.loads(spec.execute({"operation": "ensure_index_objects", **base_args}))
            self.assertTrue(ensure_payload["ok"])
            self.assertTrue(ensure_payload["ensured"])

            upsert_payload = json.loads(
                spec.execute(
                    {
                        "operation": "upsert_object",
                        "object_name": "document",
                        "object_id": "doc:adb:1",
                        "object_payload": {
                            "_id": "doc:adb:1",
                            "namespace_id": "ns_repo_knowledge",
                            "title": "Finder class",
                            "source_uri": "file://finder.py",
                            "content": "class Finder: pass",
                        },
                        **base_args,
                    }
                )
            )
            self.assertTrue(upsert_payload["ok"])
            self.assertEqual(upsert_payload["object_payload"]["title"], "Finder class")

            load_object_payload = json.loads(
                spec.execute(
                    {
                        "operation": "load_object",
                        "object_name": "document",
                        "object_id": "doc:adb:1",
                        **base_args,
                    }
                )
            )
            self.assertTrue(load_object_payload["ok"])
            self.assertEqual(load_object_payload["object_payload"]["source_uri"], "file://finder.py")

            load_objects_payload = json.loads(
                spec.execute(
                    {
                        "operation": "load_objects",
                        "object_name": "document",
                        "object_filter": {"namespace_id": "ns_repo_knowledge"},
                        "limit": 5,
                        **base_args,
                    }
                )
            )
            self.assertTrue(load_objects_payload["ok"])
            self.assertEqual(len(load_objects_payload["object_payload_list"]), 1)

            find_objects_payload = json.loads(
                spec.execute(
                    {
                        "operation": "find_objects",
                        "namespace_id": "ns_repo_knowledge",
                        "query_text": "Finder",
                        "limit": 5,
                        **base_args,
                    }
                )
            )
            self.assertTrue(find_objects_payload["ok"])
            self.assertEqual(len(find_objects_payload["object_payload_list"]), 1)

            apply_payload = json.loads(
                spec.execute(
                    {
                        "operation": "apply_operations",
                        "operations": [
                            {
                                "action": "upsert",
                                "object_name": "relation",
                                "object_id": "rel:adb:1",
                                "object_payload": {
                                    "_id": "rel:adb:1",
                                    "namespace_id": "ns_repo_knowledge",
                                    "source_entity_id": "ent:adb:1",
                                    "target_entity_id": "ent:adb:2",
                                    "relation_type": "linked_to",
                                },
                            },
                            {
                                "action": "upsert",
                                "object_name": "document",
                                "object_id": "doc:adb:2",
                                "object_payload": {
                                    "_id": "doc:adb:2",
                                    "namespace_id": "ns_repo_knowledge",
                                    "title": "Second document",
                                    "source_uri": "file://second.py",
                                },
                            },
                        ],
                        **base_args,
                    }
                )
            )
            self.assertTrue(apply_payload["ok"])
            self.assertEqual(apply_payload["applied"], 2)

            relation_graph_payload = json.loads(
                spec.execute(
                    {
                        "operation": "load_relation_graph",
                        "namespace_id": "ns_repo_knowledge",
                        "source_entity_id": "ent:adb:1",
                        "max_depth": 2,
                        **base_args,
                    }
                )
            )
            self.assertTrue(relation_graph_payload["ok"])
            self.assertEqual(len(relation_graph_payload["object_payload_list"]), 1)
            self.assertEqual(relation_graph_payload["object_payload_list"][0]["target_entity_id"], "ent:adb:2")

            delete_payload = json.loads(
                spec.execute(
                    {
                        "operation": "delete_object",
                        "object_name": "document",
                        "object_id": "doc:adb:1",
                        **base_args,
                    }
                )
            )
            self.assertTrue(delete_payload["ok"])
            self.assertTrue(delete_payload["deleted"])

            deleted_lookup_payload = json.loads(
                spec.execute(
                    {
                        "operation": "load_object",
                        "object_name": "document",
                        "object_id": "doc:adb:1",
                        **base_args,
                    }
                )
            )
            self.assertTrue(deleted_lookup_payload["ok"])
            self.assertIsNone(deleted_lookup_payload["object_payload"])

    def test_normalize_repo_runtime_config_overrides_default_namespace(self) -> None:
        base = _build_default_runtime_config()
        base.namespace_id = "ns_alde_default"
        base.namespace_slug = "alde-default"
        base.namespace_name = "ALDE Default Knowledge"

        normalized = _normalize_repo_runtime_config(base)

        self.assertEqual(normalized.namespace_id, "ns_repo_knowledge")
        self.assertEqual(normalized.namespace_slug, "repo-knowledge")
        self.assertEqual(normalized.namespace_name, "ALDE Repository Knowledge")
        self.assertEqual(normalized.agents_db_uri, base.agents_db_uri)
        self.assertEqual(normalized.default_embedding_model, base.default_embedding_model)

    def test_index_repo_builds_document_entity_relation_and_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "sample.py").write_text(
                "import os\n\nclass Demo:\n    pass\n\ndef helper():\n    return os.getcwd()\n",
                encoding="utf-8",
            )

            repo = AgentDbInMemoryRepository(str(root / "agentsdb_image.json"))
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            embedding_service = _FakeEmbeddingService()
            service = RepoIndexService(
                repo,
                knowledge_service,
                embedding_service,
                workers=1,
                runtime_config=_build_default_runtime_config(),
            )

            result = service.index_repo_object(str(root), extensions=(".py",))

            self.assertEqual(result["files_found"], 1)
            self.assertGreaterEqual(result["total_blocks"], 3)
            self.assertGreaterEqual(result["total_entities"], 4)
            self.assertGreaterEqual(result["total_relations"], 3)

            documents = repo.load_objects("document", limit=10)
            entities = repo.load_objects("entity", limit=20)
            relations = repo.load_objects("relation", limit=20)

            self.assertEqual(len(documents), 1)
            self.assertIn("module", {entity.get("entity_type") for entity in entities})
            self.assertIn("defines_class", {relation.get("relation_type") for relation in relations})
            self.assertEqual(
                {owner_type for owner_type, _ in embedding_service.calls},
                {"block", "entity", "relation"},
            )

    def test_repo_module_parser_emits_target_annotated_entity_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            module_path = root / "sample.py"
            module_path.write_text(
                "import os\n\nclass Demo:\n    pass\n\ndef helper():\n    return os.getcwd()\n",
                encoding="utf-8",
            )

            parser = RepoModuleParser()
            payload = parser.parse_object(str(module_path), repo_root=str(root))

            entity_objects = list(payload.get("entity_objects") or [])
            relation_objects = list(payload.get("relation_objects") or [])
            target_entities = [entity for entity in entity_objects if entity.get("is_target") is True]

            self.assertEqual(payload.get("title"), "sample.py")
            self.assertEqual(payload.get("source"), "repo_module_parser")
            self.assertEqual(payload.get("record_kind"), "document")
            self.assertEqual(payload.get("processing_state"), "processed")
            self.assertIs(payload.get("processed"), True)
            self.assertEqual(relation_objects, [])
            self.assertTrue(target_entities)
            self.assertTrue(any(entity.get("source_entity") == "subject" for entity in target_entities))
            self.assertIn("defines_class", {entity.get("is_relational") for entity in target_entities})
            self.assertIn("imports_module", {entity.get("is_relational") for entity in target_entities})

    def test_format_repo_knowledge_chunks_includes_relation_description(self) -> None:
        chunks = repo_code_splitter_mod._format_repo_knowledge_chunks(
            [
                {
                    "score": 0.97,
                    "payload": {
                        "relation_type": "defines_class",
                        "source_entity_id": "ent:module:sample",
                        "target_entity_id": "ent:class:demo",
                        "metadata": {
                            "source_path": "sample.py",
                            "relation_description": "sample.py defines the Demo class.",
                        },
                    },
                }
            ],
            "relation",
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["relation_description"], "sample.py defines the Demo class.")
        self.assertEqual(chunks[0]["source_path"], "sample.py")

    def test_learning_rank_profile_prefers_successful_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = AgentDbInMemoryRepository(str(Path(tmp_dir) / "agentsdb_learning_profile.json"))
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)

            repo.upsert_object(
                "retrieval_run",
                "retrieval:ok:1",
                {
                    "id": "retrieval:ok:1",
                    "namespace_id": "ns_repo_knowledge",
                    "query_text": "Frau Hund Besitzverhaeltnis",
                    "filters": {
                        "success": True,
                        "tool_name": "repo_knowledge_query",
                    },
                    "results": [
                        {
                            "rank_no": 1,
                            "result_type": "entity",
                            "result_id": "ent:frau",
                            "source_stage": "repo_knowledge_query",
                            "chosen": True,
                            "metadata": {
                                "canonical_name": "Frau",
                                "summary": "Tierhalter",
                            },
                        }
                    ],
                },
            )
            repo.upsert_object(
                "retrieval_run",
                "retrieval:fail:1",
                {
                    "id": "retrieval:fail:1",
                    "namespace_id": "ns_repo_knowledge",
                    "query_text": "irrelevant failed run",
                    "filters": {
                        "success": False,
                        "tool_name": "repo_knowledge_query",
                    },
                    "results": [
                        {
                            "rank_no": 1,
                            "result_type": "relation",
                            "result_id": "rel:ignored",
                            "source_stage": "repo_knowledge_query",
                            "chosen": True,
                            "metadata": {
                                "relation_description": "ignored relation",
                            },
                        }
                    ],
                },
            )

            profile = knowledge_service.load_learning_rank_profile(
                namespace_id="ns_repo_knowledge",
                query_text="Frau und Hund",
                tool_name="repo_knowledge_query",
            )

            self.assertGreaterEqual(int(profile.get("matched_runs") or 0), 1)
            self.assertGreaterEqual(int(profile.get("successful_runs") or 0), 1)
            owner_boost = dict(profile.get("owner_type_boost") or {})
            self.assertGreater(float(owner_boost.get("entity") or 0.0), 0.0)
            self.assertEqual(float(owner_boost.get("relation") or 0.0), 0.0)

    def test_apply_learning_rerank_boosts_similar_entity_chunks(self) -> None:
        chunks = [
            {
                "owner_type": "block",
                "heading": "General Notes",
                "content": "Unrelated content",
                "source_path": "general.py",
                "score": 0.8,
            },
            {
                "owner_type": "entity",
                "canonical_name": "Frau",
                "summary": "Tierhalter und Besitzerin",
                "source_path": "profile.py",
                "score": 0.6,
            },
        ]
        learning_profile = {
            "matched_runs": 2,
            "owner_type_boost": {
                "entity": 1.0,
                "block": 0.1,
            },
            "term_weights": {
                "frau": 2.0,
                "tierhalter": 1.5,
            },
        }

        reranked_chunks = repo_code_splitter_mod._apply_learning_rerank_to_chunks(
            chunks=chunks,
            query_text="Frau Tierhalter",
            learning_profile=learning_profile,
        )

        self.assertEqual(len(reranked_chunks), 2)
        self.assertEqual(reranked_chunks[0].get("owner_type"), "entity")
        self.assertIn("learning_rerank_score", reranked_chunks[0])
        self.assertGreater(
            float(reranked_chunks[0].get("learning_rerank_score") or 0.0),
            float(reranked_chunks[1].get("learning_rerank_score") or 0.0),
        )

    def test_load_repo_context_for_ide_agent_uses_relation_description_for_relation_chunks(self) -> None:
        with patch(
            "alde.repo_code_splitter.repo_knowledge_query",
            return_value={
                "ok": True,
                "chunks": [
                    {
                        "owner_type": "relation",
                        "relation_type": "defines_class",
                        "source_entity_id": "ent:module:sample",
                        "target_entity_id": "ent:class:demo",
                        "relation_description": "sample.py defines the Demo class.",
                        "source_path": "sample.py",
                    }
                ],
            },
        ):
            entries = load_repo_context_for_ide_agent(
                "Demo class",
                limit=1,
                owner_types=["relation"],
                use_vector=False,
            )

        self.assertEqual(len(entries), 1)
        self.assertIn("defines_class", entries[0]["title"])
        self.assertEqual(entries[0]["content"], "sample.py defines the Demo class.")
        self.assertEqual(entries[0]["source_path"], "sample.py")

    def test_load_repo_context_parser_derives_entity_and_relation_from_subject_object_prompt(self) -> None:
        captured_call: dict[str, object] = {}

        def _capture_adb_query(*args, **kwargs):
            _ = args
            captured_call.update(kwargs)
            return {"ok": True, "chunks": []}

        query_text = (
            "Die Frau geht mit Ihrem Hund zum Einkauf. "
            "Frau = Subjekt und Typ Tierhalter,Hundehalter. "
            "Hund ist Objekt und Typ Besitz/Eigentum. "
            "Beziehung zwischen Frau und Hund = Eigentums/Besitzverhaeltnis."
        )

        with patch("alde.repo_code_splitter.adb_query", side_effect=_capture_adb_query):
            entries = load_repo_context_for_ide_agent(
                query_text,
                limit=5,
                use_vector=False,
            )

        self.assertEqual(entries, [])
        self.assertEqual(captured_call.get("owner_types"), ["entity", "relation"])
        normalized_query = str(captured_call.get("query") or "")
        self.assertIn("Frau", normalized_query)
        self.assertIn("Hund", normalized_query)
        self.assertIn("Tierhalter", normalized_query)
        self.assertIn("Eigentums/Besitzverhaeltnis", normalized_query)

    def test_load_repo_context_parser_respects_explicit_owner_types(self) -> None:
        captured_call: dict[str, object] = {}

        def _capture_adb_query(*args, **kwargs):
            _ = args
            captured_call.update(kwargs)
            return {"ok": True, "chunks": []}

        query_text = "Frau = Subjekt. Hund ist Objekt. Beziehung zwischen Frau und Hund = Besitz."

        with patch("alde.repo_code_splitter.adb_query", side_effect=_capture_adb_query):
            load_repo_context_for_ide_agent(
                query_text,
                limit=3,
                owner_types=["block"],
                use_vector=False,
            )

        self.assertEqual(captured_call.get("owner_types"), ["block"])

    def test_load_context_pattern_embedding_model_returns_fixed_dimension_and_scores(self) -> None:
        parser = LoadContextPromptParser()
        role_payload = {
            "subjects": ["Frau"],
            "objects": ["Hund"],
            "relations": ["Eigentums/Besitzverhaeltnis"],
            "types": ["Tierhalter", "Hundehalter", "Besitz", "Eigentum"],
        }
        feature_values = parser._build_feature_values_object(
            query_text="Frau = Subjekt. Hund ist Objekt. Beziehung zwischen Frau und Hund = Eigentums/Besitzverhaeltnis.",
            role_payload=role_payload,
            wants_entities=True,
            wants_relations=True,
        )

        model_result = parser._pattern_embedding_model.infer_object(feature_values)

        self.assertEqual(model_result.get("dimension"), 12)
        embedding = list(model_result.get("embedding") or [])
        self.assertEqual(len(embedding), 12)
        self.assertIn("entity", list(model_result.get("owner_types") or []))
        self.assertIn("relation", list(model_result.get("owner_types") or []))
        scores = dict(model_result.get("scores") or {})
        self.assertGreater(float(scores.get("entity") or 0.0), 0.0)
        self.assertGreater(float(scores.get("relation") or 0.0), 0.0)

    def test_load_context_parser_returns_learning_signal_payload(self) -> None:
        parser = LoadContextPromptParser()
        query_text = (
            "Frau = Subjekt und Typ Tierhalter,Hundehalter. "
            "Hund ist Objekt und Typ Besitz/Eigentum. "
            "Beziehung zwischen Frau und Hund = Eigentums/Besitzverhaeltnis."
        )

        parsed_query, parsed_owner_types, signal_payload = parser.parse_with_signal_object(query_text, None)

        self.assertEqual(parsed_owner_types, ["entity", "relation"])
        self.assertIn("Frau", parsed_query)
        self.assertIn("Hund", parsed_query)
        self.assertEqual(signal_payload.get("mode"), "pattern_embedding_inference")
        self.assertEqual(int(signal_payload.get("embedding_dimension") or 0), 12)
        self.assertEqual(len(list(signal_payload.get("pattern_embedding") or [])), 12)
        self.assertIn("model_scores", signal_payload)
        self.assertIn("weight_snapshot", signal_payload)

    def test_load_context_syncs_learning_signal_to_agentsdb(self) -> None:
        captured_sync: dict[str, object] = {}

        def _capture_learning_sync(**kwargs):
            captured_sync.update(kwargs)

        with patch(
            "alde.repo_code_splitter.adb_query",
            return_value={
                "ok": True,
                "chunks": [
                    {
                        "owner_type": "entity",
                        "canonical_name": "Frau",
                        "entity_type": "person",
                        "summary": "Person subject",
                        "source_path": "demo.py",
                    }
                ],
            },
        ), patch("alde.repo_code_splitter._sync_load_context_learning_signal", side_effect=_capture_learning_sync):
            entries = load_context(
                query="Frau = Subjekt. Hund ist Objekt. Beziehung zwischen Frau und Hund = Besitz.",
                model_result={"answer": "ok"},
                runtime_context={"context_kind": "chat_runtime"},
                correlation_id="corr:test:1",
                use_vector=False,
            )

        self.assertEqual(len(entries), 1)
        self.assertEqual(str(captured_sync.get("user_prompt") or "")[:4], "Frau")
        self.assertEqual(captured_sync.get("correlation_id"), "corr:test:1")
        self.assertIsInstance(captured_sync.get("pattern_signal"), dict)

    def test_repo_knowledge_worker_is_registered_and_scans_python_files(self) -> None:
        spec = get_tool_spec("repo_knowledge_worker")
        self.assertIsNotNone(spec)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "alpha.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "ignore.md").write_text("# ignored\n", encoding="utf-8")

            result = repo_knowledge_worker("scan", root_dir=str(root), extensions=[".py"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["operation"], "scan")
            self.assertEqual([Path(path).name for path in result["files"]], ["alpha.py"])

    def test_repo_knowledge_worker_build_uses_repo_namespace_with_env_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "alpha.py").write_text("class Demo:\n    pass\n", encoding="utf-8")
            image_path = str(root / "agentsdb_repo_env_image.json")
            runtime_config = _build_default_runtime_config()
            runtime_config.namespace_id = "ns_alde_default"
            runtime_config.namespace_slug = "alde-default"
            runtime_config.namespace_name = "ALDE Default Knowledge"

            with patch("alde.repo_code_splitter.load_agentsdb_runtime_config_from_env", return_value=runtime_config), \
                 patch("alde.repo_code_splitter.load_agentsdb_pipeline_service", side_effect=RuntimeError("skip external pipeline")):
                normalized = _normalize_repo_runtime_config(runtime_config)

            self.assertEqual(normalized.namespace_id, "ns_repo_knowledge")
            self.assertEqual(normalized.namespace_slug, "repo-knowledge")
            self.assertEqual(normalized.namespace_name, "ALDE Repository Knowledge")

    def test_repo_knowledge_worker_cleanup_deletes_repo_embeddings_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = str(root / "agentsdb_cleanup_image.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()

            repo_embedding_id = "ns_alde_default:block:blk:repo:abc123:1:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
            keep_embedding_id = "ns_alde_default:block:blk:other:abc123:1:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

            repo.upsert_object(
                "embedding",
                repo_embedding_id,
                {
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_alde_default",
                    "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "owner_type": "block",
                    "owner_id": "blk:repo:abc123:1",
                    "content_sha256": "sha-repo",
                    "dimension": 384,
                    "index_namespace": "ns_alde_default",
                    "index_item_key": "block:blk:repo:abc123:1",
                    "embedding": [0.1, 0.2],
                },
            )
            repo.upsert_object(
                "embedding",
                keep_embedding_id,
                {
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_alde_default",
                    "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "owner_type": "block",
                    "owner_id": "blk:other:abc123:1",
                    "content_sha256": "sha-other",
                    "dimension": 384,
                    "index_namespace": "ns_alde_default",
                    "index_item_key": "block:blk:other:abc123:1",
                    "embedding": [0.3, 0.4],
                },
            )

            with patch("alde.repo_code_splitter.load_agentsdb_runtime_config_from_env", return_value=None):
                result = repo_knowledge_worker(
                    "cleanup",
                    image_path=image_path,
                    cleanup_namespace_ids=["ns_alde_default"],
                    cleanup_object_names=["embedding"],
                    cleanup_owner_prefixes=["blk:repo:"],
                    delete_async=False,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["operation"], "cleanup")
            self.assertEqual(result["deleted"], 1)

            reloaded_repo = AgentDbInMemoryRepository(image_path)
            remaining_embeddings = reloaded_repo.load_objects("embedding", object_filter={"namespace_id": "ns_alde_default"}, limit=10)
            remaining_ids = {str(item.get("_id") or item.get("id") or "") for item in remaining_embeddings if isinstance(item, dict)}
            self.assertNotIn(repo_embedding_id, remaining_ids)
            self.assertIn(keep_embedding_id, remaining_ids)

    def test_repo_knowledge_worker_async_cleanup_status_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = str(root / "agentsdb_async_cleanup_image.json")
            jobs_path = str(root / "repo_worker_jobs.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            repo.upsert_object(
                "embedding",
                "ns_alde_default:block:blk:repo:async:1:model",
                {
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_alde_default",
                    "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "owner_type": "block",
                    "owner_id": "blk:repo:async:1",
                    "content_sha256": "sha-async",
                    "dimension": 384,
                    "index_namespace": "ns_alde_default",
                    "index_item_key": "block:blk:repo:async:1",
                    "embedding": [0.1, 0.2],
                },
            )

            with patch.dict("os.environ", {"ALDE_REPO_WORKER_JOBS_PATH": jobs_path}, clear=False), patch(
                "alde.repo_code_splitter.load_agentsdb_runtime_config_from_env", return_value=None
            ):
                kickoff = repo_knowledge_worker(
                    "cleanup",
                    image_path=image_path,
                    cleanup_namespace_ids=["ns_alde_default"],
                    cleanup_object_names=["embedding"],
                    cleanup_owner_prefixes=["blk:repo:"],
                    delete_async=True,
                    run_async=True,
                )

                self.assertTrue(kickoff["ok"])
                self.assertTrue(kickoff["async"])
                self.assertTrue(str(kickoff.get("job_id") or "").strip())

                observed_status = "queued"
                job_payload = None
                for _ in range(20):
                    status_result = repo_knowledge_worker("status", job_id=kickoff["job_id"])
                    self.assertTrue(status_result["ok"])
                    job_payload = status_result["job"]
                    observed_status = str(job_payload.get("status") or "")
                    if observed_status in {"completed", "failed"}:
                        break
                    time.sleep(0.05)

            self.assertIn(observed_status, {"completed", "failed"})
            self.assertIsInstance(job_payload, dict)
            if observed_status == "failed":
                self.fail(f"Async cleanup job failed: {job_payload}")

            result_payload = job_payload.get("result") if isinstance(job_payload, dict) else {}
            self.assertIsInstance(result_payload, dict)
            self.assertGreaterEqual(int(result_payload.get("deleted", 0)), 1)

    def test_repo_knowledge_worker_status_loads_persisted_job_after_memory_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = str(root / "agentsdb_async_status_image.json")
            jobs_path = str(root / "repo_worker_jobs.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            repo.upsert_object(
                "embedding",
                "ns_alde_default:block:blk:repo:async:2:model",
                {
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_alde_default",
                    "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "owner_type": "block",
                    "owner_id": "blk:repo:async:2",
                    "content_sha256": "sha-async-2",
                    "dimension": 384,
                    "index_namespace": "ns_alde_default",
                    "index_item_key": "block:blk:repo:async:2",
                    "embedding": [0.1, 0.2],
                },
            )

            with patch.dict("os.environ", {"ALDE_REPO_WORKER_JOBS_PATH": jobs_path}, clear=False), patch(
                "alde.repo_code_splitter.load_agentsdb_runtime_config_from_env", return_value=None
            ):
                with repo_code_splitter_mod._REPO_WORKER_JOBS_LOCK:
                    repo_code_splitter_mod._REPO_WORKER_JOBS.clear()

                kickoff = repo_knowledge_worker(
                    "cleanup",
                    image_path=image_path,
                    cleanup_namespace_ids=["ns_alde_default"],
                    cleanup_object_names=["embedding"],
                    cleanup_owner_prefixes=["blk:repo:"],
                    delete_async=True,
                    run_async=True,
                )

                observed_status = "queued"
                for _ in range(20):
                    status_result = repo_knowledge_worker("status", job_id=kickoff["job_id"])
                    self.assertTrue(status_result["ok"])
                    observed_status = str(status_result["job"].get("status") or "")
                    if observed_status in {"completed", "failed"}:
                        break
                    time.sleep(0.05)

                with repo_code_splitter_mod._REPO_WORKER_JOBS_LOCK:
                    repo_code_splitter_mod._REPO_WORKER_JOBS.clear()

                persisted_status = repo_knowledge_worker("status", job_id=kickoff["job_id"])

            self.assertTrue(Path(jobs_path).is_file())
            self.assertTrue(persisted_status["ok"])
            self.assertIn(observed_status, {"completed", "failed"})
            if observed_status == "failed":
                self.fail(f"Async cleanup job failed before persistence check: {persisted_status}")
            self.assertEqual(str(persisted_status["job"].get("status") or ""), "completed")
            result_payload = persisted_status["job"].get("result") if isinstance(persisted_status["job"], dict) else {}
            self.assertIsInstance(result_payload, dict)
            self.assertGreaterEqual(int(result_payload.get("deleted", 0)), 1)

    def test_repo_knowledge_query_is_registered_and_returns_context(self) -> None:
        """repo_knowledge_query must be registered and return a well-formed result dict."""
        spec = get_tool_spec("repo_knowledge_query")
        self.assertIsNotNone(spec)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "mymodule.py").write_text(
                "import os\n\nclass Finder:\n    def find(self):\n        return os.getcwd()\n",
                encoding="utf-8",
            )
            image_path = str(root / "agentsdb_rkq_image.json")

            # First index the test repo so there is something to query.
            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            emb_svc = _FakeEmbeddingService()
            index_service = RepoIndexService(
                repo,
                knowledge_service,
                emb_svc,
                workers=1,
                runtime_config=_build_default_runtime_config(),
            )
            index_service.index_repo_object(str(root), extensions=(".py",))

            # Flush the image so the query helper can load it.
            if hasattr(repo, "_flush_image"):
                repo._flush_image()

            # Now query using the snapshot image (no env vars needed).
            result = repo_knowledge_query(
                "Finder class find method",
                owner_types=["block", "entity"],
                limit=5,
                image_path=image_path,
                use_vector=False,   # no model loaded in unit-test env
            )

            self.assertTrue(result["ok"], msg=result.get("error"))
            self.assertIn("chunks", result)
            self.assertIn("total", result)
            self.assertIsInstance(result["chunks"], list)

    def test_load_repo_context_for_ide_agent_tool_is_registered(self) -> None:
        spec = get_tool_spec("load_repo_context_for_ide_agent")

        self.assertIsNotNone(spec)
        self.assertTrue(callable(spec.implementation))
        self.assertEqual(spec.name, "load_repo_context_for_ide_agent")

    def test_repo_knowledge_query_vector_results_include_non_empty_chunk_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = str(Path(tmp_dir) / "agentsdb_rkq_vector_image.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            repo.upsert_object(
                "document",
                "doc:repo:test:1",
                {
                    "_id": "doc:repo:test:1",
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_repo_knowledge",
                    "document_type": "repo_source",
                    "title": "finder.py",
                    "source_uri": "file:///tmp/finder.py",
                    "content_sha256": "sha-doc-1",
                    "blocks": [
                        {
                            "block_id": "blk:repo:test:1",
                            "block_no": 1,
                            "heading": "class Finder",
                            "content": "class Finder:\n    def find(self):\n        return 'ok'\n",
                            "block_kind": "class",
                            "metadata": {"source_path": "finder.py", "kind": "class"},
                        }
                    ],
                },
            )
            repo.upsert_object(
                "entity",
                "ent:repo:test:1",
                {
                    "_id": "ent:repo:test:1",
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_repo_knowledge",
                    "entity_type": "class",
                    "canonical_name": "Finder",
                    "summary": "Finder class for repo knowledge query tests.",
                    "metadata": {"source_path": "finder.py"},
                },
            )
            repo.upsert_object(
                "embedding",
                "ns_repo_knowledge:block:blk:repo:test:1:model",
                {
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_repo_knowledge",
                    "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "owner_type": "block",
                    "owner_id": "blk:repo:test:1",
                    "content_sha256": "sha-emb-block-1",
                    "dimension": 3,
                    "index_namespace": "ns_repo_knowledge",
                    "index_item_key": "block:blk:repo:test:1",
                    "embedding": [1.0, 0.0, 0.0],
                },
            )
            repo.upsert_object(
                "embedding",
                "ns_repo_knowledge:entity:ent:repo:test:1:model",
                {
                    "tenant_id": "tenant_default",
                    "namespace_id": "ns_repo_knowledge",
                    "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "owner_type": "entity",
                    "owner_id": "ent:repo:test:1",
                    "content_sha256": "sha-emb-entity-1",
                    "dimension": 3,
                    "index_namespace": "ns_repo_knowledge",
                    "index_item_key": "entity:ent:repo:test:1",
                    "embedding": [0.9, 0.1, 0.0],
                },
            )

            if hasattr(repo, "_flush_image"):
                repo._flush_image()

            with patch("alde.repo_code_splitter.load_agentsdb_runtime_config_from_env", return_value=None), patch(
                "alde.repo_code_splitter.EntityRelationEmbeddingService.embed_object",
                return_value=[1.0, 0.0, 0.0],
            ):
                result = repo_knowledge_query(
                    "Finder class find method",
                    owner_types=["block", "entity"],
                    limit=2,
                    image_path=image_path,
                    use_vector=True,
                )

            self.assertTrue(result["ok"], msg=result.get("error"))
            self.assertTrue(result["used_vector_search"])
            self.assertGreaterEqual(result["total"], 2)
            block_chunks = [chunk for chunk in result["chunks"] if chunk.get("owner_type") == "block"]
            entity_chunks = [chunk for chunk in result["chunks"] if chunk.get("owner_type") == "entity"]
            self.assertTrue(any(str(chunk.get("heading") or "").strip() for chunk in block_chunks), msg=result)
            self.assertTrue(any(str(chunk.get("content") or "").strip() for chunk in block_chunks), msg=result)
            self.assertTrue(any(str(chunk.get("source_path") or "").strip() for chunk in block_chunks), msg=result)
            self.assertTrue(any(str(chunk.get("canonical_name") or "").strip() for chunk in entity_chunks), msg=result)
            self.assertTrue(any(str(chunk.get("summary") or "").strip() for chunk in entity_chunks), msg=result)

    # -----------------------------------------------------------------------
    # Unified Splitter API
    # -----------------------------------------------------------------------

    def test_python_code_splitter_split_text_returns_string_list(self) -> None:
        """PythonCodeSplitter.split_text() must return a non-empty list[str]."""
        source = (
            "import os\n\nclass Runner:\n    def run(self):\n        return os.getpid()\n"
            "\ndef helper():\n    pass\n"
        )
        splitter = PythonCodeSplitter()
        chunks = splitter.split_text(source, filename="runner.py")
        self.assertIsInstance(chunks, list)
        self.assertTrue(all(isinstance(c, str) for c in chunks))
        self.assertGreaterEqual(len(chunks), 2, msg="Expected at least imports + class blocks")

    def test_create_text_splitter_python_ast_strategy(self) -> None:
        """_create_text_splitter('python_ast') must return the AST adapter."""
        try:
            from alde.vstores import _create_text_splitter
        except ImportError as exc:
            self.skipTest(f"vstores not importable ({exc})")

        adapter = _create_text_splitter(chunk_strategy="python_ast")
        source = "import sys\n\ndef greet():\n    return 'hi'\n"
        chunks = adapter.split_text(source)
        self.assertIsInstance(chunks, list)
        self.assertTrue(len(chunks) >= 1)

    def test_load_repo_context_for_ide_agent_returns_attach_entries(self) -> None:
        """load_repo_context_for_ide_agent() must return dicts with title/language/content/source_path."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "demo.py").write_text(
                "import os\n\nclass Widget:\n    def paint(self):\n        pass\n",
                encoding="utf-8",
            )
            image_path = str(root / "ide_loader_image.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            emb_svc = _FakeEmbeddingService()
            index_service = RepoIndexService(
                repo, knowledge_service, emb_svc, workers=1,
                runtime_config=_build_default_runtime_config(),
            )
            index_service.index_repo_object(str(root), extensions=(".py",))
            if hasattr(repo, "_flush_image"):
                repo._flush_image()

            entries = load_repo_context_for_ide_agent(
                "Widget paint method",
                limit=3,
                image_path=image_path,
                use_vector=False,
            )

            self.assertIsInstance(entries, list)
            if entries:
                required_keys = {"title", "language", "content", "source_path"}
                for entry in entries:
                    self.assertTrue(required_keys.issubset(entry.keys()), msg=f"Missing keys in {entry}")
                    self.assertIsInstance(entry["content"], str)
                    self.assertTrue(entry["content"].strip(), msg="content must not be empty")


if __name__ == "__main__":
    unittest.main()