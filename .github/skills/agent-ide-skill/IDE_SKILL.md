---
name: agent-ide-skill
description: 'Use when configuring ALDE IDE startup/runtime environment, AgentsDB environment, .env and .env.json, env override behavior, and tree memory-cache read/write workflows. Alias: AGENT.IDE_SKILL.'
argument-hint: 'Describe the env/config task, for example: fix env_override query, sync .env/.env.json, validate tree cache'
user-invocable: true
---

# AGENT.IDE_SKILL

Alias: AGENT.IDE_SKILL

## Purpose

This skill defines a repeatable workflow for ALDE IDE environment setup and debugging:

- IDE startup environment (.env.json and .env)
- runtime environment override behavior
- AgentsDB runtime connection and source query config
- tree memory-cache read/write behavior in the explorer persistence layer

## Use When

Use this skill when one or more of the following is true:

- IDE startup loads wrong env values
- python-dotenv parse warnings appear
- .env.json or .env drift causes inconsistent runtime behavior
- AI_IDE_AGENTS_DB_SOURCES must be corrected
- env_override query returns empty or wrong fields
- tree cache must be read/written explicitly in memory-only mode

## Files And Components

Primary config files:

- ALDE/.env.json (canonical startup config)
- ALDE/.env (legacy compatibility file)
- ALDE/AppData/gui_env.json (GUI env overlay)

Core runtime code paths:

- ALDE/alde/ai_ide_v1756.py
- ALDE/alde/jstree_widget.py
- scripts/agentdb_server_socket_.py
- ALDE/alde/agents_ccomp.py

## Environment Precedence Rules

1. Existing process environment variables win, because startup loaders apply values via os.environ.setdefault.
2. Startup env file values are loaded from ALDE/.env.json (and fallbacks like .env).
3. GUI env overlay uses setdefault and skips empty values, so it should not overwrite already-set values.
4. For socket-server startup, --override-env can force env-file values to overwrite existing process env.

## .env.json Rules

ALDE/.env.json must be strict JSON.

- No comments
- No YAML frontmatter separators (---)
- No trailing commas
- Keep AI_IDE_AGENTS_DB_SOURCES as a valid JSON object

Example minimal shape:

```json
{
  "format": "alde_env_json_v1",
  "env": {
    "AI_IDE_STARTUP_ENV_FILE_PATH": "ALDE/.env.json",
    "AI_IDE_AGENTS_DB_SOURCES": {
      "strict": true,
      "sources": [],
      "allowlist": {
        "fields": {
          "document": ["_id"],
          "entity": ["_id"]
        },
        "import_sources": []
      }
    }
  }
}
```

## AgentsDB Query Guidance

For env override projections, use entity queries, not document queries.

Recommended env_override source shape:

```json
{
  "section": "ENV",
  "key": "env_override",
  "kind": "agentsdb_query",
  "object_name": "entity",
  "filter": {
    "namespace_id": "ns_repo_knowledge",
    "entity_type": "environment_override"
  },
  "fields": ["_id", "entity_type", "canonical_name", "attributes", "updated_at"],
  "limit": 200
}
```

## Tree Memory-Cache Read/Write

Relevant API behavior in TreeDataPersistenceService:

- memory_only_enabled(): evaluates AI_IDE_TREE_MEMORY_ONLY
- load_data(): returns (data, backend, source)
- save_data(data): writes to memory cache or backend depending on mode
- _store_inmemory_tree_data(...): internal in-memory write path
- persist_env_projection_from_tree_data(data): persists ENV projection back to env file path

Use this workflow when operating on cache data:

1. Instantiate TreeDataPersistenceService with AppData path.
2. Call load_data() and inspect backend/source.
3. Modify only target sections/keys.
4. Call save_data() for cache persistence.
5. If ENV section changed, call persist_env_projection_from_tree_data(data).

## Runtime Override Workflow

For controlled runtime overrides:

1. Keep canonical baseline in ALDE/.env.json.
2. Keep ALDE/.env for compatibility consumers.
3. For shell-session overrides, export env vars before launching IDE.
4. For socket server env-file dominance, use --override-env.

## Validation Checklist

After any env/config edit:

1. Validate ALDE/.env.json parses as JSON.
2. Validate AI_IDE_AGENTS_DB_SOURCES parses as JSON (from .env or .env.json env payload).
3. Confirm AI_IDE_STARTUP_ENV_FILE_PATH points to ALDE/.env.json.
4. Run a startup import smoke test for ALDE/alde/ai_ide_v1756.py.
5. Verify env_override query returns records in expected namespace.
6. Verify tree renders ENV env_override records and cache writes persist.

## Non-Goals

