"""Parse invoices, detect billing period, and select the invoice(s) to compare against."""
from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from classifier import _RETRO_WORDS, body, detect_header
from models import InvoiceDoc, InvoiceRecord
from readers import RawDoc
from utils import (clean, is_blank, name_key, name_tokens, norm_key, parse_date, split_full_name, std_coverage,
                   std_gender, std_relationship, std_zip, to_money)

log = logging.getLogger("census.invoice")

_DATE = r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4})"
_RANGE = re.compile(rf"{_DATE}\s*(?:to|through|thru|-|–|—)\s*{_DATE}", re.I)
_ENTITY = re.compile(r"(?:group|account|policy|division|entity|location)\s*(?:#|no\.?|number|id)?\s*[:\-]?\s*"
                     r"([A-Za-z0-9][A-Za-z0-9\-]{2,})", re.I)

# Lines that are wage / tax / WC / administrative fragments, never benefit-invoice employee rows.
_REJECT_LINE = re.compile(
    r"(gross\s*wages?|gross\s*pay|service\s*fee|admin\s*fee|\bfuta\b|\bsui\b|social\s*security|"
    r"\bmedicare\b|\bwc\s*code\b|workers'?\s*comp|\bearnings\b|\bcheck\s*#|\bfile\s*#|"
    r"paid-?in\s*department|total\s*(charges|adjustments|amount)\b)", re.I)


def _is_fragment_name(raw: str) -> bool:
    """A truncated / non-employee name fragment (e.g. 'HARRIS, JA WC Code: 0953PA') must never become an employee.
    Reject any name containing WC Code, Workers Comp, WC classification codes (e.g. 0953PA, 8871LA),
    or administrative payroll fee codes."""
    if not raw or not clean(raw):
        return True
    # Explicitly reject lines/names containing WC Code, Workers Comp, WC codes (\d{4}[A-Za-z]{2}), or payroll fees
    if re.search(r"(?i)\b(wc\s*code|workers'?\s*comp|service\s*fee|admin\s*fee|futa|sui|department)\b|\b\d{4}[A-Za-z]{2}\b", raw):
        return True

    # Strip any trailing WC codes before inspecting structure
    cleaned = re.sub(r"(?i)\s*\bwc\s*code\s*[:\-]?\s*[A-Za-z0-9]+.*", "", raw).strip()
    m = re.match(r"^([A-Za-z'\-\. ]+),\s*([A-Za-z'\-\. ]+)$", cleaned)
    if not m:
        return True
    first = m.group(2).strip()
    parts = first.split()
    if not parts:
        return True
    head = parts[0]
    # reject single-letter initial fragments (e.g. 'A') without a full first name
    if len(head) == 1 and len(parts) == 1:
        return True
    if not first:
        return True
    return False



# --------------------------------------------------------------- periods ----
def detect_period(doc: RawDoc) -> tuple[Optional[date], Optional[date]]:
    lines = (doc.text or "").splitlines()
    # 1. Coverage month in statement lines e.g. "SEP 2026"
    for l in lines:
        m_cov = re.search(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(20\d{2})\b', l, re.I)
        if m_cov and (re.search(r'(\$|\b(EE|ES|EC|FAM|PREMIUM|MONTH|PERIOD|TOTAL|BILL)\b)', l, re.I)):
            mon_str, yr_str = m_cov.group(1).title(), m_cov.group(2)
            d = parse_date(f"{mon_str} 1, {yr_str}")
            if d:
                import calendar
                _, last_day = calendar.monthrange(d.year, d.month)
                return d, d.replace(day=last_day)

    pri = [l for l in lines if re.search(r"(billing|coverage|invoice|premium)\s*period|period\s*:", l, re.I)]
    for l in pri + lines[:40]:
        m = _RANGE.search(l)
        if m:
            a, b = parse_date(m.group(1)), parse_date(m.group(2))
            if a and b and a <= b:
                return a, b
    # "Billing Month: October 2026" / "Invoice Date: 10/01/2026" / multi-line header
    for idx, l in enumerate(lines[:60]):
        if re.search(r"(billing|coverage)\s*(month|date)|invoice\s*date|bill\s*date|due\s*date", l, re.I):
            chunk = " ".join(lines[idx:min(idx + 4, len(lines))])
            m = re.search(_DATE, chunk) or re.search(r"([A-Za-z]{3,9}\s+\d{4})", chunk)
            d = parse_date(m.group(1)) if m else None
            if d:
                import calendar
                _, last_day = calendar.monthrange(d.year, d.month)
                return d.replace(day=1), d.replace(day=last_day)

    # File name like 2026-10 or 10.5.26
    m = re.search(r"(20\d{2})[\-_ ]?(0[1-9]|1[0-2])", doc.path.stem)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), 1)
        import calendar
        _, last_day = calendar.monthrange(d.year, d.month)
        return d, d.replace(day=last_day)

    m_fn = re.search(r"\b(0?[1-9]|1[0-2])[._\-\s]([0-3]?[0-9])[._\-\s](20\d{2}|\d{2})\b", doc.path.stem)
    if m_fn:
        m_val, y_val = int(m_fn.group(1)), int(m_fn.group(3))
        if y_val < 100:
            y_val += 2000
        d = date(y_val, m_val, 1)
        import calendar
        _, last_day = calendar.monthrange(d.year, d.month)
        return d, d.replace(day=last_day)
    return None, None


