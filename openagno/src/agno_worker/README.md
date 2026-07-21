# Agno Worker — 多租户动态智能体

基于 Agno SDK 的通用型 Agent Worker：**内置标准租户流水线**（MySQL `agno_agent` 表驱动）负责 Prompt、MCP、Session、Skill 等核心逻辑；**可选 PVC 扩展 Hook** 在标准流水线输出之上做 enrich / transform / filter。

核心设计：**只构建一个动态 Agent**（`cache_callables=False`），每次 `run()` 通过 callable `instructions` / `tools` 按租户/会话/角色实时组装配置；AgentSpec 仅作角色目录 fallback。

---

## 1. 架构分层

```
HTTP (/effyic/v1/chat, /effyic/v1/chat/stream, /effyic/v1/sessions*)
  → api/identity.py           解析 tenant-id / user-id / session-id
  → hooks/filters.py          tenant_id 必填校验（可配置）+ 可选 pre/post filter
  → runtime/engine.py         单一动态 Agent（注入请求级 request_id contextvars）
  → moderation/               敏感内容 Guardrail（可选，pre_hooks 首位）
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


| 端点                                                   | 说明                                              |
| ---------------------------------------------------- | ----------------------------------------------- |
| `POST /effyic/v1/chat`                               | 同步对话                                            |
| `POST /effyic/v1/chat/stream`                        | SSE 流式对话                                        |
| `GET /effyic/v1/sessions`                            | 按 `user_id` 分页列出历史会话                            |
| `GET /effyic/v1/sessions/{session_id}`               | 获取会话详情（含 `chat_history`）                        |
| `GET /effyic/v1/sessions/{session_id}/conversation`  | 获取纯对话（基于 `run_input`，不含 `<additional context>`） |
| `GET /effyic/v1/sessions/{session_id}/runs`          | 获取会话下所有 run                                     |
| `GET /effyic/v1/sessions/{session_id}/runs/{run_id}` | 获取单次 run 详情                                     |
| `GET /effyic/health`                                 | 健康检查                                            |
| `GET /effyic/status`                                 | 运行时状态（Hook 加载来源、指纹等）                            |




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
curl -X POST http://localhost:8090/effyic/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -H "tenant-id: tenant-a" \
  -H "user-id: alice" \
  -H "session-id: conv-123" \
  -H "role-code: triage" \
  -H "x-debug-request: false" \
  -d '{"message": "你好", "enable_thinking": false}'
```



### 3.2 跳过会话持久化（`x-ignore-db`）


| 请求头    | 行为                                            |
| ------ | --------------------------------------------- |
| `true` | **读但不写** session 表：本次 run 使用空内存会话，结束后不 upsert |


适用于后台一次性推理、不希望污染对话历史的接口。优先于 `x-debug-request`。

```bash
curl -X POST http://localhost:8090/effyic/v1/chat \
  -H "tenant-id: tenant-a" \
  -H "user-id: alice" \
  -H "session-id: ephemeral-1" \
  -H "x-ignore-db: true" \
  -d '{"message": "仅本次推理，不要入库"}'
```



### 3.2.1 结构化输出（`output_schema`）


同步 / 流式 chat 请求体可带可选字段 `output_schema`（plain JSON Schema object，或 provider `json_schema` 信封）。Worker 按次传给 `Agent.arun(output_schema=...)`，**不**改共享 Agent 实例；结构化结果序列化为 JSON 字符串写入响应的 `content`（与 stream `RunContent.content` 同名字段）。

适用于后台一次性抽取（如电子病历字段），不建议对话采集 Agent 常开。

```bash
curl -X POST http://localhost:8090/effyic/v1/chat \
  -H "tenant-id: 1" \
  -H "user-id: system" \
  -H "role-code: system_emr_generate_agent" \
  -H "x-ignore-db: true" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "根据材料填写病历字段…",
    "enable_thinking": false,
    "output_schema": {
      "title": "MedicalEmrFields",
      "type": "object",
      "properties": {
        "chief_complaint": {"type": "string", "description": "主诉"},
        "allergy_history": {"type": "string", "description": "过敏史"}
      },
      "required": ["chief_complaint", "allergy_history"],
      "additionalProperties": false
    }
  }'
```

同步响应字段与 stream 对齐：

```json
{ "content": "...", "session_id": "..." }
```

stream `RunContent`（无 thinking 时不含 `reasoning_content`）：

