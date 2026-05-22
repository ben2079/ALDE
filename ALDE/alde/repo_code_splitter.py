"""repo_code_splitter.py

Domain: repository knowledge indexing
Object: PythonCodeSplitter, RepoDocumentBuilder, RepoIndexService
Function: split source files into semantic blocks, store as DocumentObject + embeddings

Splits Python source files into semantic blocks using the AST:
- Module-level docstring  → block_kind="module_doc"
- Top-level imports block → block_kind="imports"
- Class definition        → block_kind="class"   (includes all methods)
- Standalone function     → block_kind="function"
- Remainder               → block_kind="module_code"

Each block becomes a DocumentObject block in agentsdb, then gets embedded via
EntityRelationEmbeddingService so the IDE agent can query by cosine similarity.

Namespace: ns_repo_knowledge
Document type: repo_source
Source system: repo_indexer
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

# ---------------------------------------------------------------------------
# Path bootstrap (run from project root OR from ALDE/alde/)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
for _p in (_ROOT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


_REPO_WORKER_JOBS_LOCK = threading.RLock()
_REPO_WORKER_JOBS: dict[str, dict[str, Any]] = {}


class RepoWorkerJobStoreService:
    """Persist repo worker job states so status polling survives process boundaries."""

    _STORAGE_PATH_ENV = "ALDE_REPO_WORKER_JOBS_PATH"

    def __init__(self, *, storage_path: Path | None = None) -> None:
        self._storage_path = Path(storage_path) if storage_path is not None else None
        self._lock = threading.RLock()

    def _resolve_storage_path(self) -> Path:
        configured_path = str(os.getenv(self._STORAGE_PATH_ENV, "") or "").strip()
        if configured_path:
            return Path(os.path.abspath(os.path.expanduser(configured_path)))
        if self._storage_path is not None:
            return Path(self._storage_path)
        return _HERE.parent / "AppData" / "repo_worker_jobs.json"

    def _load_job_records(self) -> dict[str, dict[str, Any]]:
        storage_path = self._resolve_storage_path()
        if not storage_path.is_file():
            return {}
        try:
            payload = json.loads(storage_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}
        jobs_payload = payload.get("jobs") if isinstance(payload, Mapping) else None
        if not isinstance(jobs_payload, Mapping):
            return {}
        return {
            str(job_id): dict(job_payload)
            for job_id, job_payload in jobs_payload.items()
            if isinstance(job_payload, Mapping)
        }

    def _store_job_records(self, job_records: Mapping[str, Mapping[str, Any]]) -> None:
        storage_path = self._resolve_storage_path()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "repo_worker_jobs_v1",
            "updated_at": time.time(),
            "jobs": {
                str(job_id): dict(job_payload)
                for job_id, job_payload in job_records.items()
                if isinstance(job_payload, Mapping)
            },
        }
        temp_path = storage_path.with_suffix(f"{storage_path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, storage_path)

    def load_object_job(self, job_id: str) -> dict[str, Any] | None:
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return None
        with self._lock:
            job_records = self._load_job_records()
            job_payload = job_records.get(normalized_job_id)
            return dict(job_payload) if isinstance(job_payload, Mapping) else None

    def store_object_job(self, job_id: str, job_payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized_job_id = str(job_id or "").strip()
        normalized_job_payload = dict(job_payload or {})
        normalized_job_payload["job_id"] = normalized_job_id
        with self._lock:
            job_records = self._load_job_records()
            job_records[normalized_job_id] = dict(normalized_job_payload)
            self._store_job_records(job_records)
        return dict(normalized_job_payload)


_REPO_WORKER_JOB_STORE = RepoWorkerJobStoreService()


def _load_repo_worker_job(job_id: str) -> dict[str, Any] | None:
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return None
    with _REPO_WORKER_JOBS_LOCK:
        job_state = _REPO_WORKER_JOBS.get(normalized_job_id)
        if isinstance(job_state, Mapping):
            return dict(job_state)
    job_state = _REPO_WORKER_JOB_STORE.load_object_job(normalized_job_id)
    if isinstance(job_state, Mapping):
        with _REPO_WORKER_JOBS_LOCK:
            _REPO_WORKER_JOBS[normalized_job_id] = dict(job_state)
        return dict(job_state)
    return None


def _store_repo_worker_job(job_id: str, job_payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized_job_id = str(job_id or "").strip()
    normalized_job_payload = dict(job_payload or {})
    normalized_job_payload["job_id"] = normalized_job_id
    with _REPO_WORKER_JOBS_LOCK:
        _REPO_WORKER_JOBS[normalized_job_id] = dict(normalized_job_payload)
    _REPO_WORKER_JOB_STORE.store_object_job(normalized_job_id, normalized_job_payload)
    return dict(normalized_job_payload)


def _update_repo_worker_job(job_id: str, **updates: Any) -> dict[str, Any]:
    job_state = _load_repo_worker_job(job_id) or {"job_id": str(job_id or "").strip()}
    job_state.update(updates)
    return _store_repo_worker_job(str(job_id or "").strip(), job_state)

from agents_db import (  # type: ignore
    AgentDbInMemoryRepository,
    BlockObject,
    DocumentObject,
    EntityObject,
    EntityRelationObject,
    EntityRelationEmbeddingService,
    KnowledgeObjectService,
    NamespaceObject,
    ObjectMappingService,
    RuntimeConfigObject,
    load_agentsdb_pipeline_service,
    load_agentsdb_runtime_config_from_env,
)


# ---------------------------------------------------------------------------
# Domain: PythonCodeSplitter
# ---------------------------------------------------------------------------

@dataclass
class CodeBlock:
    """A semantic unit extracted from a Python source file."""
    block_no: int
    block_kind: str          # "module_doc" | "imports" | "class" | "function" | "module_code"
    heading: str             # e.g. "class AgentDbInMemoryRepository" or "def embed_object"
    content: str             # raw source lines
    char_start: int
    char_end: int
    parent_heading: str | None = None   # set for methods inside a class


class PythonCodeSplitter:
    """Splits a Python source file into semantic CodeBlock units via AST."""

    MAX_BLOCK_CHARS = 4000   # guard against huge classes; will be sub-chunked

    def split_object(self, source_path: str) -> list[CodeBlock]:
        """Parse *source_path* and return ordered CodeBlock list."""
        path = Path(source_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        lines = source.splitlines(keepends=True)
        line_offsets = self._build_line_offsets(lines)

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # Fall back to a single whole-file block
            return [CodeBlock(
                block_no=1,
                block_kind="module_code",
                heading=path.name,
                content=source[:self.MAX_BLOCK_CHARS],
                char_start=0,
                char_end=len(source),
            )]

        blocks: list[CodeBlock] = []
        covered_lines: set[int] = set()  # 0-based line indices already claimed

        # --- Module docstring ---
        module_doc = ast.get_docstring(tree)
        if module_doc:
            first_node = tree.body[0] if tree.body else None
            if first_node and isinstance(first_node, ast.Expr):
                end = first_node.end_lineno or first_node.lineno
                for ln in range(first_node.lineno - 1, end):
                    covered_lines.add(ln)
                blocks.append(CodeBlock(
                    block_no=len(blocks) + 1,
                    block_kind="module_doc",
                    heading=f"Module docstring: {path.name}",
                    content=module_doc,
                    char_start=line_offsets[first_node.lineno - 1],
                    char_end=line_offsets[min(end, len(lines) - 1)],
                ))

        # --- Top-level imports ---
        import_lines: list[str] = []
        import_start: int | None = None
        import_end: int = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if not hasattr(node, "lineno"):
                    continue
                start = node.lineno - 1
                end = (node.end_lineno or node.lineno) - 1
                if import_start is None:
                    import_start = start
                import_end = end
                for ln in range(start, end + 1):
                    covered_lines.add(ln)
                    import_lines.extend(lines[ln])
        if import_lines and import_start is not None:
            blocks.append(CodeBlock(
                block_no=len(blocks) + 1,
                block_kind="imports",
                heading=f"Imports: {path.name}",
                content="".join(import_lines).strip(),
                char_start=line_offsets[import_start],
                char_end=line_offsets[min(import_end + 1, len(lines) - 1)],
            ))

        # --- Classes and standalone functions ---
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno - 1
                end = (node.end_lineno or node.lineno) - 1
                for ln in range(start, end + 1):
                    covered_lines.add(ln)
                raw = "".join(lines[start: end + 1])
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                heading = self._heading_for_node(node)

                # Sub-chunk large blocks
                for sub_block, char_s, char_e in self._maybe_subchunk(raw, kind, heading, line_offsets[start]):
                    blocks.append(CodeBlock(
                        block_no=len(blocks) + 1,
                        block_kind=kind,
                        heading=heading,
                        content=sub_block,
                        char_start=char_s,
                        char_end=char_e,
                        parent_heading=None,
                    ))

        # --- Remaining module-level code ---
        leftover_lines = [lines[i] for i in range(len(lines)) if i not in covered_lines]
        leftover = "".join(leftover_lines).strip()
        if leftover:
            blocks.append(CodeBlock(
                block_no=len(blocks) + 1,
                block_kind="module_code",
                heading=f"Module code: {path.name}",
                content=leftover[:self.MAX_BLOCK_CHARS],
                char_start=0,
                char_end=len(source),
            ))

        # Re-number sequentially
        for i, b in enumerate(blocks, 1):
            b.block_no = i

        return blocks

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _heading_for_node(self, node: ast.AST) -> str:
        name = getattr(node, "name", "?")
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases] if node.bases else []
            base_str = f"({', '.join(bases)})" if bases else ""
            return f"class {name}{base_str}"
        return f"def {name}"

    def _build_line_offsets(self, lines: list[str]) -> list[int]:
        offsets: list[int] = []
        pos = 0
        for line in lines:
            offsets.append(pos)
            pos += len(line)
        offsets.append(pos)  # sentinel
        return offsets

    def split_text(self, text: str, filename: str = "<string>") -> list[str]:
        """LangChain-compatible interface: split raw *text* and return content strings.

        Writes *text* to a NamedTemporaryFile, runs the AST splitter, and returns
        the ``content`` of each CodeBlock as a plain string list.  This allows
        ``PythonCodeSplitter`` to be used wherever a LangChain-style
        ``split_text(text) -> list[str]`` splitter is expected.
        """
        import tempfile

        suffix = Path(filename).suffix or ".py"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            blocks = self.split_object(tmp_path)
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
        return [b.content for b in blocks if b.content.strip()]

    def _maybe_subchunk(
        self,
        raw: str,
        kind: str,
        heading: str,
        base_char_start: int,
    ) -> list[tuple[str, int, int]]:
        """Split *raw* into <= MAX_BLOCK_CHARS sub-chunks if needed."""
        if len(raw) <= self.MAX_BLOCK_CHARS:
            return [(raw, base_char_start, base_char_start + len(raw))]
        chunks: list[tuple[str, int, int]] = []
        pos = 0
        while pos < len(raw):
            chunk = raw[pos: pos + self.MAX_BLOCK_CHARS]
            chunks.append((chunk, base_char_start + pos, base_char_start + pos + len(chunk)))
            pos += self.MAX_BLOCK_CHARS
        return chunks


def _build_default_runtime_config() -> RuntimeConfigObject:
    return RuntimeConfigObject(
        agents_db_uri="mongodb://unused",
        database_name="alde_repo_knowledge",
        tenant_id="tenant_default",
        namespace_id=RepoDocumentBuilder._NAMESPACE_ID,
        namespace_slug="repo-knowledge",
        namespace_name=RepoDocumentBuilder._NAMESPACE_NAME,
        default_embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        default_embedding_dimension=384,
        index_backend="faiss",
    )


def _normalize_repo_runtime_config(runtime_config: RuntimeConfigObject | None) -> RuntimeConfigObject:
    base = runtime_config or _build_default_runtime_config()
    return RuntimeConfigObject(
        agents_db_uri=base.agents_db_uri,
        database_name=base.database_name,
        tenant_id=base.tenant_id,
        namespace_id=RepoDocumentBuilder._NAMESPACE_ID,
        namespace_slug="repo-knowledge",
        namespace_name=RepoDocumentBuilder._NAMESPACE_NAME,
        default_embedding_model=base.default_embedding_model,
        default_embedding_dimension=base.default_embedding_dimension,
        index_backend=base.index_backend,
    )


class RepoModuleParser:
    """Build parser-compatible payloads for Python modules.

    The resulting payload mirrors the job parser schema already consumed by
    ObjectMappingService so repo knowledge flows through the same AgentsDB
    document/entity/relation pipeline as other parsed artifacts.
    """

    def __init__(self, splitter: PythonCodeSplitter | None = None) -> None:
        self._splitter = splitter or PythonCodeSplitter()

    def parse_object(self, source_path: str, *, repo_root: str) -> dict[str, Any]:
        path = Path(source_path)
        source = path.read_text(encoding="utf-8", errors="replace")
        blocks = self._splitter.split_object(source_path)
        rel_path = str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else path.name
        content_sha = hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()
        correlation_id = f"repo:{content_sha[:16]}"
        title = rel_path

        section_payloads = [
            {
                "section_key": f"block_{block.block_no}",
                "heading": block.heading,
                "text": block.content,
                "block_kind": block.block_kind,
                "metadata": {
                    "source_path": rel_path,
                    "kind": block.block_kind,
                    "parent": block.parent_heading,
                    "block_no": block.block_no,
                    "char_start": block.char_start,
                    "char_end": block.char_end,
                },
            }
            for block in blocks
            if block.content.strip()
        ]

        entity_objects, relation_objects = self._build_entity_relation_payloads(
            source=source,
            rel_path=rel_path,
        )

        return {
            "agent": "repo_module_parser",
            "source": "repo_module_parser",
            "source_path": str(path),
            "title": title,
            "record_kind": "document",
            "kind": "document",
            "object_name": "documents",
            "job_name": "repo_module_parser",
            "correlation_id": correlation_id,
            "content_sha256": content_sha,
            "status": "processed",
            "processing_state": "processed",
            "processed": True,
            "failed_reason": None,
            "file": {
                "path": str(path),
                "name": path.name,
                "source_path": str(path),
                "source_uri": f"file://{path}",
                "content_sha256": content_sha,
                "mime_type": "text/x-python",
            },
            "parse": {
                "is_repo_module": True,
                "language": "python",
                "extraction_quality": "high",
                "errors": [],
                "warnings": [],
            },
            "document": {
                "title": title,
                "summary": f"Python source module {rel_path}",
                "raw_text": source,
                "metadata": {
                    "source_path": rel_path,
                    "module_name": path.stem,
                    "content_sha256": content_sha,
                    "block_count": len(section_payloads),
                    "parser": "repo_code_splitter",
                },
            },
            "raw_text_document": {
                "title": title,
                "language": "python",
                "raw_text": source,
                "sections": section_payloads,
                "metadata": {
                    "source": rel_path,
                    "parser": "repo_code_splitter",
                },
            },
            "entity_objects": entity_objects,
            "relation_objects": relation_objects,
            "db_updates": {
                "correlation_id": correlation_id,
                "content_sha256": content_sha,
                "processing_state": "processed",
                "processed": True,
            },
        }

    def _build_entity_relation_payloads(self, *, source: str, rel_path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        module_name = Path(rel_path).stem
        entity_objects: list[dict[str, Any]] = [
            {
                "entity_key": "subject",
                "entity_type": "module",
                "canonical_name": module_name,
                "mention_text": module_name,
                "section_key": "block_1",
                "summary": f"Python module {rel_path}",
                "metadata": {"role": "subject", "source_field": "document.title", "source_path": rel_path},
            }
        ]
        relation_objects: list[dict[str, Any]] = []

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return entity_objects, relation_objects

        seen_entity_keys = {"subject"}

        def add_entity(*, entity_key: str, entity_type: str, canonical_name: str, section_key: str, relation_type: str | None, summary: str, source_field: str) -> None:
            if entity_key not in seen_entity_keys:
                entity_payload: dict[str, Any] = {
                    "entity_key": entity_key,
                    "entity_type": entity_type,
                    "canonical_name": canonical_name,
                    "mention_text": canonical_name,
                    "section_key": section_key,
                    "summary": summary,
                    "metadata": {"source_field": source_field, "source_path": rel_path},
                }
                if relation_type:
                    entity_payload["is_target"] = True
                    entity_payload["source_entity"] = "subject"
                    entity_payload["is_relational"] = relation_type
                    entity_payload["explicit_description"] = f"{module_name} {relation_type.replace('_', ' ')} {canonical_name}."
                entity_objects.append(
                    entity_payload
                )
                seen_entity_keys.add(entity_key)

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                add_entity(
                    entity_key=f"class:{node.name}",
                    entity_type="class",
                    canonical_name=node.name,
                    section_key=f"block_{len(entity_objects) + 1}",
                    relation_type="defines_class",
                    summary=f"Class defined in {rel_path}",
                    source_field="ast.ClassDef.name",
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_entity(
                    entity_key=f"function:{node.name}",
                    entity_type="function",
                    canonical_name=node.name,
                    section_key=f"block_{len(entity_objects) + 1}",
                    relation_type="defines_function",
                    summary=f"Function defined in {rel_path}",
                    source_field="ast.FunctionDef.name",
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    import_name = str(alias.name or "").strip()
                    if not import_name:
                        continue
                    add_entity(
                        entity_key=f"dependency:{import_name}",
                        entity_type="dependency",
                        canonical_name=import_name,
                        section_key="block_1",
                        relation_type="imports_module",
                        summary=f"Imported dependency used by {rel_path}",
                        source_field="ast.Import.name",
                    )
            elif isinstance(node, ast.ImportFrom):
                import_name = str(node.module or "").strip()
                if not import_name:
                    continue
                add_entity(
                    entity_key=f"dependency:{import_name}",
                    entity_type="dependency",
                    canonical_name=import_name,
                    section_key="block_1",
                    relation_type="imports_module",
                    summary=f"Imported dependency used by {rel_path}",
                    source_field="ast.ImportFrom.module",
                )

        return entity_objects, relation_objects


# ---------------------------------------------------------------------------
# Domain: RepoDocumentBuilder
# ---------------------------------------------------------------------------

class RepoDocumentBuilder:
    """Converts a source file + its CodeBlocks into a DocumentObject."""

    _NAMESPACE_ID = "ns_repo_knowledge"
    _NAMESPACE_NAME = "ALDE Repository Knowledge"
    _TENANT_ID = "tenant_default"
    _SOURCE_SYSTEM = "repo_indexer"
    _DOCUMENT_TYPE = "repo_source"

    def build_object(self, source_path: str, code_blocks: list[CodeBlock], *, repo_root: str) -> DocumentObject:
        path = Path(source_path)
        rel_path = str(path.relative_to(repo_root)) if path.is_relative_to(repo_root) else path.name
        source = path.read_text(encoding="utf-8", errors="replace")
        content_sha = hashlib.sha256(source.encode()).hexdigest()
        doc_id = f"doc:repo_source:{content_sha[:16]}"

        block_objects = [
            BlockObject(
                block_id=f"blk:repo:{content_sha[:12]}:{b.block_no}",
                block_no=b.block_no,
                content=b.content,
                block_kind=b.block_kind,
                heading=b.heading,
                token_count=len(b.content.split()),
                char_start=b.char_start,
                char_end=b.char_end,
                metadata={
                    "source_path": rel_path,
                    "kind": b.block_kind,
                    "parent": b.parent_heading,
                },
            )
            for b in code_blocks
        ]

        return DocumentObject(
            id=doc_id,
            tenant_id=self._TENANT_ID,
            namespace_id=self._NAMESPACE_ID,
            document_type=self._DOCUMENT_TYPE,
            title=rel_path,
            source_uri=f"file://{path}",
            content_sha256=content_sha,
            source_system=self._SOURCE_SYSTEM,
            mime_type="text/x-python",
            language_code="py",
            summary=f"Python source: {rel_path} ({len(code_blocks)} blocks)",
            metadata={"rel_path": rel_path, "block_count": len(code_blocks)},
            blocks=block_objects,
        )

    def build_namespace_object(self) -> NamespaceObject:
        return NamespaceObject(
            id=self._NAMESPACE_ID,
            name=self._NAMESPACE_NAME,
            slug="repo-knowledge",
            tenant_id=self._TENANT_ID,
            default_embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            default_embedding_dimension=384,
            description="Indexed Python source code from the ALDE repository.",
        )


# ---------------------------------------------------------------------------
# Domain: RepoIndexService
# ---------------------------------------------------------------------------

class RepoIndexService:
    """Orchestrates scan → split → store → embed for an entire repo tree."""

    _DEFAULT_EXCLUDE = {
        "__pycache__", ".venv", "venv", ".git", ".micromamba",
        ".venv-1", "node_modules", "alembic",
    }

    def __init__(
        self,
        repo: Any,
        knowledge_svc: KnowledgeObjectService,
        emb_svc: EntityRelationEmbeddingService,
        *,
        workers: int = 4,
        runtime_config: RuntimeConfigObject | None = None,
    ) -> None:
        self._repo = repo
        self._ks = knowledge_svc
        self._emb_svc = emb_svc
        self._workers = workers
        self._splitter = PythonCodeSplitter()
        self._builder = RepoDocumentBuilder()
        self._runtime_config = runtime_config or _build_default_runtime_config()
        self._module_parser = RepoModuleParser(self._splitter)
        self._mapping_service = ObjectMappingService(self._ks, self._runtime_config)

    def scan_object(self, scan_root: str, *, extensions: tuple[str, ...] = (".py",)) -> list[str]:
        """Return sorted list of source file paths under *scan_root*."""
        result: list[str] = []
        exclude = self._DEFAULT_EXCLUDE
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = [d for d in dirnames if d not in exclude]
            for fname in filenames:
                if any(fname.endswith(ext) for ext in extensions):
                    result.append(os.path.join(dirpath, fname))
        return sorted(result)

    def index_object(self, source_path: str, *, repo_root: str) -> dict[str, Any]:
        """Split, map, store, and embed a single source file. Returns per-file report."""
        payload = self._module_parser.parse_object(source_path, repo_root=repo_root)
        document_payload = payload.get("document") if isinstance(payload.get("document"), Mapping) else {}
        if not document_payload:
            return {"path": source_path, "blocks": 0, "embedded": 0, "skipped": True}

        correlation_id = str(payload.get("correlation_id") or "").strip()
        namespace_object = self._mapping_service.load_namespace_object(
            handoff_metadata={
                "tenant_id": RepoDocumentBuilder._TENANT_ID,
                "knowledge_namespace_id": RepoDocumentBuilder._NAMESPACE_ID,
                "knowledge_namespace_slug": "repo-knowledge",
                "knowledge_namespace_name": RepoDocumentBuilder._NAMESPACE_NAME,
            }
        )
        document_object = self._mapping_service.build_document_object(
            object_name="documents",
            result_payload=payload,
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            handoff_payload={"source_path": source_path, "platform": "repo_indexer"},
        )
        if document_object is None:
            return {"path": source_path, "blocks": 0, "embedded": 0, "skipped": True}

        object_payload = self._mapping_service.load_object_payload(object_name="documents", result_payload=payload)
        block_seed_objects = self._mapping_service.build_block_seed_objects(
            object_name="documents",
            object_payload=object_payload,
            correlation_id=correlation_id,
            result_payload=payload,
        )
        entity_candidate_objects = self._mapping_service.build_entity_candidate_objects(
            object_name="documents",
            object_payload=object_payload,
            correlation_id=correlation_id,
            result_payload=payload,
        )
        entity_objects = self._mapping_service.build_entity_objects(
            object_name="documents",
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            document_id=document_object.id,
            entity_candidate_objects=entity_candidate_objects,
            timestamp=document_object.created_at,
        )
        relation_objects = self._mapping_service.build_relation_objects(
            object_name="documents",
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            entity_candidate_objects=entity_candidate_objects,
            entity_objects=entity_objects,
            block_seed_objects=block_seed_objects,
            timestamp=document_object.created_at,
            result_payload=payload,
        )

        self._ks.store_namespace_object(namespace_object)
        self._ks.store_document_object(document_object)
        for entity_object in entity_objects:
            self._ks.store_entity_object(entity_object)
        for relation_object in relation_objects:
            self._ks.store_relation_object(relation_object)

        embedded = 0
        for blk in document_object.blocks:
            blk_dict = {
                "block_id": blk.block_id,
                "heading": blk.heading,
                "content": blk.content,
                "block_kind": blk.block_kind,
                "metadata": blk.metadata,
            }
            result = self._emb_svc.process_object("block", blk_dict, owner_id=blk.block_id)
            if result.get("stored"):
                embedded += 1

        for entity_object in entity_objects:
            result = self._emb_svc.process_object(
                "entity",
                {
                    "entity_id": entity_object.id,
                    "entity_type": entity_object.entity_type,
                    "canonical_name": entity_object.canonical_name,
                    "summary": entity_object.summary,
                    "attributes": entity_object.attributes,
                },
                owner_id=entity_object.id,
            )
            if result.get("stored"):
                embedded += 1

        for relation_object in relation_objects:
            result = self._emb_svc.process_object(
                "relation",
                {
                    "relation_id": relation_object.id,
                    "source_entity_id": relation_object.source_entity_id,
                    "target_entity_id": relation_object.target_entity_id,
                    "relation_type": relation_object.relation_type,
                    "metadata": relation_object.metadata,
                },
                owner_id=relation_object.id,
            )
            if result.get("stored"):
                embedded += 1

        return {
            "path": source_path,
            "doc_id": document_object.id,
            "blocks": len(document_object.blocks),
            "entities": len(entity_objects),
            "relations": len(relation_objects),
            "embedded": embedded,
            "skipped": False,
        }

    def index_repo_object(self, scan_root: str, *, extensions: Sequence[str] = (".py",)) -> dict[str, Any]:
        """Full repo index run: scan → split → store → embed. Returns summary report."""
        # Ensure namespace exists
        ns = self._builder.build_namespace_object()
        self._ks.store_namespace_object(ns)

        files = self.scan_object(scan_root, extensions=tuple(extensions))
        t0 = time.perf_counter()

        total_blocks = 0
        total_entities = 0
        total_relations = 0
        total_embedded = 0
        total_skipped = 0
        errors: list[str] = []

        def _index_one(fpath: str) -> dict[str, Any]:
            try:
                return self.index_object(fpath, repo_root=scan_root)
            except Exception as exc:
                return {"path": fpath, "blocks": 0, "embedded": 0, "skipped": True, "error": str(exc)}

        repo_backend = getattr(self._ks, "_repository", self._repo)
        flush_context = getattr(repo_backend, "deferred_flush", None)
        if not callable(flush_context):
            flush_context = getattr(repo_backend, "deferred_write_queue", None)
        if callable(flush_context):
            with flush_context():
                with ThreadPoolExecutor(max_workers=self._workers) as executor:
                    futures = {executor.submit(_index_one, fp): fp for fp in files}
                    for fut in as_completed(futures):
                        r = fut.result()
                        total_blocks += r.get("blocks", 0)
                        total_entities += r.get("entities", 0)
                        total_relations += r.get("relations", 0)
                        total_embedded += r.get("embedded", 0)
                        if r.get("skipped"):
                            total_skipped += 1
                        if r.get("error"):
                            errors.append(f"{r['path']}: {r['error']}")
        else:
            with ThreadPoolExecutor(max_workers=self._workers) as executor:
                futures = {executor.submit(_index_one, fp): fp for fp in files}
                for fut in as_completed(futures):
                    r = fut.result()
                    total_blocks += r.get("blocks", 0)
                    total_entities += r.get("entities", 0)
                    total_relations += r.get("relations", 0)
                    total_embedded += r.get("embedded", 0)
                    if r.get("skipped"):
                        total_skipped += 1
                    if r.get("error"):
                        errors.append(f"{r['path']}: {r['error']}")

        elapsed = time.perf_counter() - t0
        if hasattr(repo_backend, "_flush_image"):
            repo_backend._flush_image()

        return {
            "scan_root": scan_root,
            "files_found": len(files),
            "files_skipped": total_skipped,
            "total_blocks": total_blocks,
            "total_entities": total_entities,
            "total_relations": total_relations,
            "total_embedded": total_embedded,
            "elapsed_s": round(elapsed, 2),
            "rate_files_per_s": round((len(files) - total_skipped) / elapsed, 2) if elapsed > 0 else 0,
            "errors": errors[:10],
        }

    def delete_repo_knowledge_objects(
        self,
        *,
        object_names: Sequence[str],
        namespace_ids: Sequence[str],
        owner_prefixes: Sequence[str],
        async_delete: bool = True,
        limit: int = 50_000,
    ) -> dict[str, Any]:
        """Delete repo-knowledge artifacts via repository.delete_object().

        The method is intentionally conservative: for embeddings it requires an
        owner prefix match (e.g. ``blk:repo:``) to avoid deleting unrelated data.
        """
        repository = getattr(self._ks, "_repository", self._repo)
        delete_object = getattr(repository, "delete_object", None)
        load_objects = getattr(repository, "load_objects", None)
        if not callable(delete_object) or not callable(load_objects):
            return {
                "ok": False,
                "error": "repository_missing_load_or_delete",
            }

        normalized_names = [str(name or "").strip().lower() for name in object_names if str(name or "").strip()]
        supported_names = [name for name in normalized_names if name in {"embedding", "relation", "entity", "document"}]
        if not supported_names:
            return {
                "ok": False,
                "error": "no_supported_object_names",
                "supported": ["embedding", "relation", "entity", "document"],
            }

        normalized_namespaces = [str(namespace_id or "").strip() for namespace_id in namespace_ids if str(namespace_id or "").strip()]
        if not normalized_namespaces:
            normalized_namespaces = [RepoDocumentBuilder._NAMESPACE_ID]

        normalized_prefixes = [str(prefix or "").strip() for prefix in owner_prefixes if str(prefix or "").strip()]
        if not normalized_prefixes:
            normalized_prefixes = ["blk:repo:"]

        def _resolve_record_id(payload: Mapping[str, Any]) -> str:
            return str(payload.get("_id") or payload.get("id") or "").strip()

        def _is_repo_match(object_name: str, payload: Mapping[str, Any]) -> bool:
            if object_name == "embedding":
                owner_id = str(payload.get("owner_id") or "").strip()
                return any(owner_id.startswith(prefix) for prefix in normalized_prefixes)

            if str(payload.get("source_system") or "").strip() == RepoDocumentBuilder._SOURCE_SYSTEM:
                return True
            if str(payload.get("document_type") or "").strip() == RepoDocumentBuilder._DOCUMENT_TYPE:
                return True

            record_id = _resolve_record_id(payload)
            if object_name == "document":
                return record_id.startswith("doc:repo_source:")
            if object_name == "entity":
                return record_id.startswith("ent:documents:")
            if object_name == "relation":
                return record_id.startswith("rel:documents:")
            return False

        delete_targets: list[tuple[str, str]] = []
        per_object_totals: dict[str, int] = {name: 0 for name in supported_names}
        per_object_matches: dict[str, int] = {name: 0 for name in supported_names}
        errors: list[str] = []

        for object_name in supported_names:
            for namespace_id in normalized_namespaces:
                object_payload_list = load_objects(object_name, object_filter={"namespace_id": namespace_id}, limit=max(1, int(limit)))
                per_object_totals[object_name] += len(object_payload_list)
                for object_payload in object_payload_list:
                    if not isinstance(object_payload, Mapping):
                        continue
                    if not _is_repo_match(object_name, object_payload):
                        continue
                    object_id = _resolve_record_id(object_payload)
                    if not object_id:
                        continue
                    per_object_matches[object_name] += 1
                    delete_targets.append((object_name, object_id))

        deleted_count = 0

        def _delete_one(target: tuple[str, str]) -> bool:
            object_name, object_id = target
            try:
                return bool(delete_object(object_name, object_id))
            except Exception as exc:  # pragma: no cover - defensive guard
                errors.append(f"{object_name}:{object_id}:{type(exc).__name__}:{exc}")
                return False

        unique_targets = list(dict.fromkeys(delete_targets))
        repo_backend = getattr(self._ks, "_repository", self._repo)
        flush_context = getattr(repo_backend, "deferred_flush", None)
        if not callable(flush_context):
            flush_context = getattr(repo_backend, "deferred_write_queue", None)
        if callable(flush_context):
            with flush_context():
                if async_delete and unique_targets:
                    worker_count = max(1, min(self._workers, 8))
                    with ThreadPoolExecutor(max_workers=worker_count) as executor:
                        futures = [executor.submit(_delete_one, target) for target in unique_targets]
                        for future in as_completed(futures):
                            if future.result():
                                deleted_count += 1
                else:
                    for target in unique_targets:
                        if _delete_one(target):
                            deleted_count += 1
        else:
            if async_delete and unique_targets:
                worker_count = max(1, min(self._workers, 8))
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [executor.submit(_delete_one, target) for target in unique_targets]
                    for future in as_completed(futures):
                        if future.result():
                            deleted_count += 1
            else:
                for target in unique_targets:
                    if _delete_one(target):
                        deleted_count += 1

        if hasattr(repo_backend, "_flush_image"):
            repo_backend._flush_image()

        return {
            "ok": True,
            "deleted": deleted_count,
            "candidates": len(unique_targets),
            "namespace_ids": normalized_namespaces,
            "object_names": supported_names,
            "owner_prefixes": normalized_prefixes,
            "per_object_totals": per_object_totals,
            "per_object_matches": per_object_matches,
            "errors": errors[:20],
            "async_delete": bool(async_delete),
        }


def create_repo_index_service(
    *,
    image_path: str | None = None,
    workers: int = 4,
) -> RepoIndexService:
    resolved_runtime_config = load_agentsdb_runtime_config_from_env()
    runtime_config = _normalize_repo_runtime_config(resolved_runtime_config)
    if resolved_runtime_config is not None:
        pipeline_service = load_agentsdb_pipeline_service(runtime_config)
        knowledge_service = pipeline_service._knowledge_service
        repository = knowledge_service._repository
    else:
        repository = AgentDbInMemoryRepository(image_path=image_path)
        repository.ensure_index_objects()
        knowledge_service = KnowledgeObjectService(repository)
    embedding_service = EntityRelationEmbeddingService(knowledge_service, runtime_config)
    return RepoIndexService(
        repository,
        knowledge_service,
        embedding_service,
        workers=workers,
        runtime_config=runtime_config,
    )


def run_repo_knowledge_operation(
    *,
    operation: str,
    root_dir: str | None,
    image_path: str | None,
    workers: int,
    extensions: list[str] | str | None,
    cleanup_before_build: bool,
    cleanup_namespace_ids: list[str] | str | None,
    cleanup_object_names: list[str] | str | None,
    cleanup_owner_prefixes: list[str] | str | None,
    delete_async: bool,
) -> dict[str, Any]:
    resolved_root = Path(os.path.abspath(os.path.expanduser(root_dir or Path(__file__).resolve().parents[1])))
    resolved_extensions = extensions
    if isinstance(resolved_extensions, str):
        resolved_extensions = [resolved_extensions]
    normalized_extensions = tuple(
        item if str(item).startswith(".") else f".{item}"
        for item in (resolved_extensions or [".py"])
        if str(item).strip()
    )

    service = create_repo_index_service(image_path=image_path, workers=max(1, int(workers)))
    normalized_operation = str(operation or "build").strip().lower()
    resolved_cleanup_namespaces = cleanup_namespace_ids
    if isinstance(resolved_cleanup_namespaces, str):
        resolved_cleanup_namespaces = [resolved_cleanup_namespaces]
    normalized_cleanup_namespaces = [
        str(namespace_id or "").strip()
        for namespace_id in (resolved_cleanup_namespaces or ["ns_alde_default", RepoDocumentBuilder._NAMESPACE_ID])
        if str(namespace_id or "").strip()
    ]

    resolved_cleanup_object_names = cleanup_object_names
    if isinstance(resolved_cleanup_object_names, str):
        resolved_cleanup_object_names = [resolved_cleanup_object_names]
    normalized_cleanup_object_names = [
        str(object_name or "").strip().lower()
        for object_name in (resolved_cleanup_object_names or ["embedding", "relation", "entity", "document"])
        if str(object_name or "").strip()
    ]

    resolved_cleanup_prefixes = cleanup_owner_prefixes
    if isinstance(resolved_cleanup_prefixes, str):
        resolved_cleanup_prefixes = [resolved_cleanup_prefixes]
    normalized_cleanup_prefixes = [
        str(prefix or "").strip()
        for prefix in (resolved_cleanup_prefixes or ["blk:repo:"])
        if str(prefix or "").strip()
    ]

    if normalized_operation == "repair_namespace":
        normalized_operation = "rebuild"
        normalized_cleanup_namespaces = ["ns_alde_default", RepoDocumentBuilder._NAMESPACE_ID]
        normalized_cleanup_object_names = ["embedding", "relation", "entity", "document"]
        normalized_cleanup_prefixes = ["blk:repo:"]

    if normalized_operation == "scan":
        return {
            "ok": True,
            "operation": "scan",
            "root_dir": str(resolved_root),
            "extensions": list(normalized_extensions),
            "files": service.scan_object(str(resolved_root), extensions=normalized_extensions),
        }
    if normalized_operation == "build":
        cleanup_report = None
        if cleanup_before_build:
            cleanup_report = service.delete_repo_knowledge_objects(
                object_names=normalized_cleanup_object_names,
                namespace_ids=normalized_cleanup_namespaces,
                owner_prefixes=normalized_cleanup_prefixes,
                async_delete=bool(delete_async),
            )
        result = service.index_repo_object(str(resolved_root), extensions=normalized_extensions)
        result["ok"] = True
        result["operation"] = "build"
        if cleanup_report is not None:
            result["cleanup"] = cleanup_report
        return result
    if normalized_operation in {"cleanup", "delete"}:
        cleanup_report = service.delete_repo_knowledge_objects(
            object_names=normalized_cleanup_object_names,
            namespace_ids=normalized_cleanup_namespaces,
            owner_prefixes=normalized_cleanup_prefixes,
            async_delete=bool(delete_async),
        )
        cleanup_report["operation"] = "cleanup"
        return cleanup_report
    if normalized_operation == "rebuild":
        cleanup_report = service.delete_repo_knowledge_objects(
            object_names=normalized_cleanup_object_names,
            namespace_ids=normalized_cleanup_namespaces,
            owner_prefixes=normalized_cleanup_prefixes,
            async_delete=bool(delete_async),
        )
        result = service.index_repo_object(str(resolved_root), extensions=normalized_extensions)
        result["ok"] = True
        result["operation"] = "rebuild"
        result["cleanup"] = cleanup_report
        return result

    return {
        "ok": False,
        "error": f"Unsupported operation: {operation}",
        "allowed_operations": ["scan", "build", "cleanup", "delete", "rebuild", "status", "repair_namespace"],
    }


def run_repo_worker_job(job_id: str, job_payload: Mapping[str, Any]) -> None:
    _update_repo_worker_job(job_id, status="running", started_at=time.time(), error=None)
    try:
        result = run_repo_knowledge_operation(**dict(job_payload))
        _update_repo_worker_job(
            job_id,
            status="completed",
            finished_at=time.time(),
            result=dict(result),
            error=None,
        )
    except BaseException as exc:  # pragma: no cover - defensive guard
        _update_repo_worker_job(
            job_id,
            status="failed",
            finished_at=time.time(),
            error=f"{type(exc).__name__}: {exc}",
        )

def adb_worker(
    operation: str,
    root_dir: str | None = None,
    image_path: str | None = None,
    workers: int = 4,
    extensions: list[str] | str | None = None,
    cleanup_before_build: bool = False,
    cleanup_namespace_ids: list[str] | str | None = None,
    cleanup_object_names: list[str] | str | None = None,
    cleanup_owner_prefixes: list[str] | str | None = None,
    delete_async: bool = True,
    run_async: bool = False,
    job_id: str | None = None,
) -> dict | str:
    """Scan or build repository knowledge using the AgentsDB parser schema."""
    try:
        normalized_operation = str(operation or "build").strip().lower()
        if normalized_operation == "status":
            normalized_job_id = str(job_id or "").strip()
            if not normalized_job_id:
                return {"ok": False, "error": "status operation requires job_id"}
            job_state = _load_repo_worker_job(normalized_job_id)
            if not isinstance(job_state, dict):
                return {"ok": False, "error": f"unknown job_id: {normalized_job_id}"}
            return {"ok": True, "operation": "status", "job": dict(job_state)}

        operation_payload = {
            "operation": normalized_operation,
            "root_dir": root_dir,
            "image_path": image_path,
            "workers": workers,
            "extensions": extensions,
            "cleanup_before_build": cleanup_before_build,
            "cleanup_namespace_ids": cleanup_namespace_ids,
            "cleanup_object_names": cleanup_object_names,
            "cleanup_owner_prefixes": cleanup_owner_prefixes,
            "delete_async": delete_async,
        }

        if bool(run_async) and normalized_operation in {"build", "cleanup", "delete", "rebuild", "repair_namespace"}:
            normalized_job_id = str(job_id or f"repo-job-{uuid4().hex[:12]}").strip()
            job_state = {
                "job_id": normalized_job_id,
                "operation": normalized_operation,
                "status": "queued",
                "created_at": time.time(),
                "params": {
                    "root_dir": root_dir,
                    "image_path": image_path,
                    "workers": workers,
                    "extensions": extensions,
                    "cleanup_before_build": cleanup_before_build,
                    "cleanup_namespace_ids": cleanup_namespace_ids,
                    "cleanup_object_names": cleanup_object_names,
                    "cleanup_owner_prefixes": cleanup_owner_prefixes,
                    "delete_async": delete_async,
                },
            }
            _store_repo_worker_job(normalized_job_id, job_state)

            worker_thread = threading.Thread(
                target= run_repo_worker_job,
                args=(normalized_job_id, operation_payload),
                name=f"repo-knowledge-worker:{normalized_job_id}",
                daemon=True,
            )
            worker_thread.start()
            return {
                "ok": True,
                "operation": normalized_operation,
                "async": True,
                "job_id": normalized_job_id,
                "status": "queued",
                "status_call": {
                    "operation": "status",
                    "job_id": normalized_job_id,
                },
            }

        result = run_repo_knowledge_operation(**operation_payload)
        result["async"] = False
        return result
    except BaseException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# repo_knowledge_query – retrieve repo knowledge as IDE-Agent context
# ---------------------------------------------------------------------------

_REPO_KNOWLEDGE_NS = "ns_repo_knowledge"

_OWNER_TYPE_ALL = ("block", "entity", "relation")


def _format_repo_knowledge_chunks(candidates: list[dict], owner_type: str) -> list[dict]:
    """Normalise raw agentsdb hits into lightweight context chunks."""
    chunks: list[dict] = []
    for item in candidates:
        payload = item if not isinstance(item.get("payload"), dict) else item["payload"]
        chunk: dict = {"owner_type": owner_type}

        # --- block ---
        if owner_type == "block":
            heading = payload.get("heading") or payload.get("block_kind") or "block"
            content = payload.get("content") or payload.get("text") or ""
            meta = payload.get("metadata") or {}
            chunk["heading"] = heading
            chunk["content"] = content[:2000]
            chunk["source_path"] = meta.get("source_path") or payload.get("source_path", "")
            chunk["block_kind"] = payload.get("block_kind") or meta.get("kind", "")
            chunk["score"] = item.get("score")

        # --- entity ---
        elif owner_type == "entity":
            chunk["canonical_name"] = payload.get("canonical_name") or payload.get("mention_text", "")
            chunk["entity_type"] = payload.get("entity_type", "")
            chunk["summary"] = (payload.get("summary") or "")[:500]
            meta = payload.get("metadata") or {}
            chunk["source_path"] = meta.get("source_path") or payload.get("source_path", "")
            chunk["score"] = item.get("score")

        # --- relation ---
        elif owner_type == "relation":
            chunk["relation_type"] = payload.get("relation_type", "")
            chunk["source_entity_id"] = payload.get("source_entity_id", "")
            chunk["target_entity_id"] = payload.get("target_entity_id", "")
            meta = payload.get("metadata") or {}
            chunk["relation_description"] = payload.get("relation_description") or meta.get("relation_description") or ""
            chunk["source_path"] = meta.get("source_path") or payload.get("source_path", "")
            chunk["score"] = item.get("score")

        else:
            chunk["raw"] = payload

        chunks.append(chunk)
    return chunks


def adb_query(
    query: str,
    owner_types: list[str] | str | None = None,
    limit: int = 10,
    namespace_id: str | None = None,
    image_path: str | None = None,
    use_vector: bool = True,
) -> dict:
    """Query indexed repository knowledge and return context chunks for the IDE Agent.

    Parameters
    ----------
    query        : Natural-language search query.
    owner_types  : One or more of "block", "entity", "relation", or "all".
                   Default: ["block", "entity"].
    limit        : Max results per owner_type. Default: 10.
    namespace_id : AgentsDB namespace to query. Default: ns_repo_knowledge.
    image_path   : Snapshot path for in-memory fallback backend.
    use_vector   : Attempt dense-vector (embedding) search before text fallback.
                   Default: True.
    """
    try:
        try:
            from .repo_code_splitter import create_repo_index_service  # type: ignore
        except ImportError:
            from alde.repo_code_splitter import create_repo_index_service  # type: ignore

        try:
            from .agents_db import (  # type: ignore
                EntityRelationEmbeddingService,
                load_agentsdb_runtime_config_from_env,
                load_agentsdb_pipeline_service,
                sync_retrieval_run_to_agentsdb_knowledge,
            )
        except ImportError:
            from alde.agents_db import (  # type: ignore
                EntityRelationEmbeddingService,
                load_agentsdb_runtime_config_from_env,
                load_agentsdb_pipeline_service,
                sync_retrieval_run_to_agentsdb_knowledge,
            )

        # --- resolve owner_types ---
        if owner_types is None:
            resolved_owner_types: list[str] = ["block", "entity"]
        elif isinstance(owner_types, str):
            resolved_owner_types = list(_OWNER_TYPE_ALL) if owner_types.strip().lower() == "all" else [owner_types.strip()]
        else:
            flat = []
            for ot in owner_types:
                if str(ot).strip().lower() == "all":
                    flat.extend(_OWNER_TYPE_ALL)
                else:
                    flat.append(str(ot).strip())
            resolved_owner_types = list(dict.fromkeys(flat))  # deduplicate, preserve order

        ns = str(namespace_id or _REPO_KNOWLEDGE_NS).strip()
        safe_limit = max(1, min(int(limit), 50))

        # --- load services ---
        runtime_config = load_agentsdb_runtime_config_from_env()
        if runtime_config is None:
            # fallback: build service from in-memory snapshot (read-only query)
            service = create_repo_index_service(image_path=image_path)
            knowledge_service = service._ks
            runtime_config = service._runtime_config
        else:
            pipeline_service = load_agentsdb_pipeline_service(runtime_config)
            knowledge_service = pipeline_service._knowledge_service

        # --- embed query text (best-effort) ---
        query_vector: list[float] | None = None
        if use_vector:
            try:
                emb_svc = EntityRelationEmbeddingService(knowledge_service, runtime_config)
                query_vector = emb_svc.embed_object("query", query)
            except Exception:
                query_vector = None

        # --- run retrieval per owner_type ---
        all_chunks: list[dict] = []
        used_vector = False
        for ot in resolved_owner_types:
            candidates: list[dict] = []

            if query_vector is not None:
                try:
                    candidates = knowledge_service.build_vector_candidate_pipeline(
                        query_vector=query_vector,
                        namespace_id=ns,
                        owner_type=ot,
                        limit=safe_limit,
                    )
                    used_vector = True
                except Exception:
                    candidates = []

            if not candidates:
                # text search fallback
                candidates = knowledge_service.find_objects(
                    namespace_id=ns,
                    query_text=query,
                    limit=safe_limit,
                )

            all_chunks.extend(_format_repo_knowledge_chunks(candidates, ot))

        result: dict = {
            "ok": True,
            "query": query,
            "namespace_id": ns,
            "owner_types": resolved_owner_types,
            "used_vector_search": used_vector,
            "total": len(all_chunks),
            "chunks": all_chunks,
        }

        # --- log retrieval run (best-effort) ---
        try:
            sync_retrieval_run_to_agentsdb_knowledge(
                tool_name="repo_knowledge_query",
                query_event={"query": query, "namespace_id": ns, "owner_types": resolved_owner_types, "limit": safe_limit},
                outcome_event={"total": len(all_chunks), "used_vector_search": used_vector},
                retrieval_result=result,
            )
        except Exception:
            pass

        return result

    except BaseException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# load_repo_context_for_ide_agent – format query results for ChatWindow
# ---------------------------------------------------------------------------

def load_repo_context_for_ide_agent(
    query: str,
    *,
    limit: int = 5,
    owner_types: list[str] | str | None = None,
    namespace_id: str | None = None,
    image_path: str | None = None,
    use_vector: bool = True,
) -> list[dict]:
    """Query indexed repo knowledge and return context entries ready for
    ``ChatWindow.attach_runtime_context()``.

    Each returned dict has the keys ``title``, ``language``, ``content``,
    ``source_path`` — exactly what :py:meth:`attach_runtime_context` expects.

    Parameters
    ----------
    query:        Natural-language or symbol query.
    limit:        Max number of chunks to return (default 5).
    owner_types:  "block" | "entity" | "relation" or a list thereof.
    namespace_id: Restrict to a specific AgentsDB namespace.
    image_path:   Optional in-memory snapshot path.
    use_vector:   Whether to use vector search (falls back to text if False).
    """
    result = adb_query(
        query=query,
        owner_types=owner_types,
        limit=limit,
        namespace_id=namespace_id,
        image_path=image_path,
        use_vector=use_vector,
    )

    entries: list[dict] = []
    if not result.get("ok"):
        return entries

    for chunk in result.get("chunks", []):
        owner_type = chunk.get("owner_type", "block")

        if owner_type == "block":
            content = chunk.get("content") or ""
            heading = chunk.get("heading") or "block"
            block_kind = chunk.get("block_kind") or ""
            title = f"[{block_kind}] {heading}" if block_kind else heading
        elif owner_type == "entity":
            content = chunk.get("summary") or chunk.get("canonical_name") or ""
            canonical = chunk.get("canonical_name") or "entity"
            entity_type = chunk.get("entity_type") or ""
            title = f"[entity:{entity_type}] {canonical}" if entity_type else f"[entity] {canonical}"
        elif owner_type == "relation":
            title = (
                f"[relation:{chunk.get('relation_type', '')}] "
                f"{chunk.get('source_entity_id', '')} → {chunk.get('target_entity_id', '')}"
            )
            relation_description = str(chunk.get("relation_description") or "").strip()
            content = relation_description or title
        else:
            content = str(chunk)
            title = "repo_chunk"

        if not str(content).strip():
            continue

        entries.append({
            "title": title,
            "language": "python",
            "content": str(content).strip(),
            "source_path": chunk.get("source_path") or "",
        })

    return entries


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alde.repo_code_splitter")
    parser.add_argument("scan_root", nargs="?", default=str(_ROOT), help="Repository root to index. Default: current ALDE workspace root.")
    parser.add_argument("--workers", type=int, default=4, help="Number of indexing workers.")
    parser.add_argument("--image-path", type=str, default=None, help="Optional snapshot path for in-memory fallback storage.")
    parser.add_argument("--extensions", nargs="*", default=[".py"], help="File extensions to scan. Default: .py")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON result.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    service = create_repo_index_service(image_path=args.image_path, workers=max(1, int(args.workers)))
    result = service.index_repo_object(
        str(Path(args.scan_root).expanduser().resolve()),
        extensions=tuple(args.extensions or [".py"]),
    )
    result["extensions"] = list(args.extensions or [".py"])
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
