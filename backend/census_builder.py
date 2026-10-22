"""Build ONE standardised census from every uploaded census / enrollment / dependent file."""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from classifier import Header, body, detect_header
from models import Census, Person
from readers import RawDoc
from utils import (clean, display_name, is_blank, name_key, parse_date, split_full_name,
                   std_cobra, std_coverage, std_gender, std_relationship, std_zip, to_money)

log = logging.getLogger("census.builder")


@dataclass
class _DepRef:
    """A dependent waiting to be linked to its employee."""
    person: Person
    sub_id: str = ""
    emp_key: str = ""
    positional: Optional[Person] = None     # canonical employee from the same row/table position
    file: str = ""


@dataclass
class _State:
    census: Census = field(default_factory=Census)
    by_id: dict = field(default_factory=dict)
    by_nd: dict = field(default_factory=dict)       # (name_key, dob) -> Person
    by_name: dict = field(default_factory=dict)     # name_key -> [Person]
    files_of: dict = field(default_factory=dict)    # id(Person) -> set(files)
    employees: list = field(default_factory=list)
    dep_refs: list = field(default_factory=list)
    has_cov_col: bool = False
    has_hire_col: bool = False
    hire_missing: list = field(default_factory=list)
    saw_dependent_source: bool = False


# --------------------------------------------------------------------- row --
class _Row:
    def __init__(self, hdr: Header, series: pd.Series):
        self.h, self.s = hdr, series

    def g(self, f: str):
        i = self.h.columns.get(f)
        return None if i is None else self.s.iloc[i]

    def t(self, f: str) -> str:
        return clean(self.g(f))


def _is_masked_or_empty(s: str) -> bool:
    if not s:
        return True
    s_clean = s.strip()
    if not s_clean:
        return True
    if re.fullmatch(r"[*Xx\s\-./#?]+", s_clean):
        return True
    if s_clean.upper() in {"N/A", "NA", "NONE", "UNKNOWN", "NOT APPLICABLE", "NULL"}:
        return True
    return False


def _is_non_medical(plan_name: str, plan_type: str = "") -> bool:
    combined = f"{clean(plan_name)} {clean(plan_type)}".lower()
    non_med_words = ("dent", "vis", "life", "lif", "disab", "ltd", "std", "accid", "hsa", "fsa", "legal", "pet", "indemn", "crit", "identity", "ad&d", "spending")
    # Strong, unambiguous non-medical line signals take priority: provider/plan codes such as
    # AETDEN / AET-DMO DEN / VSP / METLDI LIF / METLDI LTD. These override weak tokens like 'ppo'
    # that appear inside dental provider codes (e.g. 'AET-APPO').
    if re.search(r"\b(den|dmo|dmd|dnt|vsp|wis|ltl)\b", combined):
        return True
    if re.search(r"\blif\b|\bltd\b|\bstd\b", combined):
        return True
    if any(w in combined for w in non_med_words):
        if "medical" not in combined:
            return True
    pt = clean(plan_type).lower().strip()
    if pt:
        if pt == "medical":
            return False
        if any(w in pt for w in ("dent", "vis", "life", "disab", "ltd", "std", "accid", "hsa", "fsa", "legal", "pet", "indemn", "crit")):
            return True
        if "med" in pt or "health" in pt:
            return False
        return True
    pn = clean(plan_name).lower()
    if pn:
        med_keywords = ("med", "health", "hmo", "ppo", "pos", "epo", "hdhp", "cdhp", "open access")
        if any(k in pn for k in non_med_words) and not any(k in pn for k in med_keywords):
            return True
    return False


def _fill(person: Person, **kw):
    for k, v in kw.items():
        if v in (None, ""):
            continue
        curr = getattr(person, k, None)
        if not curr or (k == "coverage" and str(curr).lower() in ("enrolled", "active", "yes") and str(v).lower() not in ("enrolled", "active", "yes")):
            setattr(person, k, v)


