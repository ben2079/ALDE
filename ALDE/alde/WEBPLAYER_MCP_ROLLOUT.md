# WebPlayer MCP Rollout (Chromium browser backend via wplctl)

This rollout is meant to be executed **directly on host `192.168.0.48`**.

## Current backend release

The deployed WebPlayer MCP backend is version `1.1.0`. It registers
`webplayer_seek` in the MCP tool list and routes absolute positions
(`position_seconds`) and relative offsets (`delta_seconds`) through the
Chromium `wplctl`/`playerctl` control path. The HTTP response identifies the
release as `webplayer-mcp-http/1.1.0` in its `Server` header.

## Goal

- Move existing `live_server.py` from `:8766` to `:8767`
- Start a new standalone MCP server on `:8766`
- Provide MCP tools for:
  - play
  - search play
  - playlist play
  - library play
  - open playback target
  - stop
  - forward
  - backward
  - seek
  - now playing title metadata
  - search
  - direct TIDAL API calls for search, track, manifest, and Widevine data
- Use the Chromium WebPlayer through `wplctl` for playback controls and browser playback information.
- Use direct `tidal_api_*` tools for catalog information such as search, track metadata, manifests, and DRM responses; the authenticated playlist and collection tools are the only API write paths.
- Do not use the TIDAL SDK or direct API requests as a playback-control path.

## Files added in this repo

- `ALDE/alde/webplayer_mcp_server.py`
- `deploy/systemd-user/live_server_8767.service`
- `deploy/systemd-user/webplayer_mcp_8766.service`

