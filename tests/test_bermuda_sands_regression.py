"""End-to-End Bermuda Sands Apparel, LLC Regression Test.

Verifies all 8 reported issues are resolved dynamically with zero hardcoding:
1. Population: 14 employees and 6 dependents (2 SP, 4 CH), 0 unassigned.
2. Dependent coverage in Marsh: all 6 dependents carry subscriber's 'EF' coverage, 'N' COBRA, and medical plan name.
3. Medical-only premiums: 11 enrolled employees medical total == $15,569.86 (excluding Dental & Vision).
4. Populated details: 17 medical plan names (11 EEs + 6 deps), 11+ invoice tiers, 13 available subscriber IDs.
5. Invoice-only premiums: Jairo Garcia and Se Ka each == $1,627.23 medical-only (EE tier), NOT $1,669.55.
6. Renewal information: source renewal date == 04/01/2027 (never 01/01/2027 or hardcoded UHC description).
7. Balance due reconciliation: exact $19,511.64 (matching printed Total Amount Due on Subgroup).
8. Accurate audit count: Added Invoice Employees == 0, Invoice-Only Discrepancies (Not Added to Census) == 2.
"""
from datetime import date
from pathlib import Path
import re
import sys

import openpyxl
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import census_config as C  # noqa: E402
from census_builder import build_census, refine_non_enrolled_statuses  # noqa: E402
from classifier import classify  # noqa: E402
from comparator import run_comparison  # noqa: E402
from invoice_parser import combine_records, parse_invoice, select_invoices  # noqa: E402
from models import Findings  # noqa: E402
from prospect import build_prospect  # noqa: E402
from readers import RawDoc, RawTable  # noqa: E402
from router import build_missing_information_email_parts, check_renewal_required, determine_routing  # noqa: E402
from submission_blueprint import build_submission_digest  # noqa: E402
from workbook_writer import save_marsh_workbook  # noqa: E402

EXTRACTED = ROOT / "extracted_text"
BERMUDA_EMAIL_FIXTURE = ROOT / "tests" / "fixtures" / "bermuda_original_email.txt"


def _strip_banner(txt: str) -> str:
    m = "--- [EXTRACTED TEXT CONTENT] ---"
    i = txt.find(m)
    if i >= 0:
        txt = txt[i + len(m):].lstrip("\r\n")
    t = txt.find("--- [EXTRACTED TABLES")
    if t >= 0:
        txt = txt[:t].rstrip()
    return txt


def _pipe_table(txt: str) -> pd.DataFrame:
    rows = []
    for ln in txt.splitlines():
        if " | " in ln:
            rows.append([c.strip() for c in ln.split(" | ")])
    w = max(len(r) for r in rows)
    return pd.DataFrame([(r + [""] * w)[:w] for r in rows])


def _bermuda_files_exist() -> bool:
    return all([
        (EXTRACTED / "BSA__PairPEO_Employee_Census_extracted.txt").exists(),
        (EXTRACTED / "BCBS_BillingInvoice_11162026061319256_14179816_1001_extracted.txt").exists(),
        (EXTRACTED / "BCBS_Blue_Options_PPO_1500_extracted.txt").exists(),
        BERMUDA_EMAIL_FIXTURE.exists(),
    ])


