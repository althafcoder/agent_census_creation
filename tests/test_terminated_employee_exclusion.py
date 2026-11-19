"""Regression tests for excluding Terminated / Inactive non-COBRA employees from HRIS exports."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from census_builder import build_census, refine_non_enrolled_statuses
from models import Findings
from population_sanity import evaluate_population_sanity
from readers import RawDoc, RawTable


def test_terminated_non_cobra_excluded_and_cobra_preserved():
    headers = [
        "Relationship", "First Name", "Last Name", "Sex", "DOB",
        "Class", "Employment Status", "Zip",
        "Medical Plan", "Medical Plan Coverage", "COBRA",
    ]
    rows = [
        headers,
        ["Employee", "Alice", "Active", "F", "1985-05-10", "Full-Time Employee Working 30+ Hours", "Active", "90813", "Health Net HMO", "Employee", "N"],
        ["Employee", "Tom", "Terminated", "M", "1978-12-29", "Full-Time Employee Working 30+ Hours", "Terminated", "90813", "Health Net HMO", "Employee + Child(ren)", "N"],
        ["Child", "Timmy", "Terminated", "M", "2015-03-20", "", "", "90813", "", "", ""],
        ["Employee", "Carla", "Cobra", "F", "1980-08-15", "Full-Time Employee Working 30+ Hours", "Terminated", "90813", "Health Net HMO", "Employee", "Y"],
    ]
    df = pd.DataFrame(rows)

    doc = RawDoc(
        path=Path("EENAV_Census_Medical.xlsx"),
        ext=".xlsx",
        tables=[RawTable(name="Census", df=df)],
        text="",
    )
    census = build_census([doc], effective_date=date(2027, 1, 1))
    if isinstance(census, tuple):
        census = census[0]
    refine_non_enrolled_statuses(census, effective_date=date(2027, 1, 1))

    active_names = {e.display for e in census.canonical_census_employees}
    excluded_names = {e.display for e in census.excluded_employees}

    assert active_names == {"Alice Active", "Carla Cobra"}
    assert "Tom Terminated" in excluded_names
    assert len(census.dependents) == 0
    assert any(d.display == "Timmy Terminated" for d in census.excluded_dependents)

    # Sanity gate should only count the 2 active/COBRA employees, not the terminated employee
    sanity = evaluate_population_sanity(census=census, invoice_records=[])
    assert sanity.metrics["parsed_employees"] == 2
    assert not sanity.blocked
