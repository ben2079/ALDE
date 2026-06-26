from __future__ import annotations



import ast
import colorsys
import hashlib
import json
import logging
import math
import os
import re
import socket
import socketserver
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, quote, unquote, urlparse


_AGENTSDB_SOCKET_SERVER_LOCK = threading.RLock()
_AGENTSDB_SOCKET_SERVER_STATE: dict[tuple[str, int], dict[str, Any]] = {}
_LOGGER = logging.getLogger(__name__)
_AGENTSDB_CONNECTION_CONFIG_CACHE: dict[str, Any] | None = None
UTC = timezone.utc
_AGENTSDB_SOCKET_URI_PATTERN = re.compile(
    r"^(?:agents?db)(?:://|::)?(?P<host>\[[^\]]+\]|:::+1|::1|[A-Za-z0-9._-]+)?(?::(?P<port>\d+))?(?::*)?$",
    re.IGNORECASE,
)


def _load_json_object_file(path: Path) -> dict[str, Any]:
    try:
        loaded_payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(loaded_payload) if isinstance(loaded_payload, Mapping) else {}


def _load_agentsdb_connection_config() -> dict[str, Any]:
    global _AGENTSDB_CONNECTION_CONFIG_CACHE
    if _AGENTSDB_CONNECTION_CONFIG_CACHE is not None:
        return dict(_AGENTSDB_CONNECTION_CONFIG_CACHE)

    env_config_path = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_CONFIG_PATH", "")).strip()
    candidate_paths: list[Path] = []
    if env_config_path:
        raw_path = Path(env_config_path)
        if raw_path.is_absolute():
            candidate_paths.append(raw_path)
        else:
            base_paths = [Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[1]]
            candidate_paths.extend((base_path / raw_path).resolve() for base_path in base_paths)
    else:
        project_root = Path(__file__).resolve().parents[2]
        package_root = Path(__file__).resolve().parents[1]
        candidate_paths.extend(
            [
                (project_root / "AppData" / "agentsdb_connection.json").resolve(),
                (package_root / "AppData" / "agentsdb_connection.json").resolve(),
            ]
        )

    config_payload: dict[str, Any] = {}
    for path in candidate_paths:
        if not path.exists() or not path.is_file():
            continue
        config_payload = _load_json_object_file(path)
        if config_payload:
            break

    _AGENTSDB_CONNECTION_CONFIG_CACHE = dict(config_payload)
    return dict(_AGENTSDB_CONNECTION_CONFIG_CACHE)


def _connection_config_value(config_payload: Mapping[str, Any], key_candidates: Sequence[str]) -> str:
    for key_name in key_candidates:
        value = config_payload.get(str(key_name))
        if value is None:
            continue
        normalized_value = str(value).strip()
        if normalized_value:
            return normalized_value
    return ""


def _env_or_config_value(
    env_name: str,
    config_payload: Mapping[str, Any],
    key_candidates: Sequence[str],
    default: str = "",
) -> str:
    env_value = str(os.getenv(env_name, "")).strip()
    if env_value:
        return env_value
    config_value = _connection_config_value(config_payload, key_candidates)
    if config_value:
        return config_value
    return str(default or "").strip()


def _env_or_config_int_value(
    env_name: str,
    config_payload: Mapping[str, Any],
    key_candidates: Sequence[str],
    default: int,
) -> int:
    raw_value = _env_or_config_value(env_name, config_payload, key_candidates, str(default))
    try:
        return int(raw_value)
    except Exception:
        return int(default)


def _compose_agentsdb_socket_uri(host: str, port: int) -> str:
    normalized_host = str(host or "").strip().strip("[]") or ""
    resolved_port = max(1, int(port or 0))
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return f"agentsdb://{normalized_host}:{resolved_port}"


def normalize_agentsdb_socket_uri(
    uri: Any,
    *,
    default_host: str = "",
    default_port: int = "",
    default_on_empty: bool = True,
) -> str:
    normalized_uri = str(uri or "").strip()
    if not normalized_uri:
        return _compose_agentsdb_socket_uri(default_host, default_port) if default_on_empty else ""

    loose_match = _AGENTSDB_SOCKET_URI_PATTERN.match(normalized_uri)
    if loose_match is not None:
        resolved_host = str(loose_match.group("host") or "").strip().strip("[]").lower()
        resolved_port_text = str(loose_match.group("port") or "").strip()
        try:
            resolved_port = int(resolved_port_text or default_port)
        except Exception:
            resolved_port = int(default_port)
        if resolved_host in {"", "", "", "::1", ":::1"}:
            resolved_host = str(default_host or "").strip().strip("[]") or ""
        return _compose_agentsdb_socket_uri(resolved_host or default_host, resolved_port)

    parsed_uri = urlparse(normalized_uri)
    if str(parsed_uri.scheme or "").strip().lower() != "agentsdb":
        return normalized_uri
    try:
        resolved_port = int(parsed_uri.port or default_port)
    except Exception:
        return _compose_agentsdb_socket_uri(default_host, default_port)
    resolved_host = str(parsed_uri.hostname or "").strip().strip("[]").lower()
    if resolved_host in {"", "", "", "::1", ":::1"}:
        resolved_host = str(default_host or "").strip().strip("[]") or ""
    return _compose_agentsdb_socket_uri(resolved_host or default_host, resolved_port)


def _load_agentsdb_socket_endpoint(
    uri: Any,
    *,
    default_host: str = "",
    default_port: int = "",
) -> tuple[str, str, int] | None:
    normalized_uri = normalize_agentsdb_socket_uri(
        uri,
        default_host=default_host,
        default_port=default_port,
        default_on_empty=False,
    )
    if not normalized_uri:
        return None
    parsed_uri = urlparse(normalized_uri)
    if str(parsed_uri.scheme or "").strip().lower() != "agentsdb":
        return None
    return (
        normalized_uri,
        str(parsed_uri.hostname or default_host).strip() or default_host,
        int(parsed_uri.port or default_port),
    )


def _load_agentsdb_uri_from_connection_config(config_payload: Mapping[str, Any]) -> str:
    configured_uri = _connection_config_value(config_payload, ("agents_db_uri", "agentsdb_uri", "uri", "socket_uri"))
    if configured_uri:
        return normalize_agentsdb_socket_uri(configured_uri, default_on_empty=False) or configured_uri
    host_value = _connection_config_value(config_payload, ("host", "hostname")) or ""
    port_value = _connection_config_value(config_payload, ("port",)) or ""
    try:
        resolved_port = int(port_value)
    except Exception:
        resolved_port = 2331
    return normalize_agentsdb_socket_uri(
        f"agentsdb://{host_value}:{resolved_port}",
        default_on_empty=False,
    ) or _compose_agentsdb_socket_uri(host_value, resolved_port)


def _is_true_env(value: str | None, default: bool = True) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return bool(default)
    return normalized not in {"0", "false", "no", "off"}


def _is_local_socket_host(host: str) -> bool:
    normalized_host = str(host or "").strip().lower()
    return normalized_host in {"127.0.0.1", "localhost", "::1"}


def _socket_endpoint_reachable(host: str, port: int, timeout_seconds: float) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=max(float(timeout_seconds), 0.2)):
            return True
    except Exception:
        return False


def _ensure_local_agentsdb_socket_server(agents_db_uri: str, timeout_seconds: float = 3.0) -> bool:
    connection_config = _load_agentsdb_connection_config()
    auto_start_value = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_AUTO_START", "")).strip()
    if not auto_start_value:
        auto_start_value = _connection_config_value(connection_config, ("auto_start", "autostart", "socket_auto_start"))
    if not _is_true_env(auto_start_value, default=True):
        return False
    endpoint = _load_agentsdb_socket_endpoint(agents_db_uri)
    if endpoint is None:
        return False
    _normalized_uri, resolved_host, resolved_port = endpoint
    if not _is_local_socket_host(resolved_host):
        return False
    if _socket_endpoint_reachable(resolved_host, resolved_port, timeout_seconds):
        return True

    server_key = (resolved_host, resolved_port)
    with _AGENTSDB_SOCKET_SERVER_LOCK:
        server_state = _AGENTSDB_SOCKET_SERVER_STATE.get(server_key)
        if server_state is not None:
            server_thread = server_state.get("thread")
            if isinstance(server_thread, threading.Thread) and server_thread.is_alive():
                pass
            else:
                _AGENTSDB_SOCKET_SERVER_STATE.pop(server_key, None)
                server_state = None
        if server_state is None:
            try:
                service = AgentDbSocketServerService.load_from_env()
                socket_server = _AgentDbSocketTCPServer((resolved_host, resolved_port), _AgentDbSocketRequestHandler, service)
            except Exception as exc:
                _LOGGER.warning(
                    "agentsdb auto-start failed during server setup for %s:%s (%s: %s)",
                    resolved_host,
                    resolved_port,
                    type(exc).__name__,
                    exc,
                )
                return _socket_endpoint_reachable(resolved_host, resolved_port, timeout_seconds)

            server_thread = threading.Thread(target=socket_server.serve_forever, name=f"agentsdb-socket:{resolved_host}:{resolved_port}", daemon=True)
            server_thread.start()
            _AGENTSDB_SOCKET_SERVER_STATE[server_key] = {
                "server": socket_server,
                "thread": server_thread,
            }
            _LOGGER.info(
                "agentsdb auto-start: started local socket server on %s:%s",
                resolved_host,
                resolved_port,
            )

    deadline = time.monotonic() + max(float(timeout_seconds), 0.5)
    while time.monotonic() < deadline:
        if _socket_endpoint_reachable(resolved_host, resolved_port, timeout_seconds=0.25):
            return True
        time.sleep(0.05)
    return _socket_endpoint_reachable(resolved_host, resolved_port, timeout_seconds=0.25)

def _is_agentsdb_socket_uri(uri: str | None) -> bool:
    if not str(uri or "").strip():
        return False
    normalized_uri = normalize_agentsdb_socket_uri(uri, default_on_empty=False)
    return str(normalized_uri or "").strip().lower().startswith("agentsdb://")


def _json_safe_object(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe_object(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe_object(item) for item in value]
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _deepcopy_object(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _deepcopy_object(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deepcopy_object(item) for item in value]
    return value


def _dataclass_payload(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _dataclass_payload(asdict(value))
    if isinstance(value, dict):
        return {str(key): _dataclass_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dataclass_payload(item) for item in value]
    return value


def _normalize_document_object_name(obj_name: str | None) -> str:
    normalized_obj_name = str(obj_name or "document").strip().lower().replace("-", "_")
    alias_map = {
        "job_postings": "job_posting",
        "profiles": "profile",
        "cover_letters": "cover_letter",
        "documents": "document",
    }
    if normalized_obj_name in alias_map:
        return alias_map[normalized_obj_name]
    if normalized_obj_name.endswith("ies"):
        return f"{normalized_obj_name[:-3]}y"
    if normalized_obj_name.endswith("s") and len(normalized_obj_name) > 1:
        return normalized_obj_name[:-1]
    return normalized_obj_name or "document"


def _stable_sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _first_non_empty_string(values: Iterable[Any]) -> str | None:
    for value in values:
        if isinstance(value, str):
            normalized_value = value.strip()
            if normalized_value:
                return normalized_value
    return None


def _first_number(values: Iterable[Any]) -> float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            normalized_value = value.strip()
            if not normalized_value:
                continue
            try:
                return float(normalized_value)
            except Exception:
                continue
    return None


def _normalize_limit_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        normalized_value = int(value)
    except Exception:
        return None
    if normalized_value <= 0:
        return None
    return normalized_value


def _mapping_value(payload: Mapping[str, Any], key: str) -> Any:
    current: Any = payload
    for segment in str(key or "").split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _slugify_object_name(value: Any, *, fallback_prefix: str = "value") -> str:
    normalized_value = str(value or "").strip().lower()
    slug_value = re.sub(r"[^a-z0-9]+", "_", normalized_value).strip("_")
    if slug_value:
        return slug_value
    return f"{fallback_prefix}_{_stable_sha256(str(value or ''))[:12]}"


def _load_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized_value = value.strip()
        return [normalized_value] if normalized_value else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key in ("name", "label", "value", "title", "text"):
            values.extend(_load_string_list(value.get(key)))
        return values
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values: list[str] = []
        for item in value:
            values.extend(_load_string_list(item))
        return values
    normalized_value = str(value).strip()
    return [normalized_value] if normalized_value else []


def _load_bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if normalized_value in {"true", "1", "yes", "ja", "remote"}:
            return True
        if normalized_value in {"false", "0", "no", "nein"}:
            return False
    return None


def _normalize_pattern_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _load_type_key_from_pattern(
    value: Any,
    *,
    fallback_type_key: str,
    type_key_pattern_map: Mapping[str, Sequence[str]] | None = None,
) -> str:
    normalized_value = _normalize_pattern_key(value)
    if not normalized_value:
        return fallback_type_key
    for type_key, pattern_value_list in dict(type_key_pattern_map or {}).items():
        for pattern_value in pattern_value_list:
            if normalized_value == _normalize_pattern_key(pattern_value):
                return str(type_key).strip() or fallback_type_key
    return fallback_type_key


def _load_type_key_alias(value: Any) -> str:
    normalized_value = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if not normalized_value:
        return ""
    return TYPE_KEY_ALIAS_MAP.get(normalized_value, normalized_value)


def _load_type_key_from_explicit_value(
    value: Any,
    *,
    fallback_type_key: str,
    type_key_pattern_map: Mapping[str, Sequence[str]] | None = None,
) -> str:
    normalized_value = _load_type_key_alias(value)
    if not normalized_value:
        return fallback_type_key
    for type_key in dict(type_key_pattern_map or {}).keys():
        if _normalize_pattern_key(type_key) == _normalize_pattern_key(normalized_value):
            return str(type_key).strip() or fallback_type_key
    return fallback_type_key


def _load_typed_collection_value_list(value: Any) -> list[tuple[str, str | None]]:
    typed_value_list: list[tuple[str, str | None]] = []

    def _append(value_name: Any, value_type: Any = None) -> None:
        normalized_name = str(value_name or "").strip()
        if not normalized_name:
            return
        normalized_type = str(value_type or "").strip() or None
        typed_value_list.append((normalized_name, normalized_type))

    def _collect(source_value: Any) -> None:
        if source_value is None:
            return
        if isinstance(source_value, str):
            _append(source_value)
            return
        if isinstance(source_value, Mapping):
            value_name = _first_non_empty_string(
                [
                    source_value.get("canonical_name"),
                    source_value.get("name"),
                    source_value.get("label"),
                    source_value.get("value"),
                    source_value.get("title"),
                    source_value.get("text"),
                    source_value.get("tool"),
                    source_value.get("skill"),
                    source_value.get("technology"),
                ],
            )
            value_type = _first_non_empty_string(
                [
                    source_value.get("type_key"),
                    source_value.get("entity_type"),
                    source_value.get("type"),
                    source_value.get("category"),
                    source_value.get("kind"),
                    source_value.get("classification"),
                    source_value.get("role"),
                    source_value.get("tag"),
                    source_value.get("group"),
                ],
            )
            if value_name:
                _append(value_name, value_type)
                return
            for nested_key in ("name", "label", "value", "title", "text", "tool", "skill", "technology"):
                _collect(source_value.get(nested_key))
            return
        if isinstance(source_value, Sequence) and not isinstance(source_value, (str, bytes, bytearray)):
            for nested_value in source_value:
                _collect(nested_value)
            return
        _append(source_value)

    _collect(value)

    unique_value_list: list[tuple[str, str | None]] = []
    existing_value_index_by_key: dict[str, int] = {}
    for value_name, value_type in typed_value_list:
        value_key = _normalize_pattern_key(value_name)
        if not value_key:
            continue
        if value_key in existing_value_index_by_key:
            existing_index = existing_value_index_by_key[value_key]
            existing_name, existing_type = unique_value_list[existing_index]
            if existing_type is None and value_type is not None:
                unique_value_list[existing_index] = (existing_name, value_type)
            continue
        existing_value_index_by_key[value_key] = len(unique_value_list)
        unique_value_list.append((value_name, value_type))
    return unique_value_list


def _build_namespace_object_from_runtime_config(
    runtime_config: RuntimeConfigObject,
    *,
    handoff_metadata: Mapping[str, Any] | None = None,
    handoff_payload: Mapping[str, Any] | None = None,
) -> NamespaceObject:
    tenant_id = str(
        (handoff_payload or {}).get("tenant_id")
        or (handoff_metadata or {}).get("tenant_id")
        or runtime_config.tenant_id
    ).strip() or runtime_config.tenant_id
    namespace_id = str(
        (handoff_payload or {}).get("knowledge_namespace_id")
        or (handoff_metadata or {}).get("knowledge_namespace_id")
        or runtime_config.namespace_id
    ).strip() or runtime_config.namespace_id
    namespace_slug = str(
        (handoff_payload or {}).get("knowledge_namespace_slug")
        or (handoff_metadata or {}).get("knowledge_namespace_slug")
        or runtime_config.namespace_slug
    ).strip() or runtime_config.namespace_slug
    namespace_name = str(
        (handoff_payload or {}).get("knowledge_namespace_name")
        or (handoff_metadata or {}).get("knowledge_namespace_name")
        or runtime_config.namespace_name
    ).strip() or runtime_config.namespace_name
    return NamespaceObject(
        id=namespace_id,
        tenant_id=tenant_id,
        slug=namespace_slug,
        name=namespace_name,
        description="Optional MongoDB mirror of the ALDE document pipeline.",
        index_backend=runtime_config.index_backend,
        default_embedding_model=runtime_config.default_embedding_model,
        default_embedding_dimension=runtime_config.default_embedding_dimension,
        metadata={"source": "alde_document_pipeline"},
    )


def _demo_dataset_timestamp() -> datetime:
    return datetime.now(tz=UTC)


def _demo_embedding_vector(seed: str, dimension: int = 8) -> list[float]:
    digest_source = str(seed or "demo-seed").encode("utf-8")
    vector: list[float] = []
    while len(vector) < max(1, int(dimension)):
        digest = hashlib.sha256(digest_source).digest()
        for byte in digest:
            vector.append(round((float(byte) / 127.5) - 1.0, 6))
            if len(vector) >= max(1, int(dimension)):
                break
        digest_source = digest
    return vector


@dataclass(slots=True)
class NamespaceObject:
    id: str
    tenant_id: str
    slug: str
    name: str
    default_embedding_model: str
    default_embedding_dimension:  int
    description: str = ""
    index_backend: str = "faiss"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class EntityAliasObject:
    alias: str
    alias_type: str = "synonym"
    locale: str | None = None
    confidence: float = 1.0
    source_document_id: str | None = None
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class EntityObject:
    id: str
    tenant_id: str
    namespace_id: str
    entity_type: str
    canonical_name: str
    external_key: str | None = None
    correlation_id: str | None = None
    status: str = "active"
    summary: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    aliases: list[EntityAliasObject] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class EntityMentionObject:
    entity_id: str
    mention_text: str
    extractor: str = "manual"
    confidence: float = 1.0
    char_start: int | None = None
    char_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class BlockObject:
    block_id: str
    block_no: int
    content: str
    block_kind: str = "chunk"
    heading: str | None = None
    token_count: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    parent_block_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    mentions: list[EntityMentionObject] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class DocumentObject:
    id: str
    tenant_id: str
    namespace_id: str
    document_type: str
    title: str
    source_uri: str
    content_sha256: str
    source_system: str = "local"
    mime_type: str = "text/plain"
    language_code: str | None = None
    correlation_id: str | None = None
    author_entity_id: str | None = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[BlockObject] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class RelationEvidenceObject:
    block_id: str
    evidence_role: str = "supporting"
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class EntityRelationObject:
    id: str
    tenant_id: str
    namespace_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    direction: str = "directed"
    weight: float = 1.0
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[RelationEvidenceObject] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class EmbeddingObject:
    tenant_id: str
    namespace_id: str
    model_id: str
    owner_type: str
    owner_id: str
    content_sha256: str
    dimension: int
    index_namespace: str
    index_item_key: str
    chunk_hash: str | None = None
    embedding: list[float] | None = None
    index_backend: str = "faiss"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class RetrievalResultObject:
    rank_no: int
    result_type: str
    result_id: str
    source_stage: str
    chosen: bool = True
    lexical_score: float | None = None
    vector_score: float | None = None
    graph_score: float | None = None
    rerank_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalRunObject:
    id: str
    tenant_id: str
    namespace_id: str
    query_text: str
    requested_k: int
    lexical_k: int | None = None
    graph_hops: int | None = None
    vector_k: int | None = None
    rerank_strategy: str = "none"
    correlation_id: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    results: list[RetrievalResultObject] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class DispatcherRunObject:
    id: str
    tenant_id: str
    namespace_id: str
    correlation_id: str
    processing_state: str
    processed: bool
    failed_reason: str | None = None
    source_system: str = "alde_dispatcher"
    dispatcher_db_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


@dataclass(slots=True)
class RuntimeConfigObject:
    agents_db_uri: str
    database_name: str = "alde_knowledge"
    tenant_id: str = "tenant_default"
    namespace_id: str = "ns_alde_default"
    namespace_slug: str = "alde-default"
    namespace_name: str = "ALDE Default Knowledge"
    default_embedding_model: str = "text-embedding-3-large"
    default_embedding_dimension: int = 3072
    index_backend: str = "cosine"

    @property
    def mongo_uri(self) -> str:
        return self.agents_db_uri

    @mongo_uri.setter
    def mongo_uri(self, value: str) -> None:
        self.agents_db_uri = str(value)


@dataclass(slots=True)
class MappingBlockSeedObject:
    section_key: str
    block_id: str
    block_no: int
    heading: str
    content: str
    block_kind: str = "chunk"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MappingSeedEntityObject:
    seed_key: str
    type_key: str
    canonical_name: str
    section_key: str | None = None
    relation_type_key: str | None = None
    is_target: bool = False
    source_seed_key: str | None = None
    relation_description: str | None = None
    confidence: float = 0.95
    mention_text: str | None = None
    summary: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


TECHNICAL_TYPE_KEY_PATTERN_MAP: dict[str, tuple[str, ...]] = {
    "tool": (
        "jira",
        "topdesk",
        "servicenow",
        "git",
        "github",
        "gitlab",
        "docker",
        "kubernetes",
        "k8s",
        "terraform",
        "ansible",
        "jenkins",
        "postman",
        "confluence",
        "slack",
        "teams",
        "notion",
        "sap",
        "salesforce",
    ),
    "framework": ("itil", "scrum", "kanban", "fastapi", "django", "flask", "spring", "react", "angular", "vue"),
    "database": ("postgresql", "postgres", "oracle", "mysql", "mongodb", "sqlite", "mssql", "sql server"),
    "protocol": ("tcp/ip", "tcpip", "http", "https", "http(s)", "rdp", "ssh", "rest", "graphql", "mqtt"),
}


TYPE_KEY_ALIAS_MAP: dict[str, str] = {
    "tool": "tool",
    "tools": "tool",
    "tooling": "tool",
    "technology": "tool",
    "technologies": "tool",
    "tech": "tool",
    "tech_stack": "tool",
    "skill": "skill",
    "skills": "skill",
    "hard_skill": "skill",
    "hard_skills": "skill",
    "technical_skill": "skill",
    "technical_skills": "skill",
    "framework": "framework",
    "frameworks": "framework",
    "methodology": "framework",
    "methodologies": "framework",
    "database": "database",
    "databases": "database",
    "db": "database",
    "protocol": "protocol",
    "protocols": "protocol",
    "competency": "competency",
    "competencies": "competency",
    "soft_skill": "competency",
    "soft_skills": "competency",
    "language": "language",
    "languages": "language",
    "lang": "language",
}


OBJECT_MAPPING_PATTERN_BY_NAME: dict[str, dict[str, Any]] = {
    "job_posting": {
        "subject_pattern": {
            "seed_key": "subject",
            "type_key": "job_posting",
            "section_key": "header",
            "value_path_list": ("job_title", "title"),
            "summary": "Primary object mapped from the parsed result.",
            "attribute_path_map": {
                "position_type": "position.type",
                "position_level": "position.level",
                "department": "position.department",
                "remote": "location_details.remote",
            },
        },
        "section_pattern_list": [
            {
                "section_key": "header",
                "heading": "Object Header",
                "section_type_key": "header",
                "block_kind": "section",
                "field_line_list": [
                    {"label": "Title", "path": "job_title"},
                    {"label": "Organization", "path": "company_name"},
                    {"label": "Location", "path_list": ("company_info.location", "location_details.office")},
                    {"label": "Object Type", "path": "position.type"},
                    {"label": "Object Level", "path": "position.level"},
                    {"label": "Department", "path": "position.department"},
                ],
            },
            {
                "section_key": "requirements",
                "heading": "Requirements",
                "section_type_key": "requirements",
                "field_line_list": [
                    {"label": "Education", "path": "requirements.education"},
                    {"label": "Experience Years", "path": "requirements.experience_years"},
                    {"label": "Experience", "path": "requirements.experience_description"},
                ],
                "group_line_list": [
                    {"label": "Technical Skills", "path": "requirements.technical_skills"},
                    {"label": "Tools", "path": "requirements.tools"},
                    {"label": "Soft Skills", "path": "requirements.soft_skills"},
                    {"label": "Languages", "path": "requirements.languages"},
                ],
            },
            {
                "section_key": "responsibilities",
                "heading": "Responsibilities",
                "section_type_key": "responsibilities",
                "group_line_list": [
                    {"label": "Responsibilities", "path": "responsibilities", "emit_label_only_when_items": False},
                ],
            },
            {
                "section_key": "offer",
                "heading": "Offer",
                "section_type_key": "offer",
                "group_line_list": [
                    {"label": "Benefits", "path": "compensation.benefits"},
                    {"label": "What We Offer", "path": "what_we_offer"},
                ],
            },
            {
                "section_key": "application",
                "heading": "Application",
                "section_type_key": "application",
                "field_line_list": [
                    {"label": "Deadline", "path": "application.deadline"},
                    {"label": "Application Link", "path": "application.application_link"},
                    {"label": "Contact Email", "path": "application.contact_email"},
                    {"label": "Contact Person", "path": "application.contact_person"},
                ],
            },
        ],
        "entity_pattern_list": [
            {
                "seed_key": "organization",
                "type_key": "organization",
                "section_key": "header",
                "relation_type_key": "offered_by",
                "value_path_list": ("company_name",),
                "source_field": "company_name",
                "summary": "Organization associated with the mapped object.",
                "attribute_path_map": {
                    "industry": "company_info.industry",
                    "size": "company_info.size",
                    "website": "company_info.website",
                },
            },
            {
                "seed_key": "location",
                "type_key": "location",
                "section_key": "header",
                "relation_type_key": "located_in",
                "value_path_list": ("company_info.location", "location_details.office"),
                "source_field": "company_info.location",
                "summary": "Location associated with the mapped object.",
                "attribute_path_map": {
                    "office": "location_details.office",
                    "remote": "location_details.remote",
                    "travel_required": "location_details.travel_required",
                },
            },
            {
                "seed_key": "employment_type",
                "type_key": "employment_type",
                "section_key": "header",
                "relation_type_key": "employment_type",
                "value_path_list": ("position.type",),
                "source_field": "position.type",
                "summary": "Employment type associated with the mapped object.",
            },
            {
                "seed_key": "contact_person",
                "type_key": "person",
                "section_key": "application",
                "relation_type_key": "application_contact",
                "value_path_list": ("application.contact_person",),
                "source_field": "application.contact_person",
                "summary": "Contact person associated with the application flow.",
            },
        ],
        "collection_entity_pattern_list": [
            {
                "seed_key_prefix": "technical_requirement",
                "section_key": "requirements",
                "collection_path_list": (
                    "requirements.technical_skills",
                    "requirements.tools",
                    "requirements.tooling",
                    "requirements.technologies",
                    "tools",
                ),
                "fallback_type_key": "skill",
                "type_key_pattern_map": TECHNICAL_TYPE_KEY_PATTERN_MAP,
                "relation_type_key_map": {
                    "skill": "requires_skill",
                    "tool": "requires_tool",
                    "framework": "requires_framework_knowledge",
                    "database": "requires_database_knowledge",
                    "protocol": "requires_protocol_knowledge",
                },
                "summary_prefix": "Technical capability associated with the mapped object.",
            },
            {
                "seed_key_prefix": "competency_requirement",
                "section_key": "requirements",
                "collection_path": "requirements.soft_skills",
                "source_field": "requirements.soft_skills",
                "fallback_type_key": "competency",
                "relation_type_key_map": {
                    "competency": "requires_competency",
                },
                "summary_prefix": "Behavioral capability associated with the mapped object.",
            },
            {
                "seed_key_prefix": "language_requirement",
                "section_key": "requirements",
                "collection_path": "requirements.languages",
                "source_field": "requirements.languages",
                "fallback_type_key": "language",
                "relation_type_key_map": {
                    "language": "requires_language",
                },
                "summary_prefix": "Language capability associated with the mapped object.",
            },
        ],
    },
}

class KnowledgeRepository():
    """Knowledge repository mirroring the ALDE hybrid knowledge model."""
    _OBJECT_COLLECTION_MAP = {
        "namespace": "knowledge_namespaces",
        "entity": "entities",
        "document": "documents",
        "relation": "entity_relations",
        "embedding": "embeddings",
        "retrieval_run": "retrieval_runs",
        "dispatcher_run": "dispatcher_runs",
    }

    def __init__(self, database: Mapping[str, Any] | None = None, *, image_path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._image_path = str(image_path or "").strip() or None
        self._collections: dict[str, dict[str, dict[str, Any]]] = {
            collection_name: {}
            for collection_name in self._OBJECT_COLLECTION_MAP.values()
        }
        self._index_objects: dict[str, Any] = {}
        self._load_from_mapping(database)
        self._load_image()

    def _load_from_mapping(self, database: Mapping[str, Any] | None) -> None:
        if not isinstance(database, Mapping):
            return
        collections_payload = database.get("collections") if isinstance(database.get("collections"), Mapping) else database
        if not isinstance(collections_payload, Mapping):
            return
        for collection_name, collection_payload in collections_payload.items():
            normalized_collection_name = str(collection_name or "").strip()
            if normalized_collection_name not in self._collections:
                continue
            if not isinstance(collection_payload, Mapping):
                continue
            self._collections[normalized_collection_name] = {
                str(record_id): dict(record_payload)
                for record_id, record_payload in collection_payload.items()
                if isinstance(record_payload, Mapping)
            }

    def _load_image(self) -> None:
        if not self._image_path:
            return
        path = os.path.abspath(os.path.expanduser(self._image_path))
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as image_file:
                image_payload = json.load(image_file)
        except Exception:
            return
        collections_payload = image_payload.get("collections") if isinstance(image_payload, Mapping) else None
        if not isinstance(collections_payload, Mapping):
            return
        self._load_from_mapping({"collections": collections_payload})

    def _flush_image(self) -> None:
        if not self._image_path:
            return
        path = os.path.abspath(os.path.expanduser(self._image_path))
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._lock:
            payload = {
                "schema": "agentsdb_repository_image_v1",
                "updated_at": _now_utc().isoformat(),
                "collections": _json_safe_object(self._collections),
                "index_objects": _json_safe_object(self._index_objects),
            }
        temp_path = f"{path}.{threading.get_ident()}.{time.time_ns()}.tmp"
        with open(temp_path, "w", encoding="utf-8") as image_file:
            json.dump(payload, image_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)

    @classmethod
    def create_from_uri(cls, agents_db_uri: str, database_name: str = "alde_knowledge") -> KnowledgeRepository:
        _ = (agents_db_uri, database_name)
        image_path = str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_MEMORY_IMAGE_PATH", "")
            or os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_FLUSH_IMAGE_PATH", "")
            or os.path.join("AppData", "agentsdb.json")
        ).strip()
        return cls(image_path=image_path)

    def load_collection(self, object_name: str) -> dict[str, dict[str, Any]]:
        collection_name = self._OBJECT_COLLECTION_MAP[str(object_name).strip().lower()]
        return self._collections[collection_name]

    def ensure_index_objects(self) -> None:
        self._index_objects["knowledge_namespaces"] = {
        "slug":1,
            "unique": True,
            "name": "uq_knowledge_namespaces_tenant_slug",
        }
        self._index_objects["entities_unique"] = {
           "namespace_id": 1, 
           "entity_type": 1, 
           "canonical_name": 1,
            "unique": True,
            "name": "uq_entities_namespace_type_name",
        }

        self._index_objects["entities_text"] = {
          "canonical_name": "text", "summary": "text", "aliases.alias": "text",
            "default_language": "none",
            "name": "fts_entities",
        }
        self._index_objects["documents_unique"] = {
            "namespace_id": 1, "content_sha256": 1,
            "unique": True,
            "name": "uq_documents_namespace_sha",
        }
        self._index_objects["documents_text"] = {
          "title": "text", "summary": "text", "blocks.heading": "text", "blocks.content": "text",
            "default_language": "none",
            "name": "fts_documents_blocks",
        }
        self._index_objects["entity_relations"] = {
            "namespace_id": 1, "source_entity_id": 1, "target_entity_id": 1,
            "name": "ix_entity_relations_source_target",
        }
        self._index_objects["embeddings"] = {
            "namespace_id": 1, "owner_type": 1, "owner_id": 1, "model_id": 1, "content_sha256": 1,
            "unique": True,
            "name": "uq_embeddings_owner_model_sha",
        }
        self._index_objects["retrieval_runs"] = {
            "namespace_id": 1, "correlation_id": 1,
            "name": "ix_retrieval_runs_namespace_correlation_id",
        }
        self._index_objects["dispatcher_runs_unique"] = {
            "namespace_id": 1, "correlation_id": 1,
            "unique": True,
            "name": "uq_dispatcher_runs_namespace_correlation_id",
        }
        self._index_objects["dispatcher_runs_state"] = {
            "namespace_id": 1, "processing_state": 1, "updated_at": -1,
            "name": "ix_dispatcher_runs_namespace_state_updated_at",
        }
        self._flush_image()

    def upsert_object(self, object_name: str, object_id: str, object_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            collection = self.load_collection(object_name)
            existing_payload = collection.get(str(object_id)) if isinstance(collection.get(str(object_id)), Mapping) else {}
            payload = _deepcopy_object(dict(existing_payload))
            payload.update(_deepcopy_object(dict(object_payload)))
            payload["_id"] = str(object_id)
            payload["updated_at"] = payload.get("updated_at") or _now_utc().isoformat()
            payload["created_at"] = payload.get("created_at") or existing_payload.get("created_at") or payload["updated_at"]
            collection[str(object_id)] = dict(payload)
            self._flush_image()
            return payload

    def delete_object(self, object_name: str, object_id: str) -> bool:
        with self._lock:
            collection = self.load_collection(object_name)
            normalized_object_id = str(object_id)
            deleted = normalized_object_id in collection
            if deleted:
                collection.pop(normalized_object_id, None)
                self._flush_image()
            return deleted

    def load_object(self, object_name: str, object_id: str) -> dict[str, Any] | None:
        with self._lock:
            collection = self.load_collection(object_name)
            payload = collection.get(str(object_id))
            return dict(payload) if isinstance(payload, Mapping) else None

    def load_objects(
        self,
        object_name: str,
        object_filter: Mapping[str, Any] | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            collection = self.load_collection(object_name)
            filter_payload = dict(object_filter or {})
            result_payload_list: list[dict[str, Any]] = []
            normalized_limit = _normalize_limit_value(limit)
            for object_payload in collection.values():
                if not isinstance(object_payload, Mapping):
                    continue
                if any(object_payload.get(key) != value for key, value in filter_payload.items()):
                    continue
                result_payload_list.append(dict(object_payload))
                if normalized_limit is not None and len(result_payload_list) >= normalized_limit:
                    break
            return result_payload_list

    def find_objects(self, *, namespace_id: str, query_text: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized_query = str(query_text or "").strip().lower()
        if not normalized_query:
            return []
        with self._lock:
            collection = self._collections["documents"]
            result_payload_list: list[dict[str, Any]] = []
            for document_payload in collection.values():
                if not isinstance(document_payload, Mapping):
                    continue
                if str(document_payload.get("namespace_id") or "").strip() != str(namespace_id):
                    continue
                haystack = json.dumps(_json_safe_object(document_payload), ensure_ascii=False).lower()
                if normalized_query not in haystack:
                    continue
                result_payload_list.append(
                    {
                        "document_id": str(document_payload.get("_id") or ""),
                        "title": str(document_payload.get("title") or ""),
                        "source_uri": str(document_payload.get("source_uri") or ""),
                        "document_score": 1.0,
                        "block": {},
                    }
                )
                if len(result_payload_list) >= max(1, int(limit)):
                    break
            return result_payload_list

    def load_relation_graph(self, *, namespace_id: str, source_entity_id: str, max_depth: int = 2) -> list[dict[str, Any]]:
        max_hops = max(0, int(max_depth))
        with self._lock:
            relation_collection = self._collections["entity_relations"]
            visited_sources = {str(source_entity_id)}
            frontier = {str(source_entity_id)}
            result_payload_list: list[dict[str, Any]] = []
            for _ in range(max_hops + 1):
                if not frontier:
                    break
                next_frontier: set[str] = set()
                for relation_payload in relation_collection.values():
                    if not isinstance(relation_payload, Mapping):
                        continue
                    if str(relation_payload.get("namespace_id") or "") != str(namespace_id):
                        continue
                    src = str(relation_payload.get("source_entity_id") or "")
                    tgt = str(relation_payload.get("target_entity_id") or "")
                    if src not in frontier:
                        continue
                    result_payload_list.append(dict(relation_payload))
                    if tgt and tgt not in visited_sources:
                        next_frontier.add(tgt)
                visited_sources.update(next_frontier)
                frontier = next_frontier
            return result_payload_list

    def build_vector_search_pipeline(
        self,
        *,
        query_vector: Sequence[float],
        namespace_id: str,
        owner_type: str = "",
        limit: int = 10,
        num_candidates: int = 100,
        index_name: str = "embedding_cosine",
    ) -> list[dict[str, Any]]:
        return [
            {
                "$vectorSearch": {
                    "index": index_name,
                    "path": "embedding",
                    "queryVector": list(query_vector),
                    "numCandidates": max(1, int(num_candidates)),
                    "limit": max(1, int(limit)),
                    "filter": {"namespace_id": namespace_id, "owner_type": owner_type},
                },
            },
            {
                "$project": {
                    "_id": 0,
                    "owner_id": 1,
                    "owner_type": 1,
                    "model_id": 1,
                    "score": {"$meta": "vectorSearchScore"},
                    "index_backend": 1,
                    "index_namespace": 1,
                    "index_item_key": 1,
                },
            },
        ]


class AgentDbSocketRepository:
    """Knowledge repository backed by a custom agentsdb socket endpoint."""

    _OBJECT_COLLECTION_MAP = KnowledgeRepository._OBJECT_COLLECTION_MAP
    _DEFAULT_APPLY_OPERATIONS_BATCH_SIZE = 64
    _DEFAULT_WRITE_TIMEOUT_SECONDS = 20.0
    _DEFAULT_SOCKET_TIMEOUT_SECONDS = 90.0
    _DEFAULT_REQUEST_RETRY_ATTEMPTS = 3
    _DEFAULT_REQUEST_RETRY_LOG_ENABLED = True

    def __init__(
        self,
        agents_db_uri: str,
        database_name: str = "alde_knowledge",
        timeout_seconds: float | None = None,
    ) -> None:
        endpoint = _load_agentsdb_socket_endpoint(agents_db_uri)
        if endpoint is None:
            raise ValueError(f"invalid agentsdb socket uri: {agents_db_uri}")
        normalized_uri, resolved_host, resolved_port = endpoint
        self._agents_db_uri = normalized_uri
        self._database_name = str(database_name or "alde_knowledge").strip() or "alde_knowledge"
        resolved_timeout_seconds = self._load_socket_timeout_seconds() if timeout_seconds is None else timeout_seconds
        self._timeout_seconds = max(float(resolved_timeout_seconds), 0.5)
        self._host = resolved_host
        self._port = resolved_port
        self._write_lock = threading.RLock()
        self._deferred_write_depth = 0
        self._pending_write_operations: list[dict[str, Any]] = []
        self._apply_operations_batch_size = self._load_apply_operations_batch_size()
        self._write_timeout_seconds = self._load_write_timeout_seconds()
        self._request_retry_attempts = self._load_request_retry_attempts()
        self._request_retry_log_enabled = self._load_request_retry_log_enabled()

    @classmethod
    def _load_apply_operations_batch_size(cls) -> int:
        raw_value = str(
            os.getenv(
                "AI_IDE_KNOWLEDGE_AGENTS_DB_SOCKET_APPLY_BATCH_SIZE",
                str(cls._DEFAULT_APPLY_OPERATIONS_BATCH_SIZE),
            )
            or str(cls._DEFAULT_APPLY_OPERATIONS_BATCH_SIZE)
        ).strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = cls._DEFAULT_APPLY_OPERATIONS_BATCH_SIZE
        return max(1, resolved_value)

    @classmethod
    def _load_write_timeout_seconds(cls) -> float:
        raw_value = str(
            os.getenv(
                "AI_IDE_KNOWLEDGE_AGENTS_DB_SOCKET_WRITE_TIMEOUT_SECONDS",
                str(cls._DEFAULT_WRITE_TIMEOUT_SECONDS),
            )
            or str(cls._DEFAULT_WRITE_TIMEOUT_SECONDS)
        ).strip()
        try:
            resolved_value = float(raw_value)
        except Exception:
            resolved_value = cls._DEFAULT_WRITE_TIMEOUT_SECONDS
        return max(0.5, resolved_value)

    @classmethod
    def _load_socket_timeout_seconds(cls) -> float:
        raw_value = str(
            os.getenv(
                "AI_IDE_KNOWLEDGE_AGENTS_DB_SOCKET_TIMEOUT_SECONDS",
                str(cls._DEFAULT_SOCKET_TIMEOUT_SECONDS),
            )
            or str(cls._DEFAULT_SOCKET_TIMEOUT_SECONDS)
        ).strip()
        try:
            resolved_value = float(raw_value)
        except Exception:
            resolved_value = cls._DEFAULT_SOCKET_TIMEOUT_SECONDS
        return max(0.5, resolved_value)

    @classmethod
    def _load_request_retry_attempts(cls) -> int:
        raw_value = str(
            os.getenv(
                "AI_IDE_KNOWLEDGE_AGENTS_DB_SOCKET_RETRY_ATTEMPTS",
                str(cls._DEFAULT_REQUEST_RETRY_ATTEMPTS),
            )
            or str(cls._DEFAULT_REQUEST_RETRY_ATTEMPTS)
        ).strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = cls._DEFAULT_REQUEST_RETRY_ATTEMPTS
        return max(1, resolved_value)

    @classmethod
    def _load_healthcheck_timeout_seconds(cls) -> float:
        raw_timeout = str(
            os.getenv(
                "AI_IDE_KNOWLEDGE_AGENTS_DB_HEALTHCHECK_TIMEOUT_SECONDS",
                os.getenv("AI_IDE_DISPATCHER_HEALTHCHECK_TIMEOUT_SECONDS", "1.0"),
            )
            or "1.0"
        ).strip()
        try:
            resolved_value = float(raw_timeout)
        except Exception:
            resolved_value = 1.0
        return max(0.1, min(10.0, resolved_value))

    @classmethod
    def _load_request_retry_log_enabled(cls) -> bool:
        raw_value = str(
            os.getenv(
                "AI_IDE_KNOWLEDGE_AGENTS_DB_SOCKET_RETRY_LOG_ENABLED",
                "1" if cls._DEFAULT_REQUEST_RETRY_LOG_ENABLED else "0",
            )
            or "1"
        ).strip().lower()
        return raw_value not in {"0", "false", "no", "off"}

    @classmethod
    def create_from_uri(
        cls,
        agents_db_uri: str,
        database_name: str = "alde_knowledge",
        timeout_seconds: float | None = None,
    ) -> AgentDbSocketRepository:
        return cls(
            agents_db_uri=agents_db_uri,
            database_name=database_name,
            timeout_seconds=timeout_seconds,
        )

    def _request_object(self, action_name: str, action_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request_payload = {
            "cmd": action_name,
            "database_name": self._database_name,
            "payload": _deepcopy_object(dict(action_payload or {})),
        }
        response_bytes: bytes | None = None
        last_error: Exception | None = None
        request_retry_attempts = self._load_request_retry_attempts_for_payload(request_payload)
        for attempt_index in range(request_retry_attempts):
            try:
                try:
                    response_bytes = self._send_request_bytes(request_payload)
                except OSError:
                    if _ensure_local_agentsdb_socket_server(self._agents_db_uri, timeout_seconds=self._timeout_seconds):
                        response_bytes = self._send_request_bytes(request_payload)
                    else:
                        raise
                break
            except (OSError, TimeoutError) as exc:
                last_error = exc
                if self._request_retry_log_enabled:
                    command_name = str(request_payload.get("cmd") or action_name or "request").strip() or "request"
                    attempt_number = attempt_index + 1
                    total_attempts = max(1, int(request_retry_attempts))
                    print(
                        "[agents_db] socket_retry "
                        f"cmd={command_name} "
                        f"attempt={attempt_number}/{total_attempts} "
                        f"error={type(exc).__name__}: {exc}"
                    )
                if attempt_index + 1 >= request_retry_attempts:
                    raise
                # Brief bounded backoff for transient socket saturation.
                time.sleep(min(0.1 * (attempt_index + 1), 0.3))
        if response_bytes is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("agentsdb socket request failed without response")
        if not response_bytes:
            raise RuntimeError("agentsdb socket returned no response")
        raw_line = response_bytes.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        try:
            response_payload = json.loads(raw_line)
        except Exception as exc:
            raise RuntimeError(f"agentsdb socket returned invalid JSON: {raw_line}") from exc
        if not isinstance(response_payload, Mapping):
            raise RuntimeError("agentsdb socket returned non-object response")
        if not bool(response_payload.get("ok", True)):
            error_text = str(response_payload.get("error") or "agentsdb socket request failed").strip()
            detail_text = str(response_payload.get("detail") or "").strip()
            if detail_text:
                raise RuntimeError(f"{error_text}: {detail_text}")
            raise RuntimeError(error_text)
        return dict(response_payload)

    @staticmethod
    def _is_dispatcher_healthcheck_request(request_payload: Mapping[str, Any]) -> bool:
        if str(request_payload.get("cmd") or "").strip().lower() != "load_object":
            return False
        payload = request_payload.get("payload") if isinstance(request_payload.get("payload"), Mapping) else {}
        return str(payload.get("object_id") or "").strip() == "__dispatch_healthcheck__"

    def _load_request_retry_attempts_for_payload(self, request_payload: Mapping[str, Any]) -> int:
        command_name = str(request_payload.get("cmd") or "").strip().lower()
        if command_name in {"health", "ping", "status"} or self._is_dispatcher_healthcheck_request(request_payload):
            return 1
        return self._load_request_retry_attempts()

    def _load_request_timeout_seconds(self, request_payload: Mapping[str, Any]) -> float:
        default_timeout_seconds = max(float(self._timeout_seconds), 0.5)
        command_name = str(request_payload.get("cmd") or "").strip().lower()
        if command_name in {"upsert_object", "apply_operations", "delete_object"}:
            default_timeout_seconds = max(default_timeout_seconds, float(self._write_timeout_seconds))
        if command_name in {"health", "ping", "status"} or self._is_dispatcher_healthcheck_request(request_payload):
            return min(default_timeout_seconds, self._load_healthcheck_timeout_seconds())
        return default_timeout_seconds

    def _send_request_bytes(self, request_payload: Mapping[str, Any]) -> bytes:
        serialized_request_payload = _json_safe_object(dict(request_payload))
        request_timeout_seconds = self._load_request_timeout_seconds(request_payload)
        deadline = time.monotonic() + max(float(request_timeout_seconds), 0.1)
        with socket.create_connection((self._host, self._port), timeout=request_timeout_seconds) as connection:
            # Keep recv polling bounded so callers in the UI thread cannot block forever.
            connection.settimeout(min(max(float(request_timeout_seconds), 0.1), 1.0))
            connection.sendall((json.dumps(serialized_request_payload, separators=(",", ":")) + "\n").encode("utf-8"))
            response_bytes = b""
            while b"\n" not in response_bytes:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    command_name = str(request_payload.get("cmd") or "request").strip() or "request"
                    raise TimeoutError(f"agentsdb socket timed out waiting for {command_name} response")
                connection.settimeout(min(max(remaining_seconds, 0.05), 1.0))
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                response_bytes += chunk
        return response_bytes

    def _stream_request_messages(
        self,
        action_name: str,
        action_payload: Mapping[str, Any] | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> Iterable[dict[str, Any]]:
        request_payload = {
            "cmd": action_name,
            "database_name": self._database_name,
            "payload": _deepcopy_object(dict(action_payload or {})),
        }

        def open_connection() -> socket.socket:
            connection = socket.create_connection((self._host, self._port), timeout=self._timeout_seconds)
            connection.settimeout(1.0)
            serialized_request_payload = _json_safe_object(dict(request_payload))
            connection.sendall((json.dumps(serialized_request_payload, separators=(",", ":")) + "\n").encode("utf-8"))
            return connection

        connection: socket.socket | None = None
        read_buffer = b""
        try:
            try:
                connection = open_connection()
            except OSError:
                if _ensure_local_agentsdb_socket_server(self._agents_db_uri, timeout_seconds=self._timeout_seconds):
                    connection = open_connection()
                else:
                    raise

            while stop_event is None or not stop_event.is_set():
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                read_buffer += chunk
                while b"\n" in read_buffer:
                    raw_line, read_buffer = read_buffer.split(b"\n", 1)
                    normalized_line = raw_line.decode("utf-8", errors="replace").strip()
                    if not normalized_line:
                        continue
                    try:
                        response_payload = json.loads(normalized_line)
                    except Exception as exc:
                        raise RuntimeError(f"agentsdb socket stream returned invalid JSON: {normalized_line}") from exc
                    if not isinstance(response_payload, Mapping):
                        raise RuntimeError("agentsdb socket stream returned non-object response")
                    yield dict(response_payload)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def subscribe_tree_stream(
        self,
        tree_object_id: str,
        *,
        last_event_id: str | None = None,
        stop_event: threading.Event | None = None,
        heartbeat_seconds: float = 30.0,
        include_meta: bool = False,
    ) -> Iterable[dict[str, Any]]:
        action_payload = {
            "tree_object_id": str(tree_object_id or "").strip(),
            "last_event_id": str(last_event_id or "").strip() or None,
            "heartbeat_seconds": max(float(heartbeat_seconds), 1.0),
        }
        for response_payload in self._stream_request_messages(
            "subscribe_tree_stream",
            action_payload,
            stop_event=stop_event,
        ):
            if not bool(response_payload.get("ok", True)):
                error_text = str(response_payload.get("error") or "agentsdb tree stream failed").strip()
                detail_text = str(response_payload.get("detail") or "").strip()
                if detail_text:
                    raise RuntimeError(f"{error_text}: {detail_text}")
                raise RuntimeError(error_text)
            if bool(response_payload.get("subscribed")) or bool(response_payload.get("heartbeat")):
                if include_meta:
                    yield dict(response_payload)
                continue
            yield dict(response_payload)

    def subscribe_repository_stream(
        self,
        *,
        last_event_id: str | None = None,
        stop_event: threading.Event | None = None,
        heartbeat_seconds: float = 30.0,
        object_names: Sequence[str] | None = None,
        include_meta: bool = False,
    ) -> Iterable[dict[str, Any]]:
        normalized_object_names = [
            str(object_name or "").strip().lower()
            for object_name in (object_names or [])
            if str(object_name or "").strip()
        ]
        action_payload = {
            "last_event_id": str(last_event_id or "").strip() or None,
            "heartbeat_seconds": max(float(heartbeat_seconds), 1.0),
            "object_names": normalized_object_names or None,
        }
        for response_payload in self._stream_request_messages(
            "subscribe_repository_stream",
            action_payload,
            stop_event=stop_event,
        ):
            if not bool(response_payload.get("ok", True)):
                error_text = str(response_payload.get("error") or "agentsdb repository stream failed").strip()
                detail_text = str(response_payload.get("detail") or "").strip()
                if detail_text:
                    raise RuntimeError(f"{error_text}: {detail_text}")
                raise RuntimeError(error_text)
            if bool(response_payload.get("subscribed")) or bool(response_payload.get("heartbeat")):
                if include_meta:
                    yield dict(response_payload)
                continue
            yield dict(response_payload)

    @contextmanager
    def deferred_write_queue(self) -> Iterable[None]:
        with self._write_lock:
            self._deferred_write_depth += 1
        try:
            yield
        finally:
            pending: list[dict[str, Any]] = []
            with self._write_lock:
                self._deferred_write_depth = max(0, self._deferred_write_depth - 1)
                if self._deferred_write_depth == 0 and self._pending_write_operations:
                    pending = list(self._pending_write_operations)
                    self._pending_write_operations = []
            if pending:
                self._apply_operations(pending)

    def _apply_operations(self, operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        normalized_operations = [
            _deepcopy_object(dict(operation))
            for operation in operations
            if isinstance(operation, Mapping)
        ]
        if not normalized_operations:
            return {"ok": True, "applied": 0, "deleted": 0, "results": [], "batch_count": 0}

        batch_size = max(int(self._apply_operations_batch_size or 1), 1)
        aggregated_results: list[dict[str, Any]] = []
        aggregated_applied = 0
        aggregated_deleted = 0
        batch_count = 0
        last_response_payload: dict[str, Any] = {"ok": True}

        for batch_start in range(0, len(normalized_operations), batch_size):
            batch_operations = normalized_operations[batch_start: batch_start + batch_size]
            response_payload = self._request_object(
                "apply_operations",
                {"operations": batch_operations},
            )
            last_response_payload = dict(response_payload)
            aggregated_applied += int(response_payload.get("applied") or len(batch_operations))
            aggregated_deleted += int(response_payload.get("deleted") or 0)
            batch_results = response_payload.get("results")
            if isinstance(batch_results, list):
                aggregated_results.extend(dict(item) for item in batch_results if isinstance(item, Mapping))
            batch_count += 1

        last_response_payload["applied"] = aggregated_applied
        last_response_payload["deleted"] = aggregated_deleted
        last_response_payload["results"] = aggregated_results
        last_response_payload["batch_count"] = batch_count
        return last_response_payload

    def load_collection(self, object_name: str) -> str:
        return str(self._OBJECT_COLLECTION_MAP[str(object_name).strip().lower()])

    def ensure_index_objects(self) -> None:
        self._request_object("ensure_index_objects")

    def upsert_object(self, object_name: str, object_id: str, object_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = _deepcopy_object(dict(object_payload))
        if "updated_at" not in payload:
            payload["updated_at"] = _now_utc().isoformat()
        if "created_at" not in payload:
            payload["created_at"] = payload["updated_at"]
        with self._write_lock:
            if self._deferred_write_depth > 0:
                self._pending_write_operations.append(
                    {
                        "action": "upsert",
                        "object_name": str(object_name),
                        "object_id": str(object_id),
                        "object_payload": payload,
                    }
                )
                return dict(payload)
        response_payload = self._request_object(
            "upsert_object",
            {
                "object_name": str(object_name),
                "object_id": str(object_id),
                "object_payload": payload,
            },
        )
        return dict(response_payload.get("object_payload") or payload)

    def delete_object(self, object_name: str, object_id: str) -> bool:
        with self._write_lock:
            if self._deferred_write_depth > 0:
                self._pending_write_operations.append(
                    {
                        "action": "delete",
                        "object_name": str(object_name),
                        "object_id": str(object_id),
                    }
                )
                return True
        response_payload = self._request_object(
            "delete_object",
            {
                "object_name": str(object_name),
                "object_id": str(object_id),
            },
        )
        return bool(response_payload.get("deleted"))

    def load_object(self, object_name: str, object_id: str) -> dict[str, Any] | None:
        response_payload = self._request_object(
            "load_object",
            {
                "object_name": str(object_name),
                "object_id": str(object_id),
            },
        )
        object_payload = response_payload.get("object_payload")
        return dict(object_payload) if isinstance(object_payload, Mapping) else None

    def load_objects(
        self,
        object_name: str,
        object_filter: Mapping[str, Any] | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        normalized_limit = _normalize_limit_value(limit)
        request_payload = {
            "object_name": str(object_name),
            "object_filter": _deepcopy_object(dict(object_filter or {})),
            "limit": normalized_limit if normalized_limit is not None else 0,
        }
        response_payload = self._request_object("load_objects", request_payload)
        object_payload_list = response_payload.get("object_payload_list")
        if not isinstance(object_payload_list, list):
            return []
        return [dict(item) for item in object_payload_list if isinstance(item, Mapping)]

    def find_objects(self, *, namespace_id: str, query_text: str, limit: int = 10) -> list[dict[str, Any]]:
        response_payload = self._request_object(
            "find_objects",
            {
                "namespace_id": str(namespace_id),
                "query_text": str(query_text),
                "limit": max(1, int(limit)),
            },
        )
        object_payload_list = response_payload.get("object_payload_list")
        if not isinstance(object_payload_list, list):
            return []
        return [dict(item) for item in object_payload_list if isinstance(item, Mapping)]

    def load_relation_graph(self, *, namespace_id: str, source_entity_id: str, max_depth: int = 2) -> list[dict[str, Any]]:
        response_payload = self._request_object(
            "load_relation_graph",
            {
                "namespace_id": str(namespace_id),
                "source_entity_id": str(source_entity_id),
                "max_depth": max(0, int(max_depth)),
            },
        )
        object_payload_list = response_payload.get("object_payload_list")
        if not isinstance(object_payload_list, list):
            return []
        return [dict(item) for item in object_payload_list if isinstance(item, Mapping)]

    def build_vector_search_pipeline(
        self,
        *,
        query_vector: Sequence[float],
        namespace_id: str,
        owner_type: str = "block",
        limit: int = 10,
        num_candidates: int = 100,
        index_name: str = "embedding_cosine",
    ) -> list[dict[str, Any]]:
        return KnowledgeRepository.build_vector_search_pipeline(
            self,
            query_vector=query_vector,
            namespace_id=namespace_id,
            owner_type=owner_type,
            limit=limit,
            num_candidates=num_candidates,
            index_name=index_name,
        )


class UiAgentDbSocketRepository(AgentDbSocketRepository):
    _DEFAULT_UI_SOCKET_TIMEOUT_SECONDS = 3.0
    _DEFAULT_UI_REQUEST_RETRY_ATTEMPTS = 1

    def __init__(
        self,
        agents_db_uri: str,
        database_name: str = "alde_knowledge",
        timeout_seconds: float | None = None,
        retry_attempts: int | None = None,
    ) -> None:
        resolved_timeout_seconds = self._load_ui_socket_timeout_seconds() if timeout_seconds is None else timeout_seconds
        super().__init__(
            agents_db_uri=agents_db_uri,
            database_name=database_name,
            timeout_seconds=resolved_timeout_seconds,
        )
        if retry_attempts is None:
            retry_attempts = self._load_ui_request_retry_attempts()
        try:
            resolved_retry_attempts = int(retry_attempts)
        except Exception:
            resolved_retry_attempts = self._DEFAULT_UI_REQUEST_RETRY_ATTEMPTS
        self._ui_request_retry_attempts = max(1, resolved_retry_attempts)

    @classmethod
    def _load_ui_socket_timeout_seconds(cls) -> float:
        raw_value = str(
            os.getenv(
                "AI_IDE_UI_AGENTS_DB_SOCKET_TIMEOUT_SECONDS",
                str(cls._DEFAULT_UI_SOCKET_TIMEOUT_SECONDS),
            )
            or str(cls._DEFAULT_UI_SOCKET_TIMEOUT_SECONDS)
        ).strip()
        try:
            resolved_value = float(raw_value)
        except Exception:
            resolved_value = cls._DEFAULT_UI_SOCKET_TIMEOUT_SECONDS
        return max(0.5, min(15.0, resolved_value))

    @classmethod
    def _load_ui_request_retry_attempts(cls) -> int:
        raw_value = str(
            os.getenv(
                "AI_IDE_UI_AGENTS_DB_SOCKET_RETRY_ATTEMPTS",
                str(cls._DEFAULT_UI_REQUEST_RETRY_ATTEMPTS),
            )
            or str(cls._DEFAULT_UI_REQUEST_RETRY_ATTEMPTS)
        ).strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = cls._DEFAULT_UI_REQUEST_RETRY_ATTEMPTS
        return max(1, min(3, resolved_value))

    @classmethod
    def create_from_uri(
        cls,
        agents_db_uri: str,
        database_name: str = "alde_knowledge",
        timeout_seconds: float | None = None,
        retry_attempts: int | None = None,
    ) -> UiAgentDbSocketRepository:
        return cls(
            agents_db_uri=agents_db_uri,
            database_name=database_name,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
        )

    def _load_request_retry_attempts_for_payload(self, request_payload: Mapping[str, Any]) -> int:
        command_name = str(request_payload.get("cmd") or "").strip().lower()
        if command_name in {"upsert_object", "apply_operations", "delete_object"}:
            return super()._load_request_retry_attempts_for_payload(request_payload)
        if command_name in {"health", "ping", "status"} or self._is_dispatcher_healthcheck_request(request_payload):
            return super()._load_request_retry_attempts_for_payload(request_payload)
        return max(1, int(self._ui_request_retry_attempts))

    def request(
        self,
        query_text: str,
        *,
        owner_types: Sequence[str] | str | None = None,
        limit: int = 10,
        namespace_id: str | None = None,
        use_vector: bool = True,
        job_name: str | None = None,
        target_agent: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        return AGENT_DB_QUERY_SERVICE.load_request_object(
            query_text=query_text,
            owner_types=owner_types,
            limit=limit,
            namespace_id=namespace_id,
            use_vector=use_vector,
            job_name=job_name,
            target_agent=target_agent,
            tool_name=tool_name,
        )

    def query(
        self,
        query_text: str,
        *,
        owner_types: Sequence[str] | str | None = None,
        limit: int = 10,
        namespace_id: str | None = None,
        use_vector: bool = True,
        job_name: str | None = None,
        target_agent: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        request_payload = self.request(
            query_text,
            owner_types=owner_types,
            limit=limit,
            namespace_id=namespace_id,
            use_vector=use_vector,
            job_name=job_name,
            target_agent=target_agent,
            tool_name=tool_name,
        )
        response_payload = self._request_object("query", request_payload)
        return dict(response_payload)


class AgentDbInMemoryRepository:
    """Knowledge repository that stores all objects in-memory and flushes snapshots to disk."""

    _OBJECT_COLLECTION_MAP = KnowledgeRepository._OBJECT_COLLECTION_MAP

    def __init__(self, image_path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._image_path = str(image_path or "").strip() or None
        self._deferred_flush_depth = 0
        self._flush_pending = False
        self._collections: dict[str, dict[str, dict[str, Any]]] = {
            collection_name: {}
            for collection_name in self._OBJECT_COLLECTION_MAP.values()
        }
        self._load_image()

    @contextmanager
    def deferred_flush(self) -> Iterable[None]:
        with self._lock:
            self._deferred_flush_depth += 1
        try:
            yield
        finally:
            should_flush = False
            with self._lock:
                self._deferred_flush_depth = max(0, self._deferred_flush_depth - 1)
                should_flush = self._deferred_flush_depth == 0 and self._flush_pending
                if should_flush:
                    self._flush_pending = False
            if should_flush:
                self._flush_image()

    def _mark_flush_needed(self) -> None:
        if self._deferred_flush_depth > 0:
            self._flush_pending = True
            return
        self._flush_image()

    def _load_image(self) -> None:
        if not self._image_path:
            return
        path = os.path.abspath(os.path.expanduser(self._image_path))
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as image_file:
                image_payload = json.load(image_file)
        except Exception:
            return
        collections_payload = image_payload.get("collections") if isinstance(image_payload, Mapping) else None
        if not isinstance(collections_payload, Mapping):
            return
        with self._lock:
            for collection_name, collection_payload in collections_payload.items():
                normalized_collection = str(collection_name or "").strip()
                if normalized_collection not in self._collections:
                    continue
                if not isinstance(collection_payload, Mapping):
                    continue
                self._collections[normalized_collection] = {
                    str(record_id): dict(record_payload)
                    for record_id, record_payload in collection_payload.items()
                    if isinstance(record_payload, Mapping)
                }

    def _flush_image(self) -> None:
        if not self._image_path:
            return
        path = os.path.abspath(os.path.expanduser(self._image_path))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._lock:
            image_payload = {
                "schema": "agentsdb_inmemory_image_v1",
                "updated_at": _now_utc().isoformat(),
                "collections": _json_safe_object(self._collections),
            }
        temp_path = f"{path}.{threading.get_ident()}.{time.time_ns()}.tmp"
        with open(temp_path, "w", encoding="utf-8") as image_file:
            json.dump(image_payload, image_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)

    def _load_collection_object(self, object_name: str) -> dict[str, dict[str, Any]]:
        collection_name = self._OBJECT_COLLECTION_MAP[str(object_name).strip().lower()]
        return self._collections[collection_name]

    def ensure_index_objects(self) -> None:
        return None

    def upsert_object(self, object_name: str, object_id: str, object_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            collection = self._load_collection_object(object_name)
            existing_payload = collection.get(object_id) if isinstance(collection.get(object_id), Mapping) else {}
            payload:dict = _deepcopy_object(dict(existing_payload))
            payload.update(_deepcopy_object(dict(object_payload)))
            payload["_id"] = object_id
            payload["updated_at"] = payload.get("updated_at") or _now_utc().isoformat()
            payload["created_at"] = payload.get("created_at") or existing_payload.get("created_at") or payload["updated_at"]
            collection[object_id] = dict(payload)
            self._mark_flush_needed()
            return dict(payload)

    def delete_object(self, object_name: str, object_id: str) -> bool:
        with self._lock:
            collection = self._load_collection_object(object_name)
            normalized_object_id = str(object_id)
            deleted = normalized_object_id in collection
            if deleted:
                collection.pop(normalized_object_id, None)
                self._mark_flush_needed()
            return deleted

    def load_object(self, object_name: str, object_id: str) -> dict[str, Any] | None:
        with self._lock:
            collection = self._load_collection_object(object_name)
            payload = collection.get(object_id)
            return dict(payload) if isinstance(payload, Mapping) else None

    def load_objects(
        self,
        object_name: str,
        object_filter: Mapping[str, Any] | None = None,
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            collection = self._load_collection_object(object_name)
            filter_payload = dict(object_filter or {})
            result_payload_list: list[dict[str, Any]] = []
            normalized_limit = _normalize_limit_value(limit)
            for object_payload in collection.values():
                if not isinstance(object_payload, Mapping):
                    continue
                if any(object_payload.get(key) != value for key, value in filter_payload.items()):
                    continue
                result_payload_list.append(dict(object_payload))
                if normalized_limit is not None and len(result_payload_list) >= normalized_limit:
                    break
            return result_payload_list

    def find_objects(self, *, namespace_id: str, query_text: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized_query = str(query_text or "").strip().lower()
        if not normalized_query:
            return []
        with self._lock:
            collection = self._collections["documents"]
            result_payload_list: list[dict[str, Any]] = []
            for document_payload in collection.values():
                if not isinstance(document_payload, Mapping):
                    continue
                if str(document_payload.get("namespace_id") or "").strip() != str(namespace_id):
                    continue
                haystack = json.dumps(_json_safe_object(document_payload), ensure_ascii=False).lower()
                if normalized_query not in haystack:
                    continue
                result_payload_list.append({
                    "document_id": str(document_payload.get("_id") or ""),
                    "title": str(document_payload.get("title") or ""),
                    "source_uri": str(document_payload.get("source_uri") or ""),
                    "document_score": 1.0,
                    "block": {},
                })
                if len(result_payload_list) >= max(1, int(limit)):
                    break
            return result_payload_list

    def load_relation_graph(self, *, namespace_id: str, source_entity_id: str, max_depth: int = 2) -> list[dict[str, Any]]:
        max_hops = max(0, int(max_depth))
        with self._lock:
            relation_collection = self._collections["entity_relations"]
            visited_sources = {str(source_entity_id)}
            frontier = {str(source_entity_id)}
            result_payload_list: list[dict[str, Any]] = []
            for _ in range(max_hops + 1):
                if not frontier:
                    break
                next_frontier: set[str] = set()
                for relation_payload in relation_collection.values():
                    if not isinstance(relation_payload, Mapping):
                        continue
                    if str(relation_payload.get("namespace_id") or "") != str(namespace_id):
                        continue
                    src = str(relation_payload.get("source_entity_id") or "")
                    tgt = str(relation_payload.get("target_entity_id") or "")
                    if src not in frontier:
                        continue
                    result_payload_list.append(dict(relation_payload))
                    if tgt and tgt not in visited_sources:
                        next_frontier.add(tgt)
                visited_sources.update(next_frontier)
                frontier = next_frontier
            return result_payload_list

    def request(
        self,
        query_text: str,
        *,
        owner_types: Sequence[str] | str | None = None,
        limit: int = 10,
        namespace_id: str | None = None,
        use_vector: bool = True,
        job_name: str | None = None,
        target_agent: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        return AGENT_DB_QUERY_SERVICE.load_request_object(
            query_text=query_text,
            owner_types=owner_types,
            limit=limit,
            namespace_id=namespace_id,
            image_path=self._image_path,
            use_vector=use_vector,
            job_name=job_name,
            target_agent=target_agent,
            tool_name=tool_name,
        )

    def query(
        self,
        query_text: str,
        *,
        owner_types: Sequence[str] | str | None = None,
        limit: int = 10,
        namespace_id: str | None = None,
        use_vector: bool = True,
        job_name: str | None = None,
        target_agent: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        return AGENT_DB_QUERY_SERVICE.query_object(
            self,
            query_text=query_text,
            owner_types=owner_types,
            limit=limit,
            namespace_id=namespace_id,
            image_path=self._image_path,
            use_vector=use_vector,
            job_name=job_name,
            target_agent=target_agent,
            tool_name=tool_name,
        )


class AgentDbQueryService:
    """Domain service for repository query requests and local repo-knowledge retrieval."""

    _DEFAULT_TARGET_AGENT = "_xworker"
    _DEFAULT_JOB_NAME = "adb_query"
    _DEFAULT_TOOL_NAME = None
    _DEFAULT_NAMESPACE_ID = "ns_repo_knowledge" or "ns_default_knowledge"
    _DEFAULT_NAMESPACE_SLUG = "repo-knowledge"
    _DEFAULT_NAMESPACE_NAME = "ALDE Repository Knowledge"
    _DEFAULT_OWNER_TYPES = ("block", "entity","relation")
    _OWNER_TYPE_ALL = ("block", "entity", "relation")
    _OWNER_TYPE_ALIAS_MAP = {
        "block": "block",
        "blocks": "block",
        "document": "block",
        "documents": "block",
        "doc": "block",
        "docs": "block",
        "entity": "entity",
        "entities": "entity",
        "node": "entity",
        "nodes": "entity",
        "relation": "relation",
        "relations": "relation",
        "edge": "relation",
        "edges": "relation",
    }
    _COLLECTION_LIMIT = 10000
    _RESULT_LIMIT = 50

    def load_request_object(
        self,
        *,
        query_text: str,
        owner_types: Sequence[str] | str | None = None,
        limit: int = 10,
        namespace_id: str | None = None,
        image_path: str | None = None,
        use_vector: bool = True,
        job_name: str | None = None,
        target_agent: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        resolved_query = str(query_text or "").strip()
        resolved_owner_types = self._load_owner_type_list(owner_types)
        safe_limit = max(1, min(int(limit), self._RESULT_LIMIT))
        request_payload: dict[str, Any] = {
            "target_agent": str(target_agent or self._DEFAULT_TARGET_AGENT).strip() or self._DEFAULT_TARGET_AGENT,
            "job_name": str(job_name or self._DEFAULT_JOB_NAME).strip() or self._DEFAULT_JOB_NAME,
            "tool_name": str(tool_name or self._DEFAULT_TOOL_NAME).strip() or self._DEFAULT_TOOL_NAME,
            "user_question": resolved_query,
            "query": resolved_query,
            "owner_types": resolved_owner_types,
            "limit": safe_limit,
            "namespace_id": str(namespace_id or self._DEFAULT_NAMESPACE_ID).strip() or self._DEFAULT_NAMESPACE_ID,
            "use_vector": bool(use_vector),
        }
        resolved_image_path = str(image_path or "").strip()
        if resolved_image_path:
            request_payload["image_path"] = resolved_image_path
        return request_payload

    def query_object(
        self,
        repository: Any,
        *,
        query_text: str,
        owner_types: Sequence[str] | str | None = None,
        limit: int = 10,
        namespace_id: str | None = None,
        image_path: str | None = None,
        use_vector: bool = True,
        job_name: str | None = None,
        target_agent: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        request_payload = self.load_request_object(
            query_text=query_text,
            owner_types=owner_types,
            limit=limit,
            namespace_id=namespace_id,
            image_path=image_path,
            use_vector=use_vector,
            job_name=job_name,
            target_agent=target_agent,
            tool_name=tool_name,
        )
        resolved_query = str(request_payload.get("query") or "").strip()
        if not resolved_query:
            return self._build_query_result(
                request_payload=request_payload,
                ok=False,
                chunks=[],
                used_vector_search=False,
                error="query_text is required",
            )

        owner_payload_cache: dict[str, dict[str, dict[str, Any]]] = {}
        query_vector = self._load_query_vector(
            repository,
            query_text=resolved_query,
            namespace_id=str(request_payload.get("namespace_id") or self._DEFAULT_NAMESPACE_ID),
            use_vector=bool(request_payload.get("use_vector")),
        )
        used_vector_search = False
        chunk_payload_list: list[dict[str, Any]] = []

        for owner_type in request_payload.get("owner_types") or []:
            resolved_owner_type = str(owner_type or "").strip().lower()
            if resolved_owner_type not in self._OWNER_TYPE_ALL:
                continue
            candidates: list[dict[str, Any]] = []
            if query_vector is not None:
                candidates = self._load_vector_candidate_payload_list(
                    repository,
                    owner_type=resolved_owner_type,
                    query_vector=query_vector,
                    namespace_id=str(request_payload.get("namespace_id") or self._DEFAULT_NAMESPACE_ID),
                    limit=int(request_payload.get("limit") or 10),
                    owner_payload_cache=owner_payload_cache,
                )
                if candidates:
                    used_vector_search = True
            if not candidates:
                candidates = self._load_text_candidate_payload_list(
                    repository,
                    owner_type=resolved_owner_type,
                    query_text=resolved_query,
                    namespace_id=str(request_payload.get("namespace_id") or self._DEFAULT_NAMESPACE_ID),
                    limit=int(request_payload.get("limit") or 10),
                )
            chunk_payload_list.extend(self._format_chunk_payload_list(candidates, resolved_owner_type))

        return self._build_query_result(
            request_payload=request_payload,
            ok=True,
            chunks=chunk_payload_list,
            used_vector_search=used_vector_search,
        )

    def _build_query_result(
        self,
        *,
        request_payload: Mapping[str, Any],
        ok: bool,
        chunks: Sequence[Mapping[str, Any]],
        used_vector_search: bool,
        error: str | None = None,
    ) -> dict[str, Any]:
        result_payload: dict[str, Any] = {
            "ok": bool(ok),
            "query": str(request_payload.get("query") or "").strip(),
            "namespace_id": str(request_payload.get("namespace_id") or self._DEFAULT_NAMESPACE_ID).strip() or self._DEFAULT_NAMESPACE_ID,
            "owner_types": [
                str(owner_type).strip()
                for owner_type in (request_payload.get("owner_types") or [])
                if str(owner_type).strip()
            ],
            "used_vector_search": bool(used_vector_search),
            "total": len(list(chunks)),
            "chunks": [dict(chunk) for chunk in chunks if isinstance(chunk, Mapping)],
            "target_agent": str(request_payload.get("target_agent") or self._DEFAULT_TARGET_AGENT).strip() or self._DEFAULT_TARGET_AGENT,
            "job_name": str(request_payload.get("job_name") or self._DEFAULT_JOB_NAME).strip() or self._DEFAULT_JOB_NAME,
            "tool_name": str(request_payload.get("tool_name") or self._DEFAULT_TOOL_NAME).strip() or self._DEFAULT_TOOL_NAME,
            "request": _deepcopy_object(dict(request_payload)),
        }
        if error:
            result_payload["error"] = str(error)
        return result_payload

    def _load_owner_type_list(self, owner_types: Sequence[str] | str | None) -> list[str]:
        if owner_types is None:
            return list(self._DEFAULT_OWNER_TYPES)
        if isinstance(owner_types, str):
            candidate_values = re.split(r"[,;|\s]+", owner_types)
        else:
            candidate_values = []
            for value in owner_types:
                candidate_values.extend(re.split(r"[,;|\s]+", str(value)))

        resolved_owner_types: list[str] = []
        for candidate_value in candidate_values:
            normalized_value = str(candidate_value or "").strip().lower()
            if not normalized_value:
                continue
            if normalized_value == "all":
                for owner_type in self._OWNER_TYPE_ALL:
                    if owner_type not in resolved_owner_types:
                        resolved_owner_types.append(owner_type)
                continue
            resolved_owner_type = self._load_owner_type(normalized_value)
            if resolved_owner_type and resolved_owner_type not in resolved_owner_types:
                resolved_owner_types.append(resolved_owner_type)
        return resolved_owner_types or list(self._DEFAULT_OWNER_TYPES)

    def _load_owner_type(self, owner_type: str) -> str | None:
        normalized_value = str(owner_type or "").strip().lower()
        if not normalized_value:
            return None
        resolved_owner_type = self._OWNER_TYPE_ALIAS_MAP.get(normalized_value)
        if resolved_owner_type in self._OWNER_TYPE_ALL:
            return resolved_owner_type
        if normalized_value.endswith("s") and normalized_value[:-1] in self._OWNER_TYPE_ALL:
            return normalized_value[:-1]
        if normalized_value in self._OWNER_TYPE_ALL:
            return normalized_value
        return None

    def _load_query_vector(
        self,
        repository: Any,
        *,
        query_text: str,
        namespace_id: str,
        use_vector: bool,
    ) -> list[float] | None:
        if not use_vector:
            return None
        try:
            runtime_config = self._load_runtime_config(repository, namespace_id=namespace_id)
            embedding_service = EntityRelationEmbeddingService(KnowledgeObjectService(repository), runtime_config)
            query_vector = embedding_service.embed_object("query", query_text)
        except Exception:
            return None
        return self._normalize_vector(query_vector) or None

    def _load_runtime_config(self, repository: Any, *, namespace_id: str) -> RuntimeConfigObject:
        resolved_namespace_id = str(namespace_id or self._DEFAULT_NAMESPACE_ID).strip() or self._DEFAULT_NAMESPACE_ID
        loaded_runtime_config = load_agentsdb_runtime_config_from_env()
        if loaded_runtime_config is None:
            agents_db_uri = str(getattr(repository, "_agents_db_uri", "") or "agentsmem://local").strip() or "agentsmem://local"
            database_name = str(getattr(repository, "_database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge"
            return RuntimeConfigObject(
                agents_db_uri=agents_db_uri,
                database_name=database_name,
                namespace_id=resolved_namespace_id,
                namespace_slug=self._DEFAULT_NAMESPACE_SLUG,
                namespace_name=self._DEFAULT_NAMESPACE_NAME,
            )

        namespace_slug = str(getattr(loaded_runtime_config, "namespace_slug", "") or "").strip() or self._DEFAULT_NAMESPACE_SLUG
        namespace_name = str(getattr(loaded_runtime_config, "namespace_name", "") or "").strip() or self._DEFAULT_NAMESPACE_NAME
        if resolved_namespace_id == self._DEFAULT_NAMESPACE_ID:
            namespace_slug = self._DEFAULT_NAMESPACE_SLUG
            namespace_name = self._DEFAULT_NAMESPACE_NAME
        return RuntimeConfigObject(
            agents_db_uri=str(getattr(loaded_runtime_config, "agents_db_uri", "") or "agentsmem://local").strip() or "agentsmem://local",
            database_name=str(getattr(loaded_runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge",
            tenant_id=str(getattr(loaded_runtime_config, "tenant_id", "tenant_default") or "tenant_default").strip() or "tenant_default",
            namespace_id=resolved_namespace_id,
            namespace_slug=namespace_slug,
            namespace_name=namespace_name,
            default_embedding_model=str(getattr(loaded_runtime_config, "default_embedding_model", "text-embedding-3-large") or "text-embedding-3-large").strip() or "text-embedding-3-large",
            default_embedding_dimension=max(1, int(getattr(loaded_runtime_config, "default_embedding_dimension", 3072) or 3072)),
            index_backend=str(getattr(loaded_runtime_config, "index_backend", "faiss") or "faiss").strip() or "faiss",
        )

    def _load_text_candidate_payload_list(
        self,
        repository: Any,
        *,
        owner_type: str,
        query_text: str,
        namespace_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized_query = str(query_text or "").strip().lower()
        if not normalized_query:
            return []
        if owner_type == "block":
            return self._load_block_text_candidate_payload_list(
                repository,
                query_text=normalized_query,
                namespace_id=namespace_id,
                limit=limit,
            )

        object_name = "entity" if owner_type == "entity" else "relation"
        try:
            object_payload_list = repository.load_objects(
                object_name,
                {"namespace_id": str(namespace_id)},
                limit=self._COLLECTION_LIMIT,
            )
        except Exception:
            return []

        result_payload_list: list[dict[str, Any]] = []
        for object_payload in object_payload_list:
            if not isinstance(object_payload, Mapping):
                continue
            haystack = json.dumps(_json_safe_object(object_payload), ensure_ascii=False).lower()
            score = self._load_text_match_score(haystack, normalized_query)
            if score <= 0.0:
                continue
            result_payload_list.append({"payload": dict(object_payload), "score": score})
            if len(result_payload_list) >= max(1, int(limit)):
                break
        return result_payload_list

    def _load_block_text_candidate_payload_list(
        self,
        repository: Any,
        *,
        query_text: str,
        namespace_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            document_payload_list = repository.load_objects(
                "document",
                {"namespace_id": str(namespace_id)},
                limit=self._COLLECTION_LIMIT,
            )
        except Exception:
            return []

        result_payload_list: list[dict[str, Any]] = []
        for document_payload in document_payload_list:
            if not isinstance(document_payload, Mapping):
                continue
            block_payload_list = document_payload.get("blocks")
            if not isinstance(block_payload_list, Sequence):
                continue
            for block_payload in block_payload_list:
                if not isinstance(block_payload, Mapping):
                    continue
                haystack = json.dumps(_json_safe_object(block_payload), ensure_ascii=False).lower()
                score = self._load_text_match_score(haystack, query_text)
                if score <= 0.0:
                    continue
                result_payload_list.append({"payload": dict(block_payload), "score": score})
                if len(result_payload_list) >= max(1, int(limit)):
                    return result_payload_list
        return result_payload_list

    def _load_vector_candidate_payload_list(
        self,
        repository: Any,
        *,
        owner_type: str,
        query_vector: Sequence[float],
        namespace_id: str,
        limit: int,
        owner_payload_cache: dict[str, dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        try:
            embedding_payload_list = repository.load_objects(
                "embedding",
                {"namespace_id": str(namespace_id), "owner_type": str(owner_type)},
                limit=self._COLLECTION_LIMIT,
            )
        except Exception:
            return []

        owner_payload_map = owner_payload_cache.get(owner_type)
        if owner_payload_map is None:
            owner_payload_map = self._load_owner_payload_map(repository, owner_type=owner_type, namespace_id=namespace_id)
            owner_payload_cache[owner_type] = owner_payload_map

        scored_payload_list: list[dict[str, Any]] = []
        for embedding_payload in embedding_payload_list:
            if not isinstance(embedding_payload, Mapping):
                continue
            owner_id = str(embedding_payload.get("owner_id") or "").strip()
            if not owner_id:
                continue
            owner_payload = owner_payload_map.get(owner_id)
            if owner_payload is None:
                continue
            embedding_vector = self._normalize_vector(embedding_payload.get("embedding"))
            if not embedding_vector:
                continue
            score = self._load_cosine_similarity(query_vector, embedding_vector)
            scored_payload_list.append({"payload": dict(owner_payload), "score": score})

        scored_payload_list.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return scored_payload_list[: max(1, int(limit))]

    def _load_owner_payload_map(
        self,
        repository: Any,
        *,
        owner_type: str,
        namespace_id: str,
    ) -> dict[str, dict[str, Any]]:
        if owner_type == "block":
            return self._load_block_payload_map(repository, namespace_id=namespace_id)
        object_name = "entity" if owner_type == "entity" else "relation"
        try:
            object_payload_list = repository.load_objects(
                object_name,
                {"namespace_id": str(namespace_id)},
                limit=self._COLLECTION_LIMIT,
            )
        except Exception:
            return {}

        payload_map: dict[str, dict[str, Any]] = {}
        for object_payload in object_payload_list:
            if not isinstance(object_payload, Mapping):
                continue
            object_id = str(object_payload.get("_id") or object_payload.get("id") or "").strip()
            if not object_id:
                continue
            payload_map[object_id] = dict(object_payload)
        return payload_map

    def _load_block_payload_map(self, repository: Any, *, namespace_id: str) -> dict[str, dict[str, Any]]:
        try:
            document_payload_list = repository.load_objects(
                "document",
                {"namespace_id": str(namespace_id)},
                limit=self._COLLECTION_LIMIT,
            )
        except Exception:
            return {}

        payload_map: dict[str, dict[str, Any]] = {}
        for document_payload in document_payload_list:
            if not isinstance(document_payload, Mapping):
                continue
            block_payload_list = document_payload.get("blocks")
            if not isinstance(block_payload_list, Sequence):
                continue
            for block_payload in block_payload_list:
                if not isinstance(block_payload, Mapping):
                    continue
                block_id = str(block_payload.get("block_id") or block_payload.get("_id") or block_payload.get("id") or "").strip()
                if not block_id:
                    continue
                payload_map[block_id] = dict(block_payload)
        return payload_map

    def _format_chunk_payload_list(self, candidates: Sequence[Mapping[str, Any]], owner_type: str) -> list[dict[str, Any]]:
        chunk_payload_list: list[dict[str, Any]] = []
        for candidate in candidates:
            payload = candidate.get("payload") if isinstance(candidate.get("payload"), Mapping) else candidate
            score = candidate.get("score") if isinstance(candidate, Mapping) else None
            if not isinstance(payload, Mapping):
                continue
            chunk_payload: dict[str, Any] = {"owner_type": owner_type}
            if owner_type == "block":
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
                chunk_payload.update(
                    {
                        "heading": str(payload.get("heading") or payload.get("block_kind") or "block"),
                        "content": str(payload.get("content") or payload.get("text") or "")[:2000],
                        "source_path": str(metadata.get("source_path") or payload.get("source_path") or ""),
                        "block_kind": str(payload.get("block_kind") or metadata.get("kind") or ""),
                    }
                )
            elif owner_type == "entity":
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
                chunk_payload.update(
                    {
                        "canonical_name": str(payload.get("canonical_name") or payload.get("mention_text") or ""),
                        "entity_type": str(payload.get("entity_type") or ""),
                        "summary": str(payload.get("summary") or "")[:500],
                        "source_path": str(metadata.get("source_path") or payload.get("source_path") or ""),
                    }
                )
            elif owner_type == "relation":
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
                chunk_payload.update(
                    {
                        "relation_type": str(payload.get("relation_type") or ""),
                        "source_entity_id": str(payload.get("source_entity_id") or ""),
                        "target_entity_id": str(payload.get("target_entity_id") or ""),
                        "relation_description": str(payload.get("relation_description") or metadata.get("relation_description") or ""),
                        "source_path": str(metadata.get("source_path") or payload.get("source_path") or ""),
                    }
                )
            else:
                chunk_payload["raw"] = dict(payload)
            if score is not None:
                chunk_payload["score"] = float(score)
            chunk_payload_list.append(chunk_payload)
        return chunk_payload_list

    def _normalize_vector(self, vector_payload: Any) -> list[float]:
        if not isinstance(vector_payload, Sequence) or isinstance(vector_payload, (str, bytes, bytearray)):
            return []
        normalized_vector: list[float] = []
        for item in vector_payload:
            try:
                normalized_vector.append(float(item))
            except Exception:
                return []
        return normalized_vector

    def _load_text_match_score(self, haystack_text: str, query_text: str) -> float:
        normalized_haystack = str(haystack_text or "").strip().lower()
        normalized_query = str(query_text or "").strip().lower()
        if not normalized_haystack or not normalized_query:
            return 0.0
        if normalized_query in normalized_haystack:
            return 1.0
        query_token_list = [
            token
            for token in re.split(r"[^a-z0-9_]+", normalized_query)
            if token
        ]
        if not query_token_list:
            return 0.0
        matched_token_count = sum(1 for token in query_token_list if token in normalized_haystack)
        if matched_token_count <= 0:
            return 0.0
        return matched_token_count / len(query_token_list)

    def _load_cosine_similarity(self, left_vector: Sequence[float], right_vector: Sequence[float]) -> float:
        if not left_vector or not right_vector:
            return 0.0
        dimension = min(len(left_vector), len(right_vector))
        if dimension <= 0:
            return 0.0
        dot_product = 0.0
        left_norm = 0.0
        right_norm = 0.0
        for index in range(dimension):
            left_value = float(left_vector[index])
            right_value = float(right_vector[index])
            dot_product += left_value * right_value
            left_norm += left_value * left_value
            right_norm += right_value * right_value
        if left_norm <= 0.0 or right_norm <= 0.0:
            return 0.0
        return dot_product / (math.sqrt(left_norm) * math.sqrt(right_norm))


AGENT_DB_QUERY_SERVICE = AgentDbQueryService()


def _is_memory_backend_uri(uri: str | None) -> bool:
    normalized_uri = str(uri or "").strip().lower()
    return (
        normalized_uri.startswith("agentsmem://")
        or normalized_uri.startswith("agentsdb://")
        or normalized_uri.startswith("memodb://")
        or normalized_uri.startswith("inmemdb://")
    )


class KnowledgeRepositoryProtocol(Protocol):
    def ensure_index_objects(self) -> None:
        ...

    def upsert_object(self, object_name: str, object_id: str, object_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def delete_object(self, object_name: str, object_id: str) -> bool:
        ...

    def load_object(self, object_name: str, object_id: str) -> dict[str, Any] | None:
        ...

    def load_objects(
        self,
        object_name: str,
        object_filter: Mapping[str, Any] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        ...

    def find_objects(self, *, namespace_id: str, query_text: str, limit: int = 10) -> list[dict[str, Any]]:
        ...

    def load_relation_graph(
        self,
        *,
        namespace_id: str,
        source_entity_id: str,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        ...


class KnowledgeVectorRepositoryProtocol(KnowledgeRepositoryProtocol, Protocol):
    def build_vector_search_pipeline(
        self,
        *,
        query_vector: Sequence[float],
        namespace_id: str,
        owner_type: str = "",
        limit: int = 10,
        num_candidates: int = 100,
        index_name: str = "embedding_cosine",
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class AgentDbRepositoryFactoryConfig:
    backend_uri: str
    default_database_name: str = "alde_knowledge"
    memory_image_path: str | None = None
    prefer_explicit_inmemory: bool = False
    prefer_ui_socket_repository: bool = False


class AgentDbRepositoryFactory:
    """Create repository objects from one backend-selection policy."""

    def __init__(self, config: AgentDbRepositoryFactoryConfig) -> None:
        self._config = config

    def load_repository(self, database_name: str | None = None) -> KnowledgeRepositoryProtocol:
        resolved_database_name = str(database_name or self._config.default_database_name).strip() or self._config.default_database_name
        if _is_agentsdb_socket_uri(self._config.backend_uri):
            socket_repository_class = AgentDbSocketRepository
            if self._config.prefer_ui_socket_repository and UiAgentDbSocketRepository is not None:
                socket_repository_class = UiAgentDbSocketRepository
            return socket_repository_class.create_from_uri(
                self._config.backend_uri,
                resolved_database_name,
            )
        if _is_memory_backend_uri(self._config.backend_uri) and self._config.prefer_explicit_inmemory:
            return AgentDbInMemoryRepository(self._config.memory_image_path)
        return KnowledgeRepository.create_from_uri(
            self._config.backend_uri,
            resolved_database_name,
        )


class AgentDbSocketServerService:
    """Socket server service that exposes KnowledgeRepository commands via JSON-lines."""

    def __init__(self, backend_uri: str, default_database_name: str = "alde_knowledge", memory_image_path: str | None = None) -> None:
        normalized_backend_uri = str(backend_uri or "").strip()
        if not normalized_backend_uri:
            normalized_backend_uri = "agentsdb://local"
        if _is_agentsdb_socket_uri(normalized_backend_uri):
            raise RuntimeError("agentsdb socket server backend URI must not use agentsdb://")
        self._backend_uri = normalized_backend_uri
        self._default_database_name = str(default_database_name or "alde_knowledge").strip() or "alde_knowledge"
        self._memory_image_path = str(memory_image_path or "").strip() or None
        self._repository_cache: dict[str, KnowledgeRepositoryProtocol] = {}
        self._repository_factory = AgentDbRepositoryFactory(
            AgentDbRepositoryFactoryConfig(
                backend_uri=self._backend_uri,
                default_database_name=self._default_database_name,
                memory_image_path=self._memory_image_path,
                prefer_explicit_inmemory=True,
            )
        )
        self._stream_condition = threading.Condition(threading.RLock())
        self._stream_version_by_key: dict[tuple[str, str, str], int] = {}
        self._repository_stream_state_by_database: dict[str, dict[str, Any]] = {}

    @classmethod
    def load_from_env(cls) -> AgentDbSocketServerService:
        connection_config = _load_agentsdb_connection_config()
        backend_uri = str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI", "")
            or os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_URI", ""),
        ).strip()
        if not backend_uri:
            backend_uri = _connection_config_value(connection_config, ("backend_uri", "agents_db_backend_uri", "storage_uri", "storage_backend_uri"))
        if not backend_uri:
            backend_uri = "agentsmem://local"
        memory_image_path = str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_MEMORY_IMAGE_PATH", "")
            or os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_FLUSH_IMAGE_PATH", "")
            or os.path.join("AppData", "agentsdb.json"),
        ).strip()
        if not memory_image_path:
            memory_image_path = _connection_config_value(connection_config, ("memory_image_path", "flush_image_path"))
        database_name = str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAME", "")
            or "alde_knowledge",
        ).strip() or "alde_knowledge"
        if not database_name:
            database_name = _connection_config_value(connection_config, ("database_name", "database")) or "alde_knowledge"
        return cls(backend_uri=backend_uri, default_database_name=database_name, memory_image_path=memory_image_path)

    def load_repository(self, database_name: str | None = None) -> KnowledgeRepositoryProtocol:
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        repository = self._repository_cache.get(resolved_database_name)
        if repository is not None:
            return repository
        repository = self._repository_factory.load_repository(resolved_database_name)
        self._repository_cache[resolved_database_name] = repository
        return repository

    def _stream_key(self, *, database_name: str | None, object_name: str, object_id: str) -> tuple[str, str, str]:
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        return (resolved_database_name, str(object_name or "").strip(), str(object_id or "").strip())

    def _repository_stream_key(self, *, database_name: str | None) -> tuple[str, str, str]:
        return self._stream_key(
            database_name=database_name,
            object_name="__repository__",
            object_id="__all__",
        )

    def _load_repository_stream_state(self, *, database_name: str | None = None) -> dict[str, Any] | None:
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        with self._stream_condition:
            state_payload = self._repository_stream_state_by_database.get(resolved_database_name)
            return dict(state_payload) if isinstance(state_payload, Mapping) else None

    def _stream_version(self, *, database_name: str | None, object_name: str, object_id: str) -> int:
        stream_key = self._stream_key(database_name=database_name, object_name=object_name, object_id=object_id)
        with self._stream_condition:
            return int(self._stream_version_by_key.get(stream_key, 0))

    def _notify_stream_update(
        self,
        *,
        database_name: str | None,
        object_name: str,
        object_id: str,
        change: Mapping[str, Any] | None = None,
    ) -> None:
        stream_key = self._stream_key(database_name=database_name, object_name=object_name, object_id=object_id)
        repository_stream_key = self._repository_stream_key(database_name=database_name)
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        resolved_object_name = str(object_name or "").strip()
        resolved_object_id = str(object_id or "").strip()
        with self._stream_condition:
            self._stream_version_by_key[stream_key] = int(self._stream_version_by_key.get(stream_key, 0)) + 1
            self._stream_version_by_key[repository_stream_key] = int(self._stream_version_by_key.get(repository_stream_key, 0)) + 1
            updated_at = _now_utc().isoformat()
            safe_change = _json_safe_object(
                dict(change)
                if isinstance(change, Mapping)
                else {
                    "action": "update",
                    "object_name": resolved_object_name,
                    "object_id": resolved_object_id,
                }
            )
            repository_version = int(self._stream_version_by_key.get(repository_stream_key, 0))
            event_seed = json.dumps(
                {
                    "database_name": resolved_database_name,
                    "object_name": resolved_object_name,
                    "object_id": resolved_object_id,
                    "repository_version": repository_version,
                    "updated_at": updated_at,
                    "change": safe_change,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            event_id = hashlib.sha256(event_seed.encode("utf-8")).hexdigest()[:32]
            self._repository_stream_state_by_database[resolved_database_name] = {
                "ok": True,
                "stream": "repository_update",
                "database_name": resolved_database_name,
                "object_name": resolved_object_name,
                "object_id": resolved_object_id,
                "stream_cursor": {
                    "event_id": event_id,
                    "updated_at": updated_at,
                },
                "change": safe_change,
            }
            self._stream_condition.notify_all()

    def _wait_for_stream_update(
        self,
        *,
        database_name: str | None,
        object_name: str,
        object_id: str,
        last_version: int,
        timeout_seconds: float,
    ) -> int:
        stream_key = self._stream_key(database_name=database_name, object_name=object_name, object_id=object_id)
        with self._stream_condition:
            current_version = int(self._stream_version_by_key.get(stream_key, 0))
            if current_version != last_version:
                return current_version
            self._stream_condition.wait(timeout=max(float(timeout_seconds), 0.1))
            return int(self._stream_version_by_key.get(stream_key, 0))

    def _load_tree_stream_state(
        self,
        *,
        tree_object_id: str,
        database_name: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_tree_object_id = str(tree_object_id or "").strip()
        if not normalized_tree_object_id:
            return None
        repository = self.load_repository(database_name)
        tree_record = repository.load_object("document", normalized_tree_object_id)
        if not isinstance(tree_record, Mapping):
            return None
        head_payload = repository.load_object("document", f"{normalized_tree_object_id}:stream:head")
        tree_data = tree_record.get("tree_data")
        if not isinstance(tree_data, Mapping):
            tree_data = tree_record.get("data")
        if not isinstance(tree_data, Mapping):
            return None

        head_mapping = dict(head_payload) if isinstance(head_payload, Mapping) else {}
        event_id = str(
            head_mapping.get("event_id")
            or tree_record.get("last_stream_event_id")
            or tree_record.get("content_sha256")
            or tree_record.get("updated_at")
            or ""
        ).strip()
        updated_at = str(head_mapping.get("updated_at") or tree_record.get("updated_at") or tree_record.get("created_at") or "").strip()
        tree_hash = str(head_mapping.get("tree_hash") or tree_record.get("content_sha256") or "").strip()
        change_payload = head_mapping.get("change") if isinstance(head_mapping.get("change"), Mapping) else None
        return {
            "ok": True,
            "stream": "tree_update",
            "tree_object_id": normalized_tree_object_id,
            "tree_data": dict(tree_data),
            "stream_cursor": {
                "event_id": event_id,
                "updated_at": updated_at,
                "tree_hash": tree_hash,
            },
            "change": dict(change_payload) if isinstance(change_payload, Mapping) else None,
        }

    def iter_tree_stream(
        self,
        *,
        tree_object_id: str,
        database_name: str | None = None,
        last_event_id: str | None = None,
        heartbeat_seconds: float = 30.0,
        stop_event: threading.Event | None = None,
    ) -> Iterable[dict[str, Any]]:
        normalized_tree_object_id = str(tree_object_id or "").strip()
        if not normalized_tree_object_id:
            raise ValueError("subscribe_tree_stream requires tree_object_id")
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        heartbeat_timeout = max(float(heartbeat_seconds), 1.0)
        last_seen_event_id = str(last_event_id or "").strip() or None
        last_version = self._stream_version(
            database_name=resolved_database_name,
            object_name="document",
            object_id=normalized_tree_object_id,
        )

        yield {
            "ok": True,
            "subscribed": True,
            "stream": "tree_subscription",
            "database_name": resolved_database_name,
            "tree_object_id": normalized_tree_object_id,
            "last_event_id": last_seen_event_id,
        }

        while stop_event is None or not stop_event.is_set():
            stream_state = self._load_tree_stream_state(
                tree_object_id=normalized_tree_object_id,
                database_name=resolved_database_name,
            )
            stream_cursor = stream_state.get("stream_cursor") if isinstance(stream_state, Mapping) else None
            current_event_id = str((stream_cursor or {}).get("event_id") or "").strip()
            if stream_state is not None and current_event_id and current_event_id != last_seen_event_id:
                yield dict(stream_state)
                last_seen_event_id = current_event_id
                last_version = self._stream_version(
                    database_name=resolved_database_name,
                    object_name="document",
                    object_id=normalized_tree_object_id,
                )
                continue

            next_version = self._wait_for_stream_update(
                database_name=resolved_database_name,
                object_name="document",
                object_id=normalized_tree_object_id,
                last_version=last_version,
                timeout_seconds=heartbeat_timeout,
            )
            if next_version == last_version:
                yield {
                    "ok": True,
                    "heartbeat": True,
                    "stream": "tree_heartbeat",
                    "database_name": resolved_database_name,
                    "tree_object_id": normalized_tree_object_id,
                }
            last_version = next_version

    def iter_repository_stream(
        self,
        *,
        database_name: str | None = None,
        last_event_id: str | None = None,
        heartbeat_seconds: float = 30.0,
        stop_event: threading.Event | None = None,
        object_names: Sequence[str] | None = None,
    ) -> Iterable[dict[str, Any]]:
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        heartbeat_timeout = max(float(heartbeat_seconds), 1.0)
        normalized_object_name_set = {
            str(object_name or "").strip().lower()
            for object_name in (object_names or [])
            if str(object_name or "").strip()
        }
        last_seen_event_id = str(last_event_id or "").strip() or None
        last_version = self._stream_version(
            database_name=resolved_database_name,
            object_name="__repository__",
            object_id="__all__",
        )

        yield {
            "ok": True,
            "subscribed": True,
            "stream": "repository_subscription",
            "database_name": resolved_database_name,
            "object_names": sorted(normalized_object_name_set),
            "last_event_id": last_seen_event_id,
        }

        while stop_event is None or not stop_event.is_set():
            stream_state = self._load_repository_stream_state(database_name=resolved_database_name)
            stream_cursor = stream_state.get("stream_cursor") if isinstance(stream_state, Mapping) else None
            current_event_id = str((stream_cursor or {}).get("event_id") or "").strip()
            current_object_name = str((stream_state or {}).get("object_name") or "").strip().lower()
            if stream_state is not None and current_event_id and current_event_id != last_seen_event_id:
                last_seen_event_id = current_event_id
                last_version = self._stream_version(
                    database_name=resolved_database_name,
                    object_name="__repository__",
                    object_id="__all__",
                )
                if not normalized_object_name_set or current_object_name in normalized_object_name_set:
                    yield dict(stream_state)
                continue

            next_version = self._wait_for_stream_update(
                database_name=resolved_database_name,
                object_name="__repository__",
                object_id="__all__",
                last_version=last_version,
                timeout_seconds=heartbeat_timeout,
            )
            if next_version == last_version:
                yield {
                    "ok": True,
                    "heartbeat": True,
                    "stream": "repository_heartbeat",
                    "database_name": resolved_database_name,
                    "object_names": sorted(normalized_object_name_set),
                }
            last_version = next_version

    def dispatch_object(self, cmd: str, payload: Mapping[str, Any], database_name: str | None = None) -> dict[str, Any]:
        normalized_cmd = str(cmd or "").strip().lower()
        if normalized_cmd == "health":
            return {
                "ok": True,
                "status": "ok",
                "backend": "agents_db",
                "storage_backend": "inmemory" if _is_memory_backend_uri(self._backend_uri) else "dict",
                "database_name": str(database_name or self._default_database_name),
            }
        repository = self.load_repository(database_name)
        if normalized_cmd == "ensure_index_objects":
            repository.ensure_index_objects()
            return {"ok": True, "ensured": True}
        if normalized_cmd == "upsert_object":
            object_name = str(payload.get("object_name") or "").strip()
            object_id = str(payload.get("object_id") or "").strip()
            object_payload = payload.get("object_payload")
            if not object_name or not object_id or not isinstance(object_payload, Mapping):
                raise ValueError("upsert_object requires object_name, object_id, and object_payload")
            stored_payload = repository.upsert_object(object_name, object_id, dict(object_payload))
            self._notify_stream_update(
                database_name=database_name,
                object_name=object_name,
                object_id=object_id,
                change={
                    "action": "upsert",
                    "object_name": object_name,
                    "object_id": object_id,
                },
            )
            return {"ok": True, "object_payload": _json_safe_object(stored_payload)}
        if normalized_cmd == "delete_object":
            object_name = str(payload.get("object_name") or "").strip()
            object_id = str(payload.get("object_id") or "").strip()
            if not object_name or not object_id:
                raise ValueError("delete_object requires object_name and object_id")
            deleted = repository.delete_object(object_name, object_id)
            self._notify_stream_update(
                database_name=database_name,
                object_name=object_name,
                object_id=object_id,
                change={
                    "action": "delete",
                    "object_name": object_name,
                    "object_id": object_id,
                    "deleted": bool(deleted),
                },
            )
            return {"ok": True, "deleted": bool(deleted)}
        if normalized_cmd == "load_object":
            object_name = str(payload.get("object_name") or "").strip()
            object_id = str(payload.get("object_id") or "").strip()
            if not object_name or not object_id:
                raise ValueError("load_object requires object_name and object_id")
            object_payload = repository.load_object(object_name, object_id)
            return {"ok": True, "object_payload": _json_safe_object(object_payload) if object_payload is not None else None}
        if normalized_cmd == "load_objects":
            object_name = str(payload.get("object_name") or "").strip()
            object_filter = payload.get("object_filter")
            limit_provided = "limit" in payload
            limit = payload.get("limit", 50)
            if not object_name:
                raise ValueError("load_objects requires object_name")
            if object_filter is not None and not isinstance(object_filter, Mapping):
                raise ValueError("load_objects object_filter must be an object")
            normalized_limit = _normalize_limit_value(limit)
            if normalized_limit is None and not limit_provided:
                normalized_limit = 50
            object_payload_list = repository.load_objects(
                object_name,
                dict(object_filter or {}),
                normalized_limit,
            )
            return {"ok": True, "object_payload_list": _json_safe_object(object_payload_list)}
        if normalized_cmd == "find_objects":
            namespace_id = str(payload.get("namespace_id") or "").strip()
            query_text = str(payload.get("query_text") or "").strip()
            limit = payload.get("limit", 10)
            if not namespace_id or not query_text:
                raise ValueError("find_objects requires namespace_id and query_text")
            object_payload_list = repository.find_objects(
                namespace_id=namespace_id,
                query_text=query_text,
                limit=max(1, int(limit)),
            )
            return {"ok": True, "object_payload_list": _json_safe_object(object_payload_list)}
        if normalized_cmd in {"query", "adb_query"}:
            query_text = str(payload.get("query") or payload.get("query_text") or payload.get("user_question") or "").strip()
            owner_types = payload.get("owner_types")
            if owner_types is None:
                owner_types = payload.get("owner_type")
            limit = payload.get("limit", 10)
            namespace_id = str(payload.get("namespace_id") or "").strip() or None
            use_vector_payload: Any = True
            for candidate_key in ("use_vector", "used_vector_search", "vector_search"):
                if candidate_key in payload:
                    use_vector_payload = payload.get(candidate_key)
                    break
            parsed_use_vector = _load_bool_value(use_vector_payload)
            if parsed_use_vector is None:
                if isinstance(use_vector_payload, str):
                    use_vector = str(use_vector_payload).strip().lower() not in {"", "0", "false", "no", "off"}
                else:
                    use_vector = bool(use_vector_payload)
            else:
                use_vector = parsed_use_vector
            job_name = str(payload.get("job_name") or "").strip() or None
            target_agent = str(payload.get("target_agent") or "").strip() or None
            tool_name = str(payload.get("tool_name") or "").strip() or None
            if not query_text:
                raise ValueError("query requires query or query_text")
            query_method = getattr(repository, "query", None)
            if not callable(query_method):
                raise ValueError("repository does not support query")
            result_payload = query_method(
                query_text,
                owner_types=owner_types,
                limit=max(1, int(limit)),
                namespace_id=namespace_id,
                use_vector=use_vector,
                job_name=job_name,
                target_agent=target_agent,
                tool_name=tool_name,
            )
            return dict(result_payload) if isinstance(result_payload, Mapping) else {"ok": True, "result": _json_safe_object(result_payload)}
        if normalized_cmd == "load_relation_graph":
            namespace_id = str(payload.get("namespace_id") or "").strip()
            source_entity_id = str(payload.get("source_entity_id") or "").strip()
            max_depth = payload.get("max_depth", 2)
            if not namespace_id or not source_entity_id:
                raise ValueError("load_relation_graph requires namespace_id and source_entity_id")
            object_payload_list = repository.load_relation_graph(
                namespace_id=namespace_id,
                source_entity_id=source_entity_id,
                max_depth=max(0, int(max_depth)),
            )
            return {"ok": True, "object_payload_list": _json_safe_object(object_payload_list)}
        if normalized_cmd == "apply_operations":
            operations = payload.get("operations")
            if not isinstance(operations, Sequence):
                raise ValueError("apply_operations requires operations list")
            applied = 0
            deleted = 0
            results: list[dict[str, Any]] = []
            touched_object_key_set: set[tuple[str, str]] = set()
            flush_context = getattr(repository, "deferred_flush", None)
            if not callable(flush_context):
                flush_context = getattr(repository, "deferred_write_queue", None)
            with (flush_context() if callable(flush_context) else nullcontext()):
                for operation in operations:
                    if not isinstance(operation, Mapping):
                        continue
                    action_name = str(operation.get("action") or "").strip().lower()
                    object_name = str(operation.get("object_name") or "").strip()
                    object_id = str(operation.get("object_id") or "").strip()
                    if action_name == "upsert":
                        object_payload = operation.get("object_payload")
                        if not object_name or not object_id or not isinstance(object_payload, Mapping):
                            continue
                        repository.upsert_object(object_name, object_id, dict(object_payload))
                        touched_object_key_set.add((object_name, object_id))
                        applied += 1
                        results.append({"action": "upsert", "object_name": object_name, "object_id": object_id, "ok": True})
                        continue
                    if action_name == "delete":
                        if not object_name or not object_id:
                            continue
                        deleted_flag = bool(repository.delete_object(object_name, object_id))
                        touched_object_key_set.add((object_name, object_id))
                        applied += 1
                        if deleted_flag:
                            deleted += 1
                        results.append({"action": "delete", "object_name": object_name, "object_id": object_id, "deleted": deleted_flag, "ok": True})
            for object_name, object_id in sorted(touched_object_key_set):
                self._notify_stream_update(
                    database_name=database_name,
                    object_name=object_name,
                    object_id=object_id,
                    change={
                        "action": "apply_operations",
                        "object_name": object_name,
                        "object_id": object_id,
                    },
                )
            return {"ok": True, "applied": applied, "deleted": deleted, "results": results}
        raise ValueError(f"unknown cmd: {normalized_cmd or '<empty>'}")


def _parse_agentsdb_socket_request_line(raw_line: bytes) -> tuple[str, str | None, dict[str, Any]]:
    decoded_line = raw_line.decode("utf-8", errors="replace")
    normalized_line = decoded_line.strip()
    if not normalized_line:
        return "health", None, {}

    normalized_command = normalized_line.lower()
    if normalized_command in {"health", "ping", "status"}:
        return "health", None, {}
    if normalized_command.startswith("cmd="):
        legacy_cmd = normalized_command.partition("=")[2].strip()
        if legacy_cmd in {"ping", "status"}:
            legacy_cmd = "health"
        return legacy_cmd, None, {}
    if normalized_command.startswith(("get ", "head ", "options ")):
        return "health", None, {}

    try:
        request_payload = json.loads(normalized_line)
    except Exception as exc:
        raise ValueError("request payload must be a JSON object") from exc

    if not isinstance(request_payload, Mapping):
        raise ValueError("request payload must be a JSON object")

    cmd = str(request_payload.get("cmd") or "").strip()
    database_name = str(request_payload.get("database_name") or "").strip() or None
    payload = request_payload.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        raise ValueError("payload must be a JSON object")
    return cmd, database_name, dict(payload or {})


class _AgentDbSocketRequestHandler(socketserver.StreamRequestHandler):
    def _write_response(self, response_payload: Mapping[str, Any]) -> None:
        self.wfile.write((json.dumps(_json_safe_object(dict(response_payload)), separators=(",", ":")) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _handle_tree_stream(
        self,
        service: AgentDbSocketServerService,
        *,
        payload: Mapping[str, Any],
        database_name: str | None,
    ) -> None:
        tree_object_id = str(payload.get("tree_object_id") or "").strip()
        last_event_id = str(payload.get("last_event_id") or "").strip() or None
        heartbeat_seconds = payload.get("heartbeat_seconds", 30.0)
        try:
            heartbeat_value = max(float(heartbeat_seconds), 1.0)
        except Exception:
            heartbeat_value = 30.0
        for response_payload in service.iter_tree_stream(
            tree_object_id=tree_object_id,
            database_name=database_name,
            last_event_id=last_event_id,
            heartbeat_seconds=heartbeat_value,
        ):
            try:
                self._write_response(response_payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def _handle_repository_stream(
        self,
        service: AgentDbSocketServerService,
        *,
        payload: Mapping[str, Any],
        database_name: str | None,
    ) -> None:
        last_event_id = str(payload.get("last_event_id") or "").strip() or None
        heartbeat_seconds = payload.get("heartbeat_seconds", 30.0)
        object_names = payload.get("object_names")
        try:
            heartbeat_value = max(float(heartbeat_seconds), 1.0)
        except Exception:
            heartbeat_value = 30.0
        normalized_object_names = [
            str(object_name or "").strip().lower()
            for object_name in (object_names if isinstance(object_names, Sequence) and not isinstance(object_names, (str, bytes, bytearray)) else [])
            if str(object_name or "").strip()
        ]
        for response_payload in service.iter_repository_stream(
            database_name=database_name,
            last_event_id=last_event_id,
            heartbeat_seconds=heartbeat_value,
            object_names=normalized_object_names,
        ):
            try:
                self._write_response(response_payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def handle(self) -> None:
        service: AgentDbSocketServerService = self.server.service
        raw_line = self.rfile.readline()
        if not raw_line:
            return
        response_payload: dict[str, Any]
        try:
            cmd, database_name, payload = _parse_agentsdb_socket_request_line(raw_line)
            if str(cmd or "").strip().lower() == "subscribe_tree_stream":
                self._handle_tree_stream(service, payload=payload, database_name=database_name)
                return
            if str(cmd or "").strip().lower() == "subscribe_repository_stream":
                self._handle_repository_stream(service, payload=payload, database_name=database_name)
                return
            response_payload = service.dispatch_object(cmd=cmd, payload=payload, database_name=database_name)
        except Exception as exc:
            response_payload = {
                "ok": False,
                "error": "agents_db_socket_request_failed",
                "detail": str(exc),
            }
        try:
            self._write_response(response_payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected before receiving the response; treat as benign.
            return


class _AgentDbSocketTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], request_handler_class: type[_AgentDbSocketRequestHandler], service: AgentDbSocketServerService) -> None:
        self.service = service
        super().__init__(server_address, request_handler_class)


def run_agentsdb_socket_server(
    *,
    host: str = "localhost",
    port: int = 2331,
    backend_uri: str,
    database_name: str = "alde_knowledge",
) -> None:
    service = AgentDbSocketServerService(backend_uri=backend_uri, default_database_name=database_name)
    with _AgentDbSocketTCPServer((str(host).strip() or "localhost", int(port)), _AgentDbSocketRequestHandler, service) as server:
        server.serve_forever()


def run_agentsdb_socket_server_from_env(host: str | None = None, port: int | None = None) -> None:
    connection_config = _load_agentsdb_connection_config()
    agents_db_uri = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_URI", "")).strip()
    if not agents_db_uri:
        agents_db_uri = _load_agentsdb_uri_from_connection_config(connection_config)
    endpoint = _load_agentsdb_socket_endpoint(agents_db_uri or "agentsdb://localhost:2331")
    resolved_host = str(host or (endpoint[1] if endpoint is not None else "localhost")).strip() or "localhost"
    resolved_port = int(port or (endpoint[2] if endpoint is not None else 2331))
    service = AgentDbSocketServerService.load_from_env()
    with _AgentDbSocketTCPServer((resolved_host, resolved_port), _AgentDbSocketRequestHandler, service) as server:
        server.serve_forever()


class KnowledgeObjectService:
    """Domain service for storing and querying the knowledge model."""

    _VECTOR_COLLECTION_LIMIT = 10000

    def __init__(self, repository: KnowledgeVectorRepositoryProtocol) -> None:
        self._repository = repository

    def store_object(
        self,
        *,
        object_name: str,
        object_id: str,
        object_payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._repository.upsert_object(object_name, object_id, object_payload)

    def store_namespace_object(self, namespace_object: NamespaceObject) -> Mapping[str, Any]:
        return self.store_object(
            object_name="namespace",
            object_id=namespace_object.id,
            object_payload=_dataclass_payload(namespace_object),
        )

    def store_entity_object(self, entity_object: EntityObject) -> Mapping[str, Any]:
        return self.store_object(
            object_name="entity",
            object_id=entity_object.id,
            object_payload=_dataclass_payload(entity_object),
        )

    def store_document_object(self, document_object: DocumentObject) -> Mapping[str, Any]:
        return self.store_object(
            object_name="document",
            object_id=document_object.id,
            object_payload=_dataclass_payload(document_object),
        )

    def store_relation_object(self, relation_object: EntityRelationObject) -> Mapping[str, Any]:
        return self.store_object(
            object_name="relation",
            object_id=relation_object.id,
            object_payload=_dataclass_payload(relation_object),
        )

    def store_embedding_object(self, embedding_object: EmbeddingObject) -> Mapping[str, Any]:
        object_id = ":".join(
            [
                embedding_object.namespace_id,
                embedding_object.owner_type,
                embedding_object.owner_id,
                embedding_object.model_id,
            ],
        )
        return self.store_object(
            object_name="embedding",
            object_id=object_id,
            object_payload=_dataclass_payload(embedding_object),
        )

    def store_retrieval_run_object(self, retrieval_run_object: RetrievalRunObject) -> Mapping[str, Any]:
        return self.store_object(
            object_name="retrieval_run",
            object_id=retrieval_run_object.id,
            object_payload=_dataclass_payload(retrieval_run_object),
        )

    def store_dispatcher_run_object(self, dispatcher_run_object: DispatcherRunObject) -> Mapping[str, Any]:
        return self.store_object(
            object_name="dispatcher_run",
            object_id=dispatcher_run_object.id,
            object_payload=_dataclass_payload(dispatcher_run_object),
        )

    def find_objects(self, *, namespace_id: str, query_text: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._repository.find_objects(namespace_id=namespace_id, query_text=query_text, limit=limit)

    def load_relation_object_graph(
        self,
        *,
        namespace_id: str,
        source_entity_id: str,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        return self._repository.load_relation_graph(
            namespace_id=namespace_id,
            source_entity_id=source_entity_id,
            max_depth=max_depth,
        )

    def build_vector_candidate_pipeline( 
        self,
        *,
        query_vector: Sequence[float],
        namespace_id: str,
        owner_type: str = "block",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized_owner_type = str(owner_type or "").strip().lower()
        if normalized_owner_type not in {"block", "entity", "relation"}:
            return []

        owner_payload_map = self._load_owner_payload_map(
            owner_type=normalized_owner_type,
            namespace_id=namespace_id,
        )
        if not owner_payload_map:
            return []

        try:
            embedding_payload_list = self._repository.load_objects(
                "embedding",
                {"namespace_id": str(namespace_id), "owner_type": normalized_owner_type},
                limit=self._VECTOR_COLLECTION_LIMIT,
            )
        except Exception:
            return []

        normalized_query_vector = self._normalize_vector(query_vector)
        if not normalized_query_vector:
            return []

        scored_payload_list: list[dict[str, Any]] = []
        for embedding_payload in embedding_payload_list:
            if not isinstance(embedding_payload, Mapping):
                continue
            owner_id = str(embedding_payload.get("owner_id") or "").strip()
            if not owner_id:
                continue
            owner_payload = owner_payload_map.get(owner_id)
            if owner_payload is None:
                continue
            embedding_vector = self._normalize_vector(embedding_payload.get("embedding"))
            if not embedding_vector:
                continue
            score = self._load_cosine_similarity(normalized_query_vector, embedding_vector)
            scored_payload_list.append({
                "payload": dict(owner_payload),
                "score": score,
            })

        scored_payload_list.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return scored_payload_list[: max(1, int(limit))]

    def load_learning_rank_profile(
        self,
        *,
        namespace_id: str,
        query_text: str,
        tool_name: str | None = None,
        limit: int = 120,
    ) -> dict[str, Any]:
        normalized_query_text = str(query_text or "").strip()
        if not normalized_query_text:
            return {
                "query_text": "",
                "matched_runs": 0,
                "successful_runs": 0,
                "owner_type_boost": {},
                "term_weights": {},
            }

        try:
            retrieval_run_payload_list = self._repository.load_objects(
                "retrieval_run",
                {"namespace_id": str(namespace_id)},
                limit=max(1, int(limit)),
            )
        except Exception:
            retrieval_run_payload_list = []

        query_token_set = self._tokenize_learning_text(normalized_query_text)
        owner_type_weight_map = {"block": 0.0, "entity": 0.0, "relation": 0.0}
        term_weight_map: dict[str, float] = {}
        matched_runs = 0
        successful_runs = 0
        normalized_tool_name = str(tool_name or "").strip().lower()

        for retrieval_run_payload in retrieval_run_payload_list:
            if not isinstance(retrieval_run_payload, Mapping):
                continue
            filters_payload = (
                retrieval_run_payload.get("filters")
                if isinstance(retrieval_run_payload.get("filters"), Mapping)
                else {}
            )
            run_tool_name = str(filters_payload.get("tool_name") or "").strip().lower()
            if normalized_tool_name and run_tool_name and run_tool_name != normalized_tool_name:
                continue

            learning_signal = bool(filters_payload.get("learning_signal"))
            success_value = filters_payload.get("success")
            success_signal = bool(success_value) if success_value is not None else False
            if not learning_signal and not success_signal:
                continue

            run_query_text = str(retrieval_run_payload.get("query_text") or "").strip()
            model_result_excerpt = str(filters_payload.get("model_result_excerpt") or "").strip()
            query_similarity = self._load_prompt_similarity_score(normalized_query_text, run_query_text)
            model_similarity = self._load_prompt_similarity_score(normalized_query_text, model_result_excerpt)
            run_similarity = max(query_similarity, model_similarity * 0.9)
            if run_similarity <= 0.0:
                continue

            matched_runs += 1
            if success_signal:
                successful_runs += 1

            signal_weight = run_similarity * (1.15 if learning_signal else 1.0)
            result_payload_list = retrieval_run_payload.get("results")
            if not isinstance(result_payload_list, Sequence) or isinstance(result_payload_list, (str, bytes, bytearray)):
                result_payload_list = []

            for result_payload in result_payload_list:
                if not isinstance(result_payload, Mapping):
                    continue
                if result_payload.get("chosen") is False:
                    continue

                result_type = str(result_payload.get("result_type") or "").strip().lower()
                if result_type in owner_type_weight_map:
                    owner_type_weight_map[result_type] += signal_weight

                metadata_payload = (
                    result_payload.get("metadata")
                    if isinstance(result_payload.get("metadata"), Mapping)
                    else {}
                )
                text_samples = [
                    run_query_text,
                    model_result_excerpt,
                    str(metadata_payload.get("heading") or ""),
                    str(metadata_payload.get("content") or ""),
                    str(metadata_payload.get("summary") or ""),
                    str(metadata_payload.get("relation_description") or ""),
                    str(metadata_payload.get("canonical_name") or ""),
                    str(metadata_payload.get("source_path") or ""),
                    str(metadata_payload.get("entity_type") or ""),
                    str(metadata_payload.get("relation_type") or ""),
                ]
                for text_sample in text_samples:
                    for token in self._tokenize_learning_text(text_sample):
                        if query_token_set and token not in query_token_set and len(token) <= 3:
                            continue
                        term_weight_map[token] = float(term_weight_map.get(token) or 0.0) + signal_weight

        max_owner_weight = max(owner_type_weight_map.values()) if owner_type_weight_map else 0.0
        if max_owner_weight > 0.0:
            normalized_owner_type_boost = {
                owner_type: round(float(weight) / max_owner_weight, 6)
                for owner_type, weight in owner_type_weight_map.items()
                if float(weight) > 0.0
            }
        else:
            normalized_owner_type_boost = {}

        sorted_term_weights = sorted(
            (
                (token, float(weight))
                for token, weight in term_weight_map.items()
                if str(token).strip() and float(weight) > 0.0
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if len(sorted_term_weights) > 80:
            sorted_term_weights = sorted_term_weights[:80]
        term_weights = {token: round(weight, 6) for token, weight in sorted_term_weights}

        return {
            "query_text": normalized_query_text,
            "matched_runs": int(matched_runs),
            "successful_runs": int(successful_runs),
            "owner_type_boost": normalized_owner_type_boost,
            "term_weights": term_weights,
        }

    def _tokenize_learning_text(self, text: str) -> set[str]:
        normalized_text = str(text or "").strip().lower()
        if not normalized_text:
            return set()
        return {
            token
            for token in re.findall(r"[a-z0-9_]{3,}", normalized_text)
            if token
        }

    def _load_prompt_similarity_score(self, left_text: str, right_text: str) -> float:
        left_tokens = self._tokenize_learning_text(left_text)
        right_tokens = self._tokenize_learning_text(right_text)
        if not left_tokens or not right_tokens:
            return 0.0
        intersection_size = len(left_tokens.intersection(right_tokens))
        union_size = len(left_tokens.union(right_tokens))
        if union_size <= 0:
            return 0.0
        return float(intersection_size) / float(union_size)

    def _load_owner_payload_map(
        self,
        *,
        owner_type: str,
        namespace_id: str,
    ) -> dict[str, dict[str, Any]]:
        if owner_type == "block":
            return self._load_block_payload_map(namespace_id=namespace_id)

        object_name = "entity" if owner_type == "entity" else "relation"
        try:
            object_payload_list = self._repository.load_objects(
                object_name,
                {"namespace_id": str(namespace_id)},
                limit=self._VECTOR_COLLECTION_LIMIT,
            )
        except Exception:
            return {}

        payload_map: dict[str, dict[str, Any]] = {}
        for object_payload in object_payload_list:
            if not isinstance(object_payload, Mapping):
                continue
            object_id = str(object_payload.get("_id") or object_payload.get("id") or "").strip()
            if not object_id:
                continue
            payload_map[object_id] = dict(object_payload)
        return payload_map

    def _load_block_payload_map(self, *, namespace_id: str) -> dict[str, dict[str, Any]]:
        try:
            document_payload_list = self._repository.load_objects(
                "document",
                {"namespace_id": str(namespace_id)},
                limit=self._VECTOR_COLLECTION_LIMIT,
            )
        except Exception:
            return {}

        payload_map: dict[str, dict[str, Any]] = {}
        for document_payload in document_payload_list:
            if not isinstance(document_payload, Mapping):
                continue
            block_payload_list = document_payload.get("blocks")
            if not isinstance(block_payload_list, Sequence):
                continue
            for block_payload in block_payload_list:
                if not isinstance(block_payload, Mapping):
                    continue
                block_id = str(block_payload.get("block_id") or block_payload.get("_id") or block_payload.get("id") or "").strip()
                if not block_id:
                    continue
                payload_map[block_id] = dict(block_payload)
        return payload_map

    def _normalize_vector(self, vector_payload: Any) -> list[float]:
        if not isinstance(vector_payload, Sequence) or isinstance(vector_payload, (str, bytes, bytearray)):
            return []
        normalized_vector: list[float] = []
        for item in vector_payload:
            try:
                normalized_vector.append(float(item))
            except Exception:
                return []
        return normalized_vector

    def _load_cosine_similarity(self, left_vector: Sequence[float], right_vector: Sequence[float]) -> float:
        if not left_vector or not right_vector:
            return 0.0
        dimension = min(len(left_vector), len(right_vector))
        if dimension <= 0:
            return 0.0
        dot_product = 0.0
        left_norm = 0.0
        right_norm = 0.0
        for index in range(dimension):
            left_value = float(left_vector[index])
            right_value = float(right_vector[index])
            dot_product += left_value * right_value
            left_norm += left_value * left_value
            right_norm += right_value * right_value
        if left_norm <= 0.0 or right_norm <= 0.0:
            return 0.0
        return dot_product / (math.sqrt(left_norm) * math.sqrt(right_norm))


class EntityRelationEmbeddingService:
    """Domain service for generating and persisting embeddings from EntityObjects and EntityRelationObjects.

    Pattern: Domain -> Object -> Function
    - Domain:    entity/relation knowledge graph
    - Object:    EntityRelationEmbeddingService
    - Functions: build_object_text, embed_object, store_object, process_object

    All methods accept object_name ("entity" or "relation") as an explicit
    parameter so that one generic service handles both owner types without
    object-specific branching.

    Environment variables:
        AI_IDE_ENTITY_EMBEDDING_MODEL  – HuggingFace model name (default: paraphrase-multilingual-MiniLM-L12-v2)
        AI_IDE_EMBEDDINGS_DEVICE       – "cpu", "cuda", "cuda:N" or "auto" (default: auto)
    """

    _DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    def __init__(
        self,
        knowledge_service: KnowledgeObjectService,
        runtime_config: RuntimeConfigObject,
    ) -> None:
        self._knowledge_service = knowledge_service
        self._runtime_config = runtime_config
        self._encoder: Any = None
        self._encoder_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Encoder lifecycle
    # ------------------------------------------------------------------

    def _model_name(self) -> str:
        return str(
            os.getenv("AI_IDE_ENTITY_EMBEDDING_MODEL", "").strip()
            or self._DEFAULT_MODEL
        )

    def _select_device(self) -> str:
        desired = str(os.getenv("AI_IDE_EMBEDDINGS_DEVICE", "auto") or "auto").strip()
        if desired and desired.lower() != "auto":
            return desired
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def _load_encoder(self) -> Any:
        """Lazy-load HuggingFaceEmbeddings on first use."""
        if self._encoder is not None:
            return self._encoder
        with self._encoder_lock:
            if self._encoder is not None:
                return self._encoder
            try:
                from langchain_huggingface import HuggingFaceEmbeddings  # type: ignore
            except ImportError:
                from langchain_community.embeddings import HuggingFaceEmbeddings  # type: ignore
            device = self._select_device()
            model_name = self._model_name()
            try:
                self._encoder = HuggingFaceEmbeddings(
                    model_name=model_name,
                    model_kwargs={"device": device},
                )
            except TypeError:
                self._encoder = HuggingFaceEmbeddings(model_name=model_name)
            return self._encoder

    # ------------------------------------------------------------------
    # Domain: build canonical text representation
    # ------------------------------------------------------------------

    def build_object_text(self, object_name: str, obj: dict[str, Any]) -> str:
        """Build a stable, canonical text representation for entity or relation dicts.

        Args:
            object_name: "entity" or "relation"
            obj:         raw dict payload from EntityObject or EntityRelationObject
        Returns:
            Pipe-delimited string of all semantically relevant fields.
        """
        parts: list[str] = []
        if object_name == "entity":
            for field_name in ("entity_type", "canonical_name", "summary"):
                value = str(obj.get(field_name) or "").strip()
                if value:
                    parts.append(value)
            for alias_entry in (obj.get("aliases") or []):
                alias_text = (
                    str(alias_entry)
                    if isinstance(alias_entry, str)
                    else str((alias_entry or {}).get("alias") or "")
                ).strip()
                if alias_text:
                    parts.append(alias_text)
            attrs = obj.get("attributes") or {}
            if isinstance(attrs, dict):
                for k, v in attrs.items():
                    text = str(v or "").strip()
                    if text:
                        parts.append(f"{k}: {text}")
        elif object_name == "relation":
            for field_name in ("relation_type", "source_entity_id", "target_entity_id"):
                value = str(obj.get(field_name) or "").strip()
                if value:
                    parts.append(value)
            relation_description = str(
                obj.get("relation_description")
                or ((obj.get("metadata") or {}).get("relation_description") if isinstance(obj.get("metadata"), dict) else "")
                or ""
            ).strip()
            if relation_description:
                parts.append(relation_description)
            weight = obj.get("weight")
            if weight is not None:
                parts.append(f"weight:{weight}")
            conf = obj.get("confidence")
            if conf is not None:
                parts.append(f"confidence:{conf}")
        elif object_name == "block":
            heading = str(obj.get("heading") or "").strip()
            content = str(obj.get("content") or "").strip()
            block_kind = str(obj.get("block_kind") or "").strip()
            if heading:
                parts.append(heading)
            if block_kind and block_kind != "chunk":
                parts.append(block_kind)
            if content:
                parts.append(content[:1200])
        elif object_name == "document":
            # Use document_type + title + section_name + summary + data key hints
            doc_type = str(obj.get("document_type") or "").strip()
            title = str(obj.get("title") or "").strip()
            section_name = str(obj.get("section_name") or "").strip()
            summary = str(obj.get("summary") or "").strip()
            if doc_type:
                parts.append(doc_type)
            if title:
                parts.append(title)
            if section_name:
                parts.append(section_name)
            if summary:
                parts.append(summary)
            # For ai_ide_projection: describe data keys as searchable hints
            data = obj.get("data")
            if isinstance(data, dict):
                top_keys = [str(k) for k in list(data.keys())[:8] if k not in ("schema", "_meta")]
                if top_keys:
                    parts.append("contains: " + ", ".join(top_keys))
            # Fallback: source_uri
            if not parts:
                for field_name in ("source_uri", "_id", "id"):
                    value = str(obj.get(field_name) or "").strip()
                    if value:
                        parts.append(value)
                        break
        else:
            # Generic fallback: use id or canonical_name
            for field_name in ("canonical_name", "id"):
                value = str(obj.get(field_name) or "").strip()
                if value:
                    parts.append(value)
                    break
        return " | ".join(p for p in parts if p.strip())

    # ------------------------------------------------------------------
    # Domain: embed
    # ------------------------------------------------------------------

    def embed_object(self, object_name: str, text: str) -> list[float]:
        """Generate an embedding vector for the given canonical object text.

        Args:
            object_name: owner type label (used only for dispatch tracing)
            text:        canonical text produced by build_object_text
        Returns:
            Dense float vector.
        """
        _ = object_name
        encoder = self._load_encoder()
        return list(encoder.embed_query(text))

    # ------------------------------------------------------------------
    # Domain: persist
    # ------------------------------------------------------------------

    def store_object(
        self,
        object_name: str,
        obj: dict[str, Any],
        *,
        owner_id: str,
    ) -> dict[str, Any]:
        """Build text, embed, and persist an EmbeddingObject for an entity or relation.

        Args:
            object_name: "entity" or "relation"
            obj:         raw dict payload
            owner_id:    stable entity or relation id used as embedding owner
        Returns:
            Status report dict with keys stored, owner_id, dimension, result.
        """
        text = self.build_object_text(object_name, obj)
        if not text.strip():
            return {"stored": False, "reason": "empty_text", "owner_id": owner_id}

        vector = self.embed_object(object_name, text)
        content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        model_id = self._model_name()

        emb = EmbeddingObject(
            tenant_id=self._runtime_config.tenant_id,
            namespace_id=self._runtime_config.namespace_id,
            model_id=model_id,
            owner_type=object_name,
            owner_id=owner_id,
            content_sha256=content_sha256,
            dimension=len(vector),
            index_namespace=self._runtime_config.namespace_id,
            index_item_key=f"{object_name}:{owner_id}",
            embedding=vector,
            metadata={"source_text": text[:400]},
        )
        result = self._knowledge_service.store_embedding_object(emb)
        return {
            "stored": True,
            "owner_id": owner_id,
            "object_name": object_name,
            "dimension": len(vector),
            "model_id": model_id,
            "result": dict(result or {}),
        }

    # ------------------------------------------------------------------
    # Domain: full pipeline entry point
    # ------------------------------------------------------------------

    def process_object(
        self,
        object_name: str,
        obj: dict[str, Any],
        *,
        owner_id: str,
    ) -> dict[str, Any]:
        """Full pipeline: build canonical text → embed → persist.

        This is the single entry point for calling code.

        Args:
            object_name: "entity" or "relation"
            obj:         raw dict payload
            owner_id:    stable id used as embedding owner key
        Returns:
            Status report dict (see store_object).
        """
        return self.store_object(object_name, obj, owner_id=owner_id)


class PipelineService:
    """AgentsDB bridge for runtime retrieval telemetry and shared namespace resolution."""

    def __init__(self, knowledge_service: KnowledgeObjectService, runtime_config: RuntimeConfigObject) -> None:
        self._knowledge_service = knowledge_service
        self._runtime_config = runtime_config

    def load_tenant_id(
        self,
        *,
        handoff_metadata: Mapping[str, Any] | None = None,
        handoff_payload: Mapping[str, Any] | None = None,
    ) -> str:
        return str(
            (handoff_payload or {}).get("tenant_id")
            or (handoff_metadata or {}).get("tenant_id")
            or self._runtime_config.tenant_id
        ).strip() or self._runtime_config.tenant_id

    def load_namespace_object(
        self,
        *,
        handoff_metadata: Mapping[str, Any] | None = None,
        handoff_payload: Mapping[str, Any] | None = None,
    ) -> NamespaceObject:
        return _build_namespace_object_from_runtime_config(
            self._runtime_config,
            handoff_metadata=handoff_metadata,
            handoff_payload=handoff_payload,
        )


class ObjectMappingService:
    """Map parsed result objects to generic document, entity, and relation objects."""

    def __init__(self, knowledge_service: KnowledgeObjectService, runtime_config: RuntimeConfigObject) -> None:
        self._knowledge_service = knowledge_service
        self._runtime_config = runtime_config

    def load_namespace_object(
        self,
        *,
        handoff_metadata: Mapping[str, Any] | None = None,
        handoff_payload: Mapping[str, Any] | None = None,
    ) -> NamespaceObject:
        return _build_namespace_object_from_runtime_config(
            self._runtime_config,
            handoff_metadata=handoff_metadata,
            handoff_payload=handoff_payload,
        )

    def load_object_payload(self, *, object_name: str, result_payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized_object_name = _normalize_document_object_name(object_name)
        object_payload = result_payload.get(normalized_object_name)
        if isinstance(object_payload, Mapping):
            return dict(object_payload)
        if normalized_object_name != "job_posting":
            return {}
        raw_text_payload = self.load_raw_text_document_payload(result_payload=result_payload)
        entity_payload_list = self.load_explicit_entity_payload_list(result_payload=result_payload)
        compatibility_payload: dict[str, Any] = {}
        subject_payload = next(
            (
                entity_payload
                for entity_payload in entity_payload_list
                if str(entity_payload.get("entity_key") or "").strip() == "subject"
                or str((entity_payload.get("metadata") or {}).get("role") if isinstance(entity_payload.get("metadata"), Mapping) else "").strip() == "subject"
                or str(entity_payload.get("entity_type") or entity_payload.get("type_key") or "").strip() == "job_posting"
            ),
            {},
        )
        organization_payload = next(
            (
                entity_payload
                for entity_payload in entity_payload_list
                if str(entity_payload.get("entity_type") or entity_payload.get("type_key") or "").strip() == "organization"
            ),
            {},
        )
        location_payload = next(
            (
                entity_payload
                for entity_payload in entity_payload_list
                if str(entity_payload.get("entity_type") or entity_payload.get("type_key") or "").strip() == "location"
            ),
            {},
        )
        contact_payload = next(
            (
                entity_payload
                for entity_payload in entity_payload_list
                if str(entity_payload.get("entity_type") or entity_payload.get("type_key") or "").strip() == "person"
            ),
            {},
        )
        title = _first_non_empty_string(
            [
                subject_payload.get("canonical_name") if isinstance(subject_payload, Mapping) else None,
                raw_text_payload.get("title"),
            ],
        )
        if title:
            compatibility_payload["job_title"] = title
        company_name = _first_non_empty_string(
            [organization_payload.get("canonical_name") if isinstance(organization_payload, Mapping) else None],
        )
        if company_name:
            compatibility_payload["company_name"] = company_name
        raw_text = _first_non_empty_string([raw_text_payload.get("raw_text"), raw_text_payload.get("text")])
        if raw_text:
            compatibility_payload["raw_text"] = raw_text
        summary = _first_non_empty_string(
            [
                subject_payload.get("summary") if isinstance(subject_payload, Mapping) else None,
                raw_text_payload.get("summary"),
            ],
        )
        if summary:
            compatibility_payload["summary"] = summary
        metadata_payload = raw_text_payload.get("metadata") if isinstance(raw_text_payload.get("metadata"), Mapping) else {}
        language_code = _first_non_empty_string([raw_text_payload.get("language"), metadata_payload.get("language")])
        if metadata_payload or language_code:
            compatibility_payload["metadata"] = dict(metadata_payload)
            if language_code:
                compatibility_payload["metadata"]["language"] = language_code
        if company_name or location_payload:
            compatibility_payload.setdefault("company_info", {})
        if location_payload:
            compatibility_payload["company_info"]["location"] = location_payload.get("canonical_name")
        if contact_payload:
            compatibility_payload.setdefault("application", {})
            compatibility_payload["application"]["contact_person"] = contact_payload.get("canonical_name")
        return compatibility_payload

    def load_raw_text_document_payload(self, *, result_payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_text_payload = result_payload.get("raw_text_document")
        return dict(raw_text_payload) if isinstance(raw_text_payload, Mapping) else {}

    def load_explicit_entity_payload_list(self, *, result_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        entity_payload_list = result_payload.get("entity_objects")
        if not isinstance(entity_payload_list, Sequence) or isinstance(entity_payload_list, (str, bytes, bytearray)):
            return []
        return [dict(entity_payload) for entity_payload in entity_payload_list if isinstance(entity_payload, Mapping)]

    def load_explicit_relation_payload_list(self, *, result_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        relation_payload_list = result_payload.get("relation_objects")
        if not isinstance(relation_payload_list, Sequence) or isinstance(relation_payload_list, (str, bytes, bytearray)):
            return []
        return [dict(relation_payload) for relation_payload in relation_payload_list if isinstance(relation_payload, Mapping)]

    def _build_seed_relation_payload_list(
        self,
        *,
        entity_candidate_objects: Sequence[MappingSeedEntityObject],
    ) -> list[dict[str, Any]]:
        relation_payload_list: list[dict[str, Any]] = []
        seen_relation_key_set: set[tuple[str, str, str]] = set()
        for entity_candidate_object in entity_candidate_objects:
            relation_type_key = _first_non_empty_string([entity_candidate_object.relation_type_key])
            if not relation_type_key:
                continue
            source_seed_key = _first_non_empty_string([entity_candidate_object.source_seed_key]) or "subject"
            if not source_seed_key or source_seed_key == entity_candidate_object.seed_key:
                continue
            relation_key = (source_seed_key, relation_type_key, entity_candidate_object.seed_key)
            if relation_key in seen_relation_key_set:
                continue
            seen_relation_key_set.add(relation_key)
            relation_metadata = _deepcopy_object(entity_candidate_object.metadata)
            relation_description = _first_non_empty_string(
                [
                    entity_candidate_object.relation_description,
                    relation_metadata.get("relation_description"),
                    relation_metadata.get("description"),
                ],
            )
            if relation_description:
                relation_metadata["relation_description"] = relation_description
            relation_metadata.setdefault(
                "mapped_from",
                str(entity_candidate_object.metadata.get("mapped_from") or ("explicit_target_seed" if entity_candidate_object.is_target else "parser_result")),
            )
            relation_payload: dict[str, Any] = {
                "source_seed_key": source_seed_key,
                "target_seed_key": entity_candidate_object.seed_key,
                "relation_type": relation_type_key,
                "section_key": entity_candidate_object.section_key,
                "confidence": entity_candidate_object.confidence,
                "weight": entity_candidate_object.confidence,
                "metadata": relation_metadata,
            }
            source_field = _first_non_empty_string([relation_metadata.get("source_field")])
            if source_field:
                relation_payload["source_field"] = source_field
            relation_payload_list.append(relation_payload)
        return relation_payload_list

    def load_correlation_id(
        self,
        *,
        result_payload: Mapping[str, Any],
        fallback_correlation_id: str | None = None,
    ) -> str:
        return _first_non_empty_string(
            [
                fallback_correlation_id,
                result_payload.get("correlation_id"),
                (result_payload.get("db_updates") or {}).get("correlation_id") if isinstance(result_payload.get("db_updates"), Mapping) else None,
                (result_payload.get("file") or {}).get("content_sha256") if isinstance(result_payload.get("file"), Mapping) else None,
            ],
        ) or _stable_sha256(str(result_payload))

    def load_document_title(self, *, object_name: str, object_payload: Mapping[str, Any], correlation_id: str) -> str:
        normalized_object_name = _normalize_document_object_name(object_name)
        if normalized_object_name == "job_posting":
            return _first_non_empty_string(
                [
                    object_payload.get("job_title"),
                    object_payload.get("title"),
                    object_payload.get("external_id"),
                    correlation_id,
                ],
            ) or correlation_id
        return _first_non_empty_string(
            [
                object_payload.get("title"),
                object_payload.get("name"),
                object_payload.get("full_name"),
                correlation_id,
            ],
        ) or correlation_id

    def load_document_source_uri(
        self,
        *,
        result_payload: Mapping[str, Any],
        handoff_payload: Mapping[str, Any] | None = None,
    ) -> str:
        file_payload = result_payload.get("file") if isinstance(result_payload.get("file"), Mapping) else {}
        link_payload = result_payload.get("link") if isinstance(result_payload.get("link"), Mapping) else {}
        source_payload = handoff_payload if isinstance(handoff_payload, Mapping) else {}
        return _first_non_empty_string(
            [
                file_payload.get("source_uri"),
                file_payload.get("path"),
                link_payload.get("url"),
                source_payload.get("url"),
                source_payload.get("source_path"),
            ],
        ) or "local://parser_result"

    def load_document_text(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
    ) -> str:
        normalized_object_name = _normalize_document_object_name(object_name)
        raw_text_payload = self.load_raw_text_document_payload(result_payload=result_payload)
        if normalized_object_name == "job_posting":
            return _first_non_empty_string(
                [
                    raw_text_payload.get("raw_text"),
                    raw_text_payload.get("text"),
                    object_payload.get("raw_text"),
                    (result_payload.get("parse") or {}).get("raw_text") if isinstance(result_payload.get("parse"), Mapping) else None,
                ],
            ) or ""
        return _first_non_empty_string(
            [
                object_payload.get("raw_text"),
                (result_payload.get("parse") or {}).get("raw_text") if isinstance(result_payload.get("parse"), Mapping) else None,
            ],
        ) or ""

    def build_block_seed_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
        result_payload: Mapping[str, Any] | None = None,
    ) -> list[MappingBlockSeedObject]:
        normalized_object_name = _normalize_document_object_name(object_name)
        raw_text_payload = self.load_raw_text_document_payload(result_payload=result_payload or {})
        explicit_block_seed_objects = self._build_explicit_block_seed_objects(
            object_name=normalized_object_name,
            correlation_id=correlation_id,
            raw_text_payload=raw_text_payload,
        )
        if explicit_block_seed_objects:
            return explicit_block_seed_objects
        object_pattern = self.load_object_pattern(object_name=normalized_object_name)
        if object_pattern:
            return self._build_pattern_block_seed_objects(
                object_name=normalized_object_name,
                object_payload=object_payload,
                correlation_id=correlation_id,
                object_pattern=object_pattern,
            )
        return self._build_generic_block_seed_objects(
            object_name=normalized_object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
        )

    def build_entity_candidate_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
        result_payload: Mapping[str, Any] | None = None,
    ) -> list[MappingSeedEntityObject]:
        normalized_object_name = _normalize_document_object_name(object_name)
        explicit_entity_candidate_objects = self._build_explicit_entity_candidate_objects(
            object_name=normalized_object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
            entity_payload_list=self.load_explicit_entity_payload_list(result_payload=result_payload or {}),
        )
        if explicit_entity_candidate_objects:
            return explicit_entity_candidate_objects
        object_pattern = self.load_object_pattern(object_name=normalized_object_name)
        if object_pattern:
            return self._build_pattern_seed_entity_objects(
                object_name=normalized_object_name,
                object_payload=object_payload,
                correlation_id=correlation_id,
                object_pattern=object_pattern,
            )
        return self._build_generic_entity_candidate_objects(
            object_name=normalized_object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
        )

    def build_document_block_objects(
        self,
        *,
        document_text: str,
        block_seed_objects: Sequence[MappingBlockSeedObject],
        entity_candidate_objects: Sequence[MappingSeedEntityObject],
        entity_id_by_key: Mapping[str, str],
        timestamp: datetime,
    ) -> list[BlockObject]:
        current_offset = 0
        block_objects: list[BlockObject] = []
        for block_seed_object in block_seed_objects:
            char_start = document_text.find(block_seed_object.content, current_offset) if document_text else -1
            char_end = char_start + len(block_seed_object.content) if char_start >= 0 else None
            if char_end is not None:
                current_offset = max(current_offset, char_end)
            mentions: list[EntityMentionObject] = []
            for entity_candidate_object in entity_candidate_objects:
                if entity_candidate_object.section_key != block_seed_object.section_key:
                    continue
                entity_id = entity_id_by_key.get(entity_candidate_object.seed_key)
                if not entity_id:
                    continue
                mention_text = str(entity_candidate_object.mention_text or entity_candidate_object.canonical_name).strip()
                if not mention_text:
                    continue
                mention_char_start = block_seed_object.content.find(mention_text)
                if mention_char_start < 0:
                    continue
                mentions.append(
                    EntityMentionObject(
                        entity_id=entity_id,
                        mention_text=mention_text,
                        extractor="parser_mapping",
                        confidence=entity_candidate_object.confidence,
                        char_start=mention_char_start,
                        char_end=mention_char_start + len(mention_text),
                        metadata={
                            "source_field": entity_candidate_object.metadata.get("source_field"),
                            "mapped_from": "parser_result",
                        },
                        created_at=timestamp,
                    ),
                )
            block_objects.append(
                BlockObject(
                    block_id=block_seed_object.block_id,
                    block_no=block_seed_object.block_no,
                    content=block_seed_object.content,
                    block_kind=block_seed_object.block_kind,
                    heading=block_seed_object.heading,
                    token_count=len(block_seed_object.content.split()),
                    char_start=char_start if char_start >= 0 else None,
                    char_end=char_end,
                    metadata=_deepcopy_object(block_seed_object.metadata),
                    mentions=mentions,
                    created_at=timestamp,
                ),
            )
        return block_objects

    def build_document_object(
        self,
        *,
        object_name: str,
        result_payload: Mapping[str, Any],
        namespace_object: NamespaceObject,
        correlation_id: str,
        handoff_payload: Mapping[str, Any] | None = None,
    ) -> DocumentObject | None:
        object_payload = self.load_object_payload(object_name=object_name, result_payload=result_payload)
        if not object_payload:
            return None
        timestamp = _now_utc()
        document_id = f"doc:{_normalize_document_object_name(object_name)}:{correlation_id}"
        document_text = self.load_document_text(
            object_name=object_name,
            object_payload=object_payload,
            result_payload=result_payload,
        )
        block_seed_objects = self.build_block_seed_objects(
            object_name=object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
            result_payload=result_payload,
        )
        if not document_text:
            document_text = "\n\n".join(block_seed_object.content for block_seed_object in block_seed_objects if block_seed_object.content)
        entity_candidate_objects = self.build_entity_candidate_objects(
            object_name=object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
            result_payload=result_payload,
        )
        entity_objects = self.build_entity_objects(
            object_name=object_name,
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            document_id=document_id,
            entity_candidate_objects=entity_candidate_objects,
            timestamp=timestamp,
        )
        entity_id_by_key = {
            entity_candidate_object.seed_key: entity_object.id
            for entity_candidate_object, entity_object in zip(entity_candidate_objects, entity_objects)
        }
        block_objects = self.build_document_block_objects(
            document_text=document_text,
            block_seed_objects=block_seed_objects,
            entity_candidate_objects=entity_candidate_objects,
            entity_id_by_key=entity_id_by_key,
            timestamp=timestamp,
        )
        file_payload = result_payload.get("file") if isinstance(result_payload.get("file"), Mapping) else {}
        parse_payload = result_payload.get("parse") if isinstance(result_payload.get("parse"), Mapping) else {}
        content_sha256 = _first_non_empty_string(
            [
                file_payload.get("content_sha256"),
                (result_payload.get("db_updates") or {}).get("content_sha256") if isinstance(result_payload.get("db_updates"), Mapping) else None,
                _stable_sha256(document_text) if document_text else None,
            ],
        ) or _stable_sha256(correlation_id)
        return DocumentObject(
            id=document_id,
            tenant_id=namespace_object.tenant_id,
            namespace_id=namespace_object.id,
            document_type=_normalize_document_object_name(object_name),
            title=self.load_document_title(object_name=object_name, object_payload=object_payload, correlation_id=correlation_id),
            source_uri=self.load_document_source_uri(result_payload=result_payload, handoff_payload=handoff_payload),
            content_sha256=content_sha256,
            source_system=_first_non_empty_string([
                result_payload.get("agent"),
                (handoff_payload or {}).get("platform") if isinstance(handoff_payload, Mapping) else None,
                "parser_result",
            ]) or "parser_result",
            mime_type=_first_non_empty_string([file_payload.get("mime_type"), "text/plain"]) or "text/plain",
            language_code=_first_non_empty_string([parse_payload.get("language"), _mapping_value(object_payload, "metadata.language")]),
            correlation_id=correlation_id,
            summary=_first_non_empty_string([
                object_payload.get("summary"),
                _mapping_value(object_payload, "position.level"),
                _mapping_value(object_payload, "requirements.experience_description"),
            ]) or "",
            metadata={
                "object_name": _normalize_document_object_name(object_name),
                "source_agent": result_payload.get("agent"),
                "parse": _deepcopy_object(parse_payload),
            },
            blocks=block_objects,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def build_entity_objects(
        self,
        *,
        object_name: str,
        namespace_object: NamespaceObject,
        correlation_id: str,
        document_id: str,
        entity_candidate_objects: Sequence[MappingSeedEntityObject],
        timestamp: datetime,
    ) -> list[EntityObject]:
        entity_objects: list[EntityObject] = []
        for entity_candidate_object in entity_candidate_objects:
            entity_id = self._build_entity_id(
                object_name=object_name,
                entity_type=entity_candidate_object.type_key,
                canonical_name=entity_candidate_object.canonical_name,
            )
            alias_objects = [
                EntityAliasObject(
                    alias=alias_value,
                    source_document_id=document_id,
                    created_at=timestamp,
                )
                for alias_value in dict.fromkeys(
                    alias_value.strip()
                    for alias_value in entity_candidate_object.aliases
                    if alias_value.strip() and alias_value.strip() != entity_candidate_object.canonical_name
                )
            ]
            entity_objects.append(
                EntityObject(
                    id=entity_id,
                    tenant_id=namespace_object.tenant_id,
                    namespace_id=namespace_object.id,
                    entity_type=entity_candidate_object.type_key,
                    canonical_name=entity_candidate_object.canonical_name,
                    external_key=f"{entity_candidate_object.type_key}:{_slugify_object_name(entity_candidate_object.canonical_name)}",
                    correlation_id=correlation_id,
                    summary=entity_candidate_object.summary,
                    attributes=_deepcopy_object(entity_candidate_object.attributes),
                    aliases=alias_objects,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
            )
        return entity_objects

    def build_relation_objects(
        self,
        *,
        object_name: str,
        namespace_object: NamespaceObject,
        correlation_id: str,
        entity_candidate_objects: Sequence[MappingSeedEntityObject],
        entity_objects: Sequence[EntityObject],
        block_seed_objects: Sequence[MappingBlockSeedObject],
        timestamp: datetime,
        result_payload: Mapping[str, Any] | None = None,
    ) -> list[EntityRelationObject]:
        entity_id_by_key: dict[str, str] = {}
        for entity_candidate_object, entity_object in zip(entity_candidate_objects, entity_objects):
            entity_id_by_key[entity_candidate_object.seed_key] = entity_object.id
        explicit_relation_payload_list = self.load_explicit_relation_payload_list(result_payload=result_payload or {})
        if not explicit_relation_payload_list:
            explicit_relation_payload_list = self._build_seed_relation_payload_list(
                entity_candidate_objects=entity_candidate_objects,
            )
        if explicit_relation_payload_list:
            block_id_by_key = {block_seed_object.section_key: block_seed_object.block_id for block_seed_object in block_seed_objects}
            relation_objects: list[EntityRelationObject] = []
            for relation_payload in explicit_relation_payload_list:
                source_entity_key = _first_non_empty_string(
                    [
                        relation_payload.get("source_entity_key"),
                        relation_payload.get("source_seed_key"),
                    ],
                )
                target_entity_key = _first_non_empty_string(
                    [
                        relation_payload.get("target_entity_key"),
                        relation_payload.get("target_seed_key"),
                    ],
                )
                relation_type = _first_non_empty_string([relation_payload.get("relation_type")])
                if not source_entity_key or not target_entity_key or not relation_type:
                    continue
                source_entity_id = entity_id_by_key.get(source_entity_key)
                target_entity_id = entity_id_by_key.get(target_entity_key)
                if not source_entity_id or not target_entity_id:
                    continue
                relation_payload_value = f"{source_entity_id}|{relation_type}|{target_entity_id}"
                relation_metadata = _deepcopy_object(
                    relation_payload.get("metadata") if isinstance(relation_payload.get("metadata"), Mapping) else {},
                )
                source_field = _first_non_empty_string([relation_payload.get("source_field"), relation_metadata.get("source_field")])
                if source_field:
                    relation_metadata["source_field"] = source_field
                relation_metadata: dict = relation_metadata
                relation_metadata.setdefault("mapped_from", "explicit_relation_model")
                evidence_objects: list[RelationEvidenceObject] = []
                block_id = block_id_by_key.get(str(relation_payload.get("section_key") or "").strip())
                if block_id:
                    evidence_objects.append(RelationEvidenceObject(block_id=block_id, created_at=timestamp))
                for evidence_payload in relation_payload.get("evidence") or []:
                    if not isinstance(evidence_payload, Mapping):
                        continue
                    evidence_block_id = _first_non_empty_string([evidence_payload.get("block_id")])
                    if not evidence_block_id:
                        continue
                    evidence_objects.append(
                        RelationEvidenceObject(
                            block_id=evidence_block_id,
                            evidence_role=str(evidence_payload.get("evidence_role") or "supporting"),
                            created_at=timestamp,
                        ),
                    )
                confidence = _first_number([relation_payload.get("confidence"), relation_payload.get("weight")]) or 0.95
                weight = _first_number([relation_payload.get("weight"), relation_payload.get("confidence")]) or confidence
                relation_objects.append(
                    EntityRelationObject(
                        id=f"rel:{_normalize_document_object_name(object_name)}:{_stable_sha256(relation_payload_value)[:16]}",
                        tenant_id=namespace_object.tenant_id,
                        namespace_id=namespace_object.id,
                        source_entity_id=source_entity_id,
                        target_entity_id=target_entity_id,
                        relation_type=relation_type,
                        direction=_first_non_empty_string([relation_payload.get("direction")]) or "directed",
                        weight=weight,
                        confidence=confidence,
                        correlation_id=correlation_id,
                        metadata=relation_metadata,
                        evidence=evidence_objects,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                )
            if relation_objects:
                return relation_objects
        source_entity_id = entity_id_by_key.get("subject")
        if not source_entity_id:
            return []
        block_id_by_key = {block_seed_object.section_key: block_seed_object.block_id for block_seed_object in block_seed_objects}
        relation_objects: list[EntityRelationObject] = []
        for entity_candidate_object in entity_candidate_objects:
            if entity_candidate_object.seed_key == "subject" or not entity_candidate_object.relation_type_key:
                continue
            target_entity_id = entity_id_by_key.get(entity_candidate_object.seed_key)
            if not target_entity_id:
                continue
            relation_payload = f"{source_entity_id}|{entity_candidate_object.relation_type_key}|{target_entity_id}"
            evidence: list[RelationEvidenceObject] = []
            block_id = block_id_by_key.get(entity_candidate_object.section_key or "")
            if block_id:
                evidence.append(RelationEvidenceObject(block_id=block_id, created_at=timestamp))
            relation_objects.append(
                EntityRelationObject(
                    id=f"rel:{_normalize_document_object_name(object_name)}:{_stable_sha256(relation_payload)[:16]}",
                    tenant_id=namespace_object.tenant_id,
                    namespace_id=namespace_object.id,
                    source_entity_id=source_entity_id,
                    target_entity_id=target_entity_id,
                    relation_type=entity_candidate_object.relation_type_key,
                    direction="directed",
                    weight=entity_candidate_object.confidence,
                    confidence=entity_candidate_object.confidence,
                    correlation_id=correlation_id,
                    metadata={
                        "source_field": entity_candidate_object.metadata.get("source_field"),
                        "mapped_from": "parser_result",
                    },
                    evidence=evidence,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
            )
        return relation_objects

    def store_mapped_object(
        self,
        *,
        object_name: str,
        result_payload: Mapping[str, Any],
        fallback_correlation_id: str | None = None,
        handoff_metadata: Mapping[str, Any] | None = None,
        handoff_payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        normalized_object_name = _normalize_document_object_name(object_name)
        object_payload = self.load_object_payload(object_name=normalized_object_name, result_payload=result_payload)
        if not object_payload:
            return {
                "ok": True,
                "stored": False,
                "backend": "agents_db",
                "object_name": normalized_object_name,
                "reason": "missing_object_payload",
            }
        parse_payload = result_payload.get("parse") if isinstance(result_payload.get("parse"), Mapping) else {}
        if normalized_object_name == "job_posting" and parse_payload.get("is_job_posting") is False:
            return {
                "ok": True,
                "stored": False,
                "backend": "agents_db",
                "object_name": normalized_object_name,
                "reason": "parse_marked_non_matching",
            }
        correlation_id = self.load_correlation_id(
            result_payload=result_payload,
            fallback_correlation_id=fallback_correlation_id,
        )
        namespace_object = self.load_namespace_object(
            handoff_metadata=handoff_metadata,
            handoff_payload=handoff_payload,
        )
        timestamp = _now_utc()
        document_object = self.build_document_object(
            object_name=normalized_object_name,
            result_payload=result_payload,
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            handoff_payload=handoff_payload,
        )
        if document_object is None:
            return {
                "ok": True,
                "stored": False,
                "backend": "agents_db",
                "object_name": normalized_object_name,
                "reason": "document_mapping_failed",
            }
        block_seed_objects = self.build_block_seed_objects(
            object_name=normalized_object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
            result_payload=result_payload,
        )
        entity_candidate_objects = self.build_entity_candidate_objects(
            object_name=normalized_object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
            result_payload=result_payload,
        )
        entity_objects = self.build_entity_objects(
            object_name=normalized_object_name,
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            document_id=document_object.id,
            entity_candidate_objects=entity_candidate_objects,
            timestamp=timestamp,
        )
        relation_objects = self.build_relation_objects(
            object_name=normalized_object_name,
            namespace_object=namespace_object,
            correlation_id=correlation_id,
            entity_candidate_objects=entity_candidate_objects,
            entity_objects=entity_objects,
            block_seed_objects=block_seed_objects,
            timestamp=timestamp,
            result_payload=result_payload,
        )
        self._knowledge_service.store_namespace_object(namespace_object)
        self._knowledge_service.store_document_object(document_object)
        for entity_object in entity_objects:
            self._knowledge_service.store_entity_object(entity_object)
        for relation_object in relation_objects:
            self._knowledge_service.store_relation_object(relation_object)
        return {
            "ok": True,
            "stored": True,
            "backend": "agents_db",
            "object_name": normalized_object_name,
            "namespace_id": namespace_object.id,
            "document_id": document_object.id,
            "entity_count": len(entity_objects),
            "relation_count": len(relation_objects),
        }

    def load_object_pattern(self, *, object_name: str) -> dict[str, Any] | None:
        return _deepcopy_object(OBJECT_MAPPING_PATTERN_BY_NAME.get(_normalize_document_object_name(object_name)))

    def _load_pattern_value(
        self,
        *,
        object_payload: Mapping[str, Any],
        value_path_list: Sequence[str],
    ) -> str | None:
        return _first_non_empty_string(_mapping_value(object_payload, value_path) for value_path in value_path_list)

    def _load_pattern_attribute_map(
        self,
        *,
        object_payload: Mapping[str, Any],
        attribute_path_map: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        attribute_map: dict[str, Any] = {}
        for attribute_key, value_path in dict(attribute_path_map or {}).items():
            attribute_map[str(attribute_key)] = _mapping_value(object_payload, value_path)
        return attribute_map

    def _build_pattern_section_line_list(
        self,
        *,
        object_payload: Mapping[str, Any],
        section_pattern: Mapping[str, Any],
    ) -> list[str]:
        line_list: list[str] = []
        for field_pattern in section_pattern.get("field_line_list") or []:
            path_list = tuple(field_pattern.get("path_list") or ()) or ((field_pattern.get("path"),) if field_pattern.get("path") else ())
            field_value = self._load_pattern_value(object_payload=object_payload, value_path_list=path_list)
            if not field_value:
                continue
            line_list.append(f"{field_pattern.get('label')}: {field_value}")
        for group_pattern in section_pattern.get("group_line_list") or []:
            item_list = _load_string_list(_mapping_value(object_payload, str(group_pattern.get("path") or "")))
            if not item_list:
                continue
            if bool(group_pattern.get("emit_label_only_when_items", True)):
                line_list.append(f"{group_pattern.get('label')}:")
            line_list.extend(f"- {item_value}" for item_value in item_list)
        return line_list

    def _build_pattern_block_seed_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
        object_pattern: Mapping[str, Any],
    ) -> list[MappingBlockSeedObject]:
        seed_object_list: list[MappingBlockSeedObject] = []
        for section_pattern in object_pattern.get("section_pattern_list") or []:
            line_list = self._build_pattern_section_line_list(object_payload=object_payload, section_pattern=section_pattern)
            if not line_list:
                continue
            seed_object_list.append(
                MappingBlockSeedObject(
                    section_key=str(section_pattern.get("section_key") or f"section_{len(seed_object_list) + 1}"),
                    block_id=f"blk:{correlation_id}:{len(seed_object_list) + 1}",
                    block_no=len(seed_object_list) + 1,
                    heading=str(section_pattern.get("heading") or object_name.replace("_", " ").title()),
                    content="\n".join(line_list),
                    block_kind=str(section_pattern.get("block_kind") or "chunk"),
                    metadata={"section_type": str(section_pattern.get("section_type_key") or section_pattern.get("section_key") or "section")},
                ),
            )
        if seed_object_list:
            return seed_object_list
        return self._build_generic_block_seed_objects(
            object_name=object_name,
            object_payload=object_payload,
            correlation_id=correlation_id,
        )

    def _build_explicit_block_seed_objects(
        self,
        *,
        object_name: str,
        correlation_id: str,
        raw_text_payload: Mapping[str, Any],
    ) -> list[MappingBlockSeedObject]:
        if not raw_text_payload:
            return []
        seed_object_list: list[MappingBlockSeedObject] = []
        section_payload_list = raw_text_payload.get("sections")
        if isinstance(section_payload_list, Sequence) and not isinstance(section_payload_list, (str, bytes, bytearray)):
            for section_payload in section_payload_list:
                if not isinstance(section_payload, Mapping):
                    continue
                section_text = _first_non_empty_string([section_payload.get("text"), section_payload.get("content")])
                if not section_text:
                    continue
                section_key = _first_non_empty_string([section_payload.get("section_key")]) or f"section_{len(seed_object_list) + 1}"
                section_metadata = _deepcopy_object(section_payload.get("metadata") if isinstance(section_payload.get("metadata"), Mapping) else {})
                section_metadata.setdefault("section_type", section_key)
                seed_object_list.append(
                    MappingBlockSeedObject(
                        section_key=section_key,
                        block_id=f"blk:{correlation_id}:{len(seed_object_list) + 1}",
                        block_no=len(seed_object_list) + 1,
                        heading=_first_non_empty_string([section_payload.get("heading"), raw_text_payload.get("title")]) or object_name.replace("_", " ").title(),
                        content=section_text,
                        block_kind=_first_non_empty_string([section_payload.get("block_kind")]) or "section",
                        metadata=section_metadata,
                    ),
                )
        if seed_object_list:
            return seed_object_list
        raw_text = _first_non_empty_string([raw_text_payload.get("raw_text"), raw_text_payload.get("text")])
        if not raw_text:
            return []
        return [
            MappingBlockSeedObject(
                section_key="document",
                block_id=f"blk:{correlation_id}:1",
                block_no=1,
                heading=_first_non_empty_string([raw_text_payload.get("title")]) or object_name.replace("_", " ").title(),
                content=raw_text,
                block_kind="document",
                metadata={"section_type": "document"},
            ),
        ]

    def _build_pattern_seed_entity_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
        object_pattern: Mapping[str, Any],
    ) -> list[MappingSeedEntityObject]:
        seed_object_list: list[MappingSeedEntityObject] = []
        subject_pattern = dict(object_pattern.get("subject_pattern") or {})
        subject_name = self._load_pattern_value(
            object_payload=object_payload,
            value_path_list=tuple(subject_pattern.get("value_path_list") or ("title", "name", "full_name")),
        ) or correlation_id
        seed_object_list.append(
            MappingSeedEntityObject(
                seed_key=str(subject_pattern.get("seed_key") or "subject"),
                type_key=str(subject_pattern.get("type_key") or object_name),
                canonical_name=subject_name,
                section_key=str(subject_pattern.get("section_key") or "primary"),
                mention_text=subject_name,
                confidence=float(subject_pattern.get("confidence") or 0.99),
                summary=str(subject_pattern.get("summary") or f"Primary {object_name.replace('_', ' ')} object mapped from the parsed result."),
                attributes=self._load_pattern_attribute_map(
                    object_payload=object_payload,
                    attribute_path_map=subject_pattern.get("attribute_path_map"),
                ),
            ),
        )
        for entity_pattern in object_pattern.get("entity_pattern_list") or []:
            canonical_name = self._load_pattern_value(
                object_payload=object_payload,
                value_path_list=tuple(entity_pattern.get("value_path_list") or ()),
            )
            if not canonical_name:
                continue
            type_key = str(entity_pattern.get("type_key") or object_name)
            seed_object_list.append(
                MappingSeedEntityObject(
                    seed_key=f"{type_key}:{_slugify_object_name(canonical_name)}",
                    type_key=type_key,
                    canonical_name=canonical_name,
                    section_key=str(entity_pattern.get("section_key") or "primary"),
                    relation_type_key=str(entity_pattern.get("relation_type_key") or "").strip() or None,
                    confidence=float(entity_pattern.get("confidence") or 0.95),
                    mention_text=canonical_name,
                    summary=str(entity_pattern.get("summary") or f"{type_key.replace('_', ' ').title()} associated with the mapped object."),
                    attributes=self._load_pattern_attribute_map(
                        object_payload=object_payload,
                        attribute_path_map=entity_pattern.get("attribute_path_map"),
                    ),
                    metadata={"source_field": entity_pattern.get("source_field")},
                ),
            )
        for entity_pattern in object_pattern.get("collection_entity_pattern_list") or []:
            collection_path_list = tuple(
                str(path_value).strip()
                for path_value in (entity_pattern.get("collection_path_list") or ())
                if str(path_value).strip()
            )
            single_collection_path = str(entity_pattern.get("collection_path") or "").strip()
            if single_collection_path and single_collection_path not in collection_path_list:
                collection_path_list = tuple([*collection_path_list, single_collection_path])
            if not collection_path_list:
                continue

            fallback_type_key = str(entity_pattern.get("fallback_type_key") or object_name)
            relation_type_key_map = dict(entity_pattern.get("relation_type_key_map") or {})
            seen_collection_key_set: set[tuple[str, str]] = set()
            for collection_path in collection_path_list:
                collection_source_field = str(entity_pattern.get("source_field") or collection_path).strip()
                collection_source_value = _mapping_value(object_payload, collection_path)
                collection_value_list = _load_typed_collection_value_list(collection_source_value)
                for collection_value, explicit_type_value in collection_value_list:
                    inferred_type_key = _load_type_key_from_pattern(
                        collection_value,
                        fallback_type_key=fallback_type_key,
                        type_key_pattern_map=entity_pattern.get("type_key_pattern_map"),
                    )
                    explicit_type_key = _load_type_key_from_explicit_value(
                        explicit_type_value,
                        fallback_type_key=fallback_type_key,
                        type_key_pattern_map=entity_pattern.get("type_key_pattern_map"),
                    )

                    type_key = inferred_type_key
                    if explicit_type_value:
                        type_key = explicit_type_key
                        if explicit_type_key == fallback_type_key and inferred_type_key != fallback_type_key:
                            type_key = inferred_type_key

                    relation_type_key = str(
                        relation_type_key_map.get(type_key)
                        or relation_type_key_map.get(fallback_type_key)
                        or ""
                    ).strip() or None
                    collection_key = (type_key, _normalize_pattern_key(collection_value))
                    if collection_key in seen_collection_key_set:
                        continue
                    seen_collection_key_set.add(collection_key)

                    seed_object_list.append(
                        MappingSeedEntityObject(
                            seed_key=f"{str(entity_pattern.get('seed_key_prefix') or type_key)}:{_slugify_object_name(collection_value)}",
                            type_key=type_key,
                            canonical_name=collection_value,
                            section_key=str(entity_pattern.get("section_key") or "primary"),
                            relation_type_key=relation_type_key,
                            confidence=float(entity_pattern.get("confidence") or 0.9),
                            mention_text=collection_value,
                            summary=str(entity_pattern.get("summary_prefix") or "Associated capability mapped from the parsed result."),
                            metadata={"source_field": collection_source_field},
                        ),
                    )
        unique_seed_object_list: list[MappingSeedEntityObject] = []
        seen_seed_key_set: set[str] = set()
        for seed_object in seed_object_list:
            if seed_object.seed_key in seen_seed_key_set:
                continue
            seen_seed_key_set.add(seed_object.seed_key)
            unique_seed_object_list.append(seed_object)
        return unique_seed_object_list

    def _build_explicit_entity_candidate_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
        entity_payload_list: Sequence[Mapping[str, Any]],
    ) -> list[MappingSeedEntityObject]:
        if not entity_payload_list:
            return []
        seed_object_list: list[MappingSeedEntityObject] = []
        for entity_payload in entity_payload_list:
            type_key = _first_non_empty_string([entity_payload.get("entity_type"), entity_payload.get("type_key")]) or object_name
            canonical_name = _first_non_empty_string(
                [
                    entity_payload.get("canonical_name"),
                    entity_payload.get("name"),
                    entity_payload.get("title"),
                    entity_payload.get("mention_text"),
                ],
            )
            if not canonical_name:
                continue
            entity_metadata = _deepcopy_object(entity_payload.get("metadata") if isinstance(entity_payload.get("metadata"), Mapping) else {})
            source_field = _first_non_empty_string([entity_payload.get("source_field"), entity_metadata.get("source_field")])
            if source_field:
                entity_metadata["source_field"] = source_field
            entity_metadata.setdefault("mapped_from", "explicit_entity_model")
            relation_type_key = _first_non_empty_string(
                [
                    entity_payload.get("relation_type"),
                    entity_payload.get("relation_type_key"),
                    entity_payload.get("is_relational"),
                    entity_payload.get("relation_name"),
                    entity_metadata.get("relation_type"),
                    entity_metadata.get("relation_type_key"),
                    entity_metadata.get("is_relational"),
                    entity_metadata.get("relation_name"),
                ],
            )
            relation_description = _first_non_empty_string(
                [
                    entity_payload.get("relation_description"),
                    entity_payload.get("explicit_description"),
                    entity_payload.get("description"),
                    entity_metadata.get("relation_description"),
                    entity_metadata.get("explicit_description"),
                    entity_metadata.get("description"),
                ],
            )
            if relation_description:
                entity_metadata["relation_description"] = relation_description
            source_seed_key = _first_non_empty_string(
                [
                    entity_payload.get("source_seed_key"),
                    entity_payload.get("source_entity_key"),
                    entity_payload.get("source_entity"),
                    entity_metadata.get("source_seed_key"),
                    entity_metadata.get("source_entity_key"),
                    entity_metadata.get("source_entity"),
                ],
            )
            normalized_is_target = False
            for is_target_value in (entity_payload.get("is_target"), entity_metadata.get("is_target")):
                resolved_is_target = _load_bool_value(is_target_value)
                if isinstance(resolved_is_target, bool):
                    normalized_is_target = resolved_is_target
                    break
            if not normalized_is_target and source_seed_key and relation_type_key:
                normalized_is_target = True
            seed_key = _first_non_empty_string([entity_payload.get("entity_key"), entity_payload.get("seed_key")]) or f"{type_key}:{_slugify_object_name(canonical_name)}"
            seed_object_list.append(
                MappingSeedEntityObject(
                    seed_key=seed_key,
                    type_key=type_key,
                    canonical_name=canonical_name,
                    section_key=_first_non_empty_string([entity_payload.get("section_key")]) or "primary",
                    relation_type_key=relation_type_key,
                    is_target=normalized_is_target,
                    source_seed_key=source_seed_key,
                    relation_description=relation_description,
                    confidence=_first_number([entity_payload.get("confidence")]) or 0.95,
                    mention_text=_first_non_empty_string([entity_payload.get("mention_text")]) or canonical_name,
                    summary=_first_non_empty_string([entity_payload.get("summary")]) or "",
                    attributes=_deepcopy_object(entity_payload.get("attributes") if isinstance(entity_payload.get("attributes"), Mapping) else {}),
                    aliases=_load_string_list(entity_payload.get("aliases")),
                    metadata=entity_metadata,
                ),
            )
        if not any(seed_object.seed_key == "subject" for seed_object in seed_object_list):
            canonical_name = _first_non_empty_string(
                [
                    object_payload.get("job_title"),
                    object_payload.get("title"),
                    correlation_id,
                ],
            ) or correlation_id
            seed_object_list.insert(
                0,
                MappingSeedEntityObject(
                    seed_key="subject",
                    type_key=object_name,
                    canonical_name=canonical_name,
                    section_key="primary",
                    confidence=0.99,
                    mention_text=canonical_name,
                    summary=f"Primary {object_name.replace('_', ' ')} entity mapped from parser result.",
                    metadata={"mapped_from": "compatibility_subject"},
                ),
            )
        unique_seed_object_list: list[MappingSeedEntityObject] = []
        seen_seed_key_set: set[str] = set()
        for seed_object in seed_object_list:
            if seed_object.seed_key in seen_seed_key_set:
                continue
            seen_seed_key_set.add(seed_object.seed_key)
            unique_seed_object_list.append(seed_object)
        return unique_seed_object_list

    def _build_generic_block_seed_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
    ) -> list[MappingBlockSeedObject]:
        content = _first_non_empty_string([
            object_payload.get("raw_text"),
            object_payload.get("summary"),
            object_payload.get("description"),
        ]) or str(object_payload)
        return [
            MappingBlockSeedObject(
                section_key="primary",
                block_id=f"blk:{correlation_id}:1",
                block_no=1,
                heading=object_name.replace("_", " ").title(),
                content=content,
                block_kind="section",
                metadata={"section_type": "primary"},
            ),
        ]

    def _build_generic_entity_candidate_objects(
        self,
        *,
        object_name: str,
        object_payload: Mapping[str, Any],
        correlation_id: str,
    ) -> list[MappingSeedEntityObject]:
        canonical_name = _first_non_empty_string([
            object_payload.get("title"),
            object_payload.get("name"),
            object_payload.get("full_name"),
            correlation_id,
        ]) or correlation_id
        return [
            MappingSeedEntityObject(
                seed_key="subject",
                type_key=object_name,
                canonical_name=canonical_name,
                section_key="primary",
                relation_type_key=None,
                confidence=0.99,
                mention_text=canonical_name,
                summary=f"Primary {object_name.replace('_', ' ')} entity mapped from parser result.",
            ),
        ]

    def _build_entity_id(self, *, object_name: str, entity_type: str, canonical_name: str) -> str:
        return f"ent:{_normalize_document_object_name(object_name)}:{entity_type}:{_slugify_object_name(canonical_name, fallback_prefix=entity_type)}"

    def build_retrieval_result_objects(
        self,
        *,
        tool_name: str,
        retrieval_result: Any,
    ) -> list[RetrievalResultObject]:
        if not isinstance(retrieval_result, list):
            return []
        retrieval_result_objects: list[RetrievalResultObject] = []
        for index, item in enumerate(retrieval_result, start=1):
            if isinstance(item, Mapping):
                item_payload = dict(item)
                result_id = _first_non_empty_string([
                    item_payload.get("result_id"),
                    item_payload.get("document_id"),
                    item_payload.get("id"),
                    item_payload.get("source"),
                    item_payload.get("path"),
                    item_payload.get("title"),
                ]) or f"{tool_name}:{index}"
                result_type = _first_non_empty_string([
                    item_payload.get("result_type"),
                    item_payload.get("owner_type"),
                ]) or "document"
                source_stage = _first_non_empty_string([
                    item_payload.get("source_stage"),
                    item_payload.get("backend"),
                ]) or tool_name
                metadata = _deepcopy_object(item_payload)
                lexical_score = _first_number([
                    item_payload.get("lexical_score"),
                    item_payload.get("document_score"),
                ])
                vector_score = _first_number([
                    item_payload.get("vector_score"),
                    item_payload.get("relevance_score"),
                    item_payload.get("score"),
                ])
                graph_score = _first_number([
                    item_payload.get("graph_score"),
                ])
                rerank_score = _first_number([
                    item_payload.get("rerank_score"),
                ])
            else:
                result_id = f"{tool_name}:{index}"
                result_type = "document"
                source_stage = tool_name
                metadata = {"value": _deepcopy_object(item)}
                lexical_score = None
                vector_score = None
                graph_score = None
                rerank_score = None
            retrieval_result_objects.append(
                RetrievalResultObject(
                    rank_no=index,
                    result_type=result_type,
                    result_id=result_id,
                    source_stage=source_stage,
                    chosen=True,
                    lexical_score=lexical_score,
                    vector_score=vector_score,
                    graph_score=graph_score,
                    rerank_score=rerank_score,
                    metadata=metadata,
                ),
            )
        return retrieval_result_objects

    def build_retrieval_run_object(
        self,
        *,
        tool_name: str,
        query_event: Mapping[str, Any],
        outcome_event: Mapping[str, Any],
        retrieval_result: Any,
    ) -> RetrievalRunObject:
        namespace_object = self.load_namespace_object(
            handoff_metadata=query_event if isinstance(query_event, Mapping) else None,
            handoff_payload=query_event if isinstance(query_event, Mapping) else None,
        )
        policy_snapshot = query_event.get("policy_snapshot") if isinstance(query_event.get("policy_snapshot"), Mapping) else {}
        return RetrievalRunObject(
            id=f"retrieval:{query_event.get('event_id')}",
            tenant_id=namespace_object.tenant_id,
            namespace_id=namespace_object.id,
            query_text=str(query_event.get("query_text") or ""),
            requested_k=max(1, int(query_event.get("k") or 1)),
            lexical_k=int(policy_snapshot.get("fetch_k") or 0) or None,
            graph_hops=None,
            vector_k=max(1, int(query_event.get("k") or 1)),
            rerank_strategy=str(policy_snapshot.get("rerank_method") or "none") or "none",
            correlation_id=_first_non_empty_string([
                query_event.get("event_id"),
                outcome_event.get("query_event_id"),
            ]),
            filters=_deepcopy_object(dict(policy_snapshot.get("metadata_filters") or {})),
            results=self.build_retrieval_result_objects(tool_name=tool_name, retrieval_result=retrieval_result),
            created_at=_now_utc(),
        )

    def store_retrieval_run(
        self,
        *,
        tool_name: str,
        query_event: Mapping[str, Any],
        outcome_event: Mapping[str, Any],
        retrieval_result: Any,
    ) -> Mapping[str, Any]:
        namespace_object = self.load_namespace_object(
            handoff_metadata=query_event if isinstance(query_event, Mapping) else None,
            handoff_payload=query_event if isinstance(query_event, Mapping) else None,
        )
        retrieval_run_object = self.build_retrieval_run_object(
            tool_name=tool_name,
            query_event=query_event,
            outcome_event=outcome_event,
            retrieval_result=retrieval_result,
        )
        retrieval_run_object.filters.update(
            {
                "tool_name": tool_name,
                "session_id": query_event.get("session_id"),
                "agent": query_event.get("agent"),
                "success": bool(outcome_event.get("success")),
                "latency_ms": outcome_event.get("latency_ms"),
                "reward": outcome_event.get("reward"),
            },
        )
        if outcome_event.get("error"):
            retrieval_run_object.filters["error"] = outcome_event.get("error")
        self._knowledge_service.store_namespace_object(namespace_object)
        self._knowledge_service.store_retrieval_run_object(retrieval_run_object)
        return {
            "ok": True,
            "stored": True,
            "backend": "agents_db",
            "namespace_id": namespace_object.id,
            "retrieval_run_id": retrieval_run_object.id,
        }

    def _build_learning_context_results(
        self,
        *,
        tool_name: str,
        context_payload: Any,
    ) -> list[RetrievalResultObject]:
        if isinstance(context_payload, Mapping):
            candidate_items = context_payload.get("entries")
            if not isinstance(candidate_items, list):
                candidate_items = context_payload.get("chunks")
        elif isinstance(context_payload, list):
            candidate_items = context_payload
        else:
            candidate_items = []

        if not isinstance(candidate_items, list):
            candidate_items = []

        result_objects: list[RetrievalResultObject] = []
        for index, item in enumerate(candidate_items, start=1):
            if isinstance(item, Mapping):
                item_payload = dict(item)
                result_id = _first_non_empty_string(
                    [
                        item_payload.get("id"),
                        item_payload.get("source_path"),
                        item_payload.get("title"),
                        f"context:{index}",
                    ]
                )
                result_type = str(item_payload.get("owner_type") or item_payload.get("result_type") or "context").strip() or "context"
                source_stage = str(item_payload.get("source_stage") or tool_name).strip() or tool_name
                metadata = _deepcopy_object(item_payload)
                lexical_score = _first_number([item_payload.get("lexical_score")])
                vector_score = _first_number([item_payload.get("vector_score"), item_payload.get("score")])
                graph_score = _first_number([item_payload.get("graph_score")])
                rerank_score = _first_number([item_payload.get("rerank_score")])
            else:
                result_id = f"context:{index}"
                result_type = "context"
                source_stage = tool_name
                metadata = {"value": _deepcopy_object(item)}
                lexical_score = None
                vector_score = None
                graph_score = None
                rerank_score = None

            result_objects.append(
                RetrievalResultObject(
                    rank_no=index,
                    result_type=result_type,
                    result_id=result_id,
                    source_stage=source_stage,
                    chosen=True,
                    lexical_score=lexical_score,
                    vector_score=vector_score,
                    graph_score=graph_score,
                    rerank_score=rerank_score,
                    metadata=metadata,
                )
            )
        return result_objects

    def store_learning_interaction(
        self,
        *,
        tool_name: str,
        user_prompt: str,
        model_result: Any = None,
        context_payload: Any = None,
        pattern_signal: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        namespace_id: str | None = None,
    ) -> Mapping[str, Any]:
        namespace_handoff = {"namespace_id": namespace_id} if str(namespace_id or "").strip() else None
        namespace_object = self.load_namespace_object(
            handoff_metadata=namespace_handoff,
            handoff_payload=namespace_handoff,
        )

        created_at = _now_utc()
        prompt_text = str(user_prompt or "").strip()
        context_results = self._build_learning_context_results(tool_name=tool_name, context_payload=context_payload)

        prompt_sha = _stable_sha256(prompt_text)
        model_digest_source = json.dumps(_deepcopy_object(model_result), sort_keys=True, ensure_ascii=False, default=str)
        model_result_sha = _stable_sha256(model_digest_source)
        resolved_correlation_id = _first_non_empty_string([correlation_id, prompt_sha[:16], f"learning:{created_at.timestamp()}"])

        retrieval_run_object = RetrievalRunObject(
            id=f"learning:{resolved_correlation_id}:{int(created_at.timestamp() * 1000)}",
            tenant_id=namespace_object.tenant_id,
            namespace_id=namespace_object.id,
            query_text=prompt_text,
            requested_k=max(1, len(context_results) or 1),
            lexical_k=None,
            graph_hops=None,
            vector_k=max(1, len(context_results) or 1),
            rerank_strategy="pattern_embedding_v1",
            correlation_id=resolved_correlation_id,
            filters={
                "tool_name": str(tool_name or "").strip(),
                "learning_signal": True,
                "prompt_sha256": prompt_sha,
                "model_result_sha256": model_result_sha,
                "context_items": len(context_results),
                "pattern_signal": _deepcopy_object(pattern_signal or {}),
            },
            results=context_results,
            created_at=created_at,
        )

        if model_result is not None:
            retrieval_run_object.filters["model_result_excerpt"] = str(model_result)[:1200]
        if isinstance(context_payload, Mapping):
            retrieval_run_object.filters["context_metadata"] = {
                "has_entries": bool(context_payload.get("entries")),
                "has_chunks": bool(context_payload.get("chunks")),
            }

        self._knowledge_service.store_namespace_object(namespace_object)
        self._knowledge_service.store_retrieval_run_object(retrieval_run_object)
        return {
            "ok": True,
            "stored": True,
            "backend": "agents_db",
            "namespace_id": namespace_object.id,
            "learning_run_id": retrieval_run_object.id,
        }


_AGENTS_DB_PIPELINE_SERVICE_CACHE: dict[tuple[str, ...], PipelineService] = {}


def load_agentsdb_runtime_config_from_env() -> RuntimeConfigObject | None:
    connection_config = _load_agentsdb_connection_config()
    agents_db_uri = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_URI", "")).strip()
    if not agents_db_uri:
        agents_db_uri = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI", "")).strip()
    if not agents_db_uri:
        configured_socket_uri = _load_agentsdb_uri_from_connection_config(connection_config)
        backend_uri = _connection_config_value(connection_config, ("backend_uri", "agents_db_backend_uri", "storage_uri", "storage_backend_uri"))
        agents_db_uri = configured_socket_uri or backend_uri
    if not agents_db_uri:
        return None
    agents_db_uri = normalize_agentsdb_socket_uri(agents_db_uri, default_on_empty=False) or agents_db_uri
    default_embedding_dimension = _env_or_config_int_value(
        "AI_IDE_KNOWLEDGE_AGENTS_DB_EMBEDDING_DIMENSION",
        connection_config,
        ("default_embedding_dimension", "embedding_dimension"),
        3072,
    )
    return RuntimeConfigObject(
        agents_db_uri=agents_db_uri,
        database_name=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_NAME",
            connection_config,
            ("database_name", "database"),
            "alde_knowledge",
        )
        or "alde_knowledge",
        tenant_id=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_TENANT_ID",
            connection_config,
            ("tenant_id",),
            "tenant_default",
        )
        or "tenant_default",
        namespace_id=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_ID",
            connection_config,
            ("namespace_id",),
            "ns_alde_default",
        )
        or "ns_alde_default",
        namespace_slug=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_SLUG",
            connection_config,
            ("namespace_slug",),
            "alde-default",
        )
        or "alde-default",
        namespace_name=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_NAME",
            connection_config,
            ("namespace_name",),
            "ALDE Default Knowledge",
        )
        or "ALDE Default Knowledge",
        default_embedding_model=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_EMBEDDING_MODEL",
            connection_config,
            ("default_embedding_model", "embedding_model"),
            "text-embedding-3-large",
        )
        or "text-embedding-3-large",
        default_embedding_dimension=max(1, default_embedding_dimension),
        index_backend=_env_or_config_value(
            "AI_IDE_KNOWLEDGE_AGENTS_DB_INDEX_BACKEND",
            connection_config,
            ("index_backend",),
            "faiss",
        )
        or "faiss",
    )
##

def load_agentsdb_pipeline_service(runtime_config: RuntimeConfigObject) -> PipelineService:
    cache_key = (
        runtime_config.agents_db_uri,
        runtime_config.database_name,
        runtime_config.tenant_id,
        runtime_config.namespace_id,
        runtime_config.namespace_slug,
        runtime_config.namespace_name,
        runtime_config.default_embedding_model,
        str(runtime_config.default_embedding_dimension),
        runtime_config.index_backend,
    )
    existing_service = _AGENTS_DB_PIPELINE_SERVICE_CACHE.get(cache_key)
    if existing_service is not None:
        return existing_service
    repository_factory = AgentDbRepositoryFactory(
        AgentDbRepositoryFactoryConfig(
            backend_uri=runtime_config.agents_db_uri,
            default_database_name=runtime_config.database_name,
        )
    )
    repository = repository_factory.load_repository()
    repository.ensure_index_objects()
    pipeline_service = PipelineService(KnowledgeObjectService(repository), runtime_config)
    _AGENTS_DB_PIPELINE_SERVICE_CACHE[cache_key] = pipeline_service
    return pipeline_service

##

def sync_retrieval_run_to_agentsdb_knowledge(
    *,
    tool_name: str,
    query_event: Mapping[str, Any],
    outcome_event: Mapping[str, Any],
    retrieval_result: Any,
) -> Mapping[str, Any] | None:
    runtime_config = load_agentsdb_runtime_config_from_env()
    if runtime_config is None:
        return None
    try:
        pipeline_service = load_agentsdb_pipeline_service(runtime_config)
        return pipeline_service.store_retrieval_run(
            tool_name=tool_name,
            query_event=query_event,
            outcome_event=outcome_event,
            retrieval_result=retrieval_result,
        )
    except Exception as exc:
        return {
            "ok": False,
            "stored": False,
            "backend": "agents_db",
            "error": "agents_db_sync_failed",
            "detail": str(exc),
        }


def sync_learning_interaction_to_agentsdb_knowledge(
    *,
    tool_name: str,
    user_prompt: str,
    model_result: Any = None,
    context_payload: Any = None,
    pattern_signal: Mapping[str, Any] | None = None,
    correlation_id: str | None = None,
    namespace_id: str | None = None,
) -> Mapping[str, Any] | None:
    runtime_config = load_agentsdb_runtime_config_from_env()
    if runtime_config is None:
        return None
    try:
        pipeline_service = load_agentsdb_pipeline_service(runtime_config)
        return pipeline_service.store_learning_interaction(
            tool_name=tool_name,
            user_prompt=user_prompt,
            model_result=model_result,
            context_payload=context_payload,
            pattern_signal=pattern_signal,
            correlation_id=correlation_id,
            namespace_id=namespace_id,
        )
    except Exception as exc:
        return {
            "ok": False,
            "stored": False,
            "backend": "agents_db",
            "error": "agents_db_learning_sync_failed",
            "detail": str(exc),
        }


def sync_parser_result_to_agentsdb_knowledge(
    *,
    object_name: str,
    result_payload: Mapping[str, Any],
    correlation_id: str | None = None,
    handoff_metadata: Mapping[str, Any] | None = None,
    handoff_payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    runtime_config = load_agentsdb_runtime_config_from_env()
    if runtime_config is None:
        return None
    try:
        pipeline_service = load_agentsdb_pipeline_service(runtime_config)
        mapping_service = ObjectMappingService(
            pipeline_service._knowledge_service,
            runtime_config,
        )
        return mapping_service.store_mapped_object(
            object_name=object_name,
            result_payload=result_payload,
            fallback_correlation_id=correlation_id,
            handoff_metadata=handoff_metadata,
            handoff_payload=handoff_payload,
        )
    except Exception as exc:
        return {
            "ok": False,
            "stored": False,
            "backend": "agents_db",
            "object_name": _normalize_document_object_name(object_name),
            "error": "agents_db_parser_sync_failed",
            "detail": str(exc),
        }


def build_demo_agentsdb_service(database: Any) -> KnowledgeObjectService:
    repository = KnowledgeRepository(database)
    repository.ensure_index_objects()
    return KnowledgeObjectService(repository)


# ---------------------------------------------------------------------------
# AgentMemoryService
# Migrated from agents_factory – belongs here because it extends the index
# definitions that constitute the AgentsDB core contract.
# ---------------------------------------------------------------------------

def _normalize_agent_label_for_memory(agent_name: str) -> str:
    try:
        try:
            from .agents_config import normalize_agent_label  # type: ignore
        except ImportError:
            from alde.agents_config import normalize_agent_label  # type: ignore
        return normalize_agent_label(agent_name)
    except Exception:
        return str(agent_name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _normalize_tool_name_for_memory(tool_name: str) -> str:
    try:
        try:
            from .agents_config import normalize_tool_name  # type: ignore
        except ImportError:
            from alde.agents_config import normalize_tool_name  # type: ignore
        return normalize_tool_name(tool_name)
    except Exception:
        return str(tool_name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _get_specialized_system_prompt_for_memory(agent_label: str, memory_slot: str) -> str:
    try:
        try:
            from .agents_config import get_specialized_system_prompt  # type: ignore
        except ImportError:
            from alde.agents_config import get_specialized_system_prompt  # type: ignore
        return str(get_specialized_system_prompt(agent_label, memory_slot) or "")
    except Exception:
        return ""


def _get_job_config_for_memory(job_name: str) -> dict[str, Any]:
    try:
        try:
            from .agents_config import get_job_config  # type: ignore
        except ImportError:
            from alde.agents_config import get_job_config  # type: ignore
        loaded_config = get_job_config(str(job_name or "").strip())
        return dict(loaded_config) if isinstance(loaded_config, Mapping) else {}
    except Exception:
        return {}


def _get_workflow_config_for_memory(workflow_name: str) -> dict[str, Any]:
    try:
        try:
            from .agents_config import get_workflow_config  # type: ignore
        except ImportError:
            from alde.agents_config import get_workflow_config  # type: ignore
        loaded_config = get_workflow_config(str(workflow_name or "").strip())
        return dict(loaded_config) if isinstance(loaded_config, Mapping) else {}
    except Exception:
        return {}


def _get_skill_profile_config_for_memory(skill_profile_name: str) -> dict[str, Any]:
    try:
        try:
            from .agents_config import AGENT_SKILL_PROFILES  # type: ignore
        except ImportError:
            from alde.agents_config import AGENT_SKILL_PROFILES  # type: ignore
        loaded_config = (AGENT_SKILL_PROFILES or {}).get(str(skill_profile_name or "").strip())
        return dict(loaded_config) if isinstance(loaded_config, Mapping) else {}
    except Exception:
        return {}


def _get_document_repository_for_memory() -> Any:
    try:
        try:
            from .agents_tools import DOCUMENT_REPOSITORY  # type: ignore
        except ImportError:
            from alde.agents_tools import DOCUMENT_REPOSITORY  # type: ignore
        return DOCUMENT_REPOSITORY
    except Exception:
        return None


def _get_workflow_context_thread_id_for_memory() -> int | None:
    try:
        try:
            from .agents_factory import WORKFLOW_CONTEXT_SERVICE  # type: ignore
        except ImportError:
            from alde.agents_factory import WORKFLOW_CONTEXT_SERVICE  # type: ignore
        return WORKFLOW_CONTEXT_SERVICE.load_current_thread_id()
    except Exception:
        return None


class AgentMemoryService:
    """Domain service for managing per-agent session memory inside AgentsDB.

    Extends the AgentsDB index definitions by introducing the ``agent_memory``
    object collection – therefore this class belongs to the ``agents_db``
    module, which is the authoritative home of all DB-level index and schema
    definitions.
    """

    AGENT_MEMORY_OBJECT_NAME = "agent_memory"
    MAX_SESSION_CONTEXT_ENTRIES = 12
    MAX_MESSAGE_CONTEXT_ENTRIES = 3
    MAX_MESSAGE_PAYLOAD_CHARS = 2500

    # Backward-compatibility wrappers for legacy call sites.
    def load_memory_slot(
        self,
        *,
        job_name: str | None = None,
        tool_name: str | None = None,
    ) -> str:
        return self.load_amemo_slot(job_name=job_name, tool_name=tool_name)

    def load_object_record(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
    ) -> dict[str, Any]:
        return self.load_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )

    def store_object_record(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
        object_memory: dict[str, Any],
        source_agent_label: str | None = None,
    ) -> bool:
        return self.store_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
            object_memory=object_memory,
            source_agent_label=source_agent_label,
        )

    def build_object_profile(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
    ) -> dict[str, Any]:
        return self.amemo_profile(
            agent_label=agent_label,
            memory_slot=memory_slot,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
        )

    def ensure_object_memory(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
        source_agent_label: str | None = None,
    ) -> dict[str, Any]:
        return self.ensure_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
            source_agent_label=source_agent_label,
        )

    def load_amemo_slot(
        self,
        *,
        job_name: str | None = None,
        tool_name: str | None = None,
    ) -> str:
        if isinstance(job_name, str) and job_name.strip():
            return job_name.strip()
        if isinstance(tool_name, str) and tool_name.strip():
            return _normalize_tool_name_for_memory(tool_name.strip())
        return "default"

    def load_session_scope_key(
        self,
        *,
        scope_key: str | None = None,
        thread_id: int | None = None,
    ) -> str:
        if isinstance(scope_key, str) and scope_key.strip():
            return scope_key.strip()
        resolved_thread_id = thread_id if thread_id is not None else _get_workflow_context_thread_id_for_memory()
        if resolved_thread_id is None:
            return "thread:global"
        return f"thread:{resolved_thread_id}"

    def build_object_correlation_id(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
    ) -> str:
        normalized_agent_label = _normalize_agent_label_for_memory(agent_label)
        normalized_memory_slot = str(memory_slot or "default").strip() or "default"
        normalized_scope_key = str(scope_key or "thread:global").strip() or "thread:global"
        raw_identifier = f"{normalized_agent_label}|{normalized_memory_slot}|{normalized_scope_key}"
        identifier_hash = hashlib.sha1(raw_identifier.encode("utf-8")).hexdigest()[:16]
        return f"agent_memory:{normalized_agent_label}:{normalized_memory_slot}:{identifier_hash}"

    def _stable_payload(self, payload: Any) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(payload)

    def _clip_payload_for_message(self, payload: Any) -> Any:
        serialized_payload = self._stable_payload(payload)
        if len(serialized_payload) <= self.MAX_MESSAGE_PAYLOAD_CHARS:
            return payload
        return {
            "truncated": True,
            "preview": serialized_payload[: self.MAX_MESSAGE_PAYLOAD_CHARS],
        }

    def load_amemo(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
    ) -> dict[str, Any]:
        correlation_id = self.build_object_correlation_id(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )
        document_repository = _get_document_repository_for_memory()
        if document_repository is None:
            return {}
        try:
            stored_record = document_repository.get_document(
                correlation_id,
                obj_name=self.AGENT_MEMORY_OBJECT_NAME,
            )
        except Exception:
            return {}

        if not isinstance(stored_record, Mapping):
            return {}
        section = stored_record.get(self.AGENT_MEMORY_OBJECT_NAME)
        if isinstance(section, Mapping):
            return dict(section)
        return {}

    def store_amemo(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
        object_memory: dict[str, Any],
        source_agent_label: str | None = None,
    ) -> bool:
        correlation_id = self.build_object_correlation_id(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )
        normalized_agent_label = _normalize_agent_label_for_memory(agent_label)
        normalized_source_agent = _normalize_agent_label_for_memory(source_agent_label or normalized_agent_label)

        payload: dict[str, Any] = {
            "agent": normalized_source_agent,
            "job_name": memory_slot,
            "parse": {"language": "json", "errors": [], "warnings": []},
            self.AGENT_MEMORY_OBJECT_NAME: _deepcopy_object(object_memory),
            "db_updates": {"processing_state": "stored", "processed": True},
        }

        document_repository = _get_document_repository_for_memory()
        if document_repository is None:
            return False
        try:
            document_repository.persist_document(
                correlation_id=correlation_id,
                result_payload=payload,
                obj_name=self.AGENT_MEMORY_OBJECT_NAME,
                handoff_metadata={
                    "agent_label": normalized_agent_label,
                    "job_name": memory_slot,
                    "scope_key": scope_key,
                    "object_name": self.AGENT_MEMORY_OBJECT_NAME,
                },
                handoff_payload={"output": _deepcopy_object(object_memory)},
            )
        except Exception:
            return False
        return True

    def amemo_profile(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
    ) -> dict[str, Any]:
        runtime = dict(runtime_metadata or {})
        job_skill_profiles = runtime.get("job_skill_profiles")
        if not isinstance(job_skill_profiles, dict):
            job_skill_profiles = {}
        tool_skill_profiles = runtime.get("tool_skill_profiles")
        if not isinstance(tool_skill_profiles, dict):
            tool_skill_profiles = {}

        selected_job_prompt = ""
        if memory_slot and memory_slot != "default":
            selected_job_prompt = _get_specialized_system_prompt_for_memory(agent_label, memory_slot)

        resolved_jobs = {str(job_name) for job_name in job_skill_profiles.keys() if str(job_name).strip()}
        if memory_slot and memory_slot != "default":
            resolved_jobs.add(str(memory_slot))

        return {
            "agent_label": _normalize_agent_label_for_memory(agent_label),
            "memory_slot": memory_slot,
            "jobs": sorted(resolved_jobs),
            "skills": {
                "agent_skill_profile": runtime.get("skill_profile") or "",
                "job_skill_profiles": _deepcopy_object(job_skill_profiles),
                "tool_skill_profiles": _deepcopy_object(tool_skill_profiles),
            },
            "prompts": {
                "system": str(system_prompt or ""),
                "job": selected_job_prompt,
            },
            "runtime": {
                "role": runtime.get("role") or "",
                "instance_policy": runtime.get("instance_policy") or "",
                "selection_mode": runtime.get("selection_mode") or "",
                "workflow_name": runtime.get("workflow_name") or "",
            },
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def ensure_amemo(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
        source_agent_label: str | None = None,
    ) -> dict[str, Any]:
        normalized_slot = self.load_amemo_slot(job_name=memory_slot, tool_name="")
        normalized_scope_key = self.load_session_scope_key(scope_key=scope_key)
        existing_memory = self.load_amemo(
            agent_label=agent_label,
            memory_slot=normalized_slot,
            scope_key=normalized_scope_key,
        )
        baseline_memory = _deepcopy_object(existing_memory) if isinstance(existing_memory, dict) else {}

        baseline_memory["agent_profile"] = self.amemo_profile(
            agent_label=agent_label,
            memory_slot=normalized_slot,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
        )
        session_context = baseline_memory.get("session_context")
        if not isinstance(session_context, dict):
            session_context = {}
        entries = session_context.get("entries")
        if not isinstance(entries, list):
            entries = []
        session_context["entries"] = entries
        session_context["scope_key"] = normalized_scope_key
        baseline_memory["session_context"] = session_context
        baseline_memory["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")

        if self._stable_payload(existing_memory) != self._stable_payload(baseline_memory):
            self.store_amemo(
                agent_label=agent_label,
                memory_slot=normalized_slot,
                scope_key=normalized_scope_key,
                object_memory=baseline_memory,
                source_agent_label=source_agent_label,
            )
        return baseline_memory

    def append_session_context(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
        context_type: str,
        payload: dict[str, Any],
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
        source_agent_label: str | None = None,
    ) -> bool:
        object_memory = self.ensure_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
            source_agent_label=source_agent_label,
        )

        session_context = object_memory.get("session_context")
        if not isinstance(session_context, dict):
            session_context = {}
        entries = session_context.get("entries")
        if not isinstance(entries, list):
            entries = []

        payload_fingerprint = hashlib.sha1(self._stable_payload(payload).encode("utf-8")).hexdigest()
        if entries:
            last_entry = entries[-1] if isinstance(entries[-1], dict) else {}
            if (
                str(last_entry.get("context_type") or "") == str(context_type or "")
                and str(last_entry.get("payload_fingerprint") or "") == payload_fingerprint
            ):
                return True

        entry = {
            "context_type": str(context_type or "session_context"),
            "payload": _deepcopy_object(payload),
            "payload_fingerprint": payload_fingerprint,
            "source_agent": _normalize_agent_label_for_memory(source_agent_label or agent_label),
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        entries.append(entry)
        if len(entries) > self.MAX_SESSION_CONTEXT_ENTRIES:
            entries = entries[-self.MAX_SESSION_CONTEXT_ENTRIES :]

        session_context["entries"] = entries
        session_context["scope_key"] = self.load_session_scope_key(scope_key=scope_key)
        object_memory["session_context"] = session_context
        object_memory["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")

        return self.store_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
            object_memory=object_memory,
            source_agent_label=source_agent_label,
        )

    def load_session_cache_message(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
    ) -> dict[str, str] | None:
        object_memory = self.load_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )
        if not object_memory:
            return None
        session_context = object_memory.get("session_context")
        if not isinstance(session_context, dict):
            return None
        entries = session_context.get("entries")
        if not isinstance(entries, list) or not entries:
            return None

        selected_entries = entries[-self.MAX_MESSAGE_CONTEXT_ENTRIES :]
        serialized_entries: list[dict[str, Any]] = []
        for entry in selected_entries:
            if not isinstance(entry, dict):
                continue
            serialized_entries.append(
                {
                    "context_type": str(entry.get("context_type") or "session_context"),
                    "source_agent": str(entry.get("source_agent") or ""),
                    "timestamp": str(entry.get("timestamp") or ""),
                    "payload": self._clip_payload_for_message(entry.get("payload")),
                }
            )

        if not serialized_entries:
            return None

        snapshot_payload = {
            "agent_label": _normalize_agent_label_for_memory(agent_label),
            "memory_slot": memory_slot,
            "scope_key": self.load_session_scope_key(scope_key=scope_key),
            "entries": serialized_entries,
        }
        content = (
            "Session cache context (agentsdb agent_memory) for "
            f"{memory_slot}:\n"
            f"{json.dumps(snapshot_payload, ensure_ascii=False)}"
        )
        return {"role": "user", "content": content}

    def load_handoff_target_memory_slot(
        self,
        *,
        fallback_memory_slot: str,
        handoff_metadata: dict[str, Any],
        output_payload: dict[str, Any],
        action_object: str,
    ) -> str:
        sequence_payload = output_payload.get("sequence") if isinstance(output_payload.get("sequence"), dict) else {}
        for candidate in (
            handoff_metadata.get("session_cache_memory_slot"),
            handoff_metadata.get("memory_slot"),
            handoff_metadata.get("writer_job_name"),
            output_payload.get("memory_slot"),
            output_payload.get("writer_job_name"),
            output_payload.get("job_name"),
            sequence_payload.get("job_name"),
            sequence_payload.get("writer_job_name"),
            handoff_metadata.get("job_name"),
            fallback_memory_slot,
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        resolved_action = str(output_payload.get("action") or "").strip().lower()
        if resolved_action == action_object:
            return action_object
        return str(fallback_memory_slot or "").strip()

    def load_handoff_session_context_entries(
        self,
        *,
        output_payload: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        context_entries: list[tuple[str, dict[str, Any]]] = []
        options_payload = _deepcopy_object(output_payload.get("options")) if isinstance(output_payload.get("options"), dict) else None

        def _append_dict_context(
            context_type: str,
            field_name: str,
            *,
            include_options: bool = False,
        ) -> None:
            value = output_payload.get(field_name)
            if not isinstance(value, dict):
                return
            payload: dict[str, Any] = {field_name: _deepcopy_object(value)}
            if include_options and isinstance(options_payload, dict):
                payload["options"] = _deepcopy_object(options_payload)
            context_entries.append((context_type, payload))

        _append_dict_context("object_result", "object_result")
        _append_dict_context("object_result", "object_result", include_options=True)
        _append_dict_context("dispatcher_updates", "dispatcher_updates")
        _append_dict_context("applicant_profile", "applicant_profile")
        _append_dict_context("profile_result", "profile_result")
        _append_dict_context("job_posting_result", "job_posting_result")

        if isinstance(options_payload, dict):
            context_entries.append(("options", {"options": _deepcopy_object(options_payload)}))

        return context_entries

    def cache_handoff_session_context(
        self,
        *,
        target_agent_label: str,
        target_memory_slot: str,
        source_agent_label: str | None,
        handoff_payload: dict[str, Any] | None,
        handoff_metadata: dict[str, Any] | None,
        thread_id: int | None,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
    ) -> bool:
        payload = dict(handoff_payload or {})
        metadata = dict(handoff_metadata or {})
        output_payload = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        if not isinstance(output_payload, dict):
            return False

        context_entries = self.load_handoff_session_context_entries(output_payload=output_payload)
        if not context_entries:
            return False

        resolved_memory_slot = self.load_handoff_target_memory_slot(
            fallback_memory_slot=target_memory_slot,
            handoff_metadata=metadata,
            output_payload=output_payload,
            action_object=str(target_memory_slot or "").strip(),
        )
        if not resolved_memory_slot:
            return False

        session_scope_key = self.load_session_scope_key(
            scope_key=str(metadata.get("session_cache_scope_key") or "").strip() or None,
            thread_id=thread_id,
        )
        target_runtime_metadata = dict(runtime_metadata or {})
        target_runtime_metadata["job_name"] = resolved_memory_slot

        stored_any = False
        for context_type, cache_payload in context_entries:
            stored_any = self.append_session_context(
                agent_label=target_agent_label,
                memory_slot=resolved_memory_slot,
                scope_key=session_scope_key,
                context_type=context_type,
                payload=cache_payload,
                runtime_metadata=target_runtime_metadata,
                system_prompt=system_prompt,
                source_agent_label=source_agent_label,
            ) or stored_any
        stored_attachments = OBJECT_MEMORY_ATTACHMENT_SERVICE.cache_attachment_context(
            target_agent_label=target_agent_label,
            target_memory_slot=resolved_memory_slot,
            source_agent_label=source_agent_label,
            handoff_payload=payload,
            handoff_metadata=metadata,
            scope_key=session_scope_key,
            runtime_metadata=target_runtime_metadata,
            system_prompt=system_prompt,
        )
        return stored_any or stored_attachments

    def cache_dispatch_profile_context(
        self,
        *,
        target_agent_label: str,
        target_memory_slot: str,
        source_agent_label: str | None,
        handoff_payload: dict[str, Any] | None,
        handoff_metadata: dict[str, Any] | None,
        thread_id: int | None,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
    ) -> bool:
        return self.cache_handoff_session_context(
            target_agent_label=target_agent_label,
            target_memory_slot=target_memory_slot,
            source_agent_label=source_agent_label,
            handoff_payload=handoff_payload,
            handoff_metadata=handoff_metadata,
            thread_id=thread_id,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
        )


class AgentMemoryAttachmentService:
    
    ATTACHMENT_CONTEXT_TYPE = "ATTACHMENT"
    MAX_ATTACHMENT_DOCUMENTS = 4
    GENERIC_CORRELATION_KEYS: tuple[str, ...] = (
        "correlation_id",
        "id",
        "_id",
        "content_sha256",
        "sha256",
        "profile_id",
        "job_id",
        "uuid",
        "j_id",
    )
    GENERIC_OBJECT_NAME_KEYS: tuple[str, ...] = (
        "obj_name",
        "object_name",
        "object",
        "entity",
        "kind",
        "type",
    )
    GENERIC_RESULT_CONTAINER_KEYS: tuple[str, ...] = ("result", "parsed")

    def __init__(self, agent_memory_service: AgentMemoryService) -> None:
        self.agent_memory_service = agent_memory_service

    def _first_non_empty(self, candidates: Sequence[Any]) -> str:
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    def _normalize_attachment_obj_name(self, value: str | None) -> str:
        normalized_value = str(value or "").strip().lower()
        alias_map = {
            "profile": "profiles",
            "profile_result": "profiles",
            "applicant_profile": "profiles",
            "job_posting": "job_postings",
            "job_posting_result": "job_postings",
            "parsed_job_posting": "job_postings",
        }
        return alias_map.get(normalized_value, normalized_value)

    def _collect_runtime_job_names(
        self,
        *,
        handoff_metadata: dict[str, Any],
        output_payload: dict[str, Any],
        runtime_metadata: dict[str, Any],
    ) -> list[str]:
        resolved_names: list[str] = []
        seen_names: set[str] = set()

        def _append_name(candidate: Any) -> None:
            normalized = str(candidate or "").strip()
            if not normalized or normalized in seen_names:
                return
            seen_names.add(normalized)
            resolved_names.append(normalized)

        sequence_payload = output_payload.get("sequence") if isinstance(output_payload.get("sequence"), Mapping) else {}
        for candidate in (
            handoff_metadata.get("job_name"),
            output_payload.get("job_name"),
            sequence_payload.get("job_name") if isinstance(sequence_payload, Mapping) else None,
            runtime_metadata.get("job_name"),
        ):
            _append_name(candidate)

        runtime_job_skill_profiles = runtime_metadata.get("job_skill_profiles")
        if isinstance(runtime_job_skill_profiles, Mapping):
            for job_name in runtime_job_skill_profiles.keys():
                _append_name(job_name)

        return resolved_names

    def _collect_runtime_skill_profiles(
        self,
        *,
        runtime_metadata: dict[str, Any],
        job_names: Sequence[str],
    ) -> list[str]:
        resolved_profiles: list[str] = []
        seen_profiles: set[str] = set()

        def _append_profile(candidate: Any) -> None:
            normalized = str(candidate or "").strip()
            if not normalized or normalized in seen_profiles:
                return
            seen_profiles.add(normalized)
            resolved_profiles.append(normalized)

        _append_profile(runtime_metadata.get("skill_profile"))
        runtime_job_skill_profiles = runtime_metadata.get("job_skill_profiles")
        if isinstance(runtime_job_skill_profiles, Mapping):
            for job_name in job_names:
                _append_profile(runtime_job_skill_profiles.get(job_name))

        for job_name in job_names:
            job_config = _get_job_config_for_memory(job_name)
            if not job_config:
                continue
            _append_profile(job_config.get("skill_profile"))

        return resolved_profiles

    def _collect_runtime_workflow_names(self, *, job_names: Sequence[str]) -> list[str]:
        resolved_workflows: list[str] = []
        seen_workflows: set[str] = set()
        for job_name in job_names:
            job_config = _get_job_config_for_memory(job_name)
            workflow_name = str(job_config.get("workflow_name") or "").strip()
            if not workflow_name or workflow_name in seen_workflows:
                continue
            seen_workflows.add(workflow_name)
            resolved_workflows.append(workflow_name)
        return resolved_workflows

    def _collect_runtime_default_obj_names(self, *, job_names: Sequence[str]) -> list[str]:
        resolved_names: list[str] = []
        seen_names: set[str] = set()

        def _append_obj_name(candidate: Any) -> None:
            normalized = self._normalize_attachment_obj_name(str(candidate or ""))
            if not normalized or normalized in seen_names:
                return
            seen_names.add(normalized)
            resolved_names.append(normalized)

        for job_name in job_names:
            job_config = _get_job_config_for_memory(job_name)
            _append_obj_name(job_config.get("default_object_name"))

            workflow_name = str(job_config.get("workflow_name") or "").strip()
            if not workflow_name:
                continue
            workflow_config = _get_workflow_config_for_memory(workflow_name)
            _append_obj_name(workflow_config.get("default_object_name"))
            workflow_object_defaults = workflow_config.get("object_defaults")
            if isinstance(workflow_object_defaults, Mapping):
                for value in workflow_object_defaults.values():
                    _append_obj_name(value)

        return resolved_names

    def _load_runtime_key_hints(
        self,
        *,
        handoff_metadata: dict[str, Any],
        output_payload: dict[str, Any],
        runtime_metadata: dict[str, Any],
        job_names: Sequence[str],
        skill_profiles: Sequence[str],
        workflow_names: Sequence[str],
        default_obj_names: Sequence[str],
    ) -> list[str]:
        key_hints: set[str] = {
            "correlation_id",
            "obj_name",
            "parsed",
            "result",
            "id",
            "object_result",
            "dispatcher_updates",
        }

        sequence_payload = output_payload.get("sequence") if isinstance(output_payload.get("sequence"), Mapping) else {}
        for candidate in (
            handoff_metadata.get("result_field"),
            handoff_metadata.get("parsed_field"),
            handoff_metadata.get("payload_field"),
            handoff_metadata.get("correlation_field"),
            output_payload.get("result_field"),
            output_payload.get("parsed_field"),
            output_payload.get("payload_field"),
            output_payload.get("correlation_field"),
            sequence_payload.get("result_field") if isinstance(sequence_payload, Mapping) else None,
            sequence_payload.get("parsed_field") if isinstance(sequence_payload, Mapping) else None,
            sequence_payload.get("payload_field") if isinstance(sequence_payload, Mapping) else None,
            sequence_payload.get("correlation_field") if isinstance(sequence_payload, Mapping) else None,
        ):
            normalized = re.sub(r"[^a-z0-9_]+", "_", str(candidate or "").strip().lower()).strip("_")
            if normalized:
                key_hints.add(normalized)

        runtime_text_hints = [
            str(item)
            for item in (
                *job_names,
                *skill_profiles,
                *workflow_names,
                str(handoff_metadata.get("handoff_schema") or ""),
                str(handoff_metadata.get("sequence_name") or ""),
            )
            if str(item or "").strip()
        ]
        for hint in runtime_text_hints:
            normalized_hint = re.sub(r"[^a-z0-9_]+", "_", hint.lower()).strip("_")
            if not normalized_hint:
                continue
            if "profile" in normalized_hint:
                key_hints.update({"applicant_profile", "profile_result", "profile", "parsed_profile"})
            if "job_posting" in normalized_hint or "posting" in normalized_hint:
                key_hints.update({"job_posting_result", "job_posting", "parsed_job_posting"})
            if "dispatch" in normalized_hint or "object" in normalized_hint:
                key_hints.update({"object_result", "dispatcher_updates", "result", "parsed"})
            if "writer" in normalized_hint or "cover_letter" in normalized_hint:
                key_hints.update({"cover_letter", "cover_letter_result", "document"})
            if "parser" in normalized_hint:
                key_hints.update({"parsed", "result"})

        for default_obj_name in default_obj_names:
            if default_obj_name == "profiles":
                key_hints.update({"applicant_profile", "profile_result", "profile", "parsed_profile"})
            elif default_obj_name == "job_postings":
                key_hints.update({"job_posting_result", "job_posting", "parsed_job_posting"})
            elif default_obj_name == "cover_letters":
                key_hints.update({"cover_letter", "cover_letter_result", "document"})
            elif default_obj_name == "emails":
                key_hints.update({"email", "email_result", "message"})
            elif default_obj_name == "documents":
                key_hints.update({"document", "object_result", "result", "parsed"})

        for skill_profile_name in skill_profiles:
            skill_profile = _get_skill_profile_config_for_memory(skill_profile_name)
            normalized_job_name = str(skill_profile.get("job_name") or "").strip().lower()
            if "profile" in normalized_job_name:
                key_hints.update({"applicant_profile", "profile_result"})
            if "posting" in normalized_job_name:
                key_hints.update({"job_posting_result", "job_posting"})

        return [key for key in sorted(key_hints) if key]

    def _load_correlation_candidates_from_payload(self, payload_value: Mapping[str, Any]) -> list[Any]:
        candidates: list[Any] = []

        nested_payloads: list[Mapping[str, Any]] = [payload_value]
        for container_key in ("metadata", "value", *self.GENERIC_RESULT_CONTAINER_KEYS):
            container = payload_value.get(container_key)
            if isinstance(container, Mapping):
                nested_payloads.append(container)

        for nested_payload in nested_payloads:
            for key_name in self.GENERIC_CORRELATION_KEYS:
                candidates.append(nested_payload.get(key_name))

        return candidates

    def _infer_obj_name_from_field(
        self,
        *,
        source_field: str,
        default_obj_names: Sequence[str],
    ) -> str:
        normalized_field = re.sub(r"[^a-z0-9_]+", "_", str(source_field or "").strip().lower()).strip("_")
        if "profile" in normalized_field:
            return "profiles"
        if "job_posting" in normalized_field or "posting" in normalized_field:
            return "job_postings"
        if "cover_letter" in normalized_field or normalized_field.startswith("cv"):
            return "cover_letters"
        if "mail" in normalized_field or "email" in normalized_field:
            return "emails"
        if default_obj_names:
            return str(default_obj_names[0] or "")
        return ""

    def _iter_attachment_source_objects(
        self,
        *,
        output_payload: dict[str, Any],
        key_hints: Sequence[str],
    ) -> Iterable[tuple[str, dict[str, Any]]]:
        emitted_keys: set[tuple[str, int]] = set()

        def _emit(source_field: str, source_value: Any) -> Iterable[tuple[str, dict[str, Any]]]:
            if not isinstance(source_value, Mapping):
                return []
            cache_key = (str(source_field), id(source_value))
            if cache_key in emitted_keys:
                return []
            emitted_keys.add(cache_key)
            return [(str(source_field), dict(source_value))]

        for key_hint in key_hints:
            if key_hint in output_payload:
                for emitted in _emit(key_hint, output_payload.get(key_hint)):
                    yield emitted

        for source_field, source_value in output_payload.items():
            if not isinstance(source_value, Mapping):
                continue
            for emitted in _emit(str(source_field), source_value):
                yield emitted

        for container_key in self.GENERIC_RESULT_CONTAINER_KEYS:
            container_payload = output_payload.get(container_key)
            if not isinstance(container_payload, Mapping):
                continue
            for emitted in _emit(container_key, container_payload):
                yield emitted
            for nested_key, nested_value in container_payload.items():
                if not isinstance(nested_value, Mapping):
                    continue
                source_field = f"{container_key}.{nested_key}"
                for emitted in _emit(source_field, nested_value):
                    yield emitted

    def _append_attachment_from_source(
        self,
        attachment_objects: list[dict[str, Any]],
        *,
        source_field: str,
        source_payload: dict[str, Any],
        metadata: dict[str, Any],
        output_payload: dict[str, Any],
        default_obj_names: Sequence[str],
    ) -> None:
        correlation_candidates = self._load_correlation_candidates_from_payload(source_payload)
        correlation_candidates.extend(
            [
                output_payload.get("correlation_id"),
                output_payload.get("id"),
                metadata.get("correlation_id"),
                metadata.get("id"),
                metadata.get("_id"),
            ]
        )

        obj_name_candidates: list[Any] = [
            source_payload.get(key_name)
            for key_name in self.GENERIC_OBJECT_NAME_KEYS
        ]
        inferred_obj_name = self._infer_obj_name_from_field(
            source_field=source_field,
            default_obj_names=default_obj_names,
        )
        if inferred_obj_name:
            obj_name_candidates.append(inferred_obj_name)

        resolved_obj_name = self._first_non_empty(obj_name_candidates)
        if not resolved_obj_name:
            return

        self._append_attachment_object(
            attachment_objects,
            attachment_type=source_field,
            obj_name=resolved_obj_name,
            correlation_candidates=tuple(correlation_candidates),
            source_field=source_field,
        )

    def _append_attachment_object(
        self,
        attachment_objects: list[dict[str, Any]],
        *,
        attachment_type: str,
        obj_name: str,
        correlation_candidates: Sequence[Any],
        source_field: str,
    ) -> None:
        resolved_correlation_id = self._first_non_empty(correlation_candidates)
        if not resolved_correlation_id:
            return
        resolved_obj_name = self._normalize_attachment_obj_name(obj_name)
        if not resolved_obj_name:
            return

        existing_keys = {
            (
                str(item.get("obj_name") or "").strip(),
                str(item.get("correlation_id") or "").strip(),
            )
            for item in attachment_objects
            if isinstance(item, Mapping)
        }
        object_key = (resolved_obj_name, resolved_correlation_id)
        if object_key in existing_keys:
            return

        attachment_objects.append(
            {
                "attachment_type": str(attachment_type or "generic_attachment").strip() or "generic_attachment",
                "obj_name": resolved_obj_name,
                "correlation_id": resolved_correlation_id,
                "source_field": str(source_field or "").strip(),
            }
        )

    def load_attachment_payload(
        self,
        *,
        handoff_payload: dict[str, Any] | None,
        handoff_metadata: dict[str, Any] | None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(handoff_payload or {})
        metadata = dict(handoff_metadata or {})
        runtime = dict(runtime_metadata or {})
        output_payload = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        if not isinstance(output_payload, dict):
            return {}

        runtime_job_names = self._collect_runtime_job_names(
            handoff_metadata=metadata,
            output_payload=output_payload,
            runtime_metadata=runtime,
        )
        runtime_skill_profiles = self._collect_runtime_skill_profiles(
            runtime_metadata=runtime,
            job_names=runtime_job_names,
        )
        runtime_workflow_names = self._collect_runtime_workflow_names(job_names=runtime_job_names)
        runtime_default_obj_names = self._collect_runtime_default_obj_names(job_names=runtime_job_names)
        key_hints = self._load_runtime_key_hints(
            handoff_metadata=metadata,
            output_payload=output_payload,
            runtime_metadata=runtime,
            job_names=runtime_job_names,
            skill_profiles=runtime_skill_profiles,
            workflow_names=runtime_workflow_names,
            default_obj_names=runtime_default_obj_names,
        )

        attachment_objects: list[dict[str, Any]] = []
        for source_field, source_payload in self._iter_attachment_source_objects(
            output_payload=output_payload,
            key_hints=key_hints,
        ):
            self._append_attachment_from_source(
                attachment_objects,
                source_field=source_field,
                source_payload=source_payload,
                metadata=metadata,
                output_payload=output_payload,
                default_obj_names=runtime_default_obj_names,
            )

        # Fall back to top-level generic keys whenever possible.
        self._append_attachment_from_source(
            attachment_objects,
            source_field="output",
            source_payload=output_payload,
            metadata=metadata,
            output_payload=output_payload,
            default_obj_names=runtime_default_obj_names,
        )

        if not attachment_objects:
            return {}

        sequence_payload = output_payload.get("sequence") if isinstance(output_payload.get("sequence"), dict) else {}
        return {
            "attachments": attachment_objects,
            "job_name": self._first_non_empty(
                (
                    metadata.get("job_name"),
                    output_payload.get("job_name"),
                    sequence_payload.get("job_name"),
                    runtime.get("job_name"),
                )
            ),
            "cached_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def load_handoff_attachment_payload(
        self,
        *,
        handoff_payload: dict[str, Any] | None,
        handoff_metadata: dict[str, Any] | None,
        runtime_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper for the legacy handoff-specific method name."""
        return self.load_attachment_payload(
            handoff_payload=handoff_payload,
            handoff_metadata=handoff_metadata,
            runtime_metadata=runtime_metadata,
        )

    def cache_attachment_context(
        self,
        *,
        target_agent_label: str,
        target_memory_slot: str,
        source_agent_label: str | None,
        handoff_payload: dict[str, Any] | None,
        handoff_metadata: dict[str, Any] | None,
        scope_key: str,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
    ) -> bool:
        attachment_payload = self.load_attachment_payload(
            handoff_payload=handoff_payload,
            handoff_metadata=handoff_metadata,
            runtime_metadata=runtime_metadata,
        )
        if not attachment_payload:
            return False

        return self.agent_memory_service.append_session_context(
            agent_label=target_agent_label,
            memory_slot=target_memory_slot,
            scope_key=scope_key,
            context_type=self.ATTACHMENT_CONTEXT_TYPE,
            payload=attachment_payload,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
            source_agent_label=source_agent_label,
        )

    def cache_handoff_attachment_context(
        self,
        *,
        target_agent_label: str,
        target_memory_slot: str,
        source_agent_label: str | None,
        handoff_payload: dict[str, Any] | None,
        handoff_metadata: dict[str, Any] | None,
        scope_key: str,
        runtime_metadata: dict[str, Any] | None,
        system_prompt: str,
    ) -> bool:
        """Compatibility wrapper for the legacy handoff-specific method name."""
        return self.cache_attachment_context(
            target_agent_label=target_agent_label,
            target_memory_slot=target_memory_slot,
            source_agent_label=source_agent_label,
            handoff_payload=handoff_payload,
            handoff_metadata=handoff_metadata,
            scope_key=scope_key,
            runtime_metadata=runtime_metadata,
            system_prompt=system_prompt,
        )

    def load_object_attachment_entries(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
    ) -> list[dict[str, Any]]:
        object_memory = self.agent_memory_service.load_amemo(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )
        if not object_memory:
            return []
        session_context = object_memory.get("session_context")
        if not isinstance(session_context, Mapping):
            return []
        entries = session_context.get("entries")
        if not isinstance(entries, list):
            return []

        attachment_entries: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            context_type = str(entry.get("context_type") or "").strip()
            if context_type.upper() != self.ATTACHMENT_CONTEXT_TYPE:
                continue
            payload = entry.get("payload") if isinstance(entry.get("payload"), Mapping) else {}
            attachment_objects = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
            for attachment in attachment_objects:
                if not isinstance(attachment, Mapping):
                    continue
                resolved_obj_name = self._normalize_attachment_obj_name(str(attachment.get("obj_name") or ""))
                resolved_correlation_id = str(attachment.get("correlation_id") or "").strip()
                if not resolved_obj_name or not resolved_correlation_id:
                    continue
                object_key = (resolved_obj_name, resolved_correlation_id)
                if object_key in seen_keys:
                    continue
                seen_keys.add(object_key)
                attachment_entries.append(
                    {
                        "attachment_type": str(attachment.get("attachment_type") or "attachment").strip() or "attachment",
                        "obj_name": resolved_obj_name,
                        "correlation_id": resolved_correlation_id,
                        "source_field": str(attachment.get("source_field") or "").strip(),
                        "source_agent": str(entry.get("source_agent") or "").strip(),
                        "timestamp": str(entry.get("timestamp") or "").strip(),
                    }
                )
        return attachment_entries

    def load_object_attachment_documents(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
        max_documents: int | None = None,
    ) -> list[dict[str, Any]]:
        attachment_entries = self.load_object_attachment_entries(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )
        if not attachment_entries:
            return []

        document_repository = _get_document_repository_for_memory()
        if document_repository is None:
            return []

        resolved_limit = max(1, int(max_documents or self.MAX_ATTACHMENT_DOCUMENTS))
        attachment_documents: list[dict[str, Any]] = []
        for attachment in attachment_entries:
            if len(attachment_documents) >= resolved_limit:
                break
            obj_name = str(attachment.get("obj_name") or "").strip()
            correlation_id = str(attachment.get("correlation_id") or "").strip()
            if not obj_name or not correlation_id:
                continue
            try:
                document_payload = document_repository.get_document(correlation_id, obj_name=obj_name)
            except Exception:
                document_payload = None
            if not isinstance(document_payload, Mapping):
                continue
            attachment_documents.append(
                {
                    **dict(attachment),
                    "document": _deepcopy_object(dict(document_payload)),
                }
            )
        return attachment_documents

    def load_attachment_context_message(
        self,
        *,
        agent_label: str,
        memory_slot: str,
        scope_key: str,
    ) -> dict[str, str] | None:
        attachment_documents = self.load_object_attachment_documents(
            agent_label=agent_label,
            memory_slot=memory_slot,
            scope_key=scope_key,
        )
        if not attachment_documents:
            return None

        max_payload_chars = max(800, int(getattr(self.agent_memory_service, "MAX_MESSAGE_PAYLOAD_CHARS", 2500) or 2500))
        serialized_documents: list[dict[str, Any]] = []
        for attachment in attachment_documents:
            document_payload = attachment.get("document") if isinstance(attachment.get("document"), Mapping) else {}
            document_text = json.dumps(document_payload, ensure_ascii=False, sort_keys=True, default=str)
            if len(document_text) > max_payload_chars:
                clipped_document_payload: dict[str, Any] = {
                    "truncated": True,
                    "preview": document_text[:max_payload_chars],
                }
            else:
                clipped_document_payload = _deepcopy_object(dict(document_payload))

            serialized_documents.append(
                {
                    "attachment_type": str(attachment.get("attachment_type") or "attachment"),
                    "obj_name": str(attachment.get("obj_name") or ""),
                    "correlation_id": str(attachment.get("correlation_id") or ""),
                    "source_agent": str(attachment.get("source_agent") or ""),
                    "timestamp": str(attachment.get("timestamp") or ""),
                    "document": clipped_document_payload,
                }
            )

        message_payload = {
            "agent_label": _normalize_agent_label_for_memory(agent_label),
            "memory_slot": memory_slot,
            "scope_key": self.agent_memory_service.load_session_scope_key(scope_key=scope_key),
            "attachments": serialized_documents,
        }
        content = (
            "Session attachment documents (agentsdb agent_memory) for "
            f"{memory_slot}:\n"
            f"{json.dumps(message_payload, ensure_ascii=False)}"
        )
        return {"role": "user", "content": content}


AGENT_MEMORY_SERVICE = AgentMemoryService()
OBJECT_MEMORY_ATTACHMENT_SERVICE = AgentMemoryAttachmentService(AGENT_MEMORY_SERVICE)
AGENT_MEMORY_ATTACHMENT_SERVICE = OBJECT_MEMORY_ATTACHMENT_SERVICE

# Backward-compatibility exports for legacy imports.
ObjectMemoryAttachmentService = AgentMemoryAttachmentService
MemoryAttachmentService = AgentMemoryAttachmentService


import html

class GraphViewService:
    _RELATION_LIMIT = 0
    _CATALOG_LIMIT = 0
    _WIDGET_PATH_ALIASES = {"adbgraphview"}
    _DEFAULT_WIDGET_KIND = "graph_view" 
    _DEFAULT_TOOL_ID = "graph_view"
    _RELATIONS_VIEW_KIND = "relations_graph"
    _CATALOG_VIEW_KIND = "catalog_graph"
    _WORKFLOW_VIEW_KIND = "workflow_diagram"
    _SEQUENCE_VIEW_KIND = "sequence_diagram"
    _GRAPH_LINK_SCHEME = "alde"
    _GRAPH_LINK_HOST = "graph"
    _TOOL_ITEMS: tuple[tuple[str, str, str], ...] = (
        ("agent_relation_graph", "Agent Relation Graph", "agentsdb"),
        ("web_app", "Web App Artifact", "native/web"),
        ("workflow_diagram", "Workflow Diagram", "agentsdb/mcp"),
        ("sequence_diagram", "Sequence Diagram", "agentsdb/mcp"),
  
    )
    _REMOTE_TOOL_IDS: frozenset[str] = frozenset(
        tool_id
        for tool_id, _label, transport in _TOOL_ITEMS
        if str(transport or "").strip().lower().startswith("agentsdb/")
    )
    _TOOL_RUNTIME_CLASSES: dict[str, tuple[str, ...]] = {
        "agent_relation_graph": (
            "AgentRelationGraphService",
            "RelationGraphWidgetArtifactFactory",
            "RuntimeWidget",
        ),
   
        "workflow_diagram": (
            "AgentRelationGraphService",
            "RelationGraphWidgetArtifactFactory",
            "RuntimeWidget",
        ),
        "sequence_diagram": (
            "AgentRelationGraphService",
            "RelationGraphWidgetArtifactFactory",
            "RuntimeWidget",
        ),
       
 
    }
    _TOOL_RUNTIME_ARTIFACTS: dict[str, dict[str, Any]] = {
        "agent_relation_graph": {
            "artifact_kind": "native_qwidget",
            "delivery_mode": "service_bundle",
            "bundle_scope": "dependency_closure",
            "artifact_uri": "agentsdb://127.0.0.1:2331/artifacts/agent_relation_graph",
            "artifact_version": "2026-06-12",
            "entry_module": "alde.widget_artifacts.relation_graph_artifact",
            "entry_class": "RelationGraphWidgetArtifactFactory",
            "build_method": "load_object_widget",
            "bundle_files": [
                "__init__.py",
                "artifact_backends.py",
                "widget_artifacts/relation_graph_artifact.py",
            ],
        },
      
        "workflow_diagram": {
            "artifact_kind": "native_qwidget",
            "delivery_mode": "service_bundle",
            "bundle_scope": "dependency_closure",
            "artifact_uri": "agentsdb://127.0.0.1:2331/artifacts/workflow_diagram",
            "artifact_version": "2026-06-12",
            "entry_module": "alde.widget_artifacts.relation_graph_artifact",
            "entry_class": "RelationGraphWidgetArtifactFactory",
            "build_method": "load_object_widget",
            "bundle_files": [
                "__init__.py",
                "artifact_backends.py",
                "widget_artifacts/relation_graph_artifact.py",
            ],
        },
        "sequence_diagram": {
            "artifact_kind": "native_qwidget",
            "delivery_mode": "service_bundle",
            "bundle_scope": "dependency_closure",
            "artifact_uri": "agentsdb://127.0.0.1:2331/artifacts/sequence_diagram",
            "artifact_version": "2026-06-12",
            "entry_module": "alde.widget_artifacts.relation_graph_artifact",
            "entry_class": "RelationGraphWidgetArtifactFactory",
            "build_method": "load_object_widget",
            "bundle_files": [
                "__init__.py",
                "artifact_backends.py",
                "widget_artifacts/relation_graph_artifact.py",
            ],
        },
     
    }

    def _resolve_tool_id_for_manifest(self, *, tool_id: str | None = None, source_uri: str | None = None) -> str:
        explicit_tool_id = str(tool_id or "").strip()
        if explicit_tool_id:
            return explicit_tool_id

        source_uri_text = str(source_uri or "").strip()
        if source_uri_text:
            lower_uri = source_uri_text.lower()
            for marker in ("/tools:", "/tools/"):
                marker_index = lower_uri.find(marker)
                if marker_index < 0:
                    continue
                tail_text = source_uri_text[marker_index + len(marker):].strip()
                if not tail_text:
                    break
                candidate = unquote(tail_text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]).strip()
                if candidate:
                    return candidate

            parsed_uri = urlparse(source_uri_text)
            path_parts = [unquote(part) for part in str(parsed_uri.path or "").split("/") if str(part or "").strip()]
            if len(path_parts) >= 2 and str(path_parts[0]).strip().lower() == "tools":
                candidate = str(path_parts[1] or "").strip()
                if candidate:
                    return candidate

        return self._DEFAULT_TOOL_ID

    def _load_runtime_classes_for_tool(self, tool_id: str | None = None) -> list[str]:
        resolved_tool_id = str(tool_id or self._DEFAULT_TOOL_ID).strip() or self._DEFAULT_TOOL_ID
        class_rows = self._TOOL_RUNTIME_CLASSES.get(resolved_tool_id)
        if not class_rows:
            class_rows = ("AgentRelationGraphService", "RelationGraphWidgetArtifactFactory", "RuntimeWidget")
        return [str(class_name) for class_name in class_rows if str(class_name or "").strip()]

    def _load_runtime_artifact_cache_root(self) -> Path:
        project_root = Path(__file__).resolve().parents[2]
        cache_root = (project_root / "AppData" / "runtime_artifacts").resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        return cache_root

    def _load_runtime_artifact_source_root(self) -> Path:
        return Path(__file__).resolve().parent

    def _load_complete_module_bundle_files(self) -> list[str]:
        source_root = self._load_runtime_artifact_source_root()
        excluded_directories = {
            "__pycache__",
            "venv",
            "symbols",
            "ctr",
            "_ctr_",
            "Dokumente",
            "Künstliche Intelligenz",
        }

        relative_paths: list[str] = []
        for source_path in source_root.rglob("*.py"):
            relative_path = source_path.relative_to(source_root)
            path_parts = [str(part) for part in relative_path.parts]
            if any(part in excluded_directories for part in path_parts):
                continue
            normalized_relative_path = str(relative_path).replace("\\", "/")
            if not normalized_relative_path:
                continue
            relative_paths.append(normalized_relative_path)

        relative_paths.sort()
        return relative_paths

    def _load_module_name_from_relative_path(self, relative_path: str) -> str:
        normalized_relative_path = str(relative_path or "").strip().replace("\\", "/")
        if not normalized_relative_path:
            return "alde"
        if normalized_relative_path == "__init__.py":
            return "alde"
        if normalized_relative_path.endswith("/__init__.py"):
            module_tail = normalized_relative_path[: -len("/__init__.py")]
        elif normalized_relative_path.endswith(".py"):
            module_tail = normalized_relative_path[:-3]
        else:
            module_tail = normalized_relative_path
        module_tail = module_tail.replace("/", ".").strip(".")
        return f"alde.{module_tail}" if module_tail else "alde"

    def _load_relative_path_for_module(self, module_name: str) -> str | None:
        normalized_module_name = str(module_name or "").strip()
        if not normalized_module_name or normalized_module_name == "alde":
            return "__init__.py"
        if not normalized_module_name.startswith("alde."):
            return None

        module_tail = normalized_module_name.split(".", 1)[1]
        module_relpath = module_tail.replace(".", "/")
        source_root = self._load_runtime_artifact_source_root()

        module_file = (source_root / f"{module_relpath}.py").resolve()
        if module_file.exists() and module_file.is_file():
            return f"{module_relpath}.py"

        package_init_file = (source_root / module_relpath / "__init__.py").resolve()
        if package_init_file.exists() and package_init_file.is_file():
            return f"{module_relpath}/__init__.py"
        return None

    def _load_local_module_imports(self, source_path: Path, *, current_relative_path: str) -> set[str]:
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except Exception:
            return set()
        try:
            module_tree = ast.parse(source_text)
        except Exception:
            return set()

        current_module_name = self._load_module_name_from_relative_path(current_relative_path)
        current_module_parts = [part for part in current_module_name.split(".") if part]
        current_package_parts = current_module_parts[:-1]
        imported_module_names: set[str] = set()

        for node in module_tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_name = str(alias.name or "").strip()
                    if imported_name == "alde" or imported_name.startswith("alde."):
                        imported_module_names.add(imported_name)
                continue

            if not isinstance(node, ast.ImportFrom):
                continue

            if int(node.level or 0) > 0:
                level = int(node.level or 0)
                ascends = max(0, level - 1)
                if ascends > len(current_package_parts):
                    continue
                base_parts = current_package_parts[: len(current_package_parts) - ascends]
                module_parts = [part for part in str(node.module or "").split(".") if part]
                if module_parts:
                    imported_module_names.add(".".join(base_parts + module_parts))
                else:
                    for alias in node.names:
                        alias_name = str(alias.name or "").strip()
                        if not alias_name or alias_name == "*":
                            continue
                        imported_module_names.add(".".join(base_parts + [alias_name]))
                continue

            module_name = str(node.module or "").strip()
            if module_name == "alde":
                imported_module_names.add("alde")
                for alias in node.names:
                    alias_name = str(alias.name or "").strip()
                    if alias_name and alias_name != "*":
                        imported_module_names.add(f"alde.{alias_name}")
            elif module_name.startswith("alde."):
                imported_module_names.add(module_name)

        return {
            str(module_name)
            for module_name in imported_module_names
            if str(module_name or "").strip()
        }

    def _load_dependency_closure_bundle_files(self, artifact_payload: Mapping[str, Any] | None) -> list[str]:
        source_root = self._load_runtime_artifact_source_root()
        entry_module = str((artifact_payload or {}).get("entry_module") or "").strip()
        entry_relative_path = self._load_relative_path_for_module(entry_module)

        seed_paths: set[str] = {"__init__.py"}
        explicit_paths = [
            str(item).strip().replace("\\", "/")
            for item in ((artifact_payload or {}).get("bundle_files") or [])
            if str(item or "").strip()
        ]
        seed_paths.update(explicit_paths)
        if entry_relative_path:
            seed_paths.add(entry_relative_path)

        pending_paths = [path for path in seed_paths if path]
        resolved_paths: set[str] = set()

        while pending_paths:
            current_relative_path = str(pending_paths.pop()).strip().replace("\\", "/")
            if not current_relative_path or current_relative_path in resolved_paths:
                continue
            current_source_path = (source_root / current_relative_path).resolve()
            if not current_source_path.exists() or not current_source_path.is_file():
                continue

            resolved_paths.add(current_relative_path)
            for imported_module_name in self._load_local_module_imports(
                current_source_path,
                current_relative_path=current_relative_path,
            ):
                imported_relative_path = self._load_relative_path_for_module(imported_module_name)
                if imported_relative_path and imported_relative_path not in resolved_paths:
                    pending_paths.append(imported_relative_path)

        if not resolved_paths:
            return [
                "__init__.py",
                "artifact_backends.py",
                "widget_artifacts/relation_graph_artifact.py",
            ]

        resolved_list = sorted(resolved_paths)
        return resolved_list

    def _load_runtime_artifact_bundle_files(self, artifact_payload: Mapping[str, Any] | None) -> list[str]:
        bundle_scope = str((artifact_payload or {}).get("bundle_scope") or "").strip().lower()
        if bundle_scope == "complete_module":
            complete_module_files = self._load_complete_module_bundle_files()
            if complete_module_files:
                return complete_module_files
        if bundle_scope == "dependency_closure":
            dependency_files = self._load_dependency_closure_bundle_files(artifact_payload)
            if dependency_files:
                return dependency_files

        bundle_files = [
            str(item).strip().replace("\\", "/")
            for item in ((artifact_payload or {}).get("bundle_files") or [])
            if str(item or "").strip()
        ]
        if bundle_files:
            return bundle_files
        return [
            "__init__.py",
            "artifact_backends.py",
            "widget_artifacts/relation_graph_artifact.py",
        ]

    def _load_runtime_artifact_source_entries(self, artifact_payload: Mapping[str, Any] | None) -> list[tuple[str, Path]]:
        source_root = self._load_runtime_artifact_source_root()
        source_entries: list[tuple[str, Path]] = []
        for relative_path in self._load_runtime_artifact_bundle_files(artifact_payload):
            normalized_relative_path = str(relative_path or "").strip().lstrip("/")
            if not normalized_relative_path:
                continue
            source_path = (source_root / normalized_relative_path).resolve()
            if not source_path.exists() or not source_path.is_file():
                raise FileNotFoundError(f"runtime artifact source file missing: {normalized_relative_path}")
            source_entries.append((normalized_relative_path, source_path))
        return source_entries

    def _compute_runtime_artifact_sha256(self, artifact_payload: Mapping[str, Any] | None) -> str:
        digest = hashlib.sha256()
        for relative_path, source_path in self._load_runtime_artifact_source_entries(artifact_payload):
            digest.update(relative_path.encode("utf-8", errors="ignore"))
            digest.update(b"\0")
            digest.update(source_path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _materialize_runtime_artifact_bundle(
        self,
        *,
        tool_id: str,
        artifact_payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        resolved_artifact_payload = dict(artifact_payload or {})
        artifact_version = str(resolved_artifact_payload.get("artifact_version") or "latest").strip() or "latest"
        artifact_sha256 = str(resolved_artifact_payload.get("artifact_sha256") or "").strip() or self._compute_runtime_artifact_sha256(resolved_artifact_payload)
        cache_key = str(resolved_artifact_payload.get("cache_key") or "").strip() or f"{tool_id}-{artifact_version}-{artifact_sha256[:16]}"
        cache_root = self._load_runtime_artifact_cache_root()
        bundle_root = (cache_root / str(tool_id or "runtime_artifact") / cache_key).resolve()
        package_root = bundle_root / "alde"

        bundle_entries = self._load_runtime_artifact_source_entries(resolved_artifact_payload)
        if not package_root.exists():
            package_root.mkdir(parents=True, exist_ok=True)
        for relative_path, source_path in bundle_entries:
            target_path = (package_root / relative_path).resolve()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_bytes = source_path.read_bytes()
            if not target_path.exists() or target_path.read_bytes() != source_bytes:
                target_path.write_bytes(source_bytes)

        entry_module = str(resolved_artifact_payload.get("entry_module") or "").strip()
        entry_file = ""
        if entry_module:
            entry_segments = [segment for segment in entry_module.split(".") if str(segment or "").strip()]
            if entry_segments:
                entry_file = str((bundle_root / ("/".join(entry_segments) + ".py")).resolve())

        return {
            "tool_id": str(tool_id or "").strip(),
            "delivery_mode": str(resolved_artifact_payload.get("delivery_mode") or "service_bundle"),
            "artifact_uri": str(resolved_artifact_payload.get("artifact_uri") or ""),
            "artifact_version": artifact_version,
            "artifact_sha256": artifact_sha256,
            "cache_key": cache_key,
            "bundle_scope": str(resolved_artifact_payload.get("bundle_scope") or ""),
            "bundle_root": str(bundle_root),
            "entry_file": entry_file,
            "entry_module": str(resolved_artifact_payload.get("entry_module") or ""),
            "entry_class": str(resolved_artifact_payload.get("entry_class") or ""),
            "build_method": str(resolved_artifact_payload.get("build_method") or "load_object_widget"),
            "module_search_paths": [str(bundle_root)],
            "bundle_files": [relative_path for relative_path, _source_path in bundle_entries],
            "materialization_state": "ready",
        }

    def _load_runtime_artifact_for_tool(self, tool_id: str | None = None) -> dict[str, Any]:
        resolved_tool_id = str(tool_id or self._DEFAULT_TOOL_ID).strip() or self._DEFAULT_TOOL_ID
        artifact_payload = self._TOOL_RUNTIME_ARTIFACTS.get(resolved_tool_id)
        if not isinstance(artifact_payload, dict):
            artifact_payload = {
                "artifact_kind": "native_qwidget",
                "delivery_mode": "host_builtin",
                "entry_module": "alde.widget_artifacts.relation_graph_artifact",
                "entry_class": "RelationGraphWidgetArtifactFactory",
                "build_method": "load_object_widget",
            }
        return {
            "artifact_kind": str(artifact_payload.get("artifact_kind") or "native_qwidget"),
            "delivery_mode": str(artifact_payload.get("delivery_mode") or "host_builtin"),
            "artifact_uri": str(artifact_payload.get("artifact_uri") or ""),
            "artifact_version": str(artifact_payload.get("artifact_version") or ""),
            "artifact_sha256": self._compute_runtime_artifact_sha256(artifact_payload) if str(artifact_payload.get("delivery_mode") or "").strip() == "service_bundle" else str(artifact_payload.get("artifact_sha256") or ""),
            "cache_key": str(artifact_payload.get("cache_key") or ""),
            "bundle_scope": str(artifact_payload.get("bundle_scope") or ""),
            "entry_module": str(artifact_payload.get("entry_module") or ""),
            "entry_class": str(artifact_payload.get("entry_class") or ""),
            "build_method": str(artifact_payload.get("build_method") or "load_object_widget"),
            "module_search_paths": [
                str(item)
                for item in (artifact_payload.get("module_search_paths") or [])
                if str(item or "").strip()
            ],
            "bundle_files": self._load_runtime_artifact_bundle_files(artifact_payload),
        }

    def load_tool_runtime_manifest(self, *, tool_id: str | None = None, source_uri: str | None = None) -> dict[str, Any]:
        resolved_tool_id = self._resolve_tool_id_for_manifest(tool_id=tool_id, source_uri=source_uri)
        return {
            "tool_id": resolved_tool_id,
            "runtime_classes": self._load_runtime_classes_for_tool(resolved_tool_id),
            "runtime_artifact": self._load_runtime_artifact_for_tool(resolved_tool_id),
        }

    def load_runtime_artifact_bundle(
        self,
        *,
        tool_id: str | None = None,
        source_uri: str | None = None,
        manifest_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_tool_id = self._resolve_tool_id_for_manifest(tool_id=tool_id, source_uri=source_uri)
        resolved_manifest = dict(manifest_payload or self.load_tool_runtime_manifest(tool_id=resolved_tool_id, source_uri=source_uri) or {})
        runtime_artifact = resolved_manifest.get("runtime_artifact")
        artifact_payload = dict(runtime_artifact) if isinstance(runtime_artifact, Mapping) else {}

        delivery_mode = str(artifact_payload.get("delivery_mode") or "host_builtin").strip() or "host_builtin"
        if delivery_mode == "service_bundle":
            return self._materialize_runtime_artifact_bundle(tool_id=resolved_tool_id, artifact_payload=artifact_payload)

        return {
            "tool_id": resolved_tool_id,
            "delivery_mode": delivery_mode,
            "artifact_uri": str(artifact_payload.get("artifact_uri") or ""),
            "artifact_version": str(artifact_payload.get("artifact_version") or ""),
            "artifact_sha256": str(artifact_payload.get("artifact_sha256") or ""),
            "cache_key": str(artifact_payload.get("cache_key") or ""),
            "bundle_scope": str(artifact_payload.get("bundle_scope") or ""),
            "bundle_root": "",
            "entry_file": "",
            "entry_module": str(artifact_payload.get("entry_module") or ""),
            "entry_class": str(artifact_payload.get("entry_class") or ""),
            "build_method": str(artifact_payload.get("build_method") or "load_object_widget"),
            "module_search_paths": [
                str(item)
                for item in (artifact_payload.get("module_search_paths") or [])
                if str(item or "").strip()
            ],
            "bundle_files": self._load_runtime_artifact_bundle_files(artifact_payload),
            "materialization_state": "not_required",
        }

    def load_runtime_artifact_download_payload(
        self,
        *,
        tool_id: str | None = None,
        source_uri: str | None = None,
        manifest_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_tool_id = self._resolve_tool_id_for_manifest(tool_id=tool_id, source_uri=source_uri)
        resolved_manifest = dict(manifest_payload or self.load_tool_runtime_manifest(tool_id=resolved_tool_id, source_uri=source_uri) or {})
        runtime_artifact = resolved_manifest.get("runtime_artifact")
        artifact_payload = dict(runtime_artifact) if isinstance(runtime_artifact, Mapping) else {}
        resolved_bundle = self.load_runtime_artifact_bundle(
            tool_id=resolved_tool_id,
            source_uri=source_uri,
            manifest_payload=resolved_manifest,
        )

        file_rows: list[dict[str, Any]] = []
        for relative_path, source_path in self._load_runtime_artifact_source_entries(artifact_payload):
            file_rows.append(
                {
                    "relative_path": relative_path,
                    "encoding": "utf-8",
                    "content_type": "text/plain",
                    "content_text": source_path.read_text(encoding="utf-8"),
                    "content_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                }
            )

        return {
            "ok": True,
            "tool_id": resolved_tool_id,
            "source_uri": str(source_uri or "").strip(),
            "runtime_manifest": resolved_manifest,
            "runtime_artifact_bundle": resolved_bundle,
            "download_payload": {
                "transport": "mcp.tools/call",
                "bundle_format": "alde_python_package_utf8",
                "file_count": len(file_rows),
                "files": file_rows,
            },
        }

    def load_connection_preview(self, *, source_uri: str | None = None) -> dict[str, Any]:
        runtime_config = self._load_runtime_config()
        normalized_source_uri = str(source_uri or "").strip()

        connection_rows: list[dict[str, str]] = []
        seen_uri_values: set[str] = set()

        def _add_connection_row(uri_value: str, label: str, status: str) -> None:
            normalized_uri_value = str(uri_value or "").strip()
            if not normalized_uri_value:
                return
            if normalized_uri_value in seen_uri_values:
                return
            seen_uri_values.add(normalized_uri_value)
            connection_rows.append(
                {
                    "uri": normalized_uri_value,
                    "label": str(label or "connection"),
                    "status": str(status or "configured"),
                }
            )

        if normalized_source_uri:
            _add_connection_row(normalized_source_uri, "Requested source", "active")

        runtime_uri = str(getattr(runtime_config, "agents_db_uri", "") or "").strip() if runtime_config is not None else ""
        if runtime_uri:
            _add_connection_row(runtime_uri, "Runtime config", "configured")

        connection_config = _load_agentsdb_connection_config()
        connection_config_uri = _load_agentsdb_uri_from_connection_config(connection_config)
        if connection_config_uri:
            _add_connection_row(connection_config_uri, "Connection config", "configured")

        if not connection_rows:
            _add_connection_row("agentsdb://127.0.0.1:2331/tools:agent_relation_graph", "Default local", "fallback")

        tool_rows = []
        for tool_id, label, transport in self._TOOL_ITEMS:
            tool_rows.append(
                {
                    "tool_id": tool_id,
                    "label": label,
                    "transport": transport,
                    "runtime_classes": self._load_runtime_classes_for_tool(tool_id),
                    "runtime_artifact": {
                        "artifact_kind": "native_qwidget",
                        "delivery_mode": "host_builtin",
                        "entry_module": "alde.widget_artifacts.relation_graph_artifact",
                        "entry_class": "RelationGraphWidgetArtifactFactory",
                        "build_method": "load_object_widget",
                    },
                }
            )

        return {
            "status_text": "Connection control plane ready",
            "connections": connection_rows,
            "tools": tool_rows,
        }

    def load_widget_snapshot(
        self,
        *,
        tool_id: str | None = None,
        source_uri: str | None = None,
        relation_limit: int | None = 0,
        entity_limit: int | None = 0,
        catalog_limit: int | None = 0,
    ) -> dict[str, Any]:
        resolved_tool_id = str(tool_id or self._DEFAULT_TOOL_ID).strip() or self._DEFAULT_TOOL_ID
        resolved_source_uri = str(source_uri or "").strip()

        if resolved_tool_id == self._WORKFLOW_VIEW_KIND:
            return self._load_graphic_tool_placeholder_snapshot(
                tool_id=resolved_tool_id,
                source_uri=resolved_source_uri,
                view_kind=self._WORKFLOW_VIEW_KIND,
                status_text="Workflow diagram placeholder ready",
                message="Workflow diagram projection is reserved for remote runtime tooling.",
                detail_html=(
                    "<h3>Workflow Diagram</h3>"
                    "<p>This extension tab is reserved for a future graphical workflow projection.</p>"
                    "<ul>"
                    "<li>Source plan: Control Plane workflow state and runtime payload traces.</li>"
                    "<li>Target output: state graph, transitions, and active actor context.</li>"
                    "</ul>"
                ),
            )

        if resolved_tool_id == self._SEQUENCE_VIEW_KIND:
            return self._load_graphic_tool_placeholder_snapshot(
                tool_id=resolved_tool_id,
                source_uri=resolved_source_uri,
                view_kind=self._SEQUENCE_VIEW_KIND,
                status_text="Sequence diagram placeholder ready",
                message="Sequence diagram projection is reserved for remote runtime tooling.",
                detail_html=(
                    "<h3>Sequence Diagram</h3>"
                    "<p>This extension tab is reserved for a futurea sequence projection.</p>"
                    "<ul>"
                    "<li>Source plan: chat/tool/handoff traces from the monitoring surface.</li>"
                    "<li>Target output: chronological agent-to-agent and tool-call swimlanes.</li>"
                    "</ul>"
                ),
            )

        if resolved_tool_id in self._REMOTE_TOOL_IDS:
            return self._load_graphic_tool_placeholder_snapshot(
                tool_id=resolved_tool_id,
                source_uri=resolved_source_uri,
                view_kind=self._RELATIONS_VIEW_KIND,
                status_text="Graphic tool placeholder ready",
                message="Selected remote graphic tool is not live-enumerated yet.",
                detail_html=(
                    "<h3>Graphic Tool</h3>"
                    "<p>Selected remote graphic tool is reserved until live enumeration is exposed.</p>"
                ),
            )

        try:
            snapshot_payload = self.load_graph_snapshot(
                object_name=resolved_tool_id,
                source_uri=resolved_source_uri,
                relation_limit=relation_limit,
                entity_limit=entity_limit,
                catalog_limit=catalog_limit,
            )
        except Exception as exc:
            error_text = html.escape(f"{type(exc).__name__}: {exc}")
            return self._load_graphic_tool_placeholder_snapshot(
                tool_id=resolved_tool_id,
                source_uri=resolved_source_uri,
                view_kind=self._RELATIONS_VIEW_KIND,
                status_text="Graph refresh failed",
                message=f"Could not load relation graph: {type(exc).__name__}",
                detail_html=f"<h3>Relations Graph</h3><p>{error_text}</p>",
            )

        snapshot_payload["view_kind"] = str(snapshot_payload.get("view_kind") or self._RELATIONS_VIEW_KIND)
        return snapshot_payload

    def load_graph_view_state(
        self,
        graph_snapshot: Mapping[str, Any] | None,
        *,
        layout_spread: float = 1.0,
        selected_kind: str = "",
        selected_object_id: str = "",
    ) -> dict[str, Any]:
        snapshot_payload = dict(graph_snapshot or {})
        base_overview_html = str(snapshot_payload.get("detail_html") or "<p>No graph detail available.</p>")
        node_objects = [dict(item) for item in (snapshot_payload.get("nodes") or []) if isinstance(item, dict)]
        edge_objects = [dict(item) for item in (snapshot_payload.get("edges") or []) if isinstance(item, dict)]
        if not node_objects or not edge_objects:
            return {
                "has_graph": False,
                "message": str(snapshot_payload.get("message") or "No relation graph available."),
                "overview_html": base_overview_html,
                "detail_html": base_overview_html,
                "node_draw_objects": [],
                "edge_draw_objects": [],
                "render_commands": [],
                "item_center_by_key": {},
                "selected_kind": "",
                "selected_object_id": "",
            }

        node_objects = sorted(node_objects, key=lambda item: str(item.get("label") or item.get("node_id") or "").lower())
        node_by_id = {
            str(node_object.get("node_id") or "").strip(): dict(node_object)
            for node_object in node_objects
            if str(node_object.get("node_id") or "").strip()
        }
        rendered_edge_objects, edge_by_id, edges_by_node_id = self._prepare_interactive_edge_objects(edge_objects)
        if not rendered_edge_objects or not node_by_id:
            return {
                "has_graph": False,
                "message": str(snapshot_payload.get("message") or "No relation graph available."),
                "overview_html": base_overview_html,
                "detail_html": base_overview_html,
                "node_draw_objects": [],
                "edge_draw_objects": [],
                "render_commands": [],
                "item_center_by_key": {},
                "selected_kind": "",
                "selected_object_id": "",
            }

        resolved_selected_kind = str(selected_kind or "").strip().lower()
        resolved_selected_object_id = str(selected_object_id or "").strip()
        if resolved_selected_kind == "node" and resolved_selected_object_id not in node_by_id:
            resolved_selected_kind = ""
            resolved_selected_object_id = ""
        elif resolved_selected_kind == "edge" and resolved_selected_object_id not in edge_by_id:
            resolved_selected_kind = ""
            resolved_selected_object_id = ""
        elif resolved_selected_kind not in {"", "node", "edge"}:
            resolved_selected_kind = ""
            resolved_selected_object_id = ""

        highlight_node_ids: set[str] = set()
        highlight_edge_ids: set[str] = set()
        if resolved_selected_kind == "node" and resolved_selected_object_id:
            highlight_node_ids, highlight_edge_ids = self._load_connected_graph_component(
                edges_by_node_id,
                resolved_selected_object_id,
            )
        elif resolved_selected_kind == "edge" and resolved_selected_object_id:
            selected_edge_object = edge_by_id.get(resolved_selected_object_id)
            if isinstance(selected_edge_object, dict):
                source_node_id = str(selected_edge_object.get("source") or "").strip()
                target_node_id = str(selected_edge_object.get("target") or "").strip()
                seed_node_id = source_node_id or target_node_id
                highlight_node_ids, highlight_edge_ids = self._load_connected_graph_component(edges_by_node_id, seed_node_id)
                if source_node_id:
                    highlight_node_ids.add(source_node_id)
                if target_node_id:
                    highlight_node_ids.add(target_node_id)
                highlight_edge_ids.add(resolved_selected_object_id)

        node_positions_by_id = self._build_vector_graph_positions(node_objects, rendered_edge_objects)
        spread_factor = max(0.35, min(3.5, float(layout_spread or 1.0)))
        node_width = 168.0
        node_height = 72.0
        node_geometry_by_id: dict[str, tuple[float, float, float, float]] = {}
        node_draw_objects: list[dict[str, Any]] = []
        edge_draw_objects: list[dict[str, Any]] = []
        item_center_by_key: dict[tuple[str, str], tuple[float, float]] = {}

        for node_object in node_objects:
            node_id = str(node_object.get("node_id") or "").strip()
            if not node_id:
                continue
            base_center_x, base_center_y = node_positions_by_id.get(node_id, (0.0, 0.0))
            center_x = float(base_center_x) * spread_factor
            center_y = float(base_center_y) * spread_factor
            x_pos = center_x - (node_width / 2.0)
            y_pos = center_y - (node_height / 2.0)
            item_center_by_key[("node", node_id)] = (center_x, center_y)
            node_geometry_by_id[node_id] = (x_pos, y_pos, node_width, node_height)
            node_draw_objects.append(
                {
                    "node_id": node_id,
                    "x": x_pos,
                    "y": y_pos,
                    "width": node_width,
                    "height": node_height,
                    "label": self._wrap_graph_label(str(node_object.get("label") or node_id)),
                    "tooltip": f"Knoten öffnen: {str(node_object.get('label') or node_id)}",
                    "is_highlighted": (not highlight_node_ids or node_id in highlight_node_ids),
                }
            )

        for edge_object in rendered_edge_objects:
            source_node_id = str(edge_object.get("source") or "").strip()
            target_node_id = str(edge_object.get("target") or "").strip()
            source_geometry = node_geometry_by_id.get(source_node_id)
            target_geometry = node_geometry_by_id.get(target_node_id)
            if source_geometry is None or target_geometry is None:
                continue

            source_x, source_y, source_width, source_height = source_geometry
            target_x, target_y, target_width, target_height = target_geometry
            source_center_x = source_x + (source_width / 2.0)
            source_center_y = source_y + (source_height / 2.0)
            target_center_x = target_x + (target_width / 2.0)
            target_center_y = target_y + (target_height / 2.0)
            delta_x = target_center_x - source_center_x
            delta_y = target_center_y - source_center_y
            distance = max(1.0, math.hypot(delta_x, delta_y))
            unit_x = delta_x / distance
            unit_y = delta_y / distance
            start_x = source_center_x + unit_x * (source_width / 2.0)
            start_y = source_center_y + unit_y * (source_height / 2.0)
            end_x = target_center_x - unit_x * (target_width / 2.0)
            end_y = target_center_y - unit_y * (target_height / 2.0)
            edge_id = str(edge_object.get("edge_id") or "").strip()
            midpoint_x = (start_x + end_x) / 2.0
            midpoint_y = (start_y + end_y) / 2.0
            item_center_by_key[("edge", edge_id)] = (midpoint_x, midpoint_y)
            edge_draw_objects.append(
                {
                    "edge_id": edge_id,
                    "source": source_node_id,
                    "target": target_node_id,
                    "start_x": start_x,
                    "start_y": start_y,
                    "end_x": end_x,
                    "end_y": end_y,
                    "relation_type": str(edge_object.get("label") or "related_to"),
                    "label": self._short_graph_label(str(edge_object.get("label") or "related_to"), max_length=30),
                    "tooltip": f"Relation öffnen: {str(edge_object.get('label') or 'related_to')}",
                    "description": str(edge_object.get("description") or "").strip(),
                    "is_highlighted": (not highlight_edge_ids or edge_id in highlight_edge_ids),
                    "is_selected": (
                        resolved_selected_kind == "edge"
                        and bool(resolved_selected_object_id)
                        and edge_id == resolved_selected_object_id
                    ),
                }
            )

        if not node_draw_objects or not edge_draw_objects:
            return {
                "has_graph": False,
                "message": str(snapshot_payload.get("message") or "No relation graph available."),
                "overview_html": base_overview_html,
                "detail_html": base_overview_html,
                "node_draw_objects": [],
                "edge_draw_objects": [],
                "render_commands": [],
                "item_center_by_key": {},
                "selected_kind": "",
                "selected_object_id": "",
            }

        overview_html = (
            base_overview_html
            + "<hr>"
            + "<p><b>Interaktion:</b> Knoten oder Relationen im Graph anklicken, um Details und Navigationslinks zu öffnen. "
            + "Ctrl+Mausrad zoomt, Shift+Mausrad dreht, Alt+Mausrad zieht das Netz auseinander oder zusammen.</p>"
        )

        detail_html = overview_html
        if resolved_selected_kind == "node" and resolved_selected_object_id in node_by_id:
            detail_html = self._build_node_detail_html(
                resolved_selected_object_id,
                node_by_id,
                edge_by_id,
                edges_by_node_id,
            )
        elif resolved_selected_kind == "edge" and resolved_selected_object_id in edge_by_id:
            detail_html = self._build_edge_detail_html(resolved_selected_object_id, node_by_id, edge_by_id)

        render_commands = self._build_graph_render_commands(node_draw_objects, edge_draw_objects)

        return {
            "has_graph": True,
            "message": "",
            "overview_html": overview_html,
            "detail_html": detail_html,
            "node_draw_objects": node_draw_objects,
            "edge_draw_objects": edge_draw_objects,
            "render_commands": render_commands,
            "item_center_by_key": item_center_by_key,
            "selected_kind": resolved_selected_kind,
            "selected_object_id": resolved_selected_object_id,
        }

    def load_graph_view_state_from_payload(
        self,
        graph_snapshot: Mapping[str, Any] | None,
        payload: Mapping[str, Any] | None,
        *,
        layout_spread: float = 1.0,
        fallback_selected_kind: str = "",
        fallback_selected_object_id: str = "",
    ) -> dict[str, Any]:
        selected_kind = str(fallback_selected_kind or "").strip().lower()
        selected_object_id = str(fallback_selected_object_id or "").strip()
        payload_object = dict(payload or {})
        payload_kind = str(payload_object.get("kind") or "").strip().lower()
        if payload_kind == "node":
            selected_kind = "node"
            selected_object_id = str(payload_object.get("node_id") or "").strip()
        elif payload_kind == "edge":
            selected_kind = "edge"
            selected_object_id = str(payload_object.get("edge_id") or "").strip()

        return self.load_graph_view_state(
            graph_snapshot,
            layout_spread=layout_spread,
            selected_kind=selected_kind,
            selected_object_id=selected_object_id,
        )

    def load_graph_view_state_from_link(
        self,
        graph_snapshot: Mapping[str, Any] | None,
        url_value: Any,
        *,
        layout_spread: float = 1.0,
        fallback_selected_kind: str = "",
        fallback_selected_object_id: str = "",
    ) -> dict[str, Any]:
        selected_kind = str(fallback_selected_kind or "").strip().lower()
        selected_object_id = str(fallback_selected_object_id or "").strip()
        url_text = str(url_value.toString() if hasattr(url_value, "toString") else url_value)
        parsed_url = urlparse(url_text)
        if parsed_url.scheme != self._GRAPH_LINK_SCHEME or parsed_url.netloc != self._GRAPH_LINK_HOST:
            return self.load_graph_view_state(
                graph_snapshot,
                layout_spread=layout_spread,
                selected_kind=selected_kind,
                selected_object_id=selected_object_id,
            )

        path_parts = [unquote(part) for part in parsed_url.path.strip("/").split("/", 1)]
        if len(path_parts) != 2:
            return self.load_graph_view_state(
                graph_snapshot,
                layout_spread=layout_spread,
                selected_kind=selected_kind,
                selected_object_id=selected_object_id,
            )

        linked_kind = str(path_parts[0] or "").strip().lower()
        linked_object_id = str(path_parts[1] or "").strip()
        if linked_kind == "overview":
            selected_kind = ""
            selected_object_id = ""
        elif linked_kind in {"node", "edge"} and linked_object_id:
            selected_kind = linked_kind
            selected_object_id = linked_object_id

        return self.load_graph_view_state(
            graph_snapshot,
            layout_spread=layout_spread,
            selected_kind=selected_kind,
            selected_object_id=selected_object_id,
        )

    def load_graph_item_center(
        self,
        graph_view_state: Mapping[str, Any] | None,
        *,
        kind: str,
        object_id: str,
    ) -> tuple[float, float] | None:
        if not isinstance(graph_view_state, Mapping):
            return None
        center_by_key = graph_view_state.get("item_center_by_key")
        if not isinstance(center_by_key, Mapping):
            return None
        center = center_by_key.get((str(kind or "").strip(), str(object_id or "").strip()))
        if not isinstance(center, Sequence) or len(center) < 2:
            return None
        try:
            return float(center[0]), float(center[1])
        except Exception:
            return None

    def load_graph_snapshot(
        self,
        object_name: str | None = None,
        source_uri: str | None = None,
        relation_limit: int | None = None,
        entity_limit: int | None = None,
        catalog_limit: int | None = None,
    ) -> dict[str, Any]:
        runtime_config = self._load_runtime_config()
        graph_context = self._load_graph_request_context(
            runtime_config=runtime_config,
            object_name=object_name,
            source_uri=source_uri,
        )
        snapshot_metadata = graph_context["metadata"]
        projection_config = self._load_projection_config(snapshot_metadata)
        runtime_config = graph_context.get("runtime_config") or runtime_config
        resolved_relation_limit = _normalize_limit_value(relation_limit)
        if resolved_relation_limit is None:
            resolved_relation_limit = _normalize_limit_value(projection_config.get("relation_limit"))
        if resolved_relation_limit is None:
            resolved_relation_limit = _normalize_limit_value(self._RELATION_LIMIT)

        resolved_entity_limit = _normalize_limit_value(entity_limit)
        if resolved_entity_limit is None:
            resolved_entity_limit = _normalize_limit_value(projection_config.get("entity_limit"))

        resolved_catalog_limit = _normalize_limit_value(catalog_limit)
        if resolved_catalog_limit is None:
            resolved_catalog_limit = _normalize_limit_value(projection_config.get("catalog_limit"))
        if resolved_catalog_limit is None:
            resolved_catalog_limit = _normalize_limit_value(self._CATALOG_LIMIT)
        if runtime_config is None:
            return {
                "status_text": "AgentsDB runtime config missing",
                "message": "No AgentsDB runtime config found. Check AppData/agentsdb_connection.json or the AI_IDE_KNOWLEDGE_AGENTS_DB_* environment variables.",
                "detail_html": "<h3>Graph unavailable</h3><p>No AgentsDB runtime config found.</p>",
                "nodes": [],
                "edges": [],
                "metadata": snapshot_metadata,
                "source_uri": snapshot_metadata.get("source_uri", ""),
                "widget_uri": snapshot_metadata.get("widget_uri", ""),
                "widget_kind": snapshot_metadata.get("widget_kind", self._DEFAULT_WIDGET_KIND),
                "tool_id": snapshot_metadata.get("tool_id", self._DEFAULT_TOOL_ID),
                "view_kind": projection_config.get("view_kind", self._RELATIONS_VIEW_KIND),
            }

        repository = self._load_repository(runtime_config)

        projection_mode = str(projection_config.get("mode") or "relations").strip().lower()
        if projection_mode == "catalog":
            return self._load_catalog_snapshot(
                repository=repository,
                runtime_config=runtime_config,
                snapshot_metadata=snapshot_metadata,
                projection_config=projection_config,
                catalog_limit=resolved_catalog_limit,
            )

        namespace_scope = str(projection_config.get("namespace_scope") or "single").strip().lower()
        cluster_by = str(projection_config.get("cluster_by") or "").strip().lower()
        if namespace_scope == "all" and not cluster_by:
            cluster_by = "namespace"
        resolved_namespace_id = str(
            projection_config.get("namespace_id")
            or getattr(runtime_config, "namespace_id", "")
            or ""
        ).strip()
        namespace_label = "all" if namespace_scope == "all" else (resolved_namespace_id or "n/a")

        relation_objects = self._load_relation_objects(
            repository,
            "" if namespace_scope == "all" else resolved_namespace_id,
            relation_limit=resolved_relation_limit,
        )
        if not relation_objects:
            fallback_repository = self._load_memory_fallback_repository(runtime_config)
            if fallback_repository is not None:
                fallback_relations = self._load_relation_objects(
                    fallback_repository,
                    "" if namespace_scope == "all" else resolved_namespace_id,
                    relation_limit=resolved_relation_limit,
                )
                if fallback_relations:
                    repository = fallback_repository
                    relation_objects = fallback_relations
        if not relation_objects:
            namespace_id = html.escape(namespace_label)
            database_name = html.escape(str(getattr(runtime_config, "database_name", "") or "n/a"))
            return {
                "status_text": f"No relations in {namespace_label}",
                "message": "No relation objects were found for the active namespace.",
                "detail_html": (
                    f"<h3>Relations Graph</h3><p>No relation objects were found in namespace <code>{namespace_id}</code> "
                    f"for database <code>{database_name}</code>.</p>"
                ),
                "nodes": [],
                "edges": [],
                "metadata": snapshot_metadata,
                "source_uri": snapshot_metadata.get("source_uri", ""),
                "widget_uri": snapshot_metadata.get("widget_uri", ""),
                "widget_kind": snapshot_metadata.get("widget_kind", self._DEFAULT_WIDGET_KIND),
                "tool_id": snapshot_metadata.get("tool_id", self._DEFAULT_TOOL_ID),
                "view_kind": projection_config.get("view_kind", self._RELATIONS_VIEW_KIND),
            }

        node_payload_by_id = self._load_node_payload_by_id(
            repository,
            relation_objects,
            entity_limit=resolved_entity_limit,
            relation_limit=resolved_relation_limit,
        )
        relation_type_counts: dict[str, int] = {}
        edge_objects: list[dict[str, Any]] = []
        node_objects_by_id: dict[str, dict[str, Any]] = {}
        namespace_nodes: dict[str, dict[str, Any]] = {}

        for relation_payload in relation_objects:
            source_entity_id = str(relation_payload.get("source_entity_id") or "").strip()
            target_entity_id = str(relation_payload.get("target_entity_id") or "").strip()
            if not source_entity_id or not target_entity_id:
                continue

            relation_type = str(relation_payload.get("relation_type") or "related_to").strip() or "related_to"
            relation_description = self._relation_description(relation_payload)
            relation_type_counts[relation_type] = relation_type_counts.get(relation_type, 0) + 1

            source_payload = node_payload_by_id.get(source_entity_id) or {}
            target_payload = node_payload_by_id.get(target_entity_id) or {}
            source_namespace = str(
                source_payload.get("namespace_id")
                or relation_payload.get("namespace_id")
                or ""
            ).strip() or "default"
            target_namespace = str(
                target_payload.get("namespace_id")
                or relation_payload.get("namespace_id")
                or ""
            ).strip() or "default"
            node_objects_by_id[source_entity_id] = {
                "node_id": source_entity_id,
                "label": self._entity_label(source_payload, source_entity_id),
                "kind": str(source_payload.get("entity_type") or source_payload.get("type_key") or "entity"),
                "namespace_id": source_namespace,
            }
            node_objects_by_id[target_entity_id] = {
                "node_id": target_entity_id,
                "label": self._entity_label(target_payload, target_entity_id),
                "kind": str(target_payload.get("entity_type") or target_payload.get("type_key") or "entity"),
                "namespace_id": target_namespace,
            }
            edge_objects.append(
                {
                    "source": source_entity_id,
                    "target": target_entity_id,
                    "label": relation_type,
                    "description": relation_description,
                }
            )

            if namespace_scope == "all" and cluster_by == "namespace":
                source_namespace_node_id = f"namespace:{source_namespace}"
                namespace_nodes.setdefault(
                    source_namespace_node_id,
                    {
                        "node_id": source_namespace_node_id,
                        "label": source_namespace,
                        "kind": "namespace",
                    },
                )
                edge_objects.append(
                    {
                        "source": source_namespace_node_id,
                        "target": source_entity_id,
                        "label": "in_namespace",
                        "description": "Clustered by namespace",
                    }
                )

                target_namespace_node_id = f"namespace:{target_namespace}"
                namespace_nodes.setdefault(
                    target_namespace_node_id,
                    {
                        "node_id": target_namespace_node_id,
                        "label": target_namespace,
                        "kind": "namespace",
                    },
                )
                edge_objects.append(
                    {
                        "source": target_namespace_node_id,
                        "target": target_entity_id,
                        "label": "in_namespace",
                        "description": "Clustered by namespace",
                    }
                )

        if namespace_nodes:
            node_objects_by_id.update(namespace_nodes)

        node_objects = sorted(node_objects_by_id.values(), key=lambda item: str(item.get("label") or "").lower())
        status_text = (
            f"{len(edge_objects)} relations | {len(node_objects)} nodes | "
            f"{str(getattr(runtime_config, 'database_name', 'n/a') or 'n/a')}"
        )
        return {
            "status_text": status_text,
            "message": "",
            "detail_html": self._build_detail_html(
                runtime_config,
                node_objects,
                edge_objects,
                relation_type_counts,
                snapshot_metadata,
                namespace_label=namespace_label,
            ),
            "nodes": node_objects,
            "edges": edge_objects,
            "metadata": snapshot_metadata,
            "source_uri": snapshot_metadata.get("source_uri", ""),
            "widget_uri": snapshot_metadata.get("widget_uri", ""),
            "widget_kind": snapshot_metadata.get("widget_kind", self._DEFAULT_WIDGET_KIND),
            "tool_id": snapshot_metadata.get("tool_id", self._DEFAULT_TOOL_ID),
            "view_kind": projection_config.get("view_kind", self._RELATIONS_VIEW_KIND),
        }

    def _load_projection_config(self, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        metadata_payload = metadata if isinstance(metadata, Mapping) else {}
        source_uri = str(metadata_payload.get("source_uri") or metadata_payload.get("widget_uri") or "").strip()
        parsed_uri = urlparse(source_uri)
        query_payload = parse_qs(str(parsed_uri.query or ""), keep_blank_values=False)

        def _q(name: str, default_value: str = "") -> str:
            values = query_payload.get(name) or []
            if not values:
                return str(default_value or "")
            return str(values[0] or default_value).strip()

        mode_value = _q("mode", _q("view", "relations")).strip().lower()
        if mode_value in {"adb", "catalog", "collections", "all", "model", "analysis"}:
            mode_value = "catalog"
        else:
            mode_value = "relations"

        namespace_id = _q("namespace", _q("namespace_id", "")).strip()
        namespace_scope = _q("scope", _q("namespace_scope", "")).strip().lower()
        cluster_by = _q("cluster", _q("cluster_by", "")).strip().lower()
        if namespace_id.lower() in {"*", "all", "any", "mixed"}:
            namespace_scope = "all"
            namespace_id = ""
        if namespace_scope in {"*", "all", "any", "mixed"}:
            namespace_scope = "all"
        include_embeddings = _q("embeddings", "1").strip().lower() not in {"0", "false", "no", "off"}
        include_documents = _q("documents", "1").strip().lower() not in {"0", "false", "no", "off"}
        include_blocks = _q("blocks", "1").strip().lower() not in {"0", "false", "no", "off"}
        include_seeds = _q("seeds", "1").strip().lower() not in {"0", "false", "no", "off"}
        shorthand_limit = _normalize_limit_value(_q("limit", ""))
        relation_limit = _normalize_limit_value(_q("relation_limit", _q("relations_limit", "")))
        entity_limit = _normalize_limit_value(_q("entity_limit", _q("entities_limit", "")))
        catalog_limit = _normalize_limit_value(_q("catalog_limit", _q("catalogs_limit", "")))
        if relation_limit is None:
            relation_limit = shorthand_limit
        if entity_limit is None:
            entity_limit = shorthand_limit
        if catalog_limit is None:
            catalog_limit = shorthand_limit

        return {
            "mode": mode_value,
            "view_kind": self._CATALOG_VIEW_KIND if mode_value == "catalog" else self._RELATIONS_VIEW_KIND,
            "namespace_id": namespace_id,
            "namespace_scope": namespace_scope or "single",
            "cluster_by": cluster_by,
            "include_embeddings": include_embeddings,
            "include_documents": include_documents,
            "include_blocks": include_blocks,
            "include_seeds": include_seeds,
            "relation_limit": relation_limit,
            "entity_limit": entity_limit,
            "catalog_limit": catalog_limit,
        }

    def _load_catalog_snapshot(
        self,
        *,
        repository: Any,
        runtime_config: Any,
        snapshot_metadata: Mapping[str, Any],
        projection_config: Mapping[str, Any],
        catalog_limit: int | None = None,
    ) -> dict[str, Any]:
        namespace_scope = str(projection_config.get("namespace_scope") or "single").strip().lower()
        cluster_by = str(projection_config.get("cluster_by") or "").strip().lower()
        if namespace_scope == "all" and not cluster_by:
            cluster_by = "namespace"
        namespace_id = str(
            projection_config.get("namespace_id")
            or getattr(runtime_config, "namespace_id", "")
            or ""
        ).strip()
        namespace_label = "all" if namespace_scope == "all" else (namespace_id or "n/a")
        resolved_catalog_limit = _normalize_limit_value(catalog_limit)
        if resolved_catalog_limit is None:
            resolved_catalog_limit = _normalize_limit_value(self._CATALOG_LIMIT)

        nodes_by_id: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        type_counts: dict[str, int] = {}

        def add_node(node_id: str, label: str, kind: str) -> None:
            normalized_node_id = str(node_id or "").strip()
            if not normalized_node_id:
                return
            if normalized_node_id in nodes_by_id:
                return
            nodes_by_id[normalized_node_id] = {
                "node_id": normalized_node_id,
                "label": str(label or normalized_node_id),
                "kind": str(kind or "object"),
            }
            type_counts[str(kind or "object")] = type_counts.get(str(kind or "object"), 0) + 1

        def add_edge(source_id: str, target_id: str, label: str, description: str = "") -> None:
            src = str(source_id or "").strip()
            tgt = str(target_id or "").strip()
            if not src or not tgt or src == tgt:
                return
            edges.append(
                {
                    "source": src,
                    "target": tgt,
                    "label": str(label or "related_to"),
                    "description": str(description or ""),
                }
            )

        namespace_node_id = f"namespace:{namespace_id or 'default'}"
        if namespace_scope == "all":
            add_node("namespace:all", "all namespaces", "namespace_root")
        else:
            add_node(namespace_node_id, namespace_id or "default namespace", "namespace")

        load_objects = getattr(repository, "load_objects", None)
        if not callable(load_objects):
            return {
                "status_text": "Repository does not support load_objects",
                "message": "Unable to enumerate ADB objects for catalog view.",
                "detail_html": "<h3>ADB Catalog Graph</h3><p>Repository does not support object enumeration.</p>",
                "nodes": [],
                "edges": [],
                "metadata": snapshot_metadata,
                "source_uri": snapshot_metadata.get("source_uri", ""),
                "widget_uri": snapshot_metadata.get("widget_uri", ""),
                "widget_kind": snapshot_metadata.get("widget_kind", self._DEFAULT_WIDGET_KIND),
                "tool_id": snapshot_metadata.get("tool_id", self._DEFAULT_TOOL_ID),
                "view_kind": self._CATALOG_VIEW_KIND,
            }

        collection_object_names: list[str] = ["entity", "relation"]
        if bool(projection_config.get("include_documents", True)):
            collection_object_names.append("document")
        if bool(projection_config.get("include_embeddings", True)):
            collection_object_names.append("embedding")

        owner_type_nodes: dict[str, str] = {}
        entity_node_ids: set[str] = set()

        for object_name in collection_object_names:
            collection_node_id = f"collection:{object_name}"
            add_node(collection_node_id, object_name, "collection")
            if namespace_scope != "all":
                add_edge(namespace_node_id, collection_node_id, "contains_collection")

            object_filter = {"namespace_id": namespace_id} if namespace_scope != "all" and namespace_id else None
            try:
                payload_rows = load_objects(object_name, object_filter, limit=resolved_catalog_limit)
            except TypeError:
                payload_rows = load_objects(object_name, object_filter)
            except Exception:
                payload_rows = []

            for payload in payload_rows or []:
                if not isinstance(payload, Mapping):
                    continue
                payload_id = str(payload.get("_id") or payload.get("id") or "").strip()
                if not payload_id:
                    continue
                payload_namespace = str(payload.get("namespace_id") or "default").strip() or "default"
                if namespace_scope == "all" and cluster_by == "namespace":
                    namespace_cluster_id = f"namespace:{payload_namespace}"
                    add_node(namespace_cluster_id, payload_namespace, "namespace")
                    add_edge("namespace:all", namespace_cluster_id, "contains_namespace")

                if object_name == "entity":
                    node_id = f"entity:{payload_id}"
                    label = str(payload.get("canonical_name") or payload.get("title") or payload_id)
                    entity_type = str(payload.get("entity_type") or "entity").strip() or "entity"
                    add_node(node_id, label, entity_type)
                    entity_node_ids.add(node_id)
                    add_edge(collection_node_id, node_id, "contains")
                    if namespace_scope == "all" and cluster_by == "namespace":
                        add_edge(f"namespace:{payload_namespace}", node_id, "in_namespace")

                    type_node_id = f"type:entity:{entity_type}"
                    add_node(type_node_id, entity_type, "entity_type")
                    add_edge(type_node_id, node_id, "typed_as")

                    if bool(projection_config.get("include_seeds", True)):
                        for key_name in ("seed_key", "source_seed_key"):
                            seed_value = str(payload.get(key_name) or "").strip()
                            if seed_value:
                                seed_node_id = f"seed:{seed_value}"
                                add_node(seed_node_id, seed_value, "seed")
                                add_edge(seed_node_id, node_id, "maps_to")

                elif object_name == "relation":
                    relation_node_id = f"relation:{payload_id}"
                    relation_type = str(payload.get("relation_type") or "related_to").strip() or "related_to"
                    add_node(relation_node_id, relation_type, "relation")
                    add_edge(collection_node_id, relation_node_id, "contains")
                    if namespace_scope == "all" and cluster_by == "namespace":
                        add_edge(f"namespace:{payload_namespace}", relation_node_id, "in_namespace")

                    type_node_id = f"type:relation:{relation_type}"
                    add_node(type_node_id, relation_type, "relation_type")
                    add_edge(type_node_id, relation_node_id, "typed_as")

                    source_entity_id = str(payload.get("source_entity_id") or "").strip()
                    target_entity_id = str(payload.get("target_entity_id") or "").strip()
                    if source_entity_id:
                        source_node_id = f"entity:{source_entity_id}"
                        if source_node_id not in nodes_by_id:
                            add_node(source_node_id, self._short_entity_label(source_entity_id), "entity")
                        add_edge(relation_node_id, source_node_id, "source")
                    if target_entity_id:
                        target_node_id = f"entity:{target_entity_id}"
                        if target_node_id not in nodes_by_id:
                            add_node(target_node_id, self._short_entity_label(target_entity_id), "entity")
                        add_edge(relation_node_id, target_node_id, "target")

                    if bool(projection_config.get("include_seeds", True)):
                        relation_seed = str(payload.get("source_seed_key") or payload.get("seed_key") or "").strip()
                        if relation_seed:
                            seed_node_id = f"seed:{relation_seed}"
                            add_node(seed_node_id, relation_seed, "seed")
                            add_edge(seed_node_id, relation_node_id, "describes")

                elif object_name == "document":
                    document_node_id = f"document:{payload_id}"
                    title = str(payload.get("title") or payload.get("source_uri") or payload_id)
                    add_node(document_node_id, title, "document")
                    add_edge(collection_node_id, document_node_id, "contains")
                    if namespace_scope == "all" and cluster_by == "namespace":
                        add_edge(f"namespace:{payload_namespace}", document_node_id, "in_namespace")

                    document_type = str(payload.get("document_type") or "document").strip() or "document"
                    doc_type_node_id = f"type:document:{document_type}"
                    add_node(doc_type_node_id, document_type, "document_type")
                    add_edge(doc_type_node_id, document_node_id, "typed_as")

                    if bool(projection_config.get("include_blocks", True)):
                        block_rows = payload.get("blocks") if isinstance(payload.get("blocks"), Sequence) else []
                        for block_payload in block_rows:
                            if not isinstance(block_payload, Mapping):
                                continue
                            block_id = str(
                                block_payload.get("block_id")
                                or block_payload.get("_id")
                                or block_payload.get("id")
                                or ""
                            ).strip()
                            if not block_id:
                                continue
                            block_node_id = f"block:{block_id}"
                            block_label = str(block_payload.get("heading") or block_payload.get("block_kind") or block_id)
                            add_node(block_node_id, block_label, "block")
                            add_edge(document_node_id, block_node_id, "contains_block")

                elif object_name == "embedding":
                    owner_type = str(payload.get("owner_type") or "owner").strip() or "owner"
                    owner_id = str(payload.get("owner_id") or "").strip()
                    embedding_node_id = f"embedding:{payload_id}"
                    model_id = str(payload.get("model_id") or "embedding")
                    add_node(embedding_node_id, model_id, "embedding")
                    add_edge(collection_node_id, embedding_node_id, "contains")
                    if namespace_scope == "all" and cluster_by == "namespace":
                        add_edge(f"namespace:{payload_namespace}", embedding_node_id, "in_namespace")

                    if owner_type:
                        owner_type_node_id = owner_type_nodes.get(owner_type)
                        if not owner_type_node_id:
                            owner_type_node_id = f"owner_type:{owner_type}"
                            owner_type_nodes[owner_type] = owner_type_node_id
                            add_node(owner_type_node_id, owner_type, "owner_type")
                            if namespace_scope == "all" and cluster_by == "namespace":
                                add_edge(f"namespace:{payload_namespace}", owner_type_node_id, "supports_owner")
                            else:
                                add_edge(namespace_node_id, owner_type_node_id, "supports_owner")
                        add_edge(owner_type_node_id, embedding_node_id, "owns_embedding")

                    if owner_id:
                        owner_node_id = f"{owner_type}:{owner_id}"
                        if owner_node_id not in nodes_by_id:
                            owner_label = self._short_entity_label(owner_id)
                            add_node(owner_node_id, owner_label, owner_type or "owner")
                        add_edge(embedding_node_id, owner_node_id, "embeds")

                    key_name = str(payload.get("index_item_key") or "").strip()
                    if key_name:
                        key_node_id = f"key:{key_name}"
                        add_node(key_node_id, key_name, "key")
                        add_edge(key_node_id, embedding_node_id, "indexes")

        node_objects = sorted(nodes_by_id.values(), key=lambda item: str(item.get("label") or "").lower())
        detail_html = self._build_catalog_detail_html(
            runtime_config=runtime_config,
            node_objects=node_objects,
            edge_objects=edges,
            type_counts=type_counts,
            snapshot_metadata=snapshot_metadata,
            namespace_label=namespace_label,
        )
        return {
            "status_text": f"ADB Catalog | {len(node_objects)} nodes | {len(edges)} edges",
            "message": "",
            "detail_html": detail_html,
            "nodes": node_objects,
            "edges": edges,
            "metadata": snapshot_metadata,
            "source_uri": snapshot_metadata.get("source_uri", ""),
            "widget_uri": snapshot_metadata.get("widget_uri", ""),
            "widget_kind": snapshot_metadata.get("widget_kind", self._DEFAULT_WIDGET_KIND),
            "tool_id": snapshot_metadata.get("tool_id", self._DEFAULT_TOOL_ID),
            "view_kind": self._CATALOG_VIEW_KIND,
        }

    def _build_catalog_detail_html(
        self,
        *,
        runtime_config: Any,
        node_objects: Sequence[Mapping[str, Any]],
        edge_objects: Sequence[Mapping[str, Any]],
        type_counts: Mapping[str, int],
        snapshot_metadata: Mapping[str, Any] | None = None,
        namespace_label: str | None = None,
    ) -> str:
        metadata = snapshot_metadata if isinstance(snapshot_metadata, Mapping) else {}
        resolved_namespace_label = str(namespace_label or getattr(runtime_config, "namespace_id", "n/a") or "n/a")
        type_rows = "".join(
            f"<li><b>{html.escape(str(type_name))}</b>: {int(type_count)}</li>"
            for type_name, type_count in sorted(type_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))[:16]
        )
        if not type_rows:
            type_rows = "<li>No object types loaded.</li>"

        sample_edges = "".join(
            (
                f"<li><b>{html.escape(str(edge_object.get('label') or 'related_to'))}</b>: "
                f"{html.escape(str(edge_object.get('source') or 'n/a'))} -> "
                f"{html.escape(str(edge_object.get('target') or 'n/a'))}</li>"
            )
            for edge_object in list(edge_objects)[:12]
        )
        if not sample_edges:
            sample_edges = "<li>No graph edges rendered.</li>"

        return "".join(
            [
                "<h3>ADB Catalog Graph</h3>",
                "<p>Unified graph projection for collection analysis, modeling and exploration.</p>",
                "<ul>",
                f"<li><b>Database:</b> {html.escape(str(getattr(runtime_config, 'database_name', 'n/a') or 'n/a'))}</li>",
                f"<li><b>Namespace:</b> {html.escape(resolved_namespace_label)}</li>",
                f"<li><b>Backend:</b> {html.escape(str(getattr(runtime_config, 'agents_db_uri', 'n/a') or 'n/a'))}</li>",
                f"<li><b>Widget URI:</b> {html.escape(str(metadata.get('widget_uri') or metadata.get('source_uri') or 'n/a'))}</li>",
                f"<li><b>Tool:</b> {html.escape(str(metadata.get('tool_id') or self._DEFAULT_TOOL_ID))}</li>",
                f"<li><b>Nodes:</b> {len(list(node_objects))}</li>",
                f"<li><b>Edges:</b> {len(list(edge_objects))}</li>",
                "</ul>",
                "<h4>Object Types</h4>",
                f"<ul>{type_rows}</ul>",
                "<h4>Sample Links</h4>",
                f"<ul>{sample_edges}</ul>",
                "<p>Tip: use source URI query <code>?mode=catalog</code> to open this analysis projection explicitly.</p>",
            ]
        )

    def _load_graphic_tool_placeholder_snapshot(
        self,
        *,
        tool_id: str,
        source_uri: str,
        view_kind: str,
        status_text: str,
        message: str,
        detail_html: str,
    ) -> dict[str, Any]:
        resolved_tool_id = str(tool_id or self._DEFAULT_TOOL_ID).strip() or self._DEFAULT_TOOL_ID
        resolved_source_uri = str(source_uri or "").strip()
        metadata = {
            "source_uri": resolved_source_uri,
            "widget_uri": resolved_source_uri,
            "widget_kind": resolved_tool_id,
            "tool_id": resolved_tool_id,
            "object_name": resolved_tool_id,
        }
        return {
            "status_text": str(status_text or "Extensions ready"),
            "message": str(message or ""),
            "detail_html": str(detail_html or "<p>No graph detail available.</p>"),
            "nodes": [],
            "edges": [],
            "metadata": metadata,
            "source_uri": resolved_source_uri,
            "widget_uri": resolved_source_uri,
            "widget_kind": resolved_tool_id,
            "tool_id": resolved_tool_id,
            "view_kind": str(view_kind or self._RELATIONS_VIEW_KIND),
        }

    def _prepare_interactive_edge_objects(
        self,
        edge_objects: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        edge_by_id: dict[str, dict[str, Any]] = {}
        edges_by_node_id: dict[str, list[dict[str, Any]]] = {}
        rendered_edge_objects: list[dict[str, Any]] = []
        for edge_index, edge_object in enumerate(edge_objects):
            source_node_id = str(edge_object.get("source") or "").strip()
            target_node_id = str(edge_object.get("target") or "").strip()
            if not source_node_id or not target_node_id:
                continue
            label = str(edge_object.get("label") or "related_to").strip() or "related_to"
            edge_id = str(edge_object.get("edge_id") or "").strip()
            if not edge_id:
                edge_id = f"edge:{edge_index}:{source_node_id}->{target_node_id}:{label}"
            interactive_edge_object = dict(edge_object)
            interactive_edge_object["edge_id"] = edge_id
            edge_by_id[edge_id] = interactive_edge_object
            edges_by_node_id.setdefault(source_node_id, []).append(interactive_edge_object)
            edges_by_node_id.setdefault(target_node_id, []).append(interactive_edge_object)
            rendered_edge_objects.append(interactive_edge_object)
        return rendered_edge_objects, edge_by_id, edges_by_node_id

    def _load_connected_graph_component(
        self,
        edges_by_node_id: Mapping[str, Sequence[Mapping[str, Any]]],
        start_node_id: str,
    ) -> tuple[set[str], set[str]]:
        normalized_start_node_id = str(start_node_id or "").strip()
        if not normalized_start_node_id:
            return set(), set()

        visited_node_ids: set[str] = set()
        visited_edge_ids: set[str] = set()
        queue: list[str] = [normalized_start_node_id]
        while queue:
            current_node_id = queue.pop(0)
            if current_node_id in visited_node_ids:
                continue
            visited_node_ids.add(current_node_id)
            for edge_object in edges_by_node_id.get(current_node_id, []):
                edge_id = str(edge_object.get("edge_id") or "")
                if edge_id:
                    visited_edge_ids.add(edge_id)
                for neighbor_node_id in (
                    str(edge_object.get("source") or ""),
                    str(edge_object.get("target") or ""),
                ):
                    if neighbor_node_id and neighbor_node_id not in visited_node_ids and neighbor_node_id not in queue:
                        queue.append(neighbor_node_id)
        return visited_node_ids, visited_edge_ids

    def _graph_detail_link(self, kind: str, object_id: str, label: str | None = None) -> str:
        safe_label = html.escape(str(label or object_id or "open"))
        return f'<a href="{self._GRAPH_LINK_SCHEME}://{self._GRAPH_LINK_HOST}/{quote(str(kind or ""), safe="")}/{quote(str(object_id or ""), safe="")}">{safe_label}</a>'

    def _build_node_detail_html(
        self,
        node_id: str,
        node_by_id: Mapping[str, Mapping[str, Any]],
        edge_by_id: Mapping[str, Mapping[str, Any]],
        edges_by_node_id: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> str:
        normalized_node_id = str(node_id or "").strip()
        node_object = node_by_id.get(normalized_node_id) or {}
        connected_edge_objects = [
            edge_object
            for edge_object in edges_by_node_id.get(normalized_node_id, [])
            if str(edge_object.get("edge_id") or "") in edge_by_id
        ]
        edge_rows: list[str] = []
        for edge_object in connected_edge_objects:
            edge_id = str(edge_object.get("edge_id") or "")
            source_node_id = str(edge_object.get("source") or "")
            target_node_id = str(edge_object.get("target") or "")
            edge_rows.append(
                "".join(
                    [
                        "<li>",
                        self._graph_detail_link("edge", edge_id, str(edge_object.get("label") or "related_to")),
                        ": ",
                        self._graph_detail_link("node", source_node_id, self._graph_node_label(source_node_id, node_by_id)),
                        " → ",
                        self._graph_detail_link("node", target_node_id, self._graph_node_label(target_node_id, node_by_id)),
                        "</li>",
                    ]
                )
            )
        edge_html = "".join(edge_rows) if edge_rows else "<li>Keine verbundenen Relationen.</li>"
        return "".join(
            [
                "<h3>Knoten</h3>",
                f"<p><b>{html.escape(str(node_object.get('label') or normalized_node_id))}</b></p>",
                "<ul>",
                f"<li><b>ID:</b> <code>{html.escape(normalized_node_id)}</code></li>",
                f"<li><b>Typ:</b> {html.escape(str(node_object.get('kind') or 'entity'))}</li>",
                f"<li><b>Relationen:</b> {len(connected_edge_objects)}</li>",
                "</ul>",
                "<h4>Verbundene Relationen</h4>",
                f"<ul>{edge_html}</ul>",
                f"<p>{self._graph_detail_link('overview', 'overview', 'Zurück zur Übersicht')}</p>",
            ]
        )

    def _build_edge_detail_html(
        self,
        edge_id: str,
        node_by_id: Mapping[str, Mapping[str, Any]],
        edge_by_id: Mapping[str, Mapping[str, Any]],
    ) -> str:
        normalized_edge_id = str(edge_id or "").strip()
        edge_object = edge_by_id.get(normalized_edge_id) or {}
        source_node_id = str(edge_object.get("source") or "")
        target_node_id = str(edge_object.get("target") or "")
        description = str(edge_object.get("description") or "").strip()
        description_html = f"<p>{html.escape(description)}</p>" if description else "<p><i>Keine Relationsbeschreibung vorhanden.</i></p>"
        return "".join(
            [
                "<h3>Relation</h3>",
                f"<p><b>{html.escape(str(edge_object.get('label') or 'related_to'))}</b></p>",
                "<ul>",
                f"<li><b>Quelle:</b> {self._graph_detail_link('node', source_node_id, self._graph_node_label(source_node_id, node_by_id))}</li>",
                f"<li><b>Ziel:</b> {self._graph_detail_link('node', target_node_id, self._graph_node_label(target_node_id, node_by_id))}</li>",
                f"<li><b>ID:</b> <code>{html.escape(normalized_edge_id)}</code></li>",
                "</ul>",
                "<h4>Beschreibung</h4>",
                description_html,
                f"<p>{self._graph_detail_link('overview', 'overview', 'Zurück zur Übersicht')}</p>",
            ]
        )

    def _graph_node_label(self, node_id: str, node_by_id: Mapping[str, Mapping[str, Any]]) -> str:
        node_object = node_by_id.get(str(node_id or "")) or {}
        return str(node_object.get("label") or node_id or "unknown")

    def _wrap_graph_label(self, label_text: str, max_line_length: int = 18) -> str:
        normalized_label = str(label_text or "").strip()
        if not normalized_label:
            return "unknown"
        words = normalized_label.replace("_", " ").split()
        if not words:
            return self._short_graph_label(normalized_label, max_length=max_line_length)

        line_objects: list[str] = []
        current_line = ""
        for word in words:
            candidate_line = f"{current_line} {word}".strip()
            if current_line and len(candidate_line) > max_line_length:
                line_objects.append(current_line)
                current_line = word
                continue
            current_line = candidate_line
        if current_line:
            line_objects.append(current_line)
        if len(line_objects) > 3:
            line_objects = line_objects[:3]
            line_objects[-1] = self._short_graph_label(line_objects[-1], max_length=max_line_length - 1)
        return "\n".join(line_objects)

    def _short_graph_label(self, label_text: str, max_length: int = 24) -> str:
        normalized_label = str(label_text or "").strip()
        if len(normalized_label) <= max_length:
            return normalized_label
        if max_length <= 1:
            return normalized_label[:max_length]
        return f"{normalized_label[: max_length - 1]}…"

    def _hsv_to_hex_color(self, hue_value: float, saturation_value: float, value_value: float) -> str:
        clamped_hue = float(hue_value) % 1.0
        clamped_saturation = max(0.0, min(1.0, float(saturation_value)))
        clamped_value = max(0.0, min(1.0, float(value_value)))
        red, green, blue = colorsys.hsv_to_rgb(clamped_hue, clamped_saturation, clamped_value)
        return f"#{int(round(red * 255.0)):02x}{int(round(green * 255.0)):02x}{int(round(blue * 255.0)):02x}"

    def _relation_palette_for_label(
        self,
        relation_label: str,
        *,
        is_selected: bool,
        is_highlighted: bool,
    ) -> dict[str, str]:
        normalized_relation_label = str(relation_label or "related_to").strip().lower() or "related_to"
        relation_digest = hashlib.sha1(normalized_relation_label.encode("utf-8", errors="ignore")).digest()
        hue_value = ((int(relation_digest[0]) << 8) | int(relation_digest[1])) / 65535.0

        if is_selected:
            stroke_color = self._hsv_to_hex_color(hue_value, 0.92, 0.98)
            text_color = self._hsv_to_hex_color((hue_value + 0.012) % 1.0, 0.35, 1.0)
            outline_color = self._hsv_to_hex_color(hue_value, 0.24, 1.0)
            return {
                "stroke_color": stroke_color,
                "text_color": text_color,
                "outline_color": outline_color,
            }

        if is_highlighted:
            stroke_color = self._hsv_to_hex_color(hue_value, 0.78, 0.90)
            text_color = self._hsv_to_hex_color((hue_value + 0.008) % 1.0, 0.46, 0.96)
            outline_color = self._hsv_to_hex_color(hue_value, 0.22, 0.92)
            return {
                "stroke_color": stroke_color,
                "text_color": text_color,
                "outline_color": outline_color,
            }

        stroke_color = self._hsv_to_hex_color(hue_value, 0.56, 0.74)
        text_color = self._hsv_to_hex_color((hue_value + 0.006) % 1.0, 0.28, 0.86)
        outline_color = self._hsv_to_hex_color(hue_value, 0.16, 0.82)
        return {
            "stroke_color": stroke_color,
            "text_color": text_color,
            "outline_color": outline_color,
        }

    def _build_graph_render_commands(
        self,
        node_draw_objects: Sequence[Mapping[str, Any]],
        edge_draw_objects: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        render_commands: list[dict[str, Any]] = []

        for node_object in node_draw_objects:
            node_id = str(node_object.get("node_id") or "").strip()
            x_pos = float(node_object.get("x") or 0.0)
            y_pos = float(node_object.get("y") or 0.0)
            node_width = float(node_object.get("width") or 168.0)
            node_height = float(node_object.get("height") or 72.0)
            node_payload = {"kind": "node", "node_id": node_id}
            node_tooltip = str(node_object.get("tooltip") or f"Knoten öffnen: {node_id}")
            is_highlighted = bool(node_object.get("is_highlighted", True))

            render_commands.append(
                {
                    "type": "ellipse",
                    "x": x_pos,
                    "y": y_pos,
                    "width": node_width,
                    "height": node_height,
                    "payload": node_payload,
                    "tooltip": node_tooltip,
                    "style": {
                        "stroke_role": "accent" if is_highlighted else "border",
                        "fill_role": "surface_strong" if is_highlighted else "surface",
                        "line_width": 3.0 if is_highlighted else 1.25,
                    },
                }
            )
            render_commands.append(
                {
                    "type": "text",
                    "text": str(node_object.get("label") or node_id),
                    "x": x_pos + 9.0,
                    "y": y_pos + 8.0,
                    "max_width": max(24.0, node_width - 18.0),
                    "anchor": "top_left",
                    "payload": node_payload,
                    "tooltip": node_tooltip,
                    "style": {
                        "text_role": "text_primary" if is_highlighted else "text_secondary",
                    },
                }
            )

        for edge_object in edge_draw_objects:
            edge_id = str(edge_object.get("edge_id") or "").strip()
            start_x = float(edge_object.get("start_x") or 0.0)
            start_y = float(edge_object.get("start_y") or 0.0)
            end_x = float(edge_object.get("end_x") or 0.0)
            end_y = float(edge_object.get("end_y") or 0.0)
            is_highlighted = bool(edge_object.get("is_highlighted", True))
            is_selected = bool(edge_object.get("is_selected", False))
            edge_payload = {"kind": "edge", "edge_id": edge_id}
            edge_tooltip = str(edge_object.get("tooltip") or f"Relation öffnen: {edge_id}")
            relation_type = str(edge_object.get("relation_type") or edge_object.get("label") or "related_to")
            relation_palette = self._relation_palette_for_label(
                relation_type,
                is_selected=is_selected,
                is_highlighted=is_highlighted,
            )
            line_width = 6.2 if is_selected else (4.0 if is_highlighted else 1.7)

            if is_selected:
                render_commands.append(
                    {
                        "type": "line",
                        "start_x": start_x,
                        "start_y": start_y,
                        "end_x": end_x,
                        "end_y": end_y,
                        "payload": edge_payload,
                        "tooltip": edge_tooltip,
                        "style": {
                            "stroke_role": "accent",
                            "stroke_color": relation_palette["outline_color"],
                            "line_width": line_width + 2.6,
                        },
                    }
                )

            render_commands.append(
                {
                    "type": "line",
                    "start_x": start_x,
                    "start_y": start_y,
                    "end_x": end_x,
                    "end_y": end_y,
                    "payload": edge_payload,
                    "tooltip": edge_tooltip,
                    "style": {
                        "stroke_role": "accent" if is_highlighted else "link",
                        "stroke_color": relation_palette["stroke_color"],
                        "line_width": line_width,
                    },
                }
            )

            render_commands.append(
                {
                    "type": "text",
                    "text": (
                        f"selected: {str(edge_object.get('label') or 'related_to')}"
                        if is_selected
                        else str(edge_object.get("label") or "related_to")
                    ),
                    "x": (start_x + end_x) / 2.0,
                    "y": (start_y + end_y) / 2.0,
                    "anchor": "center_above",
                    "payload": edge_payload,
                    "tooltip": edge_tooltip,
                    "style": {
                        "text_role": "accent" if is_highlighted else "text_secondary",
                        "text_color": relation_palette["text_color"],
                    },
                }
            )

        return render_commands

    def _build_vector_graph_positions(
        self,
        node_objects: Sequence[Mapping[str, Any]],
        edge_objects: Sequence[Mapping[str, Any]],
    ) -> dict[str, tuple[float, float]]:
        node_id_list = [
            str(node_object.get("node_id") or "").strip()
            for node_object in sorted(node_objects, key=lambda item: str(item.get("label") or item.get("node_id") or "").lower())
            if str(node_object.get("node_id") or "").strip()
        ]
        if not node_id_list:
            return {}

        node_count = len(node_id_list)
        if node_count == 1:
            return {node_id_list[0]: (0.0, 0.0)}

        relation_pairs = [
            (str(edge_object.get("source") or "").strip(), str(edge_object.get("target") or "").strip())
            for edge_object in edge_objects
        ]
        relation_pairs = [
            (source_node_id, target_node_id)
            for source_node_id, target_node_id in relation_pairs
            if source_node_id in node_id_list and target_node_id in node_id_list and source_node_id != target_node_id
        ]

        radius = max(190.0, 72.0 * math.sqrt(float(node_count)) + 120.0)
        golden_angle = math.pi * (3.0 - math.sqrt(5.0))
        positions: dict[str, list[float]] = {}
        for index, node_id in enumerate(node_id_list):
            ring_radius = radius * math.sqrt((index + 0.5) / node_count)
            angle = index * golden_angle
            positions[node_id] = [math.cos(angle) * ring_radius, math.sin(angle) * ring_radius]

        # Large graphs can make the full O(n^2) force layout too slow for UI workflows.
        # Fall back to the deterministic golden-angle seed layout to keep rendering responsive.
        if node_count > 320:
            return {node_id: (position[0], position[1]) for node_id, position in positions.items()}

        ideal_distance = max(190.0, min(360.0, 95.0 + (node_count * 8.0)))
        canvas_radius = max(360.0, radius * 2.35)
        node_id_pairs = [(node_id_list[i], node_id_list[j]) for i in range(node_count) for j in range(i + 1, node_count)]
        for iteration_index in range(90):
            temperature = max(0.08, 1.0 - (iteration_index / 90.0))
            forces: dict[str, list[float]] = {node_id: [0.0, 0.0] for node_id in node_id_list}

            for source_node_id, target_node_id in node_id_pairs:
                source_x, source_y = positions[source_node_id]
                target_x, target_y = positions[target_node_id]
                delta_x = source_x - target_x
                delta_y = source_y - target_y
                distance = max(18.0, math.hypot(delta_x, delta_y))
                repulsion = (ideal_distance * ideal_distance) / distance
                force_x = (delta_x / distance) * repulsion
                force_y = (delta_y / distance) * repulsion
                forces[source_node_id][0] += force_x
                forces[source_node_id][1] += force_y
                forces[target_node_id][0] -= force_x
                forces[target_node_id][1] -= force_y

            for source_node_id, target_node_id in relation_pairs:
                source_x, source_y = positions[source_node_id]
                target_x, target_y = positions[target_node_id]
                delta_x = target_x - source_x
                delta_y = target_y - source_y
                distance = max(18.0, math.hypot(delta_x, delta_y))
                attraction = ((distance - ideal_distance) * 0.075)
                force_x = (delta_x / distance) * attraction
                force_y = (delta_y / distance) * attraction
                forces[source_node_id][0] += force_x
                forces[source_node_id][1] += force_y
                forces[target_node_id][0] -= force_x
                forces[target_node_id][1] -= force_y

            for node_id in node_id_list:
                current_x, current_y = positions[node_id]
                forces[node_id][0] += -current_x * 0.012
                forces[node_id][1] += -current_y * 0.012
                force_x, force_y = forces[node_id]
                force_distance = max(1.0, math.hypot(force_x, force_y))
                step = min(42.0 * temperature, force_distance)
                next_x = current_x + (force_x / force_distance) * step
                next_y = current_y + (force_y / force_distance) * step
                next_distance_from_origin = math.hypot(next_x, next_y)
                if next_distance_from_origin > canvas_radius:
                    scale = canvas_radius / next_distance_from_origin
                    next_x *= scale
                    next_y *= scale
                positions[node_id] = [next_x, next_y]

        return {node_id: (position[0], position[1]) for node_id, position in positions.items()}

    def _load_graph_request_context(
        self,
        *,
        runtime_config: Any | None,
        object_name: str | None = None,
        source_uri: str | None = None,
    ) -> dict[str, Any]:
        requested_object_name = str(object_name or "").strip()
        requested_source_uri = str(source_uri or "").strip()
        if not requested_source_uri and requested_object_name.lower().startswith("agentsdb://"):
            requested_source_uri = requested_object_name
            requested_object_name = ""

        repository_uri = ""
        widget_uri = requested_source_uri
        if requested_source_uri:
            repository_uri, widget_uri = self._normalize_widget_source_uri(requested_source_uri)

        resolved_runtime_config = runtime_config
        if runtime_config is None and repository_uri:
            resolved_runtime_config = RuntimeConfigObject(agents_db_uri=repository_uri)
        elif runtime_config is not None and repository_uri:
            try:
                resolved_runtime_config = RuntimeConfigObject(
                    agents_db_uri=repository_uri,
                    database_name=str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge"),
                    tenant_id=str(getattr(runtime_config, "tenant_id", "tenant_default") or "tenant_default"),
                    namespace_id=str(getattr(runtime_config, "namespace_id", "ns_alde_default") or "ns_alde_default"),
                    namespace_slug=str(getattr(runtime_config, "namespace_slug", "alde-default") or "alde-default"),
                    namespace_name=str(getattr(runtime_config, "namespace_name", "ALDE Default Knowledge") or "ALDE Default Knowledge"),
                    default_embedding_model=str(getattr(runtime_config, "default_embedding_model", "text-embedding-3-large") or "text-embedding-3-large"),
                    default_embedding_dimension=int(getattr(runtime_config, "default_embedding_dimension", 3072) or 3072),
                    index_backend=str(getattr(runtime_config, "index_backend", "faiss") or "faiss"),
                )
            except Exception:
                setattr(runtime_config, "agents_db_uri", repository_uri)
                resolved_runtime_config = runtime_config

        metadata = {
            "source_uri": requested_source_uri,
            "widget_uri": widget_uri,
            "repository_uri": repository_uri,
            "widget_kind": self._DEFAULT_WIDGET_KIND,
            "tool_id": requested_object_name or self._DEFAULT_TOOL_ID,
            "object_name": requested_object_name or self._DEFAULT_WIDGET_KIND,
        }
        return {"runtime_config": resolved_runtime_config, "metadata": metadata}

    def _normalize_widget_source_uri(self, source_uri: str) -> tuple[str, str]:
        normalized_source_uri = str(source_uri or "").strip()
        parsed_uri = urlparse(normalized_source_uri)
        if str(parsed_uri.scheme or "").strip().lower() != "agentsdb":
            return normalized_source_uri, normalized_source_uri

        repository_uri = normalize_agentsdb_socket_uri(normalized_source_uri, default_on_empty=False) or normalized_source_uri
        widget_path = str(parsed_uri.path or "").strip("/")
        normalized_widget_path = "adbGraphView" if widget_path.lower() in self._WIDGET_PATH_ALIASES else widget_path
        widget_uri = f"{repository_uri}/{normalized_widget_path}" if normalized_widget_path else repository_uri
        return repository_uri, widget_uri

    def _load_runtime_config(self) -> Any | None:
        _, _, load_runtime_config = self._load_agentsdb_dependencies()
        return load_runtime_config()

    def _load_repository(self, runtime_config: Any) -> Any:
        repository_factory_class, repository_factory_config_class, _ = self._load_agentsdb_dependencies()

        project_root = Path(__file__).resolve().parents[2]
        memory_image_path = project_root / "AppData" / "agentsdb.json"
        backend_uri = str(getattr(runtime_config, "agents_db_uri", "") or "").strip()
        database_name = str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge"
        if _is_agentsdb_socket_uri(backend_uri):
            return UiAgentDbSocketRepository.create_from_uri(
                backend_uri,
                database_name,
            )
        repository_factory = repository_factory_class(
            repository_factory_config_class(
                backend_uri=backend_uri,
                default_database_name=database_name,
                memory_image_path=str(memory_image_path),
                prefer_explicit_inmemory=True,
            )
        )
        return repository_factory.load_repository(database_name)

    def _load_memory_fallback_repository(self, runtime_config: Any) -> Any | None:
        repository_factory_class, repository_factory_config_class, _ = self._load_agentsdb_dependencies()
        project_root = Path(__file__).resolve().parents[2]
        memory_image_path = project_root / "AppData" / "agentsdb.json"
        if not memory_image_path.exists():
            return None
        try:
            repository_factory = repository_factory_class(
                repository_factory_config_class(
                    backend_uri="agentsmem://local",
                    default_database_name=str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge").strip() or "alde_knowledge",
                    memory_image_path=str(memory_image_path),
                    prefer_explicit_inmemory=True,
                )
            )
            return repository_factory.load_repository(
                str(getattr(runtime_config, "database_name", "alde_knowledge") or "alde_knowledge")
            )
        except Exception:
            return None

    def _load_relation_objects(
        self,
        repository: Any,
        namespace_id: str,
        *,
        relation_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        load_objects = getattr(repository, "load_objects", None)
        if not callable(load_objects):
            return []

        resolved_relation_limit = _normalize_limit_value(relation_limit)
        if resolved_relation_limit is None:
            resolved_relation_limit = _normalize_limit_value(self._RELATION_LIMIT)

        relation_filter = {"namespace_id": namespace_id} if namespace_id else None
        relation_objects: Any = []
        for object_name in ("relation", "entity_relations", "relations"):
            try:
                relation_objects = load_objects(object_name, relation_filter, limit=resolved_relation_limit)
            except TypeError:
                try:
                    relation_objects = load_objects(object_name, relation_filter)
                except Exception:
                    relation_objects = []
            except Exception:
                relation_objects = []
            if relation_objects:
                break

        if not relation_objects:
            try:
                relation_objects = load_objects("relation", None, limit=resolved_relation_limit)
            except TypeError:
                try:
                    relation_objects = load_objects("relation", None)
                except Exception:
                    relation_objects = []
            except Exception:
                relation_objects = []
        if not relation_objects:
            for object_name in ("entity_relations", "relations"):
                try:
                    relation_objects = load_objects(object_name, None, limit=resolved_relation_limit)
                except TypeError:
                    try:
                        relation_objects = load_objects(object_name, None)
                    except Exception:
                        relation_objects = []
                except Exception:
                    relation_objects = []
                if relation_objects:
                    break

        return [dict(item) for item in (relation_objects or []) if isinstance(item, dict)]

    def _load_node_payload_by_id(
        self,
        repository: Any,
        relation_objects: list[dict[str, Any]],
        *,
        entity_limit: int | None = None,
        relation_limit: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        load_object = getattr(repository, "load_object", None)
        if not callable(load_object):
            return {}

        entity_id_list: list[str] = []
        seen_entity_ids: set[str] = set()
        for relation_payload in relation_objects:
            for entity_id in (
                str(relation_payload.get("source_entity_id") or "").strip(),
                str(relation_payload.get("target_entity_id") or "").strip(),
            ):
                if entity_id and entity_id not in seen_entity_ids:
                    seen_entity_ids.add(entity_id)
                    entity_id_list.append(entity_id)

        node_payload_by_id: dict[str, dict[str, Any]] = {}
        resolved_entity_limit = _normalize_limit_value(entity_limit)
        resolved_relation_limit = _normalize_limit_value(relation_limit)
        if resolved_relation_limit is None:
            resolved_relation_limit = _normalize_limit_value(self._RELATION_LIMIT)

        max_entities = len(entity_id_list)
        if resolved_entity_limit is not None:
            max_entities = min(max_entities, resolved_entity_limit)
        elif resolved_relation_limit is not None:
            max_entities = min(max_entities, resolved_relation_limit * 2)

        for entity_id in entity_id_list[:max_entities]:
            entity_payload = None
            for object_name in ("entity", "entities"):
                try:
                    entity_payload = load_object(object_name, entity_id)
                except Exception:
                    entity_payload = None
                if entity_payload is not None:
                    break
            node_payload_by_id[entity_id] = dict(entity_payload) if isinstance(entity_payload, dict) else {}
        return node_payload_by_id

    def _relation_description(self, relation_payload: dict[str, Any]) -> str:
        metadata = relation_payload.get("metadata") if isinstance(relation_payload.get("metadata"), dict) else {}
        description = relation_payload.get("relation_description")
        if not description:
            description = metadata.get("relation_description")
        return str(description or "").strip()

    def _entity_label(self, entity_payload: dict[str, Any], fallback_entity_id: str) -> str:
        for field_name in ("canonical_name", "title", "display_name", "name", "mention_text", "id", "_id"):
            value = str(entity_payload.get(field_name) or "").strip()
            if value:
                return value
        return self._short_entity_label(fallback_entity_id)

    def _short_entity_label(self, entity_id: str) -> str:
        normalized_entity_id = str(entity_id or "").strip()
        if not normalized_entity_id:
            return "unknown"
        return normalized_entity_id.split(":")[-1] or normalized_entity_id

    def _build_detail_html(
        self,
        runtime_config: Any,
        node_objects: list[dict[str, Any]],
        edge_objects: list[dict[str, Any]],
        relation_type_counts: dict[str, int],
        snapshot_metadata: Mapping[str, Any] | None = None,
        namespace_label: str | None = None,
    ) -> str:
        metadata = snapshot_metadata if isinstance(snapshot_metadata, Mapping) else {}
        resolved_namespace_label = str(namespace_label or getattr(runtime_config, "namespace_id", "n/a") or "n/a")
        relation_rows = "".join(
            f"<li><b>{html.escape(relation_type)}</b>: {count}</li>"
            for relation_type, count in sorted(relation_type_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        )
        if not relation_rows:
            relation_rows = "<li>No relation types loaded.</li>"

        sample_edges = "".join(
            (
                f"<li><b>{html.escape(str(edge_object.get('label') or 'related_to'))}</b>: "
                f"{html.escape(str(edge_object.get('source') or 'n/a'))} -> "
                f"{html.escape(str(edge_object.get('target') or 'n/a'))}"
                + (
                    "<br><span style=\"opacity:0.78;\">"
                    + html.escape(str(edge_object.get('description') or ''))
                    + "</span>"
                )
                if str(edge_object.get('description') or '').strip()
                else ""
                + "</li>"
            )
            for edge_object in edge_objects[:8]
        )
        if not sample_edges:
            sample_edges = "<li>No graph edges rendered.</li>"

        return "".join(
            [
                "<h3>Graph View</h3>",
                "<p>Graph projection of AgentDB objects for the selected namespace scope.</p>",
                "<ul>",   
                f"<li><b>Database:</b> {html.escape(str(getattr(runtime_config, 'database_name', 'n/a') or 'n/a'))}</li>",
                f"<li><b>Namespace:</b> {html.escape(resolved_namespace_label)}</li>",
                f"<li><b>Backend:</b> {html.escape(str(getattr(runtime_config, 'agents_db_uri', 'n/a') or 'n/a'))}</li>",
                f"<li><b>Widget URI:</b> {html.escape(str(metadata.get('widget_uri') or metadata.get('source_uri') or 'n/a'))}</li>",
                f"<li><b>Tool:</b> {html.escape(str(metadata.get('tool_id') or self._DEFAULT_TOOL_ID))}</li>",
                f"<li><b>Nodes:</b> {len(node_objects)}</li>",
                f"<li><b>Relations:</b> {len(edge_objects)}</li>",
                "</ul>",
                "<h4>Relation Types</h4>",
                f"<ul>{relation_rows}</ul>",
                "<h4>Rendered Edges</h4>",
                f"<ul>{sample_edges}</ul>",
                "<p>Workflow and sequence diagram tabs are reserved as the next extension surfaces.</p>",
            ]
        )

    def _load_agentsdb_dependencies(self) -> tuple[Any, Any, Any]:
        try:
            if __package__:
                from .agents_db import (  # type: ignore
                    AgentDbRepositoryFactory,
                    AgentDbRepositoryFactoryConfig,
                    load_agentsdb_runtime_config_from_env,
                )
            else:
                from alde.agents_db import (  # type: ignore
                    AgentDbRepositoryFactory,
                    AgentDbRepositoryFactoryConfig,
                    load_agentsdb_runtime_config_from_env,
                )
        except ImportError as exc:
            message = str(exc)
            if "attempted relative import" in message or "no known parent package" in message:
                from agents_db import (  # type: ignore
                    AgentDbRepositoryFactory,
                    AgentDbRepositoryFactoryConfig,
                    load_agentsdb_runtime_config_from_env,
                )
            else:
                raise
        return AgentDbRepositoryFactory, AgentDbRepositoryFactoryConfig, load_agentsdb_runtime_config_from_env
