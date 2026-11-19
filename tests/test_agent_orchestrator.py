"""Tests for the Claude Tool-Using Agent Orchestrator (Read -> Inspect -> Commit -> Verify)."""
from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent_orchestrator import (  # noqa: E402
    INTAKE_AGENT_TOOLS,
    execute_inspection_tool,
    run_agent_intake_plan,
    run_tool_based_workbook_audit,
)
from readers import RawDoc, RawTable  # noqa: E402


def test_intake_tools_schema_is_valid():
    names = {t["name"] for t in INTAKE_AGENT_TOOLS}
    assert names == {"run_python", "inspect_spreadsheet_rows", "inspect_pdf_text", "commit_submission_blueprint"}
    for t in INTAKE_AGENT_TOOLS:
        assert t["input_schema"].get("additionalProperties") is False


def test_execute_inspection_tools_reads_rows_and_pdf_slices():
    df = pd.DataFrame([
        ["Company Name:", "OneTel Security", "", ""],
        ["First Name", "Last Name", "SP (Y/N)", "Residential Zip Code"],
        ["Kelsey", "Kieffer", "Y", "84054-3379"],
    ])
    xls_doc = RawDoc(path=Path("OneTel_Census.xls"), ext=".xls", tables=[RawTable("Census", df)], text="")
    pdf_doc = RawDoc(
        path=Path("Benefits_Booklet.pdf"),
        ext=".pdf",
        text="2026 Employee Benefits Guide. SelectMed Gold 2000. Out-of-network OOP $2,000 / $40,000.",
    )

    row_out = execute_inspection_tool(
        "inspect_spreadsheet_rows",
        {"file_name": "OneTel_Census.xls", "start_row": 1, "end_row": 3},
        [xls_doc, pdf_doc],
    )
    assert "Residential Zip Code" in row_out
    assert "Kelsey" in row_out and "84054-3379" in row_out

    pdf_out = execute_inspection_tool(
        "inspect_pdf_text",
        {"file_name": "Benefits_Booklet.pdf", "start_char": 0, "max_chars": 500},
        [xls_doc, pdf_doc],
    )
    assert "Employee Benefits Guide" in pdf_out
    assert "$2,000 / $40,000" in pdf_out


def test_agent_intake_loop_multi_turn_inspect_then_commit(monkeypatch):
    df = pd.DataFrame([
        ["First Name", "Last Name", "DOB", "Gender", "Relationship", "Residential Zip Code"],
        ["Matthew", "Bakes", "02/04/1999", "M", "Employee", "84054-3379"],
    ])
    docs = [
        RawDoc(path=Path("Census.xls"), ext=".xls", tables=[RawTable("Census", df)], text="Census"),
        RawDoc(path=Path("Original_Email.pdf"), ext=".pdf", text="Current PEO: NA\nCurrent Medical: Select Health"),
    ]

    monkeypatch.delenv("CENSUS_DISABLE_AGENT_LOOP", raising=False)
    monkeypatch.delenv("CENSUS_DISABLE_BLUEPRINT", raising=False)
    monkeypatch.setattr("census_config.ANTHROPIC_API_KEY", "sk-ant-real-mock-key")
    monkeypatch.setattr("census_config.LLM_FIRST", True)
    monkeypatch.setattr("census_config.USE_LLM_FALLBACK", True)

    turn_calls = []

    class _FakeMessages:
        def create(self, **kwargs):
            turn_calls.append(kwargs)
            if len(turn_calls) == 1:
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            id="tu_1",
                            name="inspect_spreadsheet_rows",
                            input={"file_name": "Census.xls", "start_row": 0, "end_row": 2},
                        )
                    ],
                    usage=SimpleNamespace(input_tokens=100, output_tokens=30),
                )
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="tu_2",
                        name="commit_submission_blueprint",
                        input={
                            "documents": [
                                {"file_name": "Census.xls", "role": "census", "reason": "Employee census", "sbc_plan_ids": []},
                                {"file_name": "Original_Email.pdf", "role": "rfp", "reason": "Email", "sbc_plan_ids": []},
                            ],
                            "prospect": {
                                "company": "OneTel Security LLC",
                                "dba": "",
                                "ein": "",
                                "sic": "",
                                "address": "",
                                "city": "North Salt Lake",
                                "state": "UT",
                                "zip": "84054",
                                "effective_date": "",
                                "sales_person": "Danny Fifita",
                                "referral": "",
                                "current_carrier": "Select Health",
                                "is_peo": False,
                                "peo_name": "",
                                "contact_name": "",
                                "contact_email": "",
                                "contact_phone": "",
                                "stated_employees": 1,
                                "renewal_date": "01/01/2027",
                            },
                            "census_schemas": [
                                {
                                    "file_name": "Census.xls",
                                    "sheet_name": "Census",
                                    "header_row": 0,
                                    "is_census_table": True,
                                    "column_mappings": [
                                        {"field": "first_name", "column_index": 0},
                                        {"field": "last_name", "column_index": 1},
                                        {"field": "dob", "column_index": 2},
                                        {"field": "gender", "column_index": 3},
                                        {"field": "relationship", "column_index": 4},
                                        {"field": "zip", "column_index": 5},
                                    ],
                                    "value_maps": [],
                                }
                            ],
                            "email_facts": [],
                            "plan_links": [],
                            "invoice_control_totals": [],
                            "agent_clarifications": [
                                "Please provide official 2026 Select Health carrier SBCs (Benefits Guide does not replace SBCs)."
                            ],
                            "excluded_employees": [
                                {"person_name": "Test Employee", "reason": "Test/placeholder row"}
                            ],
                        },
                    )
                ],
                usage=SimpleNamespace(input_tokens=150, output_tokens=80),
            )

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)

    bp = run_agent_intake_plan(docs, bypass_cache=True)
    assert bp is not None
    assert len(turn_calls) == 2
    assert bp.role_for("Census.xls") == "census"
    assert bp.prospect["is_peo"] is False
    assert bp.prospect["peo_name"] == ""
    assert len(bp.census_schemas) == 1
    assert bp.census_schemas[0].columns["zip"] == 5
    assert len(bp.agent_clarifications) == 1


def test_tool_based_workbook_audit_returns_structured_dict(monkeypatch):
    monkeypatch.setattr("census_config.ANTHROPIC_API_KEY", "sk-ant-real-mock-key")

    class _FakeMessages:
        def create(self, **kwargs):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="aud_1",
                        name="submit_workbook_audit",
                        input={
                            "overall_status": "passed",
                            "confidence_summary": "Verified 59/59 lives and clean Group Info.",
                            "findings": [],
                            "safe_summary_patches": [],
                        },
                    )
                ],
                usage=SimpleNamespace(input_tokens=200, output_tokens=50),
            )

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = _FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)
    snap = {"workbook": {"workbook_name": "test.xlsx"}, "digest_text": "test snapshot"}
    ok, conf, findings, patches = run_tool_based_workbook_audit(snap)
    assert ok is True
    assert "59/59" in conf
    assert findings == []
    assert patches == []
