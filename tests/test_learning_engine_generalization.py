"""Tests for Fix #6: Learning Engine Generalization & Promotion Gates.

Verifies:
1. Broken / blocked / sanity-failed runs never promote rules to VALIDATED, while still recording
   a generalized shared-rule proposal (target module, function, suggested regression test) in
   ``shared_rule_backlog.json``.
2. Single-case repairs stay in CANDIDATE state instead of auto-promoting one-off per-case patches.
3. Confirming the same repair class across >= 2 distinct cases marks the shared-rule proposal
   ``ready_for_core_rule_fix`` and promotes the rule with accurate cumulative case_successes.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from learning_engine.contracts import (  # noqa: E402
    EarliestStage,
    EngineStatus,
    RepairAction,
    RepairPatch,
    RunContext,
    ValidationReport,
)
from learning_engine.memory_store import MemoryStore  # noqa: E402
from learning_engine.orchestrator import LearningEngineOrchestrator  # noqa: E402


def _make_orch(tmp_path: Path) -> LearningEngineOrchestrator:
    orch = LearningEngineOrchestrator()
    orch.memory_store = MemoryStore(data_dir=tmp_path)
    return orch


def _make_patch(action=RepairAction.RECONCILE_ABBREVIATED_NAME_SUBSCRIBER) -> RepairPatch:
    return RepairPatch(
        patch_id="p1",
        action=action,
        affected_stages=[EarliestStage.COMPARISON],
        issue_ids=["ISSUE_ABBREVIATED_NAME_UNMATCHED"],
        parameters={"action": action.value.lower()},
        preconditions=["abbreviated first name match"],
        verification_checks=["RULE_16_ABBREVIATED_NAME_MATCHING"],
    )


def _ctx(case_id: str, run_id: str) -> RunContext:
    return RunContext(
        case_id=case_id,
        run_id=run_id,
        instruction_hash="abc123",
        parser_version="2.1.0",
    )


def test_blocked_or_sanity_failed_run_never_promotes_rule(tmp_path):
    orch = _make_orch(tmp_path)
    bad_report = ValidationReport(
        instruction_hash="abc123",
        total_checks=5,
        passed_checks=4,
        failed_checks=0,
        blocked_checks=1,
        release_status=EngineStatus.BLOCKED_TECHNICAL,
    )
    orch._persist_verified_learning(
        [_make_patch()], _ctx("case_vikta_broken", "run_1"),
        validation_report=bad_report, sanity_passed=False,
    )

    assert orch.memory_store.load_active_rules() == []
    assert orch.memory_store.load_candidate_rule("learned_reconcile_abbreviated_name_subscriber") is None

    backlog = orch.memory_store.load_shared_rule_backlog()
    assert len(backlog) == 1
    assert backlog[0]["proposal_id"] == "shared_rule_reconcile_abbreviated_name_subscriber"
    assert "comparator.py" in backlog[0]["target_module"]
    assert backlog[0]["status"] == "proposed"


def test_single_case_stays_candidate_and_multi_case_promotes(tmp_path):
    orch = _make_orch(tmp_path)
    good_report = ValidationReport(
        instruction_hash="abc123",
        total_checks=5,
        passed_checks=5,
        failed_checks=0,
        blocked_checks=0,
        release_status=EngineStatus.PASSED,
    )

    # First clean case -> held as CANDIDATE, NOT promoted to active/VALIDATED
    orch._persist_verified_learning(
        [_make_patch()], _ctx("case_alpha", "run_a"),
        validation_report=good_report, sanity_passed=True,
    )
    cand1 = orch.memory_store.load_candidate_rule("learned_reconcile_abbreviated_name_subscriber")
    assert cand1 is not None
    assert cand1["pattern_state"] == "CANDIDATE"
    assert cand1["verification"]["promotion_eligible"] is False
    assert cand1["verification"]["case_successes"] == 1
    assert "shared_rule_proposal" in cand1["root_cause"]
    assert orch.memory_store.load_active_rules() == []

    # Same case repeated does NOT count as a second distinct case
    orch._persist_verified_learning(
        [_make_patch()], _ctx("case_alpha", "run_a2"),
        validation_report=good_report, sanity_passed=True,
    )
    assert orch.memory_store.load_active_rules() == []

    # Second distinct clean case -> upgrades backlog + promotes cleanly
    orch._persist_verified_learning(
        [_make_patch()], _ctx("case_beta", "run_b"),
        validation_report=good_report, sanity_passed=True,
    )
    active = orch.memory_store.load_active_rules()
    assert len(active) == 1
    assert active[0]["learning_id"] == "learned_reconcile_abbreviated_name_subscriber"
    assert active[0]["verification"]["case_successes"] == 2
    assert set(active[0]["verification"]["seen_case_ids"]) == {"case_alpha", "case_beta"}

    backlog = orch.memory_store.load_shared_rule_backlog()
    assert backlog[0]["status"] == "ready_for_core_rule_fix"
    assert set(backlog[0]["observed_case_ids"]) == {"case_alpha", "case_beta"}
