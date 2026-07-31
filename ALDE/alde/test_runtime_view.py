from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ALDE_Projekt.ALDE.alde.agents_policy_store import append_event
from ALDE_Projekt.ALDE.alde.agents_runtime_metrics import load_runtime_observability_snapshot
from ALDE_Projekt.ALDE.alde.agents_runtime_view import export_control_plane_snapshot, export_runtime_view, load_desktop_monitoring_snapshot, load_operator_status_snapshot, load_runtime_trace, load_runtime_view


class TestRuntimeView(unittest.TestCase):
    def test_runtime_view_exports_session_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            append_event(
                "query",
                {
                    "event_id": "query-1",
                    "session_id": "session-view",
                    "agent": "_xplaner_xrouter",
                    "tool": "vectordb",
                    "query_text": "agent routing",
                    "timestamp": "2026-01-01T10:00:00Z",
                    "k": 5,
                },
                base_dir=temp_dir,
            )
            append_event(
                "outcome",
                {
                    "event_id": "outcome-1",
                    "query_event_id": "query-1",
                    "session_id": "session-view",
                    "agent": "_xplaner_xrouter",
                    "tool": "vectordb",
                    "timestamp": "2026-01-01T10:00:01Z",
                    "success": True,
                    "latency_ms": 18,
                    "reward": 0.5,
                    "result_count": 2,
                },
                base_dir=temp_dir,
            )

            history_entries = [
                {
                    "message-id": 1,
                    "role": "assistant",
                    "content": "running retrieval",
                    "thread-name": "chat",
                    "thread-id": "thread-view",
                    "assistant-name": "_xplaner_xrouter",
                    "tool_calls": [
                        {
                            "id": "call-view-1",
                            "type": "function",
                            "function": {"name": "vectordb", "arguments": "{}"},
                        }
                    ],
                    "data": {
                        "workflow": {
                            "phase": "tool_call_start",
                            "workflow_name": "dispatcher",
                            "agent_label": "_xplaner_xrouter",
                            "scope_key": "session-view",
                            "current_state": "retrieving",
                            "event": {"kind": "tool", "name": "vectordb", "payload": {}},
                        }
                    },
                },
                {
                    "message-id": 2,
                    "role": "tool",
                    "content": "[]",
                    "thread-name": "chat",
                    "thread-id": "thread-view",
                    "assistant-name": "_xplaner_xrouter",
                    "tool_call_id": "call-view-1",
                    "name": "vectordb",
                    "data": {
                        "workflow": {
                            "phase": "tool_result",
                            "workflow_name": "dispatcher",
                            "agent_label": "_xplaner_xrouter",
                            "scope_key": "session-view",
                            "current_state": "planner_retry_pending",
                            "retry": {
                                "attempt_count": 1,
                                "remaining_attempts": 1,
                                "next_delay_seconds": 5,
                                "exhausted": False,
                            },
                            "event": {
                                "kind": "state",
                                "name": "retry_requested",
                                "payload": {
                                    "target_agent": "_xworker",
                                    "protocol": "agent_handoff_v1",
                                    "correlation_id": "corr-1",
                                },
                            },
                        }
                    },
                },
            ]

            runtime_view = load_runtime_view(
                base_dir=temp_dir,
                session_id="session-view",
                history_entries=history_entries,
            )

            self.assertEqual(runtime_view["session_count"], 1)
            self.assertEqual(runtime_view["sessions"][0]["session_id"], "session-view")
            self.assertEqual(runtime_view["sessions"][0]["metrics"]["event_count"], runtime_view["sessions"][0]["event_count"])
            self.assertEqual(runtime_view["sessions"][0]["retry"]["requested_count"], 1)
            self.assertEqual(runtime_view["sessions"][0]["handoffs"]["count"], 1)
            self.assertEqual(runtime_view["sessions"][0]["latest_workflow_state"]["status"], "scheduled")
            self.assertEqual(runtime_view["trace_count"], 2)
            self.assertEqual(runtime_view["trace"][0]["trace_kind"], "assistant_tool_call")
            self.assertEqual(runtime_view["trace"][0]["tool_calls"][0]["function"]["name"], "vectordb")
            self.assertEqual(runtime_view["trace"][1]["trace_kind"], "tool_result")
            self.assertEqual(runtime_view["trace"][1]["handoff"]["target_agent"], "_xworker")

            trace_view = load_runtime_trace(
                session_id="session-view",
                history_entries=history_entries,
            )
            self.assertEqual(len(trace_view), 2)
            self.assertEqual(trace_view[1]["workflow_payload"]["target_agent"], "_xworker")

            exported_path = export_runtime_view(
                base_dir=temp_dir,
                session_id="session-view",
                history_entries=history_entries,
            )
            with open(exported_path, "r", encoding="utf-8") as exported_file:
                exported_view = json.load(exported_file)

            self.assertEqual(exported_view["session_count"], 1)
            self.assertEqual(exported_view["sessions"][0]["handoffs"]["count"], 1)
            self.assertEqual(len(exported_view["trace"]), 2)
            self.assertEqual(exported_view["trace"][0]["tool_calls"][0]["function"]["name"], "vectordb")
            self.assertEqual(exported_view["trace"][1]["handoff"]["protocol"], "agent_handoff_v1")

            with patch(
                "alde.control_plane_runtime.QUEUE_HEALTH_SERVICE.load_queue_health",
                return_value=("inmemory", True),
            ), patch(
                "alde.control_plane_runtime.WORKFLOW_VALIDATION_SERVICE.load_report",
                return_value={
                    "valid": True,
                    "errors": [],
                    "valid_count": 4,
                    "invalid_count": 0,
                },
            ):
                snapshot = load_runtime_observability_snapshot(
                    base_dir=temp_dir,
                    session_id="session-view",
                    history_entries=history_entries,
                )

            self.assertTrue(snapshot["healthy"])
            self.assertEqual(snapshot["session_count"], 1)
            self.assertEqual(snapshot["queue_backend"], "inmemory")
            self.assertEqual(snapshot["latest_sessions"][0]["session_id"], "session-view")
            self.assertIn("handoff", snapshot["metrics"]["event_family_counts"])
            self.assertTrue(snapshot["validation"]["valid"])

            with patch(
                "alde.control_plane_runtime.QUEUE_HEALTH_SERVICE.load_queue_health",
                return_value=("inmemory", True),
            ), patch(
                "alde.control_plane_runtime.WORKFLOW_VALIDATION_SERVICE.load_report",
                return_value={
                    "valid": True,
                    "errors": [],
                    "valid_count": 4,
                    "invalid_count": 0,
                },
            ):
                monitoring_snapshot = load_desktop_monitoring_snapshot(
                    base_dir=temp_dir,
                    session_id="session-view",
                    history_entries=history_entries,
                )

            self.assertTrue(monitoring_snapshot["healthy"])
            self.assertEqual(monitoring_snapshot["queue_backend"], "inmemory")
            self.assertEqual(monitoring_snapshot["active_session_count"], 1)
            self.assertEqual(monitoring_snapshot["latest_session"]["session_id"], "session-view")
            self.assertEqual(monitoring_snapshot["validation_issue_count"], 0)
            self.assertEqual(monitoring_snapshot["trace_filter_options"]["handoffs"][0], "_xplaner_xrouter->_xworker")
            self.assertEqual(monitoring_snapshot["snapshot_kind"], "monitoring")
            self.assertEqual(monitoring_snapshot["summary_metrics"]["session_count"], 1)
            self.assertEqual(monitoring_snapshot["recent_item_count"], len(monitoring_snapshot["recent_items"]))
            self.assertGreaterEqual(monitoring_snapshot["recent_item_count"], 4)
            self.assertEqual(monitoring_snapshot["recent_items"][0]["source"], "runtime_monitoring")
            self.assertIn("runtime_monitoring", monitoring_snapshot["recent_item_filters"]["action_groups"])

            with patch(
                "alde.control_plane_runtime.QUEUE_HEALTH_SERVICE.load_queue_health",
                return_value=("inmemory", True),
            ), patch(
                "alde.control_plane_runtime.WORKFLOW_VALIDATION_SERVICE.load_report",
                return_value={
                    "valid": True,
                    "errors": [],
                    "valid_count": 4,
                    "invalid_count": 0,
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_dispatcher_status",
                return_value={
                    "dispatcher_db_path": "/tmp/dispatcher.json",
                    "dispatcher_healthy": True,
                    "dispatcher_error": None,
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_mcp_config_path",
                return_value=Path(temp_dir) / "mcp_servers.json",
            ):
                (Path(temp_dir) / "mcp_servers.json").write_text("{}", encoding="utf-8")
                control_plane_path = export_control_plane_snapshot(
                    base_dir=temp_dir,
                    session_id="session-view",
                    history_entries=history_entries,
                    mcp_probe={
                        "ok": True,
                        "returncode": 0,
                        "stdout": "probe ok",
                        "stderr": "",
                    },
                    recent_action_entries=[
                        {
                            "timestamp": "2026-04-01T11:00:00+00:00",
                            "title": "Queue probe",
                            "summary": "Queue probe: backend=inmemory healthy=True",
                            "source": "desktop_operator",
                            "status": "pass",
                        }
                    ],
                )

            with open(control_plane_path, "r", encoding="utf-8") as exported_file:
                control_plane_snapshot = json.load(exported_file)

            self.assertEqual(control_plane_snapshot["snapshot_kind"], "control_plane_bundle")
            self.assertEqual(control_plane_snapshot["monitoring"]["snapshot_kind"], "monitoring")
            self.assertEqual(control_plane_snapshot["operator"]["snapshot_kind"], "operator")
            self.assertIn("recent_item_filters", control_plane_snapshot)
            self.assertEqual(control_plane_snapshot["operator"]["recent_actions"][0]["audit_type"], "probe")
            self.assertEqual(control_plane_snapshot["operator"]["recent_actions"][0]["action_group"], "queue")

    def test_operator_status_snapshot_projects_shared_control_plane_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mcp_config_path = Path(temp_dir) / "mcp_servers.json"
            mcp_config_path.write_text("{}", encoding="utf-8")

            with patch(
                "alde.control_plane_runtime.QUEUE_HEALTH_SERVICE.load_queue_health",
                return_value=("inmemory", True),
            ), patch(
                "alde.control_plane_runtime.WORKFLOW_VALIDATION_SERVICE.load_report",
                return_value={
                    "valid": True,
                    "errors": [],
                    "valid_count": 4,
                    "invalid_count": 0,
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_dispatcher_status",
                return_value={
                    "dispatcher_db_path": "/tmp/dispatcher.json",
                    "dispatcher_healthy": True,
                    "dispatcher_error": None,
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_agentsdb_status",
                return_value={
                    "agentsdb_uri": "agentsdb://localhost:2331",
                    "agentsdb_database_name": "alde_knowledge",
                    "agentsdb_endpoint": "localhost:2331",
                    "agentsdb_healthy": None,
                    "agentsdb_error": "",
                    "agentsdb_detail": "socket @ localhost:2331",
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_mcp_config_path",
                return_value=mcp_config_path,
            ), patch(
                "alde.control_plane_runtime.REPO_WORKER_MONITORING_SERVICE.load_snapshot",
                return_value={
                    "ok": False,
                    "error": "jobs_file_missing",
                    "jobs_path": "/tmp/repo_worker_jobs.json",
                    "jobs_total": 0,
                    "active_job_count": 0,
                    "failed_job_count": 0,
                    "stale_active_job_count": 0,
                    "max_active_heartbeat_age_seconds": 0.0,
                },
            ):
                snapshot = load_operator_status_snapshot(
                    mcp_probe={
                        "ok": True,
                        "returncode": 0,
                        "stdout": "probe ok",
                        "stderr": "",
                    },
                    recent_action_entries=[
                        {
                            "timestamp": "2026-04-01T11:00:00+00:00",
                            "title": "Queue probe",
                            "summary": "Queue probe: backend=inmemory healthy=True",
                            "source": "desktop_operator",
                            "status": "pass",
                        }
                    ],
                )

            self.assertTrue(snapshot["healthy"])
            self.assertEqual(snapshot["queue_backend"], "inmemory")
            self.assertTrue(snapshot["dispatcher_healthy"])
            self.assertEqual(snapshot["validation_issue_count"], 0)
            self.assertTrue(snapshot["mcp_config_present"])
            self.assertTrue(snapshot["mcp_probe"]["ok"])
            self.assertEqual(snapshot["snapshot_kind"], "operator")
            self.assertEqual(snapshot["service_count"], 6)
            self.assertEqual(snapshot["healthy_service_count"], 4)
            self.assertEqual(snapshot["attention_count"], 0)
            self.assertEqual(
                [row["title"] for row in snapshot["service_rows"]],
                ["Queue", "AgentsDB", "Dispatcher", "MCP", "Workflow Validation", "Repo Worker"],
            )
            self.assertEqual(snapshot["recent_item_count"], 1)
            self.assertEqual(snapshot["recent_actions"][0]["title"], "Queue probe")
            self.assertEqual(snapshot["recent_actions"][0]["status"], "pass")
            self.assertEqual(snapshot["recent_actions"][0]["audit_type"], "probe")
            self.assertEqual(snapshot["recent_actions"][0]["action_group"], "queue")
            self.assertEqual(snapshot["audit_summary"]["status_counts"]["pass"], 1)
            self.assertIn("probe", snapshot["recent_action_filters"]["audit_types"])
            self.assertIn("queue", snapshot["recent_action_filters"]["action_groups"])

    def test_operator_status_snapshot_projects_alerts_for_degraded_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mcp_config_path = Path(temp_dir) / "mcp_servers.json"
            mcp_config_path.write_text("{}", encoding="utf-8")

            with patch(
                "alde.control_plane_runtime.QUEUE_HEALTH_SERVICE.load_queue_health",
                return_value=("rq", False),
            ), patch(
                "alde.control_plane_runtime.WORKFLOW_VALIDATION_SERVICE.load_report",
                return_value={
                    "valid": False,
                    "errors": ["workflow-a: missing transition", "workflow-b: invalid tool"],
                    "valid_count": 2,
                    "invalid_count": 2,
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_dispatcher_status",
                return_value={
                    "dispatcher_db_path": "/tmp/dispatcher.json",
                    "dispatcher_healthy": False,
                    "dispatcher_error": "dispatcher locked",
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_agentsdb_status",
                return_value={
                    "agentsdb_uri": "agentsdb://localhost:2331",
                    "agentsdb_database_name": "alde_knowledge",
                    "agentsdb_endpoint": "localhost:2331",
                    "agentsdb_healthy": None,
                    "agentsdb_error": "",
                    "agentsdb_detail": "socket @ localhost:2331",
                },
            ), patch(
                "alde.control_plane_runtime.OPERATOR_STATUS_SERVICE.load_mcp_config_path",
                return_value=mcp_config_path,
            ), patch(
                "alde.control_plane_runtime.REPO_WORKER_MONITORING_SERVICE.load_snapshot",
                return_value={
                    "ok": False,
                    "error": "jobs_file_missing",
                    "jobs_path": "/tmp/repo_worker_jobs.json",
                    "jobs_total": 0,
                    "active_job_count": 0,
                    "failed_job_count": 0,
                    "stale_active_job_count": 0,
                    "max_active_heartbeat_age_seconds": 0.0,
                },
            ):
                snapshot = load_operator_status_snapshot(
                    mcp_probe={
                        "ok": False,
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "probe failed",
                    },
                    recent_action_entries=[
                        "10:00:00 | Queue probe failed: backend=rq healthy=False",
                        "10:01:00 | Dispatcher repair completed: backup=/tmp/dispatcher.bak",
                    ],
                )

            self.assertFalse(snapshot["healthy"])
            self.assertEqual(snapshot["healthy_service_count"], 0)
            self.assertGreaterEqual(snapshot["attention_count"], 4)
            self.assertIn("Queue backend is unreachable.", snapshot["alerts"])
            self.assertIn("dispatcher locked", snapshot["alerts"])
            self.assertIn("probe failed", snapshot["alerts"])
            self.assertIn("workflow-a: missing transition", snapshot["alerts"])
            self.assertEqual(snapshot["service_rows"][0]["state"], "fail")
            self.assertEqual(snapshot["service_rows"][2]["state"], "fail")
            self.assertEqual(snapshot["recent_item_count"], 2)
            self.assertEqual(snapshot["recent_actions"][0]["audit_type"], "repair")
            self.assertEqual(snapshot["recent_actions"][0]["action_group"], "dispatcher")
            self.assertEqual(snapshot["recent_actions"][0]["status"], "pass")
            self.assertEqual(snapshot["recent_actions"][1]["status"], "fail")
            self.assertIn("repair", snapshot["recent_action_filters"]["audit_types"])
            self.assertIn("queue", snapshot["recent_action_filters"]["action_groups"])

    def test_runtime_observability_snapshot_projects_router_parallel_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "alde.control_plane_runtime.QUEUE_HEALTH_SERVICE.load_queue_health",
                return_value=("inmemory", True),
            ), patch(
                "alde.control_plane_runtime.WORKFLOW_VALIDATION_SERVICE.load_report",
                return_value={
                    "valid": True,
                    "errors": [],
                    "valid_count": 3,
                    "invalid_count": 0,
                },
            ), patch(
                "alde.agents_factory.get_router_parallel_runtime_status",
                return_value={
                    "total_parallel_runs": 7,
                    "total_parallel_branches": 19,
                    "total_completed_branches": 15,
                    "total_failed_branches": 2,
                    "total_timeout_branches": 2,
                    "total_partial_fail_runs": 3,
                    "total_hard_timeout_runs": 2,
                    "config": {
                        "parallel_enabled": True,
                        "hard_timeout_enabled": True,
                        "configured_worker_count": 4,
                        "configured_timeout_seconds": 0.2,
                        "max_parallel_workers": 16,
                    },
                },
            ):
                snapshot = load_runtime_observability_snapshot(
                    base_dir=temp_dir,
                    session_id="session-router-telemetry",
                    history_entries=[],
                )
                monitoring_snapshot = load_desktop_monitoring_snapshot(
                    base_dir=temp_dir,
                    session_id="session-router-telemetry",
                    history_entries=[],
                )

            self.assertIn("router_parallel_status", snapshot)
            self.assertTrue(snapshot["router_parallel_enabled"])
            self.assertEqual(snapshot["router_parallel_total_runs"], 7)
            self.assertEqual(snapshot["router_parallel_timeout_branches"], 2)
            self.assertEqual(snapshot["router_parallel_partial_fail_runs"], 3)
            self.assertEqual(snapshot["router_parallel_hard_timeout_runs"], 2)
            self.assertTrue(snapshot["router_parallel_hard_timeout_enabled"])
            self.assertEqual(snapshot["router_parallel_status"]["total_parallel_branches"], 19)

            self.assertEqual(monitoring_snapshot["summary_metrics"]["router_parallel_runs"], 7)
            self.assertEqual(monitoring_snapshot["summary_metrics"]["router_parallel_timeout_branches"], 2)
            self.assertEqual(monitoring_snapshot["summary_metrics"]["router_parallel_partial_fail_runs"], 3)
            self.assertEqual(monitoring_snapshot["summary_metrics"]["router_parallel_hard_timeout_runs"], 2)
            self.assertTrue(monitoring_snapshot["summary_metrics"]["router_parallel_hard_timeout_enabled"])
            self.assertIn("router_parallel_status", monitoring_snapshot)


if __name__ == "__main__":
    unittest.main()