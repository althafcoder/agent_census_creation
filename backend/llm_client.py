"""Anthropic Claude client operating strictly under the official instructions.

No custom or additional rules are introduced; all extraction and standardization strictly follows
the active official instructions document discovered from backend/instructions.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import contextvars
from typing import Any

from census_config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID, INSTRUCTIONS_PATH, INSTRUCTIONS_PATHS

log = logging.getLogger("census.llm")

# Context tracking for universal token utilization & cost analytics
_CURRENT_FILE: contextvars.ContextVar[str] = contextvars.ContextVar("census_current_file", default="benefits_census_submission")
_CURRENT_STEP: contextvars.ContextVar[str] = contextvars.ContextVar("census_current_step", default="llm_extraction")


def set_current_tracking(file_name: str | None = None, step_name: str | None = None) -> None:
    """Set current file and processing step for dynamic token usage tracking."""
    if file_name:
        _CURRENT_FILE.set(file_name)
    if step_name:
        _CURRENT_STEP.set(step_name)


# Load official instructions verbatim from the configured official instruction documents
_INSTRUCTIONS_CACHE: str | None = None
_INSTRUCTIONS_MTIMES: tuple[float, ...] = ()


def _read_docx_paragraphs(path: Path) -> str:
    """Read all non-empty paragraphs from a docx file."""
    try:
        import docx
        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs if p.text.strip())
    except Exception as exc:
        log.warning("Could not read docx %s: %s", path.name, exc)
        return ""


def get_instruction_doc_names() -> list[str]:
    """Return the list of filenames of all active official instruction documents."""
    try:
        from census_config import INSTRUCTIONS_PATHS
        if INSTRUCTIONS_PATHS:
            names = [p.name for p in INSTRUCTIONS_PATHS if p.exists()]
            if names:
                return names
    except Exception:
        pass
    instructions_dir = Path(__file__).resolve().parent / "instructions"
    files = sorted(instructions_dir.glob("*.docx"), key=lambda p: (0 if "combined" in p.name.lower() else 1, p.name.lower()))
    return [p.name for p in files if not p.name.startswith("~$")]


def get_instruction_doc_name() -> str:
    """Return a descriptive string of active official instruction documents."""
    names = get_instruction_doc_names()
    return "; ".join(names) if names else "Official Instructions"


def get_official_instructions() -> str:
    """Load and format all official verbatim instructions from the active instruction documents."""
    global _INSTRUCTIONS_CACHE, _INSTRUCTIONS_MTIMES

    paths: list[Path] = []
    try:
        from census_config import INSTRUCTIONS_PATHS
        if INSTRUCTIONS_PATHS:
            paths = [p for p in INSTRUCTIONS_PATHS if p.exists()]
    except Exception:
        pass

    if not paths:
        instructions_dir = Path(__file__).resolve().parent / "instructions"
        paths = sorted(instructions_dir.glob("*.docx"), key=lambda p: (0 if "combined" in p.name.lower() else 1, p.name.lower()))
        paths = [p for p in paths if not p.name.startswith("~$")]

    # Check if cached and file mtimes haven't changed
    current_mtimes = tuple(p.stat().st_mtime for p in paths if p.exists())
    if _INSTRUCTIONS_CACHE is not None and current_mtimes == _INSTRUCTIONS_MTIMES and current_mtimes:
        return _INSTRUCTIONS_CACHE

    sections: list[str] = []
    for i, path in enumerate(paths, 1):
        content = ""
        if path.suffix.lower() == ".docx":
            content = _read_docx_paragraphs(path)
        elif path.suffix.lower() == ".txt":
            try:
                content = path.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        if content.strip():
            banner = (
                f"================================================================================\n"
                f"OFFICIAL INSTRUCTION DOCUMENT {i}: {path.name}\n"
                f"================================================================================"
            )
            sections.append(f"{banner}\n\n{content.strip()}")

    if not sections:
        txt_file = Path(__file__).resolve().parent / "instructions" / "extracted_instruction.txt"
        if txt_file.exists():
            _INSTRUCTIONS_CACHE = txt_file.read_text(encoding="utf-8")
            _INSTRUCTIONS_MTIMES = (txt_file.stat().st_mtime,)
            return _INSTRUCTIONS_CACHE

    combined = "\n\n\n".join(sections)
    _INSTRUCTIONS_CACHE = combined
    _INSTRUCTIONS_MTIMES = current_mtimes
    return _INSTRUCTIONS_CACHE


# Backwards compatibility alias
get_official_rei_instructions = get_official_instructions


def get_system_prompt() -> str:
    """Dynamic system prompt referencing all active official instruction documents."""
    names = get_instruction_doc_names()
    doc_str = "; ".join(names) if names else "Official Instructions"
    return (
        f"You are the Census Creation and Invoice Comparison Engine operating strictly under the official instructions: "
        f"[{doc_str}]. Never provide analysis or extraneous prose. "
        "Return valid JSON only with zero markdown fences. Never invent or guess values; leave unavailable fields blank."
    )


_SYSTEM = get_system_prompt()


def _clean_json_text(text: str) -> str:
    """Strip thinking tags, markdown fences, and trim whitespace."""
    text = text.strip()
    text = re.sub(r"<thinking>[\s\S]*?</thinking>", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_robust(text: str) -> Any:
    """Parse JSON with fallback for wrapped or embedded markdown/content."""
    cleaned = _clean_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Search for outermost JSON object or list
        start_brace = cleaned.find("{")
        end_brace = cleaned.rfind("}")
        start_bracket = cleaned.find("[")
        end_bracket = cleaned.rfind("]")

        candidates: list[tuple[int, int]] = []
        if start_brace != -1 and end_brace != -1 and end_brace > start_brace:
            candidates.append((start_brace, end_brace + 1))
        if start_bracket != -1 and end_bracket != -1 and end_bracket > start_bracket:
            candidates.append((start_bracket, end_bracket + 1))

        candidates.sort(key=lambda x: x[0])
        for s, e in candidates:
            try:
                return json.loads(cleaned[s:e])
            except json.JSONDecodeError:
                continue
        raise


def extract_json(prompt: str, max_tokens: int = 4000) -> Any:
    """Execute prompt via Anthropic Claude and return parsed JSON data."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured in .env.")

    import anthropic

    headers = {}
    if ANTHROPIC_WORKSPACE_ID:
        headers["anthropic-workspace-id"] = ANTHROPIC_WORKSPACE_ID

    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        default_headers=headers if headers else None,
    )
    try:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=get_system_prompt(),
            messages=[
                {"role": "user", "content": prompt},
            ],
        )

        # Dynamically record token usage and cost for Token Utilization & Cost Analytics
        try:
            from core.universal_token_monitor import track_usage
            track_usage(
                resp.usage,
                model=ANTHROPIC_MODEL,
                poc_name="BENEFITS_UW",
                file_name=_CURRENT_FILE.get() or "benefits_census_submission",
                step_name=_CURRENT_STEP.get() or "llm_extraction",
            )
        except Exception as _track_err:
            log.debug("Token usage tracking skipped: %s", _track_err)

        content_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", "") == "text" or hasattr(block, "text"):
                content_parts.append(block.text)
            elif isinstance(block, dict) and "text" in block:
                content_parts.append(block["text"])
        content = "".join(content_parts)
        return _parse_json_robust(content)
    except Exception as exc:
        log.warning("Anthropic Claude API call failed: %s", exc)
        raise


