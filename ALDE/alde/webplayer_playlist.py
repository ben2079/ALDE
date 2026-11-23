"""Domain model and durable storage for the active WebPlayer playlist."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping


class PlaybackPlaylistPersistenceError(RuntimeError):
    """Raised when the playback playlist cannot be written safely."""


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    try:
        normalized = json.loads(json.dumps(dict(value), ensure_ascii=True))
    except (TypeError, ValueError):
        return {}
    return normalized if isinstance(normalized, dict) else {}


def _json_safe_weights(value: Any) -> dict[str, float]:
    normalized_weights: dict[str, float] = {}
    for key, raw_value in _json_safe_mapping(value).items():
        if isinstance(raw_value, bool):
            continue
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_value):
            normalized_weights[str(key)] = numeric_value
    return normalized_weights


@dataclass
class PlaybackAnalysis:
    status: str = "unanalysed"
    analyzer: str = ""
    analyzed_at: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "PlaybackAnalysis":
        if isinstance(value, PlaybackAnalysis):
            return cls(
                status=value.status,
                analyzer=value.analyzer,
                analyzed_at=value.analyzed_at,
                attributes=_json_safe_mapping(value.attributes),
                parameters=_json_safe_mapping(value.parameters),
                weights=_json_safe_weights(value.weights),
            )
        if not isinstance(value, Mapping):
            return cls()
        status = str(value.get("status") or "unanalysed").strip().casefold()
        if status not in {"unanalysed", "partial", "complete", "failed"}:
            status = "unanalysed"
        return cls(
            status=status,
            analyzer=str(value.get("analyzer") or "").strip(),
            analyzed_at=str(value.get("analyzed_at") or "").strip(),
            attributes=_json_safe_mapping(value.get("attributes")),
            parameters=_json_safe_mapping(value.get("parameters")),
            weights=_json_safe_weights(value.get("weights")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "analyzer": self.analyzer,
            "analyzed_at": self.analyzed_at,
            "attributes": _json_safe_mapping(self.attributes),
            "parameters": _json_safe_mapping(self.parameters),
            "weights": _json_safe_weights(self.weights),
        }


def _load_analysis_payload(payload: Mapping[str, Any]) -> Any:
    analysis = payload.get("analysis")
    if analysis is not None:
        return analysis
    direct_fields = {
        key: payload.get(key)
        for key in ("attributes", "parameters", "weights")
        if key in payload
    }
    return direct_fields


@dataclass
class PlaybackTrackReference:
    tidal_track_id: str
    position: int
    title: str = ""
    artist: str = ""
    album: str = ""
    track_url: str = ""
    duration: str = ""
    analysis: PlaybackAnalysis = field(default_factory=PlaybackAnalysis)
    source: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_track(
        cls,
        track: Mapping[str, Any],
        *,
        position: int,
        source: Mapping[str, Any] | None = None,
    ) -> "PlaybackTrackReference":
        track_id = str(track.get("tidal_track_id") or track.get("id") or "").strip()
        if not track_id:
            raise ValueError("playback_track_id_missing")

        known_keys = {
            "tidal_track_id",
            "id",
            "title",
            "artist",
            "album",
            "track_url",
            "duration",
            "analysis",
            "attributes",
            "parameters",
            "weights",
        }
        metadata = _json_safe_mapping({key: value for key, value in track.items() if key not in known_keys})
        return cls(
            tidal_track_id=track_id,
            position=max(0, int(position)),
            title=str(track.get("title") or "").strip(),
            artist=str(track.get("artist") or "").strip(),
            album=str(track.get("album") or "").strip(),
            track_url=str(track.get("track_url") or "").strip(),
            duration=str(track.get("duration") or "").strip(),
            analysis=PlaybackAnalysis.from_value(_load_analysis_payload(track)),
            source=_json_safe_mapping(source),
            metadata=metadata,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, fallback_position: int) -> "PlaybackTrackReference":
        track_id = str(payload.get("tidal_track_id") or payload.get("id") or "").strip()
        if not track_id:
            raise ValueError("playback_track_id_missing")
        try:
            position = int(payload.get("position"))
        except (TypeError, ValueError):
            position = fallback_position
        return cls(
            tidal_track_id=track_id,
            position=max(0, position),
            title=str(payload.get("title") or "").strip(),
            artist=str(payload.get("artist") or "").strip(),
            album=str(payload.get("album") or "").strip(),
            track_url=str(payload.get("track_url") or "").strip(),
            duration=str(payload.get("duration") or "").strip(),
            analysis=PlaybackAnalysis.from_value(_load_analysis_payload(payload)),
            source=_json_safe_mapping(payload.get("source")),
            metadata=_json_safe_mapping(payload.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": int(self.position),
            "tidal_track_id": self.tidal_track_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "track_url": self.track_url,
            "duration": self.duration,
            "analysis": self.analysis.to_dict(),
            "source": _json_safe_mapping(self.source),
            "metadata": _json_safe_mapping(self.metadata),
        }

    def to_track(self) -> dict[str, Any]:
        track = {
            "tidal_track_id": self.tidal_track_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "track_url": self.track_url,
            "duration": self.duration,
            "analysis": self.analysis.to_dict(),
        }
        track.update(_json_safe_mapping(self.metadata))
        return track


@dataclass
class PlaybackPlaylist:
    playlist_id: str = "active"
    name: str = "Playback Queue"
    tracks: list[PlaybackTrackReference] = field(default_factory=list)
    active_index: int = -1
    status: str = "stopped"
    backend: str = "api"
    analysis: PlaybackAnalysis = field(default_factory=PlaybackAnalysis)
    source: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_timestamp)
    updated_at: str = field(default_factory=_utc_timestamp)
    last_started_at: str = ""

    @classmethod
    def empty(cls) -> "PlaybackPlaylist":
        return cls()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlaybackPlaylist":
        raw_tracks = payload.get("tracks")
        tracks: list[PlaybackTrackReference] = []
        if isinstance(raw_tracks, list):
            for fallback_position, raw_track in enumerate(raw_tracks):
                if not isinstance(raw_track, Mapping):
                    continue
                try:
                    tracks.append(
                        PlaybackTrackReference.from_dict(
                            raw_track,
                            fallback_position=fallback_position,
                        )
                    )
                except ValueError:
                    continue
        tracks.sort(key=lambda item: item.position)
        for position, track in enumerate(tracks):
            track.position = position

        try:
            active_index = int(payload.get("active_index"))
        except (TypeError, ValueError):
            active_index = -1
        if not tracks:
            active_index = -1
        else:
            active_index = max(0, min(active_index, len(tracks) - 1))

        status = str(payload.get("status") or "stopped").strip().casefold()
        if status not in {"playing", "paused", "stopped"}:
            status = "stopped"
        return cls(
            playlist_id=str(payload.get("playlist_id") or "active").strip() or "active",
            name=str(payload.get("name") or "Playback Queue").strip() or "Playback Queue",
            tracks=tracks,
            active_index=active_index,
            status=status,
            backend=str(payload.get("backend") or "api").strip().casefold() or "api",
            analysis=PlaybackAnalysis.from_value(payload.get("analysis")),
            source=_json_safe_mapping(payload.get("source")),
            created_at=str(payload.get("created_at") or _utc_timestamp()).strip(),
            updated_at=str(payload.get("updated_at") or _utc_timestamp()).strip(),
            last_started_at=str(payload.get("last_started_at") or "").strip(),
        )

    def replace_tracks(
        self,
        tracks: list[Mapping[str, Any]],
        *,
        active_index: int,
        source: Mapping[str, Any] | None,
        backend: str,
        status: str,
        analysis: Mapping[str, Any] | PlaybackAnalysis | None = None,
    ) -> None:
        previous_track_ids = [track.tidal_track_id for track in self.tracks]
        normalized_tracks: list[PlaybackTrackReference] = []
        for position, track in enumerate(tracks):
            normalized_tracks.append(
                PlaybackTrackReference.from_track(
                    track,
                    position=position,
                    source=source,
                )
            )
        self.tracks = normalized_tracks
        self.active_index = -1 if not normalized_tracks else max(0, min(int(active_index), len(normalized_tracks) - 1))
        self.source = _json_safe_mapping(source)
        self.backend = str(backend or "api").strip().casefold() or "api"
        self.set_status(status)
        if previous_track_ids != [track.tidal_track_id for track in normalized_tracks]:
            self.analysis = PlaybackAnalysis.from_value(analysis)
            self.created_at = _utc_timestamp()
        elif analysis is not None:
            self.analysis = PlaybackAnalysis.from_value(analysis)

    def set_analysis(self, analysis: Mapping[str, Any] | PlaybackAnalysis) -> None:
        self.analysis = PlaybackAnalysis.from_value(analysis)
        self.updated_at = _utc_timestamp()

    def set_track_analysis(self, position: int, analysis: Mapping[str, Any] | PlaybackAnalysis) -> None:
        normalized_position = int(position)
        if normalized_position < 0 or normalized_position >= len(self.tracks):
            raise IndexError("playback_track_position_invalid")
        self.tracks[normalized_position].analysis = PlaybackAnalysis.from_value(analysis)
        self.updated_at = _utc_timestamp()

    def set_active_index(self, active_index: int) -> None:
        if not self.tracks:
            self.active_index = -1
        else:
            self.active_index = max(0, min(int(active_index), len(self.tracks) - 1))
        self.updated_at = _utc_timestamp()

    def set_status(self, status: str) -> None:
        normalized_status = str(status or "stopped").strip().casefold()
        if normalized_status not in {"playing", "paused", "stopped"}:
            normalized_status = "stopped"
        self.status = normalized_status
        now = _utc_timestamp()
        self.updated_at = now
        if normalized_status == "playing":
            self.last_started_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "playlist_id": self.playlist_id,
            "name": self.name,
            "tracks": [track.to_dict() for track in self.tracks],
            "active_index": int(self.active_index),
            "status": self.status,
            "backend": self.backend,
            "analysis": self.analysis.to_dict(),
            "source": _json_safe_mapping(self.source),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_started_at": self.last_started_at,
        }


class PlaybackPlaylistRepository:
    """Persist the active playback sequence without coupling it to AgentsDB."""

    DEFAULT_RELATIVE_PATH = ".config/webplayer-mcp/playback-playlist.json"

    def __init__(self, path: str | Path | None = None) -> None:
        raw_path = str(path or os.getenv("WEBPLAYER_PLAYLIST_FILE") or "").strip()
        if not raw_path:
            raw_path = str(Path.home() / self.DEFAULT_RELATIVE_PATH)
        self.path = Path(raw_path.replace("%h", str(Path.home()), 1)).expanduser().resolve()
        self._lock = RLock()
        self.last_load_error = ""

    def load_playlist(self) -> PlaybackPlaylist:
        with self._lock:
            try:
                raw_payload = self.path.read_text(encoding="utf-8")
                payload = json.loads(raw_payload)
                if not isinstance(payload, Mapping):
                    raise ValueError("playlist_payload_not_object")
                return PlaybackPlaylist.from_dict(payload)
            except FileNotFoundError:
                self.last_load_error = ""
                return PlaybackPlaylist.empty()
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                self.last_load_error = f"playlist_load_failed:{type(exc).__name__}"
                return PlaybackPlaylist.empty()

    def save_playlist(self, playlist: PlaybackPlaylist) -> None:
        if not isinstance(playlist, PlaybackPlaylist):
            raise PlaybackPlaylistPersistenceError("invalid_playlist")

        payload = playlist.to_dict()
        with self._lock:
            parent = self.path.parent
            temporary_name = ""
            try:
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(parent, 0o700)
                file_descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.",
                    dir=str(parent),
                )
                os.fchmod(file_descriptor, 0o600)
                with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
                os.chmod(self.path, 0o600)
            except Exception as exc:
                if temporary_name:
                    try:
                        os.unlink(temporary_name)
                    except OSError:
                        pass
                raise PlaybackPlaylistPersistenceError(
                    f"playlist_save_failed:{type(exc).__name__}"
                ) from exc