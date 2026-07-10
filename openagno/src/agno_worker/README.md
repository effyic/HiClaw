# Agno Worker — 多租户动态智能体

基于 Agno SDK 的通用型 Agent Worker：**内置标准租户流水线**（MySQL `agno_agent` 表驱动）负责 Prompt、MCP、Session、Skill 等核心逻辑；**可选 PVC 扩展 Hook** 在标准流水线输出之上做 enrich / transform / filter。

核心设计：**只构建一个动态 Agent**（`cache_callables=False`），每次 `run()` 通过 callable `instructions` / `tools` 按租户/会话/角色实时组装配置；AgentSpec 仅作角色目录 fallback。

---

## 1. 架构分层

```
HTTP (/v1/chat, /v1/chat/stream)
  → api/identity.py           解析 tenant-id / user-id / session-id
  → hooks/filters.py          tenant_id 必填校验（可配置）+ 可选 pre/post filter
  → runtime/engine.py         单一动态 Agent
  → runtime/builder.py        pre_hook / instructions / tools / post_hook
  → tenant/service.py         标准流水线编排 + run-scoped 缓存
      ├── tenant/context.py   租户 / 角色解析
      ├── tenant/store.py     agno_agent 配置（连接池 + TTL 缓存）
      ├── tenant/prompt.py    Prompt 与 knowledge_filters 组装
      ├── tenant/mcp.py       MCP 服务器配置
      ├── tenant/skills.py    Skill 目录扫描
      ├── tenant/session.py   会话状态更新
      └── tenant/data.py      数据查询工具（当前 stub）
  → hooks/registry.py         可选 PVC 扩展 Hook
```

---



## 2. 配置来源


| 组件                           | 主配置源                                   | Fallback                 |
| ---------------------------- | -------------------------------------- | ------------------------ |
| system_prompt / instructions | MySQL `agno_agent`                     | AgentSpec 当前角色           |
| knowledge_filters            | `agno_agent.knowledge_ids`             | AgentSpec 角色 `knowledge` |
| MCP 服务器                      | `agno_agent.mcp_config`                | 环境变量 WeKnora 默认          |
| Skill catalog                | 文件系统 `AGNO_SKILLS_DIR`                 | —                        |
| 业务上下文                        | 标准流水线 + `enrich_business_context_hook` | —                        |


**合并规则**（`hooks/compose.py`）：租户流水线产出与 AgentSpec 通过 `pick_hook_or_spec()` 合并——流水线有数据则用流水线，否则用 AgentSpec。

---



## 3. HTTP 入口与身份解析


| 端点                     | 说明                   |
| ---------------------- | -------------------- |
| `POST /v1/chat`        | 同步对话                 |
| `POST /v1/chat/stream` | SSE 流式对话             |
| `GET /health`          | 健康检查                 |
| `GET /status`          | 运行时状态（Hook 加载来源、指纹等） |




### 3.1 请求头优先级

文件：`api/identity.py`


| 字段           | 请求头                           | 备选                |
| ------------ | ----------------------------- | ----------------- |
| `tenant_id`  | `tenant-id` / `x-tenant-id`   | Query `tenant_id` |
| `user_id`    | `user-id` / `x-user-id`       | Body → Query      |
| `session_id` | `session-id` / `x-session-id` | Query             |
| `role_code`  | `role-code` / `x-role-code`   | Query `role_code` |


请求示例：

```bash
curl -X POST http://localhost:8090/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -H "tenant-id: tenant-a" \
  -H "user-id: alice" \
  -H "session-id: conv-123" \
  -H "role-code: triage" \
  -d '{"message": "你好"}'
```



### 3.2 tenant_id 必填校验

当 `AGNO_REQUIRE_TENANT_ID=true` 时，请求在扩展 Hook 之前即被拒绝（HTTP 403）。未设置时，缺失 tenant_id 会在租户解析阶段 fallback 到 `"default"`。

### 3.3 写入 RunContext