```json
{ "event": "RunContent", "content": "...", "session_id": "..." }
```



### 3.3 会话入库裁剪（`x-debug-request`）


| 请求头     | 入库行为                                                        |
| ------- | ----------------------------------------------------------- |
| 缺省      | 由 `AGNO_DEBUG_REQUEST_DEFAULT` 决定（Helm 默认 `false`，精简写入）     |
| `true`  | 完整写入（调试/审计）                                                 |
| `false` | 精简写入：仅保留 user/assistant 对话、思考内容、run 基础字段与必要 `session_state` |


精简模式会过滤：tool 消息、media、metrics、events、system 消息及内部缓存字段。

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


| 能力        | 实现                  | 说明                                                                                                              |
| --------- | ------------------- | --------------------------------------------------------------------------------------------------------------- |
| 租户/角色解析   | `tenant/context.py` | `tenant_id`：metadata → session_state → factory → `"default"`；`role_code`：metadata → session_state → `"default"` |
| 配置加载      | `tenant/store.py`   | `tenant_id` + `role_code` 精确匹配 → 同租户 `default` 行 → 全局 `default` 租户                                              |
| Prompt 组装 | `tenant/prompt.py`  | system_prompt、instructions、context_filters                                                                      |
| MCP 配置    | `tenant/mcp.py`     | 从 `mcp_config` 构建 MCPServerConfig；HTTP MCP 默认注入身份 + `x-*` headers                                               |
| Skill 扫描  | `tenant/skills.py`  | 扫描 `AGNO_SKILLS_DIR`，按 tenant_ids 过滤                                                                            |
| 会话管理      | `tenant/session.py` | init_session、build_session_updates（同步当前行的 `workflow`）                                                           |
| 数据工具      | `tenant/data.py`    | `query_tenant_data`（当前为配置摘要 stub）                                                                               |




### 5.1 角色（Agent）解析 — 选定 `agno_agent` 行

