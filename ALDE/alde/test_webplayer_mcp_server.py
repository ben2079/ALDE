from __future__ import annotations

import ast
import base64
import json
import math
import os
import re
import subprocess
import struct
import time

import pytest

import ALDE.alde.webplayer_mcp_server as webplayer_module
from ALDE.alde.webplayer_mcp_server import (
    CommandResult,
    HttpRequestObject,
    McpHttpRequestHandler,
    PlaybackScopeService,
    TidalApiPlaybackService,
    TidalApiService,
    TidalUserAuthService,
    WebPlayerMcpRequestService,
    WebPlayerService,
)


@pytest.fixture(autouse=True)
def isolate_playback_playlist_file(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBPLAYER_PLAYLIST_FILE", str(tmp_path / "playback-playlist.json"))


class TidalApiPlaybackServiceStub:
    def __init__(self, document_by_object_name: dict[str, dict]) -> None:
        self.document_by_object_name = document_by_object_name
        self.request_log: list[tuple[str, dict[str, object]]] = []

    def load_object_request(self, *, object_name: str, arguments: dict[str, object]) -> HttpRequestObject:
        self.request_log.append((object_name, dict(arguments)))
        return HttpRequestObject(
            object_name=object_name,
            method="GET",
            url="https://openapi.tidal.com/test",
            headers={},
            body=None,
            timeout_s=1,
        )

    def process_object(self, *, request_object: HttpRequestObject) -> dict[str, object]:
        document = self.document_by_object_name.get(request_object.object_name)
        if document is None:
            return {"ok": False, "status_code": 404}
        return {"ok": True, "response_body": json.dumps(document)}


class TidalApiMetadataServiceStub:
    def __init__(self, document_by_object_name: dict[str, dict]) -> None:
        self.document_by_object_name = document_by_object_name
        self.calls: list[tuple[str, dict[str, object]]] = []

    def dispatch_object(self, *, object_name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((object_name, dict(arguments)))
        document = self.document_by_object_name.get(object_name)
        if document is None:
            return {"ok": False, "error": "stub_document_missing"}
        return {"ok": True, "response_body": json.dumps(document)}


class TidalOAuthTokenServiceStub:
    def __init__(self, *, token_payload: dict[str, object] | None = None) -> None:
        self.token_payload = dict(token_payload or {})
        self.client_id = "client-123"
        self.scope = "r_usr w_usr"
        self.authorization_request = webplayer_module.TidalAuthorizationRequest(
            authorization_url="https://login.tidal.com/authorize?mock=true",
            state="state-123",
            code_verifier="verifier-123",
        )
        self.last_exchange_request: dict[str, object] = {}
        self.last_refresh_called = False

    def _load_static_authorization(self) -> str:
        return ""

    def load_token_payload(self) -> dict[str, object]:
        return dict(self.token_payload)

    def load_access_token(self) -> str:
        return str(self.token_payload.get("access_token") or "").strip()

    def build_authorization_request(self, *, state: str | None = None, code_verifier: str | None = None):
        authorization_request = self.authorization_request
        if state:
            authorization_request = webplayer_module.TidalAuthorizationRequest(
                authorization_url=authorization_request.authorization_url,
                state=state,
                code_verifier=authorization_request.code_verifier,
            )
        if code_verifier:
            authorization_request = webplayer_module.TidalAuthorizationRequest(
                authorization_url=authorization_request.authorization_url,
                state=authorization_request.state,
                code_verifier=code_verifier,
            )
        return authorization_request

    def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        state: str | None = None,
        expected_state: str | None = None,
    ) -> dict[str, object]:
        self.last_exchange_request = {
            "code": code,
            "code_verifier": code_verifier,
            "state": state,
            "expected_state": expected_state,
        }
        self.token_payload = {
            "access_token": "access-123",
            "refresh_token": "refresh-123",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
        }
        return dict(self.token_payload)

    def refresh_access_token(self) -> dict[str, object]:
        self.last_refresh_called = True
        self.token_payload = {
            "access_token": "access-123",
            "refresh_token": "refresh-123",
            "expires_at": time.time() + 3600,
            "token_type": "Bearer",
        }
        return dict(self.token_payload)


def _load_search_document(*track_ids: str) -> dict:
    references = [{"type": "tracks", "id": track_id} for track_id in track_ids]
    included = []
    for index, track_id in enumerate(track_ids, start=1):
        included.extend(
            [
                {
                    "type": "tracks",
                    "id": track_id,
                    "attributes": {
                        "title": f"Track {index}",
                        "duration": "180",
                        "explicit": False,
                        "mediaTags": ["LOSSLESS"],
                        "key": "A",
                        "keyScale": "MINOR",
                    },
                    "relationships": {
                        "artists": {"data": [{"type": "artists", "id": f"artist-{index}"}]},
                        "albums": {"data": [{"type": "albums", "id": f"album-{index}"}]},
                    },
                },
                {
                    "type": "artists",
                    "id": f"artist-{index}",
                    "attributes": {"name": f"Artist {index}"},
                },
                {
                    "type": "albums",
                    "id": f"album-{index}",
                    "attributes": {"title": f"Album {index}"},
                },
            ]
        )
    return {
        "data": {
            "type": "searchResults",
            "id": "search-result",
            "relationships": {"tracks": {"data": references}},
        },
        "included": included,
    }


class FakePlaybackProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if not self.terminated and not self.killed else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: int) -> None:
        _ = timeout

    def kill(self) -> None:
        self.killed = True


def test_api_playback_search_parses_json_api_tracks() -> None:
    tidal_api_service = TidalApiPlaybackServiceStub(
        {"tidal_api_search": _load_search_document("274309512")}
    )
    service = TidalApiPlaybackService(tidal_api_service)

    payload = service.dispatch_object(
        object_name="webplayer_search",
        query="bleeding corpse",
        arguments={},
    )

    assert payload["ok"] is True
    assert payload["track_count"] == 1
    assert payload["tracks"][0]["tidal_track_id"] == "274309512"
    assert payload["tracks"][0]["artist"] == "Artist 1"
    assert payload["tracks"][0]["album"] == "Album 1"


def test_api_playback_system_volume_adjust_preserves_known_signature(monkeypatch) -> None:
    service = TidalApiPlaybackService(TidalApiPlaybackServiceStub({}))
    set_calls: list[dict[str, object]] = []

    monkeypatch.setattr(service, "_load_system_volume_percent", lambda: 40)
    monkeypatch.setattr(
        service,
        "_set_system_volume",
        lambda **kwargs: set_calls.append(dict(kwargs)) or {"ok": True, **kwargs},
    )

    percent_payload = service.dispatch_object(
        object_name="webplayer_system_volume_adjust",
        query=None,
        arguments={"delta_percent": 5},
    )
    float_payload = service.dispatch_object(
        object_name="webplayer_system_volume_adjust",
        query=None,
        arguments={"delta_float": -0.1},
    )

    assert percent_payload["ok"] is True
    assert float_payload["ok"] is True
    assert set_calls == [
        {"volume_percent": 45, "object_name": "webplayer_system_volume_adjust"},
        {"volume_percent": 30, "object_name": "webplayer_system_volume_adjust"},
    ]


def test_api_playback_track_context_parses_artist_album_art_and_extended_metadata() -> None:
    track_resource = {
        "type": "tracks",
        "id": "305372",
        "attributes": {
            "title": "Return of the Mack",
            "duration": "PT3M33S",
            "explicit": False,
            "mediaTags": ["LOSSLESS"],
            "key": "A",
            "keyScale": "MINOR",
            "copyright": {"text": "\u00a9 1997 Mark Morrison"},
            "isrc": "GBBHF9700001",
            "externalLinks": [{"href": "https://tidal.com/browse/track/305372"}],
        },
        "relationships": {
            "artists": {"data": [{"type": "artists", "id": "16154"}]},
            "albums": {"data": [{"type": "albums", "id": "305366"}]},
        },
    }
    album_resource = {
        "type": "albums",
        "id": "305366",
        "attributes": {
            "title": "Return of the Mack",
            "releaseDate": "1997-03-11",
            "numberOfItems": 13,
            "numberOfVolumes": 1,
        },
        "relationships": {
            "coverArt": {"data": [{"type": "artworks", "id": "art-305366"}]},
        },
    }
    artist_resource = {
        "type": "artists",
        "id": "16154",
        "attributes": {
            "name": "Mark Morrison",
            "externalLinks": [{"href": "https://tidal.com/browse/artist/16154"}],
        },
    }
    artwork_resource = {
        "type": "artworks",
        "id": "art-305366",
        "attributes": {
            "files": [
                {"href": "https://resources.tidal.com/images/cover-1280.jpg", "meta": {"width": 1280}},
            ]
        },
    }
    service = TidalApiPlaybackService(TidalApiPlaybackServiceStub({}))

    payload = service._load_track_from_resource(
        resource=track_resource,
        included=[album_resource, artist_resource, artwork_resource],
    )

    assert payload["title"] == "Return of the Mack"
    assert payload["artist"] == "Mark Morrison"
    assert payload["album"] == "Return of the Mack"
    assert payload["duration_seconds"] == 213
    assert payload["tonart"] == "A MINOR"
    assert payload["copyright"] == "\u00a9 1997 Mark Morrison"
    assert payload["isrc"] == "GBBHF9700001"
    assert payload["art_url"] == "https://resources.tidal.com/images/cover-1280.jpg"
    assert payload["album_info"]["release_date"] == "1997-03-11"
    assert payload["album_info"]["number_of_items"] == 13
    assert payload["album_info"]["number_of_volumes"] == 1
    assert payload["artist_info"][0]["name"] == "Mark Morrison"
    assert payload["artist_info"][0]["url"] == "https://listen.tidal.com/artist/16154"


def test_api_playback_rejects_drm_manifest() -> None:
    tidal_api_service = TidalApiPlaybackServiceStub(
        {
            "tidal_api_search": _load_search_document("274309512"),
            "tidal_api_track_manifest": {
                "data": {
                    "type": "trackManifests",
                    "id": "274309512",
                    "attributes": {
                        "uri": "https://stream.example/manifest.m3u8",
                        "drmData": {"drmSystem": "WIDEVINE"},
                    },
                }
            },
        }
    )
    service = TidalApiPlaybackService(tidal_api_service)

    payload = service.dispatch_object(
        object_name="webplayer_search_play",
        query="bleeding corpse",
        arguments={},
    )

    assert payload["ok"] is False
    assert payload["error"] == "api_playback_drm_required:WIDEVINE"
    assert payload["stderr"] == "api_playback_drm_required:WIDEVINE"


def test_api_library_snapshot_uses_user_collection_tracks() -> None:
    search_document = _load_search_document("274309512")
    tidal_api_service = TidalApiPlaybackServiceStub(
        {
            "tidal_api_request": {
                "data": {
                    "type": "userCollectionTracks",
                    "id": "me",
                    "relationships": {
                        "items": {
                            "data": [{"type": "tracks", "id": "274309512"}],
                        }
                    },
                },
                "included": search_document["included"],
            }
        }
    )
    service = TidalApiPlaybackService(tidal_api_service)

    payload = service.dispatch_object(
        object_name="webplayer_library_snapshot",
        query=None,
        arguments={"section": "favorites_tracks"},
    )

    assert payload["ok"] is True
    assert payload["snapshot_backend"] == "api"
    assert payload["snapshot_source"] == "tidal_api"
    assert payload["snapshot_count"] == 1
    assert payload["tracks"][0]["liked"] is True


def test_api_playback_rejects_non_tidal_target() -> None:
    service = TidalApiPlaybackService(TidalApiPlaybackServiceStub({}))

    payload = service.dispatch_object(
        object_name="webplayer_open_playback_target",
        query=None,
        arguments={"target_url": "https://example.com/track/274309512"},
    )

    assert payload["ok"] is False
    assert payload["error"] == "invalid_target_url"


def test_api_like_and_toggle_use_collection_relationship_mutations() -> None:
    tidal_api_service = TidalApiPlaybackServiceStub({"tidal_api_request": {}})
    service = TidalApiPlaybackService(tidal_api_service)
    service._active_track = {
        "tidal_track_id": "274309512",
        "title": "Track 1",
        "liked": False,
    }

    like_payload = service.dispatch_object(
        object_name="webplayer_like_current",
        query=None,
        arguments={},
    )
    toggle_payload = service.dispatch_object(
        object_name="webplayer_toggle_like_current",
        query=None,
        arguments={},
    )

    assert like_payload["ok"] is True
    assert like_payload["status"] == "Liked"
    assert like_payload["liked"] is True
    assert toggle_payload["ok"] is True
    assert toggle_payload["status"] == "Unliked"
    assert toggle_payload["liked"] is False

    like_request = tidal_api_service.request_log[-2][1]
    toggle_request = tidal_api_service.request_log[-1][1]
    assert like_request["method"] == "POST"
    assert toggle_request["method"] == "DELETE"
    assert like_request["body_json"] == {"data": [{"type": "tracks", "id": "274309512"}]}
    assert toggle_request["body_json"] == {"data": [{"type": "tracks", "id": "274309512"}]}


def test_api_playback_starts_clear_manifest_and_controls_queue(monkeypatch) -> None:
    tidal_api_service = TidalApiPlaybackServiceStub(
        {
            "tidal_api_search": _load_search_document("274309512", "274309513"),
            "tidal_api_track_manifest": {
                "data": {
                    "type": "trackManifests",
                    "id": "274309512",
                    "attributes": {"uri": "https://stream.example/track.m3u8"},
                }
            },
        }
    )
    created_processes: list[tuple[list[str], FakePlaybackProcess]] = []

    def fake_popen(command: list[str], **kwargs: object) -> FakePlaybackProcess:
        _ = kwargs
        process = FakePlaybackProcess()
        created_processes.append((command, process))
        return process

    monkeypatch.setattr(webplayer_module.shutil, "which", lambda binary: "/usr/bin/cvlc" if binary == "cvlc" else None)
    monkeypatch.setattr(webplayer_module.subprocess, "Popen", fake_popen)
    service = TidalApiPlaybackService(tidal_api_service)

    started_payload = service.dispatch_object(
        object_name="webplayer_search_play",
        query="bleeding corpse",
        arguments={},
    )
    next_payload = service.dispatch_object(object_name="webplayer_forward", query=None, arguments={})
    boundary_payload = service.dispatch_object(object_name="webplayer_forward", query=None, arguments={})
    stop_payload = service.dispatch_object(object_name="webplayer_stop", query=None, arguments={})

    assert started_payload["ok"] is True
    assert started_payload["status"] == "Playing"
    assert started_payload["media_product"]["tidal_track_id"] == "274309512"
    assert started_payload["media_product"]["artist_info"][0]["name"] == "Artist 1"
    assert started_payload["playback_context"]["queue_index"] == 0
    assert started_payload["playback_context"]["status"] == "Playing"
    assert started_payload["now_playing"]["media_product"]["tidal_track_id"] == "274309512"
    assert started_payload["now_playing"]["playback_context"]["status"] == "Playing"
    assert created_processes[0][0][-1] == "https://stream.example/track.m3u8"
    assert next_payload["ok"] is True
    assert next_payload["tidal_track_id"] == "274309513"
    assert next_payload["media_product"]["tidal_track_id"] == "274309513"
    assert boundary_payload["ok"] is False
    assert boundary_payload["error"] == "api_queue_boundary"
    assert stop_payload["ok"] is True
    assert stop_payload["status"] == "Stopped"
    assert created_processes[-1][1].terminated is True


def test_api_library_play_accepts_generated_collection_track_ids(monkeypatch) -> None:
    service = TidalApiPlaybackService(TidalApiPlaybackServiceStub({}))

    monkeypatch.setattr(webplayer_module.shutil, "which", lambda binary: "/usr/bin/cvlc" if binary == "cvlc" else None)
    monkeypatch.setattr(webplayer_module.subprocess, "Popen", lambda *_args, **_kwargs: FakePlaybackProcess())
    service._load_track = lambda *, track_id, arguments: (
        {
            "tidal_track_id": track_id,
            "title": f"Track {track_id}",
        },
        "",
    )  # type: ignore[method-assign]
    service._load_manifest = lambda *, track_id: ({"uri": f"https://stream.example/{track_id}.m3u8"}, "")  # type: ignore[method-assign]

    payload = service.dispatch_object(
        object_name="webplayer_library_play",
        query=None,
        arguments={
            "track_ids": ["1", "2", "1"],
            "collection_name": "generated_focus",
        },
    )

    assert payload["ok"] is True
    assert payload["tidal_track_id"] == "1"
    assert [track["tidal_track_id"] for track in service._queue] == ["1", "2"]
    assert service._playlist.source["source_type"] == "generated_collection"
    assert service._playlist.source["collection_name"] == "generated_focus"


def test_api_playback_resumes_persisted_playlist_after_service_reload(monkeypatch) -> None:
    tidal_api_service = TidalApiPlaybackServiceStub(
        {
            "tidal_api_search": _load_search_document("274309512", "274309513"),
            "tidal_api_track_manifest": {
                "data": {
                    "type": "trackManifests",
                    "id": "274309512",
                    "attributes": {"uri": "https://stream.example/track.m3u8"},
                }
            },
        }
    )
    monkeypatch.setattr(webplayer_module.shutil, "which", lambda binary: "/usr/bin/cvlc" if binary == "cvlc" else None)
    monkeypatch.setattr(webplayer_module.subprocess, "Popen", lambda *_args, **_kwargs: FakePlaybackProcess())

    service = TidalApiPlaybackService(tidal_api_service)
    service.dispatch_object(
        object_name="webplayer_search_play",
        query="bleeding corpse",
        arguments={},
    )
    service.dispatch_object(object_name="webplayer_forward", query=None, arguments={})

    reloaded_service = TidalApiPlaybackService(tidal_api_service)
    now_playing = reloaded_service.dispatch_object(
        object_name="webplayer_now_playing",
        query=None,
        arguments={},
    )

    assert reloaded_service._queue_index == 1
    assert reloaded_service._active_track["tidal_track_id"] == "274309513"
    assert now_playing["tidal_track_id"] == "274309513"
    assert now_playing["status"] == "Stopped"


def test_api_playback_reports_playlist_persistence_failure_without_undoing_start(monkeypatch) -> None:
    tidal_api_service = TidalApiPlaybackServiceStub(
        {
            "tidal_api_search": _load_search_document("274309512"),
            "tidal_api_track_manifest": {
                "data": {
                    "type": "trackManifests",
                    "id": "274309512",
                    "attributes": {"uri": "https://stream.example/track.m3u8"},
                }
            },
        }
    )
    monkeypatch.setattr(webplayer_module.shutil, "which", lambda binary: "/usr/bin/cvlc" if binary == "cvlc" else None)
    monkeypatch.setattr(webplayer_module.subprocess, "Popen", lambda *_args, **_kwargs: FakePlaybackProcess())
    service = TidalApiPlaybackService(tidal_api_service)

    def fail_save(_playlist):
        raise OSError("disk full")

    monkeypatch.setattr(service.playlist_repository, "save_playlist", fail_save)

    payload = service.dispatch_object(
        object_name="webplayer_search_play",
        query="bleeding corpse",
        arguments={},
    )

    assert payload["ok"] is True
    assert payload["status"] == "Playing"
    assert payload["playlist_persisted"] is False
    assert payload["playlist_persistence_error"].startswith("playlist_persistence_failed:")


def test_api_playback_normalizes_http_errors() -> None:
    service = TidalApiPlaybackService(
        TidalApiPlaybackServiceStub({})
    )

    payload = service.dispatch_object(
        object_name="webplayer_search",
        query="bleeding corpse",
        arguments={},
    )

    assert payload["ok"] is False
    assert payload["error"] == "api_http_404"