@pytest.mark.skipif(not _bermuda_files_exist(), reason="Bermuda Sands extracted fixtures not present")
def test_bermuda_sands_all_eight_issues_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_DISABLE_LLM_VALUE_MAP", "1")
    monkeypatch.setattr("census_config.LLM_FIRST", False)
    monkeypatch.setattr("census_config.USE_LLM_FALLBACK", False)

    cen_txt = _strip_banner((EXTRACTED / "BSA__PairPEO_Employee_Census_extracted.txt").read_text(encoding="utf-8", errors="ignore"))
    inv_txt = _strip_banner((EXTRACTED / "BCBS_BillingInvoice_11162026061319256_14179816_1001_extracted.txt").read_text(encoding="utf-8", errors="ignore"))
    sbc_txt = _strip_banner((EXTRACTED / "BCBS_Blue_Options_PPO_1500_extracted.txt").read_text(encoding="utf-8", errors="ignore"))
    email_txt = _strip_banner(BERMUDA_EMAIL_FIXTURE.read_text(encoding="utf-8", errors="ignore"))

    cen_doc = RawDoc(path=Path("BSA _PairPEO Employee Census.xlsx"), ext=".xlsx", tables=[RawTable("Sheet1", _pipe_table(cen_txt))], text=cen_txt)
    inv_doc = RawDoc(path=Path("BCBS_BillingInvoice_11162026061319256_14179816_1001.xls"), ext=".xls", tables=[RawTable("Inv. Detail By Subscriber", _pipe_table(inv_txt))], text=inv_txt)
    sbc_doc = RawDoc(path=Path("BCBS_Blue Options PPO 1500.pdf"), ext=".pdf", text=sbc_txt)
    email_doc = RawDoc(path=Path("Original_Email.pdf"), ext=".pdf", text=email_txt)

    # Blueprint digest must scan past the 12-row Company Info banner and include header Row 12 + sample rows!
    digest = build_submission_digest([cen_doc, inv_doc, sbc_doc, email_doc])
    assert "Enrollee" in digest and "Spouse of Employee" in digest

    # Prospect & renewal date (Issue #6)
    prospect, _, _ = build_prospect([], [email_doc], all_docs=[cen_doc, inv_doc, sbc_doc, email_doc])
    assert prospect.company == "Bermuda Sands Apparel, LLC"
    assert prospect.renewal_date == date(2027, 4, 1)
    routing = determine_routing(prospect, [cen_doc, inv_doc, sbc_doc, email_doc])
    assert routing.carrier == C.CARRIER_CIGNA

    # Census population (Issue #1: 14 EEs, 6 Deps)
    census, meta = build_census([cen_doc], effective_date=prospect.effective_date)
    inv_parsed = parse_invoice(inv_doc)
    selected, _, period = select_invoices([inv_parsed])
    invoice_records = combine_records(selected)
    refine_non_enrolled_statuses(census, reference_date=selected[0].period_start, effective_date=prospect.effective_date)

    emps = [p for p in census.people if p.role == "EE" and not p.invoice_only]
    deps = [p for p in census.people if p.role in ("SP", "CH", "DP")]
    assert len(emps) == 14, f"Expected 14 employees, got {len(emps)}"
    assert len(deps) == 6, f"Expected 6 dependents, got {len(deps)}"
    assert sum(1 for d in deps if d.role == "SP") == 2
    assert sum(1 for d in deps if d.role == "CH") == 4
    assert not census.unassigned_dependents

    # Single-medical-SBC plan name backfill — verify underscore-prefixed Dental/Vision PDFs are excluded
    _non_med_doc_re = re.compile(
        r"(?:^|[^a-z0-9])(dental|vision|vsp|eyemed|life|ad&?d|std|ltd|accident|critical|hospital|ancillary)(?:$|[^a-z0-9])",
        re.I,
    )
    dental_doc = RawDoc(path=Path("BCBS_Dental 2026.pdf"), ext=".pdf", text="Dental Blue 2026 Benefit Summary")
    vision_doc = RawDoc(path=Path("BCBS_Vision.pdf"), ext=".pdf", text="Blue 20/20 Vision Benefit Summary")
    med_sbcs = [d for d in [sbc_doc, dental_doc, vision_doc] if not _non_med_doc_re.search(d.name or "")]
    assert len(med_sbcs) == 1 and med_sbcs[0].name == "BCBS_Blue Options PPO 1500.pdf"
    raw_stem = med_sbcs[0].path.stem
    if "_" in raw_stem:
        _pfx, _rest = raw_stem.split("_", 1)
        if len(_pfx.strip()) <= 8 and len(_rest.strip().split()) >= 2:
            raw_stem = _rest
    sbc_title = re.sub(r"[_\s]+", " ", raw_stem).strip()
    assert sbc_title == "Blue Options PPO 1500"
    for e in emps:
        if e.coverage in C.ACTIVE_TIERS and not e.plan:
            e.plan = sbc_title
            for d in e.dependents:
                d.plan = sbc_title
    for r_inv in invoice_records:
        if not r_inv.plan:
            r_inv.plan = sbc_title

    # Medical-only invoice records + ancillary-only record (Rosa Orellana) + group number + non-medical breakdown
    assert len(invoice_records) == 13  # 11 matched + 2 invoice-only; Rosa Orellana ($0.00 medical) kept in non_medical_records
    assert selected[0].group_number == "14179816"
    assert len(selected[0].non_medical_records) == 1
    assert selected[0].non_medical_records[0].sub_id == "111995154"
    assert selected[0].non_medical_records[0].ancillary_premium == pytest.approx(42.32)
    assert selected[0].non_medical_charges == pytest.approx(784.82)
    assert selected[0].gross_total_premium == pytest.approx(19609.14)
    assert round(sum(r.premium for r in invoice_records), 2) == pytest.approx(18824.32)
    assert float(selected[0].total_due) == pytest.approx(19511.64)

    # Simulate Current PEO = Paychex (from Original_Email.pdf) to confirm renewal note never contradicts H6
    prospect.peo_name = "Paychex"
    prospect.is_peo = True

    ren_required, ren_reason = check_renewal_required(
        prospect, renewal_doc=None, docs=[cen_doc, inv_doc, sbc_doc, email_doc],
        proposed_effective=prospect.effective_date, known_renewal_date=prospect.renewal_date,
    )
    assert ren_required is False

    F = Findings()
    F.renewal_reason = ren_reason
    meta["printed_invoice_total_due"] = float(selected[0].total_due)
    meta["invoice_non_medical_charges"] = float(selected[0].non_medical_charges)
    meta["invoice_non_medical_breakdown"] = dict(selected[0].non_medical_breakdown)
    meta["invoice_gross_total_premium"] = float(selected[0].gross_total_premium)
    meta["non_medical_invoice_records"] = list(selected[0].non_medical_records)
    results = run_comparison(census, invoice_records, None, meta, F, have_invoice=True, renewal_required=ren_required)

    # Rosa Orellana: matched on invoice with $0 medical + $42.32 dental/vision (Sub ID 111995154), NOT "Not on invoice"
    rosa = next(e for e in emps if e.last.upper() == "ORELLANA")
    rr_rosa = results[id(rosa)]
    assert rr_rosa.inv is not None and rr_rosa.inv.sub_id == "111995154"
    assert rr_rosa.group_no == "14179816"
    assert rr_rosa.status != "Not on invoice"
    assert rr_rosa.inv_premium is None
    assert any("$0.00 medical" in n[0] for n in rr_rosa.notes)

    # Missing Information Email: no false SBC or Renewal request, lists both invoice-only employees
    email_subj, email_sections = build_missing_information_email_parts(
        prospect, routing, F, census,
        have_census=True, have_invoice=True, have_renewal=False,
        renewal_required=ren_required, claims_required=False,
        selected_invoices=selected, invoice_records=invoice_records,
        results=results, docs=[cen_doc, inv_doc, sbc_doc, email_doc],
    )
    email_out = email_subj + "\n" + "\n".join(
        f"{sec}\n" + "\n".join(items) for sec, items in email_sections
    )
    assert "Bermuda Sands Apparel, LLC" in email_subj
    assert "Summary of Benefits and Coverage" not in email_out
    assert "renewal documentation" not in email_out.lower()
    assert "GARCIA, JAIRO" in email_out and "KA, SE" in email_out

    # Issue #3: 11 enrolled employees medical total == $15,569.86
    assert F.census_matched_premium == pytest.approx(15569.86)
    # Issue #5: Jairo Garcia & Se Ka each == $1,627.23 medical-only
    inv_only = [p for p in census.people if p.invoice_only]
    assert len(inv_only) == 2
    assert {p.last.upper() for p in inv_only} == {"GARCIA", "KA"}
    for p in inv_only:
        assert results[id(p)].inv_premium == pytest.approx(1627.23)
        assert results[id(p)].tier == "EE"
    # Transparent reconciliation: $18,824.32 + $784.82 - $97.50 == $19,511.64
    assert F.invoice_current_charges == pytest.approx(18824.32)
    assert F.invoice_non_medical_charges == pytest.approx(784.82)
    assert F.invoice_unexplained_adjustment == pytest.approx(-97.50)
    assert F.invoice_total_balance_due == pytest.approx(19511.64)

    # Verify Marsh workbook output
    if C.MARSH_TEMPLATE_PATH.exists():
        out_wb = tmp_path / "Marsh_Bermuda.xlsx"
        audit = {"src_employees": 14, "src_dependents": 6, "out_employees": 14, "out_dependents": 6, "missing_employees": 0}
        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, out_wb, census, prospect, results, F, audit)
        wb = openpyxl.load_workbook(out_wb, data_only=True)
        ws = wb["Group Info + Census"]

        # Col M header is "Invoice Group #" (all 14 invoice-matched rows == "14179816"), Col V is "Invoice Subscriber ID" (14 IDs incl. 111995154)
        assert ws.cell(12, 13).value == "Invoice Group #"
        assert ws.cell(12, 22).value == "Invoice Subscriber ID"
        grp_vals = [ws.cell(r, 13).value for r in range(14, 45) if ws.cell(r, 13).value]
        assert len(grp_vals) == 14 and set(grp_vals) == {"14179816"}
        sub_ids = [ws.cell(r, 22).value for r in range(14, 45) if ws.cell(r, 22).value]
        assert len(sub_ids) == 14 and "111995154" in sub_ids

        # All 6 dependents have 'EF' coverage and 'Blue Options PPO 1500'
        dep_rows = [r for r in range(14, 40) if ws.cell(r, 7).value in ("SP", "CH")]
        assert len(dep_rows) == 6
        for dr in dep_rows:
            assert ws.cell(dr, 8).value == "EF"
            assert ws.cell(dr, 9).value == "N"
            assert ws.cell(dr, 10).value == "Blue Options PPO 1500"

        # 17 medical plan names (11 enrolled EEs + 6 deps), 13+ invoice tiers
        plan_populated = [r for r in range(14, 40) if ws.cell(r, 10).value == "Blue Options PPO 1500"]
        assert len(plan_populated) == 17
        inv_tiers = [r for r in range(14, 45) if ws.cell(r, 15).value]
        assert len(inv_tiers) >= 13

        # Summary Sheet: renewal date, PEO consistency, transparent $784.82 / -$97.50 reconciliation, audit counts
        ws_sum = wb["Summary"]
        sum_kv = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(1, ws_sum.max_row + 1) if ws_sum.cell(r, 1).value}
        sum_lines = [str(ws_sum.cell(r, 1).value) for r in range(1, ws_sum.max_row + 1) if ws_sum.cell(r, 1).value]
        ren_val = str(sum_kv.get("Renewal Effective Date") or "")
        assert "04/01/2027" in ren_val
        assert "UHC" not in ren_val
        assert not any("prospect not currently with a peo" in ln.lower() for ln in sum_lines)
        assert any("paychex" in ln.lower() and "more than 90 days out" in ln.lower() for ln in sum_lines)
        assert any("784.82" in str(v) or (isinstance(v, (int, float)) and abs(v - 784.82) < 0.01) for v in sum_kv.values())
        assert any((isinstance(v, (int, float)) and abs(v - (-97.50)) < 0.01) for v in sum_kv.values())
        assert sum_kv.get("Added Invoice Employees") == 0
        assert sum_kv.get("Invoice-Only Discrepancies (Not Added to Census)") == 2
        plan_notes = [ln for ln in sum_lines if "Census-matched" in ln and "premium =" in ln]
        assert any("$15,569.86" in ln for ln in plan_notes)
        assert not any("reported term" in ln.lower() for ln in plan_notes)
        diff_lines = [ln for ln in sum_lines if ln.startswith("Difference (")]
        assert any("GARCIA, JAIRO" in ln and "KA, SE" in ln for ln in diff_lines)
