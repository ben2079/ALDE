from __future__ import annotations

# 
# 



import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _events_path(base_dir: str | None = None) -> Path:
    root = Path(base_dir) if base_dir else _repo_root() / "AppData" / "generated"
    root.mkdir(parents=True, exist_ok=True)
    return root / "learning_events.jsonl"


def append_event(event_type: str, payload: dict[str, Any], base_dir: str | None = None) -> str:
    """Append one learning event as JSONL. Returns target file path."""
    target = _events_path(base_dir)
    entry = {
        "event_type": str(event_type),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    with open(target, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()

    return str(target)
