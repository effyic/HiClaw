# HiClaw Agno Worker

独立对话 Agent 运行时（**不受 Manager 管理**），无 Matrix / MinIO 依赖。会话持久化到 **PostgreSQL**。

## 架构要点

| 组件 | 职责 |
|------|------|
| **hiclaw-controller** | 通过 CR `spec.package=nacos://…` 从 Nacos 拉取 AgentSpec，写入 **ConfigMap** 并挂载到 Pod |
| **agno-worker** | 读取 `/etc/hiclaw/agentspec`，初始化 Agno Team/Agent，暴露 `/v1/chat` |
| **PostgreSQL** | Agno `PostgresDb` 持久化 `session_id` 对话历史 |

**不需要在 agno 镜像内集成 Nacos SDK** — Nacos 访问由 controller 侧的 Go 客户端完成（与 Hermes 不同，Hermes Worker 还需自管 Skill/MCP 与 A2A 注册）。

## Worker CR 示例

```yaml
apiVersion: hiclaw.io/v1beta1
kind: Worker
metadata:
  name: medical-chat
spec:
  runtime: agno
  model: gpt-4o
  package: nacos://nacos:8848/public/medical-orchestrator?label:stable
  env:
    AGNO_DB_URL: postgresql+psycopg://root:vector_store@postgres:5432/postgres
  expose:
    - port: 8090
      protocol: HTTP
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `AGNO_AGENTSPEC_DIR` | `/etc/hiclaw/agentspec` | Controller 挂载的 AgentSpec 目录 |
| `AGNO_DB_URL` | `postgresql+psycopg://root:vector_store@localhost:5432/postgres` | 会话库 |
| `AGNO_CONTROL_PORT` | `8090` | HTTP API 端口 |
| `AGNO_ENABLE_AGENTOS` | `false` | 启用 Agno AgentOS API（供 [os.agno.com](https://os.agno.com) 控制台连接） |
| `RUNTIME_ENV` | `prd` | 设为 `dev` 时本地开发免 JWT，便于连接控制台 |
| `AGNO_SPEC_WATCH_INTERVAL` | `30` | ConfigMap 热更新检测间隔（秒） |

## 动态配置

Controller 在 reconcile 时重新拉取 Nacos AgentSpec 并 **更新 ConfigMap**；agno-worker 通过文件指纹轮询检测变更后热重载运行时，**无需 Pod 内 Nacos 长连接**。

## 构建

```bash
docker build -f HiClaw/openagno/Dockerfile HiClaw/openagno
```

镜像环境变量 `HICLAW_AGNO_WORKER_IMAGE`（controller 侧）默认为 `hiclaw/agno-worker:latest`。

## Agent 控制台（AgentOS UI）

agno-worker 可选启用 **AgentOS** 运行时 API，通过 Agno 官方 Web 控制台 [os.agno.com](https://os.agno.com) 连接本地实例，进行对话测试、会话/Trace 查看与调试。

```bash
# docker-compose 或 .env 中启用
AGNO_ENABLE_AGENTOS=true
RUNTIME_ENV=dev          # 本地免 JWT
AGNO_CONTROL_PORT=8090
```

1. 启动 worker 后确认 `GET http://localhost:8090/agents` 返回 agent 列表
2. 打开 [os.agno.com](https://os.agno.com) → **Connect OS** → **Local**
3. 填入 `http://localhost:8090`（若 Docker 在远程机器，用宿主机 IP）
4. 连接成功后可在控制台 Chat / Sessions / Traces 中调试 agent

> HiClaw 的 `POST /v1/chat` 与 AgentOS API 并存；生产环境请设置 `RUNTIME_ENV=prd` 并在 os.agno.com 配置 JWT。
