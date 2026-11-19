"""Tests for the Post-Generation Workbook Auditor (Deterministic + Scoped LLM QA Review)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import census_config as C  # noqa: E402
from llm_client import get_official_instructions, get_scoped_instructions  # noqa: E402
from models import Findings, InvoiceDoc, InvoiceRecord, Prospect  # noqa: E402
from readers import RawDoc  # noqa: E402
from workbook_auditor import (  # noqa: E402
    AUDIT_SCHEMA,
    _validate_audit_dict,
    audit_generated_deliverables,
    build_workbook_audit_snapshot,
    run_deterministic_output_audit,
)


def _walk_schema_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for v in node.values():
            yield from _walk_schema_objects(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schema_objects(item)


def test_audit_schema_is_anthropic_strict_compliant():
    objs = list(_walk_schema_objects(AUDIT_SCHEMA))
    assert len(objs) >= 3
    for obj in objs:
        assert obj.get("additionalProperties") is False
        props = set(obj.get("properties", {}).keys())
        req = set(obj.get("required", []))
        assert props == req
    # Confirm no array-style "type" (Anthropic prefers anyOf)
    def _has_type_array(n):
        if isinstance(n, dict):
            if isinstance(n.get("type"), list):
                return True
            return any(_has_type_array(v) for v in n.values())
        if isinstance(n, list):
            return any(_has_type_array(x) for x in n)
        return False
    assert not _has_type_array(AUDIT_SCHEMA)


def test_scoped_audit_instructions_reduce_prompt_dilution():
    full = get_official_instructions()
    scoped = get_scoped_instructions("audit")
    assert "POST-GENERATION WORKBOOK AUDITOR" in scoped
    assert len(scoped) < len(full) * 0.65


def _make_marsh_wb(path: Path, *, corrupt_peo=False, corrupt_group_col=False,
                   blank_plans=False, blank_dep_cov=False, leak_inv_only=False,
                   stale_added_count=False) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Group Info + Census"

    # Header block (Rows 4-7)
    ws["A4"], ws["B4"] = "Date Submitted", "11/18/2026"
    ws["D4"], ws["E4"] = "Proposed Effective Date", "01/01/2027"
    ws["A5"], ws["B5"] = "Group Name", "Acme Apparel, LLC"
    ws["D5"], ws["E5"] = "Total Employees", 6
    ws["G5"], ws["H5"] = "Current Carrier", "BCBSNC"
    ws["G6"], ws["H6"] = "Current PEO", "Paychex"

    # Row 12 headers
    base_hdrs = ["#", "First Name", "Last Name", "Gender", "DOB", "ZIP", "Member Type", "Medical Coverage", "COBRA", "Medical Plan Name"]
    all_hdrs = base_hdrs + list(C.MARSH_COMPARISON_HEADERS)
    for col_i, h in enumerate(all_hdrs, start=1):
        ws.cell(12, col_i, h)

    # 6 employees + 1 dependent + 1 invoice-only row
    emps = [
        ("ALICE", "ADAMS", "F", date(1980, 1, 1), "28277", "EE", "EF", "N", "111995001", 2500.00),
        ("BOB", "BAKER", "M", date(1981, 2, 2), "28277", "EE", "EE", "N", "111995002", 900.00),
        ("CAROL", "CLARK", "F", date(1982, 3, 3), "28277", "EE", "EE", "N", "111995003", 950.00),
        ("DAVE", "DAVIS", "M", date(1983, 4, 4), "28277", "EE", "EE", "N", "111995004", 1000.00),
        ("EVE", "EVANS", "F", date(1984, 5, 5), "28277", "EE", "EE", "N", "111995005", 1050.00),
        ("FRANK", "FORD", "M", date(1985, 6, 6), "28277", "EE", "WO", "N", "111995006", None),
    ]
    r = 14
    for idx, (fn, ln, g, dob, zp, role, cov, cob, sub_id, prem) in enumerate(emps, start=1):
        plan_v = None if (blank_plans or cov == "WO") else "Blue Options PPO 1500"
        ws.cell(r, 1, idx)
        ws.cell(r, 2, fn)
        ws.cell(r, 3, ln)
        ws.cell(r, 4, g)
        ws.cell(r, 5, dob)
        ws.cell(r, 6, zp)
        ws.cell(r, 7, role)
        ws.cell(r, 8, cov)
        ws.cell(r, 9, cob)
        ws.cell(r, 10, plan_v)
        ws.cell(r, 11, f"{ln}, {fn}")
        ws.cell(r, 12, 1)
        ws.cell(r, 13, sub_id if corrupt_group_col else "14179816")
        ws.cell(r, 14, plan_v)
        ws.cell(r, 15, cov if cov != "WO" else None)
        ws.cell(r, 16, "A")
        ws.cell(r, 17, plan_v)
        ws.cell(r, 18, prem)
        ws.cell(r, 20, "Match")
        ws.cell(r, 22, None if corrupt_group_col else sub_id)
        r += 1
        if idx == 1:
            # Dependent of Alice
            ws.cell(r, 1, 2)
            ws.cell(r, 2, "SAM")
            ws.cell(r, 3, "ADAMS")
            ws.cell(r, 4, "M")
            ws.cell(r, 5, date(1981, 7, 7))
            ws.cell(r, 6, "28277")
            ws.cell(r, 7, "SP")
            ws.cell(r, 8, None if blank_dep_cov else "EF")
            ws.cell(r, 9, None if blank_dep_cov else "N")
            ws.cell(r, 10, None if (blank_dep_cov or blank_plans) else "Blue Options PPO 1500")
            ws.cell(r, 20, "Dependent")
            r += 1

    # Blank separator + invoice-only row
    r += 1
    if leak_inv_only:
        ws.cell(r, 2, "GHOST")
        ws.cell(r, 3, "USER")
    ws.cell(r, 11, "USER, GHOST")
    ws.cell(r, 13, "111995999" if corrupt_group_col else "14179816")
    ws.cell(r, 15, "EE")
    ws.cell(r, 18, 1200.00)
    ws.cell(r, 20, "Invoice Employee Not Found In Census")
    ws.cell(r, 22, None if corrupt_group_col else "111995999")

    # Summary sheet
    ws_s = wb.create_sheet("Summary")
    ws_s.cell(1, 1, "Acme Apparel, LLC - Census vs. Invoice Summary (BCBSNC)")
    ws_s.cell(3, 1, "Census Employees (full-time, in output)")
    ws_s.cell(3, 2, 6)
    ws_s.cell(4, 1, "Added Invoice Employees")
    ws_s.cell(4, 2, 1 if stale_added_count else 0)
    ws_s.cell(5, 1, "Invoice-Only Discrepancies (Not Added to Census)")
    ws_s.cell(5, 2, 1)
    ws_s.cell(7, 1, "Premium Change Analysis")
    if corrupt_peo:
        ws_s.cell(8, 1, "Not applicable - renewal not required (prospect not currently with a PEO); renewal rates not populated.")
    else:
        ws_s.cell(8, 1, "Not applicable - renewal not required (current PEO Paychex; renewal date 04/01/2027 is more than 90 days out); renewal rates not populated.")

    wb.save(path)
    wb.close()
    return path


def test_deterministic_auditor_catches_all_injected_defects_and_auto_patches_peo(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_DISABLE_WORKBOOK_AUDITOR", "1")
    wb_path = _make_marsh_wb(
        tmp_path / "corrupt.xlsx",
        corrupt_peo=True,
        corrupt_group_col=True,
        blank_plans=True,
        blank_dep_cov=True,
        leak_inv_only=True,
        stale_added_count=True,
    )
    prospect = Prospect(company="Acme Apparel, LLC", peo_name="Paychex", is_peo=True, renewal_date=date(2027, 4, 1))
    med_sbc = RawDoc(path=Path("BCBS_Blue Options PPO 1500.pdf"), ext=".pdf", text="Blue Options Benefit Highlights")
    dental_sbc = RawDoc(path=Path("BCBS_Dental 2026.pdf"), ext=".pdf", text="Dental Blue")
    inv_doc = InvoiceDoc(path="inv.xls", records=[], group_number="14179816", total_due=8000.00)
    F = Findings(invoice_total_balance_due=7600.00)

    snap = build_workbook_audit_snapshot(
        wb_path,
        roles={"plan_design": [med_sbc, dental_sbc]},
        prospect=prospect,
        selected_invoices=[inv_doc],
        findings=F,
    )
    findings, patches = run_deterministic_output_audit(
        snap,
        roles={"plan_design": [med_sbc, dental_sbc]},
        prospect=prospect,
        selected_invoices=[inv_doc],
        findings=F,
    )
    codes = {f.code for f in findings}
    assert "PEO_RENEWAL_CONTRADICTION" in codes
    assert "GROUP_NO_VS_SUB_ID_MISMATCH" in codes
    assert "BLANK_MEDICAL_PLAN_WITH_SINGLE_SBC" in codes
    assert "BLANK_DEPENDENT_COVERAGE_MARSH" in codes
    assert "INVOICE_ONLY_LEAKED_INTO_CENSUS_COLS" in codes
    assert "BALANCE_DUE_VARIANCE" in codes
    assert len(patches) == 1

    # Full orchestrator auto-patches Summary!A8 PEO contradiction and writes QA Audit Flags without touching Cols A-J
    report = audit_generated_deliverables(
        wb_path,
        roles={"plan_design": [med_sbc, dental_sbc]},
        prospect=prospect,
        selected_invoices=[inv_doc],
        findings=F,
        apply_patches=True,
    )
    assert not report.passed
    assert any(p.applied for p in report.patches_applied)
    wb_after = openpyxl.load_workbook(wb_path)
    assert "current PEO Paychex" in wb_after["Summary"]["A8"].value
    assert "Post-Generation QA Audit Flags" in [wb_after["Summary"].cell(r, 1).value for r in range(1, wb_after["Summary"].max_row + 1)]
    # Verify Cols A-J on Row 14 were NEVER mutated
    assert wb_after["Group Info + Census"]["B14"].value == "ALICE"
    wb_after.close()


def test_clean_workbook_passes_auditor_without_modifying_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_DISABLE_WORKBOOK_AUDITOR", "1")
    wb_path = _make_marsh_wb(tmp_path / "clean.xlsx")
    prospect = Prospect(company="Acme Apparel, LLC", peo_name="Paychex", is_peo=True, renewal_date=date(2027, 4, 1))
    inv_doc = InvoiceDoc(path="inv.xls", records=[], group_number="14179816", total_due=7600.00)
    F = Findings(invoice_total_balance_due=7600.00)

    report = audit_generated_deliverables(
        wb_path,
        prospect=prospect,
        selected_invoices=[inv_doc],
        findings=F,
        apply_patches=True,
    )
    assert report.passed is True
    assert report.overall_status == "passed"
    assert report.findings == []
    wb_after = openpyxl.load_workbook(wb_path)
    assert "Post-Generation QA Audit Flags" not in [
        wb_after["Summary"].cell(r, 1).value for r in range(1, wb_after["Summary"].max_row + 1)
    ]
    wb_after.close()


def test_llm_auditor_filters_ungrounded_findings_and_surfaces_grounded_ones(tmp_path, monkeypatch):
    wb_path = _make_marsh_wb(tmp_path / "llm_check.xlsx")
    monkeypatch.delenv("CENSUS_DISABLE_WORKBOOK_AUDITOR", raising=False)
    monkeypatch.setattr("workbook_auditor._auditor_llm_enabled", lambda: True)

    fake_llm_resp = {
        "overall_status": "warnings",
        "confidence_summary": "1 grounded semantic defect found.",
        "findings": [
            {
                "severity": "high",
                "category": "non_medical_leakage_or_erasure",
                "output_location": "Group Info + Census Row 19 (FRANK FORD)",
                "source_evidence": "Invoice lists FRANK FORD with $0.00 medical and $42.32 dental/vision (SubID 111995006)",
                "summary": "Waived employee billed for ancillary coverage is missing ancillary explanation note.",
                "recommended_fix": "Add note confirming $0.00 medical and $42.32 dental/vision.",
            },
            {
                # Ungrounded hallucination (empty source_evidence) -> MUST be filtered out!
                "severity": "critical",
                "category": "other",
                "output_location": "Summary",
                "source_evidence": "",
                "summary": "Vague ungrounded complaint",
                "recommended_fix": "",
            },
        ],
        "safe_summary_patches": [],
    }
    monkeypatch.setattr("llm_client.extract_json", lambda *a, **kw: fake_llm_resp)

    report = audit_generated_deliverables(wb_path, apply_patches=True)
    assert report.llm_executed is True
    assert len(report.findings) == 1
    assert report.findings[0].source == "llm"
    assert report.findings[0].severity == "high"
    assert "FRANK FORD" in report.findings[0].output_location
    assert "POST-GENERATION WORKBOOK AUDIT" in report.format_text()
    assert _validate_audit_dict({"findings": [{"severity": "high", "output_location": "", "source_evidence": "x", "summary": "y"}]})["findings"] == []
