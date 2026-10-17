import { defineConfig, mergeConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { createShizuhaConfig } from '@shizuha/ui/vite'

const basePath = process.env.VITE_BASE_PATH || '/'

export default mergeConfig(
  createShizuhaConfig({ port: 80, serviceName: 'pulse' }),
  defineConfig({
    plugins: [react()],
    build: {
      // PLAT-875: sellable-shell perf budget — warn at build (CI-visible) when any
      // emitted chunk exceeds the budget, so bundle regressions surface before deploy.
      // Paired with route-level code splitting in App.jsx. See docs/PERFORMANCE_BUDGET.md.
      chunkSizeWarningLimit: 600,
      rollupOptions: {
        output: {
          // Split rarely-changing vendor libs into their own long-cacheable chunks
          // so the app chunk stays small and vendor isn't re-downloaded on each deploy.
          manualChunks: {
            'react-vendor': ['react', 'react-dom', 'react-router-dom'],
            'query-vendor': ['@tanstack/react-query'],
          },
        },
      },
    },
    server: {
      proxy: basePath === '/' ? {
        '/api': {
          target: process.env.VITE_API_URL || 'http://localhost:8002',
          changeOrigin: true,
        },
        '/admin': {
          target: process.env.VITE_API_URL || 'http://localhost:8002',
          changeOrigin: true,
        },
      } : {},
    },
  })
)
