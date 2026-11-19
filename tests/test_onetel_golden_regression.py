"""End-to-End OneTel Security LLC Regression Test (Copilot Parity).

Verifies all OneTel findings dynamically with zero hardcoding:
1. Population & Tiers: 23 employees + 36 dependents = 59 lives; tiers derived from dependents (EE=10, ES=1, EC=2, EF=10).
2. Residential ZIP Code column mapped for all 59 rows.
3. Clean HQ Address & City/State/ZIP (no pipe-delimited table artifacts).
4. 'Current PEO: NA' normalized to not-with-PEO -> renewal_required = False (no false PEO renewal request).
5. Employee Benefits Booklet is NOT treated as an official carrier SBC -> SBC requested in Missing Info Email.
6. No false 'Legal Entity Mismatch: Address:' clarification.
7. Explicit SP=Y without spouse row (Kelsey Kieffer, Kylee Kieffer) flagged in Missing Info Email.
8. Marsh Cols A-J match 59 rows cleanly (and Plan Col J stays blank since per-employee elections are unstated).
"""
from pathlib import Path
import sys

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import census_config as C  # noqa: E402
from census_builder import build_census, refine_non_enrolled_statuses  # noqa: E402
from clarifications import legal_entity_mismatch_items  # noqa: E402
from models import Findings  # noqa: E402
from prospect import build_prospect  # noqa: E402
from readers import read_all  # noqa: E402
from router import (  # noqa: E402
    build_missing_information_email_parts,
    check_renewal_required,
    determine_routing,
)
from workbook_writer import save_marsh_workbook, save_sbu_workbook  # noqa: E402

ONETEL_INPUT = ROOT / "New Opp - Fw New Benefit Opp Submission OneTel Security LLC_20261118-024921_618429" / "Input"


@pytest.mark.skipif(not ONETEL_INPUT.exists(), reason="OneTel input fixtures not present")
def test_onetel_security_end_to_end_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("CENSUS_DISABLE_LLM_VALUE_MAP", "1")
    monkeypatch.setenv("CENSUS_DISABLE_AGENT_LOOP", "1")
    monkeypatch.setenv("CENSUS_DISABLE_BLUEPRINT", "1")
    monkeypatch.setenv("CENSUS_DISABLE_WORKBOOK_AUDITOR", "1")
    monkeypatch.setattr("census_config.LLM_FIRST", False)
    monkeypatch.setattr("census_config.USE_LLM_FALLBACK", False)

    docs = read_all(ONETEL_INPUT)
    cen_docs = [d for d in docs if d.ext in (".xls", ".xlsx")]
    email_docs = [d for d in docs if "email" in d.name.lower()]
    booklet_docs = [d for d in docs if "booklet" in d.name.lower()]
    assert cen_docs and email_docs and booklet_docs

    prospect, _, _ = build_prospect([], email_docs, all_docs=docs)
    assert prospect.state.upper() in ("UT", "UTAH")
    assert prospect.is_peo is False
    assert not prospect.peo_name
    assert "|" not in (prospect.address or "")
    assert "|" not in (prospect.city_state_zip or "")

    routing = determine_routing(prospect, docs=docs)
    assert routing.state == "UT"
    assert routing.carrier == C.CARRIER_CIGNA

    # No false Legal Entity Mismatch ("Address:")
    assert legal_entity_mismatch_items(prospect, roles={"census": cen_docs}, docs=docs) == []

    census, _ = build_census(cen_docs, effective_date=prospect.effective_date)
    refine_non_enrolled_statuses(census, reference_date=prospect.effective_date, docs=docs)

    emps = [p for p in census.people if p.role == "EE" and not p.invoice_only]
    deps = [p for p in census.people if p.role in ("SP", "CH", "DP")]
    assert len(emps) == 23
    assert len(deps) == 36
    assert all(p.zip for p in emps)

    tier_counts = {t: sum(1 for e in emps if e.coverage == t) for t in ("EE", "ES", "EC", "FAM")}
    assert tier_counts == {"EE": 10, "ES": 1, "EC": 2, "FAM": 10}

    ren_required, _ = check_renewal_required(prospect, docs=docs, proposed_effective=prospect.effective_date)
    assert ren_required is False

    F = Findings()
    F.renewal_required = ren_required
    subj, sections = build_missing_information_email_parts(
        prospect, routing, F, census,
        have_census=True, have_invoice=False, have_renewal=False,
        renewal_required=ren_required, claims_required=False,
        docs=docs,
    )
    email_txt = subj + "\n" + "\n".join(f"{s}\n" + "\n".join(items) for s, items in sections)
    assert "Summary of Benefits and Coverage" in email_txt
    assert "PEO renewal" not in email_txt.lower()
    assert "Address:" not in email_txt
    assert "Kelsey Kieffer" in email_txt and "Kylee Kieffer" in email_txt

    if C.MARSH_TEMPLATE_PATH.exists():
        out_m = tmp_path / "Marsh_OneTel.xlsx"
        audit = {"src_employees": 23, "src_dependents": 36, "out_employees": 23, "out_dependents": 36, "missing_employees": 0}
        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, out_m, census, prospect, {}, F, audit)
        wb = openpyxl.load_workbook(out_m, data_only=True)
        ws = wb["Group Info + Census"]
        non_empty_rows = [r for r in range(14, 90) if ws.cell(r, 2).value or ws.cell(r, 3).value]
        assert len(non_empty_rows) == 59
        assert all(not ws.cell(r, 10).value for r in non_empty_rows)
        assert all(ws.cell(r, 6).value for r in non_empty_rows)

    if C.SBU_TEMPLATE_PATH.exists():
        out_s = tmp_path / "SBU_OneTel.xlsx"
        save_sbu_workbook(C.SBU_TEMPLATE_PATH, out_s, census, prospect)
        wb_s = openpyxl.load_workbook(out_s, data_only=True)
        ws_s = wb_s["Census"]
        sbu_rows = [r for r in range(5, 80) if ws_s.cell(r, 1).value or ws_s.cell(r, 2).value]
        assert len(sbu_rows) == 59
