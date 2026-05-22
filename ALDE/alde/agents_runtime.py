from __future__ import annotations

from textwrap import dedent
from typing import Any


def _text(value: str) -> str:
    return dedent(value).strip()


SYSTEM_PROMPT: dict[str, dict[str, Any]] = {
    "xrouter_xplanner": {
        "prompt": _text(
            """
            === Agent: xrouter_xplanner ===
            Description: Primary routing and planning agent.
            Goal: Understand the user request, close requirement gaps, select the correct job_name or tool_name, and route only schema-ready execution briefs.
            Keep it simple, short, and deterministic. 

            Rules:
            - Ask follow-up questions when the request is  missing required inputs.
            - Build a minimal explicit execution plan before delegating: goal, selected target_agent, selected job_name or tool_name, required inputs, and expected result.
            - Use direct tools only when the work is trivial, deterministic, and does not need a worker specialization.
            - If the user provides a concrete filesystem path and asks to read, open, or load it, call read_document directly instead of routing or querying memorydb or vectordb.
            - Keep xrouter_xplanner as the main orchestration agent and delegate execution to suitable sub-agents.
            - Route explicit AgentDB CRUD, lookup, relation-graph, and batch-operation requests to a suitable sub-agent (default: _xworker) with explicit job_name or tool_name.
            - Support multi-hop delegation when complex async or parallel task trees require sub-agent fanout.
            - Every route_to_agent call must include an explicit job_name or tool_name that matches the intended worker execution path.
            - For asynchronous route_to_agent calls, include run_async=true and max_agents>=1.
            - Prefer structured handoff payloads when downstream execution depends on schema-bound input.
            - Do not delegate until the brief is specific enough for deterministic execution.
            - Never invent file contents, paths, tool results, database state, or code behavior.
            """
        ),
        "task": {
            "mode": "xrouter_xplanner",
            "job_skill_profile_policy": {
                "selection_mode": "job_name",
                "fallback_skill_profile": "xplaner_xrouter_core",
            },
        },
        "output_schema": {},
    },
    "xworker": {
        "prompt": _text(
            """
            === Agent: xworker ===
            Description: Generic sub agent for all execution jobs.
            Goal: Execute the routed job or tool-focused task with the selected skill profile and return deterministic, source-grounded output.

            Rules:
            - Execute delegated jobs from planner or worker handoffs with deterministic boundaries.
            - Resolve the skill profile from tool_name first when configured, then fall back to job_name.
            - Respect explicit routed tool constraints when tools are provided as task options.
            - When the request names a concrete filesystem path to read, open, or load, use read_document; memorydb and vectordb are retrieval tools, not direct file loaders.
            - Keep outputs stable, explicit, and task-bounded.
            - Do not invent unsupported claims or runtime results.
            - Delegate further to sub-agents when async or parallel execution branches are explicitly required.
            - For asynchronous route_to_agent calls, include run_async=true and max_agents>=1.
            """
        ),
        "task": {
            "mode": "xworker",
            "tools": [],
            "job_skill_profile_policy": {
                "selection_mode": "tool_name",
                "fallback_selection_mode": "job_name",
                "fallback_skill_profile": "xworker_core",
            },
        },
        "output_schema": {},
    },
}


