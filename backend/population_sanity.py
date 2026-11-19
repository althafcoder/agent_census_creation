"""
Dynamic Pre-Comparison Population Sanity Gate (Fix #3).

Cross-checks a freshly parsed ``Census`` against the submission's own stated evidence
(GHQ/RFP headcount, client email headcount, invoice subscriber count, and internal
structural invariants like unassigned-dependent spikes, dependent-to-employee ratios,
and demographic completeness).

Zero client/carrier values are hardcoded: every expected count is read dynamically
from the submission's own documents, and every threshold is structural/proportional.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import census_config as C

log = logging.getLogger("census.sanity")

# Matches employer-stated headcounts in submission emails / RFPs, e.g.
# "42 employees", "total of 42 full-time employees", "42 active FT EEs".
_EMAIL_HEADCOUNT_PATTERNS = (
    re.compile(
        r"\b(?:total(?:\s+of)?|currently\s+have|employs?|headcount(?:\s+of|\s*[:=])?|has)\s+"
        r"(\d{1,5})\s+(?:total\s+|active\s+|full[\s\-]?time\s+|ft\s+|benefit[\s\-]?eligible\s+|eligible\s+)*"
        r"(?:employees?|ees?)\b",
        re.I,
    ),
    re.compile(
        r"\b(\d{1,5})\s+(?:total\s+)?(?:full[\s\-]?time|ft|active|benefit[\s\-]?eligible)\s+"
        r"(?:employees?|ees?)\b",
        re.I,
    ),
)

_NON_STATED_ROLES = {"invoice", "renewal", "plan_design", "payroll", "deliverable", "claims"}


@dataclass
class SanityIssue:
    code: str
    severity: str          # "critical" | "warning"
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass
class PopulationSanityReport:
    passed: bool
    blocked: bool
    issues: list[SanityIssue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocked": self.blocked,
            "critical_count": self.critical_count,
            "issues": [i.to_dict() for i in self.issues],
            "metrics": dict(self.metrics),
        }


def extract_stated_headcount(
    prospect: Any = None,
    docs: Optional[list[Any]] = None,
    roles: Optional[dict[str, list[Any]]] = None,
) -> tuple[Optional[int], str]:
    """Return ``(stated_employee_count, source_description)`` from the submission's own documents.

    Precedence:
      1. ``prospect.ft_employees`` (declared FT count from GHQ/RFP)
      2. ``prospect.stated_total_employees`` (table total from GHQ/RFP, e.g. '# of Employees' Totals row)
      3. Client email / RFP narrative statements (ignoring invoices, renewals, SBCs, payroll)
    """
    if prospect is not None:
        ft = getattr(prospect, "ft_employees", None)
        if isinstance(ft, int) and ft > 0:
            src = getattr(prospect, "ft_employees_source", "") or "GHQ/RFP"
            return ft, src
        st = getattr(prospect, "stated_total_employees", None)
        if isinstance(st, int) and st > 0:
            src = getattr(prospect, "stated_total_source", "") or "GHQ/RFP"
            return st, src

    excluded_ids: set[int] = set()
    candidate_docs: list[Any] = []
    if roles:
        for rname in _NON_STATED_ROLES:
            for d in roles.get(rname, []) or []:
                excluded_ids.add(id(d))
        for rname in ("rfp", "ghq", "unknown"):
            for d in roles.get(rname, []) or []:
                if id(d) not in excluded_ids:
                    candidate_docs.append(d)
    elif docs:
        candidate_docs = list(docs)

    for d in candidate_docs:
        text = getattr(d, "text", "") or ""
        if not text:
            continue
        # Only inspect email / RFP / questionnaire prose, skipping giant tabular dumps
        snippet = text[:8000]
        for pat in _EMAIL_HEADCOUNT_PATTERNS:
            m = pat.search(snippet)
            if m:
                try:
                    val = int(m.group(1))
                except ValueError:
                    continue
                if 2 <= val <= 50000:
                    return val, getattr(d, "name", "submission document")
    return None, ""


def evaluate_population_sanity(
    census: Any,
    prospect: Any = None,
    invoice_records: Optional[list[Any]] = None,
    docs: Optional[list[Any]] = None,
    roles: Optional[dict[str, list[Any]]] = None,
) -> PopulationSanityReport:
    """Evaluate structural and cross-document sanity of a built ``Census``.

    Returns a ``PopulationSanityReport`` with ``blocked=True`` when any critical
    structural failure is present (indicating likely column/relationship mis-parsing
    rather than ordinary minor HR variance).
    """
    issues: list[SanityIssue] = []

    if census is None:
        return PopulationSanityReport(passed=True, blocked=False)

    emps = [
        p for p in getattr(census, "people", [])
        if p.role == "EE"
        and not getattr(p, "is_duplicate", False)
        and not getattr(p, "invoice_only", False)
        and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")
    ]
    deps = [
        p for p in getattr(census, "people", [])
        if p.role != "EE"
        and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")
    ]
    unassigned = list(getattr(census, "unassigned_dependents", []) or [])
    emp_count = len(emps)
    dep_count = len(deps)
    unassigned_count = len(unassigned)
    enrolled_emp_count = sum(1 for e in emps if (e.coverage or "").upper() in C.ACTIVE_TIERS)
    inv_count = len(invoice_records or [])
    stated_hc, stated_src = extract_stated_headcount(prospect=prospect, docs=docs, roles=roles)

    metrics = {
        "parsed_employees": emp_count,
        "parsed_dependents": dep_count,
        "enrolled_employees": enrolled_emp_count,
        "unassigned_dependents": unassigned_count,
        "stated_headcount": stated_hc,
        "stated_headcount_source": stated_src,
        "invoice_subscribers": inv_count,
        "dep_to_emp_ratio": round((dep_count + unassigned_count) / emp_count, 2) if emp_count else None,
    }

    # 1. Unassigned dependents explosion (signature of mis-classified relationship column)
    if (emp_count == 0 and unassigned_count > 0) or (
        unassigned_count >= max(5, int(max(1, emp_count) * 0.25))
    ):
        issues.append(SanityIssue(
            code="UNASSIGNED_DEPENDENTS_SPIKE",
            severity="critical",
            message=(
                f"Population sanity gate: {unassigned_count} unassigned dependent(s) against "
                f"{emp_count} employee(s) — likely mis-mapped relationship/employee-indicator column."
            ),
            details={"unassigned_dependents": unassigned_count, "parsed_employees": emp_count},
        ))

    # 2. Employer-stated headcount (GHQ / RFP / client email) vs parsed census employees
    # Account for legitimately excluded Part-Time employees when the stated count is a total-payroll headcount.
    pt_excluded_count = len(getattr(census, "excluded_employees", []) or [])
    total_source_emp_count = emp_count + pt_excluded_count
    if stated_hc is not None and stated_hc >= 5 and emp_count > 0:
        diff = emp_count - stated_hc
        ratio = emp_count / stated_hc
        total_ratio = total_source_emp_count / stated_hc
        total_matches = 0.65 <= total_ratio <= 1.45
        if not total_matches and ((ratio >= 1.5 and diff >= 5) or (ratio <= 0.5 and -diff >= 5)):
            issues.append(SanityIssue(
                code="STATED_HEADCOUNT_DIVERGENCE",
                severity="critical",
                message=(
                    f"Population sanity gate: parsed {emp_count} census employees, but {stated_src} "
                    f"states {stated_hc} employees ({ratio:.2f}x) — check for duplicated rows or "
                    f"mis-classified employee/dependent records."
                ),
                details={
                    "parsed_employees": emp_count,
                    "stated_headcount": stated_hc,
                    "source": stated_src,
                    "ratio": round(ratio, 2),
                },
            ))

    # 3. Dependent-to-employee ratio anomaly
    total_deps = dep_count + unassigned_count
    if emp_count >= 5 and total_deps / emp_count > 3.5:
        issues.append(SanityIssue(
            code="DEPENDENT_RATIO_ANOMALY",
            severity="critical",
            message=(
                f"Population sanity gate: {total_deps} dependents for {emp_count} employees "
                f"({total_deps / emp_count:.2f}x ratio > 3.5x) — actual employees may have been "
                f"classified as dependents."
            ),
            details={"total_dependents": total_deps, "parsed_employees": emp_count},
        ))

    # 4. Invoice subscriber count vs census sanity
    if inv_count >= 5 and emp_count > 0:
        if emp_count < inv_count * 0.4 and (inv_count - emp_count) >= 5:
            issues.append(SanityIssue(
                code="INVOICE_SUBSCRIBER_DIVERGENCE",
                severity="critical",
                message=(
                    f"Population sanity gate: parsed only {emp_count} census employees vs "
                    f"{inv_count} invoice subscribers — majority of invoiced employees missing from census."
                ),
                details={"parsed_employees": emp_count, "invoice_subscribers": inv_count},
            ))
        elif enrolled_emp_count > inv_count * 2.2 and (enrolled_emp_count - inv_count) >= 10:
            issues.append(SanityIssue(
                code="INVOICE_SUBSCRIBER_DIVERGENCE",
                severity="critical",
                message=(
                    f"Population sanity gate: {enrolled_emp_count} enrolled census employees vs "
                    f"{inv_count} invoice subscribers ({enrolled_emp_count / inv_count:.2f}x) — "
                    f"possible duplicated or mis-classified roster rows."
                ),
                details={"enrolled_employees": enrolled_emp_count, "invoice_subscribers": inv_count},
            ))
        elif emp_count > inv_count * 2.5 and (emp_count - inv_count) >= 10:
            issues.append(SanityIssue(
                code="INVOICE_SUBSCRIBER_WARNING",
                severity="warning",
                message=(
                    f"Population sanity notice: {emp_count} total census employees vs "
                    f"{inv_count} invoice subscribers (verify waived/non-eligible participation)."
                ),
                details={"parsed_employees": emp_count, "invoice_subscribers": inv_count},
            ))

    # 5. Core demographics collapse (column misalignment fingerprint)
    all_people = emps + deps
    if len(all_people) >= 5:
        missing_both = sum(
            1 for p in all_people
            if not getattr(p, "dob", None) and (not getattr(p, "first", "") or not getattr(p, "last", ""))
        )
        if missing_both / len(all_people) > 0.5:
            issues.append(SanityIssue(
                code="DEMOGRAPHICS_COLLAPSE",
                severity="critical",
                message=(
                    f"Population sanity gate: {missing_both}/{len(all_people)} parsed records are "
                    f"missing both DOB and full name — likely misaligned census columns."
                ),
                details={"missing_both": missing_both, "total_people": len(all_people)},
            ))

    blocked = any(i.severity == "critical" for i in issues)
    return PopulationSanityReport(
        passed=not blocked,
        blocked=blocked,
        issues=issues,
        metrics=metrics,
    )