## 1) Install/update systemd user services

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd-user/live_server_8767.service ~/.config/systemd/user/live_server.service
cp deploy/systemd-user/webplayer_mcp_8766.service ~/.config/systemd/user/webplayer_mcp.service
systemctl --user daemon-reload
```

## 2) Configure API credentials

The `webplayer_usrauth` MCP tool handles user authentication for browser-backed collection and like actions. Keep this file outside the repository and restrict it to the service user.

```bash
install -m 600 /dev/null ~/.config/webplayer-mcp.env
${EDITOR:-vi} ~/.config/webplayer-mcp.env
```

The file may contain:

```dotenv
WEBPLAYER_PLAYBACK_BACKEND=browser
WEBPLAYER_BROWSER_BACKEND_ENABLED=1
# Either use a static access token:
# TIDAL_API_AUTHORIZATION=Bearer <ACCESS_TOKEN>
# Or use the PKCE token store created by scripts/tidal_oauth_bootstrap.py:
TIDAL_API_CLIENT_ID=<TIDAL_APP_CLIENT_ID>
TIDAL_API_REDIRECT_URI=http://127.0.0.1:8768/callback
TIDAL_API_SCOPE=<APPROVED_USER_SCOPES>
TIDAL_API_TOKEN_FILE=/home/benjamin/.config/webplayer-mcp/tidal-token.json
# Persistent active playback sequence; keep runtime state outside the repository.
WEBPLAYER_PLAYLIST_FILE=/home/benjamin/.config/webplayer-mcp/playback-playlist.json
# Optional read-only CamillaDSP websocket; defaults to ws://127.0.0.1:1234.
WEBPLAYER_CAMILLADSP_WS_URL=ws://127.0.0.1:1234
# TIDAL_API_CLIENT_TOKEN=<CLIENT_TOKEN>
# TIDAL_API_COOKIE=<OPTIONAL_COOKIE>
# TIDAL_API_MANIFEST_FORMATS=AACLC
# TIDAL_API_MANIFEST_TYPE=HLS
```

### Playback oscilloscope and DSP amplitude matrix

The matrix opens as a two-trace scope for one pro-audio signal channel:
`IN_SOURCE_1 → OUT_SINK_1`. `in1` captures channel 1 from the pre-DSP
PipeWire monitor `alde-dsp-input.monitor`; `out1` captures the first channel
of the configured playback sink monitor after CamillaDSP. The latter is also
identified as physical `Out 2` in the UI to preserve the Scarlett output
numbering. The input monitor can be changed with `WEBPLAYER_SCOPE_INPUT_SOURCE`
(default `alde-dsp-input.monitor`). This is one channel observed at two points,
not the Scarlett's physical `alsa_input…pro-input-N` capture. The backend still
accepts up to six explicitly selected traces. Captures run through `parec` and
are released two seconds after the last request; the monitor channel count must
match the configured playback channel count. This is a digital signal tap, not
an acoustic measurement.

The oscilloscope panel has no heading, mode toggle, live-status line, or
waiting-for-PCM message; only failures appear above the waveform. Channel
selection and horizontal/vertical adjustments are grouped in a compact,
collapsed settings menu opened with a transparent, borderless gear button at
the bottom right; its panel opens upward. Axis tick labels sit 1 px from their
axis and share one font size; the scope clips overflow and disables scrollbars,
including on the page and in the settings panel. Live dB values sit inside the
plot at its left edge, aligned to each lane's top;
they use a muted color and are 1 px smaller than scale ticks. Scale ticks show
magnitudes without leading signs. The calculated DSP
response data is still collected by the service, but the UI no longer shows a
**Frequenzgang** view.

The service computes supported biquad responses from the active filter
configuration and includes configured mixer/fader gain. Unsupported or
incomplete path configurations are marked unavailable rather than replaced
with a fabricated curve. The read-only CamillaDSP websocket provides the
configuration and live meters; the service needs the Python `websockets`
package and an enabled CamillaDSP websocket endpoint. PipeWire/PulseAudio
capture tools (`pactl` and `parec`) are also needed for the live waveform.
Configured delay and polarity do not represent acoustic arrival time or
measured phase response.

### Persisted track sequence and analysis model

`WEBPLAYER_PLAYLIST_FILE` stores the active linear track sequence as JSON. The
current outer schema version is `2`; schema version `1` files remain readable
and receive empty analysis objects.

Each entry in `tracks` has a stable `tidal_track_id` and zero-based `position`.
The optional `analysis` object is deliberately separate from source metadata:

```json
{
  "schema_version": 2,
  "tracks": [
    {
      "position": 0,
      "tidal_track_id": "303",
      "analysis": {
        "schema_version": 1,
        "status": "complete",
        "analyzer": "tidal-feature-v1",
        "analyzed_at": "2026-08-06T05:00:00Z",
        "attributes": {"tempo_bpm": 142.5, "genre": "metal"},
        "parameters": {"window_seconds": 30},
        "weights": {"energy": 0.8, "transition": 1.0}
      }
    }
  ],
  "analysis": {
    "status": "partial",
    "attributes": {"sequence_length": 1},
    "parameters": {"transition_window": 2},
    "weights": {"coherence": 0.91}
  }
}
```

`attributes` contain observed or derived track/sequence values,
`parameters` record the configuration used to derive them, and `weights` are
finite numeric scores keyed by model component. The persistence layer validates
and normalizes weights but does not calculate them. A new track sequence clears
the previous sequence-level analysis; reloading or stopping the same sequence
keeps it intact.

`TIDAL_API_AUTHORIZATION` may also be a bare token; the server adds `Bearer`. Do not commit this file, put credentials in source, or send them through chat/tool payloads. The systemd unit loads it through `EnvironmentFile=-%h/.config/webplayer-mcp.env`.

### Manifest-driven collection generation

`WebPlayerCollectionRequest` in `agents_tools.py` is the Phase 2 request
contract. It accepts `section`, `target_profile`, `attribute_priority_list`,
`mode`, `alpha_by_attribute`, `rules`, `sequence_mode`, `top_k`, and optional
`start_playback` with `playback_backend` (`browser`). Attribute aliases
such as `key`, `tempo`, `liked-only`, and `eq-profile` are normalized before
scoring.

The generator preserves supplied `attribute_scores`; when they are absent it
derives nkn values from the target profile for key, BPM, genre, subgenre, and
EQ, combines nkn/mmk per attribute, applies normalized priority weights, then
filters and sequences the candidates. AgentsDB documents include the target
profile, rules, seed context, sequence mode, selected IDs, selected score
traces, and generation timestamp.

For browser playback, `webplayer_library_play` accepts an ordered `track_ids`
array and persists the generated queue. `wplctl` opens or reuses the Chromium
WebPlayer and starts the first selected track through its TIDAL track URL.

### OAuth Authorization Code + PKCE bootstrap

For user-owned resources such as collection tracks and likes, use TIDAL's Authorization Code flow with PKCE. The browser is used by `wplctl` for playback and once for user consent. TIDAL's endpoints are:

- Authorization: `https://login.tidal.com/authorize`
- Token: `https://auth.tidal.com/v1/oauth2/token`