def _standardise(p: Person, row_vals: dict, is_employee: bool, st: _State) -> None:
    """Apply template standards + validation notes to a freshly parsed person."""
    g_raw = row_vals.get("gender", "")
    p.gender = std_gender(g_raw)
    if g_raw and not p.gender:
        p.note(f"Invalid Gender Value: '{g_raw}'")
    d_raw = row_vals.get("dob")
    p.dob = parse_date(d_raw)
    if not is_blank(d_raw) and not p.dob:
        p.note(f"Invalid Date of Birth: '{clean(d_raw)}'")
    elif not p.dob:
        p.note("Missing Date of Birth")
    p.zip, ok = std_zip(row_vals.get("zip"))
    if not ok:
        p.note(f"Invalid ZIP Format: '{p.zip}'")
    # "Medical/Plan/Election/Tier/Coverage" column containing EE/ES/EC/EF/WO/WP/NE
    # but the header says "PLAN" or "MEDICAL PLAN" - scan the row values for coverage codes.
    cov = row_vals.get("coverage") if row_vals.get("coverage") is not None else ""
    if (not clean(cov)) and is_employee:
        for k in ("plan", "status"):
            v = row_vals.get(k)
            s = clean(v)
            if s and std_coverage(s):
                row_vals = dict(row_vals)
                row_vals["coverage"] = s
                break
    p.cov_raw = clean(cov)
    p.coverage = std_coverage(cov)
    if is_employee and clean(cov) and not p.coverage and clean(cov).lower() not in ("enrolled", "active", "yes"):
        p.note(f"Invalid Coverage Code: '{clean(cov)}'", red=True)
    if is_employee:
        p.cobra = std_cobra(row_vals.get("cobra")) or "N"
        p.plan = clean(row_vals.get("plan"))
        p.premium = to_money(row_vals.get("premium"))
        p.hire_date = parse_date(row_vals.get("hire"))
        if st.has_hire_col and not p.hire_date:
            p.note("Missing Hire Date")
        p.effective_date = parse_date(row_vals.get("effective_date"))
        p.eligibility_class = clean(row_vals.get("eligibility_class"))
        p.worker_category = clean(row_vals.get("worker_category"))
        p.position_status = clean(row_vals.get("position_status"))
        p.waive_reason = clean(row_vals.get("waive_reason"))
        p.job_title = clean(row_vals.get("job_title"))
    else:
        p.cobra = std_cobra(row_vals.get("cobra")) or "N"

    if not p.first or not p.last:
        p.note("Missing Employee Name" if is_employee else "Missing Dependent Name")


def _eligibility_vals(r: _Row) -> dict:
    """Authoritative benefit-eligibility columns (classification evidence). Optional - only present
    when the source file carries them, so downstream logic can rely on their presence rather than
    guessing a coverage status."""
    out = {}
    for key, fld in (("eligibility_class", "eligibility_class"), ("worker_category", "worker_category"),
                     ("position_status", "position_status"), ("waive_reason", "waive_reason"),
                     ("job_title", "job_title")):
        v = r.g(key)
        if v is not None and not is_blank(v):
            out[fld] = v
    return out


_DISCLAIMER_WORDS = re.compile(
    r"\b(see\s+(attached|dep\w*|doc\w*|file|schedule|list|roster|page|tab|sheet|above|below|separate\w*)|"
    r"separately\s+attached|attached\s+separately|refer\s+to|reference\s+to|"
    r"no\s+dependents?|none\s+enrolled|not\s+applicable|as\s+per|enclosed|"
    r"placeholder|instructions?|footnote|disclaimer)\b",
    re.I
)

_JUNK_NAME_WORDS = {"required", "optional", "first", "firstname", "first name", "ee", "sp", "ch",
                    "see", "separately", "attached", "document", "none", "notes", "note", "n/a", "na",
                    "last", "lastname", "last name", "name"}


def _names(r: _Row, first_f="first_name", last_f="last_name", full_f="full_name") -> tuple[str, str]:
    first, last = r.t(first_f), r.t(last_f)
    if (not first or not last) and r.t(full_f) and not (first and last):
        f2, l2 = split_full_name(r.t(full_f))
        first, last = first or f2, last or l2
    full = f"{first} {last}".strip()
    if not full:
        return "", ""
    if _DISCLAIMER_WORDS.search(full):
        return "", ""
    if first.lower() in _JUNK_NAME_WORDS and last.lower() in _JUNK_NAME_WORDS:
        return "", ""
    return first, last


