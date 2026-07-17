// 资源归属标签：tenant_id 为空串表示全局资源（文档 3.1 / 3.2）
import { Tag } from 'antd'

export default function ScopeTag({ tenantId }: { tenantId: string }) {
  if (tenantId === '') return <Tag color="geekblue">全局</Tag>
  return <Tag>{tenantId}</Tag>
}
