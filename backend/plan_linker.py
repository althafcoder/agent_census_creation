"""
Dynamic Evidence-Based Plan Linker (Fix #4).

Links invoice billing group codes / plan strings (e.g. ``0006-PPO``, ``0004-HMO``)
to renewal plan keys (e.g. ``P621CHC``, ``S9N3ADT``) using a transparent, zero-hardcoding
evidence hierarchy:

  1. Direct name / plan-ID / carrier-mapping match (``direct_name_match``)
  2. Cross-employee census-plan consensus across matched (employee, invoice) pairs
     (``census_plan_consensus``)
  3. Exact invoice-premium-to-renewal-rate fingerprint matching against renewal and
     current composite rate tables, with network-type compatibility (``premium_rate_match``)
  4. Single-plan renewal fallback (``single_renewal_plan``)

Every resolved link records the method and human-readable audit evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import logging
import re
from typing import Any, Optional

from census_config import ACTIVE_TIERS
from utils import clean, match_plan

log = logging.getLogger("census.plan_linker")

_NET_TYPE_RE = re.compile(r"\b(PPO|HMO|EPO|POS|HDHP|HSA|INDEMNITY)\b", re.I)


@dataclass
class PlanLink:
    source_plan: str
    renewal_plan: str
    method: str
    evidence: str


def _network_types(text: str) -> set[str]:
    if not text:
        return set()
    return {m.upper() for m in _NET_TYPE_RE.findall(str(text))}


def _networks_compatible(a_text: str, b_text: str) -> bool:
    na, nb = _network_types(a_text), _network_types(b_text)
    if not na or not nb:
        return True
    return bool(na & nb)


def _match_plan_against_renewal(plan_text: str, renewal: Any) -> Optional[str]:
    """Match a plan string (invoice or census) against ``renewal.plans`` and ``renewal.plan_names``."""
    p = clean(plan_text)
    if not p or not renewal or not getattr(renewal, "plans", None):
        return None

    # 1. Explicit carrier mapping on the renewal document itself
    pmap = getattr(renewal, "plan_mapping", None) or {}
    if p in pmap and pmap[p] in renewal.plans:
        return pmap[p]

    # 2. Standard canonical/code matcher against renewal.plans
    rp = match_plan(p, renewal.plans)
    if rp:
        return rp

    # 3. Whole-word plan-ID containment (e.g. "Blue Cross Blue Shield of Texas - P621CHC PPO" -> "P621CHC")
    p_upper = p.upper()
    id_hits = [
        cand for cand in renewal.plans
        if len(cand.strip()) >= 4
        and re.search(rf"(?<![A-Z0-9]){re.escape(cand.strip().upper())}(?![A-Z0-9])", p_upper)
    ]
    if len(id_hits) == 1:
        return id_hits[0]

    # 4. Match against descriptive plan names (e.g. "Blue Choice Platinum PPO 811" -> "P621CHC")
    pnames = getattr(renewal, "plan_names", None) or {}
    if pnames:
        desc_to_id = {desc: pid for pid, desc in pnames.items() if desc and pid in renewal.plans}
        if desc_to_id:
            desc_hit = match_plan(p, list(desc_to_id.keys()))
            if desc_hit:
                return desc_to_id[desc_hit]

    return None


def _rate_matches_plan(
    rp: str,
    observed_tier_rates: dict[str, list[float]],
    renewal: Any,
    tolerance: float = 0.05,
) -> tuple[int, int, list[str]]:
    """Return ``(exact_hits, mismatches, matched_descriptions)`` comparing observed invoice
    (tier -> premiums) against ``renewal.rates`` and ``renewal.current_rates`` for plan ``rp``."""
    ren_rates = getattr(renewal, "rates", None) or {}
    cur_rates = getattr(renewal, "current_rates", None) or {}
    hits = 0
    mismatches = 0
    descs: list[str] = []

    for tier, prems in observed_tier_rates.items():
        if tier not in ACTIVE_TIERS or not prems:
            continue
        r_amt = ren_rates.get((rp, tier))
        c_amt = cur_rates.get((rp, tier))
        if r_amt is None and c_amt is None:
            continue
        # Compare modal observed premium for this tier
        modal_p = Counter(round(x, 2) for x in prems).most_common(1)[0][0]
        matched_this = False
        if r_amt is not None and abs(modal_p - r_amt) <= tolerance:
            hits += 1
            descs.append(f"{tier} ${modal_p:,.2f}==renewal ${r_amt:,.2f}")
            matched_this = True
        elif c_amt is not None and abs(modal_p - c_amt) <= tolerance:
            hits += 1
            descs.append(f"{tier} ${modal_p:,.2f}==current ${c_amt:,.2f}")
            matched_this = True
        if not matched_this:
            mismatches += 1
    return hits, mismatches, descs


def match_by_premium_fingerprint(
    inv_plan: str,
    observed_tier_rates: dict[str, list[float]],
    renewal: Any,
    tolerance: float = 0.05,
) -> Optional[PlanLink]:
    """Find a unique renewal plan matching the observed invoice (tier, premium) fingerprint."""
    if not renewal or not getattr(renewal, "plans", None) or not observed_tier_rates:
        return None
    pnames = getattr(renewal, "plan_names", None) or {}
    candidates: list[tuple[int, int, str, list[str]]] = []

    for rp in renewal.plans:
        rp_desc = f"{rp} {pnames.get(rp, '')}"
        if not _networks_compatible(inv_plan, rp_desc):
            continue
        hits, mismatches, descs = _rate_matches_plan(rp, observed_tier_rates, renewal, tolerance=tolerance)
        if hits >= 1 and mismatches == 0:
            net_bonus = 1 if (_network_types(inv_plan) & _network_types(rp_desc)) else 0
            candidates.append((hits, net_bonus, rp, descs))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if len(candidates) == 1 or (candidates[0][0], candidates[0][1]) > (candidates[1][0], candidates[1][1]):
        _, _, best_rp, descs = candidates[0]
        return PlanLink(
            source_plan=inv_plan,
            renewal_plan=best_rp,
            method="premium_rate_match",
            evidence=f"exact rate match ({', '.join(descs)})",
        )
    return None


def build_plan_link_table(
    renewal: Any,
    emps: list[Any],
    invs: list[Any],
    matches: dict[int, tuple[int, float]],
    inv_rates: Optional[dict[tuple[str, str], float]] = None,
    blueprint_links: Optional[list[Any]] = None,
) -> dict[str, PlanLink]:
    """Build a submission-wide mapping ``{invoice_plan_string: PlanLink}`` using dynamic evidence.

    Order of evidence per invoice plan:
      1. Direct name / plan-ID / carrier plan-mapping match
      2. Census plan consensus across matched (employee, invoice) rows
      3. Exact invoice-to-renewal premium rate match (with network-type compatibility)
      4. Single-plan renewal fallback
    """
    if not renewal or not getattr(renewal, "plans", None):
        return {}

    pnames = getattr(renewal, "plan_names", None) or {}
    inv_plans = sorted({clean(r.plan) for r in (invs or []) if clean(getattr(r, "plan", ""))})
    if not inv_plans:
        return {}

    # Collect observed (tier -> [premiums]) per invoice plan
    obs_rates: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (p, t), amt in (inv_rates or {}).items():
        if clean(p) and t in ACTIVE_TIERS and amt is not None:
            obs_rates[clean(p)][t].append(float(amt))
    for r in (invs or []):
        rp_str = clean(getattr(r, "plan", ""))
        t = clean(getattr(r, "tier", "")).upper()
        prem = getattr(r, "premium", None)
        if rp_str and t in ACTIVE_TIERS and prem is not None and prem > 0:
            obs_rates[rp_str][t].append(float(prem))

    # Collect census-plan votes per invoice plan from matched (employee, invoice) pairs
    census_votes: dict[str, Counter] = defaultdict(Counter)
    for emp_idx, (inv_idx, _sim) in (matches or {}).items():
        if emp_idx >= len(emps) or inv_idx >= len(invs):
            continue
        inv_p = clean(getattr(invs[inv_idx], "plan", ""))
        emp_p = clean(getattr(emps[emp_idx], "plan", ""))
        if not inv_p or not emp_p:
            continue
        resolved = _match_plan_against_renewal(emp_p, renewal)
        if resolved:
            census_votes[inv_p][resolved] += 1

    links: dict[str, PlanLink] = {}
    for inv_p in inv_plans:
        # 1. Direct name / plan ID match
        direct = _match_plan_against_renewal(inv_p, renewal)
        if direct:
            links[inv_p] = PlanLink(
                source_plan=inv_p,
                renewal_plan=direct,
                method="direct_name_match",
                evidence=f"direct match '{inv_p}' -> '{direct}'",
            )
            continue

        # 2. Cross-employee census plan consensus
        votes = census_votes.get(inv_p)
        if votes:
            total_votes = sum(votes.values())
            top_rp, top_cnt = votes.most_common(1)[0]
            if top_cnt / total_votes >= 0.60:
                # Check if premium fingerprint corroborates or conflicts
                hits, mismatches, descs = _rate_matches_plan(top_rp, obs_rates.get(inv_p, {}), renewal)
                alt_rate_link = None
                if mismatches > 0 and hits == 0:
                    alt_rate_link = match_by_premium_fingerprint(inv_p, obs_rates.get(inv_p, {}), renewal)
                if alt_rate_link is None:
                    ev = f"census plan consensus ({top_cnt}/{total_votes} matched employees -> '{top_rp}')"
                    if descs:
                        ev += f"; corroborated by rate ({', '.join(descs)})"
                    links[inv_p] = PlanLink(
                        source_plan=inv_p,
                        renewal_plan=top_rp,
                        method="census_plan_consensus",
                        evidence=ev,
                    )
                    continue

        # 3. Exact premium-rate fingerprint match
        rate_link = match_by_premium_fingerprint(inv_p, obs_rates.get(inv_p, {}), renewal)
        if rate_link:
            links[inv_p] = rate_link
            continue

        # 3b. Whole-Submission LLM Blueprint cross-document plan link (verified against renewal.plans)
        if blueprint_links:
            bp_hit = None
            for bpl in blueprint_links:
                if clean(getattr(bpl, "invoice_plan", "")).lower() == inv_p.lower():
                    cand_rp = _match_plan_against_renewal(getattr(bpl, "canonical_plan_id", ""), renewal)
                    if cand_rp:
                        bp_hit = (cand_rp, clean(getattr(bpl, "evidence", "")) or "whole-submission blueprint")
                        break
            if bp_hit:
                links[inv_p] = PlanLink(
                    source_plan=inv_p,
                    renewal_plan=bp_hit[0],
                    method="blueprint_plan_link",
                    evidence=bp_hit[1],
                )
                continue

        # 4. Single-plan renewal fallback
        if len(renewal.plans) == 1:
            only_rp = renewal.plans[0]
            if _networks_compatible(inv_p, f"{only_rp} {pnames.get(only_rp, '')}"):
                links[inv_p] = PlanLink(
                    source_plan=inv_p,
                    renewal_plan=only_rp,
                    method="single_renewal_plan",
                    evidence=f"only renewal plan available ('{only_rp}')",
                )

    for inv_p, lnk in links.items():
        if lnk.method != "direct_name_match":
            log.info("Linked invoice plan '%s' -> renewal plan '%s' [%s]: %s",
                     inv_p, lnk.renewal_plan, lnk.method, lnk.evidence)
    return links
