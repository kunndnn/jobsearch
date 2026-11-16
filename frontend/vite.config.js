import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api to the dashboard backend (python start.py).
// Override with VITE_PORT / JOBSPY_API_URL when those ports are taken.
const devPort = Number(process.env.VITE_PORT) || 5173
const apiTarget = process.env.JOBSPY_API_URL || `http://127.0.0.1:${process.env.PORT || 8000}`

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: devPort,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
