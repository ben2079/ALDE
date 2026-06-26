from __future__ import annotations

from typing import Any, Mapping, Protocol


class GraphViewBackendService(Protocol):
    def load_widget_snapshot(self, *, tool_id: str | None = None, source_uri: str | None = None) -> dict[str, Any]:
        ...

    def load_graph_view_state(
        self,
        graph_snapshot: Mapping[str, Any] | None,
        *,
        layout_spread: float = 1.0,
        selected_kind: str = "",
        selected_object_id: str = "",
    ) -> dict[str, Any]:
        ...

    def load_graph_view_state_from_payload(
        self,
        graph_snapshot: Mapping[str, Any] | None,
        payload: Mapping[str, Any] | None,
        *,
        layout_spread: float = 1.0,
        fallback_selected_kind: str = "",
        fallback_selected_object_id: str = "",
    ) -> dict[str, Any]:
        ...

    def load_graph_view_state_from_link(
        self,
        graph_snapshot: Mapping[str, Any] | None,
        url_value: Any,
        *,
        layout_spread: float = 1.0,
        fallback_selected_kind: str = "",
        fallback_selected_object_id: str = "",
    ) -> dict[str, Any]:
        ...

    def load_graph_item_center(
        self,
        graph_view_state: Mapping[str, Any] | None,
        *,
        kind: str,
        object_id: str,
    ) -> tuple[float, float] | None:
        ...


class ExtensionArtifactBackendService(Protocol):
    def load_connection_preview(self, *, source_uri: str | None = None) -> dict[str, Any]:
        ...

    def load_tool_runtime_manifest(self, *, tool_id: str | None = None, source_uri: str | None = None) -> dict[str, Any]:
        ...

    def load_runtime_artifact_bundle(
        self,
        *,
        tool_id: str | None = None,
        source_uri: str | None = None,
        manifest_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


def load_default_graph_backend_service() -> GraphViewBackendService:
    try:
        if __package__:
            from .agents_db import GraphViewService  # type: ignore
        else:
            from agents_db import GraphViewService  # type: ignore
    except ImportError as e:
        msg = str(e)
        if "attempted relative import" in msg or "no known parent package" in msg:
            from alde.agents_db import GraphViewService  # type: ignore  # noqa: E402
        else:
            raise
    return GraphViewService()


__all__ = [
    "GraphViewBackendService",
    "ExtensionArtifactBackendService",
    "load_default_graph_backend_service",
]