Register `http://127.0.0.1:8768/callback` as the redirect URI in the TIDAL developer app and set the non-secret values above. Then create an SSH loopback tunnel in one local terminal:

```bash
ssh -N -L 8768:127.0.0.1:8768 benjamin@192.168.0.48
```

In a second terminal, start the one-time bootstrap on the remote host:

```bash
ssh benjamin@192.168.0.48 \
  'cd /home/benjamin/Vs_Code_Projects/Projects/ALDE_Projekt && \
   set -a && . ~/.config/webplayer-mcp.env && set +a && \
   python3 scripts/tidal_oauth_bootstrap.py --listen-host 127.0.0.1 --listen-port 8768'
```

Open the printed `authorization_url` in the local browser and complete TIDAL consent. The callback is delivered through the SSH tunnel and the refresh-capable token is written to `TIDAL_API_TOKEN_FILE` with mode `0600`. Restart the service after changing the environment file:

```bash
ssh -F /dev/null benjamin@192.168.0.48 'systemctl --user restart webplayer_mcp.service'
```

## 3) Switch ports

```bash
systemctl --user disable --now live_server_8766.service 2>/dev/null || true
systemctl --user enable --now live_server.service
systemctl --user enable --now webplayer_mcp.service
```

If your previous live service has a different unit name, stop/disable that unit as well.

## 4) Health checks

```bash
curl -s http://127.0.0.1:8766/health
curl -s http://127.0.0.1:8767/ | head
```

## 5) MCP checks

### initialize

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"initialize","params":{}}'
```

### tools/list

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/list","params":{}}'
```

### prompts/list

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"prompts/list","params":{}}'
```

### prompts/get

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"prompts/get","params":{"name":"webplayer_operator","arguments":{"player_selector":"chromium"}}}'
```

### tidal_api_search

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"tidal_api_search","arguments":{"query":"bleeding corpse"}}}'
```

### Add a catalog track to a playlist

The embedded WebPlayer UI does not expose catalog search controls. Catalog
discovery remains available through `tidal_api_search`, while
`tidal_api_add_track_to_playlist` performs the explicit playlist write. The
latter requires an authenticated user token with `collection.write` and
`w_usr`. Use the track ID returned by the search result and do not send
playback backend arguments to this API action:

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: tidal_api_add_track_to_playlist' \
  -d '{"jsonrpc":"2.0","id":42,"method":"tools/call","params":{"name":"tidal_api_add_track_to_playlist","arguments":{"playlist_id":"<PLAYLIST_ID>","track_id":"<TRACK_ID>"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}'
```

Without a stored user token, the server returns `user_auth_required`; run
`webplayer_usrauth` and complete the PKCE flow before retrying.

### tidal_api_track_manifest

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"tidal_api_track_manifest","arguments":{"track_id":"274309512"}}}'
```

If the result contains `api_playback_drm_required:WIDEVINE` or `api_playback_drm_required:FAIRPLAY`, use a supported DRM-capable client. Do not bypass the DRM layer.

### tools/call examples

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_play","arguments":{"player_selector":"chromium","playback_backend":"browser"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_stop","arguments":{"player_selector":"chromium","playback_backend":"browser"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_forward","arguments":{"player_selector":"chromium","playback_backend":"browser"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_backward","arguments":{"player_selector":"chromium","playback_backend":"browser"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_now_playing","arguments":{"player_selector":"chromium","playback_backend":"browser"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_search","arguments":{"player_selector":"chromium","playback_backend":"browser","query":"meshuggah"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_search_play","arguments":{"player_selector":"chromium","playback_backend":"browser","query":"dark techno EBM"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_playlist_play","arguments":{"player_selector":"chromium","playback_backend":"browser","playlist_url":"https://listen.tidal.com/playlist/<ID>"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_library_play","arguments":{"player_selector":"chromium","playback_backend":"browser","section":"favorites_tracks"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_open_playback_target","arguments":{"player_selector":"chromium","playback_backend":"browser","target_url":"https://listen.tidal.com/album/<ID>"}}}'
```