def test_webplayer_defaults_to_browser_backend_for_search() -> None:
    class CommandServiceStub:
        def run_object(self, **kwargs):
            return CommandResult(
                object_name=str(kwargs["object_name"]),
                return_code=0,
                stdout="search_url=https://listen.tidal.com/search/bleeding+corpse",
                stderr="",
                command=str(kwargs["command"]),
            )

    service = WebPlayerService(
        command_service=CommandServiceStub(),
        tidal_api_service=TidalApiPlaybackServiceStub(
            {"tidal_api_search": _load_search_document("274309512")}
        ),
    )

    payload = service.dispatch_object(
        object_name="webplayer_search",
        query="bleeding corpse",
        arguments={},
    )

    assert payload["ok"] is True
    assert payload["backend"] == "browser"
    assert payload["playback_backend"] == "browser"
    assert payload["player_selector"] == "chromium"
    assert payload["controller"] == "wplctl"


def test_webplayer_ignores_legacy_api_backend_environment(monkeypatch) -> None:
    monkeypatch.setenv("WEBPLAYER_PLAYBACK_BACKEND", "api")

    service = WebPlayerService()

    assert service.playback_backend == "browser"


def test_webplayer_browser_prepare_uses_browser_command() -> None:
    class CommandServiceStub:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run_object(self, **kwargs: object):
            self.calls.append(dict(kwargs))
            return CommandResult(
                object_name=str(kwargs["object_name"]),
                return_code=0,
                stdout="status=Playing\ntitle=Browser title\nartist=Browser artist\nalbum=Browser album",
                stderr="",
                command=str(kwargs["command"]),
            )

    command_service = CommandServiceStub()
    tidal_api_service = TidalApiPlaybackServiceStub(
        {"tidal_api_search": _load_search_document("274309512")}
    )
    service = WebPlayerService(
        command_service=command_service,
        tidal_api_service=tidal_api_service,
    )

    payload = service.dispatch_object(
        object_name="webplayer_search_play",
        query="bleeding corpse",
        arguments={"playback_backend": "sdk"},
    )

    assert payload["ok"] is True
    assert payload["backend"] == "browser"
    assert payload["playback_backend"] == "browser"
    assert payload["player_selector"] == "chromium"
    assert payload["controller"] == "wplctl"
    assert payload["now_playing"]["artist"] == "Browser artist"
    assert payload["now_playing"]["album"] == "Browser album"
    assert command_service.calls
    assert tidal_api_service.request_log == []


def test_load_selected_player_name_prefers_api_transport_and_skips_spotify() -> None:
    assert webplayer_module.load_selected_player_name(
        players=["spotify.instance", "cvlc.instance"],
        player_selector="tidal-api",
    ) == "cvlc.instance"
    assert webplayer_module.load_selected_player_name(
        players=["spotify.instance"],
        player_selector="tidal-api",
    ) == ""
    assert webplayer_module.load_selected_player_name(
        players=["spotify.instance", "chromium.instance"],
        player_selector="chromium",
    ) == "chromium.instance"


def test_cover_source_for_player_does_not_fall_back_to_spotify(monkeypatch) -> None:
    class RunResult:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0
            self.stderr = ""

    calls: list[list[str]] = []

    def fake_run(command, capture_output, text, check, timeout):  # noqa: ANN001
        _ = capture_output, text, check, timeout
        calls.append(list(command))
        if command == ["playerctl", "-l"]:
            return RunResult("spotify.instance\n")
        raise AssertionError(f"unexpected playerctl call: {command}")

    monkeypatch.setattr(webplayer_module.subprocess, "run", fake_run)

    assert McpHttpRequestHandler._load_cover_source_for_player(player_selector="tidal-api") == ""
    assert calls == [["playerctl", "-l"]]


def test_cover_file_path_uses_selected_chromium_mount_namespace(monkeypatch, tmp_path) -> None:
    source_path = "/tmp/.org.chromium.Chromium.TestCover123"
    proc_root = tmp_path / "proc"
    namespace_file = proc_root / "5716" / "root" / source_path.lstrip("/")
    namespace_file.parent.mkdir(parents=True)
    namespace_file.write_bytes(b"\x89PNG\r\n\x1a\ncover")

    class RunResult:
        stdout = "chromium.instance5716\nspotify.instance\n"

    monkeypatch.setattr(webplayer_module.subprocess, "run", lambda *_args, **_kwargs: RunResult())

    assert McpHttpRequestHandler._load_cover_file_path(
        source_value=f"file://{source_path}", player_selector="chromium", proc_root=str(proc_root)
    ) == str(namespace_file)
    assert McpHttpRequestHandler._load_cover_file_path(
        source_value="file:///tmp/../etc/passwd", player_selector="chromium", proc_root=str(proc_root)
    ) == ""
    assert McpHttpRequestHandler._load_cover_file_path(
        source_value=f"file://{source_path}", player_selector="spotify", proc_root=str(proc_root)
    ) == ""


def test_tools_list_contains_required_webplayer_tools() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])
    names = {(item.get("function") or {}).get("name") for item in tools}

    assert "webplayer_play" in names
    assert "webplayer_stop" in names
    assert "webplayer_forward" in names
    assert "webplayer_backward" in names
    assert "webplayer_like_current" in names
    assert "webplayer_toggle_like_current" in names
    assert "webplayer_now_playing" in names
    assert "webplayer_search" in names
    assert "webplayer_search_play" in names
    assert "webplayer_playlist_play" in names
    assert "webplayer_library_play" in names
    assert "webplayer_open_playback_target" in names
    assert "webplayer_usrauth" in names
    assert "webplayer_volume_set" in names
    assert "webplayer_volume_adjust" in names
    assert "webplayer_system_volume_set" in names
    assert "webplayer_system_volume_adjust" in names
    assert "tidal_api_request" in names
    assert "tidal_api_search" in names
    assert "tidal_api_track" in names
    assert "tidal_api_track_manifest" in names
    assert "tidal_api_widevine" in names


def test_search_play_tool_exposes_browser_backend_only() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])

    search_play_definition = None
    for tool_definition in tools:
        function_definition = tool_definition.get("function") or {}
        if function_definition.get("name") == "webplayer_search_play":
            search_play_definition = function_definition
            break

    assert search_play_definition is not None
    properties = (search_play_definition.get("parameters") or {}).get("properties") or {}
    assert properties["playback_backend"]["enum"] == ["browser"]
    assert properties["player_selector"]["enum"] == ["chromium"]
    assert "cdp_autoclick" not in properties
    assert "cdp_port" not in properties
    assert "cdp_click_timeout_s" not in properties
    assert "wait_for_page_s" not in properties
    assert "wait_for_player_s" not in properties
    assert "play_attempts" not in properties
    assert "command_timeout_s" not in properties


def test_tools_call_routes_tidal_api_tool_to_tidal_service() -> None:
    class TidalApiServiceStub:
        def __init__(self) -> None:
            self.called_object_name = ""
            self.called_arguments: dict[str, object] = {}

        def dispatch_object(self, *, object_name: str, arguments: dict[str, object]) -> dict[str, object]:
            self.called_object_name = object_name
            self.called_arguments = arguments
            return {
                "ok": True,
                "object_name": object_name,
                "request_url": arguments.get("url"),
            }

    tidal_api_service_stub = TidalApiServiceStub()
    service = WebPlayerMcpRequestService(tidal_api_service=tidal_api_service_stub)

    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "tidal_api_request",
                "arguments": {
                    "method": "GET",
                    "url": "https://tidal.com/v1/ping",
                },
            },
        }
    )

    result_payload = json.loads(payload.get("result", {}).get("content", "{}"))
    assert tidal_api_service_stub.called_object_name == "tidal_api_request"
    assert tidal_api_service_stub.called_arguments["url"] == "https://tidal.com/v1/ping"
    assert result_payload["ok"] is True
    assert result_payload["object_name"] == "tidal_api_request"


def test_tools_call_routes_webplayer_usrauth_to_auth_service() -> None:
    class UserAuthServiceStub:
        def __init__(self) -> None:
            self.called_object_name = ""
            self.called_arguments: dict[str, object] = {}

        def dispatch_object(self, *, object_name: str, arguments: dict[str, object]) -> dict[str, object]:
            self.called_object_name = object_name
            self.called_arguments = dict(arguments)
            return {
                "ok": True,
                "object_name": object_name,
                "status": "login_required",
                "authorization_url": "https://login.tidal.com/authorize?mock=true",
            }

    user_auth_service_stub = UserAuthServiceStub()
    service = WebPlayerMcpRequestService(user_auth_service=user_auth_service_stub)

    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_usrauth",
                "arguments": {"force_login": True},
            },
        }
    )

    result_payload = json.loads(payload.get("result", {}).get("content", "{}"))
    assert user_auth_service_stub.called_object_name == "webplayer_usrauth"
    assert user_auth_service_stub.called_arguments["force_login"] is True
    assert result_payload["ok"] is True
    assert result_payload["status"] == "login_required"


def test_webplayer_usrauth_starts_login_and_completes_redirect_flow() -> None:
    token_service = TidalOAuthTokenServiceStub()
    auth_service = TidalUserAuthService(token_service=token_service)

    begin_payload = auth_service.dispatch_object(object_name="webplayer_usrauth", arguments={})
    complete_payload = auth_service.dispatch_object(
        object_name="webplayer_usrauth",
        arguments={
            "redirect_url": "http://127.0.0.1:8768/callback?code=abc123&state=state-123",
        },
    )

    assert begin_payload["ok"] is True
    assert begin_payload["login_action"] == "begin"
    assert begin_payload["status"] == "login_required"
    assert begin_payload["authorization_url"] == token_service.authorization_request.authorization_url
    assert begin_payload["state"] == "state-123"
    assert begin_payload["pending_login"] is True

    assert complete_payload["ok"] is True
    assert complete_payload["login_action"] == "complete"
    assert complete_payload["status"] == "authenticated"
    assert complete_payload["token_source"] == "token_file"
    assert token_service.last_exchange_request["code"] == "abc123"
    assert token_service.last_exchange_request["code_verifier"] == "verifier-123"
    assert token_service.last_exchange_request["expected_state"] == "state-123"
    assert auth_service._pending_authorization_request is None


def test_webplayer_usrauth_reports_existing_token_without_reauth() -> None:
    token_service = TidalOAuthTokenServiceStub(
        token_payload={
            "access_token": "stored-access",
            "refresh_token": "stored-refresh",
            "expires_at": time.time() + 3600,
        }
    )
    auth_service = TidalUserAuthService(token_service=token_service)

    payload = auth_service.dispatch_object(object_name="webplayer_usrauth", arguments={})

    assert payload["ok"] is True
    assert payload["login_action"] == "status"
    assert payload["status"] == "authenticated"
    assert payload["authentication_state"] == "ready"
    assert payload["token_current"] is True
    assert payload["has_refresh_token"] is True


def test_webplayer_usrauth_never_returns_sdk_credentials() -> None:
    expires_at = time.time() + 3600
    token_service = TidalOAuthTokenServiceStub(
        token_payload={
            "access_token": "stored-access",
            "refresh_token": "stored-refresh",
            "expires_at": expires_at,
        }
    )
    auth_service = TidalUserAuthService(token_service=token_service)

    normal_payload = auth_service.dispatch_object(object_name="webplayer_usrauth", arguments={})
    legacy_payload = auth_service.dispatch_object(
        object_name="webplayer_usrauth",
        arguments={"sdk_credentials": True},
    )

    assert normal_payload["ok"] is True
    assert "sdk_credentials" not in normal_payload
    assert legacy_payload["ok"] is True
    assert "sdk_credentials" not in legacy_payload


def test_tidal_api_request_rejects_non_tidal_host() -> None:
    service = TidalApiService()
    payload = service.dispatch_object(
        object_name="tidal_api_request",
        arguments={
            "url": "https://example.com/v1/ping",
        },
    )

    assert payload["ok"] is False
    assert payload["error"] == "invalid_host_non_tidal"


def test_tidal_api_request_is_read_only() -> None:
    service = TidalApiService()

    for request_method in ("POST", "PUT", "PATCH", "DELETE"):
        payload = service.dispatch_object(
            object_name="tidal_api_request",
            arguments={
                "method": request_method,
                "url": "https://openapi.tidal.com/v2/userCollectionTracks",
            },
        )

        assert payload["ok"] is False
        assert payload["error"] == "informational_api_read_only"


def test_tidal_api_add_track_to_playlist_builds_json_api_post() -> None:
    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_add_track_to_playlist",
        arguments={"playlist_id": "playlist/42", "track_id": "1758221"},
    )

    assert request_object.method == "POST"
    assert request_object.object_name == "tidal_api_add_track_to_playlist"
    assert request_object.url.endswith("/v2/playlists/playlist%2F42/relationships/items")
    assert json.loads(request_object.body.decode("utf-8")) == {
        "data": [{"type": "tracks", "id": "1758221"}],
    }
    assert request_object.headers["Content-Type"] == "application/vnd.api+json"


def test_tidal_api_collection_write_requires_user_authentication() -> None:
    service = TidalApiService(
        oauth_token_service=TidalOAuthTokenServiceStub(token_payload={}),
    )

    payload = service.dispatch_object(
        object_name="tidal_api_add_track_to_playlist",
        arguments={"playlist_id": "42", "track_id": "1758221"},
    )

    assert payload["ok"] is False
    assert payload["error"] == "user_auth_required"
    assert payload["required_scopes"] == ["collection.write", "w_usr"]
    assert payload["auth_tool"] == "webplayer_usrauth"


def test_tidal_api_collection_write_accepts_user_authorization_header() -> None:
    service = TidalApiService(
        oauth_token_service=TidalOAuthTokenServiceStub(token_payload={}),
    )
    request_object = service.load_object_request(
        object_name="tidal_api_add_track_to_playlist",
        arguments={
            "playlist_id": "42",
            "track_id": "1758221",
            "headers": {"Authorization": "Bearer user-token"},
        },
    )

    assert request_object.method == "POST"
    assert request_object.headers["Authorization"] == "Bearer user-token"


def test_tidal_api_add_track_to_collection_builds_json_api_post() -> None:
    service = TidalApiService(
        oauth_token_service=TidalOAuthTokenServiceStub(token_payload={"access_token": "user-token"}),
    )
    request_object = service.load_object_request(
        object_name="tidal_api_add_track_to_collection",
        arguments={"track_id": "1758221"},
    )

    assert request_object.method == "POST"
    assert request_object.url == "https://openapi.tidal.com/v2/userCollectionTracks/me/relationships/items"
    assert json.loads(request_object.body.decode("utf-8")) == {
        "data": [{"type": "tracks", "id": "1758221"}],
    }


def test_tidal_api_add_track_to_collection_rejects_non_positive_track_id() -> None:
    service = TidalApiService(
        oauth_token_service=TidalOAuthTokenServiceStub(token_payload={"access_token": "user-token"}),
    )

    payload = service.dispatch_object(
        object_name="tidal_api_add_track_to_collection",
        arguments={
            "track_id": "0",
            "headers": {"Authorization": "Bearer user-token"},
        },
    )

    assert payload == {
        "ok": False,
        "error": "invalid_track_id",
        "object_name": "tidal_api_add_track_to_collection",
    }


def test_tidal_api_collection_write_marks_successful_track_as_liked() -> None:
    service = TidalApiService(
        oauth_token_service=TidalOAuthTokenServiceStub(token_payload={"access_token": "user-token"}),
    )

    class Response:
        status = 200

        class Headers:
            def items(self):
                return [("Content-Type", "application/vnd.api+json")]

        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=None):
            return b'{"data": []}'

    service_module = webplayer_module
    original_urlopen = service_module.urlopen
    service_module.urlopen = lambda *_args, **_kwargs: Response()
    try:
        payload = service.dispatch_object(
            object_name="tidal_api_add_track_to_collection",
            arguments={
                "track_id": "1758221",
                "headers": {"Authorization": "Bearer user-token"},
            },
        )
    finally:
        service_module.urlopen = original_urlopen

    assert payload["ok"] is True
    assert payload["liked"] is True
    assert payload["liked_state"] == "liked"


def test_tidal_api_search_builds_official_json_api_query() -> None:
    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_search",
        arguments={"query": "bleeding corpse"},
    )

    assert request_object.method == "GET"
    assert request_object.object_name == "tidal_api_search"
    assert request_object.url.startswith("https://openapi.tidal.com/v2/searchResults?")
    assert "filter%5Bquery%5D=bleeding+corpse" in request_object.url
    assert "countryCode=DE" in request_object.url
    assert "include=tracks" in request_object.url
    assert "include=tracks.artists" in request_object.url
    assert "include=tracks.albums" in request_object.url
    assert "include=tracks.albums.coverArt" in request_object.url
    assert "include=topHits" in request_object.url
    assert request_object.headers["Accept"] == "application/vnd.api+json"


def test_tidal_api_json_api_response_stays_textual() -> None:
    service = TidalApiService()

    response_body, response_body_base64 = service._load_response_body_payload(
        response_body_bytes=b'{"data": {"type": "searchResults"}}',
        content_type="application/vnd.api+json; charset=utf-8",
    )

    assert response_body == '{"data": {"type": "searchResults"}}'
    assert response_body_base64 is None


def test_tidal_api_track_manifest_builds_official_query() -> None:
    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_track_manifest",
        arguments={
            "track_id": "1758221",
            "adaptive": False,
            "formats": "AACLC",
            "manifest_type": "HLS",
            "uri_scheme": "HTTPS",
            "usage": "PLAYBACK",
        },
    )

    assert request_object.method == "GET"
    assert request_object.object_name == "tidal_api_track_manifest"
    assert request_object.url.startswith("https://openapi.tidal.com/v2/trackManifests/1758221?")
    assert "adaptive=false" in request_object.url
    assert "formats=AACLC" in request_object.url
    assert "manifestType=HLS" in request_object.url
    assert "uriScheme=HTTPS" in request_object.url
    assert "usage=PLAYBACK" in request_object.url


def test_tidal_api_headers_load_from_environment_and_explicit_headers_win(monkeypatch) -> None:
    monkeypatch.setenv("TIDAL_API_AUTHORIZATION", "access-token")
    monkeypatch.setenv("TIDAL_API_CLIENT_TOKEN", "client-token")
    monkeypatch.setenv("TIDAL_API_COOKIE", "session=secret")
    service = TidalApiService()

    request_object = service.load_object_request(
        object_name="tidal_api_track_manifest",
        arguments={
            "track_id": "274309512",
            "headers": {"Authorization": "Bearer explicit-token"},
        },
    )

    assert request_object.headers["Authorization"] == "Bearer explicit-token"
    assert request_object.headers["x-tidal-token"] == "client-token"
    assert request_object.headers["Cookie"] == "session=secret"


def test_tidal_api_payload_redacts_environment_credentials(monkeypatch) -> None:
    monkeypatch.setenv("TIDAL_API_AUTHORIZATION", "access-token")
    service = TidalApiService()
    request_object = service.load_object_request(
        object_name="tidal_api_track",
        arguments={"track_id": "274309512"},
    )

    payload = service.process_object(request_object=request_object)

    assert payload["request_headers"]["Authorization"] == "<redacted>"


def test_tools_call_rejects_missing_search_query() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_search",
                "arguments": {},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_query" in result_payload.get("stdout", "")


def test_tools_call_rejects_missing_search_play_query() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_search_play",
                "arguments": {},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_query" in result_payload.get("stdout", "")


def test_mcp_webplayer_calls_force_browser_arguments() -> None:
    captured: dict[str, object] = {}

    class PlayerServiceStub:
        def dispatch_object(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"ok": True, "status": "Ready"}

    service = WebPlayerMcpRequestService(player_service=PlayerServiceStub())

    payload = service.load_tools_call_result(
        {
            "name": "webplayer_play",
            "arguments": {
                "player_selector": "tidal-api",
                "playback_backend": "api",
            },
        }
    )

    assert json.loads(payload["result"]["content"])["ok"] is True
    assert captured["player_selector"] == "chromium"
    assert captured["arguments"]["player_selector"] == "chromium"
    assert captured["arguments"]["playback_backend"] == "browser"
    assert captured["arguments"]["requested_playback_backend"] == "api"


def test_tools_call_rejects_missing_playlist_target() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_playlist_play",
                "arguments": {},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "missing_playlist_target" in result_payload.get("stdout", "")


