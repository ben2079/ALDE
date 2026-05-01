from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from alde.agents_db import (
    AgentMemoryAttachmentService,
    KnowledgeObjectService,
    ObjectMappingService,
    RuntimeConfigObject,
)


class _RecordingKnowledgeRepository:
    def __init__(self) -> None:
        self.records_by_object_name: dict[str, dict[str, dict[str, Any]]] = {}

    def upsert_object(self, object_name: str, object_id: str, object_payload: dict[str, Any]) -> dict[str, Any]:
        bucket = self.records_by_object_name.setdefault(str(object_name), {})
        bucket[str(object_id)] = dict(object_payload)
        return bucket[str(object_id)]


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