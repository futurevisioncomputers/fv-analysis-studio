"""Where the studio finds the pipeline, and where it puts a run.

The analysis code is NOT vendored. The studio imports `agents` from the
fv-analysis plugin repo so there is exactly one copy of the rules; two copies
disagree within a month and nobody can tell which produced a given report.
That already happened once on this machine — the installed plugin cache ran
the old taxonomy rules while the repo had the new ones.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root: .../fv-analysis-studio
ROOT = Path(__file__).resolve().parents[2]

# The pipeline. A sibling checkout by default; override when it lives elsewhere.
PIPELINE_PATH = Path(
    os.environ.get("FV_ANALYSIS_PATH")
    or ROOT.parent / "fv-analysis-marketplace-main"
).resolve()

# Every run gets its own directory: uploads, session, artifacts. Deleting the
# directory deletes the data, which is the whole retention policy for a local
# tool holding student PII.
RUNS_DIR = Path(os.environ.get("FV_STUDIO_RUNS") or ROOT / "runs").resolve()

# Uploads the studio will accept. Anything else is refused by extension before
# a byte is read.
ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xls"}

# Artifacts the API will serve. A whitelist, not a directory listing: the raw
# upload holds unmasked names and mobiles and must never leave the machine
# through this API. `cleaned.csv` is masked and `report.html` is PII-guarded.
SERVABLE_ARTIFACTS = {"report.html", "cleaned.csv"}

# Sheets that are reference data, not records. The institute's workbooks carry
# a `lookups` tab feeding dropdowns; ingesting it as a source produced a
# "dropped 8/8 rows" quality note on every run.
REFERENCE_SHEETS = {"lookups", "lookup", "reference", "config", "readme"}


def ensure_pipeline_importable() -> Path:
    """Put the pipeline on sys.path. Raises if it is not where we think."""
    if not (PIPELINE_PATH / "agents" / "stages.py").exists():
        raise RuntimeError(
            f"fv-analysis pipeline not found at {PIPELINE_PATH}. "
            f"Set FV_ANALYSIS_PATH to the repo root."
        )
    if str(PIPELINE_PATH) not in sys.path:
        sys.path.insert(0, str(PIPELINE_PATH))
    return PIPELINE_PATH
