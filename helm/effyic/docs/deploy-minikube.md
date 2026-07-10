# Effyic — 安装指南

## 前置条件


| 项          | 要求                                                                                                                                                                                                 |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 集群         | [minikube](https://minikube.sigs.k8s.io/) 已启动，`kubectl` 可用                                                                                                                                         |
| 工具         | Helm 3.14+                                                                                                                                                                                         |
| LLM        | 通义千问 API Key（安装时通过 `--set` 传入）                                                                                                                                                                     |
| PostgreSQL | 宿主机 PG 已建库 `nacos` 并初始化 schema（见下方命令）；`values.yaml` 默认 `host.minikube.internal` + `hostAliasIP`（Docker 网关 IP）。网关 IP：`docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}'` |
| 本地镜像       | 已构建并装入 minikube：`hiclaw/hiclaw-controller`、`hiclaw/agno-worker`、`hiclaw/hiclaw-manager`                                                                                                            |
| inotify 限制 | minikube 节点默认 `max_user_instances=128`，多 Pod 同节点时 Nacos 等 Java 服务易耗尽；安装前执行下方调优命令                                                                                                                   |


```bash
minikube ssh -- "echo -e 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/99-inotify.conf && sudo sysctl --system"
```

Nacos 使用 PostgreSQL 时需先建库并导入官方 schema（Nacos 不会自动建表）：

```bash
psql "postgresql://root:postgresql@127.0.0.1:5432/postgres" -c "CREATE DATABASE nacos"
docker run --rm --entrypoint sh nacos-registry.cn-hangzhou.cr.aliyuncs.com/nacos/nacos-server:v3.2.2 \
  -c "unzip -p /home/nacos/plugins/nacos-datasource-plugin-postgresql-3.2.2.jar META-INF/pg-schema.sql" \
  | psql "postgresql://root:postgresql@127.0.0.1:5432/nacos"
```



## 1. 准备镜像

```bash
cd /path/to/HiClaw

REG=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress
load_image() { minikube image load "$1"; }

export DOCKER_BUILDKIT=1
export DOCKER_BUILD_ARGS="\
  --build-arg APT_MIRROR=mirrors.aliyun.com \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com/"

# openclaw-base（build-manager 依赖）
docker pull "$REG/openclaw-base:20260423-8359cbc"
docker tag "$REG/openclaw-base:20260423-8359cbc" hiclaw/openclaw-base:latest

# 构建 hiclaw-controller
make build-hiclaw-controller DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"
# 构建ChatAgent
make build-agno-worker DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"
# 构建个人助手Agent
make build-hermes-worker DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"
# 构建manager, 自主协调团队时才会用到
make build-manager \
  OPENCLAW_BASE_IMAGE=hiclaw/openclaw-base \
  OPENCLAW_BASE_VERSION=latest \
  DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"

# 装入 minikube
for img in hiclaw/hiclaw-controller:latest hiclaw/agno-worker:latest hiclaw/hermes-worker:latest hiclaw/hiclaw-manager:latest; do
  load_image "$img"
done
```



## 2. 拉取 Helm 依赖

```bash
helm dependency build helm/effyic/
```



## 3. 安装

```bash
export HICLAW_LLM_API_KEY="sk-your-qwen-api-key"

helm upgrade --install effyic helm/effyic \
  --namespace default --create-namespace \
  --set credentials.llmApiKey="${HICLAW_LLM_API_KEY}" \
  --set gateway.publicURL="http://localhost:80" \
  --timeout 20m
```



## 4. 验证

```bash
kubectl get pods -n default
helm status effyic -n default
kubectl get manager.hiclaw.io -n default
```

预期 Running 组件：controller、nacos、tuwunel、minio、element-web、higress-gateway / higress-controller / higress-console、Manager Pod（`hiclaw-manager`）。

## 5. 访问

**Element Web（IM）**

浏览器打开 [http://localhost](http://localhost) ，用户名 `admin`，密码见 `values.yaml` 中 `credentials.adminPassword`（默认 `admin`）。

## 6. ChatAI（独立 Chart，可选）

ChatAI **不在** `helm/effyic` 根 Chart 中安装，而是进入子目录 `chatai/` 单独部署（依赖已运行的 HiClaw 核心）。

### 6.1 前置

- §3 核心安装已完成
- Nacos 已上传 AgentSpec `medical-orchestrator`（标签 `stable`）
- 宿主机 PostgreSQL（会话库）与 MySQL（`agno_worker` 库）可用

### 6.2 安装 ChatAI

```bash
cd helm/effyic/chatai
chmod +x install.sh

GATEWAY_IP=$(docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}')
export AGNO_DB_URL="postgresql+psycopg://root:postgresql@${GATEWAY_IP}:5432/vector_store"

./install.sh
```

或手动：

```bash
helm upgrade --install effyic-chatai helm/effyic/chatai \
  --namespace default \
  --set hiclaw.releaseName=effyic \
  --set globalEnv.AGNO_DB_URL="${AGNO_DB_URL}" \
  --set dbInit.mysql.hostAliasIP="${GATEWAY_IP}"
```

### 6.3 验证

```bash
kubectl get worker.hiclaw.io effyic-chatai
kubectl get ingress,wasmplugin -l app.kubernetes.io/component=chatai

TOKEN=$(kubectl get secret effyic-chatai-chatai-auth -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

# 平台公共域名 + /effiyc 路径（无需 Host 头）
curl -X POST http://localhost/effiyc/v1/chat \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "tenant-id: tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好"}'
```

详见 [chatai/README.md](../chatai/README.md)。

