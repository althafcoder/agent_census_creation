"""Tests for the Sandboxed Python Code Interpreter (`backend/python_sandbox.py`) and orchestrator integration."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from agent_orchestrator import INTAKE_AGENT_TOOLS, execute_inspection_tool
from python_sandbox import PythonSandboxSession, validate_sandbox_code
from readers import RawDoc, RawTable


def _make_sample_docs() -> list[RawDoc]:
    df1 = pd.DataFrame([
        ["Relationship", "First Name", "Last Name", "Class", "Employment Status", "Medical Plan"],
        ["Employee", "Alice", "Active", "Full-Time 30+", "Active", "HMO Gold"],
        ["Employee", "Bob", "PartTime", "Part-Time Employee", "Active", ""],
        ["Employee", "Charlie", "Gone", "Full-Time 30+", "Terminated", "HMO Gold"],
    ])
    df2 = pd.DataFrame([
        ["First Name", "Last Name", "Status"],
        ["Alice", "Active", "Active"],
        ["Old", "Ghost", "Active"],
    ])
    doc1 = RawDoc(path=Path("authoritative_roster.xlsx"), ext=".xlsx", tables=[RawTable(name="Census", df=df1)], text="")
    doc2 = RawDoc(path=Path("older_export.xlsx"), ext=".xlsx", tables=[RawTable(name="All", df=df2)], text="")
    doc3 = RawDoc(path=Path("rfp_summary.pdf"), ext=".pdf", tables=[], text="Employer: Acme Medical\nTotal Full-Time Eligible: 1\nState: CA")
    return [doc1, doc2, doc3]


def test_sandbox_profiles_data_and_computes_set_difference():
    docs = _make_sample_docs()
    with PythonSandboxSession(docs) as sb:
        out = sb.run("""
list_files()
column_summary("authoritative_roster.xlsx", "Census", header_row=0)
df1 = sheet_df("authoritative_roster.xlsx", "Census", header_row=0)
df2 = sheet_df("older_export.xlsx", "All", header_row=0)
s1 = set(zip(df1["First Name"], df1["Last Name"]))
s2 = set(zip(df2["First Name"], df2["Last Name"]))
print("ONLY_IN_OLD:", sorted(s2 - s1))
search_text("Full-Time Eligible", "rfp_summary.pdf")
""")
    assert "authoritative_roster.xlsx" in out
    assert "Part-Time Employee" in out
    assert "Terminated" in out
    assert "ONLY_IN_OLD: [('Old', 'Ghost')]" in out
    assert "Total Full-Time Eligible: 1" in out


def test_sandbox_security_blocks_dangerous_code():
    assert validate_sandbox_code("import subprocess\nsubprocess.run(['dir'])") is not None
    assert validate_sandbox_code("import urllib.request") is not None
    assert validate_sandbox_code("import socket") is not None
    assert validate_sandbox_code("import os\nprint(os.environ)") is not None
    assert validate_sandbox_code("open('evil.txt', 'w').write('x')") is not None
    assert validate_sandbox_code("eval('1+1')") is not None
    assert validate_sandbox_code("import os\nos.remove('x')") is not None
    # Safe analysis code passes (including str.replace, df.rename, list.remove)
    assert validate_sandbox_code("import re\ndf = sheet_df('a.xlsx').rename(columns={'A':'B'})\ns = '$100'.replace('$','')\nlst=[1,2]; lst.remove(1)\nprint(len(df), s, lst)") is None
    assert validate_sandbox_code("import os\nos.rename('a', 'b')") is not None
    assert validate_sandbox_code("import os\nos.replace('a', 'b')") is not None


def test_sandbox_timeout_guard():
    docs = _make_sample_docs()
    with PythonSandboxSession(docs, timeout_seconds=1.5) as sb:
        out = sb.run("while True:\n    pass")
    assert "timed out" in out.lower()


def test_orchestrator_exposes_and_dispatches_run_python():
    tool_names = {t["name"] for t in INTAKE_AGENT_TOOLS}
    assert "run_python" in tool_names
    assert "commit_submission_blueprint" in tool_names

    docs = _make_sample_docs()
    with PythonSandboxSession(docs) as sb:
        res = execute_inspection_tool(
            "run_python",
            {"code": "df = sheet_df('authoritative_roster.xlsx', header_row=0)\nprint('ACTIVE_FT:', len(df[(df['Employment Status']=='Active') & (~df['Class'].str.contains('Part-Time'))]))"},
            docs,
            sandbox=sb,
        )
    assert "ACTIVE_FT: 1" in res


def test_emit_canonical_census_and_convert_to_passing_census():
    from agent_orchestrator import canonical_json_to_census
    from population_sanity import evaluate_population_sanity

    docs = _make_sample_docs()
    with PythonSandboxSession(docs) as sb:
        out = sb.run("""
emit_canonical_census({
    "employees": [
        {
            "first": "Gustavo", "last": "Rodriguez", "gender": "M", "dob": "1967-10-24",
            "zip": "07109", "coverage": "FAM", "plan": "AETNA EPO 0-100 45-65 NE",
            "cobra": "N", "premium": 2823.69, "source": "authoritative_roster.xlsx",
            "dependents": [
                {"role": "SP", "first": "Sofia", "last": "Rodriguez", "gender": "F", "dob": "1972-04-22", "zip": "07109"},
                {"role": "CH", "first": "Daniel", "last": "Rodriguez", "gender": "M", "dob": "2002-01-03", "zip": "07109"},
            ],
        },
        {
            "first": "Ann", "last": "Campbell", "gender": "F", "dob": "1985-06-15",
            "zip": "06903", "coverage": "WO", "plan": "NA", "cobra": "N",
            "source": "authoritative_roster.xlsx", "dependents": [],
        },
    ],
    "excluded_count": 3,
    "notes": ["Excluded 3 part-time employees via Python rescue"],
})
""")
        payload = sb.read_canonical_census()

    assert "EMITTED canonical_census.json: 2 employees, 2 dependents" in out
    assert payload is not None
    census = canonical_json_to_census(payload, source_files=["authoritative_roster.xlsx"])
    assert census is not None
    assert len(census.canonical_census_employees) == 2
    assert len(census.dependents) == 2
    assert len(census.unassigned_dependents) == 0
    assert census.canonical_census_employees[0].zip == "07109"
    assert census.dependents[0].sub_row == census.canonical_census_employees[0].row_no

    sanity = evaluate_population_sanity(census=census, invoice_records=[])
    assert not sanity.blocked


def test_sandbox_preloads_extra_xlsx_and_pads_4digit_zip(tmp_path):
    import openpyxl
    from agent_orchestrator import canonical_json_to_census

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Census"
    ws.append(["First", "Last", "ZIP"])
    ws.append(["Jane", "Doe", "07109"])
    xlsx_path = tmp_path / "Generated_Output.xlsx"
    wb.save(xlsx_path)

    with PythonSandboxSession([], extra_files={"Generated_Output.xlsx": str(xlsx_path)}) as sb:
        out = sb.run("df = sheet_df('Generated_Output.xlsx', 'Census', header_row=0)\nprint('ROWS:', len(df), 'FIRST:', df.iloc[0]['First'].replace('J', 'j'))")
    assert "ROWS: 1 FIRST: jane" in out

    c = canonical_json_to_census({
        "employees": [{"first": "Jane", "last": "Doe", "zip": "7109", "coverage": "EE", "plan": "HMO"}],
    })
    assert c is not None
    assert c.employees[0].zip == "07109"

