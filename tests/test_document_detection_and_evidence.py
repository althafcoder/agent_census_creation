"""Tests for Fix #5:
  5A. Content-based SBC recognition (plan-ID-named PDFs like P621CHC.pdf, G9L5CHC.pdf, S9N3ADT.pdf)
      and accurate SBC-to-plan matching in Missing Information Email (zero false SBC requests).
  5B. Generic PDF faux-bold overprint character deduplication (SSPPSS++ -> SPS+ -> FAM,
      SSUUBB -> SUB -> EE, CCHH11++ -> CH1+ -> EC, SSPPSS -> SPS -> ES) + BCBS SUBSCRIBER FEES parsing.
  5C. Dynamic client/broker email evidence extraction (e.g. Diego Zubieta terminated effective 11/6/2026)
      flowing into comparator notes/exceptions and Missing Information Email.
"""
from datetime import date
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from classifier import classify, is_sbc_document  # noqa: E402
from comparator import run_comparison  # noqa: E402
from email_evidence import extract_email_evidence, find_person_evidence  # noqa: E402
from invoice_parser import _records_from_carrier_statement  # noqa: E402
from models import Census, Findings, InvoiceRecord, Person, Prospect  # noqa: E402
from readers import RawDoc  # noqa: E402
from router import RoutingDecision, build_missing_information_email_parts  # noqa: E402
from utils import clean_overprint_text, deduplicate_overprint_token, std_coverage  # noqa: E402

EXTRACTED = ROOT / "extracted_text"


# ---- 5B: Overprint dedup & SUBSCRIBER FEES parsing --------------------------
@pytest.mark.parametrize("raw,cleaned,tier", [
    ("SSPPSS++", "SPS+", "FAM"),
    ("SSUUBB", "SUB", "EE"),
    ("CCHH11++", "CH1+", "EC"),
    ("SSPPSS", "SPS", "ES"),
    ("SUB", "SUB", "EE"),
    ("SPS+", "SPS+", "FAM"),
    ("CH1+", "CH1+", "EC"),
    ("SPS", "SPS", "ES"),
])
def test_overprint_deduplication_and_tier_mapping(raw, cleaned, tier):
    assert deduplicate_overprint_token(raw) == cleaned
    assert std_coverage(raw) == tier


def test_overprint_dedup_leaves_normal_tokens_untouched():
    for tok in ("EE", "0001", "0000", "1111", "0006-PPO", "LOMMANSON", "AARON", "BOOKKEEPER"):
        assert deduplicate_overprint_token(tok) == tok
    assert clean_overprint_text("000822137241 DALLEFELD , THOMAS 0001 0006-PPO SSPPSS++ 3,520.56") == (
        "000822137241 DALLEFELD , THOMAS 0001 0006-PPO SPS+ 3,520.56"
    )


@pytest.mark.skipif(not (EXTRACTED / "premiumBill_extracted.txt").exists(), reason="premiumBill text missing")
def test_subscriber_fees_invoice_parses_all_vikta_subscribers_to_exact_cent():
    raw_text = (EXTRACTED / "premiumBill_extracted.txt").read_text(encoding="utf-8", errors="ignore")
    doc = RawDoc(path=Path("premiumBill.pdf"), ext=".pdf", text=raw_text)
    recs, meta = _records_from_carrier_statement(doc)

    # Exact control-total match against printed TOTAL FEES CURRENT CHARGES ($60,283.73)
    assert float(meta["current_charges"]) == pytest.approx(60283.73)
    assert round(sum(r.premium for r in recs), 2) == pytest.approx(60283.73)

    by_last = {(r.first.lower(), r.last.lower()): r for r in recs}
    # Spot-check every employee called out in user feedback + overprint-affected tiers
    assert ("kathleen", "barraza") in by_last and by_last[("kathleen", "barraza")].tier == "EE"
    assert ("carson", "blair") in by_last and by_last[("carson", "blair")].tier == "FAM"
    assert ("thomas", "dallefeld") in by_last and by_last[("thomas", "dallefeld")].tier == "FAM"
    assert ("micah", "forgey") in by_last and by_last[("micah", "forgey")].tier == "EE"
    assert ("stephen", "johnston") in by_last and by_last[("stephen", "johnston")].tier == "ES"
    assert ("amanda", "lommanson foltz") in by_last and by_last[("amanda", "lommanson foltz")].tier == "EC"
    assert ("daniel", "swadley") in by_last and by_last[("daniel", "swadley")].tier == "FAM"
    assert ("diego", "zubieta") in by_last and by_last[("diego", "zubieta")].tier == "EE"
    # Michael Davis zero-current product-change reversal line excluded; active 0003-PPO kept
    davis_recs = [r for r in recs if r.last.lower() == "davis"]
    assert len(davis_recs) == 1 and davis_recs[0].plan == "0003-PPO" and davis_recs[0].premium == pytest.approx(1943.66)