# ----------------------------------------------------------- registration ---
def _register_employee(p: Person, fname: str, st: _State, is_dep_listing: bool = False) -> Person:
    """Merge across files, flag same-file duplicates, return the canonical Person."""
    nk = name_key(p.first, p.last)
    existing = None
    if p.sub_id and p.sub_id in st.by_id:
        existing = st.by_id[p.sub_id]
    elif (nk, p.dob) in st.by_nd and p.dob:
        existing = st.by_nd[(nk, p.dob)]
    elif nk and not p.dob and st.by_name.get(nk):
        existing = st.by_name[nk][0]
    elif nk and p.dob and st.by_name.get(nk):
        cand = [e for e in st.by_name[nk] if not e.dob]
        existing = cand[0] if cand else None

    if existing is not None:
        files = st.files_of[id(existing)]
        if fname in files:
            if is_dep_listing:
                # Same subscriber listing multiple dependents/benefits in enrollment roster
                _fill(existing, plan=p.plan, coverage=p.coverage, premium=p.premium, effective_date=p.effective_date)
                return existing
            # same file => duplicate employee
            p.note("Duplicate Employee", red=True)
            p.is_duplicate = True
            existing.note("Duplicate Employee", red=True)
            st.census.raw_employee_rows += 1
            st.census.duplicate_employees += 1
            st.files_of[id(p)] = {fname}
            st.employees.append(p)
            st.census.people.append(p)
            return existing                                  # dependents link to the first record
        st.census.raw_employee_rows += 1
        st.census.cross_file_merges += 1                     # same person in another census version
        if existing.coverage and p.coverage and existing.coverage != p.coverage:
            existing.note(f"Coverage differs across census files ({existing.coverage} vs {p.coverage}); "
                          f"first file kept")
        _fill(existing, first=p.first, last=p.last, gender=p.gender, dob=p.dob, zip=p.zip,
              coverage=p.coverage, cobra=p.cobra, plan=p.plan, premium=p.premium,
              sub_id=p.sub_id, hire_date=p.hire_date, effective_date=p.effective_date)
        if not existing.dob and p.dob:
            existing.notes = [n for n in existing.notes if n[0] != "Missing Date of Birth"]
        files.add(fname)
        return existing

    st.census.raw_employee_rows += 1
    st.files_of[id(p)] = {fname}
    st.employees.append(p)
    st.census.people.append(p)
    if p.sub_id:
        st.by_id[p.sub_id] = p
    st.by_nd[(nk, p.dob)] = p
    st.by_name.setdefault(nk, []).append(p)
    return p


