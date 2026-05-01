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

import ast
import hashlib
import os
import re
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path bootstrap (run from project root OR from ALDE/alde/)
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
for _p in (_ROOT, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents_db import (  # type: ignore
    AgentDbInMemoryRepository,
    BlockObject,
    DocumentObject,
    EntityRelationEmbeddingService,
    KnowledgeObjectService,
    NamespaceObject,
    RuntimeConfigObject,
    _now_utc,
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
        repo: AgentDbInMemoryRepository,
        knowledge_svc: KnowledgeObjectService,
        emb_svc: EntityRelationEmbeddingService,
        *,
        workers: int = 4,
    ) -> None:
        self._repo = repo
        self._ks = knowledge_svc
        self._emb_svc = emb_svc
        self._workers = workers
        self._splitter = PythonCodeSplitter()
        self._builder = RepoDocumentBuilder()

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
        """Split, store, and embed a single source file. Returns per-file report."""
        blocks = self._splitter.split_object(source_path)
        if not blocks:
            return {"path": source_path, "blocks": 0, "embedded": 0, "skipped": True}

        doc = self._builder.build_object(source_path, blocks, repo_root=repo_root)
        self._ks.store_document_object(doc)

        embedded = 0
        for blk in doc.blocks:
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

        return {
            "path": source_path,
            "doc_id": doc.id,
            "blocks": len(doc.blocks),
            "embedded": embedded,
            "skipped": False,
        }

    def index_repo_object(self, scan_root: str) -> dict[str, Any]:
        """Full repo index run: scan → split → store → embed. Returns summary report."""
        # Ensure namespace exists
        ns = self._builder.build_namespace_object()
        self._ks.store_namespace_object(ns)

        files = self.scan_object(scan_root)
        t0 = time.perf_counter()

        total_blocks = 0
        total_embedded = 0
        total_skipped = 0
        errors: list[str] = []

        def _index_one(fpath: str) -> dict[str, Any]:
            try:
                return self.index_object(fpath, repo_root=scan_root)
            except Exception as exc:
                return {"path": fpath, "blocks": 0, "embedded": 0, "skipped": True, "error": str(exc)}

        with ThreadPoolExecutor(max_workers=self._workers) as executor:
            futures = {executor.submit(_index_one, fp): fp for fp in files}
            for fut in as_completed(futures):
                r = fut.result()
                total_blocks += r.get("blocks", 0)
                total_embedded += r.get("embedded", 0)
                if r.get("skipped"):
                    total_skipped += 1
                if r.get("error"):
                    errors.append(f"{r['path']}: {r['error']}")

        elapsed = time.perf_counter() - t0
        self._repo._flush_image()

        return {
            "scan_root": scan_root,
            "files_found": len(files),
            "files_skipped": total_skipped,
            "total_blocks": total_blocks,
            "total_embedded": total_embedded,
            "elapsed_s": round(elapsed, 2),
            "rate_files_per_s": round((len(files) - total_skipped) / elapsed, 2) if elapsed > 0 else 0,
            "errors": errors[:10],
        }
