// 敏感内容规则（敏感词）管理：CRUD、启停、全局规则合并展示、覆盖规则、orphaned 标注（文档 4.2）
import { PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App as AntApp,
  Alert,
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
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import {
  MATCH_MODE_LABELS,
  type MatchMode,
  type RuleCreatePayload,
  type SensitiveRule,
} from '../api/types'
import ScopeTag from '../components/ScopeTag'
import { GLOBAL_TENANT, useApp } from '../context/AppContext'

/** 合并全局规则展示时的单次拉取上限（服务端 page_size 上限为 500） */
const MERGE_FETCH_SIZE = 500

interface RuleFormValues {
  type_id: number
  pattern: string
  match_mode: MatchMode
  case_sensitive: boolean
  normalize: boolean
  overrides_global_rule_id?: number | null
  description?: string
  priority: number
  remark?: string
  enabled: boolean
}

type DrawerMode =
  | { kind: 'create' }
  | { kind: 'edit'; rule: SensitiveRule }
  | { kind: 'override'; target: SensitiveRule } // 为全局规则创建租户覆盖规则

export default function Rules() {
  const { tenantId, isGlobal } = useApp()
  const queryClient = useQueryClient()
  const { message } = AntApp.useApp()

  const [keyword, setKeyword] = useState('')
  const [typeFilter, setTypeFilter] = useState<number | undefined>()
  const [enabledFilter, setEnabledFilter] = useState<boolean | undefined>()
  const [includeGlobal, setIncludeGlobal] = useState(true)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  const [drawerMode, setDrawerMode] = useState<DrawerMode | null>(null)
  const [form] = Form.useForm<RuleFormValues>()

  const merged = !isGlobal && includeGlobal

  const filterParams = { keyword: keyword || undefined, type_id: typeFilter, enabled: enabledFilter }

  // 类型列表：用于类型筛选下拉、表单选择与表格中显示类型名
  const { data: typesData } = useQuery({
    queryKey: ['types', tenantId, 'all-for-rules'],
    queryFn: () => api.listTypes(tenantId, { include_global: true, page_size: 500 }),
  })
  const typeMap = useMemo(() => {
    const map = new Map<number, string>()
    for (const t of typesData?.items ?? []) map.set(t.id, t.name)
    return map
  }, [typesData])

  // 普通模式：服务端分页，仅当前路径租户自己的规则
  const { data: ownData, isFetching: ownFetching } = useQuery({
    queryKey: ['rules', tenantId, filterParams, page, pageSize],
    queryFn: () => api.listRules(tenantId, { ...filterParams, page, page_size: pageSize }),
    enabled: !merged,
  })

  // 合并模式：规则列表接口不含全局规则（文档 4.2.2），需并行拉取全局 + 本租户两个列表在前端合并
  const { data: mergedData, isFetching: mergedFetching } = useQuery({
    queryKey: ['rules-merged', tenantId, filterParams],
    queryFn: async () => {
      const [globalRes, ownRes] = await Promise.all([
        api.listRules(GLOBAL_TENANT, { ...filterParams, page_size: MERGE_FETCH_SIZE }),
        api.listRules(tenantId, { ...filterParams, page_size: MERGE_FETCH_SIZE }),
      ])
      // 与服务端一致的排序：priority 降序 → id 升序
      const items = [...globalRes.items, ...ownRes.items].sort(
        (a, b) => b.priority - a.priority || a.id - b.id,
      )
      return { items, total: globalRes.total + ownRes.total }
    },
    enabled: merged,
  })

  const isFetching = merged ? mergedFetching : ownFetching
  const dataSource = merged
    ? (mergedData?.items ?? []).slice((page - 1) * pageSize, page * pageSize)
    : ownData?.items ?? []
  const total = merged ? mergedData?.items.length ?? 0 : ownData?.total ?? 0

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['rules'] })
    queryClient.invalidateQueries({ queryKey: ['rules-merged'] })
  }

  const saveMutation = useMutation({
    mutationFn: async (values: RuleFormValues) => {
      if (drawerMode?.kind === 'edit') {
        return api.updateRule(tenantId, drawerMode.rule.id, {
          type_id: values.type_id,
          pattern: values.pattern,
          match_mode: values.match_mode,
          case_sensitive: values.case_sensitive,
          normalize: values.normalize,
          // 编辑时允许解除覆盖关系（传 null）
          overrides_global_rule_id: isGlobal ? undefined : values.overrides_global_rule_id ?? null,
          description: values.description ?? '',
          priority: values.priority,
          remark: values.remark ?? '',
        })
      }
      const payload: RuleCreatePayload = {
        type_id: values.type_id,
        pattern: values.pattern,
        match_mode: values.match_mode,
        case_sensitive: values.case_sensitive,
        normalize: values.normalize,
        description: values.description ?? '',
        priority: values.priority,
        remark: values.remark ?? '',
        enabled: values.enabled,
      }
      // 全局上下文不能携带 overrides_global_rule_id（400 invalid_override）
      if (!isGlobal && values.overrides_global_rule_id) {
        payload.overrides_global_rule_id = values.overrides_global_rule_id
      }
      return api.createRule(tenantId, payload)
    },
    onSuccess: () => {
      message.success(drawerMode?.kind === 'edit' ? '规则已更新' : '规则已创建')
      setDrawerMode(null)
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enable }: { id: number; enable: boolean }) => api.toggleRule(tenantId, id, enable),
    onSuccess: (_, { enable }) => {
      message.success(enable ? '规则已启用' : '规则已禁用')
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteRule(tenantId, id),
    onSuccess: () => {
      message.success('规则已删除')
      invalidate()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const openCreate = () => {
    form.resetFields()
    form.setFieldsValue({ match_mode: 'text', case_sensitive: false, normalize: true, priority: 0, enabled: true })
    setDrawerMode({ kind: 'create' })
  }

  const openEdit = (rule: SensitiveRule) => {
    form.resetFields()
    form.setFieldsValue({
      type_id: rule.type_id,
      pattern: rule.pattern,
      match_mode: rule.match_mode,
      case_sensitive: rule.case_sensitive,
      normalize: rule.normalize,
      overrides_global_rule_id: rule.overrides_global_rule_id,
      description: rule.description,
      priority: rule.priority,
      remark: rule.remark,
      enabled: rule.enabled,
    })
    setDrawerMode({ kind: 'edit', rule })
  }

  // 以全局规则为模板创建覆盖规则：预填其内容与覆盖目标 ID
  const openOverride = (target: SensitiveRule) => {
    form.resetFields()
    form.setFieldsValue({
      type_id: target.type_id,
      pattern: target.pattern,
      match_mode: target.match_mode,
      case_sensitive: target.case_sensitive,
      normalize: target.normalize,
      overrides_global_rule_id: target.id,
      description: target.description,
      priority: target.priority,
      remark: '',
      enabled: true,
    })
    setDrawerMode({ kind: 'override', target })
  }

  // 租户上下文下全局规则只读（文档 2.3）
  const isReadonlyRow = (record: SensitiveRule) => !isGlobal && record.tenant_id === ''

  const columns: ColumnsType<SensitiveRule> = useMemo(
    () => [
      { title: 'ID', dataIndex: 'id', width: 70 },
      {
        title: '归属',
        dataIndex: 'tenant_id',
        width: 110,
        render: (v: string) => <ScopeTag tenantId={v} />,
      },
      {
        title: '敏感词 / 模式',
        dataIndex: 'pattern',
        // 固定最小宽度，避免被其他列挤压到不可见；超长内容省略并悬浮展示全文
        width: 260,
        render: (v: string) => (
          <Typography.Text
            code
            copyable={{ text: v }}
            ellipsis={{ tooltip: v }}
            style={{ maxWidth: 230, verticalAlign: 'middle' }}
          >
            {v}
          </Typography.Text>
        ),
      },
      {
        title: '匹配模式',
        dataIndex: 'match_mode',
        width: 100,
        render: (mode: SensitiveRule['match_mode'], r) => (
          <Tooltip
            title={
              mode === 'text'
                ? `${r.case_sensitive ? '区分大小写' : '不区分大小写'}${r.normalize ? ' / 匹配前归一化' : ''}`
                : undefined
            }
          >
            <Tag color={mode === 'regex' ? 'orange' : 'default'}>{MATCH_MODE_LABELS[mode]}</Tag>
          </Tooltip>
        ),
      },
      {
        title: '所属类型',
        dataIndex: 'type_id',
        width: 150,
        ellipsis: true,
        render: (typeId: number) => typeMap.get(typeId) ?? `类型 #${typeId}`,
      },
      {
        title: '描述',
        dataIndex: 'description',
        width: 160,
        ellipsis: true,
        render: (v: string) => v || <Typography.Text type="secondary">-</Typography.Text>,
      },
      { title: '优先级', dataIndex: 'priority', width: 85 },
      {
        title: '覆盖 / 状态',
        key: 'override',
        width: 140,
        render: (_, r) => (
          <Space size={4} wrap>
            {r.overrides_global_rule_id != null && <Tag color="blue">覆盖全局 #{r.overrides_global_rule_id}</Tag>}
            {r.effective_status === 'orphaned' && (
              <Tooltip title="覆盖的全局规则已被删除，本规则不再生效，建议删除或解除覆盖关系">
                <Tag color="red">已失效（orphaned）</Tag>
              </Tooltip>
            )}
          </Space>
        ),
      },
      {
        title: '启用',
        dataIndex: 'enabled',
        width: 80,
        render: (enabled: boolean, record) => (
          <Tooltip
            title={
              isReadonlyRow(record)
                ? '全局规则在租户上下文只读，可通过覆盖规则在本租户禁用'
                : record.overrides_global_rule_id != null && !enabled
                  ? '覆盖规则处于禁用状态 = 在本租户禁用目标全局规则'
                  : undefined
            }
          >
            <Switch
              size="small"
              checked={enabled}
              disabled={isReadonlyRow(record)}
              loading={toggleMutation.isPending && toggleMutation.variables?.id === record.id}
              onChange={(checked) => toggleMutation.mutate({ id: record.id, enable: checked })}
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
        width: 150,
        render: (_, record) => {
          if (isReadonlyRow(record)) {
            return (
              <Tooltip title="创建租户覆盖规则以替换或禁用该全局规则">
                <Button type="link" size="small" onClick={() => openOverride(record)}>
                  覆盖
                </Button>
              </Tooltip>
            )
          }
          return (
            <Space>
              <Button type="link" size="small" onClick={() => openEdit(record)}>
                编辑
              </Button>
              <Popconfirm
                title="确认删除该规则？"
                description={
                  record.tenant_id === '' ? '删除全局规则后，指向它的租户覆盖规则将变为失效状态' : undefined
                }
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => deleteMutation.mutate(record.id)}
              >
                <Button type="link" size="small" danger>
                  删除
                </Button>
              </Popconfirm>
            </Space>
          )
        },
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isGlobal, typeMap, toggleMutation.isPending, toggleMutation.variables],
  )

  const typeOptions = (typesData?.items ?? []).map((t) => ({
    value: t.id,
    label: `${t.name}（${t.tenant_id === '' ? '全局' : '租户'}）`,
  }))

  const drawerTitle =
    drawerMode?.kind === 'edit'
      ? `编辑规则 #${drawerMode.rule.id}`
      : drawerMode?.kind === 'override'
        ? `覆盖全局规则 #${drawerMode.target.id}`
        : '新建规则'

  return (
    <Card
      title="敏感内容规则"
      extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建规则
        </Button>
      }
    >
      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search
          style={{ width: 260 }}
          placeholder="搜索 pattern / 描述 / 备注"
          allowClear
          onSearch={(v) => {
            setKeyword(v.trim())
            setPage(1)
          }}
        />
        <Select
          style={{ width: 200 }}
          placeholder="按类型筛选"
          allowClear
          showSearch
          optionFilterProp="label"
          value={typeFilter}
          onChange={(v) => {
            setTypeFilter(v)
            setPage(1)
          }}
          options={typeOptions}
        />
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
            <Switch
              size="small"
              checked={includeGlobal}
              onChange={(v) => {
                setIncludeGlobal(v)
                setPage(1)
              }}
            />
            <Typography.Text>含全局规则</Typography.Text>
          </Space>
        )}
      </Space>

      <Table<SensitiveRule>
        rowKey={(r) => `${r.tenant_id}-${r.id}`}
        size="middle"
        loading={isFetching}
        columns={columns}
        dataSource={dataSource}
        scroll={{ x: 1360 }}
        rowClassName={(r) => (r.effective_status === 'orphaned' ? 'sc-row-orphaned' : '')}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => {
            setPage(p)
            setPageSize(ps)
          },
        }}
      />

      <Drawer
        title={drawerTitle}
        width={520}
        open={drawerMode !== null}
        onClose={() => setDrawerMode(null)}
        destroyOnClose
        extra={
          <Space>
            <Button onClick={() => setDrawerMode(null)}>取消</Button>
            <Button type="primary" loading={saveMutation.isPending} onClick={() => form.submit()}>
              保存
            </Button>
          </Space>
        }
      >
        {drawerMode?.kind === 'override' && (
          <Alert
            style={{ marginBottom: 16 }}
            type="info"
            showIcon
            message="覆盖规则说明"
            description="启用状态保存后将替换目标全局规则；保存为禁用状态则表示在本租户禁用该全局规则。"
          />
        )}
        <Form form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}>
          <Form.Item name="type_id" label="所属类型" rules={[{ required: true, message: '请选择所属类型' }]}>
            <Select showSearch optionFilterProp="label" options={typeOptions} placeholder="选择全局类型或本租户类型" />
          </Form.Item>
          <Form.Item
            name="pattern"
            label="敏感词 / 正则模式"
            rules={[
              {
                required: true,
                whitespace: true,
                message: 'pattern 不能为空或全空白',
              },
              { max: 512, message: 'pattern 不超过 512 字符' },
            ]}
          >
            <Input.TextArea rows={2} placeholder="文本模式填敏感词；正则模式填正则表达式（避免嵌套量词如 (a+)+）" />
          </Form.Item>
          <Form.Item name="match_mode" label="匹配模式" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'text', label: '文本包含（text）' },
                { value: 'regex', label: '正则匹配（regex）' },
              ]}
            />
          </Form.Item>
          <Space size="large">
            <Form.Item name="case_sensitive" label="区分大小写" valuePropName="checked" tooltip="仅文本模式生效">
              <Switch />
            </Form.Item>
            <Form.Item
              name="normalize"
              label="匹配前归一化"
              valuePropName="checked"
              tooltip="NFKC、去零宽字符、空白折叠；仅文本模式生效"
            >
              <Switch />
            </Form.Item>
          </Space>
          {!isGlobal && (
            <Form.Item
              name="overrides_global_rule_id"
              label="覆盖的全局规则 ID"
              tooltip={
                drawerMode?.kind === 'edit'
                  ? '清空可解除覆盖关系'
                  : '仅租户规则可设置；目标必须是存在且未删除的全局规则'
              }
            >
              <InputNumber
                style={{ width: '100%' }}
                min={1}
                disabled={drawerMode?.kind === 'override'}
                placeholder="留空表示普通规则"
              />
            </Form.Item>
          )}
          <Form.Item name="priority" label="优先级" tooltip="数值越大越靠前">
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input placeholder="选填" />
          </Form.Item>
          <Form.Item name="remark" label="备注">
            <Input placeholder="选填" />
          </Form.Item>
          {drawerMode?.kind !== 'edit' && (
            <Form.Item
              name="enabled"
              label="创建后立即启用"
              valuePropName="checked"
              tooltip={drawerMode?.kind === 'override' ? '关闭 = 在本租户禁用目标全局规则' : undefined}
            >
              <Switch />
            </Form.Item>
          )}
        </Form>
      </Drawer>
    </Card>
  )
}
