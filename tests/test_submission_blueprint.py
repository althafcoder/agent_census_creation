"""Tests for Whole-Submission LLM Blueprint (Scoped Rules + Strict JSON Schema) + Python Execution + Math Gates.

Verifies:
1. BLUEPRINT_SCHEMA is 100% Anthropic Structured-Outputs compliant (additionalProperties=False, explicit required).
2. Scoped instructions ('blueprint') avoid 94KB prompt dilution while retaining core governing sections & Section 32.
3. Whole-submission digest includes indexed spreadsheet rows/cols, reversed/overprint repair, and invoice tail totals.
4. ZERO-REGEX unseen column headers & opaque value codes parse cleanly via Blueprint -> Python execution.
5. Casual natural-language broker email facts from Blueprint flow into comparator & Missing Information Email.
6. Blueprint plan links & sbc_plan_ids resolve renewal rates and suppress false SBC requests.
7. Invoice Control-Total Math Gate catches dropped invoice rows vs printed Current Charges.
8. Validator deterministically rejects placeholders, out-of-bounds columns, and invalid value-map meanings.
"""
from datetime import date
from pathlib import Path
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from census_builder import build_census  # noqa: E402
from classifier import detect_header  # noqa: E402
from comparator import run_comparison  # noqa: E402
from llm_client import get_official_instructions, get_scoped_instructions  # noqa: E402
from models import Census, Findings, InvoiceDoc, InvoiceRecord, Person, Prospect, RenewalDoc  # noqa: E402
from readers import RawDoc, RawTable  # noqa: E402
from router import RoutingDecision, build_missing_information_email_parts  # noqa: E402
from submission_blueprint import (  # noqa: E402
    BLUEPRINT_SCHEMA,
    build_submission_digest,
    validate_blueprint_dict,
    verify_invoice_control_totals,
)


def _assert_anthropic_strict_schema(node: dict, path: str = "$") -> None:
    t = node.get("type")
    if t == "object":
        assert node.get("additionalProperties") is False, f"{path} missing additionalProperties=False"
        props = node.get("properties") or {}
        req = set(node.get("required") or [])
        assert req == set(props.keys()), f"{path} required keys mismatch: {req} vs {set(props.keys())}"
        for k, sub in props.items():
            _assert_anthropic_strict_schema(sub, f"{path}.{k}")
    elif t == "array" and isinstance(node.get("items"), dict):
        _assert_anthropic_strict_schema(node["items"], f"{path}[]")


def test_blueprint_schema_is_anthropic_strict_compliant():
    _assert_anthropic_strict_schema(BLUEPRINT_SCHEMA)


def test_scoped_blueprint_instructions_reduce_prompt_dilution():
    full_inst = get_official_instructions()
    scoped = get_scoped_instructions("blueprint")
    assert "CRITICAL STAGE CHECKLIST (WHOLE-SUBMISSION BLUEPRINT)" in scoped
    assert "32. CROSS-DOCUMENT INGESTION" in scoped
    assert len(scoped) < len(full_inst), f"scoped ({len(scoped)}) should be smaller than full ({len(full_inst)})"


def test_digest_indexes_spreadsheets_repairs_pdfs_and_captures_invoice_totals():
    df = pd.DataFrame([
        ["Person Flag", "Surname", "Given", "Born"],
        ["A", "Barraza", "Kathleen", "1998-03-31"],
    ])
    census_doc = RawDoc(path=Path("roster.xlsx"), ext=".xlsx", tables=[RawTable("Data", df)])
    inv_doc = RawDoc(
        path=Path("bill.pdf"),
        ext=".pdf",
        text=("Header line\n" + ("Detail line\n" * 400) + "TOTAL FEES  2,307.00  60,283.73  62,590.73\n"),
    )
    digest = build_submission_digest([census_doc, inv_doc])
    assert "Row 0: [0]=Person Flag, [1]=Surname, [2]=Given, [3]=Born" in digest
    assert "Row 1: [0]=A, [1]=Barraza, [2]=Kathleen, [3]=1998-03-31" in digest
    assert "60,283.73" in digest


