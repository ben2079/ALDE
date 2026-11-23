from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


class TidalOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class TidalAuthorizationRequest:
    authorization_url: str
    state: str
    code_verifier: str


class TidalOAuthTokenService:
    AUTHORIZATION_URL = "https://login.tidal.com/authorize"
    TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
    _EXPIRY_SKEW_SECONDS = 60

    def __init__(
        self,
        *,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        scope: str | None = None,
        token_file: str | Path | None = None,
        timeout_s: int = 20,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.client_id = str(client_id or os.getenv("TIDAL_API_CLIENT_ID") or "").strip()
        self.redirect_uri = str(redirect_uri or os.getenv("TIDAL_API_REDIRECT_URI") or "").strip()
        self.scope = str(scope or os.getenv("TIDAL_API_SCOPE") or "collection.write w_usr").strip()
        self.token_file = self._resolve_token_file(token_file)
        self.timeout_s = max(1, min(120, int(timeout_s)))
        self._opener = opener or urlopen
        self._lock = RLock()

    def load_authorization_header(self) -> str:
        access_token = self.load_access_token()
        return f"Bearer {access_token}" if access_token else ""

    def load_access_token(self) -> str:
        static_authorization = self._load_static_authorization()
        if static_authorization:
            return static_authorization

        with self._lock:
            token_payload = self.load_token_payload()
            access_token = self._clean_token(token_payload.get("access_token"))
            if access_token and self._is_token_current(token_payload):
                return access_token

            refresh_token = self._clean_token(token_payload.get("refresh_token"))
            if not refresh_token:
                return ""
            refreshed_payload = self._refresh_access_token_locked(refresh_token, previous=token_payload)
            return self._clean_token(refreshed_payload.get("access_token"))

    def build_authorization_request(
        self,
        *,
        state: str | None = None,
        code_verifier: str | None = None,
    ) -> TidalAuthorizationRequest:
        self._require_client_configuration(require_scope=True)
        verifier = str(code_verifier or self._generate_code_verifier()).strip()
        if not verifier:
            raise TidalOAuthError("missing_code_verifier")
        challenge = self._build_code_challenge(verifier)
        request_state = str(state or secrets.token_urlsafe(32)).strip()
        if not request_state:
            raise TidalOAuthError("missing_state")

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": request_state,
            }
        )
        parsed_url = urlparse(self.AUTHORIZATION_URL)
        authorization_url = urlunparse(parsed_url._replace(query=query))
        return TidalAuthorizationRequest(
            authorization_url=authorization_url,
            state=request_state,
            code_verifier=verifier,
        )

    def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        state: str | None = None,
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        normalized_code = str(code or "").strip()
        normalized_verifier = str(code_verifier or "").strip()
        if not normalized_code:
            raise TidalOAuthError("missing_authorization_code")
        if not normalized_verifier:
            raise TidalOAuthError("missing_code_verifier")
        if expected_state is not None and str(state or "") != str(expected_state):
            raise TidalOAuthError("oauth_state_mismatch")

        self._require_client_configuration(require_scope=False)
        return self._request_token(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "code": normalized_code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": normalized_verifier,
            }
        )

    def refresh_access_token(self) -> dict[str, Any]:
        with self._lock:
            token_payload = self.load_token_payload()
            refresh_token = self._clean_token(token_payload.get("refresh_token"))
            if not refresh_token:
                raise TidalOAuthError("missing_refresh_token")
            return self._refresh_access_token_locked(refresh_token, previous=token_payload)

    def load_token_payload(self) -> dict[str, Any]:
        try:
            with self.token_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def store_token_payload(self, payload: dict[str, Any], *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TidalOAuthError("invalid_token_payload")

        previous_payload = dict(previous or {})
        access_token = self._clean_token(payload.get("access_token"))
        if not access_token:
            raise TidalOAuthError("token_response_missing_access_token")

        token_payload = dict(payload)
        token_payload["access_token"] = access_token
        token_payload["token_type"] = str(payload.get("token_type") or "Bearer").strip() or "Bearer"
        refresh_token = self._clean_token(payload.get("refresh_token")) or self._clean_token(
            previous_payload.get("refresh_token")
        )
        if refresh_token:
            token_payload["refresh_token"] = refresh_token

        try:
            expires_in = float(payload.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = 0.0
        if expires_in > 0:
            token_payload["expires_at"] = time.time() + expires_in
        elif payload.get("expires_at"):
            token_payload["expires_at"] = float(payload["expires_at"])

        self._write_token_payload(token_payload)
        return token_payload

    def _refresh_access_token_locked(self, refresh_token: str, *, previous: dict[str, Any]) -> dict[str, Any]:
        return self._request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
            previous=previous,
        )

    def _request_token(self, form_payload: dict[str, str], *, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            self.TOKEN_URL,
            data=urlencode(form_payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                raw_body = response.read()
        except HTTPError as exc:
            raise TidalOAuthError(f"token_http_{int(exc.code or 500)}") from exc
        except URLError as exc:
            raise TidalOAuthError(f"token_url_error:{exc.reason}") from exc
        except Exception as exc:
            raise TidalOAuthError(f"token_request_failed:{type(exc).__name__}") from exc

        try:
            payload = json.loads(raw_body.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TidalOAuthError("token_response_invalid_json") from exc
        if not isinstance(payload, dict):
            raise TidalOAuthError("token_response_invalid_payload")
        if payload.get("error"):
            raise TidalOAuthError(f"token_response_error:{str(payload.get('error'))[:80]}")
        return self.store_token_payload(payload, previous=previous)

    def _load_static_authorization(self) -> str:
        value = str(os.getenv("TIDAL_API_AUTHORIZATION") or "").strip()
        if value.casefold().startswith("bearer "):
            return self._clean_token(value[7:])
        return self._clean_token(value)

    def _is_token_current(self, payload: dict[str, Any]) -> bool:
        try:
            expires_at = float(payload.get("expires_at"))
        except (TypeError, ValueError):
            return True
        return expires_at > time.time() + self._EXPIRY_SKEW_SECONDS

    def _require_client_configuration(self, *, require_scope: bool) -> None:
        if not self.client_id:
            raise TidalOAuthError("missing_client_id")
        if not self.redirect_uri:
            raise TidalOAuthError("missing_redirect_uri")
        if require_scope and not self.scope:
            raise TidalOAuthError("missing_scope")

    def _resolve_token_file(self, token_file: str | Path | None) -> Path:
        raw_path = str(token_file or os.getenv("TIDAL_API_TOKEN_FILE") or "~/.config/webplayer-mcp/tidal-token.json")
        raw_path = raw_path.replace("%h", str(Path.home()), 1)
        return Path(raw_path).expanduser().resolve()

    def _write_token_payload(self, payload: dict[str, Any]) -> None:
        parent = self.token_file.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=".tidal-token-", dir=str(parent))
        try:
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
                handle.write("\n")
            os.replace(temporary_name, self.token_file)
            os.chmod(self.token_file, 0o600)
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _clean_token(value: Any) -> str:
        normalized = str(value or "").strip()
        if normalized.casefold().startswith("bearer "):
            normalized = normalized[7:].strip()
        return normalized

    @staticmethod
    def _generate_code_verifier() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")

    @staticmethod
    def _build_code_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")