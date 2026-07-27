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
import copy
import hashlib
import json
import os
import re
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


class RepoDeltaStateService:
    """Persist file hashes for incremental delta builds."""

    _STORAGE_PATH_ENV = "ALDE_REPO_INDEX_STATE_PATH"

    def __init__(self, *, storage_path: Path | None = None) -> None:
        self._storage_path = Path(storage_path) if storage_path is not None else None
        self._lock = threading.RLock()

    def _resolve_storage_path(self) -> Path:
        configured_path = str(os.getenv(self._STORAGE_PATH_ENV, "") or "").strip()
        if configured_path:
            return Path(os.path.abspath(os.path.expanduser(configured_path)))
        if self._storage_path is not None:
            return Path(self._storage_path)
        return _HERE.parent / "AppData" / "repo_delta_state.json"

    def _load_state_payload(self) -> dict[str, Any]:
        storage_path = self._resolve_storage_path()
        if not storage_path.is_file():
            return {}
        try:
            payload = json.loads(storage_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _store_state_payload(self, payload: Mapping[str, Any]) -> None:
        storage_path = self._resolve_storage_path()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = storage_path.with_suffix(f"{storage_path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, storage_path)

    def load_object_file_hashes(self, repo_root: str) -> dict[str, str]:
        normalized_root = str(os.path.abspath(os.path.expanduser(repo_root or ""))).strip()
        if not normalized_root:
            return {}

        with self._lock:
            state_payload = self._load_state_payload()

        roots_payload = state_payload.get("roots") if isinstance(state_payload.get("roots"), Mapping) else {}
        root_payload = roots_payload.get(normalized_root) if isinstance(roots_payload.get(normalized_root), Mapping) else {}
        files_payload = root_payload.get("files") if isinstance(root_payload.get("files"), Mapping) else {}
        return {
            str(rel_path): str(file_hash)
            for rel_path, file_hash in files_payload.items()
            if str(rel_path).strip() and str(file_hash).strip()
        }

    def store_object_file_hashes(self, repo_root: str, file_hashes: Mapping[str, str]) -> dict[str, Any]:
        normalized_root = str(os.path.abspath(os.path.expanduser(repo_root or ""))).strip()
        if not normalized_root:
            return {"ok": False, "error": "missing_repo_root"}

        normalized_hashes = {
            str(rel_path): str(file_hash)
            for rel_path, file_hash in dict(file_hashes or {}).items()
            if str(rel_path).strip() and str(file_hash).strip()
        }

        with self._lock:
            state_payload = self._load_state_payload()
            roots_payload = state_payload.get("roots") if isinstance(state_payload.get("roots"), Mapping) else {}
            mutable_roots = {
                str(root_key): dict(root_value)
                for root_key, root_value in roots_payload.items()
                if isinstance(root_value, Mapping)
            }
            mutable_roots[normalized_root] = {
                "updated_at": time.time(),
                "files": normalized_hashes,
            }
            state_payload.update(
                {
                    "schema": "repo_delta_state_v1",
                    "updated_at": time.time(),
                    "roots": mutable_roots,
                }
            )
            self._store_state_payload(state_payload)

        return {
            "ok": True,
            "repo_root": normalized_root,
            "files": len(normalized_hashes),
            "state_path": str(self._resolve_storage_path()),
        }

    def load_storage_path(self) -> str:
        return str(self._resolve_storage_path())


_REPO_DELTA_STATE_STORE = RepoDeltaStateService()

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
    sync_retrieval_run_to_agentsdb_knowledge,
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


class RepoEnvironmentOverrideParser:
    """Extract environment override accesses from Python source modules.

    Produces parser-compatible entity payloads so overrides flow through the
    existing AgentsDB mapping pipeline (entity -> relation -> embedding).
    """

    _ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def parse_object(
        self,
        *,
        source: str,
        rel_path: str,
        code_blocks: Sequence[CodeBlock],
    ) -> list[dict[str, Any]]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        os_aliases, getenv_aliases, environ_aliases = self._load_os_symbol_sets(tree)
        symbol_value_map = self._load_symbol_value_map(tree)
        line_offsets = self._build_line_offsets(source)

        occurrence_payload_list: list[dict[str, Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                occurrence_payload = self._extract_occurrence_from_call(
                    node,
                    os_aliases=os_aliases,
                    getenv_aliases=getenv_aliases,
                    environ_aliases=environ_aliases,
                    symbol_value_map=symbol_value_map,
                    line_offsets=line_offsets,
                    code_blocks=code_blocks,
                )
                if occurrence_payload:
                    occurrence_payload_list.append(occurrence_payload)
            elif isinstance(node, ast.Subscript):
                occurrence_payload = self._extract_occurrence_from_subscript(
                    node,
                    os_aliases=os_aliases,
                    environ_aliases=environ_aliases,
                    symbol_value_map=symbol_value_map,
                    line_offsets=line_offsets,
                    code_blocks=code_blocks,
                )
                if occurrence_payload:
                    occurrence_payload_list.append(occurrence_payload)

        return self._build_environment_entity_payloads(
            occurrence_payload_list=occurrence_payload_list,
            rel_path=rel_path,
        )

    def _load_os_symbol_sets(self, tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
        os_aliases = {"os"}
        getenv_aliases: set[str] = set()
        environ_aliases: set[str] = set()

        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if str(alias.name or "").strip() == "os":
                        os_aliases.add(str(alias.asname or "os").strip())
            elif isinstance(node, ast.ImportFrom) and str(node.module or "").strip() == "os":
                for alias in node.names:
                    imported_name = str(alias.name or "").strip()
                    local_name = str(alias.asname or imported_name).strip()
                    if imported_name == "getenv":
                        getenv_aliases.add(local_name)
                    elif imported_name == "environ":
                        environ_aliases.add(local_name)
        return os_aliases, getenv_aliases, environ_aliases

    def _load_symbol_value_map(self, tree: ast.AST) -> dict[str, str]:
        symbol_value_map: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                normalized_target = node.targets[0] if len(node.targets) == 1 else None
            elif isinstance(node, ast.AnnAssign):
                normalized_target = node.target
            else:
                normalized_target = None
            if not isinstance(normalized_target, ast.Name):
                continue
            if isinstance(node, ast.Assign):
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                value_node = node.value
            else:
                value_node = None
            if value_node is None:
                continue
            resolved_value = self._resolve_env_name_from_node(value_node, symbol_value_map={})
            if resolved_value:
                symbol_value_map[str(normalized_target.id)] = resolved_value
        return symbol_value_map

    def _build_line_offsets(self, source: str) -> list[int]:
        lines = source.splitlines(keepends=True)
        offsets: list[int] = []
        position = 0
        for line in lines:
            offsets.append(position)
            position += len(line)
        offsets.append(position)
        return offsets

    def _is_environ_node(self, node: ast.AST, *, os_aliases: set[str], environ_aliases: set[str]) -> bool:
        if isinstance(node, ast.Name):
            return str(node.id or "").strip() in environ_aliases
        if isinstance(node, ast.Attribute):
            return (
                str(node.attr or "").strip() == "environ"
                and isinstance(node.value, ast.Name)
                and str(node.value.id or "").strip() in os_aliases
            )
        return False

    def _resolve_env_name_from_node(
        self,
        node: ast.AST | None,
        *,
        symbol_value_map: Mapping[str, str],
    ) -> str:
        if node is None:
            return ""

        candidate = ""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidate = str(node.value or "").strip()
        elif isinstance(node, ast.Name):
            candidate = str(symbol_value_map.get(str(node.id or ""), "")).strip()

        if not candidate:
            return ""
        if not self._ENV_NAME_PATTERN.match(candidate):
            return ""
        return candidate

    def _load_call_argument(
        self,
        call_node: ast.Call,
        index: int,
        keyword_names: Sequence[str],
    ) -> ast.AST | None:
        if len(call_node.args) > index:
            return call_node.args[index]
        normalized_keyword_name_set = {str(name).strip() for name in keyword_names if str(name).strip()}
        for keyword in call_node.keywords:
            if str(keyword.arg or "").strip() in normalized_keyword_name_set:
                return keyword.value
        return None

    def _load_default_payload(self, default_node: ast.AST | None) -> tuple[Any, str]:
        if default_node is None:
            return None, "none"
        try:
            literal_value = ast.literal_eval(default_node)
            return literal_value, "literal"
        except Exception:
            try:
                expression_text = ast.unparse(default_node).strip()
            except Exception:
                expression_text = ""
            return expression_text or str(default_node), "expression"

    def _load_section_key(
        self,
        *,
        line_no: int,
        line_offsets: Sequence[int],
        code_blocks: Sequence[CodeBlock],
    ) -> str:
        if line_no <= 0 or not code_blocks:
            return "block_1"
        line_index = max(0, min(int(line_no) - 1, len(line_offsets) - 1))
        char_start = int(line_offsets[line_index]) if line_offsets else 0
        for block in code_blocks:
            if int(block.char_start) <= char_start <= max(int(block.char_start), int(block.char_end)):
                return f"block_{int(block.block_no)}"
        return "block_1"

    def _extract_occurrence_from_call(
        self,
        call_node: ast.Call,
        *,
        os_aliases: set[str],
        getenv_aliases: set[str],
        environ_aliases: set[str],
        symbol_value_map: Mapping[str, str],
        line_offsets: Sequence[int],
        code_blocks: Sequence[CodeBlock],
    ) -> dict[str, Any] | None:
        func_node = call_node.func
        env_name_node: ast.AST | None = None
        default_node: ast.AST | None = None
        access_path = ""
        required = False

        if isinstance(func_node, ast.Name) and str(func_node.id or "").strip() in getenv_aliases:
            env_name_node = self._load_call_argument(call_node, 0, ("key", "name"))
            default_node = self._load_call_argument(call_node, 1, ("default",))
            access_path = "os.getenv"
            required = False
        elif isinstance(func_node, ast.Attribute):
            attr_name = str(func_node.attr or "").strip()
            if attr_name == "getenv" and isinstance(func_node.value, ast.Name) and str(func_node.value.id or "").strip() in os_aliases:
                env_name_node = self._load_call_argument(call_node, 0, ("key", "name"))
                default_node = self._load_call_argument(call_node, 1, ("default",))
                access_path = "os.getenv"
                required = False
            elif attr_name in {"get", "setdefault", "pop"} and self._is_environ_node(
                func_node.value,
                os_aliases=os_aliases,
                environ_aliases=environ_aliases,
            ):
                env_name_node = self._load_call_argument(call_node, 0, ("key", "name"))
                default_node = self._load_call_argument(call_node, 1, ("default",))
                access_path = f"os.environ.{attr_name}"
                if attr_name == "get":
                    required = False
                elif attr_name == "setdefault":
                    required = False
                else:
                    required = default_node is None

        env_name = self._resolve_env_name_from_node(env_name_node, symbol_value_map=symbol_value_map)
        if not env_name:
            return None

        default_value, default_kind = self._load_default_payload(default_node)
        if default_node is None and access_path in {"os.getenv", "os.environ.get"}:
            default_kind = "implicit_none"
            default_value = None

        line_no = int(getattr(call_node, "lineno", 0) or 0)
        column_no = int(getattr(call_node, "col_offset", 0) or 0)
        section_key = self._load_section_key(
            line_no=line_no,
            line_offsets=line_offsets,
            code_blocks=code_blocks,
        )

        return {
            "env_name": env_name,
            "access_path": access_path,
            "required": bool(required),
            "line_no": line_no,
            "column_no": column_no,
            "section_key": section_key,
            "default_value": default_value,
            "default_kind": default_kind,
        }

    def _extract_occurrence_from_subscript(
        self,
        node: ast.Subscript,
        *,
        os_aliases: set[str],
        environ_aliases: set[str],
        symbol_value_map: Mapping[str, str],
        line_offsets: Sequence[int],
        code_blocks: Sequence[CodeBlock],
    ) -> dict[str, Any] | None:
        if not self._is_environ_node(node.value, os_aliases=os_aliases, environ_aliases=environ_aliases):
            return None

        env_name = self._resolve_env_name_from_node(node.slice, symbol_value_map=symbol_value_map)
        if not env_name:
            return None

        line_no = int(getattr(node, "lineno", 0) or 0)
        column_no = int(getattr(node, "col_offset", 0) or 0)
        section_key = self._load_section_key(
            line_no=line_no,
            line_offsets=line_offsets,
            code_blocks=code_blocks,
        )
        return {
            "env_name": env_name,
            "access_path": "os.environ[]",
            "required": True,
            "line_no": line_no,
            "column_no": column_no,
            "section_key": section_key,
            "default_value": None,
            "default_kind": "none",
        }

    def _build_environment_entity_payloads(
        self,
        *,
        occurrence_payload_list: Sequence[Mapping[str, Any]],
        rel_path: str,
    ) -> list[dict[str, Any]]:
        grouped_occurrence_payloads: dict[str, list[dict[str, Any]]] = {}
        for occurrence_payload in occurrence_payload_list:
            env_name = str(occurrence_payload.get("env_name") or "").strip()
            if not env_name:
                continue
            grouped_occurrence_payloads.setdefault(env_name, []).append(dict(occurrence_payload))

        module_name = Path(rel_path).stem
        entity_payload_list: list[dict[str, Any]] = []
        for env_name in sorted(grouped_occurrence_payloads.keys()):
            env_occurrences = grouped_occurrence_payloads[env_name]
            if not env_occurrences:
                continue

            access_paths = sorted(
                {
                    str(occurrence_payload.get("access_path") or "").strip()
                    for occurrence_payload in env_occurrences
                    if str(occurrence_payload.get("access_path") or "").strip()
                }
            )
            default_value_labels: set[str] = set()
            required_access_count = 0
            normalized_occurrence_payload_list: list[dict[str, Any]] = []

            for occurrence_payload in env_occurrences:
                is_required = bool(occurrence_payload.get("required"))
                if is_required:
                    required_access_count += 1
                default_value = occurrence_payload.get("default_value")
                default_kind = str(occurrence_payload.get("default_kind") or "none")
                if default_kind == "implicit_none":
                    default_value_labels.add("None")
                elif default_kind == "literal":
                    if isinstance(default_value, (dict, list, tuple)):
                        try:
                            default_value_labels.add(json.dumps(default_value, ensure_ascii=False, sort_keys=True))
                        except Exception:
                            default_value_labels.add(str(default_value))
                    elif default_value is None:
                        default_value_labels.add("None")
                    else:
                        default_value_labels.add(str(default_value))
                elif default_kind == "expression" and str(default_value or "").strip():
                    default_value_labels.add(str(default_value).strip())

                normalized_occurrence_payload_list.append(
                    {
                        "line_no": int(occurrence_payload.get("line_no") or 0),
                        "column_no": int(occurrence_payload.get("column_no") or 0),
                        "section_key": str(occurrence_payload.get("section_key") or "block_1"),
                        "access_path": str(occurrence_payload.get("access_path") or ""),
                        "required": is_required,
                        "default_kind": default_kind,
                        "default_value": default_value,
                    }
                )

            total_occurrence_count = len(env_occurrences)
            optional_access_count = max(0, total_occurrence_count - required_access_count)
            section_key = str(normalized_occurrence_payload_list[0].get("section_key") or "block_1") if normalized_occurrence_payload_list else "block_1"
            default_value_list = sorted(default_value_labels)
            summary = f"Environment override {env_name} referenced in {rel_path}."
            if default_value_list:
                summary = f"{summary} Defaults: {', '.join(default_value_list[:3])}."

            source_field = access_paths[0] if access_paths else "os.getenv"
            relation_description = f"{module_name} uses environment override {env_name}."
            entity_payload_list.append(
                {
                    "entity_key": f"environment_override:{env_name}",
                    "entity_type": "environment_override",
                    "canonical_name": env_name,
                    "mention_text": env_name,
                    "section_key": section_key,
                    "summary": summary,
                    "is_target": True,
                    "source_entity": "subject",
                    "is_relational": "uses_environment_override",
                    "explicit_description": relation_description,
                    "metadata": {
                        "mapped_from": "repo_env_override_parser",
                        "source_field": source_field,
                        "source_path": rel_path,
                        "relation_description": relation_description,
                        "occurrence_count": total_occurrence_count,
                    },
                    "attributes": {
                        "env_var_name": env_name,
                        "source_path": rel_path,
                        "occurrence_count": total_occurrence_count,
                        "required_access_count": required_access_count,
                        "optional_access_count": optional_access_count,
                        "access_paths": access_paths,
                        "default_values": default_value_list,
                        "occurrences": normalized_occurrence_payload_list,
                    },
                }
            )

        return entity_payload_list


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
        self._environment_override_parser = RepoEnvironmentOverrideParser()

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
            code_blocks=blocks,
        )
        environment_override_count = len(
            [
                entity_payload
                for entity_payload in entity_objects
                if str(entity_payload.get("entity_type") or "").strip() == "environment_override"
            ]
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
                "environment_override_count": environment_override_count,
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

    def _build_entity_relation_payloads(
        self,
        *,
        source: str,
        rel_path: str,
        code_blocks: Sequence[CodeBlock],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        module_name = Path(rel_path).stem
        subject_entity: dict[str, Any] = {
            "entity_key": "subject",
            "entity_type": "module",
            "canonical_name": module_name,
            "mention_text": module_name,
            "section_key": "block_1",
            "summary": f"Python module {rel_path}",
            "metadata": {"role": "subject", "source_field": "document.title", "source_path": rel_path},
        }
        entity_objects: list[dict[str, Any]] = [
            subject_entity
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

        environment_entity_payload_list = self._environment_override_parser.parse_object(
            source=source,
            rel_path=rel_path,
            code_blocks=code_blocks,
        )
        for environment_entity_payload in environment_entity_payload_list:
            env_entity_key = str(environment_entity_payload.get("entity_key") or "").strip()
            if not env_entity_key or env_entity_key in seen_entity_keys:
                continue
            entity_objects.append(dict(environment_entity_payload))
            seen_entity_keys.add(env_entity_key)

        self._apply_module_environment_attributes(
            subject_entity=subject_entity,
            environment_entity_payload_list=environment_entity_payload_list,
        )

        return entity_objects, relation_objects

    def _apply_module_environment_attributes(
        self,
        *,
        subject_entity: dict[str, Any],
        environment_entity_payload_list: Sequence[Mapping[str, Any]],
    ) -> None:
        environment_names: list[str] = []
        access_path_set: set[str] = set()
        total_occurrence_count = 0
        total_required_access_count = 0
        total_optional_access_count = 0
        environment_details: list[dict[str, Any]] = []

        for environment_entity_payload in environment_entity_payload_list:
            if str(environment_entity_payload.get("entity_type") or "").strip() != "environment_override":
                continue
            env_name = str(environment_entity_payload.get("canonical_name") or "").strip()
            attributes_payload = (
                environment_entity_payload.get("attributes")
                if isinstance(environment_entity_payload.get("attributes"), Mapping)
                else {}
            )

            if env_name:
                environment_names.append(env_name)
            occurrence_count = int(attributes_payload.get("occurrence_count") or 0)
            required_access_count = int(attributes_payload.get("required_access_count") or 0)
            optional_access_count = int(attributes_payload.get("optional_access_count") or 0)

            total_occurrence_count += max(0, occurrence_count)
            total_required_access_count += max(0, required_access_count)
            total_optional_access_count += max(0, optional_access_count)

            raw_access_path_list = attributes_payload.get("access_paths")
            if isinstance(raw_access_path_list, Sequence) and not isinstance(raw_access_path_list, (str, bytes)):
                for access_path in raw_access_path_list:
                    normalized_access_path = str(access_path or "").strip()
                    if normalized_access_path:
                        access_path_set.add(normalized_access_path)

            environment_details.append(
                {
                    "env_name": env_name,
                    "occurrence_count": max(0, occurrence_count),
                    "required_access_count": max(0, required_access_count),
                    "optional_access_count": max(0, optional_access_count),
                }
            )

        subject_entity["attributes"] = {
            "environment_override_count": len(environment_details),
            "environment_override_names": sorted({name for name in environment_names if name}),
            "environment_override_access_paths": sorted(access_path_set),
            "environment_override_occurrence_count": total_occurrence_count,
            "environment_override_required_access_count": total_required_access_count,
            "environment_override_optional_access_count": total_optional_access_count,
            "environment_overrides": sorted(environment_details, key=lambda item: str(item.get("env_name") or "")),
        }


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
        self._delta_state_store = _REPO_DELTA_STATE_STORE

    def scan_object(
        self,
        scan_root: str,
        *,
        extensions: tuple[str, ...] = (".py",),
        recursive: bool = True,
    ) -> list[str]:
        """Return sorted list of source file paths under *scan_root*."""
        result: list[str] = []
        exclude = self._DEFAULT_EXCLUDE
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = [d for d in dirnames if d not in exclude]
            for fname in filenames:
                if any(fname.endswith(ext) for ext in extensions):
                    result.append(os.path.join(dirpath, fname))
            if not bool(recursive):
                break
        return sorted(result)

    def _resolve_relative_object_path(self, source_path: str, *, scan_root: str) -> str:
        source = Path(source_path).resolve()
        root = Path(scan_root).resolve()
        try:
            return str(source.relative_to(root))
        except Exception:
            return str(Path(source_path).name)

    def _compute_object_content_sha256(self, source_path: str) -> str:
        path = Path(source_path)
        try:
            source_bytes = path.read_bytes()
        except OSError:
            return ""
        return hashlib.sha256(source_bytes).hexdigest()

    def _build_file_hash_map(self, *, scan_root: str, file_paths: Sequence[str]) -> dict[str, str]:
        file_hash_map: dict[str, str] = {}
        for source_path in file_paths:
            rel_path = self._resolve_relative_object_path(source_path, scan_root=scan_root)
            if not rel_path:
                continue
            file_hash_map[rel_path] = self._compute_object_content_sha256(source_path)
        return file_hash_map

    def _index_file_paths(self, *, scan_root: str, file_paths: Sequence[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        index_targets = [str(path) for path in file_paths if str(path).strip()]
        t0 = time.perf_counter()

        total_blocks = 0
        total_entities = 0
        total_relations = 0
        total_embedded = 0
        total_skipped = 0
        total_indexed = 0
        errors: list[str] = []
        file_results: list[dict[str, Any]] = []

        def _index_one(source_path: str) -> dict[str, Any]:
            try:
                return self.index_object(source_path, repo_root=scan_root)
            except Exception as exc:
                return {
                    "path": source_path,
                    "blocks": 0,
                    "entities": 0,
                    "relations": 0,
                    "embedded": 0,
                    "skipped": True,
                    "error": str(exc),
                }

        def _collect_result(result_payload: Mapping[str, Any]) -> None:
            nonlocal total_blocks, total_entities, total_relations, total_embedded, total_skipped, total_indexed
            result_object = dict(result_payload)
            file_results.append(result_object)
            total_blocks += int(result_object.get("blocks", 0) or 0)
            total_entities += int(result_object.get("entities", 0) or 0)
            total_relations += int(result_object.get("relations", 0) or 0)
            total_embedded += int(result_object.get("embedded", 0) or 0)
            if bool(result_object.get("skipped")):
                total_skipped += 1
            else:
                total_indexed += 1
            error_text = str(result_object.get("error") or "").strip()
            if error_text:
                errors.append(f"{result_object.get('path')}: {error_text}")

        repo_backend = getattr(self._ks, "_repository", self._repo)
        flush_context = getattr(repo_backend, "deferred_flush", None)
        if not callable(flush_context):
            flush_context = getattr(repo_backend, "deferred_write_queue", None)

        if callable(flush_context):
            with flush_context():
                with ThreadPoolExecutor(max_workers=self._workers) as executor:
                    futures = {executor.submit(_index_one, source_path): source_path for source_path in index_targets}
                    for future in as_completed(futures):
                        _collect_result(future.result())
        else:
            with ThreadPoolExecutor(max_workers=self._workers) as executor:
                futures = {executor.submit(_index_one, source_path): source_path for source_path in index_targets}
                for future in as_completed(futures):
                    _collect_result(future.result())

        elapsed = time.perf_counter() - t0
        if hasattr(repo_backend, "_flush_image"):
            repo_backend._flush_image()

        summary = {
            "scan_root": scan_root,
            "files_input": len(index_targets),
            "files_indexed": total_indexed,
            "files_skipped": total_skipped,
            "total_blocks": total_blocks,
            "total_entities": total_entities,
            "total_relations": total_relations,
            "total_embedded": total_embedded,
            "elapsed_s": round(elapsed, 2),
            "rate_files_per_s": round(total_indexed / elapsed, 2) if elapsed > 0 else 0,
            "errors": errors[:10],
        }
        return summary, file_results

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

    def index_repo_object(
        self,
        scan_root: str,
        *,
        extensions: Sequence[str] = (".py",),
        recursive: bool = True,
    ) -> dict[str, Any]:
        """Full repo index run: scan → split → store → embed. Returns summary report."""
        # Ensure namespace exists
        ns = self._builder.build_namespace_object()
        self._ks.store_namespace_object(ns)

        files = self.scan_object(scan_root, extensions=tuple(extensions), recursive=bool(recursive))
        summary, _file_results = self._index_file_paths(scan_root=scan_root, file_paths=files)
        summary["recursive"] = bool(recursive)
        summary["files_found"] = len(files)
        return summary

    def index_repo_delta_object(
        self,
        scan_root: str,
        *,
        extensions: Sequence[str] = (".py",),
        recursive: bool = True,
    ) -> dict[str, Any]:
        """Incremental repo index run that processes only changed files."""
        # Ensure namespace exists
        ns = self._builder.build_namespace_object()
        self._ks.store_namespace_object(ns)

        files = self.scan_object(scan_root, extensions=tuple(extensions), recursive=bool(recursive))
        previous_hash_map = self._delta_state_store.load_object_file_hashes(scan_root)
        current_hash_map = self._build_file_hash_map(scan_root=scan_root, file_paths=files)

        changed_files: list[str] = []
        unchanged_files = 0
        for source_path in files:
            rel_path = self._resolve_relative_object_path(source_path, scan_root=scan_root)
            current_hash = current_hash_map.get(rel_path, "")
            previous_hash = previous_hash_map.get(rel_path, "")
            if current_hash and current_hash == previous_hash:
                unchanged_files += 1
                continue
            changed_files.append(source_path)

        deleted_paths = sorted(set(previous_hash_map.keys()) - set(current_hash_map.keys()))
        summary, file_results = self._index_file_paths(scan_root=scan_root, file_paths=changed_files)

        failed_changed_paths = {
            str(result_payload.get("path") or "")
            for result_payload in file_results
            if str(result_payload.get("error") or "").strip()
        }

        persisted_hash_map: dict[str, str] = {}
        changed_file_set = {str(path) for path in changed_files}
        for source_path in files:
            rel_path = self._resolve_relative_object_path(source_path, scan_root=scan_root)
            if not rel_path:
                continue
            current_hash = current_hash_map.get(rel_path, "")
            if not current_hash:
                continue
            normalized_path = str(source_path)
            if normalized_path in changed_file_set and normalized_path in failed_changed_paths:
                previous_hash = previous_hash_map.get(rel_path, "")
                if previous_hash:
                    persisted_hash_map[rel_path] = previous_hash
                continue
            persisted_hash_map[rel_path] = current_hash

        state_result = self._delta_state_store.store_object_file_hashes(scan_root, persisted_hash_map)

        summary.update(
            {
                "recursive": bool(recursive),
                "files_found": len(files),
                "files_changed": len(changed_files),
                "files_unchanged": unchanged_files,
                "files_deleted_since_last_state": len(deleted_paths),
                "changed_paths_sample": [
                    self._resolve_relative_object_path(path, scan_root=scan_root)
                    for path in changed_files[:20]
                ],
                "deleted_paths_sample": deleted_paths[:20],
                "delta_state": {
                    "ok": bool(state_result.get("ok")),
                    "state_path": self._delta_state_store.load_storage_path(),
                    "tracked_files": int(state_result.get("files", 0) or 0),
                },
            }
        )
        return summary

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
    recursive: bool,
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
    if normalized_operation == "delta":
        normalized_operation = "delta_build"
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
            "recursive": bool(recursive),
            "files": service.scan_object(
                str(resolved_root),
                extensions=normalized_extensions,
                recursive=bool(recursive),
            ),
        }
    if normalized_operation == "delta_build":
        result = service.index_repo_delta_object(
            str(resolved_root),
            extensions=normalized_extensions,
            recursive=bool(recursive),
        )
        result["ok"] = True
        result["operation"] = "delta_build"
        return result
    if normalized_operation == "build":
        cleanup_report = None
        if cleanup_before_build:
            cleanup_report = service.delete_repo_knowledge_objects(
                object_names=normalized_cleanup_object_names,
                namespace_ids=normalized_cleanup_namespaces,
                owner_prefixes=normalized_cleanup_prefixes,
                async_delete=bool(delete_async),
            )
        result = service.index_repo_object(
            str(resolved_root),
            extensions=normalized_extensions,
            recursive=bool(recursive),
        )
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
        result = service.index_repo_object(
            str(resolved_root),
            extensions=normalized_extensions,
            recursive=bool(recursive),
        )
        result["ok"] = True
        result["operation"] = "rebuild"
        result["cleanup"] = cleanup_report
        return result

    return {
        "ok": False,
        "error": f"Unsupported operation: {operation}",
        "allowed_operations": ["scan", "delta_build", "build", "cleanup", "delete", "rebuild", "status", "repair_namespace"],
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
    recursive: bool = True,
    cleanup_before_build: bool = False,
    cleanup_namespace_ids: list[str] | str | None = None,
    cleanup_object_names: list[str] | str | None = None,
    cleanup_owner_prefixes: list[str] | str | None = None,
    delete_async: bool = True,
    run_async: bool = False,
    job_id: str | None = None,
) -> dict | str:
    """Scan or index repository knowledge using the AgentsDB parser schema."""
    try:
        normalized_operation = str(operation or "build").strip().lower()
        if normalized_operation == "delta":
            normalized_operation = "delta_build"
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
            "recursive": recursive,
            "cleanup_before_build": cleanup_before_build,
            "cleanup_namespace_ids": cleanup_namespace_ids,
            "cleanup_object_names": cleanup_object_names,
            "cleanup_owner_prefixes": cleanup_owner_prefixes,
            "delete_async": delete_async,
        }

        if bool(run_async) and normalized_operation in {"delta_build", "build", "cleanup", "delete", "rebuild", "repair_namespace"}:
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
                    "recursive": recursive,
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


def _tokenize_learning_query_text(text: str) -> set[str]:
    normalized_text = str(text or "").strip().lower()
    if not normalized_text:
        return set()
    return {
        token
        for token in re.findall(r"[a-z0-9_]{3,}", normalized_text)
        if token
    }


def _build_chunk_learning_text(chunk_payload: Mapping[str, Any]) -> str:
    text_parts = [
        str(chunk_payload.get("heading") or ""),
        str(chunk_payload.get("content") or ""),
        str(chunk_payload.get("canonical_name") or ""),
        str(chunk_payload.get("summary") or ""),
        str(chunk_payload.get("relation_description") or ""),
        str(chunk_payload.get("source_path") or ""),
        str(chunk_payload.get("entity_type") or ""),
        str(chunk_payload.get("relation_type") or ""),
    ]
    return " ".join(part for part in text_parts if part)


def _apply_learning_rerank_to_chunks(
    *,
    chunks: list[dict[str, Any]],
    query_text: str,
    learning_profile: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not chunks:
        return []
    if not isinstance(learning_profile, Mapping):
        return list(chunks)

    owner_type_boost = (
        learning_profile.get("owner_type_boost")
        if isinstance(learning_profile.get("owner_type_boost"), Mapping)
        else {}
    )
    term_weights = (
        learning_profile.get("term_weights")
        if isinstance(learning_profile.get("term_weights"), Mapping)
        else {}
    )
    matched_runs = int(learning_profile.get("matched_runs") or 0)
    if matched_runs <= 0 or (not owner_type_boost and not term_weights):
        return list(chunks)

    query_token_set = _tokenize_learning_query_text(query_text)
    max_term_weight = 0.0
    for weight_value in term_weights.values():
        try:
            max_term_weight = max(max_term_weight, float(weight_value))
        except Exception:
            continue
    if max_term_weight <= 0.0:
        max_term_weight = 1.0

    scored_chunks: list[tuple[float, float, int, dict[str, Any]]] = []
    for index, chunk_payload in enumerate(chunks):
        if not isinstance(chunk_payload, Mapping):
            continue
        chunk = dict(chunk_payload)

        try:
            base_score = float(chunk.get("score") or 0.0)
        except Exception:
            base_score = 0.0

        owner_type = str(chunk.get("owner_type") or "").strip().lower()
        try:
            owner_bonus = float(owner_type_boost.get(owner_type) or 0.0)
        except Exception:
            owner_bonus = 0.0

        chunk_token_set = _tokenize_learning_query_text(_build_chunk_learning_text(chunk))
        query_overlap_ratio = 0.0
        if query_token_set and chunk_token_set:
            query_overlap_ratio = float(len(query_token_set.intersection(chunk_token_set))) / float(len(query_token_set))

        weighted_overlap = 0.0
        for token in chunk_token_set:
            try:
                weighted_overlap += float(term_weights.get(token) or 0.0)
            except Exception:
                continue
        weighted_overlap_norm = weighted_overlap / (max_term_weight * max(1, len(chunk_token_set)))

        learning_bonus = (0.35 * owner_bonus) + (0.45 * weighted_overlap_norm) + (0.20 * query_overlap_ratio)
        final_score = (base_score * 0.70) + learning_bonus

        chunk["learning_rerank_score"] = round(final_score, 6)
        chunk["learning_bonus"] = round(learning_bonus, 6)
        scored_chunks.append((final_score, base_score, -index, chunk))

    scored_chunks.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in scored_chunks]


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

        learning_profile: dict[str, Any] = {}
        learning_rerank_applied = False
        try:
            learning_profile = knowledge_service.load_learning_rank_profile(
                namespace_id=ns,
                query_text=query,
                tool_name="repo_knowledge_query",
            )
            all_chunks = _apply_learning_rerank_to_chunks(
                chunks=all_chunks,
                query_text=query,
                learning_profile=learning_profile,
            )
            learning_rerank_applied = bool(int(learning_profile.get("matched_runs") or 0) > 0)
        except Exception:
            learning_profile = {}
            learning_rerank_applied = False

        result: dict = {
            "ok": True,
            "query": query,
            "namespace_id": ns,
            "owner_types": resolved_owner_types,
            "used_vector_search": used_vector,
            "learning_rerank": {
                "applied": learning_rerank_applied,
                "matched_runs": int(learning_profile.get("matched_runs") or 0),
                "successful_runs": int(learning_profile.get("successful_runs") or 0),
                "strategy": "learning_success_patterns_v1",
            },
            "total": len(all_chunks),
            "chunks": all_chunks,
        }

        # --- log retrieval run (best-effort) ---
        try:
            sync_retrieval_run_to_agentsdb_knowledge(
                tool_name="repo_knowledge_query",
                query_event={"query": query, "namespace_id": ns, "owner_types": resolved_owner_types, "limit": safe_limit},
                outcome_event={
                    "total": len(all_chunks),
                    "used_vector_search": used_vector,
                    "learning_rerank_applied": learning_rerank_applied,
                    "learning_matched_runs": int(learning_profile.get("matched_runs") or 0),
                },
                retrieval_result=result,
            )
        except Exception:
            pass

        return result

    except BaseException as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# load_context – format query results for ChatWindow
# ---------------------------------------------------------------------------


class LoadContextPromptParser:
    """Parse load_context prompts for graph-intent hints.

    If the user prompt contains graph-oriented terms like entity/relation or
    node/edge, the parser derives matching owner_types for adb_query.
    """

    _ENTITY_HINTS = {"entity", "entities", "node", "nodes"}
    _RELATION_HINTS = {"relation", "relations", "edge", "edges"}
    _HINT_TOKEN_PATTERN = re.compile(r"\b(entity|entities|relation|relations|node|nodes|edge|edges)\b", re.IGNORECASE)
    _SUBJECT_ROLE_PATTERNS = [
        re.compile(r"(?P<name>[A-Za-z0-9_\-ÄÖÜäöüß]+)\s*(?:=|ist|als)?\s*subjekt\b", re.IGNORECASE),
        re.compile(r"\bsubjekt\s*(?:=|:)?\s*(?P<name>[A-Za-z0-9_\-ÄÖÜäöüß]+)", re.IGNORECASE),
    ]
    _OBJECT_ROLE_PATTERNS = [
        re.compile(r"(?P<name>[A-Za-z0-9_\-ÄÖÜäöüß]+)\s*(?:=|ist|als)?\s*objekt\b", re.IGNORECASE),
        re.compile(r"\bobjekt\s*(?:=|:)?\s*(?P<name>[A-Za-z0-9_\-ÄÖÜäöüß]+)", re.IGNORECASE),
    ]
    _RELATION_BETWEEN_PATTERN = re.compile(
        r"(?:beziehung(?:en)?|relation(?:en)?)\s*(?:zwischen|zwichen)\s*"
        r"(?P<source>[A-Za-z0-9_\-ÄÖÜäöüß]+)\s*(?:und|&)\s*(?P<target>[A-Za-z0-9_\-ÄÖÜäöüß]+)"
        r"\s*(?:=|:|ist)?\s*(?P<relation>[^\.\n;]+)",
        re.IGNORECASE,
    )
    _RELATION_LABEL_PATTERN = re.compile(
        r"(?:beziehung(?:en)?|relation(?:en)?|is_relational)\s*(?:=|:|ist)\s*(?P<relation>[^\.\n;]+)",
        re.IGNORECASE,
    )
    _TYPE_PATTERN = re.compile(
        r"(?P<name>[A-Za-z0-9_\-ÄÖÜäöüß]+)\s*(?:=|ist)?\s*(?:subjekt|objekt)?\s*(?:und|ud|,)?\s*typ\s*(?P<types>[^\.\n;]+)",
        re.IGNORECASE,
    )

    class PatternEmbeddingModel:
        """Lightweight learnable pattern model for owner-type inference.

        The model maps prompt-pattern features into a fixed-dimensional embedding
        vector and uses weighted feature scores to infer entity/relation intent.
        """

        def __init__(
            self,
            *,
            dimension: int = 12,
            learning_rate: float = 0.08,
            min_score_threshold: float = 0.20,
        ) -> None:
            self.dimension = max(4, int(dimension))
            self.learning_rate = max(0.0, float(learning_rate))
            self.min_score_threshold = max(0.0, float(min_score_threshold))
            self._lock = threading.RLock()

            self._feature_weights: dict[str, float] = {
                "hint_entity": 1.00,
                "hint_relation": 1.00,
                "subject_count": 1.15,
                "object_count": 1.10,
                "relation_count": 1.35,
                "type_count": 0.95,
                "has_between_relation": 1.25,
                "has_role_keywords": 1.05,
                "has_type_keyword": 0.90,
                "has_relation_keyword": 1.20,
            }

            self._entity_feature_set = {
                "hint_entity",
                "subject_count",
                "object_count",
                "type_count",
                "has_role_keywords",
                "has_type_keyword",
            }
            self._relation_feature_set = {
                "hint_relation",
                "relation_count",
                "has_between_relation",
                "has_relation_keyword",
            }

            # Stable feature-to-dimension mapping by insertion order.
            self._feature_dims: dict[str, int] = {
                feature_name: index % self.dimension
                for index, feature_name in enumerate(self._feature_weights.keys())
            }

        @staticmethod
        def _normalize_embedding_object(values: list[float]) -> list[float]:
            squared_sum = sum(float(item) * float(item) for item in values)
            if squared_sum <= 0.0:
                return values
            norm = squared_sum ** 0.5
            return [float(item) / norm for item in values]

        def _weight_value_object(self, feature_name: str) -> float:
            return float(self._feature_weights.get(feature_name, 0.0))

        def _score_feature_set_object(self, feature_values: Mapping[str, float], feature_set: set[str]) -> float:
            score_value = 0.0
            for feature_name in feature_set:
                value = float(feature_values.get(feature_name, 0.0))
                if value <= 0.0:
                    continue
                score_value += value * self._weight_value_object(feature_name)
            return score_value

        def _embedding_object(self, feature_values: Mapping[str, float]) -> list[float]:
            embedding_values = [0.0] * self.dimension
            for feature_name, value in feature_values.items():
                numeric_value = float(value)
                if numeric_value <= 0.0:
                    continue
                dim_index = int(self._feature_dims.get(feature_name, 0))
                embedding_values[dim_index] += numeric_value * self._weight_value_object(feature_name)
            return self._normalize_embedding_object(embedding_values)

        def _update_weights_object(self, feature_values: Mapping[str, float]) -> None:
            if self.learning_rate <= 0.0:
                return
            for feature_name, feature_value in feature_values.items():
                numeric_value = float(feature_value)
                if numeric_value <= 0.0:
                    continue
                current_weight = self._weight_value_object(feature_name)
                updated_weight = current_weight + (self.learning_rate * numeric_value)
                # Keep weights bounded and numerically stable.
                self._feature_weights[feature_name] = min(max(updated_weight, 0.05), 4.00)

        def infer_object(self, feature_values: Mapping[str, float]) -> dict[str, Any]:
            with self._lock:
                entity_score = self._score_feature_set_object(feature_values, self._entity_feature_set)
                relation_score = self._score_feature_set_object(feature_values, self._relation_feature_set)

                owner_types: list[str] = []
                if entity_score >= self.min_score_threshold:
                    owner_types.append("entity")
                if relation_score >= self.min_score_threshold:
                    owner_types.append("relation")

                if not owner_types and (entity_score > 0.0 or relation_score > 0.0):
                    owner_types = ["entity"] if entity_score >= relation_score else ["relation"]

                embedding_values = self._embedding_object(feature_values)

                # Online adaptation: reinforce frequently observed active features.
                self._update_weights_object(feature_values)

            return {
                "owner_types": owner_types,
                "embedding": embedding_values,
                "scores": {
                    "entity": entity_score,
                    "relation": relation_score,
                },
                "dimension": self.dimension,
            }

        def export_state_object(self) -> dict[str, Any]:
            with self._lock:
                return {
                    "dimension": self.dimension,
                    "learning_rate": self.learning_rate,
                    "weights": dict(self._feature_weights),
                }

    _ROLE_KEYWORD_PATTERN = re.compile(r"\b(subjekt|objekt|subject|object)\b", re.IGNORECASE)
    _TYPE_KEYWORD_PATTERN = re.compile(r"\btyp(?:en)?\b", re.IGNORECASE)
    _RELATION_KEYWORD_PATTERN = re.compile(r"\b(beziehung|beziehungen|relation|relationen|edge|edges)\b", re.IGNORECASE)
    _ROLE_STOPWORDS = {
        "und",
        "oder",
        "mit",
        "zum",
        "zur",
        "ist",
        "als",
        "typ",
        "subjekt",
        "objekt",
        "beziehung",
        "relation",
    }

    def __init__(self) -> None:
        self._pattern_embedding_model = self.PatternEmbeddingModel()

    @staticmethod
    def _deduplicate_object(values: list[str]) -> list[str]:
        normalized_values: list[str] = []
        seen_values: set[str] = set()
        for value in values:
            item_value = str(value or "").strip()
            if not item_value:
                continue
            lower_value = item_value.lower()
            if lower_value in seen_values:
                continue
            seen_values.add(lower_value)
            normalized_values.append(item_value)
        return normalized_values

    def _normalize_role_name_object(self, role_value: str) -> str:
        normalized_value = str(role_value or "").strip(" .,;:-")
        if not normalized_value:
            return ""
        if normalized_value.lower() in self._ROLE_STOPWORDS:
            return ""
        return normalized_value

    def _extract_hint_flags(self, query_text: str) -> tuple[bool, bool]:
        tokens = {match.group(1).lower() for match in self._HINT_TOKEN_PATTERN.finditer(str(query_text or ""))}
        wants_entities = bool(tokens & self._ENTITY_HINTS)
        wants_relations = bool(tokens & self._RELATION_HINTS)
        return wants_entities, wants_relations

    def _sanitize_query_object(self, query_text: str) -> str:
        """Remove pure owner-type hint tokens to keep semantic query terms."""
        cleaned = self._HINT_TOKEN_PATTERN.sub(" ", str(query_text or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
        return cleaned

    def _extract_role_payload(self, query_object: str) -> dict[str, list[str]]:
        query_text = str(query_object or "")
        subject_names: list[str] = []
        object_names: list[str] = []
        relation_labels: list[str] = []
        type_labels: list[str] = []

        for pattern in self._SUBJECT_ROLE_PATTERNS:
            for match in pattern.finditer(query_text):
                subject_name = self._normalize_role_name_object(str(match.group("name") or ""))
                if subject_name:
                    subject_names.append(subject_name)

        for pattern in self._OBJECT_ROLE_PATTERNS:
            for match in pattern.finditer(query_text):
                object_name = self._normalize_role_name_object(str(match.group("name") or ""))
                if object_name:
                    object_names.append(object_name)

        for match in self._RELATION_BETWEEN_PATTERN.finditer(query_text):
            source_name = self._normalize_role_name_object(str(match.group("source") or ""))
            target_name = self._normalize_role_name_object(str(match.group("target") or ""))
            relation_text = str(match.group("relation") or "").strip(" ,")
            if source_name:
                subject_names.append(source_name)
            if target_name:
                object_names.append(target_name)
            if relation_text:
                relation_labels.append(relation_text)

        for match in self._RELATION_LABEL_PATTERN.finditer(query_text):
            relation_text = str(match.group("relation") or "").strip(" ,")
            if relation_text:
                relation_labels.append(relation_text)

        for match in self._TYPE_PATTERN.finditer(query_text):
            raw_types = str(match.group("types") or "")
            for value in re.split(r"[,/|]+", raw_types):
                cleaned_type = str(value or "").strip(" .")
                if cleaned_type:
                    type_labels.append(cleaned_type)

        return {
            "subjects": self._deduplicate_object(subject_names),
            "objects": self._deduplicate_object(object_names),
            "relations": self._deduplicate_object(relation_labels),
            "types": self._deduplicate_object(type_labels),
        }

    def _build_semantic_query_object(self, query_text: str, role_payload: Mapping[str, Sequence[str]]) -> str:
        query_terms: list[str] = [str(query_text or "").strip()]
        query_terms.extend([str(value) for value in role_payload.get("subjects", [])])
        query_terms.extend([str(value) for value in role_payload.get("objects", [])])
        query_terms.extend([str(value) for value in role_payload.get("relations", [])])
        query_terms.extend([str(value) for value in role_payload.get("types", [])])
        merged_query = " ".join(self._deduplicate_object(query_terms)).strip()
        return merged_query

    def _build_feature_values_object(
        self,
        query_text: str,
        role_payload: Mapping[str, Sequence[str]],
        wants_entities: bool,
        wants_relations: bool,
    ) -> dict[str, float]:
        normalized_query_text = str(query_text or "")
        relation_values = list(role_payload.get("relations") or [])
        feature_values: dict[str, float] = {
            "hint_entity": 1.0 if wants_entities else 0.0,
            "hint_relation": 1.0 if wants_relations else 0.0,
            "subject_count": float(len(list(role_payload.get("subjects") or []))),
            "object_count": float(len(list(role_payload.get("objects") or []))),
            "relation_count": float(len(relation_values)),
            "type_count": float(len(list(role_payload.get("types") or []))),
            "has_between_relation": 1.0 if (" zwischen " in f" {normalized_query_text.lower()} " or " between " in f" {normalized_query_text.lower()} ") else 0.0,
            "has_role_keywords": 1.0 if self._ROLE_KEYWORD_PATTERN.search(normalized_query_text) else 0.0,
            "has_type_keyword": 1.0 if self._TYPE_KEYWORD_PATTERN.search(normalized_query_text) else 0.0,
            "has_relation_keyword": 1.0 if self._RELATION_KEYWORD_PATTERN.search(normalized_query_text) else 0.0,
        }
        return feature_values

    def parse_object(
        self,
        query_object: str,
        owner_types_object: list[str] | str | None,
    ) -> tuple[str, list[str] | str | None]:
        parsed_query, parsed_owner_types, _signal_payload = self.parse_with_signal_object(
            query_object=query_object,
            owner_types_object=owner_types_object,
        )
        return parsed_query, parsed_owner_types

    def parse_with_signal_object(
        self,
        query_object: str,
        owner_types_object: list[str] | str | None,
    ) -> tuple[str, list[str] | str | None, dict[str, Any]]:
        """Return normalized query + inferred owner_types for load_context."""
        query_text = str(query_object or "").strip()
        if not query_text:
            return query_text, owner_types_object, {"mode": "empty_query", "pattern_embedding": []}

        # Respect explicit owner_types from caller and avoid overriding intent.
        if owner_types_object is not None:
            return query_text, owner_types_object, {
                "mode": "explicit_owner_types",
                "pattern_embedding": [],
                "owner_types": owner_types_object,
            }

        role_payload = self._extract_role_payload(query_text)
        wants_entities, wants_relations = self._extract_hint_flags(query_text)
        if role_payload.get("subjects") or role_payload.get("objects") or role_payload.get("types"):
            wants_entities = True
        if role_payload.get("relations"):
            wants_relations = True
        if not wants_entities and not wants_relations:
            return query_text, None, {
                "mode": "no_pattern_match",
                "pattern_embedding": [],
                "role_payload": role_payload,
            }

        feature_values = self._build_feature_values_object(
            query_text=query_text,
            role_payload=role_payload,
            wants_entities=wants_entities,
            wants_relations=wants_relations,
        )
        model_result = self._pattern_embedding_model.infer_object(feature_values)
        resolved_owner_types = list(model_result.get("owner_types") or [])
        if not resolved_owner_types:
            if wants_entities and wants_relations:
                resolved_owner_types = ["entity", "relation"]
            elif wants_entities:
                resolved_owner_types = ["entity"]
            else:
                resolved_owner_types = ["relation"]

        sanitized_query = self._sanitize_query_object(query_text)
        semantic_query = self._build_semantic_query_object(sanitized_query, role_payload)
        signal_payload = {
            "mode": "pattern_embedding_inference",
            "feature_values": feature_values,
            "role_payload": role_payload,
            "model_scores": copy.deepcopy(model_result.get("scores") or {}),
            "pattern_embedding": list(model_result.get("embedding") or []),
            "embedding_dimension": int(model_result.get("dimension") or 0),
            "weight_snapshot": self._pattern_embedding_model.export_state_object(),
            "owner_types": list(resolved_owner_types),
        }
        return (semantic_query or sanitized_query or query_text), resolved_owner_types, signal_payload


_LOAD_CONTEXT_PROMPT_PARSER = LoadContextPromptParser()


def _sync_load_context_learning_signal(
    *,
    user_prompt: str,
    parsed_query: str,
    parsed_owner_types: list[str] | str | None,
    model_result: Any,
    runtime_context: Any,
    retrieval_result: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    pattern_signal: Mapping[str, Any] | None,
    correlation_id: str | None,
    namespace_id: str | None,
) -> None:
    try:
        try:
            from .agents_db import sync_learning_interaction_to_agentsdb_knowledge
        except ImportError:
            from alde.agents_db import sync_learning_interaction_to_agentsdb_knowledge

        context_payload = {
            "entries": [dict(item) for item in entries if isinstance(item, Mapping)],
            "chunks": list(retrieval_result.get("chunks") or []),
            "runtime_context": copy.deepcopy(runtime_context),
            "owner_types": copy.deepcopy(parsed_owner_types),
            "parsed_query": str(parsed_query or ""),
        }
        sync_learning_interaction_to_agentsdb_knowledge(
            tool_name="load_context",
            user_prompt=str(user_prompt or ""),
            model_result=model_result,
            context_payload=context_payload,
            pattern_signal=pattern_signal,
            correlation_id=correlation_id,
            namespace_id=namespace_id,
        )
    except Exception:
        # Best-effort telemetry: never break load_context on sync issues.
        return

def load_context(
    query: str,
    *,
    limit: int = 5,
    owner_types: list[str] | str | None = None,
    namespace_id: str | None = None,
    image_path: str | None = None,
    use_vector: bool = True,
    model_result: Any = None,
    runtime_context: Any = None,
    correlation_id: str | None = None,
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
    model_result: Optional model output to persist as learning signal.
    runtime_context: Optional additional context payload for learning sync.
    correlation_id: Optional correlation id for persisted learning events.
    """
    parsed_query, parsed_owner_types, pattern_signal = _LOAD_CONTEXT_PROMPT_PARSER.parse_with_signal_object(
        query_object=query,
        owner_types_object=owner_types,
    )

    result = adb_query(
        query=parsed_query,
        owner_types=parsed_owner_types,
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

    _sync_load_context_learning_signal(
        user_prompt=query,
        parsed_query=parsed_query,
        parsed_owner_types=parsed_owner_types,
        model_result=model_result,
        runtime_context=runtime_context,
        retrieval_result=result,
        entries=entries,
        pattern_signal=pattern_signal,
        correlation_id=correlation_id,
        namespace_id=namespace_id,
    )

    return entries


def load_repo_context_for_ide_agent(
    query: str,
    *,
    limit: int = 5,
    owner_types: list[str] | str | None = None,
    namespace_id: str | None = None,
    image_path: str | None = None,
    use_vector: bool = True,
    model_result: Any = None,
    runtime_context: Any = None,
    correlation_id: str | None = None,
) -> list[dict]:
    """Backward-compatible alias for load_context()."""
    return load_context(
        query=query,
        limit=limit,
        owner_types=owner_types,
        namespace_id=namespace_id,
        image_path=image_path,
        use_vector=use_vector,
        model_result=model_result,
        runtime_context=runtime_context,
        correlation_id=correlation_id,
    )


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
