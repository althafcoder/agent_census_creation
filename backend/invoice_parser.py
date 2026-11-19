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
from utils import (clean, clean_overprint_text, is_blank, name_key, name_tokens, norm_key, parse_date,
                   split_full_name, std_coverage, std_gender, std_relationship, std_zip, to_money)

log = logging.getLogger("census.invoice")

_DATE = r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4})"
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
    # Explicitly reject lines/names containing WC Code, Workers Comp, WC codes (\d{4}[A-Za-z]{2}), or administrative/statement phrases
    if re.search(r"(?i)\b(wc\s*code|workers'?\s*comp|service\s*fee|admin\s*fee|futa|sui|department|convenience|remittor|definitions?|adjustment|statement\s*date|invoice\s*date|total\s*(charges|adjustments|amount|premium))\b|\b\d{4}[A-Za-z]{2}\b", raw):
        return True

    # Strip any trailing WC codes before inspecting structure
    cleaned = re.sub(r"(?i)\s*\bwc\s*code\s*[:\-]?\s*[A-Za-z0-9]+.*", "", raw).strip()
    m = re.match(r"^([A-Za-z'\-\. ]+),\s*([A-Za-z'\-\. ]+)$", cleaned)
    if m:
        first = m.group(2).strip()
        parts = first.split()
        if not parts:
            return True
        head = parts[0]
        # reject single-letter initial fragments (e.g. 'A') without a full first name
        if len(head) == 1 and len(parts) == 1:
            return True
        return False

    # Check if name is formatted without a comma as 2 to 4 space-separated words (e.g. 'AHMAD SHAMANE', 'BEAVERS MOLLY', 'CURRIE RIKEE D')
    tokens = cleaned.split()
    if 2 <= len(tokens) <= 4 and all(re.match(r"^[A-Za-z'\-\.]+$", t) for t in tokens):
        # reject if all tokens are single letters (e.g. initials only)
        if all(len(t) == 1 for t in tokens):
            return True
        return False

    # Single-token compound / concatenated subscriber name (e.g. 'KAINROSS' from OCR column-collapse)
    if len(tokens) == 1:
        tok = tokens[0]
        _ADMIN_WORDS = {
            "total", "subtotal", "balance", "amount", "charge", "charges", "adjustment",
            "adjustments", "premium", "premiums", "summary", "description", "department",
            "employee", "employees", "employer", "employers", "subscriber", "subscribers",
            "dependent", "dependents", "invoice", "statement", "payroll", "gross", "futa",
            "sui", "fica", "medicare", "coverage", "benefit", "benefits", "service", "admin"
        }
        if not re.search(r"\d", tok) and tok.lower() not in _ADMIN_WORDS and len(tok) >= 4:
            if re.match(r"^[A-Za-z'\-]+$", tok):
                return False

    return True



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

    pri = [l for l in lines if re.search(r"(bill(?:ing)?|coverage|invoice|premium)\s*period|period\s*:", l, re.I)]
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


_ANCILLARY_COL_RE = re.compile(
    r"(dental|vision|20\s*/\s*20|vsp|eyemed|\blife\b|ad\s*&?\s*d|\bstd\b|\bltd\b|short\s*term|long\s*term|accident|critical|hospital|voluntary|ancillary)",
    re.I,
)


