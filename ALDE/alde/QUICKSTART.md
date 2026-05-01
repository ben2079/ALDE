# Quick Start: Two-Agent Runtime

## Current Delivery Focus

- The active user-facing surface in this repository is the local desktop/runtime path.
- Local desktop runs are persisted in `ALDE/AppData/desktop_runs.json` through the desktop runtime services.
- A dedicated ALDE WebApp frontend is planned only after the desktop UI, runtime, storage, and config layers are stable.

## Desktop Control-Plane Snapshots

The desktop operator surface now uses shared control-plane projections instead of recomputing monitoring and operator state independently in the UI.

Primary load surfaces:

- `load_desktop_monitoring_snapshot(...)`
- `load_operator_status_snapshot(...)`

Primary export surfaces:

- `export_runtime_view(...)`
- `export_desktop_monitoring_snapshot(...)`
- `export_operator_status_snapshot(...)`
- `export_control_plane_snapshot(...)`

The preferred export for operator reporting is `export_control_plane_snapshot(...)` because it writes one bundle under `ALDE/AppData/generated/` containing:

- runtime view projection
- desktop monitoring snapshot
- desktop operator snapshot
- merged recent-item projection for cross-surface review

Shared monitoring/operator snapshot fields:

- `snapshot_kind`
- `healthy`
- `alerts`
- `attention_count`
- `summary_metrics`
- `recent_items`
- `recent_item_count`
- `recent_item_summary`
- `recent_item_filters`
- `detail_rows`

The operator snapshot extends the shared structure with:

- `recent_actions`
- `audit_summary`
- `recent_action_filters`

That extension is what powers the desktop UI filters for action status, action type, action group, and source in the Operations tab.

## Scenario 1: Generate Cover Letter (Simple)

**User provides:** Job posting PDF + stored profile ID

```json
{
    "action": "generate_cover_letter",
    "job_posting_path": "/path/to/job_posting.pdf",
    "profile_id": "profile_abc123"
}
```

**Workflow:**
1. `_xplaner_xrouter` validates the request and selects the required worker job.
2. `_xplaner_xrouter` loads any existing profile context from storage.
3. `_xplaner_xrouter` routes parsing work to `_xworker` with `job_name="job_posting_parser"`.
4. `_xplaner_xrouter` waits for the worker result.
5. `_xplaner_xrouter` routes generation work to `_xworker` with `job_name="cover_letter_writer"`.
6. `_xplaner_xrouter` returns the structured cover-letter result.

**Duration:** ~8-12 seconds

---

## Scenario 2: Full Workflow (New Profile + Job)

**User provides:** Job posting PDF + CV file + language/tone preferences

```json
{
    "action": "generate_cover_letter",
    "job_posting": {
        "source": "file",
        "value": "AppData/VSM_4_Data/job_posting.pdf"
    },
    "applicant_profile": {
        "source": "file",
        "value": "AppData/VSM_4_Data/cv_2026.pdf"
    },
    "options": {
        "language": "de",
        "tone": "modern",
        "max_words": 350,
        "include_enclosures": true
    }
}
```

**Workflow:**
1. `_xplaner_xrouter` validates input and plans the execution order.
2. `_xplaner_xrouter` routes CV parsing to `_xworker` with `job_name="applicant_profile_parser"`.
3. `_xplaner_xrouter` routes job parsing to `_xworker` with `job_name="job_posting_parser"`.
4. `_xplaner_xrouter` waits for both worker results.
5. `_xplaner_xrouter` routes writing to `_xworker` with `job_name="cover_letter_writer"`.
6. `_xworker` saves the generated letter.
7. `_xplaner_xrouter` returns the complete response with quality metrics.

**Duration:** ~12-18 seconds

---

## Scenario 3: Text-Based Input

**User provides:** Job description as text + profile text

