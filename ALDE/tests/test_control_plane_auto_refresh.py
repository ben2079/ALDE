import json
import os
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from alde.ai_ide_v1756 import ControlPlaneWidget


class ControlPlaneAutoRefreshTest(unittest.TestCase):
    def test_auto_refresh_defaults_to_disabled(self):
        widget = ControlPlaneWidget.__new__(ControlPlaneWidget)
        with patch.dict(os.environ, {}, clear=False):
            self.assertEqual(widget._resolve_auto_refresh_interval_ms(), 0)

    def test_auto_refresh_honors_explicit_env_value(self):
        widget = ControlPlaneWidget.__new__(ControlPlaneWidget)
        with patch.dict(os.environ, {"AI_IDE_CONTROL_PLANE_REFRESH_MS": "7500"}, clear=False):
            self.assertEqual(widget._resolve_auto_refresh_interval_ms(), 7500)


class ControlPlaneRuntimeGraphInitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_initialize_runtime_relation_graph_panel_skips_sync_engine_preflight(self):
        widget = ControlPlaneWidget.__new__(ControlPlaneWidget)
        widget.scheme = {}
        widget._latest_graph_tool_call_args = lambda: {}
        widget._apply_graph_call_query = lambda source_uri, call_args: str(source_uri or "")
        widget._schedule_runtime_state_save = lambda: None
        widget._load_runtime_relation_graph_engine_callable = lambda: (_ for _ in ()).throw(AssertionError("sync engine preflight should not run"))

        panel = QWidget()
        artifact_container = QWidget(panel)
        artifact_container.setLayout(QVBoxLayout())
        panel.setProperty("runtime_artifact_container", artifact_container)
        panel.setProperty("runtime_source_path", "agentsdb://127.0.0.1:2331/tools:graph_view")
        panel.setProperty("runtime_graph_tool_id", "graph_view")

        with patch("alde.ai_ide_v1756.ExtensionArtifactService") as artifact_service_class:
            artifact_widget = QWidget(artifact_container)
            artifact_service_class.return_value.load_object_widget.return_value = artifact_widget

            widget._initialize_runtime_relation_graph_panel(panel)

        payload = json.loads(str(panel.property("runtime_graph_payload") or "{}"))
        self.assertTrue(bool(panel.property("runtime_graph_initialized")))
        self.assertEqual(payload.get("status"), "deferred_widget_refresh")
        self.assertEqual(payload.get("tool_id"), "graph_view")


if __name__ == "__main__":
    unittest.main()
