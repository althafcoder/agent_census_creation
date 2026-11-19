"""Tests for Fix #4: Dynamic Evidence-Based Plan Linking & Composite Renewal Parsing.

Verifies:
1. Composite 'Plan ID' Current/Renewal rate block parsing on real BCBS_RENEWAL_extracted.txt
   (extracts all 6 plans x 4 tiers + plan name directory with zero truncation).
2. Evidence-based plan linking:
   - Tier 1: direct name / plan-ID match
   - Tier 2: cross-employee census plan consensus (e.g. '0006-PPO' <-> 'P621CHC')
   - Tier 3: exact premium-rate fingerprint match even with NO census plan names
     (matching either renewal or current composite rates + network-type compatibility)
3. End-to-end `run_comparison` resolves renewal rates for `0004-HMO` and `0006-PPO`
   (including invoice-only subscribers) with zero `Renewal mapping failures`.
"""
from datetime import date
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from comparator import run_comparison  # noqa: E402
from models import Census, Findings, InvoiceRecord, Person, RenewalDoc  # noqa: E402
from plan_linker import build_plan_link_table, match_by_premium_fingerprint  # noqa: E402
from readers import RawDoc  # noqa: E402
from renewal_parser import _parse_composite_renewal_blocks, parse_renewal  # noqa: E402

BCBS_REN_TXT = ROOT / "extracted_text" / "BCBS_RENEWAL_extracted.txt"


@pytest.mark.skipif(not BCBS_REN_TXT.exists(), reason="BCBS renewal extracted text missing")
def test_composite_renewal_parser_extracts_all_bcbs_plans():
    raw_text = BCBS_REN_TXT.read_text(encoding="utf-8", errors="ignore")
    ren_rates, cur_rates, pmap, pnames = _parse_composite_renewal_blocks(raw_text)

    for pid in ("P621CHC", "S9N3ADT", "G653CHC", "G9L5CHC", "G650CHC", "G9K5ADT"):
        for tier in ("EE", "ES", "EC", "FAM"):
            assert (pid, tier) in ren_rates, f"missing renewal rate {(pid, tier)}"
            assert (pid, tier) in cur_rates, f"missing current rate {(pid, tier)}"
        assert pid in pnames, f"missing plan name directory entry for {pid}"

    assert ren_rates[("P621CHC", "EE")] == pytest.approx(1173.52)
    assert ren_rates[("P621CHC", "FAM")] == pytest.approx(3520.56)
    assert cur_rates[("P621CHC", "EE")] == pytest.approx(809.86)
    assert ren_rates[("S9N3ADT", "EE")] == pytest.approx(561.56)
    assert ren_rates[("S9N3ADT", "ES")] == pytest.approx(1123.12)
    assert "PPO" in pnames["P621CHC"]
    assert "HMO" in pnames["S9N3ADT"]


def _sample_renewal() -> RenewalDoc:
    return RenewalDoc(
        path="BCBS RENEWAL.pdf",
        effective=date(2026, 8, 1),
        rates={
            ("P621CHC", "EE"): 1173.52, ("P621CHC", "ES"): 2347.04,
            ("P621CHC", "EC"): 2347.04, ("P621CHC", "FAM"): 3520.56,
            ("S9N3ADT", "EE"): 561.56, ("S9N3ADT", "ES"): 1123.12,
            ("S9N3ADT", "EC"): 1123.12, ("S9N3ADT", "FAM"): 1684.68,
            ("G653CHC", "EE"): 981.00, ("G653CHC", "EC"): 1962.00,
        },
        current_rates={
            ("P621CHC", "EE"): 809.86, ("P621CHC", "FAM"): 2429.58,
            ("S9N3ADT", "EE"): 407.99, ("S9N3ADT", "ES"): 815.98,
        },
        plans=["G653CHC", "P621CHC", "S9N3ADT"],
        plan_names={
            "P621CHC": "Blue Choice Platinum PPO 811",
            "S9N3ADT": "Blue Advantage Silver HMO 402",
            "G653CHC": "Blue Choice Gold PPO 822",
        },
    )


