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
    "company": ["companyname", "legalname", "prospectname", "employername", "nameofcompany", "businessname",
                "groupname", "organizationname", "legalbusinessname", "nameofemployer", "company", "employer"],
    "address": ["address", "streetaddress", "businessaddress", "mailingaddress", "addressline1", "address1",
                "physicaladdress"],
    "city": ["city"],
    "state": ["state"],
    "zip": ["zip", "zipcode", "postalcode"],
    "csz": ["citystatezip", "citystatezipcode", "citystzip"],
    "referral": ["onedigitalreferral", "onedigitalreferal", "referral", "referredby", "referralsource",
                 "broker", "brokeragency", "prospectbroker", "prospectreferral"],
    "sales_person": ["reisalesperson", "reisalesrep", "salesperson", "salesrep", "salesrepresentative",
                     "accountexecutive", "salesexecutive"],
    "current_carrier": ["currentcarrier", "medicalcarrier", "carrier", "priorcarrier"],
    "peo": ["currentpeo", "peo", "peoname", "payrollpeo"],
    "funding": ["fundingtype", "fundingarrangement", "planfunding", "funding"],
}


def _kv_pairs(doc: RawDoc) -> dict[str, str]:
    """label -> value from 'Label: value' text lines and from label/value table cells."""
    pairs: dict[str, str] = {}
    for line in (doc.text or "").splitlines():
        if ":" in line:
            if line.count(":") > 1:
                continue  # skip multi-column form lines that bundle multiple labels together
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


def build_prospect(ghq_docs: list[RawDoc], rfp_docs: list[RawDoc], all_docs: list[RawDoc] | None = None) -> tuple[Prospect, bool, bool]:
    """Returns (prospect, any_source_found, any_field_missing) strictly following REI instructions."""
    import pandas as pd
    sources = list(ghq_docs) + list(rfp_docs)
    merged = {f: "" for f in LABELS}

    ghq_text = "\n\n".join(d.text for d in ghq_docs if d.text)
    rfp_text = "\n\n".join(d.text for d in rfp_docs if d.text)

    # 1. Rule-based extraction pass on GHQ / RFP
    for doc in sources:
        vals = _from_doc(doc)
        for k, v in vals.items():
            if v and not merged[k]:
                merged[k] = v

    if _is_invalid_sic(merged.get("sic", "")):
        merged["sic"] = ""

    # 2. Claude Anthropic extraction from GHQ / RFP
    from census_config import USE_LLM_FALLBACK
    if USE_LLM_FALLBACK and (ghq_text.strip() or rfp_text.strip()):
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
                log.info("      [PROSPECT] Reviewing all uploaded documents (census notes & invoices) per REI instructions...")
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

    if merged.get("csz"):
        pr.city_state_zip = merged["csz"]
    elif merged.get("city") and merged.get("state") and merged.get("zip"):
        z = re.sub(r"\.0$", "", merged["zip"])
        pr.city_state_zip = f"{merged['city']}, {merged['state']} {z}"

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
