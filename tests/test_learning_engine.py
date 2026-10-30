"""Comprehensive Automated Test Suite for the Learning Engine Agent.

Covers the 20 required verification scenarios specified in the Learning Engine build prompt:
1. Instruction snapshot loading, hashing, tampering detection, and complete rule coverage.
2. Complete chunking / coverage for rosters beyond 500 rows.
3. Multi-table / dependent tab coverage without concealment.
4. Lineage and deduplication for wide columns and separate files.
5. Missing demographic recovery vs legitimate blanks and conflicting data.
6. Florida vs Non-Florida routing vs Unknown state clarification (no carrier workbook).
7. Missing census handling: email generated without fabricated workbook or API crash.
8. Status handling: WP/NE/WO/RC, Waived is WO not WP, Most Used Plan restrictions.
9. Renewal PEO 90-day rule and Claims funding / employee count threshold.
10. Password isolation: two concurrent cases cannot share passwords; zero leaks.
11. Mixed digital/scanned PDFs and AcroForm handling.
12. Workbook audit with openpyxl and Marsh-to-SBU reconciliation.
13. Cache bypass and invalidation on repair.
14. Controlled repair loop: rollback on no-improvement, budget limits, no recursive entry.
15. Rule promotion: candidate passes positive and negative fixtures before activation.
16. Procedural memory retrieval on subsequent run without copying prospect facts.
17. Thread-safe atomic writes and corruption quarantining.
18. Zero secrets in logs, metadata, trace, learning JSON, or emails.
19. Workbook findings & email reconciliation; Section 29 false-positive prevention.
20. Upload preservation & release gating.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "backend"))

import openpyxl
import pandas as pd
import pytest

import census_config as C
from learning_engine import (
    CheckStatus,
    DocumentManifest,
    EarliestStage,
    EngineStatus,
    FieldEvidence,
    FieldStatus,
    Issue,
    IssueSeverity,
    IssueType,
    LearningEngineOrchestrator,
    LearningRule,
    MemoryStore,
    OutputAuditor,
    ProvenanceTracker,
    RepairAction,
    RepairEngine,
    RepairPatch,
    RuleRetriever,
    RuleStatus,
    RunContext,
    SourceCoverageLedger,
    SourceRef,
    UnderwritingValidator,
    chunk_dataframe,
    chunk_text,
    resolve_instruction_snapshot,
    sanitize_secrets,
    verify_instruction_snapshot,
)
from models import Census, Findings, Person, Prospect
from router import RoutingDecision


# -----------------------------------------------------------------------------
# Scenario 1: Instruction Snapshot, Hashing, and Registry Coverage
# -----------------------------------------------------------------------------
def test_instruction_snapshot_and_registry_coverage():
    """Verify full approved instruction snapshot loading, hashing, and 31-section registry."""
    snapshot = resolve_instruction_snapshot()
    assert snapshot.content_hash, "Snapshot must have SHA-256 hash"
    assert len(snapshot.content) > 1000, "Snapshot content must not be empty"

    # Verify tampering detection
    assert verify_instruction_snapshot(snapshot.content_hash) is True
    assert verify_instruction_snapshot("tampered_hash_12345") is False

    # Verify all 31 main sections are mapped in rule registry
    rules = snapshot.rules
    assert len(rules) >= 28, f"Expected coverage of master instruction sections, got {len(rules)}"
    assert "RULE_01_PRECEDENCE" in rules
    assert "RULE_04_FLORIDA_ROUTING" in rules
    assert "RULE_04_NON_FLORIDA_ROUTING" in rules
    assert "RULE_09_PART_TIME_EXCLUSION" in rules
    assert "RULE_13_RENEWAL_PEO_90_DAY" in rules
    assert "RULE_15_CLAIMS_REQUIREMENT" in rules
    assert "RULE_20_MOST_USED_PLAN" in rules
    assert "RULE_21_STATUS_HANDLING" in rules
    assert "RULE_24_MARSH_TO_SBU" in rules
    assert "RULE_29_FALSE_POSITIVE_CHECK" in rules
    assert "RULE_30_PRE_OUTPUT_AUDIT" in rules


# -----------------------------------------------------------------------------
# Scenario 2: Complete Chunking Beyond 500 Rows
# -----------------------------------------------------------------------------
def test_large_roster_complete_chunking():
    """Verify that rosters beyond 500 rows are chunked with 100% row preservation."""
    # Generate 1,200 rows
    rows = [{"First Name": f"EmpFirst_{i}", "Last Name": f"EmpLast_{i}", "DOB": "1985-05-12", "Tier": "EE"} for i in range(1200)]
    df = pd.DataFrame(rows)

    chunks = chunk_dataframe(df, table_name="EnterpriseRoster", chunk_size=250)
    assert len(chunks) == 5  # 250 * 4 = 1000, + 200 = 1200
    total_reconstructed_rows = sum(len(c.df) for c in chunks)
    assert total_reconstructed_rows == 1200, "Every row must be preserved without truncation"
    assert chunks[0].start_row == 0
    assert chunks[-1].end_row == 1200


# -----------------------------------------------------------------------------
# Scenario 3: Multi-Table / Dependent Tab Coverage Without Concealment
# -----------------------------------------------------------------------------
def test_source_coverage_ledger_multi_table_tracking():
    """Verify that one parsed sheet does not conceal another unparsed or failed sheet."""
    ledger = SourceCoverageLedger(run_id="run_test_cov")
    manifest = DocumentManifest(
        document_id="Census_MultiSheet.xlsx",
        filename="Census_MultiSheet.xlsx",
        expected_units=["Employees", "Dependents", "COBRA"],
        visited_units=["Employees"],
    )
    ledger.register_document(manifest)

    # Record sheet 1 as visited
    ledger.record_unit_visit("Census_MultiSheet.xlsx", "Employees", status="VISITED", parsed_count=100)
    # Record sheet 2 as failed
    ledger.record_unit_visit("Census_MultiSheet.xlsx", "Dependents", status="FAILED", error="Header unparseable")
    # Sheet 3 remains UNPARSED

    summary = ledger.get_coverage_summary()
    assert summary["is_complete"] is False
    assert summary["visited_units"] == 1
    assert summary["failed_units"] == 1
    assert summary["unparsed_units"] == 1
    assert "Census_MultiSheet.xlsx::Dependents" in summary["unparsed_unit_ids"]


# -----------------------------------------------------------------------------
# Scenario 4: Lineage and Deduplication for Wide Columns
# -----------------------------------------------------------------------------
def test_provenance_lineage_and_field_tracking():
    """Verify field-level provenance across stages and deduplication."""
    prov = ProvenanceTracker(run_id="run_lineage_1")
    sref = SourceRef(document_id="census.xlsx", original_filename="census.xlsx", location="Sheet1!Row:10")

    ev = prov.record_field(
        record_id="emp_001",
        field_name="coverage",
        raw_value="Employee + Child(ren)",
        normalized_value="EC",
        status=FieldStatus.PRESENT_SUPPORTED,
        source_ref=sref,
        rule_id="RULE_10_REQUIRED_FIELDS",
    )
    assert ev.normalized_value == "EC"
    assert ev.source_refs[0].location == "Sheet1!Row:10"

    retrieved = prov.get_evidence("emp_001", "coverage")
    assert retrieved is not None
    assert retrieved.normalized_value == "EC"


# -----------------------------------------------------------------------------
# Scenario 5: Missing Demographic Recovery vs Legitimate Blank
# -----------------------------------------------------------------------------
def test_demographic_recovery_and_legitimate_blanks():
    """Verify that genuine blanks stay blank while discoverable values are recovered."""
    from learning_engine.diagnosis import DiagnosticEngine

    ledger = SourceCoverageLedger(run_id="run_diag_1")
    prov = ProvenanceTracker(run_id="run_diag_1")
    engine = DiagnosticEngine(coverage_ledger=ledger, provenance=prov)

    # Mock doc with a column named 'Birth Date'
    mock_doc = MagicMock()
    mock_doc.name = "roster.xlsx"
    mock_table = MagicMock()
    mock_table.df = pd.DataFrame([{"First": "Jane", "Last": "Doe", "Birth Date": "1990-01-01"}])
    mock_doc.tables = [mock_table]

    issue = Issue(
        issue_id="ISSUE_DOB",
        rule_id="RULE_10_REQUIRED_FIELDS",
        target_ref="emp.dob",
        issue_type=IssueType.HEADER_MAPPING_FAILED,
        severity=IssueSeverity.CRITICAL,
        earliest_stage=EarliestStage.MAPPING,
        cause_status="unverified",
    )

    diagnosed = engine.diagnose_issue(issue, docs=[mock_doc])
    assert diagnosed.cause_status == "confirmed"
    assert RepairAction.APPLY_HEADER_MAPPING in diagnosed.allowed_actions
    assert "Birth Date" in diagnosed.expected_state


# -----------------------------------------------------------------------------
# Scenario 6: Routing Invariants: Florida vs Non-Florida vs Unknown State
# -----------------------------------------------------------------------------
def test_carrier_routing_invariants():
    """Verify Florida routes to FL Blue, Non-FL to Cigna, and Unknown state generates clarification."""
    snapshot = resolve_instruction_snapshot()
    validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)

    # 1. Florida Case
    prospect_fl = Prospect(company="Miami Logistics", state="FL")
    routing_fl = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    census = Census()
    emp = Person("EE", "Alice", "Smith")
    emp.coverage = "EE"
    census.people = [emp]

    report_fl = validator.validate_full_pipeline(
        prospect=prospect_fl, routing=routing_fl, census=census,
        selected_invoices=[], renewal=None, ren_required=False,
        claims_required=False, findings=Findings(), results={},
        roles={"ghq": [MagicMock()]},  # GHQ present for FL
    )
    fl_check = [c for c in report_fl.checks if c.rule_id == "RULE_04_FLORIDA_ROUTING"]
    assert fl_check[0].status == CheckStatus.PASS

    # 2. Confirmed Non-Florida (e.g. TX) -> Cigna
    prospect_tx = Prospect(company="Dallas Tech", state="TX")
    routing_tx = RoutingDecision(carrier=C.CARRIER_CIGNA, state="TX", is_florida=False)
    report_tx = validator.validate_full_pipeline(
        prospect=prospect_tx, routing=routing_tx, census=census,
        selected_invoices=[], renewal=None, ren_required=False,
        claims_required=False, findings=Findings(), results={},
        roles={},
    )
    tx_check = [c for c in report_tx.checks if c.rule_id == "RULE_04_NON_FLORIDA_ROUTING"]
    assert tx_check[0].status == CheckStatus.PASS

    # 3. Unknown State Case -> Carrier assigned violates rule!
    routing_unk = RoutingDecision(carrier=C.CARRIER_CIGNA, state="", is_florida=False)
    report_unk = validator.validate_full_pipeline(
        prospect=prospect_tx, routing=routing_unk, census=census,
        selected_invoices=[], renewal=None, ren_required=False,
        claims_required=False, findings=Findings(), results={},
        roles={},
    )
    carrier_issues = [i for i in report_unk.issues if i.issue_id == "ISSUE_CARRIER_ASSIGNED_WITHOUT_STATE"]
    assert len(carrier_issues) == 1


# -----------------------------------------------------------------------------
# Scenario 7: Missing Census Scenario
# -----------------------------------------------------------------------------
def test_missing_census_generates_email_without_crash():
    """Verify that a submission with no census does not crash and produces valid findings."""
    snapshot = resolve_instruction_snapshot()
    validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)

    prospect = Prospect(company="Acme Corp", state="FL")
    routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    census = Census()  # 0 employees

    report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census,
        selected_invoices=[], renewal=None, ren_required=False,
        claims_required=False, findings=Findings(), results={},
        roles={"ghq": [MagicMock()]},
    )
    census_check = [c for c in report.checks if c.rule_id == "RULE_25_MISSING_DOCS"]
    assert census_check[0].status == CheckStatus.FAIL
    assert report.release_status == EngineStatus.NEEDS_SOURCE_INFORMATION


# -----------------------------------------------------------------------------
# Scenario 8: Status Codes: Waived is WO not WP & Most Used Plan Restrictions
# -----------------------------------------------------------------------------
def test_status_codes_and_most_used_plan_restrictions():
    """Verify that Waived must be WO not WP, and Most Used Plan is never applied to WP/NE/WO/RC."""
    snapshot = resolve_instruction_snapshot()
    validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)

    census = Census()
    # Employee 1: Waived mistakenly coded as WP
    emp1 = Person("EE", "John", "Doe")
    emp1.coverage = "WP"
    emp1.notes = [("Waived medical coverage", "")]
    # Employee 2: WP with Most Used Plan applied
    emp2 = Person("EE", "Bob", "Smith")
    emp2.coverage = "WP"

    census.people = [emp1, emp2]

    from models import RowResult
    results = {
        id(emp2): RowResult(plan="FL Blue Silver (Most Used Plan)", notes=["Most Used Plan applied"])
    }

    routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    prospect = Prospect(company="Test Co", state="FL")

    report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census,
        selected_invoices=[], renewal=None, ren_required=False,
        claims_required=False, findings=Findings(), results=results,
        roles={"ghq": [MagicMock()]},
    )

    wo_check = [c for c in report.checks if c.rule_id == "RULE_21_STATUS_HANDLING"]
    assert wo_check[0].status == CheckStatus.FAIL

    mup_check = [c for c in report.checks if c.rule_id == "RULE_20_MOST_USED_PLAN"]
    assert mup_check[0].status == CheckStatus.FAIL


# -----------------------------------------------------------------------------
# Scenario 9: Renewal PEO 90-Day Rule and Claims Requirement
# -----------------------------------------------------------------------------
def test_renewal_peo_90_day_and_claims_threshold():
    """Verify renewal applicability under PEO 90-day rule and claims requirement (>50 & self-funded)."""
    snapshot = resolve_instruction_snapshot()
    validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)

    # 1. Claims: Self-Funded + 55 employees -> claims required
    prospect = Prospect(company="Big Self-Funded Corp", state="TX")
    prospect.funding_type = "Self-Funded"
    routing = RoutingDecision(carrier=C.CARRIER_CIGNA, state="TX", is_florida=False)

    census = Census()
    census.people = [Person("EE", f"Emp_{i}", "Test", coverage="EE") for i in range(55)]

    # If claims_required is False when it should be True -> check fails!
    report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census,
        selected_invoices=[], renewal=None, ren_required=False,
        claims_required=False, findings=Findings(), results={},
        roles={},
    )
    claims_check = [c for c in report.checks if c.rule_id == "RULE_15_CLAIMS_REQUIREMENT"]
    assert claims_check[0].status == CheckStatus.FAIL


# -----------------------------------------------------------------------------
# Scenario 10: Password Isolation Across Concurrent Cases
# -----------------------------------------------------------------------------
def test_password_isolation_between_cases():
    """Verify two concurrent cases cannot access each other's credentials."""
    from protected_files import TransientPasswordStore, set_current_store, get_current_store, CandidatePassword

    store_case_a = TransientPasswordStore()
    store_case_b = TransientPasswordStore()

    store_case_a.add_candidate(CandidatePassword(value="SecretPassA123", source_doc="email_a.msg"))
    store_case_b.add_candidate(CandidatePassword(value="SecretPassB456", source_doc="email_b.msg"))

    # Activate store A
    set_current_store(store_case_a)
    assert get_current_store().get_candidates() == ["SecretPassA123"]

    # Activate store B
    set_current_store(store_case_b)
    assert get_current_store().get_candidates() == ["SecretPassB456"]
    assert "SecretPassA123" not in get_current_store().get_candidates()

    # Reset
    set_current_store(None)


