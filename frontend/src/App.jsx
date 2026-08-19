import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, fmtInt, fmtMs, pct } from './api.js'

// The operator's own pipeline diagram lists 13 agents. The plugin runs 12
// stages, because Statistical Analysis and Multifactor Analysis are both the
// Analyst's work rather than separate passes over the data. Rather than
// invent a 13th node that never lights up, the Analyst says what it contains.
const CONTAINS = {
  analyst: ['Statistical Analysis', 'Multifactor Analysis'],
}

const STATUS_ORDER = ['running', 'blocked', 'failed', 'done', 'skipped', 'pending']

export default function App() {
  const [runId, setRunId] = useState(null)
  const [state, setState] = useState(null)
  const [rail, setRail] = useState([])
  const [tab, setTab] = useState('pipeline')
  const [selected, setSelected] = useState(null)
  const [checkpoint, setCheckpoint] = useState(null)
  const [model, setModel] = useState(null)
  const [error, setError] = useState(null)
  const [live, setLive] = useState(null)

  useEffect(() => { api.rail().then(setRail).catch(() => {}) }, [])

  const refresh = useCallback(async (id) => {
    if (!id) return
    try { setState(await api.run(id)) } catch (e) { setError(e.message) }
  }, [])

  // SSE: every event restates a fact already on disk, so the refresh that
  // follows is the source of truth and a dropped stream is recoverable.
  useEffect(() => {
    if (!runId) return
    const es = new EventSource(`/api/runs/${runId}/events`)
    es.onmessage = (msg) => {
      const event = JSON.parse(msg.data)
      setLive(event)
      refresh(runId)
      if (event.type === 'stage_done' && selected === event.stage) {
        api.checkpoint(runId, event.stage).then(setCheckpoint).catch(() => {})
      }
    }
    es.onerror = () => { /* EventSource retries on its own */ }
    return () => es.close()
  }, [runId, refresh, selected])

  useEffect(() => {
    if (!runId || !selected) return
    api.checkpoint(runId, selected).then(setCheckpoint).catch(() => setCheckpoint(null))
  }, [runId, selected, state])

  // A blocked stage is the one thing the operator has to act on, so it selects
  // itself rather than waiting to be found in the rail.
  useEffect(() => {
    const stuck = state?.progress?.find((r) => r.status === 'blocked')
    if (stuck && selected !== stuck.key) setSelected(stuck.key)
  }, [state]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!runId || tab !== 'model') return
    api.model(runId).then(setModel).catch((e) => setError(e.message))
  }, [runId, tab])

  async function onUpload(file, question) {
    setError(null)
    try {
      const { run_id } = await api.create(file, question)
      setRunId(run_id)
      setSelected(null)
      setCheckpoint(null)
      await refresh(run_id)
      setTab('pipeline')
    } catch (e) { setError(e.message) }
  }

  const progress = state?.progress ?? []
  const done = progress.filter((r) => r.status === 'done').length
  const reportReady = state?.artifacts?.['report.html']

  return (
    <div className="app">
      <div className="topbar">
        <h1>FV Analysis Studio</h1>
        {state && (
          <span className="muted mono">
            {done}/{progress.length} stages · {state.sources?.length ?? 0} sheets
          </span>
        )}
        <span className="grow" />
        {runId && (
          <nav className="tabs">
            {['pipeline', 'model', 'report'].map((t) => (
              <button key={t} className="tab" data-on={tab === t ? '1' : '0'}
                      onClick={() => setTab(t)}>
                {t === 'pipeline' ? 'Pipeline' : t === 'model' ? 'Data model' : 'Report'}
              </button>
            ))}
          </nav>
        )}
        {runId && (
          <button onClick={() => { setRunId(null); setState(null); setSelected(null) }}>
            New run
          </button>
        )}
      </div>

      <div className="body">
        {error && <div className="banner bad" style={{ marginBottom: 14 }}>{error}</div>}

        {!runId && <Upload onUpload={onUpload} rail={rail} />}

        {runId && tab === 'pipeline' && (
          <div className="cols">
            <div className="side">
              <Controls state={state} runId={runId} onRefresh={() => refresh(runId)}
                        onError={setError} />
              <Rail progress={progress} selected={selected} onSelect={setSelected}
                    live={live} />
            </div>
            <div>
              {selected
                ? <AgentCard checkpoint={checkpoint} runId={runId}
                             row={progress.find((r) => r.key === selected)}
                             onRerun={() => refresh(runId)} />
                : <>
                    <Discovery integrity={state?.integrity} />
                    <Timeline progress={progress} />
                  </>}
            </div>
          </div>
        )}

        {runId && tab === 'model' && <ModelView model={model} />}

        {runId && tab === 'report' && (
          reportReady
            ? <iframe className="report" title="Institute report"
                      src={api.artifactUrl(runId, 'report.html')} />
            : <div className="card muted">
                The Report Writer has not run yet. Run the pipeline and it appears here.
              </div>
        )}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ upload */

function Upload({ onUpload, rail }) {
  const [over, setOver] = useState(false)
  const [question, setQuestion] = useState('')
  const input = useRef(null)

  const take = (files) => { if (files?.[0]) onUpload(files[0], question) }

  return (
    <>
      <div className="card">
        <header><h2>Upload a workbook</h2>
          <span className="muted">.xlsx or .csv — every sheet becomes a source</span>
        </header>

        {/* The question comes FIRST because it is needed first: Problem
            Definition refuses to invent a goal, so a file dropped before this
            is filled stops at stage 1 asking for one. */}
        <label className="muted" style={{ fontSize: 13 }}>
          What do you want to know?
        </label>
        <input value={question} onChange={(e) => setQuestion(e.target.value)}
               placeholder="Which branches and course categories drive revenue and completion?"
               style={{
                 width: '100%', margin: '6px 0 14px', padding: '9px 11px',
                 borderRadius: 8, border: '1px solid var(--line)',
                 background: 'var(--panel-2)', color: 'var(--ink)', font: 'inherit',
               }} />

        <div className="drop" data-over={over ? '1' : '0'}
             onDragOver={(e) => { e.preventDefault(); setOver(true) }}
             onDragLeave={() => setOver(false)}
             onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files) }}>
          <p style={{ margin: '0 0 12px' }}>Drop a file here, or</p>
          <button className="primary" onClick={() => input.current?.click()}>
            Choose a file
          </button>
          <input ref={input} type="file" accept=".csv,.xlsx,.xls" hidden
                 onChange={(e) => take(e.target.files)} />
          <p className="dim" style={{ marginBottom: 0, marginTop: 14, fontSize: 12 }}>
            Files stay on this machine. Deleting the run deletes them.
          </p>
        </div>
      </div>

      <div className="card">
        <header><h2>What will run</h2>
          <span className="muted">{rail.length} agents, in dependency order</span>
        </header>
        <div className="chips">
          {rail.map((s) => (
            <span key={s.key} className="chip">
              {s.n} {s.label}{s.optional ? ' (optional)' : ''}
            </span>
          ))}
        </div>
      </div>
    </>
  )
}

