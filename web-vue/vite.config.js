import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/v1': 'http://127.0.0.1:8765',
    }
  },
  build: {
    // 直接输出到后端静态托管目录(web/), 与 docs/01-快速开始.md 描述一致
    outDir: '../web',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('element-plus')) return 'element-plus'
          if (id.includes('@element-plus/icons-vue')) return 'element-icons'
        }
      }
    }
  }
})