def test_tools_call_rejects_invalid_library_section() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "tools/call",
            "params": {
                "name": "webplayer_library_play",
                "arguments": {"section": "does_not_exist"},
            },
        }
    )

    content = payload.get("result", {}).get("content")
    result_payload = json.loads(content)
    assert result_payload["ok"] is False
    assert "invalid_library_section" in result_payload.get("stdout", "")


def test_webplayer_uses_browser_backend_for_library_snapshot() -> None:
    class CommandServiceStub:
        def run_object(self, **kwargs):
            return CommandResult(
                object_name=str(kwargs["object_name"]),
                return_code=0,
                stdout="snapshot_count=0",
                stderr="",
                command=str(kwargs["command"]),
            )

    service = WebPlayerService(command_service=CommandServiceStub())
    payload = service.dispatch_object(
        object_name="webplayer_library_snapshot",
        player_selector="chromium",
        arguments={"section": "favorites_tracks", "playback_backend": "browser"},
    )

    assert payload["ok"] is True
    assert payload["backend"] == "browser"
    assert payload["playback_backend"] == "browser"
    assert payload["player_selector"] == "chromium"
    assert payload["controller"] == "wplctl"


def test_webplayer_browser_backend_runs_remote_command_when_enabled() -> None:
    class CommandServiceStub:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run_object(self, **kwargs: object) -> CommandResult:
            self.calls.append(dict(kwargs))
            return CommandResult(
                object_name=str(kwargs["object_name"]),
                return_code=0,
                stdout="\n".join(
                    [
                        "player=chromium.instance2147458",
                        "status=Playing",
                        "title=Remote title",
                        "artist=Remote artist",
                        "album=Remote album",
                    ]
                ),
                stderr="",
                command=str(kwargs["command"]),
            )

    command_service = CommandServiceStub()
    service = WebPlayerService(
        command_service=command_service,
        browser_backend_enabled=True,
    )

    payload = service.dispatch_object(
        object_name="webplayer_now_playing",
        player_selector="tidal-api",
        arguments={
            "playback_backend": "browser",
            "cdp_probe_quality": False,
        },
    )

    assert payload["ok"] is True
    assert payload["backend"] == "browser"
    assert payload["playback_backend"] == "browser"
    assert payload["player_selector"] == "chromium"
    assert payload["now_playing"]["title"] == "Remote title"
    assert command_service.calls[0]["object_name"] == "webplayer_now_playing"
    assert command_service.calls[0]["command_timeout_s"] == 20


def test_browser_like_uses_current_track_id_without_api_control() -> None:
    class CommandServiceStub:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run_object(self, **kwargs: object) -> CommandResult:
            self.calls.append(dict(kwargs))
            return CommandResult(
                object_name=str(kwargs["object_name"]),
                return_code=0,
                stdout="\n".join(
                    [
                        "player=chromium.instance2147458",
                        "status=Playing",
                        "title=Current title",
                        "artist=Current artist",
                        "album=Current album",
                        "track_url=https://tidal.com/track/5526443",
                        "tidal_track_id=5526443",
                        "liked=false",
                    ]
                ),
                stderr="",
                command=str(kwargs["command"]),
            )

    command_service = CommandServiceStub()
    tidal_api_service = TidalApiPlaybackServiceStub({"tidal_api_request": {}})
    service = WebPlayerService(
        command_service=command_service,
        tidal_api_service=tidal_api_service,
        browser_backend_enabled=True,
    )

    payload = service.dispatch_object(
        object_name="webplayer_like_current",
        player_selector="chromium",
        arguments={
            "playback_backend": "browser",
            "cdp_port": 9222,
        },
    )

    assert payload["ok"] is True
    assert payload["backend"] == "browser"
    assert payload["playback_backend"] == "browser"
    assert payload["player_selector"] == "chromium"
    assert payload["controller"] == "wplctl"
    assert payload["now_playing"]["tidal_track_id"] == "5526443"
    assert tidal_api_service.request_log == []

    refreshed_payload = service.dispatch_object(
        object_name="webplayer_now_playing",
        player_selector="chromium",
        arguments={"playback_backend": "browser"},
    )
    assert refreshed_payload["now_playing"]["tidal_track_id"] == "5526443"
    assert refreshed_payload["now_playing"]["liked"] == "false"
    assert [call["object_name"] for call in command_service.calls] == [
        "webplayer_like_current",
        "webplayer_now_playing",
    ]


def test_browser_toggle_like_uses_browser_controller_without_api_control() -> None:
    class CommandServiceStub:
        def run_object(self, **kwargs: object) -> CommandResult:
            return CommandResult(
                object_name=str(kwargs["object_name"]),
                return_code=0,
                stdout="\n".join(
                    [
                        "player=chromium.instance2147458",
                        "status=Playing",
                        "title=Current title",
                        "artist=Current artist",
                        "album=Current album",
                        "track_url=https://tidal.com/track/5526443",
                        "tidal_track_id=5526443",
                        "liked=true",
                    ]
                ),
                stderr="",
                command=str(kwargs["command"]),
            )

    tidal_api_service = TidalApiPlaybackServiceStub({"tidal_api_request": {}})
    service = WebPlayerService(
        command_service=CommandServiceStub(),
        tidal_api_service=tidal_api_service,
        browser_backend_enabled=True,
    )

    payload = service.dispatch_object(
        object_name="webplayer_toggle_like_current",
        player_selector="chromium",
        arguments={"playback_backend": "browser"},
    )

    assert payload["ok"] is True
    assert payload["backend"] == "browser"
    assert payload["playback_backend"] == "browser"
    assert payload["controller"] == "wplctl"
    assert payload["now_playing"]["tidal_track_id"] == "5526443"
    assert tidal_api_service.request_log == []


def test_webplayer_browser_backend_flag_does_not_change_public_schema() -> None:
    service = WebPlayerMcpRequestService(
        player_service=WebPlayerService(browser_backend_enabled=True),
    )
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])
    play_tool = next(item for item in tools if (item.get("function") or {}).get("name") == "webplayer_play")
    properties = ((play_tool.get("function") or {}).get("parameters") or {}).get("properties") or {}

    assert properties["playback_backend"]["enum"] == ["browser"]
    assert properties["playback_backend"]["default"] == "browser"
    assert properties["player_selector"]["enum"] == ["chromium"]
    assert properties["player_selector"]["default"] == "chromium"
    assert "cdp_port" not in properties
    assert "webplayer_pause" in {
        (item.get("function") or {}).get("name")
        for item in tools
    }
    assert {"webplayer_get_media_product", "webplayer_get_next_media_product", "webplayer_seek"}.issubset(
        {
            (item.get("function") or {}).get("name")
            for item in tools
        }
    )


def test_browser_library_play_uses_explicit_track_queue() -> None:
    service = WebPlayerService()

    command = service.load_object_command(
        object_name="webplayer_library_play",
        query=None,
        player_selector="chromium",
        arguments={"track_ids": ["107299220", "107299221", "107299220"]},
    )

    assert "https://listen.tidal.com/track/107299220" in command
    assert "CDP_ACTION=track" in command
    assert "CDP_QUERY=107299220" in command
    assert "track-row-play-button:" in command
    assert "favorites/tracks" not in command
    assert service._browser_queue_track_ids == ["107299220", "107299221"]
    assert service._browser_queue_index == 0

    next_command = service.load_object_command(
        object_name="webplayer_forward",
        query=None,
        player_selector="chromium",
        arguments={},
    )
    assert "https://listen.tidal.com/track/107299221" in next_command


def test_browser_pause_command_uses_pause_without_stopping_player() -> None:
    command = WebPlayerService()._load_pause_command(player_selector="chromium")

    assert 'playerctl -p "$selected_player" pause' in command
    assert 'playerctl -p "$selected_player" stop' not in command


def test_browser_seek_command_supports_absolute_and_relative_positions() -> None:
    service = WebPlayerService()

    absolute_command = service.load_object_command(
        object_name="webplayer_seek",
        query=None,
        player_selector="chromium",
        arguments={"position_seconds": 42.5},
    )
    relative_command = service.load_object_command(
        object_name="webplayer_seek",
        query=None,
        player_selector="chromium",
        arguments={"delta_seconds": -5},
    )

    assert 'playerctl -p "$selected_player" position 42.5' in absolute_command
    assert "position 5-" in relative_command
    assert "error=seek_failed" in absolute_command


def test_browser_seek_rejects_missing_and_invalid_positions() -> None:
    service = WebPlayerService()

    for arguments, expected_error in (
        ({}, "missing_seek_position"),
        ({"position_seconds": -1}, "invalid_seek_position"),
        ({"delta_seconds": 0}, "invalid_seek_position"),
        ({"position_seconds": "nan"}, "invalid_seek_position"),
    ):
        payload = service.dispatch_object(
            object_name="webplayer_seek",
            player_selector="chromium",
            arguments=arguments,
        )
        assert payload["ok"] is False
        assert payload["error"] == f"browser_backend_failed:{expected_error}"


def test_media_product_command_skips_deep_cdp_probe_by_default() -> None:
    service = WebPlayerService()

    media_product_command = service.load_object_command(
        object_name="webplayer_get_media_product",
        query=None,
        player_selector="chromium",
        arguments={"playback_backend": "browser"},
    )
    assert "CDP_PROBE_TIMEOUT_S" not in media_product_command

    explicit_probe_command = service.load_object_command(
        object_name="webplayer_get_media_product",
        query=None,
        player_selector="chromium",
        arguments={"playback_backend": "browser", "cdp_probe_quality": True},
    )
    assert "CDP_PROBE_TIMEOUT_S" in explicit_probe_command


def test_now_playing_command_skips_cdp_probe_by_default() -> None:
    command = WebPlayerService().load_object_command(
        object_name="webplayer_now_playing",
        query=None,
        player_selector="chromium",
        arguments={},
    )

    assert "CDP_PROBE_TIMEOUT_S" not in command


def test_webplayer_library_snapshot_payload_parses_diagnostics() -> None:
    service = WebPlayerService()
    encoded_track_list = base64.b64encode(
        json.dumps(
            [
                {
                    "tidal_track_id": "52044857",
                    "title": "Exiled Archangels",
                    "artist": "Rotting Christ",
                    "liked": True,
                }
            ]
        ).encode("utf-8")
    ).decode("ascii")

    stdout_payload = "\n".join(
        [
            "snapshot_source_url=https://listen.tidal.com/browse/favorites/tracks",
            "snapshot_anchor_count=3",
            "snapshot_data_node_count=42",
            "snapshot_page_title=My Collection",
            "snapshot_ready_state=complete",
            "snapshot_login_hint=false",
            "snapshot_shell_fallback_hint=false",
            "snapshot_recovery_attempted=true",
            "snapshot_recovery_succeeded=false",
            f"snapshot_json_base64={encoded_track_list}",
        ]
    )

    payload = service.library_snapshot_plugin.load_result_payload(stdout_payload)

    assert payload["snapshot_ok"] is True
    assert payload["snapshot_count"] == 1
    assert payload["tracks"][0]["tidal_track_id"] == "52044857"
    assert payload["snapshot_source_url"] == "https://listen.tidal.com/browse/favorites/tracks"
    assert payload["snapshot_anchor_count"] == 3
    assert payload["snapshot_data_node_count"] == 42
    assert payload["snapshot_page_title"] == "My Collection"
    assert payload["snapshot_ready_state"] == "complete"
    assert payload["snapshot_login_hint"] is False
    assert payload["snapshot_shell_fallback_hint"] is False
    assert payload["snapshot_recovery_attempted"] is True
    assert payload["snapshot_recovery_succeeded"] is False


def test_webplayer_library_snapshot_payload_classifies_tidal_shell_without_hint() -> None:
    service = WebPlayerService()
    stdout_payload = "\n".join(
        [
            "snapshot_source_url=https://tidal.com/collection/tracks",
            "snapshot_anchor_count=0",
            "snapshot_data_node_count=0",
            "snapshot_login_hint=false",
            "snapshot_shell_fallback_hint=false",
            "snapshot_recovery_attempted=true",
            "snapshot_recovery_succeeded=false",
            "snapshot_json_base64=W10=",
        ]
    )

    payload = service.library_snapshot_plugin.load_result_payload(stdout_payload)

    assert payload["snapshot_ok"] is False
    assert payload["snapshot_count"] == 0
    assert payload["snapshot_error_class"] == "shell_fallback_unextractable"


def test_initialize_protocol_version() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "initialize", "params": {}})
    assert payload.get("result", {}).get("protocolVersion") == "2025-03-26"
    capabilities = payload.get("result", {}).get("capabilities", {})
    assert "tools" in capabilities
    assert "prompts" in capabilities


def test_initialize_negotiates_requested_supported_protocol_version() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "initialize",
            "params": {"protocolVersion": "2026-07-28"},
        }
    )

    assert payload.get("result", {}).get("protocolVersion") == "2026-07-28"


def test_prompts_list_contains_webplayer_operator() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "prompts/list", "params": {}})
    prompts = payload.get("result", {}).get("prompts", [])
    names = {item.get("name") for item in prompts}
    assert "webplayer_operator" in names


def test_prompts_get_returns_text_message() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "webplayer_operator",
                "arguments": {
                    "player_selector": "chromium",
                    "search_query": "meshuggah",
                },
            },
        }
    )
    messages = payload.get("result", {}).get("messages", [])
    assert len(messages) == 1
    text = ((messages[0] or {}).get("content") or {}).get("text", "")
    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "webplayer_library_play" in text
    assert "webplayer_open_playback_target" in text
    assert "webplayer_pause" in text
    assert "webplayer_seek" in text
    assert "webplayer_volume_set" in text
    assert "webplayer_volume_adjust" in text
    assert "tidal_api_request" in text
    assert "tidal_api_search" in text
    assert "meshuggah" in text


def test_strategy_overview_mentions_current_playback_tools() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "method": "prompts/get",
            "params": {
                "name": "webplayer_strategy_overview",
                "arguments": {"player_selector": "chromium"},
            },
        }
    )

    messages = payload.get("result", {}).get("messages", [])
    assert len(messages) == 1
    text = ((messages[0] or {}).get("content") or {}).get("text", "")
    assert "webplayer_search_play" in text
    assert "webplayer_playlist_play" in text
    assert "webplayer_library_play" in text
    assert "webplayer_get_media_product" in text
    assert "webplayer_get_next_media_product" in text
    assert "webplayer_pause" in text
    assert "webplayer_seek" in text
    assert "tidal_api_track_manifest" in text


def test_jsonrpc_server_discover_returns_supported_versions() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {},
        }
    )

    assert payload.get("jsonrpc") == "2.0"
    assert payload.get("id") == 1
    result = payload.get("result") or {}
    assert result.get("resultType") == "complete"
    supported_versions = result.get("supportedVersions") or []
    assert "2026-07-28" in supported_versions
    assert "2025-03-26" in supported_versions


def test_jsonrpc_initialize_enables_mcp_app_ui_by_default() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "initialize",
            "params": {},
        }
    )

    capabilities = (payload.get("result") or {}).get("capabilities") or {}
    assert "resources" in capabilities
    assert capabilities["extensions"]["io.modelcontextprotocol/ui"]["resourceUri"] == service._UI_RESOURCE_URI


def test_jsonrpc_initialize_can_disable_mcp_app_ui_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("WEBPLAYER_MCP_UI_EXTENSION_DEFAULT", "0")
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "initialize",
            "params": {},
        }
    )

    capabilities = (payload.get("result") or {}).get("capabilities") or {}
    assert "extensions" not in capabilities


def test_jsonrpc_tools_list_returns_mcp_tool_schema() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
    )

    result = payload.get("result") or {}
    assert result.get("resultType") == "complete"
    tools = result.get("tools") or []
    webplayer_play_tools = [tool for tool in tools if tool.get("name") == "webplayer_play"]
    assert webplayer_play_tools
    webplayer_play_tool = webplayer_play_tools[0]
    assert isinstance(webplayer_play_tool.get("inputSchema"), dict)
    assert "function" not in webplayer_play_tool


def test_webplayer_tool_schema_uses_browser_backend() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object({"method": "tools/list", "params": {}})
    tools = payload.get("result", {}).get("tools", [])
    play_tool = next(item for item in tools if (item.get("function") or {}).get("name") == "webplayer_play")
    properties = ((play_tool.get("function") or {}).get("parameters") or {}).get("properties") or {}
    tool_names = {
        (item.get("function") or {}).get("name")
        for item in tools
    }

    assert properties["playback_backend"]["enum"] == ["browser"]
    assert properties["playback_backend"]["default"] == "browser"
    assert properties["player_selector"]["enum"] == ["chromium"]
    assert properties["player_selector"]["default"] == "chromium"
    assert "cdp_autoclick" not in properties
    assert "cdp_port" not in properties
    assert {"webplayer_pause", "webplayer_get_media_product", "webplayer_get_next_media_product", "webplayer_seek"}.issubset(tool_names)


def test_jsonrpc_tools_call_wraps_structured_content_and_error_state() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "webplayer_search",
                "arguments": {},
            },
        }
    )

    result = payload.get("result") or {}
    assert result.get("resultType") == "complete"
    assert result.get("isError") is True
    content = result.get("content") or []
    assert isinstance(content, list)
    assert content
    assert content[0].get("type") == "text"
    structured_content = result.get("structuredContent") or {}
    assert structured_content.get("ok") is False
    assert "missing_query" in str(structured_content.get("stdout") or "")


def test_jsonrpc_unknown_method_returns_method_not_found_error() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 404,
            "method": "unknown/method",
            "params": {},
        }
    )

    assert payload.get("jsonrpc") == "2.0"
    assert payload.get("id") == 404
    error_payload = payload.get("error") or {}
    assert error_payload.get("code") == -32601


def test_jsonrpc_initialize_advertises_resources_for_ui_capability() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "initialize",
            "params": {
                "capabilities": {
                    "extensions": {
                        "io.modelcontextprotocol/ui": {
                            "mimeTypes": ["text/html;profile=mcp-app"],
                        },
                    }
                }
            },
        }
    )

    result = payload.get("result") or {}
    capabilities = result.get("capabilities") or {}
    assert "resources" in capabilities
    assert "extensions" in capabilities
    assert "io.modelcontextprotocol/ui" in capabilities["extensions"]


def test_jsonrpc_initialize_keeps_text_fallback_for_unsupported_mime_type() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "initialize",
            "params": {
                "capabilities": {
                    "extensions": {
                        "io.modelcontextprotocol/ui": {
                            "mimeTypes": [],
                        },
                    }
                }
            },
        }
    )

    capabilities = (payload.get("result") or {}).get("capabilities") or {}
    assert "resources" in capabilities
    tools_payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/list",
            "params": {},
        }
    )
    tools = (tools_payload.get("result") or {}).get("tools") or []
    webplayer_play_tool = next(tool for tool in tools if tool.get("name") == "webplayer_play")
    assert ((webplayer_play_tool.get("_meta") or {}).get("ui") or {}).get("resourceUri") == service._UI_RESOURCE_URI


def test_jsonrpc_ui_capability_persists_for_session_follow_up_requests() -> None:
    service = WebPlayerMcpRequestService()
    request_headers = {"MCP-Session-Id": "test-ui-session"}
    initialize_payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "initialize",
            "params": {
                "capabilities": {
                    "extensions": {
                        "io.modelcontextprotocol/ui": {
                            "mimeTypes": ["text/html;profile=mcp-app"],
                        },
                    }
                }
            },
        },
        request_headers=request_headers,
    )
    assert "resources" in ((initialize_payload.get("result") or {}).get("capabilities") or {})

    tools_payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/list",
            "params": {},
        },
        request_headers=request_headers,
    )
    tools = (tools_payload.get("result") or {}).get("tools") or []
    webplayer_play_tool = next(tool for tool in tools if tool.get("name") == "webplayer_play")
    assert ((webplayer_play_tool.get("_meta") or {}).get("ui") or {}).get("resourceUri") == service._UI_RESOURCE_URI

    resources_payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 19,
            "method": "resources/read",
            "params": {"uri": service._UI_RESOURCE_URI},
        },
        request_headers=request_headers,
    )
    contents = (resources_payload.get("result") or {}).get("contents") or []
    assert contents
    assert contents[0].get("mimeType") == "text/html;profile=mcp-app"


