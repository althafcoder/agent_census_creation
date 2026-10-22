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
        m = re.search(rf"(?:renewal\s*)?effective\s*(?:date)?\s*[:\-]?\s*{_DATE}", text, re.I)
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


def parse_renewal(doc: RawDoc) -> RenewalDoc:
    eff, term = _dates(doc)
    rd = RenewalDoc(path=doc.name, effective=eff, termination=term, version_rank=_version_rank(doc))
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