def extract_prospect_fields(doc_text: str, rfp_text: str = "") -> dict:
    """Extract prospect information adhering solely to official instructions."""
    set_current_tracking(step_name="prospect_extraction")
    if rfp_text:
        combined_text = f"--- SUBMISSION EMAIL / RFP NOTES ---\n{rfp_text[:12000]}\n\n--- GHQ / QUESTIONNAIRE ---\n{doc_text[:25000]}"
    else:
        combined_text = doc_text[:35000]
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

SOURCE DOCUMENTS:
{combined_text}

Extract prospect-level information fields for the Census Template:
- proposed_effective_date: Requested effective date (e.g. "01/01/2027")
- sic_naics_code: Valid 4-digit SIC or 6-digit NAICS code. If unavailable, unconfirmed, or pending confirmation, leave blank ("").
- company_name: Official legal company name / prospect name
- address: Street physical address only (no city/state/zip)
- city: City
- state: 2-letter state abbreviation
- zip: 5-digit ZIP
- city_state_zip: "City, State ZIP"
- referral: Referring broker / agency or referral source (e.g. Professional Group Plans, OneDigital), including any commission split details if mentioned
- sales_person: Sales consultant / representative / PEO sales person (e.g. Jonathan Hernandez)
- current_carrier: Current medical insurance carrier (e.g. Aetna, Florida Blue)
- current_peo: Current PEO provider if applicable (e.g. ADP TotalSource)
- salesforce_id: Salesforce opportunity or prospect ID (e.g. HPQ-000651)
- onedigital_referral: OneDigital referral source, or blank if unavailable
- rei_sales_person: REI sales person, or blank if unavailable