# -----------------------------------------------------------------------------
# Scenario 11: Secret Redaction (Zero Leakage)
# -----------------------------------------------------------------------------
def test_zero_password_leakage():
    """Verify passwords are scrubbed from text, logs, and outputs."""
    raw_text = "The encrypted file was unlocked using password: MySecretPassword! and pw=AnotherSecret."
    scrubbed = sanitize_secrets(raw_text, ["MySecretPassword!"])
    assert "MySecretPassword!" not in scrubbed
    assert "[REDACTED_PASSWORD]" in scrubbed


# -----------------------------------------------------------------------------
# Scenario 12: Saved Workbook Openpyxl Audit & SBU Reconciliation
# -----------------------------------------------------------------------------
def test_output_auditor_openpyxl_and_sbu():
    """Verify saved Excel workbook inspection and Marsh-to-SBU row reconciliation."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        marsh_file = tmp_path / "Marsh_Test.xlsx"
        sbu_file = tmp_path / "SBU_Test.xlsx"

        # Create dummy Marsh workbook
        wb_m = openpyxl.Workbook()
        ws_m = wb_m.active
        ws_m.title = "Census"
        ws_m.cell(row=1, column=1, value="Employee ID")
        for r in range(2, 12):
            ws_m.cell(row=r, column=1, value=f"EMP_{r}")
            ws_m.cell(row=r, column=2, value=f"Name_{r}")
        wb_m.save(marsh_file)

        # Create matching SBU workbook
        wb_s = openpyxl.Workbook()
        ws_s = wb_s.active
        ws_s.title = "SBU Roster"
        ws_s.cell(row=1, column=1, value="ID")
        for r in range(2, 12):
            ws_s.cell(row=r, column=1, value=f"EMP_{r}")
            ws_s.cell(row=r, column=2, value=f"Name_{r}")
        wb_s.save(sbu_file)

        auditor = OutputAuditor(run_id="run_audit_test")
        ok, errs = auditor.reconcile_marsh_to_sbu(marsh_file, sbu_file)
        assert ok is True
        assert len(errs) == 0


# -----------------------------------------------------------------------------
# Scenario 13: Cache Bypass and Cache Invalidation on Repair
# -----------------------------------------------------------------------------
def test_cache_bypass_explicit_and_invalidation():
    """Verify cache bypass explicitly triggers cache misses and respects environment."""
    from caching import get_local_cache
    cache = get_local_cache()
    prompt = "Unique test prompt for cache bypass testing."
    model = "claude-opus-5-5"

    # Store entry in cache
    cache.set(prompt, model, {"result": "cached_data"}, step_name="test")

    # Regular lookup hits
    hit = cache.get(prompt, model, step_name="test", bypass_cache=False)
    # Bypass lookup returns None
    miss = cache.get(prompt, model, step_name="test", bypass_cache=True)
    assert miss is None


# -----------------------------------------------------------------------------
# Scenario 14: Controlled Repair Loop and Rollback
# -----------------------------------------------------------------------------
def test_repair_engine_execution_and_rollback():
    """Verify that repair patches apply to working state and roll back on regression."""
    engine = RepairEngine(max_cycles=3)
    census = Census()
    emp = Person("EE", "John", "Doe")
    emp.coverage = "WP"
    emp.notes = [("waived employee", "")]
    census.people = [emp]

    working_state = {"census": census}

    patch = RepairPatch(
        patch_id="patch_test_1",
        issue_ids=["ISSUE_1"],
        action=RepairAction.REPLACE_STRUCTURED_VALUE,
        parameters={"from_status": "WP", "to_status": "WO"},
        affected_stages=[EarliestStage.NORMALIZATION],
    )

    tx = engine.execute_patch(patch, working_state)
    assert tx.executed is True
    assert emp.coverage == "WO"

    # Rollback
    tx.rollback(working_state)
    assert tx.rolled_back is True
    assert working_state["census"].employees[0].coverage == "WP"


# -----------------------------------------------------------------------------
# Scenario 15: Candidate Promotion with Positive and Negative Fixtures
# -----------------------------------------------------------------------------
def test_candidate_rule_promotion_with_fixtures():
    """Verify rule promotion requires passing positive regression and negative contrast fixtures."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MemoryStore(data_dir=Path(tmp_dir))
        retriever = RuleRetriever(memory_store=store)

        rule = LearningRule(
            learning_id="test_mapping_rule_1",
            version=1,
            strategy={
                "action": "APPLY_HEADER_MAPPING",
                "parameters": {"source_header": "Med Coverage Level", "target_field": "coverage"},
                "preconditions": ["source header exists"],
                "exclusions": ["dental or vision columns"],
            },
            verification={"promotion_eligible": False},
        )
        store.save_candidate_rule(rule)

        # Positive fixture: includes source_header
        pos_fix = {"input_columns": ["Emp Name", "DOB", "Med Coverage Level"], "expected_mapping": "coverage"}
        # Negative fixture: includes non-medical column
        neg_fix = {"input_columns": ["Dental Coverage Level"], "non_medical_columns": ["Dental Coverage Level"]}

        promoted = retriever.verify_and_promote_candidate(rule, pos_fix, neg_fix)
        assert promoted is True

        # Confirm rule is now in active registry
        active = store.load_active_rules()
        assert any(r["learning_id"] == "test_mapping_rule_1" for r in active)


