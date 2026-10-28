"""Evidence-Based Root-Cause Diagnostic Engine.

Traces data deviations across the entire pipeline lifecycle:
EXTRACTION -> CLASSIFICATION -> MAPPING -> NORMALIZATION ->
LINKAGE -> ELIGIBILITY -> SOURCE_SELECTION -> COMPARISON -> WRITER -> SAVED_CELL.

Categorizes issues into confirmed vs suspected root causes and assigns
allowlisted remediation actions.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .contracts import (
    EarliestStage,
    FieldEvidence,
    FieldStatus,
    Issue,
    IssueSeverity,
    IssueType,
    RepairAction,
    SourceRef,
)
from .provenance import ProvenanceTracker
from .source_coverage import SourceCoverageLedger

log = logging.getLogger("census.learning_engine.diagnosis")


class DiagnosticEngine:
    """Diagnoses root cause for missing values or failed checks using source evidence."""

    def __init__(self, coverage_ledger: SourceCoverageLedger, provenance: ProvenanceTracker):
        self.coverage_ledger = coverage_ledger
        self.provenance = provenance

    def diagnose_issue(
        self,
        issue: Issue,
        docs: list[Any],
        census: Optional[Any] = None,
        invoices: Optional[list[Any]] = None,
    ) -> Issue:
        """Enrich an Issue with earliest stage diagnosis, cause status, and allowed actions."""
        # Check if already diagnosed
        if issue.cause_status == "confirmed" and issue.allowed_actions:
            return issue

        # 1. Investigate Access or Missing File Issues
        if issue.issue_type == IssueType.ACCESS_BLOCKED:
            issue.earliest_stage = EarliestStage.EXTRACTION
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.RE_READ_SOURCE_UNIT]
            return issue

        if issue.issue_type == IssueType.SOURCE_NOT_PROVIDED:
            # Check if any uploaded document was misclassified or unparsed
            for doc in docs:
                for table in getattr(doc, "tables", []):
                    # Check for census columns in non-census docs
                    df = getattr(table, "df", None)
                    if df is not None:
                        cols_lower = [str(c).lower() for c in df.columns]
                        if any("dob" in c or "birth" in c for c in cols_lower) and any("name" in c for c in cols_lower):
                            if doc.name not in [d.name for d in getattr(census, "files_used", [])]:
                                issue.earliest_stage = EarliestStage.CLASSIFICATION
                                issue.cause_status = "confirmed"
                                issue.issue_type = IssueType.ROLE_MISCLASSIFIED
                                issue.allowed_actions = [RepairAction.RECLASSIFY_SOURCE]
                                issue.observed_state = f"Document '{doc.name}' contains census table but was not classified as census"
                                return issue
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.RECOMPUTE_FINDINGS]
            return issue

        # 2. Investigate Missing Demographic or Field Values
        if issue.issue_type in (IssueType.SOURCE_VALUE_ABSENT, IssueType.HEADER_MAPPING_FAILED):
            field_name = issue.target_ref.split(".")[-1]
            # Search all raw docs and tables to see if value exists in an unmapped column
            found_evidence = self._search_sources_for_field(field_name, docs)
            if found_evidence:
                source_col, doc_name = found_evidence
                issue.earliest_stage = EarliestStage.MAPPING
                issue.issue_type = IssueType.HEADER_MAPPING_FAILED
                issue.cause_status = "confirmed"
                issue.allowed_actions = [RepairAction.APPLY_HEADER_MAPPING]
                issue.expected_state = f"Mapped from source column '{source_col}' in '{doc_name}'"
                issue.evidence_refs.append(f"{doc_name}::{source_col}")
                return issue
            else:
                # Genuinely absent in all uploaded sources
                issue.earliest_stage = EarliestStage.EXTRACTION
                issue.issue_type = IssueType.SOURCE_VALUE_ABSENT
                issue.cause_status = "confirmed"
                issue.expected_state = "Blank in source"
                issue.allowed_actions = []  # Genuinely absent: no repair, keep blank
                return issue

        # 3. Investigate Linkage Failures
        if issue.issue_type == IssueType.LINKAGE_FAILED:
            issue.earliest_stage = EarliestStage.LINKAGE
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION]
            return issue

        if issue.issue_type == IssueType.SUBSCRIBER_ROW_LINK_STALE:
            issue.earliest_stage = EarliestStage.LINKAGE
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.RECALCULATE_SUBSCRIBER_LINKS]
            return issue

        if issue.issue_type == IssueType.MULTI_COLUMN_GENDER_UNRESOLVED:
            issue.earliest_stage = EarliestStage.NORMALIZATION
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.RESOLVE_MULTI_COLUMN_GENDER]
            return issue

        if issue.issue_type == IssueType.NON_MEDICAL_DEPENDENT_INCLUDED:
            issue.earliest_stage = EarliestStage.ELIGIBILITY
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.FILTER_NON_MEDICAL_DEPENDENTS]
            return issue

        if issue.issue_type == IssueType.PART_TIME_DEPENDENT_INCLUDED:
            issue.earliest_stage = EarliestStage.ELIGIBILITY
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.EXCLUDE_PART_TIME_DEPENDENTS]
            return issue

        # 4. Investigate Business Rule / Status Misapplication
        if issue.issue_type == IssueType.BUSINESS_RULE_MISAPPLIED:
            issue.earliest_stage = EarliestStage.ELIGIBILITY
            issue.cause_status = "confirmed"
            issue.allowed_actions = [RepairAction.REPLACE_STRUCTURED_VALUE, RepairAction.RECOMPUTE_FINDINGS]
            return issue

        # Default fallback
        if not issue.allowed_actions:
            issue.allowed_actions = [RepairAction.RECOMPUTE_FINDINGS]
        return issue

    def _search_sources_for_field(self, field_name: str, docs: list[Any]) -> Optional[Tuple[str, str]]:
        """Search all uploaded document tables for candidate columns matching field_name."""
        synonyms = {
            "dob": ["date of birth", "birth date", "birth_date", "birthdate", "ee dob"],
            "coverage": ["medical coverage level", "tier", "plan tier", "med coverage", "medical tier"],
            "zip": ["postal code", "zip code", "zipcode", "home zip"],
            "gender": ["sex", "gender code", "ee sex"],
        }
        candidates = synonyms.get(field_name.lower(), [field_name.lower()])

        for doc in docs:
            for table in getattr(doc, "tables", []):
                df = getattr(table, "df", None)
                if df is not None:
                    for col in df.columns:
                        c_str = str(col).strip().lower()
                        for cand in candidates:
                            if cand in c_str:
                                return (str(col), doc.name)
        return None

