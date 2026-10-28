"""Populate the prospect block from GHQ (primary) and RFP (supplement)."""
from __future__ import annotations

import logging
import re

from models import Prospect
from readers import RawDoc
from utils import clean, norm_key, parse_date

log = logging.getLogger("census.prospect")

LABELS = {
    "effective_date": ["proposedeffectivedate", "requestedeffectivedate", "effectivedate",
                       "coverageeffectivedate", "proposedeffective", "desiredeffectivedate"],
    "sic": ["siccode", "sicnaicscode", "sicnaics", "naicscode", "sic", "naics", "siccodenaics"],
    "company": ["companyname", "clientlegalname", "legalname", "prospectname", "employername", "nameofcompany", "businessname",
                "groupname", "organizationname", "legalbusinessname", "nameofemployer", "company", "employer"],
    "address": ["address", "streetaddress", "businessaddress", "mailingaddress", "addressline1", "address1",
                "physicaladdress", "hqaddress"],
    "city": ["city"],
    "state": ["state", "states", "primarystate"],
    "zip": ["zip", "zipcode", "postalcode"],
    "csz": ["citystatezip", "citystatezipcode", "citystzip", "hqcitystatezip", "locations"],
    "referral": ["onedigitalreferral", "onedigitalreferal", "referral", "referredby", "referralsource",
                 "brokerandcommission", "broker", "brokeragency", "prospectbroker", "prospectreferral"],
    "sales_person": ["reisalesperson", "reisalesrep", "salesperson", "salesrep", "salesrepresentative",
                     "accountexecutive", "salesexecutive", "peosalesperson"],
    "current_carrier": ["currentmedicalcarrier", "currentcarrier", "medicalcarrier", "carrier", "priorcarrier"],
    "peo": ["currentpeo", "peo", "peoname", "payrollpeo"],
    "funding": ["fundingtype", "fundingarrangement", "planfunding", "funding"],
    "dba": ["dba", "dbadba", "doingbusinessas", "dbaname", "dbafictitiousname"],
    "ein": ["ein", "eintaxid", "taxid", "fein", "federaltaxid", "employeridentificationnumber"],
    "contact_name": ["contactname", "primarycontact", "primarycontactname", "contact",
                     "clientcompanycontact", "clientcontact", "companycontact", "clientcontactname",
                     "primarycompanycontact", "hrcontact", "contactperson"],
    "contact_email": ["contactemail", "email", "emailaddress", "primarycontactemail", "clientemail"],
    "contact_phone": ["contactphone", "phone", "phonenumber", "primarycontactphone", "telephone"],
}


def _parse_multi_colon_line(line: str) -> dict[str, str]:
    colon_indices = [m.start() for m in re.finditer(r'(?<=[A-Za-z0-9\)])\s*:', line)]
    if not colon_indices:
        return {}
    if len(colon_indices) == 1:
        k, _, v = line.partition(":")
        return {norm_key(k): clean(v)} if 0 < len(k.strip()) <= 45 and clean(v) else {}

    pairs = {}
    segments = [line[:colon_indices[0]].strip()]
    for i in range(len(colon_indices) - 1):
        segments.append(line[colon_indices[i] + 1: colon_indices[i + 1]].strip())
    segments.append(line[colon_indices[-1] + 1:].strip())

    curr_label = segments[0]
    for i in range(1, len(segments)):
        seg = segments[i]
        if i == len(segments) - 1:
            val = seg
            if curr_label and val:
                pairs[norm_key(curr_label)] = clean(val)
        else:
            words = seg.split()
            if len(words) > 1:
                next_label = words[-1]
                val = " ".join(words[:-1])
                if len(words) >= 3 and words[-2].lower() in ("contact", "phone", "cell", "fax", "job", "work", "alt"):
                    next_label = words[-2] + " " + words[-1]
                    val = " ".join(words[:-2])
            else:
                next_label = words[0] if words else ""
                val = ""
            if curr_label and val:
                pairs[norm_key(curr_label)] = clean(val)
            curr_label = next_label
    return pairs


def _kv_pairs(doc: RawDoc) -> dict[str, str]:
    """label -> value from 'Label: value' text lines and from label/value table cells."""
    pairs: dict[str, str] = {}
    for line in (doc.text or "").splitlines():
        if ":" in line:
            if line.count(":") > 1:
                m_pairs = _parse_multi_colon_line(line)
                for mk, mv in m_pairs.items():
                    pairs.setdefault(mk, mv)
                continue
            k, _, v = line.partition(":")
            if 0 < len(k.strip()) <= 45 and clean(v):
                pairs.setdefault(norm_key(k), clean(v))
    for t in doc.tables:
        grid = t.df.values.tolist()
        for row in grid:
            cells = [clean(c) for c in row]
            for i, c in enumerate(cells[:-1]):
                if c and len(c) <= 45:
                    nxt = next((x for x in cells[i + 1:] if x), "")
                    k = norm_key(c.rstrip(":"))
                    if nxt and k and k not in pairs:
                        pairs[k] = nxt
    return pairs


