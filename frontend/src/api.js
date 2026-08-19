// One place that knows the API shape. Every call returns parsed JSON or
// throws with the server's own message — a guessed error message wastes the
// operator's time, and the backend already says something specific.

async function json(res) {
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not json */ }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  rail: () => fetch('/api/rail').then(json),
  runs: () => fetch('/api/runs').then(json),
  run: (id) => fetch(`/api/runs/${id}`).then(json),
  model: (id) => fetch(`/api/runs/${id}/model`).then(json),
  checkpoint: (id, stage) => fetch(`/api/runs/${id}/checkpoint/${stage}`).then(json),

  create: (file, question) => {
    const body = new FormData()
    body.append('file', file)
    const qs = question ? `?question=${encodeURIComponent(question)}` : ''
    return fetch(`/api/runs${qs}`, { method: 'POST', body }).then(json)
  },

  modules: () => fetch('/api/modules').then(json),

  // `modules` null means "let stage 1 infer scope from the words"; an array
  // sets it outright, which is what answering the scope question requires.
  setQuestion: (id, question, modules = null) =>
    fetch(`/api/runs/${id}/question`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, modules }),
    }).then(json),

  start: (id, { auto = false, stages = null } = {}) =>
    fetch(`/api/runs/${id}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto, stages }),
    }).then(json),

  remove: (id) => fetch(`/api/runs/${id}`, { method: 'DELETE' }).then(json),
  artifactUrl: (id, name) => `/api/runs/${id}/artifact/${name}`,
}

export const fmtMs = (ms) =>
  ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`

export const fmtInt = (n) =>
  typeof n === 'number' ? n.toLocaleString() : String(n ?? '—')

export const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)
