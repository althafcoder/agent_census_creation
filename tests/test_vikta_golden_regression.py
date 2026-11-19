"""End-to-End Vikta Energy Technologies LLC Golden Regression Test (Fix #7).

Verifies all 6 client feedback items together through the real pipeline modules
(census builder, invoice parser, renewal parser, population sanity gate, plan linker,
comparator, email evidence extractor, Marsh + SBU workbook writers, and Missing Information Email):

1. Census population: exactly 42 employees and 26 dependents (6 spouses, 20 children),
   0 unassigned dependents, passes Population Sanity Gate cleanly against RFP stated 42.
2. Demographics integrity: every census record retains its source First Name, Last Name,
   DOB, Gender, and ZIP — zero false missing-name or missing-DOB findings.
3. Invoice matching: Kathleen Barraza, Carson Blair, Mason Bowdy, Stephen Johnston,
   Zoe Kasper, Joseph Valdez, Kristian Ventura, Sarah Vroman, Tamir Young Jordan all match
   `premiumBill.pdf` cleanly with zero false 'not on invoice' exceptions; exact $60,283.73 total.
4. Renewal mapping: `0004-HMO` -> `S9N3ADT` and `0006-PPO` -> `P621CHC` (plus `0002-PPO` -> `G653CHC`,
   `0003-PPO` -> `G9L5CHC`, `0005-PPO` -> `G650CHC`) link dynamically with ZERO renewal mapping failures.
5. SBC detection: all 6 SBCs (`P621CHC`, `G9L5CHC`, `G650CHC`, `G653CHC`, `S9N3ADT`, `G9K5ADT`,
   including the 2 rotated/reversed PDFs) are classified as `plan_design` and matched — ZERO SBC request
   in the Missing Information Email.
6. Invoice-only employees: Diego Zubieta is the SOLE genuine invoice-only case; submission email
   termination evidence (effective 11/6/2026) appears in comparator notes and Missing Information Email;
   he is NOT added to the SBU census workbook and has blank underwriting columns in Marsh.
"""
from datetime import date
from pathlib import Path
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
from email_evidence import extract_email_evidence  # noqa: E402
from invoice_parser import combine_records, parse_invoice, select_invoices  # noqa: E402
from models import Findings, Prospect  # noqa: E402
from population_sanity import evaluate_population_sanity  # noqa: E402
from readers import RawDoc, RawTable, read_file  # noqa: E402
from renewal_parser import parse_renewal  # noqa: E402
from router import RoutingDecision, build_missing_information_email_parts, format_missing_information_email_text  # noqa: E402
from workbook_writer import save_marsh_workbook, save_sbu_workbook  # noqa: E402

EXTRACTED = ROOT / "extracted_text"
CENSUS_FIXTURE = ROOT / "tests" / "fixtures" / "coded_relationship_census.xlsx"
VIKTA_EMAIL_FIXTURE = ROOT / "tests" / "fixtures" / "vikta_original_email.txt"


def _required_files_exist() -> bool:
    return all([
        CENSUS_FIXTURE.exists(),
        (EXTRACTED / "premiumBill_extracted.txt").exists(),
        (EXTRACTED / "BCBS_RENEWAL_extracted.txt").exists(),
        VIKTA_EMAIL_FIXTURE.exists(),
        (EXTRACTED / "P621CHC_extracted.txt").exists(),
    ])


