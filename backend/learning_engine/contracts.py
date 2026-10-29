"""Typed domain contracts and serialization models for the Learning Engine.

Defines schemas and dataclasses for:
- RunContext
- DocumentManifest
- SourceRef
- FieldEvidence (with FieldStatus enum)
- Issue (with IssueType, IssueSeverity enum)
- ValidationReport (with CheckResult, CheckStatus enum)
- RepairPatch (with RepairAction enum)
- LearningRule (with RuleStatus, PromotionMode enum)
- JSON serialization utility handling Decimal, dates, Path, sets, and cycle prevention.
"""
from __future__ import annotations

import copy
import dataclasses
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------

class FieldStatus(str, Enum):
    PRESENT_SUPPORTED = "PRESENT_SUPPORTED"
    MISSING_IN_SOURCE = "MISSING_IN_SOURCE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNREADABLE_SOURCE = "UNREADABLE_SOURCE"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    CONFLICTING = "CONFLICTING"
    DERIVED_BY_APPROVED_RULE = "DERIVED_BY_APPROVED_RULE"


class IssueType(str, Enum):
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    SOURCE_NOT_PROVIDED = "SOURCE_NOT_PROVIDED"
    SOURCE_VALUE_ABSENT = "SOURCE_VALUE_ABSENT"
    PAGE_OR_SHEET_SKIPPED = "PAGE_OR_SHEET_SKIPPED"
    TEXT_TRUNCATED = "TEXT_TRUNCATED"
    ROLE_MISCLASSIFIED = "ROLE_MISCLASSIFIED"
    HEADER_MAPPING_FAILED = "HEADER_MAPPING_FAILED"
    DEPENDENT_COLUMN_MISSED = "DEPENDENT_COLUMN_MISSED"
    LINKAGE_FAILED = "LINKAGE_FAILED"
    INCORRECT_NORMALIZATION = "INCORRECT_NORMALIZATION"
    WRONG_SOURCE_VERSION = "WRONG_SOURCE_VERSION"
    APPLICABILITY_UNKNOWN = "APPLICABILITY_UNKNOWN"
    BUSINESS_RULE_MISAPPLIED = "BUSINESS_RULE_MISAPPLIED"
    CACHE_REPLAY = "CACHE_REPLAY"
    OUTPUT_SCHEMA_FAILED = "OUTPUT_SCHEMA_FAILED"
    WRITER_CELL_MAPPING_FAILED = "WRITER_CELL_MAPPING_FAILED"
    SBU_TRANSFER_FAILED = "SBU_TRANSFER_FAILED"
    MULTI_COLUMN_GENDER_UNRESOLVED = "MULTI_COLUMN_GENDER_UNRESOLVED"
    GENDER_CONFLICT = "GENDER_CONFLICT"
    NON_MEDICAL_DEPENDENT_INCLUDED = "NON_MEDICAL_DEPENDENT_INCLUDED"
    SUBSCRIBER_ROW_LINK_STALE = "SUBSCRIBER_ROW_LINK_STALE"
    PART_TIME_DEPENDENT_INCLUDED = "PART_TIME_DEPENDENT_INCLUDED"
    MATCH_PERCENTAGE_FORMAT_DEFECT = "MATCH_PERCENTAGE_FORMAT_DEFECT"
    PREFERRED_NAME_ALIAS_MISMATCH = "PREFERRED_NAME_ALIAS_MISMATCH"
    CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENT_SUBSCRIBER_REPETITION = "CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENT_SUBSCRIBER_REPETITION"
    EXAMPLE_ROW_INCLUDED = "EXAMPLE_ROW_INCLUDED"
    HSA_MEDICAL_PLAN_DROPPED = "HSA_MEDICAL_PLAN_DROPPED"
    INVOICE_PRESENCE_FALSE_POSITIVE = "INVOICE_PRESENCE_FALSE_POSITIVE"
    SUMMARY_PLAN_TABLE_INCOMPLETE = "SUMMARY_PLAN_TABLE_INCOMPLETE"
    INVOICE_EXTRACTION_COLLAPSE = "INVOICE_EXTRACTION_COLLAPSE"
    PART_TIME_EMPLOYEE_INCLUDED = "PART_TIME_EMPLOYEE_INCLUDED"
    UNDETERMINED = "UNDETERMINED"


class IssueSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class EarliestStage(str, Enum):
    EXTRACTION = "EXTRACTION"
    CLASSIFICATION = "CLASSIFICATION"
    MAPPING = "MAPPING"
    NORMALIZATION = "NORMALIZATION"
    LINKAGE = "LINKAGE"
    ELIGIBILITY = "ELIGIBILITY"
    SOURCE_SELECTION = "SOURCE_SELECTION"
    COMPARISON = "COMPARISON"
    WRITER = "WRITER"
    RELEASE = "RELEASE"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


class RepairAction(str, Enum):
    RE_READ_SOURCE_UNIT = "RE_READ_SOURCE_UNIT"
    RE_OCR_PAGE = "RE_OCR_PAGE"
    RECLASSIFY_SOURCE = "RECLASSIFY_SOURCE"
    APPLY_HEADER_MAPPING = "APPLY_HEADER_MAPPING"
    REPAIR_LINKAGE_OR_NORMALIZATION = "REPAIR_LINKAGE_OR_NORMALIZATION"
    SELECT_SOURCE_VERSION = "SELECT_SOURCE_VERSION"
    REPLACE_STRUCTURED_VALUE = "REPLACE_STRUCTURED_VALUE"
    RECOMPUTE_FINDINGS = "RECOMPUTE_FINDINGS"
    REGENERATE_WORKBOOK = "REGENERATE_WORKBOOK"
    BYPASS_EVICT_CACHE = "BYPASS_EVICT_CACHE"
    RESOLVE_MULTI_COLUMN_GENDER = "RESOLVE_MULTI_COLUMN_GENDER"
    FILTER_NON_MEDICAL_DEPENDENTS = "FILTER_NON_MEDICAL_DEPENDENTS"
    RECALCULATE_SUBSCRIBER_LINKS = "RECALCULATE_SUBSCRIBER_LINKS"
    EXCLUDE_PART_TIME_DEPENDENTS = "EXCLUDE_PART_TIME_DEPENDENTS"
    FORMAT_MATCH_PERCENTAGE = "FORMAT_MATCH_PERCENTAGE"
    RESOLVE_PREFERRED_NAME_ALIAS = "RESOLVE_PREFERRED_NAME_ALIAS"
    RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS = "RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS"
    FILTER_TEMPLATE_EXAMPLE_ROWS = "FILTER_TEMPLATE_EXAMPLE_ROWS"
    REPAIR_HSA_MEDICAL_PLAN_CLASSIFICATION = "REPAIR_HSA_MEDICAL_PLAN_CLASSIFICATION"
    RECONCILE_INVOICE_PRESENCE = "RECONCILE_INVOICE_PRESENCE"
    PARSE_MERITAIN_STATEMENT = "PARSE_MERITAIN_STATEMENT"
    EXCLUDE_PART_TIME_POPULATION = "EXCLUDE_PART_TIME_POPULATION"


class EngineStatus(str, Enum):
    PASSED = "PASSED"
    REPAIRED_AND_PASSED = "REPAIRED_AND_PASSED"
    VALID_WITH_DOCUMENTED_LIMITATIONS = "VALID_WITH_DOCUMENTED_LIMITATIONS"
    NEEDS_SOURCE_INFORMATION = "NEEDS_SOURCE_INFORMATION"
    BLOCKED_TECHNICAL = "BLOCKED_TECHNICAL"


class RuleStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    VALIDATED = "VALIDATED"
    REJECTED = "rejected"
    RETIRED = "retired"


