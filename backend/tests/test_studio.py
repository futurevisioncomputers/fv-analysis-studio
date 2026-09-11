"""End-to-end tests for the studio, over real HTTP against a running server.

No TestClient: the parts most likely to break are the parts a test client
fakes — the multipart upload, the SSE stream, the proxy path. Driving actual
HTTP tests what the browser will do.

Start the backend first:
    cd backend && python -m uvicorn app.main:app --port 8010

Run:
    cd backend && python -m tests.test_studio

8010, not 8000: a force-killed uvicorn on Windows can leave a LISTEN socket
owned by a dead PID, so the rebind fails with WinError 10048 on a port nothing
is serving. Several "failures" here were really a backend that never came up.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("FV_STUDIO_URL", "http://127.0.0.1:8010")

# The institute workbook. These tests assert against its real shape, because a
# fixture that joins perfectly would not have caught the float-vs-int bug that
# made 99% joins report as 0%. That rules out a synthetic fixture, so the file
# has to be found rather than built.
#
# Everything is resolved from the sibling checkouts — the assumption the backend
# already makes to import `agents` — rather than one machine's Downloads.
_WORKSPACE = Path(__file__).resolve().parents[3]
_CANDIDATES = (
    # Produced by the plugin's scripts/restructure_workbooks.py. Not in git:
    # `output/` is ignored, and the script needs exported CSVs that are not in
    # git either, so this exists only where someone has run the whole chain.
    _WORKSPACE / "fv-analysis-marketplace-main" / "output" / "FV_Students_v2.xlsx",
    # The shipped sample of the same shape: students (PK student_id) ->
    # fee_receipts, certificates. This is what is actually on disk.
    _WORKSPACE / "samples" / "FV_Students_v3_1.xlsx",
)


def _find_workbook() -> Path:
    override = os.environ.get("FV_TEST_WORKBOOK")
    if override:
        return Path(override)
    for candidate in _CANDIDATES:
        if candidate.exists():
            return candidate
    return _CANDIDATES[0]      # reported in the skip message below


WORKBOOK = _find_workbook()

# Why these tests are skipped, or None when they can run. Computed once so the
# custom runner and pytest give the same answer for the same reason.
def _blocker() -> "str | None":
    if not WORKBOOK.exists():
        listed = "\n    ".join(str(c) for c in _CANDIDATES)
        return (
            f"no institute workbook found. Looked in:\n    {listed}\n"
            "  Set FV_TEST_WORKBOOK to one, or generate the first with:\n"
            "    cd fv-analysis-marketplace-main && python scripts/restructure_workbooks.py --src <exported-csv-dir>"
        )
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=5).close()
    except Exception as exc:  # noqa: BLE001
        return (
            f"no backend at {BASE} ({exc}).\n"
            "  Start it with: cd backend && python -m uvicorn app.main:app --port 8010"
        )
    return None


# These tests drive a real server over real HTTP, so they need one running and a
# real workbook to upload. Under pytest that has to be declared, because pytest
# calls each `test_*` directly and never reaches the guard in `_run()` below —
# without this a missing workbook surfaced as twelve identical FileNotFoundError
# tracebacks that said nothing about what to do. Imported defensively so the
# stdlib-only `python -m tests.test_studio` path still works with no pytest.
_BLOCKER = _blocker()      # computed once: it makes a real request

try:
    import pytest as _pytest
except ImportError:                                     # pragma: no cover
    pass
else:
    pytestmark = _pytest.mark.skipif(_BLOCKER is not None, reason=_BLOCKER or "")

QUESTION = "Which branches and course categories drive revenue and completion?"

_created: list = []


# ------------------------------------------------------------------ helpers

def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as res:
        return json.load(res)


def _get_text(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=30) as res:
        return res.read().decode("utf-8", "replace")


def _post(path: str, payload=None):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.load(res)


def _upload(path: Path, question: str = "") -> dict:
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    write = lambda s: body.write(s if isinstance(s, bytes) else s.encode())
    write(f"--{boundary}\r\n")
    write(f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n')
    write("Content-Type: application/octet-stream\r\n\r\n")
    write(path.read_bytes())
    write(f"\r\n--{boundary}--\r\n")
    url = BASE + "/api/runs"
    if question:
        url += "?question=" + urllib.parse.quote(question)
    req = urllib.request.Request(
        url, data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as res:
        payload = json.load(res)
    _created.append(payload["run_id"])
    return payload


def _wait_idle(run_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _get(f"/api/runs/{run_id}")
        if not state["running"]:
            return state
        time.sleep(0.5)
    raise AssertionError("run never went idle")


def _status(path: str, payload=None, method="GET") -> int:
    """The status code of a request expected to fail."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        return 200
    except urllib.error.HTTPError as exc:
        return exc.code


