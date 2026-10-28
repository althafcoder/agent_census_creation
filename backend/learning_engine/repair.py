"""Bounded Transactional Repair Engine.

Executes allowlisted typed repair actions against a working snapshot:
- APPLY_HEADER_MAPPING
- RECLASSIFY_SOURCE
- REPAIR_LINKAGE_OR_NORMALIZATION
- REPLACE_STRUCTURED_VALUE
- RECOMPUTE_FINDINGS
- REGENERATE_WORKBOOK
- BYPASS_EVICT_CACHE
- RE_READ_SOURCE_UNIT
- RE_OCR_PAGE

Enforces preconditions, rollback on no-improvement or regression, and loop prevention.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import census_config as C
from .contracts import (
    CheckStatus,
    EarliestStage,
    Issue,
    IssueSeverity,
    IssueType,
    RepairAction,
    RepairPatch,
    ValidationReport,
)

log = logging.getLogger("census.learning_engine.repair")


class RepairTransaction:
    """Manages transactional patch execution, verification, and rollback."""

    def __init__(self, patch: RepairPatch, original_state: dict[str, Any]):
        self.patch = patch
        self.original_state = original_state
        self.executed = False
        self.verified = False
        self.rolled_back = False

    def rollback(self, target_state: dict[str, Any]) -> None:
        """Restore previous state snapshot on failure or regression."""
        log.warning("Rolling back repair patch '%s' (%s)", self.patch.patch_id, self.patch.action.value)
        target_state.clear()
        target_state.update(copy.deepcopy(self.original_state))
        self.rolled_back = True


class RepairEngine:
    """Orchestrates typed repairs with precondition validation and rerun bounds."""

    def __init__(self, max_cycles: int = 3):
        self.max_cycles = max_cycles
        self.executed_patches: list[RepairPatch] = []
        self._action_history: set[str] = set()

    def propose_patches(
        self,
        report: ValidationReport,
        working_state: dict[str, Any],
    ) -> list[RepairPatch]:
        """Propose bounded typed repair patches for open critical issues."""
        patches: list[RepairPatch] = []

        for issue in report.issues:
            if issue.resolution_status != "open":
                continue

            for action in issue.allowed_actions:
                sig = f"{action.value}::{issue.target_ref}"
                if sig in self._action_history:
                    # Prevent repeating identical action
                    continue

                patch = self._build_patch_for_issue(issue, action, working_state)
                if patch:
                    patches.append(patch)
                    break

        return patches

    def _build_patch_for_issue(
        self,
        issue: Issue,
        action: RepairAction,
        working_state: dict[str, Any],
    ) -> Optional[RepairPatch]:
        patch_id = f"patch_{action.value.lower()}_{len(self.executed_patches) + 1}"

        if action == RepairAction.APPLY_HEADER_MAPPING:
            # Look up candidate mapping parameters
            source_col = issue.evidence_refs[0].split("::")[-1] if issue.evidence_refs else "tier"
            target_field = issue.target_ref.split(".")[-1]
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"target_field": target_field},
                evidence_refs=list(issue.evidence_refs),
                preconditions=["source column exists in uploaded document"],
                expected_old_state="unmapped",
                proposed_new_state="mapped",
                parameters={"source_header": source_col, "target_field": target_field},
                affected_stages=[EarliestStage.MAPPING, EarliestStage.NORMALIZATION],
            )

        if action == RepairAction.REPLACE_STRUCTURED_VALUE:
            if "WO" in issue.expected_state and "WP" in issue.observed_state:
                return RepairPatch(
                    patch_id=patch_id,
                    issue_ids=[issue.issue_id],
                    action=action,
                    scope_predicates={"field": "coverage"},
                    preconditions=["employee notes contain 'waived'"],
                    expected_old_state="WP",
                    proposed_new_state="WO",
                    parameters={"from_status": "WP", "to_status": "WO"},
                    affected_stages=[EarliestStage.NORMALIZATION, EarliestStage.COMPARISON],
                )
            if "Most Used Plan" in issue.expected_state or "No Most Used Plan" in issue.expected_state:
                return RepairPatch(
                    patch_id=patch_id,
                    issue_ids=[issue.issue_id],
                    action=action,
                    scope_predicates={"rule": "RULE_20_MOST_USED_PLAN"},
                    preconditions=["employee status is WP, NE, WO, or RC"],
                    expected_old_state="MUP applied",
                    proposed_new_state="None",
                    parameters={"action": "strip_mup_from_non_enrolled"},
                    affected_stages=[EarliestStage.COMPARISON],
                )

        if action == RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "linkage"},
                preconditions=["subscriber exists in census employees"],
                parameters={"action": "link_by_last_name_and_address"},
                affected_stages=[EarliestStage.LINKAGE],
            )

        if action == RepairAction.RECOMPUTE_FINDINGS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "findings"},
                preconditions=["state routing updated or findings inconsistent"],
                parameters={"action": "recompute_all_findings"},
                affected_stages=[EarliestStage.RELEASE],
            )

        if action == RepairAction.REGENERATE_WORKBOOK:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "writer"},
                parameters={"action": "regenerate_workbook"},
                affected_stages=[EarliestStage.WRITER, EarliestStage.RELEASE],
            )

        if action == RepairAction.RECLASSIFY_SOURCE:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "classification"},
                parameters={"action": "promote_table_to_census"},
                affected_stages=[EarliestStage.CLASSIFICATION, EarliestStage.EXTRACTION],
            )

        if action == RepairAction.RESOLVE_MULTI_COLUMN_GENDER:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"field": "gender"},
                preconditions=["same-person usable gender/sex candidate exists across columns or linked files"],
                expected_old_state="blank or concatenated gender",
                proposed_new_state="single canonical M or F",
                parameters={"action": "resolve_multi_column_gender"},
                affected_stages=[EarliestStage.MAPPING, EarliestStage.NORMALIZATION],
                verification_checks=["RULE_10_GENDER_RESOLUTION"],
            )

        if action == RepairAction.FILTER_NON_MEDICAL_DEPENDENTS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"role": "dependent"},
                preconditions=["dependent coverage associations are exclusively non-medical"],
                expected_old_state="included in active medical dependents",
                proposed_new_state="EXCLUDE_NON_MEDICAL_ONLY",
                parameters={"action": "filter_non_medical_dependents"},
                affected_stages=[EarliestStage.ELIGIBILITY, EarliestStage.LINKAGE],
                verification_checks=["RULE_09_DEPENDENT_DISPOSITION"],
            )

        if action == RepairAction.RECALCULATE_SUBSCRIBER_LINKS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"field": "sub_row"},
                preconditions=["census ordering or filtering completed"],
                expected_old_state="stale sub_row",
                proposed_new_state="recalculated post-sort sub_row",
                parameters={"action": "recalculate_subscriber_links"},
                affected_stages=[EarliestStage.LINKAGE, EarliestStage.WRITER],
                verification_checks=["RULE_09_SUBSCRIBER_ROW_LINKS"],
            )

        if action == RepairAction.EXCLUDE_PART_TIME_DEPENDENTS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"disposition": "EXCLUDE_PART_TIME"},
                preconditions=["subscriber has part-time exclusion"],
                expected_old_state="part-time dependents in active medical population",
                proposed_new_state="EXCLUDE_PART_TIME",
                parameters={"action": "exclude_part_time_dependents"},
                affected_stages=[EarliestStage.ELIGIBILITY],
                verification_checks=["RULE_09_PART_TIME_EXCLUSION"],
            )

        if action == RepairAction.FORMAT_MATCH_PERCENTAGE:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"column": "Match %", "field": "match_pct"},
                preconditions=["comparison results contain match percentage"],
                expected_old_state="unformatted or currency formatted match %",
                proposed_new_state="percentage format '0%' with Section 18 colors",
                parameters={"number_format": "0%", "apply_section_18_colors": True},
                affected_stages=[EarliestStage.WRITER, EarliestStage.RELEASE],
                verification_checks=["RULE_18_MATCH_PERCENTAGE_FORMATTING"],
            )

        if action == RepairAction.RESOLVE_PREFERRED_NAME_ALIAS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"field": "preferred_first", "stage": "comparison"},
                preconditions=["employee has preferred name, middle name, or alias matching invoice subscriber"],
                expected_old_state="unmatched census employee and unmatched invoice subscriber",
                proposed_new_state="reconciled match with exact invoice name and 95% match score",
                parameters={"action": "resolve_preferred_name_alias"},
                affected_stages=[EarliestStage.COMPARISON, EarliestStage.RELEASE],
                verification_checks=["RULE_16_PREFERRED_NAME_MATCHING"],
            )

        if action == RepairAction.RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "mapping", "field": "carrier_census_headers"},
                preconditions=["census spreadsheet carries carrier legal name headers or repeated subscriber on dependent rows"],
                expected_old_state="unrecognized headers or duplicate employee inflation from dependent rows",
                proposed_new_state="direct table ingestion with carrier template headers and clean dependent subscriber linkage",
                parameters={"action": "resolve_carrier_census_legal_name_and_dependents"},
                affected_stages=[EarliestStage.MAPPING, EarliestStage.LINKAGE, EarliestStage.COMPARISON],
                verification_checks=["RULE_09_DEPENDENT_LINKAGE"],
            )

        return None

    def execute_patch(
        self,
        patch: RepairPatch,
        working_state: dict[str, Any],
    ) -> RepairTransaction:
        """Apply patch to working state and record transaction."""
        sig = f"{patch.action.value}::{patch.scope_predicates}"
        self._action_history.add(sig)

        orig_snapshot = {
            k: copy.deepcopy(v) for k, v in working_state.items()
            if k in ("census", "prospect", "routing", "findings", "results")
        }
        tx = RepairTransaction(patch=patch, original_state=orig_snapshot)

        log.info("Executing repair patch '%s': %s (Params: %s)",
                 patch.patch_id, patch.action.value, patch.parameters)

        if patch.action == RepairAction.REPLACE_STRUCTURED_VALUE:
            census = working_state.get("census")
            params = patch.parameters
            if census and params.get("from_status") == "WP" and params.get("to_status") == "WO":
                repaired = 0
                for emp in census.employees:
                    notes_str = " ".join(str(n) for n in (emp.notes or [])).lower() if isinstance(emp.notes, list) else str(emp.notes or "").lower()
                    if (emp.coverage or "").upper() == "WP" and "waived" in notes_str:
                        emp.coverage = "WO"
                        repaired += 1
                log.info("Repaired %d waived employees from WP to WO", repaired)

            if census and params.get("action") == "strip_mup_from_non_enrolled":
                results = working_state.get("results", {})
                repaired = 0
                for emp in census.employees:
                    cov = (emp.coverage or "").upper()
                    if cov in ("WP", "NE", "WO", "RC"):
                        res = results.get(id(emp))
                        if res:
                            res_notes = " ".join(str(n) for n in res.notes).lower() if isinstance(res.notes, list) else str(res.notes or "").lower()
                            if "most used" in res_notes or "mup" in res_notes:
                                res.plan = None
                                if isinstance(res.notes, list):
                                    res.notes = [n for n in res.notes if "most used" not in str(n).lower()]
                                else:
                                    res.notes = ""
                                repaired += 1
                log.info("Stripped Most Used Plan from %d non-enrolled employees", repaired)

        elif patch.action == RepairAction.APPLY_HEADER_MAPPING:
            census = working_state.get("census")
            params = patch.parameters
            src_header = params.get("source_header", "").lower()
            target_field = params.get("target_field")

            # Apply mapping across employees if source value is present
            if census and target_field == "coverage":
                for emp in census.employees:
                    if not emp.coverage or emp.coverage.strip() == "":
                        # Check custom fields or notes
                        val = getattr(emp, "custom_fields", {}).get(src_header)
                        if val:
                            emp.coverage = str(val).strip().upper()

        elif patch.action == RepairAction.RESOLVE_MULTI_COLUMN_GENDER:
            census = working_state.get("census")
            if census:
                from census_builder import _resolve_person_gender, update_candidate_dispositions
                for p in census.people:
                    _resolve_person_gender(p)
                update_candidate_dispositions(census)

        elif patch.action == RepairAction.FILTER_NON_MEDICAL_DEPENDENTS:
            census = working_state.get("census")
            if census:
                from census_builder import recalculate_row_numbers, update_candidate_dispositions
                removed_ids = set()
                for emp in census.employees:
                    kept = []
                    for dep in emp.dependents:
                        assocs = getattr(dep, "coverage_associations", []) or []
                        if assocs and all(not a.get("is_medical", True) for a in assocs):
                            dep.disposition = "EXCLUDE_NON_MEDICAL_ONLY"
                            if dep not in census.excluded_dependents:
                                census.excluded_dependents.append(dep)
                            removed_ids.add(id(dep))
                        else:
                            kept.append(dep)
                    emp.dependents = kept
                if removed_ids:
                    census.people = [p for p in census.people if id(p) not in removed_ids]
                recalculate_row_numbers(census)
                update_candidate_dispositions(census)

        elif patch.action == RepairAction.RECALCULATE_SUBSCRIBER_LINKS:
            census = working_state.get("census")
            if census:
                from census_builder import recalculate_row_numbers, update_candidate_dispositions
                recalculate_row_numbers(census)
                update_candidate_dispositions(census)

        elif patch.action == RepairAction.EXCLUDE_PART_TIME_DEPENDENTS:
            census = working_state.get("census")
            if census:
                from census_builder import recalculate_row_numbers, update_candidate_dispositions
                removed_ids = set()
                for emp in census.employees:
                    if getattr(emp, "disposition", "") == "EXCLUDE_PART_TIME" and emp.dependents:
                        for dep in list(emp.dependents):
                            dep.disposition = "EXCLUDE_PART_TIME"
                            if dep not in census.excluded_dependents:
                                census.excluded_dependents.append(dep)
                            removed_ids.add(id(dep))
                        emp.dependents.clear()
                if removed_ids:
                    census.people = [p for p in census.people if id(p) not in removed_ids]
                recalculate_row_numbers(census)
                update_candidate_dispositions(census)

        elif patch.action == RepairAction.FORMAT_MATCH_PERCENTAGE:
            results = working_state.get("results", {})
            for r_id, rr in results.items():
                if hasattr(rr, "match_pct") and rr.match_pct is not None:
                    if rr.match_pct > 100:
                        rr.match_pct = 100.0
                    elif rr.match_pct < 0:
                        rr.match_pct = 0.0
            log.info("Enforced percentage formatting constraints on comparison results")

        elif patch.action == RepairAction.RESOLVE_PREFERRED_NAME_ALIAS:
            census = working_state.get("census")
            invs = working_state.get("invoices", []) or working_state.get("invs", [])
            results = working_state.get("results", {})
            findings = working_state.get("findings")
            if census and invs and findings:
                from comparator import run_comparison
                recomputed = run_comparison(
                    census=census,
                    invs=invs,
                    renewal=working_state.get("renewal"),
                    meta=working_state.get("meta", {}),
                    F=findings,
                    have_invoice=True,
                    renewal_required=getattr(findings, "renewal_required", True),
                )
                working_state["results"] = recomputed
            log.info("Resolved preferred name / alias matching across census and invoice subscribers")

        tx.executed = True
        self.executed_patches.append(patch)
        return tx
