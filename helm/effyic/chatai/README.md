# ChatAI —  Helm Chart

## 前置条件

1. 已安装 HiClaw 核心：

```bash
helm upgrade --install effyic ../ \
  --namespace default \
  --set credentials.llmApiKey="${HICLAW_LLM_API_KEY}" \
  --set gateway.publicURL="http://localhost:80"
```

1. Nacos 中已上传 AgentSpec（如 `medical-orchestrator`，标签 `stable`）
2. 宿主机 PostgreSQL 可访问（minikube 场景见 `docs/deploy-minikube.md`）



## 安装

```bash
cd helm/effyic/chatai

GATEWAY_IP=$(docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}')

helm upgrade --install effyic-chatai . \
  --namespace default \
  --set hiclaw.releaseName=effyic \
  --set gateway.publicURL="http://localhost:80" \
  --set globalEnv.AGNO_DB_URL="postgresql+psycopg://root:postgresql@${GATEWAY_IP}:5432/vector_store" \
  --set dbInit.mysql.hostAliasIP="${GATEWAY_IP}"
```



## 验证

```bash
TOKEN=$(kubectl get secret effyic-chatai-chatai-auth -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

curl -X POST http://localhost/effiyc/v1/chat \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "tenant-id: tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}'
```



## 配置说明


| values 路径            | 说明                                              |
| -------------------- | ----------------------------------------------- |
| `hiclaw.releaseName` | 已安装的 HiClaw Helm release 名（默认 `effyic`）         |
| `globalEnv`          | Worker 共享环境变量                                   |
| `workers[]`          | Worker 列表（name / package / env）                 |
| `dbInit`             | MySQL 表结构初始化 Job                                |
| `auth.token`         | API Token；留空则自动生成                               |
| `gateway.publicURL`  | 平台公共 URL（与核心 `gateway.publicURL` 对齐，用于文档/NOTES） |
| `gateway.host`       | Ingress Host；留空则匹配网关所有入站 Host                   |
| `gateway.path`       | 路径前缀，默认 `/effiyc`                               |
| `workers[].expose`   | 可选；启用后为 Worker 创建子域名路由                          |




## 卸载

```bash
helm uninstall effyic-chatai -n default
```

仅移除 ChatAI 相关资源，不影响 HiClaw 核心。