"""Regression test suite validating Agent 1 (Copilot Studio) Ground Truth rules.

Derived directly from 'SLM Waste and Recycling Services Inc Descripencies.docx':
1. Assert 0 ghost WC employees (reject 'HARRIS, JA WC Code: 0953PA', etc.).
2. Assert employee count == 65 (not inflated to 71 by 6 false WC fragments).
3. Assert Maria Guzman == 'WP' (hire date within 30-90 days of effective date).
4. Assert Bryan Detweiler == 'NE' (part-time / hours < 30/week).
5. Assert James Bertolotti medical premium == $733.84 (reject $16.82 ancillary deduction).
6. Assert company metadata extraction (DBA, EIN, Contact Name, Contact Email, Contact Phone).
"""
import sys
from datetime import date
from pathlib import Path
import pytest

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from models import Census, Person, InvoiceRecord
from readers import RawDoc
from invoice_parser import _is_fragment_name, _records_from_peo_employee_details, _clean_emp_name
from census_builder import refine_non_enrolled_statuses, _State
from prospect import _apply_llm_prospect, build_prospect


def test_ghost_wc_employees_are_strictly_rejected():
    """Assert that Workers' Comp invoice text fragments are rejected and never become employees."""
    ghost_names = [
        "HARRIS, JA WC Code: 0953PA",
        "HIPP, RICH WC Code: 0953PA",
        "KEAN, SAR WC Code: 0953PA",
        "KEMERY, A WC Code: 0953PA",
        "KIER, BREE WC Code: 8871LA",
        "JA WC Code: 0953PA HARRIS",
        "8871LA",
    ]
    for ghost in ghost_names:
        assert _is_fragment_name(ghost) is True, f"Expected fragment filter to reject '{ghost}'"


def test_clean_emp_name_strips_wc_suffixes():
    """Assert that _clean_emp_name removes WC code suffixes from valid names."""
    cleaned = _clean_emp_name("HARRIS, JAMES WC Code: 0953PA")
    assert cleaned == "HARRIS, JAMES"


def test_james_bertolotti_medical_premium_guard():
    """Assert James Bertolotti medical premium == $733.84, rejecting $16.82 ancillary deduction."""
    sample_text = """
Employee Details
Provider Plan Description Type Tier Effective Cost

BERTOLOTTI, JAMES A VSP VISION Vision Employee EE 10/01/2026 $16.82
EE ID:100123 AETNA OPEN ACCESS Medical Employee EE 10/01/2026 $733.84
Total $750.66
"""
    doc = RawDoc(path=Path("SLM_Invoice.txt"), ext=".txt", text=sample_text)
    recs = _records_from_peo_employee_details(doc)

    assert len(recs) == 1, f"Expected 1 record, got {len(recs)}"
    bertolotti = recs[0]
    assert bertolotti.last == "BERTOLOTTI"
    assert bertolotti.first == "JAMES A"
    assert bertolotti.premium == 733.84, f"Expected $733.84, got ${bertolotti.premium}"


def test_slm_employee_count_not_inflated_by_wc_fragments():
    """Assert employee count remains 65 (not inflated to 71 by false WC lines)."""
    # 65 valid employee names + 6 WC code fragments
    valid_names = [f"Smith, Person{chr(65 + (i % 26))}{chr(65 + ((i*3) % 26))}" for i in range(1, 66)]
    ghost_lines = [
        "HARRIS, JA WC Code: 0953PA",
        "HIPP, RICH WC Code: 0953PA",
        "KEAN, SAR WC Code: 0953PA",
        "KEMERY, A WC Code: 0953PA",
        "KIER, BREE WC Code: 8871LA",
        "8871LA",
    ]

    all_lines = valid_names + ghost_lines
    accepted = [name for name in all_lines if not _is_fragment_name(name)]
    assert len(accepted) == 65, f"Expected exactly 65 employees, got {len(accepted)}"



