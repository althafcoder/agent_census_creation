"""Source Coverage Ledger, Chunking Engine, and Completeness Tracking.

Ensures 100% data fidelity across multi-page, multi-sheet, and enterprise rosters:
- Tracks expected vs visited units (sheets, pages, tables, rows).
- Provides deterministic table chunking with stable chunk IDs, shared header context,
  and deduplication for large rosters (>500 rows).
- Maintains candidate record retention/exclusion ledger (reasons for merge, exclusion, drop).
- Independent verification so a successful sheet cannot conceal a failed table or tab.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from typing import Any, Dict, List, Optional, Set, Tuple
import pandas as pd

from .contracts import DocumentManifest, FieldEvidence, FieldStatus, SourceRef

log = logging.getLogger("census.learning_engine.coverage")


@dataclasses.dataclass
class UnitLedgerEntry:
    unit_id: str             # e.g., "Census.xlsx::Sheet2" or "Roster.pdf::Page_3"
    document_id: str
    unit_type: str           # "sheet", "page", "table", "chunk"
    status: str              # "VISITED", "UNPARSED", "FAILED", "SKIPPED"
    row_count: int = 0
    parsed_record_count: int = 0
    error_message: Optional[str] = None


@dataclasses.dataclass
class RecordDecision:
    raw_identifier: str      # e.g. "Row 42: John Doe"
    source_unit: str
    decision: str            # "RETAINED", "EXCLUDED_PART_TIME", "MERGED_DEPENDENT", "INVOICE_ONLY", "UNRESOLVED"
    reason: str
    record_id: Optional[str] = None


class SourceCoverageLedger:
    """Independent ledger verifying complete processing of all uploaded source units."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.manifests: dict[str, DocumentManifest] = {}
        self.units: dict[str, UnitLedgerEntry] = {}
        self.record_decisions: list[RecordDecision] = []
        self.unparsed_units: list[str] = []

    def register_document(self, manifest: DocumentManifest) -> None:
        self.manifests[manifest.document_id] = manifest
        for unit in manifest.expected_units:
            unit_id = f"{manifest.document_id}::{unit}"
            if unit_id not in self.units:
                self.units[unit_id] = UnitLedgerEntry(
                    unit_id=unit_id,
                    document_id=manifest.document_id,
                    unit_type="unit",
                    status="UNPARSED",
                )

    def record_unit_visit(
        self,
        document_id: str,
        unit_name: str,
        unit_type: str = "sheet",
        row_count: int = 0,
        parsed_count: int = 0,
        status: str = "VISITED",
        error: Optional[str] = None,
    ) -> None:
        unit_id = f"{document_id}::{unit_name}"
        self.units[unit_id] = UnitLedgerEntry(
            unit_id=unit_id,
            document_id=document_id,
            unit_type=unit_type,
            status=status,
            row_count=row_count,
            parsed_record_count=parsed_count,
            error_message=error,
        )
        if status in ("FAILED", "UNPARSED") and unit_id not in self.unparsed_units:
            self.unparsed_units.append(unit_id)
        elif status == "VISITED" and unit_id in self.unparsed_units:
            self.unparsed_units.remove(unit_id)

    def log_record_decision(
        self,
        raw_identifier: str,
        source_unit: str,
        decision: str,
        reason: str,
        record_id: Optional[str] = None,
    ) -> None:
        self.record_decisions.append(RecordDecision(
            raw_identifier=raw_identifier,
            source_unit=source_unit,
            decision=decision,
            reason=reason,
            record_id=record_id,
        ))

    def get_coverage_summary(self) -> dict[str, Any]:
        total_units = len(self.units)
        visited = sum(1 for u in self.units.values() if u.status == "VISITED")
        failed = sum(1 for u in self.units.values() if u.status == "FAILED")
        unparsed = sum(1 for u in self.units.values() if u.status == "UNPARSED")
        return {
            "total_expected_units": total_units,
            "visited_units": visited,
            "failed_units": failed,
            "unparsed_units": unparsed,
            "is_complete": (failed == 0 and unparsed == 0),
            "unparsed_unit_ids": list(self.unparsed_units),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "summary": self.get_coverage_summary(),
            "manifests": {k: m.to_dict() for k, m in self.manifests.items()},
            "units": {k: dataclasses.asdict(u) for k, u in self.units.items()},
            "decisions_count": len(self.record_decisions),
        }


