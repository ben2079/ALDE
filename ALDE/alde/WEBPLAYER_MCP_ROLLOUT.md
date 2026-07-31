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
  - stop
  - forward
  - backward
  - now playing title metadata
  - search
  - direct TIDAL API calls for track, manifest, and Widevine data

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

## 2) Switch ports

```bash
systemctl --user disable --now live_server_8766.service 2>/dev/null || true
systemctl --user enable --now live_server.service
systemctl --user enable --now webplayer_mcp.service
```

If your previous live service has a different unit name, stop/disable that unit as well.

## 3) Health checks

```bash
curl -s http://127.0.0.1:8766/health
curl -s http://127.0.0.1:8767/ | head
```

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
