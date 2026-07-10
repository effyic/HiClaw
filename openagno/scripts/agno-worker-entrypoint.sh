#!/bin/bash
# agno-worker-entrypoint.sh — standalone Agno conversational worker
set -e

WORKER_NAME="${HICLAW_WORKER_NAME:?HICLAW_WORKER_NAME is required}"

log() {
    echo "[hiclaw-agno-worker $(date '+%Y-%m-%d %H:%M:%S')] $1"
}

if [ -n "${TZ}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
fi

: "${AGNO_AGENTSPEC_DIR:=/etc/hiclaw/agentspec}"
: "${AGNO_HOOKS_DIR:=/etc/hiclaw/hooks}"
: "${AGNO_DB_URL:=postgresql+psycopg://root:vector_store@localhost:5432/postgres}"
: "${AGNO_AGENT_DB_URL:=}"
: "${AGNO_CONTROL_PORT:=8090}"

export AGNO_AGENTSPEC_DIR AGNO_HOOKS_DIR AGNO_DB_URL AGNO_AGENT_DB_URL AGNO_CONTROL_PORT

log "Starting agno-worker: ${WORKER_NAME}"
log "  AgentSpec dir: ${AGNO_AGENTSPEC_DIR}"
log "  Hooks dir: ${AGNO_HOOKS_DIR} (optional extension hooks)"
log "  Agent DB: ${AGNO_AGENT_DB_URL:-<from AGNO_DB_URL if MySQL>}"
log "  API port: ${AGNO_CONTROL_PORT}"

exec /opt/venv/agno/bin/agno-worker --name "${WORKER_NAME}" --api-port "${AGNO_CONTROL_PORT}"
