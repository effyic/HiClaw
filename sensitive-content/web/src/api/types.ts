// 与 docs/frontend-api.md 第 2.7 / 3.x 节保持一致的类型与枚举定义

/** 响应行为枚举（8 种） */
export const ACTIONS = [
  'END_CONVERSATION',
  'FIXED_REPLY',
  'BLOCK_REQUEST',
  'REDACT_AND_CONTINUE',
  'LOG_ONLY',
  'BUSINESS_ACTION',
  'CUSTOM_RESPONSE',
  'ADJUST_PROMPT',
] as const

export type Action = (typeof ACTIONS)[number]

/** 行为枚举中文名 */
export const ACTION_LABELS: Record<Action, string> = {
  END_CONVERSATION: '结束对话',
  FIXED_REPLY: '固定回复',
  BLOCK_REQUEST: '阻断请求',
  REDACT_AND_CONTINUE: '脱敏放行',
  LOG_ONLY: '仅记录',
  BUSINESS_ACTION: '业务动作',
  CUSTOM_RESPONSE: '自定义响应',
  ADJUST_PROMPT: '语气调整',
}

/** action_config 可配置 key 联合类型 */
export type ActionConfigKey =
  | 'reply_text'
  | 'replacement'
  | 'business_action'
  | 'prompt_guidance'

/** 各行为对应的 action_config 可配置 key（白名单见文档 2.7） */
export const ACTION_CONFIG_KEYS: Record<Action, ActionConfigKey[]> = {
  END_CONVERSATION: ['reply_text'],
  FIXED_REPLY: ['reply_text'],
  BLOCK_REQUEST: [],
  REDACT_AND_CONTINUE: ['replacement'],
  LOG_ONLY: [],
  BUSINESS_ACTION: ['business_action'],
  CUSTOM_RESPONSE: ['reply_text'],
  ADJUST_PROMPT: ['prompt_guidance'],
}

export type MatchMode = 'text' | 'regex'

export const MATCH_MODE_LABELS: Record<MatchMode, string> = {
  text: '文本包含',
  regex: '正则匹配',
}

export type EffectiveStatus = 'active' | 'orphaned'

export type AuditAction = 'create' | 'update' | 'delete' | 'enable' | 'disable'

export const AUDIT_ACTION_LABELS: Record<AuditAction, string> = {
  create: '创建',
  update: '更新',
  delete: '删除',
  enable: '启用',
  disable: '禁用',
}

export type Granularity = 'hour' | 'day' | 'week' | 'month'

/** 分页列表响应统一形态 */
export interface Paged<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

/** 敏感内容类型对象（文档 3.1） */
export interface SensitiveType {
  id: number
  tenant_id: string
  code: string
  name: string
  action: Action
  action_config: Record<string, string>
  priority: number
  description: string
  enabled: boolean
  deleted: boolean
  created_at: string
  updated_at: string
}

/** 敏感内容规则对象（文档 3.2） */
export interface SensitiveRule {
  id: number
  tenant_id: string
  type_id: number
  pattern: string
  match_mode: MatchMode
  case_sensitive: boolean
  normalize: boolean
  overrides_global_rule_id: number | null
  description: string
  priority: number
  remark: string
  enabled: boolean
  deleted: boolean
  created_at: string
  updated_at: string
  effective_status: EffectiveStatus
}

/** 命中事件对象（文档 3.3） */
export interface HitEvent {
  event_id: string
  rule_id: number
  type_id: number
  rule_action: Action
  final_action: Action
  selected: boolean
  final_rule_id: number
  tenant_id: string
  session_id: string
  policy_version: string
  hit_count: number
  hit_at: string
}

/** 审计日志对象（文档 3.4） */
export interface AuditLog {
  id: number
  tenant_id: string
  action: AuditAction
  target_kind: 'rule' | 'type'
  target_id: number
  changes: Record<string, unknown>
  operator: string
  created_at: string
}

// ---- 统计 API 响应 ----

export interface MetricsSummary {
  total_events: number
  total_hits: number
  hit_requests: number
  final_actions: Record<string, number>
}

export interface ByRuleItem {
  rule_id: number
  type_id: number
  events: number
  hits: number
  last_hit_at: string | null
  last_session_id: string | null
}

export interface ByTypeItem {
  type_id: number
  events: number
  hits: number
  last_hit_at: string | null
}

export interface ByActionResponse {
  by_rule_action: Array<{ rule_action: string; events: number; hits: number }>
  by_final_action: Array<{ final_action: string; requests: number }>
}

export interface TrendItem {
  bucket: string
  events: number
  hits: number
  requests: number
}

// ---- 请求体 ----

export interface TypeCreatePayload {
  /** 可选；缺省由服务端自动生成 */
  code?: string
  name: string
  action: Action
  action_config?: Record<string, string>
  priority?: number
  description?: string
  enabled?: boolean
}

export type TypeUpdatePayload = Partial<Pick<TypeCreatePayload, 'name' | 'action' | 'action_config' | 'priority' | 'description'>>

export interface RuleCreatePayload {
  type_id: number
  pattern: string
  match_mode?: MatchMode
  case_sensitive?: boolean
  normalize?: boolean
  overrides_global_rule_id?: number | null
  description?: string
  priority?: number
  remark?: string
  enabled?: boolean
}

export type RuleUpdatePayload = Partial<Omit<RuleCreatePayload, 'enabled'>>
