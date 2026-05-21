from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

from PySide6 import QtCore
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
	QFrame,
	QGraphicsScene,
	QGraphicsView,
	QHBoxLayout,
	QLabel,
	QSizePolicy,
	QSplitter,
	QStackedWidget,
	QTextBrowser,
	QToolButton,
	QVBoxLayout,
	QWidget,
)

try:
	if __package__:
		from ..agents_db import AgentRelationGraphService  # type: ignore
	else:
		from agents_db import AgentRelationGraphService  # type: ignore
except ImportError as e:
	msg = str(e)
	if "attempted relative import" in msg or "no known parent package" in msg:
		from alde.agents_db import AgentRelationGraphService  # type: ignore  # noqa: E402
	else:
		raise


class RelationGraphView(QGraphicsView):
	graphItemActivated = Signal(object)
	graphSpreadRequested = Signal(float)
	_GRAPH_PAYLOAD_ROLE = 0

	def __init__(self, parent: QWidget | None = None) -> None:
		super().__init__(parent)
		self.setFrameShape(QFrame.NoFrame)
		self.setRenderHint(QPainter.Antialiasing, True)
		self.setDragMode(QGraphicsView.ScrollHandDrag)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
		self.setResizeAnchor(QGraphicsView.AnchorViewCenter)

	def wheelEvent(self, event) -> None:  # type: ignore[override]
		angle_delta = event.angleDelta().y()
		if angle_delta == 0:
			super().wheelEvent(event)
			return

		modifiers = event.modifiers()
		if modifiers & Qt.ControlModifier:
			factor = 1.16 if angle_delta > 0 else 1 / 1.16
			self.scale(factor, factor)
			event.accept()
			return

		if modifiers & Qt.ShiftModifier:
			self.rotate(7.5 if angle_delta > 0 else -7.5)
			event.accept()
			return

		if modifiers & Qt.AltModifier:
			self.graphSpreadRequested.emit(1.12 if angle_delta > 0 else 1 / 1.12)
			event.accept()
			return

		super().wheelEvent(event)

	def mousePressEvent(self, event) -> None:  # type: ignore[override]
		item_at_position = self.itemAt(event.position().toPoint() if hasattr(event, "position") else event.pos())
		current_item = item_at_position
		while current_item is not None:
			try:
				payload = current_item.data(self._GRAPH_PAYLOAD_ROLE)
			except Exception:
				payload = None
			if isinstance(payload, dict):
				self.graphItemActivated.emit(dict(payload))
				event.accept()
				return
			try:
				current_item = current_item.parentItem()
			except Exception:
				current_item = None
		super().mousePressEvent(event)

	def resizeEvent(self, event) -> None:  # type: ignore[override]
		super().resizeEvent(event)
		self.fit_graph_scene()

	def fit_graph_scene(self) -> None:
		scene = self.scene()
		if scene is None:
			return
		scene_rect = scene.itemsBoundingRect()
		if scene_rect.isNull() or scene_rect.width() <= 0 or scene_rect.height() <= 0:
			return
		self.fitInView(scene_rect.adjusted(-72, -72, 72, 72), Qt.KeepAspectRatio)


