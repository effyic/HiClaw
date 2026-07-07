# Effyic — 安装指南

## 前置条件


| 项          | 要求                                                                                                                                                                                                 |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 集群         | [minikube](https://minikube.sigs.k8s.io/) 已启动，`kubectl` 可用                                                                                                                                         |
| 工具         | Helm 3.14+                                                                                                                                                                                         |
| LLM        | 通义千问 API Key（安装时通过 `--set` 传入）                                                                                                                                                                     |
| MySQL      | 宿主机 MySQL 已建库 `nacos`；`values.yaml` 默认 `host.minikube.internal` + `hostAliasIP`（Docker 网关 IP，不随局域网 IP 变化）。网关 IP 查询：`docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}'` |
| 本地镜像       | 已构建并装入 minikube：`hiclaw/hiclaw-controller`、`hiclaw/agno-worker`、`hiclaw/hiclaw-manager`                                                                                                            |
| inotify 限制 | minikube 节点默认 `max_user_instances=128`，多 Pod 同节点时 Nacos 等 Java 服务易耗尽；安装前执行下方调优命令                                                                                                                   |


```bash
minikube ssh -- "echo -e 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/99-inotify.conf && sudo sysctl --system"
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

## 6. Agno-Worker（ChatAi）

Agno Worker 是独立对话运行时，通过 **Worker CR** 由 hiclaw-controller 部署，不依赖 Matrix / MinIO。医疗场景 AgentSpec 由 Controller 从 Nacos 拉取并写入 ConfigMap 挂载到 Pod。

### 6.1 PostgreSQL（会话持久化）

Agno Worker 需要 PostgreSQL 持久化对话，挂载外部数据源：

```yaml
# docker-compose 示例（network_mode: host）
# POSTGRES_DB=vector_store  POSTGRES_USER=root  POSTGRES_PASSWORD=postgresql  端口 5432
```

Worker CR 中通过 `spec.env.AGNO_DB_URL` 指向宿主机 PG。minikube Pod 访问宿主机需使用 **Docker 网关 IP**（与 Nacos MySQL 相同）：

```bash
# 查询网关 IP（通常为 192.168.49.1）
docker network inspect minikube --format '{{(index .IPAM.Config 0).Gateway}}'
```



### 6.2 注册 AgentSpec（二选一）

**方式 A — Nacos（推荐）**

在 Nacos 控制台（`kubectl port-forward svc/effyic-nacos 8848:8848`）上传 `openagno/examples/medical-orchestrator.agentspec.yaml` 为 AgentSpec `medical-orchestrator`，并发布 `stable` 标签。

**方式 B — file:// 本地验证**

将 AgentSpec 目录复制到 controller Pod，Worker CR 的 `package` 使用 `file://` URI：

```bash
CTRL_POD=$(kubectl get pod -l app.kubernetes.io/name=effyic-controller -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$CTRL_POD" -- mkdir -p /tmp/agentspec/effyic-chatai
kubectl cp openagno/examples/. "$CTRL_POD:/tmp/agentspec/effyic-chatai/"
# 部署时设置 PACKAGE_URI=file:///tmp/agentspec/effyic-chatai
```

