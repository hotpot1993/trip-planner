import { fileURLToPath } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 后端默认端口，与 lushu/config.py 的 PORT 保持一致
const BACKEND = 'http://127.0.0.1:8756'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // shadcn/ui 的组件默认按 @/ 前缀引用，先把这个别名叫好
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 开发期把接口转发给本地后端，前端代码里一律用相对路径 /api
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