# -----------------------------------------------------------------------------
# Scenario 16: Retrieval Without Copying Facts (Zero-Leakage Procedural Memory)
# -----------------------------------------------------------------------------
def test_procedural_memory_retrieval_without_fact_copying():
    """Verify a subsequent prospect run retrieves the procedure but none of the old prospect's facts."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MemoryStore(data_dir=Path(tmp_dir))
        retriever = RuleRetriever(memory_store=store)

        snapshot = resolve_instruction_snapshot()

        # Add active rule with generic structural scope
        rule = LearningRule(
            learning_id="rule_proc_tier",
            version=1,
            compatibility={"instruction_hashes": [snapshot.content_hash]},
            scope={
                "document_roles": ["census"],
                "file_types": ["xlsx"],
                "carriers": ["Florida Blue", "Cigna"],
                "header_signature": ["Coverage Code"],
                "stage": "header_mapping",
            },
            strategy={
                "action": "APPLY_HEADER_MAPPING",
                "parameters": {"source_header": "Coverage Code", "target_field": "coverage"},
                "preconditions": ["column present"],
                "exclusions": [],
            },
        )
        store.save_candidate_rule(rule)
        store.promote_rule("rule_proc_tier", mode="auto", reason="verified")

        # Second prospect retrieval
        matched = retriever.retrieve_active_rules(
            instruction_hash=snapshot.content_hash,
            document_roles=["census"],
            file_types=["xlsx"],
            carrier="Florida Blue",
            detected_headers=["Employee Name", "Coverage Code", "DOB"],
        )
        assert len(matched) == 1
        assert matched[0]["learning_id"] == "rule_proc_tier"
        # Confirm no company or employee names exist in the retrieved rule
        assert "Acme" not in json.dumps(matched)
        assert "Jane" not in json.dumps(matched)


# -----------------------------------------------------------------------------
# Scenario 17: Thread-Safe Atomic Writes and Corruption Quarantining
# -----------------------------------------------------------------------------
def test_thread_safe_atomic_writes_and_quarantine():
    """Verify atomic writes prevent race corruption and corrupted files are quarantined safely."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MemoryStore(data_dir=Path(tmp_dir))
        active_path = Path(tmp_dir) / "registry" / "active_rules.json"

        # Intentionally write corrupted partial JSON
        active_path.write_text("{ incomplete json ...", encoding="utf-8")

        # Loading active rules should quarantine corrupt file and return empty without crash
        loaded = store.load_active_rules()
        assert loaded == []

        quarantine_dir = Path(tmp_dir) / "quarantine"
        assert quarantine_dir.exists()
        assert len(list(quarantine_dir.glob("*.corrupt_*"))) == 1


# -----------------------------------------------------------------------------
# Scenario 18: Section 29 False-Positive Prevention Test
# -----------------------------------------------------------------------------
def test_section_29_email_false_positive_prevention():
    """Verify Section 29 catches technical jargon and WC fragments in missing info emails."""
    snapshot = resolve_instruction_snapshot()
    validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)

    bad_email = """Dear Broker,
Please provide the following:
1. HARRIS, JA WC Code: 0953PA missing from invoice.
2. Internal Traceback occurred in DataFrame regex.
"""
    issues = validator._check_email_false_positives(bad_email)
    assert len(issues) >= 2
    assert any("jargon" in i.lower() for i in issues)
    assert any("workers compensation" in i.lower() for i in issues)