class PromotionMode(str, Enum):
    NOT_PROMOTED = "not_promoted"
    AUTO_ALLOWLISTED = "auto_allowlisted"
    MANUAL_APPROVED = "manual_approved"


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclasses.dataclass
class SourceRef:
    """Precise source reference pointing to original evidence."""
    document_id: str
    original_filename: str
    location: str  # e.g., "Sheet1!Row:14:Col:B" or "Page 2 lines 10-15"
    content_hash: str = ""
    extraction_method: str = "structured_reader"
    evidence_snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class FieldEvidence:
    """Provenance and evidence record for a single field or prospect fact."""
    record_id: str  # e.g. "emp_12", "dep_4", or "prospect"
    field_name: str
    raw_value: Any
    normalized_value: Any
    status: FieldStatus = FieldStatus.PRESENT_SUPPORTED
    source_refs: list[SourceRef] = dataclasses.field(default_factory=list)
    rule_id: Optional[str] = None
    conflicts: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "field_name": self.field_name,
            "raw_value": serialize_safe(self.raw_value),
            "normalized_value": serialize_safe(self.normalized_value),
            "status": self.status.value,
            "source_refs": [s.to_dict() for s in self.source_refs],
            "rule_id": self.rule_id,
            "conflicts": list(self.conflicts),
        }


@dataclasses.dataclass
class DocumentManifest:
    """Metadata and processing state for an uploaded source document."""
    document_id: str
    filename: str
    parent_chain: list[str] = dataclasses.field(default_factory=list)
    content_hash: str = ""
    roles: list[str] = dataclasses.field(default_factory=list)
    version_or_period: str = ""
    access_status: str = "READABLE"
    expected_units: list[str] = dataclasses.field(default_factory=list)
    visited_units: list[str] = dataclasses.field(default_factory=list)
    extraction_status: str = "COMPLETED"
    errors: list[str] = dataclasses.field(default_factory=list)
    affected_capabilities: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Issue:
    """Defect, instruction violation, or data-loss issue."""
    issue_id: str
    rule_id: str
    target_ref: str
    issue_type: IssueType
    severity: IssueSeverity
    earliest_stage: EarliestStage
    expected_state: str = ""
    observed_state: str = ""
    evidence_refs: list[str] = dataclasses.field(default_factory=list)
    cause_status: str = "confirmed"  # confirmed, suspected, undetermined
    allowed_actions: list[RepairAction] = dataclasses.field(default_factory=list)
    resolution_status: str = "open"  # open, repaired, unresolved, not_applicable

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "rule_id": self.rule_id,
            "target_ref": self.target_ref,
            "issue_type": self.issue_type.value,
            "severity": self.severity.value,
            "earliest_stage": self.earliest_stage.value,
            "expected_state": self.expected_state,
            "observed_state": self.observed_state,
            "evidence_refs": list(self.evidence_refs),
            "cause_status": self.cause_status,
            "allowed_actions": [a.value for a in self.allowed_actions],
            "resolution_status": self.resolution_status,
        }


@dataclasses.dataclass
class CheckResult:
    """Individual rule verification check result."""
    rule_id: str
    section: str
    name: str
    status: CheckStatus
    reason: str = ""
    evidence_refs: list[str] = dataclasses.field(default_factory=list)
    issues: list[Issue] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "section": self.section,
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclasses.dataclass
class ValidationReport:
    """Complete validation report for a submission run."""
    instruction_hash: str
    total_checks: int = 0
    passed_checks: int = 0
    failed_checks: int = 0
    blocked_checks: int = 0
    accuracy: float = 1.0
    accuracy_threshold: float = 0.90
    checks: list[CheckResult] = dataclasses.field(default_factory=list)
    issues: list[Issue] = dataclasses.field(default_factory=list)
    release_status: EngineStatus = EngineStatus.PASSED
    supported_release_decisions: list[str] = dataclasses.field(default_factory=list)
    known_limitations: list[str] = dataclasses.field(default_factory=list)
    execution_gates: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction_hash": self.instruction_hash,
            "total_checks": self.total_checks,
            "passed_checks": self.passed_checks,
            "failed_checks": self.failed_checks,
            "blocked_checks": self.blocked_checks,
            "accuracy": self.accuracy,
            "accuracy_threshold": self.accuracy_threshold,
            "checks": [c.to_dict() for c in self.checks],
            "issues": [i.to_dict() for i in self.issues],
            "release_status": self.release_status.value,
            "supported_release_decisions": list(self.supported_release_decisions),
            "known_limitations": list(self.known_limitations),
            "execution_gates": copy.deepcopy(self.execution_gates),
        }


