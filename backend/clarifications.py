"""Agent clarification detection (data-driven, no case-specific values).

Single source of truth for census items that must be raised with the agent in the
Missing Information Email. Both ``router.generate_missing_information_email`` and the
Learning Engine validator call these functions, so an item that is detected is always
either asked in the email or flagged as an omission by the validator.

Detected categories:
  1. UNRESOLVED_COVERAGE  - census employee whose coverage is blank or an invalid code
                            (e.g. '-1'); status/election must be confirmed.
  2. INFERRED_COVERAGE    - non-standard source code standardized heuristically
                            (e.g. 'QW' -> WO); interpretation must be confirmed.
  3. PAYROLL_NOT_ON_CENSUS - person paid on a payroll register/labor distribution but
                            absent from the census (and from the invoice).
  4. HEADCOUNT_MISMATCH   - employer-declared full-time count (GHQ/RFP) differs from
                            the census employee count.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

_CODE_LABELS = {"WO": "Waived", "RC": "Refused Coverage", "WP": "Waiting Period", "NE": "Not Eligible",
                "EE": "Employee Only", "ES": "Employee + Spouse", "EC": "Employee + Child(ren)", "FAM": "Family"}

_NAME_SUFFIX_RE = re.compile(r"(?:jr|sr|ii|iii|iv)$")
# Strong payroll person marker: "LAST,FIRST M (ID:1234)" / "Last, First (Emp ID# 12)"
_PAYROLL_ID_NAME_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z'\-\. ]{1,40}?)\s*,\s*([A-Za-z][A-Za-z'\-]{1,30})(?:\s+[A-Za-z]\.?)?\s*"
    r"\((?:emp(?:loyee)?\s*)?(?:id|#)\s*[:#]?\s*\w+\)", re.M | re.I)
_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}"
_PERIOD_RES = [
    re.compile(rf"period\s*(?:start)?\s*[-/]?\s*(?:end)?\s*(?:date)?s?\s*[:|]?\s*({_DATE})\s*(?:-|–|to|thru|through)\s*({_DATE})", re.I),
    re.compile(rf"(?:pay\s*period|period\s*(?:begin|beginning|from))\s*[:|]?\s*({_DATE}).{{0,40}}?(?:end(?:ing)?|to|thru|-)\s*[:|]?\s*({_DATE})", re.I),
]
_PAYROLL_DOC_HINT = re.compile(r"payroll|labor\s*dist|pay\s*register|earnings\s*register|pay\s*period|period\s*start", re.I)


@dataclass
class ClarificationItem:
    category: str
    employees: list[str] = field(default_factory=list)
    text: str = ""
    evidence: dict = field(default_factory=dict)


# ----------------------------------------------------------------- helpers --
def _join_names(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _norm_last(s: str) -> str:
    k = _alnum(s)
    if len(k) > 5:
        k2 = _NAME_SUFFIX_RE.sub("", k)
        if len(k2) >= 3:
            k = k2
    return k


def _first_compatible(a: str, b: str) -> bool:
    a, b = _alnum(a), _alnum(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and long_.startswith(short)


def _same_person(f1: str, l1: str, f2: str, l2: str) -> bool:
    la, lb = _norm_last(l1), _norm_last(l2)
    if not la or not lb:
        return False
    last_ok = la == lb or (min(len(la), len(lb)) >= 4 and (la in lb or lb in la)) \
        or SequenceMatcher(None, la, lb).ratio() >= 0.8
    return last_ok and _first_compatible(f1, f2)


def _to_iso_mdY(s: str) -> str:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s or "")
    if not m:
        return s
    mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000
    return f"{mo:02d}/{dy:02d}/{yr:04d}"


def _is_reportable_employee(emp: Any) -> bool:
    return not (getattr(emp, "invoice_only", False) or getattr(emp, "is_duplicate", False)
                or getattr(emp, "disposition", "") == "EXCLUDE_PART_TIME")


def _display(p: Any) -> str:
    return (f"{getattr(p, 'first', '') or ''} {getattr(p, 'last', '') or ''}").strip()


# ------------------------------------------------------- 1/2. coverage --------
def unresolved_coverage_items(census: Any) -> list[ClarificationItem]:
    """Employees with blank/invalid coverage, grouped by the source value they carried."""
    groups: dict[tuple[str, str], list[str]] = {}
    for emp in getattr(census, "employees", []) or []:
        if not _is_reportable_employee(emp):
            continue
        if (getattr(emp, "coverage", "") or "").strip():
            continue
        raw = (getattr(emp, "cov_raw", "") or "").strip()
        hdr = ((getattr(emp, "raw_data", {}) or {}).get("coverage_header") or "").strip()
        groups.setdefault((raw, hdr), []).append(_display(emp))
    items = []
    for (raw, hdr), names in groups.items():
        col = f"census {hdr}" if hdr else "census coverage level"
        each = "each employee's" if len(names) > 1 else "the employee's"
        if raw:
            lead = f"{col} is {raw} (not a valid option)"
        else:
            lead = f"{col} was left blank (coverage type not provided)"
        text = (f"{_join_names(names)}: {lead}. Please confirm {each} status "
                f"(waiting period with hire date, waived, or not eligible) and any medical election.")
        items.append(ClarificationItem("UNRESOLVED_COVERAGE", names, text, {"raw_value": raw, "header": hdr}))
    return items


def inferred_coverage_items(census: Any) -> list[ClarificationItem]:
    """Employees whose non-standard source code was heuristically standardized (e.g. QW -> WO)."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for emp in getattr(census, "employees", []) or []:
        if not _is_reportable_employee(emp):
            continue
        rd = getattr(emp, "raw_data", {}) or {}
        src = (rd.get("coverage_inferred_from") or "").strip()
        if not src or not getattr(emp, "coverage", ""):
            continue
        groups.setdefault((src, emp.coverage, (rd.get("coverage_header") or "").strip()), []).append(_display(emp))
    items = []
    for (src, code, hdr), names in groups.items():
        col = f"census {hdr}" if hdr else "census coverage level"
        label = _CODE_LABELS.get(code, code)
        verb = "these employees" if len(names) > 1 else "this employee"
        text = (f"{_join_names(names)}: {col} code '{src}' is not a standard coverage option and was recorded as "
                f"{label} ({code}). Please confirm {verb} {'waived' if code in ('WO', 'RC') else 'have this'} medical coverage.")
        items.append(ClarificationItem("INFERRED_COVERAGE", names, text, {"source_code": src, "code": code}))
    return items


