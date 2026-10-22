"""Census-vs-invoice comparison, tier validation, WP/NE/WO handling, premium population."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import mode, StatisticsError
from typing import Optional

from census_config import ACTIVE_TIERS, FUZZY_ACCEPT, FUZZY_WITH_DOB, NON_ENROLLED
from models import Census, Findings, InvoiceRecord, Person, RenewalDoc, RowResult
from utils import clean, match_plan, name_similarity, name_tokens

TERMINATED = ("term", "cancel", "inactive", "lapse", "disenroll")


# ----------------------------------------------------------------- helpers --
def tier_from_deps(emp: Person) -> str:
    roles = {d.role for d in emp.dependents}
    if not roles:
        return "EE"
    if roles == {"SP"}:
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
        if r.tier in ACTIVE_TIERS and r.premium and r.premium > 0:
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

    # pass 3 - fuzzy (typos, middle names); greedy best-first
    pairs = []
    for i, e in enumerate(emps):
        if i in res or not emp_tok[i]:
            continue
        for j, t in enumerate(inv_tok):
            if j in used or not t:
                continue
            s = name_similarity(emp_tok[i], t)
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
                   meta: dict, F: Findings, have_invoice: bool) -> dict[int, RowResult]:
    results_dup: dict[int, RowResult] = {}
    for d in census.employees:                      # duplicates stay in the census but are not compared
        if d.is_duplicate:
            exc_dup = RowResult()
            results_dup[id(d)] = exc_dup
            F.exceptions.append(("Duplicate employees", d.display, "Duplicate record in source census"))
    emps = [e for e in census.employees if not e.is_duplicate]
    rates = build_rate_table(invs) if have_invoice else {}
    top_plan = most_used_plan(invs, census) if have_invoice else ""
    inv_plans = sorted({r.plan for r in invs if r.plan})
    F.plan_rates = rates
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
            bad = ((cov == "EE" and roles) or (cov == "ES" and ("SP" not in roles or "CH" in roles)) or
                   (cov == "EC" and ("CH" not in roles or "SP" in roles)) or (cov == "FAM" and not roles))
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

                if e.dob and inv.dob and e.dob != inv.dob:
                    issues.append("DOB")
                if e.gender and inv.gender and e.gender != inv.gender:
                    issues.append("Gender")
                if e.zip and inv.zip and e.zip[:5] != inv.zip[:5]:
                    issues.append("ZIP")
                if cov in ACTIVE_TIERS and inv.tier in ACTIVE_TIERS and cov != inv.tier:
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
                pass                                                  # expected - no notes, no highlight
            elif cov == "WP":
                wp["total"] += 1
                elected = clean(e.plan)
                tier = tier_from_deps(e)
                if elected:
                    plan = match_plan(elected, inv_plans) or elected
                    wp["elected"] += 1
                    rr.notes.append(("WP Employee - Awaiting Eligibility", True))
                else:
                    plan = top_plan
                    if plan:
                        wp["most_used"] += 1
                        rr.notes.append(("WP Employee - Most Used Plan Applied", True))
                    else:
                        wp["missing_election"] += 1
                rr.plan, rr.tier = plan, tier
                rr.inv_premium = rates.get((plan, tier))
                if plan:
                    amt, status = renewal_for(plan, tier)
                    if status == "ok":
                        rr.renewal_premium = amt
                    elif status == "unmapped":
                        rr.notes.append(("Renewal Plan Match Not Identified", True))
                        F.renewal_unmapped_plans.add(plan)
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
            # For employees missing from census, populate info from invoice per REI instructions
            p = Person("EE", r.first, r.last, gender=r.gender, dob=r.dob, zip=r.zip, coverage=r.tier or "EE",
                       plan=r.plan, premium=r.premium, sub_id=r.sub_id, source=r.source, invoice_only=True)
            p.cobra = None
            census.people.append(p)
            status_text = r.status or "Active"
            rr = RowResult(inv=r, inv_name=r.raw_name, plan=r.plan, tier=r.tier or "EE", status=status_text,
                           inv_premium=r.premium, result="Missing From Census", result_red=True)
            rr.match_text = "N/A - Invoice Only"
            note_parts = ["Invoice Employee Not Found In Census"]
            if status_text and status_text.lower() != "active":
                note_parts.append(f"invoice marks employee as ({status_text})")

            # Check if any census notes mention this employee by last name
            for cn in getattr(census, "census_notes", []):
                if r.last and r.last.lower() in cn.lower():
                    label, sep, body_txt = cn.partition(" | ")
                    if not sep:
                        label, body_txt = "", cn
                    # split on sentence ends, but not after a single-letter initial like "A."
                    for s in re.split(r"(?<!\b[A-Z])[.\n]\s+", body_txt):
                        if r.last.lower() in s.lower():
                            c_clean = s.strip().rstrip(".")
                            if c_clean:
                                note_parts.append(f"census notes: {c_clean}")
                                break
                    break

            rr.notes.append(("; ".join(note_parts), True))
            amt, status = renewal_for(r.plan, r.tier or "EE") if (r.tier or "EE") in ACTIVE_TIERS else (None, "none")
            if status == "ok":
                rr.renewal_premium = amt
            elif status == "unmapped":
                rr.notes.append(("Renewal Plan Match Not Identified", True))
                F.renewal_unmapped_plans.add(r.plan)
            results[id(p)] = rr
            cnt["missing_census"] += 1
            exc("Employees missing from census", r.raw_name, f"Invoice tier {r.tier or 'n/a'}")

    for d in census.unassigned_dependents:
        exc("Invalid dependent structures", d.display, "Unassigned dependent - no matching subscriber")

    # renewal rates for plan tables
    if renewal:
        plans = {p for (p, _t) in list(rates) + list(F.plan_enrollment)}
        for p in plans:
            for t in ACTIVE_TIERS:
                amt, st = renewal_for(p, t)
                if st == "ok" and amt is not None:
                    F.renewal_rates[(p, t)] = amt
    F.counters.update(cnt)
    F.wp_stats = dict(wp)
    return results
