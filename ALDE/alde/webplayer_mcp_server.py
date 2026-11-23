"""Standalone MCP server for local WebPlayer control.

This module is intentionally independent from the main ALDE MCP stack so it
can run with minimal dependencies on remote hosts.
"""

from __future__ import annotations

import argparse
import base64
import collections
import fnmatch
import glob
import gzip
import ipaddress
import json
import math
import os
import re
import secrets
import select
import shlex
import shutil
import socketserver
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

WEBPLAYER_MCP_VERSION = "1.1.0"


def _load_seek_spec(*, position_seconds: Any, delta_seconds: Any) -> tuple[str, str]:
    if position_seconds is None and delta_seconds is None:
        raise ValueError("missing_seek_position")

    raw_value = position_seconds if position_seconds is not None else delta_seconds
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_seek_position") from exc
    if not math.isfinite(value):
        raise ValueError("invalid_seek_position")
    if position_seconds is not None:
        if value < 0:
            raise ValueError("invalid_seek_position")
        return f"{value:g}", "absolute"
    if value == 0:
        raise ValueError("invalid_seek_position")
    return f"{abs(value):g}{'+' if value > 0 else '-'}", "relative"


try:
    from .mcp_plugins.webplayer_library_snapshot_plugin import WPlibSnaPlugin
except Exception:
    from alde.mcp_plugins.webplayer_library_snapshot_plugin import WPlibSnaPlugin  # type: ignore

try:
    from .tidal_oauth import TidalAuthorizationRequest, TidalOAuthError, TidalOAuthTokenService  # type: ignore
except Exception:
    from alde.tidal_oauth import TidalAuthorizationRequest, TidalOAuthError, TidalOAuthTokenService  # type: ignore

try:
    from .webplayer_playlist import PlaybackPlaylistRepository
except Exception:
    from alde.webplayer_playlist import PlaybackPlaylistRepository  # type: ignore


@dataclass
class CommandResult:
    object_name: str
    return_code: int
    stdout: str
    stderr: str
    command: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "object_name": self.object_name,
            "ok": self.return_code == 0,
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "command": self.command,
        }


@dataclass
class HttpRequestObject:
    object_name: str
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout_s: int


def load_tonart_value(raw_text: str) -> str:
    match = re.search(r"\b([A-G](?:#|b)?\s*(?:maj(?:or)?|min(?:or)?|m|dur|moll))\b", raw_text, flags=re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", str(match.group(1) or "")).strip()

    json_key_match = re.search(
        r'"(?:musicalKey|initialKey|musical_key|key)"\s*:\s*"([A-G](?:_SHARP|_FLAT)?_(?:MAJOR|MINOR)|[A-G](?:#|b)?(?:m|maj|min|major|minor)?)"',
        raw_text,
        flags=re.IGNORECASE,
    )
    if not json_key_match:
        return ""

    normalized = str(json_key_match.group(1) or "").strip().replace("_", " ")
    normalized = normalized.replace("SHARP", "#").replace("FLAT", "b")
    return re.sub(r"\s+", " ", normalized).strip()


def load_audio_quality_metrics(raw_text: str) -> tuple[int | None, float | None]:
    normalized_text = str(raw_text or "").replace("\n", " ").replace("\r", " ")
    bit_depth_match = re.search(
        r'"(?:bitDepth|bit_depth|bitdepth)"\s*:\s*"?(\d{1,2})"?',
        normalized_text,
        flags=re.IGNORECASE,
    )
    if not bit_depth_match:
        bit_depth_match = re.search(r"\b(\d{1,2})\s*[- ]?bit\b", normalized_text, flags=re.IGNORECASE)
    if not bit_depth_match:
        bit_depth_match = re.search(r"\bbit\s*depth\s*[:=]\s*(\d{1,2})\b", normalized_text, flags=re.IGNORECASE)

    sample_rate_match = re.search(
        r'"(?:sampleRateKHz|sample_rate_khz|sampleRateHz|sample_rate_hz|sampleRate|sample_rate|samplerate|samplingRate)"\s*:\s*"?(\d+(?:\.\d+)?)"?',
        normalized_text,
        flags=re.IGNORECASE,
    )
    sample_rate_khz = 0.0
    if sample_rate_match:
        try:
            sample_rate_khz = float(str(sample_rate_match.group(1) or "0"))
        except (TypeError, ValueError):
            sample_rate_khz = 0.0
    if sample_rate_khz <= 0:
        sample_rate_match = re.search(r"\b(\d+(?:\.\d+)?)\s*kHz\b", normalized_text, flags=re.IGNORECASE)
        if sample_rate_match:
            try:
                sample_rate_khz = float(str(sample_rate_match.group(1) or "0"))
            except (TypeError, ValueError):
                sample_rate_khz = 0.0
    if sample_rate_khz <= 0:
        sample_rate_match = re.search(r"\b(\d{4,6}(?:\.\d+)?)\s*Hz\b", normalized_text, flags=re.IGNORECASE)
        if sample_rate_match:
            try:
                sample_rate_khz = float(str(sample_rate_match.group(1) or "0")) / 1000.0
            except (TypeError, ValueError):
                sample_rate_khz = 0.0
    if sample_rate_khz >= 1000:
        sample_rate_khz /= 1000.0

    bit_depth = int(bit_depth_match.group(1)) if bit_depth_match else None
    return bit_depth, sample_rate_khz if sample_rate_khz > 0 else None


def load_quality_resolution(raw_text: str) -> str:
    bit_depth, sample_rate_khz = load_audio_quality_metrics(raw_text)

    resolution_parts = []
    if bit_depth is not None:
        resolution_parts.append(f"{bit_depth:g}-bit")
    if sample_rate_khz is not None:
        resolution_parts.append(f"{sample_rate_khz:g} kHz")
    return " ".join(resolution_parts).strip()


def load_audio_mode_from_text(raw_text: str) -> str:
    normalized_text = str(raw_text or "").strip()
    if not normalized_text:
        return ""
    haystack = normalized_text.casefold()
    if "atmos" in haystack or "dolby" in haystack or "ec-3" in haystack or "eac3" in haystack:
        return "DOLBY_ATMOS"
    if "360" in haystack or "sony" in haystack or "mha1" in haystack:
        return "SONY_360RA"
    if "stereo" in haystack:
        return "STEREO"
    if "lossless" in haystack or "flac" in haystack or "alac" in haystack:
        return "LOSSLESS"
    if "hi_res" in haystack or "hi-res" in haystack or "master" in haystack:
        return "HI_RES"
    if "aac" in haystack or "mp4a" in haystack:
        return "AAC"
    return ""


def load_tidal_dsp_metadata(attributes: dict[str, Any]) -> dict[str, Any]:
    normalization_data = {
        "track": attributes.get("trackAudioNormalizationData") or attributes.get("track_audio_normalization_data"),
        "album": attributes.get("albumAudioNormalizationData") or attributes.get("album_audio_normalization_data"),
    }
    metadata: dict[str, Any] = {}
    display_parts: list[str] = []
    for scope, raw_data in normalization_data.items():
        if not isinstance(raw_data, dict):
            continue
        values: dict[str, int | float] = {}
        for output_name, input_names in (
            ("replay_gain_db", ("replayGain", "replay_gain", "replayGainDb", "replay_gain_db")),
            ("peak_amplitude", ("peakAmplitude", "peak_amplitude")),
        ):
            raw_value = next((raw_data.get(key) for key in input_names if raw_data.get(key) is not None), None)
            if raw_value is None or isinstance(raw_value, bool):
                continue
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError):
                continue
            normalized_value: int | float = int(numeric_value) if numeric_value.is_integer() else numeric_value
            values[output_name] = normalized_value
            metadata[f"{scope}_{output_name}"] = normalized_value
        if "replay_gain_db" in values:
            display_parts.append(f"{scope.title()} RG {values['replay_gain_db']:g} dB")
        if "peak_amplitude" in values:
            display_parts.append(f"{scope.title()} peak {values['peak_amplitude']:g}")
    if display_parts:
        metadata["dsp"] = "TIDAL API | " + " | ".join(display_parts)
    return metadata


def load_selected_player_name(
    *,
    players: list[str],
    player_selector: str,
    api_prefixes: tuple[str, ...] = ("cvlc", "vlc", "mpv", "mpd"),
) -> str:
    normalized_players = [str(player_name).strip() for player_name in players if str(player_name).strip()]
    if not normalized_players:
        return ""

    selector_prefix = str(player_selector or "tidal-api").strip().casefold()
    if selector_prefix in {"", "api", "tidal-api"}:
        candidate_prefixes = tuple(prefix.strip().casefold() for prefix in api_prefixes if str(prefix).strip())
    else:
        candidate_prefixes = (selector_prefix,)

    for candidate_prefix in candidate_prefixes:
        for player_name in normalized_players:
            if player_name.casefold().startswith(candidate_prefix):
                return player_name
    return ""


class McpUiSessionRegistry:
    def __init__(self, max_entries: int = 256) -> None:
        self._max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._ui_enabled_by_session: dict[str, bool] = {}

    def store_ui_capability(self, session_id: str | None, *, ui_enabled: bool) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return

        with self._lock:
            if normalized_session_id not in self._ui_enabled_by_session and len(self._ui_enabled_by_session) >= self._max_entries:
                oldest_session_id = next(iter(self._ui_enabled_by_session))
                self._ui_enabled_by_session.pop(oldest_session_id, None)
            self._ui_enabled_by_session[normalized_session_id] = bool(ui_enabled)

    def is_ui_enabled(self, session_id: str | None) -> bool:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return False

        with self._lock:
            return bool(self._ui_enabled_by_session.get(normalized_session_id))


class LocalCommandService:
    def __init__(self, command_timeout_s: int = 20) -> None:
        self.command_timeout_s = int(command_timeout_s)

    def run_object(self, *, object_name: str, command: str, command_timeout_s: int | None = None) -> CommandResult:
        shell_command = ["bash", "-lc", str(command)]
        timeout_s = int(command_timeout_s) if command_timeout_s is not None else self.command_timeout_s
        try:
            completed = subprocess.run(
                shell_command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                object_name=object_name,
                return_code=124,
                stdout=str(exc.stdout or "").strip(),
                stderr=f"timeout_after_seconds={timeout_s}",
                command=" ".join(shlex.quote(part) for part in shell_command),
            )

        return CommandResult(
            object_name=object_name,
            return_code=int(completed.returncode),
            stdout=(completed.stdout or "").strip(),
            stderr=(completed.stderr or "").strip(),
            command=" ".join(shlex.quote(part) for part in shell_command),
        )


class TidalApiService:
    _SUPPORTED_ACTIONS = {
        "tidal_api_request",
        "tidal_api_add_track_to_playlist",
        "tidal_api_add_track_to_collection",
        "tidal_api_search",
        "tidal_api_track",
        "tidal_api_track_manifest",
        "tidal_api_widevine",
    }
    _ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
    _INFORMATIONAL_METHODS = {"GET", "HEAD", "OPTIONS"}
    _ALLOWED_SCHEMES = {"http", "https"}
    _SENSITIVE_HEADERS = {"authorization", "cookie", "x-tidal-token"}
    _USER_WRITE_ACTIONS = {"tidal_api_add_track_to_playlist", "tidal_api_add_track_to_collection"}

    def __init__(self, *, max_response_bytes: int = 2_000_000, oauth_token_service: TidalOAuthTokenService | None = None) -> None:
        self.max_response_bytes = int(max_response_bytes)
        self.oauth_token_service = oauth_token_service or TidalOAuthTokenService()

    def load_supported_actions(self) -> set[str]:
        return set(self._SUPPORTED_ACTIONS)

    def dispatch_object(self, *, object_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized_object_name = str(object_name or "").strip().lower()
        if normalized_object_name not in self._SUPPORTED_ACTIONS:
            return {
                "ok": False,
                "error": "unsupported_tool",
                "object_name": normalized_object_name,
                "supported_tools": sorted(self._SUPPORTED_ACTIONS),
            }

        if normalized_object_name in self._USER_WRITE_ACTIONS and not self._load_user_authorization(arguments):
            return {
                "ok": False,
                "error": "user_auth_required",
                "object_name": normalized_object_name,
                "required_scopes": ["collection.write", "w_usr"],
                "auth_tool": "webplayer_usrauth",
            }

        try:
            request_object = self.load_object_request(object_name=normalized_object_name, arguments=arguments)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "object_name": normalized_object_name,
            }

        return self.process_object(request_object=request_object)

    def _load_user_authorization(self, arguments: dict[str, Any]) -> bool:
        explicit_headers = self._load_headers_object(arguments.get("headers"))
        if any(str(key).casefold() == "authorization" and str(value).strip() for key, value in explicit_headers.items()):
            return True
        try:
            return bool(self.oauth_token_service.load_authorization_header())
        except Exception:
            return False

    def load_object_request(self, *, object_name: str, arguments: dict[str, Any]) -> HttpRequestObject:
        if object_name == "tidal_api_request":
            return self._load_generic_request_object(arguments=arguments)
        if object_name == "tidal_api_add_track_to_playlist":
            return self._load_add_track_to_playlist_request_object(arguments=arguments)
        if object_name == "tidal_api_add_track_to_collection":
            return self._load_add_track_to_collection_request_object(arguments=arguments)
        if object_name == "tidal_api_search":
            return self._load_search_request_object(arguments=arguments)
        if object_name == "tidal_api_track":
            return self._load_track_request_object(arguments=arguments)
        if object_name == "tidal_api_track_manifest":
            return self._load_track_manifest_request_object(arguments=arguments)
        if object_name == "tidal_api_widevine":
            return self._load_widevine_request_object(arguments=arguments)
        raise ValueError(f"unsupported_tool:{object_name}")

    def _load_add_track_to_playlist_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        playlist_id = str(arguments.get("playlist_id") or "").strip()
        track_id = str(arguments.get("track_id") or "").strip()
        if not playlist_id:
            raise ValueError("missing_playlist_id")
        if not track_id:
            raise ValueError("missing_track_id")

        request_headers = self._load_api_headers(arguments.get("headers"))
        request_headers.setdefault("Accept", "application/vnd.api+json")
        request_headers.setdefault("Content-Type", "application/vnd.api+json")
        request_payload = {"data": [{"type": "tracks", "id": track_id}]}
        return HttpRequestObject(
            object_name="tidal_api_add_track_to_playlist",
            method="POST",
            url=f"https://openapi.tidal.com/v2/playlists/{quote(playlist_id, safe='')}/relationships/items",
            headers=request_headers,
            body=self._load_request_body(arguments={"body_json": request_payload}, request_headers=request_headers),
            timeout_s=self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20),
        )

    def _load_add_track_to_collection_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        track_id = self._load_positive_track_id(arguments=arguments)

        request_headers = self._load_api_headers(arguments.get("headers"))
        request_headers.setdefault("Accept", "application/vnd.api+json")
        request_headers.setdefault("Content-Type", "application/vnd.api+json")
        request_payload = {"data": [{"type": "tracks", "id": track_id}]}
        return HttpRequestObject(
            object_name="tidal_api_add_track_to_collection",
            method="POST",
            url="https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items",
            headers=request_headers,
            body=self._load_request_body(arguments={"body_json": request_payload}, request_headers=request_headers),
            timeout_s=self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20),
        )

    @staticmethod
    def _load_positive_track_id(*, arguments: dict[str, Any]) -> str:
        raw_track_id = str(arguments.get("track_id") or "").strip()
        if not raw_track_id:
            raise ValueError("missing_track_id")
        try:
            track_id = int(raw_track_id)
        except ValueError as exc:
            raise ValueError("invalid_track_id") from exc
        if track_id <= 0:
            raise ValueError("invalid_track_id")
        return str(track_id)

    def process_object(self, *, request_object: HttpRequestObject) -> dict[str, Any]:
        request_headers = dict(request_object.headers)
        request = Request(
            request_object.url,
            data=request_object.body,
            headers=request_headers,
            method=request_object.method,
        )

        status_code = 0
        response_headers: dict[str, str] = {}
        response_body_bytes = b""
        response_was_truncated = False
        request_error = ""

        try:
            with urlopen(request, timeout=request_object.timeout_s) as response:
                status_code = int(getattr(response, "status", 200))
                response_headers = dict(response.headers.items())
                response_body_bytes = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            status_code = int(exc.code or 500)
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            response_body_bytes = exc.read(self.max_response_bytes + 1) if exc.fp else b""
        except URLError as exc:
            request_error = f"url_error:{exc.reason}"
        except Exception as exc:
            request_error = f"request_failed:{exc}"

        if request_error:
            return {
                "object_name": request_object.object_name,
                "ok": False,
                "error": request_error,
                "status_code": 0,
                "request_method": request_object.method,
                "request_url": request_object.url,
                "request_headers": self._load_sanitized_headers(request_object.headers),
            }

        if len(response_body_bytes) > self.max_response_bytes:
            response_was_truncated = True
            response_body_bytes = response_body_bytes[: self.max_response_bytes]

        response_content_type = str(response_headers.get("Content-Type") or "")
        response_body_text, response_body_base64 = self._load_response_body_payload(
            response_body_bytes=response_body_bytes,
            content_type=response_content_type,
        )

        payload: dict[str, Any] = {
            "object_name": request_object.object_name,
            "ok": 200 <= status_code <= 299,
            "status_code": status_code,
            "request_method": request_object.method,
            "request_url": request_object.url,
            "request_headers": self._load_sanitized_headers(request_object.headers),
            "response_headers": response_headers,
            "response_body": response_body_text,
            "response_size_bytes": len(response_body_bytes),
            "response_truncated": response_was_truncated,
        }
        if request_object.object_name == "tidal_api_add_track_to_collection" and payload["ok"]:
            payload["liked"] = True
            payload["liked_state"] = "liked"
        if response_body_base64 is not None:
            payload["response_body_base64"] = response_body_base64
        return payload

    def _load_generic_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        raw_url = str(arguments.get("url") or "").strip()
        if not raw_url:
            raise ValueError("missing_url")

        request_method = str(arguments.get("method") or "GET").strip().upper()
        if request_method not in self._ALLOWED_METHODS:
            raise ValueError("invalid_method")
        if request_method not in self._INFORMATIONAL_METHODS:
            raise ValueError("informational_api_read_only")

        validated_url = self._load_validated_tidal_url(raw_url)
        query_url = self._load_url_with_query(
            base_url=validated_url,
            query_payload=arguments.get("query") if "query" in arguments else None,
        )
        request_headers = self._load_api_headers(arguments.get("headers"))
        request_body = self._load_request_body(arguments=arguments, request_headers=request_headers)
        timeout_s = self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20)

        return HttpRequestObject(
            object_name="tidal_api_request",
            method=request_method,
            url=query_url,
            headers=request_headers,
            body=request_body,
            timeout_s=timeout_s,
        )

    def _load_search_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("missing_query")

        country_code = str(arguments.get("country_code") or "DE").strip().upper() or "DE"
        include_value = arguments.get("include") or "tracks,tracks.artists,tracks.albums,tracks.albums.coverArt,topHits"
        if isinstance(include_value, str):
            include_values = [item.strip() for item in include_value.split(",") if item.strip()]
        elif isinstance(include_value, (list, tuple)):
            include_values = [str(item).strip() for item in include_value if str(item).strip()]
        else:
            include_values = ["tracks", "tracks.artists", "tracks.albums", "tracks.albums.coverArt", "topHits"]

        request_headers = self._load_api_headers(arguments.get("headers"))
        request_headers.setdefault("Accept", "application/vnd.api+json")
        timeout_s = self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20)
        query_payload: dict[str, Any] = {
            "filter[query]": query,
            "countryCode": country_code,
            "include": include_values,
        }
        for argument_name, query_name in (
            ("explicit_filter", "explicitFilter"),
            ("device_type", "deviceType"),
            ("system_type", "systemType"),
            ("client_version", "clientVersion"),
        ):
            argument_value = str(arguments.get(argument_name) or "").strip()
            if argument_value:
                query_payload[query_name] = argument_value

        base_url = "https://openapi.tidal.com/v2/searchResults"
        request_url = self._load_url_with_query(base_url=base_url, query_payload=query_payload)
        return HttpRequestObject(
            object_name="tidal_api_search",
            method="GET",
            url=request_url,
            headers=request_headers,
            body=None,
            timeout_s=timeout_s,
        )

    def _load_track_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        track_id = str(arguments.get("track_id") or "").strip()
        if not track_id:
            raise ValueError("missing_track_id")

        country_code = str(arguments.get("country_code") or "DE").strip().upper() or "DE"
        include = str(arguments.get("include") or "albums,artists,albums.coverArt").strip() or "albums,artists,albums.coverArt"
        request_headers = self._load_api_headers(arguments.get("headers"))
        timeout_s = self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20)

        base_url = f"https://openapi.tidal.com/v2/tracks/{quote(track_id, safe='')}"
        request_url = self._load_url_with_query(
            base_url=base_url,
            query_payload={
                "countryCode": country_code,
                "include": include,
            },
        )

        return HttpRequestObject(
            object_name="tidal_api_track",
            method="GET",
            url=request_url,
            headers=request_headers,
            body=None,
            timeout_s=timeout_s,
        )

    def _load_track_manifest_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        track_id = str(arguments.get("track_id") or "").strip()
        if not track_id:
            raise ValueError("missing_track_id")

        adaptive = self._load_boolean_argument(arguments.get("adaptive"), default_value=True)
        raw_formats = arguments.get("formats") or "AACLC"
        if isinstance(raw_formats, str):
            formats = [item.strip() for item in raw_formats.split(",") if item.strip()]
        elif isinstance(raw_formats, (list, tuple)):
            formats = [str(item).strip() for item in raw_formats if str(item).strip()]
        else:
            formats = ["AACLC"]
        manifest_type = str(arguments.get("manifest_type") or "HLS").strip() or "HLS"
        uri_scheme = str(arguments.get("uri_scheme") or "HTTPS").strip() or "HTTPS"
        usage = str(arguments.get("usage") or "PLAYBACK").strip() or "PLAYBACK"
        request_headers = self._load_api_headers(arguments.get("headers"))
        request_headers.setdefault("Accept", "application/vnd.api+json")
        timeout_s = self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20)

        base_url = f"https://openapi.tidal.com/v2/trackManifests/{quote(track_id, safe='')}"
        request_url = self._load_url_with_query(
            base_url=base_url,
            query_payload={
                "adaptive": "true" if adaptive else "false",
                "formats": formats,
                "manifestType": manifest_type,
                "uriScheme": uri_scheme,
                "usage": usage,
            },
        )

        return HttpRequestObject(
            object_name="tidal_api_track_manifest",
            method="GET",
            url=request_url,
            headers=request_headers,
            body=None,
            timeout_s=timeout_s,
        )

    def _load_widevine_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        body_base64 = str(arguments.get("body_base64") or "").strip()
        if not body_base64:
            raise ValueError("missing_body_base64")

        try:
            request_body = base64.b64decode(body_base64, validate=True)
        except Exception as exc:
            raise ValueError("invalid_body_base64") from exc

        request_headers = self._load_api_headers(arguments.get("headers"))
        request_headers.setdefault("Content-Type", "application/octet-stream")
        timeout_s = self._load_timeout_seconds(arguments.get("timeout_s"), default_timeout_s=20)

        return HttpRequestObject(
            object_name="tidal_api_widevine",
            method="POST",
            url="https://api.tidal.com/v2/widevine",
            headers=request_headers,
            body=request_body,
            timeout_s=timeout_s,
        )

    def _load_request_body(self, *, arguments: dict[str, Any], request_headers: dict[str, str]) -> bytes | None:
        if "body_base64" in arguments:
            body_base64 = str(arguments.get("body_base64") or "").strip()
            if not body_base64:
                return None
            try:
                return base64.b64decode(body_base64, validate=True)
            except Exception as exc:
                raise ValueError("invalid_body_base64") from exc

        if "body_json" in arguments:
            request_headers.setdefault("Content-Type", "application/json")
            return json.dumps(arguments.get("body_json"), ensure_ascii=False).encode("utf-8")

        if "body_text" in arguments:
            return str(arguments.get("body_text") or "").encode("utf-8")

        return None

    def _load_validated_tidal_url(self, raw_url: str) -> str:
        parsed = urlparse(str(raw_url or "").strip())
        if parsed.scheme not in self._ALLOWED_SCHEMES:
            raise ValueError("invalid_url_scheme")

        hostname = (parsed.hostname or "").casefold()
        if hostname != "tidal.com" and not hostname.endswith(".tidal.com"):
            raise ValueError("invalid_host_non_tidal")
        return parsed.geturl()

    def _load_url_with_query(self, *, base_url: str, query_payload: Any) -> str:
        if not isinstance(query_payload, dict) or not query_payload:
            return base_url

        parsed = urlparse(base_url)
        existing_query = parse_qs(parsed.query, keep_blank_values=True)
        for key, value in query_payload.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            if isinstance(value, (list, tuple)):
                existing_query[normalized_key] = [str(item) for item in value]
                continue
            existing_query[normalized_key] = [str(value)]

        merged_query = urlencode(existing_query, doseq=True)
        return urlunparse(parsed._replace(query=merged_query))

    def _load_headers_object(self, raw_headers: Any) -> dict[str, str]:
        if not isinstance(raw_headers, dict):
            return {}
        headers: dict[str, str] = {}
        for key, value in raw_headers.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            headers[normalized_key] = str(value)
        return headers

    def _load_api_headers(self, raw_headers: Any) -> dict[str, str]:
        headers = self._load_headers_object(raw_headers)
        header_names = {str(key).casefold() for key in headers}

        try:
            authorization_value = self.oauth_token_service.load_authorization_header()
        except Exception:
            authorization_value = ""
        if authorization_value and "authorization" not in header_names:
            headers["Authorization"] = authorization_value

        client_token = str(os.getenv("TIDAL_API_CLIENT_TOKEN") or "").strip()
        if client_token and "x-tidal-token" not in header_names:
            headers["x-tidal-token"] = client_token

        cookie_value = str(os.getenv("TIDAL_API_COOKIE") or "").strip()
        if cookie_value and "cookie" not in header_names:
            headers["Cookie"] = cookie_value

        return headers

    def _load_timeout_seconds(self, raw_timeout: Any, *, default_timeout_s: int) -> int:
        try:
            timeout_s = int(raw_timeout)
        except Exception:
            return int(default_timeout_s)
        return max(1, min(120, timeout_s))

    def _load_boolean_argument(self, raw_value: Any, *, default_value: bool) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if raw_value is None:
            return default_value

        normalized_value = str(raw_value).strip().casefold()
        if normalized_value in {"1", "true", "yes", "on"}:
            return True
        if normalized_value in {"0", "false", "no", "off"}:
            return False
        return default_value

    def _load_sanitized_headers(self, headers: dict[str, str]) -> dict[str, str]:
        sanitized_headers: dict[str, str] = {}
        for key, value in headers.items():
            if str(key).strip().casefold() in self._SENSITIVE_HEADERS:
                sanitized_headers[key] = "<redacted>"
                continue
            sanitized_headers[key] = str(value)
        return sanitized_headers

    def _load_response_body_payload(self, *, response_body_bytes: bytes, content_type: str) -> tuple[str, str | None]:
        if not response_body_bytes:
            return "", None

        normalized_content_type = str(content_type or "").casefold()
        media_type = normalized_content_type.split(";", 1)[0].strip()
        looks_textual = (
            "application/json" in normalized_content_type
            or "text/" in normalized_content_type
            or "application/problem+json" in normalized_content_type
            or "application/xml" in normalized_content_type
            or media_type.endswith("+json")
        )
        if looks_textual:
            return response_body_bytes.decode("utf-8", errors="replace"), None

        response_body_base64 = base64.b64encode(response_body_bytes).decode("ascii")
        return "", response_body_base64


@dataclass(frozen=True)
class TidalPreviewStream:
    body: bytes
    content_type: str = "audio/mp4"


class TidalApiPreviewService:
    """Resolve a clear TIDAL API preview into a browser-playable local stream."""

    _MAX_SEGMENTS = 64
    _MAX_SEGMENT_BYTES = 2_000_000
    _MAX_TOTAL_BYTES = 8_000_000

    def __init__(self, tidal_api_service: TidalApiService | None = None) -> None:
        self.tidal_api_service = tidal_api_service or TidalApiService()

    def load_object(self, *, track_id: str) -> TidalPreviewStream:
        normalized_track_id = str(track_id or "").strip()
        if not re.fullmatch(r"[0-9]{1,20}", normalized_track_id):
            raise ValueError("invalid_track_id")

        manifest_result = self.tidal_api_service.dispatch_object(
            object_name="tidal_api_track_manifest",
            arguments={
                "track_id": normalized_track_id,
                "formats": "AACLC",
                "manifest_type": "HLS",
                "uri_scheme": "HTTPS",
                "usage": "PLAYBACK",
            },
        )
        if not manifest_result.get("ok"):
            raise ValueError(str(manifest_result.get("error") or "tidal_preview_manifest_failed"))

        try:
            manifest_envelope = json.loads(str(manifest_result.get("response_body") or "{}"))
        except Exception as exc:
            raise ValueError("tidal_preview_manifest_invalid") from exc

        attributes = manifest_envelope.get("data", {}).get("attributes", {})
        if not isinstance(attributes, dict):
            raise ValueError("tidal_preview_attributes_missing")

        presentation = str(attributes.get("trackPresentation") or "").strip().upper()
        if presentation and presentation != "PREVIEW":
            raise ValueError("tidal_preview_full_asset_not_proxyable")

        manifest_url = self._load_tidal_url(attributes.get("uri"))
        manifest_text = self._load_text(manifest_url)
        variant_url, variant_text = self._load_variant_playlist(
            manifest_text,
            manifest_url,
        )
        if variant_text is None:
            variant_text = self._load_text(variant_url)

        source_urls = self._load_source_urls(variant_text, variant_url)
        parts = [self._load_bytes(source_url) for source_url in source_urls]
        total_bytes = sum(len(part) for part in parts)
        if total_bytes <= 0 or total_bytes > self._MAX_TOTAL_BYTES:
            raise ValueError("tidal_preview_size_limit")
        return TidalPreviewStream(body=b"".join(parts))

    def _load_tidal_url(self, raw_url: Any) -> str:
        parsed_url = urlparse(str(raw_url or "").strip())
        hostname = str(parsed_url.hostname or "").casefold()
        if parsed_url.scheme != "https" or (hostname != "tidal.com" and not hostname.endswith(".tidal.com")):
            raise ValueError("tidal_preview_source_host_invalid")
        return parsed_url.geturl()

    def _load_text(self, raw_url: str) -> str:
        request = Request(
            self._load_tidal_url(raw_url),
            headers={"Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, text/plain"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read(256_000)
        except HTTPError as exc:
            raise ValueError(f"tidal_preview_source_http_{int(exc.code or 0)}") from exc
        except Exception as exc:
            raise ValueError("tidal_preview_source_unavailable") from exc
        return body.decode("utf-8", errors="replace")

    def _load_bytes(self, raw_url: str) -> bytes:
        request = Request(self._load_tidal_url(raw_url), method="GET")
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read(self._MAX_SEGMENT_BYTES + 1)
        except HTTPError as exc:
            raise ValueError(f"tidal_preview_segment_http_{int(exc.code or 0)}") from exc
        except Exception as exc:
            raise ValueError("tidal_preview_segment_unavailable") from exc
        if len(body) > self._MAX_SEGMENT_BYTES:
            raise ValueError("tidal_preview_segment_size_limit")
        return body

    def _load_variant_playlist(self, playlist_text: str, playlist_url: str) -> tuple[str, str | None]:
        lines = [line.strip() for line in str(playlist_text or "").splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            variant_line = next((candidate for candidate in lines[index + 1 :] if not candidate.startswith("#")), "")
            if not variant_line:
                raise ValueError("tidal_preview_variant_missing")
            return self._load_tidal_url(urljoin(playlist_url, variant_line)), None
        return playlist_url, str(playlist_text or "")

    def _load_source_urls(self, playlist_text: str, playlist_url: str) -> list[str]:
        lines = [line.strip() for line in str(playlist_text or "").splitlines() if line.strip()]
        for line in lines:
            if (line.startswith("#EXT-X-KEY:") or line.startswith("#EXT-X-SESSION-KEY:")) and not re.search(
                r"METHOD=NONE(?:,|$)",
                line,
                flags=re.IGNORECASE,
            ):
                raise ValueError("tidal_preview_encrypted_manifest")

        source_urls: list[str] = []
        map_line = next((line for line in lines if line.startswith("#EXT-X-MAP:")), "")
        map_match = re.search(r'URI="([^"]+)"', map_line, flags=re.IGNORECASE)
        if map_match:
            source_urls.append(self._load_tidal_url(urljoin(playlist_url, map_match.group(1))))

        source_urls.extend(
            self._load_tidal_url(urljoin(playlist_url, line))
            for line in lines
            if not line.startswith("#")
        )
        if not source_urls or len(source_urls) > self._MAX_SEGMENTS + 1:
            raise ValueError("tidal_preview_segments_invalid")
        return source_urls


class TidalUserAuthService:
    _SUPPORTED_ACTIONS = {"webplayer_usrauth"}

    def __init__(self, *, token_service: TidalOAuthTokenService | None = None) -> None:
        self.token_service = token_service or TidalOAuthTokenService()
        self._pending_authorization_request: TidalAuthorizationRequest | None = None

    def dispatch_object(self, *, object_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized_object_name = str(object_name or "").strip().lower()
        if normalized_object_name not in self._SUPPORTED_ACTIONS:
            return self._load_result(
                object_name=normalized_object_name,
                error="unsupported_tool",
                supported_tools=sorted(self._SUPPORTED_ACTIONS),
            )

        argument_payload = dict(arguments or {})
        force_login = self._load_boolean_argument(argument_payload.get("force_login"), default_value=False)
        redirect_url = str(argument_payload.get("redirect_url") or "").strip()
        code = str(argument_payload.get("code") or "").strip()
        state = str(argument_payload.get("state") or "").strip()
        code_verifier = str(argument_payload.get("code_verifier") or "").strip()

        if redirect_url and not code:
            redirect_response = self._load_redirect_response_payload(redirect_url)
            redirect_error = str(redirect_response.get("error") or "").strip()
            if redirect_error:
                token_summary = self._load_token_summary()
                return self._load_result(
                    object_name=normalized_object_name,
                    error=f"authorization_denied:{redirect_error}",
                    status="login_required",
                    login_action="complete",
                    authentication_state="login_failed",
                    error_description=str(redirect_response.get("error_description") or "").strip() or None,
                    **self._load_token_summary_details(token_summary),
                )
            redirect_code = str(redirect_response.get("code") or "").strip()
            redirect_state = str(redirect_response.get("state") or "").strip()
            if redirect_code:
                code = redirect_code
            if redirect_state and not state:
                state = redirect_state

        token_summary = self._load_token_summary()
        if code:
            return self._complete_login_object(
                code=code,
                state=state,
                code_verifier=code_verifier,
                token_summary=token_summary,
            )

        if token_summary["authenticated"] and not force_login:
            payload = self._load_result(
                object_name=normalized_object_name,
                status="authenticated",
                login_action="status",
                authentication_state="ready",
                **self._load_token_summary_details(token_summary),
            )
            return payload

        return self._begin_login_object(
            object_name=normalized_object_name,
            force_login=force_login,
            token_summary=token_summary,
        )

    def _begin_login_object(
        self,
        *,
        object_name: str,
        force_login: bool,
        token_summary: dict[str, Any],
    ) -> dict[str, Any]:
        pending_request = self._pending_authorization_request
        if pending_request is None or force_login:
            try:
                pending_request = self.token_service.build_authorization_request()
            except TidalOAuthError as exc:
                return self._load_result(
                    object_name=object_name,
                    error=str(exc),
                    status="login_required",
                    login_action="begin",
                    authentication_state=token_summary.get("authentication_state"),
                    **self._load_token_summary_details(token_summary),
                )
            self._pending_authorization_request = pending_request

        callback_path = self._load_callback_path()
        payload = self._load_result(
            object_name=object_name,
            status="login_required",
            login_action="begin",
            authentication_state="awaiting_callback",
            authorization_url=pending_request.authorization_url,
            redirect_uri=self._load_redirect_uri(),
            callback_path=callback_path,
            state=pending_request.state,
            pending_login=True,
            **self._load_token_summary_details(token_summary),
        )
        if token_summary.get("load_error"):
            payload["warning"] = str(token_summary.get("load_error") or "")
        return payload

    def _complete_login_object(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        token_summary: dict[str, Any],
    ) -> dict[str, Any]:
        pending_request = self._pending_authorization_request
        resolved_code_verifier = str(code_verifier or "").strip()
        expected_state = str(state or "").strip() or None
        if not resolved_code_verifier and pending_request is not None:
            resolved_code_verifier = str(pending_request.code_verifier or "").strip()
        if expected_state is None and pending_request is not None:
            expected_state = str(pending_request.state or "").strip() or None
        if not resolved_code_verifier:
            return self._load_result(
                object_name="webplayer_usrauth",
                error="missing_code_verifier",
                status="login_required",
                login_action="complete",
                authentication_state=token_summary.get("authentication_state"),
                **self._load_token_summary_details(token_summary),
            )

        try:
            token_payload = self.token_service.exchange_authorization_code(
                code=code,
                code_verifier=resolved_code_verifier,
                state=state or None,
                expected_state=expected_state,
            )
        except TidalOAuthError as exc:
            return self._load_result(
                object_name="webplayer_usrauth",
                error=str(exc),
                status="login_required",
                login_action="complete",
                authentication_state="login_failed",
                **self._load_token_summary_details(token_summary),
            )

        self._pending_authorization_request = None
        refreshed_summary = self._load_token_summary()
        payload = self._load_result(
            object_name="webplayer_usrauth",
            status="authenticated",
            login_action="complete",
            authentication_state="ready",
            token_store=self._load_token_file_path(),
            token_response_type=str(token_payload.get("token_type") or "Bearer"),
            **self._load_token_summary_details(refreshed_summary),
        )
        return payload

    def _load_token_summary(self) -> dict[str, Any]:
        static_authorization = ""
        try:
            static_authorization = str(self.token_service._load_static_authorization() or "").strip()
        except Exception:
            static_authorization = ""
        if static_authorization:
            return {
                "authenticated": True,
                "authentication_state": "ready",
                "token_source": "environment",
                "has_access_token": True,
                "has_refresh_token": False,
                "token_current": True,
                "load_error": "",
                "token_file": self._load_token_file_path(),
                "expires_at": None,
                "expires_in_seconds": None,
            }

        token_payload = self.token_service.load_token_payload()
        load_error = ""
        resolved_access_token = ""
        try:
            resolved_access_token = str(self.token_service.load_access_token() or "").strip()
        except TidalOAuthError as exc:
            load_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            load_error = f"token_load_failed:{type(exc).__name__}"
        refreshed_payload = self.token_service.load_token_payload()
        if refreshed_payload:
            token_payload = refreshed_payload

        has_access_token = bool(str(token_payload.get("access_token") or "").strip())
        has_refresh_token = bool(str(token_payload.get("refresh_token") or "").strip())
        expires_at = self._load_float(token_payload.get("expires_at"))
        expires_in_seconds: int | None = None
        if expires_at is not None:
            expires_in_seconds = int(expires_at - time.time())

        token_current = bool(resolved_access_token)
        if token_current:
            authentication_state = "ready"
        elif has_access_token or has_refresh_token:
            authentication_state = "refresh_failed" if load_error else "expired"
        else:
            authentication_state = "missing"

        return {
            "authenticated": token_current,
            "authentication_state": authentication_state,
            "token_source": "token_file",
            "has_access_token": has_access_token,
            "has_refresh_token": has_refresh_token,
            "token_current": token_current,
            "load_error": load_error,
            "token_file": self._load_token_file_path(),
            "expires_at": expires_at,
            "expires_in_seconds": expires_in_seconds,
        }

    def _load_result(self, *, object_name: str, error: str | None = None, **details: Any) -> dict[str, Any]:
        normalized_error = str(error or "").strip()
        metadata = {key: value for key, value in details.items() if value is not None}
        stdout_lines = [f"command={object_name}", f"status={str(metadata.get('status') or ('error' if normalized_error else 'ready'))}"]
        if normalized_error:
            stdout_lines.insert(0, f"error={normalized_error}")
        for key, value in metadata.items():
            if key == "status":
                continue
            if isinstance(value, (str, int, float, bool)):
                safe_value = str(value).replace("\n", " ").replace("\r", " ").replace("=", "-").strip()
                stdout_lines.append(f"{key}={safe_value}")

        payload: dict[str, Any] = {
            "object_name": object_name,
            "ok": not normalized_error,
            "return_code": 0 if not normalized_error else 1,
            "stdout": "\n".join(stdout_lines),
            "stderr": "" if not normalized_error else normalized_error,
            "command": object_name,
        }
        if normalized_error:
            payload["error"] = normalized_error
        payload.update(metadata)
        return payload

    def _load_token_file_path(self) -> str:
        return str(getattr(self.token_service, "token_file", "") or "").strip()

    def _load_token_summary_details(self, token_summary: dict[str, Any]) -> dict[str, Any]:
        summary_payload = dict(token_summary or {})
        summary_payload.pop("authentication_state", None)
        return summary_payload

    def _load_boolean_argument(self, raw_value: Any, *, default_value: bool) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if raw_value is None:
            return default_value

        normalized_value = str(raw_value).strip().casefold()
        if normalized_value in {"1", "true", "yes", "on"}:
            return True
        if normalized_value in {"0", "false", "no", "off"}:
            return False
        return default_value

    def _load_float(self, raw_value: Any) -> float | None:
        try:
            return float(raw_value)
        except Exception:
            return None

    def _load_redirect_response_payload(self, redirect_url: str) -> dict[str, str]:
        parsed_url = urlparse(str(redirect_url or "").strip())
        response_payload = parse_qs(parsed_url.query, keep_blank_values=True)
        return {
            "code": str((response_payload.get("code") or [""])[0] or "").strip(),
            "state": str((response_payload.get("state") or [""])[0] or "").strip(),
            "error": str((response_payload.get("error") or [""])[0] or "").strip(),
            "error_description": str((response_payload.get("error_description") or [""])[0] or "").strip(),
        }

    def _load_callback_path(self) -> str:
        redirect_uri = self._load_redirect_uri()
        parsed_uri = urlparse(redirect_uri)
        callback_path = str(parsed_uri.path or "/").strip() or "/"
        return callback_path if callback_path.startswith("/") else f"/{callback_path}"

    def _load_redirect_uri(self) -> str:
        return str(getattr(self.token_service, "redirect_uri", "") or "").strip()


class TidalApiPlaybackService:
    _SUPPORTED_ACTIONS = {
        "webplayer_play",
        "webplayer_stop",
        "webplayer_forward",
        "webplayer_backward",
        "webplayer_now_playing",
        "webplayer_get_media_product",
        "webplayer_get_next_media_product",
        "webplayer_pause",
        "webplayer_seek",
        "webplayer_search",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_library_snapshot",
        "webplayer_open_playback_target",
        "webplayer_volume_set",
        "webplayer_volume_adjust",
        "webplayer_system_volume_set",
        "webplayer_system_volume_adjust",
        "webplayer_like_current",
        "webplayer_toggle_like_current",
    }
    _API_PLAYER_PREFIXES = ("cvlc", "vlc", "mpv", "mpd")

    def __init__(
        self,
        tidal_api_service: TidalApiService,
        *,
        player_binary: str | None = None,
        playlist_repository: PlaybackPlaylistRepository | None = None,
    ) -> None:
        self.tidal_api_service = tidal_api_service
        self.player_binary = str(player_binary or os.getenv("TIDAL_API_PLAYER_BINARY") or "cvlc").strip() or "cvlc"
        self.playlist_repository = playlist_repository or PlaybackPlaylistRepository()
        self._playlist = self.playlist_repository.load_playlist()
        self._process: subprocess.Popen | None = None
        self._queue: list[dict[str, Any]] = [track.to_track() for track in self._playlist.tracks]
        self._queue_index = self._playlist.active_index
        self._active_track: dict[str, Any] = (
            dict(self._queue[self._queue_index])
            if self._queue and 0 <= self._queue_index < len(self._queue)
            else {}
        )
        self._playlist_persistence_error = str(self.playlist_repository.last_load_error or "").strip()

    def dispatch_object(
        self,
        *,
        object_name: str,
        query: str | None,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_object_name = str(object_name or "").strip().lower()
        if normalized_object_name not in self._SUPPORTED_ACTIONS:
            return self._load_result(
                object_name=normalized_object_name,
                error="api_backend_unsupported_tool",
            )

        sdk_prepare = self._load_boolean_argument(arguments.get("sdk_prepare"), default_value=False)
        try:
            if normalized_object_name == "webplayer_search":
                return self._search_object(query=query, arguments=arguments)
            if normalized_object_name == "webplayer_search_play":
                return self._search_play_object(query=query, arguments=arguments, sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_playlist_play":
                return self._playlist_play_object(arguments=arguments, sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_library_play":
                return self._library_play_object(arguments=arguments, sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_library_snapshot":
                return self._library_snapshot_object(arguments=arguments)
            if normalized_object_name == "webplayer_open_playback_target":
                return self._open_target_object(arguments=arguments, sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_play":
                return self._play_current_object(sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_pause":
                if sdk_prepare:
                    return self._sdk_control_object(object_name=normalized_object_name, arguments=arguments)
                return self._pause_object(arguments=arguments)
            if normalized_object_name == "webplayer_stop":
                if sdk_prepare:
                    return self._sdk_control_object(object_name=normalized_object_name, arguments=arguments)
                return self._stop_object()
            if normalized_object_name == "webplayer_forward":
                return self._skip_object(step=1, sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_backward":
                return self._skip_object(step=-1, sdk_prepare=sdk_prepare)
            if normalized_object_name == "webplayer_now_playing":
                return self._now_playing_object()
            if normalized_object_name == "webplayer_get_media_product":
                return self._media_product_object()
            if normalized_object_name == "webplayer_get_next_media_product":
                return self._next_media_product_object()
            if normalized_object_name == "webplayer_seek":
                if sdk_prepare:
                    return self._sdk_control_object(object_name=normalized_object_name, arguments=arguments)
                return self._seek_object(arguments=arguments)
            if normalized_object_name == "webplayer_volume_set":
                if sdk_prepare:
                    return self._sdk_control_object(object_name=normalized_object_name, arguments=arguments)
                return self._volume_set_object(arguments=arguments)
            if normalized_object_name == "webplayer_volume_adjust":
                if sdk_prepare:
                    return self._sdk_control_object(object_name=normalized_object_name, arguments=arguments)
                return self._volume_adjust_object(arguments=arguments)
            if normalized_object_name == "webplayer_system_volume_set":
                return self._system_volume_set_object(arguments=arguments)
            if normalized_object_name == "webplayer_system_volume_adjust":
                return self._system_volume_adjust_object(arguments=arguments)
            if normalized_object_name in {"webplayer_like_current", "webplayer_toggle_like_current"}:
                return self._like_current_object(object_name=normalized_object_name, arguments=arguments)
        except Exception as exc:
            return self._load_result(
                object_name=normalized_object_name,
                error=f"api_backend_failed:{type(exc).__name__}",
            )

        return self._load_result(object_name=normalized_object_name, error="api_backend_unhandled_tool")

    def _sdk_control_object(self, *, object_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return an SDK command without controlling a local process or MPRIS player."""
        if object_name == "webplayer_seek":
            has_absolute = arguments.get("position_seconds") is not None
            has_delta = arguments.get("delta_seconds") is not None
            if not has_absolute and not has_delta:
                return self._load_result(object_name=object_name, error="missing_seek_position")
            try:
                if has_absolute:
                    position_seconds = float(arguments.get("position_seconds"))
                    if position_seconds < 0:
                        raise ValueError("negative_position")
                    seek_mode = "absolute"
                else:
                    position_seconds = float(arguments.get("delta_seconds"))
                    if position_seconds == 0:
                        raise ValueError("zero_delta")
                    seek_mode = "relative"
            except Exception:
                return self._load_result(object_name=object_name, error="invalid_seek_position")
            return self._load_result(
                object_name=object_name,
                status="Ready",
                sdk_command="seek",
                seek_mode=seek_mode,
                seek_value=position_seconds,
                **self._load_active_track_details(),
            )

        if object_name == "webplayer_volume_set":
            raw_percent = arguments.get("volume_percent")
            raw_float = arguments.get("volume_float")
            try:
                volume_percent = int(raw_percent) if raw_percent is not None else round(float(raw_float) * 100)
            except Exception:
                return self._load_result(object_name=object_name, error="invalid_volume")
            return self._load_result(
                object_name=object_name,
                status="Ready",
                sdk_command="set_volume",
                volume_percent=max(0, min(100, volume_percent)),
                **self._load_active_track_details(),
            )

        if object_name == "webplayer_volume_adjust":
            raw_percent = arguments.get("delta_percent")
            raw_float = arguments.get("delta_float")
            try:
                delta_percent = int(raw_percent) if raw_percent is not None else round(float(raw_float) * 100)
            except Exception:
                return self._load_result(object_name=object_name, error="invalid_volume_delta")
            return self._load_result(
                object_name=object_name,
                status="Ready",
                sdk_command="adjust_volume",
                delta_percent=delta_percent,
                **self._load_active_track_details(),
            )

        if object_name == "webplayer_pause":
            return self._load_result(
                object_name=object_name,
                status="Paused",
                sdk_command="pause",
                **self._load_active_track_details(),
            )

        if object_name == "webplayer_stop":
            self._stop_process()
            playlist_persisted, persistence_error = self._persist_playlist_state(status="stopped")
            return self._load_result(
                object_name=object_name,
                status="Stopped",
                sdk_command="reset",
                playlist_persisted=playlist_persisted,
                playlist_persistence_error=persistence_error,
                **self._load_active_track_details(),
            )

        return self._load_result(object_name=object_name, error="sdk_command_unsupported")

    def _search_object(self, *, query: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized_query = str(query or arguments.get("query") or "").strip()
        if not normalized_query:
            return self._load_result(object_name="webplayer_search", error="missing_query")

        tracks, error = self._load_search_tracks(query=normalized_query, arguments=arguments)
        if error:
            return self._load_result(object_name="webplayer_search", error=error, query=normalized_query)
        return self._load_result(
            object_name="webplayer_search",
            query=normalized_query,
            track_count=len(tracks),
            tracks=tracks,
        )

    def _search_play_object(
        self,
        *,
        query: str | None,
        arguments: dict[str, Any],
        sdk_prepare: bool = False,
    ) -> dict[str, Any]:
        normalized_query = str(query or arguments.get("query") or "").strip()
        if not normalized_query:
            return self._load_result(object_name="webplayer_search_play", error="missing_query")

        tracks, error = self._load_search_tracks(query=normalized_query, arguments=arguments)
        if error:
            return self._load_result(object_name="webplayer_search_play", error=error, query=normalized_query)
        if not tracks:
            return self._load_result(
                object_name="webplayer_search_play",
                error="api_search_no_tracks",
                query=normalized_query,
            )
        return self._play_queue_object(
            object_name="webplayer_search_play",
            tracks=tracks,
            queue_index=0,
            query=normalized_query,
            playlist_source={
                "object_name": "webplayer_search_play",
                "query": normalized_query,
                "source_type": "search",
            },
            sdk_prepare=sdk_prepare,
        )

    def _playlist_play_object(self, *, arguments: dict[str, Any], sdk_prepare: bool = False) -> dict[str, Any]:
        playlist_url = str(arguments.get("playlist_url") or "").strip()
        playlist_id = str(arguments.get("playlist_id") or "").strip()
        if playlist_url:
            validated_playlist_url = self._load_validated_tidal_target_url(playlist_url)
            if not validated_playlist_url:
                return self._load_result(object_name="webplayer_playlist_play", error="invalid_target_url")
            playlist_id = self._load_target_id(validated_playlist_url, target_kind="playlist")
        if not playlist_id:
            return self._load_result(object_name="webplayer_playlist_play", error="missing_playlist_target")

        tracks, error = self._load_collection_tracks(collection_kind="playlists", collection_id=playlist_id, arguments=arguments)
        if error:
            return self._load_result(object_name="webplayer_playlist_play", error=error)
        return self._play_queue_object(
            object_name="webplayer_playlist_play",
            tracks=tracks,
            queue_index=0,
            playlist_source={
                "object_name": "webplayer_playlist_play",
                "playlist_id": playlist_id,
                "playlist_url": playlist_url,
                "source_type": "playlist",
            },
            sdk_prepare=sdk_prepare,
        )

    def _library_play_object(self, *, arguments: dict[str, Any], sdk_prepare: bool = False) -> dict[str, Any]:
        raw_track_ids = arguments.get("track_ids")
        track_ids = [
            str(track_id or "").strip()
            for track_id in raw_track_ids
            if str(track_id or "").strip()
        ] if isinstance(raw_track_ids, list) else []
        if track_ids:
            tracks: list[dict[str, Any]] = []
            for track_id in dict.fromkeys(track_ids):
                track, error = self._load_track(track_id=track_id, arguments=arguments)
                if error:
                    return self._load_result(
                        object_name="webplayer_library_play",
                        error=error,
                        track_id=track_id,
                        requested_track_count=len(track_ids),
                    )
                tracks.append(track)
            return self._play_queue_object(
                object_name="webplayer_library_play",
                tracks=tracks,
                queue_index=0,
                playlist_source={
                    "object_name": "webplayer_library_play",
                    "collection_name": str(arguments.get("collection_name") or "generated_collection").strip(),
                    "source_type": "generated_collection",
                    "track_ids": [str(track.get("tidal_track_id") or "").strip() for track in tracks],
                },
                sdk_prepare=sdk_prepare,
            )

        track_id = str(arguments.get("track_id") or "").strip()
        target_url = str(arguments.get("library_url") or "").strip()
        if target_url:
            validated_target_url = self._load_validated_tidal_target_url(target_url)
            if not validated_target_url:
                return self._load_result(object_name="webplayer_library_play", error="invalid_target_url")
            track_id = self._load_target_id(validated_target_url, target_kind="track")
        section = str(arguments.get("section") or "favorites_tracks").strip().casefold()
        if not track_id and not target_url and section not in {
            "favorites_tracks",
            "favorites_albums",
            "favorites_playlists",
            "my_collection_tracks",
            "history",
            "home",
        }:
            return self._load_result(object_name="webplayer_library_play", error="invalid_library_section")
        if not track_id and not target_url:
            if section not in {"favorites_tracks", "my_collection_tracks"}:
                return self._load_result(
                    object_name="webplayer_library_play",
                    error="api_library_section_unsupported",
                )
            tracks, error = self._load_collection_tracks(
                collection_kind="userCollectionTracks",
                collection_id="me",
                arguments=arguments,
            )
            if error:
                return self._load_result(object_name="webplayer_library_play", error=error)
            return self._play_queue_object(
                object_name="webplayer_library_play",
                tracks=tracks,
                queue_index=0,
                playlist_source={
                    "object_name": "webplayer_library_play",
                    "section": section,
                    "source_type": "library",
                },
                sdk_prepare=sdk_prepare,
            )
        if track_id:
            track, error = self._load_track(track_id=track_id, arguments=arguments)
            if error:
                return self._load_result(object_name="webplayer_library_play", error=error)
            return self._play_queue_object(
                object_name="webplayer_library_play",
                tracks=[track],
                queue_index=0,
                playlist_source={
                    "object_name": "webplayer_library_play",
                    "section": section,
                    "track_id": track_id,
                    "source_type": "library",
                },
                sdk_prepare=sdk_prepare,
            )

        return self._load_result(
            object_name="webplayer_library_play",
            error="api_library_requires_track_id_or_track_url",
        )

    def _library_snapshot_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        section = str(arguments.get("section") or "favorites_tracks").strip().casefold()
        if section not in {"favorites_tracks", "my_collection_tracks"}:
            return self._load_result(
                object_name="webplayer_library_snapshot",
                error="api_library_section_unsupported",
                snapshot_backend="api",
                snapshot_count=0,
                tracks=[],
            )

        tracks, error = self._load_collection_tracks(
            collection_kind="userCollectionTracks",
            collection_id="me",
            arguments=arguments,
        )
        max_items = self._load_bounded_int(
            arguments.get("max_items"),
            default_value=200,
            minimum=1,
            maximum=2000,
        )
        limited_tracks = tracks[:max_items]
        if error:
            return self._load_result(
                object_name="webplayer_library_snapshot",
                error=error,
                snapshot_backend="api",
                snapshot_count=0,
                tracks=[],
            )
        return self._load_result(
            object_name="webplayer_library_snapshot",
            snapshot_backend="api",
            snapshot_source="tidal_api",
            snapshot_ok=True,
            snapshot_count=len(limited_tracks),
            tracks=limited_tracks,
        )

    def _open_target_object(self, *, arguments: dict[str, Any], sdk_prepare: bool = False) -> dict[str, Any]:
        raw_target_url = str(arguments.get("target_url") or "").strip()
        if not raw_target_url:
            return self._load_result(object_name="webplayer_open_playback_target", error="missing_target_url")
        target_url = self._load_validated_tidal_target_url(raw_target_url)
        if not target_url:
            return self._load_result(object_name="webplayer_open_playback_target", error="invalid_target_url")

        target_kind = self._load_target_kind(target_url)
        target_id = self._load_target_id(target_url, target_kind=target_kind)
        if not target_kind or not target_id:
            return self._load_result(object_name="webplayer_open_playback_target", error="unsupported_target_url")

        if target_kind == "track":
            track, error = self._load_track(track_id=target_id, arguments=arguments)
            if error:
                return self._load_result(object_name="webplayer_open_playback_target", error=error)
            tracks = [track]
        else:
            tracks, error = self._load_collection_tracks(
                collection_kind=f"{target_kind}s",
                collection_id=target_id,
                arguments=arguments,
            )
            if error:
                return self._load_result(object_name="webplayer_open_playback_target", error=error)

        return self._play_queue_object(
            object_name="webplayer_open_playback_target",
            tracks=tracks,
            queue_index=0,
            target_url=target_url,
            playlist_source={
                "object_name": "webplayer_open_playback_target",
                "target_url": target_url,
                "target_kind": target_kind,
                "target_id": target_id,
                "source_type": "target",
            },
            sdk_prepare=sdk_prepare,
        )

    def _play_current_object(self, *, sdk_prepare: bool = False) -> dict[str, Any]:
        if not self._active_track:
            return self._load_result(object_name="webplayer_play", error="api_no_current_track")
        return self._play_queue_object(
            object_name="webplayer_play",
            tracks=self._queue or [dict(self._active_track)],
            queue_index=max(0, self._queue_index),
            sdk_prepare=sdk_prepare,
        )

    def _stop_object(self) -> dict[str, Any]:
        self._stop_process()
        playlist_persisted, persistence_error = self._persist_playlist_state(status="stopped")
        return self._load_result(
            object_name="webplayer_stop",
            status="Stopped",
            playlist_persisted=playlist_persisted,
            playlist_persistence_error=persistence_error,
            **self._load_active_track_details(),
        )

    def _skip_object(self, *, step: int, sdk_prepare: bool = False) -> dict[str, Any]:
        if not self._queue:
            return self._load_result(
                object_name="webplayer_forward" if step > 0 else "webplayer_backward",
                error="api_no_queue",
            )

        next_index = self._queue_index + int(step)
        if next_index < 0 or next_index >= len(self._queue):
            return self._load_result(
                object_name="webplayer_forward" if step > 0 else "webplayer_backward",
                error="api_queue_boundary",
            )

        return self._play_queue_object(
            object_name="webplayer_forward" if step > 0 else "webplayer_backward",
            tracks=self._queue,
            queue_index=next_index,
            sdk_prepare=sdk_prepare,
        )

    def _now_playing_object(self) -> dict[str, Any]:
        status = "Playing" if self._is_process_running() else "Stopped"
        return self._load_result(
            object_name="webplayer_now_playing",
            status=status,
            **self._load_active_track_details(),
        )

    def _media_product_object(self) -> dict[str, Any]:
        if not self._active_track:
            return self._load_result(object_name="webplayer_get_media_product", error="api_no_current_track")
        status = "Playing" if self._is_process_running() else "Stopped"
        return self._load_result(
            object_name="webplayer_get_media_product",
            status=status,
            **self._load_active_track_details(),
        )

    def _next_media_product_object(self) -> dict[str, Any]:
        next_media_product = self._load_next_media_product_snapshot()
        if not next_media_product:
            return self._load_result(object_name="webplayer_get_next_media_product", error="api_no_next_media_product")

        return self._load_result(
            object_name="webplayer_get_next_media_product",
            status="Ready",
            media_product=next_media_product,
            next_media_product=next_media_product,
            playback_context=self._load_playback_context(),
            **next_media_product,
        )

    def _pause_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        player_selector = str(arguments.get("player_selector") or "tidal-api").strip() or "tidal-api"
        selected_player, pause_output = self._run_playerctl_command(
            player_selector=player_selector,
            command_name="pause",
            timeout_s=5,
        )
        if not selected_player:
            return self._load_result(
                object_name="webplayer_pause",
                error=pause_output or "playerctl_no_players",
                **self._load_active_track_details(),
            )
        if pause_output.startswith("playerctl_"):
            return self._load_result(
                object_name="webplayer_pause",
                error=pause_output,
                player=selected_player,
                **self._load_active_track_details(),
            )

        status_text = self._load_playerctl_status(player_selector=player_selector)
        persisted_status = "paused"
        if status_text and status_text.strip():
            persisted_status = status_text.strip().casefold()
        playlist_persisted, persistence_error = self._persist_playlist_state(status=persisted_status)
        return self._load_result(
            object_name="webplayer_pause",
            status="Paused",
            player=selected_player,
            playlist_persisted=playlist_persisted,
            playlist_persistence_error=persistence_error,
            **self._load_active_track_details(),
        )

    def _seek_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        player_selector = str(arguments.get("player_selector") or "tidal-api").strip() or "tidal-api"
        raw_position_seconds = arguments.get("position_seconds")
        raw_delta_seconds = arguments.get("delta_seconds")
        if raw_position_seconds is None and raw_delta_seconds is None:
            return self._load_result(
                object_name="webplayer_seek",
                error="missing_seek_position",
                **self._load_active_track_details(),
            )

        try:
            seek_spec, seek_mode = _load_seek_spec(
                position_seconds=raw_position_seconds,
                delta_seconds=raw_delta_seconds,
            )
        except ValueError:
            return self._load_result(
                object_name="webplayer_seek",
                error="invalid_seek_position",
                **self._load_active_track_details(),
            )

        selected_player, seek_output = self._run_playerctl_command(
            player_selector=player_selector,
            command_name="position",
            command_arguments=[seek_spec],
            timeout_s=5,
        )
        if not selected_player:
            return self._load_result(
                object_name="webplayer_seek",
                error=seek_output or "playerctl_no_players",
                **self._load_active_track_details(),
            )
        if seek_output.startswith("playerctl_"):
            return self._load_result(
                object_name="webplayer_seek",
                error=seek_output,
                player=selected_player,
                seek_spec=seek_spec,
                seek_mode=seek_mode,
                **self._load_active_track_details(),
            )

        status_text = self._load_playerctl_status(player_selector=player_selector)
        return self._load_result(
            object_name="webplayer_seek",
            status=status_text.title() if status_text else "Stopped",
            player=selected_player,
            seek_spec=seek_spec,
            seek_mode=seek_mode,
            **self._load_active_track_details(),
        )

    def _volume_set_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_percent = arguments.get("volume_percent")
        raw_float = arguments.get("volume_float")
        try:
            if raw_percent is not None:
                volume_percent = int(raw_percent)
            elif raw_float is not None:
                volume_percent = round(float(raw_float) * 100)
            else:
                return self._load_result(object_name="webplayer_volume_set", error="missing_volume")
        except Exception:
            return self._load_result(object_name="webplayer_volume_set", error="invalid_volume")

        return self._set_system_volume(volume_percent=volume_percent, object_name="webplayer_volume_set")

    def _volume_adjust_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_percent = arguments.get("delta_percent")
        raw_float = arguments.get("delta_float")
        try:
            if raw_percent is not None:
                delta_percent = int(raw_percent)
            elif raw_float is not None:
                delta_percent = round(float(raw_float) * 100)
            else:
                return self._load_result(object_name="webplayer_volume_adjust", error="missing_volume_delta")
        except Exception:
            return self._load_result(object_name="webplayer_volume_adjust", error="invalid_volume_delta")

        current_percent = self._load_system_volume_percent()
        if current_percent is None:
            return self._load_result(object_name="webplayer_volume_adjust", error="system_volume_unavailable")
        return self._set_system_volume(
            volume_percent=current_percent + delta_percent,
            object_name="webplayer_volume_adjust",
        )

    def _system_volume_set_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_percent = arguments.get("volume_percent")
        raw_float = arguments.get("volume_float")
        try:
            if raw_percent is not None:
                volume_percent = int(raw_percent)
            elif raw_float is not None:
                volume_percent = round(float(raw_float) * 100)
            else:
                return self._load_result(object_name="webplayer_system_volume_set", error="missing_volume")
        except Exception:
            return self._load_result(object_name="webplayer_system_volume_set", error="invalid_volume")

        return self._set_system_volume(volume_percent=volume_percent, object_name="webplayer_system_volume_set")

    def _system_volume_adjust_object(self, *, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_percent = arguments.get("delta_percent")
        raw_float = arguments.get("delta_float")
        try:
            if raw_percent is not None:
                delta_percent = int(raw_percent)
            elif raw_float is not None:
                delta_percent = round(float(raw_float) * 100)
            else:
                return self._load_result(
                    object_name="webplayer_system_volume_adjust",
                    error="missing_volume_delta",
                )
        except Exception:
            return self._load_result(
                object_name="webplayer_system_volume_adjust",
                error="invalid_volume_delta",
            )

        current_percent = self._load_system_volume_percent()
        if current_percent is None:
            return self._load_result(
                object_name="webplayer_system_volume_adjust",
                error="system_volume_unavailable",
            )
        return self._set_system_volume(
            volume_percent=current_percent + delta_percent,
            object_name="webplayer_system_volume_adjust",
        )

    def _like_current_object(self, *, object_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        requested_track_id = str(arguments.get("track_id") or "").strip()
        active_track = dict(self._active_track)
        if requested_track_id:
            if str(active_track.get("tidal_track_id") or "").strip() != requested_track_id:
                active_track = {"tidal_track_id": requested_track_id}
                for metadata_key in ("title", "artist", "album", "track_url", "duration"):
                    metadata_value = str(arguments.get(metadata_key) or "").strip()
                    if metadata_value:
                        active_track[metadata_key] = metadata_value
            if "liked" in arguments:
                active_track["liked"] = self._load_boolean_argument(arguments.get("liked"), default_value=False)
            else:
                active_track.setdefault("liked", False)
            self._active_track = dict(active_track)

        if not active_track:
            return self._load_result(object_name=object_name, error="api_no_current_track")

        track_id = str(active_track.get("tidal_track_id") or "").strip()
        if not track_id:
            return self._load_result(object_name=object_name, error="api_track_id_missing")

        currently_liked = bool(active_track.get("liked"))
        should_remove = object_name == "webplayer_toggle_like_current" and currently_liked
        if object_name == "webplayer_like_current" and currently_liked:
            active_track_details = self._load_active_track_details()
            active_track_details["liked"] = True
            return self._load_result(
                object_name=object_name,
                status="Already liked",
                **active_track_details,
            )

        request_headers = dict(arguments.get("headers") or {}) if isinstance(arguments.get("headers"), dict) else {}
        request_headers.setdefault("Accept", "application/vnd.api+json")
        request_headers.setdefault("Content-Type", "application/vnd.api+json")
        request_payload = {
            "data": [
                {
                    "type": "tracks",
                    "id": track_id,
                }
            ]
        }
        response_payload, error = self._load_api_response(
            object_name="tidal_api_request",
            arguments={
                "method": "DELETE" if should_remove else "POST",
                "url": "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items",
                "headers": request_headers,
                "body_json": request_payload,
                "timeout_s": arguments.get("timeout_s"),
            },
        )
        if error:
            return self._load_result(
                object_name=object_name,
                error=error,
                api_response_error=response_payload.get("error"),
                **self._load_active_track_details(),
            )

        self._active_track["liked"] = not should_remove
        active_track_details = self._load_active_track_details()
        active_track_details["liked"] = not should_remove
        return self._load_result(
            object_name=object_name,
            status="Unliked" if should_remove else "Liked",
            **active_track_details,
        )

    def _set_system_volume(self, *, volume_percent: int, object_name: str) -> dict[str, Any]:
        normalized_percent = max(0, min(100, int(volume_percent)))
        if shutil.which("pactl") is None:
            return self._load_result(object_name=object_name, error="pactl_missing")
        completed = subprocess.run(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{normalized_percent}%"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return self._load_result(object_name=object_name, error="system_volume_set_failed")
        return self._load_result(object_name=object_name, volume_percent=normalized_percent)

    def _load_system_volume_percent(self) -> int | None:
        if shutil.which("pactl") is None:
            return None
        completed = subprocess.run(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            capture_output=True,
            text=True,
            check=False,
        )
        match = re.search(r"(\d+)%", str(completed.stdout or ""))
        if not match:
            return None
        return int(match.group(1))

    def _play_queue_object(
        self,
        *,
        object_name: str,
        tracks: list[dict[str, Any]],
        queue_index: int,
        playlist_source: dict[str, Any] | None = None,
        sdk_prepare: bool = False,
        **details: Any,
    ) -> dict[str, Any]:
        if not tracks:
            return self._load_result(object_name=object_name, error="api_queue_empty", **details)
        normalized_index = max(0, min(int(queue_index), len(tracks) - 1))
        track = dict(tracks[normalized_index])
        track_id = str(track.get("tidal_track_id") or track.get("id") or "").strip()
        if not track_id:
            return self._load_result(object_name=object_name, error="api_track_id_missing", **details)

        if sdk_prepare:
            # The official Web SDK resolves the playback information and owns
            # the media element. Keep this server step API-only: resolve and
            # persist metadata/queue state, but never fetch a manifest or
            # start a local player process.
            for queue_track in tracks:
                queue_track.pop("stream_uri", None)
                queue_track.pop("manifest_uri", None)
                queue_track.pop("manifest", None)
            tracks = self._hydrate_track_contexts(
                tracks=tracks,
                arguments={},
                indices=[normalized_index, normalized_index + 1],
            )
            track = dict(tracks[normalized_index])
            track["media_product_type"] = "track"
            self._stop_process()
            self._queue = [dict(item) for item in tracks]
            self._queue_index = normalized_index
            self._active_track = track
            active_track_details = self._load_active_track_details()
            if isinstance(active_track_details.get("playback_context"), dict):
                active_track_details["playback_context"]["status"] = "Ready"
            sdk_playlist_source = self._load_playlist_source(
                object_name=object_name,
                details=details,
                source=playlist_source,
            )
            sdk_playlist_source["backend"] = "sdk"
            playlist_persisted, persistence_error = self._persist_playlist_state(
                status="stopped",
                source=sdk_playlist_source,
                backend="sdk",
            )
            result_details = dict(track)
            result_details.update(details)
            result_details.update(active_track_details)
            payload = self._load_result(
                object_name=object_name,
                status="Ready",
                playback_backend="sdk",
                sdk_prepare=True,
                playlist_persisted=playlist_persisted,
                playlist_persistence_error=persistence_error,
                **result_details,
            )
            payload["backend"] = "sdk"
            payload["now_playing"] = dict(payload)
            return payload

        manifest, error = self._load_manifest(track_id=track_id)
        if error:
            return self._load_result(object_name=object_name, error=error, **track, **details)
        stream_uri = str(manifest.get("uri") or "").strip()
        if not stream_uri:
            return self._load_result(object_name=object_name, error="api_manifest_uri_missing", **track, **details)
        track["stream_uri"] = stream_uri
        track["media_product_type"] = "track"
        track["manifest"] = dict(manifest.get("manifest") or {})
        for manifest_key in (
            "stream_quality",
            "audio_mode",
            "quality_resolution",
            "bitrate_kbps",
            "sample_rate_khz",
            "bit_depth",
            "channel_count",
            "codec",
            "track_presentation",
        ):
            manifest_value = manifest.get(manifest_key)
            if manifest_value is not None:
                track[manifest_key] = manifest_value
        if manifest.get("uri"):
            track["manifest_uri"] = stream_uri

        player_path = shutil.which(self.player_binary)
        if not player_path:
            return self._load_result(object_name=object_name, error="api_player_missing", **track, **details)

        self._stop_process()
        try:
            self._process = subprocess.Popen(
                [
                    player_path,
                    "--intf",
                    "dummy",
                    "--no-video",
                    "--play-and-exit",
                    "--quiet",
                    stream_uri,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self._process = None
            return self._load_result(object_name=object_name, error="api_player_start_failed", **track, **details)

        self._queue = [dict(item) for item in tracks]
        self._queue_index = normalized_index
        self._active_track = track
        active_track_details = self._load_active_track_details()
        playlist_source = self._load_playlist_source(
            object_name=object_name,
            details=details,
            source=playlist_source,
        )
        playlist_persisted, persistence_error = self._persist_playlist_state(
            status="playing",
            source=playlist_source,
        )
        result_details = dict(track)
        result_details.update(details)
        result_details.update(active_track_details)
        payload = self._load_result(
            object_name=object_name,
            status="Playing",
            playlist_persisted=playlist_persisted,
            playlist_persistence_error=persistence_error,
            **result_details,
        )
        payload["now_playing"] = dict(payload)
        return payload

    def _load_playlist_source(
        self,
        *,
        object_name: str,
        details: dict[str, Any],
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(source, dict):
            normalized_source = dict(source)
        else:
            normalized_source = {"object_name": object_name}
            for key in ("query", "target_url", "section", "playlist_id", "track_id"):
                value = details.get(key)
                if value is not None and str(value).strip():
                    normalized_source[key] = value
        normalized_source.setdefault("backend", "api")
        return normalized_source

    def _persist_playlist_state(
        self,
        *,
        status: str,
        source: dict[str, Any] | None = None,
        backend: str = "api",
    ) -> tuple[bool, str]:
        if source is None:
            source = dict(self._playlist.source)
        try:
            self._playlist.replace_tracks(
                self._queue,
                active_index=self._queue_index,
                source=source,
                backend=backend,
                status=status,
            )
            self.playlist_repository.save_playlist(self._playlist)
        except Exception as exc:
            self._playlist_persistence_error = f"playlist_persistence_failed:{type(exc).__name__}"
            return False, self._playlist_persistence_error
        self._playlist_persistence_error = ""
        return True, ""

    def _hydrate_track_contexts(
        self,
        *,
        tracks: list[dict[str, Any]],
        arguments: dict[str, Any],
        indices: list[int],
    ) -> list[dict[str, Any]]:
        hydrated_tracks = [dict(track) for track in tracks]
        for index in dict.fromkeys(indices):
            if index < 0 or index >= len(hydrated_tracks):
                continue
            track = hydrated_tracks[index]
            has_artist_context = bool(track.get("artist") or track.get("artist_info") or track.get("artists"))
            has_album_context = bool(track.get("album") or track.get("album_info"))
            if has_artist_context and has_album_context:
                continue
            track_id = str(track.get("tidal_track_id") or track.get("id") or "").strip()
            if not track_id:
                continue
            detailed_track, error = self._load_track(track_id=track_id, arguments=arguments)
            if error or not detailed_track:
                continue
            if "liked" in track:
                detailed_track["liked"] = track["liked"]
            hydrated_tracks[index] = detailed_track
        return hydrated_tracks

    def _load_search_tracks(self, *, query: str, arguments: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        document, error = self._load_api_document(
            object_name="tidal_api_search",
            arguments={
                "query": query,
                "country_code": arguments.get("country_code", "DE"),
                "include": arguments.get(
                    "include",
                    "tracks,tracks.artists,tracks.albums,tracks.albums.coverArt,topHits",
                ),
                "headers": arguments.get("headers"),
                "timeout_s": arguments.get("timeout_s"),
            },
        )
        if error:
            return [], error
        return self._load_tracks_from_search_document(document), ""

    def _load_track(self, *, track_id: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        document, error = self._load_api_document(
            object_name="tidal_api_track",
            arguments={
                "track_id": track_id,
                "country_code": arguments.get("country_code", "DE"),
                "include": arguments.get("track_include", "albums,artists,albums.coverArt"),
                "headers": arguments.get("headers"),
                "timeout_s": arguments.get("timeout_s"),
            },
        )
        if error:
            return {}, error
        resource = document.get("data") if isinstance(document.get("data"), dict) else {}
        track = self._load_track_from_resource(resource=resource, included=document.get("included"))
        if not track.get("tidal_track_id"):
            return {}, "api_track_not_found"
        return track, ""

    def _load_collection_tracks(
        self,
        *,
        collection_kind: str,
        collection_id: str,
        arguments: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str]:
        collection_url = f"https://openapi.tidal.com/v2/{quote(collection_kind, safe='')}/{quote(collection_id, safe='')}"
        document, error = self._load_api_document(
            object_name="tidal_api_request",
            arguments={
                "url": collection_url,
                "query": {"include": "items"},
                "headers": arguments.get("headers"),
                "timeout_s": arguments.get("timeout_s"),
            },
        )
        if error:
            return [], error

        primary_resource = document.get("data") if isinstance(document.get("data"), dict) else {}
        included_resources = self._load_included_resource_map(document.get("included"))
        relationship = (primary_resource.get("relationships") or {}).get("items")
        item_references = relationship.get("data") if isinstance(relationship, dict) else []
        tracks: list[dict[str, Any]] = []
        if isinstance(item_references, list):
            for reference in item_references:
                if not isinstance(reference, dict) or str(reference.get("type") or "") != "tracks":
                    continue
                resource_key = self._load_resource_key(reference)
                resource = included_resources.get(resource_key) or reference
                track = self._load_track_from_resource(resource=resource, included=included_resources.values())
                if track.get("tidal_track_id"):
                    if collection_kind == "userCollectionTracks":
                        track["liked"] = True
                    tracks.append(track)
        return tracks, "" if tracks else "api_collection_no_tracks"

    def _load_manifest(self, *, track_id: str) -> tuple[dict[str, Any], str]:
        document, error = self._load_api_document(
            object_name="tidal_api_track_manifest",
            arguments={
                "track_id": track_id,
                "formats": os.getenv("TIDAL_API_MANIFEST_FORMATS") or "AACLC",
                "manifest_type": os.getenv("TIDAL_API_MANIFEST_TYPE") or "HLS",
                "uri_scheme": "HTTPS",
                "usage": "PLAYBACK",
            },
        )
        if error:
            return {}, error

        resource = document.get("data") if isinstance(document.get("data"), dict) else {}
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        drm_data = attributes.get("drmData")
        if isinstance(drm_data, dict) and drm_data:
            drm_system = str(drm_data.get("drmSystem") or "UNKNOWN").strip() or "UNKNOWN"
            return {}, f"api_playback_drm_required:{drm_system}"

        manifest_text = json.dumps(attributes, ensure_ascii=True, sort_keys=True)
        stream_quality = self._load_manifest_text_value(
            attributes,
            "audioQuality",
            "audio_quality",
            "streamQuality",
            "stream_quality",
            "quality",
            "qualityLabel",
        )
        audio_mode = self._load_manifest_text_value(
            attributes,
            "audioMode",
            "audio_mode",
        )
        if not audio_mode:
            audio_mode = load_audio_mode_from_text(manifest_text)

        quality_resolution = self._load_manifest_text_value(
            attributes,
            "qualityResolution",
            "quality_resolution",
            "audioResolution",
            "audio_resolution",
            "resolution",
        )
        if not quality_resolution:
            quality_resolution = load_quality_resolution(manifest_text)

        bitrate_kbps = self._load_manifest_number_value(
            attributes,
            "bitrate",
            "bit_rate",
            "bitRate",
            "audioBitrate",
            "audio_bitrate",
            "bitrateKbps",
            "bitrate_kbps",
        )
        sample_rate_value = self._load_manifest_number_value(
            attributes,
            "sampleRate",
            "sample_rate",
            "samplingRate",
            "sampleRateHz",
            "sample_rate_hz",
            "sampleRateKHz",
            "sample_rate_khz",
        )
        sample_rate_khz: int | float | None = None
        if sample_rate_value is not None:
            sample_rate_numeric = float(sample_rate_value)
            sample_rate_khz = sample_rate_numeric / 1000.0 if sample_rate_numeric >= 1000 else sample_rate_numeric
        bit_depth = self._load_manifest_number_value(
            attributes,
            "bitDepth",
            "bit_depth",
            "bitsPerSample",
            "bits_per_sample",
        )
        channel_count = self._load_manifest_number_value(
            attributes,
            "channelCount",
            "channel_count",
            "channels",
            "audioChannels",
            "audio_channels",
        )
        codec = self._load_manifest_text_value(
            attributes,
            "codec",
            "audioCodec",
            "audio_codec",
            "mimeType",
            "mime_type",
        )

        if not quality_resolution:
            resolution_parts: list[str] = []
            if bit_depth is not None:
                resolution_parts.append(f"{bit_depth:g}-bit")
            if sample_rate_khz is not None:
                resolution_parts.append(f"{sample_rate_khz:g} kHz")
            if resolution_parts:
                quality_resolution = " ".join(resolution_parts)

        return {
            "uri": str(attributes.get("uri") or "").strip(),
            "formats": attributes.get("formats") or [],
            "track_presentation": str(attributes.get("trackPresentation") or "").strip(),
            "manifest": dict(attributes),
            **load_tidal_dsp_metadata(attributes),
            "stream_quality": stream_quality,
            "audio_mode": audio_mode,
            "quality_resolution": quality_resolution,
            "bitrate_kbps": bitrate_kbps,
            "sample_rate_khz": sample_rate_khz,
            "bit_depth": bit_depth,
            "channel_count": channel_count,
            "codec": codec,
        }, ""

    def _load_api_response(self, *, object_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        try:
            request_object = self.tidal_api_service.load_object_request(
                object_name=object_name,
                arguments=arguments,
            )
        except ValueError as exc:
            return {}, str(exc)

        response_payload = self.tidal_api_service.process_object(request_object=request_object)
        if not response_payload.get("ok"):
            status_code = int(response_payload.get("status_code") or 0)
            if status_code:
                return response_payload, f"api_http_{status_code}"
            response_error = str(response_payload.get("error") or "").strip()
            return response_payload, f"api_request_failed:{response_error}" if response_error else "api_request_failed"
        return response_payload, ""

    def _load_api_document(self, *, object_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
        response_payload, error = self._load_api_response(object_name=object_name, arguments=arguments)
        if error:
            return {}, error

        try:
            document = json.loads(str(response_payload.get("response_body") or "{}"))
        except Exception:
            return {}, "api_response_not_json"
        if not isinstance(document, dict):
            return {}, "api_response_not_object"
        return document, ""

    def _load_tracks_from_search_document(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        included_resources = self._load_included_resource_map(document.get("included"))
        primary_resource = document.get("data") if isinstance(document.get("data"), dict) else {}
        relationships = primary_resource.get("relationships") if isinstance(primary_resource, dict) else {}
        track_references = relationships.get("tracks", {}).get("data", []) if isinstance(relationships, dict) else []
        if not isinstance(track_references, list):
            track_references = []

        tracks: list[dict[str, Any]] = []
        seen_track_ids: set[str] = set()
        for reference in track_references:
            if not isinstance(reference, dict) or str(reference.get("type") or "") != "tracks":
                continue
            resource = included_resources.get(self._load_resource_key(reference)) or reference
            track = self._load_track_from_resource(resource=resource, included=included_resources.values())
            track_id = str(track.get("tidal_track_id") or "").strip()
            if track_id and track_id not in seen_track_ids:
                seen_track_ids.add(track_id)
                tracks.append(track)

        if tracks:
            return tracks
        for resource in included_resources.values():
            if str(resource.get("type") or "") != "tracks":
                continue
            track = self._load_track_from_resource(resource=resource, included=included_resources.values())
            track_id = str(track.get("tidal_track_id") or "").strip()
            if track_id and track_id not in seen_track_ids:
                seen_track_ids.add(track_id)
                tracks.append(track)
        return tracks

    def _load_track_from_resource(self, *, resource: dict[str, Any], included: Any) -> dict[str, Any]:
        if not isinstance(resource, dict) or str(resource.get("type") or "") != "tracks":
            return {}
        track_id = str(resource.get("id") or "").strip()
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        included_resources = self._load_included_resource_map(included)
        artist_resources = self._load_related_resources(
            resource=resource,
            relationship_name="artists",
            included=included_resources,
        )
        album_resource = self._load_related_resource(resource=resource, relationship_name="albums", included=included_resources)
        artist_info = [
            artist_summary
            for artist_summary in (
                self._load_resource_summary(resource=artist_resource)
                for artist_resource in artist_resources
            )
            if artist_summary
        ]
        album_info = self._load_album_info(resource=album_resource, included=included_resources)
        album_art_url = str(album_info.get("art_url") or "").strip()
        if not album_art_url:
            album_art_url = self._load_artwork_url_from_resource(resource=album_resource, included=included_resources)
        track_info = self._load_resource_summary(resource=resource)
        bpm = self._load_manifest_number_value(
            attributes,
            "bpm",
            "BPM",
            "tempo",
            "beatsPerMinute",
            "beats_per_minute",
        )
        track_number = self._load_manifest_number_value(attributes, "trackNumber", "track_number", "number")
        volume_number = self._load_manifest_number_value(attributes, "volumeNumber", "volume_number", "volume")
        release_date = self._load_manifest_text_value(attributes, "releaseDate", "release_date")
        version = self._load_manifest_text_value(attributes, "version", "trackVersion", "track_version")
        isrc = self._load_manifest_text_value(attributes, "isrc", "ISRC")
        copyright_value = attributes.get("copyright")
        if isinstance(copyright_value, dict):
            copyright_text = self._load_manifest_text_value(copyright_value, "text", "value")
        else:
            copyright_text = self._load_manifest_text_value(attributes, "copyrightText", "copyright_text", "copyright")
        duration_value = str(attributes.get("duration") or "").strip()
        track_payload = {
            "tidal_track_id": track_id,
            "title": str(attributes.get("title") or "").strip(),
            "artist": self._load_related_name(artist_resources[0] if artist_resources else {}),
            "artist_info": artist_info,
            "artists": artist_info,
            "album": self._load_related_name(album_resource),
            "album_info": album_info,
            "track_info": track_info,
            "track_url": f"https://listen.tidal.com/track/{quote(track_id, safe='')}" if track_id else "",
            "duration": duration_value,
            "explicit": bool(attributes.get("explicit")),
            "media_tags": list(attributes.get("mediaTags") or []) if isinstance(attributes.get("mediaTags"), list) else [],
            "tonart": self._load_track_key(attributes=attributes),
            "art_url": album_art_url,
            "album_art_url": album_art_url,
        }
        duration_seconds = self._load_duration_seconds(duration_value)
        if duration_seconds is not None:
            track_payload["duration_seconds"] = duration_seconds
        for output_key, attribute_key in (
            ("external_links", "externalLinks"),
            ("availability", "availability"),
            ("access_type", "accessType"),
            ("popularity", "popularity"),
            ("created_at", "createdAt"),
        ):
            attribute_value = attributes.get(attribute_key)
            if attribute_value is not None and attribute_value != "":
                track_payload[output_key] = attribute_value
        if bpm is not None:
            track_payload["bpm"] = bpm
        if track_number is not None:
            track_payload["track_number"] = track_number
        if volume_number is not None:
            track_payload["volume_number"] = volume_number
        if release_date:
            track_payload["release_date"] = release_date
        if version:
            track_payload["version"] = version
        if isrc:
            track_payload["isrc"] = isrc
        if copyright_text:
            track_payload["copyright"] = copyright_text
        return track_payload

    def _load_related_resources(
        self,
        *,
        resource: dict[str, Any],
        relationship_name: str,
        included: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        relationships = resource.get("relationships") if isinstance(resource.get("relationships"), dict) else {}
        relationship = relationships.get(relationship_name)
        relationship_data = relationship.get("data") if isinstance(relationship, dict) else None
        if isinstance(relationship_data, dict):
            relationship_items = [relationship_data]
        elif isinstance(relationship_data, list):
            relationship_items = list(relationship_data)
        else:
            relationship_items = []

        related_resources: list[dict[str, Any]] = []
        for relationship_item in relationship_items:
            if not isinstance(relationship_item, dict):
                continue
            related_resource = included.get(self._load_resource_key(relationship_item)) or relationship_item
            if isinstance(related_resource, dict):
                related_resources.append(dict(related_resource))
        return related_resources

    def _load_resource_summary(self, *, resource: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(resource, dict) or not resource:
            return {}

        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        summary: dict[str, Any] = {
            "resource_type": str(resource.get("type") or "").strip(),
            "resource_id": str(resource.get("id") or "").strip(),
        }
        for key in ("name", "title"):
            text_value = str(attributes.get(key) or "").strip()
            if text_value:
                summary["name"] = text_value
                break
        else:
            summary["name"] = ""

        for key in ("url", "href", "src"):
            text_value = str(attributes.get(key) or "").strip()
            if text_value:
                summary["url"] = text_value
                break

        resource_type = str(summary.get("resource_type") or "").strip()
        resource_id = str(summary.get("resource_id") or "").strip()
        if "url" not in summary and resource_id and resource_type in {"tracks", "albums", "artists", "playlists"}:
            summary["url"] = f"https://listen.tidal.com/{resource_type.rstrip('s')}/{quote(resource_id, safe='')}"

        for output_key, attribute_keys in (
            ("release_date", ("releaseDate", "release_date")),
            ("number_of_tracks", ("numberOfTracks", "number_of_tracks")),
            ("number_of_items", ("numberOfItems", "number_of_items")),
            ("number_of_volumes", ("numberOfVolumes", "number_of_volumes")),
            ("track_count", ("trackCount", "track_count")),
            ("duration", ("duration",)),
            ("copyright", ("copyright",)),
            ("external_links", ("externalLinks", "external_links")),
            ("availability", ("availability",)),
            ("access_type", ("accessType", "access_type")),
            ("popularity", ("popularity",)),
            ("created_at", ("createdAt", "created_at")),
            ("album_type", ("albumType", "album_type")),
            ("owner_type", ("ownerType", "owner_type")),
        ):
            for attribute_key in attribute_keys:
                attribute_value = attributes.get(attribute_key)
                if attribute_value is not None and attribute_value != "":
                    if output_key == "copyright" and isinstance(attribute_value, dict):
                        attribute_value = attribute_value.get("text") or attribute_value.get("value") or ""
                    summary[output_key] = attribute_value
                    break

        for key in ("art_url", "artUrl", "cover_url", "coverUrl", "image_url", "imageUrl", "thumbnail_url", "thumbnailUrl"):
            text_value = str(attributes.get(key) or "").strip()
            if text_value:
                summary["art_url"] = text_value
                break

        files_value = attributes.get("files")
        if "art_url" not in summary and isinstance(files_value, list):
            for file_value in files_value:
                if not isinstance(file_value, dict):
                    continue
                candidate = str(file_value.get("href") or file_value.get("url") or "").strip()
                if candidate.startswith(("http://", "https://")):
                    summary["art_url"] = candidate
                    break

        return {key: value for key, value in summary.items() if value is not None and value != ""}

    def _load_album_info(self, *, resource: dict[str, Any], included: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(resource, dict) or not resource:
            return {}

        album_info = self._load_resource_summary(resource=resource)
        album_art_url = self._load_artwork_url_from_resource(resource=resource, included=included)
        if album_art_url:
            album_info["art_url"] = album_art_url
        return album_info

    def _load_artwork_url_from_resource(self, *, resource: dict[str, Any], included: dict[str, dict[str, Any]]) -> str:
        if not isinstance(resource, dict) or not resource:
            return ""

        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        for key in (
            "art_url",
            "artUrl",
            "cover_url",
            "coverUrl",
            "image_url",
            "imageUrl",
            "thumbnail_url",
            "thumbnailUrl",
            "url",
            "href",
            "src",
        ):
            candidate = str(attributes.get(key) or resource.get(key) or "").strip()
            if candidate.startswith("http://") or candidate.startswith("https://"):
                return candidate

        files_value = attributes.get("files")
        if isinstance(files_value, list):
            for file_value in files_value:
                if not isinstance(file_value, dict):
                    continue
                candidate = str(file_value.get("href") or file_value.get("url") or "").strip()
                if candidate.startswith(("http://", "https://")):
                    return candidate

        for relationship_name in ("coverArt", "artwork", "artworks", "thumbnailArt", "profileArt"):
            related_resources = self._load_related_resources(
                resource=resource,
                relationship_name=relationship_name,
                included=included,
            )
            for related_resource in related_resources:
                artwork_url = self._load_artwork_url_from_resource(resource=related_resource, included=included)
                if artwork_url:
                    return artwork_url
        return ""

    def _load_manifest_text_value(self, attributes: dict[str, Any], *keys: str) -> str:
        for key in keys:
            candidate = str(attributes.get(key) or "").strip()
            if candidate:
                return candidate
        return ""

    def _load_manifest_number_value(self, attributes: dict[str, Any], *keys: str) -> int | float | None:
        for key in keys:
            raw_value = attributes.get(key)
            if raw_value is None or isinstance(raw_value, bool):
                continue
            if isinstance(raw_value, (int, float)):
                return raw_value
            text_value = str(raw_value).strip()
            if not text_value:
                continue
            try:
                number_value = float(text_value)
            except Exception:
                continue
            return int(number_value) if number_value.is_integer() else number_value
        return None

    def _load_duration_seconds(self, raw_value: Any) -> int | float | None:
        text_value = str(raw_value or "").strip()
        if not text_value:
            return None
        try:
            numeric_value = float(text_value)
        except Exception:
            numeric_value = None
        if numeric_value is not None and math.isfinite(numeric_value):
            return int(numeric_value) if numeric_value.is_integer() else numeric_value

        match = re.fullmatch(
            r"P(?:([0-9]+)D)?T?(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+(?:\.[0-9]+)?)S)?",
            text_value,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        days, hours, minutes, seconds = (float(value or 0) for value in match.groups())
        total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds
        return int(total_seconds) if total_seconds.is_integer() else total_seconds

    def _load_track_key(self, *, attributes: dict[str, Any]) -> str:
        key_value = str(attributes.get("key") or "").strip()
        scale_value = str(attributes.get("keyScale") or "").strip()
        if not key_value or key_value == "UNKNOWN":
            return ""
        return f"{key_value} {scale_value}".strip()

    def _load_related_resource(
        self,
        *,
        resource: dict[str, Any],
        relationship_name: str,
        included: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        relationships = resource.get("relationships") if isinstance(resource.get("relationships"), dict) else {}
        relationship = relationships.get(relationship_name)
        relationship_data = relationship.get("data") if isinstance(relationship, dict) else None
        if isinstance(relationship_data, list):
            relationship_data = relationship_data[0] if relationship_data else None
        if not isinstance(relationship_data, dict):
            return {}
        return included.get(self._load_resource_key(relationship_data), relationship_data)

    def _load_related_name(self, resource: dict[str, Any]) -> str:
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        return str(attributes.get("name") or attributes.get("title") or "").strip()

    def _load_included_resource_map(self, included: Any) -> dict[str, dict[str, Any]]:
        if isinstance(included, dict):
            resources = list(included.values())
        elif isinstance(included, (list, tuple, set)):
            resources = list(included)
        elif hasattr(included, "__iter__") and not isinstance(included, (str, bytes)):
            resources = list(included)
        else:
            resources = []
        return {
            self._load_resource_key(resource): dict(resource)
            for resource in resources
            if isinstance(resource, dict) and self._load_resource_key(resource)
        }

    def _load_resource_key(self, resource: dict[str, Any]) -> str:
        return f"{str(resource.get('type') or '').strip()}:{str(resource.get('id') or '').strip()}"

    def _load_target_kind(self, target_url: str) -> str:
        path_parts = [part for part in urlparse(target_url).path.split("/") if part]
        for target_kind in ("track", "album", "playlist"):
            if target_kind in path_parts:
                return target_kind
        return ""

    def _load_bounded_int(self, raw_value: Any, *, default_value: int, minimum: int, maximum: int) -> int:
        try:
            normalized_value = int(raw_value)
        except Exception:
            normalized_value = int(default_value)
        return max(minimum, min(maximum, normalized_value))

    def _load_validated_tidal_target_url(self, raw_url: Any) -> str:
        parsed_url = urlparse(str(raw_url or "").strip())
        if parsed_url.scheme not in {"http", "https"}:
            return ""
        hostname = (parsed_url.hostname or "").casefold()
        if hostname != "tidal.com" and not hostname.endswith(".tidal.com"):
            return ""
        return parsed_url.geturl()

    def _load_target_id(self, target_url: str, *, target_kind: str) -> str:
        path_parts = [part for part in urlparse(target_url).path.split("/") if part]
        try:
            target_index = path_parts.index(target_kind)
        except ValueError:
            return ""
        if target_index + 1 >= len(path_parts):
            return ""
        return str(path_parts[target_index + 1]).strip()

    def _is_process_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _load_boolean_argument(self, raw_value: Any, *, default_value: bool) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if raw_value is None:
            return default_value

        normalized_value = str(raw_value).strip().casefold()
        if normalized_value in {"1", "true", "yes", "on"}:
            return True
        if normalized_value in {"0", "false", "no", "off"}:
            return False
        return default_value

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def _load_active_track_details(self) -> dict[str, Any]:
        details = dict(self._active_track)
        details.pop("stream_uri", None)
        persisted_backend = str(self._playlist.backend or "").strip().casefold()
        if persisted_backend in {"api", "sdk"}:
            details["backend"] = persisted_backend
        current_media_product = self._load_media_product_snapshot(details)
        details["media_product"] = current_media_product
        next_media_product = self._load_next_media_product_snapshot()
        if next_media_product:
            details["next_media_product"] = next_media_product
        details["playback_context"] = self._load_playback_context()
        return details

    def _load_playback_context(self) -> dict[str, Any]:
        return {
            "queue_index": self._queue_index,
            "queue_length": len(self._queue),
            "has_previous": self._queue_index > 0,
            "has_next": self._queue_index + 1 < len(self._queue),
            "status": "Playing" if self._is_process_running() else "Stopped",
        }

    def _load_media_product_snapshot(self, track: dict[str, Any]) -> dict[str, Any]:
        snapshot = {
            key: value
            for key, value in dict(track).items()
            if key not in {"media_product", "next_media_product", "playback_context"} and value is not None
        }
        snapshot.setdefault("media_product_type", "track")
        return snapshot

    def _load_next_media_product_snapshot(self) -> dict[str, Any]:
        if not self._queue or self._queue_index + 1 >= len(self._queue):
            return {}
        next_track = dict(self._queue[self._queue_index + 1])
        next_track.pop("stream_uri", None)
        return self._load_media_product_snapshot(next_track)

    def _load_playerctl_selected_player(self, *, player_selector: str) -> str:
        try:
            players_result = subprocess.run(
                ["playerctl", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            return ""

        players = [line.strip() for line in str(players_result.stdout or "").splitlines() if line.strip()]
        if not players:
            return ""
        return load_selected_player_name(
            players=players,
            player_selector=player_selector,
            api_prefixes=self._API_PLAYER_PREFIXES,
        )

    def _run_playerctl_command(
        self,
        *,
        player_selector: str,
        command_name: str,
        command_arguments: list[str] | None = None,
        timeout_s: int = 5,
    ) -> tuple[str, str]:
        selected_player = self._load_playerctl_selected_player(player_selector=player_selector)
        if not selected_player:
            return "", "playerctl_no_players"

        command = ["playerctl", "-p", selected_player, command_name]
        if command_arguments:
            command.extend(command_arguments)
        try:
            command_result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except Exception as exc:
            return selected_player, f"playerctl_exception:{type(exc).__name__}"

        if command_result.returncode != 0:
            stderr_text = str(command_result.stderr or "").strip()
            return selected_player, stderr_text or f"playerctl_command_failed:{command_name}"

        return selected_player, str(command_result.stdout or "").strip()

    def _load_playerctl_status(self, *, player_selector: str) -> str:
        selected_player, status_output = self._run_playerctl_command(
            player_selector=player_selector,
            command_name="status",
            timeout_s=3,
        )
        if not selected_player:
            return "unknown"
        normalized_status = str(status_output or "").strip()
        return normalized_status or "unknown"

    def _load_result(self, *, object_name: str, error: str | None = None, **details: Any) -> dict[str, Any]:
        normalized_error = str(error or "").strip()
        metadata = {key: value for key, value in details.items() if value is not None}
        backend_value = str(metadata.get("playback_backend") or metadata.get("backend") or "api").strip() or "api"
        stdout_lines = [f"backend={backend_value}"]
        if normalized_error:
            stdout_lines.insert(0, f"error={normalized_error}")
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)):
                safe_value = str(value).replace("\n", " ").replace("\r", " ").replace("=", "-").strip()
                stdout_lines.append(f"{key}={safe_value}")
        payload: dict[str, Any] = {
            "object_name": object_name,
            "ok": not normalized_error,
            "return_code": 0 if not normalized_error else 1,
            "stdout": "\n".join(stdout_lines),
            "stderr": "" if not normalized_error else normalized_error,
            "command": "tidal_api_playback",
            "backend": "api",
        }
        if normalized_error:
            payload["error"] = normalized_error
        payload.update(metadata)
        return payload


class WebPlayerService:
    _PITCH_CLASS_BY_NOTE = {
        "C": 0,
        "B#": 0,
        "C#": 1,
        "DB": 1,
        "D": 2,
        "D#": 3,
        "EB": 3,
        "E": 4,
        "FB": 4,
        "F": 5,
        "E#": 5,
        "F#": 6,
        "GB": 6,
        "G": 7,
        "G#": 8,
        "AB": 8,
        "A": 9,
        "A#": 10,
        "BB": 10,
        "B": 11,
        "CB": 11,
    }
    _PITCH_CLASS_TO_SHARP_NOTE = {
        0: "C",
        1: "C#",
        2: "D",
        3: "D#",
        4: "E",
        5: "F",
        6: "F#",
        7: "G",
        8: "G#",
        9: "A",
        10: "A#",
        11: "B",
    }
    _CAMELOT_MAJOR_BY_PITCH_CLASS = {
        0: 8,
        7: 9,
        2: 10,
        9: 11,
        4: 12,
        11: 1,
        6: 2,
        1: 3,
        8: 4,
        3: 5,
        10: 6,
        5: 7,
    }
    _CAMELOT_MINOR_BY_PITCH_CLASS = {
        9: 8,
        4: 9,
        11: 10,
        6: 11,
        1: 12,
        8: 1,
        3: 2,
        10: 3,
        5: 4,
        0: 5,
        7: 6,
        2: 7,
    }
    _PITCH_CLASS_BY_CAMELOT_MAJOR = {
        1: 11,
        2: 6,
        3: 1,
        4: 8,
        5: 3,
        6: 10,
        7: 5,
        8: 0,
        9: 7,
        10: 2,
        11: 9,
        12: 4,
    }
    _PITCH_CLASS_BY_CAMELOT_MINOR = {
        1: 8,
        2: 3,
        3: 10,
        4: 5,
        5: 0,
        6: 7,
        7: 2,
        8: 9,
        9: 4,
        10: 11,
        11: 6,
        12: 1,
    }
    _MINOR_MODE_TOKENS = {"m", "moll", "minor", "min"}
    _MAJOR_MODE_TOKENS = {"dur", "major", "maj"}

    _LIBRARY_SECTION_URLS = {
        "favorites_tracks": "https://listen.tidal.com/browse/favorites/tracks",
        "favorites_albums": "https://listen.tidal.com/browse/favorites/albums",
        "favorites_playlists": "https://listen.tidal.com/browse/favorites/playlists",
        "my_collection_tracks": "https://tidal.com/my-collection/tracks",
        "history": "https://listen.tidal.com/browse/history",
        "home": "https://listen.tidal.com/",
    }
    _PLAYBACK_METADATA_ACTIONS = {
        "webplayer_play",
        "webplayer_pause",
        "webplayer_forward",
        "webplayer_backward",
        "webplayer_get_media_product",
        "webplayer_get_next_media_product",
        "webplayer_seek",
        "webplayer_like_current",
        "webplayer_toggle_like_current",
        "webplayer_now_playing",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
        "webplayer_volume_set",
        "webplayer_volume_adjust",
        "webplayer_system_volume_set",
        "webplayer_system_volume_adjust",
    }
    _LONG_RUNNING_ACTIONS = {
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
        "webplayer_library_snapshot",
    }

    _SUPPORTED_ACTIONS = {
        "webplayer_play",
        "webplayer_pause",
        "webplayer_stop",
        "webplayer_forward",
        "webplayer_backward",
        "webplayer_get_media_product",
        "webplayer_get_next_media_product",
        "webplayer_seek",
        "webplayer_like_current",
        "webplayer_toggle_like_current",
        "webplayer_now_playing",
        "webplayer_search",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_library_snapshot",
        "webplayer_open_playback_target",
        "webplayer_volume_set",
        "webplayer_volume_adjust",
        "webplayer_system_volume_set",
        "webplayer_system_volume_adjust",
    }

    def __init__(
        self,
        command_service: LocalCommandService | None = None,
        tidal_api_service: TidalApiService | None = None,
        playback_backend: str | None = None,
        browser_backend_enabled: bool | None = None,
    ) -> None:
        self.command_service = command_service or LocalCommandService()
        self.library_snapshot_plugin = WPlibSnaPlugin()
        self.tidal_api_service = tidal_api_service or TidalApiService()
        self._browser_api_metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        _ = playback_backend
        self.playback_backend = "browser"
        self.browser_backend_enabled = self._load_boolean_argument(
            browser_backend_enabled
            if browser_backend_enabled is not None
            else os.getenv("WEBPLAYER_BROWSER_BACKEND_ENABLED"),
            default_value=True,
        )
        self._browser_controller_name = "wplctl"
        self._browser_queue_track_ids: list[str] = []
        self._browser_queue_index = -1

    def dispatch_object(
        self,
        *,
        object_name: str,
        query: str | None = None,
        player_selector: str = "chromium",
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_object_name = str(object_name or "").strip().lower()
        if normalized_object_name not in self._SUPPORTED_ACTIONS:
            return {
                "ok": False,
                "error": "unsupported_tool",
                "object_name": normalized_object_name,
                "supported_tools": sorted(self._SUPPORTED_ACTIONS),
            }

        argument_payload = dict(arguments or {})
        if query is not None and "query" not in argument_payload:
            argument_payload["query"] = query
        effective_query = str(argument_payload.get("query") or "").strip() if "query" in argument_payload else query

        requested_backend = str(
            argument_payload.get("playback_backend") or self.playback_backend
        ).strip().casefold() or "browser"
        requested_backend_was = requested_backend
        if requested_backend != "browser":
            requested_backend = "browser"
            argument_payload["playback_backend"] = "browser"
            argument_payload["requested_playback_backend"] = requested_backend_was

        if not self.browser_backend_enabled:
            return {
                "ok": False,
                "error": "browser_backend_disabled",
                "stderr": "browser_backend_disabled",
                "object_name": normalized_object_name,
                "backend": "browser",
                "playback_backend": "browser",
                "requested_playback_backend": requested_backend_was,
                "player_selector": "chromium",
            }

        if requested_backend == "browser":
            effective_player_selector = str(player_selector or "chromium").strip() or "chromium"
            if effective_player_selector.casefold() in {"tidal-api", "tidal-sdk", "api", "sdk"}:
                effective_player_selector = "chromium"
            payload = self._dispatch_browser_object(
                object_name=normalized_object_name,
                query=effective_query,
                player_selector=effective_player_selector,
                arguments=argument_payload,
            )
            if requested_backend_was != "browser":
                payload["requested_playback_backend"] = requested_backend_was
                payload["playback_backend_forced"] = True
            return payload

        return {
            "ok": False,
            "error": "unsupported_playback_backend",
            "stderr": "unsupported_playback_backend",
            "object_name": normalized_object_name,
            "backend": "browser",
            "playback_backend": "browser",
            "requested_playback_backend": requested_backend_was,
            "player_selector": "chromium",
        }

    def _dispatch_browser_object(
        self,
        *,
        object_name: str,
        query: str | None,
        player_selector: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            command = self.load_object_command(
                object_name=object_name,
                query=query,
                player_selector=player_selector,
                arguments=arguments,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": f"browser_backend_failed:{exc}",
                "object_name": object_name,
                "backend": "browser",
                "playback_backend": "browser",
                "player_selector": player_selector,
            }

        default_timeout_s = 90 if object_name in self._LONG_RUNNING_ACTIONS else 20
        command_timeout_s = self._load_bounded_int(
            arguments.get("command_timeout_s"),
            default_value=default_timeout_s,
            minimum=1,
            maximum=180,
        )
        command_result = self.command_service.run_object(
            object_name=object_name,
            command=command,
            command_timeout_s=command_timeout_s,
        )
        payload = command_result.to_payload()
        payload["backend"] = "browser"
        payload["playback_backend"] = "browser"
        payload["player_selector"] = player_selector
        payload["controller"] = self._browser_controller_name
        if object_name == "webplayer_library_snapshot":
            payload.update(self.library_snapshot_plugin.load_result_payload(command_result.stdout))
        if object_name in self._PLAYBACK_METADATA_ACTIONS:
            metadata_stdout = command_result.stdout
            if object_name in {"webplayer_now_playing", "webplayer_get_media_product"}:
                if metadata_stdout and not metadata_stdout.endswith("\n"):
                    metadata_stdout += "\n"
                metadata_stdout += self._load_audio_route_metadata_stdout()
            enrich_api_metadata = object_name in {"webplayer_now_playing", "webplayer_get_media_product"}
            now_playing = self.load_object_metadata(
                metadata_stdout,
                enrich_api_metadata=enrich_api_metadata
                and self._load_boolean_argument(arguments.get("api_metadata"), default_value=True),
                api_arguments=arguments,
            )
            if object_name == "webplayer_now_playing":
                track_id = str(now_playing.get("tidal_track_id") or "").strip()
            payload["now_playing"] = now_playing
        return payload

    def load_object_command(
        self,
        *,
        object_name: str,
        query: str | None,
        player_selector: str,
        arguments: dict[str, Any],
    ) -> str:
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=10,
            minimum=0,
            maximum=120,
        )
        play_attempts = self._load_bounded_int(
            arguments.get("play_attempts"),
            default_value=6,
            minimum=1,
            maximum=20,
        )

        if object_name == "webplayer_play":
            cdp_port = self._load_bounded_int(
                arguments.get("cdp_port"),
                default_value=9222,
                minimum=1,
                maximum=65535,
            )
            cdp_click_timeout_s = self._load_bounded_int(
                arguments.get("cdp_click_timeout_s"),
                default_value=12,
                minimum=1,
                maximum=120,
            )
            cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=False)
            return self._load_play_command(
                player_selector=player_selector,
                wait_for_player_s=wait_for_player_s,
                play_attempts=play_attempts,
                cdp_autoclick=cdp_autoclick,
                cdp_port=cdp_port,
                cdp_click_timeout_s=cdp_click_timeout_s,
            )
        if object_name == "webplayer_pause":
            return self._load_pause_command(player_selector=player_selector)
        if object_name == "webplayer_seek":
            seek_spec, _ = _load_seek_spec(
                position_seconds=arguments.get("position_seconds"),
                delta_seconds=arguments.get("delta_seconds"),
            )
            return self._load_seek_command(
                player_selector=player_selector,
                seek_spec=seek_spec,
            )
        if object_name == "webplayer_stop":
            return self._load_stop_command(player_selector=player_selector)
        if object_name == "webplayer_forward":
            return self._load_forward_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_backward":
            return self._load_backward_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_like_current":
            return self._load_like_current_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_toggle_like_current":
            return self._load_toggle_like_current_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_now_playing":
            return self._load_now_playing_command(player_selector=player_selector, arguments=arguments)
        if object_name in {"webplayer_get_media_product", "webplayer_get_next_media_product"}:
            metadata_arguments = dict(arguments)
            metadata_arguments.setdefault("cdp_probe_quality", False)
            return self._load_now_playing_command(
                player_selector=player_selector,
                arguments=metadata_arguments,
            )
        if object_name == "webplayer_search":
            return self._load_search_command(query=query)
        if object_name == "webplayer_search_play":
            return self._load_search_play_command(
                query=query,
                player_selector=player_selector,
                arguments=arguments,
            )
        if object_name == "webplayer_playlist_play":
            return self._load_playlist_play_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_library_play":
            return self._load_library_play_command(player_selector=player_selector, arguments=arguments)
        if object_name == self.library_snapshot_plugin.object_name:
            _ = player_selector
            return self.library_snapshot_plugin.load_object_command(
                arguments=arguments,
                library_section_urls=self._LIBRARY_SECTION_URLS,
                load_validated_tidal_url=self._load_validated_tidal_url,
                load_bounded_int=self._load_bounded_int,
                load_browser_open_command=self._load_browser_open_command,
            )
        if object_name == "webplayer_open_playback_target":
            return self._load_open_playback_target_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_volume_set":
            return self._load_volume_set_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_volume_adjust":
            return self._load_volume_adjust_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_system_volume_set":
            return self._load_system_volume_set_command(arguments=arguments)
        if object_name == "webplayer_system_volume_adjust":
            return self._load_system_volume_adjust_command(arguments=arguments)
        raise ValueError(f"Unsupported tool: {object_name}")

    def load_object_metadata(
        self,
        stdout_payload: str,
        *,
        enrich_api_metadata: bool = False,
        api_arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for line in str(stdout_payload or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            metadata[key.strip()] = value.strip()

        fallback_key_map = {
            "title": "cdp_title",
            "artist": "cdp_artist",
            "album": "cdp_album",
            "track_url": "cdp_track_url",
            "tidal_track_id": "cdp_tidal_track_id",
            "stream_quality": "cdp_stream_quality",
            "audio_mode": "cdp_audio_mode",
            "quality_resolution": "cdp_quality_resolution",
            "tonart": "cdp_tonart",
            "liked_state": "cdp_liked_state",
            "liked": "cdp_liked",
            "art_url": "cdp_art_url",
        }
        cdp_track_context_available = bool(str(metadata.get("cdp_tidal_track_id") or "").strip())
        for target_key, source_key in fallback_key_map.items():
            source_value = str(metadata.get(source_key) or "").strip()
            if target_key == "art_url" and source_value:
                metadata[target_key] = source_value
                continue
            if cdp_track_context_available and source_value:
                metadata[target_key] = source_value
                continue
            if str(metadata.get(target_key) or "").strip():
                continue
            if source_value:
                metadata[target_key] = source_value

        resolved_track_id = self._load_browser_tidal_track_id(metadata)
        if resolved_track_id:
            metadata["tidal_track_id"] = resolved_track_id
        if enrich_api_metadata:
            metadata.update(
                self._load_browser_api_metadata(
                    metadata,
                    arguments=api_arguments or {},
                )
            )

        sample_rate_value = metadata.get("sample_rate_khz")
        try:
            sample_rate_numeric = float(sample_rate_value)
        except (TypeError, ValueError):
            sample_rate_numeric = 0.0
        if sample_rate_numeric > 0:
            metadata["sample_rate_khz"] = sample_rate_numeric / 1000.0 if sample_rate_numeric >= 1000 else sample_rate_numeric

        bit_depth_value = metadata.get("bit_depth")
        try:
            bit_depth_numeric = int(float(bit_depth_value))
        except (TypeError, ValueError):
            bit_depth_numeric = 0
        if bit_depth_numeric > 0:
            metadata["bit_depth"] = bit_depth_numeric

        quality_source = " ".join(
            str(metadata.get(key) or "").strip()
            for key in ("quality_resolution", "stream_quality", "audio_mode")
        )
        bit_depth, sample_rate_khz = load_audio_quality_metrics(quality_source)
        if bit_depth is not None and not str(metadata.get("bit_depth") or "").strip():
            metadata["bit_depth"] = bit_depth
        if sample_rate_khz is not None and not str(metadata.get("sample_rate_khz") or "").strip():
            metadata["sample_rate_khz"] = sample_rate_khz

        tonart_candidates = (
            ("tonart", metadata.get("tonart")),
            ("initial_key", metadata.get("initial_key")),
            ("musical_key", metadata.get("musical_key")),
            ("key", metadata.get("key")),
        )
        tonart_metadata: dict[str, str] = {}
        for _, tonart_candidate in tonart_candidates:
            if not str(tonart_candidate or "").strip():
                continue
            tonart_metadata = self._load_tonart_metadata(str(tonart_candidate))
            if tonart_metadata:
                break
        if tonart_metadata:
            metadata.update(tonart_metadata)
        else:
            for tonart_key, tonart_candidate in tonart_candidates:
                if str(tonart_candidate or "").strip():
                    metadata.pop(tonart_key, None)
        return metadata

    def _load_browser_api_metadata(
        self,
        metadata: dict[str, Any],
        *,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        track_id = self._load_browser_tidal_track_id(metadata)
        title = str(metadata.get("title") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        if not track_id and not title:
            return {}

        cache_key = (
            f"track:{track_id}"
            if track_id
            else f"search:{title.casefold()}|{artist.casefold()}"
        )
        cached_value = self._browser_api_metadata_cache.get(cache_key)
        if cached_value is not None and time.monotonic() - cached_value[0] < 30:
            return self._merge_browser_api_metadata(metadata, cached_value[1])

        api_metadata: dict[str, Any] = {}
        request_arguments = self._load_browser_api_arguments(arguments)
        if not track_id:
            search_document = self._load_browser_api_document(
                object_name="tidal_api_search",
                arguments={
                    **request_arguments,
                    "query": " ".join(part for part in (title, artist) if part),
                    "include": arguments.get(
                        "api_search_include",
                        "tracks,tracks.artists,tracks.albums,tracks.albums.coverArt,topHits",
                    ),
                },
            )
            search_track = self._load_browser_api_search_track(
                document=search_document,
                title=title,
                artist=artist,
            )
            if search_track:
                api_metadata.update(search_track)
                track_id = str(search_track.get("tidal_track_id") or "").strip()

        if track_id:
            track_document = self._load_browser_api_document(
                object_name="tidal_api_track",
                arguments={
                    **request_arguments,
                    "track_id": track_id,
                    "include": arguments.get("track_include", "albums,artists,albums.coverArt"),
                },
            )
            api_track = self._load_browser_api_track_from_document(track_document)
            if api_track:
                api_metadata.update(api_track)

            manifest_document = self._load_browser_api_document(
                object_name="tidal_api_track_manifest",
                arguments={
                    **request_arguments,
                    "track_id": track_id,
                    "formats": arguments.get("manifest_formats") or os.getenv("TIDAL_API_MANIFEST_FORMATS") or "AACLC",
                    "manifest_type": arguments.get("manifest_type") or os.getenv("TIDAL_API_MANIFEST_TYPE") or "HLS",
                    "uri_scheme": "HTTPS",
                    "usage": "PLAYBACK",
                },
            )
            api_metadata.update(self._load_browser_api_manifest_metadata(manifest_document))

            liked_metadata = self._load_browser_api_liked_metadata(
                track_id=track_id,
                arguments=request_arguments,
            )
            api_metadata.update(liked_metadata)

        self._browser_api_metadata_cache[cache_key] = (time.monotonic(), dict(api_metadata))
        return self._merge_browser_api_metadata(metadata, api_metadata)

    def _load_browser_api_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request_arguments: dict[str, Any] = {}
        for key in ("headers", "country_code", "timeout_s"):
            if key in arguments:
                request_arguments[key] = arguments.get(key)
        return request_arguments

    def _load_browser_api_document(
        self,
        *,
        object_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        dispatch_object = getattr(self.tidal_api_service, "dispatch_object", None)
        if not callable(dispatch_object):
            return None
        try:
            response_payload = dispatch_object(
                object_name=object_name,
                arguments=dict(arguments),
            )
        except Exception:
            return None
        if not isinstance(response_payload, dict) or not response_payload.get("ok"):
            return None
        try:
            document = json.loads(str(response_payload.get("response_body") or "{}"))
        except Exception:
            return None
        return document if isinstance(document, dict) else None

    def _merge_browser_api_metadata(
        self,
        metadata: dict[str, Any],
        api_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        merged_metadata: dict[str, Any] = {}
        for key, value in api_metadata.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            existing_value = metadata.get(key)
            existing_text = str(existing_value or "").strip().casefold()
            replace_artwork = key in {"art_url", "album_art_url"} and existing_text.startswith("file://")
            if replace_artwork or existing_text in {"", "-", "unknown", "none", "null"}:
                merged_metadata[key] = value
        if api_metadata:
            merged_metadata.setdefault("api_metadata_source", "tidal_api")
        return merged_metadata

    def _load_browser_tidal_track_id(self, metadata: dict[str, Any]) -> str:
        for key in ("tidal_track_id", "track_id", "cdp_tidal_track_id"):
            candidate = str(metadata.get(key) or "").strip()
            if re.fullmatch(r"[0-9]{1,20}", candidate):
                return candidate
        for key in ("track_url", "xesam_url", "url", "opened_url"):
            candidate = str(metadata.get(key) or "").strip()
            match = re.search(r"/(?:browse/)?track/([0-9]{1,20})(?:[/?#]|$)", candidate, flags=re.IGNORECASE)
            if match:
                return str(match.group(1)).strip()
        return ""

    def _load_browser_api_search_track(
        self,
        *,
        document: dict[str, Any] | None,
        title: str,
        artist: str,
    ) -> dict[str, Any]:
        if not document:
            return {}
        candidates = [
            self._load_browser_api_track_from_resource(resource=resource, included=document.get("included"))
            for resource in self._load_browser_api_track_resources(document)
        ]
        candidates = [candidate for candidate in candidates if candidate.get("tidal_track_id")]
        if not candidates:
            return {}

        normalized_title = self._load_browser_api_match_text(title)
        normalized_artist = self._load_browser_api_match_text(artist)

        def score(candidate: dict[str, Any]) -> tuple[int, int]:
            candidate_title = self._load_browser_api_match_text(candidate.get("title"))
            candidate_artist = self._load_browser_api_match_text(candidate.get("artist"))
            match_score = 0
            if normalized_title and candidate_title == normalized_title:
                match_score += 4
            elif normalized_title and normalized_title in candidate_title:
                match_score += 2
            if normalized_artist and candidate_artist == normalized_artist:
                match_score += 3
            elif normalized_artist and normalized_artist in candidate_artist:
                match_score += 1
            return match_score, -candidates.index(candidate)

        return max(candidates, key=score)

    def _load_browser_api_match_text(self, value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    def _load_browser_api_track_resources(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        included_resources = self._load_browser_api_included_resource_map(document.get("included"))
        primary_value = document.get("data")
        primary_resources = primary_value if isinstance(primary_value, list) else [primary_value]
        resources: list[dict[str, Any]] = []
        for primary_resource in primary_resources:
            if not isinstance(primary_resource, dict):
                continue
            if str(primary_resource.get("type") or "") == "tracks":
                resources.append(primary_resource)
            relationships = primary_resource.get("relationships")
            tracks_relationship = relationships.get("tracks") if isinstance(relationships, dict) else None
            track_references = tracks_relationship.get("data") if isinstance(tracks_relationship, dict) else None
            if isinstance(track_references, dict):
                track_references = [track_references]
            if isinstance(track_references, list):
                for reference in track_references:
                    if not isinstance(reference, dict) or str(reference.get("type") or "") != "tracks":
                        continue
                    resource = included_resources.get(self._load_browser_api_resource_key(reference)) or reference
                    resources.append(resource)
        resources.extend(
            resource
            for resource in included_resources.values()
            if str(resource.get("type") or "") == "tracks"
        )

        unique_resources: list[dict[str, Any]] = []
        seen_resource_keys: set[str] = set()
        for resource in resources:
            resource_key = self._load_browser_api_resource_key(resource)
            if not resource_key or resource_key in seen_resource_keys:
                continue
            seen_resource_keys.add(resource_key)
            unique_resources.append(resource)
        return unique_resources

    def _load_browser_api_track_from_document(self, document: dict[str, Any] | None) -> dict[str, Any]:
        if not document:
            return {}
        data = document.get("data")
        if isinstance(data, dict) and str(data.get("type") or "") == "tracks":
            return self._load_browser_api_track_from_resource(resource=data, included=document.get("included"))
        return {}

    def _load_browser_api_track_from_resource(self, *, resource: dict[str, Any], included: Any) -> dict[str, Any]:
        if not isinstance(resource, dict) or str(resource.get("type") or "") != "tracks":
            return {}
        track_id = str(resource.get("id") or "").strip()
        if not track_id:
            return {}
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        included_resources = self._load_browser_api_included_resource_map(included)
        artist_resources = self._load_browser_api_related_resources(
            resource=resource,
            relationship_name="artists",
            included=included_resources,
        )
        album_resources = self._load_browser_api_related_resources(
            resource=resource,
            relationship_name="albums",
            included=included_resources,
        )
        artist_info = [
            summary
            for summary in (self._load_browser_api_resource_summary(item) for item in artist_resources)
            if summary
        ]
        album_info = self._load_browser_api_resource_summary(album_resources[0]) if album_resources else {}
        album_art_url = self._load_browser_api_art_url(
            resource=album_resources[0] if album_resources else {},
            included=included_resources,
        )
        track_info = self._load_browser_api_resource_summary(resource)
        key_value = self._load_browser_api_text_value(attributes, "tonart", "musicalKey", "initialKey", "key")
        key_scale = self._load_browser_api_text_value(attributes, "keyScale", "key_scale")
        if key_value and key_scale and key_scale.casefold() not in key_value.casefold():
            key_value = f"{key_value} {key_scale}".strip()

        track_payload: dict[str, Any] = {
            "tidal_track_id": track_id,
            "title": self._load_browser_api_text_value(attributes, "title", "name"),
            "artist": self._load_browser_api_text_value(attributes, "artist", "artistName")
            or (self._load_browser_api_text_value(
                artist_resources[0].get("attributes") if artist_resources else {},
                "name",
                "title",
            )),
            "artist_info": artist_info,
            "artists": artist_info,
            "album": self._load_browser_api_text_value(attributes, "album", "albumName")
            or (self._load_browser_api_text_value(
                album_resources[0].get("attributes") if album_resources else {},
                "name",
                "title",
            )),
            "album_info": album_info,
            "track_info": track_info,
            "track_url": f"https://listen.tidal.com/track/{quote(track_id, safe='')}",
            "duration": self._load_browser_api_text_value(attributes, "duration"),
            "tonart": key_value,
            "art_url": album_art_url,
            "album_art_url": album_art_url,
        }
        if "explicit" in attributes:
            track_payload["explicit"] = bool(attributes.get("explicit"))
        media_tags = attributes.get("mediaTags")
        if isinstance(media_tags, list):
            track_payload["media_tags"] = list(media_tags)
        for output_key, attribute_keys in (
            ("bpm", ("bpm", "BPM", "tempo", "beatsPerMinute", "beats_per_minute")),
            ("track_number", ("trackNumber", "track_number", "number")),
            ("volume_number", ("volumeNumber", "volume_number", "volume")),
        ):
            value = self._load_browser_api_number_value(attributes, *attribute_keys)
            if value is not None:
                track_payload[output_key] = value
        for output_key, attribute_keys in (
            ("release_date", ("releaseDate", "release_date")),
            ("version", ("version", "trackVersion", "track_version")),
            ("isrc", ("isrc", "ISRC")),
            ("copyright", ("copyrightText", "copyright_text", "copyright")),
        ):
            value = self._load_browser_api_text_value(attributes, *attribute_keys)
            if isinstance(attributes.get(attribute_keys[-1]), dict):
                value = self._load_browser_api_text_value(attributes.get(attribute_keys[-1]), "text", "value")
            if value:
                track_payload[output_key] = value
        return {
            key: value
            for key, value in track_payload.items()
            if value is not None and value != "" and value != [] and value != {}
        }

    def _load_browser_api_manifest_metadata(self, document: dict[str, Any] | None) -> dict[str, Any]:
        if not document or not isinstance(document.get("data"), dict):
            return {}
        resource = document["data"]
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        manifest_text = json.dumps(attributes, ensure_ascii=True, sort_keys=True)
        stream_quality = self._load_browser_api_text_value(
            attributes,
            "audioQuality",
            "audio_quality",
            "streamQuality",
            "stream_quality",
            "quality",
            "qualityLabel",
        )
        audio_mode = self._load_browser_api_text_value(attributes, "audioMode", "audio_mode")
        if not audio_mode:
            audio_mode = load_audio_mode_from_text(manifest_text)
        quality_resolution = self._load_browser_api_text_value(
            attributes,
            "qualityResolution",
            "quality_resolution",
            "audioResolution",
            "audio_resolution",
            "resolution",
        ) or load_quality_resolution(manifest_text)
        sample_rate_value = self._load_browser_api_number_value(
            attributes,
            "sampleRate",
            "sample_rate",
            "samplingRate",
            "sampleRateHz",
            "sample_rate_hz",
            "sampleRateKHz",
            "sample_rate_khz",
        )
        sample_rate_khz: int | float | None = None
        if sample_rate_value is not None:
            sample_rate_numeric = float(sample_rate_value)
            sample_rate_khz = sample_rate_numeric / 1000 if sample_rate_numeric >= 1000 else sample_rate_numeric
        bit_depth = self._load_browser_api_number_value(attributes, "bitDepth", "bit_depth", "bitsPerSample", "bits_per_sample")
        resolution_parts = []
        if bit_depth is not None:
            resolution_parts.append(f"{bit_depth:g}-bit")
        if sample_rate_khz is not None and not quality_resolution:
            resolution_parts.append(f"{sample_rate_khz:g} kHz")
        if resolution_parts and not quality_resolution:
            quality_resolution = " ".join(resolution_parts)
        metadata: dict[str, Any] = {
            **load_tidal_dsp_metadata(attributes),
            "stream_quality": stream_quality,
            "audio_mode": audio_mode,
            "quality_resolution": quality_resolution,
            "bitrate_kbps": self._load_browser_api_number_value(
                attributes,
                "bitrate",
                "bit_rate",
                "bitRate",
                "audioBitrate",
                "audio_bitrate",
                "bitrateKbps",
                "bitrate_kbps",
            ),
            "sample_rate_khz": sample_rate_khz,
            "bit_depth": bit_depth,
            "channel_count": self._load_browser_api_number_value(
                attributes,
                "channelCount",
                "channel_count",
                "channels",
                "audioChannels",
                "audio_channels",
            ),
            "codec": self._load_browser_api_text_value(attributes, "codec", "audioCodec", "audio_codec", "mimeType", "mime_type"),
            "asset_presentation": self._load_browser_api_text_value(attributes, "trackPresentation", "track_presentation"),
        }
        return {
            key: value
            for key, value in metadata.items()
            if value is not None and value != ""
        }

    def _load_browser_api_liked_metadata(
        self,
        *,
        track_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        document = self._load_browser_api_document(
            object_name="tidal_api_request",
            arguments={
                **arguments,
                "url": "https://openapi.tidal.com/v2/userCollectionTracks/me",
                "query": {"include": "items"},
            },
        )
        if document is None:
            return {}
        liked = track_id in self._load_browser_api_track_ids(document)
        return {
            "liked": liked,
            "liked_state": "liked" if liked else "not_liked",
        }

    def _load_browser_api_track_ids(self, document: dict[str, Any]) -> set[str]:
        track_ids: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if str(value.get("type") or "") == "tracks":
                    track_id = str(value.get("id") or "").strip()
                    if track_id:
                        track_ids.add(track_id)
                for child_value in value.values():
                    visit(child_value)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(document)
        return track_ids

    def _load_browser_api_included_resource_map(self, included: Any) -> dict[str, dict[str, Any]]:
        if isinstance(included, dict):
            resources = list(included.values())
        elif isinstance(included, (list, tuple, set)):
            resources = list(included)
        else:
            resources = []
        return {
            self._load_browser_api_resource_key(resource): dict(resource)
            for resource in resources
            if isinstance(resource, dict) and self._load_browser_api_resource_key(resource)
        }

    def _load_browser_api_resource_key(self, resource: dict[str, Any]) -> str:
        return f"{str(resource.get('type') or '').strip()}:{str(resource.get('id') or '').strip()}"

    def _load_browser_api_related_resources(
        self,
        *,
        resource: dict[str, Any],
        relationship_name: str,
        included: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        relationships = resource.get("relationships") if isinstance(resource.get("relationships"), dict) else {}
        relationship = relationships.get(relationship_name)
        related_data = relationship.get("data") if isinstance(relationship, dict) else None
        if isinstance(related_data, dict):
            related_data = [related_data]
        if not isinstance(related_data, list):
            return []
        return [
            dict(included.get(self._load_browser_api_resource_key(reference)) or reference)
            for reference in related_data
            if isinstance(reference, dict)
        ]

    def _load_browser_api_resource_summary(self, resource: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(resource, dict) or not resource:
            return {}
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        resource_type = str(resource.get("type") or "").strip()
        resource_id = str(resource.get("id") or "").strip()
        name = self._load_browser_api_text_value(attributes, "name", "title")
        summary: dict[str, Any] = {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "name": name,
        }
        if resource_type and resource_id and resource_type in {"tracks", "albums", "artists", "playlists"}:
            summary["url"] = f"https://listen.tidal.com/{resource_type.rstrip('s')}/{quote(resource_id, safe='')}"
        return {key: value for key, value in summary.items() if value}

    def _load_browser_api_art_url(
        self,
        *,
        resource: dict[str, Any],
        included: dict[str, dict[str, Any]],
    ) -> str:
        if not isinstance(resource, dict) or not resource:
            return ""
        attributes = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
        for key in ("art_url", "artUrl", "cover_url", "coverUrl", "image_url", "imageUrl", "thumbnail_url", "thumbnailUrl", "url", "href", "src"):
            candidate = str(attributes.get(key) or resource.get(key) or "").strip()
            if candidate.startswith(("http://", "https://")):
                return candidate
        files_value = attributes.get("files")
        if isinstance(files_value, list):
            for file_value in files_value:
                if not isinstance(file_value, dict):
                    continue
                candidate = str(file_value.get("href") or file_value.get("url") or "").strip()
                if candidate.startswith(("http://", "https://")):
                    return candidate
        for relationship_name in ("coverArt", "artwork", "artworks", "thumbnailArt", "profileArt"):
            for related_resource in self._load_browser_api_related_resources(
                resource=resource,
                relationship_name=relationship_name,
                included=included,
            ):
                candidate = self._load_browser_api_art_url(resource=related_resource, included=included)
                if candidate:
                    return candidate
        return ""

    def _load_browser_api_text_value(self, attributes: Any, *keys: str) -> str:
        if not isinstance(attributes, dict):
            return ""
        for key in keys:
            value = attributes.get(key)
            if isinstance(value, (dict, list, tuple, set)):
                continue
            text_value = str(value or "").strip()
            if text_value:
                return text_value
        return ""

    def _load_browser_api_number_value(self, attributes: Any, *keys: str) -> int | float | None:
        if not isinstance(attributes, dict):
            return None
        for key in keys:
            value = attributes.get(key)
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return int(value) if float(value).is_integer() else value
            try:
                numeric_value = float(str(value).strip())
            except Exception:
                continue
            if math.isfinite(numeric_value):
                return int(numeric_value) if numeric_value.is_integer() else numeric_value
        return None

    def _load_tonart_metadata(self, tonart_value: str | None) -> dict[str, str]:
        raw_tonart = str(tonart_value or "").strip()
        if not raw_tonart:
            return {}

        tonart_components = self._load_tonart_components(raw_tonart)
        if tonart_components is None:
            return {}

        pitch_class, mode_value = tonart_components
        note_label = self._PITCH_CLASS_TO_SHARP_NOTE.get(pitch_class, "")
        if not note_label:
            return {}

        is_minor = mode_value == "minor"
        tonart_alpha = f"{note_label}m" if is_minor else note_label
        tonart_mode = "moll" if is_minor else "dur"
        tonart_dur_moll = f"{note_label} {'Moll' if is_minor else 'Dur'}"
        tonart_numeric = self._load_tonart_numeric(pitch_class=pitch_class, mode_value=mode_value)

        display_parts = [tonart_dur_moll, tonart_alpha]
        if tonart_numeric:
            display_parts.append(tonart_numeric)

        return {
            "tonart_raw": raw_tonart,
            "tonart_mode": tonart_mode,
            "tonart_dur_moll": tonart_dur_moll,
            "tonart_alpha": tonart_alpha,
            "tonart_numeric": tonart_numeric,
            "tonart_display": " | ".join(display_parts),
        }

    def _load_tonart_numeric(self, *, pitch_class: int, mode_value: str) -> str:
        if mode_value == "minor":
            tonart_number = self._CAMELOT_MINOR_BY_PITCH_CLASS.get(pitch_class)
            return f"{tonart_number}A" if tonart_number is not None else ""
        tonart_number = self._CAMELOT_MAJOR_BY_PITCH_CLASS.get(pitch_class)
        return f"{tonart_number}B" if tonart_number is not None else ""

    def _load_tonart_components(self, tonart_value: str) -> tuple[int, str] | None:
        normalized_value = str(tonart_value or "").strip()
        if not normalized_value:
            return None

        normalized_value = normalized_value.replace("♯", "#").replace("♭", "b")
        normalized_value = normalized_value.replace("_", " ")
        normalized_value = re.sub(r"\s+", " ", normalized_value)

        camelot_match = re.search(r"\b(1[0-2]|[1-9])\s*([aAbB])\b", normalized_value)
        if camelot_match:
            tonart_number = int(str(camelot_match.group(1) or "0"))
            tonart_type = str(camelot_match.group(2) or "").upper()
            if tonart_type == "A":
                pitch_class = self._PITCH_CLASS_BY_CAMELOT_MINOR.get(tonart_number)
                if pitch_class is not None:
                    return pitch_class, "minor"
            if tonart_type == "B":
                pitch_class = self._PITCH_CLASS_BY_CAMELOT_MAJOR.get(tonart_number)
                if pitch_class is not None:
                    return pitch_class, "major"

        open_key_match = re.search(r"\b(1[0-2]|[1-9])\s*([mMdD])\b", normalized_value)
        if open_key_match:
            tonart_number = int(str(open_key_match.group(1) or "0"))
            tonart_type = str(open_key_match.group(2) or "").lower()
            if tonart_type == "m":
                pitch_class = self._PITCH_CLASS_BY_CAMELOT_MINOR.get(tonart_number)
                if pitch_class is not None:
                    return pitch_class, "minor"
            if tonart_type == "d":
                pitch_class = self._PITCH_CLASS_BY_CAMELOT_MAJOR.get(tonart_number)
                if pitch_class is not None:
                    return pitch_class, "major"

        note_with_word_accidental_match = re.search(
            r"\b([A-Ga-g])\s*(sharp|flat)\s*(dur|major|maj|moll|minor|min)\b",
            normalized_value,
            flags=re.IGNORECASE,
        )
        if note_with_word_accidental_match:
            note_letter = str(note_with_word_accidental_match.group(1) or "").upper()
            accidental_word = str(note_with_word_accidental_match.group(2) or "").casefold()
            accidental = "#" if accidental_word == "sharp" else "b"
            mode_token = str(note_with_word_accidental_match.group(3) or "")
            mode_value = self._load_tonart_mode(mode_token)
            pitch_class = self._load_pitch_class(note_letter=note_letter, accidental=accidental)
            if pitch_class is not None and mode_value:
                return pitch_class, mode_value

        note_with_mode_match = re.search(
            r"\b([A-Ga-g])\s*([#b]?)\s*(dur|major|maj|moll|minor|min)\b",
            normalized_value,
            flags=re.IGNORECASE,
        )
        if note_with_mode_match:
            note_letter = str(note_with_mode_match.group(1) or "").upper()
            accidental = str(note_with_mode_match.group(2) or "")
            mode_token = str(note_with_mode_match.group(3) or "")
            mode_value = self._load_tonart_mode(mode_token)
            pitch_class = self._load_pitch_class(note_letter=note_letter, accidental=accidental)
            if pitch_class is not None and mode_value:
                return pitch_class, mode_value

        compact_minor_match = re.search(r"\b([A-Ga-g])\s*([#b]?)m\b", normalized_value)
        if compact_minor_match:
            note_letter = str(compact_minor_match.group(1) or "").upper()
            accidental = str(compact_minor_match.group(2) or "")
            pitch_class = self._load_pitch_class(note_letter=note_letter, accidental=accidental)
            if pitch_class is not None:
                return pitch_class, "minor"

        return None

    def _load_tonart_mode(self, mode_token: str) -> str:
        normalized_mode = str(mode_token or "").strip().casefold()
        if normalized_mode in self._MINOR_MODE_TOKENS:
            return "minor"
        if normalized_mode in self._MAJOR_MODE_TOKENS:
            return "major"
        return ""

    def _load_pitch_class(self, *, note_letter: str, accidental: str) -> int | None:
        normalized_letter = str(note_letter or "").strip().upper()
        normalized_accidental = str(accidental or "").strip()
        if normalized_accidental == "♯":
            normalized_accidental = "#"
        if normalized_accidental == "♭":
            normalized_accidental = "b"
        if normalized_accidental not in {"", "#", "b"}:
            return None

        note_key = f"{normalized_letter}{normalized_accidental}"
        return self._PITCH_CLASS_BY_NOTE.get(note_key.upper())

    def _load_player_pick_script(self, *, player_selector: str, wait_seconds: int) -> str:
        selector_prefix = shlex.quote(str(player_selector or "chromium"))
        return (
            "selected_player=''; "
            "wait_counter=0; "
            f"while [ \"$wait_counter\" -le {wait_seconds} ]; do "
            "players=$(playerctl -l 2>/dev/null || true); "
            "selector_prefix="
            + selector_prefix
            + "; "
            "selected_player=$(printf '%s\\n' \"$players\" | while IFS= read -r candidate_player; do "
            "if [ -n \"$candidate_player\" ] && [ \"$(printf '%s' \"$candidate_player\" | awk -v prefix=\"$selector_prefix\" 'index($0,prefix)==1{print 1; exit}')\" = \"1\" ] && [ \"$(playerctl -p \"$candidate_player\" status 2>/dev/null || true)\" = \"Playing\" ]; then printf '%s\\n' \"$candidate_player\"; break; fi; "
            "done); "
            "if [ -z \"$selected_player\" ]; then selected_player=$(printf '%s\\n' \"$players\" | awk -v prefix=\"$selector_prefix\" 'index($0,prefix)==1{print; exit}'); fi; "
            "if [ -n \"$selected_player\" ]; then break; fi; "
            "sleep 1; wait_counter=$((wait_counter+1)); "
            "done; "
            "if [ -z \"$selected_player\" ]; then echo 'error=no_player'; exit 1; fi; "
        )

    def _load_player_audio_metadata_command(self) -> str:
        return (
            "stream_quality=''; for metadata_key in tidal:audioQuality xesam:audioQuality mpris:audioQuality audio-quality quality; do "
            "stream_quality=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$stream_quality\" ]; then break; fi; done; "
            "audio_mode=''; for metadata_key in tidal:audioMode xesam:audioMode mpris:audioMode audio-mode audio_mode; do "
            "audio_mode=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$audio_mode\" ]; then break; fi; done; "
            "quality_resolution=''; for metadata_key in tidal:qualityResolution xesam:qualityResolution quality-resolution quality_resolution audioResolution audio-resolution audio_resolution resolution; do "
            "quality_resolution=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$quality_resolution\" ]; then break; fi; done; "
            "bitrate_kbps=''; for metadata_key in tidal:audioBitrate xesam:audioBitrate mpris:audioBitrate audio-bitrate audio_bitrate bitrate; do "
            "bitrate_kbps=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$bitrate_kbps\" ]; then break; fi; done; "
            "sample_rate_khz=''; for metadata_key in tidal:sampleRate xesam:audioSampleRate mpris:audioSampleRate audio-sample-rate audio_sample_rate sample-rate sample_rate samplingRate frequency audioFrequency; do "
            "sample_rate_khz=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$sample_rate_khz\" ]; then break; fi; done; "
            "bit_depth=''; for metadata_key in tidal:bitDepth xesam:audioBitDepth mpris:audioBitDepth audio-bit-depth audio_bit_depth bit-depth bit_depth bitWidth bit_width audioBitWidth audio_bit_width bitsPerSample bits_per_sample; do "
            "bit_depth=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$bit_depth\" ]; then break; fi; done; "
            "channel_count=''; for metadata_key in tidal:audioChannels xesam:audioChannels mpris:audioChannels audio-channels audio_channels channels channelCount channel_count; do "
            "channel_count=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$channel_count\" ]; then break; fi; done; "
            "codec=''; for metadata_key in tidal:audioCodec xesam:audioCodec mpris:audioCodec audio-codec audio_codec codec; do "
            "codec=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$codec\" ]; then break; fi; done; "
            "tonart=''; for metadata_key in xesam:initialKey xesam:key tidal:key key initialKey; do "
            "tonart=$(playerctl -p \"$selected_player\" metadata \"$metadata_key\" 2>/dev/null || true); "
            "if [ -n \"$tonart\" ]; then break; fi; done; "
            "echo stream_quality=$stream_quality; "
            "echo audio_mode=$audio_mode; "
            "echo quality_resolution=$quality_resolution; "
            "echo bitrate_kbps=$bitrate_kbps; "
            "echo sample_rate_khz=$sample_rate_khz; "
            "echo bit_depth=$bit_depth; "
            "echo channel_count=$channel_count; "
            "echo codec=$codec; "
            "echo tonart=$tonart; "
        )

    @staticmethod
    def _load_pactl_node_value(raw_text: str, *, node_kind: str, target_name: str, field_name: str) -> str:
        in_target = False
        node_header_names = {
            "Sink": ("Sink", "Ziel"),
            "Source": ("Source", "Quelle"),
        }.get(node_kind, (node_kind,))
        node_header = re.compile(rf"^(?:{'|'.join(map(re.escape, node_header_names))}) #[0-9]+$")
        field_prefixes = {
            "Description": ("Description:", "Beschreibung:"),
            "Card": ("Card:", "Karte:"),
        }.get(field_name, (f"{field_name}:",))
        for raw_line in str(raw_text or "").splitlines():
            line = raw_line.strip()
            if node_header.match(line):
                in_target = False
                continue
            if line.startswith("Name:"):
                in_target = line.partition(":")[2].strip() == str(target_name or "").strip()
                continue
            if in_target:
                for field_prefix in field_prefixes:
                    if line.startswith(field_prefix):
                        return line.partition(":")[2].strip()
        return ""

    @staticmethod
    def _run_pactl_command(arguments: list[str]) -> str:
        if shutil.which("pactl") is None:
            return ""
        try:
            completed = subprocess.run(
                ["pactl", *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode != 0:
            return ""
        return str(completed.stdout or "").strip()

    @staticmethod
    def _load_pactl_card_value(
        raw_text: str,
        *,
        card_index: str,
        field_name: str,
        card_name: str = "",
    ) -> str:
        target_index = str(card_index or "").strip()
        target_name = str(card_name or "").strip()
        if not target_index and not target_name:
            return ""
        in_target = False
        field_value = ""
        property_values: dict[str, str] = {}
        card_header = re.compile(r"^(?:Card|Karte) #([0-9]+)$")
        for raw_line in str(raw_text or "").splitlines():
            line = raw_line.strip()
            header_match = card_header.match(line)
            if header_match:
                in_target = bool(target_index) and header_match.group(1) == target_index
                continue
            if not target_index and line.startswith("Name:"):
                in_target = line.partition(":")[2].strip() == target_name
                continue
            if not in_target:
                continue
            for field_prefix in (f"{field_name}:", f"{field_name} ="):
                if line.startswith(field_prefix):
                    field_value = line.partition("=")[2].strip().strip('"') if "=" in line else line.partition(":")[2].strip()
                    break
            if field_name == "Description":
                for property_prefix in (
                    "device.nick =",
                    "alsa.card_name =",
                    "api.alsa.card.name =",
                    "device.description =",
                    "device.product.name =",
                ):
                    if line.startswith(property_prefix):
                        property_values[property_prefix] = line.partition("=")[2].strip().strip('"')
                        break
        if field_name == "Description":
            for property_prefix in (
                "device.nick =",
                "alsa.card_name =",
                "api.alsa.card.name =",
                "device.description =",
                "device.product.name =",
            ):
                if property_values.get(property_prefix):
                    return property_values[property_prefix]
        return field_value

    def _load_audio_route_metadata(self) -> dict[str, str]:
        sink_name = self._run_pactl_command(["get-default-sink"])
        source_name = self._run_pactl_command(["get-default-source"])
        if not sink_name or not source_name:
            info_text = self._run_pactl_command(["info"])
            for line in info_text.splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                normalized_key = key.strip().casefold()
                if normalized_key == "default sink" and not sink_name:
                    sink_name = value.strip()
                elif normalized_key == "default source" and not source_name:
                    source_name = value.strip()

        sinks_text = self._run_pactl_command(["list", "sinks"])
        sources_text = self._run_pactl_command(["list", "sources"])
        cards_text = self._run_pactl_command(["list", "cards"])
        sink_description = self._load_pactl_node_value(
            sinks_text,
            node_kind="Sink",
            target_name=sink_name,
            field_name="Description",
        )
        source_description = self._load_pactl_node_value(
            sources_text,
            node_kind="Source",
            target_name=source_name,
            field_name="Description",
        )
        card_index = self._load_pactl_node_value(
            sinks_text,
            node_kind="Sink",
            target_name=sink_name,
            field_name="Card",
        )
        card_name_candidates: list[str] = []
        if sink_name:
            card_name = sink_name.replace("alsa_output.", "alsa_card.", 1)
            card_name_candidates = [
                card_name,
                re.sub(r"\.(?:pro|analog|hdmi|iec958|surround).*", "", card_name),
            ]
        card_description = self._load_pactl_card_value(
            cards_text,
            card_index=card_index,
            field_name="Description",
            card_name=card_name_candidates[0] if not card_index and card_name_candidates else "",
        )
        if not card_description and not card_index:
            for candidate_card_name in dict.fromkeys(card_name_candidates):
                card_description = self._load_pactl_card_value(
                    cards_text,
                    card_index="",
                    field_name="Description",
                    card_name=candidate_card_name,
                )
                if card_description:
                    break
        profile = ""
        if card_index:
            in_target_card = False
            for raw_line in cards_text.splitlines():
                line = raw_line.strip()
                if line.startswith(("Card #", "Karte #")):
                    in_target_card = re.sub(r"^(?:Card|Karte) #", "", line).strip() == card_index
                    continue
                if in_target_card:
                    for profile_prefix in ("Active Profile:", "Aktives Profil:"):
                        if line.startswith(profile_prefix):
                            profile = line.partition(":")[2].strip()
                            break
                    if profile:
                        break
        if not profile and sink_name:
            for candidate_card_name in dict.fromkeys(card_name_candidates):
                in_target_card = False
                for raw_line in cards_text.splitlines():
                    line = raw_line.strip()
                    if line.startswith(("Card #", "Karte #")):
                        in_target_card = False
                        continue
                    if line.startswith("Name:"):
                        in_target_card = line.partition(":")[2].strip() == candidate_card_name
                        continue
                    if in_target_card:
                        for profile_prefix in ("Active Profile:", "Aktives Profil:"):
                            if line.startswith(profile_prefix):
                                profile = line.partition(":")[2].strip()
                                break
                        if profile:
                            break
                if profile:
                    break
        if profile.casefold() == "pro-audio":
            profile = "Pro"

        metadata = {
            "audio_source": source_description or source_name,
            "audio_source_name": source_name,
            "audio_sink": sink_description or sink_name,
            "audio_sink_name": sink_name,
            "audio_interface": card_description or sink_description or sink_name,
            "audio_profile": profile,
        }
        return {key: value for key, value in metadata.items() if value        }

    @staticmethod
    def _load_audio_aux_routing(raw_text: str) -> list[dict[str, Any]]:
        channels = [
            {"channel": f"AUX{index}", "input": [], "output": []}
            for index in range(6)
        ]
        current_port = ""
        connections: list[tuple[str, str]] = []

        for raw_line in str(raw_text or "").splitlines():
            line = raw_line.strip()
            if line.startswith("|->"):
                if current_port:
                    connections.append((current_port, line[3:].strip()))
            elif line.startswith("|<-"):
                if current_port:
                    connections.append((line[3:].strip(), current_port))
            elif ":" in line:
                current_port = line

        for source_port, destination_port in connections:
            input_match = re.search(r":capture_AUX([0-5])$", source_port, re.IGNORECASE)
            if input_match and re.search(r"camilladsp[^:]*:input_[^:]+$", destination_port, re.IGNORECASE):
                channel = channels[int(input_match.group(1))]
                route = f"{source_port.rsplit(':', 1)[-1]} -> {destination_port.rsplit(':', 1)[-1]}"
                if route not in channel["input"]:
                    channel["input"].append(route)
                continue

            output_match = re.search(r":playback_AUX([0-5])$", destination_port, re.IGNORECASE)
            if output_match and re.search(r"camilladsp[^:]*:output_[^:]+$", source_port, re.IGNORECASE):
                channel = channels[int(output_match.group(1))]
                route = f"{source_port.rsplit(':', 1)[-1]} -> {destination_port.rsplit(':', 1)[-1]}"
                if route not in channel["output"]:
                    channel["output"].append(route)

        return channels

    @classmethod
    def _load_audio_aux_routing_metadata(cls) -> dict[str, str]:
        pw_link_path = shutil.which("pw-link")
        if not pw_link_path:
            return {
                "audio_aux_routing_status": "pw_link_missing",
                "audio_aux_routing": "[]",
            }
        try:
            completed = subprocess.run(
                [pw_link_path, "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            return {
                "audio_aux_routing_status": "pw_link_timeout",
                "audio_aux_routing": "[]",
            }
        except OSError:
            return {
                "audio_aux_routing_status": "pw_link_unavailable",
                "audio_aux_routing": "[]",
            }
        if completed.returncode != 0:
            return {
                "audio_aux_routing_status": f"pw_link_exit_{completed.returncode}",
                "audio_aux_routing": "[]",
            }
        return {
            "audio_aux_routing_status": "ready",
            "audio_aux_routing": json.dumps(
                cls._load_audio_aux_routing(completed.stdout),
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _load_audio_metric_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _load_camilladsp_delay_ms(cls, parameters: dict[str, Any], sample_rate: float | None) -> float | None:
        delay_value = cls._load_audio_metric_number(parameters.get("delay"))
        if delay_value is None:
            return None
        unit = str(parameters.get("unit") or "ms").strip().casefold()
        if unit in {"ms", "millisecond", "milliseconds"}:
            return delay_value
        if unit in {"sample", "samples"} and sample_rate and sample_rate > 0:
            return delay_value * 1000 / sample_rate
        if unit in {"mm", "millimeter", "millimeters"}:
            return delay_value / 343
        return None

    @classmethod
    def _load_camilladsp_biquad_coefficients(
        cls,
        *,
        parameters: dict[str, Any],
        sample_rate: float,
    ) -> tuple[float, float, float, float, float, float] | None:
        frequency = cls._load_audio_metric_number(parameters.get("freq"))
        q_value = cls._load_audio_metric_number(parameters.get("q"))
        gain_db = cls._load_audio_metric_number(parameters.get("gain"))
        filter_type = str(parameters.get("type") or "").strip().casefold()
        if frequency is None or q_value is None or q_value <= 0 or not 0 < frequency < sample_rate / 2:
            return None

        omega = 2 * math.pi * frequency / sample_rate
        cosine = math.cos(omega)
        alpha = math.sin(omega) / (2 * q_value)
        if filter_type == "lowpass":
            numerator = ((1 - cosine) / 2, 1 - cosine, (1 - cosine) / 2)
            denominator = (1 + alpha, -2 * cosine, 1 - alpha)
        elif filter_type == "highpass":
            numerator = ((1 + cosine) / 2, -(1 + cosine), (1 + cosine) / 2)
            denominator = (1 + alpha, -2 * cosine, 1 - alpha)
        elif filter_type == "peaking":
            if gain_db is None:
                return None
            amplitude = 10 ** (gain_db / 40)
            numerator = (
                1 + alpha * amplitude,
                -2 * cosine,
                1 - alpha * amplitude,
            )
            denominator = (
                1 + alpha / amplitude,
                -2 * cosine,
                1 - alpha / amplitude,
            )
        elif filter_type == "notch":
            numerator = (1, -2 * cosine, 1)
            denominator = (1 + alpha, -2 * cosine, 1 - alpha)
        elif filter_type == "bandpass":
            numerator = (alpha, 0, -alpha)
            denominator = (1 + alpha, -2 * cosine, 1 - alpha)
        elif filter_type == "allpass":
            numerator = (1 - alpha, -2 * cosine, 1 + alpha)
            denominator = (1 + alpha, -2 * cosine, 1 - alpha)
        else:
            return None

        a0, a1, a2 = denominator
        if not math.isfinite(a0) or a0 == 0:
            return None
        b0, b1, b2 = numerator
        return b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0

    @classmethod
    def _load_camilladsp_playback_response(
        cls,
        *,
        config: dict[str, Any],
        channel_index: int,
        sample_rate: float | None,
        fader_values: list[Any] | None = None,
    ) -> tuple[str, list[dict[str, float]], str]:
        if sample_rate is None or sample_rate <= 40:
            return "", [], "sample_rate_unavailable"

        filters = config.get("filters") if isinstance(config.get("filters"), dict) else {}
        mixers = config.get("mixers") if isinstance(config.get("mixers"), dict) else {}
        pipeline = config.get("pipeline") if isinstance(config.get("pipeline"), list) else []
        biquad_coefficients: list[tuple[float, float, float, float, float, float]] = []
        total_gain_db = 0.0
        path_label = ""
        fader_indices: dict[str, int] = {}
        for stage in pipeline:
            if not isinstance(stage, dict) or str(stage.get("type") or "").strip().casefold() != "filter":
                continue
            stage_filter_names = stage.get("names")
            if not isinstance(stage_filter_names, list):
                continue
            for filter_name in stage_filter_names:
                filter_config = filters.get(str(filter_name))
                parameters = filter_config.get("parameters") if isinstance(filter_config, dict) else None
                if (
                    isinstance(filter_config, dict)
                    and str(filter_config.get("type") or "").strip().casefold() == "volume"
                    and isinstance(parameters, dict)
                ):
                    fader_name = str(parameters.get("fader") or "").strip()
                    if fader_name and fader_name not in fader_indices:
                        fader_indices[fader_name] = len(fader_indices)

        for stage in pipeline:
            if not isinstance(stage, dict) or stage.get("bypassed") is True:
                continue
            stage_type = str(stage.get("type") or "").strip().casefold()
            if stage_type == "mixer":
                mixer = mixers.get(str(stage.get("name") or ""))
                mappings = mixer.get("mapping") if isinstance(mixer, dict) else []
                matching_mapping = next(
                    (
                        mapping
                        for mapping in mappings or []
                        if isinstance(mapping, dict)
                        and str(mapping.get("dest", "")) == str(channel_index)
                    ),
                    None,
                )
                if matching_mapping is None:
                    continue
                sources = matching_mapping.get("sources")
                if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
                    return path_label, [], "mixed_sources"
                source_gain = cls._load_audio_metric_number(sources[0].get("gain"))
                total_gain_db += 0.0 if source_gain is None else source_gain
                continue

            if stage_type != "filter":
                continue
            selected_channels = stage.get("channels")
            filter_names = stage.get("names")
            if (
                not isinstance(selected_channels, list)
                or channel_index not in selected_channels
                or not isinstance(filter_names, list)
            ):
                continue

            stage_description = str(stage.get("description") or "").strip()
            label_match = re.search(r"\boutput\s+\d+\s*/\s*(.+)$", stage_description, re.IGNORECASE)
            if label_match:
                path_label = label_match.group(1).strip()

            for filter_name in filter_names:
                filter_config = filters.get(str(filter_name))
                if not isinstance(filter_config, dict):
                    return path_label, [], "filter_config_unavailable"
                filter_type = str(filter_config.get("type") or "").strip().casefold()
                parameters = filter_config.get("parameters")
                if not isinstance(parameters, dict):
                    return path_label, [], "filter_parameters_unavailable"
                if filter_type == "gain":
                    gain_db = cls._load_audio_metric_number(parameters.get("gain"))
                    if gain_db is None:
                        return path_label, [], "filter_gain_unavailable"
                    total_gain_db += gain_db
                elif filter_type == "volume":
                    fader_name = str(parameters.get("fader") or "").strip()
                    fader_index = fader_indices.get(fader_name)
                    if fader_index is None or not isinstance(fader_values, list) or fader_index >= len(fader_values):
                        return path_label, [], "fader_unavailable"
                    fader = fader_values[fader_index]
                    if not isinstance(fader, dict):
                        return path_label, [], "fader_unavailable"
                    if fader.get("mute") is True:
                        return path_label, [], "muted"
                    fader_gain_db = cls._load_audio_metric_number(fader.get("volume"))
                    if fader_gain_db is None:
                        return path_label, [], "fader_unavailable"
                    total_gain_db += fader_gain_db
                elif filter_type == "biquad":
                    coefficients = cls._load_camilladsp_biquad_coefficients(
                        parameters=parameters,
                        sample_rate=sample_rate,
                    )
                    if coefficients is None:
                        return path_label, [], "unsupported_biquad"
                    biquad_coefficients.append(coefficients)
                elif filter_type != "delay":
                    return path_label, [], "unsupported_filter"

        maximum_frequency = min(20_000.0, sample_rate * 0.49)
        if maximum_frequency <= 20:
            return path_label, [], "frequency_range_unavailable"

        response_curve: list[dict[str, float]] = []
        for index in range(101):
            frequency = 20 * (maximum_frequency / 20) ** (index / 100)
            omega = 2 * math.pi * frequency / sample_rate
            z1 = complex(math.cos(omega), -math.sin(omega))
            z2 = z1 * z1
            magnitude = 1.0
            for b0, b1, b2, a0, a1, a2 in biquad_coefficients:
                numerator = b0 + b1 * z1 + b2 * z2
                denominator = a0 + a1 * z1 + a2 * z2
                if abs(denominator) == 0:
                    return path_label, [], "invalid_biquad_response"
                magnitude *= abs(numerator / denominator)
            if total_gain_db:
                magnitude *= 10 ** (total_gain_db / 20)
            level_db = 20 * math.log10(max(magnitude, 1e-12))
            response_curve.append({"frequency_hz": frequency, "level_db": level_db})

        return path_label, response_curve, "ready"

    @classmethod
    def _load_camilladsp_channel_settings(
        cls,
        *,
        config: dict[str, Any],
        capture_channel_count: int,
        playback_channel_count: int,
    ) -> dict[str, list[dict[str, Any]]]:
        devices = config.get("devices") if isinstance(config.get("devices"), dict) else {}
        sample_rate = cls._load_audio_metric_number(devices.get("samplerate"))
        if sample_rate is None:
            sample_rate = cls._load_audio_metric_number(devices.get("capture_samplerate"))
        channel_settings = {
            "capture": [
                {"frequency_filters": [], "delay_ms": 0.0, "phase_inverted": False, "phase_known": True}
                for _ in range(max(0, capture_channel_count))
            ],
            "playback": [
                {"frequency_filters": [], "delay_ms": 0.0, "phase_inverted": False, "phase_known": True}
                for _ in range(max(0, playback_channel_count))
            ],
        }
        filters = config.get("filters") if isinstance(config.get("filters"), dict) else {}
        mixers = config.get("mixers") if isinstance(config.get("mixers"), dict) else {}
        pipeline = config.get("pipeline") if isinstance(config.get("pipeline"), list) else []
        active_side = "capture"

        for stage in pipeline:
            if not isinstance(stage, dict):
                continue
            stage_type = str(stage.get("type") or "").strip().casefold()
            if stage_type == "mixer":
                mixer_name = str(stage.get("name") or "")
                mixer = mixers.get(mixer_name)
                mappings = mixer.get("mapping") if isinstance(mixer, dict) else []
                if isinstance(mappings, list):
                    for mapping in mappings:
                        if not isinstance(mapping, dict):
                            continue
                        try:
                            output_index = int(mapping.get("dest"))
                        except (TypeError, ValueError):
                            continue
                        output_settings = channel_settings["playback"]
                        if output_index < 0 or output_index >= len(output_settings):
                            continue
                        sources = mapping.get("sources")
                        if not isinstance(sources, list) or not sources:
                            continue
                        inversions = [
                            source.get("inverted") is True
                            for source in sources
                            if isinstance(source, dict)
                        ]
                        if len(inversions) != len(sources) or len(set(inversions)) > 1:
                            output_settings[output_index]["phase_known"] = False
                        elif inversions:
                            output_settings[output_index]["phase_inverted"] ^= inversions[0]
                active_side = "playback"
                continue

            if stage_type != "filter" or stage.get("bypassed") is True:
                continue
            selected_channels = stage.get("channels")
            filter_names = stage.get("names")
            if not isinstance(selected_channels, list) or not isinstance(filter_names, list):
                continue
            settings_for_side = channel_settings[active_side]
            for channel_value in selected_channels:
                try:
                    channel_index = int(channel_value)
                except (TypeError, ValueError):
                    continue
                if channel_index < 0 or channel_index >= len(settings_for_side):
                    continue
                channel_settings_entry = settings_for_side[channel_index]
                for filter_name in filter_names:
                    filter_config = filters.get(str(filter_name))
                    if not isinstance(filter_config, dict):
                        continue
                    filter_type = str(filter_config.get("type") or "").strip().casefold()
                    parameters = filter_config.get("parameters")
                    if not isinstance(parameters, dict):
                        continue
                    if filter_type == "biquad":
                        frequency_hz = cls._load_audio_metric_number(parameters.get("freq"))
                        filter_kind = str(parameters.get("type") or "").strip()
                        if frequency_hz is not None and filter_kind:
                            channel_settings_entry["frequency_filters"].append(
                                {
                                    "kind": filter_kind,
                                    "frequency_hz": frequency_hz,
                                    "gain_db": cls._load_audio_metric_number(parameters.get("gain")),
                                    "q": cls._load_audio_metric_number(parameters.get("q")),
                                }
                            )
                    elif filter_type == "delay":
                        delay_ms = cls._load_camilladsp_delay_ms(parameters, sample_rate)
                        if delay_ms is None:
                            channel_settings_entry["delay_ms"] = None
                        elif channel_settings_entry["delay_ms"] is not None:
                            channel_settings_entry["delay_ms"] += delay_ms
                    if parameters.get("inverted") is True:
                        channel_settings_entry["phase_inverted"] = not channel_settings_entry["phase_inverted"]

        for settings in channel_settings.values():
            for channel_settings_entry in settings:
                phase_known = channel_settings_entry.pop("phase_known")
                phase_inverted = channel_settings_entry.pop("phase_inverted")
                channel_settings_entry["phase_deg"] = (
                    None if not phase_known else 180.0 if phase_inverted else 0.0
                )

        return channel_settings

    @classmethod
    def _load_camilladsp_metadata(cls) -> dict[str, str]:
        websocket_url = str(os.getenv("WEBPLAYER_CAMILLADSP_WS_URL") or "ws://127.0.0.1:1234").strip()
        try:
            from websockets.exceptions import WebSocketException
            from websockets.sync.client import connect
        except ImportError:
            return {
                "audio_aux_dsp_status": "websockets_package_missing",
                "audio_aux_dsp_data": "{}",
            }

        def load_response(websocket: Any, command: str) -> Any:
            websocket.send(json.dumps(command))
            response = json.loads(websocket.recv(timeout=2))
            command_result = response.get(command) if isinstance(response, dict) else None
            if not isinstance(command_result, dict) or command_result.get("result") != "Ok":
                raise ValueError(f"CamillaDSP command failed: {command}")
            return command_result.get("value")

        try:
            with connect(
                websocket_url,
                open_timeout=2,
                close_timeout=1,
                max_size=2_000_000,
                proxy=None,
            ) as websocket:
                signal_levels = load_response(websocket, "GetSignalLevels")
                config_value = load_response(websocket, "GetConfigJson")
                fader_values = load_response(websocket, "GetFaders")
            if not isinstance(signal_levels, dict):
                raise ValueError("CamillaDSP signal-level response is invalid")
            config = json.loads(config_value) if isinstance(config_value, str) else config_value
            if not isinstance(config, dict):
                raise ValueError("CamillaDSP config response is invalid")
            if not isinstance(fader_values, list):
                raise ValueError("CamillaDSP fader response is invalid")

            devices = config.get("devices") if isinstance(config.get("devices"), dict) else {}
            capture_device = devices.get("capture") if isinstance(devices.get("capture"), dict) else {}
            playback_device = devices.get("playback") if isinstance(devices.get("playback"), dict) else {}
            capture_values = [
                signal_levels.get("capture_rms"),
                signal_levels.get("capture_peak"),
            ]
            playback_values = [
                signal_levels.get("playback_rms"),
                signal_levels.get("playback_peak"),
            ]
            capture_channel_count = max(
                [int(capture_device.get("channels") or 0)]
                + [len(values) for values in capture_values if isinstance(values, list)]
            )
            playback_channel_count = max(
                [int(playback_device.get("channels") or 0)]
                + [len(values) for values in playback_values if isinstance(values, list)]
            )
            scope_input_source = str(
                os.getenv("WEBPLAYER_SCOPE_INPUT_SOURCE") or "alde-dsp-input.monitor"
            ).strip()
            PLAYBACK_SCOPE_SERVICE.configure(
                playback_device=playback_device,
                input_source=scope_input_source,
            )
            channel_settings = cls._load_camilladsp_channel_settings(
                config=config,
                capture_channel_count=capture_channel_count,
                playback_channel_count=playback_channel_count,
            )
            sample_rate = cls._load_audio_metric_number(devices.get("samplerate"))
            if sample_rate is None:
                sample_rate = cls._load_audio_metric_number(devices.get("capture_samplerate"))
            for channel_index, settings in enumerate(channel_settings["playback"]):
                path_label, response_curve, response_status = cls._load_camilladsp_playback_response(
                    config=config,
                    channel_index=channel_index,
                    sample_rate=sample_rate,
                    fader_values=fader_values,
                )
                settings["path_label"] = path_label or f"Output {channel_index + 1}"
                settings["response_curve"] = response_curve
                settings["response_status"] = response_status

            def load_level_rows(side: str, channel_count: int) -> list[dict[str, float | None]]:
                rms_values = signal_levels.get(f"{side}_rms")
                peak_values = signal_levels.get(f"{side}_peak")
                rms_values = rms_values if isinstance(rms_values, list) else []
                peak_values = peak_values if isinstance(peak_values, list) else []
                return [
                    {
                        "rms_db": cls._load_audio_metric_number(rms_values[index])
                        if index < len(rms_values)
                        else None,
                        "peak_db": cls._load_audio_metric_number(peak_values[index])
                        if index < len(peak_values)
                        else None,
                    }
                    for index in range(channel_count)
                ]

            return {
                "audio_aux_dsp_status": "ready",
                "audio_aux_dsp_data": json.dumps(
                    {
                        "capture": load_level_rows("capture", capture_channel_count),
                        "playback": load_level_rows("playback", playback_channel_count),
                        "capture_settings": channel_settings["capture"],
                        "playback_settings": channel_settings["playback"],
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            }
        except TimeoutError:
            status = "camilladsp_timeout"
        except OSError:
            status = "camilladsp_unavailable"
        except WebSocketException:
            status = "camilladsp_connection_error"
        except (TypeError, ValueError):
            status = "camilladsp_invalid_response"
        return {
            "audio_aux_dsp_status": status,
            "audio_aux_dsp_data": "{}",
        }

    def _load_system_volume_metadata(self) -> dict[str, str]:
        volume_percent = ""
        volume_controller = ""
        if shutil.which("wpctl") is not None:
            try:
                completed = subprocess.run(
                    ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            volume_match = re.search(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)", str(completed.stdout if completed else ""))
            if completed and completed.returncode == 0 and volume_match:
                volume_percent = f"{round(float(volume_match.group(1)) * 100):g}"
                volume_controller = "wpctl"

        if not volume_percent and shutil.which("pactl") is not None:
            try:
                completed = subprocess.run(
                    ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            volume_match = re.search(r"([0-9]+(?:\.[0-9]+)?)%", str(completed.stdout if completed else ""))
            if completed and completed.returncode == 0 and volume_match:
                volume_percent = f"{round(float(volume_match.group(1))):g}"
                volume_controller = "pactl"

        if not volume_percent:
            return {}

        if volume_controller == "wpctl":
            volume_sink = self._run_pactl_command(["get-default-sink"]) or "@DEFAULT_AUDIO_SINK@"
        else:
            volume_sink = self._run_pactl_command(["get-default-sink"]) or "@DEFAULT_SINK@"
        return {
            "system_volume_percent": volume_percent,
            "volume_target": "sink",
            "volume_sink": volume_sink,
            "volume_controller": volume_controller,
        }

    def _load_audio_route_metadata_stdout(self) -> str:
        metadata = self._load_audio_route_metadata()
        metadata.update(self._load_system_volume_metadata())
        metadata.update(self._load_audio_aux_routing_metadata())
        metadata.update(self._load_camilladsp_metadata())
        return "".join(f"{key}={value}\n" for key, value in metadata.items())

    def _load_play_retry_command(
        self,
        *,
        player_selector: str,
        wait_for_player_s: int,
        play_attempts: int,
        require_playing: bool,
    ) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=wait_for_player_s)
            + "attempt_counter=0; status=unknown; "
            f"while [ \"$attempt_counter\" -lt {play_attempts} ]; do "
            "playerctl -p \"$selected_player\" play 2>/dev/null || true; "
            "status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown); "
            "if [ \"$status\" = \"Playing\" ]; then break; fi; "
            "sleep 1; attempt_counter=$((attempt_counter+1)); "
            "done; "
            + (
                "if [ \"$status\" != \"Playing\" ]; then echo 'error=playback_not_started'; exit 2; fi; "
                if require_playing
                else ""
            )
            + "echo player=$selected_player; "
            "echo status=$status; "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo artist=$(playerctl -p \"$selected_player\" metadata xesam:artist 2>/dev/null || true); "
            "echo album=$(playerctl -p \"$selected_player\" metadata xesam:album 2>/dev/null || true); "
            "echo art_url=$(playerctl -p \"$selected_player\" metadata mpris:artUrl 2>/dev/null || true); "
            "echo track_url=$(playerctl -p \"$selected_player\" metadata xesam:url 2>/dev/null || true); "
            + self._load_player_audio_metadata_command()
            + "echo play_attempts_used=$attempt_counter;"
        )

    def _load_play_command(
        self,
        *,
        player_selector: str,
        wait_for_player_s: int,
        play_attempts: int,
        cdp_autoclick: bool,
        cdp_port: int,
        cdp_click_timeout_s: int,
    ) -> str:
        final_play_command = self._load_play_retry_command(
            player_selector=player_selector,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            require_playing=True,
        )

        if not cdp_autoclick:
            return final_play_command

        cdp_click_command = self._load_cdp_autoclick_command(
            action="generic",
            query="",
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )

        return (
            cdp_click_command
            + "cdp_exit_code=$?; if [ \"$cdp_exit_code\" -ne 0 ]; then echo cdp_click_warning=failed; fi; "
            + final_play_command
        )

    def _load_pause_command(self, *, player_selector: str) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "if ! playerctl -p \"$selected_player\" pause 2>/dev/null; then echo 'error=pause_failed'; exit 2; fi; "
            "echo player=$selected_player; "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown); "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo artist=$(playerctl -p \"$selected_player\" metadata xesam:artist 2>/dev/null || true); "
            "echo album=$(playerctl -p \"$selected_player\" metadata xesam:album 2>/dev/null || true);"
        )

    def _load_seek_command(self, *, player_selector: str, seek_spec: str) -> str:
        quoted_seek_spec = shlex.quote(seek_spec)
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + f"if ! playerctl -p \"$selected_player\" position {quoted_seek_spec} 2>/dev/null; then "
            "echo 'error=seek_failed'; exit 2; fi; "
            "echo player=$selected_player; "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown); "
            "echo position_seconds=$(playerctl -p \"$selected_player\" position 2>/dev/null || echo unknown); "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo artist=$(playerctl -p \"$selected_player\" metadata xesam:artist 2>/dev/null || true); "
            "echo album=$(playerctl -p \"$selected_player\" metadata xesam:album 2>/dev/null || true);"
        )

    def _load_stop_command(self, *, player_selector: str) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "playerctl -p \"$selected_player\" stop 2>/dev/null || playerctl -p \"$selected_player\" pause 2>/dev/null || true; "
            "echo player=$selected_player; "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown);"
        )

    def _load_forward_command(self, *, player_selector: str, arguments: dict[str, Any] | None = None) -> str:
        if self._browser_queue_track_ids and self._browser_queue_index + 1 < len(self._browser_queue_track_ids):
            self._browser_queue_index += 1
            return self._load_browser_queue_target_command(
                player_selector=player_selector,
                arguments=arguments or {},
            )
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "playerctl -p \"$selected_player\" next 2>/dev/null || true; "
            "echo player=$selected_player; "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown);"
        )

    def _load_backward_command(self, *, player_selector: str, arguments: dict[str, Any] | None = None) -> str:
        if self._browser_queue_track_ids and self._browser_queue_index > 0:
            self._browser_queue_index -= 1
            return self._load_browser_queue_target_command(
                player_selector=player_selector,
                arguments=arguments or {},
            )
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "playerctl -p \"$selected_player\" previous 2>/dev/null || true; "
            "echo player=$selected_player; "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown);"
        )

    def _load_browser_queue_target_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        track_id = self._browser_queue_track_ids[self._browser_queue_index]
        return self._load_open_url_and_play_command(
            target_url=f"https://listen.tidal.com/track/{quote(track_id, safe='')}",
            player_selector=player_selector,
            wait_for_page_s=self._load_bounded_int(
                arguments.get("wait_for_page_s"),
                default_value=4,
                minimum=0,
                maximum=120,
            ),
            wait_for_player_s=self._load_bounded_int(
                arguments.get("wait_for_player_s"),
                default_value=15,
                minimum=0,
                maximum=120,
            ),
            play_attempts=self._load_bounded_int(
                arguments.get("play_attempts"),
                default_value=8,
                minimum=1,
                maximum=20,
            ),
            cdp_action="track",
            cdp_query=track_id,
            cdp_autoclick=True,
            cdp_port=self._load_bounded_int(
                arguments.get("cdp_port"),
                default_value=9222,
                minimum=1,
                maximum=65535,
            ),
            cdp_click_timeout_s=self._load_bounded_int(
                arguments.get("cdp_click_timeout_s"),
                default_value=12,
                minimum=1,
                maximum=120,
            ),
        )

    def _load_like_current_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=12,
            minimum=1,
            maximum=120,
        )

        cdp_like_command = self._load_cdp_autoclick_command(
            action="like",
            query="",
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )
        now_playing_command = self._load_now_playing_command(
            player_selector=player_selector,
            arguments={
                "cdp_probe_quality": True,
                "cdp_port": cdp_port,
                "cdp_probe_timeout_s": min(3, cdp_click_timeout_s),
                "cdp_probe_debug": False,
            },
        )

        return (
            "cdp_like_output=$(\n"
            + cdp_like_command
            + "); cdp_exit_code=$?; printf '%s\\n' \"$cdp_like_output\"; "
            + now_playing_command
            + "if printf '%s\\n' \"$cdp_like_output\" | grep -qE '^cdp_like_state=(liked|already_liked)$'; then echo 'liked=true'; echo 'liked_state=liked'; fi; "
        )

    def _load_toggle_like_current_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=12,
            minimum=1,
            maximum=120,
        )

        cdp_like_command = self._load_cdp_autoclick_command(
            action="like_toggle",
            query="",
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )
        now_playing_command = self._load_now_playing_command(
            player_selector=player_selector,
            arguments={
                "cdp_probe_quality": True,
                "cdp_port": cdp_port,
                "cdp_probe_timeout_s": min(3, cdp_click_timeout_s),
                "cdp_probe_debug": False,
            },
        )

        return (
            "cdp_like_output=$(\n"
            + cdp_like_command
            + "); cdp_exit_code=$?; printf '%s\\n' \"$cdp_like_output\"; "
            + now_playing_command
            + "if printf '%s\\n' \"$cdp_like_output\" | grep -qE '^cdp_like_state=unliked$'; then echo 'liked=false'; echo 'liked_state=unliked'; fi; "
        )

    def _load_now_playing_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        cdp_probe_quality = self._load_boolean_argument(arguments.get("cdp_probe_quality"), default_value=False)
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_probe_timeout_s = self._load_bounded_int(
            arguments.get("cdp_probe_timeout_s"),
            default_value=2,
            minimum=1,
            maximum=10,
        )
        cdp_probe_debug = self._load_boolean_argument(arguments.get("cdp_probe_debug"), default_value=False)

        cdp_probe_snippet = ""
        if cdp_probe_quality:
            cdp_probe_command = self._load_cdp_now_playing_probe_command(
                cdp_port=cdp_port,
                cdp_probe_timeout_s=cdp_probe_timeout_s,
                cdp_probe_debug=cdp_probe_debug,
            )
            if cdp_probe_debug:
                cdp_probe_snippet = "echo cdp_probe_debug_active=1; " + cdp_probe_command + " "
            else:
                cdp_probe_snippet = cdp_probe_command + " 2>/dev/null; "

        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "echo player=$selected_player; "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown); "
            "echo position_seconds=$(LC_ALL=C playerctl -p \"$selected_player\" position 2>/dev/null || true); "
            "duration_us=$(LC_ALL=C playerctl -p \"$selected_player\" metadata mpris:length 2>/dev/null || true); "
            "echo duration_seconds=$(LC_ALL=C awk -v duration_us=\"$duration_us\" 'BEGIN { if (duration_us ~ /^[0-9]+$/ && duration_us > 0) printf \"%.3f\", duration_us / 1000000 }' 2>/dev/null); "
            "current_volume=$(LC_ALL=C playerctl -p \"$selected_player\" volume 2>/dev/null || true); "
            "echo volume_percent=$(LC_ALL=C awk -v volume=\"$current_volume\" 'BEGIN { if (volume == \"\") exit 1; printf \"%.0f\", volume*100 }' 2>/dev/null || true); "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo artist=$(playerctl -p \"$selected_player\" metadata xesam:artist 2>/dev/null || true); "
            "echo album=$(playerctl -p \"$selected_player\" metadata xesam:album 2>/dev/null || true); "
            "echo art_url=$(playerctl -p \"$selected_player\" metadata mpris:artUrl 2>/dev/null || true); "
            "echo track_url=$(playerctl -p \"$selected_player\" metadata xesam:url 2>/dev/null || true); "
            + self._load_player_audio_metadata_command()
            + cdp_probe_snippet
        )

    def _load_cdp_navigate_command(self, *, target_url: str, cdp_port: int) -> str:
        env_prefix = (
            "CDP_TARGET_URL="
            + shlex.quote(str(target_url or "").strip())
            + f" CDP_PORT={int(cdp_port)} "
        )
        cdp_script = r'''python3 - <<'PY'
import asyncio
import json
import os
import urllib.request

try:
    import websockets
except Exception:
    print('error=cdp_websockets_missing')
    raise SystemExit(10)

cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
target_url = str(os.environ.get('CDP_TARGET_URL') or '').strip()


def load_page_websocket_url() -> str:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=5) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))

    page_targets = [
        target
        for target in targets
        if str(target.get('type') or '') == 'page'
        and str(target.get('webSocketDebuggerUrl') or '').strip()
    ]
    tidal_targets = [
        target
        for target in page_targets
        if 'tidal.com' in str(target.get('url') or '').casefold()
    ]
    for target in tidal_targets + page_targets:
        websocket_url = str(target.get('webSocketDebuggerUrl') or '').strip()
        if websocket_url:
            return websocket_url
    raise RuntimeError('cdp_no_page_target')


async def run() -> int:
    websocket_url = load_page_websocket_url()
    async with websockets.connect(websocket_url, max_size=2_000_000) as websocket:
        await websocket.send(
            json.dumps(
                {
                    'id': 1,
                    'method': 'Page.navigate',
                    'params': {'url': target_url},
                }
            )
        )
        while True:
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=8)
            message = json.loads(raw_message)
            if message.get('id') != 1:
                continue
            if isinstance(message.get('error'), dict):
                raise RuntimeError(str(message['error'].get('message') or 'cdp_navigation_failed'))
            print('cdp_navigate_ok=true')
            return 0
try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_navigate_exception:' + str(exc).replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))
    raise SystemExit(13)
PY
'''
        return env_prefix + cdp_script + " "

    def _load_browser_open_command(
        self,
        *,
        target_url: str,
        log_file_path: str,
        allow_xdg_open_fallback: bool,
        cdp_port: int = 9222,
    ) -> str:
        normalized_cdp_port = self._load_bounded_int(
            cdp_port,
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        quoted_url = shlex.quote(target_url)
        quoted_log_path = shlex.quote(log_file_path)
        if allow_xdg_open_fallback:
            browser_discovery = (
                "if command -v chromium >/dev/null 2>&1; then BROWSER_CMD=chromium; "
                "elif command -v chromium-browser >/dev/null 2>&1; then BROWSER_CMD=chromium-browser; "
                "elif command -v xdg-open >/dev/null 2>&1; then xdg-open "
                + quoted_url
                + " >/dev/null 2>&1; echo opened_url="
                + quoted_url
                + "; echo status=started; exit 0; "
                "else echo 'error=no_browser'; exit 1; fi; "
            )
        else:
            browser_discovery = (
                "if command -v chromium >/dev/null 2>&1; then BROWSER_CMD=chromium; "
                "elif command -v chromium-browser >/dev/null 2>&1; then BROWSER_CMD=chromium-browser; "
                "else echo 'error=no_browser'; exit 1; fi; "
            )

        launch_command = (
            "profile_mode=user_profile; "
            "nohup \"$BROWSER_CMD\" --ozone-platform=wayland --enable-features=UseOzonePlatform,HardwareMediaKeyHandling "
            f"--remote-debugging-port={normalized_cdp_port} --remote-debugging-address=127.0.0.1 --remote-allow-origins=* "
            "--no-first-run --no-default-browser-check --new-window "
            + quoted_url
            + " >"
            + quoted_log_path
            + " 2>&1 & "
            "sleep 1; "
            f"if command -v curl >/dev/null 2>&1 && curl -sS --max-time 2 http://127.0.0.1:{normalized_cdp_port}/json/version >/dev/null 2>&1; then cdp_port_ready=true; else "
            "profile_mode=fresh_profile_fallback; "
            "CDP_PROFILE_DIR=\"/tmp/webplayer_mcp_cdp_profile_$UID_NUM\"; mkdir -p \"$CDP_PROFILE_DIR\"; "
            "nohup \"$BROWSER_CMD\" --ozone-platform=wayland --enable-features=UseOzonePlatform,HardwareMediaKeyHandling "
            f"--remote-debugging-port={normalized_cdp_port} --remote-debugging-address=127.0.0.1 --remote-allow-origins=* "
            "--no-first-run --no-default-browser-check --user-data-dir=\"$CDP_PROFILE_DIR\" --new-window "
            + quoted_url
            + " >"
            + quoted_log_path
            + " 2>&1 & "
            "sleep 1; "
            f"if command -v curl >/dev/null 2>&1 && curl -sS --max-time 2 http://127.0.0.1:{normalized_cdp_port}/json/version >/dev/null 2>&1; then cdp_port_ready=true; fi; "
            "fi; "
        )

        return (
            browser_discovery
            + "UID_NUM=$(id -u); export XDG_RUNTIME_DIR=\"/run/user/$UID_NUM\"; "
            "if [ -S \"$XDG_RUNTIME_DIR/bus\" ]; then export DBUS_SESSION_BUS_ADDRESS=\"unix:path=$XDG_RUNTIME_DIR/bus\"; fi; "
            "if [ -S \"$XDG_RUNTIME_DIR/wayland-0\" ]; then export WAYLAND_DISPLAY=wayland-0; "
            "elif [ -S \"$XDG_RUNTIME_DIR/wayland-1\" ]; then export WAYLAND_DISPLAY=wayland-1; fi; "
            "cdp_port_ready=false; profile_mode=unresolved; "
            f"if command -v curl >/dev/null 2>&1 && curl -sS --max-time 2 http://127.0.0.1:{normalized_cdp_port}/json/version >/dev/null 2>&1; then "
            + self._load_cdp_navigate_command(target_url=target_url, cdp_port=normalized_cdp_port)
            + " cdp_navigate_exit_code=$?; if [ \"$cdp_navigate_exit_code\" -ne 0 ]; then echo 'error=cdp_navigate_failed'; exit 1; fi; profile_mode=existing_cdp; cdp_port_ready=true; "
            + "else "
            + launch_command
            + "fi; "
            "echo opened_url="
            + quoted_url
            + "; "
            "echo profile_mode=$profile_mode; "
            "echo status=started; "
            "echo cdp_port_ready=$cdp_port_ready;"
        )

    def _load_library_snapshot_payload(self, stdout_payload: str) -> dict[str, Any]:
        metadata: dict[str, str] = {}
        for line in str(stdout_payload or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            metadata[str(key).strip()] = str(value).strip()

        track_object_list: list[dict[str, Any]] = []
        snapshot_json_base64 = str(metadata.get("snapshot_json_base64") or "").strip()
        if snapshot_json_base64:
            try:
                decoded_payload = base64.b64decode(snapshot_json_base64.encode("ascii"))
                loaded_payload = json.loads(decoded_payload.decode("utf-8", errors="replace"))
                if isinstance(loaded_payload, list):
                    for item in loaded_payload:
                        if isinstance(item, dict):
                            track_object_list.append(dict(item))
            except Exception:
                track_object_list = []

        return {
            "snapshot_ok": bool(track_object_list),
            "snapshot_count": len(track_object_list),
            "tracks": track_object_list,
        }

    def _load_cdp_autoclick_command(
        self,
        *,
        action: str,
        query: str,
        cdp_port: int,
        cdp_click_timeout_s: int,
    ) -> str:
        env_prefix = (
            "CDP_ACTION="
            + shlex.quote(str(action or "generic"))
            + " CDP_QUERY="
            + shlex.quote(str(query or ""))
            + f" CDP_PORT={int(cdp_port)} CDP_CLICK_TIMEOUT_S={int(cdp_click_timeout_s)} "
        )
        cdp_script = '''python3 - <<'PY'
import asyncio
import json
import os
import sys
import urllib.request

try:
    import websockets
except Exception:
    print('error=cdp_websockets_missing')
    raise SystemExit(10)

action = str(os.environ.get('CDP_ACTION') or 'generic').strip().lower()
query = str(os.environ.get('CDP_QUERY') or '').strip()
cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
timeout_s = int(str(os.environ.get('CDP_CLICK_TIMEOUT_S') or '12'))

def load_target_url() -> str:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=5) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))
    page_targets = [target for target in targets if str(target.get('type') or '') == 'page']
    tidal_targets = [target for target in page_targets if 'tidal.com' in str(target.get('url') or '')]
    if not tidal_targets:
        raise RuntimeError('cdp_no_tidal_target')
    if action == 'search' and query:
        lowered_query = query.casefold()
        for target in tidal_targets:
            if lowered_query in str(target.get('url') or '').casefold():
                return str(target.get('webSocketDebuggerUrl') or '')
    if action in {'like', 'like_toggle'}:
        track_targets = [target for target in tidal_targets if '/track/' in str(target.get('url') or '')]
        if track_targets:
            return str(track_targets[0].get('webSocketDebuggerUrl') or '')
    return str(tidal_targets[0].get('webSocketDebuggerUrl') or '')

def load_evaluate_expression(action_name: str, search_query: str) -> str:
    action_payload = json.dumps(action_name)
    query_payload = json.dumps(search_query)
    return f"""
(() => {{
  const action = {action_payload};
  const query = {query_payload};
  const isVisible = (element) => {{
    if (!element) return false;
    const style = window.getComputedStyle(element);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 4 && rect.height > 4;
  }};
  const clickElement = (element, selector) => {{
    element.scrollIntoView({{ block: 'center', inline: 'center', behavior: 'instant' }});
    element.dispatchEvent(new MouseEvent('mousemove', {{ bubbles: true, cancelable: true, view: window }}));
    element.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true, view: window }}));
    element.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, cancelable: true, view: window }}));
    element.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, view: window }}));
    return {{ clicked: true, selector }};
  }};
    const loadElementText = (element) => {{
        if (!element) return '';
        return [
            element.getAttribute('aria-label') || '',
            element.getAttribute('title') || '',
            element.getAttribute('data-test') || '',
            element.getAttribute('data-testid') || '',
            element.textContent || '',
        ].join(' ').toLowerCase();
    }};
  if (action === 'search' && query) {{
    const searchInput = document.querySelector('input[type="search"],input[placeholder*="Search" i],input[aria-label*="Search" i],input[name*="search" i]');
    if (searchInput) {{
      searchInput.focus();
      searchInput.value = query;
      searchInput.dispatchEvent(new InputEvent('input', {{ bubbles: true }}));
      searchInput.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', bubbles: true }}));
      searchInput.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Enter', code: 'Enter', bubbles: true }}));
    }}
  }}
    if (action === 'like' || action === 'like_toggle') {{
        const addLikePatterns = [
            /add to (favorites|favourites|library|collection)/i,
            /add to my collection/i,
            /like this track/i,
            /favorite this track/i,
            /save track/i,
            /zu favoriten/i,
            /zur sammlung/i,
            /als favorit/i,
        ];
        const removeLikePatterns = [
            /remove from (favorites|favourites|library|collection)/i,
            /remove from my collection/i,
            /unlike this track/i,
            /unlike/i,
            /aus favoriten/i,
            /aus der sammlung/i,
            /entfernen/i,
        ];
        const likeSelectors = [
            'button[data-test*="favorite" i]',
            'button[data-test*="favourite" i]',
            'button[data-testid*="favorite" i]',
            'button[data-testid*="favourite" i]',
            'button[aria-label*="favorite" i]',
            'button[aria-label*="favourite" i]',
            'button[aria-label*="favorit" i]',
            'button[aria-label*="collection" i]',
            'button[aria-label*="my collection" i]',
            'button[aria-label*="like" i]',
            'button[title*="favorite" i]',
            'button[title*="favourite" i]',
            'button[title*="favorit" i]',
            'button[title*="collection" i]',
            'button[title*="my collection" i]',
            'button[title*="like" i]',
            '[role="button"][aria-label*="favorite" i]',
            '[role="button"][aria-label*="favourite" i]',
            '[role="button"][aria-label*="favorit" i]',
            '[role="button"][aria-label*="collection" i]',
            '[role="button"][aria-label*="like" i]',
            'button[class*="heart" i]',
            '[role="button"][class*="heart" i]',
        ];
        let fallbackButton = null;
        for (const selector of likeSelectors) {{
            const elements = Array.from(document.querySelectorAll(selector));
            for (const element of elements) {{
                if (!isVisible(element)) continue;
                const elementText = loadElementText(element);
                if (removeLikePatterns.some((pattern) => pattern.test(elementText))) {{
                    if (action === 'like_toggle') {{
                        return Object.assign(clickElement(element, selector), {{ like_state: 'unliked' }});
                    }}
                    return {{ clicked: true, selector, like_state: 'already_liked' }};
                }}
                if (addLikePatterns.some((pattern) => pattern.test(elementText))) {{
                    return Object.assign(clickElement(element, selector), {{ like_state: 'liked' }});
                }}
                if (!fallbackButton) {{
                    fallbackButton = {{ element, selector }};
                }}
            }}
        }}
        if (fallbackButton) {{
            const fallbackState = action === 'like_toggle' ? 'toggled_unverified' : 'liked_unverified';
            return Object.assign(clickElement(fallbackButton.element, fallbackButton.selector + ':fallback'), {{ like_state: fallbackState }});
        }}
        return {{ clicked: false, selector: '', like_state: 'not_found' }};
    }}
    if (action === 'track' && query) {{
        const trackTitle = Array.from(document.querySelectorAll('[data-test="table-row-title"][data-id], [data-test="table-cell-title"][data-id]'))
            .find((element) => element.getAttribute('data-id') === query);
        let trackContainer = trackTitle;
        for (let depth = 0; trackContainer && depth < 8; depth += 1, trackContainer = trackContainer.parentElement) {{
            const trackButton = trackContainer.querySelector('button[data-test="play-button"], button[data-testid="play-button"]');
            if (trackButton && isVisible(trackButton)) {{
                return clickElement(trackButton, 'track-row-play-button:' + query);
            }}
        }}
        return {{ clicked: false, selector: '' }};
    }}
  const selectors = [
        'button[data-test="play-button"]',
        'button[data-testid="play-button"]',
        '[role="button"][data-test="play-button"]',
        'button[data-type="button__play"]',
        '[role="button"][data-type="button__play"]',
    'button[aria-label*="Play" i]',
        'button[aria-label*="Abspiel" i]',
    'button[title*="Play" i]',
        'button[title*="Abspiel" i]',
    '[role="button"][aria-label*="Play" i]',
        '[role="button"][aria-label*="Abspiel" i]',
    'a[href*="/track/"] button',
    '[class*="play" i] button',
  ];
  for (const selector of selectors) {{
    const elements = Array.from(document.querySelectorAll(selector));
    for (const element of elements) {{
      if (!isVisible(element)) continue;
      return clickElement(element, selector);
    }}
  }}
  const fallbackLinks = Array.from(document.querySelectorAll('a[href*="/track/"],a[href*="/album/"],a[href*="/playlist/"]'));
  for (const link of fallbackLinks) {{
    if (!isVisible(link)) continue;
    return clickElement(link, 'fallback_link');
  }}
  if (document.body) {{
    document.body.focus();
    document.body.dispatchEvent(new KeyboardEvent('keydown', {{ key: ' ', code: 'Space', keyCode: 32, which: 32, bubbles: true }}));
    document.body.dispatchEvent(new KeyboardEvent('keyup', {{ key: ' ', code: 'Space', keyCode: 32, which: 32, bubbles: true }}));
    document.body.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'k', code: 'KeyK', keyCode: 75, which: 75, bubbles: true }}));
    document.body.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'k', code: 'KeyK', keyCode: 75, which: 75, bubbles: true }}));
    return {{ clicked: true, selector: 'keyboard_fallback' }};
  }}
  return {{ clicked: false, selector: '' }};
}})()
"""

async def run() -> int:
    ws_url = load_target_url()
    if not ws_url:
        print('error=cdp_no_ws_url')
        return 11

    expression = load_evaluate_expression(action, query)
    async with websockets.connect(ws_url, max_size=8_000_000) as websocket:
        message_id = 0
        async def send_method(method_name: str, params: dict | None = None) -> dict:
            nonlocal message_id
            message_id += 1
            payload = {'id': message_id, 'method': method_name}
            if params:
                payload['params'] = params
            await websocket.send(json.dumps(payload))
            while True:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                if message.get('id') == message_id:
                    return message

        await send_method('Page.enable')
        await send_method('Runtime.enable')

        for _ in range(max(1, timeout_s)):
            response = await send_method(
                'Runtime.evaluate',
                {
                    'expression': expression,
                    'returnByValue': True,
                    'awaitPromise': True,
                },
            )
            eval_result = ((response.get('result') or {}).get('result') or {}).get('value') or {}
            if isinstance(eval_result, dict) and bool(eval_result.get('clicked')):
                print('cdp_clicked=true')
                print('cdp_selector=' + str(eval_result.get('selector') or ''))
                like_state = str(eval_result.get('like_state') or '').strip()
                if like_state:
                    print('cdp_like_state=' + like_state)
                return 0
            await asyncio.sleep(1)

    print('error=cdp_click_not_found')
    return 12

try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_click_exception:' + str(exc))
    raise SystemExit(13)
PY
'''
        return env_prefix + cdp_script + " "

    def _load_cdp_now_playing_probe_command(self, *, cdp_port: int, cdp_probe_timeout_s: int, cdp_probe_debug: bool) -> str:
        debug_flag = "1" if cdp_probe_debug else "0"
        env_prefix = f"CDP_PORT={int(cdp_port)} CDP_PROBE_TIMEOUT_S={int(cdp_probe_timeout_s)} CDP_PROBE_DEBUG={debug_flag} "
        cdp_script = '''python3 - <<'PY'
import asyncio
import base64
import json
import os
import re
import time
import urllib.request

try:
    import websockets
except Exception:
    raise SystemExit(0)

cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
timeout_s = int(str(os.environ.get('CDP_PROBE_TIMEOUT_S') or '2'))
probe_debug = str(os.environ.get('CDP_PROBE_DEBUG') or '0').strip() in {'1', 'true', 'yes', 'on'}

def load_target_urls() -> list[tuple[str, str]]:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=2) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))
    page_targets = [target for target in targets if str(target.get('type') or '') == 'page' and str(target.get('webSocketDebuggerUrl') or '')]
    tidal_targets = [target for target in page_targets if 'tidal.com' in str(target.get('url') or '')]
    track_targets = [target for target in tidal_targets if '/track/' in str(target.get('url') or '')]
    ordered_targets = track_targets + [target for target in tidal_targets if target not in track_targets]
    if not ordered_targets:
        ordered_targets = page_targets

    websocket_targets: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for target in ordered_targets[:8]:
        ws_url = str(target.get('webSocketDebuggerUrl') or '')
        target_url = str(target.get('url') or '')
        if not ws_url or ws_url in seen_urls:
            continue
        seen_urls.add(ws_url)
        websocket_targets.append((ws_url, target_url))
    if probe_debug:
        print('cdp_probe_ws_count=' + str(len(websocket_targets)))
    return websocket_targets

def load_tidal_service_worker_ws_url() -> str:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=2) as response:
            targets = json.loads(response.read().decode('utf-8', errors='replace'))
    except Exception:
        return ''

    for target in targets:
        target_type = str(target.get('type') or '').strip().lower()
        target_url = str(target.get('url') or '').strip().lower()
        ws_url = str(target.get('webSocketDebuggerUrl') or '').strip()
        if target_type != 'service_worker' or not ws_url:
            continue
        if 'tidal.com/service-worker' in target_url:
            return ws_url
    return ''

def load_quality_value(raw_text: str) -> str:
    badge_matches = re.findall(r'quality-badge-([a-z0-9_-]+)', raw_text, flags=re.IGNORECASE)
    for badge_value in badge_matches:
        normalized_badge = str(badge_value or '').strip().casefold()
        if not normalized_badge:
            continue
        if 'atmos' in normalized_badge or 'dolby' in normalized_badge:
            return 'ATMOS'
        if normalized_badge in {'max', 'master', 'hires', 'hi-res'}:
            return 'MAX'
        if normalized_badge in {'lossless', 'flac', 'alac', 'cd'}:
            return 'LOSSLESS'
        if normalized_badge in {'high', 'aac', '320', '256'}:
            return 'HIGH'
        if normalized_badge in {'normal', 'low', '128', '96'}:
            return 'NORMAL'

    patterns = [
        (r'\\b(DOLBY\\s+ATMOS|ATMOS)\\b', 'ATMOS'),
        (r'\\b(MAX|HI[ -]?RES|MASTER|24\\s*BIT|96\\s*KHZ|192\\s*KHZ)\\b', 'MAX'),
        (r'\\b(LOSSLESS|FLAC|ALAC|CD\\s*QUALITY|CD-QUALITY)\\b', 'LOSSLESS'),
        (r'\\b(HIGH|AAC\\s*320|AAC\\s*256|320\\s*K(?:BPS)?|256\\s*K(?:BPS)?)\\b', 'HIGH'),
        (r'\\b(NORMAL|LOW|128\\s*K(?:BPS)?|96\\s*K(?:BPS)?)\\b', 'NORMAL'),
    ]
    for pattern, quality_value in patterns:
        if re.search(pattern, raw_text, flags=re.IGNORECASE):
            return quality_value
    return ''

def load_tonart_value(raw_text: str) -> str:
    match = re.search(r'\\b([A-G](?:#|b)?\\s*(?:maj(?:or)?|min(?:or)?|m|dur|moll))\\b', raw_text, flags=re.IGNORECASE)
    if match:
        return re.sub(r'\\s+', ' ', str(match.group(1) or '')).strip()

    json_key_match = re.search(
        r'\"(?:musicalKey|initialKey|musical_key|key)\"\\s*:\\s*\"([A-G](?:_SHARP|_FLAT)?_(?:MAJOR|MINOR)|[A-G](?:#|b)?(?:m|maj|min|major|minor)?)\"',
        raw_text,
        flags=re.IGNORECASE,
    )
    if not json_key_match:
        return ''

    normalized = str(json_key_match.group(1) or '').strip().replace('_', ' ')
    normalized = normalized.replace('SHARP', '#').replace('FLAT', 'b')
    return re.sub(r'\\s+', ' ', normalized).strip()

def load_quality_resolution(raw_text: str) -> str:
    normalized_text = str(raw_text or '').replace('\\n', ' ').replace('\\r', ' ')
    bit_depth_match = re.search(
        r'"(?:bitDepth|bit_depth|bitdepth)"\\s*:\\s*"?(\\d{1,2})"?',
        normalized_text,
        flags=re.IGNORECASE,
    )
    if not bit_depth_match:
        bit_depth_match = re.search(r'\\b(\\d{1,2})\\s*[- ]?bit\\b', normalized_text, flags=re.IGNORECASE)
    if not bit_depth_match:
        bit_depth_match = re.search(r'\\bbit\\s*depth\\s*[:=]\\s*(\\d{1,2})\\b', normalized_text, flags=re.IGNORECASE)

    sample_rate_match = re.search(
        r'"(?:sampleRate|sample_rate|samplerate|samplingRate)"\\s*:\\s*"?(\\d+(?:\\.\\d+)?)"?',
        normalized_text,
        flags=re.IGNORECASE,
    )
    sample_rate_khz = 0.0
    if sample_rate_match:
        try:
            sample_rate_khz = float(str(sample_rate_match.group(1) or '0'))
        except (TypeError, ValueError):
            sample_rate_khz = 0.0
    if sample_rate_khz <= 0:
        sample_rate_match = re.search(r'\\b(\\d+(?:\\.\\d+)?)\\s*kHz\\b', normalized_text, flags=re.IGNORECASE)
        if sample_rate_match:
            try:
                sample_rate_khz = float(str(sample_rate_match.group(1) or '0'))
            except (TypeError, ValueError):
                sample_rate_khz = 0.0
    if sample_rate_khz <= 0:
        sample_rate_match = re.search(r'\\b(\\d{4,6}(?:\\.\\d+)?)\\s*Hz\\b', normalized_text, flags=re.IGNORECASE)
        if sample_rate_match:
            try:
                sample_rate_khz = float(str(sample_rate_match.group(1) or '0')) / 1000.0
            except (TypeError, ValueError):
                sample_rate_khz = 0.0
    if sample_rate_khz >= 1000:
        sample_rate_khz /= 1000.0

    resolution_parts = []
    if bit_depth_match:
        resolution_parts.append(f"{str(bit_depth_match.group(1) or '').strip()}-bit")
    if sample_rate_khz > 0:
        resolution_parts.append(f"{sample_rate_khz:g} kHz")
    return ' '.join(resolution_parts).strip()

def load_audio_mode_from_text(raw_text: str) -> str:
    normalized_text = str(raw_text or '').strip()
    if not normalized_text:
        return ''
    haystack = normalized_text.casefold()
    if 'atmos' in haystack or 'dolby' in haystack or 'ec-3' in haystack or 'eac3' in haystack:
        return 'DOLBY_ATMOS'
    if '360' in haystack or 'sony' in haystack or 'mha1' in haystack:
        return 'SONY_360RA'
    if 'stereo' in haystack:
        return 'STEREO'
    if 'lossless' in haystack or 'flac' in haystack or 'alac' in haystack:
        return 'LOSSLESS'
    if 'hi_res' in haystack or 'hi-res' in haystack or 'master' in haystack:
        return 'HI_RES'
    if 'aac' in haystack or 'mp4a' in haystack:
        return 'AAC'
    if 'opus' in haystack:
        return 'OPUS'
    return ''

def sanitize_value(raw_value: str) -> str:
    return str(raw_value or '').replace('\\n', ' ').replace('\\r', ' ').replace('=', '-').strip()

def load_track_id_from_url(raw_url: str) -> str:
    match = re.search(r'/track/([A-Za-z0-9_-]+)', str(raw_url or ''), flags=re.IGNORECASE)
    if not match:
        return ''
    return str(match.group(1) or '').strip()

def load_track_id_from_text(raw_text: str) -> str:
    search_text = str(raw_text or '')
    patterns = [
        '"track(?:Id|_id)"\\s*:\\s*"?([0-9]{4,})"?',
        r'/track/([0-9]{4,})',
        r'/tracks/([0-9]{4,})',
        r'trackId=([0-9]{4,})',
    ]
    for pattern in patterns:
        match = re.search(pattern, search_text, flags=re.IGNORECASE)
        if match:
            return str(match.group(1) or '').strip()
    return ''

async def load_tidal_api_track_enrichment(*, ws_url: str, track_id: str) -> tuple[str, str, str]:
    normalized_ws_url = str(ws_url or '').strip()
    normalized_track_id = str(track_id or '').strip()
    if not normalized_ws_url or not normalized_track_id:
        return '', '', ''

    service_worker_ws_url = load_tidal_service_worker_ws_url()
    if service_worker_ws_url:
        normalized_ws_url = service_worker_ws_url

    try:
        async with websockets.connect(normalized_ws_url, max_size=8_000_000) as websocket:
            message_id = 0
            pending_responses: dict[object, dict] = {}
            pending_events: list[dict] = []

            async def send_method(method_name: str, params: dict | None = None) -> dict:
                nonlocal message_id
                message_id += 1
                request_id = message_id
                payload = {'id': request_id, 'method': method_name}
                if params:
                    payload['params'] = params
                await websocket.send(json.dumps(payload))
                while True:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=max(1, timeout_s))
                    message = json.loads(raw_message)
                    response_id = message.get('id')
                    if response_id is None:
                        if isinstance(message, dict):
                            pending_events.append(message)
                        continue
                    if response_id == request_id:
                        return message
                    pending_responses[response_id] = message

            def push_unique(collection: list[str], value: str, *, max_items: int = 8) -> None:
                normalized_value = str(value or '').strip()
                if not normalized_value:
                    return
                if normalized_value in collection:
                    return
                if len(collection) >= max_items:
                    return
                collection.append(normalized_value)

            def collect_header_candidates(
                headers: dict[str, object],
                *,
                request_url: str,
                token_candidates: list[str],
                auth_candidates: list[str],
            ) -> None:
                normalized_url = str(request_url or '').strip().lower()
                for raw_name, raw_value in headers.items():
                    header_name = str(raw_name or '').strip().lower()
                    header_value = str(raw_value or '').strip()
                    if not header_name or not header_value:
                        continue

                    if normalized_url and 'tidal.com' not in normalized_url and header_name not in {'authorization', 'x-tidal-token'}:
                        continue

                    if header_name == 'authorization':
                        bearer_match = re.match(r'^Bearer[ \t]+(.+)$', header_value, flags=re.IGNORECASE)
                        if bearer_match:
                            push_unique(auth_candidates, str(bearer_match.group(1) or ''))
                        else:
                            push_unique(auth_candidates, header_value)
                        continue

                    if header_name == 'x-tidal-token':
                        push_unique(token_candidates, header_value)
                        continue

                    if header_name in {'x-auth-token', 'x-access-token'}:
                        push_unique(auth_candidates, header_value)
                        continue

                    if header_name in {'x-token', 'tidal-token'}:
                        push_unique(token_candidates, header_value)

                    if header_name == 'cookie':
                        for cookie_chunk in header_value.split(';'):
                            cookie_name, _, cookie_value = cookie_chunk.partition('=')
                            normalized_name = cookie_name.strip().lower()
                            normalized_value = cookie_value.strip()
                            if not normalized_name or not normalized_value:
                                continue
                            if 'token' in normalized_name:
                                push_unique(token_candidates, normalized_value)
                            if 'auth' in normalized_name or 'session' in normalized_name or 'jwt' in normalized_name:
                                push_unique(auth_candidates, normalized_value)

            def normalize_audio_mode(raw_value: str) -> str:
                normalized_text = str(raw_value or '').strip()
                if not normalized_text:
                    return ''
                haystack = normalized_text.casefold()
                if 'atmos' in haystack or 'dolby' in haystack or 'ec-3' in haystack or 'eac3' in haystack:
                    return 'DOLBY_ATMOS'
                if '360' in haystack or 'sony' in haystack or 'mha1' in haystack:
                    return 'SONY_360RA'
                if 'stereo' in haystack:
                    return 'STEREO'
                if 'lossless' in haystack or 'flac' in haystack or 'alac' in haystack:
                    return 'LOSSLESS'
                if 'hi_res' in haystack or 'hi-res' in haystack or 'master' in haystack:
                    return 'HI_RES'
                if 'aac' in haystack or 'mp4a' in haystack:
                    return 'AAC'
                if 'opus' in haystack:
                    return 'OPUS'
                return ''

            def decode_manifest_payload(manifest_value: str) -> str:
                raw_manifest = str(manifest_value or '').strip()
                if not raw_manifest:
                    return ''

                if raw_manifest.startswith('{') or raw_manifest.startswith('['):
                    return raw_manifest

                # TIDAL playback manifest is usually base64 encoded JSON.
                base64_candidates = [
                    raw_manifest,
                    raw_manifest.replace('-', '+').replace('_', '/'),
                ]
                for candidate in base64_candidates:
                    padded_candidate = candidate + ('=' * ((4 - (len(candidate) % 4)) % 4))
                    try:
                        decoded_bytes = base64.b64decode(padded_candidate.encode('ascii'))
                    except Exception:
                        continue
                    decoded_text = decoded_bytes.decode('utf-8', errors='replace').strip()
                    if decoded_text:
                        return decoded_text
                return ''

            def parse_playback_payload(payload_text: str) -> tuple[str, str, str]:
                try:
                    payload = json.loads(str(payload_text or ''))
                except Exception:
                    return '', '', ''

                if not isinstance(payload, dict):
                    return '', '', ''

                quality_candidates = [
                    payload.get('audioQuality'),
                    payload.get('audioquality'),
                    payload.get('quality'),
                    payload.get('streamQuality'),
                ]
                mode_candidates = [
                    payload.get('audioMode'),
                    payload.get('codec'),
                    payload.get('manifestMimeType'),
                    payload.get('mimeType'),
                    payload.get('assetPresentation'),
                ]

                quality_resolution = load_quality_resolution(payload_text)
                manifest_text = decode_manifest_payload(str(payload.get('manifest') or ''))
                if manifest_text:
                    manifest_quality_resolution = load_quality_resolution(manifest_text)
                    if manifest_quality_resolution:
                        quality_resolution = manifest_quality_resolution
                    mode_candidates.append(manifest_text)
                    try:
                        manifest_payload = json.loads(manifest_text)
                    except Exception:
                        manifest_payload = {}
                    if isinstance(manifest_payload, dict):
                        mode_candidates.extend(
                            [
                                manifest_payload.get('audioMode'),
                                manifest_payload.get('mimeType'),
                                manifest_payload.get('manifestMimeType'),
                                manifest_payload.get('codecs'),
                                manifest_payload.get('codec'),
                            ]
                        )

                def collect_nested_mode_candidates(value: object, *, depth: int = 0) -> None:
                    if depth > 4 or value is None:
                        return
                    if isinstance(value, str):
                        mode_candidates.append(value)
                        return
                    if isinstance(value, list):
                        for item in value[:20]:
                            collect_nested_mode_candidates(item, depth=depth + 1)
                        return
                    if isinstance(value, dict):
                        for key, nested_value in list(value.items())[:80]:
                            key_text = str(key or '').casefold()
                            if isinstance(nested_value, str) and (
                                'audio' in key_text
                                or 'mode' in key_text
                                or 'codec' in key_text
                                or 'mime' in key_text
                                or 'quality' in key_text
                                or 'spatial' in key_text
                                or 'atmos' in key_text
                            ):
                                mode_candidates.append(nested_value)
                            collect_nested_mode_candidates(nested_value, depth=depth + 1)

                collect_nested_mode_candidates(payload)

                stream_quality = ''
                audio_mode = ''
                for candidate in quality_candidates:
                    normalized_candidate = sanitize_value(str(candidate or '').strip())
                    if normalized_candidate:
                        stream_quality = normalized_candidate
                        break
                for candidate in mode_candidates:
                    normalized_candidate = normalize_audio_mode(str(candidate or '').strip())
                    if normalized_candidate:
                        audio_mode = normalized_candidate
                        break

                if not audio_mode and stream_quality:
                    audio_mode = normalize_audio_mode(stream_quality)
                return stream_quality, audio_mode, quality_resolution

            def collect_cookie_candidates(
                cookies: list[dict],
                *,
                token_candidates: list[str],
                auth_candidates: list[str],
            ) -> None:
                for cookie in cookies:
                    if not isinstance(cookie, dict):
                        continue
                    domain_text = str(cookie.get('domain') or '').strip().lower()
                    if domain_text and 'tidal' not in domain_text:
                        continue
                    cookie_name = str(cookie.get('name') or '').strip().lower()
                    cookie_value = str(cookie.get('value') or '').strip()
                    if not cookie_value:
                        continue
                    if re.match(r'^eyJ[A-Za-z0-9._-]+$', cookie_value):
                        push_unique(auth_candidates, cookie_value)
                    if 'token' in cookie_name:
                        push_unique(token_candidates, cookie_value)
                    if 'auth' in cookie_name or 'session' in cookie_name or 'jwt' in cookie_name:
                        push_unique(auth_candidates, cookie_value)

            async def load_network_header_candidates() -> tuple[list[str], list[str], str, str, str]:
                token_candidates: list[str] = []
                auth_candidates: list[str] = []
                request_url_by_id: dict[str, str] = {}
                playback_request_ids: set[str] = set()
                playback_stream_quality = ''
                playback_audio_mode = ''
                playback_quality_resolution = ''

                def process_network_event(event_message: dict) -> None:
                    nonlocal playback_stream_quality, playback_audio_mode, playback_quality_resolution
                    method_name = str(event_message.get('method') or '')
                    params = event_message.get('params') if isinstance(event_message.get('params'), dict) else {}
                    if method_name == 'Network.requestWillBeSent':
                        request_id = str(params.get('requestId') or '')
                        request_payload = params.get('request') if isinstance(params.get('request'), dict) else {}
                        request_url = str(request_payload.get('url') or '')
                        if request_id:
                            request_url_by_id[request_id] = request_url
                        request_headers = request_payload.get('headers') if isinstance(request_payload.get('headers'), dict) else {}
                        collect_header_candidates(
                            request_headers,
                            request_url=request_url,
                            token_candidates=token_candidates,
                            auth_candidates=auth_candidates,
                        )
                        if 'playbackinfopostpaywall' in request_url.lower() and request_id:
                            playback_request_ids.add(request_id)
                        return

                    if method_name == 'Network.requestWillBeSentExtraInfo':
                        request_id = str(params.get('requestId') or '')
                        request_url = request_url_by_id.get(request_id, '')
                        extra_headers = params.get('headers') if isinstance(params.get('headers'), dict) else {}
                        collect_header_candidates(
                            extra_headers,
                            request_url=request_url,
                            token_candidates=token_candidates,
                            auth_candidates=auth_candidates,
                        )
                        return

                    if method_name == 'Network.responseReceived':
                        request_id = str(params.get('requestId') or '')
                        response_payload = params.get('response') if isinstance(params.get('response'), dict) else {}
                        response_url = str(response_payload.get('url') or request_url_by_id.get(request_id, '')).strip()
                        if request_id and 'playbackinfopostpaywall' in response_url.lower():
                            playback_request_ids.add(request_id)
                        return

                async def try_load_playback_response_body(request_id: str) -> None:
                    nonlocal playback_stream_quality, playback_audio_mode
                    if not request_id or (playback_stream_quality and playback_audio_mode and playback_quality_resolution):
                        return
                    try:
                        body_response = await send_method('Network.getResponseBody', {'requestId': request_id})
                    except Exception:
                        return
                    body_result = body_response.get('result') if isinstance(body_response.get('result'), dict) else {}
                    raw_body = str(body_result.get('body') or '')
                    if not raw_body:
                        return
                    if bool(body_result.get('base64Encoded')):
                        try:
                            raw_body = __import__('base64').b64decode(raw_body.encode('ascii')).decode('utf-8', errors='replace')
                        except Exception:
                            return
                    (
                        stream_quality_candidate,
                        audio_mode_candidate,
                        quality_resolution_candidate,
                    ) = parse_playback_payload(raw_body)
                    if stream_quality_candidate and not playback_stream_quality:
                        playback_stream_quality = stream_quality_candidate
                    if audio_mode_candidate and not playback_audio_mode:
                        playback_audio_mode = audio_mode_candidate
                    if quality_resolution_candidate and not playback_quality_resolution:
                        playback_quality_resolution = quality_resolution_candidate

                try:
                    await send_method('Network.enable')
                except Exception:
                    return (
                        token_candidates,
                        auth_candidates,
                        playback_stream_quality,
                        playback_audio_mode,
                        playback_quality_resolution,
                    )

                try:
                    cookie_response = await send_method('Network.getAllCookies')
                    cookie_result = cookie_response.get('result') if isinstance(cookie_response.get('result'), dict) else {}
                    cookies = cookie_result.get('cookies') if isinstance(cookie_result.get('cookies'), list) else []
                    collect_cookie_candidates(
                        cookies,
                        token_candidates=token_candidates,
                        auth_candidates=auth_candidates,
                    )
                except Exception:
                    pass

                try:
                    warmup_expression = f"""
(async () => {{
    const endpoint = new URL(`https://api.tidal.com/v1/tracks/${{encodeURIComponent({json.dumps(normalized_track_id)})}}/playbackinfopostpaywall`);
    endpoint.searchParams.set('audioquality', 'HI_RES_LOSSLESS');
    endpoint.searchParams.set('playbackmode', 'STREAM');
    endpoint.searchParams.set('assetpresentation', 'FULL');
    endpoint.searchParams.set('countryCode', 'DE');
    try {{
        await fetch(endpoint.toString(), {{ method: 'GET', credentials: 'include' }});
    }} catch (_) {{}}
    return true;
}})()
"""
                    await send_method(
                        'Runtime.evaluate',
                        {
                            'expression': warmup_expression,
                            'returnByValue': True,
                            'awaitPromise': True,
                        },
                    )
                except Exception:
                    pass

                while pending_events:
                    process_network_event(pending_events.pop(0))
                for request_id in list(playback_request_ids):
                    await try_load_playback_response_body(request_id)

                deadline = time.monotonic() + max(0.6, min(1.8, float(timeout_s)))
                while time.monotonic() < deadline:
                    timeout_window = min(0.25, max(0.05, deadline - time.monotonic()))
                    try:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=timeout_window)
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break

                    try:
                        message = json.loads(raw_message)
                    except Exception:
                        continue

                    response_id = message.get('id')
                    if response_id is not None:
                        pending_responses[response_id] = message
                        continue
                    process_network_event(message)
                    for request_id in list(playback_request_ids):
                        await try_load_playback_response_body(request_id)

                return (
                    token_candidates,
                    auth_candidates,
                    playback_stream_quality,
                    playback_audio_mode,
                    playback_quality_resolution,
                )

            await send_method('Page.enable')
            await send_method('Runtime.enable')
            (
                network_tokens,
                network_auth,
                playback_network_quality,
                playback_network_mode,
                playback_network_quality_resolution,
            ) = await load_network_header_candidates()
            if probe_debug:
                print('cdp_probe_network_token_candidates=' + str(len(network_tokens)))
                print('cdp_probe_network_auth_candidates=' + str(len(network_auth)))
                print('cdp_probe_network_playback_quality=' + sanitize_value(playback_network_quality))
                print('cdp_probe_network_playback_mode=' + sanitize_value(playback_network_mode))
                print('cdp_probe_network_quality_resolution=' + sanitize_value(playback_network_quality_resolution))

            if playback_network_quality and playback_network_mode and playback_network_quality_resolution:
                return (
                    sanitize_value(playback_network_quality),
                    sanitize_value(playback_network_mode),
                    sanitize_value(playback_network_quality_resolution),
                )

            expression = f"""
(async () => {{
    const trackId = {json.dumps(normalized_track_id)};
    if (!trackId) {{
        return {{ streamQuality: '', audioMode: '' }};
    }}

    const prefetchedStreamQuality = {json.dumps(sanitize_value(playback_network_quality))};
    const prefetchedAudioMode = {json.dumps(sanitize_value(playback_network_mode))};
    const prefetchedQualityResolution = {json.dumps(sanitize_value(playback_network_quality_resolution))};
    const tokenCandidates = Array.from(new Set({json.dumps(network_tokens)}));
    const authCandidates = Array.from(new Set({json.dumps(network_auth)}));
    const pushUnique = (collection, value) => {{
        const normalized = String(value || '').trim();
        if (!normalized) return;
        if (!collection.includes(normalized)) collection.push(normalized);
    }};

    const inspectValue = (value, depth = 0) => {{
        if (depth > 4 || value == null) return;
        if (typeof value === 'string') {{
            const raw = value.trim();
            if (!raw) return;
            const bearerMatch = raw.match(/^Bearer\\s+(.+)$/i);
            if (bearerMatch && bearerMatch[1]) pushUnique(authCandidates, bearerMatch[1]);
            if (/^eyJ[A-Za-z0-9._-]+$/.test(raw)) pushUnique(authCandidates, raw);
            if (/^[A-Za-z0-9._~+-]{6,}$/.test(raw) && !raw.includes(' ')) pushUnique(tokenCandidates, raw);
            return;
        }}
        if (Array.isArray(value)) {{
            for (const item of value.slice(0, 20)) inspectValue(item, depth + 1);
            return;
        }}
        if (typeof value === 'object') {{
            for (const [key, nestedValue] of Object.entries(value).slice(0, 80)) {{
                const keyText = String(key || '').toLowerCase();
                if (typeof nestedValue === 'string') {{
                    if (keyText.includes('token') && !keyText.includes('refresh')) pushUnique(tokenCandidates, nestedValue);
                    if ((keyText.includes('access') || keyText.includes('auth')) && keyText.includes('token')) pushUnique(authCandidates, nestedValue);
                    if ((keyText.includes('authorization') || keyText.includes('bearer')) && nestedValue.length >= 20) pushUnique(authCandidates, nestedValue);
                }}
                inspectValue(nestedValue, depth + 1);
            }}
        }}
    }};

    const inspectStoreWithCursor = async (store) => {{
        await new Promise((resolve) => {{
            let seenRows = 0;
            let done = false;
            const finish = () => {{
                if (done) return;
                done = true;
                resolve(undefined);
            }};
            let cursorRequest;
            try {{
                cursorRequest = store.openCursor();
            }} catch (_) {{
                finish();
                return;
            }}
            cursorRequest.onerror = () => finish();
            cursorRequest.onsuccess = () => {{
                try {{
                    const cursor = cursorRequest.result;
                    if (!cursor || seenRows >= 16) {{
                        finish();
                        return;
                    }}
                    seenRows += 1;
                    inspectValue(cursor.value, 0);
                    cursor.continue();
                }} catch (_) {{
                    finish();
                }}
            }};
        }});
    }};

    const collectIndexedDbCandidates = async () => {{
        if (!globalThis.indexedDB || typeof globalThis.indexedDB.databases !== 'function') return;
        let databaseInfos = [];
        try {{
            databaseInfos = await globalThis.indexedDB.databases();
        }} catch (_) {{
            return;
        }}

        for (const databaseInfo of (databaseInfos || []).slice(0, 8)) {{
            const databaseName = String((databaseInfo || {{}}).name || '').trim();
            if (!databaseName) continue;

            await new Promise((resolveDb) => {{
                let resolved = false;
                const finishDb = () => {{
                    if (resolved) return;
                    resolved = true;
                    resolveDb(undefined);
                }};

                let openRequest;
                try {{
                    openRequest = globalThis.indexedDB.open(databaseName);
                }} catch (_) {{
                    finishDb();
                    return;
                }}

                openRequest.onerror = () => finishDb();
                openRequest.onupgradeneeded = () => finishDb();
                openRequest.onsuccess = async () => {{
                    const database = openRequest.result;
                    try {{
                        const storeNames = Array.from(database.objectStoreNames || []).slice(0, 12);
                        for (const storeName of storeNames) {{
                            try {{
                                const transaction = database.transaction(storeName, 'readonly');
                                const store = transaction.objectStore(storeName);
                                await inspectStoreWithCursor(store);
                            }} catch (_) {{}}
                        }}
                    }} catch (_) {{}}
                    try {{
                        database.close();
                    }} catch (_) {{}}
                    finishDb();
                }};
            }});
        }}
    }};

    try {{
        if (window.localStorage) {{
            for (let i = 0; i < window.localStorage.length; i += 1) {{
                const key = window.localStorage.key(i);
                if (!key) continue;
                const value = window.localStorage.getItem(key);
                inspectValue(value, 0);
                try {{
                    inspectValue(JSON.parse(String(value || '')), 0);
                }} catch (_) {{}}
            }}
        }}
    }} catch (_) {{}}

    try {{ inspectValue(window.__NEXT_DATA__, 0); }} catch (_) {{}}
    try {{ inspectValue(window.__APOLLO_STATE__, 0); }} catch (_) {{}}
    try {{ inspectValue(window.__NUXT__, 0); }} catch (_) {{}}
    try {{
        const windowKeys = Object.keys(window || {{}});
        for (const key of windowKeys.slice(0, 600)) {{
            const keyText = String(key || '').toLowerCase();
            if (!/(token|auth|access|session|state|store|user|profile)/.test(keyText)) continue;
            try {{
                inspectValue(window[key], 0);
            }} catch (_) {{}}
        }}
    }} catch (_) {{}}
    try {{
        const cookieText = String(document.cookie || '');
        inspectValue(cookieText, 0);
        for (const chunk of cookieText.split(';')) {{
            const [, rawValue = ''] = chunk.split('=');
            inspectValue(rawValue.trim(), 0);
        }}
    }} catch (_) {{}}
    try {{
        await collectIndexedDbCandidates();
    }} catch (_) {{}}

    const endpoint = new URL(`https://api.tidal.com/v1/tracks/${{encodeURIComponent(trackId)}}/playbackinfopostpaywall`);
    endpoint.searchParams.set('audioquality', 'HI_RES_LOSSLESS');
    endpoint.searchParams.set('playbackmode', 'STREAM');
    endpoint.searchParams.set('assetpresentation', 'FULL');
    endpoint.searchParams.set('countryCode', 'DE');

    const headerCombos = [{{}}];
    const tokenSample = tokenCandidates.slice(0, 4);
    const authSample = authCandidates.slice(0, 4);
    for (const token of tokenSample) {{
        headerCombos.push({{ 'x-tidal-token': token }});
    }}
    for (const auth of authSample) {{
        headerCombos.push({{ Authorization: `Bearer ${{auth}}` }});
    }}
    for (const token of tokenSample) {{
        headerCombos.push({{ Authorization: `Bearer ${{token}}` }});
    }}
    for (const token of tokenSample) {{
        for (const auth of authSample) {{
            headerCombos.push({{ 'x-tidal-token': token, Authorization: `Bearer ${{auth}}` }});
        }}
    }}
    for (const tokenA of tokenSample) {{
        for (const tokenB of tokenSample) {{
            if (tokenA === tokenB) continue;
            headerCombos.push({{ 'x-tidal-token': tokenA, Authorization: `Bearer ${{tokenB}}` }});
        }}
    }}

    const normalizeQuality = (payload) => {{
        if (!payload || typeof payload !== 'object') return '';
        const candidates = [
            payload.audioQuality,
            payload.audioquality,
            payload.quality,
            payload.streamQuality,
        ];
        for (const candidate of candidates) {{
            const text = String(candidate || '').trim();
            if (text) return text;
        }}
        return '';
    }};

    const normalizeMode = (payload) => {{
        const normalizeModeValue = (rawValue) => {{
            const normalized = String(rawValue || '').trim();
            if (!normalized) return '';
            const haystack = normalized.toLowerCase();
            if (haystack.includes('atmos') || haystack.includes('dolby') || haystack.includes('ec-3') || haystack.includes('eac3')) return 'DOLBY_ATMOS';
            if (haystack.includes('360') || haystack.includes('sony') || haystack.includes('mha1')) return 'SONY_360RA';
            if (haystack.includes('stereo')) return 'STEREO';
            if (haystack.includes('lossless') || haystack.includes('flac') || haystack.includes('alac')) return 'LOSSLESS';
            if (haystack.includes('hi_res') || haystack.includes('hi-res') || haystack.includes('master')) return 'HI_RES';
            if (haystack.includes('aac') || haystack.includes('mp4a')) return 'AAC';
            if (haystack.includes('opus')) return 'OPUS';
            return '';
        }};

        const decodeManifestPayload = (manifestValue) => {{
            const rawManifest = String(manifestValue || '').trim();
            if (!rawManifest) return '';
            if (rawManifest.startsWith('{{') || rawManifest.startsWith('[')) return rawManifest;

            const candidates = [
                rawManifest,
                rawManifest.replace(/-/g, '+').replace(/_/g, '/'),
            ];
            for (const candidate of candidates) {{
                try {{
                    const padded = candidate + '='.repeat((4 - (candidate.length % 4)) % 4);
                    const decoded = atob(padded);
                    if (decoded) return decoded;
                }} catch (_) {{}}
            }}
            return '';
        }};

        if (!payload || typeof payload !== 'object') return '';
        const candidates = [
            payload.audioMode,
            payload.codec,
            payload.manifestMimeType,
            payload.mimeType,
            payload.assetPresentation,
            payload.audioQuality,
            payload.quality,
        ];

        const decodedManifest = decodeManifestPayload(payload.manifest);
        if (decodedManifest) {{
            candidates.push(decodedManifest);
            try {{
                const manifestPayload = JSON.parse(decodedManifest);
                if (manifestPayload && typeof manifestPayload === 'object') {{
                    candidates.push(manifestPayload.audioMode);
                    candidates.push(manifestPayload.mimeType);
                    candidates.push(manifestPayload.manifestMimeType);
                    candidates.push(manifestPayload.codecs);
                    candidates.push(manifestPayload.codec);
                }}
            }} catch (_) {{}}
        }}

        for (const candidate of candidates) {{
            const normalized = normalizeModeValue(candidate);
            if (normalized) return normalized;
        }}
        return '';
    }};

    const normalizeResolution = (payload) => {{
        const rawValues = [];
        try {{
            rawValues.push(JSON.stringify(payload || {{}}));
        }} catch (_) {{}}

        const rawManifest = String((payload || {{}}).manifest || '').trim();
        if (rawManifest) {{
            if (rawManifest.startsWith('{{') || rawManifest.startsWith('[')) {{
                rawValues.push(rawManifest);
            }} else {{
                try {{
                    const paddedManifest = rawManifest + '='.repeat((4 - (rawManifest.length % 4)) % 4);
                    const decodedManifest = atob(paddedManifest.replace(/-/g, '+').replace(/_/g, '/'));
                    if (decodedManifest) rawValues.push(decodedManifest);
                }} catch (_) {{}}
            }}
        }}

        const combinedText = rawValues.join(' ');
        let bitDepthMatch = combinedText.match(/"(?:bitDepth|bit_depth|bitdepth)"\\s*:\\s*"?([0-9]{{1,2}})"?/i);
        if (!bitDepthMatch) bitDepthMatch = combinedText.match(/\\b([0-9]{{1,2}})\\s*[- ]?bit\\b/i);

        let sampleRateMatch = combinedText.match(/"(?:sampleRate|sample_rate|samplerate|samplingRate)"\\s*:\\s*"?([0-9]+(?:\\.[0-9]+)?)"?/i);
        let sampleRateKHz = sampleRateMatch ? Number(sampleRateMatch[1]) : 0;
        if (!sampleRateKHz) {{
            sampleRateMatch = combinedText.match(/\\b([0-9]+(?:\\.[0-9]+)?)\\s*kHz\\b/i);
            sampleRateKHz = sampleRateMatch ? Number(sampleRateMatch[1]) : 0;
        }}
        if (!sampleRateKHz) {{
            sampleRateMatch = combinedText.match(/\\b([0-9]{{4,6}}(?:\\.[0-9]+)?)\\s*Hz\\b/i);
            sampleRateKHz = sampleRateMatch ? Number(sampleRateMatch[1]) / 1000 : 0;
        }}
        if (sampleRateKHz >= 1000) sampleRateKHz /= 1000;

        const resolutionParts = [];
        if (bitDepthMatch && bitDepthMatch[1]) resolutionParts.push(`${{bitDepthMatch[1]}}-bit`);
        if (sampleRateKHz > 0 && Number.isFinite(sampleRateKHz)) {{
            const normalizedRate = Number.isInteger(sampleRateKHz) ? String(sampleRateKHz) : String(Number(sampleRateKHz.toFixed(3)));
            resolutionParts.push(`${{normalizedRate}} kHz`);
        }}
        return resolutionParts.join(' ');
    }};

    for (const comboHeaders of headerCombos.slice(0, 12)) {{
        try {{
            const response = await fetch(endpoint.toString(), {{
                method: 'GET',
                credentials: 'include',
                headers: {{
                    Accept: 'application/json',
                    ...comboHeaders,
                }},
            }});
            if (!response.ok) continue;
            const payload = await response.json();
            return {{
                streamQuality: normalizeQuality(payload) || prefetchedStreamQuality,
                audioMode: normalizeMode(payload) || prefetchedAudioMode,
                qualityResolution: normalizeResolution(payload) || prefetchedQualityResolution,
            }};
        }} catch (_) {{}}
    }}

    return {{
        streamQuality: prefetchedStreamQuality,
        audioMode: prefetchedAudioMode,
        qualityResolution: prefetchedQualityResolution,
    }};
}})()
"""

            response = await send_method(
                'Runtime.evaluate',
                {
                    'expression': expression,
                    'returnByValue': True,
                    'awaitPromise': True,
                },
            )
    except Exception:
        return '', '', ''

    value = (((response.get('result') or {}).get('result') or {}).get('value') or {})
    if not isinstance(value, dict):
        return '', '', ''

    stream_quality = sanitize_value(str(value.get('streamQuality') or '').strip())
    audio_mode = sanitize_value(str(value.get('audioMode') or '').strip())
    quality_resolution = sanitize_value(str(value.get('qualityResolution') or '').strip())
    return stream_quality, audio_mode, quality_resolution

async def load_target_text(websocket, *, send_method) -> tuple[str, str, str, str, str, str, str, str, str, str]:
    expression = r"""
(() => {
  const values = [];
  const pushValue = (rawValue) => {
    if (rawValue == null) return;
    const text = String(rawValue).trim();
    if (!text) return;
    values.push(text);
  };

    const loadElementText = (element) => {
        if (!element) return '';
        return [
            element.getAttribute('aria-label') || '',
            element.getAttribute('title') || '',
            element.getAttribute('data-test') || '',
            element.getAttribute('data-testid') || '',
            element.textContent || '',
        ].join(' ').toLowerCase();
    };

    const isVisible = (element) => {
        if (!element) return false;
        const style = window.getComputedStyle(element);
        if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
        const rect = element.getBoundingClientRect();
        return rect.width > 4 && rect.height > 4;
    };

    const loadLikedState = () => {
        const addLikePatterns = [
            /add to (favorites|favourites|library|collection)/i,
            /favorite this track/i,
            /save track/i,
            /zu favoriten/i,
            /zur sammlung/i,
            /als favorit/i,
        ];
        const removeLikePatterns = [
            /remove from (favorites|favourites|library|collection)/i,
            /unlike/i,
            /aus favoriten/i,
            /aus der sammlung/i,
            /entfernen/i,
        ];
        const likeSelectors = [
            'button[data-test*="favorite" i]',
            'button[data-test*="favourite" i]',
            'button[data-testid*="favorite" i]',
            'button[data-testid*="favourite" i]',
            'button[aria-label*="favorite" i]',
            'button[aria-label*="favourite" i]',
            'button[aria-label*="favorit" i]',
            'button[title*="favorite" i]',
            'button[title*="favourite" i]',
            'button[title*="favorit" i]',
            '[role="button"][aria-label*="favorite" i]',
            '[role="button"][aria-label*="favourite" i]',
            '[role="button"][aria-label*="favorit" i]',
            'button[class*="heart" i]',
            '[role="button"][class*="heart" i]',
        ];

        for (const selector of likeSelectors) {
            const elements = Array.from(document.querySelectorAll(selector));
            for (const element of elements) {
                if (!isVisible(element)) continue;
                const elementText = loadElementText(element);
                if (removeLikePatterns.some((pattern) => pattern.test(elementText))) {
                    return 'liked';
                }
                if (addLikePatterns.some((pattern) => pattern.test(elementText))) {
                    return 'not_liked';
                }

                const ariaPressed = String(element.getAttribute('aria-pressed') || '').toLowerCase();
                if (ariaPressed === 'true') {
                    return 'liked';
                }
                if (ariaPressed === 'false') {
                    return 'not_liked';
                }

                const ariaChecked = String(element.getAttribute('aria-checked') || '').toLowerCase();
                if (ariaChecked === 'true') {
                    return 'liked';
                }
                if (ariaChecked === 'false') {
                    return 'not_liked';
                }
            }
        }

        return '';
    };

    const loadTrackId = (nowPlayingMetadata) => {
        const matchTrackId = (inputValue) => {
            const text = String(inputValue || '');
            if (!text) return '';

            const urlMatch = text.match(/[/]track[/]([0-9]{4,})/i);
            if (urlMatch && urlMatch[1]) {
                return String(urlMatch[1]).trim();
            }

            const idMatch = text.match(/"track(?:Id|ID|_id)"[ \t]*:[ \t]*"?([0-9]{4,})"?/i);
            if (idMatch && idMatch[1]) {
                return String(idMatch[1]).trim();
            }

            return '';
        };

        const mediaTrackId = matchTrackId(nowPlayingMetadata && nowPlayingMetadata.trackUrl);
        if (mediaTrackId) return mediaTrackId;

        const pathnameId = matchTrackId(window.location && window.location.pathname);
        if (pathnameId) return pathnameId;

        const hrefId = matchTrackId(window.location && window.location.href);
        if (hrefId) return hrefId;

        const linkNodes = Array.from(document.querySelectorAll('a[href*="/track/"]')).slice(0, 250);
        for (const linkNode of linkNodes) {
            const hrefValue = linkNode && linkNode.getAttribute ? linkNode.getAttribute('href') : '';
            const candidate = matchTrackId(hrefValue);
            if (candidate) return candidate;
        }

        const stateCandidates = [];
        try {
            if (window.__NEXT_DATA__) stateCandidates.push(JSON.stringify(window.__NEXT_DATA__));
        } catch (_) {}
        try {
            if (window.__NUXT__) stateCandidates.push(JSON.stringify(window.__NUXT__));
        } catch (_) {}
        try {
            if (window.__APOLLO_STATE__) stateCandidates.push(JSON.stringify(window.__APOLLO_STATE__));
        } catch (_) {}

        for (const stateText of stateCandidates) {
            const candidate = matchTrackId(stateText);
            if (candidate) return candidate;
        }

        for (const scriptNode of Array.from(document.querySelectorAll('script')).slice(0, 220)) {
            const candidate = matchTrackId(String(scriptNode.textContent || '').slice(0, 30000));
            if (candidate) return candidate;
        }

        return '';
    };

    const loadPlaybackInfoFromResources = () => {
        const normalizeQuality = (rawValue) => {
            const normalized = String(rawValue || '').trim().toUpperCase();
            if (!normalized) return '';
            if (normalized.includes('HI_RES') || normalized.includes('MASTER') || normalized.includes('MAX')) return 'MAX';
            if (normalized.includes('LOSSLESS') || normalized.includes('FLAC') || normalized.includes('CD')) return 'LOSSLESS';
            if (normalized.includes('HIGH')) return 'HIGH';
            if (normalized.includes('LOW') || normalized.includes('NORMAL')) return 'NORMAL';
            return normalized;
        };

        const parsePlaybackUrl = (urlText) => {
            try {
                const urlObject = new URL(String(urlText || ''));
                const qualityValue = normalizeQuality(urlObject.searchParams.get('audioquality') || urlObject.searchParams.get('audioQuality'));
                const modeValue = String(urlObject.searchParams.get('assetpresentation') || '').trim().toUpperCase();
                return { qualityValue, modeValue };
            } catch (_) {
                return { qualityValue: '', modeValue: '' };
            }
        };

        const playbackUrls = [];
        const pushPlaybackUrl = (urlText) => {
            const normalized = String(urlText || '').trim();
            if (!normalized) return;
            if (!/playbackinfopostpaywall/i.test(normalized)) return;
            playbackUrls.push(normalized);
        };

        try {
            const resourceEntries = performance.getEntriesByType('resource');
            for (const entry of resourceEntries.slice(-500)) {
                if (entry && entry.name) {
                    pushPlaybackUrl(entry.name);
                }
            }
        } catch (_) {}

        const playbackUrlPattern = /https?:\\/\\/api\\.tidal\\.com\\/v1\\/tracks\\/[^\\s"'`]+\\/playbackinfopostpaywall\\?[^\\s"'`]+/ig;
        for (const scriptNode of Array.from(document.querySelectorAll('script')).slice(0, 120)) {
            const scriptText = String(scriptNode.textContent || '').slice(0, 30000);
            const matches = scriptText.match(playbackUrlPattern) || [];
            for (const matchText of matches.slice(0, 5)) {
                pushPlaybackUrl(matchText);
            }
        }

        for (const playbackUrl of playbackUrls.reverse()) {
            const parsed = parsePlaybackUrl(playbackUrl);
            if (parsed.qualityValue || parsed.modeValue) {
                return {
                    qualityValue: parsed.qualityValue,
                    modeValue: parsed.modeValue,
                };
            }
        }

        return { qualityValue: '', modeValue: '' };
    };

    const loadNowPlayingMetadata = () => {
        const metadata = {
            title: '',
            artist: '',
            album: '',
            trackUrl: '',
            artUrl: '',
        };

        const setIfEmpty = (key, rawValue) => {
            if (!Object.prototype.hasOwnProperty.call(metadata, key)) return;
            if (metadata[key]) return;
            const normalized = String(rawValue || '').replace(/\\s+/g, ' ').trim();
            if (normalized) metadata[key] = normalized;
        };

        const toAbsoluteUrl = (hrefValue) => {
            const normalized = String(hrefValue || '').trim();
            if (!normalized) return '';
            try {
                return new URL(normalized, window.location && window.location.origin ? window.location.origin : 'https://listen.tidal.com').toString();
            } catch (_) {
                return normalized;
            }
        };

        const loadArtworkUrl = (sessionMetadata) => {
            const artworkItems = sessionMetadata && Array.isArray(sessionMetadata.artwork)
                ? Array.from(sessionMetadata.artwork)
                : [];
            const loadArtworkArea = (item) => {
                const sizeText = String((item && item.sizes) || '').trim();
                const match = sizeText.match(/(\\d+)x(\\d+)/i);
                if (!match) return 0;
                return Number(match[1]) * Number(match[2]);
            };
            artworkItems.sort((left, right) => loadArtworkArea(right) - loadArtworkArea(left));
            for (const artworkItem of artworkItems) {
                const artworkUrl = toAbsoluteUrl(artworkItem && artworkItem.src);
                if (/^https?:\\/\\//i.test(artworkUrl)) {
                    return artworkUrl;
                }
            }
            return '';
        };

        try {
            const sessionMetadata = navigator && navigator.mediaSession ? navigator.mediaSession.metadata : null;
            if (sessionMetadata) {
                setIfEmpty('title', sessionMetadata.title);
                setIfEmpty('artist', sessionMetadata.artist);
                setIfEmpty('album', sessionMetadata.album);
                setIfEmpty('artUrl', loadArtworkUrl(sessionMetadata));
            }
        } catch (_) {}

        const pickFromSelector = (selector, key, hrefKey) => {
            for (const element of Array.from(document.querySelectorAll(selector)).slice(0, 200)) {
                if (!isVisible(element)) continue;
                setIfEmpty(key, element.textContent || element.getAttribute('aria-label') || element.getAttribute('title') || '');
                if (hrefKey) {
                    setIfEmpty(hrefKey, toAbsoluteUrl(element.getAttribute('href') || ''));
                }
                if (metadata[key] && (!hrefKey || metadata[hrefKey])) {
                    break;
                }
            }
        };

        pickFromSelector('a[href*="/track/"]', 'title', 'trackUrl');
        pickFromSelector('a[href*="/artist/"]', 'artist', '');
        pickFromSelector('a[href*="/album/"]', 'album', '');

        try {
            const locationHref = String((window.location && window.location.href) || '').trim();
            if (/\\/track\\/[0-9]{4,}/i.test(locationHref)) {
                setIfEmpty('trackUrl', locationHref);
            }
        } catch (_) {}

        if (!metadata.title) {
            const documentTitle = String(document.title || '').trim();
            if (documentTitle) {
                const cleanedTitle = documentTitle.replace(/\\s*\\|\\s*tidal.*$/i, '').trim();
                setIfEmpty('title', cleanedTitle || documentTitle);
            }
        }

        const artworkSelectors = [
            'img[src*="resources.tidal.com" i]',
            'img[src*="images.tidal.com" i]',
        ];
        for (const selector of artworkSelectors) {
            for (const element of Array.from(document.querySelectorAll(selector)).slice(0, 80)) {
                if (!isVisible(element)) continue;
                const artworkUrl = toAbsoluteUrl(element.currentSrc || element.getAttribute('src') || '');
                if (/^https?:\\/\\//i.test(artworkUrl)) {
                    setIfEmpty('artUrl', artworkUrl);
                }
                if (metadata.artUrl) break;
            }
            if (metadata.artUrl) break;
        }

        return metadata;
    };

    const selectorList = [
    '[aria-label*="audio" i]',
    '[aria-label*="quality" i]',
    '[title*="audio" i]',
    '[title*="quality" i]',
    '[data-test*="audio" i]',
    '[data-test*="quality" i]',
    '[data-testid*="audio" i]',
    '[data-testid*="quality" i]',
    '[class*="audio" i]',
    '[class*="quality" i]',
    '[id*="audio" i]',
    '[id*="quality" i]'
  ];

  for (const selector of selectorList) {
    for (const element of Array.from(document.querySelectorAll(selector)).slice(0, 3000)) {
      pushValue(element.getAttribute('aria-label'));
      pushValue(element.getAttribute('title'));
      pushValue(element.getAttribute('data-test'));
      pushValue(element.getAttribute('data-testid'));
      pushValue(element.textContent);
    }
  }

  for (const scriptNode of Array.from(document.querySelectorAll('script')).slice(0, 220)) {
    pushValue(String(scriptNode.textContent || '').slice(0, 30000));
  }

  if (document.body && typeof document.body.innerText === 'string') {
    pushValue(document.body.innerText.slice(0, 180000));
  }

    const playbackInfo = loadPlaybackInfoFromResources();
    const nowPlaying = loadNowPlayingMetadata();
    return {
        text: values.join('\\n'),
        likedState: loadLikedState(),
        trackId: loadTrackId(nowPlaying),
        playbackApiQuality: playbackInfo.qualityValue,
        playbackApiMode: playbackInfo.modeValue,
        title: nowPlaying.title,
        artist: nowPlaying.artist,
        album: nowPlaying.album,
        trackUrl: nowPlaying.trackUrl,
        artUrl: nowPlaying.artUrl,
    };
})()
"""

    response = await send_method(
        'Runtime.evaluate',
        {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
        },
    )
    response_result = response.get('result') or {}
    if probe_debug and isinstance(response_result, dict):
        exception_details = response_result.get('exceptionDetails')
        if isinstance(exception_details, dict):
            print('cdp_probe_eval_exception=' + sanitize_value(str(exception_details.get('text') or 'unknown')))
        eval_error = response_result.get('result') or {}
        if isinstance(eval_error, dict) and str(eval_error.get('subtype') or '') == 'error':
            print('cdp_probe_eval_error=' + sanitize_value(str(eval_error.get('description') or 'unknown')))

    eval_result = ((response_result or {}).get('result') or {}).get('value') or {}
    if not isinstance(eval_result, dict):
        return '', '', '', '', '', '', '', '', '', ''
    return (
        str(eval_result.get('text') or ''),
        str(eval_result.get('likedState') or ''),
        str(eval_result.get('trackId') or ''),
        str(eval_result.get('playbackApiQuality') or ''),
        str(eval_result.get('playbackApiMode') or ''),
        str(eval_result.get('title') or ''),
        str(eval_result.get('artist') or ''),
        str(eval_result.get('album') or ''),
        str(eval_result.get('trackUrl') or ''),
        str(eval_result.get('artUrl') or ''),
    )

async def run() -> None:
    ws_targets = load_target_urls()
    if not ws_targets:
        return
    best_quality = ''
    best_quality_resolution = ''
    best_tonart = ''
    best_liked_state = ''
    best_track_id = ''
    best_audio_mode = ''
    best_title = ''
    best_artist = ''
    best_album = ''
    best_track_url = ''
    best_art_url = ''
    best_ws_url = ''
    best_track_context_ws_url = ''
    best_track_context_id = ''
    best_track_context_score = -1
    best_score = -1

    for ws_url, target_url in ws_targets:
        try:
            async with websockets.connect(ws_url, max_size=8_000_000) as websocket:
                message_id = 0

                async def send_method(method_name: str, params: dict | None = None) -> dict:
                    nonlocal message_id
                    message_id += 1
                    payload = {'id': message_id, 'method': method_name}
                    if params:
                        payload['params'] = params
                    await websocket.send(json.dumps(payload))
                    while True:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=max(1, timeout_s))
                        message = json.loads(raw_message)
                        if message.get('id') == message_id:
                            return message

                await send_method('Page.enable')
                await send_method('Runtime.enable')
                (
                    raw_text,
                    liked_state_value,
                    extracted_track_id,
                    playback_api_quality_raw,
                    playback_api_mode_raw,
                    extracted_title,
                    extracted_artist,
                    extracted_album,
                    extracted_track_url,
                    extracted_art_url,
                ) = await load_target_text(websocket, send_method=send_method)
        except Exception:
            if probe_debug:
                print('cdp_probe_target_error=1')
            continue

        if not raw_text:
            continue

        track_id = str(extracted_track_id or '').strip()
        if not track_id:
            track_id = load_track_id_from_url(target_url)
        if not track_id:
            track_id = load_track_id_from_text(raw_text)
        quality_value = sanitize_value(load_quality_value(raw_text))
        quality_resolution = sanitize_value(load_quality_resolution(raw_text))
        tonart_value = sanitize_value(load_tonart_value(raw_text))
        title_value = sanitize_value(str(extracted_title or '').strip())
        artist_value = sanitize_value(str(extracted_artist or '').strip())
        album_value = sanitize_value(str(extracted_album or '').strip())
        track_url_value = sanitize_value(str(extracted_track_url or '').strip())
        art_url_value = str(extracted_art_url or '').replace('\\n', ' ').replace('\\r', ' ').strip()
        if not re.match(r'^https?://', art_url_value, flags=re.IGNORECASE):
            art_url_value = ''
        liked_state = sanitize_value(str(liked_state_value or '').strip().casefold())
        if liked_state not in {'liked', 'not_liked'}:
            liked_state = ''

        playback_api_quality = sanitize_value(str(playback_api_quality_raw or '').strip())
        playback_api_mode = sanitize_value(str(playback_api_mode_raw or '').strip())
        if playback_api_quality and not quality_value:
            quality_value = playback_api_quality
        if not playback_api_mode and playback_api_quality:
            playback_api_mode = load_audio_mode_from_text(playback_api_quality)

        target_url_lower = str(target_url or '').lower()
        track_context_score = 0
        if track_id:
            track_context_score += 100
        if 'tidal.com' in target_url_lower:
            track_context_score += 80
        if '/track/' in target_url_lower or '/album/' in target_url_lower:
            track_context_score += 40
        if playback_api_quality:
            track_context_score += 20
        if playback_api_mode:
            track_context_score += 10

        if track_context_score > best_track_context_score:
            best_track_context_score = track_context_score
            best_track_context_ws_url = ws_url
            best_track_context_id = track_id

        score = 0
        if quality_value:
            score += 100
        if tonart_value:
            score += 60
        if liked_state:
            score += 20
        if title_value:
            score += 60
        if artist_value:
            score += 40
        if album_value:
            score += 20
        if art_url_value:
            score += 10
        if quality_resolution:
            score += 15
        score += min(len(raw_text), 200000) // 50000

        if probe_debug:
            print('cdp_probe_target_quality=' + quality_value)
            print('cdp_probe_target_tonart=' + tonart_value)
            print('cdp_probe_target_liked_state=' + liked_state)
            print('cdp_probe_target_track_id=' + track_id)
            print('cdp_probe_target_playback_api_quality=' + playback_api_quality)
            print('cdp_probe_target_playback_api_mode=' + playback_api_mode)
            print('cdp_probe_target_title=' + title_value)
            print('cdp_probe_target_artist=' + artist_value)
            print('cdp_probe_target_album=' + album_value)
            print('cdp_probe_target_track_url=' + track_url_value)
            print('cdp_probe_target_score=' + str(score))

        if score > best_score:
            best_score = score
            best_quality = quality_value
            best_quality_resolution = quality_resolution
            best_tonart = tonart_value
            best_liked_state = liked_state
            best_track_id = track_id
            best_audio_mode = playback_api_mode
            best_title = title_value
            best_artist = artist_value
            best_album = album_value
            best_track_url = track_url_value
            best_art_url = art_url_value
            best_ws_url = ws_url

        if best_quality and best_tonart and best_art_url:
            break

    if not best_track_id and best_track_context_id:
        best_track_id = best_track_context_id

    enrichment_ws_url = best_track_context_ws_url or best_ws_url
    if best_track_id and enrichment_ws_url and (not best_quality or not best_audio_mode or probe_debug):
        api_stream_quality, api_audio_mode, api_quality_resolution = await load_tidal_api_track_enrichment(
            ws_url=enrichment_ws_url,
            track_id=best_track_id,
        )
        if api_stream_quality and not best_quality:
            best_quality = api_stream_quality
        if api_audio_mode:
            best_audio_mode = api_audio_mode
        if api_quality_resolution and not best_quality_resolution:
            best_quality_resolution = api_quality_resolution
        if probe_debug:
            print('cdp_probe_api_stream_quality=' + api_stream_quality)
            print('cdp_probe_api_audio_mode=' + api_audio_mode)
            print('cdp_probe_api_quality_resolution=' + api_quality_resolution)
            print('cdp_probe_track_id=' + best_track_id)

    if not best_audio_mode and best_quality:
        best_audio_mode = load_audio_mode_from_text(best_quality)

    if best_quality:
        print('stream_quality=' + best_quality)
        print('cdp_stream_quality=' + best_quality)
    if best_quality_resolution:
        print('quality_resolution=' + best_quality_resolution)
        print('cdp_quality_resolution=' + best_quality_resolution)
    if best_tonart:
        print('tonart=' + best_tonart)
        print('cdp_tonart=' + best_tonart)
    if best_audio_mode:
        print('audio_mode=' + best_audio_mode)
        print('cdp_audio_mode=' + best_audio_mode)
    if best_title:
        print('cdp_title=' + best_title)
    if best_artist:
        print('cdp_artist=' + best_artist)
    if best_album:
        print('cdp_album=' + best_album)
    if best_track_url:
        print('cdp_track_url=' + best_track_url)
    if best_art_url:
        print('cdp_art_url=' + best_art_url)
    if best_track_id:
        print('tidal_track_id=' + best_track_id)
        print('cdp_tidal_track_id=' + best_track_id)
    if best_liked_state:
        print('liked_state=' + best_liked_state)
        print('liked=' + ('true' if best_liked_state == 'liked' else 'false'))
        print('cdp_liked_state=' + best_liked_state)
        print('cdp_liked=' + ('true' if best_liked_state == 'liked' else 'false'))
    if probe_debug:
        print('cdp_probe_best_quality=' + best_quality)
        print('cdp_probe_best_quality_resolution=' + best_quality_resolution)
        print('cdp_probe_best_tonart=' + best_tonart)
        print('cdp_probe_best_liked_state=' + best_liked_state)

try:
    asyncio.run(run())
except Exception:
    raise SystemExit(0)
PY
'''
        return env_prefix + cdp_script

    def _load_cdp_volume_sync_expression(self, *, target_volume_expression: str) -> str:
        volume_storage_key = json.dumps("alde-webplayer-volume-percent")
        target_expression = str(target_volume_expression or "0").strip() or "0"
        return f"""
(() => {{
    const volumeStorageKey = {volume_storage_key};
    const clamp = (value) => Math.max(0, Math.min(1, Number(value) || 0));
    const toPercent = (value) => Math.round(clamp(value) * 100);
    const loadStoredVolume = () => {{
        try {{
            if (!window.localStorage) return null;
            const storedValue = window.localStorage.getItem(volumeStorageKey);
            if (storedValue == null || storedValue === "") return null;
            return clamp(Number(storedValue) / 100);
        }} catch (error) {{
            return null;
        }}
    }};
    const readAppliedVolume = () => {{
        for (const element of document.querySelectorAll("audio,video")) {{
            if (typeof element.volume === "number" && Number.isFinite(element.volume)) {{
                return clamp(element.volume);
            }}
        }}
        try {{
            if (window.app && window.app.player && typeof window.app.player.getVolume === "function") {{
                return clamp(window.app.player.getVolume());
            }}
        }} catch (error) {{}}
        return window.__aldeVolumeSync ? clamp(window.__aldeVolumeSync.volume) : loadStoredVolume();
    }};
    const storeVolume = (value) => {{
        try {{
            if (window.localStorage) window.localStorage.setItem(volumeStorageKey, String(toPercent(value)));
        }} catch (error) {{}}
    }};
    const applyToMediaElements = (value) => {{
        let applied = false;
        for (const element of document.querySelectorAll("audio,video")) {{
            try {{
                element.volume = clamp(value);
                applied = true;
            }} catch (error) {{}}
        }}
        return applied;
    }};
    const applyToPlayer = (value) => {{
        try {{
            if (window.app && window.app.player && typeof window.app.player.setVolume === "function") {{
                window.app.player.setVolume(clamp(value));
                return true;
            }}
        }} catch (error) {{}}
        return false;
    }};
    const ensureVolumeKeeper = (value) => {{
        const keeper = window.__aldeVolumeSync || (window.__aldeVolumeSync = {{
            volume: clamp(value),
            apply(nextVolume) {{
                this.volume = clamp(nextVolume);
                storeVolume(this.volume);
                const mediaApplied = applyToMediaElements(this.volume);
                const playerApplied = mediaApplied ? false : applyToPlayer(this.volume);
                return {{
                    ok: mediaApplied || playerApplied,
                    method: mediaApplied ? "element_volume" : playerApplied ? "tidal_player_setVolume" : "stored_volume_only",
                    current_volume: this.volume,
                }};
            }},
            restore() {{
                const storedVolume = loadStoredVolume();
                if (storedVolume !== null) {{
                    this.volume = storedVolume;
                }}
                return this.apply(this.volume);
            }},
            schedule() {{
                if (this._scheduled) return;
                this._scheduled = true;
                const scheduleFrame = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 0));
                scheduleFrame(() => {{
                    this._scheduled = false;
                    this.apply(this.volume);
                }});
            }},
        }});
        if (!keeper._wired) {{
            keeper._wired = true;
            const observer = typeof MutationObserver === "function" ? new MutationObserver(() => keeper.schedule()) : null;
            if (observer) {{
                try {{
                    observer.observe(document.documentElement || document.body, {{ childList: true, subtree: true }});
                }} catch (error) {{}}
            }}
            document.addEventListener("loadedmetadata", () => keeper.schedule(), true);
            document.addEventListener("canplay", () => keeper.schedule(), true);
            document.addEventListener("play", () => keeper.schedule(), true);
        }}
        return keeper.apply(value);
    }};
    const requestedVolume = clamp({target_expression});
    return ensureVolumeKeeper(requestedVolume);
}})()
"""

    def _load_cdp_volume_command(self, *, volume_spec: str, cdp_port: int, cdp_timeout_s: int) -> str:
        env_prefix = (
            "CDP_ACTION=volume "
            + "CDP_VOLUME="
            + shlex.quote(str(volume_spec or ""))
            + f" CDP_PORT={int(cdp_port)} CDP_TIMEOUT_S={int(cdp_timeout_s)} "
        )
        expression = self._load_cdp_volume_sync_expression(target_volume_expression=str(volume_spec or "0"))
        expression_literal = json.dumps(expression)
        cdp_script = '''python3 - <<'PY'
import asyncio
import json
import os
import urllib.request

try:
    import websockets
except Exception:
    print('error=cdp_websockets_missing')
    raise SystemExit(10)

cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
timeout_s = int(str(os.environ.get('CDP_TIMEOUT_S') or '12'))

def load_target_url() -> str:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=5) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))
    page_targets = [target for target in targets if str(target.get('type') or '') == 'page']
    tidal_targets = [target for target in page_targets if 'tidal.com' in str(target.get('url') or '')]
    if not tidal_targets:
        raise RuntimeError('cdp_no_tidal_target')
    return str(tidal_targets[0].get('webSocketDebuggerUrl') or '')

async def run() -> int:
    ws_url = load_target_url()
    if not ws_url:
        print('error=cdp_no_ws_url')
        return 11

    async with websockets.connect(ws_url, max_size=8_000_000) as websocket:
        message_id = 0
        async def send_method(method_name: str, params: dict | None = None) -> dict:
            nonlocal message_id
            message_id += 1
            payload = {'id': message_id, 'method': method_name}
            if params:
                payload['params'] = params
            await websocket.send(json.dumps(payload))
            while True:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                if message.get('id') == message_id:
                    return message

        await send_method('Page.enable')
        await send_method('Runtime.enable')

        expression = ''' + expression_literal + '''

        for _ in range(max(1, timeout_s)):
            response = await send_method('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
            eval_result = ((response.get('result') or {}).get('result') or {}).get('value') or {}
            if isinstance(eval_result, dict) and bool(eval_result.get('ok')):
                print('cdp_volume_set=true')
                print('cdp_method=' + str(eval_result.get('method') or ''))
                print('cdp_volume=' + str(eval_result.get('current_volume') or ''))
                return 0
            await asyncio.sleep(1)

    print('error=cdp_volume_not_set')
    return 13

try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_volume_exception:' + str(exc))
    raise SystemExit(14)
PY
'''
        return env_prefix + cdp_script + " "
    def _load_open_url_and_play_command(
        self,
        *,
        target_url: str,
        player_selector: str,
        wait_for_page_s: int,
        wait_for_player_s: int,
        play_attempts: int,
        cdp_action: str,
        cdp_query: str,
        cdp_autoclick: bool,
        cdp_port: int,
        cdp_click_timeout_s: int,
    ) -> str:
        open_command = self._load_browser_open_command(
            target_url=target_url,
            log_file_path="/tmp/webplayer_mcp_target.log",
            allow_xdg_open_fallback=False,
            cdp_port=cdp_port,
        )
        initial_play_command = (
            "if command -v playerctl >/dev/null 2>&1 && [ -n \"$(playerctl -l 2>/dev/null || true)\" ]; then "
            + self._load_play_retry_command(
                player_selector=player_selector,
                wait_for_player_s=wait_for_player_s,
                play_attempts=play_attempts,
                require_playing=False,
            )
            + " fi; "
        )
        final_play_command = self._load_play_retry_command(
            player_selector=player_selector,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            require_playing=False,
        )

        player_selector_prefix = shlex.quote(str(player_selector or "chromium"))
        playback_probe_command = (
            "players_snapshot=$(playerctl -l 2>/dev/null || true); "
            "selected_player_snapshot=$(printf '%s\\n' \"$players_snapshot\" | awk -v prefix="
            + player_selector_prefix
            + " 'index($0,prefix)==1{print; exit}'); "
            "if [ -n \"$selected_player_snapshot\" ]; then status_snapshot=$(playerctl -p \"$selected_player_snapshot\" status 2>/dev/null || echo unknown); "
            "else status_snapshot=unknown; fi; "
        )

        if not cdp_autoclick:
            return (
                open_command
                + f" sleep {wait_for_page_s}; "
                + initial_play_command
                + " "
                + playback_probe_command
                + final_play_command
                + " status_after=$(printf '%s' \"$status\" | tr '[:upper:]' '[:lower:]'); "
                "if [ \"$status_after\" != \"playing\" ]; then echo warning=playback_not_started; fi; "
            )

        cdp_click_command = self._load_cdp_autoclick_command(
            action=cdp_action,
            query=cdp_query,
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )

        return (
            open_command
            + f" sleep {wait_for_page_s}; "
            + initial_play_command
            + " "
            + playback_probe_command
            + cdp_click_command
            + " cdp_exit_code=$?; if [ \"$cdp_exit_code\" -ne 0 ]; then echo error=cdp_click_failed; exit 3; fi; "
            + final_play_command
            + " status_after=$(printf '%s' \"$status\" | tr '[:upper:]' '[:lower:]'); "
            "if [ \"$status_after\" != \"playing\" ]; then echo warning=playback_not_started; fi; "
        )

    def _load_library_snapshot_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        library_url = str(arguments.get("library_url") or "").strip()
        if library_url:
            target_url = self._load_validated_tidal_url(library_url)
            if target_url is None:
                return "echo 'error=invalid_library_url'; exit 1;"
        else:
            section = str(arguments.get("section") or "favorites_tracks").strip().casefold()
            target_url = self._LIBRARY_SECTION_URLS.get(section)
            if target_url is None:
                supported_sections = ",".join(sorted(self._LIBRARY_SECTION_URLS.keys()))
                return f"echo 'error=invalid_library_section'; echo 'supported_sections={supported_sections}'; exit 1;"

        wait_for_page_s = self._load_bounded_int(
            arguments.get("wait_for_page_s"),
            default_value=4,
            minimum=0,
            maximum=120,
        )
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        max_items = self._load_bounded_int(
            arguments.get("max_items"),
            default_value=200,
            minimum=1,
            maximum=2000,
        )
        scroll_steps = self._load_bounded_int(
            arguments.get("scroll_steps"),
            default_value=24,
            minimum=1,
            maximum=500,
        )
        scroll_pause_ms = self._load_bounded_int(
            arguments.get("scroll_pause_ms"),
            default_value=250,
            minimum=10,
            maximum=5000,
        )

        cdp_snapshot_command = self._load_cdp_library_snapshot_command(
            cdp_port=cdp_port,
            max_items=max_items,
            scroll_steps=scroll_steps,
            scroll_pause_ms=scroll_pause_ms,
            target_url=target_url,
        )

        return (
            self._load_browser_open_command(
                target_url=target_url,
                log_file_path="/tmp/webplayer_mcp_snapshot.log",
                allow_xdg_open_fallback=False,
                cdp_port=cdp_port,
            )
            + f" sleep {wait_for_page_s}; "
            + cdp_snapshot_command
            + " snapshot_exit_code=$?; if [ \"$snapshot_exit_code\" -ne 0 ]; then echo 'error=library_snapshot_failed'; exit 3; fi; "
            + "echo section="
            + shlex.quote(str(arguments.get("section") or "favorites_tracks").strip().casefold() or "favorites_tracks")
            + ";"
        )

    def _load_cdp_library_snapshot_command(
        self,
        *,
        cdp_port: int,
        max_items: int,
        scroll_steps: int,
        scroll_pause_ms: int,
        target_url: str,
    ) -> str:
        env_prefix = (
            f"CDP_PORT={int(cdp_port)} "
            f"CDP_MAX_ITEMS={int(max_items)} "
            f"CDP_SCROLL_STEPS={int(scroll_steps)} "
            f"CDP_SCROLL_PAUSE_MS={int(scroll_pause_ms)} "
            + "CDP_TARGET_URL="
            + shlex.quote(str(target_url or "").strip())
            + " "
        )
        cdp_script = '''python3 - <<'PY'
import asyncio
import base64
import json
import os
import urllib.request

try:
    import websockets
except Exception:
    print('error=cdp_websockets_missing')
    raise SystemExit(10)

cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
max_items = int(str(os.environ.get('CDP_MAX_ITEMS') or '200'))
scroll_steps = int(str(os.environ.get('CDP_SCROLL_STEPS') or '24'))
scroll_pause_ms = int(str(os.environ.get('CDP_SCROLL_PAUSE_MS') or '250'))
target_url_hint = str(os.environ.get('CDP_TARGET_URL') or '').strip()

def load_target_url() -> str:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=5) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))

    page_targets = [target for target in targets if str(target.get('type') or '') == 'page']
    if not page_targets:
        raise RuntimeError('cdp_no_page_target')

    def normalize(value: object) -> str:
        return str(value or '').strip().casefold()

    normalized_hint = normalize(target_url_hint)
    hint_targets = [
        target
        for target in page_targets
        if normalized_hint and normalized_hint in normalize(target.get('url'))
    ]
    if hint_targets:
        chosen_target = hint_targets[-1]
        return str(chosen_target.get('webSocketDebuggerUrl') or '')

    tidal_targets = [target for target in page_targets if 'tidal.com' in str(target.get('url') or '')]
    if not tidal_targets:
        raise RuntimeError('cdp_no_tidal_target')

    favorite_targets = [target for target in tidal_targets if '/favorites/' in str(target.get('url') or '')]
    chosen_target = favorite_targets[-1] if favorite_targets else tidal_targets[-1]
    return str(chosen_target.get('webSocketDebuggerUrl') or '')

async def run() -> int:
    ws_url = load_target_url()
    if not ws_url:
        print('error=cdp_no_ws_url')
        return 11

    async with websockets.connect(ws_url, max_size=8_000_000) as websocket:
        message_id = 0

        async def send_method(method_name: str, params: dict | None = None) -> dict:
            nonlocal message_id
            message_id += 1
            payload = {'id': message_id, 'method': method_name}
            if params:
                payload['params'] = params
            await websocket.send(json.dumps(payload))
            while True:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                if message.get('id') == message_id:
                    return message

        await send_method('Page.enable')
        await send_method('Runtime.enable')

        if target_url_hint:
            await send_method('Page.navigate', {'url': target_url_hint})
            await asyncio.sleep(1.2)

        expression = """
(function () {
    var maxItems = %(max_items)d;
    var itemMap = Object.create(null);
    var itemList = [];

    function toText(value) {
        return String(value || '').replace(/\\s+/g, ' ').replace(/^\\s+|\\s+$/g, '');
    }

    function pushTrack(trackId, sourceUrl, titleText, artistText, albumText) {
        var normalizedId = toText(trackId);
        if (!normalizedId) return;
        if (itemMap[normalizedId]) return;
        itemMap[normalizedId] = true;
        itemList.push({
            tidal_track_id: normalizedId,
            title: toText(titleText),
            artist: toText(artistText),
            album: toText(albumText),
            liked: true,
            source_url: toText(sourceUrl || ('https://listen.tidal.com/track/' + normalizedId))
        });
    }

    function collectFromHtml() {
        var htmlNode = document.documentElement;
        var html = String((htmlNode && htmlNode.innerHTML) || '');
        if (!html) return;
        var patternList = [
            /\\/track\\/(\\d{4,})/gi,
            /\"track(?:Id|_id)\"\\s*:\\s*\"?(\\d{4,})\"?/gi,
            /trackId=(\\d{4,})/gi
        ];
        for (var i = 0; i < patternList.length; i += 1) {
            var pattern = patternList[i];
            var match = null;
            while ((match = pattern.exec(html)) !== null) {
                if (match && match[1]) {
                    pushTrack(String(match[1]), 'https://listen.tidal.com/track/' + String(match[1]), '', '', '');
                    if (itemList.length >= maxItems) return;
                }
            }
            if (itemList.length >= maxItems) return;
        }
    }

    function collectFromAnchors() {
        var anchors = document.querySelectorAll('a[href*="/track/"]');
        for (var i = 0; i < anchors.length; i += 1) {
            if (itemList.length >= maxItems) break;
            var anchor = anchors[i];
            var href = toText(anchor.getAttribute('href') || anchor.href || '');
            var match = href.match(/\\/track\\/(\\d{4,})/i);
            if (!match || !match[1]) continue;
            var row = anchor.closest ? (anchor.closest('tr,li,article,div') || anchor) : anchor;
            var rowText = toText((row && (row.innerText || row.textContent || '')) || '');
            var lines = rowText.split('\\\\n');
            var titleText = toText(anchor.innerText || (lines.length > 0 ? lines[0] : ''));
            var artistText = toText(lines.length > 1 ? lines[1] : '');
            var albumText = toText(lines.length > 2 ? lines[2] : '');
            pushTrack(String(match[1]), href, titleText, artistText, albumText);
        }
    }

    collectFromAnchors();
    if (itemList.length < maxItems) {
        collectFromHtml();
    }

    return {
        source_url: String((window.location && window.location.href) || ''),
        anchor_count: document.querySelectorAll('a[href*="/track/"]').length,
        tracks: itemList.slice(0, maxItems)
    };
})()
""" % {"max_items": int(max_items)}

        response = await send_method(
            'Runtime.evaluate',
            {
                'expression': expression,
                'returnByValue': True,
                'awaitPromise': True,
            },
        )

        runtime_result = response.get('result') if isinstance(response.get('result'), dict) else {}
        exception_details = runtime_result.get('exceptionDetails') if isinstance(runtime_result, dict) else None
        if isinstance(exception_details, dict):
            exception_text = str(exception_details.get('text') or '').strip()
            if exception_text:
                print('snapshot_eval_error=' + exception_text.replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))
            line_number = exception_details.get('lineNumber')
            column_number = exception_details.get('columnNumber')
            if isinstance(line_number, int) and isinstance(column_number, int):
                print('snapshot_eval_location=' + str(line_number) + ':' + str(column_number))
                expression_lines = str(expression or '').splitlines()
                if 0 <= line_number < len(expression_lines):
                    print('snapshot_eval_line=' + expression_lines[line_number].replace('=', '-'))
            exception_object = exception_details.get('exception') if isinstance(exception_details.get('exception'), dict) else {}
            exception_description = str(exception_object.get('description') or '').strip()
            if exception_description:
                print('snapshot_eval_description=' + exception_description.replace('=', '-').replace(chr(10), ' ').replace(chr(13), ' '))

        value = ((runtime_result.get('result') or {}).get('value') if isinstance(runtime_result, dict) else [])
        print('snapshot_value_type=' + type(value).__name__)
        snapshot_source_url = ''
        track_object_list: list[dict] = []
        if isinstance(value, dict):
            candidate_list = value.get('tracks')
            if isinstance(candidate_list, list):
                track_object_list = [dict(item) for item in candidate_list if isinstance(item, dict)]
            snapshot_source_url = str(value.get('source_url') or '').strip()
            snapshot_anchor_count = value.get('anchor_count')
            if isinstance(snapshot_anchor_count, (int, float, str)):
                try:
                    print('snapshot_anchor_count=' + str(int(snapshot_anchor_count)))
                except Exception:
                    pass
        elif isinstance(value, list):
            track_object_list = [dict(item) for item in value if isinstance(item, dict)]

        if snapshot_source_url:
            print('snapshot_source_url=' + snapshot_source_url)
        normalized_track_object_list = [dict(item) for item in track_object_list if isinstance(item, dict)]

        encoded_payload = base64.b64encode(
            json.dumps(normalized_track_object_list, ensure_ascii=False).encode('utf-8')
        ).decode('ascii')
        print('snapshot_count=' + str(len(normalized_track_object_list)))
        print('snapshot_json_base64=' + encoded_payload)
        return 0

try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_snapshot_exception:' + str(exc))
    raise SystemExit(13)
PY
'''
        return env_prefix + cdp_script + " "

    def _load_search_command(self, *, query: str | None) -> str:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return "echo 'error=missing_query'; exit 1;"

        encoded_query = quote_plus(normalized_query)
        search_url = f"https://listen.tidal.com/search/{encoded_query}"
        quoted_url = shlex.quote(search_url)
        return self._load_browser_open_command(
            target_url=search_url,
            log_file_path="/tmp/webplayer_mcp_search.log",
            allow_xdg_open_fallback=True,
        ) + " echo search_url=" + quoted_url + ";"

    def _load_search_play_command(self, *, query: str | None, player_selector: str, arguments: dict[str, Any]) -> str:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return "echo 'error=missing_query'; exit 1;"

        wait_for_page_s = self._load_bounded_int(
            arguments.get("wait_for_page_s"),
            default_value=4,
            minimum=0,
            maximum=120,
        )
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=15,
            minimum=0,
            maximum=120,
        )
        play_attempts = self._load_bounded_int(
            arguments.get("play_attempts"),
            default_value=8,
            minimum=1,
            maximum=20,
        )
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=12,
            minimum=1,
            maximum=120,
        )
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=False)
        target_url = f"https://listen.tidal.com/search/{quote_plus(normalized_query)}"
        return self._load_open_url_and_play_command(
            target_url=target_url,
            player_selector=player_selector,
            wait_for_page_s=wait_for_page_s,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            cdp_action="search",
            cdp_query=normalized_query,
            cdp_autoclick=cdp_autoclick,
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )

    def _load_playlist_play_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        playlist_url = str(arguments.get("playlist_url") or "").strip()
        playlist_id = str(arguments.get("playlist_id") or "").strip()
        if not playlist_url and not playlist_id:
            return "echo 'error=missing_playlist_target'; exit 1;"

        if playlist_url:
            target_url = self._load_validated_tidal_url(playlist_url)
            if target_url is None:
                return "echo 'error=invalid_playlist_url'; exit 1;"
        else:
            target_url = f"https://listen.tidal.com/playlist/{quote(playlist_id, safe='')}"

        wait_for_page_s = self._load_bounded_int(
            arguments.get("wait_for_page_s"),
            default_value=4,
            minimum=0,
            maximum=120,
        )
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=15,
            minimum=0,
            maximum=120,
        )
        play_attempts = self._load_bounded_int(
            arguments.get("play_attempts"),
            default_value=8,
            minimum=1,
            maximum=20,
        )
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=12,
            minimum=1,
            maximum=120,
        )
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=False)
        return self._load_open_url_and_play_command(
            target_url=target_url,
            player_selector=player_selector,
            wait_for_page_s=wait_for_page_s,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            cdp_action="playlist",
            cdp_query="",
            cdp_autoclick=cdp_autoclick,
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )

    def _load_library_play_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        raw_track_ids = arguments.get("track_ids")
        track_ids: list[str] = []
        if isinstance(raw_track_ids, list):
            for raw_track_id in raw_track_ids:
                track_id = str(raw_track_id or "").strip()
                if track_id and track_id not in track_ids:
                    track_ids.append(track_id)
        explicit_queue = bool(track_ids)
        if explicit_queue:
            self._browser_queue_track_ids = track_ids
            self._browser_queue_index = 0
            target_url = f"https://listen.tidal.com/track/{quote(track_ids[0], safe='')}"
        else:
            self._browser_queue_track_ids = []
            self._browser_queue_index = -1
            library_url = str(arguments.get("library_url") or "").strip()
            if library_url:
                target_url = self._load_validated_tidal_url(library_url)
                if target_url is None:
                    return "echo 'error=invalid_library_url'; exit 1;"
            else:
                section = str(arguments.get("section") or "favorites_tracks").strip().casefold()
                target_url = self._LIBRARY_SECTION_URLS.get(section)
                if target_url is None:
                    supported_sections = ",".join(sorted(self._LIBRARY_SECTION_URLS.keys()))
                    return f"echo 'error=invalid_library_section'; echo 'supported_sections={supported_sections}'; exit 1;"

        wait_for_page_s = self._load_bounded_int(
            arguments.get("wait_for_page_s"),
            default_value=4,
            minimum=0,
            maximum=120,
        )
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=15,
            minimum=0,
            maximum=120,
        )
        play_attempts = self._load_bounded_int(
            arguments.get("play_attempts"),
            default_value=8,
            minimum=1,
            maximum=20,
        )
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=12,
            minimum=1,
            maximum=120,
        )
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=False)
        return self._load_open_url_and_play_command(
            target_url=target_url,
            player_selector=player_selector,
            wait_for_page_s=wait_for_page_s,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            cdp_action="track" if explicit_queue else "library",
            cdp_query=track_ids[0] if explicit_queue else "",
            cdp_autoclick=True if explicit_queue else cdp_autoclick,
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )

    def _load_open_playback_target_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        target_url = str(arguments.get("target_url") or "").strip()
        if not target_url:
            return "echo 'error=missing_target_url'; exit 1;"

        validated_target_url = self._load_validated_tidal_url(target_url)
        if validated_target_url is None:
            return "echo 'error=invalid_target_url'; exit 1;"

        wait_for_page_s = self._load_bounded_int(
            arguments.get("wait_for_page_s"),
            default_value=4,
            minimum=0,
            maximum=120,
        )
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=15,
            minimum=0,
            maximum=120,
        )
        play_attempts = self._load_bounded_int(
            arguments.get("play_attempts"),
            default_value=8,
            minimum=1,
            maximum=20,
        )
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=12,
            minimum=1,
            maximum=120,
        )
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=False)
        return self._load_open_url_and_play_command(
            target_url=validated_target_url,
            player_selector=player_selector,
            wait_for_page_s=wait_for_page_s,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            cdp_action="generic",
            cdp_query="",
            cdp_autoclick=cdp_autoclick,
            cdp_port=cdp_port,
            cdp_click_timeout_s=cdp_click_timeout_s,
        )

    def _load_volume_set_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        volume_percent = arguments.get("volume_percent")
        volume_float = arguments.get("volume_float")

        try:
            if volume_percent is not None:
                pct = int(volume_percent)
            elif volume_float is not None:
                pct = round(float(volume_float) * 100)
            else:
                return "echo 'error=missing_volume'; exit 1;"
        except Exception:
            return "echo 'error=invalid_volume_value'; exit 1;"

        normalized_percent = max(0, min(100, int(pct)))
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_timeout_s = self._load_bounded_int(
            arguments.get("cdp_timeout_s", arguments.get("cdp_click_timeout_s")),
            default_value=12,
            minimum=1,
            maximum=120,
        )
        cdp_command = self._load_cdp_volume_command(
            volume_spec=f"{normalized_percent / 100:.3f}",
            cdp_port=cdp_port,
            cdp_timeout_s=cdp_timeout_s,
        )
        return self._load_volume_command_with_mpris_fallback(
            cdp_command=cdp_command,
            player_selector=player_selector,
            volume_argument=normalized_percent / 100.0,
            is_relative=False,
        )

    def _load_volume_command_with_mpris_fallback(
        self,
        *,
        cdp_command: str,
        player_selector: str,
        volume_argument: float,
        is_relative: bool,
    ) -> str:
        volume_mode = "relative" if is_relative else "absolute"
        volume_argument_text = shlex.quote(str(volume_argument))
        return (
            "cdp_output=$(" + cdp_command + "); cdp_exit_code=$?; "
            "if [ \"$cdp_exit_code\" -eq 0 ]; then "
            "printf '%s\\n' \"$cdp_output\"; echo controller=wplctl; echo volume_control=browser; "
            "else "
            "if ! command -v playerctl >/dev/null 2>&1; then printf '%s\\n' \"$cdp_output\"; exit \"$cdp_exit_code\"; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "current_volume=$(LC_ALL=C playerctl -p \"$selected_player\" volume 2>/dev/null) || exit 1; "
            + "target_volume=$(LC_ALL=C awk -v current=\"$current_volume\" -v argument=" + volume_argument_text
            + " -v mode=" + shlex.quote(volume_mode)
            + " 'BEGIN { value=(mode==\"relative\" ? current+argument : argument); "
            "if (value<0) value=0; if (value>1) value=1; printf \"%.6f\", value }'); "
            "if ! LC_ALL=C playerctl -p \"$selected_player\" volume \"$target_volume\" 2>/dev/null; then echo error=mpris_volume_failed; exit 1; fi; "
            "applied_volume=$(LC_ALL=C playerctl -p \"$selected_player\" volume 2>/dev/null) || exit 1; "
            "echo controller=playerctl; echo volume_control=browser; "
            "echo volume_percent=$(LC_ALL=C awk -v volume=\"$applied_volume\" 'BEGIN { printf \"%.0f\", volume*100 }'); "
            "fi; "
        )

    def _load_cdp_volume_adjust_command(self, *, delta_float: float, cdp_port: int, cdp_timeout_s: int) -> str:
        env_prefix = (
            "CDP_ACTION=volume_adjust "
            + "CDP_DELTA="
            + shlex.quote(str(delta_float))
            + f" CDP_PORT={int(cdp_port)} CDP_TIMEOUT_S={int(cdp_timeout_s)} "
        )
        expression = self._load_cdp_volume_sync_expression(
            target_volume_expression=f"(readAppliedVolume() === null ? 0 : readAppliedVolume()) + ({delta_float})",
        )
        expression_literal = json.dumps(expression)
        cdp_script = '''python3 - <<'PY'
import asyncio
import json
import os
import urllib.request

try:
    import websockets
except Exception:
    print('error=cdp_websockets_missing')
    raise SystemExit(10)

cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))
timeout_s = int(str(os.environ.get('CDP_TIMEOUT_S') or '12'))

def load_target_url() -> str:
    with urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json/list', timeout=5) as response:
        targets = json.loads(response.read().decode('utf-8', errors='replace'))
    page_targets = [target for target in targets if str(target.get('type') or '') == 'page']
    tidal_targets = [target for target in page_targets if 'tidal.com' in str(target.get('url') or '')]
    if not tidal_targets:
        raise RuntimeError('cdp_no_tidal_target')
    return str(tidal_targets[0].get('webSocketDebuggerUrl') or '')

async def run() -> int:
    ws_url = load_target_url()
    if not ws_url:
        print('error=cdp_no_ws_url')
        return 11

    async with websockets.connect(ws_url, max_size=8_000_000) as websocket:
        message_id = 0
        async def send_method(method_name: str, params: dict | None = None) -> dict:
            nonlocal message_id
            message_id += 1
            payload = {'id': message_id, 'method': method_name}
            if params:
                payload['params'] = params
            await websocket.send(json.dumps(payload))
            while True:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                if message.get('id') == message_id:
                    return message

        await send_method('Page.enable')
        await send_method('Runtime.enable')

        expression = ''' + expression_literal + '''

        for _ in range(max(1, timeout_s)):
            response = await send_method('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
            eval_result = ((response.get('result') or {}).get('result') or {}).get('value') or {}
            if isinstance(eval_result, dict) and bool(eval_result.get('ok')):
                print('cdp_volume_adjust=true')
                print('cdp_method=' + str(eval_result.get('method') or ''))
                print('cdp_volume=' + str(eval_result.get('current_volume') or ''))
                return 0
            await asyncio.sleep(1)

    print('error=cdp_volume_not_adjusted')
    return 13

try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_volume_adjust_exception:' + str(exc))
    raise SystemExit(14)
PY
'''
        return env_prefix + cdp_script + " "
    def _load_volume_adjust_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        delta_percent = arguments.get("delta_percent")
        delta_float = arguments.get("delta_float")

        try:
            if delta_percent is not None:
                delta_pct = int(delta_percent)
            elif delta_float is not None:
                delta_pct = int(round(float(delta_float) * 100))
            else:
                return "echo 'error=missing_delta'; exit 1;"
        except Exception:
            return "echo 'error=invalid_delta_value'; exit 1;"

        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=1,
            maximum=65535,
        )
        cdp_timeout_s = self._load_bounded_int(
            arguments.get("cdp_timeout_s", arguments.get("cdp_click_timeout_s")),
            default_value=12,
            minimum=1,
            maximum=120,
        )
        cdp_command = self._load_cdp_volume_adjust_command(
            delta_float=delta_pct / 100.0,
            cdp_port=cdp_port,
            cdp_timeout_s=cdp_timeout_s,
        )
        return self._load_volume_command_with_mpris_fallback(
            cdp_command=cdp_command,
            player_selector=player_selector,
            volume_argument=delta_pct / 100.0,
            is_relative=True,
        ) + f"echo delta_percent={delta_pct};"

    def _load_system_volume_set_command(self, *, arguments: dict[str, Any]) -> str:
        volume_percent = arguments.get("volume_percent")
        volume_float = arguments.get("volume_float")
        try:
            if volume_percent is not None:
                pct = int(volume_percent)
            elif volume_float is not None:
                pct = round(float(volume_float) * 100)
            else:
                return "echo 'error=missing_volume'; exit 1;"
        except Exception:
            return "echo 'error=invalid_volume_value'; exit 1;"

        normalized_percent = max(0, min(100, int(pct)))
        normalized_float = f"{normalized_percent / 100:.3f}"
        return (
            "if command -v wpctl >/dev/null 2>&1; then "
            "wpctl set-volume @DEFAULT_AUDIO_SINK@ " + shlex.quote(normalized_float) + "; "
            "applied_volume=$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null) || exit 1; "
            "volume_percent=$(printf '%s\\n' \"$applied_volume\" | LC_ALL=C awk '/Volume:/ {printf \"%.0f\", $2*100; exit}'); "
            "echo controller=wpctl; echo volume_control=system; echo volume_target=sink; echo volume_sink=@DEFAULT_AUDIO_SINK@; echo sink=@DEFAULT_AUDIO_SINK@; echo volume_percent=$volume_percent; "
            "elif command -v pactl >/dev/null 2>&1; then "
            "pactl set-sink-volume @DEFAULT_SINK@ " + str(normalized_percent) + "%; "
            "applied_volume=$(pactl get-sink-volume @DEFAULT_SINK@ 2>/dev/null) || exit 1; "
            "volume_percent=$(printf '%s\\n' \"$applied_volume\" | grep -oE '[0-9]+%' | head -1 | tr -d '%'); "
            "echo controller=pactl; echo volume_control=system; echo volume_target=sink; echo volume_sink=@DEFAULT_SINK@; echo sink=@DEFAULT_SINK@; echo volume_percent=$volume_percent; "
            "else echo error=system_volume_control_unavailable; exit 1; fi"
        )

    def _load_system_volume_adjust_command(self, *, arguments: dict[str, Any]) -> str:
        delta_percent = arguments.get("delta_percent")
        delta_float = arguments.get("delta_float")
        try:
            if delta_percent is not None:
                delta_pct = int(delta_percent)
            elif delta_float is not None:
                delta_pct = int(round(float(delta_float) * 100))
            else:
                return "echo 'error=missing_delta'; exit 1;"
        except Exception:
            return "echo 'error=invalid_delta_value'; exit 1;"

        return (
            "if command -v wpctl >/dev/null 2>&1; then "
            "current_volume=$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null) || exit 1; "
            "current_percent=$(printf '%s\\n' \"$current_volume\" | LC_ALL=C awk '/Volume:/ {printf \"%.0f\", $2*100; exit}'); "
            "target_percent=$(LC_ALL=C awk -v current=\"$current_percent\" -v delta=" + str(delta_pct) + " 'BEGIN { value=current+delta; if (value<0) value=0; if (value>100) value=100; printf \"%.0f\", value }'); "
            "wpctl set-volume @DEFAULT_AUDIO_SINK@ \"$(LC_ALL=C awk -v value=\"$target_percent\" 'BEGIN { printf \"%.3f\", value/100 }')\"; "
            "echo controller=wpctl; echo volume_control=system; echo volume_target=sink; echo volume_sink=@DEFAULT_AUDIO_SINK@; echo sink=@DEFAULT_AUDIO_SINK@; echo volume_percent=$target_percent; echo delta_percent=" + str(delta_pct) + "; "
            "elif command -v pactl >/dev/null 2>&1; then "
            "current_volume=$(pactl get-sink-volume @DEFAULT_SINK@ 2>/dev/null) || exit 1; "
            "current_percent=$(printf '%s\\n' \"$current_volume\" | grep -oE '[0-9]+%' | head -1 | tr -d '%'); "
            "target_percent=$(awk -v current=\"$current_percent\" -v delta=" + str(delta_pct) + " 'BEGIN { value=current+delta; if (value<0) value=0; if (value>100) value=100; printf \"%.0f\", value }'); "
            "pactl set-sink-volume @DEFAULT_SINK@ \"$target_percent%\"; "
            "echo controller=pactl; echo volume_control=system; echo volume_target=sink; echo volume_sink=@DEFAULT_SINK@; echo sink=@DEFAULT_SINK@; echo volume_percent=$target_percent; echo delta_percent=" + str(delta_pct) + "; "
            "else echo error=system_volume_control_unavailable; exit 1; fi"
        )

    def _load_validated_tidal_url(self, raw_url: str) -> str | None:
        parsed_url = urlparse(str(raw_url or "").strip())
        if parsed_url.scheme not in {"http", "https"}:
            return None
        hostname = (parsed_url.hostname or "").casefold()
        if hostname != "tidal.com" and not hostname.endswith(".tidal.com"):
            return None
        return parsed_url.geturl()

    def _load_bounded_int(self, raw_value: Any, *, default_value: int, minimum: int, maximum: int) -> int:
        try:
            normalized_value = int(raw_value)
        except Exception:
            normalized_value = int(default_value)
        return max(minimum, min(maximum, normalized_value))

    def _load_boolean_argument(self, raw_value: Any, *, default_value: bool) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if raw_value is None:
            return default_value

        normalized_value = str(raw_value).strip().casefold()
        if normalized_value in {"1", "true", "yes", "on"}:
            return True
        if normalized_value in {"0", "false", "no", "off"}:
            return False
        return default_value


class WebPlayerMcpRequestService:
    _SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-03-26")
    _DEFAULT_PROTOCOL_VERSION = "2025-03-26"
    _UI_EXTENSION_NAME = "io.modelcontextprotocol/ui"
    _UI_APP_NAME = "Mpc.App.ui"
    _UI_RESOURCE_URI = f"ui://{_UI_APP_NAME}"
    _UI_LEGACY_RESOURCE_URIS = frozenset({"ui://WebMpc.app.ui"})
    _UI_COVER_HTTP_PATH = f"/ui/{_UI_APP_NAME}/cover"
    _UI_PREVIEW_HTTP_PATH = f"/ui/{_UI_APP_NAME}/preview"
    _UI_SPECTRUM_HTTP_PATH = f"/ui/{_UI_APP_NAME}/spectrum"
    _UI_SCOPE_HTTP_PATH = f"/ui/{_UI_APP_NAME}/scope"
    _UI_LEGACY_SCOPE_HTTP_PATHS = frozenset(
        f"/ui/{uri.split('://', 1)[1]}/scope"
        for uri in _UI_LEGACY_RESOURCE_URIS
    )
    _UI_SCOPE_TOOL_NAME = "webplayer_playback_scope"
    _UI_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
    _UI_RESOURCE_META = {
        "ui": {
            "prefersBorder": False,
            "csp": {
                "connectDomains": [
                    "http://192.168.0.47:8766",
                    "http://192.168.0.48:8766",
                    "http://localhost:8766",
                    "http://127.0.0.1:8766",
                    "https://cdn.jsdelivr.net",
                    "https://openapi.tidal.com",
                    "https://api.tidal.com",
                    "https://im-fa.manifest.tidal.com",
                    "https://sp-ad-fa.audio.tidal.com",
                    "https://*.tidal.com",
                ],
                "resourceDomains": [
                    "https://cdn.jsdelivr.net",
                    "https://resources.tidal.com",
                    "https://images.tidal.com",
                    "https://im-fa.manifest.tidal.com",
                    "https://sp-ad-fa.audio.tidal.com",
                    "https://*.tidal.com",
                ],
            },
        }
    }
    _STRATEGY_RISK_TIER_ALLOWED_VALUES = {"low", "standard", "high", "critical"}
    _STRATEGY_RISK_TIER_DEFAULT = "standard"
    _STRATEGY_COMPLIANCE_MODE_ALLOWED_VALUES = {"strict", "balanced", "adaptive"}
    _STRATEGY_COMPLIANCE_MODE_DEFAULT = "balanced"
    _WEBPLAYER_PLAYBACK_TOOL_NAMES = (
        "webplayer_play",
        "webplayer_pause",
        "webplayer_seek",
        "webplayer_get_media_product",
        "webplayer_get_next_media_product",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
    )
    _WEBPLAYER_STATE_TOOL_NAMES = (
        "webplayer_now_playing",
        "webplayer_stop",
        "webplayer_forward",
        "webplayer_backward",
        "webplayer_like_current",
        "webplayer_toggle_like_current",
        "webplayer_pause",
        "webplayer_seek",
        "webplayer_get_media_product",
        "webplayer_get_next_media_product",
        "webplayer_search",
        "webplayer_library_snapshot",
        "webplayer_volume_set",
        "webplayer_volume_adjust",
        "webplayer_system_volume_set",
        "webplayer_system_volume_adjust",
    )
    _WEBPLAYER_AUTH_TOOL_NAMES = ("webplayer_usrauth",)
    _TIDAL_API_TOOL_NAMES = (
        "tidal_api_request",
        "tidal_api_add_track_to_playlist",
        "tidal_api_add_track_to_collection",
        "tidal_api_search",
        "tidal_api_track",
        "tidal_api_track_manifest",
        "tidal_api_widevine",
    )

    def __init__(
        self,
        player_service: WebPlayerService | None = None,
        tidal_api_service: TidalApiService | None = None,
        user_auth_service: TidalUserAuthService | None = None,
    ) -> None:
        self.player_service = player_service or WebPlayerService()
        self.tidal_api_service = tidal_api_service or TidalApiService()
        shared_token_service = getattr(self.tidal_api_service, "oauth_token_service", None)
        self.user_auth_service = user_auth_service or TidalUserAuthService(token_service=shared_token_service)
        self.ui_session_registry = McpUiSessionRegistry()

    def _normalize_prompt_argument(
        self,
        raw_value: Any,
        *,
        default_value: str,
        allowed_values: set[str],
    ) -> tuple[str, str, bool]:
        raw_text = str(raw_value or "").strip()
        if not raw_text:
            return "", default_value, True

        normalized_text = raw_text.casefold()
        if normalized_text not in allowed_values:
            return raw_text, default_value, False
        return raw_text, normalized_text, True

    def _build_webplayer_strategy_overview_text(self, *, player_selector: str) -> str:
        backend_guidance = "- Use playback_backend='browser' for every WebPlayer control and information call. The Chromium browser backend is controlled by wplctl."
        selector_guidance = "- Use player_selector='chromium'; it identifies the Chromium WebPlayer controlled by wplctl."
        return "\n".join(
            [
                "WebPlayer MCP strategy overview:",
                "- Use prompts/get before tools/call for explicit execution policy.",
                "- Resolve player_selector first and keep it stable across a session.",
                "- Prefer webplayer_now_playing after each state-changing tool call.",
                "- For playback start, prefer webplayer_search_play, webplayer_playlist_play, webplayer_library_play, or webplayer_open_playback_target over manual sequences.",
                "- Use webplayer_usrauth to initialize, finalize, or repair TIDAL user login when the token file is missing, expired, or an API call returns 401.",
                backend_guidance,
                "- Direct tidal_api_* calls are for read-only API information and never control playback.",
                f"- Playback tools={', '.join(self._WEBPLAYER_PLAYBACK_TOOL_NAMES)}",
                f"- State tools={', '.join(self._WEBPLAYER_STATE_TOOL_NAMES)}",
                f"- Auth tools={', '.join(self._WEBPLAYER_AUTH_TOOL_NAMES)}",
                f"- TIDAL API tools={', '.join(self._TIDAL_API_TOOL_NAMES)}",
                "- When the workflow needs richer context, consult the AgentsDB MCP layer for graph/workflow/sequence context via agentsdb/mcp.",
                selector_guidance,
                "- When tool results are ambiguous, report stderr/stdout and propose the next corrective action.",
            ]
        ).strip()

    def _build_webplayer_ifaai_strategy_text(
        self,
        *,
        player_selector: str,
        risk_tier: str,
        compliance_mode: str,
    ) -> str:
        raw_risk_tier, normalized_risk_tier, risk_tier_is_valid = self._normalize_prompt_argument(
            risk_tier,
            default_value=self._STRATEGY_RISK_TIER_DEFAULT,
            allowed_values=self._STRATEGY_RISK_TIER_ALLOWED_VALUES,
        )
        raw_compliance_mode, normalized_compliance_mode, compliance_mode_is_valid = self._normalize_prompt_argument(
            compliance_mode,
            default_value=self._STRATEGY_COMPLIANCE_MODE_DEFAULT,
            allowed_values=self._STRATEGY_COMPLIANCE_MODE_ALLOWED_VALUES,
        )

        validation_warnings: list[str] = []
        if raw_risk_tier and not risk_tier_is_valid:
            validation_warnings.append(
                f"- validation_warning=risk_tier '{raw_risk_tier}' defaulted to {self._STRATEGY_RISK_TIER_DEFAULT}"
            )
        if raw_compliance_mode and not compliance_mode_is_valid:
            validation_warnings.append(
                f"- validation_warning=compliance_mode '{raw_compliance_mode}' defaulted to {self._STRATEGY_COMPLIANCE_MODE_DEFAULT}"
            )

        return "\n".join(
            [
                "Generalized agentic governance profile (IFAAI-aligned baseline):",
                "- profile_source=provisional_baseline",
                "- normative_status=non_authoritative_fallback",
                f"- player_selector={player_selector}",
                f"- risk_tier_raw={raw_risk_tier or '(not provided)'}",
                f"- risk_tier_effective={normalized_risk_tier}",
                f"- compliance_mode_raw={raw_compliance_mode or '(not provided)'}",
                f"- compliance_mode_effective={normalized_compliance_mode}",
                *validation_warnings,
                "",
                "Core principles:",
                "- Human oversight: require explicit confirmation for state-changing actions when uncertainty is present.",
                "- Safety and robustness: keep tool execution deterministic and bounded.",
                "- Transparency: report tool outcomes, errors, and the next action clearly.",
                "- Accountability: retain traces for control actions and resulting state changes.",
                "",
                "Operational controls:",
                "- Before action, resolve player state and confirm the intended action.",
                "- After each action, call webplayer_now_playing and summarize status/title/artist/album.",
                "- Use webplayer_search only with a non-empty search query.",
                "- If a tool fails, return stderr/stdout and propose the next corrective step.",
            ]
        ).strip()

    def dispatch_object(self, payload: Any, *, request_headers: dict[str, str] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"error": "invalid_json"}

        if str(payload.get("jsonrpc") or "").strip() == "2.0":
            return self._dispatch_jsonrpc_object(payload=payload, request_headers=request_headers)

        return self._dispatch_legacy_object(payload)

    def _dispatch_legacy_object(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = str(payload.get("method") or "")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}

        handlers = {
            "initialize": self.load_initialize_result,
            "resources/list": self.load_resources_list_result,
            "resources/read": self.load_resources_read_result,
            "tools/list": self.load_tools_list_result,
            "tools/call": self.load_tools_call_result,
            "prompts/list": self.load_prompts_list_result,
            "prompts/get": self.load_prompts_get_result,
            "server/discover": self.load_server_discover_result,
        }
        handler = handlers.get(method)
        if handler is None:
            return {"error": "method_not_implemented"}
        return handler(params)

    def _dispatch_jsonrpc_object(self, *, payload: dict[str, Any], request_headers: dict[str, str] | None = None) -> dict[str, Any]:
        request_id = payload.get("id")
        method = str(payload.get("method") or "").strip()
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        request_context = self._load_request_context_payload(
            method=method,
            params=params,
            request_headers=request_headers,
        )

        if not method:
            return self._load_jsonrpc_error_payload(
                request_id=request_id,
                code=-32600,
                message="Invalid request: missing method",
            )

        handlers = {
            "initialize": lambda call_params: self._load_initialize_result_mcp(call_params, request_context=request_context),
            "initialized": self._load_notification_ack_result_mcp,
            "notifications/initialized": self._load_notification_ack_result_mcp,
            "notifications/cancelled": self._load_notification_ack_result_mcp,
            "server/discover": lambda call_params: self._load_server_discover_result_mcp(call_params, request_context=request_context),
            "resources/list": lambda call_params: self._load_resources_list_result_mcp(call_params, request_context=request_context),
            "resources/read": lambda call_params: self._load_resources_read_result_mcp(call_params, request_context=request_context),
            "tools/list": lambda call_params: self._load_tools_list_result_mcp(call_params, request_context=request_context),
            "tools/call": self._load_tools_call_result_mcp,
            "prompts/list": self._load_prompts_list_result_mcp,
            "prompts/get": self._load_prompts_get_result_mcp,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._load_jsonrpc_error_payload(
                request_id=request_id,
                code=-32601,
                message=f"Method not found: {method}",
            )

        try:
            result_payload = handler(params)
        except Exception as exc:  # noqa: BLE001
            return self._load_jsonrpc_error_payload(
                request_id=request_id,
                code=-32603,
                message=f"Internal error: {exc}",
            )
        return self._load_jsonrpc_success_payload(request_id=request_id, result_payload=result_payload)

    @staticmethod
    def _load_header_value_from_dict(request_headers: dict[str, str] | None, header_name: str) -> str:
        if not request_headers:
            return ""
        target_name = str(header_name or "").strip().casefold()
        if not target_name:
            return ""
        for key, value in request_headers.items():
            if str(key or "").strip().casefold() == target_name:
                return str(value or "").strip()
        return ""

    @staticmethod
    def _load_csv_values(raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            return [str(item).strip() for item in raw_value if str(item).strip()]
        return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]

    def _load_ui_capability_supported(self, *, params: dict[str, Any]) -> bool:
        capabilities_payload = params.get("capabilities")
        if not isinstance(capabilities_payload, dict):
            return False

        extensions_payload = capabilities_payload.get("extensions")
        if isinstance(extensions_payload, dict) and self._UI_EXTENSION_NAME in extensions_payload:
            ui_capability_payload = extensions_payload.get(self._UI_EXTENSION_NAME)
            if isinstance(ui_capability_payload, dict):
                mime_types = ui_capability_payload.get("mimeTypes")
                if isinstance(mime_types, list) and mime_types:
                    normalized_mime_types = {
                        str(mime_type or "").strip().casefold()
                        for mime_type in mime_types
                        if str(mime_type or "").strip()
                    }
                    if (
                        self._UI_RESOURCE_MIME_TYPE.casefold() not in normalized_mime_types
                        and "text/html" not in normalized_mime_types
                        and "*/*" not in normalized_mime_types
                    ):
                        return False
            return True

        experimental_payload = capabilities_payload.get("experimental")
        if isinstance(experimental_payload, dict) and self._UI_EXTENSION_NAME in experimental_payload:
            return True

        return False

    def _load_ui_extension_requested(
        self,
        *,
        method: str,
        params: dict[str, Any],
        request_headers: dict[str, str] | None,
    ) -> bool:
        if self._load_ui_capability_supported(params=params):
            return True

        # Capability negotiation belongs to initialize. The following markers
        # remain opt-in compatibility paths for direct legacy requests.
        if str(method or "").strip().casefold() == "initialize":
            ui_default_env = str(os.getenv("WEBPLAYER_MCP_UI_EXTENSION_DEFAULT") or "1").strip().casefold()
            return ui_default_env not in {"0", "false", "no", "off"}

        meta_payload = params.get("_meta")
        if isinstance(meta_payload, dict):
            meta_extensions = self._load_csv_values(meta_payload.get("io.modelcontextprotocol/extensions"))
            if self._UI_EXTENSION_NAME in meta_extensions:
                return True

        header_extensions = self._load_csv_values(
            self._load_header_value_from_dict(request_headers, "MCP-Extensions")
        )
        if self._UI_EXTENSION_NAME in header_extensions:
            return True

        ui_default_env = str(os.getenv("WEBPLAYER_MCP_UI_EXTENSION_DEFAULT") or "1").strip().casefold()
        return ui_default_env not in {"0", "false", "no", "off"}

    def _load_request_context_payload(
        self,
        *,
        method: str,
        params: dict[str, Any],
        request_headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        normalized_method = str(method or "").strip().casefold()
        session_id = self._load_header_value_from_dict(request_headers, "MCP-Session-Id")
        ui_enabled = self._load_ui_extension_requested(
            method=normalized_method,
            params=params,
            request_headers=request_headers,
        )
        if normalized_method == "initialize":
            self.ui_session_registry.store_ui_capability(session_id, ui_enabled=ui_enabled)
        else:
            ui_enabled = ui_enabled or self.ui_session_registry.is_ui_enabled(session_id)
        return {
            "ui_enabled": ui_enabled,
            "session_id": session_id,
        }

    def _load_jsonrpc_success_payload(self, *, request_id: Any, result_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": dict(result_payload or {}),
        }

    def _load_jsonrpc_error_payload(
        self,
        *,
        request_id: Any,
        code: int,
        message: str,
        data: Any | None = None,
    ) -> dict[str, Any]:
        error_payload: dict[str, Any] = {
            "code": int(code),
            "message": str(message or "error"),
        }
        if data is not None:
            error_payload["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error_payload,
        }

    def _load_server_capabilities_payload(self, *, ui_enabled: bool = False) -> dict[str, Any]:
        capabilities_payload: dict[str, Any] = {
            "tools": {
                "listChanged": False,
            },
            "prompts": {},
            "resources": {
                "listChanged": False,
            },
        }
        if ui_enabled:
            capabilities_payload["extensions"] = {
                self._UI_EXTENSION_NAME: {
                    "resourceUri": self._UI_RESOURCE_URI,
                }
            }
        return capabilities_payload

    def _load_initialize_result_mcp(self, _params: dict[str, Any], *, request_context: dict[str, Any]) -> dict[str, Any]:
        ui_enabled = bool(request_context.get("ui_enabled"))
        initialize_result = dict((self.load_initialize_result(_params, ui_enabled=ui_enabled) or {}).get("result") or {})
        initialize_result.setdefault("resultType", "complete")
        initialize_result["supportedVersions"] = list(self._SUPPORTED_PROTOCOL_VERSIONS)
        initialize_result["capabilities"] = self._load_server_capabilities_payload(ui_enabled=ui_enabled)
        return initialize_result

    def _load_server_discover_result_mcp(self, _params: dict[str, Any], *, request_context: dict[str, Any]) -> dict[str, Any]:
        ui_enabled = bool(request_context.get("ui_enabled"))
        discover_result = dict((self.load_server_discover_result(_params, ui_enabled=ui_enabled) or {}).get("result") or {})
        discover_result.setdefault("resultType", "complete")
        return discover_result

    def _load_notification_ack_result_mcp(self, _params: dict[str, Any]) -> dict[str, Any]:
        # Compatibility no-op for MCP notifications sent as JSON-RPC calls.
        return {
            "resultType": "complete",
            "acknowledged": True,
        }

    def _load_resources_list_result_mcp(self, _params: dict[str, Any], *, request_context: dict[str, Any]) -> dict[str, Any]:
        ui_enabled = bool(request_context.get("ui_enabled"))
        legacy_payload = self.load_resources_list_result(_params, ui_enabled=ui_enabled)
        return {
            "resultType": "complete",
            "resources": list(((legacy_payload.get("result") or {}).get("resources") or [])),
            "ttlMs": 60_000,
            "cacheScope": "private",
        }

    def _load_resources_read_result_mcp(self, params: dict[str, Any], *, request_context: dict[str, Any]) -> dict[str, Any]:
        ui_enabled = bool(request_context.get("ui_enabled"))
        legacy_payload = self.load_resources_read_result(params, ui_enabled=ui_enabled)
        if "error" in legacy_payload:
            return {
                "resultType": "complete",
                "contents": [],
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": str(legacy_payload.get("error") or "resource_error"),
                    }
                ],
            }

        result_payload = dict(legacy_payload.get("result") or {})
        result_payload.setdefault("resultType", "complete")
        result_payload.setdefault("ttlMs", 60_000)
        result_payload.setdefault("cacheScope", "private")
        return result_payload

    def _load_tools_list_result_mcp(self, _params: dict[str, Any], *, request_context: dict[str, Any]) -> dict[str, Any]:
        return {
            "resultType": "complete",
            # UI metadata is safe to advertise because each tool keeps a
            # text/structured fallback for hosts without MCP Apps support.
            "tools": self._load_mcp_tool_definitions(ui_enabled=True),
            "ttlMs": 60_000,
            "cacheScope": "private",
        }

    def _load_tools_call_result_mcp(self, params: dict[str, Any]) -> dict[str, Any]:
        legacy_payload = self.load_tools_call_result(params)
        if "error" in legacy_payload:
            return {
                "resultType": "complete",
                "content": [
                    {
                        "type": "text",
                        "text": str(legacy_payload.get("error") or "tool_error"),
                    }
                ],
                "isError": True,
            }

        raw_content = ((legacy_payload.get("result") or {}).get("content") or "")
        structured_content: Any = None
        if isinstance(raw_content, str):
            try:
                structured_content = json.loads(raw_content)
            except Exception:
                structured_content = None
        else:
            structured_content = raw_content

        if structured_content is not None:
            text_payload = json.dumps(structured_content, ensure_ascii=False)
        else:
            text_payload = str(raw_content)

        is_error = bool(isinstance(structured_content, dict) and structured_content.get("ok") is False)
        result_payload: dict[str, Any] = {
            "resultType": "complete",
            "content": [
                {
                    "type": "text",
                    "text": text_payload,
                }
            ],
            "isError": is_error,
        }
        if structured_content is not None:
            result_payload["structuredContent"] = structured_content
        return result_payload

    def _load_prompts_list_result_mcp(self, _params: dict[str, Any]) -> dict[str, Any]:
        legacy_payload = self.load_prompts_list_result(_params)
        return {
            "resultType": "complete",
            "prompts": list(((legacy_payload.get("result") or {}).get("prompts") or [])),
        }

    def _load_prompts_get_result_mcp(self, params: dict[str, Any]) -> dict[str, Any]:
        legacy_payload = self.load_prompts_get_result(params)
        if "error" in legacy_payload:
            return {
                "resultType": "complete",
                "messages": [
                    {
                        "role": "system",
                        "content": {
                            "type": "text",
                            "text": str(legacy_payload.get("error") or "prompt_error"),
                        },
                    }
                ],
                "isError": True,
            }
        result_payload = dict(legacy_payload.get("result") or {})
        result_payload.setdefault("resultType", "complete")
        return result_payload

    def load_initialize_result(self, _params: dict[str, Any], *, ui_enabled: bool = False) -> dict[str, Any]:
        requested_protocol_version = str(_params.get("protocolVersion") or "").strip()
        protocol_version = (
            requested_protocol_version
            if requested_protocol_version in self._SUPPORTED_PROTOCOL_VERSIONS
            else self._DEFAULT_PROTOCOL_VERSION
        )
        return {
            "result": {
                "protocolVersion": protocol_version,
                "serverInfo": {"name": "webplayer-mcp"},
                "capabilities": self._load_server_capabilities_payload(ui_enabled=ui_enabled),
            }
        }

    def load_server_discover_result(self, _params: dict[str, Any], *, ui_enabled: bool = False) -> dict[str, Any]:
        return {
            "result": {
                "resultType": "complete",
                "supportedVersions": list(self._SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": self._load_server_capabilities_payload(ui_enabled=ui_enabled),
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "webplayer-mcp",
                        "version": "1.0.0",
                    }
                },
                "ttlMs": 3_600_000,
                "cacheScope": "public",
            }
        }

    def _load_ui_resource_descriptor(self) -> dict[str, Any]:
        return {
            "uri": self._UI_RESOURCE_URI,
            "name": self._UI_APP_NAME,
            "title": "TIDAL MCP App UI",
            "description": "Compact MCP App UI for TIDAL WebPlayer playback controls and now playing metadata.",
            "mimeType": self._UI_RESOURCE_MIME_TYPE,
        }

    def _load_ui_resource_markup(self) -> str:
        ui_default_arguments = {
            "playback_backend": "browser",
            "player_selector": "chromium",
        }
        ui_default_arguments_json = json.dumps(ui_default_arguments, ensure_ascii=True, separators=(",", ":"))
        ui_play_arguments = dict(ui_default_arguments)
        ui_play_arguments_json = json.dumps(ui_play_arguments, ensure_ascii=True, separators=(",", ":"))
        ui_cover_path_json = json.dumps(self._UI_COVER_HTTP_PATH, ensure_ascii=True)
        ui_preview_path_json = json.dumps(self._UI_PREVIEW_HTTP_PATH, ensure_ascii=True)
        ui_scope_path_json = json.dumps(self._UI_SCOPE_HTTP_PATH, ensure_ascii=True)

        return """<!doctype html>
<html lang=\"en\">
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>TIDAL MCP App UI</title>
        <style>
            :root {
                color-scheme: dark;
                --wp-surface: #0b0d10;
                --wp-surface-2: #1a1d21;
                --wp-border: #2d333b;
                --wp-border-strong: #464e58;
                --wp-text: #f4f6f8;
                --wp-muted: #b1b8c1;
                --wp-accent: #1ce2d8;
                --wp-accent-soft: rgba(28, 226, 216, 0.16);
                --wp-positive: #36d399;
                --wp-warning: #f5b94b;
                --wp-danger: #ff7b7b;
                --wp-neutral: #9ca3af;
                --wp-value-gap: 6px;
            }
            html,
            body {
                margin: 0;
                padding: 0;
            }
            body {
                font-family: var(--fontFamilyBase, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif);
                margin: 4px;
                background: transparent;
                color: var(--wp-text);
                min-width: 0;
                max-width: 100%;
                overflow: hidden;
            }
            .player-layout {
                display: grid;
                container-name: player-layout;
                container-type: inline-size;
                grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
                grid-template-areas: "player aux-matrix";
                align-items: start;
                gap: 8px;
                width: 100%;
                min-width: 0;
            }
            .panel {
                grid-area: player;
                border: none;
                border-radius: 0;
                padding: calc(8px + var(--mcp-safe-area-top, 0px))
                    calc(8px + var(--mcp-safe-area-right, 0px))
                    calc(8px + var(--mcp-safe-area-bottom, 0px))
                    calc(8px + var(--mcp-safe-area-left, 0px));
                background: transparent;
                box-sizing: border-box;
                display: grid;
                grid-template-columns: 85px minmax(0, 1fr) auto;
                grid-template-rows: auto auto auto;
                grid-template-areas:
                    "cover track controls"
                    "cover audio-pipe brand"
                    "details details details";
                column-gap: 8px;
                row-gap: 4px;
                width: 100%;
                min-width: 0;
                max-width: 100%;
                overflow: hidden;
                box-shadow: none;
                position: relative;
            }
            .brand-row {
                display: contents;
            }
            .brand {
                display: contents;
            }
            .header-track-info {
                display: flex;
                flex-direction: column;
                gap: 2px;
                min-width: 0;
                max-width: 100%;
                grid-area: track;
                align-self: start;
            }
            .header-track-title,
            .header-track-album,
            .header-track-artist {
                min-width: 0;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .header-track-title-row {
                display: flex;
                align-items: center;
                gap: 4px;
                min-width: 0;
            }
            .header-track-title {
                color: var(--wp-text);
                font-size: 13px;
                font-weight: 800;
                line-height: 1.2;
            }
            .track-status-row .like-button {
                width: 18px;
                height: 18px;
                min-width: 18px;
                min-height: 18px;
                padding: 0;
                margin: 0;
                color: var(--wp-muted);
            }
            .track-status-row .like-button svg {
                width: 14px;
                height: 14px;
            }
            .track-status-row {
                display: grid;
                grid-template-columns: 18px minmax(0, 1fr);
                grid-template-areas:
                    "like values"
                    "meta meta";
                column-gap: var(--wp-value-gap);
                row-gap: 2px;
                align-items: center;
                min-width: 0;
            }
            .track-status-row .like-button {
                grid-area: like;
            }
            .track-status-values {
                grid-area: values;
                display: flex;
                flex-direction: row;
                align-items: center;
                gap: var(--wp-value-gap);
                min-width: 0;
            }
            .track-status-row .track-meta {
                grid-area: meta;
            }
            .header-track-artist {
                color: var(--wp-muted);
                font-size: 10px;
                font-weight: 600;
                line-height: 1.2;
            }
            .header-track-album {
                color: var(--wp-muted);
                font-size: 10px;
                line-height: 1.2;
            }
            .brand-meta {
                display: inline-flex;
                align-items: center;
                min-width: 0;
                grid-area: brand;
                align-self: center;
                font-size: 10px;
                text-transform: uppercase;
                letter-spacing: 0;
                color: var(--wp-muted);
            }
            .audio-pipe-meta {
                display: inline-flex;
                align-items: center;
                justify-self: stretch;
                align-self: start;
                gap: 6px;
                min-width: 0;
                max-width: 100%;
                padding-top: 4px;
                border-top: 1px solid var(--wp-border);
                grid-area: audio-pipe;
                color: var(--wp-muted);
                font-size: 10px;
                line-height: 1.2;
                text-align: left;
                white-space: nowrap;
                overflow: hidden;
            }
            .audio-pipe-label {
                flex: 0 0 auto;
                color: var(--wp-text);
                font-weight: 700;
            }
            .track-countdown {
                display: inline-flex;
                align-items: center;
                min-width: 34px;
                min-height: 20px;
                padding: 0 6px;
                color: var(--wp-text);
                font-size: 10px;
                font-variant-numeric: tabular-nums;
                line-height: 1;
                white-space: nowrap;
            }
            .progress-row {
                display: grid;
                grid-template-columns: auto minmax(0, 1fr) auto;
                align-items: center;
                gap: 6px;
                width: 100%;
                min-width: 0;
            }
            .progress-time {
                min-width: 34px;
                color: var(--wp-muted);
                font-size: 9px;
                font-variant-numeric: tabular-nums;
                text-align: center;
            }
            .track-progress {
                appearance: none;
                width: 100%;
                height: 16px;
                margin: 0;
                --wp-progress: 0%;
                cursor: pointer;
                background: transparent;
            }
            .track-progress::-webkit-slider-runnable-track {
                height: 4px;
                border-radius: 999px;
                background: linear-gradient(
                    to right,
                    var(--wp-text) 0 var(--wp-progress),
                    rgba(255, 255, 255, 0.2) var(--wp-progress) 100%
                );
            }
            .track-progress::-webkit-slider-thumb {
                appearance: none;
                width: 10px;
                height: 10px;
                margin-top: -3px;
                border: 2px solid var(--wp-surface);
                border-radius: 50%;
                background: var(--wp-text);
                box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.18);
            }
            .track-progress::-moz-range-track {
                height: 4px;
                border-radius: 999px;
                background: rgba(255, 255, 255, 0.2);
            }
            .track-progress::-moz-range-progress {
                height: 4px;
                border-radius: 999px;
                background: var(--wp-text);
            }
            .track-progress::-moz-range-thumb {
                width: 6px;
                height: 6px;
                border: 2px solid var(--wp-surface);
                border-radius: 50%;
                background: var(--wp-text);
                box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.18);
            }
            .track-progress:focus-visible {
                outline: none;
            }
            .track-progress:focus-visible::-webkit-slider-thumb {
                box-shadow: 0 0 0 3px var(--wp-neutral);
            }
            .track-progress:focus-visible::-moz-range-thumb {
                box-shadow: 0 0 0 3px var(--wp-neutral);
            }
            .track-progress:disabled {
                cursor: default;
                opacity: 0.45;
            }
            .track-meta {
                display: inline-flex;
                align-items: center;
                gap: var(--wp-value-gap);
                min-width: 0;
                color: var(--wp-muted);
                font-size: 10px;
                white-space: nowrap;
            }
            .quality-side {
                align-self: center;
                justify-self: start;
            }
            .header-track-info > .quality-side {
                align-self: flex-start;
            }
            .quality-text {
                color: var(--wp-muted);
                font-size: 10px;
                line-height: 1;
                white-space: nowrap;
            }
            .quality-text[data-quality-level="hi-res"] {
                color: #f59e0b;
            }
            .quality-text[data-quality-level="lossless"] {
                color: #22c55e;
            }
            .quality-text[data-quality-level="low"] {
                color: #000000;
            }
            .volume-text {
                color: var(--wp-muted);
                font-size: 10px;
                line-height: 1;
                font-variant-numeric: tabular-nums;
                white-space: nowrap;
            }
            .audio-output-text {
                color: var(--wp-muted);
                font-size: 10px;
                line-height: 1;
                white-space: nowrap;
                max-width: 180px;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .brand-dot {
                width: 8px;
                height: 8px;
                border-radius: 999px;
                background: var(--wp-accent);
                box-shadow: 0 0 0 3px var(--wp-accent-soft);
            }
            .content {
                display: contents;
            }
            .cover-shell {
                width: 85px;
                aspect-ratio: 1 / 1;
                padding: 3px;
                border-radius: 10px;
                overflow: hidden;
                border: none;
                background: transparent;
                box-sizing: border-box;
                grid-area: cover;
                align-self: start;
            }
            #cover {
                width: 100%;
                height: 100%;
                border-radius: inherit;
                border: none;
                object-fit: cover;
                display: none;
                background: transparent;
            }
            .cover-shell {
                cursor: zoom-in;
            }
            .panel.cover-expanded {
                grid-template-columns: minmax(180px, 320px) minmax(0, 1fr) auto;
            }
            .panel.cover-expanded .cover-shell {
                width: 100%;
                max-width: 320px;
            }
            @container player-layout (max-width: 600px) {
                .panel,
                .panel.cover-expanded {
                    grid-template-columns: minmax(0, 1fr);
                    grid-template-rows: auto;
                    grid-template-areas:
                        "cover"
                        "track"
                        "audio-pipe"
                        "brand"
                        "details";
                }
                .panel .cover-shell {
                    width: clamp(48px, 16cqi, 85px);
                }
                .panel .playback-control-stack {
                    position: static;
                    transform: none;
                    width: 100%;
                    max-width: 100%;
                    box-sizing: border-box;
                    align-items: stretch;
                }
                .panel .controls {
                    width: 100%;
                    max-width: 100%;
                    height: auto;
                    box-sizing: border-box;
                }
                .panel .controls [data-icon="volume-down"],
                .panel .controls [data-icon="volume-up"],
                .panel .controls .volume-text {
                    position: static;
                    transform: none;
                }
                .panel .controls button,
                .panel .controls fluent-button {
                    width: clamp(26px, 6cqi, 32px);
                    height: clamp(26px, 6cqi, 32px);
                    min-width: clamp(26px, 6cqi, 32px);
                    min-height: clamp(26px, 6cqi, 32px);
                    padding: 3px;
                }
                .panel .controls button svg,
                .panel .controls fluent-button svg {
                    width: clamp(14px, 3.5cqi, 18px);
                    height: clamp(14px, 3.5cqi, 18px);
                }
            }
            .cover-preview {
                position: fixed;
                inset: 0;
                z-index: 20;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 24px;
                background: rgba(0, 0, 0, 0.86);
                box-sizing: border-box;
                cursor: zoom-out;
            }
            .cover-preview[hidden] {
                display: none;
            }
            .cover-preview img {
                width: min(88vw, 640px);
                height: min(88vh, 640px);
                object-fit: contain;
                border-radius: 8px;
                box-shadow: 0 18px 60px rgba(0, 0, 0, 0.55);
            }
            .cover-preview-close {
                position: absolute;
                top: 12px;
                right: 12px;
                width: 32px;
                height: 32px;
                border: 0;
                padding: 0;
                color: var(--wp-text);
                background: rgba(0, 0, 0, 0.45);
                border-radius: 50%;
                font-size: 22px;
                line-height: 1;
                cursor: pointer;
            }
            #info-box {
                border: none;
                border-radius: 10px;
                background: transparent;
                padding: 0;
                font-size: 11px;
                line-height: 1.35;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                gap: 5px;
                min-width: 0;
                max-width: 100%;
                grid-area: details;
            }
            .now-playing {
                grid-area: details;
                min-width: 0;
                max-width: 100%;
                align-self: start;
            }
            .now-playing-eyebrow {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                font-size: 10px;
                text-transform: uppercase;
                letter-spacing: 0;
                color: var(--wp-muted);
            }
            .now-playing-title {
                font-size: 13px;
                line-height: 1.2;
                font-weight: 700;
                color: var(--wp-text);
                word-break: break-word;
            }
            .now-playing-subtitle {
                color: var(--wp-muted);
                word-break: break-word;
            }
            .embedded-link {
                color: inherit;
                text-decoration: none;
            }
            .embedded-link:hover,
            .embedded-link:focus-visible {
                color: var(--wp-accent);
                text-decoration: underline;
                outline: none;
            }
            .now-playing-link {
                align-self: flex-start;
                color: var(--wp-accent);
                font-size: 10px;
                font-weight: 700;
                text-decoration: none;
            }
            .now-playing-link:hover,
            .now-playing-link:focus-visible {
                text-decoration: underline;
                outline: none;
            }
            .meta-chips {
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
                min-width: 0;
            }
            .meta-chip {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                min-height: 18px;
                padding: 2px 6px;
                border-radius: 999px;
                border: 1px solid var(--wp-border);
                background: rgba(255, 255, 255, 0.03);
                font-size: 10px;
                line-height: 1;
                white-space: nowrap;
                color: var(--wp-text);
            }
            .meta-chip strong {
                font-weight: 600;
                color: var(--wp-muted);
            }
            .meta-chip[data-variant="accent"] {
                border-color: rgba(28, 226, 216, 0.28);
                background: rgba(28, 226, 216, 0.12);
            }
            .meta-chip[data-variant="good"] {
                border-color: rgba(54, 211, 153, 0.28);
                background: rgba(54, 211, 153, 0.12);
            }
            .meta-chip[data-variant="warn"] {
                border-color: rgba(245, 185, 75, 0.28);
                background: rgba(245, 185, 75, 0.12);
            }
            .meta-chip[data-variant="bad"] {
                border-color: rgba(255, 123, 123, 0.28);
                background: rgba(255, 123, 123, 0.12);
            }
            .meta-inline {
                display: inline-flex;
                align-items: baseline;
                gap: 5px;
                min-height: 18px;
                font-size: 10px;
                line-height: 1;
                white-space: nowrap;
                color: var(--wp-text);
            }
            .meta-inline strong {
                font-weight: 600;
                color: var(--wp-muted);
            }
            .meta-inline[data-variant="warn"] {
                color: var(--wp-warning);
                font-weight: 700;
            }
            .meta-inline[data-variant="warn"] strong {
                color: inherit;
            }
            .meta-inline[data-variant="accent"] {
                color: var(--wp-accent);
                font-weight: 700;
            }
            .meta-inline[data-variant="accent"] strong {
                color: inherit;
            }
            .info-summary {
                display: grid;
                grid-template-columns: minmax(0, 1fr) auto;
                align-items: center;
                gap: 6px;
                min-width: 0;
            }
            .info-summary .meta-chips {
                min-width: 0;
            }
            .quality-chip {
                justify-self: end;
                min-width: max-content;
            }
            .brand-meta .like-button {
                width: 18px;
                height: 18px;
                min-width: 18px;
                min-height: 18px;
                padding: 0;
                margin: 0;
                color: var(--wp-muted);
            }
            .brand-meta .like-button svg {
                width: 14px;
                height: 14px;
            }
            .details-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
                gap: 4px 8px;
                min-width: 0;
                font-size: 10px;
                color: var(--wp-muted);
            }
            .details-grid > div {
                min-width: 0;
                max-width: 100%;
                overflow-wrap: anywhere;
            }
            .aux-matrix {
                grid-area: aux-matrix;
                position: relative;
                display: flex;
                flex-direction: column;
                gap: 4px;
                align-self: start;
                min-width: 0;
                max-width: 100%;
                box-sizing: border-box;
                padding: 8px 0 8px 8px;
                border-left: 1px solid var(--wp-border);
                color: var(--wp-muted);
                font-size: 9px;
            }
            .aux-matrix-scroll {
                width: 100%;
                min-width: 0;
                overflow: hidden;
                scrollbar-width: none;
            }
            .aux-matrix-scroll::-webkit-scrollbar,
            .scope-settings-panel::-webkit-scrollbar {
                display: none;
            }
            .aux-matrix [hidden] {
                display: none !important;
            }
            .scope-settings {
                position: absolute;
                z-index: 4;
                top: auto;
                bottom: 4px;
                right: 4px;
                display: flex;
                flex-direction: column-reverse;
                width: min(320px, calc(100vw - 32px));
            }
            .scope-settings-toggle {
                display: grid;
                width: 24px;
                height: 24px;
                margin-left: auto;
                padding: 0;
                place-items: center;
                list-style: none;
                border: 0;
                border-radius: 50%;
                background: transparent;
                color: var(--wp-muted);
                cursor: pointer;
            }
            .scope-settings-toggle::-webkit-details-marker {
                display: none;
            }
            .scope-settings-toggle::marker {
                content: "";
            }
            .scope-settings-toggle:hover,
            .scope-settings-toggle:focus-visible,
            .scope-settings[open] .scope-settings-toggle {
                color: var(--wp-text);
            }
            .scope-settings-toggle:focus-visible {
                outline: 2px solid var(--wp-accent);
                outline-offset: 2px;
            }
            .scope-settings-toggle svg {
                width: 14px;
                height: 14px;
                fill: currentColor;
            }
            .scope-settings-panel {
                width: 100%;
                max-height: min(65vh, 420px);
                margin: 0 0 4px;
                padding: 8px;
                overflow: auto;
                scrollbar-width: none;
                border: 1px solid var(--wp-border-strong);
                border-radius: 6px;
                background: var(--wp-surface);
                box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32);
            }
            .aux-matrix-content {
                min-width: 0;
                width: 100%;
            }
            .aux-response-chart {
                display: block;
                width: 100%;
                height: auto;
                overflow: visible;
                color: var(--wp-muted);
                font-size: 10px;
            }
            .aux-response-grid {
                stroke: var(--wp-border);
                stroke-width: 1;
            }
            .aux-response-axis {
                stroke: var(--wp-border-strong);
                stroke-width: 1.2;
            }
            .aux-scope-wave {
                fill: none;
                stroke-width: 1.4;
                stroke-linecap: round;
                stroke-linejoin: round;
                vector-effect: non-scaling-stroke;
            }
            .aux-response-chart text {
                fill: var(--wp-muted);
                font-family: inherit;
                font-size: var(--aux-scope-font-size, 10px);
            }
            .aux-response-chart .aux-response-axis-label {
                fill: var(--wp-text);
                font-weight: 700;
                letter-spacing: 0.04em;
            }
            .aux-response-chart .aux-scope-axis-tick {
                font-size: var(--aux-scope-font-size, 10px);
                font-variant-numeric: tabular-nums;
            }
            .aux-response-chart .aux-scope-lane-level {
                font-weight: 700;
                font-variant-numeric: tabular-nums;
                font-size: calc(var(--aux-scope-font-size, 10px) - 1px);
            }
            .aux-scope-center {
                stroke: var(--wp-border-strong);
                stroke-width: 1;
                stroke-dasharray: 2 2;
            }
            .aux-scope-pair-separator {
                stroke: var(--wp-accent);
                stroke-width: 1.8;
            }
            .aux-scope-frame {
                fill: none;
                stroke: var(--wp-border-strong);
                stroke-width: 1.2;
            }
            .scope-controls {
                display: flex;
                flex-direction: column;
                gap: 4px;
                min-width: 0;
                padding-right: 8px;
                color: var(--wp-text);
                font-size: 10px;
            }
            .scope-control-row {
                display: flex;
                align-items: center;
                flex-wrap: wrap;
                gap: 4px;
                min-width: 0;
            }
            .scope-control-label {
                flex: 0 0 62px;
                color: var(--wp-muted);
                font-weight: 700;
                letter-spacing: 0.04em;
            }
            .scope-step,
            .scope-unit button {
                min-width: 24px;
                height: 20px;
                padding: 0 6px;
                border: 1px solid var(--wp-border-strong);
                border-radius: 4px;
                background: transparent;
                color: var(--wp-text);
                font: inherit;
                font-weight: 700;
                cursor: pointer;
            }
            .scope-unit {
                display: inline-flex;
            }
            .scope-unit button + button {
                margin-left: -1px;
            }
            .scope-unit button[aria-pressed="true"] {
                background: var(--wp-accent-soft);
                border-color: var(--wp-accent);
            }
            .scope-timebase {
                flex: 1 1 80px;
                min-width: 64px;
                margin: 0;
                accent-color: var(--wp-accent);
            }
            .scope-value {
                min-width: 58px;
                text-align: center;
                font-weight: 700;
                font-variant-numeric: tabular-nums;
            }
            .scope-sources {
                display: grid;
                grid-template-columns: minmax(0, 1fr);
                gap: 8px;
            }
            .scope-source-pair {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 4px 8px;
                min-width: 0;
            }
            .scope-source-pair-label {
                grid-column: 1 / -1;
                padding-bottom: 2px;
                border-bottom: 1px solid var(--wp-border);
                color: var(--wp-muted);
                font-weight: 700;
                letter-spacing: 0.04em;
            }
            .scope-source {
                display: flex;
                align-items: center;
                gap: 4px;
                min-width: 0;
            }
            .scope-source span {
                color: var(--scope-color);
                font-weight: 700;
            }
            .scope-source select {
                flex: 1 1 auto;
                min-width: 0;
                height: 20px;
                border: 1px solid var(--wp-border-strong);
                border-radius: 4px;
                background: var(--wp-surface);
                color: var(--wp-text);
                font: inherit;
            }
            .scope-controls {
                container-type: inline-size;
            }
            @container (max-width: 260px) {
                .scope-control-label {
                    display: none;
                }
                .scope-value {
                    min-width: 48px;
                }
            }
            .artist-details {
                display: none;
                margin-top: 4px;
            }
            .panel.cover-expanded .artist-details {
                display: grid;
            }
            .detail-label {
                display: block;
                margin-bottom: 1px;
                text-transform: uppercase;
                font-size: 9px;
                letter-spacing: 0;
                color: var(--wp-muted);
            }
            .detail-value {
                display: block;
                color: var(--wp-text);
                word-break: break-word;
            }
            .playback-control-stack {
                position: absolute;
                left: 50%;
                top: calc(var(--mcp-safe-area-top, 0px) + 34.5px);
                transform: translateX(-50%);
                z-index: 2;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 2px;
                width: min(260px, calc(100% - 16px));
                min-width: 0;
            }
            .controls {
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
                align-items: center;
                justify-content: center;
                position: relative;
                flex: 0 1 auto;
                width: max-content;
                min-width: 0;
                margin-top: 0;
                height: 32px;
            }
            .controls [data-icon="volume-down"] {
                position: absolute;
                right: calc(100% + 4px);
                top: 50%;
                transform: translateY(-50%);
            }
            .controls [data-icon="volume-up"] {
                position: absolute;
                left: calc(100% + 4px);
                top: 50%;
                transform: translateY(-50%);
            }
            .controls .volume-text {
                position: absolute;
                left: calc(100% + 40px);
                top: 50%;
                transform: translateY(-50%);
                width: 85px;
                text-align: right;
                pointer-events: none;
            }
            .controls button,
            .controls fluent-button {
                box-sizing: border-box;
                width: 32px;
                height: 32px;
                min-width: 32px;
                min-height: 32px;
                padding: 4px 6px;
                font-size: 12px;
                line-height: 1;
                font-weight: 700;
                color: var(--wp-text);
                margin: 0;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 6px;
                border: none !important;
                background: transparent;
                box-shadow: none !important;
                cursor: pointer;
            }
            .controls button:hover,
            .controls button:focus-visible,
            .controls fluent-button:hover,
            .controls fluent-button:focus-visible {
                background: transparent;
                outline: none;
                border: none !important;
                box-shadow: none !important;
            }
            .controls fluent-button[data-liked="true"] {
                color: var(--wp-danger);
            }
            .controls button svg,
            .controls fluent-button svg {
                display: block;
                width: 18px;
                height: 18px;
                pointer-events: none;
            }
            .header-actions {
                display: inline-flex;
                align-items: center;
                gap: 6px;
                min-width: 0;
            }
            .header-actions button {
                width: 28px;
                height: 28px;
                padding: 4px;
                border: 1px solid var(--wp-border);
                border-radius: 7px;
                background: rgba(255, 255, 255, 0.03);
                color: var(--wp-text);
                display: inline-flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
            }
            .header-actions button:hover,
            .header-actions button:focus-visible {
                border-color: var(--wp-accent);
                outline: none;
            }
            .header-actions button svg {
                display: block;
                width: 16px;
                height: 16px;
                pointer-events: none;
            }
            .backend-row {
                display: flex;
                align-items: center;
                gap: 8px;
                width: 100%;
                min-width: 0;
                margin-top: 8px;
            }
            .backend-row label {
                flex: 0 0 auto;
                color: var(--wp-muted);
                font-size: 10px;
                font-weight: 700;
            }
            .backend-row select {
                flex: 1 1 auto;
                min-width: 0;
                height: 30px;
                box-sizing: border-box;
                border: 1px solid var(--wp-border-strong);
                border-radius: 8px;
                padding: 0 8px;
                background: var(--wp-surface-2);
                color: var(--wp-text);
                font: inherit;
                font-size: 10px;
                outline: none;
            }
            .backend-row select:focus {
                border-color: var(--wp-accent);
                box-shadow: 0 0 0 2px var(--wp-accent-soft);
            }
            .auth-box {
                display: flex;
                flex-direction: column;
                gap: 7px;
                margin-top: 6px;
                padding: 7px;
                border: 1px solid var(--wp-border);
                border-radius: 8px;
                background: rgba(255, 255, 255, 0.03);
                color: var(--wp-muted);
                font-size: 11px;
                order: 4;
            }
            .auth-box[hidden] {
                display: none;
            }
            .auth-box a {
                color: var(--wp-accent);
                font-weight: 700;
            }
            .auth-box input {
                min-width: 0;
                height: 30px;
                box-sizing: border-box;
                border: 1px solid var(--wp-border-strong);
                border-radius: 6px;
                padding: 0 8px;
                background: rgba(255, 255, 255, 0.05);
                color: var(--wp-text);
                font: inherit;
                font-size: 10px;
            }
            .auth-box button {
                align-self: flex-start;
                min-height: 28px;
                padding: 4px 9px;
                border: 1px solid var(--wp-border);
                border-radius: 6px;
                background: var(--wp-accent-soft);
                color: var(--wp-text);
                cursor: pointer;
                font: inherit;
                font-size: 10px;
                font-weight: 700;
            }
            .auth-box .auth-error {
                color: var(--wp-danger);
            }
            .auth-recover {
                align-self: flex-start;
                min-height: 28px;
                padding: 4px 9px;
                border: 1px solid var(--wp-accent);
                border-radius: 6px;
                background: var(--wp-accent-soft);
                color: var(--wp-text);
                cursor: pointer;
                font: inherit;
                font-size: 10px;
                font-weight: 700;
            }
            .auth-recover:hover,
            .auth-recover:focus-visible {
                background: rgba(28, 226, 216, 0.24);
                outline: none;
            }
            .spinner {
                width: 12px;
                height: 12px;
                border: 2px solid rgba(156, 163, 175, 0.3);
                border-top-color: var(--wp-accent);
                border-radius: 50%;
                animation: webplayer-spin 0.8s linear infinite;
                flex: 0 0 auto;
            }
            @keyframes webplayer-spin {
                to { transform: rotate(360deg); }
            }
            .skip-vol-gap {
                display: inline-block;
                width: 4px;
            }
            .status-row {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                min-width: 0;
            }
            .status-message {
                color: var(--wp-muted);
                font-size: 11px;
            }
            .error-message {
                color: var(--wp-danger);
                font-size: 11px;
            }
            .retry-button {
                margin-left: 2px;
            }
            body.embed-mode {
                margin: 0;
                background: transparent;
            }
            body.embed-mode .panel {
                border-radius: 8px;
                box-shadow: none;
                width: 100%;
            }
            @media (prefers-reduced-motion: reduce) {
                *, *::before, *::after {
                    scroll-behavior: auto !important;
                    animation-duration: 0.01ms !important;
                    animation-iteration-count: 1 !important;
                    transition-duration: 0.01ms !important;
                }
            }
        </style>
    </head>
    <body>
        <div class=\"player-layout\">
        <section class=\"panel\">
            <div class=\"brand-row\">
                <div class=\"brand\">
                    <div class=\"header-track-info\">
                        <div class=\"header-track-title-row\">
                            <span id=\"header-track-title\" class=\"header-track-title\">--</span>
                        </div>
                        <span id=\"header-track-album\" class=\"header-track-album\">--</span>
                        <span id=\"header-track-artist\" class=\"header-track-artist\">--</span>
                        <div class=\"track-status-row\">
                            <fluent-button class=\"like-button\" appearance=\"subtle\" type=\"button\" data-like-button=\"true\" data-tool=\"tidal_api_add_track_to_collection\" aria-label=\"add current track to tracks\" title=\"[API] Add current track to tracks\"><svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\" width=\"20px\" height=\"20px\" fill=\"currentColor\"><path d=\"M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z\"/></svg></fluent-button>
                            <div class=\"track-status-values\">
                                <span id=\"track-countdown\" class=\"track-countdown\" aria-label=\"Remaining track time\">--</span>
                            </div>
                            <div id=\"track-meta\" class=\"track-meta quality-side\">
                                <span id=\"header-quality\" class=\"quality-text\" hidden></span>
                            </div>
                        </div>
                <div class=\"playback-control-stack\">
                <div class=\"controls\" role=\"toolbar\" aria-label=\"TIDAL WebPlayer controls\">
            <fluent-button appearance=\"subtle\" type=\"button\" data-tool=\"webplayer_backward\" data-icon=\"skip-previous\" aria-label=\"previous\" title=\"Previous track\"><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"20px\" viewBox=\"0 -960 960 960\" width=\"20px\" fill=\"currentColor\"><path d=\"M240-240v-480h72v480h-72Zm480-24L384-480l336-216v432Zm-72-216Zm0 84v-168l-131 84 131 84Z\"/></svg></fluent-button>
            <fluent-button appearance=\"subtle\" type=\"button\" data-tool=\"webplayer_play\" data-toggle=\"play-pause\" data-play-tool=\"webplayer_play\" data-stop-tool=\"webplayer_pause\" data-play-args='__WEBPLAYER_UI_PLAY_ARGUMENTS__' data-icon=\"play-stop\" aria-label=\"play\" title=\"Start playback\"><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"20px\" viewBox=\"0 -960 960 960\" width=\"20px\" fill=\"currentColor\"><path d=\"m384-312 264-168-264-168v336Zm96.28 216Q401-96 331-126t-122.5-82.5Q156-261 126-330.96t-30-149.5Q96-560 126-629.5q30-69.5 82.5-122T330.96-834q69.96-30 149.5-30t149.04 30q69.5 30 122 82.5T834-629.28q30 69.73 30 149Q864-401 834-331t-82.5 122.5Q699-156 629.28-126q-69.73 30-149 30Zm-.28-72q130 0 221-91t91-221q0-130-91-221t-221-91q-130 0-221 91t-91 221q0 130 91 221t221 91Zm0-312Z\"/></svg></fluent-button>
                <fluent-button appearance=\"subtle\" type=\"button\" data-tool=\"webplayer_forward\" data-icon=\"skip-next\" aria-label=\"next\" title=\"Next track\"><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"20px\" viewBox=\"0 -960 960 960\" width=\"20px\" fill=\"currentColor\"><path d=\"M648-240v-480h72v480h-72Zm-408-24v-432l336 216-336 216Zm72-216Zm0 84 131-84-131-84v168Z\"/></svg></fluent-button>
                <button type=\"button\" data-tool=\"webplayer_system_volume_adjust\" data-args='{"delta_percent":-5}' data-icon=\"volume-down\" aria-label=\"sink volume down\" title=\"Sink volume down\"><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"20px\" viewBox=\"0 -960 960 960\" width=\"20px\" fill=\"currentColor\"><path d=\"M240-384v-192h144l192-192v576L384-384H240Zm408 55v-302q46 19 71 61t25 90q0 48-25 89.5T648-329ZM504-594l-90 90H312v48h102l90 90v-228Zm-97 114Z\"/></svg></button>
                <button type=\"button\" data-tool=\"webplayer_system_volume_adjust\" data-args='{"delta_percent":5}' data-icon=\"volume-up\" aria-label=\"sink volume up\" title=\"Sink volume up\"><svg xmlns=\"http://www.w3.org/2000/svg\" height=\"20px\" viewBox=\"0 -960 960 960\" width=\"20px\" fill=\"currentColor\"><path d=\"M560-131v-82q90-26 145-103t55-164q0-87-55-164T560-747v-82q124 28 202 125.5T840-480q0 126-78 223.5T560-131ZM240-384v-192h144l192-192v576L384-384H240Zm264-210-90 90H312v48h102l90 90v-228Zm-97 114Z\"/></svg></button>
                <span id=\"header-volume\" class=\"volume-text\" hidden></span>
                </div>
                <div class=\"progress-row\" aria-label=\"Track progress\">
                    <span id=\"track-position\" class=\"progress-time\">--:--</span>
                    <input id=\"track-progress\" class=\"track-progress\" type=\"range\" min=\"0\" max=\"100\" step=\"0.1\" value=\"0\" disabled aria-label=\"Track progress\" />
                    <span id=\"track-duration\" class=\"progress-time\">--:--</span>
                </div>
                </div>
            </div>
            <div id=\"auth-box\" class=\"auth-box\" hidden aria-live=\"polite\"></div>
            <div class=\"content\">
                <div class=\"cover-shell\" role=\"button\" tabindex=\"0\" aria-expanded=\"false\" aria-label=\"Expand player and album art\" title=\"Expand player and album art\">
                    <img id=\"cover\" alt=\"Now playing cover art\" />
                </div>
                <div id=\"audio-pipe-info\" class=\"audio-pipe-meta\" hidden aria-label=\"Audio Pipe\">
                    <span class=\"audio-pipe-label\">Audio Pipe:</span>
                    <span id=\"header-audio-interface\" class=\"audio-output-text\" hidden></span>
                    <span id=\"header-dsp-separator\" class=\"meta-separator\" aria-hidden=\"true\" hidden>|</span>
                    <span id=\"header-dsp\" class=\"audio-output-text\" hidden></span>
                    <span id=\"header-audio-pipe-separator\" class=\"meta-separator\" aria-hidden=\"true\" hidden>|</span>
                </div>
                <div class=\"now-playing\">
                    <div id=\"info-box\" aria-live=\"polite\" role=\"status\">--</div>
                </div>
            </div>
            <div id=\"cover-preview\" class=\"cover-preview\" hidden role=\"dialog\" aria-modal=\"true\" aria-label=\"Album art preview\">
                <button id=\"cover-preview-close\" class=\"cover-preview-close\" type=\"button\" aria-label=\"Close album art preview\" title=\"Close\">&times;</button>
                <img id=\"cover-preview-image\" alt=\"Album art preview\" />
            </div>
        </section>
        <aside id=\"aux-matrix\" class=\"aux-matrix\" aria-label=\"Live playback oscilloscope\">
        </aside>
        </div>
        <script type=\"module\">
            const panelEl = document.querySelector(".panel");
            const controlsEl = document.querySelector(".controls");
            const infoBoxEl = document.getElementById("info-box");
            const auxMatrixEl = document.getElementById("aux-matrix");
            const coverEl = document.getElementById("cover");
            const coverPreviewEl = document.getElementById("cover-preview");
            const coverPreviewImageEl = document.getElementById("cover-preview-image");
            const coverPreviewCloseEl = document.getElementById("cover-preview-close");
            const coverShellEl = document.querySelector(".cover-shell");
            const trackCountdownEl = document.getElementById("track-countdown");
            const trackProgressEl = document.getElementById("track-progress");
            const trackPositionEl = document.getElementById("track-position");
            const trackDurationEl = document.getElementById("track-duration");
            const headerVolumeEl = document.getElementById("header-volume");
            const headerQualityEl = document.getElementById("header-quality");
            const trackMetaEl = document.getElementById("track-meta");
            const audioPipeEl = document.getElementById("audio-pipe-info");
            const headerAudioInterfaceEl = document.getElementById("header-audio-interface");
            const headerDspSeparatorEl = document.getElementById("header-dsp-separator");
            const headerDspEl = document.getElementById("header-dsp");
            const headerAudioPipeSeparatorEl = document.getElementById("header-audio-pipe-separator");
            const headerTrackTitleEl = document.getElementById("header-track-title");
            const headerTrackArtistEl = document.getElementById("header-track-artist");
            const headerTrackAlbumEl = document.getElementById("header-track-album");
            const authBoxEl = document.getElementById("auth-box");
            const playPauseButtonEl = document.querySelector('fluent-button[data-toggle="play-pause"]');
            const likeButtonEl = document.querySelector("fluent-button[data-like-button]");
            const closeCoverPreview = () => {
                if (coverPreviewEl) {
                    coverPreviewEl.hidden = true;
                }
            };
            const openCoverPreview = () => {
                if (!coverPreviewEl || !coverPreviewImageEl || !coverEl || !coverEl.src || coverEl.style.display === "none") {
                    return;
                }
                coverPreviewImageEl.src = coverEl.src;
                coverPreviewImageEl.alt = coverEl.alt || "Album art preview";
                coverPreviewEl.hidden = false;
                coverPreviewCloseEl?.focus();
            };
            let coverClickTimerId = null;
            const toggleCoverExpanded = () => {
                if (!coverEl || coverEl.style.display === "none") {
                    return;
                }
                const nextExpanded = !panelEl?.classList.contains("cover-expanded");
                panelEl?.classList.toggle("cover-expanded", nextExpanded);
                coverShellEl?.setAttribute("aria-expanded", nextExpanded ? "true" : "false");
                coverShellEl?.setAttribute(
                    "aria-label",
                    nextExpanded ? "Collapse player and album art" : "Expand player and album art"
                );
                coverShellEl?.setAttribute(
                    "title",
                    nextExpanded ? "Collapse player and album art" : "Expand player and album art"
                );
                postEmbedSize();
            };
            const scheduleCoverExpansion = () => {
                if (coverClickTimerId !== null) {
                    window.clearTimeout(coverClickTimerId);
                }
                coverClickTimerId = window.setTimeout(() => {
                    coverClickTimerId = null;
                    toggleCoverExpanded();
                }, 220);
            };
            coverShellEl?.addEventListener("click", scheduleCoverExpansion);
            coverShellEl?.addEventListener("dblclick", () => {
                if (coverClickTimerId !== null) {
                    window.clearTimeout(coverClickTimerId);
                    coverClickTimerId = null;
                }
                openCoverPreview();
            });
            coverShellEl?.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    toggleCoverExpanded();
                }
            });
            coverPreviewCloseEl?.addEventListener("click", closeCoverPreview);
            coverPreviewEl?.addEventListener("click", (event) => {
                if (event.target === coverPreviewEl) {
                    closeCoverPreview();
                }
            });
            document.addEventListener("keydown", (event) => {
                if (event.key === "Escape") {
                    closeCoverPreview();
                }
            });
            const controlButtons = Array.from(document.querySelectorAll('[data-tool]'));
            let inFlightUserAction = false;
            let inFlightRefresh = false;
            let callSequence = 0;
            let lastAppliedSequence = 0;
            let app = null;
            let appHostConnected = false;
            const vscodeHostApi = (() => {
                try {
                    return typeof acquireVsCodeApi === "function" ? acquireVsCodeApi() : null;
                } catch (_) {
                    return null;
                }
            })();
            const handleEmbeddedLinkClick = (event) => {
                const target = event.target instanceof Element
                    ? event.target.closest("a[data-open-link]")
                    : null;
                const href = target instanceof HTMLAnchorElement ? target.href : "";
                if (!target || !href) {
                    return;
                }
                if (appHostConnected && app && typeof app.openLink === "function") {
                    event.preventDefault();
                    try {
                        Promise.resolve(app.openLink({ url: href })).catch(() => {
                            window.open(href, "_blank", "noopener,noreferrer");
                        });
                    } catch (_) {
                        window.open(href, "_blank", "noopener,noreferrer");
                    }
                    return;
                }
                if (vscodeHostApi && typeof vscodeHostApi.postMessage === "function") {
                    event.preventDefault();
                    vscodeHostApi.postMessage({ type: "webplayer-open-link", url: href });
                }
            };
            document.addEventListener("click", handleEmbeddedLinkClick);
            let refreshIntervalId = null;
            let countdownIntervalId = null;
            const SCOPE_FRAME_INTERVAL_MS = 1000 / 30;
            const SCOPE_SLOT_COUNT = 2;
            const SCOPE_CHANNEL_PAIRS = [
                { label: "PRO AUDIO · IN_SOURCE_1 → OUT_SINK_1", slots: [0, 1] },
            ];
            const SCOPE_OUTPUT_DISPLAY_CHANNEL_START = 2;
            const SCOPE_HORIZONTAL_DIVISIONS = 10;
            const SCOPE_LANE_DIVISIONS = 4;
            const SCOPE_POINT_COUNT = 512;
            const SCOPE_TIMEBASE_STEPS_MS = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20];
            const SCOPE_VOLT_STEPS = [1, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001];
            const SCOPE_DB_STEPS = [30, 24, 18, 12, 9, 6, 3];
            const SCOPE_COLORS = ["#1ce2d8", "#f5b94b", "#8b9dff", "#36d399", "#f472b6", "#fb7185"];
            const SCOPE_WAVE_COLOR = "#168bff";
            const SCOPE_OVERLOAD_COLOR = "#e05a4a";
            const SCOPE_PLOT = { left: 64, right: 556, top: 16, bottom: 400 };
            const SCOPE_DEFAULT_SOURCES = [
                { id: "in1", label: "IN_SOURCE_1", kind: "input", channel: 0 },
                { id: "in2", label: "IN_SOURCE_2", kind: "input", channel: 1 },
                { id: "out1", label: "OUT_SINK_1", kind: "output", channel: 0 },
                { id: "out2", label: "OUT_SINK_2", kind: "output", channel: 1 },
                { id: "out3", label: "OUT_SINK_3", kind: "output", channel: 2 },
                { id: "out4", label: "OUT_SINK_4", kind: "output", channel: 3 },
            ];
            let scopeSettings = {
                channels: ["in1", "out1"],
                timebaseIndex: 5,
                unit: "dB",
                voltIndex: 1,
                dbIndex: 1,
            };
            let audioScopeSources = SCOPE_DEFAULT_SOURCES.slice();
            let audioScopeSourceLabels = {};
            let audioScopeDspMeters = { capture: [], playback: [] };
            let audioScopeSourcesSignature = "";
            let lastAudioScopeChartHtml = "";
            let scopePollTimeoutId = null;
            let scopeRequestPending = false;
            let scopeRenderFrameId = null;
            let scopePendingPayload = null;
            let scopePollingActive = false;
            let lastAudioScopePayload = null;
            let audioScopeStatusText = "";
            let webLightTheme = {};
            let webDarkTheme = {};
            const isEmbedded = (() => {
                try {
                    return window.self !== window.top;
                } catch (_) {
                    return true;
                }
            })();
            if (isEmbedded) {
                document.body.classList.add("embed-mode");
            }

            const vscodeApi = (() => {
                try {
                    if (typeof acquireVsCodeApi === "function") {
                        return acquireVsCodeApi();
                    }
                } catch (_) {}
                return null;
            })();

            if (isEmbedded) {
                try {
                    const appModule = await import("https://cdn.jsdelivr.net/npm/@modelcontextprotocol/ext-apps/+esm");
                    if (typeof appModule.App === "function") {
                        app = new appModule.App({ name: "Mpc.App.ui", version: "1.0.0" });
                    }
                } catch (_) {
                    app = null;
                }
                try {
                    const themeModule = await import("https://cdn.jsdelivr.net/npm/@fluentui/tokens/+esm");
                    webLightTheme = themeModule.webLightTheme || {};
                    webDarkTheme = themeModule.webDarkTheme || {};
                } catch (_) {
                    webLightTheme = {};
                    webDarkTheme = {};
                }
            }

            const postEmbedMessage = (payload) => {
                if (vscodeApi && typeof vscodeApi.postMessage === "function") {
                    try {
                        vscodeApi.postMessage(payload);
                    } catch (_) {}
                }
                if (!isEmbedded) {
                    return;
                }
                try {
                    window.parent.postMessage(payload, "*");
                } catch (_) {
                    // Best-effort messaging for iframe integrations.
                }
            };

            const loadMcpHttpEndpoint = () => {
                try {
                    const queryParams = new URLSearchParams(String(window.location.search || ""));
                    const endpointFromQuery = String(
                        queryParams.get("mcp_endpoint") || queryParams.get("mcpEndpoint") || ""
                    ).trim();
                    if (endpointFromQuery) {
                        return endpointFromQuery;
                    }
                } catch (_) {
                    // Ignore query parsing failures and continue with defaults.
                }

                if (String(window.location.protocol || "").toLowerCase() === "file:") {
                    return "http://192.168.0.48:8766/mcp";
                }
                return "/mcp";
            };

            let MCP_HTTP_ENDPOINT = loadMcpHttpEndpoint();
            const MCP_PROTOCOL_VERSION = "2026-07-28";
            let lastToolRequestName = "webplayer_now_playing";
            let lastToolRequestArgs = {};
            const loadMcpHttpOrigin = () => {
                try {
                    return new URL(MCP_HTTP_ENDPOINT, window.location.href).origin;
                } catch (_) {
                    return String(window.location.origin || "").trim();
                }
            };
            let MCP_HTTP_ORIGIN = loadMcpHttpOrigin();
            const UI_COVER_PATH = __MCP_APP_UI_COVER_PATH__;
            const UI_PREVIEW_PATH = __MCP_APP_UI_PREVIEW_PATH__;
            const UI_SCOPE_PATH = __MCP_APP_UI_SCOPE_PATH__;

                const applyTheme = (themeName) => {
                    const themeKey = String(themeName || "").trim().toLowerCase();
                    const tokens = themeKey === "dark" ? webDarkTheme : webLightTheme;
                    const rootElement = document.documentElement;
                    for (const [tokenName, tokenValue] of Object.entries(tokens)) {
                        rootElement.style.setProperty(`--${tokenName}`, tokenValue);
                    }
                    document.body.style.color = "var(--wp-text)";
                if (!isEmbedded) {
                    document.body.style.background = "#0b0d10";
                }
            };

            const applyHostContext = (ctx) => {
                if (!ctx || typeof ctx !== "object") {
                    return;
                }
                if (ctx.mcpEndpoint) {
                    MCP_HTTP_ENDPOINT = String(ctx.mcpEndpoint).trim();
                    MCP_HTTP_ORIGIN = loadMcpHttpOrigin();
                    if (lastInfoView && Object.keys(lastInfoView).length > 0) {
                        renderInfoView(lastInfoView);
                    }
                }
                if (ctx.theme) {
                    applyTheme(ctx.theme);
                }

                const hostVariables = ctx.styles && ctx.styles.variables;
                if (hostVariables && typeof hostVariables === "object") {
                    for (const [variableName, variableValue] of Object.entries(hostVariables)) {
                        const normalizedName = String(variableName || "").trim();
                        if (!normalizedName) {
                            continue;
                        }
                        const cssName = normalizedName.startsWith("--") ? normalizedName : `--${normalizedName}`;
                        document.documentElement.style.setProperty(cssName, String(variableValue ?? ""));
                    }
                }

                const hostFonts = ctx.styles && ctx.styles.css && ctx.styles.css.fonts;
                if (typeof hostFonts === "string" && hostFonts.trim()) {
                    let hostFontStyle = document.getElementById("mcp-host-fonts");
                    if (!hostFontStyle) {
                        hostFontStyle = document.createElement("style");
                        hostFontStyle.id = "mcp-host-fonts";
                        document.head.appendChild(hostFontStyle);
                    }
                    hostFontStyle.textContent = hostFonts;
                }

                const safeAreaInsets = ctx.safeAreaInsets;
                if (panelEl && safeAreaInsets && typeof safeAreaInsets === "object") {
                    const loadInset = (value) => {
                        const numericValue = Number(value);
                        return Number.isFinite(numericValue) ? `${Math.max(0, numericValue)}px` : "0px";
                    };
                    panelEl.style.setProperty("--mcp-safe-area-top", loadInset(safeAreaInsets.top));
                    panelEl.style.setProperty("--mcp-safe-area-right", loadInset(safeAreaInsets.right));
                    panelEl.style.setProperty("--mcp-safe-area-bottom", loadInset(safeAreaInsets.bottom));
                    panelEl.style.setProperty("--mcp-safe-area-left", loadInset(safeAreaInsets.left));
                }

                const displayMode = String(ctx.displayMode || "").trim();
                if (displayMode) {
                    document.body.dataset.displayMode = displayMode;
                }
            };

            const DEFAULT_TOOL_ARGUMENTS = __WEBPLAYER_UI_DEFAULT_ARGUMENTS__;
            const selectedPlaybackBackend = "browser";
            const loadSelectedBackendArguments = () => ({
                ...DEFAULT_TOOL_ARGUMENTS,
                playback_backend: "browser",
                player_selector: "chromium",
            });
            const syncBackendSelection = () => {
                if (playPauseButtonEl) {
                    const backendArguments = loadSelectedBackendArguments();
                    playPauseButtonEl.setAttribute("data-play-args", JSON.stringify(backendArguments));
                    if (playPauseButtonEl.getAttribute("data-tool") === "webplayer_play") {
                        playPauseButtonEl.setAttribute("data-args", JSON.stringify(backendArguments));
                    }
                }
            };
            const SDK_PREPARE_TOOL_NAMES = new Set([
                "webplayer_play",
                "webplayer_forward",
                "webplayer_backward",
                "webplayer_search_play",
                "webplayer_playlist_play",
                "webplayer_library_play",
                "webplayer_open_playback_target",
            ]);
            const SDK_LOCAL_TOOL_NAMES = new Set([
                "webplayer_pause",
                "webplayer_stop",
                "webplayer_seek",
                "webplayer_volume_set",
                "webplayer_volume_adjust",
            ]);
            let tidalPlayerModule = null;
            let tidalSdkLoadPromise = null;
            let tidalSdkLoadedProductId = "";
            let tidalSdkMediaProduct = null;
            let tidalSdkPlaybackContext = null;
            let tidalSdkPlaybackState = "IDLE";
            let tidalSdkLastError = "";
            let tidalSdkPrimePromise = null;
            let tidalAudioOutputDeviceId = "";
            let tidalAudioOutputDeviceMode = "";
            let tidalAudioOutputPassThrough = null;
            let audioOutputInfo = {
                audioSource: "",
                audioSink: "",
                audioSourceName: "",
                audioSinkName: "",
                audioInterface: "",
                audioProfile: "",
                dsp: "",
            };
            let audioOutputInfoPromise = null;
            let pendingSdkUserPlay = false;
            let tidalApiPreviewMediaElement = null;
            let tidalApiPreviewObjectUrl = "";
            let tidalApiPreviewProductId = "";
            let tidalApiPreviewLoadPromise = null;
            let tidalApiPreviewState = "IDLE";
            let tidalApiPreviewLastError = "";
            let tidalApiPreviewPresentation = "";
            let tidalApiPreviewReason = "";
            let lastServerPayload = {};

            const postEmbedSize = () => {
                const height = Math.max(
                    56,
                    Math.ceil(document.documentElement.scrollHeight || 0),
                    Math.ceil(document.body.scrollHeight || 0)
                );
                postEmbedMessage({ type: "webplayer-mini-controls-size", height });
            };

            const setPlayerBackground = (imageSource) => {
                if (!panelEl) {
                    return;
                }
                const normalizedSource = String(imageSource || "").trim();
                if (!normalizedSource) {
                    panelEl.style.removeProperty("--player-background-image");
                    panelEl.removeAttribute("data-background");
                    return;
                }
                const cssSource = JSON.stringify(normalizedSource);
                panelEl.style.setProperty("--player-background-image", `url(${cssSource})`);
                panelEl.setAttribute("data-background", "cover");
            };

            const setControlsBusy = (isBusy) => {
                const nextValue = Boolean(isBusy);
                const busyElements = [
                    ...controlButtons,
                ].filter(Boolean);
                for (const buttonEl of busyElements) {
                    if (buttonEl && "disabled" in buttonEl) {
                        buttonEl.disabled = nextValue;
                    }
                    if (buttonEl && typeof buttonEl.toggleAttribute === "function") {
                        buttonEl.toggleAttribute("disabled", nextValue);
                    }
                    buttonEl.setAttribute("aria-busy", nextValue ? "true" : "false");
                }
            };

            const syncPanelWidthToControls = () => {
                if (!panelEl) {
                    return;
                }
                panelEl.style.width = "100%";
                panelEl.style.maxWidth = "100%";
            };

            const formatField = (value) => {
                const text = String(value == null ? "" : value).trim();
                return text || "-";
            };

            const loadBooleanFromUnknown = (rawValue) => {
                if (typeof rawValue === "boolean") {
                    return rawValue;
                }
                const normalized = String(rawValue == null ? "" : rawValue).trim().toLowerCase();
                if (!normalized) {
                    return null;
                }
                if (["1", "true", "yes", "on", "liked", "already_liked", "filled", "active", "selected", "pressed"].includes(normalized)) {
                    return true;
                }
                if (["0", "false", "no", "off", "not_liked", "unliked", "outline", "outlined", "not_found", "unknown"].includes(normalized)) {
                    return false;
                }
                return null;
            };

            const loadIsLiked = (nowPayload) => {
                const candidates = [
                    nowPayload && nowPayload.liked,
                    nowPayload && nowPayload.is_liked,
                    nowPayload && nowPayload.liked_state,
                ];

                for (const candidate of candidates) {
                    const parsed = loadBooleanFromUnknown(candidate);
                    if (parsed !== null) {
                        return parsed;
                    }
                }

                return false;
            };

            const loadResourceSummaryText = (resourceValue, options = {}) => {
                if (!resourceValue || typeof resourceValue !== "object" || Array.isArray(resourceValue)) {
                    return formatField(resourceValue);
                }

                const parts = [];
                if (options.includeName !== false) {
                    const nameText = formatField(resourceValue.name || resourceValue.title || resourceValue.display_name);
                    if (nameText !== "-") {
                        parts.push(nameText);
                    }
                }

                const resourceTypeText = formatField(resourceValue.resource_type || resourceValue.type);
                const resourceIdText = formatField(resourceValue.resource_id || resourceValue.id);
                if (resourceTypeText !== "-" || resourceIdText !== "-") {
                    parts.push(resourceTypeText !== "-" ? resourceTypeText : "resource");
                    if (resourceIdText !== "-") {
                        parts[parts.length - 1] = `${parts[parts.length - 1]}:${resourceIdText}`;
                    }
                }

                const releaseDateText = formatField(resourceValue.release_date || resourceValue.releaseDate);
                if (releaseDateText !== "-") {
                    parts.push(`Release ${releaseDateText}`);
                }
                const itemCount = resourceValue.number_of_items ?? resourceValue.numberOfItems ?? resourceValue.number_of_tracks ?? resourceValue.numberOfTracks;
                if (itemCount != null && formatField(itemCount) !== "-") {
                    parts.push(`${formatField(itemCount)} tracks`);
                }
                const resourceDuration = formatField(resourceValue.duration);
                if (resourceDuration !== "-") {
                    parts.push(formatDurationText(resourceDuration));
                }
                const resourceUrl = formatField(
                    resourceValue.url
                    || resourceValue.href
                    || resourceValue.external_url
                    || resourceValue.externalUrl
                );
                if (resourceUrl !== "-") {
                    parts.push(resourceUrl);
                }

                return parts.join(" \u2022 ") || "-";
            };

            const loadResourceListSummaryText = (resourceValues, options = {}) => {
                if (!Array.isArray(resourceValues)) {
                    return loadResourceSummaryText(resourceValues, options);
                }

                const parts = resourceValues
                    .map((resourceValue) => loadResourceSummaryText(resourceValue, options))
                    .filter((part) => part !== "-");
                return parts.length > 0 ? parts.join("; ") : "-";
            };

            const loadTrackSummaryText = (trackValue, options = {}) => {
                if (!trackValue || typeof trackValue !== "object" || Array.isArray(trackValue)) {
                    return formatField(trackValue);
                }

                const parts = [];
                const titleText = formatField(trackValue.title || trackValue.name);
                const artistText = formatField(trackValue.artist);
                const albumText = formatField(trackValue.album);
                const trackIdText = formatField(trackValue.tidal_track_id || trackValue.track_id || trackValue.id);
                const tonartText = formatField(trackValue.tonart || trackValue.initial_key || trackValue.key);

                if (options.includeTitle !== false && titleText !== "-") {
                    parts.push(titleText);
                }
                if (options.includeArtist !== false && artistText !== "-") {
                    parts.push(artistText);
                }
                if (options.includeAlbum !== false && albumText !== "-") {
                    parts.push(albumText);
                }
                if (options.includeTrackId !== false && trackIdText !== "-") {
                    parts.push(`#${trackIdText}`);
                }
                if (options.includeTonart !== false && tonartText !== "-") {
                    parts.push(tonartText);
                }

                return parts.join(" \u2022 ") || "-";
            };

            const loadPlaybackContextText = (contextValue) => {
                if (!contextValue || typeof contextValue !== "object" || Array.isArray(contextValue)) {
                    return formatField(contextValue);
                }

                const queueIndexText = formatField(contextValue.queue_index);
                const queueLengthText = formatField(contextValue.queue_length);
                const parts = [];
                if (queueIndexText !== "-" || queueLengthText !== "-") {
                    parts.push(`Queue ${queueIndexText}/${queueLengthText}`);
                }
                if (contextValue.has_previous != null) {
                    parts.push(`Previous ${contextValue.has_previous ? "yes" : "no"}`);
                }
                if (contextValue.has_next != null) {
                    parts.push(`Next ${contextValue.has_next ? "yes" : "no"}`);
                }
                return parts.join(" \u2022 ") || "-";
            };

            const formatMetricText = (value, suffix) => {
                const text = formatField(value);
                return text === "-" ? "-" : `${text}${suffix}`;
            };

            const loadAudioQualityMetrics = (value) => {
                const text = formatField(value);
                if (text === "-") {
                    return { sampleRateKhz: "", bitDepth: "" };
                }
                const bitDepthMatch = text.match(/\b(\d{1,2})\s*[- ]?bit\b/i);
                const sampleRateKHzMatch = text.match(/\b(\d+(?:\.\d+)?)\s*kHz\b/i);
                const sampleRateHzMatch = text.match(/\b(\d{4,6}(?:\.\d+)?)\s*Hz\b/i);
                let sampleRateKhz = sampleRateKHzMatch ? Number(sampleRateKHzMatch[1]) : 0;
                if (!sampleRateKhz && sampleRateHzMatch) {
                    sampleRateKhz = Number(sampleRateHzMatch[1]) / 1000;
                }
                if (sampleRateKhz >= 1000) {
                    sampleRateKhz /= 1000;
                }
                return {
                    sampleRateKhz: Number.isFinite(sampleRateKhz) && sampleRateKhz > 0 ? sampleRateKhz : "",
                    bitDepth: bitDepthMatch ? bitDepthMatch[1] : "",
                };
            };

            const loadAudioOutputInfo = async () => {
                const mediaElement = tidalApiPreviewMediaElement
                    || (tidalPlayerModule && typeof tidalPlayerModule.getMediaElement === "function"
                        ? tidalPlayerModule.getMediaElement()
                        : null);
                const sinkId = String(mediaElement && mediaElement.sinkId || "").trim();
                let audioInterfaceText = "System default";

                if (tidalPlayerModule && typeof tidalPlayerModule.getOutputDevices === "function") {
                    try {
                        const outputDevices = await tidalPlayerModule.getOutputDevices();
                        const activeDevice = (Array.isArray(outputDevices) ? outputDevices : []).find((device) => (
                            device
                            && (
                                String(device.id || "").trim() === String(tidalAudioOutputDeviceId || "").trim()
                                || String(device.webDeviceId || "").trim() === sinkId
                                || (!sinkId && String(device.id || "").trim() === "default")
                            )
                        ));
                        audioInterfaceText = String(activeDevice && activeDevice.name || "").trim() || audioInterfaceText;
                    } catch (_) {
                        // Fall through to the browser media-device list.
                    }
                }

                if (audioInterfaceText === "System default" && navigator.mediaDevices
                    && typeof navigator.mediaDevices.enumerateDevices === "function") {
                    try {
                        const devices = await navigator.mediaDevices.enumerateDevices();
                        const outputDevices = devices.filter((device) => device && device.kind === "audiooutput");
                        const selectedDevice = outputDevices.find((device) => (
                            sinkId ? String(device.deviceId || "").trim() === sinkId : device.deviceId === "default"
                        )) || outputDevices.find((device) => device.deviceId === "default");
                        audioInterfaceText = String(selectedDevice && selectedDevice.label || "").trim() || audioInterfaceText;
                    } catch (_) {
                        // The default device remains the only reliable value without permission.
                    }
                }

                if (!audioInterfaceText) {
                    audioInterfaceText = sinkId && sinkId !== "default" ? sinkId : "System default";
                }

                let dspText = "Browser/OS";
                if (tidalAudioOutputPassThrough === true) {
                    dspText = "Pass-through";
                } else if (tidalAudioOutputPassThrough === false) {
                    dspText = "Processed";
                } else if (tidalAudioOutputDeviceMode) {
                    dspText = tidalAudioOutputDeviceMode;
                } else if (!mediaElement && !tidalPlayerModule) {
                    dspText = "Unavailable";
                }

                return {
                    audioInterface: audioInterfaceText,
                    dsp: dspText,
                };
            };

            const refreshAudioOutputInfo = () => {
                if (audioOutputInfoPromise) {
                    return audioOutputInfoPromise;
                }
                audioOutputInfoPromise = loadAudioOutputInfo()
                    .then((nextInfo) => {
                        const normalizedInfo = {
                            audioSource: String(nextInfo.audioSource || audioOutputInfo.audioSource || "").trim(),
                            audioSink: String(nextInfo.audioSink || audioOutputInfo.audioSink || "").trim(),
                            audioSourceName: String(nextInfo.audioSourceName || audioOutputInfo.audioSourceName || "").trim(),
                            audioSinkName: String(nextInfo.audioSinkName || audioOutputInfo.audioSinkName || "").trim(),
                            audioInterface: String(nextInfo.audioInterface || audioOutputInfo.audioInterface || "").trim(),
                            audioProfile: String(nextInfo.audioProfile || audioOutputInfo.audioProfile || "").trim(),
                            dsp: String(nextInfo.dsp || "").trim(),
                        };
                        const changed = normalizedInfo.audioSource !== audioOutputInfo.audioSource
                            || normalizedInfo.audioSink !== audioOutputInfo.audioSink
                            || normalizedInfo.audioSourceName !== audioOutputInfo.audioSourceName
                            || normalizedInfo.audioSinkName !== audioOutputInfo.audioSinkName
                            || normalizedInfo.audioInterface !== audioOutputInfo.audioInterface
                            || normalizedInfo.audioProfile !== audioOutputInfo.audioProfile
                            || normalizedInfo.dsp !== audioOutputInfo.dsp;
                        audioOutputInfo = normalizedInfo;
                        if (changed && lastInfoView && Object.keys(lastInfoView).length > 0) {
                            lastInfoView = {
                                ...lastInfoView,
                                ...audioOutputInfo,
                            };
                            lastInfoSignature = loadInfoSignature(lastInfoView);
                            renderInfoView(lastInfoView);
                        }
                        return normalizedInfo;
                    })
                    .catch(() => audioOutputInfo)
                    .finally(() => {
                        audioOutputInfoPromise = null;
                    });
                return audioOutputInfoPromise;
            };

            const escapeHtml = (value) => {
                const text = String(value == null ? "" : value);
                return text
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/\"/g, "&quot;")
                    .replace(/'/g, "&#39;");
            };

            const loadQualityLevel = (qualityValue) => {
                const normalized = String(qualityValue || "").trim().toLowerCase();
                if (!normalized || normalized === "-" || normalized === "unknown") {
                    return "";
                }
                if (/(hi[ _-]?res|master|max|24 ?bit|96 ?khz|192 ?khz)/.test(normalized)) {
                    return "hi-res";
                }
                if (/(lossless|flac|alac|cd)/.test(normalized)) {
                    return "lossless";
                }
                if (/(normal|low|96|128)/.test(normalized)) {
                    return "low";
                }
                return "";
            };

            const parseJsonOrNull = (rawText) => {
                if (typeof rawText !== "string") {
                    return null;
                }
                try {
                    return JSON.parse(rawText);
                } catch (_) {
                    return null;
                }
            };

            const extractStructuredPayload = (toolResult) => {
                if (!toolResult || typeof toolResult !== "object") {
                    return {};
                }

                if (toolResult.structuredContent && typeof toolResult.structuredContent === "object") {
                    return toolResult.structuredContent;
                }

                const nestedResult = toolResult.result;
                if (nestedResult && typeof nestedResult === "object") {
                    if (nestedResult.structuredContent && typeof nestedResult.structuredContent === "object") {
                        return nestedResult.structuredContent;
                    }
                    if (Array.isArray(nestedResult.content)) {
                        for (const contentItem of nestedResult.content) {
                            if (contentItem && contentItem.type === "text") {
                                const parsed = parseJsonOrNull(contentItem.text);
                                if (parsed && typeof parsed === "object") {
                                    return parsed;
                                }
                            }
                        }
                    }
                    if (typeof nestedResult.content === "string") {
                        const parsed = parseJsonOrNull(nestedResult.content);
                        if (parsed && typeof parsed === "object") {
                            return parsed;
                        }
                    }
                }

                if (Array.isArray(toolResult.content)) {
                    for (const contentItem of toolResult.content) {
                        if (contentItem && contentItem.type === "text") {
                            const parsed = parseJsonOrNull(contentItem.text);
                            if (parsed && typeof parsed === "object") {
                                return parsed;
                            }
                        }
                    }
                }

                if (typeof toolResult.content === "string") {
                    const parsed = parseJsonOrNull(toolResult.content);
                    if (parsed && typeof parsed === "object") {
                        return parsed;
                    }
                }

                return {};
            };

            const loadToolError = (resultPayload, toolResult) => {
                const payload = resultPayload && typeof resultPayload === "object" ? resultPayload : {};
                const result = toolResult && typeof toolResult === "object"
                    ? (toolResult.result && typeof toolResult.result === "object" ? toolResult.result : toolResult)
                    : {};
                if (payload.ok !== false && result.isError !== true) {
                    return "";
                }
                const stdoutError = /(?:^|\\n)error=([^\\n]+)/.exec(String(payload.stdout || ""));
                const textItem = Array.isArray(result.content)
                    ? result.content.find((item) => item && item.type === "text")
                    : null;
                return String(
                    payload.error
                    || (stdoutError && stdoutError[1])
                    || payload.stderr
                    || (textItem && textItem.text)
                    || `MCP tool failed: ${payload.object_name || "unknown"}`
                ).trim();
            };

            const withTimeout = (promiseValue, timeoutMs, errorMessage) => {
                let timeoutHandle = null;
                const timeoutPromise = new Promise((_, reject) => {
                    timeoutHandle = setTimeout(() => reject(new Error(errorMessage)), timeoutMs);
                });
                return Promise.race([Promise.resolve(promiseValue), timeoutPromise]).finally(() => {
                    if (timeoutHandle !== null) {
                        clearTimeout(timeoutHandle);
                    }
                });
            };

            const ensureTidalApiPreviewMediaElement = () => {
                if (tidalApiPreviewMediaElement) {
                    return tidalApiPreviewMediaElement;
                }
                const audioElement = document.createElement("audio");
                audioElement.preload = "auto";
                audioElement.crossOrigin = "anonymous";
                audioElement.setAttribute("aria-hidden", "true");
                audioElement.tabIndex = -1;
                audioElement.style.position = "fixed";
                audioElement.style.left = "-10000px";
                audioElement.style.top = "-10000px";
                audioElement.style.width = "1px";
                audioElement.style.height = "1px";
                audioElement.style.opacity = "0";
                audioElement.style.pointerEvents = "none";
                audioElement.addEventListener("playing", () => {
                    tidalApiPreviewState = "PLAYING";
                    tidalApiPreviewLastError = "";
                    renderSdkState();
                });
                audioElement.addEventListener("pause", () => {
                    if (!audioElement.ended) {
                        tidalApiPreviewState = "NOT_PLAYING";
                        renderSdkState();
                    }
                });
                audioElement.addEventListener("waiting", () => {
                    tidalApiPreviewState = "STALLED";
                    renderSdkState();
                });
                audioElement.addEventListener("ended", () => {
                    tidalApiPreviewState = "IDLE";
                    renderSdkState();
                });
                audioElement.addEventListener("error", () => {
                    const mediaError = audioElement.error;
                    tidalApiPreviewLastError = mediaError && mediaError.code
                        ? `tidal_preview_media_error_${mediaError.code}`
                        : "tidal_preview_media_error";
                    tidalApiPreviewState = "STALLED";
                    renderSdkState();
                });
                document.body.appendChild(audioElement);
                tidalApiPreviewMediaElement = audioElement;
                return audioElement;
            };

            const waitForTidalPreviewMedia = (audioElement) => new Promise((resolve, reject) => {
                if (audioElement.readyState >= 2) {
                    resolve();
                    return;
                }
                let timeoutHandle = null;
                const cleanup = () => {
                    audioElement.removeEventListener("canplay", onReady);
                    audioElement.removeEventListener("loadeddata", onReady);
                    audioElement.removeEventListener("error", onError);
                    if (timeoutHandle !== null) {
                        clearTimeout(timeoutHandle);
                    }
                };
                const onReady = () => {
                    cleanup();
                    resolve();
                };
                const onError = () => {
                    cleanup();
                    reject(new Error("tidal_preview_media_not_playable"));
                };
                audioElement.addEventListener("canplay", onReady);
                audioElement.addEventListener("loadeddata", onReady);
                audioElement.addEventListener("error", onError);
                timeoutHandle = setTimeout(() => {
                    cleanup();
                    reject(new Error("tidal_preview_media_ready_timeout"));
                }, 10000);
            });

            const resetTidalApiPreviewMedia = () => {
                const audioElement = tidalApiPreviewMediaElement;
                if (audioElement) {
                    audioElement.pause();
                    audioElement.removeAttribute("src");
                    audioElement.load();
                }
                if (tidalApiPreviewObjectUrl) {
                    URL.revokeObjectURL(tidalApiPreviewObjectUrl);
                }
                tidalApiPreviewObjectUrl = "";
                tidalApiPreviewProductId = "";
                tidalApiPreviewState = "IDLE";
                tidalApiPreviewPresentation = "";
                tidalApiPreviewReason = "";
            };

            const primeTidalApiPreview = (payloadValue) => {
                const mediaProduct = loadSdkMediaProductFromPayload(payloadValue);
                if (!mediaProduct || mediaProduct.productType !== "track") {
                    return Promise.resolve(null);
                }
                if (tidalApiPreviewProductId === mediaProduct.productId && tidalApiPreviewObjectUrl) {
                    return Promise.resolve(tidalApiPreviewMediaElement);
                }
                if (tidalApiPreviewLoadPromise) {
                    return tidalApiPreviewLoadPromise;
                }

                resetTidalApiPreviewMedia();
                const audioElement = ensureTidalApiPreviewMediaElement();
                tidalApiPreviewLastError = "";
                const productId = mediaProduct.productId;
                tidalApiPreviewLoadPromise = (async () => {
                    try {
                        const toolResult = await callServerToolRaw("tidal_api_track_manifest", {
                            track_id: productId,
                            formats: "AACLC",
                            manifest_type: "HLS",
                            uri_scheme: "HTTPS",
                            usage: "PLAYBACK",
                        });
                        const toolPayload = extractStructuredPayload(toolResult);
                        const manifestEnvelope = typeof toolPayload.response_body === "string"
                            ? parseJsonOrNull(toolPayload.response_body)
                            : toolPayload.response_body;
                        const attributes = manifestEnvelope
                            && manifestEnvelope.data
                            && manifestEnvelope.data.attributes
                            && typeof manifestEnvelope.data.attributes === "object"
                            ? manifestEnvelope.data.attributes
                            : {};
                        const presentation = String(attributes.trackPresentation || "").trim().toUpperCase();
                        const previewReason = String(attributes.previewReason || "").trim();
                        tidalApiPreviewPresentation = presentation;
                        tidalApiPreviewReason = previewReason;
                        if (presentation && presentation !== "PREVIEW") {
                            return null;
                        }
                        const previewOrigin = String(MCP_HTTP_ORIGIN || window.location.origin || "").trim();
                        const previewUrl = new URL(
                            UI_PREVIEW_PATH,
                            previewOrigin || window.location.origin,
                        );
                        previewUrl.searchParams.set("track_id", productId);
                        const previewResponse = await fetch(previewUrl.href, {
                            credentials: "include",
                            cache: "no-store",
                        });
                        if (!previewResponse.ok) {
                            let previewError = "";
                            try {
                                const previewErrorPayload = await previewResponse.json();
                                previewError = String(previewErrorPayload.error || "").trim();
                            } catch (_) {
                                previewError = "";
                            }
                            throw new Error(previewError || `tidal_preview_http_${previewResponse.status}`);
                        }
                        const previewBlob = await previewResponse.blob();
                        if (!previewBlob.size) {
                            throw new Error("tidal_preview_empty_stream");
                        }
                        const objectUrl = URL.createObjectURL(new Blob([previewBlob], { type: previewBlob.type || "audio/mp4" }));
                        audioElement.src = objectUrl;
                        audioElement.load();
                        await waitForTidalPreviewMedia(audioElement);
                        tidalApiPreviewObjectUrl = objectUrl;
                        tidalApiPreviewProductId = productId;
                        tidalApiPreviewState = "NOT_PLAYING";
                        renderSdkState();
                        return audioElement;
                    } catch (error) {
                        tidalApiPreviewLastError = String(error && error.message ? error.message : error);
                        tidalApiPreviewState = "STALLED";
                        renderSdkState();
                        return null;
                    } finally {
                        tidalApiPreviewLoadPromise = null;
                    }
                })();
                return tidalApiPreviewLoadPromise;
            };

            const startTidalApiPreviewFromUserGesture = () => {
                const audioElement = tidalApiPreviewMediaElement;
                if (!audioElement || !tidalApiPreviewProductId || !tidalApiPreviewObjectUrl) {
                    return false;
                }
                try {
                    const playPromise = audioElement.play();
                    tidalApiPreviewState = "PLAYING";
                    tidalApiPreviewLastError = "";
                    renderSdkState();
                    if (playPromise && typeof playPromise.catch === "function") {
                        playPromise.catch((error) => {
                            tidalApiPreviewLastError = String(error && error.message ? error.message : error);
                            tidalApiPreviewState = "NOT_PLAYING";
                            renderSdkState();
                        });
                    }
                    return true;
                } catch (error) {
                    tidalApiPreviewLastError = String(error && error.message ? error.message : error);
                    tidalApiPreviewState = "NOT_PLAYING";
                    renderSdkState();
                    return false;
                }
            };

            const stopTidalApiPreview = () => {
                if (tidalApiPreviewMediaElement) {
                    tidalApiPreviewMediaElement.pause();
                }
                if (tidalApiPreviewProductId) {
                    tidalApiPreviewState = "IDLE";
                }
            };

            const loadNowPlayingView = (toolResult) => {
                const payload = extractStructuredPayload(toolResult);
                const basePayload = payload && typeof payload === "object" ? payload : {};
                const nestedNowPayload = basePayload.now_playing && typeof basePayload.now_playing === "object"
                    ? basePayload.now_playing
                    : {};
                const nowPayload = { ...basePayload, ...nestedNowPayload };
                const serverAudioOutputInfo = {
                    audioSource: String(nowPayload.audio_source || nowPayload.audioSource || "").trim(),
                    audioSink: String(nowPayload.audio_sink || nowPayload.audioSink || "").trim(),
                    audioSourceName: String(nowPayload.audio_source_name || nowPayload.audioSourceName || "").trim(),
                    audioSinkName: String(nowPayload.audio_sink_name || nowPayload.audioSinkName || "").trim(),
                    audioInterface: String(nowPayload.audio_interface || nowPayload.audioInterface || "").trim(),
                    audioProfile: String(nowPayload.audio_profile || nowPayload.audioProfile || "").trim(),
                };
                if (Object.values(serverAudioOutputInfo).some((value) => value)) {
                    audioOutputInfo = {
                        ...audioOutputInfo,
                        ...Object.fromEntries(
                            Object.entries(serverAudioOutputInfo).filter(([, value]) => Boolean(value))
                        ),
                    };
                }
                const mediaProductValue = nowPayload.media_product && typeof nowPayload.media_product === "object"
                    ? nowPayload.media_product
                    : nowPayload;
                const nextMediaProductValue = nowPayload.next_media_product && typeof nowPayload.next_media_product === "object"
                    ? nowPayload.next_media_product
                    : {};
                const playbackContextValue = nowPayload.playback_context && typeof nowPayload.playback_context === "object"
                    ? nowPayload.playback_context
                    : {};

                const playerName = formatField(nowPayload.player);
                const trackUrl = String(nowPayload.track_url || nowPayload.url || nowPayload.opened_url || "").trim();
                const tonartDisplay = (() => {
                    const explicitDisplay = String(nowPayload.tonart_display || "").trim();
                    if (explicitDisplay) {
                        return explicitDisplay;
                    }

                    const normalizedParts = [
                        nowPayload.tonart_dur_moll,
                        nowPayload.tonart_alpha,
                        nowPayload.tonart_numeric,
                        mediaProductValue.tonart_dur_moll,
                        mediaProductValue.tonart_alpha,
                        mediaProductValue.tonart_numeric,
                    ]
                        .map((part) => String(part == null ? "" : part).trim())
                        .filter((part) => Boolean(part));
                    if (normalizedParts.length > 0) {
                        return normalizedParts.join(" | ");
                    }

                    return String(
                        nowPayload.tonart
                        || nowPayload.initial_key
                        || nowPayload.key
                        || nowPayload.musical_key
                        || mediaProductValue.tonart
                        || mediaProductValue.initial_key
                        || mediaProductValue.key
                        || mediaProductValue.musical_key
                        || ""
                    ).trim();
                })();

                const streamQualityValue = (
                    nowPayload.stream_quality
                    || nowPayload.audio_quality
                    || nowPayload.quality
                    || nowPayload.tidal_api_stream_quality
                    || mediaProductValue.stream_quality
                    || mediaProductValue.audio_quality
                    || mediaProductValue.quality
                );
                const audioModeValue = (
                    nowPayload.audio_mode
                    || nowPayload.tidal_audio_mode
                    || nowPayload.tidal_api_audio_mode
                    || mediaProductValue.audio_mode
                    || mediaProductValue.audioMode
                );
                const qualityResolutionValue = (
                    nowPayload.quality_resolution
                    || nowPayload.audio_resolution
                    || nowPayload.quality_details
                    || mediaProductValue.quality_resolution
                    || mediaProductValue.audio_resolution
                    || mediaProductValue.quality_details
                );
                const parsedQualityMetrics = loadAudioQualityMetrics(qualityResolutionValue);
                const bitrateKbpsValue = (
                    nowPayload.bitrate_kbps
                    || nowPayload.bitrate
                    || nowPayload.audio_bitrate
                    || mediaProductValue.bitrate_kbps
                    || mediaProductValue.bitrate
                    || mediaProductValue.audio_bitrate
                );
                const sampleRateKhzValue = (
                    nowPayload.sample_rate_khz
                    || nowPayload.sample_rate
                    || nowPayload.sampleRateKHz
                    || mediaProductValue.sample_rate_khz
                    || mediaProductValue.sample_rate
                    || mediaProductValue.sampleRateKHz
                    || parsedQualityMetrics.sampleRateKhz
                );
                const bitDepthValue = (
                    nowPayload.bit_depth
                    || nowPayload.bitDepth
                    || mediaProductValue.bit_depth
                    || mediaProductValue.bitDepth
                    || parsedQualityMetrics.bitDepth
                );
                const channelCountValue = (
                    nowPayload.channel_count
                    || nowPayload.channelCount
                    || mediaProductValue.channel_count
                    || mediaProductValue.channelCount
                );
                const codecValue = (
                    nowPayload.codec
                    || nowPayload.audioCodec
                    || mediaProductValue.codec
                    || mediaProductValue.audioCodec
                );
                const tidalApiDspValue = (
                    nowPayload.dsp
                    || nowPayload.tidal_api_dsp
                    || mediaProductValue.dsp
                    || mediaProductValue.tidal_api_dsp
                );
                const bpmValue = (
                    nowPayload.bpm
                    || nowPayload.tempo
                    || nowPayload.beats_per_minute
                    || mediaProductValue.bpm
                    || mediaProductValue.tempo
                    || mediaProductValue.beats_per_minute
                );
                const mediaTagsValue = Array.isArray(nowPayload.media_tags)
                    ? nowPayload.media_tags
                    : Array.isArray(nowPayload.mediaTags)
                    ? nowPayload.mediaTags
                    : Array.isArray(mediaProductValue.media_tags)
                    ? mediaProductValue.media_tags
                    : Array.isArray(mediaProductValue.mediaTags)
                    ? mediaProductValue.mediaTags
                    : [];
                const artistInfoValue = nowPayload.artist_info || mediaProductValue.artist_info || [];
                const albumInfoValue = nowPayload.album_info || mediaProductValue.album_info || {};
                const trackInfoValue = nowPayload.track_info || mediaProductValue.track_info || {};
                const artistResourceValue = Array.isArray(artistInfoValue)
                    ? artistInfoValue[0]
                    : artistInfoValue;
                const artistUrlValue = (
                    nowPayload.artist_url
                    || nowPayload.artistUrl
                    || mediaProductValue.artist_url
                    || mediaProductValue.artistUrl
                    || loadTidalResourceUrl(artistResourceValue, "artist")
                );
                const albumUrlValue = (
                    nowPayload.album_url
                    || nowPayload.albumUrl
                    || mediaProductValue.album_url
                    || mediaProductValue.albumUrl
                    || loadTidalResourceUrl(albumInfoValue, "album")
                );
                const coverUrlValue = (
                    nowPayload.art_url
                    || nowPayload.album_art_url
                    || nowPayload.cover_url
                    || nowPayload.image_url
                    || nowPayload.artwork_url
                    || mediaProductValue.art_url
                    || mediaProductValue.album_art_url
                    || mediaProductValue.cover_url
                    || mediaProductValue.image_url
                    || mediaProductValue.artwork_url
                    || ""
                );
                const titleText = formatField(nowPayload.title || mediaProductValue.title || mediaProductValue.name);
                const artistText = formatField(nowPayload.artist || mediaProductValue.artist);
                const albumText = formatField(nowPayload.album || mediaProductValue.album);
                const trackIdText = formatField(
                    nowPayload.tidal_track_id
                    || nowPayload.track_id
                    || nowPayload.id
                    || mediaProductValue.tidal_track_id
                    || mediaProductValue.track_id
                    || mediaProductValue.id
                );
                const durationText = formatField(
                    nowPayload.duration
                    || nowPayload.duration_seconds
                    || nowPayload.track_duration
                    || nowPayload.playback_duration
                    || nowPayload.mpris_length
                    || mediaProductValue.duration_seconds
                    || mediaProductValue.duration
                    || mediaProductValue.track_duration
                    || mediaProductValue.playback_duration
                    || mediaProductValue.mpris_length
                );
                const assetPositionValue = (
                    nowPayload.asset_position
                    ?? nowPayload.assetPosition
                    ?? nowPayload.position_seconds
                    ?? nowPayload.position
                    ?? mediaProductValue.asset_position
                    ?? mediaProductValue.assetPosition
                    ?? mediaProductValue.position_seconds
                    ?? mediaProductValue.position
                );
                const volumePercentValue = (
                    nowPayload.system_volume_percent
                    ?? nowPayload.sink_volume_percent
                    ?? mediaProductValue.system_volume_percent
                    ?? mediaProductValue.sink_volume_percent
                    ?? nowPayload.volume_percent
                    ?? nowPayload.volume
                    ?? mediaProductValue.volume_percent
                    ?? mediaProductValue.volume
                );
                const volumeTargetValue = (
                    nowPayload.volume_target
                    || mediaProductValue.volume_target
                    || "sink"
                );
                const volumeSinkValue = (
                    nowPayload.volume_sink
                    || mediaProductValue.volume_sink
                );

                const hasLikeMetadata = [
                    "liked",
                    "is_liked",
                    "liked_state",
                ].some((fieldName) => nowPayload[fieldName] != null);
                const hasExplicitMetadata = [
                    "explicit",
                    "is_explicit",
                    "isExplicit",
                ].some((fieldName) => nowPayload[fieldName] != null);

                return {
                    player: playerName,
                    status: formatField(nowPayload.status),
                    artist: artistText,
                    artistInfo: loadResourceListSummaryText(artistInfoValue, { includeName: false }),
                    album: albumText,
                    albumInfo: loadResourceSummaryText(albumInfoValue, { includeName: false }),
                    trackInfo: loadResourceSummaryText(trackInfoValue, { includeName: false }),
                    track: titleText,
                    mediaProduct: loadTrackSummaryText(mediaProductValue),
                    nextMediaProduct: loadTrackSummaryText(nextMediaProductValue),
                    playbackContext: loadPlaybackContextText(playbackContextValue),
                    mediaProductType: formatField(nowPayload.media_product_type || mediaProductValue.media_product_type),
                    trackId: trackIdText,
                    duration: durationText,
                    volumePercent: formatField(volumePercentValue),
                    volumeTarget: formatField(volumeTargetValue),
                    volumeSink: formatField(volumeSinkValue),
                    explicit: loadBooleanFromUnknown(
                        nowPayload.explicit
                        ?? nowPayload.is_explicit
                        ?? nowPayload.isExplicit
                        ?? mediaProductValue.explicit
                        ?? mediaProductValue.is_explicit
                        ?? mediaProductValue.isExplicit
                    ),
                    isLiked: loadIsLiked(nowPayload),
                    streamProvider: formatField(
                        nowPayload.stream_provider
                        || nowPayload.provider
                        || nowPayload.service
                        || nowPayload.platform
                        || "TIDAL"
                    ),
                    streamQuality: formatField(streamQualityValue),
                    audioMode: formatField(audioModeValue),
                    qualityResolution: formatField(qualityResolutionValue),
                    bitrateKbps: formatField(bitrateKbpsValue),
                    sampleRateKhz: formatField(sampleRateKhzValue),
                    bitDepth: formatField(bitDepthValue),
                    channelCount: formatField(channelCountValue),
                    codec: formatField(codecValue),
                    bpm: formatField(bpmValue),
                    tonart: formatField(tonartDisplay),
                    hasLikeMetadata,
                    hasExplicitMetadata,
                    playbackBackend: formatField(nowPayload.playback_backend || nowPayload.backend),
                    backend: formatField(nowPayload.backend || nowPayload.playback_backend),
                    mediaTags: formatField(mediaTagsValue.length > 0 ? mediaTagsValue.join(", ") : ""),
                    releaseDate: formatField(nowPayload.release_date || mediaProductValue.release_date),
                    trackNumber: formatField(nowPayload.track_number ?? mediaProductValue.track_number),
                    volumeNumber: formatField(nowPayload.volume_number ?? mediaProductValue.volume_number),
                    version: formatField(nowPayload.version || mediaProductValue.version),
                    isrc: formatField(nowPayload.isrc || mediaProductValue.isrc),
                    copyright: formatField(nowPayload.copyright || mediaProductValue.copyright),
                    sdkPlayerVersion: formatField(nowPayload.sdk_player_version || mediaProductValue.sdk_player_version),
                    assetPresentation: formatField(nowPayload.asset_presentation || mediaProductValue.asset_presentation),
                    previewReason: formatField(nowPayload.preview_reason || mediaProductValue.preview_reason),
                    streamType: formatField(nowPayload.stream_type || mediaProductValue.stream_type),
                    playbackSessionId: formatField(nowPayload.playback_session_id || mediaProductValue.playback_session_id),
                    assetPosition: formatField(assetPositionValue),
                    bandwidthKbps: formatField(nowPayload.bandwidth_kbps || mediaProductValue.bandwidth_kbps),
                    sdkError: formatField(nowPayload.sdk_error || mediaProductValue.sdk_error),
                    audioSource: formatField(nowPayload.audio_source || nowPayload.audioSource || audioOutputInfo.audioSource),
                    audioSink: formatField(nowPayload.audio_sink || nowPayload.audioSink || audioOutputInfo.audioSink),
                    audioSourceName: formatField(
                        nowPayload.audio_source_name
                        || nowPayload.audioSourceName
                        || audioOutputInfo.audioSourceName
                    ),
                    audioSinkName: formatField(
                        nowPayload.audio_sink_name
                        || nowPayload.audioSinkName
                        || audioOutputInfo.audioSinkName
                    ),
                    audioInterface: formatField(
                        nowPayload.audio_interface
                        || nowPayload.audioInterface
                        || audioOutputInfo.audioInterface
                    ),
                    audioProfile: formatField(
                        nowPayload.audio_profile
                        || nowPayload.audioProfile
                        || audioOutputInfo.audioProfile
                    ),
                    audioAuxRouting: formatField(nowPayload.audio_aux_routing || mediaProductValue.audio_aux_routing),
                    audioAuxRoutingStatus: formatField(
                        nowPayload.audio_aux_routing_status
                        || mediaProductValue.audio_aux_routing_status
                    ),
                    audioAuxDspData: formatField(nowPayload.audio_aux_dsp_data || mediaProductValue.audio_aux_dsp_data),
                    audioAuxDspStatus: formatField(
                        nowPayload.audio_aux_dsp_status
                        || mediaProductValue.audio_aux_dsp_status
                    ),
                    dsp: formatField(tidalApiDspValue || audioOutputInfo.dsp),
                    coverUrl: String(coverUrlValue).trim(),
                    trackUrl,
                    artistUrl: loadTidalResourceUrl(artistUrlValue, "artist"),
                    albumUrl: loadTidalResourceUrl(albumUrlValue, "album"),
                };
            };

            const loadSdkPlaybackStateText = (stateValue) => {
                const normalized = String(stateValue || "").trim().toUpperCase();
                if (normalized === "PLAYING") {
                    return "Playing";
                }
                if (normalized === "NOT_PLAYING") {
                    return "Paused";
                }
                if (normalized === "STALLED") {
                    return "Stalled";
                }
                return "Stopped";
            };

            const loadSdkMediaProductFromPayload = (payloadValue, next = false) => {
                const payload = payloadValue && typeof payloadValue === "object" ? payloadValue : {};
                const nestedNow = payload.now_playing && typeof payload.now_playing === "object"
                    ? payload.now_playing
                    : {};
                const candidate = next
                    ? (payload.next_media_product || nestedNow.next_media_product || {})
                    : (payload.media_product || nestedNow.media_product || payload);
                if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
                    return null;
                }
                const productId = String(
                    candidate.productId
                    || candidate.product_id
                    || candidate.tidal_track_id
                    || candidate.track_id
                    || candidate.id
                    || ""
                ).trim();
                if (!productId) {
                    return null;
                }
                const productType = String(
                    candidate.productType
                    || candidate.product_type
                    || candidate.media_product_type
                    || "track"
                ).trim().toLowerCase() === "video" ? "video" : "track";
                return {
                    productId,
                    productType,
                    sourceId: String(candidate.sourceId || candidate.source_id || productId).trim() || productId,
                    sourceType: String(candidate.sourceType || candidate.source_type || productType.toUpperCase()).trim() || productType.toUpperCase(),
                    ...(candidate.referenceId ? { referenceId: String(candidate.referenceId) } : {}),
                    ...(candidate.shareCode ? { shareCode: String(candidate.shareCode) } : {}),
                };
            };

            const loadSdkPayload = () => {
                const serverPayload = lastServerPayload && typeof lastServerPayload === "object" ? lastServerPayload : {};
                const sdkBackendActive = selectedPlaybackBackend === "sdk";
                let sdkProduct = sdkBackendActive ? tidalSdkMediaProduct : null;
                let sdkContext = sdkBackendActive && tidalSdkPlaybackContext && typeof tidalSdkPlaybackContext === "object"
                    ? tidalSdkPlaybackContext
                    : {};
                let sdkNextProduct = null;
                if (sdkBackendActive && tidalPlayerModule && typeof tidalPlayerModule.getMediaProduct === "function") {
                    try {
                        sdkProduct = tidalPlayerModule.getMediaProduct() || sdkProduct;
                    } catch (_) {
                        // Keep the last transition when the SDK has no active player yet.
                    }
                }
                if (sdkBackendActive && tidalPlayerModule && typeof tidalPlayerModule.getPlaybackState === "function") {
                    try {
                        const currentState = String(tidalPlayerModule.getPlaybackState() || "").trim().toUpperCase();
                        if (currentState) {
                            tidalSdkPlaybackState = currentState;
                        }
                    } catch (_) {
                        // Keep the last event state when the SDK is not ready yet.
                    }
                }
                if (sdkBackendActive && tidalPlayerModule && typeof tidalPlayerModule.getPlaybackContext === "function") {
                    try {
                        sdkContext = tidalPlayerModule.getPlaybackContext() || sdkContext;
                    } catch (_) {
                        // Keep the last transition when the SDK has no context yet.
                    }
                }
                if (sdkBackendActive && tidalPlayerModule && typeof tidalPlayerModule.getNextMediaProduct === "function") {
                    try {
                        sdkNextProduct = tidalPlayerModule.getNextMediaProduct() || null;
                    } catch (_) {
                        // The SDK returns null when no next item is prepared.
                    }
                }
                const apiPreviewActive = Boolean(
                    selectedPlaybackBackend === "api"
                    && tidalApiPreviewProductId
                    && tidalApiPreviewObjectUrl
                    && tidalApiPreviewMediaElement
                );
                const effectivePlaybackState = apiPreviewActive
                    ? tidalApiPreviewState
                    : sdkBackendActive
                    ? tidalSdkPlaybackState
                    : String(serverPayload.status || "Ready");
                const apiPreviewPosition = apiPreviewActive && tidalApiPreviewMediaElement
                    ? Number(tidalApiPreviewMediaElement.currentTime)
                    : NaN;
                const apiPreviewDuration = apiPreviewActive && tidalApiPreviewMediaElement
                    ? Number(tidalApiPreviewMediaElement.duration)
                    : NaN;
                const serverMediaProduct = loadSdkMediaProductFromPayload(serverPayload);
                const serverNextMediaProduct = loadSdkMediaProductFromPayload(serverPayload, true);
                const productId = String(
                    sdkProduct?.productId
                    || serverMediaProduct?.productId
                    || serverPayload.tidal_track_id
                    || serverPayload.track_id
                    || ""
                ).trim();
                const mediaProduct = productId
                    ? {
                        ...(serverPayload.media_product && typeof serverPayload.media_product === "object" ? serverPayload.media_product : {}),
                        ...(serverMediaProduct || {}),
                        ...(sdkProduct || {}),
                        tidal_track_id: productId,
                        media_product_type: sdkProduct?.productType || serverMediaProduct?.productType || "track",
                    }
                    : (serverPayload.media_product || {});
                const sampleRateValue = Number(sdkContext.sampleRate);
                const bandwidthValue = Number(sdkContext.bandwidth);
                const sdkPayload = {
                    ...serverPayload,
                    status: apiPreviewActive
                        ? loadSdkPlaybackStateText(effectivePlaybackState)
                        : sdkBackendActive
                        ? loadSdkPlaybackStateText(effectivePlaybackState)
                        : formatField(serverPayload.status || "Ready"),
                    player: apiPreviewActive ? "tidal-api-preview" : sdkBackendActive ? "tidal-sdk" : "tidal-api",
                    backend: apiPreviewActive ? "tidal-api" : sdkBackendActive ? "tidal-sdk" : "tidal-api",
                    playback_backend: apiPreviewActive ? "api" : sdkBackendActive ? "sdk" : "api",
                    media_product: mediaProduct,
                    media_product_type: mediaProduct.media_product_type || "track",
                    tidal_track_id: productId,
                    track_id: productId,
                    playback_context: {
                        ...(serverPayload.playback_context && typeof serverPayload.playback_context === "object" ? serverPayload.playback_context : {}),
                        ...sdkContext,
                        status: apiPreviewActive
                            ? loadSdkPlaybackStateText(effectivePlaybackState)
                            : sdkBackendActive
                            ? loadSdkPlaybackStateText(effectivePlaybackState)
                            : formatField(serverPayload.status || "Ready"),
                    },
                    next_media_product: sdkNextProduct || serverNextMediaProduct || serverPayload.next_media_product || {},
                    sdk_player_version: "@tidal-music/player@0.20.1",
                    asset_presentation: sdkContext.actualAssetPresentation || tidalApiPreviewPresentation || serverPayload.asset_presentation || "",
                    preview_reason: sdkContext.previewReason || tidalApiPreviewReason || serverPayload.preview_reason || "",
                    stream_type: sdkContext.actualStreamType || serverPayload.stream_type || "",
                    playback_session_id: sdkContext.playbackSessionId || serverPayload.playback_session_id || "",
                    asset_position: Number.isFinite(apiPreviewPosition)
                        ? apiPreviewPosition
                        : Number.isFinite(Number(sdkContext.assetPosition))
                        ? Number(sdkContext.assetPosition)
                        : serverPayload.asset_position,
                    stream_quality: sdkContext.actualAudioQuality || serverPayload.stream_quality || "",
                    audio_mode: sdkContext.actualAudioMode || serverPayload.audio_mode || "",
                    sample_rate_khz: Number.isFinite(sampleRateValue) && sampleRateValue > 0
                        ? (sampleRateValue >= 1000 ? sampleRateValue / 1000 : sampleRateValue)
                        : serverPayload.sample_rate_khz,
                    bit_depth: sdkContext.bitDepth ?? serverPayload.bit_depth,
                    codec: sdkContext.codec || serverPayload.codec || "",
                    bandwidth_kbps: Number.isFinite(bandwidthValue) && bandwidthValue > 0
                        ? bandwidthValue / 1000
                        : serverPayload.bandwidth_kbps,
                    bitrate_kbps: Number.isFinite(bandwidthValue) && bandwidthValue > 0
                        ? bandwidthValue / 1000
                        : serverPayload.bitrate_kbps,
                };
                if (Number.isFinite(Number(sdkContext.actualDuration)) && Number(sdkContext.actualDuration) > 0) {
                    sdkPayload.duration = Number(sdkContext.actualDuration);
                } else if (Number.isFinite(apiPreviewDuration) && apiPreviewDuration > 0) {
                    sdkPayload.duration = apiPreviewDuration;
                }
                if (tidalApiPreviewLastError || tidalSdkLastError) {
                    sdkPayload.sdk_error = tidalApiPreviewLastError || tidalSdkLastError;
                }
                return sdkPayload;
            };

            const primeTidalSdkProduct = (payloadValue) => {
                if (selectedPlaybackBackend !== "sdk") {
                    return Promise.resolve(null);
                }
                if (tidalSdkPrimePromise) {
                    return tidalSdkPrimePromise;
                }
                tidalSdkPrimePromise = (async () => {
                    try {
                        const mediaProduct = loadSdkMediaProductFromPayload(payloadValue);
                        if (!mediaProduct) {
                            return null;
                        }
                        const playerModule = await ensureTidalSdk();
                        const activeProduct = typeof playerModule.getMediaProduct === "function"
                            ? playerModule.getMediaProduct()
                            : null;
                        if (!activeProduct || String(activeProduct.productId) !== mediaProduct.productId) {
                            const payloadObject = payloadValue && typeof payloadValue === "object" ? payloadValue : {};
                            const rawPosition = payloadObject.asset_position == null ? 0 : Number(payloadObject.asset_position);
                            const assetPosition = Number.isFinite(rawPosition) && rawPosition >= 0 ? rawPosition : 0;
                            await playerModule.load(mediaProduct, assetPosition);
                            tidalSdkMediaProduct = mediaProduct;
                            tidalSdkLoadedProductId = mediaProduct.productId;
                        } else if (!tidalSdkLoadedProductId) {
                            tidalSdkLoadedProductId = mediaProduct.productId;
                        }
                        renderSdkState();
                        return loadSdkPayload();
                    } catch (error) {
                        tidalSdkLastError = String(error && error.message ? error.message : error);
                        return null;
                    } finally {
                        tidalSdkPrimePromise = null;
                    }
                })();
                return tidalSdkPrimePromise;
            };

            const startTidalSdkMediaFromUserGesture = () => {
                if (!tidalPlayerModule || !tidalSdkLoadedProductId || typeof tidalPlayerModule.getMediaElement !== "function") {
                    return false;
                }
                const mediaElement = tidalPlayerModule.getMediaElement();
                if (!mediaElement || typeof mediaElement.play !== "function") {
                    return false;
                }
                try {
                    const playPromise = mediaElement.play();
                    tidalSdkPlaybackState = "PLAYING";
                    renderSdkState();
                    if (playPromise && typeof playPromise.catch === "function") {
                        playPromise.catch((error) => {
                            tidalSdkLastError = String(error && error.message ? error.message : error);
                            tidalSdkPlaybackState = "NOT_PLAYING";
                            renderSdkState();
                        });
                    }
                    return true;
                } catch (error) {
                    tidalSdkLastError = String(error && error.message ? error.message : error);
                    tidalSdkPlaybackState = "NOT_PLAYING";
                    renderSdkState();
                    return false;
                }
            };

            const renderSdkState = () => {
                const sdkPayload = loadSdkPayload();
                if (!sdkPayload.tidal_track_id && !sdkPayload.title && !sdkPayload.track) {
                    return;
                }
                const infoView = mergeInfoView(loadNowPlayingView({ structuredContent: sdkPayload }));
                renderInfoView(infoView);
                lastInfoSignature = loadInfoSignature(infoView);
                lastInfoView = infoView;
                updateMediaSession(infoView);
            };

            const formatDurationText = (value) => {
                const text = String(value == null ? "" : value).trim();
                if (!text) {
                    return "-";
                }
                if (/^\\d+$/.test(text)) {
                    const totalSeconds = Number(text);
                    if (Number.isFinite(totalSeconds) && totalSeconds >= 0) {
                        const minutes = Math.floor(totalSeconds / 60);
                        const seconds = totalSeconds % 60;
                        return `${minutes}:${String(seconds).padStart(2, "0")}`;
                    }
                }
                const isoDurationMatch = /^PT(?:(\\d+)H)?(?:(\\d+)M)?(?:(\\d+(?:\\.\\d+)?)S)?$/i.exec(text);
                if (isoDurationMatch) {
                    const totalSeconds = Number(isoDurationMatch[1] || 0) * 3600
                        + Number(isoDurationMatch[2] || 0) * 60
                        + Number(isoDurationMatch[3] || 0);
                    if (Number.isFinite(totalSeconds)) {
                        const minutes = Math.floor(totalSeconds / 60);
                        const seconds = Math.floor(totalSeconds % 60);
                        return `${minutes}:${String(seconds).padStart(2, "0")}`;
                    }
                }
                return text;
            };

            const parseDurationSeconds = (value) => {
                const text = String(value == null ? "" : value).trim();
                if (/^\\d+(?:\\.\\d+)?$/.test(text)) {
                    const seconds = Number(text);
                    return Number.isFinite(seconds) && seconds >= 0 ? seconds : NaN;
                }
                const isoDurationMatch = /^PT(?:(\\d+)H)?(?:(\\d+)M)?(?:(\\d+(?:\\.\\d+)?)S)?$/i.exec(text);
                if (!isoDurationMatch) {
                    return NaN;
                }
                const seconds = Number(isoDurationMatch[1] || 0) * 3600
                    + Number(isoDurationMatch[2] || 0) * 60
                    + Number(isoDurationMatch[3] || 0);
                return Number.isFinite(seconds) && seconds >= 0 ? seconds : NaN;
            };

            const formatCountdownText = (seconds) => {
                if (!Number.isFinite(seconds) || seconds < 0) {
                    return "--";
                }
                const remainingSeconds = Math.max(0, Math.ceil(seconds));
                const minutes = Math.floor(remainingSeconds / 60);
                const displaySeconds = remainingSeconds % 60;
                return `-${minutes}:${String(displaySeconds).padStart(2, "0")}`;
            };

            const formatProgressTime = (seconds) => {
                if (!Number.isFinite(seconds) || seconds < 0) {
                    return "--:--";
                }
                return formatDurationText(String(Math.floor(seconds)));
            };

            const formatVolumeDbText = (value) => {
                const text = formatField(value);
                if (text === "-") {
                    return "-";
                }
                const percent = Number.parseFloat(text.replace("%", ""));
                if (!Number.isFinite(percent)) {
                    return text;
                }
                if (percent >= 99.5) {
                    return "-0 dB";
                }
                if (percent <= 0) {
                    return "-80 dB";
                }
                const attenuationDb = Math.max(0, Math.round(-20 * Math.log10(percent / 100)));
                return `-${attenuationDb} dB`;
            };

            let trackCountdownState = {
                durationSeconds: NaN,
                positionSeconds: 0,
                anchorMs: 0,
                isPlaying: false,
            };

            const renderTrackCountdown = () => {
                if (!trackCountdownEl) {
                    return;
                }
                const elapsedSeconds = trackCountdownState.isPlaying && trackCountdownState.anchorMs > 0
                    ? (Date.now() - trackCountdownState.anchorMs) / 1000
                    : 0;
                const remainingSeconds = trackCountdownState.durationSeconds
                    - trackCountdownState.positionSeconds
                    - elapsedSeconds;
                const countdownText = formatCountdownText(remainingSeconds);
                trackCountdownEl.textContent = countdownText;
                trackCountdownEl.setAttribute("aria-label", countdownText === "--"
                    ? "Remaining track time unavailable"
                    : `Remaining track time ${countdownText}`);
                const currentSeconds = trackCountdownState.positionSeconds + elapsedSeconds;
                const durationSeconds = trackCountdownState.durationSeconds;
                if (trackProgressEl && trackPositionEl && trackDurationEl) {
                    const hasProgress = Number.isFinite(durationSeconds) && durationSeconds > 0;
                    trackProgressEl.disabled = !hasProgress;
                    const progressPercent = hasProgress
                        ? Math.min(100, Math.max(0, currentSeconds / durationSeconds * 100))
                        : 0;
                    trackProgressEl.value = String(progressPercent);
                    trackProgressEl.style.setProperty("--wp-progress", `${progressPercent}%`);
                    trackPositionEl.textContent = formatProgressTime(currentSeconds);
                    trackDurationEl.textContent = formatProgressTime(durationSeconds);
                    trackProgressEl.setAttribute("aria-valuetext", hasProgress
                        ? `${formatProgressTime(currentSeconds)} of ${formatProgressTime(durationSeconds)}`
                        : "Track progress unavailable");
                }
            };

            const setTrackCountdown = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                const durationSeconds = parseDurationSeconds(view.duration);
                const rawPosition = Number(view.assetPosition);
                const positionSeconds = Number.isFinite(rawPosition) && rawPosition >= 0 ? rawPosition : 0;
                const statusText = String(view.status == null ? "" : view.status).trim().toLowerCase();
                trackCountdownState = {
                    durationSeconds,
                    positionSeconds,
                    anchorMs: Date.now(),
                    isPlaying: statusText === "playing",
                };
                renderTrackCountdown();
            };

            const loadStatusState = (statusValue) => {
                const normalized = String(statusValue == null ? "" : statusValue).trim().toLowerCase();
                if (normalized === "playing") {
                    return "playing";
                }
                if (normalized === "paused") {
                    return "paused";
                }
                if (normalized === "error" || normalized === "failed") {
                    return "error";
                }
                if (normalized === "loading" || normalized === "pending") {
                    return "loading";
                }
                if (!normalized || normalized === "stopped" || normalized === "unknown") {
                    return "idle";
                }
                return normalized;
            };

            const loadChipHtml = (label, value, variant = "") => {
                const text = formatField(value);
                if (text === "-") {
                    return "";
                }
                const variantAttribute = String(variant || "").trim();
                return [
                    `<span class="meta-chip"${variantAttribute ? ` data-variant="${escapeHtml(variantAttribute)}"` : ""}>`,
                    `<strong>${escapeHtml(label)}</strong>`,
                    `<span>${escapeHtml(text)}</span>`,
                    "</span>",
                ].join("");
            };

            const loadInlineMetaHtml = (label, value, variant = "") => {
                const text = formatField(value);
                if (text === "-") {
                    return "";
                }
                const variantAttribute = String(variant || "").trim();
                return [
                    `<span class="meta-inline"${variantAttribute ? ` data-variant="${escapeHtml(variantAttribute)}"` : ""}>`,
                    `<strong>${escapeHtml(label)}</strong>`,
                    `<span>${escapeHtml(text)}</span>`,
                    "</span>",
                ].join("");
            };

            const loadQualityDisplayText = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                const rawValues = [view.streamQuality, view.audioMode, view.qualityResolution];
                if (Array.isArray(view.mediaTags)) {
                    rawValues.push(...view.mediaTags);
                } else {
                    rawValues.push(view.mediaTags);
                }
                const qualityText = rawValues
                    .map((value) => formatField(value))
                    .filter((value) => value !== "-")
                    .join(" ");
                const normalizedQuality = qualityText.toLowerCase();
                const qualityLabel = /(dolby|atmos)/.test(normalizedQuality)
                    ? "Dolby Atmos"
                    : /(hi[ _-]?res|master|max|24\s*[- ]?\s*bit|96\s*[- ]?\s*khz|192\s*[- ]?\s*khz)/.test(normalizedQuality)
                    ? "Hi-Res"
                    : /(lossless|flac|alac|cd)/.test(normalizedQuality)
                    ? "Lossless"
                    : /(high|aac|320|256)/.test(normalizedQuality)
                    ? "High"
                    : "";
                return qualityLabel || qualityText || "-";
            };

            const loadAudioRouteValues = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                const sourceText = formatField(view.audioSource);
                const sinkText = formatField(view.audioSink);
                const sourceNameText = formatField(view.audioSourceName);
                const sinkNameText = formatField(view.audioSinkName);
                const interfaceText = formatField(view.audioInterface);
                const profileText = formatField(view.audioProfile);
                return {
                    sourceText,
                    sinkText,
                    sourceNameText,
                    sinkNameText,
                    interfaceText,
                    channelText: formatMetricText(view.channelCount, " ch"),
                    profileText,
                };
            };

            const loadAudioAuxMatrixHtml = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                const dspStatus = formatField(view.audioAuxDspStatus);
                const dspValue = parseJsonOrNull(String(view.audioAuxDspData || ""));
                audioScopeDspMeters = {
                    capture: Array.isArray(dspValue && dspValue.capture) ? dspValue.capture : [],
                    playback: Array.isArray(dspValue && dspValue.playback) ? dspValue.playback : [],
                };
                const dspReady = dspStatus === "ready" && dspValue && typeof dspValue === "object";
                const playbackSettings = dspReady && Array.isArray(dspValue.playback_settings)
                    ? dspValue.playback_settings
                    : [];
                audioScopeSourceLabels = Object.fromEntries(playbackSettings.map((settings, pathIndex) => [
                    `out${pathIndex + 1}`,
                    String(settings && settings.path_label || "").trim(),
                ]));
                const plot = SCOPE_PLOT;
                const laneHeight = (plot.bottom - plot.top) / SCOPE_SLOT_COUNT;
                const plotWidth = plot.right - plot.left;
                const laneHtml = Array.from({ length: SCOPE_SLOT_COUNT }, (_unused, slotIndex) => {
                    const laneTop = plot.top + slotIndex * laneHeight;
                    const laneCenterY = laneTop + laneHeight / 2;
                    const divisionHtml = Array.from({ length: SCOPE_LANE_DIVISIONS + 1 }, (_division, divisionIndex) => {
                        const y = laneTop + divisionIndex * laneHeight / SCOPE_LANE_DIVISIONS;
                        const isEdge = divisionIndex === 0 || divisionIndex === SCOPE_LANE_DIVISIONS;
                        return isEdge
                            ? ""
                            : `<line class="${divisionIndex === SCOPE_LANE_DIVISIONS / 2 ? "aux-scope-center" : "aux-response-grid"}" x1="${plot.left}" y1="${y}" x2="${plot.right}" y2="${y}" />`;
                    }).join("");
                    const tickHtml = [0.5, 0, -0.5].map((fraction) => {
                        const y = laneCenterY - fraction * laneHeight / 2;
                        return `<text class="aux-scope-axis-tick" data-scope-amplitude-tick="${fraction}" x="${plot.left - 2}" y="${y + 3}" text-anchor="end"></text>`;
                    }).join("");
                    return [
                        slotIndex > 0
                            ? `<line class="aux-response-axis${slotIndex % 2 === 0 ? " aux-scope-pair-separator" : ""}" data-scope-lane-separator="${slotIndex}" x1="${plot.left}" y1="${laneTop}" x2="${plot.right}" y2="${laneTop}" />`
                            : "",
                        divisionHtml,
                        `<text class="aux-scope-lane-level" x="${plot.left + 1}" y="${laneTop + 1}" dominant-baseline="hanging" text-anchor="start" fill="var(--wp-muted)" data-scope-level="${slotIndex}">—</text>`,
                        tickHtml,
                    ].join("");
                }).join("");
                const timeGridHtml = Array.from({ length: SCOPE_HORIZONTAL_DIVISIONS + 1 }, (_unused, divisionIndex) => {
                    const x = plot.left + divisionIndex * plotWidth / SCOPE_HORIZONTAL_DIVISIONS;
                    const isCenter = divisionIndex === SCOPE_HORIZONTAL_DIVISIONS / 2;
                    return [
                        `<line class="${isCenter ? "aux-scope-center" : "aux-response-grid"}" x1="${x}" y1="${plot.top}" x2="${x}" y2="${plot.bottom}" />`,
                        divisionIndex % 2 === 0
                            ? `<text class="aux-scope-axis-tick" data-scope-time-division="${divisionIndex}" x="${x}" y="${plot.bottom + 1}" dominant-baseline="hanging" text-anchor="middle"></text>`
                            : "",
                    ].join("");
                }).join("");
                const scopeWavesHtml = Array.from({ length: SCOPE_SLOT_COUNT }, (_unused, slotIndex) => (
                    `<polyline class="aux-scope-wave" data-scope-channel="${slotIndex}" stroke="${SCOPE_WAVE_COLOR}"><title>CH${slotIndex + 1}: live PCM waveform</title></polyline>`
                )).join("");
                return [
                    '<div class="aux-matrix-scroll" role="region" aria-label="Live playback oscilloscope">',
                    '<div class="aux-matrix-content">',
                    '<svg class="aux-response-chart" viewBox="0 0 640 347.2" role="img" aria-label="Two-trace oscilloscope for one pro-audio signal channel">',
                    '<title>One-channel Pro Audio signal path: IN_SOURCE_1 to OUT_SINK_1</title>',
                    '<g transform="scale(1 0.8)">',
                    '<text class="aux-response-axis-label" x="320" y="10" text-anchor="middle" data-scope-status></text>',
                    timeGridHtml,
                    laneHtml,
                    `<rect class="aux-scope-frame" x="${plot.left}" y="${plot.top}" width="${plotWidth}" height="${plot.bottom - plot.top}" />`,
                    scopeWavesHtml,
                    "</g>",
                    "</svg>",
                    "</div>",
                    "</div>",
                ].join("");
            };

            const loadAudioScopeSourceLabel = (source) => {
                const channelIndex = Number(source && source.channel);
                if (source && Number.isInteger(channelIndex) && channelIndex >= 0) {
                    if (source.kind === "input") {
                        return `IN_SOURCE_${channelIndex + 1}`;
                    }
                    if (source.kind === "output") {
                        return `OUT_SINK_${channelIndex + 1} (Out ${channelIndex + SCOPE_OUTPUT_DISPLAY_CHANNEL_START})`;
                    }
                }
                return String(source && (source.label || source.id) || "Aus");
            };

            const loadAudioScopeWaveColor = (slotIndex) => {
                const sourceId = scopeSettings.channels[slotIndex] || "off";
                const source = audioScopeSources.find((entry) => entry.id === sourceId);
                if (!source) {
                    return SCOPE_WAVE_COLOR;
                }
                const channelIndex = Number(source.channel);
                if (!Number.isInteger(channelIndex) || channelIndex < 0) {
                    return SCOPE_WAVE_COLOR;
                }
                const levelRows = source.kind === "input"
                    ? audioScopeDspMeters.capture
                    : source.kind === "output"
                        ? audioScopeDspMeters.playback
                        : [];
                const levelRow = levelRows[channelIndex];
                const peakDb = levelRow && levelRow.peak_db;
                return typeof peakDb === "number" && Number.isFinite(peakDb) && peakDb > 0
                    ? SCOPE_OVERLOAD_COLOR
                    : SCOPE_WAVE_COLOR;
            };

            const loadAudioScopeSourceText = (source) => {
                const pathLabel = audioScopeSourceLabels[source.id] || "";
                const sourceLabel = loadAudioScopeSourceLabel(source);
                return pathLabel ? `${sourceLabel} · ${pathLabel}` : sourceLabel;
            };

            const loadAudioScopeSourceOptionsHtml = (slotIndex) => {
                const selectedId = scopeSettings.channels[slotIndex] || "off";
                return [
                    `<option value="off"${selectedId === "off" ? " selected" : ""}>Aus</option>`,
                    ...audioScopeSources.map((source) => (
                        `<option value="${escapeHtml(source.id)}"${source.id === selectedId ? " selected" : ""}>${escapeHtml(loadAudioScopeSourceText(source))}</option>`
                    )),
                ].join("");
            };

            const loadAudioScopeControlsHtml = () => {
                const timebaseTicksHtml = SCOPE_TIMEBASE_STEPS_MS.map((_step, stepIndex) => `<option value="${stepIndex}"></option>`).join("");
                const sourceHtml = SCOPE_CHANNEL_PAIRS.map((pair) => [
                    `<div class="scope-source-pair" role="group" aria-label="${pair.label}">`,
                    `<span class="scope-source-pair-label">${pair.label}</span>`,
                    ...pair.slots.map((slotIndex) => [
                        `<label class="scope-source" style="--scope-color: ${SCOPE_COLORS[slotIndex % SCOPE_COLORS.length]}">`,
                        `<span>CH${slotIndex + 1}</span>`,
                        `<select data-scope-source="${slotIndex}" aria-label="Quelle CH${slotIndex + 1}">${loadAudioScopeSourceOptionsHtml(slotIndex)}</select>`,
                        "</label>",
                    ].join("")),
                    "</div>",
                ].join("")).join("");
                return [
                    '<details class="scope-settings" data-scope-settings>',
                    '<summary class="scope-settings-toggle" aria-label="Oszilloskop-Einstellungen öffnen" aria-controls="scope-settings-panel" title="Oszilloskop-Einstellungen">',
                    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">',
                    '<path d="M19.14 12.94c.04-.31.06-.62.06-.94s-.02-.63-.07-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.3 7.3 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.49-.42h-3.76a.5.5 0 0 0-.49.42l-.36 2.54c-.6.23-1.15.55-1.63.94l-2.39-.96a.5.5 0 0 0-.61.22L2.72 8.84a.5.5 0 0 0 .12.64l2.03 1.58a6 6 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32a.5.5 0 0 0 .61.22l2.39-.96c.48.39 1.03.71 1.63.94l.36 2.54a.5.5 0 0 0 .49.42h3.76a.5.5 0 0 0 .49-.42l.36-2.54c.6-.23 1.15-.55 1.63-.94l2.39.96a.5.5 0 0 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58ZM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z"/>',
                    '</svg>',
                    '</summary>',
                    '<div id="scope-settings-panel" class="scope-controls scope-settings-panel" data-scope-controls>',
                    '<div class="scope-control-row" role="group" aria-label="Horizontal">',
                    '<span class="scope-control-label">HORIZONTAL</span>',
                    '<button type="button" class="scope-step" data-scope-action="time-minus" aria-label="Zeitbasis verkürzen">t−</button>',
                    `<input class="scope-timebase" type="range" min="0" max="${SCOPE_TIMEBASE_STEPS_MS.length - 1}" step="1" value="${scopeSettings.timebaseIndex}" list="scope-timebase-ticks" data-scope-timebase aria-label="Zeitbasis">`,
                    `<datalist id="scope-timebase-ticks">${timebaseTicksHtml}</datalist>`,
                    '<button type="button" class="scope-step" data-scope-action="time-plus" aria-label="Zeitbasis verlängern">t+</button>',
                    '<output class="scope-value" data-scope-timebase-label></output>',
                    "</div>",
                    '<div class="scope-control-row" role="group" aria-label="Vertikal">',
                    '<span class="scope-control-label">VERTIKAL</span>',
                    '<button type="button" class="scope-step" data-scope-action="vertical-minus" aria-label="Vertikale Verstärkung verringern">−</button>',
                    '<output class="scope-value" data-scope-vertical-label></output>',
                    '<button type="button" class="scope-step" data-scope-action="vertical-plus" aria-label="Vertikale Verstärkung erhöhen">+</button>',
                    '<span class="scope-unit" role="group" aria-label="Vertikale Einheit">',
                    '<button type="button" data-scope-unit="V" aria-pressed="true" title="Linear, normiert auf Full Scale (1 V ≙ 0 dBFS)">V</button>',
                    '<button type="button" data-scope-unit="dB" aria-pressed="false" title="Logarithmisch in dBFS">dB</button>',
                    "</span>",
                    "</div>",
                    `<div class="scope-control-row scope-sources" role="group" aria-label="Kanäle">${sourceHtml}</div>`,
                    "</div>",
                    "</details>",
                ].join("");
            };

            const loadAudioScopeWindowMs = () => (
                SCOPE_TIMEBASE_STEPS_MS[scopeSettings.timebaseIndex] * SCOPE_HORIZONTAL_DIVISIONS
            );

            const loadAudioScopeNumberText = (value) => String(Number(Number(value).toPrecision(3)));

            const loadAudioScopeTimebaseText = () => {
                const timebaseMs = SCOPE_TIMEBASE_STEPS_MS[scopeSettings.timebaseIndex];
                return timebaseMs < 1
                    ? `${loadAudioScopeNumberText(timebaseMs * 1000)} µs/div`
                    : `${loadAudioScopeNumberText(timebaseMs)} ms/div`;
            };

            const loadAudioScopeVerticalText = () => (
                scopeSettings.unit === "dB"
                    ? `${SCOPE_DB_STEPS[scopeSettings.dbIndex]} dB/div`
                    : `${loadAudioScopeNumberText(SCOPE_VOLT_STEPS[scopeSettings.voltIndex])} V/div`
            );

            const loadAudioScopeAmplitudeText = (fraction) => {
                if (scopeSettings.unit === "dB") {
                    const dbPerDivision = SCOPE_DB_STEPS[scopeSettings.dbIndex];
                    const rangeDb = dbPerDivision * SCOPE_LANE_DIVISIONS / 2;
                    return fraction === 0 ? `${rangeDb}` : loadAudioScopeNumberText(rangeDb * (1 - Math.abs(fraction)));
                }
                const value = Math.abs(fraction * SCOPE_VOLT_STEPS[scopeSettings.voltIndex] * SCOPE_LANE_DIVISIONS / 2);
                return value === 0 ? "0" : loadAudioScopeNumberText(value);
            };

            // Maps a full-scale sample onto the lane in half-lane units (±1 = lane edge).
            const loadAudioScopeLaneValue = (value) => {
                const sample = Number(value);
                if (!Number.isFinite(sample)) {
                    return 0;
                }
                if (scopeSettings.unit === "dB") {
                    const magnitude = Math.abs(sample);
                    if (!(magnitude > 0)) {
                        return 0;
                    }
                    const rangeDb = SCOPE_DB_STEPS[scopeSettings.dbIndex] * SCOPE_LANE_DIVISIONS / 2;
                    const level = Math.max(0, 1 + 20 * Math.log10(magnitude) / rangeDb);
                    return Math.sign(sample) * Math.min(1.05, level);
                }
                const fullScale = SCOPE_VOLT_STEPS[scopeSettings.voltIndex] * SCOPE_LANE_DIVISIONS / 2;
                return Math.max(-1.05, Math.min(1.05, sample / fullScale));
            };

            const syncAudioScopeSourceOptions = () => {
                if (!auxMatrixEl) {
                    return;
                }
                const signature = JSON.stringify([audioScopeSources, audioScopeSourceLabels]);
                if (signature === audioScopeSourcesSignature) {
                    return;
                }
                audioScopeSourcesSignature = signature;
                auxMatrixEl.querySelectorAll("[data-scope-source]").forEach((select) => {
                    select.innerHTML = loadAudioScopeSourceOptionsHtml(Number(select.dataset.scopeSource));
                });
            };

            const applyAudioScopeControls = () => {
                if (!auxMatrixEl) {
                    return;
                }
                const timebaseInput = auxMatrixEl.querySelector("[data-scope-timebase]");
                if (timebaseInput) {
                    timebaseInput.value = String(scopeSettings.timebaseIndex);
                }
                const timebaseLabel = auxMatrixEl.querySelector("[data-scope-timebase-label]");
                if (timebaseLabel) {
                    timebaseLabel.textContent = loadAudioScopeTimebaseText();
                }
                const verticalLabel = auxMatrixEl.querySelector("[data-scope-vertical-label]");
                if (verticalLabel) {
                    verticalLabel.textContent = loadAudioScopeVerticalText();
                }
                auxMatrixEl.querySelectorAll("[data-scope-unit]").forEach((button) => {
                    button.setAttribute("aria-pressed", String(button.dataset.scopeUnit === scopeSettings.unit));
                });
                auxMatrixEl.querySelectorAll("[data-scope-source]").forEach((select) => {
                    select.value = scopeSettings.channels[Number(select.dataset.scopeSource)] || "off";
                });
                const timebaseMs = SCOPE_TIMEBASE_STEPS_MS[scopeSettings.timebaseIndex];
                auxMatrixEl.querySelectorAll("[data-scope-time-division]").forEach((tick) => {
                    tick.textContent = loadAudioScopeNumberText(Number(tick.dataset.scopeTimeDivision) * timebaseMs);
                });
                auxMatrixEl.querySelectorAll("[data-scope-amplitude-tick]").forEach((tick) => {
                    tick.textContent = loadAudioScopeAmplitudeText(Number(tick.dataset.scopeAmplitudeTick));
                });
            };

            const updateAudioScopeStatus = () => {
                const scopeStatus = auxMatrixEl?.querySelector("[data-scope-status]");
                if (scopeStatus) {
                    scopeStatus.textContent = audioScopeStatusText;
                }
            };

            const renderAudioScopePayload = (payload) => {
                if (payload && Array.isArray(payload.sources) && payload.sources.length) {
                    audioScopeSources = [
                        ...payload.sources.filter((source) => source && source.kind === "input"),
                        ...payload.sources.filter((source) => source && source.kind === "output"),
                    ];
                    syncAudioScopeSourceOptions();
                }
                if (!payload || payload.ok !== true || !Array.isArray(payload.channels)) {
                    const error = String(payload && payload.error || "scope_data_unavailable");
                    const messages = {
                        pactl_unavailable: "pactl nicht verfügbar",
                        parec_unavailable: "parec nicht verfügbar",
                        playback_monitor_not_configured: "Playback-Monitor nicht konfiguriert",
                        playback_monitor_source_unavailable: "Playback-Monitor nicht verfügbar",
                        playback_monitor_channel_count_mismatch: "Kanalzahl des Playback-Monitors stimmt nicht überein",
                        playback_input_not_configured: "Scarlett-Eingang nicht konfiguriert",
                        playback_input_source_unavailable: "Scarlett-Eingang nicht verfügbar",
                        waiting_for_pcm_samples: "",
                    };
                    audioScopeStatusText = messages[error] ?? `Oszilloskop: ${error}`;
                    updateAudioScopeStatus();
                    return;
                }

                const channels = payload.channels;
                const frameCount = Math.max(0, ...channels.map((channel) => (
                    Array.isArray(channel && channel.samples)
                        ? channel.samples.length
                        : Number(channel && channel.frame_count) || 0
                )));
                if (frameCount < 2) {
                    audioScopeStatusText = "";
                    updateAudioScopeStatus();
                    return;
                }

                const plot = SCOPE_PLOT;
                const plotWidth = plot.right - plot.left;
                const laneHeight = (plot.bottom - plot.top) / SCOPE_SLOT_COUNT;
                const windowMs = loadAudioScopeWindowMs();
                const fallbackRate = Number(payload.sample_rate_hz) || 48000;
                const loadPoint = (timeMs, value, laneCenterY) => {
                    const x = plot.left + Math.min(1, Math.max(0, timeMs / windowMs)) * plotWidth;
                    const y = laneCenterY - loadAudioScopeLaneValue(value) * laneHeight / 2;
                    return `${x.toFixed(2)},${y.toFixed(2)}`;
                };
                auxMatrixEl?.querySelectorAll("[data-scope-channel]").forEach((waveform) => {
                    const slotIndex = Number(waveform.dataset.scopeChannel);
                    const channel = channels.find((entry) => Number(entry && entry.index) === slotIndex);
                    const laneCenterY = plot.top + (slotIndex + 0.5) * laneHeight;
                    const sampleRate = Number(channel && channel.sample_rate_hz) || fallbackRate;
                    const points = [];
                    if (channel && Array.isArray(channel.samples)) {
                        channel.samples.forEach((sample, sampleIndex) => {
                            points.push(loadPoint(sampleIndex * 1000 / sampleRate, sample, laneCenterY));
                        });
                    } else if (channel && Array.isArray(channel.min) && Array.isArray(channel.max)) {
                        const bucketCount = Math.min(channel.min.length, channel.max.length);
                        const bucketFrames = (Number(channel.frame_count) || bucketCount) / Math.max(1, bucketCount);
                        for (let bucketIndex = 0; bucketIndex < bucketCount; bucketIndex += 1) {
                            const timeMs = (bucketIndex + 0.5) * bucketFrames * 1000 / sampleRate;
                            const pair = bucketIndex % 2 === 0
                                ? [channel.min[bucketIndex], channel.max[bucketIndex]]
                                : [channel.max[bucketIndex], channel.min[bucketIndex]];
                            pair.forEach((value) => points.push(loadPoint(timeMs, value, laneCenterY)));
                        }
                    }
                    waveform.setAttribute("stroke", loadAudioScopeWaveColor(slotIndex));
                    waveform.setAttribute("points", points.join(" "));
                    const levelText = auxMatrixEl.querySelector(`[data-scope-level="${slotIndex}"]`);
                    if (levelText) {
                        const rmsDb = channel ? Number(channel.rms_db) : NaN;
                        levelText.textContent = channel && channel.rms_db !== null && Number.isFinite(rmsDb)
                            ? `${rmsDb.toFixed(1)} dB`
                            : "—";
                    }
                });

                audioScopeStatusText = "";
                updateAudioScopeStatus();
            };

            const loadAudioScopeRequest = () => ({
                channels: scopeSettings.channels.join(","),
                window_ms: loadAudioScopeWindowMs(),
                points: SCOPE_POINT_COUNT,
                trigger: "rising",
            });

            const loadAudioScopeToolPayload = async () => {
                const toolResult = await callServerToolRaw("webplayer_playback_scope", loadAudioScopeRequest());
                const resultPayload = toolResult && typeof toolResult === "object" && toolResult.result
                    && typeof toolResult.result === "object"
                    ? toolResult.result
                    : toolResult;
                if (resultPayload && typeof resultPayload.structuredContent === "object" && resultPayload.structuredContent) {
                    return resultPayload.structuredContent;
                }
                const textItem = Array.isArray(resultPayload && resultPayload.content)
                    ? resultPayload.content.find((item) => item && item.type === "text")
                    : null;
                const parsedPayload = textItem ? parseJsonOrNull(String(textItem.text || "")) : null;
                if (!parsedPayload || typeof parsedPayload !== "object") {
                    throw new Error("scope_tool_empty_response");
                }
                return parsedPayload;
            };

            const loadAudioScopeHttpPayload = async () => {
                const scopeUrl = new URL(UI_SCOPE_PATH, MCP_HTTP_ORIGIN || window.location.origin);
                Object.entries(loadAudioScopeRequest()).forEach(([key, value]) => {
                    scopeUrl.searchParams.set(key, String(value));
                });
                let response = null;
                let payload = null;
                let responseError = "scope_empty_response";
                for (let attempt = 0; attempt < 3; attempt += 1) {
                    response = await fetch(scopeUrl.href, { cache: "no-store" });
                    const responseText = await response.text();
                    if (!responseText.trim()) {
                        responseError = `scope_empty_response_http_${response.status}`;
                    } else {
                        try {
                            payload = JSON.parse(responseText);
                        } catch (_) {
                            responseError = `scope_invalid_json_http_${response.status}`;
                        }
                        if (payload !== null) {
                            break;
                        }
                    }
                    if (!response.ok || attempt === 2) {
                        if (response.status === 403) {
                            responseError = `scope_forbidden_origin_http_403_${window.location.origin}`;
                        }
                        throw new Error(responseError);
                    }
                    await new Promise((resolve) => window.setTimeout(resolve, 100));
                }
                if (!response || !response.ok) {
                    if (response && response.status === 403) {
                        throw new Error(`scope_forbidden_origin_http_403_${window.location.origin}`);
                    }
                    throw new Error(String(payload && payload.error || responseError));
                }
                return payload;
            };

            const loadAudioScopeTransport = () => {
                // Webview hosts (VS Code chat) resolve relative URLs against vscode-webview://
                // and block them, so scope frames must travel through the MCP App host bridge.
                if (appHostConnected && app && typeof app.callServerTool === "function") {
                    return "tool";
                }
                const scopeOrigin = String(MCP_HTTP_ORIGIN || window.location.origin || "");
                return /^https?:\/\//i.test(scopeOrigin) ? "http" : "tool";
            };

            const refreshAudioScope = async () => {
                if (scopeRequestPending || !auxMatrixEl || document.hidden) {
                    return;
                }
                scopeRequestPending = true;
                const startedAt = performance.now();
                let retryDelayMs = null;
                try {
                    const payload = loadAudioScopeTransport() === "tool"
                        ? await loadAudioScopeToolPayload()
                        : await loadAudioScopeHttpPayload();
                    if (!scopePollingActive || document.hidden) {
                        return;
                    }
                    if (payload && payload.ok === true) {
                        lastAudioScopePayload = payload;
                    } else {
                        retryDelayMs = 250;
                    }
                    scopePendingPayload = payload;
                    if (scopeRenderFrameId === null) {
                        scopeRenderFrameId = window.requestAnimationFrame(() => {
                            scopeRenderFrameId = null;
                            const latestPayload = scopePendingPayload;
                            scopePendingPayload = null;
                            if (scopePollingActive && !document.hidden) {
                                renderAudioScopePayload(latestPayload);
                            }
                        });
                    }
                } catch (error) {
                    if (!scopePollingActive || document.hidden) {
                        return;
                    }
                    audioScopeStatusText = `Oszilloskop nicht verfügbar: ${String(error && error.message || error)}`;
                    updateAudioScopeStatus();
                    retryDelayMs = 500;
                } finally {
                    scopeRequestPending = false;
                    if (scopePollingActive && !document.hidden) {
                        const delayMs = retryDelayMs ?? Math.max(
                            0, SCOPE_FRAME_INTERVAL_MS - (performance.now() - startedAt)
                        );
                        scopePollTimeoutId = window.setTimeout(() => {
                            scopePollTimeoutId = null;
                            void refreshAudioScope();
                        }, delayMs);
                    }
                }
            };

            const stopAudioScopePolling = () => {
                scopePollingActive = false;
                if (scopePollTimeoutId !== null) {
                    window.clearTimeout(scopePollTimeoutId);
                    scopePollTimeoutId = null;
                }
                if (scopeRenderFrameId !== null) {
                    window.cancelAnimationFrame(scopeRenderFrameId);
                    scopeRenderFrameId = null;
                }
                scopePendingPayload = null;
            };

            const syncAudioScopePolling = () => {
                if (!auxMatrixEl) {
                    return;
                }
                if (!document.hidden) {
                    scopePollingActive = true;
                    if (scopePollTimeoutId === null && !scopeRequestPending) {
                        void refreshAudioScope();
                    }
                } else {
                    stopAudioScopePolling();
                }
            };
            document.addEventListener("visibilitychange", syncAudioScopePolling);

            const syncAudioScopeTextSize = () => {
                if (!auxMatrixEl) {
                    return;
                }
                const chartEl = auxMatrixEl.querySelector(".aux-response-chart");
                const scopeText = chartEl && chartEl.querySelector("text");
                const transform = scopeText && scopeText.getScreenCTM();
                if (!chartEl || !transform) {
                    return;
                }
                const scale = Math.hypot(transform.c, transform.d);
                if (!Number.isFinite(scale) || scale <= 0) {
                    return;
                }
                const referenceText = document.querySelector(".header-track-artist, .audio-pipe-meta");
                const referenceFontSize = referenceText
                    ? Number.parseFloat(window.getComputedStyle(referenceText).fontSize)
                    : 10;
                if (!Number.isFinite(referenceFontSize) || referenceFontSize <= 0) {
                    return;
                }
                const scopeFontSize = Math.max(1, referenceFontSize - 1);
                chartEl.style.setProperty("--aux-scope-font-size", `${scopeFontSize / scale}px`);
            };

            const audioScopeTextResizeObserver = typeof ResizeObserver === "function"
                ? new ResizeObserver(syncAudioScopeTextSize)
                : null;
            if (audioScopeTextResizeObserver && auxMatrixEl) {
                audioScopeTextResizeObserver.observe(auxMatrixEl);
            } else {
                window.addEventListener("resize", syncAudioScopeTextSize, { passive: true });
            }

            const renderAudioScopeView = (view) => {
                if (!auxMatrixEl) {
                    return;
                }
                // Controls are created once so open selects and slider drags survive metadata refreshes.
                if (!auxMatrixEl.querySelector("[data-scope-controls]")) {
                    auxMatrixEl.innerHTML = `${loadAudioScopeControlsHtml()}<div class="aux-scope-chart" data-scope-chart></div>`;
                    lastAudioScopeChartHtml = "";
                    audioScopeSourcesSignature = "";
                }
                const chartHtml = loadAudioAuxMatrixHtml(view);
                const chartEl = auxMatrixEl.querySelector("[data-scope-chart]");
                if (chartEl && chartHtml !== lastAudioScopeChartHtml) {
                    chartEl.innerHTML = chartHtml;
                    lastAudioScopeChartHtml = chartHtml;
                    syncAudioScopeTextSize();
                }
                syncAudioScopeSourceOptions();
                applyAudioScopeControls();
                if (lastAudioScopePayload) {
                    renderAudioScopePayload(lastAudioScopePayload);
                }
                syncAudioScopePolling();
            };

            const updateAudioScopeSettings = (changes) => {
                scopeSettings = { ...scopeSettings, ...changes };
                applyAudioScopeControls();
                if (lastAudioScopePayload) {
                    renderAudioScopePayload(lastAudioScopePayload);
                }
            };

            const loadAudioScopeStepIndex = (index, steps) => Math.max(0, Math.min(steps.length - 1, index));

            const handleAudioScopeControlEvent = (event) => {
                const target = event.target;
                if (!target || typeof target.closest !== "function") {
                    return;
                }
                if (event.type === "click") {
                    const unitButton = target.closest("[data-scope-unit]");
                    if (unitButton) {
                        updateAudioScopeSettings({ unit: unitButton.dataset.scopeUnit === "dB" ? "dB" : "V" });
                        return;
                    }
                    const actionButton = target.closest("[data-scope-action]");
                    const action = actionButton ? actionButton.dataset.scopeAction : "";
                    const verticalKey = scopeSettings.unit === "dB" ? "dbIndex" : "voltIndex";
                    const verticalSteps = scopeSettings.unit === "dB" ? SCOPE_DB_STEPS : SCOPE_VOLT_STEPS;
                    const changes = {
                        "time-minus": { timebaseIndex: loadAudioScopeStepIndex(scopeSettings.timebaseIndex - 1, SCOPE_TIMEBASE_STEPS_MS) },
                        "time-plus": { timebaseIndex: loadAudioScopeStepIndex(scopeSettings.timebaseIndex + 1, SCOPE_TIMEBASE_STEPS_MS) },
                        "vertical-minus": { [verticalKey]: loadAudioScopeStepIndex(scopeSettings[verticalKey] - 1, verticalSteps) },
                        "vertical-plus": { [verticalKey]: loadAudioScopeStepIndex(scopeSettings[verticalKey] + 1, verticalSteps) },
                    }[action];
                    if (changes) {
                        updateAudioScopeSettings(changes);
                    }
                    return;
                }
                if (event.type === "input" && target.matches("[data-scope-timebase]")) {
                    updateAudioScopeSettings({
                        timebaseIndex: loadAudioScopeStepIndex(Number(target.value) || 0, SCOPE_TIMEBASE_STEPS_MS),
                    });
                    return;
                }
                if (event.type === "change" && target.matches("[data-scope-source]")) {
                    const channels = scopeSettings.channels.slice();
                    channels[Number(target.dataset.scopeSource)] = String(target.value || "off");
                    updateAudioScopeSettings({ channels });
                }
            };
            ["click", "input", "change"].forEach((eventName) => {
                auxMatrixEl?.addEventListener(eventName, handleAudioScopeControlEvent);
            });

            const loadAudioPipeDeviceText = (audioRoute) => {
                const interfaceText = formatField(
                    audioRoute.interfaceText === "-" ? audioRoute.sinkText : audioRoute.interfaceText
                );
                const profileText = formatField(audioRoute.profileText);
                if (interfaceText === "-" && profileText === "-") {
                    return "";
                }
                const normalizedInterfaceText = /scarlett/i.test(interfaceText) && /2i4/i.test(interfaceText)
                    ? interfaceText.replace(/\\bUSB\\b/i, "USB0")
                    : interfaceText;
                const deviceParts = normalizedInterfaceText === "-"
                    ? []
                    : [normalizedInterfaceText];
                if (
                    profileText !== "-"
                    && !normalizedInterfaceText.toLocaleLowerCase().includes(profileText.toLocaleLowerCase())
                ) {
                    deviceParts.push(profileText);
                }
                return deviceParts.join(" ");
            };

            const loadDetailHtml = (label, value) => {
                const text = formatField(value);
                if (text === "-") {
                    return "";
                }
                return [
                    "<div>",
                    `<span class="detail-label">${escapeHtml(label)}</span>`,
                    `<span class="detail-value">${escapeHtml(text)}</span>`,
                    "</div>",
                ].join("");
            };

            const loadTidalUrl = (value) => {
                const candidate = String(value || "").trim();
                if (!candidate) {
                    return "";
                }
                try {
                    const parsedUrl = new URL(candidate);
                    return /^https?:$/i.test(parsedUrl.protocol) && /(^|\.)tidal\.com$/i.test(parsedUrl.hostname)
                        ? parsedUrl.toString()
                        : "";
                } catch (_) {
                    return "";
                }
            };

            const loadTidalResourceUrl = (resourceValue, resourceType) => {
                const resource = resourceValue && typeof resourceValue === "object" && !Array.isArray(resourceValue)
                    ? resourceValue
                    : {};
                const directUrl = loadTidalUrl(
                    typeof resourceValue === "string"
                        ? resourceValue
                        : resource.url || resource.href || resource.external_url || resource.externalUrl
                );
                if (directUrl) {
                    return directUrl;
                }
                const resourceId = String(
                    resource.resource_id
                    || resource.resourceId
                    || resource.tidal_track_id
                    || resource.track_id
                    || resource.id
                    || ""
                ).trim();
                const normalizedType = String(resourceType || "").trim().replace(/s$/, "");
                return resourceId && /^(artist|album|track)$/.test(normalizedType)
                    ? `https://listen.tidal.com/${normalizedType}/${encodeURIComponent(resourceId)}`
                    : "";
            };

            const loadEmbeddedLinkHtml = (label, href, className = "embedded-link") => {
                const text = formatField(label);
                if (text === "-") {
                    return "";
                }
                const safeUrl = loadTidalUrl(href);
                if (!safeUrl) {
                    return escapeHtml(text);
                }
                return `<a class="${escapeHtml(className)}" data-open-link="true" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
            };

            const loadMediaMetadataArtwork = (artworkUrl) => {
                const imageUrl = String(artworkUrl || "").trim();
                if (!imageUrl) {
                    return [];
                }
                return [
                    { src: imageUrl, sizes: "96x96", type: "image/jpeg" },
                    { src: imageUrl, sizes: "192x192", type: "image/jpeg" },
                ];
            };

            const updateMediaSession = (viewObject) => {
                if (typeof navigator === "undefined" || !("mediaSession" in navigator)) {
                    return;
                }
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                const normalizedStatus = loadStatusState(view.status);
                try {
                    if ("playbackState" in navigator.mediaSession) {
                        navigator.mediaSession.playbackState = normalizedStatus === "playing"
                            ? "playing"
                            : normalizedStatus === "paused"
                                ? "paused"
                                : "none";
                    }
                } catch (_) {
                    // Ignore host media-session failures.
                }

                if (typeof MediaMetadata !== "function") {
                    return;
                }

                const titleText = formatField(view.track);
                const artistText = formatField(view.artist);
                const albumText = formatField(view.album);
                const artworkUrl = String(view.coverUrl || "").trim();
                try {
                    navigator.mediaSession.metadata = new MediaMetadata({
                        title: titleText === "-" ? "TIDAL WebPlayer" : titleText,
                        artist: artistText === "-" ? undefined : artistText,
                        album: albumText === "-" ? undefined : albumText,
                        artwork: loadMediaMetadataArtwork(artworkUrl),
                    });
                } catch (_) {
                    // Best-effort metadata only.
                }
            };

            const clearMediaSession = () => {
                if (typeof navigator === "undefined" || !("mediaSession" in navigator)) {
                    return;
                }
                try {
                    if ("playbackState" in navigator.mediaSession) {
                        navigator.mediaSession.playbackState = "none";
                    }
                    navigator.mediaSession.metadata = null;
                } catch (_) {
                    // Best-effort clear only.
                }
            };

            const loadTimestampText = () => {
                const now = new Date();
                return `${now.toLocaleDateString("de-DE")} ${now.toLocaleTimeString("de-DE")}`;
            };

            const updateStatusPill = (_statusValue, _fallbackText) => {};

            const updateDocumentTitle = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                const trackText = formatField(view.track);
                const artistText = formatField(view.artist);
                const profileText = formatField(view.audioProfile) === "Pro" ? "ProAudio" : formatField(view.audioProfile);
                const titleParts = [trackText, artistText].filter((part) => part !== "-");
                const playerTitle = profileText !== "-" ? `ProAudio - TIDAL WebPlayer` : "TIDAL WebPlayer";
                document.title = titleParts.length > 0 ? `${titleParts.join(" - ")} - ${playerTitle}` : playerTitle;
            };

            const renderLoadingView = (messageText) => {
                const safeMessage = escapeHtml(String(messageText || "Loading...").trim() || "Loading...");
                closeCoverPreview();
                setTrackCountdown({});
                headerQualityEl.textContent = "";
                headerQualityEl.hidden = true;
                trackMetaEl.hidden = true;
                headerVolumeEl.textContent = "";
                headerVolumeEl.hidden = true;
                audioPipeEl.hidden = true;
                headerAudioInterfaceEl.textContent = "";
                headerAudioInterfaceEl.hidden = true;
                headerDspSeparatorEl.hidden = true;
                headerDspEl.textContent = "";
                headerDspEl.hidden = true;
                headerAudioPipeSeparatorEl.hidden = true;
                headerTrackTitleEl.textContent = "--";
                headerTrackArtistEl.textContent = "--";
                headerTrackAlbumEl.textContent = "--";
                updateStatusPill("Loading", "Loading");
                document.title = "TIDAL WebPlayer";
                setPlayerBackground("");
                if (coverEl) {
                    coverEl.removeAttribute("src");
                    coverEl.style.display = "none";
                    coverEl.alt = "Now playing cover art";
                }
                clearMediaSession();
                infoBoxEl.innerHTML = [
                    '<div class="status-row">',
                    '<span class="spinner" aria-label="loading" role="img"></span>',
                    `<span class="status-message">${safeMessage}</span>`,
                    '</div>',
                ].join("");
                syncPanelWidthToControls();
                postEmbedSize();
            };

            const renderAuthView = (payloadValue) => {
                if (!authBoxEl) {
                    return;
                }
                const payload = payloadValue && typeof payloadValue === "object" ? payloadValue : {};
                const authUrl = String(payload.authorization_url || "").trim();
                const safeAuthUrl = /^https?:\\/\\//i.test(authUrl) ? authUrl : "";
                const errorText = String(payload.error || payload.error_description || "").trim();
                const isAuthenticated = payload.status === "authenticated" || payload.authentication_state === "ready";
                const authenticationRequired = payload.authentication_state === "required" || payload.error === "user_auth_required";
                const statusText = isAuthenticated
                    ? "TIDAL account connected"
                    : authenticationRequired
                        ? "TIDAL login required"
                    : errorText
                        ? "TIDAL login failed"
                        : "TIDAL login required";
                const contentParts = [
                    '<div class="status-row"><strong>' + escapeHtml(statusText) + "</strong></div>",
                ];
                if (safeAuthUrl) {
                    contentParts.push(
                        '<a href="' + escapeHtml(safeAuthUrl) + '" target="_blank" rel="noopener">Open TIDAL login</a>',
                        '<input id="auth-callback-input" type="text" autocomplete="off" placeholder="Paste redirect URL or authorization code" aria-label="OAuth redirect URL or authorization code" />',
                        '<button type="button" data-auth-complete>Complete login</button>',
                    );
                }
                if (errorText) {
                    contentParts.push('<span class="auth-error">' + escapeHtml(errorText) + "</span>");
                } else if (isAuthenticated) {
                    contentParts.push('<span>TIDAL Web SDK authentication is ready for playback.</span>');
                } else if (authenticationRequired) {
                    contentParts.push('<span>Use Authenticate TIDAL to continue.</span>');
                }
                authBoxEl.innerHTML = contentParts.join("");
                authBoxEl.hidden = false;
                const completeButton = authBoxEl.querySelector("[data-auth-complete]");
                const callbackInput = authBoxEl.querySelector("#auth-callback-input");
                if (completeButton && callbackInput) {
                    completeButton.addEventListener("click", () => {
                        const callbackValue = String(callbackInput.value || "").trim();
                        if (!callbackValue) {
                            callbackInput.focus();
                            return;
                        }
                        const completeArguments = /^https?:\\/\\//i.test(callbackValue)
                            ? { redirect_url: callbackValue }
                            : { code: callbackValue };
                        callTool("webplayer_usrauth", completeArguments, {
                            onlyWhenChanged: true,
                            isUserAction: true,
                        });
                    });
                }
                syncPanelWidthToControls();
                postEmbedSize();
            };

            const loadInfoSignature = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                return [
                    formatField(view.status),
                    formatField(view.artist),
                    formatField(view.artistInfo),
                    formatField(view.album),
                    formatField(view.albumInfo),
                    formatField(view.trackInfo),
                    formatField(view.track),
                    formatField(view.mediaProduct),
                    formatField(view.nextMediaProduct),
                    formatField(view.playbackContext),
                    formatField(view.mediaProductType),
                    formatField(view.trackId),
                    formatField(view.duration),
                    formatField(view.volumePercent),
                    formatField(view.volumeTarget),
                    formatField(view.volumeSink),
                    String(Boolean(view.explicit)),
                    String(Boolean(view.isLiked)),
                    formatField(view.streamProvider),
                    formatField(view.streamQuality),
                    formatField(view.audioMode),
                    formatField(view.qualityResolution),
                    formatField(view.bitrateKbps),
                    formatField(view.sampleRateKhz),
                    formatField(view.bitDepth),
                    formatField(view.channelCount),
                    formatField(view.codec),
                    formatField(view.bpm),
                    formatField(view.tonart),
                    formatField(view.playbackBackend),
                    formatField(view.backend),
                    formatField(view.mediaTags),
                    formatField(view.releaseDate),
                    formatField(view.trackNumber),
                    formatField(view.volumeNumber),
                    formatField(view.version),
                    formatField(view.isrc),
                    formatField(view.copyright),
                    formatField(view.sdkPlayerVersion),
                    formatField(view.assetPresentation),
                    formatField(view.previewReason),
                    formatField(view.streamType),
                    formatField(view.playbackSessionId),
                    formatField(view.assetPosition),
                    formatField(view.bandwidthKbps),
                    formatField(view.sdkError),
                    formatField(view.audioSource),
                    formatField(view.audioSink),
                    formatField(view.audioSourceName),
                    formatField(view.audioSinkName),
                    formatField(view.audioInterface),
                    formatField(view.audioProfile),
                    formatField(view.audioAuxRouting),
                    formatField(view.audioAuxRoutingStatus),
                    formatField(view.audioAuxDspData),
                    formatField(view.audioAuxDspStatus),
                    formatField(view.dsp),
                    String(view.coverUrl || "").trim(),
                    String(view.trackUrl || "").trim(),
                ].join("|");
            };

            let lastInfoSignature = "";
            let lastInfoView = {};

            const isMeaningfulInfoValue = (value) => {
                const text = String(value == null ? "" : value).trim();
                return Boolean(text && text !== "-");
            };

            const mergeInfoView = (viewObject) => {
                const baseView = viewObject && typeof viewObject === "object" ? viewObject : {};
                const apiPreviewActive = Boolean(
                    selectedPlaybackBackend === "api"
                    && tidalApiPreviewProductId
                    && tidalApiPreviewObjectUrl
                    && tidalApiPreviewMediaElement
                );
                const apiPreviewPosition = apiPreviewActive && tidalApiPreviewMediaElement
                    ? Number(tidalApiPreviewMediaElement.currentTime)
                    : NaN;
                const apiPreviewDuration = apiPreviewActive && tidalApiPreviewMediaElement
                    ? Number(tidalApiPreviewMediaElement.duration)
                    : NaN;
                const nextView = apiPreviewActive
                    ? {
                        ...baseView,
                        status: loadSdkPlaybackStateText(tidalApiPreviewState),
                        player: "tidal-api-preview",
                        backend: "tidal-api",
                        playbackBackend: "api",
                        assetPresentation: tidalApiPreviewPresentation || baseView.assetPresentation,
                        previewReason: tidalApiPreviewReason || baseView.previewReason,
                        assetPosition: Number.isFinite(apiPreviewPosition)
                            ? apiPreviewPosition
                            : baseView.assetPosition,
                        duration: Number.isFinite(apiPreviewDuration) && apiPreviewDuration > 0
                            ? apiPreviewDuration
                            : baseView.duration,
                    }
                    : baseView;
                const previousView = lastInfoView && typeof lastInfoView === "object" ? lastInfoView : {};
                const mergedView = { ...previousView };
                const textFields = [
                    "player",
                    "status",
                    "artist",
                    "artistInfo",
                    "album",
                    "albumInfo",
                    "trackInfo",
                    "track",
                    "mediaProduct",
                    "nextMediaProduct",
                    "playbackContext",
                    "mediaProductType",
                    "trackId",
                    "duration",
                    "volumePercent",
                    "streamProvider",
                    "streamQuality",
                    "audioMode",
                    "qualityResolution",
                    "bitrateKbps",
                    "sampleRateKhz",
                    "bitDepth",
                    "channelCount",
                    "codec",
                    "bpm",
                    "tonart",
                    "playbackBackend",
                    "backend",
                    "mediaTags",
                    "releaseDate",
                    "trackNumber",
                    "volumeNumber",
                    "version",
                    "isrc",
                    "copyright",
                    "sdkPlayerVersion",
                    "assetPresentation",
                    "previewReason",
                    "streamType",
                    "playbackSessionId",
                    "assetPosition",
                    "bandwidthKbps",
                    "sdkError",
                    "audioSource",
                    "audioSink",
                    "audioSourceName",
                    "audioSinkName",
                    "audioInterface",
                    "audioProfile",
                    "audioAuxRouting",
                    "audioAuxRoutingStatus",
                    "audioAuxDspData",
                    "audioAuxDspStatus",
                    "dsp",
                    "coverUrl",
                    "trackUrl",
                ];

                for (const fieldName of textFields) {
                    if (fieldName === "coverUrl" && isMeaningfulInfoValue(previousView[fieldName])) {
                        const nextCover = String(nextView[fieldName] || "").trim();
                        const previousCover = String(previousView[fieldName] || "").trim();
                        if (!/^https?:\\/\\//i.test(nextCover) && /^https?:\\/\\//i.test(previousCover)) {
                            continue;
                        }
                    }
                    if (isMeaningfulInfoValue(nextView[fieldName]) || !isMeaningfulInfoValue(previousView[fieldName])) {
                        mergedView[fieldName] = nextView[fieldName];
                    }
                }

                if (nextView.hasLikeMetadata || !Object.prototype.hasOwnProperty.call(previousView, "isLiked")) {
                    mergedView.isLiked = Boolean(nextView.isLiked);
                }
                if (
                    previousView.isLiked === true
                    && nextView.isLiked === false
                    && previousView.trackId
                    && nextView.trackId
                    && String(previousView.trackId) === String(nextView.trackId)
                ) {
                    mergedView.isLiked = true;
                }
                if (nextView.hasExplicitMetadata || !Object.prototype.hasOwnProperty.call(previousView, "explicit")) {
                    mergedView.explicit = nextView.explicit;
                }
                mergedView.hasLikeMetadata = Boolean(nextView.hasLikeMetadata || previousView.hasLikeMetadata);
                mergedView.hasExplicitMetadata = Boolean(nextView.hasExplicitMetadata || previousView.hasExplicitMetadata);
                return mergedView;
            };

            const PLAY_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" height="20px" viewBox="0 -960 960 960" width="20px" fill="currentColor"><path d="m384-312 264-168-264-168v336Zm96.28 216Q401-96 331-126t-122.5-82.5Q156-261 126-330.96t-30-149.5Q96-560 126-629.5q30-69.5 82.5-122T330.96-834q69.96-30 149.5-30t149.04 30q69.5 30 122 82.5T834-629.28q30 69.73 30 149Q864-401 834-331t-82.5 122.5Q699-156 629.28-126q-69.73 30-149 30Zm-.28-72q130 0 221-91t91-221q0-130-91-221t-221-91q-130 0-221 91t-91 221q0 130 91 221t221 91Zm0-312Z"/></svg>';
            const PAUSE_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" height="20px" viewBox="0 -960 960 960" width="20px" fill="currentColor"><path d="M528-192v-576h240v576H528Zm-336 0v-576h240v576H192Zm408-72h96v-432h-96v432Zm-336 0h96v-432h-96v432Zm0-432v432-432Zm336 0v432-432Z"/></svg>';
            const HEART_OUTLINE_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20px" height="20px" fill="currentColor"><path d="M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3zm-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z"/></svg>';
            const HEART_FILLED_ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20px" height="20px" fill="currentColor"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';

            const renderHeartButton = (isLiked) => {
                if (!likeButtonEl) {
                    return;
                }
                likeButtonEl.innerHTML = isLiked ? HEART_FILLED_ICON_SVG : HEART_OUTLINE_ICON_SVG;
                likeButtonEl.setAttribute("data-tool", "tidal_api_add_track_to_collection");
                likeButtonEl.setAttribute("data-liked", isLiked ? "true" : "false");
                likeButtonEl.setAttribute("aria-pressed", isLiked ? "true" : "false");
                likeButtonEl.setAttribute("aria-label", isLiked ? "track in My Collection" : "add current track to tracks");
                likeButtonEl.setAttribute("title", isLiked ? "Already in My Collection Tracks" : "[API] Add current track to tracks");
            };

            const renderPlayPauseButton = (viewValue) => {
                if (!playPauseButtonEl) {
                    return;
                }

                const view = viewValue && typeof viewValue === "object" ? viewValue : {};
                const normalizedStatus = String(view.status == null ? "" : view.status).trim().toLowerCase();
                const isPlaying = normalizedStatus === "playing";
                const basePlayToolName = playPauseButtonEl.getAttribute("data-play-tool") || "webplayer_play";
                const stopToolName = playPauseButtonEl.getAttribute("data-stop-tool") || "webplayer_pause";
                const basePlayArgs = parseJsonOrNull(playPauseButtonEl.getAttribute("data-play-args") || "{}") || {};
                playPauseButtonEl.setAttribute("data-play-tool", basePlayToolName);
                playPauseButtonEl.setAttribute("data-play-args", JSON.stringify(basePlayArgs));
                playPauseButtonEl.setAttribute("data-stop-tool", stopToolName);

                if (isPlaying) {
                    playPauseButtonEl.setAttribute("data-tool", stopToolName);
                    playPauseButtonEl.setAttribute("data-args", "{}");
                    playPauseButtonEl.innerHTML = PAUSE_ICON_SVG;
                    playPauseButtonEl.setAttribute("aria-label", "pause");
                    playPauseButtonEl.setAttribute("title", "Pause playback");
                    return;
                }

                playPauseButtonEl.setAttribute("data-tool", basePlayToolName);
                playPauseButtonEl.setAttribute("data-args", JSON.stringify(basePlayArgs));
                playPauseButtonEl.innerHTML = PLAY_ICON_SVG;
                playPauseButtonEl.setAttribute("aria-label", "play");
                playPauseButtonEl.setAttribute("title", "Start playback");
            };

            const renderInfoView = (viewObject) => {
                const view = viewObject && typeof viewObject === "object" ? viewObject : {};
                setTrackCountdown(view);
                const titleText = formatField(view.track);
                const artistText = formatField(view.artist);
                const albumText = formatField(view.album);
                const displayTitleText = titleText !== "-" ? titleText : "Unknown track";
                const qualityText = loadQualityDisplayText(view);
                const qualityLevel = loadQualityLevel(qualityText);
                const toneChip = loadChipHtml("Tone", formatField(view.tonart), "accent");
                const audioQaChip = qualityText !== "-"
                    && qualityLevel !== "hi-res"
                    && qualityLevel !== "lossless"
                    ? loadInlineMetaHtml("Audio QA", qualityText, "accent")
                    : "";
                const audioRoute = loadAudioRouteValues(view);
                const coverCandidate = String(view.coverUrl || "").trim();
                const normalizedPlayerSelector = String(view.player || "tidal-sdk").trim() || "tidal-sdk";
                const playerSelector = encodeURIComponent(normalizedPlayerSelector);
                const coverSource = coverCandidate
                    ? `&source=${encodeURIComponent(coverCandidate)}`
                    : "";
                const coverProxyPath = `${UI_COVER_PATH}?player_selector=${playerSelector}${coverSource}`;
                const coverProxySrc = MCP_HTTP_ORIGIN ? `${MCP_HTTP_ORIGIN}${coverProxyPath}` : coverProxyPath;
                let effectiveCoverSrc = "";
                if (/^https?:\\/\\//i.test(coverCandidate)) {
                    effectiveCoverSrc = coverCandidate;
                } else if (/^file:\\/\\//i.test(coverCandidate)) {
                    effectiveCoverSrc = coverProxySrc;
                } else {
                    effectiveCoverSrc = coverProxySrc;
                }
                const tidalTrackUrl = loadTidalUrl(view.trackUrl);
                const artistUrl = loadTidalUrl(view.artistUrl);
                const albumUrl = loadTidalUrl(view.albumUrl);
                const artistDetailHtml = [
                    loadDetailHtml("Artist details", view.artistInfo),
                    loadDetailHtml("Album details", view.albumInfo),
                    loadDetailHtml("BPM", formatMetricText(view.bpm, " BPM")),
                    loadDetailHtml("Media tags", view.mediaTags),
                    loadDetailHtml("Release date", view.releaseDate),
                    loadDetailHtml("Track number", view.trackNumber),
                    loadDetailHtml("Volume number", view.volumeNumber),
                    loadDetailHtml("Version", view.version),
                    loadDetailHtml("ISRC", view.isrc),
                    loadDetailHtml("Copyright", view.copyright),
                    view.hasLikeMetadata ? loadDetailHtml("Liked", view.isLiked ? "Yes" : "No") : "",
                ]
                    .filter(Boolean)
                    .join("");
                const artistDetailContentHtml = artistDetailHtml;
                if (headerQualityEl) {
                    headerQualityEl.textContent = qualityText === "-" ? "" : qualityText;
                    headerQualityEl.hidden = qualityText === "-";
                    trackMetaEl.hidden = qualityText === "-";
                    if (qualityLevel) {
                        headerQualityEl.setAttribute("data-quality-level", qualityLevel);
                    } else {
                        headerQualityEl.removeAttribute("data-quality-level");
                    }
                }
                if (headerVolumeEl) {
                    const volumeText = formatVolumeDbText(view.volumePercent);
                    const headerText = volumeText === "-" ? "" : volumeText;
                    headerVolumeEl.textContent = headerText;
                    headerVolumeEl.title = headerText;
                    headerVolumeEl.hidden = !headerText;
                }
                if (headerAudioInterfaceEl) {
                    const audioDeviceText = loadAudioPipeDeviceText(audioRoute);
                    headerAudioInterfaceEl.textContent = audioDeviceText;
                    headerAudioInterfaceEl.title = audioDeviceText;
                    headerAudioInterfaceEl.hidden = !audioDeviceText;
                }
                if (headerDspEl && headerDspSeparatorEl) {
                    const dspText = formatField(view.dsp);
                    const headerText = dspText === "-" ? "" : "CamillaDsp";
                    headerDspEl.textContent = headerText;
                    headerDspEl.title = headerText;
                    headerDspEl.hidden = !headerText;
                    headerDspSeparatorEl.hidden = !headerText || headerAudioInterfaceEl.hidden;
                    headerAudioPipeSeparatorEl.hidden = !headerText && headerAudioInterfaceEl.hidden;
                    audioPipeEl.hidden = headerAudioInterfaceEl.hidden && headerDspEl.hidden;
                }
                if (headerTrackArtistEl) {
                    const displayArtistText = artistText === "-" ? "Unknown artist" : artistText;
                    headerTrackArtistEl.innerHTML = loadEmbeddedLinkHtml(displayArtistText, artistUrl);
                    headerTrackArtistEl.title = displayArtistText;
                }
                if (headerTrackAlbumEl) {
                    const displayAlbumText = albumText === "-" ? "Unknown album" : albumText;
                    headerTrackAlbumEl.innerHTML = loadEmbeddedLinkHtml(displayAlbumText, albumUrl);
                    headerTrackAlbumEl.title = displayAlbumText;
                }
                if (headerTrackTitleEl) {
                    headerTrackTitleEl.innerHTML = loadEmbeddedLinkHtml(displayTitleText, tidalTrackUrl);
                    headerTrackTitleEl.title = displayTitleText;
                }
                updateStatusPill(view.status, "Ready");
                updateDocumentTitle(view);
                renderAudioScopeView(view);
                infoBoxEl.innerHTML = [
                    `<div class="info-summary"><div class="meta-chips">${[toneChip, audioQaChip].filter(Boolean).join("")}</div></div>`,
                    artistDetailContentHtml ? `<div id="artist-details" class="details-grid artist-details">${artistDetailContentHtml}</div>` : "",
                ].join("");
                renderPlayPauseButton(view);
                renderHeartButton(Boolean(view.isLiked));

                if (coverEl) {
                    if (effectiveCoverSrc) {
                        setPlayerBackground(effectiveCoverSrc);
                        if (coverEl.getAttribute("src") !== effectiveCoverSrc) {
                            coverEl.src = effectiveCoverSrc;
                        }
                        coverEl.alt = titleText !== "-" ? `Cover art for ${titleText}` : "Now playing cover art";
                        coverEl.style.display = "block";
                    } else {
                        setPlayerBackground("");
                        coverEl.removeAttribute("src");
                        coverEl.alt = "Now playing cover art";
                        coverEl.style.display = "none";
                    }
                }

                updateMediaSession(view);
                syncPanelWidthToControls();
                postEmbedSize();
            };

            const renderErrorView = (toolName, errorText) => {
                const sanitizedMessage = escapeHtml(String(errorText || "Something went wrong. Please try again."));
                const normalizedErrorText = String(errorText || "");
                const authenticationRequired = /(api_http_401|authentication_required|\\b401\\b|unauthorized|authentication)/i.test(normalizedErrorText);
                closeCoverPreview();
                setTrackCountdown({});
                headerQualityEl.textContent = "";
                headerQualityEl.hidden = true;
                trackMetaEl.hidden = true;
                headerTrackTitleEl.textContent = "--";
                headerTrackArtistEl.textContent = "--";
                headerTrackAlbumEl.textContent = "--";
                updateStatusPill("Error", "Error");
                document.title = "TIDAL WebPlayer";
                setPlayerBackground("");
                if (coverEl) {
                    coverEl.removeAttribute("src");
                    coverEl.alt = "Now playing cover art";
                    coverEl.style.display = "none";
                }
                clearMediaSession();
                infoBoxEl.innerHTML = [
                    '<div class="status-row">',
                    `<span class="error-message">${sanitizedMessage}</span>`,
                    '<fluent-button class="retry-button" appearance="outline" type="button">Try again</fluent-button>',
                    '</div>',
                    authenticationRequired
                        ? '<div class="status-message">TIDAL user authentication is required before this account action can run.</div><button class="auth-recover" type="button" data-auth-recover>Authenticate TIDAL</button>'
                        : "",
                    `<div class="status-message">Tool: ${escapeHtml(String(toolName || "webplayer_now_playing"))}</div>`,
                ].join("");
                const retryButton = infoBoxEl.querySelector(".retry-button");
                if (retryButton) {
                    retryButton.addEventListener("click", () => {
                        renderLoadingView("Retrying...");
                        callTool(lastToolRequestName, lastToolRequestArgs, {
                            onlyWhenChanged: false,
                            isUserAction: true,
                        });
                    });
                }
                const authRecoverButton = infoBoxEl.querySelector("[data-auth-recover]");
                if (authRecoverButton) {
                    authRecoverButton.addEventListener("click", () => {
                        renderLoadingView("Starting TIDAL login...");
                        callTool("webplayer_usrauth", {}, {
                            onlyWhenChanged: false,
                            isUserAction: true,
                        });
                    });
                }
                syncPanelWidthToControls();
                postEmbedSize();
            };

            const bindMediaSessionHandlers = () => {
                if (typeof navigator === "undefined" || !("mediaSession" in navigator)) {
                    return;
                }
                const mediaSession = navigator.mediaSession;
                if (typeof mediaSession.setActionHandler !== "function") {
                    return;
                }

                const invokeTool = (toolName, args = {}) => {
                    callTool(toolName, args, {
                        onlyWhenChanged: true,
                        isUserAction: true,
                    });
                };

                const loadPlayAction = () => {
                    const toolName = playPauseButtonEl?.getAttribute("data-play-tool") || playPauseButtonEl?.getAttribute("data-tool") || "webplayer_play";
                    const args = parseJsonOrNull(playPauseButtonEl?.getAttribute("data-play-args") || playPauseButtonEl?.getAttribute("data-args") || "{}") || {};
                    return { toolName, args };
                };

                try {
                    mediaSession.setActionHandler("play", () => {
                        const { toolName, args } = loadPlayAction();
                        invokeTool(toolName, args);
                    });
                } catch (_) {
                    // Ignore unsupported action handlers.
                }
                try {
                    mediaSession.setActionHandler("pause", () => {
                        invokeTool(playPauseButtonEl?.getAttribute("data-stop-tool") || "webplayer_pause", {});
                    });
                } catch (_) {
                    // Ignore unsupported action handlers.
                }
                try {
                    mediaSession.setActionHandler("stop", () => {
                        invokeTool(playPauseButtonEl?.getAttribute("data-stop-tool") || "webplayer_stop", {});
                    });
                } catch (_) {
                    // Ignore unsupported action handlers.
                }
                try {
                    mediaSession.setActionHandler("nexttrack", () => {
                        invokeTool("webplayer_forward", {});
                    });
                } catch (_) {
                    // Ignore unsupported action handlers.
                }
                try {
                    mediaSession.setActionHandler("previoustrack", () => {
                        invokeTool("webplayer_backward", {});
                    });
                } catch (_) {
                    // Ignore unsupported action handlers.
                }
            };

            const callDirectMcpTool = async (name, args) => {
                const response = await fetch(MCP_HTTP_ENDPOINT, {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": name,
                    },
                    body: JSON.stringify({
                        jsonrpc: "2.0",
                        id: "webplayer-ui-" + Date.now() + "-" + (++callSequence),
                        method: "tools/call",
                        params: {
                            name,
                            arguments: args,
                            _meta: {
                                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                            },
                        },
                    }),
                });
                let payload = {};
                try {
                    payload = await response.json();
                } catch (_) {
                    payload = {};
                }
                if (!response.ok) {
                    throw new Error("MCP HTTP request failed (" + response.status + ")");
                }
                if (payload && payload.error) {
                    throw new Error(String(payload.error.message || payload.error.code || "MCP request failed"));
                }
                return payload;
            };

            const pendingVsCodeCalls = new Map();
            const callVsCodeTool = (name, args) => new Promise((resolve, reject) => {
                const requestId = `webplayer-ui-${Date.now()}-${++callSequence}`;
                const timeoutId = window.setTimeout(() => {
                    pendingVsCodeCalls.delete(requestId);
                    reject(new Error("VS Code MCP tool request timed out"));
                }, 30000);
                pendingVsCodeCalls.set(requestId, { resolve, reject, timeoutId });
                try {
                    vscodeApi.postMessage({ type: "webplayer-call-tool", requestId, name, arguments: args });
                } catch (error) {
                    window.clearTimeout(timeoutId);
                    pendingVsCodeCalls.delete(requestId);
                    reject(error);
                }
            });

            const callServerToolRaw = async (name, args) => {
                if (appHostConnected && app && typeof app.callServerTool === "function") {
                    return app.callServerTool({ name, arguments: args });
                }
                if (vscodeApi) {
                    return callVsCodeTool(name, args);
                }
                return callDirectMcpTool(name, args);
            };

            const handleTidalSdkEvent = (eventName, event) => {
                const detail = event && event.detail && typeof event.detail === "object" ? event.detail : {};
                if (eventName === "active-device-changed") {
                    tidalAudioOutputDeviceId = String(event && event.detail || "").trim();
                } else if (eventName === "active-device-mode-changed") {
                    tidalAudioOutputDeviceMode = String(event && event.detail || "").trim();
                } else if (eventName === "active-device-pass-through-changed") {
                    tidalAudioOutputPassThrough = typeof event.detail === "boolean" ? event.detail : null;
                } else if (eventName === "device-change") {
                    void refreshAudioOutputInfo();
                } else if (eventName === "playback-state-change") {
                    tidalSdkPlaybackState = String(detail.state || "IDLE").trim().toUpperCase() || "IDLE";
                } else if (eventName === "media-product-transition") {
                    tidalSdkMediaProduct = detail.mediaProduct && typeof detail.mediaProduct === "object"
                        ? detail.mediaProduct
                        : tidalSdkMediaProduct;
                    tidalSdkPlaybackContext = detail.playbackContext && typeof detail.playbackContext === "object"
                        ? detail.playbackContext
                        : tidalSdkPlaybackContext;
                    if (tidalSdkMediaProduct && tidalSdkMediaProduct.productId) {
                        tidalSdkLoadedProductId = String(tidalSdkMediaProduct.productId);
                    }
                    tidalSdkLastError = "";
                } else if (eventName === "playback-quality-changed") {
                    tidalSdkPlaybackContext = detail.playbackContext && typeof detail.playbackContext === "object"
                        ? detail.playbackContext
                        : tidalSdkPlaybackContext;
                } else if (eventName === "error") {
                    tidalSdkLastError = String(
                        detail.message
                        || detail.errorMessage
                        || detail.errorCode
                        || detail.errorId
                        || "TIDAL Web SDK playback error"
                    ).trim();
                } else if (eventName === "ended") {
                    tidalSdkPlaybackState = "IDLE";
                }
                setTimeout(() => {
                    try {
                        renderSdkState();
                    } catch (_) {
                        // The first SDK event can arrive before the initial view is mounted.
                    }
                }, 0);
            };

            const ensureTidalSdkWebCrypto = () => {
                const currentCrypto = typeof window.crypto === "object" ? window.crypto : null;
                if (currentCrypto && currentCrypto.subtle && typeof currentCrypto.subtle.digest === "function") {
                    return;
                }

                const loadBytes = (value) => {
                    if (value instanceof ArrayBuffer) {
                        return new Uint8Array(value);
                    }
                    if (ArrayBuffer.isView(value)) {
                        return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
                    }
                    throw new TypeError("tidal_sdk_crypto_data_invalid");
                };

                const digestSha1 = async (algorithm, value) => {
                    const algorithmName = typeof algorithm === "string" ? algorithm : algorithm && algorithm.name;
                    if (String(algorithmName || "").trim().toUpperCase() !== "SHA-1") {
                        throw new Error("tidal_sdk_crypto_digest_unsupported");
                    }

                    const input = loadBytes(value);
                    const bitLength = input.length * 8;
                    const paddedLength = ((input.length + 9 + 63) >> 6) << 6;
                    const padded = new Uint8Array(paddedLength);
                    padded.set(input);
                    padded[input.length] = 0x80;
                    const bitLengthHigh = Math.floor(bitLength / 0x100000000);
                    const bitLengthLow = bitLength >>> 0;
                    for (let index = 0; index < 4; index += 1) {
                        padded[paddedLength - 8 + index] = (bitLengthHigh >>> (24 - index * 8)) & 0xff;
                        padded[paddedLength - 4 + index] = (bitLengthLow >>> (24 - index * 8)) & 0xff;
                    }

                    const rotateLeft = (valueToRotate, shift) => (
                        (valueToRotate << shift) | (valueToRotate >>> (32 - shift))
                    ) >>> 0;
                    let h0 = 0x67452301;
                    let h1 = 0xefcdab89;
                    let h2 = 0x98badcfe;
                    let h3 = 0x10325476;
                    let h4 = 0xc3d2e1f0;
                    const words = new Uint32Array(80);

                    for (let offset = 0; offset < padded.length; offset += 64) {
                        for (let index = 0; index < 16; index += 1) {
                            const wordOffset = offset + index * 4;
                            words[index] = (
                                (padded[wordOffset] << 24)
                                | (padded[wordOffset + 1] << 16)
                                | (padded[wordOffset + 2] << 8)
                                | padded[wordOffset + 3]
                            ) >>> 0;
                        }
                        for (let index = 16; index < 80; index += 1) {
                            words[index] = rotateLeft(
                                words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16],
                                1,
                            );
                        }

                        let a = h0;
                        let b = h1;
                        let c = h2;
                        let d = h3;
                        let e = h4;
                        for (let index = 0; index < 80; index += 1) {
                            let roundFunction;
                            let roundConstant;
                            if (index < 20) {
                                roundFunction = (b & c) | ((~b) & d);
                                roundConstant = 0x5a827999;
                            } else if (index < 40) {
                                roundFunction = b ^ c ^ d;
                                roundConstant = 0x6ed9eba1;
                            } else if (index < 60) {
                                roundFunction = (b & c) | (b & d) | (c & d);
                                roundConstant = 0x8f1bbcdc;
                            } else {
                                roundFunction = b ^ c ^ d;
                                roundConstant = 0xca62c1d6;
                            }
                            const temporary = (
                                rotateLeft(a, 5)
                                + roundFunction
                                + e
                                + roundConstant
                                + words[index]
                            ) >>> 0;
                            e = d;
                            d = c;
                            c = rotateLeft(b, 30);
                            b = a;
                            a = temporary;
                        }

                        h0 = (h0 + a) >>> 0;
                        h1 = (h1 + b) >>> 0;
                        h2 = (h2 + c) >>> 0;
                        h3 = (h3 + d) >>> 0;
                        h4 = (h4 + e) >>> 0;
                    }

                    const digest = new Uint8Array(20);
                    const digestWords = [h0, h1, h2, h3, h4];
                    digestWords.forEach((word, wordIndex) => {
                        digest[wordIndex * 4] = (word >>> 24) & 0xff;
                        digest[wordIndex * 4 + 1] = (word >>> 16) & 0xff;
                        digest[wordIndex * 4 + 2] = (word >>> 8) & 0xff;
                        digest[wordIndex * 4 + 3] = word & 0xff;
                    });
                    return digest.buffer;
                };

                const nativeGetRandomValues = currentCrypto && typeof currentCrypto.getRandomValues === "function"
                    ? currentCrypto.getRandomValues.bind(currentCrypto)
                    : null;
                const getRandomValues = (target) => {
                    if (!target || !ArrayBuffer.isView(target)) {
                        throw new TypeError("tidal_sdk_crypto_random_target_invalid");
                    }
                    if (nativeGetRandomValues) {
                        return nativeGetRandomValues(target);
                    }
                    for (let index = 0; index < target.length; index += 1) {
                        target[index] = Math.floor(Math.random() * 256);
                    }
                    return target;
                };
                const createRandomUuid = () => {
                    const bytes = getRandomValues(new Uint8Array(16));
                    bytes[6] = (bytes[6] & 0x0f) | 0x40;
                    bytes[8] = (bytes[8] & 0x3f) | 0x80;
                    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
                    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
                };
                const randomUuid = currentCrypto && typeof currentCrypto.randomUUID === "function"
                    ? currentCrypto.randomUUID.bind(currentCrypto)
                    : createRandomUuid;
                const subtleCrypto = Object.create(currentCrypto && currentCrypto.subtle ? currentCrypto.subtle : null);
                subtleCrypto.digest = digestSha1;
                const fallbackCrypto = Object.create(currentCrypto || null);
                fallbackCrypto.getRandomValues = getRandomValues;
                fallbackCrypto.randomUUID = randomUuid;
                fallbackCrypto.subtle = subtleCrypto;

                try {
                    Object.defineProperty(window, "crypto", {
                        configurable: true,
                        enumerable: true,
                        value: fallbackCrypto,
                        writable: true,
                    });
                } catch (_) {
                    // Some hosts expose a non-configurable crypto object.
                    try {
                        window.crypto.subtle = subtleCrypto;
                    } catch (_) {
                        // Let the SDK report the unsupported host capability.
                    }
                }
                if (!window.crypto || !window.crypto.subtle || typeof window.crypto.subtle.digest !== "function") {
                    throw new Error("tidal_sdk_webcrypto_unavailable");
                }
            };

            const ensureTidalSdk = async () => {
                throw new Error("sdk_backend_disabled");
            };

            const loadSdkProductForPayload = async (payloadValue, args = {}, options = {}) => {
                const mediaProduct = loadSdkMediaProductFromPayload(payloadValue);
                if (!mediaProduct) {
                    throw new Error("sdk_media_product_missing");
                }
                const payloadObject = payloadValue && typeof payloadValue === "object" ? payloadValue : {};
                const assetPosition = args.asset_position == null
                    ? (payloadObject.asset_position == null ? 0 : Number(payloadObject.asset_position))
                    : Number(args.asset_position);
                if (!Number.isFinite(assetPosition) || assetPosition < 0) {
                    throw new Error("invalid_asset_position");
                }
                const shouldAutoStart = Boolean(options && options.autoStart);

                const playerModule = await ensureTidalSdk();
                const activeProduct = typeof playerModule.getMediaProduct === "function"
                    ? playerModule.getMediaProduct()
                    : null;
                if (!activeProduct || String(activeProduct.productId) !== mediaProduct.productId || tidalSdkLoadedProductId !== mediaProduct.productId) {
                    tidalSdkLastError = "";
                    await withTimeout(
                        playerModule.load(mediaProduct, assetPosition),
                        10000,
                        "tidal_sdk_load_timeout",
                    );
                    tidalSdkMediaProduct = mediaProduct;
                    tidalSdkLoadedProductId = mediaProduct.productId;
                }

                if (shouldAutoStart) {
                    try {
                        await withTimeout(playerModule.play(), 2500, "tidal_sdk_play_timeout");
                        tidalSdkPlaybackState = "PLAYING";
                    } catch (error) {
                        tidalSdkLastError = String(error && error.message ? error.message : error);
                        tidalSdkPlaybackState = "STALLED";
                    }
                }
                try {
                    tidalSdkMediaProduct = playerModule.getMediaProduct() || tidalSdkMediaProduct || mediaProduct;
                } catch (_) {
                    tidalSdkMediaProduct = tidalSdkMediaProduct || mediaProduct;
                }
                try {
                    tidalSdkPlaybackContext = playerModule.getPlaybackContext() || tidalSdkPlaybackContext;
                } catch (_) {
                    // Keep the transition context when the SDK has not exposed one yet.
                }
                return loadSdkPayload();
            };

            const executeSdkTool = async (name, args, serverPayload = {}) => {
                if (SDK_PREPARE_TOOL_NAMES.has(name)) {
                    const shouldAutoStart = pendingSdkUserPlay
                        || (name !== "webplayer_play" && tidalSdkPlaybackState === "PLAYING");
                    pendingSdkUserPlay = false;
                    return loadSdkProductForPayload(serverPayload, args, { autoStart: shouldAutoStart });
                }

                const playerModule = await ensureTidalSdk();
                const commandArgs = args && typeof args === "object" ? args : {};
                const commandPayload = serverPayload && typeof serverPayload === "object" ? serverPayload : {};
                if (name === "webplayer_pause") {
                    if (tidalApiPreviewMediaElement && tidalApiPreviewProductId) {
                        tidalApiPreviewMediaElement.pause();
                        tidalApiPreviewState = "NOT_PLAYING";
                    }
                    await withTimeout(playerModule.pause(), 1500, "tidal_sdk_pause_timeout").catch(() => null);
                    tidalSdkPlaybackState = "NOT_PLAYING";
                    return { ...loadSdkPayload(), sdk_command: "pause" };
                }
                if (name === "webplayer_stop") {
                    stopTidalApiPreview();
                    await withTimeout(playerModule.reset(), 1500, "tidal_sdk_reset_timeout").catch(() => null);
                    tidalSdkPlaybackState = "IDLE";
                    return { ...loadSdkPayload(), sdk_command: "reset" };
                }
                if (name === "webplayer_seek") {
                    const rawAbsolute = commandArgs.position_seconds != null
                        ? commandArgs.position_seconds
                        : commandPayload.seek_mode === "absolute" ? commandPayload.seek_value : null;
                    const rawDelta = commandArgs.delta_seconds != null
                        ? commandArgs.delta_seconds
                        : commandPayload.seek_mode === "relative" ? commandPayload.seek_value : null;
                    if (rawAbsolute == null && rawDelta == null) {
                        throw new Error("missing_seek_position");
                    }
                    const apiPreviewActive = Boolean(
                        selectedPlaybackBackend === "api"
                        && tidalApiPreviewMediaElement
                        && tidalApiPreviewProductId
                        && tidalApiPreviewObjectUrl
                    );
                    const currentPosition = apiPreviewActive
                        ? Number(tidalApiPreviewMediaElement.currentTime) || 0
                        : typeof playerModule.getAssetPosition === "function"
                        ? Number(playerModule.getAssetPosition()) || 0
                        : 0;
                    const requestedPosition = rawAbsolute == null
                        ? currentPosition + Number(rawDelta)
                        : Number(rawAbsolute);
                    if (!Number.isFinite(requestedPosition) || requestedPosition < 0) {
                        throw new Error("invalid_seek_position");
                    }
                    const sdkContext = typeof playerModule.getPlaybackContext === "function"
                        ? playerModule.getPlaybackContext()
                        : null;
                    const duration = apiPreviewActive
                        ? Number(tidalApiPreviewMediaElement.duration)
                        : Number(sdkContext && sdkContext.actualDuration);
                    const boundedPosition = Number.isFinite(duration) && duration > 0
                        ? Math.min(requestedPosition, duration)
                        : requestedPosition;
                    if (apiPreviewActive) {
                        tidalApiPreviewMediaElement.currentTime = boundedPosition;
                    } else {
                        await withTimeout(playerModule.seek(boundedPosition), 1500, "tidal_sdk_seek_timeout").catch(() => null);
                    }
                    return { ...loadSdkPayload(), sdk_command: "seek", asset_position: boundedPosition };
                }
                if (name === "webplayer_volume_set") {
                    const requestedVolume = commandArgs.volume_percent != null
                        ? Number(commandArgs.volume_percent)
                        : commandArgs.volume_float != null
                            ? Number(commandArgs.volume_float) * 100
                            : Number(commandPayload.volume_percent);
                    if (!Number.isFinite(requestedVolume)) {
                        throw new Error("invalid_volume");
                    }
                    const volumePercent = Math.max(0, Math.min(100, requestedVolume));
                    playerModule.setVolumeLevel(volumePercent);
                    if (tidalApiPreviewMediaElement) {
                        tidalApiPreviewMediaElement.volume = volumePercent / 100;
                    }
                    return { ...loadSdkPayload(), sdk_command: "set_volume", volume_percent: volumePercent };
                }
                if (name === "webplayer_volume_adjust") {
                    const deltaPercent = commandArgs.delta_percent != null
                        ? Number(commandArgs.delta_percent)
                        : commandArgs.delta_float != null
                            ? Number(commandArgs.delta_float) * 100
                            : Number(commandPayload.delta_percent);
                    if (!Number.isFinite(deltaPercent)) {
                        throw new Error("invalid_volume_delta");
                    }
                    const currentVolume = typeof playerModule.getVolumeLevel === "function"
                        ? Number(playerModule.getVolumeLevel()) || 0
                        : 0;
                    const volumePercent = Math.max(0, Math.min(100, currentVolume + deltaPercent));
                    playerModule.setVolumeLevel(volumePercent);
                    if (tidalApiPreviewMediaElement) {
                        tidalApiPreviewMediaElement.volume = volumePercent / 100;
                    }
                    return { ...loadSdkPayload(), sdk_command: "adjust_volume", volume_percent: volumePercent };
                }
                throw new Error("sdk_command_unsupported");
            };

            const callTool = async (name, args, options = {}) => {
                const onlyWhenChanged = Boolean(options.onlyWhenChanged);
                const suppressErrorView = Boolean(options.suppressErrorView);
                const isUserAction = Boolean(options.isUserAction);
                const isRefresh = Boolean(options.isRefresh);
                const effectiveArgs = {
                    ...loadSelectedBackendArguments(),
                    ...(args && typeof args === "object" ? args : {}),
                };
                if (isRefresh && (inFlightUserAction || inFlightRefresh)) {
                    return;
                }

                const currentCallSequence = ++callSequence;
                if (isUserAction) {
                    inFlightUserAction = true;
                    setControlsBusy(true);
                } else if (isRefresh) {
                    inFlightRefresh = true;
                }

                try {
                    lastToolRequestName = name;
                    lastToolRequestArgs = { ...effectiveArgs };
                    let result = null;
                    if (name === "webplayer_usrauth") {
                        result = await callServerToolRaw(name, effectiveArgs);
                        const authPayload = extractStructuredPayload(result);
                        renderAuthView(authPayload);
                        lastAppliedSequence = currentCallSequence;
                        postEmbedMessage({
                            type: "webplayer-mini-controls-auth-state",
                            status: String(authPayload.status || "unknown"),
                            ok: authPayload.ok !== false,
                        });
                        return;
                    }

                    let resultPayload;
                    if (SDK_LOCAL_TOOL_NAMES.has(name) && selectedPlaybackBackend === "sdk") {
                        resultPayload = await executeSdkTool(name, effectiveArgs, lastServerPayload);
                        resultPayload = {
                            ...resultPayload,
                            object_name: name,
                            ok: true,
                            return_code: 0,
                        };
                        result = { structuredContent: resultPayload };
                    } else {
                        result = await callServerToolRaw(name, effectiveArgs);
                        resultPayload = extractStructuredPayload(result);
                        const serverError = loadToolError(resultPayload, result);
                        if (serverError) {
                            throw new Error(serverError);
                        }
                        lastServerPayload = { ...lastServerPayload, ...resultPayload };
                        if (SDK_PREPARE_TOOL_NAMES.has(name) && selectedPlaybackBackend === "sdk") {
                            resultPayload = await executeSdkTool(name, effectiveArgs, resultPayload);
                            resultPayload = {
                                ...resultPayload,
                                object_name: name,
                                ok: true,
                                return_code: 0,
                            };
                            result = { structuredContent: resultPayload };
                        } else if (name === "webplayer_now_playing" && selectedPlaybackBackend === "sdk" && tidalPlayerModule) {
                            resultPayload = loadSdkPayload();
                            resultPayload = {
                                ...resultPayload,
                                object_name: name,
                                ok: true,
                                return_code: 0,
                            };
                            result = { structuredContent: resultPayload };
                        }
                        if (name === "webplayer_now_playing" && selectedPlaybackBackend === "sdk" && !tidalPlayerModule) {
                            void primeTidalSdkProduct(resultPayload);
                        }
                    }
                    const toolError = loadToolError(resultPayload, result);
                    if (toolError) {
                        throw new Error(toolError);
                    }

                    const infoView = mergeInfoView(loadNowPlayingView({ structuredContent: resultPayload }));
                    const signature = loadInfoSignature(infoView);
                    if (currentCallSequence < lastAppliedSequence) {
                        return;
                    }
                    if (!onlyWhenChanged || signature !== lastInfoSignature) {
                        renderInfoView(infoView);
                        lastInfoSignature = signature;
                    }
                    lastInfoView = infoView;
                    lastAppliedSequence = currentCallSequence;
                    postEmbedMessage({ type: "webplayer-mini-controls-result", tool: name, ok: true, result, info: infoView });
                } catch (err) {
                    if (!suppressErrorView) {
                        renderErrorView(name, err);
                    }
                    postEmbedMessage({ type: "webplayer-mini-controls-result", tool: name, ok: false, error: String(err) });
                } finally {
                    if (isUserAction) {
                        inFlightUserAction = false;
                        setControlsBusy(false);
                    }
                    if (isRefresh) {
                        inFlightRefresh = false;
                    }
                }

                if (isUserAction && name !== "webplayer_now_playing" && name !== "tidal_api_add_track_to_collection") {
                    refreshNowPlaying();
                }
            };

            bindMediaSessionHandlers();

            trackProgressEl?.addEventListener("change", () => {
                const durationSeconds = trackCountdownState.durationSeconds;
                const progressValue = Number(trackProgressEl.value);
                if (!Number.isFinite(durationSeconds) || durationSeconds <= 0 || !Number.isFinite(progressValue)) {
                    return;
                }
                const positionSeconds = Math.min(
                    durationSeconds,
                    Math.max(0, progressValue / 100 * durationSeconds)
                );
                callTool("webplayer_seek", { position_seconds: positionSeconds }, {
                    onlyWhenChanged: false,
                    isUserAction: true,
                });
            });

            controlsEl.addEventListener("click", (event) => {
                const eventPath = typeof event.composedPath === "function" ? event.composedPath() : [];
                const buttonEl = eventPath.find((element) => (
                    element
                    && typeof element.matches === "function"
                    && element.matches("[data-tool]")
                )) || (event.target && typeof event.target.closest === "function"
                    ? event.target.closest("[data-tool]")
                    : null);
                if (!buttonEl || !controlsEl.contains(buttonEl)) {
                    return;
                }
                    if (inFlightUserAction) {
                        return;
                    }
                    let toolName = buttonEl.getAttribute("data-tool") || "";
                    const argsText = buttonEl.getAttribute("data-args") || "{}";
                    let args = {};
                    try { args = JSON.parse(argsText); } catch (_) { args = {}; }
                    if (toolName === "tidal_api_add_track_to_collection") {
                        const trackUrl = String(lastInfoView && lastInfoView.trackUrl || "").trim();
                        const trackUrlMatch = /\\/track\\/(\\d+)(?:[/?#]|$)/i.exec(trackUrl);
                        const trackId = String(
                            lastInfoView && (lastInfoView.trackId || lastInfoView.tidalTrackId)
                            || (trackUrlMatch && trackUrlMatch[1])
                            || ""
                        ).trim();
                        if (!trackId) {
                            toolName = "webplayer_like_current";
                        }
                        args = {
                            ...args,
                            ...(trackId ? { track_id: trackId } : {}),
                        };
                    }
                    if (selectedPlaybackBackend === "sdk" && SDK_PREPARE_TOOL_NAMES.has(toolName)) {
                        if (toolName === "webplayer_play") {
                            if (!startTidalSdkMediaFromUserGesture()) {
                                pendingSdkUserPlay = true;
                            }
                        } else if (toolName !== "webplayer_forward" && toolName !== "webplayer_backward") {
                            pendingSdkUserPlay = true;
                        }
                    }
                    callTool(toolName, args, { onlyWhenChanged: true, isUserAction: true });
            });

            const refreshNowPlaying = () => {
                callTool("webplayer_now_playing", {}, { onlyWhenChanged: true, suppressErrorView: true, isRefresh: true });
            };

            syncBackendSelection();

            if (app) {
                app.ontoolresult = async (result) => {
                    const toolPayload = extractStructuredPayload(result);
                    if (toolPayload.object_name === "webplayer_usrauth") {
                        renderAuthView(toolPayload);
                        return;
                    }
                    if (inFlightUserAction || inFlightRefresh) {
                        // callTool owns results for actions initiated by this UI.
                        return;
                    }
                    lastServerPayload = { ...lastServerPayload, ...toolPayload };
                    const toolName = String(toolPayload.object_name || "").trim();
                    const toolError = loadToolError(toolPayload, result);
                    if (toolError) {
                        renderErrorView(toolName, toolError);
                        return;
                    }
                    if (selectedPlaybackBackend === "sdk" && (SDK_PREPARE_TOOL_NAMES.has(toolName) || SDK_LOCAL_TOOL_NAMES.has(toolName))) {
                        try {
                            const sdkPayload = await executeSdkTool(toolName, {}, toolPayload);
                            const mergedPayload = {
                                ...sdkPayload,
                                object_name: toolName,
                                ok: true,
                                return_code: 0,
                            };
                            lastServerPayload = { ...lastServerPayload, ...mergedPayload };
                            const infoView = mergeInfoView(loadNowPlayingView({ structuredContent: mergedPayload }));
                            renderInfoView(infoView);
                            lastInfoSignature = loadInfoSignature(infoView);
                            lastInfoView = infoView;
                            return;
                        } catch (error) {
                            renderErrorView(toolName, error);
                            return;
                        }
                    }
                    if (tidalPlayerModule) {
                        renderSdkState();
                    } else {
                        const infoView = mergeInfoView(loadNowPlayingView({ structuredContent: toolPayload }));
                        renderInfoView(infoView);
                        lastInfoSignature = loadInfoSignature(infoView);
                        lastInfoView = infoView;
                    }
                };
                app.onhostcontextchanged = applyHostContext;
                app.onteardown = async () => {
                    stopPolling();
                    return {};
                };
            }

            renderLoadingView("Loading playback status...");
            window.addEventListener("load", postEmbedSize);
            window.addEventListener("load", syncPanelWidthToControls);
            window.addEventListener("resize", syncPanelWidthToControls);
            window.addEventListener("message", (event) => {
                const messageData = event && event.data;
                if (!messageData || typeof messageData !== "object") {
                    return;
                }
                if (messageData.type === "webplayer-tool-result") {
                    const requestId = String(messageData.requestId || "");
                    const pendingCall = pendingVsCodeCalls.get(requestId);
                    if (!pendingCall) {
                        return;
                    }
                    pendingVsCodeCalls.delete(requestId);
                    window.clearTimeout(pendingCall.timeoutId);
                    if (messageData.error) {
                        pendingCall.reject(new Error(String(messageData.error)));
                    } else {
                        pendingCall.resolve(messageData.result);
                    }
                    return;
                }
                if (messageData.type === "vscode-host-context" || messageData.type === "host-context") {
                    applyHostContext(messageData.context || messageData);
                }
            });
            setTimeout(postEmbedSize, 0);
            setTimeout(postEmbedSize, 250);
            setTimeout(syncPanelWidthToControls, 0);
            setTimeout(syncPanelWidthToControls, 250);
            const startPolling = () => {
                if (refreshIntervalId !== null) {
                    return;
                }
                refreshIntervalId = window.setInterval(refreshNowPlaying, 4000);
                countdownIntervalId = window.setInterval(renderTrackCountdown, 1000);
                void refreshAudioOutputInfo();
                if (navigator.mediaDevices && typeof navigator.mediaDevices.addEventListener === "function") {
                    navigator.mediaDevices.addEventListener("devicechange", refreshAudioOutputInfo);
                }
            };
            const stopPolling = () => {
                stopAudioScopePolling();
                document.removeEventListener("visibilitychange", syncAudioScopePolling);
                if (refreshIntervalId !== null) {
                    window.clearInterval(refreshIntervalId);
                    refreshIntervalId = null;
                }
                if (countdownIntervalId !== null) {
                    window.clearInterval(countdownIntervalId);
                    countdownIntervalId = null;
                }
                if (navigator.mediaDevices && typeof navigator.mediaDevices.removeEventListener === "function") {
                    navigator.mediaDevices.removeEventListener("devicechange", refreshAudioOutputInfo);
                }
            };
            window.addEventListener("pagehide", stopPolling, { once: true });
            if (isEmbedded && app && typeof app.connect === "function") {
                try {
                    await app.connect();
                    appHostConnected = true;
                    const hostContext = app.getHostContext();
                    applyHostContext(hostContext || { theme: "light" });
                } catch (_) {
                    appHostConnected = false;
                    applyTheme("light");
                }
            } else {
                applyTheme("light");
            }
            if (vscodeApi) {
                vscodeApi.postMessage({ type: "webplayer-ui-ready" });
            }
            callTool("webplayer_now_playing", {});
            void refreshAudioOutputInfo();
            startPolling();
        </script>
    </body>
</html>
""".replace(
    "__WEBPLAYER_UI_PLAY_ARGUMENTS__",
    ui_play_arguments_json,
).replace(
    "__WEBPLAYER_UI_DEFAULT_ARGUMENTS__",
    ui_default_arguments_json,
).replace(
    "__MCP_APP_UI_COVER_PATH__",
    ui_cover_path_json,
).replace(
    "__MCP_APP_UI_PREVIEW_PATH__",
    ui_preview_path_json,
).replace(
    "__MCP_APP_UI_SCOPE_PATH__",
    ui_scope_path_json,
)

    def load_resources_list_result(self, _params: dict[str, Any], *, ui_enabled: bool = False) -> dict[str, Any]:
        # The resource is still MCP-only; unsupported hosts may ignore it.
        return {"result": {"resources": [self._load_ui_resource_descriptor()]}}

    def load_resources_read_result(self, params: dict[str, Any], *, ui_enabled: bool = False) -> dict[str, Any]:
        resource_uri = str(params.get("uri") or "").strip()
        if resource_uri != self._UI_RESOURCE_URI and resource_uri not in self._UI_LEGACY_RESOURCE_URIS:
            return {"error": "resource_not_found"}

        return {
            "result": {
                "contents": [
                    {
                        "uri": resource_uri,
                        "mimeType": self._UI_RESOURCE_MIME_TYPE,
                        "text": self._load_ui_resource_markup(),
                        "_meta": self._UI_RESOURCE_META,
                    }
                ]
            }
        }

    def load_tools_list_result(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"result": {"tools": self.load_tool_definitions()}}

    def load_prompts_list_result(self, _params: dict[str, Any]) -> dict[str, Any]:
        operator_description = "Operational prompt for controlling the Chromium WebPlayer through wplctl, reading browser playback metadata, volume, authentication, and using separate TIDAL API information tools via MCP calls."
        prompts_payload = [
            {
                "name": "webplayer_operator",
                "description": operator_description,
                "arguments": [
                    {
                        "name": "player_selector",
                        "description": "Chromium WebPlayer selector controlled by wplctl.",
                        "required": False,
                    },
                    {
                        "name": "search_query",
                        "description": "Optional query used when a search is requested.",
                        "required": False,
                    },
                ],
            },
            {
                "name": "webplayer_strategy_overview",
                "description": "General strategy overview for the current WebPlayer playback, authentication, volume control, search, and TIDAL API tool set.",
                "arguments": [
                    {
                        "name": "player_selector",
                        "description": "Chromium WebPlayer selector controlled by wplctl.",
                        "required": False,
                    }
                ],
            },
            {
                "name": "webplayer_strategy_ifaai_generalized",
                "description": "Generalized governance strategy for the current WebPlayer playback and TIDAL API operations.",
                "arguments": [
                    {
                        "name": "player_selector",
                        "description": "Chromium WebPlayer selector controlled by wplctl.",
                        "required": False,
                    },
                    {
                        "name": "risk_tier",
                        "description": "low|standard|high|critical",
                        "required": False,
                    },
                    {
                        "name": "compliance_mode",
                        "description": "strict|balanced|adaptive",
                        "required": False,
                    },
                ],
            },
        ]
        return {"result": {"prompts": prompts_payload}}

    def load_prompts_get_result(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt_name = str(params.get("name") or "").strip()
        arguments_payload = params.get("arguments") or {}
        if not isinstance(arguments_payload, dict):
            arguments_payload = {}

        player_selector = str(arguments_payload.get("player_selector") or "chromium").strip() or "chromium"

        if prompt_name == "webplayer_strategy_overview":
            prompt_text = self._build_webplayer_strategy_overview_text(player_selector=player_selector)
        elif prompt_name == "webplayer_strategy_ifaai_generalized":
            prompt_text = self._build_webplayer_ifaai_strategy_text(
                player_selector=player_selector,
                risk_tier=str(arguments_payload.get("risk_tier") or ""),
                compliance_mode=str(arguments_payload.get("compliance_mode") or ""),
            )
        elif prompt_name == "webplayer_operator":
            search_query = str(arguments_payload.get("search_query") or "").strip()
            search_line = (
                f"9) For content discovery call webplayer_search_play with query='{search_query}'."
                if search_query
                else "9) For content discovery call webplayer_search_play with a non-empty query."
            )
            selector_line = "Use player_selector='chromium' for the browser player controlled by wplctl.\n"
            backend_line = "Use playback_backend='browser' for all WebPlayer controls and information. Direct tidal_api_* tools are for API information only and never control playback.\n"
            control_lines = (
                "4) Pause playback with webplayer_pause when requested; use webplayer_stop only when a full stop is requested.\n"
                "5) Seek the active player with webplayer_seek when a precise position change is requested.\n"
                "6) Skip next with webplayer_forward when requested.\n"
                "7) Skip previous with webplayer_backward when requested.\n"
                "8) Use webplayer_get_media_product or webplayer_get_next_media_product for richer media-product inspection.\n"
            )

            prompt_text = (
                "You are controlling a TIDAL playback service through MCP tool calls.\n"
                + selector_line
                + backend_line
                + "Execution sequence:\n"
                "1) Check state first with webplayer_now_playing.\n"
                "2) Use webplayer_get_media_product or webplayer_get_next_media_product when you need richer track and queue metadata, including stream quality, album art, bitrate, sample rate, key, BPM, and artist info.\n"
                "2b) If the token is missing, expired, or any TIDAL API call returns 401, call webplayer_usrauth before retrying playback.\n"
                "3) Start playback with webplayer_search_play for searches, webplayer_playlist_play for playlists, webplayer_library_play for library sections, or webplayer_open_playback_target for direct TIDAL URLs when the API needs a link.\n"
                "4) Use webplayer_play only as a generic retry when playback is already prepared.\n"
                + control_lines
                + f"{search_line}\n"
                + "10) Use webplayer_volume_set or webplayer_volume_adjust to change the browser player volume (volume_percent 0-100 or volume_float 0.0-1.0).\n"
                + "11) Use webplayer_usrauth to initialize or finalize TIDAL user login when the user wants authentication or the token file needs repair.\n"
                + "12) Use tidal_api_request, tidal_api_search, tidal_api_track, tidal_api_track_manifest, or tidal_api_widevine when the user asks for direct API inspection or manifest data.\n"
                + "13) If a manifest reports Widevine or FairPlay DRM, report that playback requires the supported DRM client and do not bypass it.\n"
                + "After each control action, call webplayer_now_playing and summarize status/title/artist/album.\n"
                + "If any tool returns ok=false, surface stderr/stdout and propose the next corrective action."
            )
        else:
            return {"error": "prompt_not_found"}

        return {
            "result": {
                "description": "Prompt template for WebPlayer operations via MCP tools.",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": prompt_text,
                        },
                    }
                ],
            }
        }

    def load_tools_call_result(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        if not tool_name:
            return {"error": "missing_tool_name"}

        arguments_payload = params.get("arguments") or {}
        if isinstance(arguments_payload, str):
            try:
                arguments_payload = json.loads(arguments_payload)
            except Exception:
                arguments_payload = {}
        if not isinstance(arguments_payload, dict):
            arguments_payload = {}

        if tool_name == "webplayer_usrauth":
            auth_result = self.user_auth_service.dispatch_object(
                object_name=tool_name,
                arguments=arguments_payload,
            )
            return {"result": {"content": json.dumps(auth_result, ensure_ascii=True)}}

        if tool_name.startswith("tidal_api_"):
            tidal_api_result = self.tidal_api_service.dispatch_object(
                object_name=tool_name,
                arguments=arguments_payload,
            )
            return {"result": {"content": json.dumps(tidal_api_result, ensure_ascii=True)}}

        if tool_name == self._UI_SCOPE_TOOL_NAME:
            try:
                scope_payload = PLAYBACK_SCOPE_SERVICE.load_scope_payload(arguments_payload)
            except Exception:
                scope_payload = {"ok": False, "status": "unavailable", "error": "scope_request_failed"}
            return {"result": {"content": json.dumps(scope_payload, ensure_ascii=True)}}

        if tool_name.startswith("webplayer_"):
            arguments_payload = dict(arguments_payload)
            requested_backend = str(arguments_payload.get("playback_backend") or "browser").strip().casefold() or "browser"
            if requested_backend != "browser":
                arguments_payload["requested_playback_backend"] = requested_backend
            arguments_payload["playback_backend"] = "browser"
            arguments_payload["player_selector"] = "chromium"

        player_selector = str(arguments_payload.get("player_selector") or "chromium").strip() or "chromium"
        query = str(arguments_payload.get("query") or "").strip() if "query" in arguments_payload else None

        result_payload = self.player_service.dispatch_object(
            object_name=tool_name,
            player_selector=player_selector,
            query=query,
            arguments=arguments_payload,
        )
        if requested_backend != "browser":
            result_payload["requested_playback_backend"] = requested_backend
            result_payload["playback_backend_forced"] = True
        return {"result": {"content": json.dumps(result_payload, ensure_ascii=True)}}

    def _load_mcp_tool_definitions(self, *, ui_enabled: bool = False) -> list[dict[str, Any]]:
        tool_definitions: list[dict[str, Any]] = []
        for tool_payload in self.load_tool_definitions():
            function_payload = dict(tool_payload.get("function") or {})
            tool_name = str(function_payload.get("name") or "").strip()
            if not tool_name:
                continue

            input_schema_payload = function_payload.get("parameters")
            if not isinstance(input_schema_payload, dict):
                input_schema_payload = {
                    "type": "object",
                    "additionalProperties": False,
                }

            mcp_tool_payload: dict[str, Any] = {
                "name": tool_name,
                "title": "TIDAL WebPlayer" if tool_name == "webplayer_now_playing" else tool_name,
                "description": str(function_payload.get("description") or ""),
                "inputSchema": input_schema_payload,
            }
            if ui_enabled and tool_name.startswith("webplayer_"):
                ui_meta_payload: dict[str, Any] = {"resourceUri": self._UI_RESOURCE_URI}
                if tool_name == self._UI_SCOPE_TOOL_NAME:
                    ui_meta_payload["visibility"] = ["app"]
                tool_meta_payload: dict[str, Any] = {"ui": ui_meta_payload}
                if tool_name == "webplayer_now_playing":
                    tool_meta_payload["openai/ui"] = {
                        "entrypoints": [{"type": "thread"}],
                    }
                mcp_tool_payload["_meta"] = tool_meta_payload

            tool_definitions.append(mcp_tool_payload)
        return tool_definitions

    def load_tool_definitions(self) -> list[dict[str, Any]]:
        base_properties = {
            "player_selector": {
                "type": "string",
                "enum": ["chromium"],
                "description": "Chromium WebPlayer selector controlled by wplctl.",
                "default": "chromium",
            },
            "playback_backend": {
                "type": "string",
                "enum": ["browser"],
                "description": "Browser backend controlled by wplctl. Direct tidal_api_* tools are for API information only.",
                "default": "browser",
            }
        }

        return [
            {
                "type": "function",
                "function": {
                    "name": "webplayer_play",
                    "description": "Start the active TIDAL media product through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "asset_position": {
                                "type": "number",
                                "description": "Optional start position in seconds.",
                                "minimum": 0,
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": self._UI_SCOPE_TOOL_NAME,
                    "description": "Return live 24-bit PCM oscilloscope traces. The default pair compares one channel before CamillaDSP (in1, the DSP input monitor) and after DSP (out1, the first playback-sink monitor); optional source ids can select up to six traces.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channels": {
                                "type": "string",
                                "description": "Comma-separated source ids for up to six trace slots, e.g. in1,out1. The default is the IN_SOURCE_1 to OUT_SINK_1 path.",
                            },
                            "window_ms": {
                                "type": "number",
                                "description": "Horizontal window in milliseconds (0.25..200).",
                            },
                            "points": {
                                "type": "integer",
                                "description": "Maximum points per trace; longer windows return min/max envelopes (32..2048).",
                            },
                            "trigger": {
                                "type": "string",
                                "enum": ["rising", "off"],
                                "description": "Rising zero-crossing trigger on CH1.",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_pause",
                    "description": "Pause the active Chromium WebPlayer without clearing the queue.",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_get_media_product",
                    "description": "Return current browser playback and media-product metadata from wplctl.",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_get_next_media_product",
                    "description": "Return the next queued media product and its metadata.",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_seek",
                    "description": "Seek the active Chromium WebPlayer in seconds.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "position_seconds": {
                                "type": "number",
                                "description": "Absolute position in seconds from the start of the track.",
                            },
                            "delta_seconds": {
                                "type": "number",
                                "description": "Relative seek offset in seconds; positive or negative.",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_stop",
                    "description": "Stop the active Chromium WebPlayer and reset its media state.",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_forward",
                    "description": "Skip forward to next track.",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_backward",
                    "description": "Go back to previous track.",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_like_current",
                    "description": "Add the currently playing track through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": dict(base_properties),
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_toggle_like_current",
                    "description": "Toggle the current track like through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": dict(base_properties),
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_now_playing",
                    "description": "Show current track metadata (title, artist, album, status).",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_volume_set",
                    "description": "Set the Chromium WebPlayer volume.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "volume_percent": {"type": "integer", "description": "Volume as percent (0-100)."},
                            "volume_float": {"type": "number", "description": "Volume as float (0.0-1.0)."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_volume_adjust",
                    "description": "Adjust the Chromium WebPlayer volume by a relative delta.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "delta_percent": {"type": "integer", "description": "Relative percent to change (e.g. 5 or -5)."},
                            "delta_float": {"type": "number", "description": "Relative float to change (e.g. 0.05 or -0.05)."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_system_volume_set",
                    "description": "Set the Linux system volume of the default audio sink.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "volume_percent": {"type": "integer", "description": "System volume as percent (0-100)."},
                            "volume_float": {"type": "number", "description": "System volume as float (0.0-1.0)."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_system_volume_adjust",
                    "description": "Adjust the Linux system volume of the default audio sink by a relative delta.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "delta_percent": {"type": "integer", "description": "Relative system volume change (e.g. 5 or -5)."},
                            "delta_float": {"type": "number", "description": "Relative system volume change (e.g. 0.05 or -0.05)."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_search",
                    "description": "Search TIDAL through the Chromium WebPlayer without using the API as a control path.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "query": {"type": "string", "description": "Search query for TIDAL."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_search_play",
                    "description": "Search TIDAL through the Chromium WebPlayer and start the result through wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "query": {"type": "string", "description": "Search query for TIDAL."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_playlist_play",
                    "description": "Load a TIDAL playlist and play it through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "playlist_url": {
                                "type": "string",
                                "description": "Full TIDAL playlist URL.",
                            },
                            "playlist_id": {
                                "type": "string",
                                "description": "TIDAL playlist id used when playlist_url is omitted.",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_library_play",
                    "description": "Load TIDAL user collection tracks and play them through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "section": {
                                "type": "string",
                                "description": "Library section (favorites_tracks, favorites_albums, favorites_playlists, my_collection_tracks, history, home).",
                                "default": "favorites_tracks",
                            },
                            "library_url": {
                                "type": "string",
                                "description": "Optional explicit TIDAL library URL; overrides section.",
                            },
                            "track_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional ordered TIDAL track IDs for a generated collection queue.",
                            },
                            "collection_name": {
                                "type": "string",
                                "description": "Optional generated collection name stored with the playback queue.",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_library_snapshot",
                    "description": "Read TIDAL user collection information through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "section": {
                                "type": "string",
                                "description": "Library section (favorites_tracks, favorites_albums, favorites_playlists, my_collection_tracks, history, home).",
                                "default": "favorites_tracks",
                            },
                            "library_url": {
                                "type": "string",
                                "description": "Optional explicit TIDAL library URL; overrides section.",
                            },
                            "max_items": {
                                "type": "integer",
                                "description": "Maximum number of track entries collected from the page.",
                                "default": 200,
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_open_playback_target",
                    "description": "Resolve a validated TIDAL URL and play it through the Chromium WebPlayer controlled by wplctl.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "target_url": {
                                "type": "string",
                                "description": "Absolute TIDAL URL (tidal.com or subdomain).",
                            },
                        },
                        "required": ["target_url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_usrauth",
                    "description": "Start, complete, or inspect TIDAL user OAuth for browser-backed WebPlayer operations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Authorization code returned by the OAuth redirect URL.",
                            },
                            "state": {
                                "type": "string",
                                "description": "OAuth state returned by the redirect URL.",
                            },
                            "redirect_url": {
                                "type": "string",
                                "description": "Full OAuth redirect URL, if available.",
                            },
                            "code_verifier": {
                                "type": "string",
                                "description": "Optional PKCE code verifier when the login request was created externally.",
                            },
                            "force_login": {
                                "type": "boolean",
                                "description": "Start a fresh login even if a valid token is already stored.",
                                "default": False,
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_add_track_to_collection",
                    "description": "Add the current TIDAL track to the authenticated user's My Collection Tracks.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "track_id": {
                                "type": "string",
                                "description": "TIDAL track ID.",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers.",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["track_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_add_track_to_playlist",
                    "description": "Add a TIDAL track to a user playlist through the official API.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "playlist_id": {
                                "type": "string",
                                "description": "TIDAL playlist ID.",
                            },
                            "track_id": {
                                "type": "string",
                                "description": "TIDAL track ID.",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers.",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["playlist_id", "track_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_request",
                    "description": "Call a TIDAL HTTP endpoint on tidal.com subdomains.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "method": {
                                "type": "string",
                                "description": "HTTP method (GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD).",
                                "default": "GET",
                            },
                            "url": {
                                "type": "string",
                                "description": "Absolute HTTPS URL on tidal.com or subdomain.",
                            },
                            "query": {
                                "type": "object",
                                "description": "Optional query parameter map.",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers (Authorization/Cookie will be redacted in output).",
                            },
                            "body_json": {
                                "type": "object",
                                "description": "Optional JSON body; sets Content-Type application/json if missing.",
                            },
                            "body_text": {
                                "type": "string",
                                "description": "Optional UTF-8 text body.",
                            },
                            "body_base64": {
                                "type": "string",
                                "description": "Optional base64-encoded binary body.",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_search",
                    "description": "Search TIDAL catalog resources through the official searchResults API.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "TIDAL search query."},
                            "country_code": {
                                "type": "string",
                                "description": "Country code for catalog context.",
                                "default": "DE",
                            },
                            "include": {
                                "type": "string",
                                "description": "Comma-separated relationships; nested paths such as tracks.artists and tracks.albums.coverArt are supported.",
                                "default": "tracks,tracks.artists,tracks.albums,tracks.albums.coverArt,topHits",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers.",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_track",
                    "description": "Fetch track metadata from openapi.tidal.com/v2/tracks/{track_id}.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "track_id": {"type": "string", "description": "TIDAL track id."},
                            "country_code": {
                                "type": "string",
                                "description": "Country code for catalog context.",
                                "default": "DE",
                            },
                            "include": {
                                "type": "string",
                                "description": "Include selector, for example albums,artists.",
                                "default": "albums,artists",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers.",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["track_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_track_manifest",
                    "description": "Fetch track manifest metadata from openapi.tidal.com/v2/trackManifests/{track_id}.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "track_id": {"type": "string", "description": "TIDAL track id."},
                            "adaptive": {
                                "type": "boolean",
                                "description": "Adaptive manifest flag.",
                                "default": True,
                            },
                            "formats": {
                                "type": "string",
                                "description": "Manifest formats selector.",
                                "default": "EMBEDDED",
                            },
                            "manifest_type": {
                                "type": "string",
                                "description": "Manifest type selector.",
                                "default": "FULL",
                            },
                            "uri_scheme": {
                                "type": "string",
                                "description": "URI scheme selector.",
                                "default": "HTTPS",
                            },
                            "usage": {
                                "type": "string",
                                "description": "Usage selector.",
                                "default": "STREAM",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers.",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["track_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "tidal_api_widevine",
                    "description": "POST binary widevine challenge to api.tidal.com/v2/widevine.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "body_base64": {
                                "type": "string",
                                "description": "Widevine challenge payload as base64.",
                            },
                            "headers": {
                                "type": "object",
                                "description": "Optional request headers (for example Authorization, x-tidal-token).",
                            },
                            "timeout_s": {
                                "type": "integer",
                                "description": "HTTP timeout in seconds (1..120).",
                                "default": 20,
                            },
                        },
                        "required": ["body_base64"],
                    },
                },
            },
        ]


@dataclass
class PlaybackScopeCaptureObject:
    object_name: str
    source_name: str = ""
    channel_count: int = 0
    configured_channel_count: int = 0
    channel_map: str = ""
    sample_rate: int = 0
    sample_format: str = "s32le"
    process: subprocess.Popen[bytes] | None = None
    reader_thread: threading.Thread | None = None
    last_request_at: float = 0.0
    status: str = "not_configured"
    error: str = ""
    sample_frames: collections.deque[tuple[float, ...]] | None = None

    def __post_init__(self) -> None:
        if self.sample_frames is None:
            self.sample_frames = collections.deque(maxlen=PlaybackScopeService._BUFFER_FRAME_COUNT)


class PlaybackScopeService:
    """Capture live PCM at the DSP input source and playback sink monitor."""

    _FRAME_COUNT = 1024
    _BUFFER_FRAME_COUNT = 2048
    _BUFFER_SECONDS = 0.5
    _IDLE_TIMEOUT_SECONDS = 2.0
    _STARTUP_TIMEOUT_SECONDS = 0.5
    _SLOT_COUNT = 6
    _DEFAULT_POINT_COUNT = 512
    _MIN_POINT_COUNT = 32
    _MAX_POINT_COUNT = 2048
    _MIN_WINDOW_MS = 0.25
    _MAX_WINDOW_MS = 200.0
    _SAMPLE_DIGITS = 6
    _SOURCE_SPEC_TTL_SECONDS = 5.0
    _TRIGGER_HYSTERESIS = 0.002
    _PCM_FORMATS = {
        "s16le": ("h", 2, 32768.0),
        "s32le": ("i", 4, 2147483648.0),
    }
    _OBJECT_SOURCE_PREFIXES = {"output": "out", "input": "in"}
    _DEFAULT_CHANNEL_IDS = ("in1", "out1")
    _OBJECT_SOURCE_LABELS = {"output": "Out Sink", "input": "In Source"}

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._captures: dict[str, PlaybackScopeCaptureObject] = {
            object_name: PlaybackScopeCaptureObject(
                object_name=object_name,
                error=f"playback_{'monitor' if object_name == 'output' else 'input'}_not_configured",
            )
            for object_name in self._OBJECT_SOURCE_PREFIXES
        }
        self._source_specs: dict[str, dict[str, Any]] | None = None
        self._source_specs_loaded_at = 0.0

    # Compatibility accessors for the primary output monitor capture.
    @property
    def _sample_frames(self) -> collections.deque[tuple[float, ...]]:
        return self._captures["output"].sample_frames  # type: ignore[return-value]

    @property
    def _process(self) -> subprocess.Popen[bytes] | None:
        return self._captures["output"].process

    @_process.setter
    def _process(self, process: Any) -> None:
        self._captures["output"].process = process

    @property
    def _reader_thread(self) -> threading.Thread | None:
        return self._captures["output"].reader_thread

    @property
    def _sample_rate(self) -> int:
        return self._captures["output"].sample_rate

    @_sample_rate.setter
    def _sample_rate(self, sample_rate: int) -> None:
        self._captures["output"].sample_rate = sample_rate

    @staticmethod
    def load_input_source_name(sink_name: str) -> str:
        if not sink_name.startswith("alsa_output.") or ".pro-output-" not in sink_name:
            return ""
        return sink_name.replace("alsa_output.", "alsa_input.", 1).replace(".pro-output-", ".pro-input-", 1)

    def configure(self, *, playback_device: dict[str, Any], input_source: str | None = None) -> None:
        sink_name = str(playback_device.get("autoconnect_to") or "").strip()
        monitor_source = f"{sink_name}.monitor" if sink_name else ""
        try:
            channel_count = int(playback_device.get("channels") or 0)
        except (TypeError, ValueError, OverflowError):
            channel_count = 0
        input_source_name = (
            str(input_source).strip() if input_source is not None else self.load_input_source_name(sink_name)
        )
        with self._condition:
            self.configure_object(object_name="output", source_name=monitor_source, channel_count=channel_count)
            self.configure_object(object_name="input", source_name=input_source_name, channel_count=0)

    def configure_object(self, *, object_name: str, source_name: str, channel_count: int) -> None:
        capture = self._captures[object_name]
        if source_name == capture.source_name and channel_count == capture.configured_channel_count:
            return
        self._stop_capture_locked(object_name)
        capture.sample_frames.clear()  # type: ignore[union-attr]
        capture.source_name = source_name
        capture.channel_count = channel_count
        capture.configured_channel_count = channel_count
        capture.sample_rate = 0
        self._source_specs = None
        is_configured = bool(source_name) and (channel_count > 0 or object_name != "output")
        capture.status = "ready" if is_configured else "not_configured"
        capture.error = "" if is_configured else f"playback_{'monitor' if object_name == 'output' else 'input'}_not_configured"

    @classmethod
    def _load_pcm_frames(
        cls, payload: bytes, *, channel_count: int, sample_format: str = "s16le"
    ) -> list[tuple[float, ...]]:
        type_code, sample_width, full_scale = cls._PCM_FORMATS[sample_format]
        frame_width = channel_count * sample_width
        frame_total = len(payload) // frame_width
        if frame_total <= 0:
            return []
        values = struct.unpack_from(f"<{frame_total * channel_count}{type_code}", payload)
        scale = 1.0 / full_scale
        return [
            tuple(value * scale for value in values[offset : offset + channel_count])
            for offset in range(0, len(values), channel_count)
        ]

    def _load_source_specs(self) -> dict[str, dict[str, Any]]:
        if not shutil.which("pactl"):
            raise RuntimeError("pactl_unavailable")
        try:
            result = subprocess.run(
                ["pactl", "list", "sources"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("pipewire_sources_unavailable") from exc
        if result.returncode != 0:
            raise RuntimeError("pipewire_sources_unavailable")

        source_specs: dict[str, dict[str, Any]] = {}
        for source_block in re.split(r"(?m)^\s*Source #\d+", result.stdout):
            name_match = re.search(r"(?m)^\s*Name:\s*(.*?)\s*$", source_block)
            sample_match = re.search(
                r"(?m)^\s*Sample Specification:\s+(\S+)\s+(\d+)ch\s+(\d+)Hz\s*$",
                source_block,
            )
            if not name_match or not sample_match:
                continue
            channel_count = int(sample_match.group(2))
            channel_map_match = re.search(r"(?m)^\s*Channel Map:\s*(\S+)\s*$", source_block)
            channel_map = channel_map_match.group(1).strip() if channel_map_match else ""
            if len(channel_map.split(",")) != channel_count:
                channel_map = ""
            source_specs[name_match.group(1).strip()] = {
                "device_format": sample_match.group(1).strip(),
                "channel_count": channel_count,
                "sample_rate": int(sample_match.group(3)),
                "channel_map": channel_map,
            }
        return source_specs

    def _load_object_sample_spec(self, object_name: str) -> dict[str, Any]:
        capture = self._captures[object_name]
        error_prefix = "playback_monitor" if object_name == "output" else "playback_input"
        now = time.monotonic()
        if self._source_specs is None or now - self._source_specs_loaded_at > self._SOURCE_SPEC_TTL_SECONDS:
            self._source_specs = self._load_source_specs()
            self._source_specs_loaded_at = now
        sample_spec = self._source_specs.get(capture.source_name)
        if not sample_spec:
            raise RuntimeError(f"{error_prefix}_source_unavailable")
        if capture.configured_channel_count > 0 and sample_spec["channel_count"] != capture.configured_channel_count:
            raise RuntimeError(f"{error_prefix}_channel_count_mismatch")
        if sample_spec["sample_rate"] <= 0:
            raise RuntimeError(f"invalid_{error_prefix}_sample_rate")
        return sample_spec

    def _load_monitor_sample_spec(self) -> dict[str, Any]:
        return self._load_object_sample_spec("output")

    def _load_monitor_sample_rate(self) -> int:
        return int(self._load_monitor_sample_spec()["sample_rate"])

    def _load_source_catalog_locked(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for object_name, capture in self._captures.items():
            if not capture.source_name:
                continue
            channel_count = capture.channel_count
            if channel_count <= 0:
                try:
                    channel_count = int(self._load_object_sample_spec(object_name)["channel_count"])
                except RuntimeError:
                    continue
                capture.channel_count = channel_count
            prefix = self._OBJECT_SOURCE_PREFIXES[object_name]
            label = self._OBJECT_SOURCE_LABELS[object_name]
            catalog.extend(
                {
                    "id": f"{prefix}{channel_index + 1}",
                    "label": f"{label} {channel_index + 1}",
                    "kind": object_name,
                    "channel": channel_index,
                }
                for channel_index in range(channel_count)
            )
        return catalog

    @classmethod
    def load_scope_request(cls, request: dict[str, Any] | None = None) -> dict[str, Any]:
        values = request if isinstance(request, dict) else {}

        def first_value(key: str) -> Any:
            value = values.get(key)
            if isinstance(value, list) and value and not isinstance(value[0], (list, dict)) and key != "channels":
                return value[0]
            return value

        raw_channels = values.get("channels")
        if isinstance(raw_channels, str):
            channel_ids = raw_channels.split(",")
        elif isinstance(raw_channels, list):
            channel_ids = [part for item in raw_channels for part in str(item).split(",")]
        else:
            channel_ids = []
        channel_ids = [str(item).strip().casefold() for item in channel_ids][: cls._SLOT_COUNT]
        channel_ids = [item if re.fullmatch(r"(out|in)\d{1,2}|off", item) else "off" for item in channel_ids]

        def bounded_number(key: str, default: float, low: float, high: float) -> float:
            try:
                number = float(first_value(key))
            except (TypeError, ValueError, OverflowError):
                return default
            return min(high, max(low, number)) if math.isfinite(number) else default

        return {
            "channels": channel_ids,
            "window_ms": bounded_number("window_ms", 0.0, cls._MIN_WINDOW_MS, cls._MAX_WINDOW_MS)
            if first_value("window_ms") not in (None, "")
            else 0.0,
            "points": int(
                bounded_number("points", cls._DEFAULT_POINT_COUNT, cls._MIN_POINT_COUNT, cls._MAX_POINT_COUNT)
            ),
            "trigger": str(first_value("trigger") or "rising").strip().casefold() != "off",
        }

    def _start_capture_locked(self, object_name: str = "output") -> None:
        capture = self._captures[object_name]
        error_prefix = "playback_monitor" if object_name == "output" else "playback_input"
        if not capture.source_name or (object_name == "output" and capture.channel_count <= 0):
            raise RuntimeError(f"{error_prefix}_not_configured")
        if not shutil.which("parec"):
            raise RuntimeError("parec_unavailable")
        sample_spec = self._load_object_sample_spec(object_name)
        sample_rate = int(sample_spec["sample_rate"])
        channel_count = int(sample_spec["channel_count"])
        command = [
            "parec",
            f"--device={capture.source_name}",
            f"--format={capture.sample_format}",
            f"--rate={sample_rate}",
            f"--channels={channel_count}",
            "--latency-msec=20",
            "--process-time-msec=10",
            "--raw",
        ]
        # Pro-audio monitors expose aux0..auxN positions; without the source map parec
        # remixes them onto front/rear positions and silences odd channels.
        if sample_spec.get("channel_map"):
            command.append(f"--channel-map={sample_spec['channel_map']}")
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise RuntimeError(f"{error_prefix}_capture_failed") from exc

        buffer_frames = max(self._BUFFER_FRAME_COUNT, int(sample_rate * self._BUFFER_SECONDS))
        capture.sample_frames = collections.deque(maxlen=buffer_frames)
        capture.process = process
        capture.channel_count = channel_count
        capture.channel_map = str(sample_spec.get("channel_map") or "")
        capture.sample_rate = sample_rate
        capture.last_request_at = time.monotonic()
        capture.status = "starting"
        capture.error = ""
        capture.reader_thread = threading.Thread(
            target=self._read_capture,
            args=(process, channel_count, sample_rate, object_name),
            name=f"webplayer-audio-scope-{object_name}",
            daemon=True,
        )
        capture.reader_thread.start()

    def _stop_capture_locked(self, object_name: str | None = None) -> None:
        object_names = [object_name] if object_name else list(self._captures)
        for name in object_names:
            capture = self._captures[name]
            process = capture.process
            capture.process = None
            if process is not None and hasattr(process, "poll") and process.poll() is None:
                process.terminate()

    def _read_capture(
        self,
        process: subprocess.Popen[bytes],
        channel_count: int,
        sample_rate: int,
        object_name: str = "output",
    ) -> None:
        capture = self._captures[object_name]
        sample_format = capture.sample_format
        output = process.stdout
        pending_bytes = bytearray()
        frame_width = channel_count * self._PCM_FORMATS[sample_format][1]
        failure = ""
        try:
            if output is None:
                raise RuntimeError("playback_monitor_capture_stream_unavailable")
            while process.poll() is None:
                with self._condition:
                    if process is not capture.process:
                        break
                    is_idle = time.monotonic() - capture.last_request_at > self._IDLE_TIMEOUT_SECONDS
                if is_idle:
                    break

                readable, _, _ = select.select([output], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(output.fileno(), frame_width * 1024)
                if not chunk:
                    break
                pending_bytes.extend(chunk)
                usable_length = len(pending_bytes) - (len(pending_bytes) % frame_width)
                if not usable_length:
                    continue
                frames = self._load_pcm_frames(
                    bytes(pending_bytes[:usable_length]),
                    channel_count=channel_count,
                    sample_format=sample_format,
                )
                del pending_bytes[:usable_length]
                with self._condition:
                    if process is not capture.process:
                        break
                    capture.sample_frames.extend(frames)  # type: ignore[union-attr]
                    capture.sample_rate = sample_rate
                    capture.status = "ready"
                    capture.error = ""
                    self._condition.notify_all()
        except (OSError, RuntimeError, ValueError) as exc:
            failure = str(exc) or "playback_monitor_capture_failed"
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            with self._condition:
                if process is capture.process:
                    capture.process = None
                    capture.reader_thread = None
                    is_idle = time.monotonic() - capture.last_request_at > self._IDLE_TIMEOUT_SECONDS
                    capture.status = "idle" if is_idle else "unavailable"
                    capture.error = "" if is_idle else failure or "playback_monitor_capture_stopped"
                    self._condition.notify_all()

    @staticmethod
    def _load_trigger_offset(samples: list[float], *, window_frames: int) -> int | None:
        """Return the start of the latest window whose first sample is a rising zero crossing."""
        hysteresis = PlaybackScopeService._TRIGGER_HYSTERESIS
        for offset in range(len(samples) - window_frames, 0, -1):
            if samples[offset - 1] < 0 <= samples[offset] and min(samples[max(0, offset - 8) : offset]) <= -hysteresis:
                return offset
        return None

    @classmethod
    def _load_channel_trace(cls, samples: list[float], *, point_count: int) -> dict[str, Any]:
        digits = cls._SAMPLE_DIGITS
        peak = max((abs(value) for value in samples), default=0.0)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) if samples else 0.0
        trace: dict[str, Any] = {
            "peak_db": round(20 * math.log10(peak), 1) if peak > 0 else None,
            "rms_db": round(20 * math.log10(rms), 1) if rms > 0 else None,
        }
        if len(samples) <= point_count:
            trace["samples"] = [round(value, digits) for value in samples]
            return trace
        bucket_edges = [round(index * len(samples) / point_count) for index in range(point_count + 1)]
        trace["min"] = [
            round(min(samples[bucket_edges[index] : bucket_edges[index + 1]]), digits) for index in range(point_count)
        ]
        trace["max"] = [
            round(max(samples[bucket_edges[index] : bucket_edges[index + 1]]), digits) for index in range(point_count)
        ]
        return trace

    def _ensure_capture_locked(self, object_name: str) -> str:
        capture = self._captures[object_name]
        capture.last_request_at = time.monotonic()
        if capture.process is not None:
            return ""
        try:
            self._start_capture_locked(object_name)
        except RuntimeError as exc:
            capture.status = "unavailable"
            capture.error = str(exc) or "playback_monitor_capture_failed"
            return capture.error
        return ""

    def load_scope_payload(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        scope_request = self.load_scope_request(request)
        output_capture = self._captures["output"]
        with self._condition:
            if not output_capture.source_name or output_capture.channel_count <= 0:
                return {"ok": False, "status": output_capture.status, "error": output_capture.error}
            try:
                sources = self._load_source_catalog_locked()
            except RuntimeError as exc:
                return {"ok": False, "status": "unavailable", "error": str(exc)}
            source_index = {source["id"]: source for source in sources}
            channel_ids = scope_request["channels"] or list(self._DEFAULT_CHANNEL_IDS)
            missing_source = next(
                (
                    channel_id
                    for channel_id in channel_ids
                    if channel_id != "off" and channel_id not in source_index
                ),
                None,
            )
            if missing_source:
                return {
                    "ok": False,
                    "status": "unavailable",
                    "error": f"playback_scope_source_unavailable:{missing_source}",
                    "sources": sources,
                }
            slots = [
                (slot_index, source_index.get(channel_id))
                for slot_index, channel_id in enumerate(channel_ids)
                if channel_id != "off"
            ]
            object_names = sorted({source["kind"] for _slot, source in slots if source}, key=list(self._captures).index)
            object_errors = {object_name: self._ensure_capture_locked(object_name) for object_name in object_names}

            def required_frames(object_name: str) -> int:
                capture = self._captures[object_name]
                if not scope_request["window_ms"]:
                    return self._FRAME_COUNT
                window_frames = max(2, round(scope_request["window_ms"] * capture.sample_rate / 1000))
                return window_frames * (2 if scope_request["trigger"] else 1)

            deadline = time.monotonic() + self._STARTUP_TIMEOUT_SECONDS
            while True:
                pending = [
                    object_name
                    for object_name in object_names
                    if not object_errors[object_name]
                    and self._captures[object_name].process is not None
                    and len(self._captures[object_name].sample_frames) < required_frames(object_name)  # type: ignore[arg-type]
                ]
                remaining = deadline - time.monotonic()
                if not pending or remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)

            snapshots: dict[str, tuple[list[tuple[float, ...]], int]] = {}
            for object_name in object_names:
                capture = self._captures[object_name]
                frames = list(capture.sample_frames)  # type: ignore[arg-type]
                if frames:
                    snapshots[object_name] = (frames, capture.sample_rate)
                elif not object_errors[object_name]:
                    object_errors[object_name] = capture.error or "waiting_for_pcm_samples"

        if not snapshots:
            first_error = next((error for error in object_errors.values() if error), "waiting_for_pcm_samples")
            return {"ok": False, "status": output_capture.status, "error": first_error, "sources": sources}

        trigger_slot = next(
            ((slot_index, source) for slot_index, source in slots if source and source["kind"] in snapshots),
            None,
        )
        window_by_object: dict[str, tuple[int, int]] = {}
        triggered = False
        for object_name, (frames, sample_rate) in snapshots.items():
            window_frames = (
                max(2, round(scope_request["window_ms"] * sample_rate / 1000))
                if scope_request["window_ms"]
                else self._FRAME_COUNT
            )
            window_frames = min(window_frames, len(frames))
            start = len(frames) - window_frames
            if scope_request["trigger"] and trigger_slot and trigger_slot[1]["kind"] == object_name:
                trigger_samples = [frame[trigger_slot[1]["channel"]] for frame in frames]
                trigger_offset = self._load_trigger_offset(trigger_samples, window_frames=window_frames)
                if trigger_offset is not None:
                    start = trigger_offset
                    triggered = True
            window_by_object[object_name] = (start, window_frames)

        channels: list[dict[str, Any]] = []
        for slot_index, source in slots:
            if not source:
                continue
            object_name = source["kind"]
            if object_name not in snapshots:
                channels.append(
                    {
                        "index": slot_index,
                        "source": source["id"],
                        "label": source["label"],
                        "error": object_errors.get(object_name) or "waiting_for_pcm_samples",
                    }
                )
                continue
            frames, sample_rate = snapshots[object_name]
            start, window_frames = window_by_object[object_name]
            samples = [frame[source["channel"]] for frame in frames[start : start + window_frames]]
            channels.append(
                {
                    "index": slot_index,
                    "source": source["id"],
                    "label": source["label"],
                    "sample_rate_hz": sample_rate,
                    "frame_count": len(samples),
                    **self._load_channel_trace(samples, point_count=scope_request["points"]),
                }
            )

        primary_object = trigger_slot[1]["kind"] if trigger_slot else next(iter(snapshots))
        primary_frames, primary_rate = snapshots[primary_object]
        primary_window = window_by_object[primary_object][1]
        return {
            "ok": True,
            "status": "ready",
            "sample_rate_hz": primary_rate,
            "bit_depth": 24,
            "sample_format": output_capture.sample_format,
            "window_ms": primary_window * 1000.0 / primary_rate,
            "frame_count": primary_window,
            "points": scope_request["points"],
            "triggered": triggered,
            "trigger_source": trigger_slot[1]["id"] if trigger_slot and scope_request["trigger"] else "",
            "sources": sources,
            "channels": channels,
        }


PLAYBACK_SCOPE_SERVICE = PlaybackScopeService()
MCP_REQUEST_SERVICE = WebPlayerMcpRequestService()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")


class McpTcpRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            raw_line = self.rfile.readline()
            if not raw_line:
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                request_payload = json.loads(line)
            except Exception:
                self.wfile.write(_json_bytes({"error": "invalid_json"}))
                continue

            response_payload = MCP_REQUEST_SERVICE.dispatch_object(request_payload)
            self.wfile.write(_json_bytes(response_payload))


class McpTcpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class McpHttpRequestHandler(BaseHTTPRequestHandler):
    server_version = f"webplayer-mcp-http/{WEBPLAYER_MCP_VERSION}"

    @staticmethod
    def _load_normalized_request_path(raw_path: str) -> str:
        return str(urlparse(str(raw_path or "")).path or "").rstrip("/")

    @staticmethod
    def _load_jsonrpc_error_payload(*, request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        error_payload: dict[str, Any] = {
            "code": int(code),
            "message": str(message or "error"),
        }
        if data is not None:
            error_payload["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error_payload,
        }

    def _load_request_header_value(self, header_name: str) -> str:
        target_name = str(header_name or "").strip().casefold()
        if not target_name:
            return ""
        for key, value in self.headers.items():
            if str(key or "").strip().casefold() == target_name:
                return str(value or "").strip()
        return ""

    def _has_streamable_http_headers(self) -> bool:
        return any(
            bool(self._load_request_header_value(header_name))
            for header_name in ("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name")
        )

    def _load_accept_header_values(self) -> list[str]:
        raw_accept_header = self._load_request_header_value("Accept")
        return [item.strip().casefold() for item in str(raw_accept_header or "").split(",") if item.strip()]

    def _wants_sse_stream(self) -> bool:
        for accept_item in self._load_accept_header_values():
            if accept_item == "text/event-stream" or accept_item.startswith("text/event-stream;"):
                return True
        return False

    @staticmethod
    def _load_sse_bootstrap_body(message_path: str = "/mcp") -> str:
        normalized_message_path = str(message_path or "/mcp").strip() or "/mcp"
        if not normalized_message_path.startswith("/"):
            normalized_message_path = "/" + normalized_message_path
        return f"event: endpoint\ndata: {normalized_message_path}\n\n"

    @staticmethod
    def _decode_sentinel_header_value(raw_value: str) -> str:
        value = str(raw_value or "")
        if value.startswith("=?base64?") and value.endswith("?="):
            encoded_payload = value[len("=?base64?") : -2]
            decoded = base64.b64decode(encoded_payload.encode("ascii"), validate=True)
            return decoded.decode("utf-8", errors="strict")
        return value

    @staticmethod
    def _load_local_origin_hostnames() -> set[str]:
        return {"localhost", "127.0.0.1", "::1"}

    def _load_request_origin_hostnames(self) -> set[str]:
        hostnames = set(self._load_local_origin_hostnames())
        for header_name in ("Host", "X-Forwarded-Host"):
            raw_header_value = self._load_request_header_value(header_name)
            if not raw_header_value:
                continue
            for host_candidate in str(raw_header_value).split(","):
                parsed_host = urlparse(f"//{str(host_candidate or '').strip()}")
                hostname = str(parsed_host.hostname or "").strip().casefold()
                if hostname:
                    hostnames.add(hostname)
        return hostnames

    @staticmethod
    def _origin_matches_allowlist_entry(origin: str, allowlist_entry: str) -> bool:
        entry = str(allowlist_entry or "").strip()
        if not entry:
            return False
        if entry == "*":
            return True
        return fnmatch.fnmatch(str(origin or "").casefold(), entry.casefold())

    def _origin_is_allowed(self, raw_origin: str) -> bool:
        origin = str(raw_origin or "").strip()
        if not origin:
            return True

        if origin.casefold() == "null":
            return True

        configured_allowlist = [
            item.strip()
            for item in str(os.getenv("WEBPLAYER_MCP_ALLOWED_ORIGINS") or "").split(",")
            if item.strip()
        ]
        if configured_allowlist:
            return any(self._origin_matches_allowlist_entry(origin, entry) for entry in configured_allowlist)

        parsed_origin = urlparse(origin)
        origin_scheme = str(parsed_origin.scheme or "").strip().casefold()
        if origin_scheme and origin_scheme not in {"http", "https"}:
            return True

        origin_hostname = str(parsed_origin.hostname or "").strip().casefold()
        if not origin_hostname:
            return False

        if "vscode" in origin_hostname or origin_hostname.endswith(".vscode-cdn.net") or origin_hostname.endswith(".vscode-vfs.net"):
            return True

        if (
            origin_hostname in self._load_request_origin_hostnames()
            or origin_hostname.endswith(".localhost")
            or origin_hostname.endswith(".local")
        ):
            return True

        try:
            ip_obj = ipaddress.ip_address(origin_hostname)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                return True
        except ValueError:
            pass

        return False

    def _load_streamable_http_validation_error(self, request_payload: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
        if str(request_payload.get("jsonrpc") or "").strip() != "2.0":
            return None

        request_id = request_payload.get("id")
        origin_header = self._load_request_header_value("Origin")
        if origin_header and not self._origin_is_allowed(origin_header):
            return (
                self._load_jsonrpc_error_payload(
                    request_id=request_id,
                    code=-32010,
                    message="Forbidden origin",
                ),
                403,
            )

        params_payload = request_payload.get("params")
        if not isinstance(params_payload, dict):
            params_payload = {}
        meta_payload = params_payload.get("_meta")
        if not isinstance(meta_payload, dict):
            meta_payload = {}
        body_protocol_version = str(meta_payload.get("io.modelcontextprotocol/protocolVersion") or "").strip()
        initialize_protocol_version = str(params_payload.get("protocolVersion") or "").strip()
        protocol_header = self._load_request_header_value("MCP-Protocol-Version")
        request_method = str(request_payload.get("method") or "").strip()

        if protocol_header and body_protocol_version and body_protocol_version != protocol_header:
            return (
                self._load_jsonrpc_error_payload(
                    request_id=request_id,
                    code=-32020,
                    message=(
                        "Header mismatch: MCP-Protocol-Version header does not match "
                        "params._meta.io.modelcontextprotocol/protocolVersion"
                    ),
                ),
                400,
            )

        if protocol_header and initialize_protocol_version and initialize_protocol_version != protocol_header:
            return (
                self._load_jsonrpc_error_payload(
                    request_id=request_id,
                    code=-32020,
                    message="Header mismatch: MCP-Protocol-Version header does not match params.protocolVersion",
                ),
                400,
            )

        if request_method != "initialize" and self._has_streamable_http_headers() and not protocol_header:
            return (
                self._load_jsonrpc_error_payload(
                    request_id=request_id,
                    code=-32020,
                    message="Header mismatch: missing MCP-Protocol-Version header",
                ),
                400,
            )

        negotiated_protocol_version = (
            protocol_header or initialize_protocol_version or body_protocol_version
        )
        if (
            negotiated_protocol_version
            and negotiated_protocol_version not in WebPlayerMcpRequestService._SUPPORTED_PROTOCOL_VERSIONS
        ):
            return (
                self._load_jsonrpc_error_payload(
                    request_id=request_id,
                    code=-32022,
                    message=f"Unsupported protocol version: {negotiated_protocol_version}",
                    data={"supportedVersions": list(WebPlayerMcpRequestService._SUPPORTED_PROTOCOL_VERSIONS)},
                ),
                400,
            )

        method_header = self._load_request_header_value("Mcp-Method")
        if method_header and method_header != request_method:
            return (
                self._load_jsonrpc_error_payload(
                    request_id=request_id,
                    code=-32020,
                    message=(
                        f"Header mismatch: Mcp-Method header '{method_header}' does not "
                        f"match body value '{request_method}'"
                    ),
                ),
                400,
            )

        expected_name = ""
        if request_method in {"tools/call", "prompts/get", "resources/read"}:
            if request_method == "resources/read":
                expected_name = str(params_payload.get("uri") or "").strip()
            else:
                expected_name = str(params_payload.get("name") or "").strip()

            name_header = self._load_request_header_value("Mcp-Name")
            if name_header:
                try:
                    normalized_name_header = self._decode_sentinel_header_value(name_header)
                except Exception:
                    return (
                        self._load_jsonrpc_error_payload(
                            request_id=request_id,
                            code=-32020,
                            message="Header mismatch: invalid Mcp-Name encoding",
                        ),
                        400,
                    )
                if expected_name and normalized_name_header != expected_name:
                    return (
                        self._load_jsonrpc_error_payload(
                            request_id=request_id,
                            code=-32020,
                            message=(
                                f"Header mismatch: Mcp-Name header '{normalized_name_header}' "
                                f"does not match body value '{expected_name}'"
                            ),
                        ),
                        400,
                    )

        return None

    @staticmethod
    def _load_http_status_code(response_payload: dict[str, Any]) -> int:
        if "error" in response_payload:
            error_payload = response_payload.get("error")
            if isinstance(error_payload, dict) and int(error_payload.get("code") or 0) == -32601:
                return 404
            return 400
        if "result" in response_payload:
            return 200
        return 400

    def _send_cors_headers(self) -> None:
        origin_header = self._load_request_header_value("Origin")
        if origin_header and self._origin_is_allowed(origin_header):
            self.send_header("Access-Control-Allow-Origin", origin_header)
            self.send_header("Access-Control-Allow-Credentials", "true")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS, HEAD")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Accept, MCP-Protocol-Version, Mcp-Method, Mcp-Name, MCP-Session-Id, Authorization",
        )
        self.send_header("Access-Control-Expose-Headers", "MCP-Session-Id, Content-Type, MCP-Protocol-Version")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _write_json_response(
        self,
        payload: dict[str, Any],
        status: int = 200,
        response_headers: dict[str, str] | None = None,
        *,
        compress: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        use_gzip = compress and len(body) >= 4096 and self._accepts_gzip()
        if use_gzip:
            body = gzip.compress(body, compresslevel=1)
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if compress:
            self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        for header_name, header_value in (response_headers or {}).items():
            self.send_header(str(header_name), str(header_value))
        self.end_headers()
        self.wfile.write(body)

    def _accepts_gzip(self) -> bool:
        for entry in self._load_request_header_value("Accept-Encoding").split(","):
            encoding, *parameters = entry.strip().split(";")
            if encoding.strip().casefold() != "gzip":
                continue
            quality = 1.0
            for parameter in parameters:
                name, separator, value = parameter.partition("=")
                if separator and name.strip().casefold() == "q":
                    try:
                        quality = float(value.strip())
                    except ValueError:
                        return False
            return 0 < quality <= 1
        return False

    def _write_binary_response(self, payload: bytes, *, content_type: str, status: int = 200) -> None:
        body = bytes(payload or b"")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", str(content_type or "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _load_cover_source_for_player(*, player_selector: str) -> str:
        selector_prefix = str(player_selector or "chromium").strip() or "chromium"
        try:
            players_result = subprocess.run(
                ["playerctl", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            return ""

        players = [line.strip() for line in str(players_result.stdout or "").splitlines() if line.strip()]
        if not players:
            return ""

        selected_player = load_selected_player_name(players=players, player_selector=selector_prefix)
        if not selected_player:
            return ""

        try:
            cover_result = subprocess.run(
                ["playerctl", "-p", selected_player, "metadata", "mpris:artUrl"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            return ""

        return str(cover_result.stdout or "").strip()

    @staticmethod
    def _load_cover_file_path(*, source_value: str, player_selector: str, proc_root: str = "/proc") -> str:
        source_path = urlparse(source_value).path if source_value.casefold().startswith("file://") else source_value
        source_path = str(source_path or "").strip()
        normalized_path = os.path.realpath(source_path)
        if normalized_path.startswith("/tmp/") and os.path.isfile(normalized_path):
            return normalized_path

        if not re.fullmatch(r"/tmp/\.org\.chromium\.Chromium\.[A-Za-z0-9]+", source_path):
            return ""
        try:
            players_result = subprocess.run(
                ["playerctl", "-l"], capture_output=True, text=True, check=False, timeout=5
            )
        except Exception:
            return ""
        players = [line.strip() for line in str(players_result.stdout or "").splitlines() if line.strip()]
        selected_player = load_selected_player_name(players=players, player_selector=player_selector)
        player_match = re.fullmatch(r"chromium\.instance([0-9]+)", selected_player)
        if not player_match:
            return ""
        namespace_path = os.path.join(proc_root, player_match.group(1), "root", source_path.lstrip("/"))
        try:
            if os.path.isfile(namespace_path) and os.stat(namespace_path).st_uid == os.geteuid():
                return namespace_path
        except OSError:
            pass
        return ""

    @staticmethod
    def _load_cover_placeholder_svg() -> bytes:
        return (
            "<svg xmlns='http://www.w3.org/2000/svg' width='112' height='112' viewBox='0 0 112 112'>"
            "<path d='M52 24v41a12 12 0 1 1-8-11V34l36-9v33a11 11 0 1 1-8-10V16z' fill='#ffffff'/>"
            "</svg>"
        ).encode("utf-8")

    @staticmethod
    def _load_spectrum_timeout_seconds() -> int:
        raw_timeout = str(os.getenv("WEBPLAYER_EQ_ANALYZER_TIMEOUT_S") or "").strip()
        try:
            timeout_s = int(raw_timeout)
        except Exception:
            timeout_s = 3
        return max(1, min(20, timeout_s))

    @staticmethod
    def _load_spectrum_payload_from_text(raw_text: str) -> Any:
        text_value = str(raw_text or "").strip()
        if not text_value:
            return {}

        try:
            return json.loads(text_value)
        except Exception:
            pass

        object_match = re.search(r"\{[\s\S]*\}", text_value)
        if object_match:
            try:
                return json.loads(object_match.group(0))
            except Exception:
                pass

        array_match = re.search(r"\[[\s\S]*\]", text_value)
        if array_match:
            try:
                return json.loads(array_match.group(0))
            except Exception:
                pass

        numeric_values = re.findall(r"-?\d+(?:\.\d+)?", text_value)
        if numeric_values:
            return {"bars": [float(value) for value in numeric_values]}

        return {}

    @staticmethod
    def _load_numeric_sequence(raw_value: Any) -> list[float]:
        if not isinstance(raw_value, (list, tuple)):
            return []

        numeric_list: list[float] = []
        for item in raw_value:
            try:
                number_value = float(item)
            except Exception:
                continue
            if number_value != number_value:
                continue
            if number_value == float("inf") or number_value == float("-inf"):
                continue
            numeric_list.append(number_value)
        return numeric_list

    @classmethod
    def _load_spectrum_bars_from_payload(cls, raw_payload: Any, *, target_count: int = 48) -> list[float]:
        candidate_keys = (
            "bins_l",
            "bins_r",
            "levels_l_db",
            "levels_r_db",
            "levels_l",
            "levels_r",
            "bars",
            "values",
            "graph_data",
            "graphData",
            "graph",
            "spectrum",
            "spectrum_data",
            "fft",
            "bands",
            "bins",
            "magnitudes",
            "amplitudes",
            "points",
            "y",
            "data",
            "bands_hz",
        )

        if isinstance(raw_payload, str):
            parsed_payload = cls._load_spectrum_payload_from_text(raw_payload)
        else:
            parsed_payload = raw_payload

        stack: list[Any] = [parsed_payload]
        visited: set[int] = set()

        while stack:
            current = stack.pop(0)
            current_id = id(current)
            if current_id in visited:
                continue
            visited.add(current_id)

            numeric_sequence = cls._load_numeric_sequence(current)
            if numeric_sequence:
                return cls._normalize_spectrum_bars(numeric_sequence, target_count=target_count)

            if isinstance(current, dict):
                for key in candidate_keys:
                    if key in current:
                        stack.append(current.get(key))
                for nested_value in current.values():
                    if isinstance(nested_value, (dict, list, tuple)):
                        stack.append(nested_value)

        return []

    @staticmethod
    def _normalize_spectrum_bars(raw_values: list[float], *, target_count: int) -> list[float]:
        if not raw_values:
            return []

        values = list(raw_values)
        min_value = min(values)
        max_value = max(values)
        normalized: list[float] = []

        if min_value < 0.0:
            range_value = max_value - min_value
            if range_value <= 1e-9:
                normalized = [0.0 for _ in values]
            else:
                normalized = [(value - min_value) / range_value for value in values]
        elif max_value > 1.0:
            scale = max(max_value, 1e-9)
            normalized = [value / scale for value in values]
        else:
            normalized = list(values)

        clipped = [max(0.0, min(1.0, float(value))) for value in normalized]
        if not clipped:
            return []

        desired_count = max(8, min(96, int(target_count)))
        if len(clipped) == desired_count:
            return clipped
        if len(clipped) == 1:
            return [clipped[0] for _ in range(desired_count)]

        last_index = len(clipped) - 1
        resampled: list[float] = []
        for index in range(desired_count):
            mapped_index = int(round((index * last_index) / max(1, desired_count - 1)))
            mapped_index = max(0, min(last_index, mapped_index))
            resampled.append(clipped[mapped_index])
        return resampled

    @classmethod
    def _load_spectrum_payload_from_url(cls, *, analyzer_url: str, player_selector: str) -> dict[str, Any]:
        timeout_s = cls._load_spectrum_timeout_seconds()
        parsed_url = urlparse(str(analyzer_url or "").strip())
        if parsed_url.scheme not in {"http", "https"}:
            return {
                "ok": False,
                "error": "invalid_analyzer_url",
                "source": "url",
                "bars": [],
            }

        query_payload = parse_qs(parsed_url.query, keep_blank_values=True)
        query_payload["player_selector"] = [str(player_selector or "chromium").strip() or "chromium"]
        query_payload["format"] = ["json"]
        request_url = urlunparse(parsed_url._replace(query=urlencode(query_payload, doseq=True)))

        try:
            request = Request(request_url, headers={"Accept": "application/json"}, method="GET")
            with urlopen(request, timeout=timeout_s) as response:
                content_type = str(getattr(response, "headers", {}).get("Content-Type") or "").strip().lower()
                if "text/event-stream" in content_type:
                    # Read the first SSE event and parse its data payload as JSON.
                    event_data_lines: list[str] = []
                    deadline = time.monotonic() + max(0.5, float(timeout_s) - 0.2)
                    while time.monotonic() < deadline:
                        raw_line = response.readline(4096)
                        if not raw_line:
                            break
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line:
                            if event_data_lines:
                                break
                            continue
                        if line.startswith("data:"):
                            event_data_lines.append(line[5:].lstrip())
                    response_text = "\n".join(event_data_lines).strip()
                else:
                    response_text = response.read(300_000).decode("utf-8", errors="replace")
        except Exception as exc:
            return {
                "ok": False,
                "error": f"analyzer_url_failed:{type(exc).__name__}:{exc}",
                "source": "url",
                "bars": [],
            }

        parsed_payload = cls._load_spectrum_payload_from_text(response_text)
        bars = cls._load_spectrum_bars_from_payload(parsed_payload)
        return {
            "ok": bool(bars),
            "source": "url",
            "url": request_url,
            "bars": bars,
            "raw": parsed_payload,
        }

    @classmethod
    def _load_spectrum_payload_from_autodiscovered_url(cls, *, player_selector: str) -> dict[str, Any]:
        candidate_urls = [
            "http://127.0.0.1:8767/api/snapshot",
            "http://localhost:8767/api/snapshot",
            "http://127.0.0.1:8767/api/spectrum_stream",
            "http://localhost:8767/api/spectrum_stream",
        ]

        last_error_payload: dict[str, Any] | None = None
        for candidate_url in candidate_urls:
            candidate_payload = cls._load_spectrum_payload_from_url(
                analyzer_url=candidate_url,
                player_selector=player_selector,
            )
            if candidate_payload.get("ok"):
                candidate_payload["source"] = "url_autodiscovery"
                return candidate_payload
            last_error_payload = candidate_payload

        if isinstance(last_error_payload, dict):
            last_error_payload["source"] = "url_autodiscovery"
            return last_error_payload
        return {
            "ok": False,
            "error": "analyzer_url_autodiscovery_failed",
            "source": "url_autodiscovery",
            "bars": [],
        }

    @classmethod
    def _load_spectrum_payload_from_command(cls, *, analyzer_command: str, player_selector: str) -> dict[str, Any]:
        timeout_s = cls._load_spectrum_timeout_seconds()
        command_text = str(analyzer_command or "").strip()
        if not command_text:
            return {
                "ok": False,
                "error": "missing_analyzer_command",
                "source": "command",
                "bars": [],
            }

        command_environment = dict(os.environ)
        command_environment["WEBPLAYER_EQ_PLAYER_SELECTOR"] = str(player_selector or "chromium").strip() or "chromium"
        command_environment["PLAYER_SELECTOR"] = command_environment["WEBPLAYER_EQ_PLAYER_SELECTOR"]

        try:
            command_result = subprocess.run(
                ["bash", "-lc", command_text],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
                env=command_environment,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"analyzer_command_failed:{type(exc).__name__}:{exc}",
                "source": "command",
                "bars": [],
            }

        parsed_payload = cls._load_spectrum_payload_from_text(str(command_result.stdout or ""))
        bars = cls._load_spectrum_bars_from_payload(parsed_payload)
        if command_result.returncode != 0 and not bars:
            return {
                "ok": False,
                "error": f"analyzer_command_exit_{int(command_result.returncode)}",
                "stderr": str(command_result.stderr or "").strip(),
                "source": "command",
                "bars": [],
            }

        return {
            "ok": bool(bars),
            "source": "command",
            "bars": bars,
            "raw": parsed_payload,
            "stderr": str(command_result.stderr or "").strip(),
            "return_code": int(command_result.returncode),
        }

    @staticmethod
    def _load_spectrum_payload_from_file(*, file_path: str, source: str = "file") -> dict[str, Any]:
        normalized_file_path = os.path.realpath(os.path.expanduser(str(file_path or "").strip()))
        if not normalized_file_path or not os.path.isfile(normalized_file_path):
            return {
                "ok": False,
                "error": "analyzer_file_not_found",
                "source": source,
                "bars": [],
                "file": normalized_file_path,
            }

        try:
            with open(normalized_file_path, "r", encoding="utf-8", errors="replace") as file_handle:
                raw_text = file_handle.read(300_000)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"analyzer_file_read_failed:{type(exc).__name__}:{exc}",
                "source": source,
                "bars": [],
                "file": normalized_file_path,
            }

        parsed_payload = McpHttpRequestHandler._load_spectrum_payload_from_text(raw_text)
        bars = McpHttpRequestHandler._load_spectrum_bars_from_payload(parsed_payload)
        return {
            "ok": bool(bars),
            "source": source,
            "bars": bars,
            "raw": parsed_payload,
            "file": normalized_file_path,
        }

    @classmethod
    def _load_autodiscovered_spectrum_file_path(cls) -> str:
        home_dir = os.path.expanduser("~")
        module_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
        candidate_paths = [
            "/tmp/aiequalizer_graph.json",
            "/tmp/aiequalizer_spectrum.json",
            "/tmp/aieq_graph.json",
            "/tmp/aieq_spectrum.json",
            "/tmp/eq_spectrum.json",
            "/tmp/eq_graph_data.json",
            os.path.join(home_dir, "aiequalizer", "graph_data.json"),
            os.path.join(home_dir, "aiequalizer", "spectrum.json"),
            os.path.join(home_dir, "aiequalizer", "output", "graph_data.json"),
            os.path.join(home_dir, "aiequalizer", "output", "spectrum.json"),
            os.path.join(module_root, "AppData", "aiequalizer_graph.json"),
            os.path.join(module_root, "AppData", "spectrum_graph.json"),
        ]

        for path_candidate in candidate_paths:
            normalized_candidate = os.path.realpath(os.path.expanduser(path_candidate))
            if os.path.isfile(normalized_candidate):
                return normalized_candidate

        glob_patterns = [
            "/tmp/*spectrum*.json",
            "/tmp/*eq*graph*.json",
            "/var/tmp/*spectrum*.json",
            os.path.join(home_dir, "**", "*spectrum*.json"),
            os.path.join(home_dir, "**", "*eq*graph*.json"),
        ]
        matched_files: list[str] = []
        for pattern in glob_patterns:
            recursive_search = "**" in pattern
            for match_path in glob.glob(pattern, recursive=recursive_search):
                normalized_match = os.path.realpath(os.path.expanduser(match_path))
                if os.path.isfile(normalized_match):
                    matched_files.append(normalized_match)

        if not matched_files:
            return ""

        matched_files = sorted(
            set(matched_files),
            key=lambda path_value: os.path.getmtime(path_value) if os.path.exists(path_value) else 0.0,
            reverse=True,
        )
        return matched_files[0] if matched_files else ""

    @staticmethod
    def _load_player_status_for_selector(*, player_selector: str) -> str:
        selector_prefix = str(player_selector or "chromium").strip() or "chromium"
        try:
            players_result = subprocess.run(
                ["playerctl", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except Exception:
            return "unknown"

        players = [line.strip() for line in str(players_result.stdout or "").splitlines() if line.strip()]
        if not players:
            return "unknown"

        selected_player = load_selected_player_name(players=players, player_selector=selector_prefix)
        if not selected_player:
            return "unknown"

        try:
            status_result = subprocess.run(
                ["playerctl", "-p", selected_player, "status"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except Exception:
            return "unknown"

        status_text = str(status_result.stdout or "").strip().lower()
        if status_text in {"playing", "paused", "stopped"}:
            return status_text
        return "unknown"

    @staticmethod
    def _load_spectrum_payload_synthetic(*, player_selector: str) -> dict[str, Any]:
        status_value = McpHttpRequestHandler._load_player_status_for_selector(player_selector=player_selector)
        is_playing = status_value == "playing"
        sample_count = 48
        phase = float(time.time())
        bars: list[float] = []

        for index in range(sample_count):
            wave_a = 0.5 + 0.5 * math.sin((phase * 3.7) + (index * 0.31))
            wave_b = 0.5 + 0.5 * math.sin((phase * 6.1) + (index * 0.17))
            wave_c = 0.5 + 0.5 * math.sin((phase * 1.9) + (index * 0.53))
            if is_playing:
                value = 0.12 + (0.55 * wave_a) + (0.23 * wave_b) + (0.10 * wave_c)
            else:
                value = 0.02 + (0.08 * wave_a) + (0.04 * wave_b)
            bars.append(max(0.0, min(1.0, float(value))))

        return {
            "ok": True,
            "source": "synthetic",
            "status": status_value,
            "bars": bars,
        }

    @classmethod
    def _load_spectrum_payload_for_player(cls, *, player_selector: str) -> dict[str, Any]:
        normalized_selector = str(player_selector or "chromium").strip() or "chromium"
        analyzer_url = str(os.getenv("WEBPLAYER_EQ_ANALYZER_URL") or "").strip()
        analyzer_command = str(os.getenv("WEBPLAYER_EQ_ANALYZER_CMD") or "").strip()
        analyzer_file = str(os.getenv("WEBPLAYER_EQ_ANALYZER_FILE") or "").strip()

        if analyzer_url:
            payload = cls._load_spectrum_payload_from_url(analyzer_url=analyzer_url, player_selector=normalized_selector)
        elif analyzer_command:
            payload = cls._load_spectrum_payload_from_command(analyzer_command=analyzer_command, player_selector=normalized_selector)
        elif analyzer_file:
            payload = cls._load_spectrum_payload_from_file(file_path=analyzer_file, source="file")
        else:
            autodiscovered_url_payload = cls._load_spectrum_payload_from_autodiscovered_url(player_selector=normalized_selector)
            if autodiscovered_url_payload.get("ok"):
                payload = autodiscovered_url_payload
            else:
                autodiscovered_file = cls._load_autodiscovered_spectrum_file_path()
                if autodiscovered_file:
                    payload = cls._load_spectrum_payload_from_file(file_path=autodiscovered_file, source="file_autodiscovery")
                else:
                    payload = {
                        "ok": False,
                        "error": "spectrum_analyzer_not_configured",
                        "source": "none",
                        "bars": [],
                    }

        bars = cls._load_spectrum_bars_from_payload(payload.get("bars") if isinstance(payload, dict) else payload)
        if not bars and isinstance(payload, dict):
            bars = cls._load_spectrum_bars_from_payload(payload.get("raw"))

        source_value = str((payload or {}).get("source") or "none") if isinstance(payload, dict) else "none"
        fallback_payload: dict[str, Any] | None = None
        if not bars:
            fallback_payload = cls._load_spectrum_payload_synthetic(player_selector=normalized_selector)
            bars = cls._load_spectrum_bars_from_payload(fallback_payload.get("bars"))
            if bars:
                source_value = str(fallback_payload.get("source") or "synthetic")

        result_payload = {
            "ok": bool(bars),
            "player_selector": normalized_selector,
            "source": source_value,
            "bars": bars,
        }
        if fallback_payload and isinstance(payload, dict):
            result_payload["fallback_from_source"] = str(payload.get("source") or "none")
        if isinstance(payload, dict) and payload.get("error"):
            result_payload["error"] = str(payload.get("error"))
        if isinstance(payload, dict) and payload.get("stderr"):
            result_payload["stderr"] = str(payload.get("stderr"))
        if isinstance(payload, dict) and payload.get("file"):
            result_payload["file"] = str(payload.get("file"))
        if isinstance(payload, dict) and payload.get("url"):
            result_payload["url"] = str(payload.get("url"))
        return result_payload

    def _write_sse_response(self, sse_body: str, status: int = 200) -> None:
        body = str(sse_body or "").encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        request_path = self._load_normalized_request_path(self.path)
        ui_cover_path = WebPlayerMcpRequestService._UI_COVER_HTTP_PATH.rstrip("/")
        ui_spectrum_path = WebPlayerMcpRequestService._UI_SPECTRUM_HTTP_PATH.rstrip("/")
        ui_scope_path = WebPlayerMcpRequestService._UI_SCOPE_HTTP_PATH.rstrip("/")
        ui_scope_paths = {ui_scope_path, *WebPlayerMcpRequestService._UI_LEGACY_SCOPE_HTTP_PATHS}
        if request_path == "/health":
            self._write_json_response({"ok": True, "server": "webplayer-mcp-http"}, status=200)
            return

        if request_path == "":
            if self._wants_sse_stream():
                self._write_sse_response(self._load_sse_bootstrap_body("/mcp"), status=200)
                return

            self._write_json_response(
                {
                    "ok": True,
                    "server": "webplayer-mcp-http",
                    "mcpEndpoint": "/mcp",
                    "uiResourceUri": WebPlayerMcpRequestService._UI_RESOURCE_URI,
                    "healthEndpoint": "/health",
                },
                status=200,
            )
            return

        if request_path == ui_cover_path:
            parsed_request = urlparse(self.path)
            query_params = parse_qs(parsed_request.query, keep_blank_values=False)
            source_value = str((query_params.get("source") or [""])[0] or "").strip()
            player_selector = str((query_params.get("player_selector") or ["chromium"])[0] or "chromium").strip() or "chromium"
            if not source_value:
                source_value = self._load_cover_source_for_player(player_selector=player_selector)
            if not source_value:
                self._write_binary_response(
                    self._load_cover_placeholder_svg(),
                    content_type="image/svg+xml; charset=utf-8",
                    status=200,
                )
                return

            normalized_path = self._load_cover_file_path(source_value=source_value, player_selector=player_selector)
            if not normalized_path:
                fallback_source = self._load_cover_source_for_player(player_selector=player_selector)
                if fallback_source:
                    normalized_path = self._load_cover_file_path(
                        source_value=fallback_source, player_selector=player_selector
                    )

            if not os.path.isfile(normalized_path):
                self._write_binary_response(
                    self._load_cover_placeholder_svg(),
                    content_type="image/svg+xml; charset=utf-8",
                    status=200,
                )
                return

            try:
                with open(normalized_path, "rb") as cover_file:
                    cover_bytes = cover_file.read(2_000_000 + 1)
            except Exception:
                self._write_binary_response(
                    self._load_cover_placeholder_svg(),
                    content_type="image/svg+xml; charset=utf-8",
                    status=200,
                )
                return

            if len(cover_bytes) > 2_000_000:
                self._write_binary_response(
                    self._load_cover_placeholder_svg(),
                    content_type="image/svg+xml; charset=utf-8",
                    status=200,
                )
                return

            content_type = "application/octet-stream"
            if cover_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                content_type = "image/png"
            elif cover_bytes.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif cover_bytes.startswith(b"GIF87a") or cover_bytes.startswith(b"GIF89a"):
                content_type = "image/gif"
            elif cover_bytes.startswith(b"RIFF") and cover_bytes[8:12] == b"WEBP":
                content_type = "image/webp"

            self._write_binary_response(cover_bytes, content_type=content_type, status=200)
            return

        if request_path == ui_spectrum_path:
            parsed_request = urlparse(self.path)
            query_params = parse_qs(parsed_request.query, keep_blank_values=False)
            player_selector = str((query_params.get("player_selector") or ["chromium"])[0] or "chromium").strip() or "chromium"
            spectrum_payload = self._load_spectrum_payload_for_player(player_selector=player_selector)
            self._write_json_response(spectrum_payload, status=200)
            return

        if request_path in ui_scope_paths:
            origin_header = self._load_request_header_value("Origin")
            if origin_header and not self._origin_is_allowed(origin_header):
                self._write_json_response({"error": "forbidden_origin"}, status=403)
                return
            try:
                scope_query = parse_qs(urlparse(self.path).query, keep_blank_values=False)
                scope_payload = PLAYBACK_SCOPE_SERVICE.load_scope_payload(scope_query)
            except Exception as exc:
                self.log_error("Playback oscilloscope request failed: %s", exc)
                self._write_json_response(
                    {"ok": False, "status": "unavailable", "error": "scope_request_failed"},
                    status=503,
                    response_headers={"Cache-Control": "no-store"},
                )
                return
            self._write_json_response(
                scope_payload,
                status=200,
                response_headers={"Cache-Control": "no-store"},
                compress=True,
            )
            return

        self._write_json_response({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        request_path = self._load_normalized_request_path(self.path)
        if request_path not in {"", "/mcp"}:
            self._write_json_response({"error": "not_found"}, status=404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            content_length = 0

        raw_body = self.rfile.read(max(content_length, 0)) if content_length > 0 else b""
        if not raw_body:
            self._write_json_response({"error": "invalid_json"}, status=400)
            return

        try:
            request_payload = json.loads(raw_body.decode("utf-8", errors="replace"))
        except Exception:
            self._write_json_response({"error": "invalid_json"}, status=400)
            return

        request_method = str(request_payload.get("method") or "").strip().casefold()
        if (
            self._has_streamable_http_headers()
            or self._load_request_header_value("Origin")
            or request_method == "initialize"
        ):
            validation_error = self._load_streamable_http_validation_error(request_payload)
            if validation_error is not None:
                error_payload, status_code = validation_error
                self._write_json_response(error_payload, status=status_code)
                return

        request_headers = {str(key): str(value) for key, value in self.headers.items()}
        session_id = self._load_request_header_value("MCP-Session-Id")
        if request_method == "initialize" and not session_id:
            session_id = f"mcp-{secrets.token_urlsafe(24)}"
            request_headers["MCP-Session-Id"] = session_id

        response_payload = MCP_REQUEST_SERVICE.dispatch_object(
            request_payload,
            request_headers=request_headers,
        )
        status_code = self._load_http_status_code(response_payload)
        response_headers = {}
        if request_method == "initialize" and session_id and "result" in response_payload:
            response_headers["MCP-Session-Id"] = session_id
        request_params = request_payload.get("params")
        compress = (
            request_method == "tools/call"
            and isinstance(request_params, dict)
            and request_params.get("name") == WebPlayerMcpRequestService._UI_SCOPE_TOOL_NAME
        )
        self._write_json_response(
            response_payload, status=status_code, response_headers=response_headers, compress=compress
        )

    def log_message(self, _format: str, *_args: object) -> None:  # noqa: A003
        return


class WebPlayerMcpNetworkServerService:
    def run_tcp_server(self, *, host: str, port: int) -> None:
        with McpTcpServer((host, port), McpTcpRequestHandler) as server:
            server.serve_forever()

    def run_http_server(self, *, host: str, port: int) -> None:
        with ThreadingHTTPServer((host, port), McpHttpRequestHandler) as server:
            server.serve_forever()


MCP_NETWORK_SERVER_SERVICE = WebPlayerMcpNetworkServerService()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run standalone WebPlayer MCP server")
    parser.add_argument("--transport", choices=["tcp", "http"], default="http", help="Network transport mode")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8765 tcp, 8766 http)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    transport = str(args.transport or "http").strip().lower()
    host = str(args.host or "127.0.0.1").strip() or "127.0.0.1"
    port = int(args.port) if args.port is not None else (8765 if transport == "tcp" else 8766)

    if transport == "http":
        MCP_NETWORK_SERVER_SERVICE.run_http_server(host=host, port=port)
        return

    MCP_NETWORK_SERVER_SERVICE.run_tcp_server(host=host, port=port)


if __name__ == "__main__":
    main()