JOB_PROMPTS: dict[str, dict[str, Any]] = {
    
    "dispatch_documents": {
        "prompt": _text(
            """
            === Job: dispatch_documents ===
            Description: Deterministic dispatch job for job-posting PDFs and related store updates.
            Goal: Discover eligible inputs, classify them against store state, and forward only required parsing work.

            Rules:
            - Discover PDF files deterministically.
            - Prefer content_sha256 as stable identity; filename alone is not sufficient.
            - Do not forward documents that are already processed or currently queued or processing.
            - Use dispatch_documents only for filesystem scan requests that start from scan_dir.
            - Use execute_action_request or upsert_object_record when structured payloads are already available.
            - For batch processing, forward one handoff payload per document and let runtime execute one routed tool call per emitted handoff message.
            - A single broken PDF must not abort the whole run.
            - If DB access is uncertain, report UNKNOWN instead of inventing state.
            """
        ),
        "task": {
            "input_contract": {
                "required": ["scan_dir"],
                "optional": [
                    "db",
                    "db_path",
                    "obj_name",
                    "thread_id",
                    "dispatcher_message_id",
                    "recursive",
                    "extensions",
                    "max_files",
                    "action",
                    "applicant_profile",
                    "profile_result",
                    "job_posting",
                    "job_posting_result",
                    "options",
                    "cover_letter_context",
                    "source_document",
                    "agent_name",
                    "parser_agent_name",
                    "parser_job_name",
                    "dry_run",
                    "handoff_message_id",
                ],
            },
            "workflow": [
                "If the request already contains structured non-file payloads for job/profile ingestion or DB synchronization, execute the matching deterministic action tool instead of scanning directories.",
                "List files in scan_dir and filter to PDFs.",
                "Check readability and compute content_sha256, file_size_bytes, and mtime_epoch.",
                "Look up each document in the dispatcher DB and classify it as new, known_unprocessed, known_processing, known_processed, or error.",
                "Use parser_job_name for job specializations such as job_posting_parser; keep agent_name and parser_agent_name reserved for runtime agent labels such as _xworker or _xrouter_xplanner.",
                "Forward new or known_unprocessed items to the job_posting_parser job when parsing work is still required.",
                "When a generate_cover_letter-style request lands on an already processed document, resume from the stored job_posting result and route directly to cover_letter_writer when the profile input is already available.",
                "Emit one handoff message per forwarded item so runtime can fire one route_to_agent tool call per document.",
                "When parsed job data is already available and dispatcher/job-posting stores must be updated together, prefer upsert_object_record over separate store/status writes.",
                "Return a structured report with summary, forwarded items, and errors.",
            ],
        },
        "output_schema": {
            "agent": "xworker",
            "job_name": "dispatch_documents",
            "scan_dir": "/path",
            "summary": {
                "pdf_found": 0,
                "new": 0,
                "known_unprocessed": 0,
                "known_processing": 0,
                "known_processed": 0,
                "errors": 0,
            },
            "forwarded": [{"path": "/path/a.pdf", "content_sha256": "...", "link": {"thread_id": "...", "message_id": "..."}}],
            "errors": [],
        },
    },

    "applicant_profile_parser": {
        "prompt": _text(
            """
            === Job: applicant_profile_parser ===
            Description: Structured applicant profile parsing job.
            Goal: Convert CV or applicant-profile input into a reusable, storage-ready JSON profile.

            Rules:
            - Be strictly source-grounded.
            - Generate a stable profile_id from email when email is present.
            - If email is missing, set profile_id to null and add missing_email_for_profile_id to warnings.
            - On re-parse, overwrite only values clearly present in the new source.
            - Do not downgrade populated values to null unless the source explicitly requests removal.
            """
        ),
        "task": {
            "specialization": "applicant_profile",
            "input_contract": {
                "variants": ["applicant_profile_text", "applicant_profile_file"],
                "correlation_id_fallback": "file.content_sha256 or null",
            },
            "extraction_guidance": [
                "Keep date and duration formats source-faithful when normalization is ambiguous.",
                "Deduplicate skills.",
                "Include language levels only when explicitly stated.",
                "Use empty lists instead of placeholder rows when nothing can be extracted.",
            ],
        },
        "output_schema": {
            "agent": "xworker",
            "job_name": "applicant_profile_parser",
            "correlation_id": None,
            "parse": {"language": "de", "extraction_quality": "high", "errors": [], "warnings": []},
            "profile": {
                "profile_id": "profile:<sha256(email)>",
                "personal_info": {
                    "full_name": None,
                    "date_of_birth": None,
                    "citizenship": None,
                    "address": None,
                    "phone": None,
                    "email": None,
                    "linkedin": None,
                    "portfolio": None,
                },
                "professional_summary": "",
                "experience": [],
                "education": [],
                "skills": {"technical": [], "soft": [], "languages": []},
                "certifications": [],
                "projects": [],
                "preferences": {"tone": "modern", "max_length": 350, "language": "de", "focus_areas": []},
                "additional_information": {"travel_willingness": None, "work_authorization": None, "marital_status": None},
            },
        },
    },
    "job_posting_parser": {
        "prompt": _text(
            """
            === Job: job_posting_parser ===
            Description: Structured job-posting extraction and latest-selection job.
            Goal: Convert dispatcher payloads and batch posting lists into a knowledge-ready JSON representation with raw-text, entity, and relational projections.

            Rules:
            - Be strictly source-grounded; do not invent facts, fields, or scores.
            - Determine whether each source is actually a job posting.
            - Support both single dispatcher payload mode and latest batch mode.
            - In latest batch mode, deduplicate deterministically by source_id, then source_url, then content_sha256.
            - In latest batch mode, sort by posting_date descending, then fetched_at descending, then source_id ascending.
            - Populate db_updates only as the desired state transition.
            - Keep salaries in the original currency and preserve source formatting when normalization is ambiguous.
            - Preserve the original extracted source in raw_text_document.raw_text whenever available.
            - Emit entity_objects and relation_objects only when the source provides evidence.
            - You may encode an evidence-backed relation either in relation_objects or directly on the target entity via is_target, source_entity, is_relational, and explicit_description.
            - Keep job_posting as a flattened compatibility projection for existing storage and downstream consumers.
            - If parse.is_job_posting is false, set db_updates.processing_state to failed and db_updates.processed to false.
            - Return JSON only.
            """
        ),
        "task": {
            "specialization": "job_posting",
            "input_contract": {
                "type": "job_posting_pdf",
                "required": ["correlation_id", "link", "file", "db", "requested_actions"],
                "optional": ["items", "latest_limit"],
                "variants": [
                    {
                        "name": "single_dispatch_payload",
                        "required": ["correlation_id", "link", "file", "db", "requested_actions"],
                    },
                    {
                        "name": "latest_job_postings_batch",
                        "required": ["items"],
                        "optional": ["latest_limit"],
                        "latest_limit_default": 10,
                        "deduplication_order": ["source_id", "source_url", "content_sha256"],
                        "sort_order": ["posting_date_desc", "fetched_at_desc", "source_id_asc"],
                    },
                ],
                "missing_field_policy": "Mirror missing fields as null and report them in parse.errors.",
            },
            "extraction_guidance": [
                "Use YYYY-MM-DD only when a date is unambiguous.",
                "Deduplicate ordered lists with most important items first.",
                "Put the full extracted text into raw_text_document.raw_text and mirror it into job_posting.raw_text when available.",
                "Represent the posting as a primary subject entity in entity_objects, usually with entity_key 'subject'.",
                "Use stable, reusable entity keys so relation_objects or target-annotated entity_objects can reference them deterministically.",
                "If you already know the target entity, you may encode the relation on that entity with is_target=true, source_entity='<seed_key>', is_relational='<relation_type>', and explicit_description when a human-readable explanation helps.",
                "Emit only evidence-backed relation types such as offered_by, located_in, requires_skill, requires_language, or application_contact.",
                "For latest batch mode, keep only the newest postings after deterministic deduplication and sorting.",
                "For latest batch mode, include dropped duplicates and non-job-posting items in warnings or dropped_items metadata.",
                "If the source is not a job posting, keep the schema stable and mark db_updates as failed.",
            ],
            "batch_output_schema": {
                "summary": {
                    "total_input": 0,
                    "valid_postings": 0,
                    "returned_latest": 0,
                    "dropped_duplicates": 0,
                    "dropped_non_job_postings": 0,
                },
                "latest_job_postings": [],
                "dropped_items": [],
                "errors": [],
                "warnings": [],
            },
        },
        "output_schema": {
            "agent": "xworker",
            "job_name": "job_posting_parser",
            "correlation_id": "<content_sha256>",
            "link": {"thread_id": "...", "message_id": "..."},
            "file": {"path": "...", "name": "...", "content_sha256": "..."},
            "parse": {"is_job_posting": True, "language": "de", "extraction_quality": "high", "errors": [], "warnings": []},
            "raw_text_document": {
                "document_type": "job_posting",
                "title": None,
                "language": "de",
                "raw_text": "",
                "sections": [
                    {
                        "section_key": "header",
                        "heading": "Object Header",
                        "text": "",
                        "metadata": {},
                    }
                ],
                "metadata": {"content_sha256": None, "source": None},
            },
            "entity_objects": [
                {
                    "entity_key": "subject",
                    "entity_type": "job_posting",
                    "canonical_name": None,
                    "mention_text": None,
                    "summary": None,
                    "confidence": 0.99,
                    "aliases": [],
                    "attributes": {},
                    "metadata": {"role": "subject", "source_field": "job_posting.job_title"},
                },
                {
                    "entity_key": "organization:example_gmbh",
                    "entity_type": "organization",
                    "canonical_name": None,
                    "mention_text": None,
                    "summary": None,
                    "confidence": 0.95,
                    "aliases": [],
                    "attributes": {},
                    "is_target": True,
                    "source_entity": "subject",
                    "is_relational": "offered_by",
                    "explicit_description": "Employer named in the posting header.",
                    "metadata": {"source_field": "job_posting.company_name"},
                }
            ],
            "relation_objects": [
                {
                    "source_entity_key": "subject",
                    "target_entity_key": "organization:example_gmbh",
                    "relation_type": "offered_by",
                    "section_key": "header",
                    "confidence": 0.95,
                    "metadata": {"source_field": "job_posting.company_name"},
                }
            ],
            "job_posting": {
                "job_title": None,
                "company_name": None,
                "company_info": {"industry": None, "size": None, "location": None, "website": None},
                "position": {"type": None, "level": None, "department": None, "reports_to": None},
                "location_details": {"office": None, "remote": None, "travel_required": None},
                "compensation": {"salary_min": None, "salary_max": None, "salary_period": None, "currency": None, "benefits": []},
                "requirements": {
                    "education": None,
                    "experience_years": None,
                    "experience_description": None,
                    "technical_skills": [],
                    "soft_skills": [],
                    "languages": [],
                },
                "responsibilities": [],
                "what_we_offer": [],
                "application": {"deadline": None, "application_link": None, "contact_email": None, "contact_person": None},
                "metadata": {"posting_date": None, "job_id": None, "source": None, "language": None},
                "raw_text": "",
            },
            "db_updates": {
                "existing_record_id": None,
                "correlation_id": "<content_sha256>",
                "content_sha256": "...",
                "processing_state": "processed",
                "processed": True,
                "failed_reason": None,
            },
        },
    },
    "cover_letter_writer": {
        "prompt": _text(
            """
            === Job: cover_letter_writer ===
            Description: Structured application-package writing job.
            Goal: Produce a tailored two-page package from structured job-posting and applicant-profile inputs.

            Rules:
            - Use only facts present in job_posting_result and profile_result.
            - If recipient or contact details are missing, use neutral wording.
            - Match requirements only when there is explicit evidence in the profile.
            - If a required skill is missing, do not invent it; record it in quality.red_flags.
            - Build exactly two pages: page 1 = application, page 2 = CV tailored to the job offer.
            - Provide page_content for both pages and page-level metadata (content_sha, title/titel, signature, page).
            - Return JSON only.
            """
        ),
        "task": {
            "specialization": "cover_letter",
            "input_contract": {
                "required": ["job_posting_result", "profile_result", "options"],
                "option_fallback": "Use profile_result.profile.preferences when options values are missing.",
            },
            "writing_guidance": [
                "Use active, specific language.",
                "Respect options.language, options.tone, and options.max_words.",
                "Prefer evidence-backed statements over generic enthusiasm.",
                "Use neutral wording when structured input is incomplete.",
            ],
        },
        "output_schema": {
            "agent": "xworker",
            "job_name": "cover_letter_writer",
            "correlation": {
                "job_posting_correlation_id": "...",
                "profile_correlation_id": "...",
                "correlation_id": "...",
            },
            "cover_letter": {
                "header": {
                    "sender": "<mehrzeilig oder leer>",
                    "recipient": "<mehrzeilig oder leer>",
                    "date": "<Ort, YYYY-MM-DD oder leer>",
                    "subject": "<Betreff>",
                },
                "salutation": "<Anrede>",
                "body": {
                    "opening": "...",
                    "main_paragraph_1": "...",
                    "main_paragraph_2": "...",
                    "main_paragraph_3": "...",
                    "closing": "...",
                },
                "signature": "<closing + name>",
                "enclosures": ["Lebenslauf", "Zeugnisse"],
                "full_text": "<full cover letter as continuous text>",
            },
            "cv": {
                "target_role": "<job title + company or empty>",
                "summary": "<job-tailored profile summary>",
                "sections": {
                    "job_fit": [],
                    "skills": [],
                    "experience": [],
                    "education": [],
                    "languages": [],
                },
                "signature": "<candidate name>",
                "full_text": "<full CV as continuous text>",
            },
            "pages": [
                {
                    "page": 1,
                    "title": "Application",
                    "titel": "Application",
                    "signature": "...",
                    "page_content": "...",
                    "content_sha": "...",
                    "metadata": {
                        "page": 1,
                        "title": "Application",
                        "titel": "Application",
                        "signature": "...",
                        "content_sha": "...",
                    },
                },
                {
                    "page": 2,
                    "title": "CV",
                    "titel": "CV",
                    "signature": "...",
                    "page_content": "...",
                    "content_sha": "...",
                    "metadata": {
                        "page": 2,
                        "title": "CV",
                        "titel": "CV",
                        "signature": "...",
                        "content_sha": "...",
                    },
                },
            ],
            "page_count": 2,
            "document": {
                "full_text": "<application + pagebreak + cv>",
                "page_break_marker": "<!-- pagebreak -->",
            },
            "quality": {
                "word_count": 0,
                "tone_used": "modern",
                "language": "de",
                "matched_requirements": [],
                "highlighted_skills": [],
                "red_flags": [],
            },
        },
    },
    "router_planner_cover_letter_sequence": {
        "prompt": _text(
            """
            === Job: Xrouter_Xplanner for cover_letter_generation_sequence ===
            Description: Specialized router planner for the deterministic dispatch -> parse -> cover-letter writer pipeline.
            Goal: Build exactly one initialization payload that starts the cover-letter sequence from applicant-profile or profile_id plus job-posting inputs.

            Required behavior:
            - Produce one deterministic handoff for job_name=document_dispatch and target_agent=_xworker.
            - Treat the request as sequence initialization only, not as direct writing or parsing execution.
            - Accept the minimal initialization call with profile_id plus job_posting.
            - When only profile_id is present, emit applicant_profile={source: profile_id, value: <profile_id>} inside the sequence payload.
            - Preserve applicant_profile, job_posting, job_posting_result, cover_letter_context, source_document, and options unchanged when they are present.
            - Set action=generate_cover_letter only when action is missing or empty.
            - Keep sequence metadata explicit and consistent: sequence_name, parser_job_name, writer_job_name.
            - Use the canonical sequence identity dispatch_parse_generate_cover_letter.

            Constraints:
            - Do not invent applicant facts, job requirements, attachments, filesystem paths, database state, or execution results.
            - Do not rename payload fields unless required by the documented output schema.
            - Do not emit multiple routes, alternatives, commentary, or free-text explanation.
            - Return only the structured route initialization payload for the sequence.
            """
        ),

        "task": {
            "mode": "sequence_router_planner",
            "defaults": {
                "target_agent": "_xworker",
                "job_name": "document_dispatch",
            },
            "sequence_name": "dispatch_parse_generate_cover_letter",
            "available_jobs": [
                "document_dispatch",
                "job_posting_parser",
                "cover_letter_writer",
            ],
        
 
              
              
        },

        "output_schema": {
            "target_agent": "_xworker",
            "job_name": "document_dispatch",
            "user_question": "{\"action\": \"generate_cover_letter\", \"profile_id\": \"profile:123\", \"job_posting\": {\"job_title\": \"AI Engineer\"}}",

            "handoff_metadata": {
                "sequence_name": "dispatch_parse_generate_cover_letter",
                "parser_job_name": "job_posting_parser",
                "writer_job_name": "cover_letter_writer",
            },
        },
    },
    "adb_operation": {
        "prompt": _text(
            """
            === Job: adb_operation ===
            Description: Deterministic generic AgentDB execution job.
            Goal: Execute one explicit AgentDB repository operation and return the structured result unchanged.

            Rules:
            - Use only the adb_operation tool for this job.
            - Keep the operation explicit: health, ensure_index_objects, upsert_object, delete_object, load_object, load_objects, find_objects, load_relation_graph, or apply_operations.
            - Pass only the fields required by the selected operation.
            - Use object_name and object_id exactly as provided; do not invent identifiers or payload fields.
            - Preserve batch operation ordering when apply_operations is requested.
            - Return the tool result payload unchanged.
            - Do not invent repository state, object payloads, or graph edges.
            """
        ),
        "task": {
            "mode": "tool_execution",
            "tool_name": "adb_operation",
        },
        "output_schema": {
            "ok": True,
            "operation": "health|ensure_index_objects|upsert_object|delete_object|load_object|load_objects|find_objects|load_relation_graph|apply_operations",
            "repository": {
                "repository_type": "AgentDbInMemoryRepository|AgentDbSocketRepository|KnowledgeRepository",
                "database_name": "alde_knowledge",
            },
            "object_payload": {},
            "object_payload_list": [],
            "deleted": False,
            "applied": 0,
            "results": [],
        },
    },
    "adb_worker": {
        "prompt": _text(
            """
            === Job: adb_worker ===
            Description: Deterministic repository indexing execution job.
            Goal: Run the adb_worker tool with explicit parameters and return its result unchanged.

            Rules:
            - Use only the adb_worker tool for this job.
                        - Keep operation explicit: scan, build, cleanup, delete, rebuild, status, or repair_namespace.
                        - Use repair_namespace to cleanup wrong namespace writes and rebuild in one deterministic run.
                        - For long runs prefer run_async=true and poll with operation=status + job_id.
                        - Pass root_dir, extensions, workers, image_path, cleanup_namespace_ids, cleanup_object_names,
                            cleanup_owner_prefixes, cleanup_before_build, delete_async, run_async, and job_id only when provided.
            - Return the tool result payload unchanged.
            - Do not invent repository state, indexing metrics, or errors.
            """
        ),
        "task": {
            "mode": "tool_execution",
            "tool_name": "adb_worker",
        },
        "output_schema": {
            "ok": True,
            "operation": "scan|build|cleanup|delete|rebuild|status|repair_namespace",
            "async": False,
            "job_id": None,
            "status": "queued|running|completed|failed",
            "files_found": 0,
            "total_blocks": 0,
            "total_entities": 0,
            "total_relations": 0,
            "total_embedded": 0,
            "cleanup": {
                "deleted": 0,
                "candidates": 0,
                "namespace_ids": [],
                "object_names": [],
                "async_delete": True,
            },
            "errors": [],
        },
    },
    "adb_query": {
        "prompt": _text(
            """
            === Job: adb_query ===
            Description: Deterministic repository retrieval execution job.
            Goal: Run the adb_query tool and return context chunks for downstream reasoning.

            Rules:
            - Use only the adb_query tool for this job.
            - Require a non-empty query.
            - Pass owner_types, limit, namespace_id, image_path, and use_vector only when provided.
            - Return the tool result payload unchanged.
            - Do not invent matches, scores, or source paths.
            """
        ),
        "task": {
            "mode": "tool_execution",
            "tool_name": "adb_query",
        },
        "output_schema": {
            "ok": True,
            "query": "...",
            "namespace_id": "ns_repo_knowledge",
            "owner_types": ["block", "entity"],
            "used_vector_search": True,
            "total": 0,
            "chunks": [],
        },
    },
    "router_planner_repo_knowledge_async": {
        "prompt": _text(
            """
            === Job: Xrouter_Xplanner for repo_knowledge_async ===
            Description: Specialized router planner forns_repo asynchronous repo-knowledge fanout to xworker.
            Goal: Emit deterministic route_to_agent branches for adb_worker and adb_query execution.

            Rules:
            - Route only to _xworker.
            - Use explicit job_name per branch: adb_worker or adb_query.
            - Keep branch payloads source-grounded and deterministic.
            - For asynchronous branch execution, set run_async=true and explicit max_agents (default 4) on every route_to_agent branch.
            - Do not invent tool outputs.
            """
        ),
        "task": {
            "mode": "parallel_router_planner",
            "defaults": {
                "target_agent": "_xworker",
            },
            "parallel": {
                "enabled": True,
                "workers": 4,
                "mode": "router_parallel_branches",
            },
            "available_jobs": [
                "adb_worker",
                "adb_query",
            ],
        },
        "output_schema": {
            "branches": [
                {
                    "target_agent": "_xworker",
                    "job_name": "adb_worker",
                    "user_question": "{...tool payload json...}",
                    "run_async": True,
                    "max_agents": 4,
                },
                {
                    "target_agent": "_xworker",
                    "job_name": "adb_query",
                    "user_question": "{...tool payload json...}",
                    "run_async": True,
                    "max_agents": 4,
                },
            ],
            "parallel": {
                "enabled": True,
                "workers": 4,
            },
        },
    },
   
    "mail_agent_runtime": {
        "prompt": _text(
            """
            === Job: mail_agent_runtime ===
            Description: Runtime bridge job for the standalone Projekt_Mail_Agent process.
            Goal: Start the external mail agent in deterministic once-mode or background watch-mode via run_mail_agent.

            Rules:
            - Prefer mode=once for bounded execution.
            - Use mode=watch only when the user explicitly asks for continuous inbox watching.
            - Return the run_mail_agent result payload unchanged.
            - Do not invent IMAP/SMTP/Drive status.
            """
        ),
        "task": {
            "mode": "mail_agent_runtime",
            "action_tool": "run_mail_agent",
        },
        "output_schema": {
            "status": "ok|error|started",
            "mode": "once|watch",
            "exit_code": 0,
            "project_dir": "...",
            "python": "...",
            "stdout": "...",
            "stderr": "...",
            "pid": 12345,
        },
    },
  
}


