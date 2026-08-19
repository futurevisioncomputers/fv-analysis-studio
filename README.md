# FV Analysis Studio

A web view of the `fv-analysis` pipeline: upload a workbook, watch each agent
work, read what it did and what it cost, open the report.

The analysis lives in the **fv-analysis plugin**, not here. This project drives
it and renders it. There is exactly one copy of the rules; two copies disagree
within a month and nobody can tell which produced a given report.

## Run it

Two processes, both local.

```bash
# backend — needs the plugin repo as a sibling, or set FV_ANALYSIS_PATH
cd backend
python -m uvicorn app.main:app --reload --port 8000

# frontend
cd frontend
npm install
npm run dev          # http://localhost:5173
```

The frontend proxies `/api` to port 8000, so the browser sees one origin and
SSE needs no CORS negotiation.

## What it does

**Upload → Dataset discovery.** Before any stage runs, the sheets are read and
every foreign key is tested. That is the first screen: sheets, row counts,
keys, and whether the keys resolve. A spinner tells you nothing; this tells you
whether the workbook can be joined at all.

The key test compares ids **as numbers** when the column is numeric. Real
institute sheets carry `student_id` as `697.0` on one tab and `697` on another;
compared as text they share nothing, and the workbook looks unlinkable when it
joins at 99%.

**Agent pipeline.** Twelve stages, live. Exactly one node moves at a time, and
motion is the only animation in the app, so movement always means "working".
The status is read from the session file, which the pipeline writes *before*
each agent starts — a crash leaves `running` behind, which is the truth.

**Agent report.** Per agent: the work report (its own summary and details),
the performance panel (duration, share of the run, what it produced), and its
artifacts. All of it comes from the pipeline's checkpoint contract — the studio
computes nothing.

**Data model.** The institute's own diagrams, filled in from the upload. A node
the data cannot fill is greyed and says what is missing, rather than rendering
an empty chart under a confident heading.

## Layout

```
backend/app
  config.py     where the pipeline is, where runs live, what may be served
  integrity.py  referential integrity of an upload
  model.py      the operator's diagrams, annotated with real coverage
  runner.py     background thread per run + SSE fan-out
  main.py       the API
frontend/src
  App.jsx       agent list, agent report, diagrams, report
  api.js        the API shape, in one place
  styles.css    one token set, light and dark
```

## Rules this project keeps

- **The session directory is passed explicitly.** No "current run" global —
  that is what makes hosting this later a configuration change, not a rewrite.
- **Raw uploads are never served.** They hold real student names and mobile
  numbers. Only masked artifacts (`cleaned.csv`, `report.html`) leave the
  process, and only by whitelist — never a directory listing.
- **Deleting a run deletes its data.** That is the whole retention policy for a
  local tool holding student PII.
- **Stages run on a worker thread**, never the event loop. One Data Engineer
  pass is ~750 ms of pandas and would freeze every open page.
- **Every SSE event restates a fact already on disk.** A client that misses the
  whole stream can reload and be correct.

## The operator's diagrams

Five were supplied, and they agree with each other:

| Diagram | Where it lives |
|---|---|
| App flow (upload → discovery → pipeline → report) | the three top-level tabs |
| Agent pipeline (13 agents) | the rail — 12 stages, since Statistical and Multifactor Analysis are both the Analyst's work rather than separate passes |
| Data model (student_id spine) | Data model tab |
| Executive dashboard (Enquiry / Admission / Finance) | Data model tab, lower |
| System architecture (Multi-Factor → LLM Insight → Management Report) | the pipeline itself: `multifactor.py`, `insights_agent.py`, `report_agent.py` |

## Status

Phase 1. Upload, discovery, live pipeline, agent reports, data model, and the
embedded report all work end to end against the real institute workbook.

Not built yet: charts and tables inside an agent report (the Visualization
stage already produces 14 charts — they are listed, not drawn), run history,
and comparison between runs.