def test_jsonrpc_tools_list_embeds_ui_resource_meta_for_webplayer_tools() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/extensions": ["io.modelcontextprotocol/ui"],
                }
            },
        }
    )

    tools = (payload.get("result") or {}).get("tools") or []
    webplayer_play_tool = next((tool for tool in tools if tool.get("name") == "webplayer_play"), {})
    webplayer_scope_tool = next(
        (tool for tool in tools if tool.get("name") == "webplayer_playback_scope"),
        {},
    )
    tidal_request_tool = next((tool for tool in tools if tool.get("name") == "tidal_api_request"), {})

    assert ((webplayer_play_tool.get("_meta") or {}).get("ui") or {}).get("resourceUri") == service._UI_RESOURCE_URI
    scope_ui_meta = (webplayer_scope_tool.get("_meta") or {}).get("ui") or {}
    assert scope_ui_meta.get("resourceUri") == service._UI_RESOURCE_URI
    assert scope_ui_meta.get("visibility") == ["app"]
    assert "_meta" not in tidal_request_tool


def test_jsonrpc_resources_list_and_read_return_ui_resource() -> None:
    service = WebPlayerMcpRequestService()
    resources_list_payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "resources/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/extensions": "io.modelcontextprotocol/ui",
                }
            },
        }
    )

    resources = (resources_list_payload.get("result") or {}).get("resources") or []
    assert resources
    assert resources[0].get("uri") == service._UI_RESOURCE_URI
    assert service._UI_APP_NAME == "Mpc.App.ui"
    assert service._UI_RESOURCE_URI == "ui://Mpc.App.ui"
    assert resources[0].get("name") == "Mpc.App.ui"

    resources_read_payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "resources/read",
            "params": {
                "uri": service._UI_RESOURCE_URI,
                "_meta": {
                    "io.modelcontextprotocol/extensions": "io.modelcontextprotocol/ui",
                },
            },
        }
    )

    contents = (resources_read_payload.get("result") or {}).get("contents") or []
    assert contents
    assert contents[0].get("mimeType") == "text/html;profile=mcp-app"
    ui_meta = (contents[0].get("_meta") or {}).get("ui") or {}
    csp_meta = ui_meta.get("csp") or {}
    connect_domains = csp_meta.get("connectDomains", [])
    assert "http://192.168.0.47:8766" in connect_domains
    assert "http://192.168.0.48:8766" in connect_domains
    assert "http://localhost:8766" in connect_domains
    assert "http://127.0.0.1:8766" in connect_domains
    assert "https://resources.tidal.com" in csp_meta.get("resourceDomains", [])
    assert "https://cdn.jsdelivr.net" in csp_meta.get("resourceDomains", [])
    assert "frameDomains" not in csp_meta
    content_text = str(contents[0].get("text") or "")
    assert "tidal-embed" not in content_text
    assert "embed.tidal.com" not in content_text
    assert "TIDAL_EMBED_ORIGIN" not in content_text
    assert "renderTidalEmbed" not in content_text
    assert "webplayer_backward" in content_text
    assert "webplayer_volume_adjust" in content_text
    assert "data-toggle=\"play-pause\"" in content_text
    assert "data-stop-tool=\"webplayer_pause\"" in content_text
    assert "data-icon=\"skip-previous\"" in content_text
    assert "data-icon=\"play-stop\"" in content_text
    assert "data-icon=\"skip-next\"" in content_text
    assert 'data-icon=\"volume-down\"' in content_text
    assert 'data-icon=\"volume-up\"' in content_text
    assert 'class=\"like-button\"' in content_text
    like_markup_start = content_text.index('<fluent-button class=\"like-button\"')
    title_row_start = content_text.index('<div class=\"header-track-title-row\">')
    title_row_end = content_text.index('</div>', title_row_start)
    track_status_start = content_text.index('<div class=\"track-status-row\">')
    track_status_values_start = content_text.index('<div class=\"track-status-values\">')
    track_countdown_start = content_text.index('id=\"track-countdown\"')
    assert title_row_start < title_row_end < track_status_start < like_markup_start < track_status_values_start < track_countdown_start
    assert 'title-like-separator' not in content_text
    assert 'track-divider' not in content_text
    assert 'id=\"header-dsp-separator\"' in content_text
    assert 'header-volume-separator' not in content_text
    assert 'header-quality-separator' not in content_text
    assert 'header-explicit-separator' not in content_text
    assert 'header-audio-interface-separator' not in content_text
    assert '.now-playing-eyebrow::after' not in content_text
    assert "const audioQaValues" not in content_text
    assert 'backendValues.join(" / ")' not in content_text
    assert 'routeParts.join(" / ")' not in content_text
    assert 'controlsEl.appendChild(likeButtonEl)' not in content_text
    assert content_text.index('data-icon=\"skip-previous\"') < content_text.index('data-icon=\"play-stop\"')
    assert content_text.index('data-icon=\"play-stop\"') < content_text.index('data-icon=\"skip-next\"')
    assert '.controls [data-icon=\"volume-down\"]' in content_text
    assert '.controls [data-icon=\"volume-up\"]' in content_text
    assert 'id=\"track-meta\" class=\"track-meta quality-side\">' in content_text
    assert 'flex-direction: row;' in content_text
    assert '.track-status-row .like-button' in content_text
    assert '.track-status-row .like-button svg' in content_text
    assert 'grid-template-areas:' in content_text
    assert '"like values"' in content_text
    assert '"meta meta"' in content_text
    assert '.track-status-row .track-meta' in content_text
    volume_button_end = content_text.index('</button>', content_text.index('data-icon=\"volume-up\"'))
    volume_text_start = content_text.index('<span id=\"header-volume\" class=\"volume-text\"')
    assert volume_button_end < volume_text_start
    track_meta_start = content_text.index('<div id=\"track-meta\" class=\"track-meta quality-side\">')
    track_meta_end = content_text.index('</div>', track_meta_start)
    assert track_meta_end < volume_text_start
    assert 'height: 32px;' in content_text
    assert 'top: 50%;' in content_text
    assert 'transform: translateY(-50%);' in content_text
    assert 'right: calc(100% + 4px);' in content_text
    assert 'left: calc(100% + 4px);' in content_text
    assert 'id=\"system-volume-slider\"' not in content_text
    assert "background: transparent;" in content_text
    assert "PLAY_ICON_SVG" in content_text
    assert "PAUSE_ICON_SVG" in content_text
    assert "marquee-line-icons" not in content_text
    assert "marquee-line-icon" not in content_text
    assert "onlyWhenChanged: true" in content_text
    assert "renderPlayPauseButton" in content_text
    assert 'id=\"status-pill\"' not in content_text
    assert "now-playing-eyebrow" in content_text
    assert "now-playing-title-link" not in content_text
    assert 'id=\"header-track-title\"' in content_text
    assert 'id=\"header-track-artist\"' in content_text
    assert 'id=\"header-track-album\"' in content_text
    assert 'id=\"header-track-links\"' not in content_text
    assert "headerTrackTitleEl.textContent" in content_text
    assert "headerTrackArtistEl.textContent" in content_text
    assert "headerTrackAlbumEl.textContent" in content_text
    assert "headerTrackTitleEl.innerHTML = loadEmbeddedLinkHtml" in content_text
    assert "headerTrackArtistEl.innerHTML = loadEmbeddedLinkHtml" in content_text
    assert "headerTrackAlbumEl.innerHTML = loadEmbeddedLinkHtml" in content_text
    assert "headerTrackLinksEl" not in content_text
    assert "const loadTidalUrl = (value) =>" in content_text
    assert "const loadTidalResourceUrl = (resourceValue, resourceType) =>" in content_text
    assert "const loadEmbeddedLinkHtml = (label, href" in content_text
    assert "const loadTidalEmbedUrl = (value, resourceType) =>" not in content_text
    assert "tidal-content-embeds" not in content_text
    assert "<iframe" not in content_text
    assert 'data-open-link=\"true\"' in content_text
    assert "app.openLink({ url: href })" in content_text
    assert 'type: \"webplayer-open-link\"' in content_text
    assert "artistUrl: loadTidalResourceUrl(artistUrlValue, \"artist\")" in content_text
    assert "albumUrl: loadTidalResourceUrl(albumUrlValue, \"album\")" in content_text
    assert "creditsUrl" not in content_text
    assert "track-credit-link" not in content_text
    track_info_start = content_text.index('<div class=\"header-track-info\">')
    track_status_start = content_text.index('<div class=\"track-status-row\">')
    like_button_start = content_text.index('<fluent-button class=\"like-button\"')
    track_status_values_start = content_text.index('<div class=\"track-status-values\">')
    track_countdown_start = content_text.index('id=\"track-countdown\"')
    track_meta_start = content_text.index('<div id=\"track-meta\" class=\"track-meta quality-side\">')
    header_quality_start = content_text.index('id=\"header-quality\"')
    assert track_info_start < track_status_start < like_button_start < track_status_values_start < track_countdown_start < track_meta_start < header_quality_start
    assert "grid-template-columns: 85px minmax(0, 1fr) auto;" in content_text
    assert "width: 85px;" in content_text
    assert '.panel.cover-expanded' in content_text
    assert 'grid-template-columns: minmax(180px, 320px) minmax(0, 1fr) auto;' in content_text
    assert 'role=\"button\" tabindex=\"0\" aria-expanded=\"false\"' in content_text
    assert 'id=\"cover-preview\"' in content_text
    assert 'const coverShellEl = document.querySelector(\".cover-shell\")' in content_text
    assert 'const scheduleCoverExpansion = () =>' in content_text
    assert 'coverShellEl?.addEventListener(\"click\", scheduleCoverExpansion)' in content_text
    assert 'coverShellEl?.addEventListener(\"dblclick\", () =>' in content_text
    assert 'toggleCoverExpanded();' in content_text
    assert 'event.key === \"Escape\"' in content_text
    assert "quality-text" in content_text
    assert 'id=\"header-quality\" class=\"quality-text\"' in content_text
    assert '.quality-text[data-quality-level=\"hi-res\"]' in content_text
    assert 'color: #f59e0b;' in content_text
    assert '.quality-text[data-quality-level=\"lossless\"]' in content_text
    assert 'color: #22c55e;' in content_text
    assert '.quality-text[data-quality-level=\"low\"]' in content_text
    assert 'color: #000000;' in content_text
    assert 'id=\"header-volume\" class=\"volume-text\"' in content_text
    assert 'id=\"header-volume-separator\"' not in content_text
    assert "formatVolumeDbText(view.volumePercent)" in content_text
    assert 'return "-0 dB";' in content_text
    assert 'return "-80 dB";' in content_text
    assert "return `-${attenuationDb} dB`;" in content_text
    assert "width: 85px;" in content_text
    assert "text-align: right;" in content_text
    assert "--wp-value-gap: 6px;" in content_text
    assert ".track-status-values" in content_text
    assert ".track-status-values" in content_text and "gap: var(--wp-value-gap);" in content_text
    assert ".track-meta" in content_text
    assert content_text.index('id=\"track-countdown\"') < content_text.index('id=\"header-volume\"')
    assert content_text.index('id=\"header-quality\"') < content_text.index('id=\"header-volume\"')
    assert 'const headerText = volumeText === "-" ? "" : volumeText' in content_text
    assert 'id=\"track-meta\" class=\"track-meta quality-side\"' in content_text
    assert 'id=\"track-countdown\"' in content_text
    assert 'id=\"track-progress\" class=\"track-progress\" type=\"range\"' in content_text
    assert 'id=\"track-position\" class=\"progress-time\"' in content_text
    assert 'id=\"track-duration\" class=\"progress-time\"' in content_text
    assert "nowPayload.duration_seconds" in content_text
    assert "nowPayload.position_seconds" in content_text
    assert "mediaProductValue.position_seconds" in content_text
    assert 'trackProgressEl?.addEventListener("change"' in content_text
    assert 'callTool("webplayer_seek", { position_seconds: positionSeconds }' in content_text
    assert 'trackProgressEl.setAttribute("aria-valuetext"' in content_text
    assert "appearance: none;" in content_text
    assert "::-webkit-slider-runnable-track" in content_text
    assert "::-webkit-slider-thumb" in content_text
    assert "::-moz-range-track" in content_text
    assert "::-moz-range-progress" in content_text
    assert "::-moz-range-thumb" in content_text
    assert "linear-gradient(" in content_text
    progress_css = content_text.split(".track-progress {", 1)[1].split(".track-meta {", 1)[0]
    assert "var(--wp-accent)" not in progress_css
    assert "var(--wp-accent-soft)" not in progress_css
    assert progress_css.count("var(--wp-text)") == 4
    assert progress_css.count("var(--wp-neutral)") == 2
    assert 'trackProgressEl.style.setProperty("--wp-progress", `${progressPercent}%`)' in content_text
    assert '.progress-row' in content_text
    assert '.playback-control-stack' in content_text
    controls_markup_start = content_text.index('<div class=\"controls\" role=\"toolbar\"')
    progress_markup_start = content_text.index('<div class=\"progress-row\" aria-label=\"Track progress\">')
    control_stack_start = content_text.rindex('<div class=\"playback-control-stack\">', 0, controls_markup_start)
    assert control_stack_start < controls_markup_start < progress_markup_start
    assert 'top: calc(var(--mcp-safe-area-top, 0px) + 34.5px);' in content_text
    assert 'class=\"track-divider\"' not in content_text
    assert 'id=\"header-dsp-separator\"' in content_text
    assert 'id=\"header-explicit-separator\"' not in content_text
    assert 'id=\"header-explicit\"' not in content_text
    assert 'id=\"header-quality\"' in content_text
    assert "position: absolute;" in content_text
    assert "left: 50%;" in content_text
    assert "transform: translateX(-50%);" in content_text
    assert 'id=\"header-info-toggle\"' not in content_text
    assert "toggleInfoDetails" not in content_text
    assert "data-info-toggle" not in content_text
    assert "info-details" not in content_text
    assert 'panelEl.style.width = "100%"' in content_text
    assert 'panelEl.style.maxWidth = "100%"' in content_text
    assert 'headerQualityEl.textContent = qualityText' in content_text
    assert 'formatMetricText(view.sampleRateKhz, " kHz")' not in content_text
    assert 'formatMetricText(view.bitDepth, "-bit")' not in content_text
    assert "audioQaText" not in content_text
    assert 'return qualityLabel || qualityText || "-";' in content_text
    assert 'const qualityLevel = loadQualityLevel(qualityText);' in content_text
    assert 'const audioQaChip = qualityText !== "-"' in content_text
    assert '&& qualityLevel !== "hi-res"' in content_text
    assert '&& qualityLevel !== "lossless"' in content_text
    assert '${[toneChip, audioQaChip].filter(Boolean).join(\"\")}' in content_text
    assert 'headerQualityEl.setAttribute("data-quality-level", qualityLevel)' in content_text
    assert 'trackMetaEl.hidden = qualityText === "-"' in content_text
    assert 'justify-content: center;' in content_text
    assert 'border: none !important;' in content_text
    assert 'box-shadow: none !important;' in content_text
    assert "formatCountdownText" in content_text
    assert "window.setInterval(renderTrackCountdown, 1000)" in content_text
    assert "meta-chips" in content_text
    assert "info-summary" in content_text
    assert 'id=\"artist-details\" class=\"details-grid artist-details\"' in content_text
    assert '.panel.cover-expanded .artist-details' in content_text
    assert 'const artistDetailHtml = [' in content_text
    assert 'loadDetailHtml("Artist details", view.artistInfo)' in content_text
    assert "systemDetailHtml" not in content_text
    assert "systemDetailContentHtml" not in content_text
    assert 'class="info-document"' not in content_text
    assert 'grid-template-areas:' in content_text
    assert '"cover track controls"' in content_text
    assert '"cover audio-pipe brand"' in content_text
    assert '"details details details"' in content_text
    assert "info-expanded" not in content_text
    assert 'class="details-cover" data-details-cover' not in content_text
    assert 'loadChipHtml("Time", formatDurationText(view.duration)' not in content_text
    assert "loadQualityDisplayText" in content_text
    assert 'return qualityLabel || qualityText || "-";' in content_text
    assert "audioQaText" not in content_text
    assert 'loadChipHtml("Quality", loadQualityDisplayText(view)' not in content_text
    assert 'loadChipHtml("Tone", formatField(view.tonart)' in content_text
    assert 'loadInlineMetaHtml("Audio QA", qualityText, "accent")' in content_text
    assert 'class="meta-inline"' in content_text
    assert '.meta-inline[data-variant="accent"]' in content_text
    assert 'color: var(--wp-accent);' in content_text
    assert 'loadChipHtml("Explicit"' not in content_text
    assert "details-grid" in content_text
    assert "loadTrackSummaryText" in content_text
    assert "loadResourceListSummaryText" in content_text
    assert "loadPlaybackContextText" in content_text
    assert 'formatMetricText(view.bitrateKbps, " kbps")' not in content_text
    assert "Waiting-Queue:" not in content_text
    assert "Radio:" not in content_text
    assert "<fluent-button" in content_text
    assert "Track/Titel:" not in content_text
    assert "Kuenstler:" not in content_text
    assert "HEART_OUTLINE_ICON_SVG" in content_text
    assert "HEART_FILLED_ICON_SVG" in content_text
    assert "renderHeartButton" in content_text
    assert "data-like-button" in content_text
    assert 'querySelector("fluent-button[data-like-button]")' in content_text
    assert 'data-liked", isLiked ? "true" : "false"' in content_text
    assert 'aria-pressed", isLiked ? "true" : "false"' in content_text
    assert "bindMediaSessionHandlers" in content_text
    assert "mediaSession.setActionHandler" in content_text
    assert "updateMediaSession" in content_text
    assert "clearMediaSession" in content_text
    assert "tidal_api_add_track_to_collection" in content_text
    assert "webplayer_toggle_like_current" not in content_text
    assert "webplayer_search_play" in content_text
    assert "webplayer_usrauth" in content_text
    assert "search-input" not in content_text
    assert "search-row" not in content_text
    assert 'id="search-button"' not in content_text
    assert "header-actions" in content_text
    assert 'id=\"auth-button\"' not in content_text
    assert 'loadChipHtml("Status"' not in content_text
    assert 'loadDetailHtml("Media product"' not in content_text
    assert 'loadDetailHtml("Stream quality"' not in content_text
    assert 'loadDetailHtml("Tone"' not in content_text
    assert "auth-callback-input" in content_text
    assert 'href=\"https://tidal.com\"' not in content_text
    assert "brand-wordmark" not in content_text
    assert "brand-context" not in content_text
    assert "aria-label=\"TIDAL WebPlayer\"" not in content_text
    assert "now-playing-link" in content_text
    assert "auth-recover" in content_text
    assert "const toolError = loadToolError(resultPayload, result);" in content_text
    assert "--wp-surface: #0b0d10" in content_text
    assert "background: transparent" in content_text
    assert "background-image: var(--player-background-image, none)" not in content_text
    assert "background-blend-mode: soft-light" not in content_text
    assert "border: none;" in content_text
    assert "const setPlayerBackground = (imageSource)" in content_text
    assert "setPlayerBackground(effectiveCoverSrc)" in content_text
    assert "grid-template-columns: 85px minmax(0, 1fr) auto" in content_text
    assert "width: 85px" in content_text
    assert "overflow-wrap: anywhere" in content_text
    assert "controls fluent-button:hover" in content_text
    assert "<script type=\"module\">" in content_text
    assert content_text.count("<script") == content_text.count("</script>")
    assert "callDirectMcpTool" in content_text
    assert 'const MCP_PROTOCOL_VERSION = "2026-07-28";' in content_text
    assert '"MCP-Protocol-Version": MCP_PROTOCOL_VERSION' in content_text
    assert '"Mcp-Method": "tools/call"' in content_text
    assert '"Mcp-Name": name' in content_text
    assert '"io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION' in content_text
    assert "if (isEmbedded)" in content_text
    assert 'if (isEmbedded && app && typeof app.connect === "function")' in content_text
    assert "if (isEmbedded || vscodeApi)" not in content_text
    assert 'const selectedPlaybackBackend = "browser";' in content_text
    assert 'const DEFAULT_TOOL_ARGUMENTS = {"playback_backend":"browser","player_selector":"chromium"};' in content_text
    assert 'callServerToolRaw(name, effectiveArgs)' in content_text
    assert "playerctl" not in content_text
    assert "cvlc" not in content_text
    assert "https://unpkg.com/@fluentui/web-components" not in content_text


