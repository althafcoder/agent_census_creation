"""Diagnostic Record Tracing System across all pipeline stages.

Provides stage-by-stage observability for any person or record:
1. Source extraction
2. Normalized identity and relationship
3. Subscriber linkage
4. Eligibility and coverage assignment
5. Invoice matching
6. Final workbook values

Enables programmatic inspection and verification of diagnostic fixtures
(e.g. Mackenzie Storch, Sean Storch, Jeremy Aschman) or any arbitrary record.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger("census.tracer")


@dataclass
class TraceEvent:
    stage: str
    timestamp: datetime = field(default_factory=datetime.now)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordTrace:
    record_id: str
    name: str
    events: list[TraceEvent] = field(default_factory=list)

    def add_event(self, stage: str, data: dict[str, Any]) -> None:
        self.events.append(TraceEvent(stage=stage, data=dict(data)))

    def get_stage_data(self, stage: str) -> Optional[dict[str, Any]]:
        for ev in reversed(self.events):
            if ev.stage == stage:
                return ev.data
        return None

    def find_first_deviation(self, field_name: str, expected_val: Any) -> Optional[tuple[str, Any]]:
        """Identify the first stage where `field_name` deviates from `expected_val`."""
        for ev in self.events:
            if field_name in ev.data:
                actual = ev.data[field_name]
                if actual != expected_val:
                    return (ev.stage, actual)
        return None

    def format_summary(self) -> str:
        lines = [f"=== TRACE FOR RECORD: {self.name} (ID: {self.record_id}) ==="]
        for ev in self.events:
            lines.append(f"  [{ev.stage}]")
            for k, v in ev.data.items():
                lines.append(f"    - {k}: {v}")
        return "\n".join(lines)


class DiagnosticTracer:
    """Thread-safe diagnostic tracer for submission records."""

    STAGE_SOURCE = "1_SOURCE_EXTRACTION"
    STAGE_NORMALIZATION = "2_NORMALIZATION"
    STAGE_LINKAGE = "3_SUBSCRIBER_LINKAGE"
    STAGE_ELIGIBILITY = "4_ELIGIBILITY_ASSIGNMENT"
    STAGE_INVOICE_MATCH = "5_INVOICE_MATCHING"
    STAGE_WORKBOOK = "6_FINAL_WORKBOOK_VALUES"

    def __init__(self) -> None:
        self._traces: dict[str, RecordTrace] = {}       # record_id -> RecordTrace
        self._name_to_id: dict[str, str] = {}           # normalized_name -> record_id

    def reset(self) -> None:
        self._traces.clear()
        self._name_to_id.clear()

    def _normalize_name(self, name: str) -> str:
        return " ".join(name.strip().lower().split())

    def _get_or_create(self, record_id: str, name: str) -> RecordTrace:
        if record_id not in self._traces:
            trace = RecordTrace(record_id=record_id, name=name)
            self._traces[record_id] = trace
            norm = self._normalize_name(name)
            if norm:
                self._name_to_id[norm] = record_id
        return self._traces[record_id]

    def record_source(self, record_id: str, name: str, source_file: str,
                      source_table: str, source_row: int, raw_values: dict[str, Any]) -> None:
        t = self._get_or_create(record_id, name)
        t.add_event(self.STAGE_SOURCE, {
            "source_file": source_file,
            "source_table": source_table,
            "source_row": source_row,
            "raw_values": raw_values,
        })
        log.debug("Trace [%s] %s -> source: file=%s, row=%d", record_id, name, source_file, source_row)

    def record_normalization(self, record_id: str, name: str, role: str, rel_raw: str,
                             gender: str, dob: Any, zip_code: str, plan: str, coverage: str) -> None:
        t = self._get_or_create(record_id, name)
        t.add_event(self.STAGE_NORMALIZATION, {
            "role": role,
            "rel_raw": rel_raw,
            "gender": gender,
            "dob": str(dob) if dob else "",
            "zip": zip_code,
            "plan": plan,
            "coverage": coverage,
        })
        log.debug("Trace [%s] %s -> norm: role=%s, rel_raw=%s, cov=%s", record_id, name, role, rel_raw, coverage)

    def record_linkage(self, record_id: str, name: str, role: str,
                       subscriber_id: str, subscriber_name: str, employee_found: bool) -> None:
        t = self._get_or_create(record_id, name)
        t.add_event(self.STAGE_LINKAGE, {
            "role": role,
            "subscriber_id": subscriber_id,
            "subscriber_name": subscriber_name,
            "employee_found": employee_found,
        })
        log.debug("Trace [%s] %s -> linkage: role=%s, sub_id=%s, sub_name=%s",
                  record_id, name, role, subscriber_id, subscriber_name)

    def record_eligibility(self, record_id: str, name: str, coverage: str,
                           eligibility_status: str, waiver: str, rule_applied: str, notes: list[str]) -> None:
        t = self._get_or_create(record_id, name)
        t.add_event(self.STAGE_ELIGIBILITY, {
            "coverage": coverage,
            "eligibility_status": eligibility_status,
            "waiver": waiver,
            "rule_applied": rule_applied,
            "notes": list(notes),
        })
        log.debug("Trace [%s] %s -> eligibility: cov=%s, status=%s, rule=%s",
                  record_id, name, coverage, eligibility_status, rule_applied)

    def record_invoice_match(self, record_id: str, name: str, matched_name: str,
                             similarity: float, plan: str, tier: str, premium: Any,
                             comparison_result: str, invoice_only: bool) -> None:
        t = self._get_or_create(record_id, name)
        t.add_event(self.STAGE_INVOICE_MATCH, {
            "matched_invoice_name": matched_name,
            "similarity": similarity,
            "plan": plan,
            "tier": tier,
            "premium": premium,
            "comparison_result": comparison_result,
            "invoice_only": invoice_only,
        })
        log.debug("Trace [%s] %s -> invoice_match: inv_name=%s, result=%s", record_id, name, matched_name, comparison_result)

    def record_workbook_output(
        self,
        record_id: str,
        name: str,
        excel_row: int = 0,
        data_row: int | None = None,
        first: str = "",
        last: str = "",
        gender: str = "",
        dob: str = "",
        zip_code: str = "",
        role: str = "",
        subscriber_row: int | None = None,
        coverage: str = "",
        cobra: str = "",
        plan: str = "",
        premium: float | None = None,
        inv_name: str = "",
        match_pct: float | None = None,
        inv_plan: str = "",
        inv_tier: str = "",
        inv_premium: float | None = None,
        renewal_premium: float | None = None,
        comparison_result: str = "",
        *args,
        sheet_name: str = "Census",
        col_values: dict[str, Any] | None = None,
        notes: list[str] | None = None,
        **kwargs,
    ) -> None:
        t = self._get_or_create(record_id, name)
        event_data = {
            "excel_row": excel_row,
            "data_row": data_row,
            "name": f"{first} {last}".strip() or name,
            "role": role,
            "subscriber_row": subscriber_row,
            "coverage": coverage,
            "plan": plan,
            "premium": premium,
            "inv_name": inv_name,
            "match_pct": match_pct,
            "inv_plan": inv_plan,
            "inv_tier": inv_tier,
            "inv_premium": inv_premium,
            "renewal_premium": renewal_premium,
            "comparison_result": comparison_result,
        }
        if col_values:
            event_data.update(col_values)
        if notes:
            event_data["notes"] = notes
        t.add_event(self.STAGE_WORKBOOK, event_data)
        log.debug("Trace [%s] %s -> workbook: row_no=%s, role=%s, result=%s", record_id, name, data_row or excel_row, role, comparison_result)

    def get_trace(self, name_or_id: str) -> Optional[RecordTrace]:
        if name_or_id in self._traces:
            return self._traces[name_or_id]
        norm = self._normalize_name(name_or_id)
        rec_id = self._name_to_id.get(norm)
        if rec_id:
            return self._traces.get(rec_id)
        # Search by substring or partial name
        for norm_k, r_id in self._name_to_id.items():
            if norm in norm_k or norm_k in norm:
                return self._traces.get(r_id)
        return None

    def all_traces(self) -> list[RecordTrace]:
        return list(self._traces.values())

    def get_diagnostic_report(self) -> str:
        lines = ["================================================================================",
                 "                       DIAGNOSTIC PIPELINE RECORD TRACES                        ",
                 "================================================================================"]
        for trace in self._traces.values():
            lines.append(trace.format_summary())
            lines.append("-" * 80)
        return "\n".join(lines)

    def format_summary(self) -> str:
        """Alias for get_diagnostic_report."""
        return self.get_diagnostic_report()


# Global singleton
_TRACER: DiagnosticTracer = DiagnosticTracer()


def get_tracer() -> DiagnosticTracer:
    return _TRACER