def test_unseen_column_names_and_opaque_codes_parse_via_blueprint_without_regex(monkeypatch):
    """Brand-new unseen headers (zero FIELD_ALIASES matches) + opaque codes (A/B, 1/2) parse 100% cleanly."""
    monkeypatch.setenv("CENSUS_DISABLE_LLM_VALUE_MAP", "1")
    df = pd.DataFrame([
        ["Acme Custom Export - Q4"],
        ["IND_CODE", "NAME_L", "NAME_F", "DT_BIRTH", "SEX_CD", "POSTAL_CD", "COV_OPT", "MED_PKG"],
        ["A", "Blair", "Carson", "1989-10-18", "1", "78738", "Family", "BCBS Platinum P621CHC"],
        ["B", "Blair", "Angela", "1987-08-07", "2", "78738", "", ""],
        ["B", "Blair", "Caden", "2023-04-14", "1", "78738", "", ""],
        ["A", "Barraza", "Kathleen", "1998-03-31", "2", "78253", "EE", "BCBS Platinum P621CHC"],
    ])
    doc = RawDoc(path=Path("Unseen_Export.xlsx"), ext=".xlsx", tables=[RawTable("ExportTab", df)])

    # Without Blueprint, regex FIELD_ALIASES cannot recognize these unseen column names at all!
    assert detect_header(df) is None

    # With a validated Blueprint, Python executes deterministically across 100% of rows
    bp = validate_blueprint_dict({
        "documents": [{"file_name": "Unseen_Export.xlsx", "role": "census", "reason": "roster", "sbc_plan_ids": []}],
        "prospect": {},
        "census_schemas": [{
            "file_name": "Unseen_Export.xlsx",
            "sheet_name": "ExportTab",
            "header_row": 1,
            "is_census_table": True,
            "column_mappings": [
                {"field": "relationship", "column_index": 0},
                {"field": "last_name", "column_index": 1},
                {"field": "first_name", "column_index": 2},
                {"field": "dob", "column_index": 3},
                {"field": "gender", "column_index": 4},
                {"field": "zip", "column_index": 5},
                {"field": "coverage", "column_index": 6},
                {"field": "plan", "column_index": 7},
            ],
            "value_maps": [
                {"field": "relationship", "mappings": [{"raw_code": "A", "meaning": "Employee"}, {"raw_code": "B", "meaning": "Dependent"}]},
                {"field": "gender", "mappings": [{"raw_code": "1", "meaning": "Male"}, {"raw_code": "2", "meaning": "Female"}]},
            ],
        }],
        "email_facts": [],
        "plan_links": [],
        "invoice_control_totals": [],
    }, docs=[doc])

    census, _ = build_census([doc], effective_date=date(2027, 1, 1), blueprint=bp)
    emps = [p for p in census.people if p.role == "EE"]
    deps = [p for p in census.people if p.role in ("SP", "CH")]
    assert len(emps) == 2
    assert len(deps) == 2
    assert not census.unassigned_dependents
    carson = next(e for e in emps if e.last == "Blair")
    assert carson.gender == "M" and carson.coverage == "FAM" and carson.plan == "BCBS Platinum P621CHC"
    assert sorted((d.role, d.first, d.gender) for d in carson.dependents) == [("CH", "Caden", "M"), ("SP", "Angela", "F")]