`runtime/engine.py` 将 HTTP 解析结果写入 `metadata` 和 Agno run kwargs：

```python
run_metadata["tenant_id"] = tenant_id   # 非空时
run_metadata["user_id"] = user_id
run_metadata["session_id"] = session_id
run_metadata["role_code"] = role_code
```

Hook 开发者可通过以下路径读取租户信息：


| 读取位置              | 路径                                                      |
| ----------------- | ------------------------------------------------------- |
| metadata          | `run_context.metadata["tenant_id"]`                     |
| dependencies      | `run_context.dependencies["tenant"]["tenant_id"]`       |
| user_profile      | `run_context.dependencies["user_profile"]["tenant_id"]` |
| knowledge_filters | `run_context.knowledge_filters.get("tenant_id")`        |


---



## 4. 单次 Run 执行流程

```
RequestFilterPipeline.apply_pre_filter()   # tenant_id 校验 + request_pre_filter_hook

Agno Agent.run()
  │
  ├─ pre_hook (AgentBuilder._make_pre_hook)
  │    ├─ TenantSessionManager.init_session()
  │    ├─ TenantAgentService.prepare_run_context()   # 一次性解析，写入 run-scoped 缓存
  │    │    ├─ TenantContextResolver → AgentStore (MySQL)
  │    │    ├─ enrich_business_context_hook (可选)
  │    │    ├─ TenantPromptBuilder → transform_prompt_hook (可选)
  │    │    ├─ TenantMCPBuilder → transform_mcp_servers_hook (可选)
  │    │    └─ TenantSkillCatalog → transform_skills_hook (可选)
  │    ├─ knowledge_filters → run_context.knowledge_filters
  │    └─ build_run_dependencies() → run_context.dependencies
  │
  ├─ instructions() (callable)
  │    ├─ get_prompt_bundle()          # 读 run-scoped 缓存
  │    └─ pick_hook_or_spec vs AgentSpec + skill_catalog_summary
  │
  ├─ tools() (callable)
  │    ├─ get_mcp_servers()            # 读 run-scoped 缓存
  │    ├─ build_mcp_tools + mcp_connection_hook (可选)
  │    ├─ DynamicSkillsManager.build_tools
  │    ├─ TenantDataProvider.get_tools
  │    └─ mcp_tool_filter_hook (可选)
  │
  ├─ LLM 推理 + 工具调用
  │
  └─ post_hook
       └─ TenantSessionManager.build_session_updates()
            → transform_workflow_hook / transform_session_state_hook (可选)

RequestFilterPipeline.apply_post_filter()  # request_post_filter_hook (可选)
```

同一次 run 内，`TenantContextResolver.resolve()` 与 `prepare_run_context()` 结果通过 run-scoped 缓存复用，避免 pre_hook 与 instructions/tools 重复查库。

---



## 5. 标准租户流水线（内置，不可通过 PVC 替换）

以下逻辑由 `tenant/` 模块实现，**不**通过 PVC Hook 加载。如需定制，应修改 MySQL `agno_agent` 配置，或使用对应的 transform / enrich 扩展 Hook。


| 能力        | 实现                  | 说明                                                        |
| --------- | ------------------- | --------------------------------------------------------- |
| 租户/角色解析   | `tenant/context.py` | `tenant_id`：metadata → session_state → factory → `"default"`；`role_code`：metadata → session_state → `"default"` |
| 配置加载      | `tenant/store.py`   | `tenant_id` + `role_code` 精确匹配 → 同租户 `default` 行 → 全局 `default` 租户 |
| Prompt 组装 | `tenant/prompt.py`  | system_prompt、instructions、context_filters                |
| MCP 配置    | `tenant/mcp.py`     | 从 `mcp_config` 构建 MCPServerConfig 列表                      |
| Skill 扫描  | `tenant/skills.py`  | 扫描 `AGNO_SKILLS_DIR`，按 tenant_ids 过滤                      |
| 会话管理      | `tenant/session.py` | init_session、build_session_updates（同步当前行的 `workflow`） |
| 数据工具      | `tenant/data.py`    | `query_tenant_data`（当前为配置摘要 stub）                         |


