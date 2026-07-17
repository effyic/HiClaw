# 敏感词业务模块 — 前端对接接口文档

> 服务：`sensitive-content`（敏感内容规则管理服务）
> 面向读者：管理后台前端开发
> 对应代码：`sensitive-content/src/sensitive_content/api/`（`admin.py` / `metrics.py`）
> 文档版本：与仓库当前代码同步（2026-07）

---

## 1. 概述

敏感词模块提供以下能力，前端管理后台需要对接的接口分为两组：

| 分组 | 前缀 | 用途 |
|------|------|------|
| 管理 API | `/api/v1/tenants/{tenant_id}` | 敏感内容**类型**与**规则**（词库）的增删改查、启停，命中事件明细查询，审计日志查询 |
| 统计 API | `/api/v1/tenants/{tenant_id}/metrics` | 命中概览、按规则/类型/行为聚合、时间趋势 |

> 另有内部 API（`/internal/v1/*`，Runtime Token 鉴权）供 Agent 检测端拉取策略快照、上报命中事件，**不供前端调用**，本文档不涉及。

### 1.1 核心概念

- **敏感内容类型（sensitive-type）**：一类敏感内容（如"政治敏感"、"隐私信息"），决定命中后的**响应行为**（`action`）。规则必须挂在某个类型下。
- **敏感内容规则（sensitive-rule）**：具体的词条或正则模式（即"敏感词"本体），命中即触发所属类型的行为。
- **Agent 规则绑定**：规则启用后不会自动对租户内所有 Agent 生效；只有被 `agno_agent.id` 显式绑定的规则才会进入该 Agent 的策略快照。
- **全局 vs 租户**：`tenant_id` 路径参数取 `global` 时表示全局（平台级）资源；取具体租户 ID 时表示该租户的资源。全局类型/规则对所有租户可见，但仍需逐 Agent 绑定；租户可通过"覆盖规则"（`overrides_global_rule_id`）替换或禁用已绑定的全局规则。
- **命中事件（hit-event）**：检测端上报的命中记录，**不含用户消息原文**，含明文 `session_id` 供跳转会话详情。
- **审计日志（audit-log）**：所有写操作的操作记录；`changes` 字段只存字段名 + 值哈希/长度，不含明文。

---

## 2. 通用约定

### 2.1 服务地址

服务默认监听 `0.0.0.0:8091`（环境变量 `SENSITIVE_CONTENT_HOST` / `SENSITIVE_CONTENT_PORT`）。生产环境经认证网关转发，前端以网关下发的实际 Base URL 为准。下文示例均省略 Base URL。

### 2.2 鉴权

所有管理/统计 API 均要求：

```
Authorization: Bearer <SENSITIVE_CONTENT_ADMIN_TOKEN>
```

| 情况 | HTTP 状态 | 响应体 |
|------|-----------|--------|
| Token 缺失 / 错误 | 401 | `{"detail": {"code": "unauthorized", "message": "invalid admin token"}}` |
| 服务端未配置 Token | 503 | `{"detail": {"code": "token_not_configured", "message": "admin token not configured"}}` |

**操作人标识**：写操作会记录操作人，取请求头 `X-Operator`。生产环境由认证网关注入（覆盖客户端值），前端**无需也不应**自行设置；本地联调无网关时可手工传入，缺省记为 `admin-token`。

### 2.3 路径参数 `tenant_id`

| 取值 | 含义 |
|------|------|
| `global` | 全局资源（平台管理员视角）；在命中事件/统计接口中表示**跨全部租户** |
| 其他任意字符串 | 具体租户 ID |

**权限约束**（服务端强制，前端需做好交互提示）：

- 租户上下文（`tenant_id != global`）**可以读**全局类型/规则，但**不能修改、启停、删除**全局资源，否则返回 403（`forbidden_global_type` / `forbidden_global_rule`）。前端应对 `tenant_id=''`（全局）的行禁用编辑/删除按钮，仅提供"创建覆盖规则"入口。
- 全局上下文（`tenant_id = global`）创建的规则不能设置 `overrides_global_rule_id`（400 `invalid_override`）。

### 2.4 分页

所有列表接口统一使用：

