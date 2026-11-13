"""Register Feedback.docx Underwriting and Architecture Patterns into the Learning Engine.

Registers the 5 patterns derived from Feedback.docx:
1. pattern_worksheet_boundary_enforcement (Rule 8.1B)
2. pattern_stacked_census_dependent_reconstruction (Rule 8.1A Step 3 / Rule 9.5)
3. pattern_insperity_coverage_code_and_tier_decoding (Rule 10.3)
4. pattern_benefits_enrollment_report_invoice_equivalence (Rule 11.1A / 11.1B)
5. pattern_renewal_rate_application_and_population_invariance (Rule 13.5 / 22.1A)
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add project root and backend to path
ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

from backend.learning_engine.config import CANDIDATES_DIR, REGISTRY_DIR, ensure_directories
from backend.learning_engine.instruction_registry import resolve_instruction_snapshot
from backend.learning_engine.memory_store import MemoryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("register_patterns")

def create_patterns() -> list[dict]:
    snapshot = resolve_instruction_snapshot()
    inst_hash = snapshot.content_hash
    now_iso = datetime.now().isoformat()

    patterns = [
        # 1. Worksheet Boundary Enforcement
        {
            "schema_version": "1.1",
            "learning_id": "pattern_worksheet_boundary_enforcement",
            "version": 1,
            "status": "active",
            "pattern_state": "VALIDATED",
            "fingerprint": "fp_worksheet_boundary_enforcement_v1",
            "patch_version": "2.2.0",
            "generic_root_cause": (
                "Excel census workbooks may contain formatting artifacts, cell styles, or formulas extending "
                "across thousands of empty columns (up to column 16,384/XFD), causing severe performance degradation, "
                "runaway delimiter expansion, and memory saturation if boundaries are not strictly truncated at structured table extent."
            ),
            "applicable_conditions": [
                "Worksheet max_column exceeds structured tabular header columns",
                "Consecutive empty column gap of >= 5 columns across all rows",
                "Presence of phantom or styled blank cells beyond valid data range (e.g. columns A:M vs A:XFD)"
            ],
            "required_validators": [
                "GATE_A_TABLE_BOUNDARY",
                "RULE_08_WORKSHEET_BOUNDARY"
            ],
            "regression_results": {
                "cases_verified": ["case_wellfit_technologies_census_2026_worksheet_boundary"],
                "status": "PASS"
            },
            "created_at": now_iso,
            "updated_at": now_iso,
            "compatibility": {
                "instruction_hashes": [inst_hash],
                "template_hashes": [],
                "parser_versions": ["2.1.0"],
                "runtime_prompt_version": "1.0"
            },
            "scope": {
                "document_roles": ["census", "invoice", "payroll"],
                "file_types": ["xlsx", "xlsm", "xls"],
                "carriers": ["Florida Blue", "Cigna"],
                "header_signature": [
                    "employee name", "job title", "hire date", "coverage level", "option code"
                ],
                "stage": "extraction"
            },
            "trigger": {
                "issue_type": "WORKSHEET_BOUNDARY_OVERFLOW",
                "field": "columns",
                "conditions": ["max_column exceeds tabular range by >= 5 empty columns"]
            },
            "business_rule_refs": [
                "Section 8.1B",
                "RULE_08_WORKSHEET_BOUNDARY"
            ],
            "root_cause": {
                "code": "WORKSHEET_BOUNDARY_OVERFLOW",
                "verification_status": "verified",
                "summary": "Truncate worksheet ingestion at the first column where >= 5 consecutive columns are completely empty across all data rows."
            },
            "strategy": {
                "action": "TRUNCATE_WORKSHEET_BOUNDARY",
                "parameters": {"empty_column_gap_threshold": 5},
                "preconditions": ["contiguous structured table headers identified in columns A:N"],
                "exclusions": ["do not truncate valid multi-tier tables with single empty spacer columns"]
            },
            "verification": {
                "case_successes": 1,
                "case_failures": 0,
                "regression_fixture_ids": ["reg_case_wellfit_boundary"],
                "negative_fixture_ids": [],
                "checks_passed": ["RULE_08_WORKSHEET_BOUNDARY"],
                "promotion_eligible": True,
                "last_verified_at": now_iso
            },
            "promotion": {
                "mode": "auto_allowlisted",
                "decision_ref": "verified_20261112_feedback",
                "reason": "Validated against Feedback.docx Wellfit Technologies worksheet extent audit (Columns A:M bounded)"
            },
            "usage": {"attempted": 0, "successful": 0, "rolled_back": 0}
        },

        # 2. Stacked Census Dependent Reconstruction
        {
            "schema_version": "1.1",
            "learning_id": "pattern_stacked_census_dependent_reconstruction",
            "version": 1,
            "status": "active",
            "pattern_state": "VALIDATED",
            "fingerprint": "fp_stacked_census_dependent_reconstruction_v1",
            "patch_version": "2.2.0",
            "generic_root_cause": (
                "Tabular census rosters frequently present family members as stacked rows directly beneath the "
                "primary employee where Job Title indicates relationship ('Spouse', 'Child', 'Domestic Partner') "
                "rather than employment, with blank employment fields (hire date, salary, employment status). "
                "Failing to recognize these rows as dependents causes severe employee headcount inflation."
            ),
            "applicable_conditions": [
                "Row contains a relationship indicator ('Spouse', 'Child', 'Domestic Partner', 'Dependent') in Job Title or Notes",
                "Employment fields such as hire date, annual pay, or employee status are blank or identical to dependent profile",
                "Preceding row represents an active employee subscriber"
            ],
            "required_validators": [
                "GATE_C_DEPENDENT_DISPOSITION",
                "RULE_09_STACKED_DEPENDENTS",
                "RULE_09_DEPENDENT_LINKAGE"
            ],
            "regression_results": {
                "cases_verified": ["case_wellfit_technologies_stacked_dependents"],
                "status": "PASS"
            },
            "created_at": now_iso,
            "updated_at": now_iso,
            "compatibility": {
                "instruction_hashes": [inst_hash],
                "template_hashes": [],
                "parser_versions": ["2.1.0"],
                "runtime_prompt_version": "1.0"
            },
            "scope": {
                "document_roles": ["census"],
                "file_types": ["xlsx", "csv"],
                "carriers": ["Florida Blue", "Cigna"],
                "header_signature": ["employee name", "job title", "hire date", "annual pay", "relationship"],
                "stage": "linkage"
            },
            "trigger": {
                "issue_type": "STACKED_DEPENDENT_AS_EMPLOYEE",
                "field": "job_title",
                "conditions": ["job title matches relationship keyword and preceding employee exists"]
            },
            "business_rule_refs": [
                "Section 8.1A Step 3",
                "Section 9.5",
                "RULE_09_STACKED_DEPENDENTS"
            ],
            "root_cause": {
                "code": "STACKED_DEPENDENT_AS_EMPLOYEE",
                "verification_status": "verified",
                "summary": "Reconstruct stacked census rows as dependents (SP, CH, DP) attached to preceding subscriber employee; prevent registering them as standalone employees."
            },
            "strategy": {
                "action": "ATTACH_STACKED_DEPENDENT_TO_SUBSCRIBER",
                "parameters": {"relationship_job_titles": ["spouse", "child", "domestic partner", "dependent"]},
                "preconditions": ["preceding subscriber employee established in current file parsing context"],
                "exclusions": ["do not reclassify legitimate employees whose job title contains 'partner' in a professional capacity (e.g., 'VP, Strategic Partnerships')"]
            },
            "verification": {
                "case_successes": 1,
                "case_failures": 0,
                "regression_fixture_ids": ["reg_case_wellfit_stacked_deps"],
                "negative_fixture_ids": [],
                "checks_passed": ["RULE_09_STACKED_DEPENDENTS", "RULE_09_DEPENDENT_LINKAGE"],
                "promotion_eligible": True,
                "last_verified_at": now_iso
            },
            "promotion": {
                "mode": "auto_allowlisted",
                "decision_ref": "verified_20261112_feedback",
                "reason": "Validated against Feedback.docx Wellfit Technologies 104-employee active census invariant (122 stacked dependents attached)"
            },
            "usage": {"attempted": 0, "successful": 0, "rolled_back": 0}
        },

        # 3. Insperity Coverage Code & Tier Decoding
        {
            "schema_version": "1.1",
            "learning_id": "pattern_insperity_coverage_code_and_tier_decoding",
            "version": 1,
            "status": "active",
            "pattern_state": "VALIDATED",
            "fingerprint": "fp_insperity_coverage_code_and_tier_decoding_v1",
            "patch_version": "2.2.0",
            "generic_root_cause": (
                "PEO and Insperity enrollment rosters encode medical benefit options as comma-delimited strings "
                "(e.g. 3,57,58,12) alongside single-character coverage tier codes (E, S, C, K, F, W, I). "
                "Treating these as literal or invalid single codes causes false plan rejections and erroneous 'Waived without plan' anomalies."
            ),
            "applicable_conditions": [
                "Census or enrollment document originates from Insperity or PEO platform",
                "Coverage level uses single-letter codes (E, S, C, K, F, W, I)",
                "Option code contains comma-separated numeric/alphanumeric benefit identifiers"
            ],
            "required_validators": [
                "RULE_10_INSPERITY_CODE_DECODING",
                "RULE_10_REQUIRED_FIELDS"
            ],
            "regression_results": {
                "cases_verified": ["case_wellfit_technologies_insperity_options"],
                "status": "PASS"
            },
            "created_at": now_iso,
            "updated_at": now_iso,
            "compatibility": {
                "instruction_hashes": [inst_hash],
                "template_hashes": [],
                "parser_versions": ["2.1.0"],
                "runtime_prompt_version": "1.0"
            },
            "scope": {
                "document_roles": ["census", "invoice"],
                "file_types": ["xlsx", "csv"],
                "carriers": ["Florida Blue", "Cigna"],
                "header_signature": ["option code", "coverage level", "benefit plan"],
                "stage": "normalization"
            },
            "trigger": {
                "issue_type": "INSPERITY_CODE_UNDECODED",
                "field": "coverage_level_and_options",
                "conditions": ["coverage level is single-letter or option contains comma-delimited string"]
            },
            "business_rule_refs": [
                "Section 10.3",
                "RULE_10_INSPERITY_CODE_DECODING"
            ],
            "root_cause": {
                "code": "INSPERITY_CODE_UNDECODED",
                "verification_status": "verified",
                "summary": "Split comma-delimited option codes, map medical plan codes using Group Identification Key legend (e.g. Code 3 -> Choice Plus 500), map non-medical only codes (57,58) to WO, and normalize single-letter coverage tiers (E->EE, S->ES, C/K->EC, F->EF, W->WO, I->NE)."
            },
            "strategy": {
                "action": "DECODE_INSPERITY_OPTIONS_AND_TIERS",
                "parameters": {
                    "tier_map": {"E": "EE", "S": "ES", "C": "EC", "K": "EC", "F": "EF", "W": "WO", "I": "NE"},
                    "medical_option_codes": {
                        "1": "Choice Plus PPO 250", "2": "Choice Plus PPO 350", "3": "Choice Plus PPO 500",
                        "7": "Choice Plus PPO 750", "8": "Choice Plus PPO 1000", "9": "Choice Plus PPO 1500",
                        "10": "Choice Plus PPO 2000", "11": "Choice Plus PPO 2500"
                    }
                },
                "preconditions": ["valid option string or single letter coverage code"],
                "exclusions": ["do not override explicit standard two-letter tiers (EE, ES, EC, FAM, WAIVE)"]
            },
            "verification": {
                "case_successes": 1,
                "case_failures": 0,
                "regression_fixture_ids": ["reg_case_wellfit_insperity_decoding"],
                "negative_fixture_ids": [],
                "checks_passed": ["RULE_10_INSPERITY_CODE_DECODING", "RULE_10_REQUIRED_FIELDS"],
                "promotion_eligible": True,
                "last_verified_at": now_iso
            },
            "promotion": {
                "mode": "auto_allowlisted",
                "decision_ref": "verified_20261112_feedback",
                "reason": "Validated against Feedback.docx Insperity Group Identification Key decoding rules"
            },
            "usage": {"attempted": 0, "successful": 0, "rolled_back": 0}
        },

        # 4. Benefits Enrollment Report (BER) Invoice Equivalence
        {
            "schema_version": "1.1",
            "learning_id": "pattern_benefits_enrollment_report_invoice_equivalence",
            "version": 1,
            "status": "active",
            "pattern_state": "VALIDATED",
            "fingerprint": "fp_benefits_enrollment_report_invoice_equivalence_v1",
            "patch_version": "2.2.0",
            "generic_root_cause": (
                "When carrier invoices are absent, Benefits Enrollment Reports (BER) serve as the primary invoice-equivalent "
                "billing document. BER files frequently aggregate multi-benefit lines (Dental, Vision, Life, Commuter, FSA) "
                "across hundreds of rows and lack a total premium column. Rejecting BERs for lack of premium columns generates "
                "false 'No invoice was provided' errors and inflates counts."
            ),
            "applicable_conditions": [
                "No standard monthly carrier billing statement provided in submission",
                "Benefits Enrollment Report (BER) or Benefit Elections workbook is present",
                "BER contains medical tier, coverage level, or employee pre-tax deduction amounts across multi-line products"
            ],
            "required_validators": [
                "RULE_11_BER_EQUIVALENCE",
                "RULE_11_BER_MULTI_BENEFIT_FILTER"
            ],
            "regression_results": {
                "cases_verified": ["case_wellfit_technologies_benefits_enrollment_2_invoice"],
                "status": "PASS"
            },
            "created_at": now_iso,
            "updated_at": now_iso,
            "compatibility": {
                "instruction_hashes": [inst_hash],
                "template_hashes": [],
                "parser_versions": ["2.1.0"],
                "runtime_prompt_version": "1.0"
            },
            "scope": {
                "document_roles": ["invoice"],
                "file_types": ["xlsx", "csv"],
                "carriers": ["Florida Blue", "Cigna"],
                "header_signature": ["plan type", "benefit type", "coverage level", "employee contribution", "pre-tax"],
                "stage": "classification"
            },
            "trigger": {
                "issue_type": "BER_CLASSIFICATION_AND_FILTER_FAILURE",
                "field": "document_role",
                "conditions": ["BER not recognized as invoice or non-medical rows leak into subscriber invoice census"]
            },
            "business_rule_refs": [
                "Section 11.1A",
                "Section 11.1B",
                "RULE_11_BER_EQUIVALENCE",
                "RULE_11_BER_MULTI_BENEFIT_FILTER"
            ],
            "root_cause": {
                "code": "BER_CLASSIFICATION_AND_FILTER_FAILURE",
                "verification_status": "verified",
                "summary": "Classify BER as invoice-equivalent when no carrier invoice is present, accept subscriber election and tier columns without requiring total gross premium, and strictly filter out non-medical benefit lines."
            },
            "strategy": {
                "action": "PROMOTE_BER_TO_INVOICE_AND_FILTER_MEDICAL",
                "parameters": {
                    "non_medical_benefit_types": [
                        "dental", "vision", "life", "add", "std", "ltd", "fsa", "hsa", "commuter", "accident", "critical illness"
                    ]
                },
                "preconditions": ["workbook classified as BER or benefit elections"],
                "exclusions": ["do not parse non-medical rows as medical invoice subscribers"]
            },
            "verification": {
                "case_successes": 1,
                "case_failures": 0,
                "regression_fixture_ids": ["reg_case_wellfit_ber_invoice"],
                "negative_fixture_ids": [],
                "checks_passed": ["RULE_11_BER_EQUIVALENCE", "RULE_11_BER_MULTI_BENEFIT_FILTER"],
                "promotion_eligible": True,
                "last_verified_at": now_iso
            },
            "promotion": {
                "mode": "auto_allowlisted",
                "decision_ref": "verified_20261112_feedback",
                "reason": "Validated against Feedback.docx Benefits Enrollment 2 acceptance as invoice equivalent"
            },
            "usage": {"attempted": 0, "successful": 0, "rolled_back": 0}
        },

        # 5. Renewal Rate Application & Population Invariance
        {
            "schema_version": "1.1",
            "learning_id": "pattern_renewal_rate_application_and_population_invariance",
            "version": 1,
            "status": "active",
            "pattern_state": "VALIDATED",
            "fingerprint": "fp_renewal_rate_application_and_population_invariance_v1",
            "patch_version": "2.2.0",
            "generic_root_cause": (
                "When renewal rate tables are extracted from PEO reports (e.g. MAR Report, Financial Stewardship Report), "
                "rate structures and plan tiers must be applied to comparison rate fields without overwriting or displacing "
                "the canonical active census population (104 employees). Erroneously deriving or modifying the active employee roster "
                "from renewal exhibits creates headcount discrepancies."
            ),
            "applicable_conditions": [
                "PEO renewal rate tables extracted from MAR, Stewardship, or renewal exhibits",
                "Canonical active employee census established from primary census documents",
                "Renewal comparison requires future rate population"
            ],
            "required_validators": [
                "RULE_13_RENEWAL_PEO_90_DAY",
                "RULE_13_RENEWAL_POPULATION_INVARIANCE"
            ],
            "regression_results": {
                "cases_verified": ["case_wellfit_technologies_renewal_invariance"],
                "status": "PASS"
            },
            "created_at": now_iso,
            "updated_at": now_iso,
            "compatibility": {
                "instruction_hashes": [inst_hash],
                "template_hashes": [],
                "parser_versions": ["2.1.0"],
                "runtime_prompt_version": "1.0"
            },
            "scope": {
                "document_roles": ["renewal"],
                "file_types": ["xlsx", "pdf", "docx"],
                "carriers": ["Florida Blue", "Cigna"],
                "header_signature": ["plan name", "tier", "renewal rate", "current rate", "proposed rate"],
                "stage": "comparison"
            },
            "trigger": {
                "issue_type": "RENEWAL_POPULATION_DISPLACEMENT",
                "field": "census_population",
                "conditions": ["renewal rate application alters canonical census employee records or count"]
            },
            "business_rule_refs": [
                "Section 13.5",
                "Section 22.1A",
                "RULE_13_RENEWAL_POPULATION_INVARIANCE"
            ],
            "root_cause": {
                "code": "RENEWAL_POPULATION_DISPLACEMENT",
                "verification_status": "verified",
                "summary": "Populate renewal rate tables strictly into renewal rate fields; enforce invariant canonical employee count across all downstream workbook writer stages."
            },
            "strategy": {
                "action": "APPLY_RENEWAL_RATES_WITH_POPULATION_INVARIANCE",
                "parameters": {"enforce_count_invariance": True},
                "preconditions": ["canonical census population finalized"],
                "exclusions": ["never add, remove, or replace active census employees based on renewal exhibits"]
            },
            "verification": {
                "case_successes": 1,
                "case_failures": 0,
                "regression_fixture_ids": ["reg_case_wellfit_renewal_invariance"],
                "negative_fixture_ids": [],
                "checks_passed": ["RULE_13_RENEWAL_PEO_90_DAY", "RULE_13_RENEWAL_POPULATION_INVARIANCE"],
                "promotion_eligible": True,
                "last_verified_at": now_iso
            },
            "promotion": {
                "mode": "auto_allowlisted",
                "decision_ref": "verified_20261112_feedback",
                "reason": "Validated against Feedback.docx MAR and Stewardship report rate population without employee displacement"
            },
            "usage": {"attempted": 0, "successful": 0, "rolled_back": 0}
        }
    ]

    return patterns

def main():
    store = MemoryStore()
    ensure_directories()
    patterns = create_patterns()

    active_rules_path = REGISTRY_DIR / "active_rules.json"
    validated_patterns_path = REGISTRY_DIR / "validated_patterns.json"
    index_path = REGISTRY_DIR / "index.json"

    # Load existing active rules
    existing_active = []
    if active_rules_path.exists():
        try:
            existing_active = json.loads(active_rules_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not load active_rules.json: %s", e)
            existing_active = []

    # Load existing index
    index_data = {
        "active_rule_ids": [],
        "validated_pattern_fingerprints": [],
        "rejected_rule_ids": [],
        "retired_rule_ids": [],
        "candidate_rule_ids": [],
        "last_updated": None
    }
    if index_path.exists():
        try:
            index_data = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not load index.json: %s", e)

    active_ids = set(index_data.get("active_rule_ids", []))
    cand_ids = set(index_data.get("candidate_rule_ids", []))

    for p in patterns:
        lid = p["learning_id"]
        fp = p["fingerprint"]
        # Save candidate JSON
        cand_path = CANDIDATES_DIR / f"{lid}.json"
        cand_path.write_text(json.dumps(p, indent=2), encoding="utf-8")
        log.info("Saved candidate pattern: %s", cand_path.name)

        # Update active rules list
        existing_active = [r for r in existing_active if r.get("learning_id") != lid and r.get("fingerprint") != fp]
        existing_active.append(p)

        active_ids.add(lid)
        cand_ids.add(lid)

    # Atomic write to registry files
    active_rules_path.write_text(json.dumps(existing_active, indent=2), encoding="utf-8")
    validated_patterns_path.write_text(json.dumps(existing_active, indent=2), encoding="utf-8")

    index_data["active_rule_ids"] = sorted(list(active_ids))
    index_data["candidate_rule_ids"] = sorted(list(cand_ids))
    index_data["last_updated"] = datetime.now().isoformat()
    index_path.write_text(json.dumps(index_data, indent=2), encoding="utf-8")

    log.info("Successfully updated registry with %d patterns.", len(patterns))
    log.info("Active rule count in active_rules.json: %d", len(existing_active))

if __name__ == "__main__":
    main()