# ------------------------------------------------ 3. payroll not on census ----
def _payroll_docs(roles: Optional[dict], docs: Optional[Iterable[Any]]) -> list[Any]:
    roles = roles or {}
    out = list(roles.get("payroll", []) or [])
    excluded = {id(d) for k in ("census", "invoice", "ghq", "rfp", "renewal", "sbc") for d in (roles.get(k) or [])}
    for d in docs or []:
        if d in out or id(d) in excluded:
            continue
        head = f"{getattr(d, 'name', '')}\n{(getattr(d, 'text', '') or '')[:4000]}"
        if _PAYROLL_DOC_HINT.search(head):
            out.append(d)
    return out


def _payroll_period(text: str) -> str:
    for rx in _PERIOD_RES:
        m = rx.search(text or "")
        if m:
            return f"{_to_iso_mdY(m.group(1))}-{_to_iso_mdY(m.group(2))}"
    return ""


def payroll_not_on_census_items(census: Any, roles: Optional[dict] = None, docs: Optional[Iterable[Any]] = None,
                                invoice_records: Optional[Iterable[Any]] = None,
                                extra_payroll_names: Optional[Iterable[tuple[str, str]]] = None) -> list[ClarificationItem]:
    """People paid on payroll who are on neither the census nor the medical invoice."""
    if census is None:
        return []
    known: list[tuple[str, str]] = []
    for p in list(getattr(census, "people", []) or []) + list(getattr(census, "excluded_employees", []) or []):
        if getattr(p, "invoice_only", False):
            continue
        known.append((getattr(p, "first", "") or "", getattr(p, "last", "") or ""))
        for a in getattr(p, "aliases", []) or []:
            parts = str(a).split()
            if len(parts) >= 2:
                known.append((parts[0], parts[-1]))
    billed = [(getattr(r, "first", "") or "", getattr(r, "last", "") or "") for r in (invoice_records or [])]
    if not known:
        return []

    by_period: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()

    def _consider(first: str, last: str, period: str) -> None:
        key = (_alnum(first), _norm_last(last))
        if not key[0] or not key[1] or key in seen:
            return
        seen.add(key)
        if any(_same_person(first, last, f, l) for f, l in known):
            return
        if any(_same_person(first, last, f, l) for f, l in billed):
            return  # already surfaced as 'missing from census' by the invoice comparison
        by_period.setdefault(period, []).append(f"{first.strip().title()} {last.strip().title()}")

    for d in _payroll_docs(roles, docs):
        text = getattr(d, "text", "") or ""
        period = _payroll_period(text)
        for m in _PAYROLL_ID_NAME_RE.finditer(text):
            _consider(m.group(2), m.group(1), period)
    for first, last in extra_payroll_names or []:
        _consider(first, last, "")

    items = []
    for period, names in by_period.items():
        on_what = f"the {period} payroll" if period else "the payroll register"
        text = (f"{_join_names(names)} {'are' if len(names) > 1 else 'is'} on {on_what} but not on the census. "
                f"Please confirm full-time/part-time status and, if full-time, provide census data "
                f"(DOB, gender, ZIP, medical election).")
        items.append(ClarificationItem("PAYROLL_NOT_ON_CENSUS", names, text, {"period": period}))
    return items