| Query 参数 | 类型 | 默认 | 约束 |
|------------|------|------|------|
| `page` | int | 1 | ≥ 1 |
| `page_size` | int | 50 | 1 ~ 500 |

列表响应统一形态：

```json
{
  "items": [ ... ],
  "page": 1,
  "page_size": 50,
  "total": 123
}
```

### 2.5 时间参数与格式

- 筛选参数 `from` / `to` 为 ISO 8601 时间串（如 `2026-07-01T00:00:00Z`），可只传其一，区间为闭区间（`>= from`、`<= to`）。
- 响应中的时间字段（`created_at`、`updated_at`、`hit_at` 等）均为带时区的 ISO 8601 字符串（UTC）。

### 2.6 错误响应格式

前端需处理**三种**错误体形态：

| 来源 | HTTP 状态 | 响应体形态 |
|------|-----------|-----------|
| 业务错误（StoreError） | 400 / 403 / 404 / 409 | `{"error": {"code": "...", "message": "...", ...扩展字段}}` |
| 鉴权错误（HTTPException） | 401 / 503 | `{"detail": {"code": "...", "message": "..."}}` |
| 参数校验失败（FastAPI/Pydantic） | 422 | `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` |

**业务错误码总表**：

| HTTP | code | 触发场景 | 前端建议处理 |
|------|------|----------|--------------|
| 400 | `empty_pattern` | 规则 pattern 为空或全空白 | 表单校验提示 |
| 400 | `pattern_too_long` | pattern 超过 512 字符 | 表单校验提示（前端可预校验） |
| 400 | `invalid_regex` | 正则语法错误 | 表单校验提示，message 含具体原因 |
| 400 | `regex_too_complex` | 正则含嵌套量词（如 `(a+)+`，有灾难性回溯风险） | 提示用户简化正则 |
| 400 | `invalid_action_config` | `action_config` 含白名单之外的 key | 表单校验提示 |
| 400 | `invalid_override` | 全局上下文创建规则时设置了 `overrides_global_rule_id` | 隐藏该字段即可避免 |
| 400 | `invalid_override_target` | 覆盖目标不是"存在且未删除的全局规则" | 提示重新选择覆盖目标 |
| 400 | `invalid_granularity` | trend 接口 `granularity` 非法 | 使用固定枚举下拉即可避免 |
| 401 | `unauthorized` | Bearer Token 无效 | 跳转登录 / 提示凭据失效 |
| 403 | `forbidden_global_type` | 租户上下文修改全局类型 | 禁用全局行的编辑入口 |
| 403 | `forbidden_global_rule` | 租户上下文修改全局规则 | 同上，引导创建覆盖规则 |
| 404 | `type_not_found` | 类型不存在 / 不属于当前租户可见范围 | 提示"不存在或无权访问" |
| 404 | `rule_not_found` | 规则不存在 / 属于其他租户 | 同上 |
| 404 | `agent_not_found` | Agent 不存在或不属于路径租户 | 刷新 Agent 数据后重试 |
| 409 | `duplicate_code` | 同租户下类型 `code` 重复（显式传入时），或自动分配失败 | 提示冲突并重试 |
| 409 | `duplicate_rule` | 同租户下存在规范化后等价的规则（message 含已存在规则 id） | 提示已存在等价规则 |
| 409 | `rule_not_assignable` | 尝试给 Agent 新增停用、类型停用或 orphaned 的规则 | 禁止新选该项；已绑定项仍可保留或解除 |
| 409 | `type_in_use` | 删除类型时仍被规则引用；扩展字段 `references` 为引用数 | 提示"仍有 N 条规则引用该类型" |
| 503 | `token_not_configured` | 服务端未配置 Token | 提示联系运维 |

### 2.7 枚举值

**响应行为 `action`**（8 种，与 `sensitive_action` 目录表一致）：

