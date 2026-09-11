"""One run's numbers, reshaped for a UI that draws charts.

The pipeline already computes everything a report needs: 96 chart specs, 12 KPI
cards, breakdowns by faculty and branch and course, two conversion rates, a
churn census. None of it reaches HTTP. `/checkpoint/{stage}` returns prose
capped at eight entries and `/runs/{id}` returns scalars, so a view that wanted
a real bar chart had no way to ask for one and drew a made-up constant instead.

This module reshapes and computes nothing. Every number is read out of the
session and passed through untouched. That is the whole design rule, not
laziness: the moment this file averages something itself, the React view and
`report.html` can disagree about the same run and nobody can tell which one is
lying.

Two consequences worth naming:

* `chartjs` is dropped on the way out. Each chart already carries
  `table_fallback`, a flat row list, which is both the shape a charting library
  wants and the exact array the report's own SVG renders from. Shipping only
  that makes the two views structurally incapable of disagreeing.
* A tab the data cannot fill is listed in `coverage` with the reason, instead of
  being handed an empty array to draw. Same rule as `model.py`: an empty chart
  under a confident heading is worse than an honest gap, because only one of
  those two ever gets fixed.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

JsonDict = Dict[str, Any]

# Fields stripped from every chart before it leaves this process.
#
# `chartjs` is a rendering config for a library this UI does not use, and it is
# the larger half of the payload. `png_path` points inside the run directory; a
# path is not a URL and handing one to a browser only invites someone to try to
# fetch it.
CHART_DROP = ("chartjs", "png_path")

# What each report tab needs, and the column that would have to exist for the
# three the pipeline cannot feed at all. These three are not bugs to fix later:
# nothing in either workbook carries the field, so the honest answer on screen
# is the column name, not a chart.
UNSUPPORTED: Dict[str, str] = {
    "course_pacing": "Course Duration (IN DAYS) is absent from every sheet, so "
                     "pace against a planned duration cannot be computed.",
    "disciplinary": "No disciplinary or attendance-incident column exists in "
                    "the uploaded data.",
    "faculty_timetable": "No timetable, slot or class-schedule column exists in "
                         "the uploaded data.",
}


# ------------------------------------------------------------------ accessors
#
# Each reads one place in the session and tolerates its absence. A run halfway
# through the pipeline has real answers for the stages that finished and nothing
# at all for the ones that have not started, and asking for its model should
# return the former rather than raising on the latter.

def _stage(state: JsonDict, name: str) -> JsonDict:
    stage = (state.get("stages") or {}).get(name) or {}
    return stage.get("result") or {}


def _answered(state: JsonDict) -> List[JsonDict]:
    return list(_stage(state, "analyst").get("answered") or [])


def _by_question(state: JsonDict) -> Dict[str, JsonDict]:
    return dict(_stage(state, "visualize").get("by_question") or {})


# -------------------------------------------------------------------- sections

def _metrics(state: JsonDict) -> Dict[str, JsonDict]:
    """Every headline number, keyed by metric name.

    Keyed by metric rather than grouped into fixed `finance` / `admissions`
    blocks, because the metric set follows the uploaded columns. A workbook
    without fees answers no fee questions, and a hardcoded `finance` key would
    then be an empty object pretending to be a section.
    """
    out: Dict[str, JsonDict] = {}
    for row in _answered(state):
        head = (row.get("analysis") or {}).get("headline_number") or {}
        metric = head.get("metric")
        if not metric:
            continue
        out[str(metric)] = {
            "metric": metric,
            "value": head.get("value"),
            "n": head.get("n"),
            "ci_95": head.get("ci_95"),
            "module": row.get("module"),
            "question_id": row.get("question_id"),
            "question": row.get("question"),
        }
    return out


def _modules(state: JsonDict) -> Dict[str, List[str]]:
    """Module -> the metrics it answered, so a tab can ask for its own slice."""
    out: Dict[str, List[str]] = {}
    for spec in _metrics(state).values():
        module = str(spec.get("module") or "other")
        out.setdefault(module, []).append(str(spec["metric"]))
    return out


def _kpis(state: JsonDict) -> List[JsonDict]:
    """The pre-formatted cards, already carrying their own display strings."""
    out: List[JsonDict] = []
    for question_id, block in _by_question(state).items():
        for card in block.get("kpi_cards") or []:
            out.append({**card, "question_id": block.get("question_id")
                        or question_id})
    return out


def _charts(state: JsonDict) -> List[JsonDict]:
    """Every chart spec, each given an id that is unique across the run.

    The pipeline numbers charts within a question, so twelve questions produce
    twelve charts called `chart_6`. That is fine inside one report section and
    useless as a key: anything indexing this list by `id` silently keeps the
    last one. `uid` is the question id and the chart id together, which is
    unique for all 96 and stable across re-reads of the same run.
    """
    out: List[JsonDict] = []
    for question_id, block in _by_question(state).items():
        qid = block.get("question_id") or question_id
        for chart in block.get("charts") or []:
            slim = {k: v for k, v in chart.items() if k not in CHART_DROP}
            slim["question_id"] = qid
            slim["uid"] = f"{qid}:{chart.get('id')}"
            out.append(slim)
    return out


def _breakdowns(state: JsonDict) -> Dict[str, List[JsonDict]]:
    """Segment rows grouped by the dimension they cut on.

    Each row keeps the metric it measures. A branch row from the conversion
    question and a branch row from the default-rate question are both "branch",
    and a tab that lost track of which was which would stack percentages of
    different things in one bar.
    """
    out: Dict[str, List[JsonDict]] = {}
    for row in _answered(state):
        analysis = row.get("analysis") or {}
        metric = (analysis.get("headline_number") or {}).get("metric")
        for cut in analysis.get("breakdowns") or []:
            dimension = str(cut.get("dimension") or "other")
            out.setdefault(dimension, []).append({
                **cut,
                "metric": metric,
                "question_id": row.get("question_id"),
            })
    return out


def _conversion(state: JsonDict) -> JsonDict:
    """Both conversion rates, each with the population it was measured over.

    This run holds two, and they are not a contradiction: 29.1% counts distinct
    people who enquired and later appear as admitted, while 54.9% is the rate
    within the students sheet alone. Reported side by side with their `basis`
    they are two facts. Reported as one number labelled "conversion" they are a
    trap, so neither is promoted here — `funnel_default` stays null until
    somebody who knows the business decides which one the funnel means.
    """
    enquiry = _stage(state, "clean").get("enquiry_conversion") or {}
    within = _metrics(state).get("admission_conversion_rate") or {}
    rates: List[JsonDict] = []
    if enquiry:
        rates.append({
            "key": "enquiry_conversion",
            "label": "Enquiry to admission",
            "value": enquiry.get("conversion_rate"),
            "numerator": enquiry.get("converted_persons"),
            "denominator": enquiry.get("enquired_persons"),
            "basis": "distinct people who enquired, matched on the salted "
                     "person id",
            # Zero here means no conversion was matched ACROSS the two
            # workbooks: every one was found inside the enquiry sheet itself.
            # It is the single most load-bearing caveat on this number.
            "cross_source_conversions": enquiry.get("cross_source_conversions"),
        })
    if within:
        rates.append({
            "key": "admission_conversion_rate",
            "label": "Admission conversion",
            "value": within.get("value"),
            "numerator": None,
            "denominator": within.get("n"),
            "basis": "rows within the students sheet",
            "ci_95": within.get("ci_95"),
            "question_id": within.get("question_id"),
        })
    return {"rates": rates, "funnel_default": None}


def _churn(state: JsonDict) -> JsonDict:
    summary = _stage(state, "clean").get("churn_summary") or {}
    if not summary:
        return {}
    return {
        "as_of": summary.get("as_of"),
        "as_of_source": summary.get("as_of_source"),
        "grace_months": summary.get("grace_months"),
        "counts": summary.get("counts") or {},
        "labelled_rows": summary.get("labelled_rows"),
        "unlabelled_rows": summary.get("unlabelled_rows"),
        "at_risk_rows": summary.get("at_risk_rows"),
        "churn_rate_of_at_risk": summary.get("churn_rate_of_at_risk"),
        "notes": list(summary.get("notes") or []),
    }


def _quality(state: JsonDict) -> JsonDict:
    clean = _stage(state, "clean")
    report = clean.get("quality_report") or {}
    return {
        "original_row_count": report.get("original_row_count"),
        "row_count": clean.get("row_count"),
        "drop_count": report.get("drop_count"),
        "dropped_reasons": report.get("dropped_reasons") or {},
        "deduplication_keys": list(report.get("deduplication_keys") or []),
        "known_issues": list(report.get("known_issues") or []),
    }


def _relationships(state: JsonDict, final: JsonDict) -> JsonDict:
    rel = final.get("relationships") or {}
    return {
        "master_source": rel.get("master_source"),
        "accepted": list(rel.get("accepted") or []),
        "rejected": list(rel.get("rejected") or []),
        "joined_sources": list(rel.get("joined_sources") or []),
        "unjoined_sources": list(rel.get("unjoined_sources") or []),
    }


def _trends(charts: Iterable[JsonDict]) -> List[str]:
    """Ids of the time-ordered charts, so the trends tab can find its own.

    `uid`s rather than copies. Sending the objects again cost a seventh of the
    payload and, worse, put two copies of one chart in a document where a
    consumer could update the wrong one. The client indexes `charts` by `uid`.

    Naming them here also keeps the rule that "line means time" on the side that
    knows it. A tab filtering on `type == "line"` would be guessing.
    """
    return [str(c["uid"]) for c in charts
            if c.get("type") == "line" and c.get("uid")]


def _integrity(meta: JsonDict) -> JsonDict:
    block = meta.get("integrity") or {}
    return {
        "ok": block.get("ok"),
        "verdict": block.get("verdict"),
        "files": list(block.get("files") or []),
        "total_rows": block.get("total_rows"),
        "cross_file_links": block.get("cross_file_links"),
        "tables": list(block.get("tables") or []),
        "links": list(block.get("links") or []),
    }


# ------------------------------------------------------------------- coverage

def _coverage(state: JsonDict, model: JsonDict) -> Dict[str, JsonDict]:
    """What each tab can honestly draw from this particular run.

    Read from the assembled model rather than declared, so a workbook that
    answers fewer questions downgrades its own tabs instead of leaving a tab to
    discover at render time that its array is empty.
    """
    breakdowns = model["breakdowns"]
    metrics = model["metrics"]
    churn = model["churn"]

    def rule(ready: bool, reason: str, partial: bool = False) -> JsonDict:
        if ready and not partial:
            return {"status": "ok", "reason": ""}
        if ready and partial:
            return {"status": "partial", "reason": reason}
        return {"status": "unsupported", "reason": reason}

    out: Dict[str, JsonDict] = {
        "overview": rule(bool(model["kpis"]),
                         "No stage produced a KPI card for this run."),
        "admissions_funnel": rule(
            bool(model["conversion"]["rates"]),
            "Neither conversion rate could be computed from this data."),
        "financials": rule(
            any(spec.get("module") == "fee_management"
                for spec in metrics.values()),
            "No fee column in this upload, so no financial metric exists."),
        "course_performance": rule(
            bool(breakdowns.get("course_category")),
            "No course dimension survived cleaning."),
        "branch_analysis": rule(bool(breakdowns.get("branch")),
                                "No branch dimension survived cleaning."),
        "faculty_analysis": rule(bool(breakdowns.get("faculty")),
                                 "No faculty dimension survived cleaning."),
        "seasonal_trends": rule(bool(model["trends"]),
                                "No date column supported a time series."),
        "data_cleaning": rule(bool(model["quality"].get("row_count")),
                              "The cleaning stage has not run yet."),
    }

    # Churn is the one genuinely partial tab: the census exists, but a labelled
    # churn rate does not, and the summary says why in its own words.
    labelled = churn.get("labelled_rows")
    out["attendance_churn"] = rule(
        bool(churn.get("counts")),
        (churn.get("notes") or ["No churn label could be produced."])[0],
        partial=not labelled,
    )

    for tab, reason in UNSUPPORTED.items():
        out[tab] = {"status": "unsupported", "reason": reason}
    return out


# ----------------------------------------------------------------------- build

def build(state: JsonDict, meta: Optional[JsonDict] = None) -> JsonDict:
    """Assemble the model for one run. Pure reshaping — no arithmetic."""
    meta = meta or {}
    final = state.get("final_report") or {}
    charts = _charts(state)

    model: JsonDict = {
        "run_id": meta.get("run_id"),
        "created_at": meta.get("created_at"),
        "question": state.get("question") or "",
        "integrity": _integrity(meta),
        "kpis": _kpis(state),
        "metrics": _metrics(state),
        "modules": _modules(state),
        "breakdowns": _breakdowns(state),
        "charts": charts,
        "trends": _trends(charts),
        "conversion": _conversion(state),
        "churn": _churn(state),
        "quality": _quality(state),
        "relationships": _relationships(state, final),
        "findings": list(final.get("headline_findings") or []),
        "recommendations": list(final.get("top_recommendations") or []),
        "skipped": list(final.get("skipped")
                        or _stage(state, "analyst").get("skipped") or []),
        "counts": {
            "questions_answered": final.get("questions_answered"),
            "questions_skipped": final.get("questions_skipped"),
            "charts": len(charts),
        },
    }
    model["coverage"] = _coverage(state, model)
    return model
