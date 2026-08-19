"""The institute's own two diagrams, as data the UI can render.

The operator drew these, so the studio uses them as its navigation rather than
inventing a structure:

  DATA MODEL     student_id is the spine. Enquiry / Admission / Fee Data hang
                 off it, resolve to Current Student, and end at Certificate or
                 Churn, both feeding Management Reporting.

  DASHBOARD      Executive -> Enquiry / Admission / Finance, each with its own
                 dimensions, joining at a Conversion Engine.

Every node here carries what the run actually found. A node the data cannot
fill is marked `missing` and says so on screen — an empty chart under a
confident heading is worse than an honest gap, because only one of them gets
fixed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

JsonDict = Dict[str, Any]

# --- the data model diagram -------------------------------------------------
# Each entity: the sheet it comes from, and the fields the operator listed.
DATA_MODEL: List[JsonDict] = [
    {"id": "master", "label": "STUDENT MASTER", "key": "student_id",
     "sheet": "students", "fields": ["student_id", "name", "mobile"],
     "parents": []},
    {"id": "enquiry", "label": "ENQUIRY", "sheet": "enquiries",
     "fields": ["counsellor", "lead_source", "course", "branch",
                "mode_of_enquiry"],
     "parents": ["master"]},
    {"id": "admission", "label": "ADMISSION", "sheet": "students",
     "fields": ["course", "branch", "faculty", "admission_date"],
     "parents": ["master"]},
    {"id": "fee", "label": "FEE DATA", "sheet": "students",
     "fields": ["total_fees", "amt_pending", "fee_status"],
     "parents": ["master"]},
    {"id": "current", "label": "CURRENT STUDENT", "sheet": "students",
     "fields": ["status"], "parents": ["enquiry", "admission", "fee"]},
    {"id": "receipts", "label": "FEE RECEIPTS", "sheet": "fee_receipts",
     "fields": ["receipt_id", "date_of_receipt", "paid_amt",
                "mode_of_payment"],
     "parents": ["fee"]},
    {"id": "completed", "label": "COURSE COMPLETION", "sheet": "students",
     "fields": ["status"], "parents": ["current"]},
    {"id": "not_coming", "label": "NOT COMING", "sheet": "students",
     "fields": ["status", "status_detail"], "parents": ["current"]},
    {"id": "certificate", "label": "CERTIFICATE", "sheet": "certificates",
     "fields": ["certificate_number", "certificate_issue_date"],
     "parents": ["completed"]},
    {"id": "churn", "label": "CHURN", "sheet": "students",
     "fields": ["course_duration_days"], "parents": ["not_coming"]},
    {"id": "reporting", "label": "MANAGEMENT REPORTING", "sheet": None,
     "fields": [], "parents": ["certificate", "churn", "receipts"]},
]

# --- the executive dashboard diagram ---------------------------------------
DASHBOARD: List[JsonDict] = [
    {"id": "enquiry", "label": "ENQUIRY", "sheet": "enquiries",
     "dimensions": ["course", "branch", "counsellor", "lead_source",
                    "mode_of_enquiry", "education_level", "presently_doing",
                    "area", "pincode"]},
    {"id": "admission", "label": "ADMISSION", "sheet": "students",
     "dimensions": ["course", "branch", "faculty", "lead_source",
                    "education_level", "presently_doing", "age", "area",
                    "pincode"]},
    {"id": "finance", "label": "FINANCE", "sheet": "fee_receipts",
     "dimensions": ["paid_amt", "receipt_id", "amt_pending"]},
]

# What a dimension is called when the sheet spells it differently.
ALIASES = {
    "lead_source": ("lead_source", "lead_source_bucket", "source"),
    "presently_doing": ("presently_doing", "occupation"),
    "education_level": ("education_level", "education"),
    "mode_of_enquiry": ("mode_of_enquiry", "mode"),
    "amt_pending": ("amt_pending", "pending", "outstanding"),
    "paid_amt": ("paid_amt", "paid", "amount"),
    "age": ("age", "dob", "date_of_birth"),
    "area": ("area", "locality", "residential_area"),
    "pincode": ("pincode", "pin_code", "postcode"),
}


def _find(frame: pd.DataFrame, name: str) -> Optional[str]:
    lowered = {str(c).strip().lower(): c for c in frame.columns}
    for candidate in ALIASES.get(name, (name,)):
        if candidate in lowered:
            return lowered[candidate]
    return None


def annotate(sheets: Dict[str, pd.DataFrame]) -> JsonDict:
    """Both diagrams, with each node's real coverage from this upload."""

    def coverage(sheet: Optional[str], field: str) -> JsonDict:
        frame = sheets.get(sheet or "")
        if frame is None:
            return {"field": field, "present": False, "filled": 0, "rows": 0}
        col = _find(frame, field)
        if col is None:
            return {"field": field, "present": False, "filled": 0,
                    "rows": int(len(frame))}
        return {"field": field, "present": True, "column": str(col),
                "filled": int(frame[col].notna().sum()),
                "rows": int(len(frame))}

    model = []
    for node in DATA_MODEL:
        frame = sheets.get(node.get("sheet") or "")
        fields = [coverage(node.get("sheet"), f) for f in node["fields"]]
        model.append({**node,
                      "rows": int(len(frame)) if frame is not None else None,
                      "fields": fields,
                      "missing": [f["field"] for f in fields if not f["present"]]})

    dashboard = []
    for node in DASHBOARD:
        dims = [coverage(node["sheet"], d) for d in node["dimensions"]]
        dashboard.append({**node, "dimensions": dims,
                          "missing": [d["field"] for d in dims
                                      if not d["present"]]})
    return {"data_model": model, "dashboard": dashboard}