| 值 | 名称 | 说明 |
|----|------|------|
| `END_CONVERSATION` | 结束对话 | 返回终止话术并结束当前请求 |
| `FIXED_REPLY` | 固定回复 | 返回预设固定文案，不调用 LLM |
| `BLOCK_REQUEST` | 阻断请求 | 对话端返回 422 `sensitive_content_blocked` |
| `REDACT_AND_CONTINUE` | 脱敏放行 | 替换命中区间后继续对话 |
| `LOG_ONLY` | 仅记录 | 记录命中事件后放行 |
| `BUSINESS_ACTION` | 业务动作 | 调用检测端预注册的白名单处理器 |
| `CUSTOM_RESPONSE` | 自定义响应 | 返回租户配置的自定义响应文案 |
| `ADJUST_PROMPT` | 语气调整 | 放行请求，并将 `prompt_guidance` 注入本轮对话 instructions |

开箱全局类型 `self_harm`（`priority=70`，`ADJUST_PROMPT`）附带**中文基础规则集**（关键词覆盖，非完整自伤风险分类器）。与其它类型共同命中时仍按类型优先级裁决：

| 共同命中 | 最终行为 | 指引注入 |
|----------|----------|----------|
| `self_harm(70)` + `privacy(60)` | `ADJUST_PROMPT` | 注入（不做隐私脱敏） |
| `self_harm(70)` + `violence(80)` | `BLOCK_REQUEST` | 不注入 |
| `privacy` 胜出且同时命中 `self_harm` | `REDACT_AND_CONTINUE` | 注入（叠加） |

**匹配模式 `match_mode`**：

| 值 | 说明 |
|----|------|
| `text` | 文本包含匹配（默认；受 `case_sensitive` / `normalize` 影响） |
| `regex` | 正则匹配（pattern 保持原样，大小写语义由引擎 flag 决定） |

**`action_config`** 允许的 key（其余一律 400 `invalid_action_config`）：

| key | 适用行为 | 说明 |
|-----|----------|------|
| `reply_text` | `FIXED_REPLY` / `CUSTOM_RESPONSE` / `END_CONVERSATION` | 回复文案 |
| `replacement` | `REDACT_AND_CONTINUE` | 脱敏替换符（如 `***`） |
| `business_action` | `BUSINESS_ACTION` | 业务动作处理器名称 |
| `prompt_guidance` | `ADJUST_PROMPT`（必填，非空，≤2000） | 注入本轮 instructions 的语气指引；其它行为携带则 400 |

**规则生效状态 `effective_status`**（只读，规则查询响应携带）：

| 值 | 说明 |
|----|------|
| `active` | 正常 |
| `orphaned` | 该规则是覆盖规则，但其覆盖的全局规则已被删除；不会进入检测端策略快照。前端应醒目标注并引导用户清理 |

---

## 3. 数据结构

### 3.1 类型对象（SensitiveType）