- Do not store secrets in git-tracked files unless explicitly required.
- Do not switch object_name between entity/document without verifying repository object model.
- Do not treat commented JSON-like text as valid .env.json content.


## IDE Environment and AgentsDB Environment Configuration File

1. _AgentsDB Environment Configuration:_

- This JSON file is used to configure the environment variables for the ALDE IDE 
- and its underlying AgentsDB knowledge database.
- It defines the necessary settings for connecting to the knowledge database,
- including API keys, database URLs, and other relevant configuration details.

2. _Tree Environment Configuration:_

- The following environment variables are used to configure 
- the AI IDE's AgentsDB tree view and its data sources.
- They define how the IDE interacts with the underlying knowledge database,
- including which sections and keys to query,
- the structure of the queries, and the fields to retrieve.

```json
{
  "format": "alde_env_json_v1",
  "env": {
    "OPENAI_API_KEY": "your_openai_api_key_here",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_URI": "agentsdb://127.0.0.1:2331",
    "AI_IDE_MCP_CONNECTION_PROXY_URI": "agentsdb://127.0.0.1:2331/tools:graph_view",
    "ALDE_MCP_CONNECTION_PROXY_URI": "agentsdb://127.0.0.1:2331/tools:graph_view",
    "ALDE_MCP_DEFAULT_SERVER": "local-tcp",
    "ALDE_MCP_FALLBACK_ORDER": "local-http",
    "ALDE_MCP_ENABLE_STDIO_FALLBACK": "0",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_NAME": "alde_knowledge",
    "AI_IDE_KNOWLEDGE_AGENTS_IMAGE_PATH": "/home/ben/Vs_Code_Projects/Projects/ALDE_Projekt/ALDE/AppData/agentsdb.json",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_TENANT_ID": "tenant_default",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_ID": "ns_alde_default",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_SLUG": "alde-default",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_NAMESPACE_NAME": "ALDE Default Knowledge",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_EMBEDDING_MODEL": "text-embedding-3-large",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_EMBEDDING_DIMENSION": "3072",
    "AI_IDE_KNOWLEDGE_AGENTS_DB_INDEX_BACKEND": "embedding_cosine",
    "AI_IDE_AGENTS_DB_TREE_STRICT": "0",
    "AI_IDE_TREE_AGENTS_DB_OBJECT_ID": "tree_widget:tree_data",
    "AI_IDE_STARTUP_ENV_FILE_PATH": "ALDE/.env.json",
    "AI_IDE_AGENTSDB_SERVER_SCRIPT_PATH": "scripts/agentdb_server_socket.py",
    "AI_IDE_TREE_MEMORY_ONLY": "1",

    "AI_IDE_TREE_SECTION_ALLOWLIST": "ENV,CHAT_HISTORY,RUNTIME",
    "AI_IDE_AGENTS_DB_TREE_REPOSITORY_VIEW": "0",
    "AI_IDE_AGENTS_DB_PIPELINE_STRICT": "1",
    "AI_IDE_AGENTS_DB_SOURCES": {
      "strict": true,
      "sources": [
        {
          "section": "CHAT_HISTORY",
          "key": "chat_history",
          "kind": "agentsdb_query",
          "object_name": "document",
          "filter": {
            "namespace_id": "ns_alde_default",
            "document_type": "chat_history"
          },
          "fields": [
            "_id",
            "title",
            "updated_at",
            "document_type",
            "source_uri",
            "notes"
          ],
          "limit": 200
        },
        {
          "section": "RUNTIME",
          "key": "agents_runtime",
          "kind": "agentsdb_query",
          "object_name": "document",
          "filter": {
            "namespace_id": "ns_alde_default",
            "document_type": "document",
            "title": "agent_runtime.py"
          },
          "fields": [
            "_id",
            "title",
            "updated_at",
            "document_type",
            "source_uri",
            "notes"
          ],
          "limit": 200
        },
        {
          "section": "ENV",
          "key": "env_override",
          "kind": "agentsdb_query",
          "object_name": "entity",
          "filter": {
            "namespace_id": "ns_repo_knowledge",
            "entity_type": "environment_override"
          },
          "fields": [
            "_id",
            "entity_type",
            "canonical_name",
            "attributes",
            "updated_at"
          ],
          "limit": 200
        }
      ],
      "allowlist": {
        "fields": {
          "document": [
            "_id",
            "title",
            "updated_at",
            "document_type",
            "source_uri",
            "notes"
          ],
          "entity": [
            "_id",
            "entity_type",
            "canonical_name",
            "attributes",
            "updated_at"
          ]
        },
        "import_sources": [
          "profile_id",
          "profiles_db",
          "correlation_id",
          "job_postings_db"
        ]
      }
    }
  }
}
```