# ------------------------------------------------------------ table parse ---
def _parse_table(doc: RawDoc, tname: str, df: pd.DataFrame, st: _State) -> bool:
    if any(w in tname.lower() for w in ["note", "company", "info", "summary", "population"]):
        for _, row in df.iterrows():
            text_vals = [clean(v) for v in row.dropna() if clean(v)]
            if text_vals:
                st.census.census_notes.append(" | ".join(text_vals))

    hdr = detect_header(df)
    if not hdr:
        return False
    cols = hdr.columns
    has_name = any(k in cols for k in ("first_name", "last_name", "full_name", "dep_first", "dep_last", "dep_full"))
    if not has_name:
        return False
    # invoice-looking tables (premium + plan but no dob/coverage) are not census tables
    if "premium" in cols and "dob" not in cols and "coverage" not in cols and not hdr.wide_deps:
        return False

    st.has_cov_col |= "coverage" in cols
    st.has_hire_col |= "hire_date" in cols
    dep_listing = any(k in cols for k in ("dep_first", "dep_last", "dep_full"))
    # A table that carries plan / eligibility-class structure is a benefit-eligibility roster:
    # it enumerates the population the plan actually covers, so absence from it is meaningful.
    is_roster = any(k in cols for k in ("eligibility_class", "plan", "plan_type"))
    fname = doc.name
    last_emp: Optional[Person] = None
    used = False

    for _, series in body(df, hdr).iterrows():
        r = _Row(hdr, series)
        non_empty = [clean(v) for v in series.tolist() if clean(v)]
        if not non_empty:
            continue
        joined = " ".join(v.lower() for v in non_empty)
        if joined.startswith(("total", "grand total", "count", "subtotal")):
            continue
        if _DISCLAIMER_WORDS.search(joined) and len(non_empty) <= 3:
            st.census.census_notes.append(" | ".join(non_empty))
            continue

        pt_val = r.t("plan_type") if "plan_type" in cols else ""
        p_val = r.t("plan") if "plan" in cols else ""
        is_non_med = bool((pt_val or p_val) and _is_non_medical(p_val, pt_val))
        if dep_listing:
            # Table has dependent columns (dep_first, dep_last, etc.)
            ef, el = _names(r)
            # generic DOB/Gender columns describe the employee when the row's primary name columns name the employee
            demo_is_employee = bool(ef or el)
            if not (ef or el) and any(k in cols for k in ("emp_first", "emp_last", "emp_full")):
                ef, el = _names(r, "emp_first", "emp_last", "emp_full")

            canon = None
            if ef or el:
                values = {"gender": r.t("gender"), "dob": r.g("dob"), "zip": r.g("zip"),
                          "hire": r.g("hire_date"), "status": r.g("status")}
                values.update(_eligibility_vals(r))
                if not is_non_med:
                    prem = r.g("premium")
                    if prem is None and ("ee_cost" in cols or "er_cost" in cols):
                        ee_c = to_money(r.g("ee_cost")) or 0.0
                        er_c = to_money(r.g("er_cost")) or 0.0
                        if ee_c > 0 or er_c > 0:
                            prem = round(ee_c + er_c, 2)
                    values.update({"coverage": r.g("coverage"), "cobra": r.g("cobra"),
                                   "plan": r.g("plan"), "premium": prem, "effective_date": r.g("effective_date")})
                rel_raw = r.t("relationship")
                p_emp = Person("EE", ef, el, source=fname, sub_id=r.t("sub_id"), rel_raw=rel_raw)
                _standardise(p_emp, values, True, st)
                canon = _register_employee(p_emp, fname, st, is_dep_listing=True)
                canon.benefit_roster = canon.benefit_roster or is_roster
                last_emp = canon
                used = True

            if not is_non_med:
                dfirst, dlast = _names(r, "dep_first", "dep_last", "dep_full")
                if not (_is_masked_or_empty(dfirst) and _is_masked_or_empty(dlast)):
                    rel_raw = r.t("relationship")
                    p = Person("CH", dfirst, dlast, source=fname, rel_raw=rel_raw)
                    if demo_is_employee:
                        d_gender = r.t("dep_gender") if "dep_gender" in cols else ""
                        d_dob = r.g("dep_dob") if "dep_dob" in cols else None
                    else:
                        d_gender = r.t("dep_gender") or r.t("gender")
                        d_dob = r.g("dep_dob") if r.g("dep_dob") is not None else r.g("dob")
                    _standardise(p, {"gender": d_gender, "dob": d_dob, "zip": r.g("zip")}, False, st)
                    rel = std_relationship(rel_raw)
                    if rel in {"SP", "CH"}:
                        p.role = rel
                    else:
                        p.role = "CH"
                        p.note("Missing Dependent Relationship" if not rel_raw else
                               f"Unrecognised Relationship: '{rel_raw}'")
                    emp_ref = name_key(ef, el) or name_key(*split_full_name(r.t("emp_full")))
                    ref = _DepRef(p, sub_id=r.t("sub_id"), emp_key=emp_ref, positional=last_emp, file=fname)
                    st.dep_refs.append(ref)
                    st.census.raw_dependent_rows += 1
                    st.saw_dependent_source = True
                    used = True
            continue

        if is_non_med:
            # Register employee demographics without setting non-medical plan/coverage
            first, last = _names(r)
            if first or last:
                values = {"gender": r.t("gender"), "dob": r.g("dob"), "zip": r.g("zip"),
                          "hire": r.g("hire_date"), "status": r.g("status"), "effective_date": r.g("effective_date")}
                values.update(_eligibility_vals(r))
                p_emp = Person("EE", first, last, source=fname, sub_id=r.t("sub_id"))
                _standardise(p_emp, values, True, st)
                last_emp = _register_employee(p_emp, fname, st)
                last_emp.benefit_roster = last_emp.benefit_roster or is_roster
                used = True
            continue

        first, last = _names(r)
        if not (first or last):
            continue
        rel_raw = r.t("relationship")
        rel = std_relationship(rel_raw) if "relationship" in cols else "EE"
        if "relationship" in cols and rel_raw and not rel:
            rel = "?"            # present but unrecognised -> treated as a dependent w/o relationship
        if "relationship" in cols and not rel_raw:
            rel = "EE"

        prem = r.g("premium")
        if prem is None and ("ee_cost" in cols or "er_cost" in cols):
            ee_c = to_money(r.g("ee_cost")) or 0.0
            er_c = to_money(r.g("er_cost")) or 0.0
            if ee_c > 0 or er_c > 0:
                prem = round(ee_c + er_c, 2)
        values = {"gender": r.t("gender"), "dob": r.g("dob"), "zip": r.g("zip"),
                  "coverage": r.g("coverage"), "cobra": r.g("cobra"), "plan": r.g("plan"),
                  "premium": prem, "hire": r.g("hire_date"), "status": r.g("status"),
                  "effective_date": r.g("effective_date")}
        values.update(_eligibility_vals(r))

        if rel in {"SP", "CH", "?"}:
            st.saw_dependent_source = True
            p = Person("CH" if rel == "?" else rel, first, last, source=fname, rel_raw=rel_raw)
            _standardise(p, values, False, st)
            if rel == "?":
                p.note(f"Unrecognised Relationship: '{rel_raw}'")
            ef, el = clean(r.g("emp_first")), clean(r.g("emp_last"))
            emp_ref = name_key(ef, el) or name_key(*split_full_name(r.t("emp_full")))
            # when first/last hold the dependent, a separate full_name column names the employee
            if not emp_ref and r.t("full_name") and r.t("first_name") and r.t("last_name"):
                emp_ref = name_key(*split_full_name(r.t("full_name")))
            st.dep_refs.append(_DepRef(p, sub_id=r.t("sub_id"), emp_key=emp_ref,
                                       positional=last_emp, file=fname))
            st.census.raw_dependent_rows += 1
            used = True
            continue

        # ---- employee row
        p = Person("EE", first, last, source=fname, sub_id=r.t("sub_id"), rel_raw=rel_raw)
        _standardise(p, values, True, st)
        canon = _register_employee(p, fname, st)
        canon.benefit_roster = canon.benefit_roster or is_roster
        last_emp = canon
        used = True

        # wide-format dependents on the employee row
        for (kind, n), flds in sorted(hdr.wide_deps.items()):
            gv = lambda k: clean(series.iloc[flds[k]]) if k in flds else ""
            dfirst, dlast = gv("first"), gv("last")
            if not (dfirst or dlast) and gv("name"):
                dfirst, dlast = split_full_name(gv("name"))
            ddob = series.iloc[flds["dob"]] if "dob" in flds else None
            if not (dfirst or dlast or not is_blank(ddob)):
                continue
            if _is_masked_or_empty(dfirst) and _is_masked_or_empty(dlast):
                continue
            dp = Person(kind, dfirst, dlast, source=fname, rel_raw=kind)
            _standardise(dp, {"gender": gv("gender"), "dob": ddob}, False, st)
            st.dep_refs.append(_DepRef(dp, positional=canon, file=fname))
            st.census.raw_dependent_rows += 1
            st.saw_dependent_source = True
    return used


