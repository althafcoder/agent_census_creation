"""Regression test for waived employee coverage recognition, column header aliases, and invoice comparison.

Verifies:
1. Column header aliases: 'Medical Coverage Level', 'Medical Plan Selected', 'Plan Rate', '*Employee Dependent'
2. Standardized single-letter codes: 'W' -> 'WO', 'F' -> 'FAM'
3. Waived employee (e.g. Scott Grosscup) with 'Waive' / 'W':
   - Correctly receives coverage = 'WO' (not 'EE')
   - Status / plan 'Waive' moved to waive_reason
   - Not flagged with 'Census Employee Not Found On Invoice'
   - Not included in unbilled active / missing invoice deliverable email
4. Waiting period employee (e.g. Kylie McKinley) with 'Waiting Period' / 'WP':
   - Correctly receives coverage = 'WP' (not 'EE')
   - Awaiting eligibility handled per underwriting instructions
"""
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd
import pytest

root = Path(__file__).resolve().parent.parent
backend_dir = root / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from classifier import detect_header
from utils import std_coverage
from readers import RawDoc, RawTable
from census_builder import build_census
from comparator import run_comparison
from models import Findings, InvoiceRecord, Prospect
from router import generate_missing_information_email, RoutingDecision


def test_coverage_aliases_and_single_letter_codes():
    """Verify single-letter codes 'W' -> 'WO' and 'F' -> 'FAM'."""
    assert std_coverage("W") == "WO"
    assert std_coverage("w") == "WO"
    assert std_coverage("Waive") == "WO"
    assert std_coverage("Waived") == "WO"
    assert std_coverage("F") == "FAM"
    assert std_coverage("f") == "FAM"
    assert std_coverage("Family") == "FAM"


def test_census_header_alias_detection():
    """Verify 'Medical Coverage Level', 'Medical Plan Selected', 'Plan Rate', and '*Employee Dependent' detection."""
    df = pd.DataFrame([
        ["*Last Name", "*First Name", "*Employee Dependent", "*Gender", "*Job Title", "*DOB", "*Home Zip",
         "Medical Plan Selected", "Plan Rate", "Medical Coverage Level", "*Annual Salary"],
        ["Grosscup", "Scott", "Employee", "M", "Attorney", "11/29/1972", "81601",
         "Waive", "", "W", "100000"],
        ["McKinley", "Kylie", "Employee", "F", "Paralegal", "05/03/1996", "81601",
         "Waiting Period", "", "WP", "79000"],
        ["Boe", "Cassi", "Employee", "F", "Paralegal", "06/21/1990", "81652",
         "UHC HSA 4250/80% SILVER", "1097.11", "EE", "79000"],
    ])

    h = detect_header(df)
    assert h is not None
    assert "last_name" in h.columns
    assert "first_name" in h.columns
    assert "relationship" in h.columns
    assert "plan" in h.columns
    assert "premium" in h.columns
    assert "coverage" in h.columns