# -------------------------------------------------------------------- tests

def test_health_reports_the_pipeline_it_will_run() -> None:
    """The studio must say WHICH copy of the analysis it drives.

    Two copies of the rules is how a report becomes unattributable — it
    already happened on this machine, where the installed plugin cache ran the
    old taxonomy rules while the repo had the new ones.
    """
    health = _get("/api/health")
    assert health["ok"] is True
    assert len(health["stages"]) == 12, health["stages"]
    assert Path(health["pipeline"]).exists(), health["pipeline"]


def test_the_rail_is_available_before_any_run_exists() -> None:
    """The UI draws the pipeline on the upload screen, with no run yet."""
    rail = _get("/api/rail")
    assert len(rail) == 12
    keys = [s["key"] for s in rail]
    assert keys[0] == "problem" and keys[-1] == "report"
    assert "clean" in dict(zip(keys, rail))["eda"]["requires"]
    assert any(s["optional"] for s in rail)


def test_a_non_data_file_is_refused_by_extension() -> None:
    """Before a byte is parsed, not after."""
    junk = Path(os.environ.get("TEMP", "/tmp")) / "fv-studio-test.txt"
    junk.write_text("not a workbook")
    try:
        _upload(junk)
        raise AssertionError("a .txt was accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400, exc.code
    finally:
        junk.unlink(missing_ok=True)


def test_upload_reports_referential_integrity_before_anything_runs() -> None:
    """The first screen is the joins, not a spinner.

    The resolve rates here are the regression test for the float-vs-int bug:
    `students.student_id` is float and `fee_receipts.student_id` is int, so a
    string comparison reports 0% on data that joins completely.
    """
    payload = _upload(WORKBOOK, QUESTION)
    report = payload["integrity"]
    assert report["ok"] is True, report["verdict"]
    assert report["verdict"] == "every foreign key resolves"

    sheets = {t["sheet"]: t for t in report["tables"]}
    assert "students" in sheets and sheets["students"]["primary_key"] == "student_id"
    assert "lookups" not in sheets, "a reference tab is not a data source"

    for link in report["links"]:
        assert link["resolve_rate"] == 1.0, link
        assert link["orphans"] == 0, link


def test_a_child_table_with_repeated_keys_still_resolves() -> None:
    """2,093 receipts for 1,528 students.

    The first version of this check compared DISTINCT ids against 0.9x the row
    count, so every child table failed the numeric test, fell back to string
    comparison, and reported 0% resolve with 1,528 orphans.
    """
    payload = _upload(WORKBOOK)
    links = {l["from_sheet"]: l for l in payload["integrity"]["links"]}
    receipts = links["fee_receipts"]
    assert receipts["distinct_keys"] < 2093, "keys repeat across receipt rows"
    assert receipts["resolve_rate"] == 1.0, receipts


def test_a_run_without_a_question_reports_what_the_data_supports() -> None:
    """A workbook and no question is the ordinary way this gets used.

    This used to stop at stage 1 asking what business problem to solve. There
    was no problem statement, but there was a workbook — and the questions are
    derived from its columns, so there was a real report to write. Asking first
    only blocked an operator who dropped a sheet in and pressed run.
    """
    run_id = _upload(WORKBOOK)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    state = _wait_idle(run_id)

    blocked = [r for r in state["progress"] if r["status"] == "blocked"]
    assert not blocked, blocked
    assert state["progress"][0]["status"] == "done", state["progress"][0]
    assert state["artifacts"].get("report.html") is True, state["artifacts"]


def test_the_question_is_answerable_without_re_uploading() -> None:
    """The workbook has not changed — only the question has.

    A run started with no question now completes on its own, so this is no
    longer about unblocking one: it is about re-scoping a finished run and
    having everything computed from the old brief thrown away.
    """
    run_id = _upload(WORKBOOK)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    _wait_idle(run_id)

    answer = _post(f"/api/runs/{run_id}/question", {"question": QUESTION})
    # Everything computed from the old brief goes, not just stage 1. When the
    # run blocked at `problem` there was nothing downstream to discard; now that
    # it completes, a new question invalidates all of it.
    assert answer["cleared"][0] == "problem", answer
    assert "analyst" in answer["cleared"], answer
    assert "report" in answer["cleared"], answer

    _post(f"/api/runs/{run_id}/start", {"auto": True})
    state = _wait_idle(run_id)
    assert state["progress"][0]["status"] == "done"
    assert state["artifacts"].get("report.html") is True

    # An empty question is not an answer.
    assert _status(f"/api/runs/{run_id}/question", {"question": "  "},
                   "POST") == 400


def test_a_full_run_times_every_stage_and_writes_the_report() -> None:
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    state = _wait_idle(run_id)

    ran = [r for r in state["progress"] if r["status"] == "done"]
    assert len(ran) >= 10, [r["key"] for r in ran]
    for row in ran:
        assert row["duration_ms"] is not None, row["key"]
        assert row["duration_share"] is not None, row["key"]
    assert abs(sum(r["duration_share"] for r in ran) - 1.0) < 0.01

    assert state["artifacts"].get("report.html") is True
    assert state["artifacts"].get("cleaned.csv") is True


def test_every_checkpoint_carries_a_work_report_and_a_performance_report() -> None:
    """Both panels of the agent card come from the pipeline, not the studio."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    _wait_idle(run_id)

    clean = _get(f"/api/runs/{run_id}/checkpoint/clean")
    assert clean["status"] == "done"
    assert clean["summary"]
    assert clean["details"], "the work report"
    assert clean["duration_ms"] > 0, "the performance report"
    assert clean["metrics"]["rows_out"] > 0
    assert clean["metrics"]["rows_in"] >= clean["metrics"]["rows_out"]


def test_the_operators_diagrams_are_annotated_with_real_coverage() -> None:
    """A node the data cannot fill says so, instead of drawing an empty chart."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    model = _get(f"/api/runs/{run_id}/model")

    nodes = {n["id"]: n for n in model["data_model"]}
    assert nodes["master"]["rows"] == 1536
    assert nodes["receipts"]["rows"] == 2093
    # The enquiry sheet is in the OTHER workbook, so this node is honestly empty.
    assert nodes["enquiry"]["rows"] is None
    assert nodes["enquiry"]["missing"], "an absent sheet must be marked"

    dash = {n["id"]: n for n in model["dashboard"]}
    admission = dash["admission"]
    present = {d["field"] for d in admission["dimensions"] if d["present"]}
    assert {"course", "branch", "faculty"} <= present
    # Nothing in either workbook carries these, and the UI must not pretend.
    assert {"age", "area", "pincode"} <= set(admission["missing"])


def test_the_report_model_serves_the_numbers_the_report_prints() -> None:
    """The computed values reach HTTP, and they are the report's own.

    Two renderings of one run that each do their own arithmetic will disagree
    eventually, and nobody will be able to say which is wrong. So the model is
    checked against the report it must agree with, not against constants.
    """
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    _wait_idle(run_id)

    model = _get(f"/api/runs/{run_id}/report-model")
    assert model["counts"]["questions_answered"] >= 1
    assert model["charts"], "visualize computed specs; they have to ship"
    assert len(model["charts"]) == model["counts"]["charts"]

    # Chart ids repeat across questions — a dozen questions each have a
    # `chart_6` — so anything keying on `id` keeps only the last of each name.
    uids = [c["uid"] for c in model["charts"]]
    assert len(set(uids)) == len(uids), "chart uids must be unique per run"
    assert set(model["trends"]) <= set(uids), "a trend must name a real chart"

    # `table_fallback` is what a charting library reads and what the report's
    # own SVG draws from. `chartjs` targets a library this UI does not use.
    assert any(c.get("table_fallback") for c in model["charts"])
    assert all("chartjs" not in c for c in model["charts"])

    report = _get_text(f"/api/runs/{run_id}/artifact/report.html")
    for card in model["kpis"]:
        assert str(card["value"]) in report, (card["metric"], card["value"])


def test_the_report_model_names_what_it_cannot_draw() -> None:
    """Every tab is either usable or says which column it lacks."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    _wait_idle(run_id)

    coverage = _get(f"/api/runs/{run_id}/report-model")["coverage"]

    # Nothing in either workbook carries a planned duration, a disciplinary
    # record or a timetable, and these three tabs must admit that on screen.
    for tab in ("course_pacing", "disciplinary", "faculty_timetable"):
        assert coverage[tab]["status"] == "unsupported", tab
        assert coverage[tab]["reason"], f"{tab} has to say why"

    for tab, row in coverage.items():
        assert row["status"] in {"ok", "partial", "unsupported"}, tab
        if row["status"] != "ok":
            assert row["reason"], f"{tab} is not ok and gives no reason"


def test_the_report_model_carries_no_student_identity() -> None:
    """This payload goes to a browser; the upload's names and mobiles do not.

    The masking happens upstream, which is exactly why it is worth asserting
    here: a future section added to the model could reach past it.
    """
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    _wait_idle(run_id)

    blob = json.dumps(_get(f"/api/runs/{run_id}/report-model"))
    assert not re.search(r"\b[6-9]\d{9}\b", blob), "an Indian mobile number"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]{2,}", blob), "an email address"


def test_only_masked_artifacts_are_servable() -> None:
    """The upload holds real names and mobile numbers and never leaves."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    _wait_idle(run_id)

    assert _status(f"/api/runs/{run_id}/artifact/report.html") == 200
    for forbidden in ("canonical.parquet", "session.json", "FV_Students_v2.xlsx"):
        assert _status(f"/api/runs/{run_id}/artifact/{forbidden}") == 403, forbidden


def test_a_run_id_cannot_climb_out_of_the_runs_directory() -> None:
    assert _status("/api/runs/..%2f..%2fetc") in (400, 404)
    assert _status("/api/runs/does-not-exist") == 404


def test_two_runs_of_the_same_workbook_do_not_share_state() -> None:
    """Session-per-run is what makes hosting this later a config change."""
    first = _upload(WORKBOOK, QUESTION)["run_id"]
    second = _upload(WORKBOOK, "How much fee is pending by branch?")["run_id"]
    assert first != second
    _post(f"/api/runs/{first}/start", {"auto": True})
    _wait_idle(first)

    other = _get(f"/api/runs/{second}")
    assert all(r["status"] == "pending" for r in other["progress"]), \
        "running one run advanced another"


def test_the_event_stream_reports_each_stage_as_it_happens() -> None:
    """The rail moves off SSE; without it the UI is a spinner with extra steps."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    seen: list = []

    def listen() -> None:
        try:
            with urllib.request.urlopen(
                    f"{BASE}/api/runs/{run_id}/events", timeout=120) as stream:
                for raw in stream:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    seen.append(event)
                    if event["type"] == "run_finished":
                        return
        except Exception:  # noqa: BLE001 - the assertions below are the check
            pass

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(0.5)
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    thread.join(timeout=120)

    kinds = [e["type"] for e in seen]
    assert "stage_started" in kinds, kinds[:5]
    assert "stage_done" in kinds, kinds[:5]
    assert kinds[-1] == "run_finished", kinds[-3:]
    done = [e for e in seen if e["type"] == "stage_done"]
    assert any(e["stage"] == "report" for e in done)
    assert all(e["duration_ms"] is not None for e in done)


def test_a_second_start_on_a_working_run_is_refused() -> None:
    """Two threads writing one session file would interleave stage records."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    _post(f"/api/runs/{run_id}/start", {"auto": True})
    code = _status(f"/api/runs/{run_id}/start", {"auto": True}, "POST")
    _wait_idle(run_id)
    assert code == 409, code


def test_deleting_a_run_removes_the_uploaded_data() -> None:
    """That is the entire retention policy for a local tool holding PII."""
    run_id = _upload(WORKBOOK, QUESTION)["run_id"]
    assert _status(f"/api/runs/{run_id}") == 200
    _post_delete = urllib.request.Request(f"{BASE}/api/runs/{run_id}",
                                          method="DELETE")
    with urllib.request.urlopen(_post_delete, timeout=30) as res:
        assert json.load(res)["deleted"] == run_id
    assert _status(f"/api/runs/{run_id}") == 404
    if run_id in _created:
        _created.remove(run_id)


def _cleanup() -> None:
    for run_id in list(_created):
        try:
            req = urllib.request.Request(f"{BASE}/api/runs/{run_id}",
                                         method="DELETE")
            urllib.request.urlopen(req, timeout=30)
        except Exception:  # noqa: BLE001
            pass


def _run() -> int:
    # Re-checked rather than reusing _BLOCKER: the module may have been imported
    # before the server was started.
    blocker = _blocker()
    if blocker:
        print(f"SKIP all: {blocker}")
        return 0
    print(f"workbook: {WORKBOOK}")

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    _cleanup()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
