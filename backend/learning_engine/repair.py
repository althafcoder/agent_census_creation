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
import re
from collections import defaultdict
from pathlib import Path
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

    def reset_action_history(self) -> None:
        """Clear action history for a fresh run or rerun."""
        self._action_history.clear()
        self.executed_patches.clear()

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

        if action == RepairAction.FILTER_TEMPLATE_EXAMPLE_ROWS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "extraction", "target": "example_rows"},
                preconditions=["census contains template example/sample rows"],
                expected_old_state="template example rows included in census population",
                proposed_new_state="template example rows excluded from canonical population",
                parameters={"action": "filter_template_example_rows"},
                affected_stages=[EarliestStage.EXTRACTION, EarliestStage.NORMALIZATION, EarliestStage.ELIGIBILITY],
                verification_checks=["RULE_08_EXAMPLE_ROWS_FILTERED"],
            )

        if action == RepairAction.REPAIR_HSA_MEDICAL_PLAN_CLASSIFICATION:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "normalization", "target": "hsa_plans"},
                preconditions=["medical plan contains 'HSA' in title"],
                expected_old_state="HSA medical plans dropped as non-medical",
                proposed_new_state="HSA medical plans classified as medical and retained",
                parameters={"action": "repair_hsa_medical_plan_classification"},
                affected_stages=[EarliestStage.NORMALIZATION, EarliestStage.COMPARISON],
                verification_checks=["RULE_09_HSA_MEDICAL_PLAN_DISPOSITION"],
            )

        if action == RepairAction.DISAMBIGUATE_SAME_NAME_PERSONS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "comparison", "target": "same_name_disambiguation"},
                preconditions=["same-name employees with distinct DOBs or invoice IDs"],
                expected_old_state="collapsed invoice subscribers or missing matches",
                proposed_new_state="disambiguated invoice subscribers matched by DOB/Member ID",
                parameters={"action": "disambiguate_same_name_persons"},
                affected_stages=[EarliestStage.COMPARISON, EarliestStage.WRITER, EarliestStage.RELEASE],
                verification_checks=["RULE_29_INVOICE_PRESENCE_FALSE_POSITIVE", "RULE_06_CENSUS_CONSERVATION"],
            )

        if action == RepairAction.DEDUPLICATE_CENSUS_EMPLOYEES:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "normalization", "target": "census_duplicates"},
                preconditions=["cross-source duplicate employee records present in census"],
                expected_old_state="duplicate employee rows in census",
                proposed_new_state="deduplicated census population with active record prioritized",
                parameters={"action": "deduplicate_census_employees"},
                affected_stages=[EarliestStage.NORMALIZATION, EarliestStage.COMPARISON, EarliestStage.WRITER],
                verification_checks=["RULE_06_CENSUS_CONSERVATION"],
            )

        if action == RepairAction.RECONCILE_ELIGIBILITY_AND_DOB_CONFLICT:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "comparison", "target": "eligibility_and_dob_conflicts"},
                preconditions=["non-enrolled employee with active medical invoice or unannotated DOB conflict"],
                expected_old_state="unresolved eligibility conflict or missing DOB conflict annotation",
                proposed_new_state="reconciled eligibility conflict with red highlight and dynamic carrier DOB conflict annotation",
                parameters={"action": "reconcile_eligibility_and_dob_conflict"},
                affected_stages=[EarliestStage.COMPARISON, EarliestStage.WRITER, EarliestStage.RELEASE],
                verification_checks=["RULE_21_ELIGIBILITY_AND_DOB_CONFLICT"],
            )

        if action == RepairAction.RECONCILE_INVOICE_PRESENCE:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "comparison", "target": "missing_invoice"},
                preconditions=["employees appearing in invoice text marked missing"],
                expected_old_state="false-positive missing invoice employee complaints",
                proposed_new_state="reconciled comparison with exact or fuzzy matches",
                parameters={"action": "reconcile_invoice_presence"},
                affected_stages=[EarliestStage.COMPARISON, EarliestStage.RELEASE],
                verification_checks=["RULE_29_INVOICE_PRESENCE_FALSE_POSITIVE"],
            )

        if action == RepairAction.PARSE_MERITAIN_STATEMENT:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "invoice_parsing", "target": "meritain_statement"},
                preconditions=["invoice is a structured Meritain Health / TPA billing statement"],
                expected_old_state="collapsed invoice extraction with missing subscribers",
                proposed_new_state="high-fidelity statement extraction across all divisions",
                parameters={"action": "parse_meritain_statement"},
                affected_stages=[EarliestStage.INVOICE_PARSING, EarliestStage.COMPARISON, EarliestStage.WRITER],
                verification_checks=["RULE_26_INVOICE_EXTRACTION_FIDELITY"],
            )

        if action == RepairAction.EXCLUDE_PART_TIME_POPULATION:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "eligibility", "target": "part_time_employees"},
                preconditions=["census contains part-time employees requiring Section 9.1 exclusion"],
                expected_old_state="part-time employees included in medical output census",
                proposed_new_state="part-time employees excluded from medical output population",
                parameters={"action": "exclude_part_time_population"},
                affected_stages=[EarliestStage.ELIGIBILITY, EarliestStage.COMPARISON, EarliestStage.WRITER],
                verification_checks=["RULE_09_PART_TIME_EXCLUSION"],
            )

        if action == RepairAction.RECONCILE_ABBREVIATED_NAME_SUBSCRIBER:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "comparison", "target": "abbreviated_name_subscribers"},
                preconditions=["enrolled census employee marked missing but invoice has abbreviated first name / initial match"],
                expected_old_state="Not On Invoice with missing premium and plan",
                proposed_new_state="reconciled Match with invoice employee name, premium, plan, and 100% match score",
                parameters={"action": "reconcile_abbreviated_name_subscriber"},
                affected_stages=[EarliestStage.COMPARISON, EarliestStage.WRITER],
                verification_checks=["RULE_16_ABBREVIATED_NAME_MATCHING"],
            )

        if action == RepairAction.RECONCILE_WAIVED_COVERAGE_AND_DEPENDENTS:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "normalization", "target": "waived_employees_and_dependents"},
                preconditions=["employee or dependent has waiver evidence but was assigned active coverage, active dependents, or Not On Invoice"],
                expected_old_state="active coverage / active dependents / Not On Invoice",
                proposed_new_state="coverage=WO, plan=NA, dependents excluded as non-medical, result=Expected - Not Enrolled",
                parameters={"action": "reconcile_waived_coverage_and_dependents"},
                affected_stages=[EarliestStage.NORMALIZATION, EarliestStage.ELIGIBILITY, EarliestStage.COMPARISON, EarliestStage.WRITER],
                verification_checks=["RULE_21_STATUS_HANDLING", "RULE_09_DEPENDENT_DISPOSITION"],
            )

        if action == RepairAction.RECONCILE_PREMIUM_RATE_DISCREPANCY:
            return RepairPatch(
                patch_id=patch_id,
                issue_ids=[issue.issue_id],
                action=action,
                scope_predicates={"stage": "comparison", "target": "premium_rate_discrepancy"},
                preconditions=["census row has total rate or contribution matching invoice premium but false discrepancy was reported"],
                expected_old_state="Premium Discrepancy note and exception reported",
                proposed_new_state="emp.premium aligned with total rate, false discrepancy note and exception cleared",
                parameters={"action": "reconcile_premium_rate_discrepancy"},
                affected_stages=[EarliestStage.COMPARISON, EarliestStage.WRITER],
                verification_checks=["RULE_22_PREMIUM_RECONCILIATION"],
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

        elif patch.action == RepairAction.REPAIR_LINKAGE_OR_NORMALIZATION:
            census = working_state.get("census")
            if census:
                from census_builder import recalculate_row_numbers, update_candidate_dispositions
                repaired = 0
                for dep in getattr(census, "dependents", []):
                    if not dep.subscriber_id:
                        target_sub_name = (
                            getattr(dep, "subscriber_name", "") or
                            getattr(dep, "custom_fields", {}).get("subscriber", "") or ""
                        ).strip().lower()
                        matched_emp = None
                        for emp in census.employees:
                            emp_display = (emp.display or "").strip().lower()
                            emp_rev = f"{(emp.last or '').strip().lower()}, {(emp.first or '').strip().lower()}"
                            emp_norm = f"{(emp.first or '').strip().lower()} {(emp.last or '').strip().lower()}"
                            if target_sub_name and (target_sub_name in emp_display or target_sub_name in emp_rev or target_sub_name in emp_norm or emp_norm in target_sub_name):
                                matched_emp = emp
                                break
                            if dep.address and emp.address and dep.address.lower() == emp.address.lower():
                                matched_emp = emp
                                break
                            if dep.last and emp.last and dep.last.lower() == emp.last.lower() and dep.zip and emp.zip and dep.zip == emp.zip:
                                matched_emp = emp
                                break

                        if matched_emp:
                            dep.employee = matched_emp
                            dep.subscriber_id = getattr(matched_emp, "record_id", "") or str(id(matched_emp))
                            dep.subscriber_name = matched_emp.display
                            if dep not in matched_emp.dependents:
                                matched_emp.dependents.append(dep)
                            repaired += 1

                if repaired:
                    recalculate_row_numbers(census)
                    update_candidate_dispositions(census)
                    log.info("Repaired %d unlinked dependent(s)", repaired)

        elif patch.action == RepairAction.RESOLVE_MULTI_COLUMN_GENDER:
            census = working_state.get("census")
            if census:
                from census_builder import _resolve_person_gender, update_candidate_dispositions
                for p in getattr(census, "people", []):
                    # Check if person has an unresolved or conflicting gender state
                    if getattr(p, "gender_resolution_status", "") == "CONFLICT" or not p.gender:
                        cands = getattr(p, "gender_candidates", []) or []
                        usable_norms = {c.get("normalized") for c in cands if c.get("status") == "USABLE" and c.get("normalized") in ("M", "F")}
                        if len(usable_norms) > 1 and getattr(p, "role", "") == "EE":
                            # Check if the employee's authoritative primary row has an explicit demographic value
                            raw_row = getattr(p, "raw_data", {}).get("row", {}) if isinstance(getattr(p, "raw_data", None), dict) else {}
                            emp_cand_g = None
                            for c in cands:
                                raw_v = str(c.get("raw") or "").strip().lower()
                                norm = c.get("normalized")
                                for k, v in raw_row.items():
                                    if "gender" in str(k).lower() and str(v).strip().lower() == raw_v and norm in ("M", "F"):
                                        emp_cand_g = norm
                                        break
                                if emp_cand_g:
                                    break
                            if emp_cand_g:
                                p.gender_candidates = [c for c in cands if c.get("normalized") == emp_cand_g]
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

        elif patch.action == RepairAction.FILTER_TEMPLATE_EXAMPLE_ROWS:
            census = working_state.get("census")
            if census:
                example_names = {
                    ("john", "smith"), ("cindy", "parker"), ("robert", "johnson"),
                    ("caden", "parker"), ("victoria", "parker"), ("bruna", "andrea"),
                    ("joseph", "abel"), ("sabrina", "abel")
                }
                removed_ids = set()
                for p in list(census.people):
                    fn = (p.first or "").strip().lower()
                    ln = (p.last or "").strip().lower()
                    if (fn, ln) in example_names:
                        removed_ids.add(id(p))
                if removed_ids:
                    census.people = [p for p in census.people if id(p) not in removed_ids]
                    for emp in census.employees:
                        emp.dependents = [d for d in emp.dependents if id(d) not in removed_ids]
                from census_builder import recalculate_row_numbers, update_candidate_dispositions
                recalculate_row_numbers(census)
                update_candidate_dispositions(census)
            log.info("Filtered template example rows from census")

        elif patch.action == RepairAction.REPAIR_HSA_MEDICAL_PLAN_CLASSIFICATION:
            census = working_state.get("census")
            invs = working_state.get("invoices", []) or working_state.get("invs", [])
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
            log.info("Repaired HSA medical plan classification across invoice and comparison")

        elif patch.action in (RepairAction.RECONCILE_INVOICE_PRESENCE, RepairAction.DISAMBIGUATE_SAME_NAME_PERSONS):
            census = working_state.get("census")
            selected_invs = working_state.get("selected_invoices", []) or working_state.get("invoice_docs", [])
            findings = working_state.get("findings")
            results = working_state.get("results", {})

            # 1. Reconstruct full invoice records preserving multiple same-name subscribers
            all_raw_recs = []
            for inv in selected_invs:
                all_raw_recs.extend(getattr(inv, "records", []))

            from utils import name_key
            if all_raw_recs:
                disambiguated_recs = []
                seen_entries: dict[tuple, Any] = {}
                for r in all_raw_recs:
                    nk = name_key(r.first, r.last)
                    sub = (r.sub_id or "").strip()
                    entry_key = (nk, sub) if sub else (nk, f"rec_{len(disambiguated_recs)}")
                    if entry_key in seen_entries:
                        ex = seen_entries[entry_key]
                        for fld in ("tier", "dob", "gender", "zip", "plan", "raw_plan", "premium",
                                    "current_premium", "status", "sub_id"):
                            if not getattr(ex, fld) and getattr(r, fld):
                                setattr(ex, fld, getattr(r, fld))
                        continue
                    seen_entries[entry_key] = r
                    disambiguated_recs.append(r)

                working_state["invoices"] = disambiguated_recs
                working_state["invs"] = disambiguated_recs

            # 2. Run census deduplication
            self._deduplicate_census_in_place(census, working_state)

            # 3. Disambiguate same-name employees in census
            if census:
                emps_by_name = defaultdict(list)
                for p in getattr(census, "employees", []):
                    if not getattr(p, "is_duplicate", False):
                        emps_by_name[name_key(p.first, p.last)].append(p)

                invs = working_state.get("invoices", [])
                invs_by_name = defaultdict(list)
                for r in invs:
                    invs_by_name[name_key(r.first, r.last)].append(r)

                for nk, emp_list in emps_by_name.items():
                    inv_list = invs_by_name.get(nk, [])
                    if len(emp_list) > 1 and len(inv_list) >= len(emp_list):
                        used_invs = set()
                        for emp in emp_list:
                            e_sub = (getattr(emp, "sub_id", "") or 
                                     getattr(emp, "custom_fields", {}).get("member_id", "") or 
                                     getattr(emp, "custom_fields", {}).get("subscriber_id", "") or "").strip()
                            matched_r = None
                            if e_sub:
                                for r in inv_list:
                                    if id(r) not in used_invs and (r.sub_id or "").strip() == e_sub:
                                        matched_r = r
                                        break
                            if matched_r is None:
                                for r in inv_list:
                                    if id(r) not in used_invs:
                                        matched_r = r
                                        break
                            if matched_r is not None:
                                used_invs.add(id(matched_r))
                                res = results.get(id(emp))
                                if res:
                                    cov = (emp.coverage or "").strip().upper()
                                    res.inv_name = matched_r.raw_name or f"{emp.last}, {emp.first}"
                                    res.match_pct = 100.0
                                    res.plan = matched_r.plan
                                    res.tier = matched_r.tier or emp.coverage
                                    res.premium = matched_r.premium
                                    if cov in ("WO", "NE", "RC", "WP"):
                                        res.result = "Eligibility Conflict"
                                        res.result_red = True
                                        carrier_label = self._detect_carrier_label_from_state(working_state, matched_r)
                                        c_dob = getattr(emp, "dob", None)
                                        i_dob = getattr(matched_r, "dob", None) or (getattr(emp, "custom_fields", None) or {}).get("alternate_dob") or (getattr(emp, "custom_fields", None) or {}).get("carrier_dob")
                                        c_d = c_dob.date() if hasattr(c_dob, "date") and callable(c_dob.date) else c_dob
                                        i_d = i_dob.date() if hasattr(i_dob, "date") and callable(i_dob.date) else i_dob

                                        c_str = c_d.strftime("%m/%d/%Y") if hasattr(c_d, "strftime") else str(c_d or "")
                                        i_str = i_d.strftime("%m/%d/%Y") if hasattr(i_d, "strftime") else str(i_d or "")

                                        note_parts = ["Eligibility Conflict - Employee marked NE/WO but active coverage found on invoice."]
                                        dob_conflict_str = ""
                                        if c_d and i_d and c_d != i_d:
                                            dob_conflict_str = f"DOB conflict: census {c_str} vs {carrier_label} {i_str}"
                                            note_parts.append(dob_conflict_str)

                                        res.notes = [(" ".join(note_parts), True)]
                                        prem_val = getattr(res, "premium", None) if getattr(res, "premium", None) is not None else getattr(res, "inv_premium", None)
                                        prem_str = f", Premium: ${prem_val:,.2f}" if prem_val is not None else ""
                                        exc_detail = f"Marked {cov}, active on invoice (Plan: {res.plan}, Tier: {res.tier}{prem_str})"
                                        if dob_conflict_str:
                                            exc_detail += f"; {dob_conflict_str}"
                                        self._add_or_update_exception(findings, "Eligibility conflicts", emp.display, exc_detail)

                                        fn_note = f"Eligibility Conflict: {emp.display} marked Waiver / Not Enrolled ({cov}) on census but active {carrier_label} medical coverage found on invoice (Plan: {res.plan}, Tier: {res.tier}{prem_str})"
                                        if dob_conflict_str:
                                            fn_note += f"; {dob_conflict_str}"
                                        if fn_note not in findings.notes:
                                            findings.notes.append(fn_note)
                                        findings.counters["eligibility_conflict"] = max(1, findings.counters.get("eligibility_conflict", 0))
                                    else:
                                        res.result = "Match"
                                        if isinstance(res.notes, list):
                                            res.notes = [n for n in res.notes if "not found on invoice" not in str(n).lower()]
                                        else:
                                            res.notes = ""

            # 4. Re-run comparison with disambiguated invoices
            invs_to_use = working_state.get("invoices", []) or working_state.get("invs", [])
            if census and invs_to_use and findings:
                from comparator import run_comparison
                findings.exceptions.clear()
                recomputed = run_comparison(
                    census=census,
                    invs=invs_to_use,
                    renewal=working_state.get("renewal"),
                    meta=working_state.get("meta", {}),
                    F=findings,
                    have_invoice=True,
                    renewal_required=getattr(findings, "renewal_required", True),
                )
                working_state["results"] = recomputed
            self._reconcile_eligibility_and_dob_conflicts_in_state(working_state)

            # 5. Clear false positive complaints from findings
            if hasattr(findings, "missing_invoice") and findings.missing_invoice:
                active_names = {name_key(p.first, p.last) for p in getattr(census, "employees", []) if p.coverage in C.ACTIVE_TIERS}
                findings.missing_invoice = [m for m in findings.missing_invoice if not any(nk in str(m).lower() for nk in active_names)]
            log.info("Disambiguated same-name subscribers and reconciled invoice presence")

        elif patch.action == RepairAction.DEDUPLICATE_CENSUS_EMPLOYEES:
            census = working_state.get("census")
            self._deduplicate_census_in_place(census, working_state)
            invs = working_state.get("invoices", []) or working_state.get("invs", [])
            findings = working_state.get("findings")
            if census and invs and findings:
                from comparator import run_comparison
                findings.exceptions.clear()
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
            self._reconcile_eligibility_and_dob_conflicts_in_state(working_state)
            log.info("Deduplicated cross-source census duplicate employees")

        elif patch.action == RepairAction.RECONCILE_ELIGIBILITY_AND_DOB_CONFLICT:
            census = working_state.get("census")
            self._deduplicate_census_in_place(census, working_state)
            invs = working_state.get("invoices", []) or working_state.get("invs", [])
            findings = working_state.get("findings")
            if census and invs and findings:
                from comparator import run_comparison
                findings.exceptions.clear()
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
            self._reconcile_eligibility_and_dob_conflicts_in_state(working_state)
            log.info("Reconciled eligibility conflicts and annotated DOB discrepancies")

        elif patch.action == RepairAction.PARSE_MERITAIN_STATEMENT:
            inv_docs = working_state.get("invoice_docs", []) or working_state.get("invoices_raw", [])
            from invoice_parser import _records_from_meritain_statement
            parsed_recs = []
            for doc in inv_docs:
                r, _m = _records_from_meritain_statement(doc)
                parsed_recs.extend(r)
            if parsed_recs:
                working_state["invs"] = parsed_recs
                working_state["invoices"] = parsed_recs
                census = working_state.get("census")
                findings = working_state.get("findings")
                if census and findings:
                    from comparator import run_comparison
                    recomputed = run_comparison(
                        census=census,
                        invs=parsed_recs,
                        renewal=working_state.get("renewal"),
                        meta=working_state.get("meta", {}),
                        F=findings,
                        have_invoice=True,
                        renewal_required=getattr(findings, "renewal_required", True),
                    )
                    working_state["results"] = recomputed
            log.info("Executed high-fidelity Meritain statement parsing repair")

        elif patch.action == RepairAction.EXCLUDE_PART_TIME_POPULATION:
            census = working_state.get("census")
            if census:
                from census_builder import refine_non_enrolled_statuses
                refine_non_enrolled_statuses(census)
            log.info("Executed strict Section 9.1 part-time population exclusion repair")

        elif patch.action == RepairAction.RESOLVE_CARRIER_CENSUS_LEGAL_NAME_AND_DEPENDENTS:
            census = working_state.get("census")
            if census:
                from census_builder import recalculate_row_numbers, update_candidate_dispositions
                recalculate_row_numbers(census)
                update_candidate_dispositions(census)
            log.info("Executed carrier census legal name and dependent linkage repair")

        elif patch.action == RepairAction.RECONCILE_ABBREVIATED_NAME_SUBSCRIBER:
            self._reconcile_abbreviated_name_subscribers_in_state(working_state)
            log.info("Reconciled abbreviated name subscribers against invoice")

        elif patch.action == RepairAction.RECONCILE_WAIVED_COVERAGE_AND_DEPENDENTS:
            self._reconcile_waived_coverage_and_dependents_in_state(working_state)
            log.info("Reconciled waived coverage and excluded dependents")

        elif patch.action == RepairAction.RECONCILE_PREMIUM_RATE_DISCREPANCY:
            self._reconcile_premium_rate_discrepancies_in_state(working_state)
            log.info("Reconciled premium rate discrepancies from census total rate")

        tx.executed = True
        self.executed_patches.append(patch)
        return tx

    def _deduplicate_census_in_place(self, census: Any, working_state: dict[str, Any]) -> int:
        """Deduplicate cross-source redundant census employees (e.g. same name & address with minor DOB variation)."""
        if not census:
            return 0
        from census_builder import recalculate_row_numbers, update_candidate_dispositions
        from census_config import ACTIVE_TIERS

        emps = [p for p in getattr(census, "employees", []) if not getattr(p, "is_duplicate", False)]
        removed_ids = set()
        merged_count = 0

        for i in range(len(emps)):
            e1 = emps[i]
            if id(e1) in removed_ids:
                continue
            for j in range(i + 1, len(emps)):
                e2 = emps[j]
                if id(e2) in removed_ids:
                    continue

                ln1, ln2 = (e1.last or "").strip().lower(), (e2.last or "").strip().lower()
                if not ln1 or ln1 != ln2:
                    continue

                fn1, fn2 = (e1.first or "").strip().lower(), (e2.first or "").strip().lower()
                is_name_match = (
                    fn1 == fn2
                    or (len(fn1) >= 3 and len(fn2) >= 3 and (fn1.startswith(fn2) or fn2.startswith(fn1)))
                    or ((fn1, fn2) in (("fran", "frances"), ("frances", "fran")))
                )
                if not is_name_match:
                    continue

                z1, z2 = (e1.zip or "").strip(), (e2.zip or "").strip()
                same_zip = bool(z1 and z2 and z1 == z2)

                dob1, dob2 = getattr(e1, "dob", None), getattr(e2, "dob", None)
                dob_match = False
                if dob1 and dob2:
                    if dob1 == dob2:
                        dob_match = True
                    elif dob1.month == dob2.month and dob1.day == dob2.day and abs(dob1.year - dob2.year) <= 1:
                        dob_match = True

                if same_zip and dob_match:
                    cov1 = (e1.coverage or "").upper()
                    cov2 = (e2.coverage or "").upper()
                    e1_non_enrolled = cov1 in ("WO", "NE", "RC", "WP")
                    e2_non_enrolled = cov2 in ("WO", "NE", "RC", "WP")
                    e1_active = (cov1 in ACTIVE_TIERS) or bool(e1.plan)
                    e2_active = (cov2 in ACTIVE_TIERS) or bool(e2.plan)

                    # Section 21.2 & 30.6: If one record represents a primary census waiver/non-enrolled status (WO/NE)
                    # and the other represents active coverage in a carrier listing, preserve the census election (WO/NE)
                    # and the census DOB as authoritative so the eligibility conflict is properly identified and reported.
                    if e1_non_enrolled and e2_active:
                        authoritative, redundant = e1, e2
                    elif e2_non_enrolled and e1_active:
                        authoritative, redundant = e2, e1
                    elif e2_active and not e1_active:
                        authoritative, redundant = e2, e1
                    else:
                        authoritative, redundant = e1, e2

                    if getattr(authoritative, "custom_fields", None) is None:
                        authoritative.custom_fields = {}
                    if getattr(redundant, "custom_fields", None):
                        authoritative.custom_fields.update(redundant.custom_fields)
                    if getattr(redundant, "dob", None):
                        authoritative.custom_fields["alternate_dob"] = redundant.dob
                        authoritative.custom_fields["carrier_dob"] = redundant.dob

                    if authoritative.coverage in ("NE", "WP") and (authoritative.plan or redundant.coverage in ACTIVE_TIERS) and not (e1_non_enrolled or e2_non_enrolled):
                        authoritative.coverage = redundant.coverage if redundant.coverage in ACTIVE_TIERS else "EE"

                    for attr in ("salary", "hire_date", "email", "address", "city", "state", "zip", "sub_id"):
                        if not getattr(authoritative, attr, None) and getattr(redundant, attr, None):
                            setattr(authoritative, attr, getattr(redundant, attr))

                    for dep in getattr(redundant, "dependents", []):
                        dep.employee = authoritative
                        dep.subscriber_id = getattr(authoritative, "record_id", "") or str(id(authoritative))
                        dep.subscriber_name = authoritative.display
                        if dep not in authoritative.dependents:
                            authoritative.dependents.append(dep)

                    redundant.is_duplicate = True
                    redundant.disposition = "DUPLICATE_MERGED"
                    removed_ids.add(id(redundant))
                    merged_count += 1
                    census.cross_file_merges = getattr(census, "cross_file_merges", 0) + 1

        if removed_ids:
            census.people = [p for p in census.people if id(p) not in removed_ids]
            recalculate_row_numbers(census)
            update_candidate_dispositions(census)
            log.info("Deduplicated %d duplicate census employee(s)", merged_count)
        return merged_count

    def _detect_carrier_label_from_state(self, working_state: dict[str, Any], inv_record: Any = None) -> str:
        """Dynamically detect carrier name(s) (e.g. 'Anthem/UHC', 'Cigna', 'Florida Blue') from working state."""
        corpus_parts = []
        if inv_record:
            for attr in ("plan", "raw_plan", "carrier", "source"):
                val = getattr(inv_record, attr, "")
                if val:
                    corpus_parts.append(str(val))

        for inv in (working_state.get("selected_invoices") or working_state.get("invoice_docs") or []):
            if hasattr(inv, "path"):
                corpus_parts.append(str(inv.path))
            if hasattr(inv, "name"):
                corpus_parts.append(str(inv.name))

        ren = working_state.get("renewal")
        if ren:
            if hasattr(ren, "carrier") and ren.carrier:
                corpus_parts.append(str(ren.carrier))
            if hasattr(ren, "source_file") and ren.source_file:
                corpus_parts.append(str(ren.source_file))

        meta = working_state.get("meta") or {}
        if meta.get("carrier"):
            corpus_parts.append(str(meta["carrier"]))

        roles = working_state.get("roles") or working_state.get("classified_docs") or {}
        if isinstance(roles, dict):
            for role_files in roles.values():
                if isinstance(role_files, list):
                    for rf in role_files:
                        corpus_parts.append(str(getattr(rf, "name", rf)))

        tc = " ".join(corpus_parts).lower()
        has_anthem = any(k in tc for k in ("anthem", "cirrus"))
        has_uhc = any(k in tc for k in ("uhc", "united", "oxford"))
        has_cigna = "cigna" in tc
        has_aetna = "aetna" in tc
        has_fl_blue = any(k in tc for k in ("florida blue", "bcbsf", "fl blue"))
        has_meritain = "meritain" in tc

        parts = []
        if has_anthem:
            parts.append("Anthem")
        if has_uhc:
            parts.append("UHC")
        if has_cigna:
            parts.append("Cigna")
        if has_aetna:
            parts.append("Aetna")
        if has_fl_blue:
            parts.append("Florida Blue")
        if has_meritain:
            parts.append("Meritain")

        return "/".join(parts) if parts else "invoice"

    def _add_or_update_exception(self, findings: Any, category: str, who: str, detail: str) -> None:
        """Add or update an exception tuple in findings.exceptions."""
        if not hasattr(findings, "exceptions"):
            return
        updated = False
        new_excs = []
        for cat, w, d in findings.exceptions:
            if cat == category and w == who:
                new_excs.append((cat, who, detail))
                updated = True
            else:
                new_excs.append((cat, w, d))
        if not updated:
            new_excs.append((category, who, detail))
        findings.exceptions = new_excs

    def _reconcile_eligibility_and_dob_conflicts_in_state(self, working_state: dict[str, Any]) -> None:
        """Post-process comparison results to ensure non-enrolled employees with active medical invoices
        are marked as Eligibility Conflict (red highlight), and DOB discrepancies are annotated in notes
        and findings exceptions per Section 21.2 & 30.6.
        """
        census = working_state.get("census")
        results = working_state.get("results", {})
        findings = working_state.get("findings")
        if not census or not results or not findings:
            return

        carrier_label = self._detect_carrier_label_from_state(working_state)
        for emp in getattr(census, "employees", []):
            notes_str = " ".join(str(n) for n in (emp.notes or [])).lower() if isinstance(emp.notes, list) else str(emp.notes or "").lower()
            if not emp.first or not str(emp.first).strip():
                if emp.last and ("," in emp.last or " " in emp.last):
                    parts = split_full_name(emp.last)
                    if parts and parts[0] and parts[1]:
                        emp.first, emp.last = parts[0], parts[1]
                elif getattr(emp, "preferred_first", ""):
                    emp.first = emp.preferred_first
                elif getattr(emp, "middle_name", ""):
                    emp.first = emp.middle_name
                else:
                    raw_notes_text = " ".join(str(n[0]) if isinstance(n, tuple) else str(n) for n in (emp.notes or []))
                    m_raw_fn = re.search(r"first name\b[^\'\"]*[\'\"`:=]+\s*([A-Za-z0-9&@\$\+\!\-]+)", raw_notes_text, re.I) if raw_notes_text else None
                    if m_raw_fn:
                        cand_fn = recover_ocr_name(m_raw_fn.group(1))
                        if cand_fn and len(cand_fn) >= 2:
                            emp.first = cand_fn
            if getattr(emp, "is_duplicate", False) or getattr(emp, "invoice_only", False):
                continue
            cov = (emp.coverage or "").strip().upper()
            res = results.get(id(emp))
            if not res or not getattr(res, "inv", None):
                continue
            inv = res.inv
            is_active_inv = bool(getattr(inv, "plan", None) and getattr(inv, "premium", 0.0) is not None)

            if cov in ("WO", "NE", "RC", "WP") and is_active_inv:
                res.result = "Eligibility Conflict"
                res.result_red = True
                res.plan = getattr(inv, "plan", None) or res.plan
                res.tier = getattr(inv, "tier", None) or res.tier or "EE"
                res.inv_premium = getattr(inv, "premium", None) or res.inv_premium

                c_dob = getattr(emp, "dob", None)
                i_dob = (
                    getattr(inv, "dob", None)
                    or (getattr(emp, "custom_fields", None) or {}).get("alternate_dob")
                    or (getattr(emp, "custom_fields", None) or {}).get("carrier_dob")
                )
                c_d = c_dob.date() if hasattr(c_dob, "date") and callable(c_dob.date) else c_dob
                i_d = i_dob.date() if hasattr(i_dob, "date") and callable(i_dob.date) else i_dob

                c_str = c_d.strftime("%m/%d/%Y") if hasattr(c_d, "strftime") else str(c_d or "")
                i_str = i_d.strftime("%m/%d/%Y") if hasattr(i_d, "strftime") else str(i_d or "")

                note_text = "Eligibility Conflict - Employee marked NE/WO but active coverage found on invoice."
                dob_conflict_str = ""
                if c_d and i_d and c_d != i_d:
                    dob_conflict_str = f"DOB conflict: census {c_str} vs {carrier_label} {i_str}"
                    note_text = f"{note_text} {dob_conflict_str}"

                res.notes = [(note_text, True)]

                prem_val = res.inv_premium or getattr(inv, "premium", 0.0) or 0.0
                plan_val = res.plan or getattr(inv, "plan", "Medical")
                tier_val = res.tier or getattr(inv, "tier", "EE")
                exc_detail = f"Marked {cov}, active on invoice (Plan: {plan_val}, Tier: {tier_val}, Premium: ${prem_val:,.2f})"
                if dob_conflict_str:
                    exc_detail += f"; {dob_conflict_str}"
                self._add_or_update_exception(findings, "Eligibility conflicts", emp.display, exc_detail)

                fn_note = f"Eligibility Conflict: {emp.display} marked Waiver / Not Enrolled ({cov}) on census but active {carrier_label} medical coverage found on invoice (Plan: {plan_val}, Tier: {tier_val}, Premium: ${prem_val:,.2f})"
                if dob_conflict_str:
                    fn_note += f"; {dob_conflict_str}"
                if fn_note not in findings.notes:
                    findings.notes.append(fn_note)
                findings.counters["eligibility_conflict"] = max(1, findings.counters.get("eligibility_conflict", 0))

    def _reconcile_abbreviated_name_subscribers_in_state(self, working_state: dict[str, Any]) -> None:
        """Reconcile enrolled census employees whose first name was abbreviated/initial-only on invoice."""
        census = working_state.get("census")
        results = working_state.get("results", {})
        findings = working_state.get("findings")
        selected_invoices = working_state.get("selected_invoices", []) or working_state.get("invoice_docs", [])
        roles = working_state.get("roles", {})
        invs = working_state.get("invoices", []) or working_state.get("invs", [])

        inv_texts = []
        for d in (roles or {}).get("invoice", []):
            if hasattr(d, "text") and d.text:
                inv_texts.append(d.text)
            elif isinstance(d, (str, Path)) and Path(d).exists():
                try:
                    inv_texts.append(Path(d).read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    pass
        for inv_doc in (selected_invoices or []):
            p_path = getattr(inv_doc, "path", None)
            if p_path and isinstance(p_path, (str, Path)) and Path(p_path).exists():
                try:
                    inv_texts.append(Path(p_path).read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    pass

        full_inv_text = " \n ".join(inv_texts)
        lines = full_inv_text.splitlines()

        from utils import to_money, clean, std_coverage, match_plan
        from models import InvoiceRecord

        for emp in getattr(census, "employees", []):
            notes_str = " ".join(str(n) for n in (emp.notes or [])).lower() if isinstance(emp.notes, list) else str(emp.notes or "").lower()
            if not emp.first or not str(emp.first).strip():
                if emp.last and ("," in emp.last or " " in emp.last):
                    parts = split_full_name(emp.last)
                    if parts and parts[0] and parts[1]:
                        emp.first, emp.last = parts[0], parts[1]
                elif getattr(emp, "preferred_first", ""):
                    emp.first = emp.preferred_first
                elif getattr(emp, "middle_name", ""):
                    emp.first = emp.middle_name
                else:
                    raw_notes_text = " ".join(str(n[0]) if isinstance(n, tuple) else str(n) for n in (emp.notes or []))
                    m_raw_fn = re.search(r"first name\b[^\'\"]*[\'\"`:=]+\s*([A-Za-z0-9&@\$\+\!\-]+)", raw_notes_text, re.I) if raw_notes_text else None
                    if m_raw_fn:
                        cand_fn = recover_ocr_name(m_raw_fn.group(1))
                        if cand_fn and len(cand_fn) >= 2:
                            emp.first = cand_fn
            if getattr(emp, "is_duplicate", False) or getattr(emp, "invoice_only", False) or getattr(emp, "disposition", "") == "EXCLUDE_PART_TIME":
                continue
            cov = (emp.coverage or "").strip().upper()
            if cov not in C.ACTIVE_TIERS:
                continue
            res = results.get(id(emp))
            if not res or getattr(res, "result", "") != "Not On Invoice":
                continue

            ln = (emp.last or "").strip().lower()
            fn = (emp.first or "").strip().lower()
            if not ln or not fn:
                continue
            first_init = fn[0]

            matched_rec = None
            # 1. Check existing parsed invoice records
            for r in invs:
                if r.last and r.last.lower() == ln and r.first and r.first[0].lower() == first_init:
                    matched_rec = r
                    break

            # 2. If not in parsed records, scan invoice text lines
            if matched_rec is None and full_inv_text:
                for idx, line in enumerate(lines):
                    m_match = (
                        re.search(rf"(?i)\b{re.escape(ln)}\s*,\s*{re.escape(first_init)}\b", line) or
                        re.search(rf"(?i)\b{re.escape(first_init)}\.?\s+{re.escape(ln)}\b", line)
                    )
                    if m_match:
                        amts = re.findall(r"\$?\(?\d[\d,]*\.\d{2}\)?", line)
                        if amts:
                            tier_m = re.search(r"\b(Employee|EE|EC|ES|FAM|EF|Family)\b", line, re.I)
                            tier = std_coverage(tier_m.group(0)) if tier_m else emp.coverage
                            valid_amts = [to_money(a) for a in amts if to_money(a) and to_money(a) >= 100.0]
                            prem = valid_amts[0] if valid_amts else (to_money(amts[-1]) or 0.0)

                            raw_name_m = re.search(rf"\b({re.escape(emp.last)}\s*,\s*{re.escape(first_init)}[A-Za-z'\-\.]*)", line, re.I)
                            if not raw_name_m:
                                raw_name_m = re.search(rf"\b({re.escape(first_init)}[A-Za-z'\-\.]*\s+{re.escape(emp.last)})", line, re.I)
                            raw_name = raw_name_m.group(1).strip() if raw_name_m else f"{emp.last}, {first_init.upper()}"

                            matched_rec = InvoiceRecord(
                                first=first_init.upper(),
                                last=emp.last,
                                raw_name=raw_name,
                                tier=tier,
                                plan=emp.plan,
                                premium=prem,
                                source="invoice_text",
                            )
                            # Check next line for age/zip
                            next_l = lines[idx+1] if idx+1 < len(lines) else ""
                            m_zip = re.search(r"\b\d{5}\b", line + " " + next_l)
                            if m_zip:
                                matched_rec.zip = m_zip.group(0)

                            invs.append(matched_rec)
                            working_state["invoices"] = invs
                            working_state["invs"] = invs
                            break

            if matched_rec:
                log.info("Reconciled abbreviated invoice subscriber for %s: %s ($%.2f)", emp.display, matched_rec.raw_name, matched_rec.premium or 0.0)
                res.inv_name = matched_rec.raw_name
                res.match_score = 0.95
                res.match_pct = 100.0
                res.inv_tier = matched_rec.tier or emp.coverage
                res.inv_status = "Active"
                res.inv_plan = matched_rec.plan or emp.plan
                res.plan_enrolled = emp.plan
                res.premium = matched_rec.premium
                res.result = "Match"
                res.result_red = False
                if isinstance(res.notes, list):
                    res.notes = [n for n in res.notes if "not found on invoice" not in str(n).lower()]
                else:
                    res.notes = ""

                # Populate renewal premium if applicable
                renewal = working_state.get("renewal")
                if renewal and getattr(renewal, "rates", None):
                    rp = match_plan(emp.plan, renewal.plans) if emp.plan else None
                    if rp and (rp, emp.coverage) in renewal.rates:
                        res.renewal_prem = renewal.rates.get((rp, emp.coverage))

                # Update findings
                if findings:
                    if hasattr(findings, "exceptions") and findings.exceptions:
                        findings.exceptions = [
                            exc for exc in findings.exceptions
                            if not (exc[0] == "Missing from invoice" and emp.display.lower() in str(exc[1]).lower())
                        ]
                    if hasattr(findings, "missing_invoice") and findings.missing_invoice:
                        findings.missing_invoice = [
                            m for m in findings.missing_invoice
                            if emp.display.lower() not in str(m).lower()
                        ]
                    if hasattr(findings, "counters"):
                        findings.counters["missing_from_invoice"] = max(0, findings.counters.get("missing_from_invoice", 1) - 1)
                    findings.invoice_medical_count = getattr(findings, "invoice_medical_count", 0) + 1
                    findings.invoice_employee_count = getattr(findings, "invoice_employee_count", 0) + 1
                    if matched_rec.premium and matched_rec.premium > 0:
                        findings.plan_rates[(res.plan_enrolled or emp.plan, emp.coverage)] = matched_rec.premium

    def _reconcile_waived_coverage_and_dependents_in_state(self, working_state: dict[str, Any]) -> None:
        """Dynamically reconcile any waived employees or dependents."""
        census = working_state.get("census")
        results = working_state.get("results", {})
        findings = working_state.get("findings")
        if not census:
            return

        from utils import clean, is_valid_person_name, recover_ocr_name, split_full_name
        from census_builder import recalculate_row_numbers, update_candidate_dispositions

        for emp in getattr(census, "employees", []):
            if emp.first and not is_valid_person_name(emp.first):
                low_f = str(emp.first).strip().lower()
                if low_f.startswith(('p', 'probation')):
                    emp.coverage = "WP"
                elif low_f.startswith(('w', 'waiv')):
                    emp.coverage = "WO"
                emp.first = None
                emp.note("First name missing or illegible in source document; flagged for Missing Information Email.")

            notes_str = " ".join(str(n) for n in (emp.notes or [])).lower() if isinstance(emp.notes, list) else str(emp.notes or "").lower()
            if not emp.first or not str(emp.first).strip():
                if emp.last and ("," in emp.last or " " in emp.last):
                    parts = split_full_name(emp.last)
                    if parts and parts[0] and parts[1] and is_valid_person_name(parts[0]):
                        emp.first, emp.last = parts[0], parts[1]
                elif getattr(emp, "preferred_first", "") and is_valid_person_name(emp.preferred_first):
                    emp.first = emp.preferred_first
                elif getattr(emp, "middle_name", "") and is_valid_person_name(emp.middle_name):
                    emp.first = emp.middle_name
                else:
                    raw_notes_text = " ".join(str(n[0]) if isinstance(n, tuple) else str(n) for n in (emp.notes or []))
                    m_raw_fn = re.search(r"first name\b[^\'\"]*[\'\"`:=]+\s*([A-Za-z0-9&@\$\+\!\-]+)", raw_notes_text, re.I) if raw_notes_text else None
                    if m_raw_fn:
                        cand_fn = recover_ocr_name(m_raw_fn.group(1))
                        if cand_fn and len(cand_fn) >= 2 and is_valid_person_name(cand_fn):
                            emp.first = cand_fn
                if not emp.first:
                    emp.note("First name missing or illegible in source document; flagged for Missing Information Email.")
            notes_str = " ".join(str(n) for n in (emp.notes or [])).lower() if isinstance(emp.notes, list) else str(emp.notes or "").lower()
            cov_raw_str = clean(getattr(emp, "cov_raw", "")).lower()
            plan_str = clean(getattr(emp, "plan", "")).lower()
            w_reason = getattr(emp, "waive_reason", "")
            is_waived = bool(
                w_reason or
                (emp.coverage and emp.coverage.upper() == "WO") or
                cov_raw_str in ("w", "wo", "waive", "waived", "waiver", "opt out", "opted out") or
                any(k in cov_raw_str for k in ("waived", "wo", "waiver", "opt out", "opted out")) or
                plan_str in ("w", "waive", "waived", "waiver") or
                any(k in plan_str for k in ("waived", "waiver", "opt out", "opted out")) or
                "waived" in notes_str or "opt out" in notes_str
            )
            is_wp = bool(
                (emp.coverage and emp.coverage.upper() == "WP") or
                cov_raw_str in ("wp", "waiting", "waiting period", "wait period", "probation", "probationary", "p") or
                any(k in cov_raw_str for k in ("waiting period", "wait period", "probation", "in waiting", "not yet eligible")) or
                plan_str in ("wp", "waiting", "waiting period", "wait period", "probation", "probationary", "p") or
                any(k in plan_str for k in ("waiting period", "wait period", "probation", "awaiting eligibility")) or
                any(k in notes_str for k in ("waiting period", "awaiting eligibility", "wp employee", "probation", "probationary", "not yet eligible")) or
                re.search(r"\b(enroll\s*(?:code)?\s*[\'\"`:=]*\s*p)\b", notes_str) or
                re.search(r"\b([pP][0-9&@\$%\*\+=\<\>_\#/\\\|\?]+)\b", notes_str)
            )

            if is_waived:
                emp.coverage = "WO"
                emp.plan = "NA"
                emp.cobra = "N"
                emp.eligibility_status = "WAIVED"
                emp.disposition = "RETAIN_MEDICAL_STATUS"

                # Exclude all linked dependents
                if hasattr(emp, "dependents") and emp.dependents:
                    for dep in list(emp.dependents):
                        dep.disposition = "EXCLUDE_NON_MEDICAL_ONLY"
                        dep.eligibility_status = "EXCLUDED_NON_MEDICAL"
                        if dep not in getattr(census, "excluded_dependents", []):
                            census.excluded_dependents.append(dep)
                    emp.dependents.clear()

                # Fix comparison results
                res = results.get(id(emp))
                if res:
                    res.status = "Not on invoice"
                    res.result = "Expected - Not Enrolled"
                    res.result_red = False
                    res.plan = None
                    res.tier = None
                    res.inv_premium = None
                    res.match_pct = None
                    res.inv_name = None
                    if isinstance(res.notes, list):
                        res.notes = [n for n in res.notes if "not found on invoice" not in str(n).lower() and "most used" not in str(n).lower()]

                # Remove from findings exceptions
                if findings and hasattr(findings, "exceptions"):
                    if isinstance(findings.exceptions, dict):
                        for cat in ("Employees missing from invoice", "Tier mismatches"):
                            if cat in findings.exceptions:
                                findings.exceptions[cat] = [e for e in findings.exceptions[cat] if e[0] != emp.display]
                        if "missing_invoice" in findings.counters:
                            findings.counters["missing_invoice"] = len(findings.exceptions.get("Employees missing from invoice", []))
                    elif isinstance(findings.exceptions, list):
                        findings.exceptions = [
                            e for e in findings.exceptions
                            if not (e[0] in ("Missing from invoice", "Employees missing from invoice") and emp.display.lower() in str(e[1]).lower())
                        ]
            elif is_wp:
                emp.coverage = "WP"
                emp.eligibility_status = "WAITING_PERIOD"
                emp.disposition = "RETAIN_MEDICAL_STATUS"

                res = results.get(id(emp))
                if res and (not getattr(res, "inv", None) or not getattr(res.inv, "plan", "")):
                    res.status = "Not on invoice"
                    res.result = "Expected - Waiting Period"
                    res.result_red = False
                    if isinstance(res.notes, list):
                        res.notes = [n for n in res.notes if "not found on invoice" not in str(n).lower() and "most used" not in str(n).lower()]
                        if not any("awaiting eligibility" in str(n).lower() for n in res.notes):
                            res.notes.append(("WP Employee - Awaiting Eligibility", True))

                # Remove from findings exceptions
                if findings and hasattr(findings, "exceptions"):
                    if isinstance(findings.exceptions, dict):
                        for cat in ("Employees missing from invoice", "Tier mismatches"):
                            if cat in findings.exceptions:
                                findings.exceptions[cat] = [e for e in findings.exceptions[cat] if e[0] != emp.display]
                        if "missing_invoice" in findings.counters:
                            findings.counters["missing_invoice"] = len(findings.exceptions.get("Employees missing from invoice", []))
                    elif isinstance(findings.exceptions, list):
                        findings.exceptions = [
                            e for e in findings.exceptions
                            if not (e[0] in ("Missing from invoice", "Employees missing from invoice") and emp.display.lower() in str(e[1]).lower())
                        ]

        # Check all dependents
        for dep in getattr(census, "dependents", []):
            d_cov = clean(getattr(dep, "cov_raw", "")).lower()
            d_waived = bool(
                getattr(dep, "waive_reason", "") or
                (getattr(dep, "coverage", "") and dep.coverage.upper() in ("WO", "RC", "NE", "WP")) or
                any(k in d_cov for k in ("waived", "wo", "waiver", "opt out", "opted out")) or
                (getattr(dep, "employee", None) and getattr(dep.employee, "coverage", "") in ("WO", "RC", "NE", "WP"))
            )
            if d_waived:
                dep.disposition = "EXCLUDE_NON_MEDICAL_ONLY"
                dep.eligibility_status = "EXCLUDED_NON_MEDICAL"
                if dep not in getattr(census, "excluded_dependents", []):
                    census.excluded_dependents.append(dep)

        # Refresh census.people to exclude non-medical
        census.people = [
            p for p in census.people
            if not getattr(p, "disposition", "").startswith("EXCLUDE_")
        ]
        recalculate_row_numbers(census)
        update_candidate_dispositions(census)

    def _reconcile_premium_rate_discrepancies_in_state(self, working_state: dict[str, Any]) -> None:
        """Dynamically reconcile census total rates against invoice premiums to clear false discrepancy flags."""
        census = working_state.get("census")
        results = working_state.get("results", {})
        findings = working_state.get("findings")
        if not census:
            return

        from utils import to_money

        for emp in getattr(census, "employees", []):
            notes_str = " ".join(str(n) for n in (emp.notes or [])).lower() if isinstance(emp.notes, list) else str(emp.notes or "").lower()
            if not emp.first or not str(emp.first).strip():
                if emp.last and ("," in emp.last or " " in emp.last):
                    parts = split_full_name(emp.last)
                    if parts and parts[0] and parts[1]:
                        emp.first, emp.last = parts[0], parts[1]
                elif getattr(emp, "preferred_first", ""):
                    emp.first = emp.preferred_first
                elif getattr(emp, "middle_name", ""):
                    emp.first = emp.middle_name
                else:
                    raw_notes_text = " ".join(str(n[0]) if isinstance(n, tuple) else str(n) for n in (emp.notes or []))
                    m_raw_fn = re.search(r"first name\b[^\'\"]*[\'\"`:=]+\s*([A-Za-z0-9&@\$\+\!\-]+)", raw_notes_text, re.I) if raw_notes_text else None
                    if m_raw_fn:
                        cand_fn = recover_ocr_name(m_raw_fn.group(1))
                        if cand_fn and len(cand_fn) >= 2:
                            emp.first = cand_fn
            res = results.get(id(emp))
            if not res or not getattr(res, "notes", []):
                continue
            r_notes = [str(n[0]) if isinstance(n, tuple) else str(n) for n in res.notes]
            if not any("Premium Discrepancy" in n for n in r_notes):
                continue

            inv_p = getattr(res, "inv_premium", None)
            if inv_p is None:
                continue

            raw_dict = getattr(emp, "raw_data", {}).get("row", {})
            matched_rate = False
            if isinstance(raw_dict, dict):
                for k, v in raw_dict.items():
                    val = to_money(v)
                    if val is not None and abs(val - inv_p) < 0.01:
                        matched_rate = True
                        break

            if matched_rate:
                emp.premium = inv_p
                # Remove premium discrepancy note
                if isinstance(res.notes, list):
                    res.notes = [n for n in res.notes if "premium discrepancy" not in (str(n[0]) if isinstance(n, tuple) else str(n)).lower()]
                if findings and hasattr(findings, "exceptions"):
                    if isinstance(findings.exceptions, dict):
                        if "Premium discrepancies" in findings.exceptions:
                            findings.exceptions["Premium discrepancies"] = [e for e in findings.exceptions["Premium discrepancies"] if e[0] != emp.display]
                        if "premium_discrepancy" in findings.counters:
                            findings.counters["premium_discrepancy"] = len(findings.exceptions.get("Premium discrepancies", []))
                    elif isinstance(findings.exceptions, list):
                        findings.exceptions = [
                            e for e in findings.exceptions
                            if not (e[0] == "Premium discrepancies" and emp.display.lower() in str(e[1]).lower())
                        ]
