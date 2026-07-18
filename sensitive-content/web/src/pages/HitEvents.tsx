// 命中事件明细：筛选 + 会话跳转；不提供"查看原文"入口（隐私约束，文档 4.3）
import { useQuery } from '@tanstack/react-query'
import { Card, DatePicker, Input, InputNumber, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import type { HitEvent } from '../api/types'
import ActionTag from '../components/ActionTag'
import SessionLink from '../components/SessionLink'
import { useApp } from '../context/AppContext'

export default function HitEvents() {
  const { tenantId, isGlobal } = useApp()

  const [ruleId, setRuleId] = useState<number | undefined>()
  const [typeId, setTypeId] = useState<number | undefined>()
  const [roleCode, setRoleCode] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  const params = {
    rule_id: ruleId,
    type_id: typeId,
    role_code: roleCode || undefined,
    session_id: sessionId || undefined,
    from: range?.[0]?.toISOString(),
    to: range?.[1]?.toISOString(),
    page,
    page_size: pageSize,
  }

  const { data, isFetching } = useQuery({
    queryKey: ['hit-events', tenantId, params],
    queryFn: () => api.listHitEvents(tenantId, params),
  })

  const columns: ColumnsType<HitEvent> = useMemo(() => {
    const cols: ColumnsType<HitEvent> = [
      {
        title: '命中时间',
        dataIndex: 'hit_at',
        width: 170,
        render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
      },
    ]
    // 全局视图下跨租户查询，展示事件真实租户
    if (isGlobal) {
      cols.push({ title: '租户', dataIndex: 'tenant_id', width: 120 })
    }
    cols.push(
      {
        title: 'Agent',
        dataIndex: 'role_code',
        width: 180,
        render: (v: string | null) => v || '-',
      },
      { title: '规则', dataIndex: 'rule_id', width: 90, render: (v: number) => `#${v}` },
      { title: '类型', dataIndex: 'type_id', width: 90, render: (v: number) => `#${v}` },
      {
        title: '规则行为',
        dataIndex: 'rule_action',
        width: 120,
        render: (v: string) => <ActionTag action={v} />,
      },
      {
        title: '最终行为',
        dataIndex: 'final_action',
        width: 150,
        render: (v: string, record) => (
          <Space size={4}>
            <ActionTag action={v} />
            {record.selected && (
              <Tooltip title="本条命中是该请求的最终裁决所选">
                <Tag color="green">最终裁决</Tag>
              </Tooltip>
            )}
          </Space>
        ),
      },
      { title: '命中次数', dataIndex: 'hit_count', width: 90 },
      {
        title: '会话',
        dataIndex: 'session_id',
        ellipsis: true,
        render: (v: string) => <SessionLink sessionId={v} />,
      },
      {
        title: '策略版本',
        dataIndex: 'policy_version',
        width: 150,
        render: (v: string) => (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {v}
          </Typography.Text>
        ),
      },
    )
    return cols
  }, [isGlobal])

  return (
    <Card title="命中事件明细">
      <Typography.Paragraph type="secondary" style={{ marginTop: -8 }}>
        命中事件不含用户消息原文，默认保留 90 天；查看上下文请通过会话链接跳转会话详情。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 16 }} wrap>
        <InputNumber
          placeholder="规则 ID"
          min={1}
          value={ruleId}
          onChange={(v) => {
            setRuleId(v ?? undefined)
            setPage(1)
          }}
        />
        <InputNumber
          placeholder="类型 ID"
          min={1}
          value={typeId}
          onChange={(v) => {
            setTypeId(v ?? undefined)
            setPage(1)
          }}
        />
        <Input.Search
          style={{ width: 220 }}
          placeholder="Agent role-code（精确匹配）"
          allowClear
          onSearch={(v) => {
            setRoleCode(v.trim())
            setPage(1)
          }}
        />
        <Input.Search
          style={{ width: 240 }}
          placeholder="会话 ID（精确匹配）"
          allowClear
          onSearch={(v) => {
            setSessionId(v.trim())
            setPage(1)
          }}
        />
        <DatePicker.RangePicker
          showTime
          value={range}
          onChange={(v) => {
            setRange(v as [Dayjs, Dayjs] | null)
            setPage(1)
          }}
          presets={[
            { label: '近 24 小时', value: [dayjs().subtract(24, 'hour'), dayjs()] },
            { label: '近 7 天', value: [dayjs().subtract(7, 'day'), dayjs()] },
            { label: '近 30 天', value: [dayjs().subtract(30, 'day'), dayjs()] },
            { label: '近 90 天', value: [dayjs().subtract(90, 'day'), dayjs()] },
          ]}
        />
      </Space>

      <Table<HitEvent>
        rowKey="event_id"
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