class RuntimeWidget(QWidget):
	widgetStateChanged = Signal()

	def __init__(
		self,
		*,
		object_name: str,
		source_uri: str,
		graph_service: AgentRelationGraphService,
		scheme: Mapping[str, str] | None = None,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self._object_name = str(object_name or "agent_relation_graph").strip() or "agent_relation_graph"
		self._source_uri = str(source_uri or "").strip()
		self._graph_service = graph_service
		self._scheme: dict[str, str] = dict(scheme or {})

		self._graph_snapshot: dict[str, Any] = {}
		self._graph_view_state: dict[str, Any] = {}
		self._graph_layout_spread = 1.0

		self._build_object_ui()
		self.update_scheme(self._scheme)
		self.refresh_object(fit_view=True)

	def _build_object_ui(self) -> None:
		root_layout = QVBoxLayout(self)
		root_layout.setContentsMargins(0, 0, 0, 0)
		root_layout.setSpacing(0)

		self._content_stack = QStackedWidget(self)
		root_layout.addWidget(self._content_stack, 1)

		self._relations_page = QWidget(self._content_stack)
		relations_layout = QVBoxLayout(self._relations_page)
		relations_layout.setContentsMargins(0, 0, 0, 0)
		relations_layout.setSpacing(0)

		self._relations_splitter = QSplitter(Qt.Horizontal, self._relations_page)

		self._graph_panel = QWidget(self._relations_splitter)
		graph_panel_layout = QVBoxLayout(self._graph_panel)
		graph_panel_layout.setContentsMargins(0, 0, 0, 0)
		graph_panel_layout.setSpacing(0)

		self._graph_scene = QGraphicsScene(self)
		self._graph_view = RelationGraphView(self._graph_panel)
		self._graph_view.setScene(self._graph_scene)
		graph_panel_layout.addWidget(self._graph_view, 1)

		self._graph_footer = QWidget(self._graph_panel)
		self._graph_footer.setObjectName("graphFooter")
		footer_layout = QHBoxLayout(self._graph_footer)
		footer_layout.setContentsMargins(8, 4, 8, 4)
		footer_layout.setSpacing(6)

		self._status_label = QLabel("", self._graph_footer)
		self._status_label.setWordWrap(False)
		self._status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
		footer_layout.addWidget(self._status_label, 1)

		footer_layout.addWidget(
			self._create_control_button(
				"<",
				"Rotate graph left",
				lambda: self._rotate_graph_view(-15.0),
			),
			0,
		)
		footer_layout.addWidget(
			self._create_control_button(
				">",
				"Rotate graph right",
				lambda: self._rotate_graph_view(15.0),
			),
			0,
		)
		footer_layout.addWidget(
			self._create_control_button(
				"-",
				"Compact graph layout",
				lambda: self._handle_graph_spread_requested(1 / 1.18),
			),
			0,
		)
		footer_layout.addWidget(
			self._create_control_button(
				"+",
				"Expand graph layout",
				lambda: self._handle_graph_spread_requested(1.18),
			),
			0,
		)
		footer_layout.addWidget(
			self._create_control_button(
				"[]",
				"Fit graph scene",
				self._fit_graph_view,
			),
			0,
		)
		footer_layout.addWidget(
			self._create_control_button(
				"R",
				"Reload extension widget",
				lambda: self.refresh_object(fit_view=False),
			),
			0,
		)

		graph_panel_layout.addWidget(self._graph_footer, 0)

		self._summary_browser = QTextBrowser(self._relations_splitter)
		self._summary_browser.setOpenExternalLinks(False)
		self._summary_browser.setOpenLinks(False)
		self._summary_browser.setMinimumWidth(280)

		self._relations_splitter.addWidget(self._graph_panel)
		self._relations_splitter.addWidget(self._summary_browser)
		self._relations_splitter.setStretchFactor(0, 4)
		self._relations_splitter.setStretchFactor(1, 2)
		relations_layout.addWidget(self._relations_splitter, 1)

		self._detail_page = QTextBrowser(self._content_stack)
		self._detail_page.setOpenExternalLinks(False)
		self._detail_page.setOpenLinks(False)

		self._content_stack.addWidget(self._relations_page)
		self._content_stack.addWidget(self._detail_page)

		self._graph_view.graphItemActivated.connect(self._handle_graph_item_activated)
		self._graph_view.graphSpreadRequested.connect(self._handle_graph_spread_requested)
		self._summary_browser.anchorClicked.connect(self._handle_graph_link_clicked)

	def _create_control_button(self, text: str, tooltip: str, slot_callable) -> QToolButton:
		button = QToolButton(self._graph_footer)
		button.setText(str(text or ""))
		button.setToolButtonStyle(Qt.ToolButtonTextOnly)
		button.setCursor(Qt.PointingHandCursor)
		button.setFixedHeight(24)
		button.setToolTip(str(tooltip or ""))
		button.clicked.connect(slot_callable)
		return button

	def current_source_uri(self) -> str:
		return str(self._source_uri or "")

	def current_object_name(self) -> str:
		return str(self._object_name or "")

	def snapshot_summary(self) -> dict[str, Any]:
		snapshot = dict(self._graph_snapshot or {})
		return {
			"tool_id": str(snapshot.get("tool_id") or self._object_name),
			"view_kind": str(snapshot.get("view_kind") or ""),
			"status_text": str(snapshot.get("status_text") or ""),
			"node_count": len([item for item in (snapshot.get("nodes") or []) if isinstance(item, dict)]),
			"edge_count": len([item for item in (snapshot.get("edges") or []) if isinstance(item, dict)]),
		}

	def refresh_object(self, *, fit_view: bool) -> None:
		try:
			snapshot_payload = self._graph_service.load_widget_snapshot(
				tool_id=self._object_name,
				source_uri=self._source_uri,
			)
		except Exception as exc:
			error_text = html.escape(f"{type(exc).__name__}: {exc}")
			snapshot_payload = {
				"tool_id": self._object_name,
				"source_uri": self._source_uri,
				"view_kind": "relations_graph",
				"status_text": "Extension load failed",
				"message": f"Could not load extension: {type(exc).__name__}",
				"detail_html": f"<h3>Extension load failed</h3><p>{error_text}</p>",
				"nodes": [],
				"edges": [],
			}

		self._graph_snapshot = dict(snapshot_payload or {})
		self._apply_snapshot_state(fit_view=fit_view)
		self.widgetStateChanged.emit()

	def _apply_snapshot_state(self, *, fit_view: bool) -> None:
		snapshot_payload = dict(self._graph_snapshot or {})
		status_text = str(snapshot_payload.get("status_text") or snapshot_payload.get("message") or "")
		self._status_label.setText(status_text)
		self.setToolTip(status_text)

		view_kind = str(snapshot_payload.get("view_kind") or "relations_graph").strip().lower()
		if view_kind not in {"relations", "relations_graph", "catalog", "catalog_graph"}:
			detail_html = str(snapshot_payload.get("detail_html") or "<p>No details available.</p>")
			self._detail_page.setHtml(detail_html)
			self._content_stack.setCurrentWidget(self._detail_page)
			return

		graph_view_state = self._graph_service.load_graph_view_state(
			snapshot_payload,
			layout_spread=float(self._graph_layout_spread),
			selected_kind=str(self._graph_view_state.get("selected_kind") or ""),
			selected_object_id=str(self._graph_view_state.get("selected_object_id") or ""),
		)
		self._apply_graph_view_state(graph_view_state, fit_view=fit_view)
		self._content_stack.setCurrentWidget(self._relations_page)

	def _apply_graph_view_state(
		self,
		graph_view_state: Mapping[str, Any] | None,
		*,
		fit_view: bool,
		center_selected: bool = False,
	) -> None:
		view_state = dict(graph_view_state or {})
		self._graph_view_state = view_state

		summary_html = str(view_state.get("detail_html") or view_state.get("overview_html") or "<p>No detail available.</p>")
		self._summary_browser.setHtml(summary_html)

		render_commands = [dict(item) for item in (view_state.get("render_commands") or []) if isinstance(item, dict)]
		if not render_commands:
			message_text = str(view_state.get("message") or "No graph content available.")
			self._render_empty_scene(message_text)
			return

		self._render_scene_commands(render_commands, fit_view=fit_view)
		if center_selected:
			self._center_selected_item()

	def _resolve_render_color(self, role_name: str, fallback_color: str) -> str:
		role_to_scheme = {
			"accent": "col2",
			"border": "col10",
			"link": "col2",
			"surface": "col7",
			"surface_strong": "col10",
			"text_primary": "col6",
			"text_secondary": "col8",
		}
		scheme_key = role_to_scheme.get(str(role_name or "").strip().lower(), "")
		if scheme_key:
			color_value = str(self._scheme.get(scheme_key) or "").strip()
			if color_value:
				return color_value
		return str(fallback_color or "#ffffff")

	def _render_scene_commands(self, render_commands: Sequence[Mapping[str, Any]], *, fit_view: bool) -> None:
		self._graph_scene.clear()

		for command in render_commands:
			command_type = str(command.get("type") or "").strip().lower()
			payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
			tooltip = str(command.get("tooltip") or "Open graph item")
			style_payload = command.get("style") if isinstance(command.get("style"), dict) else {}

			if command_type == "ellipse":
				x_pos = float(command.get("x") or 0.0)
				y_pos = float(command.get("y") or 0.0)
				width = float(command.get("width") or 0.0)
				height = float(command.get("height") or 0.0)

				stroke_role = str(style_payload.get("stroke_role") or "border")
				fill_role = str(style_payload.get("fill_role") or "surface")
				line_width = float(style_payload.get("line_width") or 1.5)
				stroke_color = QColor(self._resolve_render_color(stroke_role, "#6d7399"))
				fill_color = QColor(self._resolve_render_color(fill_role, "#1f2339"))

				pen = QPen(stroke_color)
				pen.setWidthF(line_width)
				item = self._graph_scene.addEllipse(x_pos, y_pos, width, height, pen, QBrush(fill_color))
				self._configure_graph_item(item, payload, tooltip)
				continue

			if command_type == "line":
				start_x = float(command.get("start_x") or 0.0)
				start_y = float(command.get("start_y") or 0.0)
				end_x = float(command.get("end_x") or 0.0)
				end_y = float(command.get("end_y") or 0.0)

				stroke_role = str(style_payload.get("stroke_role") or "link")
				line_width = float(style_payload.get("line_width") or 1.5)
				pen = QPen(QColor(self._resolve_render_color(stroke_role, "#6280ff")))
				pen.setWidthF(line_width)

				item = self._graph_scene.addLine(start_x, start_y, end_x, end_y, pen)
				self._configure_graph_item(item, payload, tooltip)
				continue

			if command_type == "text":
				label_text = str(command.get("text") or "")
				text_item = self._graph_scene.addText(label_text)
				text_role = str(style_payload.get("text_role") or "text_primary")
				text_item.setDefaultTextColor(QColor(self._resolve_render_color(text_role, "#ffffff")))

				max_width = float(command.get("max_width") or 0.0)
				if max_width > 0.0:
					text_item.setTextWidth(max_width)

				x_pos = float(command.get("x") or 0.0)
				y_pos = float(command.get("y") or 0.0)
				anchor_mode = str(command.get("anchor") or "top_left").strip().lower()
				text_rect = text_item.boundingRect()
				if anchor_mode == "center_above":
					text_item.setPos(x_pos - (text_rect.width() / 2.0), y_pos - text_rect.height() - 4.0)
				else:
					text_item.setPos(x_pos, y_pos)

				self._configure_graph_item(text_item, payload, tooltip)
				continue

		scene_rect = self._graph_scene.itemsBoundingRect()
		if not scene_rect.isNull():
			self._graph_scene.setSceneRect(scene_rect.adjusted(-42, -42, 42, 42))
		if fit_view:
			self._graph_view.fit_graph_scene()

	def _configure_graph_item(self, item: Any, payload: Mapping[str, Any], tooltip: str) -> None:
		try:
			item.setData(RelationGraphView._GRAPH_PAYLOAD_ROLE, dict(payload or {}))
			item.setToolTip(str(tooltip or "Open graph item"))
			item.setCursor(QCursor(Qt.PointingHandCursor))
			item.setAcceptedMouseButtons(Qt.LeftButton)
		except Exception:
			return

	def _render_empty_scene(self, message_text: str) -> None:
		self._graph_scene.clear()
		text_item = self._graph_scene.addText(str(message_text or "No graph content available."))
		text_item.setDefaultTextColor(QColor(self._scheme.get("col8", "#B7B7B7")))
		text_item.setPos(12, 12)
		rect = text_item.boundingRect()
		self._graph_scene.setSceneRect(rect.adjusted(-18, -18, 18, 18))

	@Slot(object)
	def _handle_graph_item_activated(self, payload: object) -> None:
		graph_view_state = self._graph_service.load_graph_view_state_from_payload(
			self._graph_snapshot,
			payload if isinstance(payload, dict) else {},
			layout_spread=float(self._graph_layout_spread),
			fallback_selected_kind=str(self._graph_view_state.get("selected_kind") or ""),
			fallback_selected_object_id=str(self._graph_view_state.get("selected_object_id") or ""),
		)
		self._apply_graph_view_state(graph_view_state, fit_view=False, center_selected=True)
		self.widgetStateChanged.emit()

	def _handle_graph_link_clicked(self, url: object) -> None:
		graph_view_state = self._graph_service.load_graph_view_state_from_link(
			self._graph_snapshot,
			url,
			layout_spread=float(self._graph_layout_spread),
			fallback_selected_kind=str(self._graph_view_state.get("selected_kind") or ""),
			fallback_selected_object_id=str(self._graph_view_state.get("selected_object_id") or ""),
		)
		self._apply_graph_view_state(graph_view_state, fit_view=False, center_selected=True)
		self.widgetStateChanged.emit()

	@Slot(float)
	def _handle_graph_spread_requested(self, factor: float) -> None:
		self._graph_layout_spread = max(0.35, min(3.5, float(self._graph_layout_spread) * float(factor or 1.0)))
		graph_view_state = self._graph_service.load_graph_view_state(
			self._graph_snapshot,
			layout_spread=float(self._graph_layout_spread),
			selected_kind=str(self._graph_view_state.get("selected_kind") or ""),
			selected_object_id=str(self._graph_view_state.get("selected_object_id") or ""),
		)
		self._apply_graph_view_state(graph_view_state, fit_view=False)
		self.widgetStateChanged.emit()

	def _center_selected_item(self) -> None:
		selected_kind = str(self._graph_view_state.get("selected_kind") or "")
		selected_object_id = str(self._graph_view_state.get("selected_object_id") or "")
		if not selected_kind or not selected_object_id:
			return
		center = self._graph_service.load_graph_item_center(
			self._graph_view_state,
			kind=selected_kind,
			object_id=selected_object_id,
		)
		if center is None:
			return
		self._graph_view.centerOn(float(center[0]), float(center[1]))

	def _rotate_graph_view(self, angle_degrees: float) -> None:
		self._graph_view.rotate(float(angle_degrees))

	def _fit_graph_view(self) -> None:
		self._graph_view.resetTransform()
		self._graph_view.fit_graph_scene()

	def update_scheme(self, scheme: Mapping[str, str]) -> None:
		self._scheme = dict(scheme or {})
		self.setStyleSheet(
			f"""
QWidget {{
	background: {self._scheme.get('col7', '#191f2f')};
}}
QWidget#graphFooter {{
	background: {self._scheme.get('col9', '#22345c')};
}}
QToolButton {{
	color: {self._scheme.get('col6', '#ecf2ff')};
	background: {self._scheme.get('col7', '#191f2f')};
	border: 1px solid {self._scheme.get('col10', '#33406a')};
	border-radius: 7px;
	padding: 6px 10px;
}}
QToolButton:hover {{
	background: {self._scheme.get('col9', '#22345c')};
}}
QTextBrowser {{
	color: {self._scheme.get('col6', '#e7eeff')};
	background: {self._scheme.get('col9', '#22345c')};
	border: 1px solid {self._scheme.get('col10', '#33406a')};
	border-radius: 8px;
	padding: 8px;
}}
QTextBrowser QScrollBar:vertical,
QTextBrowser QScrollBar:horizontal {{
	background: transparent;
	margin: 0px;
	border: none;
}}
QTextBrowser QScrollBar:vertical {{
	width: 6px;
}}
QTextBrowser QScrollBar:horizontal {{
	height: 6px;
}}
QTextBrowser QScrollBar:hover,
QTextBrowser QScrollBar:vertical:hover,
QTextBrowser QScrollBar:horizontal:hover {{
	background: transparent;
}}
QTextBrowser QScrollBar::handle:vertical,
QTextBrowser QScrollBar::handle:horizontal {{
	background: rgba(0, 0, 0, 0.0);
	border-radius: 3px;
	min-height: 28px;
	min-width: 28px;
}}
QTextBrowser QScrollBar::handle:vertical:hover,
QTextBrowser QScrollBar::handle:horizontal:hover,
QTextBrowser QScrollBar::handle:hover:vertical,
QTextBrowser QScrollBar::handle:hover:horizontal {{
	background: {self._scheme.get('col10', '#33406a')};
}}
QTextBrowser QScrollBar::handle:vertical:pressed,
QTextBrowser QScrollBar::handle:horizontal:pressed,
QTextBrowser QScrollBar::handle:pressed:vertical,
QTextBrowser QScrollBar::handle:pressed:horizontal {{
	background: {self._scheme.get('col2', '#6280ff')};
}}
QTextBrowser QScrollBar::add-line,
QTextBrowser QScrollBar::sub-line,
QTextBrowser QScrollBar::add-page,
QTextBrowser QScrollBar::sub-page {{
	background: none;
	border: none;
	width: 0px;
	height: 0px;
}}
QGraphicsView {{
	background: {self._scheme.get('col9', '#22345c')};
	border: 1px solid {self._scheme.get('col10', '#33406a')};
}}
QLabel {{
	color: {self._scheme.get('col8', '#a8afc7')};
}}
"""
		)
		self._graph_view.setBackgroundBrush(QBrush(QColor(self._scheme.get("col9", "#22345c"))))
		if bool(self._graph_snapshot):
			self._apply_snapshot_state(fit_view=False)


class RelationGraphWidgetArtifactFactory:
	def __init__(self, graph_service: AgentRelationGraphService | None = None) -> None:
		self._graph_service = graph_service or AgentRelationGraphService()

	def load_object_widget(
		self,
		*,
		object_name: str,
		source_uri: str,
		parent: QWidget | None = None,
		scheme: Mapping[str, str] | None = None,
	) -> QWidget:
		return RuntimeWidget(
			object_name=object_name,
			source_uri=source_uri,
			graph_service=self._graph_service,
			scheme=scheme,
			parent=parent,
		)


__all__ = [
	"RelationGraphView",
	"RuntimeWidget",
	"RelationGraphWidgetArtifactFactory",
]
