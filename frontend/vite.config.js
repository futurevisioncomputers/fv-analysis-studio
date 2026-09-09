import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API is proxied rather than called cross-origin, so the browser sees one
// origin and SSE needs no CORS negotiation. Keeps the hosted story simple too:
// in production the same paths sit behind one server.

// 8010, not 8000: a force-killed uvicorn on Windows can leave a zombie LISTEN
// socket on 8000 that outlives its process and blocks rebinding. The whole
// project standardises on this port — backend tests, playwright.config.js and
// the README all name 8010. Override with FV_API_URL, but move both ends.
const API_TARGET = process.env.FV_API_URL || 'http://127.0.0.1:8010'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        configure: (proxy) => {
          // SSE must not be buffered by the proxy or the rail stops moving.
          proxy.on('proxyRes', (res) => { res.headers['x-accel-buffering'] = 'no' })

          // A backend on the wrong port fails silently and misleadingly: the
          // proxy logs one ECONNREFUSED line, the browser only ever says
          // "Failed to fetch", and curl against the port you did start reports
          // a perfectly healthy API. Say what is actually wrong, once.
          let warned = false
          proxy.on('error', (err) => {
            if (err.code !== 'ECONNREFUSED' || warned) return
            warned = true
            console.error(
              `\n  No backend at ${API_TARGET}.\n` +
              '  Start it with:  cd backend && python -m uvicorn app.main:app --reload --port 8010\n' +
              '  Using another port? Set FV_API_URL to match when you run the frontend.\n'
            )
          })
        },
      },
    },
  },
})