def test_waived_employee_comparison_and_email_not_missing():
    """Verify waived employee Scott Grosscup receives WO, no false 'Not on invoice' notes, and excluded from missing invoice email."""
    df = pd.DataFrame([
        ["*Last Name", "*First Name", "*Employee Dependent", "*Gender", "*Job Title", "*DOB", "*Home Zip",
         "Medical Plan Selected", "Plan Rate", "Medical Coverage Level", "*Annual Salary"],
        ["Grosscup", "Scott", "Employee", "M", "Attorney", "11/29/1972", "81601",
         "Waive", "", "W", "100000"],
        ["McKinley", "Kylie", "Employee", "F", "Paralegal", "05/03/1996", "81601",
         "Waiting Period", "", "WP", "79000"],
        ["Boe", "Cassi", "Employee", "F", "Paralegal", "06/21/1990", "81652",
         "UHC HSA 4250/80% SILVER", "1097.11", "EE", "79000"],
    ])

    t = RawTable(name="Census", df=df)
    doc = RawDoc(path=Path("Census_Balcomb & Green.xlsx"), ext=".xlsx", tables=[t])

    census, meta = build_census([doc])
    assert meta["has_coverage_column"] is True

    scott = next(p for p in census.employees if p.last == "Grosscup")
    assert scott.coverage == "WO"
    assert scott.waive_reason == "Waive"
    assert scott.plan in ("", "NA")

    kylie = next(p for p in census.employees if p.last == "McKinley")
    assert kylie.coverage == "WP"
    assert kylie.plan in ("", "NA")

    cassi = next(p for p in census.employees if p.last == "Boe")
    assert cassi.coverage == "EE"
    assert cassi.plan == "UHC HSA 4250/80% SILVER"

    # Run comparison with only Cassi Boe on invoice
    inv_records = [
        InvoiceRecord(first="Cassi", last="Boe", plan="UHC HSA 4250/80% SILVER", tier="EE", premium=1097.11),
    ]

    F = Findings()
    results = run_comparison(census, inv_records, renewal=None, meta=meta, F=F, have_invoice=True, renewal_required=False)

    rr_scott = results[id(scott)]
    assert rr_scott.status == "Not on invoice"
    assert rr_scott.result == "Expected - Not Enrolled"
    # Ensure no false "Census Employee Not Found On Invoice" note
    assert not any("Census Employee Not Found On Invoice" in n[0] for n in rr_scott.notes)

    # Email deliverable check
    prospect = Prospect(company="Balcomb & Green PC")
    routing = RoutingDecision(carrier="Cigna")
    email = generate_missing_information_email(
        prospect=prospect, routing=routing, findings=F, census=census,
        have_census=True, have_invoice=True, have_renewal=False, renewal_required=False, claims_required=False,
        results=results
    )

    # Scott Grosscup is waived so he must NOT be flagged as missing on invoice
    assert "Grosscup" not in email
    # Kylie McKinley is in waiting period, so email asks about eligibility instead of claiming missing invoice
    assert "The enrollment report shows him on , Employee Only" not in email


