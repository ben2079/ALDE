from __future__ import annotations


# Maintainer contact: see repository README.

"""Agent registry.

This module must be safe to import (no side effects). It only defines
`AGENTS_REGISTRY` (agent configs: model, system prompt, tools).
"""

try:
    from .agents_config import get_agents_registry_data  # type: ignore
except Exception:
    from alde.agents_config import get_agents_registry_data  # type: ignore


class AgentRegistryService:
    def load_object_registry(self, object_name: str | None = None) -> dict[str, dict]:
        registry_data = get_agents_registry_data()
        if not object_name:
            return registry_data

        selected_registry = registry_data.get(object_name)
        if not selected_registry:
            return {}
        return {object_name: selected_registry}


AGENT_REGISTRY_SERVICE = AgentRegistryService()

# Public registry consumed by agents_factory.execute_route_to_agent.
AGENTS_REGISTRY: dict[str, dict] = AGENT_REGISTRY_SERVICE.load_object_registry()


# Maintainer contact: see repository README.

"""Agent registry.

This module must be safe to import (no side effects). It only defines
`AGENTS_REGISTRY` (agent configs: model, system prompt, tools).
"""

try:
    from .agents_config import get_agents_registry_data  # type: ignore
except Exception:
    from alde.agents_config import get_agents_registry_data  # type: ignore


class AgentRegistryService:
    def load_object_registry(self, object_name: str | None = None) -> dict[str, dict]:
        registry_data = get_agents_registry_data()
        if not object_name:
            return registry_data

        selected_registry = registry_data.get(object_name)
        if not selected_registry:
            return {}
        return {object_name: selected_registry}


AGENT_REGISTRY_SERVICE = AgentRegistryService()

# Public registry consumed by agents_factory.execute_route_to_agent.
AGENTS_REGISTRY: dict[str, dict] = AGENT_REGISTRY_SERVICE.load_object_registry()