JOB_CONFIGS: dict[str, dict[str, Any]] = {
 
 
    "generic_execution": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_core",
        "default_object_name": "documents",
        "is_default_for_agent": True,
    },
    "adb_operation": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_core",
        "default_object_name": "agents_db",
        "workflow_name": "xworker_adb_operation_leaf",
        "default_tool_names": ["adb_operation"],
    },
    "agent_relation_graph_analysis": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_core",
        "default_object_name": "agents_db_graph",
        "default_tool_names": ["agent_relation_graph"],
    },
    "adb_worker": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_core",
        "default_object_name": "repo_knowledge",
        "workflow_name": "xworker_adb_worker_leaf",
        "default_tool_names": ["repo_knowledge_worker"],
    },
    "adb_query": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_core",
        "default_object_name": "repo_knowledge",
        "workflow_name": "xworker_adb_query_leaf",
        "default_tool_names": ["repo_knowledge_query"],
    },
    "router_planner_repo_knowledge_async": {
        "runtime_agent": "_xrouter_xplanner",
        "skill_profile": "xrouter_repo_knowledge_async_planner",
        "default_object_name": "repo_knowledge",
        "workflow_name": "xrouter_repo_knowledge_async_router",
        "route_defaults": {
            "target_agent": "_xworker",
            "handoff_metadata": {
                "workflow_name": "xrouter_repo_knowledge_async_router",
                "parallel_mode": "router_parallel_branches",
                "parallel_workers": 4,
                "run_async": True,
                "max_agents": 4,
                "parallel_enabled_env": "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED",
                "parallel_workers_env": "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS",
            },
        },
    },
    "document_dispatch": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_dispatch",
        "default_object_name": "documents",
    },
    "document_dispatch_ingest_import_pipeline": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_dispatch_ingest_import_pipeline",
        "default_object_name": "documents",
    },
    "generic_parser": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_generic_parser",
        "default_object_name": "documents",
        "default_tool_names": ["read_document", "pypdf_read_document", "list_documents"],
    },
    "applicant_profile_parser": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_profile_parser",
        "default_object_name": "profiles",
        "workflow_name": "xworker_profile_parser_leaf",
        "default_tool_names": ["read_document", "pypdf_read_document", "list_documents"],
        "specialized_prompt": {"agent_type": "parser", "task_name": "applicant_profile"},
    },
    "job_posting_parser": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_job_posting_parser",
        "default_object_name": "job_postings",
        "workflow_name": "xworker_job_posting_parser_leaf",
        "default_tool_names": ["read_document", "pypdf_read_document", "list_documents"],
        "specialized_prompt": {"agent_type": "parser", "task_name": "job_posting"},
    },
    "generic_writer": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_generic_writer",
        "default_object_name": "documents",
    },
    "cover_letter_writer": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_cover_letter_writer",
        "default_object_name": "cover_letters",
        "disable_runtime_tools": True,
        "specialized_prompt": {"agent_type": "writer", "task_name": "cover_letter"},
    },
    "router_planner_cover_letter_sequence": {
        "runtime_agent": "_xrouter_xplanner",
        "skill_profile": "xrouter_cover_letter_sequence_planner",
        "default_object_name": "documents",
        "is_default_for_agent": True,
        "route_defaults": {
            "target_agent": "_xworker",
            "job_name": "document_dispatch",
            "handoff_metadata": {
                "sequence_name": "dispatch_parse_generate_cover_letter",
                "parser_job_name": "job_posting_parser",
                "writer_job_name": "cover_letter_writer",
            },
            "sequence_payload": {
                "action": "generate_cover_letter",
                "sequence": {
                    "name": "dispatch_parse_generate_cover_letter",
                    "steps": [
                        "dispatch_document",
                        "job_posting_parser",
                        "cover_letter_writer",
                    ],
                    "parser_job_name": "job_posting_parser",
                    "writer_job_name": "cover_letter_writer",
                },
            },
        },
    },
  
    "mail_agent_runtime": {
        "runtime_agent": "_xworker",
        "skill_profile": "xworker_mail_agent_runtime",
        "default_object_name": "emails",
    },
  
}


def _default_job_name_for_agent(agent_label: str) -> str:
    for job_name, config in JOB_CONFIGS.items():
        if str(config.get("runtime_agent") or "").strip() != str(agent_label or "").strip():
            continue
        if bool(config.get("is_default_for_agent")):
            return job_name
    return ""


def _job_skill_profiles_for_agent(agent_label: str) -> dict[str, str]:
    return {
        job_name: str(config.get("skill_profile") or "")
        for job_name, config in JOB_CONFIGS.items()
        if str(config.get("runtime_agent") or "").strip() == str(agent_label or "").strip()
        and str(config.get("skill_profile") or "").strip()
    }


def _tool_skill_profiles_for_agent(agent_label: str) -> dict[str, str]:
    if str(agent_label or "").strip() != "_xworker":
        return {}

    return {
      
        "execute_action_request": "xworker_dispatch",
        "upsert_object_record": "xworker_dispatch",
        "ingest_object": "xworker_dispatch",
        "store_object_result": "xworker_dispatch",
        "adb_operation": "xworker_core",
        "agent_relation_graph": "xworker_core",
        "run_mail_agent": "xworker_mail_agent_runtime",
        "vdb_worker": "xworker_core",
        "repo_knowledge_worker": "xworker_core",
        "repo_knowledge_query": "xworker_core",
        "dispatch_documents": "xworker_dispatch",
        "read_document": "xworker_core",
        "list_documents": "xworker_core",
        "write_document": "xworker_generic_writer",
        "update_document": "xworker_core",
        "delete_document": "xworker_core",
        "md_to_pdf": "xworker_generic_writer",
    }


SPECIALIZED_JOB_PROMPT_MAP: dict[tuple[str, str], str] = {
    (str(prompt_ref.get("agent_type") or "").strip(), str(prompt_ref.get("task_name") or "").strip()): job_name
    for job_name, config in JOB_CONFIGS.items()
    for prompt_ref in [config.get("specialized_prompt") or {}]
    if isinstance(prompt_ref, dict)
    and str(prompt_ref.get("agent_type") or "").strip()
    and str(prompt_ref.get("task_name") or "").strip()
}


LEGACY_AGENT_NAME_MAP: dict[str, str] = {
    # Legacy typo alias kept for backward compatibility.
    "_xplaner_xrouter": "xrouter_xplanner",
    "_xrouter_xplanner": "xrouter_xplanner",
    "_xworker": "xworker",
}

CANONICAL_AGENT_LABEL_MAP: dict[str, str] = {
    "xrouter_xplanner": "_xrouter_xplanner",
    "xplaner_xrouter": "_xrouter_xplanner",
    "xworker": "_xworker",
}

# Backward-compatible aliases consumed by agents_config imports.
_SPECIALIZED_JOB_PROMPT_MAP = SPECIALIZED_JOB_PROMPT_MAP
_LEGACY_AGENT_NAME_MAP = LEGACY_AGENT_NAME_MAP
_CANONICAL_AGENT_LABEL_MAP = CANONICAL_AGENT_LABEL_MAP


AGENT_RUNTIME: dict[str, dict[str, Any]] = {
    "_xrouter_xplanner": {
        "canonical_name": "xrouter_xplanner",
        "model": "gpt-4o",
        "tools": [ "route_to_agent", "execute_action_request", "upsert_object_record", "@dispatcher", "@doc_rw"],
        "defaults": {
            "job_name": _default_job_name_for_agent("_xrouter_xplanner"),
            "skill": "",
            "profile": "xplaner_xrouter_core",
        },
        "workflow": {"definition": "xplaner_xrouter_router"},
    },
    "_xworker": {
        "canonical_name": "xworker",
        "model": "gpt-4o-mini",
        "tools": [
            "route_to_agent",
            "execute_action_request",
            "upsert_object_record",
            "ingest_object",
            "store_object_result",
            "adb_operation",
            "agent_relation_graph",
            "run_mail_agent",
            "vdb_worker",
            "repo_knowledge_worker",
            "repo_knowledge_query",
            "@dispatcher",
            "@doc_ro",
            "@doc_rw",
        ],
        "defaults": {
            "job_name": _default_job_name_for_agent("_xworker"),
            "skill": "",
            "profile": "xworker_core",
        },
        "workflow": {"definition": "xworker_leaf"},
    },
}


AGENT_ROLE: dict[str, dict[str, Any]] = {
    "xrouter_xplanner": {
        "description": "Primary agent role for planning, clarification, and routing.",
        "can_route": True,
        "default_instance_policy": "session_scoped",
        "default_tool_policy": "xrouter_xplanner",
        "default_handoff_policy": {
            "default_protocol": "agent_handoff_v1",
            "accepted_protocols": ["message_text", "agent_handoff_v1"],
            "emitted_protocols": ["message_text", "agent_handoff_v1"],
            "allowed_targets": [],
            "allowed_sources": [],
            "target_policies": {},
            "source_policies": {},
        },
        "default_history_policy": {
            "followup_history_depth": 15,
            "include_routed_history": True,
            "routed_history_depth": 12,
        },
    },
    "xworker": {
        "description": "Execution role for routed worker jobs with optional sub-agent delegation.",
        "can_route": True,
        "default_instance_policy": "ephemeral",
        "default_tool_policy": "xworker",
        "default_handoff_policy": {
            "default_protocol": "agent_handoff_v1",
            "accepted_protocols": ["message_text", "agent_handoff_v1"],
            "emitted_protocols": ["message_text", "agent_handoff_v1"],
            "allowed_targets": [],
            "allowed_sources": [],
            "target_policies": {},
            "source_policies": {},
        },
        "default_history_policy": {
            "followup_history_depth": 8,
            "include_routed_history": False,
            "routed_history_depth": 0,
        },
    },
}


ALLOWED_INSTANCE_POLICIES = {
    "ephemeral",
    "session_scoped",
    "workflow_scoped",
    "service_scoped",
}







