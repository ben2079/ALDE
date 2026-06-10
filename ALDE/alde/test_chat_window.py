from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QTabWidget, QWidget


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from alde.ai_ide_v1756 import (
    ChatEditorPanel,
    ChatSegment,
    ChatWindow,
    CodeViewer,
    ControlPlaneWidget,
    ExtensionsWorkspaceWidget,
    MsgWidget,
)


APP = QApplication.instance() or QApplication([])


class _FakeFontMetrics:
    def horizontalAdvance(self, value: str) -> int:
        return len(str(value or "")) * 7


class _FakeTabBar:
    def __init__(self, labels: list[str], *, tab_width: int = 40) -> None:
        self._labels = list(labels)
        self._tab_width = max(1, int(tab_width))
        self._tab_data: dict[int, str] = {}

    def count(self) -> int:
        return len(self._labels)

    def tabRect(self, _index: int) -> QRect:
        return QRect(0, 0, self._tab_width, 20)

    def fontMetrics(self) -> _FakeFontMetrics:
        return _FakeFontMetrics()

    def tabText(self, index: int) -> str:
        return self._labels[index]

    def setTabText(self, index: int, value: str) -> None:
        self._labels[index] = str(value or "")

    def tabData(self, index: int) -> str | None:
        return self._tab_data.get(index)

    def setTabData(self, index: int, value: str) -> None:
        self._tab_data[index] = str(value or "")


class _FakeTabs:
    def __init__(self, labels: list[str], *, tab_width: int = 40) -> None:
        self._tab_bar = _FakeTabBar(labels, tab_width=tab_width)
        self._tooltips: dict[int, str] = {}

    def count(self) -> int:
        return self._tab_bar.count()

    def tabBar(self) -> _FakeTabBar:
        return self._tab_bar

    def tabText(self, index: int) -> str:
        return self._tab_bar.tabText(index)

    def setTabText(self, index: int, value: str) -> None:
        self._tab_bar.setTabText(index, value)

    def setTabToolTip(self, index: int, value: str) -> None:
        self._tooltips[index] = str(value or "")

    def close(self) -> None:
        return


class _DummyExtensionsTabMarquee:
    _start_tab_hover_marquee = ExtensionsWorkspaceWidget._start_tab_hover_marquee
    _stop_tab_hover_marquee = ExtensionsWorkspaceWidget._stop_tab_hover_marquee

    def __init__(self) -> None:
        self.extensions_tabs = _FakeTabs(
            [
                "VeryLongExtensionTabLabelThatNeedsMarquee",
                "SecondTab",
                "ThirdTab",
            ],
            tab_width=40,
        )
        self._hover_tab_index = -1
        self._hover_tab_base_text = ""
        self._hover_tab_phase = 0
        self._hover_tab_marquee_timer = QTimer()


class _DummyControlPlaneTabMarquee:
    _format_control_plane_tab_text = ControlPlaneWidget._format_control_plane_tab_text
    _control_plane_tab_full_text = ControlPlaneWidget._control_plane_tab_full_text
    _set_control_plane_tab_text = ControlPlaneWidget._set_control_plane_tab_text
    _start_control_plane_tab_hover_marquee = ControlPlaneWidget._start_control_plane_tab_hover_marquee
    _stop_control_plane_tab_hover_marquee = ControlPlaneWidget._stop_control_plane_tab_hover_marquee

    def __init__(self) -> None:
        self.tabs = _FakeTabs(
            [
                "VeryLongControlPlaneTabLabelThatNeedsMarquee",
                "SecondTab",
                "ThirdTab",
            ],
            tab_width=40,
        )
        self._tab_bar_label_max_chars = 10
        self._control_tab_hover_index = -1
        self._control_tab_hover_base_text = ""
        self._control_tab_hover_phase = 0
        self._control_tab_hover_marquee_timer = QTimer()


class _DummyControlPlaneStyleHarness:
    _runtime_tab_page_background_color = ControlPlaneWidget._runtime_tab_page_background_color
    _apply_runtime_tab_page_scheme = ControlPlaneWidget._apply_runtime_tab_page_scheme
    _apply_builder_panel_scheme = ControlPlaneWidget._apply_builder_panel_scheme

    def __init__(self) -> None:
        self.scheme = {
            "col7": "#0b0b0b",
            "col9": "#101010",
            "col10": "#303030",
            "col2": "#6280ff",
        }
        self._runtime_tab_records: dict[QWidget, dict[str, str]] = {}


