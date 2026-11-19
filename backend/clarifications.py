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


_NAME_SUFFIX_WORD_RE = re.compile(r"\b(?:jr|sr|ii|iii|iv)\.?$", re.I)


def _alnum(s: str) -> str:
    s_clean = _NAME_SUFFIX_WORD_RE.sub("", (s or "").strip()).strip()
    return re.sub(r"[^a-z]", "", s_clean.lower())


def _norm_last(s: str) -> str:
    s_clean = _NAME_SUFFIX_WORD_RE.sub("", (s or "").strip()).strip()
    k = re.sub(r"[^a-z]", "", s_clean.lower())
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


# The pay-period header sits at the top of a register. Scanning only a bounded, whitespace-collapsed
# slice prevents catastrophic regex backtracking on very large extracted documents (e.g. 700k-char policies).
_PERIOD_SCAN_CHARS = 6000
_NAME_SCAN_CHARS = 400_000


def _payroll_period(text: str) -> str:
    head = re.sub(r"\s+", " ", (text or "")[:_PERIOD_SCAN_CHARS])
    for rx in _PERIOD_RES:
        m = rx.search(head)
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
        text = (getattr(d, "text", "") or "")[:_NAME_SCAN_CHARS]
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
    reportable = [e for e in (getattr(census, "employees", []) or []) if _is_reportable_employee(e)]
    actual = len(reportable)
    cobra_cnt = sum(1 for e in reportable if str(getattr(e, "cobra", "") or "").upper() == "Y")
    active_cnt = actual - cobra_cnt
    if not actual or (int(declared) == actual and cobra_cnt == 0):
        return []
    src = (getattr(prospect, "ft_employees_source", "") or "")
    if not src or re.search(r"ghq|questionnaire|company\s*info", src, re.I):
        src_label = "the GHQ"
    elif re.search(r"application", src, re.I):
        from pathlib import Path as _P
        src_label = f"the {_P(src.split(',')[0].strip()).stem}"
    elif re.search(r"census|rfp|proposal", src, re.I):
        src_label = "the GHQ/RFP business information"
    else:
        src_label = f"the {src}"

    declared_pt = getattr(prospect, "pt_employees", None) if prospect is not None else None
    pt_clause = f" (and {int(declared_pt)} part-time)" if (declared_pt is not None and int(declared_pt) > 0) else ""
    if cobra_cnt > 0:
        census_clause = (
            f"{active_cnt} active employee{'s' if active_cnt != 1 else ''} + "
            f"{cobra_cnt} COBRA participant{'s' if cobra_cnt != 1 else ''} ({actual} total)"
        )
    else:
        census_clause = f"{actual}"

    text = (f"Headcount: {src_label} shows {int(declared)} full-time employees{pt_clause}, "
            f"but the census lists {census_clause}. "
            f"Please confirm the eligible employee count.")
    return [ClarificationItem("HEADCOUNT_MISMATCH", [], text, {
        "declared": int(declared),
        "declared_pt": int(declared_pt) if declared_pt is not None else 0,
        "census": actual,
        "active_employees": active_cnt,
        "cobra_participants": cobra_cnt,
        "source": src,
    })]


# ----------------------------------------- 5. cross-document DOB conflicts ---
_RENEWAL_MEMBER_LINE_RE = re.compile(
    r"^\s*([A-Z][A-Za-z'\-\. ]{1,35}?)\s*,\s*([A-Z][A-Za-z'\-\. ]{1,25}?)\s+"
    r"(-|EE|E|C|CH|S|SP|D|DP|Employee|Child|Spouse)\s+(?:\d{1,3}\s+)?(\d{1,2}/\d{1,2}/\d{2,4})\b",
    re.M,
)