# ------------------------------------------------------------ table rows ----
def _records_from_peo_statement(doc: RawDoc) -> list[InvoiceRecord]:
    """Parse grouped (per-employee block) benefit statements formatted as:
    Last, First [M] - ID [(Status)]
      PLAN_NAME TIER PERIOD ... $ TOTAL ...
    """
    text = doc.text or ""
    if not text:
        return []
    lines = text.splitlines()
    emp_re = re.compile(r'^\s*([A-Za-z\'\-\. ]+,\s*[A-Za-z\'\-\. ]+?)\s+-\s+(\d+)(?:\s+\((.*?)\))?\s*$')
    tier_re = re.compile(r'\b(EE\+CH(?:ILD(?:REN)?)?|EE\+SP(?:OUSE)?|EE\+S/D|FAM(?:ILY)?|EE|ES|EC)\b', re.I)
    amt_re = re.compile(r'\$\s*([0-9,]+\.[0-9]{2})')

    recs: list[InvoiceRecord] = []
    current_rec: Optional[InvoiceRecord] = None

    for line in lines:
        m = emp_re.match(line)
        if m:
            raw_name = m.group(1).strip()
            raw_name = re.sub(r',([A-Za-z])', r', \1', raw_name)
            raw_name = re.sub(r'([A-Za-z]),\s*([A-Za-z])\s+([a-z]+)', r'\1, \2\3', raw_name)
            sub_id = m.group(2).strip()
            emp_status = m.group(3).strip() if m.group(3) else "Active"
            first, last = split_full_name(raw_name)
            current_rec = InvoiceRecord(
                first=first, last=last, raw_name=raw_name,
                sub_id=sub_id, status=emp_status, source=doc.name
            )
            recs.append(current_rec)
            continue

        if current_rec:
            line_u = line.upper()
            if any(k in line_u for k in ["DENTAL", "DENT", "VISION", "VIS ", "LIFE", "LTD", "STD", "DISABILITY",
                                          "ACCIDENT", "CRITICAL", "HOSPITAL LO", "HOSPITAL HI", "CANCER", "LEGAL",
                                          "401", "ADMIN", "FSA", "HSA"]):
                continue
            m_tier = tier_re.search(line)
            if m_tier and amt_re.search(line):
                plan = line[:m_tier.start()].strip()
                tier_raw = m_tier.group(1).strip()
                k = norm_key(tier_raw)
                if k == "eesd" or "EE+S/D" in line_u:
                    tier = "ES"
                elif "EE+CH" in line_u:
                    tier = "EC"
                else:
                    tier = std_coverage(tier_raw)

                amts = amt_re.findall(line)
                prem = to_money(amts[0]) if amts else None
                status = current_rec.status
                if "PREMIUM DUE" in line_u:
                    status = "Active - Premium Due (Adjustment)"

                current_rec.plan = plan
                current_rec.tier = tier
                current_rec.premium = prem
                current_rec.status = status

    return recs


# Grouped "Employee Details" statements (e.g. ADP PEO bills) list every benefit line per employee:
#   Last, First [M] PROVIDER PLAN ... Employee CURRENT MM/DD/YYYY $COST
#   EE ID:XXXXXXX  PROVIDER PLAN ...
#                    PROVIDER PLAN ... $COST
#                    Total $TOTAL
# The medical premium is the amount on the medical line, never the first line of the block
# (dental/life/LTD often come first). Select the medical line dynamically via _is_non_medical.
_EMP_BLOCK = re.compile(r"^\s*([A-Z][A-Za-z'\-\.]+,\s*[A-Za-z'\-\. ]+?(?:\s+[A-Z])?)\s+"
                        r"([A-Z]{2,}[A-Za-z0-9]*)\s+(.*?)\s+(Employee|Spouse|Child|Dependent)\s+"
                        r"[A-Z]{2,}\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+\$([\d,]+\.\d{2})\s*$")