# ------------------------------------------------------------ dependents ----
# A plan states its waiting period in one of two structural forms:
#   * a number of days     -> 'Waiting Period 30 Days'
#   * a calendar rule      -> 'Waiting Period 1st of Month > DOH'
# Legal/ACA wording ('a waiting period will be no greater than 60 days') CAPS the period but does
# not declare it, so it must never be read as the plan's waiting period.
_WAIT_LIMIT_RE = re.compile(
    r"(?:no\s+greater\s+than|not\s+to\s+exceed|not\s+more\s+than|no\s+more\s+than|"
    r"maximum|max\.|up\s+to|less\s+than|fewer\s+than|limitation)", re.I)

# Tight adjacency forms: the declared value must follow its label directly.
_WAIT_RULE_RES = (
    re.compile(r"wait(?:ing)?\s*period\s*[:=\-]?\s*1st\s+of\s+(?:the\s+)?month\s*>\s*"
               r"(?:DOH|Date\s*of\s*Hire)", re.I),
    re.compile(r"wait(?:ing)?\s*period\s*[:=\-]?\s*first\s+of\s+(?:the\s+)?month\s+"
               r"(?:following|after)\s+(?:the\s+)?(?:date\s*of\s*hire|DOH)", re.I),
)
_WAIT_DAYS_RES = (
    re.compile(r"wait(?:ing)?\s*period\s*[:=\-]?\s*(\d{1,3})\s*days?\b", re.I),
    re.compile(r"(\d{1,3})[-\s]*day\s+wait(?:ing)?\s*period", re.I),
)


def extract_waiting_period_rule(docs: list[RawDoc]) -> Optional[dict]:
    """Read the plan's declared waiting-period rule from plan-design text.

    Returns {"kind": "days", "days": N} for a numeric period, {"kind": "first_of_month"} for a
    calendar rule ('1st of Month > DOH' = the first day of the month after the hire month), or
    None when the submission states no waiting period. Both forms are structural: the value and
    the rule are read from the submission, never assumed.
    """
    texts = [d.text or "" for d in docs if (d.text or "").strip()]

    # 1) Calendar rule - the most specific declaration, so it wins over any numeric wording.
    for text in texts:
        for pat in _WAIT_RULE_RES:
            if pat.search(text):
                return {"kind": "first_of_month", "days": None}

    # 2) Numeric period. Prefer a value that sits directly beside its label; fall back to a
    #    looser search that ignores ACA-style limits ('no greater than 60 days').
    best = None
    for text in texts:
        for pat in _WAIT_DAYS_RES:
            for m in pat.finditer(text):
                gap = text[max(0, m.start() - 12):m.start()]
                if _WAIT_LIMIT_RE.search(gap):
                    continue
                v = int(m.group(1))
                if 0 < v <= 365:
                    best = v if best is None else min(best, v)
    if best is not None:
        return {"kind": "days", "days": best}

    # 3) Looser numeric search, rejecting any match whose gap states a limit.
    for text in texts:
        for m in re.finditer(r"wait(?:ing)?\s*period([^0-9\n]{0,30}?)(\d{1,3})\s*days?\b", text, re.I):
            if _WAIT_LIMIT_RE.search(m.group(1)):
                continue
            v = int(m.group(2))
            if 0 < v <= 365:
                return {"kind": "days", "days": v}
    return None


def extract_waiting_period_days(docs: list[RawDoc]) -> Optional[int]:
    """The declared waiting period in days, or None when the plan states a non-numeric rule
    (e.g. '1st of Month > DOH') or states none at all."""
    rule = extract_waiting_period_rule(docs)
    if rule and rule.get("kind") == "days":
        return rule.get("days")
    return None