def dob_discrepancy_items(
    census: Any,
    roles: Optional[dict] = None,
    docs: Optional[Iterable[Any]] = None,
    invoice_records: Optional[Iterable[Any]] = None,
) -> list[ClarificationItem]:
    """Detect and flag DOB discrepancies between the canonical census and renewal/invoice sources (P0.4)."""
    if census is None:
        return []
    people = [
        p for p in (getattr(census, "people", []) or [])
        if not getattr(p, "is_duplicate", False)
        and not getattr(p, "invoice_only", False)
        and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")
        and getattr(p, "dob", None) is not None
    ]
    if not people:
        return []

    from utils import parse_date
    roles = roles or {}
    aux_docs: list[Any] = []
    for k in ("renewal", "invoice", "ghq", "rfp"):
        for d in roles.get(k) or []:
            if d not in aux_docs:
                aux_docs.append(d)
    for d in docs or []:
        if d not in aux_docs and d not in (roles.get("census") or []) and d not in (roles.get("deliverable") or []):
            aux_docs.append(d)

    observed: list[tuple[str, str, str, date, str]] = []  # (first, last, role_hint, dob, source_label)
    for d in aux_docs:
        d_name = getattr(d, "name", "") or ""
        low_name = d_name.lower()
        if "renewal" in low_name or "proposal" in low_name:
            src_label = "the renewal proposal"
        elif "invoice" in low_name or "bill" in low_name:
            src_label = "the invoice"
        else:
            src_label = d_name or "the supporting document"
        # 1. Dynamic structured table scan across any auxiliary document (renewal, GHQ, RFP, invoice)
        from classifier import body as _tbl_body, detect_header as _detect_hdr
        from utils import clean as _clean_cell, split_full_name as _split_name, std_relationship as _std_rel
        for tbl in getattr(d, "tables", []) or []:
            hdr = _detect_hdr(tbl.df)
            if not hdr or "dob" not in hdr.columns:
                continue
            cols = hdr.columns
            if not any(k in cols for k in ("first_name", "last_name", "full_name")):
                continue
            for _, row_s in _tbl_body(tbl.df, hdr).iterrows():
                fn_c = _clean_cell(row_s.iloc[cols["first_name"]]) if "first_name" in cols else ""
                ln_c = _clean_cell(row_s.iloc[cols["last_name"]]) if "last_name" in cols else ""
                if (not fn_c or not ln_c) and "full_name" in cols:
                    f2, l2 = _split_name(_clean_cell(row_s.iloc[cols["full_name"]]))
                    fn_c, ln_c = fn_c or f2, ln_c or l2
                dt_c = parse_date(row_s.iloc[cols["dob"]])
                if not (fn_c and ln_c and dt_c):
                    continue
                rel_c = _std_rel(row_s.iloc[cols["relationship"]]) if "relationship" in cols else "EE"
                r_hint = "EE" if rel_c in ("", "EE") else rel_c
                observed.append((fn_c, ln_c, r_hint, dt_c, src_label))

        # 2. Unstructured / PDF text line scan (e.g. Renewal Proposal 'Census Details' blocks)
        txt = getattr(d, "text", "") or ""
        for m in _RENEWAL_MEMBER_LINE_RE.finditer(txt):
            last_raw, first_raw, rel_tok, dob_str = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().upper(), m.group(4).strip()
            dt = parse_date(dob_str)
            if not dt:
                continue
            r_hint = "EE" if rel_tok in ("-", "EE", "E", "EMPLOYEE") else ("SP" if rel_tok in ("S", "SP", "SPOUSE", "D", "DP") else "CH")
            observed.append((first_raw, last_raw, r_hint, dt, src_label))

    for inv_r in invoice_records or []:
        inv_dob = getattr(inv_r, "dob", None)
        if inv_dob:
            inv_d = inv_dob.date() if hasattr(inv_dob, "date") and callable(inv_dob.date) else inv_dob
            observed.append((getattr(inv_r, "first", "") or "", getattr(inv_r, "last", "") or "", "EE", inv_d, "the invoice"))

    items: list[ClarificationItem] = []
    seen_person_ids: set[int] = set()

    for first_raw, last_raw, r_hint, other_dob, src_label in observed:
        cands = [p for p in people if _same_person(first_raw, last_raw, p.first, p.last)]
        if not cands:
            continue
        if r_hint == "EE":
            role_cands = [p for p in cands if p.role == "EE"] or cands
        else:
            role_cands = [p for p in cands if p.role != "EE"] or cands
        if len(role_cands) > 1:
            role_cands.sort(key=lambda p: (
                0 if (p.dob.month == other_dob.month and p.dob.day == other_dob.day) else 1,
                abs(p.dob.year - other_dob.year),
            ))
        target = role_cands[0]
        if id(target) in seen_person_ids:
            continue
        p_dob = target.dob.date() if hasattr(target.dob, "date") and callable(target.dob.date) else target.dob
        if p_dob == other_dob:
            continue

        seen_person_ids.add(id(target))
        if target.role != "EE" and getattr(target, "employee", None) is not None:
            sub_disp = _display(target.employee)
            who_label = f"{_display(target)} (dependent of {sub_disp})"
            emp_names = [_display(target.employee), _display(target)]
        else:
            who_label = _display(target)
            emp_names = [who_label]

        note_txt = f"DOB Discrepancy: Census shows {p_dob:%m/%d/%Y} vs {src_label} {other_dob:%m/%d/%Y} - confirm correct DOB"
        if hasattr(target, "note"):
            target.note(note_txt, red=True)
        text = (
            f"{who_label}: Date of birth discrepancy between the submitted census ({p_dob:%m/%d/%Y}) "
            f"and {src_label} ({other_dob:%m/%d/%Y}). Please confirm the correct date of birth."
        )
        items.append(ClarificationItem("DOB_DISCREPANCY", emp_names, text, {
            "person": who_label,
            "census_dob": f"{p_dob:%m/%d/%Y}",
            "other_dob": f"{other_dob:%m/%d/%Y}",
            "source": src_label,
        }))

    return items


