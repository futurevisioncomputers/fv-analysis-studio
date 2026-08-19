import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API is proxied rather than called cross-origin, so the browser sees one
// origin and SSE needs no CORS negotiation. Keeps the hosted story simple too:
// in production the same paths sit behind one server.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Port is configurable because a force-killed uvicorn can leave a zombie
      // LISTEN socket on Windows that outlives its process and blocks rebinding.
      '/api': {
        target: process.env.FV_API_URL || 'http://127.0.0.1:8010',
        changeOrigin: true,
        // SSE must not be buffered by the proxy or the rail stops moving.
        configure: (proxy) => {
          proxy.on('proxyRes', (res) => { res.headers['x-accel-buffering'] = 'no' })
        },
      },
    },
  },
})
