// 全局应用状态：当前租户上下文 + Token 设置对话框开关（localStorage 持久化）
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

const TENANT_KEY = 'sc-tenant-id'
const TENANT_HISTORY_KEY = 'sc-tenant-history'

/** 全局上下文的特殊租户 ID（文档 2.3） */
export const GLOBAL_TENANT = 'global'

interface AppContextValue {
  /** 当前租户 ID；'global' 表示全局上下文 */
  tenantId: string
  /** 是否处于全局上下文 */
  isGlobal: boolean
  setTenantId: (id: string) => void
  /** 最近使用过的租户 ID 列表（用于切换器候选） */
  tenantHistory: string[]
  tokenModalOpen: boolean
  openTokenModal: () => void
  closeTokenModal: () => void
}

const AppContext = createContext<AppContextValue | null>(null)

function loadHistory(): string[] {
  try {
    const raw = localStorage.getItem(TENANT_HISTORY_KEY)
    const list = raw ? (JSON.parse(raw) as string[]) : []
    return Array.isArray(list) ? list : []
  } catch {
    return []
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [tenantId, setTenantIdState] = useState<string>(
    () => localStorage.getItem(TENANT_KEY) || GLOBAL_TENANT,
  )
  const [tenantHistory, setTenantHistory] = useState<string[]>(loadHistory)
  const [tokenModalOpen, setTokenModalOpen] = useState(false)

  const setTenantId = useCallback((id: string) => {
    const trimmed = id.trim()
    if (!trimmed) return
    setTenantIdState(trimmed)
    localStorage.setItem(TENANT_KEY, trimmed)
    if (trimmed !== GLOBAL_TENANT) {
      setTenantHistory((prev) => {
        const next = [trimmed, ...prev.filter((t) => t !== trimmed)].slice(0, 10)
        localStorage.setItem(TENANT_HISTORY_KEY, JSON.stringify(next))
        return next
      })
    }
  }, [])

  const value = useMemo<AppContextValue>(
    () => ({
      tenantId,
      isGlobal: tenantId === GLOBAL_TENANT,
      setTenantId,
      tenantHistory,
      tokenModalOpen,
      openTokenModal: () => setTokenModalOpen(true),
      closeTokenModal: () => setTokenModalOpen(false),
    }),
    [tenantId, setTenantId, tenantHistory, tokenModalOpen],
  )

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useApp 必须在 AppProvider 内使用')
  return ctx
}