def gender_discrepancy_items(census: Any) -> list[ClarificationItem]:
    """Flag any canonical census member whose gender has an unresolved conflict across sources (Upgrade 4)."""
    if census is None:
        return []
    items: list[ClarificationItem] = []
    for p in getattr(census, "people", []) or []:
        if getattr(p, "is_duplicate", False) or getattr(p, "invoice_only", False):
            continue
        if str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_"):
            continue
        if getattr(p, "gender_resolution_status", "") == "CONFLICT":
            who_label = _display(p)
            cands = getattr(p, "gender_candidates", []) or []
            vals = sorted({c.get("normalized") for c in cands if c.get("normalized") in ("M", "F")})
            val_str = "/".join(vals) if vals else "conflicting values"
            text = f"{who_label}: Conflicting gender values ({val_str}) found across submission sources. Please confirm the correct gender."
            items.append(ClarificationItem("GENDER_DISCREPANCY", [who_label], text, {"person": who_label, "values": vals}))
    return items


# ----------------------------------------- 6. invoice validity / recency -----
_EMAIL_SENT_DATE_RE = re.compile(
    r"Sent\s*:\s*(?:[A-Za-z]+,\s*)?([A-Za-z]+\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2,4})",
    re.I,
)


def invoice_recency_items(
    selected_invoices: Optional[Iterable[Any]] = None,
    prospect: Any = None,
    roles: Optional[dict] = None,
    docs: Optional[Iterable[Any]] = None,
) -> list[ClarificationItem]:
    """Flag selected invoices that are older than 90 days relative to submission/effective date (Section 11.2 / P1.7)."""
    if not selected_invoices:
        return []
    from utils import parse_date
    sent_date: Optional[date] = None
    all_scan_docs = list(docs or [])
    if roles:
        for d_list in roles.values():
            for d in d_list or []:
                if d not in all_scan_docs:
                    all_scan_docs.append(d)
    for d in all_scan_docs:
        m = _EMAIL_SENT_DATE_RE.search(getattr(d, "text", "") or "")
        if m:
            dt = parse_date(m.group(1))
            if dt:
                sent_date = dt
                break

    eff_date = getattr(prospect, "effective_date", None) if prospect is not None else None
    if hasattr(eff_date, "date") and callable(eff_date.date):
        eff_date = eff_date.date()
    ref_date = sent_date or eff_date
    if not ref_date:
        return []

    items: list[ClarificationItem] = []
    for inv_doc in selected_invoices:
        p_start = getattr(inv_doc, "period_start", None)
        p_end = getattr(inv_doc, "period_end", None)
        d_date = getattr(inv_doc, "doc_date", None)
        inv_ref = p_end or p_start or d_date
        if not inv_ref:
            continue
        if hasattr(inv_ref, "date") and callable(inv_ref.date):
            inv_ref = inv_ref.date()
        days_old = (ref_date - inv_ref).days
        if days_old > 90:
            doc_dt_part = f"is dated {d_date:%m/%d/%Y} " if d_date else ""
            if p_start and p_end:
                cov_part = f"with coverage {p_start:%m/%d/%Y}–{p_end:%m/%d/%Y}"
            elif p_start:
                cov_part = f"with coverage starting {p_start:%m/%d/%Y}"
            else:
                cov_part = ""
            inv_name = getattr(inv_doc, "path", "") or "the submitted invoice"
            text = (
                f"Invoice Validity: The submitted invoice ({inv_name}) {doc_dt_part}{cov_part}, "
                f"which is more than 90 days old. Please provide an updated current medical invoice."
            ).replace(" ,", ",").replace("  ", " ")
            items.append(ClarificationItem("OUTDATED_INVOICE", [], text, {
                "invoice": inv_name,
                "doc_date": f"{d_date:%m/%d/%Y}" if d_date else "",
                "period_start": f"{p_start:%m/%d/%Y}" if p_start else "",
                "period_end": f"{p_end:%m/%d/%Y}" if p_end else "",
                "days_old": days_old,
            }))
    return items