def _pick(pairs: dict[str, str], field: str) -> str:
    for lab in LABELS[field]:
        if lab in pairs and pairs[lab]:
            return pairs[lab]
    return ""


def _from_doc(doc: RawDoc) -> dict:
    p = _kv_pairs(doc)
    out = {f: _pick(p, f) for f in LABELS}
    return out


def _llm_fill(doc: RawDoc, missing: list[str]) -> dict:
    from census_config import USE_LLM_FALLBACK
    if not USE_LLM_FALLBACK or not doc.text.strip():
        return {}
    try:
        from llm_client import extract_prospect_fields
        return extract_prospect_fields(doc.text, missing)
    except Exception as exc:  # pragma: no cover
        log.warning("LLM prospect fallback failed: %s", exc)
        return {}



def _is_invalid_sic(val: str) -> bool:
    if not val:
        return True
    low = val.lower()
    if any(bad in low for bad in ["#", "ft", "pt", "employee", "code:", "tax", "owner", "corp"]):
        return True
    clean_num = re.sub(r"\D", "", val)
    return len(clean_num) not in (3, 4, 5, 6)


def _is_garbage_val(val: str) -> bool:
    if not val:
        return True
    if ":" in val:
        return True
    if any(k in val.lower() for k in ["carrier:", "phone:", "years in business", "owner name", "plan:"]):
        return True
    return False


def _apply_llm_prospect(merged: dict, llm_res: dict) -> None:
    if llm_res.get("company_name") and (_is_garbage_val(merged.get("company", "")) or not merged.get("company")):
        merged["company"] = llm_res["company_name"]
    if llm_res.get("proposed_effective_date") and (not merged.get("effective_date") or len(str(llm_res["proposed_effective_date"])) > 2):
        merged["effective_date"] = str(llm_res["proposed_effective_date"])
    if llm_res.get("sic_naics_code") and not _is_invalid_sic(llm_res["sic_naics_code"]):
        merged["sic"] = llm_res["sic_naics_code"]
    elif _is_invalid_sic(merged.get("sic", "")):
        merged["sic"] = ""
    if llm_res.get("address") and not merged.get("address"):
        merged["address"] = llm_res["address"]
    if llm_res.get("city_state_zip") and not merged.get("csz"):
        merged["csz"] = llm_res["city_state_zip"]
    if llm_res.get("city") and not merged.get("city"):
        merged["city"] = llm_res["city"]
    if llm_res.get("state") and not merged.get("state"):
        merged["state"] = llm_res["state"]
    if llm_res.get("zip") and not merged.get("zip"):
        merged["zip"] = str(llm_res["zip"])
    ref = llm_res.get("referral") or llm_res.get("onedigital_referral")
    if ref and (_is_garbage_val(merged.get("referral", "")) or not merged.get("referral")):
        merged["referral"] = ref
    sp = llm_res.get("sales_person") or llm_res.get("rei_sales_person")
    if sp and (_is_garbage_val(merged.get("sales_person", "")) or not merged.get("sales_person")):
        merged["sales_person"] = sp
    cc = llm_res.get("current_carrier")
    if cc and (_is_garbage_val(merged.get("current_carrier", "")) or not merged.get("current_carrier")):
        merged["current_carrier"] = cc
    peo = llm_res.get("current_peo") or llm_res.get("peo_name")
    if peo and (_is_garbage_val(merged.get("peo", "")) or not merged.get("peo")):
        merged["peo"] = peo
    sf_id = llm_res.get("salesforce_id")
    if sf_id and not merged.get("salesforce_id"):
        merged["salesforce_id"] = sf_id
    dba = llm_res.get("dba") or llm_res.get("dba_name")
    if dba and not merged.get("dba"):
        merged["dba"] = dba
    ein = llm_res.get("ein") or llm_res.get("ein_tax_id")
    if ein and not merged.get("ein"):
        merged["ein"] = ein
    cn = llm_res.get("contact_name")
    if cn and not merged.get("contact_name"):
        merged["contact_name"] = cn
    ce = llm_res.get("contact_email")
    if ce and not merged.get("contact_email"):
        merged["contact_email"] = ce
    cp = llm_res.get("contact_phone")
    if cp and not merged.get("contact_phone"):
        merged["contact_phone"] = cp


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")
_EIN_RE = re.compile(r"\b(\d{2}-\d{7})\b")
_FT_RE = re.compile(r"#?\s*FT\s*Employees?\s*[:\-]?\s*(\d{1,5})", re.I)
_FULLTIME_RE = re.compile(r"full[\s\-]?time\s+employees?\s*[:\-]?\s*(\d{1,5})", re.I)
# Broker / agency / vendor domains that must never be captured as the *client's* contact email.
_AGENCY_DOMAINS = ("adp.com", "archonpeo.com", "archon", "benefitfocus", "bswift", "peo")


