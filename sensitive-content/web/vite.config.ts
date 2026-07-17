import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发环境将 API 请求代理到本地 sensitive-content 服务（默认 8091 端口）
// 注意：必须用 127.0.0.1 而非 localhost——Node 会优先解析 IPv6 ::1，而服务只监听 IPv4，会导致代理 502
const DEV_API_TARGET = process.env.SC_DEV_API_TARGET || 'http://127.0.0.1:8091'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      '/api': { target: DEV_API_TARGET, changeOrigin: true },
      '/healthz': { target: DEV_API_TARGET, changeOrigin: true },
    },
  },
})
