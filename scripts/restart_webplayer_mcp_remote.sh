#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/restart_webplayer_mcp_remote.sh
# Optional environment overrides: REMOTE_HOST, REMOTE_USER, SERVICE_NAME, SERVICE_SCOPE, SSH_OPTS

REMOTE_HOST=${REMOTE_HOST:-192.168.0.47}
REMOTE_USER=${REMOTE_USER:-benjamin}
SERVICE_NAME=${SERVICE_NAME:-webplayer_mcp.service}
SERVICE_SCOPE=${SERVICE_SCOPE:-user}
REMOTE_HEALTH_URL=${REMOTE_HEALTH_URL:-http://127.0.0.1:8766/health}
SSH_OPTS=${SSH_OPTS:-}

if [[ "${SERVICE_SCOPE}" == "user" ]]; then
  SYSTEMCTL_CMD="systemctl --user"
elif [[ "${SERVICE_SCOPE}" == "system" ]]; then
  SYSTEMCTL_CMD="sudo systemctl"
else
  echo "invalid SERVICE_SCOPE='${SERVICE_SCOPE}' (expected user|system)"
  exit 2
fi

echo "Restarting ${SERVICE_NAME} on ${REMOTE_USER}@${REMOTE_HOST}..."
ssh ${SSH_OPTS} ${REMOTE_USER}@${REMOTE_HOST} "if [[ '${SERVICE_NAME}' != 'webplayer_mcp_8766.service' && '${SERVICE_SCOPE}' == 'user' ]]; then systemctl --user disable --now webplayer_mcp_8766.service 2>/dev/null || true; fi; ${SYSTEMCTL_CMD} restart ${SERVICE_NAME} && sleep 1 && ${SYSTEMCTL_CMD} status ${SERVICE_NAME} --no-pager -n 8" || {
  echo "remote restart failed"
  exit 2
}

echo "Checking remote HTTP health endpoint: ${REMOTE_HEALTH_URL}"
ssh ${SSH_OPTS} ${REMOTE_USER}@${REMOTE_HOST} "curl -sS --max-time 5 '${REMOTE_HEALTH_URL}'" || {
  echo "remote health check failed"
  exit 3
}

echo "OK"
