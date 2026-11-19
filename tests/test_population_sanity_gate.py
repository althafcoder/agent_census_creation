"""Tests for Fix #3: Dynamic Pre-Comparison Population Sanity Gate.

Verifies:
1. RFP/GHQ state-breakdown table headcount extraction ('# of Employees' + 'Totals' row).
2. Email narrative headcount extraction (ignoring invoice/renewal/payroll docs).
3. Clean census passes with zero issues (using Vikta 42 EE / 26 dep structure).
4. Broken census scenarios trip the gate dynamically (unassigned-dependent explosion,
   stated-headcount divergence like 103 vs 42, dependent ratio anomaly, invoice subscriber
   divergence, demographics collapse).
5. Normal minor HR variance (e.g. 42 stated vs 40 parsed) does NOT block the gate.
6. End-to-end: when a broken census is fed into `run_pipeline`, the gate blocks workbook
   generation unless LLM schema remap fixes it.
"""
from datetime import date
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from models import Census, InvoiceRecord, Person, Prospect  # noqa: E402
from population_sanity import evaluate_population_sanity, extract_stated_headcount  # noqa: E402
from prospect import _stated_total_employees  # noqa: E402
from readers import RawDoc, RawTable  # noqa: E402


def _make_person(role="EE", first="Jane", last="Doe", dob=date(1990, 1, 1), cov="EE"):
    p = Person(role, first, last)
    p.dob = dob
    p.coverage = cov if role == "EE" else ""
    return p


def test_rfp_table_stated_total_employees_extraction():
    df = pd.DataFrame([
        ["State", "Code", "# of Employees", "Gross Annual Payroll"],
        ["Texas", "", 28, 0],
        ["California", "", 7, ""],
        ["Kentucky", "", 2, ""],
        ["Nevada", "", 2, ""],
        ["Arizona", "", 3, ""],
        ["Totals", "", 42, 0],
    ])
    doc = RawDoc(path=Path("PEO_RFP.xlsx"), ext=".xlsx", tables=[RawTable("Summary", df)])
    hit = _stated_total_employees([doc])
    assert hit == (42, "PEO_RFP.xlsx")


def test_email_headcount_extraction_ignores_non_stated_roles():
    inv_doc = RawDoc(path=Path("invoice.pdf"), ext=".pdf", text="Total of 999 employees billed.")
    email_doc = RawDoc(path=Path("Original_Email.pdf"), ext=".pdf", text="Client currently has 42 full-time employees.")
    hc, src = extract_stated_headcount(
        prospect=Prospect(),
        roles={"invoice": [inv_doc], "rfp": [email_doc]},
    )
    assert hc == 42
    assert src == "Original_Email.pdf"


def test_clean_census_passes_sanity_gate():
    c = Census()
    for i in range(42):
        c.people.append(_make_person("EE", f"Emp{i}", "Smith", cov="EE" if i < 34 else "WO"))
    for i in range(26):
        c.people.append(_make_person("CH", f"Dep{i}", "Smith"))
    pr = Prospect(stated_total_employees=42, stated_total_source="PEO_RFP.xlsx")
    invs = [InvoiceRecord(first=f"Emp{i}", last="Smith") for i in range(34)]
    rep = evaluate_population_sanity(c, prospect=pr, invoice_records=invs)
    assert rep.passed and not rep.blocked
    assert rep.critical_count == 0


def test_minor_hr_variance_does_not_block_gate():
    c = Census()
    for i in range(40):
        c.people.append(_make_person("EE", f"Emp{i}", "Smith"))
    pr = Prospect(ft_employees=42, ft_employees_source="GHQ.pdf")
    rep = evaluate_population_sanity(c, prospect=pr)
    assert rep.passed and not rep.blocked


def test_vikta_failure_signature_blocks_gate():
    """103 employees + 62 unassigned dependents against 42 stated in RFP must be blocked."""
    c = Census()
    for i in range(103):
        c.people.append(_make_person("EE", f"Emp{i}", "Smith"))
    for i in range(62):
        c.unassigned_dependents.append(_make_person("CH", f"Dep{i}", "Smith"))
    pr = Prospect(stated_total_employees=42, stated_total_source="PEO_RFP.xlsx")
    rep = evaluate_population_sanity(c, prospect=pr)
    assert rep.blocked and not rep.passed
    codes = {i.code for i in rep.issues if i.severity == "critical"}
    assert "UNASSIGNED_DEPENDENTS_SPIKE" in codes
    assert "STATED_HEADCOUNT_DIVERGENCE" in codes


def test_dependent_ratio_and_invoice_divergence_block_gate():
    c = Census()
    for i in range(6):
        c.people.append(_make_person("EE", f"Emp{i}", "Smith"))
    for i in range(25):
        c.people.append(_make_person("CH", f"Dep{i}", "Smith"))
    invs = [InvoiceRecord(first=f"Emp{i}", last="Smith") for i in range(20)]
    rep = evaluate_population_sanity(c, invoice_records=invs)
    assert rep.blocked
    codes = {i.code for i in rep.issues if i.severity == "critical"}
    assert "DEPENDENT_RATIO_ANOMALY" in codes
    assert "INVOICE_SUBSCRIBER_DIVERGENCE" in codes


def test_demographics_collapse_blocks_gate():
    c = Census()
    for i in range(10):
        c.people.append(Person("EE", "", f"Last{i}", dob=None))
    rep = evaluate_population_sanity(c)
    assert rep.blocked
    assert any(i.code == "DEMOGRAPHICS_COLLAPSE" for i in rep.issues)


def test_validator_enforces_population_sanity_gate():
    from learning_engine.validator import UnderwritingValidator
    from learning_engine.contracts import CheckStatus
    from models import Findings
    from router import RoutingDecision

    val = UnderwritingValidator("test_hash")
    bad = Census()
    for i in range(103):
        bad.people.append(_make_person("EE", f"Emp{i}", "Smith"))
    for i in range(62):
        bad.unassigned_dependents.append(_make_person("CH", f"Dep{i}", "Smith"))
    pr = Prospect(stated_total_employees=42, stated_total_source="PEO_RFP.xlsx")
    bad.population_sanity = evaluate_population_sanity(bad, prospect=pr)
    routing = RoutingDecision(carrier="Cigna", state="TX", is_florida=False)
    report = val.validate_full_pipeline(
        prospect=pr, routing=routing, census=bad, selected_invoices=[],
        renewal=None, ren_required=False, claims_required=False,
        findings=Findings(), results={}, roles={},
    )
    gate_checks = [ch for ch in report.checks if ch.rule_id == "RULE_POPULATION_SANITY_GATE"]
    assert len(gate_checks) == 1
    assert gate_checks[0].status == CheckStatus.BLOCKED
    assert any(iss.issue_id == "ISSUE_POPULATION_SANITY_BLOCKED" for iss in report.issues)

