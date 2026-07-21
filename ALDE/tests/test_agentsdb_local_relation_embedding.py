import unittest

from alde.agents_db import (
    EntityRelationObject,
    KnowledgeObjectService,
    KnowledgeRepository,
    LocalRelationEmbeddingService,
    RuntimeConfigObject,
)


class AgentsDbLocalRelationEmbeddingTest(unittest.TestCase):
    def test_local_relation_embedding_service_processes_namespace_without_third_party_model(self):
        repository = KnowledgeRepository()
        knowledge_service = KnowledgeObjectService(repository)
        runtime_config = RuntimeConfigObject(
            agents_db_uri="agentsmem://local",
            tenant_id="tenant_test",
            namespace_id="ns_test",
        )
        relation_a = EntityRelationObject(
            id="rel_a",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            source_entity_id="ent_1",
            target_entity_id="ent_2",
            relation_type="depends_on",
            confidence=0.9,
            weight=0.8,
            metadata={"relation_description": "service A depends on service B"},
        )
        relation_b = EntityRelationObject(
            id="rel_b",
            tenant_id="tenant_test",
            namespace_id="ns_test",
            source_entity_id="ent_2",
            target_entity_id="ent_3",
            relation_type="calls",
            confidence=0.7,
            weight=0.6,
            metadata={"relation_description": "service B calls service C"},
        )
        knowledge_service.store_relation_object(relation_a)
        knowledge_service.store_relation_object(relation_b)

        service = LocalRelationEmbeddingService(
            knowledge_service=knowledge_service,
            runtime_config=runtime_config,
            dimension=32,
        )
        result = service.process_namespace_objects(
            object_name="relation",
            namespace_id="ns_test",
            limit=100,
        )

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("stored_count"), 2)
        self.assertEqual(result.get("dimension"), 32)
        self.assertGreater(result.get("idf_vocab_size") or 0, 0)

        relation_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "relation"},
            limit=20,
        )
        self.assertEqual(len(relation_embeddings), 2)
        self.assertTrue(
            all(len(embedding_payload.get("embedding") or []) == 32 for embedding_payload in relation_embeddings),
        )
        self.assertTrue(
            all(str(embedding_payload.get("model_id") or "") == "local-hash-idf-v1" for embedding_payload in relation_embeddings),
        )

    def test_local_relation_embedding_service_creates_new_record_on_content_change(self):
        repository = KnowledgeRepository()
        knowledge_service = KnowledgeObjectService(repository)
        runtime_config = RuntimeConfigObject(
            agents_db_uri="agentsmem://local",
            tenant_id="tenant_test",
            namespace_id="ns_test",
        )
        service = LocalRelationEmbeddingService(
            knowledge_service=knowledge_service,
            runtime_config=runtime_config,
            dimension=16,
        )
        relation_v1 = {
            "id": "rel_x",
            "namespace_id": "ns_test",
            "source_entity_id": "ent_1",
            "target_entity_id": "ent_2",
            "relation_type": "supports",
            "weight": 0.8,
            "confidence": 0.8,
            "metadata": {"relation_description": "model supports ranking"},
        }
        relation_v2 = {
            "id": "rel_x",
            "namespace_id": "ns_test",
            "source_entity_id": "ent_1",
            "target_entity_id": "ent_2",
            "relation_type": "supports",
            "weight": 0.8,
            "confidence": 0.8,
            "metadata": {"relation_description": "model strongly supports ranking"},
        }

        service.fit_namespace_idf(object_name="relation", namespace_id="ns_test")
        service.process_object("relation", relation_v1, owner_id="rel_x")
        service.process_object("relation", relation_v2, owner_id="rel_x")

        relation_embeddings = repository.load_objects(
            "embedding",
            {"namespace_id": "ns_test", "owner_type": "relation"},
            limit=20,
        )
        self.assertEqual(len(relation_embeddings), 2)
        self.assertEqual(
            len(
                {
                    str(embedding_payload.get("content_sha256") or "")
                    for embedding_payload in relation_embeddings
                },
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