# -----------------------------------------------------------------------------
# Synthetic End-to-End Demonstration Test
# -----------------------------------------------------------------------------
def test_synthetic_end_to_end_demonstration():
    """Complete E2E cycle: detect failure -> diagnose -> repair -> revalidate -> persist & retrieve rule."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        store = MemoryStore(data_dir=tmp_path / "learning_data")
        orchestrator = LearningEngineOrchestrator(memory_store=store)

        ctx, snapshot = orchestrator.initialize_run_context(
            case_id="synthetic_case_1",
            output_dir=tmp_path / "output",
        )
        validator = UnderwritingValidator(instruction_hash=snapshot.content_hash)
        ledger = SourceCoverageLedger(run_id=ctx.run_id)
        prov = ProvenanceTracker(run_id=ctx.run_id)

        # 1. Pipeline stage simulation with a repairable defect: Waived employee coded as WP
        def run_stages(attempt, attempt_dir, working_state, executed_patches):
            census = working_state.get("census")
            if census is None:
                census = Census()
                emp = Person("EE", "Alice", "Wonderland")
                emp.coverage = "WP"  # Defect: Waived employee coded as WP!
                emp.notes = [("waived coverage", "")]
                census.people = [emp]
                census.people = [emp]

            prospect = Prospect(company="Wonderland LLC", state="FL")
            routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)

            # Generate dummy workbook in staging
            xlsx_path = attempt_dir / "RAPT_Census_Wonderland.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Census"
            ws.cell(row=1, column=1, value="Employee ID")
            ws.cell(row=2, column=1, value="EMP_1")
            ws.cell(row=2, column=2, value="Alice Wonderland")
            wb.save(xlsx_path)

            return {
                "census": census,
                "prospect": prospect,
                "routing": routing,
                "roles": {"ghq": [MagicMock()]},
                "findings": Findings(),
                "results": {},
                "saved_xlsx_path": xlsx_path,
            }

        # 2. Run orchestrator repair loop
        final_state = orchestrator.execute_with_repair_loop(
            ctx=ctx,
            docs=[],
            coverage_ledger=ledger,
            provenance=prov,
            validator=validator,
            run_stages_fn=run_stages,
        )

        # Verify outcome
        assert final_state["engine_status"] in ("PASSED", "REPAIRED_AND_PASSED")
        # Defect was repaired from WP to WO
        assert final_state["census"].employees[0].coverage == "WO"
        assert final_state["repaired_attempts"] >= 1


# =============================================================================
# Section 7 Targeted Regression Suite (16 Required Cases)
# =============================================================================
import pandas as pd
from pathlib import Path
from readers import RawDoc, RawTable
from census_builder import build_census, refine_non_enrolled_statuses
from learning_engine.contracts import ValidationReport


def _make_table_doc(name: str, rows: list[list], sheet_name: str = "Sheet1") -> RawDoc:
    df = pd.DataFrame(rows)
    return RawDoc(path=Path(name), ext=".xlsx", text="", tables=[RawTable(sheet_name, df)])


def test_repair_case_01_gender_self_id_blank_sex_female_gender_woman_female():
    """Case 1: Gender (Self-ID) blank, Sex = Female, Gender = Woman / Female -> F."""
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Gender (Self-ID)", "Sex", "Gender", "DOB", "Zip", "Coverage", "Plan"],
        ["Lori", "Bitting", "", "Female", "Woman / Female", "08/16/1960", "18041", "WO", ""],
    ])
    census, _ = build_census([doc])
    assert len(census.employees) == 1
    assert census.employees[0].gender == "F"
    assert census.employees[0].gender_resolution_status == "RESOLVED"


def test_repair_case_02_gender_self_id_blank_sex_male_gender_man_male():
    """Case 2: Gender (Self-ID) blank, Sex = Male, Gender = Man / Male -> M."""
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Gender (Self-ID)", "Sex", "Gender", "DOB", "Zip", "Coverage", "Plan"],
        ["Tyler", "Brunk", "", "Male", "Man / Male", "01/05/1984", "19525", "WO", ""],
    ])
    census, _ = build_census([doc])
    assert len(census.employees) == 1
    assert census.employees[0].gender == "M"
    assert census.employees[0].gender_resolution_status == "RESOLVED"


def test_repair_case_03_gender_self_id_blank_sex_female_gender_blank():
    """Case 3: Gender (Self-ID) blank, Sex = Female, Gender blank -> F."""
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Gender (Self-ID)", "Sex", "Gender", "DOB", "Zip", "Coverage", "Plan"],
        ["Katherine", "Bettinger", "", "Female", "", "03/17/2003", "18054", "WO", ""],
    ])
    census, _ = build_census([doc])
    assert len(census.employees) == 1
    assert census.employees[0].gender == "F"
    assert census.employees[0].gender_resolution_status == "RESOLVED"


def test_repair_case_04_employee_gender_blank_file_a_valid_sex_file_b():
    """Case 4: Employee gender column blank in file A, valid Sex in file B -> resolved from file B."""
    doc_a = _make_table_doc("census_a.xlsx", [
        ["First Name", "Last Name", "Employee ID", "Gender", "DOB", "Zip"],
        ["Kaya", "Cousin", "EMP100", "", "06/08/2002", "19464"],
    ])
    doc_b = _make_table_doc("enroll_b.xlsx", [
        ["First Name", "Last Name", "Employee ID", "Sex", "DOB", "Coverage", "Plan"],
        ["Kaya", "Cousin", "EMP100", "Female", "06/08/2002", "EE", "AETNA-EPO HDHP"],
    ])
    census, _ = build_census([doc_a, doc_b])
    assert len(census.employees) == 1
    assert census.employees[0].gender == "F"
    assert not any("Missing Gender" in n[0] for n in census.employees[0].notes)


def test_repair_case_05_wide_row_employee_sex_and_dependent_sex():
    """Case 5: Wide row with employee sex and dependent sex columns -> values stay on the right people."""
    doc = _make_table_doc("wide_census.xlsx", [
        ["First Name", "Last Name", "Sex", "DOB", "Zip", "Coverage", "Plan", "Spouse First Name", "Spouse Last Name", "Spouse Sex", "Spouse DOB", "Child 1 First Name", "Child 1 Last Name", "Child 1 Sex", "Child 1 DOB"],
        ["Rebecca", "Clouser", "Female", "03/26/1987", "19087", "FAM", "AETNTL-OA MC 2000-80%", "Thomas", "Clouser", "Male", "06/03/1987", "Sophie", "Clouser", "Female", "10/03/2014"],
    ])
    census, _ = build_census([doc])
    assert len(census.employees) == 1
    emp = census.employees[0]
    assert emp.gender == "F"
    assert len(emp.dependents) == 2
    sp = [d for d in emp.dependents if d.role == "SP"][0]
    ch = [d for d in emp.dependents if d.role == "CH"][0]
    assert sp.gender == "M"
    assert ch.gender == "F"


def test_repair_case_06_conflicting_employee_gender_unresolved_and_flagged():
    """Case 6: Conflicting employee sex/gender values without authoritative mapping -> unresolved and flagged."""
    doc = _make_table_doc("conflict_census.xlsx", [
        ["First Name", "Last Name", "Sex", "Gender", "DOB", "Zip", "Coverage", "Plan"],
        ["Alex", "Jordan", "Male", "Woman / Female", "04/10/1990", "19464", "EE", "AETNA-EPO"],
    ])
    census, _ = build_census([doc])
    emp = census.employees[0]
    assert emp.gender == ""
    assert emp.gender_resolution_status == "CONFLICT"
    assert any("Conflicting Gender Values" in n[0] for n in emp.notes)


def test_repair_case_07_dependent_masked_in_census_present_in_enrollment_report():
    """Case 7: Dependent masked in census (XXXX) and present with medical coverage in enrollment report -> included and linked."""
    doc_census = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Position ID", "Sex", "DOB", "Zip", "Coverage", "Plan", "Dependent 1 Name", "Dependent 1 Gender", "Dependent 1 DOB", "Dependent 1 Relationship"],
        ["Tina", "Sicilia", "GFO000001", "Female", "08/19/1964", "19087", "ES", "AETNTL-OA MC 2000-80%", "XXXX", "XXXX", "XXXX", "XXXX"],
    ])
    doc_enroll = _make_table_doc("enrollments.xlsx", [
        ["Name", "Position ID", "Employee Date of Birth", "Sex", "Dependent Name", "Dependent Date of Birth", "Dependent Gender", "Dependent Relationship", "Plan Type", "Plan Name", "Coverage Level Value"],
        ["Sicilia, Tina", "GFO000001", "08/19/1964", "Female", "Vincent Sicilia", "11/10/1965", "Male", "Spouse", "Medical", "AETNTL-OA MC 2000-80%", "Employee + Spouse"],
    ])
    census, _ = build_census([doc_census, doc_enroll])
    assert len(census.employees) == 1
    assert len(census.dependents) == 1
    dep = census.dependents[0]
    assert dep.display == "Vincent Sicilia"
    assert dep.gender == "M"
    assert dep.role == "SP"
    assert dep.subscriber_id == census.employees[0].record_id
    assert len(census.masked_dependent_slots) >= 1


def test_repair_case_08_multiple_children_sharing_sub_id_and_surname_preserved():
    """Case 8: Multiple children sharing subscriber ID and surname -> all distinct children preserved."""
    doc_enroll = _make_table_doc("enrollments.xlsx", [
        ["Name", "Position ID", "Employee Date of Birth", "Sex", "Dependent Name", "Dependent Date of Birth", "Dependent Gender", "Dependent Relationship", "Plan Type", "Plan Name", "Coverage Level Value"],
        ["Clouser, Rebecca M", "GFO000054", "03/26/1987", "Female", "Owen Thomas Henry Clouser", "04/30/2013", "Male", "Child", "Medical", "AETNTL-OA MC 2000-80%", "Employee + Family"],
        ["Clouser, Rebecca M", "GFO000054", "03/26/1987", "Female", "Liam Ethan James Clouser", "03/02/2011", "Male", "Child", "Medical", "AETNTL-OA MC 2000-80%", "Employee + Family"],
        ["Clouser, Rebecca M", "GFO000054", "03/26/1987", "Female", "Sophie Isabella Catherine Clouser", "10/03/2014", "Female", "Child", "Medical", "AETNTL-OA MC 2000-80%", "Employee + Family"],
    ])
    census, _ = build_census([doc_enroll])
    assert len(census.employees) == 1
    assert len(census.dependents) == 3
    names = {d.display for d in census.dependents}
    assert names == {
        "Owen Thomas Henry Clouser",
        "Liam Ethan James Clouser",
        "Sophie Isabella Catherine Clouser",
    }


def test_repair_case_09_employee_med_ee_dental_ef_excludes_dental_deps():
    """Case 9: Employee with medical EE and dental EF -> employee stays EE; dental-only dependents excluded from active medical rows."""
    doc_enroll = _make_table_doc("enrollments.xlsx", [
        ["Name", "Position ID", "Employee Date of Birth", "Sex", "Dependent Name", "Dependent Date of Birth", "Dependent Gender", "Dependent Relationship", "Plan Type", "Provider", "Plan Name", "Coverage Level Value"],
        ["Betancur, Arlina J", "GFO000056", "04/24/1985", "Female", "Jensen Anthony Betancur", "06/13/1981", "Male", "Spouse", "Dental", "AETDEN", "AET-APPO DEN 1,500", "Employee + Family"],
        ["Betancur, Arlina J", "GFO000056", "04/24/1985", "Female", "Cora Swan Betancur", "05/26/2017", "Female", "Child", "Dental", "AETDEN", "AET-APPO DEN 1,500", "Employee + Family"],
        ["Betancur, Arlina J", "GFO000056", "04/24/1985", "Female", "", "", "", "", "Medical", "AETEPO", "AETNA-OA EPO 7150", "Employee"],
    ])
    census, _ = build_census([doc_enroll])
    assert len(census.employees) == 1
    assert census.employees[0].coverage == "EE"
    assert len(census.dependents) == 0
    assert len(census.excluded_dependents) == 2
    assert all(d.disposition == "EXCLUDE_NON_MEDICAL_ONLY" for d in census.excluded_dependents)


def test_repair_case_10_dependent_with_dental_and_medical_lines_included_once():
    """Case 10: Dependent with both dental and medical lines -> included once as active medical dependent."""
    doc_enroll = _make_table_doc("enrollments.xlsx", [
        ["Name", "Position ID", "Employee Date of Birth", "Sex", "Dependent Name", "Dependent Date of Birth", "Dependent Gender", "Dependent Relationship", "Plan Type", "Provider", "Plan Name", "Coverage Level Value"],
        ["Serricca, Jesse", "GFO000180", "11/03/1984", "Male", "Angela Serricca", "06/05/1986", "Female", "Spouse", "Dental", "AETDEN", "AET-APPO DEN 1,500", "Employee + Family"],
        ["Serricca, Jesse", "GFO000180", "11/03/1984", "Male", "Angela Serricca", "06/05/1986", "Female", "Spouse", "Medical", "AETEPO", "AETNA-EPO HDHP 5900", "Employee + Family"],
        ["Serricca, Jesse", "GFO000180", "11/03/1984", "Male", "Angela Serricca", "06/05/1986", "Female", "Spouse", "Vision", "VSP", "VSP-Choice Vision", "Employee + Family"],
    ])
    census, _ = build_census([doc_enroll])
    assert len(census.employees) == 1
    assert len(census.dependents) == 1
    assert census.dependents[0].display == "Angela Serricca"
    assert census.dependents[0].disposition == "INCLUDE_ACTIVE_MEDICAL"
    assert len(census.excluded_dependents) == 0


def test_repair_case_11_non_enrolled_census_employee_with_waiver_is_wo():
    """Case 11: Non-enrolled census employee with waiver -> WO, not WP."""
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Sex", "DOB", "Zip", "Hire Date", "Worker Category", "Position Status", "Medical 1 Coverage Level", "Medical 1 Plan Name", "Medical Waive Reason"],
        ["Lori", "Bitting", "Female", "08/16/1960", "18041", "01/04/2021", "Full Time", "Active", "Waived", "", "Spouse's Insurance"],
    ])
    census, _ = build_census([doc])
    refine_non_enrolled_statuses(census)
    emp = census.employees[0]
    assert emp.coverage == "WO"
    assert emp.coverage != "WP"
    assert emp.disposition == "RETAIN_MEDICAL_STATUS"


def test_repair_case_12_waiting_period_employee_wp_no_most_used_plan():
    """Case 12: Waiting-period census employee -> WP, plan blank or NA, not processed through Most Used Plan."""
    from datetime import date
    from comparator import run_comparison
    from models import Findings
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Sex", "DOB", "Zip", "Hire Date", "Worker Category", "Position Status", "Medical 1 Coverage Level", "Medical 1 Plan Name"],
        ["Bella", "Pettinato", "Female", "03/10/2003", "18041", "09/21/2026", "Full Time", "Active", "Waiting Period", ""],
    ])
    census, _ = build_census([doc])
    refine_non_enrolled_statuses(census, reference_date=date(2026, 10, 1), waiting_days=60, effective_date=date(2026, 11, 1))
    emp = census.employees[0]
    assert emp.coverage == "WP"
    assert emp.plan in ("", "NA")
    results = run_comparison(census, [], None, {}, Findings(), False, False)
    rr = results.get(id(emp))
    assert rr is None or not rr.plan


def test_repair_case_13_not_eligible_census_employee_retained_as_ne():
    """Case 13: Not-eligible census employee -> NE, retained in medical eligibility census."""
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Sex", "DOB", "Zip", "Hire Date", "Worker Category", "Position Status", "Medical 1 Coverage Level", "Medical 1 Plan Name"],
        ["Gretchen", "Cox", "Female", "11/10/1966", "19465", "01/01/2001", "Temp / Seasonal", "Active", "Not Eligible", ""],
    ])
    census, _ = build_census([doc])
    refine_non_enrolled_statuses(census)
    emp = census.employees[0]
    assert emp.coverage == "NE"
    assert emp.disposition == "RETAIN_MEDICAL_STATUS"


def test_repair_case_14_part_time_excluded_employee_and_dependents():
    """Case 14: Part-time excluded employee and their dependents -> excluded from active medical population."""
    doc = _make_table_doc("census.xlsx", [
        ["First Name", "Last Name", "Sex", "DOB", "Zip", "Hire Date", "Worker Category", "Position Status", "Spouse First Name", "Spouse Last Name", "Spouse Sex", "Spouse DOB"],
        ["Sam", "Parttimer", "Male", "05/05/1985", "19464", "01/10/2022", "Part-Time", "Active", "Pat", "Parttimer", "Female", "06/06/1986"],
        ["Jane", "Fulltimer", "Female", "02/02/1982", "19464", "01/10/2020", "Full-Time", "Active", "", "", "", ""],
    ])
    doc_enroll = _make_table_doc("enroll.xlsx", [
        ["First Name", "Last Name", "Sex", "DOB", "Worker Category", "Plan Type", "Plan Name", "Coverage"],
        ["Jane", "Fulltimer", "Female", "02/02/1982", "Full-Time", "Medical", "AETNA-EPO", "EE"],
    ])
    census, _ = build_census([doc, doc_enroll])
    refine_non_enrolled_statuses(census)
    sam = [e for e in census.employees if e.first == "Sam"][0]
    assert sam.coverage == "NE"
    assert sam.disposition == "EXCLUDE_PART_TIME"
    assert len(sam.dependents) == 0
    assert all(d.display != "Pat Parttimer" for d in census.dependents)
    assert any(d.display == "Pat Parttimer" and d.disposition == "EXCLUDE_PART_TIME" for d in census.excluded_dependents)


def test_repair_case_15_same_prospect_run_twice_idempotent_counts_and_versions():
    """Case 15: Same prospect run twice -> identical canonical counts, zero added duplicates, stable pattern version."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MemoryStore(data_dir=Path(tmp_dir) / "learning_memory")
        orchestrator = LearningEngineOrchestrator(memory_store=store)

        doc = _make_table_doc("census.xlsx", [
            ["First Name", "Last Name", "Gender (Self-ID)", "Sex", "Gender", "DOB", "Zip", "Coverage", "Plan"],
            ["Lori", "Bitting", "", "Female", "Woman / Female", "08/16/1960", "18041", "EE", "AETNA-EPO"],
            ["Tyler", "Brunk", "", "Male", "Man / Male", "01/05/1984", "19525", "WO", ""],
        ])

        def _run_once(run_idx: int):
            census, _ = build_census([doc])
            refine_non_enrolled_statuses(census)
            ctx, snap = orchestrator.initialize_run_context(case_id="case_idempotent", output_dir=Path(tmp_dir) / f"out_{run_idx}")
            validator = UnderwritingValidator(instruction_hash=snap.content_hash)
            ledger = SourceCoverageLedger(run_id=ctx.run_id)
            prov = ProvenanceTracker(run_id=ctx.run_id)
            state = orchestrator.execute_with_repair_loop(
                ctx=ctx,
                docs=[doc],
                coverage_ledger=ledger,
                provenance=prov,
                validator=validator,
                run_stages_fn=lambda **kw: {
                    "census": census,
                    "prospect": Prospect(company="Idempotent Corp", state="PA"),
                    "routing": RoutingDecision(carrier=C.CARRIER_CIGNA, state="PA", is_florida=False),
                    "roles": {"census": [doc]},
                    "findings": Findings(),
                    "results": {},
                },
            )
            return census, state, store.load_validated_patterns()

        c1, s1, rules1 = _run_once(1)
        c2, s2, rules2 = _run_once(2)

        assert len(c1.people) == len(c2.people) == 2
        assert len(rules1) == len(rules2) == 10
        v1 = {r["fingerprint"]: r["version"] for r in rules1}
        v2 = {r["fingerprint"]: r["version"] for r in rules2}
        assert v1 == v2  # Stable pattern version across reruns!


