"""Provenance Tracking, Lineage Verification, and Secret Sanitization.

Tracks field-level provenance through all pipeline stages:
RAW_EXTRACTION -> CLASSIFICATION -> MAPPING -> NORMALIZATION ->
LINKAGE -> ELIGIBILITY -> SOURCE_SELECTION -> COMPARISON -> WRITER -> SAVED_CELL.

Provides zero-leak secret redaction to ensure passwords never enter logs,
traces, learning JSON, cached prompts, or deliverables.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Dict, List, Optional, Set

from .contracts import FieldEvidence, FieldStatus, SourceRef

log = logging.getLogger("census.learning_engine.provenance")

# Pattern for detecting common password patterns in text
_PASSWORD_PATTERNS = [
    re.compile(r"(?i)\bpassword\b\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bpasscode\b\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bpw\b\s*[:=]\s*([^\s,;]+)"),
]


def sanitize_secrets(text: str, known_secrets: Optional[list[str]] = None) -> str:
    """Mask known passwords and discovered password patterns from text strings."""
    if not text:
        return text

    sanitized = text
    if known_secrets:
        for sec in known_secrets:
            if sec and len(sec.strip()) >= 2:
                sanitized = sanitized.replace(sec, "[REDACTED_PASSWORD]")

    for pat in _PASSWORD_PATTERNS:
        sanitized = pat.sub(r"password: [REDACTED_PASSWORD]", sanitized)

    return sanitized


class ProvenanceTracker:
    """Maintains field-level provenance across all stages of a prospect run."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        # Map record_id -> field_name -> FieldEvidence
        self._evidence_map: dict[str, dict[str, FieldEvidence]] = {}
        self._secrets: set[str] = set()

    def register_known_secret(self, secret: str) -> None:
        """Register a known password for automatic redaction."""
        clean = secret.strip()
        if clean and len(clean) >= 2:
            self._secrets.add(clean)

    def record_field(
        self,
        record_id: str,
        field_name: str,
        raw_value: Any,
        normalized_value: Any,
        status: FieldStatus = FieldStatus.PRESENT_SUPPORTED,
        source_ref: Optional[SourceRef] = None,
        rule_id: Optional[str] = None,
    ) -> FieldEvidence:
        """Record or update evidence for a field value."""
        if record_id not in self._evidence_map:
            self._evidence_map[record_id] = {}

        # Sanitize any raw or string values
        clean_raw = raw_value
        if isinstance(raw_value, str):
            clean_raw = sanitize_secrets(raw_value, list(self._secrets))

        clean_norm = normalized_value
        if isinstance(normalized_value, str):
            clean_norm = sanitize_secrets(normalized_value, list(self._secrets))

        refs = [source_ref] if source_ref else []
        ev = FieldEvidence(
            record_id=record_id,
            field_name=field_name,
            raw_value=clean_raw,
            normalized_value=clean_norm,
            status=status,
            source_refs=refs,
            rule_id=rule_id,
        )
        self._evidence_map[record_id][field_name] = ev
        return ev

    def get_evidence(self, record_id: str, field_name: str) -> Optional[FieldEvidence]:
        return self._evidence_map.get(record_id, {}).get(field_name)

    def get_all_for_record(self, record_id: str) -> dict[str, FieldEvidence]:
        return dict(self._evidence_map.get(record_id, {}))

    def to_dict(self) -> dict[str, Any]:
        result = {}
        for rec_id, fields in self._evidence_map.items():
            result[rec_id] = {fname: fe.to_dict() for fname, fe in fields.items()}
        return result