```json
{
  "id": 12,
  "tenant_id": "",
  "code": "politics",
  "name": "政治敏感",
  "action": "BLOCK_REQUEST",
  "action_config": {},
  "priority": 100,
  "description": "涉政敏感内容，默认阻断请求",
  "enabled": true,
  "deleted": false,
  "created_at": "2026-06-01T08:00:00+00:00",
  "updated_at": "2026-06-01T08:00:00+00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 类型 ID |
| `tenant_id` | string | 所属租户；`""`（空串）表示全局 |
| `code` | string | 类型编码，1~64 字符，同租户内唯一（逻辑删除后可复用）；创建时可省略，由服务端自动生成 |
| `name` | string | 展示名称，1~128 字符 |
| `action` | string | 响应行为枚举（见 2.7） |
| `action_config` | object | 行为配置，key 白名单见 2.7 |
| `priority` | int | 优先级，数值越大越靠前（列表按 priority 降序） |
| `description` | string | 描述 |
| `enabled` | bool | 是否启用 |
| `deleted` | bool | 逻辑删除标记（正常查询恒为 false） |
| `created_at` / `updated_at` | string | 创建/更新时间 |

系统预置 6 个全局种子类型：`politics`（政治敏感）、`porn`（色情低俗）、`violence`（暴力恐怖）、`privacy`（隐私信息）、`abuse`（辱骂攻击）、`general`（一般敏感词），均可通过管理 API 修改。

### 3.2 规则对象（SensitiveRule）

```json
{
  "id": 101,
  "tenant_id": "tenant-a",
  "type_id": 12,
  "pattern": "示例敏感词",
  "match_mode": "text",
  "case_sensitive": false,
  "normalize": true,
  "overrides_global_rule_id": null,
  "description": "",
  "priority": 0,
  "remark": "",
  "enabled": true,
  "deleted": false,
  "created_at": "2026-07-01T02:00:00+00:00",
  "updated_at": "2026-07-01T02:00:00+00:00",
  "effective_status": "active"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 规则 ID |
| `tenant_id` | string | 所属租户；`""` 表示全局 |
| `type_id` | int | 所属类型 ID（必须是全局类型或本租户类型） |
| `pattern` | string | 敏感词文本或正则，非空，≤ 512 字符 |
| `match_mode` | string | `text` / `regex` |
| `case_sensitive` | bool | 是否大小写敏感（默认 false） |
| `normalize` | bool | 文本匹配前是否归一化（NFKC、去零宽字符、空白折叠；默认 true） |
| `overrides_global_rule_id` | int \| null | 覆盖的全局规则 ID（仅租户规则可设置） |
| `description` | string | 描述 |
| `priority` | int | 优先级（列表按 priority 降序） |
| `remark` | string | 备注 |
| `enabled` | bool | 是否启用 |
| `effective_status` | string | 只读，`active` / `orphaned`（见 2.7） |

**覆盖规则语义**（前端需理解以正确设计交互）：

- 租户创建 `overrides_global_rule_id = X` 且 `enabled = true` 的规则 → 用本规则**替换**全局规则 X；
- 同样的覆盖规则但 `enabled = false` → 相当于在本租户**禁用**全局规则 X；
- 全局规则 X 被删除后，覆盖规则变为 `orphaned`，不再生效。

**重复判定**：同租户内，`match_mode` / `case_sensitive` / `normalize` 相同且 pattern **规范化后相等**的规则视为重复，创建/更新会被拒绝（409 `duplicate_rule`）。例如 `normalize=true, case_sensitive=false` 时，`"AbC "` 与 `"abc"` 等价。

### 3.3 命中事件对象（HitEvent）

```json
{
  "event_id": "3f6e2c9a-1b7d-4e8f-9a2b-6c5d4e3f2a1b",
  "rule_id": 101,
  "type_id": 12,
  "rule_action": "BLOCK_REQUEST",
  "final_action": "BLOCK_REQUEST",
  "selected": true,
  "final_rule_id": 101,
  "tenant_id": "tenant-a",
  "agent_id": 42,
  "session_id": "sess-20260701-0001",
  "policy_version": "global-5:tenant-3:agent-2",
  "hit_count": 2,
  "hit_at": "2026-07-01T03:12:45+00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_id` | string(UUID) | 事件唯一 ID |
| `rule_id` / `type_id` | int | 命中的规则/类型 |
| `rule_action` | string | 该规则所属类型配置的行为 |
| `final_action` | string | 本次请求最终执行的行为（一次请求命中多条规则时按类型优先级裁决） |
| `selected` | bool | 本条命中是否为最终裁决所选（一次请求只有一条 `selected=true`） |
| `final_rule_id` | int | 最终裁决所选的规则 ID |
| `tenant_id` | string | 真实租户 ID（恒非 global） |
| `agent_id` | int \| null | 命中所属 `agno_agent.id`；升级前历史数据可能为 null |
| `session_id` | string | 明文会话 ID；可跳转网关会话接口 `/effyic/v1/sessions/{session_id}` 查看完整会话。**旧数据可能为空串** |
| `policy_version` | string | 命中时的策略版本（`global-{N}:tenant-{M}:agent-{A}`） |
| `hit_count` | int | 该规则在该请求中的命中次数 |
| `hit_at` | string | 命中时间 |

> 隐私说明：命中事件不存储用户消息原文，前端不要设计"查看原文"入口；查看上下文请通过 `session_id` 跳转会话详情。命中事件默认保留 90 天（服务端自动清理）。

### 3.4 审计日志对象（AuditLog）

```json
{
  "id": 501,
  "tenant_id": "tenant-a",
  "action": "update",
  "target_kind": "rule",
  "target_id": 101,
  "changes": {
    "pattern": {"sha256": "9f86d0…", "length": 5},
    "priority": {"sha256": "4a44dc…", "length": 2},
    "overrides_global_rule_id": {"null": true}
  },
  "operator": "alice@example.com",
  "created_at": "2026-07-01T03:20:00+00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `action` | string | `create` / `update` / `delete` / `enable` / `disable` |
| `target_kind` | string | `rule` / `type` / `agent_binding` |
| `target_id` | int | 目标对象 ID |
| `changes` | object | 变更字段元数据：每个字段为 `{"sha256": "...", "length": N}` 或 `{"null": true}`，**不含明文**。前端只展示"哪些字段被改过"即可 |
| `operator` | string | 操作人（网关注入的 `X-Operator`） |

---

## 4. 管理 API

统一前缀：`/api/v1/tenants/{tenant_id}`

### 4.1 敏感内容类型

#### 4.1.1 创建类型

```
POST /api/v1/tenants/{tenant_id}/sensitive-types
```

请求体：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `code` | string | 否 | 服务端生成（如 `t_xxxxxxxxxxxx`） | 1~64 字符，同租户唯一；管理端可不传 |
| `name` | string | 是 | — | 1~128 字符 |
| `action` | string | 是 | — | 行为枚举（见 2.7） |
| `action_config` | object | 否 | `{}` | key 白名单见 2.7 |
| `priority` | int | 否 | 0 | |
| `description` | string | 否 | `""` | |
| `enabled` | bool | 否 | true | |

```json
{
  "name": "内部机密",
  "action": "FIXED_REPLY",
  "action_config": {"reply_text": "该话题不便讨论。"},
  "priority": 70,
  "description": "公司内部机密相关词条"
}
```

响应：`201 Created`，body 为完整类型对象（3.1）；未传 `code` 时响应体中含服务端生成的编码。

错误：`409 duplicate_code`（显式 `code` 冲突时）、`400 invalid_action_config`。

#### 4.1.2 类型列表

```
GET /api/v1/tenants/{tenant_id}/sensitive-types
```

| Query | 类型 | 默认 | 说明 |
|-------|------|------|------|
| `enabled` | bool | — | 按启用状态筛选 |
| `include_global` | bool | true | 租户上下文时是否包含全局类型（前端做"我的类型/全部类型"切换时使用） |
| `page` / `page_size` | int | 1 / 50 | 分页 |

响应：`200`，`{"items": [类型对象...], "page", "page_size", "total"}`。排序：`tenant_id` 升序（全局在前）→ `priority` 降序 → `id` 升序。

#### 4.1.3 类型详情

```
GET /api/v1/tenants/{tenant_id}/sensitive-types/{type_id}
```

响应：`200`，类型对象。租户可查看全局类型与本租户类型；其他租户的类型返回 `404 type_not_found`。

#### 4.1.4 更新类型

```
PUT /api/v1/tenants/{tenant_id}/sensitive-types/{type_id}
```

请求体（全部可选，只更新传入的字段；`code`、`enabled` 不可通过本接口修改，启停走专用接口）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | |
| `action` | string | 行为枚举 |
| `action_config` | object | 传入即整体替换 |
| `priority` | int | |
| `description` | string | |

响应：`200`，更新后的类型对象。

错误：`403 forbidden_global_type`（租户改全局）、`404 type_not_found`、`400 invalid_action_config`。

#### 4.1.5 启用 / 禁用类型

```
POST /api/v1/tenants/{tenant_id}/sensitive-types/{type_id}:enable
POST /api/v1/tenants/{tenant_id}/sensitive-types/{type_id}:disable
```

无请求体。响应：`200`，更新后的类型对象。

> 注意：禁用类型后，该类型下的所有规则（含租户规则）在检测端立即失效。前端应在禁用时给出影响提示。

#### 4.1.6 删除类型（逻辑删除）

```
DELETE /api/v1/tenants/{tenant_id}/sensitive-types/{type_id}
```

响应：`204 No Content`。

错误：`409 type_in_use`（仍被任意租户的未删除规则引用，响应含 `error.references` 引用计数）。前端应先引导用户处理关联规则。

### 4.2 敏感内容规则（敏感词条目）

#### 4.2.1 创建规则

```
POST /api/v1/tenants/{tenant_id}/sensitive-rules
```

请求体：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `type_id` | int | 是 | — | 所属类型（全局类型或本租户类型） |
| `pattern` | string | 是 | — | 非空，≤ 512 字符；`regex` 模式下须为合法且无嵌套量词的正则 |
| `match_mode` | string | 否 | `text` | `text` / `regex` |
| `case_sensitive` | bool | 否 | false | |
| `normalize` | bool | 否 | true | |
| `overrides_global_rule_id` | int | 否 | null | 覆盖的全局规则 ID；仅租户上下文可传 |
| `description` | string | 否 | `""` | |
| `priority` | int | 否 | 0 | |
| `remark` | string | 否 | `""` | |
| `enabled` | bool | 否 | true | 覆盖规则传 false 表示"在本租户禁用目标全局规则" |

```json
{
  "type_id": 12,
  "pattern": "(?i)forbidden\\s+topic",
  "match_mode": "regex",
  "description": "英文敏感话题",
  "priority": 10
}
```

响应：`201 Created`，完整规则对象（3.2）。

错误：`400 empty_pattern / pattern_too_long / invalid_regex / regex_too_complex / invalid_override / invalid_override_target`、`404 type_not_found`、`409 duplicate_rule`。

#### 4.2.2 规则列表

```
GET /api/v1/tenants/{tenant_id}/sensitive-rules
```

| Query | 类型 | 说明 |
|-------|------|------|
| `keyword` | string | 模糊搜索（对 `pattern` / `description` / `remark` 做不区分大小写的包含匹配） |
| `type_id` | int | 按类型筛选 |
| `enabled` | bool | 按启用状态筛选 |
| `page` / `page_size` | int | 分页 |

响应：`200`，`{"items": [规则对象...], ...分页}`。排序：`priority` 降序 → `id` 升序。

> 注意：规则列表**只返回路径租户自己的规则**（与类型列表不同，不含全局规则）。展示"本租户可绑定的全部词条"时，需要另外用 `tenant_id=global` 请求全局规则列表并在前端合并展示。

#### 4.2.3 规则详情

```
GET /api/v1/tenants/{tenant_id}/sensitive-rules/{rule_id}
```

响应：`200`，规则对象。规则不属于路径租户时返回 `404 rule_not_found`。

#### 4.2.4 更新规则

```
PUT /api/v1/tenants/{tenant_id}/sensitive-rules/{rule_id}
```

请求体（全部可选，只更新传入字段；`enabled` 走专用启停接口）：`type_id`、`pattern`、`match_mode`、`case_sensitive`、`normalize`、`overrides_global_rule_id`（可传 null 解除覆盖关系）、`description`、`priority`、`remark`。

校验与创建一致（pattern 校验基于"合并后的最终值"，例如只改 `match_mode` 为 `regex` 时会用现有 pattern 重新校验正则）。

响应：`200`，更新后的规则对象。

错误：`403 forbidden_global_rule`（租户改全局规则）、`404 rule_not_found / type_not_found`、`400` 系列校验错误、`409 duplicate_rule`。

#### 4.2.5 启用 / 禁用规则

```
POST /api/v1/tenants/{tenant_id}/sensitive-rules/{rule_id}:enable
POST /api/v1/tenants/{tenant_id}/sensitive-rules/{rule_id}:disable
```

无请求体。响应：`200`，更新后的规则对象。

#### 4.2.6 删除规则（逻辑删除）

```
DELETE /api/v1/tenants/{tenant_id}/sensitive-rules/{rule_id}
```

响应：`204 No Content`。

> 删除全局规则后，指向它的租户覆盖规则会变为 `orphaned`。

#### 4.2.7 Agent 规则绑定

创建 Agent 的表单可继续使用现有规则列表接口加载选项：并行请求
`GET /api/v1/tenants/global/sensitive-rules` 与
`GET /api/v1/tenants/{tenant_id}/sensitive-rules` 后合并。Agent 创建成功并获得
`agno_agent.id` 后，由 Agent 管理后端调用以下绑定接口。

```
GET    /api/v1/tenants/{tenant_id}/agents/{agent_id}/sensitive-rules
PUT    /api/v1/tenants/{tenant_id}/agents/{agent_id}/sensitive-rules
DELETE /api/v1/tenants/{tenant_id}/agents/{agent_id}/sensitive-rules
```

`GET` 支持 `keyword`、`type_id`、`page`、`page_size`，一次返回全局与本租户的规则选项：

```json
{
  "agent_id": 42,
  "selected_rule_ids": [101, 205],
  "items": [
    {
      "id": 101,
      "tenant_id": "",
      "pattern": "示例规则",
      "selected": true,
      "assignable": true,
      "inactive_reason": null
    }
  ],
  "page": 1,
  "page_size": 50,
  "total": 12
}
```

`inactive_reason` 可能为 `rule_disabled`、`type_disabled`、
`tenant_override_disabled` 或 `orphaned`。`assignable=false` 的规则不能新增选择；
若它已绑定，则仍会出现在 `selected_rule_ids` 中并可在编辑 Agent 时解除。

`PUT` 以完整集合整体替换，重复提交幂等，空数组表示清空：

```json
{"rule_ids": [101, 205]}
```

Agent 管理后端应在 Agent 创建/编辑保存后调用 `PUT`，仅在两侧都成功后向页面报告成功；
删除 Agent 时调用幂等 `DELETE`。绑定失败时 Agent 创建记录保持零绑定，编辑记录保持旧绑定，可安全重试。

绑定全局规则后，租户启用覆盖会自动替换它，禁用覆盖会使该 Agent 不执行它；
禁用规则不会删除绑定，重新启用后自动恢复。删除规则会自动移除相关 Agent 绑定。

### 4.3 命中事件明细

```
GET /api/v1/tenants/{tenant_id}/hit-events
```

`tenant_id=global` 表示跨全部租户查询（事件行内的 `tenant_id` 均为真实租户）。

| Query | 类型 | 说明 |
|-------|------|------|
| `rule_id` | int | 按规则筛选 |
| `type_id` | int | 按类型筛选 |
| `agent_id` | int | 按 `agno_agent.id` 精确筛选 |
| `session_id` | string | 按会话精确筛选 |
| `from` / `to` | datetime | 命中时间范围 |
| `page` / `page_size` | int | 分页 |

响应：`200`，`{"items": [命中事件对象...], ...分页}`。排序：`hit_at` 降序。

前端交互建议：`session_id` 非空时渲染为链接，跳转网关会话接口 `/effyic/v1/sessions/{session_id}` 对应的会话详情页。

### 4.4 审计日志

```
GET /api/v1/tenants/{tenant_id}/audit-logs
```

查询的是路径租户自身的操作记录（全局资源操作记录在 `global` 下）。

| Query | 类型 | 说明 |
|-------|------|------|
| `target_kind` | string | `rule` / `type` / `agent_binding` |
| `target_id` | int | 目标对象 ID |
| `action` | string | `create` / `update` / `delete` / `enable` / `disable` |
| `from` / `to` | datetime | 操作时间范围 |
| `page` / `page_size` | int | 分页 |

响应：`200`，`{"items": [审计日志对象...], ...分页}`。排序：`id` 降序（即时间倒序）。

---

## 5. 统计 API

统一前缀：`/api/v1/tenants/{tenant_id}/metrics`。所有接口支持 `from` / `to` 时间范围；`tenant_id=global` 表示跨全部租户聚合。

统计口径说明：

- **events**：命中事件条数（一次请求可能产生多条，每条对应一个命中的规则）；
- **hits**：命中次数合计（`hit_count` 求和，同一规则在同一请求内可命中多次）；
- **requests / hit_requests**：命中的请求数（按 `request_fingerprint` 去重）。

### 5.1 概览

```
GET /api/v1/tenants/{tenant_id}/metrics/summary
```

响应：

```json
{
  "total_events": 320,
  "total_hits": 415,
  "hit_requests": 118,
  "final_actions": {
    "BLOCK_REQUEST": 66,
    "REDACT_AND_CONTINUE": 40,
    "LOG_ONLY": 12
  }
}
```

`final_actions` 为"最终执行行为 → 请求数"的映射（只统计 `selected=true` 的裁决记录）。

### 5.2 按规则 Top N

```
GET /api/v1/tenants/{tenant_id}/metrics/by-rule
```

| Query | 类型 | 默认 | 约束 |
|-------|------|------|------|
| `top` | int | 10 | 1 ~ 1000 |

响应：

```json
{
  "items": [
    {
      "rule_id": 101,
      "type_id": 12,
      "events": 45,
      "hits": 60,
      "last_hit_at": "2026-07-15T09:30:00+00:00",
      "last_session_id": "sess-20260715-0042"
    }
  ]
}
```

按 `hits` 降序。`last_session_id` 是该规则最近一次命中的会话 ID（可能为 null），可作为跳转会话详情的示例入口。接口只返回 `rule_id`，规则 pattern 等展示信息需前端结合规则接口自行关联。

### 5.3 按类型 Top N

```
GET /api/v1/tenants/{tenant_id}/metrics/by-type
```

Query 同 5.2（`top`）。响应：

```json
{
  "items": [
    {"type_id": 12, "events": 120, "hits": 150, "last_hit_at": "2026-07-15T09:30:00+00:00"}
  ]
}
```

### 5.4 按行为

```
GET /api/v1/tenants/{tenant_id}/metrics/by-action
```

响应（双维度）：

```json
{
  "by_rule_action": [
    {"rule_action": "BLOCK_REQUEST", "events": 200, "hits": 260}
  ],
  "by_final_action": [
    {"final_action": "BLOCK_REQUEST", "requests": 66}
  ]
}
```

- `by_rule_action`：按规则配置行为统计命中事件/命中次数（全部事件）；
- `by_final_action`：按实际执行行为统计请求数（仅 `selected=true`，`request_fingerprint` 去重）。

### 5.5 时间趋势

```
GET /api/v1/tenants/{tenant_id}/metrics/trend
```

| Query | 类型 | 默认 | 说明 |
|-------|------|------|------|
| `granularity` | string | `day` | `hour` / `day` / `week` / `month`，其余值返回 400 `invalid_granularity` |

响应：

```json
{
  "items": [
    {"bucket": "2026-07-14T00:00:00+00:00", "events": 30, "hits": 41, "requests": 12},
    {"bucket": "2026-07-15T00:00:00+00:00", "events": 25, "hits": 33, "requests": 9}
  ]
}
```

`bucket` 为该粒度的时间桶起点，按时间升序；**没有命中的时间桶不会返回**，前端绘图时需自行补零。

---

## 6. 健康检查

```
GET /healthz
```

无需鉴权。响应：`200 {"status": "ok"}`。

---

## 7. 前端对接要点速查

1. **租户切换**：所有接口的租户上下文都在 URL 路径里（`{tenant_id}`），`global` 是特殊值；建议在 API client 层统一注入。
2. **全局资源只读**：租户视图下，`tenant_id=""` 的类型/规则行禁用编辑、启停、删除操作，规则提供"覆盖"入口。
3. **绑定后才生效**：规则启用只是必要条件；Agent 必须显式绑定。绑定更新会递增 Agent 策略版本，检测端在下次拉取快照时生效，无需"发布"步骤。
4. **表单预校验**：pattern 非空、≤512 字符可前端预校验；正则合法性与复杂度以服务端校验结果为准（`invalid_regex` / `regex_too_complex` 的 message 可直接展示）。
5. **三种错误体**：业务错误在 `error.code`，鉴权错误在 `detail.code`，参数校验错误 `detail` 是数组，需分别解析。
6. **统计与明细联动**：`by-rule` / `by-type` 只返回 ID，需要用规则/类型接口补充名称；`orphaned` 或已删除的规则 ID 可能查不到详情，需容错展示（如"规则 #101（已删除）"）。
7. **会话跳转**：命中事件与 `by-rule` 的 `last_session_id` 可跳转 `/effyic/v1/sessions/{session_id}`；空值（旧数据）不渲染链接。
8. **命中事件有保留期**：默认 90 天，超期数据被自动清理，时间筛选器不必提供过久的范围。
9. **Agent 表单**：创建时先用全局与租户规则列表加载选项，拿到 Agent ID 后由 Agent 后端整体保存绑定；编辑时用 Agent 绑定 GET 回显选中项和停用原因。
