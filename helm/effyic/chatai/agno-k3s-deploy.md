# K3s 重新部署 sensitive-content 与 agno-worker（latest）

本文用于从 ARM64 Mac 构建 `linux/amd64` 镜像，并将
`hiclaw/sensitive-content:latest` 与 `hiclaw/agno-worker:latest` 离线导入 K3s。

> `latest` 不会体现镜像版本变化，而且两个工作负载均使用
> `imagePullPolicy: IfNotPresent`。导入同名新镜像后，必须显式重建 Pod；仅执行
> `helm upgrade` 或刷新前端不能保证使用新镜像。

当前 Agent 绑定语义为 **敏感词类型**（`/agents/{role_code}/sensitive-types`），
策略快照字段为 `binding_type_ids`。Helm upgrade 会触发 migrate Job（含 `0005`）。

---

## 1. 前置检查

代码分支：

```bash
cd /Users/depei/Developer/HiClaw
git switch feat/agent-type-binding
git branch --show-current
```

预期输出：

```text
feat/agent-type-binding
```

确认 K3s 节点架构：

```bash
kubectl get nodes \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.nodeInfo.architecture}{"\n"}{end}'
```

下文假定节点为 `amd64`。如果节点实际为 `arm64`，将所有
`linux/amd64` 改为 `linux/arm64`。

## 2. 统一环境变量

在 Mac 和 K3s 机器上均使用：

```bash
FIX_TAG=latest
```

## 3. 在 Mac 构建 AMD64 镜像

```bash
cd /Users/depei/Developer/HiClaw

FIX_TAG=latest

make build-sensitive-content build-agno-worker \
  VERSION="$FIX_TAG" \
  DOCKER_PLATFORM=linux/amd64 \
  DOCKER_BUILD_ARGS="--no-cache"
```

确认两个镜像的平台：

```bash
docker image inspect \
  --format '{{.RepoTags}}  {{.Os}}/{{.Architecture}}' \
  hiclaw/sensitive-content:latest \
  hiclaw/agno-worker:latest
```

两个镜像都应显示 `linux/amd64`。

## 4. 构建后本地验证

验证 agno-worker 包含敏感内容检测模块：

```bash
FIX_TAG=latest

docker run --rm \
  --platform linux/amd64 \
  --entrypoint /opt/venv/agno/bin/python \
  "hiclaw/agno-worker:$FIX_TAG" \
  -c 'import agno_worker.moderation; print("moderation package OK")'
```

预期输出：

```text
moderation package OK
```

验证 sensitive-content 包含以 `role_code` 为标识的 **类型绑定**接口：

```bash
FIX_TAG=latest

docker run --rm \
  --platform linux/amd64 \
  --entrypoint /opt/venv/sensitive-content/bin/python \
  "hiclaw/sensitive-content:$FIX_TAG" \
  -c 'from sensitive_content.api import create_app; p="/api/v1/tenants/{tenant_id}/agents/{role_code}/sensitive-types"; assert p in create_app(enable_cleanup=False).openapi()["paths"]; print("type-binding API OK")'
```

预期输出：

```text
type-binding API OK
```

任意一个验证失败，都不要继续部署。

## 5. 导出镜像并传到 K3s 机器

将两个镜像打入同一个离线包：

```bash
FIX_TAG=latest
IMAGE_ARCHIVE="/tmp/effyic-sensitive-images-$FIX_TAG.tar"

docker save \
  "hiclaw/sensitive-content:$FIX_TAG" \
  "hiclaw/agno-worker:$FIX_TAG" \
  -o "$IMAGE_ARCHIVE"

scp "$IMAGE_ARCHIVE" root@172.16.1.23:/tmp/
```

## 6. 在 K3s 机器上替换缓存镜像

登录 K3s 机器：

```bash
ssh root@172.16.1.23
```

确认离线包存在：

```bash
FIX_TAG=latest
IMAGE_ARCHIVE="/tmp/effyic-sensitive-images-$FIX_TAG.tar"

ls -lh "$IMAGE_ARCHIVE"
```

删除两个精确的旧镜像引用。已经运行的容器不会因此立即停止：

```bash
k3s ctr images remove docker.io/hiclaw/sensitive-content:latest || true
k3s ctr images remove docker.io/hiclaw/agno-worker:latest || true
```

导入新镜像：

```bash
k3s ctr images import "$IMAGE_ARCHIVE"
```

确认两个 `latest` 引用均已存在：

```bash
k3s ctr images list | grep -E 'hiclaw/(sensitive-content|agno-worker):latest'
```

## 7. 回到 Mac 更新 Helm

```bash
cd /Users/depei/Developer/HiClaw

FIX_TAG=latest

helm upgrade effyic-chatai helm/effyic/chatai \
  -n effyic \
  --reuse-values \
  --set sensitiveContent.enabled=true \
  --set-string sensitiveContent.image.tag="$FIX_TAG" \
  --set-string worker.image.tag="$FIX_TAG" \
  --timeout 10m
```

`sensitiveContent.adminToken` 不再需要设置。管理 API 直接使用
`effyic-chatai-chatai-auth.CHATAI_API_TOKEN`。

migrate Job 会应用至 `0005`（Agent 类型绑定）。若库中存在历史
**跨类型 override**（`override.type_id != global_rule.type_id`），`0005` 会
`RAISE EXCEPTION` 失败。此时先人工清理脏数据，再重跑迁移：