If a value is unavailable in the source documents, leave the field blank rather than guessing.
Return JSON only:
{{
  "proposed_effective_date": "",
  "sic_naics_code": "",
  "company_name": "",
  "address": "",
  "city": "",
  "state": "",
  "zip": "",
  "city_state_zip": "",
  "referral": "",
  "sales_person": "",
  "current_carrier": "",
  "current_peo": "",
  "salesforce_id": "",
  "onedigital_referral": "",
  "rei_sales_person": ""
}}
"""
    try:
        data = extract_json(prompt, max_tokens=2000)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("extract_prospect_fields failed: %s", exc)
        return {}


def extract_invoice_rows(text: str) -> list[dict]:
    """Extract subscriber rows strictly adhering to official invoice instructions."""
    set_current_tracking(step_name="invoice_rows_extraction")
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

INVOICE DOCUMENT EXCERPTS:
{text[:45000]}

Extract each subscriber employee row from the invoice.
Keys required for each subscriber:
- name: Exact subscriber name as printed on invoice
- tier: Standard coverage tier abbreviation (EE, ES, EC, FAM)
- dob: Date of birth (mm/dd/yyyy) if available, else null
- gender: M or F if available, else null
- zip: 5-digit ZIP code if available, else null
- plan: Medical plan name enrolled
- premium: Monthly total premium for the employee (numeric float)
- status: Active, Terminated, COBRA, or current enrollment status
- member_id: Member ID / Subscriber ID

Return a JSON list of objects only:
[
  {{
    "name": "LAST, FIRST M",
    "tier": "EE",
    "dob": "01/15/1985",
    "gender": "M",
    "zip": "10019",
    "plan": "Plan Name",
    "premium": 450.00,
    "status": "Active",
    "member_id": "123456789"
  }}
]
"""
    try:
        data = extract_json(prompt, max_tokens=8000)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for val in data.values():
                if isinstance(val, list):
                    return val
    except Exception as exc:
        log.warning("extract_invoice_rows failed: %s", exc)
    return []


def extract_renewal_rates(text: str) -> list[dict]:
    """Extract medical renewal rate tables strictly adhering to official instructions."""
    set_current_tracking(step_name="renewal_rates_extraction")
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

RENEWAL DOCUMENT EXCERPTS:
{text[:30000]}

Extract medical renewal rate tables covering the upcoming renewal coverage period.
- Target medical plans only
- Tiers must use approved standard codes: EE, ES, EC, FAM
- Rates must be the monthly premium float for the upcoming renewal period
- Extract the renewal effective date

Return JSON list of objects only:
[
  {{
    "plan": "Medical Plan Name",
    "tier": "EE",
    "rate": 550.00,
    "effective_date": "01/01/2027"
  }}
]
"""
    try:
        data = extract_json(prompt, max_tokens=4000)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
    except Exception as exc:
        log.warning("extract_renewal_rates failed: %s", exc)
    return []


def extract_census_rows(text: str) -> list[dict]:
    """Extract employee and dependent records strictly adhering to official instructions."""
    set_current_tracking(step_name="census_rows_extraction")
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

CENSUS TEXT EXCERPTS:
{text[:45000]}

Extract all employee and dependent records:
- first_name, last_name
- gender: M or F
- dob: mm/dd/yyyy
- zip: 5-digit ZIP code
- relationship: EE for employee, SP for spouse, CH for children
- subscriber_name: If dependent, the full name of the employee they belong to
- coverage: Approved codes (EE, ES, EC, FAM, NE, WP, RC, WO)
- cobra: Y or N
- plan: Medical plan name if specified
- premium: Enrolled premium if specified

Return JSON list of objects only:
[
  {{
    "first_name": "John",
    "last_name": "Smith",
    "gender": "M",
    "dob": "05/12/1980",
    "zip": "10019",
    "relationship": "EE",
    "subscriber_name": "",
    "coverage": "EE",
    "cobra": "N",
    "plan": "PPO 500",
    "premium": 500.00
  }}
]
"""
    try:
        data = extract_json(prompt, max_tokens=8000)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
    except Exception as exc:
        log.warning("extract_census_rows failed: %s", exc)
    return []


def classify_document(text: str, filename: str) -> str:
    """Classify document role strictly adhering to official definitions."""
    set_current_tracking(file_name=filename, step_name="document_classification")
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

Classify this document into exactly one of these 6 roles:
1. "census": Employee or dependent census rosters, enrollment spreadsheets, benefits eligibility listings, demographic rosters.
2. "invoice": Monthly health insurance billing statement, payroll deduction register, carrier billing invoice, statement of premiums.
3. "renewal": Renewal rate sheet, client benefit election (CBE) renewal options, future plan year rates, renewal proposal.
4. "ghq": Group Health Questionnaire, company business information form, SIC code, prior carrier, employer questionnaire.
5. "rfp": Request for Proposal, submission email from broker/sales rep, submission notes with employer details.
6. "unknown": Document not related to health insurance census or billing.

Filename: {filename}
Document Text:
{text[:6000]}

Return JSON only:
{{"role": "census|invoice|renewal|ghq|rfp|unknown"}}
"""
    try:
        data = extract_json(prompt, max_tokens=500)
        if isinstance(data, dict):
            role = str(data.get("role", "")).lower().strip()
            if role in {"census", "invoice", "renewal", "ghq", "rfp", "unknown"}:
                return role
    except Exception as exc:
        log.warning("classify_document failed: %s", exc)
    return "unknown"


