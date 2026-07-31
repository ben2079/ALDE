from __future__ import annotations

# Maintainer contact: see repository README.

import httpx
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError, responses
import base64
import requests 
import subprocess
import os 
import json
import time
from typing import Any, Dict, List
from pathlib import Path

from datetime import datetime
import sys
from dotenv import load_dotenv
from typing import List, Dict, Tuple
from types import SimpleNamespace


_THIS_MODULE = sys.modules.get(__name__)
if _THIS_MODULE is not None:
    if __name__.startswith("ALDE_Projekt.ALDE.alde"):
        sys.modules.setdefault("alde.agents_ccomp", _THIS_MODULE)
    elif __name__.startswith("alde."):
        sys.modules.setdefault("ALDE_Projekt.ALDE.alde.agents_ccomp", _THIS_MODULE)

# NOTE: retry utilities live in `alde.error_recovery`, but this module does
# not currently use them. Avoid importing optional deps at import-time.

try:
    from .counter import Counter  # type: ignore
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from counter import Counter  # type: ignore
    else:
        raise

try:
    from .get_path import GetPath  # type: ignore
except ImportError as e:
    msg = str(e)
    if "no known parent package" in msg or "attempted relative import" in msg:
        from get_path import GetPath  # type: ignore
    else:
        raise

try:
    from .vstores import VectorStore  # type: ignore
except BaseException as e:
    # VectorStore (LangChain/FAISS stack) is optional for the GUI.
    # When unavailable, keep ChatHistory usable for logging/persistence.
    VectorStore = object  # type: ignore
    _VSTORE_IMPORT_ERROR = e
else:
    _VSTORE_IMPORT_ERROR = None

# Prefer importing ChatCom/ChatHistory from `alde.chat_runtime` (or the
# `chat_completion` compatibility alias). This module remains the legacy
# implementation surface for callers that still rely on agents_ccomp.


# ---------------------------------------------------------------------------
# Canonical AppData paths
#
# This repo contains two historical layouts:
#   1) <pkg>/AppData/...            (canonical; alongside the `alde` package)
#   2) <repo>/ALDE/AppData  (legacy; one directory higher)
#
# Additionally, an older bug created a folder with a trailing space:
#   `VSM_3_Data `
#
# We always *write* to the canonical path, but we can *read* from legacy
# locations and migrate once.
# ---------------------------------------------------------------------------

_PKG_ROOT = Path(__file__).resolve().parents[1]          # .../ALDE/ALDE
_REPO_LEVEL = Path(__file__).resolve().parents[2]        # .../ALDE

_APPDATA_CANON = _PKG_ROOT / "AppData"
_APPDATA_LEGACY = _REPO_LEVEL / "AppData"

_VSM3_CANON = _APPDATA_CANON / "VSM_3_Data"
_VSM3_LEGACY = _APPDATA_LEGACY / "VSM_3_Data"

_VSM3_CANON_TRAILING = _APPDATA_CANON / "VSM_3_Data "
_VSM3_LEGACY_TRAILING = _APPDATA_LEGACY / "VSM_3_Data "

_HISTORY_CANON = _VSM3_CANON / "history.json"
_HISTORY_LEGACY = _VSM3_LEGACY / "history.json"
_HISTORY_CANON_TRAILING = _VSM3_CANON_TRAILING / "history.json"
_HISTORY_LEGACY_TRAILING = _VSM3_LEGACY_TRAILING / "history.json"

_MANIFEST_CANON = _VSM3_CANON / "manifest.json"
_MANIFEST_LEGACY = _VSM3_LEGACY / "manifest.json"


def _verbose_terminal_logs_enabled() -> bool:
    return str(os.getenv("AI_IDE_VERBOSE_TERMINAL_LOGS", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _compact_preview(value: Any, limit: int = 220) -> str:
    text = str(value or "")
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(0, limit - 3)]}..."


