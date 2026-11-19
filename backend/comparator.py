"""Census-vs-invoice comparison, tier validation, WP/NE/WO handling, premium population."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import mode, StatisticsError
from typing import Optional

from census_config import ACTIVE_TIERS, FUZZY_ACCEPT, FUZZY_WITH_DOB, NON_ENROLLED
from models import Census, Findings, InvoiceRecord, Person, RenewalDoc, RowResult
from email_evidence import find_person_evidence
from plan_linker import _match_plan_against_renewal, build_plan_link_table, match_by_premium_fingerprint
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
        tok_candidates = [tuple(emp_tok[i])]
        if getattr(e, "middle_name", "") and e.last:
            full_mid_tok = tuple(name_tokens(f"{e.first} {e.middle_name}", e.last))
            if full_mid_tok and full_mid_tok not in tok_candidates:
                tok_candidates.append(full_mid_tok)
        for tk in tok_candidates:
            cands = [j for j in by_tok.get(tk, []) if j not in used]
            if cands:
                cands.sort(key=lambda j: 0 if (e.dob and invs[j].dob == e.dob) else 1)
                res[i] = (cands[0], 1.0)
                used.add(cands[0])
                break

    # pass 2a - primary first-name token + last-name match when one side includes a middle name
    for i, e in enumerate(emps):
        if i in res or not e.first or not e.last:
            continue
        ef_parts = [re.sub(r"[^a-z]", "", p.lower()) for p in clean(e.first).split() if re.sub(r"[^a-z]", "", p.lower())]
        el_clean = re.sub(r"[^a-z]", "", clean(e.last).lower())
        em_clean = re.sub(r"[^a-z]", "", clean(getattr(e, "middle_name", "") or "").lower())
        if not ef_parts or not el_clean:
            continue
        ef0 = ef_parts[0]
        ef_mid = ef_parts[1] if len(ef_parts) > 1 else em_clean
        # Ensure no other unmatched employee shares the same (primary_first, last)
        competing = [
            k for k, other in enumerate(emps)
            if k != i and k not in res
            and other.first and other.last
            and re.sub(r"[^a-z]", "", clean(other.first).split()[0].lower()) == ef0
            and re.sub(r"[^a-z]", "", clean(other.last).lower()) == el_clean
        ]
        if competing:
            continue
        for j, inv in enumerate(invs):
            if j in used or not inv.first or not inv.last:
                continue
            if_parts = [re.sub(r"[^a-z]", "", p.lower()) for p in clean(inv.first).split() if re.sub(r"[^a-z]", "", p.lower())]
            il_clean = re.sub(r"[^a-z]", "", clean(inv.last).lower())
            if not if_parts or il_clean != el_clean or if_parts[0] != ef0:
                continue
            if_mid = if_parts[1] if len(if_parts) > 1 else ""
            if ef_mid and if_mid and not (ef_mid.startswith(if_mid) or if_mid.startswith(ef_mid)):
                continue
            res[i] = (j, 1.0)
            used.add(j)
            break

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

    # pass 2c - compound / concatenated unspaced name matching (e.g. KAINROSS == Kain + Ross)
    for i, e in enumerate(emps):
        if i in res or not e.first or not e.last:
            continue
        e_first_clean = clean(e.first).lower()
        e_last_clean = clean(e.last).lower()
        if len(e_first_clean) < 2 or len(e_last_clean) < 2:
            continue
        compound_cands = {
            f"{e_last_clean}{e_first_clean}",
            f"{e_first_clean}{e_last_clean}",
        }
        for j, inv in enumerate(invs):
            if j in used:
                continue
            inv_raw_clean = re.sub(r"[^a-z]", "", (inv.raw_name or "").lower())
            if inv_raw_clean and inv_raw_clean in compound_cands:
                res[i] = (j, 1.0)
                used.add(j)
                break

    # pass 2d - multi-word / compound surname subset match (e.g. 'Carlos Oropeza Mejia' <-> 'MEJIA, CARLOS')
    for i, e in enumerate(emps):
        if i in res or not e.first or not e.last:
            continue
        ef_toks = [re.sub(r"[^a-z]", "", p.lower()) for p in clean(e.first).split() if re.sub(r"[^a-z]", "", p.lower())]
        el_toks = [re.sub(r"[^a-z]", "", p.lower()) for p in re.split(r"[\s\-]+", clean(e.last)) if len(re.sub(r"[^a-z]", "", p.lower())) >= 2]
        if not ef_toks or not el_toks:
            continue
        for j, inv in enumerate(invs):
            if j in used or not inv.first or not inv.last:
                continue
            if_toks = [re.sub(r"[^a-z]", "", p.lower()) for p in clean(inv.first).split() if re.sub(r"[^a-z]", "", p.lower())]
            il_toks = [re.sub(r"[^a-z]", "", p.lower()) for p in re.split(r"[\s\-]+", clean(inv.last)) if len(re.sub(r"[^a-z]", "", p.lower())) >= 2]
            if not if_toks or not il_toks or ef_toks[0] != if_toks[0]:
                continue
            el_set, il_set = set(el_toks), set(il_toks)
            if (el_set < il_set or il_set < el_set) and (el_set & il_set):
                res[i] = (j, 0.94)
                used.add(j)
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

    # Ensure idempotency across Learning Engine reruns: strip any prior invoice_only rows
    census.people = [p for p in census.people if not getattr(p, "invoice_only", False)]
    F.exceptions = [
        exc_item for exc_item in F.exceptions
        if exc_item[0] not in (
            "Duplicate employees", "Invalid dependent structures", "Eligibility conflicts",
            "Tier mismatches", "Coverage mismatches", "Employees missing from invoice",
            "Employee missing from census",
        )
    ]
    F.plan_enrollment.clear()
    F.renewal_unmapped_plans.clear()
    F.renewal_rates.clear()

    results_dup: dict[int, RowResult] = {}
    for d in census.people:                         # duplicates stay in census.people but are not compared
        if d.role != "EE":
            continue
        if d.is_duplicate:
            exc_dup = RowResult()
            results_dup[id(d)] = exc_dup
            F.exceptions.append(("Duplicate employees", d.display, "Duplicate record in source census"))
        elif getattr(d, "disposition", "") == "EXCLUDE_PART_TIME":
            results_dup[id(d)] = RowResult(result="Excluded - Part-Time", result_red=False)
        elif str(getattr(d, "disposition", "") or "").startswith("EXCLUDE_"):
            results_dup[id(d)] = RowResult(result="Excluded - Terminated/Inactive", result_red=False)

    emps = [
        e for e in census.canonical_census_employees
        if not e.is_duplicate and not str(getattr(e, "disposition", "") or "").startswith("EXCLUDE_")
    ]
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

    def _lookup_member_renewal_rate(emp_obj: Optional[Person] = None, inv_obj: Optional[InvoiceRecord] = None) -> Optional[float]:
        if not renewal or not getattr(renewal, "employee_rates", None):
            return None
        emp_rates = renewal.employee_rates
        candidates: list[tuple[str, str, str]] = []
        if emp_obj and (emp_obj.first or emp_obj.last):
            fp = re.sub(r"[^a-z]", "", (emp_obj.first.split()[0] if emp_obj.first and emp_obj.first.split() else "").lower())
            fc = re.sub(r"[^a-z]", "", (emp_obj.first or "").lower())
            mc = re.sub(r"[^a-z]", "", (getattr(emp_obj, "middle_name", "") or "").lower())
            lc = re.sub(r"[^a-z]", "", (emp_obj.last or "").lower())
            if fp and lc:
                candidates.append((fp, mc[:1] if mc else (fc[len(fp):len(fp)+1] if len(fc) > len(fp) else ""), lc))
        if inv_obj and (inv_obj.first or inv_obj.last):
            fp = re.sub(r"[^a-z]", "", (inv_obj.first.split()[0] if inv_obj.first and inv_obj.first.split() else "").lower())
            parts = [re.sub(r"[^a-z]", "", p.lower()) for p in (inv_obj.first or "").split() if re.sub(r"[^a-z]", "", p.lower())]
            mi = parts[1][:1] if len(parts) > 1 else ""
            lc = re.sub(r"[^a-z]", "", (inv_obj.last or "").lower())
            if fp and lc:
                candidates.append((fp, mi, lc))
        for fp, mi, lc in candidates:
            for key_cand in (f"{fp}{mi}{lc}", f"{fp}{lc}", f"{lc}{fp}{mi}", f"{lc}{fp}"):
                if key_cand in emp_rates:
                    return emp_rates[key_cand].get("renewal_premium")
            for k, rec in emp_rates.items():
                if k.startswith(fp) and k.endswith(lc) and len(k) == len(fp) + len(lc) + 1:
                    return rec.get("renewal_premium")
        return None

    plan_links: dict = {}

    def renewal_for(plan: str, tier: str, emp: Optional[Person] = None, inv: Optional[InvoiceRecord] = None) -> tuple[Optional[float], str]:
        if not renewal:
            return None, "no_doc"
        member_amt = _lookup_member_renewal_rate(emp, inv)
        if member_amt is not None:
            return member_amt, "ok"

        rp = None
        p_clean = clean(plan)
        if p_clean and p_clean in plan_links:
            rp = plan_links[p_clean].renewal_plan
        if not rp and p_clean:
            rp = _match_plan_against_renewal(p_clean, renewal)
        # Fallback 1: employee's census plan directly names a renewal plan ID/name
        if not rp and emp is not None and clean(getattr(emp, "plan", "")):
            rp = _match_plan_against_renewal(emp.plan, renewal)
        # Fallback 2: exact premium-rate fingerprint on this subscriber's invoice row
        if not rp and inv is not None and getattr(inv, "premium", None) and tier in ACTIVE_TIERS:
            fp_link = match_by_premium_fingerprint(p_clean or clean(inv.plan), {tier: [float(inv.premium)]}, renewal)
            if fp_link:
                rp = fp_link.renewal_plan
        # Fallback 3: single-plan renewal when no plan name was given
        if not rp and not p_clean and len(renewal.plans) == 1:
            rp = renewal.plans[0]

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

    # Dynamic evidence-based plan linking (Fix #4 + Whole-Submission Blueprint):
    # resolve invoice billing codes (e.g. 0006-PPO, 0004-HMO) to renewal plan IDs via
    # direct match -> census plan consensus -> exact premium-rate fingerprint -> blueprint plan links.
    if renewal and have_invoice:
        plan_links.update(build_plan_link_table(
            renewal, emps, invs, matches,
            inv_rates=rates,
            blueprint_links=meta.get("blueprint_plan_links"),
        ))
        for inv_p, lnk in plan_links.items():
            if lnk.method != "direct_name_match":
                note = f"Renewal plan linked: '{inv_p}' -> '{lnk.renewal_plan}' ({lnk.evidence})"
                if note not in F.renewal_notes:
                    F.renewal_notes.append(note)
    # Also surface any Blueprint plan links for SBC matching even when renewal is not required
    for bpl in (meta.get("blueprint_plan_links") or []):
        ip = clean(getattr(bpl, "invoice_plan", ""))
        cp = clean(getattr(bpl, "canonical_plan_id", ""))
        if ip and cp:
            note = f"Renewal plan linked: '{ip}' -> '{cp}' ({clean(getattr(bpl, 'evidence', '')) or 'blueprint'})"
            if note not in F.renewal_notes:
                F.renewal_notes.append(note)

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
                    amt, status = renewal_for(rr.plan, tier_for_renewal, emp=e, inv=inv)
                    if status == "ok":
                        rr.renewal_premium = amt
                else:
                    rr.plan, rr.tier, rr.status, rr.inv_premium = None, None, "No Medical on Invoice", None
                    rr.renewal_premium = None
                    rr.result, rr.result_red = "Not Enrolled - Expected", False
            else:
                census_plan_raw = clean(e.plan)
                if not e.plan and inv.plan:
                    e.plan = inv.plan
                if e.premium is None and inv.premium is not None:
                    e.premium = inv.premium

                eff_inv_tier = inv.tier
                if not eff_inv_tier and getattr(inv, "dep_medical_premium", 0.0) > 0.0 and cov in ("ES", "EC", "FAM"):
                    eff_inv_tier = cov
                rr.plan, rr.tier, rr.inv_premium = inv.plan or e.plan, eff_inv_tier, inv.premium
                if rr.plan and census_plan_raw:
                    F.plan_display_names.setdefault(rr.plan, census_plan_raw)
                if "premium due" in (inv.status or "").lower():
                    rr.status = "Active - Premium Due (Adjustment)"
                    prem_str = f" (${inv.premium:,.2f})" if inv.premium is not None else ""
                    rr.notes.append((f"Invoice shows medical as PREMIUM DUE adjustment{prem_str} rather than payroll deduction - verify billing", False))
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
                    rr.result_red = True
                    cnt["name_mismatch"] += 1
                    exc("Name mismatches", who, f"Census '{e.display}' vs Invoice '{inv.raw_name}' (matched on plan/tier)")
                    rr.notes.append((f"Name mismatch - census name differs from invoice subscriber; matched on plan/tier/coverage. Invoice Name: {inv.raw_name}", True))
                    F.notes.append(f"Name Mismatch for Review: Census employee '{e.display}' matched to invoice subscriber '{inv.raw_name}' based on plan and tier - verify identity.")
                else:
                    rr.result = "Match" if not issues else "Mismatch: " + "; ".join(issues)
                    rr.result_red = red_issue

            if cov not in NON_ENROLLED:
                tier_for_renewal = (inv.tier if inv.tier in ACTIVE_TIERS else
                                    cov if cov in ACTIVE_TIERS else tier_from_deps(e))
                amt, status = renewal_for(rr.plan, tier_for_renewal, emp=e, inv=inv)
                if status == "unmapped":
                    rr.notes.append(("Renewal Plan Match Not Identified", True))
                    F.renewal_unmapped_plans.add(rr.plan or "(no plan name)")
                    exc("Renewal mapping failures", who, f"Plan '{rr.plan}'")
                    cnt["missing_renewal"] += 1
                elif status == "ok":
                    rr.renewal_premium = amt
                    if amt is None:
                        cnt["missing_renewal"] += 1

        # ================= employee NOT on medical invoice
        elif have_invoice:
            if cov in NON_ENROLLED:
                # Check if employee appears on a combined bill with $0.00 medical + ancillary (Dental/Vision) only
                nm_match = None
                nm_sim = 0.0
                for nm_rec in (meta.get("non_medical_invoice_records") or []):
                    sc_nm = name_similarity((e.first, e.last), (nm_rec.first, nm_rec.last))
                    if sc_nm >= 0.85 and sc_nm > nm_sim:
                        nm_sim = sc_nm
                        nm_match = nm_rec
                if nm_match is not None:
                    nm_pct = int(round(nm_sim * 100))
                    rr.inv = nm_match
                    rr.inv_name = nm_match.raw_name
                    rr.group_no = nm_match.group_no
                    rr.match_pct = nm_pct
                    rr.match_text = "100% Match" if nm_pct >= 100 else f"{nm_pct}% Match | Invoice Name: {nm_match.raw_name}"
                    rr.status = nm_match.status or "Ancillary Only"
                    rr.result = "Match"
                    rr.result_red = False
                    rr.plan, rr.tier, rr.inv_premium = None, None, None
                    anc_desc = nm_match.ancillary_detail or (
                        f"${nm_match.ancillary_premium:,.2f} non-medical" if nm_match.ancillary_premium else "non-medical"
                    )
                    rr.notes.append((f"On invoice with $0.00 medical ({anc_desc} only); waived medical coverage confirmed.", False))
                else:
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
                census_plan = clean(e.plan)
                if e.premium is not None:
                    plan = census_plan or top_plan
                    prem = e.premium
                    used_most_used = False
                elif census_plan and (census_plan, cov) in rates:
                    plan = census_plan
                    prem = rates[(census_plan, cov)]
                    used_most_used = False
                else:
                    plan = top_plan
                    prem = rates.get((top_plan, cov)) if top_plan else None
                    used_most_used = bool(top_plan)
                rr.plan, rr.tier = plan, cov
                if prem is not None:
                    rr.inv_premium = prem
                if used_most_used:
                    rr.used_most_used_plan = True
                    cnt["most_used"] += 1
                    rr.notes.append(("Most used plan used", True))
                elif prem is not None:
                    rr.notes.append(("Expected Census Premium", True))
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

            # Check if any census notes or client/broker submission emails mention this employee
            broker_info = ""
            for cn in getattr(census, "census_notes", []):
                if r.last and r.last.lower() in cn.lower():
                    broker_info = cn
                    break
            email_ev = find_person_evidence(meta.get("email_evidence"), r.first, r.last)

            retro_note = f" (plus {r.retro_period} retro {r.retro_code} ${r.retro_amount:,.2f})" if r.retro_amount else ""
            if email_ev and email_ev.event_type == "terminated":
                eff_part = f" effective {email_ev.effective_date}" if email_ev.effective_date else ""
                note_text = (
                    f"Invoice Employee Not Found In Census - on {r.service_period or 'invoice'} invoice{retro_note}. "
                    f"Submission email ({email_ev.source}) states terminated{eff_part}; confirm removal from billing (not added to census)."
                )
                term_clause = f"; submission email states terminated{eff_part}"
            elif "recent term" in broker_info.lower() or "term" in broker_info.lower():
                note_text = f"Invoice Employee Not Found In Census - on {r.service_period or 'invoice'} invoice{retro_note}. Broker submission email states he is a recent term; demographics not available."
                term_clause = "; broker reports recent term"
            else:
                note_text = f"Invoice Employee Not Found In Census - on {r.service_period or 'invoice'} invoice{retro_note}; demographics not available."
                term_clause = "; demographics not available"

            rr.notes.append((note_text, True))
            if renewal:
                amt, status = renewal_for(r.plan, r.tier or "EE", inv=r) if (r.tier or "EE") in ACTIVE_TIERS else (None, "none")
                if status == "ok":
                    rr.renewal_premium = amt
                elif status == "unmapped":
                    rr.notes.append(("Renewal Plan Match Not Identified", True))
                    F.renewal_unmapped_plans.add(r.plan)
            results[id(p)] = rr
            cnt["missing_census"] += 1
            prem_clause = f", ${r.premium:,.2f}" if r.premium is not None else ""
            plan_clause = r.plan or "Medical"
            tier_clause = r.tier or "Employee Only"
            exc("Employee missing from census", r.raw_name, f"On invoice ({plan_clause}, {tier_clause}{prem_clause}){term_clause}")

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

    for d in census.dependents:
        rr_dep = RowResult(notes=list(d.notes))
        rr_dep.result = "Dependent"
        results[id(d)] = rr_dep

    # Renewal rates for plan tables (only when renewal is required and provided)
    if renewal:
        plans = {p for (p, _t) in list(rates) + list(F.plan_enrollment)}
        for p in plans:
            for t in ACTIVE_TIERS:
                amt, st = renewal_for(p, t)
                if st == "ok" and amt is not None:
                    F.renewal_rates[(p, t)] = amt
        # Fallback for member-level / age-banded renewals where an employee's census/invoice tier
        # differs from the renewal appendix tier code: populate F.renewal_rates from matched employee renewal_premiums
        emp_ren_bucket: dict[tuple[str, str], list[float]] = defaultdict(list)
        for p_person in census.people:
            if p_person.role != "EE":
                continue
            rr_p = results.get(id(p_person))
            if rr_p and rr_p.plan and rr_p.renewal_premium is not None:
                t_std = std_coverage(rr_p.tier or p_person.coverage)
                if t_std in ACTIVE_TIERS:
                    emp_ren_bucket[(rr_p.plan, t_std)].append(round(rr_p.renewal_premium, 2))
        for k_pt, vals in emp_ren_bucket.items():
            if k_pt not in F.renewal_rates and vals:
                F.renewal_rates[k_pt] = _modal(vals)
        # Record current -> renewal plan mapping on F.plan_display_names / F.notes when present
        if getattr(renewal, "plan_mapping", None):
            for p in sorted(plans):
                if not p:
                    continue
                matched_curr = match_plan(p, list(renewal.plan_mapping.keys()))
                if matched_curr and renewal.plan_mapping.get(matched_curr):
                    ren_code = renewal.plan_mapping[matched_curr]
                    base_disp = F.plan_display_names.get(p, p)
                    if ren_code not in base_disp:
                        F.plan_display_names[p] = f"{base_disp} (Renewal: {ren_code})"

    # Deterministic Premium Reconciliation calculation
    matched_emps = [
        e for e in census.employees
        if not e.invoice_only
        and results.get(id(e))
        and results[id(e)].inv is not None
        and not getattr(results[id(e)].inv, "ancillary_only", False)
    ]
    matched_prem = sum((results[id(e)].inv_premium or 0.0) for e in matched_emps)
    inv_only_recs = [r for j, r in enumerate(invs) if j not in matched_inv]
    inv_only_prem = sum((r.premium or 0.0) for r in inv_only_recs)
    cur_charges = matched_prem + inv_only_prem
    retro_total = sum((r.retro_amount or 0.0) for r in invs)
    total_due = cur_charges + retro_total
    printed_total_due = meta.get("printed_invoice_total_due")
    adj_label = ""
    non_med_amt: Optional[float] = None
    non_med_lbl = ""
    unexp_amt: Optional[float] = None
    unexp_lbl = ""
    if printed_total_due is not None and float(printed_total_due) > 0 and abs(float(printed_total_due) - total_due) > 0.01:
        # Combined multi-benefit invoice (e.g. Medical + Dental/Vision + invoice-level credits):
        # reconcile medical-only charges transparently to the printed invoice Total Amount Due.
        retro_total = round(float(printed_total_due) - cur_charges, 2)
        total_due = round(float(printed_total_due), 2)
        raw_non_med = meta.get("invoice_non_medical_charges")
        raw_bd = meta.get("invoice_non_medical_breakdown") or {}
        raw_gross = meta.get("invoice_gross_total_premium")
        if raw_non_med is not None and float(raw_non_med) > 0:
            non_med_amt = round(float(raw_non_med), 2)
            bd_parts = " + ".join(f"{k} ${v:,.2f}" for k, v in raw_bd.items() if v)
            non_med_lbl = f"Invoice non-medical charges ({bd_parts})" if bd_parts else "Invoice non-medical charges (Dental/Vision)"
            unexp = round(total_due - (cur_charges + non_med_amt), 2)
            if abs(unexp) > 0.01:
                unexp_amt = unexp
                gross_disp = f"Gross ${float(raw_gross):,.2f} vs " if raw_gross else ""
                unexp_lbl = f"Invoice-level credit / adjustment (unexplained in invoice; {gross_disp}Amount Due ${total_due:,.2f})"
                recon_note = (
                    f"Invoice reconciliation note: Medical charges (${cur_charges:,.2f}) + non-medical charges "
                    f"(${non_med_amt:,.2f}: {bd_parts or 'Dental/Vision'}) total ${cur_charges + non_med_amt:,.2f}, "
                    f"whereas the printed invoice Amount Due is ${total_due:,.2f} (a {unexp:+,.2f} invoice-level "
                    f"credit/adjustment not itemized in the supplied invoice)."
                )
                if recon_note not in F.notes:
                    F.notes.append(recon_note)
        adj_label = "Invoice non-medical charges & adjustments"

    F.census_matched_premium = round(matched_prem, 2)
    F.invoice_current_charges = round(cur_charges, 2)
    F.invoice_only_difference = round(inv_only_prem, 2)
    F.invoice_retro_adjustments = round(retro_total, 2)
    F.invoice_total_balance_due = round(total_due, 2)
    F.invoice_adjustments_label = adj_label
    F.invoice_non_medical_charges = non_med_amt
    F.invoice_non_medical_label = non_med_lbl
    F.invoice_unexplained_adjustment = unexp_amt
    F.invoice_unexplained_label = unexp_lbl

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

