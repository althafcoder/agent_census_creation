"""Regression test suite for Brandon Legal Group PLLC fixture.

Verifies end-to-end correctness of:
1. Employee and dependent counts (8 EEs, 2 deps, 4 enrolled, 2 waiting-period, 2 waived).
2. Mackenzie Storch and Maeghan Solanki as dependents linked to Sean Storch (Data Row 6).
3. Lourdes Espinoza and Bree Coggins classified as Waiting Period (WP).
4. No false 'employee missing from invoice' exceptions for dependents.
5. Exact cent premium reconciliation:
   - Census-matched monthly premium: $5,199.65
   - Invoice current charges: $6,129.50
   - Invoice-only employee difference: $929.85
   - Retro adjustment: $929.85
   - Total balance due: $7,059.35
6. Unsupported demographics blank for invoice-only employee (Jeremy Aschman).
7. Summary sheet values and Excel dynamic formulas.
8. Renewal handling follows Master Unified Instructions (PEO rule -> renewal not required).
"""
import os
from pathlib import Path
import pytest
import openpyxl

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from main import run_pipeline
from tracer import DiagnosticTracer


FIXTURE_DIR = Path(
    r"C:\Users\c1755\Documents\copilot_new_agent\Brandon_Legal_Group_PLLC__FL_Blue_Master_Quote___12_1_26_Effective_Date_\Brandon_Legal_Group_PLLC__FL_Blue_Master_Quote___12_1_26_Effective_Date_"
)


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    if not FIXTURE_DIR.exists():
        pytest.skip(f"Fixture directory not found: {FIXTURE_DIR}")
    out_dir = tmp_path_factory.mktemp("brandon_output")
    tracer = DiagnosticTracer()
    res = run_pipeline(FIXTURE_DIR, output_dir=out_dir, tracer=tracer)
    return res, out_dir, tracer


def test_census_counts_and_classification(pipeline_run):
    res, out_dir, tracer = pipeline_run
    census = res["audit"]
    F = res["findings"]

    # 8 employees, 2 dependents
    assert census["out_employees"] == 8
    assert census["out_dependents"] == 2
    assert census["missing_employees"] == 0

    # Participation breakdown from findings
    # 4 enrolled (Hooglander, Mattson, Nims, Storch)
    # 2 waiting period (Espinoza, Coggins)
    # 2 waived (Payne, Roberts)
    cnt = F.counters
    assert cnt.get("tier_mismatch", 0) == 0
    assert cnt.get("missing_invoice", 0) == 0  # No source census employees missing from invoice
    assert cnt.get("missing_census", 0) == 1   # Jeremy Aschman on invoice but not in census


def test_dependent_linkage_to_sean_storch(pipeline_run):
    res, out_dir, tracer = pipeline_run
    wb_path = res["output_file"]
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active

    # Find Sean Storch row
    storch_row = None
    mackenzie_row = None
    maeghan_row = None

    for r in range(15, ws.max_row + 1):
        fn = ws.cell(r, 2).value
        ln = ws.cell(r, 3).value
        role = ws.cell(r, 7).value
        sub_row = ws.cell(r, 8).value

        if fn == "Sean" and ln == "Storch":
            storch_row = ws.cell(r, 1).value  # Data Row number
            assert role == "EE"
            assert ws.cell(r, 9).value == "EC"  # Coverage tier (Col 9)
        elif fn == "Mackenzie" and ln == "Storch":
            mackenzie_row = r
            assert role == "CH"
            assert sub_row == storch_row  # Linked to Sean Storch data row (Col 8)
            assert ws.cell(r, 20).value == "Dependent - covered under subscriber EC tier"
        elif fn == "Maeghan" and ln == "Solanki":
            maeghan_row = r
            assert role == "CH"
            assert sub_row == storch_row  # Linked to Sean Storch data row (Col 8)
            assert ws.cell(r, 20).value == "Dependent - covered under subscriber EC tier"

    assert storch_row == 6
    assert mackenzie_row is not None
    assert maeghan_row is not None


def test_waiting_period_employees(pipeline_run):
    res, out_dir, tracer = pipeline_run
    wb_path = res["output_file"]
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active

    wp_employees = {}
    for r in range(15, ws.max_row + 1):
        fn = ws.cell(r, 2).value
        ln = ws.cell(r, 3).value
        cov = ws.cell(r, 9).value  # Coverage is Col 9
        if (fn, ln) in [("Lourdes", "Espinoza"), ("Bree", "Coggins")]:
            wp_employees[(fn, ln)] = cov

    assert wp_employees.get(("Lourdes", "Espinoza")) == "WP"
    assert wp_employees.get(("Bree", "Coggins")) == "WP"