```bash
kubectl logs -n effyic \
  -l app.kubernetes.io/component=sensitive-content-migrate \
  --tail=100
```

## 8. 确认工作负载镜像配置

检查 sensitive-content Deployment：

```bash
kubectl get deployment effyic-chatai-sensitive-content -n effyic \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

预期输出：

```text
hiclaw/sensitive-content:latest
```

检查 Worker CR：

```bash
kubectl get worker.hiclaw.io effyic-chatai -n effyic \
  -o jsonpath='{.spec.image}{"\n"}'
```

预期输出：

```text
hiclaw/agno-worker:latest
```

如果 Worker CR 不是预期镜像，说明 `workers[0].image` 有独立覆盖：

```bash
FIX_TAG=latest

helm upgrade effyic-chatai helm/effyic/chatai \
  -n effyic \
  --reuse-values \
  --set sensitiveContent.enabled=true \
  --set-string sensitiveContent.image.tag="$FIX_TAG" \
  --set-string "workers[0].image=hiclaw/agno-worker:$FIX_TAG" \
  --timeout 10m
```

## 9. 强制重建 Pod

由于镜像标签仍然是 `latest`，必须显式重建两个 Pod。

重建 sensitive-content：

```bash
kubectl rollout restart deployment/effyic-chatai-sensitive-content -n effyic
kubectl rollout status deployment/effyic-chatai-sensitive-content -n effyic \
  --timeout=5m
```

重建 Worker Pod，Worker Controller 会自动创建新 Pod：

```bash
kubectl delete pod hiclaw-worker-effyic-chatai -n effyic

kubectl wait -n effyic \
  --for=create pod/hiclaw-worker-effyic-chatai \
  --timeout=2m

kubectl wait -n effyic \
  --for=condition=Ready pod/hiclaw-worker-effyic-chatai \
  --timeout=5m
```

确认两个 Pod 的镜像 ID 已更新：

```bash
kubectl get pod -n effyic \
  hiclaw-worker-effyic-chatai \
  -o jsonpath='{.spec.containers[0].image}{"  "}{.status.containerStatuses[0].imageID}{"\n"}'

kubectl get pod -n effyic \
  -l app.kubernetes.io/instance=effyic-chatai,app.kubernetes.io/component=sensitive-content \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.spec.containers[0].image}{"  "}{.status.containerStatuses[0].imageID}{"\n"}{end}'
```

## 10. 验证 Pod 内代码

验证 Worker：

```bash
kubectl exec -n effyic hiclaw-worker-effyic-chatai -- \
  /opt/venv/agno/bin/python -c \
  'from agno_worker.moderation.models import PolicySnapshot; assert "binding_type_ids" in PolicySnapshot.__dataclass_fields__; print("binding_type_ids OK")'
```

验证 sensitive-content：

```bash
SENSITIVE_POD=$(kubectl get pod -n effyic \
  -l app.kubernetes.io/instance=effyic-chatai,app.kubernetes.io/component=sensitive-content \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n effyic "$SENSITIVE_POD" -- \
  /opt/venv/sensitive-content/bin/python -c \
  'from sensitive_content.api import create_app; p="/api/v1/tenants/{tenant_id}/agents/{role_code}/sensitive-types"; assert p in create_app(enable_cleanup=False).openapi()["paths"]; print("type-binding API OK")'
```

预期分别输出：

```text
binding_type_ids OK
type-binding API OK
```

可选：确认迁移版本包含 `5`（在 Postgres Pod 内执行，库名与 Chart 一致）：

```bash
kubectl exec -n effyic -it deploy/effyic-chatai-postgres -- \
  psql -U postgres -d chatai -c \
  "SELECT version FROM sensitive_content.schema_migration ORDER BY version;"
```

（Deployment / 库名若与上不同，按实际资源名替换。）

## 11. 验证管理 Token

sensitive-content 管理 Token 应与 ChatAI API Token 完全相同：

```bash
SENSITIVE_POD=$(kubectl get pod -n effyic \
  -l app.kubernetes.io/instance=effyic-chatai,app.kubernetes.io/component=sensitive-content \
  -o jsonpath='{.items[0].metadata.name}')

CHAT=$(kubectl get secret effyic-chatai-chatai-auth -n effyic \
  -o jsonpath='{.data.CHATAI_API_TOKEN}' | base64 -d)

ADMIN=$(kubectl exec -n effyic "$SENSITIVE_POD" -- \
  printenv SENSITIVE_CONTENT_ADMIN_TOKEN)

test "$CHAT" = "$ADMIN" && echo "admin token OK"
```

预期输出：

```text
admin token OK
```

## 12. 与旧版文档的差异

| 项 | 旧（规则绑定） | 当前（类型绑定） |
|----|----------------|------------------|
| 分支 | `codex/effiyc-sensitive-word-service` 等 | `feat/agent-type-binding` |
| 管理 API | `.../agents/{role_code}/sensitive-rules` | `.../agents/{role_code}/sensitive-types` |
| 快照字段 | `binding_rule_ids` | `binding_type_ids` |
| 迁移 | 至 `0004` | 至 **`0005`**（跨类型 override 会 fail-fast） |