def test_ui_resource_uses_browser_defaults_even_with_legacy_browser_service() -> None:
    service = WebPlayerMcpRequestService(
        player_service=WebPlayerService(browser_backend_enabled=True),
    )

    content_text = service._load_ui_resource_markup()

    assert 'const DEFAULT_TOOL_ARGUMENTS = {"playback_backend":"browser","player_selector":"chromium"};' in content_text
    assert 'const selectedPlaybackBackend = "browser";' in content_text
    assert 'id="backend-select"' not in content_text
    assert 'value="sdk">Remote WebPlayer (SDK)</option>' not in content_text
    assert 'value="api">TIDAL API</option>' not in content_text
    assert 'const BACKEND_STORAGE_KEY = "tidal-webplayer-playback-backend";' not in content_text
    assert 'arguments: args' in content_text
    assert 'data-play-args=\'{"playback_backend":"browser","player_selector":"chromium"}\'' in content_text
    assert 'data-stop-tool="webplayer_pause"' in content_text
    assert 'data-tool="webplayer_search_play"' not in content_text
    assert 'const trackUrl = String(view.trackUrl || "").trim();' not in content_text
    assert 'const playToolName = trackUrl ? "webplayer_open_playback_target" : basePlayToolName;' not in content_text
    assert 'target_url: trackUrl' not in content_text


def test_ui_resource_has_working_like_and_api_playlist_controls() -> None:
    service = WebPlayerMcpRequestService()
    content_text = service._load_ui_resource_markup()

    assert "[API] Add current track to tracks" in content_text
    assert "window.prompt" not in content_text
    assert "event.composedPath" in content_text
    assert 'tidal_api_add_track_to_collection' in content_text
    assert 'controlsEl.addEventListener("click"' in content_text
    like_command = WebPlayerService().load_object_command(
        object_name="webplayer_like_current",
        query=None,
        player_selector="chromium",
        arguments={},
    )
    assert "collection" in like_command
    assert WebPlayerService._LIBRARY_SECTION_URLS["my_collection_tracks"] == "https://tidal.com/my-collection/tracks"


def test_ui_resource_omits_catalog_search_controls() -> None:
    service = WebPlayerMcpRequestService()
    content_text = service._load_ui_resource_markup()

    assert 'id="catalog-box"' not in content_text
    assert 'id="catalog-search-form"' not in content_text
    assert 'id="catalog-query"' not in content_text
    assert 'id="catalog-playlist-id"' not in content_text
    assert 'id="catalog-results"' not in content_text
    assert 'const selectedPlaybackBackend = "browser";' in content_text


def test_browser_like_commands_preserve_cdp_like_state() -> None:
    service = WebPlayerService()

    like_command = service.load_object_command(
        object_name="webplayer_like_current",
        query=None,
        player_selector="chromium",
        arguments={},
    )
    toggle_command = service.load_object_command(
        object_name="webplayer_toggle_like_current",
        query=None,
        player_selector="chromium",
        arguments={},
    )

    assert "cdp_like_output=$(" in like_command
    assert "cdp_like_state=(liked|already_liked)$" in like_command
    assert "cdp_like_state=unliked$" in toggle_command


def test_ui_resource_uses_app_host_bridge_in_vs_code() -> None:
    service = WebPlayerMcpRequestService()

    content_text = service._load_ui_resource_markup()

    assert 'new appModule.App({ name: "Mpc.App.ui", version: "1.0.0" });' in content_text
    assert "app.callServerTool" in content_text
    assert "app.connect()" in content_text
    assert "app.getHostContext()" in content_text
    assert "app.ontoolresult" in content_text
    assert "app.onhostcontextchanged" in content_text
    assert "appHostConnected" in content_text
    assert "const callServerToolRaw = async (name, args)" in content_text
    assert "return callDirectMcpTool(name, args);" in content_text
    assert "const applyHostContext = (ctx) =>" in content_text
    assert "ctx.styles.variables" in content_text
    assert "ctx.safeAreaInsets" in content_text
    assert "const startPolling = () =>" in content_text
    assert "window.clearInterval(refreshIntervalId);" in content_text
    assert "app.onteardown = async () =>" in content_text
    assert "window.mcp.callTool" not in content_text


def test_ui_resource_uses_api_metadata_and_stable_cover_source() -> None:
    service = WebPlayerMcpRequestService()

    content_text = service._load_ui_resource_markup()

    assert "const nowPayload = { ...basePayload, ...nestedNowPayload };" in content_text
    assert "nowPayload.stream_quality" in content_text
    assert "nowPayload.audio_mode" in content_text
    assert "nowPayload.quality_resolution" in content_text
    assert "nowPayload.bitrate_kbps" in content_text
    assert "nowPayload.sample_rate_khz" in content_text
    assert "nowPayload.bit_depth" in content_text
    assert "nowPayload.channel_count" in content_text
    assert "nowPayload.codec" in content_text
    assert "nowPayload.dsp" in content_text
    assert "const tidalApiDspValue" in content_text
    assert "dsp: formatField(tidalApiDspValue || audioOutputInfo.dsp)" in content_text
    assert "nowPayload.artist_info" in content_text
    assert "nowPayload.album_info" in content_text
    assert "nowPayload.media_product" in content_text
    assert "nowPayload.next_media_product" in content_text
    assert "nowPayload.playback_context" in content_text
    assert "nowPayload.track_id" in content_text
    assert "nowPayload.duration" in content_text
    assert "nowPayload.volume_percent" in content_text
    assert "nowPayload.explicit" in content_text
    assert "nowPayload.playback_backend" in content_text
    assert "nowPayload.backend" in content_text
    assert "nowPayload.media_tags" in content_text
    assert "loadResourceListSummaryText" in content_text
    assert "loadTrackSummaryText" in content_text
    assert "loadPlaybackContextText" in content_text
    assert "formatMetricText(view.sampleRateKhz, \" kHz\")" not in content_text
    assert "formatMetricText(view.bitrateKbps, \" kbps\")" not in content_text
    assert "formatVolumeDbText(view.volumePercent)" in content_text
    assert "const loadAudioQualityMetrics = (value)" in content_text
    assert "parsedQualityMetrics.sampleRateKhz" in content_text
    assert "parsedQualityMetrics.bitDepth" in content_text
    assert "&source=${encodeURIComponent(coverCandidate)}" in content_text
    assert "coverEl.getAttribute(\"src\") !== effectiveCoverSrc" in content_text
    assert "let lastInfoView = {};" in content_text
    assert "const mergeInfoView = (viewObject) =>" in content_text
    assert "renderPlayPauseButton(view);" in content_text
    assert "updateMediaSession(view);" in content_text
    assert "bindMediaSessionHandlers" in content_text
    assert "window.mcp.callTool" not in content_text
    assert "callToolViaHttp" not in content_text
    assert 'appearance="subtle"' in content_text
    assert 'appearance="outline"' in content_text


def test_ui_resource_keeps_player_and_scope_in_three_to_two_layout() -> None:
    markup = WebPlayerMcpRequestService()._load_ui_resource_markup()

    assert 'class="player-layout"' in markup
    assert "container-name: player-layout;" in markup
    assert "container-type: inline-size;" in markup
    assert 'grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);' in markup
    assert 'grid-template-areas: "player aux-matrix";' in markup
    assert 'grid-area: player;' in markup
    assert 'min-width: 620px' not in markup
    assert 'minmax(250px, 35%)' not in markup
    assert '"aux-matrix aux-matrix aux-matrix"' not in markup
    assert 'grid-template-columns: minmax(0, 1fr);' in markup
    assert '@container player-layout (max-width: 600px)' in markup
    assert 'width: clamp(48px, 16cqi, 85px);' in markup
    assert 'width: clamp(26px, 6cqi, 32px);' in markup
    panel_start = markup.index('<section class="panel">')
    panel_end = markup.index("</section>", panel_start)
    scope_start = markup.index('<aside id="aux-matrix"')
    script_start = markup.index('<script type="module">')
    assert panel_start < panel_end < scope_start < script_start
    assert markup.count('id="aux-matrix"') == 1


def test_ui_resource_exposes_audio_output_metadata() -> None:
    service = WebPlayerMcpRequestService()

    content_text = service._load_ui_resource_markup()

    assert 'id="header-audio-interface"' in content_text
    assert 'id="header-audio-profile"' not in content_text
    assert 'id="header-dsp"' in content_text
    assert 'id="audio-pipe-info" class="audio-pipe-meta"' in content_text
    assert '<span class="audio-pipe-label">Audio Pipe:</span>' in content_text
    assert 'id="aux-matrix" class="aux-matrix"' in content_text
    assert 'aria-label="Two-trace oscilloscope for one pro-audio signal channel"' in content_text
    assert '<details class="scope-settings" data-scope-settings>' in content_text
    assert 'aria-label="Oszilloskop-Einstellungen öffnen"' in content_text
    settings_start = content_text.index('<details class="scope-settings" data-scope-settings>')
    settings_tag_end = content_text.index(">", settings_start)
    settings_end = content_text.index("</details>", settings_start)
    settings_markup = content_text[settings_start:settings_end]
    assert " open" not in content_text[settings_start:settings_tag_end]
    assert 'aria-controls="scope-settings-panel"' in settings_markup
    assert 'id="scope-settings-panel" class="scope-controls scope-settings-panel" data-scope-controls>' in settings_markup
    assert 'aria-label="Kanäle">${sourceHtml}</div>' in settings_markup
    assert "scope-settings-toggle svg" in content_text
    assert "data-scope-controls" in content_text
    assert 'data-scope-action="time-minus"' in content_text
    assert 'data-scope-action="time-plus"' in content_text
    assert "data-scope-timebase aria-label=\"Zeitbasis\"" in content_text
    assert '<span class="scope-control-label">VERTIKAL</span>' in content_text
    assert 'data-scope-action="vertical-minus"' in content_text
    assert 'data-scope-action="vertical-plus"' in content_text
    assert 'data-scope-unit="V"' in content_text
    assert 'data-scope-unit="dB"' in content_text
    assert "data-scope-source=" in content_text
    assert "const SCOPE_SLOT_COUNT = 2;" in content_text
    assert 'const SCOPE_OUTPUT_DISPLAY_CHANNEL_START = 2;' in content_text
    assert 'channels: ["in1", "out1"],' in content_text
    assert '{ label: "PRO AUDIO · IN_SOURCE_1 → OUT_SINK_1", slots: [0, 1] }' in content_text
    assert "aux-scope-pair-separator" in content_text
    assert "data-scope-format" not in content_text
    assert "Trig CH1" not in content_text
    assert "t / ms" not in content_text
    assert "data-scope-time-label" not in content_text
    body_css_start = content_text.index('            body {\n                font-family: var(--fontFamilyBase')
    body_css_end = content_text.index("}", body_css_start)
    assert "overflow: hidden;" in content_text[body_css_start:body_css_end]
    settings_position_css_start = content_text.index(".scope-settings {")
    settings_position_css_end = content_text.index("}", settings_position_css_start)
    settings_position_css = content_text[settings_position_css_start:settings_position_css_end]
    assert "top: auto;" in settings_position_css
    assert "bottom: 4px;" in settings_position_css
    assert "right: 4px;" in settings_position_css
    assert "flex-direction: column-reverse;" in settings_position_css
    assert 'class="aux-scope-axis-tick" data-scope-amplitude-tick="${fraction}" x="${plot.left - 2}"' in content_text
    assert 'class="aux-scope-axis-tick" data-scope-time-division="${divisionIndex}" x="${x}" y="${plot.bottom + 1}" dominant-baseline="hanging"' in content_text
    assert 'class="aux-scope-lane-level" x="${plot.left + 1}" y="${laneTop + 1}" dominant-baseline="hanging" text-anchor="start"' in content_text
    assert "≤-" not in content_text
    assert ".aux-response-chart .aux-scope-axis-tick" in content_text
    scroll_css_start = content_text.index(".aux-matrix-scroll {")
    scroll_css_end = content_text.index("}", scroll_css_start)
    scroll_css = content_text[scroll_css_start:scroll_css_end]
    assert "overflow: hidden;" in scroll_css
    assert "overflow-x: auto;" not in scroll_css
    assert "scrollbar-width: none;" in scroll_css
    assert ".aux-matrix-scroll::-webkit-scrollbar" in content_text
    settings_panel_css_start = content_text.index(".scope-settings-panel {")
    settings_panel_css_end = content_text.index("}", settings_panel_css_start)
    settings_panel_css = content_text[settings_panel_css_start:settings_panel_css_end]
    assert "scrollbar-width: none;" in settings_panel_css
    assert "margin: 0 0 4px;" in settings_panel_css
    settings_toggle_css_start = content_text.index(".scope-settings-toggle {")
    settings_toggle_css_end = content_text.index("}", settings_toggle_css_start)
    settings_toggle_css = content_text[settings_toggle_css_start:settings_toggle_css_end]
    assert "border: 0;" in settings_toggle_css
    assert "background: transparent;" in settings_toggle_css
    settings_hover_css_start = content_text.index(".scope-settings-toggle:hover,")
    settings_hover_css_end = content_text.index("}", settings_hover_css_start)
    assert "border-color:" not in content_text[settings_hover_css_start:settings_hover_css_end]
    assert 'grid-area: aux-matrix;' in content_text
    assert "MATRIX ·" not in content_text
    assert "aux-matrix-heading" not in content_text
    assert "data-matrix-mode" not in content_text
    assert "Frequenzgang" not in content_text
    assert "LIVE PCM" not in content_text
    assert "Live-PCM" not in content_text
    assert 'waiting_for_pcm_samples: "",' in content_text
    assert "loadAudioAuxMatrixHtml" in content_text
    assert '<div class="aux-matrix-scroll" role="region" aria-label="Live playback oscilloscope">' in content_text
    assert '<svg class="aux-response-chart" viewBox="0 0 640 347.2"' in content_text
    assert '<g transform="scale(1 0.8)">' in content_text
    assert "data-matrix-view" not in content_text
    assert 'class="aux-scope-wave" data-scope-channel=' in content_text
    lane_info_css_start = content_text.index(".aux-response-chart .aux-scope-lane-level {")
    lane_info_css_end = content_text.index("}", lane_info_css_start)
    lane_info_css = content_text[lane_info_css_start:lane_info_css_end]
    assert ".aux-response-chart .aux-scope-lane-level" in lane_info_css
    assert "font-size: calc(var(--aux-scope-font-size, 10px) - 1px);" in lane_info_css
    assert 'fill="var(--wp-muted)"' in content_text
    assert 'levelText.setAttribute("fill"' not in content_text
    assert ".aux-scope-lane-label" not in content_text
    assert "data-scope-zone-label" not in content_text
    assert ".aux-scope-lane-source" not in content_text
    assert "data-scope-lane-source" not in content_text
    assert "font-size: calc(var(--aux-scope-font-size, 10px) - 1px);" in lane_info_css
    assert 'document.querySelector(".header-track-artist, .audio-pipe-meta")' in content_text
    assert "getScreenCTM()" in content_text
    assert 'chartEl.style.setProperty("--aux-scope-font-size"' in content_text
    assert "new ResizeObserver(syncAudioScopeTextSize)" in content_text
    assert 'audioScopeStatusText = "";' in content_text
    assert "syncAudioScopePolling" in content_text
    assert "const refreshAudioScope = async () =>" in content_text
    assert "const responseText = await response.text();" in content_text
    assert "scope_empty_response_http_" in content_text
    assert "scope_forbidden_origin_http_403_${window.location.origin}" in content_text
    assert "scope_invalid_json_http_" in content_text
    assert "attempt < 3" in content_text
    assert 'const UI_SCOPE_PATH = "/ui/Mpc.App.ui/scope";' in content_text
    assert "aux-vu-track" not in content_text
    assert "aux-vu-fill" not in content_text
    assert "const frequencyTicks = [" not in content_text
    assert "const curveHtml" not in content_text
    assert "aux-response-curve" not in content_text
    assert "CALCULATED · NOT ACOUSTIC" not in content_text
    scope_chart_css_start = content_text.index(".aux-response-chart {")
    scope_chart_css_end = content_text.index("}", scope_chart_css_start)
    scope_chart_css = content_text[scope_chart_css_start:scope_chart_css_end]
    assert "font-size: 10px;" in scope_chart_css
    scope_text_css_start = content_text.index(".aux-response-chart text {")
    scope_text_css_end = content_text.index("}", scope_text_css_start)
    scope_text_css = content_text[scope_text_css_start:scope_text_css_end]
    assert "font-size: var(--aux-scope-font-size, 10px);" in scope_text_css
    assert "Fr/Hz" not in content_text
    assert 'playback_settings' in content_text
    assert "grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);" in content_text
    assert "min-width: 620px;" not in content_text
    assert "@container player-layout (max-width: 600px)" in content_text
    assert "dBFS" in content_text
    assert "nowPayload.audio_aux_routing" in content_text
    assert "nowPayload.audio_aux_routing_status" in content_text
    assert "nowPayload.audio_aux_dsp_data" in content_text


def test_previous_ui_resource_uri_remains_readable_for_cached_hosts() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "resources/read",
            "params": {
                "uri": "ui://WebMpc.app.ui",
                "_meta": {
                    "io.modelcontextprotocol/extensions": "io.modelcontextprotocol/ui",
                },
            },
        }
    )

    contents = (payload.get("result") or {}).get("contents") or []
    assert contents
    assert contents[0].get("uri") == "ui://WebMpc.app.ui"
    assert contents[0].get("mimeType") == "text/html;profile=mcp-app"
    content_text = str(contents[0].get("text") or "")
    assert 'new appModule.App({ name: "Mpc.App.ui", version: "1.0.0" });' in content_text
    assert 'const UI_SCOPE_PATH = "/ui/Mpc.App.ui/scope";' in content_text
    assert "nowPayload.audio_aux_dsp_status" in content_text
    assert '"cover audio-pipe brand"' in content_text
    assert 'grid-area: audio-pipe;' in content_text
    assert 'text-align: left;' in content_text
    cover_markup_start = content_text.index('<div class="cover-shell"')
    audio_pipe_markup_start = content_text.index('<div id="audio-pipe-info"')
    assert cover_markup_start < audio_pipe_markup_start
    assert "loadAudioOutputInfo" in content_text
    assert "navigator.mediaDevices.enumerateDevices" in content_text
    assert "nowPayload.audio_source" in content_text
    assert "nowPayload.audio_sink" in content_text
    assert "nowPayload.audio_source_name" in content_text
    assert "nowPayload.audio_sink_name" in content_text
    assert "nowPayload.audio_profile" in content_text
    assert "nowPayload.system_volume_percent" in content_text
    assert 'const audioDeviceText = loadAudioPipeDeviceText(audioRoute)' in content_text
    assert 'interfaceText.replace(/\\bUSB\\b/i, "USB0")' in content_text
    assert 'const profileText = formatField(view.audioProfile) === "Pro" ? "ProAudio" : formatField(view.audioProfile)' in content_text
    assert 'const playerTitle = profileText !== "-" ? `ProAudio - TIDAL WebPlayer` : "TIDAL WebPlayer"' in content_text
    assert 'Audio/Source: ${audioRoute.sourceText}' not in content_text
    assert 'DAC/ADC Device: ${audioRoute.dacAdcDeviceText}' not in content_text
    assert 'Ch. routing: ${audioRoute.channelText}' not in content_text
    assert 'Controler Pro Audio' not in content_text
    assert 'normalizeAudioDeviceText' not in content_text
    assert 'DAC OUT: ${audioRoute.dacOutText}' not in content_text
    assert 'ADC IN: ${audioRoute.adcInText}' not in content_text
    assert 'loadAudioRouteValues' in content_text
    assert 'Snd-Iface: ${audioInterfaceText}' not in content_text
    assert 'const headerText = dspText === "-" ? "" : "CamillaDsp"' in content_text
    assert "loadAudioPipeDeviceText(audioRoute)" in content_text
    assert 'loadDetailHtml("Audio/Sink", audioRoute.sinkText)' not in content_text
    assert 'loadDetailHtml("Audio/Source", audioRoute.sourceText)' not in content_text
    assert 'loadDetailHtml("ALSA Sink", audioRoute.sinkNameText)' not in content_text
    assert 'loadDetailHtml("ALSA Source", audioRoute.sourceNameText)' not in content_text
    assert 'loadDetailHtml("Channel routing", audioRoute.channelText)' not in content_text
    assert 'loadDetailHtml("Sink volume", formatVolumeDbText(view.volumePercent))' not in content_text


