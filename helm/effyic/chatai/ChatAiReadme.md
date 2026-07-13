# ChatAI — Helm Chart

独立子 Chart，在已安装的 HiClaw / Effyic 核心之上部署 Agno Worker、PostgreSQL schema、Higress 路由与 API Token。

## 前置条件

1. 已安装 HiClaw 核心（namespace `effyic`），参见 [README.md](../README.md)
2. 宿主机 PostgreSQL 可访问；默认由 `dbInit` Job 自动建库并导入 schema（`CHATAI_DB_INIT=false` 可跳过）
3. **仅当** `CHATAI_AGENTSPEC_ENABLED=true`（默认）时：Nacos 中已上传对应 AgentSpec

## 安装

```bash
helm upgrade --install effyic-chatai helm/effyic/chatai \
  --namespace effyic --create-namespace \
  --set hiclaw.releaseName="${HICLAW_RELEASE:-effyic}" \
  --set credentials.modelProvider="${HICLAW_MODEL_PROVIDER:-qwen}" \
  --set credentials.defaultModel="${HICLAW_DEFAULT_MODEL:-qwen3.6-plus}" \
  --set postgres.host="${CHATAI_DB_HOST:-host.minikube.internal}" \
  --set postgres.port="${CHATAI_DB_PORT:-5432}" \
  --set postgres.database="${CHATAI_DB_DATABASE:-aip_hub_test}" \
  --set postgres.username="${CHATAI_DB_USERNAME:-root}" \
  --set postgres.password="${CHATAI_DB_PASSWORD:-postgresql}" \
  --set dbInit.enabled="${CHATAI_DB_INIT:-false}" \
  --set agentspec.enabled="${CHATAI_AGENTSPEC_ENABLED:-false}" \
  --set agentspec.dataId="${CHATAI_AGENTSPEC_DATA_ID:-medical-orchestrator}" \
  --set agentspec.label="${CHATAI_AGENTSPEC_LABEL:-stable}" \
  --set gateway.publicURL="http://localhost:80" \
  --timeout 20m
```



## 验证

```bash
kubectl get worker.hiclaw.io effyic-chatai -n effyic
kubectl get ingress -l higress.io/resource-definer=higress -n effyic | grep chatai
kubectl get mcpbridge default -n effyic -o jsonpath='{.spec.registries[*].name}' ; echo
kubectl get wasmplugin -l higress.io/resource-definer=higress -n effyic | grep chatai

TOKEN=$(kubectl get secret effyic-chatai-chatai-auth -n effyic -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

curl -X POST http://localhost/effyic/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "tenant-id: default" \
  -H "user-id: default" \
  -H "session-id: 02ed7765-a1cc-4bcd-a0f9-307ba5bc6cda" \
  -H "role-code: default" \
  -H "x-debug-request: false" \
  -d '{"message":"你好"}'
```



### Session 接口（`/effyic/v1/sessions*`）

认证与 chat 相同：`Authorization: Bearer <TOKEN>`

**入库裁剪**：未传 `x-debug-request` 时默认精简写入（`AGNO_DEBUG_REQUEST_DEFAULT=false`）；传 `x-debug-request: true` 可完整入库。

| 方法       | 路径                                 | 说明                                                                                           |
| -------- | ---------------------------------- | -------------------------------------------------------------------------------------------- |
| `GET`    | `/effyic/v1/sessions`              | 分页列出会话；常用 query：`user_id`、`type`（agent）、`component_id`、`limit`、`page`、`sort_by`、`sort_order` |
| `GET`    | `/effyic/v1/sessions/{session_id}` | 获取会话详情，响应含 `chat_history`（推荐用于展示对话记录）                                                        |
|          |                                    |                                                                                              |
| `DELETE` | `/effyic/v1/sessions/{session_id}` | 删除单个会话                                                                                       |
| `DELETE` | `/effyic/v1/sessions`              | 批量删除会话                                                                                       |
|          |                                    |                                                                                              |


```bash
TOKEN=$(kubectl get secret effyic-chatai-chatai-auth -n effyic -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

# 1. 按 user_id 列出历史会话
curl "http://localhost/effyic/v1/sessions?user_id=default&limit=20&page=1&sort_order=desc" \
  -H "Authorization: Bearer $TOKEN"

# 2. 获取会话对话记录（chat_history）
curl "http://localhost/effyic/v1/sessions/<agno-session-id>" \
  -H "Authorization: Bearer $TOKEN"

# 3. 删除单个会话
curl -X DELETE "http://localhost/effyic/v1/sessions/<agno-session-id>" \
  -H "Authorization: Bearer $TOKEN"
```



## 卸载

```bash
helm uninstall effyic-chatai -n effyic
```

仅移除 ChatAI 相关资源，不影响 HiClaw 核心。