# ---- 5A: Content-based SBC recognition & zero false SBC requests ------------
@pytest.mark.skipif(not (EXTRACTED / "P621CHC_extracted.txt").exists(), reason="P621CHC SBC text missing")
def test_plan_id_named_sbc_classified_and_matched_without_false_email_request(monkeypatch):
    monkeypatch.setattr("census_config.LLM_FIRST", False)
    monkeypatch.setattr("census_config.USE_LLM_FALLBACK", False)

    sbc_names = ["P621CHC", "G9L5CHC", "G650CHC", "G653CHC", "S9N3ADT", "G9K5ADT"]
    sbc_docs = []
    for pid in sbc_names:
        p = EXTRACTED / f"{pid}_extracted.txt"
        txt = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else (
            f"Summary of Benefits and Coverage: What this Plan Covers & What You Pay\n{pid} Plan\nCoverage Period: 01/01/2026 - 12/31/2026"
        )
        d = RawDoc(path=Path(f"{pid}.pdf"), ext=".pdf", text=txt)
        assert is_sbc_document(d)
        assert classify(d) == "plan_design"
        sbc_docs.append(d)

    # An email merely mentioning "Summary of Benefits and Coverage" in prose must NOT be hijacked as an SBC
    email_doc = RawDoc(
        path=Path("Original_Email.pdf"),
        ext=".pdf",
        text="Good morning, please find attached the Summary of Benefits and Coverage documents for your review.",
    )
    assert not is_sbc_document(email_doc)

    # Build a census using all active Vikta plan descriptions and confirm ZERO SBCs are requested
    c = Census()
    for idx, plan_str in enumerate([
        "Blue Cross Blue Shield of Texas - P621CHC PPO",
        "Blue Cross Blue Shield of Texas - G9L5CHC PPO",
        "Blue Cross Blue Shield of Texas - G653CHC PPO",
        "Blue Cross Blue Shield of Texas - S9N3ADT HMO",
        "Blue Cross Blue Shield of Texas - G650CHC PPO",
        "Blue Cross Blue Shield of Texas - G9K5ADT HMO",
    ]):
        c.people.append(Person("EE", f"Emp{idx}", "Test", coverage="EE", plan=plan_str, dob=date(1990, 1, 1)))

    _, sections = build_missing_information_email_parts(
        prospect=Prospect(company="Vikta Energy Technologies LLC", state="TX"),
        routing=RoutingDecision(carrier="Cigna", state="TX", is_florida=False),
        findings=Findings(),
        census=c,
        have_census=True,
        have_invoice=True,
        have_renewal=True,
        renewal_required=True,
        claims_required=False,
        ghq_present=False,
        roles={"plan_design": sbc_docs},
        docs=sbc_docs,
    )
    sec_titles = [title for title, _ in sections]
    assert "SBC" not in sec_titles, f"False SBC request generated: {sections}"


# ---- 5C: Client/Broker Email Evidence Extraction ----------------------------
VIKTA_EMAIL = ROOT / "tests" / "fixtures" / "vikta_original_email.txt"


@pytest.mark.skipif(not VIKTA_EMAIL.exists(), reason="Vikta email fixture missing")
def test_email_evidence_extracts_termination_and_propagates_to_comparator_and_email():
    email_txt = VIKTA_EMAIL.read_text(encoding="utf-8", errors="ignore")
    email_doc = RawDoc(path=Path("Original_Email.pdf"), ext=".pdf", text=email_txt)

    evs = extract_email_evidence([email_doc], roles={"rfp": [email_doc]})
    ev = find_person_evidence(evs, "Diego", "Zubieta")
    assert ev is not None
    assert ev.event_type == "terminated"
    assert ev.effective_date == "11/6/2026"

    c = Census()
    c.people.append(Person("EE", "Kathleen", "Barraza", coverage="EE", dob=date(1998, 3, 31)))
    invs = [
        InvoiceRecord(first="Kathleen", last="Barraza", raw_name="BARRAZA, KATHLEEN",
                      plan="0006-PPO", tier="EE", premium=1173.52),
        InvoiceRecord(first="Diego", last="Zubieta", raw_name="ZUBIETA, DIEGO",
                      plan="0006-PPO", tier="EE", premium=1173.52),
    ]
    F = Findings()
    results = run_comparison(c, invs, None, {"saw_dependent_source": False, "email_evidence": evs},
                             F, have_invoice=True, renewal_required=False)

    inv_only = [p for p in c.people if p.invoice_only]
    assert len(inv_only) == 1 and inv_only[0].last == "Zubieta"
    rr = results[id(inv_only[0])]
    notes_joined = " ".join(n[0] for n in rr.notes)
    assert "11/6/2026" in notes_joined
    assert "terminated" in notes_joined.lower()

    _, sections = build_missing_information_email_parts(
        prospect=Prospect(company="Vikta Energy Technologies LLC", state="TX"),
        routing=RoutingDecision(carrier="Cigna", state="TX", is_florida=False),
        findings=F,
        census=c,
        have_census=True,
        have_invoice=True,
        have_renewal=False,
        renewal_required=False,
        claims_required=False,
        ghq_present=False,
        roles={"rfp": [email_doc]},
        docs=[email_doc],
    )
    comp_items = [item for title, items in sections if "Comparison" in title for item in items]
    assert any("ZUBIETA" in it.upper() and "11/6/2026" in it and "not been added to the census" in it for it in comp_items), comp_items