def _extract_tool_names(tools: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(tools, list):
        return names
    for entry in tools:
        name = ""
        if isinstance(entry, dict):
            function_payload = entry.get("function") if isinstance(entry.get("function"), dict) else {}
            name = str(function_payload.get("name") or entry.get("name") or "").strip()
        else:
            function_payload = getattr(entry, "function", None)
            name = str(getattr(function_payload, "name", "") or getattr(entry, "name", "")).strip()
        if name:
            names.append(name)
    return names


def _extract_tool_call_names(tool_calls: Any) -> list[str]:
    if not isinstance(tool_calls, list):
        return []
    names: list[str] = []
    for call in tool_calls:
        function_payload = getattr(call, "function", None)
        call_name = str(getattr(function_payload, "name", "") or "").strip()
        if not call_name and isinstance(call, dict):
            function_payload = call.get("function") if isinstance(call.get("function"), dict) else {}
            call_name = str(function_payload.get("name") or "").strip()
        if call_name:
            names.append(call_name)
    return names


def _extract_pseudo_tool_call_payload(content: Any) -> dict[str, Any] | None:
    text = str(content or "").strip()
    if not text:
        return None

    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()

    if not (text.startswith("{") and text.endswith("}")):
        return None

    try:
        payload = json.loads(text)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    tool_name = str(
        payload.get("name")
        or payload.get("function_name")
        or payload.get("tool_name")
        or payload.get("function")
        or ""
    ).strip()
    if not tool_name:
        return None

    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("args")
    if arguments is None:
        arguments = payload.get("parameters")
    if isinstance(arguments, str):
        arg_text = arguments.strip()
        if arg_text.startswith("{") and arg_text.endswith("}"):
            try:
                arguments = json.loads(arg_text)
            except Exception:
                arguments = {}
        else:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    return {
        "name": tool_name,
        "arguments": arguments,
    }



"""
    '''
    A trivial singleton wrapper around `list[dict[str, Any]]`.
    Only one instance will ever be created.
    '''
Citizen
    _instance: "ChatHistory | None" = None

    def __new__(cls) -> "ChatHistory":  # noqa: D401
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # Convenience alias
    def add(self, role: str, content: str, **extra: str ) -> None:
        self.append({"role": role, "content": content, **extra})


    _HISTORY = ChatHistory()  # eager instantiation
    """



def _normalize_chat_model_name(model_name: Any, provider: str | None = None) -> str:
    text = str(model_name or "").strip().lstrip("/")
    if not text:
        return ""
    text = " ".join(text.split()).replace(" ", "-").lower()
    provider_name = str(provider or os.getenv("AI_IDE_MODEL_PROVIDER") or "").strip().lower()
    if provider_name in {"github", "github_models", "github-models"} and "/" not in text:
        return f"openai/{text}"
    return text


   # This is the main class that is used to generate a chat response
class ChatCompletion():

    @staticmethod
    def _read_api_key() -> str:
            __root_env_json = Path(__file__).resolve().parents[1] / ".env.json"
            __local_env_json = Path(__file__).with_suffix(".env.json")
            for __json_file in (__root_env_json, __local_env_json):
                if not __json_file.exists():
                    continue
                try:
                    __payload = json.loads(__json_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                __env_payload = __payload.get("env") if isinstance(__payload, dict) and isinstance(__payload.get("env"), dict) else __payload
                if not isinstance(__env_payload, dict):
                    continue
                for __key, __value in __env_payload.items():
                    __name = str(__key or "").strip()
                    if not __name:
                        continue
                    if isinstance(__value, (dict, list)):
                        __text = json.dumps(__value, ensure_ascii=False)
                    else:
                        __text = str(__value)
                    if __text.strip() == "":
                        continue
                    if __name in {"AI_IDE_CHAT_MODEL"}:
                        __text = __text.strip().lstrip("/")
                    os.environ.setdefault(__name, __text)

            __root_env = Path(__file__).resolve().parents[1] / ".env"
            __local_env = Path(__file__).with_suffix(".env")

            for f in (__root_env, __local_env):
                if f.exists():
                    load_dotenv(f, override=False)
                    break

            load_dotenv()                     # fallback
            __key = os.getenv("OPENAI_API_KEY")
            if not __key:
                raise RuntimeError(
                    "OPENAI_API_KEY not found – supply it via .env.json/.env or environment."
                )
            return __key

    # Single shared OpenAI client instance for this module.
    # Lazily initialized so imports work without OPENAI_API_KEY set.
    _client: OpenAI | None = None
    _http_client: httpx.Client | None = None

    @classmethod
    def _build_http_client(cls) -> httpx.Client:
        import httpx

        # Keep the GUI resilient: prevent long-lived idle connections from
        # accumulating in half-closed states (e.g. CLOSE_WAIT) after remote close.
        limits = httpx.Limits(
            max_connections=20,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
        )
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        return httpx.Client(limits=limits, timeout=timeout)

    @classmethod
    def _close_clients(cls) -> None:
        try:
            if cls._client is not None:
                cls._client.close()
        except Exception:
            pass
        finally:
            cls._client = None

        try:
            if cls._http_client is not None:
                cls._http_client.close()
        except Exception:
            pass
        finally:
            cls._http_client = None

    @classmethod
    def _get_client(cls) -> OpenAI:
        if cls._client is None:
            try:
                import atexit
                import httpx
            except Exception:
                atexit = None  # type: ignore
                httpx = None  # type: ignore

            if cls._http_client is None:
                try:
                    cls._http_client = cls._build_http_client()  # type: ignore[arg-type]
                except Exception:
                    cls._http_client = None

            _api_key = cls._read_api_key()
            _base_url = str(os.getenv("OPENAI_BASE_URL") or "").strip()
            if _base_url:
                cls._client = OpenAI(api_key=_api_key, base_url=_base_url, http_client=cls._http_client)
            else:
                cls._client = OpenAI(api_key=_api_key, http_client=cls._http_client)

            try:
                if atexit is not None:
                    atexit.register(cls._close_clients)
            except Exception:
                pass
        return cls._client


class Caller(ChatCompletion):
    """ public attributes,,
    accesable for subclasses 
    and inheritors. """

    '@public'

    _BYdA:str = '%B %Y, %d %A'
    _dmY:str = '%d%m%Y'
    _hMs:str = '%H:%M:%S'  
    _nowTime:datetime = datetime.now()
    _date_f1:str = _nowTime.strftime(_BYdA)
    _date_f2:str = _nowTime.strftime(_dmY)
    _time:str = _nowTime.strftime
    _unix_t:str = _nowTime.timestamp
    spLit:float = _unix_t
    spLit:str = str(spLit).split('.')
    _id:str = f'{spLit[0]}{spLit[1]}'
     
    def __init__(self):
        
        _count = Counter()
        _count.increment()
        self._vnr = _count._global_count
          
        _BYdA:str = '%B %Y, %d %A'
        _dmY:str = '%d%m%Y'
        _hMs:str = '%H:%M:%S'
        _nowTime:datetime = datetime.now()
        self._date_f1:str = _nowTime.strftime(_BYdA)
        self._date_f2:str = _nowTime.strftime(_dmY)
        self._time:str = _nowTime.strftime(_hMs) 
        self._unix_t:str = _nowTime.timestamp() 
  
        spLit:float = self._unix_t
        spLit:str = str(spLit).split('.')
        self._id:str = f'{spLit[0]}{spLit[1]}'
        self.path_read:str = ""
        self.fileTl:str = ""                              # first part of title file to write
        self.path:str = ""                                # get path from sys.arg[]/__file__ / ..
        self.file:str = ""                                # first partof title file to write
        
        if len(sys.argv) >=2: self.path_read = sys.argv[1] 
        else: 
            # Default to workspace root with wildcard pattern
            # User should provide path via command line argument for production use
            self.path_read = str(Path.cwd() / "*" / "*")
        
        self.workdir:str = GetPath().get_workdir()        # type: ignore # path to current working directory
        self.path_new:str = ""                            # get file from sys.arg[]
        self.dbg_file:str = ""
        self.dir:str = ""
    


def _unique(self):             
        """returns a unique file_name 
        with title/  sequenz_number/date/unixtime
        params: titel -> str.
        only to use with with path tools from caller class"""                    
'''
        try: 
            get.path_new = f"{get.path}{get.dir}" if self.workdir() !="dbgfile" else get.path
            print(f"new path:{ get.path_new}")
            get.dbg_file = f"{get.fileTl}_{get._date_f2}_{get._vnr}_{get._id}"
            print(f"debuggig file:{ get.dbg_file}")
            print(f'pfad zum schreiben der datei {get.path_new}')
        except Exception as e:print(f"Error (error while building path or file): {e}")
         
def __repr__():
        return (f"Caller(date='get._date_f1',"f"dateTime='{get._time}',"
                f"args='',unix_t='{get._unix_t}',"
                f"vn='{get._vnr}, path='{get.path_read}',"
                f"SessionID='get._id',Originpath='{get.path}',"
                f"date'{get._date_f2}','filename'{get.file}"
                )
'''
from typing import List, Dict, Tuple

class ChatHistory(VectorStore):
    """ State and persistence for chat messages, nodes and tools history."""
    XMeDB:ChatHistory = List[dict[Any, Any]]  # type: ChatHistory | None
    # NOTE: always use canonical AppData location (alongside the alde package).
    # Keep whitespace-clean; older versions created `VSM_3_Data ` (trailing space).
    _ROOT_DIR = str(_VSM3_CANON)
    _APP_DIR = str(_VSM3_CANON)

    # Vector-store metadata lives in manifest.json.
    _MANIFEST_PATH = str(_MANIFEST_CANON)
    # File-based history persistence is disabled.
    # History is stored and restored from agentsdb agent_memory only.
    _HISTORY_PATH = ""
    _LEGACY_HISTORY_PATHS: list[str] = []
    _FINAL_PATH = ""

    # Autosave (throttled): helps ensure the most recent GUI run is persisted
    # even if the process crashes or is terminated before Qt shutdown hooks run.
    #
    # Controls:
    #   AI_IDE_HISTORY_AUTOSAVE=0/1 (default: 1)
    #   AI_IDE_HISTORY_AUTOSAVE_EVERY_N (default: 8)
    #   AI_IDE_HISTORY_AUTOSAVE_MIN_SECONDS (default: 3)
    _AUTOSAVE_ENABLED = os.getenv("AI_IDE_HISTORY_AUTOSAVE", "1").strip() in {"1", "true", "True", "yes", "Yes", "on", "On"}
    try:
        _AUTOSAVE_EVERY_N = max(1, int(os.getenv("AI_IDE_HISTORY_AUTOSAVE_EVERY_N", "8").strip() or "8"))
    except Exception:
        _AUTOSAVE_EVERY_N = 8
    try:
        _AUTOSAVE_MIN_SECONDS = max(0.0, float(os.getenv("AI_IDE_HISTORY_AUTOSAVE_MIN_SECONDS", "3").strip() or "3"))
    except Exception:
        _AUTOSAVE_MIN_SECONDS = 3.0
    _autosave_dirty_count: int = 0
    _autosave_last_ts: float = 0.0
    _input:List[Dict[str,str]] = []
    # Lazy initialization: heavy operations (model loading, FAISS build)
    # must not run at import time because importing this module is done
    # during application startup and may happen before a QApplication
    # exists. Initialize on first ChatHistory() instantiation instead.
    #vsm_projekt:VectorDBmanager = None  # type: VectorDBmanager | None
    #vsm_application:VectorDBmanager = None  # type: VectorDBmanager | None
    # 1) Gemeinsamer Speicher (Singleton-Light)
    # wird nur 1× angelegt

    #vsm:VectorStore = None  # type: VectorStore | None
    _history_: List[List[Dict[str, str]]] = []
    # vdb_history:VectorStore = None  # type: VectorStore | None
    # Liste bereits existierender Assistenten
    _assis_colec:list[dict[str,str]] = []
    # Lazy import to avoid loading embedding model a startup
    _assistant_id = Caller()._id
    _count:Counter= Counter()
    _msg_iD:int = _count.increment()
    print(f"ChatHistory message ID start at: {_msg_iD}")
    _thread_iD:int = Counter._global_count

    _dev:str = None
    _sys:bool | None = None
    _dev_state:bool | None = None
    _sys_state:bool | None = None
    _AGENT_MEMORY_SYNC_ENV = "AI_IDE_HISTORY_AGENT_MEMORY_SYNC"
    _AGENT_MEMORY_AGENT_LABEL_ENV = "AI_IDE_HISTORY_AGENT_MEMORY_AGENT_LABEL"
    _AGENT_MEMORY_SLOT_ENV = "AI_IDE_HISTORY_AGENT_MEMORY_SLOT"
    _AGENT_MEMORY_CONTEXT_TYPE_ENV = "AI_IDE_HISTORY_AGENT_MEMORY_CONTEXT_TYPE"
    _AGENT_MEMORY_SCOPE_KEY_ENV = "AI_IDE_HISTORY_AGENT_MEMORY_SCOPE_KEY"

    # Ensure app dir exists (safe if already present).
    try:
        os.makedirs(_APP_DIR, exist_ok=True)
    except Exception:
        pass
    # ----------------------------------------------------------------------
    # 1) Aus _history_ alle eindeutige_APP_DIR (assistant-name, assistant-id)-Paare
    #    extrahieren und einmalig in _assis_colec ablegen
    # ----------------------------------------------------------------------
    # ----------------------------------------------------------------------
    # 2) Liefert zwei Listen: alle Namen, alle IDs  (für Abfragen o. Ä.)
    # ----------------------------------------------------------------------
    @staticmethod
    def _deep_get(obj: Any, key_path: str) -> Any:
        """Safe getter for nested structures.

        Supports dotted paths like "data.user.id".
        Returns None when the path cannot be resolved.
        """

        if not key_path:
            return None

        cur: Any = obj
        for part in str(key_path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
                continue

            if isinstance(cur, list):
                # Optional numeric list indexing ("items.0.name")
                if part.isdigit():
                    idx = int(part)
                    if 0 <= idx < len(cur):
                        cur = cur[idx]
                        continue
                    return None
                # Non-numeric list traversal is not well-defined; stop here.
                return None

            return None

        return cur

    @staticmethod
    def _value_matches(value: Any, needle: str) -> bool:
        """Return True if *needle* matches (possibly nested) *value*."""

        if value is None:
            return False

        if isinstance(value, (str, int, float, bool)):
            return str(value) == needle

        if isinstance(value, dict):
            for k, v in value.items():
                if str(k) == needle:
                    return True
                if ChatHistory._value_matches(v, needle):
                    return True
            return False

        if isinstance(value, (list, tuple)):
            return any(ChatHistory._value_matches(v, needle) for v in value)

        # Fallback for custom objects
        try:
            return str(value) == needle
        except Exception:
            return False

    @staticmethod
    def _deep_match(obj: Any, query: Any) -> bool:
        """Return True if *obj* matches *query*.

        - If *query* is a dict: all keys in *query* must exist in *obj* and match.
          Keys may be dotted paths ("data.user.id").
        - If *query* is a list: each query item must match at least one element in *obj*.
        - Otherwise: direct equality.
        """

        if isinstance(query, dict):
            if not isinstance(obj, dict):
                return False
            for k, v in query.items():
                if isinstance(k, str) and "." in k:
                    candidate = ChatHistory._deep_get(obj, k)
                else:
                    candidate = obj.get(k) if isinstance(obj, dict) else None
                if not ChatHistory._deep_match(candidate, v):
                    return False
            return True

        if isinstance(query, list):
            if not isinstance(obj, list):
                return False
            return all(any(ChatHistory._deep_match(item, q) for item in obj) for q in query)

        return obj == query

    @classmethod
    def find(cls, query: dict[str, Any], *, data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Find history entries matching a (possibly nested) query dict."""

        haystack = data if data is not None else cls._history_
        if not isinstance(haystack, list):
            return []
        return [entry for entry in haystack if isinstance(entry, dict) and cls._deep_match(entry, query)]

    @classmethod
    def _key_values(cls,keys: List[str], data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return a list of dicts containing only the requested *keys*."""

        pre_filter: List[Dict[str, Any]] = []
        if not isinstance(data, list):
            return pre_filter
        for entry in data:
            if not isinstance(entry, dict):
                continue
            chunk: Dict[str, Any] = {}
            for key in keys:
                value = cls._deep_get(entry, key)
                if value in (None, ""):
                    continue
                chunk[key] = value
            if chunk:
                pre_filter.append(chunk)
        return pre_filter
    @classmethod
    def check(cls, _value: str, keys: List[str],*
              , data: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        """Return the first key-bundle where *_value* matches one of the keys."""

        for data_objct in cls._key_values(keys, data):
            if any(cls._value_matches(data_objct.get(key), _value) for key in keys):
                post_filter = data_objct
                return post_filter 

        cls._value_name = None
        return None

    @classmethod
    def _history_agent_label(cls) -> str:
        return str(os.getenv(cls._AGENT_MEMORY_AGENT_LABEL_ENV, "_chat_history")).strip() or "_chat_history"

    @classmethod
    def _history_context_type(cls) -> str:
        return str(os.getenv(cls._AGENT_MEMORY_CONTEXT_TYPE_ENV, "history.msg")).strip() or "history.msg"

    @staticmethod
    def _history_message_from_session_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        history_message_payload = payload.get("history.msg")
        if not isinstance(history_message_payload, dict):
            session_payload = payload.get("session")
            if isinstance(session_payload, dict):
                agent_memory_payload = session_payload.get("agent_memory")
                if isinstance(agent_memory_payload, dict):
                    history_message_payload = agent_memory_payload.get("history.msg")

        if not isinstance(history_message_payload, dict):
            return None

        content_payload = history_message_payload.get("content")
        if isinstance(content_payload, (dict, list, tuple)):
            try:
                content_payload = json.dumps(content_payload, ensure_ascii=False)
            except Exception:
                content_payload = str(content_payload)
        elif content_payload is None:
            content_payload = ""
        elif not isinstance(content_payload, (str, int, float, bool)):
            content_payload = str(content_payload)

        tool_name = str(history_message_payload.get("tool_name") or "").strip()
        tool_call_id = str(history_message_payload.get("tool_call_id") or "").strip()

        normalized_message: dict[str, Any] = {
            "message-id": history_message_payload.get("message_id"),
            "role": str(history_message_payload.get("role") or "user").strip() or "user",
            "content": content_payload,
            "object": str(history_message_payload.get("object_name") or "chat"),
            "date": str(history_message_payload.get("date") or ""),
            "time": str(history_message_payload.get("time") or ""),
            "thread-name": str(history_message_payload.get("thread_name") or ""),
            "thread-id": history_message_payload.get("thread_id"),
            "assistant-name": str(history_message_payload.get("assistant_name") or ""),
            "assistant-id": str(history_message_payload.get("assistant_id") or ""),
            "tools": [],
            "data": None,
            "tool_choices": "auto",
            "dev": None,
            "sys": None,
            "tool_calls": [],
            "tool_response_required": True,
        }
        if tool_name:
            normalized_message["name"] = tool_name
        if tool_call_id:
            normalized_message["tool_call_id"] = tool_call_id
        return normalized_message

    @classmethod
    def _load_history_messages_from_agent_memory(cls) -> list[dict[str, Any]]:
        try:
            try:
                from .agents_db import AGENT_MEMORY_SERVICE  # type: ignore
            except ImportError as e:
                msg = str(e)
                if "attempted relative import" in msg or "no known parent package" in msg:
                    try:
                        from alde.agents_db import AGENT_MEMORY_SERVICE  # type: ignore
                    except ImportError:
                        from agents_db import AGENT_MEMORY_SERVICE  # type: ignore
                else:
                    raise
        except Exception:
            return []

        try:
            try:
                from .agents_tools import DOCUMENT_REPOSITORY  # type: ignore
            except ImportError as e:
                msg = str(e)
                if "attempted relative import" in msg or "no known parent package" in msg:
                    try:
                        from alde.agents_tools import DOCUMENT_REPOSITORY  # type: ignore
                    except ImportError:
                        from agents_tools import DOCUMENT_REPOSITORY  # type: ignore
                else:
                    raise
        except Exception:
            DOCUMENT_REPOSITORY = None  # type: ignore[assignment]

        configured_scope_key = str(os.getenv(cls._AGENT_MEMORY_SCOPE_KEY_ENV, "")).strip()
        history_agent_label = cls._history_agent_label()
        history_context_type = cls._history_context_type()
        history_memory_slot = AGENT_MEMORY_SERVICE.load_amemo_slot(
            job_name=str(os.getenv(cls._AGENT_MEMORY_SLOT_ENV, "history.msg")).strip() or "history.msg",
            tool_name="chat_history",
        )

        history_entry_list: list[tuple[str, int, dict[str, Any]]] = []

        def collect_entry_payloads(object_memory: Any) -> None:
            if not isinstance(object_memory, dict):
                return
            session_context = object_memory.get("session_context")
            if not isinstance(session_context, dict):
                return
            entry_list = session_context.get("entries")
            if not isinstance(entry_list, list):
                return

            for entry_index, entry_payload in enumerate(entry_list):
                if not isinstance(entry_payload, dict):
                    continue
                if str(entry_payload.get("context_type") or "").strip() != history_context_type:
                    continue
                normalized_message = cls._history_message_from_session_payload(entry_payload.get("payload"))
                if not isinstance(normalized_message, dict):
                    continue
                entry_timestamp = str(entry_payload.get("timestamp") or "").strip()
                history_entry_list.append((entry_timestamp, entry_index, normalized_message))

        if configured_scope_key:
            resolved_scope_key = AGENT_MEMORY_SERVICE.load_session_scope_key(
                scope_key=configured_scope_key,
                thread_id=None,
            )
            object_memory = AGENT_MEMORY_SERVICE.load_amemo(
                agent_label=history_agent_label,
                memory_slot=history_memory_slot,
                scope_key=resolved_scope_key,
            )
            collect_entry_payloads(object_memory)
        elif DOCUMENT_REPOSITORY is not None:
            try:
                agent_memory_db = DOCUMENT_REPOSITORY.load_db(
                    db_name=AGENT_MEMORY_SERVICE.AGENT_MEMORY_OBJECT_NAME,
                    obj_name=AGENT_MEMORY_SERVICE.AGENT_MEMORY_OBJECT_NAME,
                )
            except Exception:
                agent_memory_db = {}

            record_map = agent_memory_db.get(AGENT_MEMORY_SERVICE.AGENT_MEMORY_OBJECT_NAME)
            if isinstance(record_map, dict):
                for record_payload in record_map.values():
                    if not isinstance(record_payload, dict):
                        continue
                    record_job_name = str(record_payload.get("job_name") or "").strip()
                    if record_job_name and record_job_name != history_memory_slot:
                        continue
                    record_agent_label = str(record_payload.get("agent") or "").strip()
                    if record_agent_label and record_agent_label != history_agent_label:
                        continue

                    object_memory = record_payload.get(AGENT_MEMORY_SERVICE.AGENT_MEMORY_OBJECT_NAME)
                    collect_entry_payloads(object_memory)

        history_entry_list.sort(
            key=lambda item: (
                str(item[0] or ""),
                str((item[2] or {}).get("message-id") or ""),
                int(item[1]),
            )
        )

        deduplicated_history: list[dict[str, Any]] = []
        seen_fingerprint_set: set[str] = set()
        for _timestamp, _entry_index, message_payload in history_entry_list:
            try:
                fingerprint = json.dumps(
                    {
                        "message-id": message_payload.get("message-id"),
                        "role": message_payload.get("role"),
                        "thread-id": message_payload.get("thread-id"),
                        "content": message_payload.get("content"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            except Exception:
                fingerprint = str(message_payload)

            if fingerprint in seen_fingerprint_set:
                continue
            seen_fingerprint_set.add(fingerprint)
            deduplicated_history.append(message_payload)

        return deduplicated_history

    @classmethod
    def _load(cls,path:str|None=None) -> List[Any]:
            _ = path
            return cls._load_history_messages_from_agent_memory()

    @classmethod
    def _cleanup_legacy_history_files(cls, *, loaded_data: list[Any] | None = None) -> None:
        _ = loaded_data
        # File-based history behavior is disabled.
        return
  
    """
    Hält den kompletten Nachrichten­verlauf **prozessweit** vor.
    Jede Instanz greift auf dieselbe Liste `_history_` zu.
    """
    """
    Minimal helper that hides all file-system interaction.
            Persistence
        p = ChatHistory()                     # create helper
        history = p.load()                    # load old history
        ...
        p.flush(history)                      # store on exit
    """
    @classmethod
    def _flush(cls) -> None:
        # File-based persistence is disabled. History is synced per message
        # via _store_history_msg_to_session_agent_memory.
        return

    @classmethod
    def _maybe_autosave(cls) -> None:
        # File autosave is disabled together with file history persistence.
        return

    # ------------------------------------------------------------------
    #                         __init__()
    #
    #     Alias auf die Klassen­variable legen (kein Überschreiben!)
    #            self._history_ = ChatHistory._history_
    #      
    # ------------------------------------------------------------------
    
    # <-- 05.08.2025 ---------------------------------------------------
    #_msg_iD: int = _count.increment()
    

    def __init__(self) -> None:
        self._dmY = '%d%m%Y'
        self._hMs = '%H:%M:%S'
        self._nowTime: datetime = datetime.now()
        self._date: str = self._nowTime.strftime(self._dmY)
        self._time: str = self._nowTime.strftime(self._hMs)
        self._name:str = ""
        self._history_ = ChatHistory._history_
        self.current_thread_id = None

    # Public alias for backward/UX naming: many call sites expect `ChatHistory.log()`.
    def log(self, *args: Any, **kwargs: Any) -> None:
        return self._log(*args, **kwargs)

    # Vector store initialization - call this explicitly when needed
        """Initialize vector stores lazily - call this when you actually need them."""
        # Ensure the vector-store systems are initialized lazily and
        # resiliently. This avoids heavy model loads and file I/O at
        # import time which can interfere with GUI startup.
    def init_vector_store(self) -> None:
        try:
            if VectorStore is object:
                raise RuntimeError(f"VectorStore unavailable: {_VSTORE_IMPORT_ERROR}")
            # Lazy import to avoid loading embedding model at startup
            if ChatHistory.vdb_history is None:
                ChatHistory.vdb_history = VectorStore
                 # type: VectorStore | None

                # Instantiate VecStore but avoid heavy initialization at import time.
                # initialize() may load models / files; defer until first use.
                # override default paths to point to AppData/VSM_1_Data, ...
                base_dir = GetPath()._parent(parg=f"{__file__}")
                store_path = f"{base_dir}/AppData/VSM_1_Data"
                manifest_file = f"{base_dir}/AppData/VSM_1_Data/manifest.json"  
 
                ChatHistory.vdb_projekt = VectorStore(
                    store_path=store_path, 
                manifest_file=manifest_file
                )
                ChatHistory.vdb_projekt.build(GetPath().get_path(
                    parg=f"{__file__}",opt="p"))
                
                """
                # Index erstellen / erweitern
                store_path = GetPath()._parent(
                    parg=f"{__file__}"
                ) + "AppData/VSM_2_Data"
                manifest_file = GetPath()._parent(
                    parg=f"{__file__}"
                ) + "AppData/VSM_2_Data/manifest.json"

                ChatHistory.vsm_application = self.VectorDBmanager(
                    store_path=store_path, 
                manifest_file=manifest_file
                )
                ChatHistory.vsm_application.build(GetPath().get_path(
                    parg="home ben Applications Job_offers",opt="s"))
                """

                store_path = GetPath()._parent(
                    parg=f"{__file__}",
                ) + "/AppData/VSM_3_Data"
                manifest_file = GetPath()._parent(
                    parg=f"{__file__}"
                ) + "/AppData/VSM_3_Data/manifest.json"

                ChatHistory.vdb_history = VectorStore(
                    store_path=store_path,  
                manifest_file=manifest_file
                )
                #ChatHistory.vdb_history.wipe()
                # Keep memorydb scoped to its own store directory; building from
                # whole AppData pulls in VSM_4_Data job docs and pollutes results.
                ChatHistory.vdb_history.build(store_path)

        except Exception as e:
            # Non-fatal: log and continue. The app can still run without
            # embeddings; vector features will be unavailable until the
            # user triggers a rebuild.
            print(f"[WARNING] VectorStore initialization failed: {e}")

    # 2) the chat history will be load from disk to cache
    # ------------------------------------------------------------------
    #
    # 2) Öffentliche Methoden
    #
    # ------------------------------------------------------------------
    # ---------------------- NEW 25.07.2025 ---------------- persistence

    def get_history(): return ChatHistory._history_           # <- hinzugefuegt am 24.08.2025
    """Initialize vector stores lazily - call this when you actually need them."""

    @classmethod
    def _store_history_msg_to_session_agent_memory(cls, message_payload: dict[str, Any]) -> None:
        if not isinstance(message_payload, dict):
            return

        sync_enabled = str(os.getenv(cls._AGENT_MEMORY_SYNC_ENV, "1")).strip().lower()
        if sync_enabled in {"0", "false", "no", "off"}:
            return

        try:
            try:
                from .agents_db import AGENT_MEMORY_SERVICE  # type: ignore
            except ImportError as e:
                msg = str(e)
                if "attempted relative import" in msg or "no known parent package" in msg:
                    try:
                        from alde.agents_db import AGENT_MEMORY_SERVICE  # type: ignore
                    except ImportError:
                        from agents_db import AGENT_MEMORY_SERVICE  # type: ignore
                else:
                    raise
        except Exception:
            return

        raw_thread_id = message_payload.get("thread-id")
        thread_id: int | None = None
        if isinstance(raw_thread_id, int):
            thread_id = raw_thread_id
        elif isinstance(raw_thread_id, str):
            normalized_thread_id = raw_thread_id.strip()
            if normalized_thread_id.isdigit():
                try:
                    thread_id = int(normalized_thread_id)
                except Exception:
                    thread_id = None

        configured_scope_key = str(os.getenv(cls._AGENT_MEMORY_SCOPE_KEY_ENV, "")).strip() or None
        scope_key = AGENT_MEMORY_SERVICE.load_session_scope_key(
            scope_key=configured_scope_key,
            thread_id=thread_id,
        )

        agent_label = str(os.getenv(cls._AGENT_MEMORY_AGENT_LABEL_ENV, "_chat_history")).strip() or "_chat_history"
        memory_slot = AGENT_MEMORY_SERVICE.load_amemo_slot(
            job_name=str(os.getenv(cls._AGENT_MEMORY_SLOT_ENV, "history.msg")).strip() or "history.msg",
            tool_name="chat_history",
        )
        context_type = str(os.getenv(cls._AGENT_MEMORY_CONTEXT_TYPE_ENV, "history.msg")).strip() or "history.msg"

        content_payload = message_payload.get("content")
        if isinstance(content_payload, (dict, list, tuple)):
            try:
                content_payload = json.dumps(content_payload, ensure_ascii=False)
            except Exception:
                content_payload = str(content_payload)
        elif not isinstance(content_payload, (str, int, float, bool)) and content_payload is not None:
            content_payload = str(content_payload)

        history_message_payload = {
            "message_id": message_payload.get("message-id"),
            "role": str(message_payload.get("role") or ""),
            "content": content_payload,
            "date": str(message_payload.get("date") or ""),
            "time": str(message_payload.get("time") or ""),
            "thread_name": str(message_payload.get("thread-name") or ""),
            "thread_id": thread_id,
            "assistant_name": str(message_payload.get("assistant-name") or ""),
            "assistant_id": str(message_payload.get("assistant-id") or ""),
            "tool_call_id": str(message_payload.get("tool_call_id") or ""),
            "tool_name": str(message_payload.get("name") or ""),
            "object_name": str(message_payload.get("object") or "chat"),
        }
        session_payload = {
            "history.msg": history_message_payload,
            "session": {
                "agent_memory": {
                    "history.msg": history_message_payload,
                }
            },
        }

        try:
            AGENT_MEMORY_SERVICE.append_session_context(
                agent_label=agent_label,
                memory_slot=memory_slot,
                scope_key=scope_key,
                context_type=context_type,
                payload=session_payload,
                runtime_metadata={
                    "job_name": memory_slot,
                    "tool_name": "chat_history",
                    "workflow_name": "chat_runtime",
                    "role": "chat_history",
                },
                system_prompt="ChatHistory session cache for history.msg",
                source_agent_label=agent_label,
            )
        except Exception:
            pass

    def _log(
        self,
        _role: str = 'user',
        _content: str | list = None,
        _obj: str = "",
        _data: list | None = None,
        _thread_name: str | None = "" or 'chat',
        _name: str | None = "",
        _dev: bool = None,
        _sys: bool = None,
        _tool_calls: list | None = None,
        _tool_call_id: str | None = None,
        _name_tool: str | None = None,  # Renamed to avoid conflict with _name
        _tool_response_required: bool = True,
) ->     None:
        # ... (existing normalization code) ...
        """Log a message or State to the history with detailed metadata."""

        # Normalize role to prevent OpenAI API errors like "Invalid value: 'system '".
        if isinstance(_role, str):
            _role = _role.strip()

       
        
        # Serialize tool_calls - handle both objects and dicts
        def serialize_tc(tc):
            if isinstance(tc, dict):
                return tc
            elif hasattr(tc, 'model_dump'):
                return tc.model_dump()
            elif hasattr(tc, '__dict__'):
                return {
                    'id': getattr(tc, 'id', ''),
                    'type': 'function',
                    'function': {
                        'name': getattr(tc.function, 'name', '') if hasattr(tc, 'function') else '',
                        'arguments': getattr(tc.function, 'arguments', '{}') if hasattr(tc, 'function') else '{}'
                    }
                }
            return tc
        
        serialized_tool_calls = [serialize_tc(tc) for tc in _tool_calls] if _tool_calls else []

    # Defensive: some callers accidentally pass a non-string (e.g. list/dict)
        # for `_name_tool`. OpenAI expects `message.name` to be a string.
        if _name_tool is not None and not isinstance(_name_tool, str):
            try:
                if isinstance(_name_tool, (list, tuple)) and _name_tool:
                    first = _name_tool[0]
                    if isinstance(first, dict):
                        cand = first.get("name") or first.get("type")
                        if isinstance(cand, str) and cand:
                            _name_tool = cand
                        else:
                            _name_tool = json.dumps(_name_tool, ensure_ascii=False)
                    elif all(isinstance(x, str) for x in _name_tool):
                        _name_tool = ",".join(_name_tool)
                    else:
                        _name_tool = json.dumps(_name_tool, ensure_ascii=False)
                elif isinstance(_name_tool, dict):
                    cand = _name_tool.get("name") or _name_tool.get("type")
                    _name_tool = cand if isinstance(cand, str) else json.dumps(_name_tool, ensure_ascii=False)
                else:
                    _name_tool = str(_name_tool)
            except Exception:
                _name_tool = None

        # Defensive: tool_call_id must be hashable (string) because we later map
        # tool_call_id -> tool response in `_insert()`. If a list slips in here,
        # it will crash with "unhashable type: 'list'".
        if _tool_call_id is not None and not isinstance(_tool_call_id, str):
            try:
                if isinstance(_tool_call_id, (list, tuple)):
                    if all(isinstance(x, str) for x in _tool_call_id):
                        _tool_call_id = ",".join(_tool_call_id)
                    else:
                        _tool_call_id = json.dumps(_tool_call_id, ensure_ascii=False)
                elif isinstance(_tool_call_id, dict):
                    _tool_call_id = json.dumps(_tool_call_id, ensure_ascii=False)
                else:
                    _tool_call_id = str(_tool_call_id)
            except Exception:
                _tool_call_id = None
        
        _message: dict = {
            'message-id':  self._count.increment(),
            'role': _role,
            'content': _content,
            'object':'chat',
            'date': datetime.now().strftime('%Y-%m-%d'),
            'time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'thread-name': _thread_name,
            'thread-id': self._thread_iD,
            'assistant-name': _name,
            'assistant-id': self._assistant_id,
            'tools': [],
            'data': _data,
            'tool_choices': 'auto',
            'dev': None,
            'sys': None,
            'tool_calls': serialized_tool_calls,
            'tool_call_id': _tool_call_id,
            'name': _name_tool,  # For tool messages
            'tool_response_required': bool(_tool_response_required),
        }                             # instruction for the assistant      
                                              # if the assistant did'n exist already a name ,
                                          # assitants are derivates of real existing modell
                                          # object as an general classification, objects are predefined.
                                          # a new assistant must match a classification, if not its creation is omitted.
                                          # Validierungs-/Debug-Ausgabe   (kann später entfernt werden)

        message_logged = False
       
        if ChatHistory._dev_state == False and _role == "developer" and _dev == True:
            ChatHistory._dev_state = True
            try:           
                ChatHistory._history_.append(_message)
                message_logged = True
            except Exception as e:
                print(f'Error during log messages to history: {e}')       
        elif ChatHistory._sys_state == False and _role == "system" and _sys == True:
            ChatHistory._sys_state = False
            try:
                ChatHistory._history_.append(_message)
                message_logged = True
            except Exception as e:
                print(f'Error during log messages to history: {e}')       
        elif _role == "user":
            try:  
                ChatHistory._history_.append(_message)
                message_logged = True
            except Exception as e:
                print(f'Error during log messages to history: {e}')       
        elif _role == "assistant":
            try:  
                ChatHistory._history_.append(_message)
                message_logged = True
            except Exception as e:
                print(f'Error during log messages to history: {e}')   
        elif _role == "tool":
            try:
                ChatHistory._history_.append(_message)
                message_logged = True
            except Exception as e:
                print(f'Error during log messages to history: {e}')

        if message_logged:
            try:
                ChatHistory._store_history_msg_to_session_agent_memory(_message)
            except Exception:
                pass

        # Throttled autosave to reduce history loss on crashes.
        try:
            ChatHistory._maybe_autosave()
        except Exception:
            pass

# ------------------------------------------------------------------------------
    def _insert(self,tool:bool | None = False, f_depth:int | None = None , f_role:str | None = None) -> List[Dict[str, Any]]:
        """Return the message object for the model API. tool = true includes messages with role 'tool'.
        f_deph limits the number of messages returned to the last f_deph entries.
        if role is specified, only messages with that role are included.
        
        IMPORTANT: This method validates tool_call sequences to prevent OpenAI API errors.
        Assistant messages with tool_calls that lack matching tool responses are stripped of tool_calls.
        """
        
        # IMPORTANT: Avoid sending unbounded chat history to the model.
            # Default to a bounded slice of recent history
        def _truncate_text(val: Any, *, max_chars: int) -> str:
            if val is None:
                return ""
            if not isinstance(val, str):
                try:
                    val = json.dumps(val, ensure_ascii=False)
                except Exception:
                    val = str(val)
            if len(val) <= max_chars:
                return val
            return val[:max_chars] + "\n\n[TRUNCATED]"

        def _normalize_msg_name(val: Any) -> str | None:
            """OpenAI chat messages accept optional `name` but it must be a string."""
            if val is None:
                return None
            if isinstance(val, str):
                return val
            try:
                if isinstance(val, (list, tuple)) and val:
                    first = val[0]
                    if isinstance(first, dict):
                        cand = first.get("name") or first.get("type")
                        if isinstance(cand, str) and cand:
                            return cand
                    if all(isinstance(x, str) for x in val):
                        joined = ",".join(val)
                        return joined or None
                    return json.dumps(val, ensure_ascii=False)
                if isinstance(val, dict):
                    cand = val.get("name") or val.get("type")
                    if isinstance(cand, str) and cand:
                        return cand
                    return json.dumps(val, ensure_ascii=False)
                return str(val)
            except Exception:
                return None

        # Build valid message sequence ensuring tool messages follow their tool_calls
        # Step 1: Map tool_call_id -> tool response
        tool_responses: dict[str, dict] = {}
        for idx, entry in enumerate(self._history_):
            idx = idx
            if isinstance(entry, dict) and entry.get("role") == "tool":
                tid = entry.get("tool_call_id")
                if tid:
                    tool_name = _normalize_msg_name(entry.get("name"))
                    tool_responses[tid] = {
                        "role": "tool",
                        # Tool outputs can be massive (vector results, JSON dumps). Hard-cap them.
                        "content": _truncate_text(entry.get("content", ""), max_chars=8000),
                        "tool_call_id": tid,
                        "tool_response_required": bool(entry.get("tool_response_required", True)),
                        **({"name": tool_name} if tool_name else {}),
                    }
            # If callers pass 0/None, treat it as "recent history".
        
        # Step 2: Build filtered list with proper sequencing
        filtered: list[dict[str, Any]] = []
        for idx, entry in enumerate(self._history_):
            idx = idx
            if not isinstance(entry, dict):
                continue
        
            role = entry.get("role")
            # Filter to current conversation thread (prevents mixing threads)
            entry_thread_id = entry.get("thread-id")
            if entry_thread_id is not None and entry_thread_id != self._thread_iD:
                continue

            role = entry.get("role")
            # Skip tool messages here - they will be inserted after their assistant message
            if role == "tool":
                continue

             # Build base message
            msg = {
                "role": role,
                "content": entry.get("content", ""),
            }
            
            # Ensure content is a string and clamp size.
            msg["content"] = _truncate_text(msg.get("content"), max_chars=20000)
            
            name_val = entry.get("name")
            name_str = _normalize_msg_name(name_val)
            if name_str:
                msg["name"] = name_str
            
            # Handle assistant messages with tool_calls
            if role == "assistant" and entry.get("tool_calls") and tool:
                tool_calls = entry.get("tool_calls")
                # Only include tool_calls that have matching responses
                valid_tool_calls = []
                pending_tool_responses = []
                
                for tc in tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id and tc_id in tool_responses:
                        response_message = tool_responses[tc_id]
                        if not bool(response_message.get("tool_response_required", True)):
                            continue
                        # Serialize tool_call
                        if isinstance(tc, dict):
                            valid_tool_calls.append(tc)
                        elif hasattr(tc, "model_dump"):
                            valid_tool_calls.append(tc.model_dump())
                        else:
                            valid_tool_calls.append({
                                "id": tc_id, 
                                "type": "function", 
                                "function": {
                                    "name": getattr(tc.function, "name", "") if hasattr(tc, "function") else "",
                                    "arguments": getattr(tc.function, "arguments", "{}") if hasattr(tc, "function") else "{}"
                                }
                            })
                        pending_tool_responses.append(response_message)
                
                if valid_tool_calls:
                    msg["tool_calls"] = valid_tool_calls
                    filtered.append(msg)
                    # Immediately add the tool responses after assistant message
                    filtered.extend(pending_tool_responses)
                    continue  # Skip the normal append

                if not str(msg.get("content") or "").strip() or str(msg.get("content") or "").strip() == "[tool calls executed]":
                    continue
            
            # Skip role filter
            if msg.get("role") == f_role:
                continue
            filtered.append(msg)
            

        # IMPORTANT: Avoid sending unbounded chat history to the model.
        # If f_depth is None/0/negative, fall back to a safe default.
        try:
            depth = int(f_depth) if f_depth is not None else 0
        except Exception:
            depth = 0
        if depth <= 0:
            depth = 15

        out = filtered[-depth:]
        # Safety: never start a prompt with a tool message.
        # If truncation cuts off the preceding assistant/tool_calls, OpenAI rejects the request.
        while out and isinstance(out[0], dict) and out[0].get("role") == "tool":
            out.pop(0)
        print(f'Filtered messages for model API (count={len(out)})')
        
        return out
        
    # ---------------------------------------------------------------------------
    # 3) Komfort-Ausgabe (Debug)
    # ---------------------------------------------------------------------------

    def __repr__(self) -> str:                       # pragma: no cover
            return f"{self.__class__.__name__}{self._history_!r}"

# This class is used to generate a image description from an image URL
class ImageDescription(ChatCompletion):

    def __init__(self,
            _model:str = None,
            _url:str = str,
            _input_text:str = str,
            res:str = None
            ):

        super().__init__()
        self.model = _model
        self._url =_url
        self.input_text =_input_text
        self._res = "high"    
        
        message = [{"role":"user", "content":
           [{"type":"text", "text":self.input_text},
           {"type":"image_url", "image_url": 
           {"url":f"data:image/jpeg;base64,{self._img_to_b64()}",
           "detail":self._res
           }
        }
        ]
        }
        ]
        try:
            ChatHistory().log(
                _role="tool",
                _content="openai.chat.completions.create request",
                _obj="model",
                _data={"model": self.model, "n_messages": len(message)},
                _thread_name="model",
                _name_tool="openai.chat.completions.create",
            )
        except Exception:
            pass

        try:
            self.img_response = self._get_client().chat.completions.create(
                model=self.model,
                messages=message,
            )
        except Exception as exc:
            try:
                ChatHistory().log(
                    _role="tool",
                    _content="openai.chat.completions.create error",
                    _obj="model",
                    _data={"model": self.model, "error": f"{type(exc).__name__}: {exc}"},
                    _thread_name="model",
                    _name_tool="openai.chat.completions.create",
                )
            except Exception:
                pass
            raise

        try:
            ChatHistory().log(
                _role="tool",
                _content="openai.chat.completions.create response",
                _obj="model",
                _data={
                    "model": self.model,
                    "response_id": getattr(self.img_response, "id", None),
                    "has_choices": bool(getattr(self.img_response, "choices", None)),
                },
                _thread_name="model",
                _name_tool="openai.chat.completions.create",
            )
        except Exception:
            pass
    def _img_to_b64(_url:str) -> str|list:  
        for url in _url:
            with open(url, "rb") as _f:
                return base64.b64encode(
                _f.read()).decode('utf-8')  

    def get_descript(self):
            print(self.img_response.choices[0].message.content)
            return self.img_response
    

class ChatDialogue(ChatCompletion):
    _object:str = "audio"

    def __init__(self,
        model:str = None,
        mod:str = None
        ): # ((;
      
       super().__init__(
            )

       self.model = model 
       self.mod = mod
       self.voice = "shimmer"
       self.format = "mp3"

    def get_response(self,input_text):
        try:
            ChatHistory().log(
                _role="tool",
                _content="openai.chat.completions.create request",
                _obj="model",
                _data={"model": self.model, "modalities": ["text", "audio"]},
                _thread_name="model",
                _name_tool="openai.chat.completions.create",
            )
        except Exception:
            pass

        try:
            self.response = self._get_client().chat.completions.create(
                model=self.model,
                modalities=["text", "audio"],
                audio={"voice": {self.voice}, "format": {self.format}},
                messages=input_text,
                temperature=1.3,
                frequency_penalty=1.2,
                presence_penalty=1.2,
            )
        except Exception as exc:
            try:
                ChatHistory().log(
                    _role="tool",
                    _content="openai.chat.completions.create error",
                    _obj="model",
                    _data={"model": self.model, "error": f"{type(exc).__name__}: {exc}"},
                    _thread_name="model",
                    _name_tool="openai.chat.completions.create",
                )
            except Exception:
                pass
            raise

        try:
            ChatHistory().log(
                _role="tool",
                _content="openai.chat.completions.create response",
                _obj="model",
                _data={"model": self.model, "response_id": getattr(self.response, "id", None)},
                _thread_name="model",
                _name_tool="openai.chat.completions.create",
            )
        except Exception:
            pass
       
    def get_message(self):
            return self.response.choices[0].message.audio.transcript

    def get_audio(self):
            return self.response.choices[0].message.audio.data

# This class is used to generate images


# ─ ChatCom ‑ Wrapper, with in memory context cache implementation and vector store query ─
class ChatCom(ChatCompletion,ChatHistory):

    def __init__(self,
            *,
            _model: str,
            _input_text: str|list,
            tools: dict[list]|list[str]|None = None,
            tool_choice: str | None = None,
            _name: str|None=None,
            _url: str=None,
            _res: str=None
            ) -> None:

        self._model = _model
        self._input_text = _input_text
        self._tool_choice = tool_choice or "auto"
        tools=tools
        _name
        _url = _url
        _res = _res
        _object = "chat"        
        # instantiate chat history (context cache) -
        if not ChatHistory._history_:
            ChatHistory._history_ = ChatHistory._load()
        _chat = ChatHistory()

        # system message: instructions for the planner/router entry agent
        # Use the unified tool registry (shared with agenszie_factory) instead of a
        # custom, inconsistent schema. Keep the list small to reduce model confusion.
        try:
            from alde.agents_tools import UNIFIED_TOOLS  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "no known parent package" in msg or "attempted relative import" in msg:
                from alde.agents_tools import UNIFIED_TOOLS  # type: ignore
            else:
                raise
        try:
            from alde.agents_factory import get_agent_runtime_tools, get_agent_tools  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "no known parent package" in msg or "attempted relative import" in msg:
                from alde.agents_factory import get_agent_runtime_tools, get_agent_tools  # type: ignore
            else:
                raise
        try:
            from . import agents_registry as agents_registry  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "no known parent package" in msg or "attempted relative import" in msg:
                from alde import agents_registry as agents_registry  # type: ignore
            else:
                raise

        try:
            from .agents_config import get_agent_config, resolve_forced_route  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "no known parent package" in msg or "attempted relative import" in msg:
                from alde.agents_config import get_agent_config, resolve_forced_route  # type: ignore
            else:
                raise

        _requested_agent_label = "_xplaner_xrouter"
        _agent_cfg = get_agent_config(_requested_agent_label) or agents_registry.AGENTS_REGISTRY.get(_requested_agent_label) or agents_registry.AGENTS_REGISTRY.get("_xrouter_xplanner") or {
            "model": "gpt-4.1-mini-2025-04-14",
            "system": "You are xrouter_xplanner.",
            "tools": ["route_to_agent"],
        }
        self._agent_label = str(_agent_cfg.get("agent_label") or _requested_agent_label).strip() or _requested_agent_label
        self._agent_runtime = {
            "agent_label": _agent_cfg.get("agent_label") or self._agent_label,
            "canonical_name": _agent_cfg.get("canonical_name") or "xrouter_xplanner",
            "role": _agent_cfg.get("role") or "xrouter_xplanner",
            "skill_profile": _agent_cfg.get("skill_profile") or "",
            "instance_policy": _agent_cfg.get("instance_policy") or "ephemeral",
            "routing_policy": dict(_agent_cfg.get("routing_policy") or {}),
        }
        self._instance_policy = self._agent_runtime.get("instance_policy") or "ephemeral"
        if _verbose_terminal_logs_enabled():
            print(f"Using agent config: {_agent_cfg}")
        else:
            cfg_tools = _extract_tool_names(_agent_cfg.get("tools"))
            print(
                "Using agent config: "
                f"agent_label={self._agent_label} "
                f"model={_agent_cfg.get('model')} "
                f"tool_count={len(cfg_tools)}"
            )


        def _normalize_user_text(val: Any) -> str:
            """Ensure user text is a string (never list[str])."""
            if val is None:
                return ""
            if isinstance(val, str):
                return val
            if isinstance(val, (list, tuple)):
                return "\n".join(str(x) for x in val)
            if isinstance(val, dict):
                try:
                    return json.dumps(val, ensure_ascii=False)
                except Exception:
                    return str(val)
            return str(val)

        def _should_use_deterministic_action_result(result: Any) -> bool:
            """Only short-circuit when deterministic action execution produced a valid terminal result."""
            if result is None:
                return False
            if not isinstance(result, str):
                return True
            try:
                parsed_result = json.loads(result)
            except Exception:
                return True
            if not isinstance(parsed_result, dict):
                return True
            error_name = str(parsed_result.get("error") or "").strip().lower()
            if error_name in {
                "invalid_action_request",
                "invalid_action_request_json",
                "unknown_or_unsupported_action",
                "action_request_must_be_object",
            }:
                return False
            return True

        original_workflow_input = self._input_text
        self._deterministic_action_result: str | None = None
        self._deterministic_action_meta: dict[str, Any] | None = None
        try:
            try:
                from .agents_tools import execute_deterministic_action_request, resolve_configured_request_payload  # type: ignore
            except ImportError as e:
                msg = str(e)
                if "no known parent package" in msg or "attempted relative import" in msg:
                    from ALDE_Projekt.ALDE.alde.agents_tools import execute_deterministic_action_request, resolve_configured_request_payload  # type: ignore
                else:
                    raise
            deterministic_action_result = execute_deterministic_action_request(self._input_text)
            if _should_use_deterministic_action_result(deterministic_action_result):
                self._deterministic_action_result = str(deterministic_action_result)
            if self._deterministic_action_result is not None:
                parsed_request = None
                if isinstance(self._input_text, str):
                    try:
                        parsed_request = json.loads(self._input_text)
                    except Exception:
                        parsed_request = None
                elif isinstance(self._input_text, dict):
                    parsed_request = dict(self._input_text)
                if isinstance(parsed_request, dict):
                    resolved_request = resolve_configured_request_payload(parsed_request)
                    if not isinstance(resolved_request, dict):
                        resolved_request = parsed_request
                    self._deterministic_action_meta = {
                        "action": resolved_request.get("action"),
                        "correlation_id": resolved_request.get("correlation_id")
                        or ((resolved_request.get("payload") or {}).get("correlation_id") if isinstance(resolved_request.get("payload"), dict) else None),
                    }
            normalized_workflow_input = (
                self._input_text
                if self._deterministic_action_result is not None
                else resolve_configured_request_payload(self._input_text)
            )
        except Exception:
            normalized_workflow_input = self._input_text

        self._input_text = normalized_workflow_input
        msg_user_text: str = _normalize_user_text(self._input_text)
        if _verbose_terminal_logs_enabled():
            print(f"""USER INPUT:
              { msg_user_text}""")
        else:
            print(f"USER INPUT: chars={len(msg_user_text)} preview={_compact_preview(msg_user_text)}")

        # ------------------------------------------------------------------
        # Deterministic routing shortcuts from declarative config.
        # ------------------------------------------------------------------
        self._forced_route: dict[str, str] | None = None
        try:
            available_agents = set(getattr(agents_registry, "AGENTS_REGISTRY", {}).keys())
            if not available_agents:
                available_agents = {"_xrouter_xplanner", "_xplaner_xrouter", "_xworker"}
            self._forced_route = resolve_forced_route(
                self._agent_label,
                self._input_text,
                available_agents,
            )
        except Exception:
            # Never let routing shortcuts break normal chat execution.
            self._forced_route = None

        def _img_to_b64(_url: str | list | None) -> list[str]:
            img: list[str] = []
            if _url is None:
                return img
            paths: list[str]
            if isinstance(_url, str):
                paths = [_url]
            else:
                paths = [str(p) for p in _url]
                
            for path in paths:
                with open(path, "rb") as _f:
                    img.append(base64.b64encode(_f.read()).decode("utf-8"))
            return img
        
        _img_b64_list = _img_to_b64(_url)
        _msg_user_content_data_img: list[dict[str, Any]] = (
            [{"type": "text", "text": msg_user_text}]
            + [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": _res or "auto",
                    },
                }
                for b64 in _img_b64_list
            ]
        )
        # log messages to context cache for conversation liftime, -  
        # developer / system message / user message (with optional image data) -
        
        _chat._log(
            'user', 
            _msg_user_content_data_img if _url 
            else msg_user_text,
            _object, _name = 'xplaner_xrouter',
            _data={"agent_runtime": dict(self._agent_runtime)},
        )
        # initialize vector store -
        #_chat.init_vector_store()

        _user_content: Any = _msg_user_content_data_img if _img_b64_list else msg_user_text

        # Build model input from bounded history so the assistant can remember prior turns.
        # We prepend exactly one system message and then append recent history (excluding system)
        # to avoid duplicated system instructions.

        msg_sys_content_txt = str(_agent_cfg.get("system") or "")
        if _verbose_terminal_logs_enabled():
            print(f"""SYSTEM INSTRUCTION\\:
        {msg_sys_content_txt}""")
        else:
            print(f"SYSTEM INSTRUCTION: chars={len(msg_sys_content_txt)} preview={_compact_preview(msg_sys_content_txt)}")
        
        inserted = _chat._insert(tool=True, f_depth=0, f_role="system")
        _input: list[dict[str, Any]] = [{"role": "system", "content": msg_sys_content_txt}]
        if inserted:
            _input.extend(inserted)
        else:
            # Fallback: ensure the current user message is present.
            _input.append({"role": "user", "content": _user_content})

        print(f"INSERT: {len(inserted) if inserted else 0}")

        # Optional: include lightweight vector-store context (best-effort). TODO: this is a temporary experiment; we will likely replace it with a more robust retrieval-augmented generation (RAG) system in the future. The main goal here is to test whether including some vector-based context can improve model responses without causing errors due to malformed input.
        embeddings_context = ""
        try:
            vdb = getattr(ChatHistory, "vdb_history", None)
            if vdb is not None:
                embeddings_context = str(vdb.query(query=msg_user_text, k=2))
        except Exception as e:
            print(f"[WARNING] VectorStore query failed: {e}")

        if embeddings_context:
            _input.append({"role": "system", "content": f"Embeddings: {embeddings_context}"})
        try:
            _input_chars = sum(len(str(m.get("content", ""))) for m in _input if isinstance(m, dict))
        except Exception:
            _input_chars = -1
        print(f"INPUT: messages="'\n',len(_input),'\n'+f'approx_chars=','\n', _input_chars)
        # --------------------------------------------------- call to OpenAI's API
        def _response(_input: list[dict[str, Any]] = _input):
           
            """Return the full OpenAI response (choices/tool_calls live here)."""
            # Ensure we pass the object list expected by the API (not a single string).
            attempted_provider_fallback = False
            try:
                env_model = str(os.getenv("AI_IDE_CHAT_MODEL") or "").strip()
                explicit_model = str(getattr(self, "_model", "") or "").strip().lstrip("/")
                config_model = str((_agent_cfg.get("model") if isinstance(_agent_cfg, dict) else "") or "").strip().lstrip("/")
                model = explicit_model or env_model or config_model
                print(f"Using model: {model}")
            except Exception:
                model = str(os.getenv("AI_IDE_CHAT_MODEL") or "").strip().lstrip("/")

            if not str(model or "").strip():
                raise RuntimeError(
                    "Chat model is not configured. Set AI_IDE_CHAT_MODEL or provide an explicit model."
                )


            try:
                tools = get_agent_runtime_tools(self._agent_label)
                if _verbose_terminal_logs_enabled():
                    print(f"Using tools: {tools}")
                else:
                    tool_names = _extract_tool_names(tools)
                    print(f"Using tools: count={len(tool_names)} names={tool_names[:12]}")

            except Exception:
                try:
                    tools = get_agent_tools(_agent_cfg.get("tools") )
                except Exception:
                    tools = None

            model = _normalize_chat_model_name(model, provider=os.getenv("AI_IDE_MODEL_PROVIDER"))
           
            try:
                _chat.log(
                    _role="tool",
                    _content="openai.chat.completions.create request",
                    _obj="model",
                    _data={"model": model, "n_messages": len(_input), "has_tools": bool(tools)},
                    _thread_name="model",
                    _name_tool="openai.chat.completions.create",
                )
            except Exception:
                pass

            try:
                response = self._get_client().chat.completions.create(
                    model=model,
                    messages=_input,
                    tools=tools,
                    tool_choice="auto",
                )
            except Exception as exc:
                err_text = str(exc)
                base_url = str(os.getenv("OPENAI_BASE_URL") or "").strip().lower()
                is_ollama_endpoint = "127.0.0.1:11434" in base_url or "localhost:11434" in base_url
                should_retry_without_tools = bool(tools) and is_ollama_endpoint and "invalid request" in err_text.lower()
                if should_retry_without_tools:
                    try:
                        _chat.log(
                            _role="tool",
                            _content="openai.chat.completions.create retry_without_tools",
                            _obj="model",
                            _data={"model": model, "error": err_text},
                            _thread_name="model",
                            _name_tool="openai.chat.completions.create",
                        )
                    except Exception:
                        pass
                    response = self._get_client().chat.completions.create(
                        model=model,
                        messages=_input,
                    )
                    return response
                if not attempted_provider_fallback and any(token in err_text.lower() for token in ("no_access", "unknown_model")):
                    attempted_provider_fallback = True
                try:
                    _chat.log(
                        _role="tool",
                        _content="openai.chat.completions.create error",
                        _obj="model",
                        _data={"model": model, "error": f"{type(exc).__name__}: {exc}"},
                        _thread_name="model",
                        _name_tool="openai.chat.completions.create",
                    )
                except Exception:
                    pass
                raise

            try:
                _chat.log(
                    _role="tool",
                    _content="openai.chat.completions.create response",
                    _obj="model",
                    _data={"model": model, "response_id": getattr(response, "id", None)},
                    _thread_name="model",
                    _name_tool="openai.chat.completions.create",
                )
            except Exception:
                pass

            return response
                
        # If we already know we will route deterministically, skip the initialcontent: "
        # primary-model call (it would just add latency/cost).
        if self._deterministic_action_result is not None:
            self.assistant_msg_content = str(self._deterministic_action_result)
            self.assistant_msg = SimpleNamespace(content=self.assistant_msg_content, tool_calls=None)
        elif self._forced_route:
            self.assistant_msg_content = ""
            self.assistant_msg = SimpleNamespace(content="", tool_calls=None)
        else:
            try:
                _resp = _response(_input)
            except Exception as exc:
                active_model = str(getattr(self, "_model", "") or "").strip() or "(unset)"
                active_base_url = str(os.getenv("OPENAI_BASE_URL") or "(default OpenAI)").strip()
                err_text = (
                    "OpenAI chat call failed: "
                    f"{exc}. model={active_model} base_url={active_base_url}. "
                    "Check OPENAI_API_KEY, OPENAI_BASE_URL, and model availability."
                )
                self.assistant_msg_content = err_text
                self.assistant_msg = SimpleNamespace(content=err_text, tool_calls=None)
                _chat._log(
                    'assistant',
                    err_text,
                    _object,
                    _name='xplaner_xrouter',
                    _data={"agent_runtime": dict(self._agent_runtime)},
                )
                return
            self.assistant_msg = _resp.choices[0].message
            self.assistant_msg_content = (getattr(self.assistant_msg, 'content', '') or "")

            if not getattr(self.assistant_msg, 'tool_calls', None):
                pseudo_tool_call = _extract_pseudo_tool_call_payload(self.assistant_msg_content)
                available_tool_names = set(_extract_tool_names(tools)) if isinstance(tools, list) else set()

                if pseudo_tool_call and available_tool_names:
                    pseudo_name = str(pseudo_tool_call.get("name") or "").strip()
                    pseudo_arguments = pseudo_tool_call.get("arguments") if isinstance(pseudo_tool_call.get("arguments"), dict) else {}

                    if pseudo_name in available_tool_names:
                        synthetic_tool_call = SimpleNamespace(
                            id=f"pseudo_call_{int(time.time() * 1000)}",
                            type="function",
                            function=SimpleNamespace(
                                name=pseudo_name,
                                arguments=json.dumps(pseudo_arguments, ensure_ascii=False),
                            ),
                        )
                        self.assistant_msg = SimpleNamespace(content="", tool_calls=[synthetic_tool_call])
                        self.assistant_msg_content = ""
                    else:
                        try:
                            retry_model = _normalize_chat_model_name(getattr(self, "_model", "") or os.getenv("AI_IDE_CHAT_MODEL") or "", provider=os.getenv("AI_IDE_MODEL_PROVIDER"))
                            if not retry_model:
                                raise RuntimeError("Retry model is not configured.")
                            plain_text_retry = self._get_client().chat.completions.create(
                                model=retry_model,
                                messages=[
                                    *_input,
                                    {
                                        "role": "system",
                                        "content": (
                                            "Answer as plain text only. "
                                            "Do not output JSON tool/function call objects."
                                        ),
                                    },
                                ],
                            )
                            self.assistant_msg = plain_text_retry.choices[0].message
                            self.assistant_msg_content = (getattr(self.assistant_msg, 'content', '') or "")
                        except Exception:
                            pass

        #print(f"USER INPUT:\n\n{_msg_user_text}\n\nMODEL RESPONSE\n\n{self.assistant_msg_content}")
        # -------------------------------------- log response to context cache -
        assistant_log_content = self.assistant_msg_content
        if not assistant_log_content:
            if self._forced_route:
                assistant_log_content = "[forced route prepared]"
            elif getattr(self.assistant_msg, 'tool_calls', None):
                assistant_log_content = "[tool calls executed]"

        _chat._log('assistant',
            assistant_log_content,
            _object,
            _tool_calls=getattr(self.assistant_msg, 'tool_calls'),
            _name = 'xplaner_xrouter',
            _data={
                "agent_runtime": dict(self._agent_runtime),
                "deterministic_action": dict(self._deterministic_action_meta or {}),
            },
        )
    # -------------------------------- API (retrieve the model's response) -
        # -------------------------------- Tool-call handling via agents_factory -

    def get_response(self) -> str:
            import sys
            """Return the assistant reply as plain text.

            This method is used by both the GUI and headless callers.
            Always return a string to avoid UI crashes like: 'str' has no attribute 'content'
            or 'ChatCompletionMessage' is not JSON serializable.
            """
            if getattr(self, "_deterministic_action_result", None) is not None:
                return str(getattr(self, "_deterministic_action_result"))

            # Shortcut: user explicitly selected an agent via @prefix, or a
            # cover-letter request was detected with required info.
            if getattr(self, "_forced_route", None):
                try:
                    try:
                        from . import agents_factory as _agents_factory  # type: ignore
                    except ImportError as e:
                        msg = str(e)
                        if "no known parent package" in msg or "attempted relative import" in msg:
                            from alde import agents_factory as _agents_factory  # type: ignore
                        else:
                            raise

                    return _agents_factory.execute_forced_route(
                        dict(getattr(self, "_forced_route") or {}),
                        ChatCom=ChatCom,
                        origin_agent_label=getattr(self, "_agent_label", "_xplaner_xrouter"),
                    )
                except Exception as e:
                    return f"Routing failed: {e}"

            tool_calls = getattr(self.assistant_msg, 'tool_calls', None)
            if tool_calls:
                # Note: tool-call responses often have no direct text content.
                if _verbose_terminal_logs_enabled():
                    print(f'Tool calls: {tool_calls}')
                else:
                    tool_call_names = _extract_tool_call_names(tool_calls)
                    print(f"Tool calls: count={len(tool_call_names)} names={tool_call_names[:12]}")
                try:
                    from .  import agents_factory  as _agents_factory  # type: ignore
                except ImportError as e:
                    msg = str(e)
                    if "no known parent package" in msg or "attempted relative import" in msg:
                        from alde import agents_factory as _agents_factory  # type: ignore
                    else:
                        raise

                final_result = _agents_factory._handle_tool_calls(
                    self.assistant_msg,
                    ChatCom=ChatCom,
                    depth=0,
                    agent_label=getattr(self, "_agent_label", "_xplaner_xrouter"),
                )

                if _verbose_terminal_logs_enabled():
                    print(f'FINAL_RESULT: {final_result}')
                else:
                    print(f"FINAL_RESULT: preview={_compact_preview(final_result, limit=280)}")
                if final_result is None:
                    return ""
                if isinstance(final_result, str):
                    return final_result
                try:
                    return json.dumps(final_result, ensure_ascii=False)
                except Exception:
                    return str(final_result)

            return (self.assistant_msg_content or "")

# This class is used to generate images
class ImageCreate(ChatCompletion,ChatHistory):
    _object = "img"
    _previous_response_id: str = ''

    def __init__(self,
        _model:str|None="",
        _input_text:str=""
        ):  # ((:
        _input_text = _input_text or ""
        image_base64 = ""
        super().__init__()

        self._log( 
            _role="user", 
            _content=_input_text , 
            _obj=self._object,
            _tool_call_id = self._previous_response_id,
            _name_tool="image_generation"
        )
        if not self._previous_response_id:
            try:
                self.log(
                    _role="tool",
                    _content="openai.responses.create request",
                    _obj="model",
                    _data={"model": _model, "previous_response_id": None},
                    _thread_name="model",
                    _name_tool="openai.responses.create",
                )
            except Exception:
                pass
            try:
                response = self._get_client().responses.create(
                    model=_model,
                    input=_input_text,
                    tools=[{
                        "type": "image_generation",
                        "background": "transparent",
                        "action": "auto",
                        "quality": "medium",
                        "size": "1024x1024",
                        "moderation": "low",
                    }],
                )
            except Exception as exc:
                try:
                    self.log(
                        _role="tool",
                        _content="openai.responses.create error",
                        _obj="model",
                        _data={"model": _model, "error": f"{type(exc).__name__}: {exc}"},
                        _thread_name="model",
                        _name_tool="openai.responses.create",
                    )
                except Exception:
                    pass
                raise
            self._previous_response_id = response.id
        else:
            try:
                self.log(
                    _role="tool",
                    _content="openai.images.create request",
                    _obj="model",
                    _data={"model": _model, "previous_response_id": self._previous_response_id},
                    _thread_name="model",
                    _name_tool="openai.images.create",
                )
            except Exception:
                pass
            try:
                response = self._get_client().responses.create(
                    previous_response_id=self._previous_response_id,
                    model=_model,
                     input=[
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": _input_text},
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{self._insert(f_depth=0, f_role='user')}",
                },
               
            ],
        }
    ],
            tools=[{
                        "type": "image_generation",
                        "background": "transparent",
                        "action": "edit",
                        "quality": "medium",
                        "size": "1024x1024",
                        "moderation": "low",
                    }],
                )
                self._previous_response_id = response.id

            except Exception as exc:
                try:
                    self.log(
                        _role="tool",
                        _content="openai.responses.create error",
                        _obj="model",
                        _data={"model": _model, "error": f"{type(exc).__name__}: {exc}"},
                        _thread_name="model",
                        _name_tool="openai.responses.create",
                    )
                except Exception:
                    pass
                raise

        try:
            self.log(
                _role="tool",
                _content="openai.responses.create response",
                _obj="model",
                _data={"model": _model, "response_id": self._previous_response_id},
                _thread_name="model",
                _name_tool="openai.responses.create",
            )
        except Exception:
            pass
        image_generation_calls = [
            outp
            for outp in response.output
            if outp.type == "image_generation_call"
            ]
        image_data = [outp.result for outp in image_generation_calls]
        if image_data:
            image_base64: bytes= image_data[0]
        self._log(
            _role="assistant",
            _content=(image_base64 if image_data else "Image creation failed."
            ),
            _obj=self._object,
            _tool_call_id=self._previous_response_id,
        )
        self.image_base64 = image_base64

    def get_img(self) -> object:
            if not self.image_base64 :
                raise ValueError(
            "No image data returned from image generation"
        )
            return self.image_base64 
        

class ChatComE(ChatCompletion,ChatHistory):     
        if not ChatHistory._history_:
            ChatHistory._history_ 

        def __init__(self, 
            _model :str,
            # path:str|list|None = None, file:str|list|None = None,
            _messages:list,
            tools:list[dict],
            tool_choice:str
            ):
            self.model:str = _normalize_chat_model_name(_model or os.getenv("AI_IDE_CHAT_MODEL") or "", provider=os.getenv("AI_IDE_MODEL_PROVIDER"))
            if not self.model:
                raise RuntimeError(
                    "Chat model is not configured. Set AI_IDE_CHAT_MODEL or pass _model explicitly."
                )
            self._messages:list = _messages
            self.tools:list[dict] = tools
            self.tool_choice:str = tool_choice
            super().__init__()
            api_key = self._read_api_key()
            #print(tools)
            self.instruction = """
                  You are an expert DevOps assistant. 
                  Du generierst sicheren, getesteten und ausfürlich dokumentierten Code für Python GUI's mit Qt6-PySide6. 
                  Du bist verantwortlich für schreiben, debugging und refactoring 
                  jede Antwort muss: 
                  (1) kompilierten/ready to run Code oder 
                  (2) dropin patches, ein oder mehrteilig, liefern. 
                  (3) betroffener, fehlerhafter Code muss neu geschrieben werden. 
                  (4) eine Kurz­erklärung liefern 
              """
            api_key
            #self.path = path   
            #self.file = file 
            self.editor = "editor"
            self.model 
            self.client = self._get_client()
            """self.messages_chat.append([
                       {
                 "role":"system", "content":self.system_message
                 },
                 {
                 "role":"developer", "content":self.instruction
                 },
                 {
                 "role":"user", "content":self.input_text
                 },
                     {
                     "role":"assistant", "content":self.response
             },
             ])"""
       
        def _response(self):
                tool_choice = getattr(self, "tool_choice", None) or "auto"
                if _verbose_terminal_logs_enabled():
                    print(
                        "FOLLOWUP CHAT RESPONSE MODEL:",
                        {
                            "model": self.model,
                            "provider": os.getenv("AI_IDE_MODEL_PROVIDER"),
                            "base_url": os.getenv("OPENAI_BASE_URL"),
                            "client_base_url": str(getattr(self.client, "base_url", "")),
                        },
                    )
                try:
                    self.log(
                        _role="tool",
                        _content="openai.chat.completions.create request",
                        _obj="model",
                        _data={"model": self.model, "n_messages": len(self._messages), "has_tools": bool(self.tools)},
                        _thread_name="model",
                        _name_tool="openai.chat.completions.create",
                    )
                except Exception:
                    pass
                try:
                    self.response = self.client.chat.completions.create(
                        model=self.model,
                        messages=self._messages,
                        tools=self.tools,
                        tool_choice=tool_choice,
                    )
                except Exception as exc:
                    err_text = str(exc)
                    base_url = str(os.getenv("OPENAI_BASE_URL") or "").strip().lower()
                    is_ollama_endpoint = "127.0.0.1:11434" in base_url or "localhost:11434" in base_url
                    should_retry_without_tools = bool(self.tools) and is_ollama_endpoint and "invalid request" in err_text.lower()
                    if should_retry_without_tools:
                        self.response = self.client.chat.completions.create(
                            model=self.model,
                            messages=self._messages,
                        )
                        return self.response
                    if any(token in err_text.lower() for token in ("no_access", "unknown_model")):
                        pass
                    try:
                        self.log(
                            _role="tool",
                            _content="openai.chat.completions.create error",
                            _obj="model",
                            _data={"model": self.model, "error": f"{type(exc).__name__}: {exc}"},
                            _thread_name="model",
                            _name_tool="openai.chat.completions.create",
                        )
                    except Exception:
                        pass
                    raise
                try:
                    self.log(
                        _role="tool",
                        _content="openai.chat.completions.create response",
                        _obj="model",
                        _data={"model": self.model, "response_id": getattr(self.response, "id", None)},
                        _thread_name="model",
                        _name_tool="openai.chat.completions.create",
                    )
                except Exception:
                    pass
                # Return the full response object so caller can check tool_calls
                return self.response
        def _editor(self,e):
            e=e
            return subprocess.run(({self.editor}), 
            shell = False
                )
        def _readit(self):
            path = self.path
            for path,file in self.path,self.file:
                with open(f"{path}{file}", "r") as f:
                    return f.read()
        def _writeit(self,w):
                w=w
                with open(f"{self.path}{self.file}", "a") as file:
                    return file.write(f"{w}")        

#if __name__ == "__main__":
##chat_comp = ChatComp("gpt-4o", api_key="sk-...", input_text="What is the weather like today?", filename="editor")
#print(chat_comp.response())
'''
# CALL THE IMAGE DESCRIPTION CLASS
# Example: Replace with your own image path
# image_description = ImageDescription(api_key,"gpt-4o","path/to/your/image.png","Describe this image")
# image_description.get_descript().choices[0].message.content'''


# CALL THE IMAGE CREATE CLASS
'''chat_dialogue = ChatDialogue(api_key,"gpt-4o","What is the weather like today?")
   print(chat_dialogue.main())'''


# CALL THE IMAGE CREATE CLASS
'''image_create = ImageCreate(api_key,"dall-e-3","Create an image of a friendly alien.")
   image_create.main()
'''

# CALL THE EDITOR CLASS
'''filename="vs_code_1.txt"
   filename_1="vs_code_2.txt"
   editor="gnome-text-editor"
   input_text = ChatComEditor(filename,input_text="").get_readedit()
   print(input_text)

   w=ChatComEditor(input_text=input_text).get_response()           
   print(w)
   ChatComEditor(filename_1).get_writeedit(w=w)
'''

# CALL IN SHELL WITH PIPING
# Example usage

'''code-insiders Vs_Code_Projects/Debugger/debug_file.txt && ssh -v -T -D 42539 -o ConnectTimeout=15 gitlab.com &&>> 
Vs_Code_Projects/Debugger/debug_file.txt & python3 Vs_Code_Projects/Debugger/ChatClassCompletion.py'''


"""
if __name__ == "__main__":  

    # Example: Use relative path from this file's location
    orpath = str(Path(__file__).resolve().parent)
    api_key = ChatClassCompletion._read_api_key()
    path = os.path.join(orpath)


    file = "debug_AIIDE" 


    editor= "gnome-text-editor"
    input_text = ""    

    chatcom = ChatComEditor(api_key, path, file, _input_text="") 
    _input_text = chatcom._readit()

    chatcom_t = ChatComEditor(api_key,path,file,_input_text=_input_text)
    w = chatcom._response()
    chatcom = ChatComEditor(api_key,path,file,_input_text=None)
    chatcom._writeit('\n\n\n'f" '''{w}''' ")"""


     


