"""Parse renewal rate documents and pick the applicable FUTURE-period renewal."""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

from classifier import FIELD_ALIASES, body, detect_header
from models import RenewalDoc
from readers import RawDoc
from utils import clean, is_blank, norm_key, parse_date, std_coverage, to_money

log = logging.getLogger("census.renewal")

_TIERS = ["EE", "ES", "EC", "FAM"]
_DATE = r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4})"
_RENEW_PREM = ["renewalpremium", "renewalrate", "newrate", "newpremium", "proposedrate", "proposedpremium",
               "renewal", "premium", "rate", "monthlyrate", "monthlypremium", "totalpremium"]


def _dates(doc: RawDoc) -> tuple[Optional[date], Optional[date]]:
    text = doc.text or ""
    eff = term = None
    # Check Plan Year: MM/DD/YYYY - MM/DD/YYYY e.g. "Plan Year: 06/01/2026 - 05/31/2027"
    m_py = re.search(rf"(?:renewal|coverage|plan)\s*(?:period|year)\s*[:\-]?\s*{_DATE}\s*(?:to|through|thru|-|–)\s*{_DATE}", text, re.I)
    if m_py:
        eff = parse_date(m_py.group(1))
        term = parse_date(m_py.group(2))
    m = re.search(rf"rates?\s+(?:as\s+of|effective)\s*[:\-]?\s*{_DATE}", text, re.I)
    explicit = bool(m and parse_date(m.group(1)))
    if explicit:
        eff = parse_date(m.group(1))
    if not eff:
        m = re.search(rf"(?:renewal\s*date|(?:renewal\s*)?effective\s*(?:date)?)\s*[:\-]?\s*{_DATE}", text, re.I)
        if m:
            eff = parse_date(m.group(1))
    if not term:
        m = re.search(rf"(?:termination|term(?:ination)?\s*date|expir\w*)\s*[:\-]?\s*{_DATE}", text, re.I)
        if m:
            term = parse_date(m.group(1))
    # Plan-level effective dates printed on rate rows are more authoritative than a report/header date.
    row_dates = []
    for line in text.splitlines():
        if len(re.findall(r"\$\s*[0-9,]+\.[0-9]{2}", line)) >= 2:
            for ds in re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", line):
                d = parse_date(ds)
                if d:
                    row_dates.append(d)
    if row_dates and not explicit:
        eff = max(row_dates)
    return eff, term


def _version_rank(doc: RawDoc) -> int:
    low = (doc.path.stem + " " + (doc.text or "")[:1200]).lower()
    rank = 0
    if re.search(r"\bfinal\b", low):
        rank += 2
    if re.search(r"\b(revised|rev\b|updated|amended)", low):
        rank += 1
    if re.search(r"\bdraft\b|preliminary|illustrative", low):
        rank -= 2
    return rank


def _tier_columns(labels: list[str]) -> dict[str, int]:
    out = {}
    for i, lab in enumerate(labels):
        t = std_coverage(lab)
        if t in _TIERS and t not in out:
            out[t] = i
    return out


def _records_from_renewal_text(text: str) -> dict:
    tier_map = {
        'employee': 'EE',
        'employee+child(ren)': 'EC',
        'employee+child': 'EC',
        'employee+children': 'EC',
        'ee+spouse/domestic partner': 'ES',
        'ee+spouse': 'ES',
        'employee+spouse': 'ES',
        'family': 'FAM'
    }
    rates = {}
    amt_re = re.compile(r'\$\s*([0-9,]+\.[0-9]{2})')

    for page in text.split('--- [Page'):
        current_plan = ''
        for line in page.splitlines():
            l_strip = line.strip()
            m_p = re.search(r'(?:^\d{5,}\s+)?([A-Z]{2,}\s+(?:PPO|HMO|HDHP|EPO|POS|INDEMNITY)\s+[A-Za-z0-9\- ]+)', l_strip)
            if m_p:
                plan_cand = re.sub(r'^\d+\s+', '', m_p.group(1)).strip()
                parts = re.split(r'\s{4,}', plan_cand)
                cand = parts[0].strip()
                if not any(w in cand.upper() for w in ['DENTAL', 'VISION', 'LIFE', 'DEN ']):
                    current_plan = cand

            m_tier = re.search(r'^\s*(Employee(?:\+Child(?:ren)?)?|EE\+Spouse(?:/Domestic Partner)?|Family)\b', line, re.I)
            if m_tier and current_plan:
                t_str = m_tier.group(1).lower().replace(' ', '')
                t = None
                for k, v in tier_map.items():
                    if k.replace(' ', '') == t_str:
                        t = v
                        break
                if t:
                    amts = amt_re.findall(line)
                    if len(amts) >= 2:
                        rates[(current_plan, t)] = to_money(amts[1])
                    elif len(amts) == 1:
                        rates[(current_plan, t)] = to_money(amts[0])
    return rates


