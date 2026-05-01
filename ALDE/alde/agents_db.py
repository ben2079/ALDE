from __future__ import annotations



import hashlib
import json
import logging
import os
import re
import socket
import socketserver
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


_AGENTSDB_SOCKET_SERVER_LOCK = threading.RLock()
_AGENTSDB_SOCKET_SERVER_STATE: dict[tuple[str, int], dict[str, Any]] = {}
_LOGGER = logging.getLogger(__name__)
_AGENTSDB_CONNECTION_CONFIG_CACHE: dict[str, Any] | None = None


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


def _load_agentsdb_uri_from_connection_config(config_payload: Mapping[str, Any]) -> str:
    configured_uri = _connection_config_value(config_payload, ("agents_db_uri", "agentsdb_uri", "uri", "socket_uri"))
    if configured_uri:
        return configured_uri
    host_value = _connection_config_value(config_payload, ("host", "hostname")) or "localhost"
    port_value = _connection_config_value(config_payload, ("port",)) or "2331"
    try:
        resolved_port = int(port_value)
    except Exception:
        resolved_port = 2331
    return f"agentsdb://{host_value}:{resolved_port}"


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
    parsed_uri = urlparse(str(agents_db_uri or "").strip())
    if str(parsed_uri.scheme or "").strip().lower() != "agentsdb":
        return False
    resolved_host = str(parsed_uri.hostname or "localhost").strip() or "localhost"
    resolved_port = int(parsed_uri.port or 2331)
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
    return str(uri or "").strip().lower().startswith("agentsdb://")


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
    return datetime.now( tzinfo=UTC)


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
    default_embedding_dimension: int
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
    index_backend: str = "faiss"

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
    confidence: float = 0.95
    mention_text: str | None = None
    summary: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