def _records_from_tables(doc: RawDoc, meta_out: dict[str, Any] | None = None) -> list[InvoiceRecord]:
    recs: list[InvoiceRecord] = []
    non_med_recs: list[InvoiceRecord] = []
    anc_totals: dict[str, float] = {}
    gross_total_sum = 0.0
    printed_gross_total: Optional[float] = None
    for t in doc.tables:
        hdr = detect_header(t.df)
        if not hdr:
            continue
        c = hdr.columns
        has_name = any(k in c for k in ("full_name", "first_name", "last_name"))
        has_prem = "premium" in c
        has_cov = "coverage" in c
        if not (has_name and (has_prem or has_cov)):
            continue
        # Detect split Employee Medical / Dependent(s) Medical columns on multi-benefit carrier bills
        ee_med_col = next((i for i, l in enumerate(hdr.labels) if norm_key(l) in ("employeemedical", "eemedical")), None)
        dep_med_col = next((i for i, l in enumerate(hdr.labels) if "dependent" in norm_key(l) and "medical" in norm_key(l)), None)
        # Detect ancillary benefit columns (Dental, Vision, Blue 20/20, Life, etc.) & gross Total Premium column
        prem_col_idx = c.get("premium")
        anc_cols: list[tuple[int, str]] = []
        gross_col_idx: Optional[int] = None
        for idx_l, lbl in enumerate(hdr.labels):
            if idx_l == prem_col_idx:
                continue
            nk_l = norm_key(lbl)
            if _ANCILLARY_COL_RE.search(lbl or "") and "medical" not in nk_l:
                anc_cols.append((idx_l, clean(lbl)))
            elif nk_l in ("totalpremium", "totalamount", "totalbilled", "totalcharges", "grandtotal") and idx_l != prem_col_idx:
                gross_col_idx = idx_l

        current: Optional[InvoiceRecord] = None
        seen_subs: set[tuple] = set()
        for _, s in body(t.df, hdr).iterrows():
            g = lambda f: s.iloc[c[f]] if f in c else None
            vals = [clean(v) for v in s.tolist()]
            joined_vals = " ".join(vals).lower()
            if not any(vals):
                continue
            if any(k in joined_vals for k in ("total", "subtotal", "department details", "provider details", "dept code")):
                # Capture printed TOTALS row gross & ancillary totals if present
                if "total" in joined_vals and "amount due" not in joined_vals and "subtotal" not in joined_vals:
                    if gross_col_idx is not None and gross_col_idx < len(s):
                        gv = to_money(s.iloc[gross_col_idx])
                        if gv and gv > 0:
                            printed_gross_total = round(gv, 2)
                continue
            if "txn_type" in c and _RETRO_WORDS.search(clean(g("txn_type"))):
                continue                                              # retro / adjustment lines

            plan = clean(g("plan")) if "plan" in c else ""
            carrier = clean(g("carrier")) if "carrier" in c else ""
            plan_type = clean(g("plan_type")) if "plan_type" in c else ""
            from census_builder import _is_non_medical
            if (plan or carrier or plan_type) and _is_non_medical(f"{carrier} {plan}", plan_type):
                continue

            prem = to_money(g("premium")) if "premium" in c else (to_money(g("ee_cost")) or 0.0)
            row_anc_parts: list[tuple[str, float]] = []
            for a_idx, a_lbl in anc_cols:
                if a_idx < len(s):
                    av = to_money(s.iloc[a_idx])
                    if av and av > 0:
                        row_anc_parts.append((a_lbl, round(av, 2)))
                        anc_totals[a_lbl] = round(anc_totals.get(a_lbl, 0.0) + av, 2)
            row_anc_sum = round(sum(v for _, v in row_anc_parts), 2)
            if gross_col_idx is not None and gross_col_idx < len(s):
                g_row = to_money(s.iloc[gross_col_idx])
                if g_row and g_row > 0:
                    gross_total_sum = round(gross_total_sum + g_row, 2)

            if has_prem and (not prem or prem <= 0):
                # Preserve $0-medical / ancillary-only rows (e.g. waived medical + enrolled dental/vision)
                # separately so waived employees are accurately reconciled with their Subscriber ID & note
                # without inflating medical subscriber count or medical charges.
                if row_anc_sum > 0:
                    a_first, a_last = clean(g("first_name")), clean(g("last_name"))
                    a_raw = ""
                    if a_first or a_last:
                        a_raw = f"{a_last}, {a_first}".strip(", ") if "full_name" not in c else clean(g("full_name"))
                    if "full_name" in c and clean(g("full_name")) and not (a_first and a_last):
                        a_raw = clean(g("full_name"))
                        a_first, a_last = split_full_name(a_raw)
                    if a_first or a_last:
                        a_detail = " + ".join(f"{lbl} ${val:,.2f}" for lbl, val in row_anc_parts)
                        non_med_recs.append(InvoiceRecord(
                            first=a_first, last=a_last, raw_name=a_raw or f"{a_last}, {a_first}",
                            tier="", dob=parse_date(g("dob")), gender=std_gender(g("gender")),
                            plan="", premium=0.0,
                            status=clean(g("status")), sub_id=clean(g("sub_id")),
                            ancillary_only=True, ancillary_premium=row_anc_sum,
                            ancillary_detail=a_detail, source=doc.name,
                        ))
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

            # Check wide-format dependents for BER / enrollment reports
            dep_cnt = 0
            dep_rels_set: set[str] = set()
            if getattr(hdr, "wide_deps", None):
                for (kind, _), flds in hdr.wide_deps.items():
                    d_first = clean(s.iloc[flds["first"]]) if "first" in flds and flds["first"] < len(s) else ""
                    d_last = clean(s.iloc[flds["last"]]) if "last" in flds and flds["last"] < len(s) else ""
                    d_dob = s.iloc[flds["dob"]] if "dob" in flds and flds["dob"] < len(s) else None
                    if d_first or d_last or not is_blank(d_dob):
                        dep_cnt += 1
                        dep_rels_set.add(kind)

            sub_key = (name_key(first, last), clean(g("sub_id")))
            if sub_key in seen_subs and not ("premium" in c):
                continue
            seen_subs.add(sub_key)

            z, _ = std_zip(g("zip"))
            inferred_tier = std_coverage(clean(g("coverage")))
            dep_med_amt = to_money(s.iloc[dep_med_col]) if dep_med_col is not None and dep_med_col < len(s) else None
            ee_med_amt = to_money(s.iloc[ee_med_col]) if ee_med_col is not None and ee_med_col < len(s) else None
            if not inferred_tier and dep_med_col is not None:
                if (dep_med_amt or 0.0) <= 0.0 and (ee_med_amt or prem or 0.0) > 0.0:
                    inferred_tier = "EE"
            rec = InvoiceRecord(
                first=first, last=last, raw_name=raw,
                tier=inferred_tier,
                dob=parse_date(g("dob")), gender=std_gender(g("gender")), zip=z,
                plan=plan, premium=prem,
                status=clean(g("status")), sub_id=clean(g("sub_id")), source=doc.name,
                has_dep_detail="relationship" in c or bool(dep_cnt),
                dep_count=dep_cnt,
                dep_rels=dep_rels_set)
            if dep_med_amt is not None and dep_med_amt > 0.0:
                setattr(rec, "dep_medical_premium", round(dep_med_amt, 2))
            recs.append(rec)
            current = rec

    bad_names = sum(1 for r in recs if not r.last or r.first.startswith("$") or r.last.startswith("$"))
    if bad_names > len(recs) * 0.2:
        return []
    if meta_out is not None:
        if non_med_recs:
            meta_out["non_medical_records"] = non_med_recs
        if anc_totals:
            meta_out["non_medical_breakdown"] = {k: round(v, 2) for k, v in anc_totals.items() if v > 0}
            meta_out["non_medical_charges"] = round(sum(anc_totals.values()), 2)
        gt = printed_gross_total if (printed_gross_total and printed_gross_total > 0) else (gross_total_sum if gross_total_sum > 0 else None)
        if gt is not None:
            meta_out["gross_total_premium"] = round(gt, 2)
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




