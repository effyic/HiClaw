# sensitive-content

HiClaw 敏感内容规则管理服务：提供敏感内容类型/规则的 CRUD 管理、策略快照下发、命中事件采集与统计、审计日志。使用 chatai PostgreSQL 中独立的 `sensitive_content` schema，检测端（`SensitiveContentGuardrail`）位于 `openagno` 包内，通过内部 API 拉取快照并上报命中事件。

## 目录结构

```
sensitive-content/
├── pyproject.toml
├── Dockerfile                  # python-slim 多阶段
├── migrations/                 # 唯一数据库 schema 来源（版本化 SQL）
│   └── 0001_init.sql           # 7 张表 + 索引 + 种子类型数据
├── src/sensitive_content/
│   ├── cli.py                  # sensitive-content serve / migrate
│   ├── config.py               # 环境变量
│   ├── db.py                   # SQLAlchemy engine + 连接池
│   ├── migrate.py              # advisory lock + 版本化迁移
│   ├── models.py               # pydantic 模型 + 行为枚举 + 校验纯函数
│   ├── store.py                # SQL 访问层 + 合并算法/ETag 纯函数
│   ├── audit.py                # 审计写入（同事务）+ 策略版本递增
│   └── api/                    # admin / internal / metrics 路由
└── tests/
```

## 数据模型

7 张表，全部位于独立 `sensitive_content` schema：

| 表 | 说明 |
|----|------|
| `sensitive_type` | 敏感内容类型（`tenant_id=''` 为全局），行为 + `action_config`，`UNIQUE(tenant_id, code)`（未删除行） |
| `sensitive_rule` | 敏感内容规则，`type_id NOT NULL`，支持 `overrides_global_rule_id` 覆盖全局规则 |
| `policy_version` | 每租户策略版本，任何写操作同事务递增 |
| `hit_event` | 命中事件（`event_id` UUID 幂等；不含用户原文与规则明文），5 个统计索引 |
| `audit_log` | 审计日志（changes 仅存字段名 + 值哈希/长度元数据） |
| `sensitive_action` | 7 种响应行为目录（种子数据） |
| `schema_migration` | 迁移版本记录 |

删除均为逻辑删除。全局类型被任意租户未删除规则引用时删除返回 `409 type_in_use`；全局规则逻辑删除后其租户覆盖规则进入 `orphaned` 状态（不进快照，API 查询标注 `effective_status`）。

## API

所有 API 使用 `Authorization: Bearer <token>`；`tenant_id=global` 表示全局。

### 管理 API（Admin Token；操作人取网关注入的 `X-Operator`）

```
POST/GET       /api/v1/tenants/{tenant_id}/sensitive-types
GET/PUT/DELETE /api/v1/tenants/{tenant_id}/sensitive-types/{id}
POST           /api/v1/tenants/{tenant_id}/sensitive-types/{id}:enable|:disable
POST/GET       /api/v1/tenants/{tenant_id}/sensitive-rules      # keyword/type_id/enabled 筛选 + 分页
GET/PUT/DELETE /api/v1/tenants/{tenant_id}/sensitive-rules/{id}
POST           /api/v1/tenants/{tenant_id}/sensitive-rules/{id}:enable|:disable
GET            /api/v1/tenants/{tenant_id}/audit-logs           # 目标/操作/时间筛选
```

约束：租户上下文不能修改全局类型/规则（403）；规则 `type_id` 须为全局类型或本租户类型；正则规则校验语法、长度（≤512）与嵌套量词复杂度；同租户完全重复规则（规范化后相等）拒绝创建。所有写操作同事务写审计并递增策略版本。

> 部署注意：`X-Operator` 须由认证网关覆盖写入，不得透传客户端值；无网关时回退为 Admin Token 主体标识。

### 内部 API（Runtime Token）

```
GET  /internal/v1/tenants/{tenant_id}/policy-snapshot   # 组合版本 + 规范化 ETag，支持 If-None-Match → 304
POST /internal/v1/hit-events:batch                      # event_id 幂等（ON CONFLICT DO NOTHING）
```