TECHNICAL_TYPE_KEY_PATTERN_MAP: dict[str, tuple[str, ...]] = {
    "tool": ("jira", "topdesk", "servicenow"),
    "framework": ("itil", "scrum", "kanban"),
    "database": ("postgresql", "postgres", "oracle", "mysql", "mongodb"),
    "protocol": ("tcp/ip", "tcpip", "http", "https", "http(s)", "rdp", "ssh"),
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
                "collection_path": "requirements.technical_skills",
                "source_field": "requirements.technical_skills",
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
        payload = {
            "schema": "agentsdb_repository_image_v1",
            "updated_at": _now_utc().isoformat(),
            "collections": _json_safe_object(self._collections),
            "index_objects": _json_safe_object(self._index_objects),
        }
        temp_path = f"{path}.tmp"
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

    def load_objects(self, object_name: str, object_filter: Mapping[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            collection = self.load_collection(object_name)
            filter_payload = dict(object_filter or {})
            result_payload_list: list[dict[str, Any]] = []
            for object_payload in collection.values():
                if not isinstance(object_payload, Mapping):
                    continue
                if any(object_payload.get(key) != value for key, value in filter_payload.items()):
                    continue
                result_payload_list.append(dict(object_payload))
                if len(result_payload_list) >= max(1, int(limit)):
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
        owner_type: str = "block",
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

    def __init__(self, agents_db_uri: str, database_name: str = "alde_knowledge", timeout_seconds: float = 5.0) -> None:
        self._agents_db_uri = str(agents_db_uri or "").strip()
        self._database_name = str(database_name or "alde_knowledge").strip() or "alde_knowledge"
        self._timeout_seconds = max(float(timeout_seconds), 0.5)
        parsed_uri = urlparse(self._agents_db_uri)
        self._host = str(parsed_uri.hostname or "localhost")
        self._port = int(parsed_uri.port or 2331)

    @classmethod
    def create_from_uri(
        cls,
        agents_db_uri: str,
        database_name: str = "alde_knowledge",
        timeout_seconds: float = 5.0,
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
        try:
            response_bytes = self._send_request_bytes(request_payload)
        except OSError:
            if _ensure_local_agentsdb_socket_server(self._agents_db_uri, timeout_seconds=self._timeout_seconds):
                response_bytes = self._send_request_bytes(request_payload)
            else:
                raise
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
            raise RuntimeError(str(response_payload.get("error") or "agentsdb socket request failed"))
        return dict(response_payload)

    def _send_request_bytes(self, request_payload: Mapping[str, Any]) -> bytes:
        serialized_request_payload = _json_safe_object(dict(request_payload))
        with socket.create_connection((self._host, self._port), timeout=self._timeout_seconds) as connection:
            connection.sendall((json.dumps(serialized_request_payload, separators=(",", ":")) + "\n").encode("utf-8"))
            response_bytes = b""
            while b"\n" not in response_bytes:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response_bytes += chunk
        return response_bytes

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

    def load_objects(self, object_name: str, object_filter: Mapping[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        response_payload = self._request_object(
            "load_objects",
            {
                "object_name": str(object_name),
                "object_filter": _deepcopy_object(dict(object_filter or {})),
                "limit": max(1, int(limit)),
            },
        )
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


class AgentDbInMemoryRepository:
    """Knowledge repository that stores all objects in-memory and flushes snapshots to disk."""

    _OBJECT_COLLECTION_MAP = KnowledgeRepository._OBJECT_COLLECTION_MAP

    def __init__(self, image_path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._image_path = str(image_path or "").strip() or None
        self._collections: dict[str, dict[str, dict[str, Any]]] = {
            collection_name: {}
            for collection_name in self._OBJECT_COLLECTION_MAP.values()
        }
        self._load_image()

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
        image_payload = {
            "schema": "agentsdb_inmemory_image_v1",
            "updated_at": _now_utc().isoformat(),
            "collections": _json_safe_object(self._collections),
        }
        temp_path = f"{path}.tmp"
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
            self._flush_image()
            return dict(payload)

    def delete_object(self, object_name: str, object_id: str) -> bool:
        with self._lock:
            collection = self._load_collection_object(object_name)
            normalized_object_id = str(object_id)
            deleted = normalized_object_id in collection
            if deleted:
                collection.pop(normalized_object_id, None)
                self._flush_image()
            return deleted

    def load_object(self, object_name: str, object_id: str) -> dict[str, Any] | None:
        with self._lock:
            collection = self._load_collection_object(object_name)
            payload = collection.get(object_id)
            return dict(payload) if isinstance(payload, Mapping) else None

    def load_objects(self, object_name: str, object_filter: Mapping[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            collection = self._load_collection_object(object_name)
            filter_payload = dict(object_filter or {})
            result_payload_list: list[dict[str, Any]] = []
            for object_payload in collection.values():
                if not isinstance(object_payload, Mapping):
                    continue
                if any(object_payload.get(key) != value for key, value in filter_payload.items()):
                    continue
                result_payload_list.append(dict(object_payload))
                if len(result_payload_list) >= max(1, int(limit)):
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


def _is_memory_backend_uri(uri: str | None) -> bool:
    normalized_uri = str(uri or "").strip().lower()
    return normalized_uri.startswith("agentsdb://") or normalized_uri.startswith("memodb://") or normalized_uri.startswith("inmemdb://")


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
        self._repository_cache: dict[str, Any] = {}

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

    def load_repository(self, database_name: str | None = None) -> Any:
        resolved_database_name = str(database_name or self._default_database_name).strip() or self._default_database_name
        repository:AgentDbInMemoryRepository|KnowledgeRepository = self._repository_cache.get(resolved_database_name)
        if repository is not None:
            return repository
        if _is_memory_backend_uri(self._backend_uri):
            repository = AgentDbInMemoryRepository(self._memory_image_path)
        else:
            repository = KnowledgeRepository.create_from_uri(self._backend_uri, resolved_database_name)
        self._repository_cache[resolved_database_name] = repository
        return repository

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
        repository: AgentDbInMemoryRepository | KnowledgeRepository = self.load_repository(database_name)
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
            return {"ok": True, "object_payload": _json_safe_object(stored_payload)}
        if normalized_cmd == "delete_object":
            object_name = str(payload.get("object_name") or "").strip()
            object_id = str(payload.get("object_id") or "").strip()
            if not object_name or not object_id:
                raise ValueError("delete_object requires object_name and object_id")
            deleted = repository.delete_object(object_name, object_id)
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
            limit = payload.get("limit", 50)
            if not object_name:
                raise ValueError("load_objects requires object_name")
            if object_filter is not None and not isinstance(object_filter, Mapping):
                raise ValueError("load_objects object_filter must be an object")
            object_payload_list = repository.load_objects(
                object_name,
                dict(object_filter or {}),
                max(1, int(limit)),
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
    def handle(self) -> None:
        service: AgentDbSocketServerService = self.server.service
        raw_line = self.rfile.readline()
        if not raw_line:
            return
        response_payload: dict[str, Any]
        try:
            cmd, database_name, payload = _parse_agentsdb_socket_request_line(raw_line)
            response_payload = service.dispatch_object(cmd=cmd, payload=payload, database_name=database_name)
        except Exception as exc:
            response_payload = {
                "ok": False,
                "error": "agents_db_socket_request_failed",
                "detail": str(exc),
            }
        self.wfile.write((json.dumps(_json_safe_object(response_payload), separators=(",", ":")) + "\n").encode("utf-8"))


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
    parsed_uri = urlparse(agents_db_uri or "agentsdb://localhost:2331")
    resolved_host = str(host or parsed_uri.hostname or "localhost").strip() or "localhost"
    resolved_port = int(port or parsed_uri.port or 2331)
    service = AgentDbSocketServerService.load_from_env()
    with _AgentDbSocketTCPServer((resolved_host, resolved_port), _AgentDbSocketRequestHandler, service) as server:
        server.serve_forever()


class KnowledgeObjectService:
    """Domain service for storing and querying the knowledge model."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repository = repository

    def store_namespace_object(self, namespace_object: NamespaceObject) -> Mapping[str, Any]:
        return self._repository.upsert_object("namespace", namespace_object.id, _dataclass_payload(namespace_object))

    def store_entity_object(self, entity_object: EntityObject) -> Mapping[str, Any]:
        return self._repository.upsert_object("entity", entity_object.id, _dataclass_payload(entity_object))

    def store_document_object(self, document_object: DocumentObject) -> Mapping[str, Any]:
        return self._repository.upsert_object("document", document_object.id, _dataclass_payload(document_object))

    def store_relation_object(self, relation_object: EntityRelationObject) -> Mapping[str, Any]:
        return self._repository.upsert_object("relation", relation_object.id, _dataclass_payload(relation_object))

    def store_embedding_object(self, embedding_object: EmbeddingObject) -> Mapping[str, Any]:
        object_id = ":".join(
            [
                embedding_object.namespace_id,
                embedding_object.owner_type,
                embedding_object.owner_id,
                embedding_object.model_id,
            ],
        )
        return self._repository.upsert_object("embedding", object_id, _dataclass_payload(embedding_object))

    def store_retrieval_run_object(self, retrieval_run_object: RetrievalRunObject) -> Mapping[str, Any]:
        return self._repository.upsert_object(
            "retrieval_run",
            retrieval_run_object.id,
            _dataclass_payload(retrieval_run_object),
        )

    def store_dispatcher_run_object(self, dispatcher_run_object: DispatcherRunObject) -> Mapping[str, Any]:
        return self._repository.upsert_object(
            "dispatcher_run",
            dispatcher_run_object.id,
            _dataclass_payload(dispatcher_run_object),
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
        return self._repository.load_relation_object_graph(
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
        return self._repository.build_vector_search_pipeline(
            query_vector=query_vector,
            namespace_id=namespace_id,
            owner_type=owner_type,
            limit=limit,
        )


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
            collection_value_list = _load_string_list(_mapping_value(object_payload, str(entity_pattern.get("collection_path") or "")))
            for collection_value in collection_value_list:
                type_key = _load_type_key_from_pattern(
                    collection_value,
                    fallback_type_key=str(entity_pattern.get("fallback_type_key") or object_name),
                    type_key_pattern_map=entity_pattern.get("type_key_pattern_map"),
                )
                relation_type_key_map = dict(entity_pattern.get("relation_type_key_map") or {})
                relation_type_key = str(relation_type_key_map.get(type_key) or relation_type_key_map.get(str(entity_pattern.get("fallback_type_key") or "")) or "").strip() or None
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
                        metadata={"source_field": entity_pattern.get("source_field")},
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
            seed_key = _first_non_empty_string([entity_payload.get("entity_key"), entity_payload.get("seed_key")]) or f"{type_key}:{_slugify_object_name(canonical_name)}"
            seed_object_list.append(
                MappingSeedEntityObject(
                    seed_key=seed_key,
                    type_key=type_key,
                    canonical_name=canonical_name,
                    section_key=_first_non_empty_string([entity_payload.get("section_key")]) or "primary",
                    relation_type_key=_first_non_empty_string([entity_payload.get("relation_type"), entity_payload.get("relation_type_key")]),
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


_AGENTS_DB_PIPELINE_SERVICE_CACHE: dict[tuple[str, ...], PipelineService] = {}


def load_agentsdb_runtime_config_from_env() -> RuntimeConfigObject | None:
    connection_config = _load_agentsdb_connection_config()
    agents_db_uri = str(
        os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_URI", "")
        or os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI", ""),
    ).strip()
    if not agents_db_uri:
        configured_socket_uri = _load_agentsdb_uri_from_connection_config(connection_config)
        backend_uri = _connection_config_value(connection_config, ("backend_uri", "agents_db_backend_uri", "storage_uri", "storage_backend_uri"))
        agents_db_uri = configured_socket_uri or backend_uri
    if not agents_db_uri:
        return None
    try:
        default_embedding_dimension = int(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_EMBEDDING_DIMENSION", "")
            or "3072"
            or 3072,
        )
    except Exception:
        default_embedding_dimension = 3072
    return RuntimeConfigObject(
        agents_db_uri=agents_db_uri,
        database_name=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAME", "")
            or "alde_knowledge",
        ).strip()
        or _connection_config_value(connection_config, ("database_name", "database"))
        or "alde_knowledge",
        tenant_id=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_TENANT_ID", "")
            or "tenant_default",
        ).strip()
        or "tenant_default",
        namespace_id=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_ID", "")
            or "ns_alde_default",
        ).strip()
        or "ns_alde_default",
        namespace_slug=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_SLUG", "")
            or "alde-default",
        ).strip()
        or "alde-default",
        namespace_name=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_NAME", "")
            or "ALDE Default Knowledge",
        ).strip()
        or "ALDE Default Knowledge",
        default_embedding_model=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_EMBEDDING_MODEL", "")
            or "text-embedding-3-large",
        ).strip()
        or "text-embedding-3-large",
        default_embedding_dimension=max(1, default_embedding_dimension),
        index_backend=str(
            os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_INDEX_BACKEND", "")
            or "faiss",
        ).strip()
        or "faiss",
    )


def load_mongodb_runtime_config_from_env() -> RuntimeConfigObject | None:
    return load_agentsdb_runtime_config_from_env()


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
    if _is_agentsdb_socket_uri(runtime_config.agents_db_uri):
        repository: Any = AgentDbSocketRepository.create_from_uri(
            runtime_config.agents_db_uri,
            runtime_config.database_name,
                
        )
    else:
        repository = KnowledgeRepository.create_from_uri(runtime_config.agents_db_uri, runtime_config.database_name)
    repository.ensure_index_objects()
    pipeline_service = PipelineService(KnowledgeObjectService(repository), runtime_config)
    _AGENTS_DB_PIPELINE_SERVICE_CACHE[cache_key] = pipeline_service
    return pipeline_service


def load_mongodb_pipeline_service(runtime_config: RuntimeConfigObject) -> PipelineService:
    return load_agentsdb_pipeline_service(runtime_config)


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

