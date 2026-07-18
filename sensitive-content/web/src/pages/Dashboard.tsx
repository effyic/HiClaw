// 统计看板：概览、时间趋势（补零）、行为分布、Top 规则/类型（文档 5）
import { Column, Line, Pie } from '@ant-design/plots'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, DatePicker, Empty, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs, { type Dayjs } from 'dayjs'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import {
  ACTION_LABELS,
  type Action,
  type ByRuleItem,
  type ByTypeItem,
  type Granularity,
  type TrendItem,
} from '../api/types'
import ActionTag from '../components/ActionTag'
import SessionLink from '../components/SessionLink'
import { GLOBAL_TENANT, useApp } from '../context/AppContext'

const actionLabel = (a: string) => ACTION_LABELS[a as Action] ?? a

/** 趋势数据补零：服务端不返回无命中的时间桶，绘图前补齐（文档 5.5） */
function zeroFill(items: TrendItem[], granularity: Granularity): TrendItem[] {
  if (items.length < 2) return items
  const map = new Map(items.map((i) => [dayjs.utc(i.bucket).valueOf(), i]))
  const result: TrendItem[] = []
  let cursor = dayjs.utc(items[0].bucket)
  const last = dayjs.utc(items[items.length - 1].bucket)
  // 以首尾时间桶为界逐格推进，缺失的桶补 0
  while (cursor.valueOf() <= last.valueOf() && result.length < 2000) {
    result.push(
      map.get(cursor.valueOf()) ?? { bucket: cursor.toISOString(), events: 0, hits: 0, requests: 0 },
    )
    cursor = cursor.add(1, granularity)
  }
  return result
}

const BUCKET_FORMAT: Record<Granularity, string> = {
  hour: 'MM-DD HH:00',
  day: 'YYYY-MM-DD',
  week: 'YYYY-MM-DD',
  month: 'YYYY-MM',
}

