from __future__ import annotations

import html
from threading import Thread
from typing import Any, Callable, Mapping, Sequence

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

from ..artifact_backends import GraphViewBackendService, load_default_graph_backend_service

class RelationGraphView(QGraphicsView):
	graphItemActivated = Signal(object)
	graphSpreadRequested = Signal(float)
	_GRAPH_PAYLOAD_ROLE = 0
	_MIN_FIT_SCALE = 0.02

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
		try:
			current_scale = float(self.transform().m11())
		except Exception:
			current_scale = 1.0
		if current_scale > 0.0 and current_scale < float(self._MIN_FIT_SCALE):
			self.scale(float(self._MIN_FIT_SCALE) / current_scale, float(self._MIN_FIT_SCALE) / current_scale)
			self.centerOn(scene_rect.center())


class RuntimeWidget(QWidget):
	widgetStateChanged = Signal()
	_refresh_async_result_ready = Signal(object)
	_view_state_async_result_ready = Signal(object)

	def __init__(
		self,
		*,
		object_name: str,
		source_uri: str,
		graph_service: GraphViewBackendService,
		scheme: Mapping[str, str] | None = None,
		parent: QWidget | None = None,
	) -> None:
		super().__init__(parent)
		self._object_name = str(object_name or "graph_view").strip() or "graph_view"
		self._source_uri = str(source_uri or "").strip()
		self._backend_service = graph_service
		self._scheme: dict[str, str] = dict(scheme or {})

		self._graph_snapshot: dict[str, Any] = {}
		self._graph_view_state: dict[str, Any] = {}
		self._graph_layout_spread = 1.0
		self._refresh_inflight = False
		self._refresh_pending_fit_view = False
		self._view_state_request_serial = 0
		self._render_chunk_size = 180
		self._render_command_objects: list[dict[str, Any]] = []
		self._render_next_index = 0
		self._render_fit_view = False
		self._render_center_selected = False
		self._render_was_truncated = False
		self._render_max_commands = 0
		self._render_base_status_text = ""
		self._render_initial_fit_applied = False
		self._render_timer = QtCore.QTimer(self)
		self._render_timer.setSingleShot(True)
		self._render_timer.timeout.connect(self._render_next_chunk)

		self._refresh_async_result_ready.connect(self._handle_refresh_result)
		self._view_state_async_result_ready.connect(self._handle_view_state_result)
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
		if self._refresh_inflight:
			self._refresh_pending_fit_view = self._refresh_pending_fit_view or bool(fit_view)
			return

		self._refresh_inflight = True
		self._view_state_request_serial += 1
		self._status_label.setText("Loading graph…")
		self._refresh_pending_fit_view = False

		def _load_payload() -> None:
			try:
				snapshot_payload = self._backend_service.load_widget_snapshot(
					tool_id=self._object_name,
					source_uri=self._source_uri,
				)
				try:
					self._refresh_async_result_ready.emit(
						{
							"ok": True,
							"snapshot": snapshot_payload,
							"view_state": {},
							"fit_view": bool(fit_view),
							"partial": True,
						}
					)
				except RuntimeError:
					return

				view_state_payload = self._backend_service.load_graph_view_state(
					snapshot_payload,
					layout_spread=float(self._graph_layout_spread),
					selected_kind=str(self._graph_view_state.get("selected_kind") or ""),
					selected_object_id=str(self._graph_view_state.get("selected_object_id") or ""),
				)
				raw_payload = {
					"ok": True,
					"snapshot": snapshot_payload,
					"view_state": view_state_payload,
					"fit_view": bool(fit_view),
					"partial": False,
				}
			except Exception as exc:
				error_text = html.escape(f"{type(exc).__name__}: {exc}")
				raw_payload = {
					"ok": False,
					"snapshot": {
						"tool_id": self._object_name,
						"source_uri": self._source_uri,
						"view_kind": "relations_graph",
						"status_text": "Extension load failed",
						"message": f"Could not load extension: {type(exc).__name__}",
						"detail_html": f"<h3>Extension load failed</h3><p>{error_text}</p>",
						"nodes": [],
						"edges": [],
					},
					"view_state": {},
					"fit_view": bool(fit_view),
					"partial": False,
				}
			try:
				self._refresh_async_result_ready.emit(raw_payload)
			except RuntimeError:
				return

		Thread(target=_load_payload, daemon=True).start()

	@Slot(object)
	def _handle_refresh_result(self, payload: object) -> None:
		if not isinstance(payload, dict):
			self._refresh_inflight = False
			if self._refresh_pending_fit_view:
				self._refresh_pending_fit_view = False
				self.refresh_object(fit_view=True)
			return

		if bool(payload.get("partial")):
			self._graph_snapshot = dict(payload.get("snapshot") or {})
			self._apply_partial_snapshot_state()
			self.widgetStateChanged.emit()
			return

		self._refresh_inflight = False

		fit_view = bool(payload.get("fit_view", False))
		self._graph_snapshot = dict(payload.get("snapshot") or {})
		self._apply_snapshot_state(fit_view=fit_view, graph_view_state=payload.get("view_state"))
		self.widgetStateChanged.emit()

		if self._refresh_pending_fit_view:
			self._refresh_pending_fit_view = False
			self.refresh_object(fit_view=True)

	def _apply_partial_snapshot_state(self) -> None:
		snapshot_payload = dict(self._graph_snapshot or {})
		base_status_text = str(snapshot_payload.get("status_text") or snapshot_payload.get("message") or "Loading graph...")
		self._status_label.setText(self._compose_status_text(base_status_text, "building layout"))
		self.setToolTip(base_status_text)

		summary_html = str(snapshot_payload.get("detail_html") or "<p>Building graph layout...</p>")
		self._summary_browser.setHtml(summary_html)
		self._content_stack.setCurrentWidget(self._relations_page)
		self._render_empty_scene("Building graph layout...")

	def _request_view_state_async(
		self,
		*,
		fit_view: bool,
		center_selected: bool,
		worker_callable: Callable[[], Mapping[str, Any] | None],
	) -> None:
		self._view_state_request_serial += 1
		request_serial = int(self._view_state_request_serial)
		self._status_label.setText("Updating graph…")

		def _load_view_state() -> None:
			try:
				view_state_payload = dict(worker_callable() or {})
				result_payload = {
					"ok": True,
					"request_serial": request_serial,
					"view_state": view_state_payload,
					"fit_view": bool(fit_view),
					"center_selected": bool(center_selected),
				}
			except Exception as exc:
				error_text = html.escape(f"{type(exc).__name__}: {exc}")
				result_payload = {
					"ok": False,
					"request_serial": request_serial,
					"error_text": error_text,
					"fit_view": bool(fit_view),
					"center_selected": bool(center_selected),
				}
			try:
				self._view_state_async_result_ready.emit(result_payload)
			except RuntimeError:
				return

		Thread(target=_load_view_state, daemon=True).start()

	@Slot(object)
	def _handle_view_state_result(self, payload: object) -> None:
		if not isinstance(payload, dict):
			return

		request_serial = int(payload.get("request_serial") or 0)
		if request_serial != int(self._view_state_request_serial):
			return

		if not bool(payload.get("ok")):
			error_text = str(payload.get("error_text") or "Graph update failed")
			self._status_label.setText("Graph update failed")
			self._summary_browser.setHtml(f"<h3>Graph update failed</h3><p>{error_text}</p>")
			self._render_empty_scene("Graph update failed.")
			self.widgetStateChanged.emit()
			return

		view_state_payload = payload.get("view_state")
		resolved_view_state = view_state_payload if isinstance(view_state_payload, Mapping) else {}
		status_text = str(self._graph_snapshot.get("status_text") or self._graph_snapshot.get("message") or "")
		if status_text:
			self._status_label.setText(status_text)
		self._apply_graph_view_state(
			resolved_view_state,
			fit_view=bool(payload.get("fit_view")),
			center_selected=bool(payload.get("center_selected")),
		)
		self.widgetStateChanged.emit()

	def _apply_snapshot_state(self, *, fit_view: bool, graph_view_state: Mapping[str, Any] | None = None) -> None:
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

		resolved_graph_view_state = dict(graph_view_state or {})
		if not resolved_graph_view_state:
			resolved_graph_view_state = dict(self._graph_view_state or {})
		self._apply_graph_view_state(resolved_graph_view_state, fit_view=fit_view)
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

		self._render_scene_commands(render_commands, fit_view=fit_view, center_selected=center_selected)

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

	def _extract_payload_object_id(self, payload: Mapping[str, Any]) -> str:
		for key_name in ("edge_id", "node_id", "object_id", "id"):
			value = str(payload.get(key_name) or "").strip()
			if value:
				return value
		return ""

	def _is_selected_render_command(self, command: Mapping[str, Any]) -> bool:
		selected_kind = str(self._graph_view_state.get("selected_kind") or "").strip().lower()
		selected_object_id = str(self._graph_view_state.get("selected_object_id") or "").strip()
		if not selected_kind or not selected_object_id:
			return False

		payload = command.get("payload") if isinstance(command.get("payload"), Mapping) else {}
		payload_kind = str(payload.get("kind") or "").strip().lower()
		payload_object_id = self._extract_payload_object_id(payload)
		return bool(payload_kind == selected_kind and payload_object_id == selected_object_id)

	def _sample_index_list_evenly(self, index_list: Sequence[int], limit: int) -> list[int]:
		resolved_limit = max(0, int(limit))
		resolved_indexes = [int(index_value) for index_value in index_list]
		if resolved_limit <= 0 or not resolved_indexes:
			return []
		if len(resolved_indexes) <= resolved_limit:
			return list(resolved_indexes)
		if resolved_limit == 1:
			return [resolved_indexes[len(resolved_indexes) // 2]]

		step = (len(resolved_indexes) - 1) / float(resolved_limit - 1)
		sampled_indexes: list[int] = []
		seen_indexes: set[int] = set()
		for sample_index in range(resolved_limit):
			candidate = resolved_indexes[int(round(sample_index * step))]
			if candidate in seen_indexes:
				continue
			seen_indexes.add(candidate)
			sampled_indexes.append(candidate)

		if len(sampled_indexes) < resolved_limit:
			for candidate in resolved_indexes:
				if candidate in seen_indexes:
					continue
				seen_indexes.add(candidate)
				sampled_indexes.append(candidate)
				if len(sampled_indexes) >= resolved_limit:
					break
		return sampled_indexes[:resolved_limit]

	def _truncate_render_commands_for_large_graph(
		self,
		render_command_objects: Sequence[dict[str, Any]],
		max_render_commands: int,
	) -> list[dict[str, Any]]:
		resolved_max = max(1, int(max_render_commands))
		resolved_commands = [dict(command) for command in render_command_objects if isinstance(command, Mapping)]
		if len(resolved_commands) <= resolved_max:
			return resolved_commands

		selected_indexes = [
			index_value
			for index_value, command in enumerate(resolved_commands)
			if self._is_selected_render_command(command)
		]
		selected_sample = self._sample_index_list_evenly(selected_indexes, min(len(selected_indexes), resolved_max))
		selected_set = set(selected_sample)

		edge_line_indexes: list[int] = []
		node_ellipse_indexes: list[int] = []
		edge_text_indexes: list[int] = []
		node_text_indexes: list[int] = []
		other_indexes: list[int] = []

		for index_value, command in enumerate(resolved_commands):
			if index_value in selected_set:
				continue
			command_type = str(command.get("type") or "").strip().lower()
			payload = command.get("payload") if isinstance(command.get("payload"), Mapping) else {}
			payload_kind = str(payload.get("kind") or "").strip().lower()
			if command_type == "line" and payload_kind == "edge":
				edge_line_indexes.append(index_value)
			elif command_type == "ellipse" and payload_kind == "node":
				node_ellipse_indexes.append(index_value)
			elif command_type == "text" and payload_kind == "edge":
				edge_text_indexes.append(index_value)
			elif command_type == "text" and payload_kind == "node":
				node_text_indexes.append(index_value)
			else:
				other_indexes.append(index_value)

		remaining_slots = max(0, resolved_max - len(selected_sample))
		if remaining_slots <= 0:
			return [resolved_commands[index_value] for index_value in selected_sample[:resolved_max]]

		pool_items: list[tuple[str, list[int], float]] = [
			("edge_line", edge_line_indexes, 0.46),
			("node_ellipse", node_ellipse_indexes, 0.24),
			("node_text", node_text_indexes, 0.12),
			("edge_text", edge_text_indexes, 0.12),
			("other", other_indexes, 0.06),
		]
		allocated_counts: dict[str, int] = {}
		for pool_name, index_pool, ratio_value in pool_items:
			desired_count = int(round(remaining_slots * ratio_value))
			if desired_count <= 0 and index_pool:
				desired_count = 1
			allocated_counts[pool_name] = min(len(index_pool), desired_count)

		used_slots = sum(allocated_counts.values())
		remaining_allocation = max(0, remaining_slots - used_slots)
		allocation_order = ["edge_line", "node_ellipse", "node_text", "edge_text", "other"]
		while remaining_allocation > 0:
			allocation_changed = False
			for pool_name in allocation_order:
				pool_size = len(next(pool for name, pool, _ in pool_items if name == pool_name))
				if allocated_counts.get(pool_name, 0) >= pool_size:
					continue
				allocated_counts[pool_name] = int(allocated_counts.get(pool_name, 0)) + 1
				remaining_allocation -= 1
				allocation_changed = True
				if remaining_allocation <= 0:
					break
			if not allocation_changed:
				break

		sampled_indexes: list[int] = []
		for pool_name in allocation_order:
			index_pool = next(pool for name, pool, _ in pool_items if name == pool_name)
			sampled_indexes.extend(self._sample_index_list_evenly(index_pool, int(allocated_counts.get(pool_name, 0))))

		sampled_indexes.extend(selected_sample)
		if len(sampled_indexes) < resolved_max:
			existing_indexes = set(sampled_indexes)
			for index_value in range(len(resolved_commands)):
				if index_value in existing_indexes:
					continue
				sampled_indexes.append(index_value)
				existing_indexes.add(index_value)
				if len(sampled_indexes) >= resolved_max:
					break

		return [resolved_commands[index_value] for index_value in sampled_indexes[:resolved_max]]

	def _render_scene_commands(
		self,
		render_commands: Sequence[Mapping[str, Any]],
		*,
		fit_view: bool,
		center_selected: bool = False,
	) -> None:
		render_command_objects = [dict(command) for command in (render_commands or []) if isinstance(command, Mapping)]
		max_render_commands = 3600
		was_truncated = len(render_command_objects) > max_render_commands
		if was_truncated:
			# Keep a balanced subset so relation edges remain visible in very large graphs.
			render_command_objects = self._truncate_render_commands_for_large_graph(
				render_command_objects,
				max_render_commands=max_render_commands,
			)

		if self._render_timer.isActive():
			self._render_timer.stop()
		self._graph_scene.clear()
		self._render_command_objects = list(render_command_objects)
		self._render_next_index = 0
		self._render_fit_view = bool(fit_view)
		self._render_center_selected = bool(center_selected)
		self._render_was_truncated = bool(was_truncated)
		self._render_max_commands = int(max_render_commands)
		self._render_initial_fit_applied = False
		self._render_base_status_text = str(
			self._graph_snapshot.get("status_text")
			or self._graph_snapshot.get("message")
			or self._status_label.text()
			or ""
		)

		if not self._render_command_objects:
			self._render_empty_scene("No graph content available.")
			return
		self._status_label.setText(self._compose_status_text(self._render_base_status_text, "rendering"))
		self._render_timer.start(0)

	def _render_next_chunk(self) -> None:
		total_commands = len(self._render_command_objects)
		if total_commands <= 0:
			return

		start_index = int(self._render_next_index)
		end_index = min(total_commands, start_index + int(self._render_chunk_size))
		for command in self._render_command_objects[start_index:end_index]:
			self._render_single_command(command)
		self._render_next_index = end_index

		scene_rect = self._graph_scene.itemsBoundingRect()
		if not scene_rect.isNull():
			self._graph_scene.setSceneRect(scene_rect.adjusted(-42, -42, 42, 42))
			if bool(self._render_fit_view) and not bool(self._render_initial_fit_applied):
				# Apply an early fit so large graphs become visible before full render completes.
				self._graph_view.fit_graph_scene()
				self._render_initial_fit_applied = True

		if end_index < total_commands:
			progress_text = f"rendering {end_index}/{total_commands}"
			self._status_label.setText(self._compose_status_text(self._render_base_status_text, progress_text))
			self._render_timer.start(0)
			return

		if self._render_was_truncated:
			status_text = self._compose_status_text(
				self._render_base_status_text,
				f"truncated render ({self._render_max_commands} items)",
			)
		else:
			status_text = str(self._render_base_status_text or "")
		self._status_label.setText(status_text)

		if bool(self._render_fit_view):
			self._graph_view.fit_graph_scene()
		if bool(self._render_center_selected):
			self._center_selected_item()

		self._render_command_objects = []
		self._render_next_index = 0
		self._render_initial_fit_applied = False

	def _render_single_command(self, command: Mapping[str, Any]) -> None:
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
			stroke_color_hex = str(style_payload.get("stroke_color") or "").strip()
			fill_color_hex = str(style_payload.get("fill_color") or "").strip()
			stroke_color = QColor(stroke_color_hex) if stroke_color_hex else QColor(self._resolve_render_color(stroke_role, "#6d7399"))
			fill_color = QColor(fill_color_hex) if fill_color_hex else QColor(self._resolve_render_color(fill_role, "#1f2339"))
			if not stroke_color.isValid():
				stroke_color = QColor(self._resolve_render_color(stroke_role, "#6d7399"))
			if not fill_color.isValid():
				fill_color = QColor(self._resolve_render_color(fill_role, "#1f2339"))

			pen = QPen(stroke_color)
			pen.setWidthF(line_width)
			item = self._graph_scene.addEllipse(x_pos, y_pos, width, height, pen, QBrush(fill_color))
			self._configure_graph_item(item, payload, tooltip)
			return

		if command_type == "line":
			start_x = float(command.get("start_x") or 0.0)
			start_y = float(command.get("start_y") or 0.0)
			end_x = float(command.get("end_x") or 0.0)
			end_y = float(command.get("end_y") or 0.0)

			stroke_role = str(style_payload.get("stroke_role") or "link")
			line_width = float(style_payload.get("line_width") or 1.5)
			stroke_color_hex = str(style_payload.get("stroke_color") or "").strip()
			stroke_color = QColor(stroke_color_hex) if stroke_color_hex else QColor(self._resolve_render_color(stroke_role, "#6280ff"))
			if not stroke_color.isValid():
				stroke_color = QColor(self._resolve_render_color(stroke_role, "#6280ff"))
			pen = QPen(stroke_color)
			pen.setWidthF(line_width)

			item = self._graph_scene.addLine(start_x, start_y, end_x, end_y, pen)
			self._configure_graph_item(item, payload, tooltip)
			return

		if command_type == "text":
			label_text = str(command.get("text") or "")
			text_item = self._graph_scene.addText(label_text)
			text_role = str(style_payload.get("text_role") or "text_primary")
			text_color_hex = str(style_payload.get("text_color") or "").strip()
			text_color = QColor(text_color_hex) if text_color_hex else QColor(self._resolve_render_color(text_role, "#ffffff"))
			if not text_color.isValid():
				text_color = QColor(self._resolve_render_color(text_role, "#ffffff"))
			text_item.setDefaultTextColor(text_color)

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

	def _compose_status_text(self, base_status: str, suffix: str) -> str:
		normalized_base = str(base_status or "").strip()
		normalized_suffix = str(suffix or "").strip()
		if not normalized_base:
			return normalized_suffix
		if not normalized_suffix:
			return normalized_base
		return f"{normalized_base} | {normalized_suffix}"

	def _configure_graph_item(self, item: Any, payload: Mapping[str, Any], tooltip: str) -> None:
		try:
			item.setData(RelationGraphView._GRAPH_PAYLOAD_ROLE, dict(payload or {}))
			item.setToolTip(str(tooltip or "Open graph item"))
			item.setCursor(QCursor(Qt.PointingHandCursor))
			item.setAcceptedMouseButtons(Qt.LeftButton)
		except Exception:
			return

	def _render_empty_scene(self, message_text: str) -> None:
		if self._render_timer.isActive():
			self._render_timer.stop()
		self._render_command_objects = []
		self._render_next_index = 0
		self._render_fit_view = False
		self._render_center_selected = False
		self._render_initial_fit_applied = False
		self._graph_scene.clear()
		text_item = self._graph_scene.addText(str(message_text or "No graph content available."))
		text_item.setDefaultTextColor(QColor(self._scheme.get("col8", "#B7B7B7")))
		text_item.setPos(12, 12)
		rect = text_item.boundingRect()
		self._graph_scene.setSceneRect(rect.adjusted(-18, -18, 18, 18))

	@Slot(object)
	def _handle_graph_item_activated(self, payload: object) -> None:
		snapshot_payload = dict(self._graph_snapshot or {})
		selected_payload = dict(payload) if isinstance(payload, dict) else {}
		layout_spread = float(self._graph_layout_spread)
		fallback_kind = str(self._graph_view_state.get("selected_kind") or "")
		fallback_object_id = str(self._graph_view_state.get("selected_object_id") or "")

		def _worker() -> Mapping[str, Any] | None:
			return self._backend_service.load_graph_view_state_from_payload(
				snapshot_payload,
				selected_payload,
				layout_spread=layout_spread,
				fallback_selected_kind=fallback_kind,
				fallback_selected_object_id=fallback_object_id,
			)

		self._request_view_state_async(
			fit_view=False,
			center_selected=True,
			worker_callable=_worker,
		)

	def _handle_graph_link_clicked(self, url: object) -> None:
		snapshot_payload = dict(self._graph_snapshot or {})
		resolved_url = url.toString() if hasattr(url, "toString") else str(url or "")
		layout_spread = float(self._graph_layout_spread)
		fallback_kind = str(self._graph_view_state.get("selected_kind") or "")
		fallback_object_id = str(self._graph_view_state.get("selected_object_id") or "")

		def _worker() -> Mapping[str, Any] | None:
			return self._backend_service.load_graph_view_state_from_link(
				snapshot_payload,
				resolved_url,
				layout_spread=layout_spread,
				fallback_selected_kind=fallback_kind,
				fallback_selected_object_id=fallback_object_id,
			)

		self._request_view_state_async(
			fit_view=False,
			center_selected=True,
			worker_callable=_worker,
		)

	@Slot(float)
	def _handle_graph_spread_requested(self, factor: float) -> None:
		self._graph_layout_spread = max(0.35, min(3.5, float(self._graph_layout_spread) * float(factor or 1.0)))
		snapshot_payload = dict(self._graph_snapshot or {})
		layout_spread = float(self._graph_layout_spread)
		selected_kind = str(self._graph_view_state.get("selected_kind") or "")
		selected_object_id = str(self._graph_view_state.get("selected_object_id") or "")

		def _worker() -> Mapping[str, Any] | None:
			return self._backend_service.load_graph_view_state(
				snapshot_payload,
				layout_spread=layout_spread,
				selected_kind=selected_kind,
				selected_object_id=selected_object_id,
			)

		self._request_view_state_async(
			fit_view=False,
			center_selected=False,
			worker_callable=_worker,
		)

	def _center_selected_item(self) -> None:
		selected_kind = str(self._graph_view_state.get("selected_kind") or "")
		selected_object_id = str(self._graph_view_state.get("selected_object_id") or "")
		if not selected_kind or not selected_object_id:
			return
		center = self._backend_service.load_graph_item_center(
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
		if bool(self._graph_view_state):
			self._apply_graph_view_state(self._graph_view_state, fit_view=False)


class RelationGraphWidgetArtifactFactory:
	def __init__(self, graph_service: GraphViewBackendService | None = None) -> None:
		self._backend_service = graph_service or load_default_graph_backend_service()
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
			graph_service=self._backend_service,
			scheme=scheme,
			parent=parent,
		)


__all__ = [
	"RelationGraphView",
	"RuntimeWidget",
	"RelationGraphWidgetArtifactFactory",
]