@dataclasses.dataclass
class RepairPatch:
    """Proposal or record of a typed repair operation."""
    patch_id: str
    issue_ids: list[str]
    action: RepairAction
    scope_predicates: dict[str, Any] = dataclasses.field(default_factory=dict)
    evidence_refs: list[str] = dataclasses.field(default_factory=list)
    preconditions: list[str] = dataclasses.field(default_factory=list)
    expected_old_state: Any = None
    proposed_new_state: Any = None
    parameters: dict[str, Any] = dataclasses.field(default_factory=dict)
    affected_stages: list[EarliestStage] = dataclasses.field(default_factory=list)
    rollback_data: Optional[dict[str, Any]] = None
    verification_checks: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "issue_ids": list(self.issue_ids),
            "action": self.action.value,
            "scope_predicates": serialize_safe(self.scope_predicates),
            "evidence_refs": list(self.evidence_refs),
            "preconditions": list(self.preconditions),
            "expected_old_state": serialize_safe(self.expected_old_state),
            "proposed_new_state": serialize_safe(self.proposed_new_state),
            "parameters": serialize_safe(self.parameters),
            "affected_stages": [s.value for s in self.affected_stages],
            "rollback_data": serialize_safe(self.rollback_data),
            "verification_checks": list(self.verification_checks),
        }


@dataclasses.dataclass
class LearningRule:
    """Procedural learning rule definition conforming to section 11 & repair section 8 schema."""
    schema_version: str = "1.1"
    learning_id: str = ""
    version: int = 1
    status: RuleStatus = RuleStatus.CANDIDATE
    pattern_state: str = "CANDIDATE"  # CANDIDATE | VALIDATED | REJECTED
    fingerprint: str = ""
    patch_version: str = "2.2.0"
    generic_root_cause: str = ""
    applicable_conditions: list[str] = dataclasses.field(default_factory=list)
    required_validators: list[str] = dataclasses.field(default_factory=list)
    regression_results: dict[str, Any] = dataclasses.field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    compatibility: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "instruction_hashes": [],
        "template_hashes": [],
        "parser_versions": [],
        "runtime_prompt_version": "1.0",
    })
    scope: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "document_roles": ["census"],
        "file_types": ["xlsx", "csv"],
        "carriers": ["Florida Blue", "Cigna"],
        "header_signature": [],
        "stage": "header_mapping",
    })
    trigger: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "issue_type": "HEADER_MAPPING_FAILED",
        "field": "",
        "conditions": [],
    })
    business_rule_refs: list[str] = dataclasses.field(default_factory=list)
    root_cause: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "code": "HEADER_MAPPING_FAILED",
        "verification_status": "unverified",
        "summary": "",
    })
    strategy: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "action": "APPLY_HEADER_MAPPING",
        "parameters": {},
        "preconditions": [],
        "exclusions": [],
    })
    verification: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "case_successes": 0,
        "case_failures": 0,
        "regression_fixture_ids": [],
        "negative_fixture_ids": [],
        "checks_passed": [],
        "promotion_eligible": False,
        "last_verified_at": None,
    })
    promotion: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "mode": "not_promoted",
        "decision_ref": None,
        "reason": "Initial candidate",
    })
    usage: dict[str, Any] = dataclasses.field(default_factory=lambda: {
        "attempted": 0,
        "successful": 0,
        "rolled_back": 0,
    })

    def to_dict(self) -> dict[str, Any]:
        st_val = self.status.value if isinstance(self.status, RuleStatus) else str(self.status)
        p_state = self.pattern_state
        if not p_state or p_state == "CANDIDATE":
            if st_val in ("active", "VALIDATED"):
                p_state = "VALIDATED"
            elif st_val in ("rejected", "REJECTED"):
                p_state = "REJECTED"
            else:
                p_state = "CANDIDATE"
        return {
            "schema_version": self.schema_version,
            "learning_id": self.learning_id,
            "version": self.version,
            "status": st_val,
            "pattern_state": p_state,
            "fingerprint": self.fingerprint or self.learning_id,
            "patch_version": self.patch_version,
            "generic_root_cause": self.generic_root_cause or self.root_cause.get("summary", ""),
            "applicable_conditions": list(self.applicable_conditions or self.trigger.get("conditions", [])),
            "required_validators": list(self.required_validators or self.verification.get("checks_passed", [])),
            "regression_results": copy.deepcopy(self.regression_results),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "compatibility": copy.deepcopy(self.compatibility),
            "scope": copy.deepcopy(self.scope),
            "trigger": copy.deepcopy(self.trigger),
            "business_rule_refs": list(self.business_rule_refs),
            "root_cause": copy.deepcopy(self.root_cause),
            "strategy": copy.deepcopy(self.strategy),
            "verification": copy.deepcopy(self.verification),
            "promotion": copy.deepcopy(self.promotion),
            "usage": copy.deepcopy(self.usage),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LearningRule:
        d = dict(data)
        if "status" in d and isinstance(d["status"], str):
            raw_st = d["status"]
            if raw_st.upper() == "CANDIDATE":
                d["status"] = RuleStatus.CANDIDATE
            elif raw_st.upper() == "REJECTED":
                d["status"] = RuleStatus.REJECTED
            elif raw_st.upper() == "RETIRED":
                d["status"] = RuleStatus.RETIRED
            elif raw_st in ("active", "VALIDATED", "ACTIVE"):
                d["status"] = RuleStatus.ACTIVE
            else:
                d["status"] = RuleStatus(raw_st)
        valid_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


@dataclasses.dataclass
class RunContext:
    """Isolated, immutable execution context for a single submission run."""
    case_id: str
    run_id: str
    tenant_id: str = "default"
    instruction_hash: str = ""
    app_version: str = "2.1.0"
    schema_version: str = "2026.1"
    parser_version: str = "2.1.0"
    template_hash: str = ""
    model_name: str = "claude-opus-5-5"
    review_date: str = ""  # ISO YYYY-MM-DD
    timezone: str = "UTC"
    bypass_cache: bool = False
    frozen_active_rules: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    attempt_budget: int = 3
    current_attempt: int = 0
    staging_dir: Optional[Path] = None
    output_dir: Optional[Path] = None
    extracted_text_dir: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "instruction_hash": self.instruction_hash,
            "app_version": self.app_version,
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "template_hash": self.template_hash,
            "model_name": self.model_name,
            "review_date": self.review_date,
            "timezone": self.timezone,
            "bypass_cache": self.bypass_cache,
            "frozen_active_rule_count": len(self.frozen_active_rules),
            "attempt_budget": self.attempt_budget,
            "current_attempt": self.current_attempt,
            "staging_dir": str(self.staging_dir) if self.staging_dir else None,
            "output_dir": str(self.output_dir) if self.output_dir else None,
        }


