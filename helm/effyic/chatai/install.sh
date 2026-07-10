#!/usr/bin/env bash
# install.sh — deploy ChatAI (Agno Worker) on top of an existing Effyic / HiClaw release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}"

NAMESPACE="${NAMESPACE:-effiyc}"
RELEASE_NAME="${RELEASE_NAME:-effyic-chatai}"
HICLAW_RELEASE="${HICLAW_RELEASE:-effyic}"
GATEWAY_IP="${GATEWAY_IP:-$(docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)}"

PG_HOST="${PG_HOST:-host.minikube.internal}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-root}"
PG_PASSWORD="${PG_PASSWORD:-postgresql}"
PG_DATABASE="${PG_DATABASE:-aip_hub_test}"

helm upgrade --install "${RELEASE_NAME}" "${CHART_DIR}" \
  --namespace "${NAMESPACE}" --create-namespace \
  --set hiclaw.releaseName="${HICLAW_RELEASE}" \
  --set gateway.publicURL="${GATEWAY_PUBLIC_URL:-http://localhost:80}" \
  --set postgres.host="${PG_HOST}" \
  --set postgres.hostAliasIP="${GATEWAY_IP}" \
  --set postgres.port="${PG_PORT}" \
  --set postgres.database="${PG_DATABASE}" \
  --set postgres.username="${PG_USER}" \
  --set postgres.password="${PG_PASSWORD}" \
  --timeout 15m

echo "ChatAI installed in namespace ${NAMESPACE} (release ${RELEASE_NAME})."
echo "PostgreSQL: ${PG_DATABASE} @ ${PG_HOST}:${PG_PORT} (session + tenant)"