def test_geiger_dependents_and_workbook_dependent_blanking():
    """Verify Christopher Geiger's dependents are extracted and dependent rows leave coverage/plan/discrepancies blank."""
    import openpyxl
    import census_config as C
    from workbook_writer import write_marsh_sheet, write_census_sheet

    df = pd.DataFrame([
        ["*Last Name", "*First Name", "*Employee Dependent", "*Gender", "*Job Title", "*DOB", "*Home Zip",
         "Medical Plan Selected", "Plan Rate", "Medical Coverage Level", "*Annual Salary"],
        ["Geiger", "Christopher", "Employee", "M", "Attorney", "06/09/1972", "81601",
         "UHC HSA 3500/80% SILVER", "3157.74", "EC", "100000"],
        ["Geiger", "Cole", "Dependent", "M", "", "2005-07-28", "81601",
         "Dependent", "", "", ""],
        ["Geiger", "Amelia", "Dependent", "F", "", "2008-03-19", "81601",
         "Dependent", "", "", ""],
        ["Grosscup", "Scott", "Employee", "M", "Attorney", "11/29/1972", "81601",
         "Waive", "", "W", "100000"],
    ])

    t = RawTable(name="Census", df=df)
    doc = RawDoc(path=Path("Census_Balcomb.xlsx"), ext=".xlsx", tables=[t])
    census, meta = build_census([doc])

    assert len(census.people) == 4
    assert len(census.employees) == 2
    assert len(census.dependents) == 2

    geiger = next(p for p in census.employees if p.last == "Geiger")
    assert geiger.coverage == "EC"
    assert len(geiger.dependents) == 2

    cole = next(p for p in census.dependents if p.first == "Cole")
    amelia = next(p for p in census.dependents if p.first == "Amelia")
    assert cole.employee is geiger
    assert amelia.employee is geiger

    F = Findings()
    results = run_comparison(census, [], renewal=None, meta=meta, F=F, have_invoice=False, renewal_required=False)

    assert results[id(cole)].result == "Dependent"
    assert results[id(amelia)].result == "Dependent"

    # 1. Test Marsh Sheet Formatting
    wb_marsh = openpyxl.load_workbook(C.MARSH_TEMPLATE_PATH)
    ws_marsh = wb_marsh["Group Info + Census"]
    write_marsh_sheet(ws_marsh, census, Prospect(company="Balcomb & Green PC"), results)

    # Row 14: Christopher Geiger (EE)
    assert ws_marsh.cell(14, 2).value == "Christopher"
    assert ws_marsh.cell(14, 7).value == "EE"
    assert ws_marsh.cell(14, 8).value == "EC"
    assert ws_marsh.cell(14, 9).value == "N"
    assert ws_marsh.cell(14, 10).value == "UHC HSA 3500/80% SILVER"

    # Row 15: Cole Geiger (CH - dependent row)
    assert ws_marsh.cell(15, 2).value == "Cole"
    assert ws_marsh.cell(15, 7).value == "CH"
    assert ws_marsh.cell(15, 8).value is None     # Medical Coverage MUST be None
    assert ws_marsh.cell(15, 9).value is None     # Cobra MUST be None
    assert ws_marsh.cell(15, 10).value is None    # Medical Plan Name MUST be None
    # All comparison and discrepancy/notes columns MUST be None except Col 20 (Comparison Result) = 'Dependent'
    for col_idx in list(range(11, 20)) + [21]:
        assert ws_marsh.cell(15, col_idx).value is None
    assert ws_marsh.cell(15, 20).value == "Dependent"

    # Row 16: Amelia Geiger (CH - dependent row)
    assert ws_marsh.cell(16, 2).value == "Amelia"
    assert ws_marsh.cell(16, 7).value == "CH"
    assert ws_marsh.cell(16, 8).value is None
    assert ws_marsh.cell(16, 9).value is None
    assert ws_marsh.cell(16, 10).value is None
    for col_idx in list(range(11, 20)) + [21]:
        assert ws_marsh.cell(16, col_idx).value is None
    assert ws_marsh.cell(16, 20).value == "Dependent"

    # Row 17: Scott Grosscup (Waived EE)
    assert ws_marsh.cell(17, 2).value == "Scott"
    assert ws_marsh.cell(17, 7).value == "EE"
    assert ws_marsh.cell(17, 8).value == "WO"
    assert ws_marsh.cell(17, 10).value is None

    # 2. Test RAPT Sheet Formatting
    wb_rapt = openpyxl.load_workbook(C.TEMPLATE_PATH)
    ws_rapt = wb_rapt["Sheet1"]
    write_census_sheet(ws_rapt, census, Prospect(company="Balcomb & Green PC"), results)

    # Row 15: Christopher Geiger (EE)
    tc = C.TEMPLATE_COLS
    assert ws_rapt.cell(15, tc["first"]).value == "Christopher"
    assert ws_rapt.cell(15, tc["relationship"]).value == "EE"
    assert ws_rapt.cell(15, tc["coverage"]).value == "EC"
    assert ws_rapt.cell(15, tc["cobra"]).value == "N"
    assert ws_rapt.cell(15, tc["plan"]).value == "UHC HSA 3500/80% SILVER"
    assert ws_rapt.cell(15, tc["premium"]).value == 3157.74

    # Row 16: Cole Geiger (CH - dependent row)
    assert ws_rapt.cell(16, tc["first"]).value == "Cole"
    assert ws_rapt.cell(16, tc["relationship"]).value == "CH"
    assert ws_rapt.cell(16, tc["dep_of"]).value == 1    # Points to Geiger Row 1
    assert ws_rapt.cell(16, tc["coverage"]).value is None
    assert ws_rapt.cell(16, tc["cobra"]).value is None
    assert ws_rapt.cell(16, tc["plan"]).value is None
    assert ws_rapt.cell(16, tc["premium"]).value is None
    for col_idx in list(range(13, 20)) + [21]:
        assert ws_rapt.cell(16, col_idx).value is None
    assert ws_rapt.cell(16, 20).value == "Dependent"

    # Row 17: Amelia Geiger (CH - dependent row)
    assert ws_rapt.cell(17, tc["first"]).value == "Amelia"
    assert ws_rapt.cell(17, tc["relationship"]).value == "CH"
    assert ws_rapt.cell(17, tc["dep_of"]).value == 1    # Points to Geiger Row 1
    assert ws_rapt.cell(17, tc["coverage"]).value is None
    assert ws_rapt.cell(17, tc["cobra"]).value is None
    assert ws_rapt.cell(17, tc["plan"]).value is None
    assert ws_rapt.cell(17, tc["premium"]).value is None
    for col_idx in list(range(13, 20)) + [21]:
        assert ws_rapt.cell(17, col_idx).value is None
    assert ws_rapt.cell(17, 20).value == "Dependent"


