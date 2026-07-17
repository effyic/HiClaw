// 审计日志：查询路径租户自身的写操作记录；changes 只展示变更字段名（文档 4.4）
import { useQuery } from '@tanstack/react-query'
import { Card, DatePicker, InputNumber, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import { useState } from 'react'
import { api } from '../api/client'
import { AUDIT_ACTION_LABELS, type AuditAction, type AuditLog } from '../api/types'
import { useApp } from '../context/AppContext'

const ACTION_COLORS: Record<AuditAction, string> = {
  create: 'green',
  update: 'blue',
  delete: 'red',
  enable: 'cyan',
  disable: 'orange',
}

const columns: ColumnsType<AuditLog> = [
  { title: 'ID', dataIndex: 'id', width: 80 },
  {
    title: '操作时间',
    dataIndex: 'created_at',
    width: 170,
    render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
  },
  {
    title: '操作',
    dataIndex: 'action',
    width: 90,
    render: (v: AuditAction) => <Tag color={ACTION_COLORS[v]}>{AUDIT_ACTION_LABELS[v] ?? v}</Tag>,
  },
  {
    title: '目标',
    key: 'target',
    width: 130,
    render: (_, r) => `${r.target_kind === 'rule' ? '规则' : '类型'} #${r.target_id}`,
  },
  {
    title: '变更字段',
    dataIndex: 'changes',
    render: (changes: Record<string, unknown>) => {
      const keys = Object.keys(changes ?? {})
      if (!keys.length) return <Typography.Text type="secondary">-</Typography.Text>
      // changes 只存字段名 + 哈希/长度元数据，仅展示"改了哪些字段"
      return (
        <Space size={4} wrap>
          {keys.map((k) => (
            <Tag key={k}>{k}</Tag>
          ))}
        </Space>
      )
    },
  },
  { title: '操作人', dataIndex: 'operator', width: 200, ellipsis: true },
]

export default function AuditLogs() {
  const { tenantId } = useApp()

  const [targetKind, setTargetKind] = useState<string | undefined>()
  const [targetId, setTargetId] = useState<number | undefined>()
  const [action, setAction] = useState<string | undefined>()
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  const params = {
    target_kind: targetKind,
    target_id: targetId,
    action,
    from: range?.[0]?.toISOString(),
    to: range?.[1]?.toISOString(),
    page,
    page_size: pageSize,
  }

  const { data, isFetching } = useQuery({
    queryKey: ['audit-logs', tenantId, params],
    queryFn: () => api.listAuditLogs(tenantId, params),
  })

  return (
    <Card title="审计日志">
      <Typography.Paragraph type="secondary" style={{ marginTop: -8 }}>
        记录当前租户上下文的所有写操作；全局资源的操作记录在「全局（global）」下查询。变更内容仅存字段元数据，不含明文。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          style={{ width: 130 }}
          placeholder="目标种类"
          allowClear
          value={targetKind}
          onChange={(v) => {
            setTargetKind(v)
            setPage(1)
          }}
          options={[
            { value: 'rule', label: '规则' },
            { value: 'type', label: '类型' },
          ]}
        />
        <InputNumber
          placeholder="目标 ID"
          min={1}
          value={targetId}
          onChange={(v) => {
            setTargetId(v ?? undefined)
            setPage(1)
          }}
        />
        <Select
          style={{ width: 130 }}
          placeholder="操作类型"
          allowClear
          value={action}
          onChange={(v) => {
            setAction(v)
            setPage(1)
          }}
          options={Object.entries(AUDIT_ACTION_LABELS).map(([value, label]) => ({ value, label }))}
        />
        <DatePicker.RangePicker
          showTime
          value={range}
          onChange={(v) => {
            setRange(v as [Dayjs, Dayjs] | null)
            setPage(1)
          }}
        />
      </Space>

      <Table<AuditLog>
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
    </Card>
  )
}
