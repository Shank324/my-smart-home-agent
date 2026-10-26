import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // 编辑器的原子写入会在源码目录留下临时目录，chokidar 监听它会抛 EBUSY 把 dev server 打挂
    watch: {
      ignored: ['**/.*.tmpdir/**', '**/.*.tmp', '**/*.tmp'],
    },
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true, changeOrigin: true },
    },
  },
})
