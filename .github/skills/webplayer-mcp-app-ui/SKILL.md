---
name: webplayer-mcp-app-ui
description: 'Use when opening, testing, or exposing the ALDE WebPlayer MCP App UI (ui://Mpc.App.ui) in an MCP agent host or chat, rendering it in the VS Code editor, or building and installing the optional VS Code Webview extension.'
argument-hint: 'Choose a target: agent-host/chat, VS Code editor, or extension installation; include the MCP endpoint when it differs from the default.'
user-invocable: true
---

# WebPlayer MCP App UI

## Purpose

Run the ALDE WebPlayer UI through the MCP Apps resource `ui://Mcp.App.ui`. Prefer the MCP App path for chat and agent hosts. The VS Code extension is an optional editor-specific WebviewPanel integration, not a prerequisite for MCP App rendering.

## Targets

Choose one target before acting:

- **Agent host or chat**: connect the host to `http://192.168.0.47:8766/mcp` or a local `/mcp` endpoint and let the host render the UI linked from the tool metadata.
- **VS Code editor without installing the extension**: use an MCP-capable VS Code host/client configured with the same MCP endpoint. The host must support MCP Apps; ordinary chat clients that only display text cannot render the iframe UI.
- **VS Code editor with the optional extension**: build and install `deploy/vscode-extension/webplayer-mcp-ui-*.vsix`. The extension creates a native `vscode.window.createWebviewPanel` and calls the server directly.

## MCP App Contract

The server must expose all of these behaviors:

1. `initialize` succeeds and advertises the UI extension/resource capability when the host supports MCP Apps.
2. `tools/list` returns each relevant `webplayer_*` tool with:

   ```json
   {
     "_meta": {
       "ui": {
         "resourceUri": "ui://Mpc.App.ui"
       }
     }
   }
   ```

3. `resources/read` for `ui://Mpc.App.ui` returns `mimeType: text/html;profile=mcp-app` and non-empty HTML.
4. A `tools/call` response still includes text or structured content for hosts without UI support.
5. HTTP clients from browser/webview hosts receive valid CORS headers and pass the MCP protocol/session headers required by the server.
6. The oscilloscope uses the MCP `webplayer_playback_scope` tool bridge when connected. Its HTTP fallback is a `GET` to `/ui/Mpc.App.ui/scope` on the configured MCP server origin; it is not an MCP `resources/read` request.

The UI should be rendered by the host after a linked `webplayer_*` tool call. Do not make the chat call `resources/read` as a substitute for a tool call unless testing the resource directly.

## Agent Host / Chat Workflow

1. Start or verify the MCP HTTP server:

   ```bash
   cd /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt
   WEBPLAYER_MCP_UI_EXTENSION_DEFAULT=1 PYTHONPATH="$PWD" \
     python3 -m ALDE.alde.webplayer_mcp_server \
     --transport http --host 0.0.0.0 --port 8766
   ```

   For the deployed service, use `http://192.168.0.47:8766/mcp`.

2. Configure the agent host's MCP server URL as the `/mcp` endpoint. Do not configure `/health` or the site root as the MCP endpoint.
3. Ask the host to call a WebPlayer tool, for example `webplayer_now_playing`, `webplayer_play`, or `webplayer_forward`.
4. Confirm that the host displays the MCP App UI rather than only the JSON/text result.
5. If the UI does not appear, inspect `tools/list` for `_meta.ui.resourceUri`, then test `resources/read` for the exact URI.

## VS Code Editor Without Extension

This path requires an MCP Apps-capable VS Code chat/agent host. Installing the extension is unnecessary when the host itself supports MCP Apps.

1. Register `http://192.168.0.47:8766/mcp` in the host's MCP configuration.
2. Start a WebPlayer tool call from the editor chat.
3. Verify that the chat host receives `ui://Mpc.App.ui` from the tool definition and renders its HTML resource.
4. If the editor only shows text, check host capability support first. A normal VS Code chat pane cannot render an MCP App unless its MCP integration implements the Apps UI lifecycle.

## Optional VS Code Extension

Use this path when a native editor panel or sidebar is required.

1. Install dependencies and compile:

   ```bash
   cd /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt/deploy/vscode-extension
   npm install
   npm run compile
   ```

2. Package the extension:

   ```bash
   npx @vscode/vsce package --no-dependencies
   ```