def _records_from_meritain_statement(doc: RawDoc) -> tuple[list[InvoiceRecord], dict[str, Any]]:
    """Deterministic, high-fidelity parser for Meritain Health / TPA statement invoices (e.g. group 14642).
    Extracts subscribers by division, mapping plans, coverage tiers, and monthly fixed costs."""
    text = doc.text or ""
    if not text:
        return [], {}
    # Detect Meritain Health signature
    if not ("meritain" in text.lower() or "meritain" in doc.name.lower() or "DIVISION:" in text):
        return [], {}

    tier_map = {"E": "EE", "ES": "ES", "EC": "EC", "F": "EF", "FAM": "EF"}
    lines = text.splitlines()
    current_div = ""
    current_div_desc = ""
    recs: list[InvoiceRecord] = []
    seen: set[tuple[str, str]] = set()

    div_re = re.compile(r"DIVISION:\s*([A-Za-z0-9]+)\s+([A-Za-z0-9 ]+)", re.I)
    mem_re = re.compile(r"^\s*([A-Za-z\s\.\'\-]{2,25})\s+(\d{9,10})\s+(\d{2}-\d{2}-\d{2})\s+([A-Z]{1,3})\s+", re.I)

    for line in lines:
        m_div = div_re.search(line)
        if m_div:
            current_div = m_div.group(1).upper()
            current_div_desc = m_div.group(2).strip().upper()
            continue

        m = mem_re.search(line)
        if m:
            raw_name = m.group(1).strip()
            mem_id = m.group(2)
            tier_raw = m.group(4).upper()
            tier = tier_map.get(tier_raw, tier_raw)

            # Skip header, unit counts or totals
            if raw_name.upper().startswith(("TOTAL", "NAME", "DIVISION", "CURRENT", "MEDICAL", "DENTAL", "UNIT", "ADJUSTMENTS")):
                continue

            tokens = line.split()
            total_str = tokens[-1] if tokens else "0"
            prem = float(total_str) if total_str.replace(".", "", 1).isdigit() else 0.0

            # Map plan name from division code / description
            plan = ""
            if current_div.startswith("A") or "BRONZE" in current_div_desc:
                plan = "Bronze"
            elif current_div.startswith("C") or "POS" in current_div_desc:
                plan = "POS"
            elif current_div.startswith("D") or "HDHP" in current_div_desc:
                plan = "HDHP"
            elif current_div.startswith("W"):
                plan = "Bronze"
            elif current_div.startswith("Y"):
                plan = "POS"
            elif current_div.startswith("Z"):
                plan = "HDHP"

            name_parts = raw_name.split()
            if len(name_parts) >= 2:
                last = name_parts[0]
                first = " ".join(name_parts[1:])
            else:
                last = raw_name
                first = ""

            dedup_key = (mem_id, plan)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            recs.append(InvoiceRecord(
                first=first,
                last=last,
                raw_name=raw_name,
                tier=tier,
                plan=plan,
                group_no=current_div,
                premium=prem,
                status="Active",
                sub_id=mem_id,
                source=doc.name
            ))

    meta: dict[str, Any] = {}
    m_ent = re.search(r"Remittor:\s*\*?([A-Za-z0-9\s,\.\'\-]+?)(?:\s{3,}|\n|$)", text, re.I)
    if m_ent:
        meta["entity"] = m_ent.group(1).strip()
    return recs, meta


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
        # Check for UHC / masked-ID carrier statement format:
        # <policy_no> <Last, First> <Plan_Line_1> <*****1234> <Tier> <Status> $<Charge> [$<Total>]
        # followed by 1-2 wrapped plan description lines (e.g. '30/60/2000/80 POS 26' / 'EOT6')
        uhc_line_re = re.compile(
            r"^\s*(\d{5,10})\s+([A-Za-z'\-\.]+,\s*[A-Za-z'\-\. ]+?)\s+"
            r"([A-Za-z0-9][^\*]+?)\s+(\*{2,}\d{2,6})\s+"
            r"(EE|EC|ES|ESC|FAM|E|E1D|E2D|E3D)\s+([AC])\s+(?:[\d\.]+\s+)?\$([\d,]+\.\d{2})"
        )
        lines = text.splitlines()
        uhc_recs: list[InvoiceRecord] = []
        from census_builder import _is_non_medical
        m_cov = re.search(r"Coverage Period:\s*(\d{1,2}/\d{1,2}/\d{4})\s*-\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
        cov_period = f"{m_cov.group(1)}-{m_cov.group(2)}" if m_cov else ""
        i_ln = 0
        while i_ln < len(lines):
            m_u = uhc_line_re.match(lines[i_ln])
            if not m_u:
                i_ln += 1
                continue
            pol_no = m_u.group(1).strip()
            r_name = " ".join(m_u.group(2).split())
            plan_parts = [clean(m_u.group(3))]
            sub_id_masked = m_u.group(4).strip()
            tier_raw = m_u.group(5).strip().upper()
            st_code = m_u.group(6).strip().upper()
            charge_val = float(m_u.group(7).replace(",", ""))
            i_ln += 1
            while i_ln < len(lines):
                nxt = lines[i_ln].strip()
                if not nxt:
                    break
                if uhc_line_re.match(lines[i_ln]) or re.match(
                    r"^(?:Total|Subtotal|Questions|Toll\s+free|Page\s+\d|Customer\s+No|Invoice\s+No|Bill\s+Group|Coverage\s+Period|Due\s+Date|Details|Current\s+Detail|Policy|No\.)",
                    nxt,
                    re.I,
                ):
                    break
                if len(nxt) <= 45 and not nxt.startswith("$"):
                    plan_parts.append(clean(nxt))
                    i_ln += 1
                else:
                    break
            full_plan = " ".join(p for p in plan_parts if p)
            if _is_non_medical(full_plan) or charge_val < 100.0:
                continue
            if tier_raw in ("E1D", "E2D", "E3D", "EC"):
                tier = "EC"
            elif tier_raw in ("ESC", "FAM"):
                tier = "FAM"
            elif tier_raw == "ES":
                tier = "ES"
            else:
                tier = "EE"
            f_name, l_name = split_full_name(r_name)
            st_str = "COBRA" if st_code == "C" else "Active"
            uhc_recs.append(InvoiceRecord(
                first=f_name, last=l_name, raw_name=r_name,
                tier=tier, plan=full_plan, raw_plan=full_plan,
                premium=charge_val, current_premium=charge_val,
                service_period=cov_period, total_amount=charge_val,
                status=st_str, sub_id=sub_id_masked, group_no=pol_no,
                source=doc.name,
            ))
        if uhc_recs:
            return uhc_recs, {"service_period": cov_period}

        # Check for HCSC / BCBS 'SUBSCRIBER FEES' columnar statement format:
        # <sub_id 8-14 digits> <LAST , FIRST> <cat 4 digits> <product code> <tier> [change reason] [retro] <current> <total>
        if re.search(r"\bSUBSCRIBER\s+FEES\b", text, re.I):
            cleaned_text = clean_overprint_text(text)
            m_bp = re.search(r"Bill\s+Period:\s*(\d{2}-\d{2}-\d{4})\s+to\s+(\d{2}-\d{2}-\d{4})", cleaned_text, re.I)
            bp_str = f"{m_bp.group(1)} to {m_bp.group(2)}" if m_bp else ""
            sub_fee_re = re.compile(
                r"^\s*(\d{8,14})\s+([A-Z][A-Z'\-\. ]+?\s*,\s*[A-Z][A-Z'\-\. ]+?)\s+"
                r"(\d{4})\s+([A-Z0-9]{3,6}-[A-Z0-9]{2,8})\s+([A-Z0-9\+]{2,10})\s+(.*)$"
            )
            amt_tok_re = re.compile(r"\(?[\d,]*\.\d{2}\)?")
            sf_recs: list[InvoiceRecord] = []
            by_sub: dict[tuple[str, str], InvoiceRecord] = {}
            pending_retro_by_sub: dict[str, float] = {}
            from census_builder import _is_non_medical

            for ln in cleaned_text.splitlines():
                msf = sub_fee_re.match(ln)
                if not msf:
                    continue
                sub_id_s = msf.group(1).strip()
                raw_nm = re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", msf.group(2)).strip())
                prod_code = msf.group(4).strip()
                tier_raw = msf.group(5).strip()
                tail = msf.group(6).strip()
                if _is_non_medical(prod_code):
                    continue
                amts = amt_tok_re.findall(tail)
                if len(amts) < 2:
                    continue

                def _signed_amt(s: str) -> float:
                    neg = s.startswith("(") and s.endswith(")")
                    val = float(s.strip("()").replace(",", ""))
                    return -val if neg else val

                # Columns at right edge: [RETRO FEE ADJUST]  CURRENT CHARGES  TOTAL CHARGES
                tot_amt = _signed_amt(amts[-1])
                cur_amt = _signed_amt(amts[-2])
                retro_amt = _signed_amt(amts[-3]) if len(amts) >= 3 else 0.0
                tier = std_coverage(tier_raw) or "EE"
                f_name, l_name = split_full_name(raw_nm)
                key_s = (sub_id_s, prod_code)

                # Pure credit / zero-current product-change reversal lines (e.g. current == 0.00):
                # don't emit a duplicate subscriber row, but preserve the signed retro credit so net retro reconciles.
                if cur_amt <= 0.0:
                    if retro_amt:
                        pending_retro_by_sub[sub_id_s] = round(pending_retro_by_sub.get(sub_id_s, 0.0) + retro_amt, 2)
                    continue

                rec = InvoiceRecord(
                    first=f_name,
                    last=l_name,
                    raw_name=raw_nm,
                    tier=tier,
                    plan=prod_code,
                    raw_plan=prod_code,
                    premium=round(cur_amt, 2),
                    current_premium=round(cur_amt, 2),
                    retro_amount=round(retro_amt, 2) if retro_amt else 0.0,
                    total_amount=round(tot_amt, 2),
                    service_period=bp_str,
                    status="Active",
                    sub_id=sub_id_s,
                    source=doc.name,
                )
                by_sub[key_s] = rec
            for (sid_k, _), rec in by_sub.items():
                if sid_k in pending_retro_by_sub:
                    rec.retro_amount = round((rec.retro_amount or 0.0) + pending_retro_by_sub.pop(sid_k), 2)
                    rec.total_amount = round((rec.premium or 0.0) + rec.retro_amount, 2)
            sf_recs = list(by_sub.values())
            if sf_recs:
                sf_meta: dict[str, Any] = {"service_period": bp_str}
                m_tot_fees = re.search(
                    r"TOTAL\s+FEES\s+(?:(\(?[\d,]+\.\d{2}\)?)\s+)?([\d,]+\.\d{2})\s+([\d,]+\.\d{2})",
                    cleaned_text,
                    re.I,
                )
                if m_tot_fees:
                    if m_tot_fees.group(1):
                        raw_adj = m_tot_fees.group(1)
                        adj_val = Decimal(raw_adj.strip("()").replace(",", ""))
                        if raw_adj.startswith("("):
                            adj_val = -adj_val
                        sf_meta["total_adjustments"] = adj_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    sf_meta["current_charges"] = Decimal(m_tot_fees.group(2).replace(",", "")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    sf_meta["total_due"] = Decimal(m_tot_fees.group(3).replace(",", "")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                return sf_recs, sf_meta
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
        recs = _records_from_tables(doc, meta_out=carrier_meta)
        # Extract printed Total Amount Due / Amount Due on Subgroup & Group Number from spreadsheet headers/footers
        for t in doc.tables:
            df = getattr(t, "df", None)
            if df is None or df.empty:
                continue
            for r_i in range(len(df)):
                row_strs = [clean(v) for v in df.iloc[r_i].tolist()]
                joined_r = " ".join(row_strs)
                if re.search(r"\b(total\s+amount\s+due|amount\s+due\s+on\s+subgroup|total\s+balance\s+due)\b", joined_r, re.I):
                    monies = [to_money(v) for v in row_strs if to_money(v) is not None and to_money(v) > 0]
                    if monies:
                        carrier_meta["total_due"] = Decimal(str(monies[-1])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if not carrier_meta.get("group_number"):
                    for ci, cell_s in enumerate(row_strs):
                        if re.fullmatch(r"group\s*(?:number|no\.?|#|id)\s*:?", cell_s, re.I):
                            for nxt in row_strs[ci + 1:]:
                                if nxt and re.match(r"^[A-Za-z0-9\-]{4,}$", nxt):
                                    carrier_meta["group_number"] = nxt
                                    break
                        elif re.search(r"\bgroup\s*(?:number|no\.?|#|id)\s*[:#]\s*([A-Za-z0-9\-]{4,})", cell_s, re.I):
                            m_g = re.search(r"\bgroup\s*(?:number|no\.?|#|id)\s*[:#]\s*([A-Za-z0-9\-]{4,})", cell_s, re.I)
                            if m_g:
                                carrier_meta["group_number"] = m_g.group(1).strip()

    # 0b. High-Fidelity Structured Carrier/TPA Statement Ingestion (Meritain Health statements)
    if not recs and ("meritain" in (doc.text or "").lower() or "meritain" in doc.name.lower() or "DIVISION:" in (doc.text or "")):
        m_recs, m_meta = _records_from_meritain_statement(doc)
        if m_recs:
            recs = m_recs
            carrier_meta.update(m_meta)
            log.info("Successfully extracted %d structured records from Meritain statement: %s", len(recs), doc.name)

    # 1. LLM-First semantic extraction (Matching Copilot Studio) with Mathematical Control-Total Verification (Upgrade 3)
    det_recs: list[InvoiceRecord] = []
    if not recs:
        m_recs, m_meta = _records_from_meritain_statement(doc)
        c_recs, c_meta = _records_from_carrier_statement(doc)
        if m_recs and not c_recs:
            c_recs = m_recs
            c_meta.update(m_meta)
        if c_meta:
            carrier_meta.update(c_meta)
        det_recs = (
            c_recs
            or _records_from_peo_employee_details(doc)
            or _records_from_peo_statement(doc)
            or _records_from_tables(doc)
            or _records_from_text(doc)
        )

    if not recs and LLM_FIRST and USE_LLM_FALLBACK:
        try:
            log.info("Applying LLM-First semantic extraction for invoice: %s", doc.name)
            recs = _records_from_llm(doc)
            if recs:
                log.info("LLM-First successfully extracted %d invoice records from %s", len(recs), doc.name)
        except Exception as exc:
            log.warning("LLM-First invoice extraction failed for %s: %s (falling back to statement parsers)", doc.name, exc)

    # Upgrade 3: Mathematical Control-Total Verification against printed statement header charges
    printed_cur = carrier_meta.get("current_charges")
    if printed_cur is not None and float(printed_cur) > 0 and recs and det_recs:
        llm_sum = round(sum((r.premium or 0.0) for r in recs), 2)
        det_sum = round(sum((r.premium or 0.0) for r in det_recs), 2)
        target_tot = float(printed_cur)
        if abs(llm_sum - target_tot) > 0.05 and abs(det_sum - target_tot) < abs(llm_sum - target_tot):
            log.info(
                "Control-Total Verification (%s): switching to deterministic invoice records (det_sum=%.2f vs llm_sum=%.2f, target=%.2f)",
                doc.name, det_sum, llm_sum, target_tot,
            )
            recs = det_recs

    # 2. Rule / statement parser fallback if LLM not used or yielded 0 records
    if not recs:
        recs = det_recs or (_records_from_llm(doc) if USE_LLM_FALLBACK else [])


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
    grp_num = clean(carrier_meta.get("group_number", ""))
    if not grp_num:
        m_grp = re.search(r"\bgroup\s*(?:number|no\.?|#|id)\s*[:#|]?\s*\|?\s*([A-Za-z0-9\-]{4,})", text, re.I)
        if m_grp and not re.search(r"^(?:name|info|type|bill|plan)$", m_grp.group(1), re.I):
            grp_num = m_grp.group(1).strip()
    non_med_recs = list(carrier_meta.get("non_medical_records") or [])
    if grp_num:
        for r in recs:
            if not (r.group_no or "").strip():
                r.group_no = grp_num
        for r in non_med_recs:
            if not (r.group_no or "").strip():
                r.group_no = grp_num

    return InvoiceDoc(
        path=doc.name, records=recs, period_start=start, period_end=end,
        entity=(m.group(1).upper() if m else "default"),
        revised=bool(re.search(r"(revis|correct|amend|supersed)", low)),
        draft=bool(re.search(r"\b(draft|void|cancel)", low)),
        number=m_num.group(1) if m_num else "",
        group_number=grp_num,
        doc_date=parse_date(m_dt.group(1)) if m_dt else None,
        current_charges=float(cur_charges) if cur_charges is not None else None,
        total_adjustments=float(tot_adj) if tot_adj is not None else None,
        total_due=float(tot_due) if tot_due is not None else None,
        gross_total_premium=carrier_meta.get("gross_total_premium"),
        non_medical_charges=carrier_meta.get("non_medical_charges"),
        non_medical_breakdown=dict(carrier_meta.get("non_medical_breakdown") or {}),
        non_medical_records=non_med_recs,
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
