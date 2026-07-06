#!/usr/bin/env bash
# apply-chatai.sh — 渲染并部署 Agno ChatAI Worker CR + 可选 Ingress
set -euo pipefail

WORKER_NAME="${1:-effyic-chatai}"
NAMESPACE="${2:-default}"
APPLY_INGRESS="${APPLY_INGRESS:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

render() {
  local src="$1"
  local pkg="${PACKAGE_URI:-nacos://effyic-nacos:8848/public/medical-orchestrator?label:stable}"
  sed \
    -e "s/__WORKER_NAME__/${WORKER_NAME}/g" \
    -e "s/__NAMESPACE__/${NAMESPACE}/g" \
    -e "s/__GATEWAY_KEY__/changeme-agno-chatai-key/g" \
    -e "s|nacos://effyic-nacos:8848/public/medical-orchestrator?label:stable|${pkg}|g" \
    "$src"
}

echo "==> Applying Agno ChatAI Worker: ${WORKER_NAME} (namespace=${NAMESPACE})"
render "${SCRIPT_DIR}/chatai.yaml" | kubectl apply -n "${NAMESPACE}" -f -

if [[ "${APPLY_INGRESS}" == "1" ]]; then
  echo "==> Applying temporary Ingress + key-auth (APPLY_INGRESS=1)"
  render "${SCRIPT_DIR}/chatai_ingress.yaml" | kubectl apply -n "${NAMESPACE}" -f -
fi

echo "==> Waiting for Worker CR..."
kubectl wait --for=condition=Ready "worker.hiclaw.io/${WORKER_NAME}" -n "${NAMESPACE}" --timeout=300s 2>/dev/null \
  || kubectl get "worker.hiclaw.io/${WORKER_NAME}" -n "${NAMESPACE}" -o wide

echo "Done. Chat API: POST /v1/chat  (port 8090)"