@pytest.mark.skipif(not _required_files_exist(), reason="Vikta extracted fixtures not present")
def test_vikta_end_to_end_golden_regression(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_DISABLE_LLM_VALUE_MAP", "1")
    monkeypatch.setattr("census_config.LLM_FIRST", False)
    monkeypatch.setattr("census_config.USE_LLM_FALLBACK", False)

    # 1. Load source documents
    census_doc = read_file(CENSUS_FIXTURE)
    inv_doc_raw = RawDoc(
        path=Path("premiumBill.pdf"),
        ext=".pdf",
        text=(EXTRACTED / "premiumBill_extracted.txt").read_text(encoding="utf-8", errors="ignore"),
    )
    ren_doc_raw = RawDoc(
        path=Path("BCBS RENEWAL.pdf"),
        ext=".pdf",
        text=(EXTRACTED / "BCBS_RENEWAL_extracted.txt").read_text(encoding="utf-8", errors="ignore"),
    )
    email_doc_raw = RawDoc(
        path=Path("Original_Email.pdf"),
        ext=".pdf",
        text=VIKTA_EMAIL_FIXTURE.read_text(encoding="utf-8", errors="ignore"),
    )
    rfp_df = pd.DataFrame([
        ["State", "Code", "# of Employees", "Gross Annual Payroll"],
        ["Texas", "", 28, 0],
        ["California", "", 7, ""],
        ["Kentucky", "", 2, ""],
        ["Nevada", "", 2, ""],
        ["Arizona", "", 3, ""],
        ["Totals", "", 42, 0],
    ])
    rfp_doc_raw = RawDoc(
        path=Path("PEO RFP - Vikta Energy Technologies LLC.xlsx"),
        ext=".xlsx",
        tables=[RawTable("Summary", rfp_df)],
        text=(EXTRACTED / "PEO_RFP_-_Vikta_Energy_Technologies_LLC_extracted.txt").read_text(
            encoding="utf-8", errors="ignore"
        ) if (EXTRACTED / "PEO_RFP_-_Vikta_Energy_Technologies_LLC_extracted.txt").exists() else "",
    )

    sbc_docs = []
    for pid in ("P621CHC", "G9L5CHC", "G650CHC", "G653CHC", "S9N3ADT", "G9K5ADT"):
        txt = (EXTRACTED / f"{pid}_extracted.txt").read_text(encoding="utf-8", errors="ignore")
        d = RawDoc(path=Path(f"{pid}.pdf"), ext=".pdf", text=txt)
        assert classify(d) == "plan_design", f"SBC {pid}.pdf failed classification"
        sbc_docs.append(d)

    all_docs = [census_doc, inv_doc_raw, ren_doc_raw, email_doc_raw, rfp_doc_raw] + sbc_docs
    roles = {
        "census": [census_doc],
        "invoice": [inv_doc_raw],
        "renewal": [ren_doc_raw],
        "rfp": [email_doc_raw, rfp_doc_raw],
        "ghq": [],
        "plan_design": sbc_docs,
    }

    prospect = Prospect(
        company="Vikta Energy Technologies LLC",
        state="TX",
        effective_date=date(2027, 1, 1),
        stated_total_employees=42,
        stated_total_source="PEO RFP - Vikta Energy Technologies LLC.xlsx",
    )
    routing = RoutingDecision(carrier=C.CARRIER_CIGNA, state="TX", is_florida=False)

    # 2. Build census + parse invoice + parse renewal
    census, meta = build_census([census_doc], effective_date=prospect.effective_date)
    inv_parsed = parse_invoice(inv_doc_raw)
    selected_invs, _, period = select_invoices([inv_parsed])
    invoice_records = combine_records(selected_invs)
    refine_non_enrolled_statuses(census, reference_date=selected_invs[0].period_start, effective_date=prospect.effective_date)
    renewal = parse_renewal(ren_doc_raw)

    # 3. Verify population & sanity gate (Feedback #1 & #2)
    emps = [p for p in census.people if p.role == "EE" and not p.invoice_only]
    deps = [p for p in census.people if p.role in ("SP", "CH", "DP")]
    assert len(emps) == 42
    assert len(deps) == 26
    assert sum(1 for d in deps if d.role == "SP") == 6
    assert sum(1 for d in deps if d.role == "CH") == 20
    assert not census.unassigned_dependents
    assert all(p.first and p.last and p.dob and p.gender and p.zip for p in emps + deps)

    sanity = evaluate_population_sanity(
        census, prospect=prospect, invoice_records=invoice_records, docs=all_docs, roles=roles,
    )
    assert sanity.passed and not sanity.blocked, f"Sanity gate unexpectedly blocked: {sanity.issues}"

    # 4. Run comparison with email evidence + dynamic plan linking (Feedback #3, #4, #6)
    meta["email_evidence"] = extract_email_evidence(all_docs, roles=roles)
    F = Findings()
    results = run_comparison(
        census, invoice_records, renewal, meta, F,
        have_invoice=True, renewal_required=True,
    )

    # Feedback #3: zero false 'not on invoice' for employees clearly present on premiumBill.pdf
    emp_by_name = {(e.first.lower(), e.last.lower()): e for e in emps}
    for first_l, last_l in [
        ("kathleen", "barraza"),
        ("carson", "blair"),
        ("mason", "bowdy"),
        ("stephen", "johnston"),
        ("zoe", "kasper"),
        ("joseph", "valdez"),
        ("kristian", "ventura"),
        ("sarah", "vroman"),
        ("tamir", "young jordan"),
    ]:
        emp = emp_by_name.get((first_l, last_l))
        assert emp is not None, f"Missing expected employee {first_l} {last_l} from census"
        rr = results[id(emp)]
        assert rr.inv is not None, f"False 'not on invoice' for {first_l} {last_l}: {rr.result}"
        assert rr.result != "Not On Invoice"

    # Feedback #4: ZERO renewal mapping failures across all billing codes (0004-HMO, 0006-PPO, etc.)
    assert not F.renewal_unmapped_plans, f"Unexpected renewal mapping failures: {F.renewal_unmapped_plans}"
    assert not any(cat == "Renewal mapping failures" for cat, _, _ in F.exceptions)
    assert F.renewal_rates.get(("0006-PPO", "EE")) == pytest.approx(1173.52)
    assert F.renewal_rates.get(("0004-HMO", "ES")) == pytest.approx(1123.12)

    # Feedback #6: Diego Zubieta is the SOLE genuine invoice-only case, with 11/6/2026 termination evidence
    inv_only = [p for p in census.people if p.invoice_only]
    assert len(inv_only) == 1, f"Expected only Diego Zubieta as invoice-only, got {[p.display for p in inv_only]}"
    assert inv_only[0].first.lower() == "diego" and inv_only[0].last.lower() == "zubieta"
    z_notes = " ".join(n[0] for n in results[id(inv_only[0])].notes)
    assert "11/6/2026" in z_notes and "terminated" in z_notes.lower()

    # 5. Feedback #5 & #6 in Missing Information Email: ZERO SBC request + Zubieta termination noted
    subj, sections = build_missing_information_email_parts(
        prospect=prospect, routing=routing, findings=F, census=census,
        have_census=True, have_invoice=True, have_renewal=True, renewal_required=True,
        claims_required=False, ghq_present=False, roles=roles,
        selected_invoices=selected_invs, invoice_records=invoice_records,
        results=results, docs=all_docs,
    )
    email_txt = format_missing_information_email_text(subj, sections)
    assert not any(t == "SBC" for t, _ in sections), f"False SBC request in email:\n{email_txt}"
    assert "ZUBIETA" in email_txt.upper() and "11/6/2026" in email_txt

    # 6. Verify Marsh & SBU workbooks do NOT add Diego Zubieta into confirmed census records
    if C.MARSH_TEMPLATE_PATH.exists() and C.SBU_TEMPLATE_PATH.exists():
        marsh_out = tmp_path / "Marsh_Vikta.xlsx"
        sbu_out = tmp_path / "SBU_Vikta.xlsx"
        audit = {"src_employees": 42, "src_dependents": 26, "out_employees": 42, "out_dependents": 26, "missing_employees": 0}
        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, marsh_out, census, prospect, results, F, audit)
        save_sbu_workbook(C.SBU_TEMPLATE_PATH, sbu_out, census, prospect)

        # SBU census (population-only) must contain ONLY the 42 + 26 = 68 confirmed census rows, NOT Zubieta
        wb_sbu = openpyxl.load_workbook(sbu_out, data_only=True)
        ws_sbu = wb_sbu["Census"]
        sbu_names = [
            (ws_sbu.cell(r, 1).value, ws_sbu.cell(r, 2).value)
            for r in range(C.SBU_FIRST_DATA_ROW, ws_sbu.max_row + 1)
            if ws_sbu.cell(r, 1).value or ws_sbu.cell(r, 2).value
        ]
        assert len(sbu_names) == 68
        assert not any((ln or "").lower() == "zubieta" for _, ln in sbu_names)

        # Marsh census must keep confirmed employee count = 42 and keep ALL 10 underwriting cols (A-J) blank for Zubieta
        wb_m = openpyxl.load_workbook(marsh_out, data_only=True)
        ws_m = wb_m.active
        assert ws_m["E5"].value == 42
        z_rows = [
            r for r in range(C.MARSH_FIRST_DATA_ROW, ws_m.max_row + 1)
            if "zubieta" in str(ws_m.cell(r, C.MARSH_COMPARISON_START_COL).value or "").lower()
        ]
        assert len(z_rows) == 1
        zr = z_rows[0]
        assert all(ws_m.cell(zr, c).value is None for c in range(1, 11))  # ALL census cols A-J blank
        assert F.invoice_retro_adjustments == pytest.approx(2307.00)
        assert F.invoice_total_balance_due == pytest.approx(62590.73)
        assert F.counters.get("name_mismatch", 0) == 0