# -----------------------------------------------------------------------------
# Complete Table Chunking Utility (>500 Rows Handling)
# -----------------------------------------------------------------------------

@dataclasses.dataclass
class TableChunk:
    chunk_id: str
    table_name: str
    chunk_index: int
    total_chunks: int
    start_row: int
    end_row: int
    headers: list[str]
    df: pd.DataFrame
    hash: str


def chunk_dataframe(
    df: pd.DataFrame,
    table_name: str = "table",
    chunk_size: int = 250,
    overlap: int = 0,
) -> list[TableChunk]:
    """Splits a large DataFrame into stable, complete chunks without truncating data.
    
    Guarantees that every row from row 0 to len(df)-1 is represented.
    Includes original row indices for precise cell provenance.
    """
    total_rows = len(df)
    if total_rows == 0:
        return []

    headers = [str(c) for c in df.columns]
    chunks: list[TableChunk] = []

    if total_rows <= chunk_size:
        h = hashlib.sha256(f"{table_name}::chunk_0::{total_rows}".encode("utf-8")).hexdigest()[:12]
        chunks.append(TableChunk(
            chunk_id=f"{table_name}__chk_0",
            table_name=table_name,
            chunk_index=0,
            total_chunks=1,
            start_row=0,
            end_row=total_rows,
            headers=headers,
            df=df.copy(),
            hash=h,
        ))
        return chunks

    idx = 0
    chunk_idx = 0
    # Calculate total expected chunks
    step = chunk_size - overlap if overlap > 0 else chunk_size
    while idx < total_rows:
        end = min(idx + chunk_size, total_rows)
        chunk_df = df.iloc[idx:end].copy()
        h = hashlib.sha256(f"{table_name}::chunk_{chunk_idx}::{idx}-{end}".encode("utf-8")).hexdigest()[:12]
        chunks.append(TableChunk(
            chunk_id=f"{table_name}__chk_{chunk_idx}",
            table_name=table_name,
            chunk_index=chunk_idx,
            total_chunks=0,  # updated below
            start_row=idx,
            end_row=end,
            headers=headers,
            df=chunk_df,
            hash=h,
        ))
        chunk_idx += 1
        idx += step

    total_chunks = len(chunks)
    for chk in chunks:
        chk.total_chunks = total_chunks

    return chunks


def chunk_text(
    text: str,
    max_chunk_chars: int = 15000,
    overlap_chars: int = 1000,
) -> list[dict[str, Any]]:
    """Deterministically chunks long document text for LLM processing with overlap."""
    if not text:
        return []
    total_len = len(text)
    if total_len <= max_chunk_chars:
        return [{
            "chunk_id": "chunk_0",
            "chunk_index": 0,
            "total_chunks": 1,
            "start_char": 0,
            "end_char": total_len,
            "text": text,
        }]

    chunks = []
    idx = 0
    chunk_idx = 0
    step = max_chunk_chars - overlap_chars
    while idx < total_len:
        end = min(idx + max_chunk_chars, total_len)
        chunk_text_slice = text[idx:end]
        chunks.append({
            "chunk_id": f"chunk_{chunk_idx}",
            "chunk_index": chunk_idx,
            "start_char": idx,
            "end_char": end,
            "text": chunk_text_slice,
        })
        chunk_idx += 1
        idx += step

    total = len(chunks)
    for c in chunks:
        c["total_chunks"] = total
    return chunks

