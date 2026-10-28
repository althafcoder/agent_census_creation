"""Central configuration for the Census Comparison pipeline.
Dedicated unique module name to avoid any namespace collision with parent config packages or GPU server extractors.
"""
from pathlib import Path
import os
import re
from typing import Any

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
    # Order Master / Unified / Combined instruction first if present, then other instructions alphabetically
    return sorted(found, key=lambda p: (0 if any(k in p.name.lower() for k in ("master", "unified", "combined")) else 1, p.name.lower()))


# Carrier definitions
CARRIER_FLORIDA_BLUE = "Florida Blue"
CARRIER_CIGNA = "Cigna"

# Template Paths
RAPT_TEMPLATE_PATH = _discover("RAPT_TEMPLATE_PATH", BACKEND_DIR / "templates", "*RAPT*.xlsx")
MARSH_TEMPLATE_PATH = _discover("MARSH_TEMPLATE_PATH", BACKEND_DIR / "templates", "*Marsh*.xlsx")
SBU_TEMPLATE_PATH = _discover("SBU_TEMPLATE_PATH", BACKEND_DIR / "templates", "*SBU*.xlsx")

# Default template path for backwards compatibility
TEMPLATE_PATH = _discover("CENSUS_TEMPLATE_PATH", BACKEND_DIR / "templates", "*RAPT*.xlsx")

# Instructions Paths: dynamic discovery directly accessing backend/instructions/
INSTRUCTIONS_DIR = BACKEND_DIR / "instructions"

def get_instruction_paths() -> list[Path]:
    """Dynamically return all active instruction documents currently in backend/instructions/."""
    if not INSTRUCTIONS_DIR.exists():
        return []
    override = os.getenv("CENSUS_INSTRUCTIONS_PATHS") or os.getenv("CENSUS_INSTRUCTIONS_PATH")
    if override:
        paths = [Path(p.strip()) for p in re.split(r"[,;]", override) if Path(p.strip()).exists()]
        if paths:
            return paths
    files = [
        p for p in INSTRUCTIONS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".txt", ".docx", ".md", ".pdf")
        and not p.name.startswith("~$") and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: (
        0 if any(k in p.name.lower() for k in ("dynamic", "master", "unified", "combined", "instruction")) else 1,
        p.name.lower()
    ))
    return files

def get_instruction_path() -> Path:
    """Return the primary active instruction document path dynamically."""
    paths = get_instruction_paths()
    return paths[0] if paths else (INSTRUCTIONS_DIR / "Dynamic_Unified_Medical_Submission_Agent_Instruction.txt")

def __getattr__(name: str) -> Any:
    if name == "INSTRUCTIONS_PATHS":
        return get_instruction_paths()
    if name == "INSTRUCTIONS_PATH":
        return get_instruction_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
MARSH_COMPARISON_START_COL = 11  # Col K
MARSH_COLS = {
    "row_no": 1, "first": 2, "last": 3, "gender": 4, "dob": 5, "zip": 6,
    "relationship": 7, "coverage": 8, "cobra": 9, "plan": 10,
}
MARSH_COMPARISON_HEADERS = [
    "Invoice Employee Name",   # K (11)
    "Match %",                 # L (12)
    "Invoice Group #",         # M (13)
    "Invoice Plan",            # N (14)
    "Invoice Tier",            # O (15)
    "Invoice Status",          # P (16)
    "Plan Enrolled",           # Q (17)
    "Invoice Premium",         # R (18)
    "Renewal Premium",         # S (19)
    "Comparison Result",       # T (20)
    "Notes",                   # U (21)
]
MARSH_COMPARISON_WIDTHS = [26, 12, 18, 35, 14, 18, 35, 16, 16, 24, 40]

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
ANTHROPIC_EFFORT = os.getenv("ANTHROPIC_EFFORT", "high")
ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "16000"))
_temp_env = os.getenv("ANTHROPIC_TEMPERATURE", "").strip()
ANTHROPIC_TEMPERATURE = (
    float(_temp_env)
    if (_temp_env and not any(tag in ANTHROPIC_MODEL.lower() for tag in ("opus-5-5", "opus-5.5")))
    else None
)
ANTHROPIC_STRUCTURED_OUTPUTS = os.getenv("ANTHROPIC_STRUCTURED_OUTPUTS", "false").lower() in ("true", "1", "yes")
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "")

USE_LLM_FALLBACK = bool(ANTHROPIC_API_KEY)
LLM_FIRST = os.getenv("LLM_FIRST", "true").lower() in ("true", "1", "yes")