```json
{
    "action": "generate_cover_letter",
    "job_posting": {
        "source": "text",
        "value": "Senior Python Developer at TechCorp...\nRequirements: Python, FastAPI, PostgreSQL..."
    },
    "applicant_profile": {
        "source": "text",
        "value": "Max Mustermann, Senior Software Engineer...\nSkills: Python, PySide6, FastAPI..."
    },
    "options": {
        "language": "de",
        "tone": "modern"
    }
}
```

**Duration:** ~10-15 seconds

---

## Scenario 4: Batch Processing (Multiple Job Postings)

**User provides:** Directory with multiple job posting PDFs + stored profile

```json
{
    "action": "generate_cover_letters_batch",
    "job_postings_dir": "AppData/VSM_4_Data/",
    "profile_id": "profile_abc123",
    "options": {
        "language": "de",
        "tone": "modern"
    }
}
```

**Workflow:**
1. `_xplaner_xrouter` triggers `dispatch_documents` or routes `_xworker` with `job_name="document_dispatch"`.
2. `_xworker` scans PDFs, checks DB status, and prepares parse work for unprocessed inputs.
3. `_xworker` executes parsing jobs and stores the results.
4. `_xplaner_xrouter` triggers generation for each job using `_xworker` with `job_name="cover_letter_writer"`.
5. `_xplaner_xrouter` returns batch results with a summary.

**Duration:** ~5-10 seconds per job (after dispatcher)

---

## Response Examples

### Success Response
```json
{
    "status": "success",
    "cover_letter": {
        "full_text": "Sehr geehrte Damen und Herren,\n\nI am excited to apply...",
        "word_count": 342,
        "language": "de"
    },
    "quality_metrics": {
        "matched_requirements": ["Python", "FastAPI", "PostgreSQL"],
        "red_flags": [],
        "extraction_quality": "high"
    },
    "metadata": {
        "job_posting_id": "job_xyz789",
        "profile_id": "profile_abc123",
        "generated_at": "2026-01-21T10:30:00Z"
    }
}
```

### Incomplete Profile Response
```json
{
    "status": "incomplete",
    "message": "Applicant profile required",
    "options": [
        "Provide profile file path",
        "Paste profile as text",
        "Use existing profile ID"
    ]
}
```

### Partial Failure (Bad extraction)
```json
{
    "status": "partial_failure",
    "warnings": [
        "Job posting extraction quality is MEDIUM",
        "Missing salary information",
        "Company details incomplete"
    ],
    "cover_letter": {
        "full_text": "...",
        "word_count": 287
    },
    "suggested_actions": [
        "Review for accuracy",
        "Add missing salary information manually",
        "Verify company name and address"
    ]
}
```

---

## Key Tools Used By `_xplaner_xrouter`

### 1. `route_to_agent` - Delegate to `_xworker`
```python
route_to_agent(
    target_agent="_xworker",
    job_name="cover_letter_writer",
    message_text={...payload...}
)
```

Direct desktop calls that target `_xworker` without a specialized workflow currently normalize to `job_name="generic_execution"` inside the desktop/runtime caller layer.

### 2. `dispatch_documents` / persistence tools - Access runtime stores
```python
dispatch_documents(scan_dir="/path/to/jobs")
# Combined with worker persistence and retrieval helpers
```

### 3. `read_document` - Load profile from file
```python
read_document(file_path="/path/to/cv.pdf")
```

### 4. `write_document` - Save cover letter
```python
write_document(
    content="Full cover letter text",
    path="/path/to/output/letter.txt"
)
```

---

## Default Behavior

| Parameter | Default | Override |
|-----------|---------|----------|
| Language | `de` (German) | Set `options.language: "en"` |
| Tone | `modern` | Set `options.tone: "professional"` \| `"creative"` |
| Max words | `350` | Set `options.max_words: 400` |
| Profile location | `AppData/applicant_profile.json` | Provide explicit path or ID |
| Output directory | `AppData/VSM_4_Data/cover_letters/` | Set in `options.output_dir` |

---

## Common Issues & Fixes