def test_playback_scope_decodes_interleaved_pcm_samples() -> None:
    payload = struct.pack("<6h", -32768, 16384, 0, -8192, 32767, 0)

    frames = PlaybackScopeService._load_pcm_frames(payload, channel_count=2)

    assert frames == [(-1.0, 0.5), (0.0, -0.25), (32767 / 32768, 0.0)]


def test_playback_scope_decodes_24_bit_samples_in_32_bit_frames() -> None:
    lsb_24_bit = 1 << 8
    payload = struct.pack("<4i", -(2**31), lsb_24_bit, 2**30, -lsb_24_bit) + b"\x00\x01"

    frames = PlaybackScopeService._load_pcm_frames(payload, channel_count=2, sample_format="s32le")

    assert frames == [(-1.0, 2**-23), (0.5, -(2**-23))]


def test_playback_scope_loads_pipewire_monitor_sample_rate(monkeypatch) -> None:
    service = PlaybackScopeService()
    service.configure(
        playback_device={
            "autoconnect_to": "alsa_output.test.pro-output-0",
            "channels": 2,
        }
    )
    source_listing = (
        "Source #3\n"
        "\tName: alsa_output.test.pro-output-0.monitor\n"
        "\tSample Specification: s32le 2ch 44100Hz\n"
        "\tChannel Map: front-left,front-right\n"
    )
    monkeypatch.setattr(webplayer_module.shutil, "which", lambda _command: "/usr/bin/pactl")
    monkeypatch.setattr(
        webplayer_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, source_listing, ""),
    )

    assert service._load_monitor_sample_rate() == 44100


def test_playback_scope_captures_and_returns_live_pcm_frames(monkeypatch, tmp_path) -> None:
    parec_path = tmp_path / "parec"
    capture_arguments_path = tmp_path / "capture-arguments.json"
    monkeypatch.setenv("SCOPE_CAPTURE_ARGUMENTS", str(capture_arguments_path))
    parec_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, struct, sys, time\n"
        "with open(os.environ['SCOPE_CAPTURE_ARGUMENTS'], 'w') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n"
        "block = struct.pack('<2i', 12000 << 16, -6000 << 16) * 1024\n"
        "while True:\n"
        "    sys.stdout.buffer.write(block)\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    parec_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(
        webplayer_module.shutil,
        "which",
        lambda command: str(parec_path) if command == "parec" else "/usr/bin/pactl" if command == "pactl" else None,
    )
    monkeypatch.setattr(
        webplayer_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            "Source #3\n"
            "\tName: alsa_output.test.pro-output-0.monitor\n"
            "\tSample Specification: s32le 2ch 44100Hz\n",
            "",
        ),
    )
    service = PlaybackScopeService()
    service.configure(
        playback_device={
            "autoconnect_to": "alsa_output.test.pro-output-0",
            "channels": 2,
        }
    )

    try:
        payload = service.load_scope_payload({"channels": "out1,out2", "points": 2048, "trigger": "off"})
    finally:
        with service._condition:
            reader_thread = service._reader_thread
            service._stop_capture_locked()
        if reader_thread is not None:
            reader_thread.join(timeout=2)

    assert payload["ok"] is True
    assert payload["sample_rate_hz"] == 44100
    assert [channel["index"] for channel in payload["channels"]] == [0, 1]
    assert all(len(channel["samples"]) == 1024 for channel in payload["channels"])
    assert payload["window_ms"] == pytest.approx(1024 * 1000 / 44100)
    assert payload["channels"][0]["samples"][0] == pytest.approx(12000 / 32768, abs=0.00005)
    assert payload["channels"][1]["samples"][0] == pytest.approx(-6000 / 32768, abs=0.00005)
    capture_arguments = json.loads(capture_arguments_path.read_text())
    assert "--latency-msec=20" in capture_arguments
    assert "--process-time-msec=10" in capture_arguments
    assert "--format=s32le" in capture_arguments
    assert payload["bit_depth"] == 24
    assert [channel["source"] for channel in payload["channels"]] == ["out1", "out2"]


def test_playback_scope_preserves_pro_audio_channel_map(monkeypatch) -> None:
    service = PlaybackScopeService()
    service.configure(playback_device={"autoconnect_to": "test", "channels": 4})
    monkeypatch.setattr(webplayer_module.shutil, "which", lambda _command: "/usr/bin/test")
    monkeypatch.setattr(
        webplayer_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0,
            "Source #3\n\tName: test.monitor\n"
            "\tSample Specification: s32le 4ch 48000Hz\n"
            "\tChannel Map: aux0,aux1,aux2,aux3\n",
            "",
        ),
    )
    arguments = []
    monkeypatch.setattr(
        webplayer_module.subprocess, "Popen",
        lambda command, **_kwargs: arguments.extend(command),
    )
    monkeypatch.setattr(webplayer_module.threading.Thread, "start", lambda _thread: None)

    with service._condition:
        service._start_capture_locked()

    assert "--channel-map=aux0,aux1,aux2,aux3" in arguments
    assert "--format=s32le" in arguments
    assert "--channels=4" in arguments
    assert "--rate=48000" in arguments
    assert "--latency-msec=20" in arguments


def test_playback_scope_retains_latest_frames_and_low_level_detail(monkeypatch) -> None:
    monkeypatch.setattr(
        PlaybackScopeService,
        "_load_source_catalog_locked",
        lambda _service: [
            {"id": f"out{index + 1}", "label": f"Out Sink {index + 1}", "kind": "output", "channel": index}
            for index in range(4)
        ],
    )
    service = PlaybackScopeService()
    service.configure(playback_device={"autoconnect_to": "test", "channels": 4})
    service._process = object()
    service._sample_rate = 48000
    service._sample_frames.extend(
        (index / 32768, -1 / 32768, 0.0, 1 / 32768)
        for index in range(5000)
    )

    payload = service.load_scope_payload(
        {"channels": "out1,out2,out3,out4", "points": 2048, "trigger": "off"}
    )

    assert len(service._sample_frames) == 2048
    assert payload["ok"] is True
    assert payload["window_ms"] == pytest.approx(1024 * 1000 / 48000)
    assert [channel["index"] for channel in payload["channels"]] == [0, 1, 2, 3]
    assert all(len(channel["samples"]) == 1024 for channel in payload["channels"])
    assert payload["channels"][0]["samples"][0] == round((5000 - 1024) / 32768, 6)
    assert payload["channels"][0]["samples"][-1] == round(4999 / 32768, 6)
    assert payload["channels"][1]["samples"][0] == pytest.approx(-1 / 32768, abs=0.0000005)
    assert payload["channels"][3]["samples"][0] == pytest.approx(1 / 32768, abs=0.0000005)


def test_playback_scope_request_normalizes_channels_window_and_points() -> None:
    request = PlaybackScopeService.load_scope_request(
        {"channels": ["OUT1,in2", "bogus"], "window_ms": ["9999"], "points": ["7"], "trigger": ["off"]}
    )

    assert request == {"channels": ["out1", "in2", "off"], "window_ms": 200.0, "points": 32, "trigger": False}
    assert PlaybackScopeService.load_scope_request(None)["channels"] == []
    assert PlaybackScopeService.load_scope_request(
        {"channels": "in1,in2,out1,out2,out3,out4,out5"}
    )["channels"] == ["in1", "in2", "out1", "out2", "out3", "out4"]


def test_playback_scope_derives_scarlett_input_from_pro_audio_sink() -> None:
    assert PlaybackScopeService.load_input_source_name(
        "alsa_output.usb-Focusrite_Scarlett_2i4_USB-00.pro-output-0"
    ) == "alsa_input.usb-Focusrite_Scarlett_2i4_USB-00.pro-input-0"
    assert PlaybackScopeService.load_input_source_name("camilladsp_sink") == ""


def test_playback_scope_mixes_output_monitor_and_input_channels(monkeypatch) -> None:
    sink = "alsa_output.usb-Focusrite_Scarlett_2i4_USB-00.pro-output-0"
    source_listing = (
        f"Source #3\n\tName: {sink}.monitor\n"
        "\tSample Specification: s32le 4ch 48000Hz\n\tChannel Map: aux0,aux1,aux2,aux3\n"
        "Source #4\n\tName: alde-dsp-input.monitor\n"
        "\tSample Specification: float32le 2ch 48000Hz\n\tChannel Map: front-left,front-right\n"
    )
    monkeypatch.setattr(webplayer_module.shutil, "which", lambda _command: "/usr/bin/test")
    monkeypatch.setattr(
        webplayer_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, source_listing, ""),
    )
    started = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_start(service, object_name="output"):
        capture = service._captures[object_name]
        spec = service._load_object_sample_spec(object_name)
        capture.process = FakeProcess()
        capture.channel_count = spec["channel_count"]
        capture.sample_rate = spec["sample_rate"]
        started.append(object_name)
        if object_name == "output":
            capture.sample_frames.extend((0.5, -0.25, 0.125, 0.0) for _ in range(4800))
        else:
            capture.sample_frames.extend((0.01, -0.02) for _ in range(4800))

    monkeypatch.setattr(PlaybackScopeService, "_start_capture_locked", fake_start)
    service = PlaybackScopeService()
    service.configure(
        playback_device={"autoconnect_to": sink, "channels": 4},
        input_source="alde-dsp-input.monitor",
    )

    payload = service.load_scope_payload(
        {"channels": "in1,in2,out1,out2,out3,out4", "window_ms": "20", "points": "512", "trigger": "off"}
    )

    assert started == ["output", "input"]
    assert payload["ok"] is True
    assert payload["sample_rate_hz"] == 48000
    assert payload["bit_depth"] == 24
    assert [source["id"] for source in payload["sources"]] == ["out1", "out2", "out3", "out4", "in1", "in2"]
    assert [(channel["index"], channel["source"]) for channel in payload["channels"]] == [
        (0, "in1"), (1, "in2"), (2, "out1"), (3, "out2"), (4, "out3"), (5, "out4")
    ]
    assert all(channel["frame_count"] == 960 for channel in payload["channels"])
    assert payload["channels"][0]["min"][0] == 0.01
    assert payload["channels"][0]["max"][0] == 0.01
    assert payload["channels"][1]["min"][0] == -0.02
    assert payload["channels"][1]["max"][0] == -0.02
    assert len(payload["channels"][2]["min"]) == 512
    assert payload["channels"][2]["rms_db"] == pytest.approx(-6.0, abs=0.05)
    default_payload = service.load_scope_payload({"window_ms": "20", "trigger": "off"})
    assert [(channel["index"], channel["source"]) for channel in default_payload["channels"]] == [
        (0, "in1"), (1, "out1")
    ]


def test_playback_scope_reports_unavailable_requested_source(monkeypatch) -> None:
    sink = "alsa_output.usb-Focusrite_Scarlett_2i4_USB-00.pro-output-0"
    source_listing = (
        f"Source #3\n\tName: {sink}.monitor\n"
        "\tSample Specification: s32le 4ch 48000Hz\n\tChannel Map: aux0,aux1,aux2,aux3\n"
    )
    monkeypatch.setattr(webplayer_module.shutil, "which", lambda _command: "/usr/bin/test")
    monkeypatch.setattr(
        webplayer_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, source_listing, ""),
    )
    service = PlaybackScopeService()
    service.configure(
        playback_device={"autoconnect_to": sink, "channels": 4},
        input_source="alde-dsp-input.monitor",
    )

    payload = service.load_scope_payload({"channels": "in1,out1"})

    assert payload["ok"] is False
    assert payload["status"] == "unavailable"
    assert payload["error"] == "playback_scope_source_unavailable:in1"


def test_playback_scope_envelope_preserves_peaks_and_trigger_aligns_rising_edge() -> None:
    samples = [0.0] * 1000
    samples[437] = 0.9
    samples[611] = -0.8

    trace = PlaybackScopeService._load_channel_trace(samples, point_count=100)

    assert max(trace["max"]) == 0.9
    assert min(trace["min"]) == -0.8
    assert trace["peak_db"] == pytest.approx(-0.9, abs=0.05)

    wave = [math.sin(2 * math.pi * index / 100 + 1.0) for index in range(1000)]
    offset = PlaybackScopeService._load_trigger_offset(wave, window_frames=400)

    assert offset is not None and offset <= 600
    assert wave[offset - 1] < 0 <= wave[offset]
    assert PlaybackScopeService._load_trigger_offset([0.1] * 100, window_frames=50) is None


def test_playback_scope_tool_accepts_scope_request_arguments(monkeypatch) -> None:
    captured = {}

    def fake_payload(request=None):
        captured["request"] = request
        return {"ok": True, "channels": []}

    monkeypatch.setattr(webplayer_module.PLAYBACK_SCOPE_SERVICE, "load_scope_payload", fake_payload)
    service = WebPlayerMcpRequestService()
    tool = next(
        entry["function"]
        for entry in service.load_tool_definitions()
        if entry["function"]["name"] == "webplayer_playback_scope"
    )

    response = service.load_tools_call_result(
        {"name": "webplayer_playback_scope", "arguments": {"channels": "out1,in1", "window_ms": 10}}
    )

    assert set(tool["parameters"]["properties"]) == {"channels", "window_ms", "points", "trigger"}
    assert captured["request"] == {"channels": "out1,in1", "window_ms": 10}
    assert json.loads(response["result"]["content"]) == {"ok": True, "channels": []}


