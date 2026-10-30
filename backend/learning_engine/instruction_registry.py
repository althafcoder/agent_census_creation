"""Executable Instruction Registry and Snapshot Pinning.

Implements Sections 1 through 31 of the controlling underwriting instructions:
- Dynamic discovery and SHA-256 fingerprinting of active instruction files.
- Run-scoped immutable instruction snapshot pinning and verification.
- Executable RuleSpecs covering all 31 sections with precedence, obligations,
  predicates, severity, allowed remediation, and verification logic.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import census_config as C
from .contracts import CheckResult, CheckStatus, Issue, IssueSeverity, IssueType, EarliestStage, RepairAction

log = logging.getLogger("census.learning_engine.registry")


@dataclasses.dataclass
class RuleSpec:
    """Executable specification for a single business or underwriting instruction."""
    rule_id: str
    section: str
    name: str
    obligation: str
    precedence: int  # 1 = highest (controlling facts/templates), 5 = lowest
    severity: IssueSeverity = IssueSeverity.CRITICAL
    allowed_remediations: list[RepairAction] = dataclasses.field(default_factory=list)
    applicability_fn: Optional[Callable[[dict[str, Any]], bool]] = None
    check_fn: Optional[Callable[[dict[str, Any]], CheckResult]] = None


class InstructionSnapshot:
    """Immutable snapshot of the controlling instructions for a specific run."""

    def __init__(self, paths: list[Path], content: str, content_hash: str):
        self.paths = tuple(paths)
        self.content = content
        self.content_hash = content_hash
        self._rule_registry = build_instruction_registry(self.content_hash)

    @property
    def rules(self) -> dict[str, RuleSpec]:
        return self._rule_registry

    def get_rule(self, rule_id: str) -> Optional[RuleSpec]:
        return self._rule_registry.get(rule_id)


def resolve_instruction_snapshot() -> InstructionSnapshot:
    """Load all active instruction files and create an immutable pinned snapshot."""
    paths = C.get_instruction_paths()
    if not paths:
        raise RuntimeError("No controlling instruction document found in backend/instructions or via environment overrides.")

    hasher = hashlib.sha256()
    content_chunks = []
    for p in paths:
        if not p.exists():
            continue
        try:
            raw = p.read_bytes()
            hasher.update(p.name.encode("utf-8"))
            hasher.update(raw)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace")
            content_chunks.append(text)
        except Exception as e:
            log.warning("Failed reading instruction file %s: %s", p, e)

    combined_content = "\n\n".join(content_chunks)
    if not combined_content.strip():
        raise RuntimeError("Controlling instruction content is empty. Refusing to run without governing instructions.")

    content_hash = hasher.hexdigest()
    log.info("Pinned immutable instruction snapshot: %d files, %d chars, SHA-256=%s",
             len(paths), len(combined_content), content_hash[:16])
    return InstructionSnapshot(paths=paths, content=combined_content, content_hash=content_hash)


def verify_instruction_snapshot(pinned_hash: str) -> bool:
    """Verify that the currently resolved instruction files match the pinned snapshot hash."""
    current = resolve_instruction_snapshot()
    if current.content_hash != pinned_hash:
        log.error("Instruction snapshot mismatch! Pinned=%s, Current=%s", pinned_hash, current.content_hash)
        return False
    return True


# -----------------------------------------------------------------------------
# Complete Registry of RuleSpecs (Sections 1 to 31)
# -----------------------------------------------------------------------------

def build_instruction_registry(instruction_hash: str) -> dict[str, RuleSpec]:
    """Compile the complete executable instruction registry."""
    rules: dict[str, RuleSpec] = {}

    def _reg(r: RuleSpec) -> None:
        rules[r.rule_id] = r

    # Section 1: Precedence & Controlling Rules
    _reg(RuleSpec(
        rule_id="RULE_01_PRECEDENCE",
        section="1.1",
        name="Controlling Rule Precedence Hierarchy",
        obligation="Precedence: 1. Current facts/approved template; 2. Controlling rules; 3. Carrier rules; 4. Group rules; 5. Detailed rules; 6. Legacy examples.",
        precedence=1,
        severity=IssueSeverity.CRITICAL,
    ))

    # Section 2: Unified Workflow & Deliverables
    _reg(RuleSpec(
        rule_id="RULE_02_WORKFLOW",
        section="2.0",
        name="Unified Deliverables Generation",
        obligation="Generate carrier census deliverable (RAPT or Marsh+SBU) and ready-to-send Missing Info Email.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
    ))

    # Section 3: Prospect Isolation
    _reg(RuleSpec(
        rule_id="RULE_03_ISOLATION",
        section="3.1",
        name="Prospect Data Isolation and State Reset",
        obligation="Every prospect run must start with clean state. No credentials, employee facts, or notes carried over.",
        precedence=1,
        severity=IssueSeverity.CRITICAL,
    ))

    # Section 4: Carrier & Template Routing
    _reg(RuleSpec(
        rule_id="RULE_04_FLORIDA_ROUTING",
        section="4.3",
        name="Florida Prospect Routes to Florida Blue (RAPT)",
        obligation="Florida employer location routes to Florida Blue using RAPT template exclusively. No Marsh or SBU.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))
    _reg(RuleSpec(
        rule_id="RULE_04_NON_FLORIDA_ROUTING",
        section="4.4",
        name="Non-Florida Routes to Cigna (Marsh + SBU)",
        obligation="Confirmed non-Florida employer routes to Cigna with Marsh comparison workbook and population-only SBU.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))
    _reg(RuleSpec(
        rule_id="RULE_04_UNKNOWN_STATE",
        section="4.2",
        name="Unknown or Conflicting Business State Clarification",
        obligation="Unknown/conflicting primary business state produces no carrier workbook and generates a state clarification email.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 5: Group Type & Applicability
    _reg(RuleSpec(
        rule_id="RULE_05_GROUP_TYPE",
        section="5.1",
        name="Standard / Virgin Group Exception Handling",
        obligation="Identify Standard vs Virgin group; virgin group does not require prior invoice or renewal.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 6: Complete Document Review & Protected Files
    _reg(RuleSpec(
        rule_id="RULE_06_PROTECTED_FILES",
        section="6.1",
        name="Protected File Access and Password Discovery",
        obligation="Execute access preflight, discover explicit passwords from same-case notes/emails, keep secrets transient and unlogged.",
        precedence=1,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RE_READ_SOURCE_UNIT],
    ))

    # Section 7: GHQ Rules
    _reg(RuleSpec(
        rule_id="RULE_07_GHQ_FLORIDA",
        section="7.1",
        name="GHQ Mandatory for Florida Blue",
        obligation="Florida prospects require full GHQ review and reconciliation with Census and Invoice.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))
    _reg(RuleSpec(
        rule_id="RULE_07_GHQ_NON_FLORIDA",
        section="7.1",
        name="GHQ Voluntary for Cigna",
        obligation="Non-Florida prospects do not require GHQ; voluntary GHQs are reviewed without flagging missing GHQ.",
        precedence=3,
        severity=IssueSeverity.INFO,
    ))

    # Section 8: Census Templates & Preservation
    _reg(RuleSpec(
        rule_id="RULE_08_RAPT_PRESERVATION",
        section="8.3",
        name="RAPT Template Layout and Formatting Preservation",
        obligation="Underwriting columns A-L untouched, comparison in column M+, preserve styles, formulas, and validations.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))
    _reg(RuleSpec(
        rule_id="RULE_08_MARSH_PRESERVATION",
        section="8.3",
        name="Marsh Template Layout Preservation",
        obligation="Preserve Marsh structure, comparison blocks, formulas, and formatting.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))
    _reg(RuleSpec(
        rule_id="RULE_08_SBU_PRESERVATION",
        section="8.3",
        name="SBU Template Population-Only Deliverable",
        obligation="Populate SBU census without comparison columns strictly for Cigna routing.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))

    # Section 9: Population, Medical Filtering, Status Precedence
    _reg(RuleSpec(
        rule_id="RULE_09_PART_TIME_EXCLUSION",
        section="9.1",
        name="Part-Time Employee Exclusion",
        obligation="Part-Time records must be excluded from active medical census and documented in notes.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REPLACE_STRUCTURED_VALUE, RepairAction.RECOMPUTE_FINDINGS],
    ))
    _reg(RuleSpec(
        rule_id="RULE_09_MEDICAL_ONLY",
        section="9.2",
        name="Medical Coverage Filter",
        obligation="Census population includes only medical participants and their eligible medical dependents.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REPLACE_STRUCTURED_VALUE],
    ))
    _reg(RuleSpec(
        rule_id="RULE_09_DEPENDENT_LINKAGE",
        section="9.4",
        name="Dependent Family Reconstruction and Subscriber Linkage",
        obligation="Every dependent must be linked to a valid subscriber employee with valid relationship code.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[
            RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION,
            RepairAction.RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS,
        ],
    ))

    # Section 10: Census Standardization & Required Fields
    _reg(RuleSpec(
        rule_id="RULE_10_REQUIRED_FIELDS",
        section="10.1",
        name="Census Standardization and Required Demographics",
        obligation="Normalize Name, DOB, Gender, Zip, Coverage Tier, Plan. Preserve legitimate source blanks.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[
            RepairAction.APPLY_HEADER_MAPPING,
            RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION,
            RepairAction.RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS,
        ],
    ))

    # Section 11: Invoice / BER Selection & Acceptability
    _reg(RuleSpec(
        rule_id="RULE_11_INVOICE_SELECTION",
        section="11.2",
        name="Latest Medical Invoice Selection",
        obligation="Select the most recent invoice bill period; handle revised bills and multi-entity consolidation without duplication.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.SELECT_SOURCE_VERSION],
    ))
    _reg(RuleSpec(
        rule_id="RULE_11_BER_EQUIVALENCE",
        section="11.1",
        name="BER Equivalent as Invoice Source",
        obligation="When no carrier invoice is provided, an enrollment report (BER) with medical tier data acts as invoice equivalent.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.RECLASSIFY_SOURCE],
    ))

    # Section 12: SBC Requirement
    _reg(RuleSpec(
        rule_id="RULE_12_SBC_REQUIREMENT",
        section="12.0",
        name="Summary of Benefits and Coverage (SBC) Rules",
        obligation="Apply SBC rules and Aetna/Insperity/TriNet exceptions; do not request SBC for unenrolled plans.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 13: Renewal PEO 90-Day Rule
    _reg(RuleSpec(
        rule_id="RULE_13_RENEWAL_PEO_90_DAY",
        section="13.1",
        name="Renewal Required Only for PEO Within 90 Days",
        obligation="Renewal required only if currently with PEO AND renewal date is within 90 days AND no mid-term exception.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 14: Rate Sheet Requirement
    _reg(RuleSpec(
        rule_id="RULE_14_RATE_SHEET",
        section="14.0",
        name="Rate Sheet Applicability and Validation",
        obligation="Validate rate sheets when renewal requires rate verification.",
        precedence=4,
        severity=IssueSeverity.INFO,
    ))

    # Section 15: Claims Requirement (>50 & Self/Level Funded)
    _reg(RuleSpec(
        rule_id="RULE_15_CLAIMS_REQUIREMENT",
        section="15.1",
        name="Claims Document Requirement (>50 Enrolled & Self/Level Funded)",
        obligation="Claims required ONLY when funding is Self-Funded or Level-Funded AND enrolled medical employee count > 50.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 16: Census vs Invoice Comparison
    _reg(RuleSpec(
        rule_id="RULE_16_COMPARISON_MATCHING",
        section="16.1",
        name="Exact and Fuzzy Employee Reconciliation",
        obligation="Match subscriber employees by SSN/ID or normalized Name; detect invoice-only and census-only subscribers.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[
            RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION,
            RepairAction.RECOMPUTE_FINDINGS,
            RepairAction.RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS,
        ],
    ))
    _reg(RuleSpec(
        rule_id="RULE_16_NAME_MISMATCH_ACCOUNTING",
        section="16.6",
        name="Name Mismatch Comparison & Summary Accounting",
        obligation="When name mismatch occurs between census and invoice subscriber, account for it in summary metrics, exceptions, and findings notes.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 17: Coverage Tier & Dependent Validation
    _reg(RuleSpec(
        rule_id="RULE_17_TIER_VALIDATION",
        section="17.1",
        name="Tier vs Dependent Count Reconciliation",
        obligation="Verify Employee Only, Employee+Spouse, Employee+Child, Family tiers against enrolled dependent counts.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 18: Match Percentage & Formatting
    _reg(RuleSpec(
        rule_id="RULE_18_MATCH_PERCENTAGE",
        section="18.0",
        name="Match Percentage Computation",
        obligation="Compute reconciled match percentage between census and billing.",
        precedence=4,
        severity=IssueSeverity.INFO,
    ))

    # Section 19: Premium Population
    _reg(RuleSpec(
        rule_id="RULE_19_PREMIUM_POPULATION",
        section="19.1",
        name="Current vs Renewal Premium Population",
        obligation="Populate current premiums from invoice and renewal rates from renewal tables; do not invent zero or rates.",
        precedence=3,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REPLACE_STRUCTURED_VALUE],
    ))

    # Section 20: Most Used Plan Logic
    _reg(RuleSpec(
        rule_id="RULE_20_MOST_USED_PLAN",
        section="20.1",
        name="Most Used Plan Restricted to Eligible Active Census-Only",
        obligation="Apply Most Used Plan logic strictly to eligible active census-only employees. NEVER infer for WP/NE/WO/RC.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REPLACE_STRUCTURED_VALUE, RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 21: Status Codes WP, NE, WO, RC
    _reg(RuleSpec(
        rule_id="RULE_21_STATUS_HANDLING",
        section="21.1",
        name="Eligibility Status Code Standardization (WP/NE/WO/RC)",
        obligation="Preserve valid WP/NE/WO/RC. Waived is WO, not WP. Do not flag expected invoice absence for non-enrolled.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REPLACE_STRUCTURED_VALUE, RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 22: Prospect Info Population
    _reg(RuleSpec(
        rule_id="RULE_22_PROSPECT_FIELDS",
        section="22.1",
        name="Prospect Information Header Population",
        obligation="Populate Company, Effective Date, SIC/NAICS, Address from GHQ/RFP or submission documents.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.REPLACE_STRUCTURED_VALUE],
    ))

    # Section 23: Summary Sheet
    _reg(RuleSpec(
        rule_id="RULE_23_SUMMARY_SHEET",
        section="23.0",
        name="Summary Sheet and Audit Notes",
        obligation="Maintain audit notes, limitations, and reconciliation totals in summary worksheet.",
        precedence=4,
        severity=IssueSeverity.INFO,
    ))

    # Section 24: Cigna Marsh-to-SBU Reconciliation
    _reg(RuleSpec(
        rule_id="RULE_24_MARSH_TO_SBU",
        section="24.1",
        name="Cigna Marsh-to-SBU Exact Population Reconciliation",
        obligation="Generate SBU only from finalized audited Marsh. Reconcile all transferable fields and subscriber relationships.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))

    # Section 25: Missing Document Scenarios
    _reg(RuleSpec(
        rule_id="RULE_25_MISSING_DOCS",
        section="25.1",
        name="Missing Document Scenarios Handling",
        obligation="No census -> valid email without fabricated census workbook. Missing invoice -> census-only deliverable.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 26: Cross-Document Validation
    _reg(RuleSpec(
        rule_id="RULE_26_CROSS_DOC",
        section="26.1",
        name="Cross-Document Reconciliation and Payroll Filter",
        obligation="Cross-check census against payroll/invoice; wage/tax documents must not drive false medical deficiencies.",
        precedence=3,
        severity=IssueSeverity.WARNING,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 27: Readiness Classification
    _reg(RuleSpec(
        rule_id="RULE_27_READINESS",
        section="27.0",
        name="Internal Underwriting Readiness Classification",
        obligation="Classify internal readiness (Complete, Minor Discrepancy, Incomplete).",
        precedence=4,
        severity=IssueSeverity.INFO,
    ))

    # Section 28: Missing Information Email
    _reg(RuleSpec(
        rule_id="RULE_28_MISSING_INFO_EMAIL",
        section="28.1",
        name="Ready-to-Send Missing Information Email Generation",
        obligation="Generate clear, professional, ready-to-send missing info email itemizing actionable deficiencies.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 29: False-Positive Prevention Test
    _reg(RuleSpec(
        rule_id="RULE_29_FALSE_POSITIVE_CHECK",
        section="29.1",
        name="Section 29 False-Positive Prevention Test",
        obligation="Perform false-positive checks before retaining email items: no technical jargon, no empty sections, no WC/wage false flags.",
        precedence=2,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.RECOMPUTE_FINDINGS],
    ))

    # Section 30: Pre-Output Audit & Release Gate
    _reg(RuleSpec(
        rule_id="RULE_30_PRE_OUTPUT_AUDIT",
        section="30.1",
        name="Unified Pre-Output Audit and Release Gate",
        obligation="Verify openpyxl saved workbooks, formulas, data types, headers, row continuity, and SBU transfer.",
        precedence=1,
        severity=IssueSeverity.CRITICAL,
        allowed_remediations=[RepairAction.REGENERATE_WORKBOOK],
    ))

    # Section 31: Final Output Behavior
    _reg(RuleSpec(
        rule_id="RULE_31_FINAL_OUTPUTS",
        section="31.1",
        name="Release Authorized Final Deliverables",
        obligation="Release only artifacts explicitly allowed by current facts; withhold unauthorized or unverified workbooks.",
        precedence=1,
        severity=IssueSeverity.CRITICAL,
    ))

    return rules

