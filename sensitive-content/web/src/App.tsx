// 应用骨架：路由、侧边导航、租户切换器、Token 设置入口
import {
  AuditOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  KeyOutlined,
  TagsOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, Button, Layout, Menu, Select, Space, Tooltip, Typography } from 'antd'
import { useEffect, useMemo, useRef } from 'react'
import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, UNAUTHORIZED_EVENT } from './api/client'
import TokenModal from './components/TokenModal'
import { AppProvider, GLOBAL_TENANT, useApp } from './context/AppContext'
import AuditLogs from './pages/AuditLogs'
import Dashboard from './pages/Dashboard'
import HitEvents from './pages/HitEvents'
import Rules from './pages/Rules'
import Types from './pages/Types'

const MENU_ITEMS = [
  { key: '/dashboard', icon: <DashboardOutlined />, label: '统计看板' },
  { key: '/types', icon: <TagsOutlined />, label: '类型管理' },
  { key: '/rules', icon: <UnorderedListOutlined />, label: '规则管理' },
  { key: '/hit-events', icon: <FileSearchOutlined />, label: '命中事件' },
  { key: '/audit-logs', icon: <AuditOutlined />, label: '审计日志' },
]

function TenantSwitcher() {
  const { tenantId, setTenantId, tenantHistory } = useApp()

  const options = useMemo(() => {
    const list = [{ value: GLOBAL_TENANT, label: '全局（global）' }]
    for (const t of tenantHistory) {
      if (t !== GLOBAL_TENANT) list.push({ value: t, label: t })
    }
    return list
  }, [tenantHistory])

  return (
    <Space>
      <Typography.Text>租户：</Typography.Text>
      <Tooltip title="选择 global 表示全局（平台管理员视角）；输入任意租户 ID 后回车即可切换">
        <Select
          showSearch
          style={{ width: 220 }}
          value={tenantId}
          options={options}
          onChange={setTenantId}
          onInputKeyDown={(e) => {
            // 支持直接输入新租户 ID 回车切换
            if (e.key === 'Enter') {
              const input = (e.target as HTMLInputElement).value.trim()
              if (input) setTenantId(input)
            }
          }}
          filterOption={(input, option) =>
            (option?.value ?? '').toLowerCase().includes(input.toLowerCase())
          }
        />
      </Tooltip>
    </Space>
  )
}

function Shell() {
  const location = useLocation()
  const navigate = useNavigate()
  const { openTokenModal, tenantId, isGlobal } = useApp()

  // 收到 401 时自动弹出 Token 设置对话框
  useEffect(() => {
    const handler = () => openTokenModal()
    window.addEventListener(UNAUTHORIZED_EVENT, handler)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler)
  }, [openTokenModal])

  const selectedKey = MENU_ITEMS.find((it) => location.pathname.startsWith(it.key))?.key ?? '/dashboard'

  return (
    // 固定整体高度，仅内容区内部滚动，侧边栏与顶栏始终可见
    <Layout style={{ height: '100vh', overflow: 'hidden' }}>
      <Layout.Sider theme="dark" width={200} style={{ overflowY: 'auto' }}>
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            fontSize: 15,
            fontWeight: 600,
          }}
        >
          敏感词管理后台
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key)}
        />
      </Layout.Sider>
      <Layout>
        <Layout.Header
          style={{
            background: '#fff',
            padding: '0 24px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: '1px solid #f0f0f0',
          }}
        >
          <Space size="large">
            <TenantSwitcher />
            <Typography.Text type="secondary">
              {isGlobal ? '当前为全局上下文，可管理平台级类型/规则' : `当前租户：${tenantId}（全局资源只读）`}
            </Typography.Text>
          </Space>
          <Button icon={<KeyOutlined />} onClick={openTokenModal}>
            设置 Token
          </Button>
        </Layout.Header>
        <Layout.Content style={{ padding: 24, overflow: 'auto' }}>
          <Routes>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/types" element={<Types />} />
            <Route path="/rules" element={<Rules />} />
            <Route path="/hit-events" element={<HitEvents />} />
            <Route path="/audit-logs" element={<AuditLogs />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </Layout.Content>
      </Layout>
      <TokenModal />
    </Layout>
  )
}

export default function App() {
  const { message } = AntApp.useApp()
  const lastErrorRef = useRef<{ msg: string; at: number }>({ msg: '', at: 0 })

  const queryClient = useMemo(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: (failureCount, error) => {
              // 4xx 业务/鉴权错误不重试
              if (error instanceof ApiError && error.status < 500) return false
              return failureCount < 2
            },
            staleTime: 15_000,
          },
        },
        queryCache: new QueryCache({
          onError: (error) => {
            // 401 由 Token 对话框统一处理；其余错误提示一次（短时间去重）
            if (error instanceof ApiError && error.status === 401) return
            const msg = error.message || '请求失败'
            const now = Date.now()
            if (lastErrorRef.current.msg === msg && now - lastErrorRef.current.at < 3000) return
            lastErrorRef.current = { msg, at: now }
            message.error(msg)
          },
        }),
      }),
    [message],
  )

  return (
    <QueryClientProvider client={queryClient}>
      <AppProvider>
        <HashRouter>
          <Shell />
        </HashRouter>
      </AppProvider>
    </QueryClientProvider>
  )
}