def test_ui_scope_scheduling_rendering_and_controls() -> None:
    markup = WebPlayerMcpRequestService()._load_ui_resource_markup()
    state = markup[
        markup.index("const SCOPE_FRAME_INTERVAL_MS"):
        markup.index("let webLightTheme")
    ]
    scope_functions = markup[
        markup.index("const loadAudioScopeSourceLabel"):
        markup.index("const loadAudioPipeDeviceText")
    ]
    stop_polling = markup[
        markup.index("const stopPolling ="):
        markup.index('window.addEventListener("pagehide", stopPolling')
    ]
    script = r"""
const assert = require("node:assert/strict");
let now = 0;
let sequence = 0;
const timers = new Map();
const animations = new Map();
const requests = [];
const listeners = {};
const resizeObservers = [];
let renderedScopeScale = 0.4;
class ResizeObserver {
    constructor(callback) { this.callback = callback; resizeObservers.push(this); }
    observe(target) { this.target = target; }
}
const makeEl = (dataset = {}) => ({ dataset, textContent: "", value: "", innerHTML: "", setAttribute(key, value) { this[key] = value; } });
const waveforms = [0, 1].map((index) => makeEl({ scopeChannel: String(index) }));
const levelTexts = [0, 1].map((index) => makeEl({ scopeLevel: String(index) }));
const selects = [0, 1].map((index) => makeEl({ scopeSource: String(index) }));
const status = makeEl();
const timebaseLabel = makeEl();
const verticalLabel = makeEl();
const timeTick = makeEl({ scopeTimeDivision: "10" });
const amplitudeTick = makeEl({ scopeAmplitudeTick: "0.5" });
const zeroAmplitudeTick = makeEl({ scopeAmplitudeTick: "0" });
const negativeAmplitudeTick = makeEl({ scopeAmplitudeTick: "-0.5" });
const scopeChartFontStyles = {};
const scopeChartText = { getScreenCTM: () => ({ c: 0, d: renderedScopeScale }) };
const scopeChart = {
    querySelector: selector => selector === "text" ? scopeChartText : null,
    style: { setProperty(name, value) { scopeChartFontStyles[name] = value; } },
};
const referenceText = {};
const document = {
    hidden: false,
    querySelector: () => referenceText,
    addEventListener() {},
    removeEventListener(name) { this.removed = name; },
};
const navigator = {};
const performance = { now: () => now };
const window = {
    location: { origin: "vscode-webview://test" },
    getComputedStyle: () => ({ fontSize: "10px" }),
    setTimeout(callback, delay) {
        const id = ++sequence;
        timers.set(id, { callback, delay });
        return id;
    },
    clearTimeout(id) { timers.delete(id); },
    requestAnimationFrame(callback) {
        const id = ++sequence;
        animations.set(id, callback);
        return id;
    },
    cancelAnimationFrame(id) { animations.delete(id); },
    clearInterval() {},
};
const single = {
    "[data-scope-status]": status,
    "[data-scope-timebase-label]": timebaseLabel,
    "[data-scope-vertical-label]": verticalLabel,
};
const auxMatrixEl = {
    addEventListener(name, callback) { listeners[name] = callback; },
    querySelector(selector) {
        if (selector === ".aux-response-chart") return scopeChart;
        const level = selector.match(/^\[data-scope-level="(\d)"\]$/);
        return level ? levelTexts[Number(level[1])] : single[selector] || null;
    },
    querySelectorAll(selector) {
        if (selector === "[data-scope-channel]") return waveforms;
        if (selector === "[data-scope-source]") return selects;
        if (selector === "[data-scope-time-division]") return [timeTick];
        if (selector === "[data-scope-amplitude-tick]") return [amplitudeTick, zeroAmplitudeTick, negativeAmplitudeTick];
        return [];
    },
};
const escapeHtml = (value) => String(value);
const parseJsonOrNull = (text) => { try { return JSON.parse(text); } catch (_) { return null; } };
const callServerToolRaw = async () => { throw new Error("tool transport not expected"); };
const app = null;
const appHostConnected = false;
const UI_SCOPE_PATH = "/ui/Mpc.App.ui/scope";
const MCP_HTTP_ORIGIN = "http://localhost:8766";
let refreshIntervalId = null;
let countdownIntervalId = null;
const fetch = (url, options) => new Promise((resolve, reject) => {
    requests.push({ url: new URL(url), options, resolve, reject });
});
const flush = () => new Promise(resolve => setImmediate(resolve));
const runTimer = (id) => {
    const timer = timers.get(id);
    assert.ok(timer);
    timers.delete(id);
    now += timer.delay;
    timer.callback();
};
const respond = (request, payload, httpStatus = 200) => request.resolve({
    ok: httpStatus === 200,
    status: httpStatus,
    text: async () => JSON.stringify(payload),
});
const draw = () => {
    assert.equal(animations.size, 1);
    const [frameId, callback] = animations.entries().next().value;
    animations.delete(frameId);
    callback();
};
const click = (dataset) => listeners.click({ type: "click", target: { closest: (selector) => {
    const key = selector.slice(6, -1).replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
    return dataset[key] !== undefined ? { dataset } : null;
} } });
""" + state + scope_functions + stop_polling + r"""
(async () => {
    assert.equal(SCOPE_FRAME_INTERVAL_MS, 1000 / 30);
    assert.equal(SCOPE_SLOT_COUNT, 2);
    assert.equal(resizeObservers[0].target, auxMatrixEl);
    syncAudioScopeTextSize();
    assert.equal(scopeChartFontStyles["--aux-scope-font-size"], "22.5px");
    renderedScopeScale = 0.25;
    resizeObservers[0].callback();
    assert.equal(scopeChartFontStyles["--aux-scope-font-size"], "36px");
    assert.deepEqual(SCOPE_CHANNEL_PAIRS.map(pair => pair.label), ["PRO AUDIO · IN_SOURCE_1 → OUT_SINK_1"]);
    assert.equal(loadAudioScopeSourceLabel(audioScopeSources.find(source => source.id === "in1")), "IN_SOURCE_1");
    assert.equal(loadAudioScopeSourceLabel(audioScopeSources.find(source => source.id === "out1")), "OUT_SINK_1 (Out 2)");
    assert.equal(loadAudioScopeSourceLabel(audioScopeSources.find(source => source.id === "out4")), "OUT_SINK_4 (Out 5)");
    audioScopeDspMeters = { capture: [{ peak_db: 0.1 }], playback: [{ peak_db: 0 }] };
    assert.equal(loadAudioScopeWaveColor(0), SCOPE_OVERLOAD_COLOR);
    assert.equal(loadAudioScopeWaveColor(1), SCOPE_WAVE_COLOR);
    audioScopeDspMeters = { capture: [{ peak_db: 0 }], playback: [{ peak_db: 0.1 }] };
    assert.equal(loadAudioScopeWaveColor(0), SCOPE_WAVE_COLOR);
    assert.equal(loadAudioScopeWaveColor(1), SCOPE_OVERLOAD_COLOR);
    audioScopeDspMeters = { capture: [{ peak_db: 0.1 }], playback: [{ peak_db: 0 }] };
    applyAudioScopeControls();
    assert.equal(timebaseLabel.textContent, "2 ms/div");
    assert.equal(verticalLabel.textContent, "24 dB/div");
    assert.equal(timeTick.textContent, "20");
    assert.equal(amplitudeTick.textContent, "24");
    assert.equal(zeroAmplitudeTick.textContent, "48");
    assert.equal(negativeAmplitudeTick.textContent, "24");
    syncAudioScopePolling();
    assert.equal(requests.length, 1);
    const firstUrl = requests[0].url;
    assert.equal(firstUrl.origin + firstUrl.pathname, "http://localhost:8766/ui/Mpc.App.ui/scope");
    assert.equal(firstUrl.searchParams.get("channels"), "in1,out1");
    assert.equal(firstUrl.searchParams.get("window_ms"), "20");
    assert.equal(firstUrl.searchParams.get("points"), "512");
    assert.equal(firstUrl.searchParams.get("trigger"), "rising");
    syncAudioScopePolling();
    assert.equal(requests.length, 1, "only one request may be in flight");
    const samples = Array.from({ length: 960 }, (_, index) => Math.sin(index / 20) / 2);
    const envelope = Array.from({ length: 512 }, () => 0.25);
    respond(requests[0], {
        ok: true, sample_rate_hz: 48000, bit_depth: 24, triggered: true,
        sources: [
            { id: "in1", label: "In Source 1", kind: "input", channel: 0 },
            { id: "in2", label: "In Source 2", kind: "input", channel: 1 },
            { id: "out1", label: "Out Sink 1", kind: "output", channel: 0 },
            { id: "out2", label: "Out Sink 2", kind: "output", channel: 1 },
            { id: "out3", label: "Out Sink 3", kind: "output", channel: 2 },
            { id: "out4", label: "Out Sink 4", kind: "output", channel: 3 },
        ],
        channels: [
            { index: 0, source: "in1", sample_rate_hz: 48000, frame_count: 960, rms_db: -9.03, samples },
            { index: 1, source: "out1", sample_rate_hz: 48000, frame_count: 1920, rms_db: null, min: envelope.map(v => -v), max: envelope },
        ],
    });
    await flush();
    assert.equal(waveforms[0].points, undefined, "render waits for a browser animation frame");
    draw();
    assert.equal(waveforms[0].stroke, SCOPE_OVERLOAD_COLOR);
    assert.equal(waveforms[1].stroke, SCOPE_WAVE_COLOR);
    assert.equal(levelTexts[0].fill, undefined);
    assert.equal(levelTexts[1].fill, undefined);
    const wavePoints = waveforms[0].points.split(" ");
    assert.equal(wavePoints.length, 960);
    assert.ok(Number(wavePoints.at(-1).split(",")[0]) > 555, "a 20 ms window spans the full graticule");
    assert.equal(waveforms[1].points.split(" ").length, 1024, "min/max envelope keeps peaks");
    assert.equal(levelTexts[0].textContent, "-9.0 dB");
    assert.equal(levelTexts[1].textContent, "—");
    assert.match(selects[0].innerHTML, /IN_SOURCE_1/);
    assert.match(selects[1].innerHTML, /OUT_SINK_1/);
    assert.equal(status.textContent, "", "live frames show no status text");
    assert.ok(timers.get(scopePollTimeoutId).delay <= 1000 / 30);

    click({ scopeAction: "time-plus" });
    assert.equal(timebaseLabel.textContent, "5 ms/div");
    click({ scopeUnit: "V" });
    assert.equal(verticalLabel.textContent, "0.5 V/div");
    assert.equal(amplitudeTick.textContent, "0.5");
    assert.equal(zeroAmplitudeTick.textContent, "0");
    assert.equal(negativeAmplitudeTick.textContent, "0.5");
    click({ scopeUnit: "dB" });
    assert.equal(verticalLabel.textContent, "24 dB/div");
    assert.equal(amplitudeTick.textContent, "24");
    assert.equal(zeroAmplitudeTick.textContent, "48");
    assert.ok(Math.abs(loadAudioScopeLaneValue(0.01) - (1 - 40 / 48)) < 1e-9);
    click({ scopeAction: "vertical-plus" });
    assert.equal(verticalLabel.textContent, "18 dB/div");
    assert.equal(zeroAmplitudeTick.textContent, "36");
    listeners.change({ type: "change", target: { closest() { return null; }, matches: () => true, dataset: { scopeSource: "1" }, value: "out2" } });
    assert.equal(selects[1].value, "out2");
    runTimer(scopePollTimeoutId);
    assert.equal(requests.length, 2);
    assert.equal(requests[1].url.searchParams.get("window_ms"), "50");
    assert.equal(requests[1].url.searchParams.get("channels"), "in1,out2");

    respond(requests[1], { error: "forbidden_origin" }, 403);
    await flush();
    assert.match(status.textContent, /scope_forbidden_origin_http_403_vscode-webview:\/\/test/);
    assert.equal(timers.get(scopePollTimeoutId).delay, 500);
    document.hidden = true;
    syncAudioScopePolling();
    assert.equal(timers.size, 0);
    assert.equal(animations.size, 0);
    document.hidden = false;
    syncAudioScopePolling();
    assert.equal(requests.length, 3);
    stopPolling();
    respond(requests[2], { ok: true, sample_rate_hz: 48000, channels: [{ index: 0, samples }] });
    await flush();
    assert.equal(timers.size, 0);
    assert.equal(animations.size, 0);
    assert.equal(document.removed, "visibilitychange");
    console.log("scope scheduling, rendering, controls and teardown verified");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    try:
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=False, timeout=10
        )
    except FileNotFoundError:
        pytest.skip("Node.js is unavailable")
    assert result.returncode == 0, result.stderr


def test_playback_scope_reports_missing_monitor_configuration() -> None:
    payload = PlaybackScopeService().load_scope_payload()

    assert payload == {
        "ok": False,
        "status": "not_configured",
        "error": "playback_monitor_not_configured",
    }


def test_audio_route_metadata_command_reads_default_source_sink_and_profile(monkeypatch, tmp_path) -> None:
    pactl_path = tmp_path / "pactl"
    pactl_path.write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'get-default-sink ') echo alsa_output.usb-test.pro-output-0;;\n"
        "  'get-default-source ') echo alsa_input.usb-test.pro-input-0;;\n"
        "  'list sinks') printf '%s\\n' 'Sink #258' '    Name: alsa_output.usb-test.pro-output-0' '    Description: Verbundes USB-Audiointerface';;\n"
        "  'list sources') printf '%s\\n' 'Source #96' '    Name: alsa_input.usb-test.pro-input-0' '    Description: Verbundes USB-Audiointerface';;\n"
        "  'list cards') printf '%s\\n' 'Card #211' '    Name: alsa_card.usb-test' '    device.description = \"Scarlett 2i4\"' '    device.nick = \"Scarlett 2i4 USB\"' '    Active Profile: Pro';;\n"
        "esac\n",
        encoding="utf-8",
    )
    pactl_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    service = WebPlayerService()
    metadata = service._load_audio_route_metadata()

    assert metadata["audio_source"] == "Verbundes USB-Audiointerface"
    assert metadata["audio_sink"] == "Verbundes USB-Audiointerface"
    assert metadata["audio_interface"] == "Scarlett 2i4 USB"
    assert metadata["audio_profile"] == "Pro"


def test_audio_aux_routing_metadata_reads_live_pipewire_connections(monkeypatch, tmp_path) -> None:
    pw_link_path = tmp_path / "pw-link"
    pw_link_path.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = \"-l\" ] || exit 2\n"
        "printf '%s\\n' "
        "'camilladsp-passive-sub-capture:output_1' "
        "'  |-> alsa_output.usb-scarlett:playback_AUX0' "
        "'camilladsp-passive-sub-capture:output_2' "
        "'  |-> alsa_output.usb-scarlett:playback_AUX1' "
        "'camilladsp-passive-sub-capture:output_3' "
        "'  |-> alsa_output.usb-scarlett:playback_AUX2' "
        "'camilladsp-passive-sub-capture:output_4' "
        "'  |-> alsa_output.usb-scarlett:playback_AUX3' "
        "'alsa_input.usb-scarlett:capture_AUX0' "
        "'  |-> camilladsp-passive-sub-capture:input_FL' "
        "'camilladsp-passive-sub-capture:input_FR' "
        "'  |<- alsa_input.usb-scarlett:capture_AUX1'\n",
        encoding="utf-8",
    )
    pw_link_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    metadata = WebPlayerService._load_audio_aux_routing_metadata()
    routing = json.loads(metadata["audio_aux_routing"])

    assert metadata["audio_aux_routing_status"] == "ready"
    assert routing[0] == {
        "channel": "AUX0",
        "input": ["capture_AUX0 -> input_FL"],
        "output": ["output_1 -> playback_AUX0"],
    }
    assert routing[1] == {
        "channel": "AUX1",
        "input": ["capture_AUX1 -> input_FR"],
        "output": ["output_2 -> playback_AUX1"],
    }
    assert routing[2]["output"] == ["output_3 -> playback_AUX2"]
    assert routing[3]["output"] == ["output_4 -> playback_AUX3"]
    assert routing[4] == {"channel": "AUX4", "input": [], "output": []}
    assert routing[5] == {"channel": "AUX5", "input": [], "output": []}


def test_camilladsp_channel_settings_map_filters_delay_and_polarity() -> None:
    config = {
        "devices": {"samplerate": 2000},
        "filters": {
            "input_delay": {
                "type": "Delay",
                "parameters": {"delay": 1, "unit": "samples"},
            },
            "input_invert": {
                "type": "Gain",
                "parameters": {"gain": 0, "inverted": True},
            },
            "main_hp": {
                "type": "Biquad",
                "parameters": {"type": "Highpass", "freq": 80, "q": 0.707107},
            },
            "main_peq": {
                "type": "Biquad",
                "parameters": {"type": "Peaking", "freq": 1000, "q": 0.707107, "gain": -2.5},
            },
        },
        "mixers": {
            "stereo": {
                "mapping": [
                    {"dest": 0, "sources": [{"channel": 0, "inverted": False}]},
                    {"dest": 1, "sources": [{"channel": 1, "inverted": True}]},
                ],
            },
        },
        "pipeline": [
            {"type": "Filter", "channels": [0], "names": ["input_delay", "input_invert"]},
            {"type": "Mixer", "name": "stereo"},
            {"type": "Filter", "channels": [0, 1], "names": ["main_hp"]},
            {"type": "Filter", "channels": [1], "names": ["main_peq"]},
        ],
    }

    settings = WebPlayerService._load_camilladsp_channel_settings(
        config=config,
        capture_channel_count=2,
        playback_channel_count=2,
    )

    assert settings["capture"][0] == {
        "frequency_filters": [],
        "delay_ms": 0.5,
        "phase_deg": 180.0,
    }
    assert settings["capture"][1]["phase_deg"] == 0.0
    assert settings["playback"][0] == {
        "frequency_filters": [{"kind": "Highpass", "frequency_hz": 80.0, "gain_db": None, "q": 0.707107}],
        "delay_ms": 0.0,
        "phase_deg": 0.0,
    }
    assert settings["playback"][1]["frequency_filters"][1] == {
        "kind": "Peaking",
        "frequency_hz": 1000.0,
        "gain_db": -2.5,
        "q": 0.707107,
    }
    assert settings["playback"][1]["phase_deg"] == 180.0


def test_camilladsp_playback_response_calculates_filter_magnitude_and_path_label() -> None:
    config = {
        "devices": {"samplerate": 48000},
        "filters": {
            "main_hp": {
                "type": "Biquad",
                "parameters": {"type": "Highpass", "freq": 80, "q": 0.707107},
            },
            "main_gain": {
                "type": "Gain",
                "parameters": {"gain": 1.5},
            },
            "main_volume": {
                "type": "Volume",
                "parameters": {"fader": "Main Left"},
            },
        },
        "mixers": {
            "stereo": {
                "mapping": [
                    {"dest": 0, "sources": [{"channel": 0, "gain": 0.0}]},
                ],
            },
        },
        "pipeline": [
            {"type": "Mixer", "name": "stereo"},
            {"type": "Filter", "channels": [0], "names": ["main_hp"]},
            {
                "type": "Filter",
                "channels": [0],
                "names": ["main_gain", "main_volume"],
                "description": "Output 1 / Mains Left",
            },
        ],
    }

    label, response, status = WebPlayerService._load_camilladsp_playback_response(
        config=config,
        channel_index=0,
        sample_rate=48000,
        fader_values=[{"volume": -11.5, "mute": False}],
    )

    assert status == "ready"
    assert label == "Mains Left"
    assert len(response) == 101
    cutoff_response = min(response, key=lambda point: abs(point["frequency_hz"] - 80))
    assert cutoff_response["level_db"] == pytest.approx(-13.0, abs=0.1)


def test_camilladsp_metadata_reads_signal_levels_and_active_config(monkeypatch) -> None:
    import websockets.sync.client as websocket_client

    configured_scope = []
    config = {
        "devices": {
            "capture": {"channels": 1},
            "playback": {"channels": 1},
            "samplerate": 48000,
        },
        "filters": {
            "main_hp": {
                "type": "Biquad",
                "parameters": {"type": "Highpass", "freq": 80, "q": 0.707107},
            },
        },
        "mixers": {
            "mono": {
                "mapping": [
                    {"dest": 0, "sources": [{"channel": 0, "inverted": False}]},
                ],
            },
        },
        "pipeline": [
            {"type": "Mixer", "name": "mono"},
            {"type": "Filter", "channels": [0], "names": ["main_hp"]},
        ],
    }

    class FakeWebSocket:
        def __init__(self) -> None:
            self.responses = {
                "GetSignalLevels": {
                    "result": "Ok",
                    "value": {
                        "capture_rms": [-12.0],
                        "capture_peak": [-3.0],
                        "playback_rms": [-8.0],
                        "playback_peak": [-1.0],
                    },
                },
                "GetConfigJson": {
                    "result": "Ok",
                    "value": json.dumps(config),
                },
                "GetFaders": {
                    "result": "Ok",
                    "value": [],
                },
            }
            self.pending_response = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def send(self, raw_command: str) -> None:
            command = json.loads(raw_command)
            self.pending_response = json.dumps({command: self.responses[command]})

        def recv(self, *, timeout: float) -> str:
            return self.pending_response

    def fake_connect(websocket_url: str, **_kwargs):
        assert websocket_url == "ws://127.0.0.1:1234"
        return FakeWebSocket()

    monkeypatch.delenv("WEBPLAYER_CAMILLADSP_WS_URL", raising=False)
    monkeypatch.delenv("WEBPLAYER_SCOPE_INPUT_SOURCE", raising=False)
    monkeypatch.setattr(websocket_client, "connect", fake_connect)
    monkeypatch.setattr(
        webplayer_module.PLAYBACK_SCOPE_SERVICE,
        "configure",
        lambda **kwargs: configured_scope.append(kwargs),
    )

    metadata = WebPlayerService._load_camilladsp_metadata()
    data = json.loads(metadata["audio_aux_dsp_data"])

    assert metadata["audio_aux_dsp_status"] == "ready"
    assert configured_scope == [
        {
            "playback_device": config["devices"]["playback"],
            "input_source": "alde-dsp-input.monitor",
        }
    ]
    assert data["capture"] == [{"rms_db": -12.0, "peak_db": -3.0}]
    assert data["playback"] == [{"rms_db": -8.0, "peak_db": -1.0}]
    assert data["playback_settings"][0]["frequency_filters"] == [
        {"kind": "Highpass", "frequency_hz": 80.0, "gain_db": None, "q": 0.707107}
    ]
    assert data["playback_settings"][0]["path_label"] == "Output 1"
    assert data["playback_settings"][0]["response_status"] == "ready"
    assert len(data["playback_settings"][0]["response_curve"]) == 101


def test_load_object_metadata_prefers_cdp_quality_and_artwork() -> None:
    service = WebPlayerService()

    metadata = service.load_object_metadata(
        "art_url=file:///tmp/.org.chromium.Chromium-missing\n"
        "cdp_stream_quality=MAX\n"
        "cdp_quality_resolution=24-bit 96 kHz\n"
        "cdp_art_url=https://resources.tidal.com/images/example/640x640.jpg\n"
    )

    assert metadata.get("stream_quality") == "MAX"
    assert metadata.get("quality_resolution") == "24-bit 96 kHz"
    assert metadata.get("bit_depth") == 24
    assert metadata.get("sample_rate_khz") == 96
    assert metadata.get("art_url") == "https://resources.tidal.com/images/example/640x640.jpg"


def test_load_object_metadata_keeps_cdp_track_fields_consistent() -> None:
    service = WebPlayerService()

    metadata = service.load_object_metadata(
        "title=Stale MPRIS title\n"
        "artist=Stale MPRIS artist\n"
        "album=Stale MPRIS album\n"
        "cdp_title=Current TIDAL title\n"
        "cdp_artist=Current TIDAL artist\n"
        "cdp_album=Current TIDAL album\n"
        "cdp_tidal_track_id=18140927\n"
    )

    assert metadata.get("title") == "Current TIDAL title"
    assert metadata.get("artist") == "Current TIDAL artist"
    assert metadata.get("album") == "Current TIDAL album"
    assert metadata.get("tidal_track_id") == "18140927"


def test_browser_metadata_enrichment_uses_catalog_manifest_and_collection_reads() -> None:
    track_resource = {
        "type": "tracks",
        "id": "5526443",
        "attributes": {
            "title": "API title",
            "key": "C",
            "keyScale": "MAJOR",
            "duration": "PT3M",
            "explicit": True,
            "bpm": 128,
            "mediaTags": ["LOSSLESS"],
        },
        "relationships": {
            "artists": {"data": [{"type": "artists", "id": "artist-1"}]},
            "albums": {"data": [{"type": "albums", "id": "album-1"}]},
        },
    }
    api_service = TidalApiMetadataServiceStub(
        {
            "tidal_api_track": {
                "data": track_resource,
                "included": [
                    {
                        "type": "artists",
                        "id": "artist-1",
                        "attributes": {"name": "API artist"},
                    },
                    {
                        "type": "albums",
                        "id": "album-1",
                        "attributes": {"name": "API album"},
                        "relationships": {
                            "coverArt": {"data": [{"type": "coverArt", "id": "cover-1"}]},
                        },
                    },
                    {
                        "type": "coverArt",
                        "id": "cover-1",
                        "attributes": {
                            "files": [{"href": "https://resources.tidal.com/images/example/640x640.jpg"}],
                        },
                    },
                ],
            },
            "tidal_api_track_manifest": {
                "data": {
                    "type": "trackManifests",
                    "id": "5526443",
                    "attributes": {
                        "audioQuality": "LOSSLESS",
                        "audioMode": "STEREO",
                        "bitrate": 1411,
                        "sampleRate": 96000,
                        "bitDepth": 24,
                        "channelCount": 2,
                        "codec": "FLAC",
                        "albumAudioNormalizationData": {
                            "replayGain": -11.37,
                            "peakAmplitude": 1.0,
                        },
                        "trackAudioNormalizationData": {
                            "replayGain": -10.39,
                            "peakAmplitude": 0.991394,
                        },
                    },
                },
            },
            "tidal_api_request": {
                "data": {
                    "type": "userCollectionTracks",
                    "id": "me",
                    "relationships": {
                        "items": {"data": [{"type": "tracks", "id": "5526443"}]},
                    },
                },
            },
        }
    )
    service = WebPlayerService(tidal_api_service=api_service)

    metadata = service.load_object_metadata(
        "status=Playing\n"
        "title=Browser title\n"
        "artist=Browser artist\n"
        "track_url=https://listen.tidal.com/track/5526443\n"
        "art_url=file:///tmp/browser-art\n"
        "volume_percent=42\n",
        enrich_api_metadata=True,
    )

    assert metadata["tidal_track_id"] == "5526443"
    assert metadata["title"] == "Browser title"
    assert metadata["artist"] == "Browser artist"
    assert metadata["album"] == "API album"
    assert metadata["art_url"] == "https://resources.tidal.com/images/example/640x640.jpg"
    assert metadata["stream_quality"] == "LOSSLESS"
    assert metadata["audio_mode"] == "STEREO"
    assert metadata["track_replay_gain_db"] == -10.39
    assert metadata["track_peak_amplitude"] == 0.991394
    assert metadata["album_replay_gain_db"] == -11.37
    assert metadata["album_peak_amplitude"] == 1
    assert metadata["dsp"] == (
        "TIDAL API | Track RG -10.39 dB | Track peak 0.991394 | Album RG -11.37 dB | Album peak 1"
    )
    assert metadata["quality_resolution"] == "24-bit 96 kHz"
    assert metadata["sample_rate_khz"] == 96
    assert metadata["tonart_display"] == "C Dur | C | 8B"
    assert metadata["liked"] is True
    assert metadata["liked_state"] == "liked"
    assert "uri" not in metadata
    assert "manifest" not in metadata
    assert [call[0] for call in api_service.calls] == [
        "tidal_api_track",
        "tidal_api_track_manifest",
        "tidal_api_request",
    ]


def test_browser_metadata_enrichment_resolves_track_with_catalog_search() -> None:
    track_resource = {
        "type": "tracks",
        "id": "18140927",
        "attributes": {"title": "Search title"},
        "relationships": {
            "artists": {"data": [{"type": "artists", "id": "artist-2"}]},
        },
    }
    api_service = TidalApiMetadataServiceStub(
        {
            "tidal_api_search": {
                "data": {
                    "type": "searchResults",
                    "id": "search-1",
                    "relationships": {
                        "tracks": {"data": [{"type": "tracks", "id": "18140927"}]},
                    },
                },
                "included": [
                    track_resource,
                    {
                        "type": "artists",
                        "id": "artist-2",
                        "attributes": {"name": "Search artist"},
                    },
                ],
            },
            "tidal_api_track": {
                "data": track_resource,
                "included": [
                    {
                        "type": "artists",
                        "id": "artist-2",
                        "attributes": {"name": "Search artist"},
                    },
                ],
            },
        }
    )
    service = WebPlayerService(tidal_api_service=api_service)

    metadata = service.load_object_metadata(
        "status=Playing\ntitle=Search title\nartist=Search artist\n",
        enrich_api_metadata=True,
    )

    assert metadata["tidal_track_id"] == "18140927"
    assert metadata["track_url"] == "https://listen.tidal.com/track/18140927"
    assert metadata["artist"] == "Search artist"
    assert metadata["api_metadata_source"] == "tidal_api"
    assert [call[0] for call in api_service.calls] == [
        "tidal_api_search",
        "tidal_api_track",
        "tidal_api_track_manifest",
        "tidal_api_request",
    ]


def test_now_playing_command_reads_mpris_volume() -> None:
    command = WebPlayerService()._load_now_playing_command(
        player_selector="chromium",
        arguments={},
    )

    assert 'playerctl -p "$selected_player" position' in command
    assert 'playerctl -p "$selected_player" metadata mpris:length' in command
    assert "echo position_seconds=" in command
    assert "echo duration_seconds=" in command
    assert 'playerctl -p "$selected_player" volume' in command
    assert "echo volume_percent=" in command
    assert "sampleRate" in command
    assert "bitWidth" in command
    assert "channelCount" in command
    assert "audioCodec" in command
    assert "pactl" not in command


def test_load_object_metadata_keeps_mpris_progress_fields() -> None:
    metadata = WebPlayerService().load_object_metadata(
        "position_seconds=37.25\n"
        "duration_seconds=202.5\n"
    )

    assert metadata["position_seconds"] == "37.25"
    assert metadata["duration_seconds"] == "202.5"


def test_browser_volume_adjust_uses_browser_controller() -> None:
    service = WebPlayerService(browser_backend_enabled=True)

    command = service._load_volume_adjust_command(
        player_selector="chromium",
        arguments={
            "delta_percent": 5,
            "cdp_autoclick": True,
            "cdp_port": 9222,
        },
    )

    assert "CDP_ACTION=volume_adjust" in command
    assert "pactl" not in command
    assert "controller=wplctl" in command


def test_browser_volume_uses_mpris_when_cdp_is_unavailable(monkeypatch, tmp_path) -> None:
    playerctl_path = tmp_path / "playerctl"
    volume_path = tmp_path / "volume"
    volume_path.write_text("1.000000")
    playerctl_path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  -l) echo chromium.instance1; exit 0;;\n"
        "  -p) case \"$3\" in\n"
        "    status) echo Playing;;\n"
        "    volume) if [ -n \"$4\" ]; then printf '%s' \"$4\" > \"$VOLUME_FILE\"; else cat \"$VOLUME_FILE\"; fi;;\n"
        "  esac;;\n"
        "esac\n"
    )
    playerctl_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("VOLUME_FILE", str(volume_path))
    service = WebPlayerService(browser_backend_enabled=True)
    monkeypatch.setattr(service, "_load_cdp_volume_command", lambda **_kwargs: "false; ")
    monkeypatch.setattr(service, "_load_cdp_volume_adjust_command", lambda **_kwargs: "false; ")

    for object_name, arguments, expected_percent in (
        ("webplayer_volume_set", {"volume_percent": 35}, "35"),
        ("webplayer_volume_adjust", {"delta_percent": -5}, "30"),
    ):
        command = service.load_object_command(
            object_name=object_name,
            query=None,
            player_selector="chromium",
            arguments=arguments,
        )
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert "controller=playerctl" in result.stdout
        assert f"volume_percent={expected_percent}" in result.stdout
        assert round(float(volume_path.read_text()) * 100) == int(expected_percent)


def test_cdp_volume_expression_applies_set_and_adjust() -> None:
    service = WebPlayerService(browser_backend_enabled=True)
    set_expression = service._load_cdp_volume_sync_expression(target_volume_expression="0.35")
    adjust_expression = service._load_cdp_volume_sync_expression(
        target_volume_expression="(readAppliedVolume() === null ? 0 : readAppliedVolume()) + (-0.05)"
    )
    script = (
        "const media={volume:1}; const store=new Map();"
        "globalThis.window={localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},requestAnimationFrame:()=>{}};"
        "globalThis.document={querySelectorAll:()=>[media],addEventListener:()=>{},documentElement:{}};"
        "globalThis.MutationObserver=class{observe(){}};"
        f"const first=eval({json.dumps(set_expression)});"
        f"const second=eval({json.dumps(adjust_expression)});"
        "console.log(JSON.stringify({first,second,volume:media.volume,stored:store.get('alde-webplayer-volume-percent')}));"
    )
    try:
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        pytest.skip("Node.js is unavailable")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["first"]["ok"] is True
    assert payload["second"]["ok"] is True
    assert payload["volume"] == pytest.approx(0.30)
    assert payload["stored"] == "30"


def test_browser_volume_commands_persist_across_track_changes() -> None:
    service = WebPlayerService(browser_backend_enabled=True)

    set_command = service._load_volume_set_command(
        player_selector="chromium",
        arguments={"volume_percent": 35},
    )
    adjust_command = service._load_volume_adjust_command(
        player_selector="chromium",
        arguments={"delta_percent": -5},
    )

    for command in (set_command, adjust_command):
        assert "window.__aldeVolumeSync" in command
        assert "MutationObserver" in command
        assert "localStorage" in command
        assert "loadedmetadata" in command
        assert "canplay" in command
        assert "play" in command

    assert "readAppliedVolume()" in adjust_command
    assert "stored_volume_only" in set_command
    assert "stored_volume_only" in adjust_command


def test_system_volume_adjust_uses_wpctl_default_sink(monkeypatch, tmp_path) -> None:
    wpctl_path = tmp_path / "wpctl"
    volume_path = tmp_path / "system-volume"
    volume_path.write_text("0.50")
    wpctl_path.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  get-volume) printf 'Volume: %s\\n' \"$(cat \"$VOLUME_FILE\")\";;\n"
        "  set-volume) printf '%s' \"$3\" > \"$VOLUME_FILE\";;\n"
        "  *) exit 1;;\n"
        "esac\n"
    )
    wpctl_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("VOLUME_FILE", str(volume_path))

    service = WebPlayerService(browser_backend_enabled=True)
    command = service._load_system_volume_adjust_command(arguments={"delta_percent": 5})
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "controller=wpctl" in result.stdout
    assert "volume_control=system" in result.stdout
    assert "sink=@DEFAULT_AUDIO_SINK@" in result.stdout
    assert "volume_percent=55" in result.stdout
    assert volume_path.read_text() == "0.550"


def test_cdp_probe_extracts_bit_depth_and_sample_rate() -> None:
    command = WebPlayerService()._load_cdp_now_playing_probe_command(
        cdp_port=9222,
        cdp_probe_timeout_s=2,
        cdp_probe_debug=True,
    )

    assert "bitDepth" in command
    assert "sampleRate" in command
    assert "quality_resolution=" in command
    assert "cdp_quality_resolution=" in command
    assert "qualityResolution" in command


def test_generated_cdp_quality_resolution_parser_returns_audio_parameters() -> None:
    command = WebPlayerService()._load_cdp_now_playing_probe_command(
        cdp_port=9222,
        cdp_probe_timeout_s=2,
        cdp_probe_debug=False,
    )
    generated_script = command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    parsed_module = ast.parse(generated_script, filename="generated_cdp_probe.py")
    parser_node = next(
        node
        for node in parsed_module.body
        if isinstance(node, ast.FunctionDef) and node.name == "load_quality_resolution"
    )
    parser_namespace: dict[str, object] = {"re": re}
    exec(
        compile(
            ast.Module(body=[parser_node], type_ignores=[]),
            filename="generated_cdp_probe.py",
            mode="exec",
        ),
        parser_namespace,
    )
    load_quality_resolution = parser_namespace["load_quality_resolution"]

    assert load_quality_resolution('{"bitDepth":24,"sampleRate":96000}') == "24-bit 96 kHz"
    assert load_quality_resolution('{"bit_depth":16,"sample_rate":44100}') == "16-bit 44.1 kHz"
    assert load_quality_resolution("codec=FLAC 24-bit 44.1 kHz") == "24-bit 44.1 kHz"


def test_spectrum_bars_parser_accepts_graph_data_values() -> None:
    raw_payload = {
        "graph_data": {
            "values": [0.0, 2.0, 4.0, 2.0, 0.5],
        }
    }

    bars = McpHttpRequestHandler._load_spectrum_bars_from_payload(raw_payload)

    assert bars
    assert len(bars) == 48
    assert min(bars) >= 0.0
    assert max(bars) <= 1.0


def test_spectrum_bars_parser_accepts_plain_text_numbers() -> None:
    raw_text = "0.1 0.4 0.9 0.2 0.05"

    bars = McpHttpRequestHandler._load_spectrum_bars_from_payload(raw_text)

    assert bars
    assert len(bars) == 48
    assert min(bars) >= 0.0
    assert max(bars) <= 1.0


def test_spectrum_payload_for_player_reads_configured_file(monkeypatch, tmp_path) -> None:
    spectrum_file = tmp_path / "spectrum_graph.json"
    spectrum_file.write_text(json.dumps({"graph_data": {"values": [1, 2, 3, 4, 5]}}), encoding="utf-8")

    monkeypatch.setenv("WEBPLAYER_EQ_ANALYZER_FILE", str(spectrum_file))
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_URL", raising=False)
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_CMD", raising=False)

    payload = McpHttpRequestHandler._load_spectrum_payload_for_player(player_selector="chromium")

    assert payload.get("ok") is True
    assert payload.get("source") == "file"
    assert len(payload.get("bars") or []) == 48


def test_spectrum_payload_for_player_falls_back_to_synthetic(monkeypatch) -> None:
    monkeypatch.setenv("WEBPLAYER_EQ_ANALYZER_FILE", "/tmp/this_file_should_not_exist_aieq.json")
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_URL", raising=False)
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_CMD", raising=False)

    payload = McpHttpRequestHandler._load_spectrum_payload_for_player(player_selector="chromium")

    assert payload.get("ok") is True
    assert payload.get("source") == "synthetic"
    assert len(payload.get("bars") or []) == 48


def test_spectrum_payload_for_player_prefers_url_autodiscovery(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_URL", raising=False)
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_CMD", raising=False)
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_FILE", raising=False)

    def _fake_url_loader(*, analyzer_url: str, player_selector: str):
        if analyzer_url.endswith("/api/spectrum_stream"):
            return {
                "ok": True,
                "source": "url",
                "url": analyzer_url,
                "bars": [0.1, 0.4, 0.7, 0.2],
                "raw": {"bins_l": [0.1, 0.4, 0.7, 0.2]},
            }
        return {
            "ok": False,
            "source": "url",
            "bars": [],
            "error": "failed",
        }

    monkeypatch.setattr(McpHttpRequestHandler, "_load_spectrum_payload_from_url", staticmethod(_fake_url_loader))

    payload = McpHttpRequestHandler._load_spectrum_payload_for_player(player_selector="chromium")

    assert payload.get("ok") is True
    assert payload.get("source") == "url_autodiscovery"
    assert str(payload.get("url") or "").endswith("/api/spectrum_stream")
    assert len(payload.get("bars") or []) == 48


def test_spectrum_payload_autodiscovery_prefers_stable_snapshot(monkeypatch) -> None:
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_URL", raising=False)
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_CMD", raising=False)
    monkeypatch.delenv("WEBPLAYER_EQ_ANALYZER_FILE", raising=False)

    def _fake_url_loader(*, analyzer_url: str, player_selector: str):
        _ = player_selector
        return {
            "ok": True,
            "source": "url",
            "url": analyzer_url,
            "bars": [0.1, 0.4, 0.7, 0.2],
            "raw": {"values": [0.1, 0.4, 0.7, 0.2]},
        }

    monkeypatch.setattr(McpHttpRequestHandler, "_load_spectrum_payload_from_url", staticmethod(_fake_url_loader))

    payload = McpHttpRequestHandler._load_spectrum_payload_for_player(player_selector="chromium")

    assert payload.get("ok") is True
    assert str(payload.get("url") or "").endswith("/api/snapshot")


def test_now_playing_probe_command_contains_liked_state_detection() -> None:
    command = WebPlayerService()._load_cdp_now_playing_probe_command(
        cdp_port=9222,
        cdp_probe_timeout_s=2,
        cdp_probe_debug=False,
    )

    assert "likedState" in command
    assert "liked_state=" in command
    assert "button[class*=\"heart\" i]" in command
    assert "const mediaTrackId = matchTrackId(nowPlayingMetadata && nowPlayingMetadata.trackUrl);" in command
    assert "trackId: loadTrackId(nowPlaying)" in command


def test_browser_open_command_navigates_existing_cdp_target() -> None:
    command = WebPlayerService()._load_browser_open_command(
        target_url="https://tidal.com/track/274309512",
        log_file_path="/tmp/webplayer_mcp_test.log",
        allow_xdg_open_fallback=False,
        cdp_port=9222,
    )

    assert "http://127.0.0.1:9222/json/version" in command
    assert "Page.navigate" in command
    assert "profile_mode=existing_cdp" in command
    assert "--remote-debugging-port=9222" in command


@pytest.mark.parametrize(
    ("object_name", "query", "arguments"),
    [
        ("webplayer_search_play", "bleeding corpse", {}),
        ("webplayer_playlist_play", None, {"playlist_id": "123"}),
        ("webplayer_library_play", None, {}),
        ("webplayer_open_playback_target", None, {"target_url": "https://tidal.com/track/285839829"}),
    ],
)
def test_open_playback_paths_disable_cdp_by_default(object_name, query, arguments) -> None:
    command = WebPlayerService().load_object_command(
        object_name=object_name,
        query=query,
        player_selector="chromium",
        arguments=arguments,
    )

    assert "CDP_ACTION=" not in command


def test_open_url_and_play_does_not_block_cdp_on_missing_mpris_player() -> None:
    command = WebPlayerService()._load_open_url_and_play_command(
        target_url="https://tidal.com/track/285839829",
        player_selector="chromium",
        wait_for_page_s=4,
        wait_for_player_s=15,
        play_attempts=8,
        cdp_action="generic",
        cdp_query="",
        cdp_autoclick=True,
        cdp_port=9222,
        cdp_click_timeout_s=12,
    )

    assert "if command -v playerctl" in command
    assert "[ -n \"$(playerctl -l 2>/dev/null || true)\" ]" in command
    assert command.index("CDP_ACTION=generic") > command.index("if command -v playerctl")


def test_load_object_metadata_formats_tonart_dur_moll_alpha_numeric() -> None:
    service = WebPlayerService()
    metadata = service.load_object_metadata("status=Playing\ntonart=C# minor\n")

    assert metadata.get("tonart_raw") == "C# minor"
    assert metadata.get("tonart_mode") == "moll"
    assert metadata.get("tonart_dur_moll") == "C# Moll"
    assert metadata.get("tonart_alpha") == "C#m"
    assert metadata.get("tonart_numeric") == "12A"
    assert metadata.get("tonart_display") == "C# Moll | C#m | 12A"


def test_load_object_metadata_formats_camelot_to_dur_moll_alpha_numeric() -> None:
    service = WebPlayerService()
    metadata = service.load_object_metadata("status=Playing\ntonart=8B\n")

    assert metadata.get("tonart_raw") == "8B"
    assert metadata.get("tonart_mode") == "dur"
    assert metadata.get("tonart_dur_moll") == "C Dur"
    assert metadata.get("tonart_alpha") == "C"
    assert metadata.get("tonart_numeric") == "8B"
    assert metadata.get("tonart_display") == "C Dur | C | 8B"


def test_load_object_metadata_rejects_audio_source_as_tonart() -> None:
    service = WebPlayerService()
    metadata = service.load_object_metadata(
        "status=Playing\n"
        "tonart=audio_source=Dummy Source for Restricted Clients\n"
        "audio_source=Dummy Source for Restricted Clients\n"
    )

    assert "tonart" not in metadata
    assert "tonart_raw" not in metadata
    assert metadata.get("audio_source") == "Dummy Source for Restricted Clients"


def test_jsonrpc_resources_read_returns_registered_resource_without_ui_extension() -> None:
    service = WebPlayerMcpRequestService()
    payload = service.dispatch_object(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "resources/read",
            "params": {
                "uri": service._UI_RESOURCE_URI,
            },
        }
    )

    result = payload.get("result") or {}
    contents = result.get("contents") or []
    assert contents
    assert contents[0].get("uri") == service._UI_RESOURCE_URI
    assert contents[0].get("mimeType") == "text/html;profile=mcp-app"