# -----------------------------------------------------------------------------
# Safe Serialization Helpers
# -----------------------------------------------------------------------------

def serialize_safe(obj: Any, seen: Optional[set[int]] = None) -> Any:
    """Recursively convert arbitrary objects to JSON-serializable primitives.
    
    Prevents cycles, handles Decimal, dates, Path, sets, dataclasses, and custom objects.
    """
    if obj is None:
        return None
    if isinstance(obj, (int, float, str, bool)):
        return obj
    if isinstance(obj, Decimal):
        # Money/numbers serialize cleanly as string to avoid float imprecision
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, set):
        return [serialize_safe(item, seen) for item in sorted(list(obj), key=str)]

    # Cycle detection
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return f"<CycleRef:{type(obj).__name__}:{obj_id}>"
    seen.add(obj_id)

    try:
        if dataclasses.is_dataclass(obj):
            res = {}
            for f in dataclasses.fields(obj):
                res[f.name] = serialize_safe(getattr(obj, f.name), seen)
            return res
        if hasattr(obj, "to_dict") and callable(obj.to_dict):
            return serialize_safe(obj.to_dict(), seen)
        if isinstance(obj, dict):
            return {str(k): serialize_safe(v, seen) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [serialize_safe(item, seen) for item in obj]
        if hasattr(obj, "__dict__"):
            # Generic class instance (avoiding private or cyclic props)
            res = {}
            for k, v in obj.__dict__.items():
                if not k.startswith("_"):
                    res[k] = serialize_safe(v, seen)
            return res
        return str(obj)
    finally:
        seen.remove(obj_id)

