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
def std_gender(v) -> str:
    s = clean(v).lower()
    if any(k in s for k in ("female", "woman", "girl")):
        return "F"
    if any(k in s for k in ("male", "man", "boy")):
        return "M"
    if s.startswith("f"):
        return "F"
    if s.startswith("m"):
        return "M"
    return ""


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
    "es": "ES", "eesp": "ES", "eespouse": "ES", "employeespouse": "ES", "employeeandspouse": "ES",
    "employeeplusspouse": "ES", "subscriberspouse": "ES", "subscriberandspouse": "ES",
    "employeesp": "ES", "esp": "ES", "empspouse": "ES", "twoparty": "ES", "couple": "ES",
    "eesd": "ES", "ees/d": "ES", "ee+s/d": "ES", "eesp/dp": "ES", "eespousedp": "ES",
    "ec": "EC", "eech": "EC", "eechild": "EC", "eechildren": "EC", "eechildren": "EC",
    "employeechild": "EC", "employeechildren": "EC", "employeeandchild": "EC",
    "employeeandchildren": "EC", "employeepluschild": "EC", "employeepluschildren": "EC",
    "employeechildren": "EC", "subscriberchild": "EC", "subscriberchildren": "EC",
    "employeechildren": "EC", "empchild": "EC", "empchildren": "EC", "eechildren": "EC",
    "fam": "FAM", "family": "FAM", "employeefamily": "FAM", "eefamily": "FAM",
    "employeeplusfamily": "FAM", "subscriberfamily": "FAM", "eefam": "FAM", "empfamily": "FAM",
    "ne": "NE", "noteligible": "NE", "ineligible": "NE", "noteligibleforcoverage": "NE",
    "wp": "WP", "waitingperiod": "WP", "waiting": "WP", "inwaitingperiod": "WP", "waitperiod": "WP",
    "rc": "RC", "refused": "RC", "refusedcoverage": "RC", "refusingcoverage": "RC",
    "declined": "RC", "refuse": "RC", "refusal": "RC",
    "wo": "WO", "waived": "WO", "waive": "WO", "waivedothercoverage": "WO",
    "waivingforothercoverage": "WO", "waiver": "WO", "optout": "WO", "optedout": "WO",
    "waivedcoverage": "WO", "otherCoverage".lower(): "WO",
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
    if re.search(r"wait", low):
        return "WP"
    if re.search(r"(waiv|opt)", low):
        return "WO"
    if re.search(r"(refus|declin)", low):
        return "RC"
    if re.search(r"(not eligible|ineligible)", low):
        return "NE"
    return ""


def std_relationship(v) -> str:
    s = clean(v).lower()
    if not s:
        return ""
    if s in {"ee", "e", "emp", "employee", "self", "subscriber", "primary", "member", "s", "insured"}:
        return "EE"
    if re.search(r"(spouse|wife|husband|partner|domestic|\bsp\b|^sp$)", s) or s in {"sp", "w", "h"}:
        return "SP"
    if re.search(r"(child|son|daughter|dependent|step|\bch\b|^ch$|^c$|^d$)", s) or s in {"ch", "c", "dep", "d"}:
        return "CH"
    return ""


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
    return None
