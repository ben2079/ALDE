import json
import unittest
from unittest.mock import patch

from alde.agents_db import GraphViewService
from alde.agents_factory import execute_adb_relation_graph_service
from alde.agents_tools import GraphToolService


class GraphConnectionPreviewTest(unittest.TestCase):
    def test_load_connection_preview_avoids_expensive_runtime_artifact_hashing(self):
        service = GraphViewService()
        with patch.object(service, "_compute_runtime_artifact_sha256", side_effect=AssertionError("expensive hashing should not run")):
            result = service.load_connection_preview(source_uri="agentsdb://127.0.0.1:2331/tools:graph_view_analysis")

        self.assertTrue(result.get("connections"))
        self.assertTrue(result.get("tools"))

    def test_graph_tool_error_payload_includes_status_fields(self):
        service = GraphToolService()
        payload = json.loads(service._error_result("agent_relation_graph_load_failed", detail="boom"))

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_status"], "agent_relation_graph_load_failed")
        self.assertEqual(payload["detail"], "boom")

    def test_execute_adb_relation_graph_service_preserves_namespace_query_args(self):
        with patch("alde.agents_factory.execute_tool_payload", return_value={"ok": True}) as mocked:
            execute_adb_relation_graph_service(
                backend_call={
                    "tool": "/tools:graph_view_analysis",
                    "source_uri": "agentsdb://127.0.0.1:2331/tools:graph_view_analysis",
                    "namespace_id": "ns_demo",
                    "namespace_scope": "all",
                    "cluster_by": "namespace",
                },
            )

        payload_args = mocked.call_args.args[1]
        resolved_source_uri = payload_args["backend_call"]["source_uri"]
        self.assertIn("namespace=ns_demo", resolved_source_uri)
        self.assertIn("scope=all", resolved_source_uri)
        self.assertIn("cluster=namespace", resolved_source_uri)


if __name__ == "__main__":
    unittest.main()
