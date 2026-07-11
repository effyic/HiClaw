# ChatAI — Helm Chart

独立子 Chart，在已安装的 HiClaw / Effyic 核心之上部署 Agno Worker、PostgreSQL schema、Higress 路由与 API Token。

## 前置条件

1. 已安装 HiClaw 核心（namespace `effiyc`），参见 [README.md](../README.md)
2. 宿主机 PostgreSQL 可访问；默认由 `dbInit` Job 自动建库并导入 schema（`CHATAI_DB_INIT=false` 可跳过）
3. **仅当** `CHATAI_AGENTSPEC_ENABLED=true`（默认）时：Nacos 中已上传对应 AgentSpec

## 安装

```bash

helm upgrade --install effyic-chatai helm/effyic/chatai \
  --namespace effiyc --create-namespace \
  --set hiclaw.releaseName="${HICLAW_RELEASE:-effyic}" \
  --set credentials.defaultModel="${HICLAW_DEFAULT_MODEL:-qwen3.6-plus}" \
  --set postgres.host="${CHATAI_DB_HOST:-host.minikube.internal}" \
  --set postgres.port="${CHATAI_DB_PORT:-5432}" \
  --set postgres.database="${CHATAI_DB_DATABASE:-aip_hub_test}" \
  --set postgres.username="${CHATAI_DB_USERNAME:-root}" \
  --set postgres.password="${CHATAI_DB_PASSWORD:-postgresql}" \
  --set dbInit.enabled="${CHATAI_DB_INIT:-true}" \
  --set agentspec.enabled="${CHATAI_AGENTSPEC_ENABLED:-false}" \
  --set agentspec.dataId="${CHATAI_AGENTSPEC_DATA_ID:-medical-orchestrator}" \
  --set agentspec.label="${CHATAI_AGENTSPEC_LABEL:-stable}" \
  --set gateway.publicURL="http://localhost:80" \
  --timeout 10m
```



## 验证

```bash
kubectl get worker.hiclaw.io effyic-chatai -n effiyc
kubectl get ingress,wasmplugin -l app.kubernetes.io/component=chatai -n effiyc

TOKEN=$(kubectl get secret effyic-chatai-chatai-auth -n effiyc -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

curl -X POST http://localhost/effiyc/v1/chat \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "tenant-id: tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}'
```



## 卸载

```bash
helm uninstall effyic-chatai -n effiyc
```

仅移除 ChatAI 相关资源，不影响 HiClaw 核心。