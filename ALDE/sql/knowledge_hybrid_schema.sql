-- ALDE knowledge schema
--
-- Ziel:
-- - relationale Wahrheit fuer Entitaeten, Dokumente und Korrelationen
-- - Graph-Layer fuer Relationen zwischen Entitaeten
-- - Block-Layer fuer Chunking / RAG Retrieval
-- - Embedding-Layer fuer FAISS oder spaetere SQL-nahe Vector-Suche
--
-- Ausgelegt fuer PostgreSQL 15+.
-- Die dense vectors koennen optional in `embedding` gehalten werden; die
-- produktive Suche kann weiterhin ueber FAISS laufen, referenziert durch
-- `index_backend`, `index_namespace` und `index_item_key`.

create schema if not exists alde;

create table if not exists alde.knowledge_namespaces (
    id text primary key,
    tenant_id varchar(24) not null,
    slug varchar(96) not null,
    name varchar(160) not null,
    description text not null default '',
    index_backend varchar(32) not null default 'faiss',
    default_embedding_model varchar(160) not null,
    default_embedding_dimension integer not null,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint ck_knowledge_namespaces_dimension_positive
        check (default_embedding_dimension > 0),
    constraint uq_knowledge_namespaces_tenant_slug
        unique (tenant_id, slug)
);

create index if not exists ix_knowledge_namespaces_tenant_id
    on alde.knowledge_namespaces (tenant_id);

create index if not exists ix_knowledge_namespaces_metadata_json
    on alde.knowledge_namespaces using gin (metadata_json);


create table if not exists alde.entities (
    id text primary key,
    tenant_id varchar(24) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    entity_type varchar(64) not null,
    canonical_name varchar(320) not null,
    external_key varchar(160),
    correlation_id varchar(128),
    status varchar(32) not null default 'active',
    summary text not null default '',
    attributes_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint ck_entities_status
        check (status in ('active', 'merged', 'archived')),
    constraint uq_entities_namespace_type_name
        unique (namespace_id, entity_type, canonical_name),
    constraint uq_entities_namespace_external_key
        unique nulls not distinct (namespace_id, external_key)
);

create index if not exists ix_entities_tenant_id
    on alde.entities (tenant_id);

create index if not exists ix_entities_namespace_id
    on alde.entities (namespace_id);

create index if not exists ix_entities_type
    on alde.entities (entity_type);

create index if not exists ix_entities_correlation_id
    on alde.entities (correlation_id);

create index if not exists ix_entities_attributes_json
    on alde.entities using gin (attributes_json);


create table if not exists alde.entity_aliases (
    id bigserial primary key,
    entity_id text not null references alde.entities(id) on delete cascade,
    alias varchar(320) not null,
    alias_type varchar(48) not null default 'synonym',
    locale varchar(16),
    confidence numeric(5,4) not null default 1.0,
    source_document_id text,
    created_at timestamptz not null default now(),
    constraint ck_entity_aliases_confidence
        check (confidence >= 0 and confidence <= 1),
    constraint uq_entity_aliases_entity_alias_locale
        unique nulls not distinct (entity_id, alias, locale)
);

create index if not exists ix_entity_aliases_alias
    on alde.entity_aliases (alias);


create table if not exists alde.documents (
    id text primary key,
    tenant_id varchar(24) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    document_type varchar(64) not null,
    title varchar(320) not null,
    source_uri text not null,
    source_system varchar(64) not null default 'local',
    mime_type varchar(128) not null default 'text/plain',
    language_code varchar(16),
    content_sha256 char(64) not null,
    correlation_id varchar(128),
    author_entity_id text references alde.entities(id) on delete set null,
    summary text not null default '',
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    search_vector tsvector generated always as (
        to_tsvector(
            'simple',
            coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(source_uri, '')
        )
    ) stored,
    constraint uq_documents_namespace_sha
        unique (namespace_id, content_sha256),
    constraint uq_documents_namespace_source_uri
        unique (namespace_id, source_uri)
);