HANDOFF_PROTOCOL: dict[str, dict[str, Any]] = {
    "message_text": {
        "description": "Plain text handoff transported as the routed user message.",
        "transport": "user_message",
        "mode": "text",
    },
    "agent_handoff_v1": {
        "description": "Structured handoff envelope for agent-to-agent communication.",
        "transport": "user_message",
        "mode": "json_envelope",
        "required_payload_keys": ["agent_label", "handoff_to"],
        "content_keys": ["output", "generated", "msg"],
    },
}


HANDOFF_SCHEMA: dict[str, dict[str, Any]] = {
    "xrouter_to_xworker": {
        "handoff_id": "structured",
        "protocol": "agent_handoff_v1",
        "description": "Generic xrouter_xplanner to xworker structured handoff.",
        "required_payload_any": ["output", "generated", "msg"],
        "preferred_payload_paths": ["output", "generated", "msg"],
        "workflow_name": "xworker_leaf",
        "instructions": [
            "Treat the handoff payload as the authoritative execution brief.",
            "Load the selected job-specific skill profile before acting.",
        ],
        "variants": {
            "document_dispatch": {
                "handoff_id": "dispatch_request",
                "job_name": "document_dispatch",
                "protocol": "message_text",
                "description": "xrouter_xplanner request for the deterministic dispatch workflow.",
                "required_message_text": True,
                "workflow_name": "xworker_documents_dispatch_chain",
                "instructions": [
                    "Treat the routed user message as the dispatch request.",
                    "Execute the document_dispatch job deterministically and do not invent filesystem or DB state.",
                ],
            },
        
            "generic_parser": {
                "handoff_id": "parser_brief",
                "description": "xrouter_xplanner brief for a parser-style xworker job.",
                "job_names": [
                    "generic_parser",
                    "applicant_profile_parser",
                    "job_posting_parser",
                ],
                "workflow_name": "xworker_generic_parser_leaf",
                "result_postprocess": {
                    "tool": "store_object_result",
                    "source_agent": "target_agent",
                },
                "instructions": [
                    "Treat the handoff payload as the primary parser input.",
                    "Keep extraction source-grounded and preserve schema stability.",
                    "Return the structured parser JSON so runtime persistence can store the parsed object result deterministically.",
                ],
            },
            "generic_writer": {
                "handoff_id": "writer_brief",
                "description": "xrouter_xplanner brief for a writer-style xworker job.",
                "job_names": [
                    "generic_writer",
                    "cover_letter_writer",
                ],
                "workflow_name": "xworker_generic_writer_leaf",
                "instructions": [
                    "Use the handoff payload as the writing brief.",
                    "Do not add unsupported claims beyond the provided structured input.",
                ],
            },
        
            "cover_letter_writer": {
                "handoff_id": "cover_letter_writer",
                "job_name": "cover_letter_writer",
                "description": "xrouter_xplanner brief for the cover-letter writer job with deterministic artifact persistence.",
                "workflow_name": "xworker_cover_letter_writer_leaf",
                "result_postprocess": {
                    "tool": "persist_cover_letter_artifacts",
                    "text_writer_tool": "write_document",
                    "pdf_writer_tool": "md_to_pdf",
                    "default_write_pdf": True,
                },
                "instructions": [
                    "Use the handoff payload as the writing brief.",
                    "Do not add unsupported claims beyond the provided structured input.",
                    "Return the structured cover-letter JSON so runtime persistence can write markdown and PDF artifacts.",
                ],
            },
        },
    },
    "xworker_to_xworker": {
        "handoff_id": "structured",
        "protocol": "agent_handoff_v1",
        "description": "Generic internal xworker handoff for chained worker jobs.",
        "required_payload_any": ["output", "generated", "msg"],
        "preferred_payload_paths": ["output", "generated", "msg"],
        "workflow_name": "xworker_leaf",
        "instructions": [
            "Treat the handoff payload as the next worker-stage brief.",
            "Preserve correlation, job_name, and source-grounded inputs.",
        ],
        "variants": {
            "job_posting_parser": {
                "handoff_id": "job_posting_parser",
                "job_name": "job_posting_parser",
                "description": "Internal xworker handoff for the job-posting parser job.",
                "required_payload_paths": [
                    "output.type",
                    "output.correlation_id",
                    "output.link.thread_id",
                    "output.file.path",
                    "output.file.content_sha256",
                    "output.db.processing_state",
                    "output.requested_actions",
                ],
                "required_metadata_paths": ["correlation_id", "dispatcher_message_id", "dispatcher_db_path", "obj_name", "obj_db_path"],
                "preferred_payload_paths": ["output", "msg"],
                "target_input_path": "output",
                "workflow_name": "xworker_job_posting_parser_leaf",
                "result_postprocess": {
                    "tool": "upsert_object_record",
                    "source_agent": "target_agent",
                },
                "instructions": [
                    "Treat output as the authoritative dispatch payload.",
                    "Use metadata.correlation_id to preserve workflow linkage.",
                ],
            },
            "cover_letter_writer": {
                "handoff_id": "cover_letter_writer",
                "job_name": "cover_letter_writer",
                "description": "Internal xworker handoff for the cover-letter writer job with deterministic artifact persistence.",
                "preferred_payload_paths": ["output", "msg"],
                "target_input_path": "output",
                "workflow_name": "xworker_cover_letter_writer_leaf",
                "result_postprocess": {
                    "tool": "persist_cover_letter_artifacts",
                    "text_writer_tool": "write_document",
                    "pdf_writer_tool": "md_to_pdf",
                    "default_write_pdf": True,
                },
                "instructions": [
                    "Treat output as the authoritative writer brief.",
                    "Use metadata.correlation_id to preserve workflow linkage.",
                    "Return the structured cover-letter JSON so runtime persistence can write markdown and PDF artifacts.",
                ],
            },
        },
    },
}


ACTIONS: dict[str, dict[str, Any]] = {
 
    "dispatch_documents": {
        "description": "Deterministic document dispatch request that scans a directory for new job-offer PDFs and updates dispatcher state.",
        "actions": ["dispatch_documents", "document_dispatch"],
        "required_paths": ["action", "scan_dir"],
        "recommended_paths": [
            "db_path",
            "dispatcher_db_path",
            "thread_id",
            "dispatcher_message_id",
            "recursive",
            "extensions",
            "agent_name",
            "parser_job_name",
        ],
        "conditions": {
            "all": [
                {"action": {"in": ["dispatch_documents", "document_dispatch"]}},
                {"scan_dir": {"exists": True}},
            ]
        },
        "action_execution": {
            "handler_name": "dispatch_documents",
        },
    },

    "platform_job_posting_ingest_request": {
        "description": "Deterministic non-PDF ingest request for job postings from platforms, APIs, or pre-parsed sources.",
        "actions": ["ingest_object", "store_object_result"],
        "required_paths": ["action"],
        "conditions": {
            "all": [
                {"action": {"in": ["ingest_object", "store_object_result"]}},
                {
                    "any": [
                        {"job_posting_result": {"exists": True}},
                        {"job_posting": {"exists": True}},
                    ]
                },
            ]
        },
        "recommended_paths": ["correlation_id", "source_agent", "source_payload"],
        "request_resolution": {
            "objects": [
                {
                    "binding_name": "job_posting",
                    "request_field": "job_posting",
                    "result_field": "job_posting_result",
                    "default_obj_name": "job_postings",
                    "obj_name_config_key": "job_posting_obj_name",
                    "db_path_field_key": "job_posting_db_path_field",
                    "default_source": "text",
                }
            ],
        },
        "action_execution": {
            "handler_name": "ingest_object",
            "binding_name": "job_posting",
            "object_payload_field": "job_posting",
            "request_payload_field": "job_posting",
            "result_payload_field": "job_posting_result",
            "correlation_id_fields": ["correlation_id"],
            "db_path_fields": ["obj_db_path", "job_postings_db_path", "db_path"],
            "source_agent_fields": ["source_agent"],
            "source_payload_fields": ["source_payload"],
            "parse_fields": ["parse"],
            "default_request_source": "text",
        },
    },
   
  
    "dispatcher_job_record_upsert_request": {
        "description": "Deterministic combined request that updates both job_postings_db and dispatcher_doc_db for the same correlation id.",
        "actions": ["upsert_object_record"],
        "required_paths": ["action", "dispatcher_db_path", "obj_db_path"],
        "conditions": {
            "all": [
                {"action": {"in": ["upsert_object_record"]}},
                {
                    "any": [
                        {"job_posting_result": {"exists": True}},
                        {"job_posting": {"exists": True}},
                    ]
                },
            ]
        },
        "recommended_paths": ["correlation_id", "processing_state", "source_agent"],
        "request_resolution": {
            "objects": [
                {
                    "binding_name": "job_posting",
                    "request_field": "job_posting",
                    "result_field": "job_posting_result",
                    "default_obj_name": "job_postings",
                    "obj_name_config_key": "job_posting_obj_name",
                    "db_path_field_key": "job_posting_db_path_field",
                    "default_source": "text",
                }
            ],
        },
        "action_execution": {
            "handler_name": "upsert_object_record",
            "binding_name": "job_posting",
            "object_payload_field": "job_posting",
            "result_payload_field": "job_posting_result",
            "correlation_id_fields": ["correlation_id"],
            "dispatcher_db_path_fields": ["dispatcher_db_path"],
            "obj_db_path_fields": ["obj_db_path", "job_postings_db_path", "db_path"],
            "processing_state_fields": ["processing_state"],
            "processed_fields": ["processed"],
            "failed_reason_fields": ["failed_reason"],
            "source_agent_fields": ["source_agent"],
            "source_payload_fields": ["source_payload"],
            "dispatcher_updates_fields": ["dispatcher_updates"],
        },
    },

    "platform_profile_ingest_request": {
        "description": "Deterministic ingest request for applicant profiles from platforms, APIs, or pre-parsed sources.",
        "actions": ["ingest_object", "store_object_result"],
        "required_paths": ["action"],
        "conditions": {
            "all": [
                {"action": {"in": ["ingest_object", "store_object_result"]}},
                {
                    "any": [
                        {"profile_result": {"exists": True}},
                        {"applicant_profile": {"exists": True}},
                        {"profile": {"exists": True}},
                    ]
                },
            ]
        },
        "recommended_paths": ["correlation_id", "source_agent"],
        "request_resolution": {
            "objects": [
                {
                    "binding_name": "profile",
                    "request_field": "applicant_profile",
                    "result_field": "profile_result",
                    "default_obj_name": "profiles",
                    "obj_name_config_key": "profile_obj_name",
                    "db_path_field_key": "profile_db_path_field",
                    "default_source": "text",
                }
            ],
        },
        "action_execution": {
            "handler_name": "ingest_object",
            "binding_name": "profile",
            "object_payload_field": "profile",
            "request_payload_field": "applicant_profile",
            "result_payload_field": "profile_result",
            "correlation_id_fields": ["correlation_id"],
            "db_path_fields": ["obj_db_path", "profiles_db_path", "db_path"],
            "source_agent_fields": ["source_agent"],
            "source_payload_fields": ["source_payload"],
            "default_request_source": "text",
        },
    },
    
    "cover_letter_generation_request": {
        "description": "Cover-letter generation request that routes directly to the writer when structured inputs are ready and otherwise relies on dispatch-driven per-document fan-out.",
        "actions": ["generate_cover_letter"],
        "required_paths": ["action", "applicant_profile"],
        "conditions": {
            "all": [
                {"action": {"in": ["generate_cover_letter"]}},
                {
                    "any": [
                        {"job_posting_result": {"exists": True}},
                        {"job_posting": {"exists": True}},
                    ]
                },
            ]
        },
        "recommended_paths": ["options.language", "options.tone", "options.max_words"],
        "request_resolution": {
            "objects": [
                {
                    "binding_name": "profile",
                    "request_field": "applicant_profile",
                    "result_field": "profile_result",
                    "default_obj_name": "profiles",
                    "obj_name_config_key": "profile_obj_name",
                    "db_path_field_key": "profile_db_path_field",
                    "default_source": "text",
                    "store_sources": ["profile_id", "profiles_db", "stored_profile", "persisted_profile"],
                    "file_sources": ["file", "path", "json_file", "structured_file", "document_file"],
                    "inline_sources": ["text", "json", "dict", "object", "structured", "inline"],
                },
                {
                    "binding_name": "job_posting",
                    "request_field": "job_posting",
                    "result_field": "job_posting_result",
                    "default_obj_name": "job_postings",
                    "obj_name_config_key": "job_posting_obj_name",
                    "db_path_field_key": "job_posting_db_path_field",
                    "default_source": "text",
                    "store_sources": ["correlation_id", "job_postings_db", "stored_job_posting", "persisted_job_posting"],
                    "drop_request_field_when_resolved": True,
                    "drop_db_path_field_when_resolved": True,
                }
            ],
            "default_fields": [],
            "dispatcher_route_target": "_xworker",
            "ready_route_target": "_xworker",
 
        },
    },
}


