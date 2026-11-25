"""Regression tests for universal Phase A & Phase B underwriting invariants.

All assertions are structural and client-agnostic (zero hardcoded prospect logic):
  1. COBRA Enrollee / continuation participant maps to primary subscriber (EE, cobra='Y').
  2. Invalid ZIPs (e.g. 6-digit '310122') are left blank and flagged — never truncated.
  3. Unnumbered leading template rows before sequential 1..N index and form-prompt rows
     are filtered structurally without a hardcoded fake-name list.
  4. Month-labeled current premium columns (e.g. 'November 2026' vs 'Effective Dec. 1, 2026')
     are parsed by invoice_parser for dual-purpose BER/renewal workbooks.
  5. PythonSandboxSession enforces ZIP validity in emit_canonical_census and supports
     emit_canonical_renewal -> canonical_json_to_renewal.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from agent_orchestrator import canonical_json_to_renewal
from census_builder import build_census
from invoice_parser import _records_from_tables
from readers import RawDoc, RawTable
from python_sandbox import PythonSandboxSession
from utils import std_relationship


def test_cobra_enrollee_maps_to_primary_subscriber():
    assert std_relationship("COBRA Enrollee") == "EE"
    assert std_relationship("COBRA Participant") == "EE"
    assert std_relationship("Continuation Coverage") == "EE"
    assert std_relationship("COBRA Spouse") == "SP"
    assert std_relationship("COBRA Child") == "CH"
    assert std_relationship("COBRA Dependent") == "CH"


def test_structural_template_rows_cobra_and_invalid_zip():
    rows = [
        ["", "", "First Name", "Last Name", "Sex", "Date of Birth", "Zip Code", "Enrollee Type", "Current Plan Name", "Current Enrollment Tier"],
        ["", "", "Please provide each person's legal name", "", "Please select from the dropdown", "mm/dd/yyyy", "Permanent Address", "Please select from the dropdown", "Please provide the complete plan name", "Please select from the dropdown"],
        ["", "EXAMPLE", "Janet", "sample", "Female", datetime.datetime(1968, 11, 12), "02135", "Employee", "Plan 1", "Employee and Child"],
        ["", "", "Michael", "template", "Male", datetime.datetime(1994, 12, 21), "18508", "Employee", "", "Has Waived Benefits"],
        ["", "", "Pamela", "census", "Female", datetime.datetime(1978, 3, 4), "18519", "Employee", "Plan 2", "Employee and Family"],
        ["", 1, "Alice", "Carter", "Female", datetime.datetime(1990, 5, 10), "310122", "Employee", "Waived", "Has Waived Benefits"],
        ["", 2, "Bob", "Miller", "Male", datetime.datetime(1985, 7, 19), "27615", "Employee", "PPO 1650 HSA", "Employee Only"],
        ["", 3, "Carol", "Reed", "Female", datetime.datetime(1988, 3, 22), "07078", "Employee", "PPO 2000", "Employee Only"],
        ["", 4, "David", "Stone", "Male", None, None, "COBRA Enrollee", "Cobra", "Employee Only"],
    ]
    df = pd.DataFrame(rows)
    doc = RawDoc(path=Path("Blank_Template.xlsx"), ext=".xlsx", tables=[RawTable(name="Census", df=df)])

    census, _ = build_census([doc])
    names = [f"{e.first} {e.last}" for e in census.employees]
    assert names == ["Alice Carter", "Bob Miller", "Carol Reed", "David Stone"]
    assert len(census.unassigned_dependents) == 0

    alice = next(e for e in census.employees if e.first == "Alice")
    assert alice.zip == ""
    assert any("310122" in str(getattr(n, "text", n)) for n in alice.notes)

    david = next(e for e in census.employees if e.first == "David")
    assert david.role == "EE"
    assert david.cobra == "Y"
    assert david.coverage == "EE"
    assert david.plan == ""


def test_invoice_parser_month_labeled_current_premium_column():
    rows = [
        ["Major medical premium by employee", None, None, None, None, None],
        ["Employee", "Aetna plan", "Coverage tier", "November 2026", "Effective Dec. 1, 2026", "Monthly increase"],
        ["Alice Carter", "Waived", None, 0, 0, 0],
        ["Bob Miller", "NY PPO $1650 HSA -> $1700 HSA", "Employee only", 746.11, 803.17, 57.06],
        ["Carol Reed", "NY PPO $2000", "Family", 2399.09, 2582.28, 183.19],
        ["Total", None, None, 3145.20, 3385.45, 240.25],
    ]
    df = pd.DataFrame(rows)
    doc = RawDoc(
        path=Path("Benefits_Renewal_and_Employer_Cost.xlsx"),
        ext=".xlsx",
        tables=[RawTable(name="Major Medical by Employee", df=df)],
    )
    recs = _records_from_tables(doc)
    active = [r for r in recs if (r.premium or 0) > 0]
    assert len(active) == 2
    assert round(sum(r.premium for r in active), 2) == 3145.20


def test_sandbox_zip_invariant_and_renewal_emitter():
    with PythonSandboxSession([]) as sb:
        out = sb.run(
            """
emit_canonical_census({
    "employees": [
        {"first": "Alice", "last": "Carter", "coverage": "WO", "zip": "310122", "cobra": "N"},
        {"first": "David", "last": "Stone", "coverage": "EE", "zip": "", "cobra": "Y"},
    ],
    "excluded_count": 0,
    "notes": []
})
emit_canonical_renewal({
    "file_name": "Renewal.xlsx",
    "effective": "2026-12-01",
    "rates": [
        {"plan": "Aetna NY PPO $1650 -> $1700 HSA", "tier": "EE", "rate": 803.17},
        {"plan": "Aetna NY PPO $2000", "tier": "EF", "rate": 2582.28},
    ]
})
"""
        )
        assert "EMITTED canonical_census.json" in out
        assert "EMITTED canonical_renewal.json" in out
        c_data = sb.read_canonical_census()
        assert c_data["employees"][0]["zip"] == ""
        assert any("310122" in n for n in c_data["employees"][0].get("notes", []))

        r_data = sb.read_canonical_renewal()
        ren = canonical_json_to_renewal(r_data)
        assert ren is not None
        assert str(ren.effective) == "2026-12-01"
        assert len(ren.rates) == 2
        assert ren.rates[("Aetna NY PPO $2000", "FAM")] == 2582.28
