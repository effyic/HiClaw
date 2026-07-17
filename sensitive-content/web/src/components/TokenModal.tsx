// 管理 Token 设置对话框：Token 存 localStorage，保存后刷新全部查询
import { App as AntApp, Input, Modal, Typography } from 'antd'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { getToken, setToken } from '../api/client'
import { useApp } from '../context/AppContext'

export default function TokenModal() {
  const { tokenModalOpen, closeTokenModal } = useApp()
  const [value, setValue] = useState('')
  const queryClient = useQueryClient()
  const { message } = AntApp.useApp()

  useEffect(() => {
    if (tokenModalOpen) setValue(getToken())
  }, [tokenModalOpen])

  const save = () => {
    setToken(value.trim())
    closeTokenModal()
    message.success('Token 已保存')
    queryClient.invalidateQueries()
  }

  return (
    <Modal
      title="设置管理 Token"
      open={tokenModalOpen}
      onOk={save}
      onCancel={closeTokenModal}
      okText="保存"
      cancelText="取消"
      destroyOnClose
    >
      <Typography.Paragraph type="secondary">
        所有管理/统计接口需携带 <code>Authorization: Bearer &lt;SENSITIVE_CONTENT_ADMIN_TOKEN&gt;</code>。
        Token 仅保存在本地浏览器（localStorage）。
      </Typography.Paragraph>
      <Input.Password
        placeholder="请输入管理 Token"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onPressEnter={save}
        autoFocus
      />
    </Modal>
  )
}