### Issue: "Profile not found"
**Solution 1:** Check default location
```
ALDE/ALDE/AppData/VSM_4_Data/applicant_profile.json
```

**Solution 2:** Provide explicit profile
```json
{
    "applicant_profile": {
        "source": "file",
        "value": "/your/cv/path.pdf"
    }
}
```

**Solution 3:** Use stored ID
```json
{
    "applicant_profile": {
        "source": "stored_id",
        "value": "profile_sha256hash"
    }
}
```

---

### Issue: "PDF extraction failed"
**Solution:** Use text input instead
```json
{
    "job_posting": {
        "source": "text",
        "value": "<paste job posting text here>"
    }
}
```

---

### Issue: "Cover letter word count wrong"
**Solution:** Adjust max_words
```json
{
    "options": {
        "max_words": 400
    }
}
```

---

## Performance Tips

1. **Reuse profiles** → Much faster (no parsing)
2. **Use batch mode** → Process multiple jobs efficiently
3. **Check extraction quality** → Understand which jobs need manual review
4. **Store successful profiles** → Avoid re-parsing

---

## Router Parallel Branch Runtime (Optional)

The runtime can parallelize multiple `route_to_agent` branches from `_xplaner_xrouter` and merge them in tool-call order.
This mode is opt-in.

### Environment Variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `ALDE_ROUTER_BRANCH_PARALLEL_ENABLED` | `0` | Enable parallel execution for multi-branch `route_to_agent` tool-call groups |
| `ALDE_ROUTER_BRANCH_PARALLEL_WORKERS` | `4` | Maximum worker concurrency (`1..16`) |
| `ALDE_ROUTER_BRANCH_TIMEOUT_SECONDS` | `30.0` | Per-branch timeout threshold used by timeout classification (`0.1..600`) |
| `ALDE_ROUTER_BRANCH_HARD_TIMEOUT_ENABLED` | `0` | Use hard timeout mode with subprocess termination for overdue branches |

### Recommended Presets

Balanced local profile:

```bash
source scripts/router_parallel_presets.env.example

export ALDE_ROUTER_BRANCH_PARALLEL_ENABLED=1
export ALDE_ROUTER_BRANCH_PARALLEL_WORKERS=2
export ALDE_ROUTER_BRANCH_TIMEOUT_SECONDS=30
export ALDE_ROUTER_BRANCH_HARD_TIMEOUT_ENABLED=0
```

Strict timeout profile:

```bash
source scripts/router_parallel_presets_strict.env.example

export ALDE_ROUTER_BRANCH_PARALLEL_ENABLED=1
export ALDE_ROUTER_BRANCH_PARALLEL_WORKERS=4
export ALDE_ROUTER_BRANCH_TIMEOUT_SECONDS=10
export ALDE_ROUTER_BRANCH_HARD_TIMEOUT_ENABLED=1
```

### Runtime Semantics

1. Result merge order remains deterministic (tool-call order), even when branches execute concurrently.
2. Partial failure is reported when at least one branch is `failed` or `timeout`.
3. In hard-timeout mode, timed-out branches are actively terminated via subprocess control.
4. In non-hard mode, timeout status is classification-based (no forced cancellation).

### Observability Fields

Runtime observability and desktop monitoring snapshots project router-branch telemetry through:

- `router_parallel_total_runs`
- `router_parallel_timeout_branches`
- `router_parallel_partial_fail_runs`
- `router_parallel_hard_timeout_runs`
- `router_parallel_hard_timeout_enabled`

---

## Next Steps

1. **Read**: [ORCHESTRATOR_USAGE.md](ORCHESTRATOR_USAGE.md) for detailed reference
2. **Explore**: [agents_registry.py](agents_registry.py) to see all agents
3. **Review**: [agents_config.py](agents_config.py) for runtime instructions, manifests, and workflow policy
4. **Test**: Run `python alde/agents_registry.py` to inspect registry

---

**Version**: 1.0  
**Last Updated**: 21 January 2026
