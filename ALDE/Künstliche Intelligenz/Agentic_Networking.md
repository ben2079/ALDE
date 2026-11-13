# Agentic Networking

## Purpose

This document captures the validated near-term state and the recommended target design for agentic communication and coordination in a LAN-first ALDE deployment.

It is intended as a planning note for later implementation work.
It complements existing architecture notes such as `TARGET_ARCHITECTURE.md`, `REQUEST_RESPONSE_HANDOFF_FLOW.md`, and the current workflow/runtime implementation in `alde/agents_factory.py` and `alde/agents_runtime.py`.

## Validated Current State

As of 2026-04-25, the following runtime and workflow slices were validated:

- AgentsDB-backed agent memory now supports generic structured handoff session context instead of only the previous writer-specific applicant-profile special case.
- Session cache entries can be persisted per target memory slot and later re-injected into routed requests.
- Supported cached context blocks now include:
  - `applicant_profile`
  - `profile_result`
  - `job_posting_result`
  - `object_result`
  - `dispatcher_updates`
  - `options`
- A persistence defect in agent memory storage was fixed in the agent-memory write path.
- The dispatch runtime policy was normalized to the canonical requested action `store_object_result` instead of the inconsistent `store_object` value.

## Validation Summary

The following validations were executed successfully:

- Focused workflow tests for agent-memory bootstrap, writer cache reinjection, and generic handoff cache persistence.
- Dispatch smoke workflow validation.
- Real AgentsDB socket pipeline integration validation.

Representative commands used during validation:

```bash
/home/ben/Vs_Code_Projects/Projects/.venv/bin/python -m unittest \
  alde.test_workflow.TestWorkflowIntegration.test_route_to_agent_bootstraps_agentsdb_agent_memory_profile \
  alde.test_workflow.TestWorkflowIntegration.test_dispatch_profile_is_cached_for_writer_and_attached_to_writer_messages \
  alde.test_workflow.TestWorkflowIntegration.test_route_to_agent_caches_generic_handoff_context_for_target_job
```

```bash
/home/ben/Vs_Code_Projects/Projects/.venv/bin/python -m unittest \
  alde.Tests.test_dispatch_pipeline_smoke.TestDispatchPipelineSmoke \
  alde.test_workflow.TestWorkflowIntegration.test_real_agentsdb_socket_pipeline_ingests_job_posting_and_syncs_knowledge
```

## Why A Separate Networking Model Is Needed

The ALDE runtime is already moving toward explicit routing, structured handoff payloads, bounded session context, and deterministic action execution.
A LAN deployment should preserve those properties instead of collapsing them into ad hoc chat transport, email side effects, or direct service-to-service calls.

The networking model therefore needs to separate:

- human collaboration traffic
- agent-to-agent execution traffic
- identity and policy enforcement
- storage and retrieval traffic
- external bridge traffic such as email

## Design Goals

1. Keep human messaging and machine coordination separate.
2. Give every user and every agent a first-class identity.
3. Make routing deterministic and auditable.
4. Keep session context structured instead of prompt-only.
5. Enforce least privilege at the network and token layer.
6. Make external bridges explicit and isolated.
7. Support replay, retry, and correlation for agent jobs.
8. Preserve a future path to distributed execution without rewriting ALDE runtime semantics.

## Recommended Reference Stack

### Human Collaboration Layer

Use Matrix for user-to-user and user-to-agent collaboration.

Why Matrix:

- real messaging protocol
- rooms, membership, and access control
- device and user identity model
- established encryption model
- suitable for operator workflows and human-visible audit trails

This is the right layer for:

- operator interaction
- approvals
- agent notifications
- shared incident or task rooms
- user-facing collaboration around workflows

### Internal Agent Messaging Layer

Use NATS with JetStream for agent-to-agent coordination.

Why NATS:

- low-latency LAN transport
- request-reply support
- subject-based routing
- streaming and replay support
- consumer groups and durable subscriptions
- dead-letter and retry friendly patterns

This is the right layer for:

- planner to worker dispatch
- retrieval requests
- memory update events
- asynchronous workflow state changes
- background processing and orchestration signals

Matrix should not be used as the internal machine bus.
Conversely, NATS should not be treated as the user-facing messenger.

### Identity Layer

Use an internal OIDC provider such as Keycloak or Authentik.

Why:

- central user identity
- central service identity
- token issuance and expiry
- role and group management
- auditable access boundaries

Every agent should have its own identity.
Every user should authenticate through the same identity system.

### Gateway And Routing Layer

Use ASP.NET Core with YARP as the ingress and policy gateway.

Why:

- stable HTTP ingress for UI, automation, and bridge services
- token validation and claim inspection
- route-specific authorization checks
- rate limits and request shaping
- pragmatic fit for LAN service routing and firewall-aware exposure

This gateway should sit between users or external systems and the internal runtime plane.

### Data Layer

Use separate stores by responsibility:

- AgentsDB or Postgres for structured state and persistent records
- object storage such as MinIO for documents and artifacts
- a vector store such as Qdrant or Milvus for embeddings and retrieval indexes
- optional relational store for policy, audit, and workflow metadata

The current AgentsDB-backed session cache fits naturally as agent-scoped structured memory, keyed by:

- `agent_label`
- `memory_slot`
- `scope_key`

## Recommended LAN Zone Model

### 1. Ingress Zone

Hosts externally reachable services inside the LAN perimeter:

- YARP gateway
- Matrix homeserver
- web UI entrypoints
- optional operator API endpoints

Rules:

- terminate TLS here or at an adjacent reverse proxy
- expose only explicitly approved ports
- no direct data-plane access from public-facing clients

### 2. Control Plane Zone

Hosts control and orchestration services:

- planner or router services
- policy service
- approval service
- OIDC identity provider
- workflow coordination service

Rules:

- may publish to the internal message bus
- may query policy and identity stores
- should not directly execute unsafe worker tasks

### 3. Worker Zone

Hosts execution agents:

- parsers
- writers
- retrievers
- transformation services
- document handlers

Rules:

- no unrestricted east-west traffic
- outbound access only to approved bus, storage, retrieval, and bridge endpoints
- no direct access to human-facing ingress unless explicitly required

### 4. Data Zone

Hosts stateful services:

- AgentsDB
- Postgres
- vector store
- object storage
- audit log backends

Rules:

- reachable only by approved services
- no direct user traffic
- encryption in transit required for all internal clients

### 5. Bridge Zone

Hosts integrations with external or semi-external protocols:

- email bridge
- legacy APIs
- ERP or HR adapters
- file drop importers

Rules:

- treat this zone as partially trusted
- sanitize and normalize inbound data before it reaches the internal bus
- require explicit policy for every outbound side effect

## Physical Network Media

For copper Ethernet links, use twisted-pair cabling and select its category for the required link speed and channel length. Cat 6A is suitable for 10GBASE-T links up to 100 m; follow the applicable cabling standard and installation requirements. Use unshielded cable where the environment permits, and use shielded cable only when needed and correctly bonded. Physical cabling does not replace the logical isolation and access controls between LAN zones.

