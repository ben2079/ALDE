"""
Minimal MCP stdio server exposing the unified tool registry.

Methods handled:
- initialize
- tools/list
- tools/call

Transport: stdio (one JSON object per line).
"""

import json
import os
import sys
from typing import Any, Dict

try:
    from .agents_factory import execute_tool  # type: ignore
    from .agents_tools import get_tool_registry  # type: ignore
except Exception:
    try:
        from alde.agents_factory import execute_tool  # type: ignore
        from alde.agents_tools import get_tool_registry  # type: ignore
    except Exception:
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        from alde.agents_factory import execute_tool  # type: ignore
        from alde.agents_tools import get_tool_registry  # type: ignore


class McpRequestService:
    """Domain service for MCP request dispatch independent of transport."""

    def safe_serialize_object(self, object_payload: Any) -> Any:
        """Best-effort serialization; fallback to string for non-serializable objects."""
        try:
            json.dumps(object_payload)
            return object_payload
        except Exception:
            try:
                return str(object_payload)
            except Exception:
                return "<unserializable>"

    def load_initialize_result(self, _params: Dict[str, Any]) -> dict[str, Any]:
        return {
            "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "alde-local-mcp"},
            }
        }

    def load_tools_list_result(self, _params: Dict[str, Any]) -> dict[str, Any]:
        tool_registry = get_tool_registry()
        tools = [self.safe_serialize_object(spec) for _name, spec in tool_registry.items()]
        return {"result": {"tools": tools}}

    def load_tools_call_result(self, params: Dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not name:
            return {"error": "Missing tool name"}

        arguments_payload = params.get("arguments") or {}
        if isinstance(arguments_payload, str):
            try:
                arguments_payload = json.loads(arguments_payload)
            except Exception:
                arguments_payload = {}

        try:
            result, route_req = execute_tool(name, arguments_payload, params.get("id"))
            payload: Dict[str, Any] = {"content": self.safe_serialize_object(result)}
            if route_req:
                payload["route_request"] = self.safe_serialize_object(route_req)
            return {"result": payload}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"tool_error: {exc}"}

    def dispatch_object(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"error": "invalid_json"}

        method = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}


        handlers = {
            "initialize": self.load_initialize_result,
            "tools/list": self.load_tools_list_result,
            "tools/call": self.load_tools_call_result,
        }

        handler = handlers.get(str(method or ""))
        if not handler:
            return {"error": "method_not_implemented"}
        return handler(params)


MCP_REQUEST_SERVICE = McpRequestService()


def _response(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            _response({"error": "invalid_json"})
            continue

        _response(MCP_REQUEST_SERVICE.dispatch_object(payload))


if __name__ == "__main__":
    main()