from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QApplication, QLineEdit, QStackedWidget, QTabBar, QTabWidget, QToolButton, QWidget


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PKG_ROOT = Path(__file__).resolve().parents[1]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from alde.ai_ide_v1756 import (
    ChatEditorPanel,
    ChatSegment,
    ChatWindow,
    CodeViewer,
    ControlPlaneTabBar,
    ControlPlaneWidget,
    ExtensionsWorkspaceWidget,
    MsgWidget,
    _content_resize_icon,
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


class _DummyControlPlaneBoardHarness:
    _BOARD_ITEM_WIDGET_KIND = ControlPlaneWidget._BOARD_ITEM_WIDGET_KIND
    _EXTENSIONS_WORKSPACE_WIDGET_KIND = ControlPlaneWidget._EXTENSIONS_WORKSPACE_WIDGET_KIND
    _BUILD_RUNTIME_TAB_LABEL = ControlPlaneWidget._BUILD_RUNTIME_TAB_LABEL
    _LEGACY_BUILD_RUNTIME_SLASH_LABEL = ControlPlaneWidget._LEGACY_BUILD_RUNTIME_SLASH_LABEL
    _control_plane_tab_full_text = ControlPlaneWidget._control_plane_tab_full_text
    _config_monitor_section_state_for = ControlPlaneWidget._config_monitor_section_state_for
    _board_context_attribute_names = ControlPlaneWidget._board_context_attribute_names
    _board_context_scope = ControlPlaneWidget._board_context_scope
    _control_monitor_splitter_category = ControlPlaneWidget._control_monitor_splitter_category
    _control_monitor_group_top_margin = ControlPlaneWidget._control_monitor_group_top_margin
    _primary_board_item_titles = ControlPlaneWidget._primary_board_item_titles
    _next_board_tab_name = ControlPlaneWidget._next_board_tab_name
    _board_item_widget_kind_for_title = ControlPlaneWidget._board_item_widget_kind_for_title
    _runtime_tab_name_for_section_title = ControlPlaneWidget._runtime_tab_name_for_section_title

    def __init__(self) -> None:
        self.tabs = _FakeTabs(
            [
                "Board 1",
                self._LEGACY_BUILD_RUNTIME_SLASH_LABEL,
                "Board 2",
                "Loadable Extensions",
            ],
            tab_width=60,
        )
        self.config_monitor_sections = [object(), object(), object()]
        self._config_monitor_section_state = {
            self.config_monitor_sections[0]: {"title": "Monitoring Summary"},
            self.config_monitor_sections[1]: {"title": "Extensions"},
            self.config_monitor_sections[2]: {"title": "Operator Log"},
        }
        self._primary_board_context = {
            "config_monitor_sections": self.config_monitor_sections,
            "_config_monitor_section_state": self._config_monitor_section_state,
        }


class _DummyControlPlaneSerializeHarness:
    _BOARD_ITEM_WIDGET_KIND = ControlPlaneWidget._BOARD_ITEM_WIDGET_KIND
    _serialize_runtime_widget_panel = ControlPlaneWidget._serialize_runtime_widget_panel


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

        self.assertIn("QWidget#controlRuntimeTabPage", tab_page.styleSheet())
        self.assertIn("background: transparent;", tab_page.styleSheet())
        self.assertIn("QWidget#controlRuntimeTabPage > QSplitter#controlViewportSplitter", tab_page.styleSheet())
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


class TestControlPlaneBoardHelpers(unittest.TestCase):
    def test_content_resize_icon_changes_shape_for_reset_state(self) -> None:
        expand_image = _content_resize_icon(reset=False).pixmap(18, 18).toImage()
        reset_image = _content_resize_icon(reset=True).pixmap(18, 18).toImage()

        self.assertEqual(expand_image.pixelColor(3, 3).alpha(), 0)
        self.assertGreater(reset_image.pixelColor(3, 3).alpha(), 0)
        self.assertGreater(expand_image.pixelColor(3, 14).alpha(), 0)
        self.assertEqual(reset_image.pixelColor(3, 14).alpha(), 0)

    def test_new_board_factory_replicates_primary_section_structure(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        primary_board = widget._primary_board_context
        clone_tab = widget._create_board_runtime_tab(activate=False)
        clone_board = widget._board_context_by_tab[clone_tab]

        with widget._board_context_scope(primary_board):
            primary_titles = [
                str(widget._config_monitor_section_state_for(section).get("title") or "")
                for section in widget.config_monitor_sections
            ]
        with widget._board_context_scope(clone_board):
            clone_titles = [
                str(widget._config_monitor_section_state_for(section).get("title") or "")
                for section in widget.config_monitor_sections
            ]

        self.assertEqual(clone_titles, primary_titles)

    def test_expand_action_can_restore_section_to_baseline_height(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()
        widget.resize(1280, 900)
        widget.show()
        APP.processEvents()

        with widget._board_context_scope(widget._primary_board_context):
            section = next(
                candidate
                for candidate in widget.config_monitor_sections
                if str(widget._config_monitor_section_state_for(candidate).get("title") or "") == "Monitoring Summary"
            )
            state = widget._config_monitor_section_state_for(section)
            self.assertIsNotNone(state)
            remember_size = state["remember_size"]
            baseline_size = int(remember_size["baseline_size"])
            content_widget = state["content_widget"]

            widget._expand_config_monitor_splitter_section(section)
            APP.processEvents()
            expanded_size = int(remember_size["expanded_size"])

            widget._expand_config_monitor_splitter_section(section)
            APP.processEvents()
            restored_size = int(remember_size["expanded_size"])

        self.assertGreater(expanded_size, baseline_size)
        self.assertEqual(restored_size, baseline_size)
        self.assertLessEqual(content_widget.minimumHeight(), baseline_size)

    def test_primary_board_item_titles_preserve_section_order(self) -> None:
        harness = _DummyControlPlaneBoardHarness()

        self.assertEqual(
            harness._primary_board_item_titles(),
            ["Monitoring Summary", "Extensions", "Operator Log"],
        )

    def test_runtime_tab_name_for_section_title_keeps_build_special_case(self) -> None:
        harness = _DummyControlPlaneBoardHarness()

        self.assertEqual(
            harness._runtime_tab_name_for_section_title("Build"),
            harness._BUILD_RUNTIME_TAB_LABEL,
        )
        self.assertEqual(
            harness._runtime_tab_name_for_section_title("Monitoring Summary"),
            "Monitoring Summary",
        )

    def test_threat_flow_uses_blue_group_and_group_margin_only_on_group_change(self) -> None:
        harness = _DummyControlPlaneBoardHarness()

        self.assertEqual(
            harness._control_monitor_splitter_category("Threat Flow"),
            "blue",
        )
        self.assertEqual(
            harness._control_monitor_group_top_margin("blue", "blue"),
            0,
        )
        self.assertEqual(
            harness._control_monitor_group_top_margin("blue", "orange"),
            3,
        )

    def test_next_board_tab_name_uses_next_numeric_suffix(self) -> None:
        harness = _DummyControlPlaneBoardHarness()

        self.assertEqual(harness._next_board_tab_name(), "Board 3")

    def test_board_item_payload_serializes_target_title(self) -> None:
        harness = _DummyControlPlaneSerializeHarness()
        panel = QWidget()
        panel.setProperty("runtime_widget_kind", "board_item")
        panel.setProperty("runtime_widget_title", "Extensions")
        panel.setProperty("runtime_board_item_title", "Extensions")

        self.assertEqual(
            harness._serialize_runtime_widget_panel(panel),
            {
                "kind": "board_item",
                "title": "Extensions",
                "board_item_title": "Extensions",
            },
        )

    def test_board_item_payload_serializes_normalized_kind_and_title_fallback(self) -> None:
        harness = _DummyControlPlaneSerializeHarness()
        panel = QWidget()
        panel.setProperty("runtime_widget_kind", " Board_Item ")
        panel.setProperty("runtime_widget_title", "Monitoring Summary")

        self.assertEqual(
            harness._serialize_runtime_widget_panel(panel),
            {
                "kind": "board_item",
                "title": "Monitoring Summary",
                "board_item_title": "Monitoring Summary",
            },
        )

    def test_board_item_kind_mapping_uses_real_widgets_for_runtime_surfaces(self) -> None:
        harness = _DummyControlPlaneBoardHarness()

        self.assertEqual(harness._board_item_widget_kind_for_title("Build"), "builder_panel")
        self.assertEqual(
            harness._board_item_widget_kind_for_title("Extensions"),
            harness._EXTENSIONS_WORKSPACE_WIDGET_KIND,
        )
        self.assertEqual(
            harness._board_item_widget_kind_for_title("Monitoring Summary"),
            harness._BOARD_ITEM_WIDGET_KIND,
        )

    def test_open_section_in_new_tab_creates_runtime_board_item_tab(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        with widget._board_context_scope(widget._primary_board_context):
            monitoring_section = next(
                section
                for section in widget.config_monitor_sections
                if str(widget._config_monitor_section_state_for(section).get("title") or "") == "Monitoring Summary"
            )

        panel = widget._open_config_monitor_section_in_runtime_tab(monitoring_section)
        runtime_tab = widget._find_runtime_tab_by_name("Monitoring Summary")

        self.assertIsNotNone(panel)
        self.assertIsNotNone(runtime_tab)
        self.assertIn(runtime_tab, widget._runtime_tab_records)
        self.assertEqual(str(panel.property("runtime_widget_kind") or ""), widget._BOARD_ITEM_WIDGET_KIND)
        self.assertEqual(str(panel.property("runtime_board_item_title") or ""), "Monitoring Summary")
        runtime_index = widget.tabs.indexOf(runtime_tab)
        self.assertGreaterEqual(runtime_index, 0)
        tab_color = widget.tabs.tabBar().tabTextColor(runtime_index).name().lower()
        self.assertEqual(tab_color, "#ffd7ac")
        self.assertTrue(widget.tabs.tabIcon(runtime_index).isNull())
        self.assertIsInstance(widget.tabs.tabBar(), ControlPlaneTabBar)
        tab_palette = widget.tabs.tabBar().tab_palette_for_index(runtime_index)
        self.assertEqual(str(tab_palette.get("label_bg") or ""), "rgba(255, 140, 0, 0.24)")

    def test_control_plane_surface_renders_rounded_border_under_tabs(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        # Verify TabWidget itself has border applied
        self.assertIsInstance(widget.tabs, QTabWidget)
        self.assertTrue(widget.tabs.testAttribute(Qt.WA_StyledBackground))
        
        # Check TabWidget's local stylesheet contains visible border with bright blue on all sides
        tabs_style = widget.tabs.styleSheet()
        self.assertIn("QTabWidget#controlPlaneTabs", tabs_style)
        self.assertIn("border: 2px solid", tabs_style)
        self.assertIn("#3a5fff", tabs_style)  # Bright blue color
        self.assertIn("border-radius: 14px;", tabs_style)
        self.assertIn("background:", tabs_style)

    def test_build_section_uses_runtime_builder_widget_in_board_tabs(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        self.assertEqual(widget._config_tab.objectName(), "BoardTabWidget")
        primary_builder_panel = widget.build_section_panel.findChild(QWidget, "controlBuilderPanel")
        self.assertIsNotNone(primary_builder_panel)
        self.assertFalse(bool(primary_builder_panel.property("_builder_show_toolbar")))
        self.assertEqual(widget.build_section_panel.objectName(), "BuildTabWidget")
        primary_build_header = widget.build_section_panel.findChild(QWidget, "runtimeWidgetPanel")
        self.assertIsNotNone(primary_build_header)
        primary_build_title = primary_build_header.findChild(QWidget, "runtimeWidgetTitle")
        self.assertIsNotNone(primary_build_title)
        self.assertEqual(primary_build_title.text(), widget._BUILD_RUNTIME_TAB_LABEL)
        self.assertEqual(widget._BUILD_RUNTIME_TAB_LABEL, "<Build>")
        primary_build_actions = primary_build_header.findChildren(QToolButton, "runtimeWidgetActionButton")
        self.assertEqual(len(primary_build_actions), 4)
        primary_toolbar = primary_builder_panel.findChild(QWidget, "builderToolbarWidget")
        self.assertIsNotNone(primary_toolbar)
        self.assertTrue(primary_toolbar.isHidden())

        board_tab = widget._create_board_runtime_tab(activate=False)
        board_context = widget._board_context_by_tab.get(board_tab)
        self.assertIsInstance(board_context, dict)
        self.assertEqual(board_tab.objectName(), "BoardTabWidget")
        board_build_section = board_context.get("build_section_panel")
        self.assertIsInstance(board_build_section, QWidget)
        self.assertEqual(board_build_section.objectName(), "BuildTabWidget")
        board_build_header = board_build_section.findChild(QWidget, "runtimeWidgetPanel")
        self.assertIsNotNone(board_build_header)
        board_build_title = board_build_header.findChild(QWidget, "runtimeWidgetTitle")
        self.assertIsNotNone(board_build_title)
        self.assertEqual(board_build_title.text(), widget._BUILD_RUNTIME_TAB_LABEL)
        board_build_actions = board_build_header.findChildren(QToolButton, "runtimeWidgetActionButton")
        self.assertEqual(len(board_build_actions), 4)
        board_builder_panel = board_build_section.findChild(QWidget, "controlBuilderPanel")
        self.assertIsNotNone(board_builder_panel)
        self.assertFalse(bool(board_builder_panel.property("_builder_show_toolbar")))
        board_toolbar = board_builder_panel.findChild(QWidget, "builderToolbarWidget")
        self.assertIsNotNone(board_toolbar)
        self.assertTrue(board_toolbar.isHidden())

    def test_board_tab_bar_does_not_show_corner_add_button(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        self.assertIsNone(widget.tabs.cornerWidget(Qt.TopRightCorner))
        self.assertIsNone(widget._control_tab_corner_widget)
        self.assertIsNone(widget._control_tab_corner_add_button)
        self.assertIsNone(widget._control_tab_corner_menu)

    def test_open_section_in_new_tab_appends_clone_when_runtime_tab_exists(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        with widget._board_context_scope(widget._primary_board_context):
            monitoring_section = next(
                section
                for section in widget.config_monitor_sections
                if str(widget._config_monitor_section_state_for(section).get("title") or "") == "Monitoring Summary"
            )

        first_panel = widget._open_config_monitor_section_in_runtime_tab(monitoring_section)
        runtime_tab = widget._find_runtime_tab_by_name("Monitoring Summary")
        second_panel = widget._open_config_monitor_section_in_runtime_tab(monitoring_section)

        self.assertIsNotNone(first_panel)
        self.assertIsNotNone(second_panel)
        self.assertIsNot(first_panel, second_panel)
        self.assertIsNotNone(runtime_tab)
        self.assertEqual(len(widget._runtime_tab_panels(runtime_tab)), 2)

    def test_extensions_section_handle_embeds_workspace_tab_bar_proxy(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        with widget._board_context_scope(widget._primary_board_context):
            extensions_section = next(
                section
                for section in widget.config_monitor_sections
                if str(widget._config_monitor_section_state_for(section).get("title") or "") == "Extensions"
            )
            section_state = widget._config_monitor_section_state_for(extensions_section)
            content_widget = section_state.get("content_widget")
            extensions_workspace = content_widget.findChild(ExtensionsWorkspaceWidget)

        self.assertIsInstance(extensions_workspace, ExtensionsWorkspaceWidget)
        self.assertTrue(extensions_workspace.extensions_tabs.tabBar().isHidden())
        self.assertEqual(len(extensions_workspace._embedded_tab_bar_proxies), 1)
        extensions_workspace.open_new_connection_tab(activate=True)

        embedded_proxy = next(
            proxy
            for proxy in extensions_workspace._embedded_tab_bar_proxies
            if isinstance(proxy, QTabBar)
        )
        self.assertGreaterEqual(embedded_proxy.count(), 2)
        proxy_style = embedded_proxy.styleSheet()
        self.assertIn("QTabBar#extensionsEmbeddedTabBar", proxy_style)
        self.assertIn("background-color: transparent", proxy_style)
        self.assertIn("border-top: 0px solid transparent", proxy_style)
        self.assertNotIn("QTabBar#extensionsEmbeddedTabBar { background-color: rgba(255, 204, 0, 0.08)", proxy_style)
        self.assertIn("border: 1px solid rgba(255, 204, 0, 0.62)", proxy_style)
        self.assertIn("background-color: rgba(255, 204, 0, 0.24)", proxy_style)
        self.assertIn("padding: 0px 28px 0px 9px", proxy_style)
        self.assertIn("margin-bottom: 4px", proxy_style)
        self.assertIn("padding-bottom: 4px", proxy_style)
        self.assertIn("min-width: 64px", proxy_style)
        self.assertIn("min-height: 18px", proxy_style)
        close_button = embedded_proxy.tabButton(1, QTabBar.RightSide)
        self.assertIsInstance(close_button, QToolButton)
        self.assertTrue(close_button.isHidden())
        self.assertEqual(close_button.width(), 0)
        extensions_workspace._update_hover_close_buttons(embedded_proxy, 1)
        self.assertFalse(close_button.isHidden())
        self.assertEqual(close_button.width(), 16)
        extensions_workspace._update_hover_close_buttons(embedded_proxy, -1)
        self.assertTrue(close_button.isHidden())
        self.assertEqual(close_button.width(), 0)

        header_style = extensions_workspace.styleSheet()
        self.assertIn("QTabWidget#extensionsTabs QTabBar::tab", header_style)
        self.assertIn("QTabWidget#extensionsTabs QTabBar", header_style)
        self.assertIn("border-top: 0px solid transparent", header_style)
        self.assertIn("margin-bottom: 4px", header_style)
        self.assertIn("padding-bottom: 4px", header_style)
        self.assertIn("border: 1px solid rgba(255, 204, 0, 0.62)", header_style)
        self.assertIn("padding: 1px 28px 1px 9px", header_style)
        self.assertIn("min-width: 64px", header_style)
        self.assertIn("min-height: 18px", header_style)
        proxy_host = embedded_proxy.parentWidget()
        self.assertIsInstance(proxy_host, QWidget)
        add_button = proxy_host.findChild(QToolButton, "extensionsEmbeddedTabAddButton")
        self.assertIsInstance(add_button, QToolButton)
        add_button_style = add_button.styleSheet()
        self.assertIn("background-color: #101010", add_button_style)
        self.assertIn("border: 1px solid transparent", add_button_style)
        self.assertNotIn("border-left: 2px solid rgba(255, 204, 0", add_button_style)

    def test_extensions_runtime_panel_keeps_header_address_bar_and_loads_web_tool(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        with widget._board_context_scope(widget._primary_board_context):
            extensions_section = next(
                section
                for section in widget.config_monitor_sections
                if str(widget._config_monitor_section_state_for(section).get("title") or "") == "Extensions"
            )

        panel = widget._open_config_monitor_section_in_runtime_tab(extensions_section)

        self.assertIsNotNone(panel)
        runtime_workspace = panel.findChild(ExtensionsWorkspaceWidget)
        self.assertIsNotNone(runtime_workspace)
        self.assertEqual(runtime_workspace.current_tool_id(), widget._EXTENSIONS_RUNTIME_TOOL_ID)
        self.assertTrue(runtime_workspace.extensions_tabs.tabBar().isHidden())

        header_proxy = next(
            input_widget
            for input_widget in panel.findChildren(QLineEdit)
            if bool(input_widget.property("extensions_external_uri_proxy"))
        )
        self.assertEqual(str(header_proxy.text() or "").strip(), str(panel.property("runtime_source_path") or "").strip())

    def test_clone_runtime_widget_panel_appends_same_panel_kind(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        runtime_tab = widget.create_runtime_tab(
            "Clone Panel Test",
            activate=False,
            add_default_widget=False,
            persist=False,
        )
        panel = widget._add_widget_to_runtime_tab(
            runtime_tab,
            widget_kind="board_item",
            title="Operator Log",
            board_item_title="Operator Log",
            persist=False,
        )

        clone_panel = widget._clone_runtime_widget_panel(panel)

        self.assertIsNotNone(clone_panel)
        self.assertIsNot(panel, clone_panel)
        self.assertEqual(len(widget._runtime_tab_panels(runtime_tab)), 2)
        self.assertEqual(str(clone_panel.property("runtime_widget_kind") or ""), "board_item")
        self.assertEqual(str(clone_panel.property("runtime_board_item_title") or ""), "Operator Log")

    def test_add_widget_to_runtime_tab_normalizes_board_item_kind_and_preserves_title(self) -> None:
        scheme = {
            "col1": "#3a5fff",
            "col2": "#6280ff",
            "col5": "#1a1a1a",
            "col6": "#e3e3de",
            "col7": "#0b0b0b",
            "col8": "#9a9a95",
            "col9": "#101010",
            "col10": "#303030",
        }
        widget = ControlPlaneWidget(dict(scheme), dict(scheme))
        self.addCleanup(widget.deleteLater)
        widget._refresh_timer.stop()
        widget._runtime_state_save_timer.stop()
        widget._control_tab_hover_marquee_timer.stop()

        runtime_tab = widget.create_runtime_tab(
            "Board Item Restore",
            activate=False,
            add_default_widget=False,
            persist=False,
        )
        panel = widget._add_widget_to_runtime_tab(
            runtime_tab,
            widget_kind=" Board_Item ",
            title="Operator Log",
            board_item_title="Operator Log",
            persist=False,
        )

        self.assertIsNotNone(panel)
        self.assertEqual(str(panel.property("runtime_widget_kind") or ""), "board_item")
        self.assertEqual(str(panel.property("runtime_board_item_title") or ""), "Operator Log")


if __name__ == "__main__":
    unittest.main()
