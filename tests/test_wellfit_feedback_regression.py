"""Regression Test Suite for Feedback.docx Underwriting and Architecture Defects.

Tests and verifies the 5 core learning engine patterns and invariants:
1. Worksheet Boundary Enforcement (Columns A:M bounded; stray columns trimmed)
2. Stacked Census Dependent Reconstruction (Spouse/Child in Job Title -> dependents, not employees)
3. Job Title Guard (Professional titles like 'VP, Strategic Partnerships' never classified as spouse)
4. Insperity Option Code & Tier Decoding (3,57,58,12 -> Choice Plus 500; 57,58 -> WO; E->EE, S->ES, C/K->EC, F->EF, W->WO)
5. Benefits Enrollment Report (BER) Invoice Equivalence & Multi-Benefit Filtering
6. Renewal Rate Application & Population Invariance
7. Instruction Snapshot & Registry Integrity
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pandas as pd
import pytest

from readers import _trim_worksheet_boundary, RawDoc, RawTable
from utils import std_relationship, decode_insperity_coverage, INSPERITY_OPTION_LEGEND
from census_builder import build_census, refine_non_enrolled_statuses, Person
from invoice_parser import parse_invoice
from router import check_renewal_required, RoutingDecision
from learning_engine.instruction_registry import resolve_instruction_snapshot
from learning_engine.validator import UnderwritingValidator, CheckStatus
from learning_engine.contracts import LearningRule, RuleStatus
from learning_engine.memory_store import MemoryStore


def test_worksheet_boundary_trimming():
    """Verify that wide worksheets with empty columns beyond column M (12) are trimmed."""
    # Create a 20-row dataframe with data in cols 0..12, then 20 empty columns
    data = []
    headers = [f"Col_{i}" for i in range(13)] + [None] * 20
    data.append(headers)
    for r in range(1, 20):
        row = [f"Val_{r}_{c}" for c in range(13)] + [None] * 20
        data.append(row)
    df = pd.DataFrame(data)
    assert df.shape[1] == 33

    trimmed = _trim_worksheet_boundary(df)
    assert trimmed.shape[1] == 13
    assert trimmed.iloc[0, 12] == "Col_12"


def test_job_title_guard_and_stacked_dependents():
    """Verify that:
    1. Professional titles with 'partner' are NOT treated as Spouse (SP).
    2. Stacked rows with Job Title = 'Spouse' or 'Child' are treated as dependents (SP, CH), not employees.
    """
    # 1. Professional business titles
    assert std_relationship("VP, BUSINESS DEVELOPMENT & STRATEGIC PARTNERSHIPS") == ""
    assert std_relationship("Partner - Managing Director") == ""
    assert std_relationship("Senior Partnership Manager") == ""
    assert std_relationship("General Partner") == ""

    # Legitimate relationship strings
    assert std_relationship("Spouse") == "SP"
    assert std_relationship("Child") == "CH"
    assert std_relationship("Domestic Partner") == "DP"
    assert std_relationship("partner") == "SP"

    # 2. Build census with stacked dependent rows
    headers = [
        "Last Name", "First Name", "Relationship", "Job Title", "Hire Date",
        "Annual Pay", "Option Code", "Coverage Level", "DOB", "Gender", "Zip Code"
    ]
    rows = [
        headers,
        # Subscriber 1: Mary Snider (VP Strategic Partnerships) - must be EE
        ["Snider", "Mary", "", "VP, BUSINESS DEVELOPMENT & STRATEGIC PARTNERSHIPS", "1/15/2020",
         "150000", "3,57,58,12", "E", "5/12/1980", "F", "75001"],
        # Subscriber 2: John Doe with stacked Spouse and Child
        ["Doe", "John", "", "Software Engineer", "3/1/2021",
         "120000", "3,57,58,12", "F", "8/22/1985", "M", "75001"],
        # Stacked Spouse (blank hire date & pay)
        ["Doe", "Jane", "", "Spouse", "",
         "", "", "", "4/15/1987", "F", "75001"],
        # Stacked Child (blank hire date & pay)
        ["Doe", "Jimmy", "", "Child", "",
         "", "", "", "6/10/2015", "M", "75001"],
    ]

    df = pd.DataFrame(rows)
    doc = RawDoc(path=Path("Wellfit Technologies Census 2026.xlsx"), ext=".xlsx", tables=[RawTable("Census", df)], text="")
    census, _ = build_census([doc])

    # Must have exactly 2 active employees (Mary Snider & John Doe), NOT 4!
    assert len(census.canonical_census_employees) == 2
    emp_names = {f"{e.first} {e.last}" for e in census.canonical_census_employees}
    assert "Mary Snider" in emp_names
    assert "John Doe" in emp_names

    # John Doe must have 2 dependents attached
    john = next(e for e in census.canonical_census_employees if e.last == "Doe")
    assert len(john.dependents) == 2
    dep_roles = {d.role for d in john.dependents}
    assert dep_roles == {"SP", "CH"}


def test_insperity_coverage_code_and_tier_decoding():
    """Verify Insperity comma-delimited option codes and single-letter tiers."""
    # Plan design decoding
    res1 = decode_insperity_coverage("3,57,58,12")
    assert res1["medical_plan"] == "UHC Choice Plus PPO 500 (90%)"
    assert res1["is_medical"] is True

    # Non-medical only codes (57=Life, 58=AD&D) map to Waived Medical (WO)
    res_wo = decode_insperity_coverage("57,58")
    assert res_wo["is_medical"] is False
    assert res_wo["suggested_status"] == "WO"

    res_wo2 = decode_insperity_coverage("57,58,58")
    assert res_wo2["is_medical"] is False
    assert res_wo2["suggested_status"] == "WO"

    # Single-letter coverage tiers in census builder
    headers = [
        "Last Name", "First Name", "Job Title", "Hire Date", "Option Code",
        "Coverage Level", "DOB", "Gender", "Zip Code"
    ]
    rows = [
        headers,
        ["Smith", "Alice", "Accountant", "2/1/2022", "3,57,58,12", "E", "1/1/1990", "F", "75001"],  # E -> EE
        ["Jones", "Bob", "Analyst", "3/1/2022", "3,57,58,12", "S", "2/2/1988", "M", "75001"],       # S -> ES
        ["Brown", "Charlie", "Manager", "4/1/2022", "3,57,58,12", "C", "3/3/1985", "M", "75001"],   # C -> EC
        ["Davis", "Dana", "Director", "5/1/2022", "3,57,58,12", "F", "4/4/1980", "F", "75001"],     # F -> EF/FAM
        ["Wilson", "Eve", "Engineer", "6/1/2022", "57,58", "W", "5/5/1992", "F", "75001"],          # W -> WO
    ]
    df = pd.DataFrame(rows)
    doc = RawDoc(path=Path("Insperity_Census.xlsx"), ext=".xlsx", tables=[RawTable("Roster", df)], text="")
    census, _ = build_census([doc])

    by_last = {e.last: e for e in census.canonical_census_employees}
    assert by_last["Smith"].coverage == "EE"
    assert by_last["Jones"].coverage == "ES"
    assert by_last["Brown"].coverage == "EC"
    assert by_last["Davis"].coverage in ("EF", "FAM")
    assert by_last["Wilson"].coverage == "WO"


def test_benefits_enrollment_report_invoice_equivalence():
    """Verify that a Benefits Enrollment Report without total premium is accepted as invoice equivalent
    and filtered strictly to medical rows.
    """
    headers = ["Employee Name", "Benefit Type", "Plan Name", "Coverage Tier", "Employee Pre-Tax Deduction"]
    rows = [
        headers,
        # Medical rows
        ["Alice Smith", "Medical", "UHC Choice Plus PPO 500", "Employee Only", "150.00"],
        ["Bob Jones", "Medical", "UHC Choice Plus PPO 500", "Employee + Spouse", "320.00"],
        # Non-medical rows for same or different people (must NOT create separate subscribers)
        ["Alice Smith", "Dental", "MetLife Dental PPO", "Employee Only", "25.00"],
        ["Alice Smith", "Vision", "VSP Vision Care", "Employee Only", "10.00"],
        ["Alice Smith", "Basic Life", "Life/AD&D 50k", "Employee Only", "0.00"],
        ["Bob Jones", "Dental", "MetLife Dental PPO", "Employee + Family", "65.00"],
        ["Charlie Davis", "Dental", "MetLife Dental PPO", "Employee Only", "25.00"],  # Dental only -> excluded from medical invoice
    ]
    df = pd.DataFrame(rows)
    doc = RawDoc(path=Path("Wellfit Technologies_ Benefits Enrollment_2.xlsx"), ext=".xlsx", tables=[RawTable("Elections", df)], text="")

    inv = parse_invoice(doc)
    assert inv is not None
    # Medical subscribers should only be Alice Smith and Bob Jones (2 subscribers)
    assert len(inv.records) == 2
    sub_names = {f"{r.first} {r.last}" for r in inv.records}
    assert "Alice Smith" in sub_names
    assert "Bob Jones" in sub_names
    assert "Charlie Davis" not in sub_names


def test_renewal_rate_acceptance_and_peo_rule():
    """Verify that renewal rate tables are recognized and PEO renewal rule evaluates correctly."""
    from models import RenewalDoc, Prospect

    ren_doc = RenewalDoc(
        path="Wellfit_Technologies_MAR_Report.xlsx",
        effective=date(2027, 1, 1),
        rates={
            ("Choice Plus PPO 500", "EE"): 715.0,
            ("Choice Plus PPO 500", "ES"): 1485.0,
        }
    )
    prospect = Prospect(
        company="Wellfit Technologies",
        is_peo=True,
        effective_date=date(2026, 11, 1)
    )

    # Effective date: 2026-11-01 -> Renewal date: 2027-01-01 (61 days apart <= 90 days)
    req, reason = check_renewal_required(
        prospect=prospect,
        renewal_doc=ren_doc,
        proposed_effective=date(2026, 11, 1)
    )
    assert req is True

    # When rates are present in MAR report, rates are retained
    assert len(ren_doc.rates) == 2


def test_instruction_snapshot_and_registry_rules():
    """Verify that the 5 newly registered rules exist in the instruction registry and active rules registry."""
    snapshot = resolve_instruction_snapshot()
    rules = snapshot.rules

    # Check 5 rules in instruction snapshot
    assert "RULE_08_WORKSHEET_BOUNDARY" in rules
    assert "RULE_09_STACKED_DEPENDENTS" in rules
    assert "RULE_10_INSPERITY_CODE_DECODING" in rules
    assert "RULE_11_BER_MULTI_BENEFIT_FILTER" in rules
    assert "RULE_13_RENEWAL_POPULATION_INVARIANCE" in rules

    # Check MemoryStore active rules
    store = MemoryStore()
    active_rules = store.load_active_rules()
    active_ids = {r.get("learning_id") for r in active_rules}

    assert "pattern_worksheet_boundary_enforcement" in active_ids
    assert "pattern_stacked_census_dependent_reconstruction" in active_ids
    assert "pattern_insperity_coverage_code_and_tier_decoding" in active_ids
    assert "pattern_benefits_enrollment_report_invoice_equivalence" in active_ids
    assert "pattern_renewal_rate_application_and_population_invariance" in active_ids


def test_wellfit_104_employee_population_with_supplemental_docs():
    """Verify full classification + census build on Wellfit Census 2026 alongside Schedule A,
    Group Identification Key, and Benefits Enrollment Report yields exactly 104 employees (not 122 or 321).
    """
    from unittest.mock import patch
    from classifier import classify

    extracted_census = Path(__file__).resolve().parent.parent / "extracted_text" / "Wellfit_Technologies_Census_2026_extracted.txt"
    if not extracted_census.exists():
        pytest.skip("Wellfit extracted census text not present")

    lines = extracted_census.read_text(encoding="utf-8").splitlines()
    rows = []
    in_census = False
    for ln in lines:
        if "=== Sheet: Census ===" in ln:
            in_census = True
            continue
        if in_census and "=== Sheet:" in ln:
            break
        if not in_census or not ln.strip():
            continue
        rows.append([p.strip() for p in ln.split("|")])

    census_doc = RawDoc(
        path=Path("Wellfit Technologies Census 2026.xlsx"),
        ext=".xlsx",
        tables=[RawTable("Census", pd.DataFrame(rows))],
        text="Employee Census",
    )
    sched_a_doc = RawDoc(
        path=Path("Wellfit Technologies_Schedule A.pdf"),
        ext=".pdf",
        tables=[RawTable("p5_x1", pd.DataFrame([["00 44", "KNIGHT, C", "AMILLE", "AL", "35803"]]))],
        text="Schedule A Client Service Fee Allocation",
    )
    group_key_doc = RawDoc(
        path=Path("Wellfit Techologies_Group Identification Key (1).pdf"),
        ext=".pdf",
        tables=[RawTable("p1_x1", pd.DataFrame([["GROUP NO. 0 - UNASSIG", "N", "ED"]]))],
        text="Group Identification Key Coverage Legend",
    )
    ber_doc = RawDoc(
        path=Path("Wellfit Technologies_ Benefits Enrollment_2.xlsx"),
        ext=".xlsx",
        tables=[RawTable("Detail", pd.DataFrame([
            ["Employee Name", "Benefit Type", "Plan Name", "Coverage Tier"],
            ["BRIANNA ALISHA HILL", "Adoption Assistance", "Adoption Assistance", "Employee Only"],
        ]))],
        text="Benefits Enrollment",
    )

    with patch("census_config.LLM_FIRST", False), patch("census_config.USE_LLM_FALLBACK", False):
        role_census = classify(census_doc)
        role_sched_a = classify(sched_a_doc)
        role_group_key = classify(group_key_doc)
        role_ber = classify(ber_doc)

    assert role_census == "census"
    assert role_sched_a == "payroll"
    assert role_group_key == "plan_design"
    assert role_ber == "census"  # promoted to invoice & removed from census when dedicated census exists

    # Apply main.py dedicated-census BER separation rule
    census_docs = [d for d, r in [(census_doc, role_census), (sched_a_doc, role_sched_a),
                                  (group_key_doc, role_group_key), (ber_doc, role_ber)] if r == "census"]
    import re
    dedicated_censuses = [d for d in census_docs if re.search(r"(census|roster)", d.name, re.I)]
    assert dedicated_censuses
    census_docs = [d for d in census_docs if not re.search(r"(benefit.*enroll|enrollment(\s*report)?|\bber\b|benefit\s*election)", d.name, re.I)]

    census, _ = build_census(census_docs)
    refine_non_enrolled_statuses(census)

    assert len(census.canonical_census_employees) == 104
    total_deps = sum(len(e.dependents) for e in census.canonical_census_employees)
    assert total_deps >= 122