## 6) IDE Agent MCP endpoint

Use:

- URL: `http://192.168.0.48:8766/mcp`
- Methods: `initialize`, `tools/list`, `prompts/list`, `prompts/get`, `tools/call`

## 7) Browser controller origin

For local testing, tunnel the remote MCP server and open the UI through
`localhost`:

```bash
ssh -F /dev/null -N -L 8767:127.0.0.1:8766 benjamin@192.168.0.48
```

Open `http://localhost:8767/ui/Mpc.App.ui` in the MCP host or a browser. The
app sends all `webplayer_*` actions to the MCP server, which routes them through
the Chromium browser controller `wplctl`.

The canonical MCP App resource URI is `ui://Mpc.App.ui`. The previous URI,
`ui://WebMpc.app.ui`, remains readable as a compatibility alias for hosts with
cached tool metadata; new resource listings advertise only the canonical URI.
The previous HTTP scope path `/ui/WebMpc.app.ui/scope` also remains available
for UI documents cached before the rename.

## 8) Playback sequence and backend boundaries

The WebPlayer service persists the active playback sequence as an atomic JSON document at `WEBPLAYER_PLAYLIST_FILE` (default: `~/.config/webplayer-mcp/playback-playlist.json`). The document contains ordered TIDAL track references, stable track IDs, positions, title/artist/album metadata, source provenance, active index, status, backend, and timestamps. The file is created with mode `0600` in a directory with mode `0700`.

On service reload, the queue and active index are restored but playback is not started automatically. `webplayer_play` explicitly resumes the restored active track. A write failure does not terminate already-started audio; the response reports `playlist_persisted=false` and a `playlist_persistence_error`.

Backend responsibilities remain separate:

- `wplctl`: Chromium playback control and browser-derived now-playing, queue, like, volume, and quality information.
- TIDAL JSON API: catalog search and track/album/artist metadata, manifests, and DRM response inspection; authenticated playlist and collection writes are explicit account actions.
- API links such as `listen.tidal.com/track/<id>` are identifiers passed to `wplctl`; API tools do not control playback.
- EQ icon coverage is an asset/markup concern. When spectrum data is enabled, `/api/snapshot` is preferred as the stable JSON source, followed by `/api/spectrum_stream`, configured files/commands, and finally the existing synthetic fallback.

## Live PCM oscilloscope

The player and oscilloscope stay side by side in a 3:2 layout: 60% for the
player on the left and 40% for the scope/DSP panel on the right, excluding the
8 px gap. Charts scale to their panel instead of requiring a 620 px horizontal
scroll area. On narrow views, player controls and scope legends wrap within
their own columns; expanding cover art or system details keeps the same split.

The visible oscilloscope targets 30 updates per second, with at most one HTTP
request in flight. Responses are drawn on the next browser animation frame;
slow requests reduce the update rate rather than building up a queue. Inside
an MCP host the frames travel through the host bridge (`webplayer_playback_scope`
tool); standalone browser pages use the HTTP endpoint. Failures are shown in
the scope status and retried after 500 ms. Hiding the view or closing the app
stops polling and drawing; an outstanding request may finish, but its result is
not drawn and no new request is queued. Control changes apply to the next frame,
and the controls survive metadata refreshes.

Controls above the graticule:

- **HORIZONTAL** `t− [slider] t+`: timebase in 1-2-5 steps from 50 µs/div to
  20 ms/div across 10 divisions (0.5–200 ms window), with a dashed centre line.
