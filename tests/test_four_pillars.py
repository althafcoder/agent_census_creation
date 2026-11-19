"""Tests covering Pillars #1-#4: Agent-Python-First Census+Invoices, Recipe Memory, and Adversarial Verifier."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from agent_orchestrator import canonical_json_to_census, canonical_json_to_invoices
from python_sandbox import PythonSandboxSession
from readers import RawDoc, RawTable
from recipe_memory import format_recipes_for_prompt, record_new_recipe, retrieve_relevant_recipes
from workbook_auditor import _validate_audit_dict


def test_pillar2_recipe_memory_retrieval_and_pii_guard(tmp_path):
    df = pd.DataFrame([
        [float("nan"), "EE-First Name", "EE-Last Name", None, "DEP-Relation", "DEP-First Name", "Benefit Plan description", "Auto Enrollment"],
        [None, "Jane", "Smith", float("nan"), "WIFE", "Ann", "MetLife NY PFL -ME", "Y"],
    ])
    doc = RawDoc(path=Path("Benefit_Report.xlsx"), ext=".xlsx", tables=[RawTable(name="Census", df=df)], text="")
    recipes = retrieve_relevant_recipes([doc], max_recipes=3)
    assert recipes
    assert recipes[0]["id"] == "horizontal_ee_dep_multi_benefit"
    prompt_txt = format_recipes_for_prompt(recipes)
    assert "LEARNED PROCEDURAL RECIPES" in prompt_txt

    custom_file = tmp_path / "recipes.json"
    # PII-free recipe persists
    assert record_new_recipe("custom_peo_rule", "Custom PEO Rule", ["peo_code"], "Always map code X to WO.", custom_path=custom_file) is True
    # PII-containing recipe is rejected
    assert record_new_recipe("bad_pii", "Bad", ["x"], "Email john@example.com or SSN 123-45-6789", custom_path=custom_file) is False


def test_pillar4_emit_canonical_invoices_and_convert():
    doc = RawDoc(path=Path("Oct_Invoice.pdf"), ext=".pdf", tables=[], text="DOE, JANE  AETNA EPO  EE  650.00\nTOTAL DUE: 650.00")
    with PythonSandboxSession([doc]) as sb:
        out = sb.run("""
emit_canonical_invoices({
    "invoices": [{
        "file_name": "Oct_Invoice.pdf",
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "group_number": "14179816",
        "current_charges": 650.00,
        "total_adjustments": 0.00,
        "total_due": 650.00,
        "records": [
            {"first": "Jane", "last": "Doe", "raw_name": "DOE, JANE", "plan": "AETNA EPO", "tier": "EE", "premium": 650.00, "sub_id": "1001"}
        ]
    }]
})
""")
        inv_payload = sb.read_canonical_invoices()

    assert "EMITTED canonical_invoices.json: 1 invoice(s), 1 medical subscriber record(s)" in out
    inv_docs = canonical_json_to_invoices(inv_payload)
    assert len(inv_docs) == 1
    assert inv_docs[0].current_charges == 650.00
    assert inv_docs[0].group_number == "14179816"
    assert len(inv_docs[0].records) == 1
    assert inv_docs[0].records[0].first == "Jane"
    assert inv_docs[0].records[0].premium == 650.00


def test_pillar3_adversarial_verifier_field_fixes_validation():
    validated = _validate_audit_dict({
        "overall_status": "warnings",
        "confidence_summary": "Caught 1 blank gender and 1 false COBRA=Y.",
        "findings": [],
        "safe_summary_patches": [],
        "repair_actions": {
            "exclude_employees": [],
            "status_overrides": [],
            "dependent_links": [],
            "field_fixes": [
                {"person_name": "Adam Krauseneck", "gender": "M", "dob": "1982-12-22", "zip": "06807", "cobra": "N", "reason": "Present in EE-Gender/EE-Birth Date; Auto Enrollment is not COBRA"},
            ],
        },
    })
    ff = validated["repair_actions"]["field_fixes"]
    assert len(ff) == 1
    assert ff[0]["gender"] == "M"
    assert ff[0]["cobra"] == "N"
    assert ff[0]["zip"] == "06807"
