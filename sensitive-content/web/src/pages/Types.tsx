// 敏感内容类型管理：列表、创建/编辑、启停、删除（文档 4.1）
import { PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App as AntApp,
  Button,
  Card,
  Drawer,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import {
  ACTIONS,
  ACTION_CONFIG_KEYS,
  ACTION_LABELS,
  type Action,
  type SensitiveType,
  type TypeCreatePayload,
} from '../api/types'
import ActionTag from '../components/ActionTag'
import ScopeTag from '../components/ScopeTag'
import { useApp } from '../context/AppContext'

const ACTION_OPTIONS = ACTIONS.map((a) => ({ value: a, label: `${ACTION_LABELS[a]}（${a}）` }))

const CONFIG_FIELD_META = {
  reply_text: { label: '回复文案', placeholder: '命中后返回给用户的文案' },
  replacement: { label: '脱敏替换符', placeholder: '如 ***' },
  business_action: { label: '业务动作处理器', placeholder: '检测端预注册的白名单处理器名称' },
  prompt_guidance: {
    label: '语气指引（prompt_guidance）',
    placeholder:
      '注入本轮对话的语气/回应要求；勿在文案中要求向用户暴露内部检测或命中规则',
  },
} as const

interface TypeFormValues {
  name: string
  action: Action
  reply_text?: string
  replacement?: string
  business_action?: string
  prompt_guidance?: string
  priority: number
  description?: string
  enabled: boolean
}

/** 根据所选行为组装 action_config（切换 action 时只保留该行为允许的 key） */
function buildActionConfig(values: TypeFormValues): Record<string, string> {
  const config: Record<string, string> = {}
  for (const key of ACTION_CONFIG_KEYS[values.action]) {
    const v = values[key]
    if (v !== undefined && v !== '') config[key] = v
  }
  return config
}

export default function Types() {
  const { tenantId, isGlobal } = useApp()
  const queryClient = useQueryClient()
  const { message, modal } = AntApp.useApp()

  const [enabledFilter, setEnabledFilter] = useState<boolean | undefined>()
  const [includeGlobal, setIncludeGlobal] = useState(true)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState<SensitiveType | null>(null)
  const [form] = Form.useForm<TypeFormValues>()
  const selectedAction = Form.useWatch('action', form)

  const listParams = {
    enabled: enabledFilter,
    include_global: isGlobal ? undefined : includeGlobal,
    page,
    page_size: pageSize,
  }

  const { data, isFetching } = useQuery({
    queryKey: ['types', tenantId, listParams],
    queryFn: () => api.listTypes(tenantId, listParams),
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['types', tenantId] })

  const saveMutation = useMutation({
    mutationFn: async (values: TypeFormValues) => {
      const actionConfig = buildActionConfig(values)
      if (editing) {
        return api.updateType(tenantId, editing.id, {
          name: values.name,
          action: values.action,
          action_config: actionConfig,
          priority: values.priority,
          description: values.description ?? '',
        })
      }
      // code 由服务端自动生成，前端不再传
      const payload: TypeCreatePayload = {
        name: values.name,
        action: values.action,
        action_config: actionConfig,
        priority: values.priority,
        description: values.description ?? '',
        enabled: values.enabled,
      }
      return api.createType(tenantId, payload)
    },
    onSuccess: () => {
      message.success(editing ? '类型已更新' : '类型已创建')
      setDrawerOpen(false)
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enable }: { id: number; enable: boolean }) => api.toggleType(tenantId, id, enable),
    onSuccess: (_, { enable }) => {
      message.success(enable ? '类型已启用' : '类型已禁用')
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteType(tenantId, id),
    onSuccess: () => {
      message.success('类型已删除')
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ action: 'LOG_ONLY', priority: 0, enabled: true })
    setDrawerOpen(true)
  }

  const openEdit = (record: SensitiveType) => {
    setEditing(record)
    form.resetFields()
    form.setFieldsValue({
      name: record.name,
      action: record.action,
      priority: record.priority,
      description: record.description,
      enabled: record.enabled,
      reply_text: record.action_config.reply_text,
      replacement: record.action_config.replacement,
      business_action: record.action_config.business_action,
      prompt_guidance: record.action_config.prompt_guidance,
    })
    setDrawerOpen(true)
  }

  const confirmToggle = (record: SensitiveType, enable: boolean) => {
    if (enable) {
      toggleMutation.mutate({ id: record.id, enable })
      return
    }
    // 禁用类型影响其下所有规则，需二次确认（文档 4.1.5）
    modal.confirm({
      title: `确认禁用类型「${record.name}」？`,
      content: '禁用后，该类型下的所有规则（含租户规则）在检测端将立即失效。',
      okText: '禁用',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => toggleMutation.mutate({ id: record.id, enable: false }),
    })
  }

  // 租户上下文下全局行只读（文档 2.3）
  const isReadonlyRow = (record: SensitiveType) => !isGlobal && record.tenant_id === ''

  const columns: ColumnsType<SensitiveType> = useMemo(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '归属',
        dataIndex: 'tenant_id',
        width: 110,
        render: (v: string) => <ScopeTag tenantId={v} />,
      },
      { title: '名称', dataIndex: 'name', width: 160 },
      {
        title: '响应行为',
        dataIndex: 'action',
        width: 130,
        render: (v: string) => <ActionTag action={v} />,
      },
      {
        title: '行为配置',
        dataIndex: 'action_config',
        ellipsis: true,
        render: (v: Record<string, string>) =>
          Object.keys(v).length ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {Object.entries(v)
                .map(([k, val]) => `${k}=${val}`)
                .join('；')}
            </Typography.Text>
          ) : (
            '-'
          ),
      },
      { title: '优先级', dataIndex: 'priority', width: 90 },
      { title: '描述', dataIndex: 'description', ellipsis: true },
      {
        title: '启用',
        dataIndex: 'enabled',
        width: 80,
        render: (enabled: boolean, record) => (
          <Tooltip title={isReadonlyRow(record) ? '全局类型在租户上下文只读' : undefined}>
            <Switch
              size="small"
              checked={enabled}
              disabled={isReadonlyRow(record)}
              loading={toggleMutation.isPending && toggleMutation.variables?.id === record.id}
              onChange={(checked) => confirmToggle(record, checked)}
            />
          </Tooltip>
        ),
      },
      {
        title: '更新时间',
        dataIndex: 'updated_at',
        width: 165,
        render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
      },
      {
        title: '操作',
        key: 'actions',
        width: 130,
        render: (_, record) => {
          const readonly = isReadonlyRow(record)
          return (
            <Space>
              <Tooltip title={readonly ? '全局类型在租户上下文只读' : undefined}>
                <Button type="link" size="small" disabled={readonly} onClick={() => openEdit(record)}>
                  编辑
                </Button>
              </Tooltip>
              <Popconfirm
                title="确认删除该类型？"
                description="仍被规则引用的类型无法删除"
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                disabled={readonly}
                onConfirm={() => deleteMutation.mutate(record.id)}
              >
                <Button type="link" size="small" danger disabled={readonly}>
                  删除
                </Button>
              </Popconfirm>
            </Space>
          )
        },
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isGlobal, toggleMutation.isPending, toggleMutation.variables],
  )

  const configKeys = selectedAction ? ACTION_CONFIG_KEYS[selectedAction] : []

  return (
    <Card
      title="敏感内容类型"
      extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建类型
        </Button>
      }
    >
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          style={{ width: 140 }}
          placeholder="启用状态"
          allowClear
          value={enabledFilter}
          onChange={(v) => {
            setEnabledFilter(v)
            setPage(1)
          }}
          options={[
            { value: true, label: '仅启用' },
            { value: false, label: '仅禁用' },
          ]}
        />
        {!isGlobal && (
          <Space>
            <Switch size="small" checked={includeGlobal} onChange={setIncludeGlobal} />
            <Typography.Text>含全局类型</Typography.Text>
          </Space>
        )}
      </Space>

      <Table<SensitiveType>
        rowKey="id"
        size="middle"
        loading={isFetching}
        columns={columns}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => {
            setPage(p)
            setPageSize(ps)
          },
        }}
      />

      <Drawer
        title={editing ? `编辑类型 #${editing.id}` : '新建类型'}
        width={480}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose
        extra={
          <Space>
            <Button onClick={() => setDrawerOpen(false)}>取消</Button>
            <Button type="primary" loading={saveMutation.isPending} onClick={() => form.submit()}>
              保存
            </Button>
          </Space>
        }
      >
        <Form form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}>
          <Form.Item
            name="name"
            label="展示名称"
            rules={[
              { required: true, message: '请输入名称' },
              { max: 128, message: '不超过 128 字符' },
            ]}
          >
            <Input placeholder="如 内部机密" />
          </Form.Item>
          <Form.Item name="action" label="响应行为" rules={[{ required: true, message: '请选择响应行为' }]}>
            <Select options={ACTION_OPTIONS} />
          </Form.Item>
          {configKeys.map((key) => (
            <Form.Item
              key={key}
              name={key}
              label={CONFIG_FIELD_META[key].label}
              rules={
                key === 'prompt_guidance'
                  ? [
                      { required: true, message: '请填写语气指引' },
                      { max: 2000, message: '不超过 2000 字符' },
                    ]
                  : undefined
              }
            >
              {key === 'prompt_guidance' ? (
                <Input.TextArea
                  rows={6}
                  showCount
                  maxLength={2000}
                  placeholder={CONFIG_FIELD_META[key].placeholder}
                />
              ) : (
                <Input placeholder={CONFIG_FIELD_META[key].placeholder} />
              )}
            </Form.Item>
          ))}
          <Form.Item name="priority" label="优先级" tooltip="数值越大越靠前">
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} />
          </Form.Item>
          {!editing && (
            <Form.Item name="enabled" label="创建后立即启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          )}
        </Form>
      </Drawer>
    </Card>
  )
}