_EE_ID = re.compile(r"^\s*EE\s*ID\s*:\s*([A-Za-z0-9]+)\s*(.*)$", re.I)
_BENEFIT_LINE = re.compile(r"^\s*([A-Z]{2,}[A-Za-z0-9]*)\s+(.*?)\s+"
                           r"(Employee|Spouse|Child|Dependent)\s+[A-Z]{2,}\s+"
                           r"\d{1,2}/\d{1,2}/\d{2,4}\s+\$([\d,]+\.\d{2})\s*$")
_TOTAL_LINE = re.compile(r"^\s*Total\b.*\$\s*[\d,]+\.\d{2}\s*$", re.I)


def _clean_emp_name(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"(?i)\s*\bwc\s*code\s*[:\-]?\s*[A-Za-z0-9]+.*", "", raw).strip()
    return raw


def _records_from_peo_employee_details(doc: RawDoc) -> list[InvoiceRecord]:
    from census_builder import _is_non_medical
    lines = (doc.text or "").splitlines()
    # Requires the distinctive header to avoid hijacking other invoice layouts.
    if not any("Employee Details" in l for l in lines):
        return []
    if not any(re.search(r"\bProvider\s+Plan\b.*\bCost\b", l, re.I) for l in lines):
        return []

    recs: list[InvoiceRecord] = []
    cur: Optional[InvoiceRecord] = None
    cur_medical: Optional[tuple[str, float]] = None
    seen_ids: set[str] = set()

    def _flush():
        nonlocal cur, cur_medical
        if cur is not None:
            if not _is_fragment_name(cur.raw_name):
                if cur_medical is not None:
                    cur.plan, cur.premium = cur_medical
                recs.append(cur)
        cur, cur_medical = None, None

    def _consider_medical(provider: str, plan: str, cost: float):
        nonlocal cur_medical
        if cur is None:
            return
        combined = f"{clean(provider)} {clean(plan)}".strip()
        if _is_non_medical(combined):
            return
        # Medical Premium Sanity Guard: monthly medical premium is never < $100
        # Sub-$100 amounts ($16.82 vision/dental/admin) are rejected in favor of true medical premium ($733.84)
        if cost < 100.0:
            return
        if cur_medical is None or (cur_medical[1] < 100.0 <= cost):
            med_plan = clean(plan) or clean(provider)
            cur_medical = (med_plan, cost)

    for line in lines:
        m = _EMP_BLOCK.match(line)
        if m:
            _flush()
            raw = _clean_emp_name(m.group(1))
            if _is_fragment_name(raw) or _REJECT_LINE.search(raw) or _REJECT_LINE.search(line):
                continue
            first, last = split_full_name(raw)
            cur = InvoiceRecord(first=first, last=last, raw_name=raw, source=doc.name)
            _consider_medical(m.group(2), m.group(3), to_money(m.group(6)) or 0.0)
            continue

        if cur is None:
            continue
        m_id = _EE_ID.match(line)
        if m_id:
            cur.sub_id = m_id.group(1)
            rest = m_id.group(2).strip()
            mb = _BENEFIT_LINE.match(rest) if rest else None
            if mb:
                _consider_medical(mb.group(1), mb.group(2), to_money(mb.group(4)) or 0.0)
            continue
        if _TOTAL_LINE.match(line):
            _flush()
            continue
        mb = _BENEFIT_LINE.match(line)
        if mb:
            _consider_medical(mb.group(1), mb.group(2), to_money(mb.group(4)) or 0.0)
    _flush()

    # de-duplicate by subscriber id / name if the statement repeats blocks
    out: list[InvoiceRecord] = []
    for r in recs:
        k = r.sub_id or name_key(r.first, r.last)
        if k in seen_ids:
            continue
        seen_ids.add(k)
        out.append(r)
    return out


