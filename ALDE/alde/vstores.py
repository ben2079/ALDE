from __future__ import annotations
from annotated_types import doc
from numpy import rint
from sqlalchemy import literal

#  August 2025
# Maintainer contact: see repository README.
#  Module: vstores.py
# ───────────────────────────── Imports ──────────────────────────────

import json
import sys
import hashlib
from pathlib import Path
from typing import Iterable, List, Set, Dict, Any
import os

from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import CharacterTextSplitter, MarkdownTextSplitter, RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from contextlib import suppress
from langchain_core.documents import Document
from langchain_community.document_loaders import (  # add PyPDFLoader + custom loader
    PyPDFLoader,
    TextLoader,
    PythonLoader,
    DirectoryLoader
)
try:
    from .embed_tool import build  # type: ignore
except Exception:
    from embed_tool import build  # type: ignore

try:
    from . import torch_init  # type: ignore
except Exception:
    import torch_init  # type: ignore

try:
    from .get_path import GetPath  # type: ignore
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from get_path import GetPath  # type: ignore
    else:
        raise
# ───────────────────────────── Modul-Variablen ─────────────────────────────
__all__ = ["VectorStore"]  # stellt sicher, dass nur das Public-API exportiert wird
# ─────────────────────── Konfigurations-Konstanten ──────────────────────
# Ordner, der indiziert wird. Kann beim CLI-Aufruf über --path überschrieben werden.
DEFAULT_PROJECT_ROOT = GetPath().get_path(parg = f"{__file__}" 'AppData', opt = 'p')
# Ablageort für Index + Metadaten (FAISS benötigt ein Verzeichnis, kein File)
FAISS_INDEX_PATH:GetPath = GetPath()._parent(parg = f"{__file__}") + f"AppData/VSM_0_Data"

if not os.path.isdir(FAISS_INDEX_PATH):
    os.makedirs(FAISS_INDEX_PATH, exist_ok=True)

# Hugging-Face Modell
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Text-Chunk Parameter
CHUNK_STRATEGY = os.getenv("AI_IDE_VSTORE_CHUNK_STRATEGY", "recursive").strip().lower() or "recursive"
CHUNK_SIZE = int(os.getenv("AI_IDE_VSTORE_CHUNK_SIZE", "1000") or 1000)
CHUNK_OVERLAP = int(os.getenv("AI_IDE_VSTORE_CHUNK_OVERLAP", "150") or 150)
# Trefferzahl für query()
DEFAULT_TOP_K = 50
# Datei, in der bereits indizierte Quell­pfade gespeichert werden
MANIFEST_FILE:GetPath = GetPath()._parent(parg = f"{__file__}") + f"AppData/VSM_0_Data/manifest.json"

# Embeddings device selection.
# - Set `AI_IDE_EMBEDDINGS_DEVICE=cuda` (or `cuda:0`) to force GPU.
# - Set `AI_IDE_EMBEDDINGS_DEVICE=cpu` to force CPU.
# - Default: `auto` (use GPU if available, else CPU).
EMBEDDINGS_DEVICE = os.getenv("AI_IDE_EMBEDDINGS_DEVICE", "auto")
FAISS_USE_GPU = os.getenv("AI_IDE_FAISS_USE_GPU", "1").strip() in {"1", "true", "True"}
FAISS_REQUIRE_GPU = os.getenv("AI_IDE_FAISS_REQUIRE_GPU", "0").strip() in {"1", "true", "True"}
FAISS_GPU_DEVICE = int(os.getenv("AI_IDE_FAISS_GPU_DEVICE", "0") or 0)


def _select_embeddings_device() -> str:
    desired = (EMBEDDINGS_DEVICE or "auto").strip()
    if desired and desired.lower() != "auto":
        return desired

    # Auto: prefer CUDA if available.
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load_faiss_module():
    import faiss

    return faiss


def _faiss_gpu_status() -> dict[str, Any]:
    try:
        faiss = _load_faiss_module()
    except Exception as exc:
        return {
            "available": False,
            "num_gpus": 0,
            "reason": f"faiss import failed: {type(exc).__name__}: {exc}",
        }

    try:
        num_gpus = int(faiss.get_num_gpus()) if hasattr(faiss, "get_num_gpus") else 0
    except Exception as exc:
        return {
            "available": False,
            "num_gpus": 0,
            "reason": f"faiss.get_num_gpus() failed: {type(exc).__name__}: {exc}",
        }

    has_resources = hasattr(faiss, "StandardGpuResources")
    has_cpu_to_gpu = hasattr(faiss, "index_cpu_to_gpu")
    has_gpu_to_cpu = hasattr(faiss, "index_gpu_to_cpu")
    available = bool(num_gpus > 0 and has_resources and has_cpu_to_gpu and has_gpu_to_cpu)

    if available:
        reason = "gpu bindings available"
    elif num_gpus <= 0:
        reason = "faiss reports 0 GPUs"
    elif not has_resources:
        reason = "faiss has no StandardGpuResources"
    elif not has_cpu_to_gpu:
        reason = "faiss has no index_cpu_to_gpu"
    else:
        reason = "faiss has no index_gpu_to_cpu"

    return {
        "available": available,
        "num_gpus": num_gpus,
        "reason": reason,
    }