### 5.1 角色（Agent）解析 — 选定 `agno_agent` 行

**Worker 不会根据 `workflow` JSON、`route_key` 或 `kind` 自动切换 Agent。** 每次 run 加载哪一行配置，仅由解析出的 `role_code` 决定：

```
HTTP role-code / x-role-code（或 query role_code）
  → metadata.role_code
  → session_state.role_code / active_role / role / agent
  → "default"
```

`AgentStore.load_agent_resolved(tenant_id, role_code)` 的 DB fallback 链：

1. `tenant_id` + 精确 `role_code`
2. 同租户 `role_code = 'default'`
3. 全局租户 `default` + `role_code = 'default'`

多阶段业务（如分诊 → 问诊 → 病历）需在**调用方**切换 `role-code`，或在 `transform_session_state_hook` 中更新 `session_state.active_role` / `role_code`；框架本身不做阶段路由。

### 5.2 `workflow` JSON 字段用途

`agno_agent.workflow` 为可扩展 JSON 元数据。标准流水线**只读取以下字段**：

| 字段 | 读取位置 | 作用 |
| ---- | -------- | ---- |
| `prompt_append` | `tenant/prompt.py` | 追加到 system prompt |
| `instructions_append` | `tenant/prompt.py` | 追加到 instructions |
| `phase` | `tenant/session.py` | 写入 `session_state.phase` |
| `knowledge_provider` | `tenant/prompt.py` | 覆盖知识检索 provider |

`kind`、`route_key`、`next_phase` 等自定义字段**不被标准流水线用于选 Agent 或自动流转**；可作为业务标注，或由 PVC Hook / 外部编排读取。`post_hook` 会将**当前已加载行**的 `workflow` 同步到 `session_state.workflow`。


---



## 6. 扩展 Hook（PVC 可选，共 11 个）

挂载目录：`AGNO_HOOKS_DIR`（默认 `/etc/hiclaw/hooks`）。

- 目录**不存在**时 Worker 正常启动，仅运行标准流水线
- 每个 Hook **独立可选**，缺省不报错
- 文件变更后约 30s 热重载（SHA256 指纹）



### 6.1 加载机制

`HookRegistry` 对每个 Hook 名按以下文件名顺序扫描，**找到即停**：

```
hooks.py → filters.py → transform.py → business.py → prompt.py
→ mcp.py → skills.py → session.py → data.py → __init__.py
```

模块内以**同名函数**导出，例如 `business.py` 中定义 `enrich_business_context_hook`。

接口定义与类型见 `hooks/protocols.py` 的 `EXTENSION_HOOK_NAMES`。

### 6.2 Hook 一览


| Hook                           | 调用时机                | 调用位置                                  | 作用                                          |
| ------------------------------ | ------------------- | ------------------------------------- | ------------------------------------------- |
| `enrich_business_context_hook` | prepare_run_context | `tenant/service.py`                   | 在标准 business_context 上注入业务表数据（如 department） |
| `transform_prompt_hook`        | prepare_run_context | `tenant/service.py`                   | 变换标准 prompt_bundle                          |
| `transform_mcp_servers_hook`   | prepare_run_context | `tenant/service.py`                   | 变换 MCP 服务器列表                                |
| `transform_skills_hook`        | prepare_run_context | `tenant/service.py`                   | 变换 Skill catalog                            |
| `transform_workflow_hook`      | post_hook           | `tenant/service.py`                   | 变换 workflow / session_state 更新              |
| `transform_session_state_hook` | post_hook           | `tenant/service.py`                   | 变换 session_state 更新                         |
| `mcp_connection_hook`          | MCP 连接前             | `tenant/service.py` → `mcp/loader.py` | 鉴权、env/headers 注入                           |
| `mcp_tool_filter_hook`         | tools 组装后           | `tenant/service.py`                   | 裁剪最终工具列表                                    |
| `result_processing_hook`       | 数据查询后               | `tenant/service.py`                   | 格式化 `query_tenant_data` 结果                  |
| `request_pre_filter_hook`      | HTTP 请求前            | `hooks/filters.py`                    | 准入控制；返回 `{"allowed": False}` 拒绝             |
| `request_post_filter_hook`     | HTTP 响应后            | `hooks/filters.py`                    | 修改 reply / session_id                       |