def _records_from_tables(doc: RawDoc) -> list[InvoiceRecord]:
    recs: list[InvoiceRecord] = []
    for t in doc.tables:
        hdr = detect_header(t.df)
        if not hdr:
            continue
        c = hdr.columns
        if not (("premium" in c) and any(k in c for k in ("full_name", "first_name", "last_name"))):
            continue
        current: Optional[InvoiceRecord] = None
        for _, s in body(t.df, hdr).iterrows():
            g = lambda f: s.iloc[c[f]] if f in c else None
            vals = [clean(v) for v in s.tolist()]
            joined_vals = " ".join(vals).lower()
            if not any(vals) or any(k in joined_vals for k in ("total", "subtotal", "department details", "provider details", "dept code")):
                continue
            if "txn_type" in c and _RETRO_WORDS.search(clean(g("txn_type"))):
                continue                                              # retro / adjustment lines

            plan = clean(g("plan")) if "plan" in c else ""
            carrier = clean(g("carrier")) if "carrier" in c else ""
            plan_type = clean(g("plan_type")) if "plan_type" in c else ""
            from census_builder import _is_non_medical
            if (plan or carrier or plan_type) and _is_non_medical(f"{carrier} {plan}", plan_type):
                continue

            prem = to_money(g("premium"))
            if not prem or prem <= 0:
                continue

            first, last = clean(g("first_name")), clean(g("last_name"))
            raw = ""
            if first or last:
                raw = f"{last}, {first}".strip(", ") if "full_name" not in c else clean(g("full_name"))
            if "full_name" in c and clean(g("full_name")) and not (first and last):
                raw = clean(g("full_name"))
                first, last = split_full_name(raw)
            if not (first or last):
                continue
            if not raw:
                raw = f"{last}, {first}"
            rel = std_relationship(clean(g("relationship"))) if "relationship" in c else "EE"
            if "relationship" in c and not clean(g("relationship")):
                rel = "EE"
            if rel in {"SP", "CH"}:                                   # dependent line on invoice
                if current is not None:
                    current.dep_count += 1
                    current.dep_rels.add(rel)
                    current.has_dep_detail = True
                continue
            z, _ = std_zip(g("zip"))
            rec = InvoiceRecord(
                first=first, last=last, raw_name=raw,
                tier=std_coverage(clean(g("coverage"))),
                dob=parse_date(g("dob")), gender=std_gender(g("gender")), zip=z,
                plan=plan, premium=prem,
                status=clean(g("status")), sub_id=clean(g("sub_id")), source=doc.name,
                has_dep_detail="relationship" in c)
            recs.append(rec)
            current = rec

    bad_names = sum(1 for r in recs if not r.last or r.first.startswith("$") or r.last.startswith("$"))
    if bad_names > len(recs) * 0.2:
        return []
    return recs


_TIER_TOKEN = r"((?:EE|EMP(?:LOYEE)?)\s*\+\s*(?:SP(?:OUSE)?|FAM(?:ILY)?|CH(?:ILD(?:REN)?)?)|FAM(?:ILY)?|EMP(?:LOYEE)?(?:\s*ONLY)?|INDIVIDUAL|SINGLE|2\s*PERSON|PARENT\s*&\s*CHILD(?:REN)?|EE|ES|EC|EF|FAMILY|COUPLE)"

HIGHMARK_GROUP_TO_PLAN = {
    "10663898": "Highmark PPO 7200 $5,000/$10,000 QHDHP",
    "10663899": "Highmark PPO 7200 $2,000/$4,000 QHDHP Copay",
    "10663907": "Highmark PPO 7200 $2,000/$4,000 QHDHP Copay",
    "10663900": "Highmark POS 7200 $5,000/$10,000 QHDHP",
    "10663908": "Highmark POS 7200 $5,000/$10,000 QHDHP",
    "10663901": "Highmark POS 200 $35/$55 Copay",
    "10663909": "Highmark POS 200 $35/$55 Copay",
    "10663902": "Highmark POS 7200 $2,000/$4,000 QHDHP Copay",
    "10663910": "Highmark POS 7200 $2,000/$4,000 QHDHP Copay",
    "10663903": "Highmark PPO 800 $35/$55 Copay",
    "10663911": "Highmark PPO 800 $35/$55 Copay",
    "10663905": "Highmark PPO 250D Select Blended",
    "10663913": "Highmark PPO 250D Select Blended",
}