PROMPT_FRAGMENTS: dict[str, dict[str, Any]] = {
    "source_grounding": {
        "text": "Use only source-grounded facts. State uncertainty explicitly instead of inventing details.",
    },
    "json_output": {
        "text": "Return machine-readable JSON only when the task contract requires structured output.",
    },
    "router_handoff": {
        "text": "Delegate only when specialization or deterministic workflow handling is required.",
    },
    "deterministic_workflow": {
        "text": "Follow declared workflow/state transitions deterministically instead of improvising orchestration.",
    },
}


AGENT_SKILLS: dict[str, dict[str, Any]] = {
    "xplaner_xrouter_core": {
        "role": "xplaner_xrouter",
        "prompt_fragments": ["source_grounding", "router_handoff"],
        "description": "Default planning and routing profile for the primary agent.",
        "job_name": "interactive_planning",
    },
    "xrouter_cover_letter_sequence_planner": {
        "role": "xplaner_xrouter",
        "prompt_fragments": ["source_grounding", "router_handoff", "deterministic_workflow"],
        "description": "Planning profile for deterministic dispatch->parse->cover-letter sequence initialization.",
        "job_name": "router_planner_cover_letter_sequence",
    },
    "xrouter_repo_knowledge_async_planner": {
        "role": "xplaner_xrouter",
        "prompt_fragments": ["source_grounding", "router_handoff", "deterministic_workflow"],
        "description": "Planning profile for async repo-knowledge fanout routing to xworker.",
        "job_name": "router_planner_repo_knowledge_async",
    },
  
    "xworker_core": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding"],
        "description": "Default worker profile for generic execution.",
        "job_name": "generic_execution",
    },
    "xworker_dispatch": {
        "role": "xworker",
        
        "prompt_fragments": ["source_grounding", "deterministic_workflow"],
        "description": "Worker profile for deterministic dispatch and document bucketing.",
        "job_name": "document_dispatch",
    },
    "xworker_dispatch_ingest_import_pipeline": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding", "deterministic_workflow"],
        "description": "Worker profile for deterministic document ingest/import pipeline execution.",
        "job_name": "document_dispatch_ingest_import_pipeline",
    },

    "xworker_generic_parser": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding", "json_output"],
        "description": "Worker profile for generic structured parsing.",
        "job_name": "generic_parser",
    },
    "xworker_profile_parser": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding", "json_output"],
        "description": "Worker profile for applicant profile parsing.",
        "job_name": "applicant_profile_parser",
    },
    "xworker_job_posting_parser": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding", "json_output"],
        "description": "Worker profile for job posting parsing.",
        "job_name": "job_posting_parser",
    },
    "xworker_generic_writer": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding", "json_output"],
        "description": "Worker profile for generic structured writing.",
        "job_name": "generic_writer",
    },
    "xworker_cover_letter_writer": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding", "json_output"],
        "description": "Worker profile for cover-letter generation.",
        "job_name": "cover_letter_writer",
    },
  
    "xworker_mail_agent_runtime": {
        "role": "xworker",
        "prompt_fragments": ["source_grounding"],
        "description": "Worker profile for running the standalone mail-agent runtime bridge.",
        "job_name": "mail_agent_runtime",
    },
  
}


AGENT_MANIFEST: dict[str, dict[str, Any]] = {
    "_xrouter_xplanner": {
        "role": "xrouter_xplanner",
        "skill_profile": "xplaner_xrouter_core",
        "instance_policy": "session_scoped",
        "routing_policy": {"mode": "xrouter_xplanner", "can_route": True},
        "skill_profile_loading": {
            "mode": "job_name",
            "fallback_skill_profile": "xplaner_xrouter_core",
        },
        "job_skill_profiles": _job_skill_profiles_for_agent("_xrouter_xplanner"),
        "handoff_policy": {
            "allowed_targets": [],
            "target_policies": {
                "_xworker": {
                    "default_protocol": "agent_handoff_v1",
                    "accepted_protocols": ["message_text", "agent_handoff_v1"],
                    "handoff_schema": "xrouter_to_xworker",
                },
            },
        },
    },
    "_xplaner_xrouter": {
        "role": "xrouter_xplanner",
        "skill_profile": "xplaner_xrouter_core",
        "instance_policy": "session_scoped",
        "routing_policy": {"mode": "xrouter_xplanner", "can_route": True},
        "skill_profile_loading": {
            "mode": "job_name",
            "fallback_skill_profile": "xplaner_xrouter_core",
        },
        "job_skill_profiles": _job_skill_profiles_for_agent("_xrouter_xplanner"),
        "handoff_policy": {
            "allowed_targets": [],
            "target_policies": {
                "_xworker": {
                    "default_protocol": "agent_handoff_v1",
                    "accepted_protocols": ["message_text", "agent_handoff_v1"],
                    "handoff_schema": "xrouter_to_xworker",
                },
            },
        },
    },
    "_xworker": {
        "role": "xworker",
        "skill_profile": "xworker_core",
        "routing_policy": {"mode": "xworker", "can_route": True},
        "skill_profile_loading": {
            "mode": "tool_name",
            "fallback_selection_mode": "job_name",
            "fallback_skill_profile": "xworker_core",
        },
        "job_skill_profiles": _job_skill_profiles_for_agent("_xworker"),
        "tool_skill_profiles": _tool_skill_profiles_for_agent("_xworker"),
        "handoff_policy": {
            "allowed_sources": ["_xrouter_xplanner", "_xplaner_xrouter", "_xworker"],
            "allowed_targets": [],
            "source_policies": {
                "_xrouter_xplanner": {
                    "accepted_protocols": ["message_text", "agent_handoff_v1"],
                    "handoff_schema": "xrouter_to_xworker",
                },
                "_xplaner_xrouter": {
                    "accepted_protocols": ["message_text", "agent_handoff_v1"],
                    "handoff_schema": "xrouter_to_xworker",
                },
                "_xworker": {
                    "accepted_protocols": ["agent_handoff_v1"],
                    "handoff_schema": "xworker_to_xworker",
                },
            },
        },
    },
}