_MEMBER_RENEWAL_LINE_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z'\-\. ]{3,45}?)\s+(\d{2})\s+([MF])\s+([ECFS]|EE|ES|EC|FAM)\s+([AC])\s+"
    r"(?:\d{1,2}\s+)?(?:\d{1,2}\s+)?([A-Za-z0-9\-/]{3,25})\s+\$\s*([0-9,]+\.[0-9]{2})\s+"
    r"([A-Za-z0-9\-/]{3,25})\s+\$\s*([0-9,]+\.[0-9]{2})\b",
    re.M,
)


def _parse_member_level_renewal(text: str) -> tuple[dict, dict[str, str], dict]:
    """Dynamically extract member-level renewal schedules (e.g. Appendix A 'Employee enrollment detail and rates')
    returning (employee_rates, plan_mapping, modal_tier_rates)."""
    from collections import Counter, defaultdict
    emp_rates: dict = {}
    plan_map: dict[str, str] = {}
    bucket: dict[tuple[str, str], list[float]] = defaultdict(list)

    for m in _MEMBER_RENEWAL_LINE_RE.finditer(text or ""):
        raw_name = clean(m.group(1))
        if raw_name.lower().replace(" ", "") in ("coveredemployee", "totalpremium", "employeename"):
            continue
        cov_raw = clean(m.group(4))
        tier = std_coverage(cov_raw) or "EE"
        curr_plan = clean(m.group(6))
        curr_prem = to_money(m.group(7))
        ren_plan = clean(m.group(8))
        ren_prem = to_money(m.group(9))
        if not curr_plan or not ren_plan or ren_prem is None:
            continue
        norm_name = re.sub(r"[^a-z]", "", raw_name.lower())
        if not norm_name:
            continue
        emp_rates[norm_name] = {
            "raw_name": raw_name,
            "tier": tier,
            "current_plan": curr_plan,
            "current_premium": curr_prem,
            "renewal_plan": ren_plan,
            "renewal_premium": ren_prem,
        }
        plan_map[curr_plan] = ren_plan
        bucket[(curr_plan, tier)].append(round(ren_prem, 2))
        bucket[(ren_plan, tier)].append(round(ren_prem, 2))

    modal_rates: dict[tuple[str, str], float] = {}
    for k, vals in bucket.items():
        if vals:
            modal_rates[k] = Counter(vals).most_common(1)[0][0]
    return emp_rates, plan_map, modal_rates


_PLAN_ID_LINE_RE = re.compile(
    r"^\s*Plan\s+ID\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,19})(?:\s+([A-Z0-9][A-Z0-9\-]{3,19}))?\s*$",
    re.I,
)
_COMP_TIER_PAIR_RE = re.compile(
    r"\b(EO|EE|ES|EC|EF|FAM|Employee\s+Only|Employee\s*\+\s*Spouse|Employee\s*\+\s*Child(?:\(ren\)|ren)?|Family)\s*:\s*\$\s*([0-9,]+\.[0-9]{2})",
    re.I,
)
_PLAN_DIR_LINE_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9]{4,14})\s+([A-Za-z][A-Za-z0-9\s\-/&\.]{5,60}?)\s+\d+\s+\$[0-9,]+\.[0-9]{2}\s*$"
)
_COMP_TIER_NORM = {
    "eo": "EE", "ee": "EE", "employeeonly": "EE",
    "es": "ES", "employee+spouse": "ES",
    "ec": "EC", "employee+child": "EC", "employee+children": "EC", "employee+child(ren)": "EC",
    "ef": "FAM", "fam": "FAM", "family": "FAM",
}


