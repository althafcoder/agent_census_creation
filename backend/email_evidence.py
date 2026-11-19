"""
Dynamic Client/Broker Email Evidence Extractor (Fix #5C).

Extracts structured person-level operational facts from client or broker submission
emails and RFP narratives — e.g. recent terminations ("Diego Zubieta was terminated
effective 11/6/2026, though he still appears on the most recent medical invoice"),
recent hires, or pending adds — without hardcoding any names or client formats.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Optional

from utils import clean, name_tokens, split_full_name

log = logging.getLogger("census.email_evidence")

_EXCLUDED_ROLES = {"invoice", "renewal", "plan_design", "payroll", "deliverable", "claims", "census"}

# Matches a capitalized person name (2-3 words, allowing hyphen/apostrophe/middle initial)
_NAME_PAT = r"([A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?)"
_DATE_PAT = r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})"

_TERM_PATTERNS = (
    # "Diego Zubieta was terminated effective 11/6/2026..."
    re.compile(
        rf"{_NAME_PAT}\s+(?:was|has\s+been|is)\s+(?:terminated|termed|separated|let\s+go|no\s+longer\s+employed)"
        rf"(?:[^.\n]{{0,40}}?(?:effective|on|as\s+of)\s+{_DATE_PAT})?",
        re.I,
    ),
    # "Diego Zubieta termed 11/6/2026" / "Diego Zubieta - terminated effective 11/6/2026"
    re.compile(
        rf"{_NAME_PAT}\s*(?:[-–:]\s*)?(?:terminated|termed|recent\s+term(?:ination)?)"
        rf"(?:[^.\n]{{0,30}}?(?:effective|on|as\s+of)?\s*{_DATE_PAT})?",
        re.I,
    ),
    # "Terminated employee: Diego Zubieta (effective 11/6/2026)"
    re.compile(
        rf"(?:terminated|termed|recent\s+term(?:ination)?)\s*(?:employee)?\s*[:\-]\s*{_NAME_PAT}"
        rf"(?:[^.\n]{{0,40}}?(?:effective|on|as\s+of)?\s*{_DATE_PAT})?",
        re.I,
    ),
)

_HIRE_PATTERNS = (
    re.compile(
        rf"{_NAME_PAT}\s+(?:was|is\s+a)?\s*(?:newly\s+hired|new\s+hire|hired|starts?|starting)"
        rf"(?:[^.\n]{{0,40}}?(?:effective|on|as\s+of)?\s*{_DATE_PAT})?",
        re.I,
    ),
)

_NON_PERSON_LEAD_WORDS = {
    "note", "please", "attention", "subject", "from", "sent", "body", "dear",
    "good", "hello", "hi", "best", "regards", "thanks", "thank", "current",
    "medical", "dental", "vision", "benefit", "benefits", "coverage", "employee",
    "employees", "group", "company", "client", "broker", "renewal", "invoice",
    "billing", "effective", "target", "recent", "total", "active", "cobra",
}


@dataclass
class PersonEvidence:
    raw_name: str
    first: str
    last: str
    event_type: str        # "terminated" | "new_hire"
    effective_date: str    # raw date string as stated in email (e.g. "11/6/2026")
    sentence: str
    source: str


def _looks_like_person_name(cand: str) -> bool:
    parts = [p.strip(".,;:") for p in clean(cand).split() if p.strip(".,;:")]
    if len(parts) < 2 or len(parts) > 4:
        return False
    if parts[0].lower() in _NON_PERSON_LEAD_WORDS or parts[-1].lower() in _NON_PERSON_LEAD_WORDS:
        return False
    if any(not p[0].isupper() for p in parts if len(p) > 1):
        return False
    return True


def _extract_sentence(text: str, start: int, end: int) -> str:
    # Expand to sentence / line boundaries cleanly
    left = max(text.rfind("\n\n", 0, start), text.rfind(". ", 0, start))
    left = 0 if left < 0 else left + 2
    right_nl = text.find("\n\n", end)
    right_dot = text.find(".", end)
    ends = [pos for pos in (right_nl, right_dot) if pos >= 0]
    right = min(ends) + 1 if ends else min(len(text), end + 160)
    sent = re.sub(r"\s+", " ", text[left:right]).strip()
    return sent[:260]


def extract_email_evidence(
    docs: Optional[list[Any]] = None,
    roles: Optional[dict[str, list[Any]]] = None,
) -> list[PersonEvidence]:
    """Scan client/broker submission emails & RFP narratives for person-level evidence."""
    excluded_ids: set[int] = set()
    candidates: list[Any] = []
    if roles:
        for rname in _EXCLUDED_ROLES:
            for d in roles.get(rname, []) or []:
                excluded_ids.add(id(d))
        for rname in ("rfp", "ghq", "unknown"):
            for d in roles.get(rname, []) or []:
                if id(d) not in excluded_ids:
                    candidates.append(d)
    elif docs:
        candidates = list(docs)

    out: list[PersonEvidence] = []
    seen: set[tuple[str, str]] = set()

    for d in candidates:
        text = getattr(d, "text", "") or ""
        if not text:
            continue
        # Normalize wrapped PDF lines inside paragraphs so "NOTE: Diego Zubieta was\nterminated" matches
        norm_text = re.sub(r"[ \t]+", " ", text)
        norm_text = re.sub(r"(?<!\n)\n(?!\n)", " ", norm_text)
        src = getattr(d, "name", "submission email")

        for event_type, patterns in (("terminated", _TERM_PATTERNS), ("new_hire", _HIRE_PATTERNS)):
            for pat in patterns:
                for m in pat.finditer(norm_text):
                    raw_nm = clean(m.group(1))
                    # Strip leading prose label like "NOTE: " if it leaked into capture
                    raw_nm = re.sub(r"^(?:NOTE|FYI|UPDATE|RE)\s*[:\-]\s*", "", raw_nm, flags=re.I).strip()
                    if not _looks_like_person_name(raw_nm):
                        continue
                    eff_dt = clean(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else ""
                    first, last = split_full_name(raw_nm)
                    key = (event_type, " ".join(name_tokens(first, last)))
                    if not key[1] or key in seen:
                        continue
                    seen.add(key)
                    sent = _extract_sentence(norm_text, m.start(), m.end())
                    out.append(PersonEvidence(
                        raw_name=raw_nm,
                        first=first,
                        last=last,
                        event_type=event_type,
                        effective_date=eff_dt,
                        sentence=sent,
                        source=src,
                    ))
                    log.info("Extracted email evidence (%s): %s eff=%s [%s]", event_type, raw_nm, eff_dt or "n/a", src)
    return out


def find_person_evidence(
    evidence_list: Optional[list[PersonEvidence]],
    first: str,
    last: str,
) -> Optional[PersonEvidence]:
    """Find matching ``PersonEvidence`` for ``(first, last)`` using order-insensitive name tokens."""
    if not evidence_list or not (first or last):
        return None
    target_toks = set(name_tokens(first, last))
    if not target_toks:
        return None
    last_low = clean(last).lower()
    for ev in evidence_list:
        ev_toks = set(name_tokens(ev.first, ev.last))
        if ev_toks == target_toks:
            return ev
        if last_low and last_low in ev_toks and (target_toks & ev_toks) and len(target_toks & ev_toks) >= 2:
            return ev
    return None