TOOL_CONFIGS: list[dict[str, Any]] = [
    
    {
        "name": "vdb_worker",
        "description": "Create/list/build/wipe vector store directories under AppData (runs in a subprocess).",
        "parameters": [
            {"name": "operation", "type": "string", "description": "Operation to run: list|create|status|build|wipe.", "required": True, "enum": ["list", "create", "status", "build", "wipe"]},
            {"name": "store", "type": "string", "description": "Store id/name. Examples: '1' => VSM_1_Data, 'my_store' => VSM_my_store_Data. Empty => auto-next."},
            {"name": "root_dir", "type": "string", "description": "Root directory to index (only used for build). Default: project root."},
            {"name": "doc_types", "type": "array", "description": "Optional suffix filter for build operations, e.g. ['.txt', '.md']. When provided, only matching files are indexed.", "items": {"type": "string"}},
            {"name": "chunk_strategy", "type": "string", "description": "Optional chunking strategy for build operations: recursive|character|markdown."},
            {"name": "chunk_size", "type": "integer", "description": "Optional chunk size for build operations."},
            {"name": "overlap", "type": "integer", "description": "Optional chunk overlap for build operations."},
            {"name": "force", "type": "boolean", "description": "Required for wipe operations.", "default": False},
            {"name": "remove_store_dir", "type": "boolean", "description": "If true and operation=wipe: delete the whole store directory. Otherwise remove only index+manifest files.", "default": False},
        ],
    },
    {
        "name": "adb_operation",
        "description": "Execute a generic AgentDB repository operation such as health, index setup, object upsert/load/delete, text search, relation-graph load, or batch apply_operations.",
        "parameters": [
            {"name": "operation", "type": "string", "description": "Operation to run: health|ensure_index_objects|upsert_object|delete_object|load_object|load_objects|find_objects|load_relation_graph|apply_operations.", "required": True, "enum": ["health", "ensure_index_objects", "upsert_object", "delete_object", "load_object", "load_objects", "find_objects", "load_relation_graph", "apply_operations"]},
            {"name": "object_name", "type": "string", "description": "Logical AgentDB object name such as document, entity, relation, or embedding.", "required": False},
            {"name": "object_id", "type": "string", "description": "Object id used by upsert_object, load_object, or delete_object.", "required": False},
            {"name": "object_payload", "type": "object", "description": "Object payload used by upsert_object.", "required": False},
            {"name": "object_filter", "type": "object", "description": "Optional filter object for load_objects.", "required": False},
            {"name": "limit", "type": "integer", "description": "Result limit for load_objects or find_objects.", "required": False, "default": 50},
            {"name": "namespace_id", "type": "string", "description": "Namespace id used by find_objects or load_relation_graph.", "required": False},
            {"name": "query_text", "type": "string", "description": "Query text used by find_objects.", "required": False},
            {"name": "source_entity_id", "type": "string", "description": "Source entity id used by load_relation_graph.", "required": False},
            {"name": "max_depth", "type": "integer", "description": "Maximum traversal depth for load_relation_graph.", "required": False, "default": 2},
            {"name": "operations", "type": "array", "description": "Batch operation list for apply_operations. Each item may contain action, object_name, object_id, and object_payload.", "required": False, "items": {"type": "object"}},
            {"name": "agents_db_uri", "type": "string", "description": "Optional AgentsDB socket uri such as agentsdb://localhost:2331.", "required": False},
            {"name": "backend_uri", "type": "string", "description": "Optional backend repository uri such as agentsmem://local.", "required": False},
            {"name": "database_name", "type": "string", "description": "Optional logical database name.", "required": False},
            {"name": "memory_image_path", "type": "string", "description": "Optional image path for the in-memory backend.", "required": False},
        ],
    },
    {
        "name": "adb_relation_graph",
        "description": "Load and analyze the AgentsDB relation graph for visualization and AI/ML data-model exploration.",
        "implementation_name": "adb_relation_graph",
        "parameters": [
            {"name": "source_uri", "type": "string", "description": "Optional AgentsDB tool endpoint URI. Example: agentsdb://127.0.0.1:2331/tools:adb_relation_graph.", "required": False},
            {"name": "tool_id", "type": "string", "description": "Graphic tool id to resolve. Default: adb_relation_graph.", "required": False, "enum": ["adb_relation_graph", "workflow_diagram", "sequence_diagram"], "default": "adb_relation_graph"},
            {"name": "include_view_state", "type": "boolean", "description": "When true, include render-oriented node/edge draw objects.", "required": False, "default": True},
            {"name": "layout_spread", "type": "number", "description": "Optional graph layout spread factor used for view_state generation.", "required": False, "default": 1.0},
            {"name": "selected_kind", "type": "string", "description": "Optional focus selector kind for the graph view state.", "required": False, "enum": ["", "node", "edge"], "default": ""},
            {"name": "selected_object_id", "type": "string", "description": "Optional focused node_id or edge_id used with selected_kind.", "required": False},
            {"name": "include_connection_preview", "type": "boolean", "description": "When true, include connection/tool preview metadata from the graph control plane.", "required": False, "default": False},
        ],
    },
    {
        "name": "repo_knowledge_worker",
        "description": "Scan, cleanup, rebuild, and async-run repository knowledge as AgentsDB document, entity, relation, and embedding objects.",
        "parameters": [
            {"name": "operation", "type": "string", "description": "Operation to run: scan|build|cleanup|delete|rebuild|status|repair_namespace.", "required": True, "enum": ["scan", "build", "cleanup", "delete", "rebuild", "status", "repair_namespace"]},
            {"name": "root_dir", "type": "string", "description": "Repository root directory to parse. Default: current ALDE workspace root."},
            {"name": "image_path", "type": "string", "description": "Optional snapshot path when the runtime falls back to an in-memory AgentsDB backend."},
            {"name": "workers", "type": "integer", "description": "Number of indexing workers.", "default": 4},
            {"name": "extensions", "type": "array", "description": "Optional extension filter, default ['.py'].", "items": {"type": "string"}},
            {"name": "cleanup_before_build", "type": "boolean", "description": "When operation=build, run cleanup first via delete_object calls.", "required": False, "default": False},
            {"name": "cleanup_namespace_ids", "type": "array", "description": "Namespaces to clean during cleanup/rebuild. Default: ['ns_alde_default', 'ns_repo_knowledge'].", "required": False, "items": {"type": "string"}},
            {"name": "cleanup_object_names", "type": "array", "description": "Object types to clean: embedding|relation|entity|document.", "required": False, "items": {"type": "string"}},
            {"name": "cleanup_owner_prefixes", "type": "array", "description": "Owner-i.d prefixes used for safe embedding cleanup. Default: ['blk:repo:'].", "required": False, "items": {"type": "string"}},
            {"name": "delete_async", "type": "boolean", "description": "Perform delete phase concurrently (ThreadPool) for cleanup/rebuild operations.", "required": False, "default": True},
            {"name": "run_async", "type": "boolean", "description": "Run build/cleanup/rebuild/repair in background and return job_id immediately.", "required": False, "default": False},
            {"name": "job_id", "type": "string", "description": "Job id for operation=status polling. Optional custom id when run_async=true.", "required": False},
        ],
    },
    {
        "name": "adb_query",
        "description": "Query indexed repository knowledge and return relevant code context chunks (blocks, entities, relations) for the IDE Agent. Uses dense-vector search with text-search fallback.",
        "parameters": [
            {"name": "query", "type": "string", "description": "Natural-language search query, e.g. 'how does the AgentsDB pipeline store embeddings'.", "required": True},
            {"name": "owner_types", "type": "array", "description": "Owner types to query: block | entity | relation | all. Default: [block, entity].", "items": {"type": "string"}},
            {"name": "limit", "type": "integer", "description": "Max results per owner_type (1–50). Default: 10.", "default": 10},
            {"name": "namespace_id", "type": "string", "description": "AgentsDB namespace to query. Default: ns_repo_knowledge."},
            {"name": "image_path", "type": "string", "description": "Optional snapshot path for in-memory fallback backend."},
            {"name": "use_vector", "type": "boolean", "description": "Attempt dense-vector search before text fallback. Default: true.", "default": True},
        ],
    },
    {
        "name": "write_document",
        "description": "Persist the generated document to disk.",
        "parameters": [
            {"name": "content", "type": "string", "description": "text to write to disk.", "required": True},
            {"name": "path", "type": "string", "description": "Directory to store the file.", "default_ref": "default_save_dir"},
            {"name": "filename", "type": "string", "description": "Optional filename for the markdown artifact."},
        ],
    },
    {
        "name": "read_document",
        "description": "Read the content of a known file from disk. Use this when the request provides a concrete file path to open, read, or load.",
        "final_result": False,
        "tool_response_required": True,
        "parameters": [
            {"name": "file_path", "type": "string", "description": "The absolute path to the file to read.", "required": True},
        ],
    },
    {
        "name": "pypdf_read_document",
        "description": "Read a concrete PDF file from disk using pypdf extraction only.",
        "final_result": False,
        "tool_response_required": True,
        "parameters": [
            {"name": "file_path", "type": "string", "description": "The absolute path to the PDF file to read.", "required": True},
        ],
    },
    {
        "name": "update_document",
        "description": "Update a document's metadata.",
        "parameters": [
            {"name": "data", "type": "array", "description": "List of documents to search through.", "required": True, "items": {"type": "object"}},
            {"name": "item", "type": "string", "description": "The metadata field name to match and update.", "required": True},
            {"name": "updatestr", "type": "string", "description": "The new value to set for the matched field.", "required": True},
        ],
    },
    {
        "name": "delete_document",
        "description": "Delete a document from disk.",
        "parameters": [
            {"name": "file_path", "type": "string", "description": "The absolute path to the file to delete.", "required": True},
        ],
    },
    {
        "name": "list_documents",
        "description": "List all documents in a directory.",
        "parameters": [
            {"name": "directory", "type": "string", "description": "Directory path to list.", "default_ref": "default_save_dir"},
        ],
    },
    {
        "name": "md_to_pdf",
        "description": "Convert a Markdown file to a clean PDF (ReportLab).",
        "parameters": [
            {"name": "md_path", "type": "string", "description": "Path to the input Markdown file.", "required": True},
            {"name": "pdf_path", "type": "string", "description": "Path to the output PDF file.", "required": True},
            {"name": "title", "type": "string", "description": "Optional PDF title."},
            {"name": "author", "type": "string", "description": "Optional PDF author."},
            {"name": "pagesize", "type": "string", "description": "Page size.", "enum": ["A4", "LETTER"], "default": "A4"},
            {"name": "margin_left_mm", "type": "number", "description": "Left margin in mm.", "default": 18},
            {"name": "margin_right_mm", "type": "number", "description": "Right margin in mm.", "default": 18},
            {"name": "margin_top_mm", "type": "number", "description": "Top margin in mm.", "default": 16},
            {"name": "margin_bottom_mm", "type": "number", "description": "Bottom margin in mm.", "default": 16},
        ],
    },
    {
        "name": "calendar",
        "description": "Schedule an event in the calendar.",
        "parameters": [
            {"name": "event", "type": "string", "description": "Name or description of the event.", "required": True},
            {"name": "date", "type": "string", "description": "Date of the event (e.g., '2025-12-01').", "required": True},
            {"name": "time", "type": "string", "description": "Time of the event (e.g., '14:00').", "required": True},
        ],
    },
   
    {
        "name": "run_mail_agent",
        "description": "Start the standalone Projekt_Mail_Agent in once or watch mode.",
        "parameters": [
            {"name": "mode", "type": "string", "description": "Execution mode: once or watch.", "required": False, "enum": ["once", "watch"], "default": "once"},
            {"name": "project_dir", "type": "string", "description": "Optional absolute path to Projekt_Mail_Agent. If omitted, default path or MAIL_AGENT_PROJECT_DIR is used.", "required": False},
            {"name": "python_executable", "type": "string", "description": "Optional Python executable for the mail-agent process. If omitted, .venv/bin/python is preferred.", "required": False},
            {"name": "timeout_seconds", "type": "integer", "description": "Timeout for once mode execution.", "required": False, "default": 120},
            {"name": "background", "type": "boolean", "description": "When mode=watch, run detached in background.", "required": False, "default": True},
        ],
    },
    
    {
        "name": "iter_documents",
        "description": "Load supported documents from one or more files or directories with optional type, pattern, and recursion filters.",
        "parameters": [
            {"name": "root", "type": "string", "description": "Single absolute or relative path to scan.", "required": False},
            {"name": "roots", "type": "array", "description": "Optional list of absolute or relative paths to scan.", "items": {"type": "string"}},
            {"name": "doc_types", "type": "array", "description": "Optional file extensions or aliases to include, e.g. ['.md', 'pdf', 'py'].", "items": {"type": "string"}},
            {"name": "patterns", "type": "array", "description": "Optional glob-style path filters, e.g. ['**/*.md', 'docs/**/*.txt'].", "items": {"type": "string"}},
            {"name": "recursive", "type": "boolean", "description": "Recurse into subdirectories.", "required": False, "default": True},
            {"name": "max_depth", "type": "integer", "description": "Optional maximum directory depth relative to each root. 0 means only the root directory.", "required": False},
        ],
    },
    {
        "name": "dispatch_documents",
        "description": "Discover documents in a directory, fingerprint them (SHA-256), check/update a small DB, and prepare handoff payloads for a parser agent.",
        "implementation_name": "dispatch_documents",
        "dispatch_policy": {
            "obj_name": "job_postings",
            "obj_db_path_field": "obj_db_path",
            "parser_job_name": "job_posting_parser",
            "document_type": "file",
            "requested_actions": ["parse", "extract_text", "store_object_result", "mark_processed_on_success"],
            "default_target_agent": "_xworker",
            "source_agent": "_xworker",
            "handoff_protocol": "agent_handoff_v1",
            "metadata_defaults": {
                "obj_db_path": {
                    "resolver": "default_document_db_path",
                    "obj_name": "job_postings_db"
                }
            },
        },
        "parameters": [
            {"name": "scan_dir", "type": "string", "description": "Directory to scan for documents.", "required": True},
            {"name": "db", "type": "object", "description": "Optional DB adapter/config. Supported: { 'path': '/abs/path/to/db.json' }", "required": False},
            {"name": "db_path", "type": "string", "description": "Optional DB JSON path (file-based DB). Overrides db.path.", "required": False},
            {"name": "obj_name", "type": "string", "description": "Logical object/store name used to derive object-specific DB metadata and handoff fields, e.g. 'job_postings'.", "required": False, "default": "job_postings_DB"},
            {"name": "thread_id", "type": "string", "description": "Thread id for link.thread_id (or UNKNOWN).", "required": False},
            {"name": "dispatcher_message_id", "type": "string", "description": "Dispatcher message id for reporting (or UNKNOWN).", "required": False},
            {"name": "recursive", "type": "boolean", "description": "Recurse into subdirectories.", "required": False, "default": True},
            {"name": "extensions", "type": "array", "description": "File extensions to include (default: ['.pdf', '.PDF']).", "required": False, "items": {"type": "string"}},
            {"name": "max_files", "type": "integer", "description": "Optional max number of PDFs to scan.", "required": False},
            {"name": "action", "type": "string", "description": "Optional higher-level action context, e.g. generate_cover_letter, preserved across dispatch handoffs.", "required": False},
            {"name": "profile_id", "type": "string", "description": "Optional profile identifier for cover-letter sequences. When applicant_profile is missing, dispatch preserves profile_id and derives applicant_profile={source: profile_id, value: <profile_id>} for downstream handoffs.", "required": False},
            {"name": "applicant_profile", "type": "object", "description": "Optional applicant-profile request envelope preserved for cover-letter sequences.", "required": False},
            {"name": "profile_result", "type": "object", "description": "Optional resolved profile_result payload preserved for cover-letter sequences.", "required": False},
            {"name": "job_posting", "type": "object", "description": "Optional job-posting request envelope preserved for downstream parser/writer handoffs.", "required": False},
            {"name": "job_posting_result", "type": "object", "description": "Optional resolved job_posting_result payload preserved for downstream handoffs.", "required": False},
            {"name": "options", "type": "object", "description": "Optional cover-letter writing options preserved across dispatch handoffs.", "required": False},
            {"name": "cover_letter_context", "type": "object", "description": "Optional additional cover-letter context preserved across dispatch handoffs.", "required": False},
            {"name": "source_document", "type": "object", "description": "Optional source-document metadata preserved across dispatch handoffs.", "required": False},
            {"name": "agent_name", "type": "string", "description": "Runtime target agent for emitted handoff messages. Use only agent labels such as _xworker or _xrouter_xplanner, not job names.", "required": False, "default": "_xworker"},
            {"name": "parser_agent_name", "type": "string", "description": "Optional legacy runtime target-agent override for emitted handoff messages. Use only agent labels here; parser jobs belong in parser_job_name.", "required": False},
            {"name": "parser_job_name", "type": "string", "description": "Parser job_name for each emitted handoff payload, for example job_posting_parser.", "required": False, "default": "job_posting_parser"},
            {"name": "dry_run", "type": "boolean", "description": "If true: do not update DB and do not create handoff messages.", "required": False, "default": False},
        ],
    },
    {
        "name": "execute_action_request",
        "description": "Execute a deterministic action request via the action layer, e.g. ingest_object, store_object_result, or upsert_object_record, so workflow agents can update stores explicitly through a single tool entry point.",
        "snapshot_view": {
            "kind": "dispatcher_action",
            "title": "Dispatcher action executed",
            "summary_fields": ["action", "correlation_id"],
        },
        "parameters": [
            {"name": "action_request", "type": "object", "description": "Full action request object including action and payload fields.", "required": False},
            {"name": "action", "type": "string", "description": "Optional action name used with payload, e.g. ingest_object, store_object_result, or upsert_object_record.", "required": False},
            {"name": "payload", "type": "object", "description": "Optional payload object merged with action when action_request is not supplied.", "required": False},
        ],
    },
    {
        "name": "upsert_object_record",
        "description": "Atomically update an object store and the dispatcher DB for the same logical record, with rollback if the second write fails.",
        "snapshot_view": {
            "kind": "dispatcher_action",
            "title": "Dispatcher object record upserted",
            "summary_fields": ["action", "correlation_id"],
        },
        "parameters": [
            {"name": "object_result", "type": "object", "description": "Normalized or parser-style object result payload to persist.", "required": True},
            {"name": "correlation_id", "type": "string", "description": "Optional explicit correlation id for both stores.", "required": False},
            {"name": "dispatcher_db_path", "type": "string", "description": "Path to dispatcher_doc_db.json.", "required": False},
            {"name": "obj_db_path", "type": "string", "description": "Path to the target object DB file.", "required": False},
            {"name": "obj_name", "type": "string", "description": "Logical object/store name to upsert in the object DB.", "required": False, "default": "documents"},
            {"name": "processing_state", "type": "string", "description": "Optional dispatcher processing state override.", "required": False},
            {"name": "processed", "type": "boolean", "description": "Optional processed flag override.", "required": False},
            {"name": "failed_reason", "type": "string", "description": "Optional dispatcher failure reason.", "required": False},
            {"name": "source_agent", "type": "string", "description": "Optional logical source label.", "required": False},
            {"name": "source_payload", "type": "object", "description": "Optional source envelope for traceability.", "required": False},
            {"name": "dispatcher_updates", "type": "object", "description": "Optional extra dispatcher record fields to upsert.", "required": False},
        ],
    },
    {
        "name": "store_object_result",
        "description": "Persist a normalized or parser-style object result directly into the selected object store.",
        "parameters": [
            {"name": "object_result", "type": "object", "description": "Object result payload to store.", "required": True},
            {"name": "correlation_id", "type": "string", "description": "Optional explicit correlation id.", "required": False},
            {"name": "db_path", "type": "string", "description": "Optional path to the target object DB file.", "required": False},
            {"name": "obj_name", "type": "string", "description": "Logical object/store name to persist into.", "required": False, "default": "documents"},
            {"name": "source_agent", "type": "string", "description": "Optional logical source label.", "required": False},
            {"name": "source_payload", "type": "object", "description": "Optional source envelope or original payload for traceability.", "required": False},
        ],
    },
    {
        "name": "ingest_object",
        "description": "Ingest a normalized object payload or a parser-style object result directly into the selected object store.",
        "parameters": [
            {"name": "object_payload", "type": "object", "description": "Normalized object payload to persist when no parser-style result object is supplied.", "required": False},
            {"name": "request_payload", "type": "object", "description": "Optional request-style envelope using source/value fields.", "required": False},
            {"name": "object_result", "type": "object", "description": "Optional parser-style object result payload to persist directly.", "required": False},
            {"name": "correlation_id", "type": "string", "description": "Optional explicit correlation id.", "required": False},
            {"name": "db_path", "type": "string", "description": "Optional path to the target object DB file.", "required": False},
            {"name": "obj_name", "type": "string", "description": "Logical object/store name to persist into.", "required": False, "default": "documents"},
            {"name": "source_agent", "type": "string", "description": "Optional logical source label.", "required": False},
            {"name": "source_payload", "type": "object", "description": "Optional source envelope or original payload for traceability.", "required": False},
            {"name": "parse", "type": "object", "description": "Optional parse metadata used when only object_payload is supplied.", "required": False},
        ],
    },
 
    {
        "name": "call_api",
        "description": "Call an external API endpoint.",
        "parameters": [
            {"name": "endpoint", "type": "string", "description": "The API endpoint URL.", "required": True},
            {"name": "method", "type": "string", "description": "HTTP method to use.", "enum": ["GET", "POST"], "default": "GET"},
            {"name": "payload", "type": "string", "description": "JSON payload for POST requests."},
        ],
    },
  
    {
        "name": "route_to_agent",
        "description": "Route the request to a specialized agent with an explicit job_name or tool_name so the runtime can select the correct handoff schema, worker specialization, and optional explicit tool set.",
        "implementation_name": None,
        "parameters": [
            {"name": "target_agent", "type": "string", "description": "The target agent to route to. Optional when handoff_payload.handoff_to or agent_response.handoff_to is provided.", "required": False, "enum_ref": "agent_labels"},
            {"name": "job_name", "type": "string", "description": "Optional routing attribute. Use this for job-specialized xworker execution such as document_dispatch, applicant_profile_parser, job_posting_parser, cover_letter_writer, agent_system_builder, or generic_execution. Required when no tool_name is provided.", "required": False, "enum_ref": "job_names"},
            {"name": "tool_name", "type": "string", "description": "Optional routing attribute for direct tool-focused xworker execution. When provided, the runtime may resolve the worker profile and default tool allowlist from this tool.", "required": False, "enum_ref": "tool_names"},
            {"name": "tools", "type": "array", "description": "Optional explicit xworker tool allowlist. Provide concrete tool names such as ['read_document'] to restrict the routed worker call to those tools.", "required": False, "items": {"type": "string"}},
            {"name": "message_text", "type": "string", "description": "Plain-text handoff message to pass to the agent.", "required": False},
            {"name": "user_question", "type": "string", "description": "Legacy alias for message_text.", "required": False},
            {"name": "run_async", "type": "boolean", "description": "Enable asynchronous routed execution. When true, max_agents is required.", "required": False, "default": False},
            {"name": "max_agents", "type": "integer", "description": "Maximum number of parallel agent branches for async routed execution. Required when run_async=true.", "required": False},
            {"name": "handoff_protocol", "type": "string", "description": "Optional handoff protocol. Supported: message_text, agent_handoff_v1.", "required": False},
            {"name": "agent_response", "type": "object", "description": "Structured response object to normalize into a handoff envelope. Example: {agent_label, output|generated|msg, handoff_to}.", "required": False},
            {"name": "handoff_payload", "type": "object", "description": "Structured payload for handoff protocols.", "required": False},
            {"name": "handoff_metadata", "type": "object", "description": "Optional metadata attached to the handoff envelope.", "required": False},
        ],
    },
]


