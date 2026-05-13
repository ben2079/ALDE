"""One-off enrichment: upserts missing skill/tool/language/competency entities
and their relations for all job_postings in ns_alde_default."""
import hashlib
from datetime import datetime, timezone
from ALDE.alde.agents_db import AgentDbSocketRepository

NS = "ns_alde_default"
TENANT = "tenant_default"
NOW = datetime.now(timezone.utc).isoformat()


def slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace("/", "_").replace("-", "_").replace(".", "")


def rel_id(src: str, rtype: str, tgt: str) -> str:
    h = hashlib.sha256(f"{src}:{rtype}:{tgt}".encode()).hexdigest()[:16]
    return f"rel:job_posting:{h}"


def make_entity(etype: str, name: str) -> dict:
    key = slug(name)
    return {
        "id": f"ent:job_posting:{etype}:{key}",
        "tenant_id": TENANT,
        "namespace_id": NS,
        "entity_type": etype,
        "canonical_name": name,
        "external_key": f"{etype}:{key}",
        "correlation_id": f"enrichment:{etype}:{key}",
        "status": "active",
        "summary": f"{etype}: {name}",
        "attributes": {},
        "aliases": [],
        "created_at": NOW,
        "updated_at": NOW,
    }


def make_relation(job_id: str, rtype: str, target_id: str) -> dict:
    return {
        "id": rel_id(job_id, rtype, target_id),
        "tenant_id": TENANT,
        "namespace_id": NS,
        "source_entity_id": job_id,
        "target_entity_id": target_id,
        "relation_type": rtype,
        "direction": "directed",
        "weight": 0.9,
        "confidence": 0.9,
        "valid_from": None,
        "valid_to": None,
        "correlation_id": f"enrichment:{rtype}:{slug(target_id)}",
        "metadata": {"source_field": "enrichment", "mapped_from": "manual_upsert"},
        "evidence": [],
        "created_at": NOW,
        "updated_at": NOW,
    }


ENTITY_TYPE_MAP = {
    "skills": "skill",
    "tools": "tool",
    "languages": "language",
    "competencies": "competency",
}
RELATION_TYPE_MAP = {
    "skills": "requires_skill",
    "tools": "requires_tool",
    "languages": "requires_language",
    "competencies": "requires_competency",
}

JOB_DATA = {
    "automation_engineer": {
        "skills": ["Python", "Scripting", "Workflow Automation"],
        "tools": ["Docker", "Linux", "CI/CD"],
        "languages": ["English", "German"],
        "competencies": ["Process Automation", "System Integration"],
    },
    "autonomous_workflow_engineer": {
        "skills": ["Python", "LLM Integration", "Workflow Automation"],
        "tools": ["LangChain", "Docker"],
        "languages": ["English"],
        "competencies": ["Workflow Design", "AI Agent Engineering"],
    },
    "senior_data_engineer": {
        "skills": ["Python", "SQL", "Data Pipelines"],
        "tools": ["Apache Spark", "dbt", "Docker"],
        "languages": ["English", "German"],
        "competencies": ["Data Engineering", "ETL Design"],
    },
    "runtime_persisted_engineer": {
        "skills": ["Python", "Persistence", "REST API"],
        "tools": ["Redis", "SQLAlchemy", "Docker"],
        "languages": ["English"],
        "competencies": ["Backend Engineering", "Database Design"],
    },
    "platform_support_engineer": {
        "skills": ["Python", "Troubleshooting", "Linux Administration"],
        "tools": ["Docker", "Linux"],
        "languages": ["English", "German"],
        "competencies": ["Platform Engineering", "Incident Management"],
    },
    "remote_python_engineer": {
        "skills": ["Python", "REST API", "Testing"],
        "tools": ["FastAPI", "Docker"],
        "languages": ["English"],
        "competencies": ["Backend Engineering", "API Design"],
    },
    "knowledge_pipeline_engineer": {
        "skills": ["Python", "NLP", "Embeddings"],
        "tools": ["LangChain", "Qdrant", "Docker"],
        "languages": ["English"],
        "competencies": ["Knowledge Management", "ML Engineering"],
    },
    "erp_integration_engineer": {
        "skills": ["Python", "ERP Integration", "SAP"],
        "tools": ["SAP BTP", "REST API"],
        "languages": ["English", "German"],
        "competencies": ["ERP Systems", "System Integration"],
    },
    "python_engineer": {
        "skills": ["Python", "REST API", "Testing"],
        "tools": ["Docker", "FastAPI"],
        "languages": ["English"],
        "competencies": ["Software Engineering", "API Design"],
    },
    "fullstack_software_entwickler_m_w_d": {
        "skills": ["JavaScript", "TypeScript", "Python", "React"],
        "tools": ["React", "Node.js", "Docker"],
        "languages": ["German", "English"],
        "competencies": ["Frontend Engineering", "Backend Engineering"],
    },
}


def main() -> None:
    repo = AgentDbSocketRepository.create_from_uri("agentsdb://127.0.0.1:2331", "alde_knowledge")
    ec, rc = 0, 0
    for job_key, categories in JOB_DATA.items():
        job_id = f"ent:job_posting:job_posting:{job_key}"
        for category, items in categories.items():
            rtype = RELATION_TYPE_MAP[category]
            etype = ENTITY_TYPE_MAP[category]
            for name in items:
                e = make_entity(etype, name)
                repo.upsert_object("entity", e["id"], e)
                ec += 1
                r = make_relation(job_id, rtype, e["id"])
                repo.upsert_object("relation", r["id"], r)
                rc += 1
                print(f"  {job_key} --[{rtype}]--> {name}")
    print(f"\nDone: {ec} entities, {rc} relations upserted.")


if __name__ == "__main__":
    main()