def _harvest(regex: "re.Pattern[str]", docs: list[RawDoc]) -> str:
    for d in docs:
        m = regex.search(d.text or "")
        if m:
            return m.group(0).strip()
    return ""


def _contact_name_from_text(docs: list[RawDoc]) -> str:
    """Harvest a client contact person from GHQ/RFP-style text such as
    'Client Company Contact: Bonni Mauro Title: HR Manager'. Label-driven, so it generalises."""
    lab = re.compile(
        r"(?:client\s+(?:company\s+)?contact|company\s+contact|primary\s+contact|contact\s+(?:name|person))"
        r"\s*[:\-]?\s*([A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){0,3})", re.I)
    for d in docs:
        for m in lab.finditer(d.text or ""):
            name = m.group(1).strip()
            name = re.split(r"\s+(?:Title|Phone|Fax|Email|E-?mail)\b", name, flags=re.I)[0].strip()
            if 1 <= len(name.split()) <= 4 and not re.search(r"\d|@", name):
                return name
    return ""


def _client_domain(company: str, docs: list[RawDoc]) -> str:
    """Best-effort client domain from the company name tokens (e.g. 'SLM Waste ...' -> 'slm')."""
    toks = [t for t in re.findall(r"[A-Za-z]{3,}", company or "") if t.lower() not in
            {"inc", "llc", "corp", "company", "services", "service", "group", "the", "and"}]
    if not toks:
        return ""
    return toks[0].lower()


def _pick_client_email(docs: list[RawDoc], company: str) -> str:
    """Return the first email whose domain plausibly belongs to the client (not a broker/agency)."""
    domain_hint = _client_domain(company, docs)
    candidates: list[str] = []
    for d in docs:
        candidates.extend(_EMAIL_RE.findall(d.text or ""))
    for em in candidates:
        low = em.lower()
        if any(a in low for a in _AGENCY_DOMAINS):
            continue
        if domain_hint and domain_hint in low:
            return em
    for em in candidates:
        low = em.lower()
        if not any(a in low for a in _AGENCY_DOMAINS):
            return em
    return ""


def _sales_person_from_text(docs: list[RawDoc]) -> str:
    for d in docs:
        text = d.text or ""
        m = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*\n\s*(?:Senior\s+)?(?:Sales\s+(?:Manager|Rep|Representative|Executive|Director)|Account\s+Executive|PEO\s+Consultant)", text)
        if m:
            return m.group(1).strip()
        m2 = re.search(r"From\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\s*<[^>]+@(?:decisionhr|cognethro|onedigital)[^>]*>", text, re.I)
        if m2:
            return m2.group(1).strip()
    return ""


def _sic_from_text(docs: list[RawDoc]) -> str:
    for d in docs:
        for t in getattr(d, "tables", []):
            grid = t.df.values.tolist()
            for row in grid:
                for idx, cell in enumerate(row[:-1]):
                    c_clean = clean(cell)
                    if c_clean and re.search(r"\bSIC\b|\bSIC\s*CODE\b", c_clean, re.I):
                        val = clean(row[idx + 1])
                        if val and not _is_invalid_sic(val):
                            return val
        text = d.text or ""
        m = re.search(r"\bSIC(?:\s*CODE)?\s*[:\-]?\s*([0-9]{4}(?:-[0-9]{1,4})?|[0-9]{3,6})\b", text, re.I)
        if m:
            sic_cand = m.group(1).strip()
            if not _is_invalid_sic(sic_cand):
                return sic_cand
    return ""


