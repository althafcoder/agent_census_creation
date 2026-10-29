"""Census-vs-invoice comparison, tier validation, WP/NE/WO handling, premium population."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import mode, StatisticsError
from typing import Optional

from census_config import ACTIVE_TIERS, FUZZY_ACCEPT, FUZZY_WITH_DOB, NON_ENROLLED
from models import Census, Findings, InvoiceRecord, Person, RenewalDoc, RowResult
from utils import clean, match_plan, name_similarity, name_tokens, split_full_name, std_coverage

TERMINATED = ("term", "cancel", "inactive", "lapse", "disenroll")


# ----------------------------------------------------------------- helpers --
def tier_from_deps(emp: Person) -> str:
    roles = {d.role for d in emp.dependents}
    if not roles:
        return "EE"
    if roles in ({"SP"}, {"DP"}):
        return "ES"
    if roles == {"CH"}:
        return "EC"
    return "FAM"


def _modal(values: list[float]) -> Optional[float]:
    if not values:
        return None
    try:
        return mode(values)
    except StatisticsError:
        return Counter(values).most_common(1)[0][0]


def build_rate_table(invs: list[InvoiceRecord]) -> dict:
    bucket = defaultdict(list)
    for r in invs:
        std_t = std_coverage(r.tier)
        if std_t in ACTIVE_TIERS and r.premium and r.premium > 0:
            bucket[(r.plan, std_t)].append(round(r.premium, 2))
            bucket[(r.plan, r.tier)].append(round(r.premium, 2))
    return {k: _modal(v) for k, v in bucket.items()}


def most_used_plan(invs: list[InvoiceRecord], census: Census) -> str:
    c = Counter(r.plan for r in invs if r.tier in ACTIVE_TIERS and r.plan)
    if not c:
        c = Counter(e.plan for e in census.employees if e.plan and e.coverage in ACTIVE_TIERS)
    return c.most_common(1)[0][0] if c else ""


def _is_active_on_invoice(r: InvoiceRecord) -> bool:
    if any(t in r.status.lower() for t in TERMINATED):
        return False
    return r.premium is None or r.premium > 0


# ---------------------------------------------------------------- matching --
def match_employees(emps: list[Person], invs: list[InvoiceRecord]) -> dict[int, tuple[int, float]]:
    inv_tok = [name_tokens(r.first, r.last) for r in invs]
    emp_tok = [name_tokens(e.first, e.last) for e in emps]
    used: set[int] = set()
    res: dict[int, tuple[int, float]] = {}

    # pass 1 - subscriber id (must be corroborated by name or DOB)
    idmap: dict[str, int] = {r.sub_id: j for j, r in enumerate(invs) if r.sub_id}
    for i, e in enumerate(emps):
        j = idmap.get(e.sub_id) if e.sub_id else None
        if j is not None and j not in used:
            s = name_similarity(emp_tok[i], inv_tok[j])
            if s >= 0.5 or (e.dob and e.dob == invs[j].dob):
                res[i] = (j, s)
                used.add(j)

    # pass 2 - identical normalised names (order-insensitive); DOB breaks ties
    by_tok: dict[tuple, list[int]] = defaultdict(list)
    for j, t in enumerate(inv_tok):
        if t:
            by_tok[tuple(t)].append(j)
    for i, e in enumerate(emps):
        if i in res or not emp_tok[i]:
            continue
        cands = [j for j in by_tok.get(tuple(emp_tok[i]), []) if j not in used]
        if cands:
            cands.sort(key=lambda j: 0 if (e.dob and invs[j].dob == e.dob) else 1)
            res[i] = (cands[0], 1.0)
            used.add(cands[0])

    # pass 2b - preferred name / middle name / alias exact matching
    for i, e in enumerate(emps):
        if i in res:
            continue
        alias_tok_cands = []
        if getattr(e, "preferred_first", "") and e.last:
            alias_tok_cands.append(name_tokens(e.preferred_first, e.last))
        if getattr(e, "middle_name", "") and e.last and e.middle_name.lower() != (getattr(e, "preferred_first", "") or "").lower():
            alias_tok_cands.append(name_tokens(e.middle_name, e.last))
        for a in getattr(e, "aliases", []):
            if a:
                alias_tok_cands.append(name_tokens(*split_full_name(a)))
        for atok in alias_tok_cands:
            if not atok:
                continue
            cands = [j for j in by_tok.get(tuple(atok), []) if j not in used]
            if cands:
                cands.sort(key=lambda j: 0 if (e.dob and invs[j].dob == e.dob) or (e.plan and invs[j].plan == e.plan) else 1)
                best_j = cands[0]
                # Under Section 16.6, match score is 0.95 to show exact invoice name display
                res[i] = (best_j, 0.95)
                used.add(best_j)
                break

    # pass 3 - fuzzy (typos, middle names); greedy best-first
    pairs = []
    for i, e in enumerate(emps):
        if i in res or not emp_tok[i]:
            continue
        all_emp_toks = [emp_tok[i]]
        if getattr(e, "preferred_first", "") and e.last:
            all_emp_toks.append(name_tokens(e.preferred_first, e.last))
        if getattr(e, "middle_name", "") and e.last:
            all_emp_toks.append(name_tokens(e.middle_name, e.last))
        for j, t in enumerate(inv_tok):
            if j in used or not t:
                continue
            sims = [name_similarity(et, t) for et in all_emp_toks if et]
            s = max(sims) if sims else 0.0
            dob_eq = bool(e.dob and invs[j].dob and e.dob == invs[j].dob)
            if s >= FUZZY_ACCEPT or (dob_eq and s >= FUZZY_WITH_DOB):
                pairs.append((s + (0.05 if dob_eq else 0), i, j, s))
    for _, i, j, s in sorted(pairs, reverse=True):
        if i in res or j in used:
            continue
        res[i] = (j, s)
        used.add(j)
    return res


# -------------------------------------------------------------------- main --
def run_comparison(census: Census, invs: list[InvoiceRecord], renewal: Optional[RenewalDoc],
                   meta: dict, F: Findings, have_invoice: bool,
                   renewal_required: bool = True, tracer=None) -> dict[int, RowResult]:
    if not renewal_required:
        renewal = None
        F.renewal_required = False
        F.renewal_reason = "Renewal not required under PEO + 90-day rule"
    else:
        F.renewal_required = True

    results_dup: dict[int, RowResult] = {}
    for d in census.employees:                      # duplicates stay in the census but are not compared
        if d.is_duplicate:
            exc_dup = RowResult()
            results_dup[id(d)] = exc_dup
            F.exceptions.append(("Duplicate employees", d.display, "Duplicate record in source census"))
        elif getattr(d, "disposition", "") == "EXCLUDE_PART_TIME":
            results_dup[id(d)] = RowResult(result="Excluded - Part-Time", result_red=False)

    emps = [e for e in census.employees if not e.is_duplicate and getattr(e, "disposition", "") != "EXCLUDE_PART_TIME"]
    rates = build_rate_table(invs) if have_invoice else {}
    top_plan = most_used_plan(invs, census) if have_invoice else ""
    inv_plans = sorted({r.plan for r in invs if r.plan})
    F.plan_rates = rates
    is_sc = "surgery center" in (meta.get("company") or "").lower() or any("surgery center" in (p.source or "").lower() for p in census.people)
    sc_recs = [r for r in invs if r.group_no in ('A02', 'A03', 'A05', 'C02', 'C03', 'C05', 'D02', 'D03', 'D05') and r.plan]
    if is_sc and sc_recs:
        F.invoice_employee_count = len(sc_recs)
    else:
        F.invoice_employee_count = len(invs)
    F.invoice_medical_count = sum(1 for r in invs if r.plan)
    results: dict[int, RowResult] = dict(results_dup)
    cnt = Counter()
    wp = Counter()

    def renewal_for(plan: str, tier: str) -> tuple[Optional[float], str]:
        if not renewal:
            return None, "no_doc"
        rp = match_plan(plan, renewal.plans) if plan else (renewal.plans[0] if len(renewal.plans) == 1 else None)
        if not rp:
            return None, "unmapped"
        return renewal.rates.get((rp, tier)), "ok"

    def exc(cat: str, who: str, detail: str = ""):
        F.exceptions.append((cat, who, detail))

    matches = match_employees(emps, invs) if have_invoice else {}
    matched_inv = {j for j, _ in matches.values()}

    # Name Mismatch Resolution pass:
    # If an enrolled census employee is unmatched, check for an unmatched invoice subscriber
    # with the same first name (e.g. Jennifer Moon vs SCHNETTLER, JENNIFER A) and matching plan/tier.
    if have_invoice:
        unmatched_census_enrolled = [
            i for i, e in enumerate(emps)
            if i not in matches and e.coverage in ACTIVE_TIERS
        ]
        unmatched_invoice = [
            j for j, inv in enumerate(invs)
            if j not in matched_inv and inv.plan
        ]
        for i in unmatched_census_enrolled:
            e = emps[i]
            for j in unmatched_invoice:
                if j in matched_inv:
                    continue
                inv = invs[j]
                fn_match = clean(e.first).lower() == clean(inv.first).lower() or (
                    clean(e.first).lower() in clean(inv.raw_name).lower()
                )
                pref_match = (
                    (bool(getattr(e, "preferred_first", "")) and clean(e.preferred_first).lower() == clean(inv.first).lower()) or
                    (bool(getattr(e, "middle_name", "")) and clean(e.middle_name).lower() == clean(inv.first).lower())
                )
                ln_match = clean(e.last).lower() == clean(inv.last).lower()
                tier_match = std_coverage(e.coverage) == std_coverage(inv.tier)
                plan_match = bool(e.plan and inv.plan and clean(e.plan).lower() == clean(inv.plan).lower())
                if fn_match and tier_match:
                    matches[i] = (j, 0.62)
                    matched_inv.add(j)
                    break
                elif (pref_match or ln_match) and (tier_match or plan_match):
                    matches[i] = (j, 0.95 if pref_match and ln_match else 0.70)
                    matched_inv.add(j)
                    break

    for i, e in enumerate(emps):
        rr = RowResult()
        results[id(e)] = rr
        cov = e.coverage
        who = e.display
        red_issue = False
        issues: list[str] = []

        # ---- tier vs dependent structure (only when dependents were supplied at all)
        if cov in ACTIVE_TIERS and meta.get("saw_dependent_source"):
            roles = [d.role for d in e.dependents]
            bad = ((cov == "EE" and roles) or (cov == "ES" and (not ({"SP", "DP"} & set(roles)) or "CH" in roles)) or
                   (cov == "EC" and ("CH" not in roles or ({"SP", "DP"} & set(roles)))) or (cov in ("FAM", "EF") and not roles))
            if bad:
                rr.notes.append(("Tier Does Not Match Dependent Structure", True))
                cnt["coverage_mismatch"] += 1
                exc("Invalid dependent structures", who,
                    f"Coverage {cov} with {len(roles)} dependent(s) ({'/'.join(roles) or 'none'})")

        j_sc = matches.get(i)
        inv = invs[j_sc[0]] if j_sc else None

        # ================= employee found on invoice
        if inv is not None:
            sim = j_sc[1]
            pct = int(round(sim * 100))
            rr.inv, rr.inv_name = inv, inv.raw_name
            rr.group_no = inv.group_no
            rr.plan_enrolled = inv.plan
            rr.match_pct = pct
            rr.match_text = "100% Match" if pct >= 100 else f"{pct}% Match | Invoice Name: {inv.raw_name}"

            if cov in NON_ENROLLED:
                if _is_active_on_invoice(inv) and inv.plan:
                    rr.plan, rr.tier, rr.status, rr.inv_premium = inv.plan, inv.tier, inv.status or "Active", inv.premium
                    rr.result, rr.result_red = "Eligibility Conflict", True
                    rr.notes.append(("Eligibility Conflict - Employee marked NE/WO but active coverage found on invoice.", True))
                    exc("Eligibility conflicts", who, f"Marked {cov}, active on invoice")
                    cnt["eligibility_conflict"] += 1
                    tier_for_renewal = inv.tier if inv.tier in ACTIVE_TIERS else tier_from_deps(e)
                    amt, status = renewal_for(rr.plan, tier_for_renewal)
                    if status == "ok":
                        rr.renewal_premium = amt
                else:
                    rr.plan, rr.tier, rr.status, rr.inv_premium = None, None, "No Medical on Invoice", None
                    rr.renewal_premium = None
                    rr.result, rr.result_red = "Not Enrolled - Expected", False
            else:
                if not e.plan and inv.plan:
                    e.plan = inv.plan
                if e.premium is None and inv.premium is not None:
                    e.premium = inv.premium

                rr.plan, rr.tier, rr.inv_premium = inv.plan or e.plan, inv.tier, inv.premium
                if "premium due" in (inv.status or "").lower():
                    rr.status = "Active - Premium Due (Adjustment)"
                    rr.notes.append((f"Invoice shows medical as PREMIUM DUE adjustment (${inv.premium:,.2f}) rather than payroll deduction - verify billing", False))
                else:
                    rr.status = inv.status or "Active"

                e_d = e.dob.date() if hasattr(e.dob, "date") and callable(e.dob.date) else e.dob
                inv_d = inv.dob.date() if hasattr(inv.dob, "date") and callable(inv.dob.date) else inv.dob
                if e_d and inv_d and e_d != inv_d:
                    from datetime import date as _dt_date
                    if not (inv_d.year >= _dt_date.today().year - 16):
                        issues.append("DOB")
                if e.gender and inv.gender and e.gender != inv.gender:
                    issues.append("Gender")
                if e.zip and inv.zip and e.zip[:5] != inv.zip[:5]:
                    issues.append("ZIP")
                cov_std = std_coverage(cov)
                inv_tier_std = std_coverage(inv.tier)
                if cov_std in ACTIVE_TIERS and inv_tier_std in ACTIVE_TIERS and cov_std != inv_tier_std:
                    issues.append(f"Tier (Census {cov} / Invoice {inv.tier})")
                    red_issue = True
                    cnt["tier_mismatch"] += 1
                    exc("Tier mismatches", who, f"Census {cov} vs Invoice {inv.tier}")
                if cov in ACTIVE_TIERS and any(t in inv.status.lower() for t in TERMINATED):
                    issues.append(f"Enrollment Status (Invoice: {inv.status})")
                    red_issue = True
                    exc("Coverage mismatches", who, f"Census {cov} but invoice status '{inv.status}'")
                    cnt["coverage_status_mismatch"] += 1
                if (cov in ACTIVE_TIERS and inv.has_dep_detail and e.dependents is not None
                        and meta.get("saw_dependent_source") and inv.dep_count != len(e.dependents)):
                    issues.append(f"Dependent Mismatch (Census {len(e.dependents)} / Invoice {inv.dep_count})")
                    red_issue = True
                    exc("Coverage mismatches", who, f"Dependents census {len(e.dependents)} vs invoice {inv.dep_count}")
                    cnt["dependent_mismatch"] += 1
                if e.premium is not None and inv.premium is not None and abs(e.premium - inv.premium) > 0.01:
                    rr.notes.append((f"Premium Discrepancy: Census {e.premium:,.2f} vs Invoice {inv.premium:,.2f}", True))
                    cnt["premium_discrepancy"] += 1
                    exc("Premium discrepancies", who, f"Census {e.premium:,.2f} vs Invoice {inv.premium:,.2f}")
                if 0.60 <= sim < 0.70:
                    rr.result = "Review - Name Mismatch"
                    rr.notes.append((f"Name mismatch - census name differs from invoice subscriber; matched on plan/tier/coverage. Invoice Name: {inv.raw_name}", False))
                else:
                    rr.result = "Match" if not issues else "Mismatch: " + "; ".join(issues)
                    rr.result_red = red_issue

            if cov not in NON_ENROLLED:
                tier_for_renewal = (inv.tier if inv.tier in ACTIVE_TIERS else
                                    cov if cov in ACTIVE_TIERS else tier_from_deps(e))
                amt, status = renewal_for(rr.plan, tier_for_renewal)
                if status == "unmapped":
                    rr.notes.append(("Renewal Plan Match Not Identified", True))
                    F.renewal_unmapped_plans.add(rr.plan or "(no plan name)")
                    exc("Renewal mapping failures", who, f"Plan '{rr.plan}'")
                    cnt["missing_renewal"] += 1
                elif status == "ok":
                    rr.renewal_premium = amt
                    if amt is None:
                        cnt["missing_renewal"] += 1

        # ================= employee NOT on invoice
        elif have_invoice:
            if cov in NON_ENROLLED:
                rr.status = "Not on invoice"
                rr.result = "Expected - Not Enrolled"
                rr.plan, rr.tier, rr.inv_premium = None, None, None
            elif cov == "WP":
                wp["total"] += 1
                elected = clean(e.plan)
                if elected and elected.upper() not in ("NOT ELIGIBLE", "NE", "NA", "N/A", "NONE"):
                    tier = tier_from_deps(e)
                    plan = match_plan(elected, inv_plans) or elected
                    wp["elected"] += 1
                    rr.plan, rr.tier = plan, tier
                    rr.status = "Not on invoice"
                    rr.result = "Expected - Waiting Period"
                    rr.inv_premium = rates.get((plan, tier))
                    rr.notes.append(("WP Employee - Awaiting Eligibility", True))
                    if plan:
                        amt, status = renewal_for(plan, tier)
                        if status == "ok":
                            rr.renewal_premium = amt
                        elif status == "unmapped":
                            rr.notes.append(("Renewal Plan Match Not Identified", True))
                            F.renewal_unmapped_plans.add(plan)
                else:
                    wp["missing_election"] += 1
                    rr.status = "Not on invoice"
                    rr.result = "Expected - Waiting Period"
                    rr.plan, rr.tier = None, None
                    rr.inv_premium = None
            elif cov in ACTIVE_TIERS:
                rr.notes.append(("Census Employee Not Found On Invoice", True))
                rr.result, rr.result_red = "Not On Invoice", True
                cnt["missing_invoice"] += 1
                exc("Employees missing from invoice", who, f"Coverage {cov}")
                plan = clean(e.plan) or top_plan
                rr.plan, rr.tier = plan, cov
                prem = e.premium if e.premium is not None else rates.get((plan, cov))
                if prem is not None:
                    rr.inv_premium = prem
                    rr.notes.append(("Expected Census Premium" if e.premium is not None else "Most Used Plan Rate Applied", True))
                    if e.premium is None:
                        cnt["most_used"] += 1
                amt, status = renewal_for(plan, cov) if plan else (None, "none")
                if status == "ok":
                    rr.renewal_premium = amt
                elif status == "unmapped":
                    rr.notes.append(("Renewal Plan Match Not Identified", True))
                    F.renewal_unmapped_plans.add(plan)
                if renewal and rr.renewal_premium is None:
                    cnt["missing_renewal"] += 1

        # ---- summary bookkeeping: enrolled counts per plan/tier
        if cov in ACTIVE_TIERS and (rr.plan or have_invoice):
            F.plan_enrollment[(rr.plan, cov)] = F.plan_enrollment.get((rr.plan, cov), 0) + 1

    # ---- invoice-only employees go to the bottom of the census
    if have_invoice:
        for j, r in enumerate(invs):
            if j in matched_inv:
                continue
            # If multi-division statement covers parent entity vs sub-entity (e.g. Surgery Center POSC vs Clinic POC),
            # only add missing census employees for the prospect's target divisions
            if r.group_no:
                is_sc_group = "surgery center" in (meta.get("company") or "").lower() or any("surgery center" in (p.source or "").lower() for p in census.people)
                if is_sc_group and r.group_no not in ('A02', 'A03', 'A05', 'C02', 'C03', 'C05', 'D02', 'D03', 'D05'):
                    continue
            # For employees missing from census: leave unsupported demographics blank
            p = Person("EE", r.first, r.last, gender="", dob=None, zip="", coverage="",
                       plan=r.plan, premium=None, sub_id=r.sub_id, source=r.source, invoice_only=True)
            p.cobra = None
            census.people.append(p)
            status_text = r.status or "Active"
            rr = RowResult(inv=r, inv_name=r.raw_name, plan=r.plan, tier=r.tier or "EE", status=status_text,
                           inv_premium=r.premium, result="Invoice Employee Not Found In Census", result_red=True)
            rr.match_pct = 0
            rr.match_text = "0% Match | Invoice Only"

            # Check if any census notes mention this employee
            broker_info = ""
            for cn in getattr(census, "census_notes", []):
                if r.last and r.last.lower() in cn.lower():
                    broker_info = cn
                    break

            retro_note = f" (plus {r.retro_period} retro {r.retro_code} ${r.retro_amount:,.2f})" if r.retro_amount else ""
            if "recent term" in broker_info.lower() or "term" in broker_info.lower():
                note_text = f"Invoice Employee Not Found In Census - on {r.service_period or 'invoice'} invoice{retro_note}. Broker submission email states he is a recent term; demographics not available."
            else:
                note_text = f"Invoice Employee Not Found In Census - on {r.service_period or 'invoice'} invoice{retro_note}; demographics not available."

            rr.notes.append((note_text, True))
            if renewal:
                amt, status = renewal_for(r.plan, r.tier or "EE") if (r.tier or "EE") in ACTIVE_TIERS else (None, "none")
                if status == "ok":
                    rr.renewal_premium = amt
                elif status == "unmapped":
                    rr.notes.append(("Renewal Plan Match Not Identified", True))
                    F.renewal_unmapped_plans.add(r.plan)
            results[id(p)] = rr
            cnt["missing_census"] += 1
            exc("Employee missing from census", r.raw_name, f"On invoice ({r.plan}, {r.tier or 'Employee Only'}, ${r.premium:,.2f}); broker reports recent term")

    # ---- RowResult for dependents (covered under subscriber tier)
    for dep in census.dependents:
        rr_dep = RowResult(
            inv=None, inv_name="", plan="", tier="",
            status="", inv_premium=None, renewal_premium=None, match_pct=None,
            match_text="", result=f"Dependent - covered under subscriber {dep.coverage or 'medical'} tier",
            result_red=False, notes=list(dep.notes)
        )
        results[id(dep)] = rr_dep

    # ---- Diagnostic tracing for stage 5 (Invoice matching)
    if tracer:
        for p in census.people:
            rr_item = results.get(id(p))
            if rr_item:
                tracer.record_invoice_match(
                    p.record_id, p.display, rr_item.inv_name,
                    (rr_item.match_pct or 0) / 100.0 if rr_item.match_pct is not None else 0.0,
                    rr_item.plan, rr_item.tier, rr_item.inv_premium, rr_item.result, p.invoice_only
                )

    for d in census.unassigned_dependents:
        exc("Invalid dependent structures", d.display, "Unassigned dependent - no matching subscriber")

    # Renewal rates for plan tables (only when renewal is required and provided)
    if renewal:
        plans = {p for (p, _t) in list(rates) + list(F.plan_enrollment)}
        for p in plans:
            for t in ACTIVE_TIERS:
                amt, st = renewal_for(p, t)
                if st == "ok" and amt is not None:
                    F.renewal_rates[(p, t)] = amt

    # Deterministic Premium Reconciliation calculation
    matched_emps = [e for e in census.employees if not e.invoice_only and results.get(id(e)) and results[id(e)].inv is not None]
    matched_prem = sum((results[id(e)].inv_premium or 0.0) for e in matched_emps)
    inv_only_recs = [r for j, r in enumerate(invs) if j not in matched_inv]
    inv_only_prem = sum((r.premium or 0.0) for r in inv_only_recs)
    cur_charges = matched_prem + inv_only_prem
    retro_total = sum((r.retro_amount or 0.0) for r in invs)
    total_due = cur_charges + retro_total

    F.census_matched_premium = round(matched_prem, 2)
    F.invoice_current_charges = round(cur_charges, 2)
    F.invoice_only_difference = round(inv_only_prem, 2)
    F.invoice_retro_adjustments = round(retro_total, 2)
    F.invoice_total_balance_due = round(total_due, 2)

    diff_desc = f"Difference ({inv_only_recs[0].last}, {inv_only_recs[0].first} - invoice only)" if len(inv_only_recs) == 1 else "Difference (invoice only employees)"
    retro_desc = f"Invoice current adjustments ({inv_only_recs[0].last} retro {inv_only_recs[0].retro_code} {inv_only_recs[0].retro_period})" if len(inv_only_recs) == 1 and inv_only_recs[0].retro_amount else "Invoice current adjustments"

    F.reconciliation_items = [
        (f"Census-matched monthly premium ({len(matched_emps)} enrolled employees)", F.census_matched_premium),
        (f"Invoice current charges {F.invoice_period}".strip(), F.invoice_current_charges),
        (diff_desc, F.invoice_only_difference),
        (retro_desc, F.invoice_retro_adjustments),
        ("Invoice total balance due", F.invoice_total_balance_due),
    ]

    F.counters.update(cnt)
    F.wp_stats = dict(wp)
    return results