# Use multithreading only when explicitly enabled; some native stacks (torch/faiss)
# can crash when combined with aggressive multithreading.
USE_MULTITHREADING = os.getenv("AI_IDE_VSTORE_MULTITHREAD", "0").strip() in {"1", "true", "True"}

# Retrieval tuning
VSTORE_DEDUP = os.getenv("AI_IDE_VSTORE_DEDUP", "1").strip() in {"1", "true", "True"}
VSTORE_RERANK = os.getenv("AI_IDE_VSTORE_RERANK", "1").strip() in {"1", "true", "True"}
VSTORE_RERANK_METHOD = os.getenv("AI_IDE_VSTORE_RERANK_METHOD", "mmr").strip().lower()
VSTORE_RERANK_MODEL = os.getenv(
    "AI_IDE_VSTORE_RERANK_MODEL",
    # multilingual + reasonably small
    "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
)
VSTORE_FETCH_K = int(os.getenv("AI_IDE_VSTORE_FETCH_K", "0") or 0)  # 0 => auto
VSTORE_MAX_CONTENT_CHARS = int(os.getenv("AI_IDE_VSTORE_MAX_CONTENT_CHARS", "2000") or 2000)
VSTORE_INCLUDE_METADATA = os.getenv("AI_IDE_VSTORE_INCLUDE_METADATA", "1").strip() in {"1", "true", "True"}

_CROSS_ENCODER = None


def _get_cross_encoder():
    global _CROSS_ENCODER
    if VSTORE_RERANK_METHOD != "crossencoder":
        return None
    if _CROSS_ENCODER is not None:
        return _CROSS_ENCODER
    try:
        from sentence_transformers import CrossEncoder

        _CROSS_ENCODER = CrossEncoder(VSTORE_RERANK_MODEL, device=_select_embeddings_device())
    except Exception:
        _CROSS_ENCODER = None
    return _CROSS_ENCODER
# ─────────────────────── Performance Monitoring ───────────────────────


# ─────────────────────── Metadata-Normalisierung ───────────────────────
def _norm_source(value: object) -> str:
    """Normalisiert Document.metadata['source'] zu einem hashbaren String.

    Einige Loader liefern strukturierte Werte (z.B. dict) als `source`.
    Diese sind nicht hashbar und brechen Set/Dict-Operationen.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(value)
    return str(value)


def _safe_title_from_source(source: str) -> str:
    with suppress(Exception):
        return Path(source).name
    return (source or "unknown")


def _document_key_from_metadata(metadata: dict[str, Any]) -> str:
    source = _norm_source(metadata.get("source"))
    source_key = metadata.get("source-key")
    if source_key not in (None, ""):
        return f"{source}#entry:{source_key}"
    page = metadata.get("page")
    if page not in (None, ""):
        return f"{source}#page:{page}"
    return source


def _document_key(doc: Document) -> str:
    return _document_key_from_metadata(doc.metadata)


def _normalize_requested_doc_types(doc_types: str | Iterable[str] | None) -> set[str] | None:
    if doc_types is None:
        return None

    if isinstance(doc_types, str):
        raw_items = [doc_types]
    else:
        raw_items = [str(item) for item in doc_types]

    suffixes: set[str] = set()
    alias_map: dict[str, set[str]] = {
        "text": {".txt", ".text"},
        ".text": {".txt", ".text"},
        "markdown": {".md"},
        "md": {".md"},
        "txt": {".txt"},
        "py": {".py"},
        "python": {".py"},
        "pdf": {".pdf"},
        "json": {".json"},
        "yaml": {".yaml"},
        "yml": {".yml"},
        "rst": {".rst"},
        "toml": {".toml"},
        "sqlite": {".sqlite"},
        "sqlite3": {".sqlite3"},
    }

    for raw in raw_items:
        token = (raw or "").strip().lower()
        if not token:
            continue
        if token in alias_map:
            suffixes.update(alias_map[token])
            continue
        if not token.startswith("."):
            token = f".{token}"
        suffixes.add(token)

    return suffixes


def _manifest_key_to_source_path(key: str) -> str:
    return str(key).split("#entry:", 1)[0].split("#page:", 1)[0]


def _json_item_to_text(item: Any) -> str:
    if isinstance(item, dict):
        parts: list[str] = []
        content = item.get("content")
        if content not in (None, ""):
            parts.append(str(content))
        for field in (
            "role",
            "name",
            "assistant-name",
            "thread-name",
            "event",
            "generated",
            "date",
            "time",
        ):
            value = item.get(field)
            if value not in (None, "", [], {}):
                parts.append(f"{field}: {value}")
        for field in ("tool_calls", "data"):
            value = item.get(field)
            if value not in (None, "", [], {}):
                try:
                    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                except Exception:
                    rendered = str(value)
                parts.append(f"{field}: {rendered}")
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(item)


def _resolve_chunking_config(
    chunk_strategy: str | None = None,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> tuple[str, int, int]:
    strategy_aliases = {
        "default": "recursive",
        "recursivecharacter": "recursive",
        "char": "character",
        "charactertext": "character",
        "md": "markdown",
    }
    strategy = str(chunk_strategy or CHUNK_STRATEGY or "recursive").strip().lower()
    strategy = strategy_aliases.get(strategy, strategy)
    valid_strategies = {"recursive", "character", "markdown"}
    if strategy not in valid_strategies:
        raise ValueError(
            f"Unsupported chunk_strategy {chunk_strategy!r}. Expected one of: {sorted(valid_strategies)}"
        )

    resolved_chunk_size = CHUNK_SIZE if chunk_size is None else int(chunk_size)
    resolved_overlap = CHUNK_OVERLAP if overlap is None else int(overlap)

    if resolved_chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {resolved_chunk_size}")
    if resolved_overlap < 0:
        raise ValueError(f"overlap must be >= 0, got {resolved_overlap}")
    if resolved_overlap >= resolved_chunk_size:
        raise ValueError(
            f"overlap must be smaller than chunk_size, got overlap={resolved_overlap}, chunk_size={resolved_chunk_size}"
        )
    return strategy, resolved_chunk_size, resolved_overlap


def _create_text_splitter(
    chunk_strategy: str | None = None,
    chunk_size: int | None = None,
    overlap: int | None = None,
):
    strategy, resolved_chunk_size, resolved_overlap = _resolve_chunking_config(
        chunk_strategy=chunk_strategy,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    splitter_cls_map = {
        "recursive": RecursiveCharacterTextSplitter,
        "character": CharacterTextSplitter,
        "markdown": MarkdownTextSplitter,
    }
    splitter_cls = splitter_cls_map[strategy]
    return splitter_cls(
        chunk_size=resolved_chunk_size,
        chunk_overlap=resolved_overlap,
        length_function=len,
    )

# ─────────────────────── Hilfs-/Utility-Funktionen ───────────────────────
def _log(e:str=None, msg:str="") -> None:
        """'Einfaches Konsolen-Logging."""
        #print(f"{e}\n[VectorStoreProject]{msg}\n")