def _text_fill(merged: dict, primary_docs: list[RawDoc], fallback_docs: list[RawDoc]) -> None:
    """Harvest contact name, email, phone, and EIN from the company-info-bearing sources.

    Prefer the authoritative GHQ/RFP documents; only fall back to other submission
    documents (invoices, census tabs) for EIN, which is a stable company identifier.
    Contact name/email/phone are never taken from invoices to avoid capturing a
    broker's or carrier rep's details on broker-handled or multi-entity submissions.
    """
    if not merged.get("contact_name"):
        nm = _contact_name_from_text(primary_docs) or _contact_name_from_text(fallback_docs)
        if nm:
            merged["contact_name"] = nm
    client_em = _pick_client_email(primary_docs, merged.get("company", ""))
    cur_em = (merged.get("contact_email") or "").lower()
    cur_is_agency = any(a in cur_em for a in _AGENCY_DOMAINS) or "adp.com" in cur_em
    if client_em and (not cur_em or cur_is_agency) and client_em.lower() != cur_em:
        merged["contact_email"] = client_em
    if not merged.get("contact_phone"):
        merged["contact_phone"] = _harvest(_PHONE_RE, primary_docs) or _harvest(_PHONE_RE, fallback_docs)
    if not merged.get("sales_person"):
        sp = _sales_person_from_text(primary_docs) or _sales_person_from_text(fallback_docs)
        if sp:
            merged["sales_person"] = sp
    if not merged.get("ein"):
        val = _harvest(_EIN_RE, primary_docs) or _harvest(_EIN_RE, fallback_docs)
        merged["ein"] = val
    if not merged.get("ft_employees"):
        m = _FT_RE.search("\n".join(d.text or "" for d in primary_docs)) or \
            _FULLTIME_RE.search("\n".join(d.text or "" for d in primary_docs))
        if m:
            merged["ft_employees"] = int(m.group(1))