create index if not exists ix_documents_tenant_id
    on alde.documents (tenant_id);

create index if not exists ix_documents_namespace_id
    on alde.documents (namespace_id);

create index if not exists ix_documents_document_type
    on alde.documents (document_type);

create index if not exists ix_documents_correlation_id
    on alde.documents (correlation_id);

create index if not exists ix_documents_author_entity_id
    on alde.documents (author_entity_id);

create index if not exists ix_documents_metadata_json
    on alde.documents using gin (metadata_json);

create index if not exists ix_documents_search_vector
    on alde.documents using gin (search_vector);


create table if not exists alde.document_blocks (
    id text primary key,
    tenant_id varchar(24) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    document_id text not null references alde.documents(id) on delete cascade,
    block_no integer not null,
    block_kind varchar(32) not null default 'chunk',
    heading varchar(320),
    content text not null,
    token_count integer,
    char_start integer,
    char_end integer,
    parent_block_id text references alde.document_blocks(id) on delete cascade,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    search_vector tsvector generated always as (
        to_tsvector('simple', coalesce(heading, '') || ' ' || coalesce(content, ''))
    ) stored,
    constraint ck_document_blocks_block_kind
        check (block_kind in ('chunk', 'section', 'table', 'code', 'quote', 'summary')),
    constraint ck_document_blocks_token_count_nonnegative
        check (token_count is null or token_count >= 0),
    constraint ck_document_blocks_char_range
        check (
            (char_start is null and char_end is null)
            or (char_start is not null and char_end is not null and char_start >= 0 and char_end > char_start)
        ),
    constraint uq_document_blocks_document_block_no
        unique (document_id, block_no)
);

create index if not exists ix_document_blocks_tenant_id
    on alde.document_blocks (tenant_id);

create index if not exists ix_document_blocks_namespace_id
    on alde.document_blocks (namespace_id);

create index if not exists ix_document_blocks_document_id
    on alde.document_blocks (document_id);

create index if not exists ix_document_blocks_parent_block_id
    on alde.document_blocks (parent_block_id);

create index if not exists ix_document_blocks_metadata_json
    on alde.document_blocks using gin (metadata_json);

create index if not exists ix_document_blocks_search_vector
    on alde.document_blocks using gin (search_vector);


create table if not exists alde.entity_mentions (
    id bigserial primary key,
    entity_id text not null references alde.entities(id) on delete cascade,
    block_id text not null references alde.document_blocks(id) on delete cascade,
    mention_text varchar(320) not null,
    char_start integer,
    char_end integer,
    extractor varchar(80) not null default 'manual',
    confidence numeric(5,4) not null default 1.0,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint ck_entity_mentions_confidence
        check (confidence >= 0 and confidence <= 1),
    constraint ck_entity_mentions_char_range
        check (
            (char_start is null and char_end is null)
            or (char_start is not null and char_end is not null and char_start >= 0 and char_end > char_start)
        )
);

create index if not exists ix_entity_mentions_entity_id
    on alde.entity_mentions (entity_id);

create index if not exists ix_entity_mentions_block_id
    on alde.entity_mentions (block_id);

create index if not exists ix_entity_mentions_metadata_json
    on alde.entity_mentions using gin (metadata_json);


create table if not exists alde.entity_relations (
    id text primary key,
    tenant_id varchar(24) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    source_entity_id text not null references alde.entities(id) on delete cascade,
    target_entity_id text not null references alde.entities(id) on delete cascade,
    relation_type varchar(80) not null,
    direction varchar(16) not null default 'directed',
    weight numeric(8,5) not null default 1.0,
    confidence numeric(5,4) not null default 1.0,
    valid_from timestamptz,
    valid_to timestamptz,
    correlation_id varchar(128),
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint ck_entity_relations_direction
        check (direction in ('directed', 'undirected')),
    constraint ck_entity_relations_weight_positive
        check (weight >= 0),
    constraint ck_entity_relations_confidence
        check (confidence >= 0 and confidence <= 1),
    constraint ck_entity_relations_valid_range
        check (valid_to is null or valid_from is null or valid_to >= valid_from),
    constraint ck_entity_relations_self_edge
        check (source_entity_id <> target_entity_id)
);

