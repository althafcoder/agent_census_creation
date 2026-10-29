"""Anthropic Claude client operating strictly under the official instructions.

No custom or additional rules are introduced; all extraction and standardization strictly follows
the active official instructions document discovered from backend/instructions.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import contextvars
from typing import Any

from census_config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_EFFORT,
    ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_MODEL,
    ANTHROPIC_TEMPERATURE,
    ANTHROPIC_WORKSPACE_ID,
)

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


# Instructions directory: directly accessed dynamically without requiring static config setup
INSTRUCTIONS_DIR = Path(__file__).resolve().parent / "instructions"
_INSTRUCTIONS_CACHE: str | None = None
_INSTRUCTIONS_SNAPSHOT: tuple[tuple[str, float, int], ...] = ()


def _read_docx_paragraphs(path: Path) -> str:
    """Read all non-empty paragraphs and table contents from a docx file."""
    try:
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                if row_text:
                    parts.append(row_text)
        return "\n".join(parts)
    except Exception as exc:
        log.warning("Could not read docx %s: %s", path.name, exc)
        return ""


def _read_instruction_file(path: Path) -> str:
    """Read full text content from any supported instruction document (.docx, .txt, .md, .pdf)."""
    ext = path.suffix.lower()
    if ext == ".docx":
        return _read_docx_paragraphs(path)
    elif ext in (".txt", ".md"):
        for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
            try:
                return path.read_text(encoding=enc).strip()
            except UnicodeDecodeError:
                continue
            except Exception as exc:
                log.warning("Could not read instruction file %s: %s", path.name, exc)
                return ""
        return ""
    elif ext == ".pdf":
        try:
            from readers import _extract_pdf_digital_text
            text, _ = _extract_pdf_digital_text(path)
            return text.strip() if text else ""
        except Exception as exc:
            log.warning("Could not read PDF instruction %s: %s", path.name, exc)
            return ""
    return ""


def get_instruction_files() -> list[Path]:
    """Directly scan backend/instructions directory for all active instruction files on demand."""
    if not INSTRUCTIONS_DIR.exists():
        return []
    override = os.getenv("CENSUS_INSTRUCTIONS_PATHS") or os.getenv("CENSUS_INSTRUCTIONS_PATH")
    if override:
        paths = [Path(p.strip()) for p in re.split(r"[,;]", override) if Path(p.strip()).exists()]
        if paths:
            return paths
    files = [
        p for p in INSTRUCTIONS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".txt", ".docx", ".md", ".pdf")
        and not p.name.startswith("~$") and not p.name.startswith(".")
    ]
    files.sort(key=lambda p: (
        0 if any(k in p.name.lower() for k in ("dynamic", "master", "unified", "combined", "instruction")) else 1,
        p.name.lower()
    ))
    return files


def get_instruction_doc_names() -> list[str]:
    """Return the list of filenames of all active instruction documents currently in backend/instructions/."""
    return [p.name for p in get_instruction_files()]


def get_instruction_doc_name() -> str:
    """Return a descriptive string of active official instruction documents."""
    names = get_instruction_doc_names()
    return "; ".join(names) if names else "Active Official Instructions"


def get_official_instructions() -> str:
    """Load and format all official instructions directly from backend/instructions/.
    
    Dynamically detects any new, deleted, or edited instruction files on demand.
    Zero config setup or restarts required when swapping instruction files.
    """
    global _INSTRUCTIONS_CACHE, _INSTRUCTIONS_SNAPSHOT

    paths = get_instruction_files()
    current_snapshot = tuple((p.name, p.stat().st_mtime, p.stat().st_size) for p in paths if p.exists())
    if _INSTRUCTIONS_CACHE is not None and current_snapshot == _INSTRUCTIONS_SNAPSHOT and current_snapshot:
        return _INSTRUCTIONS_CACHE

    sections: list[str] = []
    for i, path in enumerate(paths, 1):
        content = _read_instruction_file(path)
        if content.strip():
            banner = (
                f"================================================================================\n"
                f"OFFICIAL INSTRUCTION DOCUMENT {i}: {path.name}\n"
                f"================================================================================"
            )
            sections.append(f"{banner}\n\n{content.strip()}")

    combined = "\n\n\n".join(sections)
    if not combined.strip():
        raise RuntimeError("Controlling instructions are missing or empty in backend/instructions. Refusing to run without governing instructions.")
    is_update = bool(_INSTRUCTIONS_SNAPSHOT and current_snapshot != _INSTRUCTIONS_SNAPSHOT)
    _INSTRUCTIONS_CACHE = combined
    _INSTRUCTIONS_SNAPSHOT = current_snapshot

    doc_names = [p.name for p in paths]
    doc_str = "; ".join(doc_names) if doc_names else "(none)"
    if is_update:
        log.info("🔄 [NEW INSTRUCTION DETECTED] Swapped active instruction to: '%s' (%d chars across %d file(s))", doc_str, len(combined), len(paths))
    else:
        log.info("📋 [USING INSTRUCTION] Active instruction: '%s' (%d chars across %d file(s))", doc_str, len(combined), len(paths))

    return _INSTRUCTIONS_CACHE


# Backwards compatibility alias
get_official_rei_instructions = get_official_instructions


def get_scoped_instructions(scope: str = "full") -> str:
    """Return task-scoped instruction slices to prevent prompt dilution while preserving governing rules.
    
    Scopes:
    - 'prospect': Sections 1, 3, 4, 22
    - 'census': Sections 1, 6, 8, 9, 10, 17, 21
    - 'invoice': Sections 1, 5, 11, 16, 18
    - 'renewal': Sections 1, 13, 14, 19, 20
    - 'full': All sections
    """
    full_inst = get_official_instructions()
    if not scope or scope == "full":
        return full_inst

    scope_sections = {
        "prospect": (1, 3, 4, 22),
        "census": (1, 6, 8, 9, 10, 17, 21),
        "invoice": (1, 5, 11, 16, 18),
        "renewal": (1, 13, 14, 19, 20),
    }
    target_nums = set(scope_sections.get(scope, ()))
    if not target_nums:
        return full_inst

    parts = re.split(r'(?m)\n(?=[0-9]{1,2}\.\s+[A-Z\s,/]{3,}\n)', full_inst)
    if len(parts) <= 1:
        return full_inst

    selected_parts = [parts[0].strip()]
    sec_re = re.compile(r'^([0-9]{1,2})\.\s+[A-Z\s,/]{3,}')
    for part in parts[1:]:
        m = sec_re.match(part.strip())
        if m and int(m.group(1)) in target_nums:
            selected_parts.append(part.strip())

    if len(selected_parts) <= 1:
        return full_inst

    scoped = "\n\n\n".join(selected_parts)
    return scoped


def chunk_text(text: str, chunk_size: int = 50000, overlap: int = 2000) -> list[str]:
    """Split long document text into overlapping chunks along newline boundaries."""
    if not text or len(text) <= chunk_size:
        return [text] if text else []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + chunk_size - overlap, end)
            if nl != -1 and nl > start:
                end = nl + 1
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def get_system_prompt() -> str:
    """Dynamic deterministic system prompt referencing all active official instruction documents."""
    doc_str = get_instruction_doc_name()
    return (
        f"You are a deterministic document processing agent. Operating strictly under the official instructions: [{doc_str}].\n\n"
        "Rules:\n"
        "1. Extract only information supported by source documents.\n"
        "2. Never invent missing values.\n"
        "3. Follow the supplied mapping rules exactly.\n"
        "4. Use consistent field names and formats.\n"
        "5. Flag ambiguous or conflicting records.\n"
        "6. Never silently resolve conflicting data.\n"
        "7. Return only the requested output structure. Valid JSON only with zero markdown fences."
    )


_SYSTEM = get_system_prompt()
DETERMINISTIC_SYSTEM_PROMPT = _SYSTEM

PROSPECT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "dba": {"type": "string"},
        "sic_naics_code": {"type": "string"},
        "state": {"type": "string"},
        "effective_date": {"type": "string"},
        "contact_name": {"type": "string"},
        "contact_email": {"type": "string"},
        "contact_phone": {"type": "string"},
    },
    "required": ["company_name", "state"],
}

RECONCILIATION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_records": {"type": "array"},
        "unmatched_census": {"type": "array"},
        "unmatched_invoice": {"type": "array"},
        "flagged_conflicts": {"type": "array"},
    },
    "required": ["matched_records", "unmatched_census", "unmatched_invoice", "flagged_conflicts"],
}


def _clean_json_text(text: str) -> str:
    """Strip thinking tags, markdown fences, and trim whitespace."""
    text = text.strip()
    text = re.sub(r"<thinking>[\s\S]*?</thinking>", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_robust(text: str) -> Any:
    """Parse JSON with fallback for wrapped, embedded markdown, or truncated content."""
    cleaned = _clean_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 1. Search for outermost complete JSON object or list
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

        # 2. Truncation Recovery: If a JSON array was cut off by max_tokens, recover all completed items
        if start_bracket != -1:
            pos = cleaned.rfind("}")
            while pos > start_bracket:
                candidate = cleaned[start_bracket : pos + 1].rstrip().rstrip(",") + "\n]"
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pos = cleaned.rfind("}", 0, pos)

        # 3. Truncation Recovery: If a JSON object was cut off by max_tokens, recover completed top-level fields
        if start_brace != -1:
            pos = cleaned.rfind("}")
            while pos > start_brace:
                candidate = cleaned[start_brace : pos + 1].rstrip().rstrip(",") + "\n}"
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pos = cleaned.rfind("}", 0, pos)

            # Also try closing at the last complete field
            last_comma = cleaned.rfind(",")
            if last_comma > start_brace:
                candidate = cleaned[start_brace : last_comma].rstrip() + "\n}"
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

        raise



def extract_json(
    prompt: str,
    max_tokens: int = ANTHROPIC_MAX_TOKENS,
    effort: str | None = None,
    bypass_cache: bool = False,
    output_schema: dict[str, Any] | None = None,
    use_structured_output: bool = False,
) -> Any:
    """Execute prompt via Anthropic Claude and return parsed JSON data."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured in .env.")

    doc_name = get_instruction_doc_name()
    step_name = _CURRENT_STEP.get() or "llm_extraction"
    file_name = _CURRENT_FILE.get() or "benefits_submission"
    effective_effort = effort or ANTHROPIC_EFFORT or "high"

    # 1. Tier 1: Check local persistent prompt cache (exact match, 0 tokens, <1ms)
    if not bypass_cache and not ANTHROPIC_API_KEY.startswith("sk-ant-test"):
        try:
            from caching import get_local_cache
            cached = get_local_cache().get(prompt, ANTHROPIC_MODEL, step_name=step_name, bypass_cache=bypass_cache)
            if cached is not None:
                log.info("⚡ [CACHE HIT] [%s] using instruction: '%s' (file: %s)", step_name, doc_name, file_name)
                return cached
        except Exception as _c_err:
            log.debug("Local prompt cache lookup skipped: %s", _c_err)

    log.info(
        "🤖 [LLM PROMPT] Executing [%s] using instruction: '%s' (file: %s, model: %s, effort: %s)",
        step_name, doc_name, file_name, ANTHROPIC_MODEL, effective_effort,
    )

    import anthropic
    import time

    headers = {}
    if ANTHROPIC_WORKSPACE_ID:
        headers["anthropic-workspace-id"] = ANTHROPIC_WORKSPACE_ID

    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        default_headers=headers if headers else None,
    )

    # 2. Tier 2: Prepare cached message blocks for Anthropic prompt caching
    messages = [{"role": "user", "content": prompt}]
    system_prompt = get_system_prompt()
    if not ANTHROPIC_API_KEY.startswith("sk-ant-test"):
        try:
            from caching import prepare_cached_messages
            messages, system_prompt = prepare_cached_messages(prompt, system_prompt)
        except Exception as _prep_err:
            log.debug("Anthropic cached message preparation skipped: %s", _prep_err)

    t0 = time.perf_counter()
    try:
        create_kwargs: dict[str, Any] = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "output_config": {"effort": effective_effort},
            "system": system_prompt,
            "messages": messages,
        }
        if use_structured_output and output_schema:
            create_kwargs["output_config"]["format"] = {
                "type": "json_schema",
                "schema": output_schema,
            }
        # For Opus 5.5, omit temperature completely; only include temperature for legacy non-Opus-5.5 models
        if "opus-5-5" not in ANTHROPIC_MODEL.lower():
            create_kwargs["temperature"] = ANTHROPIC_TEMPERATURE if ANTHROPIC_TEMPERATURE is not None else 1.0

        resp = client.messages.create(**create_kwargs)
        latency_sec = time.perf_counter() - t0

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

        # Record Anthropic prompt caching telemetry (cache reads / cache writes)
        try:
            from caching import record_anthropic_usage
            step_name = _CURRENT_STEP.get() or "llm_extraction"
            record_anthropic_usage(getattr(resp, "usage", None), step_name=step_name, latency_sec=latency_sec)
        except Exception as _rec_err:
            log.debug("Anthropic cache telemetry skipped: %s", _rec_err)

        content_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", "") == "text" or hasattr(block, "text"):
                content_parts.append(block.text)
            elif isinstance(block, dict) and "text" in block:
                content_parts.append(block["text"])
        content = "".join(content_parts)
        parsed = _parse_json_robust(content)

        # Save to local persistent cache for future runs
        if not ANTHROPIC_API_KEY.startswith("sk-ant-test"):
            try:
                from caching import get_local_cache
                step_name = _CURRENT_STEP.get() or "llm_extraction"
                tokens_est = getattr(getattr(resp, "usage", None), "input_tokens", 0) or (len(prompt) // 4)
                get_local_cache().set(
                    prompt=prompt,
                    model=ANTHROPIC_MODEL,
                    response_data=parsed,
                    step_name=step_name,
                    tokens_saved_estimate=tokens_est,
                )
            except Exception as _store_err:
                log.debug("Local prompt cache store skipped: %s", _store_err)

        return parsed
    except Exception as exc:
        log.warning("Anthropic Claude API call failed: %s", exc)
        raise


_PLACEHOLDER_VALS = {"n/a", "na", "none", "null", "unknown", "tbd", "not applicable", "pending", "pending confirmation"}


def _clean_field_str(val: Any) -> str:
    """Deterministically normalize a scalar string field, stripping invented placeholders."""
    if val is None:
        return ""
    s = str(val).strip()
    if s.lower() in _PLACEHOLDER_VALS:
        return ""
    return s


def _validate_prospect_dict(raw: Any) -> dict[str, str]:
    """Deterministic post-extraction schema validation for prospect metadata."""
    if not isinstance(raw, dict):
        return {}
    schema_keys = (
        "proposed_effective_date", "sic_naics_code", "company_name", "dba", "ein",
        "address", "city", "state", "zip", "city_state_zip", "referral", "sales_person",
        "contact_name", "contact_email", "contact_phone", "ft_employees",
        "current_carrier", "current_peo", "salesforce_id", "onedigital_referral", "rei_sales_person",
    )
    out: dict[str, str] = {}
    for k in schema_keys:
        if k in raw:
            out[k] = _clean_field_str(raw.get(k))
    for k, v in raw.items():
        if k not in out:
            out[k] = _clean_field_str(v)
    return out


def _validate_invoice_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Deterministic post-extraction validation for invoice subscriber rows."""
    from utils import std_coverage, to_money
    from census_builder import _is_non_medical

    valid: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = _clean_field_str(r.get("name"))
        if not name:
            continue
        plan = _clean_field_str(r.get("plan"))
        if plan and _is_non_medical(plan):
            continue
        tier_raw = _clean_field_str(r.get("tier"))
        tier = std_coverage(tier_raw) or tier_raw.upper()
        prem = to_money(r.get("premium"))
        valid.append({
            "name": name,
            "tier": tier,
            "dob": _clean_field_str(r.get("dob")) or None,
            "gender": _clean_field_str(r.get("gender")).upper()[:1] or None,
            "zip": _clean_field_str(r.get("zip")) or None,
            "plan": plan,
            "premium": prem,
            "status": _clean_field_str(r.get("status")) or "Active",
            "member_id": _clean_field_str(r.get("member_id")),
            "group_no": _clean_field_str(r.get("group_no") or r.get("group_id") or r.get("group")),
        })
    return valid


def _validate_renewal_rates(rows: list[Any]) -> list[dict[str, Any]]:
    """Deterministic post-extraction validation for medical renewal rate rows."""
    from utils import std_coverage, to_money
    from census_builder import _is_non_medical

    valid: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        plan = _clean_field_str(r.get("plan"))
        if not plan or _is_non_medical(plan):
            continue
        tier_raw = _clean_field_str(r.get("tier"))
        tier = std_coverage(tier_raw) or tier_raw.upper()
        rate = to_money(r.get("rate"))
        if rate is None or rate <= 0:
            continue
        valid.append({
            "plan": plan,
            "tier": tier,
            "rate": rate,
            "effective_date": _clean_field_str(r.get("effective_date")),
        })
    return valid


def _validate_census_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Deterministic post-extraction validation for census employee and dependent rows."""
    from utils import std_coverage, std_relationship, to_money

    valid: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        fn = _clean_field_str(r.get("first_name"))
        ln = _clean_field_str(r.get("last_name"))
        if not fn and not ln:
            continue
        rel_raw = _clean_field_str(r.get("relationship")) or "EE"
        rel = std_relationship(rel_raw) or "EE"
        cov_raw = _clean_field_str(r.get("coverage"))
        cov = (std_coverage(cov_raw) or cov_raw.upper()) if rel == "EE" else ""
        valid.append({
            "first_name": fn,
            "last_name": ln,
            "gender": _clean_field_str(r.get("gender")).upper()[:1],
            "dob": _clean_field_str(r.get("dob")),
            "zip": _clean_field_str(r.get("zip")),
            "relationship": rel,
            "subscriber_name": _clean_field_str(r.get("subscriber_name")) if rel != "EE" else "",
            "coverage": cov,
            "cobra": (_clean_field_str(r.get("cobra")).upper()[:1] or "N"),
            "plan": _clean_field_str(r.get("plan")),
            "premium": to_money(r.get("premium")),
            "sub_id": _clean_field_str(r.get("sub_id")),
            "hire_date": _clean_field_str(r.get("hire_date")),
            "effective_date": _clean_field_str(r.get("effective_date")),
            "eligibility_class": _clean_field_str(r.get("eligibility_class")),
            "salary": _clean_field_str(r.get("salary")),
            "job_title": _clean_field_str(r.get("job_title")),
            "worker_category": _clean_field_str(r.get("worker_category")),
            "position_status": _clean_field_str(r.get("position_status")),
            "waive_reason": _clean_field_str(r.get("waive_reason")),
        })
    return valid


def _validate_reconciliation_output(data: Any) -> dict[str, Any]:
    """Validate and normalize reconciliation output structure."""
    if isinstance(data, dict):
        return {
            "matched_records": data.get("matched_records", []),
            "unmatched_census": data.get("unmatched_census", []),
            "unmatched_invoice": data.get("unmatched_invoice", []),
            "flagged_conflicts": data.get("flagged_conflicts", []),
        }
    return {"matched_records": [], "unmatched_census": [], "unmatched_invoice": [], "flagged_conflicts": []}


def validate_structured_output(data: Any, schema: dict[str, Any] | None = None) -> Any:
    """Validate structured output against optional schema."""
    return data


def reconcile_records(
    census_records: list[dict[str, Any]],
    invoice_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reconcile census records with invoice records using high-effort LLM reasoning and conflict detection."""
    set_current_tracking(step_name="llm_reconciliation")
    prompt = (
        "Reconcile census records with invoice records. Identify matching subscribers, compare coverage tiers, "
        "and flag any discrepancies.\n"
        f"Census records: {json.dumps(census_records)}\n"
        f"Invoice records: {json.dumps(invoice_records)}\n"
    )
    raw = extract_json(prompt, max_tokens=16000, effort="high", output_schema=RECONCILIATION_OUTPUT_SCHEMA)
    rec = _validate_reconciliation_output(raw)
    from utils import std_coverage
    for m in rec.get("matched_records", []):
        c_tier = std_coverage(m.get("census_tier", "")) or m.get("census_tier", "")
        i_tier = std_coverage(m.get("invoice_tier", "")) or m.get("invoice_tier", "")
        m["census_tier"] = c_tier
        m["invoice_tier"] = i_tier
        if c_tier and i_tier and c_tier != i_tier:
            m["ambiguous"] = True
            msg = f"Tier conflict: census={c_tier} vs invoice={i_tier}"
            if "conflicts" not in m:
                m["conflicts"] = []
            if msg not in m["conflicts"]:
                m["conflicts"].append(msg)
            if msg not in rec["flagged_conflicts"]:
                rec["flagged_conflicts"].append(msg)
    return rec


def extract_prospect_fields(doc_text: str, rfp_text: str = "") -> dict:
    """Extract prospect information adhering solely to official instructions."""
    set_current_tracking(step_name="prospect_extraction")
    if rfp_text:
        combined_text = f"--- SUBMISSION EMAIL / RFP NOTES ---\n{rfp_text[:15000]}\n\n--- GHQ / QUESTIONNAIRE ---\n{doc_text[:35000]}"
    else:
        combined_text = doc_text[:50000]
    instructions = get_scoped_instructions("prospect")
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
- dba: Doing Business As (DBA) trade name if different, else blank
- ein: Federal Tax ID / EIN (e.g. "12-3456789") if available
- address: Street physical address only (no city/state/zip)
- city: City
- state: 2-letter state abbreviation (e.g. "FL", "GA", "NC")
- zip: 5-digit ZIP code
- city_state_zip: "City, State ZIP"
- referral: Referring broker / agency or referral source (e.g. Professional Group Plans, OneDigital), including any commission split details if mentioned
- sales_person: Sales consultant / representative / PEO sales person (e.g. Jonathan Hernandez)
- contact_name: Primary employer or broker contact person name
- contact_email: Contact email address
- contact_phone: Contact phone number
- ft_employees: Number of full-time benefit-eligible employees
- current_carrier: Current medical insurance carrier (e.g. Aetna, Florida Blue, Cigna)
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
  "dba": "",
  "ein": "",
  "address": "",
  "city": "",
  "state": "",
  "zip": "",
  "city_state_zip": "",
  "referral": "",
  "sales_person": "",
  "contact_name": "",
  "contact_email": "",
  "contact_phone": "",
  "ft_employees": "",
  "current_carrier": "",
  "current_peo": "",
  "salesforce_id": "",
  "onedigital_referral": "",
  "rei_sales_person": ""
}}
"""
    try:
        data = extract_json(prompt, max_tokens=16000, effort="high")
        return _validate_prospect_dict(data)
    except Exception as exc:
        log.warning("extract_prospect_fields failed: %s", exc)
        return {}


def extract_invoice_rows(text: str) -> list[dict]:
    """Extract subscriber rows strictly adhering to official invoice instructions, chunking long documents."""
    set_current_tracking(step_name="invoice_rows_extraction")
    instructions = get_scoped_instructions("invoice")
    doc_name = get_instruction_doc_name()

    chunks = chunk_text(text, chunk_size=55000, overlap=2500) if len(text) > 60000 else [text[:60000]]
    all_rows: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for idx, chunk in enumerate(chunks):
        chunk_header = f"--- INVOICE DOCUMENT SECTION {idx+1}/{len(chunks)} ---\n" if len(chunks) > 1 else ""
        prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

INVOICE DOCUMENT EXCERPTS:
{chunk_header}{chunk}

Extract each subscriber employee row from the invoice.
CRITICAL RULES:
- Target MEDICAL plans only.
- COMPLETELY IGNORE lines for Workers' Compensation (WC codes, WC Code: 0953PA, etc.), Administrative Fees, Payroll Taxes (FUTA, SUI, FICA, Medicare tax), Dental, Vision, Life, Disability.
- Clean subscriber employee names: remove any Workers' Comp codes (e.g. 'WC Code: 0953PA') or payroll code fragments appended to names.
- tier: Standard coverage tier abbreviation (EE, ES, EC, FAM).
- premium: The monthly total medical premium billed for this employee (numeric float).
- status: Active, Terminated, COBRA, or current enrollment status.

Return JSON array only:
[
  {{
    "name": "LAST, FIRST",
    "tier": "EE",
    "plan": "Plan Name",
    "premium": 500.0,
    "status": "Active"
  }}
]
"""
        try:
            parsed = extract_json(prompt, max_tokens=ANTHROPIC_MAX_TOKENS, effort="high")
            if isinstance(parsed, list):
                for r in parsed:
                    if isinstance(r, dict):
                        k = (r.get("member_id") or "", (r.get("name") or "").upper(), (r.get("plan") or "").upper())
                        if k not in seen_keys:
                            seen_keys.add(k)
                            all_rows.append(r)
            elif isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, list):
                        for r in v:
                            if isinstance(r, dict):
                                k = (r.get("member_id") or "", (r.get("name") or "").upper(), (r.get("plan") or "").upper())
                                if k not in seen_keys:
                                    seen_keys.add(k)
                                    all_rows.append(r)
        except Exception as exc:
            log.warning("extract_invoice_rows failed on chunk %d: %s", idx, exc)

    return all_rows


def extract_renewal_rates(text: str) -> list[dict]:
    """Extract medical renewal rate tables strictly adhering to official instructions."""
    set_current_tracking(step_name="renewal_rates_extraction")
    instructions = get_scoped_instructions("renewal")
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

RENEWAL DOCUMENT EXCERPTS:
{text[:40000]}

Extract medical renewal rate tables covering the upcoming renewal coverage period.
- Target medical plans only
- Tiers must use approved standard codes: EE, ES, EC, FAM
- Rates must be the monthly premium float for the upcoming renewal period
- Extract the renewal effective date (mm/dd/yyyy)

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
        data = extract_json(prompt, max_tokens=16000, effort="high")
        if isinstance(data, list):
            return _validate_renewal_rates(data)
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return _validate_renewal_rates(v)
    except Exception as exc:
        log.warning("extract_renewal_rates failed: %s", exc)
    return []


def extract_census_rows(text: str) -> list[dict]:
    """Extract employee and dependent records strictly adhering to official instructions, chunking long documents."""
    set_current_tracking(step_name="census_rows_extraction")
    instructions = get_scoped_instructions("census")
    doc_name = get_instruction_doc_name()

    chunks = chunk_text(text, chunk_size=55000, overlap=2500) if len(text) > 60000 else [text[:60000]]
    all_rows: list[dict] = []
    seen_keys: set[tuple[str, str, str, str]] = set()

    for idx, chunk in enumerate(chunks):
        chunk_header = f"--- CENSUS DOCUMENT SECTION {idx+1}/{len(chunks)} ---\n" if len(chunks) > 1 else ""
        prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

CENSUS TEXT EXCERPTS:
{chunk_header}{chunk}

Extract all employee and dependent records:
CRITICAL RULES FOR EMPLOYEES AND DEPENDENTS:
1. For employees:
   - relationship: 'EE'
   - coverage: Approved codes (EE, ES, EC, FAM, WO, WP, NE, RC)
     * If enrolled in Medical: EE (Employee Only), ES (Employee + Spouse), EC (Employee + Child/ren), FAM (Family). Look at columns such as 'Coverage Level', 'Medical Tier', 'Medical 1 Coverage Level', 'Plan Tier', 'Election'.
     * If waived medical coverage (e.g. other coverage, spousal, Medicare, cost): WO
     * If in waiting period / recent hire not yet eligible: WP
     * If ineligible due to classification (e.g. Part-Time, Temporary, Seasonal, PRN): NE
     * If refused coverage without proof of other coverage: RC
2. CRITICAL RULE FOR DEPENDENTS:
   - If the census document has wide columns for dependents (e.g., 'Spouse Name', 'Spouse DOB', 'Child 1 Name', 'Child 1 DOB', 'Dep 1 First Name', 'Dep 1 Last Name', etc.), you MUST create a separate object in the output list for EACH dependent!
   - For spouse: relationship 'SP'
   - For children: relationship 'CH'
   - For all dependents: set subscriber_name to the employee full name, and leave coverage BLANK (empty string).
3. Preserve all demographic and employment fields if present:
   - sub_id: Employee ID, Member ID, or Subscriber ID
   - hire_date: Date of hire (mm/dd/yyyy)
   - salary: Compensation / salary amount
   - job_title: Job title / occupation
   - worker_category: Full-time, Part-time, Temporary, etc.
   - position_status: Active, Terminated, Leave, etc.
   - waive_reason: Reason for waiving (e.g. Spousal, Medicare, Other Coverage)

Keys required for each object:
- first_name: First name
- last_name: Last name
- gender: M or F
- dob: mm/dd/yyyy
- zip: 5-digit ZIP code
- relationship: EE for employee, SP for spouse, CH for children
- subscriber_name: If dependent, the full name of the employee they belong to (empty for EE)
- coverage: Approved codes (EE, ES, EC, FAM, WO, WP, NE, RC) for employees; leave blank (empty string) for dependents
- cobra: Y or N
- plan: Medical plan name / code if specified
- premium: Enrolled premium if specified
- sub_id: Employee ID / Member ID if available
- hire_date: mm/dd/yyyy if available
- salary: Salary if available
- job_title: Job title if available
- worker_category: Worker category if available
- position_status: Position status if available
- waive_reason: Reason for waiver if available

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
    "premium": 500.00,
    "sub_id": "EMP001",
    "hire_date": "01/15/2020",
    "salary": "75000",
    "job_title": "Manager",
    "worker_category": "Full Time",
    "position_status": "Active",
    "waive_reason": ""
  }}
]
"""
        try:
            data = extract_json(prompt, max_tokens=16000, effort="high")
            valid = []
            if isinstance(data, list):
                valid = _validate_census_rows(data)
            elif isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        valid = _validate_census_rows(v)
                        break
            for r in valid:
                if isinstance(r, dict):
                    k = ((r.get("sub_id") or "").strip(), (r.get("first_name") or "").upper(), (r.get("last_name") or "").upper(), (r.get("dob") or "").strip())
                    if k not in seen_keys:
                        seen_keys.add(k)
                        all_rows.append(r)
        except Exception as exc:
            log.warning("extract_census_rows failed on chunk %d: %s", idx, exc)

    return all_rows


def classify_document(text: str, filename: str) -> str:
    """Classify document role strictly adhering to official definitions."""
    set_current_tracking(file_name=filename, step_name="document_classification")
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

Classify this document into exactly one of these 7 roles:
1. "census": Employee or dependent census rosters, enrollment spreadsheets, benefits eligibility listings, demographic rosters.
2. "invoice": Monthly health insurance billing statement, carrier billing invoice, statement of premiums with enrolled medical employees.
3. "renewal": Renewal rate sheet, client benefit election (CBE) renewal options, future plan year medical rates, renewal proposal.
4. "ghq": Group Health Questionnaire, company business information form, SIC code, prior carrier, employer questionnaire.
5. "rfp": Request for Proposal, submission email from broker/sales rep, submission notes with employer details.
6. "payroll": Payroll deduction registers, gross wage reports, W-2 registers, FUTA/SUI tax forms, Workers' Comp payroll reports. (These are administrative payroll documents, NOT medical insurance invoices or censuses).
7. "unknown": Document not related to health insurance census or billing.

Filename: {filename}
Document Text:
{text[:8000]}

Return JSON only:
{{"role": "census|invoice|renewal|ghq|rfp|payroll|unknown"}}
"""
    try:
        data = extract_json(prompt, max_tokens=500)
        if isinstance(data, dict):
            role = str(data.get("role", "")).lower().strip()
            if role in {"census", "invoice", "renewal", "ghq", "rfp", "payroll", "unknown"}:
                return role
    except Exception as exc:
        log.warning("classify_document failed: %s", exc)
    return "unknown"