def build_prospect(ghq_docs: list[RawDoc], rfp_docs: list[RawDoc], all_docs: list[RawDoc] | None = None) -> tuple[Prospect, bool, bool]:
    """Returns (prospect, any_source_found, any_field_missing) strictly following official instructions."""
    import pandas as pd
    sources = list(ghq_docs) + list(rfp_docs)
    merged = {f: "" for f in LABELS}

    ghq_text = "\n\n".join(d.text for d in ghq_docs if d.text)
    rfp_text = "\n\n".join(d.text for d in rfp_docs if d.text)

    from census_config import LLM_FIRST, USE_LLM_FALLBACK

    # 1. LLM-First semantic extraction from GHQ / RFP (Matching Copilot Studio)
    if LLM_FIRST and USE_LLM_FALLBACK and (ghq_text.strip() or rfp_text.strip()):
        try:
            log.info("      [PROSPECT] Calling Claude Anthropic (LLM-First) for prospect fields extraction...")
            from llm_client import extract_prospect_fields
            llm_res = extract_prospect_fields(ghq_text, rfp_text)
            if llm_res:
                _apply_llm_prospect(merged, llm_res)
        except Exception as exc:
            log.warning("LLM-First prospect extraction error on GHQ/RFP: %s", exc)

    # 2. Rule-based extraction pass for any remaining unfilled fields
    for doc in sources:
        vals = _from_doc(doc)
        for k, v in vals.items():
            if v and not merged[k]:
                merged[k] = v

    if _is_invalid_sic(merged.get("sic", "")):
        merged["sic"] = ""

    # 3. Fallback LLM pass if not run earlier and fields missing
    if not LLM_FIRST and USE_LLM_FALLBACK and (ghq_text.strip() or rfp_text.strip()):
        try:
            log.info("      [PROSPECT] Calling Claude Anthropic for prospect fields extraction from GHQ/RFP...")
            from llm_client import extract_prospect_fields
            llm_res = extract_prospect_fields(ghq_text, rfp_text)
            if llm_res:
                _apply_llm_prospect(merged, llm_res)
        except Exception as exc:
            log.warning("LLM prospect extraction error on GHQ/RFP: %s", exc)


    # 3. If core prospect fields are still missing, review all uploaded documents (census notes tabs, invoices, reports)
    # as mandated by the official instructions ("Source Document Review: All files must be reviewed and combined")
    core_missing = not merged.get("company") or not merged.get("effective_date") or not merged.get("address")
    found_in_other = False
    if core_missing and all_docs:
        other_texts = []
        for d in all_docs:
            if d in sources:
                continue
            vals = _from_doc(d)
            for k, v in vals.items():
                if v and not merged.get(k):
                    merged[k] = v
                    found_in_other = True
            # Deterministic invoice header address/CSZ extraction when missing from GHQ/RFP
            if d.text and (not merged.get("address") or not merged.get("csz") or not re.search(r"\b\d{5}\b", merged.get("csz", ""))):
                m_inv_addr = re.search(
                    r"Address\s+Contact\s+Information[\s\S]{0,160}?\n\s*(\d+\s+[A-Za-z0-9\s.\-]+?)(?:\s{2,}|\n)[\s\S]{0,160}?\n\s*([A-Za-z .\-]+,\s*[A-Z]{2},?\s*\d{5}(?:-\d{4})?)",
                    d.text,
                    re.I,
                )
                if m_inv_addr:
                    if not merged.get("address"):
                        merged["address"] = clean(m_inv_addr.group(1))
                    if not merged.get("csz") or not re.search(r"\b\d{5}\b", merged.get("csz", "")):
                        merged["csz"] = clean(m_inv_addr.group(2))
                    found_in_other = True
            # Collect census notes/company tabs and headers
            for t in getattr(d, "tables", []):
                t_lower = t.name.lower()
                if any(w in t_lower for w in ["company", "note", "summary", "census", "client"]):
                    sample = "\n".join(" | ".join(str(c) for c in r if pd.notna(c)) for r in t.df.values.tolist()[:40])
                    other_texts.append(f"--- {d.name} [{t.name}] ---\n{sample}")
            # Header text (first 2500 chars of invoices / reports)
            if d.text:
                other_texts.append(f"--- {d.name} ---\n{d.text[:2500]}")

        combined_other = "\n\n".join(other_texts)
        if combined_other.strip() and USE_LLM_FALLBACK:
            try:
                log.info("      [PROSPECT] Reviewing all uploaded documents (census notes & invoices) per official instructions...")
                from llm_client import extract_prospect_fields
                llm_res = extract_prospect_fields(combined_other)
                if llm_res:
                    _apply_llm_prospect(merged, llm_res)
                    if llm_res.get("company_name") or llm_res.get("proposed_effective_date"):
                        found_in_other = True
            except Exception as exc:
                log.warning("LLM prospect extraction from other documents failed: %s", exc)

    if _is_invalid_sic(merged.get("sic", "")):
        merged["sic"] = ""

    # 3b. Harvest supported contact details (email, phone, EIN) from all current-submission documents
    _text_fill(merged, sources, list(all_docs or []))

    sic_from_text = _sic_from_text(sources + list(all_docs or []))
    if sic_from_text and ("-" in sic_from_text or not merged.get("sic")):
        merged["sic"] = sic_from_text

    pr = Prospect()
    d = parse_date(merged.get("effective_date"))
    pr.effective_date = d
    pr.sic = merged.get("sic", "")
    pr.company = re.sub(r"[,.]+$", "", merged.get("company", "")).strip()
    pr.address = merged.get("address", "")
    pr.city = merged.get("city", "")
    pr.state = merged.get("state", "")
    pr.current_carrier = merged.get("current_carrier", "")
    pr.peo_name = merged.get("peo", "")
    pr.funding_type = merged.get("funding", "")
    pr.salesforce_id = merged.get("salesforce_id", "")
    pr.dba = merged.get("dba", "")
    pr.ein = merged.get("ein", "")
    pr.contact_name = merged.get("contact_name", "")
    pr.contact_email = merged.get("contact_email", "")
    pr.contact_phone = merged.get("contact_phone", "")
    if merged.get("ft_employees"):
        try:
            pr.ft_employees = int(merged["ft_employees"])
        except (TypeError, ValueError):
            pr.ft_employees = None

    if merged.get("csz"):
        csz_clean = re.sub(r"\([^)]*\)", "", merged["csz"]).strip()
        pr.city_state_zip = csz_clean
    elif merged.get("city") and merged.get("state") and merged.get("zip"):
        z = re.sub(r"\.0$", "", merged["zip"])
        pr.city_state_zip = f"{merged['city']}, {merged['state']} {z}"

    if pr.state and "," in pr.state:
        pr.state = pr.state.split(",")[0].strip()

    # Extract or normalize state if needed
    if not pr.state and pr.city_state_zip:
        from router import normalize_state
        st = normalize_state(pr.city_state_zip)
        if st:
            pr.state = st

    m_zip = re.search(r"\b(\d{5})$", pr.city_state_zip or "")
    if m_zip and all_docs:
        z5 = m_zip.group(1)
        for d in all_docs:
            m4 = re.search(rf"\b{z5}-(\d{{4}})\b", d.text or "")
            if m4:
                pr.city_state_zip = f"{pr.city_state_zip}-{m4.group(1)}"
                break
    pr.referral = merged.get("referral", "")
    pr.sales_person = merged.get("sales_person", "")
    if pr.sales_person and pr.sales_person == pr.referral:
        pr.sales_person = ""

    required = [pr.effective_date, pr.sic, pr.company, pr.address, pr.city_state_zip, pr.referral, pr.sales_person]
    has_source = bool(sources) or found_in_other
    return pr, has_source, any(not v for v in required)
