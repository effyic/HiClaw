// API 请求封装：Bearer Token 注入、三种错误体统一解析（文档 2.2 / 2.6）
import type {
  AgentRuleBindingResult,
  AgentRuleBindings,
  AuditLog,
  ByActionResponse,
  ByRuleItem,
  ByTypeItem,
  Granularity,
  HitEvent,
  MetricsSummary,
  Paged,
  RuleCreatePayload,
  RuleUpdatePayload,
  SensitiveRule,
  SensitiveType,
  TrendItem,
  TypeCreatePayload,
  TypeUpdatePayload,
} from './types'

const TOKEN_KEY = 'sc-admin-token'

/** 生产环境可通过 VITE_API_BASE_URL 指定网关地址；默认同源（开发期走 Vite proxy） */
const BASE_URL: string = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? ''

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}

/** 401 时广播事件，由布局层弹出 Token 设置对话框 */
export const UNAUTHORIZED_EVENT = 'sc:unauthorized'

export class ApiError extends Error {
  code: string
  status: number
  extra: Record<string, unknown>

  constructor(status: number, code: string, message: string, extra: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.extra = extra
  }
}

/** 业务错误码 → 中文提示（文档 2.6 错误码总表） */
const CODE_MESSAGES: Record<string, string> = {
  empty_pattern: 'pattern 不能为空',
  pattern_too_long: 'pattern 超过 512 字符上限',
  invalid_regex: '正则语法错误',
  regex_too_complex: '正则包含嵌套量词，存在灾难性回溯风险，请简化',
  invalid_action_config: 'action_config 含不允许的配置项',
  invalid_override: '全局上下文不能创建覆盖规则',
  invalid_override_target: '覆盖目标必须是存在且未删除的全局规则，请重新选择',
  invalid_granularity: '时间粒度参数非法',
  unauthorized: '管理 Token 无效，请重新设置',
  forbidden_global_type: '租户上下文不能修改全局类型',
  forbidden_global_rule: '租户上下文不能修改全局规则，请创建覆盖规则',
  type_not_found: '类型不存在或无权访问',
  rule_not_found: '规则不存在或无权访问',
  agent_not_found: 'Agent 不存在或不属于当前租户',
  invalid_agent_scope: '全局上下文不能配置 Agent 规则绑定',
  rule_not_assignable: '所选规则当前不可绑定，请检查启用状态与覆盖关系',
  duplicate_code: '类型编码冲突，请重试',
  duplicate_rule: '已存在等价规则（规范化后重复）',
  type_in_use: '该类型仍被规则引用，无法删除',
  token_not_configured: '服务端未配置管理 Token，请联系运维',
}

interface ValidationItem {
  loc: Array<string | number>
  msg: string
  type: string
}

/** 按文档 2.6 解析三种错误体形态，统一抛出 ApiError */
async function parseError(resp: Response): Promise<ApiError> {
  let body: unknown
  try {
    body = await resp.json()
  } catch {
    return new ApiError(resp.status, 'unknown', `请求失败（HTTP ${resp.status}）`)
  }

  const obj = body as Record<string, unknown>

  // 业务错误：{"error": {"code", "message", ...扩展字段}}
  if (obj.error && typeof obj.error === 'object') {
    const err = obj.error as Record<string, unknown>
    const code = String(err.code ?? 'unknown')
    const serverMsg = String(err.message ?? '')
    const { code: _c, message: _m, ...extra } = err
    let msg = CODE_MESSAGES[code] ?? serverMsg ?? `请求失败（${code}）`
    // 正则类错误与重复规则的 message 含具体原因，直接展示
    if (['invalid_regex', 'regex_too_complex', 'duplicate_rule'].includes(code) && serverMsg) {
      msg = `${CODE_MESSAGES[code]}：${serverMsg}`
    }
    if (code === 'type_in_use' && typeof extra.references === 'number') {
      msg = `仍有 ${extra.references} 条规则引用该类型，请先处理关联规则`
    }
    return new ApiError(resp.status, code, msg, extra)
  }

  // 鉴权错误：{"detail": {"code", "message"}}
  if (obj.detail && typeof obj.detail === 'object' && !Array.isArray(obj.detail)) {
    const detail = obj.detail as Record<string, unknown>
    const code = String(detail.code ?? 'unknown')
    return new ApiError(resp.status, code, CODE_MESSAGES[code] ?? String(detail.message ?? ''))
  }

  // 参数校验错误：{"detail": [{loc, msg, type}]}
  if (Array.isArray(obj.detail)) {
    const items = obj.detail as ValidationItem[]
    const msg = items
      .map((it) => `${it.loc.filter((p) => p !== 'body' && p !== 'query').join('.')}: ${it.msg}`)
      .join('；')
    return new ApiError(resp.status, 'validation_error', `参数校验失败：${msg}`)
  }

  return new ApiError(resp.status, 'unknown', `请求失败（HTTP ${resp.status}）`)
}

type QueryParams = Record<string, string | number | boolean | undefined | null>

function buildQuery(params?: QueryParams): string {
  if (!params) return ''
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    sp.set(key, String(value))
  }
  const qs = sp.toString()
  return qs ? `?${qs}` : ''
}