/* --------------------------------------------------------------- discovery */

function Discovery({ integrity }) {
  if (!integrity) return null
  const { tables = [], links = [], ok, verdict, total_rows } = integrity
  return (
    <div className="card">
      <header><h2>Dataset discovery</h2>
        <span className="muted">{tables.length} sheets · {fmtInt(total_rows)} rows</span>
      </header>
      <div className={`banner ${ok ? 'ok' : links.length ? 'bad' : 'warn'}`}>{verdict}</div>
      <div className="scroll" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr><th>Sheet</th><th className="num">Rows</th><th className="num">Cols</th>
                <th>Key</th></tr>
          </thead>
          <tbody>
            {tables.map((t) => (
              <tr key={t.sheet}>
                <td>{t.sheet}</td>
                <td className="num">{fmtInt(t.rows)}</td>
                <td className="num">{t.columns}</td>
                <td className="mono dim">{t.primary_key ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {links.length > 0 && (
        <div className="scroll" style={{ marginTop: 12 }}>
          <table>
            <thead>
              <tr><th>Relationship</th><th className="num">Keys</th>
                  <th className="num">Orphans</th><th className="num">Resolves</th></tr>
            </thead>
            <tbody>
              {links.map((l, i) => (
                <tr key={i}>
                  <td className="mono">{l.from_sheet} → {l.to_sheet}
                    <span className="dim"> ({l.column})</span></td>
                  <td className="num">{fmtInt(l.distinct_keys)}</td>
                  <td className="num">{fmtInt(l.orphans)}</td>
                  <td className="num" style={{ color: l.ok ? 'var(--ok)' : 'var(--bad)' }}>
                    {pct(l.resolve_rate)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/* -------------------------------------------------------------------- rail */

function Rail({ progress, selected, onSelect, live }) {
  return (
    <div className="card">
      <header><h2>Agent pipeline</h2>
        <span className="muted">
          {live?.type === 'stage_started' ? `working: ${live.stage}` : 'click an agent'}
        </span>
      </header>
      <div className="rail">
        {progress.map((r) => (
          <button key={r.key} className="node" data-status={r.status}
                  data-sel={selected === r.key ? '1' : '0'}
                  onClick={() => onSelect(r.key)}>
            <span className="n">{r.n}</span>
            <span style={{ minWidth: 0 }}>
              <div className="label">
                {r.label}{r.optional ? <span className="dim"> · optional</span> : null}
              </div>
              {CONTAINS[r.key] && (
                <div className="sub dim">contains {CONTAINS[r.key].join(' · ')}</div>
              )}
              {r.summary && <div className="sub">{r.summary}</div>}
            </span>
            <span className={`pill ${r.status}`}>
              {r.status === 'done' && r.duration_ms != null
                ? fmtMs(r.duration_ms) : r.status}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- timeline */

function Timeline({ progress }) {
  const timed = progress.filter((r) => r.duration_ms != null)
  if (!timed.length) return null
  const total = timed.reduce((sum, r) => sum + r.duration_ms, 0)
  const worst = Math.max(...timed.map((r) => r.duration_ms))
  return (
    <div className="card">
      <header><h2>Where the time went</h2>
        <span className="muted mono">{fmtMs(total)} total</span>
      </header>
      <div className="timeline">
        {timed.map((r) => (
          <div className="trow" key={r.key}>
            <span className="dim" style={{ overflow: 'hidden', textOverflow: 'ellipsis',
                                           whiteSpace: 'nowrap' }}>{r.label}</span>
            <span className="track">
              <span className="fill" data-hot={r.duration_ms === worst ? '1' : '0'}
                    style={{ width: `${Math.max(1, (r.duration_share ?? 0) * 100)}%` }} />
            </span>
            <span className="mono dim" style={{ textAlign: 'right' }}>
              {pct(r.duration_share)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- controls */

function Controls({ state, runId, onRefresh, onError }) {
  const [busy, setBusy] = useState(false)
  const next = state?.next_stage

  async function go(options) {
    setBusy(true)
    try { await api.start(runId, options); onRefresh() }
    catch (e) { onError(e.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="card">
      <header><h2>Run</h2>
        {state?.running && <span className="pill running">working</span>}
      </header>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <button className="primary" disabled={busy || state?.running || !next}
                onClick={() => go({ auto: true })}>
          Run everything
        </button>
        <button disabled={busy || state?.running || !next}
                onClick={() => go({ stages: [next] })}>
          {next ? `Next: ${next}` : 'Complete'}
        </button>
      </div>
      {state?.errors?.length > 0 && (
        <ul className="list muted" style={{ marginTop: 12, fontSize: 12 }}>
          {state.errors.slice(-3).map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- agent card */

// Problem Definition asks eleven questions when it has no goal, and ten of
// them are KPI targets it already has a default for. Listing all eleven with
// equal weight makes an unblockable-looking wall out of a single missing
// sentence, so the one that actually blocks is separated from the rest.
const SOFT = /^confirm/i

function Asking({ detail }) {
  const [open, setOpen] = useState(false)
  const items = (Array.isArray(detail) ? detail : [detail])
    .map((d) => (typeof d === 'string' ? d : JSON.stringify(d)))
  const blocking = items.filter((d) => !SOFT.test(d))
  const optional = items.filter((d) => SOFT.test(d))

  return (
    <div style={{ marginBottom: 12 }}>
      <h3>It is asking for</h3>
      <ul className="list">
        {blocking.map((d, i) => <li key={i}>{d}</li>)}
      </ul>
      {optional.length > 0 && (
        <>
          <button className="subtab" onClick={() => setOpen(!open)}
                  style={{ padding: '3px 0', color: 'var(--ink-2)' }}>
            {open ? '▾' : '▸'} {optional.length} more it can default
          </button>
          {open && (
            <ul className="list muted" style={{ fontSize: 13 }}>
              {optional.map((d, i) => <li key={i}>{d}</li>)}
            </ul>
          )}
        </>
      )}
    </div>
  )
}

function AnswerClarification({ runId, onDone }) {
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [modules, setModules] = useState([])
  const [picked, setPicked] = useState(null)   // null = all modules

  useEffect(() => { api.modules().then(setModules).catch(() => setModules([])) }, [])

  function toggle(key) {
    setPicked((prev) => {
      const base = prev ?? modules.map((m) => m.key)
      return base.includes(key) ? base.filter((k) => k !== key) : [...base, key]
    })
  }

  async function submit() {
    if (!question.trim()) return
    setBusy(true); setErr(null)
    try {
      // Scope goes over explicitly. Stage 1 asks "all modules or only some?"
      // and treats an explicit module map as the answer; leaving it to the
      // free text means the answer only lands if it happens to contain a
      // module keyword, and re-asks the same question when it does not.
      //
      // Unless the module list has not arrived yet. `modules` starts empty and
      // fills from a fetch, so answering quickly — type, Enter — used to send
      // an explicit scope of NO modules and come back with "at least one
      // module has to be in scope", while the chips it was complaining about
      // rendered underneath the error a moment later. Sending null there means
      // "infer the scope from the words", which is the honest fallback: it
      // degrades to a guess rather than to a contradiction.
      const scope = picked ?? (modules.length ? modules.map((m) => m.key) : null)
      await api.setQuestion(runId, question, scope)
      // Stage 1 is the only thing that has run, so re-running it is the whole
      // recovery — the workbook has not changed, only the brief.
      await api.start(runId, { auto: true })

      // Say so when the answer did not take. Without this the card re-renders
      // identically — same question, same blocked badge — and the run looks
      // like it never happened.
      const state = await waitIdle(runId)
      const stage1 = state.progress.find((r) => r.key === 'problem')
      if (stage1?.status === 'blocked') {
        setErr('Stage 1 is still blocked on that answer. Narrow the scope '
             + 'below, or name what you want measured (fees, dropout, '
             + 'certificates, enquiries).')
      }
      onDone?.()
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  const all = picked === null || picked.length === modules.length

  return (
    <div style={{ marginTop: 4 }}>
      <label className="muted" style={{ fontSize: 13 }}>
        Answer it here — the workbook is already uploaded.
      </label>
      <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
        <input value={question} autoFocus
               onChange={(e) => setQuestion(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
               placeholder="Which branches and course categories drive revenue and completion?"
               style={{
                 flex: 1, padding: '9px 11px', borderRadius: 8,
                 border: '1px solid var(--line)', background: 'var(--panel-2)',
                 color: 'var(--ink)', font: 'inherit',
               }} />
        <button className="primary" disabled={busy || !question.trim()}
                onClick={submit}>
          {busy ? 'Running…' : 'Run'}
        </button>
      </div>

      {modules.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
            Scope — {all ? 'all modules' : `${picked.length} of ${modules.length} modules`}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            <button className="subtab" data-on={all ? '1' : '0'}
                    onClick={() => setPicked(null)}
                    style={{ padding: '4px 10px' }}>All</button>
            {modules.map((m) => {
              const on = picked === null || picked.includes(m.key)
              return (
                <button key={m.key} className="subtab" data-on={on ? '1' : '0'}
                        onClick={() => toggle(m.key)}
                        title={m.metrics.join(', ')}
                        style={{ padding: '4px 10px' }}>
                  {m.key.replace(/_/g, ' ')}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {err && <div className="banner bad" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  )
}

/** Poll until the run stops working, so "did it actually get past stage 1?"
 *  has an answer. Bounded — a hung worker must not hang the button. */
async function waitIdle(runId, tries = 40) {
  for (let i = 0; i < tries; i++) {
    const state = await api.run(runId)
    if (!state.running) return state
    await new Promise((r) => setTimeout(r, 400))
  }
  return api.run(runId)
}

function AgentCard({ checkpoint, row, runId, onRerun }) {
  const [pane, setPane] = useState('summary')
  if (!checkpoint || !row) {
    return <div className="card muted">Select an agent to see what it did.</div>
  }
  const metrics = Object.entries(checkpoint.metrics ?? {})
  const artifacts = Object.keys(checkpoint.artifacts ?? {})
  const details = checkpoint.details ?? []
  const blocked = checkpoint.status === 'blocked'
  const pending = checkpoint.status === 'pending'

  // Tiles are the two or three numbers that describe this agent's work, not
  // every metric — a wall of equal-weight numbers reads as none of them.
  const tiles = metrics.slice(0, 3)

  const panes = [
    ['summary', 'Summary', details.length],
    ['performance', 'Performance', metrics.length],
    ['artifacts', 'Artifacts', artifacts.length],
  ]

  return (
    <div className="card">
      <header>
        <h2>{checkpoint.n} · {checkpoint.label}</h2>
        <span className={`pill ${checkpoint.status}`}>{checkpoint.status}</span>
        <span className="grow" style={{ flex: 1 }} />
        {checkpoint.duration_ms != null && (
          <span className="muted mono">{fmtMs(checkpoint.duration_ms)}</span>
        )}
      </header>

      {pending && (
        <p className="muted" style={{ marginTop: 0 }}>
          This agent has not run yet.
          {row.requires?.length > 0 && ` It needs ${row.requires.join(', ')} first.`}
        </p>
      )}

      {blocked && (
        <>
          <div className="banner bad" style={{ marginBottom: 12 }}>
            {checkpoint.summary}
          </div>
          {checkpoint.detail && <Asking detail={checkpoint.detail} />}
          {checkpoint.stage === 'problem' && (
            <AnswerClarification runId={runId} onDone={onRerun} />
          )}
        </>
      )}

      {!pending && !blocked && (
        <>
          {tiles.length > 0 && (
            <div className="tiles">
              {tiles.map(([k, v]) => (
                <div className="tile" key={k}>
                  <div className="v">{fmtInt(v)}</div>
                  <div className="k">{k.replace(/_/g, ' ')}</div>
                </div>
              ))}
              <div className="tile">
                <div className="v">{pct(row.duration_share)}</div>
                <div className="k">of run time</div>
              </div>
            </div>
          )}

          <div className="subtabs">
            {panes.map(([id, label, count]) => (
              <button key={id} className="subtab" data-on={pane === id ? '1' : '0'}
                      onClick={() => setPane(id)}>
                {label} <span className="count">{count}</span>
              </button>
            ))}
          </div>

          {pane === 'summary' && (
            <>
              {checkpoint.summary && <p style={{ marginTop: 0 }}>{checkpoint.summary}</p>}
              {details.length > 0
                ? <ul className="list">{details.map((d, i) => <li key={i}>{d}</li>)}</ul>
                : <p className="muted">No detail beyond the summary.</p>}
            </>
          )}

          {pane === 'performance' && (
            <dl className="kv">
              <dt>duration</dt><dd>{fmtMs(checkpoint.duration_ms)}</dd>
              <dt>share of run</dt><dd>{pct(row.duration_share)}</dd>
              <dt>started</dt><dd>{checkpoint.started_at ?? '—'}</dd>
              {metrics.map(([k, v]) => (
                <React.Fragment key={k}>
                  <dt>{k.replace(/_/g, ' ')}</dt><dd>{fmtInt(v)}</dd>
                </React.Fragment>
              ))}
            </dl>
          )}

          {pane === 'artifacts' && (
            artifacts.length > 0
              ? <div className="chips">
                  {artifacts.map((name) => <span key={name} className="chip">{name}</span>)}
                </div>
              : <p className="muted">This agent wrote no files.</p>
          )}
        </>
      )}
    </div>
  )
}

/* ------------------------------------------------------------- model views */

function boxState(node) {
  const missing = node.missing?.length ?? 0
  const total = (node.fields ?? node.dimensions ?? []).length
  if (!total) return 'ok'
  if (missing === 0) return 'ok'
  return missing === total ? 'missing' : 'partial'
}

function ModelView({ model }) {
  if (!model) return <div className="card muted">Reading the workbook…</div>
  return (
    <>
      <div className="card">
        <header><h2>Data model</h2>
          <span className="muted">student_id is the spine</span>
        </header>
        <div className="flow">
          {[['master'], ['enquiry', 'admission', 'fee'],
            ['current', 'receipts'], ['completed', 'not_coming'],
            ['certificate', 'churn'], ['reporting']].map((ids, i) => (
            <React.Fragment key={i}>
              {i > 0 && <span className="arrow">↓</span>}
              <div className="flow-row">
                {ids.map((id) => {
                  const node = model.data_model.find((n) => n.id === id)
                  if (!node) return null
                  return (
                    <div className="box" key={id} data-state={boxState(node)}>
                      <div className="t">{node.label}</div>
                      <div className="c">
                        {node.rows != null ? `${fmtInt(node.rows)} rows` : 'derived'}
                        {node.missing?.length > 0 && ` · missing ${node.missing.join(', ')}`}
                      </div>
                    </div>
                  )
                })}
              </div>
            </React.Fragment>
          ))}
        </div>
      </div>

      <div className="card">
        <header><h2>Executive dashboard</h2>
          <span className="muted">dimensions this upload can actually fill</span>
        </header>
        <div className="flow-row" style={{ alignItems: 'flex-start' }}>
          {model.dashboard.map((node) => (
            <div className="box" key={node.id} data-state={boxState(node)}
                 style={{ minWidth: 260 }}>
              <div className="t">{node.label}</div>
              <div className="chips" style={{ marginTop: 8 }}>
                {node.dimensions.map((d) => (
                  <span key={d.field} className={`chip ${d.present ? 'ok' : 'miss'}`}>
                    {d.field}{d.present ? ` ${fmtInt(d.filled)}` : ''}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