def test_plan_linker_via_pure_premium_fingerprint_without_census_plans():
    """Even with NO census plan names, exact premium + network links 0006-PPO and 0004-HMO."""
    ren = _sample_renewal()
    l_ppo = match_by_premium_fingerprint("0006-PPO", {"EE": [1173.52], "FAM": [3520.56]}, ren)
    assert l_ppo is not None
    assert l_ppo.renewal_plan == "P621CHC"
    assert l_ppo.method == "premium_rate_match"

    l_hmo = match_by_premium_fingerprint("0004-HMO", {"ES": [1123.12]}, ren)
    assert l_hmo is not None
    assert l_hmo.renewal_plan == "S9N3ADT"
    assert l_hmo.method == "premium_rate_match"

    # Also matches against current_rates (pre-renewal invoice)
    l_curr = match_by_premium_fingerprint("BILL-A PPO", {"EE": [809.86]}, ren)
    assert l_curr is not None and l_curr.renewal_plan == "P621CHC"


def test_plan_linker_respects_network_compatibility():
    """An HMO billing code must never falsely link to a PPO plan even if a rate collided."""
    ren = RenewalDoc(
        path="ren.pdf",
        rates={("PLAN_PPO", "EE"): 1000.00, ("PLAN_HMO", "EE"): 1000.00},
        plans=["PLAN_PPO", "PLAN_HMO"],
        plan_names={"PLAN_PPO": "Gold PPO", "PLAN_HMO": "Silver HMO"},
    )
    l_hmo = match_by_premium_fingerprint("0004-HMO", {"EE": [1000.00]}, ren)
    assert l_hmo is not None and l_hmo.renewal_plan == "PLAN_HMO"
    l_ppo = match_by_premium_fingerprint("0006-PPO", {"EE": [1000.00]}, ren)
    assert l_ppo is not None and l_ppo.renewal_plan == "PLAN_PPO"


def test_comparator_resolves_billing_codes_with_zero_renewal_mapping_failures():
    ren = _sample_renewal()
    c = Census()
    e1 = Person("EE", "Kathleen", "Barraza", coverage="EE",
                plan="Blue Cross Blue Shield of Texas - P621CHC PPO", dob=date(1998, 3, 31))
    e2 = Person("EE", "Stephen", "Johnston", coverage="ES",
                plan="Blue Cross Blue Shield of Texas - S9N3ADT HMO", dob=date(1991, 3, 1))
    c.people.extend([e1, e2])

    invs = [
        InvoiceRecord(first="Kathleen", last="Barraza", raw_name="BARRAZA , KATHLEEN",
                      plan="0006-PPO", tier="EE", premium=1173.52),
        InvoiceRecord(first="Stephen", last="Johnston", raw_name="JOHNSTON , STEPHEN",
                      plan="0004-HMO", tier="ES", premium=1123.12),
        # Invoice-only employee on 0006-PPO (must also resolve cleanly via submission-level plan link)
        InvoiceRecord(first="Diego", last="Zubieta", raw_name="ZUBIETA , DIEGO",
                      plan="0006-PPO", tier="EE", premium=1173.52),
    ]
    F = Findings()
    results = run_comparison(c, invs, ren, {"saw_dependent_source": False}, F,
                             have_invoice=True, renewal_required=True)

    assert not F.renewal_unmapped_plans, f"unexpected unmapped plans: {F.renewal_unmapped_plans}"
    assert not any(cat == "Renewal mapping failures" for cat, _, _ in F.exceptions)
    assert results[id(e1)].renewal_premium == pytest.approx(1173.52)
    assert results[id(e2)].renewal_premium == pytest.approx(1123.12)
    inv_only = [p for p in c.people if p.invoice_only][0]
    assert results[id(inv_only)].renewal_premium == pytest.approx(1173.52)
    assert F.renewal_rates.get(("0006-PPO", "EE")) == pytest.approx(1173.52)
    assert F.renewal_rates.get(("0004-HMO", "ES")) == pytest.approx(1123.12)