**transform 类 Hook 返回值规则**：返回 `None` 表示不修改，使用标准流水线产出；返回非 `None` 则替换对应字段。

### 6.3 enrich_business_context_hook

```python
def enrich_business_context_hook(
    run_context: Any,
    base_context: dict[str, Any],
) -> dict[str, Any] | None:
    """
    base_context 含: tenant_id, role_code, agent_config, agents
    （agents 为同租户下 list_agents() 结果，每项含 role_code / workflow 等）
    返回 partial dict，合并进 business_context。
    """
```

参考实现：`examples/hooks/business.py`（department 表 enrich）。

### 6.4 request_pre_filter_hook

```python
def request_pre_filter_hook(
    user_context: UserContext,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    # 通过: return {"metadata": {...}} 或 return None
    # 拒绝: return {"allowed": False, "reason": "..."}
```



### 6.5 transform_prompt_hook

```python
def transform_prompt_hook(
    run_context: Any,
    prompt_bundle: dict[str, Any],
) -> dict[str, Any] | None:
    # prompt_bundle 含: system_prompt, instructions, context_filters,
    #                   agent_config, business_context
```

---



## 7. PVC 挂载目录结构（推荐）

与 `examples/hooks/` 对齐：

```
/etc/hiclaw/hooks/
├── business.py     # enrich_business_context_hook
├── transform.py    # transform_prompt/mcp/skills/workflow/session_state_hook
├── filters.py      # request_pre/post_filter_hook
├── mcp.py          # mcp_connection_hook, mcp_tool_filter_hook
└── ...
```

空实现（返回 `None`）即可，不影响标准流水线运行：

```python
def transform_prompt_hook(run_context, prompt_bundle):
    return None
```

---



## 8. AgentSpec 合并策略

文件：`hooks/compose.py`

```python
def pick_hook_or_spec(hook_value, spec_value):
    # 租户流水线 / transform hook 有数据 → 用 hook_value
    # 否则 → 用 AgentSpec fallback
```

