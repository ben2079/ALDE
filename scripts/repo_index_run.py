"""repo_index_run.py

Indexes the entire ALDE Python repository into agentsdb as repo_source documents.
Splits each .py file into semantic blocks (class / function / imports / docstring)
and embeds them via EntityRelationEmbeddingService.

Usage:
    cd /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt
    .venv/bin/python scripts/repo_index_run.py [--scan-root ALDE/alde] [--workers 4] [--dry-run]

After completion, the IDE agent can query:
    AppData/agentsdb.json  →  collections["embeddings"]  (namespace: ns_repo_knowledge)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

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
from repo_code_splitter import RepoIndexService  # type: ignore


_DB_PATH = str(_ROOT / "AppData" / "agentsdb.json")


def _print_section(title: str) -> None:
    print(f"\n{'='*62}\n  {title}\n{'='*62}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index ALDE repo Python sources into agentsdb")
    parser.add_argument("--scan-root", default=str(_ALDE_SRC), help="Root directory to scan")
    parser.add_argument("--workers", type=int, default=4, help="Parallel embed workers")
    parser.add_argument("--dry-run", action="store_true", help="Only count files, do not store")
    args = parser.parse_args()

    scan_root = str(Path(args.scan_root).resolve())

    _print_section("ALDE Repo Knowledge Indexer")
    print(f"  scan root : {scan_root}")
    print(f"  agentsdb  : {_DB_PATH}")
    print(f"  workers   : {args.workers}")

    if args.dry_run:
        from repo_code_splitter import RepoIndexService as _S  # noqa: F401
        svc = _S.__new__(_S)
        svc._splitter = __import__("repo_code_splitter").PythonCodeSplitter()
        svc._DEFAULT_EXCLUDE = _S._DEFAULT_EXCLUDE
        files = svc.scan_object(scan_root)
        print(f"\n  [dry-run] Would index {len(files)} files:")
        for f in files:
            print(f"    {f}")
        return

    # --- Setup services ---
    _print_section("1 / Setup")
    repo = AgentDbInMemoryRepository(_DB_PATH)
    config = RuntimeConfigObject(agents_db_uri="agentsmem://local", namespace_id="ns_repo_knowledge")
    ks = KnowledgeObjectService(repo)  # type: ignore[arg-type]
    emb_svc = EntityRelationEmbeddingService(ks, config)

    print("  Loading embedding model ...")
    t_model = time.perf_counter()
    _ = emb_svc.embed_object("warmup", "warm-up")
    print(f"  Model ready in {time.perf_counter() - t_model:.2f}s  →  {emb_svc._model_name()}")

    index_svc = RepoIndexService(repo, ks, emb_svc, workers=args.workers)

    # --- Index ---
    _print_section("2 / Indexing")
    report = index_svc.index_repo_object(scan_root)

    # --- Report ---
    _print_section("3 / Report")
    print(f"  files found    : {report['files_found']}")
    print(f"  files skipped  : {report['files_skipped']}")
    print(f"  total blocks   : {report['total_blocks']}")
    print(f"  total embedded : {report['total_embedded']}")
    print(f"  elapsed        : {report['elapsed_s']}s")
    print(f"  throughput     : {report['rate_files_per_s']} files/s")

    if report["errors"]:
        print(f"\n  Errors ({len(report['errors'])}):")
        for e in report["errors"]:
            print(f"    {e}")

    # Verify
    emb_count = len(repo._load_collection_object("embedding"))
    doc_count = len(repo._load_collection_object("document"))
    print(f"\n  agentsdb embeddings total : {emb_count}")
    print(f"  agentsdb documents  total : {doc_count}")

    ns_repo = repo.load_object("namespace", "ns_repo_knowledge")
    print(f"  namespace ns_repo_knowledge : {'OK' if ns_repo else 'MISSING'}")

    if report["total_embedded"] > 0:
        print("\n  === REPO INDEX PASSED ===")
    else:
        print("\n  === REPO INDEX FAILED – no embeddings stored ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
