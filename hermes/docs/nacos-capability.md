# Hermes Worker 超级智能体能力设计

本文档定义 Hermes Worker 的四项扩展能力，供 `hermes_worker` 开发与评审使用。

| # | 能力 | 一句话 |
|---|------|--------|
| 1 | **Nacos 注册与长连接** | 启动后在 Nacos 注册 Agent 身份，后台维持心跳/租约，平台可发现在线 Worker |
| 2 | **Skill / MCP 自管理与持久化** | 从 Nacos 发现、安装、卸载能力，写入本地与 MinIO，重启不丢，形成可独立演进的「超级智能体」 |
| 3 | **控制端口** | 暴露 HTTP 控制面，接收平台/编排器指令，**不依赖 Matrix 消息** |
| 4 | **能力热更新** | 监听 Nacos 与本地 workspace 中 skill、prompt、MCP 的变更，动态刷新运行时 |

Matrix 仍为 Human-in-the-loop 主通道；上述能力面向 **平台集成、自动化编排、能力市场**，与 Matrix 互补。

---

## 架构总览

```
hermes-worker start()
  mirror_all()                         # 现有：MinIO → workspace
       ↓
CapabilityEngine.start()             # 新增统一引擎
  ├─ NacosClient                     # httpx 直连 Nacos AI Registry
  ├─ AgentRegistry                   # F1：注册 + 长连接心跳
  ├─ CapabilityStore                 # F2：skill/mcp 状态 + MinIO 持久化
  ├─ ControlServer                   # F3：HTTP 控制端口
  └─ CapabilityWatcher               # F4：变更检测 + 热应用
       ↓
bridge → _sync_skills → _copy_mcporter_config → start_gateway()
```

**外部关系（精简）**

- Nacos **业务 API**（发现 / 下载 / 注册）：Worker **直连** Nacos Server（`/nacos/v3/client/ai/*`）
- Nacos **认证**：`nacos` / `none` 直连 Nacos login；`sts-hiclaw` 仅经 hiclaw-controller 签发 STS（不代理业务 API）
- MCP **工具调用**：`mcporter` → Higress AI Gateway（`HICLAW_AI_GATEWAY_URL` + Worker Gateway Key）
- 平台侧 CR Provisioning（`remoteSkills`、`spec.mcpServers`）与 Worker 自管可在 `hybrid` 模式并存

---

## F1 — Nacos 注册与长连接

### 目标

Worker 启动后在 Nacos AI Registry 注册可发现身份，**持续在线**直至进程退出；其他 Agent / 平台可通过 Nacos 检索到该 Worker 及其能力快照。

### 行为

| 项 | 设计 |
|----|------|
| 注册类型 | **A2A AgentCard**（非 Naming `registerInstance`） |
| 注册内容 | `name`、`description`、`labels`（`runtime=hermes`、`workerName`、`team`…）、`capabilities`（当前 skill / MCP 列表）、**控制端口 URL**（见 F3） |
| 启动时机 | `bridge` 完成后首次注册（需已知 skill/mcp 快照） |
| 长连接 | 后台 **HeartbeatLoop**：按间隔刷新 AgentCard / 续租（具体 API 以部署 Nacos 版本 OpenAPI 为准；无原生 push 时用轮询 + 定期 re-register） |
| 断线恢复 | 指数退避重连；恢复后全量 re-register 并刷新 capabilities |
| 退出 | `stop()` / SIGTERM：**注销** AgentCard（best-effort） |

### 验收

- Nacos 控制台 / Client API 可查到 Worker AgentCard，状态在线
- 控制端口 URL 写入 AgentCard metadata，平台可直接调用 F3 API
- 杀进程后 AgentCard 下线或租约过期

---

## F2 — Skill / MCP 自管理与持久化

### 目标

Worker 作为 **超级智能体**：自行从 Nacos 挂载/卸载 Skill 与 MCP，状态持久化到 MinIO，不依赖 Manager 逐条推送。

### Skill

| 项 | 设计 |
|----|------|
| 发现 / 安装 | `GET /nacos/v3/client/ai/skills` → 下载 ZIP → `${HERMES_HOME}/skills/{name}/` |
| 触发 | 启动：`NACOS_BOOTSTRAP_SKILLS`；运行时：F3 控制 API / Agent `find-skills`（改调 `hermes nacos`） |
| 持久化 | 安装后立即 push → MinIO `agents/{worker}/skills/`；`capability-state.json` 记录来源（nacos）、版本、label |
| 防误删 | `sync.pull_all()` **不得**删除 `capability-state.json` 标记为 Nacos 自装的 skill |

### MCP

| 项 | 设计 |
|----|------|
| 发现 | `/nacos/v3/client/ai/mcp*` → 元数据映射为 mcporter 条目 |
| 映射 | `{HIGLAW_AI_GATEWAY_URL}/mcp-servers/{higressRoute}/mcp` + `Authorization: Bearer {HICLAW_WORKER_GATEWAY_KEY}` |
| 元数据约定 | `metadata.higressRoute`、`metadata.transport`（平台统一规范） |
| 写入 | workspace `mcporter-servers.json` + `config/mcporter.json` → `_copy_mcporter_config()` |
| 前提 | MCP 已在 Higress 注册；Worker Consumer 已授权对应路由 |