**Worker 不会根据** `workflow` **JSON 的** `kind` **/** `phase` **自动切换 Agent。** 每次 run 加载哪一行配置，仅由解析出的 `role_code` 决定：

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

### 5.2 采集对话薄协议（collection dialogue）

配置驱动的槽位采集 FSM，适用于问诊 / 分诊 / 问卷等。**进度只写在** `session_state.collection`，随 Agno session 落库（`AGNO_DB_URL`），精简入库白名单含 `collection`，**集群多副本不丢状态**。

启用方式（`agno_agent.workflow`）：

```json
{
  "kind": "medical",
  "phase": "inquiry",
  "collection": {
    "kind": "collection_dialogue",
    "confirm_required": true,
    "ask_batch_size": 2,
    "schema": {
      "source": "inline",
      "fields": [
        {"name": "主诉", "required": true, "description": "主要症状"},
        {"name": "持续时间", "required": true}
      ]
    },
    "required_actions": [
      {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"}
    ],
    "auto_mark_done": true
  }
}
```

也可顶层 `"kind": "collection_dialogue"`。`schema.source=mcp` 时由模型先调字段列表 MCP，再把结果传给 `collection_load_schema(fields_json=...)`。

**结束后必做动作（统一字段 `required_actions`）**

单动作与多动作共用同一列表；一项即单动作，多项即多动作。不再使用 `complete_action`。

```json
"required_actions": [
  {"type": "mcp", "tool": "md_get_dept_list", "when": "missing_empty"},
  {"type": "mcp", "tool": "mec_create_emr_case", "when": "missing_empty"}
]
```

| 配置 | 含义 |
|------|------|
| `required_actions[].type` | 目前仅支持 `mcp` |
| `required_actions[].tool` | MCP 工具名 |
| `required_actions[].when` | `missing_empty`（默认：必填采齐 **且 probe 结束后**）或 `before_mark_done`（仅 mark_done 前） |
| `auto_mark_done` | 必做 MCP 全部成功后是否自动 `completed=true`（默认 true） |
| `probe` | 可选扩采 loop：`gate_fields` 或全部 required 齐后进入 `phase=probing`（先扩采再填剩余槽位/写库） |

**扩采 loop（`probe`）— 平台只提供 FSM；问什么由租户 Agent 的 `goal` / `instructions` 决定**

```json
"probe": {
  "enabled": true,
  "min_rounds": 2,
  "max_rounds": 5,
  "allow_skip": true,
  "goal": "optional domain purpose (from the published agent)",
  "hints": [],
  "gate_fields": ["slot_a", "slot_b"],
  "early_finish_keywords": ["拒绝", "refuse", "emergency"]
}
```

| 配置 | 含义 |
|------|------|
| `enabled` | 是否启用扩采 |
| `min_rounds` | 未达轮数时禁止提前 `finish`（除非 reason 命中 `early_finish_keywords`） |
| `max_rounds` | 最多追问轮数（每轮 1 问） |
| `allow_skip` | 是否允许 `collection_probe_finish` 提前结束 |
| `goal` | 扩采目的说明（注入协议）；医疗话术/分诊逻辑写在 Agent 指令，不写在平台 |
| `hints` | 可选维度清单，**推荐 `[]`**，由模型按 `goal` + 对话自适应追问 |
| `gate_fields` | 可选；这些槽位齐后即可 probing（其余 required 扩采后再采）。省略则等全部 required 齐 |
| `early_finish_keywords` | 提前结束 reason 白名单；默认仅通用「拒绝/refuse…」，领域词由租户配置 |

| 工具 | 作用 |
|------|------|
| `collection_probe_note` | 记录一轮扩采笔记并 `probe_rounds++`（答完须同轮调用） |
| `collection_probe_finish` | 结束扩采（`allow_skip=true`） |

写库工具名、摘要语义、领域规则均由租户 Agent / `required_actions` 配置，平台不做场景硬编码。


| 工具                         | 作用                                      |
| -------------------------- | --------------------------------------- |
| `collection_load_schema`   | 加载字段清单（`schema.source=inline` 时通常已自动加载） |
| `collection_update_fields` | 合并采集值（**仅允许 schema 内字段名**）并重算 missing   |
| `collection_status`        | 只读进度                                    |
| `collection_probe_note`    | 扩采笔记（phase=probing）                     |
| `collection_probe_finish`  | 结束扩采 loop                               |
| `collection_confirm`       | 用户确认（可选，由 `confirm_required` 控制）        |
| `collection_complete`      | 可选：写入 `draft_payload` 快照                |
| `collection_mark_done`     | 写库 / 更新成功后记账；必做动作未完成时拒绝                   |


写库 MCP（如 `mec_create_emr_case`）始终对模型可见，可早写、可多次更新。缺必填字段时由 prompt + `missing` 驱动继续追问；`completed` 表示「至少成功写过一次」，不冻结 FSM——用户补充病情后可再 `update_fields` 并再次写库。

客户端可用请求头 `x-collection-confirm: true` 在本轮标记确认。同步回复末尾附加 `<!--COLLECTION_STATUS {...}-->`；流式场景请读 `session_state.collection` 或 run `metadata.collection_status`（不要只依赖 SSE 文本标记）。

实现：`tenant/collection.py`；接线：`tenant/service.py`、`tenant/session.py`、`tenant/prompt.py`、`tenant/data.py`、`runtime/storage.py`、`runtime/builder.py`。

---



## 6. 扩展 Hook（PVC 可选，共 12 个）

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
| `mcp_headers_hook`             | MCP headers 默认透传后   | `tenant/service.py`                   | 二次加工 HTTP MCP headers；返回 `None` 表示不改        |
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
├── mcp.py          # mcp_connection_hook, mcp_headers_hook, mcp_tool_filter_hook
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

**AgentSpec fallback 角色**（`hooks/compose.py` 的 `resolve_active_role`，仅在与 AgentSpec 合并 prompt/tools 时使用，**不决定** PG 加载哪一行）：`session_state["active_role"]` → `role` → `phase` → `agent`，均需在 `spec.agents` 中存在，否则用第一个角色。

---



## 9. RunContext 关键字段

pre_hook 执行后，Hook 开发者可用的 `run_context` 字段：


| 字段                                  | 来源               | 说明                                       |
| ----------------------------------- | ---------------- | ---------------------------------------- |
| `metadata["tenant_id"]`             | HTTP 解析          | 租户 ID                                    |
| `metadata["user_id"]`               | HTTP 解析          | 用户 ID                                    |
| `metadata["session_id"]`            | HTTP / Agno      | 会话 ID                                    |
| `metadata["role_code"]`             | HTTP 解析          | 角色编码                                     |
| `metadata["request_headers"]`       | HTTP 原始请求头       | 供 `x-*` 透传 / `mcp_headers_hook`；精简持久化时剥离 |
| `metadata["user_requirements"]`     | pre_hook 从用户消息提取 | Skill 匹配                                 |
| `session_state`                     | Agno 持久化         | 含 `active_role`、`phase`、`workflow` 等     |
| `knowledge_filters`                 | pre_hook 写入      | WeKnora MCP 知识检索参数                       |
| `dependencies["tenant"]`            | pre_hook 写入      | `{tenant_id, knowledge_ids, provider}`   |
| `dependencies["user_profile"]`      | pre_hook 写入      | `{user_id, tenant_id}`                   |
| `dependencies["skill_catalog"]`     | pre_hook 写入      | Skill 元数据列表                              |
| `dependencies["business_context"]`  | pre_hook 写入      | 含 enrich hook 注入的业务数据                    |
| `dependencies["_tenant_run_cache"]` | 内部               | run-scoped 缓存，勿依赖其结构                     |


---



## 10. 环境变量


| 变量                            | 说明                                          | 默认                           |
| ----------------------------- | ------------------------------------------- | ---------------------------- |
| `AGNO_AGENT_DB_URL`           | 租户配置 PostgreSQL（`agno_agent` 表）             | 必填（`postgresql+psycopg://…`） |
| `AGNO_DB_URL`                 | 会话持久化 DB                                    | Postgres                     |
| `AGNO_REQUIRE_TENANT_ID`      | 缺失 tenant_id 时拒绝请求                          | `false`                      |
| `AGNO_AGENT_CONFIG_CACHE_TTL` | agno_agent 配置 TTL 缓存（秒）                     | `60`                         |
| `AGNO_AGENT_DB_POOL_SIZE`     | pg 连接池大小                                    | `5`                          |
| `AGNO_AGENT_DB_POOL_OVERFLOW` | 连接池 overflow                                | `10`                         |
| `AGNO_HOOKS_DIR`              | 扩展 Hook PVC 挂载路径                            | `/etc/hiclaw/hooks`          |
| `AGNO_AGENTSPEC_DIR`          | AgentSpec YAML 目录                           | `/etc/hiclaw/agentspec`      |
| `AGNO_SKILLS_DIR`             | Skill 文件目录                                  | `/etc/hiclaw/skills`         |
| `AGNO_CONTROL_PORT`           | HTTP 端口                                     | `8090`                       |
| `AGNO_ENABLE_SESSION_API`     | 挂载 `/effyic/v1/sessions*` 会话查询 API          | `true`                       |
| `AGNO_DEBUG_REQUEST_DEFAULT`  | 未传 `x-debug-request` 时是否完整入库                | `false`（精简入库）                |
| `AGNO_ENABLE_AGENTOS`         | 启用完整 AgentOS（根路径 API + os.agno.com）；与对话延迟无关 | `false`                      |
| `AGNO_TELEMETRY`              | 上报 os-api.agno.com（非 AgentOS）               | `false`（本仓库默认）               |
| `AGNO_SPEC_WATCH_INTERVAL`    | AgentSpec/Hook 热重载间隔（秒）                     | `30`                         |

### 10.1 敏感内容检测（`moderation/`，可选）

配置 `SENSITIVE_CONTENT_SERVICE_URL` 后启用；未配置时 Worker 行为与原来完全一致。`SensitiveContentGuardrail` 挂载在 Agent `pre_hooks` **首位**（业务 pre_hook 之前），因此脱敏后的文本才进入 `user_requirements`、Prompt 与会话上下文；被阻断/终止的原始输入不写入会话历史。

支持八种响应行为：结束对话 / 固定回复 / 阻断 / 脱敏放行 / 仅记录 / 业务动作 / 自定义回复 / **语气调整（`ADJUST_PROMPT`）**。`ADJUST_PROMPT` 放行请求并将 `prompt_guidance` 经请求上下文注入本轮 `instructions`（阻断类胜出时不注入）；注入块含冲突优先级说明。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `SENSITIVE_CONTENT_SERVICE_URL` | sensitive-content 管理服务地址（空 = 关闭） | 空 |
| `SENSITIVE_CONTENT_RUNTIME_TOKEN` | 内部 API Runtime Token | 空 |
| `SENSITIVE_CONTENT_CACHE_PATH` | 快照落盘路径（不可写降级纯内存） | `/var/lib/agno/moderation/policy-snapshot.json` |
| `SENSITIVE_CONTENT_REFRESH_INTERVAL` | 快照刷新间隔（秒） | `30` |
| `SENSITIVE_CONTENT_MAX_STALE` | 快照过期上限（秒） | `600` |
| `SENSITIVE_CONTENT_FAIL_MODE` | 无有效快照/正则超时处理：`open` / `closed` | `open` |
| `SENSITIVE_CONTENT_FINGERPRINT_KEY` | 命中事件 HMAC-SHA256 指纹密钥 | 空 |
| `SENSITIVE_CONTENT_REGEX_TIMEOUT_MS` | 单条正则匹配超时（毫秒） | `50` |
| `SENSITIVE_CONTENT_BREAKER_THRESHOLD` | 正则连续超时熔断阈值 | `3` |
| `SENSITIVE_CONTENT_BREAKER_COOLDOWN` | 熔断冷却时长（秒） | `60` |


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

热重载时清空 `AgentStore` TTL 缓存并重置 PG 连接池。

---



## 12. 错误处理


| 异常                     | 触发场景                                                  | HTTP 响应                |
| ---------------------- | ----------------------------------------------------- | ---------------------- |
| `RequestRejectedError` | tenant_id 缺失（`AGNO_REQUIRE_TENANT_ID`）或 pre_filter 拒绝 | 403                    |
| `HookLoadError`        | 代码主动 `registry.call()` 未加载的 Hook                      | 500                    |
| `HookExecutionError`   | 扩展 Hook 运行时抛错                                         | 500，`detail` 含 hook 名称 |
| `SensitiveContentDecisionError` | 敏感内容命中且最终行为为 `BLOCK_REQUEST`             | 422 `sensitive_content_blocked` |
| `SensitivePolicyUnavailableError` | 无有效策略快照且 `SENSITIVE_CONTENT_FAIL_MODE=closed` | 503 `sensitive_policy_unavailable` |

敏感内容策略按实际解析到的 `agno_agent.id` 隔离。运行时请求
`/internal/v1/tenants/{tenant_id}/agents/{agent_id}/policy-snapshot`，本地缓存键为
`(tenant_id, agent_id)`；未绑定规则返回合法空策略，不属于“策略不可用”。同租户角色回退到
`default` 时使用实际默认 Agent 的绑定，跨租户模板回退或数据库中没有对应 Agent 时使用空策略。
命中事件同时上报 `agent_id`，策略版本格式为
`global-{G}:tenant-{T}:agent-{A}`。升级后的缓存格式版本为 2，旧租户级缓存会被删除重拉。


PVC 目录缺失或 Hook 函数未实现**不会**导致启动失败。

---



## 13. 关键源文件索引


| 文件                   | 职责                                                          |
| -------------------- | ----------------------------------------------------------- |
| `api/identity.py`    | tenant / user / session ID 解析                               |
| `api/server.py`      | HTTP 入口                                                     |
| `api/sessions.py`    | `/effyic/v1/sessions*` AgentOS 会话 API + `/conversation` 纯对话 |
| `worker.py`          | Worker 生命周期、热重载                          |
| `runtime/engine.py`  | 单一动态 Agent 构建与 run                       |
| `runtime/builder.py` | pre/post/instructions/tools 注入点          |
| `runtime/storage.py` | 按 `x-debug-request` 控制入库裁剪               |
| `runtime/agent.py`   | StorageAwareAgent（覆盖 Agno scrub 钩子）      |
| `tenant/service.py`  | 标准流水线编排 + 扩展 Hook 调度                     |
| `tenant/context.py`  | 租户上下文解析（含 run-scoped 缓存）                 |
| `tenant/store.py`    | PG agno_agent 配置                         |
| `tenant/db.py`       | PG 连接池                                   |
| `hooks/registry.py`  | PVC 扩展 Hook 加载                           |
| `hooks/protocols.py` | Hook 接口与类型定义                             |
| `hooks/compose.py`   | 流水线/Spec 合并、dependencies 组装              |
| `hooks/filters.py`   | 请求 pre/post filter                       |
| `moderation/guardrail.py` | SensitiveContentGuardrail（pre_hooks 首位） |
| `moderation/detector.py`  | 敏感内容检测引擎（归一化 / 正则超时 / 熔断）  |
| `moderation/snapshot.py`  | 策略快照客户端（内存 + 落盘 + 后台刷新）      |
| `moderation/reporter.py`  | 命中事件批量上报（有界队列 + HMAC 指纹）      |
| `examples/hooks/`    | PVC Hook 参考实现                            |

