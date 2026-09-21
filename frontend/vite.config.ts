import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// 后端地址。默认 8020——刻意避开 Verda 占用的 8010，两套系统可以同时跑。
const BACKEND = process.env.XM3_BACKEND ?? 'http://127.0.0.1:8020'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3500,
    // 端口被占时直接失败，而不是悄悄换一个——否则代理会指向错误的后端而无人察觉
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // 只用 `src/` 下的用例，**不用 vitest 的默认 glob**。
    //
    // 默认 glob 会把 `e2e/**.spec.ts` 也收进来，而那些文件 import 的是
    // `@playwright/test`——在 jsdom 里跑它们会报"没配置 browser"之类
    // 与代码无关的错。这里写死成 `src/` 而不是 exclude `e2e/`：
    // 将来再多一个目录（比如 `loadtest` 里的脚本）时，
    // 默认行为是"不被收进来"，而不是"又混进一堆要排查的东西"。
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
