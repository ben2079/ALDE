#!/usr/bin/env bash
set -euo pipefail

# Deploy the ALDE project to a remote host and (re)start an MCP service via systemd.
#
# Required:
#   REMOTE_HOST
#
# Optional overrides:
#   REMOTE_USER (default: current user)
#   REMOTE_PROJECT_DIR (default: /home/$REMOTE_USER/Vs_Code_Projects/Projects/ALDE_Projekt)
#   PYTHON_BIN (default: /usr/bin/python3)
#   SERVICE_SCOPE (user|system, default: user)
#   SERVICE_NAME (default: webplayer_mcp.service)
#   MCP_MODULE (default: alde.webplayer_mcp_server)
#   MCP_TRANSPORT (default: http)
#   MCP_HOST (default: 0.0.0.0)
#   MCP_PORT (default: 8766)
#   MCP_EXTRA_ARGS (default: empty)
#   MCP_ENV_FILE (default: %h/.config/webplayer-mcp.env)
#   SSH_OPTS (default: empty)
#   SYNC_DELETE (1|0, default: 1)
#   HEALTHCHECK_ATTEMPTS (default: 30)
#   HEALTHCHECK_INTERVAL_S (default: 1)

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

REMOTE_HOST=${REMOTE_HOST:-}
REMOTE_USER=${REMOTE_USER:-${USER:-benjamin}}
REMOTE_PROJECT_DIR=${REMOTE_PROJECT_DIR:-/home/${REMOTE_USER}/Vs_Code_Projects/Projects/ALDE_Projekt}
PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}
SERVICE_SCOPE=${SERVICE_SCOPE:-user}
SERVICE_NAME=${SERVICE_NAME:-webplayer_mcp.service}
MCP_MODULE=${MCP_MODULE:-alde.webplayer_mcp_server}
MCP_TRANSPORT=${MCP_TRANSPORT:-http}
MCP_HOST=${MCP_HOST:-0.0.0.0}
MCP_PORT=${MCP_PORT:-8766}
MCP_EXTRA_ARGS=${MCP_EXTRA_ARGS:-}
MCP_ENV_FILE=${MCP_ENV_FILE:-%h/.config/webplayer-mcp.env}
SSH_OPTS=${SSH_OPTS:-}
SYNC_DELETE=${SYNC_DELETE:-1}
HEALTHCHECK_ATTEMPTS=${HEALTHCHECK_ATTEMPTS:-30}
HEALTHCHECK_INTERVAL_S=${HEALTHCHECK_INTERVAL_S:-1}

if [[ -z "${REMOTE_HOST}" ]]; then
  echo "ERROR: REMOTE_HOST is required."
  echo "Example: REMOTE_HOST=192.168.0.47 REMOTE_USER=benjamin ./scripts/deploy_mcp_remote.sh"
  exit 2
fi

if [[ "${SERVICE_SCOPE}" != "user" && "${SERVICE_SCOPE}" != "system" ]]; then
  echo "ERROR: SERVICE_SCOPE must be 'user' or 'system' (got: ${SERVICE_SCOPE})."
  exit 2
fi

