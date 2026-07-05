# Effyic — 安装指南

## 前置条件


| 项           | 要求                                                                                        |
| ----------- | ----------------------------------------------------------------------------------------- |
| 集群          | [minikube](https://minikube.sigs.k8s.io/) 已启动，`kubectl` 可用                                |
| 工具          | Helm 3.14+                                                                                |
| LLM         | 通义千问 API Key（安装时通过 `--set` 传入）                                                            |
| Nacos MySQL | 外部 MySQL 已建库 `nacos`，且 minikube 节点能访问 `nacos.mysql.host`                                  |
| 本地镜像        | 已构建并装入 minikube：`hiclaw/hiclaw-controller`、`hiclaw/hermes-worker`、`hiclaw/hiclaw-manager` |
| inotify 限制  | minikube 节点默认 `max_user_instances=128`，多 Pod 同节点时 Nacos 等 Java 服务易耗尽；安装前执行下方调优命令          |


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

# 本地构建
make build-hiclaw-controller DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"
make build-hermes-worker DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"
make build-manager \
  OPENCLAW_BASE_IMAGE=hiclaw/openclaw-base \
  OPENCLAW_BASE_VERSION=latest \
  DOCKER_BUILD_ARGS="${DOCKER_BUILD_ARGS}"

# 装入 minikube
for img in hiclaw/hiclaw-controller:latest hiclaw/hermes-worker:latest hiclaw/hiclaw-manager:latest; do
  load_image "$img"
done
for img in \
  "$REG/tuwunel:20260216" \
  "$REG/minio:20260216" \
  "$REG/element-web:20260216" \
  "$REG/higress:2.2.1" \
  "$REG/pilot:2.2.1" \
  "$REG/gateway:2.2.1" \
  "$REG/console:2.2.1"; do
  docker pull "$img"
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

## 6. ChatAI Worker（可选）

Hermes Chat API

```bash
修改 chatai.yaml 模板
kubectl apply chatai.yaml

curl -sS http://127.0.0.1:18080/v1/chat/completions \
  -H 'Host: worker-effyic-chatai-8642-local.hiclaw.io' \
  -H "Authorization: Bearer ${KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"effyic-chatai","messages":[{"role":"user","content":"hello"}]}'
```



## 7. 卸载

```bash
kubectl delete -f helm/effyic/chatai/chatai.yaml --ignore-not-found

# 卸载保留 PVC：
helm uninstall effyic -n default --wait --timeout 15m

# 卸载删 PVC
helm uninstall effyic -n default --no-hooks; kubectl delete pvc data-effyic-tuwunel-0 data-effyic-minio-0 -n default --ignore-not-found
```