# ---------------------------------------------------- 4. headcount ----------
def census_full_time_count(census: Any) -> int:
    return sum(1 for e in getattr(census, "employees", []) or [] if _is_reportable_employee(e))


def headcount_items(prospect: Any, census: Any) -> list[ClarificationItem]:
    declared = getattr(prospect, "ft_employees", None) if prospect is not None else None
    if not declared or census is None:
        return []
    actual = census_full_time_count(census)
    if not actual or int(declared) == actual:
        return []
    src = (getattr(prospect, "ft_employees_source", "") or "")
    if not src or re.search(r"ghq|questionnaire|company\s*info", src, re.I):
        src_label = "the GHQ"
    elif re.search(r"census|rfp|proposal", src, re.I):
        src_label = "the GHQ/RFP business information"
    else:
        src_label = f"the {src}"
    text = (f"Headcount: {src_label} shows {int(declared)} full-time employees, but the census lists {actual}. "
            f"Please confirm the eligible employee count.")
    return [ClarificationItem("HEADCOUNT_MISMATCH", [], text, {"declared": int(declared), "census": actual, "source": src})]


# ------------------------------------------------------------ aggregate -----
def collect_clarifications(prospect: Any, census: Any, roles: Optional[dict] = None,
                           docs: Optional[Iterable[Any]] = None, invoice_records: Optional[Iterable[Any]] = None,
                           extra_payroll_names: Optional[Iterable[tuple[str, str]]] = None) -> list[ClarificationItem]:
    items: list[ClarificationItem] = []
    for fn in (lambda: unresolved_coverage_items(census),
               lambda: inferred_coverage_items(census),
               lambda: payroll_not_on_census_items(census, roles, docs, invoice_records, extra_payroll_names),
               lambda: headcount_items(prospect, census)):
        try:
            items.extend(fn())
        except Exception:  # clarification detection must never break the pipeline
            continue
    return items


def missing_from_email(items: list[ClarificationItem], email_text: str) -> list[str]:
    """Names/items that were detected but not asked in the email (omission check)."""
    low = (email_text or "").lower()
    gaps = []
    for it in items:
        if it.category == "HEADCOUNT_MISMATCH":
            if "headcount" not in low:
                gaps.append(it.text)
            continue
        for n in it.employees:
            if n.lower() not in low:
                gaps.append(f"{it.category}: {n}")
    return gaps