# --------------------- 7. legal-entity mismatch & missing SIC ----------------
_CORP_STOPWORDS = {
    "llc", "inc", "incorporated", "corp", "corporation", "ltd", "lp", "llp", "pc", "pa",
    "pllc", "co", "company", "group", "the", "and", "of", "dba", "services", "service",
    "enterprises", "holdings", "international", "usa",
}


def _entity_tokens(name: str) -> set[str]:
    from utils import clean
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z0-9]{2,}", clean(name))
        if t.lower() not in _CORP_STOPWORDS
    }


def _entities_compatible(a: str, b: str) -> bool:
    ta, tb = _entity_tokens(a), _entity_tokens(b)
    if not ta or not tb:
        return True
    if ta & tb:
        return True
    raw_a = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    raw_b = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    if any(len(t) >= 4 and t in raw_b for t in ta) or any(len(t) >= 4 and t in raw_a for t in tb):
        return True
    return False


def legal_entity_mismatch_items(
    prospect: Any,
    roles: Optional[dict] = None,
    docs: Optional[Iterable[Any]] = None,
) -> list[ClarificationItem]:
    """Detect when the prospect legal entity name (from RFP/email/GHQ) differs from the entity/group name
    on the submitted carrier census, invoice, or renewal documents."""
    from utils import clean, norm_key
    if prospect is None:
        return []
    p_comp = clean(getattr(prospect, "company", "") or "")
    p_dba = clean(getattr(prospect, "dba", "") or "")
    if not p_comp:
        return []

    roles = roles or {}
    observed_entities: list[tuple[str, str]] = []  # (entity_name, source_label)

    # 1. Check census table 'Company Name' / 'Legal Entity' / 'Employer Name' columns
    for d in roles.get("census") or []:
        for tbl in getattr(d, "tables", []) or []:
            df = getattr(tbl, "df", None)
            if df is None or df.empty:
                continue
            _FORM_LABELS = {
                "companyname", "nan", "none", "address", "streetaddress", "city", "state",
                "zip", "citystatezip", "taxid", "ein", "fein", "industry", "sic", "naics",
                "renewaldate", "effectivedate", "date", "phone", "email", "contact",
            }
            for r_i in range(min(12, len(df))):
                row_vals = [clean(v) for v in df.iloc[r_i].tolist()]
                comp_col = next(
                    (c_i for c_i, lab in enumerate(row_vals)
                     if norm_key(lab) in ("companyname", "legalentity", "legalentityname", "employername", "groupname", "clientname")),
                    None,
                )
                if comp_col is not None:
                    raw_hdr = row_vals[comp_col]
                    next_row_val = clean(df.iloc[r_i + 1, comp_col]) if (r_i + 1 < len(df)) else ""
                    is_row_banner = (
                        raw_hdr.rstrip().endswith(":")
                        or next_row_val.rstrip().endswith(":")
                        or norm_key(next_row_val) in _FORM_LABELS
                    )
                    if is_row_banner:
                        val = next((v for v in row_vals[comp_col + 1:] if v and len(v) >= 4 and not v.rstrip().endswith(":") and norm_key(v) not in _FORM_LABELS), "")
                        if val:
                            observed_entities.append((val, "census"))
                        break
                    if r_i + 1 < len(df):
                        for data_i in range(r_i + 1, min(r_i + 15, len(df))):
                            val = clean(df.iloc[data_i, comp_col])
                            if val and len(val) >= 4 and not val.rstrip().endswith(":") and norm_key(val) not in _FORM_LABELS:
                                observed_entities.append((val, "census"))
                                break
                    break

    # 2. Check medical invoice group / customer headers
    for d in roles.get("invoice") or []:
        txt = getattr(d, "text", "") or ""
        m_grp = re.search(
            r"\b\d{5,9}\s*-\s*([A-Z][A-Z0-9\s,\.&'\-]{4,60}?\b(?:INC|LLC|CORP|LTD|LP|PC|PA|PLLC|CENTER|COMPANY)\b)",
            txt,
        )
        if m_grp:
            observed_entities.append((clean(m_grp.group(1)), "medical invoice"))
            continue
        m_cust = re.search(
            r"^\s*([A-Z][A-Z0-9\s,\.&'\-]{4,60}?\b(?:INC|LLC|CORP|LTD|LP|PC|PA|PLLC)\b)\s*\n\s*Customer\s*No\s*:",
            txt,
            re.M,
        )
        if m_cust:
            observed_entities.append((clean(m_cust.group(1)), "medical invoice"))

    # 3. Check renewal customer / employer name headers
    for d in roles.get("renewal") or []:
        txt = getattr(d, "text", "") or ""
        m_ren = re.search(r"Customer\s*Name\s*:\s*([A-Za-z0-9\s,\.&'\-]+?)(?:\s+State\s*:|\n|$)", txt, re.I)
        if m_ren and len(clean(m_ren.group(1))) >= 4:
            observed_entities.append((clean(m_ren.group(1)), "renewal"))

    mismatched: list[tuple[str, str]] = []
    for ent, src_lbl in observed_entities:
        if not _entities_compatible(p_comp, ent) and (not p_dba or not _entities_compatible(p_dba, ent)):
            mismatched.append((ent, src_lbl))

    if not mismatched:
        return []

    # Prefer a well-spaced entity name for display (e.g. 'RALEIGH HAND REHABILITATION CENTER, INC')
    spaced_cands = [ent for ent, _ in mismatched if " " in ent]
    best_ent = max(spaced_cands, key=len) if spaced_cands else mismatched[0][0]
    src_labels = []
    for _, s_lbl in mismatched:
        if s_lbl not in src_labels:
            src_labels.append(s_lbl)
    src_str = _join_names(src_labels) if src_labels else "carrier documents"
    text = (
        f"Legal Entity Mismatch: The submission identifies the prospect legal entity as {p_comp}, "
        f"whereas the submitted {src_str} list {best_ent}. Please clarify the legal-entity relationship "
        f"(e.g., DBA, parent/subsidiary, or common ownership) and confirm the applying legal entity."
    )
    return [ClarificationItem("LEGAL_ENTITY_MISMATCH", [], text, {
        "prospect_company": p_comp,
        "document_entity": best_ent,
        "sources": src_labels,
    })]