class SafeTextLoader(TextLoader):
    """
    Erweiterung von LangChains TextLoader, die *jedes* Encoding akzeptiert.
    Fehlschläge beim Lesen einzelner Dateien werden nicht propagiert, sondern
    lediglich protokolliert.  Dadurch bricht der Build-Vorgang nie wegen
    UnicodeDecodeError ab.
    """
    def load(self) -> List[Document]:                           # type: ignore[override]
        try:
            return super().load()
        except Exception as err:
            _log(err, f"Überspringe Datei (Encoding-Fehler): {self.file_path}")
            return []

def _load_json(path: str | Path) -> List[Document]:
        """Load JSON files from *path* and convert list/dict entries to Documents."""
        root = Path(path).expanduser().resolve()
        json_files: list[Path]
        if root.is_file():
            json_files = [root] if root.suffix.lower() == ".json" else []
        else:
            json_files = [p for p in root.rglob("*.json") if p.is_file()]

        documents: list[Document] = []
        for json_file in json_files:
            if json_file.name == "manifest.json":
                continue
            try:
                with open(json_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as err:
                _log(str(err), f"JSON übersprungen (Lade-Fehler): {json_file}")
                continue

            items = data if isinstance(data, list) else [data]
            for idx, item in enumerate(items):
                content_str = _json_item_to_text(item)
                stable_seed = f"{json_file}:{idx}:{content_str}"
                if isinstance(item, dict):
                    stable_seed = "|".join(
                        [
                            str(json_file),
                            str(item.get("message-id", "")),
                            str(item.get("thread-id", "")),
                            str(item.get("time", "")),
                            str(idx),
                            content_str,
                        ]
                    )
                source_key = hashlib.sha1(stable_seed.encode("utf-8", errors="ignore")).hexdigest()

                metadata: dict[str, Any] = {
                    "source": str(json_file),
                    "source-key": source_key,
                    "titel": json_file.name,
                    "index": idx,
                }
                if isinstance(item, dict):
                    metadata.update(
                        {
                            "role": item.get("role"),
                            "message-id": item.get("message-id"),
                            "thread-id": item.get("thread-id"),
                            "time": item.get("time"),
                            "date": item.get("date"),
                            "vector": item.get("vec"),
                            "id": item.get("id"),
                            "generated": item.get("generated"),
                            "event": item.get("event"),
                            "model": item.get("model"),
                            "response_id": item.get("response_id"),
                            "stage": item.get("stage"),
                            "assistant-name": item.get("assistant-name"),
                            "thread-name": item.get("thread-name"),
                            "tool_call_id": item.get("tool_call_id"),
                            "name": item.get("name"),
                        }
                    )
                metadata["id"] = metadata.get("id") or source_key
                documents.append(Document(page_content=content_str, metadata=metadata))

        return documents
# ──────────────────────────────────────────────────────────────────────────    
    # 2) HELPER: robuster PDF-Loader  ──────────────────────────────────────────
def _load_pdf(path: str | Path) -> list[Document]:
    """
    Liefert eine Liste Document-Objekte für *path*.

    • 1. Versuch:  PyPDFLoader  (schnell, nutzt Text-Layer des PDF)
    • 2. Fallback: UnstructuredPDFLoader mit OCR
                   (wenn PyPDF keine verwertbaren Texte liefert)
    
    OCR-Sprache: Deutsch (deu) für bessere Texterkennung bei deutschen PDFs.
    Tesseract muss mit deutschen Sprachdaten installiert sein:
        sudo apt install tesseract-ocr tesseract-ocr-deu
    """
    _log("", f"Versuche PDF zu laden: {path}")
    try:
        docs = PyPDFLoader(str(path)).load()
    except Exception as err:
        _log(str(err), f"✗ PDF übersprungen (Loader-Fehler): {path}")
        return []

    docs = [d for d in docs if (d.page_content or "").strip()]
    if not docs:
        _log("", f"✗ PDF übersprungen (kein Text extrahierbar): {path}")
        return []

    for d in docs:
        d.metadata["source"] = str(path)
        d.metadata["titel"] = Path(path).name
        d.metadata["applied"] = "no"
        d.metadata["id"] = hex(hash(f"{d.metadata['source']}{(d.page_content or '')[:100]}"))

    preview = docs[0].page_content[:200].replace("\n", " ")
    _log("", f"✓ PDF erfolgreich mit PyPDFLoader geladen: {path} ({len(docs)} Seiten)")
    _log("", f"  Preview: {preview}...")
    return docs
# ──────────────────────────────────────────────────────────────────────────
# 3)  _iter_documents  (komplett ersetzen)  
def _iter_documents(root: str | Path, doc_types: str | Iterable[str] | None = None) -> list[Document]:
    """
    Traversiert *root* rekursiv und erzeugt LangChain-Document-Objekte für

        • *.py                 → PythonLoader
    _log("", f"✗ PDF übersprungen (kein Text extrahierbar): {path}")
        • *.pdf                → PyPDFLoader;  Fallback: OCR

    Binäre / kaputte Dateien werden stumm übersprungen, damit der
    Vector-Store-Build niemals abbricht.
    """
    root = Path(root).expanduser().resolve()
    docs: list[Document] = []
    requested_suffixes = _normalize_requested_doc_types(doc_types)

    # Verzeichnisse die übersprungen werden sollen (normalerweise keine relevanten Dokumente)
    SKIP_DIRS ={
        'venv', '.venv', '__pycache__', '.git', 'node_modules', '.micromamba', '.tools', 'AppData',
        'ai_ide_v1756.egg-info', 'venv/lib/python3.13/site-packages',
        'site-packages', '.pytest_cache', '.mypy_cache',
        'matplotlib/mpl-data/images', 
        } # matplotlib icons - meist nur Grafiken ohne Text
    

    def should_skip_path(path: Path) -> bool:
        """Prüft ob ein Pfad übersprungen werden soll"""
        path_str = str(path)  
        return any( skip_dir  in path_str for skip_dir in SKIP_DIRS)
        


    # 1) ------------ Python-Quelltexte -----------------------------------
    if requested_suffixes is None or ".py" in requested_suffixes:
        py_loader = DirectoryLoader(
            str(root),
            glob="*.py",
            loader_cls=PythonLoader,
            use_multithreading=USE_MULTITHREADING,
            show_progress=False,
        )
        py_docs = py_loader.load()
        for d in py_docs:
            d.metadata["source"] = _norm_source(d.metadata.get("source"))
            d.metadata["titel"] = _safe_title_from_source(d.metadata["source"])
            d.metadata['id'] = hex(hash(f"{d.metadata['source']}{d.page_content[:100]}"))
        docs.extend(py_docs)
    

    # 2) ------------ Reine Textformate -----------------------------------
    text_patterns: dict[str, tuple[str, ...]] = {
        ".txt": ("**/*.txt",),
        ".text": ("**/*.text",),
        ".md": ("**/*.md",),
        ".rst": ("**/*.rst",),
        ".yaml": ("**/*.yaml",),
        ".yml": ("**/*.yml",),
        ".sqlite": ("**/*.sqlite",),
        ".sqlite3": ("**/*.sqlite3",),
        ".toml": ("**/*.toml",),
    }
    selected_patterns: list[str] = []
    if requested_suffixes is None:
        for patterns in text_patterns.values():
            selected_patterns.extend(patterns)
    else:
        for suffix, patterns in text_patterns.items():
            if suffix in requested_suffixes:
                selected_patterns.extend(patterns)

    for pattern in selected_patterns:
        for path in root.rglob(pattern):
            if should_skip_path(path):
                continue
            with suppress(Exception):                      # Encoding-Fehler ignorieren
                t_loader = SafeTextLoader(str(path), autodetect_encoding=True)
                dokument = t_loader.load()
                for d in dokument:
                    d.metadata["source"] = str(path)
                    d.metadata["titel"] = path.name
                    d.metadata['id'] = hex(hash(f"{d.metadata['source']}{d.page_content[:100]}"))
                docs.extend(dokument)
    # 2) ------------ Reine Textformate -----------------------------------
   
    

    # 2.1) ------------ JSON-Dateien ---------------------------------------`
    if requested_suffixes is None or ".json" in requested_suffixes:
        docs.extend(_load_json(root))
    # 3) ------------ PDF-Dateien  (mit robustem Loader) ------------------
    pdf_count = 0
    pdf_success_count = 0
    if requested_suffixes is None or ".pdf" in requested_suffixes:
        for pdf_path in root.rglob("*.pdf"):
            if should_skip_path(pdf_path):
                continue

            pdf_count += 1
            pdf_docs = _load_pdf(pdf_path)
            if pdf_docs:
                docs.extend(pdf_docs)
                pdf_success_count += 1
                _log("", f"✓ PDF indexiert: {pdf_path} ({len(pdf_docs)} Dokumente)")
    
    _log("", f"PDF-Verarbeitung: {pdf_success_count}/{pdf_count} erfolgreich")

    # 4) ------------ Duplikate entfernen ---------------------------------
    unique: dict[str, Document] = {}
    for d in docs:
        d.metadata["source"] = _norm_source(d.metadata.get("source"))
        unique.setdefault(_document_key(d), d)
        #print(d.metadata["source"])     # Quelle als Key
    
    print("", f"Dokumente vor Duplikat-Entfernung: {len(docs)}")
    print("", f"Dokumente nach Duplikat-Entfernung: {len(unique)}")

    
    return list(unique.values())

# ───────────────────────── VectorStoreManager ──────────────────────────
class VectorStore():
    '''
    Verwaltet den kompletten Lebens­zyklus eines FAISS Vector-Stores.

    Methoden
    --------
    build(path: Path | str = DEFAULT_PROJECT_ROOT)
        Erstellt / aktualisiert den Index (inkrementell).

    query(text: str, k: int = DEFAULT_TOP_K)
        Führt eine Ähnlichkeitssuche durch und gibt Top-K Treffer auf der Konsole aus.

    wipe()
        Löscht Index + Manifest (für Neustart oder Debugging).
    '''
    # Temporärer Speicher für Dokumente
    # ------------------------------------------------------------
    doc_mem: dict[str, list[Document]] = None
    _initialized: bool = False


    def __init__(self, store_path: str = None, manifest_file: str = None, enable_monitoring: bool = True) -> None:
        # Lazy initialization - don't load embeddings until needed
        self.embeddings = None
        self.store = None
        self._gpu_resources = None
        self._gpu_index_enabled = False
        self._gpu_device = None
        self.FAISS_INDEX_PATH = store_path if store_path else FAISS_INDEX_PATH
        self.MANIFEST_FILE = manifest_file if manifest_file else MANIFEST_FILE
        self.manifest: Set[str] = self._load_manifest()
        # Performance Monitor
        self._initialized = False   

        if os.path.isdir(self.FAISS_INDEX_PATH):
            print(f'APP DIR: {self.FAISS_INDEX_PATH} OK')
        else:
            os.mkdir(self.FAISS_INDEX_PATH)
        # VectorStoreManager.doc_mem = self.load_directorys()
        print(f'ALL DOCS LENGTH: {len(self.manifest)}')
    # extract root paths from manifest and avoid duplicates
    def load_directorys(self) -> list:
        seen_path: list = [] # seen paths to avoid duplicates
        all_docs: list = []
        for d in self.manifest:
            project_path = GetPath().get_path(parg=_manifest_key_to_source_path(d), opt='p')
            #print(f'Checking path: {project_path}')
            if project_path not in seen_path:
                #print(f'Adding path: {project_path}')
                seen_path.append(project_path)
                # extend() statt append() um flache Liste zu erhalten
                all_docs.extend(_iter_documents(project_path))
                #print(f'seen path: {len(seen_path)}')
        docs_injected = self.metadata_injection(all_docs)
        return docs_injected

    def _load_manifest(self) -> Set[str]:
        """
        Liest bereits indizierte File-Pfade aus manifest.json und gibt sie
        als Set[str] zurück. Uses instance-level MANIFEST_FILE.
        """
        if not Path(self.MANIFEST_FILE).exists():
            return set()
        try:
            with open(self.MANIFEST_FILE, encoding="utf-8") as fh:
                raw_items = json.load(fh)
                # Manifest may contain mixed numeric/string entries from older runs; normalize to str.
                return {str(item) for item in raw_items}
        except Exception as err:
            _log(err, "Warnung: manifest.json defekt – wird neu aufgebaut.")
            return set()

    def _save_manifest(self, paths: Iterable[str]) -> None:
        """Saves manifest using instance-level MANIFEST_FILE."""
        normalized = [str(p) for p in paths]
        data = json.dumps(sorted(normalized, key=str), indent=2, ensure_ascii=False)
        with open(self.MANIFEST_FILE, "w", encoding="utf-8") as f:
            f.write(data)

    def _initialize(self) -> None:
        """Lazy initialization of embeddings and store."""
        if self._initialized:
            return
        self._initialized = True
        # Embeddings (CPU/GPU automatisch via HF-Transformers)
        device = _select_embeddings_device()
        try:
            # langchain-huggingface forwards model_kwargs to sentence-transformers
            self.embeddings = HuggingFaceEmbeddings(
                model_name=MODEL_NAME,
                model_kwargs={"device": device},
            )
        except TypeError:
            # Backward-compat: older versions may not accept model_kwargs.
            self.embeddings = HuggingFaceEmbeddings(model_name=MODEL_NAME)
        device: str = str(self.embeddings._client.device)
        print(  f"HuggingFace-Embeddings fuer VectorStore geladen auf Gerät: {device}")

    def _ensure_cpu_index(self) -> None:
        if self.store is None or not self._gpu_index_enabled:
            return
        faiss = _load_faiss_module()
        if not hasattr(faiss, "index_gpu_to_cpu"):
            raise RuntimeError("FAISS GPU index cannot be persisted because index_gpu_to_cpu is unavailable.")
        self.store.index = faiss.index_gpu_to_cpu(self.store.index)
        self._gpu_resources = None
        self._gpu_device = None
        self._gpu_index_enabled = False

    def _maybe_enable_gpu_index(self) -> None:
        if self.store is None or self._gpu_index_enabled or not FAISS_USE_GPU:
            return

        status = _faiss_gpu_status()
        if not status.get("available"):
            if FAISS_REQUIRE_GPU:
                raise RuntimeError(f"FAISS GPU required but unavailable: {status.get('reason', 'unknown reason')}")
            return

        faiss = _load_faiss_module()
        device = max(0, min(int(FAISS_GPU_DEVICE), int(status.get("num_gpus", 1)) - 1))
        try:
            self._gpu_resources = faiss.StandardGpuResources()
            self.store.index = faiss.index_cpu_to_gpu(self._gpu_resources, device, self.store.index)
            self._gpu_index_enabled = True
            self._gpu_device = device
            print(f"FAISS-Index für Query auf GPU aktiviert (device={device}).")
        except Exception as exc:
            self._gpu_resources = None
            self._gpu_device = None
            self._gpu_index_enabled = False
            if FAISS_REQUIRE_GPU:
                raise RuntimeError(f"FAISS GPU required but CPU→GPU transfer failed: {type(exc).__name__}: {exc}") from exc

    def _load_faiss_store(self) -> None:
        # Persistierten Index laden – falls vorhanden
        # FAISS save_local/load_local expect a DIRECTORY containing index.faiss and index.pkl 
        if self.store is not None:
            return
        store_dir = Path(self.FAISS_INDEX_PATH)
        index_faiss_file = store_dir / "index.faiss"
        if store_dir.exists() and index_faiss_file.exists():
            try:
                self.store: FAISS = FAISS.load_local(
                    str(store_dir), 
                    self.embeddings, 
                    allow_dangerous_deserialization=True
                )
                print(f"Persistierter Index geladen – Vektoren: {self.store.index.ntotal}")
            except Exception as e:
                err = str(e)
                low = err.lower()
                # Propagate missing-FAISS errors so caller code can fall back to
                # a compatible runtime (e.g., micromamba GPU env).
                if (
                    "could not import faiss" in low
                    or "no module named 'faiss'" in low
                    or 'no module named "faiss"' in low
                    or "faiss module not installed" in low
                ):
                    raise RuntimeError(err) from e
                print(f"Fehler beim Laden des Index: {err}")
                self.store = None
        else:
            self.store = None
            _log("Kein existierender Index gefunden – wird beim ausfuehren von build() erstellt.")
        return self.store

            
    def metadata_injection(self, all_docs: list[Document]) -> list[Document]:
        """Injiziert Metadata in page_content für bessere Suche."""
        new_docs: list[Document] = []
        for d in all_docs:
            d.metadata["source"] = _norm_source(d.metadata.get("source"))
            if _document_key(d) not in self.manifest:
                metadata_str = " | ".join([f"{k}: {v}" for k, v in d.metadata.items()])
                enriched_content = f"{d.page_content}\n\nMetadata: {metadata_str}"
                new_docs.append(Document(page_content=enriched_content, metadata=d.metadata))
        print(f'New doc added with metadata injection for {len(new_docs)} documents.')
        if not new_docs:
            _log("Keine neuen Dateien – Index ist aktuell.")
            return []
        return new_docs
        # Persistierten Index laden – falls vorhanden
        # FAISS save_local/load_local expect a DIRECTORY containing index.faiss and index.pkl
    # ------------------------------------------------------------
    def build(
        self,
        path: Path | str = DEFAULT_PROJECT_ROOT,
        doc_types: str | Iterable[str] | None = None,
        chunk_strategy: str | None = None,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> None:
        '''Erstellt oder erweitert den Vector-Store um neue Dateien.'''
        # Initialize embeddings if not already done
        self._initialize()
        project_root = Path(path).expanduser().resolve()
        #_log(f"Suche nach neuen Dateien in: {project_root}")
        self.all_docs = _iter_documents(project_root, doc_types=doc_types)
        for d in self.all_docs:
            d.metadata["source"] = _norm_source(d.metadata.get("source"))
        # Korrekte Implementierung für Metadata-Injektion in page_content
        new_docs = [d for d in self.all_docs if _document_key(d) not in self.manifest]
        injected_docs: list[Document] = self.metadata_injection(new_docs)
        # ------------------------------------------------------------
        _log(f"{len(injected_docs)} neue Dateien – erstelle Text-Chunks …")

        resolved_strategy, resolved_chunk_size, resolved_overlap = _resolve_chunking_config(
            chunk_strategy=chunk_strategy,
            chunk_size=chunk_size,
            overlap=overlap,
        )
        splitter = _create_text_splitter(
            chunk_strategy=resolved_strategy,
            chunk_size=resolved_chunk_size,
            overlap=resolved_overlap,
        )
        _log(
            msg=(
                f"Chunking-Konfiguration: strategy={resolved_strategy}, "
                f"chunk_size={resolved_chunk_size}, overlap={resolved_overlap}"
            )
        )
        # -------------------------------------------------------------
        chunks = splitter.split_documents(injected_docs)
        _log(f"Insgesamt {len(chunks)} Chunks generiert.")
        
        # Check if chunks is empty before creating index
        if not chunks:
            _log("Keine Chunks zum Indizieren vorhanden. Überspringe Index-Erstellung.")
            return

        self._ensure_cpu_index()
        
        # Index initial erstellen oder erweitern
        if self.store is None:
            # Embeddings (CPU/GPU automatisch via HF-Transformers)
            self.store = FAISS.from_documents(chunks, self.embeddings)
            _log("Neuer FAISS-Index erstellt.")
        elif chunks:
            self.store.add_documents(chunks)

            _log("Bestehender Index erweitert.")
        self.store.save_local(self.FAISS_INDEX_PATH)
        _log(f"Index gespeichert → {self.FAISS_INDEX_PATH}")
        # Manifest aktualisieren
        self.manifest.update(_document_key(d) for d in new_docs)
        self._save_manifest(self.manifest)
        _log("Manifest aktualisiert.")
            # ------------------------------------------------------------ 

    def query(self,query: str|None = None, 
              k: int = DEFAULT_TOP_K,
              filter_dict:dict|None = None) -> list:
        '''Lädt vollständigen Quellcode der ähnlichsten
            Chunks mit Performance-Monitoring.'''
        filter_dict = filter_dict if filter_dict is not None else {} 
        print(f'Filter Dict im Query: {filter_dict}')
        for key in filter_dict:
            print(f'Filter Key im Query: {key} ')
        results : list[tuple[Document, float]]

        self._initialize()
        self._load_faiss_store()
        if self.store is None:
            _log("Kein Index vorhanden – bitte zuerst build() ausführen.")
            return []
        self._maybe_enable_gpu_index()
        query_text = (query or "").strip()
        _log(f'Query: "{query_text}"  (Top-{k})')
        print(query_text)
        if query_text:
            # `AI_IDE_VSTORE_FETCH_K=0` means auto. Passing k=0 to FAISS always
            # returns an empty result set, which previously caused `memorydb -> []`.
            requested_k = max(1, int(k))
            fetch_k = int(VSTORE_FETCH_K) if int(VSTORE_FETCH_K) > 0 else max(requested_k, 20)

            results = self.store.similarity_search_with_score(query=query_text, k=fetch_k)
            if not results:
                print("   ↳ Keine Treffer.")
                return []

            pairs: list[tuple[Document, float]] = []
            for doc, score in results:
                src = _norm_source(doc.metadata.get("source", ""))
                print(f"   ↳ Treffer: {src} (Distance: {float(score):.4f})")
                pairs.append((doc, float(score)))
            # Rerank/diversify
            if VSTORE_RERANK and pairs and VSTORE_RERANK_METHOD == "mmr":
                try:
                    # LangChain FAISS MMR (diversity-aware selection)
                    mmr_docs = self.store.max_marginal_relevance_search(
                        query_text,
                        k=min(int(k), len(pairs)),
                        fetch_k=fetch_k
                    )
                    # Attach approximate distance score by document key (best match wins)
                    best_score_by_key: dict[str, float] = {}
                    for d, s in pairs:
                        doc_key = _document_key(d)
                        prev = best_score_by_key.get(doc_key)
                        if prev is None or float(s) < prev:
                            best_score_by_key[doc_key] = float(s)
                    pairs = [(d, best_score_by_key.get(_document_key(d), float("inf"))) for d in mmr_docs]
                except Exception:
                    # Fallback: plain distance ordering
                    pairs.sort(key=lambda x: x[1])

            if VSTORE_DEDUP and pairs:
                best_by_key: dict[str, tuple[Document, float]] = {}  
                for doc, score in pairs:
                    doc_key = _document_key(doc)
                    prev = best_by_key.get(doc_key)
                    # FAISS distance: smaller is better
                    if prev is None or score < prev[1]:
                        best_by_key[doc_key] = (doc, score)
                pairs = list(best_by_key.values())

            if VSTORE_RERANK and pairs and VSTORE_RERANK_METHOD == "crossencoder":
                ce = _get_cross_encoder()
                if ce is not None:
                    try:
                        rerank_inputs = [(query, (doc.page_content or "")[:4000]) for doc, _ in pairs]
                        rerank_scores = ce.predict(rerank_inputs)
                        pairs = [p for _, p in sorted(
                            zip(rerank_scores, pairs),
                            key=lambda x: float(x[0]),
                            reverse=True,
                        )]
                    except Exception:
                        pairs.sort(key=lambda x: x[1])
                else:
                    pairs.sort(key=lambda x: x[1])

            if not (VSTORE_RERANK and VSTORE_RERANK_METHOD in {"mmr", "crossencoder"}):
                pairs.sort(key=lambda x: x[1])

            pairs = pairs[: int(k)]

            payload: list[dict[str, Any]] = []
            for rank, (doc, score) in enumerate(pairs, 1):
                source = _norm_source(doc.metadata.get("source", ""))
                entry_ref = _document_key(doc)
                title = doc.metadata.get("titel") or _safe_title_from_source(source)
                content = (doc.page_content or "").strip()
                if VSTORE_MAX_CONTENT_CHARS > 0 and len(content) > VSTORE_MAX_CONTENT_CHARS:
                    content = content[:VSTORE_MAX_CONTENT_CHARS] + "\n…[truncated]"
                item: dict[str, Any] = {
                    "rank": rank,
                    "distance": float(score),
                    "score": float(score),
                    "score_kind": "faiss_distance",
                    "source": source,
                    "entry_ref": entry_ref,
                    "source_key": doc.metadata.get("source-key"),
                    "title": title,
                    "page": doc.metadata.get("page"),
                    "content": content,
                    "metadata": doc.metadata
                }
                if VSTORE_INCLUDE_METADATA:
                    item["metadata"] = dict(doc.metadata)
                payload.append(item)
            return payload
            # Fllbk: exact filename match in docstore (useful when the query is a filename)
        return []
    
        """
            for itms in payload:
                match = True
                for ky in filter_dict:
                    if ky in itms.metadata:
                        if str(itms.metadata[ky]) != str(filter_dict[ky]):
                            match = False
                            break
                    else:
                        match = False
                        break
                if match:
                    full_content.append(itms)
            print(full_content)
            return payload, full_content"""
               


"""
               
               #payload = itms.metadata[u
         
# ──────            ───────────────────────── CLI ────────────────────────────────
# [data['title','content'] for data in payload
def _bui            ld_argparser() -> argparse.ArgumentParser:
pars            er = argparse.ArgumentParser(
        prog="vector_store_manager",.
        description="Erstellt und durchsucht einen FAISS Vector-Store.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # build
    p_build = sub.add_parser("build", help="Index erstellen / erweitern")
    p_build.add_argument(
        "--path",
        default=str(DEFAULT_PROJECT_ROOT),
        help="Projekt-Root (default: aktuelles Verzeichnis)",
    )

    # query
    p_query = sub.add_parser("query", help="Anfrage an den Vector-Store")
    p_query.add_argument("text", help="Query-Text / Suchbegriff")
    p_query.add_argument(
        "-k", "--top_k", type=int, default=DEFAULT_TOP_K, help="Anzahl der Treffer"
    )

    # wipe
    sub.add_parser("wipe", help="Index + Manifest löschen (Reset)")

    return parser

def _main() -> None:
    args = _build_argparser().parse_args()
    vs = VectorStoreManager()

    match args.cmd:
        case "build":
            vs.build(args.path)
        case "query":
            vs.query(args.text, k=args.top_k)
        case "wipe":
            vs.wipe()
        case _:  # pragma: no cover
            raise RuntimeError("Unbekannter Befehl")

               itms.metadata[ky] = filter_dict[m][ky]

if __name__ == "__main__":
    # Ermöglicht:  python -m vector_store_manager <cmd> [...]
     _main()
"""

# usage eg.
# DEFAULT_PROJECT_ROOT:list|str = GetPath().get_path(f"home ben Applications Job_offers",opt="s")#
 # Index erstellen / erweitern

#def vsm_query(text: str, k:int) -> list:/VSM_4_Data/' '**.pdf')
#store_path:GetPath=GetPath()._parent( parg = f"{__file__}"        ) + "AppData/VSM_4_Data/"
#manifest_file:GetPath= GetPath()._parent(parg = f"{__file__}") + "AppData/VSM_4_Data/manifest.json"
#VectorStore(store_path   , manifest_file).build()
#vsm_application.query(text,k=k=
#text:dict[str,dict]|strvsmvsm
#query = "8 punkte learnig path fuer RAG/AI/ML mit LangChain Torch, "
#filterquery_dict= {'mkeys': {'titel':'test_dict.py'},'rkeys':{'id','source','applied'},'ukeys':{'updated':'08.01.2026'}}
'''
k: int= 20
qy: str= "Stellenauschreibung für KI-Entwickler im Bereich RAG mit LangChain und PyTorch, idealerweise mit Fokus auf Vektor-Datenbanken"
path: str= "AppData/VSM_3_Data/"

store_path: GetPath = GetPath()._parent(parg = f"{__file__}") + f"AppData/VSM_3_Data/"
manifest_file: GetPath = store_path + "manifest.json"
vsm: VectorStore = VectorStore(store_path=store_path, manifest_file=manifest_file)
vsm.build(GetPath()._parent(parg = Path(store_path)))
# Example query with filter - replace with your own PDF path:(()
doc = vsm.query(query=qy, k=k)

for itm in doc:

    print(f"Rank: { itm.metadata['rank']}, Score: {itm['score']:.4f}, Title: {itm['title']}, Source: {itm['source']}")
    print(f"Content Preview: {itm['content'][:200]}...\n")
'''