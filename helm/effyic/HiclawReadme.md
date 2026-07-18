# Effyic — 安装指南

## 前置条件


| 项          | 要求                                                                                      |
| ---------- | --------------------------------------------------------------------------------------- |
| 集群         | [minikube](https://minikube.sigs.k8s.io/) 已启动，`kubectl` 可用                              |
| 工具         | Helm 3.14+                                                                              |
| LLM        | 通义千问 API Key（安装时通过 `--set` 传入）                                                          |
| PostgreSQL | 宿主机已运行 PostgreSQL；默认由 `nacos.dbInit` Job 自动建库并导入 schema（`NACOS_DB_INIT=false` 可跳过）      |
| 本地镜像       | 已构建并装入 minikube：`hiclaw/hiclaw-controller`、`hiclaw/agno-worker`、`hiclaw/hiclaw-manager` |
| inotify 限制 | minikube 节点默认 `max_user_instances=128`，多 Pod 同节点时 Nacos 等 Java 服务易耗尽；安装前执行下方调优命令        |


```bash
minikube ssh -- "echo -e 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/99-inotify.conf && sudo sysctl --system"
```

SQL 脚本位于 `files/nacos-pg-schema.sql`。若库表已提前准备好，安装时设 `NACOS_DB_INIT=false` 跳过建库与导表（`nacos.dbInit.enabled=false`）。

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
# 构建敏感词后端（可选；不含 web 前端）
make build-sensitive-content DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"
# 构建manager, 自主协调团队时才会用到
make build-manager \
  OPENCLAW_BASE_IMAGE=hiclaw/openclaw-base \
  OPENCLAW_BASE_VERSION=latest \
  DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"

# 装入 minikube
for img in hiclaw/hiclaw-controller:latest hiclaw/agno-worker:latest hiclaw/hermes-worker:latest hiclaw/hiclaw-manager:latest hiclaw/sensitive-content:latest; do
  load_image "$img"
done
```



## 2. 拉取 Helm 依赖

```bash
helm dependency build helm/effyic/
```



## 3. 安装

默认命名空间为 `effyic`（见 `values.yaml` 中 `global.namespace`）。

```bash
helm upgrade --install effyic helm/effyic \
  --namespace effyic --create-namespace \
  --set credentials.llmApiKey="${HICLAW_LLM_API_KEY}" \
  --set credentials.llmProvider="${HICLAW_LLM_PROVIDER:-qwen}" \
  --set credentials.defaultModel="${HICLAW_DEFAULT_MODEL:-qwen3.6-plus}" \
  --set credentials.llmBaseUrl="${HICLAW_LLM_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}" \
  --set nacos.database.host="${NACOS_DB_HOST:-host.minikube.internal}" \
  --set nacos.database.port="${NACOS_DB_PORT:-5432}" \
  --set nacos.database.database="${NACOS_DB_DATABASE:-nacos}" \
  --set nacos.database.username="${NACOS_DB_USERNAME:-root}" \
  --set nacos.database.password="${NACOS_DB_PASSWORD:-postgresql}" \
  --set nacos.dbInit.enabled="${NACOS_DB_INIT:-true}" \
  --set gateway.publicURL="http://localhost:80" \
  --timeout 20m  
```



## 4. 验证

```bash
kubectl get pods -n effyic
helm status effyic -n effyic
kubectl get manager.hiclaw.io -n effyic
```

预期 Running 组件：controller、nacos、tuwunel、minio、element-web、higress-gateway / higress-controller / higress-console、Manager Pod（`hiclaw-manager`）。

## 5. 访问

**Element Web（IM）**

浏览器打开 [http://localhost](http://localhost) ，用户名 `admin`，密码见 `values.yaml` 中 `credentials.adminPassword`（默认 `admin`）。

## 6. ChatAI（独立 Chart，可选安装）

ChatAI **不在** `helm/effyic` 根 Chart 中安装，而是进入子目录 `chatai/` 单独部署（依赖已运行的 HiClaw 核心），详见 [chatai/README.md](../chatai/README.md)。

### 7. 卸载

```bash
# 卸载保留 PVC：
helm uninstall effyic -n effyic --wait --timeout 15m

# 卸载删 PVC
helm uninstall effyic -n effyic --no-hooks; kubectl delete pvc data-effyic-tuwunel-0 data-effyic-minio-0 -n effyic --ignore-not-found
```