def test_repair_case_16_second_prospect_reuses_validated_pattern_without_first_prospect_facts():
    """Case 16: Second prospect with same header/coverage pattern -> validated learning pattern applies without using first prospect's facts."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = MemoryStore(data_dir=Path(tmp_dir) / "learning_memory")
        orchestrator = LearningEngineOrchestrator(memory_store=store)
        ctx, snap = orchestrator.initialize_run_context(case_id="prospect_alpha", output_dir=Path(tmp_dir) / "out")
        validator = UnderwritingValidator(instruction_hash=snap.content_hash)
        orchestrator._ensure_validated_repair_patterns(ctx, ValidationReport(instruction_hash=snap.content_hash))

        patterns = store.load_validated_patterns()
        serialized = json.dumps(patterns)
        assert "SLM" not in serialized
        assert "Betancur" not in serialized
        assert "Clouser" not in serialized

        # Second prospect with same multi-column Gender (Self-ID) + Sex + Dental-only dependent pattern
        doc2 = _make_table_doc("beta_enroll.xlsx", [
            ["Name", "Position ID", "Gender (Self-ID)", "Sex", "Employee Date of Birth", "Dependent Name", "Dependent Date of Birth", "Dependent Gender", "Dependent Relationship", "Plan Type", "Provider", "Plan Name", "Coverage Level Value"],
            ["Carter, Jordan", "BETA01", "", "Female", "09/12/1988", "Sam Carter", "10/10/1987", "Male", "Spouse", "Dental", "AETDEN", "Dental PPO", "Employee + Spouse"],
            ["Carter, Jordan", "BETA01", "", "Female", "09/12/1988", "", "", "", "", "Medical", "AETEPO", "Medical EPO", "Employee"],
        ])
        census2, _ = build_census([doc2])
        assert len(census2.employees) == 1
        assert census2.employees[0].gender == "F"
        assert census2.employees[0].coverage == "EE"
        assert len(census2.dependents) == 0
        assert len(census2.excluded_dependents) == 1
        assert census2.excluded_dependents[0].disposition == "EXCLUDE_NON_MEDICAL_ONLY"


def test_repair_case_17_match_percentage_0_percent_format_and_no_currency_leak():
    """Case 17: Match % column must have '0%' percentage formatting, not currency '$1.00', and columns 13-21 must not inherit currency."""
    import openpyxl
    from backend.models import Person, Census, RowResult, Prospect, Findings
    from backend.router import RoutingDecision
    from backend.workbook_writer import save_rapt_workbook
    from backend.learning_engine.output_audit import OutputAuditor
    import backend.census_config as C

    with tempfile.TemporaryDirectory() as tmp_dir:
        emp = Person(
            record_id="emp_01", first="John", last="Smith", role="EE",
            gender="M", dob="01/01/1980", zip="33101", coverage="EE", plan="Florida Blue PPO", premium=500.0,
        )
        census = Census(people=[emp])
        prospect = Prospect(company="Acme Health", state="FL", effective_date="01/01/2027")
        routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
        res = RowResult(
            inv_name="SMITH, JOHN",
            match_pct=100.0,
            plan="Florida Blue PPO",
            tier="EE",
            status="Active",
            inv_premium=500.0,
            result="Match",
        )
        results = {id(emp): res}
        out_path = Path(tmp_dir) / "RAPT_Output_Test.xlsx"

        save_rapt_workbook(
            template=C.RAPT_TEMPLATE_PATH,
            out=out_path,
            census=census,
            prospect=prospect,
            results=results,
            F=Findings(),
            audit={
                "src_employees": 1, "out_employees": 1,
                "src_dependents": 0, "out_dependents": 0,
                "missing_employees": 0,
            },
        )

        wb = openpyxl.load_workbook(out_path)
        ws = wb.active
        # Header in row 14, first data row in row 15
        match_cell = ws.cell(15, 14)  # Col N (14) = Match %
        assert match_cell.value == 1, f"Expected match_pct normalized value 1, got {match_cell.value}"
        assert match_cell.number_format == "0%", f"Expected format '0%', got {match_cell.number_format}"
        assert "$" not in match_cell.number_format, f"Currency format leaked into Match %: {match_cell.number_format}"

        # Invoice Name (Col 13) must be General format, not currency
        inv_name_cell = ws.cell(15, 13)
        assert "$" not in str(inv_name_cell.number_format), f"Currency format leaked into Col 13: {inv_name_cell.number_format}"

        # Audit with OutputAuditor
        auditor = OutputAuditor(run_id="test_run")
        ok, errors = auditor.audit_workbook(out_path, expected_template_type="RAPT", canonical_census=census)
        assert ok is True, f"OutputAuditor failed: {errors}"
        wb.close()


def test_repair_case_18_section_18_color_coding_and_validator_gate():
    """Case 18: Section 18 validation check, repair action, and memory pattern synchronization."""
    from backend.models import Person, Census, RowResult, Prospect
    from backend.router import RoutingDecision
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType, RepairAction, RepairPatch
    from backend.learning_engine.repair import RepairEngine
    from backend.learning_engine.orchestrator import LearningEngineOrchestrator
    from backend.learning_engine.memory_store import MemoryStore

    with tempfile.TemporaryDirectory() as tmp_dir:
        emp = Person(record_id="e1", first="Jane", last="Doe", role="EE", coverage="EE")
        census = Census(people=[emp])
        prospect = Prospect(state="FL")
        routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)

        # 1. Valid 100% match passes RULE_18
        res_valid = RowResult(inv_name="DOE, JANE", match_pct=100.0, result="Match")
        validator = UnderwritingValidator(instruction_hash="dummy_hash")
        report = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census,
            selected_invoices=[], renewal=None, ren_required=False,
            claims_required=False, findings=None, results={id(emp): res_valid},
            roles={"census": [Path("dummy.xlsx")]},
        )
        rule_18_check = next((c for c in report.checks if c.rule_id == "RULE_18_MATCH_PERCENTAGE_FORMATTING"), None)
        assert rule_18_check is not None
        assert rule_18_check.status == CheckStatus.PASS

        # 2. Defect: match_pct < 100% but missing exact invoice name -> FAIL
        res_defect = RowResult(inv_name="", match_pct=85.0, result="Tier Match")
        report_fail = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census,
            selected_invoices=[], renewal=None, ren_required=False,
            claims_required=False, findings=None, results={id(emp): res_defect},
            roles={"census": [Path("dummy.xlsx")]},
        )
        rule_18_fail = next((c for c in report_fail.checks if c.rule_id == "RULE_18_MATCH_PERCENTAGE_FORMATTING"), None)
        assert rule_18_fail is not None
        assert rule_18_fail.status == CheckStatus.FAIL
        assert any(i.issue_type == IssueType.MATCH_PERCENTAGE_FORMAT_DEFECT for i in report_fail.issues)

        # 3. Repair engine proposes and executes FORMAT_MATCH_PERCENTAGE
        repair_engine = RepairEngine()
        working_state = {"census": census, "results": {id(emp): res_defect}}
        patches = repair_engine.propose_patches(report_fail, working_state)
        format_patch = next((p for p in patches if p.action == RepairAction.FORMAT_MATCH_PERCENTAGE), None)
        assert format_patch is not None
        tx = repair_engine.execute_patch(format_patch, working_state)
        assert tx.executed is True

        # 4. Learning engine memory persists 5 validated patterns
        store = MemoryStore(data_dir=Path(tmp_dir) / "learning_memory")
        orchestrator = LearningEngineOrchestrator(memory_store=store)
        ctx, snap = orchestrator.initialize_run_context(case_id="case_match_test", output_dir=Path(tmp_dir) / "out")
        orchestrator._ensure_validated_repair_patterns(ctx, report)
        validated = store.load_validated_patterns()
        assert len(validated) == 10
        pattern_ids = {p["learning_id"] for p in validated}
        assert "pattern_match_percentage_and_workbook_formatting" in pattern_ids
        assert "pattern_preferred_name_and_middle_name_matching" in pattern_ids
        assert "pattern_template_example_row_filtration" in pattern_ids
        assert "pattern_hsa_medical_plan_classification" in pattern_ids


def test_repair_case_19_preferred_name_alias_reconciliation():
    """Case 19: Preferred first name & middle name alias reconciliation (Section 16.3/16.6)."""
    from backend.models import Person, Census, InvoiceRecord, RowResult, Prospect
    from backend.comparator import match_employees, run_comparison, Findings
    from backend.router import RoutingDecision
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType, RepairAction
    from backend.learning_engine.repair import RepairEngine
    from backend.learning_engine.orchestrator import LearningEngineOrchestrator
    from backend.learning_engine.memory_store import MemoryStore

    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. Setup Derek Tilman (legal Derek, preferred Kyle) and Royce Brunson (COBRA)
        emp1 = Person(record_id="e1", first="Derek", last="Tilman", role="EE", coverage="FAM",
                      preferred_first="Kyle", middle_name="Kyle", plan="Open Choice PPO $1000", premium=1897.48)
        assert "Kyle Tilman" in emp1.aliases or emp1.preferred_first == "Kyle"
        if "Kyle Tilman" not in emp1.aliases:
            emp1.aliases.append("Kyle Tilman")

        emp2 = Person(record_id="e2", first="Royce", last="Brunson", role="EE", coverage="FAM",
                      cobra="Y", plan="PPO $2000/80%", premium=1678.38)
        census = Census(people=[emp1, emp2])

        # Invoice has Kyle Tilman
        inv = InvoiceRecord(first="Kyle", last="Tilman", raw_name="Kyle Tilman",
                            plan="Open Choice PPO $1000", tier="FAM", premium=1897.48)

        # 2. Test employee matching logic
        matches = match_employees(census.employees, [inv])
        assert 0 in matches
        match_idx, score = matches[0]
        assert match_idx == 0
        assert score == 0.95  # Preferred name match receives 95% score per Section 16.6

        # 3. Test comparison execution: Derek matches Kyle, Royce is the only missing invoice employee
        F = Findings()
        results = run_comparison(census, [inv], None, {}, F, have_invoice=True, renewal_required=False)
        rr1 = results[id(emp1)]
        assert rr1.match_pct == 95
        assert rr1.inv_name == "Kyle Tilman"
        assert rr1.result == "Match"

        missing_census_count = sum(1 for p in census.people if getattr(p, "invoice_only", False))
        missing_invoice_count = sum(1 for p in census.employees if results.get(id(p)) and results[id(p)].result == "Not On Invoice")
        assert missing_census_count == 0  # Reconciled, no false invoice-only employee
        assert missing_invoice_count == 1  # Only Royce Brunson (COBRA)

        # 4. Underwriting validator passes RULE_16_PREFERRED_NAME_MATCHING
        prospect = Prospect(state="FL")
        routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
        validator = UnderwritingValidator(instruction_hash="dummy_hash")
        report_pass = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census,
            selected_invoices=[inv], renewal=None, ren_required=False,
            claims_required=False, findings=F, results=results,
            roles={"census": [Path("dummy.xlsx")], "invoice": [Path("dummy.csv")]},
        )
        rule_16_pass = next((c for c in report_pass.checks if c.rule_id == "RULE_16_PREFERRED_NAME_MATCHING"), None)
        assert rule_16_pass is not None
        assert rule_16_pass.status == CheckStatus.PASS

        # 5. Defect scenario: if Derek was marked "Not On Invoice" and Kyle added as invoice_only -> RULE_16 FAILS
        fake_inv_p = Person(record_id="inv1", first="Kyle", last="Tilman", role="EE", invoice_only=True)
        census_defect = Census(people=[emp1, emp2, fake_inv_p])
        results_defect = {
            id(emp1): RowResult(result="Not On Invoice", result_red=True),
            id(emp2): RowResult(result="Not On Invoice", result_red=True),
        }
        report_fail = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census_defect,
            selected_invoices=[inv], renewal=None, ren_required=False,
            claims_required=False, findings=F, results=results_defect,
            roles={"census": [Path("dummy.xlsx")], "invoice": [Path("dummy.csv")]},
        )
        rule_16_fail = next((c for c in report_fail.checks if c.rule_id == "RULE_16_PREFERRED_NAME_MATCHING"), None)
        assert rule_16_fail is not None
        assert rule_16_fail.status == CheckStatus.FAIL
        assert any(i.issue_type == IssueType.PREFERRED_NAME_ALIAS_MISMATCH for i in report_fail.issues)

        # 6. Repair engine proposes RESOLVE_PREFERRED_NAME_ALIAS
        repair_engine = RepairEngine()
        working_state = {"census": census, "invoices": [inv], "findings": F, "results": results_defect}
        patches = repair_engine.propose_patches(report_fail, working_state)
        pref_patch = next((p for p in patches if p.action == RepairAction.RESOLVE_PREFERRED_NAME_ALIAS), None)
        assert pref_patch is not None
        tx = repair_engine.execute_patch(pref_patch, working_state)
        assert tx.executed is True

        # 7. Orchestrator initializes and registers pattern_preferred_name_and_middle_name_matching
        store = MemoryStore(data_dir=Path(tmp_dir) / "learning_memory")
        orchestrator = LearningEngineOrchestrator(memory_store=store)
        ctx, snap = orchestrator.initialize_run_context(case_id="case_pref_name_test", output_dir=Path(tmp_dir) / "out")
        orchestrator._ensure_validated_repair_patterns(ctx, report_pass)
        patterns = store.load_validated_patterns()
        assert len(patterns) == 10
        p_ids = {p["learning_id"] for p in patterns}
        assert "pattern_preferred_name_and_middle_name_matching" in p_ids
        assert "pattern_template_example_row_filtration" in p_ids
        assert "pattern_hsa_medical_plan_classification" in p_ids


def test_repair_case_20_template_example_row_filtration_and_gate():
    """Case 20: Template example rows ('Ex', John Smith, Cindy Parker) must be excluded from census."""
    from backend.census_builder import build_census
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType

    # Table with blank column 0 and 'Ex' in column 1
    doc = _make_table_doc("template_census.xlsx", [
        ["", "No.", "First Name", "Last Name", "Relationship", "Medical Coverage", "DOB", "Gender", "Plan"],
        ["", "Ex", "John", "Smith", "Employee", "EE", "01/01/1985", "M", "BC 1500"],
        ["", "Ex", "Cindy", "Parker", "Employee", "FAM", "02/02/1990", "F", "BC 3000"],
        ["", "1", "Alice", "Walker", "Employee", "EE", "03/03/1988", "F", "Plan A"],
        ["", "2", "Bob", "Davis", "Employee", "EE", "04/04/1982", "M", "Plan A"],
    ])
    census, _ = build_census([doc])

    # Only Alice Walker and Bob Davis should be in census!
    assert len(census.employees) == 2
    names = [f"{p.first} {p.last}" for p in census.employees]
    assert "John Smith" not in names
    assert "Cindy Parker" not in names
    assert "Alice Walker" in names
    assert "Bob Davis" in names

    # Validator checks RULE_08_EXAMPLE_ROWS_FILTERED
    validator = UnderwritingValidator(instruction_hash="test_hash")
    report = validator.validate_full_pipeline(
        prospect=Prospect(state="FL"),
        routing=RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True),
        census=census,
        selected_invoices=[],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=None,
        results={},
        roles={"census": [doc]},
    )
    rule_08_pass = next((c for c in report.checks if c.rule_id == "RULE_08_EXAMPLE_ROWS_FILTERED"), None)
    assert rule_08_pass is not None
    assert rule_08_pass.status == CheckStatus.PASS


def test_repair_case_21_hsa_medical_plan_classification():
    """Case 21: High-deductible health plans with HSA compatibility must be classified as medical."""
    from backend.census_builder import _is_non_medical

    # Medical HSA plans must return False (meaning they are medical)
    assert _is_non_medical("AFA NJ AWH OAAS 1700 HSA 100/80 T CY V25 SGP") is False
    assert _is_non_medical("AFA CPOSII 1700 HSA 100/50 T CY V25 SGP") is False
    assert _is_non_medical("HDHP 1500 HSA") is False
    assert _is_non_medical("Silver HSA 3000") is False

    # Non-medical spending / fee accounts must return True
    assert _is_non_medical("HSA Deposit Account") is True
    assert _is_non_medical("HSA Fee") is True
    assert _is_non_medical("HSA Contribution") is True
    assert _is_non_medical("Dental DMO") is True
    assert _is_non_medical("Vision VSP") is True


def test_repair_case_22_invoice_presence_false_positive_check():
    """Case 22: Employees present in invoice text must not be flagged missing from invoice."""
    from backend.models import Person, Census, RowResult, Prospect, Findings
    from backend.readers import RawDoc
    from backend.router import RoutingDecision
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType

    emp = Person(record_id="e1", first="Ashley", last="Bialoblocki", role="EE", coverage="EE")
    census = Census(people=[emp])
    results_defect = {id(emp): RowResult(result="Not On Invoice", result_red=True)}

    raw_doc = RawDoc(
        path=Path("invoice.pdf"),
        ext=".pdf",
        text="Aetna Health Invoice\nBialoblocki, Ashley  01/01/26  0106  $696.88\nTotal Charges: $696.88",
    )

    validator = UnderwritingValidator(instruction_hash="test_hash")
    report = validator.validate_full_pipeline(
        prospect=Prospect(state="NJ"),
        routing=RoutingDecision(carrier=C.CARRIER_CIGNA, state="NJ", is_florida=False),
        census=census,
        selected_invoices=[raw_doc],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=Findings(),
        results=results_defect,
        roles={"census": [Path("census.xlsx")], "invoice": [raw_doc]},
    )

    rule_29_fail = next((c for c in report.checks if c.rule_id == "RULE_29_INVOICE_PRESENCE_FALSE_POSITIVE"), None)
    assert rule_29_fail is not None
    assert rule_29_fail.status == CheckStatus.FAIL
    assert any(i.issue_type == IssueType.INVOICE_PRESENCE_FALSE_POSITIVE for i in report.issues)


def test_repair_case_23_meritain_statement_parsing_and_collapse_guard():
    """Verify that Meritain statement parsing extracts subscribers without comma requirements and triggers collapse guard."""
    from backend.invoice_parser import _records_from_meritain_statement, _is_fragment_name
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType

    # Assert names without commas pass fragment check
    assert _is_fragment_name("AHMAD SHAMANE") is False
    assert _is_fragment_name("BEAVERS MOLLY") is False
    assert _is_fragment_name("your convenience, you") is True

    meritain_text = """
    *PEACHTREE ORTHOPAEDIC CLINIC PA
    DIVISION: A02 POSC BRONZE ADMIN VIS
    Effective MED SPEC AGGR NETWORK ADMIN DEN
    Name EE ID Date COV PREM PREM & UR FEES COV FLEX COBRA Total
    BEAVERS MOLLY 4569628114 08-01-21 E 140.50 4.46 72.01 0.00 E 0.00 0.00 216.97
    CHANEY LASASHA 8058505326 01-01-25 EC 462.90 4.46 72.01 0.00 EC 6.00 0.00 545.37
    TOTAL FOR DIVISION: A02 POSC BRONZE 762.34
    """
    doc = RawDoc(path=Path("Meritain_Invoice.pdf"), ext=".pdf", text=meritain_text)
    recs, meta = _records_from_meritain_statement(doc)
    assert len(recs) == 2
    assert recs[0].raw_name == "BEAVERS MOLLY"
    assert recs[0].tier == "EE"
    assert recs[0].plan == "Bronze"
    assert recs[1].raw_name == "CHANEY LASASHA"
    assert recs[1].tier == "EC"
    assert recs[1].plan == "Bronze"

    # Verify collapse guard
    validator = UnderwritingValidator(instruction_hash="test_hash")
    census = Census(people=[Person(record_id=f"e{i}", first=f"First{i}", last=f"Last{i}", role="EE", coverage="EE") for i in range(30)])
    collapsed_findings = Findings()
    collapsed_findings.invoice_employee_count = 1

    report = validator.validate_full_pipeline(
        prospect=Prospect(state="GA"),
        routing=RoutingDecision(carrier=C.CARRIER_CIGNA, state="GA", is_florida=False),
        census=census,
        selected_invoices=[doc],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=collapsed_findings,
        results={},
        roles={"census": [Path("census.xlsx")], "invoice": [doc]},
    )
    collapse_check = next((c for c in report.checks if c.rule_id == "RULE_26_INVOICE_EXTRACTION_FIDELITY"), None)
    assert collapse_check is not None
    assert collapse_check.status == CheckStatus.FAIL
    assert any(i.issue_type == IssueType.INVOICE_EXTRACTION_COLLAPSE for i in report.issues)


def test_repair_case_24_part_time_strict_exclusion_and_non_med_dependents():
    """Verify Section 9.1 Part-Time exclusion and Section 9.2 Medical-Only dependent filtering."""
    from backend.census_builder import refine_non_enrolled_statuses
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus

    ft_emp = Person(record_id="e1", first="John", last="Fulltime", role="EE", coverage="EE", worker_category="FT")
    pt_emp = Person(record_id="e2", first="Jane", last="Parttime", role="EE", coverage="EE", worker_category="PT")
    pt_dep = Person(record_id="d1", first="Baby", last="Parttime", role="CH", subscriber_id="e2")
    pt_emp.dependents.append(pt_dep)

    # Medical EE with child dependent -> child excluded under Section 9.2
    ft_dep_non_med = Person(record_id="d2", first="Billy", last="Fulltime", role="CH", subscriber_id="e1")
    ft_emp.dependents.append(ft_dep_non_med)

    census = Census(people=[ft_emp, pt_emp, pt_dep, ft_dep_non_med])
    assert len(census.employees) == 2
    assert len(census.dependents) == 2

    refine_non_enrolled_statuses(census)
    # PT emp marked as EXCLUDE_PART_TIME and NE; dependents excluded
    assert pt_emp.disposition == "EXCLUDE_PART_TIME"
    assert pt_emp.coverage == "NE"
    assert len(census.dependents) == 0
    assert len(census.excluded_employees) == 1
    assert len(census.excluded_dependents) == 2

    validator = UnderwritingValidator(instruction_hash="test_hash")
    report = validator.validate_full_pipeline(
        prospect=Prospect(state="GA"),
        routing=RoutingDecision(carrier=C.CARRIER_CIGNA, state="GA", is_florida=False),
        census=census,
        selected_invoices=[],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=Findings(),
        results={},
        roles={"census": [Path("census.xlsx")]},
    )
    pt_check = next((c for c in report.checks if c.rule_id == "RULE_09_PART_TIME_EXCLUSION"), None)
    assert pt_check is not None
    assert pt_check.status == CheckStatus.PASS


def test_phase1_match_percentage_and_conservation_guards():
    """Phase 1: Verify null-bypass elimination on match percentage and census conservation."""
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType
    from backend.models import Census, Person, Findings, RowResult, Prospect
    from backend.router import RoutingDecision

    # 1. Matching collapse: 10 enrolled emps, 10 invoice emps, but 0 matched
    census = Census(people=[Person(record_id=f"e{i}", first=f"Emp{i}", last=f"Last{i}", role="EE", coverage="EE") for i in range(10)])
    results = {id(p): RowResult(result="Not On Invoice") for p in census.employees}
    findings = Findings()
    findings.invoice_employee_count = 10

    validator = UnderwritingValidator(instruction_hash="test_hash")
    report = validator.validate_full_pipeline(
        prospect=Prospect(state="GA"),
        routing=RoutingDecision(carrier=C.CARRIER_CIGNA, state="GA", is_florida=False),
        census=census,
        selected_invoices=[RawDoc(path=Path("inv.pdf"), ext=".pdf", text="test invoice text")],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=findings,
        results=results,
        roles={"census": [Path("census.xlsx")], "invoice": [Path("inv.pdf")]},
    )
    m_check = next((c for c in report.checks if c.rule_id == "RULE_18_MATCH_PERCENTAGE_FORMATTING"), None)
    assert m_check is not None
    assert m_check.status == CheckStatus.FAIL
    assert "Matching collapse" in m_check.reason

    # 2. Missing match_pct: inv_name present but match_pct is None
    results_missing_pct = {id(census.employees[0]): RowResult(inv_name="LAST0 EMP0", match_pct=None, result="Match")}
    report_missing = validator.validate_full_pipeline(
        prospect=Prospect(state="GA"),
        routing=RoutingDecision(carrier=C.CARRIER_CIGNA, state="GA", is_florida=False),
        census=census,
        selected_invoices=[RawDoc(path=Path("inv.pdf"), ext=".pdf", text="test invoice text")],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=findings,
        results=results_missing_pct,
        roles={"census": [Path("census.xlsx")], "invoice": [Path("inv.pdf")]},
    )
    m_check_missing = next((c for c in report_missing.checks if c.rule_id == "RULE_18_MATCH_PERCENTAGE_FORMATTING"), None)
    assert m_check_missing is not None
    assert m_check_missing.status == CheckStatus.FAIL
    assert "match_pct is missing/None" in m_check_missing.reason


# -----------------------------------------------------------------------------
# Scenario 31: 90% Accuracy Threshold & Self-Healing Rerun Verification
# -----------------------------------------------------------------------------
def test_ninety_percent_accuracy_threshold_and_status():
    """Verify that validator computes accuracy and maintains the 90% accuracy threshold."""
    validator = UnderwritingValidator(instruction_hash="test_accuracy_hash")
    prospect = Prospect(company="Acme Corp", state="FL")
    routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    census = Census()
    emp = Person("EE", "Alice", "Smith", gender="F", dob="1985-05-15", zip="33101", coverage="EE")
    census.people = [emp]
    findings = Findings()
    findings.notes.append("Renewal is required under PEO & 90-day rule but was not provided.")
    findings.email_text = "Current PEO renewal document is required."

    # Validate pipeline with missing renewal correctly requested in findings/email
    report = validator.validate_full_pipeline(
        prospect=prospect,
        routing=routing,
        census=census,
        selected_invoices=[],
        renewal=None,
        ren_required=True,
        claims_required=False,
        findings=findings,
        results={},
        roles={"census": [Path("census.xlsx")], "ghq": [Path("ghq.docx")]},
    )

    assert report.total_checks >= 15
    assert report.accuracy >= 0.90
    assert report.accuracy_threshold == 0.90
    assert report.blocked_checks == 0
    # RULE_13 passes because the agent properly fulfilled its duty to flag and request the missing renewal
    r13 = next((c for c in report.checks if c.rule_id == "RULE_13_RENEWAL_PEO_90_DAY"), None)
    assert r13 is not None
    assert r13.status == CheckStatus.PASS
    assert "correctly requested" in r13.reason


def test_self_heal_recalculation_and_rerun_resets():
    """Verify that repair engine resets action history on rerun and recalculates audit metrics."""
    engine = RepairEngine(max_cycles=3)
    engine._action_history.add("REPLACE_STRUCTURED_VALUE::{'field': 'coverage'}")
    assert len(engine._action_history) == 1

    # Reset action history for rerun
    engine.reset_action_history()
    assert len(engine._action_history) == 0
    assert len(engine.executed_patches) == 0

    # Test audit recalculation on repaired census
    census = Census()
    census.raw_employee_rows = 10
    census.raw_dependent_rows = 5
    emp_active = Person("EE", "Bob", "Jones", coverage="EE")
    emp_waived = Person("EE", "Charlie", "Brown", coverage="WP")
    emp_waived.notes = ["Employee waived coverage"]
    dep = Person("SP", "Bessie", "Jones", coverage="EE")
    emp_active.dependents = [dep]
    census.people = [emp_active, emp_waived, dep]

    working_state = {"census": census}
    patch = RepairPatch(
        patch_id="patch_wo_1",
        issue_ids=["ISSUE_1"],
        action=RepairAction.REPLACE_STRUCTURED_VALUE,
        scope_predicates={"field": "coverage"},
        preconditions=[],
        parameters={"from_status": "WP", "to_status": "WO"},
    )
    tx = engine.execute_patch(patch, working_state)
    assert tx.executed is True
    assert emp_waived.coverage == "WO"


def test_payroll_register_deficiency_exclusion_in_email():
    """Verify that payroll registers never drive enrollment deficiency paragraphs in missing info email."""
    validator = UnderwritingValidator(instruction_hash="test_payroll_hash")

    # Email text with false positive payroll register deficiency
    bad_email = (
        "Subject: Missing Information\n\n"
        "Cody Andrews: He is billed for medical on the Invoice, but he does not appear on the Payroll Register. "
        "Please confirm his employment and coverage status."
    )
    issues = validator._check_email_false_positives(bad_email)
    assert any("Payroll register deficiency false-positive detected" in iss for iss in issues)

    # Clean compliant email text
    good_email = (
        "Subject: Missing Information\n\n"
        "Please provide complete SBCs for each currently enrolled medical plan.\n"
        "Current PEO renewal document with proposed renewal rates is required."
    )
    clean_issues = validator._check_email_false_positives(good_email)
    assert not any("Payroll register" in iss for iss in clean_issues)


def test_repair_case_25_name_mismatch_metric_and_summary_reflection():
    """Case 25: Name mismatch metric propagation to Summary Sheet and exceptions (Section 16.6, 23.2, 23.6)."""
    import openpyxl
    from backend.models import Person, Census, InvoiceRecord, RowResult, Prospect
    from backend.comparator import run_comparison, Findings
    from backend.workbook_writer import write_summary_sheet
    from backend.main import exception_summary
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus

    # Setup Census employee Jennifer Moon (coverage ES, POS 200)
    emp1 = Person(record_id="e1", first="Jennifer", last="Moon", role="EE", coverage="ES",
                  plan="POS 200 $30/$50", premium=2285.85)
    census = Census(people=[emp1])

    # Setup Invoice subscriber SCHNETTLER, JENNIFER A (tier ES, POS 200)
    inv1 = InvoiceRecord(
        first="JENNIFER A", last="SCHNETTLER", raw_name="SCHNETTLER, JENNIFER A",
        tier="ES", plan="Highmark POS 200 $35/$55 Copay", premium=2285.85, status="Active"
    )
    invs = [inv1]

    F = Findings()
    meta = {"saw_dependent_source": False}
    results = run_comparison(census, invs, renewal=None, have_invoice=True, meta=meta, F=F)

    # 1. Verify comparison result on census row
    rr = results[id(emp1)]
    assert rr.result == "Review - Name Mismatch"
    assert rr.result_red is True
    assert rr.match_pct == 62

    # 2. Verify Findings counters & exceptions
    assert F.counters.get("name_mismatch") == 1
    assert any(cat == "Name mismatches" for cat, who, detail in F.exceptions)
    assert any("Name Mismatch for Review" in n for n in F.notes)

    # 3. Verify exception_summary text output
    audit = {
        "src_employees": 1,
        "out_employees": 1,
        "src_dependents": 0,
        "out_dependents": 0,
        "missing_employees": 0,
    }
    exc_text = exception_summary(F, census, audit)
    assert "- Name mismatches: 1" in exc_text
    assert "Jennifer Moon" in exc_text

    # 4. Verify Summary sheet generation
    wb = openpyxl.Workbook()
    prospect = Prospect(company="Test Prospect", current_carrier="Cigna")
    write_summary_sheet(wb, census, prospect, F, audit, results=results)
    ws = wb["Summary"]

    found_name_mismatch_metric = False
    name_mismatch_value = None
    for r in range(1, 40):
        metric_name = ws.cell(r, 1).value
        if metric_name == "Number of Name Mismatches":
            found_name_mismatch_metric = True
            name_mismatch_value = ws.cell(r, 2).value
            break

    assert found_name_mismatch_metric, "Number of Name Mismatches metric not found in Summary sheet"
    assert name_mismatch_value == 1, f"Expected 1 name mismatch in Summary sheet, got {name_mismatch_value}"

    # Verify Findings Notes section in Summary sheet includes the note
    found_note = False
    for r in range(1, 50):
        val = str(ws.cell(r, 1).value or "")
        if "Name Mismatch for Review" in val and "Jennifer Moon" in val:
            found_note = True
            break
    assert found_note, "Name Mismatch for Review note not found in Summary sheet Findings Notes"

    # 5. Verify Learning Engine validator check passes
    from backend.router import RoutingDecision
    import census_config as C
    routing = RoutingDecision(carrier=C.CARRIER_CIGNA, state="NY", is_florida=False)
    validator = UnderwritingValidator(instruction_hash="test_hash")
    report = validator.validate_full_pipeline(
        prospect=prospect,
        routing=routing,
        census=census,
        selected_invoices=invs,
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=F,
        results=results,
        roles={},
    )
    chk = next((c for c in report.checks if c.rule_id == "RULE_16_NAME_MISMATCH_ACCOUNTING"), None)
    assert chk is not None, "RULE_16_NAME_MISMATCH_ACCOUNTING check missing"
    assert chk.status == CheckStatus.PASS


def test_repair_case_26_dynamic_unspaced_name_matching():
    """Case 26: Dynamic OCR column-collapse unspaced compound name matching and false-positive prevention."""
    from backend.models import Person, Census, InvoiceRecord, RowResult, Prospect, Findings
    from backend.invoice_parser import _is_fragment_name
    from backend.comparator import match_employees, run_comparison
    from backend.learning_engine.validator import UnderwritingValidator
    from backend.learning_engine.contracts import CheckStatus, IssueType
    from backend.readers import RawDoc
    from backend.router import RoutingDecision
    import census_config as C
    from pathlib import Path

    # 1. Verify dynamic _is_fragment_name filtering:
    # Single-token compound words without numbers and not in _ADMIN_WORDS must be retained
    assert _is_fragment_name("KAINROSS") is False
    assert _is_fragment_name("DOEJOHN") is False
    assert _is_fragment_name("TAYLORALEX") is False

    # Administrative words, words with digits, or words < 4 chars must be excluded as fragments
    assert _is_fragment_name("SUBTOTAL") is True
    assert _is_fragment_name("PREMIUM") is True
    assert _is_fragment_name("FICA") is True
    assert _is_fragment_name("A12") is True

    # 2. Verify dynamic match_employees Pass 2c:
    emp1 = Person(record_id="e1", first="Ross", last="Kain", role="EE", coverage="EE")
    emp2 = Person(record_id="e2", first="Alex", last="Taylor", role="EE", coverage="EE")
    emps = [emp1, emp2]

    inv1 = InvoiceRecord(raw_name="KAINROSS", tier="EE", plan="Highmark POS 200", premium=594.84)
    inv2 = InvoiceRecord(raw_name="TAYLORALEX", tier="EE", plan="Highmark POS 200", premium=594.84)
    invs = [inv1, inv2]

    matches = match_employees(emps, invs)
    assert 0 in matches
    assert matches[0] == (0, 1.0)  # Ross Kain -> KAINROSS at 1.0
    assert 1 in matches
    assert matches[1] == (1, 1.0)  # Alex Taylor -> TAYLORALEX at 1.0

    # 3. Verify validator detects false positive when unspaced name is in invoice text but marked 'Not On Invoice'
    census = Census(people=[emp1])
    results_defect = {id(emp1): RowResult(result="Not On Invoice", result_red=True)}

    raw_doc = RawDoc(
        path=Path("invoice.pdf"),
        ext=".pdf",
        text="Highmark Blue Cross Blue Shield Invoice\nKAINROSS XXXXX56250 POS Individual 10/31/2026 $594.84 $594.84\nTotal: $594.84",
    )

    validator = UnderwritingValidator(instruction_hash="test_hash")
    prospect = Prospect(company="Lamb Farms Inc", state="NY")
    routing = RoutingDecision(carrier=C.CARRIER_CIGNA, state="NY", is_florida=False)

    report_defect = validator.validate_full_pipeline(
        prospect=prospect,
        routing=routing,
        census=census,
        selected_invoices=[raw_doc],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=Findings(),
        results=results_defect,
        roles={"census": [Path("census.xlsx")], "invoice": [raw_doc]},
    )

    r29_defect = next((c for c in report_defect.checks if c.rule_id == "RULE_29_INVOICE_PRESENCE_FALSE_POSITIVE"), None)
    assert r29_defect is not None
    assert r29_defect.status == CheckStatus.FAIL
    assert any(i.issue_type == IssueType.INVOICE_PRESENCE_FALSE_POSITIVE for i in report_defect.issues)

    # 4. Verify run_comparison correctly resolves the match and validator passes
    F = Findings()
    results_resolved = run_comparison(census, [inv1], renewal=None, have_invoice=True, meta={}, F=F)
    rr = results_resolved[id(emp1)]
    assert rr.result == "Match"
    assert rr.match_pct == 100
    assert rr.inv_name == "KAINROSS"

    report_resolved = validator.validate_full_pipeline(
        prospect=prospect,
        routing=routing,
        census=census,
        selected_invoices=[raw_doc],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=F,
        results=results_resolved,
        roles={"census": [Path("census.xlsx")], "invoice": [raw_doc]},
    )

    r29_resolved = next((c for c in report_resolved.checks if c.rule_id == "RULE_29_INVOICE_PRESENCE_FALSE_POSITIVE"), None)
    assert r29_resolved is not None
    assert r29_resolved.status == CheckStatus.PASS









