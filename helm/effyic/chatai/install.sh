#!/usr/bin/env bash
# Install ChatAI on top of an existing HiClaw / Effyic release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}"

HICLAW_RELEASE="${HICLAW_RELEASE:-effyic}"
CHATAI_RELEASE="${CHATAI_RELEASE:-effyic-chatai}"
NAMESPACE="${NAMESPACE:-default}"
GATEWAY_IP="${GATEWAY_IP:-$(docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)}"
AGNO_DB_URL="${AGNO_DB_URL:-postgresql+psycopg://root:postgresql@${GATEWAY_IP}:5432/vector_store}"

if [[ -z "${GATEWAY_IP}" ]]; then
  echo "WARN: could not detect minikube gateway IP; set GATEWAY_IP manually" >&2
fi

helm upgrade --install "${CHATAI_RELEASE}" "${CHART_DIR}" \
  --namespace "${NAMESPACE}" \
  --set hiclaw.releaseName="${HICLAW_RELEASE}" \
  --set global.namespace="${NAMESPACE}" \
  --set globalEnv.AGNO_DB_URL="${AGNO_DB_URL}" \
  --set dbInit.mysql.hostAliasIP="${GATEWAY_IP}" \
  "$@"

echo ""
echo "Done. See: helm status ${CHATAI_RELEASE} -n ${NAMESPACE}"
