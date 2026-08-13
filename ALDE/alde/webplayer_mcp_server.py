"""Standalone MCP server for local WebPlayer control.

This module is intentionally independent from the main ALDE MCP stack so it
can run with minimal dependencies on remote hosts.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import socketserver
import subprocess
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


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
        "tidal_api_track",
        "tidal_api_track_manifest",
        "tidal_api_widevine",
    }
    _ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
    _ALLOWED_SCHEMES = {"http", "https"}
    _SENSITIVE_HEADERS = {"authorization", "cookie", "x-tidal-token"}

    def __init__(self, *, max_response_bytes: int = 2_000_000) -> None:
        self.max_response_bytes = int(max_response_bytes)

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

        try:
            request_object = self.load_object_request(object_name=normalized_object_name, arguments=arguments)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "object_name": normalized_object_name,
            }

        return self.process_object(request_object=request_object)

    def load_object_request(self, *, object_name: str, arguments: dict[str, Any]) -> HttpRequestObject:
        if object_name == "tidal_api_request":
            return self._load_generic_request_object(arguments=arguments)
        if object_name == "tidal_api_track":
            return self._load_track_request_object(arguments=arguments)
        if object_name == "tidal_api_track_manifest":
            return self._load_track_manifest_request_object(arguments=arguments)
        if object_name == "tidal_api_widevine":
            return self._load_widevine_request_object(arguments=arguments)
        raise ValueError(f"unsupported_tool:{object_name}")

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

        validated_url = self._load_validated_tidal_url(raw_url)
        query_url = self._load_url_with_query(
            base_url=validated_url,
            query_payload=arguments.get("query") if "query" in arguments else None,
        )
        request_headers = self._load_headers_object(arguments.get("headers"))
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

    def _load_track_request_object(self, *, arguments: dict[str, Any]) -> HttpRequestObject:
        track_id = str(arguments.get("track_id") or "").strip()
        if not track_id:
            raise ValueError("missing_track_id")

        country_code = str(arguments.get("country_code") or "DE").strip().upper() or "DE"
        include = str(arguments.get("include") or "albums,artists").strip() or "albums,artists"
        request_headers = self._load_headers_object(arguments.get("headers"))
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
        formats = str(arguments.get("formats") or "EMBEDDED").strip() or "EMBEDDED"
        manifest_type = str(arguments.get("manifest_type") or "FULL").strip() or "FULL"
        uri_scheme = str(arguments.get("uri_scheme") or "HTTPS").strip() or "HTTPS"
        usage = str(arguments.get("usage") or "STREAM").strip() or "STREAM"
        request_headers = self._load_headers_object(arguments.get("headers"))
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

        request_headers = self._load_headers_object(arguments.get("headers"))
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
            return self._load_environment_default_headers()
        headers: dict[str, str] = self._load_environment_default_headers()
        for key, value in raw_headers.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            headers[normalized_key] = str(value)
        return headers

    def _load_environment_default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        authorization_value = self._load_environment_header_value("WEBPLAYER_MCP_TIDAL_AUTHORIZATION")
        if authorization_value:
            headers["Authorization"] = authorization_value

        x_tidal_token_value = self._load_environment_header_value("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN")
        if x_tidal_token_value:
            headers["x-tidal-token"] = x_tidal_token_value

        return headers

    def _load_environment_header_value(self, environment_name: str) -> str:
        raw_value = os.getenv(environment_name)
        if raw_value is None:
            return ""
        return str(raw_value).strip()

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
        looks_textual = (
            "application/json" in normalized_content_type
            or "text/" in normalized_content_type
            or "application/problem+json" in normalized_content_type
            or "application/xml" in normalized_content_type
        )
        if looks_textual:
            return response_body_bytes.decode("utf-8", errors="replace"), None

        response_body_base64 = base64.b64encode(response_body_bytes).decode("ascii")
        return "", response_body_base64


class WebPlayerService:
    _LIBRARY_SECTION_URLS = {
        "favorites_tracks": "https://listen.tidal.com/browse/favorites/tracks",
        "favorites_albums": "https://listen.tidal.com/browse/favorites/albums",
        "favorites_playlists": "https://listen.tidal.com/browse/favorites/playlists",
        "my_collection_tracks": "https://listen.tidal.com/browse/my-collection/tracks",
        "history": "https://listen.tidal.com/browse/history",
        "home": "https://listen.tidal.com/",
    }
    _PLAYBACK_METADATA_ACTIONS = {
        "webplayer_play",
        "webplayer_forward",
        "webplayer_backward",
        "webplayer_now_playing",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
    }
    _LONG_RUNNING_ACTIONS = {
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
    }

    _SUPPORTED_ACTIONS = {
        "webplayer_play",
        "webplayer_stop",
        "webplayer_forward",
        "webplayer_backward",
        "webplayer_now_playing",
        "webplayer_search",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
    }

    def __init__(self, command_service: LocalCommandService | None = None) -> None:
        self.command_service = command_service or LocalCommandService()

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

        command = self.load_object_command(
            object_name=normalized_object_name,
            query=effective_query,
            player_selector=player_selector,
            arguments=argument_payload,
        )
        command_timeout_s: int | None = None
        if normalized_object_name in self._LONG_RUNNING_ACTIONS:
            command_timeout_s = self._load_bounded_int(
                argument_payload.get("command_timeout_s"),
                default_value=90,
                minimum=20,
                maximum=600,
            )
        command_result = self.command_service.run_object(
            object_name=normalized_object_name,
            command=command,
            command_timeout_s=command_timeout_s,
        )
        payload = command_result.to_payload()
        payload["player_selector"] = player_selector
        if normalized_object_name in self._PLAYBACK_METADATA_ACTIONS:
            payload["now_playing"] = self.load_object_metadata(payload.get("stdout", ""))
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
            return self._load_play_command(
                player_selector=player_selector,
                wait_for_player_s=wait_for_player_s,
                play_attempts=play_attempts,
            )
        if object_name == "webplayer_stop":
            return self._load_stop_command(player_selector=player_selector)
        if object_name == "webplayer_forward":
            return self._load_forward_command(player_selector=player_selector)
        if object_name == "webplayer_backward":
            return self._load_backward_command(player_selector=player_selector)
        if object_name == "webplayer_now_playing":
            return self._load_now_playing_command(player_selector=player_selector)
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
        if object_name == "webplayer_open_playback_target":
            return self._load_open_playback_target_command(player_selector=player_selector, arguments=arguments)
        raise ValueError(f"Unsupported tool: {object_name}")

    def load_object_metadata(self, stdout_payload: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for line in str(stdout_payload or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            metadata[key.strip()] = value.strip()
        return metadata

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
            "selected_player=$(printf '%s\\n' \"$players\" | awk -v prefix=\"$selector_prefix\" 'index($0,prefix)==1{print; exit}'); "
            "if [ -z \"$selected_player\" ]; then selected_player=$(printf '%s\\n' \"$players\" | head -n1); fi; "
            "if [ -n \"$selected_player\" ]; then break; fi; "
            "sleep 1; wait_counter=$((wait_counter+1)); "
            "done; "
            "if [ -z \"$selected_player\" ]; then echo 'error=no_player'; exit 1; fi; "
        )

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
            "echo play_attempts_used=$attempt_counter;"
        )

    def _load_play_command(self, *, player_selector: str, wait_for_player_s: int, play_attempts: int) -> str:
        return self._load_play_retry_command(
            player_selector=player_selector,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            require_playing=False,
        )

    def _load_stop_command(self, *, player_selector: str) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "playerctl -p \"$selected_player\" stop 2>/dev/null || playerctl -p \"$selected_player\" pause 2>/dev/null || true; "
            "echo player=$selected_player; "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown);"
        )

    def _load_forward_command(self, *, player_selector: str) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "playerctl -p \"$selected_player\" next 2>/dev/null || true; "
            "echo player=$selected_player; "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown);"
        )

    def _load_backward_command(self, *, player_selector: str) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "playerctl -p \"$selected_player\" previous 2>/dev/null || true; "
            "echo player=$selected_player; "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown);"
        )

    def _load_now_playing_command(self, *, player_selector: str) -> str:
        return (
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=0)
            + "echo player=$selected_player; "
            "echo status=$(playerctl -p \"$selected_player\" status 2>/dev/null || echo unknown); "
            "echo title=$(playerctl -p \"$selected_player\" metadata xesam:title 2>/dev/null || true); "
            "echo artist=$(playerctl -p \"$selected_player\" metadata xesam:artist 2>/dev/null || true); "
            "echo album=$(playerctl -p \"$selected_player\" metadata xesam:album 2>/dev/null || true);"
        )

    def _load_browser_open_command(
        self,
        *,
        target_url: str,
        log_file_path: str,
        allow_xdg_open_fallback: bool,
    ) -> str:
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

        return (
            browser_discovery
            + "UID_NUM=$(id -u); export XDG_RUNTIME_DIR=\"/run/user/$UID_NUM\"; "
            "if [ -S \"$XDG_RUNTIME_DIR/wayland-0\" ]; then export WAYLAND_DISPLAY=wayland-0; "
            "elif [ -S \"$XDG_RUNTIME_DIR/wayland-1\" ]; then export WAYLAND_DISPLAY=wayland-1; fi; "
            "nohup \"$BROWSER_CMD\" --ozone-platform=wayland --enable-features=UseOzonePlatform "
            "--remote-debugging-port=9222 --remote-allow-origins=* --new-window "
            + quoted_url
            + " >"
            + quoted_log_path
            + " 2>&1 & "
            "sleep 1; "
            "echo opened_url="
            + quoted_url
            + "; "
            "echo status=started;"
        )

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
        cdp_script = '''
python3 - <<'PY'
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
  const selectors = [
    'button[data-test*="play" i]',
    'button[data-testid*="play" i]',
    '[role="button"][data-test*="play" i]',
    'button[aria-label*="Play" i]',
    'button[title*="Play" i]',
    '[role="button"][aria-label*="Play" i]',
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
        )
        initial_play_command = self._load_play_retry_command(
            player_selector=player_selector,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            require_playing=False,
        )
        final_play_command = self._load_play_retry_command(
            player_selector=player_selector,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            require_playing=True,
        )

        player_selector_prefix = shlex.quote(str(player_selector or "chromium"))
        playback_probe_command = (
            "players_snapshot=$(playerctl -l 2>/dev/null || true); "
            "selected_player_snapshot=$(printf '%s\\n' \"$players_snapshot\" | awk -v prefix="
            + player_selector_prefix
            + " 'index($0,prefix)==1{print; exit}'); "
            "if [ -z \"$selected_player_snapshot\" ]; then selected_player_snapshot=$(printf '%s\\n' \"$players_snapshot\" | head -n1); fi; "
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
                + "if [ \"$status_snapshot\" = \"Playing\" ]; then "
                "echo player=$selected_player_snapshot; "
                "echo status=$status_snapshot; "
                "echo title=$(playerctl -p \"$selected_player_snapshot\" metadata xesam:title 2>/dev/null || true); "
                "echo artist=$(playerctl -p \"$selected_player_snapshot\" metadata xesam:artist 2>/dev/null || true); "
                "echo album=$(playerctl -p \"$selected_player_snapshot\" metadata xesam:album 2>/dev/null || true); "
                "exit 0; fi; "
                + final_play_command
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
            + "if [ \"$status_snapshot\" = \"Playing\" ]; then "
            "echo player=$selected_player_snapshot; "
            "echo status=$status_snapshot; "
            "echo title=$(playerctl -p \"$selected_player_snapshot\" metadata xesam:title 2>/dev/null || true); "
            "echo artist=$(playerctl -p \"$selected_player_snapshot\" metadata xesam:artist 2>/dev/null || true); "
            "echo album=$(playerctl -p \"$selected_player_snapshot\" metadata xesam:album 2>/dev/null || true); "
            "exit 0; fi; "
            + cdp_click_command
            + " cdp_exit_code=$?; if [ \"$cdp_exit_code\" -ne 0 ]; then echo error=cdp_click_failed; exit 3; fi; "
            + final_play_command
        )

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
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=True)
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
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=True)
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
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=True)
        return self._load_open_url_and_play_command(
            target_url=target_url,
            player_selector=player_selector,
            wait_for_page_s=wait_for_page_s,
            wait_for_player_s=wait_for_player_s,
            play_attempts=play_attempts,
            cdp_action="library",
            cdp_query="",
            cdp_autoclick=cdp_autoclick,
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
        cdp_autoclick = self._load_boolean_argument(arguments.get("cdp_autoclick"), default_value=True)
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
    _UI_EXTENSION_NAME = "io.modelcontextprotocol/ui"
    _UI_RESOURCE_URI = "ui://webplayer/mini-controls.html"
    _UI_RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"
    _STRATEGY_RISK_TIER_ALLOWED_VALUES = {"low", "standard", "high", "critical"}
    _STRATEGY_RISK_TIER_DEFAULT = "standard"
    _STRATEGY_COMPLIANCE_MODE_ALLOWED_VALUES = {"strict", "balanced", "adaptive"}
    _STRATEGY_COMPLIANCE_MODE_DEFAULT = "balanced"
    _WEBPLAYER_PLAYBACK_TOOL_NAMES = (
        "webplayer_play",
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
        "webplayer_search",
    )
    _TIDAL_API_TOOL_NAMES = (
        "tidal_api_request",
        "tidal_api_track",
        "tidal_api_track_manifest",
        "tidal_api_widevine",
    )

    def __init__(
        self,
        player_service: WebPlayerService | None = None,
        tidal_api_service: TidalApiService | None = None,
    ) -> None:
        self.player_service = player_service or WebPlayerService()
        self.tidal_api_service = tidal_api_service or TidalApiService()
        self._ui_enabled = False

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
        return "\n".join(
            [
                "WebPlayer MCP strategy overview:",
                "- Use prompts/get before tools/call for explicit execution policy.",
                "- Resolve player_selector first and keep it stable across a session.",
                "- Prefer webplayer_now_playing after each state-changing tool call.",
                "- For playback start, prefer webplayer_search_play, webplayer_playlist_play, webplayer_library_play, or webplayer_open_playback_target over manual sequences.",
                "- Use tidal_api_request, tidal_api_track, tidal_api_track_manifest, and tidal_api_widevine when the workflow needs direct TIDAL API context.",
                f"- Playback tools={', '.join(self._WEBPLAYER_PLAYBACK_TOOL_NAMES)}",
                f"- State tools={', '.join(self._WEBPLAYER_STATE_TOOL_NAMES)}",
                f"- TIDAL API tools={', '.join(self._TIDAL_API_TOOL_NAMES)}",
                "- When the workflow needs richer context, consult the AgentsDB MCP layer for graph/workflow/sequence context via agentsdb/mcp.",
                f"- Active player_selector={player_selector}",
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

    @staticmethod
    def _load_csv_values(raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            return [str(item).strip() for item in raw_value if str(item).strip()]
        return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]

    def _load_ui_extension_requested(self, params: dict[str, Any]) -> bool:
        capabilities = params.get("capabilities")
        if isinstance(capabilities, dict):
            extensions = capabilities.get("extensions")
            if isinstance(extensions, dict) and self._UI_EXTENSION_NAME in extensions:
                return True
            experimental = capabilities.get("experimental")
            if isinstance(experimental, dict) and self._UI_EXTENSION_NAME in experimental:
                return True
        meta = params.get("_meta")
        if isinstance(meta, dict) and self._UI_EXTENSION_NAME in self._load_csv_values(
            meta.get("io.modelcontextprotocol/extensions")
        ):
            return True
        return str(os.getenv("WEBPLAYER_MCP_UI_EXTENSION_DEFAULT") or "").strip().casefold() in {
            "1", "true", "yes", "on"
        }

    def _load_ui_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._load_ui_extension_requested(params):
            return {"error": "ui_extension_not_enabled"}
        return {
            "uri": self._UI_RESOURCE_URI,
            "name": "webplayer-mini-controls",
            "title": "WebPlayer Mini Controls",
            "description": "Compact MCP App controls for WebPlayer playback.",
            "mimeType": self._UI_RESOURCE_MIME_TYPE,
        }

    def _load_ui_resource_markup(self) -> str:
        return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WebPlayer Mini Controls</title>
<style>
:root{color-scheme:dark}body{font:12px system-ui,sans-serif;margin:6px;background:transparent;color:#d1d5db}
.panel{display:inline-block;min-width:220px;padding:8px;border:1px solid #6b7280;border-radius:10px;background:rgba(0,0,0,.82)}
.controls{display:flex;gap:4px;align-items:center}.controls button{border:0;border-radius:6px;padding:6px 9px;color:#e5e7eb;background:#374151;cursor:pointer}
.controls button:hover{background:#4b5563}.status{margin-top:8px;min-height:2.5em;white-space:pre-wrap}
</style></head>
<body><div class="panel"><div class="controls">
<button data-tool="webplayer_backward" aria-label="previous">|&lt;</button>
<button data-tool="webplayer_play" data-args='{"playback_backend":"browser","player_selector":"chromium","cdp_port":9222,"cdp_autoclick":true}' aria-label="play">Play</button>
<button data-tool="webplayer_stop" aria-label="stop">Stop</button>
<button data-tool="webplayer_forward" aria-label="next">&gt;|</button>
<button data-tool="webplayer_volume_adjust" data-args='{"delta_percent":-5}' aria-label="volume down">-</button>
<button data-tool="webplayer_volume_adjust" data-args='{"delta_percent":5}' aria-label="volume up">+</button>
</div><div id="status" class="status">Ready.</div></div>
<script>
const statusEl=document.getElementById("status");
const defaultArgs={player_selector:"chromium",cdp_port:9222};
async function callTool(name,args){
  const argumentsValue={...defaultArgs,...(args||{})};
  if(window.mcp&&typeof window.mcp.callTool==="function"){
    return window.mcp.callTool({name,arguments:argumentsValue});
  }
  const response=await fetch("/mcp",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({jsonrpc:"2.0",id:Date.now(),method:"tools/call",params:{name,arguments:argumentsValue}})});
  if(!response.ok)throw new Error("MCP HTTP "+response.status);
  return response.json();
}
function render(result){statusEl.textContent=typeof result==="string"?result:JSON.stringify(result,null,2)}
async function refresh(){try{render(await callTool("webplayer_now_playing",{}))}catch(error){render(String(error))}}
for(const button of document.querySelectorAll("[data-tool]"))button.addEventListener("click",async()=>{
  button.disabled=true;try{render(await callTool(button.dataset.tool,JSON.parse(button.dataset.args||"{}")));await refresh()}catch(error){render(String(error))}finally{button.disabled=false}
});
refresh();
</script></body></html>"""

    def _load_requested_protocol_version(self, params: dict[str, Any]) -> str:
        requested = str(params.get("protocolVersion") or "").strip()
        if not requested:
            return "2026-07-28"
        if requested not in self._SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(f"Unsupported protocol version: {requested}")
        return requested

    @staticmethod
    def _load_jsonrpc_error(*, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _load_mcp_tool_definitions(self, *, ui_enabled: bool) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for spec in self.load_tool_definitions():
            function = dict((spec or {}).get("function") or {})
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            definition: dict[str, Any] = {
                "name": name,
                "title": name,
                "description": str(function.get("description") or ""),
                "inputSchema": function.get("parameters")
                if isinstance(function.get("parameters"), dict)
                else {"type": "object", "additionalProperties": False},
            }
            if ui_enabled:
                definition["_meta"] = {"ui": {"resourceUri": self._UI_RESOURCE_URI}}
            definitions.append(definition)
        return definitions

    def _dispatch_jsonrpc_object(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        method = str(payload.get("method") or "").strip()
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}
        requested_ui = self._load_ui_extension_requested(params)
        if method == "initialize":
            try:
                protocol_version = self._load_requested_protocol_version(params)
            except ValueError as exc:
                return self._load_jsonrpc_error(request_id=request_id, code=-32602, message=str(exc))
            self._ui_enabled = requested_ui
            capabilities: dict[str, Any] = {"tools": {}, "prompts": {}}
            if self._ui_enabled:
                capabilities["resources"] = {"listChanged": False}
                capabilities["extensions"] = {
                    self._UI_EXTENSION_NAME: {"resourceUri": self._UI_RESOURCE_URI}
                }
            result = {
                "protocolVersion": protocol_version,
                "serverInfo": {"name": "webplayer-mcp"},
                "capabilities": capabilities,
                "supportedVersions": list(self._SUPPORTED_PROTOCOL_VERSIONS),
            }
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": self._load_mcp_tool_definitions(ui_enabled=self._ui_enabled or requested_ui),
            }
        elif method == "resources/list":
            resource = self._load_ui_resource(
                {"_meta": {"io.modelcontextprotocol/extensions": self._UI_EXTENSION_NAME}}
                if self._ui_enabled or requested_ui
                else params
            )
            if "error" in resource:
                result = {"resources": [], "resultType": "complete"}
            else:
                result = {"resources": [resource], "resultType": "complete"}
        elif method == "resources/read":
            resource = self._load_ui_resource(
                {**params, "_meta": {"io.modelcontextprotocol/extensions": self._UI_EXTENSION_NAME}}
                if self._ui_enabled or requested_ui
                else params
            )
            if "error" in resource:
                return self._load_jsonrpc_error(request_id=request_id, code=-32001, message=str(resource["error"]))
            if str(params.get("uri") or "").strip() != self._UI_RESOURCE_URI:
                return self._load_jsonrpc_error(request_id=request_id, code=-32001, message="resource_not_found")
            result = {
                "contents": [{
                    "uri": self._UI_RESOURCE_URI,
                    "mimeType": self._UI_RESOURCE_MIME_TYPE,
                    "text": self._load_ui_resource_markup(),
                }],
                "resultType": "complete",
            }
        elif method == "tools/call":
            result_payload = self._load_jsonrpc_tools_call_result(params)
            if "error" in result_payload:
                return self._load_jsonrpc_error(request_id=request_id, code=-32602, message=str(result_payload["error"]))
            result = result_payload.get("result") or {}
        elif method == "prompts/list":
            result = dict(self.load_prompts_list_result(params).get("result") or {})
            result["resultType"] = "complete"
        elif method == "prompts/get":
            result_payload = self.load_prompts_get_result(params)
            if "error" in result_payload:
                return self._load_jsonrpc_error(request_id=request_id, code=-32602, message=str(result_payload["error"]))
            result = result_payload.get("result") or {}
        else:
            return self._load_jsonrpc_error(
                request_id=request_id, code=-32601, message=f"Method not found: {method}"
            )
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def dispatch_object(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            return self._dispatch_jsonrpc_object(payload)
        if not isinstance(payload, dict):
            return {"error": "invalid_json"}

        method = str(payload.get("method") or "")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {}

        handlers = {
            "initialize": self.load_initialize_result,
            "tools/list": self.load_tools_list_result,
            "tools/call": self.load_tools_call_result,
            "prompts/list": self.load_prompts_list_result,
            "prompts/get": self.load_prompts_get_result,
        }
        handler = handlers.get(method)
        if handler is None:
            return {"error": "method_not_implemented"}
        return handler(params)

    def load_initialize_result(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_params = dict(params or {})
        protocol_version = self._load_requested_protocol_version(normalized_params)
        return {
            "result": {
                "protocolVersion": protocol_version,
                "serverInfo": {"name": "webplayer-mcp"},
                "capabilities": {
                    "tools": {},
                    "prompts": {},
                },
            }
        }

    def load_tools_list_result(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"result": {"tools": self.load_tool_definitions()}}

    def load_prompts_list_result(self, _params: dict[str, Any]) -> dict[str, Any]:
        prompts_payload = [
            {
                "name": "webplayer_operator",
                "description": "Operational prompt for controlling the local web player and TIDAL API surface via MCP tool calls.",
                "arguments": [
                    {
                        "name": "player_selector",
                        "description": "Preferred MPRIS player prefix (default: chromium).",
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
                "description": "General strategy overview for the current WebPlayer playback, search, and TIDAL API tool set.",
                "arguments": [
                    {
                        "name": "player_selector",
                        "description": "Preferred MPRIS player prefix.",
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
                        "description": "Preferred MPRIS player prefix.",
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
                f"7) For content discovery call webplayer_search_play with query='{search_query}'."
                if search_query
                else "7) For content discovery call webplayer_search_play with a non-empty query."
            )

            prompt_text = (
                "You are controlling a local web player and TIDAL API surface through MCP tool calls.\n"
                f"Use player_selector='{player_selector}' unless the user requests another player.\n"
                "Execution sequence:\n"
                "1) Check state first with webplayer_now_playing.\n"
                "2) Start playback with webplayer_search_play for searches, webplayer_playlist_play for playlists, webplayer_library_play for library sections, or webplayer_open_playback_target for direct TIDAL URLs.\n"
                "3) Use webplayer_play only as a generic retry when playback is already prepared.\n"
                "4) Stop playback with webplayer_stop when requested.\n"
                "5) Skip next with webplayer_forward when requested.\n"
                "6) Skip previous with webplayer_backward when requested.\n"
                f"{search_line}\n"
                "8) Use tidal_api_request, tidal_api_track, tidal_api_track_manifest, or tidal_api_widevine when the user asks for direct API inspection or manifest data.\n"
                "After each control action, call webplayer_now_playing and summarize status/title/artist/album.\n"
                "If any tool returns ok=false, surface stderr/stdout and propose the next corrective action."
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

    @staticmethod
    def _load_tools_call_arguments_payload(raw_arguments: Any) -> dict[str, Any]:
        arguments_payload = raw_arguments or {}
        if isinstance(arguments_payload, str):
            try:
                arguments_payload = json.loads(arguments_payload)
            except Exception:
                arguments_payload = {}
        if not isinstance(arguments_payload, dict):
            arguments_payload = {}
        return arguments_payload

    def _load_tools_call_execution_payload(
        self,
        *,
        tool_name: str,
        arguments_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name.startswith("tidal_api_"):
            return self.tidal_api_service.dispatch_object(
                object_name=tool_name,
                arguments=arguments_payload,
            )

        player_selector = str(arguments_payload.get("player_selector") or "chromium").strip() or "chromium"
        query = str(arguments_payload.get("query") or "").strip() if "query" in arguments_payload else None

        return self.player_service.dispatch_object(
            object_name=tool_name,
            player_selector=player_selector,
            query=query,
            arguments=arguments_payload,
        )

    def _load_jsonrpc_tools_call_result(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        if not tool_name:
            return {"error": "missing_tool_name"}

        arguments_payload = self._load_tools_call_arguments_payload(params.get("arguments"))
        execution_payload = self._load_tools_call_execution_payload(
            tool_name=tool_name,
            arguments_payload=arguments_payload,
        )
        text_payload = json.dumps(execution_payload, ensure_ascii=True)
        return {
            "result": {
                "content": [{"type": "text", "text": text_payload}],
                "structuredContent": execution_payload,
                "isError": bool(execution_payload.get("ok") is False),
            }
        }

    def load_tools_call_result(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        if not tool_name:
            return {"error": "missing_tool_name"}
        arguments_payload = self._load_tools_call_arguments_payload(params.get("arguments"))
        result_payload = self._load_tools_call_execution_payload(
            tool_name=tool_name,
            arguments_payload=arguments_payload,
        )
        return {"result": {"content": json.dumps(result_payload, ensure_ascii=True)}}

    def load_tool_definitions(self) -> list[dict[str, Any]]:
        base_properties = {
            "player_selector": {
                "type": "string",
                "description": "Preferred MPRIS player prefix.",
                "default": "chromium",
            }
        }
        cdp_autoclick_properties = {
            "cdp_autoclick": {
                "type": "boolean",
                "description": "Enable Chrome DevTools auto-click fallback when playback stays stopped.",
                "default": True,
            },
            "cdp_port": {
                "type": "integer",
                "description": "Chrome DevTools remote debugging port.",
                "default": 9222,
            },
            "cdp_click_timeout_s": {
                "type": "integer",
                "description": "Maximum seconds spent searching/clicking play controls via CDP.",
                "default": 12,
            },
        }

        return [
            {
                "type": "function",
                "function": {
                    "name": "webplayer_play",
                    "description": "Start playback in web player with automatic player discovery retries.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player before failing.",
                                "default": 10,
                            },
                            "play_attempts": {
                                "type": "integer",
                                "description": "How often play should be retried.",
                                "default": 6,
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
                    "description": "Stop or pause playback in web player.",
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
                    "name": "webplayer_now_playing",
                    "description": "Show current track metadata (title, artist, album, status).",
                    "parameters": {"type": "object", "properties": dict(base_properties), "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_search",
                    "description": "Open TIDAL search for the provided query.",
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
                    "description": "Open a TIDAL search query and attempt playback automatically.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "query": {"type": "string", "description": "Search query for TIDAL."},
                            "wait_for_page_s": {
                                "type": "integer",
                                "description": "Delay after page open before starting playback.",
                                "default": 4,
                            },
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player.",
                                "default": 15,
                            },
                            "play_attempts": {
                                "type": "integer",
                                "description": "How often play should be retried.",
                                "default": 8,
                            },
                            "command_timeout_s": {
                                "type": "integer",
                                "description": "Overall shell timeout for this workflow.",
                                "default": 90,
                            },
                            **dict(cdp_autoclick_properties),
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_playlist_play",
                    "description": "Open a TIDAL playlist and start playback.",
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
                            "wait_for_page_s": {
                                "type": "integer",
                                "description": "Delay after page open before starting playback.",
                                "default": 4,
                            },
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player.",
                                "default": 15,
                            },
                            "play_attempts": {
                                "type": "integer",
                                "description": "How often play should be retried.",
                                "default": 8,
                            },
                            "command_timeout_s": {
                                "type": "integer",
                                "description": "Overall shell timeout for this workflow.",
                                "default": 90,
                            },
                            **dict(cdp_autoclick_properties),
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_library_play",
                    "description": "Open TIDAL library/favorites section and start playback.",
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
                            "wait_for_page_s": {
                                "type": "integer",
                                "description": "Delay after page open before starting playback.",
                                "default": 4,
                            },
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player.",
                                "default": 15,
                            },
                            "play_attempts": {
                                "type": "integer",
                                "description": "How often play should be retried.",
                                "default": 8,
                            },
                            "command_timeout_s": {
                                "type": "integer",
                                "description": "Overall shell timeout for this workflow.",
                                "default": 90,
                            },
                            **dict(cdp_autoclick_properties),
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_open_playback_target",
                    "description": "Open a TIDAL URL and force playback start after page load.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "target_url": {
                                "type": "string",
                                "description": "Absolute TIDAL URL (tidal.com or subdomain).",
                            },
                            "wait_for_page_s": {
                                "type": "integer",
                                "description": "Delay after page open before starting playback.",
                                "default": 4,
                            },
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player.",
                                "default": 15,
                            },
                            "play_attempts": {
                                "type": "integer",
                                "description": "How often play should be retried.",
                                "default": 8,
                            },
                            "command_timeout_s": {
                                "type": "integer",
                                "description": "Overall shell timeout for this workflow.",
                                "default": 90,
                            },
                            **dict(cdp_autoclick_properties),
                        },
                        "required": ["target_url"],
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
    server_version = "webplayer-mcp-http/1.0"

    @staticmethod
    def _allowed_origins() -> set[str]:
        configured = {
            origin.strip()
            for origin in str(os.getenv("WEBPLAYER_MCP_ALLOWED_ORIGINS") or "").split(",")
            if origin.strip()
        }
        return configured or {
            "http://localhost",
            "http://127.0.0.1",
            "https://localhost",
            "https://127.0.0.1",
        }

    def _cors_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip()
        if origin and origin in self._allowed_origins():
            return origin
        return "*" if not origin else ""

    def _write_json_response(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        cors_origin = self._cors_origin()
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, MCP-Protocol-Version, MCP-Extensions")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        cors_origin = self._cors_origin()
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, MCP-Protocol-Version, MCP-Extensions")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._write_json_response({"ok": True, "server": "webplayer-mcp-http"}, status=200)
            return
        if self.path.rstrip("/") == "":
            self._write_json_response(
                {"ok": True, "server": "webplayer-mcp-http", "mcpEndpoint": "/mcp", "healthEndpoint": "/health"},
                status=200,
            )
            return
        self._write_json_response({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in {"", "/mcp"}:
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

        extensions = str(self.headers.get("MCP-Extensions") or "").strip()
        if extensions and isinstance(request_payload, dict) and request_payload.get("jsonrpc") == "2.0":
            params = request_payload.get("params")
            normalized_params = dict(params) if isinstance(params, dict) else {}
            metadata = dict(normalized_params.get("_meta") or {})
            metadata.setdefault("io.modelcontextprotocol/extensions", extensions)
            normalized_params["_meta"] = metadata
            request_payload["params"] = normalized_params

        response_payload = MCP_REQUEST_SERVICE.dispatch_object(request_payload)
        status_code = 200 if "result" in response_payload else 400
        self._write_json_response(response_payload, status=status_code)

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
