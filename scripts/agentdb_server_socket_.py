from __future__ import annotations

import argparse
import json
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

    @staticmethod
    def _stringify_env_value(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (str, int, float)):
            return str(value)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    def _load_structured_variable_map(self) -> dict[str, str] | None:
        suffix = str(self._env_file_path.suffix or "").strip().lower()
        if suffix not in {".json", ".yaml", ".yml", ".toml"}:
            return None

        try:
            file_text = self._env_file_path.read_text(encoding="utf-8")
        except Exception:
            return {}

        parsed_payload: object
        if suffix == ".json":
            try:
                parsed_payload = json.loads(file_text)
            except Exception:
                return {}
        elif suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except Exception:
                return {}
            try:
                parsed_payload = yaml.safe_load(file_text)
            except Exception:
                return {}
        else:  # .toml
            toml_module = None
            try:
                import tomllib as toml_module  # type: ignore
            except Exception:
                try:
                    import tomli as toml_module  # type: ignore
                except Exception:
                    toml_module = None
            if toml_module is None:
                return {}
            try:
                parsed_payload = toml_module.loads(file_text)
            except Exception:
                return {}

        if not isinstance(parsed_payload, dict):
            return {}
        env_payload = parsed_payload.get("env") if isinstance(parsed_payload.get("env"), dict) else parsed_payload
        if not isinstance(env_payload, dict):
            env_payload = {}

        variable_map: dict[str, str] = {}
        for raw_key, raw_value in env_payload.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            value = self._stringify_env_value(raw_value)
            if value == "":
                continue
            variable_map[key] = value
        return variable_map

    def load_variable_map(self) -> dict[str, str]:
        variable_map: dict[str, str] = {}
        if not self._env_file_path.exists():
            return variable_map
        structured_variable_map = self._load_structured_variable_map()
        if structured_variable_map is not None:
            return structured_variable_map
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
            os.environ["AI_IDE_KNOWLEDGE_AGENTS_DB_URI"] = self._backend_uri
        if self._memory_image_path is not None:
            os.environ["AI_IDE_KNOWLEDGE_AGENTS_DB_IMAGE_PATH"] = self._memory_image_path
        return variable_map

    def _backend_available(self, backend_uri: str) -> bool:
        normalized_backend_uri = str(backend_uri or "").strip().lower()
        if not normalized_backend_uri:
            return False
        if normalized_backend_uri.startswith("agentsdb://"):
            return False
        if normalized_backend_uri.startswith(("agentsmem://", "memory://", "inmemory://")):
            return True
        return True
     
    def _ensure_runtime_backend(self) -> None:
        backend_uri = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI", "")).strip()
        if not backend_uri:
            backend_uri = "agentsmem://local"
        if self._backend_available(backend_uri):
            os.environ["AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI"] = backend_uri
            if backend_uri.lower().startswith(("agentsmem://", "memory://", "inmemory://")):
                preferred_image_path = str(os.getenv("AI_IDE_KNOWLEDGE_AGENTS_IMAGE_PATH", "")).strip()
                if not preferred_image_path:
                    preferred_image_path = str((REPO_ROOT / "AppData" / "agentsdb.json").resolve())
                os.environ.setdefault(
                    "AI_IDE_KNOWLEDGE_AGENTS_DB_IMAGE_PATH",
                    preferred_image_path,
                )
            return

        os.environ["AI_IDE_KNOWLEDGE_AGENTS_DB_URI"] = "agentsdb://localhost:2331"
        os.environ.setdefault(
            "AI_IDE_KNOWLEDGE_AGENTS_IMAGE_PATH",
            str((REPO_ROOT / "AppData" / "agentsdb.json").resolve()),
        )
        print("[WARNING] AgentsDB backend unavailable; agentsdb switched to in-memory backend.")

    def run(self) -> None:
        self.apply_env_file()
        self._ensure_runtime_backend()
        run_agentsdb_socket_server_from_env()


def _load_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the local agent server socket using ALDE env configuration.")
    parser.add_argument(
        "--env-file",
        default=os.getenv("AI_IDE_STARTUP_ENV_FILE_PATH", "ALDE/.env.json"),
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
