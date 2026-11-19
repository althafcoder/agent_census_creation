"""Regression test for linking different-surname dependents on a separate Dependent tab via ZIP + tier compatibility."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from census_builder import build_census, refine_non_enrolled_statuses
from readers import RawDoc, RawTable


def test_different_surname_dependents_link_to_tier_compatible_same_zip_subscriber():
    emp_df = pd.DataFrame([
        ["First Name", "Last Name", "Gender", "Date of Birth", "Home Zipcode", "Coverage Tier", "Medical Plan"],
        ["Tina", "Lin", "F", "1985-04-01", "91030", "EE", "BS-CA PPO 1000"],
        ["Scarlet", "Sy", "F", "1971-03-11", "91030", "EC", "BS-CA PPO 1000"],
        ["Apriyanti", "Cang", "F", "1978-04-13", "91755", "EF", "BS-CA PPO 1000"],
        ["Scott", "Thomas", "M", "1976-05-24", "90004", "EF", "BS-CA PPO 1000"],
    ])
    dep_df = pd.DataFrame([
        ["First Name", "Last Name", "Gender", "Date of Birth", "Home Zipcode", "Relationship to Employee"],
        ["Kai", "Olson", "M", "2006-07-14", "91030", "CH"],
        ["Marcus", "Olson", "M", "2007-11-14", "91030", "CH"],
        ["Soebekti", "Salim", "M", "1973-08-26", "91755", "SP"],
        ["Elizabeth", "Raposo", "F", "1976-09-11", "90004", "SP"],
    ])
    doc = RawDoc(
        path=Path("Employee.Dependent Census.xlsx"),
        ext=".xlsx",
        tables=[
            RawTable(name="Employee Census", df=emp_df),
            RawTable(name="Dependant Census", df=dep_df),
        ],
        text="",
    )
    census = build_census([doc], effective_date=date(2027, 1, 1))
    if isinstance(census, tuple):
        census = census[0]
    refine_non_enrolled_statuses(census, effective_date=date(2027, 1, 1))

    dep_map = {d.display: d.employee.display for d in census.dependents if d.employee}
    assert dep_map == {
        "Kai Olson": "Scarlet Sy",
        "Marcus Olson": "Scarlet Sy",
        "Soebekti Salim": "Apriyanti Cang",
        "Elizabeth Raposo": "Scott Thomas",
    }
    assert len(census.unassigned_dependents) == 0


def test_apply_dependent_links_rescues_excluded_and_relinks_misassigned_dependent():
    from census_builder import apply_dependent_links
    from models import Census, Person

    emp_wrong = Person(role="EE", first="Mera", last="Ramos", coverage="EC", plan="AETNA", record_id="rec_1")
    emp_right = Person(role="EE", first="Scott", last="Thomas", coverage="EF", plan="BS-CA PPO 1000", zip="90004", record_id="rec_2")
    dep_rescued = Person(
        role="SP", first="Elizabeth", last="Raposo", zip="90004",
        disposition="EXCLUDE_NON_MEDICAL", eligibility_status="EXCLUDED_NON_MEDICAL",
        employee=emp_wrong, subscriber_id="rec_1",
    )
    census = Census(
        people=[emp_wrong, emp_right],
        excluded_dependents=[dep_rescued],
    )

    n = apply_dependent_links(census, [
        {"dependent_name": "Elizabeth Raposo", "subscriber_name": "Scott Thomas", "reason": "Exact ZIP 90004 + EF tier"},
    ])
    assert n == 1
    assert dep_rescued in emp_right.dependents
    assert dep_rescued.employee is emp_right
    assert dep_rescued.disposition == "INCLUDE_ACTIVE_MEDICAL"
    assert dep_rescued not in census.excluded_dependents
    assert dep_rescued in census.people
    assert dep_rescued.sub_row == emp_right.row_no


def test_workbook_auditor_validates_repair_actions():
    from workbook_auditor import _validate_audit_dict

    validated = _validate_audit_dict({
        "overall_status": "warnings",
        "confidence_summary": "Found 1 leaked PT employee and 1 dropped spouse.",
        "findings": [],
        "safe_summary_patches": [],
        "repair_actions": {
            "exclude_employees": [{"person_name": "Lisa Bejarano", "reason": "Part-Time Employee"}],
            "status_overrides": [{"person_name": "Jane Doe", "coverage": "WO", "cobra": "N", "reason": "Declined"}],
            "dependent_links": [{"dependent_name": "Elizabeth Raposo", "subscriber_name": "Scott Thomas", "reason": "ZIP 90004"}],
        },
    })
    rep = validated["repair_actions"]
    assert len(rep["exclude_employees"]) == 1
    assert rep["status_overrides"][0]["coverage"] == "WO"
    assert rep["dependent_links"][0]["subscriber_name"] == "Scott Thomas"


def test_non_medical_plans_dbl_pfl_dppo_mco_tax_and_same_table_continuation_deps():
    from census_builder import _is_non_medical

    assert _is_non_medical("MetLife NY PFL -ME") is True
    assert _is_non_medical("MetLife New York DBL -ME") is True
    assert _is_non_medical("METLIFE LOW2 DPPO R3") is True
    assert _is_non_medical("METLIFE BASIC LIFE AD_D 10KS") is True
    assert _is_non_medical("AETNA NY MCO TAX") is True
    assert _is_non_medical("AETNA EPO 0-100 45-65 NE") is False
    assert _is_non_medical("AETNA MCPOS HSA 3500-90 NE") is False

    # Same-table horizontal continuation row under an EE-only employee should NOT become unassigned_dependents
    df = pd.DataFrame([
        ["EE-First Name", "EE-Last Name", "Employee Type", "EE-Birth Date", "EE-Gender", "EE-Zip Code",
         "DEP-Relation", "DEP-First Name", "DEP-Last Name", "DEP-Gender", "DEP-Birth Date", "Benefit Plan description", "Plan Type"],
        ["Leonard", "Ramos", "F", "1980-01-10", "M", "07109",
         "WIFE", "Sandra", "Ramos", "F", "1981-02-15", "AETNA EPO 0-100 45-65 NE", "EE"],
        [None, None, None, None, None, None,
         "SON", "Leo", "Ramos", "M", "2010-05-05", None, None],
        ["Gustavo", "Rodriguez", "F", "1967-10-24", "M", "07109",
         "WIFE", "Sofia", "Rodriguez", "F", "1972-04-22", "MetLife NY PFL -ME", ""],
        ["Gustavo", "Rodriguez", "F", "1967-10-24", "M", "07109",
         "WIFE", "Sofia", "Rodriguez", "F", "1972-04-22", "AETNA EPO 0-100 45-65 NE", "FAM"],
        [None, None, None, None, None, None,
         "SON", "Daniel", "Rodriguez", "M", "2002-01-03", None, None],
    ])
    doc = RawDoc(
        path=Path("Employee Benefit Census Report.xlsx"),
        ext=".xlsx",
        tables=[RawTable(name="Census", df=df)],
        text="",
    )
    census = build_census([doc], effective_date=date(2027, 1, 1))
    if isinstance(census, tuple):
        census = census[0]
    refine_non_enrolled_statuses(census, effective_date=date(2027, 1, 1))

    # Zero ghost unassigned dependents!
    assert len(census.unassigned_dependents) == 0
    # Gustavo (FAM) keeps Sofia + Daniel; Leonard (EE) has Sandra + Leo cleanly moved to excluded_dependents
    active_dep_names = {d.display for d in census.dependents}
    assert active_dep_names == {"Sofia Rodriguez", "Daniel Rodriguez"}


