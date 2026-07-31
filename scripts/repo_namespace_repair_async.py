"""Start a cleanup+rebuild repo knowledge job and poll status until completion.

Usage:
    PYTHONPATH=. .venv-1/bin/python scripts/repo_namespace_repair_async.py \
        --root-dir /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt/ALDE/alde
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ALDE_ROOT = ROOT / "ALDE"
for candidate in (ROOT, ALDE_ROOT):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)

try:
    from ALDE.alde.agents_tools import repo_knowledge_worker
except ImportError:
    from alde.agents_tools import repo_knowledge_worker  # type: ignore


def main() -> int:
    parser = argparse.ArgumentParser(description="Run async namespace repair for repo knowledge")
    parser.add_argument("--root-dir", default=str(ROOT / "ALDE" / "alde"), help="Repo root to index")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=900.0,
        help="Fail fast when status polling exceeds this timeout.",
    )
    args = parser.parse_args()

    kickoff = repo_knowledge_worker(
        operation="repair_namespace",
        root_dir=str(Path(args.root_dir).expanduser().resolve()),
        extensions=[".py"],
        workers=max(1, int(args.workers)),
        run_async=True,
        delete_async=True,
    )
    print(json.dumps({"kickoff": kickoff}, ensure_ascii=False, indent=2))

    if not isinstance(kickoff, dict) or not kickoff.get("ok") or not kickoff.get("job_id"):
        return 1

    job_id = str(kickoff.get("job_id"))
    started_at = time.monotonic()
    while True:
        if time.monotonic() - started_at > max(1.0, float(args.timeout_seconds)):
            print(
                json.dumps(
                    {
                        "status": {
                            "ok": False,
                            "operation": "status",
                            "job_id": job_id,
                            "error": "timeout",
                            "timeout_seconds": float(args.timeout_seconds),
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        status_payload = repo_knowledge_worker(operation="status", job_id=job_id)
        print(json.dumps({"status": status_payload}, ensure_ascii=False, indent=2))
        if not isinstance(status_payload, dict) or not status_payload.get("ok"):
            return 1
        job = status_payload.get("job") if isinstance(status_payload.get("job"), dict) else {}
        state = str(job.get("status") or "")
        if state in {"completed", "failed"}:
            return 0 if state == "completed" else 1
        time.sleep(max(0.1, float(args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
