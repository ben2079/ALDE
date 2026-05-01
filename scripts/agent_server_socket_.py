from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path
from typing import Mapping



REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "ALDE"
for import_root in (REPO_ROOT, PACKAGE_ROOT):
    import_root_text = str(import_root)
    if import_root_text not in sys.path:
        sys.path.insert(0, import_root_text)

from ALDE.alde.agents_db import run_agentsdb_socket_server_from_env


class EnvFileService:
    def __init__(self, env_file_path: Path) -> None:
        self._env_file_path = env_file_path

    def load_variable_map(self) -> dict[str, str]:
        variable_map: dict[str, str] = {}
        if not self._env_file_path.exists():
            return variable_map
        for raw_line in self._env_file_path.read_text(encoding="utf-8").splitlines():
            stripped_line = raw_line.strip()
            if not stripped_line or stripped_line.startswith("#") or "=" not in stripped_line:
                continue
            key, value = stripped_line.split("=", 1)
            normalized_key = key.strip()
            if not normalized_key:
                continue
            normalized_value = value.strip()
            if normalized_value and normalized_value[0] in {'"', "'"}:
                try:
                    normalized_value = str(shlex.split(f"x={normalized_value}", posix=True)[0]).split("=", 1)[1]
                except Exception:
                    normalized_value = normalized_value.strip("\"'")
            variable_map[normalized_key] = normalized_value
        return variable_map


class AgentDbSocketServerRunner:
    def __init__(
        self,
        env_file_path: Path,
        override_env: bool = False,
        backend_uri: str | None = None,
        memory_image_path: str | None = None,
    ) -> None:
        self._env_file_path = env_file_path
        self._override_env = bool(override_env)
        self._backend_uri = str(backend_uri or "").strip() or None
        self._memory_image_path = str(memory_image_path or "").strip() or None

    def apply_env_file(self) -> dict[str, str]:
        env_service = EnvFileService(self._env_file_path)
        variable_map = env_service.load_variable_map()
        for key, value in variable_map.items():
            if self._override_env or key not in os.environ:
                os.environ[key] = value
        if self._backend_uri is not None:
            os.environ["****AGENTS_DB_URI"] = self._backend_uri
        if self._memory_image_path is not None:
            os.environ["****AGENTS_DB_IMAGE_PATH"] = self._memory_image_path
        return variable_map

    def _backend_available(self, backend_uri: str) -> bool:
        normalized_backend_uri = str(backend_uri or "").strip().lower()
        if normalized_backend_uri.startswith(("agentsdb://", "memory://", "inmemory://")):
            return True
     
    def _ensure_runtime_backend(self) -> None:
        backend_uri = str(os.getenv("****AGENTS_DB_BACKEND_URI", "")).strip()
        if not backend_uri:
            backend_uri = "agentsdb://localhost:2331"
        if self._backend_available(backend_uri):
            os.environ["****AGENTS_DB_BACKEND_URI"] = backend_uri
            if backend_uri.lower().startswith(("agentsmem://", "memory://", "inmemory://")):
                os.environ.setdefault(
                    "****AGENTS_DB_IMAGE_PATH",
                    str((REPO_ROOT / "AppData" / "agentsdb_image.json").resolve()),
                )
            return

        os.environ["****AGENTS_DB_URI"] = "agentsdb://localhost:2331"
        os.environ.setdefault(
            "****AGENTS_IMAGE_PATH",
            str((REPO_ROOT / "AppData" / "agentsdb_image.json").resolve()),
        )
        print("[WARNING] AgentsDB backend unavailable; agentsdb switched to in-memory backend.")

    def run(self) -> None:
        self.apply_env_file()
        self._ensure_runtime_backend()
        run_agentsdb_socket_server_from_env()


def _load_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the local agent server socket using ALDE .env configuration.")
    parser.add_argument(
        "--env-file",
        default=os.getenv("AI_IDE_STARTUP_ENV_FILE_PATH", "ALDE/.env"),
        help="Path to env file used for startup variables.",
    )
    parser.add_argument(
        "--override-env",
        action="store_true",
        help="Override already exported shell variables with values from --env-file.",
    )
    parser.add_argument(
        "--backend-uri",
        default="",
        help="Optional backend URI override (e.g. mongodb://... or agentsmem://local).",
    )
    parser.add_argument(
        "--memory-image-path",
        default="",
        help="Optional snapshot file used when running with in-memory backend.",
    )
    return parser.parse_args()


def main() -> int:
    args = _load_args()
    env_file_path = Path(args.env_file)
    if not env_file_path.is_absolute():
        env_file_path = (REPO_ROOT / env_file_path).resolve()
    runner = AgentDbSocketServerRunner(
        env_file_path=env_file_path,
        override_env=bool(args.override_env),
        backend_uri=str(args.backend_uri or "").strip() or None,
        memory_image_path=str(args.memory_image_path or "").strip() or None,
    )
    runner.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