TOOL_NAMES: dict[str, str] = {
    "adb_operation": "adb_operation",
    "agentdb_operation": "adb_operation",
    "agentsdb_operation": "adb_operation",
    "agent_relation_graph": "adb_relation_graph",
    "agentdb_relation_graph": "adb_relation_graph",
    "agentsdb_relation_graph": "adb_relation_graph",
    "agentsdb://127.0.0.1:2331/tools:agent_relation_graph": "adb_relation_graph",
    "dispatch_docs": "dispatch_documents",
    "dispatch_documents": "dispatch_documents",
    "data_dispatcher/dispatch_documents": "dispatch_documents",
    "data_dispatcher.dispatch_documents": "dispatch_documents",
    "dispatch_job_posting_pdfs": "dispatch_documents",
    "ingest_object": "ingest_object",
    "ingest_profile": "ingest_object",
    "ingest_job_posting": "ingest_object",
    "ingest_document": "ingest_object",
    "persist_cover_letter_artifacts": "persist_document_artifacts",
    "persist_document_artifacts": "persist_document_artifacts",
    "store_object_result": "store_object_result",
    "store_job_posting_result": "store_object_result",
    "store_profile_result": "store_object_result",
    "store_document_result": "store_object_result",
    "upsert_object_record": "upsert_object_record",
    "upsert_dispatcher_job_record": "upsert_object_record",
    "upsert_job_record": "upsert_object_record",
    "batch_document_generator": "dispatch_documents",
    "batch_generate_documents": "dispatch_documents",
    "batch_generate_cover_letters": "dispatch_documents",
    "pypdf_read_document": "pypdf_read_document",
    "pypdf_read": "pypdf_read_document",
    "read_pdf_with_pypdf": "pypdf_read_document",
    "store_profile": "store_object_result",
    "persist_profile": "store_object_result",
}


ACTION_NAMES: dict[str, str] = {
    "generate_cover_letter": "generate_cover_letter",
    "cover_letter_writer": "generate_cover_letter",
    "cover_letter_generation": "generate_cover_letter",
    "dispatch_document": "dispatch_documents",
    "generate_cover_letters_batch": "dispatch_documents",
    "batch_generate_documents": "dispatch_documents",
    "batch_generate_cover_letters": "dispatch_documents",
   
    "data_dispatcher/dispatch_documents": "dispatch_documents",
    "ingest_object": "ingest_object",
    "store_object_result": "store_object_result",
    "upsert_object_record": "upsert_object_record",
    "ingest_job_posting": "ingest_object",
    "store_job_posting": "store_object_result",
    "store_job_posting_result": "store_object_result",
    "ingest_profile": "ingest_object",
    "store_profile": "store_object_result",
    "store_profile_result": "store_object_result",
    "persist_profile": "store_object_result",
    "upsert_dispatcher_job_record": "upsert_object_record",
    "upsert_job_record": "upsert_object_record",
}


TOOL_GROUPS: dict[str, list[str]] = {
    "doc_ro": [
        "read_document",
        "pypdf_read_document",
        "list_documents",
    ],
    "docs_rw": [
        "read_document",
        "pypdf_read_document",
        "write_document",
        "update_document",
        "delete_document",
        "list_documents",
        "md_to_pdf",
    ],
    "doc_rw": [
        "read_document",
        "pypdf_read_document",
        "write_document",
        "update_document",
        "delete_document",
        "list_documents",
        "md_to_pdf",
    ],
    "web": ["fetch_url", "fetch_data", "call_api"],
    "comms": ["send_mail", "run_mail_agent", "calendar", "call", "accept_call", "reject_call"],
    "code": ["code_tool", "iter_documents"],
    "dispatcher": ["dispatch_documents", "execute_action_request", "upsert_object_record", "ingest_object", "store_object_result", "vdb_worker", "repo_knowledge_worker"],
    "agentdb": ["adb_operation", "agent_relation_graph", "repo_knowledge_worker", "repo_knowledge_query"],
    "repo_knowledge": ["adb_operation", "agent_relation_graph", "repo_knowledge_worker", "repo_knowledge_query"],
}


FORCED_ROUTES: dict[str, list[dict[str, Any]]] = {
    "_xrouter_xplanner": [
        {
            "name": "agent_prefix",
            "trigger": {"type": "at_prefix"},
        },
        {
            "name": "cover_letter_writer_direct_payload",
            "trigger": {
                "type": "json_payload",
                "conditions": {
                    "all": [
                        {"action": {"eq": "generate_cover_letter"}},
                        {"job_posting_result": {"exists": True}},
                        {"profile_result": {"exists": True}},
                    ]
                },
            },
            "route": {
                "target_agent": "_xworker",
                "job_name": "cover_letter_writer",
                "handoff_protocol": "agent_handoff_v1",
                "handoff_payload": {
                    "agent_label": "_xrouter_xplanner",
                    "handoff_to": "_xworker",
                    "output": "__cover_letter_writer_payload__",
                },
            },
        },
        {
            "name": "cover_letter_request",
            "trigger": {
                "type": "json_payload",
                "conditions": {
                    "all": [
                        {"action": {"eq": "generate_cover_letter"}},
                        {"job_posting": {"exists": True}},
                        {"applicant_profile": {"exists": True}},
                    ]
                },
            },
            "route": {
                "target_agent": "_xworker",
                "job_name": "document_dispatch",
                "user_question": "__original_input__",
            },
        },
      
    ],
}


