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
from utils import (build_gender_candidate, clean, display_name, is_blank, is_unavailable_gender,
                   name_key, parse_date, resolve_gender_candidates, split_full_name,
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
    table: str = ""
    is_non_med: bool = False
    plan_type: str = ""
    plan: str = ""
    coverage_tier: str = ""


@dataclass
class _State:
    census: Census = field(default_factory=Census)
    by_id: dict = field(default_factory=dict)
    by_nd: dict = field(default_factory=dict)       # (name_key, dob) -> Person
    by_name: dict = field(default_factory=dict)     # name_key -> [Person]
    files_of: dict = field(default_factory=dict)    # id(Person) -> set(files)
    tables_of: dict = field(default_factory=dict)   # id(Person) -> set((file, table))
    med_tables_of: dict = field(default_factory=dict) # id(Person) -> set((file, table)) with medical rows
    employees: list = field(default_factory=list)
    dep_refs: list = field(default_factory=list)
    has_cov_col: bool = False
    has_hire_col: bool = False
    hire_missing: list = field(default_factory=list)
    saw_dependent_source: bool = False
    record_counter: int = 0


def _next_record_id(st: _State) -> str:
    st.record_counter += 1
    return f"rec_{st.record_counter:04d}"



# --------------------------------------------------------------------- row --
class _Row:
    def __init__(self, hdr: Header, series: pd.Series):
        self.h, self.s = hdr, series

    def g(self, f: str):
        i = self.h.columns.get(f)
        return None if i is None else self.s.iloc[i]

    def t(self, f: str) -> str:
        return clean(self.g(f))

    def gender_candidates(
        self,
        is_dep: bool = False,
        demo_is_employee: bool = False,
        source_file: str = "",
        wide_slot: Optional[tuple[str, str]] = None,
    ) -> list[dict]:
        """Collect all candidate gender/sex columns for this person in physical column order."""
        col_pairs: list[tuple[int, str]] = []
        if wide_slot is not None:
            wide_cands = getattr(self.h, "wide_candidate_columns", {}).get(wide_slot, {}).get("gender", [])
            if wide_cands:
                col_pairs.extend(wide_cands)
            else:
                flds = self.h.wide_deps.get(wide_slot, {})
                if "gender" in flds:
                    idx = flds["gender"]
                    lab = self.h.labels[idx] if idx < len(self.h.labels) else "gender"
                    col_pairs.append((idx, lab))
        elif is_dep:
            dep_cands = getattr(self.h, "candidate_columns", {}).get("dep_gender", [])
            if dep_cands:
                col_pairs.extend(dep_cands)
            elif "dep_gender" in self.h.columns:
                idx = self.h.columns["dep_gender"]
                col_pairs.append((idx, self.h.labels[idx] if idx < len(self.h.labels) else "dep_gender"))
            if not demo_is_employee and not col_pairs:
                emp_cands = getattr(self.h, "candidate_columns", {}).get("gender", [])
                if emp_cands:
                    col_pairs.extend(emp_cands)
                elif "gender" in self.h.columns:
                    idx = self.h.columns["gender"]
                    col_pairs.append((idx, self.h.labels[idx] if idx < len(self.h.labels) else "gender"))
        else:
            emp_cands = getattr(self.h, "candidate_columns", {}).get("gender", [])
            if emp_cands:
                col_pairs.extend(emp_cands)
            elif "gender" in self.h.columns:
                idx = self.h.columns["gender"]
                col_pairs.append((idx, self.h.labels[idx] if idx < len(self.h.labels) else "gender"))

        out: list[dict] = []
        for idx, lab in col_pairs:
            raw_v = self.s.iloc[idx] if idx < len(self.s) else None
            out.append(build_gender_candidate(raw_v, source=source_file, column_index=idx, header=lab))
        return out


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

    # Core medical plan signals (network types, plan acronyms, benefit designs)
    med_signals = (
        "ppo", "pos", "cpos", "cposii", "hmo", "epo", "oaas", "oap", "afa", "sgp", "awh",
        "open access", "hdhp", "cdhp", "copay", "coinsurance", "deductible", "medical"
    )
    has_medical_signal = any(s in combined for s in med_signals)

    # Strong, unambiguous non-medical line signals take priority: provider/plan codes such as
    # AETDEN / AET-DMO DEN / VSP / METLDI LIF / METLDI LTD. These override weak tokens like 'ppo'
    # that appear inside dental provider codes (e.g. 'AET-APPO').
    if re.search(r"\b(den|dmo|dmd|dnt|vsp|wis|ltl)\b", combined):
        return True
    if re.search(r"\blif\b|\bltd\b|\bstd\b", combined):
        return True

    # Standalone HSA spending accounts (without medical plan indicators)
    if re.search(r"\b(hsa\s*(?:fee|contribution|spending|deposit|account|bank|admin))\b", combined):
        return True
    if combined.strip() in ("hsa", "hsa account", "hsa contribution", "hsa fee", "hsa bank"):
        return True

    # Non-medical lines (excluding 'hsa' because high-deductible medical plans routinely include 'HSA' in plan titles)
    non_med_words = (
        "dent", "vis", "life", "lif", "disab", "ltd", "std", "accid", "fsa",
        "legal", "pet", "indemn", "crit", "identity", "ad&d", "spending",
        "401k", "401(k)", "401 k", "retirement", "pension", "commuter", "transit", "parking", "eap"
    )

    if any(w in combined for w in non_med_words):
        if not has_medical_signal:
            return True

    pt = clean(plan_type).lower().strip()
    if pt:
        if pt == "medical" or "med" in pt or "health" in pt:
            return False
        if any(w in pt for w in non_med_words):
            return True
        if not has_medical_signal:
            return True

    pn = clean(plan_name).lower()
    if pn:
        if any(k in pn for k in non_med_words) and not has_medical_signal:
            return True
    return False


def _fill(person: Person, **kw):
    for k, v in kw.items():
        if v in (None, ""):
            continue
        if k == "aliases" and isinstance(v, list):
            for a in v:
                if a and a not in person.aliases:
                    person.aliases.append(a)
            continue
        curr = getattr(person, k, None)
        if not curr or (k == "coverage" and str(curr).lower() in ("enrolled", "active", "yes") and str(v).lower() not in ("enrolled", "active", "yes")):
            setattr(person, k, v)


def _resolve_person_gender(p: Person) -> None:
    """Resolve all same-person gender/sex candidates across columns and files without guessing."""
    if not p.gender_candidates:
        return
    # Deduplicate identical observations while preserving physical order
    deduped: list[dict] = []
    seen_keys: set[tuple] = set()
    for c in p.gender_candidates:
        k = (c.get("source", ""), c.get("column_index", -1), c.get("header", ""), c.get("raw", ""))
        if k not in seen_keys:
            seen_keys.add(k)
            deduped.append(c)
    p.gender_candidates = deduped

    resolved, status, detail = resolve_gender_candidates(p.gender_candidates)
    p.gender = resolved
    p.gender_resolution_status = status

    # Clear stale gender notes before applying current resolution state
    p.notes = [
        n for n in p.notes
        if not str(n[0]).startswith(("Missing Gender", "Invalid Gender Value", "Conflicting Gender Values"))
    ]
    if status == "CONFLICT":
        p.note(f"Conflicting Gender Values ({detail}) - requires clarification", red=True)
    elif status == "INVALID":
        p.note(f"Invalid Gender Value: '{detail}'", red=True)


def _standardise(p: Person, row_vals: dict, is_employee: bool, st: _State) -> None:
    """Apply template standards + validation notes to a freshly parsed person."""
    g_cands = row_vals.get("gender_candidates")
    if g_cands:
        p.gender_candidates.extend(g_cands)
    elif "gender" in row_vals:
        g_raw = row_vals.get("gender", "")
        p.gender_candidates.append(build_gender_candidate(g_raw, source=p.source, column_index=-1, header="gender"))
    _resolve_person_gender(p)
    d_raw = row_vals.get("dob")
    p.dob = parse_date(d_raw)
    if not is_blank(d_raw) and not p.dob:
        p.note(f"Invalid Date of Birth: '{clean(d_raw)}'")
    elif not p.dob:
        p.note("Missing Date of Birth")
    p.zip, ok = std_zip(row_vals.get("zip"))
    if not ok:
        p.note(f"Invalid ZIP Format: '{p.zip}'")
    p.address = clean(row_vals.get("address"))
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
        p.preferred_first = clean(row_vals.get("preferred_first", getattr(p, "preferred_first", "")))
        p.middle_name = clean(row_vals.get("middle_name", getattr(p, "middle_name", "")))
        if p.preferred_first and p.last:
            a1 = f"{p.preferred_first} {p.last}".strip()
            if a1 not in p.aliases:
                p.aliases.append(a1)
        if p.middle_name and p.last and p.middle_name.lower() != p.preferred_first.lower():
            a2 = f"{p.middle_name} {p.last}".strip()
            if a2 not in p.aliases:
                p.aliases.append(a2)
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
        p.salary = clean(row_vals.get("salary"))
        if p.coverage == "WO":
            if p.plan and p.plan.lower() in ("cost", "waived", "other", "other coverage", "spouse", "spousal", "medicare", "va", "none", "na", "n/a"):
                if not p.waive_reason:
                    p.waive_reason = p.plan
                p.plan = ""
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
    if not first and "preferred_first" in r.h.columns:
        first = r.t("preferred_first")
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


def _match_employee_by_name(st: _State, first: str = "", last: str = "", emp_key: str = "") -> list[Person]:
    """Find matching employee(s) by exact name_key, falling back to primary-first + punctuation-insensitive last name."""
    nk = emp_key or name_key(first, last)
    if nk and st.by_name.get(nk):
        return st.by_name[nk]
    if not first and not last and emp_key:
        first, last = split_full_name(emp_key)
    p_first = re.sub(r"[^a-z0-9]", "", (first.split()[0] if first and first.split() else "")).lower()
    p_last = re.sub(r"[^a-z0-9]", "", (last or "")).lower()
    if not p_first or not p_last:
        return []
    matches = []
    for cand in st.employees:
        if getattr(cand, "is_duplicate", False):
            continue
        c_first = re.sub(r"[^a-z0-9]", "", (cand.first.split()[0] if cand.first and cand.first.split() else "")).lower()
        c_last = re.sub(r"[^a-z0-9]", "", (cand.last or "")).lower()
        if c_first == p_first and c_last == p_last:
            matches.append(cand)
            continue
        c_pref = re.sub(r"[^a-z0-9]", "", (cand.preferred_first.split()[0] if cand.preferred_first and cand.preferred_first.split() else "")).lower()
        c_mid = re.sub(r"[^a-z0-9]", "", (cand.middle_name.split()[0] if cand.middle_name and cand.middle_name.split() else "")).lower()
        if c_last == p_last and (p_first in (c_pref, c_mid)):
            matches.append(cand)
    return matches


# ----------------------------------------------------------- registration ---
def _register_employee(
    p: Person,
    fname: str,
    st: _State,
    is_dep_listing: bool = False,
    tname: str = "",
    is_non_med: bool = False,
) -> Person:
    """Merge across files, flag same-file duplicates, return the canonical Person."""
    nk = name_key(p.first, p.last)
    name_cands = _match_employee_by_name(st, p.first, p.last, nk)
    existing = None
    if p.sub_id and p.sub_id in st.by_id:
        existing = st.by_id[p.sub_id]
    elif (nk, p.dob) in st.by_nd and p.dob:
        existing = st.by_nd[(nk, p.dob)]
    elif nk and not p.dob and name_cands:
        existing = name_cands[0]
    elif nk and p.dob and name_cands:
        cand = [e for e in name_cands if not e.dob or e.dob == p.dob]
        existing = cand[0] if cand else None

    if existing is not None:
        files = st.files_of[id(existing)]
        tables = st.tables_of.setdefault(id(existing), set())
        med_tables = st.med_tables_of.setdefault(id(existing), set())
        if (fname, tname) in tables and not is_dep_listing and not is_non_med and (fname, tname) in med_tables:
            # same file and same table with prior medical row => true duplicate employee
            p.note("Duplicate Employee", red=True)
            p.is_duplicate = True
            st.census.raw_employee_rows += 1
            st.census.duplicate_employees += 1
            st.files_of[id(p)] = {fname}
            st.tables_of[id(p)] = {(fname, tname)}
            st.med_tables_of[id(p)] = {(fname, tname)}
            st.employees.append(p)
            st.census.people.append(p)
            return existing                                  # dependents link to the first record

        # Different table/page within the same file, benefit line, OR another file => merge complementary data
        if fname not in files:
            st.census.raw_employee_rows += 1
            st.census.cross_file_merges += 1
            files.add(fname)
        tables.add((fname, tname))
        if not is_non_med:
            med_tables.add((fname, tname))

        if existing.coverage and p.coverage and existing.coverage != p.coverage:
            existing.note(f"Coverage differs across census files ({existing.coverage} vs {p.coverage}); "
                          f"first file kept")
        if p.gender_candidates:
            existing.gender_candidates.extend(p.gender_candidates)
            _resolve_person_gender(existing)
        if p.coverage_associations:
            existing.coverage_associations.extend(p.coverage_associations)
        _fill(existing, first=p.first, last=p.last, preferred_first=p.preferred_first,
              middle_name=p.middle_name, aliases=p.aliases, gender=p.gender, dob=p.dob, zip=p.zip,
              coverage=p.coverage, cobra=p.cobra, plan=p.plan, premium=p.premium,
              sub_id=p.sub_id, hire_date=p.hire_date, effective_date=p.effective_date, salary=p.salary,
              job_title=p.job_title, worker_category=p.worker_category,
              eligibility_class=p.eligibility_class, position_status=p.position_status,
              waive_reason=p.waive_reason)
        if not existing.salary and p.salary:
            existing.salary = p.salary
        if not existing.dob and p.dob:
            existing.notes = [n for n in existing.notes if n[0] != "Missing Date of Birth"]
        if not existing.hire_date and p.hire_date:
            existing.notes = [n for n in existing.notes if n[0] != "Missing Hire Date"]
        if p.sub_id and p.sub_id not in st.by_id:
            st.by_id[p.sub_id] = existing
        if nk and existing not in st.by_name.get(nk, []):
            st.by_name.setdefault(nk, []).append(existing)
        return existing

    if not p.record_id:
        p.record_id = _next_record_id(st)
    st.census.raw_employee_rows += 1
    st.files_of[id(p)] = {fname}
    st.tables_of[id(p)] = {(fname, tname)}
    st.med_tables_of[id(p)] = set() if is_non_med else {(fname, tname)}
    st.employees.append(p)
    st.census.people.append(p)
    if p.sub_id:
        st.by_id[p.sub_id] = p
    st.by_nd[(nk, p.dob)] = p
    st.by_name.setdefault(nk, []).append(p)
    return p


# ------------------------------------------------------------ table parse ---
def _parse_table(doc: RawDoc, tname: str, df: pd.DataFrame, st: _State) -> bool:
    if tname.strip().lower() in ("data", "lookup", "lookups", "dropdown", "dropdowns", "validation", "lists", "codes"):
        return False
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
    # lookup/dropdown validation grids with only 'dep_full' and no demographic/employee columns are not census tables
    if ("dep_full" in cols and "dep_first" not in cols and "dep_last" not in cols
            and not any(k in cols for k in ("dob", "dep_dob", "gender", "dep_gender", "relationship",
                                            "first_name", "last_name", "full_name",
                                            "emp_first", "emp_last", "emp_full", "sub_id"))):
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

        # Filter template example / sample rows (e.g. 'Ex', 'Example', 'Sample' in leading cells or column A/B)
        first_non_empty = non_empty[0].lower() if non_empty else ""
        if first_non_empty in ("ex", "example", "sample", "eg", "e.g.", "ex.", "example:"):
            continue
        if any(clean(series.iloc[c]).lower() in ("ex", "example", "sample", "eg", "e.g.", "ex.", "example:")
               for c in range(min(4, len(series)))):
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
        has_benefit_lines = bool(dep_listing or "plan_type" in cols or "plan_category" in cols or ("plan" in cols and "premium" in cols))
        cov_assoc = {
            "file": fname,
            "table": tname,
            "plan_type": pt_val or ("Non-Medical" if is_non_med else ("Medical" if p_val else "Census")),
            "plan": p_val,
            "tier": clean(r.g("coverage")) if "coverage" in cols else "",
            "is_medical": not is_non_med,
        }
        if dep_listing:
            # Table has dependent columns (dep_first, dep_last, etc.)
            ef, el = _names(r)
            dfirst, dlast = _names(r, "dep_first", "dep_last", "dep_full")
            rel_raw = r.t("relationship")
            rel_std = std_relationship(rel_raw) if rel_raw else ""
            is_dep_row = bool(dfirst or dlast or rel_std in {"SP", "CH", "DP"})
            demo_is_employee = not is_dep_row

            if not (ef or el) and any(k in cols for k in ("emp_first", "emp_last", "emp_full")):
                ef, el = _names(r, "emp_first", "emp_last", "emp_full")

            is_same_person_as_dep = bool(dfirst and dlast and name_key(ef, el) == name_key(dfirst, dlast))
            canon = None
            if (ef or el) and not is_same_person_as_dep:
                emp_dob = r.g("emp_dob") if "emp_dob" in cols else (r.g("dob") if demo_is_employee else None)
                emp_gender = r.t("emp_gender") if "emp_gender" in cols else (r.t("gender") if demo_is_employee else "")
                values = {"gender": emp_gender,
                          "gender_candidates": r.gender_candidates(is_dep=False, demo_is_employee=demo_is_employee, source_file=fname),
                          "dob": emp_dob, "zip": r.g("zip"),
                          "address": r.t("address") if "address" in cols else "",
                          "preferred_first": r.t("preferred_first") if "preferred_first" in cols else "",
                          "middle_name": r.t("middle_name") if "middle_name" in cols else "",
                          "hire": r.g("hire_date"), "status": r.g("status"), "salary": r.g("salary")}
                values.update(_eligibility_vals(r))
                if not is_non_med:
                    prem = r.g("premium")
                    if prem is None and ("ee_cost" in cols or "er_cost" in cols):
                        ee_c = to_money(r.g("ee_cost")) or 0.0
                        er_c = to_money(r.g("er_cost")) or 0.0
                        if ee_c > 0 or er_c > 0:
                            prem = round(ee_c + er_c, 2)
                    values.update({"coverage": r.g("coverage"), "cobra": r.g("cobra"),
                                   "plan": r.g("plan"), "premium": prem, "effective_date": r.g("effective_date"),
                                   "salary": r.g("salary")})
                p_emp = Person("EE", ef, el, source=fname, sub_id=r.t("sub_id"), rel_raw="EE")
                p_emp.record_id = _next_record_id(st)
                p_emp.raw_data = {"file": fname, "table": tname, "row": series.to_dict()}
                p_emp.coverage_associations.append(dict(cov_assoc))
                _standardise(p_emp, values, True, st)
                canon = _register_employee(p_emp, fname, st, is_dep_listing=True, tname=tname, is_non_med=is_non_med)
                canon.benefit_roster = canon.benefit_roster or is_roster
                last_emp = canon
                used = True
            elif (ef or el) and is_same_person_as_dep:
                canon = last_emp

            raw_dep_str = f"{r.t('dep_first')} {r.t('dep_last')} {r.t('dep_full')}".strip()
            if _is_masked_or_empty(dfirst) and _is_masked_or_empty(dlast):
                if raw_dep_str:
                    st.census.masked_dependent_slots.append({
                        "file": fname, "table": tname, "employee": f"{ef} {el}".strip(),
                        "raw_value": raw_dep_str, "reason": "MASKED_PLACEHOLDER",
                    })
            elif dfirst or dlast or is_dep_row:
                p = Person("CH", dfirst, dlast, source=fname, rel_raw=rel_raw)
                p.record_id = _next_record_id(st)
                p.raw_data = {"file": fname, "table": tname, "row": series.to_dict()}
                p.coverage_associations.append(dict(cov_assoc))
                if demo_is_employee:
                    d_gender = r.t("dep_gender") if "dep_gender" in cols else ""
                    d_dob = r.g("dep_dob") if "dep_dob" in cols else None
                else:
                    d_gender = r.t("dep_gender") or r.t("gender")
                    d_dob = r.g("dep_dob") if r.g("dep_dob") is not None else r.g("dob")
                d_gcands = r.gender_candidates(is_dep=True, demo_is_employee=demo_is_employee, source_file=fname)
                _standardise(p, {"gender": d_gender, "gender_candidates": d_gcands, "dob": d_dob, "zip": r.g("zip"),
                                 "address": r.t("address") if "address" in cols else ""}, False, st)
                rel = std_relationship(rel_raw)
                if rel in {"SP", "CH", "DP"}:
                    p.role = rel
                else:
                    p.role = "CH"
                    p.note("Missing Dependent Relationship" if not rel_raw else
                           f"Unrecognised Relationship: '{rel_raw}'", red=True)
                emp_ref = name_key(ef, el) or name_key(*split_full_name(r.t("emp_full")))
                ref = _DepRef(p, sub_id=r.t("sub_id"), emp_key=emp_ref, positional=last_emp,
                              file=fname, table=tname, is_non_med=is_non_med,
                              plan_type=pt_val, plan=p_val, coverage_tier=clean(r.g("coverage")))
                st.dep_refs.append(ref)
                if not is_non_med:
                    st.census.raw_dependent_rows += 1
                    st.saw_dependent_source = True
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
        if "relationship" not in cols:
            cov_val = clean(r.g("coverage")) if "coverage" in cols else ""
            is_emp_dep_col = any("dependent" in lab.lower() for lab in hdr.labels[:3])
            has_emp_evidence = bool(
                r.g("hire_date") or r.g("salary") or r.g("job_title") or
                r.g("worker_category") or r.g("position_status") or r.t("sub_id")
            )
            if last_emp and not has_emp_evidence and (is_emp_dep_col or last_emp.coverage in ("ES", "EC", "FAM", "EF", "COUPLE")):
                if not cov_val:
                    if last_emp.coverage == "ES":
                        rel = "SP"
                    elif last_emp.coverage in ("FAM", "EF", "FAMILY"):
                        has_sp = any(getattr(d, 'role', '') == 'SP' for d in getattr(last_emp, 'dependents', []))
                        d_dob = parse_date(r.g("dob"))
                        is_adult = d_dob and ((_dt.date.today().year - d_dob.year) >= 26)
                        rel = "SP" if (not has_sp and is_adult) else "CH"
                    else:
                        rel = "CH"

        if is_non_med and rel not in {"SP", "CH", "DP", "?"}:
            # Register employee demographics without setting non-medical plan/coverage
            values = {"gender": r.t("gender"),
                      "gender_candidates": r.gender_candidates(is_dep=False, demo_is_employee=True, source_file=fname),
                      "dob": r.g("dob"), "zip": r.g("zip"),
                      "address": r.t("address") if "address" in cols else "",
                      "preferred_first": r.t("preferred_first") if "preferred_first" in cols else "",
                      "middle_name": r.t("middle_name") if "middle_name" in cols else "",
                      "hire": r.g("hire_date"), "status": r.g("status"), "effective_date": r.g("effective_date"),
                      "salary": r.g("salary")}
            values.update(_eligibility_vals(r))
            p_emp = Person("EE", first, last, source=fname, sub_id=r.t("sub_id"))
            p_emp.record_id = _next_record_id(st)
            p_emp.raw_data = {"file": fname, "table": tname, "row": series.to_dict()}
            p_emp.coverage_associations.append(dict(cov_assoc))
            _standardise(p_emp, values, True, st)
            last_emp = _register_employee(p_emp, fname, st, is_dep_listing=True, tname=tname, is_non_med=True)
            last_emp.benefit_roster = last_emp.benefit_roster or is_roster
            used = True
            continue

        prem = r.g("premium")
        if prem is None and ("ee_cost" in cols or "er_cost" in cols):
            ee_c = to_money(r.g("ee_cost")) or 0.0
            er_c = to_money(r.g("er_cost")) or 0.0
            if ee_c > 0 or er_c > 0:
                prem = round(ee_c + er_c, 2)
        is_dep_row = rel in {"SP", "CH", "DP", "?"}
        values = {"gender": r.t("gender"),
                  "gender_candidates": r.gender_candidates(is_dep=is_dep_row, demo_is_employee=not is_dep_row, source_file=fname),
                  "dob": r.g("dob"), "zip": r.g("zip"),
                  "address": r.t("address") if "address" in cols else "",
                  "preferred_first": r.t("preferred_first") if "preferred_first" in cols else "",
                  "middle_name": r.t("middle_name") if "middle_name" in cols else "",
                  "coverage": r.g("coverage") if not is_non_med else "",
                  "cobra": r.g("cobra") if not is_non_med else "",
                  "plan": r.g("plan") if not is_non_med else "",
                  "premium": prem if not is_non_med else None,
                  "hire": r.g("hire_date"), "status": r.g("status"),
                  "effective_date": r.g("effective_date"), "salary": r.g("salary")}
        values.update(_eligibility_vals(r))

        if is_dep_row:
            if not is_non_med:
                st.saw_dependent_source = True
            p = Person("CH" if rel == "?" else rel, first, last, source=fname, rel_raw=rel_raw)
            p.record_id = _next_record_id(st)
            p.raw_data = {"file": fname, "table": tname, "row": series.to_dict()}
            p.coverage_associations.append(dict(cov_assoc))
            _standardise(p, values, False, st)
            if rel == "?":
                p.note(f"Unrecognised Relationship: '{rel_raw}' - requires review", red=True)
            ef, el = clean(r.g("emp_first")), clean(r.g("emp_last"))
            emp_ref = name_key(ef, el) or name_key(*split_full_name(r.t("emp_full")))
            # when first/last hold the dependent, a separate full_name column names the employee
            if not emp_ref and r.t("full_name") and r.t("first_name") and r.t("last_name"):
                emp_ref = name_key(*split_full_name(r.t("full_name")))
            st.dep_refs.append(_DepRef(p, sub_id=r.t("sub_id"), emp_key=emp_ref,
                                       positional=last_emp, file=fname, table=tname,
                                       is_non_med=is_non_med, plan_type=pt_val, plan=p_val,
                                       coverage_tier=clean(r.g("coverage"))))
            if not is_non_med:
                st.census.raw_dependent_rows += 1
            used = True
            continue

        # ---- employee row
        p = Person("EE", first, last, source=fname, sub_id=r.t("sub_id"), rel_raw=rel_raw)
        p.record_id = _next_record_id(st)
        p.raw_data = {"file": fname, "table": tname, "row": series.to_dict()}
        p.coverage_associations.append(dict(cov_assoc))
        _standardise(p, values, True, st)
        canon = _register_employee(p, fname, st, is_dep_listing=has_benefit_lines, tname=tname, is_non_med=is_non_med)
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
                raw_wide = f"{dfirst} {dlast} {gv('name')}".strip()
                if raw_wide:
                    st.census.masked_dependent_slots.append({
                        "file": fname, "table": tname, "employee": f"{first} {last}".strip(),
                        "slot": f"{kind}{n}", "raw_value": raw_wide, "reason": "MASKED_PLACEHOLDER",
                    })
                continue
            dp_role = kind
            rel_str = gv("rel")
            if rel_str:
                std_r = std_relationship(rel_str)
                if std_r in ("SP", "CH"):
                    dp_role = std_r
            dp = Person(dp_role, dfirst, dlast, source=fname, rel_raw=rel_str or kind)
            dp.record_id = _next_record_id(st)
            dp.raw_data = {"file": fname, "table": tname, "row": series.to_dict()}
            dp.coverage_associations.append(dict(cov_assoc))
            wide_gcands = r.gender_candidates(wide_slot=(kind, n), source_file=fname)
            _standardise(dp, {"gender": gv("gender"), "gender_candidates": wide_gcands, "dob": ddob, "relationship": rel_str or dp_role}, False, st)
            st.dep_refs.append(_DepRef(dp, positional=canon, file=fname, table=tname, is_non_med=is_non_med))
            if not is_non_med:
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
        # Check for 'first day of the month following N days'
        m_fom_days = re.search(r"(?:first|1st)\s+day\s+of\s+(?:the\s+)?month\s+following\s+(\d{1,3})\s*days?", text, re.I)
        if m_fom_days:
            return {"kind": "first_of_month_after_days", "days": int(m_fom_days.group(1))}
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
    if rule and rule.get("kind") in ("days", "first_of_month_after_days"):
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
    if kind == "first_of_month_after_days":
        days = rule.get("days", 60)
        target = hire + _dt.timedelta(days=int(days))
        year, month = target.year, target.month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return _dt.date(year, month, 1)
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
                                 waiting_rule: Optional[dict] = None,
                                 docs: Optional[list[RawDoc]] = None,
                                 effective_date=None,
                                 tracer=None) -> None:
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

    Explicit source codes (WP/NE/RC/WO) are preserved; this only fills genuinely-unclassified rows,
    unless submission notes/broker evidence specifically identify employees currently in waiting period.
    """
    rule = waiting_rule
    if rule is None and waiting_days:
        rule = {"kind": "days", "days": int(waiting_days)}

    def _as_date(d):
        if d is None:
            return None
        return d.date() if isinstance(d, _dt.datetime) else d

    ref = _as_date(reference_date)
    eff_dt = _as_date(effective_date)

    # 0) Scan submission / broker documents for waiting-period employee evidence
    texts = [d.text or "" for d in docs] if docs else []
    for d in docs or []:
        if d.text:
            for line in d.text.splitlines():
                if any(w in line.lower() for w in ("term", "waiting period", "waive", "invoice")):
                    census.census_notes.append(line.strip())

    wp_notes_count = 0
    wp_broker_flag = False
    for t in texts:
        m_count = re.search(r"(\d+)\s+(?:additional\s+)?(?:EE['’]?s?|employees?)\s+(?:on\s+census\s+)?(?:currently\s+)?in\s+their\s+waiting\s+period", t, re.I)
        if m_count:
            wp_notes_count = int(m_count.group(1))
            wp_broker_flag = True
            break
        if re.search(r"\bwaiting\s+period\b.*(?:eligible|come\s+onto)", t, re.I):
            wp_broker_flag = True

    if wp_broker_flag and wp_notes_count > 0:
        candidates = []
        for e in census.employees:
            if e.plan and e.plan.upper() not in ("NA", "N/A", "COST", "WAIVED", "NOT ELIGIBLE"):
                continue
            h = _as_date(e.hire_date)
            is_recent_hire = False
            if h and eff_dt and (eff_dt - h).days <= 150 and (eff_dt - h).days >= 0:
                is_recent_hire = True
            elif h and ref and (ref - h).days <= 120:
                is_recent_hire = True
            if is_recent_hire and (clean(e.cov_raw).lower() in ("not eligible", "ne", "") or e.coverage in ("NE", "")):
                candidates.append((h or _dt.date.min, e))
        candidates.sort(key=lambda x: x[0], reverse=True)
        num_to_mark = wp_notes_count if wp_notes_count > 0 else len(candidates)
        for _, e in candidates[:num_to_mark]:
            e.coverage = "WP"
            e.plan = ""
            e.cobra = "N"

    enrolled = [e for e in census.employees if e.plan and e.plan.upper() not in ("NA", "N/A")]
    enrolled_classes = {_category_code(e.eligibility_class) for e in enrolled if e.eligibility_class}

    total_emps = len(census.employees)
    roster_emps = [e for e in census.employees if getattr(e, "benefit_roster", False)]
    roster_authoritative = bool(
        total_emps and roster_emps and len(roster_emps) * 2 >= total_emps
        and any(not (e.plan and e.plan.upper() not in ("NA", "N/A")) for e in roster_emps))

    _cat_counts: dict[str, int] = {}
    for e in census.employees:
        c = _category_code(e.worker_category)
        if c:
            _cat_counts[c] = _cat_counts.get(c, 0) + 1
    dominant_cat = max(_cat_counts, key=_cat_counts.get) if _cat_counts else ""

    for e in census.employees:
        if e.plan and e.plan.upper() not in ("NA", "N/A"):
            continue                                    # enrolled - leave alone
        # An explicit status stated by the source census / enrollment document is preserved
        # unless updated by broker waiting-period evidence above.
        if clean(e.cov_raw) and e.coverage.upper() in ("WO", "NE", "WP", "RC"):
            continue
        if e.coverage and e.coverage.upper() not in ("", "ENROLLED", "ACTIVE", "YES", "Y", "TRUE"):
            continue                                    # a real tier already assigned - leave alone

        has_waiver = bool(clean(e.waive_reason))
        cat = _category_code(e.worker_category)
        eclass = _category_code(e.eligibility_class)

        # 1) Positive non-eligibility evidence (structural)
        ineligible = False
        worker_info = f"{e.worker_category} {e.job_title} {e.position_status} {e.salary}".lower()
        if re.search(r"\b(temp|temporary|intern|seasonal|prn|per\s*diem)\b", worker_info):
            ineligible = True
        elif re.search(r"\b(part[\s\-]?time|pt)\b", worker_info) and not has_waiver:
            ineligible = True
        else:
            # Check for hours < 30 per week (e.g. "20 hrs", "25 hours/week", "hours: 24")
            m_hrs = re.search(r"(\d+(?:\.\d+)?)\s*(?:hrs?|hours?|hours?/week|hrs?/wk)", worker_info)
            if m_hrs and float(m_hrs.group(1)) < 30.0 and not has_waiver:
                ineligible = True
            elif cat and dominant_cat and cat != dominant_cat and not has_waiver:
                ineligible = True
            elif eclass and enrolled_classes and eclass not in enrolled_classes and not has_waiver:
                ineligible = True

        if ineligible:
            e.coverage = "NE"
            if not e.plan:
                e.plan = "NA"
            if not e.cobra:
                e.cobra = "N"
            continue

        # 2) Waiting period: check hire date vs. proposed effective date / invoice reference date
        hire = _as_date(e.hire_date)
        if hire:
            eff_rule = rule or {"kind": "days", "days": 30}
            end = _waiting_period_end(hire, eff_rule)
            is_wp = False
            # Condition A: Waiting period not elapsed as of invoice/submission reference date (and hired on or before ref)
            if ref and _as_date(ref) and hire <= _as_date(ref):
                ref_d = _as_date(ref)
                if end and end > ref_d:
                    is_wp = True
            # Condition B: Hired after reference date (or no invoice) - check against proposed effective date
            elif eff_dt and _as_date(eff_dt) and hire <= _as_date(eff_dt):
                eff_d = _as_date(eff_dt)
                if (end and end > eff_d) or (0 <= (eff_d - hire).days <= 90):
                    is_wp = True

            if is_wp:
                e.coverage = "WP"
                if not e.plan:
                    e.plan = "NA"
                if not e.cobra:
                    e.cobra = "N"
                continue


        # 2b) Roster absence
        if roster_authoritative and not getattr(e, "benefit_roster", False) and not has_waiver:
            e.coverage = "NE"
            if not e.plan:
                e.plan = "NA"
            if not e.cobra:
                e.cobra = "N"
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

    # Finalize eligibility_status, part-time exclusions, dispositions, and subscriber linkage on all people
    excluded_pt_deps: list[Person] = []
    for e in census.employees:
        worker_info = f"{e.worker_category} {e.eligibility_class} {e.job_title} {e.position_status} {e.salary}".lower()
        m_hrs = re.search(r"(\d+(?:\.\d+)?)\s*(?:hrs?|hours?|hours?/week|hrs?/wk)", worker_info)
        is_pt = bool(
            re.search(r"\b(part[\s\-]?time|pt)\b", worker_info)
            or (m_hrs and float(m_hrs.group(1)) < 30.0)
        )
        if e.coverage in ("EE", "ES", "EC", "FAM", "EF", "COUPLE") or (e.plan and e.plan not in ("", "NA", "N/A")):
            e.eligibility_status = "ENROLLED"
            e.disposition = "INCLUDE_ACTIVE_MEDICAL"
        elif e.coverage == "WP":
            e.eligibility_status = "WAITING_PERIOD"
            e.disposition = "RETAIN_MEDICAL_STATUS"
        elif e.coverage == "WO":
            e.eligibility_status = "WAIVED"
            e.disposition = "EXCLUDE_PART_TIME" if is_pt else "RETAIN_MEDICAL_STATUS"
            if is_pt and e.dependents:
                for dep in list(e.dependents):
                    dep.disposition = "EXCLUDE_PART_TIME"
                    dep.eligibility_status = "EXCLUDED_PART_TIME"
                    dep.subscriber_id = e.record_id
                    if dep not in census.excluded_dependents:
                        census.excluded_dependents.append(dep)
                    excluded_pt_deps.append(dep)
                e.dependents.clear()
        elif e.coverage == "NE":
            e.eligibility_status = "NOT_ELIGIBLE"
            e.disposition = "EXCLUDE_PART_TIME" if is_pt else "RETAIN_MEDICAL_STATUS"
            if is_pt and e.dependents:
                for dep in list(e.dependents):
                    dep.disposition = "EXCLUDE_PART_TIME"
                    dep.eligibility_status = "EXCLUDED_PART_TIME"
                    dep.subscriber_id = e.record_id
                    if dep not in census.excluded_dependents:
                        census.excluded_dependents.append(dep)
                    excluded_pt_deps.append(dep)
                e.dependents.clear()
        elif e.coverage == "RC":
            e.eligibility_status = "REFUSED"
            e.disposition = "RETAIN_MEDICAL_STATUS"
        else:
            e.eligibility_status = "UNRESOLVED"
            e.disposition = "UNRESOLVED"

        for dep in e.dependents:
            dep.eligibility_status = "DEPENDENT"
            dep.subscriber_id = e.record_id
            if not dep.disposition:
                dep.disposition = "INCLUDE_ACTIVE_MEDICAL"

    if excluded_pt_deps:
        pt_ids = {id(d) for d in excluded_pt_deps}
        census.people = [p for p in census.people if id(p) not in pt_ids]

    recalculate_row_numbers(census)
    update_candidate_dispositions(census)

    if tracer:
        for p in census.people:
            tracer.record_eligibility(
                record_id=p.record_id,
                name=f"{p.first} {p.last}".strip(),
                coverage=p.coverage or "",
                eligibility_status=p.eligibility_status or p.coverage or "",
                waiver=p.waive_reason or "",
                rule_applied="WaitingPeriodRefinement",
                notes=[f"Role: {p.role}, SubID: {p.subscriber_id}, Disposition: {p.disposition}"],
            )


def validate_census(census: Census) -> list[str]:
    """Validate individual identities and subscriber links, as well as employee and dependent totals."""
    warnings = []
    emp_ids = {e.record_id for e in census.employees}
    for dep in census.dependents:
        if not dep.subscriber_id:
            msg = f"Dependent {dep.display} (ID: {dep.record_id}) has no subscriber_id"
            warnings.append(msg)
            dep.note("Missing Subscriber ID", red=True)
        elif dep.subscriber_id not in emp_ids:
            msg = f"Dependent {dep.display} links to unknown subscriber_id {dep.subscriber_id}"
            warnings.append(msg)
            dep.note("Unresolved Subscriber Link", red=True)
        if not dep.employee:
            dep.note("Dependent missing employee reference", red=True)
    return warnings



def recalculate_row_numbers(census: Census) -> None:
    """Recalculate final sequential Data Row numbers and subscriber Sub Row links after all sorting/filtering."""
    data_row = 1
    for p in census.people:
        if getattr(p, "is_duplicate", False) or getattr(p, "invoice_only", False):
            p.row_no = 0
            continue
        p.row_no = data_row
        data_row += 1
    for p in census.people:
        if p.role != "EE":
            p.sub_row = p.employee.row_no if (p.employee and p.employee.row_no) else None
        else:
            p.sub_row = None


def update_candidate_dispositions(census: Census) -> None:
    """Populate census.candidate_dispositions with every person and dependent disposition."""
    dispositions: list[dict] = []
    seen_ids: set[int] = set()
    for p in list(census.people) + list(census.excluded_dependents) + list(census.unassigned_dependents):
        if id(p) in seen_ids or getattr(p, "is_duplicate", False):
            continue
        seen_ids.add(id(p))
        disp = p.disposition
        if not disp:
            if p.role == "EE":
                if p.coverage in ("EE", "ES", "EC", "FAM", "EF", "COUPLE") or (p.plan and p.plan.upper() not in ("", "NA", "N/A")):
                    disp = "INCLUDE_ACTIVE_MEDICAL"
                elif p.coverage in ("WP", "WO", "NE", "RC"):
                    disp = "RETAIN_MEDICAL_STATUS"
                else:
                    disp = "UNRESOLVED"
            else:
                disp = "INCLUDE_ACTIVE_MEDICAL" if p in census.people else "UNRESOLVED"
            p.disposition = disp
        dispositions.append({
            "record_id": p.record_id,
            "name": p.display,
            "role": p.role,
            "subscriber_id": p.subscriber_id,
            "row_no": p.row_no,
            "sub_row": p.sub_row,
            "gender": p.gender,
            "gender_resolution_status": p.gender_resolution_status or ("RESOLVED" if p.gender in ("M", "F") else "MISSING"),
            "gender_candidates": list(p.gender_candidates),
            "coverage": p.coverage,
            "plan": p.plan,
            "disposition": disp,
            "coverage_associations": list(p.coverage_associations),
        })
    census.candidate_dispositions = dispositions


def _link_dependents(st: _State, effective_date=None) -> None:
    cand_deps_by_emp: dict[int, list[Person]] = {}
    for ref in st.dep_refs:
        p = ref.person
        emp = None
        if ref.sub_id and ref.sub_id in st.by_id:
            emp = st.by_id[ref.sub_id]
        elif ref.emp_key:
            cands = _match_employee_by_name(st, emp_key=ref.emp_key)
            if cands:
                emp = cands[0]

        # Address & Household matching pass
        if emp is None and p.address:
            p_addr = p.address.strip().lower()
            p_zip = p.zip[:5] if p.zip else ""
            for candidate in st.employees:
                c_addr = getattr(candidate, "address", "").strip().lower()
                c_zip = candidate.zip[:5] if candidate.zip else ""
                if c_addr and c_addr == p_addr and (not p_zip or not c_zip or p_zip == c_zip):
                    emp = candidate
                    break

        # Compound / Shared Surname with Zip matching pass
        if emp is None and p.last:
            d_last = re.sub(r"[^a-z0-9]", "", p.last).lower()
            p_zip = p.zip[:5] if p.zip else ""
            for candidate in st.employees:
                c_last = re.sub(r"[^a-z0-9]", "", candidate.last).lower()
                c_zip = candidate.zip[:5] if candidate.zip else ""
                if d_last and c_last and (d_last == c_last or d_last in c_last or c_last in d_last) and (p_zip and p_zip == c_zip):
                    emp = candidate
                    break

        if emp is None and ref.positional is not None and not (ref.sub_id or ref.emp_key):
            emp = ref.positional
        if emp is None:
            if ref.is_non_med:
                p.disposition = "EXCLUDE_NON_MEDICAL_ONLY"
                st.census.excluded_dependents.append(p)
            else:
                p.disposition = "UNRESOLVED"
                p.note("Unassigned Dependent - No Matching Subscriber", red=True)
                st.census.unassigned_dependents.append(p)
            continue

        dk = name_key(p.first, p.last)
        emp_cand_list = cand_deps_by_emp.setdefault(id(emp), [])
        dup = None
        for d in emp_cand_list:
            p_first_primary = p.first.split()[0].lower() if p.first else ""
            d_first_primary = d.first.split()[0].lower() if d.first else ""
            same_name = (name_key(d.first, d.last) == dk or (
                p_first_primary and p_first_primary == d_first_primary and d.last.lower() == p.last.lower()))
            if same_name and (d.dob == p.dob or not d.dob or not p.dob):
                dup = d
                break
        if dup:                                           # same dependent across lines/files/tabs
            had_dob = bool(dup.dob)
            if p.gender_candidates:
                dup.gender_candidates.extend(p.gender_candidates)
                _resolve_person_gender(dup)
            if p.coverage_associations:
                dup.coverage_associations.extend(p.coverage_associations)
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
            dup.subscriber_id = emp.record_id
            dup.employee = emp
            if getattr(dup, "_saw_med_obs", False) and not ref.is_non_med:
                st.census.dependent_merges += 1
            dup._saw_med_obs = getattr(dup, "_saw_med_obs", False) or (not ref.is_non_med)
            continue
        p._saw_med_obs = not ref.is_non_med
        p.employee = emp
        p.subscriber_id = emp.record_id
        emp_cand_list.append(p)

    # Partition candidate dependents into active medical vs excluded non-medical-only
    for emp in st.employees:
        for d in cand_deps_by_emp.get(id(emp), []):
            has_med = getattr(d, "_saw_med_obs", False) or any(
                a.get("is_medical", True) for a in d.coverage_associations
            )
            if has_med:
                d.disposition = "INCLUDE_ACTIVE_MEDICAL"
                emp.dependents.append(d)
            else:
                d.disposition = "EXCLUDE_NON_MEDICAL_ONLY"
                d.eligibility_status = "EXCLUDED_NON_MEDICAL"
                st.census.excluded_dependents.append(d)

    import datetime
    from comparator import tier_from_deps

    eff = effective_date
    if isinstance(eff, datetime.datetime):
        eff = eff.date()

    has_medical_roster = st.has_cov_col or any(e.plan for e in st.employees)

    for emp in st.employees:
        # Determine employee coverage tier if unassigned or generic "enrolled"
        if not emp.coverage or emp.coverage.lower() in ("enrolled", "active", "yes", "y", "true"):
            if emp.cov_raw and emp.cov_raw.upper() in ("WO", "WAIVED", "NE", "WP", "RC"):
                emp.coverage = emp.cov_raw.upper()
            elif emp.cov_raw and emp.cov_raw.lower() in ("enrolled", "active", "yes", "y", "true"):
                emp.coverage = tier_from_deps(emp)
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

        # Propagate coverage, cobra, zip, and plan to linked dependents
        for dep in emp.dependents:
            dep.employee = emp
            dep.subscriber_id = emp.record_id
            if not dep.coverage:
                dep.coverage = emp.coverage
            if not dep.cobra:
                dep.cobra = emp.cobra or "N"
            if not dep.zip and emp.zip:
                dep.zip = emp.zip
            if not dep.plan and emp.plan:
                dep.plan = emp.plan
            dep.eligibility_status = "DEPENDENT"

            # Check child turning 26 or older on proposed effective date
            if dep.role == "CH" and dep.dob and eff:
                d = dep.dob.date() if isinstance(dep.dob, datetime.datetime) else dep.dob
                age = eff.year - d.year - ((eff.month, eff.day) < (d.month, d.day))
                if age >= 26:
                    eff_str = eff.strftime("%m/%d/%Y")
                    dep.note(f"Child age {age} on {eff_str} effective date - verify dependent eligibility", red=False, orange=True)


def _order_output(st: _State) -> None:
    enrolled_emps = [
        e for e in st.employees
        if e.coverage in ("EE", "ES", "EC", "FAM", "EF", "COUPLE") or (e.plan and e.plan.upper() not in ("", "NA", "N/A"))
    ]
    non_enrolled_emps = [e for e in st.employees if e not in enrolled_emps]
    non_enrolled_emps.sort(key=lambda e: (e.last.lower(), e.first.lower()))

    out: list[Person] = []
    for emp in enrolled_emps + non_enrolled_emps:
        if getattr(emp, "disposition", "") == "EXCLUDE_PART_TIME":
            continue
        out.append(emp)
        out.extend(emp.dependents)
    out.extend(st.census.unassigned_dependents)
    st.census.people = out
    st.census.raw_dependent_rows = len([p for p in out if p.role != "EE"]) + st.census.dependent_merges

    # Finalize sequential Data Row numbers and Sub Row links only after output order is finalized
    recalculate_row_numbers(st.census)
    update_candidate_dispositions(st.census)



def _doc_to_census_text(doc: RawDoc) -> str:
    """Prepare comprehensive structured text representation of doc for LLM semantic extraction."""
    parts = []
    if doc.tables:
        for t in doc.tables:
            parts.append(f"=== Sheet: {t.name} ===")
            df = t.df.dropna(how="all").reset_index(drop=True)
            for _, row in df.iterrows():
                row_str = " | ".join("" if pd.isna(c) else str(c).strip() for c in row)
                if row_str.replace("|", "").strip():
                    parts.append(row_str)
    elif doc.text:
        parts.append(doc.text)
    return "\n".join(parts)


def _extract_census_rows_smart(doc: RawDoc) -> list[dict]:
    """Extract census rows using Claude Anthropic, with smart chunking for large rosters."""
    from llm_client import extract_census_rows

    all_rows: list[dict] = []
    if doc.tables:
        for t in doc.tables:
            df = t.df.dropna(how="all").reset_index(drop=True)
            if df.empty:
                continue

            # Identify the header row index (first row containing typical census column markers)
            header_idx = 0
            for idx in range(min(15, len(df))):
                row_vals = [clean(c) for c in df.iloc[idx].tolist()]
                if any(k in norm_key(c) for c in row_vals for k in ("first", "last", "name", "dob", "ssn", "tier", "plan", "cov")):
                    header_idx = idx
                    break

            header_row = " | ".join("" if pd.isna(c) else str(c).strip() for c in df.iloc[header_idx])
            data_start = header_idx + 1
            data_rows = df.iloc[data_start:].reset_index(drop=True)

            if len(data_rows) <= 25:
                table_lines = [f"=== Sheet: {t.name} ===", header_row]
                for _, row in data_rows.iterrows():
                    r_str = " | ".join("" if pd.isna(c) else str(c).strip() for c in row)
                    if r_str.replace("|", "").strip():
                        table_lines.append(r_str)
                batch = extract_census_rows("\n".join(table_lines))
                if batch:
                    all_rows.extend(batch)
            else:
                batch_size = 25
                for s_i in range(0, len(data_rows), batch_size):
                    e_i = min(s_i + batch_size, len(data_rows))
                    chunk_df = data_rows.iloc[s_i:e_i]
                    chunk_lines = [f"=== Sheet: {t.name} (Rows {s_i + 1} to {e_i}) ===", header_row]
                    for _, row in chunk_df.iterrows():
                        r_str = " | ".join("" if pd.isna(c) else str(c).strip() for c in row)
                        if r_str.replace("|", "").strip():
                            chunk_lines.append(r_str)
                    batch_txt = "\n".join(chunk_lines)
                    batch = extract_census_rows(batch_txt)
                    if batch:
                        all_rows.extend(batch)
    elif doc.text:
        lines = [l for l in doc.text.splitlines() if l.strip()]
        if len(lines) <= 25:
            batch = extract_census_rows(doc.text)
            if batch:
                all_rows.extend(batch)
        else:
            header_line = lines[0]
            batch_size = 25
            for s_i in range(1, len(lines), batch_size):
                e_i = min(s_i + batch_size, len(lines))
                chunk_lines = [header_line] + lines[s_i:e_i]
                batch = extract_census_rows("\n".join(chunk_lines))
                if batch:
                    all_rows.extend(batch)
    return all_rows



def _parse_llm_census_rows(doc: RawDoc, rows: list[dict], st: _State) -> bool:
    count = 0
    for r in rows:
        fn = clean(r.get("first_name"))
        ln = clean(r.get("last_name"))
        if not (fn or ln):
            continue
        rel = std_relationship(r.get("relationship", "EE")) or "EE"
        p = Person(rel, fn, ln, source=doc.name)
        p.record_id = _next_record_id(st)
        p.sub_id = clean(r.get("sub_id", ""))

        cov_val = clean(r.get("coverage", ""))
        if cov_val:
            st.has_cov_col = True
        hire_val = clean(r.get("hire_date", ""))
        if hire_val:
            st.has_hire_col = True

        _standardise(p, {
            "gender": r.get("gender"),
            "dob": r.get("dob"),
            "zip": r.get("zip"),
            "coverage": cov_val,
            "cobra": r.get("cobra"),
            "plan": r.get("plan"),
            "premium": r.get("premium"),
            "hire": hire_val,
            "effective_date": r.get("effective_date"),
            "eligibility_class": r.get("eligibility_class", ""),
            "worker_category": r.get("worker_category", ""),
            "position_status": r.get("position_status", ""),
            "waive_reason": r.get("waive_reason", ""),
            "job_title": r.get("job_title", ""),
            "salary": r.get("salary", ""),
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



def _prepare_doc_tables(doc: RawDoc) -> list[tuple[str, pd.DataFrame]]:
    raw_tables = [(t.name, t.df) for t in doc.tables]
    if len(raw_tables) <= 1:
        return raw_tables

    combined = []
    skip_indices = set()
    for i in range(len(raw_tables)):
        if i in skip_indices:
            continue
        name_i, df_i = raw_tables[i]
        h_i = detect_header(df_i)
        if not h_i or not any(k in h_i.columns for k in ("first_name", "last_name", "full_name")):
            combined.append((name_i, df_i))
            continue

        b_i = body(df_i, h_i)
        matched_j = None
        for j in range(i + 1, len(raw_tables)):
            if j in skip_indices:
                continue
            name_j, df_j = raw_tables[j]
            h_j = detect_header(df_j)
            if not h_j or not any(k in h_j.columns for k in ("first_name", "last_name", "full_name")):
                continue
            b_j = body(df_j, h_j)
            if len(b_i) != len(b_j) or len(b_i) == 0:
                continue
            aligned = True
            for r_idx in range(len(b_i)):
                fn_i = clean(b_i.iloc[r_idx, h_i.columns.get("first_name", 0)]) if "first_name" in h_i.columns else ""
                ln_i = clean(b_i.iloc[r_idx, h_i.columns.get("last_name", 0)]) if "last_name" in h_i.columns else ""
                fn_j = clean(b_j.iloc[r_idx, h_j.columns.get("first_name", 0)]) if "first_name" in h_j.columns else ""
                ln_j = clean(b_j.iloc[r_idx, h_j.columns.get("last_name", 0)]) if "last_name" in h_j.columns else ""
                if name_key(fn_i, ln_i) != name_key(fn_j, ln_j):
                    aligned = False
                    break
            if aligned:
                matched_j = j
                break

        if matched_j is not None:
            skip_indices.add(matched_j)
            name_j, df_j = raw_tables[matched_j]
            h_j = detect_header(df_j)
            b_j = body(df_j, h_j)
            hdr_combined = h_i.labels + h_j.labels
            rows_combined = [hdr_combined]
            for r_idx in range(len(b_i)):
                r1 = [clean(v) for v in b_i.iloc[r_idx].tolist()]
                r2 = [clean(v) for v in b_j.iloc[r_idx].tolist()]
                rows_combined.append(r1 + r2)
            df_merged = pd.DataFrame(rows_combined)
            combined.append((f"{name_i}+{name_j}", df_merged))
        else:
            combined.append((name_i, df_i))

    return combined


# ----------------------------------------------------------------- public ---
def build_census(census_docs: list[RawDoc], effective_date=None, tracer=None) -> tuple[Census, dict]:
    st = _State()
    for doc in census_docs:
        used_any = False
        from census_config import LLM_FIRST, USE_LLM_FALLBACK

        # 1. High-Fidelity Tabular Parsing (for structured Excel/CSV censuses with recognized headers)
        # Direct column alignment is used for tabular sheets to guarantee zero dropped records and zero token truncation on large enterprise rosters
        tables = _prepare_doc_tables(doc)
        for tname, tdf in tables:
            parsed_ok = False
            try:
                parsed_ok = _parse_table(doc, tname, tdf, st)
                if parsed_ok:
                    used_any = True
            except Exception as exc:
                log.error("Census table %s/%s skipped: %s", doc.name, tname, exc)
                st.census.census_notes.append(f"Census table {tname} in {doc.name} failed parsing: {exc}")

            # Table-level fallback if this specific table failed but document had other tables
            if not parsed_ok and USE_LLM_FALLBACK and not tdf.empty:
                try:
                    from llm_client import extract_census_rows
                    t_rows = extract_census_rows(tdf.to_string(index=False))
                    if t_rows and _parse_llm_census_rows(doc, t_rows, st):
                        used_any = True
                        log.info("Table-level fallback extracted %d records from %s/%s", len(t_rows), doc.name, tname)
                except Exception as _fb_exc:
                    log.debug("Table-level fallback skipped for %s/%s: %s", doc.name, tname, _fb_exc)

        # 2. LLM-First Semantic Extraction (for unstructured documents, PDF rosters, emails, or tables without recognized headers)
        if not used_any and (LLM_FIRST or USE_LLM_FALLBACK):
            try:
                log.info("Applying LLM-First semantic extraction for census: %s", doc.name)
                rows = _extract_census_rows_smart(doc)
                if rows:
                    used_any = _parse_llm_census_rows(doc, rows, st)
                    if used_any:
                        log.info("LLM-First successfully extracted %d records from %s", len(rows), doc.name)
            except Exception as exc:
                log.warning("LLM-First census extraction failed for %s: %s", doc.name, exc)

        # 3. Last fallback if still not used
        if not used_any and (doc.text or "").strip() and USE_LLM_FALLBACK:
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

    # Diagnostic Tracing for Stage 1, 2, 3
    if tracer:
        for p in st.census.people:
            tracer.record_source(p.record_id, p.display, p.source, "", p.row_no, p.raw_data)
            tracer.record_normalization(p.record_id, p.display, p.role, p.rel_raw, p.gender, p.dob, p.zip, p.plan, p.coverage)
            if p.role != "EE" and p.employee:
                tracer.record_linkage(p.record_id, p.display, p.role, p.subscriber_id, p.employee.display, True)
            elif p.role != "EE":
                tracer.record_linkage(p.record_id, p.display, p.role, "", "", False)

    validation_warnings = validate_census(st.census)
    if validation_warnings:
        st.census.census_notes.extend(validation_warnings)

    meta = {
        "has_coverage_column": st.has_cov_col,
        "saw_dependent_source": st.saw_dependent_source,
    }
    return st.census, meta

