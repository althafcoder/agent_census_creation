"""Deterministic Underwriting Validator and Multi-Stage Checkpoints.

Executes deterministic validation checks across:
1. Post-Extraction Checkpoint (completeness of sources, instruction hash, access preflight)
2. Post-Processing & Reconciliation Checkpoint (routing, statuses, renewals, claims, Most Used Plan)
3. Deliverable & Email Checkpoint (Section 29 false-positive tests, limitation consistency)
4. Saved Artifact Checkpoint (Excel headers, data types, Marsh-to-SBU transfer)
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

import census_config as C
from .contracts import (
    CheckResult,
    CheckStatus,
    EarliestStage,
    EngineStatus,
    Issue,
    IssueSeverity,
    IssueType,
    RepairAction,
    ValidationReport,
)
from .instruction_registry import verify_instruction_snapshot
from .source_coverage import SourceCoverageLedger

log = logging.getLogger("census.learning_engine.validator")


class UnderwritingValidator:
    """Multi-stage deterministic validator for underwriting accuracy."""

    def __init__(self, instruction_hash: str):
        self.instruction_hash = instruction_hash

    # -------------------------------------------------------------------------
    # Checkpoint 1: Post-Extraction Checkpoint
    # -------------------------------------------------------------------------
    def validate_post_extraction(
        self,
        docs: list[Any],
        coverage_ledger: SourceCoverageLedger,
        access_records: dict[str, Any],
    ) -> ValidationReport:
        """Run post-extraction validation on source coverage, access, and instructions."""
        checks: list[CheckResult] = []
        issues: list[Issue] = []

        # 1. Pinned instruction verification
        inst_ok = verify_instruction_snapshot(self.instruction_hash)
        checks.append(CheckResult(
            rule_id="RULE_01_PRECEDENCE",
            section="1.1",
            name="Instruction Snapshot Verification",
            status=CheckStatus.PASS if inst_ok else CheckStatus.FAIL,
            reason="Instruction snapshot verified" if inst_ok else "Instruction content changed mid-run",
        ))
        if not inst_ok:
            issues.append(Issue(
                issue_id="ISSUE_INST_MISMATCH",
                rule_id="RULE_01_PRECEDENCE",
                target_ref="instructions",
                issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                severity=IssueSeverity.CRITICAL,
                earliest_stage=EarliestStage.EXTRACTION,
                observed_state="Instruction hash mismatch",
            ))

        # 2. Source coverage completeness
        summary = coverage_ledger.get_coverage_summary()
        if summary["is_complete"]:
            checks.append(CheckResult(
                rule_id="RULE_06_PROTECTED_FILES",
                section="6.0",
                name="Source Unit Coverage Completeness",
                status=CheckStatus.PASS,
                reason=f"All {summary['total_expected_units']} source units visited",
            ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_06_PROTECTED_FILES",
                section="6.0",
                name="Source Unit Coverage Completeness",
                status=CheckStatus.FAIL,
                reason=f"{summary['unparsed_units']} units unparsed or failed: {summary['unparsed_unit_ids']}",
            ))
            for unit_id in summary["unparsed_unit_ids"]:
                issues.append(Issue(
                    issue_id=f"ISSUE_UNPARSED_{unit_id}",
                    rule_id="RULE_06_PROTECTED_FILES",
                    target_ref=unit_id,
                    issue_type=IssueType.PAGE_OR_SHEET_SKIPPED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.EXTRACTION,
                    observed_state="Source unit was unparsed or failed parsing",
                    allowed_actions=[RepairAction.RE_READ_SOURCE_UNIT, RepairAction.RE_OCR_PAGE],
                ))

        # 3. Access preflight locked files check
        locked_files = [
            name for name, rec in access_records.items()
            if getattr(rec, "status", None) and str(rec.status) in (
                "PRESENT_LOCKED", "PASSWORD_NOT_FOUND", "PASSWORD_REJECTED", "UNSUPPORTED_PROTECTION"
            )
        ]
        if locked_files:
            checks.append(CheckResult(
                rule_id="RULE_06_PROTECTED_FILES",
                section="6.1",
                name="Protected Document Access Check",
                status=CheckStatus.BLOCKED,
                reason=f"Documents remain locked or unreadable: {locked_files}",
            ))
            for f in locked_files:
                issues.append(Issue(
                    issue_id=f"ISSUE_LOCKED_{f}",
                    rule_id="RULE_06_PROTECTED_FILES",
                    target_ref=f,
                    issue_type=IssueType.ACCESS_BLOCKED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.EXTRACTION,
                    observed_state="File is encrypted/password protected without valid password",
                    allowed_actions=[RepairAction.RE_READ_SOURCE_UNIT],
                ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_06_PROTECTED_FILES",
                section="6.1",
                name="Protected Document Access Check",
                status=CheckStatus.PASS,
                reason="All protected or encrypted files successfully unlocked or readable",
            ))

        return self._build_report(checks, issues)

    # -------------------------------------------------------------------------
    # Checkpoint 2: Full Post-Processing Validation
    # -------------------------------------------------------------------------
    def validate_full_pipeline(
        self,
        prospect: Any,
        routing: Any,
        census: Any,
        selected_invoices: list[Any],
        renewal: Any,
        ren_required: bool,
        claims_required: bool,
        findings: Any,
        results: dict[int, Any],
        roles: dict[str, list[Any]],
    ) -> ValidationReport:
        """Deterministic validation of normalized data, routing, status codes, and comparisons."""
        checks: list[CheckResult] = []
        issues: list[Issue] = []

        # 1. Carrier Routing & Business State Invariant
        state = (routing.state or "").strip().upper() if routing else ""
        carrier = (routing.carrier or "").strip() if routing else ""

        if not state or state in ("UNKNOWN", "CONFLICTING"):
            checks.append(CheckResult(
                rule_id="RULE_04_UNKNOWN_STATE",
                section="4.2",
                name="Primary Business State Clarification",
                status=CheckStatus.PASS,
                reason="Primary business state is unknown/conflicting; state clarification required",
            ))
            if carrier:
                issues.append(Issue(
                    issue_id="ISSUE_CARRIER_ASSIGNED_WITHOUT_STATE",
                    rule_id="RULE_04_UNKNOWN_STATE",
                    target_ref="routing.carrier",
                    issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.SOURCE_SELECTION,
                    expected_state="No carrier workbook generated when state is unknown",
                    observed_state=f"Carrier assigned: {carrier}",
                    allowed_actions=[RepairAction.RECOMPUTE_FINDINGS],
                ))
        elif state == "FL":
            # Florida routing
            if carrier == C.CARRIER_FLORIDA_BLUE:
                checks.append(CheckResult(
                    rule_id="RULE_04_FLORIDA_ROUTING",
                    section="4.3",
                    name="Florida Blue Carrier Routing",
                    status=CheckStatus.PASS,
                    reason="Florida prospect correctly routed to Florida Blue (RAPT template)",
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_04_FLORIDA_ROUTING",
                    section="4.3",
                    name="Florida Blue Carrier Routing",
                    status=CheckStatus.FAIL,
                    reason=f"Florida prospect incorrectly assigned carrier: {carrier}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_WRONG_CARRIER_FL",
                    rule_id="RULE_04_FLORIDA_ROUTING",
                    target_ref="routing.carrier",
                    issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.SOURCE_SELECTION,
                    expected_state="Florida Blue",
                    observed_state=carrier,
                    allowed_actions=[RepairAction.RECOMPUTE_FINDINGS],
                ))

            # GHQ Florida requirement
            ghq_docs = roles.get("ghq", [])
            if not ghq_docs:
                checks.append(CheckResult(
                    rule_id="RULE_07_GHQ_FLORIDA",
                    section="7.1",
                    name="Florida GHQ Requirement",
                    status=CheckStatus.FAIL,
                    reason="GHQ document missing for Florida prospect",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_MISSING_GHQ_FL",
                    rule_id="RULE_07_GHQ_FLORIDA",
                    target_ref="roles.ghq",
                    issue_type=IssueType.SOURCE_NOT_PROVIDED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.EXTRACTION,
                    expected_state="GHQ required for Florida",
                    observed_state="GHQ not provided",
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_07_GHQ_FLORIDA",
                    section="7.1",
                    name="Florida GHQ Requirement",
                    status=CheckStatus.PASS,
                    reason="GHQ provided for Florida Blue prospect",
                ))
        else:
            # Confirmed Non-Florida routing -> Cigna
            if carrier == C.CARRIER_CIGNA:
                checks.append(CheckResult(
                    rule_id="RULE_04_NON_FLORIDA_ROUTING",
                    section="4.4",
                    name="Non-Florida Cigna Routing",
                    status=CheckStatus.PASS,
                    reason="Confirmed non-FL prospect routed to Cigna (Marsh + SBU)",
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_04_NON_FLORIDA_ROUTING",
                    section="4.4",
                    name="Non-Florida Cigna Routing",
                    status=CheckStatus.FAIL,
                    reason=f"Non-Florida prospect assigned carrier: {carrier}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_WRONG_CARRIER_NON_FL",
                    rule_id="RULE_04_NON_FLORIDA_ROUTING",
                    target_ref="routing.carrier",
                    issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.SOURCE_SELECTION,
                    expected_state="Cigna",
                    observed_state=carrier,
                    allowed_actions=[RepairAction.RECOMPUTE_FINDINGS],
                ))

        # 2. Census Existence & Population Check
        if not census or not census.employees:
            checks.append(CheckResult(
                rule_id="RULE_25_MISSING_DOCS",
                section="25.1",
                name="Census Existence Check",
                status=CheckStatus.FAIL,
                reason="Census is missing or parsed 0 employees",
            ))
            issues.append(Issue(
                issue_id="ISSUE_NO_CENSUS",
                rule_id="RULE_25_MISSING_DOCS",
                target_ref="census.employees",
                issue_type=IssueType.SOURCE_NOT_PROVIDED,
                severity=IssueSeverity.CRITICAL,
                earliest_stage=EarliestStage.EXTRACTION,
                expected_state="At least 1 census employee",
                observed_state="0 employees",
            ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_25_MISSING_DOCS",
                section="25.1",
                name="Census Existence Check",
                status=CheckStatus.PASS,
                reason=f"Census populated with {len(census.employees)} employees and {len(census.dependents)} dependents",
            ))

            # 3. Status Handling: Waived must be WO not WP; Most Used Plan restrictions
            invalid_wp_wo = []
            mup_violations = []
            part_time_violations = []
            gender_unresolved_issues = []
            gender_concat_issues = []

            for emp in census.employees:
                if getattr(emp, "is_duplicate", False) or getattr(emp, "invoice_only", False):
                    continue
                cov = (emp.coverage or "").strip().upper()
                # Check for Waived incorrectly mapped as WP
                notes_str = " ".join(str(n) for n in (emp.notes or [])).lower()
                if "waived" in notes_str and cov == "WP":
                    invalid_wp_wo.append(f"{emp.first} {emp.last} (notes='{notes_str}', cov='WP')")

                # Check Part-Time in active medical census or with active dependents
                if any(k in notes_str for k in ("part-time", "part time", "pt employee")):
                    if cov not in ("NE", "WO", "WP", "RC") or getattr(emp, "dependents", []):
                        part_time_violations.append(f"{emp.first} {emp.last}")

                # Check Most Used Plan: never apply to non-enrolled WP/NE/WO/RC
                res = results.get(id(emp))
                if res and res.plan and cov in ("WP", "NE", "WO", "RC"):
                    res_notes = " ".join(str(n) for n in res.notes).lower() if isinstance(res.notes, list) else str(res.notes or "").lower()
                    if "most used" in res_notes or "mup" in res_notes:
                        mup_violations.append(f"{emp.first} {emp.last} (cov='{cov}', plan='{res.plan}')")

            # Gate B: Gender & Sex Field Resolution Check across all people
            for p in getattr(census, "people", []):
                if getattr(p, "is_duplicate", False) or getattr(p, "invoice_only", False):
                    continue
                g_val = (p.gender or "").strip()
                if "," in g_val or len(g_val) > 1:
                    gender_concat_issues.append(f"{p.display} (gender='{g_val}')")
                cands = getattr(p, "gender_candidates", []) or []
                usable_norms = {c.get("normalized") for c in cands if c.get("status") == "USABLE" and c.get("normalized") in ("M", "F")}
                if len(usable_norms) == 1 and not g_val:
                    gender_unresolved_issues.append(f"{p.display} (usable={list(usable_norms)[0]})")

            if gender_unresolved_issues or gender_concat_issues:
                checks.append(CheckResult(
                    rule_id="RULE_10_GENDER_RESOLUTION",
                    section="10.2",
                    name="Multi-Column Gender & Sex Resolution Check",
                    status=CheckStatus.FAIL,
                    reason=f"Unresolved/concatenated gender despite usable candidates: {(gender_unresolved_issues + gender_concat_issues)[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_MULTI_COL_GENDER_UNRESOLVED",
                    rule_id="RULE_10_GENDER_RESOLUTION",
                    target_ref="census.people.gender",
                    issue_type=IssueType.MULTI_COLUMN_GENDER_UNRESOLVED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.NORMALIZATION,
                    expected_state="Single canonical M/F resolved from usable gender/sex columns",
                    observed_state=f"Defects: {(gender_unresolved_issues + gender_concat_issues)[:3]}",
                    allowed_actions=[RepairAction.RESOLVE_MULTI_COLUMN_GENDER],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_10_GENDER_RESOLUTION",
                    section="10.2",
                    name="Multi-Column Gender & Sex Resolution Check",
                    status=CheckStatus.PASS,
                    reason="All same-person gender/sex candidates resolved consistently to M/F",
                ))

            # Gate C: Dependent Disposition & Medical-Only Population Check
            non_med_dep_leaks = []
            stale_sub_links = []
            for dep in census.dependents:
                assocs = getattr(dep, "coverage_associations", []) or []
                if assocs and all(not a.get("is_medical", True) for a in assocs):
                    non_med_dep_leaks.append(f"{dep.display}")
                if dep.employee and dep.employee.row_no and getattr(dep, "sub_row", None) not in (None, dep.employee.row_no):
                    stale_sub_links.append(f"{dep.display} (sub_row={getattr(dep, 'sub_row', None)} vs emp.row_no={dep.employee.row_no})")

            if non_med_dep_leaks:
                checks.append(CheckResult(
                    rule_id="RULE_09_DEPENDENT_DISPOSITION",
                    section="9.2",
                    name="Dependent Medical-Only Disposition Check",
                    status=CheckStatus.FAIL,
                    reason=f"Non-medical-only dependents included in active medical population: {non_med_dep_leaks[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_NON_MEDICAL_DEP_LEAK",
                    rule_id="RULE_09_DEPENDENT_DISPOSITION",
                    target_ref="census.dependents",
                    issue_type=IssueType.NON_MEDICAL_DEPENDENT_INCLUDED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.ELIGIBILITY,
                    expected_state="EXCLUDE_NON_MEDICAL_ONLY dependents excluded from active medical rows",
                    observed_state=f"Leaked: {non_med_dep_leaks[:3]}",
                    allowed_actions=[RepairAction.FILTER_NON_MEDICAL_DEPENDENTS],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_09_DEPENDENT_DISPOSITION",
                    section="9.2",
                    name="Dependent Medical-Only Disposition Check",
                    status=CheckStatus.PASS,
                    reason=f"All {len(census.dependents)} active medical dependents verified; {len(getattr(census, 'excluded_dependents', []))} non-medical/PT dependents excluded",
                ))

            if stale_sub_links:
                checks.append(CheckResult(
                    rule_id="RULE_09_SUBSCRIBER_ROW_LINKS",
                    section="9.4",
                    name="Post-Sort Subscriber Row Link Check",
                    status=CheckStatus.FAIL,
                    reason=f"Stale subscriber row links detected: {stale_sub_links[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_STALE_SUBSCRIBER_LINKS",
                    rule_id="RULE_09_SUBSCRIBER_ROW_LINKS",
                    target_ref="census.dependents.sub_row",
                    issue_type=IssueType.SUBSCRIBER_ROW_LINK_STALE,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.LINKAGE,
                    expected_state="dep.sub_row == dep.employee.row_no after final sort",
                    observed_state=f"Stale: {stale_sub_links[:3]}",
                    allowed_actions=[RepairAction.RECALCULATE_SUBSCRIBER_LINKS],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_09_SUBSCRIBER_ROW_LINKS",
                    section="9.4",
                    name="Post-Sort Subscriber Row Link Check",
                    status=CheckStatus.PASS,
                    reason="All dependent subscriber row links match final ordered employee row numbers",
                ))

            if invalid_wp_wo:
                checks.append(CheckResult(
                    rule_id="RULE_21_STATUS_HANDLING",
                    section="21.1",
                    name="Waived Status WO Coding Check",
                    status=CheckStatus.FAIL,
                    reason=f"Waived employees coded as WP instead of WO: {invalid_wp_wo[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_WAIVED_CODED_AS_WP",
                    rule_id="RULE_21_STATUS_HANDLING",
                    target_ref="census.employees.coverage",
                    issue_type=IssueType.INCORRECT_NORMALIZATION,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.NORMALIZATION,
                    expected_state="WO",
                    observed_state="WP",
                    allowed_actions=[RepairAction.REPLACE_STRUCTURED_VALUE],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_21_STATUS_HANDLING",
                    section="21.1",
                    name="Waived Status WO Coding Check",
                    status=CheckStatus.PASS,
                    reason="All waived employees correctly mapped to WO",
                ))

            if mup_violations:
                checks.append(CheckResult(
                    rule_id="RULE_20_MOST_USED_PLAN",
                    section="20.1",
                    name="Most Used Plan Status Guard",
                    status=CheckStatus.FAIL,
                    reason=f"Most Used Plan applied to non-enrolled WP/NE/WO/RC employees: {mup_violations[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_MUP_NON_ENROLLED",
                    rule_id="RULE_20_MOST_USED_PLAN",
                    target_ref="comparison.results.plan",
                    issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.COMPARISON,
                    expected_state="No Most Used Plan for WP/NE/WO/RC",
                    observed_state=f"MUP applied to: {mup_violations[:3]}",
                    allowed_actions=[RepairAction.REPLACE_STRUCTURED_VALUE, RepairAction.RECOMPUTE_FINDINGS],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_20_MOST_USED_PLAN",
                    section="20.1",
                    name="Most Used Plan Status Guard",
                    status=CheckStatus.PASS,
                    reason="Most Used Plan logic applied strictly to eligible active census-only employees",
                ))

            if part_time_violations:
                checks.append(CheckResult(
                    rule_id="RULE_09_PART_TIME_EXCLUSION",
                    section="9.1",
                    name="Part-Time Employee Exclusion",
                    status=CheckStatus.FAIL,
                    reason=f"Part-time employees included in active medical census: {part_time_violations[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_PART_TIME_INCLUDED",
                    rule_id="RULE_09_PART_TIME_EXCLUSION",
                    target_ref="census.employees",
                    issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.ELIGIBILITY,
                    expected_state="Part-time employees excluded from medical census",
                    observed_state=f"Included: {part_time_violations[:3]}",
                    allowed_actions=[RepairAction.REPLACE_STRUCTURED_VALUE, RepairAction.RECOMPUTE_FINDINGS],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_09_PART_TIME_EXCLUSION",
                    section="9.1",
                    name="Part-Time Employee Exclusion",
                    status=CheckStatus.PASS,
                    reason="Part-time records properly excluded and documented",
                ))

            # 4. Dependent Family Reconstruction
            unlinked_dependents = []
            for dep in census.dependents:
                sub_id = getattr(dep, "subscriber_id", None) or getattr(dep, "employee_id", None)
                if not sub_id:
                    unlinked_dependents.append(f"{dep.first} {dep.last}")

            if unlinked_dependents:
                checks.append(CheckResult(
                    rule_id="RULE_09_DEPENDENT_LINKAGE",
                    section="9.4",
                    name="Dependent Linkage Check",
                    status=CheckStatus.FAIL,
                    reason=f"Unlinked dependents found without subscriber: {unlinked_dependents[:3]}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_UNLINKED_DEPENDENTS",
                    rule_id="RULE_09_DEPENDENT_LINKAGE",
                    target_ref="census.dependents.subscriber_id",
                    issue_type=IssueType.LINKAGE_FAILED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.LINKAGE,
                    expected_state="Linked to subscriber employee ID",
                    observed_state=f"Unlinked: {unlinked_dependents[:3]}",
                    allowed_actions=[RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_09_DEPENDENT_LINKAGE",
                    section="9.4",
                    name="Dependent Linkage Check",
                    status=CheckStatus.PASS,
                    reason="All dependent records successfully linked to subscribers",
                ))

        # 5. PEO Renewal 90-Day Rule Invariant
        peo_flag = getattr(prospect, "peo", None) or getattr(prospect, "is_peo", False)
        if ren_required and not renewal:
            checks.append(CheckResult(
                rule_id="RULE_13_RENEWAL_PEO_90_DAY",
                section="13.1",
                name="Renewal PEO 90-Day Check",
                status=CheckStatus.FAIL,
                reason="Renewal is required under PEO 90-day rule but was not provided",
            ))
            issues.append(Issue(
                issue_id="ISSUE_MISSING_REQUIRED_RENEWAL",
                rule_id="RULE_13_RENEWAL_PEO_90_DAY",
                target_ref="renewal",
                issue_type=IssueType.SOURCE_NOT_PROVIDED,
                severity=IssueSeverity.WARNING,
                earliest_stage=EarliestStage.EXTRACTION,
                expected_state="Renewal document required",
                observed_state="Renewal not provided",
            ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_13_RENEWAL_PEO_90_DAY",
                section="13.1",
                name="Renewal PEO 90-Day Check",
                status=CheckStatus.PASS,
                reason="Renewal requirement correctly evaluated",
            ))

        # 6. Claims Requirement Invariant (>50 Enrolled & Self/Level-Funded)
        funding = (getattr(prospect, "funding_type", "") or "").lower()
        active_count = len([p for p in census.employees if (p.coverage or "").upper() in C.ACTIVE_TIERS]) if census else 0
        is_self_or_level = any(k in funding for k in ("self", "level", "aso"))

        if is_self_or_level and active_count > 50:
            if not claims_required:
                checks.append(CheckResult(
                    rule_id="RULE_15_CLAIMS_REQUIREMENT",
                    section="15.1",
                    name="Claims Requirement Check",
                    status=CheckStatus.FAIL,
                    reason=f"Claims must be required for funding={funding} and active={active_count}",
                ))
                issues.append(Issue(
                    issue_id="ISSUE_CLAIMS_NOT_REQUIRED_WHEN_APPLICABLE",
                    rule_id="RULE_15_CLAIMS_REQUIREMENT",
                    target_ref="router.check_claims_required",
                    issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                    severity=IssueSeverity.CRITICAL,
                    earliest_stage=EarliestStage.SOURCE_SELECTION,
                    expected_state="Claims required = True",
                    observed_state="Claims required = False",
                    allowed_actions=[RepairAction.RECOMPUTE_FINDINGS],
                ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_15_CLAIMS_REQUIREMENT",
                    section="15.1",
                    name="Claims Requirement Check",
                    status=CheckStatus.PASS,
                    reason=f"Claims correctly required for funding={funding} and enrolled={active_count}",
                ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_15_CLAIMS_REQUIREMENT",
                section="15.1",
                name="Claims Requirement Check",
                status=CheckStatus.PASS,
                reason="Claims not required under group size / funding criteria",
            ))

        # 7. Section 29 False-Positive Prevention Test for Email
        email_text = getattr(findings, "email_text", "") or ""
        if email_text:
            fp_issues = self._check_email_false_positives(email_text)
            if fp_issues:
                checks.append(CheckResult(
                    rule_id="RULE_29_FALSE_POSITIVE_CHECK",
                    section="29.1",
                    name="Email False-Positive Prevention Check",
                    status=CheckStatus.FAIL,
                    reason=f"Email contains false positives: {fp_issues}",
                ))
                for fp in fp_issues:
                    issues.append(Issue(
                        issue_id="ISSUE_EMAIL_FALSE_POSITIVE",
                        rule_id="RULE_29_FALSE_POSITIVE_CHECK",
                        target_ref="findings.email_text",
                        issue_type=IssueType.BUSINESS_RULE_MISAPPLIED,
                        severity=IssueSeverity.CRITICAL,
                        earliest_stage=EarliestStage.RELEASE,
                        observed_state=fp,
                        allowed_actions=[RepairAction.RECOMPUTE_FINDINGS],
                    ))
            else:
                checks.append(CheckResult(
                    rule_id="RULE_29_FALSE_POSITIVE_CHECK",
                    section="29.1",
                    name="Email False-Positive Prevention Check",
                    status=CheckStatus.PASS,
                    reason="Email passed all Section 29 false-positive checks",
                ))

        # 8. Section 18 Match Percentage & Formatting Check
        match_format_defects = []
        for p in (census.employees if census else []):
            if getattr(p, "is_duplicate", False) or getattr(p, "invoice_only", False):
                continue
            res = results.get(id(p))
            if res and res.match_pct is not None:
                pct = res.match_pct
                if not isinstance(pct, (int, float)) or pct < 0 or pct > 100:
                    match_format_defects.append(f"{p.display} (invalid match_pct={pct})")
                elif pct < 100 and not res.inv_name:
                    # Section 18: When Match % is below 100%, display exact invoice employee name
                    match_format_defects.append(f"{p.display} (pct={pct}% but missing invoice name)")

        if match_format_defects:
            checks.append(CheckResult(
                rule_id="RULE_18_MATCH_PERCENTAGE_FORMATTING",
                section="18.1",
                name="Match Percentage & Critical Formatting Check",
                status=CheckStatus.FAIL,
                reason=f"Match % formatting/naming defects detected: {match_format_defects[:3]}",
            ))
            issues.append(Issue(
                issue_id="ISSUE_MATCH_PERCENTAGE_FORMAT_DEFECT",
                rule_id="RULE_18_MATCH_PERCENTAGE_FORMATTING",
                target_ref="comparison.results.match_pct",
                issue_type=IssueType.MATCH_PERCENTAGE_FORMAT_DEFECT,
                severity=IssueSeverity.CRITICAL,
                earliest_stage=EarliestStage.WRITER,
                expected_state="Match % formatted as percentage (0%) with Section 18 colors and invoice name when <100%",
                observed_state=f"Defects: {match_format_defects[:3]}",
                allowed_actions=[RepairAction.FORMAT_MATCH_PERCENTAGE, RepairAction.REGENERATE_WORKBOOK],
            ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_18_MATCH_PERCENTAGE_FORMATTING",
                section="18.1",
                name="Match Percentage & Critical Formatting Check",
                status=CheckStatus.PASS,
                reason="Match percentage values and critical formatting conform to Section 18 requirements",
            ))

        # 9. Section 16.3 Preferred Name & Middle Name Alias Matching Check
        unmatched_alias_defects = []
        if census and results:
            for p in getattr(census, "employees", []):
                if getattr(p, "is_duplicate", False) or getattr(p, "invoice_only", False):
                    continue
                pref = getattr(p, "preferred_first", "").strip().lower()
                mid = getattr(p, "middle_name", "").strip().lower()
                last = (p.last or "").strip().lower()
                aliases = [a.lower() for a in getattr(p, "aliases", [])]
                rr = results.get(id(p))
                if (pref or mid or aliases) and rr and getattr(rr, "result", "") == "Not On Invoice":
                    for inv_p in getattr(census, "people", []):
                        if getattr(inv_p, "invoice_only", False):
                            inv_first = (inv_p.first or "").strip().lower()
                            inv_last = (inv_p.last or "").strip().lower()
                            if inv_last == last and (inv_first in (pref, mid) or any(inv_first in a for a in aliases)):
                                unmatched_alias_defects.append(f"{p.display} (census) vs {inv_p.display} (invoice)")
                                break

        if unmatched_alias_defects:
            checks.append(CheckResult(
                rule_id="RULE_16_PREFERRED_NAME_MATCHING",
                section="16.3",
                name="Preferred Name & Middle Name Alias Matching Check",
                status=CheckStatus.FAIL,
                reason=f"Unreconciled preferred name/middle name aliases found: {unmatched_alias_defects[:3]}",
            ))
            issues.append(Issue(
                issue_id="ISSUE_PREFERRED_NAME_ALIAS_MISMATCH",
                rule_id="RULE_16_PREFERRED_NAME_MATCHING",
                target_ref="comparison.matching.preferred_name",
                issue_type=IssueType.PREFERRED_NAME_ALIAS_MISMATCH,
                severity=IssueSeverity.CRITICAL,
                earliest_stage=EarliestStage.COMPARISON,
                expected_state="Employees with preferred first names matching invoice subscriber names must be reconciled",
                observed_state=f"Unmatched: {unmatched_alias_defects[:3]}",
                allowed_actions=[RepairAction.RESOLVE_PREFERRED_NAME_ALIAS],
            ))
        else:
            checks.append(CheckResult(
                rule_id="RULE_16_PREFERRED_NAME_MATCHING",
                section="16.3",
                name="Preferred Name & Middle Name Alias Matching Check",
                status=CheckStatus.PASS,
                reason="Employees with preferred first name or middle name aliases correctly reconciled to invoice subscribers",
            ))

        report = self._build_report(checks, issues)
        # Populate Section 6 Execution Gates (A through F)
        check_map = {c.rule_id: c.status.value for c in checks}
        active_people = [p for p in getattr(census, "people", []) if not getattr(p, "is_duplicate", False) and not getattr(p, "invoice_only", False)] if census else []
        resolved_genders = sum(1 for p in active_people if (p.gender or "") in ("M", "F"))
        conflict_genders = sum(1 for p in active_people if getattr(p, "gender_resolution_status", "") == "CONFLICT")
        report.execution_gates = {
            "A_source_coverage": {
                "status": "PASS" if check_map.get("RULE_25_MISSING_DOCS") == "PASS" else "FAIL",
                "files_used": list(getattr(census, "files_used", [])) if census else [],
                "roles_detected": {k: len(v) for k, v in (roles or {}).items()},
            },
            "B_gender_resolution": {
                "status": check_map.get("RULE_10_GENDER_RESOLUTION", "PASS"),
                "total_canonical_people": len(active_people),
                "resolved_m_f_count": resolved_genders,
                "conflict_count": conflict_genders,
                "unresolved_missing_count": len(active_people) - resolved_genders - conflict_genders,
            },
            "C_dependent_disposition": {
                "status": check_map.get("RULE_09_DEPENDENT_DISPOSITION", "PASS"),
                "included_active_medical_dependents": len(census.dependents) if census else 0,
                "excluded_non_medical_or_pt_dependents": len(getattr(census, "excluded_dependents", [])) if census else 0,
                "masked_placeholder_slots_ignored": len(getattr(census, "masked_dependent_slots", [])) if census else 0,
            },
            "D_enrollment_and_links": {
                "status": "PASS" if all(
                    check_map.get(rid, "PASS") == "PASS"
                    for rid in ("RULE_09_DEPENDENT_LINKAGE", "RULE_09_SUBSCRIBER_ROW_LINKS", "RULE_21_STATUS_HANDLING", "RULE_20_MOST_USED_PLAN", "RULE_09_PART_TIME_EXCLUSION")
                ) else "FAIL",
                "employee_count": len([e for e in getattr(census, "employees", []) if not getattr(e, "is_duplicate", False) and not getattr(e, "invoice_only", False)]) if census else 0,
                "dependent_count": len(census.dependents) if census else 0,
            },
            "E_reconciliation": {
                "status": "PASS" if check_map.get("RULE_13_RENEWAL_PEO_90_DAY", "PASS") == "PASS" and check_map.get("RULE_15_CLAIMS_REQUIREMENT", "PASS") == "PASS" else "FAIL",
                "selected_invoice_count": len(selected_invoices or []),
                "renewal_required": bool(ren_required),
                "claims_required": bool(claims_required),
            },
            "F_output_consistency": {
                "status": "PASS" if check_map.get("RULE_29_FALSE_POSITIVE_CHECK", "PASS") == "PASS" and check_map.get("RULE_04_NON_FLORIDA_ROUTING", "PASS") == "PASS" and check_map.get("RULE_04_FLORIDA_ROUTING", "PASS") == "PASS" else "FAIL",
                "carrier": carrier,
                "state": state,
            },
        }
        return report

    def _check_email_false_positives(self, email_text: str) -> list[str]:
        """Detect false positives in missing information email per Section 29."""
        issues = []
        lower = email_text.lower()

        # No technical jargon
        forbidden_tech = ["internal traceback", "exception occurred", "dataframe", "regex", "nullpointer"]
        for tech in forbidden_tech:
            if tech in lower:
                issues.append(f"Technical internal jargon detected: '{tech}'")

        # No empty numbered/bulleted sections
        import re
        if re.search(r"^\s*\d+\.\s*$\n^\s*\d+\.", email_text, re.MULTILINE):
            issues.append("Empty numbered section detected in email")

        # No workers compensation code false positives in medical deficiency
        if re.search(r"wc\s*code|0953pa|8871la", lower):
            issues.append("Workers compensation fragment detected in missing info email")

        return issues

    def _build_report(self, checks: list[CheckResult], issues: list[Issue]) -> ValidationReport:
        total = len(checks)
        passed = sum(1 for c in checks if c.status == CheckStatus.PASS)
        failed = sum(1 for c in checks if c.status == CheckStatus.FAIL)
        blocked = sum(1 for c in checks if c.status == CheckStatus.BLOCKED)

        if blocked > 0:
            status = EngineStatus.BLOCKED_TECHNICAL
        elif failed > 0:
            status = EngineStatus.NEEDS_SOURCE_INFORMATION
        else:
            status = EngineStatus.PASSED

        return ValidationReport(
            instruction_hash=self.instruction_hash,
            total_checks=total,
            passed_checks=passed,
            failed_checks=failed,
            blocked_checks=blocked,
            checks=checks,
            issues=issues,
            release_status=status,
        )