async function request<T>(
  method: string,
  path: string,
  opts: { params?: QueryParams; body?: unknown } = {},
): Promise<T> {
  const tokenUsed = getToken()
  const headers: Record<string, string> = {
    Authorization: `Bearer ${tokenUsed}`,
  }
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'

  const resp = await fetch(`${BASE_URL}${path}${buildQuery(opts.params)}`, {
    method,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })

  if (!resp.ok) {
    const err = await parseError(resp)
    // 仅当 401 对应的 Token 仍是当前 Token 时才弹窗：
    // 避免"保存新 Token 后，旧 Token 发出的在途请求返回 401"把对话框重新弹出来
    if (err.status === 401 && tokenUsed === getToken()) {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
    }
    throw err
  }

  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

const tenantBase = (tenantId: string) => `/api/v1/tenants/${encodeURIComponent(tenantId)}`

export interface ListParams extends Record<string, string | number | boolean | undefined | null> {
  page?: number
  page_size?: number
}

export const api = {
  // ---- 敏感内容类型（文档 4.1） ----
  listTypes: (tenantId: string, params?: ListParams & { enabled?: boolean; include_global?: boolean }) =>
    request<Paged<SensitiveType>>('GET', `${tenantBase(tenantId)}/sensitive-types`, { params }),
  createType: (tenantId: string, body: TypeCreatePayload) =>
    request<SensitiveType>('POST', `${tenantBase(tenantId)}/sensitive-types`, { body }),
  updateType: (tenantId: string, typeId: number, body: TypeUpdatePayload) =>
    request<SensitiveType>('PUT', `${tenantBase(tenantId)}/sensitive-types/${typeId}`, { body }),
  toggleType: (tenantId: string, typeId: number, enable: boolean) =>
    request<SensitiveType>('POST', `${tenantBase(tenantId)}/sensitive-types/${typeId}:${enable ? 'enable' : 'disable'}`),
  deleteType: (tenantId: string, typeId: number) =>
    request<void>('DELETE', `${tenantBase(tenantId)}/sensitive-types/${typeId}`),

  // ---- 敏感内容规则（文档 4.2） ----
  listRules: (tenantId: string, params?: ListParams & { keyword?: string; type_id?: number; enabled?: boolean }) =>
    request<Paged<SensitiveRule>>('GET', `${tenantBase(tenantId)}/sensitive-rules`, { params }),
  createRule: (tenantId: string, body: RuleCreatePayload) =>
    request<SensitiveRule>('POST', `${tenantBase(tenantId)}/sensitive-rules`, { body }),
  updateRule: (tenantId: string, ruleId: number, body: RuleUpdatePayload) =>
    request<SensitiveRule>('PUT', `${tenantBase(tenantId)}/sensitive-rules/${ruleId}`, { body }),
  toggleRule: (tenantId: string, ruleId: number, enable: boolean) =>
    request<SensitiveRule>('POST', `${tenantBase(tenantId)}/sensitive-rules/${ruleId}:${enable ? 'enable' : 'disable'}`),
  deleteRule: (tenantId: string, ruleId: number) =>
    request<void>('DELETE', `${tenantBase(tenantId)}/sensitive-rules/${ruleId}`),

  // ---- Agent 规则绑定（供 Agent 管理端复用） ----
  getAgentRuleBindings: (
    tenantId: string,
    roleCode: string,
    params?: ListParams & { keyword?: string; type_id?: number },
  ) => request<AgentRuleBindings>('GET', `${tenantBase(tenantId)}/agents/${encodeURIComponent(roleCode)}/sensitive-rules`, { params }),
  replaceAgentRuleBindings: (tenantId: string, roleCode: string, ruleIds: number[]) =>
    request<AgentRuleBindingResult>('PUT', `${tenantBase(tenantId)}/agents/${encodeURIComponent(roleCode)}/sensitive-rules`, {
      body: { rule_ids: ruleIds },
    }),
  clearAgentRuleBindings: (tenantId: string, roleCode: string) =>
    request<void>('DELETE', `${tenantBase(tenantId)}/agents/${encodeURIComponent(roleCode)}/sensitive-rules`),

  // ---- 命中事件（文档 4.3） ----
  listHitEvents: (
    tenantId: string,
    params?: ListParams & { rule_id?: number; type_id?: number; role_code?: string; session_id?: string; from?: string; to?: string },
  ) => request<Paged<HitEvent>>('GET', `${tenantBase(tenantId)}/hit-events`, { params }),

  // ---- 审计日志（文档 4.4） ----
  listAuditLogs: (
    tenantId: string,
    params?: ListParams & { target_kind?: string; target_id?: number; action?: string; from?: string; to?: string },
  ) => request<Paged<AuditLog>>('GET', `${tenantBase(tenantId)}/audit-logs`, { params }),

  // ---- 统计（文档 5） ----
  metricsSummary: (tenantId: string, params?: { from?: string; to?: string }) =>
    request<MetricsSummary>('GET', `${tenantBase(tenantId)}/metrics/summary`, { params }),
  metricsByRule: (tenantId: string, params?: { from?: string; to?: string; top?: number }) =>
    request<{ items: ByRuleItem[] }>('GET', `${tenantBase(tenantId)}/metrics/by-rule`, { params }),
  metricsByType: (tenantId: string, params?: { from?: string; to?: string; top?: number }) =>
    request<{ items: ByTypeItem[] }>('GET', `${tenantBase(tenantId)}/metrics/by-type`, { params }),
  metricsByAction: (tenantId: string, params?: { from?: string; to?: string }) =>
    request<ByActionResponse>('GET', `${tenantBase(tenantId)}/metrics/by-action`, { params }),
  metricsTrend: (tenantId: string, params?: { from?: string; to?: string; granularity?: Granularity }) =>
    request<{ items: TrendItem[] }>('GET', `${tenantBase(tenantId)}/metrics/trend`, { params }),
}

/** 会话详情跳转地址（文档 4.3 / 7.7） */
export function sessionUrl(sessionId: string): string {
  return `/effyic/v1/sessions/${encodeURIComponent(sessionId)}`
}
