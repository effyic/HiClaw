// 响应行为枚举标签：不同行为用不同颜色区分
import { Tag } from 'antd'
import { ACTION_LABELS, type Action } from '../api/types'

const ACTION_COLORS: Record<Action, string> = {
  END_CONVERSATION: 'volcano',
  FIXED_REPLY: 'blue',
  BLOCK_REQUEST: 'red',
  REDACT_AND_CONTINUE: 'gold',
  LOG_ONLY: 'default',
  BUSINESS_ACTION: 'purple',
  CUSTOM_RESPONSE: 'cyan',
  ADJUST_PROMPT: 'green',
}

export default function ActionTag({ action }: { action: string }) {
  const known = action in ACTION_LABELS
  return (
    <Tag color={known ? ACTION_COLORS[action as Action] : 'default'}>
      {known ? ACTION_LABELS[action as Action] : action}
    </Tag>
  )
}