快照组合版本为 `global-{全局版本}:tenant-{租户版本}`；ETag 为规范化序列化后快照内容的 SHA-256，与版本号解耦（内容等价即 304）。

### 统计 API（Admin Token）

```
GET /api/v1/tenants/{tenant_id}/metrics/summary     # 命中请求数（request_fingerprint 去重）+ 各最终行为次数
GET /api/v1/tenants/{tenant_id}/metrics/by-rule     # 命中次数、最近命中时间、top N
GET /api/v1/tenants/{tenant_id}/metrics/by-type
GET /api/v1/tenants/{tenant_id}/metrics/by-action   # 双维度：rule_action 命中次数 / final_action 实际请求数
GET /api/v1/tenants/{tenant_id}/metrics/trend       # granularity=hour|day|week|month
```

均支持 `from`/`to` 时间范围；`tenant_id=global` 表示跨全部租户聚合。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SENSITIVE_CONTENT_DB_URL` | （必填） | PostgreSQL URL，如 `postgresql+psycopg://user:pass@host:5432/chatai` |
| `SENSITIVE_CONTENT_ADMIN_TOKEN` | — | 管理/统计 API Bearer Token |
| `SENSITIVE_CONTENT_RUNTIME_TOKEN` | — | 内部 API Bearer Token |
| `SENSITIVE_CONTENT_FINGERPRINT_KEY` | — | HMAC 指纹密钥（检测端使用） |
| `SENSITIVE_CONTENT_RETENTION_DAYS` | `90` | 命中事件保留天数 |
| `SENSITIVE_CONTENT_CLEANUP_INTERVAL` | `86400` | 清理任务间隔（秒） |
| `SENSITIVE_CONTENT_MIGRATE_WAIT_SECONDS` | `120` | migrate 等待 DB 可用上限 |
| `SENSITIVE_CONTENT_MIGRATIONS_DIR` | 源码树 `migrations/` | 迁移 SQL 目录 |
| `SENSITIVE_CONTENT_HOST` / `SENSITIVE_CONTENT_PORT` | `0.0.0.0` / `8091` | 监听地址 |
| `SENSITIVE_CONTENT_DB_POOL_SIZE` 等 | `5/10/3600` | 连接池参数 |

## migrate 用法

```bash
# 执行全部未应用的迁移（唯一 DDL 执行方；服务启动不执行 DDL）
sensitive-content migrate

# 自定义 DB 等待上限
sensitive-content migrate --wait-seconds 60
```

行为：先等待数据库可用（退避重试，默认上限 120s，涵盖"数据库尚不存在"等连接失败），获取 PostgreSQL advisory lock（防多个 migration Job 并发），按版本号顺序执行 `migrations/NNNN_*.sql` 中未应用的文件，每个版本在 `schema_migration` 记录，重复执行幂等。生产部署由 Helm `pre-install/pre-upgrade` migration Job 调用。

### 全新安装的顺序约束（Helm）

chatai chart 中创建目标数据库（`CREATE DATABASE`）的 db-init Job 是 **post-install** hook，而本服务的 migration Job 是 **pre-install** hook——首次安装时若目标数据库尚不存在，migration Job 会重试 120s 后失败并中止安装（migrate 本身不负责建库，只在 `sensitive_content` schema 内执行 DDL）。两种处理方式任选其一：

1. 安装前手工创建目标数据库（`postgres.database`）；
2. 首次安装时设 `sensitiveContent.enabled=false`，待 db-init 建库完成后再 `helm upgrade` 开启——此时 pre-upgrade migration Job 能连上已存在的数据库正常执行。

升级场景（数据库已存在）不受影响。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

export SENSITIVE_CONTENT_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/postgres
sensitive-content migrate
SENSITIVE_CONTENT_ADMIN_TOKEN=dev-admin SENSITIVE_CONTENT_RUNTIME_TOKEN=dev-runtime sensitive-content serve

# 测试（若本机有 Docker/Podman，自动拉起临时 Postgres 跑集成测试；否则跳过 DB 用例）
pytest
```
