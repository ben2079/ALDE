import time
import unittest

from PySide6.QtWidgets import QApplication

from alde.widget_artifacts.relation_graph_artifact import RuntimeWidget


class _SlowBackendService:
    def __init__(self) -> None:
        self.call_count = 0

    def load_widget_snapshot(self, *, tool_id: str, source_uri: str) -> dict:
        self.call_count += 1
        time.sleep(0.2)
        return {
            "tool_id": tool_id,
            "source_uri": source_uri,
            "view_kind": "relations_graph",
            "status_text": "ok",
            "message": "",
            "detail_html": "<p>ok</p>",
            "nodes": [],
            "edges": [],
        }

    def load_graph_view_state(self, snapshot_payload, **kwargs) -> dict:
        return {
            "detail_html": "<p>ready</p>",
            "render_commands": [],
        }


class RelationGraphArtifactBackgroundRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_refresh_object_returns_before_backend_finishes(self) -> None:
        backend = _SlowBackendService()
        widget = RuntimeWidget(object_name="graph", source_uri="", graph_service=backend)
        widget._refresh_inflight = False

        started_at = time.perf_counter()
        widget.refresh_object(fit_view=False)
        elapsed = time.perf_counter() - started_at

        self.assertLess(elapsed, 0.1)
        self.assertTrue(widget._refresh_inflight)
        self.assertEqual(backend.call_count, 2)


if __name__ == "__main__":
    unittest.main()
