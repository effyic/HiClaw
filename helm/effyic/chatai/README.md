# ChatAI —  Helm Chart

## 前置条件

1. 已安装 HiClaw 核心（namespace `effiyc`）：

```bash
helm upgrade --install effyic ../ \
  --namespace effiyc --create-namespace \
  --set credentials.llmApiKey="${HICLAW_LLM_API_KEY}" \
  --set gateway.publicURL="http://localhost:80"
```

2. Nacos 中已上传 AgentSpec（如 `medical-orchestrator`，标签 `stable`）
3. 外部 PostgreSQL 库 `aip_hub_test` 可访问（Agno 会话与租户配置共用同一库）



## 安装

```bash
cd helm/effyic/chatai
chmod +x install.sh
./install.sh
```

或手动：

```bash
GATEWAY_IP=$(docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}')

helm upgrade --install effyic-chatai . \
  --namespace effiyc --create-namespace \
  --set hiclaw.releaseName=effyic \
  --set gateway.publicURL="http://localhost:80" \
  --set postgres.hostAliasIP="${GATEWAY_IP}"
```



## 验证

```bash
TOKEN=$(kubectl get secret effyic-chatai-chatai-auth -n effiyc -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

curl -X POST http://localhost/effiyc/v1/chat \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "tenant-id: tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}'
```



## 配置说明


| values 路径            | 说明                                              |
| -------------------- | ----------------------------------------------- |
| `global.namespace`   | 目标 namespace（默认 `effiyc`）                      |
| `hiclaw.releaseName` | 已安装的 HiClaw Helm release 名（默认 `effyic`）         |
| `postgres.*`         | 外部 PostgreSQL（会话 + 租户共用，`AGNO_DB_URL` / `AGNO_AGENT_DB_URL` 自动注入） |
| `globalEnv`          | Worker 共享环境变量（可选覆盖 `AGNO_DB_URL`）                                   |
| `workers[]`          | Worker 列表（name / package / env）                 |
| `dbInit`             | PostgreSQL 表结构初始化 Job                              |
| `auth.token`         | API Token；留空则自动生成                               |
| `gateway.publicURL`  | 平台公共 URL（与核心 `gateway.publicURL` 对齐，用于文档/NOTES） |
| `gateway.host`       | Ingress Host；留空则匹配网关所有入站 Host                   |
| `gateway.path`       | 路径前缀，默认 `/effiyc`                               |
| `workers[].expose`   | 可选；启用后为 Worker 创建子域名路由                          |




## 卸载

```bash
helm uninstall effyic-chatai -n effiyc
```

仅移除 ChatAI 相关资源，不影响 HiClaw 核心。
