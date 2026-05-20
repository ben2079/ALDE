from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from alde.agents_db import (
    AgentMemoryAttachmentService,
    AgentDbQueryService,
    EntityRelationEmbeddingService,
    KnowledgeObjectService,
    ObjectMappingService,
    RuntimeConfigObject,
)
from alde.agents_tools import AgentDbOperationService


class _RecordingKnowledgeRepository:
    def __init__(self) -> None:
        self.records_by_object_name: dict[str, dict[str, dict[str, Any]]] = {}

    def upsert_object(self, object_name: str, object_id: str, object_payload: dict[str, Any]) -> dict[str, Any]:
        bucket = self.records_by_object_name.setdefault(str(object_name), {})
        bucket[str(object_id)] = dict(object_payload)
        return bucket[str(object_id)]


class _StubAgentDbOperationRepository:
    def ensure_index_objects(self) -> bool:
        return True

    def load_object(self, object_name: str, object_id: str) -> dict[str, Any]:
        _ = object_id
        if str(object_name) != "relation":
            return {}
        return {
            "id": "rel:sample:demo",
            "relation_type": "defines_class",
            "source_entity_id": "ent:module:sample",
            "target_entity_id": "ent:class:demo",
            "metadata": {
                "relation_description": "sample.py defines the Demo class.",
            },
        }

    def load_objects(self, object_name: str, object_filter: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        _ = object_filter
        _ = limit
        if str(object_name) != "relation":
            return []
        return [self.load_object("relation", "rel:sample:demo")]

    def load_relation_graph(self, *, namespace_id: str, source_entity_id: str, max_depth: int = 2) -> list[dict[str, Any]]:
        _ = namespace_id
        _ = source_entity_id
        _ = max_depth
        return [self.load_object("relation", "rel:sample:demo")]


class _StubAgentMemoryService:
    def append_session_context(self, **_: Any) -> bool:
        return True

    def load_object_record(self, **_: Any) -> dict[str, Any]:
        return {}

    def load_session_scope_key(
        self,
        *,
        scope_key: str | None = None,
        thread_id: int | None = None,
    ) -> str:
        _ = thread_id
        if isinstance(scope_key, str) and scope_key.strip():
            return scope_key.strip()
        return "thread:test"


class TestObjectMappingService(unittest.TestCase):
    def test_store_mapped_object_supports_explicit_raw_text_entity_and_relation_models(self) -> None:
        repository = _RecordingKnowledgeRepository()
        mapping_service = ObjectMappingService(
            KnowledgeObjectService(repository),
            RuntimeConfigObject(agents_db_uri="mongodb://unused"),
        )

        result = mapping_service.store_mapped_object(
            object_name="job_postings",
            fallback_correlation_id="job-explicit-1",
            result_payload={
                "agent": "job_posting_parser",
                "correlation_id": "job-explicit-1",
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "file": {"content_sha256": "job-explicit-1", "path": "/tmp/job-explicit-1.pdf"},
                "link": {"thread_id": "thread-1", "message_id": "message-1"},
                "db_updates": {"correlation_id": "job-explicit-1", "content_sha256": "job-explicit-1", "processing_state": "processed", "processed": True},
                "raw_text_document": {
                    "title": "Knowledge Graph Engineer",
                    "language": "de",
                    "raw_text": "Knowledge Graph Engineer\nPlatform Co\nPython\nMongoDB",
                    "sections": [
                        {
                            "section_key": "header",
                            "heading": "Object Header",
                            "text": "Title: Knowledge Graph Engineer\nOrganization: Platform Co",
                        },
                        {
                            "section_key": "requirements",
                            "heading": "Requirements",
                            "text": "- Python\n- MongoDB",
                        },
                    ],
                    "metadata": {"source": "unit_test"},
                },
                "entity_objects": [
                    {
                        "entity_key": "subject",
                        "entity_type": "job_posting",
                        "canonical_name": "Knowledge Graph Engineer",
                        "mention_text": "Knowledge Graph Engineer",
                        "section_key": "header",
                        "summary": "Primary job posting subject.",
                        "metadata": {"role": "subject", "source_field": "job_posting.job_title"},
                    },
                    {
                        "entity_key": "organization:platform_co",
                        "entity_type": "organization",
                        "canonical_name": "Platform Co",
                        "mention_text": "Platform Co",
                        "section_key": "header",
                    },
                    {
                        "entity_key": "skill:python",
                        "entity_type": "skill",
                        "canonical_name": "Python",
                        "mention_text": "Python",
                        "section_key": "requirements",
                    },
                    {
                        "entity_key": "database:mongodb",
                        "entity_type": "database",
                        "canonical_name": "MongoDB",
                        "mention_text": "MongoDB",
                        "section_key": "requirements",
                    },
                ],
                "relation_objects": [
                    {
                        "source_entity_key": "subject",
                        "target_entity_key": "organization:platform_co",
                        "relation_type": "offered_by",
                        "section_key": "header",
                    },
                    {
                        "source_entity_key": "subject",
                        "target_entity_key": "skill:python",
                        "relation_type": "requires_skill",
                        "section_key": "requirements",
                    },
                    {
                        "source_entity_key": "subject",
                        "target_entity_key": "database:mongodb",
                        "relation_type": "requires_database_knowledge",
                        "section_key": "requirements",
                    },
                ],
            },
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["stored"])
        self.assertEqual(result["document_id"], "doc:job_posting:job-explicit-1")
        self.assertEqual(result["entity_count"], 4)
        self.assertEqual(result["relation_count"], 3)

        document_record = repository.records_by_object_name["document"]["doc:job_posting:job-explicit-1"]
        self.assertEqual(document_record["title"], "Knowledge Graph Engineer")
        self.assertEqual(document_record["metadata"]["parse"]["language"], "de")
        self.assertEqual(len(document_record["blocks"]), 2)

        entity_bucket = repository.records_by_object_name["entity"]
        self.assertIn("ent:job_posting:organization:platform_co", entity_bucket)
        self.assertIn("ent:job_posting:skill:python", entity_bucket)

        relation_bucket = repository.records_by_object_name["relation"]
        relation_types = {relation_record["relation_type"] for relation_record in relation_bucket.values()}
        self.assertEqual(
            relation_types,
            {"offered_by", "requires_skill", "requires_database_knowledge"},
        )

    def test_store_mapped_object_builds_relations_from_target_annotated_entity_seeds(self) -> None:
        repository = _RecordingKnowledgeRepository()
        mapping_service = ObjectMappingService(
            KnowledgeObjectService(repository),
            RuntimeConfigObject(agents_db_uri="mongodb://unused"),
        )

        result = mapping_service.store_mapped_object(
            object_name="job_postings",
            fallback_correlation_id="job-target-seed-1",
            result_payload={
                "agent": "job_posting_parser",
                "correlation_id": "job-target-seed-1",
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "file": {"content_sha256": "job-target-seed-1", "path": "/tmp/job-target-seed-1.pdf"},
                "db_updates": {"correlation_id": "job-target-seed-1", "content_sha256": "job-target-seed-1", "processing_state": "processed", "processed": True},
                "raw_text_document": {
                    "title": "Platform Team Engineer",
                    "language": "de",
                    "raw_text": "Platform Team Engineer\nPlatform Team\nPython",
                    "sections": [
                        {
                            "section_key": "header",
                            "heading": "Object Header",
                            "text": "Title: Platform Team Engineer\nTeam: Platform Team",
                        },
                        {
                            "section_key": "requirements",
                            "heading": "Requirements",
                            "text": "- Python",
                        },
                    ],
                    "metadata": {"source": "unit_test"},
                },
                "entity_objects": [
                    {
                        "entity_key": "subject",
                        "entity_type": "job_posting",
                        "canonical_name": "Platform Team Engineer",
                        "mention_text": "Platform Team Engineer",
                        "section_key": "header",
                        "summary": "Primary job posting subject.",
                        "metadata": {"role": "subject", "source_field": "job_posting.job_title"},
                    },
                    {
                        "entity_key": "team:platform_team",
                        "entity_type": "team",
                        "canonical_name": "Platform Team",
                        "mention_text": "Platform Team",
                        "section_key": "header",
                    },
                    {
                        "entity_key": "skill:python",
                        "entity_type": "skill",
                        "canonical_name": "Python",
                        "mention_text": "Python",
                        "section_key": "requirements",
                        "is_target": True,
                        "source_entity": "team:platform_team",
                        "is_relational": "requires_skill",
                        "explicit_description": "Platform Team requires Python for the role.",
                    },
                ],
            },
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["stored"])
        self.assertEqual(result["entity_count"], 3)
        self.assertEqual(result["relation_count"], 1)

        entity_bucket = repository.records_by_object_name["entity"]
        relation_bucket = repository.records_by_object_name["relation"]
        team_entity_id = next(record["id"] for record in entity_bucket.values() if record["canonical_name"] == "Platform Team")
        python_entity_id = next(record["id"] for record in entity_bucket.values() if record["canonical_name"] == "Python")
        relation_record = next(iter(relation_bucket.values()))

        self.assertEqual(relation_record["relation_type"], "requires_skill")
        self.assertEqual(relation_record["source_entity_id"], team_entity_id)
        self.assertEqual(relation_record["target_entity_id"], python_entity_id)
        self.assertEqual(relation_record["metadata"]["relation_description"], "Platform Team requires Python for the role.")
        self.assertEqual(relation_record["metadata"]["mapped_from"], "explicit_entity_model")

    def test_store_mapped_object_maps_requirement_tools_and_skills_to_relations(self) -> None:
        repository = _RecordingKnowledgeRepository()
        mapping_service = ObjectMappingService(
            KnowledgeObjectService(repository),
            RuntimeConfigObject(agents_db_uri="mongodb://unused"),
        )

        result = mapping_service.store_mapped_object(
            object_name="job_postings",
            fallback_correlation_id="job-tools-1",
            result_payload={
                "agent": "job_posting_parser",
                "correlation_id": "job-tools-1",
                "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                "file": {"content_sha256": "job-tools-1", "path": "/tmp/job-tools-1.pdf"},
                "db_updates": {
                    "correlation_id": "job-tools-1",
                    "content_sha256": "job-tools-1",
                    "processing_state": "processed",
                    "processed": True,
                },
                "job_posting": {
                    "job_title": "Runtime Persisted Engineer",
                    "company_name": "Route Storage Co",
                    "requirements": {
                        "technical_skills": [
                            {"name": "Python", "type": "skill"},
                            {"name": "GitLab", "type": "tool"},
                            "Docker",
                        ],
                        "tools": [
                            {"name": "Jira", "type": "tool"},
                            "Confluence",
                        ],
                        "soft_skills": [],
                        "languages": [],
                    },
                    "application": {
                        "deadline": None,
                        "application_link": None,
                        "contact_email": None,
                        "contact_person": None,
                    },
                    "metadata": {},
                },
            },
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["stored"])

        entity_bucket = repository.records_by_object_name["entity"]
        relation_bucket = repository.records_by_object_name["relation"]

        python_entity = next(
            (record for record in entity_bucket.values() if record.get("canonical_name") == "Python"),
            None,
        )
        self.assertIsNotNone(python_entity)
        self.assertEqual((python_entity or {}).get("entity_type"), "skill")

        tool_entity_name_set = {
            str(record.get("canonical_name") or "")
            for record in entity_bucket.values()
            if str(record.get("entity_type") or "") == "tool"
        }
        self.assertTrue({"GitLab", "Docker", "Jira", "Confluence"}.issubset(tool_entity_name_set))

        relation_type_set = {
            str(relation_record.get("relation_type") or "")
            for relation_record in relation_bucket.values()
        }
        self.assertIn("requires_skill", relation_type_set)
        self.assertIn("requires_tool", relation_type_set)

        requires_tool_source_field_set = {
            str((relation_record.get("metadata") or {}).get("source_field") or "")
            for relation_record in relation_bucket.values()
            if str(relation_record.get("relation_type") or "") == "requires_tool"
        }
        self.assertIn("requirements.tools", requires_tool_source_field_set)


class TestRelationQueryService(unittest.TestCase):
    def test_format_chunk_payload_list_includes_relation_description(self) -> None:
        service = AgentDbQueryService()

        chunks = service._format_chunk_payload_list(
            [
                {
                    "score": 0.88,
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
        self.assertEqual(chunks[0]["score"], 0.88)


class TestAgentDbOperationService(unittest.TestCase):
    def test_execute_operation_load_object_promotes_relation_description(self) -> None:
        service = AgentDbOperationService()
        repository = _StubAgentDbOperationRepository()

        with patch.object(service, "_load_repository", return_value=repository):
            result_payload = json.loads(
                service.execute_operation(
                    operation="load_object",
                    object_name="relation",
                    object_id="rel:sample:demo",
                    backend_uri="agentsmem://local",
                )
            )

        self.assertTrue(result_payload["ok"])
        self.assertEqual(result_payload["operation"], "load_object")
        self.assertEqual(
            (result_payload.get("object_payload") or {}).get("relation_description"),
            "sample.py defines the Demo class.",
        )

    def test_execute_operation_load_relation_graph_promotes_relation_description(self) -> None:
        service = AgentDbOperationService()
        repository = _StubAgentDbOperationRepository()

        with patch.object(service, "_load_repository", return_value=repository):
            result_payload = json.loads(
                service.execute_operation(
                    operation="load_relation_graph",
                    namespace_id="ns_repo",
                    source_entity_id="ent:module:sample",
                    backend_uri="agentsmem://local",
                )
            )

        self.assertTrue(result_payload["ok"])
        self.assertEqual(result_payload["operation"], "load_relation_graph")
        self.assertEqual(
            ((result_payload.get("object_payload_list") or [{}])[0]).get("relation_description"),
            "sample.py defines the Demo class.",
        )


class TestEntityRelationEmbeddingService(unittest.TestCase):
    def test_build_object_text_includes_relation_description(self) -> None:
        service = EntityRelationEmbeddingService(
            KnowledgeObjectService(_RecordingKnowledgeRepository()),
            RuntimeConfigObject(agents_db_uri="mongodb://unused"),
        )

        object_text = service.build_object_text(
            "relation",
            {
                "relation_type": "defines_class",
                "source_entity_id": "ent:module:sample",
                "target_entity_id": "ent:class:demo",
                "metadata": {
                    "relation_description": "sample.py defines the Demo class.",
                },
            },
        )

        self.assertIn("defines_class", object_text)
        self.assertIn("sample.py defines the Demo class.", object_text)


class TestAgentMemoryAttachmentService(unittest.TestCase):
    def test_load_attachment_payload_uses_job_default_object_for_document_field(self) -> None:
        service = AgentMemoryAttachmentService(_StubAgentMemoryService())

        attachment_payload = service.load_attachment_payload(
            handoff_payload={
                "output": {
                    "document": {
                        "id": "cover-letter-doc-001",
                    }
                }
            },
            handoff_metadata={"job_name": "cover_letter_writer"},
            runtime_metadata={"job_name": "cover_letter_writer"},
        )

        attachments = list(attachment_payload.get("attachments") or [])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get("obj_name"), "cover_letters")
        self.assertEqual(attachments[0].get("correlation_id"), "cover-letter-doc-001")
        self.assertEqual(attachments[0].get("source_field"), "document")
        self.assertEqual(attachment_payload.get("job_name"), "cover_letter_writer")

    def test_load_attachment_payload_uses_runtime_job_skill_profiles_when_job_name_missing(self) -> None:
        service = AgentMemoryAttachmentService(_StubAgentMemoryService())

        attachment_payload = service.load_attachment_payload(
            handoff_payload={
                "output": {
                    "document": {
                        "id": "cover-letter-doc-002",
                    }
                }
            },
            handoff_metadata={},
            runtime_metadata={
                "job_skill_profiles": {
                    "cover_letter_writer": "xworker_cover_letter_writer",
                }
            },
        )

        attachments = list(attachment_payload.get("attachments") or [])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get("obj_name"), "cover_letters")
        self.assertEqual(attachments[0].get("correlation_id"), "cover-letter-doc-002")
        self.assertEqual(attachments[0].get("source_field"), "document")

    def test_load_attachment_payload_uses_generic_root_keys_without_runtime_config(self) -> None:
        service = AgentMemoryAttachmentService(_StubAgentMemoryService())

        attachment_payload = service.load_attachment_payload(
            handoff_payload={
                "output": {
                    "obj_name": "documents",
                    "correlation_id": "generic-correlation-001",
                    "result": {
                        "title": "Generic Payload",
                    },
                }
            },
            handoff_metadata={},
            runtime_metadata={"job_name": "unknown_job"},
        )

        attachments = list(attachment_payload.get("attachments") or [])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get("obj_name"), "documents")
        self.assertEqual(attachments[0].get("correlation_id"), "generic-correlation-001")
        self.assertEqual(attachments[0].get("source_field"), "output")

    def test_load_attachment_payload_infers_profile_object_from_generic_id_key(self) -> None:
        service = AgentMemoryAttachmentService(_StubAgentMemoryService())

        attachment_payload = service.load_attachment_payload(
            handoff_payload={
                "output": {
                    "parsed_profile": {
                        "id": "profile-generic-001",
                    }
                }
            },
            handoff_metadata={},
            runtime_metadata={},
        )

        attachments = list(attachment_payload.get("attachments") or [])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get("obj_name"), "profiles")
        self.assertEqual(attachments[0].get("correlation_id"), "profile-generic-001")
        self.assertEqual(attachments[0].get("source_field"), "parsed_profile")


if __name__ == "__main__":
    unittest.main()