def test_blueprint_casual_email_facts_and_plan_links_drive_comparison_and_sbc():
    bp = validate_blueprint_dict({
        "documents": [
            {"file_name": "benefit_doc_1.pdf", "role": "plan_design", "reason": "SBC", "sbc_plan_ids": ["PLAT811"]},
        ],
        "prospect": {
            "company": "Acme Energy LLC", "dba": "N/A", "ein": "12-3456789", "sic": "TBD",
            "address": "100 Main St", "city": "Austin", "state": "TX", "zip": "78701",
            "effective_date": "01/01/2027", "sales_person": "", "referral": "",
            "current_carrier": "BCBS", "is_peo": False, "peo_name": "",
            "contact_name": "", "contact_email": "", "contact_phone": "",
            "stated_employees": 2, "renewal_date": "",
        },
        "census_schemas": [],
        "email_facts": [{
            "person_name": "John Doe",
            "event_type": "terminated",
            "effective_date": "11/10/2026",
            "note": "Broker casually wrote: FYI John left last Friday, please drop him from billing.",
            "source_file": "Broker_Note.pdf",
        }],
        "plan_links": [{
            "invoice_plan": "GRP-99",
            "canonical_plan_id": "PLAT811",
            "sbc_file": "benefit_doc_1.pdf",
            "evidence": "Matched via rate sheet and email note",
        }],
        "invoice_control_totals": [],
    })

    # Placeholders ("N/A", "TBD") stripped cleanly
    pr = Prospect()
    bp.apply_to_prospect(pr)
    assert pr.company == "Acme Energy LLC"
    assert pr.dba == "" and pr.sic == ""
    assert pr.stated_total_employees == 2

    # Plan link bridges opaque GRP-99 -> PLAT811 renewal rate & SBC
    c = Census()
    c.people.append(Person("EE", "Jane", "Smith", coverage="EE", plan="GRP-99", dob=date(1990, 5, 1)))
    invs = [
        InvoiceRecord(first="Jane", last="Smith", raw_name="SMITH, JANE", plan="GRP-99", tier="EE", premium=900.00),
        InvoiceRecord(first="John", last="Doe", raw_name="DOE, JOHN", plan="GRP-99", tier="EE", premium=900.00),
    ]
    ren = RenewalDoc(path="ren.pdf", effective=date(2027, 1, 1), rates={("PLAT811", "EE"): 990.00}, plans=["PLAT811"])
    F = Findings()
    meta = {"saw_dependent_source": False, "email_evidence": bp.as_person_evidence(), "blueprint_plan_links": bp.plan_links}
    results = run_comparison(c, invs, ren, meta, F, have_invoice=True, renewal_required=True)

    assert not F.renewal_unmapped_plans
    assert results[id(c.people[0])].renewal_premium == pytest.approx(990.00)

    inv_only = [p for p in c.people if p.invoice_only][0]
    assert "11/10/2026" in " ".join(n[0] for n in results[id(inv_only)].notes)

    sbc_doc = RawDoc(path=Path("benefit_doc_1.pdf"), ext=".pdf", text="Summary of Benefits and Coverage: Platinum")
    setattr(sbc_doc, "sbc_plan_ids", bp.sbc_plan_ids_for("benefit_doc_1.pdf"))
    _, sections = build_missing_information_email_parts(
        prospect=pr, routing=RoutingDecision(carrier="Cigna", state="TX", is_florida=False),
        findings=F, census=c, have_census=True, have_invoice=True, have_renewal=True,
        renewal_required=True, claims_required=False, ghq_present=False,
        roles={"plan_design": [sbc_doc]}, docs=[sbc_doc],
    )
    assert not any(t == "SBC" for t, _ in sections), f"False SBC request: {sections}"


def test_invoice_control_total_math_gate():
    good_inv = InvoiceDoc(
        path="bill.pdf",
        records=[InvoiceRecord(first="A", last="B", premium=1000.00), InvoiceRecord(first="C", last="D", premium=500.00)],
        current_charges=1500.00,
    )
    assert verify_invoice_control_totals([good_inv])["passed"] is True

    dropped_inv = InvoiceDoc(
        path="bill.pdf",
        records=[InvoiceRecord(first="A", last="B", premium=1000.00)],
        current_charges=1500.00,
    )
    gate = verify_invoice_control_totals([dropped_inv])
    assert gate["passed"] is False
    assert gate["checks"][0]["variance"] == pytest.approx(-500.00)
    assert "differ from printed Current Charges" in gate["issues"][0]


def test_validator_rejects_out_of_bounds_columns_and_invalid_value_maps():
    df = pd.DataFrame([["ColA", "ColB"], ["x", "y"]])
    doc = RawDoc(path=Path("tiny.xlsx"), ext=".xlsx", tables=[RawTable("S1", df)])
    bp = validate_blueprint_dict({
        "documents": [{"file_name": "nonexistent.pdf", "role": "invoice", "reason": "", "sbc_plan_ids": []}],
        "prospect": {},
        "census_schemas": [{
            "file_name": "tiny.xlsx",
            "sheet_name": "S1",
            "header_row": 0,
            "is_census_table": True,
            "column_mappings": [
                {"field": "first_name", "column_index": 0},
                {"field": "last_name", "column_index": 99},  # out of bounds -> must be dropped
            ],
            "value_maps": [
                {"field": "gender", "mappings": [{"raw_code": "1", "meaning": "NotAGender"}]},  # invalid -> dropped
            ],
        }],
        "email_facts": [],
        "plan_links": [],
        "invoice_control_totals": [],
    }, docs=[doc])
    assert bp.documents == {}
    assert bp.census_schemas[0].columns == {"first_name": 0}
    assert bp.census_schemas[0].value_maps == {}
