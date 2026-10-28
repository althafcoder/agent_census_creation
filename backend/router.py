"""Carrier and Underwriting Routing Decision Engine.

Operating strictly according to the Master Unified Medical Submission Agent Instruction:
1. Determine prospect's primary business state from company address (GHQ, RFP, census, invoice, renewal).
2. Florida (FL) -> Florida Blue:
   - Primary template: RAPT Census - Template
   - GHQ: Required
   - Additional SBU census: Not required
3. Non-Florida -> Cigna:
   - Primary template: Marsh PEO Practice - Census Template - DHR
   - SBU Census: Required after completing Marsh census
   - GHQ: Not required (do not request or report as missing)
4. State missing / conflicting:
   - Identify conflict/missing state in Missing Information Email (Submission Routing).
   - Do not assume carrier or template.
5. Renewal rule:
   - Required ONLY when prospect is with a PEO AND renewal date is within next 90 days.
6. Claims rule:
   - Required ONLY when plan is Self-Funded or Level-Funded AND enrolled employee count > 50.
7. Ready-to-send Missing Information Email generator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
import re
from typing import Optional

import census_config as C
from models import Census, Findings, InvoiceDoc, InvoiceRecord, Person, Prospect, RowResult
from readers import RawDoc

log = logging.getLogger("census.router")

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico",
}

PEO_PROVIDERS = [
    "adp", "totalsource", "trinet", "insperity", "justworks", "paychex", "oasis",
    "vensure", "bbsi", "coadvantage", "engage", "acadia", "alcott", "amplified",
    "peo", "professional employer", "co-employment"
]


@dataclass
class RoutingDecision:
    state: str = ""                         # 2-letter state code, e.g. "FL", "NY"
    state_name: str = ""                    # Full name, e.g. "Florida"
    carrier: str = ""                       # "Florida Blue" or "Cigna"
    primary_template: Path = C.RAPT_TEMPLATE_PATH
    requires_sbu: bool = False
    sbu_template: Path = C.SBU_TEMPLATE_PATH
    ghq_required: bool = False
    state_missing: bool = False
    state_conflicting: bool = False
    conflict_detail: str = ""
    is_florida: bool = False
    notes: list[str] = field(default_factory=list)


def normalize_state(text: str) -> Optional[str]:
    """Identify a 2-letter US state code from a text snippet."""
    if not text:
        return None
    text_clean = text.strip()
    # Direct 2-letter match
    upper = text_clean.upper()
    if upper in US_STATES:
        return upper
    # Look for full state names
    for code, name in US_STATES.items():
        if re.search(rf"\b{re.escape(name)}\b", text_clean, re.IGNORECASE):
            return code
    # Look for standard 'City, ST 12345' or ', ST'
    m = re.search(r",\s*([A-Za-z]{2})\b", text_clean)
    if m and m.group(1).upper() in US_STATES:
        return m.group(1).upper()
    # Look for any isolated 2-letter code in state position
    m2 = re.search(r"\b([A-Za-z]{2})\s+\d{5}\b", text_clean)
    if m2 and m2.group(1).upper() in US_STATES:
        return m2.group(1).upper()
    return None


def extract_prospect_state(prospect: Prospect, docs: list[RawDoc] | None = None) -> tuple[str, bool, str]:
    """Extract and validate the prospect's primary business state.
    
    Returns:
        (state_code, is_conflicting, conflict_detail)
    """
    detected_states: dict[str, list[str]] = {}

    def _record(code: str, source: str):
        if code and code in US_STATES:
            detected_states.setdefault(code, []).append(source)

    # 1. From prospect object
    if prospect.state:
        st = normalize_state(prospect.state)
        if st:
            _record(st, "Prospect State field")

    if prospect.city_state_zip:
        st = normalize_state(prospect.city_state_zip)
        if st:
            _record(st, "Prospect City/State/Zip")

    if prospect.address:
        st = normalize_state(prospect.address)
        if st:
            _record(st, "Prospect Address")

    # 2. Check GHQ / RFP headers for prospect company address
    if docs:
        for d in docs:
            role = getattr(d, "role", "")
            d_name = d.name.lower()
            is_prospect_doc = any(k in d_name for k in ["ghq", "rfp", "questionnaire", "company", "group"])
            if is_prospect_doc or role in ("ghq", "rfp"):
                # Review first 2000 chars of text for company address
                text_sample = (d.text or "")[:2500]
                lines = text_sample.splitlines()
                for line in lines:
                    low = line.lower()
                    if any(k in low for k in ["company address", "business address", "hq address", "physical address", "headquarters"]):
                        st = normalize_state(line)
                        if st:
                            _record(st, f"{d.name} company address line")
                    elif "state:" in low or "state :" in low or "st:" in low:
                        st = normalize_state(line.split(":")[-1])
                        if st:
                            _record(st, f"{d.name} State field")

    if not detected_states:
        return "", False, "Prospect primary business state could not be identified."

    # If multiple different states found for the prospect company
    if len(detected_states) > 1:
        sources_str = "; ".join(f"{st} found in {', '.join(srcs)}" for st, srcs in detected_states.items())
        log.warning("Conflicting prospect states found: %s", sources_str)
        return "", True, f"Conflicting prospect states identified: {sources_str}"

    primary_state = list(detected_states.keys())[0]
    return primary_state, False, ""


def determine_routing(prospect: Prospect, docs: list[RawDoc] | None = None) -> RoutingDecision:
    """Apply the Mandatory Routing Matrix from the Master Unified Medical Submission Agent Instruction."""
    state_code, is_conflicting, conflict_detail = extract_prospect_state(prospect, docs)
    r = RoutingDecision()

    if is_conflicting:
        r.state_conflicting = True
        r.conflict_detail = conflict_detail
        r.notes.append(f"Routing Warning: {conflict_detail}. Awaiting state confirmation.")
        log.warning("Submission Routing conflict: %s", conflict_detail)
        return r

    if not state_code:
        r.state_missing = True
        r.conflict_detail = "Prospect primary business state is missing from current submission."
        r.notes.append("Routing Warning: Prospect state is missing. Awaiting state confirmation.")
        log.warning("Submission Routing state missing: %s", r.conflict_detail)
        return r

    r.state = state_code
    r.state_name = US_STATES.get(state_code, state_code)
    prospect.state = state_code

    if state_code == "FL":
        # Florida Routing -> Florida Blue
        r.is_florida = True
        r.carrier = C.CARRIER_FLORIDA_BLUE
        r.primary_template = C.RAPT_TEMPLATE_PATH
        r.requires_sbu = False
        r.ghq_required = True
        r.notes.append(f"Carrier assigned to Florida Blue based on Florida business address ({state_code}).")
        r.notes.append("Census template: RAPT Census - Template.")
        r.notes.append("GHQ is required for Florida Blue submission.")
    else:
        # Non-Florida Routing -> Cigna
        r.is_florida = False
        r.carrier = C.CARRIER_CIGNA
        r.primary_template = C.MARSH_TEMPLATE_PATH
        r.requires_sbu = True
        r.sbu_template = C.SBU_TEMPLATE_PATH
        r.ghq_required = False
        r.notes.append(f"Carrier assigned to Cigna based on non-Florida business address ({state_code}).")
        r.notes.append("Primary comparison template: Marsh PEO Practice - Census Template - DHR.")
        r.notes.append("Additional deliverable: SBU Census Template (populated from finalized Marsh census).")
        r.notes.append("GHQ is not required for Cigna submission.")

    return r


def check_peo_status(prospect: Prospect, docs: list[RawDoc] | None = None) -> tuple[bool, str]:
    """Determine if the prospect is currently with a PEO."""
    # Check explicit fields
    if prospect.is_peo:
        return True, prospect.peo_name or "PEO"

    if prospect.peo_name:
        low = prospect.peo_name.lower().strip()
        if low and low not in ("none", "n/a", "no", "false", "0"):
            return True, prospect.peo_name

    # Check prospect carrier or notes
    combined = f"{prospect.company} {prospect.current_carrier} {prospect.referral}".lower()
    for p in PEO_PROVIDERS:
        if p in combined:
            return True, p.title()

    # Check doc texts for PEO declaration
    if docs:
        for d in docs:
            t = (d.text or "")[:4000].lower()
            if "current peo:" in t or "peo:" in t:
                for line in t.splitlines():
                    if "current peo:" in line or "peo:" in line:
                        val = line.split(":")[-1].strip()
                        if val and val not in ("none", "n/a", "no", "false", ""):
                            return True, val
            for p in PEO_PROVIDERS:
                if f"peo: {p}" in t or f"peo - {p}" in t or f"payroll: {p}" in t:
                    return True, p.title()

    return False, ""


def check_renewal_required(
    prospect: Prospect,
    renewal_doc=None,
    docs: list[RawDoc] | None = None,
    proposed_effective: date | None = None,
    known_renewal_date: date | None = None,
) -> tuple[bool, str]:
    """Renewal is required ONLY when prospect is currently with a PEO AND renewal date is within 90 days."""
    is_peo, peo_name = check_peo_status(prospect, docs)
    if not is_peo:
        return False, "Prospect is not currently with a PEO; renewal documentation is not required."

    ref_date = proposed_effective or prospect.effective_date or date.today()
    ren_date = None
    if renewal_doc and getattr(renewal_doc, "effective", None):
        ren_date = renewal_doc.effective
    elif known_renewal_date:
        ren_date = known_renewal_date          # current PEO plan-year renewal stated in the uploaded documents
    elif docs:
        from datetime import timedelta
        from utils import parse_date
        for d in docs:
            txt = d.text or ""
            # 1) Explicit "4/1 renewal" or "renewal date: MM/DD/YYYY" in submission/email/GHQ text
            m_ren = (re.search(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*renewal\b", txt, re.I)
                     or re.search(r"\brenewal\s*(?:date)?\s*[:\-]?\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", txt, re.I))
            if m_ren:
                raw_d = m_ren.group(1)
                if raw_d.count("/") == 1:
                    yr = ref_date.year
                    cand = parse_date(f"{raw_d}/{yr}")
                    if cand and cand < date.today():
                        cand = parse_date(f"{raw_d}/{yr + 1}")
                    ren_date = cand
                else:
                    ren_date = parse_date(raw_d)
                if ren_date:
                    break
            # 2) Medical Plan Period range in Benefits Enrollment Report (e.g. "Apr 01, 2026 - Mar 31, 2027")
            m_per = re.search(r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2})\s*[-–]\s*([A-Za-z]{3,9}\s+\d{1,2},\s+20\d{2})\b", txt)
            if m_per:
                end_dt = parse_date(m_per.group(2))
                if end_dt:
                    ren_date = end_dt + timedelta(days=1)
                    break
    if not ren_date and prospect.effective_date:
        ren_date = prospect.effective_date

    if ren_date:
        days_diff = (ren_date - date.today()).days
        # Within 90 days in either direction or upcoming
        if 0 <= days_diff <= 90:
            return True, f"Prospect is with PEO ({peo_name}) and renewal falls within 90 days ({ren_date:%m/%d/%Y})."
        elif days_diff > 90:
            return False, f"Prospect is with PEO ({peo_name}) but renewal date ({ren_date:%m/%d/%Y}) is more than 90 days away."

    return False, "Renewal date or PEO status does not satisfy the 90-day PEO requirement."


def check_claims_required(prospect: Prospect, enrolled_medical_count: int, docs: list[RawDoc] | None = None) -> tuple[bool, str]:
    """Claims documents are required ONLY when funding is Self-Funded or Level-Funded AND enrolled EEs > 50."""
    funding = (prospect.funding_type or "").lower()
    if not funding and docs:
        for d in docs:
            t = (d.text or "")[:3000].lower()
            if "level-funded" in t or "level funded" in t:
                funding = "level-funded"
                break
            if "self-funded" in t or "self funded" in t:
                funding = "self-funded"
                break

    is_self_or_level = any(f in funding for f in ["self", "level", "aso"])
    if not is_self_or_level:
        return False, "Fully insured group; claims documents are not required."

    if enrolled_medical_count > 50:
        return True, f"Funding is {funding.title()} and enrolled employees ({enrolled_medical_count}) exceed 50."

    return False, f"Enrolled medical count ({enrolled_medical_count}) does not exceed 50."


def _extract_payroll_info(roles: dict | None, docs: list[RawDoc] | None) -> tuple[str, dict[tuple[str, str], float], str, set[tuple[str, str]]]:
    """Extract pay date, per-employee medical deduction amounts, payroll doc description, and set of payroll employee names."""
    pay_date = ""
    med_deductions: dict[tuple[str, str], float] = {}
    payroll_names: set[tuple[str, str]] = set()

    payroll_list = roles.get("payroll", []) if roles else []
    inv_list = roles.get("invoice", []) if roles else []
    all_docs = (docs or []) + payroll_list + inv_list

    has_register = False
    has_payroll_invoice = False

    for d in all_docs:
        low = d.name.lower()
        if "payroll register" in low or "payroll_register" in low:
            has_register = True
        if "payroll invoice" in low or "payroll_invoice" in low:
            has_payroll_invoice = True
        if not pay_date:
            m_dt = re.search(r"Pay\s*Date\s*[:|]?\s*(\d{1,2}/\d{1,2}/\d{2,4})", d.text or "", re.I)
            if m_dt:
                pay_date = m_dt.group(1)
            else:
                m_fn = re.search(r"(?:oct|nov|dec|jan|feb|mar|apr|may|jun|jul|aug|sep)[a-z]*[\s_-]*(\d{1,2})", d.path.stem, re.I)
                if m_fn and "payroll" in low:
                    mon_map = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
                               "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"}
                    mon_str = d.path.stem[:3].lower()
                    if mon_str in mon_map:
                        pay_date = f"{mon_map[mon_str]}/{int(m_fn.group(1)):02d}/2026"

    # Match medical deduction pattern (avoid Medicare statutory tax 'Med')
    pat_med = re.compile(r"\b(?:32\s+)?MEDICAL\s+([0-9]+\.[0-9]{2})\b", re.I)

    for d in payroll_list:
        for t in d.tables:
            df = t.df
            for r_idx, row in df.iterrows():
                row_str = " ".join(str(v) for v in row.values)
                # Check for medical deduction
                m_med = pat_med.search(row_str)
                amt = float(m_med.group(1)) if m_med else None

                # Check for employee name
                for val in row.values:
                    s = str(val).strip()
                    if "," in s:
                        for line in s.split("\n"):
                            line = line.strip()
                            m_name = re.match(r"^([A-Za-z\s\.\'\-]+),\s+([A-Za-z\s\.\'\-]+)$", line)
                            if m_name:
                                last = m_name.group(1).strip().lower()
                                first = m_name.group(2).strip().lower().split()[0]
                                if not any(w in last for w in ["department", "company", "total", "payroll", "earnings"]):
                                    last = " ".join(last.split())
                                    payroll_names.add((first, last))
                                    if amt is not None:
                                        med_deductions[(first, last)] = amt
                                    break

    doc_desc_parts = []
    if has_register:
        doc_desc_parts.append("Payroll Register")
    if has_payroll_invoice:
        doc_desc_parts.append("Payroll Invoice")

    payroll_docs_desc = " or ".join(doc_desc_parts) if doc_desc_parts else "Payroll Register"
    if pay_date:
        payroll_docs_desc = f"{pay_date} {payroll_docs_desc}"

    return pay_date, med_deductions, payroll_docs_desc, payroll_names


def generate_missing_information_email(
    prospect: Prospect,
    routing: RoutingDecision,
    findings: Findings,
    census: Census | None,
    have_census: bool,
    have_invoice: bool,
    have_renewal: bool,
    renewal_required: bool,
    claims_required: bool,
    sbc_missing: list[str] | None = None,
    ghq_present: bool = True,
    ghq_partial: bool = False,
    ghq_details: list[str] | None = None,
    roles: dict | None = None,
    selected_invoices: list[InvoiceDoc] | None = None,
    invoice_records: list[InvoiceRecord] | None = None,
    results: dict | None = None,
    docs: list[RawDoc] | None = None,
) -> str:
    """Generate the standardized ready-to-send Missing Information Email per the Master Unified Medical Submission Agent Instruction."""
    prospect_name = prospect.company.strip() if prospect.company else "Prospect Name Required"
    subject = f"Medical Submission Missing Information - {prospect_name}"

    sections: list[tuple[str, list[str]]] = []

    # 0. Document Access Section (Section 6.1 F)
    access_records = getattr(findings, "access_records", {}) or {}
    access_items = []
    for fn, rec in access_records.items():
        st = getattr(rec, "status", None)
        st_val = getattr(st, "value", str(st))
        if st_val in ("PASSWORD_REJECTED", "PASSWORD_NOT_FOUND", "PRESENT_LOCKED", "DECRYPTION_TOOL_UNAVAILABLE", "UNSUPPORTED_PROTECTION"):
            if getattr(rec, "remedy_message", ""):
                access_items.append(rec.remedy_message)
            elif st_val == "PASSWORD_REJECTED":
                access_items.append(f"Please confirm the password for {fn} or provide an unlocked copy; the password supplied in the submission email did not open the file.")
            elif st_val == "PASSWORD_NOT_FOUND":
                access_items.append(f"Please confirm the password for {fn} or provide an unlocked copy.")
            else:
                access_items.append(f"Please provide an unlocked copy of {fn}.")
    if access_items:
        sections.append(("Document Access", access_items))

    # 1. GHQ Section (Florida Blue only)
    if routing.is_florida:
        ghq_items = []
        if not ghq_present:
            ghq_items.append("Complete and signed Group Health Questionnaire (GHQ) is required for Florida Blue submission.")
        elif ghq_partial:
            if ghq_details:
                ghq_items.extend(ghq_details)
            else:
                ghq_items.append("Group Health Questionnaire (GHQ) is incomplete; please provide complete underwriting questions and medical disclosures.")
        if ghq_items:
            sections.append(("GHQ", ghq_items))

    # 2. Submission Routing Section (if missing or conflicting)
    if routing.state_conflicting or routing.state_missing:
        routing_items = []
        if routing.state_conflicting:
            routing_items.append(f"Conflicting primary business addresses identified: {routing.conflict_detail}. Please confirm the prospect's primary business state.")
        elif routing.state_missing:
            routing_items.append("Prospect primary business address / state was not provided. Please confirm the primary business state to complete carrier assignment.")
        if routing_items:
            sections.append(("Submission Routing", routing_items))

    # 3. SBC Section
    sbc_items = []
    all_docs = (docs or []) + (roles.get("sbc", []) if roles else [])
    sbc_docs = [
        d for d in all_docs
        if "sbc" in d.name.lower() or "summary of benefits" in d.name.lower() or
        re.search(r"summary of benefits and coverage|\bcoverage period\b", (d.text or "")[:1500], re.I)
    ]
    has_sbc_doc = bool(sbc_docs)

    active_plans = sorted({
        emp.plan for emp in (census.employees if census else [])
        if emp.coverage in C.ACTIVE_TIERS and emp.plan and emp.plan not in ("Medical", "Waived", "None", "NA")
    })

    if sbc_missing:
        missing_list = sbc_missing
    elif has_sbc_doc and active_plans:
        missing_list = []
        for p in active_plans:
            matched = False
            p_low = p.lower()
            nums = re.findall(r"\d+", p)
            metals = [m for m in ["bronze", "silver", "gold", "platinum"] if m in p_low]
            carriers = [c for c in ["oxford", "anthem", "aetna", "cigna", "uhc", "united", "blue", "bcbs", "florida blue", "humana"] if c in p_low]
            for sbc in sbc_docs:
                sbc_str = (sbc.name + " " + (sbc.text or "")[:1000]).lower()
                if carriers and not any(c in sbc_str for c in carriers):
                    continue
                if metals and not any(m in sbc_str for m in metals):
                    continue
                if nums:
                    key_nums = [n for n in nums if len(n) >= 3 or n in ("40", "80", "50", "60", "30", "100")]
                    if key_nums and all(n in sbc_str for n in key_nums):
                        matched = True
                        break
                    elif not key_nums and any(n in sbc_str for n in nums):
                        matched = True
                        break
                else:
                    matched = True
                    break
            if not matched:
                missing_list.append(p)
    elif not has_sbc_doc and not routing.is_florida:
        missing_list = active_plans
    else:
        missing_list = []

    if missing_list:
        sbc_items.append("Please provide complete SBCs for each currently enrolled medical plan:")
        sbc_items.extend(missing_list)
        sections.append(("SBC", sbc_items))

    # 4. Census vs. Invoice Comparison Section
    comparison_items = []
    if not have_census and have_invoice:
        comparison_items.append("Employee census document was not provided. Census creation and comparison could not be completed.")
    elif have_census and not have_invoice:
        comparison_items.append("Current medical invoice or Benefit Enrollment Register was not provided. Full census versus invoice comparison could not be completed.")
    elif have_census and have_invoice:
        pay_date, med_deductions, payroll_docs_desc, payroll_names = _extract_payroll_info(roles, docs)

        # Invoice details
        inv_doc = selected_invoices[0] if selected_invoices else None
        inv_num = inv_doc.number if inv_doc else ""
        inv_period = inv_doc.period_start.strftime("%B %Y") if inv_doc and inv_doc.period_start else ""
        inv_base = f"{inv_period} Benefit Invoice" if inv_period else "Benefit Invoice"
        inv_with_num = f"{inv_base} (#{inv_num})" if inv_num else inv_base
        inv_without_num = inv_base

        unbilled_handled = set()
        conflict_handled = set()

        # (A) Unbilled Active Employees (enrolled on census, missing from invoice)
        unbilled_active = []
        if census:
            for emp in census.employees:
                if emp.coverage in C.ACTIVE_TIERS and not emp.invoice_only:
                    res = results.get(id(emp)) if results else None
                    if not res or not res.inv or getattr(res, "status", "") == "Unbilled Active":
                        unbilled_active.append(emp)

        for idx, emp in enumerate(unbilled_active):
            who = f"{emp.first} {emp.last}".strip()
            unbilled_handled.add(who.lower())
            p_obj = "him" if emp.gender in ("M", "Male", "Man / Male") else ("her" if emp.gender in ("F", "Female", "Woman / Female") else "them")
            p_subj = "he" if emp.gender in ("M", "Male", "Man / Male") else ("she" if emp.gender in ("F", "Female", "Woman / Female") else "they")
            p_subj_cap = p_subj.capitalize()
            p_poss = "his" if emp.gender in ("M", "Male", "Man / Male") else ("her" if emp.gender in ("F", "Female", "Woman / Female") else "their")

            # Coverage tier text
            if emp.coverage == "EE":
                tier_str = "Employee Only"
            elif emp.coverage == "ES":
                sp = next((d for d in emp.dependents if getattr(d, "role", "") == "SP" or getattr(d, "relationship", "") == "SP"), None)
                if sp:
                    tier_str = f"Employee + Spouse (spouse {sp.first} {sp.last})"
                else:
                    tier_str = "Employee + Spouse"
            elif emp.coverage == "EC":
                chs = [d for d in emp.dependents if getattr(d, "role", "") == "CH" or getattr(d, "relationship", "") == "CH"]
                if chs:
                    tier_str = f"Employee + Child(ren) (child {', '.join(d.first + ' ' + d.last for d in chs)})"
                else:
                    tier_str = "Employee + Child(ren)"
            elif emp.coverage == "FAM":
                deps = emp.dependents
                if deps:
                    tier_str = f"Family ({', '.join(d.first + ' ' + d.last for d in deps)})"
                else:
                    tier_str = "Family"
            else:
                tier_str = emp.coverage

            eff_str = f", effective {emp.effective_date:%m/%d/%Y}" if getattr(emp, "effective_date", None) else ""

            fn = emp.first.lower().split()[0] if emp.first else ""
            ln = emp.last.lower().strip() if emp.last else ""
            ded_amt = med_deductions.get((fn, ln))

            inv_ref = inv_with_num if idx == 0 else inv_without_num

            if ded_amt is not None:
                ded_clause = f", and the {pay_date} payroll deducts ${ded_amt:.2f} for medical"
                inv_clause = f"However, {p_subj} is not billed for medical on the {inv_ref}."
            else:
                ded_clause = ""
                inv_clause = f"{p_subj_cap} is not billed for medical on the {inv_ref}."

            paragraph = (
                f"{who}: The enrollment report shows {p_obj} on {emp.plan}, {tier_str}{eff_str}{ded_clause}. "
                f"{inv_clause} Please confirm {p_poss} coverage status and provide a corrected invoice or Benefit Enrollment Register."
            )
            comparison_items.append(paragraph)

        # (B) Cross-document conflicts (Billed on invoice, missing from payroll)
        if payroll_names and census:
            for emp in census.employees:
                if emp.coverage in C.ACTIVE_TIERS and not emp.invoice_only:
                    fn = emp.first.lower().split()[0] if emp.first else ""
                    ln = emp.last.lower().strip() if emp.last else ""
                    if (fn, ln) not in payroll_names:
                        res = results.get(id(emp)) if results else None
                        if res and res.inv:
                            who = f"{emp.first} {emp.last}".strip()
                            conflict_handled.add(who.lower())
                            p_subj_cap = "He" if emp.gender in ("M", "Male", "Man / Male") else ("She" if emp.gender in ("F", "Female", "Woman / Female") else "They")
                            p_poss = "his" if emp.gender in ("M", "Male", "Man / Male") else ("her" if emp.gender in ("F", "Female", "Woman / Female") else "their")
                            plan_disp = res.inv.plan or emp.plan
                            tier_disp = "Employee Only" if res.inv.tier in ("EE", "Employee") else res.inv.tier
                            prem_disp = f", ${res.inv.premium:.2f}" if res.inv.premium else ""
                            paragraph = (
                                f"{who}: {p_subj_cap} is billed for medical on the {inv_without_num} ({plan_disp}, {tier_disp}{prem_disp}), "
                                f"but {p_subj_cap.lower()} does not appear on the {payroll_docs_desc}. "
                                f"Please confirm {p_poss} employment and coverage status, including any termination date, leave or COBRA status."
                            )
                            comparison_items.append(paragraph)

        # (C) Waiting Period Employees (coverage == WP)
        if census:
            for emp in census.employees:
                if emp.coverage == "WP":
                    who = f"{emp.first} {emp.last}".strip()
                    p_subj = "he" if emp.gender in ("M", "Male", "Man / Male") else ("she" if emp.gender in ("F", "Female", "Woman / Female") else "they")
                    hire_str = f" (hired {emp.hire_date:%m/%d/%Y})" if emp.hire_date else ""
                    if emp.hire_date:
                        y = emp.hire_date.year + (1 if emp.hire_date.month == 12 else 0)
                        m = 1 if emp.hire_date.month == 12 else emp.hire_date.month + 1
                        elig_dt = date(y, m, 1)
                        elig_str = f" when {p_subj} becomes eligible on {elig_dt:%m/%d/%Y}"
                    else:
                        elig_str = f" when {p_subj} becomes eligible"
                    paragraph = (
                        f"{who}{hire_str}: No medical election is on file. "
                        f"Please confirm whether {p_subj} will enroll (plan and tier) or waive{elig_str}."
                    )
                    comparison_items.append(paragraph)

        # (D) Not-Eligible Employees (coverage == NE)
        if census:
            ne_emps = [e for e in census.employees if e.coverage == "NE"]
            if ne_emps:
                ne_names = [f"{e.first} {e.last}".strip() for e in ne_emps]
                if len(ne_names) == 1:
                    names_str = ne_names[0]
                    verb_str = "this employee"
                elif len(ne_names) == 2:
                    names_str = f"{ne_names[0]} and {ne_names[1]}"
                    verb_str = "these employees"
                else:
                    names_str = f"{', '.join(ne_names[:-1])} and {ne_names[-1]}"
                    verb_str = "these employees"
                paragraph = (
                    f"{names_str}: There are no benefit eligibility or enrollment records for {verb_str}. "
                    f"Please confirm whether they are part-time or otherwise not eligible for medical coverage."
                )
                comparison_items.append(paragraph)

        # (E) Fallback for any other exceptions in findings (if not handled above)
        if findings:
            for cat, who, detail in findings.exceptions:
                low_who = who.lower().strip()
                cat_low = cat.lower()
                det_low = detail.lower()
                if ("missing from invoice" in cat_low or "missing from invoice" in det_low) and low_who not in unbilled_handled:
                    comparison_items.append(f"Employee {who} is listed on the census with active coverage but is missing from the invoice.")
                elif "missing from census" in cat_low or "missing from census" in det_low:
                    comparison_items.append(f"Employee {who} appears on the invoice but is not included on the employee census.")
                elif "tier mismatch" in cat_low or "tier mismatch" in det_low:
                    comparison_items.append(f"Coverage tier mismatch for {who}: {detail}.")
                elif ("eligibility conflict" in cat_low or "eligibility conflict" in det_low) and low_who not in conflict_handled:
                    comparison_items.append(f"Eligibility conflict for {who}: {detail}.")

    if comparison_items:
        sections.append(("Census vs. Invoice Comparison", comparison_items))

    # 5. Renewal Section (Include ONLY when renewal is required under PEO & 90-day rule)
    if renewal_required and not have_renewal:
        sections.append(("Renewal", [
            "Current PEO renewal document with proposed renewal rates is required as the prospect is with a PEO and renewal is within 90 days."
        ]))

    # 6. Claims Documents Section (Include ONLY when required under Self/Level & >50 EEs rule)
    if claims_required:
        sections.append(("Claims Documents", [
            "Monthly claims reports and high-claimant information are required for self/level-funded group with over 50 enrolled employees."
        ]))

    # Build final message
    lines = [
        f"Subject: {subject}",
        "Hello,",
        "Please provide or clarify the following information required to complete the medical submission:",
    ]
    if not sections:
        lines.append("No missing information or material discrepancies were identified in the submitted medical documentation.")
    else:
        for sec_title, items in sections:
            lines.append(sec_title)
            for it in items:
                lines.append(it)

    lines.append("Thank you.")
    return "\n".join(lines)