- **VERTIKAL** defaults to **dB** at 24 dB/div so the pre-/post-DSP level
  difference remains visible; `− [value] +` and the **V / dB** toggle adjust it.
  V is linear and
  normalised to full scale (1 V ≙ 0 dBFS, 1 V/div to 1 mV/div). dB is a
  logarithmic dBFS scale (30 to 3 dB/div). Each lane spans four divisions.
- **CH1/CH2** show one signal channel at two points: `in1` (`IN_SOURCE_1`,
  before DSP) and `out1` (`OUT_SINK_1`, after DSP). The UI defaults to this pair;
  other source IDs remain available through the MCP scope request. The physical
  playback monitor number is included in parentheses and CamillaDSP path labels
  are appended to output names.

The client sends `channels`, `window_ms`, `points` and `trigger` to both the
HTTP endpoint (query string) and the `webplayer_playback_scope` tool
(arguments). The server records each source natively as `s32le` (24-bit
Scarlett samples in 32-bit frames, normalised by 2^31) at the PipeWire
graph rate. Bit depth, sample rate and trigger status remain in the scope
payload but are not displayed in the UI. A rising zero-crossing trigger on CH1
(`IN_SOURCE_1` by default) keeps periodic signals steady. Windows longer than
`points` (default 512) are
reduced to per-bucket min/max envelopes, so peaks are never lost. Each channel
also returns RMS and peak in dBFS. The Scarlett 2i4 supports at most 96 kHz
(not 192 kHz). The scope never changes the device or graph sample rate.

On the deployed Scarlett 2i4 system, PipeWire fixes its default and allowed
graph rates at 96 kHz in
`~/.config/pipewire/pipewire.conf.d/95-alde-audio-rate.conf`. CamillaDSP also
uses 96 kHz for capture and processing, with its own resampler disabled.
Streams presented to the graph at other rates are resampled to 96 kHz; this
does not imply that the source recording is native 96 kHz.

The deployed signal path is Chromium → virtual sink `alde-dsp-input` →
CamillaDSP (captures the sink monitor) → Scarlett. WirePlumber cannot link a
stream directly to another stream, so the virtual sink is required; WirePlumber
`stream.rules` only affect restore state, not link targets. The targets are
therefore set by PipeWire client rules, and both streams use
`node.dont-fallback` so neither falls back to the Scarlett output or inputs:

- `~/.config/pipewire/pipewire.conf.d/96-alde-dsp-input.conf` creates the
  96 kHz stereo `alde-dsp-input` sink; it does not become the default sink.
- `~/.config/pipewire/pipewire-pulse.conf.d/95-alde-browser-routing.conf`
  routes Chromium playback to `alde-dsp-input`.
- `~/.config/pipewire/client.conf.d/95-alde-camilladsp-capture.conf` makes
  CamillaDSP capture the `alde-dsp-input` monitor (`stream.capture.sink`).

Chromium capture and other applications retain their normal routing. After
restarting `pipewire` or `pipewire-pulse`, Chromium may keep playing silently
on a dead PulseAudio connection; reload the TIDAL tab to recreate its stream.

Large scope JSON responses use fast gzip compression when the client advertises
`Accept-Encoding: gzip`; clients requesting identity encoding keep the same
uncompressed response shape. This reduces network traffic without dropping
samples. The deployed MCP scope-tool transport uses the same compression policy.

This changes visualization only; it does not change music quality, routing,
or volume. Actual frame rate depends on capture, network, and host rendering
performance. Reload the MCP App resource to load the updated polling code.

## 9) Troubleshooting

```bash
systemctl --user status webplayer_mcp.service
journalctl --user -u webplayer_mcp.service -n 120 --no-pager

systemctl --user status live_server.service
journalctl --user -u live_server.service -n 120 --no-pager
```

For `api_http_401` or `authentication_required`, run `webplayer_usrauth` and
complete the PKCE callback, then reload the UI. Use the `localhost` SSH tunnel
above rather than the raw HTTP LAN address when the MCP host blocks LAN UI
requests. Entitlement and DRM errors are returned by the browser/API boundary;
there is no DRM bypass.
