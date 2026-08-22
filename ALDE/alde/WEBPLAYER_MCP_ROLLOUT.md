# WebPlayer MCP Rollout (No SSH)

This rollout is meant to be executed **directly on host `192.168.0.48`**.

## Goal

- Move existing `live_server.py` from `:8766` to `:8767`
- Start a new standalone MCP server on `:8766`
- Provide MCP tools for:
  - play
  - search play
  - playlist play
  - library play
  - open playback target
  - heart/favorite current track
  - volume adjust
  - stop
  - forward
  - backward
  - now playing title metadata
  - search
  - direct TIDAL API calls for track, manifest, and Widevine data

## Files added in this repo

- `ALDE/alde/webplayer_mcp_server.py`
- `ALDE/alde/mcp_server.py` (ALDE MCP App UI host bridge and `ui://` resource)
- `ALDE/alde/mcp_net_server.py` (ALDE browser CORS preflight and endpoint discovery)
- `deploy/systemd-user/webplayer_mcp_8765.service`
- `deploy/systemd-user/live_server_8767.service`
- `deploy/systemd-user/webplayer_mcp_8766.service`

## 1) Install/update systemd user services

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd-user/webplayer_mcp_8765.service ~/.config/systemd/user/webplayer_mcp_8765.service
cp deploy/systemd-user/live_server_8767.service ~/.config/systemd/user/live_server.service
cp deploy/systemd-user/webplayer_mcp_8766.service ~/.config/systemd/user/webplayer_mcp.service
systemctl --user daemon-reload
```

## 2) Switch ports

```bash
systemctl --user disable --now live_server_8766.service 2>/dev/null || true
systemctl --user enable --now webplayer_mcp_8765.service
systemctl --user enable --now live_server.service
systemctl --user enable --now webplayer_mcp.service
```

If your previous live service has a different unit name, stop/disable that unit as well.

## 3) Health checks

```bash
curl -s http://127.0.0.1:8766/health
curl -s http://127.0.0.1:8767/ | head
```

## MCP App UI / WebApp extension path

The browser-facing WebPlayer MCP endpoint is `http://192.168.0.48:8766/mcp`. Its UI
extension is negotiated with `io.modelcontextprotocol/ui`; it exposes the widget
resource at `ui://webplayer/mini-controls.html`. The same contract is also
available on the ALDE MCP server at `ui://alde/operator-console.html`. A webapp
can verify the WebPlayer path without loading a local file:

```bash
curl -s -X POST http://192.168.0.48:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{"extensions":{"io.modelcontextprotocol/ui":{}}}}}'
```

The host bridge provides `window.mcp.callTool({name, arguments})` to the widget.
The widget falls back to the `/mcp` HTTP endpoint when it is opened standalone.
For browser-native fallback rendering, the same mini-controls app is also served
as plain HTTP at `/ui/webplayer/mini-controls` (alias:
`/ui/webplayer/mini-controls.html`) on the same local server. Open that path
directly when inline MCP App rendering is unavailable. The fallback view uses
icon buttons plus a scrolling now-playing strip instead of dumping raw tool
output into the page.
The favorite button saves the current track through the TIDAL collection API
using the configured account credentials and verifies the resulting collection
state. A transient `role="status"` overlay reports loading, success, and
concrete API or transport errors without restoring the removed permanent status
bar. When a cached access token expires, the server uses the configured CDP port
to open a temporary background TIDAL tab, captures a browser-authenticated API
request, validates its Bearer token through `/v1/sessions`, stores it atomically
with mode `0600`, and closes the temporary tab.
The volume buttons operate on the active Chromium sink-input via `pactl` so they
actually change the audible browser stream; `playerctl` remains a fallback path.
When inline rendering is unavailable, the same app can be delivered on localhost via
TCP transport using `127.0.0.1:8765` and the `webplayer_mcp_8765.service` unit.
The network server supports browser `OPTIONS` preflight and advertises the
`/mcp` and `/health` paths at its root endpoint.

### Rich now-playing metadata

The mini-controls normalize TIDAL quality tags to the current Player SDK values
(`LOW`, `HIGH`, `LOSSLESS`, or `HI_RES_LOSSLESS`). The Bit/Hz pill uses exact
playback-info or manifest values when available and otherwise shows a
quality-tier reference. Track metadata supplies album artwork, BPM, and musical
key. Lyrics are read from the official v2 `lyrics` relationship, with the
legacy authenticated lyrics endpoint as a fallback; synchronized LRC text is
advanced using the current MPRIS playback position.

TIDAL SDK reference: <https://tidal-music.github.io/tidal-sdk-web/index.html>

## 4) MCP checks

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

### tools/call examples

```bash
curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_play","arguments":{"player_selector":"chromium"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_stop","arguments":{"player_selector":"chromium"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_forward","arguments":{"player_selector":"chromium"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_backward","arguments":{"player_selector":"chromium"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_favorite_current_track","arguments":{"player_selector":"chromium"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_volume_adjust","arguments":{"player_selector":"chromium","delta_percent":5}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_now_playing","arguments":{"player_selector":"chromium"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_search","arguments":{"player_selector":"chromium","query":"meshuggah"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_search_play","arguments":{"player_selector":"chromium","query":"dark techno EBM"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_playlist_play","arguments":{"player_selector":"chromium","playlist_url":"https://listen.tidal.com/playlist/<ID>"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_library_play","arguments":{"player_selector":"chromium","section":"favorites_tracks"}}}'

curl -s -X POST http://127.0.0.1:8766/mcp \
  -H 'Content-Type: application/json' \
  -d '{"method":"tools/call","params":{"name":"webplayer_open_playback_target","arguments":{"player_selector":"chromium","target_url":"https://listen.tidal.com/album/<ID>"}}}'
```

## 5) IDE Agent MCP endpoint

Use:

- URL: `http://192.168.0.48:8766/mcp`
- Methods: `initialize`, `tools/list`, `prompts/list`, `prompts/get`, `tools/call`

## 6) Troubleshooting

```bash
systemctl --user status webplayer_mcp.service
journalctl --user -u webplayer_mcp.service -n 120 --no-pager

systemctl --user status live_server.service
journalctl --user -u live_server.service -n 120 --no-pager
```

If playback tools return `error=no_player`, start media playback in Chromium first.