create index if not exists ix_entity_relations_tenant_id
    on alde.entity_relations (tenant_id);

create index if not exists ix_entity_relations_namespace_id
    on alde.entity_relations (namespace_id);

create index if not exists ix_entity_relations_source_target
    on alde.entity_relations (source_entity_id, target_entity_id);

create index if not exists ix_entity_relations_relation_type
    on alde.entity_relations (relation_type);

create index if not exists ix_entity_relations_correlation_id
    on alde.entity_relations (correlation_id);

create index if not exists ix_entity_relations_metadata_json
    on alde.entity_relations using gin (metadata_json);


create table if not exists alde.relation_evidence (
    relation_id text not null references alde.entity_relations(id) on delete cascade,
    block_id text not null references alde.document_blocks(id) on delete cascade,
    evidence_role varchar(32) not null default 'supporting',
    created_at timestamptz not null default now(),
    primary key (relation_id, block_id),
    constraint ck_relation_evidence_role
        check (evidence_role in ('supporting', 'contradicting', 'mention', 'source'))
);

create index if not exists ix_relation_evidence_block_id
    on alde.relation_evidence (block_id);


create table if not exists alde.embedding_models (
    id text primary key,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    model_name varchar(160) not null,
    provider varchar(64) not null,
    task_type varchar(32) not null default 'retrieval_document',
    dimension integer not null,
    distance_metric varchar(16) not null default 'cosine',
    is_active boolean not null default true,
    config_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint ck_embedding_models_dimension_positive
        check (dimension > 0),
    constraint ck_embedding_models_distance_metric
        check (distance_metric in ('cosine', 'l2', 'ip')),
    constraint uq_embedding_models_namespace_model_task
        unique (namespace_id, model_name, task_type)
);

create index if not exists ix_embedding_models_namespace_id
    on alde.embedding_models (namespace_id);

create index if not exists ix_embedding_models_active
    on alde.embedding_models (is_active);


create table if not exists alde.embeddings (
    id bigserial primary key,
    tenant_id varchar(24) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    model_id text not null references alde.embedding_models(id) on delete cascade,
    owner_type varchar(24) not null,
    owner_id text not null,
    content_sha256 char(64) not null,
    chunk_hash char(64),
    embedding real[],
    dimension integer not null,
    index_backend varchar(32) not null default 'faiss',
    index_namespace varchar(160) not null,
    index_item_key varchar(160) not null,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint ck_embeddings_owner_type
        check (owner_type in ('document', 'block', 'entity', 'relation', 'query_template')),
    constraint ck_embeddings_dimension_positive
        check (dimension > 0),
    constraint ck_embeddings_array_dimension
        check (embedding is null or coalesce(array_length(embedding, 1), 0) = dimension),
    constraint uq_embeddings_owner_model_sha
        unique (namespace_id, owner_type, owner_id, model_id, content_sha256),
    constraint uq_embeddings_index_item
        unique (index_backend, index_namespace, index_item_key)
);

create index if not exists ix_embeddings_tenant_id
    on alde.embeddings (tenant_id);

create index if not exists ix_embeddings_namespace_model
    on alde.embeddings (namespace_id, model_id);

create index if not exists ix_embeddings_owner
    on alde.embeddings (owner_type, owner_id);

create index if not exists ix_embeddings_index_lookup
    on alde.embeddings (index_backend, index_namespace, index_item_key);

create index if not exists ix_embeddings_metadata_json
    on alde.embeddings using gin (metadata_json);


create table if not exists alde.retrieval_runs (
    id text primary key,
    tenant_id varchar(24) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    query_text text not null,
    requested_k integer not null default 5,
    lexical_k integer,
    graph_hops integer,
    vector_k integer,
    rerank_strategy varchar(48) not null default 'none',
    correlation_id varchar(128),
    filters_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    constraint ck_retrieval_runs_requested_k_positive
        check (requested_k > 0)
);

