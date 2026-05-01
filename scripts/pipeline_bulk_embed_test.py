"""pipeline_bulk_embed_test.py

Full pipeline performance test:
  1. Load all entities, relations, and document blocks from agentsdb.json
  2. Embed each via EntityRelationEmbeddingService (parallel batches)
  3. Run cosine-similarity retrieval against the embedded vectors
  4. Report per-stage timing and summary

Usage:
    cd /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt
    source .venv/bin/activate
    python scripts/pipeline_bulk_embed_test.py

Optional ENV:
    AI_IDE_ENTITY_EMBEDDING_MODEL   (default: paraphrase-multilingual-MiniLM-L12-v2)
    AI_IDE_EMBEDDINGS_DEVICE        (default: auto)
    PIPELINE_TEST_RETRIEVAL_K       (default: 5)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup – allow running from project root without installing the package
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_ALDE_SRC = _ROOT / "ALDE" / "alde"
for _p in (_ROOT, _ALDE_SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents_db import (  # type: ignore
    AgentDbInMemoryRepository,
    EntityRelationEmbeddingService,
    KnowledgeObjectService,
    RuntimeConfigObject,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DB_PATH = str(_ROOT / "AppData" / "agentsdb.json")
_RETRIEVAL_K = int(os.getenv("PIPELINE_TEST_RETRIEVAL_K", "5"))
_EMBED_WORKERS = int(os.getenv("PIPELINE_TEST_EMBED_WORKERS", "4"))

_RETRIEVAL_QUERIES = [
    # Job-Posting Queries
    "Python software engineer automation",
    "Data pipeline automation engineer",
    "ERP integration developer SAP",
    "Remote work Python developer",
    "Fullstack software developer web",
    "Senior data engineer platform cloud",
    "Knowledge graph pipeline engineer",
    "Autonomous workflow dispatcher agent",
    # Profil / Bewerber Queries
    "Applicant profile Python developer skills",
    "Candidate Deutsch Englisch software development",
    "Agile development ownership responsibility",
    "Software engineer machine learning NLP",
    # System / Runtime Queries
    "dispatcher document database records",
    "job postings database schema",
    "runtime configuration tabs agent",
    "profiles database candidate information",
]


# ---------------------------------------------------------------------------
# Domain: cosine retrieval against in-memory embedding store
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class InMemoryCosineRetriever:
    """Simple in-memory cosine retrieval from the embeddings collection."""

    def __init__(self, embeddings: dict[str, dict[str, Any]]) -> None:
        # Pre-compute index: list of (owner_id, owner_type, vector)
        self._index: list[tuple[str, str, list[float]]] = []
        for payload in embeddings.values():
            vec = payload.get("embedding")
            owner_id = str(payload.get("owner_id") or "")
            owner_type = str(payload.get("owner_type") or "")
            if vec and owner_id:
                self._index.append((owner_id, owner_type, vec))

    def query(self, query_vector: list[float], k: int = 5) -> list[dict[str, Any]]:
        scored = [
            {
                "owner_id": owner_id,
                "owner_type": owner_type,
                "score": _cosine(query_vector, vec),
            }
            for owner_id, owner_type, vec in self._index
        ]
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:k]


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

class PipelineBulkEmbedService:
    """Orchestrates bulk embedding for entities, relations, and document blocks."""

    def __init__(
        self,
        emb_svc: EntityRelationEmbeddingService,
        repo: AgentDbInMemoryRepository,
    ) -> None:
        self._emb_svc = emb_svc
        self._repo = repo

    def load_object_name(
        self,
        object_name: str,
        *,
        skip_embedded: bool = True,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Load all (owner_id, payload) pairs for a given collection type.

        When skip_embedded=True, items whose owner_id is already in the
        embeddings collection are skipped (incremental mode).
        """
        collection = self._repo._load_collection_object(object_name)
        if skip_embedded:
            existing = self._already_embedded_ids()
            return [
                (oid, dict(payload))
                for oid, payload in collection.items()
                if oid not in existing
            ]
        return [(oid, dict(payload)) for oid, payload in collection.items()]

    def _already_embedded_ids(self) -> set[str]:
        emb_col = self._repo._load_collection_object("embedding")
        return {str(v.get("owner_id") or "") for v in emb_col.values()}

    def collect_blocks(self, *, skip_embedded: bool = True) -> list[tuple[str, dict[str, Any]]]:
        """Extract all blocks from document collection as (block_id, block_dict) pairs."""
        docs = self._repo._load_collection_object("document")
        existing = self._already_embedded_ids() if skip_embedded else set()
        result: list[tuple[str, dict[str, Any]]] = []
        for doc_payload in docs.values():
            for block in (doc_payload.get("blocks") or []):
                if isinstance(block, dict):
                    bid = str(block.get("block_id") or "")
                    if bid and bid not in existing:
                        result.append((bid, block))
        return result

    def collect_documents(
        self,
        *,
        skip_embedded: bool = True,
        min_text_length: int = 8,
        allowed_types: list[str] | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Collect embeddable documents (those without blocks or with meaningful text).

        Builds a synthetic owner_id as ``doc:<doc_id>`` to avoid collisions.
        Only includes documents whose build_object_text result is non-empty.
        """
        docs = self._repo._load_collection_object("document")
        existing = self._already_embedded_ids() if skip_embedded else set()
        result: list[tuple[str, dict[str, Any]]] = []
        for doc_id, doc_payload in docs.items():
            # Use synthetic owner_id prefixed with doc: so it is distinct from block-IDs
            owner_id = f"doc:{doc_id}" if not str(doc_id).startswith("doc:") else doc_id
            if owner_id in existing:
                continue
            dtype = str(doc_payload.get("document_type") or "").strip()
            if allowed_types and dtype not in allowed_types:
                continue
            # Skip docs that already have blocks (blocks are embedded separately)
            if doc_payload.get("blocks"):
                continue
            text = self._emb_svc.build_object_text("document", doc_payload)
            if len(text.strip()) >= min_text_length:
                # Attach the synthetic owner_id so store_object can use it
                payload_with_id = dict(doc_payload)
                payload_with_id["_owner_id"] = owner_id
                result.append((owner_id, payload_with_id))
        return result

    def embed_batch(
        self,
        object_name: str,
        items: list[tuple[str, dict[str, Any]]],
        *,
        workers: int = 4,
    ) -> dict[str, Any]:
        """Embed a batch in parallel threads. Returns stage report."""
        stored = 0
        failed = 0
        skipped = 0
        errors: list[str] = []
        t0 = time.perf_counter()

        def _embed_one(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
            owner_id, payload = item
            try:
                return self._emb_svc.process_object(object_name, payload, owner_id=owner_id)
            except Exception as exc:
                return {"stored": False, "owner_id": owner_id, "error": str(exc)}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_embed_one, item): item for item in items}
            for fut in as_completed(futures):
                result = fut.result()
                if result.get("stored"):
                    stored += 1
                elif result.get("reason") == "empty_text":
                    skipped += 1
                else:
                    failed += 1
                    errors.append(str(result.get("error") or result.get("reason") or "unknown"))

        elapsed = time.perf_counter() - t0
        return {
            "object_name": object_name,
            "total": len(items),
            "stored": stored,
            "skipped": skipped,
            "failed": failed,
            "elapsed_s": round(elapsed, 3),
            "rate_per_s": round(stored / elapsed, 2) if elapsed > 0 and stored > 0 else 0,
            "errors": errors[:5],
        }


# ---------------------------------------------------------------------------
# Retrieval stage
# ---------------------------------------------------------------------------

class RetrievalTestService:
    """Run retrieval smoke tests against the populated embedding index."""

    def __init__(
        self,
        retriever: InMemoryCosineRetriever,
        emb_svc: EntityRelationEmbeddingService,
    ) -> None:
        self._retriever = retriever
        self._emb_svc = emb_svc

    def run_query_object(self, query_text: str, k: int = 5) -> dict[str, Any]:
        t0 = time.perf_counter()
        vec = self._emb_svc.embed_object("query", query_text)
        hits = self._retriever.query(vec, k=k)
        elapsed = time.perf_counter() - t0
        return {
            "query": query_text,
            "elapsed_ms": round((elapsed) * 1000, 1),
            "hits": hits,
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def main() -> None:
    _print_section("ALDE Pipeline Bulk Embed + Retrieval Test")
    print(f"  agentsdb: {_DB_PATH}")
    print(f"  embed workers: {_EMBED_WORKERS}")
    print(f"  retrieval k: {_RETRIEVAL_K}")

    # --- Setup ---
    _print_section("1 / Setup – loading repository + services")
    t_start = time.perf_counter()

    repo = AgentDbInMemoryRepository(_DB_PATH)
    config = RuntimeConfigObject(
        agents_db_uri="agentsmem://local",
    )
    ks = KnowledgeObjectService(repo)  # type: ignore[arg-type]  # duck typing
    emb_svc = EntityRelationEmbeddingService(ks, config)
    pipeline_svc = PipelineBulkEmbedService(emb_svc, repo)

    # Warm-up encoder (triggers model download / load)
    print("  Loading embedding model (first call, may take a moment) ...")
    t_model = time.perf_counter()
    _ = emb_svc.embed_object("warmup", "warm-up")
    print(f"  Model ready in {time.perf_counter() - t_model:.2f}s  |  model: {emb_svc._model_name()}")

    # --- Stage 1: entities ---
    _print_section("2 / Stage: embed entities (incremental)")
    entities = pipeline_svc.load_object_name("entity", skip_embedded=True)
    print(f"  New (not yet embedded): {len(entities)} entities")
    stage_ent = pipeline_svc.embed_batch("entity", entities, workers=_EMBED_WORKERS)
    print(f"  stored={stage_ent['stored']}  skipped={stage_ent['skipped']}  "
          f"failed={stage_ent['failed']}  elapsed={stage_ent['elapsed_s']}s  "
          f"rate={stage_ent['rate_per_s']}/s")
    if stage_ent["errors"]:
        print(f"  errors: {stage_ent['errors']}")

    # --- Stage 2: relations ---
    _print_section("3 / Stage: embed relations (incremental)")
    relations = pipeline_svc.load_object_name("relation", skip_embedded=True)
    print(f"  New (not yet embedded): {len(relations)} relations")
    stage_rel = pipeline_svc.embed_batch("relation", relations, workers=_EMBED_WORKERS)
    print(f"  stored={stage_rel['stored']}  skipped={stage_rel['skipped']}  "
          f"failed={stage_rel['failed']}  elapsed={stage_rel['elapsed_s']}s  "
          f"rate={stage_rel['rate_per_s']}/s")
    if stage_rel["errors"]:
        print(f"  errors: {stage_rel['errors']}")

    # --- Stage 3: document blocks ---
    _print_section("4 / Stage: embed document blocks (incremental)")
    blocks = pipeline_svc.collect_blocks(skip_embedded=True)
    print(f"  New (not yet embedded): {len(blocks)} blocks")
    stage_blk = pipeline_svc.embed_batch("block", blocks, workers=_EMBED_WORKERS)
    print(f"  stored={stage_blk['stored']}  skipped={stage_blk['skipped']}  "
          f"failed={stage_blk['failed']}  elapsed={stage_blk['elapsed_s']}s  "
          f"rate={stage_blk['rate_per_s']}/s")
    if stage_blk["errors"]:
        print(f"  errors: {stage_blk['errors']}")

    # --- Stage 4: documents (without blocks, e.g. ai_ide_projection) ---
    _print_section("5 / Stage: embed documents (blockless, incremental)")
    documents = pipeline_svc.collect_documents(skip_embedded=True)
    print(f"  New (not yet embedded): {len(documents)} documents")
    stage_doc = pipeline_svc.embed_batch("document", documents, workers=_EMBED_WORKERS)
    print(f"  stored={stage_doc['stored']}  skipped={stage_doc['skipped']}  "
          f"failed={stage_doc['failed']}  elapsed={stage_doc['elapsed_s']}s  "
          f"rate={stage_doc['rate_per_s']}/s")
    if stage_doc["errors"]:
        print(f"  errors: {stage_doc['errors']}")

    # Flush to disk
    print("\n  Flushing embeddings to agentsdb.json ...")
    repo._flush_image()
    emb_count = len(repo._load_collection_object("embedding"))
    print(f"  Total embeddings persisted: {emb_count}")

    # --- Stage 5: retrieval ---
    _print_section("6 / Stage: retrieval smoke test")
    all_embeddings = repo._load_collection_object("embedding")
    retriever = InMemoryCosineRetriever(all_embeddings)
    retrieval_svc = RetrievalTestService(retriever, emb_svc)

    for query in _RETRIEVAL_QUERIES:
        result = retrieval_svc.run_query_object(query, k=_RETRIEVAL_K)
        print(f"\n  Query: \"{result['query']}\"  ({result['elapsed_ms']}ms)")
        for i, hit in enumerate(result["hits"], 1):
            print(f"    {i}. [{hit['owner_type']:10}] score={hit['score']:.4f}  {hit['owner_id'][:70]}")

    # --- Summary ---
    t_total = time.perf_counter() - t_start
    _print_section("Summary")
    total_new = stage_ent["stored"] + stage_rel["stored"] + stage_blk["stored"] + stage_doc["stored"]
    print(f"  entities  new embedded : {stage_ent['stored']} / {stage_ent['total']}")
    print(f"  relations new embedded : {stage_rel['stored']} / {stage_rel['total']}")
    print(f"  blocks    new embedded : {stage_blk['stored']} / {stage_blk['total']}")
    print(f"  documents new embedded : {stage_doc['stored']} / {stage_doc['total']}")
    print(f"  total new this run     : {total_new}")
    print(f"  total in agentsdb      : {emb_count}")
    print(f"  retrieval queries      : {len(_RETRIEVAL_QUERIES)}")
    print(f"  total elapsed          : {t_total:.2f}s")
    print()

    failed_total = stage_ent["failed"] + stage_rel["failed"] + stage_blk["failed"] + stage_doc["failed"]
    if failed_total == 0:
        print("  === PIPELINE TEST PASSED ===")
    else:
        print(f"  === PIPELINE TEST COMPLETED WITH {failed_total} FAILURES ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
