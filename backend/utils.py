"""Normalisation / standardisation helpers (names, dates, gender, coverage, zip...)."""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Optional

import pandas as pd
from dateutil import parser as dateparser

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def is_blank(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() == "" or str(v).strip().lower() in {"nan", "none", "null", "n/a", "na", "-"}


def clean(v) -> str:
    return "" if is_blank(v) else re.sub(r"\s+", " ", str(v)).strip()


def clean_company_name(name: str) -> str:
    """Clean company name by removing table delimiters, extra whitespace, and trailing punctuation."""
    if is_blank(name):
        return ""
    s = str(name).strip()
    # Strip table bar artifacts like '| National Insurance Brokerage of NY Inc | | | |'
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        s = parts[0] if parts else s
    # Strip leading/trailing formatting artifacts and punctuation
    s = re.sub(r"^[\s|:;,.\-_]+|[\s|:;,.\-_]+$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def sanitize_filename(name: str, max_length: int = 50, default: str = "Output") -> str:
    """Sanitize a string to be completely safe for use in filenames on Windows, Mac, and Linux.

    Removes invalid filesystem characters: < > : " / \\ | ? * and ASCII control characters.
    Collapses whitespace and underscores, and trims leading/trailing dots, spaces, and underscores.
    """
    if is_blank(name):
        return default
    # If table pipe artifacts exist, clean company name first
    s = clean_company_name(str(name))
    # Remove characters forbidden in Windows and POSIX filenames
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    # Replace whitespace with underscore
    s = re.sub(r"\s+", "_", s)
    # Collapse multiple underscores
    s = re.sub(r"_+", "_", s).strip("_. ")
    if not s:
        return default
    return s[:max_length]


def norm_key(s: str) -> str:
    """Lower-case, alphanumerics only - used for header / plan matching."""
    return re.sub(r"[^a-z0-9]", "", clean(s).lower())


# ---------------------------------------------------------------- names ----
def split_full_name(full: str) -> tuple[str, str]:
    """'Smith, John A' -> ('John A','Smith');  'John A Smith' -> ('John A','Smith')."""
    full = clean(full)
    if not full:
        return "", ""
    if "," in full:
        last, _, first = full.partition(",")
        return first.strip(), last.strip()
    parts = full.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    # trailing suffix stays with the last name
    if parts[-1].lower().strip(".") in SUFFIXES and len(parts) > 2:
        return " ".join(parts[:-2]), " ".join(parts[-2:])
    return " ".join(parts[:-1]), parts[-1]


def name_tokens(first: str, last: str) -> list[str]:
    raw = re.sub(r"[^a-z\s]", " ", f"{clean(first)} {clean(last)}".lower()).split()
    return sorted(t for t in raw if len(t) > 1 and t not in SUFFIXES)


def name_key(first: str, last: str) -> str:
    return " ".join(name_tokens(first, last))


def name_similarity(a: list[str], b: list[str]) -> float:
    """Order-insensitive similarity. 'Smith, John' == 'John Smith' == 1.0."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()


def display_name(first: str, last: str) -> str:
    return f"{clean(first)} {clean(last)}".strip()


# ---------------------------------------------------------------- dates ----
def parse_date(v) -> Optional[date]:
    if is_blank(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # Excel serial date
        if 20000 < float(v) < 60000:
            return (datetime(1899, 12, 30) + pd.Timedelta(days=float(v))).date()
        return None
    s = clean(v)
    if re.fullmatch(r"\d{8}", s):  # 19800131 / 01311980
        for fmt in ("%m%d%Y", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    try:
        d = dateparser.parse(s, dayfirst=False, fuzzy=False).date()
        if d.year < 100:
            return None
        if d.year > date.today().year + 1 and d.year - 100 > 1900:  # 2-digit year fix
            d = d.replace(year=d.year - 100)
        return d
    except (ValueError, OverflowError, TypeError):
        return None


# --------------------------------------------------------------- gender ----
_UNAVAILABLE_GENDER_TOKENS = {
    "unknown", "unk", "u", "undisclosed", "not specified", "notspecified",
    "not provided", "notprovided", "unspecified", "prefer not to say",
    "prefer not to answer", "declined", "decline to state", "missing",
    "masked", "tbd", "pending", "n/a", "na", "none", "null", "nan",
}
_FEMALE_TOKENS = {"f", "female", "woman", "women", "girl", "fem"}
_MALE_TOKENS = {"m", "male", "man", "men", "boy", "masc"}


def is_unavailable_gender(v) -> bool:
    """Return True if value is blank, masked (e.g. XXXX), or an unavailable placeholder token."""
    if is_blank(v):
        return True
    s = clean(v)
    if not s:
        return True
    if re.fullmatch(r"[*Xx\s\-./#?_]+", s):
        return True
    low = s.lower()
    if low in _UNAVAILABLE_GENDER_TOKENS:
        return True
    return False


def std_gender(v) -> str:
    """Normalize unambiguous gender/sex strings to 'M' or 'F'.
    Returns '' for unavailable placeholders, masked strings, or ambiguous/conflicting strings."""
    if is_unavailable_gender(v):
        return ""
    s = clean(v).lower()
    tokens = [t for t in re.split(r"[^a-z]+", s) if t]
    has_f = any(t in _FEMALE_TOKENS for t in tokens)
    has_m = any(t in _MALE_TOKENS for t in tokens)
    if has_f and has_m:
        return ""
    if has_f:
        return "F"
    if has_m:
        return "M"
    # Fallback for compound strings without separators (e.g. 'gendermale', 'sexmale')
    if any(k in s for k in ("female", "woman", "girl")) and not any(k in re.sub(r"female|woman", "", s) for k in ("male", "man", "boy")):
        return "F"
    if any(k in s for k in ("male", "man", "boy")) and not any(k in s for k in ("female", "woman", "girl")):
        return "M"
    return ""


def build_gender_candidate(raw_v, source: str = "", column_index: int = -1, header: str = "") -> dict:
    """Create a structured candidate gender observation preserving physical column & source identity."""
    raw_clean = clean(raw_v) if not is_blank(raw_v) else ""
    if is_unavailable_gender(raw_v):
        return {
            "source": source,
            "column_index": column_index,
            "header": header,
            "raw": raw_clean,
            "normalized": "",
            "status": "UNAVAILABLE",
        }
    norm = std_gender(raw_v)
    return {
        "source": source,
        "column_index": column_index,
        "header": header,
        "raw": raw_clean,
        "normalized": norm,
        "status": "USABLE" if norm in ("M", "F") else "INVALID",
    }


def resolve_gender_candidates(candidates: list[dict]) -> tuple[str, str, str]:
    """Resolve all candidate gender/sex observations for the same person across columns and sources.

    Returns (resolved_gender, status, detail) where:
      - resolved_gender is 'M', 'F', or ''
      - status is 'RESOLVED', 'CONFLICT', 'INVALID', or 'MISSING'
      - detail describes any conflict or invalid raw token
    """
    usable = [c for c in candidates if c.get("status") == "USABLE" and c.get("normalized") in ("M", "F")]
    usable_vals = {c["normalized"] for c in usable}
    if len(usable_vals) == 1:
        return next(iter(usable_vals)), "RESOLVED", ""
    if len(usable_vals) > 1:
        parts = []
        for c in usable:
            hdr = c.get("header") or f"col_{c.get('column_index', '?')}"
            src = c.get("source") or ""
            loc = f"{src}:{hdr}" if src else hdr
            parts.append(f"{loc}='{c.get('raw')}'->{c.get('normalized')}")
        return "", "CONFLICT", ", ".join(parts)
    invalids = [c for c in candidates if c.get("status") == "INVALID"]
    if invalids:
        return "", "INVALID", invalids[0].get("raw", "")
    return "", "MISSING", ""


# ------------------------------------------------------------------ zip ----
def std_zip(v) -> tuple[str, bool]:
    """Returns (zip, is_valid). Pads leading zeros lost by Excel."""
    if is_blank(v):
        return "", True
    s = clean(v)
    numeric = isinstance(v, (int, float)) and not isinstance(v, bool)
    if isinstance(v, float) and v == int(v):
        s = str(int(v))
    s = s.replace(" ", "")
    if numeric and re.fullmatch(r"\d{3,4}", s):      # Excel dropped the leading zero of a numeric ZIP
        s = s.zfill(5)
    if re.fullmatch(r"\d{5}", s):
        return s, True
    if re.fullmatch(r"\d{9}", s):
        return f"{s[:5]}-{s[5:]}", True
    if re.fullmatch(r"\d{5}-\d{4}", s):
        return s, True
    return s, False


# ------------------------------------------------------------- coverage ----
_COVERAGE_MAP = {
    "ee": "EE", "e": "EE", "emp": "EE", "employee": "EE", "employeeonly": "EE", "eeonly": "EE",
    "subscriberonly": "EE", "subscriber": "EE", "single": "EE", "individual": "EE", "self": "EE",
    "es": "ES", "s": "ES", "eesp": "ES", "eespouse": "ES", "employeespouse": "ES", "employeeandspouse": "ES",
    "employeeplusspouse": "ES", "subscriberspouse": "ES", "subscriberandspouse": "ES",
    "employeesp": "ES", "esp": "ES", "empspouse": "ES", "twoparty": "ES", "couple": "ES",
    "eesd": "ES", "ees/d": "ES", "ee+s/d": "ES", "eesp/dp": "ES", "eespousedp": "ES",
    "employeespouseordomestic": "ES", "employeeandspouseordomestic": "ES",
    "employeespouseordomesticpartner": "ES", "employeeandspouseordomesticpartner": "ES",
    "ec": "EC", "c": "EC", "k": "EC", "eech": "EC", "eechild": "EC", "eechildren": "EC",
    "employeechild": "EC", "employeechildren": "EC", "employeeandchild": "EC",
    "employeeandchildren": "EC", "employeepluschild": "EC", "employeepluschildren": "EC",
    "subscriberchild": "EC", "subscriberchildren": "EC",
    "empchild": "EC", "empchildren": "EC", "child": "EC", "children": "EC",
    "fam": "FAM", "family": "FAM", "employeefamily": "FAM", "eefamily": "FAM",
    "employeeplusfamily": "FAM", "subscriberfamily": "FAM", "eefam": "FAM", "empfamily": "FAM",
    "ef": "FAM", "ee+f": "FAM", "eef": "FAM", "f": "FAM",
    "ne": "NE", "i": "NE", "noteligible": "NE", "ineligible": "NE", "noteligibleforcoverage": "NE",
    "wp": "WP", "waitingperiod": "WP", "waiting": "WP", "inwaitingperiod": "WP", "waitperiod": "WP",
    "probation": "WP", "probationary": "WP", "probationaryperiod": "WP", "p": "WP",
    "rc": "RC", "refused": "RC", "refusedcoverage": "RC", "refusingcoverage": "RC",
    "declined": "RC", "refuse": "RC", "refusal": "RC",
    "wo": "WO", "waived": "WO", "waive": "WO", "waivedothercoverage": "WO",
    "waivingforothercoverage": "WO", "waiver": "WO", "optout": "WO", "optedout": "WO",
    "waivedcoverage": "WO", "w": "WO", "otherCoverage".lower(): "WO",
}


def std_coverage(v) -> str:
    """Map any wording to an approved code; returns '' when it cannot be mapped."""
    k = norm_key(v)
    if not k:
        return ""
    if k in _COVERAGE_MAP:
        return _COVERAGE_MAP[k]
    # token-based fallbacks
    low = clean(v).lower()
    if re.search(r"famil", low):
        return "FAM"
    if re.search(r"(spouse|partner)", low) and re.search(r"(child|dep)", low):
        return "FAM"
    if re.search(r"(spouse|partner|\bsp\b)", low):
        return "ES"
    if re.search(r"(child|kid)", low):
        return "EC"
    if re.search(r"wait", low) or re.search(r"\bprobation(ary)?\b", low) or low in ("p", "probation", "probationary"):
        return "WP"
    if re.search(r"(waiv|opt)", low):
        return "WO"
    # Qualified / spousal / other-coverage waiver shorthand codes (e.g. 'QW', 'SW', 'OW',
    # 'Q-W', 'Qual W'): a short alpha code whose final token is a bare 'W' denotes a waiver.
    if re.fullmatch(r"(?:q|qual|qualified|s|sp|o|oc|other)[\s\-_/]*w", low):
        return "WO"
    if re.search(r"(refus|declin)", low):
        return "RC"
    if re.search(r"(not eligible|ineligible)", low):
        return "NE"
    return ""


def coverage_code_inferred(v) -> bool:
    """True when ``v`` mapped to a coverage code only through a heuristic fallback
    (i.e. it is not an approved/known coverage spelling). Such values are standardized
    but should be confirmed with the agent in the Missing Information Email."""
    k = norm_key(v)
    return bool(k) and k not in _COVERAGE_MAP and bool(std_coverage(v))


def std_relationship(v) -> str:
    s = clean(v).lower()
    if not s:
        return ""
    if s in {"ee", "e", "emp", "employee", "self", "subscriber", "primary", "member", "s", "insured"}:
        return "EE"

    if re.search(r"\b(domestic\s*partner|\bdp\b)\b", s) or s == "dp":
        return "DP"

    # Business / employment job title guard: job titles must never be treated as dependent relationships
    _JOB_TITLE_WORDS = (
        "vp", "vice president", "president", "manager", "director", "lead", "head", "officer",
        "development", "business", "engineer", "analyst", "specialist", "coordinator", "assistant",
        "supervisor", "consultant", "administrator", "representative", "operations", "tech",
        "technician", "executive", "clerk", "architect", "accountant", "counsel",
        "general", "senior", "managing", "principal", "associate", "founding"
    )
    if any(re.search(rf"\b{re.escape(w)}\b", s) for w in _JOB_TITLE_WORDS):
        return ""

    if re.search(r"\b(spouse|wife|husband)\b|\bpartner\b(?!ship)", s) or s in {"sp", "w", "h"}:
        return "SP"
    if re.search(r"\b(child|son|daughter|dependent|step)\b|\bch\b|\bc\b|\bdep\b", s) or s in {"ch", "c", "dep", "d"}:
        return "CH"
    return ""


INSPERITY_OPTION_LEGEND = {
    "3": {"plan": "UHC Choice Plus PPO 500 (90%)", "deductible": 500, "is_medical": True},
    "32": {"plan": "UHC Choice Plus PPO HDHP 1700", "deductible": 1700, "is_medical": True},
    "33": {"plan": "UHC Choice Plus PPO 250", "deductible": 250, "is_medical": True},
    "46": {"plan": "Kaiser HMO CA South", "deductible": 0, "is_medical": True},
    "51": {"plan": "UHC of CA HMO South", "deductible": 0, "is_medical": True},
    "58": {"plan": "UHC Choice Plus PPO HDHP 5000", "deductible": 5000, "is_medical": True},
    "96": {"plan": "Kaiser HMO CO", "deductible": 0, "is_medical": True},
    "12": {"plan": "Dental/Vision", "is_medical": False},
    "57": {"plan": "Basic Life", "is_medical": False},
}


def decode_insperity_coverage(v: str) -> dict:
    """Decode Insperity comma-delimited Coverage Group / Option codes (e.g. '3,57,58,12')."""
    s = clean(v)
    if not s or not re.search(r"^\d+(?:\s*,\s*\d+)+$", s):
        return {"is_insperity_code": False, "medical_plan": "", "is_medical": False, "suggested_status": ""}
    codes = [c.strip() for c in s.split(",") if c.strip()]
    med_plans = []
    has_medical = False
    for c in codes:
        meta = INSPERITY_OPTION_LEGEND.get(c)
        if meta and meta.get("is_medical"):
            # Check special case for 58: when paired with 57 and no other medical code, 58 is Basic AD&D
            if c == "58" and ("57" in codes) and not any(k in codes for k in ("3", "32", "33", "46", "51", "96")):
                continue
            med_plans.append(meta["plan"])
            has_medical = True
    plan_name = med_plans[0] if med_plans else ""
    suggested_status = "WO" if (not has_medical and any(c in ("12", "57", "58") for c in codes)) else ""
    return {
        "is_insperity_code": True,
        "medical_plan": plan_name,
        "is_medical": has_medical,
        "suggested_status": suggested_status,
        "codes": codes
    }


def std_cobra(v) -> str:
    s = clean(v).lower()
    if s in {"y", "yes", "true", "1", "cobra"}:
        return "Y"
    if s in {"n", "no", "false", "0"}:
        return "N"
    return ""


def to_money(v) -> Optional[float]:
    if is_blank(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = clean(v)
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in {"", "-", "."}:
        return None
    try:
        x = float(s)
    except ValueError:
        return None
    return -abs(x) if neg else x


# ----------------------------------------------------------------- plans ----
_PLAN_NOISE = re.compile(r"\b(plan|medical|health|the|option|network|ppo|hmo)\b")


def plan_key(name: str) -> str:
    s = clean(name).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def plan_loose_key(name: str) -> str:
    s = _PLAN_NOISE.sub(" ", clean(name).lower())
    return re.sub(r"[^a-z0-9]", "", s)


def _plan_deductibles(name: str) -> set[str]:
    """Extract deductible-like numbers (>=100) from a plan name, stripping coinsurance percentages."""
    s = re.sub(r"\(\s*\d+\s*%\s*\)", " ", clean(name).lower())
    s = re.sub(r"\b\d+\s*%", " ", s)
    s = re.sub(r"\bin\s+20\d{2}\b", " ", s)
    nums = set()
    for m in re.findall(r"\b(\d{3,4})\b", s):
        val = int(m)
        if 100 <= val <= 9999:
            nums.add(m)
    return nums


def match_plan(plan: str, candidates: list[str]) -> Optional[str]:
    """Confident plan matching only. Returns the candidate string or None."""
    if not clean(plan):
        return None
    pk = plan_key(plan)
    for c in candidates:
        if plan_key(c) == pk:
            return c
    lk = plan_loose_key(plan)
    if lk:
        hits = [c for c in candidates if plan_loose_key(c) == lk]
        if len(hits) == 1:
            return hits[0]
    best, score = None, 0.0
    for c in candidates:
        r = SequenceMatcher(None, pk, plan_key(c)).ratio()
        if r > score:
            best, score = c, r
    second = sorted((SequenceMatcher(None, pk, plan_key(c)).ratio() for c in candidates), reverse=True)[1:2]
    if best and score >= 0.88 and (not second or score - second[0] > 0.05):
        return best

    # Semantic deductible / HMO regional matching (e.g. 'UHC Choice Plus PPO 500 (90%)' <-> '500 Deductible',
    # 'UHC Choice Plus PPO HDHP 1750' <-> '1700 Deductible - HDHP (1750 in 2027)',
    # 'UHC Signature Value HMO CA South' <-> 'UHC of CA HMO South')
    p_low = clean(plan).lower()
    p_deds = _plan_deductibles(plan)
    p_hdhp = bool(re.search(r"\b(hdhp|hsa)\b", p_low))
    if p_deds:
        ded_hits = []
        for c in candidates:
            c_low = clean(c).lower()
            if any(w in c_low for w in ("dental", "vision", "life", "ad&d")):
                continue
            c_deds = _plan_deductibles(c)
            c_hdhp = bool(re.search(r"\b(hdhp|hsa)\b", c_low))
            if (p_deds & c_deds) and (p_hdhp == c_hdhp):
                ded_hits.append(c)
        if len(ded_hits) == 1:
            return ded_hits[0]

    region_tokens = {"uhc", "kaiser", "bca", "aetna", "cigna", "anthem", "hmo", "ppo", "pos", "epo",
                     "ca", "co", "tx", "fl", "ny", "wa", "or", "north", "south", "northwest", "washington"}
    p_toks = set(re.findall(r"[a-z]+", p_low)) & region_tokens
    if len(p_toks) >= 3:
        reg_hits = []
        for c in candidates:
            c_low = clean(c).lower()
            c_toks = set(re.findall(r"[a-z]+", c_low)) & region_tokens
            if c_toks == p_toks:
                reg_hits.append(c)
        if len(reg_hits) == 1:
            return reg_hits[0]

    return None


_DEFAULT_PEO_LEGEND: dict[str, str] = {
    "3": "500 Deductible",
    "12": "50 Deductible Dental and Vision",
    "32": "1700 Deductible - HDHP (1750 in 2027)",
    "33": "250 Deductible",
    "46": "Kaiser HMO CA South",
    "51": "UHC of CA HMO South",
    "57": "Basic Life Insurance",
    "58": "Basic AD&D",
    "58_MED": "5000 Deductible - HDHP",
    "96": "Kaiser HMO CO",
}


def extract_peo_coverage_legend(texts: list[str]) -> dict[str, str]:
    """Dynamically extract PEO Coverage Group / Option code legend from Group Identification Key
    or Schedule A text (Section 10.1A).
    """
    legend: dict[str, str] = {}
    line_re = re.compile(r"^\s*(\d{1,3})\s+([A-Za-z0-9][^\n\r]{3,65}?)\s*$")
    for txt in texts:
        if not txt or "LEGEND" not in txt.upper():
            continue
        in_legend = False
        for raw_line in txt.splitlines():
            s = raw_line.strip()
            if re.search(r"\bLEGEND\s*:", s, re.I):
                in_legend = True
                continue
            if not in_legend:
                continue
            if re.search(r"Coverage\s+Level\s*:", s, re.I) or re.search(r"Insperity\s*•", s, re.I):
                in_legend = False
                continue
            m = line_re.match(s)
            if m:
                code, desc = m.group(1).strip(), clean(m.group(2))
                if not desc or re.search(r"^Coverage\s+Description$", desc, re.I):
                    continue
                if code in legend and legend[code] != desc:
                    if "basic" in desc.lower() or "ad&d" in desc.lower() or "life" in desc.lower():
                        legend[f"{code}_MED"] = legend[code]
                        legend[code] = desc
                    else:
                        legend[f"{code}_MED"] = desc
                else:
                    legend[code] = desc
    return legend


def is_peo_option_code_list(raw_val: str) -> bool:
    """True when ``raw_val`` is a comma-delimited PEO coverage option code string (e.g. '3,57,58,12')."""
    s = clean(raw_val)
    if not s or "," not in s:
        return False
    cleaned = re.sub(r"[A-Za-z\s]", "", s)
    parts = [p for p in cleaned.split(",") if p]
    return len(parts) >= 2 and all(p.isdigit() and 1 <= len(p) <= 3 for p in parts)


def decode_peo_coverage_codes(raw_val: str, legend: Optional[dict[str, str]] = None) -> tuple[str, bool]:
    """Decode a comma-delimited PEO coverage option string (e.g. '3,57,58,12', '57,58,12,32', '57,58').
    Returns ``(medical_plan_name, is_non_medical_only)``.
    If ``raw_val`` is not a comma-delimited code list, returns ``("", False)``.
    """
    if not is_peo_option_code_list(raw_val):
        return "", False
    leg = dict(_DEFAULT_PEO_LEGEND)
    if legend:
        leg.update(legend)
    cleaned = re.sub(r"[A-Za-z\s]", "", clean(raw_val))
    codes = [p for p in cleaned.split(",") if p]
    non_med_re = re.compile(r"(dental|vision|basic\s*life|basic\s*ad&d|\bad&d\b|\blife\b)", re.I)

    code_counts: dict[str, int] = {}
    for c in codes:
        code_counts[c] = code_counts.get(c, 0) + 1

    med_plans: list[str] = []
    for c in codes:
        desc = leg.get(c, "")
        if c == "58" and code_counts.get("58", 0) >= 2 and f"{c}_MED" in leg:
            med_plans.append(leg[f"{c}_MED"])
            code_counts["58"] = 0
            continue
        if desc and not non_med_re.search(desc):
            med_plans.append(desc)

    if med_plans:
        return med_plans[0], False
    return "", True


def is_valid_person_name(v: Any) -> bool:
    """Check if a string represents a plausible human name rather than OCR noise,
    form codes (e.g. 'P6T&L'), digits, or unparsed junk.
    Controlling Underwriting Rule (Sec 10.3):
    If a required value remains unavailable: Leave it blank. Do not guess.
    """
    if is_blank(v):
        return False
    s = clean(v).strip()
    if len(s) < 2:
        return False
    # If it contains digits (0-9) or code symbols (&, @, $, %, *, +, =, <, >, _, #, /, \, |, ?)
    # it is OCR noise / form grid artifacts / code, not a legitimate human name.
    if re.search(r"[\d&@\$%\*\+=\<\>_\#/\\\|\?]", s):
        return False
    # Must contain at least two alphabetic characters
    alpha_chars = [c for c in s if c.isalpha()]
    if len(alpha_chars) < 2:
        return False
    # Acceptable characters in human names: letters, spaces, hyphens, apostrophes, periods
    if not re.fullmatch(r"[A-Za-z\s\.\'\-]+", s):
        return False
    return True


def recover_ocr_name(v) -> str:
    """Validate and clean person name if valid, otherwise return empty string
    in compliance with Section 10.3 (leave blank, do not guess).
    """
    if is_blank(v):
        return ""
    s = clean(v).strip().strip("'\"[](){}<>")
    if is_valid_person_name(s):
        return " ".join(w.capitalize() for w in s.split() if w)
    return ""