For background on twisted-pair construction and shielding, see [Twisted-Pair-Kabel](https://de-academic.com/dic.nsf/dewiki/1420650).

## Authentication And Authorization Model

### User Authentication

Users authenticate through the OIDC provider.
Their identity is mapped to roles such as:

- operator
- reviewer
- admin
- workflow_observer
- workflow_executor

### Agent Authentication

Each agent receives a dedicated service identity.
Do not reuse shared tokens across multiple agents.

Each service should authenticate with:

- short-lived OIDC access token for application-level authorization
- mutual TLS certificate for transport-level service authentication

### Authorization

Authorization should happen at multiple layers:

- gateway route policy
- message-bus subject permissions
- datastore role-based access
- per-tool or per-action ALDE runtime policy

Examples:

- a parser agent may read documents and publish parse results, but may not send email
- a mail bridge may send outbound email, but may not mutate policy state
- a reviewer may approve release of a generated document, but may not access raw credential stores

## Encryption Model

### Transport Encryption

Use TLS 1.3 wherever supported.
Use mutual TLS for service-to-service traffic inside the trusted LAN as well.

### Message-Level Protection

For especially sensitive payloads, add envelope encryption or message signing on top of transport encryption.
This is relevant for:

- applicant profiles
- HR documents
- contract drafts
- regulated internal correspondence

### Key Management

Store secrets and signing material in a dedicated secret system such as Vault.
At minimum, use centrally managed encrypted secret distribution instead of file-local plaintext credentials.

## Messaging Model

### Human Messaging

Human-visible messages belong in Matrix rooms or the UI layer.
Examples:

- approval request
- agent status summary
- review needed
- workflow completed
- failure requiring operator action

### Machine Messaging

Machine-level events belong on the internal agent bus.
Representative subjects or topics:

- `planner.tasks.create`
- `worker.parse.request`
- `worker.write.request`
- `retrieval.query.request`
- `memory.session.update`
- `workflow.status.changed`
- `bridge.email.send.request`

Every message should carry:

- `correlation_id`
- `source_agent`
- `target_agent` or logical target subject
- `timestamp`
- `session_scope_key` when applicable
- idempotency key where replay is possible

## Email Integration Model

Email should not be the native internal coordination protocol.
Instead, implement email through a dedicated bridge service.

### Inbound Email

Flow:

1. Receive through SMTP or IMAP ingestion.
2. Validate sender, envelope, and attachment policy.
3. Scan for malware and normalize MIME structure.
4. Extract structured metadata and content.
5. Publish normalized events to the internal bus.

### Outbound Email

Flow:

1. Internal agent emits a send-request event.
2. Policy or approval layer verifies that the email is allowed.
3. Mail bridge renders or validates the final content.
4. Mail bridge sends via approved SMTP relay.
5. Audit record is stored with correlation and approval metadata.

This prevents parser or writer agents from sending email directly.

## Role Of AgentsDB Session Cache In A Networked Deployment

The current session cache pattern is useful in a distributed deployment because it reduces the need to rebuild working context from full history on every handoff.

Recommended role of the session cache:

- store structured short- to medium-lived working context
- scope context by agent and task slot
- avoid unbounded prompt growth
- rehydrate downstream routed jobs with only the context they need

The cache should not become a hidden source of truth for business state.
Canonical state still belongs in explicit stores.
The session cache is a runtime acceleration and continuity layer.

## Recommended ALDE Runtime Mapping

### Human Plane

- Matrix rooms
- UI operator surfaces
- approval dashboards

### Control Plane

- ALDE router or planner logic
- policy service
- workflow session coordinator
- identity provider

### Execution Plane

- `_xworker` specializations
- parser jobs
- writer jobs
- retrieval workers
- batch processors

### Data Plane

- AgentsDB
- object store
- vector store
- relational metadata store

### Bridge Plane

- email bridge
- external system adapters
- import and export services

## Example Job Lifecycle

1. A user or system submits a request through the UI, Matrix, or gateway API.
2. The gateway authenticates the caller and attaches identity and policy context.
3. The planner creates a structured execution request with a `correlation_id`.
4. The planner publishes a machine-readable job to the internal bus.
5. A worker consumes the job and loads any relevant AgentsDB session context.
6. The worker performs the task and persists structured outputs.
7. The runtime emits status updates and optional human-readable summaries.
8. A bridge service performs any approved side effects such as email.
9. Audit records remain queryable by correlation, actor, and workflow state.

## Mandatory Security Rules

1. No agent runs without its own service identity.
2. No long-lived shared tokens.
3. No unrestricted worker-to-worker direct communication.
4. No direct email capability from general-purpose workers.
5. No implicit trust between network zones.
6. No unbounded prompt history used as the only state source.
7. Every workflow action must be attributable through `correlation_id` and actor metadata.
8. Every external bridge must be isolated and policy-controlled.

## Minimum Viable Deployment Path

### Phase 1

- Keep ALDE runtime local but formalize service identities.
- Add YARP gateway in front of internal services.
- Add Matrix for human-visible collaboration.
- Keep AgentsDB session cache and structured runtime handoffs.

### Phase 2

- Introduce NATS JetStream for internal job and event transport.
- Move parser and writer execution to separate worker processes.
- Centralize policy checks and approval events.

### Phase 3

- Add isolated email bridge and external integration services.
- Introduce stronger message-level protection for sensitive payloads.
- Expand audit, replay, and operational observability.

### Phase 4

- Scale workers horizontally.
- Add richer policy enforcement and dynamic routing.
- Support tenant or domain isolation where required.

## Suggested First Implementation Slice

If this architecture is implemented incrementally, the first practical slice should be:

1. YARP gateway
2. OIDC identity provider
3. NATS JetStream internal bus
4. worker identities with mTLS
5. email bridge as a separate side-effect service
6. continued use of AgentsDB session cache for structured routed context

This gives the biggest architectural gain without forcing a full rewrite of ALDE runtime semantics.

## Open Questions For Later

- Which ALDE actions should always require human approval before side effects?
- Which payload classes require message-level encryption beyond TLS?
- Should session cache retention be time-based, count-based, or policy-based?
- Which events should be mirrored into Matrix, and which should remain machine-only?
- Does the deployment need single-tenant LAN isolation or multi-team tenancy?
- Which runtime components remain Python-native, and which are better hosted behind .NET services?

## Summary

The recommended target model is:

- Matrix for human collaboration
- NATS JetStream for agent-to-agent execution traffic
- OIDC plus mTLS for identity and trust
- YARP as the LAN ingress and policy gateway
- isolated bridge services for email and external integrations
- AgentsDB session cache as structured runtime continuity, not business truth

This keeps ALDE aligned with the current direction toward explicit routing, structured handoffs, deterministic actions, and scoped session memory while opening a clean path to a distributed agentic LAN architecture.