WORKFLOWS: dict[str, dict[str, Any]] = {
    "xplaner_xrouter_router": {
        "description": "Primary xplaner_xrouter workflow with generic xworker delegation.",
        "entry_state": "xplaner_ready",
        "retry_policy": {
            "max_attempts": 2,
            "backoff_seconds": [1, 2],
        },
        "states": {
            "xplaner_ready": {
                "actor": {"kind": "agent", "name": "_xrouter_xplanner"},
                "terminal": False,
            },
            "xworker_delegated": {
                "actor": {"kind": "tool", "name": "route_to_agent"},
                "terminal": False,
            },
            "xplaner_retry_pending": {
                "actor": {"kind": "state", "name": "retry_pending"},
                "terminal": False,
            },
            "xplaner_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
            "workflow_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "xplaner_ready",
                "on": {
                    "kind": "tool",
                    "name": "route_to_agent",
                    "conditions": {"target_agent": "_xworker"},
                },
                "to": "xworker_delegated",
            },
            {
                "from": "xworker_delegated",
                "on": {
                    "kind": "state",
                    "name": "routed_agent_complete",
                    "conditions": {"target_agent": "_xworker"},
                },
                "to": "workflow_complete",
            },
            {
                "from": ["xplaner_ready", "xworker_delegated"],
                "on": {
                    "kind": "state",
                    "name": ["model_failed", "routed_agent_failed"],
                    "conditions": {
                        "any": [
                            {"error": {"exists": True}},
                            {"result": {"exists": True}},
                            {"target_agent": {"in": ["_xworker"]}},
                        ]
                    },
                },
                "to": "xplaner_retry_pending",
            },
            {
                "from": "xplaner_retry_pending",
                "on": {"kind": "state", "name": "retry_requested"},
                "to": "xplaner_ready",
            },
            {
                "from": "xplaner_retry_pending",
                "on": {"kind": "state", "name": "retry_exhausted"},
                "to": "xplaner_failed",
            },
        ],
    },
    "xworker_leaf": {
        "description": "Generic leaf workflow for xworker jobs without further routing.",
        "entry_state": "xworker_active",
        "states": {
            "xworker_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "xworker_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
            "xworker_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "xworker_active",
                "on": {"kind": "state", "name": ["followup_complete", "tool_complete"]},
                "to": "xworker_complete",
            },
            {
                "from": "xworker_active",
                "on": {"kind": "state", "name": ["model_failed", "tool_failed"]},
                "to": "xworker_failed",
            },
        ],
    },
  
    "xworker_generic_parser_leaf": {
        "description": "Leaf workflow for generic xworker parser jobs.",
        "entry_state": "parser_active",
        "states": {
            "parser_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "parser_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "parser_active",
                "on": {
                    "kind": "state",
                    "name": "followup_complete",
                    "conditions": {"result": {"exists": True}},
                },
                "to": "parser_complete",
            },
        ],
    },
    "xworker_generic_writer_leaf": {
        "description": "Leaf workflow for generic xworker writer jobs.",
        "entry_state": "writer_active",
        "states": {
            "writer_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "writer_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "writer_active",
                "on": {
                    "kind": "state",
                    "name": "followup_complete",
                    "conditions": {
                        "all": [
                            {"result": {"exists": True}},
                            {"result": {"truthy": True}},
                        ]
                    },
                },
                "to": "writer_complete",
            },
        ],
    },
    "xworker_profile_parser_leaf": {
        "description": "Leaf workflow for the applicant profile parser job.",
        "entry_state": "profile_parser_active",
        "states": {
            "profile_parser_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "profile_parser_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "profile_parser_active",
                "on": {
                    "kind": "state",
                    "name": ["followup_complete", "routed_agent_complete"],
                    "conditions": {"any": [{"result": {"exists": True}}, {"target_agent": "_xworker"}]},
                },
                "to": "profile_parser_complete",
            }
        ],
    },
    "xworker_job_posting_parser_leaf": {
        "description": "Leaf workflow for the job-posting parser job.",
        "entry_state": "job_posting_parser_active",
        "states": {
            "job_posting_parser_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "job_posting_parser_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "job_posting_parser_active",
                "on": {
                    "kind": "state",
                    "name": ["followup_complete", "routed_agent_complete"],
                    "conditions": {"any": [{"result": {"exists": True}}, {"target_agent": "_xworker"}]},
                },
                "to": "job_posting_parser_complete",
            }
        ],
    },
    "xworker_cover_letter_writer_leaf": {
        "description": "Leaf workflow for the cover-letter writer job.",
        "entry_state": "cover_letter_writer_active",
        "states": {
            "cover_letter_writer_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "cover_letter_writer_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "cover_letter_writer_active",
                "on": {
                    "kind": "state",
                    "name": ["followup_complete", "routed_agent_complete"],
                    "conditions": {
                        "all": [
                            {"result": {"exists": True}},
                            {"result": {"truthy": True}},
                        ]
                    },
                },
                "to": "cover_letter_writer_complete",
            }
        ],
    },
    "xworker_adb_operation_leaf": {
        "description": "Leaf workflow for the adb_operation tool-execution job.",
        "entry_state": "adb_operation_active",
        "states": {
            "adb_operation_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "adb_operation_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
            "adb_operation_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "adb_operation_active",
                "on": {"kind": "state", "name": ["followup_complete", "tool_complete"]},
                "to": "adb_operation_complete",
            },
            {
                "from": "adb_operation_active",
                "on": {"kind": "state", "name": ["model_failed", "tool_failed"]},
                "to": "adb_operation_failed",
            },
        ],
    },
    "xworker_adb_worker_leaf": {
        "description": "Leaf workflow for adb_worker tool-execution job.",
        "entry_state": "adb_worker_active",
        "states": {
            "adb_worker_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "adb_worker_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
            "adb_worker_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "adb_worker_active",
                "on": {"kind": "state", "name": ["followup_complete", "tool_complete"]},
                "to": "adb_worker_complete",
            },
            {
                "from": "adb_worker_active",
                "on": {"kind": "state", "name": ["model_failed", "tool_failed"]},
                "to": "adb_worker_failed",
            },
        ],
    },
    "xworker_adb_query_leaf": {
        "description": "Leaf workflow for adb_query tool-execution job.",
        "entry_state": "adb_query_active",
        "states": {
            "adb_query_active": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "adb_query_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
            "adb_query_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "adb_query_active",
                "on": {"kind": "state", "name": ["followup_complete", "tool_complete"]},
                "to": "adb_query_complete",
            },
            {
                "from": "adb_query_active",
                "on": {"kind": "state", "name": ["model_failed", "tool_failed"]},
                "to": "adb_query_failed",
            },
        ],
    },
    "xrouter_repo_knowledge_async_router": {
        "description": "xrouter_xplanner workflow for async repo-knowledge fanout to xworker branches.",
        "entry_state": "xrouter_repo_knowledge_ready",
        "parallel": {
            "mode": "router_parallel_branches",
            "enabled": True,
            "workers": 4,
            "enabled_env": "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED",
            "workers_env": "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS",
        },
        "states": {
            "xrouter_repo_knowledge_ready": {
                "actor": {"kind": "agent", "name": "_xrouter_xplanner"},
                "terminal": False,
            },
            "xrouter_repo_knowledge_routed": {
                "actor": {"kind": "tool", "name": "route_to_agent"},
                "terminal": False,
            },
            "xrouter_repo_knowledge_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
            "xrouter_repo_knowledge_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "xrouter_repo_knowledge_ready",
                "on": {
                    "kind": "tool",
                    "name": "route_to_agent",
                    "conditions": {"target_agent": "_xworker"},
                },
                "to": "xrouter_repo_knowledge_routed",
            },
            {
                "from": "xrouter_repo_knowledge_routed",
                "on": {
                    "kind": "state",
                    "name": ["followup_complete", "routed_agent_complete"],
                    "conditions": {
                        "any": [
                            {"result": {"exists": True}},
                            {"target_agent": "_xworker"},
                        ]
                    },
                },
                "to": "xrouter_repo_knowledge_complete",
            },
            {
                "from": ["xrouter_repo_knowledge_ready", "xrouter_repo_knowledge_routed"],
                "on": {"kind": "state", "name": ["model_failed", "tool_failed", "routed_agent_failed"]},
                "to": "xrouter_repo_knowledge_failed",
            },
        ],
    },
    "xworker_documents_dispatch_chain": {
        "description": "Deterministic xworker dispatch chain for document discovery, action execution, and parser handoff.",
        "entry_state": "dispatcher_ready",
        "retry_policy": {
            "max_attempts": 3,
            "backoff_seconds": [1, 2, 4],
        },
        "states": {
            "dispatcher_ready": {
                "actor": {"kind": "agent", "name": "_xworker"},
                "terminal": False,
            },
            "documents_dispatched": {
                "actor": {"kind": "tool", "name": "dispatch_documents"},
                "terminal": False,
            },
            "action_executed": {
                "actor": {"kind": "tool", "name": "execute_action_request"},
                "terminal": False,
            },
            "job_record_upserted": {
                "actor": {"kind": "tool", "name": "upsert_object_record"},
                "terminal": False,
            },
            "parser_routed": {
                "actor": {"kind": "tool", "name": "route_to_agent"},
                "terminal": False,
            },
            "dispatcher_retry_pending": {
                "actor": {"kind": "state", "name": "retry_pending"},
                "terminal": False,
            },
            "dispatcher_failed": {
                "actor": {"kind": "state", "name": "workflow_failed"},
                "terminal": True,
            },
            "workflow_complete": {
                "actor": {"kind": "state", "name": "workflow_complete"},
                "terminal": True,
            },
        },
        "transitions": [
            {
                "from": "dispatcher_ready",
                "on": {"kind": "tool", "name": "dispatch_documents"},
                "to": "documents_dispatched",
            },
            {
                "from": "dispatcher_ready",
                "on": {"kind": "tool", "name": "execute_action_request"},
                "to": "action_executed",
            },
            {
                "from": "dispatcher_ready",
                "on": {"kind": "tool", "name": "upsert_object_record"},
                "to": "job_record_upserted",
            },
            {
                "from": "documents_dispatched",
                "on": {
                    "kind": "tool",
                    "name": "route_to_agent",
                    "conditions": {"target_agent": "_xworker"},
                },
                "to": "parser_routed",
            },
            {
                "from": "parser_routed",
                "on": {"kind": "state", "name": "followup_complete"},
                "to": "workflow_complete",
            },
            {
                "from": ["documents_dispatched", "action_executed", "job_record_upserted"],
                "on": {
                    "kind": "state",
                    "name": ["followup_complete", "tool_complete"],
                    "conditions": {
                        "all": [
                            {"result": {"exists": True}},
                            {"result": {"truthy": True}},
                        ]
                    },
                },
                "to": "workflow_complete",
            },
            {
                "from": ["dispatcher_ready", "documents_dispatched", "action_executed", "job_record_upserted", "parser_routed"],
                "on": {
                    "kind": "state",
                    "name": ["tool_failed", "model_failed", "routed_agent_failed"],
                    "conditions": {
                        "any": [
                            {"tool_name": {"in": ["dispatch_documents", "execute_action_request", "upsert_object_record", "route_to_agent"]}},
                            {"error": {"exists": True}},
                            {"target_agent": "_xworker"},
                        ]
                    },
                },
                "to": "dispatcher_retry_pending",
            },
            {
                "from": "dispatcher_retry_pending",
                "on": {"kind": "state", "name": "retry_requested"},
                "to": "dispatcher_ready",
            },
            {
                "from": "dispatcher_retry_pending",
                "on": {"kind": "state", "name": "retry_exhausted"},
                "to": "dispatcher_failed",
            },
        ],
    },
}