def test_learning_engine_wo_and_wp_validation_and_threshold():
    """Verify that the Learning Engine dynamically validates both WO and WP status codes,
    maintains >= 90% accuracy threshold, and repairs any misclassified status codes."""
    from learning_engine.instruction_registry import resolve_instruction_snapshot
    from learning_engine.validator import UnderwritingValidator
    from learning_engine.repair import RepairEngine
    from learning_engine.contracts import CheckStatus, RepairAction
    from router import RoutingDecision
    from models import InvoiceRecord
    import census_config as C

    snapshot = resolve_instruction_snapshot()
    validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)

    # 1. Setup multi-status census with Enrolled, Waived (W/Waive), and Waiting Period (WP/Waiting Period)
    df = pd.DataFrame([
        ["*Last Name", "*First Name", "*Employee Dependent", "*Gender", "*Job Title", "*DOB", "*Home Zip",
         "Medical Plan Selected", "Plan Rate", "Medical Coverage Level", "*Annual Salary"],
        ["Boe", "Cassi", "Employee", "F", "Paralegal", "06/21/1990", "81652",
         "UHC HSA 4250/80% SILVER", "1097.11", "EE", "79000"],
        ["Grosscup", "Scott", "Employee", "M", "Attorney", "11/29/1972", "81601",
         "Waive", "", "W", "100000"],
        ["McKinley", "Kylie", "Employee", "F", "Paralegal", "05/03/1996", "81601",
         "Waiting Period", "", "WP", "79000"],
    ])

    t = RawTable(name="Census", df=df)
    doc = RawDoc(path=Path("Census_MultiStatus.xlsx"), ext=".xlsx", tables=[t])
    census, meta = build_census([doc])

    # Enrolled employee on invoice; Waived & WP absent from invoice
    invoices = [
        InvoiceRecord(first="Cassi", last="Boe", raw_name="BOE, CASSI", plan="UHC HSA 4250/80% SILVER", tier="EE", premium=1097.11, status="Active")
    ]
    prospect = Prospect(company="Balcomb & Green PC", state="CO")
    routing = RoutingDecision(carrier=C.CARRIER_CIGNA, state="CO", is_florida=False)
    findings = Findings()
    results = run_comparison(census, invoices, None, meta, findings, True, renewal_required=False)

    # Validate with Learning Engine UnderwritingValidator
    report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census, selected_invoices=invoices,
        renewal=None, ren_required=False, claims_required=False,
        findings=findings, results=results,
        roles={"census": [Path("Census_MultiStatus.xlsx")], "invoice": [Path("Invoice.pdf")]}
    )

    # Verify RULE_21 passes and accuracy threshold >= 0.90 is maintained
    r21 = next((c for c in report.checks if c.rule_id == "RULE_21_STATUS_HANDLING"), None)
    assert r21 is not None
    assert r21.status == CheckStatus.PASS
    assert getattr(report, "accuracy", 0.0) >= report.accuracy_threshold
    assert report.accuracy_threshold == 0.90

    # 2. Test repair loop when WP status is misclassified (e.g. flagged as Not On Invoice)
    kylie = next(p for p in census.employees if p.last == "McKinley")
    r_kylie = results[id(kylie)]
    r_kylie.result = "Not On Invoice"
    r_kylie.notes.append(("Census Employee Not Found On Invoice", True))

    # Validator catches the defect
    defect_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census, selected_invoices=invoices,
        renewal=None, ren_required=False, claims_required=False,
        findings=findings, results=results,
        roles={"census": [Path("Census_MultiStatus.xlsx")], "invoice": [Path("Invoice.pdf")]}
    )
    r21_defect = next((c for c in defect_report.checks if c.rule_id == "RULE_21_STATUS_HANDLING"), None)
    assert r21_defect is not None
    assert r21_defect.status == CheckStatus.FAIL

    # Execute dynamic repair
    repairer = RepairEngine()
    working_state = {
        "census": census,
        "results": results,
        "findings": findings,
        "invoices": invoices,
        "meta": meta,
        "prospect": prospect,
    }
    patches = repairer.propose_patches(defect_report, working_state)
    assert len(patches) > 0
    tx = repairer.execute_patch(patches[0], working_state)
    assert tx.executed and not tx.rolled_back

    # Re-validate repaired state
    repaired_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=working_state["census"], selected_invoices=invoices,
        renewal=None, ren_required=False, claims_required=False,
        findings=working_state["findings"], results=working_state["results"],
        roles={"census": [Path("Census_MultiStatus.xlsx")], "invoice": [Path("Invoice.pdf")]}
    )
    r21_repaired = next((c for c in repaired_report.checks if c.rule_id == "RULE_21_STATUS_HANDLING"), None)
    assert r21_repaired is not None
    assert r21_repaired.status == CheckStatus.PASS
    assert getattr(repaired_report, "accuracy", 0.0) >= 0.90