def _waiting_period_end(hire, rule: Optional[dict]):
    """Date on which an employee hired on `hire` completes the plan's waiting period."""
    if hire is None or not rule:
        return None
    kind = rule.get("kind")
    if kind == "days":
        days = rule.get("days")
        if not days:
            return None
        return hire + _dt.timedelta(days=int(days))
    if kind == "first_of_month":
        # '1st of Month > DOH' - the first day of the month AFTER the hire month.
        year, month = hire.year, hire.month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return _dt.date(year, month, 1)
    return None



def _category_code(value: str) -> str:
    """Extract the leading structural code from a coded value such as 'F - Full Time' -> 'F'."""
    v = clean(value)
    m = re.match(r"\s*([A-Za-z0-9]+)\s*[-–:]", v)
    return (m.group(1) if m else v).upper()


def refine_non_enrolled_statuses(census: Census, reference_date=None,
                                 waiting_days: Optional[int] = None,
                                 waiting_rule: Optional[dict] = None) -> None:
    """Assign NE / WP / WO to eligible-but-not-enrolled employees using STRUCTURAL evidence from the
    submission itself - never a hardcoded name, class label, or fixed date. The 'eligible' sets are
    learned from the employees who ARE enrolled:

      * an employee whose worker-category code is not represented among enrolled employees, and who
        has no waiver and no enrollment, is NOT ELIGIBLE for the plan -> NE;
      * an employee whose eligibility-class code is not represented among enrolled employees, with no
        waiver and no enrollment, is similarly -> NE;
      * an otherwise-eligible, non-enrolled employee whose waiting period has not yet elapsed on the
        reference date -> WP (the period is read from the plan design: N days, or a calendar rule
        such as '1st of Month > DOH');
      * an employee with no benefit-eligibility record at all who is absent from a benefit roster
        that enumerates the majority of the census -> NE (they are outside the covered population);
      * every remaining eligible, non-enrolled employee -> WO (waived / not enrolled);
      * employees with no evidence at all are left unchanged (never fabricated).

    Explicit source codes (WP/NE/RC/WO) are preserved; this only fills genuinely-unclassified rows.
    """
    rule = waiting_rule
    if rule is None and waiting_days:
        rule = {"kind": "days", "days": int(waiting_days)}

    def _as_date(d):
        if d is None:
            return None
        return d.date() if isinstance(d, _dt.datetime) else d

    ref = _as_date(reference_date)

    enrolled = [e for e in census.employees if e.plan and e.plan.upper() not in ("NA", "N/A")]
    enrolled_classes = {_category_code(e.eligibility_class) for e in enrolled if e.eligibility_class}

    # A benefit roster is authoritative only when it enumerates the great majority of the census AND
    # includes at least one employee who is not medically enrolled (so it is a population roster, not
    # merely a list of elections). A partial extract proves nothing about the employees it omits.
    total_emps = len(census.employees)
    roster_emps = [e for e in census.employees if getattr(e, "benefit_roster", False)]
    roster_authoritative = bool(
        total_emps and roster_emps and len(roster_emps) * 2 >= total_emps
        and any(not (e.plan and e.plan.upper() not in ("NA", "N/A")) for e in roster_emps))

    # The dominant worker category is the full-time/standard population. A minority worker category
    # (e.g. part-time) that is normally not benefit-eligible is the structural basis for NE.
    _cat_counts: dict[str, int] = {}
    for e in census.employees:
        c = _category_code(e.worker_category)
        if c:
            _cat_counts[c] = _cat_counts.get(c, 0) + 1
    dominant_cat = max(_cat_counts, key=_cat_counts.get) if _cat_counts else ""

    for e in census.employees:
        if e.plan and e.plan.upper() not in ("NA", "N/A"):
            continue                                    # enrolled - leave alone
        # An explicit status stated by the source census / enrollment document is never overwritten.
        if clean(e.cov_raw) and e.coverage.upper() in ("WO", "NE", "WP", "RC"):
            continue
        if e.coverage and e.coverage.upper() not in ("", "ENROLLED", "ACTIVE", "YES", "Y", "TRUE"):
            continue                                    # a real tier already assigned - leave alone

        has_waiver = bool(clean(e.waive_reason))
        cat = _category_code(e.worker_category)
        eclass = _category_code(e.eligibility_class)

        # 1) Positive non-eligibility evidence (structural): the employee's worker category is a
        #    minority (non-dominant) category that is not benefit-eligible, OR their eligibility class
        #    is not one any enrolled employee shares - and there is no waiver on file.
        ineligible = False
        if cat and dominant_cat and cat != dominant_cat and not has_waiver:
            ineligible = True
        if eclass and enrolled_classes and eclass not in enrolled_classes and not has_waiver:
            ineligible = True
        if ineligible:
            e.coverage = "NE"
            if not e.plan:
                e.plan = "NA"
            if not e.cobra:
                e.cobra = "N"
            e.dependents = []
            continue

        # 2) Waiting period: the employee's waiting period has not yet elapsed on the reference date
        #    and they have no waiver and no enrollment -> coverage not yet effective.
        hire = _as_date(e.hire_date)
        if hire and ref and rule:
            end = _waiting_period_end(hire, rule)
            if end and end > ref and (ref - hire).days >= 0:
                e.coverage = "WP"
                if not e.plan:
                    e.plan = "NA"
                if not e.cobra:
                    e.cobra = "N"
                e.dependents = []
                continue

        # 2b) Roster absence: the submission's benefit roster enumerates the covered population (it
        #     includes waived and waiting-period employees, so membership means eligibility, not
        #     enrollment). An active census employee with no benefit record of any kind who is absent
        #     from that roster is therefore outside the eligible population -> NE.
        if roster_authoritative and not getattr(e, "benefit_roster", False) and not has_waiver:
            e.coverage = "NE"
            if not e.plan:
                e.plan = "NA"
            if not e.cobra:
                e.cobra = "N"
            e.dependents = []
            continue

        # 3) Default: eligible, active, not enrolled -> waived / not enrolled.
        has_blocker = False
        pos = clean(e.position_status)
        if pos and re.search(r"\b(terminat|inactive|separat|cobra)\b", pos, re.I):
            has_blocker = True
        if not has_blocker and (has_waiver or e.sub_id or hire or e.eligibility_class or e.worker_category):
            e.coverage = "WO"
            if not e.plan:
                e.plan = "NA"
            if not e.cobra:
                e.cobra = "N"
            e.dependents = []