def _parse_composite_renewal_blocks(text: str) -> tuple[dict, dict, dict[str, str], dict[str, str]]:
    """Deterministically extract Current & Renewal composite rate blocks and Plan ID -> Name directory.

    Handles carrier 'Renewal at a Glance' blocks where a ``Plan ID <curr> [<ren>]`` line is followed
    by tier lines such as ``EO: $809.86   EO: $1,173.52`` (current rate then renewal rate, or a
    single renewal rate). Returns ``(renewal_rates, current_rates, plan_mapping, plan_names)``.
    """
    ren_rates: dict[tuple[str, str], float] = {}
    cur_rates: dict[tuple[str, str], float] = {}
    plan_map: dict[str, str] = {}
    plan_names: dict[str, str] = {}

    curr_plan = ""
    ren_plan = ""
    lines_since_plan = 0

    for line in (text or "").splitlines():
        if re.match(r"^\s*(?:---+\s*\[EXTRACTED\s+TABLES|===\s*Table)", line, re.I):
            curr_plan = ren_plan = ""
            continue
        m_dir = _PLAN_DIR_LINE_RE.match(line)
        if m_dir:
            pid, pname = m_dir.group(1).strip(), clean(m_dir.group(2))
            if pid and pname and not any(w in pname.upper() for w in ("DENTAL", "VISION", "LIFE")):
                plan_names.setdefault(pid, pname)

        m_pid = _PLAN_ID_LINE_RE.match(line)
        if m_pid:
            curr_plan = m_pid.group(1).strip()
            ren_plan = (m_pid.group(2) or m_pid.group(1)).strip()
            lines_since_plan = 0
            if curr_plan and ren_plan:
                plan_map.setdefault(curr_plan, ren_plan)
            continue
        if re.match(r"^\s*Plan\s+ID\b", line, re.I):
            # Any other Plan ID header (e.g. a table row with NaN) ends the previous plan block
            curr_plan = ren_plan = ""
            continue

        if ren_plan:
            lines_since_plan += 1
            if lines_since_plan > 25 or re.search(r"Total\s+Monthly\s+Medical\s+Premium", line, re.I):
                curr_plan = ren_plan = ""
                continue
            pairs = _COMP_TIER_PAIR_RE.findall(line)
            if not pairs:
                continue
            # Group amounts by canonical tier on this line (typical layout: Current col then Renewal col)
            by_tier: dict[str, list[float]] = {}
            for raw_t, raw_amt in pairs:
                tk = _COMP_TIER_NORM.get(re.sub(r"\s+", "", raw_t).lower())
                amt = to_money(raw_amt)
                if tk and amt is not None and amt > 0:
                    by_tier.setdefault(tk, []).append(amt)
            for tk, amts in by_tier.items():
                if len(amts) >= 2:
                    cur_rates.setdefault((curr_plan, tk), amts[0])
                    ren_rates.setdefault((ren_plan, tk), amts[1])
                elif len(amts) == 1:
                    ren_rates.setdefault((ren_plan, tk), amts[0])

    return ren_rates, cur_rates, plan_map, plan_names