def test_maria_guzman_classified_as_waiting_period_wp():
    """Assert Maria Guzman with recent hire date (within 30-90 days) is classified as WP, not WO."""
    census = Census()
    # Proposed effective date: 2027-01-01. Hired 2026-11-15 (47 days prior -> within 30-90 days)
    maria = Person(
        role="EE",
        first="Maria",
        last="Guzman",
        hire_date=date(2026, 11, 15),
        worker_category="Full Time",
        position_status="Active",
        source="census.xlsx"
    )
    census.people = [maria]

    refine_non_enrolled_statuses(
        census,
        reference_date=date(2026, 10, 1),
        effective_date=date(2027, 1, 1)
    )

    assert maria.coverage == "WP", f"Expected WP for Maria Guzman, got '{maria.coverage}'"


def test_bryan_detweiler_classified_as_not_eligible_ne():
    """Assert Bryan Detweiler with part-time status (<30 hours) is classified as NE, not WO."""
    census = Census()
    bryan = Person(
        role="EE",
        first="Bryan",
        last="Detweiler",
        hire_date=date(2024, 5, 10),
        worker_category="Part Time",
        job_title="Associate - 20 hrs/week",
        position_status="Active",
        source="census.xlsx"
    )
    census.people = [bryan]

    refine_non_enrolled_statuses(
        census,
        reference_date=date(2026, 10, 1),
        effective_date=date(2027, 1, 1)
    )

    assert bryan.coverage == "NE", f"Expected NE for Bryan Detweiler, got '{bryan.coverage}'"


def test_slm_company_metadata_mapping():
    """Assert company information fields (DBA, EIN, Contact Name, Contact Email, Contact Phone) are populated."""
    merged = {}
    llm_mock_res = {
        "company_name": "SLM Waste and Recycling Services Inc",
        "dba": "SLM-Facility Solutions Nationwide",
        "ein": "23-2950805",
        "contact_name": "Bonni Mauro",
        "contact_email": "Bonni.Mauro@slmfacilities.com",
        "contact_phone": "267-429-7411",
        "proposed_effective_date": "01/01/2027",
        "sic_naics_code": "4953"
    }

    _apply_llm_prospect(merged, llm_mock_res)

    assert merged["company"] == "SLM Waste and Recycling Services Inc"
    assert merged["dba"] == "SLM-Facility Solutions Nationwide"
    assert merged["ein"] == "23-2950805"
    assert merged["contact_name"] == "Bonni Mauro"
    assert merged["contact_email"] == "Bonni.Mauro@slmfacilities.com"
    assert merged["contact_phone"] == "267-429-7411"
    assert merged["effective_date"] == "01/01/2027"
    assert merged["sic"] == "4953"


def test_large_census_roster_zero_truncation():
    """Assert enterprise rosters (e.g. 200+ employees) are parsed with 100% fidelity without truncation."""
    import pandas as pd
    from readers import RawDoc, RawTable
    from census_builder import build_census

    # Create 200 mock employee rows
    data = [
        ["First Name", "Last Name", "DOB", "Gender", "Zip Code", "Relationship", "Coverage Level", "Plan"]
    ]
    for i in range(1, 201):
        data.append([
            f"Employee{i}",
            f"TestLastName{i}",
            "05/15/1985",
            "M" if i % 2 == 0 else "F",
            "10001",
            "Employee",
            "EE",
            "Standard Medical PPO"
        ])

    df = pd.DataFrame(data)
    doc = RawDoc(path=Path("large_census.xlsx"), ext=".xlsx", tables=[RawTable("Sheet1", df)], text="")
    census, _ = build_census([doc])

    assert len(census.people) == 200, f"Expected 200 employees, got {len(census.people)}"
    assert census.people[0].first == "Employee1"
    assert census.people[-1].first == "Employee200"


def test_json_truncation_recovery_walks_backwards():
    """Assert _parse_json_robust cleanly recovers complete items when Claude output is cut off mid-stream."""
    from llm_client import _parse_json_robust

    truncated_json_array = """[
      {"first_name": "Cody", "last_name": "Andrews", "coverage": "FAM"},
      {"first_name": "Michael", "last_name": "Barry", "coverage": "EE"},
      {"first_name": "James", "last_name": "Black", "bio": "contains {brace} inside", "plan": "POS 200", "broken_val": "unfini
    """

    recovered = _parse_json_robust(truncated_json_array)
    assert isinstance(recovered, list)
    assert len(recovered) == 2
    assert recovered[0]["first_name"] == "Cody"
    assert recovered[1]["first_name"] == "Michael"

