import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// /api/auth/* goes to the shared Quarc Auth service; everything else under
// /api/* goes to this app's own API. Mirrors how nginx splits these two routes
// in production (shared between `dev` and `preview`).
const apiProxy = {
  '/api/auth': {
    target: process.env.VITE_AUTH_PROXY_TARGET || 'http://localhost:3902',
    changeOrigin: true,
  },
  '/api': {
    target: process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000',
    changeOrigin: true,
  },
}

export default defineConfig({
  plugins: [react()],
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
  test: { environment: 'node' },
})
