import unittest
from unittest.mock import patch

from alde.agents_db import (
    DocumentObject,
    EntityObject,
    EntityRelationObject,
    EntityRelationEmbeddingService,
    KnowledgeObjectService,
    KnowledgeRepository,
    NamespaceObject,
    ObjectMappingService,
    RuntimeConfigObject,
)


class AgentsDbEmbeddingIngestSmokeTest(unittest.TestCase):
    def test_store_mapped_object_persists_entity_and_relation_embeddings(self):
        repository = KnowledgeRepository()
        knowledge_service = KnowledgeObjectService(repository)
        runtime_config = RuntimeConfigObject(
            agents_db_uri="agentsmem://local",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            namespace_slug="ns-test",
            namespace_name="NS Test",
            default_embedding_model="smoke-model",
            default_embedding_dimension=3,
        )
        service = ObjectMappingService(knowledge_service=knowledge_service, runtime_config=runtime_config)

        namespace_object = NamespaceObject(
            id="ns_test",
            tenant_id="tenant_test",
            slug="ns-test",
            name="NS Test",
            default_embedding_model="smoke-model",
            default_embedding_dimension=3,
            index_backend="cosine",
        )
        document_object = DocumentObject(
            id="doc_1",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            document_type="job_posting",
            title="ML Engineer",
            source_uri="memory://doc_1",
            content_sha256="sha-doc-1",
        )
        entity_objects = [
            EntityObject(
                id="ent_subject",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                entity_type="role",
                canonical_name="ML Engineer",
                summary="Builds ML systems",
            ),
            EntityObject(
                id="ent_company",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                entity_type="organization",
                canonical_name="ALDE",
                summary="AI company",
            ),
        ]
        relation_objects = [
            EntityRelationObject(
                id="rel_works_at",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                source_entity_id="ent_subject",
                target_entity_id="ent_company",
                relation_type="works_at",
                confidence=0.9,
                weight=0.9,
                metadata={"relation_description": "subject works at company"},
            )
        ]

        with (
            patch.object(service, "load_object_payload", return_value={"job_title": "ML Engineer"}),
            patch.object(service, "build_document_object", return_value=document_object),
            patch.object(service, "build_block_seed_objects", return_value=[]),
            patch.object(service, "build_entity_candidate_objects", return_value=[]),
            patch.object(service, "build_entity_objects", return_value=entity_objects),
            patch.object(service, "build_relation_objects", return_value=relation_objects),
            patch.object(EntityRelationEmbeddingService, "embed_object", return_value=[0.1, 0.2, 0.3]),
        ):
            result = service.store_mapped_object(
                object_name="job_posting",
                result_payload={"parse": {"is_job_posting": True}},
            )

        self.assertTrue(result.get("stored"))
        self.assertEqual(result.get("entity_embedding_count"), 2)
        self.assertEqual(result.get("relation_embedding_count"), 1)

        entity_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "entity"},
            limit=50,
        )
        relation_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "relation"},
            limit=50,
        )

        self.assertEqual(len(entity_embeddings), 2)
        self.assertEqual(len(relation_embeddings), 1)
        self.assertEqual(len(relation_embeddings[0].get("embedding") or []), 3)
        self.assertIn("works_at", str((relation_embeddings[0].get("metadata") or {}).get("source_text") or ""))

    def test_store_mapped_object_reingest_is_embedding_idempotent(self):
        repository = KnowledgeRepository()
        knowledge_service = KnowledgeObjectService(repository)
        runtime_config = RuntimeConfigObject(
            agents_db_uri="agentsmem://local",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            namespace_slug="ns-test",
            namespace_name="NS Test",
            default_embedding_model="smoke-model",
            default_embedding_dimension=3,
        )
        service = ObjectMappingService(knowledge_service=knowledge_service, runtime_config=runtime_config)

        document_object = DocumentObject(
            id="doc_1",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            document_type="job_posting",
            title="ML Engineer",
            source_uri="memory://doc_1",
            content_sha256="sha-doc-1",
        )
        entity_objects = [
            EntityObject(
                id="ent_subject",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                entity_type="role",
                canonical_name="ML Engineer",
                summary="Builds ML systems",
            ),
            EntityObject(
                id="ent_company",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                entity_type="organization",
                canonical_name="ALDE",
                summary="AI company",
            ),
        ]
        relation_objects = [
            EntityRelationObject(
                id="rel_works_at",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                source_entity_id="ent_subject",
                target_entity_id="ent_company",
                relation_type="works_at",
                confidence=0.9,
                weight=0.9,
                metadata={"relation_description": "subject works at company"},
            )
        ]

        with (
            patch.object(service, "load_object_payload", return_value={"job_title": "ML Engineer"}),
            patch.object(service, "build_document_object", return_value=document_object),
            patch.object(service, "build_block_seed_objects", return_value=[]),
            patch.object(service, "build_entity_candidate_objects", return_value=[]),
            patch.object(service, "build_entity_objects", return_value=entity_objects),
            patch.object(service, "build_relation_objects", return_value=relation_objects),
            patch.object(EntityRelationEmbeddingService, "embed_object", return_value=[0.1, 0.2, 0.3]),
        ):
            first_result = service.store_mapped_object(
                object_name="job_posting",
                result_payload={"parse": {"is_job_posting": True}},
            )
            second_result = service.store_mapped_object(
                object_name="job_posting",
                result_payload={"parse": {"is_job_posting": True}},
            )

        self.assertTrue(first_result.get("stored"))
        self.assertTrue(second_result.get("stored"))
        self.assertEqual(first_result.get("entity_embedding_count"), 2)
        self.assertEqual(first_result.get("relation_embedding_count"), 1)
        self.assertEqual(second_result.get("entity_embedding_count"), 2)
        self.assertEqual(second_result.get("relation_embedding_count"), 1)

        entity_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "entity"},
            limit=50,
        )
        relation_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "relation"},
            limit=50,
        )

        self.assertEqual(len(entity_embeddings), 2)
        self.assertEqual(len(relation_embeddings), 1)

    def test_store_mapped_object_changed_relation_text_creates_new_embedding_record(self):
        repository = KnowledgeRepository()
        knowledge_service = KnowledgeObjectService(repository)
        runtime_config = RuntimeConfigObject(
            agents_db_uri="agentsmem://local",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            namespace_slug="ns-test",
            namespace_name="NS Test",
            default_embedding_model="smoke-model",
            default_embedding_dimension=3,
        )
        service = ObjectMappingService(knowledge_service=knowledge_service, runtime_config=runtime_config)

        document_object = DocumentObject(
            id="doc_1",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            document_type="job_posting",
            title="ML Engineer",
            source_uri="memory://doc_1",
            content_sha256="sha-doc-1",
        )
        entity_objects = [
            EntityObject(
                id="ent_subject",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                entity_type="role",
                canonical_name="ML Engineer",
                summary="Builds ML systems",
            ),
            EntityObject(
                id="ent_company",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                entity_type="organization",
                canonical_name="ALDE",
                summary="AI company",
            ),
        ]
        relation_objects_v1 = [
            EntityRelationObject(
                id="rel_works_at",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                source_entity_id="ent_subject",
                target_entity_id="ent_company",
                relation_type="works_at",
                confidence=0.9,
                weight=0.9,
                metadata={"relation_description": "subject works at company"},
            )
        ]
        relation_objects_v2 = [
            EntityRelationObject(
                id="rel_works_at",
                tenant_id="tenant_test",
                namespace_id="ns_test",
                source_entity_id="ent_subject",
                target_entity_id="ent_company",
                relation_type="works_at",
                confidence=0.9,
                weight=0.9,
                metadata={"relation_description": "subject works at innovative company"},
            )
        ]

        with (
            patch.object(service, "load_object_payload", return_value={"job_title": "ML Engineer"}),
            patch.object(service, "build_document_object", return_value=document_object),
            patch.object(service, "build_block_seed_objects", return_value=[]),
            patch.object(service, "build_entity_candidate_objects", return_value=[]),
            patch.object(service, "build_entity_objects", return_value=entity_objects),
            patch.object(EntityRelationEmbeddingService, "embed_object", return_value=[0.1, 0.2, 0.3]),
        ):
            with patch.object(service, "build_relation_objects", return_value=relation_objects_v1):
                service.store_mapped_object(
                    object_name="job_posting",
                    result_payload={"parse": {"is_job_posting": True}},
                )
            with patch.object(service, "build_relation_objects", return_value=relation_objects_v2):
                service.store_mapped_object(
                    object_name="job_posting",
                    result_payload={"parse": {"is_job_posting": True}},
                )

        relation_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "relation"},
            limit=50,
        )
        self.assertEqual(len(relation_embeddings), 2)
        content_hashes = {
            str(embedding_payload.get("content_sha256") or "")
            for embedding_payload in relation_embeddings
            if isinstance(embedding_payload, dict)
        }
        self.assertEqual(len(content_hashes), 2)


if __name__ == "__main__":
    unittest.main()