### 持久化模型

```json
// agents/{worker}/capability-state.json（Worker 自管，push 到 MinIO）
{
  "skills": {
    "literature-review": { "source": "nacos", "version": "1.0.0", "installedAt": "..." }
  },
  "mcp": {
    "github": { "source": "nacos", "higressRoute": "github", "installedAt": "..." }
  },
  "prompts": {
    "SOUL.md": { "hash": "...", "updatedAt": "..." },
    "AGENTS.md": { "hash": "...", "updatedAt": "..." }
  }
}
```

启动时：`mirror_all()` 恢复 `capability-state.json` → CapabilityEngine 按记录 reconcile Nacos 与本地（缺则补、版本落后则升）。

**hybrid 模式**：platform CR 下发的 skill/mcp 与自管条目 **按名合并**；同名时 **Nacos 自管覆盖** platform。

---

## F3 — 控制端口（非 Matrix 指令通道）

### 目标

暴露 Worker 本地 HTTP 服务，供 Nacos 平台、hiclaw-controller、外部编排器下发指令，**不经过 Matrix Room**。

### 绑定

| 项 | 设计 |
|----|------|
| 端口 | `HERMES_CONTROL_PORT`（默认 `8088`，与现有 `HICLAW_CONSOLE_PORT` 对齐）；K8s 可通过 Worker CR `spec.expose` 经 Higress 对外发布 |
| 监听 | `127.0.0.1`（仅本机）或 `0.0.0.0`（容器 / 需 expose 时），由 `HERMES_CONTROL_BIND` 控制 |
| 鉴权 | `Authorization: Bearer {HERMES_CONTROL_TOKEN}`；token 由 hiclaw-controller 注入（不写 Nacos） |
| 框架 | `asyncio` + 轻量 HTTP（如 `aiohttp` / stdlib 扩展）；与 gateway 同进程，由 `CapabilityEngine` 管理生命周期 |

### API 草案（`/v1`）

| Method | Path | 说明 |
|--------|------|------|
| `GET` | `/health` | 存活探针 |
| `GET` | `/status` | 运行态：AgentCard 是否在线、已装 skill/mcp、gateway 状态 |
| `GET` | `/capabilities` | 当前 capability-state 快照 |
| `POST` | `/capabilities/skills/install` | body: `{ "name", "version?", "label?" }` |
| `DELETE` | `/capabilities/skills/{name}` | 卸载并更新持久化 |
| `POST` | `/capabilities/mcp/mount` | body: `{ "name" }` 或 `{ "discover": "label=..." }` |
| `DELETE` | `/capabilities/mcp/{name}` | 卸载 MCP |
| `POST` | `/prompt/reload` | 从 MinIO / Nacos 重新拉取 SOUL.md、AGENTS.md 并 re-bridge |
| `POST` | `/agent/reregister` | 强制刷新 Nacos AgentCard |

控制 API 执行成功后：更新 `capability-state.json` → push MinIO → 触发热应用（F4）→ 刷新 AgentCard capabilities（F1）。

Matrix 任务分配 **不变**；控制端口面向机器到机器（M2M）编排。

---

## F4 — Skill / Prompt / MCP 变更监听与热更新

### 目标

不重启 Worker 即可感知并应用 skill、prompt（SOUL/AGENTS）、MCP 配置变更。

### 监听源

| 对象 | 检测方式 | 热应用动作 |
|------|----------|------------|
| **Nacos Skill** | 轮询 Client API 版本 / label（间隔 `NACOS_WATCH_INTERVAL`，默认 60s） | 下载 → 覆盖 `${HERMES_HOME}/skills/` → push MinIO → gateway 重新加载 skill 目录 |
| **Nacos MCP** | 轮询 MCP 元数据 | 重建 mcporter JSON → `_copy_mcporter_config()` |
| **Prompt** | ① Nacos AgentSpec / config（若绑定）② MinIO `SOUL.md` / `AGENTS.md` mtime（`sync_loop` 已有 pull） | `bridge_openclaw_to_hermes()` 增量重跑 prompt 相关字段 |
| **本地 workspace** | `watchdog` 或 periodic diff `capability-state.json` vs 磁盘 | 与上一致 |

### 热更新流程

```
CapabilityWatcher.tick()
  → diff(desired from Nacos, actual from capability-state + disk)
  → 有变更则 CapabilityStore.apply(delta)
  → 持久化 MinIO
  → RuntimeReloader.reload(scope: skills | mcp | prompts)
  → AgentRegistry.refresh_capabilities()   # 回写 F1 AgentCard
```

`RuntimeReloader` 职责边界：

- **skills / mcporter**：文件级更新 + 通知 gateway（若 hermes-agent 支持 reload hook；否则记录「下次 turn 生效」并在 `/status` 暴露）
- **prompts**：re-bridge SOUL/AGENTS，**不**重启 Matrix 连接

