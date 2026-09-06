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
from textwrap import dedent
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
    _DEFAULT_WEB_TOKEN = "CzET4vdadNUFQ5JU"
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
        headers["x-tidal-token"] = x_tidal_token_value or self._DEFAULT_WEB_TOKEN

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
        "favorites_tracks": "https://tidal.com/my-collection/tracks",
        "favorites_albums": "https://tidal.com/my-collection/albums",
        "favorites_playlists": "https://tidal.com/my-collection/playlists",
        "my_collection_tracks": "https://tidal.com/my-collection/tracks",
        "history": "https://tidal.com/browse/history",
        "home": "https://tidal.com/",
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
        "webplayer_favorite_current_track",
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
        "webplayer_favorite_current_track",
        "webplayer_volume_adjust",
        "webplayer_search",
        "webplayer_search_play",
        "webplayer_playlist_play",
        "webplayer_library_play",
        "webplayer_open_playback_target",
    }
    _API_ONLY_SUPPORTED_ACTIONS = {
        "webplayer_favorite_current_track",
        "webplayer_search",
    }

    def __init__(self, command_service: LocalCommandService | None = None) -> None:
        self.command_service = command_service or LocalCommandService()

    def load_supported_actions(self) -> set[str]:
        return set(self._SUPPORTED_ACTIONS)

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
        api_only_mode = self._load_api_only_mode(arguments=arguments)
        if api_only_mode:
            if object_name == "webplayer_favorite_current_track":
                return self._load_favorite_track_api_only_command(arguments=arguments)
            if object_name == "webplayer_search":
                return self._load_search_api_only_command(query=query, arguments=arguments)
            return self._load_api_only_unsupported_command(object_name=object_name)

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
        if object_name == "webplayer_favorite_current_track":
            return self._load_favorite_current_track_command(player_selector=player_selector, arguments=arguments)
        if object_name == "webplayer_volume_adjust":
            return self._load_volume_adjust_command(player_selector=player_selector, arguments=arguments)
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

    def _load_track_metadata_helpers_script(self) -> str:
        return dedent(
            r"""
            QUALITY_ALIASES = {
                "HIRES_LOSSLESS": "HI_RES_LOSSLESS",
                "HI_RES_LOSSLESS": "HI_RES_LOSSLESS",
                "HIRES": "HI_RES_LOSSLESS",
                "HI_RES": "HI_RES_LOSSLESS",
                "MASTER": "HI_RES_LOSSLESS",
                "LOSSLESS": "LOSSLESS",
                "HIGH": "HIGH",
                "LOW": "LOW",
            }
            QUALITY_PRIORITY = ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW")
            AUDIO_FORMAT_REFERENCES = {
                "HI_RES_LOSSLESS": ("24", "192", "24/192 kHz"),
                "LOSSLESS": ("16", "44.1", "16/44.1 kHz"),
                "HIGH": ("none", "none", "320 kbps"),
                "LOW": ("none", "none", "96 kbps"),
            }

            def normalize_stream_quality(*quality_values: object) -> str:
                canonical_values: list[str] = []
                for quality_value in quality_values:
                    values = quality_value if isinstance(quality_value, (list, tuple, set)) else [quality_value]
                    for value in values:
                        for token in str(value or "").replace(";", ",").split(","):
                            normalized_token = token.strip().upper().replace("-", "_")
                            canonical_token = QUALITY_ALIASES.get(normalized_token)
                            if canonical_token and canonical_token not in canonical_values:
                                canonical_values.append(canonical_token)
                for supported_quality in QUALITY_PRIORITY:
                    if supported_quality in canonical_values:
                        return supported_quality
                return "none"

            def load_audio_format_reference(quality_value: str) -> tuple[str, str, str]:
                return AUDIO_FORMAT_REFERENCES.get(
                    normalize_stream_quality(quality_value),
                    ("none", "none", "none"),
                )

            def load_quality_bitrate_reference(quality_value: str) -> str:
                _bit_depth, _sample_rate_khz, format_reference = load_audio_format_reference(quality_value)
                return format_reference

            def load_artwork_url(cover_value: object) -> str:
                cover = str(cover_value or "").strip().lower()
                if not cover:
                    return ""
                parts = cover.split("-")
                if len(parts) != 5 or any(not part or any(character not in "0123456789abcdef" for character in part) for part in parts):
                    return ""
                return "https://resources.tidal.com/images/" + "/".join(parts) + "/320x320.jpg"

            def load_musical_key(key_value: object, key_scale_value: object) -> str:
                key = (
                    str(key_value or "")
                    .strip()
                    .replace("_SHARP", "#")
                    .replace("_FLAT", "b")
                    .replace("Sharp", "#")
                    .replace("Flat", "b")
                )
                key_scale = str(key_scale_value or "").strip().lower()
                return " ".join(part for part in (key, key_scale) if part)
            """
        ).strip()

    def _load_api_only_mode(self, *, arguments: dict[str, Any]) -> bool:
        backend_argument = str(arguments.get("playback_backend") or "").strip().casefold()
        if backend_argument in {"api", "api_only"}:
            return True
        if backend_argument == "browser":
            return False

        env_backend = str(os.environ.get("WEBPLAYER_PLAYBACK_BACKEND") or "").strip().casefold()
        if env_backend in {"api", "api_only"}:
            return True
        if env_backend == "browser":
            return False

        return self._load_boolean_argument(os.environ.get("WEBPLAYER_API_ONLY_MODE"), default_value=False)

    def _load_country_code(self, raw_value: Any, *, default_value: str = "DE") -> str:
        normalized_value = str(raw_value or "").strip().upper()
        if len(normalized_value) != 2 or not normalized_value.isalpha():
            return str(default_value or "DE").strip().upper() or "DE"
        return normalized_value

    def _load_api_only_unsupported_command(self, *, object_name: str) -> str:
        return (
            "echo 'error=unsupported_api_only'; "
            f"echo object_name={shlex.quote(str(object_name or '').strip())}; "
            "echo playback_backend=api_only; "
            f"echo supported_api_only_actions={shlex.quote(','.join(sorted(self._API_ONLY_SUPPORTED_ACTIONS)))}; "
            "exit 1;"
        )

    def _load_search_api_only_command(self, *, query: str | None, arguments: dict[str, Any]) -> str:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return "echo 'error=missing_query'; exit 1;"

        country_code = self._load_country_code(arguments.get("country_code"), default_value="DE")
        query_payload = shlex.quote(normalized_query)
        country_code_payload = shlex.quote(country_code)
        python_script = dedent(
            r"""
            import json
            import os
            from urllib.error import HTTPError, URLError
            from urllib.parse import urlencode
            from urllib.request import Request, urlopen

            PUBLIC_WEB_TOKEN = "__PUBLIC_WEB_TOKEN__"
            USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            SEARCH_URL = "https://listen.tidal.com/v1/search"

            def emit(message: str) -> None:
                print(message, flush=True)

            query = str(os.environ.get("WEBPLAYER_SEARCH_QUERY") or "").strip()
            country_code = str(os.environ.get("WEBPLAYER_COUNTRY_CODE") or "DE").strip().upper() or "DE"
            if not query:
                emit("error=missing_query")
                raise SystemExit(1)

            request_headers = {
                "Accept": "application/json",
                "Origin": "https://listen.tidal.com",
                "Referer": "https://listen.tidal.com/search",
                "User-Agent": USER_AGENT,
                "x-tidal-token": str(os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN") or PUBLIC_WEB_TOKEN),
            }
            request_url = SEARCH_URL + "?" + urlencode(
                {
                    "query": query,
                    "countryCode": country_code,
                    "types": "TRACKS",
                    "limit": 5,
                    "offset": 0,
                }
            )

            status_code = 0
            response_body = ""
            try:
                request = Request(request_url, headers=request_headers, method="GET")
                with urlopen(request, timeout=20) as response:
                    status_code = int(getattr(response, "status", 200))
                    response_body = response.read().decode("utf-8", "replace")
            except HTTPError as exc:
                status_code = int(exc.code or 500)
                response_body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            except URLError as exc:
                emit("error=search_request_failed")
                emit(f"error_reason={exc.reason}")
                raise SystemExit(2)
            except Exception as exc:
                emit("error=search_request_failed")
                emit(f"error_reason={exc}")
                raise SystemExit(2)

            if status_code // 100 != 2:
                emit("error=search_request_failed")
                emit(f"status_code={status_code}")
                response_preview = response_body.replace("\n", " ").replace("\r", " ").strip()
                if response_preview:
                    emit(f"response_preview={response_preview[:500]}")
                raise SystemExit(3)

            try:
                payload = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError:
                payload = {}

            track_items: list[dict[str, object]] = []
            if isinstance(payload, dict):
                tracks_payload = payload.get("tracks")
                if isinstance(tracks_payload, dict):
                    items_payload = tracks_payload.get("items")
                    if isinstance(items_payload, list):
                        track_items = [item for item in items_payload if isinstance(item, dict)]

            emit("status=Search")
            emit(f"query={query}")
            emit(f"country_code={country_code}")
            emit("search_backend=api_only")
            emit(f"result_count={len(track_items)}")

            if track_items:
                first_track = track_items[0]
                track_id = str(first_track.get("id") or "").strip()
                title = str(first_track.get("title") or "").strip()
                artist_name = ""
                artist_payload = first_track.get("artist")
                if isinstance(artist_payload, dict):
                    artist_name = str(artist_payload.get("name") or "").strip()
                if not artist_name:
                    artists_payload = first_track.get("artists")
                    if isinstance(artists_payload, list) and artists_payload:
                        first_artist = artists_payload[0]
                        if isinstance(first_artist, dict):
                            artist_name = str(first_artist.get("name") or "").strip()

                if track_id:
                    emit(f"track_id={track_id}")
                if title:
                    emit(f"title={title}")
                if artist_name:
                    emit(f"artist={artist_name}")
            """
        ).replace("__PUBLIC_WEB_TOKEN__", TidalApiService._DEFAULT_WEB_TOKEN).strip()
        return (
            "if ! command -v python3 >/dev/null 2>&1; then echo 'error=python3_missing'; exit 1; fi; "
            + f"WEBPLAYER_SEARCH_QUERY={query_payload} "
            + f"WEBPLAYER_COUNTRY_CODE={country_code_payload} "
            + "python3 - <<'PY'\n"
            + python_script
            + "\nPY"
        )

    def _load_browser_access_token_service_script(self) -> str:
        return dedent(
            r"""
            class TidalBrowserAccessTokenService:
                def __init__(
                    self,
                    *,
                    debug_port: int,
                    timeout_s: int,
                    token_path: Path | None,
                    public_web_token: str,
                    user_agent: str,
                ) -> None:
                    self.debug_port = int(debug_port)
                    self.timeout_s = max(5, int(timeout_s))
                    self.token_path = token_path
                    self.public_web_token = str(public_web_token)
                    self.user_agent = str(user_agent)
                    self.last_error = ""
                    self.last_warning = ""
                    self.target_id = ""

                def load_access_token(self) -> str:
                    if self.debug_port <= 0:
                        self.last_error = "invalid_cdp_port"
                        return ""
                    if websockets is None:
                        self.last_error = "cdp_websockets_missing"
                        return ""
                    try:
                        access_token, session_payload = asyncio.run(self._capture_access_token())
                    except HTTPError as exc:
                        self.last_error = f"cdp_http_{int(exc.code or 500)}"
                        return ""
                    except (URLError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
                        self.last_error = f"cdp_unavailable:{type(exc).__name__}"
                        return ""
                    except (json.JSONDecodeError, WebSocketException) as exc:
                        self.last_error = f"cdp_protocol_error:{type(exc).__name__}"
                        return ""
                    finally:
                        self._close_page_target()
                    if not access_token:
                        if not self.last_error:
                            self.last_error = "authorization_not_observed"
                        return ""
                    self._store_access_token(access_token, session_payload)
                    return access_token

                async def _capture_access_token(self) -> tuple[str, dict[str, object]]:
                    target = self._create_page_target()
                    websocket_url = str(target.get("webSocketDebuggerUrl") or "").strip()
                    if not websocket_url:
                        self.last_error = "cdp_target_websocket_missing"
                        return "", {}

                    async with websockets.connect(websocket_url, max_size=8_000_000) as websocket:
                        message_id = 0
                        event_queue: list[dict[str, object]] = []

                        async def send_method(
                            method_name: str,
                            params: dict[str, object] | None = None,
                        ) -> dict[str, object]:
                            nonlocal message_id
                            message_id += 1
                            payload: dict[str, object] = {"id": message_id, "method": method_name}
                            if params:
                                payload["params"] = params
                            await websocket.send(json.dumps(payload))
                            while True:
                                raw_message = await asyncio.wait_for(
                                    websocket.recv(),
                                    timeout=min(10, self.timeout_s),
                                )
                                message = json.loads(raw_message)
                                if message.get("id") == message_id:
                                    return message
                                if isinstance(message, dict):
                                    event_queue.append(message)

                        try:
                            await send_method("Network.enable")
                            await send_method("Page.enable")
                            await send_method("Page.navigate", {"url": "https://listen.tidal.com/"})
                            deadline = time.monotonic() + self.timeout_s
                            tested_tokens: set[str] = set()
                            while event_queue or time.monotonic() < deadline:
                                if event_queue:
                                    message = event_queue.pop(0)
                                else:
                                    remaining = max(0.1, deadline - time.monotonic())
                                    try:
                                        raw_message = await asyncio.wait_for(
                                            websocket.recv(),
                                            timeout=remaining,
                                        )
                                    except asyncio.TimeoutError:
                                        break
                                    message = json.loads(raw_message)
                                access_token = self._load_bearer_token(message)
                                if not access_token or access_token in tested_tokens:
                                    continue
                                tested_tokens.add(access_token)
                                session_payload = self._load_session_payload(access_token)
                                if session_payload:
                                    return access_token, session_payload
                            self.last_error = (
                                "browser_tokens_rejected"
                                if tested_tokens
                                else "authorization_not_observed"
                            )
                            return "", {}
                        finally:
                            message_id += 1
                            try:
                                await websocket.send(
                                    json.dumps({"id": message_id, "method": "Page.close"})
                                )
                            except WebSocketException:
                                self.last_warning = "cdp_target_close_failed"

                def _create_page_target(self) -> dict[str, object]:
                    target_url = (
                        f"http://127.0.0.1:{self.debug_port}/json/new?"
                        + quote("about:blank", safe="")
                    )
                    request = Request(target_url, method="PUT")
                    with urlopen(request, timeout=min(10, self.timeout_s)) as response:
                        payload = json.loads(response.read().decode("utf-8", "replace"))
                    if not isinstance(payload, dict):
                        self.last_error = "cdp_target_invalid"
                        return {}
                    self.target_id = str(payload.get("id") or "").strip()
                    return payload

                def _close_page_target(self) -> None:
                    if not self.target_id:
                        return
                    close_url = (
                        f"http://127.0.0.1:{self.debug_port}/json/close/"
                        + quote(self.target_id, safe="")
                    )
                    try:
                        with urlopen(close_url, timeout=min(5, self.timeout_s)) as response:
                            response.read()
                    except HTTPError as exc:
                        if int(exc.code or 500) != 404:
                            self.last_warning = f"cdp_target_close_http_{int(exc.code or 500)}"
                    except (URLError, OSError):
                        self.last_warning = "cdp_target_close_failed"
                    finally:
                        self.target_id = ""

                @staticmethod
                def _load_bearer_token(message: dict[str, object]) -> str:
                    method_name = str(message.get("method") or "")
                    params = message.get("params")
                    if not isinstance(params, dict):
                        return ""
                    headers: object = {}
                    if method_name == "Network.requestWillBeSent":
                        request_payload = params.get("request")
                        if isinstance(request_payload, dict):
                            headers = request_payload.get("headers")
                    elif method_name == "Network.requestWillBeSentExtraInfo":
                        headers = params.get("headers")
                    if not isinstance(headers, dict):
                        return ""
                    authorization = next(
                        (
                            str(value or "").strip()
                            for key, value in headers.items()
                            if str(key).casefold() == "authorization"
                        ),
                        "",
                    )
                    scheme, separator, token = authorization.partition(" ")
                    if separator and scheme.casefold() == "bearer" and len(token.strip()) > 32:
                        return token.strip()
                    return ""

                def _load_session_payload(self, access_token: str) -> dict[str, object]:
                    request = Request(
                        "https://api.tidal.com/v1/sessions",
                        headers={
                            "Accept": "application/json",
                            "Authorization": "Bearer " + access_token,
                            "Origin": "https://listen.tidal.com",
                            "Referer": "https://listen.tidal.com/",
                            "User-Agent": self.user_agent,
                            "x-tidal-token": self.public_web_token,
                        },
                    )
                    try:
                        with urlopen(request, timeout=min(20, self.timeout_s)) as response:
                            if int(getattr(response, "status", 200)) // 100 != 2:
                                return {}
                            payload = json.loads(response.read().decode("utf-8", "replace"))
                    except (HTTPError, URLError, OSError, json.JSONDecodeError):
                        return {}
                    if not isinstance(payload, dict):
                        return {}
                    if not str(payload.get("userId") or "").strip():
                        return {}
                    return payload

                @staticmethod
                def _load_token_expiry(access_token: str) -> int | None:
                    try:
                        payload_segment = access_token.split(".")[1]
                        padding = "=" * (-len(payload_segment) % 4)
                        payload = json.loads(
                            base64.urlsafe_b64decode(payload_segment + padding).decode(
                                "utf-8",
                                "replace",
                            )
                        )
                    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
                        return None
                    if not isinstance(payload, dict):
                        return None
                    try:
                        return int(payload.get("exp") or 0)
                    except (TypeError, ValueError):
                        return None

                def _store_access_token(
                    self,
                    access_token: str,
                    session_payload: dict[str, object],
                ) -> None:
                    if self.token_path is None:
                        self.last_warning = "token_file_not_configured"
                        return
                    token_payload: dict[str, object] = {}
                    if self.token_path.is_file():
                        try:
                            existing_payload = json.loads(
                                self.token_path.read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            existing_payload = {}
                        if isinstance(existing_payload, dict):
                            token_payload.update(existing_payload)

                    current_time = int(time.time())
                    expires_at = self._load_token_expiry(access_token)
                    if expires_at is None:
                        expires_at = current_time + 300
                    token_payload.update(
                        {
                            "access_token": access_token,
                            "expires_at": expires_at,
                            "expires_in": max(0, expires_at - current_time),
                            "session_id": str(session_payload.get("sessionId") or "").strip(),
                            "source": "browser_cdp_network",
                            "token_type": "Bearer",
                            "updated_at": current_time,
                            "user_id": str(session_payload.get("userId") or "").strip(),
                        }
                    )

                    temporary_path = ""
                    try:
                        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        file_descriptor, temporary_path = tempfile.mkstemp(
                            prefix=f".{self.token_path.name}.",
                            suffix=".tmp",
                            dir=str(self.token_path.parent),
                        )
                        os.fchmod(file_descriptor, 0o600)
                        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                            json.dump(token_payload, handle, ensure_ascii=True, indent=2)
                            handle.write("\n")
                        os.replace(temporary_path, self.token_path)
                        temporary_path = ""
                        os.chmod(self.token_path, 0o600)
                    except OSError as exc:
                        self.last_warning = f"token_store_failed:{type(exc).__name__}"
                    finally:
                        if temporary_path:
                            try:
                                Path(temporary_path).unlink(missing_ok=True)
                            except OSError:
                                self.last_warning = "token_temp_cleanup_failed"
            """
        ).strip()

    def _load_favorite_track_api_only_command(self, *, arguments: dict[str, Any]) -> str:
        track_id = str(arguments.get("track_id") or "").strip()
        if not track_id:
            return (
                "echo 'error=missing_track_id_api_only'; "
                "echo 'hint=pass_track_id_argument'; "
                "echo playback_backend=api_only; "
                "exit 1;"
            )
        if not track_id.isdigit():
            return "echo 'error=invalid_track_id'; exit 1;"

        country_code = self._load_country_code(arguments.get("country_code"), default_value="DE")
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=0,
            maximum=65535,
        )
        token_refresh_timeout_s = self._load_bounded_int(
            arguments.get("token_refresh_timeout_s"),
            default_value=45,
            minimum=5,
            maximum=120,
        )
        track_id_payload = shlex.quote(track_id)
        country_code_payload = shlex.quote(country_code)
        python_script = dedent(
            r"""
            import asyncio
            import base64
            import binascii
            import json
            import os
            import tempfile
            import time
            from pathlib import Path
            from urllib.error import HTTPError, URLError
            from urllib.parse import quote, urlencode
            from urllib.request import Request, urlopen

            try:
                import websockets
                from websockets.exceptions import WebSocketException
            except ImportError:
                websockets = None

                class WebSocketException(Exception):
                    pass

            __TRACK_METADATA_HELPERS__

            PUBLIC_WEB_TOKEN = "__PUBLIC_WEB_TOKEN__"
            USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

            __BROWSER_ACCESS_TOKEN_SERVICE__

            def emit(message: str) -> None:
                print(message, flush=True)

            track_id = str(os.environ.get("WEBPLAYER_TRACK_ID") or "").strip()
            country_code = str(os.environ.get("WEBPLAYER_COUNTRY_CODE") or "DE").strip().upper() or "DE"
            if not track_id:
                emit("error=missing_track_id_api_only")
                raise SystemExit(1)

            def load_token_path() -> Path | None:
                token_file = str(
                    os.environ.get("TIDAL_API_TOKEN_FILE")
                    or os.path.expanduser("~/.config/webplayer-mcp/tidal-token.json")
                ).strip()
                if token_file.startswith("%h/"):
                    token_file = str(Path.home() / token_file.removeprefix("%h/"))
                return Path(token_file) if token_file else None

            def load_access_token() -> str:
                try:
                    from alde.tidal_oauth import TidalOAuthTokenService
                except Exception:
                    TidalOAuthTokenService = None  # type: ignore[assignment]
                if TidalOAuthTokenService is not None:
                    try:
                        token_service = TidalOAuthTokenService(timeout_s=30)
                        oauth_access_token = str(token_service.load_access_token() or "").strip()
                        if oauth_access_token:
                            return oauth_access_token
                    except Exception:
                        pass

                token_path = load_token_path()
                if token_path is not None and token_path.is_file():
                    try:
                        token_payload = json.loads(token_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        token_payload = {}
                    if isinstance(token_payload, dict):
                        access_token_value = str(token_payload.get("access_token") or "").strip()
                        expires_at = token_payload.get("expires_at")
                        token_is_current = not isinstance(expires_at, (int, float)) or (
                            float(expires_at) > time.time() + 60
                        )
                        if access_token_value and token_is_current:
                            return access_token_value

                browser_token_service = TidalBrowserAccessTokenService(
                    debug_port=int(os.environ.get("WEBPLAYER_CDP_PORT") or 9222),
                    timeout_s=int(os.environ.get("WEBPLAYER_CDP_TOKEN_TIMEOUT_S") or 45),
                    token_path=token_path,
                    public_web_token=str(
                        os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN")
                        or PUBLIC_WEB_TOKEN
                    ),
                    user_agent=USER_AGENT,
                )
                browser_access_token = browser_token_service.load_access_token()
                if browser_access_token:
                    emit("access_token_source=browser_cdp_network")
                    if browser_token_service.last_warning:
                        emit(f"token_refresh_warning={browser_token_service.last_warning}")
                    return browser_access_token
                if browser_token_service.last_error:
                    emit(f"token_refresh_error={browser_token_service.last_error}")
                return ""

            access_token = load_access_token()
            if not access_token:
                emit("error=missing_access_token")
                raise SystemExit(2)

            headers = {
                "Accept": "application/json",
                "Authorization": "Bearer " + access_token,
                "Origin": "https://listen.tidal.com",
                "Referer": "https://listen.tidal.com/",
                "User-Agent": USER_AGENT,
                "x-tidal-token": str(os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN") or PUBLIC_WEB_TOKEN),
            }

            def fetch_payload(url: str, headers_payload: dict[str, str], *, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
                try:
                    request = Request(url, data=body, headers=headers_payload, method=method)
                    with urlopen(request, timeout=20) as response:
                        status_code = int(getattr(response, "status", 200))
                        payload = response.read().decode("utf-8", "replace")
                    return status_code, payload
                except HTTPError as exc:
                    status_code = int(exc.code or 500)
                    payload = exc.read().decode("utf-8", "replace") if exc.fp else ""
                    return status_code, payload
                except URLError as exc:
                    return 0, f"url_error:{exc.reason}"
                except Exception as exc:
                    return 0, f"request_failed:{exc}"

            def load_stream_quality_via_relationship_items(
                track_id_value: str,
                country_code_value: str,
                headers_payload: dict[str, str],
            ) -> tuple[str, str]:
                track_id_clean = str(track_id_value or "").strip()
                if not track_id_clean:
                    return "", "track_id_missing"
                relationship_url = (
                    "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items?"
                    + urlencode({"page[size]": 100})
                )
                relationship_status, relationship_payload = fetch_payload(relationship_url, headers_payload)
                relationship_source = "relationship_not_run"
                if relationship_status // 100 != 2 or not relationship_payload:
                    relationship_source = f"relationship_http_{relationship_status}"
                else:
                    try:
                        relationship_data = json.loads(relationship_payload)
                    except json.JSONDecodeError:
                        relationship_source = "relationship_json_error"
                    else:
                        relationship_items = relationship_data.get("data") if isinstance(relationship_data, dict) else []
                        if not isinstance(relationship_items, list):
                            relationship_source = "relationship_items_missing"
                        else:
                            relationship_track_ids = {
                                str(item.get("id") or "").strip()
                                for item in relationship_items
                                if isinstance(item, dict) and str(item.get("id") or "").strip()
                            }
                            relationship_source = (
                                "relationship_track_match"
                                if track_id_clean in relationship_track_ids
                                else "relationship_track_missing"
                            )
                track_url = (
                    "https://openapi.tidal.com/v2/tracks/"
                    + track_id_clean
                    + "?"
                    + urlencode({"countryCode": country_code_value})
                )
                track_status, track_payload = fetch_payload(track_url, headers_payload)
                if track_status // 100 != 2 or not track_payload:
                    return "", f"track_http_{track_status}"
                try:
                    track_data = json.loads(track_payload)
                except json.JSONDecodeError:
                    return "", "track_json_error"
                data_object = track_data.get("data") if isinstance(track_data, dict) else {}
                attributes = data_object.get("attributes") if isinstance(data_object, dict) else {}
                if not isinstance(attributes, dict):
                    return "", "track_attributes_missing"
                media_tags = attributes.get("mediaTags")
                if isinstance(media_tags, list):
                    normalized_media_tags = [
                        str(tag).strip()
                        for tag in media_tags
                        if str(tag).strip()
                    ]
                    if normalized_media_tags:
                        quality_value = ",".join(normalized_media_tags)
                        if relationship_source == "relationship_track_match":
                            return quality_value, "openapi_tracks_mediaTags_via_relationship_items"
                        return quality_value, f"openapi_tracks_mediaTags_{relationship_source}"
                audio_quality = str(attributes.get("audioQuality") or "").strip()
                if audio_quality:
                    if relationship_source == "relationship_track_match":
                        return audio_quality, "openapi_tracks_audioQuality_via_relationship_items"
                    return audio_quality, f"openapi_tracks_audioQuality_{relationship_source}"
                return "", f"track_quality_missing_{relationship_source}"

            session_url = "https://api.tidal.com/v1/sessions?" + urlencode({"countryCode": country_code})
            session_status, session_payload = fetch_payload(session_url, headers)
            if session_status // 100 != 2:
                emit("error=session_lookup_failed")
                emit(f"session_status={session_status}")
                if session_payload:
                    emit(f"session_payload={session_payload[:500].replace(chr(10), ' ').replace(chr(13), ' ')}")
                raise SystemExit(3)

            try:
                session_data = json.loads(session_payload) if session_payload else {}
            except json.JSONDecodeError:
                session_data = {}

            user_id = str(session_data.get("userId") or "").strip() if isinstance(session_data, dict) else ""
            session_id = str(session_data.get("sessionId") or "").strip() if isinstance(session_data, dict) else ""
            if not user_id:
                emit("error=missing_user_id")
                raise SystemExit(4)

            favorite_payload = json.dumps({"data": [{"id": track_id, "type": "tracks"}]}).encode("utf-8")
            favorite_headers = dict(headers)
            favorite_headers["Accept"] = "application/vnd.api+json"
            favorite_headers["Content-Type"] = "application/vnd.api+json"
            favorite_url = "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items"
            favorite_status, favorite_response = fetch_payload(
                favorite_url,
                favorite_headers,
                method="POST",
                body=favorite_payload,
            )

            if favorite_status // 100 != 2 and favorite_status != 409:
                emit("error=favorite_write_failed")
                emit(f"favorite_status={favorite_status}")
                if favorite_response:
                    emit(f"favorite_payload={favorite_response[:500].replace(chr(10), ' ').replace(chr(13), ' ')}")
                raise SystemExit(5)

            favorite_result = "already_present" if favorite_status == 409 else "added"
            stream_quality, stream_quality_source = load_stream_quality_via_relationship_items(
                track_id,
                country_code,
                favorite_headers,
            )
            quality_value = normalize_stream_quality(stream_quality)
            bit_depth, sample_rate_khz, bitrate_value = load_audio_format_reference(quality_value)
            emit("status=Favorited")
            emit(f"track_id={track_id}")
            emit(f"user_id={user_id}")
            if session_id:
                emit(f"token_session_id={session_id}")
            if stream_quality:
                emit(f"stream_quality={quality_value}")
            emit(f"stream_quality_source={stream_quality_source}")
            emit(f"quality={quality_value}")
            emit(f"bitrate={bitrate_value}")
            emit(f"bit_depth={bit_depth}")
            emit(f"sample_rate_khz={sample_rate_khz}")
            emit(f"favorite_result={favorite_result}")
            emit("favorite_state=liked")
            emit("favorite_verified=true")
            emit("favorite_verify_source=openapi_userCollectionTracks_me")
            emit(f"favorite_verify_openapi_status={favorite_status}")
            emit("favorite_verify_ids_status=not_run")
            emit("account_alignment_required=false")
            emit("favorite_backend=api_only")
            """
        ).replace(
            "__TRACK_METADATA_HELPERS__",
            self._load_track_metadata_helpers_script(),
        ).replace(
            "__BROWSER_ACCESS_TOKEN_SERVICE__",
            self._load_browser_access_token_service_script(),
        ).replace("__PUBLIC_WEB_TOKEN__", TidalApiService._DEFAULT_WEB_TOKEN).strip()
        return (
            "if ! command -v python3 >/dev/null 2>&1; then echo 'error=python3_missing'; exit 1; fi; "
            + f"WEBPLAYER_TRACK_ID={track_id_payload} "
            + f"WEBPLAYER_COUNTRY_CODE={country_code_payload} "
            + f"WEBPLAYER_CDP_PORT={cdp_port} "
            + f"WEBPLAYER_CDP_TOKEN_TIMEOUT_S={token_refresh_timeout_s} "
            + "python3 - <<'PY'\n"
            + python_script
            + "\nPY"
        )

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
        player_selector_env = shlex.quote(str(player_selector or "chromium"))
        python_script = dedent(
            r"""
            import base64
            import json
            import os
            import re
            import subprocess
            import time
            from pathlib import Path
            from urllib.error import HTTPError, URLError
            from urllib.parse import urlencode, urljoin
            from urllib.request import Request, urlopen

            __TRACK_METADATA_HELPERS__

            PUBLIC_WEB_TOKEN = "__PUBLIC_WEB_TOKEN__"
            USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

            def emit(message: str) -> None:
                print(message, flush=True)

            def run_playerctl(selected_player: str, *args: str) -> tuple[int, str, str]:
                completed = subprocess.run(
                    ["playerctl", "-p", selected_player, *args],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return int(completed.returncode), str(completed.stdout or "").strip(), str(completed.stderr or "").strip()

            def pick_player(prefix: str, wait_seconds: int) -> str:
                deadline = time.time() + max(0, int(wait_seconds))
                prefix_casefold = prefix.casefold()
                while True:
                    completed = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, check=False)
                    players = [line.strip() for line in str(completed.stdout or "").splitlines() if line.strip()]
                    selected = next((player for player in players if player.casefold().startswith(prefix_casefold)), "")
                    if not selected and players:
                        selected = players[0]
                    if selected:
                        return selected
                    if time.time() >= deadline:
                        return ""
                    time.sleep(1)

            def extract_track_id_from_url(track_url: str) -> str:
                url_value = str(track_url or "").strip()
                if not url_value:
                    return ""
                match = re.search(r"/track/([0-9]+)", url_value)
                if match:
                    return str(match.group(1) or "").strip()
                match = re.search(r"tidal:track:([0-9]+)", url_value)
                if match:
                    return str(match.group(1) or "").strip()
                return ""

            def extract_track_id_from_mpris_trackid(track_identifier: str) -> str:
                track_identifier_value = str(track_identifier or "").strip()
                if not track_identifier_value:
                    return ""
                match = re.search(r"/track/([0-9]+)", track_identifier_value, re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip()
                match = re.search(r"tidal:track:([0-9]+)", track_identifier_value, re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip()
                match = re.search(r"/TrackList/Track([0-9]+)$", track_identifier_value)
                if match:
                    return str(match.group(1) or "").strip()
                return ""

            def _load_candidate_cache_profile_dirs() -> list[Path]:
                roots = [
                    Path.home() / "snap/chromium/common/chromium",
                    Path.home() / ".config/chromium",
                    Path.home() / ".config/google-chrome",
                ]
                candidates: list[Path] = []
                for root in roots:
                    if not root.exists():
                        continue
                    for profile_dir in root.iterdir():
                        if not profile_dir.is_dir():
                            continue
                        cache_data_dir = profile_dir / "Cache" / "Cache_Data"
                        if not cache_data_dir.is_dir():
                            continue
                        candidates.append(profile_dir)
                candidates.sort(
                    key=lambda path: ((path / "Cache" / "Cache_Data").stat().st_mtime, path.name),
                    reverse=True,
                )
                return candidates

            def load_track_id_from_cache(title: str, artist: str, album: str) -> str:
                title_value = str(title or "").strip()
                if not title_value:
                    return ""
                profile_dirs = _load_candidate_cache_profile_dirs()
                if not profile_dirs:
                    return ""
                title_fragment = json.dumps(title_value, ensure_ascii=False)[1:-1].encode("utf-8")
                artist_value = str(artist or "").strip()
                album_value = str(album or "").strip()
                artist_fragment = (
                    json.dumps(artist_value, ensure_ascii=False)[1:-1].encode("utf-8") if artist_value else b""
                )
                album_fragment = (
                    json.dumps(album_value, ensure_ascii=False)[1:-1].encode("utf-8") if album_value else b""
                )
                pattern = re.compile(rb'"id":([0-9]+),"title":"' + re.escape(title_fragment) + rb'"')
                fallback_track_id = ""

                for profile_dir in profile_dirs:
                    cache_data_dir = profile_dir / "Cache" / "Cache_Data"
                    try:
                        cache_files = sorted(
                            (child for child in cache_data_dir.iterdir() if child.is_file()),
                            key=lambda path: path.stat().st_mtime,
                            reverse=True,
                        )
                    except OSError:
                        continue

                    for cache_file in cache_files:
                        try:
                            cache_bytes = cache_file.read_bytes()
                        except OSError:
                            continue
                        if title_fragment not in cache_bytes:
                            continue
                        for match in pattern.finditer(cache_bytes):
                            candidate_track_id = str(match.group(1).decode("ascii", "ignore")).strip()
                            if not candidate_track_id:
                                continue
                            if not fallback_track_id:
                                fallback_track_id = candidate_track_id
                            context_start = max(0, match.start() - 2048)
                            context_end = min(len(cache_bytes), match.end() + 2048)
                            context = cache_bytes[context_start:context_end]
                            artist_matches = not artist_fragment or artist_fragment in context
                            album_matches = not album_fragment or album_fragment in context
                            if artist_matches and album_matches:
                                return candidate_track_id
                return fallback_track_id

            def _extract_json_object(payload: bytes, start_index: int) -> bytes:
                depth = 0
                in_string = False
                escaped = False
                for index in range(start_index, len(payload)):
                    character = payload[index]
                    if in_string:
                        if escaped:
                            escaped = False
                            continue
                        if character == 92:
                            escaped = True
                            continue
                        if character == 34:
                            in_string = False
                        continue
                    if character == 34:
                        in_string = True
                        continue
                    if character == 123:
                        depth += 1
                        continue
                    if character == 125:
                        depth -= 1
                        if depth == 0:
                            return payload[start_index:index + 1]
                return b""

            def load_browser_favorites_snapshot() -> tuple[str, set[str]]:
                profile_dirs = _load_candidate_cache_profile_dirs()
                for profile_dir in profile_dirs:
                    cache_data_dir = profile_dir / "Cache" / "Cache_Data"
                    try:
                        cache_files = sorted(
                            (child for child in cache_data_dir.iterdir() if child.is_file()),
                            key=lambda path: path.stat().st_mtime,
                            reverse=True,
                        )
                    except OSError:
                        continue
                    for cache_file in cache_files:
                        try:
                            cache_bytes = cache_file.read_bytes()
                        except OSError:
                            continue
                        if b"/favorites/ids?" not in cache_bytes:
                            continue
                        for match in re.finditer(rb"https://tidal\.com/v1/users/([0-9]+)/favorites/ids\?", cache_bytes):
                            browser_user_id = str(match.group(1).decode("ascii", "ignore")).strip()
                            if not browser_user_id:
                                continue
                            json_start = cache_bytes.find(b"{", match.end())
                            if json_start < 0:
                                continue
                            json_payload = _extract_json_object(cache_bytes, json_start)
                            if not json_payload:
                                continue
                            try:
                                parsed_payload = json.loads(json_payload.decode("utf-8", "replace"))
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(parsed_payload, dict):
                                continue
                            track_ids = parsed_payload.get("TRACK")
                            if not isinstance(track_ids, list):
                                continue
                            favorite_track_ids = {
                                str(track_id).strip()
                                for track_id in track_ids
                                if str(track_id).strip()
                            }
                            return browser_user_id, favorite_track_ids
                return "", set()

            def load_access_token() -> str:
                try:
                    from alde.tidal_oauth import TidalOAuthTokenService
                except Exception:
                    TidalOAuthTokenService = None  # type: ignore[assignment]
                if TidalOAuthTokenService is not None:
                    try:
                        token_service = TidalOAuthTokenService(timeout_s=30)
                        oauth_access_token = str(token_service.load_access_token() or "").strip()
                        if oauth_access_token:
                            return oauth_access_token
                    except Exception:
                        pass

                token_file = str(
                    os.environ.get("TIDAL_API_TOKEN_FILE")
                    or os.path.expanduser("~/.config/webplayer-mcp/tidal-token.json")
                ).strip()
                if token_file.startswith("%h/"):
                    token_file = str(Path.home() / token_file.removeprefix("%h/"))
                if not token_file:
                    return ""
                token_path = Path(token_file)
                if not token_path.is_file():
                    return ""
                try:
                    token_payload = json.loads(token_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return ""
                if not isinstance(token_payload, dict):
                    return ""
                access_token_value = str(token_payload.get("access_token") or "").strip()
                if not access_token_value:
                    return ""
                expires_at = token_payload.get("expires_at")
                if isinstance(expires_at, (int, float)) and float(expires_at) <= time.time() + 60:
                    return ""
                return access_token_value

            def fetch_payload_soft(url: str, headers_payload: dict[str, str], *, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
                try:
                    request = Request(url, data=body, headers=headers_payload, method=method)
                    with urlopen(request, timeout=20) as response:
                        status_code = int(getattr(response, "status", 200))
                        payload = response.read().decode("utf-8", "replace")
                    return status_code, payload
                except HTTPError as exc:
                    status_code = int(exc.code or 500)
                    payload = exc.read().decode("utf-8", "replace") if exc.fp else ""
                    return status_code, payload
                except URLError as exc:
                    return 0, f"url_error:{exc.reason}"
                except Exception as exc:
                    return 0, f"request_failed:{exc}"

            def load_track_id_from_public_search(
                title_value: str,
                artist_value: str,
                country_code_value: str,
            ) -> str:
                title_clean = str(title_value or "").strip()
                if not title_clean:
                    return ""
                query = " ".join(
                    part for part in (title_clean, str(artist_value or "").strip()) if part
                )
                request_headers = {
                    "Accept": "application/json",
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/search",
                    "User-Agent": USER_AGENT,
                    "x-tidal-token": str(
                        os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN")
                        or PUBLIC_WEB_TOKEN
                    ),
                }
                search_url = "https://listen.tidal.com/v1/search?" + urlencode(
                    {
                        "query": query,
                        "countryCode": country_code_value,
                        "types": "TRACKS",
                        "limit": 5,
                        "offset": 0,
                    }
                )
                search_status, search_payload = fetch_payload_soft(search_url, request_headers)
                if search_status // 100 != 2 or not search_payload:
                    return ""
                try:
                    search_data = json.loads(search_payload)
                except json.JSONDecodeError:
                    return ""
                tracks_payload = search_data.get("tracks") if isinstance(search_data, dict) else {}
                items_payload = tracks_payload.get("items") if isinstance(tracks_payload, dict) else []
                if not isinstance(items_payload, list):
                    return ""
                title_casefold = title_clean.casefold()
                fallback_track_id = ""
                for item in items_payload:
                    if not isinstance(item, dict):
                        continue
                    candidate_track_id = str(item.get("id") or "").strip()
                    if not candidate_track_id:
                        continue
                    if not fallback_track_id:
                        fallback_track_id = candidate_track_id
                    candidate_title = str(item.get("title") or "").strip()
                    if candidate_title.casefold() == title_casefold:
                        return candidate_track_id
                return fallback_track_id

            def load_session_info(access_token: str) -> tuple[str, str]:
                access_token_value = str(access_token or "").strip()
                if not access_token_value:
                    return "DE", ""
                x_tidal_token = str(os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN") or PUBLIC_WEB_TOKEN)
                headers_payload = {
                    "Accept": "application/json",
                    "Authorization": "Bearer " + access_token_value,
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/",
                    "User-Agent": USER_AGENT,
                    "x-tidal-token": x_tidal_token,
                }
                session_url = "https://api.tidal.com/v1/sessions?" + urlencode({"countryCode": "DE"})
                session_status, session_payload = fetch_payload_soft(session_url, headers_payload)
                if session_status // 100 != 2 or not session_payload:
                    return "DE", ""
                try:
                    session_data = json.loads(session_payload)
                except json.JSONDecodeError:
                    return "DE", ""
                if not isinstance(session_data, dict):
                    return "DE", ""
                country_code = str(session_data.get("countryCode") or "DE").strip().upper() or "DE"
                user_id = str(session_data.get("userId") or "").strip()
                return country_code, user_id

            def load_stream_quality_via_relationship_items(
                track_id_value: str,
                country_code_value: str,
                access_token: str,
            ) -> tuple[str, str, str]:
                track_id_clean = str(track_id_value or "").strip()
                if not track_id_clean:
                    return "", "track_id_missing", "unknown"
                access_token_value = str(access_token or "").strip()
                if not access_token_value:
                    return "", "missing_access_token", "unknown"
                x_tidal_token = str(os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN") or PUBLIC_WEB_TOKEN)
                headers_payload = {
                    "Accept": "application/vnd.api+json",
                    "Authorization": "Bearer " + access_token_value,
                    "Content-Type": "application/vnd.api+json",
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/",
                    "User-Agent": USER_AGENT,
                    "x-tidal-token": x_tidal_token,
                }
                next_relationship_url = (
                    "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items?"
                    + urlencode({"page[size]": 100})
                )
                relationship_source = "relationship_not_run"
                favorite_state = "unknown"
                for _page_index in range(20):
                    relationship_status, relationship_payload = fetch_payload_soft(
                        next_relationship_url,
                        headers_payload,
                    )
                    if relationship_status // 100 != 2 or not relationship_payload:
                        relationship_source = f"relationship_http_{relationship_status}"
                        break
                    try:
                        relationship_data = json.loads(relationship_payload)
                    except json.JSONDecodeError:
                        relationship_source = "relationship_json_error"
                        break
                    relationship_items = relationship_data.get("data") if isinstance(relationship_data, dict) else []
                    if not isinstance(relationship_items, list):
                        relationship_source = "relationship_items_missing"
                        break
                    relationship_track_ids = {
                        str(item.get("id") or "").strip()
                        for item in relationship_items
                        if isinstance(item, dict) and str(item.get("id") or "").strip()
                    }
                    if track_id_clean in relationship_track_ids:
                        relationship_source = "relationship_track_match"
                        favorite_state = "liked"
                        break
                    links = relationship_data.get("links") if isinstance(relationship_data, dict) else {}
                    next_value = links.get("next") if isinstance(links, dict) else ""
                    if not isinstance(next_value, str) or not next_value.strip():
                        relationship_source = "relationship_track_missing"
                        favorite_state = "unliked"
                        break
                    next_relationship_url = urljoin(next_relationship_url, next_value.strip())
                else:
                    relationship_source = "relationship_scan_limit"
                track_url = (
                    "https://openapi.tidal.com/v2/tracks/"
                    + track_id_clean
                    + "?"
                    + urlencode({"countryCode": country_code_value})
                )
                track_status, track_payload = fetch_payload_soft(track_url, headers_payload)
                if track_status // 100 != 2 or not track_payload:
                    return "", f"track_http_{track_status}_{relationship_source}", favorite_state
                try:
                    track_data = json.loads(track_payload)
                except json.JSONDecodeError:
                    return "", f"track_json_error_{relationship_source}", favorite_state
                data_object = track_data.get("data") if isinstance(track_data, dict) else {}
                attributes = data_object.get("attributes") if isinstance(data_object, dict) else {}
                if not isinstance(attributes, dict):
                    return "", f"track_attributes_missing_{relationship_source}", favorite_state
                media_tags = attributes.get("mediaTags")
                if isinstance(media_tags, list):
                    quality_value = normalize_stream_quality(media_tags)
                    if quality_value != "none":
                        return (
                            quality_value,
                            (
                                "openapi_tracks_mediaTags_via_relationship_items"
                                if relationship_source == "relationship_track_match"
                                else f"openapi_tracks_mediaTags_{relationship_source}"
                            ),
                            favorite_state,
                        )
                audio_quality = str(attributes.get("audioQuality") or "").strip()
                if audio_quality:
                    return (
                        normalize_stream_quality(audio_quality),
                        (
                            "openapi_tracks_audioQuality_via_relationship_items"
                            if relationship_source == "relationship_track_match"
                            else f"openapi_tracks_audioQuality_{relationship_source}"
                        ),
                        favorite_state,
                    )
                return "", f"track_quality_missing_{relationship_source}", favorite_state

            def load_tidal_track_metadata(
                track_id_value: str,
                country_code_value: str,
                access_token: str,
            ) -> dict[str, str]:
                track_id_clean = str(track_id_value or "").strip()
                if not track_id_clean:
                    return {"track_metadata_source": "track_id_missing", "lyrics_source": "track_id_missing"}
                x_tidal_token = str(os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN") or PUBLIC_WEB_TOKEN)
                public_headers = {
                    "Accept": "application/json",
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/",
                    "User-Agent": USER_AGENT,
                    "x-tidal-token": x_tidal_token,
                }
                access_token_value = str(access_token or "").strip()
                metadata: dict[str, str] = {
                    "track_metadata_source": "not_run",
                    "lyrics_source": "missing_access_token" if not access_token_value else "not_available",
                }

                if access_token_value:
                    openapi_headers = dict(public_headers)
                    openapi_headers["Accept"] = "application/vnd.api+json"
                    openapi_headers["Authorization"] = "Be" "arer " + access_token_value
                    openapi_url = (
                        "https://openapi.tidal.com/v2/tracks/"
                        + track_id_clean
                        + "?"
                        + urlencode(
                            {
                                "countryCode": country_code_value,
                                "include": "albums.coverArt,lyrics",
                            }
                        )
                    )
                    openapi_status, openapi_payload = fetch_payload_soft(openapi_url, openapi_headers)
                    metadata["track_metadata_source"] = f"tidal_v2_http_{openapi_status}"
                    if openapi_status // 100 == 2 and openapi_payload:
                        try:
                            openapi_data = json.loads(openapi_payload)
                        except json.JSONDecodeError:
                            metadata["track_metadata_source"] = "tidal_v2_json_error"
                        else:
                            if isinstance(openapi_data, dict):
                                metadata["track_metadata_source"] = "tidal_v2"
                                data_object = openapi_data.get("data")
                                attributes = data_object.get("attributes") if isinstance(data_object, dict) else {}
                                if isinstance(attributes, dict):
                                    bpm = attributes.get("bpm")
                                    if isinstance(bpm, (int, float)) and bpm > 0:
                                        metadata["bpm"] = str(
                                            int(bpm) if float(bpm).is_integer() else bpm
                                        )
                                    key_value = str(attributes.get("key") or "").strip()
                                    key_scale = str(attributes.get("keyScale") or "").strip()
                                    if key_value and key_value != "UNKNOWN":
                                        metadata["key"] = key_value
                                    if key_scale and key_scale != "UNKNOWN":
                                        metadata["key_scale"] = key_scale
                                    musical_key = load_musical_key(
                                        metadata.get("key"),
                                        metadata.get("key_scale"),
                                    )
                                    if musical_key:
                                        metadata["musical_key"] = musical_key
                                    metadata["catalog_quality"] = normalize_stream_quality(
                                        attributes.get("mediaTags")
                                    )

                                included_items = openapi_data.get("included")
                                if isinstance(included_items, list):
                                    artwork_candidates: list[tuple[int, str]] = []
                                    for included_item in included_items:
                                        if not isinstance(included_item, dict):
                                            continue
                                        item_type = str(included_item.get("type") or "").strip()
                                        item_attributes = included_item.get("attributes")
                                        if not isinstance(item_attributes, dict):
                                            continue
                                        if item_type == "artworks":
                                            artwork_files = item_attributes.get("files")
                                            if not isinstance(artwork_files, list):
                                                continue
                                            for artwork_file in artwork_files:
                                                if not isinstance(artwork_file, dict):
                                                    continue
                                                href = str(artwork_file.get("href") or "").strip()
                                                artwork_meta = artwork_file.get("meta")
                                                width = artwork_meta.get("width") if isinstance(artwork_meta, dict) else 0
                                                if href and isinstance(width, int):
                                                    artwork_candidates.append((abs(width - 320), href))
                                        elif item_type == "lyrics":
                                            lyrics_text = str(item_attributes.get("text") or "").strip()
                                            lyrics_subtitles = str(item_attributes.get("lrcText") or "").strip()
                                            if lyrics_text:
                                                metadata["lyrics_text_base64"] = base64.b64encode(
                                                    lyrics_text.encode("utf-8")
                                                ).decode("ascii")
                                            if lyrics_subtitles:
                                                metadata["lyrics_subtitles_base64"] = base64.b64encode(
                                                    lyrics_subtitles.encode("utf-8")
                                                ).decode("ascii")
                                            if lyrics_text or lyrics_subtitles:
                                                metadata["lyrics_source"] = "tidal_v2"
                                    if artwork_candidates:
                                        metadata["artwork_url"] = min(artwork_candidates)[1]

                track_url = (
                    "https://listen.tidal.com/v1/tracks/"
                    + track_id_clean
                    + "?"
                    + urlencode({"countryCode": country_code_value})
                )
                track_status, track_payload = fetch_payload_soft(track_url, public_headers)
                if track_status // 100 == 2 and track_payload:
                    try:
                        track_data = json.loads(track_payload)
                    except json.JSONDecodeError:
                        if metadata["track_metadata_source"] == "not_run":
                            metadata["track_metadata_source"] = "tidal_v1_json_error"
                    else:
                        if isinstance(track_data, dict):
                            metadata["track_metadata_source"] = (
                                "tidal_v2+tidal_v1"
                                if metadata["track_metadata_source"] == "tidal_v2"
                                else "tidal_v1"
                            )
                            bpm = track_data.get("bpm")
                            if "bpm" not in metadata and isinstance(bpm, (int, float)) and bpm > 0:
                                metadata["bpm"] = str(int(bpm) if float(bpm).is_integer() else bpm)
                            key_value = str(track_data.get("key") or "").strip()
                            key_scale = str(track_data.get("keyScale") or "").strip()
                            if "key" not in metadata and key_value and key_value != "UNKNOWN":
                                metadata["key"] = key_value
                            if "key_scale" not in metadata and key_scale and key_scale != "UNKNOWN":
                                metadata["key_scale"] = key_scale
                            musical_key = load_musical_key(
                                metadata.get("key"),
                                metadata.get("key_scale"),
                            )
                            if musical_key:
                                metadata["musical_key"] = musical_key
                            album_value = track_data.get("album")
                            cover_value = album_value.get("cover") if isinstance(album_value, dict) else ""
                            artwork_url = load_artwork_url(cover_value)
                            if artwork_url and "artwork_url" not in metadata:
                                metadata["artwork_url"] = artwork_url
                            media_metadata = track_data.get("mediaMetadata")
                            media_tags = (
                                media_metadata.get("tags")
                                if isinstance(media_metadata, dict)
                                else []
                            )
                            if metadata.get("catalog_quality") in {"", "none", None}:
                                metadata["catalog_quality"] = normalize_stream_quality(
                                    media_tags,
                                    track_data.get("audioQuality"),
                                )
                elif metadata["track_metadata_source"] == "not_run":
                    metadata["track_metadata_source"] = f"tidal_v1_http_{track_status}"

                if (
                    not access_token_value
                    or metadata.get("lyrics_text_base64")
                    or metadata.get("lyrics_subtitles_base64")
                ):
                    return metadata
                lyrics_headers = dict(public_headers)
                lyrics_headers["Authorization"] = "Be" "arer " + access_token_value
                lyrics_url = (
                    "https://api.tidal.com/v1/tracks/"
                    + track_id_clean
                    + "/lyrics?"
                    + urlencode({"countryCode": country_code_value})
                )
                lyrics_status, lyrics_payload = fetch_payload_soft(lyrics_url, lyrics_headers)
                metadata["lyrics_source"] = f"tidal_v1_http_{lyrics_status}"
                if lyrics_status // 100 != 2 or not lyrics_payload:
                    return metadata
                try:
                    lyrics_data = json.loads(lyrics_payload)
                except json.JSONDecodeError:
                    metadata["lyrics_source"] = "tidal_v1_json_error"
                    return metadata
                if not isinstance(lyrics_data, dict):
                    metadata["lyrics_source"] = "tidal_v1_payload_invalid"
                    return metadata
                lyrics_text = str(lyrics_data.get("lyrics") or "").strip()
                lyrics_subtitles = str(lyrics_data.get("subtitles") or "").strip()
                if lyrics_text:
                    metadata["lyrics_text_base64"] = base64.b64encode(lyrics_text.encode("utf-8")).decode("ascii")
                if lyrics_subtitles:
                    metadata["lyrics_subtitles_base64"] = base64.b64encode(
                        lyrics_subtitles.encode("utf-8")
                    ).decode("ascii")
                metadata["lyrics_source"] = "tidal_v1"
                return metadata

            def load_playback_context_metadata(
                track_id_value: str,
                country_code_value: str,
                access_token: str,
                requested_quality: str,
            ) -> dict[str, str]:
                track_id_clean = str(track_id_value or "").strip()
                access_token_value = str(access_token or "").strip()
                if not track_id_clean or not access_token_value:
                    return {"audio_format_source": "quality_reference"}
                x_tidal_token = str(os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN") or PUBLIC_WEB_TOKEN)
                playback_headers = {
                    "Accept": "application/json",
                    "Authorization": "Be" "arer " + access_token_value,
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/",
                    "User-Agent": USER_AGENT,
                    "x-tidal-token": x_tidal_token,
                }
                playback_url = (
                    "https://api.tidal.com/v1/tracks/"
                    + track_id_clean
                    + "/playbackinfopostpaywall?"
                    + urlencode(
                        {
                            "audioquality": normalize_stream_quality(requested_quality),
                            "playbackmode": "STREAM",
                            "assetpresentation": "FULL",
                            "countryCode": country_code_value,
                        }
                    )
                )
                playback_status, playback_payload = fetch_payload_soft(playback_url, playback_headers)
                if playback_status // 100 != 2 or not playback_payload:
                    return {"audio_format_source": f"quality_reference_playback_http_{playback_status}"}
                try:
                    playback_data = json.loads(playback_payload)
                except json.JSONDecodeError:
                    return {"audio_format_source": "quality_reference_playback_json_error"}
                if not isinstance(playback_data, dict):
                    return {"audio_format_source": "quality_reference_playback_payload_invalid"}
                playback_metadata: dict[str, str] = {
                    "audio_format_source": "playback_info",
                    "actual_quality": normalize_stream_quality(playback_data.get("audioQuality")),
                }
                bit_depth = playback_data.get("bitDepth")
                sample_rate = playback_data.get("sampleRate")
                if isinstance(bit_depth, (int, float)) and bit_depth > 0:
                    playback_metadata["bit_depth"] = str(int(bit_depth))
                if isinstance(sample_rate, (int, float)) and sample_rate > 0:
                    sample_rate_khz = float(sample_rate) / 1000
                    playback_metadata["sample_rate_khz"] = f"{sample_rate_khz:g}"

                manifest_value = str(playback_data.get("manifest") or "").strip()
                if manifest_value and (
                    "bit_depth" not in playback_metadata
                    or "sample_rate_khz" not in playback_metadata
                ):
                    try:
                        manifest_text = base64.b64decode(manifest_value).decode("utf-8", "replace")
                    except (ValueError, UnicodeDecodeError):
                        manifest_text = ""
                    if manifest_text:
                        sample_rate_match = re.search(r'audioSamplingRate=["\']([0-9]+)', manifest_text)
                        bit_depth_match = re.search(
                            r'id=["\'][^"\']*,[0-9]+,([0-9]+)["\']',
                            manifest_text,
                        )
                        if not bit_depth_match:
                            bit_depth_match = re.search(
                                r'X-COM-TIDAL-SAMPLE-DEPTH=([0-9]+)',
                                manifest_text,
                            )
                        if bit_depth_match:
                            playback_metadata["bit_depth"] = bit_depth_match.group(1)
                        if sample_rate_match:
                            playback_metadata["sample_rate_khz"] = (
                                f"{int(sample_rate_match.group(1)) / 1000:g}"
                            )
                        if bit_depth_match or sample_rate_match:
                            playback_metadata["audio_format_source"] = "playback_manifest"
                return playback_metadata

            selected_player = pick_player(os.environ.get("WEBPLAYER_PLAYER_SELECTOR", "chromium"), 0)
            if not selected_player:
                emit("error=no_player")
                raise SystemExit(1)

            status_rc, status, _status_error = run_playerctl(selected_player, "status")
            title_rc, title, title_error = run_playerctl(selected_player, "metadata", "xesam:title")
            artist_rc, artist, _artist_error = run_playerctl(selected_player, "metadata", "xesam:artist")
            album_rc, album, _album_error = run_playerctl(selected_player, "metadata", "xesam:album")
            track_url_rc, track_url, _track_url_error = run_playerctl(selected_player, "metadata", "xesam:url")
            mpris_trackid_rc, mpris_trackid, _mpris_trackid_error = run_playerctl(selected_player, "metadata", "mpris:trackid")
            artwork_rc, mpris_artwork_url, _artwork_error = run_playerctl(
                selected_player,
                "metadata",
                "mpris:artUrl",
            )
            duration_rc, duration_microseconds, _duration_error = run_playerctl(
                selected_player,
                "metadata",
                "mpris:length",
            )
            position_rc, position_seconds, _position_error = run_playerctl(selected_player, "position")

            if title_rc != 0 or not title:
                emit("error=missing_track_title")
                if title_error:
                    emit(f"playerctl_title_error={title_error}")
                raise SystemExit(1)

            if status_rc != 0:
                status = "unknown"
            if artist_rc != 0:
                artist = ""
            if album_rc != 0:
                album = ""
            if track_url_rc != 0:
                track_url = ""
            if mpris_trackid_rc != 0:
                mpris_trackid = ""
            if artwork_rc != 0:
                mpris_artwork_url = ""
            if duration_rc != 0:
                duration_microseconds = ""
            if position_rc != 0:
                position_seconds = ""

            access_token = load_access_token()
            country_code, collection_user_id = load_session_info(access_token)
            track_id_source = ""
            track_id = extract_track_id_from_url(track_url)
            if track_id:
                track_id_source = "xesam_url"
            if not track_id:
                track_id = extract_track_id_from_mpris_trackid(mpris_trackid)
                if track_id:
                    track_id_source = "mpris_trackid"
            if not track_id:
                track_id = load_track_id_from_cache(title, artist, album)
                if track_id:
                    track_id_source = "chromium_cache"
            if not track_id:
                track_id = load_track_id_from_public_search(title, artist, country_code)
                if track_id:
                    track_id_source = "public_search"

            browser_user_id, favorite_track_ids = load_browser_favorites_snapshot()
            favorite_state = "unknown"
            favorite_state_source = "unavailable"
            if track_id and favorite_track_ids:
                favorite_state = "liked" if track_id in favorite_track_ids else "unliked"
                favorite_state_source = "browser_cache"
            stream_quality, stream_quality_source, api_favorite_state = load_stream_quality_via_relationship_items(
                track_id,
                country_code,
                access_token,
            )
            if access_token:
                favorite_state = (
                    api_favorite_state
                    if api_favorite_state in {"liked", "unliked"}
                    else "unknown"
                )
                favorite_state_source = (
                    "api_relationship_items"
                    if api_favorite_state in {"liked", "unliked"}
                    else stream_quality_source
                )
            track_metadata = load_tidal_track_metadata(track_id, country_code, access_token)
            quality_value = normalize_stream_quality(
                stream_quality,
                track_metadata.get("catalog_quality"),
            )
            playback_metadata = load_playback_context_metadata(
                track_id,
                country_code,
                access_token,
                quality_value,
            )
            actual_quality = normalize_stream_quality(playback_metadata.get("actual_quality"))
            if actual_quality != "none":
                quality_value = actual_quality
                stream_quality_source = "playback_info_actualAudioQuality"
            bit_depth, sample_rate_khz, bitrate_value = load_audio_format_reference(quality_value)
            exact_bit_depth = str(playback_metadata.get("bit_depth") or "").strip()
            exact_sample_rate_khz = str(playback_metadata.get("sample_rate_khz") or "").strip()
            if exact_bit_depth and exact_sample_rate_khz:
                bit_depth = exact_bit_depth
                sample_rate_khz = exact_sample_rate_khz
                bitrate_value = f"{bit_depth}/{sample_rate_khz} kHz"
            audio_format_source = str(
                playback_metadata.get("audio_format_source") or "quality_reference"
            ).strip()
            artwork_url = str(track_metadata.get("artwork_url") or "").strip()
            if not artwork_url and str(mpris_artwork_url or "").strip().startswith(("http://", "https://")):
                artwork_url = str(mpris_artwork_url).strip()

            emit(f"player={selected_player}")
            emit(f"status={status}")
            emit(f"title={title}")
            emit(f"artist={artist}")
            emit(f"album={album}")
            if track_id:
                emit(f"track_id={track_id}")
                emit(f"track_id_source={track_id_source or 'unknown'}")
            emit(f"country_code={country_code}")
            if browser_user_id:
                emit(f"browser_user_id={browser_user_id}")
            if collection_user_id:
                emit(f"collection_user_id={collection_user_id}")
            emit(f"favorite_state={favorite_state}")
            emit(f"favorite_state_source={favorite_state_source}")
            emit(f"stream_quality={quality_value}")
            emit(f"stream_quality_source={stream_quality_source}")
            emit(f"quality={quality_value}")
            emit(f"bitrate={bitrate_value}")
            emit(f"bit_depth={bit_depth}")
            emit(f"sample_rate_khz={sample_rate_khz}")
            emit(f"audio_format_source={audio_format_source}")
            if artwork_url:
                emit(f"artwork_url={artwork_url}")
            if position_seconds:
                emit(f"position_seconds={position_seconds}")
            if duration_microseconds.isdigit():
                emit(f"duration_seconds={int(duration_microseconds) / 1000000:g}")
            for metadata_key in (
                "bpm",
                "key",
                "key_scale",
                "musical_key",
                "lyrics_text_base64",
                "lyrics_subtitles_base64",
                "lyrics_source",
                "track_metadata_source",
            ):
                metadata_value = str(track_metadata.get(metadata_key) or "").strip()
                if metadata_value:
                    emit(f"{metadata_key}={metadata_value}")
            """
        ).replace(
            "__TRACK_METADATA_HELPERS__",
            self._load_track_metadata_helpers_script(),
        ).replace("__PUBLIC_WEB_TOKEN__", TidalApiService._DEFAULT_WEB_TOKEN).strip()
        return (
            "if ! command -v python3 >/dev/null 2>&1; then echo 'error=python3_missing'; exit 1; fi; "
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + f"WEBPLAYER_PLAYER_SELECTOR={player_selector_env} "
            + "python3 - <<'PY'\n"
            + python_script
            + "\nPY"
        )

    def _load_favorite_current_track_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=2,
            minimum=0,
            maximum=120,
        )
        cdp_port = self._load_bounded_int(
            arguments.get("cdp_port"),
            default_value=9222,
            minimum=0,
            maximum=65535,
        )
        cdp_click_timeout_s = self._load_bounded_int(
            arguments.get("cdp_click_timeout_s"),
            default_value=10,
            minimum=1,
            maximum=120,
        )
        player_selector_env = shlex.quote(str(player_selector or "chromium"))
        python_script = dedent(
            r"""
            import asyncio
            import base64
            import binascii
            import json
            import os
            import re
            import shutil
            import socket
            import subprocess
            import tempfile
            import sys
            import time
            from pathlib import Path
            from urllib.error import HTTPError, URLError
            from urllib.parse import urlencode
            from urllib.request import Request, urlopen

            try:
                import websockets
            except ImportError:
                websockets = None

            try:
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
                from cryptography.hazmat.primitives.keywrap import aes_key_unwrap
            except ImportError:
                hashes = None
                Cipher = None
                algorithms = None
                modes = None
                PBKDF2HMAC = None
                aes_key_unwrap = None

            __TRACK_METADATA_HELPERS__

            PUBLIC_WEB_TOKEN = "__PUBLIC_WEB_TOKEN__"
            USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

            def emit(message: str) -> None:
                print(message, flush=True)

            def run_playerctl(selected_player: str, *args: str) -> tuple[int, str, str]:
                completed = subprocess.run(
                    ["playerctl", "-p", selected_player, *args],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return int(completed.returncode), str(completed.stdout or "").strip(), str(completed.stderr or "").strip()

            def pick_player(prefix: str, wait_seconds: int) -> str:
                deadline = time.time() + max(0, int(wait_seconds))
                prefix_casefold = prefix.casefold()
                while True:
                    completed = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, check=False)
                    players = [line.strip() for line in str(completed.stdout or "").splitlines() if line.strip()]
                    selected = next((player for player in players if player.casefold().startswith(prefix_casefold)), "")
                    if not selected and players:
                        selected = players[0]
                    if selected:
                        return selected
                    if time.time() >= deadline:
                        return ""
                    time.sleep(1)

            def normalize(text: str) -> str:
                return re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())

            def extract_track_id_from_url(track_url: str) -> str:
                url_value = str(track_url or "").strip()
                if not url_value:
                    return ""
                match = re.search(r"/track/([0-9]+)", url_value)
                if match:
                    return str(match.group(1) or "").strip()
                match = re.search(r"tidal:track:([0-9]+)", url_value)
                if match:
                    return str(match.group(1) or "").strip()
                return ""

            def extract_track_id_from_mpris_trackid(track_identifier: str) -> str:
                track_identifier_value = str(track_identifier or "").strip()
                if not track_identifier_value:
                    return ""
                match = re.search(r"/track/([0-9]+)", track_identifier_value, re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip()
                match = re.search(r"tidal:track:([0-9]+)", track_identifier_value, re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip()
                match = re.search(r"/TrackList/Track([0-9]+)$", track_identifier_value)
                if match:
                    return str(match.group(1) or "").strip()
                return ""

            def load_player_metadata(selected_player: str) -> tuple[str, str, str, str, str]:
                title_rc, title, title_error = run_playerctl(selected_player, "metadata", "xesam:title")
                artist_rc, artist, _artist_error = run_playerctl(selected_player, "metadata", "xesam:artist")
                album_rc, album, _album_error = run_playerctl(selected_player, "metadata", "xesam:album")
                track_url_rc, track_url, _track_url_error = run_playerctl(selected_player, "metadata", "xesam:url")
                mpris_trackid_rc, mpris_trackid, _mpris_trackid_error = run_playerctl(
                    selected_player,
                    "metadata",
                    "mpris:trackid",
                )
                if title_rc != 0 or not title:
                    emit("error=missing_track_title")
                    if title_error:
                        emit(f"playerctl_title_error={title_error}")
                    raise SystemExit(1)
                if artist_rc != 0:
                    artist = ""
                if album_rc != 0:
                    album = ""
                if track_url_rc != 0:
                    track_url = ""
                if mpris_trackid_rc != 0:
                    mpris_trackid = ""
                return title.strip(), artist.strip(), album.strip(), track_url.strip(), mpris_trackid.strip()

            def _load_token_file_access_token() -> str:
                try:
                    from alde.tidal_oauth import TidalOAuthTokenService
                except Exception:
                    TidalOAuthTokenService = None  # type: ignore[assignment]
                if TidalOAuthTokenService is not None:
                    try:
                        oauth_service = TidalOAuthTokenService(timeout_s=20)
                        oauth_access_token = str(oauth_service.load_access_token() or "").strip()
                        if oauth_access_token:
                            return oauth_access_token
                    except Exception:
                        pass

                token_file = str(
                    os.environ.get("TIDAL_API_TOKEN_FILE")
                    or os.path.expanduser("~/.config/webplayer-mcp/tidal-token.json")
                ).strip()
                if token_file.startswith("%h/"):
                    token_file = str(Path.home() / token_file.removeprefix("%h/"))
                if not token_file:
                    return ""
                token_path = Path(token_file)
                if not token_path.is_file():
                    return ""
                try:
                    token_payload = json.loads(token_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return ""
                if not isinstance(token_payload, dict):
                    return ""
                access_token = str(token_payload.get("access_token") or "").strip()
                if not access_token:
                    return ""
                expires_at = token_payload.get("expires_at")
                if isinstance(expires_at, (int, float)) and float(expires_at) <= time.time() + 60:
                    return ""
                return access_token

            def _load_candidate_profile_roots() -> list[Path]:
                roots = [
                    Path.home() / "snap/chromium/common/chromium",
                    Path.home() / ".config/chromium",
                    Path.home() / ".config/google-chrome",
                ]
                candidates: list[Path] = []
                for root in roots:
                    if not root.exists():
                        continue
                    for profile_dir in root.iterdir():
                        if not profile_dir.is_dir():
                            continue
                        local_storage_dir = profile_dir / "Local Storage" / "leveldb"
                        if not local_storage_dir.is_dir():
                            continue
                        try:
                            has_leveldb_files = any(child.is_file() for child in local_storage_dir.iterdir())
                        except OSError:
                            continue
                        if has_leveldb_files:
                            candidates.append(profile_dir)
                candidates.sort(
                    key=lambda path: ((path / "Local Storage" / "leveldb").stat().st_mtime, path.name),
                    reverse=True,
                )
                return candidates

            def _load_candidate_cache_profile_dirs() -> list[Path]:
                roots = [
                    Path.home() / "snap/chromium/common/chromium",
                    Path.home() / ".config/chromium",
                    Path.home() / ".config/google-chrome",
                ]
                candidates: list[Path] = []
                for root in roots:
                    if not root.exists():
                        continue
                    for profile_dir in root.iterdir():
                        if not profile_dir.is_dir():
                            continue
                        cache_data_dir = profile_dir / "Cache" / "Cache_Data"
                        if not cache_data_dir.is_dir():
                            continue
                        candidates.append(profile_dir)
                candidates.sort(
                    key=lambda path: ((path / "Cache" / "Cache_Data").stat().st_mtime, path.name),
                    reverse=True,
                )
                return candidates

            def load_track_id_from_cache(title: str, artist: str, album: str) -> str:
                title_value = str(title or "").strip()
                if not title_value:
                    return ""
                profile_dirs = _load_candidate_cache_profile_dirs()
                if not profile_dirs:
                    return ""
                title_fragment = json.dumps(title_value, ensure_ascii=False)[1:-1].encode("utf-8")
                artist_value = str(artist or "").strip()
                album_value = str(album or "").strip()
                artist_fragment = (
                    json.dumps(artist_value, ensure_ascii=False)[1:-1].encode("utf-8") if artist_value else b""
                )
                album_fragment = (
                    json.dumps(album_value, ensure_ascii=False)[1:-1].encode("utf-8") if album_value else b""
                )
                pattern = re.compile(rb'"id":([0-9]+),"title":"' + re.escape(title_fragment) + rb'"')
                fallback_track_id = ""

                for profile_dir in profile_dirs:
                    cache_data_dir = profile_dir / "Cache" / "Cache_Data"
                    try:
                        cache_files = sorted(
                            (child for child in cache_data_dir.iterdir() if child.is_file()),
                            key=lambda path: path.stat().st_mtime,
                            reverse=True,
                        )
                    except OSError:
                        continue

                    for cache_file in cache_files:
                        try:
                            cache_bytes = cache_file.read_bytes()
                        except OSError:
                            continue
                        if title_fragment not in cache_bytes:
                            continue
                        for match in pattern.finditer(cache_bytes):
                            candidate_track_id = str(match.group(1).decode("ascii", "ignore")).strip()
                            if not candidate_track_id:
                                continue
                            if not fallback_track_id:
                                fallback_track_id = candidate_track_id
                            context_start = max(0, match.start() - 2048)
                            context_end = min(len(cache_bytes), match.end() + 2048)
                            context = cache_bytes[context_start:context_end]
                            artist_matches = not artist_fragment or artist_fragment in context
                            album_matches = not album_fragment or album_fragment in context
                            if artist_matches and album_matches:
                                return candidate_track_id
                return fallback_track_id

            def _extract_json_object(payload: bytes, start_index: int) -> bytes:
                depth = 0
                in_string = False
                escaped = False
                for index in range(start_index, len(payload)):
                    character = payload[index]
                    if in_string:
                        if escaped:
                            escaped = False
                            continue
                        if character == 92:
                            escaped = True
                            continue
                        if character == 34:
                            in_string = False
                        continue
                    if character == 34:
                        in_string = True
                        continue
                    if character == 123:
                        depth += 1
                        continue
                    if character == 125:
                        depth -= 1
                        if depth == 0:
                            return payload[start_index:index + 1]
                return b""

            def load_browser_favorites_snapshot() -> tuple[str, set[str]]:
                profile_dirs = _load_candidate_cache_profile_dirs()
                for profile_dir in profile_dirs:
                    cache_data_dir = profile_dir / "Cache" / "Cache_Data"
                    try:
                        cache_files = sorted(
                            (child for child in cache_data_dir.iterdir() if child.is_file()),
                            key=lambda path: path.stat().st_mtime,
                            reverse=True,
                        )
                    except OSError:
                        continue
                    for cache_file in cache_files:
                        try:
                            cache_bytes = cache_file.read_bytes()
                        except OSError:
                            continue
                        if b"/favorites/ids?" not in cache_bytes:
                            continue
                        for match in re.finditer(rb"https://tidal\.com/v1/users/([0-9]+)/favorites/ids\?", cache_bytes):
                            browser_user_id = str(match.group(1).decode("ascii", "ignore")).strip()
                            if not browser_user_id:
                                continue
                            json_start = cache_bytes.find(b"{", match.end())
                            if json_start < 0:
                                continue
                            json_payload = _extract_json_object(cache_bytes, json_start)
                            if not json_payload:
                                continue
                            try:
                                parsed_payload = json.loads(json_payload.decode("utf-8", "replace"))
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(parsed_payload, dict):
                                continue
                            track_ids = parsed_payload.get("TRACK")
                            if not isinstance(track_ids, list):
                                continue
                            favorite_track_ids = {
                                str(track_id).strip()
                                for track_id in track_ids
                                if str(track_id).strip()
                            }
                            return browser_user_id, favorite_track_ids
                return "", set()

            async def _run_cdp_favorite_click(cdp_port: int, timeout_s: int) -> str:
                if cdp_port <= 0:
                    return ""
                if websockets is None:
                    emit("error=missing_python_dependency:websockets")
                    return ""
                try:
                    with urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=4) as response:
                        targets = json.loads(response.read().decode("utf-8", "replace"))
                except Exception:
                    return ""
                page_target = next(
                    (
                        target
                        for target in targets
                        if str(target.get("type") or "") == "page"
                        and "tidal.com" in str(target.get("url") or "")
                        and str(target.get("webSocketDebuggerUrl") or "").strip()
                    ),
                    None,
                )
                if not page_target:
                    return ""

                async with websockets.connect(str(page_target["webSocketDebuggerUrl"]), max_size=8_000_000) as websocket:
                    message_id = 0

                    async def send_method(method_name: str, params: dict[str, object] | None = None) -> dict[str, object]:
                        nonlocal message_id
                        message_id += 1
                        payload: dict[str, object] = {"id": message_id, "method": method_name}
                        if params:
                            payload["params"] = params
                        await websocket.send(json.dumps(payload))
                        while True:
                            raw_message = await websocket.recv()
                            message = json.loads(raw_message)
                            if message.get("id") == message_id:
                                return message

                    await send_method("Page.enable")
                    await send_method("Runtime.enable")
                    expression = '''
                        (() => {
                          const selectors = [
                            'button[data-test="footer-favorite-button"]',
                            'button[data-testid="footer-favorite-button"]',
                            '[role="button"][data-test="footer-favorite-button"]',
                            '[role="button"][data-testid="footer-favorite-button"]',
                            'button[data-test="add-to-favorites-button"]',
                            'button[data-testid="add-to-favorites-button"]',
                            '[role="button"][data-test="add-to-favorites-button"]',
                            '[role="button"][data-testid="add-to-favorites-button"]',
                            'button[data-test*="favorite" i]',
                            'button[data-testid*="favorite" i]',
                            'button[data-test*="heart" i]',
                            'button[data-testid*="heart" i]',
                            'button[data-test*="like" i]',
                            'button[data-testid*="like" i]',
                            'button[aria-label*="Favorite" i]',
                            'button[aria-label*="Heart" i]',
                            'button[aria-label*="Like" i]',
                            '[role="button"][aria-label*="Favorite" i]',
                            '[role="button"][aria-label*="Heart" i]',
                            '[role="button"][aria-label*="Like" i]',
                          ];
                          const isVisible = (element) => {
                            if (!element) return false;
                            const style = window.getComputedStyle(element);
                            if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
                            const rect = element.getBoundingClientRect();
                            return rect.width > 4 && rect.height > 4;
                          };
                          const loadState = (element) => {
                            const attributes = {};
                            for (const attribute of element.attributes || []) {
                              attributes[String(attribute.name || '')] = String(attribute.value || '');
                            }
                            const className = String(element.className || '');
                            const textContent = String(element.textContent || '').trim();
                            return { attributes, className, textContent };
                          };
                          const isActive = (state) => {
                            const attrs = state.attributes || {};
                            const pressed = String(attrs['aria-pressed'] || '').toLowerCase();
                            if (pressed === 'true' || pressed === '1') return true;
                            for (const key of ['data-active', 'data-selected', 'data-state', 'aria-checked']) {
                              const value = String(attrs[key] || '').toLowerCase();
                              if (['true', '1', 'on', 'active', 'selected', 'checked'].includes(value)) return true;
                            }
                            const className = String(state.className || '').toLowerCase();
                            if (['active', 'selected', 'checked', 'liked', 'favorited'].some((token) => className.includes(token))) {
                              return true;
                            }
                            const labelText = [
                              attrs['aria-label'],
                              attrs['title'],
                              attrs['data-test'],
                              attrs['data-testid'],
                              state.textContent,
                            ]
                              .map((value) => String(value || '').toLowerCase())
                              .join(' ');
                            return [
                              'remove',
                              'entfernen',
                              'aus meine musik entfernen',
                              'aus meiner musik entfernen',
                              'remove from my collection',
                              'remove from collection',
                              'unfavorite',
                              'unfavourite',
                              'unlike',
                            ].some((token) => labelText.includes(token));
                          };
                          for (const selector of selectors) {
                            const elements = Array.from(document.querySelectorAll(selector));
                            for (const element of elements) {
                              if (!isVisible(element)) continue;
                              const beforeState = loadState(element);
                              const beforeActive = isActive(beforeState);
                              if (!beforeActive) {
                                element.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
                                element.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, view: window }));
                                element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                                element.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                                element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                              }
                              const afterState = loadState(element);
                              const afterActive = isActive(afterState);
                              return { found: true, beforeActive, afterActive, selector };
                            }
                          }
                          return { found: false };
                        })()
                    '''
                    deadline = time.time() + max(1, int(timeout_s))
                    while time.time() < deadline:
                        response = await send_method(
                            "Runtime.evaluate",
                            {
                                "expression": expression,
                                "returnByValue": True,
                                "awaitPromise": True,
                            },
                        )
                        value = (((response.get("result") or {}).get("result") or {}).get("value") or {})
                        if not isinstance(value, dict):
                            await asyncio.sleep(1)
                            continue
                        if not value.get("found"):
                            await asyncio.sleep(1)
                            continue
                        before_active = bool(value.get("beforeActive"))
                        after_active = bool(value.get("afterActive"))
                        if after_active:
                            return "already_present" if before_active else "added"
                        await asyncio.sleep(1)
                return ""

            def _load_chromium_command() -> str:
                for candidate in (
                    "chromium",
                    "chromium-browser",
                    "google-chrome",
                    "google-chrome-stable",
                ):
                    command_path = shutil.which(candidate)
                    if command_path:
                        return command_path
                return ""

            def _load_free_port() -> int:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.bind(("127.0.0.1", 0))
                    return int(sock.getsockname()[1])

            async def _load_browser_auth_payload(debug_port: int, *, strict_errors: bool) -> dict[str, str]:
                if websockets is None:
                    if strict_errors:
                        emit("error=missing_python_dependency:websockets")
                        raise SystemExit(1)
                    return {}
                page_target = None
                deadline = time.time() + 20
                while time.time() < deadline:
                    with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=10) as response:
                        targets = json.loads(response.read().decode("utf-8", "replace"))
                    page_target = next(
                        (
                            target
                            for target in targets
                            if str(target.get("type") or "") == "page"
                            and str(target.get("webSocketDebuggerUrl") or "").strip()
                        ),
                        None,
                    )
                    if page_target:
                        break
                    await asyncio.sleep(1)
                if not page_target:
                    if strict_errors:
                        emit("error=cdp_no_page_target")
                        raise SystemExit(1)
                    return {}

                async with websockets.connect(str(page_target["webSocketDebuggerUrl"]), max_size=8_000_000) as websocket:
                    message_id = 0

                    async def send_method(method_name: str, params: dict[str, object] | None = None) -> dict[str, object]:
                        nonlocal message_id
                        message_id += 1
                        payload: dict[str, object] = {"id": message_id, "method": method_name}
                        if params:
                            payload["params"] = params
                        await websocket.send(json.dumps(payload))
                        while True:
                            raw_message = await websocket.recv()
                            message = json.loads(raw_message)
                            if message.get("id") == message_id:
                                return message

                    await send_method("Page.enable")
                    await send_method("Runtime.enable")
                    await send_method("Page.navigate", {"url": "https://tidal.com/"})
                    expression = (
                        'Object.fromEntries('
                        'Object.keys(localStorage)'
                        '.filter(k => k.startsWith("AuthDB/"))'
                        '.map(k => [k, btoa(localStorage.getItem(k) || "")])'
                        ')'
                    )
                    payload_deadline = time.time() + 25
                    while time.time() < payload_deadline:
                        response = await send_method(
                            "Runtime.evaluate",
                            {
                                "expression": expression,
                                "returnByValue": True,
                            },
                        )
                        auth_payload = (((response.get("result") or {}).get("result") or {}).get("value") or {})
                        if isinstance(auth_payload, dict) and auth_payload:
                            return {str(key): str(value) for key, value in auth_payload.items()}
                        await asyncio.sleep(1)
                    if strict_errors:
                        emit("error=missing_tidal_auth_blob")
                        raise SystemExit(1)
                    return {}

            def _load_tidal_auth_payload(*, strict_errors: bool) -> dict[str, str]:
                candidates = _load_candidate_profile_roots()
                if not candidates:
                    if strict_errors:
                        emit("error=no_tidal_auth_profile")
                        raise SystemExit(1)
                    return {}

                chromium_command = _load_chromium_command()
                if not chromium_command:
                    if strict_errors:
                        emit("error=chromium_missing")
                        raise SystemExit(1)
                    return {}

                for source_profile in candidates:
                    with tempfile.TemporaryDirectory(prefix="webplayer-tidal-auth-") as temp_dir_name:
                        temp_dir = Path(temp_dir_name)
                        temp_profile = temp_dir / source_profile.name
                        temp_profile.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.copytree(
                                source_profile / "Local Storage",
                                temp_profile / "Local Storage",
                                dirs_exist_ok=True,
                            )
                            local_state_path = source_profile.parent / "Local State"
                            if local_state_path.is_file():
                                shutil.copy2(local_state_path, temp_dir / "Local State")
                        except OSError:
                            continue
                        debug_port = _load_free_port()
                        log_path = temp_dir / "chromium.log"
                        log_handle = log_path.open("wb")
                        process = subprocess.Popen(
                            [
                                chromium_command,
                                "--headless=new",
                                "--disable-gpu",
                                "--no-first-run",
                                "--no-default-browser-check",
                                f"--remote-debugging-port={debug_port}",
                                f"--user-data-dir={temp_dir}",
                                f"--profile-directory={source_profile.name}",
                                "https://tidal.com/",
                            ],
                            stdout=log_handle,
                            stderr=subprocess.STDOUT,
                        )
                        try:
                            deadline = time.time() + 30
                            debugger_ready = False
                            while True:
                                try:
                                    with urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=2) as response:
                                        response.read()
                                    debugger_ready = True
                                    break
                                except Exception:
                                    if time.time() >= deadline:
                                        break
                                    time.sleep(1)
                            if not debugger_ready:
                                continue
                            auth_payload = asyncio.run(
                                _load_browser_auth_payload(
                                    debug_port,
                                    strict_errors=strict_errors,
                                )
                            )
                            if auth_payload:
                                return auth_payload
                        except Exception:
                            continue
                        finally:
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            log_handle.close()
                if strict_errors:
                    emit("error=missing_tidal_auth_blob")
                    raise SystemExit(1)
                return {}

            def _decode_auth_blob(storage_key: str, auth_payload: dict[str, str]) -> dict[str, object]:
                prefix = f"AuthDB/{storage_key}"
                try:
                    counter = base64.b64decode(auth_payload[prefix + "Counter"].encode("ascii"), validate=True)
                    encrypted = base64.b64decode(auth_payload[prefix + "Data"].encode("ascii"), validate=True)
                    salt = base64.b64decode(auth_payload[prefix + "Salt"].encode("ascii"), validate=True)
                    wrapped_key = base64.b64decode(auth_payload[prefix + "Key"].encode("ascii"), validate=True)
                except KeyError as exc:
                    raise KeyError(f"missing_auth_blob:{storage_key}") from exc
                except binascii.Error as exc:
                    raise ValueError(f"invalid_auth_blob_base64:{storage_key}") from exc

                key_material = PBKDF2HMAC(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=salt,
                    iterations=100000,
                ).derive(storage_key.encode("utf-8"))
                try:
                    content_key = aes_key_unwrap(key_material, wrapped_key)
                except ValueError as exc:
                    raise ValueError(f"invalid_auth_blob_key:{storage_key}") from exc
                decryptor = Cipher(algorithms.AES(content_key), modes.CTR(counter)).decryptor()
                decrypted = decryptor.update(encrypted) + decryptor.finalize()
                try:
                    parsed = json.loads(decrypted.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid_auth_blob_json:{storage_key}") from exc
                if not isinstance(parsed, dict):
                    raise ValueError(f"invalid_auth_blob:{storage_key}")
                return parsed

            def _merge_tidal_credentials(
                main_credentials: dict[str, object],
                decoded_credentials: dict[str, dict[str, object]],
                fallback_client_credentials: dict[str, object] | None,
            ) -> dict[str, object]:
                client_credentials = fallback_client_credentials
                client_credentials_key = str(main_credentials.get("credentialsStorageKey") or "").strip()
                if client_credentials_key:
                    client_credentials = decoded_credentials.get(client_credentials_key) or client_credentials
                merged_credentials = dict(main_credentials)
                if client_credentials:
                    client_secret = str(client_credentials.get("clientSecret") or "").strip()
                    if client_secret:
                        merged_credentials["clientSecret"] = client_secret
                    client_id = str(client_credentials.get("clientId") or "").strip()
                    if client_id and not str(merged_credentials.get("clientId") or "").strip():
                        merged_credentials["clientId"] = client_id
                return merged_credentials

            def _load_tidal_credentials_candidates(*, strict_errors: bool) -> list[dict[str, object]]:
                try:
                    auth_payload = _load_tidal_auth_payload(strict_errors=strict_errors)
                except SystemExit:
                    if strict_errors:
                        raise
                    return []
                storage_keys = sorted(
                    {
                        key.removeprefix("AuthDB/").removesuffix("Data")
                        for key in auth_payload
                        if key.startswith("AuthDB/") and key.endswith("Data")
                    }
                )
                if not storage_keys:
                    if strict_errors:
                        emit("error=missing_tidal_auth_blob")
                        raise SystemExit(1)
                    return []
                if PBKDF2HMAC is None:
                    if strict_errors:
                        emit("error=missing_python_dependency:cryptography")
                        raise SystemExit(1)
                    return []

                decoded_credentials: dict[str, dict[str, object]] = {}
                client_credentials: dict[str, object] | None = None
                for storage_key in storage_keys:
                    try:
                        credentials = _decode_auth_blob(storage_key, auth_payload)
                    except (KeyError, ValueError):
                        continue
                    decoded_credentials[storage_key] = credentials
                    if client_credentials is None and credentials.get("clientSecret"):
                        client_credentials = credentials

                merged_candidates: list[dict[str, object]] = []
                for storage_key in storage_keys:
                    credentials = decoded_credentials.get(storage_key)
                    if not isinstance(credentials, dict):
                        continue
                    has_refresh_token = bool(str(credentials.get("refreshToken") or "").strip())
                    has_access_token = bool(credentials.get("accessToken"))
                    if not has_refresh_token and not has_access_token:
                        continue
                    merged_candidates.append(
                        _merge_tidal_credentials(
                            credentials,
                            decoded_credentials,
                            client_credentials,
                        )
                    )
                if merged_candidates:
                    return merged_candidates

                if not decoded_credentials:
                    if strict_errors:
                        emit("error=missing_tidal_auth_credentials")
                        raise SystemExit(1)
                    return []

                fallback_main = next(iter(decoded_credentials.values()))
                return [
                    _merge_tidal_credentials(
                        fallback_main,
                        decoded_credentials,
                        client_credentials,
                    )
                ]

            def _load_tidal_credentials() -> dict[str, object]:
                candidates = _load_tidal_credentials_candidates(strict_errors=True)
                if not candidates:
                    emit("error=missing_tidal_auth_credentials")
                    raise SystemExit(1)
                return candidates[0]

            def _load_refreshed_access_token(credentials: dict[str, object], *, strict_errors: bool) -> str:
                def fail(error_code: str, response_body: str = "") -> str:
                    if not strict_errors:
                        return ""
                    emit(f"error={error_code}")
                    if response_body:
                        emit(f"response_body={response_body[:400]}")
                    raise SystemExit(1)

                refresh_token = str(credentials.get("refreshToken") or "").strip()
                if not refresh_token:
                    return fail("missing_tidal_refresh_token")
                client_id = str(credentials.get("clientId") or "").strip()
                if not client_id:
                    return fail("missing_tidal_client_id")
                auth_service_base_uri = str(
                    credentials.get("tidalAuthServiceBaseUri") or "https://auth.tidal.com/v1/"
                ).strip() or "https://auth.tidal.com/v1/"

                scopes_value = credentials.get("scopes")
                if isinstance(scopes_value, str):
                    scope_value = scopes_value.strip()
                elif isinstance(scopes_value, (list, tuple)):
                    scope_value = " ".join(str(scope).strip() for scope in scopes_value if str(scope).strip())
                else:
                    scope_value = ""

                form_body: dict[str, str] = {
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }
                if scope_value:
                    form_body["scope"] = scope_value
                client_secret = str(credentials.get("clientSecret") or "").strip()
                if client_secret:
                    form_body["client_secret"] = client_secret
                request = Request(
                    auth_service_base_uri + "oauth2/token",
                    data=urlencode(form_body).encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=30) as response:
                        refreshed = json.loads(response.read().decode("utf-8", "replace"))
                except HTTPError as exc:
                    response_body = ""
                    if exc.fp:
                        try:
                            response_body = exc.read().decode("utf-8", "replace")
                        except Exception:
                            response_body = ""
                    return fail(f"refresh_token_http_{exc.code or 500}", response_body)
                except URLError as exc:
                    if strict_errors:
                        emit(f"error=url_error:{exc.reason}")
                        raise SystemExit(1)
                    return ""
                except json.JSONDecodeError:
                    return fail("refresh_token_json_error")

                token = str(refreshed.get("access_token") or "").strip()
                if token:
                    return token
                return fail("missing_tidal_access_token")

            def _load_access_token_from_credentials(credentials: dict[str, object], *, strict_errors: bool) -> str:
                access_token = credentials.get("accessToken")
                now_ms = int(time.time() * 1000)
                if isinstance(access_token, dict):
                    token = str(access_token.get("token") or "").strip()
                    expires = int(access_token.get("expires") or 0)
                    if token and expires > now_ms + 60_000:
                        return token
                if isinstance(access_token, str):
                    access_token_value = access_token.strip()
                    if access_token_value:
                        return access_token_value
                return _load_refreshed_access_token(credentials, strict_errors=strict_errors)

            def load_access_token() -> str:
                file_access_token = _load_token_file_access_token()
                if file_access_token:
                    return file_access_token
                credentials = _load_tidal_credentials()
                return _load_access_token_from_credentials(credentials, strict_errors=True)

            def load_access_token_for_browser_user(browser_user_id: str, current_access_token: str) -> str:
                target_user_id = str(browser_user_id or "").strip()
                if not target_user_id:
                    return ""
                tried_tokens: set[str] = set()

                def token_matches_browser_user(token_value: str) -> str:
                    token = str(token_value or "").strip()
                    if not token or token in tried_tokens:
                        return ""
                    tried_tokens.add(token)
                    sessions_status, sessions_payload = fetch_payload(
                        "https://api.tidal.com/v1/sessions",
                        tidal_headers_final(token),
                    )
                    if sessions_status // 100 != 2:
                        return ""
                    try:
                        sessions_data = json.loads(sessions_payload)
                    except json.JSONDecodeError:
                        return ""
                    if not isinstance(sessions_data, dict):
                        return ""
                    resolved_user_id = str(sessions_data.get("userId") or "").strip()
                    if resolved_user_id and resolved_user_id == target_user_id:
                        return token
                    return ""

                matched_token = token_matches_browser_user(current_access_token)
                if matched_token:
                    return matched_token
                file_access_token = _load_token_file_access_token()
                matched_token = token_matches_browser_user(file_access_token)
                if matched_token:
                    return matched_token
                return ""

            def tidal_headers_final(access_token: str) -> dict[str, str]:
                x_tidal_token = os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN", "").strip() or PUBLIC_WEB_TOKEN
                return {
                    "Authorization": "Be" "arer " + access_token,
                    "x-tidal-token": x_tidal_token,
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/",
                    "User-Agent": USER_AGENT,
                }

            def tidal_public_headers() -> dict[str, str]:
                x_tidal_token = os.environ.get("WEBPLAYER_MCP_TIDAL_X_TIDAL_TOKEN", "").strip() or PUBLIC_WEB_TOKEN
                return {
                    "x-tidal-token": x_tidal_token,
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin": "https://listen.tidal.com",
                    "Referer": "https://listen.tidal.com/",
                    "User-Agent": USER_AGENT,
                }

            def fetch_payload(url: str, headers: dict[str, str], *, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
                request = Request(url, data=body, headers=headers, method=method)
                try:
                    with urlopen(request, timeout=20) as response:
                        return int(getattr(response, "status", 200)), str(response.read().decode("utf-8", "replace"))
                except HTTPError as exc:
                    return int(exc.code or 500), str(exc.read().decode("utf-8", "replace") if exc.fp else "")
                except URLError as exc:
                    emit(f"error=url_error:{exc.reason}")
                    raise SystemExit(1)

            def parse_json_or_error(payload: str, fallback_error: str) -> dict[str, object]:
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    emit(f"error={fallback_error}")
                    if payload:
                        emit(f"response_body={payload[:400]}")
                    raise SystemExit(1)
                if not isinstance(parsed, dict):
                    emit(f"error={fallback_error}")
                    raise SystemExit(1)
                return parsed

            def fetch_payload_soft(url: str, headers: dict[str, str], *, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
                request = Request(url, data=body, headers=headers, method=method)
                try:
                    with urlopen(request, timeout=20) as response:
                        return int(getattr(response, "status", 200)), str(response.read().decode("utf-8", "replace"))
                except HTTPError as exc:
                    return int(exc.code or 500), str(exc.read().decode("utf-8", "replace") if exc.fp else "")
                except URLError as exc:
                    return 0, f"url_error:{exc.reason}"
                except Exception as exc:
                    return 0, f"request_failed:{exc}"

            def load_stream_quality_via_relationship_items(
                track_id_value: str,
                country_code_value: str,
                headers_payload: dict[str, str],
            ) -> tuple[str, str]:
                track_id_clean = str(track_id_value or "").strip()
                if not track_id_clean:
                    return "", "track_id_missing"
                relationship_headers = dict(headers_payload)
                relationship_headers["Accept"] = "application/vnd.api+json"
                relationship_headers["Content-Type"] = "application/vnd.api+json"
                relationship_url = (
                    "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items?"
                    + urlencode({"page[size]": 100})
                )
                relationship_status, relationship_payload = fetch_payload_soft(
                    relationship_url,
                    relationship_headers,
                )
                relationship_source = "relationship_not_run"
                if relationship_status // 100 != 2 or not relationship_payload:
                    relationship_source = f"relationship_http_{relationship_status}"
                else:
                    try:
                        relationship_data = json.loads(relationship_payload)
                    except json.JSONDecodeError:
                        relationship_source = "relationship_json_error"
                    else:
                        relationship_items = relationship_data.get("data") if isinstance(relationship_data, dict) else []
                        if not isinstance(relationship_items, list):
                            relationship_source = "relationship_items_missing"
                        else:
                            relationship_track_ids = {
                                str(item.get("id") or "").strip()
                                for item in relationship_items
                                if isinstance(item, dict) and str(item.get("id") or "").strip()
                            }
                            relationship_source = (
                                "relationship_track_match"
                                if track_id_clean in relationship_track_ids
                                else "relationship_track_missing"
                            )
                track_url = (
                    "https://openapi.tidal.com/v2/tracks/"
                    + track_id_clean
                    + "?"
                    + urlencode({"countryCode": country_code_value})
                )
                track_status, track_payload = fetch_payload_soft(track_url, relationship_headers)
                if track_status // 100 != 2 or not track_payload:
                    return "", f"track_http_{track_status}"
                try:
                    track_data = json.loads(track_payload)
                except json.JSONDecodeError:
                    return "", "track_json_error"
                data_object = track_data.get("data") if isinstance(track_data, dict) else {}
                attributes = data_object.get("attributes") if isinstance(data_object, dict) else {}
                if not isinstance(attributes, dict):
                    return "", "track_attributes_missing"
                media_tags = attributes.get("mediaTags")
                if isinstance(media_tags, list):
                    normalized_media_tags = [
                        str(tag).strip()
                        for tag in media_tags
                        if str(tag).strip()
                    ]
                    if normalized_media_tags:
                        quality_value = ",".join(normalized_media_tags)
                        if relationship_source == "relationship_track_match":
                            return quality_value, "openapi_tracks_mediaTags_via_relationship_items"
                        return quality_value, f"openapi_tracks_mediaTags_{relationship_source}"
                audio_quality = str(attributes.get("audioQuality") or "").strip()
                if audio_quality:
                    if relationship_source == "relationship_track_match":
                        return audio_quality, "openapi_tracks_audioQuality_via_relationship_items"
                    return audio_quality, f"openapi_tracks_audioQuality_{relationship_source}"
                return "", f"track_quality_missing_{relationship_source}"

            def update_session_environment(*, session_id: str, token_user_id: str, browser_user_id: str) -> None:
                token_user_value = str(token_user_id or "").strip()
                browser_user_value = str(browser_user_id or "").strip()
                session_value = str(session_id or "").strip()
                if token_user_value:
                    os.environ["WEBPLAYER_TOKEN_USER_ID"] = token_user_value
                if session_value:
                    os.environ["WEBPLAYER_TOKEN_SESSION_ID"] = session_value
                if browser_user_value:
                    os.environ["WEBPLAYER_BROWSER_USER_ID"] = browser_user_value

                env_lines: list[str] = []
                if token_user_value:
                    env_lines.append(f"WEBPLAYER_TOKEN_USER_ID={token_user_value}")
                if session_value:
                    env_lines.append(f"WEBPLAYER_TOKEN_SESSION_ID={session_value}")
                if browser_user_value:
                    env_lines.append(f"WEBPLAYER_BROWSER_USER_ID={browser_user_value}")
                if not env_lines:
                    return

                env_dir = Path.home() / ".config/webplayer-mcp/session-env"
                env_dir.mkdir(parents=True, exist_ok=True)
                session_key = re.sub(r"[^a-zA-Z0-9._-]+", "_", session_value or "unknown")
                session_env_file = env_dir / f"{session_key}.env"
                session_env_payload = "\n".join(env_lines) + "\n"
                session_env_file.write_text(session_env_payload, encoding="utf-8")
                (env_dir / "current.env").write_text(session_env_payload, encoding="utf-8")
                emit(f"token_user_env_file={session_env_file}")

            def extract_artist_name(track: dict[str, object]) -> str:
                artist_value = track.get("artist")
                if isinstance(artist_value, dict):
                    for key in ("name", "fullName"):
                        name = artist_value.get(key)
                        if isinstance(name, str) and name.strip():
                            return name.strip()
                if isinstance(artist_value, str) and artist_value.strip():
                    return artist_value.strip()

                artist_name = track.get("artistName")
                if isinstance(artist_name, str) and artist_name.strip():
                    return artist_name.strip()

                artists = track.get("artists")
                if isinstance(artists, list):
                    for item in artists:
                        if isinstance(item, dict):
                            for key in ("name", "fullName"):
                                name = item.get(key)
                                if isinstance(name, str) and name.strip():
                                    return name.strip()
                return ""

            def extract_track_candidate(payload: dict[str, object], expected_title: str, expected_artist: str) -> dict[str, object]:
                top_hit = payload.get("topHit")
                if isinstance(top_hit, dict):
                    top_hit_type = str(top_hit.get("type") or "").strip().casefold()
                    top_hit_value = top_hit.get("value")
                    if top_hit_type == "track" and isinstance(top_hit_value, dict) and top_hit_value.get("id") is not None:
                        return top_hit_value

                tracks = payload.get("tracks")
                if not isinstance(tracks, list):
                    tracks = []

                expected_title_normalized = normalize(expected_title)
                expected_artist_normalized = normalize(expected_artist)
                fallback_title_match: dict[str, object] | None = None
                fallback_first_track: dict[str, object] | None = None

                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    if fallback_first_track is None:
                        fallback_first_track = track

                    candidate_title = str(track.get("title") or "").strip()
                    candidate_artist = extract_artist_name(track)
                    candidate_title_normalized = normalize(candidate_title)
                    candidate_artist_normalized = normalize(candidate_artist)
                    title_matches = bool(
                        expected_title_normalized
                        and (
                            candidate_title_normalized == expected_title_normalized
                            or expected_title_normalized in candidate_title_normalized
                            or candidate_title_normalized in expected_title_normalized
                        )
                    )
                    artist_matches = bool(
                        not expected_artist_normalized
                        or candidate_artist_normalized == expected_artist_normalized
                        or expected_artist_normalized in candidate_artist_normalized
                        or candidate_artist_normalized in expected_artist_normalized
                    )

                    if title_matches and artist_matches:
                        return track
                    if title_matches and fallback_title_match is None:
                        fallback_title_match = track

                if fallback_title_match is not None:
                    return fallback_title_match
                if fallback_first_track is not None:
                    return fallback_first_track
                if isinstance(top_hit, dict):
                    top_hit_value = top_hit.get("value")
                    if isinstance(top_hit_value, dict):
                        return top_hit_value
                return {}

            selected_player = pick_player(os.environ.get("WEBPLAYER_PLAYER_SELECTOR", "chromium"), int(os.environ.get("WEBPLAYER_WAIT_FOR_PLAYER_S", "2")))
            if not selected_player:
                emit("error=no_player")
                raise SystemExit(1)

            title, artist, album, track_url, mpris_trackid = load_player_metadata(selected_player)
            query = " ".join(part for part in (title, artist, album) if part).strip() or title

            access_token = load_access_token()
            headers = tidal_headers_final(access_token)

            sessions_status, sessions_payload = fetch_payload("https://api.tidal.com/v1/sessions", headers)
            if sessions_status // 100 != 2:
                emit(f"error=sessions_http_{sessions_status}")
                if sessions_payload:
                    emit(f"response_body={sessions_payload[:400]}")
                raise SystemExit(1)
            sessions_data = parse_json_or_error(sessions_payload, "sessions_json_error")
            session_id = str(sessions_data.get("sessionId") or "").strip()
            user_id = sessions_data.get("userId")
            if user_id is None or str(user_id).strip() == "":
                emit("error=missing_user_id")
                raise SystemExit(1)
            country_code = str(sessions_data.get("countryCode") or "DE").strip().upper() or "DE"
            browser_user_id, browser_favorite_track_ids = load_browser_favorites_snapshot()
            if browser_user_id and str(user_id).strip() != browser_user_id:
                aligned_access_token = load_access_token_for_browser_user(browser_user_id, access_token)
                if aligned_access_token and aligned_access_token != access_token:
                    access_token = aligned_access_token
                    headers = tidal_headers_final(access_token)
                    sessions_status, sessions_payload = fetch_payload("https://api.tidal.com/v1/sessions", headers)
                    if sessions_status // 100 != 2:
                        emit(f"error=sessions_http_{sessions_status}")
                        if sessions_payload:
                            emit(f"response_body={sessions_payload[:400]}")
                        raise SystemExit(1)
                    sessions_data = parse_json_or_error(sessions_payload, "sessions_json_error")
                    session_id = str(sessions_data.get("sessionId") or "").strip()
                    user_id = sessions_data.get("userId")
                    if user_id is None or str(user_id).strip() == "":
                        emit("error=missing_user_id")
                        raise SystemExit(1)
                    country_code = str(sessions_data.get("countryCode") or "DE").strip().upper() or "DE"
                    emit("token_user_alignment=browser_user_match")
            update_session_environment(
                session_id=session_id,
                token_user_id=str(user_id),
                browser_user_id=browser_user_id,
            )
            token_browser_user_mismatch = False
            if browser_user_id and str(user_id).strip() != browser_user_id:
                cdp_favorite_result = asyncio.run(
                    _run_cdp_favorite_click(
                        int(os.environ.get("WEBPLAYER_CDP_PORT", "0")),
                        int(os.environ.get("WEBPLAYER_CDP_CLICK_TIMEOUT_S", "10")),
                    )
                )
                if cdp_favorite_result:
                    cdp_track_id_source = ""
                    cdp_track_id = extract_track_id_from_url(track_url)
                    if cdp_track_id:
                        cdp_track_id_source = "xesam_url"
                    if not cdp_track_id:
                        cdp_track_id = extract_track_id_from_mpris_trackid(mpris_trackid)
                        if cdp_track_id:
                            cdp_track_id_source = "mpris_trackid"
                    if not cdp_track_id:
                        cdp_track_id = load_track_id_from_cache(title, artist, album)
                        if cdp_track_id:
                            cdp_track_id_source = "chromium_cache"
                    cdp_stream_quality = ""
                    cdp_stream_quality_source = "not_run"
                    if cdp_track_id:
                        cdp_stream_quality, cdp_stream_quality_source = load_stream_quality_via_relationship_items(
                            cdp_track_id,
                            country_code,
                            headers,
                        )
                    cdp_quality_value = normalize_stream_quality(cdp_stream_quality)
                    cdp_bit_depth, cdp_sample_rate_khz, cdp_bitrate_value = load_audio_format_reference(
                        cdp_quality_value
                    )
                    emit(f"player={selected_player}")
                    emit("status=Favorited")
                    emit(f"title={title}")
                    emit(f"artist={artist}")
                    emit(f"album={album}")
                    emit(f"user_id={user_id}")
                    if session_id:
                        emit(f"token_session_id={session_id}")
                    emit(f"browser_user_id={browser_user_id}")
                    if cdp_track_id:
                        emit(f"track_id={cdp_track_id}")
                        emit(f"track_id_source={cdp_track_id_source or 'unknown'}")
                    if cdp_stream_quality:
                        emit(f"stream_quality={cdp_quality_value}")
                    emit(f"stream_quality_source={cdp_stream_quality_source}")
                    emit(f"quality={cdp_quality_value}")
                    emit(f"bitrate={cdp_bitrate_value}")
                    emit(f"bit_depth={cdp_bit_depth}")
                    emit(f"sample_rate_khz={cdp_sample_rate_khz}")
                    emit(f"favorite_result={cdp_favorite_result}")
                    emit("favorite_state=liked")
                    emit("favorite_backend=cdp")
                    raise SystemExit(0)
                token_browser_user_mismatch = True
                emit("warning=token_browser_user_mismatch")
                emit("account_alignment_required=true")
                emit(f"token_user_id={user_id}")
                if session_id:
                    emit(f"token_session_id={session_id}")
                emit(f"browser_user_id={browser_user_id}")

            track_id_source = ""
            track_id = extract_track_id_from_url(track_url)
            if track_id:
                track_id_source = "xesam_url"
            if not track_id:
                track_id = extract_track_id_from_mpris_trackid(mpris_trackid)
                if track_id:
                    track_id_source = "mpris_trackid"
            if not track_id:
                track_id = load_track_id_from_cache(title, artist, album)
                if track_id:
                    track_id_source = "chromium_cache"
            track = {}
            if not track_id:
                search_url = "https://api.tidal.com/v1/search?" + urlencode(
                    {"query": query, "limit": 10, "offset": 0, "types": "TRACKS", "countryCode": country_code}
                )
                search_status, search_payload = fetch_payload(search_url, headers)
                if search_status // 100 == 2:
                    search_data = parse_json_or_error(search_payload, "search_json_error")
                    track = extract_track_candidate(search_data, title, artist)
                    track_id = str(track.get("id") or "").strip()
                    if track_id:
                        track_id_source = "search"
                else:
                    fallback_search_url = "https://listen.tidal.com/v1/search?" + urlencode(
                        {"query": query, "limit": 10, "offset": 0, "types": "TRACKS", "countryCode": country_code}
                    )
                    fallback_status, fallback_payload = fetch_payload(fallback_search_url, tidal_public_headers())
                    if fallback_status // 100 == 2:
                        fallback_data = parse_json_or_error(fallback_payload, "search_json_error")
                        track = extract_track_candidate(fallback_data, title, artist)
                        track_id = str(track.get("id") or "").strip()
                        if track_id:
                            track_id_source = "listen_search_public"
                    if not track_id:
                        emit(f"error=search_http_{search_status}")
                        if search_payload:
                            emit(f"response_body={search_payload[:400]}")
                        emit(f"search_fallback_http_{fallback_status}")
                        if fallback_payload:
                            emit(f"search_fallback_body={fallback_payload[:400]}")
                        raise SystemExit(1)
            if not track_id:
                emit("error=track_not_found")
                emit(f"title={title}")
                emit(f"artist={artist}")
                emit(f"query={query}")
                raise SystemExit(1)
            if not token_browser_user_mismatch and track_id in browser_favorite_track_ids:
                stream_quality, stream_quality_source = load_stream_quality_via_relationship_items(
                    track_id,
                    country_code,
                    headers,
                )
                quality_value = normalize_stream_quality(stream_quality)
                bit_depth, sample_rate_khz, bitrate_value = load_audio_format_reference(quality_value)
                emit(f"player={selected_player}")
                emit("status=Favorited")
                emit(f"title={title}")
                emit(f"artist={artist}")
                emit(f"album={album}")
                emit(f"user_id={user_id}")
                if session_id:
                    emit(f"token_session_id={session_id}")
                if browser_user_id:
                    emit(f"browser_user_id={browser_user_id}")
                emit(f"track_id={track_id}")
                emit(f"track_id_source={track_id_source or 'unknown'}")
                if stream_quality:
                    emit(f"stream_quality={quality_value}")
                emit(f"stream_quality_source={stream_quality_source}")
                emit(f"quality={quality_value}")
                emit(f"bitrate={bitrate_value}")
                emit(f"bit_depth={bit_depth}")
                emit(f"sample_rate_khz={sample_rate_khz}")
                emit("favorite_result=already_present")
                emit("favorite_state=liked")
                raise SystemExit(0)

            favorite_url = "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items"
            favorite_headers = dict(headers)
            favorite_headers["Accept"] = "application/vnd.api+json"
            favorite_headers["Content-Type"] = "application/vnd.api+json"
            favorite_status, favorite_payload = fetch_payload(
                favorite_url,
                favorite_headers,
                method="POST",
                body=json.dumps({"data": [{"id": track_id, "type": "tracks"}]}).encode("utf-8"),
            )
            if favorite_status // 100 != 2 and favorite_status != 409:
                emit(f"error=favorite_http_{favorite_status}")
                if favorite_payload:
                    emit(f"response_body={favorite_payload[:400]}")
                raise SystemExit(1)
            favorite_result = "already_present" if favorite_status == 409 else "added"
            if favorite_payload:
                favorite_data = parse_json_or_error(favorite_payload, "favorite_json_error")
                favorite_meta = favorite_data.get("meta")
                if isinstance(favorite_meta, dict):
                    skipped = favorite_meta.get("skipped")
                    if isinstance(skipped, list):
                        for skipped_item in skipped:
                            if not isinstance(skipped_item, dict):
                                continue
                            skipped_track_id = str(skipped_item.get("id") or "").strip()
                            if skipped_track_id and skipped_track_id != track_id:
                                continue
                            skipped_reason = str(skipped_item.get("reason") or "").strip().casefold()
                            if skipped_reason == "already_present":
                                favorite_result = "already_present"
                            elif skipped_reason:
                                favorite_result = f"skipped_{skipped_reason}"
                            else:
                                favorite_result = "skipped"
                            break

            favorite_verified = favorite_result == "already_present"
            favorite_verify_source = "openapi_write_conflict" if favorite_verified else "unverified"
            favorite_verify_openapi_status = "not_run"
            favorite_verify_ids_status = "not_run"
            verify_url = (
                "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items?"
                + urlencode({"page[size]": 100})
            )
            verify_status, verify_payload = fetch_payload(verify_url, favorite_headers)
            favorite_verify_openapi_status = str(verify_status)
            if verify_status // 100 == 2 and verify_payload:
                try:
                    verify_data = json.loads(verify_payload)
                except json.JSONDecodeError:
                    verify_data = {}
                verify_items = verify_data.get("data") if isinstance(verify_data, dict) else []
                if isinstance(verify_items, list):
                    verified_track_ids = {
                        str(item.get("id") or "").strip()
                        for item in verify_items
                        if isinstance(item, dict) and str(item.get("id") or "").strip()
                    }
                    if track_id in verified_track_ids:
                        favorite_verified = True
                        favorite_verify_source = "openapi_userCollectionTracks_me"
            if not favorite_verified:
                verify_ids_url = (
                    "https://api.tidal.com/v1/users/"
                    + str(user_id).strip()
                    + "/favorites/ids?"
                    + urlencode({"countryCode": country_code, "limit": 10000, "offset": 0})
                )
                verify_ids_status, verify_ids_payload = fetch_payload(verify_ids_url, headers)
                favorite_verify_ids_status = str(verify_ids_status)
                if verify_ids_status // 100 == 2 and verify_ids_payload:
                    try:
                        verify_ids_data = json.loads(verify_ids_payload)
                    except json.JSONDecodeError:
                        verify_ids_data = {}
                    verify_tracks = verify_ids_data.get("TRACKS") if isinstance(verify_ids_data, dict) else []
                    if isinstance(verify_tracks, list):
                        verified_track_ids = {
                            str(track_value).strip()
                            for track_value in verify_tracks
                            if str(track_value).strip()
                        }
                        if track_id in verified_track_ids:
                            favorite_verified = True
                            favorite_verify_source = "v1_favorites_ids"

            stream_quality, stream_quality_source = load_stream_quality_via_relationship_items(
                track_id,
                country_code,
                headers,
            )
            quality_value = normalize_stream_quality(stream_quality)
            bit_depth, sample_rate_khz, bitrate_value = load_audio_format_reference(quality_value)

            emit(f"player={selected_player}")
            emit("status=Favorited")
            emit(f"title={title}")
            emit(f"artist={artist}")
            emit(f"album={album}")
            emit(f"user_id={user_id}")
            emit(f"favorite_target_collection_user_id={user_id}")
            if session_id:
                emit(f"token_session_id={session_id}")
            if browser_user_id:
                emit(f"browser_user_id={browser_user_id}")
            emit(f"track_id={track_id}")
            emit(f"track_id_source={track_id_source or 'unknown'}")
            if stream_quality:
                emit(f"stream_quality={quality_value}")
            emit(f"stream_quality_source={stream_quality_source}")
            emit(f"quality={quality_value}")
            emit(f"bitrate={bitrate_value}")
            emit(f"bit_depth={bit_depth}")
            emit(f"sample_rate_khz={sample_rate_khz}")
            emit(f"favorite_verified={'true' if favorite_verified else 'false'}")
            emit(f"favorite_verify_source={favorite_verify_source}")
            emit(f"favorite_verify_openapi_status={favorite_verify_openapi_status}")
            emit(f"favorite_verify_ids_status={favorite_verify_ids_status}")
            emit(f"account_alignment_required={'true' if token_browser_user_mismatch else 'false'}")
            if token_browser_user_mismatch:
                emit(f"favorite_result={favorite_result}")
                emit("favorite_state=liked")
                emit("favorite_backend=api_token_user")
            else:
                emit(f"favorite_result={favorite_result}")
                emit("favorite_state=liked")
                emit("favorite_backend=api")
            """
        ).replace(
            "__TRACK_METADATA_HELPERS__",
            self._load_track_metadata_helpers_script(),
        ).replace("__PUBLIC_WEB_TOKEN__", TidalApiService._DEFAULT_WEB_TOKEN).strip()
        return (
            "if ! command -v python3 >/dev/null 2>&1; then echo 'error=python3_missing'; exit 1; fi; "
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            + f"WEBPLAYER_PLAYER_SELECTOR={player_selector_env} "
            + f"WEBPLAYER_WAIT_FOR_PLAYER_S={wait_for_player_s} "
            + f"WEBPLAYER_CDP_PORT={cdp_port} "
            + f"WEBPLAYER_CDP_CLICK_TIMEOUT_S={cdp_click_timeout_s} "
            + "python3 - <<'PY'\n"
            + python_script
            + "\nPY"
        )

    def _load_volume_adjust_command(self, *, player_selector: str, arguments: dict[str, Any]) -> str:
        delta_percent = self._load_signed_bounded_int(
            arguments.get("delta_percent"),
            default_value=5,
            minimum=-100,
            maximum=100,
        )
        volume_target = str(arguments.get("volume_target") or "browser").strip().casefold() or "browser"
        if volume_target not in {"browser", "system"}:
            return "echo 'error=invalid_volume_target'; exit 1;"
        wait_for_player_s = self._load_bounded_int(
            arguments.get("wait_for_player_s"),
            default_value=0,
            minimum=0,
            maximum=120,
        )
        if delta_percent == 0:
            return "echo 'error=missing_delta_percent'; exit 1;"

        system_volume_command = (
            "if [ \"$delta_percent\" -gt 0 ]; then adjustment_spec=\"+${delta_percent}%\"; else adjustment_spec=\"${delta_percent}%\"; fi; "
            "pactl set-sink-volume @DEFAULT_SINK@ \"$adjustment_spec\" 2>/dev/null || { echo 'error=volume_adjust_failed'; exit 2; }; "
            "current_volume=$(LC_ALL=C pactl get-sink-volume @DEFAULT_SINK@ 2>/dev/null | awk 'NR==1 { for (i=1; i<=NF; i++) if ($i ~ /%$/) { print $i; exit } }' || true); "
            "echo player=$selected_player; "
            "echo volume_target=system; "
            "echo current_volume=$current_volume; "
            "echo delta_percent=$delta_percent; "
            "echo volume_adjustment=$adjustment_spec; "
        )

        browser_volume_command = (
            "sink_input_id=$(LC_ALL=C pactl list sink-inputs | awk '/^Sink Input #/ { current=$3; sub(/^#/, \"\", current); if (first == \"\") first = current; matched = 0; next } /application\\.name = \"Chromium\"/ || /pipewire\\.snap\\.id = \"chromium\"/ || /application\\.process\\.binary = \"chrome\"/ { matched = 1 } /^[[:space:]]*$/ { if (matched && current != \"\") { print current; exit } } END { if (matched && current != \"\") { print current; exit } if (first != \"\") print first }'); "
            "if [ -n \"$sink_input_id\" ]; then "
            "if [ \"$delta_percent\" -gt 0 ]; then adjustment_spec=\"+${delta_percent}%\"; else adjustment_spec=\"${delta_percent}%\"; fi; "
            "pactl set-sink-input-volume \"$sink_input_id\" \"$adjustment_spec\" 2>/dev/null || { echo 'error=volume_adjust_failed'; exit 2; }; "
            "echo player=$selected_player; "
            "echo volume_target=browser; "
            "echo sink_input_id=$sink_input_id; "
            "echo delta_percent=$delta_percent; "
            "echo volume_adjustment=$adjustment_spec; "
            "else "
            "if ! command -v playerctl >/dev/null 2>&1; then echo 'error=playerctl_missing'; exit 1; fi; "
            "current_volume=$(LC_ALL=C playerctl -p \"$selected_player\" volume 2>/dev/null | tr ',' '.' || echo 0); "
            "target_volume=$(LC_ALL=C awk -v current=\"$current_volume\" -v delta=\"$delta_percent\" 'BEGIN { target = current + (delta / 100.0); if (target < 0) target = 0; if (target > 1) target = 1; printf \"%.3f\", target }'); "
            "playerctl -p \"$selected_player\" volume \"$target_volume\" 2>/dev/null || { echo 'error=volume_adjust_failed'; exit 2; }; "
            "echo player=$selected_player; "
            "echo volume_target=browser; "
            "echo current_volume=$current_volume; "
            "echo target_volume=$target_volume; "
            "echo delta_percent=$delta_percent; "
            "fi; "
        )

        return (
            "if ! command -v pactl >/dev/null 2>&1; then echo 'error=pactl_missing'; exit 1; fi; "
            + self._load_player_pick_script(player_selector=player_selector, wait_seconds=wait_for_player_s)
            + f"delta_percent={int(delta_percent)}; "
            + f"volume_target={shlex.quote(volume_target)}; "
            + "if [ \"$volume_target\" = \"system\" ]; then "
            + system_volume_command
            + "else "
            + browser_volume_command
            + "fi"
        )

    def _load_browser_open_command(
        self,
        *,
        target_url: str,
        log_file_path: str,
        allow_xdg_open_fallback: bool,
        cdp_port: int = 9222,
    ) -> str:
        quoted_url = shlex.quote(target_url)
        quoted_log_path = shlex.quote(log_file_path)
        if allow_xdg_open_fallback:
            browser_discovery = (
                "if command -v chromium >/dev/null 2>&1; then BROWSER_CMD=chromium; "
                "elif command -v chromium-browser >/dev/null 2>&1; then BROWSER_CMD=chromium-browser; "
                "elif command -v google-chrome >/dev/null 2>&1; then BROWSER_CMD=google-chrome; "
                "elif command -v google-chrome-stable >/dev/null 2>&1; then BROWSER_CMD=google-chrome-stable; "
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
                "elif command -v google-chrome >/dev/null 2>&1; then BROWSER_CMD=google-chrome; "
                "elif command -v google-chrome-stable >/dev/null 2>&1; then BROWSER_CMD=google-chrome-stable; "
                "else echo 'error=no_browser'; exit 1; fi; "
            )

        return (
            browser_discovery
            + "UID_NUM=$(id -u); export XDG_RUNTIME_DIR=\"/run/user/$UID_NUM\"; "
            "if [ -S \"$XDG_RUNTIME_DIR/wayland-0\" ]; then export WAYLAND_DISPLAY=wayland-0; "
            "elif [ -S \"$XDG_RUNTIME_DIR/wayland-1\" ]; then export WAYLAND_DISPLAY=wayland-1; fi; "
            "nohup \"$BROWSER_CMD\" --ozone-platform=wayland --enable-features=UseOzonePlatform "
            "--no-first-run --no-default-browser-check --remote-debugging-port="
            + str(int(cdp_port))
            + " --remote-allow-origins=* "
            "--user-data-dir=/tmp/webplayer-mcp-browser --new-window "
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
  const selectors = action === 'favorite' ? [
    'button[data-test="footer-favorite-button"]',
    'button[data-testid="footer-favorite-button"]',
    '[role="button"][data-test="footer-favorite-button"]',
    '[role="button"][data-testid="footer-favorite-button"]',
    'button[data-test="add-to-favorites-button"]',
    'button[data-testid="add-to-favorites-button"]',
    '[role="button"][data-test="add-to-favorites-button"]',
    '[role="button"][data-testid="add-to-favorites-button"]',
    'button[data-test*="favorite" i]',
    'button[data-testid*="favorite" i]',
    'button[data-test*="heart" i]',
    'button[data-testid*="heart" i]',
    'button[data-test*="like" i]',
    'button[data-testid*="like" i]',
    'button[aria-label*="Favorite" i]',
    'button[aria-label*="Heart" i]',
    'button[aria-label*="Like" i]',
    'button[aria-label*="Save" i]',
    'button[title*="Favorite" i]',
    'button[title*="Heart" i]',
    'button[title*="Like" i]',
    'button[title*="Save" i]',
    '[role="button"][data-test*="favorite" i]',
    '[role="button"][data-testid*="favorite" i]',
    '[role="button"][aria-label*="Favorite" i]',
    '[role="button"][aria-label*="Heart" i]',
    '[role="button"][aria-label*="Like" i]',
  ] : [
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

    def _load_cdp_navigate_command(self, *, target_url: str, cdp_port: int) -> str:
        env_prefix = (
            "CDP_NAVIGATE_URL="
            + shlex.quote(str(target_url or ""))
            + f" CDP_PORT={int(cdp_port)} "
        )
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

target_url = str(os.environ.get('CDP_NAVIGATE_URL') or '').strip()
cdp_port = int(str(os.environ.get('CDP_PORT') or '9222'))

async def run() -> int:
    if not target_url:
        print('error=cdp_navigate_url_missing')
        return 11
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
    selected_target = tidal_targets[0] if tidal_targets else (page_targets[0] if page_targets else None)
    if selected_target is None:
        print('error=cdp_no_page_target')
        return 12

    async with websockets.connect(
        str(selected_target.get('webSocketDebuggerUrl') or ''),
        max_size=8_000_000,
    ) as websocket:
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
            message = json.loads(await websocket.recv())
            if message.get('id') != 1:
                continue
            result = message.get('result') or {}
            error_text = str(result.get('errorText') or '').strip()
            if error_text:
                print('error=cdp_navigate_failed:' + error_text)
                return 13
            print('cdp_navigated=true')
            print('opened_url=' + target_url)
            return 0

try:
    raise SystemExit(asyncio.run(run()))
except Exception as exc:
    print('error=cdp_navigate_exception:' + str(exc))
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
        cdp_navigate_command = self._load_cdp_navigate_command(
            target_url=target_url,
            cdp_port=cdp_port,
        )
        open_or_navigate_command = (
            cdp_navigate_command
            + " cdp_navigate_exit_code=$?; "
            + "if [ \"$cdp_navigate_exit_code\" -ne 0 ]; then "
            + open_command
            + " fi; "
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
                open_or_navigate_command
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
            open_or_navigate_command
            + f" sleep {wait_for_page_s}; "
            + "if ! command -v playerctl >/dev/null 2>&1; then "
            + cdp_click_command
            + " cdp_exit_code=$?; if [ \"$cdp_exit_code\" -ne 0 ]; then echo error=cdp_click_failed; exit 3; fi; "
            + "echo player=browser_cdp; "
            + "echo status=Playing; "
            + "echo playback_backend=browser_cdp_no_mpris; "
            + "echo warning=playerctl_missing; "
            + "exit 0; fi; "
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

    def _load_signed_bounded_int(self, raw_value: Any, *, default_value: int, minimum: int, maximum: int) -> int:
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
    _UI_HTTP_PATHS = ("/ui/webplayer/mini-controls", "/ui/webplayer/mini-controls.html")
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
        "webplayer_favorite_current_track",
        "webplayer_volume_adjust",
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

    def load_ui_http_paths(self) -> tuple[str, ...]:
        return self._UI_HTTP_PATHS

    def load_ui_http_path(self) -> str:
        return self._UI_HTTP_PATHS[0]

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
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>WebPlayer Mini Controls</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0f16;
      --bg-2: #111725;
      --panel: #131b28;
      --line: #283142;
      --line-2: #3a475c;
      --text: #edf2f7;
      --muted: #9aa8ba;
      --accent: #7dd3fc;
      --accent-2: #8b5cf6;
      --good: #86efac;
      --warn: #fbbf24;
      --bad: #fca5a5;
    }

    * { box-sizing: border-box; }
    html,
    body {
      margin: 0;
      width: 100%;
      height: 100%;
      background: transparent;
      color: var(--text);
      font: 13px/1.35 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    body {
      padding: 8px;
    }

    .app {
      min-width: 320px;
      max-width: 100%;
      display: grid;
      gap: 8px;
    }

    .panel {
      position: relative;
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 16px;
      background-color: rgba(0, 0, 0, 0.9);
      background-image:
        url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='63' height='26' viewBox='0 0 63 26'%3E%3Ctext x='0' y='19' fill='%237dd3fc' fill-opacity='.10' font-family='monospace' font-size='15' letter-spacing='0'%3E%3A%5B%5C%7C%2F%5D%3A%3C/text%3E%3C/svg%3E"),
        linear-gradient(180deg, rgba(0, 0, 0, 0.82), rgba(5, 6, 10, 0.8));
      background-position: 0 0, 0 0;
      background-repeat: repeat, no-repeat;
      background-size: 63px 26px, 100% 100%;
      box-shadow: 0 22px 60px rgba(0, 0, 0, 0.78);
      padding: 10px;
      display: grid;
      gap: 10px;
    }

    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .icon-button {
      width: 44px;
      height: 44px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--text);
      display: inline-grid;
      place-items: center;
      cursor: pointer;
      transition: transform 120ms ease, color 120ms ease, opacity 120ms ease;
      box-shadow: none;
      position: relative;
    }

    .icon-button:hover {
      transform: translateY(-1px);
      background: transparent;
      color: var(--accent);
    }

    .icon-button:active {
      transform: translateY(0);
    }

    .icon-button:disabled {
      cursor: not-allowed;
      opacity: 0.45;
      transform: none;
    }

    .icon-button.primary {
      background: transparent;
      color: #d5f3ff;
    }

    .icon-button.danger {
      background: transparent;
      color: #ffd4dd;
    }

    .icon-button.danger.is-active {
      background: transparent;
      color: #ff6f91;
      box-shadow: none;
    }

    .icon-button svg {
      width: 20px;
      height: 20px;
      fill: currentColor;
      display: block;
    }

    .favorite-icon path {
      fill: transparent;
      stroke: currentColor;
      stroke-linejoin: round;
      stroke-width: 1.8;
    }

    .icon-button.danger.is-active .favorite-icon path {
      fill: currentColor;
    }

    .volume-mode {
      margin-left: auto;
      display: inline-flex;
      border: 0;
      border-radius: 0;
      background: transparent;
      backdrop-filter: none;
      overflow: hidden;
    }

    .volume-mode-button {
      border: 0;
      background: transparent;
      color: #aab6c6;
      font-size: 11px;
      padding: 6px 10px;
      cursor: pointer;
      line-height: 1;
      transition: background-color 140ms ease, color 140ms ease, box-shadow 140ms ease;
    }

    .volume-mode-button.is-active {
      color: var(--accent);
      background: transparent;
      box-shadow: none;
    }

    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    .now-playing {
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 14px;
      background: linear-gradient(180deg, rgba(0, 0, 0, 0.38), rgba(0, 0, 0, 0.24));
      padding: 9px 10px;
      overflow: hidden;
    }

    .player-summary {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .album-artwork {
      width: 80px;
      height: 80px;
      flex: 0 0 80px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 10px;
      object-fit: cover;
      background: rgba(255, 255, 255, 0.03);
      box-shadow: 0 8px 18px rgba(0, 0, 0, 0.5);
    }

    .album-artwork[hidden] {
      display: none;
    }

    .player-details {
      min-width: 0;
      flex: 1 1 auto;
      display: grid;
      gap: 8px;
    }

    .marquee {
      overflow: hidden;
      white-space: nowrap;
      position: relative;
      mask-image: linear-gradient(90deg, transparent, #000 8%, #000 92%, transparent);
    }

    .marquee-track {
      display: flex;
      width: max-content;
      animation: marquee 16s linear infinite;
      will-change: transform;
    }

    .marquee-track.is-static {
      animation: none;
    }

    .marquee-track span {
      padding-right: 2rem;
      font-size: 14px;
      font-weight: 650;
      letter-spacing: 0.01em;
    }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .pill {
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid rgba(255, 255, 255, 0.06);
      background: transparent;
      color: #c2ccd8;
      font-size: 12px;
      white-space: nowrap;
    }

    .action-feedback {
      position: absolute;
      z-index: 10;
      left: 50%;
      bottom: 12px;
      max-width: calc(100% - 24px);
      padding: 7px 11px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 999px;
      background: rgba(8, 12, 18, 0.94);
      color: var(--text);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transform: translateX(-50%);
      pointer-events: none;
    }

    .action-feedback.loading {
      color: var(--warn);
    }

    .action-feedback.success {
      color: var(--good);
    }

    .action-feedback.error {
      color: var(--bad);
    }

    @keyframes marquee {
      from { transform: translateX(0); }
      to { transform: translateX(-50%); }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="panel" data-ansi-background=":[\\|/]:">
      <div class="controls" role="toolbar" aria-label="WebPlayer controls">
      <button id="favoriteButton" class="icon-button danger" data-tool="webplayer_favorite_current_track" data-args='{"wait_for_player_s":2}' aria-label="Favorite" aria-pressed="false" title="Favorite">
        <svg class="favorite-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M12 21.35 10.55 20.03C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.53z"></path>
        </svg>
        <span class="sr-only">Favorite</span>
      </button>
      <button class="icon-button" data-tool="webplayer_backward" aria-label="Previous track" title="Previous track">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M6 6h2v12H6zM18 6 8.5 12 18 18z"></path>
        </svg>
        <span class="sr-only">Previous track</span>
      </button>
      <button class="icon-button primary" id="playToggleButton" data-tool="webplayer_play" data-args='{"playback_backend":"browser","player_selector":"chromium","cdp_port":9222,"cdp_autoclick":true}' aria-label="Play" title="Play">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path id="playToggleIcon" d="M8 5v14l11-7z"></path>
        </svg>
        <span id="playToggleLabel" class="sr-only">Play</span>
      </button>
      <button class="icon-button" data-tool="webplayer_forward" aria-label="Next track" title="Next track">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M16 6h2v12h-2zM6 6l9.5 6L6 18z"></path>
        </svg>
          <span class="sr-only">Next track</span>
        </button>
        <button class="icon-button" data-tool="webplayer_volume_adjust" data-volume-button="true" data-args='{"delta_percent":-5}' aria-label="Volume down" title="Volume down">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M3 10v4h4l5 4V6L7 10H3zm11 2h6v2h-6z"></path>
          </svg>
          <span class="sr-only">Volume down</span>
        </button>
        <button class="icon-button" data-tool="webplayer_volume_adjust" data-volume-button="true" data-args='{"delta_percent":5}' aria-label="Volume up" title="Volume up">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M3 10v4h4l5 4V6L7 10H3zm11 2h6v2h-6zm2-3h2v2h2v2h-2v2h-2v-2h-2v-2h2z"></path>
          </svg>
          <span class="sr-only">Volume up</span>
        </button>
        <div class="volume-mode" role="group" aria-label="Volume target">
          <button id="volumeModeBrowser" class="volume-mode-button is-active" type="button" data-volume-target="browser">Browser</button>
          <button id="volumeModeSystem" class="volume-mode-button" type="button" data-volume-target="system">System</button>
        </div>
      </div>

      <div class="now-playing" aria-live="polite">
        <div class="player-summary">
          <img id="albumArtwork" class="album-artwork" alt="" hidden />
          <div class="player-details">
            <div class="marquee" aria-label="Now playing and synchronized lyrics">
              <div id="marqueeTrack" class="marquee-track">
                <span id="marqueeText">Loading current track…</span>
                <span id="marqueeTextClone" aria-hidden="true">Loading current track…</span>
              </div>
            </div>
            <div class="meta">
              <span id="metaStatus" class="pill">Status: …</span>
              <span id="metaPlayer" class="pill">Player: …</span>
              <span id="metaTitle" class="pill">Title: …</span>
              <span id="metaArtist" class="pill">Artist: …</span>
              <span id="metaAlbum" class="pill">Album: …</span>
              <span id="metaQuality" class="pill">Quality: none</span>
              <span id="metaBitrate" class="pill">Bit/Hz: none</span>
              <span id="metaBpm" class="pill" hidden>BPM: none</span>
              <span id="metaKey" class="pill" hidden>Key: none</span>
            </div>
          </div>
        </div>
      </div>

      <div id="actionFeedback" class="action-feedback" role="status" aria-live="polite" aria-atomic="true" hidden></div>
    </div>
  </div>

  <script>
    const defaultArgs = { player_selector: "chromium", cdp_port: 9222 };
    const actionFeedbackEl = document.getElementById("actionFeedback");
    const metaStatusEl = document.getElementById("metaStatus");
    const metaPlayerEl = document.getElementById("metaPlayer");
    const metaTitleEl = document.getElementById("metaTitle");
    const metaArtistEl = document.getElementById("metaArtist");
    const metaAlbumEl = document.getElementById("metaAlbum");
    const metaQualityEl = document.getElementById("metaQuality");
    const metaBitrateEl = document.getElementById("metaBitrate");
    const metaBpmEl = document.getElementById("metaBpm");
    const metaKeyEl = document.getElementById("metaKey");
    const albumArtworkEl = document.getElementById("albumArtwork");
    const marqueeTrackEl = document.getElementById("marqueeTrack");
    const marqueeTextEl = document.getElementById("marqueeText");
    const marqueeTextCloneEl = document.getElementById("marqueeTextClone");
    const favoriteButtonEl = document.getElementById("favoriteButton");
    const playToggleButtonEl = document.getElementById("playToggleButton");
    const playToggleIconEl = document.getElementById("playToggleIcon");
    const playToggleLabelEl = document.getElementById("playToggleLabel");
    const volumeModeButtons = Array.from(document.querySelectorAll(".volume-mode-button"));
    const volumeButtons = Array.from(document.querySelectorAll('[data-volume-button="true"]'));
    let volumeTargetMode = "browser";
    let currentTrackText = "Nothing playing";
    let currentPlaybackStatus = "unknown";
    let currentPositionSeconds = 0;
    let currentDurationSeconds = 0;
    let positionObservedAt = Date.now();
    let synchronizedLyrics = [];
    let plainLyrics = [];
    let nowPlayingRefreshPending = false;
    let actionFeedbackHideTimer = null;

    function parseJsonMaybe(value) {
      if (typeof value !== "string") return value;
      const text = value.trim();
      if (!text) return "";
      try {
        return JSON.parse(text);
      } catch {
        return value;
      }
    }

    function parseKeyValueLines(text) {
      const payload = {};
      for (const line of String(text || "").split(/\\r?\\n/)) {
        const equalsIndex = line.indexOf("=");
        if (equalsIndex <= 0) continue;
        const key = line.slice(0, equalsIndex).trim();
        const value = line.slice(equalsIndex + 1).trim();
        if (key) payload[key] = value;
      }
      return payload;
    }

    function normalizeToolResponse(response) {
      if (!response || typeof response !== "object") return response;
      if (response.result && typeof response.result === "object" && !("content" in response) && !("structuredContent" in response)) {
        return normalizeToolResponse(response.result);
      }
      if (response.structuredContent && typeof response.structuredContent === "object") {
        return response.structuredContent;
      }
      if (Array.isArray(response.content)) {
        const textBlock = response.content.find((item) => item && item.type === "text" && typeof item.text === "string");
        if (textBlock) return parseJsonMaybe(textBlock.text);
      }
      if (typeof response.content === "string") {
        return parseJsonMaybe(response.content);
      }
      return response;
    }

    function loadNowPlayingPayload(response) {
      const payload = normalizeToolResponse(response);
      if (payload && typeof payload === "object") {
        if (payload.now_playing && typeof payload.now_playing === "object") return payload.now_playing;
        return payload;
      }
      return {};
    }

    function buildTrackText(payload) {
      const nowPlaying = loadNowPlayingPayload(payload);
      const root = payload && typeof payload === "object" ? payload : {};
      const status = String(nowPlaying.status || root.status || "").trim();
      const title = String(nowPlaying.title || root.title || "").trim();
      const artist = String(nowPlaying.artist || root.artist || "").trim();
      const album = String(nowPlaying.album || root.album || "").trim();
      const parts = [status, title, artist, album].filter(Boolean);
      return parts.length ? parts.join(" • ") : "Nothing playing";
    }

    function setMarqueeText(text) {
      const value = String(text || "").trim() || "Nothing playing";
      if (marqueeTextEl.textContent === value) return;
      marqueeTextEl.textContent = value;
      marqueeTextCloneEl.textContent = value;
      marqueeTrackEl.classList.toggle("is-static", value.length < 24);
    }

    function decodeBase64Utf8(value) {
      const encoded = String(value || "").trim();
      if (!encoded) return "";
      try {
        const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
        return new TextDecoder().decode(bytes);
      } catch {
        return "";
      }
    }

    function parseSynchronizedLyrics(value) {
      const entries = [];
      for (const line of String(value || "").split(/\\r?\\n/)) {
        const timestampPattern = /\\[(\\d{1,2}):(\\d{2})(?:[.:](\\d{1,3}))?\\]/g;
        const lyricText = line.replace(timestampPattern, "").trim();
        if (!lyricText) continue;
        let timestampMatch;
        timestampPattern.lastIndex = 0;
        while ((timestampMatch = timestampPattern.exec(line)) !== null) {
          const fraction = String(timestampMatch[3] || "");
          const fractionSeconds = fraction
            ? Number(fraction) / (fraction.length === 3 ? 1000 : (fraction.length === 2 ? 100 : 10))
            : 0;
          entries.push({
            time: Number(timestampMatch[1]) * 60 + Number(timestampMatch[2]) + fractionSeconds,
            text: lyricText,
          });
        }
      }
      return entries.sort((left, right) => left.time - right.time);
    }

    function loadCurrentPositionSeconds() {
      const elapsedSeconds = currentPlaybackStatus.toLowerCase() === "playing"
        ? Math.max(0, (Date.now() - positionObservedAt) / 1000)
        : 0;
      return currentPositionSeconds + elapsedSeconds;
    }

    function updateLyricsMarquee() {
      const position = loadCurrentPositionSeconds();
      let lyricText = "";
      for (const entry of synchronizedLyrics) {
        if (entry.time > position) break;
        lyricText = entry.text;
      }
      if (!lyricText && plainLyrics.length) {
        const plainIndex = currentDurationSeconds > 0
          ? Math.min(
              plainLyrics.length - 1,
              Math.floor((position / currentDurationSeconds) * plainLyrics.length),
            )
          : Math.floor(position / 5) % plainLyrics.length;
        lyricText = plainLyrics[Math.max(0, plainIndex)] || "";
      }
      setMarqueeText(lyricText ? `♫ ${lyricText}` : currentTrackText);
    }

    function setOptionalPill(element, label, value) {
      if (!element) return;
      const normalized = String(value || "").trim();
      element.hidden = !normalized || normalized.toLowerCase() === "none";
      if (!element.hidden) element.textContent = `${label}: ${normalized}`;
    }

    function normalizeQuality(value) {
      const aliases = {
        HIRES_LOSSLESS: "HI_RES_LOSSLESS",
        HI_RES_LOSSLESS: "HI_RES_LOSSLESS",
        HIRES: "HI_RES_LOSSLESS",
        HI_RES: "HI_RES_LOSSLESS",
        MASTER: "HI_RES_LOSSLESS",
        LOSSLESS: "LOSSLESS",
        HIGH: "HIGH",
        LOW: "LOW",
      };
      const qualityValues = String(value || "")
        .split(",")
        .map((item) => aliases[item.trim().toUpperCase()])
        .filter(Boolean);
      return ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"].find(
        (quality) => qualityValues.includes(quality),
      ) || "none";
    }

    function updateAlbumArtwork(artworkUrl, title, album) {
      if (!albumArtworkEl) return;
      const normalizedUrl = String(artworkUrl || "").trim();
      if (!normalizedUrl) {
        albumArtworkEl.hidden = true;
        albumArtworkEl.removeAttribute("src");
        albumArtworkEl.alt = "";
        return;
      }
      albumArtworkEl.onerror = () => {
        albumArtworkEl.hidden = true;
        albumArtworkEl.removeAttribute("src");
      };
      albumArtworkEl.alt = album ? `Album cover for ${album}` : `Artwork for ${title || "current track"}`;
      albumArtworkEl.src = normalizedUrl;
      albumArtworkEl.hidden = false;
    }

    function setStatus(message, kind = "normal", timeoutMs = null) {
      if (!actionFeedbackEl) return;
      if (actionFeedbackHideTimer !== null) {
        window.clearTimeout(actionFeedbackHideTimer);
        actionFeedbackHideTimer = null;
      }

      const normalizedMessage = String(message || "").trim();
      if (!normalizedMessage) {
        actionFeedbackEl.hidden = true;
        actionFeedbackEl.textContent = "";
        return;
      }

      const supportedKinds = new Set(["normal", "loading", "success", "error"]);
      const normalizedKind = supportedKinds.has(kind) ? kind : "normal";
      actionFeedbackEl.textContent = normalizedMessage;
      actionFeedbackEl.className = `action-feedback ${normalizedKind}`;
      actionFeedbackEl.hidden = false;

      const defaultTimeoutMs = normalizedKind === "loading" ? 0 : (normalizedKind === "error" ? 6000 : 3200);
      const hideAfterMs = timeoutMs === null ? defaultTimeoutMs : Math.max(0, Number(timeoutMs) || 0);
      if (hideAfterMs > 0) {
        actionFeedbackHideTimer = window.setTimeout(() => {
          actionFeedbackEl.hidden = true;
          actionFeedbackEl.textContent = "";
          actionFeedbackHideTimer = null;
        }, hideAfterMs);
      }
    }

    function setFavoriteButtonState(state) {
      if (!favoriteButtonEl) return;
      const normalized = String(state || "").trim().toLowerCase();
      const isActive = normalized === "liked" || normalized === "already_present";
      favoriteButtonEl.classList.toggle("is-active", isActive);
      favoriteButtonEl.setAttribute("aria-pressed", isActive ? "true" : "false");
      favoriteButtonEl.setAttribute("title", isActive ? "Favorited" : "Favorite");
      favoriteButtonEl.setAttribute("aria-label", isActive ? "Favorited" : "Favorite");
    }

    function updateFavoriteButtonTarget(trackId, countryCode, title) {
      if (!favoriteButtonEl) return;
      const normalizedTrackId = String(trackId || "").trim();
      if (normalizedTrackId) {
        favoriteButtonEl.dataset.args = JSON.stringify({
          playback_backend: "api_only",
          track_id: normalizedTrackId,
          country_code: String(countryCode || "DE").trim().toUpperCase() || "DE",
        });
        favoriteButtonEl.disabled = false;
        return;
      }
      favoriteButtonEl.dataset.args = JSON.stringify({ wait_for_player_s: 2 });
      favoriteButtonEl.disabled = !String(title || "").trim();
    }

    function updatePlayToggleButton(status) {
      if (!playToggleButtonEl || !playToggleIconEl || !playToggleLabelEl) return;
      const isPlaying = String(status || "").trim().toLowerCase() === "playing";
      const toolName = isPlaying ? "webplayer_stop" : "webplayer_play";
      const buttonLabel = isPlaying ? "Stop" : "Play";
      const buttonArgs = isPlaying
        ? { player_selector: "chromium" }
        : {
            playback_backend: "browser",
            player_selector: "chromium",
            cdp_port: 9222,
            cdp_autoclick: true,
          };
      playToggleButtonEl.dataset.tool = toolName;
      playToggleButtonEl.dataset.args = JSON.stringify(buttonArgs);
      playToggleButtonEl.setAttribute("aria-label", buttonLabel);
      playToggleButtonEl.setAttribute("title", buttonLabel);
      playToggleIconEl.setAttribute("d", isPlaying ? "M7 7h10v10H7z" : "M8 5v14l11-7z");
      playToggleLabelEl.textContent = buttonLabel;
    }

    function updateVolumeButtonsTarget() {
      for (const button of volumeButtons) {
        const args = JSON.parse(button.dataset.args || "{}");
        args.volume_target = volumeTargetMode;
        button.dataset.args = JSON.stringify(args);
      }
      for (const button of volumeModeButtons) {
        const buttonTarget = String(button.dataset.volumeTarget || "").trim().toLowerCase();
        button.classList.toggle("is-active", buttonTarget === volumeTargetMode);
      }
    }

    function updateNowPlaying(payload) {
      const nowPlaying = loadNowPlayingPayload(payload);
      const root = payload && typeof payload === "object" ? payload : {};
      const status = String(nowPlaying.status || "Unknown").trim();
      const player = String(nowPlaying.player || root.player || "—").trim();
      const title = String(nowPlaying.title || root.title || "").trim();
      const artist = String(nowPlaying.artist || root.artist || "").trim();
      const album = String(nowPlaying.album || root.album || "").trim();
      const trackId = String(nowPlaying.track_id || root.track_id || "").trim();
      const countryCode = String(nowPlaying.country_code || root.country_code || "DE").trim();
      const quality = normalizeQuality(
        nowPlaying.quality || root.quality || nowPlaying.stream_quality || root.stream_quality,
      );
      const bitDepth = String(nowPlaying.bit_depth || root.bit_depth || "").trim();
      const sampleRateKhz = String(nowPlaying.sample_rate_khz || root.sample_rate_khz || "").trim();
      const hasBitDepthSampleRate = bitDepth
        && sampleRateKhz
        && bitDepth.toLowerCase() !== "none"
        && sampleRateKhz.toLowerCase() !== "none";
      const bitrate = hasBitDepthSampleRate
        ? `${bitDepth}/${sampleRateKhz} kHz`
        : String(nowPlaying.bitrate || root.bitrate || "none").trim() || "none";
      const bpm = String(nowPlaying.bpm || root.bpm || "").trim();
      const musicalKey = String(
        nowPlaying.musical_key || root.musical_key || nowPlaying.key || root.key || "",
      ).trim();
      const artworkUrl = String(nowPlaying.artwork_url || root.artwork_url || "").trim();
      const lyricsText = decodeBase64Utf8(
        nowPlaying.lyrics_text_base64 || root.lyrics_text_base64,
      );
      const lyricsSubtitles = decodeBase64Utf8(
        nowPlaying.lyrics_subtitles_base64 || root.lyrics_subtitles_base64,
      );
      const parsedPosition = Number(nowPlaying.position_seconds || root.position_seconds || 0);
      const parsedDuration = Number(nowPlaying.duration_seconds || root.duration_seconds || 0);
      const trackText = buildTrackText(nowPlaying);
      const favoriteState = String(nowPlaying.favorite_state || root.favorite_state || "").trim();

      currentTrackText = trackText;
      currentPlaybackStatus = status;
      currentPositionSeconds = Number.isFinite(parsedPosition) ? Math.max(0, parsedPosition) : 0;
      currentDurationSeconds = Number.isFinite(parsedDuration) ? Math.max(0, parsedDuration) : 0;
      positionObservedAt = Date.now();
      synchronizedLyrics = parseSynchronizedLyrics(lyricsSubtitles);
      plainLyrics = lyricsText.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);

      updatePlayToggleButton(status);
      updateFavoriteButtonTarget(trackId, countryCode, title);
      setFavoriteButtonState(favoriteState || "unliked");
      updateLyricsMarquee();
      updateAlbumArtwork(artworkUrl, title, album);
      metaStatusEl.textContent = `Status: ${status || "Unknown"}`;
      metaPlayerEl.textContent = `Player: ${player || "—"}`;
      metaTitleEl.textContent = `Title: ${title || "—"}`;
      metaArtistEl.textContent = `Artist: ${artist || "—"}`;
      metaAlbumEl.textContent = `Album: ${album || "—"}`;
      if (metaQualityEl) metaQualityEl.textContent = `Quality: ${quality}`;
      if (metaBitrateEl) metaBitrateEl.textContent = `Bit/Hz: ${bitrate}`;
      setOptionalPill(metaBpmEl, "BPM", bpm);
      setOptionalPill(metaKeyEl, "Key", musicalKey);

      if (root && root.ok === false) {
        setStatus(`Now playing unavailable: ${root.error || "request failed"}`, "error");
      }
    }

    function summarizeAction(toolName, payload) {
      const normalized = loadNowPlayingPayload(payload);
      const root = payload && typeof payload === "object" ? payload : {};
      const stdout = parseKeyValueLines(String(root.stdout || ""));
      const status = String(normalized.status || stdout.status || root.status || "").trim();
      const title = String(normalized.title || stdout.title || root.title || "").trim();
      const artist = String(normalized.artist || stdout.artist || root.artist || "").trim();
      const volumeAdjustment = String(stdout.volume_adjustment || root.volume_adjustment || "").trim();
      const currentVolume = String(stdout.current_volume || root.current_volume || "").trim();
      const targetVolume = String(stdout.target_volume || root.target_volume || "").trim();
      const volumeTarget = String(stdout.volume_target || root.volume_target || "").trim().toLowerCase();
      const favoriteResult = String(stdout.favorite_result || root.favorite_result || "").trim().toLowerCase();
      const actionError = loadActionError(payload);

      if (actionError) {
        return toolName === "webplayer_favorite_current_track"
          ? `Favorite failed: ${actionError}`
          : `Error: ${actionError}`;
      }
      if (toolName === "webplayer_favorite_current_track") {
        if (favoriteResult.startsWith("token_user_")) {
          const tokenUserId = String(stdout.token_user_id || root.token_user_id || "").trim();
          const browserUserId = String(stdout.browser_user_id || root.browser_user_id || "").trim();
          if (tokenUserId && browserUserId) {
            return `Saved with API account ${tokenUserId}; browser account is ${browserUserId} (reauth needed)`;
          }
          return "Saved with token account (browser/token user mismatch; reauth needed)";
        }
        if (favoriteResult === "already_present") {
          return title ? `Already favorited ${title}${artist ? ` — ${artist}` : ""}` : "Already favorited";
        }
        return title ? `Favorited ${title}${artist ? ` — ${artist}` : ""}` : "Favorited current track";
      }
      if (toolName === "webplayer_volume_adjust") {
        const targetLabel = volumeTarget === "system" ? "System" : "Browser";
        if (volumeAdjustment) {
          return `${targetLabel} volume ${volumeAdjustment}`;
        }
        return targetVolume ? `${targetLabel} volume ${currentVolume || "?"} → ${targetVolume}` : `${targetLabel} volume adjusted`;
      }
      if (toolName === "webplayer_now_playing") {
        return buildTrackText(normalized);
      }
      if (title || artist) {
        return [status || "Done", title && artist ? `${title} — ${artist}` : (title || artist)].filter(Boolean).join(" • ");
      }
      return status || "Done";
    }

    function loadActionError(payload) {
      const root = payload && typeof payload === "object" ? payload : {};
      const stdout = parseKeyValueLines(String(root.stdout || ""));
      const explicitError = String(stdout.error || root.error || "").trim();
      if (explicitError) return explicitError;
      if (root.ok === false) {
        return String(root.stderr || root.stdout || "request failed").trim();
      }
      return "";
    }

    async function callTool(name, args) {
      const argumentsValue = { ...defaultArgs, ...(args || {}) };
      if (window.mcp && typeof window.mcp.callTool === "function") {
        return normalizeToolResponse(await window.mcp.callTool({ name, arguments: argumentsValue }));
      }
      const response = await fetch("/mcp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: Date.now(),
          method: "tools/call",
          params: { name, arguments: argumentsValue },
        }),
      });
      if (!response.ok) throw new Error(`MCP HTTP ${response.status}`);
      return normalizeToolResponse(await response.json());
    }

    async function refreshNowPlaying() {
      if (nowPlayingRefreshPending) return;
      nowPlayingRefreshPending = true;
      try {
        const payload = await callTool("webplayer_now_playing", {});
        updateNowPlaying(payload);
      } catch (error) {
        setStatus(`Now playing unavailable: ${String(error)}`, "error");
      } finally {
        nowPlayingRefreshPending = false;
      }
    }

    for (const modeButton of volumeModeButtons) {
      modeButton.addEventListener("click", () => {
        const modeValue = String(modeButton.dataset.volumeTarget || "").trim().toLowerCase();
        volumeTargetMode = modeValue === "system" ? "system" : "browser";
        updateVolumeButtonsTarget();
        setStatus(`Volume target: ${volumeTargetMode === "system" ? "System" : "Browser"}`);
      });
    }

    updateVolumeButtonsTarget();

    for (const button of document.querySelectorAll("[data-tool]")) {
      button.addEventListener("click", async () => {
        const toolName = button.dataset.tool;
        button.disabled = true;
        if (toolName === "webplayer_favorite_current_track") {
          setStatus("Adding to favorites…", "loading", 0);
        }
        try {
          const args = JSON.parse(button.dataset.args || "{}");
          const payload = await callTool(toolName, args);
          const actionError = loadActionError(payload);
          if (toolName === "webplayer_favorite_current_track") {
            const root = payload && typeof payload === "object" ? payload : {};
            const stdout = parseKeyValueLines(String(root.stdout || ""));
            const favoriteState = String(stdout.favorite_state || root.favorite_state || "").trim().toLowerCase();
            const favoriteResult = String(stdout.favorite_result || root.favorite_result || "").trim().toLowerCase();
            if (favoriteState) {
              setFavoriteButtonState(favoriteState);
            } else if (favoriteResult === "added" || favoriteResult === "already_present") {
              setFavoriteButtonState("liked");
            }
          }
          setStatus(summarizeAction(toolName, payload), actionError ? "error" : "success");
          if (toolName === "webplayer_now_playing") {
            updateNowPlaying(payload);
          } else {
            await refreshNowPlaying();
          }
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          setStatus(
            toolName === "webplayer_favorite_current_track"
              ? `Favorite failed: ${errorMessage}`
              : errorMessage,
            "error",
          );
        } finally {
          button.disabled = false;
        }
      });
    }

    refreshNowPlaying();
    window.setInterval(refreshNowPlaying, 15000);
    window.setInterval(updateLyricsMarquee, 500);
  </script>
