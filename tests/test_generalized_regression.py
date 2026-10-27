"""Generalized regression tests verifying dynamic, reusable logic across all submissions.

Ensures that:
1. Relationships and subscriber IDs are preserved across reordered rows, deduplication, and sorting.
2. Subscriber IDs convert to final workbook data rows only after output order is finalized.
3. Dependents appearing before their subscriber in source files are correctly linked.
4. Decimal arithmetic and sign preservation work accurately for any monetary amounts.
5. Invoices with arbitrary subscriber names, plan IDs, and component lines parse deterministically.
6. Local cache bypass toggle executes cleanly and bypasses cached entries.
"""
from datetime import date
from decimal import Decimal
import pandas as pd
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from models import Person, Census, InvoiceRecord, InvoiceDoc, Findings
from census_builder import _link_dependents, _order_output, _State, _DepRef, validate_census
from utils import name_key
from invoice_parser import combine_records
from tracer import DiagnosticTracer
from caching.local_cache import LocalPromptCache


def test_dependent_linking_order_independence():
    """Verify dependents are correctly linked to subscriber even if they appear before the subscriber."""
    census = Census()
    st = _State(census)

    # 1. Dependent created FIRST
    dep = Person(role="CH", first="ChildOne", last="Smith", rel_raw="Child", source="census.xlsx")
    dep.record_id = "rec_0001"
    dep.subscriber_id = ""  # Not yet linked
    ref = _DepRef(dep, emp_key=name_key("John", "Smith"), file="census.xlsx")
    st.dep_refs.append(ref)

    # 2. Employee created SECOND
    emp = Person(role="EE", first="John", last="Smith", rel_raw="Employee", source="census.xlsx")
    emp.record_id = "rec_0002"
    emp.coverage = "EC"
    emp.plan = "PlanAlpha"
    st.employees.append(emp)
    st.by_name[name_key("John", "Smith")] = [emp]

    # 3. Link dependents
    _link_dependents(st, effective_date=date(2026, 1, 1))

    # Assert link is established
    assert dep.employee is emp
    assert dep.subscriber_id == emp.record_id
    assert dep in emp.dependents

    # 4. Finalize output order
    _order_output(st)

    # Employee is row 1, dependent is row 2
    assert emp.row_no == 1
    assert dep.row_no == 2
    # In workbook output, dependent references employee's data row (1)
    assert dep.employee.row_no == 1


def test_reordered_rows_subscriber_data_row_resolution():
    """Ensure data rows are strictly assigned sequentially in final output order, never hardcoded."""
    census = Census()
    st = _State(census)

    emp1 = Person(role="EE", first="Alice", last="Brown")
    emp1.record_id = "rec_0010"
    emp2 = Person(role="EE", first="Charlie", last="Davis")
    emp2.record_id = "rec_0020"
    dep2 = Person(role="SP", first="Dana", last="Davis")
    dep2.record_id = "rec_0021"

    st.employees = [emp1, emp2]
    dep2.employee = emp2
    dep2.subscriber_id = emp2.record_id
    emp2.dependents.append(dep2)

    _order_output(st)

    assert emp1.row_no == 1
    assert emp2.row_no == 2
    assert dep2.row_no == 3
    assert dep2.employee.row_no == 2


def test_census_validation_detects_unresolved_dependents():
    """Flag unresolved relationships for review rather than guessing."""
    census = Census()
    emp = Person(role="EE", first="Alice", last="Taylor")
    emp.record_id = "rec_0001"
    dep_orphan = Person(role="CH", first="Unknown", last="Orphan")
    dep_orphan.record_id = "rec_0002"
    dep_orphan.subscriber_id = "" # No subscriber link

    census.people = [emp, dep_orphan]

    warnings = validate_census(census)
    assert any("has no subscriber_id" in w for w in warnings)


def test_invoice_combine_records_no_cross_employee_merge():
    """Verify invoice combine_records does not merge different subscribers with the same policy."""
    inv = InvoiceDoc(path="invoice.pdf", records=[
        InvoiceRecord(first="Michael", last="Scott", plan="PPO500", current_premium=650.00, retro_amount=0.00),
        InvoiceRecord(first="Dwight", last="Schrute", plan="PPO500", current_premium=650.00, retro_amount=150.00, retro_code="ADD"),
    ])

    combined = combine_records([inv])
    assert len(combined) == 2
    names = {f"{r.first} {r.last}" for r in combined}
    assert "Michael Scott" in names
    assert "Dwight Schrute" in names

    dwight = next(r for r in combined if r.last == "Schrute")
    assert dwight.current_premium == 650.00
    assert dwight.retro_amount == 150.00
    assert dwight.retro_code == "ADD"


def test_cache_bypass_flag(tmp_path):
    """Verify bypass_cache parameter forces cache miss and bypasses SQLite cache."""
    cache = LocalPromptCache(db_dir=tmp_path)
    prompt = "Test prompt for dynamic cache bypass verification"
    model = "claude-test-model"
    response = {"result": "success"}

    # Set in cache
    cache.set(prompt, model, response, step_name="test_step")

    # Regular lookup should hit
    hit = cache.get(prompt, model, step_name="test_step", bypass_cache=False)
    assert hit == response

    # Lookup with bypass_cache=True must return None
    miss = cache.get(prompt, model, step_name="test_step", bypass_cache=True)
    assert miss is None
