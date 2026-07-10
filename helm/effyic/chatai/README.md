# ChatAI — 独立 Helm Chart

在 **已安装的 HiClaw / Effyic 核心** 之上，部署 Agno ChatAI Agent：

- MySQL `agno_agent` / `department` 表初始化
- `Worker` CR（由 hiclaw-controller 拉起 Pod）
- Higress Ingress 路由 + key-auth 鉴权
- API Token（Secret + Worker `AGNO_CONTROL_TOKEN`）

## 前置条件

1. 已安装 HiClaw 核心：

```bash
helm upgrade --install effyic ../ \
  --namespace default \
  --set credentials.llmApiKey="${HICLAW_LLM_API_KEY}" \
  --set gateway.publicURL="http://localhost:80"
```

2. Nacos 中已上传 AgentSpec（如 `medical-orchestrator`，标签 `stable`）
3. 宿主机 MySQL / PostgreSQL 可访问（minikube 场景见 `docs/deploy-minikube.md`）

## 安装

```bash
cd helm/effyic/chatai

# 方式 A：安装脚本
chmod +x install.sh
./install.sh

# 方式 B：helm 命令
GATEWAY_IP=$(docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}')

helm upgrade --install effyic-chatai . \
  --namespace default \
  --set hiclaw.releaseName=effyic \
  --set globalEnv.AGNO_DB_URL="postgresql+psycopg://root:postgresql@${GATEWAY_IP}:5432/vector_store" \
  --set dbInit.mysql.hostAliasIP="${GATEWAY_IP}"
```

## 配置说明

| values 路径 | 说明 |
|-------------|------|
| `hiclaw.releaseName` | 已安装的 HiClaw Helm release 名（默认 `effyic`） |
| `globalEnv` | Worker 共享环境变量 |
| `workers[]` | Worker 列表（name / package / env / expose） |
| `dbInit` | MySQL 表结构初始化 Job |
| `auth.token` | API Token；留空则自动生成 |
| `gateway` | Higress Ingress + key-auth |

## 卸载

```bash
helm uninstall effyic-chatai -n default
```

仅移除 ChatAI 相关资源，不影响 HiClaw 核心。