export default function Dashboard() {
  const { tenantId, isGlobal } = useApp()

  const [range, setRange] = useState<[Dayjs, Dayjs]>(() => [dayjs().subtract(7, 'day'), dayjs()])
  const [granularity, setGranularity] = useState<Granularity>('day')

  const timeParams = { from: range[0].toISOString(), to: range[1].toISOString() }

  const { data: summary } = useQuery({
    queryKey: ['metrics-summary', tenantId, timeParams],
    queryFn: () => api.metricsSummary(tenantId, timeParams),
  })
  const { data: trend } = useQuery({
    queryKey: ['metrics-trend', tenantId, timeParams, granularity],
    queryFn: () => api.metricsTrend(tenantId, { ...timeParams, granularity }),
  })
  const { data: byAction } = useQuery({
    queryKey: ['metrics-by-action', tenantId, timeParams],
    queryFn: () => api.metricsByAction(tenantId, timeParams),
  })
  const { data: byRule } = useQuery({
    queryKey: ['metrics-by-rule', tenantId, timeParams],
    queryFn: () => api.metricsByRule(tenantId, { ...timeParams, top: 10 }),
  })
  const { data: byType } = useQuery({
    queryKey: ['metrics-by-type', tenantId, timeParams],
    queryFn: () => api.metricsByType(tenantId, { ...timeParams, top: 10 }),
  })

  // Top N 只返回 ID，需要用规则/类型接口补充展示名称（文档 7.6）；查不到的按已删除容错
  const { data: ruleNameMap } = useQuery({
    queryKey: ['rule-name-map', tenantId],
    queryFn: async () => {
      const requests = [api.listRules(GLOBAL_TENANT, { page_size: 500 })]
      if (!isGlobal) requests.push(api.listRules(tenantId, { page_size: 500 }))
      const lists = await Promise.all(requests)
      const map = new Map<number, string>()
      for (const list of lists) for (const r of list.items) map.set(r.id, r.pattern)
      return map
    },
  })
  const { data: typeNameMap } = useQuery({
    queryKey: ['type-name-map', tenantId],
    queryFn: async () => {
      const list = await api.listTypes(tenantId, { include_global: true, page_size: 500 })
      const map = new Map<number, string>()
      for (const t of list.items) map.set(t.id, t.name)
      return map
    },
  })

  const trendChartData = useMemo(() => {
    const filled = zeroFill(trend?.items ?? [], granularity)
    const metricLabels = { events: '事件数', hits: '命中次数', requests: '命中请求数' } as const
    return filled.flatMap((item) =>
      (Object.keys(metricLabels) as Array<keyof typeof metricLabels>).map((key) => ({
        time: dayjs.utc(item.bucket).local().format(BUCKET_FORMAT[granularity]),
        metric: metricLabels[key],
        value: item[key],
      })),
    )
  }, [trend, granularity])

  const ruleActionChartData = useMemo(
    () =>
      (byAction?.by_rule_action ?? []).flatMap((item) => [
        { action: actionLabel(item.rule_action), metric: '事件数', value: item.events },
        { action: actionLabel(item.rule_action), metric: '命中次数', value: item.hits },
      ]),
    [byAction],
  )

  const finalActionChartData = useMemo(
    () =>
      (byAction?.by_final_action ?? []).map((item) => ({
        action: actionLabel(item.final_action),
        requests: item.requests,
      })),
    [byAction],
  )

  const byRuleColumns: ColumnsType<ByRuleItem> = [
    {
      title: '规则',
      dataIndex: 'rule_id',
      width: 220,
      render: (id: number) => {
        const pattern = ruleNameMap?.get(id)
        return pattern ? (
          <Space size={4}>
            <Typography.Text code ellipsis={{ tooltip: pattern }} style={{ maxWidth: 150, verticalAlign: 'middle' }}>
              {pattern}
            </Typography.Text>
            <Typography.Text type="secondary">#{id}</Typography.Text>
          </Space>
        ) : (
          <Tag>规则 #{id}（已删除）</Tag>
        )
      },
    },
    {
      title: '所属类型',
      dataIndex: 'type_id',
      width: 130,
      render: (id: number) => typeNameMap?.get(id) ?? `类型 #${id}（已删除）`,
    },
    { title: '事件数', dataIndex: 'events', width: 80 },
    { title: '命中次数', dataIndex: 'hits', width: 90 },
    {
      title: '最近命中',
      dataIndex: 'last_hit_at',
      width: 150,
      render: (v: string | null) => (v ? dayjs(v).format('MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '最近会话',
      dataIndex: 'last_session_id',
      width: 180,
      ellipsis: true,
      render: (v: string | null) => <SessionLink sessionId={v} />,
    },
  ]

  const byTypeColumns: ColumnsType<ByTypeItem> = [
    {
      title: '类型',
      dataIndex: 'type_id',
      width: 180,
      ellipsis: { showTitle: false },
      render: (id: number) => {
        const name = typeNameMap?.get(id) ?? `类型 #${id}（已删除）`
        return (
          <Typography.Text ellipsis={{ tooltip: name }} style={{ maxWidth: 160 }}>
            {name}
          </Typography.Text>
        )
      },
    },
    { title: '事件数', dataIndex: 'events', width: 80 },
    { title: '命中次数', dataIndex: 'hits', width: 90 },
    {
      title: '最近命中',
      dataIndex: 'last_hit_at',
      width: 140,
      render: (v: string | null) => (v ? dayjs(v).format('MM-DD HH:mm:ss') : '-'),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <Card size="small">
        <Space wrap>
          <Typography.Text>时间范围：</Typography.Text>
          <DatePicker.RangePicker
            showTime
            allowClear={false}
            value={range}
            onChange={(v) => v && setRange(v as [Dayjs, Dayjs])}
            presets={[
              { label: '近 24 小时', value: [dayjs().subtract(24, 'hour'), dayjs()] },
              { label: '近 7 天', value: [dayjs().subtract(7, 'day'), dayjs()] },
              { label: '近 30 天', value: [dayjs().subtract(30, 'day'), dayjs()] },
              { label: '近 90 天', value: [dayjs().subtract(90, 'day'), dayjs()] },
            ]}
          />
          <Typography.Text type="secondary">命中事件默认保留 90 天</Typography.Text>
        </Space>
      </Card>

      {/* 窄屏时自动换行堆叠，避免卡片互相挤压 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} xl={6}>
          <Card style={{ height: '100%' }}>
            <Statistic title="命中事件数" value={summary?.total_events ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Card style={{ height: '100%' }}>
            <Statistic title="命中次数合计" value={summary?.total_hits ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Card style={{ height: '100%' }}>
            <Statistic title="命中请求数" value={summary?.hit_requests ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={12} xl={6}>
          <Card style={{ height: '100%' }}>
            <Typography.Text type="secondary" style={{ fontSize: 14 }}>
              最终行为分布（请求数）
            </Typography.Text>
            <div style={{ marginTop: 8 }}>
              {summary && Object.keys(summary.final_actions).length ? (
                <Space size={[4, 8]} wrap>
                  {Object.entries(summary.final_actions).map(([action, count]) => (
                    <span key={action} style={{ whiteSpace: 'nowrap' }}>
                      <ActionTag action={action} />
                      {count}
                    </span>
                  ))}
                </Space>
              ) : (
                <Typography.Text type="secondary">-</Typography.Text>
              )}
            </div>
          </Card>
        </Col>
      </Row>

      <Card
        title="命中趋势"
        extra={
          <Select
            style={{ width: 120 }}
            value={granularity}
            onChange={setGranularity}
            options={[
              { value: 'hour', label: '按小时' },
              { value: 'day', label: '按天' },
              { value: 'week', label: '按周' },
              { value: 'month', label: '按月' },
            ]}
          />
        }
      >
        {trendChartData.length ? (
          <Line
            height={280}
            data={trendChartData}
            xField="time"
            yField="value"
            colorField="metric"
            axis={{ y: { title: null } }}
          />
        ) : (
          <Empty description="所选时间范围内暂无命中数据" />
        )}
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={14}>
          <Card title="按规则行为统计（全部事件）" style={{ height: '100%' }}>
            {ruleActionChartData.length ? (
              <Column
                height={260}
                data={ruleActionChartData}
                xField="action"
                yField="value"
                colorField="metric"
                group
              />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card title="按最终行为统计（请求数）" style={{ height: '100%' }}>
            {finalActionChartData.length ? (
              <Pie
                height={260}
                data={finalActionChartData}
                angleField="requests"
                colorField="action"
                innerRadius={0.6}
                label={{ text: 'requests' }}
              />
            ) : (
              <Empty description="暂无数据" />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} xl={14}>
          <Card title="Top 10 命中规则" style={{ height: '100%' }}>
            <Table<ByRuleItem>
              rowKey="rule_id"
              size="small"
              columns={byRuleColumns}
              dataSource={byRule?.items ?? []}
              pagination={false}
              scroll={{ x: 760 }}
            />
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card title="Top 10 命中类型" style={{ height: '100%' }}>
            <Table<ByTypeItem>
              rowKey="type_id"
              size="small"
              columns={byTypeColumns}
              dataSource={byType?.items ?? []}
              pagination={false}
              scroll={{ x: 490 }}
            />
          </Card>
        </Col>
      </Row>
    </Space>
  )
}