def _link_dependents(st: _State, effective_date=None) -> None:
    for ref in st.dep_refs:
        p = ref.person
        emp = None
        if ref.sub_id and ref.sub_id in st.by_id:
            emp = st.by_id[ref.sub_id]
        elif ref.emp_key and st.by_name.get(ref.emp_key):
            emp = st.by_name[ref.emp_key][0]
        elif ref.positional is not None and not (ref.sub_id or ref.emp_key):
            emp = ref.positional
        if emp is None:
            p.note("Unassigned Dependent - No Matching Subscriber", red=True)
            st.census.unassigned_dependents.append(p)
            continue
        dk = name_key(p.first, p.last)
        dup = None
        for d in emp.dependents:
            p_first_primary = p.first.split()[0].lower() if p.first else ""
            d_first_primary = d.first.split()[0].lower() if d.first else ""
            same_name = (name_key(d.first, d.last) == dk or (
                p_first_primary and p_first_primary == d_first_primary and d.last.lower() == p.last.lower()))
            if same_name and (d.dob == p.dob or not d.dob or not p.dob):
                dup = d
                break
        if dup:                                           # same dependent in two files/tabs
            had_dob, had_gender = bool(dup.dob), bool(dup.gender)
            _fill(dup, first=p.first, last=p.last, gender=p.gender, dob=p.dob, zip=p.zip)
            # keep the fuller legal name (e.g. 'Jolan Ridwan' over 'Jolan')
            if p.first and len(p.first) > len(dup.first or ""):
                dup.first = p.first
            p_rel = std_relationship(p.rel_raw) if p.rel_raw else ""
            d_rel = std_relationship(dup.rel_raw) if dup.rel_raw else ""
            if p_rel in ("SP", "CH") and d_rel not in ("SP", "CH"):
                dup.role, dup.rel_raw = p_rel, p.rel_raw    # explicit relationship beats a defaulted one
                dup.notes = [n for n in dup.notes if not str(n[0]).startswith(
                    ("Missing Dependent Relationship", "Unrecognised Relationship"))]
            elif p.role in ("SP", "CH") and dup.role not in ("SP", "CH"):
                dup.role = p.role
            elif p.role == "SP" and dup.role != "SP" and d_rel != "CH":
                dup.role = "SP"
            if not had_dob and dup.dob:
                dup.notes = [n for n in dup.notes if n[0] != "Missing Date of Birth"]
            if not had_gender and dup.gender:
                dup.notes = [n for n in dup.notes if not str(n[0]).startswith(("Missing Gender", "Invalid Gender"))]
            st.census.dependent_merges += 1
            continue
        p.employee = emp
        emp.dependents.append(p)

    import datetime
    from comparator import tier_from_deps

    eff = effective_date
    if isinstance(eff, datetime.datetime):
        eff = eff.date()

    has_medical_roster = st.saw_dependent_source or any(e.plan for e in st.employees)

    for emp in st.employees:
        # Determine employee coverage tier if unassigned or generic "enrolled"
        if not emp.coverage or emp.coverage.lower() in ("enrolled", "active", "yes", "y", "true"):
            if emp.cov_raw and emp.cov_raw.upper() in ("WO", "WAIVED", "NE", "WP", "RC"):
                emp.coverage = emp.cov_raw.upper()
            elif has_medical_roster and not emp.plan:
                # Non-enrolled status (NE/WP/WO) is resolved structurally later by
                # refine_non_enrolled_statuses(), once the invoice period and declared waiting period
                # are known. Leave the status unset here rather than guess.
                emp.coverage = ""
            else:
                emp.coverage = tier_from_deps(emp)

        if emp.coverage in ("WO", "NE", "WP") and not emp.plan:
            emp.plan = "NA"
            if not emp.cobra:
                emp.cobra = "N"

        # Per Audit Phase 11: EE, WO, NE, WP do not contain enrolled medical dependents
        if emp.coverage in ("EE", "WO", "NE", "WP"):
            emp.dependents = []

        # Propagate coverage, cobra, zip, and plan to linked dependents
        for dep in emp.dependents:
            if not dep.coverage:
                dep.coverage = emp.coverage
            if not dep.cobra:
                dep.cobra = emp.cobra or "N"
            if not dep.zip and emp.zip:
                dep.zip = emp.zip
            if not dep.plan and emp.plan:
                dep.plan = emp.plan

            # Check child turning 26 or older on proposed effective date
            if dep.role == "CH" and dep.dob and eff:
                d = dep.dob.date() if isinstance(dep.dob, datetime.datetime) else dep.dob
                age = eff.year - d.year - ((eff.month, eff.day) < (d.month, d.day))
                if age >= 26:
                    eff_str = eff.strftime("%m/%d/%Y")
                    dep.note(f"Child age {age} on {eff_str} effective date - verify dependent eligibility", red=False, orange=True)


