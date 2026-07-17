# 敏感词管理后台（sensitive-content console）

`sensitive-content` 服务的管理后台前端，对接 [docs/frontend-api.md](../docs/frontend-api.md) 定义的管理与统计 API。

技术栈：React 18 + Vite + TypeScript + Ant Design 5 + @ant-design/plots + TanStack Query。

## 功能

- **统计看板**：命中概览、时间趋势（hour/day/week/month，缺失时间桶自动补零）、按规则/最终行为分布、Top 10 命中规则/类型（自动关联规则 pattern 与类型名称）
- **类型管理**：敏感内容类型的增删改查、启停；按响应行为动态展示 `action_config` 配置项；禁用前提示影响范围
- **规则管理**：敏感词/正则规则的增删改查、启停；租户视图可合并展示全局规则；全局规则提供「覆盖」入口（替换或在本租户禁用）；`orphaned` 失效规则醒目标注
- **命中事件**：按规则/类型/会话/时间筛选，`session_id` 可跳转会话详情；不含用户消息原文
- **审计日志**：按目标/操作/时间筛选，展示变更字段名（不含明文）

顶栏支持租户上下文切换（`global` 为全局视角，可直接输入任意租户 ID 回车切换）；租户视图下全局资源自动只读。

## 本地开发

```bash
npm install
npm run dev
```

默认在 `http://localhost:5180` 启动，`/api`、`/healthz` 会代理到本地 `http://localhost:8091`（可用环境变量 `SC_DEV_API_TARGET` 覆盖代理目标）。

首次进入后点击右上角「设置 Token」，填入服务端的 `SENSITIVE_CONTENT_ADMIN_TOKEN`（保存在浏览器 localStorage）。

## 构建与部署

```bash
npm run build
```

产物输出到 `dist/`，为纯静态文件（路由使用 hash 模式，无需服务器 rewrite 配置），可由 Nginx/网关直接托管。

### 生产 API 地址

默认请求同源路径 `/api/v1/...`。若前端与 API 不同源，构建时通过环境变量指定网关 Base URL：

```bash
VITE_API_BASE_URL=https://gateway.example.com npm run build
```

> 生产环境操作人标识（`X-Operator`）由认证网关注入，前端不设置该请求头。
