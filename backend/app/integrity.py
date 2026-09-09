"""Referential integrity of an uploaded workbook, before anything runs.

The first screen of an analysis tool is usually a spinner. This is a better
one: the sheets, the keys, and whether the keys actually resolve.

It exists because of a real hour lost on this data. Four id joins reported
ZERO overlap and looked like unlinkable workbooks — `admissions.student_id`
was float (`697.0`) and `fee_receipts.student_id` was int (`697`), so a
string-wise comparison matched nothing. Numerically they overlap 99%. Every
check here compares ids as numbers when both sides are numeric.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .config import REFERENCE_SHEETS

JsonDict = Dict[str, Any]

# Columns that name an entity, in the order they should be preferred as a key.
KEY_HINTS = ("student_id", "enq_id", "enq_number", "receipt_id",
             "certificate_number", "mobile", "mobile_clean")

# Below this share of resolving foreign keys, the join is reported broken
# rather than merely imperfect.
RESOLVE_WARN = 0.90


def read_sheets(path: Path) -> Dict[str, pd.DataFrame]:
    """Every non-reference sheet in a workbook, or the single CSV.

    The workbook is opened ONCE, in a `with`, and parsed from that handle.

    Both halves of that matter. An unclosed `pd.ExcelFile` keeps a handle on
    the upload for the life of the process, and on Windows an open handle
    makes the file undeletable — which meant DELETE reported success while the
    uploaded workbook, real names and mobile numbers in it, stayed on disk.
    Parsing from the open book instead of re-calling `read_excel` per sheet
    also stops re-parsing the whole file once per sheet.
    """
    if path.suffix.lower() == ".csv":
        return {path.stem: pd.read_csv(path)}
    out: Dict[str, pd.DataFrame] = {}
    with pd.ExcelFile(path, engine="openpyxl") as book:
        for name in book.sheet_names:
            if name.strip().lower() in REFERENCE_SHEETS:
                continue
            frame = book.parse(name)
            if frame.empty and not len(frame.columns):
                continue
            out[name] = frame
    return out


def _key_set(series: pd.Series) -> set:
    """The distinct ids, compared as numbers when the column is numeric.

    This is the float-vs-int fix: `admissions.student_id` arrives as 697.0 and
    `fee_receipts.student_id` as 697, so a string comparison matches nothing
    while the data joins at 99%.

    The numeric test is on how many values PARSE, never on how many are
    distinct. A child table repeats its foreign key by definition — 2,093
    receipts for 1,528 students — so testing distinctness against row count
    sent every child table down the string path and reported 0% resolve on
    keys that resolve completely.
    """
    present = series.dropna()
    if present.empty:
        return set()
    parsed = pd.to_numeric(present, errors="coerce")
    if parsed.notna().mean() >= 0.9:
        return set(parsed.dropna().astype("int64"))
    return set(present.astype(str).str.strip())


def _primary_key(frame: pd.DataFrame) -> Optional[str]:
    """The column that identifies a row here, by name hint then uniqueness."""
    lowered = {str(c).strip().lower(): c for c in frame.columns}
    for hint in KEY_HINTS:
        col = lowered.get(hint)
        if col is None:
            continue
        values = frame[col].dropna()
        if not values.empty and values.nunique() == len(values):
            return col
    return None


def collect_sheets(paths: Sequence[Path]) -> Dict[str, tuple]:
    """Every sheet across every uploaded file, in one namespace.

    Returns `label -> (frame, source_path, sheet_name)`. The label is the bare
    sheet name; only a name used by two different files is qualified with its
    workbook, so the common case reads `enquiries`, not `FV_Enquiry__enquiries`.

    One namespace is the point: the institute's admissions live in one workbook
    and its enquiries in another, joined on ENQ_ID. Inspecting each file alone
    can never see that link, and a conversion rate computed from the
    admissions file by itself is 100% by construction — every row in it is an
    admission.
    """
    seen: Dict[str, int] = {}
    for path in paths:
        for name in read_sheets(path):
            seen[name] = seen.get(name, 0) + 1

    out: Dict[str, tuple] = {}
    for path in paths:
        for name, frame in read_sheets(path).items():
            label = f"{path.stem}::{name}" if seen.get(name, 0) > 1 else name
            out[label] = (frame, path, name)
    return out


def inspect(paths) -> JsonDict:
    """Sheets, row counts, keys, and whether every foreign key resolves.

    Accepts one path or several; with several, foreign keys are tested *across*
    the uploaded files as well as within each one.
    """
    if isinstance(paths, (str, Path)):
        paths = [Path(paths)]
    paths = [Path(p) for p in paths]

    collected = collect_sheets(paths)
    sheets = {label: frame for label, (frame, _p, _s) in collected.items()}
    origin = {label: p.name for label, (_f, p, _s) in collected.items()}

    tables: List[JsonDict] = []
    keys: Dict[str, tuple] = {}          # sheet -> (column, id set)

    for name, frame in sheets.items():
        pk = _primary_key(frame)
        if pk is not None:
            keys[name] = (pk, _key_set(frame[pk]))
        tables.append({
            "sheet": name,
            "file": origin.get(name, ""),
            "rows": int(len(frame)),
            "columns": int(len(frame.columns)),
            "primary_key": pk,
            "column_names": [str(c) for c in frame.columns],
        })

    links: List[JsonDict] = []
    for name, frame in sheets.items():
        lowered = {str(c).strip().lower(): c for c in frame.columns}
        for parent, (parent_key, parent_ids) in keys.items():
            if parent == name:
                continue
            col = lowered.get(str(parent_key).strip().lower())
            if col is None:
                continue
            child_ids = _key_set(frame[col])
            if not child_ids:
                continue
            resolved = len(child_ids & parent_ids)
            share = resolved / len(child_ids)
            links.append({
                "from_sheet": name,
                "to_sheet": parent,
                # A link whose two ends came from different uploads is the
                # one worth seeing: it is what makes the files one dataset.
                "cross_file": origin.get(name) != origin.get(parent),
                "column": str(col),
                "distinct_keys": len(child_ids),
                "resolved": resolved,
                "orphans": len(child_ids) - resolved,
                "resolve_rate": round(share, 4),
                "ok": share >= RESOLVE_WARN,
            })

    broken = [l for l in links if not l["ok"]]
    return {
        "file": ", ".join(p.name for p in paths),
        "files": [p.name for p in paths],
        "cross_file_links": sum(1 for l in links if l["cross_file"]),
        "tables": sorted(tables, key=lambda t: -t["rows"]),
        "links": sorted(links, key=lambda l: l["resolve_rate"]),
        "total_rows": sum(t["rows"] for t in tables),
        "ok": not broken,
        # Said plainly, because "0 orphans" and "no key found" are different
        # states and only one of them is good news.
        "verdict": (
            "every foreign key resolves" if links and not broken
            else f"{len(broken)} link(s) do not resolve" if broken
            else "no shared keys found between these sheets"
        ),
    }
