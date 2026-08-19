import { defineConfig, devices } from '@playwright/test'

const API_PORT = process.env.FV_API_PORT || '8010'
const API_URL = `http://127.0.0.1:${API_PORT}`

// Both servers are started BY the test run. A suite that assumes someone
// already ran uvicorn passes on the machine where they did and fails
// everywhere else — and the failure looks like a broken app rather than a
// missing process.
export default defineConfig({
  testDir: './e2e',
  // A full pipeline run is ~1s of pandas, but the first one pays import cost
  // for pandas, pyarrow and openpyxl. Generous, and each test still asserts on
  // a specific condition rather than sleeping.
  timeout: 120_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,     // one backend, one runs directory, real files
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],

  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],

  // 8010, not 8000. A force-killed uvicorn on Windows can leave a LISTEN
  // socket owned by a dead PID, and the next bind fails with WinError 10048
  // on a port nothing is actually serving. The port lives in ONE place: this
  // constant, passed to vite as FV_API_URL so the proxy cannot drift from it.
  webServer: [
    {
      command: `python -m uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: '../backend',
      url: `${API_URL}/api/health`,
      reuseExistingServer: true,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npx vite --port 5173 --strictPort --host 127.0.0.1',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: true,
      timeout: 120_000,
      // vite reads the proxy target at config load, so it must be in the
      // environment of the process playwright spawns — not merely exported in
      // whatever shell happened to start the dev server by hand.
      env: { FV_API_URL: API_URL },
    },
  ],
})