def _order_output(st: _State) -> None:
    out: list[Person] = []
    for emp in st.employees:
        out.append(emp)
        out.extend(sorted(emp.dependents, key=lambda d: 0 if d.role == "SP" else 1))
    out.extend(st.census.unassigned_dependents)
    st.census.people = out
    st.census.raw_dependent_rows = len([p for p in out if p.role != "EE"]) + st.census.dependent_merges


def _parse_llm_census_rows(doc: RawDoc, rows: list[dict], st: _State) -> bool:
    count = 0
    for r in rows:
        fn = clean(r.get("first_name"))
        ln = clean(r.get("last_name"))
        if not (fn or ln):
            continue
        rel = std_relationship(r.get("relationship", "EE")) or "EE"
        p = Person(rel, fn, ln, source=doc.name)
        _standardise(p, {
            "gender": r.get("gender"),
            "dob": r.get("dob"),
            "zip": r.get("zip"),
            "coverage": r.get("coverage"),
            "cobra": r.get("cobra"),
            "plan": r.get("plan"),
            "premium": r.get("premium"),
        }, is_employee=(rel == "EE"), st=st)

        if rel == "EE":
            st.census.raw_employee_rows += 1
            st.employees.append(p)
            st.by_name.setdefault(name_key(fn, ln), []).append(p)
            if p.dob:
                st.by_nd[(name_key(fn, ln), p.dob)] = p
        else:
            st.census.raw_dependent_rows += 1
            st.saw_dependent_source = True
            sub_name = clean(r.get("subscriber_name"))
            st.dep_refs.append(_DepRef(person=p, emp_key=name_key(*split_full_name(sub_name)) if sub_name else "", file=doc.name))
        count += 1
    return count > 0


# ----------------------------------------------------------------- public ---
def build_census(census_docs: list[RawDoc], effective_date=None) -> tuple[Census, dict]:
    st = _State()
    for doc in census_docs:
        used_any = False
        for t in doc.tables:
            try:
                used_any |= _parse_table(doc, t.name, t.df, st)
            except Exception as exc:
                log.error("Census table %s/%s skipped: %s", doc.name, t.name, exc)
        if not used_any and (doc.text or "").strip():
            from census_config import USE_LLM_FALLBACK
            if USE_LLM_FALLBACK:
                try:
                    from llm_client import extract_census_rows
                    rows = extract_census_rows(doc.text)
                    if rows:
                        used_any = _parse_llm_census_rows(doc, rows, st)
                except Exception as exc:
                    log.warning("LLM census extraction fallback failed for %s: %s", doc.name, exc)

        if used_any:
            st.census.files_used.append(doc.name)
        else:
            log.warning("No census table recognised in %s", doc.name)

    _link_dependents(st, effective_date=effective_date)
    _order_output(st)
    meta = {
        "has_coverage_column": st.has_cov_col,
        "saw_dependent_source": st.saw_dependent_source,
    }
    return st.census, meta