---

## Nacos 客户端实现

**一次性移除 `@nacos-group/cli`**，全仓库改由 Python 模块承担；不保留 CLI 兼容层。

```
hermes/src/hermes_worker/
├── capability/
│   ├── engine.py          # CapabilityEngine：编排 F1–F4
│   ├── store.py           # capability-state + MinIO 读写
│   ├── watcher.py         # F4
│   ├── control.py         # F3 HTTP server
│   └── reloader.py        # 热应用
└── nacos/
    ├── config.py
    ├── auth.py            # nacos | none | sts-hiclaw（对齐 Go nacos_credential.go）
    ├── client.py
    ├── skill.py
    ├── mcp.py
    ├── agent_registry.py  # F1
    └── cli.py             # `hermes nacos`：find-skills / 运维只读子命令
```

认证（`NACOS_AUTH_TYPE`）：

| 模式 | 行为 |
|------|------|
| `nacos` | 直连 Nacos login；**不需要** Controller |
| `none` | 无鉴权 |
| `sts-hiclaw` | `POST /api/v1/credentials/sts` 拿 STS → 业务请求仍直连 Nacos；Worker CR `accessEntries` 须含 `skill/*`、`mcp/*`、`a2a/*` |

同步移除 CLI 的镜像与脚本：`hermes/Dockerfile`、`openclaw-base/Dockerfile`、`manager/Dockerfile.copaw`、全部 `hiclaw-find-skill.sh`、`hiclaw-find-worker.sh`。

---

## 环境变量

Worker CR `spec.env`（**不要** `HICLAW_*` 前缀，与系统键冲突会被忽略）：

```bash
# F1
NACOS_AGENT_REGISTER=1
NACOS_AGENT_CARD_NAME=hermes-${HICLAW_WORKER_NAME}
NACOS_HEARTBEAT_INTERVAL=30s

# F2
NACOS_BOOTSTRAP_SKILLS=literature-review@label:stable
NACOS_MCP_NAMES=github,pubmed
NACOS_CAPABILITY_MODE=hybrid          # off | platform | self | hybrid

# F3
HERMES_CONTROL_PORT=8088
HERMES_CONTROL_BIND=0.0.0.0
# HERMES_CONTROL_TOKEN 由 controller 注入

# F4
NACOS_WATCH_INTERVAL=60s
NACOS_WATCH_ENABLED=1
```

集群级（hiclaw-controller 注入）：

```bash
SKILLS_API_URL=nacos://nacos.example.com:8848/public
NACOS_AUTH_TYPE=sts-hiclaw            # 或 nacos / none
HICLAW_AI_GATEWAY_URL=...
HICLAW_WORKER_GATEWAY_KEY=...
HICLAW_CONTROLLER_URL=...             # 仅 sts-hiclaw
```

---

## 代码改动清单

| 区域 | 改动 |
|------|------|
| `hermes/src/hermes_worker/worker.py` | `start()` 启动 `CapabilityEngine`；`stop()` 注销 + 停控制端口 |
| `hermes/src/hermes_worker/sync.py` | 保护自管 skill；mcporter / prompt pull 回调接入 F4 |
| `hermes/pyproject.toml` | `httpx`、HTTP server 依赖；`hermes nacos` entry point |
| `hermes/Dockerfile` 等 | 移除 `@nacos-group/cli` |
| `manager/agent/**/find-skills/**` | Nacos 后端 → `hermes nacos` |
| Worker CR / Helm | 可选 `spec.expose` 发布控制端口；注入 `HERMES_CONTROL_TOKEN` |

---

## 实施阶段

| 阶段 | 交付 | 验收 |
|------|------|------|
| **P0** | `nacos/*` + 移除 CLI + `CapabilityStore` + F2 bootstrap | 启动后 skill/mcp 自装且重启不丢 |
| **P0** | F3 控制端口 + 基础 API（install/mount/status） | curl 控制 API 可挂载 skill，无需 Matrix |
| **P1** | F1 AgentRegistry + HeartbeatLoop | Nacos 可见在线 AgentCard（含 control URL） |
| **P1** | F4 Nacos 轮询 watcher + mcporter/skill 热更新 | 控制台更新 skill 版本后 Worker 自动同步 |
| **P2** | F4 prompt 热更新 + gateway reload 钩子 | SOUL/AGENTS 变更不重启即可生效 |
| **P2** | 公共 nacos 层供 copaw-worker 复用 | 多 runtime 控制 API 契约一致 |

P0 捆绑：**Python 模块 + 卸 CLI + 改 shell + F2 + F3 最小 API**。

---

## 参考

- hiclaw-controller Nacos 客户端：`hiclaw-controller/internal/executor/nacos_ai_service.go`
- Hermes 启动链：`hermes/src/hermes_worker/worker.py`
- CoPaw Worker HTTP 健康/控制模式参考：`copaw/src/copaw_worker/health.py`
- Nacos 3.x MCP Client API：alibaba/nacos PR #13610