def test_millard_first_name_recovery_and_wp_learning_engine_repair():
    """Verify dynamic OCR name rejection per Sec 10.3 (leave blank, do not guess),
    probationary code 'p' -> WP mapping, RULE_10_REQUIRED_FIELDS and RULE_21_STATUS_HANDLING
    audit, and closed-loop Learning Engine repair for Waiting Period employee."""
    from utils import is_valid_person_name, recover_ocr_name, std_coverage
    from census_builder import _parse_llm_census_rows, _State
    from models import Census, Person, Findings, Prospect
    from router import RoutingDecision, generate_missing_information_email
    from learning_engine.validator import UnderwritingValidator
    from learning_engine.contracts import CheckStatus, IssueType
    from learning_engine.repair import RepairEngine
    import datetime as _dt

    # 1. Direct utility checks
    assert is_valid_person_name("P6T&L") is False
    assert is_valid_person_name("Millard") is True
    assert is_valid_person_name("Peter") is True
    assert recover_ocr_name("P6T&L") == ""  # Rejected per Section 10.3 (leave blank, do not guess)
    assert std_coverage("p") == "WP"
    assert std_coverage("probation") == "WP"
    assert std_coverage("probationary") == "WP"

    # 2. Test LLM census row parsing with OCR noise and probationary indicators
    raw_doc = RawDoc(path=Path("Census.pdf"), ext=".pdf", text='MILlaRD P6T&L 9 -25-78 m 9 7s"o1')
    st = _State(raw_doc)
    st.census = Census()

    mock_llm_rows = [
        {
            "first_name": "William",
            "last_name": "Rice",
            "gender": "M",
            "dob": "01/12/1970",
            "zip": "97503",
            "relationship": "EE",
            "coverage": "EE",
        },
        {
            "first_name": "P6T&L",  # Unparsed OCR artifact
            "last_name": "Millard",
            "gender": "M",
            "dob": "09/25/1978",
            "zip": "97501",
            "relationship": "EE",
            "coverage": "",
            "flags": [
                "Handwritten or poor-quality OCR row. Raw values: last name MILlaRD, first name P6T&L, DOB 9-25-78, gender m, ZIP 97s01.",
                "First name OCR token 'P6T&L' contains noise and was left blank.",
                "Enroll code 'p' probationary (i.e., not yet eligible)."
            ]
        }
    ]

    ok = _parse_llm_census_rows(raw_doc, mock_llm_rows, st)
    assert ok is True
    assert len(st.employees) == 2

    millard = st.employees[1]
    assert millard.last == "Millard"
    assert not millard.first  # First name left blank per Section 10.3!
    assert millard.coverage == "WP"   # Dynamically recognized from probationary code 'p'
    assert any("P6T&L" in str(n) for n in millard.notes)

    # 3. Test Learning Engine: detect invalid OCR name artifact (RULE_10) and misclassified status (RULE_21)
    unrepaired_census = Census()
    unrepaired_census.files_used.append("Census.pdf")
    p1 = Person("EE", "William", "Rice")
    p1.dob = _dt.date(1970, 1, 12)
    p1.coverage = "EE"
    p1.zip = "97503"
    p1.gender = "M"

    p2 = Person("EE", "P6T&L", "Millard")  # Invalid name artifact!
    p2.dob = _dt.date(1978, 9, 25)
    p2.coverage = "EE"                     # Misclassified as active EE instead of WP!
    p2.zip = "97501"
    p2.gender = "M"
    p2.note("Raw first name OCR token: 'P6T&L'")
    p2.note("Enroll code 'p' probationary (not yet eligible)")

    unrepaired_census.people = [p1, p2]

    prospect = Prospect(company="NFL Trucking", state="OR", effective_date=_dt.date(2026, 11, 1))
    routing = RoutingDecision(carrier="Cigna")
    findings = Findings()
    from comparator import run_comparison
    results = run_comparison(unrepaired_census, [], renewal=None, meta={}, F=findings, have_invoice=False, renewal_required=False)

    validator = UnderwritingValidator(instruction_hash="instr_test_v1")
    defect_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=unrepaired_census, selected_invoices=[],
        renewal=None, ren_required=False, claims_required=False,
        findings=findings, results=results,
        roles={"census": [Path("Census.pdf")]}
    )

    # Check RULE_10_REQUIRED_FIELDS failed due to invalid first name artifact
    r10 = next((c for c in defect_report.checks if c.rule_id == "RULE_10_REQUIRED_FIELDS"), None)
    assert r10 is not None
    assert r10.status == CheckStatus.FAIL
    assert any(i.issue_type == IssueType.MISSING_REQUIRED_DEMOGRAPHIC for i in defect_report.issues)

    # Check RULE_21_STATUS_HANDLING failed due to misclassified WP employee as EE
    r21 = next((c for c in defect_report.checks if c.rule_id == "RULE_21_STATUS_HANDLING"), None)
    assert r21 is not None
    assert r21.status == CheckStatus.FAIL

    # 4. Closed-loop repair by RepairEngine
    repairer = RepairEngine()
    working_state = {
        "census": unrepaired_census,
        "results": results,
        "findings": findings,
        "invoices": [],
        "meta": {},
        "prospect": prospect,
    }
    patches = repairer.propose_patches(defect_report, working_state)
    assert len(patches) > 0

    for patch in patches:
        tx = repairer.execute_patch(patch, working_state)
        assert tx.executed and not tx.rolled_back

    # Verify self-healing in working_state
    repaired_emp = unrepaired_census.employees[1]
    assert not repaired_emp.first  # First name sanitized to None per Section 10.3!
    assert repaired_emp.coverage == "WP"
    assert repaired_emp.eligibility_status == "WAITING_PERIOD"

    # Re-validate repaired pipeline state
    findings.exceptions.clear()
    results = run_comparison(unrepaired_census, [], renewal=None, meta={}, F=findings, have_invoice=False, renewal_required=False)
    working_state["results"] = results
    working_state["findings"] = findings

    repaired_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=unrepaired_census, selected_invoices=[],
        renewal=None, ren_required=False, claims_required=False,
        findings=findings, results=results,
        roles={"census": [Path("Census.pdf")]}
    )

    r10_repaired = next((c for c in repaired_report.checks if c.rule_id == "RULE_10_REQUIRED_FIELDS"), None)
    assert r10_repaired is not None
    assert r10_repaired.status == CheckStatus.PASS

    r21_repaired = next((c for c in repaired_report.checks if c.rule_id == "RULE_21_STATUS_HANDLING"), None)
    assert r21_repaired is not None
    assert r21_repaired.status == CheckStatus.PASS
    assert getattr(repaired_report, "accuracy", 0.0) >= 0.90

    # 5. Test Router Missing Information Email
    p_missing = Person("EE", None, "Millard")
    p_missing.coverage = "WP"
    c_missing = Census()
    c_missing.people = [p_missing]

    email_text = generate_missing_information_email(
        prospect=prospect, routing=routing, findings=findings, census=c_missing,
        have_census=True, have_invoice=False, have_renewal=False, renewal_required=False, claims_required=False
    )
    assert "Current medical invoice or Benefit Enrollment Register was not provided" in email_text
    assert "Millard: First name is missing or illegible in the uploaded census. Please provide the employee's full legal name." in email_text
    assert "Millard: No medical election is on file. Please confirm whether" in email_text
