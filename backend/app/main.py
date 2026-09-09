"""FastAPI app: create a run, watch the agents work, read the report.

Design rules, both learned the hard way in this project:

* The session directory is passed explicitly everywhere. There is no "current
  run" global — that is what makes hosting this later a configuration change
  rather than a rewrite.
* Raw uploads are never served. They hold real student names and mobile
  numbers; only masked artifacts leave this process.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import (ALLOWED_SUFFIXES, PIPELINE_PATH, REFERENCE_SHEETS,
                     RUNS_DIR, SERVABLE_ARTIFACTS, ensure_pipeline_importable)
from .integrity import inspect, read_sheets
from .model import annotate
from .runner import BUS, RUNNER, auto_stage_keys

ensure_pipeline_importable()

from agents import stages as pipeline          # noqa: E402
from agents.session import Session             # noqa: E402

JsonDict = Dict[str, Any]

app = FastAPI(title="FV Analysis Studio", version="0.1.0")

# The React dev server runs on another port. Local-only, so this is permissive
# on purpose; a hosted deployment must narrow it to the real origin.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"])


@app.on_event("startup")
async def _bind_loop() -> None:
    BUS.bind_loop(asyncio.get_running_loop())
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ helpers

def _run_dir(run_id: str) -> Path:
    path = (RUNS_DIR / run_id).resolve()
    # A run id arrives from the client; refuse anything that climbs out.
    if RUNS_DIR not in path.parents and path != RUNS_DIR:
        raise HTTPException(400, "bad run id")
    if not path.exists():
        raise HTTPException(404, f"no run {run_id}")
    return path


def _session_dir(run_id: str) -> Path:
    return _run_dir(run_id) / "session"


def _load(run_id: str) -> Session:
    return Session.load(str(_session_dir(run_id)))


def _state(run_id: str) -> JsonDict:
    # Sampled BEFORE the session is read, and the order is the whole point.
    # Reading disk first and asking the runner second lets a worker start,
    # write, and finish in between — so the answer pairs "not running" with a
    # snapshot taken before any stage ran, and a caller waiting for the run to
    # go idle gets back a run where everything is still `pending`. Sampling
    # first can only err towards "still working", which costs one more poll.
    thread_running = RUNNER.is_running(run_id)
    session = _load(run_id)
    meta_path = _run_dir(run_id) / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    progress = session.progress()
    # True if EITHER the worker is alive or a stage is marked running on disk.
    # The worker alone leaves a gap between "start accepted" and "thread
    # scheduled"; the disk alone would stay stale if a worker died mid-stage.
    working = thread_running or any(
        row["status"] == "running" for row in progress)
    return {
        "run_id": run_id,
        "question": session.state.get("question", ""),
        "sources": [s.get("name") for s in session.sources()],
        "progress": progress,
        "next_stage": session.next_stage(),
        "running": working,
        "artifacts": {name: bool(path) for name, path
                      in session.state["artifacts"].items()},
        "errors": session.state.get("errors", []),
        "integrity": meta.get("integrity"),
        "created_at": meta.get("created_at"),
    }


# -------------------------------------------------------------------- routes

@app.get("/api/health")
async def health() -> JsonDict:
    return {"ok": True, "pipeline": str(PIPELINE_PATH),
            "stages": [s["key"] for s in _rail()]}


def _rail() -> List[JsonDict]:
    from agents.session import STAGES, OPTIONAL_STAGES
    return [{"key": s["key"], "n": s["n"], "label": s["label"],
             "requires": list(s["requires"]),
             "optional": s["key"] in OPTIONAL_STAGES} for s in STAGES]


@app.get("/api/rail")
async def rail() -> List[JsonDict]:
    """The pipeline shape, before any run exists — so the UI can draw it."""
    return _rail()


@app.post("/api/runs")
async def create_run(file: List[UploadFile] = File(...),
                     question: str = "") -> JsonDict:
    """Start a run from one or more uploaded workbooks.

    Several files make ONE run, not one run each. The institute keeps its
    admissions in one workbook and its enquiries in another, joined on ENQ_ID.
    Analysed separately, the admissions file reports a 100% conversion rate —
    true and useless, because every row in it is already an admission. The
    enquiries that never converted are the denominator, and they live in the
    other file.
    """
    uploaded = [f for f in file if f and f.filename]
    if not uploaded:
        raise HTTPException(400, "no file was uploaded")

    for item in uploaded:
        suffix = Path(item.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                400, f"{item.filename}: {suffix or 'that'} is not a data file "
                     f"— upload {', '.join(sorted(ALLOWED_SUFFIXES))}")

    run_id = uuid.uuid4().hex[:12]
    run_dir = RUNS_DIR / run_id
    uploads = run_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    targets: List[Path] = []
    for item in uploaded:
        target = uploads / Path(item.filename).name
        with target.open("wb") as out:
            shutil.copyfileobj(item.file, out)
        targets.append(target)

    # The integrity report IS the first screen. Computed before any stage so
    # the operator sees whether the keys resolve before watching a pipeline
    # run on data that cannot join. With several files it also answers the
    # question that decides whether uploading them together was worth it:
    # do the keys resolve *across* the files?
    try:
        report = inspect(targets)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(run_dir, ignore_errors=True)
        raise HTTPException(400, f"could not read that file: {exc}")

    sources: List[JsonDict] = []
    for target in targets:
        sources.extend(_sources_for(target))
    _disambiguate(sources)

    session = Session.create(str(run_dir / "session"))
    session.state["data_sources"] = sources
    if question:
        session.state["question"] = question
        from scripts.run_pipeline import wrap_goal
        session.state["goal"] = wrap_goal(question)
    session.save()

    (run_dir / "run.json").write_text(json.dumps({
        "run_id": run_id,
        "file": ", ".join(t.name for t in targets),
        "files": [t.name for t in targets],
        "created_at": session.state["created_at"], "integrity": report,
    }, indent=2, default=str))
    return {"run_id": run_id, "integrity": report}


def _disambiguate(sources: List[JsonDict]) -> None:
    """Qualify only the source names that two uploads both use.

    Downstream every source is addressed by name, so a duplicate silently
    shadows the earlier one. Renaming unconditionally would be worse: the
    reports would read `FV_Students_v3_1__students` where `students` is what
    the operator calls it.
    """
    counts: Dict[str, int] = {}
    for s in sources:
        counts[s["name"]] = counts.get(s["name"], 0) + 1
    for s in sources:
        if counts[s["name"]] > 1:
            s["name"] = f"{Path(s['path']).stem}__{s['name']}"


def _sources_for(path: Path) -> List[JsonDict]:
    """Sheets as pipeline sources, reference tabs excluded.

    The institute's workbook carries a `lookups` tab feeding the dropdowns.
    Ingested as a source it produced a "dropped 8/8 rows" quality note on
    every run — noise that reads like a data problem and is not one.
    """
    if path.suffix.lower() == ".csv":
        return [{"name": path.stem, "type": "csv", "path": str(path),
                 "path_or_query": str(path)}]
    out: List[JsonDict] = []
    for name in read_sheets(path):
        out.append({"name": name, "type": "excel_sheet", "path": str(path),
                    "path_or_query": str(path), "sheet_name": name})
    return out


@app.get("/api/runs")
async def list_runs(limit: int = 40) -> List[JsonDict]:
    """Past runs, newest first, for the history list.

    Ordering used to come from `sorted(glob(...), reverse=True)`, which sorts by
    path — and the path carries a random hex run id, not a date. The "latest"
    run was whichever id happened to sort highest, so the list was shuffled with
    respect to time. It is sorted on `created_at` now.

    Only cheap facts are read. `run.json` is small; the session file that knows
    each stage's status is not, and reading a hundred of those to render one
    list would make opening the app slow. Whether the report exists is a stat
    call, which is enough for the list to say what is finished.
    """
    out: List[JsonDict] = []
    for path in RUNS_DIR.glob("*/run.json"):
        try:
            meta = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue                      # a half-written or deleted run
        files = meta.get("files") or [meta.get("file")]
        out.append({
            "run_id": meta["run_id"],
            "file": meta.get("file"),
            "files": [f for f in files if f],
            "created_at": meta.get("created_at"),
            "sheets": len((meta.get("integrity") or {}).get("tables") or []),
            "rows": (meta.get("integrity") or {}).get("total_rows"),
            "has_report": (path.parent / "session" / "artifacts"
                           / "report.html").exists(),
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:max(limit, 1)]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> JsonDict:
    return _state(run_id)


@app.get("/api/runs/{run_id}/model")
async def data_model(run_id: str) -> JsonDict:
    """The operator's own two diagrams, filled in from this upload.

    Nodes the data cannot fill come back marked `missing` so the UI can grey
    them rather than drawing an empty chart under a confident heading.
    """
    meta_path = _run_dir(run_id) / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    upload = _run_dir(run_id) / "uploads" / str(meta.get("file") or "")
    if not upload.exists():
        raise HTTPException(404, "the upload for this run is gone")
    return annotate(read_sheets(upload))


@app.get("/api/runs/{run_id}/checkpoint/{stage}")
async def get_checkpoint(run_id: str, stage: str) -> JsonDict:
    session = _load(run_id)
    try:
        return pipeline.checkpoint(session, stage)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, str(exc))


class QuestionBody(BaseModel):
    question: str
    modules: Optional[List[str]] = None


@app.get("/api/modules")
async def modules() -> List[JsonDict]:
    """The fixed module set, so the UI can offer scope as a choice.

    Stage 1 asks "all modules, or only selected?" when the question names none.
    Answering that by hoping the free text trips a keyword is a guessing game;
    naming the modules outright ends it.
    """
    from agents.problem_definition_agent import MODULE_DEFINITIONS
    return [{"key": key, "metrics": list(spec.get("metrics", []))[:4]}
            for key, spec in MODULE_DEFINITIONS.items()]


@app.post("/api/runs/{run_id}/question")
async def set_question(run_id: str, body: QuestionBody) -> JsonDict:
    """Give a run its question and clear what was computed without one.

    Problem Definition refuses to invent a goal, so a run started with no
    question stops at stage 1 asking for one. Re-uploading the workbook to
    answer that would be absurd — the file has not changed, only the question
    has. Everything downstream of `problem` is discarded because it was
    computed from a different brief.

    `modules`, when given, sets the scope explicitly. Stage 1 treats an explicit
    module map as authoritative and stops asking — which is the only reliable
    way out of the scope question, since a free-text answer only helps if it
    happens to contain a module keyword.
    """
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "a question is what this endpoint is for")
    session = _load(run_id)
    session.state["question"] = question
    from scripts.run_pipeline import wrap_goal
    goal = wrap_goal(question)
    if body.modules is not None:
        from agents.problem_definition_agent import MODULE_DEFINITIONS
        unknown = [m for m in body.modules if m not in MODULE_DEFINITIONS]
        if unknown:
            raise HTTPException(400, f"no such module: {', '.join(unknown)}")
        if not body.modules:
            raise HTTPException(400, "at least one module has to be in scope")
        goal["goal"]["modules"] = {
            name: {"enabled": name in body.modules}
            for name in MODULE_DEFINITIONS
        }
    session.state["goal"] = goal
    cleared = session.reset_from("problem")
    session.save()
    return {"question": question, "modules": body.modules, "cleared": cleared}


class StartBody(BaseModel):
    stages: Optional[List[str]] = None
    auto: bool = False


@app.post("/api/runs/{run_id}/start")
async def start(run_id: str, body: StartBody) -> JsonDict:
    keys = auto_stage_keys() if body.auto else (body.stages or [])
    if not keys:
        raise HTTPException(400, "name at least one stage, or pass auto")
    if not RUNNER.start(run_id, _session_dir(run_id), keys):
        raise HTTPException(409, "this run is already working")
    return {"started": keys}


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str) -> StreamingResponse:
    """Server-sent events. Every event restates a fact already on disk, so a
    client that misses the stream can reload and be correct."""
    _run_dir(run_id)
    queue = BUS.subscribe(run_id)

    async def stream():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # some proxies close idle streams
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
        finally:
            BUS.unsubscribe(run_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/artifact/{name}")
async def artifact(run_id: str, name: str):
    if name not in SERVABLE_ARTIFACTS:
        # Deliberately not a directory listing: the raw upload holds unmasked
        # names and mobile numbers and must never leave through this API.
        raise HTTPException(403, f"{name} is not servable")
    path = _load(run_id).get_artifact(name)
    if not path or not Path(path).exists():
        raise HTTPException(404, f"{name} has not been written yet")
    return FileResponse(path)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> JsonDict:
    """Deleting the run deletes the uploaded PII. That is the retention policy.

    So the deletion is VERIFIED, not assumed. `ignore_errors=True` on its own
    silently leaves files behind when Windows still holds a handle — a parquet
    pandas just read, a report.html a browser is still fetching — and the API
    would report success while the uploaded workbook, with real names and
    mobile numbers in it, stayed on disk. A retention policy that reports
    success without checking is not a retention policy.
    """
    if RUNNER.is_running(run_id):
        raise HTTPException(409, "still working — stop it before deleting")
    path = _run_dir(run_id)

    def drop_readonly(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    # pyarrow memory-maps a parquet it has read, and the mapping outlives the
    # DataFrame until the object is collected. On Windows that mapping is an
    # open handle and the file cannot be unlinked, so collect first rather
    # than retry against a handle nothing is going to release.
    gc.collect()

    for attempt in range(8):
        shutil.rmtree(path, onerror=drop_readonly)
        if not _files_left(path):
            return {"deleted": run_id}
        time.sleep(0.25 * (attempt + 1))   # a handle being released
        gc.collect()

    remaining = _files_left(path)
    raise HTTPException(
        500,
        f"could not delete this run — {len(remaining)} file(s) are still held "
        f"by another process: {', '.join(remaining[:5])}. The data is still on "
        f"disk; close anything reading it and try again."
    )


def _files_left(path: Path) -> List[str]:
    """What is still on disk under `path` — by ENUMERATION, not `exists()`.

    Windows puts a directory whose child still has an open handle into a
    "delete pending" state: `Path.exists()` reports False while the child file
    is still there and still readable. Verifying a PII deletion with `exists()`
    therefore reports success on exactly the case that failed. Enumeration sees
    the entries that are really left.
    """
    try:
        return [str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()]
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