create index if not exists ix_retrieval_runs_tenant_id
    on alde.retrieval_runs (tenant_id);

create index if not exists ix_retrieval_runs_namespace_id
    on alde.retrieval_runs (namespace_id);

create index if not exists ix_retrieval_runs_correlation_id
    on alde.retrieval_runs (correlation_id);

create index if not exists ix_retrieval_runs_filters_json
    on alde.retrieval_runs using gin (filters_json);


create table if not exists alde.retrieval_results (
    retrieval_run_id text not null references alde.retrieval_runs(id) on delete cascade,
    rank_no integer not null,
    result_type varchar(24) not null,
    result_id text not null,
    source_stage varchar(24) not null,
    lexical_score double precision,
    vector_score double precision,
    graph_score double precision,
    rerank_score double precision,
    chosen boolean not null default true,
    metadata_json jsonb not null default '{}'::jsonb,
    primary key (retrieval_run_id, rank_no),
    constraint ck_retrieval_results_result_type
        check (result_type in ('document', 'block', 'entity', 'relation')),
    constraint ck_retrieval_results_source_stage
        check (source_stage in ('sql', 'fts', 'graph', 'vector', 'rerank', 'manual'))
);

create index if not exists ix_retrieval_results_result_lookup
    on alde.retrieval_results (result_type, result_id);

create index if not exists ix_retrieval_results_metadata_json
    on alde.retrieval_results using gin (metadata_json);


comment on table alde.knowledge_namespaces is
'Mandantenfaehige Wissensraeume. Ein Namespace kapselt Entitaeten, Dokumente, Relationen und einen Vector-Index.';

comment on table alde.entities is
'Relationale Wahrheit fuer fachliche Objekte wie Person, Firma, Skill, Projekt oder Stelle.';

comment on table alde.entity_relations is
'Graph-Layer: gerichtete oder ungerichtete Kanten zwischen Entitaeten mit Evidenz und Gewichtung.';

comment on table alde.document_blocks is
'Chunk-Layer fuer RAG. Ein Block ist die kleinste Retrieval-Einheit fuer Volltext, Graph-Evidenz und Embeddings.';

comment on table alde.embeddings is
'Vector-Layer. `embedding` ist optional; die produktive Suche kann via FAISS ueber `index_*` referenziert werden.';


-- ---------------------------------------------------------------------------
-- Positions-Layer (UI / Graph)
-- Trennt Layout-Koordinaten von Fachdaten, damit Relationen und Entitaeten
-- unabhaengig vom Rendering neupositioniert werden koennen.
-- x_ml / y_ml sind die ML-berechneten Canvas-Positionen eines Knotens.
-- weight_pos wird aus der XY-Distanz zweier Knoten abgeleitet:
--   dist       = sqrt((x2-x1)^2 + (y2-y1)^2)
--   weight_pos = 1 / (1 + dist / d_max)
-- layout_version signalisiert dem Renderer, ob Positionen aktuell sind.
-- ---------------------------------------------------------------------------

create table if not exists alde.graph_node_positions (
    node_id varchar(256) not null,
    namespace_id text not null references alde.knowledge_namespaces(id) on delete cascade,
    x_ml double precision not null default 0.0,
    y_ml double precision not null default 0.0,
    layout_version varchar(32) not null default 'v1',
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (node_id, namespace_id, layout_version)
);

create index if not exists ix_graph_node_positions_namespace_id
    on alde.graph_node_positions (namespace_id);

create index if not exists ix_graph_node_positions_layout_version
    on alde.graph_node_positions (namespace_id, layout_version);

comment on table alde.graph_node_positions is
'Positions-Layer fuer den Relation-Graph. Haelt XY-Koordinaten der Knoten getrennt von Fachdaten.
weight_pos einer Kante n1->n2 = 1 / (1 + dist(n1,n2) / d_max).
Composite weight = alpha * weight_pos + beta * confidence + gamma * evidence.';