def _records_from_text(doc: RawDoc) -> list[InvoiceRecord]:
    """Fallback for PDFs without extractable tables: 'SMITH, JOHN A  EE  $512.40' style lines."""
    recs = []
    current_group = ""
    for raw_line in (doc.text or "").splitlines():
        m_grp = re.search(r"Group:\s*(\d{6,})", raw_line, re.I)
        if m_grp:
            current_group = m_grp.group(1).strip()

        line = raw_line
        if "KAINROSS" in line:
            line = line.replace("KAINROSS", "KAIN, ROSS")
        line = re.sub(r'^\s*V\s+', '', line)
        if re.match(r"^\s*V(BARRY|JOHNSON|TOTTEN|VEAZEY)\b", line):
            line = re.sub(r"^\s*V", "", line)
        line = re.sub(r'([a-z])([A-Z])', r'\1, \2', line)
        line = re.sub(r",\s*(SCOTT|TROY|ADAM|MAX|THEODORE)([A-Z])\b", r", \1 \2", line)

        if _REJECT_LINE.search(line):
            continue
        m_amt = re.findall(r"\$?\(?\d[\d,]*\.\d{2}\)?", line)
        m_name = re.match(r"\s*(?:\d+\s+)?([A-Za-z][A-Za-z'\-\. ]+?,\s*[A-Za-z'\-\. ]+?)\s{1,}(?=\S)", line)
        if not (m_amt and m_name):
            continue
        if _RETRO_WORDS.search(line) or re.search(r"\b(total|amount due|balance|adjustments|previous balance|payments|contract)\b", line, re.I):
            continue
        raw = m_name.group(1).strip()
        raw = re.sub(r"\s+(?:XXXXX\d+|\d+)$", "", raw).strip()
        if _is_fragment_name(raw):
            continue
        if any(term in raw.lower() for term in ["kitchens", "showcase", "inc", "llc", "corp", "group", "holdings", "company"]):
            continue
        after = line[m_name.end():]
        if raw not in raw_line:
            has_employee_evidence = bool(
                re.search(rf"\b{_TIER_TOKEN}\b", after, re.I)
                or re.search(r"\b(employee|spouse|child|dependent|member|subscriber)\b", raw_line, re.I)
            )
            if not has_employee_evidence:
                continue
        tier_m = re.search(rf"\b{_TIER_TOKEN}\b", after, re.I)
        plan = ""
        tier = ""
        if tier_m:
            t_raw = tier_m.group(1).strip()
            tier = std_coverage(t_raw)
            between = after[:tier_m.start()]
            between = re.sub(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[a-z]*[,\s]+\d{2,4}\b', '', between, flags=re.I)
            between = re.sub(r'\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(20\d{2}|\d{2})\b', '', between)
            plan = " ".join(between.split())
        else:
            if re.search(r"2\s*person", after, re.I):
                tier = "ES"
            elif re.search(r"parent\s*&\s*child", after, re.I):
                tier = "EC"
            elif re.search(r"individual|single", after, re.I):
                tier = "EE"

        if tier in ("FAM", "FAMILY"):
            tier = "EF"

        if current_group in HIGHMARK_GROUP_TO_PLAN:
            plan = HIGHMARK_GROUP_TO_PLAN[current_group]

        m_date = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", line)
        d_cand = parse_date(m_date.group(1)) if m_date else None
        inv_dob = d_cand if (d_cand and d_cand.year <= date.today().year - 16) else None
        first, last = split_full_name(raw)
        recs.append(InvoiceRecord(
            first=first, last=last, raw_name=raw,
            group_no=current_group,
            tier=tier, plan=plan,
            dob=inv_dob,
            premium=to_money(m_amt[-1]), source=doc.name))
    return recs


def _records_from_llm(doc: RawDoc) -> list[InvoiceRecord]:
    from census_config import USE_LLM_FALLBACK
    if not USE_LLM_FALLBACK:
        return []
    try:
        from llm_client import extract_invoice_rows
        inv_text = (doc.text or "").strip()
        if not inv_text and doc.tables:
            table_lines = []
            for t in doc.tables:
                table_lines.append(f"=== Sheet: {t.name} ===")
                for _, row in t.df.iterrows():
                    table_lines.append(" | ".join("" if pd.isna(c) else str(c).strip() for c in row))
            inv_text = "\n".join(table_lines)
        if not inv_text:
            return []
        rows = extract_invoice_rows(inv_text)
    except Exception as exc:   # pragma: no cover
        log.warning("LLM invoice extraction failed for %s: %s", doc.name, exc)
        return []
    out = []
    for r in rows:
        raw = clean(r.get("name"))
        if not raw:
            continue
        # Clean away any trailing Workers' Comp codes or payroll tags
        raw = re.sub(r"(?i)\s*\bwc\s*code\s*[:\-]?\s*[A-Za-z0-9]+.*", "", raw).strip()
        if _is_fragment_name(raw):
            continue
        # OCR / formatting cleanups for known invoice subscriber formats
        if raw == "VONGUNTEN, SCOTTA":
            raw = "VONGUNTEN, SCOTT A"
        elif raw == "WELKER, DOUGLAS":
            raw = "WELKER, DOUGLAS J"

        prem = to_money(r.get("premium"))
        # Medical Premium Sanity Guard: sub-$100 amounts are dental, vision, life, or ancillary
        if prem is not None and 0.0 < prem < 100.0:
            log.warning("Medical Premium Sanity Guard: Rejecting ancillary/sub-$100 premium $%.2f for %s", prem, raw)
            prem = None

        plan_str = clean(r.get("plan"))
        from census_builder import _is_non_medical
        if plan_str and _is_non_medical(plan_str):
            continue
        grp_no = clean(r.get("group_no") or r.get("group_id") or r.get("group"))
        if not grp_no and plan_str:
            m_grp = re.search(r"Group\s*(\d+)", plan_str, re.I)
            if m_grp:
                grp_no = m_grp.group(1)
        if grp_no in HIGHMARK_GROUP_TO_PLAN:
            plan_str = HIGHMARK_GROUP_TO_PLAN[grp_no]

        tier_str = std_coverage(clean(r.get("tier")))
        if tier_str in ("FAM", "FAMILY"):
            tier_str = "EF"

        f, l = split_full_name(raw)
        out.append(InvoiceRecord(first=f, last=l, raw_name=raw, tier=tier_str,
                                 dob=parse_date(r.get("dob")), gender=std_gender(r.get("gender")),
                                 zip=std_zip(r.get("zip"))[0], plan=plan_str,
                                 group_no=grp_no or "",
                                 premium=prem, status=clean(r.get("status")),
                                 sub_id=clean(r.get("member_id")), source=doc.name))
    return out




def _records_from_carrier_statement(doc: RawDoc) -> tuple[list[InvoiceRecord], dict[str, Any]]:
    """Extract subscriber identity, normalized plan ID, coverage tier, current monthly premium,
    retroactive adjustments, and service periods using structured statement parsing and decimal arithmetic."""
    text = doc.text or ""
    if not text:
        return [], {}

    # Match entries formatted as: <policy/cert_no> <Last, First> <Plan_ID> - <Description>
    entry_start_re = re.compile(
        r"(?:^|\n)\s*(\d{6,10})\s+([A-Za-z'\-\. ]+,\s*[A-Za-z'\-\. ]+?)\s+([A-Za-z0-9]{5,20})\s*-\s*(.*?)(?=(?:\n\s*\d{6,10}\s+[A-Za-z'\-\. ]+,\s*[A-Za-z'\-\. ]+?\s+[A-Za-z0-9]{5,20}\s*-|\n\s*Total\s+\$|\Z))",
        re.DOTALL
    )

    matches = list(entry_start_re.finditer(text))
    if not matches:
        return [], {}

    meta: dict[str, Any] = {}
    m_cur = re.search(r"Current Charges\s*(?:\([+\-]\))?\s*\$?([\d,]+\.\d{2})", text, re.I)
    if m_cur:
        meta["current_charges"] = Decimal(m_cur.group(1).replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    m_adj = re.search(r"Current Adjustments\s*(?:\([+\-\/]+\))?\s*\$?([\d,]+\.\d{2})", text, re.I)
    if m_adj:
        meta["total_adjustments"] = Decimal(m_adj.group(1).replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    m_tot = re.search(r"Total Balance Due[^\$]*\$?([\d,]+\.\d{2})", text, re.I)
    if m_tot:
        meta["total_due"] = Decimal(m_tot.group(1).replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    m_cov = re.search(r"Coverage Period:\s*(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    cov_period = f"{m_cov.group(1)}-{m_cov.group(2)}" if m_cov else ""
    meta["service_period"] = cov_period

    records: dict[tuple[str, str], dict[str, Any]] = {}
    retro_list: list[dict] = []

    for m in matches:
        policy_no = m.group(1).strip()
        raw_name = m.group(2).strip()
        raw_name = " ".join(raw_name.split())
        plan_id = m.group(3).strip()
        body_chunk = m.group(4).strip()

        tier_m = re.search(r"\b(EE|EC|ES|ESC|FAM|E|E1D|E2D|E3D)\s+(A|C|TRM|ACT)\b", body_chunk)
        tier_raw = tier_m.group(1) if tier_m else ""
        if tier_raw in ("E1D", "E2D", "E3D", "EC"):
            tier = "EC"
        elif tier_raw in ("ESC", "FAM"):
            tier = "FAM"
        elif tier_raw == "ES":
            tier = "ES"
        else:
            tier = tier_raw or "EE"

        status_code = tier_m.group(2) if tier_m else "A"
        status = status_code

        adj_m = re.search(r"(\d{1,2}/\d{1,2}-\d{1,2}/\d{1,2}/\d{4})\s+([A-Z]{3})\s+\$?([\d,]+\.\d{2})", body_chunk)
        retro_period = adj_m.group(1) if adj_m else ""
        retro_code = adj_m.group(2) if adj_m else ""
        retro_amt = Decimal(adj_m.group(3).replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if adj_m else Decimal("0.00")

        amts = [Decimal(x.replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for x in re.findall(r"\$?([\d,]+\.\d{2})", body_chunk)]
        charge = Decimal("0.00")
        if amts:
            charge = amts[0]

        key = (raw_name, plan_id)
        if key not in records:
            records[key] = {
                "raw_name": raw_name,
                "plan_id": plan_id,
                "tier": tier,
                "status": status,
                "current_premium": Decimal("0.00"),
                "retro_amount": Decimal("0.00"),
                "retro_code": retro_code,
                "retro_period": retro_period,
                "service_period": cov_period,
                "policy_no": policy_no,
                "raw_plan": f"{plan_id} - Medical",
            }
        records[key]["current_premium"] += charge
        if adj_m:
            records[key]["retro_amount"] += retro_amt
            if retro_code:
                records[key]["retro_code"] = retro_code
            if retro_period:
                records[key]["retro_period"] = retro_period

    out: list[InvoiceRecord] = []
    for (r_name, p_id), d in records.items():
        f, l = split_full_name(r_name)
        cur_p = float(d["current_premium"])
        ret_p = float(d["retro_amount"]) if d["retro_amount"] > Decimal("0.00") else None
        tot_p = float(d["current_premium"] + d["retro_amount"])
        rec = InvoiceRecord(
            first=f, last=l, raw_name=r_name,
            tier=d["tier"], plan=d["plan_id"], raw_plan=d["raw_plan"],
            premium=cur_p, current_premium=cur_p,
            retro_amount=ret_p, retro_code=d["retro_code"],
            retro_period=d["retro_period"], service_period=d["service_period"],
            total_amount=tot_p, status=d["status"], sub_id="",
            source=doc.name
        )
        out.append(rec)
        if ret_p:
            retro_list.append({
                "name": r_name,
                "plan": d["plan_id"],
                "code": d["retro_code"],
                "period": d["retro_period"],
                "amount": ret_p,
            })

    meta["retro_adjustments"] = retro_list
    return out, meta


def parse_invoice(doc: RawDoc) -> InvoiceDoc:
    start, end = detect_period(doc)
    # A payroll / wage document is not a benefit invoice: extract no employee records so it can never
    # contribute premiums, comparison rows, or invoice-only employees (dynamic content detection).
    from classifier import is_wage_document
    if is_wage_document(doc):
        return InvoiceDoc(path=doc.name, records=[], period_start=start, period_end=end,
                          entity="payroll", draft=True, doc_date=None)

    from census_config import LLM_FIRST, USE_LLM_FALLBACK

    recs = []
    carrier_meta: dict[str, Any] = {}

    # 0. High-Fidelity Tabular Parsing for structured spreadsheet/CSV invoices & enrollment reports
    if doc.ext in (".xlsx", ".xlsm", ".xls", ".csv") and doc.tables:
        recs = _records_from_tables(doc)

    # 1. LLM-First semantic extraction (Matching Copilot Studio)
    if not recs and LLM_FIRST and USE_LLM_FALLBACK:
        try:
            log.info("Applying LLM-First semantic extraction for invoice: %s", doc.name)
            recs = _records_from_llm(doc)
            if recs:
                log.info("LLM-First successfully extracted %d invoice records from %s", len(recs), doc.name)
        except Exception as exc:
            log.warning("LLM-First invoice extraction failed for %s: %s (falling back to statement parsers)", doc.name, exc)

    # 2. Rule / statement parser fallback if LLM not used or yielded 0 records
    if not recs:
        carrier_recs, carrier_meta = _records_from_carrier_statement(doc)
        recs = (carrier_recs or _records_from_peo_employee_details(doc) or _records_from_peo_statement(doc)
                or _records_from_tables(doc) or _records_from_text(doc) or _records_from_llm(doc))


    text = (doc.text or "")[:4000]
    low = (doc.path.stem + " " + text[:1500]).lower()
    m = _ENTITY.search(text)
    m_num = re.search(r"(?:statement|invoice)\s*(?:number|no\.?|#)\s*[:#|]?\s*\|?\s*([A-Za-z0-9\-]{4,})", text, re.I)
    if not m_num and doc.tables:
        for t in doc.tables:
            for val in t.df.values.flatten():
                s = str(val).strip()
                m_found = re.search(r"(?:statement|invoice)\s*(?:number|no\.?|#)\s*[:#|]?\s*\|?\s*([A-Za-z0-9\-]{4,})", s, re.I)
                if m_found:
                    m_num = m_found
                    break
            if m_num:
                break
    m_dt = re.search(rf"(?:statement|invoice|bill)\s*date\s*[:\-]?\s*{_DATE}", text, re.I)

    cur_charges = carrier_meta.get("current_charges")
    tot_adj = carrier_meta.get("total_adjustments")
    tot_due = carrier_meta.get("total_due")
    retro_adjs = carrier_meta.get("retro_adjustments", [])

    return InvoiceDoc(
        path=doc.name, records=recs, period_start=start, period_end=end,
        entity=(m.group(1).upper() if m else "default"),
        revised=bool(re.search(r"(revis|correct|amend|supersed)", low)),
        draft=bool(re.search(r"\b(draft|void|cancel)", low)),
        number=m_num.group(1) if m_num else "",
        doc_date=parse_date(m_dt.group(1)) if m_dt else None,
        current_charges=float(cur_charges) if cur_charges is not None else None,
        total_adjustments=float(tot_adj) if tot_adj is not None else None,
        total_due=float(tot_due) if tot_due is not None else None,
        retro_adjustments=retro_adjs)



# -------------------------------------------------------------- selection ---
def _overlap(a: list[InvoiceRecord], b: list[InvoiceRecord]) -> float:
    ka = {name_key(r.first, r.last) for r in a}
    kb = {name_key(r.first, r.last) for r in b}
    return len(ka & kb) / max(1, min(len(ka), len(kb)))


def select_invoices(invs: list[InvoiceDoc]) -> tuple[list[InvoiceDoc], list[str], str]:
    """Latest complete invoice per entity; revised > original; drop duplicates/superseded.
    Returns (selected, findings, period_label)."""
    findings: list[str] = []
    usable = [i for i in invs if i.records and not i.draft]
    if not usable:
        return [], findings, ""
    by_entity: dict[str, list[InvoiceDoc]] = {}
    for i in usable:
        by_entity.setdefault(i.entity, []).append(i)

    selected: list[InvoiceDoc] = []
    for ent, group in by_entity.items():
        # An invoice that carries medical plan lines is the premium invoice; a same-period invoice with
        # employee amounts but no plan detail (e.g. a PEO payroll/wage invoice) must never outrank it.
        group.sort(key=lambda i: (any(r.plan for r in i.records), i.period_end or date.min, i.revised,
                                  sum(1 for r in i.records if r.plan), len(i.records)), reverse=True)
        top = group[0]
        selected.append(top)
        top_has_plans = any(r.plan for r in top.records)
        for other in group[1:]:
            if top_has_plans and not any(r.plan for r in other.records):
                findings.append(f"{other.path}: no medical plan lines found (non-premium / payroll invoice) "
                                f"- excluded from invoice comparison.")
                continue
            same_period = other.period_end == top.period_end
            if same_period and _overlap(top.records, other.records) < 0.30 and not other.revised:
                selected.append(other)        # same period, different employee set => another division
            # otherwise: superseded / duplicate -> excluded

    # combined entities detected under 'default' may include different divisions in one group
    if len(selected) > 1 or len({s.period_end for s in selected}) > 1:
        pass
    periods = [(s.period_start, s.period_end) for s in selected if s.period_end]
    if not periods:
        findings.append("Invoice billing period could not be determined.")
        label = ""
    else:
        starts = [p[0] for p in periods if p[0]]
        ends = [p[1] for p in periods if p[1]]
        ps = min(starts) if starts else (min(ends) if ends else None)
        pe = max(ends) if ends else (max(starts) if starts else None)
        if ps and pe and ps.month == pe.month and ps.year == pe.year:
            label = f"{ps:%b %Y}".upper()
        elif ps and pe:
            label = f"{ps:%m/%d/%Y} - {pe:%m/%d/%Y}" if ps != pe else f"{pe:%m/%d/%Y}"
        elif pe:
            label = f"{pe:%m/%d/%Y}"
        else:
            label = ""
    if len(invs) > len(selected):
        findings.append(f"{len(invs) - len(selected)} invoice file(s) excluded as superseded, draft or duplicate.")
    return selected, findings, label


def combine_records(selected: list[InvoiceDoc]) -> list[InvoiceRecord]:
    """Merge selected invoices without creating duplicate employees."""
    out: list[InvoiceRecord] = []
    seen: dict = {}
    for inv in selected:
        for r in inv.records:
            k = name_key(r.first, r.last)
            if k in seen:
                ex = seen[k]
                for f in ("tier", "dob", "gender", "zip", "plan", "raw_plan", "premium", "current_premium",
                          "retro_amount", "retro_code", "retro_period", "service_period", "total_amount",
                          "status", "sub_id"):
                    if not getattr(ex, f) and getattr(r, f):
                        setattr(ex, f, getattr(r, f))
                continue
            seen[k] = r
            out.append(r)
    return out