def parse_renewal(doc: RawDoc) -> RenewalDoc:
    eff, term = _dates(doc)
    rd = RenewalDoc(path=doc.name, effective=eff, termination=term, version_rank=_version_rank(doc))

    has_structured_composite = False
    if (doc.text or "").strip():
        emp_rates, plan_map, member_modal_rates = _parse_member_level_renewal(doc.text)
        if emp_rates:
            rd.employee_rates.update(emp_rates)
            rd.plan_mapping.update(plan_map)
            rd.rates.update(member_modal_rates)
            rd.age_banded = True

        comp_ren, comp_cur, comp_map, comp_names = _parse_composite_renewal_blocks(doc.text)
        if comp_names:
            rd.plan_names.update(comp_names)
        if comp_ren:
            rd.rates.update(comp_ren)
            rd.current_rates.update(comp_cur)
            rd.plan_mapping.update(comp_map)
            has_structured_composite = True
            log.info("Extracted %d composite renewal rate(s) and %d current rate(s) across %d plan(s) from %s",
                     len(comp_ren), len(comp_cur), len({p for p, _ in comp_ren}), doc.name)

    from census_config import LLM_FIRST, USE_LLM_FALLBACK
    if LLM_FIRST and USE_LLM_FALLBACK and (doc.text or "").strip() and not has_structured_composite:
        try:
            log.info("Applying LLM-First semantic extraction for renewal: %s", doc.name)
            from llm_client import extract_renewal_rates
            rates_data = extract_renewal_rates(doc.text)
            for item in rates_data:
                p = clean(item.get("plan"))
                t = std_coverage(clean(item.get("tier")))
                amt = to_money(item.get("rate"))
                if p and t in _TIERS and amt:
                    rd.rates[(p, t)] = amt
            if not rd.effective:
                for item in rates_data:
                    if item.get("effective_date"):
                        eff_cand = parse_date(item["effective_date"])
                        if eff_cand:
                            rd.effective = eff_cand
                            break
            if rd.rates:
                log.info("LLM-First successfully extracted %d renewal rates from %s", len(rd.rates), doc.name)
        except Exception as exc:
            log.warning("LLM-First renewal extraction failed for %s: %s (falling back to table parsers)", doc.name, exc)

    if not rd.rates:
        for t in doc.tables:
            df = t.df
            for i in range(min(25, len(df))):
                labels = [clean(v) for v in df.iloc[i].tolist()]
                if any(re.search(r"\bage\b|age\s*band", l, re.I) for l in labels):
                    rd.age_banded = True
                tiers = _tier_columns(labels)
                if len(tiers) >= 2:                                    # WIDE: plan | EE | ES | EC | FAM
                    plan_col = next((j for j, l in enumerate(labels) if norm_key(l) in FIELD_ALIASES["plan"]
                                     or "plan" in norm_key(l)), None)
                    if plan_col is None:
                        plan_col = next((j for j in range(len(labels)) if j not in tiers.values()), 0)
                    title = ""
                    for k in range(i - 1, -1, -1):
                        cand = clean(df.iloc[k, 0])
                        if cand:
                            title = cand
                            break
                    for _, row in df.iloc[i + 1:].iterrows():
                        plan = clean(row.iloc[plan_col]) or title
                        if not plan or re.match(r"(sub)?total", plan.lower()):
                            continue
                        for tier, col in tiers.items():
                            amt = to_money(row.iloc[col])
                            if amt is not None and amt > 0:
                                rd.rates[(plan, tier)] = amt
                    break

            hdr = detect_header(df.iloc[i:].reset_index(drop=True), max_scan=1)
            if hdr and "plan" in hdr.columns and "coverage" in hdr.columns:   # LONG layout
                rate_col = next((j for j, l in enumerate(hdr.labels) if norm_key(l) in _RENEW_PREM), None)
                if rate_col is None:
                    continue
                for _, row in body(df.iloc[i:].reset_index(drop=True), hdr).iterrows():
                    plan = clean(row.iloc[hdr.columns["plan"]])
                    tier = std_coverage(clean(row.iloc[hdr.columns["coverage"]]))
                    amt = to_money(row.iloc[rate_col])
                    if plan and tier in _TIERS and amt:
                        rd.rates[(plan, tier)] = amt
                break

    if not rd.rates and (doc.text or "").strip():
        rd.rates = _records_from_renewal_text(doc.text)

    if not rd.rates:
        from census_config import USE_LLM_FALLBACK
        if USE_LLM_FALLBACK and (doc.text or "").strip():
            try:
                from llm_client import extract_renewal_rates
                rates_data = extract_renewal_rates(doc.text)
                for item in rates_data:
                    p = clean(item.get("plan"))
                    t = std_coverage(clean(item.get("tier")))
                    amt = to_money(item.get("rate"))
                    if p and t in _TIERS and amt:
                        rd.rates[(p, t)] = amt
                if not rd.effective:
                    for item in rates_data:
                        if item.get("effective_date"):
                            eff_cand = parse_date(item["effective_date"])
                            if eff_cand:
                                rd.effective = eff_cand
                                break
            except Exception as exc:
                log.warning("LLM renewal extraction failed for %s: %s", doc.name, exc)
    rd.plans = sorted({p for p, _ in rd.rates})
    return rd



def select_renewal(docs: list[RenewalDoc], invoice_end: Optional[date], invoice_start: Optional[date]
                   ) -> tuple[Optional[RenewalDoc], list[str]]:
    findings: list[str] = []
    if not docs:
        return None, ["No renewal document was provided. Renewal Premiums were not populated."]
    ref = invoice_end or invoice_start

    # Check if any document represents the current plan year with renewal > 90 days away
    for d in docs:
        if d.termination and ref:
            import datetime
            renewal_date = d.termination + datetime.timedelta(days=1)
            days_away = (renewal_date - ref).days
            if days_away > 90:
                s_str = f"{d.effective:%B %d, %Y}" if d.effective else "current plan year"
                t_str = f"{d.termination:%B %d, %Y}"
                r_str = f"{renewal_date:%B %d, %Y}"
                findings.append(f"Current plan year is {s_str} through {t_str} with a renewal effective date of {r_str}. Renewal premiums are not required as the renewal is more than 90 days away.")
                return None, findings

    future = []
    for d in docs:
        if d.age_banded and not d.rates:
            findings.append(f"{d.path}: age-banded renewal rating could not be mapped; manual review needed.")
            continue
        if not d.rates:
            continue
        if ref and d.effective and d.effective <= ref:
            continue                                   # same/earlier coverage period as the invoice
        if ref and not d.effective:
            findings.append(f"{d.path}: renewal effective date not found; treated as applicable.")
        future.append(d)
    if not future:
        findings.append("No renewal document covering a future coverage period was found. "
                        "Renewal Premiums were not populated.")
        return None, findings
    future.sort(key=lambda d: (d.effective or date.min, d.version_rank, len(d.rates)), reverse=True)
    return future[0], findings
