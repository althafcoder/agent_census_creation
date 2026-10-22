"""Central configuration for the Census Comparison pipeline.
Dedicated unique module name to avoid any namespace collision with parent config packages or GPU server extractors.
"""
from pathlib import Path
import os
import re

BASE_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = BASE_DIR / "backend"
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
EXTRACTED_TEXT_DIR = BASE_DIR / "extracted_text"
EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)

# Load environment variables from .env immediately before any paths are discovered
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

def _discover(env_var: str, folder: Path, pattern: str) -> Path:
    """Env override first, else the first file in `folder` matching `pattern` (no hardcoded filenames)."""
    override = os.getenv(env_var)
    if override and Path(override).exists():
        return Path(override)
    found = sorted(p for p in folder.glob(pattern) if not p.name.startswith("~$"))
    return found[0] if found else folder / pattern


def _discover_all(env_var: str, folder: Path, pattern: str) -> list[Path]:
    """Env override first (comma/semicolon-separated paths or folder), else all files in `folder` matching `pattern`."""
    override = os.getenv(env_var)
    if override:
        paths = []
        for part in re.split(r"[,;]", override):
            part = part.strip()
            if not part:
                continue
            p = Path(part)
            if p.is_dir():
                paths.extend(sorted(f for f in p.glob(pattern) if not f.name.startswith("~$")))
            elif p.exists():
                paths.append(p)
        if paths:
            return paths
    found = sorted(p for p in folder.glob(pattern) if not p.name.startswith("~$"))
    # Order Combined instruction first if present, then other instructions alphabetically
    return sorted(found, key=lambda p: (0 if "combined" in p.name.lower() else 1, p.name.lower()))


# Carrier definitions
CARRIER_FLORIDA_BLUE = "Florida Blue"
CARRIER_CIGNA = "Cigna"

# Template Paths
RAPT_TEMPLATE_PATH = _discover("RAPT_TEMPLATE_PATH", BACKEND_DIR / "templates", "*RAPT*.xlsx")
MARSH_TEMPLATE_PATH = _discover("MARSH_TEMPLATE_PATH", BACKEND_DIR / "templates", "*Marsh*.xlsx")
SBU_TEMPLATE_PATH = _discover("SBU_TEMPLATE_PATH", BACKEND_DIR / "templates", "*SBU*.xlsx")

# Default template path for backwards compatibility
TEMPLATE_PATH = _discover("CENSUS_TEMPLATE_PATH", BACKEND_DIR / "templates", "*RAPT*.xlsx")

# Instructions Paths (dynamic multi-instruction discovery for all controlling instruction documents)
INSTRUCTIONS_DIR = BACKEND_DIR / "instructions"
INSTRUCTIONS_PATHS = _discover_all("CENSUS_INSTRUCTIONS_PATHS", INSTRUCTIONS_DIR, "*.docx")
INSTRUCTIONS_PATH = INSTRUCTIONS_PATHS[0] if INSTRUCTIONS_PATHS else _discover("CENSUS_INSTRUCTIONS_PATH", INSTRUCTIONS_DIR, "*.docx")

# ---- Template layout (RAPT Census Template) --------------------------------
HEADER_ROW = 14
FIRST_DATA_ROW = 15

# Columns A..L are the protected underwriting section.
TEMPLATE_COLS = {
    "data_row": 1, "first": 2, "last": 3, "gender": 4, "dob": 5, "zip": 6,
    "relationship": 7, "dep_of": 8, "coverage": 9, "cobra": 10,
    "plan": 11, "premium": 12,
}

# Prospect block: value goes in the top-left cell of the merged value range.
PROSPECT_CELLS = {
    "effective_date": "C2",
    "sic": "H2",
    "company": "C4",
    "address": "C6",
    "city_state_zip": "C8",
    "referral": "C10",
    "sales_person": "H10",
}

# Comparison section: starts AFTER column L (column M = 13).
COMPARISON_START_COL = 13
COMPARISON_HEADERS = [
    "Invoice Employee Name",   # M (13)
    "Match %",                 # N (14)
    "Invoice Plan",            # O (15)
    "Invoice Tier",            # P (16)
    "Invoice Status",          # Q (17)
    "Invoice Premium",         # R (18)
    "Renewal Premium",         # S (19)
    "Comparison Result",       # T (20)
    "Notes",                   # U (21)
]
COMPARISON_WIDTHS = [26, 40, 28, 12, 30, 15, 15, 24, 70]

# ---- Marsh PEO Practice Layout ---------------------------------------------
MARSH_HEADER_ROW = 12
MARSH_FIRST_DATA_ROW = 14
MARSH_COMPARISON_START_COL = 15  # Col O (after Code Key columns K-N)
MARSH_COLS = {
    "row_no": 1, "first": 2, "last": 3, "gender": 4, "dob": 5, "zip": 6,
    "relationship": 7, "coverage": 8, "cobra": 9, "plan": 10,
}

# ---- SBU Census Template Layout --------------------------------------------
SBU_HEADER_ROW = 3
SBU_FIRST_DATA_ROW = 5
SBU_COLS = {
    "first": 1, "last": 2, "type": 3, "dob": 4, "gender": 5, "zip": 6,
    "waived": 7, "tobacco": 8, "ssn": 9, "email": 10, "phone": 11,
    "hire_date": 12, "salary": 13,
}

# ---- Colours ---------------------------------------------------------------
GREEN_HEADER_FILL = "FF00B050"
RED = "FFFF0000"
ORANGE = "FFFF8C00"
GREEN = "FF00B050"
BLUE_HEADER = "FF537BB1"

# ---- Business rules --------------------------------------------------------
APPROVED_COVERAGE = ["EE", "ES", "EC", "FAM", "NE", "WP", "RC", "WO"]
ACTIVE_TIERS = ["EE", "ES", "EC", "FAM"]
NON_ENROLLED = {"NE", "WO", "RC"}   # absent from invoice is expected, no notes
FUZZY_ACCEPT = 0.80                  # name similarity accepted as a match
FUZZY_WITH_DOB = 0.60                # lower bar when DOB also agrees

# OCR / External binaries
POPPLER_PATH = os.getenv("POPPLER_PATH", "")
TESSERACT_PATH = os.getenv("TESSERACT_PATH", "")

# ---- LLM Configuration (Anthropic Claude) -----------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "")

USE_LLM_FALLBACK = bool(ANTHROPIC_API_KEY)
