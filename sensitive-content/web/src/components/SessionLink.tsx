// 会话详情跳转链接：session_id 为空（旧数据）时不渲染链接（文档 4.3 / 7.7）
import { Typography } from 'antd'
import { sessionUrl } from '../api/client'

export default function SessionLink({ sessionId }: { sessionId: string | null | undefined }) {
  if (!sessionId) return <Typography.Text type="secondary">-</Typography.Text>
  return (
    <Typography.Link href={sessionUrl(sessionId)} target="_blank">
      {sessionId}
    </Typography.Link>
  )
}