class TestChatWindowSegmentation(unittest.TestCase):
    def test_fenced_blocks_keep_language_and_indentation(self) -> None:
        raw = "Intro\n```python\n    def demo():\n        return 1\n```\nOutro"

        segments = ChatWindow._split_segments(raw)

        self.assertEqual(
            segments,
            [
                ChatSegment(kind="text", language="", block="Intro"),
                ChatSegment(kind="editor", language="python", block="    def demo():\n        return 1"),
                ChatSegment(kind="text", language="", block="Outro"),
            ],
        )

    def test_plain_python_blocks_are_promoted_to_editor(self) -> None:
        raw = "def demo():\n    return 1\n\nclass Box:\n    pass"

        segments = ChatWindow._split_segments(raw)

        self.assertEqual(segments, [ChatSegment(kind="editor", language="python", block=raw)])

    def test_short_markdown_reply_stays_text(self) -> None:
        raw = "Summary\n\nThis is a short answer.\n\n- first item"

        segments = ChatWindow._split_segments(raw)

        self.assertEqual(segments, [ChatSegment(kind="text", language="", block=raw)])

    def test_file_blocks_keep_source_path_for_save_action(self) -> None:
        raw = (
            "[FILE] demo.py (code)\n"
            "[SOURCE] /tmp/demo.py\n"
            "```python\n"
            "def demo():\n"
            "    return 1\n"
            "```"
        )

        segments = ChatWindow._split_segments(raw)

        self.assertEqual(
            segments,
            [
                ChatSegment(kind="text", language="", block="[FILE] demo.py (code)"),
                ChatSegment(
                    kind="editor",
                    language="python",
                    block="def demo():\n    return 1",
                    file_path="/tmp/demo.py",
                ),
            ],
        )

    def test_save_helper_writes_back_to_source_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "settings.yaml"
            target_path.write_text("old: value\n", encoding="utf-8")

            MsgWidget._write_editor_text_to_path(file_path=target_path, text="new: value\n")

            self.assertEqual(target_path.read_text(encoding="utf-8"), "new: value\n")

    def test_code_viewer_starts_read_only_and_can_enter_edit_mode(self) -> None:
        viewer = CodeViewer("print('demo')\n", language="python", editable=False)

        self.assertTrue(viewer.isReadOnly())
        self.assertEqual(viewer.objectName(), "chatCodeViewer")

        viewer.set_edit_mode(True)

        self.assertFalse(viewer.isReadOnly())
        self.assertEqual(viewer.objectName(), "aiInput")

    def test_code_viewer_edit_mode_keeps_dark_background_and_accent_border(self) -> None:
        viewer = CodeViewer(
            "print('demo')\n",
            language="python",
            editable=False,
            accent_color="#0fe913",
            accent_selection_color="#58ed5b",
        )

        viewer.set_edit_mode(True)

        self.assertIn("background:#111;", viewer.styleSheet())
        self.assertIn("border:1px solid #0fe913;", viewer.styleSheet())

    def test_code_viewer_uses_scrollbars_for_code_blocks(self) -> None:
        viewer = CodeViewer("print('demo')\n", language="python", editable=False)

        self.assertEqual(viewer.verticalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertEqual(viewer.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)

    def test_code_viewer_wheel_event_scrolls_content(self) -> None:
        text = "\n".join(f"line {index} " + ("x" * 160) for index in range(180))
        viewer = CodeViewer(text, language="python", editable=False)
        viewer.resize(340, 180)
        viewer.show()
        APP.processEvents()

        scroll_bar = viewer.verticalScrollBar()
        before = scroll_bar.value()

        wheel = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewer.viewport(), wheel)
        APP.processEvents()

        self.assertGreater(scroll_bar.maximum(), 0)
        self.assertGreater(scroll_bar.value(), before)

    def test_code_viewer_wheel_event_scrolls_with_pixel_delta(self) -> None:
        text = "\n".join(f"line {index} " + ("x" * 160) for index in range(180))
        viewer = CodeViewer(text, language="python", editable=False)
        viewer.resize(340, 180)
        viewer.show()
        APP.processEvents()

        scroll_bar = viewer.verticalScrollBar()
        before = scroll_bar.value()

        wheel = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, -12),
            QPoint(0, 0),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewer.viewport(), wheel)
        APP.processEvents()

        self.assertGreater(scroll_bar.maximum(), 0)
        self.assertGreater(scroll_bar.value(), before)

    def test_editor_panel_reveals_controls_only_after_activation(self) -> None:
        panel = ChatEditorPanel(
            segment=ChatSegment(
                kind="editor",
                language="python",
                block="print('demo')\n",
                file_path="/tmp/demo.py",
            ),
            save_handler=lambda _viewer, _file_path: None,
        )

        self.assertTrue(panel.viewer.isReadOnly())
        self.assertFalse(panel._edit_btn.isChecked())

        panel.viewer.editRequested.emit()

        self.assertFalse(panel.viewer.isReadOnly())
        self.assertTrue(panel._edit_btn.isChecked())

    def test_chat_window_scrollbar_style_uses_control_plane_tokens(self) -> None:
        chat = ChatWindow(
            {
                "col9": "#101010",
                "col10": "#303030",
                "col1": "#3a5fff",
                "col2": "#6280ff",
            }
        )

        style = chat.styleSheet()
        self.assertIn("QScrollArea#chatHistoryScroller QScrollBar::handle:vertical", style)
        self.assertIn("background: #303030;", style)
        self.assertIn("background: #6280ff;", style)

    def test_chat_window_prompt_frame_uses_scheme_tokens(self) -> None:
        chat = ChatWindow(
            {
                "col9": "#101010",
                "col10": "#303030",
                "col1": "#3a5fff",
                "col2": "#6280ff",
            }
        )

        style = chat.styleSheet()
        self.assertIn("QFrame#chatPromptComposer", style)
        self.assertIn("border: 1px solid #6280ff;", style)

    def test_chat_window_prompt_container_has_no_visible_separator(self) -> None:
        chat = ChatWindow(
            {
                "col9": "#101010",
                "col10": "#303030",
                "col1": "#3a5fff",
                "col2": "#6280ff",
            }
        )

        style = chat.styleSheet()
        self.assertIn("QFrame#chatPromptContainer", style)
        self.assertIn("border-top: none;", style)

    def test_chat_inline_slot_uses_four_pixel_vertical_handle(self) -> None:
        chat = ChatWindow(
            {
                "col9": "#101010",
                "col10": "#303030",
                "col1": "#3a5fff",
                "col2": "#6280ff",
            }
        )

        style = chat.styleSheet()
        self.assertIn("QSplitter#chatInlineSlotSplitter::handle:vertical", style)
        self.assertIn("min-width: 4px;", style)
        self.assertIn("margin: 10px 0px;", style)
        self.assertIn("QSplitter#chatInlineSlotSplitter::handle:hover", style)
        self.assertIn("QSplitter#chatInlineSlotSplitter::handle:pressed", style)
        self.assertIn("background: #6280ff;", style)

    def test_builder_runtime_tab_background_matches_extensions_shell_scheme(self) -> None:
        harness = _DummyControlPlaneStyleHarness()
        tab_page = QWidget()
        tab_page.setProperty("runtime_role", "builder")
        harness._runtime_tab_records[tab_page] = {"default_widget_kind": "builder_panel"}

        harness._apply_runtime_tab_page_scheme(tab_page)

        self.assertIn("background: #0b0b0b;", tab_page.styleSheet())

        builder_panel = QWidget()
        builder_panel.setProperty("_builder_show_toolbar", False)
        builder_editor = CodeViewer(
            "{}",
            builder_panel,
            language="json",
            editable=True,
            background_color="#111111",
            surface_color="#111111",
        )
        harness._apply_builder_panel_scheme(builder_panel)

        self.assertIn("QFrame#controlBuilderPanel", builder_panel.styleSheet())
        self.assertIn("background: #0b0b0b;", builder_panel.styleSheet())
        self.assertIn("background:#0b0b0b;", builder_editor.styleSheet())

    def test_extension_tab_marquee_starts_for_overflowing_label(self) -> None:
        dummy = _DummyExtensionsTabMarquee()
        self.addCleanup(dummy.extensions_tabs.close)
        self.addCleanup(dummy._hover_tab_marquee_timer.stop)

        dummy._start_tab_hover_marquee(0)

        self.assertEqual(dummy._hover_tab_index, 0)
        self.assertEqual(dummy._hover_tab_base_text, "VeryLongExtensionTabLabelThatNeedsMarquee")
        self.assertTrue(dummy._hover_tab_marquee_timer.isActive())

    def test_control_plane_tab_marquee_starts_for_overflowing_label(self) -> None:
        dummy = _DummyControlPlaneTabMarquee()
        self.addCleanup(dummy.tabs.close)
        self.addCleanup(dummy._control_tab_hover_marquee_timer.stop)

        dummy._start_control_plane_tab_hover_marquee(0)

        self.assertEqual(dummy._control_tab_hover_index, 0)
        self.assertEqual(dummy._control_tab_hover_base_text, "VeryLongControlPlaneTabLabelThatNeedsMarquee")
        self.assertTrue(dummy._control_tab_hover_marquee_timer.isActive())


if __name__ == "__main__":
    unittest.main()