if ! [[ "${HEALTHCHECK_ATTEMPTS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: HEALTHCHECK_ATTEMPTS must be a positive integer (got: ${HEALTHCHECK_ATTEMPTS})."
  exit 2
fi

if ! [[ "${HEALTHCHECK_INTERVAL_S}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: HEALTHCHECK_INTERVAL_S must be a non-negative integer (got: ${HEALTHCHECK_INTERVAL_S})."
  exit 2
fi

for cmd in ssh rsync; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: required command '${cmd}' not found."
    exit 3
  fi
done

echo "[1/4] Creating remote project directory ${REMOTE_PROJECT_DIR} on ${REMOTE_USER}@${REMOTE_HOST}"
ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p '${REMOTE_PROJECT_DIR}'"

RSYNC_ARGS=(-az --exclude .git/ --exclude .pytest_cache/ --exclude __pycache__/ --exclude .venv/ --exclude venv/ --exclude .env --exclude .aws/)
if [[ "${SYNC_DELETE}" == "1" ]]; then
  RSYNC_ARGS+=(--delete)
fi

echo "[2/4] Syncing repository to remote host"
rsync "${RSYNC_ARGS[@]}" "${PROJECT_ROOT}/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PROJECT_DIR}/"

echo "[3/4] Installing/updating service ${SERVICE_NAME} (${SERVICE_SCOPE})"
ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" \
  "REMOTE_PROJECT_DIR='${REMOTE_PROJECT_DIR}' PYTHON_BIN='${PYTHON_BIN}' SERVICE_SCOPE='${SERVICE_SCOPE}' SERVICE_NAME='${SERVICE_NAME}' MCP_MODULE='${MCP_MODULE}' MCP_TRANSPORT='${MCP_TRANSPORT}' MCP_HOST='${MCP_HOST}' MCP_PORT='${MCP_PORT}' MCP_EXTRA_ARGS='${MCP_EXTRA_ARGS}' MCP_ENV_FILE='${MCP_ENV_FILE}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

UNIT_BODY=$(cat <<UNIT
[Unit]
Description=ALDE MCP Service (${MCP_MODULE})
After=network.target

[Service]
Type=simple
WorkingDirectory=${REMOTE_PROJECT_DIR}
Environment=PYTHONPATH=${REMOTE_PROJECT_DIR}/ALDE
Environment=TIDAL_API_TOKEN_FILE=%h/.config/webplayer-mcp/tidal-token.json
Environment=WEBPLAYER_PLAYLIST_FILE=%h/.config/webplayer-mcp/playback-playlist.json
EnvironmentFile=-${MCP_ENV_FILE}
Environment=WEBPLAYER_PLAYBACK_BACKEND=browser
Environment=WEBPLAYER_BROWSER_BACKEND_ENABLED=1
ExecStart=${PYTHON_BIN} -m ${MCP_MODULE} --transport ${MCP_TRANSPORT} --host ${MCP_HOST} --port ${MCP_PORT} ${MCP_EXTRA_ARGS}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNIT
)

if [[ "${SERVICE_SCOPE}" == "user" ]]; then
  UNIT_DIR="${HOME}/.config/systemd/user"
  mkdir -p "${UNIT_DIR}"
  if [[ "${SERVICE_NAME}" != "webplayer_mcp_8766.service" ]]; then
    systemctl --user disable --now webplayer_mcp_8766.service 2>/dev/null || true
  fi
  printf '%s\n' "${UNIT_BODY}" > "${UNIT_DIR}/${SERVICE_NAME}"
  systemctl --user daemon-reload
  systemctl --user enable --now "${SERVICE_NAME}"
  systemctl --user restart "${SERVICE_NAME}"
  systemctl --user status "${SERVICE_NAME}" --no-pager -n 12 || true
else
  printf '%s\n' "${UNIT_BODY}" | sudo tee "/etc/systemd/system/${SERVICE_NAME}" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${SERVICE_NAME}"
  sudo systemctl restart "${SERVICE_NAME}"
  sudo systemctl status "${SERVICE_NAME}" --no-pager -n 12 || true
fi
REMOTE_SCRIPT

echo "[4/4] Running remote health check"
ssh ${SSH_OPTS} "${REMOTE_USER}@${REMOTE_HOST}" \
  "attempt=1; while [[ \"\$attempt\" -le ${HEALTHCHECK_ATTEMPTS} ]]; do if curl -fsS --max-time 6 'http://127.0.0.1:${MCP_PORT}/health'; then exit 0; fi; if [[ \"\$attempt\" -lt ${HEALTHCHECK_ATTEMPTS} ]]; then sleep ${HEALTHCHECK_INTERVAL_S}; fi; attempt=\$((attempt+1)); done; echo 'ERROR: remote health check did not become ready.' >&2; exit 1"

echo "Deployment successful: ${SERVICE_NAME} on ${REMOTE_USER}@${REMOTE_HOST}:${MCP_PORT}"
