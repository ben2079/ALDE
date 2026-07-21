from __future__ import annotations    
## ai_ide_v1756.py
# Maintainer contact: see repository README.
from PySide6.QtCore import QObject, QEvent, QRect

import os
import sys
import importlib
import json
import base64
import binascii
import uuid
import html
import math
import re
import time
import subprocess
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

# Keep both repository roots on sys.path so local imports work in direct-script
# mode and when the module is imported through the lowercase package alias.
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

_workspace_root = os.path.dirname(_repo_root)
if _workspace_root not in sys.path:
    sys.path.insert(0, _workspace_root)

_projects_root = os.path.dirname(_workspace_root)
if _projects_root not in sys.path:
    sys.path.insert(0, _projects_root)

_GUI_ENV_CONFIG_ENV_NAME = "AI_IDE_GUI_ENV_CONFIG_PATH"
_GUI_ENV_CONFIG_FILENAME = "gui_env.json"
_GUI_ENV_CONFIG_FORMAT = "ai_ide_gui_env_v1"



def _stringify_gui_env_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (str, int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _gui_env_candidate_paths() -> list[Path]:
    explicit_path = str(os.getenv(_GUI_ENV_CONFIG_ENV_NAME, "") or "").strip()
    candidate_paths: list[Path] = []
    if explicit_path:
        raw_path = Path(explicit_path)
        if raw_path.is_absolute():
            candidate_paths.append(raw_path)
        else:
            for base_dir in (Path(_workspace_root), Path(_repo_root)):
                candidate_paths.append((base_dir / raw_path).resolve())
    else:
        for base_dir in (Path(_workspace_root) / "AppData", Path(_repo_root) / "AppData"):
            candidate_paths.append((base_dir / _GUI_ENV_CONFIG_FILENAME).resolve())

    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for path in candidate_paths:
        normalized_path = str(path)
        if normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        unique_paths.append(path)
    return unique_paths


def _default_gui_env_payload() -> dict[str, Any]:
    env_payload: dict[str, str] = {}
    for env_name, default_value in _GUI_ENV_DEFAULTS.items():
        env_payload[env_name] = _stringify_gui_env_value(os.getenv(env_name, default_value))
    return {
        "format": _GUI_ENV_CONFIG_FORMAT,
        "description": "GUI environment configuration for ai_ide_v1756.py and jstree_widget.py.",
        "env": env_payload,
    }


def _normalize_gui_env_entries(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}

    env_payload = payload.get("env")
    if not isinstance(env_payload, dict):
        env_payload = payload.get("environment")
    if not isinstance(env_payload, dict):
        env_payload = payload

    normalized_entries: dict[str, str] = {}
    for env_name, raw_value in env_payload.items():
        if not isinstance(env_name, str):
            continue
        normalized_name = env_name.strip()
        if not normalized_name or re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized_name) is None:
            continue
        normalized_entries[normalized_name] = _stringify_gui_env_value(raw_value)
    return normalized_entries


def _ensure_gui_env_config_file() -> Path | None:
    candidate_paths = _gui_env_candidate_paths()
    for path in candidate_paths:
        if path.exists():
            return path
    if not candidate_paths:
        return None

    target_path = candidate_paths[0]
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(_default_gui_env_payload(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target_path
    except Exception:
        return None


def _load_gui_env_config_into_process() -> Path | None:
    ensured_path = _ensure_gui_env_config_file()
    for path in _gui_env_candidate_paths():
        if not path.exists() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for env_name, env_value in _normalize_gui_env_entries(payload).items():
            if env_value == "":
                continue
            os.environ.setdefault(env_name, env_value)
        return path
    return ensured_path


_GUI_ENV_CONFIG_PATH = _load_gui_env_config_into_process()

# Workaround für GNOME GLib-GIO-ERROR mit antialiasing
# Verhindert Crash durch fehlende GNOME-Settings-Keys
os.environ.setdefault('GDK_BACKEND', 'x11')
os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')

# Unterdrücke GLib Warnings (optional, falls sie stören)
import warnings
warnings.filterwarnings('ignore', category=Warning)
from typing import Any, Callable, Final, List, Mapping, Optional, Sequence
from io import BytesIO
import mimetypes


def _shutdown_loky_runtime() -> None:
    """Best-effort cleanup for reusable loky executors before interpreter exit."""
    get_reusable_executor = None
    for module_name in ("joblib.externals.loky", "loky"):
        try:
            module = importlib.import_module(module_name)
            get_reusable_executor = getattr(module, "get_reusable_executor", None)
            if callable(get_reusable_executor):
                break
        except Exception:
            continue

    if not callable(get_reusable_executor):
        return

    try:
        executor = get_reusable_executor()
    except Exception:
        return

    if executor is None:
        return

    try:
        executor.shutdown(wait=True, kill_workers=True)
    except TypeError:
        try:
            executor.shutdown(wait=True)
        except Exception:
            pass
    except Exception:
        pass


def _split_data_uri(data: str) -> tuple[str | None, str]:
    """Split a possible data-URI into (mime, base64_payload).

    Accepts strings like: data:image/png;base64,AAAA...
    Returns (None, original) when it's not a data-URI.
    """
    s = data.strip()
    if not s.lower().startswith("data:"):
        return None, data
    try:
        header, payload = s.split(",", 1)
    except ValueError:
        return None, data
    mime = None
    # data:<mime>;base64
    try:
        meta = header[5:]
        parts = [p.strip() for p in meta.split(";") if p.strip()]
        if parts and "/" in parts[0]:
            mime = parts[0]
    except Exception:
        mime = None
    return mime, payload


def _infer_image_ext(image_bytes: bytes, mime: str | None = None) -> str:
    if mime:
        m = mime.lower()
        if "png" in m:
            return ".png"
        if "webp" in m:
            return ".webp"
        if "jpeg" in m or "jpg" in m:
            return ".jpg"

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:16]:
        return ".webp"
    return ".bin"


def decode_image_payload(payload: object) -> tuple[bytes, str | None]:
    """Decode image payload to raw bytes.

    Supports:
    - bytes/bytearray
    - base64 string
    - data-URI (data:image/png;base64,....)
    - list/tuple where the first element is any of the above

    Returns (bytes, mime_if_known).
    """
    if payload is None:
        raise ValueError("No image payload")

    if isinstance(payload, (list, tuple)):
        if not payload:
            raise ValueError("Empty image payload list")
        payload = payload[0]

    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload), None

    if isinstance(payload, str):
        mime, b64 = _split_data_uri(payload)
        b64 = "".join(b64.split())
        if len(b64) % 4:
            b64 += "=" * (4 - (len(b64) % 4))
        try:
            return base64.b64decode(b64, validate=False), mime
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Invalid base64 image payload: {exc}") from exc

    raise TypeError(f"Unsupported image payload type: {type(payload)!r}")


def save_generated_image(image_bytes: bytes, *, mime: str | None = None) -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "AppData" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = _infer_image_ext(image_bytes, mime=mime)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"gen_{stamp}_{uuid.uuid4().hex[:8]}{ext}"
    out_path = out_dir / name
    out_path.write_bytes(image_bytes)
    return out_path

# ---------------------------------------------------------------------------
#  external file viewer — provides widgets & helper used for the „open file“
#  feature below.  Keeping this import clustered here avoids a hard runtime
#  dependency for users of ai_ide_v1.7.5.py that never invoke “open file”.
# ---------------------------------------------------------------------------

try:
    try:
        from file_viewer import (
            classify as _fv_classify,
            ImageWidget as _FVImageWidget,
            ChatImageWidget as _FVChatImageWidget,
            PdfWidget as _FVPdfWidget,
            MarkdownWidget as _FVMarkdownWidget,
            TextWidget as _FVTextWidget,
            ZoomImageWidget as _FVZoomImageWidget,
        )
    except Exception:
        # Fallback for historical “run as script” mode.
        from file_viewer import (  # type: ignore
            classify as _fv_classify,
            ImageWidget as _FVImageWidget,
            ChatImageWidget as _FVChatImageWidget,
            PdfWidget as _FVPdfWidget,
            MarkdownWidget as _FVMarkdownWidget,
            TextWidget as _FVTextWidget,
            ZoomImageWidget as _FVZoomImageWidget,
        )
except Exception:    # pragma: no cover – soft-fail, detailed handling below
    _fv_classify = None  # type: ignore
    _FVImageWidget = _FVPdfWidget = _FVMarkdownWidget = _FVTextWidget = None  # type: ignore
    _FVChatImageWidget = None  # type: ignore

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    def load_dotenv(*_args, **_kwargs):
        return False

from PySide6.QtCore import( Qt, QSize, Signal, Slot, QTimer, QEvent,
                            QSettings, QByteArray, QPoint, QPointF )            # >>>  NEU ai_ide_v1.7.5.py
from PySide6 import QtCore

from PySide6.QtGui import (
    QAction,
    QBrush,
    QIcon,
    QCursor,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QIntValidator,
    QMouseEvent,
    QTextCursor,
    QTextOption,
    QFontMetrics,
    QPixmap,
    QPainter,
    QColor,
    QPen,
    QPalette,
    QKeySequence,
    QShowEvent,
)

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QInputDialog,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QToolButton,
    QSplitter,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QMenuBar,
    QStyle,
    QProxyStyle,
    QTextBrowser,

)

# --------------------------------------------------------------------------
#  3rd-party back-end  (neighbour module)
# --------------------------------------------------------------------------

try:
    if __package__:
        from .chat_runtime import ChatCom, ImageDescription, ImageCreate, ChatHistory  # type: ignore
    else:
        from chat_runtime import ChatCom, ImageDescription, ImageCreate, ChatHistory  # type: ignore
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from alde.chat_runtime import ChatCom, ImageDescription, ImageCreate, ChatHistory  # type: ignore  # noqa: E402
    else:
        raise

try:
    if __package__:
        from .litehigh import QSHighlighter, MDHighlighter, JSONHighlighter, TOMLHighlighter, YAMLHighlighter  # type: ignore
    else:
        from alde.litehigh import QSHighlighter, MDHighlighter, JSONHighlighter, TOMLHighlighter, YAMLHighlighter  # type: ignore
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from litehigh import QSHighlighter, MDHighlighter, JSONHighlighter, TOMLHighlighter, YAMLHighlighter  # type: ignore
    else:
        raise

try:
    if __package__:
        from .jstree_widget import JsonTreeWidgetWithToolbar  # type: ignore
    else:
        from alde.jstree_widget import JsonTreeWidgetWithToolbar  # type: ignore
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from jstree_widget import JsonTreeWidgetWithToolbar  # type: ignore
    else:
        raise

try:
    if __package__:
        from .ui_glyphs import (  # type: ignore
            DROPDOWN_COLLAPSED_GLYPH,
            DROPDOWN_COLLAPSED_PREFIX,
            DROPDOWN_EXPANDED_GLYPH,
            DROPDOWN_EXPANDED_PREFIX,
            dropdown_prefix,
        )
    else:
        from alde.ui_glyphs import (  # type: ignore
            DROPDOWN_COLLAPSED_GLYPH,
            DROPDOWN_COLLAPSED_PREFIX,
            DROPDOWN_EXPANDED_GLYPH,
            DROPDOWN_EXPANDED_PREFIX,
            dropdown_prefix,
        )
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from ui_glyphs import (  # type: ignore
            DROPDOWN_COLLAPSED_GLYPH,
            DROPDOWN_COLLAPSED_PREFIX,
            DROPDOWN_EXPANDED_GLYPH,
            DROPDOWN_EXPANDED_PREFIX,
            dropdown_prefix,
        )
    else:
        raise

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


# --------------------------------------------------------------------------
# Shutdown safety toggles
# --------------------------------------------------------------------------

_HISTORY_FLUSHED_ONCE = False


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip() in {"1", "true", "True", "yes", "Yes", "on", "On"}


def _maybe_flush_history(chat_obj=None) -> None:
    """Flush history at most once. 

    Controlled by env var:
      - AI_IDE_DISABLE_HISTORY_FLUSH=1  (skip history persistence)
            - AI_IDE_ENABLE_HISTORY_FLUSH_ON_QUIT=1  (enable flush hooks on quit/close)
    """
    global _HISTORY_FLUSHED_ONCE
    if _HISTORY_FLUSHED_ONCE:
        return
    if _env_truthy("AI_IDE_DISABLE_HISTORY_FLUSH", "0"):
        return
        # PySide6 can segfault when flushing during Qt shutdown hooks on some
        # environments (observed as EXIT:139). Keep shutdown flush disabled unless
        # explicitly enabled.
        if not _env_truthy("AI_IDE_ENABLE_HISTORY_FLUSH_ON_QUIT", "0"):
                return

    _HISTORY_FLUSHED_ONCE = True
    try:
        if chat_obj is not None:
            chat_obj._flush()
        else:
            ChatHistory._flush()  # type: ignore[misc]
    except Exception:
        pass

# ═══════════════════════  Farben / Style  ══════════════════════════════════

SCHEME_BLUE = {
    "col1": "#3a5fff",
    "col2": "#6280ff",
    "menu_bg": "#000000",
    "menu_sel": "rgba(58,95,255,72)",
    "col11": "#3a5fff",
    "col12": "rgba(58,95,255,48)",
}


SCHEME_GREEN = {
    "col1": "#0fe913",
    "col2": "#58ed5b",
    "menu_bg": "#000000",
    "menu_sel": "rgba(88,237,91,72)",
    "col11": "#0fe913",
    "col12": "rgba(88,237,91,48)",
}


ACCENT_ORDER: tuple[str, ...] = ("green", "blue", "system")


SCHEME_GREY = {
    "col5": "#080808",
    "col6": "#E3E3DED6",
    "col7": "#0b0b0b",
    "col8": "#E3E3DED6",
    "col9": "#101010",
    "col10":"#1f1f1f",
    "col11":"#4a4a4a",
    "px1": "4px",
    "col12": "rgba(88,237,91,48)"
}


SCHEME_DARK = {
    "col5": "#080808",
    "col6": "#E3E3DED6",
    "col7": "#0b0b0b",
    "col8": "#E3E3DED6",
    "col9": "#101010",
    "col10":"#1f1f1f",
    "col11":"#0fe913",
    "px1": "4px",
    "col12": "rgba(88,237,91,48)",
}


def _rgba_from_qcolor(color: QColor, alpha: int) -> str:
    alpha_clamped = max(0, min(255, int(alpha)))
    return f"rgba({color.red()},{color.green()},{color.blue()},{alpha_clamped})"


def _system_accent_scheme() -> dict[str, str]:
    """Build an accent scheme from the current Qt/system highlight color."""
    app = QApplication.instance()
    color = QColor()
    if app is not None:
        try:
            color = app.palette().color(QPalette.Highlight)
        except Exception:
            color = QColor()
    if not color.isValid():
        color = QColor("#9a9a9a")
    secondary = color.lighter(125)
    primary_name = color.name(QColor.HexRgb)
    secondary_name = secondary.name(QColor.HexRgb)
    return {
        "col1": primary_name,
        "col2": secondary_name,
        "menu_bg": "#000000",
        "menu_sel": _rgba_from_qcolor(color, 72),
        "col11": primary_name,
        "col12": _rgba_from_qcolor(color, 48),
    }


def _accent_from_name(name: str | None) -> dict[str, str]:
    normalized = str(name or "green").strip().lower()
    if normalized == "blue":
        return SCHEME_BLUE
    if normalized == "system":
        return _system_accent_scheme()
    return SCHEME_GREEN


def _normalize_accent_name(name: str | None) -> str:
    normalized = str(name or "green").strip().lower()
    return normalized if normalized in ACCENT_ORDER else "green"

# Traffic-light palette aligned with the agency docs visuals.
SIGNAL_RED = "#ff6b7d"
SIGNAL_YELLOW = "#ffd166"
SIGNAL_GREEN = "#7bd88f"


# ------------------------------------------------------------------ style --


_STYLE = """
QMainWindow {{
    background:  {col5};
    color:       {col6};
    }}

QWidget {{
    background:  {col7};
    color:       {col6};
    font-size:   14px;
    }}

QStatusBar {{
    background: {col5};
    color: {col6};
    font-size: 13px;
    }}

QToolBar {{
    background: {col5};
    border: 1px solid {col10};
    border-radius: 14px;
    padding: 4px;
    spacing: 4px;
    }}

QToolBar::handle {{
    background: transparent;

    }}

QToolButton {{
    background: {col7};
    color: {col6};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 4px;
    }}

QToolButton:hover {{
    background: {col7};
    border: 1px solid transparent;
    }}

QToolButton:pressed,
QToolButton:checked {{
    background: {col7};
    color: {col6};
    border: 1px solid transparent;
    }}

/* Tab widget / pane + tabs aligned with Explorer dock visuals */

QTabWidget::pane {{
    background: {col9};
    border-left: 1px solid {col10};
    border-right: 1px solid {col10};
    border-bottom: 1px solid {col10};
    border-top: none;
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
    border-bottom-left-radius: 14px;
    border-bottom-right-radius: 14px;
    margin: 0px;
    }}

QTabBar {{
    background: {col7};
    border: none;
    }}

QTabBar::tab {{
    background: {col7};
    color: {col6};
    border-top: 1px solid {col10};
    border-left: none;
    border-right: none;
    border-bottom: none;
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
    padding: 3px 10px;
    min-height: 16px;
    }}

QTabBar::tab:first {{
    border-left: 1px solid {col10};
    border-top-left-radius: 14px;
    }}

QTabBar::tab:last {{
    border-right: 1px solid {col10};
    border-top-right-radius: 14px;
    }}

QTabBar::tab:only-one {{
    border-left: 1px solid {col10};
    border-right: 1px solid {col10};
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    }}

QTabBar::tab:hover {{ 
    background: {col7};
    border-top: 1px solid {col10};
    border-left: none;
    border-right: none;
    border-bottom: none;
    }}

QTabBar::tab:selected {{ 
    background: {col7};
    color: {col1};
    border: 1px solid {col1};
    border-left: 1px solid {col7};
    border-bottom: none;
    }}

QTabBar::tab:first:hover {{
    border-left: 1px solid {col10};
    border-top-left-radius: 14px;
    }}

QTabBar::tab:last:hover {{
    border-right: 1px solid {col10};
    border-top-right-radius: 14px;
    }}

QTabBar::tab:only-one:hover {{
    border-left: 1px solid {col10};
    border-right: 1px solid {col10};
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    }}

QTabBar::tab:first:selected {{
    border-left: 1px solid {col7};
    border-top-left-radius: 14px;
    }}

QTabBar::tab:last:selected {{
    border-top-right-radius: 14px;
    }}

QTabBar::tab:only-one:selected {{
    border-left: 1px solid {col7};
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    }}

QSplitter::handle:horizontal {{
    margin: 0px {_SPLITTER_SIDE_INSET_PX}px;
    border-top: 2px solid transparent;
    border-radius: 999px;
    }}

QSplitter::handle:vertical {{
    margin: 0px;
    border-left: 2px solid transparent;
    border-radius: 999px;
    }}

QSplitter::handle:hover,
QSplitter::handle:pressed {{
    border-color: transparent;
    background: transparent;
    }}

QPushButton {{
    background: {col7};
    color: {col6};
    border-radius: 3px; 
    padding: 4px 8px;
    border: 1px solid {col10};
    }}

QPushButton:hover {{
    background: {col7};
    color: {col6};
    border: 1px solid {col10};
    }}

QPushButton:pressed {{
    background: {col7};
    color: {col6};
    border: 1px solid {col10};
    }}

QTextEdit, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {col9};
    color: {col6};
    border: 1px solid {col10};
    border-radius: 8px;
    padding: 3px 6px;
    }}

QComboBox::drop-down {{
    border: none;
    background: transparent;
    }}

QComboBox QAbstractItemView {{
    background: {col9};
    color: {col6};
    border: 1px solid {col10};
    selection-background-color: {menu_sel};
    }}

QDockWidget {{
    background: {col5};
    border: none;
    }}

QDockWidget::separator {{ 
    background: {col5}; width: {px1} 
    }}

QDockWidget::separator:hover {{ 
    background: {col5} 
    }}

QTreeView, QListView {{
    background: {col9};
    color: {col6};
    border: 1px solid {col10};
    border-radius: 10px;
    }}

QTreeView::item:hover,
QListView::item:hover {{
    background: {menu_sel};
    }}

QTreeView::item:selected,
QListView::item:selected {{
    background: {menu_sel};
    color: {col6};
    }}


/*# <---- changes 15.07.2025 AI Chat I/O Widget */

 
#aiInput {{                 /* was  #aiInput  */
    background: {col9};
    border: 1px solid {col1};   /* 1 px, Akzentfarbe */
    border-radius: 15px;
    padding: 5px;
    margin     : 0px 0px 2px 0px;      /* ⇐ 2 px Lücke nach unten */

    }}

         
/* --- NEW: sichtbarer Rahmen um die AI-Ausgabe --- changes 15.07.2025 --- */

    #aiOutput {{
        background: {col9};
        border: 1px solid {col10};   /* 1 px, Akzentfarbe */
        border-radius: 5px;         /* leicht abgerundet */
        padding: 5px;               /* Luft innen */
        margin: 5px 10px 5px 5px;   /* etwas Abstand zu Nachbarn */   
    }}
  
 """

# ─── style‐erweiterung # <– 10.07.2025 ───────────────────────────────────────── ─────
#   
#   NEU: blendet alle QMainWindow-Separatoren (die „Dock-Splitter-Griffe“)
#       unsichtbar aus, erhält aber eine 6-px breite Drag-Fläche.

_SEP_QSS = """
/*  MainWindow-Splitter: unsichtbar, aber weiter greifbar  */
QMainWindow::separator              {{ background: {col5};      width: 4px; }}
QMainWindow::separator:horizontal   {{ background: {col5};      height: 6px;}}
QMainWindow::separator:hover        {{ background: {col2}; }}
"""

# ─── Tooltip-QSS  (schwarz, opacity 230, weiße Schrift, runde Ecken) ──────
# ─── Tooltip-QSS  –  schwarz (alpha≈200/255) + weiße Schrift ──────────────
_TT_QSS = """
QToolTip {{
    background-color: rgba(0, 0, 0, 200);   /* → sehr dunkles Grau, leicht transparent   */
    color            : #FFFFFF;             /* → reinweiß                                 */
    border           : 1px solid #FFFFFF;   /* → schmale, weiße Kontur                    */
    border-radius    : 6px;                 /* → dezente Abrundung                       */
    padding          : 4px 8px;             /* → Luft um den Text                         */
}}
"""


_MENU_STYLE = """

/* ───────────────────── Menus ─────────────────────────────────── */

QMenuBar {{
    background: {menu_bg};
    color: {col6};
    font-size: 14px;
    icon-size: 14px;
}}

QMenuBar::item {{
    color: {col6};
    padding: 4px 8px;
    border-radius: 6px;
}}

QMenu {{
    background: {menu_bg};
    color: {col6};
    font-size: 14px;
    icon-size: 14px;
    border: 1px solid {col10};
    border-radius: 10px;
    padding: 5px;
}}

QMenu::item {{
    color: {col6};
    border-radius: 10px;
    padding: 5px 20px;
    margin: 0px 0px;
}}

QMenu::item:selected {{
    background-color: {menu_sel};
    border: none;
    margin: 3px 0px;
}} 

/* ───────── optional: add subtle hover to *bar* items ───────── */
QMenuBar::item:selected {{
    background: {menu_sel};
     border-radius:3px;
}}"""


def _build_scheme(accent: dict, base: dict) -> dict:
    return {**base, **accent}


def _color_with_alpha(color_value: str, alpha: int, *, fallback: str) -> str:
    """Convert a color token to rgba(...) with the requested alpha channel."""
    color = QColor(str(color_value or ""))
    if not color.isValid():
        return fallback
    alpha_clamped = max(0, min(255, int(alpha)))
    return f"rgba({color.red()},{color.green()},{color.blue()},{alpha_clamped})"


def _splitter_handle_palette(scheme: dict[str, str]) -> tuple[str, str, str]:
    """Return (idle, hover, pressed) colors for splitter handles."""
    base_color = str(scheme.get("col10") or "#404040")
    accent_color = str(scheme.get("col2") or scheme.get("col1") or "#58ed5b")
    idle = _color_with_alpha(base_color, 96, fallback="rgba(64,64,64,96)")
    hover = accent_color
    pressed = accent_color
    return idle, hover, pressed


_SURFACE_INSET_PX = 8
_SURFACE_BORDER_RADIUS_PX = 14
_SURFACE_BORDER_WIDTH_PX = 1
_SPLITTER_SIDE_INSET_PX = 8

# ─── helper zum Aufbringen des Stylesheets  ───────────────────────────────

# --- 2. apply also to the QApplication so that QMenu benefits --------------
# --------------------------------------------------------------------------
#  erweitertes _apply_style() –  fügt das neue Fragment beim Zusammenbau an
# --------------------------------------------------------------------------
def _apply_style(widget, scheme, *, _qapp_apply=True):             # patched
    """
    Compile the global stylesheet from the template fragments
    and apply it to *widget* and – optionally – QApplication.
    """
    import string

    # Allow disabling stylesheet application for crash bisection.
    if os.getenv("AI_IDE_NO_STYLE", "0") == "1":
        try:
            widget.setStyleSheet("")
            if _qapp_apply and QApplication.instance():
                QApplication.instance().setStyleSheet("")
        except Exception:
            pass
        return

    template = _STYLE + _MENU_STYLE + _SEP_QSS + _TT_QSS
    fmt = string.Formatter()

    substitutions = dict(scheme)
    substitutions.update(
        {
            "_SURFACE_INSET_PX": _SURFACE_INSET_PX,
            "_SURFACE_BORDER_RADIUS_PX": _SURFACE_BORDER_RADIUS_PX,
            "_SURFACE_BORDER_WIDTH_PX": _SURFACE_BORDER_WIDTH_PX,
            "_SPLITTER_SIDE_INSET_PX": _SPLITTER_SIDE_INSET_PX,
        }
    )

    pieces: list[str] = []
    for txt, key, spec, conv in fmt.parse(template):
        pieces.append(txt)
        if key is None:
            continue
        pieces.append(str(substitutions.get(key, "{"+key+"}")))

    qss = "".join(pieces)

    # Our templates historically used doubled braces (`{{` / `}}`) so they
    # could be fed through `str.format`. Since we now do a custom, key-safe
    # substitution, we need to unescape them back to normal QSS braces.
    qss = qss.replace("{{", "{").replace("}}", "}")

    widget.setStyleSheet(qss)
    if _qapp_apply and QApplication.instance():
        QApplication.instance().setStyleSheet(qss)


'''Patch – remove the duplicated helper and keep ONE really safe version
=====================================================================

The second definition of `_apply_style()` (≈ line 560) overwrites the
first, *robust* implementation.  
Because that late version still delegates the real work to
`str.format_map()`, any placeholder like  
def _apply_style(widget: QWidget, scheme: dict) -> None:
    """
    Globale Style-Applikation: Grund-QSS  + Menü-QSS + Separator-QSS
    """
    qss = (_STYLE + _MENU_STYLE + _SEP_QSS).format(**scheme)
    widget.setStyleSheet(qss)
'''
# ─── hardened stylesheet formatter ─────────────────────────────────────────
#
# put this right after the *_STYLE / _MENU_STYLE / _SEP_QSS* definitions
# (i.e. before the first call to `_apply_style`).

import string                                  # already imported once – harmless

# --- 2.  apply also to the QApplication so that QMenu benefits --------------

def _draw_fallback(symbol: str = "x", stroke_color: str = "#ffffff") -> QIcon:
    """
    Paints a very small 32 × 32 px pixmap with simple fallback symbols.
    Used whenever no SVG file (and no theme-icon) exists.
    """
    size = 32
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    pen = QPen(QColor(str(stroke_color or "#ffffff")))
    pen.setWidth(4)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(pen)

    if symbol == "+":
        p.drawLine(size // 2, 6, size // 2, size - 6)
        p.drawLine(6, size // 2, size - 6, size // 2)
    elif symbol == "(/)":
        # Chat marker: draw a compact (/) glyph.
        chat_pen = QPen(p.pen())
        chat_pen.setWidth(3)
        p.setPen(chat_pen)

        p.drawLine(9, 8, 7, size // 2)
        p.drawLine(7, size // 2, 9, size - 8)

        p.drawLine(size - 9, 8, size - 7, size // 2)
        p.drawLine(size - 7, size // 2, size - 9, size - 8)

        p.drawLine(13, size - 8, 19, 8)
    elif symbol == "[/]":
        # Draw a compact bracket-slash-bracket glyph for the control panel.
        control_pen = QPen(p.pen())
        control_pen.setWidth(3)
        p.setPen(control_pen)

        p.drawLine(7, 8, 7, size - 8)
        p.drawLine(7, 8, 12, 8)
        p.drawLine(7, size - 8, 12, size - 8)

        p.drawLine(14, size - 8, 19, 8)

        p.drawLine(size - 7, 8, size - 7, size - 8)
        p.drawLine(size - 12, 8, size - 7, 8)
        p.drawLine(size - 12, size - 8, size - 7, size - 8)
    elif symbol == "[\\_|":
        # Left-toolbar marker: [\_| (bracket, backslash, underscore, bar).
        left_pen = QPen(p.pen())
        left_pen.setWidth(3)
        p.setPen(left_pen)

        p.drawLine(6, 8, 6, size - 8)
        p.drawLine(6, 8, 11, 8)
        p.drawLine(6, size - 8, 11, size - 8)

        p.drawLine(12, 9, 18, size - 10)
        p.drawLine(18, size - 10, 24, size - 10)
        p.drawLine(24, 8, 24, size - 8)

    else:                             # default:  ❌
        p.drawLine(8, 8, size - 8, size - 8)
        p.drawLine(8, size - 8, size - 8, 8)
    p.end()
    return QIcon(pm)


def _draw_circle_icon() -> QIcon:
    """Paint a simple neutral circle icon for the color-scheme menu action."""
    size = 32
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    pen = QPen(QColor("#666666"))
    pen.setWidth(3)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(6, 6, size - 12, size - 12)
    p.end()
    return QIcon(pm)


def _draw_window_control_icon(kind: str, *, size: int = 12, color: str = "#E3E3DED6") -> QIcon:
    """Paint minimalist window control icons (minimize, maximize, restore, close)."""
    size_px = max(10, int(size))
    pm = QPixmap(size_px, size_px)
    pm.fill(Qt.transparent)

    pad = max(1, size_px // 4)
    stroke = max(1, size_px // 7)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidth(stroke)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    if kind == "minimize":
        y = size_px - pad - 1
        painter.drawLine(pad, y, size_px - pad, y)
    elif kind == "maximize":
        painter.drawRect(pad, pad, max(1, size_px - (2 * pad)), max(1, size_px - (2 * pad)))
    elif kind == "restore":
        inset = max(1, size_px // 5)
        w = max(1, size_px - (2 * pad) - inset)
        h = max(1, size_px - (2 * pad) - inset)
        painter.drawRect(pad + inset, pad, w, h)
        painter.drawRect(pad, pad + inset, w, h)
    else:
        painter.drawLine(pad, pad, size_px - pad, size_px - pad)
        painter.drawLine(pad, size_px - pad, size_px - pad, pad)

    painter.end()
    return QIcon(pm)


def _icon(name: str) -> QIcon:
    """
    Robust icon loader.

    1. look for an SVG file in ./symbols/
    2. fall-back to the current icon theme (QIcon.fromTheme)
    3. fall-back to a Qt standard icon
    4. finally paint our own ❌ / ➕ so that *something* is always visible
    """
    # ----------------------------------------------------- 1.  local SVG
    p = Path(__file__).with_name("symbols") / name
    if p.is_file():
        return QIcon(str(p))

    # If no QApplication yet, avoid any calls that require a QGuiApplication
    # (QIcon.fromTheme, QApplication.style(), QPixmap painting, ...).
    # Returning an empty QIcon is safe at import-time; callers can replace
    # it later when the QApplication exists.
    if QApplication.instance() is None:
        return QIcon()

    # If no QApplication yet, avoid any calls that require a QGuiApplication
    # (QIcon.fromTheme, QApplication.style(), QPixmap painting, ...).
    # Returning an empty QIcon is safe at import-time; callers can replace
    # it later when the QApplication exists.
    if QApplication.instance() is None:
        return QIcon()

    # ----------------------------------------------------- 2.  theme icon
    themed = QIcon.fromTheme(name.removesuffix(".svg"))
    if not themed.isNull():
        return themed

    # ----------------------------------------------------- 3.  Qt fallback
    std = QApplication.style().standardIcon(QStyle.SP_FileIcon)
    if not std.isNull():
        return std

    # ----------------------------------------------------- 4.  painted pixmap
    return _draw_fallback("+" if "plus" in name else "x")


def _icon_with_opacity(name: str, opacity: float = 0.72, size: int = 18) -> QIcon:
    """Return a dimmed icon variant for quieter idle toolbar/action visuals."""
    base_icon = _icon(name)
    if base_icon.isNull() or QApplication.instance() is None:
        return base_icon

    icon_size = QSize(size, size)
    source = base_icon.pixmap(icon_size)
    if source.isNull():
        return base_icon

    target = QPixmap(source.size())
    target.fill(Qt.transparent)

    painter = QPainter(target)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.setOpacity(max(0.0, min(1.0, float(opacity))))
    painter.drawPixmap(0, 0, source)
    painter.end()
    return QIcon(target)


def _content_resize_icon(reset: bool = False, size: int = 18, color: str = "#666666") -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor(color), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    pad = max(3, int(size * 0.22))
    span = max(4, int(size * 0.28))
    max_x = size - pad
    max_y = size - pad

    if reset:
        painter.drawLine(pad, pad + span, pad, pad)
        painter.drawLine(pad, pad, pad + span, pad)
        painter.drawLine(max_x - span, max_y, max_x, max_y)
        painter.drawLine(max_x, max_y, max_x, max_y - span)
    else:
        painter.drawLine(pad, max_y - span, pad, max_y)
        painter.drawLine(pad, max_y, pad + span, max_y)
        painter.drawLine(max_x - span, pad, max_x, pad)
        painter.drawLine(max_x, pad, max_x, pad + span)

    painter.end()
    return QIcon(pm)


# <– 09.07.2025 –– 269 - 296 –––––––––––––––––––––––––––––––––––––––––––––––
# ─── NEW: helper to detect the file-type (text / image / binary) ───
# put this close to the other helper functions (e.g. below “_icon()”)

import mimetypes                 #  << already from std-lib, no extra dep.
 
def detect_file_format(path: str | os.PathLike) -> str:
    """
    Very small heuristic that distinguishes the **three** classes
    we are interested in for the editor:

        • 'image'    → image/…  (png, jpg, webp …)
        • 'text'     → text/…   (py, md, txt …)
        • 'binary'   → everything else

    Returned keyword is later used inside `_open_file()`
    to decide which widget type (QTextEdit vs. QLabel) is created.
    """
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        return "binary"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/"):
        return "text"
    return "binary"


class ChatAttachmentService:
    _MAX_TEXT_LINES = 240
    _MAX_TEXT_CHARS = 12000
    _INLINE_OBJECT_KINDS = {"code", "text", "markdown", "pdf"}
    _SOURCE_HEADER_PREFIX = "[SOURCE]"
    _LANGUAGE_BY_SUFFIX = {
        ".bat": "bat",
        ".c": "c",
        ".cpp": "cpp",
        ".css": "css",
        ".go": "go",
        ".h": "c",
        ".hpp": "cpp",
        ".html": "html",
        ".htm": "html",
        ".java": "java",
        ".js": "javascript",
        ".json": "json",
        ".jsx": "jsx",
        ".md": "markdown",
        ".markdown": "markdown",
        ".php": "php",
        ".ps1": "powershell",
        ".py": "python",
        ".rb": "ruby",
        ".rs": "rust",
        ".scss": "scss",
        ".sh": "bash",
        ".sql": "sql",
        ".toml": "toml",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".txt": "text",
        ".xml": "xml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".zsh": "bash",
    }

    def normalize_object_paths(self, paths: list[str] | None) -> list[str]:
        normalized_paths: list[str] = []
        seen_paths: set[str] = set()
        for raw_path in paths or []:
            candidate_path = str(raw_path or "").strip()
            if not candidate_path:
                continue
            try:
                resolved_path = str(Path(candidate_path).expanduser().resolve())
            except Exception:
                resolved_path = os.path.abspath(os.path.expanduser(candidate_path))
            if resolved_path in seen_paths or not os.path.exists(resolved_path):
                continue
            seen_paths.add(resolved_path)
            normalized_paths.append(resolved_path)
        return normalized_paths

    def classify_object(self, file_path: str | Path) -> str:
        path = Path(file_path)
        classified_kind = ""
        if callable(_fv_classify):
            try:
                classified_kind = str(_fv_classify(path) or "").strip().lower()
            except Exception:
                classified_kind = ""

        if classified_kind in {"code", "text", "markdown"} and not self._looks_like_text(path):
            classified_kind = "unknown"

        if classified_kind:
            return classified_kind

        if path.suffix.lower() == ".pdf":
            return "pdf"

        detected_kind = detect_file_format(path)
        if detected_kind == "image":
            return "image"

        suffix = path.suffix.lower()
        if suffix in {".md", ".markdown"}:
            return "markdown"
        if suffix in self._LANGUAGE_BY_SUFFIX:
            return "code"
        if detected_kind == "text" or self._looks_like_text(path):
            return "text"
        return "unknown"

    def load_image_object_paths(self, file_paths: list[str] | None) -> list[str]:
        return [
            file_path
            for file_path in self.normalize_object_paths(file_paths)
            if self.classify_object(file_path) == "image"
        ]

    def build_status_message(self, file_paths: list[str] | None) -> str:
        normalized_paths = self.normalize_object_paths(file_paths)
        if not normalized_paths:
            return ""
        attachment_labels = [
            f"{Path(file_path).name} ({self.classify_object(file_path)})"
            for file_path in normalized_paths
        ]
        prefix = "Attachment ready" if len(attachment_labels) == 1 else "Attachments ready"
        return f"{prefix}: {', '.join(attachment_labels)}"

    def build_prompt_payload(self, *, prompt_text: str, file_paths: list[str] | None) -> tuple[str, list[str]]:
        normalized_prompt = str(prompt_text or "").strip()
        normalized_paths = self.normalize_object_paths(file_paths)
        image_paths: list[str] = []
        attachment_lines: list[str] = []
        object_blocks: list[str] = []

        for file_path in normalized_paths:
            path = Path(file_path)
            object_kind = self.classify_object(path)
            if object_kind == "image":
                image_paths.append(file_path)
                attachment_lines.append(f"- {path.name} (image)")
                continue

            if object_kind in self._INLINE_OBJECT_KINDS:
                object_block = self._build_object_block(file_path=file_path, object_kind=object_kind)
                if object_block:
                    object_blocks.append(object_block)
                    attachment_lines.append(f"- {path.name} ({object_kind}, loaded)")
                else:
                    attachment_lines.append(f"- {path.name} ({object_kind}, unreadable)")
                continue

            attachment_lines.append(f"- {path.name} ({object_kind})")

        prompt_parts: list[str] = []
        if normalized_prompt:
            prompt_parts.append(normalized_prompt)
        if attachment_lines:
            prompt_parts.append("Attached files:\n" + "\n".join(attachment_lines))
        if object_blocks:
            prompt_parts.append("\n\n".join(object_blocks))

        return "\n\n".join(part for part in prompt_parts if part).strip(), image_paths

    def _looks_like_text(self, path: Path) -> bool:
        try:
            with open(path, "rb") as handle:
                sample = handle.read(2048)
        except OSError:
            return False

        if not sample:
            return True
        if b"\x00" in sample:
            return False

        printable_bytes = sum(byte >= 32 or byte in (9, 10, 13) for byte in sample)
        return printable_bytes / max(len(sample), 1) >= 0.9

    def load_object_text(self, *, file_path: str | Path, object_kind: str) -> str:
        path = Path(file_path)
        if object_kind == "pdf":
            return self._load_pdf_text(path)
        return path.read_text(encoding="utf-8", errors="replace")

    def _load_pdf_text(self, path: Path) -> str:
        read_document = None
        try:
            if __package__:
                from .agents_tools import read_document  # type: ignore
            else:
                from alde.agents_tools import read_document  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from alde.agents_tools import read_document  # type: ignore
            else:
                raise

        extracted_text = str(read_document(str(path)) or "").strip()
        if extracted_text.startswith("Error"):
            return ""
        return extracted_text

    def _trim_object_text(self, text: str) -> tuple[str, bool]:
        trimmed_lines = str(text or "").splitlines()
        was_trimmed = False
        if len(trimmed_lines) > self._MAX_TEXT_LINES:
            trimmed_lines = trimmed_lines[: self._MAX_TEXT_LINES]
            was_trimmed = True

        trimmed_text = "\n".join(trimmed_lines)
        if len(trimmed_text) > self._MAX_TEXT_CHARS:
            trimmed_text = trimmed_text[: self._MAX_TEXT_CHARS].rstrip()
            was_trimmed = True

        return trimmed_text, was_trimmed

    def _load_code_language(self, *, path: Path, object_kind: str) -> str:
        if object_kind == "markdown":
            return "markdown"
        if object_kind in {"text", "pdf"}:
            return "text"
        return self._LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "")

    def _build_object_block(self, *, file_path: str, object_kind: str) -> str | None:
        path = Path(file_path)
        try:
            raw_text = self.load_object_text(file_path=path, object_kind=object_kind)
        except OSError:
            return None

        normalized_text = str(raw_text or "").strip("\n")
        if not normalized_text:
            normalized_text = "[empty file]"

        trimmed_text, was_trimmed = self._trim_object_text(normalized_text)
        header = f"[FILE] {path.name} ({object_kind})"
        if was_trimmed:
            header += " [truncated]"

        code_language = self._load_code_language(path=path, object_kind=object_kind)
        fence = f"```{code_language}" if code_language else "```"
        source_header = f"{self._SOURCE_HEADER_PREFIX} {path}"
        return f"{header}\n{source_header}\n{fence}\n{trimmed_text}\n```"


CHAT_ATTACHMENT_SERVICE = ChatAttachmentService()


@dataclass(frozen=True)
class ChatSegment:
    kind: str
    language: str
    block: str
    file_path: str = ""


@dataclass(frozen=True)
class ChatFileContext:
    header_line: str
    language: str
    file_path: str = ""
    body_start_index: int = 1

# ────────────────────────────────────────────────────────────────────────────
#  FIX: Tooltip-Schrift ist unsichtbar                                    (NEW)
#       Ursache: Qt 6 greift bei ToolTips nicht nur auf ToolTipText,
#       sondern – je nach Plattform-Style – auch auf WindowText / Text zu.
#       Wir setzen daher ALLE drei Rollen konsequent auf Weiß.
# ────────────────────────────────────────────────────────────────────────────

# -----------------------------------------------------------------
#  Beim Programmstart aktivieren  (einmal nach QApplication anrufen)
# -----------------------------------------------------------------


# <– changes 10.07.2025
# ───────────────────── 1. ToolButton – neue Version ──────────────────────

class ToolButton(QPushButton):
    """


UNIFIED_TOOLS: list[ToolSpec] = _build_unified_tools()
    con-Button für die Corner-Leiste.
    Eigenes objectName (#cornerBtn) => Stylesheet hat höhere Priorität
    als die globale 'QPushButton:hover'-Regel.
    """
    _ICON_SIZE = 21

    def __init__(self, svg: str, tip: str = "", slot=None, parent=None):
        super().__init__(parent)

        self.setObjectName("cornerBtn")                 # <<< wichtig
        self.setIcon(_icon(svg))
        self.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        if tip:
            self.setToolTip(tip)
        if slot:
            self.clicked.connect(slot)

        # lokales Stylesheet überschreibt die globale Hover-Regel
        self.setStyleSheet("""
            QPushButton#cornerBtn {
                background: transparent;
                border: none;
                padding: 0px;
                
            }
            QPushButton#cornerBtn:hover {
                background: rgba(255,255,255,30);  /* alter Hover-Look  */
                border: none;                      /* entfernt col1-Rahmen */
            }
            QPushButton#cornerBtn:pressed,
            QPushButton#cornerBtn:checked {
                background: rgba(255,255,255,30);
                border: none;
            }
        """)

class NoTabScrollerStyle(QProxyStyle):

# <– changes 11.07.2025

    """
    Gibt für Pixel-Metriken der Scroll-Buttons den Wert 0 zurück.
    Dadurch legt Qt keine sichtbaren/anklickbaren Pfeil-Buttons an.
    Funktioniert in Qt-5 und Qt-6.
    """

    _METRICS: set[int] = set()

    # Gewünschte Metriken – einige gibt es nur in Qt-5, andere nur in Qt-6

    for name in (
        "PM_TabBarScrollButtonWidth",       # Qt-5
        "PM_TabBarScrollButtonHeight",      # Qt-5
        "PM_TabBarScrollButtonOverlap",     # Qt-5 + Qt-6
        "PM_TabBarScrollerWidth",           # Qt-6
    ):
        value = getattr(QStyle, name, None)
        if value is not None:           # nur wenn in dieser Qt-Version vorhanden
            _METRICS.add(value)
# <– changes 12.07.2025 (leagacy,removed) –––––––––––––––––––––––––––––––––
# ───────────────────────────────────── EditorTabs ────────────────────────
"""QTabWidget mit
        • versteckten Scroll-Buttons
        • Corner-Widget (+,×,dock)
        • *festem* Abstand (30 px) zwischen letztem Tab und Corner-Widget"""

        
"""erhält der letzte Tab einen rechten Außenabstand von genau 30 px.  
    Damit entsteht der gewünschte feste Abstand zwischen Tab-Leiste
    und dem Corner-Widget – unabhängig von Theme oder DPI-Skalierung."""


# <– changes 13.07.2025 ––––––––––––––––––––––––––––––––––––––––––––––––––––––––

""" 
 PATCH ― keep first tab always visible + insert new tabs right of the current one
================================================================================

The changes are **self-contained** – simply drop the snippet anywhere _below_ the
current imports (for example just after the existing `NoTabScrollerStyle`
class).  No other lines of the original file have to be touched.
"""
"""
# ── NEW ────────────────────────────────────────────────────────────────────────
#  FixedLeftTabBar  –  custom QTabBar that
#    • blocks wheel-scrolling further to the left once the first tab is flush
#      with the left border  (thus the very first tab is _always visible_)
#    • offers a helper to insert a tab right of the currently focused one
#      (used by our EditorTabs wrapper further below)
# ───────────────────────────────────────────────────────────────────────────────
"""

from PySide6.QtWidgets import QTabBar
from PySide6.QtCore    import QPoint
from PySide6.QtGui     import QWheelEvent



class FixedLeftTabBar(QTabBar):   # v23
    """
    #  <– changes - 14.07.2025

    Custom tab-bar that prevents the content from being scrolled further to the
    right than necessary – hence the first tab can **never disappear**.

    – wheelEvent()       blocks excessive wheel / touch scrolling
    – mouseMoveEvent()   is tapped to correct the scroll-offset *during* a
                         drag-operation
    – tabMoved() signal  guarantees the correct offset *after* the re-order
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setMovable(True)                       # tabs can be grabbed
        self.tabMoved.connect(self._ensure_first_visible)

    # ---------------------------------------------------------------- wheelEvent
    def wheelEvent(self, ev: QWheelEvent) -> None:
        if self.count() <= 0:
            return super().wheelEvent(ev)
        going_left = ev.angleDelta().y() > 0          # +Δ ⇒ scroll left
        first_visible = self.tabRect(0).left() >= 0

        if going_left and first_visible:              # already flush → block
            ev.ignore()
            return
        super().wheelEvent(ev)

    # ---------------------------------------------------------------- mouseMoveEvent
    # (gets called continuously while a tab is being dragged)
    def mouseMoveEvent(self, ev) -> None:             # noqa: D401  (Qt signature)
        super().mouseMoveEvent(ev)
        self._ensure_first_visible()                  # adjust on-the-fly

    # ---------------------------------------------------------------- helper
    def _ensure_first_visible(self) -> None:
        """
        If the left border of tab #0 is outside the visible area
        (x < 0) we pull the whole bar back so that x == 0.
        """
        if self.count() <= 0:
            return
        left_px = self.tabRect(0).left()              # may be negative
        if left_px >= 0:
            return                                    # already fine

        # scrollOffset() / setScrollOffset() are protected in C++
        # → directly available inside our subclass.
        new_off = max(0, self.scrollOffset() + left_px)
        if new_off != self.scrollOffset():
            self.setScrollOffset(new_off)


"""
# <- changes 14.07.2025

What changed / why it fixes the second half of the ticket
----------------------------------------------------------

1. `mouseMoveEvent()` is now re-implemented.  
   While the user drags a tab, Qt may auto-scroll the bar; every movement is
   followed by `_ensure_first_visible()` which instantly corrects the offset
   if the first tab slipped out of view.

2. The built-in `tabMoved(int, int)` signal is connected to the same helper.
   Even after the drag finished, we make one last check and – if required –
   nudge the bar back into the allowed range.

3. `_ensure_first_visible()` uses the protected
   `scrollOffset()` / `setScrollOffset()` API that Qt provides exactly for
   such custom scroll handling.  
   Calculation:  
     • `tabRect(0).left()`  → negative pixels that the first tab is hidden  
     • add that amount to the current offset (clamped ≥ 0)

The wheel / swipe logic from the earlier patch remains untouched; together
both parts guarantee that *no interaction* can ever hide the left-most tab.
"""


class EditorTabs(QTabWidget):
    """
    QTabWidget that

      • hides the built-in scroll buttons (handled by NoTabScrollerStyle)
      • guarantees that the *left-most* tab always remains visible
      • inserts newly created tabs directly **right of the active tab**
    """

    _PADDING_AFTER_LAST_TAB = 0          # fixed gap before the corner widget

    def __init__(self, parent: QTabWidget | None = None) -> None:
        super().__init__(parent)

        # Crash-isolation helper: use a minimal, vanilla tab widget.
        if _env_truthy("AI_IDE_SIMPLE_TABS", "0"):
            editor = QTextEdit("# notes.py", tabChangesFocus=True)
            self.addTab(editor, "notes.py")
            return

        # --- supply our customised tab-bar before doing anything else -------
        enable_custom_tabbar = _env_truthy("AI_IDE_TABS_ENABLE_CUSTOM_TABBAR", "0")
        disable_custom_tabbar = _env_truthy("AI_IDE_TABS_DISABLE_CUSTOM_TABBAR", "0") or (not enable_custom_tabbar)
        if not disable_custom_tabbar:
            self.setTabBar(FixedLeftTabBar())             # <── ① custom bar
            self.tabBar().setUsesScrollButtons(False)
            self.tabBar().setStyle(
                NoTabScrollerStyle(self.tabBar().style())
            )  # hide arrow buttons
        else:
            # Keep UI close to the intended design without using the custom
            # tab-bar code path that can segfault on some setups.
            self.tabBar().setUsesScrollButtons(False)
        self.setMovable(True)
        self.setDocumentMode(False)
    
        self.setTabsClosable(False)                    # we close via corner btn

        # --- corner widget ( +   ×   ◀ ) ------------------------------------
        corner = QWidget(self)
        lay = QHBoxLayout(corner)
        lay.setContentsMargins(20, 0, 4, 0)
        lay.setSpacing(0)

        self._btn_add   = ToolButton("plus.svg",        "Neuer Tab",
                                     slot=self._new_tab)
        self._btn_close = ToolButton("close_tab.svg",   "Tab schließen",
                                     slot=self._close_tab)
        self._btn_dock  = ToolButton("left_panel_close.svg",
                                     "Alle Tabs schließen",
                                     slot=self._close_all_tabs)

        for b in (self._btn_add, self._btn_close, self._btn_dock):
            lay.addWidget(b)

        self.setCornerWidget(corner, Qt.TopRightCorner)


       # ---- stylesheet to keep the 30 px gap between last tab & corner ----
        self.setStyleSheet(
          f"QTabBar::tab:last {{ margin-right:{self._PADDING_AFTER_LAST_TAB}px; }}")

        # ---- example start-tabs (can be removed at any time) ---------------
        first_editor = QTextEdit("# notes.py", tabChangesFocus=True)
        idx0 = self.addTab(first_editor, "")
        self.setTabText(idx0, "notes.py")
        self._bind_editor(first_editor)

        # Kontextmenü & Aktionen (Öffnen / Speichern / Speichern unter / Wiederherstellen / Encoding)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # Kontextmenü auch direkt auf der Tab-Leiste anbieten
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._show_context_menu_from_tabbar)

        self._act_open = QAction("Öffnen...", self)
        self._act_open.setShortcut(QKeySequence.Open)
        self._act_open.triggered.connect(self._open_file_dialog)

        self._act_new_code_viewer = QAction("Neuen Code-Viewer-Tab", self)
        self._act_new_code_viewer.setShortcut(QKeySequence("Ctrl+Alt+N"))
        self._act_new_code_viewer.triggered.connect(self._new_code_viewer_tab)

        self._act_open_with_enc = QAction("Öffnen mit Encoding...", self)
        self._act_open_with_enc.triggered.connect(self._open_file_dialog_with_encoding)

        self._act_save = QAction("Speichern", self)
        self._act_save.setShortcut(QKeySequence.Save)
        self._act_save.triggered.connect(self._save_current_tab)

        self._act_save_as = QAction("Speichern unter...", self)
        self._act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self._act_save_as.triggered.connect(self._save_current_tab_as)

        self._act_reopen_closed = QAction("Geschlossenen Tab wiederherstellen", self)
        self._act_reopen_closed.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self._act_reopen_closed.triggered.connect(self._reopen_closed_tab)

        self._act_set_encoding = QAction("Encoding setzen...", self)
        self._act_set_encoding.triggered.connect(self._set_current_tab_encoding)

        for a in (
            self._act_new_code_viewer,
            self._act_open,
            self._act_open_with_enc,
            self._act_save,
            self._act_save_as,
            self._act_reopen_closed,
            self._act_set_encoding,
        ):
            self.addAction(a)

        # State for optional features
        self._default_encoding = "utf-8"
        self._closed_tabs_stack: list[tuple[str, str, str, str]] = []  # (title, content, file_path, encoding)
        self._recent_files: list[str] = []
        self._recent_max = 10
        self._load_recent_files()

    # ─────────────────────────── slots ──────────────────────────────────────

    @Slot()
    def _new_tab(self) -> None:
        """
        Create a fresh untitled editor **right of the tab that currently has
        the focus** instead of always appending it at the very end.
        """
        current = self.currentIndex()
        if current < 0:                                   # no tab open
            current = self.count() - 1

        index = self.insertTab(current + 1,
                               QTextEdit("# new file …"),
                               f"untitled_{self.count() + 1}.py")
        self.widget(index).setProperty("file_path", "")
        self._bind_editor(self.widget(index))
        # Highlighter anwenden (Standard-Dateiname endet auf .py → Python)
        self._apply_highlighter(self.widget(index), f"untitled_{self.count()}.py")
        self.setCurrentIndex(index)

    @Slot()
    def _new_code_viewer_tab(self) -> None:
        """Create a new editable CodeViewer tab right of the active tab."""
        current = self.currentIndex()
        if current < 0:
            current = self.count() - 1

        title = f"code_viewer_{self.count() + 1}.py"
        viewer = CodeViewer(
            "",
            language="python",
            editable=True,
            auto_fit=False,
        )
        viewer.setProperty("file_path", "")
        viewer.setProperty("file_encoding", str(getattr(self, "_default_encoding", "utf-8")))
        viewer.document().setModified(False)
        self._bind_editor(viewer)

        index = self.insertTab(current + 1, viewer, title)
        self.setCurrentIndex(index)

    @Slot()
    def _close_tab(self) -> None:
        """
        Schliesst den aktuell aktiven Tab dieser EditorTabs-Instanz.

        – Existiert kein Tab, passiert nichts  
        – Nach dem Entfernen wird automatisch der linke Nachbar aktiviert
        """
        idx = self.currentIndex()
        if idx < 0:
            return
        w = self.widget(idx)
        # snapshot for reopen (before possibly saving)
        self._snapshot_current_tab()
        if isinstance(w, (QPlainTextEdit, QTextEdit)) and w.document().isModified():
            choice = QMessageBox.question(
                self,
                "Ungespeicherte Änderungen",
                "Dieser Tab hat ungespeicherte Änderungen. Jetzt speichern?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if choice == QMessageBox.StandardButton.Save:
                self._save_current_tab()
                if w.document().isModified():
                    return
            elif choice == QMessageBox.StandardButton.Cancel:
                return
        self.removeTab(idx)
        # Seite explizit zerstören, um Artefakte zu vermeiden
        try:
            if w is not None:
                w.deleteLater()
        except Exception:
            pass
        # Wenn keine Tabs mehr vorhanden sind, das umschließende Dock schließen
        if self.count() == 0:
            dock = self._parent_dock()
            if dock is not None:
                dock.close()

    @Slot()
    def _save_current_tab(self) -> None:
        """Speichert den aktuellen Tab dieser EditorTabs-Instanz."""
        idx = self.currentIndex()
        if idx < 0:
            return
        widget = self.widget(idx)
        if not isinstance(widget, (QPlainTextEdit, QTextEdit)):
            QMessageBox.information(self, "Info", "Dieser Tab kann nicht gespeichert werden.")
            return
        path = widget.property("file_path") or ""
        if not path:
            fname, _ = QFileDialog.getSaveFileName(
                self,
                "Datei speichern",
                str(Path.home()),
                "Textdateien (*.txt *.md *.py);;Alle Dateien (*)",
            )
            if not fname:
                return
            path = fname
            widget.setProperty("file_path", path)
            self.setTabText(idx, Path(path).name)
        try:
            enc = widget.property("file_encoding") or "utf-8"
            Path(path).write_text(widget.toPlainText(), encoding=str(enc))
        except Exception as exc:
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        if isinstance(widget, (QPlainTextEdit, QTextEdit)):
            widget.document().setModified(False)
        self._update_tab_title_for_idx(idx)
        # Statusbar-Nachricht über MainWindow
        main_window = self.window()
        if hasattr(main_window, 'statusBar'):
            main_window.statusBar().showMessage(f"{path} gespeichert", 3000)

    @Slot()
    def _save_current_tab_as(self) -> None:
        """Speichert den aktuellen Tab immer unter neuem Namen (Speichern unter)."""
        idx = self.currentIndex()
        if idx < 0:
            return
        widget = self.widget(idx)
        if not isinstance(widget, (QPlainTextEdit, QTextEdit)):
            QMessageBox.information(self, "Info", "Dieser Tab kann nicht gespeichert werden.")
            return
        fname, _ = QFileDialog.getSaveFileName(
            self,
            "Datei speichern unter",
            str(Path.home()),
            "Textdateien (*.txt *.md *.py);;Alle Dateien (*)",
        )
        if not fname:
            return
        try:
            enc = widget.property("file_encoding") or "utf-8"
            Path(fname).write_text(widget.toPlainText(), encoding=str(enc))
        except Exception as exc:
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        widget.setProperty("file_path", fname)
        if isinstance(widget, (QPlainTextEdit, QTextEdit)):
            widget.document().setModified(False)
        self._update_tab_title_for_idx(idx)
        main_window = self.window()
        if hasattr(main_window, 'statusBar'):
            main_window.statusBar().showMessage(f"{fname} gespeichert", 3000)

    def _show_context_menu(self, pos: QPoint) -> None:  # noqa: D401
        """Zeigt das allgemeine Kontextmenü (Speichern / Speichern unter)."""
        menu = QMenu(self)
        menu.addAction(self._act_new_code_viewer)
        menu.addSeparator()
        recent_menu = self._build_recent_menu()
        if recent_menu is not None:
            menu.addMenu(recent_menu)
        menu.addAction(self._act_open)
        menu.addAction(self._act_open_with_enc)
        menu.addAction(self._act_save)
        menu.addAction(self._act_save_as)
        menu.addSeparator()
        menu.addAction(self._act_reopen_closed)
        menu.addAction(self._act_set_encoding)
        menu.exec(self.mapToGlobal(pos))

    def _show_context_menu_from_tabbar(self, pos: QPoint) -> None:
        """Kontextmenü, wenn auf der Tab-Leiste rechts geklickt wurde."""
        menu = QMenu(self)
        menu.addAction(self._act_new_code_viewer)
        menu.addSeparator()
        recent_menu = self._build_recent_menu()
        if recent_menu is not None:
            menu.addMenu(recent_menu)
        menu.addAction(self._act_open)
        menu.addAction(self._act_open_with_enc)
        menu.addAction(self._act_save)
        menu.addAction(self._act_save_as)
        menu.addSeparator()
        menu.addAction(self._act_reopen_closed)
        menu.addAction(self._act_set_encoding)
        menu.exec(self.tabBar().mapToGlobal(pos))

    # --------------------- Datei-Öffnen + Dirty-Indicator ------------------
    @Slot()
    def _open_file_dialog(self) -> None:
        fname, _ = QFileDialog.getOpenFileName(
            self,
            "Datei öffnen",
            str(Path.home()),
            "Textdateien (*.txt *.md *.py);;Alle Dateien (*)",
        )
        if not fname:
            return
        text, enc = self._read_with_fallbacks(fname)
        if text is None:
            return
        current = self.currentIndex()
        if current < 0:
            current = self.count() - 1
        editor = QTextEdit()
        editor.setPlainText(text)
        editor.setProperty("file_path", fname)
        editor.setProperty("file_encoding", enc)
        editor.document().setModified(False)
        self._bind_editor(editor)
        idx = self.insertTab(current + 1, editor, Path(fname).name)
        # Syntax-Highlighter anwenden
        self._apply_highlighter(editor, fname)
        self.setCurrentIndex(idx)
        self._add_recent_file(fname)

    @Slot()
    def _open_file_dialog_with_encoding(self) -> None:
        fname, _ = QFileDialog.getOpenFileName(
            self,
            "Datei öffnen",
            str(Path.home()),
            "Alle Dateien (*)",
        )
        if not fname:
            return
        enc = self._prompt_encoding()
        if not enc:
            return
        try:
            text = Path(fname).read_text(encoding=enc)
        except Exception as exc:
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        self._open_from_text(fname, text, enc)
        self._add_recent_file(fname)

    def _bind_editor(self, widget: QTextEdit | QPlainTextEdit) -> None:
        doc = widget.document()
        doc.modificationChanged.connect(lambda _m, w=widget: self._on_doc_modified(w))
        # Beim ersten Binden direkt versuchen einen passenden Highlighter
        # zu setzen (Dateipfad kann bei neuen Tabs leer sein).
        path = widget.property("file_path") or ""
        self._apply_highlighter(widget, str(path) or None)

    # --------------------- Highlighter / Klassifizierung -----------------
    def _classify_for_highlighter(self, path: str | None) -> str:
        """Einfache Klassifizierung anhand der Dateiendung.

        Gibt einen Typ zurück, der zur Wahl eines Syntax-Highlighters genutzt
        werden kann. Fällt auf "text" zurück, wenn nichts erkannt wird.
        """
        if not path:
            return "text"
        ext = Path(path).suffix.lower()
        mapping = {
            ".py": "python",
            ".md": "markdown",
            ".json": "json",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml": "yaml",
        }
        return mapping.get(ext, "text")

    def _apply_highlighter(self, editor: QTextEdit | QPlainTextEdit, path: str | None) -> None:
        """Wendet – falls verfügbar – einen passenden Highlighter an.
    
        Unterstützt derzeit: Python, Markdown, JSON. Idempotent: ersetzt nur,
        wenn sich der benötigte Highlighter-Typ unterscheidet.
        """
        kind = self._classify_for_highlighter(path)
        cls = None
        if kind == "python":
            cls = QSHighlighter
        elif kind == "markdown":
            cls = MDHighlighter
        elif kind == "json":
            cls = JSONHighlighter
        elif kind == "toml":
            cls = TOMLHighlighter
        elif kind == "yaml":
            cls = YAMLHighlighter

        if cls is None:
            return

        try:
            existing = editor.property("_highlighter")
            if existing is not None and isinstance(existing, cls):
                return
            hl = cls(editor.document())
            editor.setProperty("_highlighter", hl)
        except Exception:
            pass

    def _on_doc_modified(self, widget: QTextEdit | QPlainTextEdit) -> None:
        idx = self.indexOf(widget)
        if idx != -1:
            self._update_tab_title_for_idx(idx)

    def _update_tab_title_for_idx(self, idx: int) -> None:
        w = self.widget(idx)
        base = None
        if isinstance(w, (QPlainTextEdit, QTextEdit)):
            fp = w.property("file_path") or ""
            if fp:
                base = Path(str(fp)).name
        if not base:
            base = self.tabText(idx).lstrip("*") or f"untitled_{idx+1}.py"
        # add encoding suffix
        enc = None
        if isinstance(w, (QPlainTextEdit, QTextEdit)):
            enc = w.property("file_encoding") or self._default_encoding
        suffix = f" [{str(enc).upper()}]" if enc else ""
        title = f"{base}{suffix}"
        if isinstance(w, (QPlainTextEdit, QTextEdit)) and w.document().isModified():
            self.setTabText(idx, f"*{title}")
        else:
            self.setTabText(idx, title)

    # --------------------- Encoding helpers -------------------------------
    def _prompt_encoding(self) -> str | None:
        options = ["utf-8", "latin-1", "cp1252", "utf-16", "utf-8-sig"]
        enc, ok = QInputDialog.getItem(self, "Encoding wählen", "Encoding:", options, 0, False)
        return enc if ok else None

    def _read_with_fallbacks(self, path: str) -> tuple[str | None, str]:
        # Try editor default, then latin-1 as safe fallback
        for enc in (self._default_encoding, "utf-8", "utf-8-sig", "latin-1"):
            try:
                return Path(path).read_text(encoding=enc), enc
            except Exception:
                continue
        QMessageBox.critical(self, "Fehler", f"Konnte Datei nicht lesen: {path}")
        return None, self._default_encoding

    def _open_from_text(self, path: str, text: str, enc: str) -> None:
        current = self.currentIndex()
        if current < 0:
            current = self.count() - 1
        editor = QTextEdit()
        editor.setPlainText(text)
        editor.setProperty("file_path", path)
        editor.setProperty("file_encoding", enc)
        editor.document().setModified(False)
        self._bind_editor(editor)
        idx = self.insertTab(current + 1, editor, Path(path).name)
        self._apply_highlighter(editor, path)
        self.setCurrentIndex(idx)

    @Slot()
    def _set_current_tab_encoding(self) -> None:
        idx = self.currentIndex()
        if idx < 0:
            return
        w = self.widget(idx)
        if not isinstance(w, (QPlainTextEdit, QTextEdit)):
            return
        enc = self._prompt_encoding()
        if not enc:
            return
        w.setProperty("file_encoding", enc)
        # Optional: nothing else changes until save/open

    # --------------------- Recent files -----------------------------------
    def _add_recent_file(self, path: str) -> None:
        path = str(Path(path))
        if path in self._recent_files:
            self._recent_files.remove(path)
        self._recent_files.insert(0, path)
        if len(self._recent_files) > self._recent_max:
            self._recent_files = self._recent_files[: self._recent_max]
        self._save_recent_files()

    def _build_recent_menu(self):
        if not self._recent_files:
            return None
        m = QMenu("Zuletzt geöffnet", self)
        for p in self._recent_files:
            act = QAction(str(Path(p).name), self)
            act.setToolTip(p)
            act.triggered.connect(lambda _=False, path=p: self._open_recent(path))
            m.addAction(act)
        return m

    def _open_recent(self, path: str) -> None:
        text, enc = self._read_with_fallbacks(path)
        if text is None:
            return
        self._open_from_text(path, text, enc)

    def _load_recent_files(self) -> None:
        try:
            s = QSettings()
            arr = s.value("EditorTabs/RecentFiles", [])
            if isinstance(arr, list):
                self._recent_files = [str(x) for x in arr]
        except Exception:
            self._recent_files = []

    def _save_recent_files(self) -> None:
        try:
            s = QSettings()
            s.setValue("EditorTabs/RecentFiles", self._recent_files)
        except Exception:
            pass

    # --------------------- Reopen closed tab ------------------------------
    def _snapshot_current_tab(self) -> None:
        idx = self.currentIndex()
        if idx < 0:
            return
        w = self.widget(idx)
        if isinstance(w, (QPlainTextEdit, QTextEdit)):
            title = self.tabText(idx).lstrip("*")
            content = w.toPlainText()
            path = w.property("file_path") or ""
            enc = w.property("file_encoding") or self._default_encoding
            self._closed_tabs_stack.append((title, content, str(path), str(enc)))

    @Slot()
    def _reopen_closed_tab(self) -> None:
        if not self._closed_tabs_stack:
            return
        title, content, path, enc = self._closed_tabs_stack.pop()
        editor = QTextEdit()
        editor.setPlainText(content)
        if path:
            editor.setProperty("file_path", path)
        editor.setProperty("file_encoding", enc)
        editor.document().setModified(False)
        self._bind_editor(editor)
        idx = self.insertTab(self.currentIndex() + 1, editor, title or "wiederhergestellt")
        self.setCurrentIndex(idx)

    @Slot()
    def _close_all_tabs(self) -> None:
        """Schließt alle Tabs in diesem TabWidget."""
        # wiederhole das Schließen mit Guard; Abbruch bei Cancel
        while self.count() > 0:
            self.setCurrentIndex(0)
            before = self.count()
            self._close_tab()
            if self.count() == before:
                # abgebrochen
                break
        # Falls nach dem Vorgang keine Tabs mehr vorhanden sind: Dock schließen
        if self.count() == 0:
            dock = self._parent_dock()
            if dock is not None:
                dock.close()
    
    @Slot()
    def _close_dock(self) -> None:
        """Schließt das gesamte Dock-Widget."""
        dock = self._parent_dock()
        if dock:
            dock.close()

    # ---------------------------- helpers -----------------------------------

    def _parent_dock(self) -> QDockWidget | None:
        w = self.parentWidget()
        while w and not isinstance(w, QDockWidget):
            w = w.parentWidget()
        return w


    """
    What is fixed / how to test
        ---------------------------

        1. Run the application and open enough documents to exceed the tab-bar width.  
        • Scroll right with the mouse wheel → tabs move.  
        • Scroll left → the movement stops precisely when the first tab touches the
            left margin; it never disappears again.

        2. Activate an arbitrary tab and press the **“+”** button (or `Ctrl+N` if you
        already mapped it).  
        • The brand-new “untitled_…” tab now appears directly to the _right_ of the
            one that had the focus, not at the very end of the list.

        Both requirements from the user story are therefore fulfilled while keeping the
        original look-&-feel and without introducing any new dependencies."""

# ═══════════════════════  drag-and-drop QTextEdit  ════════════════════════

class FileDropTextEdit(QTextEdit):
    filesDropped = Signal(list)
    submitRequested = Signal()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------
    def dragEnterEvent(self, ev: QDragEnterEvent):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dropEvent(self, ev: QDropEvent):
        if ev.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in ev.mimeData().urls()]
            self.filesDropped.emit(paths)
            ev.acceptProposedAction()
        else:
            super().dropEvent(ev)

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and bool(ev.modifiers() & Qt.ControlModifier):
            self.submitRequested.emit()
            ev.accept()
            return
        super().keyPressEvent(ev)

# ═══════ << changes 09.11.2025
'''DROP-IN PATCH – 1 px linke Rahmenlinie am Chat-Dock  
===================================================  
Die Änderung betrifft ausschließlich die `ChatDock`-Klasse.  
            _sys:bool = None) -> None:

        """
        Logging message and response to context cache.
        Parameter format: List[Tuple(role, content, object, data, thread-name, assistant_name, _dev, _sys)]
        """
Ersetzen Sie den bisherigen `setStyleSheet( … )`-Block in `ChatDock.__init__`  
durch den folgenden Code (oder fügen Sie ihn als Patch darunter ein):
'''
# -------------------------------------------------------------------- ChatDock

class ChatDock(QDockWidget):
    """
    • keine Titelzeile / Buttons
    • unsichtbarer, aber benutzbarer Split-Handle
    • NEU: 1 px linke Rahmenlinie als optische Trennung
    """
    def __init__(self, accent: dict, base: dict, parent=None) -> None:
        super().__init__("AI Chat", parent)

        self.setObjectName("ChatDock")                      # wichtig für QSS
        self.setTitleBarWidget(QWidget())                   # Titelzeile ausblenden
        self.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.scheme = _build_scheme(accent, base)                # Farbschema mergen
        self._apply_dock_style()

        # ---- eigentlicher Inhalt ------------------------------------------
        self.setWidget(AIWidget(accent, base))

    def _apply_dock_style(self) -> None:
        self.setStyleSheet(f"""
            /* feste 1-px-Linie links */
            QDockWidget#ChatDock {{
                border : 1px solid {self.scheme['col5']};
            }}

            /* Split-Handle: unsichtbar aber greifbar */
            QDockWidget::separator {{
                background : {self.scheme['col5']};
                width      : 4px;
            }}
            QDockWidget::separator:hover {{
                background : {self.scheme['col2']};
            }}
        """)

    def update_scheme(self, accent: dict[str, str], base: dict[str, str]) -> None:
        self.scheme = _build_scheme(accent, base)
        self._apply_dock_style()
        chat_widget = self.widget()
        updater = getattr(chat_widget, "update_scheme", None)
        if callable(updater):
            updater(accent, base)

# ═══════════════════════  AI chat dock  ═══════════════════════════════════

class AIWidget(QWidget):
    '''AI-Chat-Dock – fehlerbereinigte Version'''

    _PROMPT_SNAP_HEIGHT = 80
    _PROMPT_MAX_HEIGHT = 260
    _PROMPT_AUTOFIT_PADDING = 12
    _PROMPT_COMPOSER_V_MARGIN = 10
    _PROMPT_COMPOSER_H_MARGIN = 12
    _PROMPT_COMPOSER_SPACING = 8
    _PROMPT_SEND_BUTTON_SIZE = 34
    _PROMPT_PLACEHOLDER_TEXT = "Prompt"
    _async_result_ready = Signal(object)

    def __init__(self,
        accent, 
        base, 
        parent=None):    

        super().__init__(parent,)

        self.api_key: str = self._read_api_key()
        self._api_key_missing: bool = not bool(self.api_key)
        self._model:   str = "o3-2025-04-16"                 # <<< zentrales Modell
        self._dropped_files: List[str] = []
        self._runtime_context_entries: list[dict[str, str]] = []
        self.scheme = _build_scheme(accent, base)                # Farbschema mergen
        self._build_ui()
        self._apply_scheme_styles()
        self._wire()
        self._async_result_ready.connect(self._handle_async_result)

        if self._api_key_missing:
            try:
                for btn in (getattr(self, "btn_send", None),):
                    if btn is not None:
                        btn.setEnabled(False)
            except Exception:
                pass
            try:
                self._append("System", "OPENAI_API_KEY not found. Set it in your environment or a .env file to enable chat.")
            except Exception:
                pass
        
        self.setAttribute(QtCore.Qt.WA_Hover, True)

    def update_scheme(self, accent: dict[str, str], base: dict[str, str]) -> None:
        self.scheme = _build_scheme(accent, base)
        self._apply_scheme_styles()
        if hasattr(self, "chat_view") and isinstance(self.chat_view, ChatWindow):
            self.chat_view.update_scheme(self.scheme)

    def _apply_scheme_styles(self) -> None:
        col6 = self.scheme.get("col6", "#E3E3DED6")
        col9 = self.scheme.get("col9", "#101010")
        col10 = self.scheme.get("col10", "#1f1f1f")
        menu_sel = self.scheme.get("menu_sel", self.scheme.get("col12", "rgba(88,237,91,48)"))
        self.setStyleSheet(
            f"""
            AIWidget {{
                background: {self.scheme.get('col7', '#0b0b0b')};
                color: {col6};
            }}
            QScrollBar:vertical {{
                background: {col9};
                width: 4px;
            }}
            QScrollBar::add-line,
            QScrollBar::sub-line {{
                height: 0px;
            }}
            QScrollBar:hover {{
                background: {col9};
            }}
            QScrollBar::handle:hover {{
                background: {col10};
            }}
            """
        )
        prompt_edit = getattr(self, "prompt_edit", None)
        if isinstance(prompt_edit, QTextEdit):
            prompt_edit.setStyleSheet(
                f"""
                QTextEdit#aiInput {{
                    
                    font-size: 15px;
                    background: transparent;
                    color: {col6};
                    border: none;
                    selection-background-color: {menu_sel};
                }}
                """
            )
        
    # ---------------------------------------------------------------- ENV
    @staticmethod
    def _read_api_key() -> str:
        root_env  = Path(__file__).resolve().parents[1] / ".env"
        local_env = Path(__file__).with_suffix(".env")
        for f in (root_env, local_env):
            if f.exists():
                load_dotenv(f, override=False)
                
        load_dotenv()
        key = (os.getenv("OPENAI_API_KEY") or "").strip()
        return key
    
    def _build_ui(self) -> None:
        """Erstellt die Oberfläche des AI-Docks.

        • oben:   Chat-History  (ChatWindow → zeigt Text + Code farbig)
        • unten:  Prompt-Composer mit Eingabefeld und internem Send-Button
        """
        # 1)  Chat-History (read-only)
        self.chat_view = ChatWindow(self.scheme)

        # 2)  Prompt-Editor  (Drag-&-Drop + Multiline)
        self.prompt_edit = FileDropTextEdit(               # neu: nur EIN Editor
            placeholderText=self._PROMPT_PLACEHOLDER_TEXT,
            objectName="aiInput"       )
        self.prompt_edit.setAttribute(Qt.WA_StyledBackground, True)
        self.prompt_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prompt_edit.setMinimumHeight(self._PROMPT_SNAP_HEIGHT)
        self.prompt_edit.setMaximumHeight(self._PROMPT_MAX_HEIGHT)
        self.prompt_edit.setFixedHeight(self._PROMPT_SNAP_HEIGHT)
        self.prompt_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.prompt_edit.setStyleSheet("QTextEdit#aiInput { font-size: 15px; }")

        self.btn_send = ToolButton("send.svg", "Send", slot=self._send)
        self.btn_send.setObjectName("chatPromptSendButton")
        self.btn_send.setFixedSize(self._PROMPT_SEND_BUTTON_SIZE, self._PROMPT_SEND_BUTTON_SIZE)
        self.btn_send.setIconSize(QSize(ToolButton._ICON_SIZE - 2, ToolButton._ICON_SIZE - 2))
        self.btn_send.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_send.setToolTip("Senden (Strg+Enter)")

        self.prompt_composer = QFrame(self)
        self.prompt_composer.setObjectName("chatPromptComposer")
        self.prompt_composer.setAttribute(Qt.WA_StyledBackground, True)
        composer_layout = QHBoxLayout(self.prompt_composer)
        composer_layout.setContentsMargins(
            self._PROMPT_COMPOSER_H_MARGIN,
            self._PROMPT_COMPOSER_V_MARGIN,
            self._PROMPT_COMPOSER_H_MARGIN,
            self._PROMPT_COMPOSER_V_MARGIN,
        )
        composer_layout.setSpacing(self._PROMPT_COMPOSER_SPACING)
        composer_layout.addWidget(self.prompt_edit, 1)
        composer_layout.addWidget(self.btn_send, 0, Qt.AlignRight | Qt.AlignBottom)

        self.footer_controls = QWidget(self)
        self.footer_controls.setObjectName("chatFooterControls")
        self.footer_controls.setAttribute(Qt.WA_StyledBackground, True)
        self.footer_controls.setMinimumHeight(30)
        self.footer_controls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.footer_controls_layout = QHBoxLayout(self.footer_controls)
        self.footer_controls_layout.setContentsMargins(2, 0, 2, 2)
        self.footer_controls_layout.setSpacing(4)
        self.btn_footer_actions = QToolButton(self.footer_controls)
        self.btn_footer_actions.setObjectName("chatFooterActionsButton")
        footer_action_icon = _icon("automation_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg")
        if not footer_action_icon.isNull():
            self.btn_footer_actions.setIcon(footer_action_icon)
        self.btn_footer_actions.setText("")
        self.btn_footer_actions.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_footer_actions.setPopupMode(QToolButton.InstantPopup)
        self.btn_footer_actions.setCursor(Qt.PointingHandCursor)
        self.btn_footer_actions.setToolTip("Configured actions starten")
        self.btn_footer_actions.setFixedHeight(24)

        self._footer_action_menu = QMenu(self.btn_footer_actions)
        self._footer_action_menu.aboutToShow.connect(self._rebuild_footer_action_menu)
        self.btn_footer_actions.setMenu(self._footer_action_menu)

        self.footer_controls_layout.addWidget(self.btn_footer_actions, 0, Qt.AlignLeft | Qt.AlignVCenter)
        self.footer_controls_layout.addStretch(1)

        # 3) Prompt in ChatWindow integrieren
        self.chat_view.set_prompt_widget(
            self.prompt_composer,
            snap_height=self._prompt_shell_height(self._PROMPT_SNAP_HEIGHT),
        )
        self.chat_view.set_footer_widget(self.footer_controls)
        QTimer.singleShot(0, self._apply_prompt_snap_height)

        # 4) Gesamtlayout
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(self.chat_view, 1)
        # ------------------------------------------------------------------- SIGNALS

        # ---------------------------------------------------------------------------
        #  SIGNAL-VERDRAHTUNG   (nur noch das Prompt-Feld liefert FilesDropped)
        # ---------------------------------------------------------------------------
    def _wire(self) -> None:
        self.prompt_edit.filesDropped.connect(self._remember_files)
        self.prompt_edit.submitRequested.connect(self._send)
        self.prompt_edit.textChanged.connect(self._schedule_prompt_autofit)
        try:
            self.prompt_edit.document().documentLayout().documentSizeChanged.connect(
                lambda _size: self._schedule_prompt_autofit()
            )
        except Exception:
            pass
        self._schedule_prompt_autofit()

    def _load_action_schema_configs(self) -> dict[str, dict[str, Any]]:
        try:
            try:
                from .agents_config import get_action_request_schema_configs  # type: ignore
            except ImportError as e:
                msg = str(e)
                if "no known parent package" in msg or "attempted relative import" in msg:
                    from alde.agents_config import get_action_request_schema_configs  # type: ignore
                else:
                    raise

            raw_configs = get_action_request_schema_configs()
            if not isinstance(raw_configs, dict):
                return {}
            return {
                str(action_object_name).strip(): dict(action_config or {})
                for action_object_name, action_config in raw_configs.items()
                if str(action_object_name).strip() and isinstance(action_config, dict)
            }
        except Exception:
            return {}

    def _load_configured_action_objects(self) -> list[dict[str, Any]]:
        action_objects: list[dict[str, Any]] = []
        for action_object_name, action_config in self._load_action_schema_configs().items():
            action_names = [
                str(value).strip()
                for value in (action_config.get("actions") or [])
                if str(value).strip()
            ]
            action_name = action_names[0] if action_names else str(action_object_name).strip()
            if not action_name:
                continue

            required_paths = [
                str(path_name).strip()
                for path_name in (action_config.get("required_paths") or [])
                if str(path_name).strip()
            ]
            required_non_action_paths = [
                path_name
                for path_name in required_paths
                if path_name.casefold() != "action"
            ]

            resolution_config = action_config.get("request_resolution")
            resolution_objects = (
                resolution_config.get("objects")
                if isinstance(resolution_config, dict)
                else []
            )
            request_fields = [
                str(resolution_object.get("request_field") or "").strip()
                for resolution_object in (resolution_objects or [])
                if isinstance(resolution_object, dict)
                and str(resolution_object.get("request_field") or "").strip()
            ]

            display_name = str(action_object_name or action_name).strip().replace("_", " ")
            display_name = " ".join(display_name.split())
            if display_name:
                display_name = display_name[0].upper() + display_name[1:]

            action_objects.append(
                {
                    "action_object_name": str(action_object_name).strip(),
                    "action_name": action_name,
                    "display_name": display_name or action_name,
                    "description": str(action_config.get("description") or "").strip(),
                    "required_non_action_paths": required_non_action_paths,
                    "request_fields": request_fields,
                }
            )

        action_objects.sort(
            key=lambda action_object: str(
                action_object.get("display_name")
                or action_object.get("action_object_name")
                or ""
            ).casefold()
        )
        return action_objects

    def _set_action_request_path_value(
        self,
        *,
        action_request: dict[str, Any],
        path_name: str,
        path_value: Any,
    ) -> None:
        path_segments = [segment.strip() for segment in str(path_name or "").split(".") if segment.strip()]
        if not path_segments:
            return

        cursor = action_request
        for segment in path_segments[:-1]:
            nested_value = cursor.get(segment)
            if not isinstance(nested_value, dict):
                nested_value = {}
                cursor[segment] = nested_value
            cursor = nested_value

        leaf_name = path_segments[-1]
        if leaf_name not in cursor:
            cursor[leaf_name] = path_value

    def _load_action_placeholder_value(self, path_name: str) -> str:
        normalized_path_name = str(path_name or "").strip()
        leaf_name = normalized_path_name.split(".")[-1] if normalized_path_name else ""
        if leaf_name in {"scan_dir", "job_postings_dir"}:
            return "<ABSOLUTE_DIRECTORY_PATH>"
        if leaf_name.endswith("_db_path") or leaf_name.endswith("_path"):
            return "<ABSOLUTE_PATH>"
        if leaf_name in {"thread_id", "dispatcher_message_id"}:
            return f"<{leaf_name.upper()}>"
        return f"<{normalized_path_name or leaf_name or 'value'}>"

    def _build_action_request_object(self, action_object: dict[str, Any]) -> dict[str, Any]:
        action_name = str(action_object.get("action_name") or "").strip()
        action_request: dict[str, Any] = {"action": action_name}

        for path_name in action_object.get("required_non_action_paths") or []:
            normalized_path_name = str(path_name or "").strip()
            if not normalized_path_name:
                continue
            self._set_action_request_path_value(
                action_request=action_request,
                path_name=normalized_path_name,
                path_value=self._load_action_placeholder_value(normalized_path_name),
            )

        for request_field in action_object.get("request_fields") or []:
            normalized_request_field = str(request_field or "").strip()
            if not normalized_request_field or normalized_request_field in action_request:
                continue
            self._set_action_request_path_value(
                action_request=action_request,
                path_name=normalized_request_field,
                path_value={
                    "source": "text",
                    "value": f"<{normalized_request_field}>",
                },
            )

        return action_request

    def _action_request_has_placeholders(self, value: Any) -> bool:
        if isinstance(value, str):
            normalized_value = value.strip()
            return normalized_value.startswith("<") and normalized_value.endswith(">")
        if isinstance(value, dict):
            return any(self._action_request_has_placeholders(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(self._action_request_has_placeholders(item) for item in value)
        return False

    def _show_footer_status(self, message: str, timeout_ms: int = 5000) -> None:
        try:
            window = self.window()
            status_bar = window.statusBar() if window is not None and hasattr(window, "statusBar") else None
            if status_bar is not None:
                status_bar.showMessage(str(message), int(timeout_ms))
        except Exception:
            pass

    def _handle_local_prompt_command(self, prompt_text: str) -> bool:
        command_text = str(prompt_text or "").strip()
        if not command_text:
            return False

        command_name, _separator, _rest = command_text.partition(" ")
        if command_name.casefold() != "/sync":
            return False

        window = self.window()
        manual_sync_runner = getattr(window, "trigger_explorer_manual_sync", None) if window is not None else None
        if not callable(manual_sync_runner):
            self._append("System", "Explorer /sync unavailable: explorer tree is not ready.")
            self._show_footer_status("Explorer /sync unavailable", 4000)
            self.prompt_edit.clear()
            return True

        sync_ok = bool(manual_sync_runner(source_label="chat_sync_command"))
        if sync_ok:
            self._append("System", "Explorer /sync completed.")
            self._show_footer_status("Explorer /sync completed", 2500)
        else:
            self._append("System", "Explorer /sync failed.")
            self._show_footer_status("Explorer /sync failed", 4500)

        self.prompt_edit.clear()
        self._dropped_files = []
        self._runtime_context_entries = []
        return True

    def _run_background_task(self, *, kind: str, worker: Callable[[], dict[str, Any]]) -> None:
        def _invoke_worker() -> None:
            try:
                payload = dict(worker() or {})
            except Exception as exc:
                payload = {
                    "kind": kind,
                    "reply": f"[ERROR] {type(exc).__name__}: {exc}",
                    "reset_context": True,
                }
            payload.setdefault("kind", kind)
            self._async_result_ready.emit(payload)

        Thread(target=_invoke_worker, daemon=True).start()

    @Slot(object)
    def _handle_async_result(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return

        kind = str(payload.get("kind") or "").strip().lower()
        status_message = str(payload.get("status_message") or "").strip()
        reset_context = bool(payload.get("reset_context", True))

        if kind in {"action", "chat", "image_describe"}:
            reply = str(payload.get("reply") or "").strip()
            self._append("AI", reply)
            if reset_context:
                self._dropped_files = []
                self._runtime_context_entries = []
            if status_message:
                self._show_footer_status(status_message)
            return

        if kind == "image_create":
            error_text = str(payload.get("error") or "").strip()
            if error_text:
                self._append("AI", f"[ERROR] {error_text}")
                if status_message:
                    self._show_footer_status(status_message)
                return

            image_path_text = str(payload.get("path") or "").strip()
            if not image_path_text:
                self._append("AI", "[ERROR] Image generation returned no path")
                if status_message:
                    self._show_footer_status(status_message)
                return

            image_path = Path(image_path_text)
            window = self.window()
            opener = getattr(window, "_open_path_in_focused_tab", None)
            if callable(opener):
                opener(image_path, title=image_path.name)
                self._append("AI", f"[IMAGE] {image_path}")
            else:
                self._append("AI", f"[IMAGE SAVED] {image_path}")

            if reset_context:
                self._dropped_files = []
                self._runtime_context_entries = []
            if status_message:
                self._show_footer_status(status_message)

    def _dispatch_action_object(self, *, action_object_name: str, action_request: dict[str, Any]) -> None:
        if getattr(self, "_api_key_missing", False):
            self._append("System", "Chat is disabled because OPENAI_API_KEY is not set.")
            return

        action_request_text = json.dumps(action_request, ensure_ascii=False)
        self._append("You", action_request_text)
        self.prompt_edit.clear()
        self._show_footer_status(f"Action gestartet: {action_object_name}")

        def _build_action_reply() -> dict[str, Any]:
            try:
                action_reply = ChatCom(
                    _model=self._model,
                    _input_text=action_request_text,
                ).get_response()
            except Exception as exc:
                action_reply = f"[ERROR] {exc}"

            return {
                "kind": "action",
                "reply": str(action_reply),
                "status_message": f"Action abgeschlossen: {action_object_name}",
                "reset_context": True,
            }

        self._run_background_task(kind="action", worker=_build_action_reply)

    def _start_action_object(self, action_object: dict[str, Any]) -> None:
        action_object_name = str(action_object.get("display_name") or action_object.get("action_object_name") or "Action").strip()
        action_request = self._build_action_request_object(action_object)
        action_request_text = json.dumps(action_request, ensure_ascii=False, indent=2)

        if self._action_request_has_placeholders(action_request):
            self.prompt_edit.setPlainText(action_request_text)
            prompt_cursor = self.prompt_edit.textCursor()
            prompt_cursor.movePosition(QTextCursor.End)
            self.prompt_edit.setTextCursor(prompt_cursor)
            self._show_footer_status(f"Action-Vorlage geladen: {action_object_name}")
            self._append(
                "System",
                f"Action-Vorlage geladen: {action_object_name}. Bitte Platzhalter ausfuellen und Send druecken.",
            )
            return

        self._dispatch_action_object(
            action_object_name=action_object_name,
            action_request=action_request,
        )

    def _rebuild_footer_action_menu(self) -> None:
        footer_menu = getattr(self, "_footer_action_menu", None)
        if not isinstance(footer_menu, QMenu):
            return

        footer_menu.clear()
        action_objects = self._load_configured_action_objects()
        if not action_objects:
            no_action = footer_menu.addAction("Keine konfigurierten Actions gefunden")
            no_action.setEnabled(False)
            return

        for action_object in action_objects:
            menu_label = str(
                action_object.get("display_name")
                or action_object.get("action_object_name")
                or action_object.get("action_name")
                or "Action"
            ).strip()
            menu_action = footer_menu.addAction(menu_label)
            menu_action.setData(dict(action_object))
            description = str(action_object.get("description") or "").strip()
            if description:
                menu_action.setToolTip(description)
                menu_action.setStatusTip(description)
            menu_action.triggered.connect(
                lambda _checked=False, action_data=dict(action_object): self._start_action_object(action_data)
            )

    def _prompt_shell_height(self, editor_height: int) -> int:
        composer = getattr(self, "prompt_composer", None)
        if composer is None:
            return int(editor_height)

        layout = composer.layout()
        if layout is None:
            return int(editor_height)

        margins = layout.contentsMargins()
        return int(editor_height) + margins.top() + margins.bottom()

    def _sync_prompt_shell_height(self, editor_height: int) -> None:
        composer = getattr(self, "prompt_composer", None)
        if composer is None:
            return
        composer.setFixedHeight(self._prompt_shell_height(editor_height))

    def _apply_prompt_snap_height(self) -> None:
        editor = getattr(self, "prompt_edit", None)
        if editor is None:
            return

        editor.setFixedHeight(self._PROMPT_SNAP_HEIGHT)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._sync_prompt_shell_height(self._PROMPT_SNAP_HEIGHT)

    def _schedule_prompt_autofit(self) -> None:
        QTimer.singleShot(0, self._autofit_prompt_editor)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._schedule_prompt_autofit()

    def _autofit_prompt_editor(self) -> None:
        editor = getattr(self, "prompt_edit", None)
        if editor is None:
            return

        document = editor.document()
        document.setTextWidth(max(1, editor.viewport().width()))
        layout = document.documentLayout()
        content_height = layout.documentSize().height() if layout is not None else document.size().height()

        target_height = int(content_height) + int(document.documentMargin() * 2) + self._PROMPT_AUTOFIT_PADDING
        target_height = max(self._PROMPT_SNAP_HEIGHT, min(target_height, self._PROMPT_MAX_HEIGHT))

        editor.setFixedHeight(target_height)
        editor.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if target_height >= self._PROMPT_MAX_HEIGHT else Qt.ScrollBarAlwaysOff
        )
        self._sync_prompt_shell_height(target_height)

    @Slot(list)
    def _remember_files(self, paths:list|None) -> None:
                self._dropped_files = CHAT_ATTACHMENT_SERVICE.normalize_object_paths(paths)
                status_message = CHAT_ATTACHMENT_SERVICE.build_status_message(self._dropped_files)
                if not status_message:
                    return
                try:
                    window = self.window()
                    status_bar = window.statusBar() if window is not None and hasattr(window, "statusBar") else None
                    if status_bar is not None:
                        status_bar.showMessage(status_message, 6000)
                except Exception:
                    pass
    # ---------------------------------------------------------------------------
    #  CHAT – Text-Prompt
    # ---------------------------------------------------------------------------
    def _runtime_context_display_title(self, *, title: str, source_path: str) -> str:
        normalized_source = str(source_path or "").strip()
        if normalized_source:
            return Path(normalized_source).name
        normalized_title = str(title or "").strip()
        return normalized_title or "runtime_widget"

    def _build_runtime_user_input_context_payload(self) -> str:
        entries = list(getattr(self, "_runtime_context_entries", []))
        if not entries:
            return ""

        max_chars = int(getattr(CHAT_ATTACHMENT_SERVICE, "_MAX_TEXT_CHARS", 12000) or 12000)
        blocks: list[str] = []
        for entry in entries:
            title = str(entry.get("title") or "runtime_widget").strip() or "runtime_widget"
            source_path = str(entry.get("source_path") or "").strip()
            language = str(entry.get("language") or "").strip().lower()
            content = str(entry.get("content") or "").strip("\n")
            if not content.strip():
                continue

            if len(content) > max_chars:
                content = content[:max_chars].rstrip() + "\n[TRUNCATED]"

            if language in {"text", "plaintext"}:
                language = ""
            fence_open = f"```{language}" if language else "```"

            header_lines = [f"[WIDGET_CONTEXT] {title}"]
            if source_path:
                header_lines.append(f"[SOURCE] {source_path}")

            blocks.append("\n".join(header_lines + [fence_open, content, "```"]))

        return "\n\n".join(blocks)

    @Slot()
    def _send(self) -> None:
        prompt_text_raw = self.prompt_edit.toPlainText()
        if self._handle_local_prompt_command(prompt_text_raw):
            return

        if getattr(self, "_api_key_missing", False):
            try:
                self._append("System", "Chat is disabled because OPENAI_API_KEY is not set.")
            except Exception:
                pass
            return
        prompt_visible, image_paths = CHAT_ATTACHMENT_SERVICE.build_prompt_payload(
            prompt_text=prompt_text_raw,
            file_paths=self._dropped_files,
        )
        runtime_context_payload = self._build_runtime_user_input_context_payload()
        prompt_for_model = prompt_visible
        if runtime_context_payload:
            prompt_for_model = (
                f"{prompt_for_model}\n\n{runtime_context_payload}"
                if prompt_for_model
                else runtime_context_payload
            )

        if not prompt_for_model and not image_paths:
            return

        self._append("You", prompt_visible)
        self.prompt_edit.clear()
        self._show_footer_status("Chat gesendet")

        def _build_chat_reply() -> dict[str, Any]:
            try:
                reply = ChatCom(
                    _model=self._model,
                    _url=image_paths or None,
                    _input_text=prompt_for_model,
                ).get_response()
            except Exception as exc:
                reply = f"[ERROR] {exc}"

            return {
                "kind": "chat",
                "reply": str(reply),
                "status_message": "Chat abgeschlossen",
                "reset_context": True,
            }

        self._run_background_task(kind="chat", worker=_build_chat_reply)

    # ---------------------------------------------------------------------------
    #  CHAT – Bild analysieren
    # ---------------------------------------------------------------------------
    @Slot()
    def _send_img(self) -> None:
        if getattr(self, "_api_key_missing", False):
            try:
                self._append("System", "Image analysis is disabled because OPENAI_API_KEY is not set.")
            except Exception:
                pass
            return
        prompt_visible = self.prompt_edit.toPlainText().strip()
        image_paths = CHAT_ATTACHMENT_SERVICE.load_image_object_paths(self._dropped_files)
        runtime_context_payload = self._build_runtime_user_input_context_payload()
        prompt_for_model = prompt_visible
        if runtime_context_payload:
            prompt_for_model = (
                f"{prompt_for_model}\n\n{runtime_context_payload}"
                if prompt_for_model
                else runtime_context_payload
            )

        if not (prompt_for_model and image_paths):
            QMessageBox.warning(self, "Info",
                "Ziehe ein Bild in das Chat-Fenster und gib anschließend deinen Prompt ein.")
            return

        self._append("You", prompt_visible)
        self.prompt_edit.clear()
        url = image_paths[0]
        self._show_footer_status("Bildanalyse gestartet")

        def _build_image_description_reply() -> dict[str, Any]:
            try:
                resp = ImageDescription(
                    _model="gpt-5",
                    _url=url,
                    _input_text=prompt_for_model,
                ).get_descript()

                if hasattr(resp, 'choices') and resp.choices:
                    reply = (resp.choices[0].message.content or "")
                elif hasattr(resp, 'content'):
                    reply = (resp.content or "")
                else:
                    reply = str(resp)
            except Exception as exc:
                reply = f"[ERROR] {exc}"

            return {
                "kind": "image_describe",
                "reply": str(reply),
                "status_message": "Bildanalyse abgeschlossen",
                "reset_context": True,
            }

        self._run_background_task(kind="image_describe", worker=_build_image_description_reply)

    # ---------------------------------------------------------------------------
    #  CHAT – Bild generieren
    # ---------------------------------------------------------------------------
    @Slot()
    def _create_img(self) -> None:
        if getattr(self, "_api_key_missing", False):
            try:
                self._append("System", "Image creation is disabled because OPENAI_API_KEY is not set.")
            except Exception:
                pass
            return
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "Info", "Bitte Prompt eingeben.")
            return  
        self._append("You", prompt)
        self.prompt_edit.clear()    
        self._show_footer_status("Bildgenerierung gestartet")

        def _build_image_create_result() -> dict[str, Any]:
            try:
                raw = ImageCreate(
                    _model="gpt-5",
                    _input_text=prompt,
                ).get_img()
                img_bytes, mime = decode_image_payload(raw)
                path = save_generated_image(img_bytes, mime=mime)
            except Exception as exc:
                return {
                    "kind": "image_create",
                    "error": f"{type(exc).__name__}: {exc}",
                    "status_message": "Bildgenerierung fehlgeschlagen",
                    "reset_context": True,
                }

            return {
                "kind": "image_create",
                "path": str(path),
                "status_message": "Bildgenerierung abgeschlossen",
                "reset_context": True,
            }

        self._run_background_task(kind="image_create", worker=_build_image_create_result)

    # ---------------------------------------------------------------------------
    #  HILFSFUNKTION – Nachricht an ChatWindow anhängen
    # ---------------------------------------------------------------------------
    def _append(self, who: str, txt: str) -> None:
        """legt eine neue Nachricht im Chat-Viewport an"""
        self.chat_view.add_message(who, txt)

    def attach_runtime_context(
        self,
        *,
        title: str,
        language: str,
        content: str,
        source_path: str = "",
    ) -> bool:
        normalized_title = str(title or "Runtime Widget").strip() or "Runtime Widget"
        normalized_language = str(language or "").strip().lower()
        normalized_content = str(content or "").strip("\n")
        normalized_source_path = str(source_path or "").strip()
        if not normalized_content.strip():
            return False

        entry = {
            "title": normalized_title,
            "language": normalized_language,
            "source_path": normalized_source_path,
            "content": normalized_content,
        }
        entries = list(getattr(self, "_runtime_context_entries", []))
        if entry not in entries:
            entries.append(entry)
        self._runtime_context_entries = entries

        prompt_title = self._runtime_context_display_title(
            title=normalized_title,
            source_path=normalized_source_path,
        )
        context_marker = prompt_title

        existing_prompt = self.prompt_edit.toPlainText().strip()
        existing_lines = [line.strip() for line in existing_prompt.splitlines() if line.strip()]
        if context_marker not in existing_lines:
            merged_prompt = f"{existing_prompt}\n{context_marker}" if existing_prompt else context_marker
        else:
            merged_prompt = existing_prompt
        self.prompt_edit.setPlainText(merged_prompt)

        cursor = self.prompt_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.prompt_edit.setTextCursor(cursor)

        try:
            window = self.window()
            status_bar = window.statusBar() if window is not None and hasattr(window, "statusBar") else None
            if status_bar is not None:
                status_bar.showMessage(f"UserInputContext ergänzt: {prompt_title}", 3200)
        except Exception:
            pass
        return True

    def open_agent_system_builder_panel(
        self,
        *,
        initial_payload: dict[str, Any],
        build_handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        previous_row = getattr(self, "_agent_builder_panel_row", None)
        if previous_row is not None:
            self.chat_view.remove_inline_panel(previous_row)
            self._agent_builder_panel_row = None

        panel = QFrame(self.chat_view.viewport)
        panel.setObjectName("chatInlineBuilderPanel")
        panel.setStyleSheet(
            """
            QFrame#chatInlineBuilderPanel {
                background: #2a2a2a;
                border: 1px solid #404040;
                border-radius: 10px;
            }
            QLabel#builderSectionTitle {
                color: #d7d7d7;
                font-weight: 700;
            }
            QPushButton#builderPrimaryButton {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 1px;
                min-width: 22px;
                min-height: 22px;
            }
            QPushButton#builderPrimaryButton:hover {
                background: rgba(255, 255, 255, 0.08);                )

    return {
        "name": normalized_job_name,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "runtime_agent": runtime_agent or None,
        "skill_profile": skill_profile_name or None,
        "default_object_name": default_object_name or None,
    }

                border-color: rgba(255, 255, 255, 0.18);
            }
            QPushButton#builderIconButton {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 1px;
                min-width: 22px;
                min-height: 22px;
            }
            QPushButton#builderIconButton:hover {
                background: rgba(255, 255, 255, 0.08);
                border-color: rgba(255, 255, 255, 0.18);
            }
            """
        )

        panel_bg = self.scheme.get("col7", "#050505")
        button_bg = panel_bg
        button_border = self.scheme.get("col10", "#242424")
        panel.setStyleSheet(
            f"""
            QFrame#chatInlineBuilderPanel {{
                background: {panel_bg};
                border: 1px solid {button_border};
                border-radius: 10px;
            }}
            QLabel#builderSectionTitle {{
                color: {self.scheme.get('col6', '#E3E3DED6')};
                font-weight: 700;
            }}
            QPushButton#builderPrimaryButton,
            QPushButton#builderIconButton {{
                background: {button_bg};
                border: 1px solid {button_border};
                border-radius: 8px;
                padding: 1px;
                min-width: 22px;
                min-height: 22px;
            }}
            QPushButton#builderPrimaryButton:hover,
            QPushButton#builderIconButton:hover {{
                background: {panel_bg};
                border-color: transparent;
            }}
            """
        )

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(8)

        top_buttons = QHBoxLayout()
        top_buttons.setContentsMargins(0, 0, 0, 0)
        top_buttons.setSpacing(6)
        btn_template = QPushButton("", panel)
        btn_build = QPushButton("", panel)
        btn_post = QPushButton("", panel)
        btn_copy = QPushButton("", panel)
        btn_template.setIcon(_icon("open_file.svg"))
        btn_build.setIcon(_icon("deployed_code.svg"))
        btn_post.setIcon(_icon("send.svg"))
        btn_copy.setIcon(_icon("file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg"))
        btn_template.setToolTip("Template laden")
        btn_build.setToolTip("Sync Build starten")
        btn_post.setToolTip("Ergebnis in Chat verschieben")
        btn_copy.setToolTip("JSON exportieren")
        btn_template.setIconSize(QSize(18, 18))
        btn_build.setIconSize(QSize(18, 18))
        btn_post.setIconSize(QSize(18, 18))
        btn_copy.setIconSize(QSize(18, 18))
        btn_template.setCursor(Qt.PointingHandCursor)
        btn_build.setCursor(Qt.PointingHandCursor)
        btn_post.setCursor(Qt.PointingHandCursor)
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_template.setObjectName("builderPrimaryButton")
        btn_build.setObjectName("builderPrimaryButton")
        btn_post.setObjectName("builderIconButton")
        btn_copy.setObjectName("builderIconButton")
        top_buttons.addWidget(btn_template, 0)
        top_buttons.addWidget(btn_build, 0)
        top_buttons.addStretch(1)
        top_buttons.addWidget(btn_post, 0)
        top_buttons.addWidget(btn_copy, 0)
        panel_layout.addLayout(top_buttons)

        editor = CodeViewer(
            json.dumps(initial_payload, ensure_ascii=False, indent=2),
            panel,
            language="json",
            editable=True,
            auto_fit=False,
            accent_color=self.scheme.get("col1", "#0fe913"),
            accent_selection_color=self.scheme.get("col2", "#58ed5b"),
            surface_color=self.scheme.get("col9", "#101010"),
            font_size_px=17,
            edit_border_radius_px=15,
            draw_border=False,
        )
        editor.setMinimumHeight(260)
        editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        panel_layout.addWidget(editor)

        bottom_buttons = QHBoxLayout()
        bottom_buttons.setContentsMargins(0, 0, 0, 0)
        bottom_buttons.setSpacing(6)
        btn_close = QPushButton("", panel)
        btn_close.setIcon(_icon("close.svg"))
        btn_close.setToolTip("Panel schliessen")
        btn_close.setIconSize(QSize(18, 18))
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setObjectName("builderIconButton")
        bottom_buttons.addWidget(btn_close, 0)
        bottom_buttons.addStretch(1)
        panel_layout.addLayout(bottom_buttons)

        panel_row = self.chat_view.add_inline_panel(panel)
        self._agent_builder_panel_row = panel_row
        latest_result: dict[str, Any] = {}

        def _set_builder_status(message: str, timeout_ms: int = 4500) -> None:
            try:
                window = self.window()
                status_bar_getter = getattr(window, "statusBar", None)
                if not callable(status_bar_getter):
                    return
                status_bar = status_bar_getter()
                if status_bar is not None:
                    status_bar.showMessage(f"Builder: {message}", timeout_ms)
            except Exception:
                pass

        _set_builder_status("Bereit")

        def _load_template() -> None:
            editor.setPlainText(json.dumps(initial_payload, ensure_ascii=False, indent=2))
            _set_builder_status("Template geladen")

        def _run_build() -> None:
            nonlocal latest_result
            raw_text = editor.toPlainText().strip()
            if not raw_text:
                _set_builder_status("Payload ist leer")
                return

            try:
                payload = json.loads(raw_text)
            except Exception as exc:
                _set_builder_status(f"JSON-Fehler ({type(exc).__name__})")
                return

            if not isinstance(payload, dict):
                _set_builder_status("Payload muss JSON-Objekt sein")
                return

            btn_build.setEnabled(False)
            try:
                latest_result = dict(build_handler(payload) or {})
                validation = dict(latest_result.get("validation") or {})
                _set_builder_status(
                    f"Build abgeschlossen (valid={bool(validation.get('valid', True))})"
                )
            except Exception as exc:
                _set_builder_status(f"Build fehlgeschlagen ({type(exc).__name__})")
                latest_result = {}
            finally:
                btn_build.setEnabled(True)

        def _post_result() -> None:
            if not latest_result:
                self._append("System", "Kein Build-Ergebnis vorhanden. Bitte zuerst Sync Build starten.")
                _set_builder_status("Kein Build-Ergebnis vorhanden")
                return
            self._append("AI", json.dumps(latest_result, ensure_ascii=False, indent=2))
            _set_builder_status("Ergebnis in Chat verschoben")

        def _copy_json() -> None:
            payload_text = editor.toPlainText()
            try:
                QApplication.clipboard().setText(payload_text)
                _set_builder_status("JSON in Zwischenablage")
            except Exception as exc:
                _set_builder_status(f"Kopieren fehlgeschlagen ({type(exc).__name__})")

        def _close_panel() -> None:
            self.chat_view.remove_inline_panel(panel_row)
            self._agent_builder_panel_row = None

        btn_template.clicked.connect(_load_template)
        btn_build.clicked.connect(_run_build)
        btn_post.clicked.connect(_post_result)
        btn_copy.clicked.connect(_copy_json)
        btn_close.clicked.connect(_close_panel)

# ---------------------------------------------------------------------------
#  HILFSFUNKTION – Nachricht an ChatWindow anhängen
# ---------------------------------------------------------------------------

'''
Kurzerklärung
─────────────
1. Das neue  ChatWindow  (inkl. MsgWidget/CodeViewer) rendert Text-Blöcke
   und ```-Fenced-Code``` separat – Code erscheint syntax-gehiglightet.

2. AIWidget benutzt jetzt
      • self.chat_view   für den gesamten Verlauf  
      • self.prompt_edit für die Eingabe
   Dadurch verschwinden veraltete Attribute (`inp_edit`, `out_edit` …).

3. Alle Chat-Routinen (_send, _send_img, _create_img) rufen intern `_append`,
   welches direkt `chat_view.add_message()` verwendet.

Der Patch ist vollständig lauffähig und benötigt lediglich die bestehenden
Hilfsklassen (FileDropTextEdit, ToolButton, ChatCom …) aus deinem Projekt.'''

# ────────────────────────────────────────────────────────────────────────────
#  2)  NEUER  CodeViewer  –  editierbare Chat-Bloecke mit Highlighting
# ────────────────────────────────────────────────────────────────────────────
def _clear_frame_chrome(frame: QFrame) -> None:
    """Disable native QFrame chrome so only stylesheet geometry remains visible."""

    frame.setFrameShape(QFrame.NoFrame)
    frame.setFrameStyle(0)
    frame.setLineWidth(0)
    frame.setMidLineWidth(0)


class CodeViewer(QPlainTextEdit):
    """Editierbarer Chat-Block fuer Code, Konfiguration und Dateiinhalt."""

    editRequested = Signal()

    _PADDING = 20
    _MIN_HEIGHT = 88
    _MAX_HEIGHT = 420
    _LANGUAGE_ALIASES = {
        "": "",
        "md": "markdown",
        "py": "python",
        "plaintext": "text",
        "text/plain": "text",
        "yml": "yaml",
    }
    _HIGHLIGHTERS = {
        "json": JSONHighlighter,
        "markdown": MDHighlighter,
        "python": QSHighlighter,
        "toml": TOMLHighlighter,
        "yaml": YAMLHighlighter,
    }
    _VIEW_BORDER_COLOR = "#2e2e2e"
    _BACKGROUND_COLOR = "#111"
    _TEXT_COLOR = "#DDD"

    def __init__(
        self,
        code: str,
        parent: QWidget | None = None,
        *,
        language: str = "",
        editable: bool = True,
        auto_fit: bool = True,
        accent_color: str = "#0fe913",
        accent_selection_color: str = "#58ed5b",
        background_color: str | None = None,
        surface_color: str = "#404040",
        font_size_px: int | None = None,
        edit_border_radius_px: int | None = None,
        edit_border_width_px: int | None = None,
        edit_border_left_width_px: int | None = None,
        edit_border_right_width_px: int | None = None,
        top_left_radius_px: int | None = None,
        top_right_radius_px: int | None = None,
        bottom_left_radius_px: int | None = None,
        bottom_right_radius_px: int | None = None,
        draw_border: bool = True,
    ) -> None:
        super().__init__(parent=parent)
        self._language = self._normalize_language(language)
        self._highlighter = None
        self._edit_mode = False
        self._accent_color = str(accent_color or "#0fe913")
        self._accent_selection_color = str(accent_selection_color or self._accent_color)
        self._background_color = str(background_color or self._BACKGROUND_COLOR)
        self._surface_color = str(surface_color or "#404040")
        self._font_size_px = int(font_size_px) if font_size_px is not None else None
        self._draw_border = bool(draw_border)
        self._uses_wrapped_layout = self._language in {"markdown", "text"}
        self._auto_fit = bool(auto_fit)

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.viewport().setAttribute(Qt.WA_StyledBackground, True)
        self.setUndoRedoEnabled(True)
        _clear_frame_chrome(self)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Fixed if self._auto_fit else QSizePolicy.Expanding,
        )
        self.setTabStopDistance(max(32, QFontMetrics(self.font()).horizontalAdvance("    ")))

        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if self._uses_wrapped_layout else QPlainTextEdit.NoWrap)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere if self._uses_wrapped_layout else QTextOption.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff if self._uses_wrapped_layout else Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff if self._uses_wrapped_layout else Qt.ScrollBarAsNeeded)
        self._edit_border_radius_px = max(0, int(edit_border_radius_px)) if edit_border_radius_px is not None else 15
        self._edit_border_width_px = max(0, int(edit_border_width_px)) if edit_border_width_px is not None else 1
        self._edit_border_left_width_px = (
            max(0, int(edit_border_left_width_px)) if edit_border_left_width_px is not None else None
        )
        self._edit_border_right_width_px = (
            max(0, int(edit_border_right_width_px)) if edit_border_right_width_px is not None else None
        )
        self._top_left_radius_px = max(0, int(top_left_radius_px)) if top_left_radius_px is not None else None
        self._top_right_radius_px = max(0, int(top_right_radius_px)) if top_right_radius_px is not None else None
        self._bottom_left_radius_px = max(0, int(bottom_left_radius_px)) if bottom_left_radius_px is not None else None
        self._bottom_right_radius_px = max(0, int(bottom_right_radius_px)) if bottom_right_radius_px is not None else None
        self.viewport().installEventFilter(self)
        self.setPlainText(code.rstrip("\n"))
        self._install_highlighter()
        self.set_edit_mode(editable)

        if self._auto_fit:
            self.textChanged.connect(self._schedule_autofit)
            try:
                self.document().documentLayout().documentSizeChanged.connect(lambda _size: self._schedule_autofit())
            except Exception:
                pass
            self._schedule_autofit()
        else:
            self.setMinimumHeight(max(220, self._MIN_HEIGHT))
            self.setMaximumHeight(16777215)

    def set_theme_colors(
        self,
        *,
        accent_color: str | None = None,
        accent_selection_color: str | None = None,
        background_color: str | None = None,
        surface_color: str | None = None,
    ) -> None:
        if accent_color:
            self._accent_color = str(accent_color)
        if accent_selection_color:
            self._accent_selection_color = str(accent_selection_color)
        if background_color:
            self._background_color = str(background_color)
        if surface_color:
            self._surface_color = str(surface_color)
        self.setStyleSheet(self._build_style(edit_mode=self._edit_mode))
        self.viewport().update()

    @classmethod
    def _normalize_language(cls, language: str | None) -> str:
        normalized = str(language or "").strip().lower()
        return cls._LANGUAGE_ALIASES.get(normalized, normalized)

    def _install_highlighter(self) -> None:
        highlighter_class = self._HIGHLIGHTERS.get(self._language)
        if highlighter_class is None:
            return
        try:
            self._highlighter = highlighter_class(self.document())
        except Exception:
            self._highlighter = None

    def _schedule_autofit(self) -> None:
        if not self._auto_fit:
            return
        QTimer.singleShot(0, self._autofit)

    def set_edit_mode(self, active: bool) -> None:
        self._edit_mode = bool(active)
        self.setReadOnly(not self._edit_mode)
        self.setTextInteractionFlags(
            Qt.TextEditorInteraction
            if self._edit_mode
            else Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self.setObjectName("aiInput" if self._edit_mode else "chatCodeViewer")
        self.viewport().setObjectName(f"{self.objectName()}Viewport")
        self.setStyleSheet(self._build_style(edit_mode=self._edit_mode))

        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.viewport().update()
        self.update()
        if self._auto_fit:
            self._schedule_autofit()

    def _build_style(self, *, edit_mode: bool) -> str:
        border_color = self._accent_color if edit_mode else self._VIEW_BORDER_COLOR
        border_radius = self._edit_border_radius_px if edit_mode else 8
        border_width = self._edit_border_width_px if edit_mode else 1
        border_top_width = border_width
        border_left_width = (
            self._edit_border_left_width_px
            if edit_mode and self._edit_border_left_width_px is not None
            else border_width
        )
        border_right_width = (
            self._edit_border_right_width_px
            if edit_mode and self._edit_border_right_width_px is not None
            else border_width
        )
        top_left_radius = self._top_left_radius_px if self._top_left_radius_px is not None else border_radius
        top_right_radius = self._top_right_radius_px if self._top_right_radius_px is not None else border_radius
        bottom_left_radius = self._bottom_left_radius_px if self._bottom_left_radius_px is not None else border_radius
        bottom_right_radius = self._bottom_right_radius_px if self._bottom_right_radius_px is not None else border_radius
        if not self._draw_border:
            border_width = 0
            border_top_width = 0
            border_left_width = 0
            border_right_width = 0
        else:
            border_top_width = 0
        selection_color = self._accent_selection_color if edit_mode else "rgba(120,120,120,96)"
        scrollbar_hover_color = self._surface_color
        scrollbar_pressed_color = self._accent_selection_color
        scrollbar_idle_color = "rgba(180, 180, 180, 0.45)" if edit_mode else "rgba(135, 135, 135, 0.40)"
        font_size_rule = f" font-size:{self._font_size_px}px;" if self._font_size_px is not None else ""
        viewport_object_name = f"{self.objectName()}Viewport"
        return (
            f"QPlainTextEdit#{self.objectName()} {{"
            f" background:{self._background_color};"
            f" color:{self._TEXT_COLOR};"
            " padding:12px;"
            f" border-style:solid;"
            f" border-color:{border_color};"
            f" border-top-width:{border_top_width}px;"
            f" border-left-width:{border_left_width}px;"
            f" border-bottom-width:{border_width}px;"
            f" border-right-width:{border_right_width}px;"
            f" border-top-left-radius:{top_left_radius}px;"
            f" border-top-right-radius:{top_right_radius}px;"
            f" border-bottom-left-radius:{bottom_left_radius}px;"
            f" border-bottom-right-radius:{bottom_right_radius}px;"
            f" selection-background-color:{selection_color};"
            f"{font_size_rule}"
            " font-family:'Fira Code','DejaVu Sans Mono','Liberation Mono',monospace;"
            "}"
            f"QWidget#{viewport_object_name} {{"
            f" background:{self._background_color};"
            " border:none;"
            f" border-top-left-radius:{top_left_radius}px;"
            f" border-top-right-radius:{top_right_radius}px;"
            f" border-bottom-left-radius:{bottom_left_radius}px;"
            f" border-bottom-right-radius:{bottom_right_radius}px;"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar:vertical {{"
            " background:transparent;"
            " width:8px;"
            " margin:0px;"
            " border:none;"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar:horizontal {{"
            " background:transparent;"
            " height:8px;"
            " margin:0px;"
            " border:none;"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:horizontal {{"
            f" background:{scrollbar_idle_color};"
            " min-height:24px;"
            " min-width:24px;"
            " border-radius:3px;"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:vertical:hover,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:horizontal:hover,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:hover:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:hover:horizontal {{"
            f" background:{scrollbar_hover_color};"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:vertical:pressed,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:horizontal:pressed,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:pressed:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::handle:pressed:horizontal {{"
            f" background:{scrollbar_pressed_color};"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::add-line:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::sub-line:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::add-line:horizontal,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::sub-line:horizontal {{"
            " width:0px;"
            " height:0px;"
            " border:none;"
            " background:transparent;"
            "}"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::add-page:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::sub-page:vertical,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::add-page:horizontal,"
            f"QPlainTextEdit#{self.objectName()} QScrollBar::sub-page:horizontal {{"
            " background:transparent;"
                "}"
        )

    def mousePressEvent(self, ev):  # noqa: N802
        if self.isReadOnly():
            self.editRequested.emit()
        super().mousePressEvent(ev)

    def eventFilter(self, obj, ev):  # noqa: N802
        if obj is self.viewport() and ev.type() == QEvent.Wheel:
            if not self._auto_fit:
                return False
            self.wheelEvent(ev)
            return bool(ev.isAccepted())
        return super().eventFilter(obj, ev)

    def wheelEvent(self, ev: QWheelEvent) -> None:
        if not self._auto_fit:
            super().wheelEvent(ev)
            return

        angle_delta = ev.angleDelta()
        pixel_delta = ev.pixelDelta()
        delta_y = angle_delta.y() if angle_delta.y() else pixel_delta.y()
        delta_x = angle_delta.x() if angle_delta.x() else pixel_delta.x()

        use_horizontal = bool(delta_x) or bool(ev.modifiers() & Qt.ShiftModifier)
        target_bar = self.horizontalScrollBar() if use_horizontal else self.verticalScrollBar()
        delta = delta_x if use_horizontal and delta_x else delta_y

        if target_bar is not None and target_bar.maximum() > target_bar.minimum() and delta:
            direction = -1 if delta > 0 else 1
            step = max(target_bar.singleStep(), 24)
            target_bar.setValue(target_bar.value() + direction * step)
            ev.accept()
            return

        super().wheelEvent(ev)

    def resizeEvent(self, ev):  # noqa: N802
        super().resizeEvent(ev)
        if self._auto_fit:
            self._schedule_autofit()

    def _autofit(self) -> None:
        document = self.document()
        if self.lineWrapMode() == QPlainTextEdit.WidgetWidth:
            document.setTextWidth(max(1, self.viewport().width()))
        else:
            document.setTextWidth(-1)

        layout = document.documentLayout()
        content_height = layout.documentSize().height() if layout is not None else document.size().height()
        target_height = int(content_height) + self._PADDING
        target_height = max(self._MIN_HEIGHT, min(target_height, self._MAX_HEIGHT))
        self.setFixedHeight(target_height)

        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff if self._uses_wrapped_layout else Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff if self._uses_wrapped_layout else Qt.ScrollBarAsNeeded)


class ChatEditorPanel(QWidget):
    """Editor-Panel fuer Chat-Bloecke mit Klick-aktiviertem Edit-Mode."""

    def __init__(
        self,
        *,
        segment: ChatSegment,
        parent: QWidget | None = None,
        save_handler: Callable[[QPlainTextEdit, str], None] | None = None,
        scheme: dict[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._file_path = str(segment.file_path or "")
        self._save_handler = save_handler
        self._scheme = dict(scheme or {})
        self._expanded = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._panel = QFrame(self)
        self._panel.setObjectName("runtimeWidgetPanel")
        _clear_frame_chrome(self._panel)
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(_SURFACE_INSET_PX, _SURFACE_INSET_PX, _SURFACE_INSET_PX, 0)
        header.setSpacing(6)

        header.addStretch(1)

        def _add_header_action(icon_name: str, tooltip: str, slot_callable, *, checkable: bool = False, checked: bool = False) -> QToolButton:
            action_btn = QToolButton(self._panel)
            action_btn.setObjectName("runtimeWidgetActionButton")
            action_btn.setIcon(_icon(icon_name))
            action_btn.setIconSize(QSize(14, 14))
            action_btn.setToolTip(tooltip)
            action_btn.setCursor(Qt.PointingHandCursor)
            action_btn.setAutoRaise(True)
            action_btn.setCheckable(bool(checkable))
            if checkable:
                action_btn.setChecked(bool(checked))
            if slot_callable is not None:
                action_btn.clicked.connect(slot_callable)
            header.addWidget(action_btn, 0)
            return action_btn

        self._copy_btn = _add_header_action(
            "file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg",
            "Block in Zwischenablage kopieren",
            lambda _checked=False: self._copy_block(),
        )

        self._expand_btn = _add_header_action(
            "expand_content_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg",
            "Block einklappen",
            lambda checked: self._toggle_expanded(checked),
            checkable=True,
            checked=True,
        )

        self._edit_btn = _add_header_action(
            "edit_26dp_999999_FILL0_wght500_GRAD0_opsz24.svg",
            "Block bearbeiten",
            self.set_edit_mode,
            checkable=True,
            checked=False,
        )

        self._open_tab_btn: QToolButton | None = None
        if self._file_path:
            self._open_tab_btn = _add_header_action(
                "open_in_new_dock.svg",
                "In neuem Tab oeffnen",
                lambda _checked=False: self._open_in_new_tab(),
            )

        self._save_btn: QToolButton | None = _add_header_action(
            "send.svg",
            "Erneut senden",
            lambda _checked=False: self._save_to_source(),
        )
        if self._file_path:
            self._save_btn.setEnabled(False)
            self._save_btn.setToolTip(f"Erneut senden: {self._file_path}")

        self.viewer = CodeViewer(
            segment.block.rstrip("\n"),
            self._panel,
            language=segment.language,
            editable=False,
            accent_color=self._scheme.get("col1", "#0fe913"),
            accent_selection_color=self._scheme.get("col2", self._scheme.get("col1", "#58ed5b")),
            surface_color=self._scheme.get("col10", "#404040"),
            font_size_px=14,
            top_left_radius_px=0,
            top_right_radius_px=0,
            bottom_left_radius_px=_SURFACE_BORDER_RADIUS_PX,
            bottom_right_radius_px=_SURFACE_BORDER_RADIUS_PX,
            draw_border=False,
        )
        self.viewer.setProperty("file_path", self._file_path)

        panel_layout.addLayout(header)
        panel_layout.addWidget(self.viewer)
        layout.addWidget(self._panel)

        panel_bg = self._scheme.get("col7", "#0b0b0b")
        panel_border = self._scheme.get("col10", "#1f1f1f")
        panel_fg = self._scheme.get("col6", "#E3E3DE")
        self._panel.setStyleSheet(
            f"""
            QFrame#runtimeWidgetPanel {{
                background: {panel_bg};
                border: {_SURFACE_BORDER_WIDTH_PX}px solid {panel_border};
                border-radius: {_SURFACE_BORDER_RADIUS_PX}px;
            }}
            QLabel#runtimeWidgetTitle {{
                color: {panel_fg};
                font-weight: 600;
            }}
            QToolButton#runtimeWidgetActionButton {{
                color: {panel_fg};
                background: {panel_bg};
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 2px;
            }}
            QToolButton#runtimeWidgetActionButton:hover {{
                background: {panel_bg};
                border: 1px solid transparent;
            }}
            """
        )

        self.viewer.editRequested.connect(self._enter_edit_mode)

        if self._save_btn is not None:
            self.viewer.document().modificationChanged.connect(self._save_btn.setEnabled)

    def _enter_edit_mode(self) -> None:
        if not self._expanded:
            self._set_expanded(True)
        if not self._edit_btn.isChecked():
            self._edit_btn.setChecked(True)
        self.set_edit_mode(True)

    def _toggle_expanded(self, expanded: bool) -> None:
        self._set_expanded(bool(expanded))

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.viewer.setVisible(self._expanded)
        self._expand_btn.setToolTip("Block einklappen" if self._expanded else "Block ausklappen")

    def set_edit_mode(self, active: bool) -> None:
        active_flag = bool(active)
        if active_flag and not self._expanded:
            self._set_expanded(True)
            if not self._expand_btn.isChecked():
                self._expand_btn.setChecked(True)
        self.viewer.set_edit_mode(active_flag)
        self._edit_btn.setIcon(
            _icon("edit_note_26dp_999999_FILL0_wght500_GRAD0_opsz24.svg")
            if active_flag
            else _icon("edit_26dp_999999_FILL0_wght500_GRAD0_opsz24.svg")
        )
        self._edit_btn.setToolTip("Bearbeiten beenden" if active_flag else "Block bearbeiten")
        if active:
            self.viewer.setFocus(Qt.MouseFocusReason)
        else:
            self.viewer.clearFocus()

    def _open_in_new_tab(self) -> None:
        if not self._file_path:
            return
        window = self.window()
        opener = getattr(window, "_open_path_in_focused_tab", None)
        if not callable(opener):
            return
        try:
            opener(Path(self._file_path), title=Path(self._file_path).name)
        except Exception:
            return

    def _copy_block(self) -> None:
        try:
            QApplication.clipboard().setText(self.viewer.toPlainText())
        except Exception:
            return

    def _save_to_source(self) -> None:
        if self._save_handler is not None and self._file_path:
            self._save_handler(self.viewer, self._file_path)
            return

        block_text = str(self.viewer.toPlainText() or "").strip("\n")
        if not block_text.strip():
            return

        language = str(getattr(self.viewer, "_language", "") or "").strip().lower()
        if not language:
            probe = block_text.lstrip()
            python_markers = (
                "#!/usr/bin/env python",
                "#!/usr/bin/python",
                "import ",
                "from ",
                "def ",
                "class ",
                "if __name__ ==",
            )
            if probe.startswith(python_markers):
                language = "python"

        payload = f"```{language}\n{block_text}\n```" if language else block_text

        dispatcher = self.window()
        append_callable = getattr(dispatcher, "_append", None)
        if not callable(append_callable):
            probe_parent = self.parent()
            while probe_parent is not None and not callable(append_callable):
                append_callable = getattr(probe_parent, "_append", None)
                probe_parent = probe_parent.parent()

        if callable(append_callable):
            try:
                append_callable("You", payload)
            except Exception:
                return


class MsgWidget(QWidget):
    """Chat-Bubble mit Text-, Bild- und editierbaren Block-Segmenten."""

    def __init__(
        self,
        who: str,
        segments: list[ChatSegment],
        parent: QWidget | None = None,
        *,
        scheme: dict[str, str] | None = None,
    ):
        super().__init__(parent)
        self.setStyleSheet("MsgWidget { background: transparent; }")
        self._scheme = dict(scheme or {})

        h_layout = QHBoxLayout(self)
        h_layout.setContentsMargins(8, 4, 8, 4)
        h_layout.setSpacing(0)

        bubble = QWidget()
        bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._bubble = bubble
        bubble_bg = self._scheme.get("col9", "#101010")
        bubble_border = self._scheme.get("col10", "#242424")
        bubble_fg = self._scheme.get("col6", "#e0e0e0")

        v_layout = QVBoxLayout(bubble)
        v_layout.setContentsMargins(14, 10, 14, 10)
        v_layout.setSpacing(6)

        from PySide6.QtWidgets import QLabel
        bubble.setStyleSheet(
            f"""
            QWidget {{
                background: {bubble_bg};
                border: 1px solid {bubble_border};
                border-radius: 10px;
                padding: 10px 14px;
                color: {bubble_fg};
            }}
            QWidget * {{
                border: none;
                outline: none;
            }}
            """
        )

        username_label = QLabel(f"<small style='opacity:0.6; color:#e0e0e0;'>{who}</small>")
        if who == "AI":
            v_layout.addWidget(username_label, 0, Qt.AlignLeft)
            h_layout.addWidget(bubble, 1)
        else:
            v_layout.addWidget(username_label, 0, Qt.AlignRight)
            h_layout.addWidget(bubble, 1)

        for segment in segments:
            kind = segment.kind
            language = segment.language
            block = segment.block
            if not str(block or "").strip():
                continue

            if kind == "editor":
                editor_panel = ChatEditorPanel(
                    segment=segment,
                    parent=bubble,
                    save_handler=self._save_editor_block if segment.file_path else None,
                    scheme=self._scheme,
                )
                v_layout.addWidget(editor_panel)
                continue

            first = block.splitlines()[0].strip()
            image_match = re.match(r'!\[.*?\]\((.*?)\)', first)
            if first.startswith("[IMAGE]") or image_match:
                path_str = None
                if first.startswith("[IMAGE]"):
                    parts = first.split(None, 1)
                    if len(parts) > 1:
                        path_str = parts[1].strip()
                elif image_match:
                    path_str = image_match.group(1)

                if path_str:
                    try:
                        p = Path(path_str)
                    except Exception:
                        p = None

                    if p and p.exists():
                        ctrl = QWidget(bubble)
                        hctrl = QHBoxLayout(ctrl)
                        hctrl.setContentsMargins(0, 0, 0, 0)
                        hctrl.addStretch(1)
                        save_btn = QToolButton(ctrl)
                        save_btn.setText("Save as")
                        export_btn = QToolButton(ctrl)
                        export_btn.setText("Export to tab")
                        hctrl.addWidget(save_btn)
                        hctrl.addWidget(export_btn)

                        img_widget = None
                        if '_FVChatImageWidget' in globals() and _FVChatImageWidget is not None:
                            try:
                                img_widget = _FVChatImageWidget(p, parent=bubble)
                            except Exception:
                                img_widget = None
                        if img_widget is None and _FVImageWidget is not None:
                            try:
                                img_widget = _FVImageWidget(p, parent=bubble)
                            except Exception:
                                img_widget = None

                        if img_widget is None:
                            lbl = QLabel(bubble, alignment=Qt.AlignCenter)
                            pix = QPixmap(str(p))
                            if not pix.isNull():
                                lbl.setPixmap(pix.scaledToWidth(400, Qt.SmoothTransformation))
                            v_layout.addWidget(lbl)
                        else:
                            cont = QWidget(bubble)
                            vbox_img = QVBoxLayout(cont)
                            vbox_img.setContentsMargins(0, 0, 0, 0)
                            vbox_img.setSpacing(4)
                            vbox_img.addWidget(ctrl)
                            vbox_img.addWidget(img_widget)
                            v_layout.addWidget(cont)

                            def _on_save() -> None:
                                fname, _ = QFileDialog.getSaveFileName(self, "Save image as", str(Path.home()))
                                if fname:
                                    try:
                                        shutil.copy(str(p), fname)
                                        QMessageBox.information(self, "Saved", f"Saved to {fname}")
                                    except Exception as exc:
                                        QMessageBox.critical(self, "Error", str(exc))

                            def _on_export() -> None:
                                win = self.window()
                                opener = getattr(win, "_open_path_in_focused_tab", None)
                                if callable(opener):
                                    opener(p, title=p.name)
                                else:
                                    QMessageBox.information(self, "Info", "No tab-dock available to export image")

                            save_btn.clicked.connect(_on_save)
                            export_btn.clicked.connect(_on_export)
                            continue

            br = QTextBrowser(bubble)
            br.setFrameShape(QFrame.NoFrame)
            br.setOpenExternalLinks(True)
            br.setMarkdown(block)
            br.document().setDocumentMargin(0)
            br.setStyleSheet("QTextBrowser { background: transparent; color: #e0e0e0; font-size: 14px; }")
            br.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            br.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            br.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._fit_browser(br)
            v_layout.addWidget(br)
            QTimer.singleShot(0, lambda b=br: self._fit_browser(b))

            try:
                br.document().documentLayout().documentSizeChanged.connect(lambda _sz, b=br: self._fit_browser(b))
            except Exception:
                pass

        v_layout.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Fixed))

    @staticmethod
    def _write_editor_text_to_path(*, file_path: str | Path, text: str) -> None:
        target_path = Path(file_path).expanduser()
        if not target_path.parent.exists():
            raise FileNotFoundError(f"Zielpfad nicht gefunden: {target_path.parent}")
        target_path.write_text(text, encoding="utf-8")

    def _save_editor_block(self, viewer: QPlainTextEdit, file_path: str) -> None:
        try:
            self._write_editor_text_to_path(file_path=file_path, text=viewer.toPlainText())
        except Exception as exc:
            QMessageBox.critical(self, "Fehler", str(exc))
            return

        viewer.document().setModified(False)
        message = f"{file_path} gespeichert"
        window = self.window()
        status_bar_getter = getattr(window, "statusBar", None)
        if callable(status_bar_getter):
            try:
                status_bar = status_bar_getter()
            except Exception:
                status_bar = None
            if status_bar is not None:
                status_bar.showMessage(message, 3000)
                return
        QMessageBox.information(self, "Gespeichert",  )

    def resizeEvent(self, ev):  # noqa: N802
        super().resizeEvent(ev)
        try:
            max_w = max(1, self.width() - 16)
            if max_w > 0 and hasattr(self, "_bubble") and self._bubble is not None:
                self._bubble.setMaximumWidth(max_w)
        except Exception:
            pass

    def _fit_browser(self, br: QTextBrowser) -> None:
        doc = br.document()
        w = max(1, br.viewport().width())
        doc.setTextWidth(w)

        h_doc = int(doc.size().height()) + 2
        font_h = QFontMetrics(br.font()).height()
        h_min = max(3, 3 * font_h)
        br.setFixedHeight(max(h_doc, h_min))


class ChatInlinePanelSlot(QFrame):
    """Inline chat slot with vertical resize handle."""

    def __init__(self, panel: QWidget, *, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("chatInlineSlot")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Vertical, self)
        self.splitter.setObjectName("chatInlineSlotSplitter")
        self.splitter.setChildrenCollapsible(True)
        self.splitter.setHandleWidth(4)
        self.splitter.setOpaqueResize(True)

        self.content_host = QWidget(self.splitter)
        content_layout = QVBoxLayout(self.content_host)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(0)
        panel.setParent(self.content_host)
        content_layout.addWidget(panel, 1)

        self.resize_buffer = QWidget(self.splitter)
        self.resize_buffer.setObjectName("chatInlineSlotResizeBuffer")
        self.resize_buffer.setMinimumHeight(22)
        self.resize_buffer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        self.splitter.addWidget(self.content_host)
        self.splitter.addWidget(self.resize_buffer)
        self.splitter.setSizes([max(220, panel.sizeHint().height() + 20), 1])
        root.addWidget(self.splitter, 1)


class ChatWindow(QWidget):
    """Container fuer den kompletten Chat-Verlauf."""

    _FILE_HEADER_PATTERN = re.compile(
        r"^\[FILE\]\s+(?P<name>.+?)\s+\((?P<kind>[^)]+)\)(?:\s+\[truncated\])?$"
    )
    _SOURCE_HEADER_PATTERN = re.compile(
        rf"^{re.escape(ChatAttachmentService._SOURCE_HEADER_PREFIX)}\s+(?P<path>.+?)\s*$"
    )
    _LANGUAGE_BY_SUFFIX = dict(ChatAttachmentService._LANGUAGE_BY_SUFFIX)
    _OUTER_MARGIN_LEFT_RIGHT = 0
    _OUTER_MARGIN_BOTTOM = 0

    def __init__(self, scheme: dict[str, str] | None = None):
        super().__init__()
        self._scheme = dict(scheme or {})
        self._prompt_widget: QWidget | None = None
        self._footer_widget: QWidget | None = None
        self._prompt_snap_height = 90

        root = QVBoxLayout(self)
        root.setContentsMargins(
            self._OUTER_MARGIN_LEFT_RIGHT,
            0,
            self._OUTER_MARGIN_LEFT_RIGHT,
            self._OUTER_MARGIN_BOTTOM,
        )
        root.setSpacing(0)
        self.setObjectName("chatHistoryWindow")

        from PySide6.QtWidgets import QScrollArea

        self._shell = QFrame(self)
        self._shell.setObjectName("chatHistoryShell")
        self._shell_layout = QVBoxLayout(self._shell)
        self._shell_layout.setContentsMargins(0, 0, 0, 0)
        self._shell_layout.setSpacing(0)
        root.addWidget(self._shell, 1)

        self.scroller = QScrollArea(self._shell)
        self.scroller.setObjectName("chatHistoryScroller")
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.NoFrame)
        self.scroller.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroller.viewport().setObjectName("chatHistoryScrollViewport")
        self._shell_layout.addWidget(self.scroller, 1)

        self.viewport = QWidget()
        self.viewport.setObjectName("chatHistoryViewport")
        self.vlayout = QVBoxLayout(self.viewport)
        self.vlayout.setContentsMargins(8, 8, 8, 8)
        self.vlayout.setSpacing(2)
        self.vlayout.setAlignment(Qt.AlignTop)
        self.scroller.setWidget(self.viewport)

        self._prompt_container = QFrame(self._shell)
        self._prompt_container.setObjectName("chatPromptContainer")
        self._prompt_layout = QVBoxLayout(self._prompt_container)
        self._prompt_layout.setContentsMargins(12, 10, 12, 0)
        self._prompt_layout.setSpacing(6)
        self._shell_layout.addWidget(self._prompt_container, 0)
        self._prompt_container.hide()

        self._apply_history_style()

    def update_scheme(self, scheme: dict[str, str] | None) -> None:
        self._scheme = dict(scheme or {})
        self._apply_history_style()
        self.update()

    def set_prompt_widget(self, prompt_widget: QWidget, *, snap_height: int = 90) -> None:
        if prompt_widget is None:
            return

        self._prompt_snap_height = max(64, int(snap_height))
        if self._prompt_widget is prompt_widget:
            self._prompt_widget.setFixedHeight(self._prompt_snap_height)
            self._prompt_container.show()
            return

        if self._prompt_widget is not None:
            self._prompt_layout.removeWidget(self._prompt_widget)
            self._prompt_widget.setParent(None)

        prompt_widget.setParent(self._prompt_container)
        prompt_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        prompt_widget.setFixedHeight(self._prompt_snap_height)
        self._prompt_layout.insertWidget(0, prompt_widget, 0)
        self._prompt_widget = prompt_widget
        self._prompt_container.show()
        prompt_widget.show()

    def set_footer_widget(self, footer_widget: QWidget | None) -> None:
        if footer_widget is None:
            return

        if self._footer_widget is footer_widget:
            self._prompt_container.show()
            return

        if self._footer_widget is not None:
            self._prompt_layout.removeWidget(self._footer_widget)
            self._footer_widget.setParent(None)

        footer_widget.setParent(self._prompt_container)
        footer_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._prompt_layout.addWidget(footer_widget, 0)
        self._footer_widget = footer_widget
        self._prompt_container.show()
        footer_widget.show()

    def _apply_history_style(self) -> None:
        history_bg = self._scheme.get("col7", "#0b0b0b")
        prompt_bg = history_bg
        history_border = self._scheme.get("col10", "#404040")
        history_accent = self._scheme.get("col2", self._scheme.get("col1", "#58ed5b"))
        slot_handle_idle, slot_handle_hover, slot_handle_pressed = _splitter_handle_palette(self._scheme)
        self.setStyleSheet(
            f"""
            QWidget#chatHistoryWindow {{
                background: transparent;
            }}
            QFrame#chatHistoryShell {{
                background: {history_bg};
                border: 1px solid {history_border};
                border-radius: 12px;
            }}
            QScrollArea#chatHistoryScroller {{
                background: transparent;
                border: none;
            }}
            QWidget#chatHistoryScrollViewport {{
                background: transparent;
                border: none;
            }}
            QWidget#chatHistoryViewport {{
                background: transparent;
                border: none;
            }}
            QFrame#chatPromptContainer {{
                background: {prompt_bg};
                border-top: none;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }}
            QFrame#chatPromptComposer {{
                background: {history_bg};
                border: 1px solid {history_accent};
                border-radius: 10px;
            }}
            QFrame#chatPromptComposer QTextEdit#aiInput {{
                background: transparent;
                border: none;
            }}
            QPushButton#chatPromptSendButton {{
                background: transparent;
                border: none;
                padding: 0px;
                border-radius: 8px;
            }}
            QPushButton#chatPromptSendButton:hover,
            QPushButton#chatPromptSendButton:pressed,
            QPushButton#chatPromptSendButton:checked {{
                background: transparent;
                border: none;
                border-radius: 8px;
            }}
            QWidget#chatFooterControls {{
                background: transparent;
                border: none;
            }}
            QToolButton#chatFooterActionsButton {{
                background: transparent;
                border: none;
                padding: 0px;
            }}
            QToolButton#chatFooterActionsButton:hover,
            QToolButton#chatFooterActionsButton:pressed,
            QToolButton#chatFooterActionsButton:checked {{
                background: transparent;
                border: none;
            }}
            QScrollArea#chatHistoryScroller QScrollBar:vertical,
            QScrollArea#chatHistoryScroller QScrollBar:horizontal {{
                background: transparent;
                margin: 0px;
                border: none;
            }}
            QScrollArea#chatHistoryScroller QScrollBar:vertical {{
                width: 6px;
            }}
            QScrollArea#chatHistoryScroller QScrollBar:horizontal {{
                height: 6px;
            }}
            QScrollArea#chatHistoryScroller QScrollBar::handle:vertical,
            QScrollArea#chatHistoryScroller QScrollBar::handle:horizontal {{
                background: transparent;
                border-radius: 3px;
                min-height: 28px;
                min-width: 28px;
            }}
            QScrollArea#chatHistoryScroller QScrollBar::handle:vertical:hover,
            QScrollArea#chatHistoryScroller QScrollBar::handle:horizontal:hover,
            QScrollArea#chatHistoryScroller QScrollBar::handle:hover:vertical,
            QScrollArea#chatHistoryScroller QScrollBar::handle:hover:horizontal {{
                background: {history_border};
            }}
            QScrollArea#chatHistoryScroller QScrollBar::handle:vertical:pressed,
            QScrollArea#chatHistoryScroller QScrollBar::handle:horizontal:pressed,
            QScrollArea#chatHistoryScroller QScrollBar::handle:pressed:vertical,
            QScrollArea#chatHistoryScroller QScrollBar::handle:pressed:horizontal {{
                background: {history_accent};
            }}
            QScrollArea#chatHistoryScroller QScrollBar::add-line,
            QScrollArea#chatHistoryScroller QScrollBar::sub-line,
            QScrollArea#chatHistoryScroller QScrollBar::add-page,
            QScrollArea#chatHistoryScroller QScrollBar::sub-page {{
                background: none;
                border: none;
                width: 5px;
                height:40px;
            }}
            QFrame#chatInlineSlot {{
                border: 1px solid {history_border};
                border-radius: 10px;
                background: transparent;
            }}
            QSplitter#chatInlineSlotSplitter::handle:horizontal {{
                background: {slot_handle_idle};
                margin: 0px 10px;
                min-height: 7px;
                border-radius: 999px;
            }}
            QSplitter#chatInlineSlotSplitter::handle:vertical {{
                background: {slot_handle_idle};
                margin: 10px 0px;
                min-width: 4px;
                border-radius: 999px;
            }}
            QSplitter#chatInlineSlotSplitter::handle:hover {{
                background: {slot_handle_hover};
            }}
            QSplitter#chatInlineSlotSplitter::handle:pressed {{
                background: {slot_handle_pressed};
            }}
            """
        )

    def add_message(self, who: str, text: str) -> None:
        msg = MsgWidget(who, self._split_segments(text), self.viewport, scheme=self._scheme)
        self.vlayout.addWidget(msg)
        bar = self.scroller.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_inline_panel(self, panel: QWidget) -> QWidget:
        slot = ChatInlinePanelSlot(panel, parent=self.viewport)
        self.vlayout.addWidget(slot)
        bar = self.scroller.verticalScrollBar()
        bar.setValue(bar.maximum())
        return slot

    def remove_inline_panel(self, row: QWidget | None) -> None:
        if row is None:
            return
        self.vlayout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()

    @staticmethod
    def _split_segments(raw: str) -> list[ChatSegment]:
        out: list[ChatSegment] = []
        buf: list[str] = []
        mode = "text"
        fence_language = ""
        pending_file_context: ChatFileContext | None = None

        for ln in raw.splitlines():
            stripped = ln.strip()
            if stripped.startswith("```"):
                if mode == "text":
                    if buf:
                        plain_segments, pending_file_context = ChatWindow._split_plain_segment(
                            "\n".join(buf),
                            allow_file_context=True,
                        )
                        out.extend(plain_segments)
                    buf = []
                    mode = "code"
                    fence_language = stripped[3:].strip()
                else:
                    out.append(
                        ChatSegment(
                            kind="editor",
                            language=ChatWindow._normalize_language(fence_language)
                            or (pending_file_context.language if pending_file_context else ""),
                            block="\n".join(buf).rstrip("\n"),
                            file_path=pending_file_context.file_path if pending_file_context else "",
                        )
                    )
                    buf = []
                    mode = "text"
                    fence_language = ""
                    pending_file_context = None
                continue
            buf.append(ln)

        if buf:
            if mode == "code":
                out.append(
                    ChatSegment(
                        kind="editor",
                        language=ChatWindow._normalize_language(fence_language)
                        or (pending_file_context.language if pending_file_context else ""),
                        block="\n".join(buf).rstrip("\n"),
                        file_path=pending_file_context.file_path if pending_file_context else "",
                    )
                )
            else:
                plain_segments, _ = ChatWindow._split_plain_segment("\n".join(buf), allow_file_context=False)
                out.extend(plain_segments)
        return out

    @classmethod
    def _split_plain_segment(
        cls,
        raw_block: str,
        *,
        allow_file_context: bool,
    ) -> tuple[list[ChatSegment], ChatFileContext | None]:
        normalized = str(raw_block or "").strip("\n")
        if not normalized.strip():
            return [], None

        lines = normalized.splitlines()
        file_context = cls._parse_file_context(lines)
        if file_context is not None:
            body = "\n".join(lines[file_context.body_start_index:]).strip("\n")
            segments: list[ChatSegment] = [ChatSegment(kind="text", language="", block=file_context.header_line)]
            if body:
                segments.append(
                    ChatSegment(
                        kind="editor",
                        
                        language=file_context.language,
                        block=body,
                        file_path=file_context.file_path,
                    )
                )
                return segments, None
            return segments, file_context if allow_file_context else None

        language = cls._infer_plain_block_language(normalized)
        if cls._should_use_editor(normalized, language):
            return [ChatSegment(kind="editor", language=language, block=normalized)], None
        return [ChatSegment(kind="text", language="", block=normalized)], None

    @classmethod
    def _parse_file_context(cls, lines: list[str]) -> ChatFileContext | None:
        if not lines:
            return None

        header_line = lines[0].strip()
        header_match = cls._FILE_HEADER_PATTERN.match(header_line)
        if header_match is None:
            return None

        body_start_index = 1
        file_path = ""
        if len(lines) > 1:
            source_match = cls._SOURCE_HEADER_PATTERN.match(lines[1].strip())
            if source_match is not None:
                file_path = source_match.group("path").strip()
                body_start_index = 2

        language = cls._language_from_file_header(
            file_name=header_match.group("name"),
            object_kind=header_match.group("kind"),
        )
        return ChatFileContext(
            header_line=header_line,
            language=language,
            file_path=file_path,
            body_start_index=body_start_index,
        )

    @classmethod
    def _normalize_language(cls, language: str | None) -> str:
        return CodeViewer._normalize_language(language)

    @classmethod
    def _language_from_file_header(cls, *, file_name: str, object_kind: str) -> str:
        normalized_kind = str(object_kind or "").strip().lower()
        if normalized_kind == "markdown":
            return "markdown"
        if normalized_kind in {"pdf", "text"}:
            return "text"
        suffix = Path(str(file_name or "").strip()).suffix.lower()
        return cls._normalize_language(cls._LANGUAGE_BY_SUFFIX.get(suffix, ""))

    @classmethod
    def _infer_plain_block_language(cls, block: str) -> str:
        lines = [line.rstrip() for line in str(block or "").splitlines() if line.strip()]
        if not lines:
            return ""

        stripped = "\n".join(lines).strip()
        if (stripped.startswith("{") or stripped.startswith("[")) and re.search(r'"[^"\\]+"\s*:', stripped):
            return "json"

        if any(re.match(r"^\s*\[[^\]]+\]\s*$", line) for line in lines) and any(
            re.match(r"^\s*[A-Za-z0-9_.-]+\s*=\s*.+$", line) for line in lines
        ):
            return "toml"

        yaml_hits = sum(1 for line in lines if re.match(r"^\s*[A-Za-z0-9_.-]+\s*:\s*.+$", line))
        if yaml_hits >= 2 or (yaml_hits >= 1 and any(re.match(r"^\s*-\s+.+$", line) for line in lines)):
            return "yaml"

        python_hits = sum(
            1
            for line in lines
            if re.match(r"^\s*(def|class|from|import|if|elif|else|for|while|try|except|with|return|async|await|yield|pass)\b", line)
        )
        if python_hits >= 2:
            return "python"

        js_hits = sum(
            1
            for line in lines
            if re.match(r"^\s*(const|let|var|function|export|import|interface|type)\b", line)
            or "=>" in line
        )
        if js_hits >= 2:
            return "javascript"

        if any(line.startswith("#!/") for line in lines):
            return "bash"

        if any(re.match(r"^\s*<[^>]+>\s*$", line) for line in lines) and any("</" in line for line in lines):
            return "html"

        return ""

    @classmethod
    def _should_use_editor(cls, block: str, language: str) -> bool:
        normalized_language = cls._normalize_language(language)
        if normalized_language and normalized_language != "markdown":
            return True
        if normalized_language == "markdown":
            return False

        lines = [line.rstrip() for line in str(block or "").splitlines() if line.strip()]
        if len(lines) < 4:
            return False

        structured_hits = sum(
            1
            for line in lines
            if re.match(r"^\s*([A-Za-z0-9_.-]+\s*[:=].+|Traceback|File \".+\")", line)
            or re.match(r"^\s*[{}\[\]<>].*$", line)
            or re.match(r"^\s*(#include|SELECT\b|INSERT\b|UPDATE\b|DELETE\b)", line, re.IGNORECASE)
        )
        sentence_hits = sum(1 for line in lines if re.search(r"[.!?]\s*$", line) and len(line.split()) > 4)
        blank_ratio = 1 - (len(lines) / max(len(str(block or "").splitlines()), 1))

        if structured_hits >= 2:
            return True
        if len(lines) >= 8 and sentence_hits * 2 < len(lines) and blank_ratio < 0.35:
            return True
        return False
        
# ───────────────────────────────────────────────────────────────
# PATCH: Mindesthöhe für QTextBrowser-Segmente im Chat
#        height = rows × font_height  + 5 px
# ───────────────────────────────────────────────────────────────
from PySide6.QtGui     import QFontMetrics
from PySide6.QtWidgets import QTextBrowser

def _autofit_browser(self, br: QTextBrowser) -> None:          # pylint: disable=unused-argument
    """
    Setzt eine **Mindesthöhe** für jedes Markdown-Segment im Chat:

        min_h =  (Zeilen × 2 × Fontsize) + margin_top + margin_bottom

    Enthält das Dokument (Bilder, Tabellen …) mehr Inhalt als der
    Zeilenzähler vermuten lässt, wird automatisch der größere Wert
    verwendet, so dass nichts abgeschnitten wird.
    """
    doc = br.document()
    doc.setDocumentMargin(0)

    w = max(1, br.viewport().width())
    doc.setTextWidth(w)

    h_doc = int(doc.size().height()) + 4
    font_h = QFontMetrics(br.font()).height()
    h_min = max(3,3 * font_h )

    br.setFixedHeight(max(h_doc, h_min))

# -- bestehende Klasse zur Laufzeit patchen -------------------------------
import types, inspect, sys

# MsgWidget befindet sich bereits im globalen Namespace des Hauptskripts
MsgWidget = next(                       # type: ignore  # noqa: N806
    obj for obj in globals().values()
    if inspect.isclass(obj) and obj.__name__ == "MsgWidget"
)

# Methode als ungebundene Funktion ersetzen (wird bei Aufruf korrekt an Instanz gebunden)
MsgWidget._fit_browser = _autofit_browser
'''
Kurzerklärung  
─────────────  
1. Ein robuster Ersatz-`__init__` für `QSHighlighter` sorgt dafür,  
   dass immer ein gültiges `QTextDocument` existiert und der
   Highlighter nicht doppelt initialisiert wird – dadurch funktioniert
   **jegliches Syntax-Highlighting** (ExplorerDock, CodeViewer, …) wieder.

2. `CodeViewer` berechnet seine Mindesthöhe nur aus der Zeilenzahl und
   nutzt `QSizePolicy.Expanding`.  Er stellt jetzt Quellcode in voller
   Breite mit korrektem Highlighting dar.

3. `MsgWidget` verwendet `QTextBrowser` mit `AdjustToContents`.
   Die Mindesthöhe wird automatisch ermittelt – dadurch werden Text-
   Nachrichten **vollständig** angezeigt (keine abges        except Exception as exc:
chnittenen Zeilen mehr).

4. Ein gepatchtes `ChatWindow` ersetzt die Originalklasse im laufenden
   Programm, ohne andere Teile der Anwendung zu verändern.

Der Patch erfordert keine weiteren Abhängigkeiten und kann jederzeit
wieder entfernt werden, um den Ursprungszustand herzustellen.
'''  



from PySide6.QtCore import Qt, QSize, QTimer, Slot
from PySide6.QtGui  import (QIcon, QTextOption, QTextCursor)
from PySide6.QtWidgets import (QMainWindow,
    QTreeWidget, QTreeWidgetItem,               #  NEU
    QDockWidget, QToolButton, QTextEdit,QWidget
)
import json
import typing as _t
from pathlib import Path
try:
    if __package__:
        from .litehigh import QSHighlighter  # type: ignore
    else:
        from alde.litehigh import QSHighlighter  # type: ignore
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from litehigh import QSHighlighter  # type: ignore
    else:
        raise
from PySide6.QtCore import (
     Qt,
     QSize,
     Signal,
     Slot,
     QTimer,
     QSettings,
     QByteArray,
     QRegularExpression,
     
     QRegularExpressionMatch,
 )

# -----------------------------------------------------------

class _SplitterToggleGlyph(QLabel):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = False
        self._hovered = False
        self._idle_color = "#9a9a9a"
        self._active_color = "#35ff8a"
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(16, 16)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setExpanded(False)

    def setColors(self, idle_color: str, active_color: str) -> None:
        if idle_color:
            self._idle_color = str(idle_color)
        if active_color:
            self._active_color = str(active_color)
        self._apply_style()

    def setExpanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.setText(DROPDOWN_EXPANDED_GLYPH if self._expanded else DROPDOWN_COLLAPSED_GLYPH)
        self._apply_style()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _apply_style(self) -> None:
        active = self._expanded
        color = self._active_color if active else self._idle_color
        self.setStyleSheet(
            f"background: transparent; border: none; color: {color}; font-size: 13px; font-weight: 700;"
        )


class _BoardCanvasView(QGraphicsView):
    CARD_MARGIN = 10.0
    CARD_GAP = 12.0
    CARD_WIDTH = 280.0
    CARD_HEIGHT = 132.0

    itemActivated = Signal(str)
    itemMoved = Signal(str, float, float)
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("boardCanvasView")
        self.setFrameShape(QFrame.NoFrame)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setScene(QGraphicsScene(self))
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._cards: list[dict[str, Any]] = []
        self._surface_color = "#0b0b0b"
        self._accent_color = "#3a5fff"
        self._text_color = "#E3E3DE"
        self._muted_color = "#9a9a95"
        self._card_item_groups: dict[str, list[Any]] = {}
        self._card_anchor_positions: dict[str, tuple[float, float]] = {}
        self._drag_item_id = ""
        self._drag_origin_anchor = QPointF()
        self._drag_press_scene_pos = QPointF()
        self._drag_origin_positions: list[tuple[Any, QPointF]] = []
        self._drag_moved = False
        self._apply_canvas_style()

    def set_cards(
        self,
        cards: Sequence[Mapping[str, Any]],
        *,
        surface_color: str,
        accent_color: str,
        text_color: str,
        muted_color: str,
    ) -> None:
        self._cards = [dict(card) for card in cards]
        self._surface_color = str(surface_color or self._surface_color)
        self._accent_color = str(accent_color or self._accent_color)
        self._text_color = str(text_color or self._text_color)
        self._muted_color = str(muted_color or self._muted_color)
        self._apply_canvas_style()
        self._render_cards()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._render_cards()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        point = event.position().toPoint() if hasattr(event, "position") else event.pos()
        item = self.itemAt(point)
        item_title = str(item.data(0) or "").strip() if item is not None else ""
        if item_title and event.button() == Qt.LeftButton:
            self._drag_item_id = item_title
            anchor_x, anchor_y = self._card_anchor_positions.get(item_title, (0.0, 0.0))
            self._drag_origin_anchor = QPointF(anchor_x, anchor_y)
            self._drag_press_scene_pos = self.mapToScene(point)
            self._drag_origin_positions = [
                (graphic_item, QPointF(graphic_item.pos()))
                for graphic_item in self._card_item_groups.get(item_title, [])
            ]
            self._drag_moved = False
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_item_id and bool(event.buttons() & Qt.LeftButton):
            point = event.position().toPoint() if hasattr(event, "position") else event.pos()
            scene_point = self.mapToScene(point)
            delta = scene_point - self._drag_press_scene_pos
            if not self._drag_moved:
                travel = abs(delta.x()) + abs(delta.y())
                if travel < float(QApplication.startDragDistance()):
                    event.accept()
                    return
                self._drag_moved = True

            clamped_delta_x = max(-self._drag_origin_anchor.x(), float(delta.x()))
            clamped_delta_y = max(-self._drag_origin_anchor.y(), float(delta.y()))
            delta_offset = QPointF(clamped_delta_x, clamped_delta_y)
            for graphic_item, origin in self._drag_origin_positions:
                graphic_item.setPos(origin + delta_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_item_id and event.button() == Qt.LeftButton:
            point = event.position().toPoint() if hasattr(event, "position") else event.pos()
            scene_point = self.mapToScene(point)
            delta = scene_point - self._drag_press_scene_pos
            clamped_delta_x = max(-self._drag_origin_anchor.x(), float(delta.x()))
            clamped_delta_y = max(-self._drag_origin_anchor.y(), float(delta.y()))
            dragged_item_id = self._drag_item_id
            moved = self._drag_moved and (abs(clamped_delta_x) > 0.01 or abs(clamped_delta_y) > 0.01)
            new_x = max(0.0, self._drag_origin_anchor.x() + clamped_delta_x)
            new_y = max(0.0, self._drag_origin_anchor.y() + clamped_delta_y)
            self._reset_drag_state()
            if moved:
                self.itemMoved.emit(dragged_item_id, float(new_x), float(new_y))
            else:
                self.itemActivated.emit(dragged_item_id)
            event.accept()
            return
        self._reset_drag_state()
        super().mouseReleaseEvent(event)

    def _apply_canvas_style(self) -> None:
        self.setStyleSheet(
            """
            QGraphicsView#boardCanvasView {
                border: 1px solid #303030;
                border-radius: 14px;
                background: transparent;
            }
            """
        )
        self.setBackgroundBrush(QColor(self._surface_color))

    def _reset_drag_state(self) -> None:
        self._drag_item_id = ""
        self._drag_origin_anchor = QPointF()
        self._drag_press_scene_pos = QPointF()
        self._drag_origin_positions = []
        self._drag_moved = False
        self.viewport().unsetCursor()

    @staticmethod
    def _coordinate_value(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except Exception:
            return float(fallback)

    def _render_cards(self) -> None:
        scene = self.scene()
        if not isinstance(scene, QGraphicsScene):
            return

        self._reset_drag_state()
        self._card_item_groups = {}
        self._card_anchor_positions = {}
        scene.clear()
        cards = list(self._cards)
        if not cards:
            placeholder = scene.addText("Board canvas is waiting for sections.")
            placeholder.setDefaultTextColor(QColor(self._muted_color))
            placeholder.setPos(12, 12)
            scene.setSceneRect(0, 0, max(280, self.viewport().width()), 72)
            return

        margin = self.CARD_MARGIN
        gap = self.CARD_GAP
        card_width = self.CARD_WIDTH
        card_height = self.CARD_HEIGHT
        available_width = max(float(self.viewport().width()) - (margin * 2.0), card_width)
        column_count = max(1, int(max(1.0, (available_width + gap) // (card_width + gap))))
        card_y_positions = [margin for _ in range(column_count)]
        max_right = margin
        max_bottom = margin

        for index, card in enumerate(cards):
            column_index = index % column_count
            default_x = margin + column_index * (card_width + gap)
            default_y = card_y_positions[column_index]
            x = self._coordinate_value(card.get("x"), default_x)
            y = self._coordinate_value(card.get("y"), default_y)
            card_y_positions[column_index] = max(card_y_positions[column_index], y + card_height + gap)

            element_id = str(card.get("id") or "").strip() or str(card.get("title") or "Section")
            title = str(card.get("title") or "Section").strip() or "Section"
            preview = str(card.get("preview") or "").strip()
            action_text = str(card.get("action_text") or f"Open {title}").strip() or f"Open {title}"
            selected = bool(card.get("selected"))

            border_color = QColor(self._accent_color if selected else self._muted_color)
            fill_color = QColor(self._surface_color)
            fill_color.setAlpha(245)
            if selected:
                fill_color = QColor(self._accent_color)
                fill_color.setAlpha(40)

            rect_item = scene.addRect(
                x,
                y,
                card_width,
                card_height,
                QPen(border_color, 2 if selected else 1),
                QBrush(fill_color),
            )
            rect_item.setData(0, element_id)

            title_item = scene.addText(title)
            title_item.setDefaultTextColor(QColor(self._text_color))
            title_item.setTextWidth(card_width - 28)
            title_item.setPos(x + 14, y + 10)
            title_item.setData(0, element_id)

            preview_item = scene.addText(preview or "Board surface ready.")
            preview_item.setDefaultTextColor(QColor(self._muted_color))
            preview_item.setTextWidth(card_width - 28)
            preview_item.setPos(x + 14, y + 38)
            preview_item.setData(0, element_id)

            action_item = scene.addText(action_text)
            action_item.setDefaultTextColor(QColor(self._accent_color if selected else self._text_color))
            action_item.setTextWidth(card_width - 28)
            action_item.setPos(x + 14, y + card_height - 30)
            action_item.setData(0, element_id)

            self._card_item_groups[element_id] = [rect_item, title_item, preview_item, action_item]
            self._card_anchor_positions[element_id] = (float(x), float(y))
            max_right = max(max_right, float(x + card_width))
            max_bottom = max(max_bottom, float(y + card_height))

        scene_width = max(max_right + margin, float(self.viewport().width()))
        scene_height = max(max_bottom + margin, max(card_y_positions) + margin - gap, 72.0)
        scene.setSceneRect(0, 0, scene_width, scene_height)

class ControlPlaneWidget(QWidget):
    snapshotChanged = Signal(dict)
    _operator_async_result_ready = Signal(object)
    _refresh_async_result_ready = Signal(object)
    _OPERATOR_FILTER_SETTINGS_PREFIX = "ControlPlane/OperatorFilters"
    _RUNTIME_LAYOUT_SETTINGS_PATH_KEY = "controlPlaneRuntimeLayoutPath"
    _RUNTIME_LAYOUT_DEFAULT_REL_PATH = "AppData/control_plane_runtime_tabs.json"
    _RUNTIME_LAYOUT_SCHEMA = 1
    _PRIMARY_BOARD_TAB_LABEL = "Board 1"
    _BOARD_ITEM_WIDGET_KIND = "board_item"
    _EXTENSIONS_WORKSPACE_WIDGET_KIND = "extensions_workspace"
    _EXTENSIONS_RUNTIME_TOOL_ID = "web_app"
    _BUILD_RUNTIME_TAB_LABEL = "<Build>"
    _LEGACY_BUILD_RUNTIME_SLASH_LABEL = "</Build>"
    _LEGACY_BUILD_RUNTIME_TAB_LABEL = "Builder"

    def _resolve_auto_refresh_interval_ms(self) -> int:
        raw_value = str(os.getenv("AI_IDE_CONTROL_PLANE_REFRESH_MS", "0") or "0").strip()
        try:
            resolved_value = int(raw_value)
        except Exception:
            resolved_value = 0
        return max(0, resolved_value)

    def _auto_refresh_hint_text(self) -> str:
        interval_ms = self._resolve_auto_refresh_interval_ms()
        if interval_ms <= 0:
            return "Auto refresh: off"
        if interval_ms % 1000 == 0:
            return f"Auto refresh: {interval_ms // 1000}s"
        return f"Auto refresh: {interval_ms / 1000.0:.1f}s"

    def __init__(self, accent: dict[str, str], base: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._accent = accent
        self._base = base
        self.scheme = _build_scheme(accent, base)
        self._metric_labels: dict[str, QLabel] = {}
        self._last_snapshot: dict[str, Any] = {}
        self._operator_log_entries: list[dict[str, Any] | str] = []
        self._active_operator_tasks: set[str] = set()
        self._operator_filter_preferences = self._load_operator_filter_preferences()
        self._agent_rows_by_label: dict[str, dict[str, Any]] = {}
        self._runtime_tab_counter = 0
        self._runtime_tab_records: dict[QWidget, dict[str, Any]] = {}
        self._board_contexts: list[dict[str, Any]] = []
        self._board_context_by_tab: dict[QWidget, dict[str, Any]] = {}
        self._primary_board_context: dict[str, Any] | None = None
        self._builder_runtime_tab: QWidget | None = None
        self._runtime_restore_active = False
        self._runtime_state_last_saved_payload = ""
        self._runtime_layout_path = self._resolve_runtime_layout_path()
        self._runtime_state_save_timer = QTimer(self)
        self._runtime_state_save_timer.setSingleShot(True)
        self._runtime_state_save_timer.setInterval(900)
        self._runtime_state_save_timer.timeout.connect(self.persist_runtime_tabs_state)
        self._operator_async_result_ready.connect(self._handle_operator_async_result)
        self._refresh_async_result_ready.connect(self._handle_refresh_async_result)
        self._refresh_inflight = False
        self._refresh_pending = False
        self._refresh_pending_include_drilldown = False
        self._tab_bar_label_max_chars = 8
        self._control_tab_hover_index = -1
        self._control_tab_hover_base_text = ""
        self._control_tab_hover_phase = 0
        self._control_tab_hover_marquee_timer = QTimer(self)
        self._control_tab_hover_marquee_timer.setInterval(160)
        self._control_tab_hover_marquee_timer.timeout.connect(self._tick_control_plane_tab_hover_marquee)
        self._last_selected_board_item_title = ""
        self._control_tab_corner_widget: QWidget | None = None
        self._control_tab_corner_add_button: QToolButton | None = None
        self._control_tab_corner_menu: QMenu | None = None
        self._build_ui()
        self.update_scheme(accent, base)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_view)
        refresh_interval_ms = self._resolve_auto_refresh_interval_ms()
        if refresh_interval_ms > 0:
            self._refresh_timer.setInterval(refresh_interval_ms)
            self._refresh_timer.start()
        self.refresh_view()

    def set_external_tabs(self, tabs_widget: Any) -> None:
        """Set an external QTabWidget to use instead of the internal tabs.
        
        This is used when ControlPlaneWidget tabs should be displayed in a parent container.
        The internal self.tabs will be replaced with the external widget, so all future tab
        operations (add/remove) use the external widget.
        """
        if tabs_widget is None or not isinstance(tabs_widget, QTabWidget):
            return
        
        self._stop_control_plane_tab_hover_marquee()

        # Replace the internal tabs with the external one
        # We don't need to remove the old tabs - they'll be replaced
        self.tabs = tabs_widget
        self._apply_control_plane_tab_text_limits()
        self._setup_control_plane_tab_bar_interactions()

    def _external_extensions_tab_text_setter(self):
        tabs_widget = getattr(self, "tabs", None)
        if not isinstance(tabs_widget, QTabWidget):
            return None
        if str(tabs_widget.objectName() or "") != "extensionsTabs":
            return None

        current_parent = tabs_widget.parentWidget()
        while current_parent is not None:
            setter = getattr(current_parent, "_set_extensions_tab_text", None)
            if callable(setter):
                return setter
            current_parent = current_parent.parentWidget()
        return None

    def _board_context_attribute_names(self) -> tuple[str, ...]:
        return (
            "config_summary_view",
            "config_manifest_view",
            "monitor_summary_view",
            "monitor_filter_panel",
            "agent_selector",
            "workflow_selector",
            "trace_agent_selector",
            "trace_workflow_selector",
            "trace_tool_selector",
            "trace_handoff_selector",
            "tree_stream_panel",
            "tree_stream_transport_value",
            "tree_stream_state_value",
            "tree_stream_event_value",
            "tree_stream_retry_value",
            "tree_stream_updated_value",
            "tree_stream_error_value",
            "board_explorer_tree_panel",
            "board_explorer_tree_widget",
            "board_explorer_tree_status_label",
            "board_canvas_panel",
            "board_canvas_view",
            "board_canvas_status_label",
            "board_canvas_elements",
            "board_canvas_selected_element_id",
            "btn_refresh_detail",
            "monitor_detail_view",
            "monitor_timeline_view",
            "monitor_trace_view",
            "config_monitor_scroll_area",
            "config_monitor_scroll_content",
            "config_monitor_host_widget",
            "config_monitor_host_splitter",
            "config_monitor_host_splitter_toggle",
            "config_monitor_host_splitter_label",
            "config_monitor_splitter",
            "config_monitor_splitter_anchor",
            "config_monitor_sections",
            "_config_monitor_section_state",
            "_config_monitor_splitter_handle_controls",
            "config_monitor_threat_flow_panel",
            "config_monitor_threat_flow_view",
            "_config_monitor_host_threat_flow_size",
            "operator_actions_panel",
            "btn_refresh_health",
            "btn_probe_queue",
            "btn_probe_agentsdb",
            "btn_probe_dispatcher",
            "btn_repair_dispatcher",
            "btn_probe_mcp",
            "btn_export_runtime",
            "operator_filters_panel",
            "operator_status_selector",
            "operator_audit_selector",
            "operator_group_selector",
            "operator_source_selector",
            "operator_summary_view",
            "operator_log_view",
            "build_section_panel",
            "extensions_section_panel",
            "extensions_section_workspace",
        )

    def _capture_current_board_context(self, tab_widget: QWidget) -> dict[str, Any]:
        context = {
            name: getattr(self, name, None)
            for name in self._board_context_attribute_names()
        }
        context["tab_widget"] = tab_widget
        return context

    @contextmanager
    def _board_context_scope(self, board_context: dict[str, Any] | None):
        if not isinstance(board_context, dict):
            yield
            return

        marker = object()
        saved: dict[str, Any] = {}
        for name in self._board_context_attribute_names():
            saved[name] = getattr(self, name, marker)
            setattr(self, name, board_context.get(name))
        try:
            yield
        finally:
            for name, value in saved.items():
                if value is marker:
                    try:
                        delattr(self, name)
                    except AttributeError:
                        pass
                else:
                    setattr(self, name, value)

    def _register_board_context(self, tab_widget: QWidget, board_context: dict[str, Any], *, primary: bool = False) -> None:
        board_context["tab_widget"] = tab_widget
        self._board_context_by_tab[tab_widget] = board_context
        if board_context not in self._board_contexts:
            self._board_contexts.append(board_context)
        if primary or self._primary_board_context is None:
            self._primary_board_context = board_context

    def _unregister_board_context(self, tab_widget: QWidget, *, persist: bool = True) -> None:
        board_context = self._board_context_by_tab.pop(tab_widget, None)
        if board_context is None:
            return
        if board_context in self._board_contexts:
            self._board_contexts.remove(board_context)
        if self._primary_board_context is board_context:
            self._primary_board_context = self._board_contexts[0] if self._board_contexts else None
        if persist:
            self._schedule_runtime_state_save()

    def _board_contexts_in_display_order(self) -> list[dict[str, Any]]:
        ordered_contexts: list[dict[str, Any]] = []
        for index in range(self.tabs.count()):
            tab_widget = self.tabs.widget(index)
            board_context = self._board_context_by_tab.get(tab_widget)
            if board_context is not None:
                ordered_contexts.append(board_context)
        return ordered_contexts

    def _active_board_context(self) -> dict[str, Any] | None:
        board_context = self._board_context_by_tab.get(self.tabs.currentWidget())
        if board_context is not None:
            return board_context
        return self._primary_board_context

    def _board_context_from_object(self, widget: Any) -> dict[str, Any] | None:
        current = widget if isinstance(widget, QWidget) else None
        while current is not None:
            board_context = self._board_context_by_tab.get(current)
            if board_context is not None:
                return board_context
            current = current.parentWidget()
        return None

    def _format_control_plane_tab_text(self, value: str) -> str:
        text = str(value or "")
        external_setter = self._external_extensions_tab_text_setter()
        if callable(external_setter):
            return text
        max_chars_raw = getattr(self, "_tab_bar_label_max_chars", 10)
        try:
            max_chars = int(max_chars_raw)
        except Exception:
            max_chars = 10

        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars]

    def _control_plane_tab_full_text(self, index: int) -> str:
        if not hasattr(self, "tabs"):
            return ""
        if index < 0 or index >= self.tabs.count():
            return ""

        tab_bar = self.tabs.tabBar()
        tab_data = tab_bar.tabData(index) if isinstance(tab_bar, QTabBar) else None
        if isinstance(tab_data, str) and tab_data:
            return tab_data
        return self.tabs.tabText(index)

    def _set_control_plane_tab_text(self, index: int, value: str) -> None:
        if not hasattr(self, "tabs"):
            return
        if index < 0 or index >= self.tabs.count():
            return

        full_text = str(value or "")
        tab_bar = self.tabs.tabBar()
        if isinstance(tab_bar, QTabBar):
            tab_bar.setTabData(index, full_text)

        external_setter = self._external_extensions_tab_text_setter()
        if callable(external_setter) and isinstance(tab_bar, QTabBar):
            external_setter(tab_bar, index, full_text)
            return

        self.tabs.setTabToolTip(index, "")
        self.tabs.setTabText(index, self._format_control_plane_tab_text(full_text))

    def _apply_control_plane_tab_text_limits(self) -> None:
        if not hasattr(self, "tabs"):
            return
        for index in range(self.tabs.count()):
            self._set_control_plane_tab_text(index, self._control_plane_tab_full_text(index))

    def _setup_control_plane_tab_bar_interactions(self) -> None:
        if not hasattr(self, "tabs"):
            return
        tab_bar = self.tabs.tabBar()
        if not isinstance(tab_bar, QTabBar):
            return
        external_setter = self._external_extensions_tab_text_setter()
        if callable(external_setter):
            try:
                tab_bar.removeEventFilter(self)
            except Exception:
                pass
            self._clear_control_plane_tab_corner_widget()
            return
        tab_bar.setMouseTracking(True)
        tab_bar.installEventFilter(self)
        self._clear_control_plane_tab_corner_widget()

    def _start_control_plane_tab_hover_marquee(self, tab_index: int) -> None:
        self._stop_control_plane_tab_hover_marquee()
        if not hasattr(self, "tabs"):
            return
        tab_bar = self.tabs.tabBar()
        if tab_index < 0 or tab_index >= tab_bar.count():
            self._stop_control_plane_tab_hover_marquee()
            return
        if tab_index == self._control_tab_hover_index:
            return

        self._stop_control_plane_tab_hover_marquee()

        base_text = self._control_plane_tab_full_text(tab_index)
        if not base_text:
            return

        tab_rect = tab_bar.tabRect(tab_index)
        available_width = max(tab_rect.width() - 20, 18)
        text_width = tab_bar.fontMetrics().horizontalAdvance(base_text)
        if text_width <= available_width:
            return

        self._control_tab_hover_index = tab_index
        self._control_tab_hover_base_text = base_text
        self._control_tab_hover_phase = 0
        self._control_tab_hover_marquee_timer.start()

    def _tick_control_plane_tab_hover_marquee(self) -> None:
        if not hasattr(self, "tabs"):
            self._stop_control_plane_tab_hover_marquee()
            return
        tab_bar = self.tabs.tabBar()
        tab_index = self._control_tab_hover_index
        if tab_index < 0 or tab_index >= tab_bar.count():
            self._stop_control_plane_tab_hover_marquee()
            return
        if not self._control_tab_hover_base_text:
            self._stop_control_plane_tab_hover_marquee()
            return

        cycle_text = f"{self._control_tab_hover_base_text}   "
        if len(cycle_text) <= 1:
            return

        self._control_tab_hover_phase = (self._control_tab_hover_phase + 1) % len(cycle_text)
        shift = self._control_tab_hover_phase
        tab_bar.setTabText(tab_index, f"{cycle_text[shift:]}{cycle_text[:shift]}")

    def _stop_control_plane_tab_hover_marquee(self) -> None:
        if self._control_tab_hover_marquee_timer.isActive():
            self._control_tab_hover_marquee_timer.stop()

        tab_index = self._control_tab_hover_index
        if tab_index >= 0 and self._control_tab_hover_base_text:
            self._set_control_plane_tab_text(tab_index, self._control_tab_hover_base_text)

        self._control_tab_hover_index = -1
        self._control_tab_hover_base_text = ""
        self._control_tab_hover_phase = 0

    def _apply_control_plane_tab_corner_widget_style(self) -> None:
        button = getattr(self, "_control_tab_corner_add_button", None)
        if not isinstance(button, QToolButton):
            return
        button.setStyleSheet(
            f"""
            QToolButton#controlTabCornerAddButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                padding: 3px 5px;
                color: {self.scheme.get('col6', '#E3E3DE')};
            }}
            QToolButton#controlTabCornerAddButton:hover {{
                background: rgba(255, 255, 255, 0.08);
                border: none;
            }}
            QToolButton#controlTabCornerAddButton:pressed {{
                background: rgba(255, 255, 255, 0.12);
                border: none;
            }}
            """
        )

    def _clear_control_plane_tab_corner_widget(self) -> None:
        tabs_widget = getattr(self, "tabs", None)
        corner_widget = getattr(self, "_control_tab_corner_widget", None)
        parent_tabs = corner_widget.parentWidget() if isinstance(corner_widget, QWidget) else None
        if isinstance(parent_tabs, QTabWidget):
            parent_tabs.setCornerWidget(None, Qt.TopRightCorner)
        elif isinstance(tabs_widget, QTabWidget):
            tabs_widget.setCornerWidget(None, Qt.TopRightCorner)

        if isinstance(corner_widget, QWidget):
            corner_widget.setParent(None)
            corner_widget.deleteLater()

        self._control_tab_corner_widget = None
        self._control_tab_corner_add_button = None
        self._control_tab_corner_menu = None

    def _primary_board_item_titles(self) -> list[str]:
        primary_board = self._primary_board_context
        if not isinstance(primary_board, dict):
            return []
        with self._board_context_scope(primary_board):
            sections = getattr(self, "config_monitor_sections", None)
            if not isinstance(sections, list):
                return []

            titles: list[str] = []
            seen: set[str] = set()
            for section in sections:
                state = self._config_monitor_section_state_for(section)
                if not isinstance(state, dict):
                    continue
                title = str(state.get("title") or "").strip()
                normalized = title.lower()
                if not title or normalized in seen:
                    continue
                seen.add(normalized)
                titles.append(title)
            return titles

    def _board_section_for_title(self, item_title: str) -> QFrame | None:
        normalized_title = str(item_title or "").strip().lower()
        if not normalized_title:
            return None

        sections = getattr(self, "config_monitor_sections", None)
        if not isinstance(sections, list):
            return None

        for section in sections:
            state = self._config_monitor_section_state_for(section)
            if not isinstance(state, dict):
                continue
            if str(state.get("title") or "").strip().lower() == normalized_title and isinstance(section, QFrame):
                return section
        return None

    def _primary_board_section_for_title(self, item_title: str) -> QFrame | None:
        primary_board = self._primary_board_context
        if not isinstance(primary_board, dict):
            return None
        with self._board_context_scope(primary_board):
            return self._board_section_for_title(item_title)

    def _primary_board_source_widget_for_title(self, item_title: str) -> QWidget | None:
        section = self._primary_board_section_for_title(item_title)
        if section is None:
            return None
        state = self._config_monitor_section_state_for(section)
        if not isinstance(state, dict):
            return None
        content_widget = state.get("content_widget")
        if isinstance(content_widget, QWidget):
            return content_widget
        return None

    def _board_item_preview_text(self, item_title: str) -> str:
        source_widget = self._primary_board_source_widget_for_title(item_title)
        preview_browser = None
        if isinstance(source_widget, QTextBrowser):
            preview_browser = source_widget
        elif isinstance(source_widget, QWidget):
            preview_browser = source_widget.findChild(QTextBrowser)

        if isinstance(preview_browser, QTextBrowser):
            preview_text = " ".join(preview_browser.toPlainText().split())
            if preview_text:
                if len(preview_text) > 220:
                    return preview_text[:217].rstrip() + "..."
                return preview_text

        fallback_previews = {
            "Monitoring Filters": "Agent, workflow, trace, and handoff filters from the master board.",
            "Operator Actions": "Shortcut to the operator actions panel on the master board.",
            "Operator Filters": "Shortcut to the operator filters on the master board.",
            "Build": "Independent build surface added to this board.",
            "Extensions": "Independent extensions workspace added to this board.",
        }
        return fallback_previews.get(
            str(item_title or "").strip(),
            f"Board-owned snapshot copied from {self._PRIMARY_BOARD_TAB_LABEL}.",
        )

    def _board_item_snapshot_html(self, item_title: str) -> str:
        source_widget = self._primary_board_source_widget_for_title(item_title)
        if isinstance(source_widget, QTextBrowser):
            html_text = str(source_widget.toHtml() or "").strip()
            if html_text:
                return html_text

        lines = [
            f"<h3>{html.escape(str(item_title or 'Item'))}</h3>",
            f"<p>Snapshot copied from <b>{html.escape(self._PRIMARY_BOARD_TAB_LABEL)}</b>.</p>",
        ]

        if isinstance(source_widget, QWidget):
            combo_values = []
            for combo in source_widget.findChildren(QComboBox):
                combo_text = str(combo.currentText() or "").strip()
                if combo_text:
                    combo_values.append(combo_text)
            if combo_values:
                lines.append("<p><b>Current selections</b></p><ul>")
                lines.extend(f"<li>{html.escape(value)}</li>" for value in combo_values)
                lines.append("</ul>")

            button_values = []
            button_widgets = list(source_widget.findChildren(QPushButton))
            button_widgets.extend(source_widget.findChildren(QToolButton))
            for button in button_widgets:
                label = str(button.toolTip() or button.text() or "").strip()
                if label:
                    button_values.append(label)
            if button_values:
                lines.append("<p><b>Available actions</b></p><ul>")
                lines.extend(f"<li>{html.escape(value)}</li>" for value in button_values[:8])
                lines.append("</ul>")

        preview_text = self._board_item_preview_text(item_title)
        if preview_text:
            lines.append(f"<p>{html.escape(preview_text)}</p>")
        return "".join(lines)

    def _focus_board_section_in_context(
        self,
        board_context: dict[str, Any] | None,
        item_title: str,
        *,
        activate_tab: bool = False,
    ) -> bool:
        if not hasattr(self, "tabs") or not isinstance(board_context, dict):
            return False

        tab_widget = board_context.get("tab_widget")
        if activate_tab and isinstance(tab_widget, QWidget):
            tab_index = self.tabs.indexOf(tab_widget)
            if tab_index >= 0:
                self.tabs.setCurrentIndex(tab_index)

        with self._board_context_scope(board_context):
            section = self._board_section_for_title(item_title)
            if section is None:
                return False

            self._set_config_monitor_section_expanded(section, True)
            scroll_area = getattr(self, "config_monitor_scroll_area", None)
            if isinstance(scroll_area, QScrollArea):
                QTimer.singleShot(
                    0,
                    lambda target_section=section, area=scroll_area: area.ensureWidgetVisible(target_section, 0, 18),
                )
        return True

    def _focus_primary_board_item(self, item_title: str) -> None:
        if not hasattr(self, "tabs"):
            return

        resolved_title = str(item_title or "").strip()
        if resolved_title:
            self._last_selected_board_item_title = resolved_title

        primary_board = self._primary_board_context
        if not isinstance(primary_board, dict):
            return
        primary_board["board_canvas_selected_element_id"] = self._board_canvas_element_id("board", resolved_title)
        self._focus_board_section_in_context(primary_board, resolved_title, activate_tab=True)
        self._schedule_runtime_state_save()
        self._render_all_board_canvas_surfaces()

    def _next_board_tab_name(self) -> str:
        if not hasattr(self, "tabs"):
            return "Board 2"

        max_number = 1
        for index in range(self.tabs.count()):
            tab_name = self._control_plane_tab_full_text(index).strip()
            match = re.fullmatch(r"Board(?:\s+(\d+))?", tab_name, re.IGNORECASE)
            if match is None:
                continue
            try:
                number = int(match.group(1) or "1")
            except Exception:
                number = 1
            max_number = max(max_number, number)
        return f"Board {max_number + 1}"

    def _is_board_runtime_tab(self, tab_widget: QWidget | None) -> bool:
        if not isinstance(tab_widget, QWidget):
            return False
        if tab_widget in self._board_context_by_tab:
            return True
        if tab_widget not in self._runtime_tab_records:
            return False
        return str(tab_widget.property("runtime_role") or "").strip().lower() == "board"

    def _active_board_runtime_tab(self) -> QWidget | None:
        if not hasattr(self, "tabs"):
            return None
        current_widget = self.tabs.currentWidget()
        if self._is_board_runtime_tab(current_widget):
            return current_widget
        return None

    def _create_board_runtime_tab(
        self,
        *,
        activate: bool = True,
        board_title: str | None = None,
        persist: bool = True,
    ) -> QWidget:
        board_title = str(board_title or "").strip() or self._next_board_tab_name()
        board_tab, board_context = self._create_board_page(board_title)
        board_index = self.tabs.addTab(board_tab, self._format_control_plane_tab_text(board_title))
        board_tab.setProperty("fullTabText", board_title)
        self._register_board_context(board_tab, board_context)
        if activate:
            self.tabs.setCurrentIndex(board_index)
        self._render_board_context(board_context, include_drilldown=True)
        if persist:
            self._schedule_runtime_state_save()
        return board_tab

    def _dispose_board_tab(self, tab_widget: QWidget, *, persist: bool = True) -> None:
        primary_board_tab = getattr(self, "_config_tab", None)
        if not isinstance(tab_widget, QWidget) or tab_widget is primary_board_tab:
            return
        index = self.tabs.indexOf(tab_widget)
        if index >= 0:
            self.tabs.removeTab(index)
        self._unregister_board_context(tab_widget, persist=False)
        tab_widget.setParent(None)
        tab_widget.deleteLater()
        if persist:
            self._schedule_runtime_state_save()

    def _ensure_board_runtime_target(self, *, activate: bool = True) -> QWidget:
        active_board = self._active_board_runtime_tab()
        if active_board is not None:
            if activate:
                active_index = self.tabs.indexOf(active_board)
                if active_index >= 0:
                    self.tabs.setCurrentIndex(active_index)
            return active_board
        return self._create_board_runtime_tab(activate=activate)

    def _create_board_browser(self, parent: QWidget) -> QTextBrowser:
        browser = QTextBrowser(parent)
        browser.setObjectName("controlBrowser")
        browser.setOpenExternalLinks(False)
        browser.setMinimumHeight(0)
        browser.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        return browser

    def _create_board_selector(self, parent: QWidget, slot: Any) -> QComboBox:
        selector = QComboBox(parent)
        selector.setObjectName("controlSelector")
        selector.setMinimumContentsLength(10)
        selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        selector.currentTextChanged.connect(slot)
        return selector

    def _create_board_tree_stream_panel(self, parent: QWidget) -> tuple[QFrame, dict[str, QLabel]]:
        panel = QFrame(parent)
        panel.setObjectName("controlPanel")
        panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        title = QLabel("<b>Explorer Tree Stream</b>", panel)
        title.setObjectName("controlMeta")
        layout.addWidget(title)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        values: dict[str, QLabel] = {}
        for field_label, key, default_text in (
            ("Transport", "transport", "n/a"),
            ("State", "state", "n/a"),
            ("Cursor", "event", "n/a"),
            ("Reconnect", "retry", "n/a"),
            ("Updated", "updated", "n/a"),
            ("Last Error", "error", "none"),
        ):
            label = QLabel(field_label, panel)
            label.setObjectName("controlMeta")
            value = QLabel(default_text, panel)
            value.setObjectName("controlMetricLabel")
            value.setWordWrap(True)
            form.addRow(label, value)
            values[key] = value
        return panel, values

    def _create_board_explorer_tree_panel(self, parent: QWidget) -> tuple[QWidget, QTreeWidget, QLabel]:
        panel = QWidget(parent)
        panel.setObjectName("controlPanel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = QLabel("<b>Explorer Tree</b>", panel)
        title.setObjectName("controlMeta")
        layout.addWidget(title)

        status_label = QLabel("Waiting for the shared explorer tree.", panel)
        status_label.setObjectName("controlMeta")
        status_label.setWordWrap(True)
        layout.addWidget(status_label)

        tree_widget = QTreeWidget(panel)
        tree_widget.setObjectName("boardExplorerTree")
        tree_widget.setHeaderHidden(True)
        tree_widget.setUniformRowHeights(False)
        tree_widget.setAnimated(False)
        tree_widget.setIndentation(0)
        tree_widget.setMinimumHeight(180)
        tree_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        tree_widget.itemDoubleClicked.connect(self._handle_board_explorer_tree_item_double_clicked)
        tree_widget.itemExpanded.connect(self._handle_board_explorer_tree_item_expanded)
        tree_widget.itemCollapsed.connect(self._handle_board_explorer_tree_item_collapsed)
        layout.addWidget(tree_widget, 1)
        return panel, tree_widget, status_label

    def _board_explorer_source_tree(self) -> QTreeWidget | None:
        window = self.window()
        explorer = getattr(window, "explorer", None) if window is not None else None
        if explorer is None:
            return None
        tree_widget = getattr(explorer, "tree", explorer)
        if isinstance(tree_widget, QTreeWidget):
            return tree_widget
        return None

    def _clone_board_explorer_tree_item(
        self,
        source_item: QTreeWidgetItem,
        *,
        parent_path: Sequence[str] | None = None,
    ) -> QTreeWidgetItem:
        item_text = str(source_item.text(0) or "").strip()
        path_segments = [segment for segment in (parent_path or ()) if str(segment or "").strip()]
        if item_text:
            path_segments.append(item_text)
        item_path = " / ".join(path_segments)
        child_count = int(source_item.childCount())
        tooltip_text = str(source_item.toolTip(0) or "").strip()
        preview_text = tooltip_text or (
            f"Explorer tree node with {child_count} child entries." if child_count > 0 else f"Explorer tree leaf from {item_path or item_text or 'Explorer'}"
        )

        clone_item = QTreeWidgetItem([source_item.text(0)])
        clone_item.setFlags(source_item.flags() & ~Qt.ItemIsEditable)
        clone_item.setData(0, Qt.DecorationRole, None)
        clone_item.setForeground(0, source_item.foreground(0))
        clone_item.setFont(0, source_item.font(0))
        clone_item.setToolTip(0, source_item.toolTip(0))
        clone_item.setData(0, Qt.UserRole, item_path)
        clone_item.setData(0, Qt.UserRole + 1, preview_text)
        for child_index in range(source_item.childCount()):
            child_item = source_item.child(child_index)
            if child_item is None:
                continue
            cloned_child = self._clone_board_explorer_tree_item(child_item, parent_path=path_segments)
            clone_item.addChild(cloned_child)
            cloned_child.setExpanded(child_item.isExpanded())
        return clone_item

    def _apply_board_explorer_tree_card_style(self, tree_widget: QTreeWidget) -> None:
        if not isinstance(tree_widget, QTreeWidget):
            return

        def _normalize_label(raw_text: str) -> str:
            text = str(raw_text or "").strip()
            if not text:
                return ""
            for prefix in (DROPDOWN_EXPANDED_PREFIX, DROPDOWN_COLLAPSED_PREFIX):
                if text.startswith(prefix):
                    text = text[len(prefix):].strip()
                    break
            if ": " in text:
                text = text.split(": ", 1)[0].strip()
            if text.endswith(" {...}"):
                text = text[:-5].strip()
            if " [" in text and text.endswith("]"):
                text = text.rsplit(" ", 1)[0].strip()
            return text.strip()

        def _root_item_for(item: QTreeWidgetItem) -> QTreeWidgetItem:
            current = item
            parent = current.parent()
            while parent is not None:
                current = parent
                parent = current.parent()
            return current

        def _item_category(item: QTreeWidgetItem) -> str:
            title_key = _normalize_label(item.text(0)).lower()
            root_key = _normalize_label(_root_item_for(item).text(0)).lower()

            def _env_mcp_group(section_name: str, label_text: str) -> str | None:
                section = str(section_name or "").strip().lower()
                text = str(label_text or "").strip().lower()
                if section == "env":
                    if any(token in text for token in ("error", "warn", "failed", "timeout", "invalid")):
                        return "alerts"
                    if any(token in text for token in ("token", "secret", "password", "apikey", "api_key", "auth", "private", "credential")):
                        return "security"
                    if any(token in text for token in ("url", "uri", "endpoint", "host", "port", "socket")):
                        return "integration"
                    if any(token in text for token in ("path", "dir", "directory", "file", "workspace", "repo", "home")):
                        return "workspace"
                    if any(token in text for token in ("runtime", "worker", "queue", "model", "provider", "thread", "cache")):
                        return "runtime"
                    return "neutral"
                if section == "mcp":
                    if any(token in text for token in ("error", "warn", "failed", "timeout", "retry", "unavailable")):
                        return "alerts"
                    if any(token in text for token in ("token", "secret", "apikey", "api_key", "auth", "credential", "key")):
                        return "security"
                    if any(token in text for token in ("server", "endpoint", "host", "port", "url", "uri", "socket", "transport", "bridge")):
                        return "integration"
                    if any(token in text for token in ("tool", "runtime", "worker", "event", "stream", "sync")):
                        return "runtime"
                    if any(token in text for token in ("path", "file", "workspace", "repo", "module")):
                        return "workspace"
                    return "integration"
                return None

            section_category_map: tuple[tuple[str, str], ...] = (
                ("projects", "workspace"),
                ("runtime", "runtime"),
                ("databases", "data"),
                ("chat_history", "history"),
                ("history", "history"),
            )
            if root_key in {"env", "mcp"}:
                env_mcp_category = _env_mcp_group(root_key, title_key)
                if env_mcp_category:
                    return env_mcp_category
            for section_name, category in section_category_map:
                if root_key == section_name:
                    return category

            keyword_groups: tuple[tuple[str, tuple[str, ...]], ...] = (
                ("alerts", ("error", "warn", "warning", "failed", "failure", "timeout", "retry", "critical", "panic")),
                ("security", ("token", "secret", "credential", "password", "apikey", "api_key", "auth", "private key")),
                ("integration", ("mcp", "server", "socket", "endpoint", "http", "https", "api", "tool", "bridge")),
                ("data", ("database", "db", "collection", "record", "vector", "index", "table", "document")),
                ("runtime", ("runtime", "worker", "queue", "job", "dispatcher", "session", "event", "stream")),
                ("workspace", ("project", "workspace", "repo", "file", "path", "module", "template")),
            )
            for category, keywords in keyword_groups:
                if any(keyword in title_key for keyword in keywords):
                    return category

            if item.childCount() > 0:
                return "container"
            if "/" in title_key or "\\" in title_key:
                return "workspace"
            if title_key.endswith((".py", ".json", ".md", ".toml", ".yaml", ".yml", ".txt")):
                return "workspace"
            return "neutral"

        def _palette_for(category: str) -> dict[str, str]:
            palettes: dict[str, dict[str, str]] = {
                "workspace": {
                    "label_fg": "#d8ecff",
                    "label_bg": "rgba(66, 120, 168, 0.24)",
                    "label_border": "rgba(66, 120, 168, 0.62)",
                },
                "runtime": {
                    "label_fg": "#d6ffe1",
                    "label_bg": "rgba(56, 150, 99, 0.24)",
                    "label_border": "rgba(56, 150, 99, 0.64)",
                },
                "data": {
                    "label_fg": "#ffe6cf",
                    "label_bg": "rgba(178, 118, 68, 0.24)",
                    "label_border": "rgba(178, 118, 68, 0.62)",
                },
                "integration": {
                    "label_fg": "#d0f5ff",
                    "label_bg": "rgba(47, 149, 168, 0.24)",
                    "label_border": "rgba(47, 149, 168, 0.60)",
                },
                "history": {
                    "label_fg": "#f0dfff",
                    "label_bg": "rgba(120, 102, 181, 0.24)",
                    "label_border": "rgba(120, 102, 181, 0.60)",
                },
                "security": {
                    "label_fg": "#ffe8e6",
                    "label_bg": "rgba(178, 48, 68, 0.34)",
                    "label_border": "rgba(227, 96, 116, 0.88)",
                },
                "alerts": {
                    "label_fg": "#fff3d8",
                    "label_bg": "rgba(212, 128, 18, 0.36)",
                    "label_border": "rgba(255, 175, 64, 0.86)",
                },
                "container": {
                    "label_fg": "#dbe4e7",
                    "label_bg": "rgba(105, 120, 127, 0.20)",
                    "label_border": "rgba(124, 141, 149, 0.54)",
                },
                "neutral": {
                    "label_fg": "#dce3e7",
                    "label_bg": "rgba(84, 96, 104, 0.20)",
                    "label_border": "rgba(113, 127, 136, 0.52)",
                },
            }
            return palettes.get(str(category or "").strip().lower(), palettes["neutral"])

        def _group_top_margin(current_category: str, previous_category: str | None) -> int:
            normalized_current = str(current_category or "").strip().lower()
            normalized_previous = str(previous_category or "").strip().lower()
            if not normalized_current or not normalized_previous:
                return 0
            if normalized_current != normalized_previous:
                return 6
            return 0

        def _apply_marker_style(marker_widget: QLabel, marker_text: str) -> None:
            glyph = str(marker_text or "").strip()
            if glyph == DROPDOWN_EXPANDED_GLYPH:
                marker_color = str(self.scheme.get("col1") or "#3a5fff")
            elif glyph == DROPDOWN_COLLAPSED_GLYPH:
                marker_color = str(self.scheme.get("col8") or "#9a9a95")
            else:
                marker_color = "transparent"
            marker_widget.setStyleSheet(
                (
                    f"color: {marker_color};"
                    "font-weight: 700;"
                    "font-size: 11px;"
                    "background: transparent;"
                    "border: none;"
                    "padding: 0px;"
                )
            )

        def _apply_label_style(label_widget: QLabel, category: str) -> None:
            palette = _palette_for(category)
            label_widget.setStyleSheet(
                (
                    f"color: {palette['label_fg']};"
                    "font-weight: 700;"
                    "font-size: 10px;"
                    f"background: {palette['label_bg']};"
                    f"border: 1px solid {palette['label_border']};"
                    "border-radius: 6px;"
                    "padding: 1px 9px;"
                )
            )

        def _iter_grouped_child_rows(parent_item: QTreeWidgetItem):
            previous_child_category: str | None = None
            for child_index in range(parent_item.childCount()):
                child_item = parent_item.child(child_index)
                if not isinstance(child_item, QTreeWidgetItem):
                    continue

                child_title = _normalize_label(child_item.text(0))
                child_category = _item_category(child_item)
                child_margin_top = _group_top_margin(child_category, previous_child_category)
                previous_child_category = child_category

                yield child_item, child_title, child_category, child_margin_top
                yield from _iter_grouped_child_rows(child_item)

        def _dropdown_marker(item: QTreeWidgetItem) -> str:
            if item.childCount() <= 0:
                return ""
            return dropdown_prefix(item.isExpanded())

        def _apply_row(item: QTreeWidgetItem, item_title: str, item_category: str, row_margin_top: int) -> None:
            safe_title = str(item_title or "").strip() or str(item.text(0) or "").strip()

            container_widget = tree_widget.itemWidget(item, 0)
            if not isinstance(container_widget, QWidget) or not bool(container_widget.property("board_explorer_tree_card_widget")):
                container_widget = QWidget(tree_widget)
                container_widget.setProperty("board_explorer_tree_card_widget", True)
                container_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                container_layout = QHBoxLayout(container_widget)
                container_layout.setContentsMargins(0, row_margin_top, 0, 0)
                container_layout.setSpacing(0)

                marker_widget = QLabel("", container_widget)
                marker_widget.setObjectName("boardExplorerTreeCardMarker")
                marker_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                marker_widget.setAlignment(Qt.AlignCenter)
                marker_widget.setFixedWidth(11)
                _apply_marker_style(marker_widget, "")

                label_widget = QLabel(safe_title, container_widget)
                label_widget.setObjectName("boardExplorerTreeCardLabel")
                label_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                label_widget.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                label_widget.setWordWrap(False)

                container_layout.addWidget(marker_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
                container_layout.addSpacing(2)
                container_layout.addWidget(label_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
                container_layout.addStretch(1)
                tree_widget.setItemWidget(item, 0, container_widget)
            else:
                container_layout = container_widget.layout()
                if isinstance(container_layout, QHBoxLayout):
                    container_layout.setContentsMargins(0, row_margin_top, 0, 0)
                marker_widget = container_widget.findChild(QLabel, "boardExplorerTreeCardMarker")
                if not isinstance(marker_widget, QLabel):
                    marker_widget = QLabel("", container_widget)
                    marker_widget.setObjectName("boardExplorerTreeCardMarker")
                    marker_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                    marker_widget.setAlignment(Qt.AlignCenter)
                    marker_widget.setFixedWidth(11)
                    _apply_marker_style(marker_widget, "")
                    if isinstance(container_layout, QHBoxLayout):
                        container_layout.insertWidget(0, marker_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)
                        container_layout.insertSpacing(1, 2)
                label_widget = container_widget.findChild(QLabel, "boardExplorerTreeCardLabel")
                if not isinstance(label_widget, QLabel):
                    label_widget = QLabel(safe_title, container_widget)
                    label_widget.setObjectName("boardExplorerTreeCardLabel")
                    label_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                    label_widget.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                    label_widget.setWordWrap(False)
                    if isinstance(container_layout, QHBoxLayout):
                        insert_index = max(0, container_layout.count() - 1)
                        container_layout.insertWidget(insert_index, label_widget, 0, Qt.AlignLeft | Qt.AlignVCenter)

            marker = _dropdown_marker(item).strip()
            marker_widget.setText(marker)
            _apply_marker_style(marker_widget, marker)
            label_widget.setText(safe_title)
            label_widget.setToolTip(str(item.toolTip(0) or safe_title))
            _apply_label_style(label_widget, item_category)

            desired_height = max(22, int(label_widget.sizeHint().height()) + row_margin_top)
            item.setSizeHint(0, QSize(0, desired_height))

        previous_top_level_category: str | None = None
        for top_level_index in range(tree_widget.topLevelItemCount()):
            top_item = tree_widget.topLevelItem(top_level_index)
            if not isinstance(top_item, QTreeWidgetItem):
                continue

            top_title = _normalize_label(top_item.text(0))
            top_category = _item_category(top_item)
            top_margin = _group_top_margin(top_category, previous_top_level_category)
            previous_top_level_category = top_category

            _apply_row(top_item, top_title, top_category, top_margin)
            for child_item, child_title, child_category, child_margin_top in _iter_grouped_child_rows(top_item):
                _apply_row(child_item, child_title, child_category, child_margin_top)

    @staticmethod
    def _board_canvas_element_id(source_kind: str, source_ref: str) -> str:
        return f"{str(source_kind or '').strip().lower()}::{str(source_ref or '').strip()}"

    @staticmethod
    def _board_canvas_tree_item_path(tree_item: QTreeWidgetItem | None) -> str:
        if not isinstance(tree_item, QTreeWidgetItem):
            return ""
        return str(tree_item.data(0, Qt.UserRole) or "").strip()

    @staticmethod
    def _board_canvas_tree_item_preview(tree_item: QTreeWidgetItem | None) -> str:
        if not isinstance(tree_item, QTreeWidgetItem):
            return ""
        preview_text = str(tree_item.data(0, Qt.UserRole + 1) or "").strip()
        if preview_text:
            return preview_text
        child_count = int(tree_item.childCount())
        if child_count > 0:
            return f"Explorer tree node with {child_count} child entries."
        label = str(tree_item.text(0) or "").strip()
        return f"Explorer tree leaf from {label or 'Explorer'}"

    def _board_canvas_element_for_board_title(self, item_title: str) -> dict[str, Any]:
        resolved_title = str(item_title or "").strip()
        return {
            "id": self._board_canvas_element_id("board", resolved_title),
            "title": resolved_title,
            "preview": self._board_item_preview_text(resolved_title),
            "source_kind": "board",
            "source_ref": resolved_title,
            "action_text": f"Open {resolved_title}",
        }

    def _board_canvas_element_for_tree_item(self, tree_item: QTreeWidgetItem | None) -> dict[str, Any] | None:
        if not isinstance(tree_item, QTreeWidgetItem):
            return None

        item_path = self._board_canvas_tree_item_path(tree_item)
        item_title = str(tree_item.text(0) or "").strip()
        if not item_path or not item_title or "not attached to this window yet" in item_title.lower():
            return None

        return {
            "id": self._board_canvas_element_id("tree", item_path),
            "title": item_title,
            "preview": self._board_canvas_tree_item_preview(tree_item),
            "source_kind": "tree",
            "source_ref": item_path,
            "action_text": f"Focus {item_title}",
        }

    def _board_canvas_element_by_id(
        self,
        board_context: dict[str, Any] | None,
        element_id: str,
    ) -> dict[str, Any] | None:
        if not isinstance(board_context, dict):
            return None
        elements = board_context.get("board_canvas_elements")
        if not isinstance(elements, list):
            return None
        normalized_id = str(element_id or "").strip()
        if not normalized_id:
            return None
        for element in elements:
            if isinstance(element, dict) and str(element.get("id") or "").strip() == normalized_id:
                return element
        return None

    @staticmethod
    def _board_canvas_numeric_value(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _board_canvas_hidden_board_titles() -> set[str]:
        return {
            "explorer tree",
            "board canvas",
            "projects",
        }

    @staticmethod
    def _board_canvas_default_origin_for_index(element_index: int) -> tuple[float, float]:
        normalized_index = max(0, int(element_index))
        column_count = 2
        column_index = normalized_index % column_count
        row_index = normalized_index // column_count
        x = _BoardCanvasView.CARD_MARGIN + column_index * (_BoardCanvasView.CARD_WIDTH + _BoardCanvasView.CARD_GAP)
        y = _BoardCanvasView.CARD_MARGIN + row_index * (_BoardCanvasView.CARD_HEIGHT + _BoardCanvasView.CARD_GAP)
        return float(x), float(y)

    def _board_canvas_element_position(self, element: Mapping[str, Any] | None) -> tuple[float, float] | None:
        if not isinstance(element, Mapping):
            return None
        x_value = self._board_canvas_numeric_value(element.get("x"))
        y_value = self._board_canvas_numeric_value(element.get("y"))
        if x_value is None or y_value is None:
            return None
        return max(0.0, float(x_value)), max(0.0, float(y_value))

    def _assign_default_positions_to_board_canvas_elements(self, elements: Sequence[dict[str, Any]]) -> None:
        occupied_positions = {
            (round(position[0], 3), round(position[1], 3))
            for position in (self._board_canvas_element_position(element) for element in elements)
            if position is not None
        }
        for element_index, element in enumerate(elements):
            if not isinstance(element, dict):
                continue
            if self._board_canvas_element_position(element) is not None:
                continue

            candidate_index = element_index
            while True:
                x_value, y_value = self._board_canvas_default_origin_for_index(candidate_index)
                marker = (round(x_value, 3), round(y_value, 3))
                if marker not in occupied_positions:
                    element["x"] = float(x_value)
                    element["y"] = float(y_value)
                    occupied_positions.add(marker)
                    break
                candidate_index += 1

    def _next_board_canvas_origin(self, elements: Sequence[dict[str, Any]]) -> tuple[float, float]:
        occupied_positions = {
            (round(position[0], 3), round(position[1], 3))
            for position in (self._board_canvas_element_position(element) for element in elements)
            if position is not None
        }
        candidate_index = len([element for element in elements if isinstance(element, dict)])
        while True:
            x_value, y_value = self._board_canvas_default_origin_for_index(candidate_index)
            marker = (round(x_value, 3), round(y_value, 3))
            if marker not in occupied_positions:
                return float(x_value), float(y_value)
            candidate_index += 1

    def _set_board_canvas_element_position(
        self,
        board_context: dict[str, Any] | None,
        element_id: str,
        x_value: float,
        y_value: float,
        *,
        persist: bool = True,
    ) -> bool:
        if not isinstance(board_context, dict):
            return False
        self._ensure_board_canvas_elements(board_context)
        element = self._board_canvas_element_by_id(board_context, element_id)
        if not isinstance(element, dict):
            return False
        element["x"] = max(0.0, float(x_value))
        element["y"] = max(0.0, float(y_value))
        board_context["board_canvas_selected_element_id"] = str(element_id or "").strip()
        if persist:
            self._schedule_runtime_state_save()
        return True

    def _find_board_tree_item_by_path(self, tree_widget: QTreeWidget, tree_path: str) -> QTreeWidgetItem | None:
        normalized_path = str(tree_path or "").strip()
        if not normalized_path:
            return None

        stack: list[QTreeWidgetItem] = []
        for item_index in range(tree_widget.topLevelItemCount() - 1, -1, -1):
            item = tree_widget.topLevelItem(item_index)
            if item is not None:
                stack.append(item)

        while stack:
            item = stack.pop()
            if self._board_canvas_tree_item_path(item) == normalized_path:
                return item
            for child_index in range(item.childCount() - 1, -1, -1):
                child = item.child(child_index)
                if child is not None:
                    stack.append(child)
        return None

    def _ensure_board_canvas_elements(self, board_context: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(board_context, dict):
            return []

        elements = board_context.get("board_canvas_elements")
        if not isinstance(elements, list):
            elements = []
            board_context["board_canvas_elements"] = elements

        hidden_titles = self._board_canvas_hidden_board_titles()
        filtered_elements: list[dict[str, Any]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            source_kind = str(element.get("source_kind") or "").strip().lower()
            source_ref = str(element.get("source_ref") or element.get("title") or "").strip().lower()
            if source_kind == "board" and source_ref in hidden_titles:
                continue
            filtered_elements.append(element)
        if filtered_elements is not elements:
            elements = filtered_elements
            board_context["board_canvas_elements"] = elements

        element_map: dict[str, dict[str, Any]] = {
            str(element.get("id") or "").strip(): element
            for element in elements
            if isinstance(element, dict) and str(element.get("id") or "").strip()
        }

        for item_title in self._primary_board_item_titles():
            if str(item_title or "").strip().lower() in hidden_titles:
                continue
            element = self._board_canvas_element_for_board_title(item_title)
            existing = element_map.get(str(element.get("id") or ""))
            if isinstance(existing, dict):
                existing.update(element)
            else:
                elements.append(element)
                element_map[str(element.get("id") or "")] = element

        tree_widget = board_context.get("board_explorer_tree_widget")
        if isinstance(tree_widget, QTreeWidget):
            for element in list(elements):
                if not isinstance(element, dict):
                    continue
                if str(element.get("source_kind") or "").strip().lower() != "tree":
                    continue
                tree_path = str(element.get("source_ref") or "").strip()
                if not tree_path:
                    continue
                tree_item = self._find_board_tree_item_by_path(tree_widget, tree_path)
                updated = self._board_canvas_element_for_tree_item(tree_item)
                if isinstance(updated, dict):
                    element.update(updated)

        selected_id = str(board_context.get("board_canvas_selected_element_id") or "").strip()
        if not selected_id:
            selected_title = str(getattr(self, "_last_selected_board_item_title", "") or "").strip()
            if selected_title:
                selected_id = self._board_canvas_element_id("board", selected_title)
        if selected_id not in element_map and elements:
            selected_id = str(elements[0].get("id") or "").strip()
        board_context["board_canvas_selected_element_id"] = selected_id
        self._assign_default_positions_to_board_canvas_elements(elements)
        return elements

    def _focus_board_tree_item_in_context(
        self,
        board_context: dict[str, Any] | None,
        tree_path: str,
        *,
        activate_tab: bool = False,
    ) -> bool:
        if not isinstance(board_context, dict):
            return False
        self._focus_board_section_in_context(board_context, "Explorer Tree", activate_tab=activate_tab)
        tree_widget = board_context.get("board_explorer_tree_widget")
        if not isinstance(tree_widget, QTreeWidget):
            return False
        target_item = self._find_board_tree_item_by_path(tree_widget, tree_path)
        if not isinstance(target_item, QTreeWidgetItem):
            return False

        parent_item = target_item.parent()
        while isinstance(parent_item, QTreeWidgetItem):
            parent_item.setExpanded(True)
            parent_item = parent_item.parent()
        tree_widget.setCurrentItem(target_item)
        tree_widget.scrollToItem(target_item)
        return True

    def _add_tree_item_to_board_canvas(
        self,
        board_context: dict[str, Any] | None,
        tree_item: QTreeWidgetItem | None,
    ) -> bool:
        if not isinstance(board_context, dict):
            return False
        element = self._board_canvas_element_for_tree_item(tree_item)
        if not isinstance(element, dict):
            return False

        elements = self._ensure_board_canvas_elements(board_context)
        existing = self._board_canvas_element_by_id(board_context, str(element.get("id") or ""))
        if isinstance(existing, dict):
            existing.update(element)
        else:
            x_value, y_value = self._next_board_canvas_origin(elements)
            element["x"] = float(x_value)
            element["y"] = float(y_value)
            elements.append(element)
        board_context["board_canvas_selected_element_id"] = str(element.get("id") or "")
        self._schedule_runtime_state_save()
        return True

    def _handle_board_explorer_tree_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        tree_widget = self.sender()
        board_context = self._board_context_from_object(tree_widget)
        if not self._add_tree_item_to_board_canvas(board_context, item):
            return
        self._render_all_board_canvas_surfaces()

    def _handle_board_explorer_tree_item_expanded(self, _item: QTreeWidgetItem) -> None:
        tree_widget = self.sender()
        if isinstance(tree_widget, QTreeWidget):
            self._apply_board_explorer_tree_card_style(tree_widget)

    def _handle_board_explorer_tree_item_collapsed(self, _item: QTreeWidgetItem) -> None:
        tree_widget = self.sender()
        if isinstance(tree_widget, QTreeWidget):
            self._apply_board_explorer_tree_card_style(tree_widget)

    def _handle_board_canvas_item_moved(self, item_id: str, x_value: float, y_value: float) -> None:
        canvas_view = self.sender()
        board_context = self._board_context_from_object(canvas_view)
        if not self._set_board_canvas_element_position(board_context, item_id, x_value, y_value, persist=True):
            return
        self._render_board_canvas_surface(board_context)

    def _sync_board_explorer_tree_panel(self, board_context: dict[str, Any]) -> None:
        tree_widget = board_context.get("board_explorer_tree_widget")
        status_label = board_context.get("board_explorer_tree_status_label")
        if not isinstance(tree_widget, QTreeWidget):
            return

        surface_color = str(self.scheme.get("col7", "#0b0b0b"))
        text_color = str(self.scheme.get("col6", "#E3E3DE"))
        frame_color = str(self.scheme.get("col10", "#303030"))

        tree_widget.setStyleSheet(
            f"""
            QTreeWidget#boardExplorerTree {{
                background-color: {surface_color};
                color: {text_color};
                border: 1px solid {frame_color};
                border-radius: 14px;
                padding: 4px 0px 6px 0px;
                outline: none;
            }}
            QTreeWidget#boardExplorerTree::item {{
                margin: 0px;
                padding: 0px;
                color: {text_color};
                border: none;
                background: transparent;
            }}
            """
        )

        source_tree = self._board_explorer_source_tree()
        tree_widget.blockSignals(True)
        tree_widget.clear()
        try:
            if not isinstance(source_tree, QTreeWidget):
                placeholder_item = QTreeWidgetItem(["Explorer tree is not attached to this window yet."])
                placeholder_item.setFlags(placeholder_item.flags() & ~Qt.ItemIsEditable)
                tree_widget.addTopLevelItem(placeholder_item)
                if isinstance(status_label, QLabel):
                    status_label.setText("Source unavailable. Open the shared explorer dock to mirror the live tree here.")
                return

            top_level_count = 0
            for item_index in range(source_tree.topLevelItemCount()):
                source_item = source_tree.topLevelItem(item_index)
                if source_item is None:
                    continue
                cloned_item = self._clone_board_explorer_tree_item(source_item)
                tree_widget.addTopLevelItem(cloned_item)
                cloned_item.setExpanded(source_item.isExpanded())
                top_level_count += 1

            self._apply_board_explorer_tree_card_style(tree_widget)

            diagnostic = self._load_tree_stream_diagnostic()
            if isinstance(status_label, QLabel):
                transport = str(diagnostic.get("transport") or "n/a").strip() or "n/a"
                state = str(diagnostic.get("connection_state") or "unavailable").strip() or "unavailable"
                status_label.setText(
                    f"{top_level_count} root sections mirrored from the shared explorer | transport: {transport} | state: {state} | double-click adds nodes to the canvas"
                )
        finally:
            tree_widget.blockSignals(False)

    def _create_board_canvas_panel(self, parent: QWidget) -> tuple[QWidget, _BoardCanvasView, QLabel]:
        panel = QWidget(parent)
        panel.setObjectName("controlPanel")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = QLabel("<b>Board Canvas</b>", panel)
        title.setObjectName("controlMeta")
        layout.addWidget(title)

        status_label = QLabel(
            f"Click a card to focus the source section on {self._PRIMARY_BOARD_TAB_LABEL}.",
            panel,
        )
        status_label.setObjectName("controlMeta")
        status_label.setWordWrap(True)
        layout.addWidget(status_label)

        canvas_view = _BoardCanvasView(panel)
        canvas_view.itemActivated.connect(self._activate_board_canvas_item)
        canvas_view.itemMoved.connect(self._handle_board_canvas_item_moved)
        layout.addWidget(canvas_view, 1)
        return panel, canvas_view, status_label

    def _activate_board_canvas_item(self, item_title: str) -> None:
        resolved_id = str(item_title or "").strip()
        if not resolved_id:
            return

        board_context = self._active_board_context()
        if not isinstance(board_context, dict):
            board_context = self._primary_board_context
        if not isinstance(board_context, dict):
            return

        board_context["board_canvas_selected_element_id"] = resolved_id
        element = self._board_canvas_element_by_id(board_context, resolved_id)
        if isinstance(element, dict):
            source_kind = str(element.get("source_kind") or "").strip().lower()
            source_ref = str(element.get("source_ref") or element.get("title") or "").strip()
            if source_kind == "tree":
                self._focus_board_tree_item_in_context(board_context, source_ref, activate_tab=True)
            else:
                self._last_selected_board_item_title = source_ref
                self._focus_board_section_in_context(board_context, source_ref, activate_tab=True)
            self._schedule_runtime_state_save()
        self._render_all_board_canvas_surfaces()

    def _render_all_board_canvas_surfaces(self) -> None:
        board_contexts = self._board_contexts_in_display_order()
        if not board_contexts and isinstance(self._primary_board_context, dict):
            board_contexts = [self._primary_board_context]
        for board_context in board_contexts:
            self._render_board_canvas_surface(board_context)

    def _render_board_canvas_surface(self, board_context: dict[str, Any]) -> None:
        canvas_view = board_context.get("board_canvas_view")
        status_label = board_context.get("board_canvas_status_label")
        if not isinstance(canvas_view, _BoardCanvasView):
            return

        elements = self._ensure_board_canvas_elements(board_context)
        selected_id = str(board_context.get("board_canvas_selected_element_id") or "").strip()
        cards = [
            {
                "id": str(element.get("id") or "").strip(),
                "title": str(element.get("title") or "").strip() or "Element",
                "preview": str(element.get("preview") or "").strip(),
                "action_text": str(element.get("action_text") or "").strip(),
                "x": element.get("x"),
                "y": element.get("y"),
                "selected": str(element.get("id") or "").strip() == selected_id,
            }
            for element in elements
            if isinstance(element, dict)
        ]
        canvas_view.set_cards(
            cards,
            surface_color=str(self.scheme.get("col7", "#0b0b0b")),
            accent_color=str(self.scheme.get("col1", "#3a5fff")),
            text_color=str(self.scheme.get("col6", "#E3E3DE")),
            muted_color=str(self.scheme.get("col8", "#9a9a95")),
        )

        if isinstance(status_label, QLabel):
            if cards:
                board_count = sum(1 for element in elements if isinstance(element, dict) and str(element.get("source_kind") or "").strip().lower() == "board")
                tree_count = sum(1 for element in elements if isinstance(element, dict) and str(element.get("source_kind") or "").strip().lower() == "tree")
                selected_card = next(
                    (card for card in cards if str(card.get("id") or "").strip() == selected_id),
                    cards[0],
                )
                selected_note = str(selected_card.get("title") or cards[0]["title"]).strip()
                status_label.setText(
                    f"{board_count} board surfaces + {tree_count} tree elements on the canvas. Active focus: {selected_note}. Double-click tree nodes to extend the composition."
                )
            else:
                status_label.setText(f"No board surfaces are available in {self._PRIMARY_BOARD_TAB_LABEL} yet.")

    def _create_board_build_section(self, parent: QWidget) -> QWidget:
        panel = QFrame(parent)
        panel.setObjectName("BuildTabWidget")
        panel.setProperty("build_tab_widget", True)
        panel.setMinimumSize(0, 0)
        panel.setMinimumHeight(416)
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_panel = QFrame(panel)
        header_panel.setObjectName("runtimeWidgetPanel")
        _clear_frame_chrome(header_panel)
        header_panel.setProperty("runtime_widget_kind", "builder_panel")
        header_panel.setProperty("runtime_widget_title", self._BUILD_RUNTIME_TAB_LABEL)

        header_layout_root = QVBoxLayout(header_panel)
        header_layout_root.setContentsMargins(0, 0, 0, 0)
        header_layout_root.setSpacing(6)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(_SURFACE_INSET_PX, _SURFACE_INSET_PX, _SURFACE_INSET_PX, 0)
        header_layout.setSpacing(6)
        title_label = QLabel(self._BUILD_RUNTIME_TAB_LABEL, header_panel)
        title_label.setObjectName("runtimeWidgetTitle")
        header_layout.addWidget(title_label, 1)

        builder_panel, _internal_build_btn, _internal_post_btn = self._create_runtime_builder_widget(
            header_panel,
            initial_text="",
            connect_text_changed=False,
            show_toolbar=False,
        )

        def _add_build_header_action(
            icon_name: str,
            tooltip: str,
            button_name: str,
        ) -> None:
            target_button = builder_panel.findChild(QPushButton, button_name)
            if not isinstance(target_button, QPushButton):
                return
            action_button = QToolButton(header_panel)
            action_button.setObjectName("runtimeWidgetActionButton")
            action_button.setIcon(_icon(icon_name))
            action_button.setIconSize(QSize(14, 14))
            action_button.setToolTip(tooltip)
            action_button.setCursor(Qt.PointingHandCursor)
            action_button.setAutoRaise(True)
            action_button.clicked.connect(
                lambda _checked=False, target=target_button: target.click()
            )
            header_layout.addWidget(action_button, 0)

        _add_build_header_action("open_file.svg", "Template laden", "builderTemplateButton")
        _add_build_header_action("deployed_code.svg", "Sync Build starten", "builderBuildButton")
        _add_build_header_action("send.svg", "Ergebnis ins Operations-Log schreiben", "builderPostButton")
        _add_build_header_action(
            "file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg",
            "JSON exportieren",
            "builderCopyButton",
        )

        builder_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        header_layout_root.addLayout(header_layout)
        header_layout_root.addWidget(builder_panel, 1)
        self._apply_runtime_widget_panel_scheme(header_panel)

        layout.addWidget(header_panel, 1)
        return panel

    def _create_runtime_builder_widget(
        self,
        parent: QWidget,
        *,
        initial_text: str,
        connect_text_changed: bool,
        show_toolbar: bool,
    ) -> tuple[QWidget, QPushButton | None, QPushButton | None]:
        try:
            initial_payload = self._build_agent_system_template("agent_system", "/create agents")
        except Exception:
            initial_payload = {"action": "build_agent_system_configs"}
        builder_panel = self._create_agent_system_builder_config_panel(
            initial_payload=initial_payload,
            build_handler=self._execute_agent_system_builder_payload,
            parent_container=parent,
            show_toolbar=show_toolbar,
        )

        internal_build_btn = builder_panel.findChild(QPushButton, "builderBuildButton")
        internal_post_btn = builder_panel.findChild(QPushButton, "builderPostButton")
        builder_editor = builder_panel.findChild(CodeViewer)
        if isinstance(builder_editor, CodeViewer):
            if initial_text:
                builder_editor.setPlainText(initial_text)
            builder_editor.setMinimumHeight(96)
            builder_editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            if connect_text_changed:
                builder_editor.textChanged.connect(self._schedule_runtime_state_save)

        return builder_panel, internal_build_btn, internal_post_btn

    def _create_board_extensions_section(self, parent: QWidget) -> tuple[QWidget, ExtensionsWorkspaceWidget]:
        panel = QWidget(parent)
        panel.setObjectName("controlPanel")
        panel.setMinimumSize(0, 0)
        panel.setMinimumHeight(380)
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        workspace = ExtensionsWorkspaceWidget(
            accent=dict(self._accent),
            base=dict(self._base),
            parent=panel,
            source_uri="agentsdb://127.0.0.1:2331/tools:graph_view",
            control_plane_widget_ref=None,
        )
        workspace.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(workspace)
        return panel, workspace

    def _create_board_page(self, board_title: str) -> tuple[QWidget, dict[str, Any]]:
        config_tab = QWidget(self.tabs)
        config_tab.setObjectName("BoardTabWidget")
        config_tab.setProperty("board_tab_widget", True)
        config_tab.setMinimumSize(0, 0)
        config_tab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        config_layout = QVBoxLayout(config_tab)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(0)

        board_context: dict[str, Any] = {
            "tab_widget": config_tab,
            "config_monitor_sections": [],
            "_config_monitor_section_state": {},
            "_config_monitor_splitter_handle_controls": {},
            "_config_monitor_host_threat_flow_size": 220,
            "config_monitor_host_splitter_toggle": None,
            "config_monitor_host_splitter_label": None,
            "board_canvas_elements": [],
            "board_canvas_selected_element_id": "",
        }

        board_context["config_summary_view"] = self._create_board_browser(config_tab)
        board_context["config_manifest_view"] = self._create_board_browser(config_tab)
        board_context["monitor_summary_view"] = self._create_board_browser(config_tab)
        board_context["monitor_detail_view"] = self._create_board_browser(config_tab)
        board_context["monitor_timeline_view"] = self._create_board_browser(config_tab)
        board_context["monitor_trace_view"] = self._create_board_browser(config_tab)
        board_context["operator_summary_view"] = self._create_board_browser(config_tab)
        board_context["operator_log_view"] = self._create_board_browser(config_tab)
        board_context["config_monitor_threat_flow_view"] = self._create_board_browser(config_tab)

        monitor_filter_panel = QWidget(config_tab)
        monitor_filter_panel.setObjectName("controlPanel")
        monitor_filter_layout = QVBoxLayout(monitor_filter_panel)
        monitor_filter_layout.setContentsMargins(12, 10, 12, 10)
        monitor_filter_layout.setSpacing(8)
        monitor_filter_title = QLabel("<b>Monitoring Filters</b>", monitor_filter_panel)
        monitor_filter_title.setObjectName("controlMeta")
        monitor_filter_layout.addWidget(monitor_filter_title)
        monitor_filter_form = QFormLayout()
        monitor_filter_form.setContentsMargins(0, 0, 0, 0)
        monitor_filter_form.setSpacing(6)
        monitor_filter_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        monitor_filter_layout.addLayout(monitor_filter_form)
        board_context["monitor_filter_panel"] = monitor_filter_panel

        board_context["agent_selector"] = self._create_board_selector(config_tab, self._refresh_drilldown_views)
        board_context["workflow_selector"] = self._create_board_selector(config_tab, self._refresh_drilldown_views)
        board_context["trace_agent_selector"] = self._create_board_selector(config_tab, self._refresh_monitoring_views)
        board_context["trace_workflow_selector"] = self._create_board_selector(config_tab, self._refresh_monitoring_views)
        board_context["trace_tool_selector"] = self._create_board_selector(config_tab, self._refresh_monitoring_views)
        board_context["trace_handoff_selector"] = self._create_board_selector(config_tab, self._refresh_monitoring_views)

        for label_text, key in (
            ("Agent", "agent_selector"),
            ("Workflow", "workflow_selector"),
            ("Trace Agent", "trace_agent_selector"),
            ("Trace Workflow", "trace_workflow_selector"),
            ("Trace Tool", "trace_tool_selector"),
            ("Trace Handoff", "trace_handoff_selector"),
        ):
            label = QLabel(label_text, monitor_filter_panel)
            label.setObjectName("controlMeta")
            monitor_filter_form.addRow(label, board_context[key])

        tree_stream_panel, tree_stream_values = self._create_board_tree_stream_panel(config_tab)
        board_context["tree_stream_panel"] = tree_stream_panel
        board_context["tree_stream_transport_value"] = tree_stream_values["transport"]
        board_context["tree_stream_state_value"] = tree_stream_values["state"]
        board_context["tree_stream_event_value"] = tree_stream_values["event"]
        board_context["tree_stream_retry_value"] = tree_stream_values["retry"]
        board_context["tree_stream_updated_value"] = tree_stream_values["updated"]
        board_context["tree_stream_error_value"] = tree_stream_values["error"]

        explorer_tree_panel, explorer_tree_widget, explorer_tree_status_label = self._create_board_explorer_tree_panel(config_tab)
        board_context["board_explorer_tree_panel"] = explorer_tree_panel
        board_context["board_explorer_tree_widget"] = explorer_tree_widget
        board_context["board_explorer_tree_status_label"] = explorer_tree_status_label

        board_canvas_panel, board_canvas_view, board_canvas_status_label = self._create_board_canvas_panel(config_tab)
        board_context["board_canvas_panel"] = board_canvas_panel
        board_context["board_canvas_view"] = board_canvas_view
        board_context["board_canvas_status_label"] = board_canvas_status_label

        detail_panel = QWidget(config_tab)
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(tree_stream_panel)
        detail_action_row = QHBoxLayout()
        detail_action_row.setContentsMargins(0, 0, 0, 0)
        detail_action_row.setSpacing(8)
        board_context["btn_refresh_detail"] = ToolButton(
            "reload_.svg",
            "Monitor-Detail aktualisieren",
            slot=self._refresh_drilldown_views,
            parent=config_tab,
        )
        board_context["btn_refresh_detail"].setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        detail_action_row.addWidget(board_context["btn_refresh_detail"], 0)
        detail_action_row.addStretch(1)
        detail_layout.addLayout(detail_action_row)
        detail_layout.addWidget(board_context["monitor_detail_view"], 1)

        operator_actions_panel = QWidget(config_tab)
        operator_actions_panel.setObjectName("controlPanel")
        operator_actions_layout = QHBoxLayout(operator_actions_panel)
        operator_actions_layout.setContentsMargins(12, 10, 12, 10)
        operator_actions_layout.setSpacing(8)
        board_context["operator_actions_panel"] = operator_actions_panel
        for key, icon_name, tooltip, slot in (
            ("btn_refresh_health", "reload_.svg", "Operator-Checks aktualisieren", self._run_operator_health_checks),
            ("btn_probe_queue", "play_green.svg", "Queue-Backend pruefen", self._probe_queue_health),
            ("btn_probe_agentsdb", "cloud_.svg", "AgentsDB pruefen", self._probe_agentsdb_health),
            ("btn_probe_dispatcher", "graph_view.svg", "Dispatcher pruefen", self._probe_dispatcher_health),
            ("btn_repair_dispatcher", "setting_tools.svg", "Dispatcher reparieren", self._repair_dispatcher_store),
            ("btn_probe_mcp", "extension.svg", "MCP pruefen", self._probe_mcp_health),
            ("btn_export_runtime", "floppy-disk.svg", "Runtime exportieren", self._export_runtime_snapshot_report),
        ):
            button = ToolButton(icon_name, tooltip, slot=slot, parent=config_tab)
            operator_actions_layout.addWidget(button, 0)
            board_context[key] = button
        operator_actions_layout.addStretch(1)

        operator_filters_panel = QWidget(config_tab)
        operator_filters_panel.setObjectName("controlPanel")
        operator_filters_layout = QVBoxLayout(operator_filters_panel)
        operator_filters_layout.setContentsMargins(12, 10, 12, 10)
        operator_filters_layout.setSpacing(8)
        operator_filters_title = QLabel("<b>Operator Filters</b>", operator_filters_panel)
        operator_filters_title.setObjectName("controlMeta")
        operator_filters_layout.addWidget(operator_filters_title)
        operator_filters_form = QFormLayout()
        operator_filters_form.setContentsMargins(0, 0, 0, 0)
        operator_filters_form.setSpacing(6)
        operator_filters_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        operator_filters_layout.addLayout(operator_filters_form)
        board_context["operator_filters_panel"] = operator_filters_panel
        for label_text, key in (
            ("Status", "operator_status_selector"),
            ("Action Type", "operator_audit_selector"),
            ("Group", "operator_group_selector"),
            ("Source", "operator_source_selector"),
        ):
            selector = self._create_board_selector(config_tab, self._handle_operator_filter_change)
            board_context[key] = selector
            label = QLabel(label_text, operator_filters_panel)
            label.setObjectName("controlMeta")
            operator_filters_form.addRow(label, selector)

        build_panel = self._create_board_build_section(config_tab)
        board_context["build_section_panel"] = build_panel
        extensions_panel, extensions_workspace = self._create_board_extensions_section(config_tab)
        board_context["extensions_section_panel"] = extensions_panel
        board_context["extensions_section_workspace"] = extensions_workspace

        threat_flow_panel = QWidget(config_tab)
        threat_flow_panel.setObjectName("controlPanel")
        threat_flow_layout = QVBoxLayout(threat_flow_panel)
        threat_flow_layout.setContentsMargins(12, 10, 12, 10)
        threat_flow_layout.setSpacing(8)
        threat_flow_title = QLabel("<b>Threat Flow</b>", threat_flow_panel)
        threat_flow_title.setObjectName("controlMeta")
        threat_flow_layout.addWidget(threat_flow_title)
        threat_flow_layout.addWidget(board_context["config_monitor_threat_flow_view"], 1)
        board_context["config_monitor_threat_flow_panel"] = threat_flow_panel

        scroll_area = QScrollArea(config_tab)
        scroll_area.setObjectName("controlMonitoringScrollArea")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_content = QWidget(scroll_area)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 3, 0, 0)
        scroll_layout.setSpacing(0)
        splitter = QSplitter(Qt.Vertical, scroll_content)
        splitter.setChildrenCollapsible(False)
        splitter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        splitter_anchor = QWidget(splitter)
        splitter_anchor.setObjectName("controlSplitterAnchor")
        splitter_anchor.setMinimumHeight(0)
        splitter_anchor.setMaximumHeight(0)
        splitter_anchor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        splitter.addWidget(splitter_anchor)
        scroll_layout.addWidget(splitter, 0)
        scroll_layout.addStretch(1)
        scroll_content.setLayout(scroll_layout)
        scroll_area.setWidget(scroll_content)
        config_layout.addWidget(scroll_area, 1)

        board_context["config_monitor_scroll_area"] = scroll_area
        board_context["config_monitor_scroll_content"] = scroll_content
        board_context["config_monitor_host_widget"] = scroll_content
        board_context["config_monitor_host_splitter"] = None
        board_context["config_monitor_splitter"] = splitter
        board_context["config_monitor_splitter_anchor"] = splitter_anchor

        section_specs = [
            ("Monitoring Summary", board_context["monitor_summary_view"], False),
            ("Monitoring Filters", board_context["monitor_filter_panel"], False),
            ("Monitoring Drilldown", detail_panel, False),
            ("Trace Detail", board_context["monitor_trace_view"], False),
            ("Event Timeline", board_context["monitor_timeline_view"], False),
            ("Threat Flow", board_context["config_monitor_threat_flow_panel"], False),
            ("Configuration Summary", board_context["config_summary_view"], False),
            ("Configuration Manifest", board_context["config_manifest_view"], False),
            ("Operator Actions", operator_actions_panel, False),
            ("Operator Filters", operator_filters_panel, False),
            ("Operator Summary", board_context["operator_summary_view"], False),
            ("Operator Log", board_context["operator_log_view"], False),
            ("Build", build_panel, True),
            ("Extensions", extensions_panel, True),
        ]

        with self._board_context_scope(board_context):
            for title, content_widget, expanded in section_specs:
                section = self._create_splitter_dropdown_section(
                    parent=self.config_monitor_splitter,
                    title=title,
                    content_widget=content_widget,
                    expanded=expanded,
                )
                self.config_monitor_sections.append(section)
                self.config_monitor_splitter.addWidget(section)
            self._setup_config_monitor_splitter_handles()
            self._sync_config_monitor_splitter_handle_states()

        return config_tab, board_context

    def _render_board_context(self, board_context: dict[str, Any], *, include_drilldown: bool = False) -> None:
        configuration_snapshot = dict(self._last_snapshot.get("configuration") or {})
        monitoring_snapshot = dict(self._last_snapshot.get("monitoring") or {})
        operator_snapshot = dict(self._last_snapshot.get("operations") or {})
        with self._board_context_scope(board_context):
            if configuration_snapshot:
                self._render_configuration_snapshot(configuration_snapshot)
                self._populate_drilldown_selectors(configuration_snapshot)
            if monitoring_snapshot:
                self._populate_trace_filter_selectors(monitoring_snapshot)
                self._render_monitoring_snapshot(monitoring_snapshot)
            if operator_snapshot:
                self._populate_operator_filter_selectors(operator_snapshot)
                self._render_operator_snapshot(operator_snapshot)
            if include_drilldown and configuration_snapshot:
                self._refresh_drilldown_views_for_context()
            self._render_operator_log()
            self._sync_board_explorer_tree_panel(board_context)
            self._render_board_canvas_surface(board_context)

    def _render_all_board_contexts(self, *, include_drilldown: bool = False) -> None:
        board_contexts = self._board_contexts_in_display_order()
        if not board_contexts and isinstance(self._primary_board_context, dict):
            board_contexts = [self._primary_board_context]
        for board_context in board_contexts:
            self._render_board_context(board_context, include_drilldown=include_drilldown)

    def _find_board_item_panel(self, tab_widget: QWidget, item_title: str) -> QWidget | None:
        normalized_title = str(item_title or "").strip().lower()
        if not normalized_title:
            return None
        for panel in self._runtime_tab_panels(tab_widget):
            panel_title = str(
                panel.property("runtime_board_item_title")
                or panel.property("runtime_widget_title")
                or ""
            ).strip().lower()
            if panel_title == normalized_title:
                return panel
        return None

    def _board_item_widget_kind_for_title(self, item_title: str) -> str:
        normalized_title = str(item_title or "").strip().lower()
        if normalized_title == "build":
            return "builder_panel"
        if normalized_title == "extensions":
            return self._EXTENSIONS_WORKSPACE_WIDGET_KIND
        return self._BOARD_ITEM_WIDGET_KIND

    def _board_extensions_source_uri(self) -> str:
        workspace = getattr(self, "extensions_section_workspace", None)
        if isinstance(workspace, ExtensionsWorkspaceWidget):
            source_uri = str(workspace.current_widget_uri() or "").strip()
            if source_uri:
                return source_uri
        return "agentsdb://127.0.0.1:2331/tools:graph_view"

    def _add_board_item_to_active_board(self, item_title: str) -> QWidget | None:
        resolved_title = str(item_title or "").strip()
        if not resolved_title:
            return None

        available_titles = self._primary_board_item_titles()
        normalized_available = {title.lower() for title in available_titles}
        if resolved_title.lower() not in normalized_available:
            return None

        self._last_selected_board_item_title = resolved_title
        self._render_all_board_canvas_surfaces()
        target_tab = self._ensure_board_runtime_target(activate=True)
        board_context = self._board_context_by_tab.get(target_tab)
        if isinstance(board_context, dict):
            with self._board_context_scope(board_context):
                target_section = self._board_section_for_title(resolved_title)
                if not isinstance(target_section, QFrame):
                    return None
                self._set_config_monitor_section_expanded(target_section, True)
                scroll_area = getattr(self, "config_monitor_scroll_area", None)
                if isinstance(scroll_area, QScrollArea):
                    QTimer.singleShot(
                        0,
                        lambda target_section=target_section, area=scroll_area: area.ensureWidgetVisible(target_section, 0, 18),
                    )
                target_state = self._config_monitor_section_state_for(target_section)
                if isinstance(target_state, dict):
                    content_widget = target_state.get("content_widget")
                    if isinstance(content_widget, QWidget):
                        return content_widget
                return target_section

        existing_panel = self._find_board_item_panel(target_tab, resolved_title)
        if existing_panel is not None:
            target_index = self.tabs.indexOf(target_tab)
            if target_index >= 0:
                self.tabs.setCurrentIndex(target_index)
            return existing_panel

        resolved_kind = self._board_item_widget_kind_for_title(resolved_title)
        source_path = ""
        board_item_title = resolved_title if resolved_kind == self._BOARD_ITEM_WIDGET_KIND else ""
        if resolved_kind == self._EXTENSIONS_WORKSPACE_WIDGET_KIND:
            source_path = self._board_extensions_source_uri()

        board_panel = self._add_widget_to_runtime_tab(
            target_tab,
            widget_kind=resolved_kind,
            title=resolved_title,
            source_path=source_path,
            board_item_title=board_item_title,
            persist=True,
        )
        target_index = self.tabs.indexOf(target_tab)
        if target_index >= 0:
            self.tabs.setCurrentIndex(target_index)
        return board_panel

    def _quick_add_selected_board_item(self) -> QWidget | None:
        item_titles = self._primary_board_item_titles()
        if not item_titles:
            QMessageBox.information(self, "Board", "Board 1 does not contain any items yet.")
            return None

        current_selection = str(getattr(self, "_last_selected_board_item_title", "") or "").strip()
        if current_selection.lower() not in {title.lower() for title in item_titles}:
            current_selection = item_titles[0]
        return self._add_board_item_to_active_board(current_selection)

    def _select_board_item_from_primary_board(self) -> QWidget | None:
        item_titles = self._primary_board_item_titles()
        if not item_titles:
            QMessageBox.information(self, "Board", "Board 1 does not contain any items yet.")
            return None

        current_selection = str(getattr(self, "_last_selected_board_item_title", "") or "").strip().lower()
        current_index = 0
        for index, item_title in enumerate(item_titles):
            if item_title.strip().lower() == current_selection:
                current_index = index
                break

        selected_item, accepted = QInputDialog.getItem(
            self,
            "Select Board Item",
            f"Item from {self._PRIMARY_BOARD_TAB_LABEL}:",
            item_titles,
            current_index,
            False,
        )
        if not accepted:
            return None
        return self._add_board_item_to_active_board(str(selected_item or "").strip())

    def _refresh_control_plane_tab_corner_menu(self) -> None:
        menu = getattr(self, "_control_tab_corner_menu", None)
        if not isinstance(menu, QMenu):
            return

        menu.clear()
        item_titles = self._primary_board_item_titles()
        selected_title = str(getattr(self, "_last_selected_board_item_title", "") or "").strip()
        if selected_title.lower() not in {title.lower() for title in item_titles}:
            selected_title = item_titles[0] if item_titles else ""

        add_board_action = menu.addAction("Add Board")
        add_board_action.triggered.connect(lambda _checked=False: self._create_board_runtime_tab(activate=True))

        add_item_label = f"Add Item: {selected_title}" if selected_title else "Add Item"
        add_item_action = menu.addAction(add_item_label)
        add_item_action.setEnabled(bool(item_titles))
        add_item_action.triggered.connect(lambda _checked=False: self._quick_add_selected_board_item())

        select_item_action = menu.addAction(f"Select Item from {self._PRIMARY_BOARD_TAB_LABEL}...")
        select_item_action.setEnabled(bool(item_titles))
        select_item_action.triggered.connect(lambda _checked=False: self._select_board_item_from_primary_board())

        if item_titles:
            menu.addSeparator()
            direct_items_menu = menu.addMenu(f"{self._PRIMARY_BOARD_TAB_LABEL} Items")
            for item_title in item_titles:
                item_action = direct_items_menu.addAction(item_title)
                item_action.triggered.connect(
                    lambda _checked=False, selected_item=item_title: self._add_board_item_to_active_board(selected_item)
                )

    def _ensure_control_plane_tab_corner_widget(self) -> None:
        tabs_widget = getattr(self, "tabs", None)
        if not isinstance(tabs_widget, QTabWidget):
            return

        corner_widget = getattr(self, "_control_tab_corner_widget", None)
        add_button = getattr(self, "_control_tab_corner_add_button", None)
        corner_menu = getattr(self, "_control_tab_corner_menu", None)

        rebuild_corner = (
            not isinstance(corner_widget, QWidget)
            or corner_widget.parent() is not tabs_widget
            or not isinstance(add_button, QToolButton)
            or not isinstance(corner_menu, QMenu)
        )
        if rebuild_corner:
            corner_widget = QWidget(tabs_widget)
            corner_layout = QHBoxLayout(corner_widget)
            corner_layout.setContentsMargins(8, 0, 4, 0)
            corner_layout.setSpacing(0)

            add_button = QToolButton(corner_widget)
            add_button.setObjectName("controlTabCornerAddButton")
            add_button.setCursor(Qt.PointingHandCursor)
            add_button.setAutoRaise(True)
            add_button.setIcon(_icon("plus_custombar_24.svg"))
            add_button.setIconSize(QSize(16, 16))
            add_button.setPopupMode(QToolButton.InstantPopup)

            corner_menu = QMenu(add_button)
            corner_menu.aboutToShow.connect(self._refresh_control_plane_tab_corner_menu)
            add_button.setMenu(corner_menu)

            corner_layout.addWidget(add_button, 0, Qt.AlignRight | Qt.AlignVCenter)

            self._control_tab_corner_widget = corner_widget
            self._control_tab_corner_add_button = add_button
            self._control_tab_corner_menu = corner_menu

        tabs_widget.setCornerWidget(corner_widget, Qt.TopRightCorner)
        self._apply_control_plane_tab_corner_widget_style()
        self._refresh_control_plane_tab_corner_menu()

    def eventFilter(self, obj, event):  # noqa: N802
        if callable(self._external_extensions_tab_text_setter()):
            return super().eventFilter(obj, event)
        if hasattr(self, "tabs") and obj is self.tabs.tabBar():
            event_type = event.type()
            if event_type == QEvent.MouseMove:
                if hasattr(event, "position"):
                    pos = event.position().toPoint()
                else:
                    pos = event.pos()
                self._start_control_plane_tab_hover_marquee(self.tabs.tabBar().tabAt(pos))
            elif event_type in (QEvent.Leave, QEvent.MouseButtonPress):
                self._stop_control_plane_tab_hover_marquee()
        return super().eventFilter(obj, event)

    def _control_plane_settings(self) -> QSettings:
        try:
            settings = QSettings(MainAIEditor.ORG_NAME, MainAIEditor.APP_NAME)
        except Exception:
            settings = QSettings()
        settings.setFallbacksEnabled(False)
        return settings

    def _load_operator_filter_preferences(self) -> dict[str, str]:
        settings = self._control_plane_settings()
        prefix = self._OPERATOR_FILTER_SETTINGS_PREFIX
        return {
            "status": str(settings.value(f"{prefix}/status", "All statuses") or "All statuses"),
            "audit_type": str(settings.value(f"{prefix}/audit_type", "All action types") or "All action types"),
            "action_group": str(settings.value(f"{prefix}/action_group", "All action groups") or "All action groups"),
            "source": str(settings.value(f"{prefix}/source", "All sources") or "All sources"),
        }

    def _current_operator_filter_preferences(self) -> dict[str, str]:
        return {
            "status": self.operator_status_selector.currentText().strip() or "All statuses",
            "audit_type": self.operator_audit_selector.currentText().strip() or "All action types",
            "action_group": self.operator_group_selector.currentText().strip() or "All action groups",
            "source": self.operator_source_selector.currentText().strip() or "All sources",
        }

    def _save_operator_filter_preferences(self) -> None:
        settings = self._control_plane_settings()
        prefix = self._OPERATOR_FILTER_SETTINGS_PREFIX
        settings.setValue(f"{prefix}/status", self._operator_filter_preferences.get("status") or "All statuses")
        settings.setValue(f"{prefix}/audit_type", self._operator_filter_preferences.get("audit_type") or "All action types")
        settings.setValue(f"{prefix}/action_group", self._operator_filter_preferences.get("action_group") or "All action groups")
        settings.setValue(f"{prefix}/source", self._operator_filter_preferences.get("source") or "All sources")
        try:
            settings.sync()
        except Exception:
            pass

    def _handle_operator_filter_change(self, _text: str = "") -> None:
        board_context = self._board_context_from_object(self.sender()) or self._active_board_context()
        with self._board_context_scope(board_context):
            self._operator_filter_preferences = self._current_operator_filter_preferences()
            self._save_operator_filter_preferences()
            self._render_operator_log()

    def _build_operator_log_entry(self, message: str) -> dict[str, Any]:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        lowered_message = message.lower()
        if any(token in lowered_message for token in ("failed", "error", "missing", "unreachable", "degraded", "locked")):
            status = "fail"
        elif any(token in lowered_message for token in ("completed", "passed", "refreshed", "ready", "healthy")):
            status = "pass"
        else:
            status = "info"
        title = message.split(":", 1)[0].strip() or "operator.action"
        return {
            "timestamp": timestamp,
            "title": title,
            "summary": message,
            "source": "desktop_operator",
            "status": status,
        }

    def _load_operator_snapshot_with_context(
        self,
        *,
        previous_operations: dict[str, Any] | None = None,
        recent_action_entries: list[Any] | None = None,
    ) -> dict[str, Any]:
        try:
            if __package__:
                from .control_plane_runtime import load_operator_status_snapshot  # type: ignore
            else:
                from alde.control_plane_runtime import load_operator_status_snapshot  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from control_plane_runtime import load_operator_status_snapshot  # type: ignore
            else:
                raise

        previous_snapshot = dict(previous_operations or {})
        return load_operator_status_snapshot(
            mcp_probe=dict(previous_snapshot.get("mcp_probe") or {}),
            recent_action_entries=list(recent_action_entries or []),
        )

    def _apply_operator_snapshot(self, snapshot: dict[str, Any], *, render_log: bool = True) -> None:
        applied_snapshot = dict(snapshot or {})
        self._last_snapshot["operations"] = applied_snapshot
        for board_context in self._board_contexts_in_display_order():
            with self._board_context_scope(board_context):
                self._populate_operator_filter_selectors(applied_snapshot)
                self._render_operator_snapshot(applied_snapshot)
                if render_log:
                    self._render_operator_log()

    def _run_operator_background_task(self, *, kind: str, worker: Callable[[], dict[str, Any]]) -> None:
        if kind in self._active_operator_tasks:
            return

        self._active_operator_tasks.add(kind)

        def _invoke_worker() -> None:
            try:
                payload = dict(worker() or {})
            except Exception as exc:
                payload = {
                    "kind": kind,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            payload.setdefault("kind", kind)
            try:
                self._operator_async_result_ready.emit(payload)
            except RuntimeError:
                # Widget already gone during shutdown; ignore late worker result.
                return

        Thread(target=_invoke_worker, daemon=True).start()

    def _build_operator_failure_message(self, kind: str, error_text: str) -> str:
        prefixes = {
            "health_checks": "Health checks failed",
            "queue_probe": "Queue probe failed",
            "agentsdb_probe": "AgentsDB probe failed",
            "dispatcher_probe": "Dispatcher probe failed",
            "dispatcher_repair": "Dispatcher repair failed",
            "mcp_probe": "MCP probe failed",
            "export_snapshot": "Runtime export failed",
        }
        return f"{prefixes.get(kind, 'Operator action failed')}: {error_text}"

    @Slot(object)
    def _handle_operator_async_result(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return

        kind = str(payload.get("kind") or "").strip()
        if kind:
            self._active_operator_tasks.discard(kind)

        error_text = str(payload.get("error") or "").strip()
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else None
        message = str(payload.get("message") or "").strip()

        if error_text:
            failure_message = message or self._build_operator_failure_message(kind, error_text)
            self._append_operator_log(failure_message, refresh_snapshot=False)
            if kind == "export_snapshot":
                QMessageBox.warning(self, "Control-Plane Snapshot", f"Export failed:\n{error_text}")
            return

        if kind == "export_snapshot":
            if message:
                self._append_operator_log(message, refresh_snapshot=False)
            export_path = str(payload.get("path") or "").strip()
            if export_path:
                QMessageBox.information(self, "Control-Plane Snapshot", f"Control-plane snapshot exported to:\n{export_path}")
            return

        if message:
            self._append_operator_log(
                message,
                operator_snapshot=snapshot,
                refresh_snapshot=False,
            )
        elif snapshot is not None:
            self._apply_operator_snapshot(snapshot)

    def _runtime_layout_root(self) -> Path:
        try:
            return Path(__file__).resolve().parents[2]
        except Exception:
            return Path.cwd()

    def _resolve_runtime_layout_path(self, configured_path: str | None = None) -> Path:
        raw_path = configured_path
        if raw_path is None:
            settings = self._control_plane_settings()
            raw_path = str(
                settings.value(
                    self._RUNTIME_LAYOUT_SETTINGS_PATH_KEY,
                    self._RUNTIME_LAYOUT_DEFAULT_REL_PATH,
                )
                or self._RUNTIME_LAYOUT_DEFAULT_REL_PATH
            )

        candidate = Path(str(raw_path or self._RUNTIME_LAYOUT_DEFAULT_REL_PATH).strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self._runtime_layout_root() / candidate
        return candidate

    def runtime_layout_path(self) -> str:
        return str(self._runtime_layout_path)

    def set_runtime_layout_path(self, layout_path: str) -> str:
        self._runtime_layout_path = self._resolve_runtime_layout_path(layout_path)
        self._runtime_state_last_saved_payload = ""
        settings = self._control_plane_settings()
        settings.setValue(self._RUNTIME_LAYOUT_SETTINGS_PATH_KEY, str(self._runtime_layout_path))
        try:
            settings.sync()
        except Exception:
            pass
        self._restore_runtime_tabs_state()
        self._ensure_builder_runtime_tab(activate=False, persist=False)
        self._update_runtime_layout_hint()
        self._update_code_tab_button_visibility(self.tabs.currentIndex())
        return str(self._runtime_layout_path)

    def _runtime_hint_text(self) -> str:
        path_name = self._runtime_layout_path.name or str(self._runtime_layout_path)
        return f"Runtime layout: {path_name} | {self._auto_refresh_hint_text()}"

    def _update_runtime_layout_hint(self) -> None:
        hint_label = getattr(self, "_runtime_hint_label", None)
        if hint_label is None:
            return
        hint_label.setText(self._runtime_hint_text())

    def _select_runtime_layout_path(self) -> None:
        start_path = str(self._runtime_layout_path)
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Runtime-Layout-Datei wählen",
            start_path,
            "JSON (*.json);;All files (*)",
        )
        if not selected_path:
            return
        resolved = self.set_runtime_layout_path(selected_path)
        try:
            window = self.window()
            status_getter = getattr(window, "statusBar", None)
            if callable(status_getter):
                status_bar = status_getter()
                if status_bar is not None:
                    status_bar.showMessage(f"Runtime-Layout Pfad gesetzt: {resolved}", 3500)
        except Exception:
            pass

    def _reload_runtime_layout_from_path(self) -> None:
        self._restore_runtime_tabs_state()
        self._ensure_builder_runtime_tab(activate=False, persist=False)
        self._update_runtime_layout_hint()
        self._update_code_tab_button_visibility(self.tabs.currentIndex())
        try:
            window = self.window()
            status_getter = getattr(window, "statusBar", None)
            if callable(status_getter):
                status_bar = status_getter()
                if status_bar is not None:
                    status_bar.showMessage(f"Runtime-Layout neu geladen: {self._runtime_layout_path}", 3200)
        except Exception:
            pass

    def _resolve_runtime_source_path(self, source_path: str) -> Path:
        candidate = Path(str(source_path or "").strip()).expanduser()
        if candidate.is_absolute():
            return candidate
        return self._runtime_layout_path.parent / candidate

    def _read_runtime_source_text(self, source_path: str) -> str | None:
        source = str(source_path or "").strip()
        if not source:
            return None
        try:
            resolved = self._resolve_runtime_source_path(source)
            if not resolved.is_file():
                return None
            return resolved.read_text(encoding="utf-8")
        except Exception:
            return None

    def _runtime_widget_editor(self, panel: QWidget) -> QPlainTextEdit | None:
        code_editor = panel.findChild(CodeViewer)
        if isinstance(code_editor, CodeViewer):
            return code_editor
        text_editor = panel.findChild(QPlainTextEdit)
        if isinstance(text_editor, QPlainTextEdit):
            return text_editor
        return None

    def _runtime_widget_language(self, panel: QWidget) -> str:
        widget_kind = str(panel.property("runtime_widget_kind") or "code_json").strip().lower()
        if widget_kind == self._BOARD_ITEM_WIDGET_KIND:
            return "text"
        if widget_kind == self._EXTENSIONS_WORKSPACE_WIDGET_KIND:
            return "text"
        if widget_kind == "code_yaml":
            return "yaml"
        if widget_kind == "code_python":
            return "python"
        if widget_kind == "code_markdown":
            return "markdown"
        if widget_kind == "code_toml":
            return "toml"
        if widget_kind == "text_view":
            return "text"
        return "json"

    def _normalize_runtime_source_path_for_storage(self, file_path: str) -> str:
        candidate = Path(str(file_path or "").strip()).expanduser()
        if not candidate:
            return ""
        try:
            resolved_path = candidate.resolve()
        except Exception:
            resolved_path = candidate

        try:
            base_dir = self._runtime_layout_path.parent.resolve()
            relative = resolved_path.relative_to(base_dir)
            return str(relative)
        except Exception:
            return str(resolved_path)

    def _import_runtime_widget_content(self, panel: QWidget) -> None:
        editor = self._runtime_widget_editor(panel)
        if editor is None:
            QMessageBox.information(self, "Info", "Dieses Widget unterstützt keinen Datei-Import.")
            return

        source_hint = str(panel.property("runtime_source_path") or "").strip()
        start_path = self._resolve_runtime_source_path(source_hint) if source_hint else self._runtime_layout_path.parent

        file_filter = "Text files (*.txt *.md *.json *.yaml *.yml *.py *.toml *.ini *.cfg *.log);;All files (*)"
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Widget-Inhalt importieren",
            str(start_path),
            file_filter,
        )
        if not selected_path:
            return

        try:
            imported_text = Path(selected_path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            QMessageBox.warning(self, "Fehler", f"Datei konnte nicht importiert werden: {exc}")
            return

        editor.setPlainText(imported_text)
        stored_source_path = self._normalize_runtime_source_path_for_storage(selected_path)
        panel.setProperty("runtime_source_path", stored_source_path)
        editor.setProperty("runtime_source_path", stored_source_path)
        self._schedule_runtime_state_save()

        try:
            window = self.window()
            status_getter = getattr(window, "statusBar", None)
            if callable(status_getter):
                status_bar = status_getter()
                if status_bar is not None:
                    status_bar.showMessage(f"Widget importiert: {Path(selected_path).name}", 3200)
        except Exception:
            pass

    def _export_runtime_widget_to_chat_context(
        self,
        panel: QWidget,
    ) -> None:
        editor = self._runtime_widget_editor(panel)
        if editor is None:
            QMessageBox.information(self, "Info", "Dieses Widget unterstützt keinen Export in den Chat-Kontext.")
            return

        title = str(panel.property("runtime_widget_title") or "Runtime Widget").strip() or "Runtime Widget"
        source_path = str(panel.property("runtime_source_path") or "").strip()
        language = self._runtime_widget_language(panel)

        content_text = editor.toPlainText().strip("\n")
        if not content_text.strip():
            QMessageBox.information(self, "Info", "Widget enthält keinen Inhalt zum Anhängen.")
            return

        ai_widget = self._resolve_ai_widget()
        attach_callable = getattr(ai_widget, "attach_runtime_context", None) if ai_widget is not None else None
        if callable(attach_callable):
            attached = bool(
                attach_callable(
                    title=title,
                    language=language,
                    content=content_text,
                    source_path=source_path,
                )
            )
            if attached:
                return

        QMessageBox.information(
            self,
            "Info",
            "Chat-Kontext nicht verfügbar. Bitte AI-Chat öffnen und erneut versuchen.",
        )

    def _collect_runtime_widget_text(self, panel: QWidget) -> str:
        widget_kind = str(panel.property("runtime_widget_kind") or "").strip().lower()
        if widget_kind == "agent_relation_graph":
            source_hint = str(panel.property("runtime_source_path") or "").strip()
            if source_hint:
                return source_hint
        code_editor = panel.findChild(CodeViewer)
        if isinstance(code_editor, CodeViewer):
            return code_editor.toPlainText()
        text_editor = panel.findChild(QPlainTextEdit)
        if isinstance(text_editor, QPlainTextEdit):
            return text_editor.toPlainText()
        return ""

    def _serialize_board_canvas_element(self, element: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in ("id", "title", "preview", "source_kind", "source_ref", "action_text"):
            value = str(element.get(key) or "").strip()
            if value:
                payload[key] = value
        position = self._board_canvas_element_position(element)
        if position is not None:
            payload["x"] = round(position[0], 3)
            payload["y"] = round(position[1], 3)
        return payload

    def _serialize_board_context_state(self, board_context: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(board_context, dict):
            return {
                "canvas_elements": [],
                "selected_element_id": "",
            }

        elements = self._ensure_board_canvas_elements(board_context)
        return {
            "canvas_elements": [
                self._serialize_board_canvas_element(element)
                for element in elements
                if isinstance(element, Mapping)
            ],
            "selected_element_id": str(board_context.get("board_canvas_selected_element_id") or "").strip(),
        }

    def _restore_board_context_state(
        self,
        board_context: dict[str, Any] | None,
        payload: Mapping[str, Any] | None,
    ) -> None:
        if not isinstance(board_context, dict):
            return
        if not isinstance(payload, Mapping):
            board_context["board_canvas_elements"] = []
            board_context["board_canvas_selected_element_id"] = ""
            self._ensure_board_canvas_elements(board_context)
            return

        restored_elements: list[dict[str, Any]] = []
        for element_payload in payload.get("canvas_elements") or []:
            if not isinstance(element_payload, Mapping):
                continue

            restored_element: dict[str, Any] = {}
            element_id = str(element_payload.get("id") or "").strip()
            source_kind = str(element_payload.get("source_kind") or "").strip().lower()
            source_ref = str(element_payload.get("source_ref") or "").strip()
            title = str(element_payload.get("title") or "").strip()
            preview = str(element_payload.get("preview") or "").strip()
            action_text = str(element_payload.get("action_text") or "").strip()

            if not element_id:
                if source_kind and source_ref:
                    element_id = self._board_canvas_element_id(source_kind, source_ref)
                elif title:
                    element_id = self._board_canvas_element_id("board", title)
            if not element_id:
                continue

            restored_kind, _separator, restored_ref = element_id.partition("::")
            restored_element["id"] = element_id
            restored_element["source_kind"] = source_kind or restored_kind or "board"
            restored_element["source_ref"] = source_ref or restored_ref or title
            if title:
                restored_element["title"] = title
            if preview:
                restored_element["preview"] = preview
            if action_text:
                restored_element["action_text"] = action_text

            x_value = self._board_canvas_numeric_value(element_payload.get("x"))
            y_value = self._board_canvas_numeric_value(element_payload.get("y"))
            if x_value is not None:
                restored_element["x"] = max(0.0, float(x_value))
            if y_value is not None:
                restored_element["y"] = max(0.0, float(y_value))
            restored_elements.append(restored_element)

        board_context["board_canvas_elements"] = restored_elements
        board_context["board_canvas_selected_element_id"] = str(payload.get("selected_element_id") or "").strip()
        self._ensure_board_canvas_elements(board_context)

    def _serialize_runtime_widget_panel(self, panel: QWidget) -> dict[str, Any]:
        widget_kind = str(panel.property("runtime_widget_kind") or "code_json").strip().lower() or "code_json"
        title = str(panel.property("runtime_widget_title") or "runtime_widget")
        if widget_kind == self._BOARD_ITEM_WIDGET_KIND:
            board_item_title = str(panel.property("runtime_board_item_title") or title).strip() or title
            return {
                "kind": widget_kind,
                "title": title,
                "board_item_title": board_item_title,
            }
        source_path = str(panel.property("runtime_source_path") or "").strip()
        content = self._collect_runtime_widget_text(panel)

        payload: dict[str, Any] = {
            "kind": widget_kind,
            "title": title,
        }
        if source_path:
            payload["source_path"] = source_path
            if self._read_runtime_source_text(source_path) is None and content:
                payload["content"] = content
        else:
            payload["content"] = content
        return payload

    def _serialize_runtime_tabs_state(self) -> dict[str, Any]:
        serialized_tabs: list[dict[str, Any]] = []
        active_runtime_tab = ""
        active_tab = ""
        primary_board_state = self._serialize_board_context_state(self._primary_board_context)

        for index in range(self.tabs.count()):
            tab_widget = self.tabs.widget(index)
            tab_name = self._control_plane_tab_full_text(index)
            if self.tabs.currentWidget() is tab_widget:
                active_tab = tab_name

            if tab_widget in self._board_context_by_tab:
                if tab_widget is getattr(self, "_config_tab", None):
                    continue
                serialized_tabs.append(
                    {
                        "tab_kind": "board",
                        "name": tab_name,
                        "board_state": self._serialize_board_context_state(self._board_context_by_tab.get(tab_widget)),
                    }
                )
                continue

            if tab_widget not in self._runtime_tab_records:
                continue

            record = self._runtime_tab_records.get(tab_widget) or {}
            splitter = record.get("splitter")
            default_widget_kind = self._runtime_tab_default_widget_kind(tab_widget)

            widget_payloads: list[dict[str, Any]] = []
            if isinstance(splitter, QSplitter):
                for widget_index in range(splitter.count()):
                    panel = splitter.widget(widget_index)
                    if isinstance(panel, QWidget) and panel.objectName() == "runtimeWidgetPanel":
                        widget_payloads.append(self._serialize_runtime_widget_panel(panel))

            serialized_tabs.append(
                {
                    "tab_kind": "runtime",
                    "name": self._control_plane_tab_full_text(index),
                    "default_widget_kind": default_widget_kind,
                    "widgets": widget_payloads,
                }
            )

            role_value = str(tab_widget.property("runtime_role") or "").strip()
            if role_value:
                serialized_tabs[-1]["role"] = role_value

            if self.tabs.currentWidget() is tab_widget:
                active_runtime_tab = self._control_plane_tab_full_text(index)

        return {
            "schema": self._RUNTIME_LAYOUT_SCHEMA,
            "primary_board": primary_board_state,
            "tabs": serialized_tabs,
            "active_tab": active_tab,
            "active_runtime_tab": active_runtime_tab,
        }

    def _schedule_runtime_state_save(self) -> None:
        if self._runtime_restore_active:
            return
        if isinstance(getattr(self, "_runtime_state_save_timer", None), QTimer):
            self._runtime_state_save_timer.start()

    def persist_runtime_tabs_state(self, *, force: bool = False) -> Path | None:
        if self._runtime_restore_active and not force:
            return None

        payload = self._serialize_runtime_tabs_state()
        serialized_payload = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        if not force and serialized_payload == self._runtime_state_last_saved_payload:
            return self._runtime_layout_path

        try:
            self._runtime_layout_path.parent.mkdir(parents=True, exist_ok=True)
            if not force and self._runtime_layout_path.exists():
                existing_payload = self._runtime_layout_path.read_text(encoding="utf-8")
                if existing_payload == serialized_payload:
                    self._runtime_state_last_saved_payload = serialized_payload
                    return self._runtime_layout_path

            temp_path = self._runtime_layout_path.with_suffix(self._runtime_layout_path.suffix + ".tmp")
            temp_path.write_text(serialized_payload, encoding="utf-8")
            temp_path.replace(self._runtime_layout_path)
            self._runtime_state_last_saved_payload = serialized_payload
        except Exception:
            return None

        settings = self._control_plane_settings()
        settings.setValue(self._RUNTIME_LAYOUT_SETTINGS_PATH_KEY, str(self._runtime_layout_path))
        try:
            settings.sync()
        except Exception:
            pass
        return self._runtime_layout_path

    def _clear_runtime_tabs(self) -> None:
        runtime_tabs = [
            self.tabs.widget(index)
            for index in range(self.tabs.count())
            if self.tabs.widget(index) in self._runtime_tab_records
        ]
        for tab_widget in runtime_tabs:
            if tab_widget is self._builder_runtime_tab:
                self._builder_runtime_tab = None
            self._dispose_runtime_tab(tab_widget, persist=False)

    def _clear_dynamic_board_tabs(self) -> None:
        primary_board_tab = getattr(self, "_config_tab", None)
        board_tabs = [
            self.tabs.widget(index)
            for index in range(self.tabs.count())
            if self.tabs.widget(index) in self._board_context_by_tab and self.tabs.widget(index) is not primary_board_tab
        ]
        for tab_widget in board_tabs:
            self._dispose_board_tab(tab_widget, persist=False)

    def _restore_runtime_tabs_state(self) -> None:
        path = self._runtime_layout_path
        if not path.exists():
            self._runtime_state_last_saved_payload = json.dumps(
                self._serialize_runtime_tabs_state(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            return

        try:
            existing_payload_text = path.read_text(encoding="utf-8")
            payload = json.loads(existing_payload_text)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        tabs_payload = payload.get("tabs")
        if not isinstance(tabs_payload, list):
            return

        self._runtime_restore_active = True
        try:
            self._clear_dynamic_board_tabs()
            self._clear_runtime_tabs()
            self._restore_board_context_state(self._primary_board_context, payload.get("primary_board"))

            for tab_entry in tabs_payload:
                if not isinstance(tab_entry, dict):
                    continue

                tab_kind = str(tab_entry.get("tab_kind") or "runtime").strip().lower() or "runtime"
                tab_name = str(tab_entry.get("name") or "").strip() or f"Runtime {self._runtime_tab_counter + 1}"
                if tab_kind == "board":
                    board_tab = self._create_board_runtime_tab(
                        activate=False,
                        board_title=tab_name,
                        persist=False,
                    )
                    board_context = self._board_context_by_tab.get(board_tab)
                    self._restore_board_context_state(board_context, tab_entry.get("board_state"))
                    self._render_board_canvas_surface(board_context)
                    continue

                tab_widget = self.create_runtime_tab(
                    tab_name,
                    activate=False,
                    add_default_widget=False,
                    persist=False,
                )

                role_value = str(tab_entry.get("role") or "").strip().lower()
                if role_value:
                    tab_widget.setProperty("runtime_role", role_value)
                    if role_value == "builder":
                        self._builder_runtime_tab = tab_widget

                record = self._runtime_tab_records.get(tab_widget) or {}
                default_widget_kind = str(tab_entry.get("default_widget_kind") or "code_json")
                self._set_runtime_tab_default_widget_kind(
                    tab_widget,
                    default_widget_kind,
                    persist=False,
                )

                restored_any = False
                widget_entries = tab_entry.get("widgets")
                if isinstance(widget_entries, list):
                    for widget_entry in widget_entries:
                        if not isinstance(widget_entry, dict):
                            continue
                        widget_kind = str(widget_entry.get("kind") or "code_json")
                        widget_title = str(widget_entry.get("title") or "").strip() or None
                        widget_content = str(widget_entry.get("content") or "")
                        widget_source_path = str(widget_entry.get("source_path") or "")
                        board_item_title = str(widget_entry.get("board_item_title") or "").strip()
                        self._add_widget_to_runtime_tab(
                            tab_widget,
                            widget_kind=widget_kind,
                            title=widget_title,
                            content=widget_content,
                            source_path=widget_source_path,
                            board_item_title=board_item_title,
                            persist=False,
                        )
                        restored_any = True

                if not restored_any:
                    self._add_widget_to_runtime_tab(
                        tab_widget,
                        widget_kind=default_widget_kind,
                        persist=False,
                    )

            active_name = str(payload.get("active_tab") or payload.get("active_runtime_tab") or "").strip().lower()
            if active_name:
                for index in range(self.tabs.count()):
                    tab_widget = self.tabs.widget(index)
                    if self._control_plane_tab_full_text(index).strip().lower() == active_name:
                        self.tabs.setCurrentIndex(index)
                        break
        finally:
            self._runtime_restore_active = False

        self._runtime_tab_counter = max(self._runtime_tab_counter, len(self._runtime_tab_records))
        self._ensure_builder_runtime_tab(activate=False, persist=False)
        serialized_payload = json.dumps(
            self._serialize_runtime_tabs_state(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        self._runtime_state_last_saved_payload = serialized_payload
        if serialized_payload != existing_payload_text:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = path.with_suffix(path.suffix + ".tmp")
                temp_path.write_text(serialized_payload, encoding="utf-8")
                temp_path.replace(path)
            except Exception:
                pass

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.primary_splitter = self._create_viewport_splitter(self)
        self.primary_splitter.setObjectName("controlPrimarySplitter")

        hero = QFrame(self)
        hero.setObjectName("controlHero")
        hero.setMinimumHeight(0)
        hero.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(12, 12, 12, 12)
        hero_layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)

        title = QLabel("Agentic Control Plane", hero)
        title.setObjectName("controlTitle")
        subtitle = QLabel(
            "Industrial workspace for agent configuration, workflow governance, and runtime monitoring.",
            hero,
        )
        subtitle.setObjectName("controlSubtitle")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        header_row.addLayout(title_box, 1)

        header_meta = QVBoxLayout()
        header_meta.setContentsMargins(0, 0, 0, 0)
        header_meta.setSpacing(4)
        self._last_refresh_label = QLabel("Refresh pending", hero)
        self._last_refresh_label.setObjectName("controlMeta")
        self._runtime_hint_label = QLabel(self._runtime_hint_text(), hero)
        self._runtime_hint_label.setObjectName("controlMeta")
        header_meta.addWidget(self._last_refresh_label, 0, Qt.AlignRight)
        header_meta.addWidget(self._runtime_hint_label, 0, Qt.AlignRight)
        header_row.addLayout(header_meta)

        self.btn_refresh = ToolButton(
            "reload_.svg",
            "Control Plane aktualisieren",
            slot=self._refresh_from_panel,
            parent=hero,
        )
        header_row.addWidget(self.btn_refresh, 0, Qt.AlignTop)

        self.btn_add_runtime_tab = ToolButton(
            "add_tab_dock.svg",
            f"Neuen {self._BUILD_RUNTIME_TAB_LABEL}-Runtime-Tab anlegen (Builder-Start)",
            slot=self._open_new_runtime_tab,
            parent=hero,
        )
        header_row.addWidget(self.btn_add_runtime_tab, 0, Qt.AlignTop)

        self.btn_select_runtime_layout = ToolButton(
            "open_file.svg",
            "Runtime-Layout Pfad wählen",
            slot=self._select_runtime_layout_path,
            parent=hero,
        )
        header_row.addWidget(self.btn_select_runtime_layout, 0, Qt.AlignTop)

        self.btn_reload_runtime_layout = ToolButton(
            "reload_.svg",
            "Runtime-Layout neu laden",
            slot=self._reload_runtime_layout_from_path,
            parent=hero,
        )
        header_row.addWidget(self.btn_reload_runtime_layout, 0, Qt.AlignTop)

        hero_layout.addLayout(header_row)

        metrics_row = QHBoxLayout()
        metrics_row.setContentsMargins(0, 0, 0, 0)
        metrics_row.setSpacing(8)
        for metric_key, metric_label in (
            ("agents", "Agents"),
            ("workflows", "Workflows"),
            ("sessions", "Sessions"),
            ("failures", "Failures"),
        ):
            card, value_label = self._create_metric_card(metric_label)
            self._metric_labels[metric_key] = value_label
            metrics_row.addWidget(card, 1)
        hero_layout.addLayout(metrics_row)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("controlPlaneTabs")
        self.tabs.setMinimumSize(0, 0)
        self.tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(Qt.ElideRight)
        self.tabs.tabBar().setExpanding(False)
        self._setup_control_plane_tab_bar_interactions()

        config_tab = QWidget(self.tabs)
        self._config_tab = config_tab
        config_tab.setObjectName("BoardTabWidget")
        config_tab.setProperty("board_tab_widget", True)
        config_tab.setMinimumSize(0, 0)
        config_tab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        config_layout = QVBoxLayout(config_tab)
        config_layout.setContentsMargins(0, 0, 0, 0)
        config_layout.setSpacing(0)

        self.config_summary_view = QTextBrowser(config_tab)
        self.config_summary_view.setObjectName("controlBrowser")
        self.config_summary_view.setOpenExternalLinks(False)
        self.config_summary_view.setMinimumHeight(0)
        self.config_summary_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.config_manifest_view = QTextBrowser(config_tab)
        self.config_manifest_view.setObjectName("controlBrowser")
        self.config_manifest_view.setOpenExternalLinks(False)
        self.config_manifest_view.setMinimumHeight(0)
        self.config_manifest_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.monitor_summary_view = QTextBrowser(config_tab)
        self.monitor_summary_view.setObjectName("controlBrowser")
        self.monitor_summary_view.setOpenExternalLinks(False)
        self.monitor_summary_view.setMinimumHeight(0)
        self.monitor_summary_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.monitor_filter_panel = QWidget(config_tab)
        self.monitor_filter_panel.setMinimumSize(0, 0)
        self.monitor_filter_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        drilldown_layout = QVBoxLayout(self.monitor_filter_panel)
        drilldown_layout.setContentsMargins(0, 0, 0, 0)
        drilldown_layout.setSpacing(6)

        drilldown_form = QFormLayout()
        drilldown_form.setContentsMargins(0, 0, 0, 0)
        drilldown_form.setSpacing(8)
        drilldown_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        agent_label = QLabel("Agent", config_tab)
        agent_label.setObjectName("controlMeta")

        self.agent_selector = QComboBox(config_tab)
        self.agent_selector.setObjectName("controlSelector")
        self.agent_selector.setMinimumContentsLength(10)
        self.agent_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.agent_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.agent_selector.currentTextChanged.connect(self._refresh_drilldown_views)
        drilldown_form.addRow(agent_label, self.agent_selector)

        workflow_label = QLabel("Workflow", config_tab)
        workflow_label.setObjectName("controlMeta")

        self.workflow_selector = QComboBox(config_tab)
        self.workflow_selector.setObjectName("controlSelector")
        self.workflow_selector.setMinimumContentsLength(10)
        self.workflow_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.workflow_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.workflow_selector.currentTextChanged.connect(self._refresh_drilldown_views)
        drilldown_form.addRow(workflow_label, self.workflow_selector)

        drilldown_layout.addLayout(drilldown_form)

        trace_filter_form = QFormLayout()
        trace_filter_form.setContentsMargins(0, 0, 0, 0)
        trace_filter_form.setSpacing(8)
        trace_filter_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        trace_agent_label = QLabel("Trace Agent", config_tab)
        trace_agent_label.setObjectName("controlMeta")
        self.trace_agent_selector = QComboBox(config_tab)
        self.trace_agent_selector.setObjectName("controlSelector")
        self.trace_agent_selector.setMinimumContentsLength(10)
        self.trace_agent_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.trace_agent_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.trace_agent_selector.currentTextChanged.connect(self._refresh_monitoring_views)
        trace_filter_form.addRow(trace_agent_label, self.trace_agent_selector)

        trace_workflow_label = QLabel("Trace Workflow", config_tab)
        trace_workflow_label.setObjectName("controlMeta")
        self.trace_workflow_selector = QComboBox(config_tab)
        self.trace_workflow_selector.setObjectName("controlSelector")
        self.trace_workflow_selector.setMinimumContentsLength(10)
        self.trace_workflow_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.trace_workflow_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.trace_workflow_selector.currentTextChanged.connect(self._refresh_monitoring_views)
        trace_filter_form.addRow(trace_workflow_label, self.trace_workflow_selector)

        trace_tool_label = QLabel("Trace Tool", config_tab)
        trace_tool_label.setObjectName("controlMeta")
        self.trace_tool_selector = QComboBox(config_tab)
        self.trace_tool_selector.setObjectName("controlSelector")
        self.trace_tool_selector.setMinimumContentsLength(10)
        self.trace_tool_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.trace_tool_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.trace_tool_selector.currentTextChanged.connect(self._refresh_monitoring_views)
        trace_filter_form.addRow(trace_tool_label, self.trace_tool_selector)

        trace_handoff_label = QLabel("Trace Handoff", config_tab)
        trace_handoff_label.setObjectName("controlMeta")
        self.trace_handoff_selector = QComboBox(config_tab)
        self.trace_handoff_selector.setObjectName("controlSelector")
        self.trace_handoff_selector.setMinimumContentsLength(10)
        self.trace_handoff_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.trace_handoff_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.trace_handoff_selector.currentTextChanged.connect(self._refresh_monitoring_views)
        trace_filter_form.addRow(trace_handoff_label, self.trace_handoff_selector)

        drilldown_layout.addLayout(trace_filter_form)

        self.tree_stream_panel = QFrame(config_tab)
        self.tree_stream_panel.setObjectName("controlMetricCard")
        self.tree_stream_panel.setMinimumSize(0, 0)
        self.tree_stream_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        tree_stream_layout = QVBoxLayout(self.tree_stream_panel)
        tree_stream_layout.setContentsMargins(12, 10, 12, 10)
        tree_stream_layout.setSpacing(6)

        tree_stream_title = QLabel("<b>Explorer Tree Stream</b>", self.tree_stream_panel)
        tree_stream_title.setObjectName("controlMeta")
        tree_stream_layout.addWidget(tree_stream_title)

        tree_stream_form = QFormLayout()
        tree_stream_form.setContentsMargins(0, 0, 0, 0)
        tree_stream_form.setSpacing(6)
        tree_stream_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        tree_stream_transport_label = QLabel("Transport", self.tree_stream_panel)
        tree_stream_transport_label.setObjectName("controlMeta")
        self.tree_stream_transport_value = QLabel("n/a", self.tree_stream_panel)
        self.tree_stream_transport_value.setObjectName("controlMetricLabel")
        self.tree_stream_transport_value.setWordWrap(True)
        tree_stream_form.addRow(tree_stream_transport_label, self.tree_stream_transport_value)

        tree_stream_state_label = QLabel("State", self.tree_stream_panel)
        tree_stream_state_label.setObjectName("controlMeta")
        self.tree_stream_state_value = QLabel("n/a", self.tree_stream_panel)
        self.tree_stream_state_value.setObjectName("controlMetricLabel")
        self.tree_stream_state_value.setWordWrap(True)
        tree_stream_form.addRow(tree_stream_state_label, self.tree_stream_state_value)

        tree_stream_event_label = QLabel("Cursor", self.tree_stream_panel)
        tree_stream_event_label.setObjectName("controlMeta")
        self.tree_stream_event_value = QLabel("n/a", self.tree_stream_panel)
        self.tree_stream_event_value.setObjectName("controlMetricLabel")
        self.tree_stream_event_value.setWordWrap(True)
        tree_stream_form.addRow(tree_stream_event_label, self.tree_stream_event_value)

        tree_stream_retry_label = QLabel("Reconnect", self.tree_stream_panel)
        tree_stream_retry_label.setObjectName("controlMeta")
        self.tree_stream_retry_value = QLabel("n/a", self.tree_stream_panel)
        self.tree_stream_retry_value.setObjectName("controlMetricLabel")
        self.tree_stream_retry_value.setWordWrap(True)
        tree_stream_form.addRow(tree_stream_retry_label, self.tree_stream_retry_value)

        tree_stream_updated_label = QLabel("Updated", self.tree_stream_panel)
        tree_stream_updated_label.setObjectName("controlMeta")
        self.tree_stream_updated_value = QLabel("n/a", self.tree_stream_panel)
        self.tree_stream_updated_value.setObjectName("controlMetricLabel")
        self.tree_stream_updated_value.setWordWrap(True)
        tree_stream_form.addRow(tree_stream_updated_label, self.tree_stream_updated_value)

        tree_stream_error_label = QLabel("Last Error", self.tree_stream_panel)
        tree_stream_error_label.setObjectName("controlMeta")
        self.tree_stream_error_value = QLabel("none", self.tree_stream_panel)
        self.tree_stream_error_value.setObjectName("controlMetricLabel")
        self.tree_stream_error_value.setWordWrap(True)
        tree_stream_form.addRow(tree_stream_error_label, self.tree_stream_error_value)

        tree_stream_layout.addLayout(tree_stream_form)
        drilldown_layout.addWidget(self.tree_stream_panel)

        self.board_explorer_tree_panel, self.board_explorer_tree_widget, self.board_explorer_tree_status_label = self._create_board_explorer_tree_panel(config_tab)
        drilldown_layout.addWidget(self.board_explorer_tree_panel, 1)

        self.board_canvas_panel, self.board_canvas_view, self.board_canvas_status_label = self._create_board_canvas_panel(config_tab)
        self.board_canvas_elements: list[dict[str, Any]] = []
        self.board_canvas_selected_element_id = ""

        detail_action_row = QHBoxLayout()
        detail_action_row.setContentsMargins(0, 0, 0, 0)
        detail_action_row.setSpacing(8)

        self.btn_refresh_detail = ToolButton(
            "reload_.svg",
            "Monitor-Detail aktualisieren",
            slot=self._refresh_drilldown_views,
            parent=config_tab,
        )
        self.btn_refresh_detail.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        detail_action_row.addWidget(self.btn_refresh_detail, 0)
        detail_action_row.addStretch(1)
        drilldown_layout.addLayout(detail_action_row)

        self.monitor_detail_view = QTextBrowser(config_tab)
        self.monitor_detail_view.setObjectName("controlBrowser")
        self.monitor_detail_view.setOpenExternalLinks(False)
        self.monitor_detail_view.setMinimumHeight(0)
        self.monitor_detail_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.monitor_timeline_view = QTextBrowser(config_tab)
        self.monitor_timeline_view.setObjectName("controlBrowser")
        self.monitor_timeline_view.setOpenExternalLinks(False)
        self.monitor_timeline_view.setMinimumHeight(0)
        self.monitor_timeline_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.monitor_trace_view = QTextBrowser(config_tab)
        self.monitor_trace_view.setObjectName("controlBrowser")
        self.monitor_trace_view.setOpenExternalLinks(False)
        self.monitor_trace_view.setMinimumHeight(0)
        self.monitor_trace_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.config_monitor_scroll_area = QScrollArea(config_tab)
        self.config_monitor_scroll_area.setObjectName("controlMonitoringScrollArea")
        self.config_monitor_scroll_area.setWidgetResizable(True)
        self.config_monitor_scroll_area.setFrameShape(QFrame.NoFrame)
        self.config_monitor_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.config_monitor_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.config_monitor_scroll_area.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

        self.config_monitor_scroll_content = QWidget(self.config_monitor_scroll_area)
        self.config_monitor_scroll_content.setMinimumSize(0, 0)
        self.config_monitor_scroll_content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        config_monitor_scroll_layout = QVBoxLayout(self.config_monitor_scroll_content)
        config_monitor_scroll_layout.setContentsMargins(0, 3, 0, 0)
        config_monitor_scroll_layout.setSpacing(0)

        self.config_monitor_host_splitter = None
        self.config_monitor_host_widget = self.config_monitor_scroll_content

        self.config_monitor_splitter = self._create_viewport_splitter(self.config_monitor_scroll_content)
        self.config_monitor_splitter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.config_monitor_splitter_anchor = QWidget(self.config_monitor_splitter)
        self.config_monitor_splitter_anchor.setObjectName("controlSplitterAnchor")
        self.config_monitor_splitter_anchor.setMinimumHeight(0)
        self.config_monitor_splitter_anchor.setMaximumHeight(0)
        self.config_monitor_splitter_anchor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.config_monitor_splitter.addWidget(self.config_monitor_splitter_anchor)

        self.config_monitor_sections: list[QFrame] = []
        self._config_monitor_section_state: dict[QFrame, dict[str, Any]] = {}
        self._config_monitor_splitter_handle_controls: dict[int, dict[str, Any]] = {}

        self.operator_actions_panel = QWidget(config_tab)
        self.operator_actions_panel.setMinimumSize(0, 0)
        self.operator_actions_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        operator_actions_layout = QVBoxLayout(self.operator_actions_panel)
        operator_actions_layout.setContentsMargins(0, 0, 0, 0)
        operator_actions_layout.setSpacing(8)

        operator_actions_grid = QGridLayout()
        operator_actions_grid.setContentsMargins(0, 0, 0, 0)
        operator_actions_grid.setHorizontalSpacing(8)
        operator_actions_grid.setVerticalSpacing(8)

        action_specs = [
            ("reload_.svg", "", "Alle Operator-Checks aktualisieren", self._run_operator_health_checks, "btn_refresh_health"),
            ("swap_horiz_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg", "", "Queue-Backend pruefen", self._probe_queue_health, "btn_probe_queue"),
            ("check_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg", "", "AgentsDB-Socket pruefen", self._probe_agentsdb_health, "btn_probe_agentsdb"),
            ("check_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg", "", "Dispatcher-Store pruefen", self._probe_dispatcher_health, "btn_probe_dispatcher"),
            ("settings_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg", "", "Dispatcher-Store reparieren", self._repair_dispatcher_store, "btn_repair_dispatcher"),
            ("deployed_code.svg", "", "MCP-Konfiguration pruefen", self._probe_mcp_health, "btn_probe_mcp"),
            ("file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg", "", "Control-Plane-Snapshot exportieren", self._export_runtime_snapshot_report, "btn_export_runtime"),
        ]
        for index, (icon_name, label_text, tooltip, slot, attr_name) in enumerate(action_specs):
            tile, button = self._create_operator_action_tile(
                icon_name,
                label_text,
                tooltip,
                slot,
                self.operator_actions_panel,
            )
            setattr(self, attr_name, button)
            operator_actions_grid.addWidget(tile, index // 3, index % 3)
        operator_actions_layout.addLayout(operator_actions_grid)

        self.operator_filters_panel = QWidget(config_tab)
        self.operator_filters_panel.setMinimumSize(0, 0)
        self.operator_filters_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        operator_filters_layout = QVBoxLayout(self.operator_filters_panel)
        operator_filters_layout.setContentsMargins(0, 0, 0, 0)
        operator_filters_layout.setSpacing(6)

        operator_filter_form = QFormLayout()
        operator_filter_form.setContentsMargins(0, 0, 0, 0)
        operator_filter_form.setSpacing(8)
        operator_filter_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        operator_status_label = QLabel("Action Status", self.operator_filters_panel)
        operator_status_label.setObjectName("controlMeta")
        self.operator_status_selector = QComboBox(self.operator_filters_panel)
        self.operator_status_selector.setObjectName("controlSelector")
        self.operator_status_selector.setMinimumContentsLength(10)
        self.operator_status_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.operator_status_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.operator_status_selector.currentTextChanged.connect(self._handle_operator_filter_change)
        operator_filter_form.addRow(operator_status_label, self.operator_status_selector)

        operator_audit_label = QLabel("Action Type", self.operator_filters_panel)
        operator_audit_label.setObjectName("controlMeta")
        self.operator_audit_selector = QComboBox(self.operator_filters_panel)
        self.operator_audit_selector.setObjectName("controlSelector")
        self.operator_audit_selector.setMinimumContentsLength(10)
        self.operator_audit_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.operator_audit_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.operator_audit_selector.currentTextChanged.connect(self._handle_operator_filter_change)
        operator_filter_form.addRow(operator_audit_label, self.operator_audit_selector)

        operator_group_label = QLabel("Action Group", self.operator_filters_panel)
        operator_group_label.setObjectName("controlMeta")
        self.operator_group_selector = QComboBox(self.operator_filters_panel)
        self.operator_group_selector.setObjectName("controlSelector")
        self.operator_group_selector.setMinimumContentsLength(10)
        self.operator_group_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.operator_group_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.operator_group_selector.currentTextChanged.connect(self._handle_operator_filter_change)
        operator_filter_form.addRow(operator_group_label, self.operator_group_selector)

        operator_source_label = QLabel("Action Source", self.operator_filters_panel)
        operator_source_label.setObjectName("controlMeta")
        self.operator_source_selector = QComboBox(self.operator_filters_panel)
        self.operator_source_selector.setObjectName("controlSelector")
        self.operator_source_selector.setMinimumContentsLength(10)
        self.operator_source_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.operator_source_selector.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.operator_source_selector.currentTextChanged.connect(self._handle_operator_filter_change)
        operator_filter_form.addRow(operator_source_label, self.operator_source_selector)
        operator_filters_layout.addLayout(operator_filter_form)

        self.operator_summary_view = QTextBrowser(config_tab)
        self.operator_summary_view.setObjectName("controlBrowser")
        self.operator_summary_view.setOpenExternalLinks(False)
        self.operator_summary_view.setMinimumHeight(0)
        self.operator_summary_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        self.operator_log_view = QTextBrowser(config_tab)
        self.operator_log_view.setObjectName("controlBrowser")
        self.operator_log_view.setOpenExternalLinks(False)
        self.operator_log_view.setMinimumHeight(0)
        self.operator_log_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

        # ── Build section panel ─────────────────────────────────────────────
        self.build_section_panel = self._create_board_build_section(config_tab)
        self.build_section_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

        # ── Extensions section panel ────────────────────────────────────────
        self.extensions_section_panel = QWidget(config_tab)
        self.extensions_section_panel.setMinimumSize(0, 0)
        self.extensions_section_panel.setMinimumHeight(380)
        self.extensions_section_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        _ext_section_layout = QVBoxLayout(self.extensions_section_panel)
        _ext_section_layout.setContentsMargins(0, 0, 0, 0)
        _ext_section_layout.setSpacing(0)
        self.extensions_section_workspace = ExtensionsWorkspaceWidget(
            accent=dict(self._accent),
            base=dict(self._base),
            parent=self.extensions_section_panel,
            source_uri="agentsdb://127.0.0.1:2331/tools:graph_view",
            control_plane_widget_ref=None,
        )
        self.extensions_section_workspace.setMinimumHeight(320)
        self.extensions_section_workspace.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        _ext_section_layout.addWidget(self.extensions_section_workspace, 1)

        self.config_monitor_threat_flow_panel = QFrame(config_tab)
        self.config_monitor_threat_flow_panel.setObjectName("controlMetricCard")
        self.config_monitor_threat_flow_panel.setMinimumSize(0, 0)
        self.config_monitor_threat_flow_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

        threat_flow_layout = QVBoxLayout(self.config_monitor_threat_flow_panel)
        threat_flow_layout.setContentsMargins(10, 10, 10, 10)
        threat_flow_layout.setSpacing(6)

        self.config_monitor_threat_flow_view = QTextBrowser(self.config_monitor_threat_flow_panel)
        self.config_monitor_threat_flow_view.setObjectName("controlBrowser")
        self.config_monitor_threat_flow_view.setOpenExternalLinks(False)
        self.config_monitor_threat_flow_view.setMinimumHeight(0)
        self.config_monitor_threat_flow_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.config_monitor_threat_flow_view.setHtml(
            "<p>Waiting for monitoring snapshot...</p>"
        )
        threat_flow_layout.addWidget(self.config_monitor_threat_flow_view, 1)

        for section_title, section_widget, section_expanded in (
            ("Monitoring Summary", self.monitor_summary_view, False),
            ("Monitoring Filters", self.monitor_filter_panel, False),
            ("Monitoring Drilldown", self.monitor_detail_view, False),
            ("Trace Detail", self.monitor_trace_view, False),
            ("Event Timeline", self.monitor_timeline_view, False),
            ("Threat Flow", self.config_monitor_threat_flow_panel, False),
            ("Configuration Summary", self.config_summary_view, False),
            ("Configuration Manifest", self.config_manifest_view, False),
            ("Operator Actions", self.operator_actions_panel, False),
            ("Operator Filters", self.operator_filters_panel, False),
            ("Operator Summary", self.operator_summary_view, False),
            ("Operator Log", self.operator_log_view, False),
            ("Build", self.build_section_panel, False),
            ("Extensions", self.extensions_section_panel, False),
        ):
            section = self._create_splitter_dropdown_section(
                parent=self.config_monitor_splitter,
                title=section_title,
                content_widget=section_widget,
                expanded=section_expanded,
            )
            self.config_monitor_sections.append(section)
            self.config_monitor_splitter.addWidget(section)

        for i in range(1 + len(self.config_monitor_sections)):
            self.config_monitor_splitter.setStretchFactor(i, 0)
        self._setup_config_monitor_splitter_handles()
        self._sync_config_monitor_splitter_handle_states()
        config_monitor_scroll_layout.addWidget(self.config_monitor_splitter, 0)
        config_monitor_scroll_layout.addStretch(1)
        self.config_monitor_scroll_area.setWidget(self.config_monitor_scroll_content)

        config_layout.addWidget(self.config_monitor_scroll_area, 1)

        config_tab_index = self.tabs.addTab(config_tab, self._PRIMARY_BOARD_TAB_LABEL)
        self._set_control_plane_tab_text(config_tab_index, self._PRIMARY_BOARD_TAB_LABEL)
        self._register_board_context(config_tab, self._capture_current_board_context(config_tab), primary=True)
        self._sync_board_explorer_tree_panel(self._primary_board_context)
        self._render_board_canvas_surface(self._primary_board_context)

        # Builder-tab symbol button removed per UX request.
        self._code_tab_new_button = None
        self._code_tab_index = -1

        self.primary_splitter.addWidget(self.tabs)
        self.primary_splitter.addWidget(hero)
        self.primary_splitter.setSizes([640, 180])
        self.primary_splitter.setStretchFactor(0, 4)
        self.primary_splitter.setStretchFactor(1, 1)
        root.addWidget(self.primary_splitter, 1)
        self._render_all_board_contexts(include_drilldown=False)
        self._update_runtime_layout_hint()
        self._restore_runtime_tabs_state()
        self._ensure_builder_runtime_tab(activate=False, persist=False)
        self._set_config_builder_visible(False)

    def _resolve_main_editor_window(self) -> QWidget | None:
        candidate = self.window()
        if callable(getattr(candidate, "_file_new_code_viewer_tab", None)):
            return candidate

        current = self.parentWidget()
        while current is not None:
            if callable(getattr(current, "_file_new_code_viewer_tab", None)):
                return current
            current = current.parentWidget()

        for top_level in QApplication.topLevelWidgets():
            if callable(getattr(top_level, "_file_new_code_viewer_tab", None)):
                return top_level
        return None

    def _open_new_code_tab_from_plane(self) -> None:
        window = self._resolve_main_editor_window()
        opener = getattr(window, "_file_new_code_viewer_tab", None) if window is not None else None
        if callable(opener):
            try:
                opener()
            except Exception as exc:
                QMessageBox.warning(self, "Fehler", f"Code-Tab konnte nicht geöffnet werden: {exc}")
            return
        QMessageBox.information(self, "Info", "Kein Tab-Dock verfügbar, um einen Code-Tab zu öffnen.")

    def _update_code_tab_button_visibility(self, current_index: int) -> None:
        if not hasattr(self, "tabs"):
            return
        button = getattr(self, "_code_tab_new_button", None)
        if button is None:
            return
        self._refresh_code_tab_button_target()
        code_index = int(getattr(self, "_code_tab_index", -1))
        if code_index < 0:
            button.hide()
            return

        tab_bar = self.tabs.tabBar()
        if tab_bar.tabButton(code_index, QTabBar.RightSide) is not button:
            tab_bar.setTabButton(code_index, QTabBar.RightSide, button)

        is_active = current_index == code_index
        button.setVisible(is_active)
        button.setEnabled(is_active)

    def _refresh_code_tab_button_target(self) -> int:
        button = getattr(self, "_code_tab_new_button", None)
        if button is None:
            self._code_tab_index = -1
            return -1

        builder_tab = getattr(self, "_builder_runtime_tab", None)
        if builder_tab not in self._runtime_tab_records:
            builder_tab = None
            for i in range(self.tabs.count()):
                candidate = self.tabs.widget(i)
                if candidate not in self._runtime_tab_records:
                    continue
                role = str(candidate.property("runtime_role") or "").strip().lower()
                tab_name = self._control_plane_tab_full_text(i).strip().lower()
                if role == "builder" or tab_name in {
                    self._LEGACY_BUILD_RUNTIME_TAB_LABEL.strip().lower(),
                    self._LEGACY_BUILD_RUNTIME_SLASH_LABEL.strip().lower(),
                    self._BUILD_RUNTIME_TAB_LABEL.strip().lower(),
                }:
                    builder_tab = candidate
                    break
            self._builder_runtime_tab = builder_tab

        code_index = self.tabs.indexOf(builder_tab) if builder_tab is not None else -1
        self._code_tab_index = code_index

        if code_index >= 0:
            tab_bar = self.tabs.tabBar()
            if tab_bar.tabButton(code_index, QTabBar.RightSide) is not button:
                tab_bar.setTabButton(code_index, QTabBar.RightSide, button)
        return code_index

    def _find_runtime_tab_by_name(self, tab_name: str) -> QWidget | None:
        normalized = str(tab_name or "").strip().lower()
        if not normalized:
            return None
        for i in range(self.tabs.count()):
            candidate = self.tabs.widget(i)
            if candidate in self._runtime_tab_records and self._control_plane_tab_full_text(i).strip().lower() == normalized:
                return candidate
        return None

    def _resolve_runtime_target_tab(self, *, preferred_name: str = "") -> QWidget | None:
        current_widget = self.tabs.currentWidget()
        if current_widget in self._runtime_tab_records:
            return current_widget

        by_name = self._find_runtime_tab_by_name(preferred_name)
        if by_name is not None:
            return by_name

        builder_tab = getattr(self, "_builder_runtime_tab", None)
        if builder_tab in self._runtime_tab_records:
            return builder_tab

        for i in range(self.tabs.count()):
            candidate = self.tabs.widget(i)
            if candidate in self._runtime_tab_records:
                return candidate
        return None

    def _ensure_builder_runtime_tab(self, *, activate: bool = False, persist: bool = False) -> QWidget:
        builder_tab = getattr(self, "_builder_runtime_tab", None)
        if builder_tab not in self._runtime_tab_records:
            builder_tab = None

        if builder_tab is None:
            for i in range(self.tabs.count()):
                candidate = self.tabs.widget(i)
                if candidate not in self._runtime_tab_records:
                    continue
                role = str(candidate.property("runtime_role") or "").strip().lower()
                if role == "builder":
                    builder_tab = candidate
                    break

        if builder_tab is None:
            by_name = self._find_runtime_tab_by_name(self._BUILD_RUNTIME_TAB_LABEL)
            if by_name is not None:
                builder_tab = by_name

        if builder_tab is None:
            by_name = self._find_runtime_tab_by_name(self._LEGACY_BUILD_RUNTIME_SLASH_LABEL)
            if by_name is not None:
                builder_tab = by_name

        if builder_tab is None:
            by_name = self._find_runtime_tab_by_name(self._LEGACY_BUILD_RUNTIME_TAB_LABEL)
            if by_name is not None:
                builder_tab = by_name

        if builder_tab is None:
            builder_tab = self.create_runtime_tab(
                self._BUILD_RUNTIME_TAB_LABEL,
                activate=activate,
                add_default_widget=False,
                persist=persist,
                default_widget_kind="builder_panel",
            )

        builder_tab.setProperty("runtime_role", "builder")
        self._builder_runtime_tab = builder_tab
        self._set_runtime_tab_default_widget_kind(builder_tab, "builder_panel", persist=False)

        builder_index = self.tabs.indexOf(builder_tab)
        if builder_index >= 0:
            self._set_control_plane_tab_text(builder_index, self._BUILD_RUNTIME_TAB_LABEL)
            self.tabs.setTabIcon(builder_index, QIcon())

        has_builder_widget = False
        record = self._runtime_tab_records.get(builder_tab) or {}
        splitter = record.get("splitter")
        if isinstance(splitter, QSplitter):
            for idx in range(splitter.count()):
                panel = splitter.widget(idx)
                if isinstance(panel, QWidget) and str(panel.property("runtime_widget_kind") or "").strip().lower() == "builder_panel":
                    has_builder_widget = True
                    break

        if not has_builder_widget:
            self._add_widget_to_runtime_tab(
                builder_tab,
                widget_kind="builder_panel",
                title="Agent System Builder",
                persist=persist,
            )

        if activate:
            self.tabs.setCurrentWidget(builder_tab)

        self._refresh_code_tab_button_target()
        return builder_tab

    def _open_new_runtime_tab(self) -> None:
        default_name = self._next_runtime_tab_name(self._BUILD_RUNTIME_TAB_LABEL)
        tab_name, ok = QInputDialog.getText(
            self,
            "Neuer Runtime-Tab",
            "Tab-Name:",
            text=default_name,
        )
        if not ok:
            return
        tab_widget = self.create_runtime_tab(
            tab_name.strip() or default_name,
            activate=True,
            add_default_widget=True,
            persist=False,
            default_widget_kind="builder_panel",
        )
        self._set_runtime_tab_default_widget_kind(tab_widget, "builder_panel", persist=False)
        self._schedule_runtime_state_save()

    def create_runtime_tab_for_kind(
        self,
        widget_kind: str,
        *,
        tab_name: str = "Runtime",
        activate: bool = True,
    ) -> QWidget:
        allowed = self._runtime_widget_supported_kinds()
        resolved_kind = str(widget_kind or "code_json").strip().lower()
        if resolved_kind not in allowed:
            resolved_kind = "code_json"

        target_tab = self._resolve_runtime_target_tab(preferred_name=tab_name)
        if target_tab is None:
            target_tab = self.create_runtime_tab(
                tab_name.strip() or "Runtime",
                activate=activate,
                add_default_widget=False,
                persist=False,
            )
        elif activate:
            target_index = self.tabs.indexOf(target_tab)
            if target_index >= 0:
                self.tabs.setCurrentIndex(target_index)
        self._set_runtime_tab_default_widget_kind(target_tab, resolved_kind, persist=False)

        content = ""
        title = None
        if resolved_kind == "code_markdown":
            title = "runtime_notes.md"
            content = "# Runtime Notes\n"
        elif resolved_kind == "text_view":
            title = "runtime_notes.txt"
            content = "Runtime notes\n"

        self._add_widget_to_runtime_tab(
            target_tab,
            widget_kind=resolved_kind,
            title=title,
            content=content,
            persist=True,
        )
        return target_tab

    def _next_runtime_tab_name(self, requested_name: str) -> str:
        base_name = str(requested_name or "").strip() or f"Runtime {self._runtime_tab_counter + 1}"
        existing = {self._control_plane_tab_full_text(i).strip().lower() for i in range(self.tabs.count())}
        if base_name.lower() not in existing:
            return base_name

        suffix = 2
        while f"{base_name} {suffix}".lower() in existing:
            suffix += 1
        return f"{base_name} {suffix}"

    def _runtime_widget_kind_for_language(self, language: str) -> str:
        normalized = str(language or "").strip().lower()
        if normalized in {"yaml", "yml"}:
            return "code_yaml"
        if normalized in {"python", "py"}:
            return "code_python"
        if normalized in {"markdown", "md"}:
            return "code_markdown"
        if normalized == "toml":
            return "code_toml"
        return "code_json"

    def _runtime_widget_menu_options(self) -> list[tuple[str, str]]:
        return [
            (self._BUILD_RUNTIME_TAB_LABEL, "builder_panel"),
            ("Agent Graph", "agent_relation_graph"),
            ("Python", "code_python"),
            ("JSON", "code_json"),
            ("YAML", "code_yaml"),
            ("Markdown", "code_markdown"),
            ("TOML", "code_toml"),
        ]

    def _runtime_widget_supported_kinds(self) -> set[str]:
        supported_kinds = {kind for _label, kind in self._runtime_widget_menu_options()}
        supported_kinds.add(self._BOARD_ITEM_WIDGET_KIND)
        supported_kinds.add(self._EXTENSIONS_WORKSPACE_WIDGET_KIND)
        return supported_kinds

    def _runtime_widget_label_for_kind(self, widget_kind: str) -> str:
        target = str(widget_kind or "code_json").strip().lower()
        if target == self._BOARD_ITEM_WIDGET_KIND:
            return "Board Item"
        if target == self._EXTENSIONS_WORKSPACE_WIDGET_KIND:
            return "Extensions"
        for label, kind in self._runtime_widget_menu_options():
            if kind == target:
                return label
        return "JSON"

    def _runtime_tab_default_widget_kind(self, tab_widget: QWidget) -> str:
        record = self._runtime_tab_records.get(tab_widget) or {}
        resolved_kind = str(record.get("default_widget_kind") or "code_json").strip().lower()
        allowed = self._runtime_widget_supported_kinds()
        if resolved_kind not in allowed:
            return "code_json"
        return resolved_kind

    def _set_runtime_tab_default_widget_kind(
        self,
        tab_widget: QWidget,
        widget_kind: str,
        *,
        persist: bool = False,
    ) -> None:
        record = self._runtime_tab_records.get(tab_widget)
        if not isinstance(record, dict):
            return

        allowed = self._runtime_widget_supported_kinds()
        resolved_kind = str(widget_kind or "code_json").strip().lower()
        if resolved_kind not in allowed:
            resolved_kind = "code_json"
        record["default_widget_kind"] = resolved_kind

        selector_actions = record.get("selector_actions")
        if isinstance(selector_actions, dict):
            for action_kind, action in selector_actions.items():
                if isinstance(action, QAction):
                    action.setChecked(str(action_kind) == resolved_kind)

        selector_button = record.get("selector")
        if isinstance(selector_button, (QPushButton, QToolButton)):
            label = self._runtime_widget_label_for_kind(resolved_kind)
            if isinstance(selector_button, QToolButton) and bool(selector_button.property("runtime_tab_mini_menu")):
                selector_button.setText("")
                selector_button.setToolTip(f"Tab-Menue (Widget: {label})")
                selector_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
                selector_button.setFixedSize(18, 18)
            else:
                selector_button.setText(label)
                selector_button.setToolTip(f"Viewer-Auswahl (aktuell: {label})")
                if isinstance(selector_button, QToolButton):
                    metrics = QFontMetrics(selector_button.font())
                    selector_button.setFixedHeight(21)
                    selector_button.setMinimumWidth(max(46, metrics.horizontalAdvance(label) + 14))

        if persist:
            self._schedule_runtime_state_save()

    def _select_runtime_widget_kind_from_menu(self, tab_widget: QWidget, action: QAction | None) -> None:
        if action is None:
            return
        action_data = action.data()
        if not isinstance(action_data, str):
            return
        selected_kind = str(action_data).strip().lower()
        if not selected_kind:
            return
        self._set_runtime_tab_default_widget_kind(tab_widget, selected_kind, persist=True)

    def _runtime_widget_template(self, widget_kind: str) -> tuple[str, str]:
        kind = str(widget_kind or "code_json").strip().lower()
        if kind == self._BOARD_ITEM_WIDGET_KIND:
            return "text", ""
        if kind == self._EXTENSIONS_WORKSPACE_WIDGET_KIND:
            return "text", ""
        if kind == "builder_panel":
            template = self._build_agent_system_template("agent_system", "/create agents")
            return "json", json.dumps(template, ensure_ascii=False, indent=2)
        if kind == "code_yaml":
            return "yaml", "runtime:\n  agents: []\n  workflows: []\n"
        if kind == "code_python":
            return "python", "runtime_config = {\n    \"agents\": [],\n    \"workflows\": [],\n}\n"
        if kind == "code_markdown":
            return "markdown", "# Runtime Notes\n\n- Build-Pipeline\n"
        if kind == "code_toml":
            return "toml", "[runtime]\nagents = []\nworkflows = []\n"
        if kind == "text_view":
            return "text", "Runtime notes\n"
        return "json", "{\n  \"runtime\": {\n    \"agents\": [],\n    \"workflows\": []\n  }\n}\n"

    def _apply_runtime_widget_panel_scheme(self, panel: QWidget) -> None:
        if not isinstance(panel, QWidget):
            return
        widget_kind = str(panel.property("runtime_widget_kind") or "").strip().lower()
        is_runtime_tab_panel = bool(panel.property("runtime_tab_panel"))
        is_builder_panel = widget_kind == "builder_panel"
        viewer_surface_kinds = {"code_json", "code_yaml", "code_python", "code_markdown", "code_toml", "text_view"}
        if is_runtime_tab_panel and widget_kind in viewer_surface_kinds:
            chrome_bg = CodeViewer._BACKGROUND_COLOR
        else:
            chrome_bg = self.scheme["col7"]
        panel_bg = chrome_bg
        panel_border = "transparent" if is_runtime_tab_panel else self.scheme["col10"]
        top_border_rule = "border-top: none;" if is_runtime_tab_panel else ""
        top_left_radius_rule = "border-top-left-radius: 0px;" if is_runtime_tab_panel else f"border-top-left-radius: {_SURFACE_BORDER_RADIUS_PX}px;"
        top_right_radius_rule = "border-top-right-radius: 0px;" if is_runtime_tab_panel else f"border-top-right-radius: {_SURFACE_BORDER_RADIUS_PX}px;"
        bottom_left_radius_rule = f"border-bottom-left-radius: {_SURFACE_BORDER_RADIUS_PX}px;"
        bottom_right_radius_rule = f"border-bottom-right-radius: {_SURFACE_BORDER_RADIUS_PX}px;"
        panel.setStyleSheet(
            f"""
            QFrame#runtimeWidgetPanel {{
                background: {panel_bg};
                border: {_SURFACE_BORDER_WIDTH_PX}px solid {panel_border};
                {top_border_rule}
                {top_left_radius_rule}
                {top_right_radius_rule}
                {bottom_left_radius_rule}
                {bottom_right_radius_rule}
            }}
            QLabel#runtimeWidgetTitle {{
                color: {self.scheme['col6']};
                font-weight: 600;
            }}
            QToolButton#runtimeWidgetActionButton {{
                background: {chrome_bg};
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 2px;
            }}
            QToolButton#runtimeWidgetActionButton:hover {{
                background: {chrome_bg};
                border: 1px solid transparent;
            }}
            QToolButton#runtimeWidgetRemoveButton {{
                background: {chrome_bg};
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 2px;
            }}
            QToolButton#runtimeWidgetRemoveButton:hover {{
                background: {chrome_bg};
                border: 1px solid transparent;
            }}
            """
        )

    def _apply_builder_panel_scheme(self, panel: QWidget, *, show_toolbar: bool | None = None) -> None:
        if not isinstance(panel, QWidget):
            return
        if show_toolbar is None:
            show_toolbar = bool(panel.property("_builder_show_toolbar"))
        panel_bg = self.scheme["col7"]
        button_bg = panel_bg
        panel_border = f"1px solid {self.scheme['col10']}" if bool(show_toolbar) else "none"
        panel_radius = "10px" if bool(show_toolbar) else "0px"
        panel.setStyleSheet(
            f"""
            QFrame#controlBuilderPanel {{
                background: {panel_bg};
                border: {panel_border};
                border-radius: {panel_radius};
            }}
            QPushButton#builderTemplateButton,
            QPushButton#builderBuildButton,
            QPushButton#builderPostButton,
            QPushButton#builderCopyButton {{
                background: {button_bg};
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 1px;
                min-width: 22px;
                min-height: 22px;
            }}
            QPushButton#builderTemplateButton:hover,
            QPushButton#builderBuildButton:hover,
            QPushButton#builderPostButton:hover,
            QPushButton#builderCopyButton:hover {{
                background: {button_bg};
                border-color: transparent;
            }}
            """
        )
        for viewer in panel.findChildren(CodeViewer):
            viewer.set_theme_colors(
                accent_color=self.scheme.get("col10", "#1f1f1f"),
                accent_selection_color=self.scheme.get("col2", "#6280ff"),
                background_color=panel_bg,
                surface_color=panel_bg,
            )

    def _runtime_tab_panels(self, tab_widget: QWidget | None) -> list[QWidget]:
        if not isinstance(tab_widget, QWidget):
            return []
        record = self._runtime_tab_records.get(tab_widget) or {}
        splitter = record.get("splitter") if isinstance(record, dict) else None
        if not isinstance(splitter, QSplitter):
            return []
        panels: list[QWidget] = []
        for idx in range(splitter.count()):
            panel = splitter.widget(idx)
            if isinstance(panel, QWidget) and panel.objectName() == "runtimeWidgetPanel":
                panels.append(panel)
        return panels

    def _runtime_tab_page_background_color(self, tab_widget: QWidget | None) -> str:
        if not isinstance(tab_widget, QWidget):
            return self.scheme.get("col9", "#101010")

        runtime_role = str(tab_widget.property("runtime_role") or "").strip().lower()
        if runtime_role == "builder":
            return self.scheme.get("col7", "#0b0b0b")

        record = self._runtime_tab_records.get(tab_widget) or {}
        default_kind = str(record.get("default_widget_kind") or "").strip().lower() if isinstance(record, dict) else ""
        if default_kind == "builder_panel":
            return self.scheme.get("col7", "#0b0b0b")
        return self.scheme.get("col9", "#101010")

    def _apply_runtime_tab_page_scheme(self, tab_widget: QWidget | None) -> None:
        if not isinstance(tab_widget, QWidget):
            return
        page_bg = self._runtime_tab_page_background_color(tab_widget)
        tab_widget.setStyleSheet(
            f"""
            QWidget#controlRuntimeTabPage {{
                background: {page_bg};
                border: none;
                border-radius: 0px;
            }}
            QWidget#controlRuntimeTabPage > QSplitter#controlViewportSplitter {{
                background: {page_bg};
                border: none;
            }}
            """
        )

    def _refresh_runtime_panel_schemes(self) -> None:
        for tab_widget in self._runtime_tab_records:
            self._apply_runtime_tab_page_scheme(tab_widget)
        for panel in self.findChildren(QFrame, "runtimeWidgetPanel"):
            self._apply_runtime_widget_panel_scheme(panel)
        for panel in self.findChildren(QFrame, "controlBuilderPanel"):
            self._apply_builder_panel_scheme(panel)
        for viewer in self.findChildren(CodeViewer):
            parent_widget = viewer.parentWidget()
            if isinstance(parent_widget, QFrame) and parent_widget.objectName() == "controlBuilderPanel":
                continue
            viewer.set_theme_colors(
                accent_color=self.scheme.get("col1", "#3a5fff"),
                accent_selection_color=self.scheme.get("col2", "#6280ff"),
                surface_color=self.scheme.get("col9", "#101010"),
            )

    def _locate_runtime_tab_for_panel(self, panel: QWidget) -> tuple[QWidget | None, QSplitter | None]:
        for tab_widget, record in self._runtime_tab_records.items():
            splitter = record.get("splitter") if isinstance(record, dict) else None
            if not isinstance(splitter, QSplitter):
                continue
            for idx in range(splitter.count()):
                if splitter.widget(idx) is panel:
                    return tab_widget, splitter
        return None, None

    def _remove_runtime_widget_panel(self, panel: QWidget) -> None:
        if panel is None:
            return

        tab_widget, splitter = self._locate_runtime_tab_for_panel(panel)
        panel.setParent(None)
        panel.deleteLater()

        def _finalize_removal() -> None:
            if tab_widget is None or splitter is None:
                self._schedule_runtime_state_save()
                return

            remaining_panels = [
                splitter.widget(i)
                for i in range(splitter.count())
                if isinstance(splitter.widget(i), QWidget)
            ]
            if not remaining_panels:
                self._dispose_runtime_tab(tab_widget, persist=True)
                return

            self._apply_runtime_tab_page_scheme(tab_widget)
            for remaining_panel in self._runtime_tab_panels(tab_widget):
                self._apply_runtime_widget_panel_scheme(remaining_panel)
            self._schedule_runtime_state_save()

        QTimer.singleShot(0, _finalize_removal)

    def _clone_runtime_widget_panel(self, panel: QWidget) -> QWidget | None:
        if not isinstance(panel, QWidget):
            return None
        tab_widget, _splitter = self._locate_runtime_tab_for_panel(panel)
        if not isinstance(tab_widget, QWidget):
            return None

        widget_kind = str(panel.property("runtime_widget_kind") or "code_json").strip().lower() or "code_json"
        title = str(panel.property("runtime_widget_title") or "runtime_widget").strip() or "runtime_widget"
        source_path = str(panel.property("runtime_source_path") or "").strip()
        board_item_title = str(panel.property("runtime_board_item_title") or "").strip()
        content = ""
        if widget_kind in {"builder_panel", "code_json", "code_yaml", "code_python", "code_markdown", "code_toml", "text_view"}:
            content = self._collect_runtime_widget_text(panel)

        return self._append_runtime_widget_clone_to_tab(
            tab_widget,
            widget_kind=widget_kind,
            title=title,
            source_path=source_path,
            board_item_title=board_item_title,
            content=content,
        )

    def _create_runtime_widget_panel(
        self,
        *,
        tab_widget: QWidget,
        widget_kind: str,
        title: str,
        content: str = "",
        source_path: str = "",
        board_item_title: str = "",
    ) -> QWidget:
        panel = QFrame(tab_widget)
        panel.setObjectName("runtimeWidgetPanel")
        _clear_frame_chrome(panel)
        panel.setProperty("runtime_tab_panel", True)
        kind = str(widget_kind or "code_json").strip().lower()
        resolved_board_item_title = str(board_item_title or title).strip() or title

        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(_SURFACE_INSET_PX, _SURFACE_INSET_PX, _SURFACE_INSET_PX, 0)
        header.setSpacing(6)
        title_label = QLabel(title, panel)
        title_label.setObjectName("runtimeWidgetTitle")
        header.addWidget(title_label, 1)

        remove_btn = QToolButton(panel)
        remove_btn.setObjectName("runtimeWidgetRemoveButton")
        remove_btn.setIcon(_icon("close.svg"))
        remove_btn.setIconSize(QSize(14, 14))
        remove_btn.setToolTip("Widget entfernen")
        remove_btn.clicked.connect(lambda _checked=False, p=panel: self._remove_runtime_widget_panel(p))
        header.addWidget(remove_btn, 0)

        def _add_runtime_header_action(icon_name: str, tooltip: str, slot_callable) -> None:
            action_btn = QToolButton(panel)
            action_btn.setObjectName("runtimeWidgetActionButton")
            action_btn.setIcon(_icon(icon_name))
            action_btn.setIconSize(QSize(14, 14))
            action_btn.setToolTip(tooltip)
            action_btn.setCursor(Qt.PointingHandCursor)
            action_btn.setAutoRaise(True)
            action_btn.clicked.connect(slot_callable)
            header.insertWidget(max(0, header.count() - 1), action_btn, 0)

        _add_runtime_header_action(
            "add_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg",
            "Neu: gleiches Panel anhängen",
            lambda _checked=False, p=panel: self._clone_runtime_widget_panel(p),
        )

        if kind not in {self._BOARD_ITEM_WIDGET_KIND, self._EXTENSIONS_WORKSPACE_WIDGET_KIND}:
            _add_runtime_header_action(
                "open_file.svg",
                "Datei in Widget importieren",
                lambda _checked=False, p=panel: self._import_runtime_widget_content(p),
            )
            _add_runtime_header_action(
                "file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg",
                "An Chat anhängen",
                lambda _checked=False, p=panel: self._export_runtime_widget_to_chat_context(p),
            )

        root.addLayout(header)

        language, default_text = self._runtime_widget_template(kind)
        resolved_source_path = str(source_path or "").strip()
        path_text = self._read_runtime_source_text(resolved_source_path)
        resolved_text = path_text if path_text is not None else (content or default_text)

        panel.setProperty("runtime_widget_kind", kind)
        panel.setProperty("runtime_widget_title", str(title))
        panel.setProperty("runtime_source_path", resolved_source_path)
        if kind == self._BOARD_ITEM_WIDGET_KIND:
            panel.setProperty("runtime_board_item_title", resolved_board_item_title)
        self._apply_runtime_widget_panel_scheme(panel)

        if kind == self._BOARD_ITEM_WIDGET_KIND:
            snapshot_browser = QTextBrowser(panel)
            snapshot_browser.setObjectName("controlBrowser")
            snapshot_browser.setOpenExternalLinks(False)
            snapshot_browser.setOpenLinks(False)
            snapshot_browser.setMinimumHeight(96)
            snapshot_browser.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            snapshot_browser.setHtml(self._board_item_snapshot_html(resolved_board_item_title))
            root.addWidget(snapshot_browser, 1)
        elif kind == self._EXTENSIONS_WORKSPACE_WIDGET_KIND:
            workspace_source_uri = resolved_source_path or self._board_extensions_source_uri()
            panel.setProperty("runtime_source_path", workspace_source_uri)
            extensions_widget = ExtensionsWorkspaceWidget(
                accent=dict(self._accent),
                base=dict(self._base),
                parent=panel,
                source_uri=workspace_source_uri,
                control_plane_widget_ref=None,
                initial_tool_id=self._EXTENSIONS_RUNTIME_TOOL_ID,
                auto_load_initial_tool=True,
                hide_internal_tab_bar=True,
            )
            extensions_widget.setMinimumHeight(180)
            extensions_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            external_uri_input = extensions_widget.create_external_uri_proxy(panel)
            external_uri_input.setProperty("runtime_widget_header_proxy", True)
            header.insertWidget(1, external_uri_input, 1)
            extensions_signal = getattr(extensions_widget, "widgetStateChanged", None)
            if extensions_signal is not None and hasattr(extensions_signal, "connect"):
                extensions_signal.connect(self._schedule_runtime_state_save)
            root.addWidget(extensions_widget, 1)
        elif kind == "agent_relation_graph":
            graph_source_uri = resolved_source_path or str(content or "").strip()
            if not graph_source_uri:
                graph_source_uri = "agentsdb://127.0.0.1:2331/tools:relation_graph_view"
            panel.setProperty("runtime_source_path", graph_source_uri)
            panel.setProperty("runtime_graph_tool_id", "relation_graph_view")
            panel.setProperty("runtime_graph_tool_path", "/tools:relation_graph_view")
            panel.setProperty("runtime_graph_initialized", False)

            artifact_container = QFrame(panel)
            artifact_container.setObjectName("runtimeArtifactContainer")
            artifact_layout = QVBoxLayout(artifact_container)
            artifact_layout.setContentsMargins(0, 0, 0, 0)
            artifact_layout.setSpacing(0)
            artifact_layout.addStretch(1)
            panel.setProperty("runtime_artifact_container", artifact_container)
            root.addWidget(artifact_container, 1)
        elif kind == "builder_panel":
            builder_panel, internal_build_btn, internal_post_btn = self._create_runtime_builder_widget(
                panel,
                initial_text=resolved_text,
                connect_text_changed=True,
                show_toolbar=False,
            )

            def _add_builder_header_action(icon_name: str, tooltip: str, target_btn: QPushButton | None) -> None:
                if target_btn is None:
                    return
                action_btn = QToolButton(panel)
                action_btn.setObjectName("runtimeWidgetActionButton")
                action_btn.setIcon(_icon(icon_name))
                action_btn.setIconSize(QSize(14, 14))
                action_btn.setToolTip(tooltip)
                action_btn.setCursor(Qt.PointingHandCursor)
                action_btn.setAutoRaise(True)
                action_btn.clicked.connect(lambda _checked=False, button=target_btn: button.click())
                header.insertWidget(max(0, header.count() - 1), action_btn, 0)

            _add_builder_header_action("deployed_code.svg", "Sync Build starten", internal_build_btn)
            _add_builder_header_action("send.svg", "Ergebnis ins Operations-Log schreiben", internal_post_btn)
            root.addWidget(builder_panel, 1)
        elif kind.startswith("code_"):
            editor = CodeViewer(
                resolved_text,
                panel,
                language=language,
                editable=True,
                auto_fit=False,
                accent_color=self.scheme.get("col1", "#0fe913"),
                accent_selection_color=self.scheme.get("col2", "#58ed5b"),
                surface_color=self.scheme.get("col9", "#101010"),
                font_size_px=14,
                top_left_radius_px=0,
                top_right_radius_px=0,
                bottom_left_radius_px=_SURFACE_BORDER_RADIUS_PX,
                bottom_right_radius_px=_SURFACE_BORDER_RADIUS_PX,
                draw_border=False,
            )
            editor.setMinimumHeight(96)
            editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            editor.setProperty("runtime_source_path", resolved_source_path)
            editor.textChanged.connect(self._schedule_runtime_state_save)
            root.addWidget(editor, 1)
        else:
            text_view = QPlainTextEdit(panel)
            text_view.setPlainText(resolved_text)
            _clear_frame_chrome(text_view)
            text_view.setLineWrapMode(QPlainTextEdit.NoWrap)
            text_view.setMinimumHeight(96)
            text_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            text_view.setProperty("runtime_source_path", resolved_source_path)
            text_view.setStyleSheet(
                f"""
                QPlainTextEdit {{
                    background: {self.scheme.get('col9', '#101010')};
                    color: {self.scheme.get('col6', '#E3E3DE')};
                    border: none;
                    border-top-left-radius: 0px;
                    border-top-right-radius: 0px;
                    border-bottom-left-radius: {_SURFACE_BORDER_RADIUS_PX}px;
                    border-bottom-right-radius: {_SURFACE_BORDER_RADIUS_PX}px;
                    padding: 12px;
                }}
                """
            )
            text_view.textChanged.connect(self._schedule_runtime_state_save)
            root.addWidget(text_view, 1)

        return panel

    def _find_runtime_widget_panel(
        self,
        *,
        tab_name: str,
        widget_kind: str,
    ) -> QWidget | None:
        normalized_tab_name = str(tab_name or "").strip().lower()
        normalized_kind = str(widget_kind or "").strip().lower()
        if not normalized_tab_name or not normalized_kind:
            return None

        target_tab = None
        for index in range(self.tabs.count()):
            candidate_tab = self.tabs.widget(index)
            if candidate_tab not in self._runtime_tab_records:
                continue
            if self._control_plane_tab_full_text(index).strip().lower() == normalized_tab_name:
                target_tab = candidate_tab
                break
        if target_tab is None:
            return None

        record = self._runtime_tab_records.get(target_tab) or {}
        splitter = record.get("splitter") if isinstance(record, dict) else None
        if not isinstance(splitter, QSplitter):
            return None

        for idx in range(splitter.count()):
            panel = splitter.widget(idx)
            if isinstance(panel, QWidget) and str(panel.property("runtime_widget_kind") or "").strip().lower() == normalized_kind:
                return panel
        return None

    def initialize_runtime_artifact_from_prompt(
        self,
        *,
        tab_name: str,
        widget_kind: str = "agent_relation_graph",
        backend_call: dict[str, Any] | None = None,
    ) -> bool:
        panel = self._find_runtime_widget_panel(tab_name=tab_name, widget_kind=widget_kind)
        if panel is None:
            return False
        if str(widget_kind or "").strip().lower() == "agent_relation_graph":
            self._initialize_runtime_relation_graph_panel(panel, backend_call=backend_call)
            return True
        return False

    def _initialize_runtime_relation_graph_panel(
        self,
        panel: QWidget,
        *,
        backend_call: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(panel, QWidget):
            return

        artifact_container = panel.property("runtime_artifact_container")
        if not isinstance(artifact_container, QWidget):
            return

        source_uri = str(panel.property("runtime_source_path") or "").strip()
        if not source_uri:
            source_uri = "agentsdb://127.0.0.1:2331/tools:relation_graph_view"
            panel.setProperty("runtime_source_path", source_uri)

        tool_id = str(panel.property("runtime_graph_tool_id") or "relation_graph_view").strip() or "relation_graph_view"
        tool_path = str(panel.property("runtime_graph_tool_path") or f"/tools:{tool_id}").strip() or f"/tools:{tool_id}"
        resolved_backend_call = dict(backend_call or {})
        if not resolved_backend_call:
            resolved_backend_call = {
                "tool": tool_path,
                "source_uri": source_uri,
            }
        else:
            resolved_backend_call.setdefault("tool", tool_path)
            resolved_backend_call.setdefault("source_uri", source_uri)

        engine_callable = self._load_runtime_relation_graph_engine_callable()
        if not callable(engine_callable):
            payload = {
                "ok": False,
                "error": "engine_tool_import_failed",
                "detail": "execute_adb_relation_graph_service unavailable",
            }
        else:
            try:
                payload = engine_callable(
                    backend_call=resolved_backend_call,
                    include_view_state=True,
                    include_connection_preview=True,
                )
            except Exception as exc:
                payload = {
                    "ok": False,
                    "error": "engine_tool_execute_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }

        panel.setProperty("runtime_graph_payload", json.dumps(payload, ensure_ascii=False))

        while artifact_container.layout() and artifact_container.layout().count() > 0:
            item = artifact_container.layout().takeAt(0)
            widget = item.widget()
            if isinstance(widget, QWidget):
                widget.setParent(None)
                widget.deleteLater()

        artifact_service = ExtensionArtifactService()
        if not bool(payload.get("ok")):
            error_widget = self._render_runtime_artifact_error_widget(
                artifact_container,
                "Artifact initialization failed",
                str(payload.get("detail") or payload.get("error") or "unknown_error"),
            )
            artifact_container.layout().addWidget(error_widget, 1)
            panel.setProperty("runtime_graph_initialized", False)
            self._schedule_runtime_state_save()
            return

        resolved_tool_id = str(payload.get("tool_id") or tool_id).strip() or tool_id
        resolved_source_uri = str(payload.get("source_uri") or source_uri).strip() or source_uri
        panel.setProperty("runtime_source_path", resolved_source_uri)

        try:
            artifact_widget = artifact_service.load_object_widget(
                object_name=resolved_tool_id,
                source_uri=resolved_source_uri,
                parent=artifact_container,
                scheme=self.scheme,
            )
        except Exception as exc:
            artifact_widget = self._render_runtime_artifact_error_widget(
                artifact_container,
                "Artifact install failed",
                f"{type(exc).__name__}: {exc}",
            )

        artifact_widget.setProperty("runtime_source_path", resolved_source_uri)
        artifact_widget.setProperty("runtime_graph_tool_id", resolved_tool_id)
        widget_signal = getattr(artifact_widget, "widgetStateChanged", None)
        if widget_signal is not None and hasattr(widget_signal, "connect"):
            widget_signal.connect(self._schedule_runtime_state_save)
        artifact_container.layout().addWidget(artifact_widget, 1)
        panel.setProperty("runtime_graph_initialized", True)
        self._schedule_runtime_state_save()

    def _add_widget_to_runtime_tab(
        self,
        tab_widget: QWidget,
        *,
        widget_kind: str | None = None,
        title: str | None = None,
        content: str = "",
        source_path: str = "",
        board_item_title: str = "",
        persist: bool = True,
    ) -> QWidget | None:
        record = self._runtime_tab_records.get(tab_widget)
        if not isinstance(record, dict):
            return None

        resolved_kind = str(widget_kind or "").strip().lower() or "code_json"
        if widget_kind is None:
            resolved_kind = self._runtime_tab_default_widget_kind(tab_widget)
        resolved_board_item_title = str(board_item_title or "").strip()
        if resolved_kind == self._BOARD_ITEM_WIDGET_KIND and not resolved_board_item_title:
            resolved_board_item_title = str(title or content or source_path or "Board Item").strip() or "Board Item"

        counter = int(record.get("widget_count") or 0) + 1
        record["widget_count"] = counter

        display_title = title
        if not display_title:
            label_by_kind = {
                self._BOARD_ITEM_WIDGET_KIND: resolved_board_item_title or "Board Item",
                "builder_panel": "agent_system_builder.json",
                "agent_relation_graph": "agent_relation_graph",
                "code_json": "runtime_config.json",
                "code_yaml": "runtime_config.yaml",
                "code_python": "runtime_config.py",
                "code_markdown": "runtime_notes.md",
                "code_toml": "runtime_config.toml",
                "text_view": "runtime_notes.txt",
            }
            display_title = f"{label_by_kind.get(resolved_kind, 'runtime_widget')} #{counter}"

        panel = self._create_runtime_widget_panel(
            tab_widget=tab_widget,
            widget_kind=resolved_kind,
            title=str(display_title),
            content=content,
            source_path=source_path,
            board_item_title=resolved_board_item_title,
        )

        splitter = record.get("splitter")
        if isinstance(splitter, QSplitter):
            splitter.addWidget(panel)
            panel_index = splitter.indexOf(panel)
            if panel_index >= 0:
                splitter.setCollapsible(panel_index, True)
        self._apply_runtime_tab_page_scheme(tab_widget)
        for runtime_panel in self._runtime_tab_panels(tab_widget):
            self._apply_runtime_widget_panel_scheme(runtime_panel)
        if persist:
            self._schedule_runtime_state_save()
        return panel

    def create_runtime_tab(
        self,
        tab_name: str,
        *,
        activate: bool = True,
        add_default_widget: bool = True,
        persist: bool = True,
        default_widget_kind: str = "code_json",
    ) -> QWidget:
        resolved_name = self._next_runtime_tab_name(tab_name)

        allowed = self._runtime_widget_supported_kinds()
        resolved_default_kind = str(default_widget_kind or "code_json").strip().lower()
        if resolved_default_kind not in allowed:
            resolved_default_kind = "code_json"

        tab_widget = QWidget(self.tabs)
        tab_widget.setObjectName("controlRuntimeTabPage")
        tab_widget.setAttribute(Qt.WA_StyledBackground, True)
        tab_layout = QVBoxLayout(tab_widget)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        workspace_splitter = self._create_viewport_splitter(tab_widget)
        tab_layout.addWidget(workspace_splitter, 1)

        self._runtime_tab_records[tab_widget] = {
            "selector": None,
            "selector_menu": None,
            "selector_actions": {},
            "default_widget_kind": resolved_default_kind,
            "splitter": workspace_splitter,
            "widget_count": 0,
        }
        self._apply_runtime_tab_page_scheme(tab_widget)
        self._set_runtime_tab_default_widget_kind(tab_widget, resolved_default_kind, persist=False)

        if add_default_widget:
            self._add_widget_to_runtime_tab(tab_widget, widget_kind=resolved_default_kind, persist=False)

        tab_index = self.tabs.addTab(tab_widget, resolved_name)
        self._set_control_plane_tab_text(tab_index, resolved_name)
        if activate:
            self.tabs.setCurrentIndex(tab_index)
        self._runtime_tab_counter += 1
        if persist:
            self._schedule_runtime_state_save()
        return tab_widget

    def _dispose_runtime_tab(self, tab_widget: QWidget, *, persist: bool = True) -> None:
        index = self.tabs.indexOf(tab_widget)
        if index >= 0:
            self.tabs.removeTab(index)
        self._runtime_tab_records.pop(tab_widget, None)
        if tab_widget is self._builder_runtime_tab:
            self._builder_runtime_tab = None
        tab_widget.setParent(None)
        tab_widget.deleteLater()
        if persist:
            self._schedule_runtime_state_save()

    def _close_runtime_tab(self, tab_widget: QWidget) -> None:
        self._dispose_runtime_tab(tab_widget, persist=True)

    def append_runtime_widget(
        self,
        *,
        tab_name: str,
        widget_kind: str = "code_json",
        content: str = "",
        source_path: str = "",
        title: str | None = None,
    ) -> QWidget | None:
        target_tab: QWidget | None = None
        normalized_name = str(tab_name or "").strip().lower()
        for i in range(self.tabs.count()):
            candidate = self.tabs.widget(i)
            if candidate in self._runtime_tab_records and self._control_plane_tab_full_text(i).strip().lower() == normalized_name:
                target_tab = candidate
                break
        if target_tab is None:
            target_tab = self.create_runtime_tab(tab_name, activate=True)
        return self._add_widget_to_runtime_tab(
            target_tab,
            widget_kind=widget_kind,
            title=title,
            content=content,
            source_path=source_path,
        )

    def append_runtime_code_view(
        self,
        *,
        tab_name: str,
        language: str = "json",
        content: str = "",
        source_path: str = "",
        title: str | None = None,
    ) -> QWidget | None:
        widget_kind = self._runtime_widget_kind_for_language(language)
        return self.append_runtime_widget(
            tab_name=tab_name,
            widget_kind=widget_kind,
            content=content,
            source_path=source_path,
            title=title,
        )

    def _create_metric_card(self, title: str) -> tuple[QFrame, QLabel]:
        card = QFrame(self)
        card.setObjectName("controlMetricCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(2)

        title_label = QLabel(title, card)
        title_label.setObjectName("controlMetricLabel")
        value_label = QLabel("--", card)
        value_label.setObjectName("controlMetricValue")
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addStretch(1)
        return card, value_label

    def _create_viewport_splitter(self, parent: QWidget) -> QSplitter:
        splitter = QSplitter(Qt.Vertical, parent)
        splitter.setObjectName("controlViewportSplitter")
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(4)
        splitter.setOpaqueResize(True)
        splitter.setMinimumSize(0, 0)
        splitter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        return splitter

    def _is_config_monitor_host_threat_flow_expanded(self) -> bool:
        splitter = getattr(self, "config_monitor_host_splitter", None)
        if not isinstance(splitter, QSplitter):
            return False
        sizes = splitter.sizes()
        return len(sizes) >= 2 and int(sizes[1]) > 0

    def _set_config_monitor_host_threat_flow_expanded(self, expanded: bool) -> None:
        splitter = getattr(self, "config_monitor_host_splitter", None)
        if not isinstance(splitter, QSplitter):
            return

        sizes = splitter.sizes()
        if len(sizes) < 2:
            return

        total_size = max(1, int(sizes[0]) + int(sizes[1]))
        if expanded:
            remembered_size = max(140, int(getattr(self, "_config_monitor_host_threat_flow_size", 240) or 240))
            target_second = min(remembered_size, max(120, total_size - 120))
        else:
            current_second = int(sizes[1])
            if current_second > 0:
                self._config_monitor_host_threat_flow_size = max(140, current_second)
            target_second = 0

        splitter.setSizes([max(1, total_size - target_second), target_second])
        self._sync_config_monitor_host_splitter_handle_state()

    def _toggle_config_monitor_host_threat_flow(self) -> None:
        self._set_config_monitor_host_threat_flow_expanded(
            not self._is_config_monitor_host_threat_flow_expanded()
        )

    def _sync_config_monitor_host_splitter_handle_state(self, *_args: Any) -> None:
        splitter = getattr(self, "config_monitor_host_splitter", None)
        if not isinstance(splitter, QSplitter):
            return

        toggle_button = getattr(self, "config_monitor_host_splitter_toggle", None)
        expanded = self._is_config_monitor_host_threat_flow_expanded()

        sizes = splitter.sizes()
        if len(sizes) >= 2 and int(sizes[1]) > 0:
            self._config_monitor_host_threat_flow_size = max(140, int(sizes[1]))

        if isinstance(toggle_button, _SplitterToggleGlyph):
            toggle_button.blockSignals(True)
            self._set_control_splitter_toggle_glyph(toggle_button, expanded)
            toggle_button.setToolTip(
                "Threat Flow einklappen" if expanded else "Threat Flow ausklappen"
            )
            toggle_button.blockSignals(False)

    def _setup_config_monitor_host_splitter_handle(self) -> None:
        splitter = getattr(self, "config_monitor_host_splitter", None)
        if not isinstance(splitter, QSplitter):
            return

        splitter.setHandleWidth(23)
        handle = splitter.handle(1)
        if handle is None:
            return
        handle.setObjectName("controlHostSplitterHandle")

        handle_layout = QHBoxLayout(handle)
        handle_layout.setContentsMargins(6, 3, 6, 1)
        handle_layout.setSpacing(2)
        handle_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        host_label_text = "Threat Flow"
        handle_label = QLabel(host_label_text, handle)
        handle_label.setObjectName("controlHostSplitterHandleLabel")
        handle_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        handle_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        handle_label.setToolTip(host_label_text)
        host_category = self._control_monitor_splitter_category(host_label_text)
        handle.setProperty("control_splitter_category", host_category)
        handle_label.setProperty("control_splitter_category", host_category)

        toggle_button = _SplitterToggleGlyph(handle)
        toggle_button.setObjectName("controlHostSplitterHandleToggle")
        toggle_button.clicked.connect(self._toggle_config_monitor_host_threat_flow)

        handle_layout.addWidget(toggle_button, 0, Qt.AlignLeft | Qt.AlignTop)
        handle_layout.addWidget(handle_label, 1, Qt.AlignLeft | Qt.AlignTop)

        self.config_monitor_host_splitter_toggle = toggle_button
        self.config_monitor_host_splitter_label = handle_label
        splitter.splitterMoved.connect(self._sync_config_monitor_host_splitter_handle_state)
        self._apply_control_splitter_handle_styles()

    def _config_monitor_section_state_for(self, section: QFrame) -> dict[str, Any] | None:
        state_map = getattr(self, "_config_monitor_section_state", None)
        if not isinstance(state_map, dict):
            return None
        state = state_map.get(section)
        if isinstance(state, dict):
            return state
        return None

    def _is_config_monitor_section_expanded(self, section: QFrame) -> bool:
        state = self._config_monitor_section_state_for(section)
        if not isinstance(state, dict):
            return False
        return bool(state.get("expanded", False))

    def _set_config_monitor_section_expanded(self, section: QFrame, expanded: bool) -> None:
        state = self._config_monitor_section_state_for(section)
        if not isinstance(state, dict):
            return

        content_widget = state.get("content_widget")
        remember_size = state.get("remember_size")
        if not isinstance(content_widget, QWidget):
            return
        if not isinstance(remember_size, dict):
            return

        self._set_splitter_dropdown_expanded(
            section=section,
            content_widget=content_widget,
            expanded=bool(expanded),
            remember_size=remember_size,
            apply_splitter_sizes=True,
        )

    def _toggle_config_monitor_section_from_handle(self, section: QFrame) -> None:
        self._set_config_monitor_section_expanded(
            section,
            not self._is_config_monitor_section_expanded(section),
        )

    def _restore_config_monitor_splitter_handle_default(self, handle_index: int) -> None:
        controls = getattr(self, "_config_monitor_splitter_handle_controls", None)
        if not isinstance(controls, dict):
            return

        control = controls.get(handle_index)
        if not isinstance(control, dict):
            return

        action_container = control.get("action_container")
        if isinstance(action_container, QWidget):
            action_container.setVisible(False)

        control["actions_visible"] = False

        self._sync_config_monitor_splitter_handle_states()
        self._apply_control_splitter_handle_styles()

    def _show_config_monitor_splitter_handle_actions(self, handle_index: int) -> None:
        splitter = getattr(self, "config_monitor_splitter", None)
        controls = getattr(self, "_config_monitor_splitter_handle_controls", None)
        if not isinstance(splitter, QSplitter) or not isinstance(controls, dict):
            return

        control = controls.get(handle_index)
        if not isinstance(control, dict):
            return

        section = control.get("section")
        if not isinstance(section, QFrame):
            return

        handle = splitter.handle(handle_index)
        if handle is None:
            return

        action_container = control.get("action_container")
        if not isinstance(action_container, QWidget):
            return

        action_container.setVisible(True)
        control["actions_visible"] = True

    def _runtime_tab_name_for_section_title(self, section_title: str) -> str:
        resolved_title = str(section_title or "").strip()
        if not resolved_title:
            return "Board Item"
        if resolved_title.lower() == "build":
            return self._BUILD_RUNTIME_TAB_LABEL
        return resolved_title

    def _runtime_widget_descriptor_for_section_title(self, section_title: str) -> dict[str, str]:
        resolved_title = str(section_title or "").strip()
        widget_kind = self._board_item_widget_kind_for_title(resolved_title)
        source_path = ""
        board_item_title = ""
        if widget_kind == self._BOARD_ITEM_WIDGET_KIND:
            board_item_title = resolved_title
        elif widget_kind == self._EXTENSIONS_WORKSPACE_WIDGET_KIND:
            source_path = self._board_extensions_source_uri()
        return {
            "widget_kind": widget_kind,
            "tab_name": self._runtime_tab_name_for_section_title(resolved_title),
            "title": resolved_title,
            "source_path": source_path,
            "board_item_title": board_item_title,
        }

    def _append_runtime_widget_clone_to_tab(
        self,
        tab_widget: QWidget,
        *,
        widget_kind: str,
        title: str,
        source_path: str = "",
        board_item_title: str = "",
        content: str = "",
    ) -> QWidget | None:
        if not isinstance(tab_widget, QWidget):
            return None
        target_index = self.tabs.indexOf(tab_widget)
        if target_index >= 0:
            self.tabs.setCurrentIndex(target_index)
        return self._add_widget_to_runtime_tab(
            tab_widget,
            widget_kind=widget_kind,
            title=title,
            source_path=source_path,
            board_item_title=board_item_title,
            content=content,
            persist=True,
        )

    def _config_monitor_section_base_height(
        self,
        content_widget: QWidget,
        remember_size: dict[str, int] | None = None,
    ) -> int:
        if isinstance(remember_size, dict):
            baseline_size = int(remember_size.get("baseline_size") or 0)
            if baseline_size > 0:
                return baseline_size
        return max(
            220,
            int(content_widget.minimumHeight() or 0),
            int(content_widget.minimumSizeHint().height() or 0),
        )

    def _is_config_monitor_section_large_expanded(self, section: QFrame) -> bool:
        state = self._config_monitor_section_state_for(section)
        if not isinstance(state, dict) or not bool(state.get("expanded", False)):
            return False

        remember_size = state.get("remember_size")
        if not isinstance(remember_size, dict):
            return False
        return bool(remember_size.get("large_expanded"))

    def _expand_config_monitor_splitter_section(self, section: QFrame) -> None:
        state = self._config_monitor_section_state_for(section)
        if not isinstance(state, dict):
            return

        content_widget = state.get("content_widget")
        remember_size = state.get("remember_size")
        if not isinstance(content_widget, QWidget) or not isinstance(remember_size, dict):
            return

        base_height = self._config_monitor_section_base_height(content_widget, remember_size)
        content_hint = max(0, int(content_widget.sizeHint().height()))
        large_size = max(base_height * 2, content_hint * 2 + 64)

        if bool(remember_size.get("large_expanded")):
            remember_size["expanded_size"] = base_height
            remember_size["large_expanded"] = False
        else:
            remember_size["expanded_size"] = large_size
            remember_size["large_expanded"] = True

        self._set_splitter_dropdown_expanded(
            section=section,
            content_widget=content_widget,
            expanded=True,
            remember_size=remember_size,
            apply_splitter_sizes=True,
        )

    def _open_config_monitor_section_in_runtime_tab(self, section: QFrame) -> QWidget | None:
        board_context = self._board_context_from_object(self.sender()) or self._active_board_context()
        with self._board_context_scope(board_context):
            state = self._config_monitor_section_state_for(section)
            if not isinstance(state, dict):
                return None
            section_title = str(state.get("title") or section.property("control_dropdown_title") or "").strip()
            if not section_title:
                return None

            descriptor = self._runtime_widget_descriptor_for_section_title(section_title)
            widget_kind = descriptor["widget_kind"]
            tab_name = descriptor["tab_name"]
            target_tab = self._find_runtime_tab_by_name(tab_name)
            if target_tab is None:
                if section_title.lower() == "build":
                    target_tab = self._ensure_builder_runtime_tab(activate=True, persist=False)
                else:
                    target_tab = self.create_runtime_tab(
                        tab_name,
                        activate=True,
                        add_default_widget=False,
                        persist=False,
                        default_widget_kind=widget_kind,
                    )
            return self._append_runtime_widget_clone_to_tab(
                target_tab,
                widget_kind=widget_kind,
                title=descriptor["title"],
                source_path=descriptor["source_path"],
                board_item_title=descriptor["board_item_title"],
            )

    def _sync_config_monitor_splitter_handle_states(self, *_args: Any) -> None:
        splitter = getattr(self, "config_monitor_splitter", None)
        controls = getattr(self, "_config_monitor_splitter_handle_controls", None)
        if not isinstance(splitter, QSplitter) or not isinstance(controls, dict):
            return

        for _handle_index, control in controls.items():
            if not isinstance(control, dict):
                continue

            section = control.get("section")
            toggle_button = control.get("toggle_button")
            handle_label = control.get("label")
            if not isinstance(section, QFrame) or not isinstance(toggle_button, _SplitterToggleGlyph):
                continue

            section_title = str(section.property("control_dropdown_title") or control.get("title") or "Widget").strip() or "Widget"
            section_category = self._control_monitor_splitter_category(section_title)
            expanded = self._is_config_monitor_section_expanded(section)

            toggle_button.blockSignals(True)
            self._set_control_splitter_toggle_glyph(toggle_button, expanded)
            toggle_button.setToolTip(
                f"{section_title} einklappen" if expanded else f"{section_title} ausklappen"
            )
            toggle_button.blockSignals(False)

            if isinstance(handle_label, QLabel):
                full_label_text = section_title
                handle_label.setText(full_label_text)
                handle_label.setToolTip(full_label_text)
                handle_label.setProperty("control_splitter_category", section_category)

            action_expand_button = control.get("action_expand_button")
            if isinstance(action_expand_button, QToolButton):
                is_large_expanded = self._is_config_monitor_section_large_expanded(section)
                action_expand_button.setIcon(
                    _content_resize_icon(reset=is_large_expanded)
                )
                action_expand_button.setToolTip(
                    f"{section_title} auf Normalgroesse setzen"
                    if is_large_expanded
                    else f"{section_title} vergroessern"
                )

            action_container = control.get("action_container")
            if isinstance(action_container, QWidget):
                action_container.setVisible(expanded)
                control["actions_visible"] = expanded

            control["category"] = section_category

    def _setup_config_monitor_splitter_handles(self) -> None:
        splitter = getattr(self, "config_monitor_splitter", None)
        if not isinstance(splitter, QSplitter):
            return

        splitter.setHandleWidth(22)
        self._config_monitor_splitter_handle_controls = {}
        previous_section_category: str | None = None

        for handle_index in range(1, splitter.count()):
            handle = splitter.handle(handle_index)
            if handle is None:
                continue
            handle.setObjectName("controlSectionSplitterHandle")
            handle.setContextMenuPolicy(Qt.DefaultContextMenu)

            section = splitter.widget(handle_index)
            if not isinstance(section, QFrame):
                continue

            section_title = str(section.property("control_dropdown_title") or f"Widget {handle_index + 1}").strip() or f"Widget {handle_index + 1}"
            section_category = self._control_monitor_splitter_category(section_title)
            handle_top_margin = self._control_monitor_group_top_margin(
                section_category,
                previous_section_category,
            )

            handle_layout = handle.layout()
            if not isinstance(handle_layout, QHBoxLayout):
                handle_layout = QHBoxLayout(handle)
            handle_layout.setContentsMargins(6, handle_top_margin, 6, 1)
            handle_layout.setSpacing(2)
            handle_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            while handle_layout.count() > 0:
                item = handle_layout.takeAt(0)
                widget = item.widget()
                if isinstance(widget, QWidget):
                    widget.deleteLater()

            toggle_button = _SplitterToggleGlyph(handle)
            toggle_button.setObjectName("controlSectionSplitterHandleToggle")
            toggle_button.clicked.connect(
                lambda sec=section: self._toggle_config_monitor_section_from_handle(sec)
            )

            full_label_text = section_title
            handle_label = QLabel(full_label_text, handle)
            handle_label.setObjectName("controlSectionSplitterHandleLabel")
            handle_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            handle_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            handle_label.setToolTip(full_label_text)
            handle.setProperty("control_splitter_category", section_category)
            handle_label.setProperty("control_splitter_category", section_category)

            handle_layout.addWidget(toggle_button, 0, Qt.AlignLeft | Qt.AlignTop)
            handle_layout.addWidget(handle_label, 1, Qt.AlignLeft | Qt.AlignTop)

            action_container: QWidget | None = None
            action_expand_button: QToolButton | None = None
            action_dock_button: QToolButton | None = None

            action_container = QWidget(handle)
            action_layout = QHBoxLayout(action_container)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(4)

            def _run_action(action_callable: Callable[[], None], *, idx: int = handle_index) -> None:
                try:
                    action_callable()
                finally:
                    self._restore_config_monitor_splitter_handle_default(idx)

            action_expand_button = QToolButton(action_container)
            action_expand_button.setObjectName("runtimeWidgetActionButton")
            action_expand_button.setIcon(_content_resize_icon(reset=False))
            action_expand_button.setIconSize(QSize(14, 14))
            action_expand_button.setToolTip(f"{section_title} vergroessern")
            action_expand_button.setCursor(Qt.PointingHandCursor)
            action_expand_button.setAutoRaise(True)
            action_expand_button.clicked.connect(
                lambda _checked=False, sec=section: _run_action(
                    lambda: self._expand_config_monitor_splitter_section(sec)
                )
            )

            action_dock_button = QToolButton(action_container)
            action_dock_button.setObjectName("runtimeWidgetActionButton")
            action_dock_button.setIcon(_icon("open_in_new_dock.svg"))
            action_dock_button.setIconSize(QSize(14, 14))
            action_dock_button.setToolTip(
                f"{section_title} in {self._runtime_tab_name_for_section_title(section_title)} oeffnen"
            )
            action_dock_button.setCursor(Qt.PointingHandCursor)
            action_dock_button.setAutoRaise(True)
            action_dock_button.clicked.connect(
                lambda _checked=False, sec=section: _run_action(
                    lambda: self._open_config_monitor_section_in_runtime_tab(sec)
                )
            )

            section_state = self._config_monitor_section_state_for(section)
            content_widget = section_state.get("content_widget") if isinstance(section_state, dict) else None
            if (
                section_title.strip().lower() == "extensions"
                and isinstance(content_widget, QWidget)
            ):
                extensions_workspace = (
                    content_widget
                    if isinstance(content_widget, ExtensionsWorkspaceWidget)
                    else content_widget.findChild(ExtensionsWorkspaceWidget)
                )
            else:
                extensions_workspace = None
            if isinstance(extensions_workspace, ExtensionsWorkspaceWidget):
                embedded_tab_strip = extensions_workspace.create_embedded_tab_bar_proxy(action_container)
                embedded_tab_strip.setProperty("control_splitter_embedded_tabs", True)
                embedded_tab_bar = embedded_tab_strip.findChild(QTabBar, "extensionsEmbeddedTabBar")
                if isinstance(embedded_tab_bar, QTabBar):
                    embedded_tab_bar.setStyleSheet(
                        self._control_monitor_embedded_tab_bar_style(section_category)
                    )
                embedded_add_button = embedded_tab_strip.findChild(
                    QToolButton, "extensionsEmbeddedTabAddButton"
                )
                if isinstance(embedded_add_button, QToolButton):
                    embedded_add_button.setStyleSheet(
                        self._control_monitor_embedded_tab_add_button_style(section_category)
                    )
                action_layout.addWidget(embedded_tab_strip, 1, Qt.AlignLeft | Qt.AlignTop)

            action_layout.addWidget(action_expand_button, 0, Qt.AlignLeft | Qt.AlignTop)
            action_layout.addWidget(action_dock_button, 0, Qt.AlignLeft | Qt.AlignTop)
            action_container.setVisible(False)
            handle_layout.addWidget(action_container, 0, Qt.AlignLeft | Qt.AlignTop)

            handle.setContextMenuPolicy(Qt.CustomContextMenu)
            handle.customContextMenuRequested.connect(
                lambda _pos, idx=handle_index: self._show_config_monitor_splitter_handle_actions(idx)
            )

            self._config_monitor_splitter_handle_controls[handle_index] = {
                "section": section,
                "title": section_title,
                "category": section_category,
                "toggle_button": toggle_button,
                "label": handle_label,
                "action_container": action_container,
                "action_expand_button": action_expand_button,
                "action_dock_button": action_dock_button,
                "actions_visible": False,
            }
            previous_section_category = section_category

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                splitter.splitterMoved.disconnect(self._sync_config_monitor_splitter_handle_states)
        except Exception:
            pass
        splitter.splitterMoved.connect(self._sync_config_monitor_splitter_handle_states)
        self._apply_control_splitter_handle_styles()

    def _set_control_splitter_toggle_glyph(self, toggle_button: _SplitterToggleGlyph, expanded: bool) -> None:
        toggle_button.setExpanded(expanded)

    def _apply_control_splitter_toggle_button_style(self, toggle_button: _SplitterToggleGlyph | None) -> None:
        if not isinstance(toggle_button, _SplitterToggleGlyph):
            return

        idle_icon_color = str(self.scheme.get("col8") or "#9a9a9a")
        active_icon_color = str(self.scheme.get("col1") or "#35ff8a")
        toggle_button.setColors(idle_icon_color, active_icon_color)

    def _control_monitor_splitter_category(self, section_title: str) -> str:
        title_key = str(section_title or "").strip().lower()
        if title_key.startswith("monitoring"):
            return "orange"
        if title_key.startswith("operator"):
            return "turquoise"
        if title_key.startswith("trace") or title_key.startswith("event") or "threat flow" in title_key:
            return "blue"
        if title_key.startswith("configuration"):
            return "magenta"
        if title_key.startswith("build") or title_key.startswith("extension"):
            return "sunyellow"
        return "blue"

    def _control_monitor_splitter_palette(self, category: str) -> dict[str, str]:
        palettes: dict[str, dict[str, str]] = {
            "orange": {
                "label_fg": "#ffd7ac",
                "label_bg": "rgba(255, 140, 0, 0.24)",
                "label_border": "rgba(255, 140, 0, 0.58)",
                "handle_bg": "rgba(255, 140, 0, 0.08)",
                "handle_bg_hover": "rgba(255, 140, 0, 0.14)",
                "handle_bg_pressed": "rgba(255, 140, 0, 0.20)",
                "handle_border": "rgba(255, 140, 0, 0.60)",
                "handle_border_hover": "rgba(255, 140, 0, 0.76)",
                "handle_border_pressed": "rgba(255, 140, 0, 0.88)",
            },
            "blue": {
                "label_fg": "#bfdcff",
                "label_bg": "rgba(49, 132, 255, 0.24)",
                "label_border": "rgba(49, 132, 255, 0.58)",
                "handle_bg": "rgba(49, 132, 255, 0.08)",
                "handle_bg_hover": "rgba(49, 132, 255, 0.14)",
                "handle_bg_pressed": "rgba(49, 132, 255, 0.20)",
                "handle_border": "rgba(49, 132, 255, 0.60)",
                "handle_border_hover": "rgba(49, 132, 255, 0.76)",
                "handle_border_pressed": "rgba(49, 132, 255, 0.88)",
            },
            "magenta": {
                "label_fg": "#ffc3f8",
                "label_bg": "rgba(225, 64, 205, 0.24)",
                "label_border": "rgba(225, 64, 205, 0.58)",
                "handle_bg": "rgba(225, 64, 205, 0.08)",
                "handle_bg_hover": "rgba(225, 64, 205, 0.14)",
                "handle_bg_pressed": "rgba(225, 64, 205, 0.20)",
                "handle_border": "rgba(225, 64, 205, 0.60)",
                "handle_border_hover": "rgba(225, 64, 205, 0.76)",
                "handle_border_pressed": "rgba(225, 64, 205, 0.88)",
            },
            "turquoise": {
                "label_fg": "#baf7ef",
                "label_bg": "rgba(34, 211, 196, 0.24)",
                "label_border": "rgba(34, 211, 196, 0.60)",
                "handle_bg": "rgba(34, 211, 196, 0.08)",
                "handle_bg_hover": "rgba(34, 211, 196, 0.14)",
                "handle_bg_pressed": "rgba(34, 211, 196, 0.20)",
                "handle_border": "rgba(34, 211, 196, 0.62)",
                "handle_border_hover": "rgba(34, 211, 196, 0.78)",
                "handle_border_pressed": "rgba(34, 211, 196, 0.90)",
            },
            "sunyellow": {
                "label_fg": "#fff3ac",
                "label_bg": "rgba(255, 204, 0, 0.24)",
                "label_border": "rgba(255, 204, 0, 0.62)",
                "handle_bg": "rgba(255, 204, 0, 0.08)",
                "handle_bg_hover": "rgba(255, 204, 0, 0.16)",
                "handle_bg_pressed": "rgba(255, 204, 0, 0.26)",
                "handle_border": "rgba(255, 204, 0, 0.64)",
                "handle_border_hover": "rgba(255, 204, 0, 0.82)",
                "handle_border_pressed": "rgba(255, 204, 0, 0.94)",
            },
        }
        return palettes.get(str(category or "").strip().lower(), palettes["blue"])

    def _control_monitor_group_top_margin(
        self,
        current_category: str,
        previous_category: str | None = None,
    ) -> int:
        normalized_current = str(current_category or "").strip().lower()
        normalized_previous = str(previous_category or "").strip().lower()
        if not normalized_previous or normalized_current != normalized_previous:
            return 3
        return 0

    def _control_monitor_splitter_handle_style(self, category: str) -> str:
        palette = self._control_monitor_splitter_palette(category)
        return (
            "QSplitterHandle {"
            f" background-color: {palette['handle_bg']};"
            " border: none;"
            f" border-left: 2px solid {palette['handle_border']};"
            " border-radius: 6px;"
            "}"
            "QSplitterHandle:hover {"
            f" background-color: {palette['handle_bg_hover']};"
            f" border-left: 2px solid {palette['handle_border_hover']};"
            "}"
            "QSplitterHandle:pressed {"
            f" background-color: {palette['handle_bg_pressed']};"
            f" border-left: 2px solid {palette['handle_border_pressed']};"
            "}"
        )

    def _control_monitor_embedded_tab_bar_style(self, category: str) -> str:
        palette = self._control_monitor_splitter_palette(category)
        return (
            "QTabBar#extensionsEmbeddedTabBar {"
            " background-color: transparent;"
            " border: none;"
            " border-top: 0px solid transparent;"
            " margin: 0px;"
            " margin-bottom: 4px;"
            " padding-bottom: 4px;"
            "}"
            "QTabBar#extensionsEmbeddedTabBar:hover {"
            " background-color: transparent;"
            " border: none;"
            "}"
            "QTabBar#extensionsEmbeddedTabBar::tab {"
            f" color: {palette['label_fg']};"
            f" background-color: {palette['label_bg']};"
            f" border: 1px solid {palette['label_border']};"
            " border-radius: 6px;"
            " font-size: 10px;"
            " font-weight: 700;"
            " padding: 0px 28px 0px 9px;"
            " margin: 0px 2px 4px 0px;"
            " margin-bottom: 4px;"
            " min-width: 0px;"
            " min-height: 16px;"
            "}"
            "QTabBar#extensionsEmbeddedTabBar::tab:hover {"
            f" color: {palette['label_fg']};"
            f" background-color: {palette['handle_bg_hover']};"
            f" border: 1px solid {palette['handle_border_hover']};"
            " border-radius: 6px;"
            "}"
            "QTabBar#extensionsEmbeddedTabBar::tab:selected {"
            f" color: {palette['label_fg']};"
            f" background-color: {palette['handle_bg_pressed']};"
            f" border: 1px solid {palette['handle_border_pressed']};"
            " border-radius: 6px;"
            "}"
            "QTabBar#extensionsEmbeddedTabBar::close-button {"
            " image: none;"
            " width: 0px;"
            " height: 0px;"
            " margin: 0px;"
            " padding: 0px;"
            "}"
            "QTabBar#extensionsEmbeddedTabBar::scroller {"
            " width: 0px;"
            "}"
            "QToolButton#extensionsEmbeddedTabCloseButton {"
            f" color: {palette['label_fg']};"
            f" background-color: {palette['label_bg']};"
            f" border: 1px solid {palette['label_border']};"
            " border-radius: 6px;"
            " font-size: 10px;"
            " font-weight: 700;"
            " padding: 0px;"
            " margin: 0px;"
            "}"
            "QToolButton#extensionsEmbeddedTabCloseButton:hover {"
            f" background-color: {palette['handle_bg_hover']};"
            f" border: 1px solid {palette['handle_border_hover']};"
            "}"
        )

    def _control_monitor_embedded_tab_add_button_style(self, category: str) -> str:
        palette = self._control_monitor_splitter_palette(category)
        chrome_bg = str(self.scheme.get("col9") or "#101010")
        return (
            "QToolButton#extensionsEmbeddedTabAddButton {"
            f" color: {palette['label_fg']};"
            f" background-color: {chrome_bg};"
            " border: 1px solid transparent;"
            " border-radius: 6px;"
            " padding: 1px 5px;"
            "}"
            "QToolButton#extensionsEmbeddedTabAddButton:hover {"
            f" background-color: {chrome_bg};"
            " border: 1px solid transparent;"
            "}"
        )

    def _apply_control_monitor_splitter_label_style(
        self,
        handle_label: QLabel | None,
        category: str,
        *,
        font_size_px: int,
    ) -> None:
        if not isinstance(handle_label, QLabel):
            return

        palette = self._control_monitor_splitter_palette(category)
        handle_label.setStyleSheet(
            (
                f"color: {palette['label_fg']};"
                "font-weight: 700;"
                f"font-size: {max(9, int(font_size_px))}px;"
                f"background: {palette['label_bg']};"
                f"border: 1px solid {palette['label_border']};"
                "border-radius: 6px;"
                "padding: 1px 9px;"
            )
        )

    def _apply_control_splitter_handle_styles(self) -> None:
        host_splitter = getattr(self, "config_monitor_host_splitter", None)
        if isinstance(host_splitter, QSplitter):
            host_category = "magenta"
            host_handle = host_splitter.handle(1)
            if isinstance(host_handle, QWidget):
                host_category = str(host_handle.property("control_splitter_category") or host_category)
                host_handle.setStyleSheet(self._control_monitor_splitter_handle_style(host_category))
            self._apply_control_monitor_splitter_label_style(
                getattr(self, "config_monitor_host_splitter_label", None),
                host_category,
                font_size_px=11,
            )

        section_splitter = getattr(self, "config_monitor_splitter", None)
        controls = getattr(self, "_config_monitor_splitter_handle_controls", None)
        if isinstance(section_splitter, QSplitter):
            for handle_index in range(1, section_splitter.count()):
                section_handle = section_splitter.handle(handle_index)
                if isinstance(section_handle, QWidget):
                    section_category = "blue"
                    if isinstance(controls, dict):
                        control = controls.get(handle_index)
                        if isinstance(control, dict):
                            section_category = str(control.get("category") or section_category)
                    section_handle.setStyleSheet(self._control_monitor_splitter_handle_style(section_category))

        self._apply_control_splitter_toggle_button_style(
            getattr(self, "config_monitor_host_splitter_toggle", None)
        )

        if isinstance(controls, dict):
            for control in controls.values():
                if not isinstance(control, dict):
                    continue
                self._apply_control_splitter_toggle_button_style(
                    control.get("toggle_button") if isinstance(control.get("toggle_button"), _SplitterToggleGlyph) else None
                )
                self._apply_control_monitor_splitter_label_style(
                    control.get("label") if isinstance(control.get("label"), QLabel) else None,
                    str(control.get("category") or "blue"),
                    font_size_px=10,
                )
                action_container = control.get("action_container")
                if isinstance(action_container, QWidget):
                    section_category = str(control.get("category") or "blue")
                    for tab_bar in action_container.findChildren(QTabBar, "extensionsEmbeddedTabBar"):
                        tab_bar.setStyleSheet(
                            self._control_monitor_embedded_tab_bar_style(section_category)
                        )
                    for add_button in action_container.findChildren(
                        QToolButton, "extensionsEmbeddedTabAddButton"
                    ):
                        add_button.setStyleSheet(
                            self._control_monitor_embedded_tab_add_button_style(section_category)
                        )

    def _set_splitter_dropdown_expanded(
        self,
        *,
        section: QFrame,
        content_widget: QWidget,
        expanded: bool,
        remember_size: dict[str, int],
        apply_splitter_sizes: bool = True,
    ) -> None:
        splitter_parent = section.parentWidget()
        splitter_handle_extent = 22
        if isinstance(splitter_parent, QSplitter):
            splitter_handle_extent = max(12, int(splitter_parent.handleWidth()))

        section_state = self._config_monitor_section_state_for(section)
        if isinstance(section_state, dict):
            section_state["expanded"] = bool(expanded)

        content_widget.setVisible(bool(expanded))

        if expanded:
            expanded_size = max(int(remember_size.get("expanded_size", 180)), splitter_handle_extent * 6)
            section.setMinimumHeight(expanded_size)
            section.setMaximumHeight(16777215)
            section.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            content_widget.setMinimumHeight(max(96, expanded_size - splitter_handle_extent - 16))
        else:
            if isinstance(splitter_parent, QSplitter):
                section_index = splitter_parent.indexOf(section)
                splitter_sizes = splitter_parent.sizes()
                if 0 <= section_index < len(splitter_sizes):
                    remember_size["expanded_size"] = max(
                        int(remember_size.get("expanded_size", 0)),
                        int(splitter_sizes[section_index]),
                    )

            section.setMinimumHeight(0)
            section.setMaximumHeight(0)
            section.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            content_widget.setMinimumHeight(0)

        section.updateGeometry()

        if not apply_splitter_sizes:
            return

        if isinstance(splitter_parent, QSplitter):
            section_index = splitter_parent.indexOf(section)
            splitter_sizes = splitter_parent.sizes()
            if 0 <= section_index < len(splitter_sizes):
                target_size = max(int(remember_size.get("expanded_size", 180)), splitter_handle_extent * 6) if expanded else 0
                splitter_sizes[section_index] = target_size
                splitter_parent.setSizes(splitter_sizes)

            if splitter_parent is getattr(self, "config_monitor_splitter", None):
                self._sync_config_monitor_splitter_handle_states()

    def _create_splitter_dropdown_section(
        self,
        *,
        parent: QSplitter,
        title: str,
        content_widget: QWidget,
        expanded: bool = True,
    ) -> QFrame:
        section = QFrame(parent)
        section.setObjectName("controlDropdownSection")
        section.setFrameShape(QFrame.NoFrame)
        section.setMinimumSize(0, 0)
        section.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(6, 6, 6, 6)
        section_layout.setSpacing(6)

        section_title = str(title or "Widget")

        content_widget.setParent(section)
        content_widget.setMinimumSize(0, 0)
        content_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        section_layout.addWidget(content_widget, 1)

        baseline_size = max(160, int(content_widget.sizeHint().height()) + 36)
        remember_size = {
            "expanded_size": baseline_size,
            "baseline_size": baseline_size,
            "large_expanded": False,
        }
        section.setProperty("control_dropdown_title", section_title)

        state_map = getattr(self, "_config_monitor_section_state", None)
        if isinstance(state_map, dict):
            state_map[section] = {
                "title": section_title,
                "content_widget": content_widget,
                "remember_size": remember_size,
                "expanded": bool(expanded),
            }

        self._set_splitter_dropdown_expanded(
            section=section,
            content_widget=content_widget,
            expanded=bool(expanded),
            remember_size=remember_size,
            apply_splitter_sizes=False,
        )
        return section

    def _create_operator_action_tile(
        self,
        icon_name: str,
        label_text: str,
        tooltip: str,
        slot,
        parent: QWidget,
    ) -> tuple[QFrame, ToolButton]:
        tile = QFrame(parent)
        tile.setObjectName("controlMetricCard")
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        button = ToolButton(icon_name, tooltip, slot=slot, parent=tile)
        button.setFixedSize(32, 32)
        layout.addWidget(button, 0, Qt.AlignHCenter)

        if str(label_text or "").strip():
            label = QLabel(label_text, tile)
            label.setObjectName("controlMeta")
            label.setAlignment(Qt.AlignHCenter)
            label.setWordWrap(True)
            layout.addWidget(label, 0, Qt.AlignHCenter)
        return tile, button

    def _set_config_builder_visible(self, visible: bool) -> None:
        if not hasattr(self, "tabs"):
            return
        config_tab = getattr(self, "_config_tab", None)

        if visible:
            self._ensure_builder_runtime_tab(activate=True, persist=False)
            return

        if config_tab is not None:
            self.tabs.setCurrentWidget(config_tab)

    def _clear_config_builder_panel(self) -> None:
        panel = getattr(self, "_config_builder_panel", None)
        if isinstance(panel, QWidget):
            panel.setParent(None)
            panel.deleteLater()
        self._config_builder_panel = None

    def _close_config_builder_panel(self) -> None:
        # Legacy wrapper: keep the panel mounted and collapse via splitter only.
        self._set_config_builder_visible(False)

    def _mount_config_builder_panel(self, panel: QWidget) -> None:
        self._clear_config_builder_panel()
        self._config_builder_panel = panel
        self._set_config_builder_visible(True)

    def _create_agent_system_builder_config_panel(
        self,
        *,
        initial_payload: dict[str, Any],
        build_handler: Callable[[dict[str, Any]], dict[str, Any]],
        parent_container: QWidget | None = None,
        show_toolbar: bool = True,
    ) -> QWidget:
        panel_parent = parent_container if isinstance(parent_container, QWidget) else self
        panel = QFrame(panel_parent)
        panel.setObjectName("controlBuilderPanel")
        _clear_frame_chrome(panel)
        panel.setProperty("_builder_show_toolbar", bool(show_toolbar))
        self._apply_builder_panel_scheme(panel, show_toolbar=show_toolbar)

        panel_layout = QVBoxLayout(panel)
        if show_toolbar:
            panel_layout.setContentsMargins(12, 12, 12, 12)
            panel_layout.setSpacing(8)
        else:
            panel_layout.setContentsMargins(0, 0, 0, 0)
            panel_layout.setSpacing(0)

        toolbar_widget = QWidget(panel)
        toolbar_widget.setObjectName("builderToolbarWidget")
        top_buttons = QHBoxLayout(toolbar_widget)
        top_buttons.setContentsMargins(0, 0, 0, 0)
        top_buttons.setSpacing(6)
        btn_template = QPushButton("", panel)
        btn_build = QPushButton("", panel)
        btn_post = QPushButton("", panel)
        btn_copy = QPushButton("", panel)
        btn_template.setIcon(_icon("open_file.svg"))
        btn_build.setIcon(_icon("deployed_code.svg"))
        btn_post.setIcon(_icon("send.svg"))
        btn_copy.setIcon(_icon("file_export_24dp_666666_FILL0_wght400_GRAD0_opsz24.svg"))
        btn_template.setToolTip("Template laden")
        btn_build.setToolTip("Sync Build starten")
        btn_post.setToolTip("Ergebnis ins Operations-Log schreiben")
        btn_copy.setToolTip("JSON exportieren")
        btn_template.setIconSize(QSize(18, 18))
        btn_build.setIconSize(QSize(18, 18))
        btn_post.setIconSize(QSize(18, 18))
        btn_copy.setIconSize(QSize(18, 18))
        btn_template.setCursor(Qt.PointingHandCursor)
        btn_build.setCursor(Qt.PointingHandCursor)
        btn_post.setCursor(Qt.PointingHandCursor)
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_template.setObjectName("builderTemplateButton")
        btn_build.setObjectName("builderBuildButton")
        btn_post.setObjectName("builderPostButton")
        btn_copy.setObjectName("builderCopyButton")
        top_buttons.addStretch(1)
        top_buttons.addWidget(btn_template, 0)
        top_buttons.addWidget(btn_build, 0)
        top_buttons.addWidget(btn_post, 0)
        top_buttons.addWidget(btn_copy, 0)
        panel_layout.addWidget(toolbar_widget)
        if not show_toolbar:
            toolbar_widget.hide()

        editor = CodeViewer(
            json.dumps(initial_payload, ensure_ascii=False, indent=2),
            panel,
            language="json",
            editable=True,
            auto_fit=False,
            accent_color=self.scheme.get("col10", "#1f1f1f"),
            accent_selection_color=self.scheme.get("col2", "#58ed5b"),
            background_color=self.scheme.get("col7", "#0b0b0b"),
            surface_color=self.scheme.get("col7", "#0b0b0b"),
            font_size_px=14,
            edit_border_radius_px=15,
            top_left_radius_px=0,
            top_right_radius_px=0,
            bottom_left_radius_px=_SURFACE_BORDER_RADIUS_PX if not show_toolbar else 0,
            bottom_right_radius_px=_SURFACE_BORDER_RADIUS_PX if not show_toolbar else 0,
            draw_border=False,
        )
        editor.setMinimumHeight(96)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        panel_layout.addWidget(editor)

        latest_result: dict[str, Any] = {}

        def _set_builder_status(message: str, timeout_ms: int = 4500) -> None:
            try:
                window = self.window()
                status_bar_getter = getattr(window, "statusBar", None)
                if not callable(status_bar_getter):
                    return
                status_bar = status_bar_getter()
                if status_bar is not None:
                    status_bar.showMessage(f"Builder: {message}", timeout_ms)
            except Exception:
                pass

        _set_builder_status("Bereit")

        def _load_template() -> None:
            editor.setPlainText(json.dumps(initial_payload, ensure_ascii=False, indent=2))
            _set_builder_status("Template geladen")

        def _run_build() -> None:
            nonlocal latest_result
            raw_text = editor.toPlainText().strip()
            if not raw_text:
                _set_builder_status("Payload ist leer")
                return

            try:
                payload = json.loads(raw_text)
            except Exception as exc:
                _set_builder_status(f"JSON-Fehler ({type(exc).__name__})")
                return

            if not isinstance(payload, dict):
                _set_builder_status("Payload muss JSON-Objekt sein")
                return

            btn_build.setEnabled(False)
            try:
                latest_result = dict(build_handler(payload) or {})
                validation = dict(latest_result.get("validation") or {})
                _set_builder_status(
                    f"Build abgeschlossen (valid={bool(validation.get('valid', True))})"
                )
            except Exception as exc:
                _set_builder_status(f"Build fehlgeschlagen ({type(exc).__name__})")
                latest_result = {}
            finally:
                btn_build.setEnabled(True)

        def _post_result() -> None:
            if not latest_result:
                self._append_operator_log("Agent builder has no result yet. Run Sync Build first.")
                _set_builder_status("Kein Ergebnis zum Loggen")
                return
            validation = dict(latest_result.get("validation") or {})
            system_name = str(latest_result.get("system_name") or "agent_system")
            self._append_operator_log(
                f"Agent builder completed: system={system_name} valid={bool(validation.get('valid', True))}"
            )
            _set_builder_status("Ergebnis im Operations-Log vermerkt")

        def _copy_json() -> None:
            payload_text = editor.toPlainText()
            try:
                QApplication.clipboard().setText(payload_text)
                _set_builder_status("JSON in Zwischenablage")
            except Exception as exc:
                _set_builder_status(f"Kopieren fehlgeschlagen ({type(exc).__name__})")

        btn_template.clicked.connect(_load_template)
        btn_build.clicked.connect(_run_build)
        btn_post.clicked.connect(_post_result)
        btn_copy.clicked.connect(_copy_json)
        return panel

    def _open_agent_system_builder_in_configuration_tab(self) -> None:
        # Compatibility wrapper: the builder lives in runtime-tab logic.
        self._ensure_builder_runtime_tab(activate=True, persist=True)

    def _build_agent_system_template(self, system_name: str, route_prefix: str) -> dict[str, Any]:
        resolved_system_name = str(system_name or "agent_system").strip() or "agent_system"
        resolved_route_prefix = str(route_prefix or "/create agents").strip() or "/create agents"

        try:
            if __package__:
                from .agents_config import AgentSystemBuilderRequestObject  # type: ignore
            else:
                from alde.agents_config import AgentSystemBuilderRequestObject  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from alde.agents_config import AgentSystemBuilderRequestObject  # type: ignore
            else:
                raise

        request_object = AgentSystemBuilderRequestObject(
            resolved_system_name,
            {
                "system_name": resolved_system_name,
                "route_prefix": resolved_route_prefix,
            },
        )
        request_config = request_object.to_config_dict()
        integration_targets = dict(request_config.get("integration_targets") or {})
        persisted_target = str(integration_targets.get("persisted_config_target") or "").strip()

        return {
            "action": "build_agent_system_configs",
            "section_identity": {
                "system_name": request_config.get("system_name"),
                "system_slug": request_config.get("system_slug"),
                "route_prefix": request_config.get("route_prefix"),
                "route_name": request_config.get("route_name"),
            },
            "section_agents": {
                "assistant_agent_name": request_config.get("assistant_agent_name"),
                "planner_agent_name": request_config.get("planner_agent_name"),
                "worker_agent_name": request_config.get("worker_agent_name"),
                "planner_prompt_name": request_config.get("planner_prompt_name"),
                "worker_prompt_name": request_config.get("worker_prompt_name"),
                "planner_model": request_config.get("planner_model"),
                "worker_model": request_config.get("worker_model"),
                "agent_specs": request_config.get("agent_specs"),
            },
            "section_workflows": {
                "planner_workflow_name": request_config.get("planner_workflow_name"),
                "builder_workflow_name": request_config.get("builder_workflow_name"),
                "workflow_specs": request_config.get("workflow_specs"),
            },
            "section_handoff_and_action": {
                "primary_to_planner_schema_name": request_config.get("primary_to_planner_schema_name"),
                "planner_to_builder_schema_name": request_config.get("planner_to_builder_schema_name"),
                "action_request_schema_name": request_config.get("action_request_schema_name"),
                "action_tool_name": request_config.get("action_tool_name"),
            },
            "section_planning": {
                "planning_schema": request_config.get("planning_schema"),
            },
            "section_integration": {
                "integration_targets": integration_targets,
            },
            "section_execution": {
                "write_file": False,
                "persist_path": persisted_target,
            },
        }

    def _resolve_builder_request_from_sections(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        request_payload = dict(payload or {})
        execution_payload: dict[str, Any] = {}

        section_names = (
            "section_identity",
            "section_agents",
            "section_workflows",
            "section_handoff_and_action",
            "section_planning",
            "section_integration",
            "section_execution",
        )

        for section_name in section_names:
            section_value = request_payload.pop(section_name, None)
            if not isinstance(section_value, dict):
                continue
            if section_name == "section_execution":
                execution_payload.update(section_value)
                continue
            for key, value in section_value.items():
                if key not in request_payload or request_payload.get(key) in (None, "", [], {}):
                    request_payload[key] = value

        return request_payload, execution_payload

    def _run_agent_system_builder_sync(
        self,
        *,
        system_name: str,
        request_payload: dict[str, Any],
        write_file: bool,
        persist_path: str | None,
    ) -> dict[str, Any]:
        try:
            if __package__:
                from .agents_tools import build_agent_system_configs_tool  # type: ignore
            else:
                from alde.agents_tools import build_agent_system_configs_tool  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from alde.agents_tools import build_agent_system_configs_tool  # type: ignore
            else:
                raise

        result_text = build_agent_system_configs_tool(
            system_name=system_name,
            action_request=request_payload,
            persist_path=persist_path,
            write_file=write_file,
        )

        if isinstance(result_text, str):
            try:
                result = json.loads(result_text)
            except Exception as exc:
                raise ValueError(f"Builder returned non-JSON output: {exc}") from exc
        elif isinstance(result_text, dict):
            result = result_text
        else:
            raise ValueError("Builder returned unsupported result type")

        if isinstance(result, dict) and result.get("ok") is False:
            error_text = str(result.get("error") or "unknown_builder_error")
            raise ValueError(error_text)

        return dict(result) if isinstance(result, dict) else {"result": result}

    def _execute_agent_system_builder_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_payload, execution_payload = self._resolve_builder_request_from_sections(payload)
        system_name = str(request_payload.get("system_name") or "agent_system").strip() or "agent_system"
        request_payload.setdefault("system_name", system_name)
        request_payload.setdefault("route_prefix", "/create agents")

        write_file = bool(execution_payload.get("write_file"))
        persist_path_text = str(execution_payload.get("persist_path") or "").strip()
        persist_path = persist_path_text or None

        return self._run_agent_system_builder_sync(
            system_name=system_name,
            request_payload=request_payload,
             persist_path=persist_path,
        )

    def _resolve_ai_widget(self) -> AIWidget | None:
        window = self.window()
        chat_dock = getattr(window, "chat_dock", None)
        chat_widget = chat_dock.widget() if chat_dock is not None and hasattr(chat_dock, "widget") else None
        if isinstance(chat_widget, AIWidget):
            return chat_widget
        return None

    def _open_agent_system_builder_in_ai_chat(self) -> None:
        try:
            self._open_agent_system_builder_in_configuration_tab()
            self._append_operator_log("Agent builder panel opened in Builder runtime tab")
        except Exception as exc:
            self._append_operator_log(f"Agent builder panel failed: {type(exc).__name__}: {exc}")
            self._open_agent_system_builder_dialog()

    def _open_agent_system_builder_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Agent System Builder (Sync, lokal)")
        dialog.resize(980, 760)

        root = QVBoxLayout(dialog)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        intro = QLabel(
            "Builder-Dict in Sections bearbeiten und synchron lokal ausfuehren. Async folgt spaeter.",
            dialog,
        )
        intro.setWordWrap(True)
        intro.setObjectName("controlMeta")
        root.addWidget(intro)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        system_name_edit = QLineEdit("agent_system", dialog)
        route_prefix_edit = QLineEdit("/create agents", dialog)
        persist_path_edit = QLineEdit("", dialog)
        write_file_box = QCheckBox("Persisted module auf Disk schreiben", dialog)

        form.addRow("System Name", system_name_edit)
        form.addRow("Route Prefix", route_prefix_edit)
        form.addRow("Persist Path", persist_path_edit)
        form.addRow("Sync Build", write_file_box)
        root.addLayout(form)

        editor_label = QLabel("Builder Dict (sectioned)", dialog)
        editor_label.setObjectName("controlMeta")
        root.addWidget(editor_label)

        payload_editor = QPlainTextEdit(dialog)
        payload_editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        payload_editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        payload_editor.setStyleSheet("QPlainTextEdit { font-size: 17px; }")
        root.addWidget(payload_editor, 1)

        result_label = QLabel("Build Result", dialog)
        result_label.setObjectName("controlMeta")
        root.addWidget(result_label)

        result_view = QPlainTextEdit(dialog)
        result_view.setReadOnly(True)
        result_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        result_view.setFixedHeight(180)
        root.addWidget(result_view)

        button_box = QDialogButtonBox(dialog)
        btn_template = button_box.addButton("Template laden", QDialogButtonBox.ActionRole)
        btn_build = button_box.addButton("Sync Build starten", QDialogButtonBox.AcceptRole)
        btn_close = button_box.addButton(QDialogButtonBox.Close)
        root.addWidget(button_box)

        def load_template() -> None:
            try:
                template = self._build_agent_system_template(
                    system_name_edit.text().strip() or "agent_system",
                    route_prefix_edit.text().strip() or "/create agents",
                )
                payload_editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
                exec_section = dict(template.get("section_execution") or {})
                if not persist_path_edit.text().strip():
                    persist_path_edit.setText(str(exec_section.get("persist_path") or ""))
                write_file_box.setChecked(bool(exec_section.get("write_file")))
                result_view.setPlainText("Template geladen.")
            except Exception as exc:
                result_view.setPlainText(f"Template konnte nicht geladen werden:\n{type(exc).__name__}: {exc}")

        def run_build_sync() -> None:
            raw_text = payload_editor.toPlainText().strip()
            if not raw_text:
                result_view.setPlainText("Builder Dict ist leer. Bitte Template laden oder JSON einfuegen.")
                return

            try:
                payload = json.loads(raw_text)
            except Exception as exc:
                result_view.setPlainText(f"Ungueltiges JSON:\n{type(exc).__name__}: {exc}")
                return

            if not isinstance(payload, dict):
                result_view.setPlainText("Builder Dict muss ein JSON-Objekt sein.")
                return

            request_payload, execution_payload = self._resolve_builder_request_from_sections(payload)
            system_name = str(
                request_payload.get("system_name")
                or system_name_edit.text().strip()
                or "agent_system"
            ).strip() or "agent_system"
            request_payload.setdefault("system_name", system_name)
            request_payload.setdefault(
                "route_prefix",
                str(route_prefix_edit.text().strip() or "/create agents").strip() or "/create agents",
            )

            write_file = bool(
                execution_payload.get("write_file")
                if "write_file" in execution_payload
                else write_file_box.isChecked()
            )
            persist_path = str(
                execution_payload.get("persist_path")
                or persist_path_edit.text().strip()
                or ""
            ).strip()
            resolved_persist_path = persist_path or None

            dialog.setCursor(Qt.WaitCursor)
            btn_build.setEnabled(False)
            try:
                result = self._run_agent_system_builder_sync(
                    system_name=system_name,
                    request_payload=request_payload,
                    write_file=write_file,
                    persist_path=resolved_persist_path,
                )
                result_view.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
                validation = dict(result.get("validation") or {}) if isinstance(result, dict) else {}
                is_valid = bool(validation.get("valid", True))
                self._append_operator_log(
                    f"Agent builder completed: system={system_name} valid={is_valid} write_file={write_file}"
                )
            except Exception as exc:
                result_view.setPlainText(f"Sync Build fehlgeschlagen:\n{type(exc).__name__}: {exc}")
                self._append_operator_log(
                    f"Agent builder failed: system={system_name} error={type(exc).__name__}: {exc}"
                )
            finally:
                dialog.unsetCursor()
                btn_build.setEnabled(True)

        btn_template.clicked.connect(load_template)
        btn_build.clicked.connect(run_build_sync)
        btn_close.clicked.connect(dialog.reject)

        load_template()
        dialog.exec()

    def _render_operator_status_row(self, title: str, chip_html: str, detail: str, note: str = "") -> str:
        note_html = (
            f"<br><span style=\"color:{self.scheme['col8']};\">{html.escape(note)}</span>"
            if note else ""
        )
        return (
            f"<li><b>{html.escape(title)}:</b> {chip_html} {html.escape(detail)}{note_html}</li>"
        )

    def _trace_entry_agent_label(self, trace_entry: dict[str, Any]) -> str:
        return str(trace_entry.get("agent_label") or trace_entry.get("assistant_name") or "").strip()

    def _trace_entry_workflow_name(self, trace_entry: dict[str, Any]) -> str:
        return str(trace_entry.get("workflow_name") or "").strip()

    def _trace_entry_tool_names(self, trace_entry: dict[str, Any]) -> list[str]:
        tool_names: list[str] = []
        direct_tool = str(trace_entry.get("tool_name") or "").strip()
        if direct_tool:
            tool_names.append(direct_tool)
        for tool_call in trace_entry.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function_object = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            tool_name = str(function_object.get("name") or tool_call.get("name") or "").strip()
            if tool_name:
                tool_names.append(tool_name)
        return sorted({name for name in tool_names if name})

    def _trace_entry_handoff_value(self, trace_entry: dict[str, Any]) -> str:
        handoff = trace_entry.get("handoff") if isinstance(trace_entry.get("handoff"), dict) else {}
        source_agent = str(handoff.get("source_agent") or "").strip() or "unknown"
        target_agent = str(handoff.get("target_agent") or "").strip()
        if not target_agent:
            return ""
        protocol = str(handoff.get("protocol") or "").strip()
        suffix = f" [{protocol}]" if protocol else ""
        return f"{source_agent}->{target_agent}{suffix}"

    def _filtered_trace_entries(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        selected_agent = self.trace_agent_selector.currentText().strip()
        selected_workflow = self.trace_workflow_selector.currentText().strip()
        selected_tool = self.trace_tool_selector.currentText().strip()
        selected_handoff = self.trace_handoff_selector.currentText().strip()

        filtered_entries: list[dict[str, Any]] = []
        for trace_entry in snapshot.get("trace") or []:
            if not isinstance(trace_entry, dict):
                continue
            agent_label = self._trace_entry_agent_label(trace_entry)
            workflow_name = self._trace_entry_workflow_name(trace_entry)
            tool_names = self._trace_entry_tool_names(trace_entry)
            handoff_value = self._trace_entry_handoff_value(trace_entry)

            if selected_agent and selected_agent != "All agents" and agent_label != selected_agent:
                continue
            if selected_workflow and selected_workflow != "All workflows" and workflow_name != selected_workflow:
                continue
            if selected_tool and selected_tool != "All tools" and selected_tool not in tool_names:
                continue
            if selected_handoff:
                if selected_handoff == "All handoffs":
                    pass
                elif selected_handoff == "Handoff only":
                    if not handoff_value:
                        continue
                elif handoff_value != selected_handoff:
                    continue
            filtered_entries.append(trace_entry)
        return filtered_entries

    def _refresh_monitoring_views(self) -> None:
        monitoring_snapshot = dict(self._last_snapshot.get("monitoring") or {})
        if monitoring_snapshot:
            board_context = self._board_context_from_object(self.sender()) or self._active_board_context()
            with self._board_context_scope(board_context):
                self._render_monitoring_snapshot(monitoring_snapshot)

    def _render_monitor_trace_block(self, label: str, value: Any) -> str:
        if value in (None, "", {}, []):
            return ""
        if isinstance(value, str):
            body = html.escape(value)
        else:
            body = html.escape(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return f"<h5>{html.escape(label)}</h5><pre>{body}</pre>"

    def _render_monitor_trace_entry(self, trace_entry: dict[str, Any]) -> str:
        meta_parts = [
            f"kind={html.escape(str(trace_entry.get('trace_kind') or 'message'))}",
            f"role={html.escape(str(trace_entry.get('role') or 'n/a'))}",
            f"agent={html.escape(str(trace_entry.get('agent_label') or trace_entry.get('assistant_name') or 'n/a'))}",
            f"workflow={html.escape(str(trace_entry.get('workflow_name') or 'n/a'))}",
        ]
        if trace_entry.get("tool_name"):
            meta_parts.append(f"tool={html.escape(str(trace_entry.get('tool_name')))}")
        if trace_entry.get("tool_call_id"):
            meta_parts.append(f"tool_call_id={html.escape(str(trace_entry.get('tool_call_id')))}")
        return "".join(
            [
                f"<h4>{html.escape(str(trace_entry.get('timestamp') or 'n/a'))}</h4>",
                f"<p><b>{html.escape(str(trace_entry.get('summary') or 'trace'))}</b><br><span style=\"color:{self.scheme['col8']};\">{' | '.join(meta_parts)}</span></p>",
                self._render_monitor_trace_block("content", trace_entry.get("content")),
                self._render_monitor_trace_block("tool_calls", trace_entry.get("tool_calls")),
                self._render_monitor_trace_block("handoff", trace_entry.get("handoff")),
                self._render_monitor_trace_block("workflow_payload", trace_entry.get("workflow_payload")),
                self._render_monitor_trace_block("workflow", trace_entry.get("workflow")),
                self._render_monitor_trace_block("workflow_snapshot", trace_entry.get("workflow_snapshot")),
                self._render_monitor_trace_block("data", trace_entry.get("data")),
            ]
        )

    def update_scheme(self, accent: dict[str, str], base: dict[str, str]) -> None:
        self._accent = accent
        self._base = base
        self.scheme = _build_scheme(accent, base)
        handle_idle, handle_hover, handle_pressed = _splitter_handle_palette(self.scheme)
        self.setStyleSheet(
            f"""
            QFrame#controlHero, QFrame#controlMetricCard {{
                background: {self.scheme['col5']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 14px;
            }}
            QFrame#controlDropdownSection {{
                background: {self.scheme['col5']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 12px;
            }}
            QSplitterHandle#controlHostSplitterHandle,
            QSplitterHandle#controlSectionSplitterHandle {{
                background: transparent;
                border: none;
                border-left: 2px solid transparent;
            }}
            QSplitterHandle#controlHostSplitterHandle:hover,
            QSplitterHandle#controlSectionSplitterHandle:hover {{
                border-left: 2px solid transparent;
            }}
            QSplitterHandle#controlHostSplitterHandle:pressed,
            QSplitterHandle#controlSectionSplitterHandle:pressed {{
                border-left: 2px solid transparent;
            }}
            QToolButton#controlHostSplitterHandleToggle {{
                background: transparent;
                border: none;
                color: {self.scheme['col8']};
                padding: 0px;
                min-width: 16px;
                min-height: 16px;
            }}
            QToolButton#controlHostSplitterHandleToggle:hover {{
                background: transparent;
                border: none;
                color: {self.scheme['col8']};
            }}
            QToolButton#controlHostSplitterHandleToggle:pressed,
            QToolButton#controlHostSplitterHandleToggle:checked {{
                background: transparent;
                border: none;
                color: {self.scheme['col1']};
            }}
            QLabel#controlHostSplitterHandleLabel {{
                color: {self.scheme['col8']};
                font-size: 11px;
                font-weight: 700;
                background: transparent;
            }}
            QToolButton#controlSectionSplitterHandleToggle {{
                background: transparent;
                border: none;
                color: {self.scheme['col8']};
                padding: 0px;
                min-width: 16px;
                min-height: 16px;
            }}
            QToolButton#controlSectionSplitterHandleToggle:hover {{
                background: transparent;
                border: none;
                color: {self.scheme['col8']};
            }}
            QToolButton#controlSectionSplitterHandleToggle:pressed,
            QToolButton#controlSectionSplitterHandleToggle:checked {{
                background: transparent;
                border: none;
                color: {self.scheme['col1']};
            }}
            QLabel#controlSectionSplitterHandleLabel {{
                color: {self.scheme['col8']};
                font-size: 10px;
                font-weight: 700;
                background: transparent;
            }}
            QFrame#controlBuilderContainer {{
                background: {self.scheme['col7']};
                border: none;
            }}
            QFrame#BuildTabWidget {{
                background: {self.scheme['col7']};
                border: none;
            }}
            QWidget#BoardTabWidget QFrame#controlBuilderContainer {{
                background: {self.scheme['col7']};
                border: none;
            }}
            QWidget#BoardTabWidget QFrame#BuildTabWidget {{
                background: {self.scheme['col7']};
                border: none;
            }}
            QWidget#BoardTabWidget QFrame#controlBuilderContainer > QFrame#controlBuilderPanel {{
                background: {self.scheme['col7']};
            }}
            QWidget#BoardTabWidget QFrame#controlBuilderContainer QWidget#builderToolbarWidget {{
                background: transparent;
            }}
            QWidget#BoardTabWidget QFrame#BuildTabWidget > QFrame#runtimeWidgetPanel {{
                background: {self.scheme['col7']};
            }}
            QWidget#BoardTabWidget QFrame#BuildTabWidget QWidget#builderToolbarWidget {{
                background: transparent;
            }}
            QTabWidget#controlPlaneTabs::pane {{
                background: {self.scheme['col7']};
                border-left: 1px solid {self.scheme['col10']};
                border-right: 1px solid {self.scheme['col10']};
                border-bottom: 1px solid {self.scheme['col10']};
                border-top: none;
                border-top-left-radius: 0px;
                border-top-right-radius: 0px;
                border-bottom-left-radius: 14px;
                border-bottom-right-radius: 14px;
                margin: 0px;
            }}
            QTabWidget#controlPlaneTabs > QStackedWidget#qt_tabwidget_stackedwidget {{
                background: {self.scheme['col7']};
                border: none;
            }}
            QTabWidget#controlPlaneTabs QTabBar {{
                background: {self.scheme['col7']};
                border: none;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab {{
                background: {self.scheme['col7']};
                color: {self.scheme['col6']};
                border-top: 1px solid {self.scheme['col10']};
                border-left: none;
                border-right: none;
                border-bottom: none;
                border-top-left-radius: 0px;
                border-top-right-radius: 0px;
                padding: 3px 10px;
                min-height: 16px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:first {{
                border-left: 1px solid {self.scheme['col10']};
                border-top-left-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:last {{
                border-right: 1px solid {self.scheme['col10']};
                border-top-right-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:only-one {{
                border-left: 1px solid {self.scheme['col10']};
                border-right: 1px solid {self.scheme['col10']};
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:hover {{
                background: {self.scheme['col7']};
                border-top: 1px solid {self.scheme['col10']};
                border-left: none;
                border-right: none;
                border-bottom: none;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:selected {{
                background: {self.scheme['col9']};
                color: {self.scheme['col1']};
                border: 1px solid {self.scheme['col1']};
                border-left: 1px solid {self.scheme['col1']};
                border-bottom: none;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:first:hover {{
                border-left: 1px solid {self.scheme['col10']};
                border-top-left-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:last:hover {{
                border-right: 1px solid {self.scheme['col10']};
                border-top-right-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:only-one:hover {{
                border-left: 1px solid {self.scheme['col10']};
                border-right: 1px solid {self.scheme['col10']};
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:first:selected {{
                border-left: 1px solid {self.scheme['col1']};
                border-top-left-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:last:selected {{
                border-top-right-radius: 14px;
            }}
            QTabWidget#controlPlaneTabs QTabBar::tab:only-one:selected {{
                border-left: 1px solid {self.scheme['col1']};
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }}
            QLabel#controlTitle {{
                color: {self.scheme['col6']};
                font-size: 18px;
                font-weight: 700;
            }}
            QLabel#controlSubtitle, QLabel#controlMeta, QLabel#controlMetricLabel {{
                color: {self.scheme['col8']};
                font-size: 12px;
            }}
            QLabel#controlMetricValue {{
                color: {self.scheme['col1']};
                font-size: 24px;
                font-weight: 700;
            }}
            QTextBrowser#controlBrowser {{
                background: {self.scheme['col9']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 12px;
                padding: 8px;
                font-size: 13px;
            }}
            QScrollArea#controlMonitoringScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea#controlMonitoringScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QTextBrowser#controlBrowser QScrollBar:vertical,
            QTextBrowser#controlBrowser QScrollBar:horizontal {{
                background: transparent;
                margin: 0px;
                border: none;
            }}
            QTextBrowser#controlBrowser QScrollBar:vertical {{
                width: 6px;
            }}
            QTextBrowser#controlBrowser QScrollBar:horizontal {{
                height: 6px;
            }}
            QTextBrowser#controlBrowser QScrollBar:hover,
            QTextBrowser#controlBrowser QScrollBar:vertical:hover,
            QTextBrowser#controlBrowser QScrollBar:horizontal:hover {{
                background: transparent;
            }}
            QTextBrowser#controlBrowser QScrollBar::handle:vertical,
            QTextBrowser#controlBrowser QScrollBar::handle:horizontal {{
                background: rgba(0, 0, 0, 0.0);
                border-radius: 3px;
                min-height: 28px;
                min-width: 28px;
            }}
            QTextBrowser#controlBrowser QScrollBar::handle:vertical:hover,
            QTextBrowser#controlBrowser QScrollBar::handle:horizontal:hover,
            QTextBrowser#controlBrowser QScrollBar::handle:hover:vertical,
            QTextBrowser#controlBrowser QScrollBar::handle:hover:horizontal {{
                background: {self.scheme['col10']};
            }}
            QTextBrowser#controlBrowser QScrollBar::handle:vertical:pressed,
            QTextBrowser#controlBrowser QScrollBar::handle:horizontal:pressed,
            QTextBrowser#controlBrowser QScrollBar::handle:pressed:vertical,
            QTextBrowser#controlBrowser QScrollBar::handle:pressed:horizontal {{
                background: {self.scheme['col2']};
            }}
            QTextBrowser#controlBrowser QScrollBar::add-line,
            QTextBrowser#controlBrowser QScrollBar::sub-line,
            QTextBrowser#controlBrowser QScrollBar::add-page,
            QTextBrowser#controlBrowser QScrollBar::sub-page {{
                background: none;
                border: none;
                width: 0px;
                height: 0px;
            }}
            QSplitter#controlViewportSplitter::handle:horizontal {{
                background: transparent;
                margin: 0px {_SPLITTER_SIDE_INSET_PX}px;
                min-height: 7px;
                border-radius: 999px;
            }}
            QSplitter#controlViewportSplitter::handle:vertical {{
                background: {handle_idle};
                margin: {_SURFACE_INSET_PX}px 0px;
                min-width: 4px;
                border-radius: 999px;
            }}
            QSplitter#controlViewportSplitter::handle:hover {{
                background: {handle_hover};
            }}
            QSplitter#controlViewportSplitter::handle:pressed {{
                background: {handle_pressed};
            }}
            QSplitter#controlPrimarySplitter::handle:horizontal {{
                background: transparent;
                margin: 0px {_SPLITTER_SIDE_INSET_PX}px;
                min-height: 7px;
                border-radius: 999px;
            }}
            QSplitter#controlPrimarySplitter::handle:vertical {{
                background: {handle_idle};
                margin: 12px 0px;
                min-width: 4px;
                border-radius: 999px;
            }}
            QSplitter#controlPrimarySplitter::handle:hover {{
                background: {handle_hover};
            }}
            QSplitter#controlPrimarySplitter::handle:pressed {{
                background: {handle_pressed};
            }}
            QComboBox#controlSelector {{
                background: {self.scheme['col9']};
                color: {self.scheme['col6']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 10px;
                padding: 6px 10px;
                min-height: 18px;
            }}
            QPushButton#controlRefresh {{
                background: {self.scheme['col5']};
                color: {self.scheme['col6']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 10px;
                padding: 6px 12px;
                font-weight: 600;
            }}
            QPushButton#controlRefresh:hover {{
                background: {self.scheme['col5']};
                border-color: {self.scheme['col10']};
            }}
            QPushButton#controlAction {{
                background: {self.scheme['col5']};
                color: {self.scheme['col6']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 10px;
                padding: 6px 12px;
                font-weight: 600;
            }}
            QPushButton#controlAction:hover {{
                border-color: {self.scheme['col10']};
                color: {self.scheme['col6']};
            }}
            QPushButton#controlLegacyTabButton {{
                background: {self.scheme['col5']};
                color: {self.scheme['col6']};
                border: 1px solid {self.scheme['col10']};
                border-radius: 8px;
                padding: 4px 10px;
                font-weight: 600;
            }}
            QPushButton#controlLegacyTabButton:hover {{
                border-color: {self.scheme['col10']};
                color: {self.scheme['col6']};
            }}
            QWidget#extensionsEmbeddedTabBarHost {{
                background: transparent;
            }}
            QTabBar#extensionsEmbeddedTabBar {{
                background: transparent;
                border-top: 0px solid transparent;
                margin: 0px;
            }}
            QTabBar#extensionsEmbeddedTabBar::tab {{
                color: {self.scheme['col8']};
                background: transparent;
                border: none;
                border-radius: 6px;
                font-size: 10px;
                font-weight: 700;
                padding: 3px 10px;
                margin: 0px 2px 0px 0px;
                min-width: 45px;
                min-height: 16px;
            }}
            QTabBar#extensionsEmbeddedTabBar::tab:hover {{
                color: {self.scheme['col8']};
                background: transparent;
                border: none;
                border-radius: 6px;
            }}
            QTabBar#extensionsEmbeddedTabBar::tab:selected {{
                color: {self.scheme['col1']};
                background: transparent;
                border: none;
                border-radius: 6px;
            }}
            QTabBar#extensionsEmbeddedTabBar::close-button {{
                image: none;
                width: 10px;
            }}
            QTabBar#extensionsEmbeddedTabBar::scroller {{
                width: 0px;
            }}
            QToolButton#extensionsEmbeddedTabAddButton {{
                color: {self.scheme['col8']};
                background: {self.scheme['col9']};
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 2px;
            }}
            QToolButton#extensionsEmbeddedTabAddButton:hover {{
                color: {self.scheme['col6']};
                background: {self.scheme['col9']};
                border: 1px solid transparent;
            }}
            """
        )
        for board_context in self._board_contexts_in_display_order():
            extensions_workspace = board_context.get("extensions_section_workspace")
            if isinstance(extensions_workspace, ExtensionsWorkspaceWidget):
                extensions_workspace.update_scheme(self._accent, self._base)
        self._refresh_runtime_panel_schemes()
        self._apply_control_splitter_handle_styles()
        self._apply_control_plane_tab_corner_widget_style()

    def _load_refresh_payload(self) -> dict[str, Any]:
        configuration_snapshot = self._load_configuration_snapshot()
        monitoring_snapshot = self._load_monitoring_snapshot()
        operator_snapshot = self._load_operator_snapshot_with_context(
            previous_operations=dict(self._last_snapshot.get("operations") or {}),
            recent_action_entries=list(self._operator_log_entries),
        )
        return {
            "configuration": configuration_snapshot,
            "monitoring": monitoring_snapshot,
            "operations": operator_snapshot,
        }

    def refresh_view(self, include_drilldown: bool = False) -> None:
        if self._refresh_inflight:
            self._refresh_pending = True
            self._refresh_pending_include_drilldown = self._refresh_pending_include_drilldown or bool(include_drilldown)
            return

        self._refresh_inflight = True
        self._last_refresh_label.setText("Updating...")
        do_drilldown = bool(include_drilldown)

        def _invoke_worker() -> None:
            try:
                snapshot_payload = self._load_refresh_payload()
                payload: dict[str, Any] = {
                    "ok": True,
                    "snapshot": snapshot_payload,
                    "include_drilldown": do_drilldown,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "include_drilldown": do_drilldown,
                }
            try:
                self._refresh_async_result_ready.emit(payload)
            except RuntimeError:
                # Widget already gone during shutdown; ignore late worker result.
                return

        Thread(target=_invoke_worker, daemon=True).start()

    @Slot(object)
    def _handle_refresh_async_result(self, payload: object) -> None:
        self._refresh_inflight = False
        try:
            if not isinstance(payload, dict):
                return

            if bool(payload.get("ok")):
                snapshot_payload = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
                configuration_snapshot = dict(snapshot_payload.get("configuration") or {})
                monitoring_snapshot = dict(snapshot_payload.get("monitoring") or {})
                operator_snapshot = dict(snapshot_payload.get("operations") or {})
                self._last_snapshot = {
                    "configuration": configuration_snapshot,
                    "monitoring": monitoring_snapshot,
                    "operations": operator_snapshot,
                }
                self._render_all_board_contexts(include_drilldown=bool(payload.get("include_drilldown")))
                self._last_refresh_label.setText(
                    f"Updated {datetime.now().strftime('%H:%M:%S')}"
                )
                self.snapshotChanged.emit(dict(self._last_snapshot))
                return

            error_text = html.escape(str(payload.get("error") or "Unknown refresh error"))
            self._last_snapshot = {
                "configuration": {"agent_count": 0, "workflow_count": 0},
                "monitoring": {"session_count": 0, "failure_count": 0},
                "operations": {"queue_backend": "n/a", "queue_healthy": False},
            }
            for board_context in self._board_contexts_in_display_order():
                with self._board_context_scope(board_context):
                    self.config_summary_view.setHtml(f"<h3>Configuration unavailable</h3><p>{error_text}</p>")
                    self.config_manifest_view.setHtml(
                        "<h3>Manifest projection failed</h3><p>Check agents_config.py imports and runtime state.</p>"
                    )
                    self.monitor_summary_view.setHtml(f"<h3>Monitoring unavailable</h3><p>{error_text}</p>")
                    self.monitor_detail_view.setHtml(
                        "<h3>Drill-down unavailable</h3><p>Workflow status detail could not be projected.</p>"
                    )
                    self.monitor_timeline_view.setHtml(
                        "<h3>Timeline unavailable</h3><p>Runtime event projection could not be loaded.</p>"
                    )
                    self.monitor_trace_view.setHtml(
                        "<h3>Trace unavailable</h3><p>Detailed chat/tool/handoff projection could not be loaded.</p>"
                    )
                    if isinstance(getattr(self, "config_monitor_threat_flow_view", None), QTextBrowser):
                        self.config_monitor_threat_flow_view.setHtml(
                            f"<p>Threat Flow unavailable: {error_text}</p>"
                        )
                    self.trace_agent_selector.clear()
                    self.trace_workflow_selector.clear()
                    self.trace_tool_selector.clear()
                    self.trace_handoff_selector.clear()
                    self.operator_status_selector.clear()
                    self.operator_audit_selector.clear()
                    self.operator_group_selector.clear()
                    self.operator_source_selector.clear()
                    self.operator_summary_view.setHtml(f"<h3>Operations unavailable</h3><p>{error_text}</p>")
                    self._render_operator_log()
            self._last_refresh_label.setText("Update failed")
            self.snapshotChanged.emit(dict(self._last_snapshot))
        finally:
            if self._refresh_pending:
                self._refresh_pending = False
                include_drilldown = self._refresh_pending_include_drilldown
                self._refresh_pending_include_drilldown = False
                self.refresh_view(include_drilldown=include_drilldown)

    @Slot()
    def _refresh_from_panel(self) -> None:
        self.refresh_view(include_drilldown=True)
        try:
            window = self.window()
            status_getter = getattr(window, "statusBar", None)
            if callable(status_getter):
                status_bar = status_getter()
                if status_bar is not None:
                    status_bar.showMessage("Control Plane refresh requested", 2500)
        except Exception:
            pass

    def _load_configuration_snapshot(self) -> dict[str, Any]:
        try:
            if __package__:
                from .agents_config import (  # type: ignore
                    get_agent_manifests,
                    get_tool_configs,
                    get_tool_group_configs,
                    get_workflow_configs,
                )
            else:
                from agents_config import (  # type: ignore
                    get_agent_manifests,
                    get_tool_configs,
                    get_tool_group_configs,
                    get_workflow_configs,
                )
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from alde.agents_config import (  # type: ignore
                    get_agent_manifests,
                    get_tool_configs,
                    get_tool_group_configs,
                    get_workflow_configs,
                )
            else:
                raise

        manifests = get_agent_manifests()
        workflows = get_workflow_configs()
        tool_configs = get_tool_configs()
        tool_groups = get_tool_group_configs()

        role_counts: dict[str, int] = {}
        workflow_usage: dict[str, int] = {}
        agent_rows: list[dict[str, Any]] = []

        for agent_label, manifest in sorted(manifests.items()):
            role = str(manifest.get("role") or "worker")
            workflow_name = str(manifest.get("workflow_name") or "unassigned")
            role_counts[role] = role_counts.get(role, 0) + 1
            workflow_usage[workflow_name] = workflow_usage.get(workflow_name, 0) + 1
            agent_rows.append(
                {
                    "agent_label": agent_label,
                    "role": role,
                    "model": str(manifest.get("model") or "unspecified"),
                    "workflow_name": workflow_name,
                    "tool_count": len(manifest.get("tools") or []),
                    "instance_policy": str(manifest.get("instance_policy") or "ephemeral"),
                }
            )

        providers: list[str] = []
        if os.getenv("OPENAI_API_KEY"):
            providers.append("OpenAI")
        if os.getenv("ANTHROPIC_API_KEY"):
            providers.append("Anthropic")
        if os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_ENDPOINT"):
            providers.append("Azure OpenAI")
        if os.getenv("OLLAMA_HOST") or os.getenv("OLLAMA_BASE_URL"):
            providers.append("Ollama")

        agentsdb_env_configured = any(
            bool(str(os.getenv(env_name, "")).strip())
            for env_name in (
                "AI_IDE_KNOWLEDGE_AGENTS_DB_URI",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_BACKEND_URI",
                "AI_IDE_KNOWLEDGE_AGENTS_DB_CONFIG_PATH",
            )
        )
        legacy_mongo_configured = bool(str(os.getenv("AI_IDE_KNOWLEDGE_MONGO_URI", "")).strip())

        env_rows = [
            ("OpenAI key", bool(os.getenv("OPENAI_API_KEY"))),
            ("Knowledge DB (AgentsDB)", agentsdb_env_configured or legacy_mongo_configured),
            ("Legacy knowledge env", legacy_mongo_configured),
            ("GPU vstore", os.getenv("AI_IDE_VSTORE_GPU_ONLY", "0") in {"1", "true", "True"}),
            ("Verbose HTTP", os.getenv("AI_IDE_VERBOSE_HTTP", "0") in {"1", "true", "True"}),
        ]

        return {
            "agent_count": len(agent_rows),
            "workflow_count": len(workflows),
            "tool_count": len(tool_configs),
            "tool_group_count": len(tool_groups),
            "providers": providers,
            "role_counts": role_counts,
            "workflow_usage": workflow_usage,
            "workflow_names": sorted(name for name in workflow_usage if name and name != "unassigned"),
            "agent_labels": [str(row.get("agent_label") or "") for row in agent_rows],
            "agent_rows_by_label": {
                str(row.get("agent_label") or ""): dict(row) for row in agent_rows if str(row.get("agent_label") or "")
            },
            "agent_rows": agent_rows,
            "env_rows": env_rows,
        }

    def _load_monitoring_snapshot(self) -> dict[str, Any]:
        try:
            if __package__:
                from .control_plane_runtime import load_desktop_monitoring_snapshot  # type: ignore
            else:
                from alde.control_plane_runtime import load_desktop_monitoring_snapshot  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from control_plane_runtime import load_desktop_monitoring_snapshot  # type: ignore
            else:
                raise

        snapshot = load_desktop_monitoring_snapshot(event_limit=40, trace_limit=80)
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot["tree_stream_diagnostic"] = self._load_tree_stream_diagnostic()
        return snapshot

    def _load_tree_stream_diagnostic(self) -> dict[str, Any]:
        diagnostic_payload: dict[str, Any] = {
            "available": False,
            "transport": "n/a",
            "connection_state": "unavailable",
            "reconnect_attempts": 0,
            "backoff_seconds": 0.0,
            "last_event_id": "",
            "last_event_at": "",
            "last_update_at": "",
            "last_error": "",
        }
        try:
            window = self.window()
            explorer = getattr(window, "explorer", None) if window is not None else None
            diagnostic_loader = getattr(explorer, "load_live_sync_diagnostic", None) if explorer is not None else None
            if not callable(diagnostic_loader):
                tree_widget = getattr(explorer, "tree", None) if explorer is not None else None
                diagnostic_loader = getattr(tree_widget, "load_live_sync_diagnostic", None) if tree_widget is not None else None
            if not callable(diagnostic_loader):
                diagnostic_payload["last_error"] = "explorer tree diagnostic unavailable"
                return diagnostic_payload
            loaded_payload = diagnostic_loader()
            if not isinstance(loaded_payload, dict):
                diagnostic_payload["last_error"] = "explorer tree diagnostic unavailable"
                return diagnostic_payload
            diagnostic_payload.update(loaded_payload)
            diagnostic_payload["available"] = True
            return diagnostic_payload
        except Exception as exc:
            diagnostic_payload["last_error"] = f"{type(exc).__name__}: {exc}"
            return diagnostic_payload

    def _load_agent_drilldown_snapshot(self, agent_label: str) -> dict[str, Any]:
        try:
            if __package__:
                from .control_plane_runtime import get_workflow_status_view  # type: ignore
            else:
                from alde.control_plane_runtime import get_workflow_status_view  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from control_plane_runtime import get_workflow_status_view  # type: ignore
            else:
                raise

        detail = get_workflow_status_view(target_agent=agent_label, limit=8)
        detail["agent_label"] = agent_label
        return detail

    def _load_workflow_drilldown_snapshot(self, workflow_name: str) -> dict[str, Any]:
        try:
            if __package__:
                from .control_plane_runtime import get_workflow_status_view  # type: ignore
            else:
                from alde.control_plane_runtime import get_workflow_status_view  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from control_plane_runtime import get_workflow_status_view  # type: ignore
            else:
                raise

        detail = get_workflow_status_view(workflow_name=workflow_name, limit=8)
        detail["workflow_name"] = workflow_name
        return detail

    def _load_operator_snapshot(self) -> dict[str, Any]:
        return self._load_operator_snapshot_with_context(
            previous_operations=dict(self._last_snapshot.get("operations") or {}),
            recent_action_entries=list(self._operator_log_entries),
        )

    def _render_configuration_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._set_metric_value("agents", snapshot.get("agent_count", 0))
        self._set_metric_value("workflows", snapshot.get("workflow_count", 0))

        env_rows_html = "".join(
            f"<li><b>{html.escape(label)}:</b> {self._render_bool_chip(bool(value))}</li>"
            for label, value in snapshot.get("env_rows") or []
        )
        role_rows_html = "".join(
            f"<li><b>{html.escape(role)}:</b> {count}</li>"
            for role, count in sorted((snapshot.get("role_counts") or {}).items())
        )
        provider_text = ", ".join(snapshot.get("providers") or []) or "No provider credentials detected"
        self.config_summary_view.setHtml(
            "".join(
                [
                    "<h3>Configuration Readiness</h3>",
                    "<p>Canonical data source: <code>agents_config.py</code>. This panel projects manifests, workflows, tools, and critical runtime flags into a single operational view.</p>",
                    f"<p><b>Providers:</b> {html.escape(provider_text)}</p>",
                    f"<p><b>Tool catalog:</b> {snapshot.get('tool_count', 0)} tools across {snapshot.get('tool_group_count', 0)} tool groups.</p>",
                    "<h4>Environment Gate</h4>",
                    f"<ul>{env_rows_html}</ul>",
                    "<h4>Role Mix</h4>",
                    f"<ul>{role_rows_html or '<li>No agents materialized</li>'}</ul>",
                ]
            )
        )

        manifest_blocks: list[str] = []
        for row in (snapshot.get("agent_rows") or [])[:10]:
            manifest_blocks.append(
                "".join(
                    [
                        f"<h4>{html.escape(str(row.get('agent_label') or 'unknown'))}</h4>",
                        "<ul>",
                        f"<li><b>Role:</b> {html.escape(str(row.get('role') or 'worker'))}</li>",
                        f"<li><b>Workflow:</b> {html.escape(str(row.get('workflow_name') or 'unassigned'))}</li>",
                        f"<li><b>Model:</b> {html.escape(str(row.get('model') or 'unspecified'))}</li>",
                        f"<li><b>Tools:</b> {int(row.get('tool_count') or 0)}</li>",
                        f"<li><b>Instance policy:</b> {html.escape(str(row.get('instance_policy') or 'ephemeral'))}</li>",
                        "</ul>",
                    ]
                )
            )
        self.config_manifest_view.setHtml(
            "<h3>Manifest Preview</h3>" + "".join(manifest_blocks or ["<p>No manifests available.</p>"])
        )

    def _render_monitoring_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._set_metric_value("sessions", snapshot.get("session_count", 0))
        self._set_metric_value("failures", snapshot.get("failure_count", 0))

        latest_session = snapshot.get("latest_session") or {}
        latest_state = (latest_session.get("latest_workflow_state") or {}) if isinstance(latest_session, dict) else {}
        latest_handoff = (latest_session.get("latest_handoff") or {}) if isinstance(latest_session, dict) else {}
        tree_stream_diagnostic = dict(snapshot.get("tree_stream_diagnostic") or {})
        filtered_trace_entries = self._filtered_trace_entries(snapshot)
        active_trace_filters = [
            selector.currentText().strip()
            for selector in (
                self.trace_agent_selector,
                self.trace_workflow_selector,
                self.trace_tool_selector,
                self.trace_handoff_selector,
            )
            if selector.currentText().strip()
            and selector.currentText().strip() not in {"All agents", "All workflows", "All tools", "All handoffs"}
        ]
        active_filter_text = ", ".join(active_trace_filters) if active_trace_filters else "none"
        alerts_html = "".join(
            f"<li>{html.escape(str(alert))}</li>" for alert in (snapshot.get("alerts") or [])
        )
        self._render_tree_stream_diagnostic(tree_stream_diagnostic)
        self.monitor_summary_view.setHtml(
            "".join(
                [
                    "<h3>Runtime Monitoring</h3>",
                    f"<p><b>Projected sessions:</b> {snapshot.get('session_count', 0)} | <b>events:</b> {snapshot.get('event_count', 0)}</p>",
                    f"<p><b>Detailed trace entries:</b> {snapshot.get('trace_count', 0)} total | <b>visible:</b> {len(filtered_trace_entries)} | <b>filters:</b> {html.escape(active_filter_text)}</p>",
                    f"<p><b>Control-plane health:</b> {html.escape('ready' if bool(snapshot.get('healthy')) else 'attention required')} | <b>Queue:</b> {html.escape(str(snapshot.get('queue_backend') or 'n/a'))} ({'ok' if bool(snapshot.get('queue_healthy')) else 'degraded'}) | <b>Active sessions:</b> {int(snapshot.get('active_session_count') or 0)} | <b>Validation issues:</b> {int(snapshot.get('validation_issue_count') or 0)}</p>",
                    f"<p><b>Explorer tree stream:</b> {html.escape(str(tree_stream_diagnostic.get('transport') or 'n/a'))} | <b>state:</b> {html.escape(str(tree_stream_diagnostic.get('connection_state') or 'unavailable'))} | <b>cursor:</b> {html.escape(str(tree_stream_diagnostic.get('last_event_id') or 'n/a'))}</p>",
                    f"<p><b>Success:</b> {snapshot.get('success_count', 0)} | <b>Failures:</b> {snapshot.get('failure_count', 0)} | <b>Avg latency:</b> {snapshot.get('average_latency_ms', 0.0):.0f} ms</p>",
                    f"<p><b>Latest workflow state:</b> {html.escape(str(latest_state.get('summary') or 'n/a'))}</p>",
                    f"<p><b>Latest handoff:</b> {html.escape(str(latest_handoff.get('summary') or 'n/a'))}</p>",
                    "<p><b>Drill-downs:</b> Use the selectors below to inspect the latest workflow state per agent and per workflow definition. Use Export Runtime for the full JSON trace.</p>",
                    "<h4>Alerts</h4>",
                    f"<ul>{alerts_html or '<li>No active alerts in the current projection.</li>'}</ul>",
                ]
            )
        )

        timeline_rows: list[str] = []
        timeline_rows: list[str] = []
        for event_object in reversed(snapshot.get("events") or []):
            timeline_rows.append(
                "".join(
                    [
                        f"<p><b>{html.escape(str(event_object.get('timestamp') or 'n/a'))}</b><br>",
                        f"{html.escape(str(event_object.get('summary') or event_object.get('event_type') or 'event'))}<br>",
                        f"<span style=\"color:{self.scheme['col8']};\">agent={html.escape(str(event_object.get('agent_label') or 'n/a'))} | workflow={html.escape(str(event_object.get('workflow_name') or 'n/a'))}</span></p>",
                    ]
                )
            )
        self.monitor_timeline_view.setHtml(
            "<h3>Recent Event Timeline</h3>" + "".join(timeline_rows or ["<p>No runtime events available.</p>"])
        )

        trace_rows = [
            self._render_monitor_trace_entry(trace_entry)
            for trace_entry in reversed(filtered_trace_entries)
            if isinstance(trace_entry, dict)
        ]
        self.monitor_trace_view.setHtml(
            "<h3>Trace Detail</h3>"
            "<p>Normalized runtime trace across chat messages, tool calls, tool results, handoffs, and workflow payloads.</p>"
            + "".join(trace_rows or ["<p>No trace entries match the active filters.</p>"])
        )
        self._render_threat_flow_snapshot(snapshot, filtered_trace_entries)

    def _render_threat_flow_snapshot(
        self,
        snapshot: dict[str, Any],
        filtered_trace_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        flow_view = getattr(self, "config_monitor_threat_flow_view", None)
        if not isinstance(flow_view, QTextBrowser):
            return

        trace_entries = filtered_trace_entries if filtered_trace_entries is not None else self._filtered_trace_entries(snapshot)
        events = [event for event in (snapshot.get("events") or []) if isinstance(event, dict)]
        recent_events = list(reversed(events[-8:]))

        event_rows = [
            "".join(
                [
                    f"<li><b>{html.escape(str(event.get('timestamp') or 'n/a'))}</b> - ",
                    f"{html.escape(str(event.get('summary') or event.get('event_type') or 'event'))}<br>",
                    f"<span style=\"color:{self.scheme['col8']};\">",
                    f"agent={html.escape(str(event.get('agent_label') or 'n/a'))} | ",
                    f"workflow={html.escape(str(event.get('workflow_name') or 'n/a'))}",
                    "</span></li>",
                ]
            )
            for event in recent_events
        ]

        handoff_rows: list[str] = []
        for trace_entry in reversed(trace_entries):
            if not isinstance(trace_entry, dict):
                continue
            handoff_value = self._trace_entry_handoff_value(trace_entry)
            if not handoff_value:
                continue
            handoff_rows.append(
                "".join(
                    [
                        f"<li><b>{html.escape(str(trace_entry.get('timestamp') or 'n/a'))}</b> - ",
                        f"{html.escape(self._trace_entry_agent_label(trace_entry))} -> {html.escape(handoff_value)}<br>",
                        f"<span style=\"color:{self.scheme['col8']};\">workflow={html.escape(self._trace_entry_workflow_name(trace_entry))}</span>",
                        "</li>",
                    ]
                )
            )
            if len(handoff_rows) >= 6:
                break

        alerts_html = "".join(
            f"<li>{html.escape(str(alert))}</li>"
            for alert in (snapshot.get("alerts") or [])[:6]
        )
        health_chip = self._render_status_chip(
            "healthy" if bool(snapshot.get("healthy")) else "attention",
            SIGNAL_GREEN if bool(snapshot.get("healthy")) else SIGNAL_RED,
        )

        flow_view.setHtml(
            "".join(
                [
                    f"<p><b>Health:</b> {health_chip} | ",
                    f"<b>Visible trace entries:</b> {len(trace_entries)} | ",
                    f"<b>Events:</b> {len(events)}</p>",
                    "<h4>Recent Runtime Events</h4>",
                    f"<ul>{''.join(event_rows) or '<li>No runtime events available.</li>'}</ul>",
                    "<h4>Recent Handoffs</h4>",
                    f"<ul>{''.join(handoff_rows) or '<li>No handoff transitions in visible trace.</li>'}</ul>",
                    "<h4>Attention</h4>",
                    f"<ul>{alerts_html or '<li>No active alerts in this projection.</li>'}</ul>",
                ]
            )
        )

    def _render_tree_stream_diagnostic(self, diagnostic: dict[str, Any]) -> None:
        transport = str(diagnostic.get("transport") or "n/a").strip() or "n/a"
        state = str(diagnostic.get("connection_state") or "unavailable").strip() or "unavailable"
        last_event_id = str(diagnostic.get("last_event_id") or "").strip()
        last_update_at = str(diagnostic.get("last_update_at") or diagnostic.get("last_event_at") or "").strip()
        last_error = str(diagnostic.get("last_error") or "").strip()
        reconnect_attempts = int(diagnostic.get("reconnect_attempts") or 0)
        backoff_seconds = float(diagnostic.get("backoff_seconds") or 0.0)
        push_enabled = bool(diagnostic.get("push_enabled"))
        push_supported = bool(diagnostic.get("push_supported"))

        if state in {"connected", "polling"}:
            state_chip = self._render_status_chip(state, SIGNAL_GREEN)
        elif state in {"connecting", "reconnecting", "backoff"}:
            state_chip = self._render_status_chip(state, SIGNAL_YELLOW)
        elif state in {"disabled", "stopped"}:
            state_chip = self._render_status_chip(state, self.scheme["col8"])
        else:
            state_chip = self._render_status_chip(state, SIGNAL_RED)

        transport_note = transport
        if transport == "poll" and push_enabled:
            transport_note = f"{transport} (fallback)"
        elif transport == "push" and not push_supported:
            transport_note = f"{transport} (degraded)"
        elif transport == "n/a" and push_enabled:
            transport_note = "n/a (push requested)"

        event_text = last_event_id or "n/a"
        if len(event_text) > 24:
            event_text = f"{event_text[:12]}...{event_text[-8:]}"
        retry_text = f"{reconnect_attempts} / {backoff_seconds:.1f}s"

        self.tree_stream_transport_value.setText(html.escape(transport_note))
        self.tree_stream_state_value.setText(state_chip)
        self.tree_stream_event_value.setText(html.escape(event_text))
        self.tree_stream_retry_value.setText(html.escape(retry_text))
        self.tree_stream_updated_value.setText(html.escape(last_update_at or "n/a"))
        self.tree_stream_error_value.setText(html.escape(last_error or "none"))

    def _render_operator_snapshot(self, snapshot: dict[str, Any]) -> None:
        service_rows = [row for row in (snapshot.get("service_rows") or []) if isinstance(row, dict)]
        audit_summary = dict(snapshot.get("audit_summary") or snapshot.get("recent_item_summary") or {})
        status_counts = dict(audit_summary.get("status_counts") or {})
        audit_type_counts = dict(audit_summary.get("audit_type_counts") or {})
        action_group_counts = dict(audit_summary.get("action_group_counts") or {})
        source_counts = dict(audit_summary.get("source_counts") or {})
        agentsdb_healthy = snapshot.get("agentsdb_healthy")
        if agentsdb_healthy is True:
            agentsdb_state = "ok"
        elif agentsdb_healthy is False:
            agentsdb_state = "degraded"
        else:
            agentsdb_state = "not-run"
        agentsdb_detail = str(snapshot.get("agentsdb_detail") or snapshot.get("agentsdb_uri") or "n/a")
        validation_error_items = [str(item) for item in (snapshot.get("validation_errors") or []) if str(item)]
        validation_errors = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in validation_error_items
        )
        status_rows_html: list[str] = []
        for row in service_rows:
            state = str(row.get("state") or "unknown").strip().lower()
            if state == "pass":
                chip_html = self._render_status_chip("pass", SIGNAL_GREEN)
            elif state == "not-run":
                chip_html = self._render_status_chip("not-run", SIGNAL_YELLOW)
            elif state == "fail":
                chip_html = self._render_status_chip("fail", SIGNAL_RED)
            else:
                chip_html = self._render_status_chip(state or "unknown", self.scheme["col8"])
            status_rows_html.append(
                self._render_operator_status_row(
                    str(row.get("title") or "service"),
                    chip_html,
                    str(row.get("detail") or "n/a"),
                    str(row.get("note") or ""),
                )
            )

        attention_html = "".join(
            f"<li>{html.escape(str(item))}</li>" for item in (snapshot.get("alerts") or [])[:6]
        )
        recent_actions = [item for item in (snapshot.get("recent_actions") or []) if isinstance(item, dict)]
        latest_action = recent_actions[0] if recent_actions else {}
        audit_types_text = ", ".join(f"{key}={value}" for key, value in list(audit_type_counts.items())[:4])
        action_groups_text = ", ".join(f"{key}={value}" for key, value in list(action_group_counts.items())[:4])
        sources_text = ", ".join(f"{key}={value}" for key, value in list(source_counts.items())[:3])
        self.operator_summary_view.setHtml(
            "".join(
                [
                    "<h3>Operator Status</h3>",
                    "<p>Focused view of queue health, AgentsDB readiness, dispatcher readiness, MCP availability, and workflow validation.</p>",
                    f"<p><b>Control-plane health:</b> {html.escape('ready' if bool(snapshot.get('healthy')) else 'attention required')} | <b>Healthy checks:</b> {int(snapshot.get('healthy_service_count') or 0)}/{int(snapshot.get('service_count') or 0)} | <b>Queue:</b> {html.escape(str(snapshot.get('queue_backend') or 'n/a'))} ({'ok' if bool(snapshot.get('queue_healthy')) else 'degraded'}) | <b>AgentsDB:</b> {html.escape(agentsdb_detail)} ({html.escape(agentsdb_state)}) | <b>Validation issues:</b> {int(snapshot.get('validation_issue_count') or 0)} | <b>Alerts:</b> {int(snapshot.get('attention_count') or 0)}</p>",
                    f"<p><b>Recent actions:</b> {int(snapshot.get('recent_item_count') or 0)} | <b>Pass:</b> {int(status_counts.get('pass') or 0)} | <b>Fail:</b> {int(status_counts.get('fail') or 0)} | <b>Latest:</b> {html.escape(str(latest_action.get('summary') or 'n/a'))}</p>",
                    f"<p><b>Audit types:</b> {html.escape(audit_types_text or 'n/a')} | <b>Groups:</b> {html.escape(action_groups_text or 'n/a')} | <b>Sources:</b> {html.escape(sources_text or 'n/a')}</p>",
                    "<h4>Service Status</h4>",
                    f"<ul>{''.join(status_rows_html) or '<li>No operator checks projected.</li>'}</ul>",
                    "<h4>Attention</h4>",
                    f"<ul>{attention_html or '<li>No immediate operator action required.</li>'}</ul>",
                    "<h4>Validation Errors</h4>",
                    f"<ul>{validation_errors or '<li>No active workflow validation errors.</li>'}</ul>",
                ]
            )
        )

    def _populate_drilldown_selectors(self, configuration_snapshot: dict[str, Any]) -> None:
        agent_labels = [label for label in (configuration_snapshot.get("agent_labels") or []) if label]
        workflow_names = [name for name in (configuration_snapshot.get("workflow_names") or []) if name]
        self._agent_rows_by_label = dict(configuration_snapshot.get("agent_rows_by_label") or {})

        current_agent = self.agent_selector.currentText().strip()
        current_workflow = self.workflow_selector.currentText().strip()

        agent_blocker = QtCore.QSignalBlocker(self.agent_selector)
        workflow_blocker = QtCore.QSignalBlocker(self.workflow_selector)
        self.agent_selector.clear()
        self.workflow_selector.clear()
        self.agent_selector.addItems(agent_labels)
        self.workflow_selector.addItems(workflow_names)

        if current_agent and current_agent in agent_labels:
            self.agent_selector.setCurrentText(current_agent)
        elif agent_labels:
            self.agent_selector.setCurrentIndex(0)

        if current_workflow and current_workflow in workflow_names:
            self.workflow_selector.setCurrentText(current_workflow)
        elif workflow_names:
            self.workflow_selector.setCurrentIndex(0)

        del agent_blocker
        del workflow_blocker

    def _populate_trace_filter_selectors(self, monitoring_snapshot: dict[str, Any]) -> None:
        filter_options = dict(monitoring_snapshot.get("trace_filter_options") or {})
        current_agent = self.trace_agent_selector.currentText().strip()
        current_workflow = self.trace_workflow_selector.currentText().strip()
        current_tool = self.trace_tool_selector.currentText().strip()
        current_handoff = self.trace_handoff_selector.currentText().strip()

        trace_agent_options = ["All agents"] + [str(item) for item in filter_options.get("agents") or [] if str(item)]
        trace_workflow_options = ["All workflows"] + [str(item) for item in filter_options.get("workflows") or [] if str(item)]
        trace_tool_options = ["All tools"] + [str(item) for item in filter_options.get("tools") or [] if str(item)]
        trace_handoff_options = ["All handoffs", "Handoff only"] + [str(item) for item in filter_options.get("handoffs") or [] if str(item)]

        agent_blocker = QtCore.QSignalBlocker(self.trace_agent_selector)
        workflow_blocker = QtCore.QSignalBlocker(self.trace_workflow_selector)
        tool_blocker = QtCore.QSignalBlocker(self.trace_tool_selector)
        handoff_blocker = QtCore.QSignalBlocker(self.trace_handoff_selector)

        self.trace_agent_selector.clear()
        self.trace_workflow_selector.clear()
        self.trace_tool_selector.clear()
        self.trace_handoff_selector.clear()

        self.trace_agent_selector.addItems(trace_agent_options)
        self.trace_workflow_selector.addItems(trace_workflow_options)
        self.trace_tool_selector.addItems(trace_tool_options)
        self.trace_handoff_selector.addItems(trace_handoff_options)

        self.trace_agent_selector.setCurrentText(current_agent if current_agent in trace_agent_options else "All agents")
        self.trace_workflow_selector.setCurrentText(current_workflow if current_workflow in trace_workflow_options else "All workflows")
        self.trace_tool_selector.setCurrentText(current_tool if current_tool in trace_tool_options else "All tools")
        self.trace_handoff_selector.setCurrentText(current_handoff if current_handoff in trace_handoff_options else "All handoffs")

        del agent_blocker
        del workflow_blocker
        del tool_blocker
        del handoff_blocker

    def _populate_operator_filter_selectors(self, operator_snapshot: dict[str, Any]) -> None:
        filter_options = dict(operator_snapshot.get("recent_action_filters") or operator_snapshot.get("recent_item_filters") or {})
        current_status = self.operator_status_selector.currentText().strip() or str(self._operator_filter_preferences.get("status") or "")
        current_audit = self.operator_audit_selector.currentText().strip() or str(self._operator_filter_preferences.get("audit_type") or "")
        current_group = self.operator_group_selector.currentText().strip() or str(self._operator_filter_preferences.get("action_group") or "")
        current_source = self.operator_source_selector.currentText().strip() or str(self._operator_filter_preferences.get("source") or "")

        status_options = ["All statuses"] + [str(item) for item in filter_options.get("statuses") or [] if str(item)]
        audit_options = ["All action types"] + [str(item) for item in filter_options.get("audit_types") or [] if str(item)]
        group_options = ["All action groups"] + [str(item) for item in filter_options.get("action_groups") or [] if str(item)]
        source_options = ["All sources"] + [str(item) for item in filter_options.get("sources") or [] if str(item)]

        status_blocker = QtCore.QSignalBlocker(self.operator_status_selector)
        audit_blocker = QtCore.QSignalBlocker(self.operator_audit_selector)
        group_blocker = QtCore.QSignalBlocker(self.operator_group_selector)
        source_blocker = QtCore.QSignalBlocker(self.operator_source_selector)

        self.operator_status_selector.clear()
        self.operator_audit_selector.clear()
        self.operator_group_selector.clear()
        self.operator_source_selector.clear()

        self.operator_status_selector.addItems(status_options)
        self.operator_audit_selector.addItems(audit_options)
        self.operator_group_selector.addItems(group_options)
        self.operator_source_selector.addItems(source_options)

        self.operator_status_selector.setCurrentText(current_status if current_status in status_options else "All statuses")
        self.operator_audit_selector.setCurrentText(current_audit if current_audit in audit_options else "All action types")
        self.operator_group_selector.setCurrentText(current_group if current_group in group_options else "All action groups")
        self.operator_source_selector.setCurrentText(current_source if current_source in source_options else "All sources")
        self._operator_filter_preferences = self._current_operator_filter_preferences()
        self._save_operator_filter_preferences()

        del status_blocker
        del audit_blocker
        del group_blocker
        del source_blocker

    def _filtered_operator_actions(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        selected_status = self.operator_status_selector.currentText().strip()
        selected_audit = self.operator_audit_selector.currentText().strip()
        selected_group = self.operator_group_selector.currentText().strip()
        selected_source = self.operator_source_selector.currentText().strip()

        filtered_entries: list[dict[str, Any]] = []
        for action_entry in snapshot.get("recent_actions") or []:
            if not isinstance(action_entry, dict):
                continue
            action_status = str(action_entry.get("status") or "").strip()
            audit_type = str(action_entry.get("audit_type") or "").strip()
            action_group = str(action_entry.get("action_group") or "").strip()
            source = str(action_entry.get("source") or "").strip()

            if selected_status and selected_status != "All statuses" and action_status != selected_status:
                continue
            if selected_audit and selected_audit != "All action types" and audit_type != selected_audit:
                continue
            if selected_group and selected_group != "All action groups" and action_group != selected_group:
                continue
            if selected_source and selected_source != "All sources" and source != selected_source:
                continue
            filtered_entries.append(action_entry)
        return filtered_entries

    def _refresh_drilldown_views(self) -> None:
        board_context = self._board_context_from_object(self.sender()) or self._active_board_context()
        with self._board_context_scope(board_context):
            self._refresh_drilldown_views_for_context()

    def _refresh_drilldown_views_for_context(self) -> None:
        agent_label = self.agent_selector.currentText().strip()
        workflow_name = self.workflow_selector.currentText().strip()

        agent_row = dict(self._agent_rows_by_label.get(agent_label) or {})
        mapped_workflow = str(agent_row.get("workflow_name") or "").strip()
        if mapped_workflow and mapped_workflow != "unassigned" and mapped_workflow != workflow_name:
            blocker = QtCore.QSignalBlocker(self.workflow_selector)
            self.workflow_selector.setCurrentText(mapped_workflow)
            del blocker
            workflow_name = self.workflow_selector.currentText().strip() or mapped_workflow

        agent_snapshot: dict[str, Any] | None = None
        workflow_snapshot: dict[str, Any] | None = None

        try:
            if agent_label:
                agent_snapshot = self._load_agent_drilldown_snapshot(agent_label)
                agent_snapshot["manifest"] = agent_row
            if workflow_name:
                workflow_snapshot = self._load_workflow_drilldown_snapshot(workflow_name)
            self._render_drilldown_snapshot(agent_snapshot, workflow_snapshot)
        except Exception as exc:
            error_text = html.escape(f"{type(exc).__name__}: {exc}")
            self.monitor_detail_view.setHtml(f"<h3>Drill-down unavailable</h3><p>{error_text}</p>")

    def _render_drilldown_snapshot(
        self,
        agent_snapshot: dict[str, Any] | None,
        workflow_snapshot: dict[str, Any] | None,
    ) -> None:
        agent_section = self._render_drilldown_section(
            title=f"Agent Focus: {str((agent_snapshot or {}).get('agent_label') or 'n/a')}",
            latest=(agent_snapshot or {}).get("latest"),
            items=(agent_snapshot or {}).get("items") or [],
            validation=(agent_snapshot or {}).get("validation") or {},
            error=(agent_snapshot or {}).get("error"),
            manifest=(agent_snapshot or {}).get("manifest"),
            empty_message="No workflow history for the selected agent.",
        )
        workflow_section = self._render_drilldown_section(
            title=f"Workflow Focus: {str((workflow_snapshot or {}).get('workflow_name') or 'n/a')}",
            latest=(workflow_snapshot or {}).get("latest"),
            items=(workflow_snapshot or {}).get("items") or [],
            validation=(workflow_snapshot or {}).get("validation") or {},
            error=(workflow_snapshot or {}).get("error"),
            manifest=None,
            empty_message="No workflow history for the selected workflow.",
        )
        self.monitor_detail_view.setHtml("".join([agent_section, workflow_section]))

    def _render_drilldown_section(
        self,
        *,
        title: str,
        latest: dict[str, Any] | None,
        items: list[dict[str, Any]],
        validation: dict[str, Any],
        error: Any,
        manifest: dict[str, Any] | None,
        empty_message: str,
    ) -> str:
        latest_view = self._summarize_workflow_entry(latest)
        activity = self._derive_activity_signal(latest_view)
        recovery_actions = self._derive_recovery_actions(latest_view, items, manifest, activity)
        manifest_html = ""
        if manifest:
            manifest_html = "".join(
                [
                    "<h4>Assigned Manifest</h4>",
                    "<ul>",
                    f"<li><b>Role:</b> {html.escape(str(manifest.get('role') or 'worker'))}</li>",
                    f"<li><b>Workflow:</b> {html.escape(str(manifest.get('workflow_name') or 'unassigned'))}</li>",
                    f"<li><b>Model:</b> {html.escape(str(manifest.get('model') or 'unspecified'))}</li>",
                    f"<li><b>Tools:</b> {int(manifest.get('tool_count') or 0)}</li>",
                    f"<li><b>Instance policy:</b> {html.escape(str(manifest.get('instance_policy') or 'ephemeral'))}</li>",
                    "</ul>",
                ]
            )
        latest_health_html = self._render_health_signal(latest_view, items)
        activity_html = self._render_activity_signal(activity)
        recovery_html = "".join(
            f"<li>{html.escape(str(item))}</li>" for item in recovery_actions
        )
        history_rows = "".join(
            "".join(
                [
                    f"<li><b>{html.escape(str(entry_view.get('title') or 'workflow event'))}</b> ",
                    f"{html.escape(str(entry_view.get('summary') or 'n/a'))}<br>",
                    f"<span style=\"color:{self.scheme['col8']};\">",
                    f"state={html.escape(str(entry_view.get('state') or 'n/a'))} | ",
                    f"actor={html.escape(str(entry_view.get('actor') or 'n/a'))} | ",
                    f"time={html.escape(str(entry_view.get('timestamp') or 'n/a'))}",
                    "</span></li>",
                ]
            )
            for entry_view in [self._summarize_workflow_entry(item) for item in items]
        )
        validation_errors = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in (validation.get("errors") or [])[:5]
        )
        error_html = f"<p>{html.escape(str(error))}</p>" if error else ""
        latest_html = "".join(
            [
                f"<p><b>Latest:</b> {html.escape(str(latest_view.get('title') or 'n/a'))}<br>",
                f"{html.escape(str(latest_view.get('summary') or 'n/a'))}<br>",
                f"<span style=\"color:{self.scheme['col8']};\">state={html.escape(str(latest_view.get('state') or 'n/a'))} | workflow={html.escape(str(latest_view.get('workflow_name') or 'n/a'))} | actor={html.escape(str(latest_view.get('actor') or 'n/a'))}</span></p>",
            ]
        ) if latest else f"<p>{html.escape(empty_message)}</p>"
        return "".join(
            [
                f"<h3>{html.escape(title)}</h3>",
                error_html,
                manifest_html,
                latest_html,
                latest_health_html,
                activity_html,
                "<h4>Recent History</h4>",
                f"<ul>{history_rows or f'<li>{html.escape(empty_message)}</li>'}</ul>",
                "<h4>Recovery</h4>",
                f"<ul>{recovery_html or '<li>No immediate operator action suggested.</li>'}</ul>",
                "<h4>Validation</h4>",
                f"<p>{self._render_bool_chip(bool(validation.get('valid', True)))}</p>",
                f"<ul>{validation_errors or '<li>No validation errors reported.</li>'}</ul>",
            ]
        )

    def _summarize_workflow_entry(self, entry: dict[str, Any] | None) -> dict[str, str]:
        if not isinstance(entry, dict):
            return {}

        workflow = entry.get("workflow") if isinstance(entry.get("workflow"), dict) else {}
        snapshot_view = workflow.get("snapshot_view") if isinstance(workflow.get("snapshot_view"), dict) else {}
        snapshot = workflow.get("snapshot") if isinstance(workflow.get("snapshot"), dict) else {}
        actor = snapshot.get("actor") if isinstance(snapshot.get("actor"), dict) else {}
        event = snapshot.get("event") if isinstance(snapshot.get("event"), dict) else {}

        return {
            "title": str(snapshot_view.get("title") or workflow.get("current_state") or entry.get("event_name") or "workflow event"),
            "summary": str(snapshot_view.get("summary") or event.get("name") or workflow.get("workflow_name") or "n/a"),
            "state": str(snapshot_view.get("state") or workflow.get("current_state") or entry.get("state") or "n/a"),
            "workflow_name": str(snapshot_view.get("workflow_name") or workflow.get("workflow_name") or entry.get("workflow_name") or "n/a"),
            "actor": str(snapshot_view.get("actor_name") or actor.get("name") or entry.get("agent_label") or "n/a"),
            "timestamp": str(entry.get("timestamp") or workflow.get("updated_at") or snapshot.get("timestamp") or "n/a"),
            "retry_attempts": str((workflow.get("retry") or {}).get("attempt_count") or 0),
            "retry_remaining": str((workflow.get("retry") or {}).get("remaining_attempts") or 0),
            "retry_exhausted": str(bool((workflow.get("retry") or {}).get("exhausted"))),
        }

    def _derive_activity_signal(self, latest_view: dict[str, str]) -> dict[str, Any]:
        timestamp = self._parse_timestamp(latest_view.get("timestamp"))
        if timestamp is None:
            return {
                "last_seen": "unknown",
                "age_minutes": None,
                "escalation": "unknown",
                "chip_color": SIGNAL_YELLOW,
                "detail": "No reliable timestamp is available for this workflow focus.",
            }

        age_seconds = max((datetime.now(timezone.utc) - timestamp).total_seconds(), 0.0)
        age_minutes = int(age_seconds // 60)
        if age_minutes >= 60:
            escalation = "critical"
            chip_color = SIGNAL_RED
        elif age_minutes >= 15:
            escalation = "elevated"
            chip_color = SIGNAL_YELLOW
        elif age_minutes >= 5:
            escalation = "watch"
            chip_color = SIGNAL_YELLOW
        else:
            escalation = "fresh"
            chip_color = SIGNAL_GREEN

        return {
            "last_seen": timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
            "age_minutes": age_minutes,
            "escalation": escalation,
            "chip_color": chip_color,
            "detail": f"Last workflow activity was {self._format_elapsed(age_seconds)} ago.",
        }

    def _render_activity_signal(self, activity: dict[str, Any]) -> str:
        age_minutes = activity.get("age_minutes")
        age_label = f"{age_minutes} min" if isinstance(age_minutes, int) else "n/a"
        return "".join(
            [
                "<h4>Activity</h4>",
                f"<p><b>Last seen:</b> {html.escape(str(activity.get('last_seen') or 'unknown'))}</p>",
                f"<p><b>Inactivity:</b> {html.escape(age_label)} | <b>Escalation:</b> {self._render_status_chip(str(activity.get('escalation') or 'unknown'), str(activity.get('chip_color') or SIGNAL_YELLOW))}</p>",
                f"<p>{html.escape(str(activity.get('detail') or ''))}</p>",
            ]
        )

    def _derive_recovery_actions(
        self,
        latest_view: dict[str, str],
        items: list[dict[str, Any]],
        manifest: dict[str, Any] | None,
        activity: dict[str, Any],
    ) -> list[str]:
        actions: list[str] = []
        state_text = str(latest_view.get("state") or "").lower()
        summary_text = str(latest_view.get("summary") or "").lower()
        workflow_name = str((manifest or {}).get("workflow_name") or latest_view.get("workflow_name") or "workflow")
        retry_exhausted = str(latest_view.get("retry_exhausted") or "False").lower() == "true"
        retry_remaining = int(str(latest_view.get("retry_remaining") or 0) or 0)
        age_minutes = activity.get("age_minutes")

        if retry_exhausted:
            actions.append(f"Retry budget for {workflow_name} is exhausted. Re-run the originating request or raise the retry policy ceiling before restarting.")
        elif "retry" in state_text and retry_remaining > 0:
            actions.append(f"Workflow is in a retry state with {retry_remaining} attempts left. Inspect the last tool failure before forcing another run.")

        if any(token in state_text for token in ("failed", "error", "blocked")) or any(token in summary_text for token in ("failed", "error", "blocked")):
            actions.append("Latest state indicates failure or blockage. Probe queue and dispatcher first, then trigger a fresh workflow run from the originating agent.")

        if isinstance(age_minutes, int) and age_minutes >= 15:
            actions.append("Workflow focus is stale. Compare the last activity timestamp with current queue health and confirm whether the session is abandoned.")

        if manifest and str(manifest.get("workflow_name") or "") == "unassigned":
            actions.append("Selected agent is not bound to a workflow definition. Assign a workflow before expecting runtime transitions.")

        if not items:
            actions.append("No history entries are projected for this focus. Start or replay a workflow run to establish runtime evidence.")

        return actions[:4]

    def _parse_timestamp(self, value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw or raw == "n/a":
            return None
        try:
            if raw.endswith("Z"):
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _format_elapsed(self, age_seconds: float) -> str:
        if age_seconds < 60:
            return f"{int(age_seconds)}s"
        if age_seconds < 3600:
            return f"{int(age_seconds // 60)}m"
        hours = int(age_seconds // 3600)
        minutes = int((age_seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

    def _render_health_signal(self, latest_view: dict[str, str], items: list[dict[str, Any]]) -> str:
        state_text = str(latest_view.get("state") or "").lower()
        summary_text = str(latest_view.get("summary") or "")
        if not latest_view:
            return f"<p><b>Health:</b> {self._render_status_chip('cold', SIGNAL_YELLOW)}</p>"

        if any(token in state_text for token in ("fail", "error", "blocked")):
            return f"<p><b>Health:</b> {self._render_status_chip('attention', SIGNAL_RED)} {html.escape(summary_text)}</p>"

        if len(items) <= 1:
            return f"<p><b>Health:</b> {self._render_status_chip('warming', SIGNAL_YELLOW)} recent history is still sparse</p>"

        return f"<p><b>Health:</b> {self._render_status_chip('stable', SIGNAL_GREEN)} workflow transitions are present</p>"

    def _run_operator_health_checks(self) -> None:
        previous_operations = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            message = "Health checks refreshed."
            projected_entries = list(recent_action_entries)
            projected_entries.append(self._build_operator_log_entry(message))
            snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=projected_entries[-12:],
            )
            return {
                "message": message,
                "snapshot": snapshot,
            }

        self._run_operator_background_task(kind="health_checks", worker=_worker)

    def _probe_queue_health(self) -> None:
        previous_operations = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            initial_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=recent_action_entries,
            )
            message = (
                f"Queue probe: backend={str(initial_snapshot.get('queue_backend') or 'n/a')} "
                f"healthy={bool(initial_snapshot.get('queue_healthy'))}"
            )
            projected_entries = list(recent_action_entries)
            projected_entries.append(self._build_operator_log_entry(message))
            final_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=projected_entries[-12:],
            )
            return {
                "message": message,
                "snapshot": final_snapshot,
             }

        self._run_operator_background_task(kind="queue_probe", worker=_worker)

    def _probe_agentsdb_health(self) -> None:
        previous_operations = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            initial_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=recent_action_entries,
            )
            agentsdb_healthy = initial_snapshot.get("agentsdb_healthy")
            if agentsdb_healthy is True:
                message = (
                    f"AgentsDB probe passed: "
                    f"{str(initial_snapshot.get('agentsdb_detail') or initial_snapshot.get('agentsdb_uri') or 'agentsdb')[:220]}"
                )
            elif agentsdb_healthy is False:
                message = f"AgentsDB probe failed: {str(initial_snapshot.get('agentsdb_error') or 'unknown error')[:220]}"
            else:
                message = (
                    f"AgentsDB probe not-run: "
                    f"{str(initial_snapshot.get('agentsdb_detail') or 'no agentsdb backend configured')[:220]}"
                )
            projected_entries = list(recent_action_entries)
            projected_entries.append(self._build_operator_log_entry(message))
            final_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=projected_entries[-12:],
            )
            return {
                "message": message,
                "snapshot": final_snapshot,
            }

        self._run_operator_background_task(kind="agentsdb_probe", worker=_worker)

    def _probe_dispatcher_health(self) -> None:
        previous_operations = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            initial_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=recent_action_entries,
            )
            if bool(initial_snapshot.get("dispatcher_healthy")):
                message = f"Dispatcher probe passed: {str(initial_snapshot.get('dispatcher_db_path') or 'n/a')}"
            else:
                message = f"Dispatcher probe failed: {str(initial_snapshot.get('dispatcher_error') or 'unknown error')}"
            projected_entries = list(recent_action_entries)
            projected_entries.append(self._build_operator_log_entry(message))
            final_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=projected_entries[-12:],
            )
            return {
                "message": message,
                "snapshot": final_snapshot,
            }

        self._run_operator_background_task(kind="dispatcher_probe", worker=_worker)

    def _repair_dispatcher_store(self) -> None:
        previous_operations = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            result = self._repair_dispatcher_store_path()
            backup_text = f" backup={result.get('backup_path')}" if result.get("backup_path") else ""
            message = f"Dispatcher repair completed:{backup_text}"
            projected_entries = list(recent_action_entries)
            projected_entries.append(self._build_operator_log_entry(message))
            final_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=previous_operations,
                recent_action_entries=projected_entries[-12:],
            )
            return {
                "message": message,
                "snapshot": final_snapshot,
            }

        self._run_operator_background_task(kind="dispatcher_repair", worker=_worker)

    def _repair_dispatcher_store_path(self, dispatcher_db_path: str | None = None) -> dict[str, Any]:
        try:
            if __package__:
                from .agents_tools import DOCUMENT_DISPATCH_SERVICE, DOCUMENT_REPOSITORY, _default_dispatcher_db_path  # type: ignore
            else:
                from ALDE_Projekt.ALDE.alde.agents_tools import DOCUMENT_DISPATCH_SERVICE, DOCUMENT_REPOSITORY, _default_dispatcher_db_path  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from ALDE_Projekt.ALDE.alde.agents_tools import DOCUMENT_DISPATCH_SERVICE, DOCUMENT_REPOSITORY, _default_dispatcher_db_path  # type: ignore
            else:
                raise

        resolved_path = str(dispatcher_db_path or _default_dispatcher_db_path())
        backup_path: str | None = None
        if os.path.isfile(resolved_path):
            backup_path = f"{resolved_path}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
            shutil.copy2(resolved_path, backup_path)

        db = DOCUMENT_REPOSITORY.load_db(resolved_path, db_name="dispatcher_documents")
        if not isinstance(db, dict):
            db = {"schema": "dispatcher_doc_db_v1", "documents": {}}
        if not isinstance(db.get("documents"), dict):
            db["documents"] = {}
        if not str(db.get("schema") or "").strip():
            db["schema"] = "dispatcher_doc_db_v1"
        DOCUMENT_REPOSITORY.save_db(resolved_path, db, db_name="dispatcher_documents")

        dispatcher_error = DOCUMENT_DISPATCH_SERVICE.check_dispatcher_access(
            resolved_db_path=resolved_path
        )
        return {
            "dispatcher_db_path": resolved_path,
            "dispatcher_healthy": dispatcher_error is None,
            "dispatcher_error": dispatcher_error,
            "backup_path": backup_path,
        }

    def _probe_mcp_health(self) -> None:
        previous_operations = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            probe = self._run_mcp_health_probe()
            if probe.get("ok"):
                active_transport = str(probe.get("active_transport") or "").strip() or "unknown"
                active_server = str(probe.get("active_server") or "").strip() or "unknown"
                fallback_note = " (fallback)" if bool(probe.get("fallback_used")) else ""
                message = f"MCP probe passed via {active_transport} @ {active_server}{fallback_note}."
            else:
                message = f"MCP probe failed: {str(probe.get('stderr') or probe.get('stdout') or 'unknown error')[:180]}"

            projected_operations = dict(previous_operations)
            projected_operations["mcp_probe"] = probe
            projected_entries = list(recent_action_entries)
            projected_entries.append(self._build_operator_log_entry(message))
            final_snapshot = self._load_operator_snapshot_with_context(
                previous_operations=projected_operations,
                recent_action_entries=projected_entries[-12:],
            )
            return {
                "message": message,
                "snapshot": final_snapshot,
            }

        self._run_operator_background_task(kind="mcp_probe", worker=_worker)

    def _run_mcp_health_probe(self) -> dict[str, Any]:
        probe_path = Path(__file__).with_name("mcp_health.py")
        if not probe_path.is_file():
            return {
                "ok": False,
                "returncode": None,
                "stdout": "",
                "stderr": f"{probe_path.name} not found",
            }

        completed = subprocess.run(
            [sys.executable, str(probe_path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(probe_path.parent),
        )
        stdout_text = (completed.stdout or "").strip()
        stderr_text = (completed.stderr or "").strip()

        result = {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

        probe_payload: dict[str, Any] | None = None
        for output_line in reversed(stdout_text.splitlines()):
            normalized_line = str(output_line or "").strip()
            if not normalized_line.startswith("MCP_PROBE_JSON="):
                continue
            try:
                payload_text = normalized_line.split("=", 1)[1]
                loaded_payload = json.loads(payload_text)
                if isinstance(loaded_payload, dict):
                    probe_payload = loaded_payload
            except Exception:
                probe_payload = None
            break

        if isinstance(probe_payload, dict):
            result["ok"] = bool(probe_payload.get("ok"))
            result["active_server"] = str(probe_payload.get("active_server") or "").strip() or None
            result["active_transport"] = str(probe_payload.get("active_transport") or "").strip() or None
            result["selected_server"] = str(probe_payload.get("selected_server") or "").strip() or None
            result["selected_transport"] = str(probe_payload.get("selected_transport") or "").strip() or None
            result["fallback_used"] = bool(probe_payload.get("fallback_used"))
            result["attempt_count"] = int(probe_payload.get("attempt_count") or 0)
            result["tool_count"] = int(probe_payload.get("tool_count") or 0)
            result["attempts"] = list(probe_payload.get("attempts") or [])
            result["probe_metrics"] = dict(probe_payload.get("probe_metrics") or {})
            if not result.get("stderr"):
                result["stderr"] = str(probe_payload.get("error") or "").strip()

        return result

    def _export_runtime_snapshot_report_path(
        self,
        *,
        operations_snapshot: dict[str, Any],
        recent_action_entries: list[Any],
    ) -> str:
        try:
            if __package__:
                from .control_plane_runtime import export_control_plane_snapshot  # type: ignore
            else:
                from alde.control_plane_runtime import export_control_plane_snapshot  # type: ignore
        except ImportError as exc:
            msg = str(exc)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from control_plane_runtime import export_control_plane_snapshot  # type: ignore
            else:
                raise

        return export_control_plane_snapshot(
            event_limit=80,
            trace_limit=400,
            mcp_probe=operations_snapshot.get("mcp_probe") if isinstance(operations_snapshot.get("mcp_probe"), dict) else None,
            recent_action_entries=list(recent_action_entries),
        )

    def _export_runtime_snapshot_report(self) -> None:
        operations_snapshot = dict(self._last_snapshot.get("operations") or {})
        recent_action_entries = list(self._operator_log_entries)

        def _worker() -> dict[str, Any]:
            export_path = self._export_runtime_snapshot_report_path(
                operations_snapshot=operations_snapshot,
                recent_action_entries=recent_action_entries,
            )
            return {
                "message": f"Control-plane snapshot exported to {export_path}",
                "path": export_path,
            }

        self._run_operator_background_task(kind="export_snapshot", worker=_worker)

    def _append_operator_log(
        self,
        message: str,
        *,
        operator_snapshot: dict[str, Any] | None = None,
        refresh_snapshot: bool = True,
    ) -> None:
        self._operator_log_entries.append(self._build_operator_log_entry(message))
        self._operator_log_entries = self._operator_log_entries[-12:]
        try:
            if isinstance(operator_snapshot, dict):
                self._apply_operator_snapshot(operator_snapshot, render_log=False)
            elif refresh_snapshot:
                operations_snapshot = self._load_operator_snapshot()
                self._apply_operator_snapshot(operations_snapshot, render_log=False)
        except Exception:
            pass
        for board_context in self._board_contexts_in_display_order():
            with self._board_context_scope(board_context):
                self._render_operator_log()

    def _render_operator_log(self) -> None:
        operations_snapshot = dict(self._last_snapshot.get("operations") or {})
        recent_actions = [item for item in (operations_snapshot.get("recent_actions") or []) if isinstance(item, dict)]
        filtered_actions = self._filtered_operator_actions(operations_snapshot) if operations_snapshot else []
        active_filter_parts = [
            f"status={self.operator_status_selector.currentText().strip() or 'All statuses'}",
            f"type={self.operator_audit_selector.currentText().strip() or 'All action types'}",
            f"group={self.operator_group_selector.currentText().strip() or 'All action groups'}",
            f"source={self.operator_source_selector.currentText().strip() or 'All sources'}",
        ]
        rows = "".join(
            "".join(
                [
                    f"<li><b>{html.escape(str(item.get('timestamp') or 'n/a'))}</b><br>",
                    f"{html.escape(str(item.get('title') or 'operator.action'))}<br>",
                    f"<span style=\"color:{self.scheme['col8']};\">{html.escape(str(item.get('summary') or ''))} | group={html.escape(str(item.get('action_group') or 'operator'))} | audit={html.escape(str(item.get('audit_type') or 'action'))} | source={html.escape(str(item.get('source') or 'desktop_operator'))} | status={html.escape(str(item.get('status') or 'info'))}</span></li>",
                ]
            )
            for item in filtered_actions
        )
        if not rows:
            if recent_actions:
                rows = "<li>No operator actions match the active filters.</li>"
            else:
                rows = "".join(
                    f"<li>{html.escape(str(item))}</li>" for item in reversed(self._operator_log_entries)
                )
        self.operator_log_view.setHtml(
            "<h3>Recent Operator Actions</h3>"
            + f"<p><b>Visible:</b> {len(filtered_actions) if recent_actions else len(self._operator_log_entries)} / <b>Total:</b> {len(recent_actions) if recent_actions else len(self._operator_log_entries)} | {' | '.join(html.escape(part) for part in active_filter_parts)}</p>"
            + (
                f"<ul>{rows}</ul>"
                if rows
                else "<p>Probe, repair, and export results appear here.</p>"
            )
        )

    def _render_status_chip(self, label: str, color: str) -> str:
        return (
            f"<span style=\"display:inline-block;padding:2px 8px;border-radius:999px;"
            f"background:{color};color:{self.scheme['col7']};font-weight:600;\">{html.escape(label)}</span>"
        )

    def _set_metric_value(self, key: str, value: Any) -> None:
        label = self._metric_labels.get(key)
        if label is not None:
            label.setText(str(value))

    def _render_bool_chip(self, value: bool) -> str:
        chip_color = SIGNAL_GREEN if value else SIGNAL_RED
        chip_text = "ready" if value else "missing"
        return (
            f"<span style=\"display:inline-block;padding:2px 8px;border-radius:999px;"
            f"background:{chip_color};color:{self.scheme['col7']};font-weight:600;\">{chip_text}</span>"
        )


@dataclass
class EnvVariableObject:
    object_name: str
    value: str
    enabled: bool


@dataclass
class EnvSectionObject:
    object_name: str
    comment_lines: list[str]
    variable_objects: list[EnvVariableObject]


@dataclass
class EnvVariableControlObject:
    variable_object: EnvVariableObject
    enabled_toggle: QCheckBox
    value_widget: QWidget
    value_kind: str


class EnvConfigDomainService:
    _ENV_ASSIGNMENT_PATTERN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

    def load_object(self, object_name: str) -> list[str]:
        env_path = Path(str(object_name or "")).expanduser()
        if not env_path.exists() or not env_path.is_file():
            return []
        try:
            return env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []

    def parse_object(self, object_name: str, line_payload: list[str]) -> list[EnvSectionObject]:
        _ = object_name
        section_objects: list[EnvSectionObject] = []
        current_section_object: EnvSectionObject | None = None
        pending_comment_lines: list[str] = []
        leading_comment_lines: list[str] = []

        for line_object in line_payload:
            raw_line = str(line_object or "")
            stripped_line = raw_line.strip()
            if not stripped_line:
                if pending_comment_lines and not section_objects:
                    leading_comment_lines.extend(pending_comment_lines)
                pending_comment_lines.clear()
                continue

            if stripped_line.startswith("#"):
                comment_payload = stripped_line[1:].strip()
                disabled_variable_object = self._parse_variable_object(comment_payload, enabled=False)
                if disabled_variable_object is not None:
                    current_section_object = self._resolve_section_object(
                        section_objects,
                        pending_comment_lines,
                        leading_comment_lines,
                        current_section_object,
                    )
                    current_section_object.variable_objects.append(disabled_variable_object)
                    pending_comment_lines.clear()
                    leading_comment_lines.clear()
                elif comment_payload:
                    pending_comment_lines.append(comment_payload)
                continue

            enabled_variable_object = self._parse_variable_object(stripped_line, enabled=True)
            if enabled_variable_object is None:
                pending_comment_lines.clear()
                continue

            current_section_object = self._resolve_section_object(
                section_objects,
                pending_comment_lines,
                leading_comment_lines,
                current_section_object,
            )
            current_section_object.variable_objects.append(enabled_variable_object)
            pending_comment_lines.clear()
            leading_comment_lines.clear()

        return section_objects

    def serialize_object(self, object_name: str, section_objects: list[EnvSectionObject]) -> str:
        _ = object_name
        serialized_lines: list[str] = []
        normalized_sections = [
            section_object for section_object in section_objects
            if isinstance(section_object, EnvSectionObject)
            and list(section_object.variable_objects)
        ]

        for section_index, section_object in enumerate(normalized_sections):
            section_title = str(section_object.object_name or "").strip()
            include_section_header = bool(section_title) and section_title.lower() != "general"
            section_comment_lines = [
                str(comment_line or "").strip()
                for comment_line in list(section_object.comment_lines)
                if str(comment_line or "").strip()
            ]

            if include_section_header:
                serialized_lines.append(f"# {section_title}")
            for comment_line in section_comment_lines:
                serialized_lines.append(f"# {comment_line}")

            for variable_object in list(section_object.variable_objects):
                variable_name = str(variable_object.object_name or "").strip()
                if not variable_name:
                    continue
                variable_value = str(variable_object.value or "")
                line_prefix = "" if bool(variable_object.enabled) else "# "
                serialized_lines.append(f"{line_prefix}{variable_name}={variable_value}")

            if section_index < len(normalized_sections) - 1:
                serialized_lines.append("")

        serialized_payload = "\n".join(serialized_lines).rstrip()
        if serialized_payload:
            serialized_payload += "\n"
        return serialized_payload

    def store_object(self, object_name: str, section_objects: list[EnvSectionObject]) -> None:
        env_path = Path(str(object_name or "")).expanduser()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        serialized_payload = self.serialize_object(object_name, section_objects)
        env_path.write_text(serialized_payload, encoding="utf-8")

    @classmethod
    def _parse_variable_object(cls, line_payload: str, *, enabled: bool) -> EnvVariableObject | None:
        payload = str(line_payload or "").strip()
        if not payload:
            return None

        assignment_match = cls._ENV_ASSIGNMENT_PATTERN.match(payload)
        if assignment_match is None:
            return None

        variable_name = str(assignment_match.group(1) or "").strip()
        if not variable_name:
            return None

        variable_value = str(assignment_match.group(2) or "")
        return EnvVariableObject(
            object_name=variable_name,
            value=variable_value,
            enabled=bool(enabled),
        )

    @staticmethod
    def _resolve_section_object(
        section_objects: list[EnvSectionObject],
        pending_comment_lines: list[str],
        leading_comment_lines: list[str],
        fallback_section_object: EnvSectionObject | None,
    ) -> EnvSectionObject:
        normalized_comment_lines = [
            str(comment_line or "").strip()
            for comment_line in pending_comment_lines
            if str(comment_line or "").strip()
        ]
        normalized_leading_comment_lines = [
            str(comment_line or "").strip()
            for comment_line in leading_comment_lines
            if str(comment_line or "").strip()
        ]
        if normalized_comment_lines:
            next_section_object = EnvSectionObject(
                object_name=normalized_comment_lines[0],
                comment_lines=list(normalized_leading_comment_lines) + list(normalized_comment_lines[1:]),
                variable_objects=[],
            )
            section_objects.append(next_section_object)
            return next_section_object

        if isinstance(fallback_section_object, EnvSectionObject):
            return fallback_section_object

        default_section_object = EnvSectionObject(
            object_name="General",
            comment_lines=list(normalized_leading_comment_lines),
            variable_objects=[],
        )
        section_objects.append(default_section_object)
        return default_section_object





try:
    if __package__:
        from .artifact_backends import ExtensionArtifactBackendService, load_default_graph_backend_service
    else:
        from artifact_backends import ExtensionArtifactBackendService, load_default_graph_backend_service
except ImportError as e:
    msg = str(e)
    if "attempted relative import" in msg or "no known parent package" in msg:
        from alde.artifact_backends import ExtensionArtifactBackendService, load_default_graph_backend_service  # type: ignore  # noqa: E402
    else:
        raise


class ExtensionArtifactService:
    def __init__(self, graph_service: ExtensionArtifactBackendService | None = None) -> None:
        self._backend_service = graph_service or load_default_graph_backend_service()

    def load_connection_preview(self, *, source_uri: str | None = None) -> dict[str, Any]:
        preview_payload = dict(self._backend_service.load_connection_preview(source_uri=source_uri) or {})
        tool_rows = [dict(item) for item in (preview_payload.get("tools") or []) if isinstance(item, dict)]

        enriched_rows: list[dict[str, Any]] = []
        for tool_row in tool_rows:
            next_tool_row = dict(tool_row)
            tool_id = str(next_tool_row.get("tool_id") or "").strip()
            manifest_payload = self._backend_service.load_tool_runtime_manifest(tool_id=tool_id, source_uri=source_uri)
            next_tool_row["runtime_manifest"] = dict(manifest_payload or {})
            artifact_payload = manifest_payload.get("runtime_artifact")
            if isinstance(artifact_payload, dict):
                next_tool_row["runtime_artifact"] = dict(artifact_payload)
            if not next_tool_row.get("runtime_classes"):
                next_tool_row["runtime_classes"] = list(manifest_payload.get("runtime_classes") or [])
            enriched_rows.append(next_tool_row)

        preview_payload["tools"] = enriched_rows
        return preview_payload

    def load_object_widget(
        self,
        *,
        object_name: str,
        source_uri: str,
        parent: QWidget | None = None,
        scheme: Mapping[str, str] | None = None,
    ) -> QWidget:
        manifest_payload = self._backend_service.load_tool_runtime_manifest(tool_id=object_name, source_uri=source_uri)
        try:
            return self._load_widget_from_manifest(
                object_name=object_name,
                source_uri=source_uri,
                manifest_payload=manifest_payload,
                parent=parent,
                scheme=scheme,
            )
        except Exception as exc:
            return self._build_error_widget(
                tool_id=object_name,
                source_uri=source_uri,
                manifest_payload=manifest_payload,
                error=exc,
                parent=parent,
            )

    def _load_widget_from_manifest(
        self,
        *,
        object_name: str,
        source_uri: str,
        manifest_payload: Mapping[str, Any],
        parent: QWidget | None = None,
        scheme: Mapping[str, str] | None = None,
    ) -> QWidget:
        runtime_artifact = manifest_payload.get("runtime_artifact") if isinstance(manifest_payload.get("runtime_artifact"), dict) else {}
        entry_module = str(runtime_artifact.get("entry_module") or "").strip()
        entry_class = str(runtime_artifact.get("entry_class") or "").strip()
        build_method = str(runtime_artifact.get("build_method") or "load_object_widget").strip() or "load_object_widget"

        if not entry_module or not entry_class:
            raise ValueError("runtime artifact is missing entry_module or entry_class")

        artifact_module = importlib.import_module(entry_module)
        entry_object = getattr(artifact_module, entry_class, None)
        if entry_object is None:
            raise ImportError(f"entry class '{entry_class}' not found in '{entry_module}'")

        if isinstance(entry_object, type):
            try:
                artifact_instance = entry_object(graph_service=self._backend_service)
            except TypeError:
                artifact_instance = entry_object()
        else:
            artifact_instance = entry_object

        build_callable = getattr(artifact_instance, build_method, None)
        if callable(build_callable):
            widget_payload = build_callable(
                object_name=object_name,
                source_uri=source_uri,
                parent=parent,
                scheme=scheme,
            )
            if isinstance(widget_payload, QWidget):
                return widget_payload

        if callable(artifact_instance):
            widget_payload = artifact_instance(
                object_name=object_name,
                source_uri=source_uri,
                parent=parent,
                scheme=scheme,
            )
            if isinstance(widget_payload, QWidget):
                return widget_payload

        raise TypeError("artifact entry did not return a QWidget instance")

    def _build_error_widget(
        self,
        *,
        tool_id: str,
        source_uri: str,
        manifest_payload: Mapping[str, Any],
        error: Exception,
        parent: QWidget | None,
    ) -> QWidget:
        error_widget = QTextBrowser(parent)
        error_widget.setOpenExternalLinks(False)
        error_widget.setOpenLinks(False)

        runtime_artifact = manifest_payload.get("runtime_artifact") if isinstance(manifest_payload.get("runtime_artifact"), dict) else {}
        artifact_module = html.escape(str(runtime_artifact.get("entry_module") or "n/a"))
        artifact_class = html.escape(str(runtime_artifact.get("entry_class") or "n/a"))
        escaped_error = html.escape(f"{type(error).__name__}: {error}")
        error_widget.setHtml(
            "".join(
                [
                    "<h3>Native QWidget artifact load failed</h3>",
                    f"<p><b>tool_id:</b> <code>{html.escape(str(tool_id or ''))}</code></p>",
                    f"<p><b>source_uri:</b> <code>{html.escape(str(source_uri or ''))}</code></p>",
                    f"<p><b>entry_module:</b> <code>{artifact_module}</code></p>",
                    f"<p><b>entry_class:</b> <code>{artifact_class}</code></p>",
                    f"<p><b>error:</b> {escaped_error}</p>",
                ]
            )
        )
        return error_widget


_TITLEBAR_URI_PROXY_DATA = "__window_titlebar_uri_proxy__"
_WINDOW_FRAME_EMBEDDED_TAB_BAR_OBJECT_NAME = "windowFrameExtensionsEmbeddedTabBar"


class ExtensionsWorkspaceTabBar(QTabBar):
    URI_PROXY_DATA = "__window_titlebar_uri_proxy__"

    def __init__(
        self,
        *args,
        text_formatter: Callable[[str], str] | None = None,
        tab_row_height: int = 16,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._text_formatter = text_formatter
        self._tab_row_height = max(16, int(tab_row_height))
        self._titlebar_uri_mouse_target: QLineEdit | None = None
        self.setLayoutDirection(Qt.RightToLeft)

    def set_text_formatter(self, text_formatter: Callable[[str], str] | None) -> None:
        self._text_formatter = text_formatter
        self.updateGeometry()
        self.update()

    def set_tab_row_height(self, value: int) -> None:
        resolved_height = max(16, int(value))
        if resolved_height == self._tab_row_height:
            return
        self._tab_row_height = resolved_height
        self.updateGeometry()
        self.update()

    def _stable_display_text(self, index: int) -> str:
        if index < 0 or index >= self.count():
            return ""
        tab_data = self.tabData(index)
        full_text = str(tab_data or self.tabText(index) or "")
        formatter = self._text_formatter
        if callable(formatter):
            try:
                return str(formatter(full_text) or "")
            except Exception:
                return full_text
        return full_text

    def _extra_button_width(self, index: int) -> int:
        extra_width = 18
        for side in (QTabBar.LeftSide, QTabBar.RightSide):
            button = self.tabButton(index, side)
            if not isinstance(button, QWidget):
                continue
            try:
                button_width = max(int(button.sizeHint().width()), int(button.width()), 0)
            except RuntimeError:
                button_width = 0
            if button_width > 0:
                extra_width += button_width + 4
        return extra_width

    def _stable_tab_size_hint(self, index: int) -> QSize:
        base_hint = super().tabSizeHint(index)
        tab_data = self.tabData(index)
        titlebar_proxy_mode = bool(self.property("extensions_titlebar_proxy_bar"))
        if tab_data == type(self).URI_PROXY_DATA:
            titlebar_button = self.tabButton(index, QTabBar.LeftSide)
            if not isinstance(titlebar_button, QWidget):
                titlebar_button = self.tabButton(index, QTabBar.RightSide)
            if isinstance(titlebar_button, QWidget):
                try:
                    button_hint = titlebar_button.sizeHint()
                except RuntimeError:
                    button_hint = QSize(0, 0)
                width = max(120, int(button_hint.width()) + 14)
                if titlebar_proxy_mode:
                    height = int(self._tab_row_height)
                else:
                    height = max(int(base_hint.height()), int(button_hint.height()), int(self._tab_row_height))
                return QSize(width, height)
        stable_text = self._stable_display_text(index)
        text_width = max(self.fontMetrics().horizontalAdvance(stable_text), 12)
        width = max(24, text_width + self._extra_button_width(index))
        if titlebar_proxy_mode:
            height = int(self._tab_row_height)
        else:
            height = max(int(base_hint.height()), int(self._tab_row_height))
        return QSize(width, height)

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802
        return self._stable_tab_size_hint(index)

    def minimumTabSizeHint(self, index: int) -> QSize:  # noqa: N802
        return self._stable_tab_size_hint(index)

    def _paint_titlebar_uri_tab_fill(self) -> None:
        if not bool(self.property("extensions_titlebar_uri_mode")):
            return
        fill_color = QColor(str(self.property("extensions_titlebar_uri_fill") or "#101010"))
        if not fill_color.isValid():
            fill_color = QColor("#101010")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill_color)
        for index in range(self.count()):
            if self.tabData(index) != type(self).URI_PROXY_DATA:
                continue
            tab_rect = self.tabRect(index).adjusted(1, 1, -1, -1)
            if not tab_rect.isValid():
                continue
            painter.drawRoundedRect(tab_rect, 6, 6)
        painter.end()

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        self._paint_titlebar_uri_tab_fill()

    def _resolve_titlebar_uri_mouse_target(self, local_pos: QPoint) -> tuple[QLineEdit, QPoint] | None:
        if not bool(self.property("extensions_titlebar_uri_mode")):
            return None
        tab_index = self.tabAt(local_pos)
        if tab_index < 0 or self.tabData(tab_index) != type(self).URI_PROXY_DATA:
            return None
        global_pos = self.mapToGlobal(local_pos)
        for side in (QTabBar.LeftSide, QTabBar.RightSide):
            tab_button = self.tabButton(tab_index, side)
            if not isinstance(tab_button, QWidget):
                continue
            container_local = tab_button.mapFromGlobal(global_pos)
            if not tab_button.rect().contains(container_local):
                continue
            handle = getattr(self, "_titlebar_uri_proxy_handle", None)
            if isinstance(handle, QWidget):
                handle_local = handle.mapFromGlobal(global_pos)
                if handle.rect().contains(handle_local):
                    return None
            line_edit = tab_button.findChild(QLineEdit, "extensionsGraphUriInput")
            if isinstance(line_edit, QLineEdit):
                return line_edit, line_edit.mapFromGlobal(global_pos)
        return None

    def _forward_titlebar_uri_mouse_event(self, line_edit: QLineEdit, event) -> None:
        global_pos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
        local_pos = line_edit.mapFromGlobal(global_pos)
        line_edit.setFocus(Qt.MouseFocusReason)
        forwarded_event = QMouseEvent(
            event.type(),
            QPointF(local_pos),
            QPointF(local_pos),
            QPointF(global_pos),
            event.button(),
            event.buttons(),
            event.modifiers(),
        )
        QApplication.sendEvent(line_edit, forwarded_event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            uri_target = self._resolve_titlebar_uri_mouse_target(event.pos())
            if uri_target is not None:
                line_edit, _line_local_pos = uri_target
                self._titlebar_uri_mouse_target = line_edit
                self._forward_titlebar_uri_mouse_event(line_edit, event)
                event.accept()
                return
        self._titlebar_uri_mouse_target = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if isinstance(self._titlebar_uri_mouse_target, QLineEdit) and (event.buttons() & Qt.LeftButton):
            self._forward_titlebar_uri_mouse_event(self._titlebar_uri_mouse_target, event)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if isinstance(self._titlebar_uri_mouse_target, QLineEdit):
            self._forward_titlebar_uri_mouse_event(self._titlebar_uri_mouse_target, event)
            self._titlebar_uri_mouse_target = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ExtensionsExternalUriLineEdit(QLineEdit):
    menuRequested = Signal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._drag_candidate = False
        self._drag_active = False
        self._menu_request_on_release = False
        self._suppress_next_release_menu = False
        self._drag_start_global = QPoint()
        self._drag_start_window = QPoint()
        self._drag_press_local = QPoint()
        self._drag_press_cursor_position = -1

    def _try_system_move(self) -> bool:
        top_window = self.window()
        handle = top_window.windowHandle() if isinstance(top_window, QWidget) else None
        if handle is None or not hasattr(handle, "startSystemMove"):
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False

    def _titlebar_tab_edit_mode_enabled(self) -> bool:
        return bool(self.property("extensions_titlebar_uri_tab_mode"))

    def _should_start_window_drag(self, current_local_pos: QPoint) -> bool:
        if self._titlebar_tab_edit_mode_enabled():
            return False
        if self.hasSelectedText():
            return False
        return True

    def mousePressEvent(self, event):  # noqa: N802
        if self._titlebar_tab_edit_mode_enabled():
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            self._drag_candidate = True
            self._drag_active = False
            self._suppress_next_release_menu = False
            self._menu_request_on_release = True
            self._drag_start_global = event.globalPosition().toPoint()
            top_window = self.window()
            self._drag_start_window = top_window.frameGeometry().topLeft() if isinstance(top_window, QWidget) else QPoint()
            self._drag_press_local = event.position().toPoint() if hasattr(event, "position") else event.pos()
            try:
                self._drag_press_cursor_position = int(self.cursorPositionAt(self._drag_press_local))
            except Exception:
                self._drag_press_cursor_position = -1
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._titlebar_tab_edit_mode_enabled():
            super().mouseMoveEvent(event)
            return
        if self._drag_active and (event.buttons() & Qt.LeftButton):
            delta = event.globalPosition().toPoint() - self._drag_start_global
            top_window = self.window()
            if isinstance(top_window, QWidget):
                top_window.move(self._drag_start_window + delta)
            event.accept()
            return

        if self._drag_candidate and (event.buttons() & Qt.LeftButton):
            current_local_pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            drag_distance = (current_local_pos - self._drag_press_local).manhattanLength()
            if drag_distance >= QApplication.startDragDistance() and self._should_start_window_drag(current_local_pos):
                self._drag_candidate = False
                self._menu_request_on_release = False
                if self._try_system_move():
                    self._suppress_next_release_menu = True
                    event.accept()
                    return
                self._drag_active = True
                delta = event.globalPosition().toPoint() - self._drag_start_global
                top_window = self.window()
                if isinstance(top_window, QWidget):
                    top_window.move(self._drag_start_window + delta)
                event.accept()
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._titlebar_tab_edit_mode_enabled():
            super().mouseReleaseEvent(event)
            return
        popup_requested = (
            event.button() == Qt.LeftButton
            and self._drag_candidate
            and not self._drag_active
            and self._menu_request_on_release
            and not self._suppress_next_release_menu
        )
        self._drag_candidate = False
        self._drag_active = False
        self._menu_request_on_release = False
        self._suppress_next_release_menu = False
        super().mouseReleaseEvent(event)
        if popup_requested:
            self.menuRequested.emit()


class ExtensionsWorkspaceWidget(QWidget):
    widgetStateChanged = Signal()
    _LOCAL_WIDGET_STATE_REL_PATH = "AppData/runtime_extensions_workspace.json"
    _START_TAB_TITLE = "Loadable Extensions"
    _TAB_LABEL_MAX_VISIBLE_CHARS = 8

    @classmethod
    def _hide_titlebar_proxy_tab_for_text(cls, tab_text: str) -> bool:
        raw_title = str(tab_text or "").strip().lower()
        if not raw_title:
            return False

        normalized_title = raw_title.replace("<", "").replace(">", "").replace("/", "").strip()
        if raw_title.startswith("loadable") or normalized_title.startswith("loadable"):
            return True
        if normalized_title in {"build", "builder"}:
            return True
        return False

    @staticmethod
    def _tab_category_for_text(tab_text: str) -> str:
        title_key = str(tab_text or "").strip().lower()
        if title_key.startswith("monitoring"):
            return "orange"
        if title_key.startswith("operator"):
            return "turquoise"
        if title_key.startswith("trace") or title_key.startswith("event") or "threat flow" in title_key:
            return "blue"
        if title_key.startswith("configuration"):
            return "magenta"
        if title_key.startswith("build") or title_key.startswith("extension") or "connect" in title_key or "loadable" in title_key:
            return "sunyellow"
        return "blue"

    @staticmethod
    def _tab_palette(category: str) -> dict[str, str]:
        return ControlPlaneWidget._control_monitor_splitter_palette(ControlPlaneWidget, category)

    def _format_extensions_tab_text(self, value: str) -> str:
        text = str(value or "")
        visible_limit = max(0, int(getattr(self, "_tab_label_max_visible_chars", self._TAB_LABEL_MAX_VISIBLE_CHARS)))

        if not text:
            return ""
        if visible_limit <= 0:
            return text
        if len(text) <= visible_limit:
            return text
        return "".join(list(text)[:visible_limit])

    def _extensions_tab_full_text(self, tab_bar: QTabBar, index: int) -> str:
        if index < 0 or index >= tab_bar.count():
            return ""
        tab_data = tab_bar.tabData(index)
        if isinstance(tab_data, str) and tab_data:
            return tab_data
        return str(tab_bar.tabText(index) or "")

    def _set_extensions_tab_text(self, tab_bar: QTabBar, index: int, value: str) -> None:
        if index < 0 or index >= tab_bar.count():
            return

        full_text = str(value or "")
        tab_bar.setTabData(index, full_text)
        tab_bar.setTabText(index, self._format_extensions_tab_text(full_text))

        try:
            source_tab_bar = self.extensions_tabs.tabBar()
        except RuntimeError:
            source_tab_bar = None
        if isinstance(source_tab_bar, QTabBar) and tab_bar is source_tab_bar:
            self.extensions_tabs.setTabToolTip(index, full_text)

    def _is_extensions_tab_bar(self, tab_bar: QTabBar) -> bool:
        try:
            if tab_bar is self.extensions_tabs.tabBar():
                return True
        except RuntimeError:
            return False
        return bool(tab_bar.property("extensions_embedded_tab_bar"))

    def __init__(
        self,
        accent: dict[str, str] | None = None,
        base: dict[str, str] | None = None,
        parent: QWidget | None = None,
        source_uri: str | None = None,
        control_plane_widget_ref: Any = None,
        initial_tool_id: str = "",
        auto_load_initial_tool: bool = False,
        hide_internal_tab_bar: bool = False,
    ) -> None:
        super().__init__(parent)
        self._accent = dict(accent or {})
        self._base = dict(base or {})
        self.scheme: dict[str, str] = _build_scheme(self._accent, self._base)
        self._backend_service = ExtensionArtifactService()
        self._session_counter = 0
        self._session_state_by_tab: dict[QWidget, dict[str, Any]] = {}
        self._preview_initialized = False
        self._initial_source_uri = str(source_uri or "").strip()
        self._control_plane_widget = control_plane_widget_ref
        self._initial_tool_id = str(initial_tool_id or "").strip()
        self._auto_load_initial_tool = bool(auto_load_initial_tool)
        self._hide_internal_tab_bar = bool(hide_internal_tab_bar)
        self._tab_label_max_visible_chars = self._TAB_LABEL_MAX_VISIBLE_CHARS
        self._embedded_tab_bar_proxies: list[QTabBar] = []
        self._external_uri_proxies: list[QLineEdit] = []
        self._titlebar_uri_drag_handle: QToolButton | None = None
        self._titlebar_uri_drag_proxy_bar: QTabBar | None = None
        self._titlebar_uri_drag_active = False
        self._titlebar_uri_drag_moved = False
        self._hover_tab_bar: QTabBar | None = None
        self._hover_tab_index = -1
        self._hover_tab_base_text = ""
        self._hover_tab_phase = 0
        self._hover_tab_marquee_timer = QTimer(self)
        self._hover_tab_marquee_timer.setInterval(160)
        self._hover_tab_marquee_timer.timeout.connect(self._tick_tab_hover_marquee)
        self._pending_preview_session_state: dict[str, Any] | None = None
        self._preview_refresh_timer = QTimer(self)
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.setInterval(260)
        self._preview_refresh_timer.timeout.connect(self._flush_scheduled_preview_refresh)

        self._build_ui()
        # Add ControlPlaneWidget as first tab if provided
        if self._control_plane_widget is not None:
            self._add_control_plane_tab()
        restored_state = self._load_local_widget_state()
        restored_sessions = [
            dict(item)
            for item in (restored_state.get("sessions") or [])
            if isinstance(item, dict)
        ]
        restored_active_session_token = str(restored_state.get("active_session_token") or "").strip()

        if restored_sessions:
            active_tab_index = -1
            for session_payload in restored_sessions:
                restored_session_state = self._add_extension_tab(
                    source_uri=str(session_payload.get("uri") or "").strip(),
                    tool_id=str(session_payload.get("selected_tool") or "").strip(),
                    keep_local=bool(session_payload.get("keep_local", True)),
                    catalog_only=bool(session_payload.get("catalog_only", False)),
                    activate=False,
                    session_token=str(session_payload.get("session_token") or "").strip(),
                )
                if str(restored_session_state.get("session_token") or "") == restored_active_session_token:
                    tab_widget = restored_session_state.get("tab_widget")
                    if isinstance(tab_widget, QWidget):
                        active_tab_index = self.extensions_tabs.indexOf(tab_widget)
            if active_tab_index >= 0:
                self.extensions_tabs.setCurrentIndex(active_tab_index)
            elif self.extensions_tabs.count() > 0:
                self.extensions_tabs.setCurrentIndex(self.extensions_tabs.count() - 1)
        else:
            initial_source_uri = self._initial_source_uri or str(restored_state.get("uri") or "")
            initial_tool_id = self._initial_tool_id or str(restored_state.get("selected_tool") or "")
            self._add_extension_tab(
                source_uri=initial_source_uri,
                tool_id=initial_tool_id,
                keep_local=bool(restored_state.get("keep_local")),
                catalog_only=True,
                activate=True,
            )
        if self._auto_load_initial_tool:
            active_state = self._active_session_state()
            if isinstance(active_state, dict):
                self._load_extension_into_session(active_state, fit_view=True)
        self.update_scheme(self._accent, self._base)

    def _local_widget_state_path(self) -> Path:
        try:
            return Path(__file__).resolve().parents[2] / self._LOCAL_WIDGET_STATE_REL_PATH
        except Exception:
            return Path.cwd() / self._LOCAL_WIDGET_STATE_REL_PATH

    def _load_local_widget_state(self) -> dict[str, Any]:
        state_path = self._local_widget_state_path()
        default_uri = "agentsdb://127.0.0.1:2331/tools:graph_view"
        default_state = {
            "uri": default_uri,
            "selected_tool": "",
            "keep_local": True,
            "last_status": "",
            "last_widget_summary": {},
            "sessions": [],
            "active_session_token": "",
        }
        if not state_path.exists():
            return dict(default_state)
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return dict(default_state)
            restored_sessions: list[dict[str, Any]] = []
            for item in (payload.get("sessions") or []):
                if not isinstance(item, dict):
                    continue
                restored_sessions.append(
                    {
                        "session_token": str(item.get("session_token") or uuid.uuid4().hex).strip() or uuid.uuid4().hex,
                        "uri": str(item.get("uri") or default_uri).strip() or default_uri,
                        "selected_tool": str(item.get("selected_tool") or "").strip(),
                        "keep_local": bool(item.get("keep_local", True)),
                        "catalog_only": bool(item.get("catalog_only", False)),
                    }
                )
            return {
                "uri": str(payload.get("uri") or default_uri),
                "selected_tool": str(payload.get("selected_tool") or ""),
                "keep_local": bool(payload.get("keep_local", True)),
                "last_status": str(payload.get("last_status") or ""),
                "last_widget_summary": dict(payload.get("last_widget_summary") or {}),
                "sessions": restored_sessions,
                "active_session_token": str(payload.get("active_session_token") or "").strip(),
            }
        except Exception:
            return dict(default_state)

    def _persisted_session_payload(self, session_state: Mapping[str, Any]) -> dict[str, Any]:
        uri_input = session_state.get("uri_input")
        selected_payload = session_state.get("selected_tool_payload")
        selected_tool_id = ""
        if isinstance(selected_payload, dict):
            selected_tool_id = str(selected_payload.get("tool_id") or "").strip()
        return {
            "session_token": str(session_state.get("session_token") or uuid.uuid4().hex).strip() or uuid.uuid4().hex,
            "uri": str(uri_input.text() if isinstance(uri_input, QLineEdit) else "").strip(),
            "selected_tool": selected_tool_id,
            "keep_local": bool(session_state.get("keep_local", True)),
            "catalog_only": bool(session_state.get("catalog_only", False)),
        }

    def _persisted_session_payloads_in_visual_order(self) -> list[dict[str, Any]]:
        persisted_sessions: list[dict[str, Any]] = []
        for tab_index in range(self.extensions_tabs.count()):
            tab_widget = self.extensions_tabs.widget(tab_index)
            if not isinstance(tab_widget, QWidget):
                continue
            session_state = self._session_state_by_tab.get(tab_widget)
            if not isinstance(session_state, dict):
                continue
            persisted_sessions.append(self._persisted_session_payload(session_state))
        return persisted_sessions

    def _loaded_widget_summary(self, session_state: Mapping[str, Any]) -> dict[str, Any]:
        loaded_widget = session_state.get("loaded_widget")
        summary_callable = getattr(loaded_widget, "snapshot_summary", None)
        if callable(summary_callable):
            try:
                summary_payload = dict(summary_callable() or {})
            except Exception:
                summary_payload = {}
        else:
            summary_payload = {}
        summary_payload.setdefault("tool_id", self._session_tool_id(session_state))
        return summary_payload

    def _persist_local_widget_state(self, session_state: Mapping[str, Any] | None = None) -> None:
        state = dict(session_state or self._active_session_state() or {})
        uri_input = state.get("uri_input")

        selected_tool_id = ""
        selected_payload = state.get("selected_tool_payload")
        if isinstance(selected_payload, dict):
            selected_tool_id = str(selected_payload.get("tool_id") or "").strip()

        payload = {
            "uri": str(uri_input.text() if isinstance(uri_input, QLineEdit) else "").strip(),
            "selected_tool": selected_tool_id,
            "keep_local": True,
            "last_status": "",
            "last_widget_summary": self._loaded_widget_summary(state),
            "sessions": self._persisted_session_payloads_in_visual_order(),
            "active_session_token": str((self._active_session_state() or {}).get("session_token") or "").strip(),
        }

        state_path = self._local_widget_state_path()
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            return

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._preview_initialized:
            return
        active_state = self._active_session_state()
        if active_state:
            self._refresh_connection_preview(active_state)
        self._preview_initialized = True

    def current_widget_uri(self) -> str:
        active_state = self._active_session_state()
        if not active_state:
            return ""
        uri_input = active_state.get("uri_input")
        if isinstance(uri_input, QLineEdit):
            return str(uri_input.text()).strip()
        return ""

    def current_tool_id(self) -> str:
        active_state = self._active_session_state()
        if not active_state:
            return "graph_view"
        selected_payload = active_state.get("selected_tool_payload")
        if isinstance(selected_payload, dict):
            return str(selected_payload.get("tool_id") or "graph_view").strip() or "graph_view"
        return "graph_view"

    def refresh_graph(self) -> None:
        active_state = self._active_session_state()
        if not active_state:
            return
        self._load_extension_into_session(active_state, fit_view=True)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.extensions_tabs = QTabWidget(self)
        self.extensions_tabs.setObjectName("extensionsTabs")
        self.extensions_tabs.setDocumentMode(True)
        self.extensions_tabs.setMovable(True)
        self.extensions_tabs.setTabsClosable(True)
        self.extensions_tabs.setTabBar(
            ExtensionsWorkspaceTabBar(
                self.extensions_tabs,
                text_formatter=self._format_extensions_tab_text,
            )
        )
        self.extensions_tabs.setUsesScrollButtons(False)
        tab_bar = self.extensions_tabs.tabBar()
        tab_bar.setUsesScrollButtons(False)
        tab_bar.setExpanding(False)
        tab_bar.setElideMode(Qt.ElideNone)
        tab_bar.setMouseTracking(True)
        tab_bar.installEventFilter(self)
        tab_bar.tabMoved.connect(self._handle_extensions_tab_moved)
        self.extensions_tabs.tabCloseRequested.connect(self._close_extension_tab)
        self.extensions_tabs.currentChanged.connect(self._handle_active_tab_changed)

        root_layout.addWidget(self.extensions_tabs, 1)
        self._sync_embedded_tab_bar_visibility()

    def _prune_embedded_tab_bar_proxies(self) -> None:
        live_proxies: list[QTabBar] = []
        for proxy in self._embedded_tab_bar_proxies:
            if not isinstance(proxy, QTabBar):
                continue
            try:
                if proxy.parent() is None:
                    continue
            except RuntimeError:
                continue
            live_proxies.append(proxy)
        self._embedded_tab_bar_proxies = live_proxies

    def _prune_external_uri_proxies(self) -> None:
        live_proxies: list[QLineEdit] = []
        for proxy in self._external_uri_proxies:
            if not isinstance(proxy, QLineEdit):
                continue
            try:
                if proxy.parent() is None:
                    continue
            except RuntimeError:
                continue
            live_proxies.append(proxy)
        self._external_uri_proxies = live_proxies

    def _sync_embedded_tab_bar_visibility(self) -> None:
        self._prune_embedded_tab_bar_proxies()
        try:
            tab_bar = self.extensions_tabs.tabBar()
        except RuntimeError:
            return
        tab_bar.setVisible(not self._hide_internal_tab_bar and not self._embedded_tab_bar_proxies)

    def _sync_embedded_tab_bar_proxies(self) -> None:
        self._prune_embedded_tab_bar_proxies()
        source_tab_bar = self.extensions_tabs.tabBar()
        tab_texts = [
            self._extensions_tab_full_text(source_tab_bar, index)
            for index in range(self.extensions_tabs.count())
        ]

        for index, tab_text in enumerate(tab_texts):
            self._set_extensions_tab_text(source_tab_bar, index, tab_text)

        current_index = self.extensions_tabs.currentIndex()
        for proxy in self._embedded_tab_bar_proxies:
            titlebar_uri_mode = bool(proxy.property("extensions_titlebar_uri_mode"))
            split_role = str(proxy.property("extensions_titlebar_split_role") or "").strip().lower()
            split_slot_index = -1
            raw_split_slot_index = proxy.property("extensions_titlebar_split_slot_index")
            try:
                if raw_split_slot_index is not None and str(raw_split_slot_index).strip() != "":
                    split_slot_index = int(raw_split_slot_index)
            except Exception:
                split_slot_index = -1
            raw_uri_slot_index = proxy.property("extensions_titlebar_uri_slot_index")
            try:
                uri_slot_index = int(raw_uri_slot_index) if raw_uri_slot_index is not None and str(raw_uri_slot_index).strip() != "" else 0
            except Exception:
                uri_slot_index = 0
            proxy_entries: list[int | str] = list(range(len(tab_texts)))
            if bool(proxy.property("extensions_titlebar_proxy_bar")):
                proxy_entries = [
                    source_index
                    for source_index, tab_text in enumerate(tab_texts)
                    if not self._hide_titlebar_proxy_tab_for_text(tab_text)
                ]
                visible_source_indices = [entry for entry in proxy_entries if isinstance(entry, int)]
                if visible_source_indices and current_index not in visible_source_indices:
                    fallback_source_index = int(visible_source_indices[0])
                    if 0 <= fallback_source_index < self.extensions_tabs.count():
                        self.extensions_tabs.setCurrentIndex(fallback_source_index)
                        return
            if titlebar_uri_mode:
                uri_slot_index = max(0, min(uri_slot_index, len(tab_texts)))
                proxy_entries = list(range(uri_slot_index)) + [ExtensionsWorkspaceTabBar.URI_PROXY_DATA] + list(range(uri_slot_index, len(tab_texts)))
            elif split_role in {"left", "right"} and split_slot_index >= 0:
                split_slot_index = max(0, min(split_slot_index, len(tab_texts)))
                if split_role == "left":
                    proxy_entries = list(range(split_slot_index))
                else:
                    proxy_entries = list(range(split_slot_index, len(tab_texts)))
            blocker = QtCore.QSignalBlocker(proxy)
            while proxy.count() > len(proxy_entries):
                proxy.removeTab(proxy.count() - 1)
            for proxy_index, proxy_entry in enumerate(proxy_entries):
                if proxy_index >= proxy.count():
                    proxy.addTab("")
                if proxy_entry == ExtensionsWorkspaceTabBar.URI_PROXY_DATA:
                    self._sync_titlebar_uri_proxy_tab(proxy, proxy_index)
                    continue
                source_index = int(proxy_entry)
                tab_text = tab_texts[source_index]
                self._clear_titlebar_uri_proxy_tab_button(proxy, proxy_index)
                self._set_extensions_tab_text(proxy, proxy_index, tab_text)
                palette = self._tab_palette(self._tab_category_for_text(tab_text))
                proxy.setTabTextColor(proxy_index, QColor(palette["label_fg"]))
                proxy.setTabData(proxy_index, source_index)
                self._set_proxy_tab_close_button(proxy, proxy_index, source_index=source_index)
            try:
                current_proxy_index = proxy_entries.index(current_index)
            except ValueError:
                current_proxy_index = -1
            if 0 <= current_proxy_index < proxy.count():
                proxy.setCurrentIndex(current_proxy_index)
            self._update_hover_close_buttons(proxy, -1)
            proxy.setVisible(bool(proxy_entries))
            del blocker
        self._sync_embedded_tab_bar_visibility()

    def _clear_titlebar_uri_proxy_tab_button(self, proxy_bar: QTabBar, tab_index: int) -> None:
        if tab_index < 0 or tab_index >= proxy_bar.count():
            return
        for side in (QTabBar.LeftSide, QTabBar.RightSide):
            tab_button = proxy_bar.tabButton(tab_index, side)
            if isinstance(tab_button, QWidget) and bool(tab_button.property("extensions_titlebar_uri_container")):
                proxy_bar.setTabButton(tab_index, side, None)

    def _ensure_titlebar_uri_proxy_container(self, proxy_bar: QTabBar) -> QWidget:
        container = getattr(proxy_bar, "_titlebar_uri_proxy_container", None)
        if isinstance(container, QWidget):
            try:
                _ = container.objectName()
                return container
            except RuntimeError:
                pass

        container = QWidget(proxy_bar)
        container.setObjectName("windowFrameExternalUriHost")
        container.setProperty("extensions_titlebar_uri_container", True)
        container.setAttribute(Qt.WA_StyledBackground, True)
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        handle = QToolButton(container)
        handle.setObjectName("windowFrameSectionHandle")
        handle.setText("::")
        handle.setAutoRaise(True)
        handle.setFocusPolicy(Qt.NoFocus)
        handle.setCursor(Qt.SizeHorCursor)
        handle.setToolTip("URI-Tab verschieben")
        handle.setFixedSize(18, 18)
        handle.setProperty("extensions_titlebar_uri_handle", True)
        handle.installEventFilter(self)
        container_layout.addWidget(handle, 0, Qt.AlignVCenter)

        setattr(proxy_bar, "_titlebar_uri_proxy_container", container)
        setattr(proxy_bar, "_titlebar_uri_proxy_container_layout", container_layout)
        setattr(proxy_bar, "_titlebar_uri_proxy_handle", handle)
        setattr(proxy_bar, "_titlebar_uri_proxy_widget", None)
        return container

    def _mount_titlebar_uri_proxy_widget(self, proxy_bar: QTabBar, uri_widget: QWidget | None) -> QWidget:
        container = self._ensure_titlebar_uri_proxy_container(proxy_bar)
        container_layout = getattr(proxy_bar, "_titlebar_uri_proxy_container_layout", None)
        mounted_widget = getattr(proxy_bar, "_titlebar_uri_proxy_widget", None)
        if isinstance(mounted_widget, QWidget) and mounted_widget is not uri_widget and isinstance(container_layout, QHBoxLayout):
            container_layout.removeWidget(mounted_widget)
            mounted_widget.setParent(None)
        setattr(proxy_bar, "_titlebar_uri_proxy_widget", None)
        if isinstance(uri_widget, QWidget) and isinstance(container_layout, QHBoxLayout):
            container_layout.addWidget(uri_widget, 1)
            setattr(proxy_bar, "_titlebar_uri_proxy_widget", uri_widget)
        return container

    def _sync_titlebar_uri_proxy_tab(self, proxy_bar: QTabBar, tab_index: int) -> None:
        if tab_index < 0 or tab_index >= proxy_bar.count():
            return
        uri_widget = getattr(proxy_bar, "_titlebar_uri_proxy_source_widget", None)
        uri_container = self._mount_titlebar_uri_proxy_widget(proxy_bar, uri_widget if isinstance(uri_widget, QWidget) else None)
        proxy_bar.setTabData(tab_index, ExtensionsWorkspaceTabBar.URI_PROXY_DATA)
        proxy_bar.setTabText(tab_index, "")
        proxy_bar.setTabToolTip(tab_index, "Adresseingabe")
        proxy_bar.setTabButton(tab_index, QTabBar.RightSide, None)
        proxy_bar.setTabButton(tab_index, QTabBar.LeftSide, uri_container)

    def _titlebar_uri_real_tab_count_before_proxy_index(self, proxy_bar: QTabBar, proxy_index: int) -> int:
        real_tab_count = 0
        for index in range(max(0, min(proxy_index, proxy_bar.count()))):
            if isinstance(proxy_bar.tabData(index), int):
                real_tab_count += 1
        return real_tab_count

    def _resolve_titlebar_uri_proxy_bar(self, widget: QWidget | None) -> QTabBar | None:
        current_widget = widget
        while isinstance(current_widget, QWidget):
            if isinstance(current_widget, QTabBar) and bool(current_widget.property("extensions_titlebar_uri_mode")):
                return current_widget
            current_widget = current_widget.parentWidget()
        return None

    def _titlebar_uri_proxy_slot_index_from_local_x(self, proxy_bar: QTabBar, local_x: int) -> int:
        slot_index = 0
        real_tab_count = 0
        for index in range(proxy_bar.count()):
            if not isinstance(proxy_bar.tabData(index), int):
                continue
            real_tab_count += 1
            try:
                tab_center_x = int(proxy_bar.tabRect(index).center().x())
            except RuntimeError:
                continue
            if tab_center_x > int(local_x):
                slot_index += 1
        return max(0, min(slot_index, real_tab_count))

    def _apply_titlebar_uri_proxy_slot_index(self, slot_index: int) -> None:
        top_level_window = self.window()
        slot_setter = getattr(top_level_window, "_set_window_titlebar_uri_tab_slot_index", None)
        if callable(slot_setter):
            slot_setter(int(slot_index))

    def create_embedded_tab_bar_proxy(self, parent: QWidget | None = None) -> QWidget:
        proxy_host = QWidget(parent or self)
        proxy_host.setObjectName("extensionsEmbeddedTabBarHost")
        proxy_layout = QHBoxLayout(proxy_host)
        proxy_layout.setContentsMargins(0, 0, 0, 0)
        proxy_layout.setSpacing(4)

        proxy_bar = ExtensionsWorkspaceTabBar(
            proxy_host,
            text_formatter=self._format_extensions_tab_text,
        )
        proxy_bar.setObjectName("extensionsEmbeddedTabBar")
        proxy_bar.setProperty("extensions_embedded_tab_bar", True)
        proxy_bar.setDocumentMode(True)
        proxy_bar.setMovable(True)
        proxy_bar.setTabsClosable(False)
        proxy_bar.setUsesScrollButtons(False)
        proxy_bar.setExpanding(False)
        proxy_bar.setElideMode(Qt.ElideNone)
        proxy_bar.setMouseTracking(True)
        proxy_bar.installEventFilter(self)
        proxy_bar.currentChanged.connect(
            lambda index: self.extensions_tabs.setCurrentIndex(index)
            if 0 <= index < self.extensions_tabs.count()
            else None
        )
        proxy_bar.tabCloseRequested.connect(self._close_extension_tab)

        add_button = QToolButton(proxy_host)
        add_button.setObjectName("extensionsEmbeddedTabAddButton")
        add_button.setIcon(_icon("plus_custombar_24.svg"))
        add_button.setIconSize(QSize(14, 14))
        add_button.setAutoRaise(True)
        add_button.setToolTip("Neue Extension-Verbindung")
        add_button.clicked.connect(lambda _checked=False: self.open_new_connection_tab(activate=True))

        proxy_layout.addWidget(proxy_bar, 1)
        proxy_layout.addWidget(add_button, 0)

        self._embedded_tab_bar_proxies.append(proxy_bar)
        proxy_bar.destroyed.connect(lambda *_args: self._sync_embedded_tab_bar_visibility())
        self._sync_embedded_tab_bar_proxies()
        return proxy_host

    def create_titlebar_tab_bar_proxy(self, parent: QWidget | None = None, *, split_role: str = "right") -> QWidget:
        proxy_host = QWidget(parent or self)
        proxy_host.setObjectName("windowFrameExtensionsTabProxyHost")
        proxy_layout = QHBoxLayout(proxy_host)
        proxy_layout.setContentsMargins(0, 0, 0, 0)
        proxy_layout.setSpacing(0)
        titlebar_tab_height = max(16, int(CustomWindowTitleBar._HEIGHT) - 6)

        proxy_bar = ExtensionsWorkspaceTabBar(
            proxy_host,
            text_formatter=self._format_extensions_tab_text,
            tab_row_height=titlebar_tab_height,
        )
        proxy_bar.setObjectName(_WINDOW_FRAME_EMBEDDED_TAB_BAR_OBJECT_NAME)
        proxy_bar.setProperty("extensions_embedded_tab_bar", True)
        proxy_bar.setProperty("extensions_titlebar_proxy_bar", True)
        proxy_bar.setProperty("extensions_titlebar_uri_mode", True)
        proxy_bar.setProperty("extensions_titlebar_uri_fill", str(self.scheme.get("col5") or "#101010"))
        proxy_bar.setProperty("extensions_titlebar_uri_slot_index", 0)
        proxy_bar.setDocumentMode(True)
        proxy_bar.setMovable(True)
        proxy_bar.setTabsClosable(False)
        proxy_bar.setUsesScrollButtons(False)
        proxy_bar.setExpanding(False)
        proxy_bar.setElideMode(Qt.ElideNone)
        proxy_bar.setMouseTracking(True)
        proxy_bar.setMinimumHeight(titlebar_tab_height)
        proxy_bar.setMaximumHeight(titlebar_tab_height)
        proxy_bar.installEventFilter(self)
        proxy_bar.currentChanged.connect(
            lambda index, bar=proxy_bar: self.extensions_tabs.setCurrentIndex(
                int(bar.tabData(index))
            )
            if 0 <= index < bar.count() and isinstance(bar.tabData(index), int) and 0 <= int(bar.tabData(index)) < self.extensions_tabs.count()
            else None
        )
        proxy_bar.tabMoved.connect(self._handle_extensions_tab_moved)
        proxy_layout.addWidget(proxy_bar, 1)

        self._embedded_tab_bar_proxies.append(proxy_bar)
        proxy_bar.destroyed.connect(lambda *_args: self._sync_embedded_tab_bar_visibility())
        self._sync_embedded_tab_bar_proxies()
        return proxy_host

    def _sync_external_uri_proxies(self) -> None:
        self._prune_external_uri_proxies()
        try:
            current_uri = self.current_widget_uri()
        except RuntimeError:
            return
        for proxy in self._external_uri_proxies:
            blocker = QtCore.QSignalBlocker(proxy)
            proxy.setText(current_uri)
            del blocker

    def _active_session_selection_menu(self) -> QMenu | None:
        active_state = self._active_session_state()
        if not isinstance(active_state, dict):
            return None
        selection_menu = active_state.get("selection_menu")
        if isinstance(selection_menu, QMenu):
            return selection_menu
        return None

    def _popup_active_session_selection_menu(self, anchor_widget: QWidget) -> bool:
        if not isinstance(anchor_widget, QWidget):
            return False
        selection_menu = self._active_session_selection_menu()
        if not isinstance(selection_menu, QMenu):
            return False
        if not selection_menu.actions():
            return False
        try:
            popup_point = anchor_widget.mapToGlobal(QPoint(0, anchor_widget.height()))
        except RuntimeError:
            return False
        selection_menu.popup(popup_point)
        return True

    def _schedule_connection_preview_refresh(self, session_state: dict[str, Any]) -> None:
        if not isinstance(session_state, dict):
            return
        self._pending_preview_session_state = session_state
        self._preview_refresh_timer.start()

    def _flush_scheduled_preview_refresh(self) -> None:
        session_state = self._pending_preview_session_state
        self._pending_preview_session_state = None
        if not isinstance(session_state, dict):
            return
        tab_widget = session_state.get("tab_widget")
        if not isinstance(tab_widget, QWidget):
            return
        if self._session_state_by_tab.get(tab_widget) is not session_state:
            return
        self._refresh_connection_preview(session_state)

    def _set_active_session_uri(self, source_uri: str) -> None:
        active_state = self._active_session_state()
        if not isinstance(active_state, dict):
            return
        uri_input = active_state.get("uri_input")
        if not isinstance(uri_input, QLineEdit):
            return
        if str(uri_input.text() or "") == str(source_uri or ""):
            return
        uri_input.setText(str(source_uri or ""))

    def _load_active_session_widget(self) -> None:
        active_state = self._active_session_state()
        if isinstance(active_state, dict):
            self._load_extension_into_session(active_state, fit_view=True)

    def create_external_uri_proxy(self, parent: QWidget | None = None) -> QLineEdit:
        proxy_input = ExtensionsExternalUriLineEdit(parent or self)
        proxy_input.setObjectName("extensionsGraphUriInput")
        proxy_input.setProperty("extensions_external_uri_proxy", True)
        proxy_input.setLayoutDirection(Qt.LeftToRight)
        proxy_input.setAttribute(Qt.WA_StyledBackground, True)
        proxy_input.setAutoFillBackground(True)
        proxy_input.setMinimumHeight(28)
        proxy_input.setPlaceholderText("agentsdb://127.0.0.1:2331/tools:graph_view")
        load_icon_idle = _icon_with_opacity("load_content.svg", opacity=0.72)
        add_tab_icon_idle = _icon_with_opacity("plus_custombar_24.svg", opacity=0.72)
        load_action = proxy_input.addAction(load_icon_idle, QLineEdit.TrailingPosition)
        load_action.setToolTip("Widget laden")
        add_tab_action = proxy_input.addAction(add_tab_icon_idle, QLineEdit.TrailingPosition)
        add_tab_action.setToolTip("Neue Extension-Verbindung")
        proxy_input.menuRequested.connect(lambda: self._popup_active_session_selection_menu(proxy_input))
        proxy_input.textEdited.connect(self._set_active_session_uri)
        proxy_input.returnPressed.connect(self._load_active_session_widget)
        load_action.triggered.connect(self._load_active_session_widget)
        add_tab_action.triggered.connect(lambda: self.open_new_connection_tab(activate=True))
        self._external_uri_proxies.append(proxy_input)
        proxy_input.destroyed.connect(lambda *_args: self._sync_external_uri_proxies())
        self._sync_external_uri_proxies()
        return proxy_input

    def _start_tab_hover_marquee(self, tab_bar: QTabBar, tab_index: int) -> None:
        self._stop_tab_hover_marquee()
        if not self._is_extensions_tab_bar(tab_bar):
            return
        if tab_index < 0 or tab_index >= tab_bar.count():
            self._stop_tab_hover_marquee()
            return
        if tab_index == self._hover_tab_index and tab_bar is self._hover_tab_bar:
            return

        self._stop_tab_hover_marquee()

        base_text = self._extensions_tab_full_text(tab_bar, tab_index)
        if not base_text:
            return

        tab_rect = tab_bar.tabRect(tab_index)
        close_button = tab_bar.tabButton(tab_index, QTabBar.RightSide)
        close_button_width = close_button.width() if isinstance(close_button, QToolButton) and close_button.isVisible() else 0
        available_width = max(tab_rect.width() - 20 - close_button_width, 18)
        text_width = tab_bar.fontMetrics().horizontalAdvance(base_text)
        if text_width <= available_width:
            return

        self._hover_tab_bar = tab_bar
        self._hover_tab_index = tab_index
        self._hover_tab_base_text = base_text
        self._hover_tab_phase = 0
        self._hover_tab_marquee_timer.start()

    def _tick_tab_hover_marquee(self) -> None:
        tab_bar = self._hover_tab_bar
        if not isinstance(tab_bar, QTabBar):
            self._stop_tab_hover_marquee()
            return
        tab_index = self._hover_tab_index
        try:
            tab_count = tab_bar.count()
        except RuntimeError:
            self._stop_tab_hover_marquee()
            return
        if tab_index < 0 or tab_index >= tab_count:
            self._stop_tab_hover_marquee()
            return
        if not self._hover_tab_base_text:
            self._stop_tab_hover_marquee()
            return

        cycle_text = f"{self._hover_tab_base_text}   "
        if len(cycle_text) <= 1:
            return

        self._hover_tab_phase = (self._hover_tab_phase + 1) % len(cycle_text)
        shift = self._hover_tab_phase
        tab_bar.setTabText(tab_index, f"{cycle_text[shift:]}{cycle_text[:shift]}")

    def _stop_tab_hover_marquee(self) -> None:
        if self._hover_tab_marquee_timer.isActive():
            self._hover_tab_marquee_timer.stop()

        tab_bar = self._hover_tab_bar
        tab_index = self._hover_tab_index
        if tab_index >= 0 and self._hover_tab_base_text and isinstance(tab_bar, QTabBar):
            try:
                if tab_index < tab_bar.count():
                    self._set_extensions_tab_text(tab_bar, tab_index, self._hover_tab_base_text)
            except RuntimeError:
                pass

        self._hover_tab_bar = None
        self._hover_tab_index = -1
        self._hover_tab_base_text = ""
        self._hover_tab_phase = 0

    def eventFilter(self, obj, event):  # noqa: N802
        if isinstance(obj, QToolButton) and bool(obj.property("extensions_titlebar_uri_handle")):
            event_type = event.type()
            if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                proxy_bar = self._resolve_titlebar_uri_proxy_bar(obj)
                if isinstance(proxy_bar, QTabBar):
                    self._titlebar_uri_drag_handle = obj
                    self._titlebar_uri_drag_proxy_bar = proxy_bar
                    self._titlebar_uri_drag_active = True
                    self._titlebar_uri_drag_moved = False
                    event.accept()
                    return True
            elif event_type == QEvent.MouseMove and self._titlebar_uri_drag_active and self._titlebar_uri_drag_handle is obj and (event.buttons() & Qt.LeftButton):
                proxy_bar = self._titlebar_uri_drag_proxy_bar
                if isinstance(proxy_bar, QTabBar):
                    self._titlebar_uri_drag_moved = True
                    global_point = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
                    local_point = proxy_bar.mapFromGlobal(global_point)
                    slot_index = self._titlebar_uri_proxy_slot_index_from_local_x(proxy_bar, local_point.x())
                    self._apply_titlebar_uri_proxy_slot_index(slot_index)
                    event.accept()
                    return True
            elif event_type == QEvent.MouseButtonRelease and self._titlebar_uri_drag_active and self._titlebar_uri_drag_handle is obj and event.button() == Qt.LeftButton:
                self._titlebar_uri_drag_handle = None
                self._titlebar_uri_drag_proxy_bar = None
                self._titlebar_uri_drag_active = False
                self._titlebar_uri_drag_moved = False
                event.accept()
                return True
        if hasattr(self, "extensions_tabs") and isinstance(obj, QTabBar):
            event_type = event.type()
            if event_type == QEvent.MouseMove:
                if hasattr(event, "position"):
                    pos = event.position().toPoint()
                else:
                    pos = event.pos()
                hover_index = obj.tabAt(pos)
                self._update_hover_close_buttons(obj, hover_index)
                if self._is_extensions_tab_bar(obj):
                    self._start_tab_hover_marquee(obj, hover_index)
            elif event_type in (QEvent.Leave, QEvent.MouseButtonPress):
                self._update_hover_close_buttons(obj, -1)
                if obj is self._hover_tab_bar:
                    self._stop_tab_hover_marquee()
        return super().eventFilter(obj, event)

    def _add_control_plane_tab(self) -> None:
        """Integrate ControlPlaneWidget's tabs directly into the main tab row."""
        if self._control_plane_widget is None:
            return
        
        # Get reference to ControlPlaneWidget's internal tabs
        internal_tabs = getattr(self._control_plane_widget, "tabs", None)
        
        if internal_tabs is None or not isinstance(internal_tabs, QTabWidget):
            return
         
        # Copy the Control Plane tabs in their existing visual order.
        tabs_to_copy = []
        for i in range(internal_tabs.count()):
            tab_widget = internal_tabs.widget(i)
            tab_text = internal_tabs.tabText(i)
            if tab_widget is not None:
                tabs_to_copy.append((tab_widget, tab_text))
        
        # Preserve the original order so the primary board remains the first board tab.
        for tab_widget, tab_text in tabs_to_copy:
            tab_index = self.extensions_tabs.addTab(tab_widget, tab_text)
            self._set_extensions_tab_text(self.extensions_tabs.tabBar(), tab_index, tab_text)
            self.extensions_tabs.tabBar().setTabButton(tab_index, QTabBar.RightSide, None)
        
        # Clear the internal tabs (they're now managed by extensions_tabs)
        while internal_tabs.count() > 0:
            internal_tabs.removeTab(0)
        
        # Tell ControlPlaneWidget to use extensions_tabs for all future operations
        if hasattr(self._control_plane_widget, "set_external_tabs"):
            self._control_plane_widget.set_external_tabs(self.extensions_tabs)
        
        # Initialize the control plane widget
        if hasattr(self._control_plane_widget, "refresh_view"):
            self._control_plane_widget.refresh_view()
        self._sync_embedded_tab_bar_proxies()

    def open_new_connection_tab(self, *, activate: bool = True) -> None:
        self._add_extension_tab(activate=activate)

    def _close_extension_tab_for_widget(self, tab_widget: QWidget) -> None:
        tab_index = self.extensions_tabs.indexOf(tab_widget)
        if tab_index >= 0:
            self._close_extension_tab(tab_index)

    def _is_extension_tab_closable(self, tab_index: int) -> bool:
        tab_widget = self.extensions_tabs.widget(tab_index)
        if not isinstance(tab_widget, QWidget):
            return False
        session_state = self._session_state_by_tab.get(tab_widget)
        if isinstance(session_state, dict) and bool(session_state.get("catalog_only")):
            return False
        return True

    def _new_extension_tab_close_button(
        self,
        parent: QWidget,
        *,
        object_name: str,
        target_widget: QWidget,
    ) -> QToolButton:
        close_button = QToolButton(parent)
        close_button.setObjectName(object_name)
        close_button.setProperty("extensions_close_button", True)
        close_button.setText("x")
        close_button.setToolTip("Extension-Verbindung schließen")
        close_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setAutoRaise(True)
        close_button.setFixedSize(0, 0)
        close_button.setVisible(False)
        close_button.clicked.connect(
            lambda _checked=False, target=target_widget: self._close_extension_tab_for_widget(target)
        )
        return close_button

    def _update_hover_close_buttons(self, tab_bar: QTabBar, hover_index: int) -> None:
        force_visible = bool(tab_bar.property("extensions_titlebar_proxy_bar"))
        try:
            source_tab_bar = self.extensions_tabs.tabBar()
        except RuntimeError:
            source_tab_bar = None
        for index in range(tab_bar.count()):
            close_button = tab_bar.tabButton(index, QTabBar.RightSide)
            if isinstance(close_button, QToolButton) and bool(close_button.property("extensions_close_button")):
                if isinstance(source_tab_bar, QTabBar) and tab_bar is source_tab_bar:
                    resolved_source_index = index
                else:
                    tab_data = tab_bar.tabData(index)
                    resolved_source_index = int(tab_data) if isinstance(tab_data, int) else -1
                is_closable = resolved_source_index >= 0 and self._is_extension_tab_closable(resolved_source_index)
                show_button = bool(is_closable and (force_visible or index == hover_index))
                close_button.setFixedSize(16, 16) if show_button else close_button.setFixedSize(0, 0)
                close_button.setVisible(show_button)

    def _set_tab_close_button(self, tab_widget: QWidget) -> None:
        tab_index = self.extensions_tabs.indexOf(tab_widget)
        if tab_index < 0:
            return

        session_state = self._session_state_by_tab.get(tab_widget)
        if isinstance(session_state, dict) and bool(session_state.get("catalog_only")):
            self.extensions_tabs.tabBar().setTabButton(tab_index, QTabBar.RightSide, None)
            return

        tab_bar = self.extensions_tabs.tabBar()
        existing_button = tab_bar.tabButton(tab_index, QTabBar.RightSide)
        if isinstance(existing_button, QToolButton) and bool(existing_button.property("extensions_close_button")):
            return

        close_button = self._new_extension_tab_close_button(
            tab_bar,
            object_name="extensionsTabCloseButton",
            target_widget=tab_widget,
        )
        tab_bar.setTabButton(tab_index, QTabBar.RightSide, close_button)

    def _set_proxy_tab_close_button(self, proxy_bar: QTabBar, tab_index: int, *, source_index: int | None = None) -> None:
        if tab_index < 0 or tab_index >= proxy_bar.count():
            return
        resolved_source_index = tab_index if source_index is None else int(source_index)
        if not self._is_extension_tab_closable(resolved_source_index):
            proxy_bar.setTabButton(tab_index, QTabBar.RightSide, None)
            return
        existing_button = proxy_bar.tabButton(tab_index, QTabBar.RightSide)
        if isinstance(existing_button, QToolButton) and bool(existing_button.property("extensions_close_button")):
            return
        target_widget = self.extensions_tabs.widget(resolved_source_index)
        if not isinstance(target_widget, QWidget):
            return
        close_button = self._new_extension_tab_close_button(
            proxy_bar,
            object_name="extensionsEmbeddedTabCloseButton",
            target_widget=target_widget,
        )
        proxy_bar.setTabButton(tab_index, QTabBar.RightSide, close_button)

    def _sync_tab_close_buttons(self) -> None:
        source_tab_bar = self.extensions_tabs.tabBar()
        for tab_index in range(self.extensions_tabs.count()):
            full_text = self._extensions_tab_full_text(source_tab_bar, tab_index)
            palette = self._tab_palette(
                self._tab_category_for_text(full_text)
            )
            source_tab_bar.setTabTextColor(tab_index, QColor(palette["label_fg"]))
            tab_widget = self.extensions_tabs.widget(tab_index)
            if isinstance(tab_widget, QWidget):
                self._set_tab_close_button(tab_widget)
        self._update_hover_close_buttons(source_tab_bar, -1)

    def _add_extension_tab(
        self,
        *,
        source_uri: str = "",
        tool_id: str = "",
        keep_local: bool = True,
        catalog_only: bool = False,
        activate: bool = True,
        session_token: str = "",
    ) -> dict[str, Any]:
        self._session_counter += 1
        session_id = self._session_counter

        tab_widget = QWidget(self.extensions_tabs)
        tab_layout = QVBoxLayout(tab_widget)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        session_stack = QStackedWidget(tab_widget)
        tab_layout.addWidget(session_stack, 1)

        session_state: dict[str, Any] = {
            "session_id": session_id,
            "session_token": str(session_token or uuid.uuid4().hex).strip() or uuid.uuid4().hex,
            "tab_widget": tab_widget,
            "stack": session_stack,
            "catalog_only": bool(catalog_only),
            "loaded": False,
            "loaded_widget": None,
        }

        control_page = self._build_session_control_page(
            session_state,
            source_uri=str(source_uri or "").strip(),
            tool_id=str(tool_id or "agent_relation_graph").strip() or "agent_relation_graph",
            keep_local=bool(keep_local),
        )
        host_page = self._build_session_host_page(session_state)

        session_state["control_page"] = control_page
        session_state["host_page"] = host_page

        session_stack.addWidget(control_page)
        session_stack.addWidget(host_page)
        session_stack.setCurrentWidget(control_page)

        self._session_state_by_tab[tab_widget] = session_state
        default_title = self._START_TAB_TITLE if bool(catalog_only) else f"Connection {session_id}"
        tab_index = self.extensions_tabs.addTab(tab_widget, default_title)
        self._set_extensions_tab_text(self.extensions_tabs.tabBar(), tab_index, default_title)
        self._set_tab_close_button(tab_widget)

        self._refresh_connection_preview(session_state)
        if activate:
            self.extensions_tabs.setCurrentIndex(tab_index)
        self._sync_embedded_tab_bar_proxies()
        self._sync_external_uri_proxies()
        return session_state

    def _close_extension_tab(self, tab_index: int) -> None:
        self._stop_tab_hover_marquee()
        tab_widget = self.extensions_tabs.widget(tab_index)
        if tab_widget is None:
            return

        control_plane = getattr(self, "_control_plane_widget", None)
        if control_plane is not None:
            primary_board = getattr(control_plane, "_config_tab", None)
            if tab_widget is primary_board:
                return

            runtime_records = getattr(control_plane, "_runtime_tab_records", None)
            close_runtime = getattr(control_plane, "_close_runtime_tab", None)
            if isinstance(runtime_records, dict) and tab_widget in runtime_records and callable(close_runtime):
                close_runtime(tab_widget)
                self.widgetStateChanged.emit()
                return

            unregister_board = getattr(control_plane, "_unregister_board_context", None)
            board_contexts = getattr(control_plane, "_board_context_by_tab", None)
            if isinstance(board_contexts, dict) and tab_widget in board_contexts and callable(unregister_board):
                unregister_board(tab_widget)

        session_state = self._session_state_by_tab.get(tab_widget)
        if isinstance(session_state, dict) and bool(session_state.get("catalog_only")):
            return

        if self.extensions_tabs.count() <= 1:
            if not isinstance(session_state, dict):
                return
            stack_widget = session_state.get("stack")
            control_page = session_state.get("control_page")
            if isinstance(stack_widget, QStackedWidget) and isinstance(control_page, QWidget):
                stack_widget.setCurrentWidget(control_page)
            self._set_session_content_widget(session_state, None)
            session_state["loaded"] = False
            session_state["loaded_widget"] = None
            self._refresh_connection_preview(session_state)
            self._set_tab_close_button(tab_widget)
            self._sync_embedded_tab_bar_proxies()
            self._sync_external_uri_proxies()
            self._persist_local_widget_state(session_state)
            return

        session_state = self._session_state_by_tab.pop(tab_widget, None)
        if isinstance(session_state, dict):
            self._set_session_content_widget(session_state, None)
            session_state.clear()
        self.extensions_tabs.removeTab(tab_index)
        self._sync_tab_close_buttons()
        self._sync_embedded_tab_bar_proxies()
        self._sync_external_uri_proxies()
        self._persist_local_widget_state()
        tab_widget.deleteLater()
        self.widgetStateChanged.emit()

    def _active_session_state(self) -> dict[str, Any] | None:
        try:
            current_widget = self.extensions_tabs.currentWidget()
        except RuntimeError:
            return None
        if current_widget is None:
            return None
        session_state = self._session_state_by_tab.get(current_widget)
        if isinstance(session_state, dict):
            return session_state
        return None

    def _handle_active_tab_changed(self, _tab_index: int) -> None:
        self._stop_tab_hover_marquee()
        self.setProperty("runtime_source_path", self.current_widget_uri())
        self._sync_embedded_tab_bar_proxies()
        self._sync_external_uri_proxies()
        self._persist_local_widget_state()
        self.widgetStateChanged.emit()

    def _move_extension_tab_owner_index(self, from_index: int, to_index: int) -> bool:
        try:
            total_tabs = int(self.extensions_tabs.count())
            source_tab_bar = self.extensions_tabs.tabBar()
        except RuntimeError:
            return False
        if total_tabs <= 1:
            return False
        if from_index < 0 or from_index >= total_tabs:
            return False
        resolved_to_index = max(0, min(int(to_index), total_tabs - 1))
        if from_index == resolved_to_index:
            return False

        moved_widget = self.extensions_tabs.widget(from_index)
        if not isinstance(moved_widget, QWidget):
            return False

        current_widget = self.extensions_tabs.currentWidget()
        full_text = self._extensions_tab_full_text(source_tab_bar, from_index)
        tab_icon = self.extensions_tabs.tabIcon(from_index)
        is_enabled = self.extensions_tabs.isTabEnabled(from_index)

        tabs_blocker = QtCore.QSignalBlocker(self.extensions_tabs)
        tab_bar_blocker = QtCore.QSignalBlocker(source_tab_bar)
        self.extensions_tabs.removeTab(from_index)
        inserted_index = self.extensions_tabs.insertTab(resolved_to_index, moved_widget, tab_icon, "")
        self._set_extensions_tab_text(source_tab_bar, inserted_index, full_text)
        self.extensions_tabs.setTabEnabled(inserted_index, is_enabled)
        if isinstance(current_widget, QWidget):
            self.extensions_tabs.setCurrentWidget(current_widget)
        del tab_bar_blocker
        del tabs_blocker
        return True

    def _resolved_proxy_move_indexes(self, proxy_bar: QTabBar, proxy_to_index: int) -> tuple[int, int] | None:
        if proxy_to_index < 0 or proxy_to_index >= proxy_bar.count():
            return None

        moved_source_index = proxy_bar.tabData(proxy_to_index)
        if not isinstance(moved_source_index, int):
            return None

        visible_source_order: list[int] = []
        for proxy_index in range(proxy_bar.count()):
            tab_data = proxy_bar.tabData(proxy_index)
            if isinstance(tab_data, int):
                visible_source_order.append(int(tab_data))

        try:
            total_tabs = int(self.extensions_tabs.count())
        except RuntimeError:
            total_tabs = 0
        if total_tabs <= 0:
            return None

        visible_source_set = set(visible_source_order)
        source_order = list(range(total_tabs))
        visible_positions = [
            source_position
            for source_position, source_index in enumerate(source_order)
            if source_index in visible_source_set
        ]
        if not visible_positions:
            return int(moved_source_index), int(proxy_to_index)

        desired_source_order = list(source_order)
        for source_position, source_index in zip(visible_positions, visible_source_order):
            desired_source_order[source_position] = int(source_index)

        try:
            target_source_index = int(desired_source_order.index(int(moved_source_index)))
        except ValueError:
            return int(moved_source_index), int(proxy_to_index)

        return int(moved_source_index), int(target_source_index)

    def _handle_extensions_tab_moved(self, _from_index: int, _to_index: int) -> None:
        sender_object = self.sender()
        try:
            source_tab_bar = self.extensions_tabs.tabBar()
        except RuntimeError:
            source_tab_bar = None
        if isinstance(sender_object, QTabBar) and sender_object is not source_tab_bar:
            if bool(sender_object.property("extensions_titlebar_uri_mode")):
                moved_tab_data = sender_object.tabData(_to_index) if 0 <= _to_index < sender_object.count() else None
                if moved_tab_data == ExtensionsWorkspaceTabBar.URI_PROXY_DATA:
                    self._apply_titlebar_uri_proxy_slot_index(
                        self._titlebar_uri_real_tab_count_before_proxy_index(sender_object, _to_index)
                    )
                elif isinstance(moved_tab_data, int):
                    self._move_extension_tab_owner_index(
                        int(moved_tab_data),
                        self._titlebar_uri_real_tab_count_before_proxy_index(sender_object, _to_index),
                    )
            else:
                resolved_move_indexes = self._resolved_proxy_move_indexes(sender_object, _to_index)
                if resolved_move_indexes is not None:
                    self._move_extension_tab_owner_index(*resolved_move_indexes)
        self._stop_tab_hover_marquee()
        self._sync_tab_close_buttons()
        self._sync_embedded_tab_bar_proxies()
        self._sync_external_uri_proxies()
        self._persist_local_widget_state()
        self.widgetStateChanged.emit()

    def _build_session_control_page(
        self,
        session_state: dict[str, Any],
        *,
        source_uri: str,
        tool_id: str,
        keep_local: bool,
    ) -> QWidget:
        control_page = QWidget(self)
        control_page.setObjectName("extensionsSessionControlPage")
        control_layout = QVBoxLayout(control_page)
        control_layout.setContentsMargins(14, 14, 14, 14)
        control_layout.setSpacing(10)
        control_layout.setAlignment(Qt.AlignTop)

        uri_input = QLineEdit(control_page)
        uri_input.setObjectName("extensionsGraphUriInput")
        uri_input.setMinimumHeight(30)
        uri_input.setPlaceholderText("agentsdb://127.0.0.1:2331/tools:graph_view")
        uri_input.setText(str(source_uri or "agentsdb://127.0.0.1:2331/tools:graph_view"))
        uri_input.hide()
        load_icon_idle = _icon_with_opacity("load_content.svg", opacity=0.72)
        load_icon_active = _icon_with_opacity("load_content.svg", opacity=1.0)
        load_action = uri_input.addAction(load_icon_idle, QLineEdit.TrailingPosition)
        load_action.setObjectName("extensionsGraphLoadAction")
        load_action.setToolTip("Widget laden")
        add_tab_icon_idle = _icon_with_opacity("plus_custombar_24.svg", opacity=0.72)
        add_tab_action = uri_input.addAction(add_tab_icon_idle, QLineEdit.TrailingPosition)
        add_tab_action.setObjectName("extensionsGraphAddTabAction")
        add_tab_action.setToolTip("Neue Extension-Verbindung")

        selection_menu = QMenu(control_page)
        selection_menu.setObjectName("extensionsGraphSelectionMenu")
        selection_menu.setToolTipsVisible(True)

        class _UriMenuEventFilter(QObject):
            def __init__(self, menu: QMenu, parent: QObject) -> None:
                super().__init__(parent)
                self._menu = menu

            def eventFilter(self, obj, event):  # noqa: N802
                if obj is uri_input and event.type() == QEvent.MouseButtonPress:
                    self._menu.popup(uri_input.mapToGlobal(QPoint(0, uri_input.height())))
                return super().eventFilter(obj, event)

        uri_filter = _UriMenuEventFilter(selection_menu, uri_input)
        uri_input.installEventFilter(uri_filter)
        session_state["requested_tool_id"] = str(tool_id or "").strip()
        control_layout.addStretch(1)

        session_state["uri_input"] = uri_input
        session_state["load_action"] = load_action
        session_state["add_tab_action"] = add_tab_action
        session_state["selection_menu"] = selection_menu
        session_state["uri_input_filter"] = uri_filter
        session_state["keep_local"] = bool(keep_local)

        def _apply_selector_choice(triggered_action: QAction | None) -> None:
            selected_payload = triggered_action.data() if triggered_action is not None else None
            if isinstance(selected_payload, dict):
                selected_uri = str(
                    selected_payload.get("uri")
                    or selected_payload.get("source_uri")
                    or uri_input.text()
                    or ""
                ).strip()
                requested_tool_id = str(selected_payload.get("tool_id") or "").strip()
                blocker = QtCore.QSignalBlocker(uri_input)
                uri_input.setText(selected_uri)
                del blocker
                session_state["selected_tool_payload"] = dict(selected_payload)
                session_state["requested_tool_id"] = requested_tool_id
            self._handle_session_control_change(session_state, refresh_preview=False)
            self._refresh_connection_preview(session_state)

        uri_input.textChanged.connect(
            lambda _text: self._handle_session_control_change(session_state, refresh_preview=True)
        )
        selection_menu.triggered.connect(_apply_selector_choice)
        load_action.triggered.connect(lambda: self._load_extension_into_session(session_state, fit_view=True))
        add_tab_action.triggered.connect(lambda: self.open_new_connection_tab(activate=True))

        return control_page

    def _build_session_host_page(self, session_state: dict[str, Any]) -> QWidget:
        host_page = QWidget(self)
        host_page.setObjectName("extensionsSessionHostPage")
        host_layout = QVBoxLayout(host_page)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(0)

        host_container = QWidget(host_page)
        host_container_layout = QVBoxLayout(host_container)
        host_container_layout.setContentsMargins(0, 0, 0, 0)
        host_container_layout.setSpacing(0)

        host_placeholder = QLabel("Load an extension widget from the control plane.", host_container)
        host_placeholder.setObjectName("extensionsHostPlaceholder")
        host_placeholder.setAlignment(Qt.AlignCenter)
        host_placeholder.setWordWrap(True)
        host_container_layout.addWidget(host_placeholder, 1)

        host_layout.addWidget(host_container, 1)

        session_state["host_container"] = host_container
        session_state["host_container_layout"] = host_container_layout
        session_state["host_placeholder"] = host_placeholder

        return host_page

    def _set_session_content_widget(self, session_state: Mapping[str, Any], widget: QWidget | None) -> None:
        host_layout = session_state.get("host_container_layout")
        if not isinstance(host_layout, QVBoxLayout):
            return

        while host_layout.count() > 0:
            child_item = host_layout.takeAt(0)
            child_widget = child_item.widget()
            if isinstance(child_widget, QWidget):
                child_widget.setParent(None)
                child_widget.deleteLater()

        if isinstance(widget, QWidget):
            host_layout.addWidget(widget, 1)

    def _session_source_uri(self, session_state: Mapping[str, Any]) -> str:
        uri_input = session_state.get("uri_input")
        if isinstance(uri_input, QLineEdit):
            return str(uri_input.text() or "").strip()
        return ""

    def _apply_session_source_uri_preset(
        self,
        session_state: dict[str, Any],
        *,
        mode_value: str,
        include_embeddings: bool,
    ) -> None:
        uri_input = session_state.get("uri_input")
        if not isinstance(uri_input, QLineEdit):
            return
        base_uri = str(uri_input.text() or "").strip()
        if not base_uri:
            base_uri = "agentsdb://127.0.0.1:2331/tools:graph_view"

        resolved_uri = self._build_session_source_uri_with_mode(
            base_uri,
            mode_value=mode_value,
            include_embeddings=include_embeddings,
        )
        blocker = QtCore.QSignalBlocker(uri_input)
        uri_input.setText(resolved_uri)
        del blocker

        self._handle_session_control_change(session_state, refresh_preview=False)
        self._refresh_connection_preview(session_state)

    def _build_session_source_uri_with_mode(
        self,
        source_uri: str,
        *,
        mode_value: str,
        include_embeddings: bool,
    ) -> str:
        uri_text = str(source_uri or "").strip()
        if not uri_text:
            uri_text = "agentsdb://127.0.0.1:2331/tools:graph_view"

        parsed_uri = urlparse(uri_text)
        query_pairs = [(str(key), str(value)) for key, value in parse_qsl(str(parsed_uri.query or ""), keep_blank_values=False)]
        preserved_pairs = [(key, value) for key, value in query_pairs if key not in {"mode", "view", "embeddings"}]
        preserved_pairs.append(("mode", str(mode_value or "relations")))
        if str(mode_value or "").strip().lower() == "catalog":
            preserved_pairs.append(("embeddings", "1" if include_embeddings else "0"))

        rebuilt_query = urlencode(preserved_pairs, doseq=True)
        rebuilt_uri = urlunparse(
            (
                parsed_uri.scheme,
                parsed_uri.netloc,
                parsed_uri.path,
                parsed_uri.params,
                rebuilt_query,
                parsed_uri.fragment,
            )
        )
        return rebuilt_uri

    def _session_tool_id(self, session_state: Mapping[str, Any]) -> str:
        selected_payload = session_state.get("selected_tool_payload")
        if isinstance(selected_payload, dict):
            return str(selected_payload.get("tool_id") or "").strip()
        return ""

    def _session_tool_label(self, session_state: Mapping[str, Any]) -> str:
        selected_payload = session_state.get("selected_tool_payload")
        if isinstance(selected_payload, dict):
            return str(selected_payload.get("label") or selected_payload.get("tool_id") or "Extension")
        return str(self._session_tool_id(session_state) or "Extension")

    def _runtime_artifact_payload_from_tool_row(self, tool_row: Mapping[str, Any]) -> dict[str, Any]:
        runtime_artifact = tool_row.get("runtime_artifact")
        if isinstance(runtime_artifact, Mapping):
            return dict(runtime_artifact)
        runtime_manifest = tool_row.get("runtime_manifest")
        if isinstance(runtime_manifest, Mapping):
            manifest_runtime_artifact = runtime_manifest.get("runtime_artifact")
            if isinstance(manifest_runtime_artifact, Mapping):
                return dict(manifest_runtime_artifact)
        return {}

    def _is_tool_row_loadable(self, tool_row: Mapping[str, Any]) -> bool:
        tool_id = str(tool_row.get("tool_id") or "").strip()
        if not tool_id:
            return False
        runtime_artifact = self._runtime_artifact_payload_from_tool_row(tool_row)
        entry_module = str(runtime_artifact.get("entry_module") or "").strip()
        entry_class = str(runtime_artifact.get("entry_class") or "").strip()
        return bool(entry_module and entry_class)

    def _set_session_tab_title(self, session_state: Mapping[str, Any], *, loaded: bool) -> None:
        tab_widget = session_state.get("tab_widget")
        if not isinstance(tab_widget, QWidget):
            return
        tab_index = self.extensions_tabs.indexOf(tab_widget)
        if tab_index < 0:
            return
        if bool(session_state.get("catalog_only")) and not loaded:
            if tab_index == self._hover_tab_index:
                self._stop_tab_hover_marquee()
            self._set_extensions_tab_text(self.extensions_tabs.tabBar(), tab_index, self._START_TAB_TITLE)
            self._set_tab_close_button(tab_widget)
            self._sync_tab_close_buttons()
            self._sync_embedded_tab_bar_proxies()
            return
        session_id = int(session_state.get("session_id") or 0)
        if tab_index == self._hover_tab_index:
            self._stop_tab_hover_marquee()
        if loaded:
            tool_label = self._session_tool_label(session_state)
            self._set_extensions_tab_text(self.extensions_tabs.tabBar(), tab_index, f"{tool_label} #{session_id}")
        else:
            self._set_extensions_tab_text(self.extensions_tabs.tabBar(), tab_index, f"Connection {session_id}")
        self._set_tab_close_button(tab_widget)
        self._sync_tab_close_buttons()
        self._sync_embedded_tab_bar_proxies()

    def _refresh_connection_preview(self, session_state: dict[str, Any]) -> None:
        source_uri = self._session_source_uri(session_state)
        preview_payload = self._backend_service.load_connection_preview(source_uri=source_uri)

        selected_tool_id = self._session_tool_id(session_state)
        requested_tool_id = str(session_state.get("requested_tool_id") or "").strip()
        advertised_tool_rows = [dict(item) for item in (preview_payload.get("tools") or []) if isinstance(item, dict)]
        tool_rows = [tool_row for tool_row in advertised_tool_rows if self._is_tool_row_loadable(tool_row)]
        selection_menu = session_state.get("selection_menu")
        if isinstance(selection_menu, QMenu):
            blocker = QtCore.QSignalBlocker(selection_menu)
            selection_menu.clear()
            selection_menu.setEnabled(True)
            display_rows: list[dict[str, Any]] = []
            for connection_row in (preview_payload.get("connections") or []):
                if not isinstance(connection_row, dict):
                    continue
                display_rows.append(
                    {
                        "kind": "connection",
                        "label": str(connection_row.get("label") or "Connection").strip(),
                        "uri": str(connection_row.get("uri") or "").strip(),
                        "tool_id": "",
                    }
                )
            if display_rows and tool_rows:
                display_rows.append({"kind": "separator"})
            for tool_row in tool_rows:
                display_rows.append(
                    {
                        "kind": "extension",
                        "label": str(tool_row.get("label") or tool_row.get("tool_id") or "Extension").strip(),
                        "uri": str(tool_row.get("source_uri") or tool_row.get("uri") or source_uri or "").strip(),
                        "tool_id": str(tool_row.get("tool_id") or "").strip(),
                        "transport": str(tool_row.get("transport") or "").strip(),
                    }
                )
            for display_row in display_rows:
                if display_row.get("kind") == "separator":
                    selection_menu.addSeparator()
                    continue
                label_text = str(display_row.get("label") or "").strip()
                if display_row.get("kind") == "extension":
                    transport = str(display_row.get("transport") or "").strip()
                    if transport:
                        label_text = f"Extension: {label_text}"
                else:
                    label_text = f"Connection: {label_text}"
                action = selection_menu.addAction(label_text)
                action.setData(dict(display_row))

            preferred_tool_id = requested_tool_id or selected_tool_id
            preferred_action: QAction | None = None
            if preferred_tool_id:
                for action in selection_menu.actions():
                    action_payload = action.data()
                    if isinstance(action_payload, dict) and str(action_payload.get("tool_id") or "").strip() == preferred_tool_id:
                        preferred_action = action
                        break
            if preferred_action is None and selection_menu.actions():
                preferred_action = next((action for action in selection_menu.actions() if isinstance(action.data(), dict)), None)
            if isinstance(preferred_action, QAction):
                session_state["selected_tool_payload"] = dict(preferred_action.data() or {})
            elif "selected_tool_payload" in session_state:
                session_state.pop("selected_tool_payload", None)
            del blocker
        session_state["requested_tool_id"] = ""

        self._set_session_tab_title(session_state, loaded=bool(session_state.get("loaded")))
        self._persist_local_widget_state(session_state)
        self._sync_external_uri_proxies()

    def _load_extension_into_session(self, session_state: dict[str, Any], *, fit_view: bool) -> None:
        source_uri = self._session_source_uri(session_state)
        tool_id = self._session_tool_id(session_state)

        if not tool_id:
            return

        if bool(session_state.get("catalog_only")):
            session_state["catalog_only"] = False

        tab_widget = session_state.get("tab_widget")
        if isinstance(tab_widget, QWidget):
            tab_widget.setProperty("runtime_source_path", source_uri)
        self.setProperty("runtime_source_path", source_uri)

        try:
            backend_widget = self._backend_service.load_object_widget(
                object_name=tool_id,
                source_uri=source_uri,
                parent=session_state.get("host_container") if isinstance(session_state.get("host_container"), QWidget) else self,
                scheme=self.scheme,
            )
        except Exception as exc:
            backend_widget = QTextBrowser(self)
            backend_widget.setOpenExternalLinks(False)
            backend_widget.setOpenLinks(False)
            backend_widget.setHtml(
                f"<h3>Widget load failed</h3><p>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</p>"
            )

        self._set_session_content_widget(session_state, backend_widget)
        session_state["loaded_widget"] = backend_widget
        session_state["loaded"] = True

        backend_signal = getattr(backend_widget, "widgetStateChanged", None)
        if backend_signal is not None and hasattr(backend_signal, "connect"):
            backend_signal.connect(lambda _session_state=session_state: self._handle_backend_widget_state_changed(_session_state))

        stack_widget = session_state.get("stack")
        host_page = session_state.get("host_page")
        if isinstance(stack_widget, QStackedWidget) and isinstance(host_page, QWidget):
            stack_widget.setCurrentWidget(host_page)

        summary_payload = self._loaded_widget_summary(session_state)
        status_text = str(summary_payload.get("status_text") or "")
        control_status_label = session_state.get("control_status_label")
        if status_text and isinstance(control_status_label, (QLabel, QLineEdit)):
            control_status_label.setText(status_text)

        self._set_session_tab_title(session_state, loaded=True)
        self._persist_local_widget_state(session_state)
        self._sync_external_uri_proxies()
        self.widgetStateChanged.emit()

    def _handle_backend_widget_state_changed(self, session_state: dict[str, Any]) -> None:
        summary_payload = self._loaded_widget_summary(session_state)
        status_text = str(summary_payload.get("status_text") or "")
        control_status_label = session_state.get("control_status_label")
        if status_text and isinstance(control_status_label, (QLabel, QLineEdit)):
            control_status_label.setText(status_text)
        self._persist_local_widget_state(session_state)
        self.widgetStateChanged.emit()

    def _handle_session_control_change(self, session_state: dict[str, Any], *, refresh_preview: bool) -> None:
        tab_widget = session_state.get("tab_widget")
        if isinstance(tab_widget, QWidget):
            tab_widget.setProperty("runtime_source_path", self._session_source_uri(session_state))
        if refresh_preview:
            self._schedule_connection_preview_refresh(session_state)
        self._persist_local_widget_state(session_state)
        self._sync_external_uri_proxies()
        self.widgetStateChanged.emit()

    def update_scheme(
        self,
        accent: dict[str, str] | None = None,
        base: dict[str, str] | None = None,
    ) -> None:
        if accent is not None:
            self._accent = dict(accent)
        if base is not None:
            self._base = dict(base)
        self.scheme = _build_scheme(self._accent, self._base)
        extension_tab_palette = self._tab_palette("sunyellow")
        extension_selected_bg = _color_with_alpha(
            extension_tab_palette.get("handle_bg_pressed", extension_tab_palette.get("label_bg", "#222222")),
            34,
            fallback=extension_tab_palette.get("handle_bg_pressed", "rgba(32,32,32,34)"),
        )
        uri_surface_bg = str(self.scheme.get("col5") or self.scheme.get("col7") or "#0b0b0b")
        uri_border_idle = str(self.scheme.get("col10") or "#1f1f1f")
        uri_frame_color = str(self.scheme.get("col1") or self.scheme.get("col2") or uri_border_idle)
        uri_glow_hover = str(self.scheme.get("col2") or self.scheme.get("col1") or "#58ed5b")
        uri_glow_focus = str(self.scheme.get("col1") or self.scheme.get("col2") or "#0fe913")
        uri_hover_bg = _color_with_alpha(uri_glow_hover, 20, fallback=uri_surface_bg)
        uri_focus_bg = _color_with_alpha(uri_glow_focus, 28, fallback=uri_surface_bg)
        self.setStyleSheet(
            f"""
QWidget#extensionsSessionControlPage,
QWidget#extensionsSessionHostPage {{
    background-color: {self.scheme.get('col7', '#0b0b0b')};
}}
QLabel#extensionsControlTitle {{
    color: {self.scheme.get('col6', '#eef3ff')};
    font-weight: 700;
    font-size: 14px;
}}
QLabel#extensionsControlDescription {{
    color: {self.scheme.get('col8', '#9a9a9a')};
}}
QLabel#extensionsHostPlaceholder {{
    color: {self.scheme.get('col8', '#9a9a9a')};
    font-size: 13px;
}}
QTabWidget#extensionsTabs::pane {{
    border: 1px solid {self.scheme.get('col10', '#2a3350')};
    border-top: 0;
    background: {self.scheme.get('col7', '#0b0b0b')};
}}
QTabWidget#extensionsTabs QTabBar {{
    background: transparent;
    border-top: 0px solid transparent;
    margin-bottom: 4px;
    padding-bottom: 4px;
}}
QTabWidget#extensionsTabs QTabBar::tab {{
    color: {extension_tab_palette['label_fg']};
    background: {extension_tab_palette['label_bg']};
    border: 1px solid {extension_tab_palette['label_border']};
    border-radius: 6px;
    font-size: 10px;
    font-weight: 700;
    padding: 0px 28px 0px 9px;
    margin: 0px 2px 4px 0px;
    margin-bottom: 4px;
    min-width: 0px;
    min-height: 16px;
}}
QTabWidget#extensionsTabs QTabBar::tab:hover {{
    color: {extension_tab_palette['label_fg']};
    background: {extension_tab_palette['handle_bg_hover']};
    border: 1px solid {extension_tab_palette['handle_border_hover']};
    border-radius: 6px;
}}
QTabWidget#extensionsTabs QTabBar::tab:selected {{
    color: {extension_tab_palette['label_fg']};
    background: {extension_selected_bg};
    border: 1px solid {extension_tab_palette['handle_border_pressed']};
    border-radius: 6px;
}}
QTabWidget#extensionsTabs QTabBar::scroller {{
    width: 0px;
}}
QTabWidget#extensionsTabs QTabBar::tear {{
    width: 0px;
    height: 0px;
}}
QToolButton#extensionsTabCloseButton {{
    color: {extension_tab_palette['label_fg']};
    background: {extension_tab_palette['label_bg']};
    border: 1px solid {extension_tab_palette['label_border']};
    border-radius: 6px;
    font-size: 10px;
    font-weight: 700;
    padding: 0px;
    margin: 0px;
}}
QToolButton#extensionsTabCloseButton:hover {{
    color: {extension_tab_palette['label_fg']};
    background: {extension_tab_palette['handle_bg_hover']};
    border: 1px solid {extension_tab_palette['handle_border_hover']};
    border-radius: 6px;
}}
QLineEdit#extensionsGraphUriInput {{
    color: {self.scheme.get('col6', '#E3E3DE')};
    background: {uri_surface_bg};
    border: 1px solid {uri_frame_color};
    border-radius: 8px;
    padding: 4px 8px;
    min-height: 30px;
    selection-background-color: {uri_glow_focus};
    selection-color: {self.scheme.get('col6', '#E3E3DE')};
}}
QLineEdit#extensionsGraphUriInput:hover {{
    background: {uri_hover_bg};
    border-color: {uri_frame_color};
}}
QLineEdit#extensionsGraphUriInput:focus {{
    background: {uri_focus_bg};
    border-color: {uri_frame_color};
}}
QToolButton#extensionsUriPresetButton {{
    color: {self.scheme.get('col8', '#9a9a9a')};
    background: transparent;
    border: 1px solid {self.scheme.get('col10', '#1f1f1f')};
    border-radius: 7px;
    padding: 2px 8px;
    font-size: 12px;
    min-height: 24px;
}}
QToolButton#extensionsUriPresetButton:hover {{
    color: {self.scheme.get('col6', '#E3E3DE')};
    background: {self.scheme.get('col9', '#101010')};
}}
QTextBrowser#extensionsConnectionsPreviewBrowser {{
    color: {self.scheme.get('col6', '#E3E3DE')};
    background: {self.scheme.get('col9', '#101010')};
    border: 1px solid {self.scheme.get('col10', '#1f1f1f')};
    border-radius: 8px;
    padding: 8px;
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:vertical,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:horizontal {{
    background: transparent;
    margin: 0px;
    border: none;
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:vertical {{
    width: 6px;
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:horizontal {{
    height: 6px;
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:hover,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:vertical:hover,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar:horizontal:hover {{
    background: transparent;
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:vertical,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:horizontal {{
    background: rgba(0, 0, 0, 0.0);
    border-radius: 3px;
    min-height: 28px;
    min-width: 28px;
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:vertical:hover,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:horizontal:hover,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:hover:vertical,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:hover:horizontal {{
    background: {self.scheme.get('col10', '#1f1f1f')};
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:vertical:pressed,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:horizontal:pressed,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:pressed:vertical,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::handle:pressed:horizontal {{
    background: {self.scheme.get('col2', '#58ed5b')};
}}
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::add-line,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::sub-line,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::add-page,
QTextBrowser#extensionsConnectionsPreviewBrowser QScrollBar::sub-page {{
    background: none;
    border: none;
    width: 0px;
    height: 0px;
}}
QToolButton#extensionsGraphControlButton {{
    color: {self.scheme.get('col6', '#E3E3DE')};
    background: {self.scheme.get('col7', '#0b0b0b')};
    border: 1px solid {self.scheme.get('col10', '#1f1f1f')};
    border-radius: 7px;
    padding: 2px;
    min-width: 30px;
    min-height: 30px;
}}
QToolButton#extensionsGraphControlButton:hover {{
    background: {self.scheme.get('col9', '#101010')};
}}
QLineEdit#extensionsGraphStatusLabel {{
    color: {self.scheme.get('col8', '#9a9a9a')};
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 4px 8px;
    min-height: 30px;
}}
QLineEdit#extensionsGraphStatusLabel:hover {{
    border-color: {self.scheme.get('col10', '#1f1f1f')};
}}
QLineEdit#extensionsGraphStatusLabel:focus {{
    border-color: {self.scheme.get('col2', '#58ed5b')};
}}
"""
        )

        for session_state in self._session_state_by_tab.values():
            if not isinstance(session_state, dict):
                continue
            loaded_widget = session_state.get("loaded_widget")
            update_scheme_callable = getattr(loaded_widget, "update_scheme", None)
            if callable(update_scheme_callable):
                try:
                    update_scheme_callable(self.scheme)
                except Exception:
                    pass
            if not bool(session_state.get("loaded")):
                self._refresh_connection_preview(session_state)


ExtensionsWorkspace = ExtensionsWorkspaceWidget


class EnvConfigWidget(QWidget):
    def __init__(
        self,
        *,
        object_name: str,
        env_service: EnvConfigDomainService,
        store_handler: Callable[[Path], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._object_name = str(object_name or "")
        self._env_service = env_service
        self._store_handler = store_handler
        self._section_widget_map: list[tuple[EnvSectionObject, list[EnvVariableControlObject]]] = []
        self._dirty = False
        self._watch_events_paused = False

        self._file_watcher = QtCore.QFileSystemWatcher(self)
        self._file_watcher.fileChanged.connect(self._handle_object_file_changed)

        self._build_object_ui()
        self._register_object_file_watch()
        self.reload_object()

    def _build_object_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        self._path_label = QLabel(f"ENV source: {self._object_name}", self)
        self._path_label.setObjectName("envWidgetPathLabel")
        header_row.addWidget(self._path_label, 1)

        self._add_button = QPushButton("Add variable", self)
        self._add_button.clicked.connect(self._add_object_variable)
        header_row.addWidget(self._add_button, 0)

        self._reload_button = QPushButton("Reload", self)
        self._reload_button.clicked.connect(self.reload_object)
        header_row.addWidget(self._reload_button, 0)

        self._save_button = QPushButton("Save .env", self)
        self._save_button.clicked.connect(self.store_object)
        header_row.addWidget(self._save_button, 0)

        root.addLayout(header_row)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.NoFrame)

        self._section_container = QWidget(self._scroll_area)
        self._section_layout = QVBoxLayout(self._section_container)
        self._section_layout.setContentsMargins(0, 0, 0, 0)
        self._section_layout.setSpacing(10)
        self._scroll_area.setWidget(self._section_container)
        root.addWidget(self._scroll_area, 1)

        self._status_label = QLabel("", self)
        self._status_label.setObjectName("envWidgetStatusLabel")
        root.addWidget(self._status_label)

        self.setStyleSheet(
            "QFrame#envSectionCard {"
            " border: 1px solid #2c2c2c;"
            " border-radius: 10px;"
            " padding: 4px;"
            " }"
            "QLabel#envSectionTitle {"
            " font-weight: 700;"
            " }"
            "QLabel#envSectionDescription {"
            " color: #9a9a9a;"
            " font-size: 11px;"
            " }"
            "QLabel#envWidgetPathLabel {"
            " color: #9a9a9a;"
            " }"
            "QLabel#envWidgetStatusLabel {"
            " color: #9a9a9a;"
            " }"
            "QLabel#envVariableTypeChip {"
            " color: #9a9a9a;"
            " border: 1px solid #2c2c2c;"
            " border-radius: 6px;"
            " padding: 1px 6px;"
            " font-size: 10px;"
            " min-width: 34px;"
            " }"
            "QLineEdit#envVariableInput {"
            " font-family: monospace;"
            " }"
            "QComboBox#envVariableBoolInput {"
            " font-family: monospace;"
            " min-width: 76px;"
            " }"
        )

    def _register_object_file_watch(self) -> None:
        env_path = Path(self._object_name)
        env_path_text = str(env_path)
        if not env_path.exists() or not env_path.is_file():
            return
        if env_path_text not in set(self._file_watcher.files()):
            self._file_watcher.addPath(env_path_text)

    @Slot(str)
    def _handle_object_file_changed(self, _path: str) -> None:
        self._register_object_file_watch()
        if self._watch_events_paused:
            return
        if self._dirty:
            self._status_label.setText("External .env update detected. Save or reload to synchronize.")
            return
        QtCore.QTimer.singleShot(80, self.reload_object)

    @Slot()
    def reload_object(self) -> None:
        line_payload = self._env_service.load_object(self._object_name)
        section_objects = self._env_service.parse_object(self._object_name, line_payload)
        self._render_object_sections(section_objects)
        self._set_dirty_state(False)
        self._status_label.setText(f"Loaded {sum(len(section.variable_objects) for section in section_objects)} variables.")

    @Slot()
    def store_object(self) -> None:
        section_objects = self._collect_object_sections()
        try:
            self._watch_events_paused = True
            self._env_service.store_object(self._object_name, section_objects)
        except Exception as exc:
            self._status_label.setText(f"Save failed: {exc}")
            return
        finally:
            QtCore.QTimer.singleShot(200, self._resume_object_watch)

        self._set_dirty_state(False)
        self._status_label.setText(f"Saved .env: {self._object_name}")
        if callable(self._store_handler):
            try:
                self._store_handler(Path(self._object_name))
            except Exception:
                pass
        self.reload_object()

    @Slot()
    def _resume_object_watch(self) -> None:
        self._watch_events_paused = False
        self._register_object_file_watch()

    @Slot()
    def _add_object_variable(self) -> None:
        variable_name, accepted = QInputDialog.getText(
            self,
            "ENV",
            "Variable name:",
        )
        if not accepted:
            return

        normalized_name = str(variable_name or "").strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", normalized_name):
            QMessageBox.information(self, "ENV", "Variable names must match [A-Za-z_][A-Za-z0-9_]*")
            return

        variable_value, accepted_value = QInputDialog.getText(
            self,
            "ENV",
            f"Value for {normalized_name}:",
        )
        if not accepted_value:
            return

        section_objects = self._collect_object_sections()
        if not section_objects:
            section_objects = [EnvSectionObject("General", [], [])]

        existing_names = {
            str(variable_object.object_name or "").strip()
            for section_object in section_objects
            for variable_object in section_object.variable_objects
        }
        if normalized_name in existing_names:
            QMessageBox.information(self, "ENV", f"Variable already exists: {normalized_name}")
            return

        section_names = [str(section_object.object_name or "General") for section_object in section_objects]
        target_section_name = section_names[0]
        if len(section_names) > 1:
            selected_section_name, section_accepted = QInputDialog.getItem(
                self,
                "ENV",
                "Target section:",
                section_names,
                0,
                False,
            )
            if not section_accepted:
                return
            target_section_name = str(selected_section_name or target_section_name)

        target_section_object = next(
            (
                section_object
                for section_object in section_objects
                if str(section_object.object_name or "General") == target_section_name
            ),
            None,
        )
        if target_section_object is None:
            target_section_object = EnvSectionObject(target_section_name, [], [])
            section_objects.append(target_section_object)

        target_section_object.variable_objects.append(
            EnvVariableObject(
                object_name=normalized_name,
                value=str(variable_value or ""),
                enabled=True,
            )
        )
        self._render_object_sections(section_objects)
        self._set_dirty_state(True)
        self._status_label.setText(f"Variable added: {normalized_name}")

    def _render_object_sections(self, section_objects: list[EnvSectionObject]) -> None:
        while self._section_layout.count():
            item = self._section_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._section_widget_map = []

        has_variables = any(section_object.variable_objects for section_object in section_objects)
        if not has_variables:
            empty_label = QLabel("No variables found. Add one to start configuring ALDE.", self._section_container)
            empty_label.setObjectName("envSectionDescription")
            self._section_layout.addWidget(empty_label)
            self._section_layout.addStretch(1)
            return

        for section_object in section_objects:
            if not section_object.variable_objects:
                continue

            card = QFrame(self._section_container)
            card.setObjectName("envSectionCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 8, 8, 8)
            card_layout.setSpacing(6)

            section_title = QLabel(str(section_object.object_name or "General"), card)
            section_title.setObjectName("envSectionTitle")
            card_layout.addWidget(section_title)

            if section_object.comment_lines:
                section_description = QLabel(" | ".join(section_object.comment_lines), card)
                section_description.setObjectName("envSectionDescription")
                section_description.setWordWrap(True)
                card_layout.addWidget(section_description)

            form_layout = QFormLayout()
            form_layout.setContentsMargins(0, 0, 0, 0)
            form_layout.setSpacing(6)
            form_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

            variable_rows: list[EnvVariableControlObject] = []
            for variable_object in section_object.variable_objects:
                variable_row_widget = QWidget(card)
                variable_row_layout = QHBoxLayout(variable_row_widget)
                variable_row_layout.setContentsMargins(0, 0, 0, 0)
                variable_row_layout.setSpacing(6)

                control_object = self._build_variable_control_object(variable_object, variable_row_widget)
                variable_row_layout.addWidget(control_object.enabled_toggle, 0)

                value_kind_chip = QLabel(control_object.value_kind.upper(), variable_row_widget)
                value_kind_chip.setObjectName("envVariableTypeChip")
                value_kind_chip.setAlignment(Qt.AlignCenter)
                variable_row_layout.addWidget(value_kind_chip, 0)

                variable_row_layout.addWidget(control_object.value_widget, 1)

                form_layout.addRow(str(variable_object.object_name or ""), variable_row_widget)
                variable_rows.append(control_object)

            card_layout.addLayout(form_layout)
            self._section_layout.addWidget(card)
            self._section_widget_map.append((section_object, variable_rows))

        self._section_layout.addStretch(1)

    def _collect_object_sections(self) -> list[EnvSectionObject]:
        collected_sections: list[EnvSectionObject] = []
        for section_object, variable_rows in self._section_widget_map:
            collected_variables: list[EnvVariableObject] = []
            for control_object in variable_rows:
                variable_object = control_object.variable_object
                variable_name = str(variable_object.object_name or "").strip()
                if not variable_name:
                    continue

                variable_value = ""
                if control_object.value_kind == "bool" and isinstance(control_object.value_widget, QComboBox):
                    variable_value = str(control_object.value_widget.currentData() or control_object.value_widget.currentText() or "")
                elif isinstance(control_object.value_widget, QLineEdit):
                    variable_value = str(control_object.value_widget.text() or "")

                collected_variables.append(
                    EnvVariableObject(
                        object_name=variable_name,
                        value=variable_value,
                        enabled=bool(control_object.enabled_toggle.isChecked()),
                    )
                )

            if collected_variables:
                collected_sections.append(
                    EnvSectionObject(
                        object_name=str(section_object.object_name or "General"),
                        comment_lines=list(section_object.comment_lines),
                        variable_objects=collected_variables,
                    )
                )

        return collected_sections

    def _build_variable_control_object(self, variable_object: EnvVariableObject, parent: QWidget) -> EnvVariableControlObject:
        variable_name = str(variable_object.object_name or "")
        variable_value = str(variable_object.value or "")
        value_kind = self._infer_variable_value_kind(variable_name, variable_value)

        enabled_toggle = QCheckBox("on", parent)
        enabled_toggle.setChecked(bool(variable_object.enabled))
        enabled_toggle.setToolTip("Enable or disable this variable in .env")
        enabled_toggle.stateChanged.connect(self._mark_object_dirty)

        if value_kind == "bool":
            true_token, false_token = self._bool_token_pair_for_value(variable_value)
            value_widget = QComboBox(parent)
            value_widget.setObjectName("envVariableBoolInput")
            value_widget.addItem(true_token, true_token)
            value_widget.addItem(false_token, false_token)
            value_widget.setCurrentIndex(0 if self._is_true_bool_value(variable_value) else 1)
            value_widget.currentIndexChanged.connect(self._mark_object_dirty)
            return EnvVariableControlObject(
                variable_object=variable_object,
                enabled_toggle=enabled_toggle,
                value_widget=value_widget,
                value_kind=value_kind,
            )

        value_widget = QLineEdit(parent)
        value_widget.setObjectName("envVariableInput")
        value_widget.setText(variable_value)
        if value_kind == "int":
            value_widget.setValidator(QIntValidator(-2147483647, 2147483647, value_widget))
            value_widget.setPlaceholderText("integer")
        if self._is_secret_variable_object(variable_name):
            value_widget.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        value_widget.textEdited.connect(self._mark_object_dirty)
        return EnvVariableControlObject(
            variable_object=variable_object,
            enabled_toggle=enabled_toggle,
            value_widget=value_widget,
            value_kind=value_kind,
        )

    @staticmethod
    def _infer_variable_value_kind(object_name: str, value: str) -> str:
        normalized_name = str(object_name or "").upper()
        normalized_value = str(value or "").strip()
        normalized_value_lower = normalized_value.lower()
        name_tokens = {
            token
            for token in normalized_name.split("_")
            if token
        }

        bool_value_tokens = {"1", "0", "true", "false", "yes", "no", "on", "off"}
        bool_name_tokens = {
            "ENABLE",
            "ENABLED",
            "DISABLE",
            "DISABLED",
            "STRICT",
            "DEBUG",
            "VERBOSE",
            "READ_ONLY",
            "READONLY",
            "GPU_ONLY",
            "TLS",
            "SSL",
            "ALLOW",
            "USE",
            "HAS",
            "AUTO",
        }
        numeric_name_tokens = {
            "PORT",
            "TIMEOUT",
            "INTERVAL",
            "RETRY",
            "RETRIES",
            "COUNT",
            "LIMIT",
            "SIZE",
            "DIM",
            "DIMENSION",
            "LENGTH",
            "TTL",
            "SECONDS",
            "MINUTES",
            "HOURS",
            "MS",
            "MAX",
            "MIN",
            "AGE",
        }

        if normalized_value_lower in bool_value_tokens:
            return "bool"
        if name_tokens & bool_name_tokens:
            if not normalized_value or normalized_value_lower in bool_value_tokens:
                return "bool"

        if re.fullmatch(r"[-+]?\\d+", normalized_value):
            return "int"
        if name_tokens & numeric_name_tokens:
            if not normalized_value or re.fullmatch(r"[-+]?\\d+", normalized_value):
                return "int"

        return "text"

    @staticmethod
    def _bool_token_pair_for_value(value: str) -> tuple[str, str]:
        normalized_value = str(value or "").strip()
        normalized_lower = normalized_value.lower()
        token_pairs = (

            ("1", "0"),
            ("true", "false"),
            ("True", "False"),
            ("TRUE", "FALSE"),
            ("yes", "no"),
            ("Yes", "No"),
            ("YES", "NO"),
            ("on", "off"),
            ("On", "Off"),
            ("ON", "OFF"),
        )
        
        for true_token, false_token in token_pairs:
            if normalized_value == true_token or normalized_value == false_token:
                return (true_token, false_token)
        for true_token, false_token in token_pairs:
            if normalized_lower == true_token.lower() or normalized_lower == false_token.lower():
                return (true_token, false_token)
        return ("1", "0")

    @staticmethod
    def _is_true_bool_value(value: str) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _mark_object_dirty(self, *_args: Any) -> None:
        self._set_dirty_state(True)

    def _set_dirty_state(self, dirty: bool) -> None:
        self._dirty = bool(dirty)
        self._save_button.setEnabled(True)
        if self._dirty:
            self._status_label.setText("Unsaved changes")

    @staticmethod
    def _is_secret_variable_object(object_name: str) -> bool:
        upper_name = str(object_name or "").upper()
        for token in ("KEY", "SECRET", "TOKEN", "PASSWORD"):
            if token in upper_name:
                return True
        return False

class CustomWindowTitleBar(QWidget):
    """Custom frame title bar with menu button and window controls."""

    sectionOrderChanged = Signal(object)
    uriAnchorRatioChanged = Signal(float)
    uriTabSlotIndexChanged = Signal(int)
    _HEIGHT = 34
    _SECTION_ORDER_DEFAULT = ("tab_strip", "external_uri")

    def __init__(self, window: "MainAIEditor") -> None:
        super().__init__(window)
        self._window = window
        self._drag_active = False
        self._drag_start_global = QPoint()
        self._drag_start_window = QPoint()

        self.setObjectName("windowFrameTitleBar")
        self.setFixedHeight(self._HEIGHT)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self._title_label = QLabel(window.windowTitle(), self)
        self._title_label.setObjectName("windowFrameTitle")
        self._title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        layout.addWidget(self._title_label, 0, Qt.AlignVCenter)

        self._menu_btn = QToolButton(self)
        self._menu_btn.setObjectName("windowFrameMenuButton")
        self._menu_btn.setIcon(_icon("menu_24.svg"))
        self._menu_btn.setIconSize(QSize(16, 16))
        self._menu_btn.setAutoRaise(True)
        self._menu_btn.setFocusPolicy(Qt.NoFocus)
        self._menu_btn.setFixedSize(22, 22)
        self._menu_btn.setToolTip("Menue")
        self._menu_btn.setCursor(Qt.PointingHandCursor)
        self._menu_btn.clicked.connect(self._open_window_menu)
        layout.addWidget(self._menu_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)

        self._sections_container = QWidget(self)
        self._sections_container.setObjectName("windowFrameSectionsContainer")
        self._sections_container.setAttribute(Qt.WA_StyledBackground, True)
        self._sections_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sections_layout = QHBoxLayout(self._sections_container)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(6)
        layout.addWidget(self._sections_container, 1, Qt.AlignVCenter)

        self._uri_anchor_ratio = 0.5
        self._uri_tab_slot_index = 0
        self._uri_anchor_drag_active = False
        self._uri_anchor_drag_moved = False
        self._uri_tab_slot_count = 0
        self._uri_tab_slot_rects: list[QRect] = []
        self._uri_anchor_layout_update_pending = False
        self._tab_strip_host = QWidget(self)
        self._tab_strip_host.setObjectName("windowFrameTabStripHost")
        self._tab_strip_host.setAttribute(Qt.WA_StyledBackground, True)
        self._tab_strip_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._tab_strip_host_layout = QHBoxLayout(self._tab_strip_host)
        self._tab_strip_host_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_strip_host_layout.setSpacing(6)
        self._tab_strip_host.setMinimumWidth(220)
        self._tab_strip_host.setVisible(False)
        self._tab_strip_handle = self._new_section_handle("tab_strip", "Tab-Leiste umpositionieren")
        self._tab_strip_handle.setVisible(False)
        self._left_tab_strip_widget: QWidget | None = None
        self._right_tab_strip_widget: QWidget | None = None
        self._tab_strip_left_host = QWidget(self._tab_strip_host)
        self._tab_strip_left_host.setObjectName("windowFrameTabStripLeftHost")
        self._tab_strip_left_host.setAttribute(Qt.WA_StyledBackground, True)
        self._tab_strip_left_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._tab_strip_left_layout = QHBoxLayout(self._tab_strip_left_host)
        self._tab_strip_left_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_strip_left_layout.setSpacing(0)
        self._tab_strip_right_host = QWidget(self._tab_strip_host)
        self._tab_strip_right_host.setObjectName("windowFrameTabStripRightHost")
        self._tab_strip_right_host.setAttribute(Qt.WA_StyledBackground, True)
        self._tab_strip_right_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._tab_strip_right_layout = QHBoxLayout(self._tab_strip_right_host)
        self._tab_strip_right_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_strip_right_layout.setSpacing(0)

        self._external_uri_widget: QWidget | None = None
        self._external_uri_host = QWidget(self._tab_strip_host)
        self._external_uri_host.setObjectName("windowFrameExternalUriHost")
        self._external_uri_host.setAttribute(Qt.WA_StyledBackground, True)
        self._external_uri_host.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._external_uri_host_layout = QHBoxLayout(self._external_uri_host)
        self._external_uri_host_layout.setContentsMargins(0, 0, 0, 0)
        self._external_uri_host_layout.setSpacing(0)
        self._external_uri_host.setMinimumWidth(280)
        self._external_uri_host.setMaximumWidth(640)
        self._external_uri_host.setVisible(False)
        self._external_uri_handle = self._new_section_handle(
            "external_uri",
            "Adresseingabe horizontal ziehen; Klick zentriert",
        )
        self._external_uri_handle.setCursor(Qt.SizeHorCursor)
        self._external_uri_handle.installEventFilter(self)
        self._external_uri_host_layout.addWidget(self._external_uri_handle, 0, Qt.AlignVCenter)
        self._tab_strip_host_layout.addWidget(self._tab_strip_left_host, 1, Qt.AlignVCenter)
        self._tab_strip_host_layout.addWidget(self._external_uri_host, 0, Qt.AlignVCenter)
        self._tab_strip_host_layout.addWidget(self._tab_strip_right_host, 1, Qt.AlignVCenter)
        self._section_widgets: dict[str, QWidget] = {
            "tab_strip": self._tab_strip_host,
        }
        self._section_stretch: dict[str, int] = {
            "tab_strip": 1,
        }
        self._section_order = list(self._SECTION_ORDER_DEFAULT)
        self._apply_section_order(self._section_order)

        self._min_btn = QToolButton(self)
        self._min_btn.setObjectName("windowFrameButton")
        self._min_btn.setText("")
        self._min_btn.setIcon(_draw_window_control_icon("minimize", size=14))
        self._min_btn.setIconSize(QSize(12, 12))
        self._min_btn.setFocusPolicy(Qt.NoFocus)
        self._min_btn.setToolTip("Minimieren")
        self._min_btn.setCursor(Qt.PointingHandCursor)
        self._min_btn.clicked.connect(window.showMinimized)
        layout.addWidget(self._min_btn, 0, Qt.AlignRight | Qt.AlignVCenter)

        self._max_btn = QToolButton(self)
        self._max_btn.setObjectName("windowFrameButton")
        self._max_btn.setIcon(_draw_window_control_icon("maximize", size=14))
        self._max_btn.setIconSize(QSize(12, 12))
        self._max_btn.setFocusPolicy(Qt.NoFocus)
        self._max_btn.setCursor(Qt.PointingHandCursor)
        self._max_btn.clicked.connect(window._toggle_window_maximize_restore)
        layout.addWidget(self._max_btn, 0, Qt.AlignRight | Qt.AlignVCenter)

        self._fullscreen_btn = QToolButton(self)
        self._fullscreen_btn.setObjectName("windowFrameButton")
        self._fullscreen_btn.setIcon(_icon("open_in_full_26dp_999999_FILL0_wght500_GRAD0_opsz24.svg"))
        self._fullscreen_btn.setIconSize(QSize(12, 12))
        self._fullscreen_btn.setFocusPolicy(Qt.NoFocus)
        self._fullscreen_btn.setCheckable(True)
        self._fullscreen_btn.setToolTip("Echtes Fullscreen (F11)")
        self._fullscreen_btn.setCursor(Qt.PointingHandCursor)
        self._fullscreen_btn.clicked.connect(window._toggle_true_fullscreen_mode)
        layout.addWidget(self._fullscreen_btn, 0, Qt.AlignRight | Qt.AlignVCenter)

        self._close_btn = QToolButton(self)
        self._close_btn.setObjectName("windowFrameCloseButton")
        self._close_btn.setIcon(_draw_window_control_icon("close", size=14))
        self._close_btn.setIconSize(QSize(12, 12))
        self._close_btn.setFocusPolicy(Qt.NoFocus)
        self._close_btn.setToolTip("Schliessen")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(window.close)
        layout.addWidget(self._close_btn, 0, Qt.AlignRight | Qt.AlignVCenter)

        window.windowTitleChanged.connect(self._title_label.setText)
        self.apply_scheme(_build_scheme(window._accent, window._base))
        self.set_maximized(window.isMaximized())

    def set_menu_enabled(self, enabled: bool) -> None:
        self._menu_btn.setEnabled(bool(enabled))

    def _new_section_handle(self, section_id: str, tool_tip: str) -> QToolButton:
        handle_button = QToolButton(self)
        handle_button.setObjectName("windowFrameSectionHandle")
        handle_button.setText("::")
        handle_button.setAutoRaise(True)
        handle_button.setFocusPolicy(Qt.NoFocus)
        handle_button.setCursor(Qt.PointingHandCursor)
        handle_button.setToolTip(tool_tip)
        handle_button.setFixedSize(18, 18)
        handle_button.setProperty("titlebar_section_id", section_id)
        return handle_button

    def _normalized_section_order(self, order: Sequence[str] | None) -> list[str]:
        normalized_order: list[str] = []
        for section_id in list(order or []):
            normalized_section_id = str(section_id or "").strip().lower()
            if normalized_section_id in self._SECTION_ORDER_DEFAULT and normalized_section_id not in normalized_order:
                normalized_order.append(normalized_section_id)
        for section_id in self._SECTION_ORDER_DEFAULT:
            if section_id not in normalized_order:
                normalized_order.append(section_id)
        return normalized_order

    def _apply_section_order(self, order: Sequence[str] | None) -> None:
        resolved_order = self._normalized_section_order(order)
        while self._sections_layout.count() > 0:
            layout_item = self._sections_layout.takeAt(0)
            layout_widget = layout_item.widget()
            if isinstance(layout_widget, QWidget):
                layout_widget.setParent(self._sections_container)
        self._sections_layout.addWidget(self._tab_strip_host, 1, Qt.AlignVCenter)
        self._section_order = resolved_order
        self._update_uri_anchor_layout()

    def current_section_order(self) -> list[str]:
        return list(self._section_order)

    def set_section_order(self, order: Sequence[str] | None) -> None:
        self._apply_section_order(order)

    def uri_anchor_ratio(self) -> float:
        return float(self._uri_anchor_ratio)

    def uri_tab_slot_index(self) -> int:
        return int(self._uri_tab_slot_index)

    def set_uri_tab_slot_count(self, count: int) -> None:
        try:
            resolved_count = int(count)
        except Exception:
            resolved_count = 0
        resolved_count = max(0, resolved_count)
        if resolved_count == self._uri_tab_slot_count:
            return
        self._uri_tab_slot_count = resolved_count
        self.set_uri_tab_slot_index(self._uri_tab_slot_index)
        self._update_uri_anchor_layout()

    def set_uri_tab_slot_index(self, index: int) -> None:
        try:
            resolved_index = int(index)
        except Exception:
            resolved_index = 0
        max_slot_index = max(0, int(self._uri_tab_slot_count))
        resolved_index = max(0, min(resolved_index, max_slot_index))
        if resolved_index == self._uri_tab_slot_index and self._uri_tab_slot_rects:
            return
        self._uri_tab_slot_index = resolved_index
        self._update_uri_anchor_layout()
        self.uriTabSlotIndexChanged.emit(int(self._uri_tab_slot_index))

    def set_uri_anchor_ratio(self, value: float) -> None:
        try:
            resolved_ratio = float(value)
        except Exception:
            resolved_ratio = 0.5
        resolved_ratio = max(0.0, min(1.0, resolved_ratio))
        if math.isclose(resolved_ratio, float(self._uri_anchor_ratio), abs_tol=1e-6):
            return
        self._uri_anchor_ratio = resolved_ratio
        self._update_uri_anchor_layout()
        self.uriAnchorRatioChanged.emit(float(self._uri_anchor_ratio))

    def _reset_uri_anchor_position(self) -> None:
        self.set_uri_anchor_ratio(0.5)

    def _resolved_external_uri_host_width(self) -> int:
        if not self._external_uri_host.isVisible():
            return 0
        try:
            hint_width = int(self._external_uri_host.sizeHint().width())
        except Exception:
            hint_width = 0
        min_width = max(0, int(self._external_uri_host.minimumWidth()))
        max_width = int(self._external_uri_host.maximumWidth())
        if max_width <= 0:
            max_width = max(min_width, hint_width)
        resolved_width = max(min_width, hint_width)
        return max(min_width, min(resolved_width, max_width))

    def _resolved_tab_strip_required_width(self) -> int:
        if not self._tab_strip_host.isVisible():
            return 0
        try:
            hint_width = int(self._tab_strip_host.sizeHint().width())
        except Exception:
            hint_width = 0
        try:
            min_hint_width = int(self._tab_strip_host.minimumSizeHint().width())
        except Exception:
            min_hint_width = 0
        min_width = max(0, int(self._tab_strip_host.minimumWidth()))
        return max(min_width, hint_width, min_hint_width)

    def _refresh_tab_strip_host_visibility(self) -> None:
        left_widget = self._left_tab_strip_widget
        right_widget = self._right_tab_strip_widget
        has_left = isinstance(left_widget, QWidget)
        has_right = isinstance(right_widget, QWidget)
        if isinstance(left_widget, QTabBar):
            try:
                has_left = left_widget.count() > 0
            except RuntimeError:
                has_left = False
        if isinstance(right_widget, QTabBar):
            try:
                has_right = right_widget.count() > 0
            except RuntimeError:
                has_right = False
        has_uri = isinstance(self._external_uri_widget, QWidget)
        self._tab_strip_left_host.setVisible(has_left or has_uri)
        self._tab_strip_right_host.setVisible(has_right or has_uri)
        self._external_uri_host.setVisible(has_uri)
        self._tab_strip_host.setVisible(has_left or has_right or has_uri)
        self._schedule_uri_anchor_layout_update()

    def _schedule_uri_anchor_layout_update(self) -> None:
        if self._uri_anchor_layout_update_pending:
            return
        self._uri_anchor_layout_update_pending = True

        def _apply_pending_update() -> None:
            self._uri_anchor_layout_update_pending = False
            try:
                self._update_uri_anchor_layout()
            except RuntimeError:
                return

        QtCore.QTimer.singleShot(0, _apply_pending_update)

    def _uses_uri_tab_slot_layout(self) -> bool:
        left_widget = self._left_tab_strip_widget
        if not isinstance(left_widget, QWidget):
            return False
        if isinstance(left_widget, QTabBar):
            try:
                return left_widget.count() > 0
            except RuntimeError:
                return False
        try:
            return left_widget.isVisible()
        except RuntimeError:
            return False

    def _set_uri_anchor_from_container_x(self, container_x: int) -> None:
        if self._uri_tab_slot_rects and self._uses_uri_tab_slot_layout():
            closest_index = 0
            closest_distance = None
            for slot_index, slot_rect in enumerate(self._uri_tab_slot_rects):
                slot_center = slot_rect.center().x()
                slot_distance = abs(int(container_x) - int(slot_center))
                if closest_distance is None or slot_distance < closest_distance:
                    closest_distance = slot_distance
                    closest_index = slot_index
            self.set_uri_tab_slot_index(closest_index)
            return
        available_width = max(0, int(self._tab_strip_host.width() or self._sections_container.width()))
        uri_width = self._resolved_external_uri_host_width()
        max_left = max(0, available_width - uri_width)
        desired_left = max(0, min(int(container_x - (uri_width / 2)), max_left))
        self.set_uri_anchor_ratio(self._uri_anchor_ratio_from_left(desired_left, max_left, uri_width))

    def _resolved_titlebar_centered_uri_left(self, max_left: int, uri_width: int) -> int:
        max_left = max(0, int(max_left))
        uri_width = max(0, int(uri_width))
        try:
            host_left = int(self._tab_strip_host.mapTo(self, QPoint(0, 0)).x())
        except Exception:
            host_left = int(self._tab_strip_host.x())
        titlebar_center = int(self.contentsRect().center().x())
        centered_left = int(round(titlebar_center - host_left - (uri_width / 2.0)))
        return max(0, min(centered_left, max_left))

    def _uri_left_from_anchor_ratio(self, ratio: float, max_left: int, uri_width: int) -> int:
        max_left = max(0, int(max_left))
        if max_left <= 0:
            return 0
        centered_left = self._resolved_titlebar_centered_uri_left(max_left, uri_width)
        resolved_ratio = max(0.0, min(1.0, float(ratio)))
        if resolved_ratio <= 0.5:
            if centered_left <= 0:
                return 0
            return int(round(centered_left * (resolved_ratio / 0.5)))
        right_span = max(0, max_left - centered_left)
        if right_span <= 0:
            return centered_left
        return int(round(centered_left + (right_span * ((resolved_ratio - 0.5) / 0.5))))

    def _uri_anchor_ratio_from_left(self, left: int, max_left: int, uri_width: int) -> float:
        max_left = max(0, int(max_left))
        desired_left = max(0, min(int(left), max_left))
        if max_left <= 0:
            return 0.5
        centered_left = self._resolved_titlebar_centered_uri_left(max_left, uri_width)
        if desired_left <= centered_left:
            if centered_left <= 0:
                return 0.0
            return max(0.0, min(0.5, 0.5 * (desired_left / float(centered_left))))
        right_span = max_left - centered_left
        if right_span <= 0:
            return 1.0
        return max(0.5, min(1.0, 0.5 + (0.5 * ((desired_left - centered_left) / float(right_span)))))

    def _tab_slot_left_from_index(self, slot_index: int) -> int:
        if not self._uri_tab_slot_rects:
            return 0
        resolved_slot_index = max(0, min(int(slot_index), len(self._uri_tab_slot_rects) - 1))
        slot_rect = self._uri_tab_slot_rects[resolved_slot_index]
        uri_width = self._resolved_external_uri_host_width()
        return max(0, int(round(slot_rect.center().x() - (uri_width / 2))))

    def _rebuild_uri_tab_slot_rects(self, available_width: int, uri_width: int) -> None:
        slot_count = max(0, int(self._uri_tab_slot_count))
        if slot_count <= 0 or available_width <= 0:
            self._uri_tab_slot_rects = []
            return
        step_count = max(1, slot_count)
        max_left = max(0, available_width - uri_width)
        self._uri_tab_slot_rects = []
        for slot_index in range(slot_count + 1):
            if slot_count <= 0:
                left = 0
            else:
                left = int(round((max_left * slot_index) / float(step_count)))
            self._uri_tab_slot_rects.append(QRect(left, 0, uri_width, max(1, int(self._sections_container.height()))))

    def _update_uri_anchor_layout(self) -> None:
        if not self._external_uri_host.isVisible():
            self._uri_tab_slot_rects = []
            self._tab_strip_left_host.setMinimumWidth(0)
            self._tab_strip_left_host.setMaximumWidth(16777215)
            return

        available_width = max(0, int(self._tab_strip_host.width() or self._sections_container.width()))
        uri_width = self._resolved_external_uri_host_width()
        self._rebuild_uri_tab_slot_rects(available_width, uri_width)
        max_left = max(0, available_width - uri_width)
        # Position the URI/input bar according to the persisted anchor: use the
        # nearest tab slot when real tabs are present, otherwise fall back to
        # the ratio-based anchor (0.5 = centered, matching the default state).
        if self._uri_tab_slot_rects and self._uses_uri_tab_slot_layout():
            desired_left = self._tab_slot_left_from_index(self._uri_tab_slot_index)
        else:
            desired_left = self._uri_left_from_anchor_ratio(self._uri_anchor_ratio, max_left, uri_width)
        desired_left = max(0, min(desired_left, max_left))
        left_spacing = max(0, int(self._tab_strip_host_layout.spacing()))
        left_spacer_width = max(0, desired_left - left_spacing)
        self._tab_strip_left_host.setMinimumWidth(left_spacer_width)
        self._tab_strip_left_host.setMaximumWidth(left_spacer_width)
        self._external_uri_host.setMinimumWidth(uri_width)
        self._external_uri_host.setMaximumWidth(uri_width)
        self._tab_strip_left_host.updateGeometry()
        self._external_uri_host.updateGeometry()
        self._tab_strip_right_host.updateGeometry()
        self._tab_strip_host_layout.invalidate()
        self._sections_layout.invalidate()
        self._sections_container.updateGeometry()
        self._sections_container.update()

    def _toggle_section_position(self, section_id: str) -> None:
        resolved_order = self._normalized_section_order(self._section_order)
        if section_id not in resolved_order or len(resolved_order) < 2:
            return
        current_index = resolved_order.index(section_id)
        target_index = 0 if current_index > 0 else len(resolved_order) - 1
        if current_index == target_index:
            return
        moved_section_id = resolved_order.pop(current_index)
        resolved_order.insert(target_index, moved_section_id)
        self._apply_section_order(resolved_order)
        self.sectionOrderChanged.emit(list(self._section_order))

    def _mount_host_widget(
        self,
        host_layout: QHBoxLayout,
        attr_name: str,
        widget: QWidget | None,
    ) -> None:
        mounted_widget = getattr(self, attr_name, None)
        if mounted_widget is widget:
            return
        if isinstance(mounted_widget, QWidget):
            host_layout.removeWidget(mounted_widget)
            mounted_widget.setParent(None)
            mounted_widget.deleteLater()
        setattr(self, attr_name, None)
        if isinstance(widget, QWidget):
            host_layout.addWidget(widget, 1)
            setattr(self, attr_name, widget)

    def set_tab_strip_widget(self, widget: QWidget | None) -> None:
        self._mount_host_widget(self._tab_strip_right_layout, "_right_tab_strip_widget", widget)
        self._mount_host_widget(self._tab_strip_left_layout, "_left_tab_strip_widget", None)
        self._refresh_tab_strip_host_visibility()
        self._update_uri_anchor_layout()

    def set_left_tab_strip_widget(self, widget: QWidget | None) -> None:
        self._mount_host_widget(self._tab_strip_left_layout, "_left_tab_strip_widget", widget)
        self._refresh_tab_strip_host_visibility()
        self._update_uri_anchor_layout()

    def set_right_tab_strip_widget(self, widget: QWidget | None) -> None:
        self._mount_host_widget(self._tab_strip_right_layout, "_right_tab_strip_widget", widget)
        self._refresh_tab_strip_host_visibility()
        self._update_uri_anchor_layout()

    def set_external_uri_widget(self, widget: QWidget | None) -> None:
        self._mount_host_widget(self._external_uri_host_layout, "_external_uri_widget", widget)
        self._external_uri_handle.setVisible(False)
        self._refresh_tab_strip_host_visibility()
        self._update_uri_anchor_layout()

    def apply_scheme(self, scheme: Mapping[str, str]) -> None:
        bg = str(scheme.get("col5") or scheme.get("col7") or "#000000")
        fg = str(scheme.get("col6") or "#E3E3DED6")
        border = str(scheme.get("col10") or "#242424")
        hover = "rgba(190,190,190,22)"
        menu_pressed = "rgba(190,190,190,24)"
        close_hover = "rgba(190,190,190,26)"
        tab_fg = "#FFFFFF"
        tab_bg = "rgba(255, 204, 0, 0.28)"
        tab_hover_bg = "rgba(255, 204, 0, 0.44)"
        tab_selected_bg = "rgba(255, 204, 0, 0.22)"
        tab_border = "rgba(255, 204, 0, 0.95)"
        uri_bg = str(scheme.get("col9") or "#101010")
        titlebar_surface_bg = str(scheme.get("col5") or bg or "#000000")
        uri_border = str(scheme.get("col10") or "#1f1f1f")
        uri_frame_color = str(scheme.get("col1") or scheme.get("col2") or uri_border)
        uri_glow_hover = str(scheme.get("col2") or scheme.get("col1") or "#58ed5b")
        uri_glow_focus = str(scheme.get("col1") or scheme.get("col2") or uri_glow_hover)
        uri_hover_bg = _color_with_alpha(uri_glow_hover, 20, fallback=titlebar_surface_bg)
        uri_focus_bg = _color_with_alpha(uri_glow_focus, 28, fallback=titlebar_surface_bg)
        titlebar_tab_height = max(16, int(self._HEIGHT) - 12)
        self.setStyleSheet(
            f"""
            QWidget#windowFrameTitleBar {{
                background: {bg};
                background-color: {bg};
                border: none;
            }}
            QWidget#windowFrameSectionsContainer {{
                background: {titlebar_surface_bg};
                background-color: {titlebar_surface_bg};
                border: none;
            }}
            QWidget#windowFrameTabStripHost,
            QWidget#windowFrameExternalUriHost,
            QWidget#windowFrameExtensionsTabProxyHost {{
                background: {titlebar_surface_bg};
                background-color: {titlebar_surface_bg};
                border: none;
            }}
            QWidget[extensions_titlebar_uri_container="true"] {{
                background: {titlebar_surface_bg};
                background-color: {titlebar_surface_bg};
                border: none;
            }}
            QLabel#windowFrameTitle {{
                color: {fg};
                font-size: 13px;
                font-weight: 600;
                padding-left: 2px;
            }}
            QToolButton#windowFrameMenuButton {{
                background: transparent;
                color: {fg};
                border: none;
                border-radius: 6px;
                padding: 0px;
                margin: 0px;
            }}
            QToolButton#windowFrameMenuButton:hover {{
                background: transparent;
                border: none;
            }}
            QToolButton#windowFrameMenuButton:pressed {{
                background: {menu_pressed};
                border: none;
            }}
            QToolButton#windowFrameMenuButton:focus {{
                background: transparent;
                border: none;
            }}
            QToolButton#windowFrameSectionHandle {{
                background: transparent;
                color: {fg};
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 0px 4px;
                margin-right: 4px;
            }}
            QToolButton#windowFrameSectionHandle:hover {{
                background: {hover};
                border-color: {border};
            }}
            QToolButton#windowFrameButton,
            QToolButton#windowFrameCloseButton {{
                background: transparent;
                color: {fg};
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 2px 7px;
            }}
            QToolButton#windowFrameButton:focus,
            QToolButton#windowFrameCloseButton:focus {{
                background: transparent;
                border-color: transparent;
            }}
            QToolButton#windowFrameButton:hover {{
                background: {hover};
                border-color: {border};
            }}
            QToolButton#windowFrameCloseButton:hover {{
                background: {close_hover};
                border-color: transparent;
            }}
            QTabBar#windowFrameExtensionsEmbeddedTabBar {{
                background: {titlebar_surface_bg};
                background-color: {titlebar_surface_bg};
            }}
            QTabBar#windowFrameExtensionsEmbeddedTabBar::tab {{
                color: {tab_fg};
                background: {tab_bg};
                border: 1px solid {tab_border};
                border-radius: 6px;
                font-size: 10px;
                font-weight: 700;
                padding: 0px 26px 0px 9px;
                margin: 0px 2px 0px 0px;
                min-width: 0px;
                min-height: 0px;
                max-height: {titlebar_tab_height}px;
            }}
            QTabBar#windowFrameExtensionsEmbeddedTabBar::tab:hover {{
                color: {tab_fg};
                background: {tab_hover_bg};
                border: 1px solid {tab_border};
            }}
            QTabBar#windowFrameExtensionsEmbeddedTabBar::tab:selected {{
                color: {tab_fg};
                background: {tab_selected_bg};
                border: 1px solid {tab_border};
            }}
            QTabBar#windowFrameExtensionsEmbeddedTabBar::scroller {{
                width: 0px;
            }}
            QToolButton#extensionsEmbeddedTabCloseButton {{
                color: {tab_fg};
                background: {tab_bg};
                border: 1px solid {tab_border};
                border-radius: 6px;
                font-size: 10px;
                font-weight: 700;
                padding: 0px;
                margin: 0px;
            }}
            QToolButton#extensionsEmbeddedTabCloseButton:hover {{
                color: {tab_fg};
                background: {tab_hover_bg};
                border: 1px solid {tab_border};
                border-radius: 6px;
            }}
            QLineEdit#extensionsGraphUriInput {{
                color: {fg};
                background: {titlebar_surface_bg};
                background-color: {titlebar_surface_bg};
                border: 1px solid {uri_frame_color};
                border-radius: 8px;
                padding: 4px 8px;
                min-height: 18px;
                selection-background-color: {uri_glow_focus};
                selection-color: {fg};
            }}
            QLineEdit#extensionsGraphUriInput:hover {{
                background: {uri_hover_bg};
                background-color: {uri_hover_bg};
                border-color: {uri_frame_color};
            }}
            QLineEdit#extensionsGraphUriInput:focus {{
                background: {uri_focus_bg};
                background-color: {uri_focus_bg};
                border-color: {uri_frame_color};
            }}
            QLineEdit#extensionsGraphUriInput QToolButton {{
                background: transparent;
                border: none;
            }}
            QToolButton:disabled {{
                color: {border};
            }}
            """
        )
        for proxy_tab_bar in self.findChildren(QTabBar, _WINDOW_FRAME_EMBEDDED_TAB_BAR_OBJECT_NAME):
            try:
                proxy_tab_bar.setProperty("extensions_titlebar_uri_fill", titlebar_surface_bg)
                proxy_tab_bar.update()
            except RuntimeError:
                continue
        self._min_btn.setIcon(_draw_window_control_icon("minimize", size=14, color=fg))
        self._close_btn.setIcon(_draw_window_control_icon("close", size=14, color=fg))
        self._fullscreen_btn.setIcon(_icon("open_in_full_26dp_999999_FILL0_wght500_GRAD0_opsz24.svg"))
        self._fullscreen_btn.setIconSize(QSize(12, 12))
        self.set_maximized(self._window.isMaximized())

    def set_maximized(self, maximized: bool) -> None:
        scheme = _build_scheme(self._window._accent, self._window._base)
        icon_color = str(scheme.get("col6") or "#E3E3DED6")
        self._max_btn.setIcon(_draw_window_control_icon("maximize", size=14, color=icon_color))
        self._max_btn.setToolTip("Wiederherstellen" if maximized else "Maximieren")
        self._max_btn.setIconSize(QSize(12, 12))
        is_fullscreen = self._window.isFullScreen()
        self._fullscreen_btn.setChecked(is_fullscreen)
        self._fullscreen_btn.setToolTip("Fullscreen beenden (F11)" if is_fullscreen else "Echtes Fullscreen (F11)")

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_uri_anchor_layout()

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._external_uri_handle:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._uri_anchor_drag_active = True
                self._uri_anchor_drag_moved = False
                event.accept()
                return True
            if event.type() == QEvent.MouseMove and self._uri_anchor_drag_active and (event.buttons() & Qt.LeftButton):
                self._uri_anchor_drag_moved = True
                global_point = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else event.globalPos()
                local_point = self._tab_strip_host.mapFromGlobal(global_point)
                self._set_uri_anchor_from_container_x(local_point.x())
                event.accept()
                return True
            if event.type() == QEvent.MouseButtonRelease and self._uri_anchor_drag_active and event.button() == Qt.LeftButton:
                self._uri_anchor_drag_active = False
                if not self._uri_anchor_drag_moved:
                    if self._uri_tab_slot_rects and self._uses_uri_tab_slot_layout():
                        midpoint_slot = int(round(len(self._uri_tab_slot_rects) / 2.0))
                        self.set_uri_tab_slot_index(midpoint_slot)
                    else:
                        self._reset_uri_anchor_position()
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def _open_window_menu(self) -> None:
        anchor = self._menu_btn.mapToGlobal(QPoint(0, self._menu_btn.height()))
        self._window._show_window_menu_popup(anchor)

    def _try_system_move(self) -> bool:
        handle = self.window().windowHandle()
        if handle is None or not hasattr(handle, "startSystemMove"):
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            if self._try_system_move():
                event.accept()
                return
            self._drag_active = True
            self._drag_start_global = event.globalPosition().toPoint()
            self._drag_start_window = self.window().frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_active and (event.buttons() & Qt.LeftButton):
            delta = event.globalPosition().toPoint() - self._drag_start_global
            self.window().move(self._drag_start_window + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_active = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._window._toggle_window_maximize_restore()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class WindowToolbarDragHandle(QWidget):
    """Drag handle for moving the frameless window."""

    def __init__(self, window: "MainAIEditor", parent: QWidget | None = None) -> None:
        super().__init__(parent or window)
        self._window = window
        self._drag_active = False
        self._drag_start_global = QPoint()
        self._drag_start_window = QPoint()

        self.setObjectName("toolbarWindowDragHandle")
        self.setCursor(Qt.SizeAllCursor)
        self.setToolTip("Fenster bewegen")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumSize(80, 24)

    def _try_system_move(self) -> bool:
        handle = self._window.windowHandle()
        if handle is None or not hasattr(handle, "startSystemMove"):
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False

    def mousePressEvent(self, event):  # noqa: N802
        if bool(getattr(self._window, "_custom_titlebar_active", False)):
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            if self._try_system_move():
                event.accept()
                return
            self._drag_active = True
            self._drag_start_global = event.globalPosition().toPoint()
            self._drag_start_window = self._window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_active and (event.buttons() & Qt.LeftButton):
            delta = event.globalPosition().toPoint() - self._drag_start_global
            self._window.move(self._drag_start_window + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_active = False
        super().mouseReleaseEvent(event)


class WindowTopFrameBar(WindowToolbarDragHandle):
    """Thin top frame shown when the custom titlebar is hidden."""

    def __init__(self, window: "MainAIEditor", *, height_px: int, parent: QWidget | None = None) -> None:
        super().__init__(window, parent or window)
        self.setObjectName("windowTopFrameBar")
        self.setToolTip("Fenster bewegen")
        self.setMinimumSize(1, max(1, int(height_px)))
        self.setFixedHeight(max(1, int(height_px)))


class MainAIEditor(QMainWindow):
    ORG_NAME: Final = "ai.bentu"

    APP_NAME: Final = "A.I.M"
    WINDOW_TITLE: Final = "A.I.M"
    _SCHEMA:  Final = 2
    _EXPLORER_WIDTH_SNAP_OFFSET_PX: Final = 60
    _EXPLORER_WIDTH_MIN_PX: Final = 120
    _EXPLORER_WIDTH_EXPAND_PX: Final = 40
    _BOARD_WIDTH_SNAP_OFFSET_PX: Final = 100
    _BOARD_WIDTH_MIN_PX: Final = 160

    # ---------------------------------------------------------------- init --

    def __init__(self):
        super().__init__()
        self._accent_name = "green"
        self._accent, self._base = _accent_from_name(self._accent_name), SCHEME_DARK
        self._tab_docks: List[QDockWidget] = []          # store all tab docks
        self._workspace_column_widths: list[int] = [260, 760, 460, 180]
        self._explorer_splitter_sizes: list[int] = [380, 130]
        self._explorer_database_panel_visible: bool = False
        self._window_menu_bar: QMenuBar | None = None
        self._title_bar_widget: CustomWindowTitleBar | None = None
        self._window_titlebar_left_tab_proxy_host: QWidget | None = None
        self._window_titlebar_tab_proxy_host: QWidget | None = None
        self._window_titlebar_uri_proxy: QLineEdit | None = None
        self._window_titlebar_section_order: list[str] = ["external_uri", "tab_strip"]
        self._window_titlebar_uri_anchor_ratio: float = 0.5
        self._window_titlebar_uri_tab_slot_index: int = 0
        self._title_menu_popup: QMenu | None = None
        self._tb_left_chrome_widget: QWidget | None = None
        self._tb_left_title_label: QLabel | None = None
        self._tb_left_menu_button: QToolButton | None = None
        self._tb_right_min_btn: QToolButton | None = None
        self._tb_right_max_btn: QToolButton | None = None
        self._tb_right_fullscreen_btn: QToolButton | None = None
        self._tb_right_close_btn: QToolButton | None = None
        self._tb_left_chrome_action: QAction | None = None
        self._tb_right_min_action: QAction | None = None
        self._tb_right_max_action: QAction | None = None
        self._tb_right_fullscreen_action: QAction | None = None
        self._tb_right_close_action: QAction | None = None
        self._tb_left_drag_handle: WindowToolbarDragHandle | None = None
        self._window_top_frame_widget: WindowTopFrameBar | None = None
        self._custom_titlebar_active = False
        self._toolbar_left_user_visible = True
        self._toolbar_right_user_visible = True
        self._was_maximized_before_fullscreen = False

        # Crash-isolation helper: progressively enable init steps.
        # Default is "full" (999). Smaller numbers build less UI.
        try:
            init_level = int(os.getenv("AI_IDE_INIT_LEVEL", "999") or "999")
        except Exception:
            init_level = 999

        self.setWindowTitle(self.WINDOW_TITLE)
        self.resize(1280, 800)
        #self.showFullScreen
        # ---- create primary widgets/layout --------------------------------
        if init_level >= 1:
            self._create_side_widgets()
        if init_level >= 2:
            self._create_central_splitters()
        else:
            # Keep a simple central widget so the window is valid.
            te = QTextEdit()
            te.setPlainText("AI_IDE_INIT_LEVEL < 2 (central UI skipped)")
            self.setCentralWidget(te)
        if init_level >= 3:
            self._create_actions()
        if init_level >= 4:
            self._create_toolbars()
        if init_level >= 5:
            self._create_menu()
        if not _env_truthy("AI_IDE_DISABLE_CUSTOM_WINDOW_FRAME", "0"):
            self._install_custom_window_frame()
        if init_level >= 6:
            self._create_status()
        if init_level >= 7:
            self._wire_vis()
        # -----------------------------------------------------------------
   
        if init_level >= 8:
            _apply_style(self, _build_scheme(self._accent, self._base))

        if init_level >= 9:
            self._load_ui_state()
        self._sync_window_chrome_toolbar_visibility()
        self._sync_fullscreen_action_state()

        # -----------------------------------------------------------------
        # <- changes 31.07.2025

        # 1) create persistence helper
        if init_level >= 10:
            self._chat = ChatHistory()
            ChatHistory._history_ = self._chat._load()
        # 2)the chat history will be load  from disk and 
        # log to cache right after the UI is set up

        # ~> loaded = True !
        
        # ~> object = chat 

    def _get_live_title_bar_widget(self) -> CustomWindowTitleBar | None:
        widget = self._title_bar_widget
        if widget is None:
            return None
        try:
            _ = widget.objectName()
        except RuntimeError:
            self._title_bar_widget = None
            return None
        return widget

    def _get_live_top_frame_widget(self) -> WindowTopFrameBar | None:
        widget = self._window_top_frame_widget
        if widget is None:
            return None
        try:
            _ = widget.objectName()
        except RuntimeError:
            self._window_top_frame_widget = None
            return None
        return widget

    def _top_frame_height_px(self) -> int:
        try:
            dpi_y = float(self.logicalDpiY() or 96.0)
        except Exception:
            dpi_y = 96.0
        # 2 mm in device pixels.
        return max(1, int(round((dpi_y * 2.0) / 25.4)))

    def _normalize_window_titlebar_section_order(self, order: Sequence[str] | None) -> list[str]:
        default_order = ["external_uri", "tab_strip"]
        normalized_order: list[str] = []
        for section_id in list(order or []):
            normalized_section_id = str(section_id or "").strip().lower()
            if normalized_section_id in default_order and normalized_section_id not in normalized_order:
                normalized_order.append(normalized_section_id)
        for section_id in default_order:
            if section_id not in normalized_order:
                normalized_order.append(section_id)
        return normalized_order

    @Slot(object)
    def _handle_window_titlebar_section_order_changed(self, order: object) -> None:
        if isinstance(order, (list, tuple)):
            self._window_titlebar_section_order = self._normalize_window_titlebar_section_order(order)

    @staticmethod
    def _normalize_window_titlebar_uri_anchor_ratio(value: object) -> float:
        try:
            resolved_ratio = float(value)
        except Exception:
            resolved_ratio = 0.5
        return max(0.0, min(1.0, resolved_ratio))

    @Slot(float)
    def _handle_window_titlebar_uri_anchor_ratio_changed(self, value: float) -> None:
        self._window_titlebar_uri_anchor_ratio = self._normalize_window_titlebar_uri_anchor_ratio(value)

    @Slot(int)
    def _handle_window_titlebar_uri_tab_slot_index_changed(self, value: int) -> None:
        try:
            self._window_titlebar_uri_tab_slot_index = max(0, int(value))
        except Exception:
            self._window_titlebar_uri_tab_slot_index = 0
        self._sync_window_titlebar_tab_slot_split()

    def _set_window_titlebar_uri_tab_slot_index(self, value: int) -> None:
        self._handle_window_titlebar_uri_tab_slot_index_changed(value)
        self._sync_window_titlebar_extensions()

    def _sync_window_titlebar_tab_slot_split(self) -> None:
        extensions_widget = getattr(self, "extensions_widget", None)
        if not isinstance(extensions_widget, ExtensionsWorkspaceWidget):
            return

        right_tab_proxy_host = self._window_titlebar_tab_proxy_host
        right_proxy_tab_bar = right_tab_proxy_host.findChild(QTabBar, _WINDOW_FRAME_EMBEDDED_TAB_BAR_OBJECT_NAME) if isinstance(right_tab_proxy_host, QWidget) else None

        if isinstance(right_proxy_tab_bar, QTabBar):
            right_proxy_tab_bar.setProperty("extensions_titlebar_uri_slot_index", self._window_titlebar_uri_tab_slot_index)
        extensions_widget._sync_embedded_tab_bar_proxies()

    def _get_or_create_top_frame_widget(self) -> WindowTopFrameBar:
        widget = self._get_live_top_frame_widget()
        height_px = self._top_frame_height_px()
        if widget is None:
            widget = WindowTopFrameBar(self, height_px=height_px, parent=self)
            self._window_top_frame_widget = widget
        else:
            widget.setFixedHeight(height_px)
        return widget

    def _install_custom_window_frame(self) -> None:
        if self._get_live_title_bar_widget() is not None:
            return

        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self._title_bar_widget = CustomWindowTitleBar(self)
        self._title_bar_widget.sectionOrderChanged.connect(self._handle_window_titlebar_section_order_changed)
        self._title_bar_widget.uriAnchorRatioChanged.connect(self._handle_window_titlebar_uri_anchor_ratio_changed)
        self._title_bar_widget.uriTabSlotIndexChanged.connect(self._handle_window_titlebar_uri_tab_slot_index_changed)
        self._title_bar_widget.set_section_order(self._window_titlebar_section_order)
        self._title_bar_widget.set_uri_anchor_ratio(self._window_titlebar_uri_anchor_ratio)
        self._title_bar_widget.set_uri_tab_slot_index(self._window_titlebar_uri_tab_slot_index)
        self._title_bar_widget.set_menu_enabled(bool(self._window_menu_bar and self._window_menu_bar.actions()))
        self.setMenuWidget(self._title_bar_widget)
        self._custom_titlebar_active = True
        self._sync_window_titlebar_scheme()
        self._refresh_window_titlebar_state()
        self._sync_window_titlebar_extensions()

        menu_toggle_action = getattr(self, "menu_visible_action", None)
        if isinstance(menu_toggle_action, QAction):
            menu_toggle_action.setChecked(False)
            menu_toggle_action.setEnabled(False)
            menu_toggle_action.setToolTip("Menue ist ueber das Titlebar-Icon erreichbar.")

        custom_titlebar_action = getattr(self, "act_toggle_custom_titlebar", None)
        if isinstance(custom_titlebar_action, QAction):
            blocker = QtCore.QSignalBlocker(custom_titlebar_action)
            custom_titlebar_action.setChecked(True)
            del blocker

        self._sync_window_chrome_toolbar_visibility()

    @Slot(bool)
    def _set_custom_titlebar_visible(self, visible: bool) -> None:
        if _env_truthy("AI_IDE_DISABLE_CUSTOM_WINDOW_FRAME", "0"):
            self._custom_titlebar_active = False
            return

        titlebar_widget = self._get_live_title_bar_widget()

        if visible:
            if titlebar_widget is None:
                self._install_custom_window_frame()
            else:
                self.setMenuWidget(titlebar_widget)
                self._custom_titlebar_active = True
                titlebar_widget.set_menu_enabled(bool(self._window_menu_bar and self._window_menu_bar.actions()))
                self._sync_window_titlebar_scheme()
                self._refresh_window_titlebar_state()
        else:
            top_frame_widget = self._get_or_create_top_frame_widget()
            try:
                self.setMenuWidget(top_frame_widget)
            except RuntimeError:
                self._title_bar_widget = None
                self._window_top_frame_widget = None
            self._custom_titlebar_active = False

        self._sync_window_titlebar_extensions()
        self._sync_window_chrome_toolbar_visibility()
        self._sync_window_chrome_toolbar_scheme()

    def _sync_toolbar_window_title(self, title: str | None = None) -> None:
        label = self._tb_left_title_label
        if not isinstance(label, QLabel):
            return
        resolved_title = str(title if title is not None else self.windowTitle() or "").strip() or self.WINDOW_TITLE
        label.setText(resolved_title)

    def _sync_toolbar_window_controls(self) -> None:
        scheme = _build_scheme(self._accent, self._base)
        icon_color = str(scheme.get("col6") or "#E3E3DED6")

        min_btn = self._tb_right_min_btn
        if isinstance(min_btn, QToolButton):
            min_btn.setText("")
            min_btn.setIcon(_draw_window_control_icon("minimize", size=14, color=icon_color))
            min_btn.setIconSize(QSize(12, 12))

        close_btn = self._tb_right_close_btn
        if isinstance(close_btn, QToolButton):
            close_btn.setIcon(_draw_window_control_icon("close", size=14, color=icon_color))
            close_btn.setIconSize(QSize(12, 12))

        max_btn = self._tb_right_max_btn
        if isinstance(max_btn, QToolButton):
            max_btn.setIcon(_draw_window_control_icon("maximize", size=14, color=icon_color))
            max_btn.setToolTip("Wiederherstellen" if self.isMaximized() else "Maximieren")
            max_btn.setIconSize(QSize(12, 12))

        fullscreen_btn = self._tb_right_fullscreen_btn
        if isinstance(fullscreen_btn, QToolButton):
            fullscreen_btn.setIcon(_icon("open_in_full_26dp_999999_FILL0_wght500_GRAD0_opsz24.svg"))
            fullscreen_btn.setIconSize(QSize(12, 12))
            fullscreen_btn.setChecked(self.isFullScreen())
            fullscreen_btn.setToolTip("Fullscreen beenden (F11)" if self.isFullScreen() else "Echtes Fullscreen (F11)")

    def _sync_window_chrome_toolbar_scheme(self) -> None:
        scheme = _build_scheme(self._accent, self._base)
        chrome_bg = str(scheme.get("col5") or "#000000")
        fg = str(scheme.get("col6") or "#E3E3DED6")
        border = str(scheme.get("col10") or "#242424")
        drag_bg = str(scheme.get("col7") or "#0b0b0b")
        chrome_widget = self._tb_left_chrome_widget
        if isinstance(chrome_widget, QWidget):
            chrome_widget.setStyleSheet(
                f"background: {chrome_bg};"
                "border: none;"
            )
        title_label = self._tb_left_title_label
        if isinstance(title_label, QLabel):
            title_label.setStyleSheet(
                f"background: {chrome_bg};"
                f"color: {fg};"
                "font-size: 10px;"
                "font-weight: 700;"
                "padding: 0px;"
                "margin: 0px;"
            )
        drag_handle = self._tb_left_drag_handle
        if isinstance(drag_handle, QWidget):
            drag_handle.setStyleSheet(
                f"background: {drag_bg};"
                f"border: 1px dashed {border};"
                "border-radius: 6px;"
            )
        top_frame_widget = self._get_live_top_frame_widget()
        if isinstance(top_frame_widget, QWidget):
            top_frame_widget.setFixedHeight(self._top_frame_height_px())
            frame_bg = str(scheme.get('col5') or '#000000')
            top_frame_widget.setStyleSheet(
                f"background: {frame_bg};"
                f"border-bottom: 1px solid {frame_bg};"
            )
        self._sync_toolbar_window_controls()

    def _set_toolbar_window_menu_enabled(self, enabled: bool) -> None:
        menu_btn = self._tb_left_menu_button
        if isinstance(menu_btn, QToolButton):
            menu_btn.setEnabled(bool(enabled))

    def _sync_toolbar_visibility_menu_actions(self) -> None:
        left_toolbar = getattr(self, "tb_left", None)
        right_toolbar = getattr(self, "tb_right", None)

        left_action = getattr(self, "act_view_sidebar_left", None)
        if isinstance(left_action, QAction):
            blocker = QtCore.QSignalBlocker(left_action)
            left_action.setChecked(bool(isinstance(left_toolbar, QToolBar) and left_toolbar.isVisible()))
            del blocker

        right_action = getattr(self, "act_view_sidebar_right", None)
        if isinstance(right_action, QAction):
            blocker = QtCore.QSignalBlocker(right_action)
            right_action.setChecked(bool(isinstance(right_toolbar, QToolBar) and right_toolbar.isVisible()))
            del blocker

    def _sync_right_workspace_tabs_visibility(self, visible: bool) -> None:
        titlebar_widget = self._get_live_title_bar_widget()
        extensions_widget = getattr(self, "extensions_widget", None)
        if isinstance(extensions_widget, ExtensionsWorkspaceWidget):
            extensions_widget._hide_internal_tab_bar = bool(titlebar_widget is not None)
            extensions_widget._sync_embedded_tab_bar_visibility()

        if titlebar_widget is None:
            return
        if bool(visible):
            self._sync_window_titlebar_extensions()
            return
        tab_proxy_host = self._window_titlebar_tab_proxy_host
        titlebar_widget.set_right_tab_strip_widget(None)
        if isinstance(tab_proxy_host, QWidget):
            try:
                tab_proxy_host.hide()
                tab_proxy_host.setParent(None)
                tab_proxy_host.deleteLater()
            except RuntimeError:
                pass
        self._window_titlebar_tab_proxy_host = None

    def _sync_toolbar_action_symbol_visibility(self) -> None:
        left_toolbar = getattr(self, "tb_left", None)
        if not isinstance(left_toolbar, QToolBar):
            return
        for action_name in (
            "act_toggle_explorer",
            "act_graph_placeholder",
        ):
            toolbar_action = getattr(self, action_name, None)
            if not isinstance(toolbar_action, QAction):
                continue
            toolbar_widget = left_toolbar.widgetForAction(toolbar_action)
            if isinstance(toolbar_widget, QWidget):
                toolbar_widget.setVisible(False)

    @Slot(bool)
    def _set_left_toolbar_visible(self, visible: bool) -> None:
        self._toolbar_left_user_visible = bool(visible)
        self._sync_window_chrome_toolbar_visibility()

    @Slot(bool)
    def _set_right_toolbar_visible(self, visible: bool) -> None:
        self._toolbar_right_user_visible = bool(visible)
        self._sync_window_chrome_toolbar_visibility()

    def _sync_window_chrome_toolbar_visibility(self) -> None:
        self._normalize_toolbar_layout()
        if self._custom_titlebar_active and self._get_live_title_bar_widget() is None:
            self._custom_titlebar_active = False
        custom_frame_disabled = _env_truthy("AI_IDE_DISABLE_CUSTOM_WINDOW_FRAME", "0")
        left_toolbar_visible = bool(self._toolbar_left_user_visible)
        right_toolbar_visible = (not custom_frame_disabled) and bool(self._toolbar_right_user_visible)
        left_chrome_visible = left_toolbar_visible and (not custom_frame_disabled) and (not bool(self._custom_titlebar_active))
        right_chrome_visible = right_toolbar_visible
        if isinstance(getattr(self, "tb_right", None), QToolBar):
            self.tb_right.setVisible(right_toolbar_visible)
        if isinstance(getattr(self, "tb_left", None), QToolBar):
            self.tb_left.setVisible(left_toolbar_visible)
        if isinstance(self._tb_left_chrome_action, QAction):
            self._tb_left_chrome_action.setVisible(left_chrome_visible)
        for action in (
            self._tb_right_min_action,
            self._tb_right_max_action,
            self._tb_right_fullscreen_action,
            self._tb_right_close_action,
        ):
            if isinstance(action, QAction):
                action.setVisible(right_chrome_visible)
        for widget in (
            self._tb_right_min_btn,
            self._tb_right_max_btn,
            self._tb_right_fullscreen_btn,
            self._tb_right_close_btn,
        ):
            if isinstance(widget, QWidget):
                widget.setVisible(right_chrome_visible)
        for widget in (
            self._tb_left_chrome_widget,
            self._tb_left_drag_handle,
        ):
            if isinstance(widget, QWidget):
                widget.setVisible(left_chrome_visible)
        self._sync_toolbar_action_symbol_visibility()
        self._set_toolbar_window_menu_enabled(bool(self._window_menu_bar and self._window_menu_bar.actions()))
        self._sync_toolbar_visibility_menu_actions()

    def _sync_window_titlebar_extensions(self) -> None:
        titlebar_widget = self._get_live_title_bar_widget()

        if titlebar_widget is None:
            tab_proxy_host = self._window_titlebar_tab_proxy_host
            if isinstance(tab_proxy_host, QWidget):
                tab_proxy_host.deleteLater()
            left_tab_proxy_host = getattr(self, "_window_titlebar_left_tab_proxy_host", None)
            if isinstance(left_tab_proxy_host, QWidget):
                left_tab_proxy_host.deleteLater()
            uri_proxy = self._window_titlebar_uri_proxy
            if isinstance(uri_proxy, QWidget):
                uri_proxy.deleteLater()
            self._window_titlebar_tab_proxy_host = None
            self._window_titlebar_left_tab_proxy_host = None
            self._window_titlebar_uri_proxy = None
            return

        extensions_widget = getattr(self, "extensions_widget", None)
        if not self._custom_titlebar_active or not isinstance(extensions_widget, ExtensionsWorkspaceWidget):
            titlebar_widget.set_right_tab_strip_widget(None)
            titlebar_widget.set_external_uri_widget(None)
            self._window_titlebar_tab_proxy_host = None
            self._window_titlebar_left_tab_proxy_host = None
            self._window_titlebar_uri_proxy = None
            return

        titlebar_widget.set_section_order(self._window_titlebar_section_order)
        titlebar_widget.set_uri_anchor_ratio(self._window_titlebar_uri_anchor_ratio)

        uri_proxy = self._window_titlebar_uri_proxy
        if not isinstance(uri_proxy, QLineEdit) or uri_proxy.parent() is None:
            uri_proxy = extensions_widget.create_external_uri_proxy(titlebar_widget)
            self._window_titlebar_uri_proxy = uri_proxy
        uri_proxy.setProperty("extensions_titlebar_uri_tab_mode", True)

        tab_proxy_host = self._window_titlebar_tab_proxy_host
        try:
            tab_proxy_host_parent = tab_proxy_host.parent() if isinstance(tab_proxy_host, QWidget) else None
        except RuntimeError:
            tab_proxy_host = None
            tab_proxy_host_parent = None
        if not isinstance(tab_proxy_host, QWidget) or tab_proxy_host_parent is None:
            tab_proxy_host = extensions_widget.create_titlebar_tab_bar_proxy(titlebar_widget)
            self._window_titlebar_tab_proxy_host = tab_proxy_host
        for orphan_host in titlebar_widget.findChildren(QWidget, "windowFrameExtensionsTabProxyHost"):
            if orphan_host is tab_proxy_host:
                continue
            try:
                orphan_host.hide()
                orphan_host.setParent(None)
                orphan_host.deleteLater()
            except RuntimeError:
                pass

        left_tab_proxy_host = getattr(self, "_window_titlebar_left_tab_proxy_host", None)
        if isinstance(left_tab_proxy_host, QWidget):
            left_tab_proxy_host.deleteLater()
        self._window_titlebar_left_tab_proxy_host = None

        def _tab_row_height(tab_bar: QTabBar | None) -> int:
            if not isinstance(tab_bar, QTabBar):
                return 0
            try:
                tab_count = int(tab_bar.count())
            except RuntimeError:
                return 0

            row_height = 0
            for tab_index in range(tab_count):
                try:
                    tab_rect = tab_bar.tabRect(tab_index)
                except RuntimeError:
                    break
                row_height = max(row_height, int(tab_rect.height()))
            if row_height > 0:
                return row_height

            try:
                return max(0, int(tab_bar.sizeHint().height()))
            except Exception:
                return 0

        proxy_tab_bar = tab_proxy_host.findChild(QTabBar, _WINDOW_FRAME_EMBEDDED_TAB_BAR_OBJECT_NAME)
        source_tab_bar = None
        try:
            source_tab_bar = extensions_widget.extensions_tabs.tabBar()
        except RuntimeError:
            source_tab_bar = None

        target_height = max(16, int(titlebar_widget.height()) - 12)
        titlebar_widget.set_uri_tab_slot_count(max(0, extensions_widget.extensions_tabs.count()))
        titlebar_widget.set_uri_tab_slot_index(self._window_titlebar_uri_tab_slot_index)
        uri_proxy.setLayoutDirection(Qt.LeftToRight)
        uri_proxy.setMinimumHeight(target_height)
        uri_proxy.setMaximumHeight(target_height)
        if isinstance(proxy_tab_bar, QTabBar):
            proxy_tab_bar.setProperty("extensions_titlebar_uri_mode", False)
            if isinstance(proxy_tab_bar, ExtensionsWorkspaceTabBar):
                proxy_tab_bar.set_tab_row_height(target_height)
            proxy_tab_bar.setMinimumHeight(target_height)
            proxy_tab_bar.setMaximumHeight(target_height)
            setattr(proxy_tab_bar, "_titlebar_uri_proxy_source_widget", None)
            proxy_tab_bar.setProperty("extensions_titlebar_uri_slot_index", self._window_titlebar_uri_tab_slot_index)
        self._sync_window_titlebar_tab_slot_split()

        right_workspace_visible = bool(getattr(self, "extensions_dock", None) and self.extensions_dock.isVisible())
        titlebar_widget.set_left_tab_strip_widget(None)
        titlebar_widget.set_right_tab_strip_widget(tab_proxy_host if right_workspace_visible else None)
        titlebar_widget.set_external_uri_widget(uri_proxy)

    def _sync_window_titlebar_scheme(self) -> None:
        titlebar_widget = self._get_live_title_bar_widget()
        if titlebar_widget is not None:
            titlebar_widget.apply_scheme(_build_scheme(self._accent, self._base))
        self._sync_window_titlebar_extensions()
        self._sync_window_chrome_toolbar_scheme()

    def _refresh_window_titlebar_state(self) -> None:
        titlebar_widget = self._get_live_title_bar_widget()
        if titlebar_widget is not None:
            titlebar_widget.set_maximized(self.isMaximized())
        self._sync_toolbar_window_controls()

    def _open_toolbar_window_menu(self) -> None:
        menu_btn = self._tb_left_menu_button
        if isinstance(menu_btn, QToolButton):
            anchor = menu_btn.mapToGlobal(QPoint(0, menu_btn.height()))
            self._show_window_menu_popup(anchor)
            return
        self._show_window_menu_popup(None)

    def _toggle_window_maximize_restore(self) -> None:
        if self.isFullScreen():
            self._set_fullscreen_enabled(False)
            return
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._refresh_window_titlebar_state()

    def _toggle_true_fullscreen_mode(self) -> None:
        self._set_fullscreen_enabled(not self.isFullScreen())

    @Slot(bool)
    def _set_fullscreen_enabled(self, enabled: bool) -> None:
        if enabled:
            self._was_maximized_before_fullscreen = self.isMaximized()
            self.showFullScreen()
        else:
            if self._was_maximized_before_fullscreen:
                self.showMaximized()
            else:
                self.showNormal()
        self._refresh_window_titlebar_state()
        self._sync_fullscreen_action_state()

    def _sync_fullscreen_action_state(self) -> None:
        action = getattr(self, "act_toggle_fullscreen", None)
        if not isinstance(action, QAction):
            return
        blocker = QtCore.QSignalBlocker(action)
        action.setChecked(self.isFullScreen())
        del blocker

    def _show_window_menu_popup(self, anchor: QPoint | None = None) -> None:
        menu_bar = self._window_menu_bar
        if not isinstance(menu_bar, QMenuBar):
            status_bar = self.statusBar() if hasattr(self, "statusBar") else None
            if isinstance(status_bar, QStatusBar):
                status_bar.showMessage("Menue ist nicht initialisiert.", 2600)
            return

        try:
            top_level_actions = list(menu_bar.actions())
        except RuntimeError:
            self._window_menu_bar = None
            status_bar = self.statusBar() if hasattr(self, "statusBar") else None
            if isinstance(status_bar, QStatusBar):
                status_bar.showMessage("Menuemodell wurde invalidiert.", 2600)
            return

        if not top_level_actions:
            status_bar = self.statusBar() if hasattr(self, "statusBar") else None
            if isinstance(status_bar, QStatusBar):
                status_bar.showMessage("Menue enthaelt keine Eintraege.", 2600)
            return

        scheme = _build_scheme(self._accent, self._base)
        popup_bg = str(scheme.get("col5") or scheme.get("col7") or "#000000")
        popup_fg = str(scheme.get("col6") or "#E3E3DED6")
        popup_border = str(scheme.get("col10") or "#242424")
        popup_sel = "rgba(190,190,190,24)"
        popup_style = (
            "QMenu {"
            f" background: {popup_bg};"
            f" color: {popup_fg};"
            f" border: 1px solid {popup_border};"
            " border-radius: 6px;"
            " padding: 4px;"
            " }"
            "QMenu::item {"
            f" color: {popup_fg};"
            " border-radius: 6px;"
            " padding: 5px 16px;"
            " margin: 1px 0px;"
            " }"
            "QMenu::item:selected {"
            f" background: {popup_sel};"
            f" color: {popup_fg};"
            " }"
            "QMenu::separator {"
            f" background: {popup_border};"
            " height: 1px;"
            " margin: 4px 8px;"
            " }"
        )

        popup = QMenu(self)
        popup.setStyleSheet(popup_style)
        has_entries = False
        for top_action in top_level_actions:
            if top_action.isSeparator():
                popup.addSeparator()
                continue

            source_menu = top_action.menu()
            if isinstance(source_menu, QMenu):
                top_popup = popup.addMenu(source_menu.title())
                top_popup.setStyleSheet(popup_style)
                if not source_menu.icon().isNull():
                    top_popup.setIcon(source_menu.icon())
                top_popup.addActions(source_menu.actions())
                has_entries = True
            else:
                popup.addAction(top_action)
                has_entries = True

        if not has_entries:
            return

        self._title_menu_popup = popup
        if anchor is None:
            toolbar_menu_btn = self._tb_left_menu_button
            if isinstance(toolbar_menu_btn, QToolButton) and toolbar_menu_btn.isVisible():
                anchor = toolbar_menu_btn.mapToGlobal(QPoint(0, toolbar_menu_btn.height()))
            else:
                titlebar_widget = self._get_live_title_bar_widget()
                if isinstance(titlebar_widget, QWidget) and titlebar_widget.isVisible():
                    anchor = titlebar_widget.mapToGlobal(QPoint(8, titlebar_widget.height()))
                else:
                    anchor = self.mapToGlobal(QPoint(8, 8))
        popup.exec(anchor)
        self._title_menu_popup = None
    
    # ====================== helper: remove title-bars & buttons ============

    def _strip_dock_decoration(self, dock: QDockWidget) -> None:
        """remove title-bar & buttons, give uniform bg-colour (col5)"""
        scheme = _build_scheme(self._accent, self._base)
        button_bg = str(scheme.get("col5") or "#000000")
        button_hover = button_bg
        button_border = str(scheme.get("col10") or "#242424")
        dock.setTitleBarWidget(QWidget())                       # hide bar
        dock.setFeatures(QDockWidget.NoDockWidgetFeatures)      # no btns
        dock.setStyleSheet(f"""
            background:{scheme.get('col5', '#000000')};
                                /* ← remove remaining frame   */
            QToolButton {{
                background: {button_bg};
                border: 1px solid transparent;
                border-radius: 6px;
            }}
            QToolButton:hover, QToolButton:pressed, QToolButton:checked {{
                background: {button_hover};
                border: 1px solid transparent;
            }}
        """)

    def _editor_surface_enabled(self) -> bool:
        return _env_truthy("AI_IDE_ENABLE_EDITOR_SURFACE", "0")

    def _terminal_surface_enabled(self) -> bool:
        return _env_truthy("AI_IDE_ENABLE_TERMINAL_SURFACE", "0")

    def _configure_workspace_actions(self) -> None:
        editor_enabled = self._editor_surface_enabled()
        terminal_enabled = self._terminal_surface_enabled()

        for action in (
            self.act_new_tab,
            self.act_close_tab,
            self.act_save_tab,
            self.act_save_tab_as,
            self.act_open,
            self.act_toggle_tabdock,
            self.act_clone_tabdock,
        ):
            action.setEnabled(editor_enabled)

        self.act_toggle_console.setEnabled(terminal_enabled)
        if not editor_enabled:
            self.act_toggle_tabdock.setChecked(False)
        if not terminal_enabled:
            self.act_toggle_console.setChecked(False)

    def _open_symbol_database_connection(self) -> None:
        return

    def _set_explorer_toggle_action_checked(self, checked: bool) -> None:
        toggle_action = getattr(self, "act_toggle_explorer", None)
        if not isinstance(toggle_action, QAction):
            return
        previous_state = toggle_action.blockSignals(True)
        toggle_action.setChecked(bool(checked))
        toggle_action.blockSignals(previous_state)

    def _normalize_splitter_sizes(self, sizes: list[int] | tuple[int, ...], *, total: int = 1000) -> list[int]:
        weights = [max(int(value), 0) for value in list(sizes)]
        if not weights:
            return []

        positive_total = sum(weights)
        if positive_total <= 0:
            return [1 for _ in weights]

        remaining_total = max(int(total), len(weights))
        remaining_weight = positive_total
        normalized: list[int] = []
        for weight in weights:
            if weight <= 0:
                normalized.append(0)
                continue
            if remaining_weight <= 0:
                normalized.append(max(1, remaining_total))
                remaining_total = 0
                continue
            mapped = max(1, int(round((weight / remaining_weight) * max(remaining_total, 1))))
            mapped = min(mapped, max(remaining_total, 1))
            normalized.append(mapped)
            remaining_total -= mapped
            remaining_weight -= weight

        return normalized

    def _explorer_splitter_heights(self) -> tuple[int, int]:
        splitter = getattr(self, "explorer_splitter", None)
        if not isinstance(splitter, QSplitter):
            return (0, 0)
        sizes = splitter.sizes()
        if len(sizes) < 2:
            return (0, 0)
        return (max(int(sizes[0]), 0), max(int(sizes[1]), 0))

    def _show_explorer_project_area_only(self) -> None:
        files_dock = getattr(self, "files_dock", None)
        splitter = getattr(self, "explorer_splitter", None)
        if not isinstance(files_dock, QDockWidget) or not isinstance(splitter, QSplitter):
            return

        if not files_dock.isVisible():
            files_dock.show()

        top_height, bottom_height = self._explorer_splitter_heights()
        if top_height > 0 and bottom_height > 0:
            self._explorer_splitter_sizes = [top_height, bottom_height]
        splitter.setSizes([1, 0])
        self._explorer_database_panel_visible = False
        self._set_explorer_toggle_action_checked(True)

        rebalance_columns = getattr(self, "_rebalance_workspace_columns", None)
        if callable(rebalance_columns):
            rebalance_columns()

    def _show_explorer_database_area_only(self) -> None:
        files_dock = getattr(self, "files_dock", None)
        splitter = getattr(self, "explorer_splitter", None)
        if not isinstance(files_dock, QDockWidget) or not isinstance(splitter, QSplitter):
            return

        if not files_dock.isVisible():
            files_dock.show()

        top_height, bottom_height = self._explorer_splitter_heights()
        if top_height > 0 and bottom_height > 0:
            self._explorer_splitter_sizes = [top_height, bottom_height]
        splitter.setSizes([0, 1])
        self._explorer_database_panel_visible = True
        self._set_explorer_toggle_action_checked(True)

        focus_widget = getattr(self, "input_db_connection_name", None)
        if isinstance(focus_widget, QLineEdit):
            focus_widget.setFocus()

        rebalance_columns = getattr(self, "_rebalance_workspace_columns", None)
        if callable(rebalance_columns):
            rebalance_columns()

    def _show_explorer_project_and_database_split(self) -> None:
        files_dock = getattr(self, "files_dock", None)
        splitter = getattr(self, "explorer_splitter", None)
        if not isinstance(files_dock, QDockWidget) or not isinstance(splitter, QSplitter):
            return

        if not files_dock.isVisible():
            files_dock.show()

        top_height, bottom_height = self._explorer_splitter_heights()

        preferred_top, preferred_bottom = list(self._explorer_splitter_sizes)[:2]
        if preferred_top <= 0 or preferred_bottom <= 0:
            preferred_top = max(top_height, 1)
            preferred_bottom = max(bottom_height, 1)
        if preferred_top <= 0 or preferred_bottom <= 0:
            preferred_top, preferred_bottom = (7, 3)

        normalized = self._normalize_splitter_sizes([int(preferred_top), int(preferred_bottom)], total=1000)
        if len(normalized) != 2:
            normalized = [7, 3]

        splitter.setSizes(normalized)
        self._explorer_splitter_sizes = list(normalized)
        self._explorer_database_panel_visible = True
        self._set_explorer_toggle_action_checked(True)

        rebalance_columns = getattr(self, "_rebalance_workspace_columns", None)
        if callable(rebalance_columns):
            rebalance_columns()

    def _toggle_explorer_project_area(self, _checked: bool = False) -> None:
        files_dock = getattr(self, "files_dock", None)
        if not isinstance(files_dock, QDockWidget):
            return

        if not files_dock.isVisible():
            files_dock.show()
            self._set_explorer_toggle_action_checked(True)
            rebalance_columns = getattr(self, "_rebalance_workspace_columns", None)
            if callable(rebalance_columns):
                rebalance_columns()
            return

        files_dock.hide()
        self._set_explorer_toggle_action_checked(False)
        self._explorer_database_panel_visible = False

    def _toggle_explorer_database_area(self) -> None:
        files_dock = getattr(self, "files_dock", None)
        splitter = getattr(self, "explorer_splitter", None)
        status_bar = self.statusBar() if hasattr(self, "statusBar") else None
        if not isinstance(files_dock, QDockWidget) or not isinstance(splitter, QSplitter):
            if status_bar is not None:
                status_bar.showMessage("Database panel is not available.", 3200)
            return

        if not files_dock.isVisible():
            self._show_explorer_database_area_only()
            return

        top_height, bottom_height = self._explorer_splitter_heights()

        # If project area currently fills the dock, click on DB symbol should
        # show both areas in a shared split.
        if top_height > 0 and bottom_height <= 0:
            self._show_explorer_project_and_database_split()
            return

        # If DB area currently fills the dock, clicking DB closes the dock.
        if bottom_height > 0 and top_height <= 0:
            files_dock.hide()
            self._set_explorer_toggle_action_checked(False)
            self._explorer_database_panel_visible = False
            return

        # If both areas are shown, clicking DB hides DB and keeps project area.
        if top_height > 0 and bottom_height > 0:
            self._show_explorer_project_area_only()
            return

        self._show_explorer_database_area_only()

    def _handle_explorer_visibility_change(self, visible: bool) -> None:
        self._set_explorer_toggle_action_checked(bool(visible))
        if not bool(visible):
            self._explorer_database_panel_visible = False

    def _reveal_explorer_database_area(self) -> None:
        files_dock = getattr(self, "files_dock", None)
        if isinstance(files_dock, QDockWidget) and not files_dock.isVisible():
            files_dock.show()

        toggle_action = getattr(self, "act_toggle_explorer", None)
        if isinstance(toggle_action, QAction) and not toggle_action.isChecked():
            previous_state = toggle_action.blockSignals(True)
            toggle_action.setChecked(True)
            toggle_action.blockSignals(previous_state)

        self._show_explorer_project_and_database_split()

        focus_widget = getattr(self, "input_db_connection_name", None)
        if isinstance(focus_widget, QLineEdit):
            focus_widget.setFocus()

    def _build_explorer_database_panel(self, parent: QWidget) -> QFrame:
        panel = QFrame(parent)
        panel.setObjectName("ExplorerDatabasePanel")
        panel.setFrameShape(QFrame.NoFrame)
        panel.setMinimumSize(0, 0)
        panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.MinimumExpanding)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        panel_layout.setSpacing(8)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        panel_title = QLabel("Database Connections", panel)
        panel_title.setObjectName("controlMeta")
        header_layout.addWidget(panel_title, 1)
        panel_layout.addLayout(header_layout)

        form_layout = QFormLayout()
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.input_db_connection_name = QLineEdit(panel)
        self.input_db_connection_name.setPlaceholderText("Connection name")
        form_layout.addRow("Name", self.input_db_connection_name)

        self.input_db_connection_type = QComboBox(panel)
        self.input_db_connection_type.addItems([
            "PostgreSQL",
            "MySQL",
            "MSSQL",
            "MongoDB",
            "SQLite",
            "Custom",
        ])
        form_layout.addRow("Type", self.input_db_connection_type)

        self.input_db_connection_host = QLineEdit(panel)
        self.input_db_connection_host.setPlaceholderText("localhost")
        self.input_db_connection_host.setText("localhost")
        form_layout.addRow("Host", self.input_db_connection_host)

        self.input_db_connection_port = QLineEdit(panel)
        self.input_db_connection_port.setPlaceholderText("5432")
        self.input_db_connection_port.setText("5432")
        form_layout.addRow("Port", self.input_db_connection_port)

        self.input_db_connection_database = QLineEdit(panel)
        self.input_db_connection_database.setPlaceholderText("database")
        form_layout.addRow("Database", self.input_db_connection_database)

        self.input_db_connection_username = QLineEdit(panel)
        self.input_db_connection_username.setPlaceholderText("username")
        form_layout.addRow("Username", self.input_db_connection_username)

        panel_layout.addLayout(form_layout)

        self.btn_explorer_database_submit = QPushButton("Add connection", panel)
        self.btn_explorer_database_submit.setObjectName("controlAction")
        self.btn_explorer_database_submit.setCursor(Qt.PointingHandCursor)
        self.btn_explorer_database_submit.clicked.connect(self._submit_explorer_database_connection)
        panel_layout.addWidget(self.btn_explorer_database_submit, 0, Qt.AlignLeft)
        panel_layout.addStretch(1)

        self._apply_explorer_database_panel_style(panel)

        return panel

    @staticmethod
    def _truncate_projection_value(value: Any, *, max_length: int = 96) -> str:
        text = str(value or "").strip()
        if not text:
            return "not set"
        if len(text) <= max_length:
            return text
        return f"{text[:max_length - 3]}..."

    @staticmethod
    def _mask_projection_uri(value: str) -> str:
        raw_value = str(value or "").strip()
        if not raw_value:
            return ""

        try:
            parsed = urlparse(raw_value)
        except Exception:
            return raw_value

        username = str(parsed.username or "").strip()
        password = parsed.password
        hostname = str(parsed.hostname or "").strip()
        if password is None or not hostname:
            return raw_value

        masked_netloc = f"{username}:***@{hostname}" if username else f"***@{hostname}"
        if parsed.port is not None:
            masked_netloc = f"{masked_netloc}:{parsed.port}"
        return parsed._replace(netloc=masked_netloc).geturl()

    @staticmethod
    def _project_connection_type_from_uri(value: str) -> str:
        try:
            scheme = str(urlparse(str(value or "").strip()).scheme or "").strip().lower()
        except Exception:
            scheme = ""

        scheme_to_type = {
            "postgres": "PostgreSQL",
            "postgresql": "PostgreSQL",
            "mysql": "MySQL",
            "mariadb": "MySQL",
            "mssql": "MSSQL",
            "sqlserver": "MSSQL",
            "mongodb": "MongoDB",
            "sqlite": "SQLite",
        }
        return scheme_to_type.get(scheme, "Custom")

    @staticmethod
    def _project_host_port_from_uri(value: str) -> tuple[str, str]:
        try:
            parsed = urlparse(str(value or "").strip())
        except Exception:
            return ("not set", "not set")

        projected_host = str(parsed.hostname or "").strip() or "not set"
        projected_port = str(parsed.port) if parsed.port is not None else "not set"
        return projected_host, projected_port

    def _submit_explorer_database_connection(self) -> None:
        explorer_widget = getattr(self, "explorer", None)
        tree_widget = getattr(explorer_widget, "tree", None)
        add_to_section = getattr(tree_widget, "add_to_section", None)
        remove_from_section = getattr(tree_widget, "remove_from_section", None)
        status_bar = self.statusBar() if hasattr(self, "statusBar") else None

        if not callable(add_to_section):
            if status_bar is not None:
                status_bar.showMessage("Explorer database store is not available.", 3200)
            return

        name_input = getattr(self, "input_db_connection_name", None)
        type_input = getattr(self, "input_db_connection_type", None)
        host_input = getattr(self, "input_db_connection_host", None)
        port_input = getattr(self, "input_db_connection_port", None)
        database_input = getattr(self, "input_db_connection_database", None)
        username_input = getattr(self, "input_db_connection_username", None)

        connection_name = str(name_input.text() if isinstance(name_input, QLineEdit) else "").strip()
        if not connection_name:
            QMessageBox.information(self, "Database Connection", "Please enter a connection name.")
            if isinstance(name_input, QLineEdit):
                name_input.setFocus()
            return

        connection_type = str(type_input.currentText() if isinstance(type_input, QComboBox) else "PostgreSQL").strip() or "PostgreSQL"
        host_value = str(host_input.text() if isinstance(host_input, QLineEdit) else "localhost").strip() or "localhost"
        database_name = str(database_input.text() if isinstance(database_input, QLineEdit) else "").strip()
        username = str(username_input.text() if isinstance(username_input, QLineEdit) else "").strip()

        port_value_raw = str(port_input.text() if isinstance(port_input, QLineEdit) else "5432").strip() or "5432"
        try:
            port_value = int(port_value_raw)
        except Exception:
            QMessageBox.information(self, "Database Connection", "Port must be a number.")
            if isinstance(port_input, QLineEdit):
                port_input.setFocus()
            return

        connection_data = {
            "name": connection_name,
            "type": connection_type,
            "host": host_value,
            "port": port_value,
            "database": database_name,
            "username": username,
        }

        if callable(remove_from_section):
            while bool(remove_from_section("DATABASES", connection_name)):
                pass
        add_to_section("DATABASES", connection_name, connection_data)

        if status_bar is not None:
            status_bar.showMessage(f"Database connection added: {connection_name}", 3200)

    def _apply_explorer_database_panel_style(self, panel: QFrame | None = None) -> None:
        target_panel = panel if panel is not None else getattr(self, "explorer_database_panel", None)
        if not isinstance(target_panel, QFrame):
            return

        scheme = _build_scheme(self._accent, self._base)
        target_panel.setStyleSheet(
            f"QFrame#ExplorerDatabasePanel {{"
            f" background: {scheme.get('col9', '#101010')};"
            f" border: 1px solid {scheme.get('col10', '#1f1f1f')};"
            " border-top-left-radius: 14px;"
            " border-top-right-radius: 14px;"
            " border-bottom-left-radius: 14px;"
            " border-bottom-right-radius: 14px;"
            " }"
            "QFrame#ExplorerDatabasePanel QLabel {"
            " background: transparent;"
            " border: none;"
            " }"
            f"QFrame#ExplorerDatabasePanel QLineEdit,"
            f"QFrame#ExplorerDatabasePanel QComboBox {{"
            f" background: {scheme.get('col9', '#101010')};"
            f" color: {scheme.get('col6', '#E3E3DED6')};"
            f" border: 1px solid {scheme.get('col10', '#1f1f1f')};"
            " border-radius: 8px;"
            " padding: 4px 8px;"
            " }"
            f"QFrame#ExplorerDatabasePanel QLineEdit:focus,"
            f"QFrame#ExplorerDatabasePanel QComboBox:focus {{"
            f" background: {scheme.get('col5', '#000000')};"
            f" border-color: {scheme.get('col10', '#1f1f1f')};"
            " }"
        )

    def _remember_explorer_splitter_sizes(self, *_args: Any) -> None:
        splitter = getattr(self, "explorer_splitter", None)
        if not isinstance(splitter, QSplitter):
            return

        sizes = splitter.sizes()
        if len(sizes) < 2:
            return

        top_size = int(sizes[0]) if len(sizes) > 0 else 0
        bottom_size = int(sizes[1]) if len(sizes) > 1 else 0
        if top_size > 0 and bottom_size > 0:
            self._explorer_splitter_sizes = [top_size, bottom_size]
    # ================================================= seitliche Widgets ===

    def _create_side_widgets(self):

        # ---------- Explorer-Dock (multi-root) -------------------------------

        self.files_dock = QDockWidget("Explorer", self)
        self.files_dock.setObjectName("FilesDock")
        self.files_dock.setMinimumSize(0, 0)
        self.files_dock.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

        disable_explorer = _env_truthy("AI_IDE_DISABLE_EXPLORER", "0")
        if not disable_explorer:
            # Use new multi-root tree widget with toolbar
            self.explorer = JsonTreeWidgetWithToolbar()
            self.explorer.setMinimumSize(0, 0)
            self.explorer.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            self._bind_explorer_initial_load_refresh()
            if hasattr(self.explorer, "tree") and isinstance(self.explorer.tree, QTreeWidget):
                self.explorer.tree.setMinimumSize(0, 0)
                self.explorer.tree.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            self.explorer.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
            self.explorer_splitter = None
            self.explorer_database_panel = None
            self.btn_explorer_database_submit = None
            self.input_db_connection_name = None
            self.input_db_connection_type = None
            self.input_db_connection_host = None
            self.input_db_connection_port = None
            self.input_db_connection_database = None
            self.input_db_connection_username = None
            self._explorer_database_panel_visible = False
            self.files_dock.setWidget(self.explorer)
            self._apply_main_splitter_style()
        else:
            self.explorer_splitter = None
            self.explorer_database_panel = None
            self.btn_explorer_database_submit = None
            self.input_db_connection_name = None
            self.input_db_connection_type = None
            self.input_db_connection_host = None
            self.input_db_connection_port = None
            self.input_db_connection_database = None
            self.input_db_connection_username = None
            self._explorer_database_panel_visible = False
            self.explorer = None
            self.files_dock.setWidget(QWidget())
        self._strip_dock_decoration(self.files_dock)

        # Add example workspace structure
        self._initialize_explorer_workspace()
              
        # ----------- set highlighting for QTextEdit Widget (self) ---------
        # ---------- Chat-Dock  --------------------------------------------

        disable_chat = _env_truthy("AI_IDE_DISABLE_CHAT", "0")
        if not disable_chat:
            self.chat_dock = ChatDock(self._accent, self._base, self)
        else:
            # Minimal placeholder to keep layout + settings code intact.
            self.chat_dock = QDockWidget("AI Chat", self)
            self.chat_dock.setObjectName("ChatDock")
            self.chat_dock.setTitleBarWidget(QWidget())
            self.chat_dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
            self.chat_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
            self.chat_dock.setWidget(QWidget())
        self.chat_dock.setMinimumSize(0, 0)
        self.chat_dock.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        chat_widget = self.chat_dock.widget()
        if isinstance(chat_widget, QWidget):
            chat_widget.setMinimumSize(0, 0)
            chat_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        if isinstance(chat_widget, AIWidget):
            if self.explorer is not None:
                scheme = _build_scheme(self._accent, self._base)
                # Keep explorer palette aligned with the active app scheme.
                self.explorer.set_text_color(scheme.get("col6", "#E3E3DED6"))
                self.explorer.set_background_color(scheme.get("col7", "#0b0b0b"))
                self.explorer.set_accent_color(scheme.get("col1", "#0fe913"))

        disable_control_plane = _env_truthy("AI_IDE_DISABLE_CONTROL_PLANE", "0")
        # Create ControlPlaneWidget and pass it to ExtensionsWorkspaceWidget
        if not disable_control_plane:
            self.control_plane_widget = ControlPlaneWidget(self._accent, self._base, self)
            self.control_plane_widget.setMinimumSize(0, 0)
            self.control_plane_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            if self.control_plane_widget is not None:
                self.control_plane_widget.snapshotChanged.connect(self._update_control_plane_status)
        else:
            self.control_plane_widget = None

        # Create extensions dock and pass control_plane_widget to it
        disable_extensions = _env_truthy("AI_IDE_DISABLE_EXTENSIONS_1", "0")
        self.extensions_dock = QDockWidget("Extensions & Control Plane", self)
        self.extensions_dock.setObjectName("ExtensionsDock")
        self.extensions_dock.setTitleBarWidget(QWidget())
        self.extensions_dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.extensions_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.extensions_dock.setMinimumSize(0, 0)
        self.extensions_dock.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        if not disable_extensions:
            self.extensions_widget = ExtensionsWorkspaceWidget(
                self._accent,
                self._base,
                self,
                control_plane_widget_ref=self.control_plane_widget,
                hide_internal_tab_bar=not _env_truthy("AI_IDE_DISABLE_CUSTOM_WINDOW_FRAME", "0"),
            )
            self.extensions_widget.setMinimumSize(0, 0)
            self.extensions_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            self.extensions_dock.setWidget(self.extensions_widget)
        else:
            self.extensions_widget = None
            self.extensions_dock.setWidget(QWidget())
        
        # Keep control_plane_dock for backward compatibility (hidden)
        self.control_plane_dock = None  # No longer used; merged into extensions_dock
    
    def _initialize_explorer_workspace(self):
        """Initialize example workspace structure in the explorer."""
        import os

        if getattr(self, "explorer", None) is None:
            return
        
        # Add current project
        project_path = os.path.dirname(os.path.abspath(__file__))

        agents_runtime_path = Path(project_path) / "agents_runtime.py"
        runtime_projection = self._load_agents_runtime_projection(agents_runtime_path)
        remove_from_section = getattr(self.explorer, "remove_from_section", None)
        if callable(remove_from_section):
            while bool(remove_from_section("PROJECTS", "agents_runtime")):
                pass
        if runtime_projection:
            self._upsert_explorer_item("RUNTIME", "agents_runtime", runtime_projection)

    def _bind_explorer_initial_load_refresh(self) -> None:
        explorer = getattr(self, "explorer", None)
        tree_widget = getattr(explorer, "tree", None)
        load_signal = getattr(tree_widget, "_initial_load_async_result_ready", None)
        if load_signal is None:
            return
        try:
            load_signal.connect(self._on_explorer_initial_load_result)
        except Exception:
            pass

    @Slot(object)
    def _on_explorer_initial_load_result(self, _payload: object) -> None:
        explorer = getattr(self, "explorer", None)
        tree_widget = getattr(explorer, "tree", None)
        load_signal = getattr(tree_widget, "_initial_load_async_result_ready", None)
        if load_signal is not None:
            try:
                load_signal.disconnect(self._on_explorer_initial_load_result)
            except Exception:
                pass
        self._initialize_explorer_workspace()

    def _upsert_explorer_item(self, section_name: str, key: str, value: Any) -> None:
        explorer = getattr(self, "explorer", None)
        if explorer is None:
            return

        remove_from_section = getattr(explorer, "remove_from_section", None)
        add_to_section = getattr(explorer, "add_to_section", None)
        if not callable(add_to_section):
            return

        if callable(remove_from_section):
            while bool(remove_from_section(section_name, key)):
                pass

        add_to_section(section_name, key, value)

    def trigger_explorer_manual_sync(self, *, source_label: str = "manual_sync") -> bool:
        explorer = getattr(self, "explorer", None)
        if explorer is None:
            return False

        manual_sync_runner = getattr(explorer, "run_manual_sync", None)
        if not callable(manual_sync_runner):
            tree_widget = getattr(explorer, "tree", None)
            manual_sync_runner = getattr(tree_widget, "run_manual_sync", None) if tree_widget is not None else None
        if not callable(manual_sync_runner):
            return False

        try:
            sync_ok = bool(manual_sync_runner(source_label=source_label))
        except Exception as exc:
            try:
                self.statusBar().showMessage(f"Explorer /sync failed: {exc}", 4500)
            except Exception:
                pass
            return False

        try:
            if sync_ok:
                self.statusBar().showMessage("Explorer /sync completed", 2500)
                self._initialize_explorer_workspace()
            else:
                self.statusBar().showMessage("Explorer /sync failed", 4500)
        except Exception:
            pass
        return sync_ok

    def _load_agents_runtime_projection(self, agents_runtime_path: Path) -> dict[str, Any]:
        source_path = Path(agents_runtime_path)
        if not source_path.is_file():
            return {}

        tree_widget = getattr(getattr(self, "explorer", None), "tree", None)
        projection_loader = getattr(tree_widget, "_load_python_module_projection", None)
        if callable(projection_loader):
            try:
                projection = projection_loader(str(source_path))
                if isinstance(projection, dict) and projection:
                    return projection
            except Exception:
                pass

        # Fallback: import the module and project uppercase runtime symbols.
        try:
            module_name = f"runtime_projection_{uuid.uuid4().hex}"
            spec = importlib.util.spec_from_file_location(module_name, str(source_path))
            if spec is None or spec.loader is None:
                return {}
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            return {
                "module_path": str(source_path),
                "module_name": source_path.stem,
                "symbol_count": 0,
                "symbols": {},
                "error": f"{type(exc).__name__}: {exc}",
            }

        symbols: dict[str, Any] = {}
        for symbol_name, symbol_value in vars(module).items():
            normalized_name = str(symbol_name or "").strip()
            if not normalized_name or normalized_name.startswith("__"):
                continue
            if not normalized_name.lstrip("_").isupper():
                continue
            try:
                json.dumps(symbol_value, ensure_ascii=False)
                symbols[normalized_name] = symbol_value
            except Exception:
                symbols[normalized_name] = str(symbol_value)

        return {
            "module_path": str(source_path),
            "module_name": source_path.stem,
            "symbol_count": len(symbols),
            "symbols": symbols,
        }

    # ================================================= zentraler Splitter ==
    
    def _create_central_splitters(self):
        self._strip_dock_decoration(self.files_dock)
        self._strip_dock_decoration(self.chat_dock)
        self._strip_dock_decoration(self.extensions_dock)

        self.setMinimumSize(0, 0)
        for dock in (self.files_dock, self.chat_dock, self.extensions_dock):
            if isinstance(dock, QDockWidget):
                dock.setMinimumSize(0, 0)
                dock.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)

        self.main_split = QSplitter(Qt.Horizontal, self)
        self.main_split.setObjectName("mainHorizontalSplitter")
        self.main_split.setChildrenCollapsible(True)
        self.main_split.setHandleWidth(4)
        self.main_split.setOpaqueResize(True)
        self.main_split.addWidget(self.files_dock)       # links
        self.main_split.addWidget(self.chat_dock)        # mitte
        self.main_split.addWidget(self.extensions_dock)  # extensions & control plane (merged)
        for index in range(self.main_split.count()):
            try:
                self.main_split.setCollapsible(index, True)
            except Exception:
                pass
        self.main_split.setStretchFactor(0, 1)
        self.main_split.setStretchFactor(1, 3)
        self.main_split.setStretchFactor(2, 2)
        self.main_split.setStretchFactor(3, 2)
        default_sizes = list(getattr(self, "_workspace_column_widths", [260, 760, 460, 180]))
        if len(default_sizes) != 4:
            default_sizes = [260, 760, 460, 180]
        self.main_split.setSizes(self._normalize_splitter_sizes(default_sizes, total=1000))
        self.main_split.splitterMoved.connect(self._remember_workspace_column_widths)
        self._remember_workspace_column_widths()
        self._apply_main_splitter_style()

        self.setCentralWidget(self.main_split)

        self._create_console_dock()
        self.console_dock.hide()

    # ----------------------------------------------------------------------
    
    def _create_console_dock(self):
        """
        Creates and configures the console dock widget for the application.

        This method initializes a QDockWidget labeled "Console", sets its object name,
        creates a QTextEdit widget for displaying console output, and adds it to the dock.
        It also removes the dock's default decorations. The dock stays detached from the
        active workspace layout while the terminal surface is temporarily disabled.

        Side Effects:
            - Modifies self.console_dock and self.console_widget attributes.
        """
        self.console_dock = QDockWidget("Console", self)
        self.console_dock.setObjectName("ConsoleDock")
        self.console_widget = QTextEdit("Console temporarily disabled")
        self.console_widget.setReadOnly(True)
        self.console_dock.setWidget(self.console_widget)
        self._strip_dock_decoration(self.console_dock)

    def _apply_main_splitter_style(self) -> None:
        scheme = _build_scheme(self._accent, self._base)
        handle_idle, handle_hover, handle_pressed = _splitter_handle_palette(scheme)
        splitter_bg = str(scheme.get("col5") or "#000000")
        main_splitter = getattr(self, "main_split", None)
        if isinstance(main_splitter, QSplitter):
            main_splitter.setStyleSheet(
                f"""
                QSplitter#mainHorizontalSplitter {{
                    background: {splitter_bg};
                }}
                QSplitter#mainHorizontalSplitter::handle:vertical {{
                    background: {handle_idle};
                    margin: 12px 0px;
                    min-width: 4px;
                    border-radius: 999px;
              }}
                QSplitter#mainHorizontalSplitter::handle:horizontal {{
                    background: transparent;
                    margin: 0px 12px;
                    min-height: 7px;
                    border-radius: 999px;
              }}
                QSplitter#mainHorizontalSplitter::handle:hover {{
                    background: {handle_hover};
                }}
                QSplitter#mainHorizontalSplitter::handle:pressed {{
                    background: {handle_pressed};
                }}
                """
            )

        explorer_splitter = getattr(self, "explorer_splitter", None)
        if isinstance(explorer_splitter, QSplitter):
            explorer_splitter.setStyleSheet(
                f"""
                QSplitter#ExplorerDockSplitter {{
                    background: {splitter_bg};
                }}
                QSplitter#ExplorerDockSplitter::handle:vertical {{
                    background: {handle_idle};
                    margin: 12px 0px;
                    min-width: 4px;
                    border-radius: 999px;
              }}
                QSplitter#ExplorerDockSplitter::handle:horizontal {{
                    background: transparent;
                    margin: 0px 12px;
                    min-height: 7px;
                    border-radius: 999px;
              }}
                QSplitter#ExplorerDockSplitter::handle:hover {{
                    background: {handle_hover};
                }}
                QSplitter#ExplorerDockSplitter::handle:pressed {{
                    background: {handle_pressed};
                }}
                """
            )

    # ----------------------------------------------------------------------
    """ URGENTLY SET FOCUS ON DOCS AND TABS """             """TODO File operations musst be processes on focused tab & doc
                                                            def _clone_tab_dock(self, set_current: bool = False) -> None:
                                                            current content have to be reloaded at next start up, there fore using path param
                                                            and tab doc id stored in history within a massage object] """
    def _add_initial_tab_dock(self):
        self._clone_tab_dock(set_current = True)

    def _chat_symbol_icon(self) -> QIcon:
        scheme = _build_scheme(self._accent, self._base)
        prompt_border_color = str(scheme.get("col2") or scheme.get("col1") or "#58ed5b")
        bubble_icon = _icon("chat_bubble_24dp_B7B7B7_FILL0_wght400_GRAD0_opsz24.svg")
        if not bubble_icon.isNull():
            return bubble_icon
        return _draw_fallback("(/)", prompt_border_color)

    def _tinted_symbol_icon(self, symbol_name: str, color_value: str, *, content_scale: float = 1.0) -> QIcon:
        source_icon = _icon(symbol_name)
        source_pixmap = source_icon.pixmap(24, 24)
        if source_pixmap.isNull():
            return source_icon
        tinted_pixmap = QPixmap(source_pixmap.size())
        tinted_pixmap.fill(Qt.transparent)
        painter = QPainter(tinted_pixmap)
        draw_width = source_pixmap.width()
        draw_height = source_pixmap.height()
        try:
            scale_value = float(content_scale)
        except Exception:
            scale_value = 1.0
        if scale_value <= 0.0:
            scale_value = 1.0
        draw_width = max(1, int(round(draw_width * scale_value)))
        draw_height = max(1, int(round(draw_height * scale_value)))
        draw_x = int((tinted_pixmap.width() - draw_width) / 2)
        draw_y = int((tinted_pixmap.height() - draw_height) / 2)
        painter.drawPixmap(QRect(draw_x, draw_y, draw_width, draw_height), source_pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(tinted_pixmap.rect(), QColor(str(color_value or "#58ed5b")))
        painter.end()
        return QIcon(tinted_pixmap)

    def _scaled_symbol_icon(self, symbol_name: str, *, content_scale: float = 1.0) -> QIcon:
        source_icon = _icon(symbol_name)
        source_pixmap = source_icon.pixmap(24, 24)
        if source_pixmap.isNull():
            return source_icon
        if abs(float(content_scale) - 1.0) < 0.001:
            return source_icon

        scaled_pixmap = QPixmap(source_pixmap.size())
        scaled_pixmap.fill(Qt.transparent)
        painter = QPainter(scaled_pixmap)
        draw_width = source_pixmap.width()
        draw_height = source_pixmap.height()
        try:
            scale_value = float(content_scale)
        except Exception:
            scale_value = 1.0
        if scale_value <= 0.0:
            scale_value = 1.0
        draw_width = max(1, int(round(draw_width * scale_value)))
        draw_height = max(1, int(round(draw_height * scale_value)))
        draw_x = int((scaled_pixmap.width() - draw_width) / 2)
        draw_y = int((scaled_pixmap.height() - draw_height) / 2)
        painter.drawPixmap(QRect(draw_x, draw_y, draw_width, draw_height), source_pixmap)
        painter.end()
        return QIcon(scaled_pixmap)

    def _explorer_toolbar_icon(self) -> QIcon:
        return self._scaled_symbol_icon(
            "network_intel_node_24dp_B7B7B7_FILL0_wght400_GRAD0_opsz24.svg",
            content_scale=1.18,
        )

    def _refresh_chat_toggle_icons(self) -> None:
        chat_icon = self._chat_symbol_icon()
        if hasattr(self, "act_toggle_chat") and isinstance(self.act_toggle_chat, QAction):
            self.act_toggle_chat.setIcon(chat_icon)
        if hasattr(self, "act_toggle_control_plane_left") and isinstance(self.act_toggle_control_plane_left, QAction):
            self.act_toggle_control_plane_left.setIcon(chat_icon)
        if hasattr(self, "act_toggle_explorer") and isinstance(self.act_toggle_explorer, QAction):
            self.act_toggle_explorer.setIcon(self._explorer_toolbar_icon())

    # ================================================= actions ============
    
    def _create_actions(self):
        """
        Creates and initializes all QAction objects used in the application's UI, including file operations,
        UI toggles, and tool actions. Sets up icons, tooltips, checkable states, and connects actions to their
        respective slots or visibility toggles. Actions include:
        - Opening and closing tabs
        - Toggling accent color
        - Showing/hiding the AI chat dock
        - Enabling/disabling greyscale mode
        - Showing/hiding the project explorer, tab dock, and console
        - Cloning the tab dock
        - Opening files and displaying the About dialog
        Also connects toggled signals to the appropriate UI components to manage their visibility.
        """
        sty = self.style()

        # ---- file / misc -------------------------------------------------
       
        self.act_new_tab = QAction(
            _icon("open_file.svg"),
            "",
            self,
            triggered=self._new_tab,
        )
        self.act_save_tab = QAction(
            _icon("save.svg"),
            "",
            self,
            triggered=self._save_current_tab,
        )
        self.act_close_tab = QAction(
            _icon("close.svg"), 
            "", self, 
            triggered = self.
            _close_tab
            )

        self.act_toggle_accent = QAction(
            _draw_circle_icon(),
            "Color Scheme", self,
            triggered = self.
            _toggle_accent
            )

        self.act_toggle_accent.setToolTip(
            f"Farbschema wechseln (aktuell: {_normalize_accent_name(getattr(self, '_accent_name', 'green'))})"
        )

        self.act_toggle_fullscreen = QAction(
            "Fullscreen",
            self,
            checkable=True,
            shortcut=QKeySequence("F11"),
            toggled=self._set_fullscreen_enabled,
        )
        self.act_toggle_fullscreen.setToolTip("Echtes Fullscreen (F11)")

        # ---------- NEU: Chat-Toggle --------------- # <– 10.07.2025 ---------

        self.act_toggle_chat = QAction(
            self._chat_symbol_icon(),
            "Chat", self, 
            checkable = True, 
            checked = True
            )

        self.act_toggle_chat.setToolTip("AI-Chat anzeigen/ausblenden")

        self.act_toggle_control_plane = QAction(
            _icon("dashboard_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg"),
            "Control Plane",
            self,
            checkable=True,
            checked=True,
        )
        self.act_toggle_control_plane.setToolTip("Configuration- und Monitoring-Panel anzeigen/ausblenden")
        self.act_toggle_control_plane.toggled.connect(self.extensions_dock.setVisible)

        self.act_refresh_control_plane = QAction(
            _icon("reload_.svg"),
            "Refresh Control Plane",
            self,
            triggered=self._refresh_control_plane,
        )
        self.act_refresh_control_plane.setToolTip("Control Plane aktualisieren")

        # ---------- Sichtbarkeit verknüpfen --------- # <– 10.07.2025 --------
        self.act_toggle_chat.toggled.connect(self.chat_dock.setVisible)

        # ---------- Right-Dock Toggle (for right side-toolbar) --------------
        # Uses panel-style icons instead of the chat glyph.
        self.act_toggle_right_dock = QAction(
            _icon("dashboard_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg"),
            "Dashboard",
            self,
            checkable=True,
            checked=True,
        )
        self.act_toggle_right_dock.setToolTip("Dashboard anzeigen/ausblenden")
        self.act_toggle_right_dock.toggled.connect(self.extensions_dock.setVisible)

        self.act_toggle_control_plane_left = QAction(
            self._chat_symbol_icon(),
            "Chat",
            self,
            checkable=True,
            checked=True,
        )
        self.act_toggle_control_plane_left.setToolTip("AI-Chat anzeigen/ausblenden")
        self.act_toggle_control_plane_left.toggled.connect(self.chat_dock.setVisible)

        # Greyscale toggle ----------------------------------------------------
        self.act_grey = QAction(
            "Greyscale", self, 
            checkable=True, 
            toggled=self
            ._toggle_grey
            )

        # ---- hide or view toggles ---------------------------------------
        # toolbar shows only icons – menu still shows the descriptive text
        
        # ---- project-overview / explorer ---------------------------------
        self.act_toggle_explorer = QAction(
            self._explorer_toolbar_icon(),
            "Explorer", self,
            checkable=True, checked=True
        )

        self.act_toggle_explorer.setToolTip("Project-Explorer anzeigen")

        self.act_graph_placeholder = QAction(
            self._scaled_symbol_icon(
                "network_intel_node_24dp_B7B7B7_FILL0_wght400_GRAD0_opsz24.svg",
                content_scale=1.18,
            ),
            "Extensions 1",
            self,
            checkable=True,
            checked=True,
        )
        self.act_graph_placeholder.setToolTip("Extensions 1 anzeigen/ausblenden")


        # ---- tabable dock ------------------------------------------------
        self.act_toggle_tabdock = QAction(
             _icon("add_tab_dock.svg"),              # Symbols/tabs.svg
             "Tab-Dock", self,
             checkable=True, checked=True
             )
        
        # self.act_toggle_tabdock.setToolTip("Tab-Dock anzeigen")
        self.act_toggle_console = QAction(
            _icon("console.svg"),                    # Symbols/console.svg
            "Console", self,
            checkable=True, checked=False
            )
        
        self.act_toggle_console.setToolTip("Konsole anzeigen")      

        # ---- clone -------------------------------------------------------
        self.act_clone_tabdock = QAction(
            _icon("add_tab_dock.svg"), "", 
            self, triggered = self._clone_tab_dock
            )

        # ---- open / about ------------------------------------------------
        self.act_open = QAction(_icon("explorer.svg"),
            "", triggered=self
            ._open_file,
            )
        
         # ---------- SAVE / SAVE-AS ---------------------------------------------
        #  NEU  –  Speichern unter …

        self.act_save_tab = QAction(
            _icon("save_.svg"), "", self,
            shortcut="Ctrl+S",
            triggered=self._save_current_tab
        )

        self.act_save_tab.setToolTip("save")

        #  NEU  –  Speichern unter …
        self.act_save_tab_as = QAction(
            _icon("save_as_.svg"), "", self,
            shortcut="Ctrl+Shift+S",
            triggered=self._save_current_tab_as
        )

        self.act_save_tab_as.setToolTip("save as")

        self.act_about = QAction(sty.standardIcon(
                QStyle.SP_MessageBoxInformation), "",
                self, triggered = self
                ._about
                )
        # connect visibility actions
        self.act_toggle_explorer.triggered.connect(self._toggle_explorer_project_area)
        self.act_graph_placeholder.toggled.connect(self.extensions_dock.setVisible)
        
        self.act_toggle_tabdock.toggled.connect(
            lambda v:[ 
            d.setVisible(v) for d in self._tab_docks]
                                                )
        
        self.act_toggle_console.toggled.connect(
            self.console_dock
                                     .setVisible
                                                )
        
        self.act_clone_tabdock.triggered.connect(
            self._clone_tab_dock)

        self._configure_workspace_actions()

    # <– changes 10.07.2025
    # ================================================= toolbars ===========

    def _create_toolbars(self):
        """
        Creates and configures the main and side toolbars for the application window.
        - Initializes the top toolbar (`tb_top`) with a custom icon size (3 pixels larger than the default).
        - Adds a set of predefined actions to the top toolbar.
        - Initializes the right (`tb_right`) vertical toolbar, applying the same icon size as the top toolbar.
        - Adds the control plane actions to the side toolbar.
        """
        scheme = _build_scheme(self._accent, self._base)
        chrome_bg = str(scheme.get("col5") or "#000000")
        chrome_hover = chrome_bg
        button_bg = chrome_bg
        button_border = str(scheme.get("col10") or "#242424")
        self.tb_top = QToolBar("Main", self)
        # QMainWindow.saveState/restoreState rely on unique objectName values.
        self.tb_top.setObjectName("ToolbarTop")
        self.tb_top.setMovable(False)
        self.tb_top.setFloatable(False)
        self.tb_top.setContentsMargins(8, 8, 8, 8)
        self.tb_top.setStyleSheet(
            f"QToolBar {{"
            f" background: {chrome_bg};"
            " border: none;"
            " border-radius: 0px;"
            " padding: 6px;"
            " spacing: 8px;"
            " }"
            f"QToolButton {{"
            f" background: {button_bg};"
            " min-width: 40px;"
            " min-height: 40px;"
            " padding: 4px;"
            " margin: 1px;"
            " border: 1px solid transparent;"
            " border-radius: 6px;"
            " }"
            f"QToolButton:hover, QToolButton:pressed, QToolButton:checked {{"
            f" background: {chrome_hover};"
            " border: 1px solid transparent;"
            " }"
        )

        """ +3 px auf die Standard-Icongröße der Toolbar addieren """

        base = self.tb_top.iconSize()                   # z. B. 24 px
        self.tb_top.setIconSize(QSize(base.width() + 5,
                          base.height() + 5))

        self.addToolBar(Qt.TopToolBarArea, self.tb_top)
        self.tb_top.addActions([
            self.act_toggle_explorer,
            self.act_graph_placeholder,
        ])
        self._tb_top_spacer = QWidget(self.tb_top)
        self._tb_top_spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._tb_top_spacer.setStyleSheet(f"background: {chrome_bg}; border: none;")
        self.tb_top.addWidget(self._tb_top_spacer)

        # ---------------- seitliche Toolbars ------------------------------- 

        self.tb_right = QToolBar(self, orientation=Qt.Vertical)
        self.tb_right.setObjectName("ToolbarRight")
        side_toolbar_width = 56
        right_toolbar_width = 56

        # auch hier die größere Icongröße übernehmen

        for bar in (self.tb_right,):
            bar.setIconSize(self.tb_top.iconSize())
            bar.setToolButtonStyle(Qt.ToolButtonIconOnly)
            bar.setMovable(False)
            bar.setFloatable(False)
            bar.setContextMenuPolicy(Qt.PreventContextMenu)
            bar.setFixedWidth(right_toolbar_width)
            bar.setLayoutDirection(Qt.RightToLeft)
            bar.setContentsMargins(0, 8, 0, 8)
            bar.setStyleSheet(
                f"QToolBar {{"
                f" background: {chrome_bg};"
                " border: none;"
                " border-radius: 0px;"
                " padding: 6px 0px 6px 0px;"
                " spacing: 8px;"
                " }"
                "QToolBar::handle {"
                " width: 0px;"
                " height: 0px;"
                " margin: 0px;"
                " padding: 0px;"
                " image: none;"
                " }"
                "QToolBarExtension {"
                " width: 0px;"
                " height: 0px;"
                " image: none;"
                " border: none;"
                " margin: 0px;"
                " padding: 0px;"
                " }"
                f"QToolButton {{"
                f" background: {button_bg};"
                " min-width: 42px;"
                " min-height: 42px;"
                " padding: 4px;"
                " margin: 1px 0px 1px 0px;"
                " border: 1px solid transparent;"
                " border-radius: 6px;"
                " }"
                "QToolButton#toolbarWindowCloseButton, QToolButton#toolbarWindowMinButton, QToolButton#toolbarWindowMaxButton, QToolButton#toolbarWindowFullscreenButton {"
                " min-width: 16px;"
                " min-height: 16px;"
                " max-width: 16px;"
                " max-height: 16px;"
                " padding: 0px;"
                " margin: 0px;"
                " border-radius: 3px;"
                " }"
                f"QToolButton:hover, QToolButton:pressed, QToolButton:checked {{"
                f" background: {chrome_hover};"
                " border: 1px solid transparent;"
                " }"
            )
            self.addToolBar(Qt.RightToolBarArea, bar)

        self._tb_right_min_btn = None
        self._tb_right_max_btn = None
        self._tb_right_fullscreen_btn = None
        self._tb_right_close_btn = None
        self._tb_right_min_action = None
        self._tb_right_max_action = None
        self._tb_right_fullscreen_action = None
        self._tb_right_close_action = None

        self.tb_right.addAction(self.act_toggle_right_dock)

        self._tb_right_spacer = QWidget(self.tb_right)
        self._tb_right_spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._tb_right_spacer.setStyleSheet(f"background: {chrome_bg}; border: none;")
        self.tb_right.addWidget(self._tb_right_spacer)

        self.tb_left = QToolBar(self, orientation=Qt.Vertical)
        self.tb_left.setObjectName("ToolbarLeft")
        self.tb_left.setIconSize(self.tb_top.iconSize())
        self.tb_left.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.tb_left.setMovable(False)
        self.tb_left.setFloatable(False)
        self.tb_left.setContextMenuPolicy(Qt.PreventContextMenu)
        self.tb_left.setFixedWidth(side_toolbar_width)
        self.tb_left.setLayoutDirection(Qt.LeftToRight)
        self.tb_left.setContentsMargins(8, 8, 8, 8)
        self.tb_left.setStyleSheet(
            f"QToolBar {{"
            f" background: {chrome_bg};"
            " border: none;"
            " border-radius: 0px;"
            " padding: 6px 6px 6px 0px;"
            " spacing: 8px;"
            " }"
            "QToolBar::handle {"
            " width: 0px;"
            " height: 0px;"
            " margin: 0px;"
            " padding: 0px;"
            " image: none;"
            " }"
            "QToolBarExtension {"
            " width: 0px;"
            " height: 0px;"
            " image: none;"
            " border: none;"
            " margin: 0px;"
            " padding: 0px;"
            " }"
            f"QToolButton {{"
            f" background: {button_bg};"
            " min-width: 42px;"
            " min-height: 42px;"
            " padding: 4px;"
            " margin: 1px 0px 1px 0px;"
            " border: 1px solid transparent;"
            " border-radius: 6px;"
            " }"
            f"QToolButton:hover, QToolButton:pressed, QToolButton:checked {{"
            f" background: {chrome_hover};"
            " border: 1px solid transparent;"
            " }"
        )
        self.addToolBar(Qt.LeftToolBarArea, self.tb_left)

        self._tb_left_chrome_widget = QWidget(self.tb_left)
        self._tb_left_chrome_widget.setObjectName("toolbarWindowChromeWidget")
        tb_left_chrome_layout = QVBoxLayout(self._tb_left_chrome_widget)
        tb_left_chrome_layout.setContentsMargins(0, 0, 0, 0)
        tb_left_chrome_layout.setSpacing(6)

        self._tb_left_title_label = QLabel(self.windowTitle(), self._tb_left_chrome_widget)
        self._tb_left_title_label.setObjectName("toolbarWindowTitleLabel")
        self._tb_left_title_label.setAlignment(Qt.AlignCenter)
        self._tb_left_title_label.setWordWrap(True)
        self._tb_left_title_label.setFixedWidth(max(side_toolbar_width - 14, 36))
        tb_left_chrome_layout.addWidget(self._tb_left_title_label, 0, Qt.AlignCenter)

        self._tb_left_menu_button = QToolButton(self._tb_left_chrome_widget)
        self._tb_left_menu_button.setObjectName("toolbarWindowMenuButton")
        self._tb_left_menu_button.setIcon(_icon("menu_24.svg"))
        self._tb_left_menu_button.setIconSize(QSize(16, 16))
        self._tb_left_menu_button.setToolTip("Menue")
        self._tb_left_menu_button.setCursor(Qt.PointingHandCursor)
        self._tb_left_menu_button.setFocusPolicy(Qt.NoFocus)
        self._tb_left_menu_button.setAutoRaise(True)
        self._tb_left_menu_button.setFixedSize(42, 42)
        self._tb_left_menu_button.clicked.connect(self._open_toolbar_window_menu)
        tb_left_chrome_layout.addWidget(self._tb_left_menu_button, 0, Qt.AlignCenter)
        self._tb_left_chrome_action = self.tb_left.addWidget(self._tb_left_chrome_widget)

        for toolbar_action in (
            getattr(self, "act_refresh_control_plane", None),
            getattr(self, "act_toggle_explorer", None),
            getattr(self, "act_toggle_control_plane_left", None),
        ):
            if isinstance(toolbar_action, QAction):
                self.tb_left.addAction(toolbar_action)

        self._tb_left_spacer = QWidget(self.tb_left)
        self._tb_left_spacer.setStyleSheet(f"background: {chrome_bg}; border: none;")
        self._tb_left_spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.tb_left.addWidget(self._tb_left_spacer)

        self.windowTitleChanged.connect(self._sync_toolbar_window_title)
        self._sync_toolbar_window_title(self.windowTitle())
        self._sync_window_titlebar_scheme()
        self._refresh_window_titlebar_state()
        self._set_toolbar_window_menu_enabled(bool(self._window_menu_bar and self._window_menu_bar.actions()))
        self._sync_window_chrome_toolbar_visibility()

    def _normalize_toolbar_layout(self) -> None:
        """Keep the fixed toolbars pinned to their intended window areas."""
        toolbar_specs: tuple[tuple[str, Qt.ToolBarArea, Qt.Orientation], ...] = (
            ("tb_top", Qt.TopToolBarArea, Qt.Horizontal),
            ("tb_left", Qt.LeftToolBarArea, Qt.Vertical),
            ("tb_right", Qt.RightToolBarArea, Qt.Vertical),
        )
        for toolbar_name, expected_area, expected_orientation in toolbar_specs:
            toolbar = getattr(self, toolbar_name, None)
            if not isinstance(toolbar, QToolBar):
                continue
            needs_area_fix = self.toolBarArea(toolbar) != expected_area
            needs_orientation_fix = toolbar.orientation() != expected_orientation
            if not (needs_area_fix or needs_orientation_fix):
                continue
            self.removeToolBar(toolbar)
            toolbar.setOrientation(expected_orientation)
            self.addToolBar(expected_area, toolbar)

    # ─────────────────────────  menu bar  ────────────────────────────────────
    
    def _create_menu(self) -> None:
        # ------------------------------------------------------------------ ui
        custom_frame_enabled = not _env_truthy("AI_IDE_DISABLE_CUSTOM_WINDOW_FRAME", "0")
        scheme = _build_scheme(self._accent, self._base)
        chrome_bg = str(scheme.get("col5") or "#000000")
        chrome_hover = str(scheme.get("col9") or chrome_bg)
        mbar: QMenuBar = QMenuBar(self)               # own menu-bar instance
        mbar.setStyleSheet(
            f"QMenuBar {{"
            f" background: {chrome_bg};"
            " border: none;"
            " }"
            f"QMenuBar::item {{"
            f" background: {chrome_bg};"
            " }"
            f"QMenuBar::item:selected {{"
            f" background: {chrome_hover};"
            " }"
        )
        if custom_frame_enabled:
            mbar.setVisible(False)
        else:
            self.setMenuBar(mbar)                     # make it the window bar
        self._window_menu_bar = mbar
        # -------------- FILE ------------------------------------------------
        filem = mbar.addMenu("File")

        act_open_txt = QAction("Öffnen…", self, shortcut=QKeySequence.Open, triggered=self._file_open_text)
        act_open_enc = QAction("Öffnen mit Encoding…", self, triggered=self._file_open_with_encoding)
        act_new      = QAction("Neu", self, shortcut=QKeySequence.New, triggered=self._new_tab)
        act_new_code = QAction("Neuen Code-Viewer-Tab", self, shortcut=QKeySequence("Ctrl+Alt+N"), triggered=self._file_new_code_viewer_tab)
        act_save     = QAction("Speichern", self, shortcut=QKeySequence.Save, triggered=self._file_save_tab_via_tabs)
        act_save_as  = QAction("Speichern unter…", self, shortcut=QKeySequence("Ctrl+Shift+S"), triggered=self._file_save_as_tab_via_tabs)
        act_reopen   = QAction("Geschlossenen Tab wiederherstellen", self, shortcut=QKeySequence("Ctrl+Shift+T"), triggered=self._file_reopen_closed_tab)
        act_set_enc  = QAction("Encoding setzen…", self, triggered=self._file_set_encoding)
        editor_enabled = self._editor_surface_enabled()
        for action in (act_open_txt, act_open_enc, act_new, act_new_code, act_save, act_save_as, act_reopen, act_set_enc):
            action.setEnabled(editor_enabled)

        # Recent submenu: rebuild on show
        self._file_recent_menu = filem.addMenu("Zuletzt geöffnet")
        self._file_recent_menu.aboutToShow.connect(self._rebuild_recent_menu)

        filem.addAction(act_new)
        filem.addAction(act_new_code)
        filem.addAction(act_open_txt)
        filem.addAction(act_open_enc)
        filem.addSeparator()
        filem.addAction(act_save)
        filem.addAction(act_save_as)
        filem.addSeparator()
        filem.addAction(act_reopen)
        filem.addAction(act_set_enc)

        # -------------- ACP -------------------------------------------------
        acp = mbar.addMenu("ACP")
        acp_select = acp.addMenu("Select")
        act_acp_json_tab = QAction(
            "JSON öffnen",
            self,
            triggered=self._acp_open_json_tab,
        )
        act_acp_yaml_tab = QAction(
            "YAML öffnen",
            self,
            triggered=self._acp_open_yaml_tab,
        )
        act_acp_python_tab = QAction(
            "Python öffnen",
            self,
            triggered=self._acp_open_python_tab,
        )
        act_acp_markdown_tab = QAction(
            "Markdown öffnen",
            self,
            triggered=self._acp_open_markdown_tab,
        )
        act_acp_toml_tab = QAction(
            "TOML öffnen",
            self,
            triggered=self._acp_open_toml_tab,
        )
   

        act_acp_new_runtime_tab = QAction(
            "Neuen Runtime-Tab öffnen",
            self,
            triggered=self._acp_open_new_runtime_tab,
        )
        act_acp_import_runtime = QAction(
            "Runtime importieren (Layout-Pfad)",
            self,
            triggered=self._acp_import_runtime_layout,
        )
        act_acp_export_runtime = QAction(
            "Runtime exportieren (Layout-Pfad)",
            self,
            triggered=self._acp_export_runtime_layout,
        )

        acp_enabled = getattr(self, "control_plane_widget", None) is not None
        for action in (
            act_acp_json_tab,
            act_acp_yaml_tab,
            act_acp_python_tab,
            act_acp_markdown_tab,
            act_acp_toml_tab,
            act_acp_new_runtime_tab,
            act_acp_import_runtime,
            act_acp_export_runtime,
        ):
            action.setEnabled(acp_enabled)

        acp_select.addAction(act_acp_json_tab)
        acp_select.addAction(act_acp_yaml_tab)
        acp_select.addAction(act_acp_python_tab)
        acp_select.addAction(act_acp_markdown_tab)
        acp_select.addAction(act_acp_toml_tab)
        acp_select.addSeparator()
        self._acp_saved_runtimes_menu = acp_select.addMenu("Runtime")
        self._acp_saved_runtimes_menu.aboutToShow.connect(self._rebuild_acp_saved_runtimes_menu)
        self._acp_saved_runtimes_menu.setEnabled(acp_enabled)
        acp.addAction(act_acp_new_runtime_tab)
        acp.addSeparator()
        acp.addAction(act_acp_import_runtime)
        acp.addAction(act_acp_export_runtime)

        # -------------- VIEW ------------------------------------------------
        view = mbar.addMenu("View")

        self.menu_visible_action = QAction("Menubar", self, 
                                           checkable = True, 
                                           checked = True,
                                           toggled = mbar
                                           .setVisible
                                           )
        self.act_toggle_custom_titlebar = QAction(
            "Custom Titlebar",
            self,
            checkable=True,
            checked=custom_frame_enabled,
            toggled=self._set_custom_titlebar_visible,
        )
        self.act_toggle_custom_titlebar.setToolTip("Custom Titlebar ein-/ausblenden")
        if not custom_frame_enabled:
            self.act_toggle_custom_titlebar.setEnabled(False)
            self.act_toggle_custom_titlebar.setToolTip("Custom Titlebar ist via Environment deaktiviert")

        left_toolbar = getattr(self, "tb_left", None)
        right_toolbar = getattr(self, "tb_right", None)
        self.act_view_sidebar_left = QAction(
            self.act_toggle_explorer.icon(),
            "Toolbar Left",
            self,
            checkable=True,
            checked=bool(isinstance(left_toolbar, QToolBar) and left_toolbar.isVisible()),
        )
        self.act_view_sidebar_left.setToolTip("Linke Toolbar anzeigen/ausblenden")
        self.act_view_sidebar_left.toggled.connect(self._set_left_toolbar_visible)

        self.act_view_sidebar_right = QAction(
            self.act_toggle_right_dock.icon(),
            "Toolbar Right",
            self,
            checkable=True,
            checked=bool(isinstance(right_toolbar, QToolBar) and right_toolbar.isVisible()),
        )
        self.act_view_sidebar_right.setToolTip("Rechte Toolbar anzeigen/ausblenden")
        self.act_view_sidebar_right.toggled.connect(self._set_right_toolbar_visible)

        # helper to insert action + separator (except after the last one)
        action_list: list = \
            [
             self.act_toggle_chat,                        # <– 10.07.2025 
             self.act_toggle_control_plane,
             self.act_toggle_explorer,
             self.act_graph_placeholder,
             self.act_toggle_fullscreen,
             self.act_toggle_accent,
             self.act_toggle_custom_titlebar,
             self.menu_visible_action,
             self.act_grey
            ]

        if self._editor_surface_enabled():
            action_list.insert(3, self.act_toggle_tabdock)
        if self._terminal_surface_enabled():
            action_list.insert(4, self.act_toggle_console)
        
        def _addActions(act: QAction, last: bool = False) -> None:
            for act in action_list:
                view.addAction(act)
                if not last:
                    view.addSeparator()

        view.addAction(self.act_view_sidebar_left)
        view.addSeparator()
        view.addAction(self.act_view_sidebar_right)
        view.addSeparator()

        _addActions(action_list) 
        
        # -------------- TOOLS ------------------------------------------------
        
        tools = mbar.addMenu("Tools")
        tools.addAction(self.act_refresh_control_plane)
        if self._editor_surface_enabled():
            tools.addSeparator()
            tools.addAction(self.act_clone_tabdock)

        titlebar_widget = self._get_live_title_bar_widget()
        if titlebar_widget is not None:
            titlebar_widget.set_menu_enabled(bool(mbar.actions()))
        self._set_toolbar_window_menu_enabled(bool(mbar.actions()))
        self._sync_window_chrome_toolbar_visibility()
   
    # ================================================= status =============
    
    def _create_status(self):
        scheme = _build_scheme(self._accent, self._base)
        st = QStatusBar(self)
        st.setStyleSheet(f"QStatusBar {{ background: {scheme.get('col5', '#000000')}; border: none; }}")
        st.showMessage("Ready")
        self._st_agents = QLabel("0 agents")
        self._st_workflows = QLabel("0 workflows")
        self._st_sessions = QLabel("0 sessions")
        self._st_runtime = QLabel("runtime n/a")
        self._st_enc = QLabel("UTF-8")
        for label in (
            self._st_agents,
            self._st_workflows,
            self._st_sessions,
            self._st_runtime,
            self._st_enc,
        ):
            label.setStyleSheet("font-size: 12px;")
        st.addPermanentWidget(self._st_agents)
        st.addPermanentWidget(self._st_workflows)
        st.addPermanentWidget(self._st_sessions)
        st.addPermanentWidget(self._st_runtime)
        # permanenter Encoding-Indikator
        st.addPermanentWidget(self._st_enc)
        self.setStatusBar(st)
        self._update_control_plane_status(getattr(getattr(self, "control_plane_widget", None), "_last_snapshot", {}))

    # ================================================= misc helpers =======
    
    def _wire_vis(self):
        self.files_dock.visibilityChanged.connect(
            self.act_toggle_explorer.setChecked
            )
        self.files_dock.visibilityChanged.connect(self._handle_explorer_visibility_change)
        self.console_dock.visibilityChanged.connect(
            self.act_toggle_console.setChecked
            )
        self.chat_dock.visibilityChanged.connect(        #  << NEU
            self.act_toggle_chat.setChecked)
        self.extensions_dock.visibilityChanged.connect(
            self.act_toggle_control_plane.setChecked
        )
        self.extensions_dock.visibilityChanged.connect(self.act_graph_placeholder.setChecked)
        if hasattr(self, "act_toggle_control_plane_left"):
            self.chat_dock.visibilityChanged.connect(self.act_toggle_control_plane_left.setChecked)
        self.files_dock.visibilityChanged.connect(lambda _v: self._rebalance_workspace_columns())
        self.chat_dock.visibilityChanged.connect(lambda _v: self._rebalance_workspace_columns())
        self.extensions_dock.visibilityChanged.connect(lambda _v: self._rebalance_workspace_columns())

        if hasattr(self, "act_toggle_right_dock"):
            self.extensions_dock.visibilityChanged.connect(self.act_toggle_right_dock.setChecked)
            self.extensions_dock.visibilityChanged.connect(self._update_right_dock_icon)
            self.extensions_dock.visibilityChanged.connect(self._sync_right_workspace_tabs_visibility)
            # Initialize icon state
            self._update_right_dock_icon(self.extensions_dock.isVisible())
            self._sync_right_workspace_tabs_visibility(self.extensions_dock.isVisible())
        self._rebalance_workspace_columns()

    @Slot()
    def _refresh_control_plane(self) -> None:
        if getattr(self, "control_plane_widget", None) is None:
            self.statusBar().showMessage("Control Plane disabled", 2500)
            return
        self.control_plane_widget.refresh_view()
        self.statusBar().showMessage("Control Plane refreshed", 2500)

    @Slot()
   

    @Slot()
    def _acp_open_json_tab(self) -> None:
        self._acp_open_runtime_widget_tab("code_json", "JSON")

    @Slot()
    def _acp_open_yaml_tab(self) -> None:
        self._acp_open_runtime_widget_tab("code_yaml", "YAML")

    @Slot()
    def _acp_open_python_tab(self) -> None:
        self._acp_open_runtime_widget_tab("code_python", "Python")

    @Slot()
    def _acp_open_markdown_tab(self) -> None:
        self._acp_open_runtime_widget_tab("code_markdown", "Markdown")

    @Slot()
    def _acp_open_toml_tab(self) -> None:
        self._acp_open_runtime_widget_tab("code_toml", "TOML")

    @Slot()
    def _acp_open_new_runtime_tab(self) -> None:
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            self.statusBar().showMessage("ACP disabled", 2500)
            return

        if hasattr(self, "extensions_dock") and isinstance(self.extensions_dock, QDockWidget):
            if not self.extensions_dock.isVisible():
                self.extensions_dock.show()

        try:
            control_plane._open_new_runtime_tab()
        except Exception as exc:
            QMessageBox.warning(self, "ACP", f"Runtime-Tab konnte nicht erstellt werden: {exc}")

    @Slot()
    def _acp_import_runtime_layout(self) -> None:
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            self.statusBar().showMessage("ACP disabled", 2500)
            return

        try:
            configured_layout_path = Path(str(control_plane.runtime_layout_path()))
        except Exception:
            configured_layout_path = Path()
        if not configured_layout_path.exists():
            QMessageBox.warning(
                self,
                "ACP",
                f"Runtime-Import fehlgeschlagen: Layout-Datei nicht gefunden ({configured_layout_path})",
            )
            return

        if hasattr(self, "extensions_dock") and isinstance(self.extensions_dock, QDockWidget):
            if not self.extensions_dock.isVisible():
                self.extensions_dock.show()

        try:
            reload_runtime = getattr(control_plane, "_reload_runtime_layout_from_path", None)
            if not callable(reload_runtime):
                raise RuntimeError("Runtime-Import nicht verfügbar")
            reload_runtime()
        except Exception as exc:
            QMessageBox.warning(self, "ACP", f"Runtime-Import fehlgeschlagen: {exc}")
            return

        try:
            layout_path = str(control_plane.runtime_layout_path())
        except Exception:
            layout_path = ""
        if layout_path:
            self.statusBar().showMessage(f"Runtime importiert: {layout_path}", 3200)
        else:
            self.statusBar().showMessage("Runtime importiert", 3200)

    @Slot()
    def _acp_export_runtime_layout(self) -> None:
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            self.statusBar().showMessage("ACP disabled", 2500)
            return

        if hasattr(self, "extensions_dock") and isinstance(self.extensions_dock, QDockWidget):
            if not self.extensions_dock.isVisible():
                self.extensions_dock.show()

        try:
            persist_runtime = getattr(control_plane, "persist_runtime_tabs_state", None)
            if not callable(persist_runtime):
                raise RuntimeError("Runtime-Export nicht verfügbar")
            export_path = persist_runtime(force=True)
        except Exception as exc:
            QMessageBox.warning(self, "ACP", f"Runtime-Export fehlgeschlagen: {exc}")
            return

        if export_path is not None:
            self.statusBar().showMessage(f"Runtime exportiert: {export_path}", 3200)
        else:
            QMessageBox.warning(self, "ACP", "Runtime-Export fehlgeschlagen.")

    def _acp_runtime_layout_payload(self) -> dict[str, Any] | None:
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            return None

        try:
            layout_path = Path(str(control_plane.runtime_layout_path()))
        except Exception:
            return None
        if not layout_path.is_file():
            return None

        try:
            payload = json.loads(layout_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        return payload

    def _acp_saved_runtime_names(self) -> list[str]:
        payload = self._acp_runtime_layout_payload()
        if not isinstance(payload, dict):
            return []

        tabs_payload = payload.get("tabs")
        if not isinstance(tabs_payload, list):
            return []

        names: list[str] = []
        seen: set[str] = set()
        for entry in tabs_payload:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            dedupe_key = name.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            names.append(name)
        return names

    def _acp_saved_runtime_widget_entries(self) -> list[tuple[str, str]]:
        payload = self._acp_runtime_layout_payload()
        if not isinstance(payload, dict):
            return []

        tabs_payload = payload.get("tabs")
        if not isinstance(tabs_payload, list):
            return []

        entries: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for tab_entry in tabs_payload:
            if not isinstance(tab_entry, dict):
                continue

            tab_name = str(tab_entry.get("name") or "").strip()
            widget_entries = tab_entry.get("widgets")
            if not tab_name or not isinstance(widget_entries, list):
                continue

            for widget in widget_entries:
                if not isinstance(widget, dict):
                    continue

                widget_title = str(widget.get("title") or "").strip()
                source_path = str(widget.get("source_path") or "").strip()
                source_name = Path(source_path).name.lower() if source_path else ""
                normalized_title = widget_title.lower()

                is_runtime_config_entry = (
                    "runtime_config" in normalized_title
                    or source_name.startswith("runtime_config")
                )
                if not is_runtime_config_entry:
                    continue

                key = (tab_name.lower(), widget_title.lower())
                if key in seen:
                    continue
                seen.add(key)
                entries.append((tab_name, widget_title or "runtime_config.json"))

        return entries

    def _acp_saved_runtime_config_paths(self) -> list[Path]:
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            return []

        candidate_dirs: list[Path] = []
        try:
            candidate_dirs.append(Path(str(control_plane.runtime_layout_path())).parent)
        except Exception:
            pass

        try:
            project_root = Path(__file__).resolve().parents[2]
            candidate_dirs.append(project_root / "AppData")
            candidate_dirs.append(project_root / "ALDE" / "AppData")
        except Exception:
            pass

        runtime_paths: list[Path] = []
        seen: set[str] = set()
        for base_dir in candidate_dirs:
            if not base_dir.is_dir():
                continue
            try:
                matches = sorted(base_dir.rglob("runtime_config*.json"))
            except Exception:
                continue
            for path in matches:
                try:
                    resolved = str(path.resolve())
                except Exception:
                    resolved = str(path)
                if resolved in seen:
                    continue
                seen.add(resolved)
                runtime_paths.append(path)

        return runtime_paths

    @Slot(str)
    def _acp_open_runtime_config_file(self, file_path: str) -> None:
        selected_path = Path(str(file_path or "").strip()).expanduser()
        if not selected_path.is_file():
            QMessageBox.information(self, "ACP", f"Runtime-Datei nicht gefunden: {selected_path}")
            return

        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            self.statusBar().showMessage("ACP disabled", 2500)
            return

        if hasattr(self, "extensions_dock") and isinstance(self.extensions_dock, QDockWidget):
            if not self.extensions_dock.isVisible():
                self.extensions_dock.show()

        try:
            runtime_text = selected_path.read_text(encoding="utf-8", errors="replace")
            append_runtime_widget = getattr(control_plane, "append_runtime_widget", None)
            if not callable(append_runtime_widget):
                raise RuntimeError("Runtime-Datei-Import nicht verfügbar")
            append_runtime_widget(
                tab_name=selected_path.stem or "Runtime",
                widget_kind="code_json",
                content=runtime_text,
                source_path=str(selected_path),
                title=selected_path.name,
            )
        except Exception as exc:
            QMessageBox.warning(self, "ACP", f"Runtime-Datei konnte nicht geöffnet werden: {exc}")
            return

        self.statusBar().showMessage(f"Runtime-Datei geöffnet: {selected_path.name}", 3200)

    def _rebuild_acp_saved_runtimes_menu(self) -> None:
        menu = getattr(self, "_acp_saved_runtimes_menu", None)
        if not isinstance(menu, QMenu):
            return

        menu.clear()
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            disabled_action = QAction("ACP disabled", self)
            disabled_action.setEnabled(False)
            menu.addAction(disabled_action)
            return

        runtime_paths = self._acp_saved_runtime_config_paths()
        runtime_widget_entries = self._acp_saved_runtime_widget_entries()
        runtime_names = self._acp_saved_runtime_names()
        if not runtime_paths and not runtime_widget_entries and not runtime_names:
            empty_action = QAction("Keine gespeicherten Runtimes", self)
            empty_action.setEnabled(False)
            menu.addAction(empty_action)
            return

        current_runtime_name = ""
        try:
            tabs = getattr(control_plane, "tabs", None)
            runtime_records = getattr(control_plane, "_runtime_tab_records", {})
            if tabs is not None and hasattr(tabs, "currentWidget") and hasattr(tabs, "tabText"):
                current_widget = tabs.currentWidget()
                if isinstance(runtime_records, dict) and current_widget in runtime_records:
                    current_runtime_name = str(tabs.tabText(tabs.currentIndex()) or "").strip()
        except Exception:
            current_runtime_name = ""

        if runtime_paths:
            files_header = QAction("Runtime-Dateien", self)
            files_header.setEnabled(False)
            menu.addAction(files_header)

            name_counts: dict[str, int] = {}
            for runtime_path in runtime_paths:
                runtime_name = runtime_path.name
                name_counts[runtime_name] = int(name_counts.get(runtime_name) or 0) + 1

            for runtime_path in runtime_paths:
                runtime_name = runtime_path.name
                if int(name_counts.get(runtime_name) or 0) > 1:
                    try:
                        label = str(runtime_path.relative_to(runtime_path.parents[1]))
                    except Exception:
                        label = str(runtime_path)
                else:
                    label = runtime_name

                action = QAction(label, self)
                action.setToolTip(str(runtime_path))
                action.triggered.connect(
                    lambda _checked=False, selected_path=str(runtime_path): self._acp_open_runtime_config_file(selected_path)
                )
                menu.addAction(action)

        if runtime_widget_entries:
            if menu.actions():
                menu.addSeparator()
            widgets_header = QAction("Gespeicherte Runtime-Widgets", self)
            widgets_header.setEnabled(False)
            menu.addAction(widgets_header)

            for tab_name, widget_title in runtime_widget_entries:
                label = f"{widget_title} ({tab_name})"
                action = QAction(label, self)
                action.setToolTip(f"Runtime-Tab: {tab_name}")
                action.triggered.connect(
                    lambda _checked=False, selected_runtime_name=tab_name: self._acp_select_saved_runtime(selected_runtime_name)
                )
                menu.addAction(action)

        if runtime_names:
            if menu.actions():
                menu.addSeparator()
            tabs_header = QAction("Runtime-Tabs", self)
            tabs_header.setEnabled(False)
            menu.addAction(tabs_header)

            for runtime_name in runtime_names:
                action = QAction(runtime_name, self)
                action.setCheckable(True)
                action.setChecked(bool(current_runtime_name) and runtime_name.strip().lower() == current_runtime_name.lower())
                action.triggered.connect(
                    lambda _checked=False, selected_runtime_name=runtime_name: self._acp_select_saved_runtime(selected_runtime_name)
                )
                menu.addAction(action)

    @Slot(str)
    def _acp_select_saved_runtime(self, runtime_name: str) -> None:
        selected_name = str(runtime_name or "").strip()
        if not selected_name:
            return

        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            self.statusBar().showMessage("ACP disabled", 2500)
            return

        if hasattr(self, "extensions_dock") and isinstance(self.extensions_dock, QDockWidget):
            if not self.extensions_dock.isVisible():
                self.extensions_dock.show()

        finder = getattr(control_plane, "_find_runtime_tab_by_name", None)
        tab_widget = None
        if callable(finder):
            try:
                tab_widget = finder(selected_name)
            except Exception:
                tab_widget = None

        # If the tab is not currently loaded, pull the latest state from the
        # configured runtime layout path and try again.
        if tab_widget is None:
            reload_runtime = getattr(control_plane, "_reload_runtime_layout_from_path", None)
            if callable(reload_runtime):
                try:
                    reload_runtime()
                except Exception:
                    pass
            if callable(finder):
                try:
                    tab_widget = finder(selected_name)
                except Exception:
                    tab_widget = None

        tabs = getattr(control_plane, "tabs", None)
        tab_index = -1
        if tabs is not None and tab_widget is not None and hasattr(tabs, "indexOf"):
            try:
                tab_index = int(tabs.indexOf(tab_widget))
            except Exception:
                tab_index = -1

        if tabs is not None and tab_index >= 0 and hasattr(tabs, "setCurrentIndex"):
            tabs.setCurrentIndex(tab_index)
            self.statusBar().showMessage(f"Runtime ausgewählt: {selected_name}", 3200)
            return

        QMessageBox.information(self, "ACP", f"Runtime nicht gefunden: {selected_name}")

    def _acp_open_runtime_widget_tab(self, widget_kind: str, tab_name: str) -> None:
        control_plane = getattr(self, "control_plane_widget", None)
        if control_plane is None:
            self.statusBar().showMessage("ACP disabled", 2500)
            return

        if hasattr(self, "extensions_dock") and isinstance(self.extensions_dock, QDockWidget):
            if not self.extensions_dock.isVisible():
                self.extensions_dock.show()

        try:
            control_plane.create_runtime_tab_for_kind(widget_kind, tab_name=tab_name, activate=True)
        except Exception as exc:
            QMessageBox.warning(self, "ACP", f"{tab_name}-Tab konnte nicht erstellt werden: {exc}")
            return

        self.statusBar().showMessage(f"ACP {tab_name}-Tab erstellt", 2500)

    def _update_control_plane_status(self, snapshot: dict[str, Any] | None) -> None:
        configuration_snapshot = (snapshot or {}).get("configuration") if isinstance(snapshot, dict) else {}
        monitoring_snapshot = (snapshot or {}).get("monitoring") if isinstance(snapshot, dict) else {}
        if hasattr(self, "_st_agents"):
            self._st_agents.setText(f"{int((configuration_snapshot or {}).get('agent_count') or 0)} agents")
        if hasattr(self, "_st_workflows"):
            self._st_workflows.setText(f"{int((configuration_snapshot or {}).get('workflow_count') or 0)} workflows")
        if hasattr(self, "_st_sessions"):
            self._st_sessions.setText(f"{int((monitoring_snapshot or {}).get('session_count') or 0)} sessions")
        if hasattr(self, "_st_runtime"):
            failure_count = int((monitoring_snapshot or {}).get("failure_count") or 0)
            runtime_text = "runtime healthy" if failure_count == 0 else f"{failure_count} failures"
            self._st_runtime.setText(runtime_text)

    def _update_right_dock_icon(self, visible: bool) -> None:
        """Update the right-toolbar icon depending on dashboard visibility."""
        if not hasattr(self, "act_toggle_right_dock"):
            return
        self.act_toggle_right_dock.setIcon(
            _icon("dashboard_25dp_B7B7B7_FILL0_wght500_GRAD0_opsz24.svg")
        )

    def _update_tabdock_toggle_state(self) -> None:
        """
        Keep the View menu toggle aligned with the actual tab-dock visibility.
        """
        act = getattr(self, "act_toggle_tabdock", None)
        if act is None:
            return
        state = bool(self._tab_docks) and all(td.isVisible() for td in self._tab_docks)
        prev = act.blockSignals(True)
        act.setChecked(state)
        act.blockSignals(prev)

    def _is_right_workspace_visible(self) -> bool:
        """Return True if any widget in the right workspace column is visible."""
        # Control Plane is now part of extensions_dock (merged)
        extensions_visible = bool(getattr(self, "extensions_dock", None) and self.extensions_dock.isVisible())
        console_visible = bool(getattr(self, "console_dock", None) and self.console_dock.isVisible())
        tabdock_visible = any(dock.isVisible() for dock in getattr(self, "_tab_docks", []))
        return extensions_visible or console_visible or tabdock_visible

    def _is_extensions_workspace_visible(self) -> bool:
        return bool(getattr(self, "extensions_dock", None) and self.extensions_dock.isVisible())

    def _right_workspace_split_widgets(self) -> list[QWidget]:
        splitter = getattr(self, "main_split", None)
        if not isinstance(splitter, QSplitter):
            return []

        fixed_widgets = {
            getattr(self, "files_dock", None),
            getattr(self, "chat_dock", None),
            getattr(self, "extensions_dock", None),
        }
        workspace_widgets: list[QWidget] = []
        for index in range(splitter.count()):
            widget = splitter.widget(index)
            if widget is None or widget in fixed_widgets:
                continue
            workspace_widgets.append(widget)
        return workspace_widgets

    def _remember_workspace_column_widths(self, *_args: Any) -> None:
        splitter = getattr(self, "main_split", None)
        if splitter is None:
            return

        sizes = splitter.sizes()
        if len(sizes) < 4:
            return

        if len(getattr(self, "_workspace_column_widths", [])) != 4:
            self._workspace_column_widths = [260, 760, 460, 180]

        left_widget = getattr(self, "files_dock", None)
        middle_widget = getattr(self, "chat_dock", None)
        extensions_widget = getattr(self, "extensions_dock", None)

        left_index = splitter.indexOf(left_widget) if isinstance(left_widget, QWidget) else -1
        middle_index = splitter.indexOf(middle_widget) if isinstance(middle_widget, QWidget) else -1
        extensions_index = splitter.indexOf(extensions_widget) if isinstance(extensions_widget, QWidget) else -1

        left_size = int(sizes[left_index]) if left_index >= 0 and left_index < len(sizes) else 0
        middle_size = int(sizes[middle_index]) if middle_index >= 0 and middle_index < len(sizes) else 0
        extensions_size = int(sizes[extensions_index]) if extensions_index >= 0 and extensions_index < len(sizes) else 0
        right_size = 0
        for widget in self._right_workspace_split_widgets():
            widget_index = splitter.indexOf(widget)
            if widget_index < 0 or widget_index >= len(sizes):
                continue
            if not widget.isVisible():
                continue
            right_size += int(sizes[widget_index])

        if left_widget is not None and left_widget.isVisible() and left_size > 0:
            self._workspace_column_widths[0] = left_size
        if middle_widget is not None and middle_widget.isVisible() and middle_size > 0:
            self._workspace_column_widths[1] = middle_size
        if self._is_right_workspace_visible() and right_size > 0:
            self._workspace_column_widths[2] = right_size
        if extensions_widget is not None and extensions_widget.isVisible() and extensions_size > 0:
            self._workspace_column_widths[3] = extensions_size

    def _expand_explorer_column_width(self, delta_px: int) -> None:
        splitter = getattr(self, "main_split", None)
        if not isinstance(splitter, QSplitter):
            return

        boost_px = max(0, int(delta_px))
        if boost_px <= 0:
            return

        left_widget = getattr(self, "files_dock", None)
        left_index = splitter.indexOf(left_widget) if isinstance(left_widget, QWidget) else -1
        if left_index < 0:
            return
        if isinstance(left_widget, QDockWidget) and not left_widget.isVisible():
            return

        sizes = [max(0, int(value)) for value in splitter.sizes()]
        if left_index >= len(sizes):
            return

        donor_candidates: list[QWidget] = []
        middle_widget = getattr(self, "chat_dock", None)
        if isinstance(middle_widget, QWidget):
            donor_candidates.append(middle_widget)
        donor_candidates.extend(self._right_workspace_split_widgets())
        extensions_widget = getattr(self, "extensions_dock", None)
        if isinstance(extensions_widget, QWidget):
            donor_candidates.append(extensions_widget)

        donor_indices: list[int] = []
        seen_indices: set[int] = {left_index}
        for candidate in donor_candidates:
            idx = splitter.indexOf(candidate)
            if idx < 0 or idx >= len(sizes) or idx in seen_indices:
                continue
            if not candidate.isVisible():
                continue
            donor_indices.append(idx)
            seen_indices.add(idx)

        remaining = boost_px
        for idx in donor_indices:
            available = max(0, sizes[idx] - 1)
            if available <= 0:
                continue
            take = min(available, remaining)
            sizes[idx] -= take
            sizes[left_index] += take
            remaining -= take
            if remaining <= 0:
                break

        if remaining >= boost_px:
            return

        splitter.setSizes(sizes)
        self._remember_workspace_column_widths()

    def _rebalance_workspace_columns(self) -> None:
        splitter = getattr(self, "main_split", None)
        if splitter is None:
            return

        self._remember_workspace_column_widths()

        fallback_widths = [260, 760, 460, 180]
        preferred_widths = list(getattr(self, "_workspace_column_widths", fallback_widths))
        if len(preferred_widths) != 4:
            preferred_widths = fallback_widths

        left_visible = bool(getattr(self, "files_dock", None) and self.files_dock.isVisible())
        middle_visible = bool(getattr(self, "chat_dock", None) and self.chat_dock.isVisible())
        right_workspace_widgets = self._right_workspace_split_widgets()
        visible_right_workspace_widgets = [widget for widget in right_workspace_widgets if widget.isVisible()]
        extensions_visible = self._is_extensions_workspace_visible()

        sizes: list[int] = [0 for _ in range(splitter.count())]

        left_widget = getattr(self, "files_dock", None)
        middle_widget = getattr(self, "chat_dock", None)
        extensions_widget = getattr(self, "extensions_dock", None)

        left_index = splitter.indexOf(left_widget) if isinstance(left_widget, QWidget) else -1
        middle_index = splitter.indexOf(middle_widget) if isinstance(middle_widget, QWidget) else -1
        extensions_index = splitter.indexOf(extensions_widget) if isinstance(extensions_widget, QWidget) else -1

        if left_visible and left_index >= 0:
            sizes[left_index] = preferred_widths[0]
        if middle_visible and middle_index >= 0:
            sizes[middle_index] = preferred_widths[1]
        if extensions_visible and extensions_index >= 0:
            sizes[extensions_index] = preferred_widths[3]

        if visible_right_workspace_widgets:
            right_distribution = self._normalize_splitter_sizes(
                [1 for _ in visible_right_workspace_widgets],
                total=max(int(preferred_widths[2]), len(visible_right_workspace_widgets)),
            )
            for widget, width in zip(visible_right_workspace_widgets, right_distribution):
                widget_index = splitter.indexOf(widget)
                if widget_index >= 0:
                    sizes[widget_index] = width

        visible_found = left_visible or middle_visible or bool(visible_right_workspace_widgets) or extensions_visible
        if not visible_found:
            if left_index >= 0:
                sizes[left_index] = preferred_widths[0]
            if middle_index >= 0:
                sizes[middle_index] = preferred_widths[1]
            if extensions_index >= 0:
                sizes[extensions_index] = preferred_widths[3]
            if right_workspace_widgets:
                fallback_distribution = self._normalize_splitter_sizes(
                    [1 for _ in right_workspace_widgets],
                    total=max(int(preferred_widths[2]), len(right_workspace_widgets)),
                )
                for widget, width in zip(right_workspace_widgets, fallback_distribution):
                    widget_index = splitter.indexOf(widget)
                    if widget_index >= 0:
                        sizes[widget_index] = width

        normalized_sizes = self._normalize_splitter_sizes(sizes, total=1000)
        if len(normalized_sizes) != splitter.count():
            splitter.setSizes([1 for _ in range(max(1, splitter.count()))])
            return

        splitter.setSizes(normalized_sizes)

    # ------------------------------------------------ tab-dock clone ------

    def _clone_tab_dock(self, set_current: bool = True):
        dock_id = len(self._tab_docks) + 1
        dock = QDockWidget(f"Tab-Dock {dock_id}", self)
        dock.setObjectName(f"TabDock_{dock_id}")
        dock.setMinimumSize(0, 0)
        dock.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        tabs = EditorTabs()
        dock.setWidget(tabs)
        # Update Status-Enc when switching tabs
        tabs.currentChanged.connect(lambda _i, s=self: s._update_status_encoding())

        self._strip_dock_decoration(dock)

        # Insert into the active workspace splitter (legacy right_split is optional).
        target_splitter = getattr(self, "right_split", None)
        if not isinstance(target_splitter, QSplitter):
            target_splitter = getattr(self, "main_split", None)

        if isinstance(target_splitter, QSplitter):
            # Use extensions_dock (which now includes control plane) as anchor, fallback to chat_dock
            anchor_widget = self.extensions_dock if target_splitter.indexOf(self.extensions_dock) >= 0 else self.chat_dock
            insert_index = target_splitter.indexOf(anchor_widget)
            if insert_index < 0:
                insert_index = target_splitter.count()
            target_splitter.insertWidget(max(0, insert_index), dock)
        else:
            dock.setParent(self)
            dock.hide()

        self._tab_docks.append(dock)
        dock.visibilityChanged.connect(
            lambda v, s=self: s._update_tabdock_toggle_state())
        dock.visibilityChanged.connect(
            lambda _v, s=self: s._rebalance_workspace_columns())


        if set_current:
            tabs.setCurrentIndex(0)

        # Keep the menu action in sync with the actual dock visibility.
        if hasattr(self, "act_toggle_tabdock"):
            self._update_tabdock_toggle_state()
    
    # ------------------------------------------------ Slot's -- api -------
    # ------------------------------------------------ new file tab --------
    
    @Slot()
    def _new_tab(self) -> None:
        """
        Öffnet einen neuen, noch ungespeicherten Tab im fokussierten Tab-Dock.
        """
        tabs = self._get_focused_tab_dock()
        if tabs is None and hasattr(self, "_clone_tab_dock"):
            try:
                self._clone_tab_dock(set_current=True)
            except Exception:
                pass
            tabs = self._get_focused_tab_dock()
        if tabs is None:
            return

        idx = tabs.addTab(                        # Tab anlegen
            QTextEdit("# new file …"),
            f"untitled_{tabs.count() + 1}.py"
        )

        tabs.widget(idx).setProperty("file_path", "")   # wichtig für Save-Logik
        tabs.setCurrentIndex(idx)
        self._update_status_encoding()
    
        # ------------------------------------------------ close tab -----------

    @Slot()
    def _close_tab(self):
        tabs = self._get_focused_tab_dock()
        if tabs is None:
            return
        tabs._close_tab()
        self._prune_empty_tab_docks()
        self._update_tabdock_toggle_state()
        self._rebalance_workspace_columns()
        self._update_status_encoding()
    
    # ------------------------------------------------ close dock -----------

    @Slot()
    def _close_dock(self):
        """
        Sucht den umgebenden QDockWidget und schließt ihn.
        Dadurch verschwindet das komplette Tab-Dock inklusive aller Tabs.
        """
        dock = self._parent_dock()
        if dock:
            dock.close()

    # ------------------------------------------------- helper ---------------
    
    def _parent_dock(self) -> QDockWidget | None:
        w = self.parentWidget()
        while w and not isinstance(w, QDockWidget):
            w = w.parentWidget()
        return w
    
    # -------------------------------------------------file open -------------

    # <– 10.07.2025
    # ─── RE-WRITE of MainAIEditor._open_file() ────────────────────────────────
    #   (old implementation is replaced completely)


    @Slot()
    def _save_current_tab(self) -> None:
        """
        Speichert den Inhalt des aktiven Tabs.
        Existiert noch kein Dateiname, wird automatisch »Speichern unter …«
        ausgeführt.
        """
        tabs = self._get_focused_tab_dock()
        if tabs is None:
            return
        idx = tabs.currentIndex()
        if idx < 0:
            return

        widget = tabs.widget(idx)
        if not isinstance(widget, (QPlainTextEdit, QTextEdit)):
            QMessageBox.information(self, "Info",
                                    "Dieser Tab enthält keine editierbare Textdatei.")
            return

        path: str = widget.property("file_path") or ""
        if not path:
            # Kein Pfad vorhanden  →  gleich Speichern unter …
            self._save_current_tab_as()
            return

        try:
            Path(path).write_text(widget.toPlainText(), encoding="utf-8")
        except Exception as exc:          # noqa: BLE001
            QMessageBox.critical(self, "Fehler", str(exc))
            return

        self.statusBar().showMessage(f"{path} gespeichert", 3000)

    # ---------------------------------------------------------------------------
    @Slot()
    def _save_current_tab_as(self) -> None:
        """
        Öffnet immer den Dateidialog „Speichern unter …“, schreibt den Inhalt
        und aktualisiert Tab-Titel & file_path-Property.
        """
        tabs = self._get_focused_tab_dock()
        if tabs is None:
            return
        idx               = tabs.currentIndex()
        if idx < 0:
            return

        widget = tabs.widget(idx)
        if not isinstance(widget, (QPlainTextEdit, QTextEdit)):
            QMessageBox.information(self, "Info",
                                    "Dieser Tab enthält keine editierbare Textdatei.")
            return

        fname, _ = QFileDialog.getSaveFileName(
            self, "Speichern unter …", str(Path.home()),
            "Textdateien (*.txt *.md *.py);;Alle Dateien (*)"
        )
        if not fname:
            return

        try:
            Path(fname).write_text(widget.toPlainText(), encoding="utf-8")
        except Exception as exc:          # noqa: BLE001
            QMessageBox.critical(self, "Fehler", str(exc))
            return

        widget.setProperty("file_path", fname)
        tabs.setTabText(idx, Path(fname).name)
        self.statusBar().showMessage(f"{fname} gespeichert", 3000)


    def _prune_empty_tab_docks(self) -> None:
        cleaned: list[QDockWidget] = []
        for dock in list(getattr(self, "_tab_docks", [])):
            if not isinstance(dock, QDockWidget):
                continue
            tabs = dock.widget()
            if isinstance(tabs, EditorTabs) and tabs.count() == 0:
                try:
                    dock.setParent(None)
                    dock.deleteLater()
                except Exception:
                    pass
                continue
            cleaned.append(dock)
        self._tab_docks = cleaned

    @Slot()
    def _get_focused_tab_dock(self) -> EditorTabs | None:
        """Findet das aktuell fokussierte TabDock oder gibt das erste zurück."""
        self._prune_empty_tab_docks()

        # Versuche das fokussierte Widget zu finden
        focused = QApplication.focusWidget()
        
        # Gehe den Widget-Baum hoch und suche nach EditorTabs
        current = focused
        while current:
            if isinstance(current, EditorTabs):
                return current
            current = current.parentWidget()
        
        # Fallback: Suche nach dem Dock, das sichtbar und aktiv ist
        for dock in self._tab_docks:
            if dock.isVisible() and not dock.isFloating():
                tabs = dock.widget()
                if isinstance(tabs, EditorTabs) and tabs.count() > 0:
                    return tabs

        # Letzter Fallback: irgendein Dock mit Tabs
        for dock in self._tab_docks:
            tabs = dock.widget()
            if isinstance(tabs, EditorTabs) and tabs.count() > 0:
                return tabs
        
        return None

    # -------------------- File menu wrappers for EditorTabs --------------
    @Slot()
    def _file_new_code_viewer_tab(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is None and hasattr(self, "_clone_tab_dock"):
            try:
                self._clone_tab_dock(set_current=True)
            except Exception:
                pass
            tabs = self._get_focused_tab_dock()

        if tabs is None:
            QMessageBox.information(self, "Info", "Kein Tab-Dock verfügbar, um einen Code-Tab zu öffnen.")
            return

        dock = tabs.parentWidget()
        while dock is not None and not isinstance(dock, QDockWidget):
            dock = dock.parentWidget()
        if isinstance(dock, QDockWidget) and not dock.isVisible():
            dock.show()

        tabs._new_code_viewer_tab()
        self._update_tabdock_toggle_state()
        self._rebalance_workspace_columns()
        self._update_status_encoding()

    @Slot()
    def _file_open_text(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._open_file_dialog()
            self._update_status_encoding()

    @Slot()
    def _file_open_with_encoding(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._open_file_dialog_with_encoding()
            self._update_status_encoding()

    @Slot()
    def _file_save_tab_via_tabs(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._save_current_tab()
            self._update_status_encoding()

    @Slot()
    def _file_save_as_tab_via_tabs(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._save_current_tab_as()
            self._update_status_encoding()

    @Slot()
    def _file_reopen_closed_tab(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._reopen_closed_tab()
            self._update_status_encoding()

    @Slot()
    def _file_set_encoding(self) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._set_current_tab_encoding()
            self._update_status_encoding()

    def _rebuild_recent_menu(self) -> None:
        if not hasattr(self, "_file_recent_menu"):
            return
        m = self._file_recent_menu
        m.clear()
        # Read the same QSettings key used by EditorTabs
        try:
            s = QSettings()
            arr = s.value("EditorTabs/RecentFiles", [])
            paths = [str(x) for x in arr] if isinstance(arr, list) else []
        except Exception:
            paths = []
        if not paths:
            dummy = QAction("(leer)", self)
            dummy.setEnabled(False)
            m.addAction(dummy)
            return
        for p in paths:
            act = QAction(str(Path(p).name), self)
            act.setToolTip(p)
            act.triggered.connect(lambda _=False, path=p: self._file_open_recent(path))
            m.addAction(act)

    def _file_open_recent(self, path: str) -> None:
        tabs = self._get_focused_tab_dock()
        if tabs is not None:
            tabs._open_recent(path)
            self._update_status_encoding()

    def _update_status_encoding(self) -> None:
        tabs = self._get_focused_tab_dock()
        enc_text = ""
        if tabs is not None and isinstance(tabs, QTabWidget):
            idx = tabs.currentIndex()
            if idx >= 0:
                w = tabs.widget(idx)
                enc = getattr(w, 'property', lambda _k: None)("file_encoding") if hasattr(w, 'property') else None
                if not enc:
                    enc = "utf-8"
                dirty = "*" if hasattr(w, 'document') and w.document() and w.document().isModified() else ""
                enc_text = f"{dirty}{enc.upper()}"
        if hasattr(self, '_st_enc'):
            self._st_enc.setText(enc_text or "UTF-8")

    def _open_path_in_focused_tab(self, path: Path, *, title: str | None = None) -> None:
        """Open an existing file path in the currently focused tab dock."""
        if not isinstance(path, Path):
            path = Path(str(path))
        if not path.exists():
            QMessageBox.warning(self, "Fehler", f"Datei nicht gefunden: {path}")
            return

        if _fv_classify is None:
            self._open_file_fallback(str(path))
            return

        ftype = _fv_classify(path)
        try:
            if ftype == "image":
                widget = _FVImageWidget(path)
            elif ftype == "pdf":
                widget = _FVPdfWidget(path)
            elif ftype == "markdown":
                widget = _FVMarkdownWidget(path)
            elif ftype in ("text", "code"):
                widget = _FVTextWidget(path, highlight=(ftype == "code"))
            else:
                raise RuntimeError("Dieser Dateityp wird nicht unterstützt.")
        except Exception as exc:
            QMessageBox.warning(self, "Fehler", str(exc))
            return

        tabs = self._get_focused_tab_dock()
        if not tabs:
            QMessageBox.warning(self, "Fehler", "Kein Tab-Dock verfügbar")
            return

        tab_title = title or path.name
        idx = tabs.addTab(widget, tab_title)
        widget.setProperty("file_path", str(path))
        tabs.setCurrentIndex(idx)
        self._update_status_encoding()

    def _open_file(self) -> None:

        """Open a file and display it inside the **focused** tab-dock.

        The heavy-lifting – i.e. figuring out *how* the file should be
        presented (text editor, image label, PDF view, …) – is delegated to
        the external :pymod:`file_viewer` helper module.  This keeps the
        MainAIEditor lean while giving us a single, well-tested
        implementation to render a broad set of file types.
        """

        fname, _ = QFileDialog.getOpenFileName(
            self,
            "Open file",
            str(Path.home()),
            "All files (*)",
        )
        if not fname:
            return

        if _fv_classify is None:

            # file_viewer could not be imported at start-up → fall back to the
            # previous minimal implementation and support only text/images.
            # The original logic has been moved into a helper so that the
            # overall user-experience is preserved even without file_viewer.
            
            self._open_file_fallback(fname)
            return

        path = Path(fname)
        ftype = _fv_classify(path)

        try:
            if ftype == "image":
                widget = _FVImageWidget(path)
            elif ftype == "pdf":
                widget = _FVPdfWidget(path)
            elif ftype == "markdown":           
                widget = _FVMarkdownWidget(path)
            elif ftype in ("text", "code"):
                widget = _FVTextWidget(path, highlight=(ftype == "code"))
            else:
                raise RuntimeError("Dieser Dateityp wird nicht unterstützt.")
        except Exception as exc:
            QMessageBox.warning(self, "Fehler", str(exc))
            return

        # Öffne im fokussierten Dock statt immer im ersten
        tabs = self._get_focused_tab_dock()
        if not tabs:
            QMessageBox.warning(self, "Fehler", "Kein Tab-Dock verfügbar")
            return
            
        idx = tabs.addTab(widget, path.name)
        widget.setProperty("file_path", str(path))
        tabs.setCurrentIndex(idx)
        self._update_status_encoding()

    # -------------------- legacy fallback (text / images only) ------------
    
    def _open_file_fallback(self, fname: str) -> None:  # pragma: no cover
        """Original, reduced implementation – kept as safety-net."""
        file_kind = detect_file_format(fname)

        # Öffne im fokussierten Dock
        tabs = self._get_focused_tab_dock()
        if not tabs:
            QMessageBox.warning(self, "Error", "Kein Tab-Dock verfügbar")
            return

        if file_kind == "text":
            try:
                txt = Path(fname).read_text(encoding="utf-8")
            except OSError as e:
                QMessageBox.critical(self, "Error", f"Cannot read file:\n{e}")
                return
            idx = tabs.addTab(QTextEdit(txt), Path(fname).name)
            tabs.widget(idx).setProperty("file_path", fname)
        elif file_kind == "image":
            pix = QPixmap(fname)
            if pix.isNull():
                QMessageBox.warning(self, "Error", "Unable to load the selected image.")
                return
            lbl = QLabel(alignment=Qt.AlignCenter)
            lbl.setPixmap(pix.scaledToWidth(512, Qt.SmoothTransformation))
            idx = tabs.addTab(lbl, Path(fname).name)
        else:
            QMessageBox.information(
                self,
                "Unsupported type",           
                "This file type cannot be displayed inside the editor.",
            )
            return

        tabs.setCurrentIndex(idx)
        self._update_status_encoding()


    # ------------------------------------------------ about --------------

    @Slot()
    def _about(self):
        QMessageBox.information(
            self, "About",
            "AI Python3 Multi-Agent-Env v0.6\n"            

            "Fully refactored layout – © ai.bentu\nPowered by Qt / PySide6"
        )

    # ------------------------------------------------ view ---------------

    @Slot()
    def _toggle_accent(self):
        current_name = _normalize_accent_name(getattr(self, "_accent_name", "green"))
        try:
            current_index = ACCENT_ORDER.index(current_name)
        except ValueError:
            current_index = 0
        self._accent_name = ACCENT_ORDER[(current_index + 1) % len(ACCENT_ORDER)]
        self._accent = _accent_from_name(self._accent_name)
        _apply_style(self, _build_scheme(self._accent, self._base))
        self._apply_main_splitter_style()
        self._sync_explorer_scheme()
        self._sync_chat_scheme()
        self._sync_control_plane_scheme()
        self._sync_extensions_scheme()
        self._sync_window_titlebar_scheme()
        if hasattr(self, "act_toggle_accent"):
            self.act_toggle_accent.setToolTip(f"Farbschema wechseln (aktuell: {self._accent_name})")

    @Slot(bool)
    def _toggle_grey(self, on: bool):
        self._base = SCHEME_GREY if on else SCHEME_DARK
        self._accent = _accent_from_name(getattr(self, "_accent_name", "green"))
        _apply_style(self, _build_scheme(self._accent, self._base))
        self._apply_main_splitter_style()
        self._sync_explorer_scheme()
        self._sync_chat_scheme()
        self._sync_control_plane_scheme()
        self._sync_extensions_scheme()
        self._sync_window_titlebar_scheme()

    def _sync_explorer_scheme(self) -> None:
        """Keep explorer colors/icons synced after scheme changes."""
        try:
            if not hasattr(self, "explorer") or self.explorer is None:
                return
            scheme = _build_scheme(self._accent, self._base)
            self.explorer.set_text_color(scheme.get("col6", "#E3E3DED6"))
            self.explorer.set_background_color(scheme.get("col7", "#0b0b0b"))
            self.explorer.set_accent_color(scheme.get("col1", "#0fe913"))
        except Exception:
            pass

    def _sync_chat_scheme(self) -> None:
        """Keep AI chat dock, prompt frame and history synced after scheme changes."""
        try:
            self._refresh_chat_toggle_icons()
            chat_dock = getattr(self, "chat_dock", None)
            updater = getattr(chat_dock, "update_scheme", None)
            if callable(updater):
                updater(self._accent, self._base)
                return
            chat_widget = chat_dock.widget() if isinstance(chat_dock, QDockWidget) else None
            widget_updater = getattr(chat_widget, "update_scheme", None)
            if callable(widget_updater):
                widget_updater(self._accent, self._base)
        except Exception:
            pass

    def _sync_control_plane_scheme(self) -> None:
        try:
            if getattr(self, "control_plane_widget", None) is None:
                return
            self.control_plane_widget.update_scheme(self._accent, self._base)
        except Exception:
            pass

    def _sync_extensions_scheme(self) -> None:
        try:
            if getattr(self, "extensions_widget", None) is None:
                return
            self.extensions_widget.update_scheme(self._accent, self._base)
        except Exception:
            pass

    # ──────────────────────── Persistence-Helpers ───────────────────────

    def _settings(self) -> QSettings:  # >>>
        s = QSettings(MainAIEditor.ORG_NAME, MainAIEditor.APP_NAME)
        s.setFallbacksEnabled(False)   # keine systemweiten Defaults
        return s

    # ---------------------------------------------------------------- load

    def _load_ui_state(self):          # >>>
        s = self._settings()
        if s.value("schema", 0, int) != self._SCHEMA:
            self._normalize_toolbar_layout()
            return                     # erste Ausführung oder inkompatibel

        g  = s.value("geometry", type=QByteArray)
        st = s.value("state",    type=QByteArray)
        disable_qt_state = os.getenv("AI_IDE_DISABLE_QT_STATE", "0").strip() in {"1", "true", "True"}
        if (not disable_qt_state) and g and st:
            self.restoreGeometry(g)
            self.restoreState(st)
        self._normalize_toolbar_layout()

        # eigene Felder ---------------------------------------------------

        self._window_titlebar_section_order = self._normalize_window_titlebar_section_order(["external_uri", "tab_strip"])
        self._window_titlebar_uri_anchor_ratio = self._normalize_window_titlebar_uri_anchor_ratio(
            s.value("titlebarUriAnchorRatio", self._window_titlebar_uri_anchor_ratio)
        )
        self._window_titlebar_uri_tab_slot_index = max(0, s.value("titlebarUriTabSlotIndex", self._window_titlebar_uri_tab_slot_index, int))

        self._accent_name = _normalize_accent_name(s.value("accent", "green"))
        self._accent = _accent_from_name(self._accent_name)
        self._base   = SCHEME_GREY  if s.value("base")   == "grey"  else SCHEME_DARK
        _apply_style(self, _build_scheme(self._accent, self._base))
        self._apply_main_splitter_style()
        self._sync_explorer_scheme()
        self._sync_chat_scheme()
        self._sync_window_titlebar_scheme()

        explorer_expand_delta_px = 0

        stored_widths = s.value("workspaceColumnWidths", [260, 760, 460, 180])
        if isinstance(stored_widths, (list, tuple)):
            try:
                parsed_widths = [int(value) for value in list(stored_widths)[:4]]
            except Exception:
                parsed_widths = []
            if len(parsed_widths) == 3 and all(value > 0 for value in parsed_widths):
                parsed_widths.append(180)
            if len(parsed_widths) == 4 and all(value > 0 for value in parsed_widths):
                self._workspace_column_widths = parsed_widths

        snap_offset_applied = s.value("explorerWidthSnapOffsetAppliedV2", True, bool)
        if not snap_offset_applied and len(self._workspace_column_widths) == 4:
            current_left_width = int(self._workspace_column_widths[0])
            snapped_left_width = max(self._EXPLORER_WIDTH_MIN_PX, current_left_width - self._EXPLORER_WIDTH_SNAP_OFFSET_PX)
            self._workspace_column_widths[0] = snapped_left_width
            s.setValue("explorerWidthSnapOffsetAppliedV2", True)

        board_snap_offset_applied = s.value("boardWidthSnapOffsetAppliedV1", False, bool)
        if not board_snap_offset_applied and len(self._workspace_column_widths) == 4:
            current_board_width = int(self._workspace_column_widths[3])
            snapped_board_width = max(self._BOARD_WIDTH_MIN_PX, current_board_width - self._BOARD_WIDTH_SNAP_OFFSET_PX)
            self._workspace_column_widths[3] = snapped_board_width
            s.setValue("boardWidthSnapOffsetAppliedV1", True)

        explorer_expand_applied = s.value("explorerWidthExpandAppliedV1", False, bool)
        if not explorer_expand_applied and len(self._workspace_column_widths) == 4:
            self._workspace_column_widths[0] = max(
                self._EXPLORER_WIDTH_MIN_PX,
                int(self._workspace_column_widths[0]) + 40,
            )
            s.setValue("explorerWidthExpandAppliedV1", True)

        explorer_expand_applied_v2 = s.value("explorerWidthExpandAppliedV2", False, bool)
        if not explorer_expand_applied_v2 and len(self._workspace_column_widths) == 4:
            self._workspace_column_widths[0] = max(
                self._EXPLORER_WIDTH_MIN_PX,
                int(self._workspace_column_widths[0]) + self._EXPLORER_WIDTH_EXPAND_PX,
            )
            explorer_expand_delta_px = self._EXPLORER_WIDTH_EXPAND_PX
            s.setValue("explorerWidthExpandAppliedV2", True)

        stored_explorer_sizes = s.value("explorerSplitterSizes", [380, 130])
        if isinstance(stored_explorer_sizes, (list, tuple)):
            try:
                parsed_explorer_sizes = [int(value) for value in list(stored_explorer_sizes)[:2]]
            except Exception:
                parsed_explorer_sizes = []
            if len(parsed_explorer_sizes) == 2 and all(value > 0 for value in parsed_explorer_sizes):
                self._explorer_splitter_sizes = parsed_explorer_sizes
        self._explorer_database_panel_visible = s.value("showExplorerDatabasePanel", False, bool)
        if isinstance(getattr(self, "explorer_splitter", None), QSplitter):
            if self._explorer_database_panel_visible:
                self.explorer_splitter.setSizes(self._normalize_splitter_sizes(list(self._explorer_splitter_sizes), total=1000))
                self._remember_explorer_splitter_sizes()
            else:
                self.explorer_splitter.setSizes([1, 0])
        
        self.chat_dock.setVisible(s.value("showChat", True,  bool))
        # Control Plane visibility is now managed by extensions_dock (merged into single dock)
        self.extensions_dock.setVisible(s.value("showExtensions", True, bool))

        
        self.files_dock.setVisible(s.value("showExplorer", True,  bool))
        self.console_dock.setVisible(False)
        tab_on = False
        for d in self._tab_docks:
            d.setVisible(False)

        self._rebalance_workspace_columns()
        if explorer_expand_delta_px > 0:
            self._expand_explorer_column_width(explorer_expand_delta_px)

        # Tabs rekonstruieren (optional)

        opened = s.value("openTabs", [])
        if self._editor_surface_enabled() and opened:
            self._tab_docks.clear()
            self._clone_tab_dock(set_current=False)
            tabs: EditorTabs = self._tab_docks[0].widget()
            tabs.clear()
            for name in opened:
                tabs.addTab(QTextEdit(f"# {name}\n"), name)
            tabs.setCurrentIndex(0)

        self._refresh_window_titlebar_state()

    # ---------------------------------------------------------------- save
    
    def _save_ui_state(self):         
        s = self._settings()
        s.clear()                      # sauberer Neu-Write
        s.setValue("schema",   self._SCHEMA)
        # Workaround: on some Qt/PySide6 combinations, saveGeometry/saveState
        # can crash (native segfault) during shutdown. Allow disabling.
        disable_qt_state = os.getenv("AI_IDE_DISABLE_QT_STATE", "0").strip() in {"1", "true", "True"}
        if not disable_qt_state:
            s.setValue("geometry", self.saveGeometry())
            s.setValue("state",    self.saveState())

        s.setValue("accent", _normalize_accent_name(getattr(self, "_accent_name", "green")))
        s.setValue("base",   "grey"  if self._base   is SCHEME_GREY  else "dark")
        s.setValue("titlebarSectionOrder", list(self._window_titlebar_section_order))
        s.setValue("titlebarUriAnchorRatio", float(self._window_titlebar_uri_anchor_ratio))
        s.setValue("titlebarUriTabSlotIndex", int(self._window_titlebar_uri_tab_slot_index))
        s.setValue("showExplorer", self.files_dock.isVisible())
        s.setValue("showConsole",  False)
        s.setValue("showChat", self.chat_dock.isVisible())   
        # Control Plane visibility is now managed by extensions_dock (merged into single dock)
        s.setValue("showExtensions", self.extensions_dock.isVisible())
        s.setValue("showTabDock",  False)
        control_plane_widget = getattr(self, "control_plane_widget", None)
        if control_plane_widget is not None and hasattr(control_plane_widget, "runtime_layout_path"):
            try:
                s.setValue("controlPlaneRuntimeLayoutPath", control_plane_widget.runtime_layout_path())
            except Exception:
                pass
        splitter = getattr(self, "explorer_splitter", None)
        if isinstance(splitter, QSplitter):
            sizes = splitter.sizes()
            if len(sizes) >= 2:
                self._explorer_database_panel_visible = int(sizes[1]) > 0
        self._remember_workspace_column_widths()
        self._remember_explorer_splitter_sizes()
        s.setValue("workspaceColumnWidths", list(self._workspace_column_widths))
        s.setValue("explorerSplitterSizes", list(self._explorer_splitter_sizes))
        s.setValue("showExplorerDatabasePanel", bool(self._explorer_database_panel_visible))

        if self._editor_surface_enabled() and self._tab_docks:
            tabs: EditorTabs = self._tab_docks[0].widget()
            s.setValue("openTabs", [tabs.tabText(i) for i in range(tabs.count())])
        else:
            s.setValue("openTabs", [])

        # Force write to disk (helps if the process crashes later).
        try:
            s.sync()
        except Exception:
            pass

    # -- <- changes 27.07.2025 ------------------------------------- closeEvent

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            self._refresh_window_titlebar_state()
            self._sync_fullscreen_action_state()

    def closeEvent(self, ev):        # >>>
        # 1) save chat history to disk
        try:
            if hasattr(self, "_chat"):
                _maybe_flush_history(self._chat)
        except Exception:
            pass

        # 1b) persist dynamic runtime tabs and widgets
        try:
            control_plane_widget = getattr(self, "control_plane_widget", None)
            if control_plane_widget is not None and hasattr(control_plane_widget, "persist_runtime_tabs_state"):
                control_plane_widget.persist_runtime_tabs_state(force=True)
        except Exception:
            pass

        # 2) save the (unrelated) UI state
        try:
            self._save_ui_state()
        except Exception:
            pass

        super().closeEvent(ev)


def main() -> None:
    safe = _env_truthy("AI_IDE_SAFE", "0")
    minimal = _env_truthy("AI_IDE_MINIMAL", "0")

    app = QApplication(sys.argv)
    app.setApplicationName(MainAIEditor.APP_NAME)
    app.setApplicationDisplayName(MainAIEditor.APP_NAME)
    app.setOrganizationName(MainAIEditor.ORG_NAME)

    # Persist chat history on clean shutdown even if MainAIEditor.closeEvent
    # is not reached (e.g. alternative quit paths).
    # History flush during Qt shutdown can segfault in some environments.
    # Keep it opt-in via AI_IDE_ENABLE_HISTORY_FLUSH_ON_QUIT=1.
    if _env_truthy("AI_IDE_ENABLE_HISTORY_FLUSH_ON_QUIT", "0"):
        try:
            # Wrap in a lambda so PySide doesn't have to bind a classmethod directly.
            app.aboutToQuit.connect(lambda: _maybe_flush_history())
        except Exception:
            pass
    try:
        app.aboutToQuit.connect(_shutdown_loky_runtime)
    except Exception:
        pass

    # Remove system/Qt drop shadows on context menus and ensure true rounded corners
    # (otherwise a dark rectangle can remain visible behind the radius).
    def _install_menu_no_shadow(qapp: QApplication) -> None:
        from PySide6.QtCore import QObject, QEvent
        from PySide6.QtWidgets import QMenu

        class _MenuShadowFilter(QObject):
            def eventFilter(self, obj, event):  # noqa: N802
                try:
                    if isinstance(obj, QMenu) and event.type() in (QEvent.Polish, QEvent.Show):
                        obj.setWindowFlag(Qt.NoDropShadowWindowHint, True)
                        obj.setAttribute(Qt.WA_TranslucentBackground, True)
                        obj.setAttribute(Qt.WA_StyledBackground, True)
                        obj.setAutoFillBackground(False)
                except Exception:
                    pass
                return False

        filt = _MenuShadowFilter(qapp)
        qapp.installEventFilter(filt)
        # keep reference alive
        setattr(qapp, "_menu_shadow_filter", filt)

    _install_menu_no_shadow(app)

    # Crash-isolation helper: allow automated runs that start and quit quickly
    # (useful with QT_QPA_PLATFORM=offscreen).
    try:
        autoquit_ms = int(os.getenv("AI_IDE_AUTOQUIT_MS", "0") or "0")
    except Exception:
        autoquit_ms = 0
    if autoquit_ms > 0:
        try:
            QtCore.QTimer.singleShot(autoquit_ms, app.quit)
        except Exception:
            pass

    if minimal:
        mini = QMainWindow()
        mini.setWindowTitle(f"{MainAIEditor.APP_NAME} - Minimal Mode")
        te = QTextEdit()
        te.setPlainText("Minimal mode active. Use normal mode to reproduce crashes.\n\nEnv flags:\n- AI_IDE_SAFE=1\n- AI_IDE_NO_STYLE=1\n- AI_IDE_QT_DEBUG=1")
        mini.setCentralWidget(te)
        mini.resize(800, 500)
        mini.show()
        try:
            exit_code = app.exec()
        finally:
            _shutdown_loky_runtime()
        sys.exit(exit_code)

    win = MainAIEditor()
    if safe:
        try:
            # Minimal safe tweaks: hide heavy docks by default
            if hasattr(win, "console_dock"):
                win.console_dock.hide()
            if hasattr(win, "chat_dock"):
                win.chat_dock.hide()
        except Exception:
            pass
    win.show()
    try:
        exit_code = app.exec()
    finally:
        _shutdown_loky_runtime()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
