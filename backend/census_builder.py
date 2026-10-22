"""Build ONE standardised census from every uploaded census / enrollment / dependent file."""
from __future__ import annotations

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
    if is_employee:
        raw = clean(row_vals.get("coverage"))
        status_raw = clean(row_vals.get("status"))
        if not raw and status_raw:
            if status_raw.upper() in ("WO", "WAIVED", "WAIVE"):
                raw = "WO"
            elif status_raw.upper() in ("NE", "NOT ELIGIBLE", "INELIGIBLE"):
                raw = "NE"
            elif status_raw.upper() in ("WP", "WAITING PERIOD"):
                raw = "WP"
            elif status_raw.upper() in ("RC", "REFUSED", "REFUSING"):
                raw = "RC"
            elif status_raw.upper() in ("ENROLLED", "ACTIVE", "YES"):
                raw = "Enrolled"
        p.cov_raw = raw
        p.coverage = std_coverage(raw)
        if raw and not p.coverage and raw.lower() not in ("enrolled", "active", "yes"):
            p.note(f"Invalid Coverage Code: '{raw}'", red=True)
        p.cobra = std_cobra(row_vals.get("cobra")) or "N"
        p.plan = clean(row_vals.get("plan"))
        p.premium = to_money(row_vals.get("premium"))
        p.hire_date = parse_date(row_vals.get("hire"))
        if st.has_hire_col and not p.hire_date:
            p.note("Missing Hire Date")
        p.effective_date = parse_date(row_vals.get("effective_date"))
    else:
        p.cobra = std_cobra(row_vals.get("cobra")) or "N"
    if not p.first or not p.last:
        p.note("Missing Employee Name" if is_employee else "Missing Dependent Name")


def _names(r: _Row, first_f="first_name", last_f="last_name", full_f="full_name") -> tuple[str, str]:
    first, last = r.t(first_f), r.t(last_f)
    if (not first or not last) and r.t(full_f) and not (first and last):
        f2, l2 = split_full_name(r.t(full_f))
        first, last = first or f2, last or l2
    if (first.lower() in {"required", "optional", "first", "firstname", "first name", "ee", "sp", "ch"} and
        last.lower() in {"required", "optional", "last", "lastname", "last name", "name"}):
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
    fname = doc.name
    last_emp: Optional[Person] = None
    used = False

    for _, series in body(df, hdr).iterrows():
        r = _Row(hdr, series)
        if all(is_blank(v) for v in series.tolist()):
            continue
        joined = " ".join(clean(v).lower() for v in series.tolist())
        if joined.startswith(("total", "grand total", "count")):
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
                p_emp = Person("EE", first, last, source=fname, sub_id=r.t("sub_id"))
                _standardise(p_emp, values, True, st)
                last_emp = _register_employee(p_emp, fname, st)
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
                if not emp.sub_id and not emp.hire_date:
                    emp.coverage = "NE"
                elif emp.hire_date and eff and (
                    (eff.year == emp.hire_date.year and eff.month == emp.hire_date.month)
                    or (eff - (emp.hire_date.date() if hasattr(emp.hire_date, 'date') else emp.hire_date)).days <= 95
                ):
                    emp.coverage = "WP"
                else:
                    emp.coverage = "WO"
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