3. Install the generated VSIX into VS Code:

   ```bash
   code --install-extension ./webplayer-mcp-ui-*.vsix --force
   ```

4. Reload VS Code, run `TIDAL WebPlayer: Open MCP App UI` from the Command Palette, and verify the endpoint setting `webplayerMcp.endpoint`.
5. The extension uses the default endpoint `http://192.168.0.47:8766/mcp` unless `webplayerMcp.endpoint` is configured.

The extension is optional. Do not use it as evidence that the MCP App contract works in a chat host; validate the MCP endpoint independently.

## Remote Deployment

Deploy server changes from the repository root, not from `ALDE/`:

```bash
cd /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt
REMOTE_HOST=192.168.0.47 REMOTE_USER=benjamin \
  ./scripts/deploy_mcp_remote.sh
```

The deployment must finish with a successful `/health` response. If the script says it cannot find `scripts/deploy_mcp_remote.sh`, the current directory is wrong.

## TIDAL User Auth For Write Methods

TIDAL collection writes require user OAuth, not only a client token. Configure the service environment with the TIDAL application values:

```dotenv
TIDAL_API_CLIENT_ID=<client id>
TIDAL_API_REDIRECT_URI=<registered callback URI>
TIDAL_API_SCOPE=collection.write w_usr
TIDAL_API_TOKEN_FILE=%h/.config/webplayer-mcp/tidal-token.json
```

The redirect URI must exactly match the URI registered for the TIDAL application. Start authentication by calling `webplayer_usrauth`. Open the returned `authorization_url`, complete login, then call `webplayer_usrauth` again with the returned redirect URL (or authorization code). The PKCE verifier and OAuth state are kept by the server for the pending login.

Before a write method, verify that `webplayer_usrauth` reports `authentication_state: ready`. Write tools such as `tidal_api_add_track_to_playlist` otherwise return `user_auth_required` with the required scopes `collection.write` and `w_usr`.

Never commit the client secret, access token, refresh token, or token file. The token file is written with mode `0600`.

## Verification Checklist

- `GET /health` returns HTTP 200.
- `initialize` returns HTTP 200.
- `tools/list` contains the requested `webplayer_*` tool.
- That tool contains `_meta.ui.resourceUri == "ui://Mpc.App.ui"`.
- `resources/read` returns `text/html;profile=mcp-app` and non-empty HTML.
- A representative tool call returns a successful result and the host renders the UI.
- For webview/browser origins, preflight returns HTTP 204 and the response includes `Access-Control-Allow-Origin`.
- Run the focused MCP tests from `ALDE/`:

  ```bash
  cd /home/ben/Vs_Code_Projects/Projects/ALDE_Projekt/ALDE
  PYTHONPATH="$PWD" python3 -m pytest alde/test_webplayer_mcp*

- **403 Forbidden**: verify the server is running the current code, deploy from the repository root, and check the request `Origin` against the server allowlist/CORS response.
- **Scope or MCP fetch blocked by the host**: ensure the MCP server origin is listed in the resource's `_meta.ui.csp.connectDomains`; CORS allowlisting alone does not grant the MCP App host permission to make network requests.
- **UI missing but tool works**: inspect `tools/list`; missing `_meta.ui.resourceUri` means UI capability negotiation or server metadata is not active.
- **`resources/read` fails**: use the exact URI `ui://Mpc.App.ui`, include the MCP session/protocol headers, and ensure the host negotiated UI support.
- **`resource_not_found` after a UI URI update**: reconnect or refresh the MCP server in the host so it reloads `tools/list`; the server keeps the previous resource URI and scope HTTP path as compatibility aliases.
- **Extension build fails at `Buffer` or TypeScript**: run `npm install` in `deploy/vscode-extension`, then `npm run compile` before packaging.
- **Extension package warnings/errors**: ensure the manifest has repository metadata and a `LICENSE` file; package from `deploy/vscode-extension`.
- **Server process exits immediately**: check for an existing process on port 8766 and use the configured systemd service or another free port.

## Boundaries

- Never put TIDAL tokens or other secrets in the skill, MCP URL, manifest, or committed configuration.
- Keep the MCP App resource URI stable unless the server and every host integration are updated together.
- Do not claim that the UI rendered merely because `resources/read` returned HTML; verify the host's visible rendering or equivalent host event/log.