</body>
</html>"""

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
                    "_meta": {
                        "ui": {
                            "csp": {
                                "resourceDomains": ["https://resources.tidal.com"],
                            }
                        }
                    },
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
                "4) Use webplayer_favorite_current_track to heart/save the current track when the user asks to like or favorite it.\n"
                "5) Use webplayer_volume_adjust for volume changes.\n"
                "6) Stop playback with webplayer_stop when requested.\n"
                "7) Skip next with webplayer_forward when requested.\n"
                "8) Skip previous with webplayer_backward when requested.\n"
                f"{search_line}\n"
                "9) Use tidal_api_request, tidal_api_track, tidal_api_track_manifest, or tidal_api_widevine when the user asks for direct API inspection or manifest data.\n"
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
            },
            "playback_backend": {
                "type": "string",
                "description": "Execution backend: browser playback control (default) or strict API-only mode.",
                "enum": ["browser", "api_only", "api"],
                "default": "browser",
            },
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
                    "name": "webplayer_favorite_current_track",
                    "description": "Heart or save the current TIDAL track via the logged-in browser session.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player before favoriting.",
                                "default": 2,
                            },
                            "track_id": {
                                "type": "string",
                                "description": "Explicit TIDAL track id. Required when playback_backend is api_only.",
                            },
                            "country_code": {
                                "type": "string",
                                "description": "Country code used for API session context in api_only mode.",
                                "default": "DE",
                            },
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "webplayer_volume_adjust",
                    "description": "Adjust the current player's volume by a percentage delta.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            **dict(base_properties),
                            "delta_percent": {
                                "type": "integer",
                                "description": "Signed percentage delta to apply to the current volume (for example, -5 or 5).",
                                "default": 5,
                            },
                            "volume_target": {
                                "type": "string",
                                "description": "Volume target to adjust: browser player volume or system output volume.",
                                "enum": ["browser", "system"],
                                "default": "browser",
                            },
                            "wait_for_player_s": {
                                "type": "integer",
                                "description": "Seconds to wait for a visible MPRIS player before adjusting volume.",
                                "default": 0,
                            },
                        },
                        "required": [],
                    },
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
                            "country_code": {
                                "type": "string",
                                "description": "Country code for API search context when playback_backend is api_only.",
                                "default": "DE",
                            },
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

    @staticmethod
    def _origin_matches_allowlist(origin: str, allowed_origins: set[str]) -> bool:
        if origin in allowed_origins:
            return True
        try:
            parsed_origin = urlparse(origin)
            _ = parsed_origin.port
        except ValueError:
            return False
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.hostname
            or parsed_origin.username
            or parsed_origin.password
            or parsed_origin.path
            or parsed_origin.params
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            return False
        for allowed_origin in allowed_origins:
            try:
                parsed_allowed = urlparse(allowed_origin)
                allowed_port = parsed_allowed.port
            except ValueError:
                continue
            if (
                parsed_allowed.scheme != parsed_origin.scheme
                or parsed_allowed.hostname != parsed_origin.hostname
                or allowed_port is not None
            ):
                continue
            if not parsed_allowed.username and not parsed_allowed.password and not parsed_allowed.path:
                return True
        return False

    def _cors_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip()
        if origin and self._origin_matches_allowlist(origin, self._allowed_origins()):
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

    def _write_html_response(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", MCP_REQUEST_SERVICE._UI_RESOURCE_MIME_TYPE)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        cors_origin = self._cors_origin()
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
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
        request_path = urlparse(self.path).path.rstrip("/")
        if request_path == "/health":
            self._write_json_response({"ok": True, "server": "webplayer-mcp-http"}, status=200)
            return
        if request_path == "":
            self._write_json_response(
                {
                    "ok": True,
                    "server": "webplayer-mcp-http",
                    "mcpEndpoint": "/mcp",
                    "healthEndpoint": "/health",
                    "uiEndpoint": MCP_REQUEST_SERVICE.load_ui_http_path(),
                    "uiEndpointAliases": list(MCP_REQUEST_SERVICE.load_ui_http_paths()[1:]),
                    "uiResourceUri": MCP_REQUEST_SERVICE._UI_RESOURCE_URI,
                },
                status=200,
            )
            return
        if request_path in MCP_REQUEST_SERVICE.load_ui_http_paths():
            self._write_html_response(MCP_REQUEST_SERVICE._load_ui_resource_markup(), status=200)
            return
        self._write_json_response({"error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        request_path = urlparse(self.path).path.rstrip("/")
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