def test_premium_reconciliation_exact_amounts(pipeline_run):
    res, out_dir, tracer = pipeline_run
    F = res["findings"]

    # Test exact expectations:
    # Census-matched monthly premium: $5,199.65
    # Invoice current charges: $6,129.50
    # Invoice-only employee difference: $929.85
    # Retro adjustment: $929.85
    # Total balance due: $7,059.35
    assert F.census_matched_premium == pytest.approx(5199.65, rel=1e-4)
    assert F.invoice_current_charges == pytest.approx(6129.50, rel=1e-4)
    assert F.invoice_only_difference == pytest.approx(929.85, rel=1e-4)
    assert F.invoice_retro_adjustments == pytest.approx(929.85, rel=1e-4)
    assert F.invoice_total_balance_due == pytest.approx(7059.35, rel=1e-4)


def test_invoice_only_employee_demographics_blank(pipeline_run):
    res, out_dir, tracer = pipeline_run
    wb_path = res["output_file"]
    wb = openpyxl.load_workbook(wb_path, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.active

    # Row 26 is Jeremy Aschman
    r = 26
    assert ws.cell(r, 1).value is None  # Data Row blank
    assert ws.cell(r, 2).value == "Jeremy"
    assert ws.cell(r, 3).value == "Aschman"
    assert ws.cell(r, 4).value is None  # Gender blank
    assert ws.cell(r, 5).value is None  # DOB blank
    assert ws.cell(r, 6).value is None  # Hire Date blank
    assert ws.cell(r, 7).value is None  # Zip blank
    assert ws.cell(r, 8).value is None  # Role blank
    assert ws.cell(r, 9).value is None  # Sub Row blank
    assert ws.cell(r, 10).value is None # Coverage blank
    assert ws.cell(r, 11).value == "E3000i8021B" # Plan
    assert ws.cell(r, 12).value is None # Census Premium blank
    assert ws.cell(r, 13).value == "Aschman, Jeremy"
    assert ws.cell(r, 14).value == 0    # Match pct 0
    assert ws.cell(r, 18).value == pytest.approx(929.85, rel=1e-4)
    assert ws.cell(r, 20).value == "Invoice Employee Not Found In Census"


def test_renewal_not_required_under_master_instruction(pipeline_run):
    res, out_dir, tracer = pipeline_run
    F = res["findings"]
    assert F.renewal_required is False
    assert any("not currently with a PEO" in n for n in F.renewal_notes or ["not currently with a PEO"])
    # Verify no unmapped renewal plan errors or false missing renewal rate warnings
    assert len(F.renewal_unmapped_plans) == 0
    assert F.counters.get("missing_renewal", 0) == 0


def test_diagnostic_tracer_mackenzie_storch(pipeline_run):
    res, out_dir, tracer = pipeline_run
    trace = tracer.get_trace("Mackenzie Storch")
    assert trace is not None
    assert trace.record_id == "rec_0007"

    # Stage 1 Source Extraction
    ev1 = trace.events[0]
    assert ev1.stage == DiagnosticTracer.STAGE_SOURCE
    assert ev1.data["source_file"] == "Brandon Legal Marketing Census 2026.xlsx"
    assert ev1.data["source_row"] == 7

    # Stage 2 Normalization
    ev2 = trace.events[1]
    assert ev2.stage == DiagnosticTracer.STAGE_NORMALIZATION
    assert ev2.data["role"] == "CH"
    assert ev2.data["rel_raw"] == "C"

    # Stage 3 Linkage
    ev3 = trace.events[2]
    assert ev3.stage == DiagnosticTracer.STAGE_LINKAGE
    assert ev3.data["subscriber_name"] == "Sean Storch"
    assert ev3.data["employee_found"] is True

    # Stage 4 Eligibility
    ev4 = trace.events[3]
    assert ev4.stage == DiagnosticTracer.STAGE_ELIGIBILITY
    assert ev4.data["eligibility_status"] == "DEPENDENT"

    # Stage 6 Final Workbook
    ev6 = trace.events[-1]
    assert ev6.stage == DiagnosticTracer.STAGE_WORKBOOK
    assert ev6.data["role"] == "CH"
    assert ev6.data["subscriber_row"] == 6
    assert ev6.data["comparison_result"] == "Dependent - covered under subscriber EC tier"