def missing_sic_items(prospect: Any) -> list[ClarificationItem]:
    """Flag when the prospect's 4-digit SIC code is missing from the submission."""
    from utils import clean
    if prospect is None:
        return []
    p_comp = clean(getattr(prospect, "company", "") or "")
    p_sic = clean(getattr(prospect, "sic", "") or "")
    if not p_comp or p_sic:
        return []
    text = (
        f"Missing SIC Code: The 4-digit Standard Industrial Classification (SIC) code for {p_comp} "
        f"was not provided in the submission. Please provide the prospect's 4-digit SIC code."
    )
    return [ClarificationItem("MISSING_SIC", [], text, {"company": p_comp})]


# ------------------------------------------------------------ aggregate -----
def collect_clarifications(prospect: Any, census: Any, roles: Optional[dict] = None,
                           docs: Optional[Iterable[Any]] = None, invoice_records: Optional[Iterable[Any]] = None,
                           extra_payroll_names: Optional[Iterable[tuple[str, str]]] = None,
                           selected_invoices: Optional[Iterable[Any]] = None) -> list[ClarificationItem]:
    items: list[ClarificationItem] = []
    for fn in (lambda: legal_entity_mismatch_items(prospect, roles, docs),
               lambda: missing_sic_items(prospect),
               lambda: unresolved_coverage_items(census),
               lambda: inferred_coverage_items(census),
               lambda: payroll_not_on_census_items(census, roles, docs, invoice_records, extra_payroll_names),
               lambda: headcount_items(prospect, census),
               lambda: dob_discrepancy_items(census, roles, docs, invoice_records),
               lambda: gender_discrepancy_items(census),
               lambda: invoice_recency_items(selected_invoices, prospect, roles, docs)):
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
        if it.category == "LEGAL_ENTITY_MISMATCH":
            if "legal entity" not in low and "legal-entity" not in low:
                gaps.append(it.text)
            continue
        if it.category == "MISSING_SIC":
            if "sic" not in low:
                gaps.append(it.text)
            continue
        if it.category == "HEADCOUNT_MISMATCH":
            if "headcount" not in low:
                gaps.append(it.text)
            continue
        if it.category == "OUTDATED_INVOICE":
            if "updated" not in low and "90 days" not in low and "invoice validity" not in low:
                gaps.append(it.text)
            continue
        for n in it.employees:
            if n.lower() not in low:
                gaps.append(f"{it.category}: {n}")
    return gaps