**AgentSpec fallback 角色**（`hooks/compose.py` 的 `resolve_active_role`，仅在与 AgentSpec 合并 prompt/tools 时使用，**不决定** MySQL 加载哪一行）：`session_state["active_role"]` → `role` → `phase` → `agent`，均需在 `spec.agents` 中存在，否则用第一个角色。MySQL 行选择见 [§5.1](#51-角色agent解析--选定-agno_agent-行)。

---



## 9. RunContext 关键字段

pre_hook 执行后，Hook 开发者可用的 `run_context` 字段：


| 字段                                  | 来源               | 说明                                     |
| ----------------------------------- | ---------------- | -------------------------------------- |
| `metadata["tenant_id"]`             | HTTP 解析          | 租户 ID                                  |
| `metadata["user_id"]`               | HTTP 解析          | 用户 ID                                  |
| `metadata["session_id"]`            | HTTP / Agno      | 会话 ID                                  |
| `metadata["user_requirements"]`     | pre_hook 从用户消息提取 | Skill 匹配                               |
| `session_state`                     | Agno 持久化         | 含 `active_role`、`phase`、`workflow` 等   |
| `knowledge_filters`                 | pre_hook 写入      | WeKnora MCP 知识检索参数                     |
| `dependencies["tenant"]`            | pre_hook 写入      | `{tenant_id, knowledge_ids, provider}` |
| `dependencies["user_profile"]`      | pre_hook 写入      | `{user_id, tenant_id}`                 |
| `dependencies["skill_catalog"]`     | pre_hook 写入      | Skill 元数据列表                            |
| `dependencies["business_context"]`  | pre_hook 写入      | 含 enrich hook 注入的业务数据                  |
| `dependencies["_tenant_run_cache"]` | 内部               | run-scoped 缓存，勿依赖其结构                   |


---



## 10. 环境变量


| 变量                            | 说明                         | 默认                      |
| ----------------------------- | -------------------------- | ----------------------- |
| `AGNO_AGENT_DB_URL`           | 租户配置 MySQL（`agno_agent` 表） | 必填                      |
| `AGNO_DB_URL`                 | 会话持久化 DB                   | Postgres                |
| `AGNO_REQUIRE_TENANT_ID`      | 缺失 tenant_id 时拒绝请求         | `false`                 |
| `AGNO_AGENT_CONFIG_CACHE_TTL` | agno_agent 配置 TTL 缓存（秒）    | `60`                    |
| `AGNO_AGENT_DB_POOL_SIZE`     | MySQL 连接池大小                | `5`                     |
| `AGNO_AGENT_DB_POOL_OVERFLOW` | 连接池 overflow               | `10`                    |
| `AGNO_HOOKS_DIR`              | 扩展 Hook PVC 挂载路径           | `/etc/hiclaw/hooks`     |
| `AGNO_AGENTSPEC_DIR`          | AgentSpec YAML 目录          | `/etc/hiclaw/agentspec` |
| `AGNO_SKILLS_DIR`             | Skill 文件目录                 | `/etc/hiclaw/skills`    |
| `AGNO_CONTROL_PORT`           | HTTP 端口                    | `8090`                  |
| `AGNO_SPEC_WATCH_INTERVAL`    | AgentSpec/Hook 热重载间隔（秒）    | `30`                    |


---



## 11. Worker 启动与热重载

```
Worker.start()
  → HookRegistry(hooks_dir)          # 可选；目录缺失时仅标准流水线
  → AgnoRuntime.build()
  → AgentBuilder.build_dynamic_agent()
  → AgnoAPIServer 监听 HTTP
  → _watch_loop()                    # 检测 AgentSpec / Hook 文件变更并热重载
```

热重载时清空 `AgentStore` TTL 缓存并重置 MySQL 连接池。

---



## 12. 错误处理


| 异常                     | 触发场景                                                  | HTTP 响应                |
| ---------------------- | ----------------------------------------------------- | ---------------------- |
| `RequestRejectedError` | tenant_id 缺失（`AGNO_REQUIRE_TENANT_ID`）或 pre_filter 拒绝 | 403                    |
| `HookLoadError`        | 代码主动 `registry.call()` 未加载的 Hook                      | 500                    |
| `HookExecutionError`   | 扩展 Hook 运行时抛错                                         | 500，`detail` 含 hook 名称 |


PVC 目录缺失或 Hook 函数未实现**不会**导致启动失败。

---



## 13. 关键源文件索引


| 文件                   | 职责                              |
| -------------------- | ------------------------------- |
| `api/identity.py`    | tenant / user / session ID 解析   |
| `api/server.py`      | HTTP 入口                         |
| `worker.py`          | Worker 生命周期、热重载                 |
| `runtime/engine.py`  | 单一动态 Agent 构建与 run              |
| `runtime/builder.py` | pre/post/instructions/tools 注入点 |
| `tenant/service.py`  | 标准流水线编排 + 扩展 Hook 调度            |
| `tenant/context.py`  | 租户上下文解析（含 run-scoped 缓存）        |
| `tenant/store.py`    | MySQL agno_agent 配置             |
| `tenant/db.py`       | MySQL 连接池                       |
| `hooks/registry.py`  | PVC 扩展 Hook 加载                  |
| `hooks/protocols.py` | Hook 接口与类型定义                    |
| `hooks/compose.py`   | 流水线/Spec 合并、dependencies 组装     |
| `hooks/filters.py`   | 请求 pre/post filter              |
| `examples/hooks/`    | PVC Hook 参考实现                   |


