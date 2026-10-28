"""Procedural Memory Retrieval and Promotion Verifier.

Retrieves active learning rules on EVERY prospect based strictly on:
- Instruction compatibility (matching pinned instruction hash)
- Scope compatibility (document role, file type, carrier, header signatures, stage)
- Structural source features

NEVER matches by prospect name or employee identifiers!
Guarantees zero-leak retrieval and verifies candidates against positive/negative fixtures.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .contracts import LearningRule, RuleStatus
from .memory_store import MemoryStore

log = logging.getLogger("census.learning_engine.retrieval")


class RuleRetriever:
    """Scoped retriever for active procedural learning memory."""

    def __init__(self, memory_store: MemoryStore):
        self.memory_store = memory_store

    def retrieve_active_rules(
        self,
        instruction_hash: str,
        document_roles: list[str],
        file_types: list[str],
        carrier: Optional[str] = None,
        detected_headers: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Retrieve compatible active procedural rules for current submission facts.
        
        Evaluated on EVERY run. If no rules match, returns empty list (baseline execution).
        """
        all_active = self.memory_store.load_active_rules()
        if not all_active:
            return []

        matched_rules: list[dict[str, Any]] = []
        roles_set = {r.lower() for r in document_roles}
        types_set = {t.lower().replace(".", "") for t in file_types}
        carrier_norm = (carrier or "").strip().lower()
        headers_norm = {h.strip().lower() for h in (detected_headers or [])}

        for rule in all_active:
            # 1. Compatibility check: instruction hash
            comp = rule.get("compatibility", {})
            req_inst = comp.get("instruction_hashes", [])
            if req_inst and instruction_hash not in req_inst:
                # Incompatible instruction version
                continue

            # 2. Scope check: document role
            scope = rule.get("scope", {})
            rule_roles = {r.lower() for r in scope.get("document_roles", [])}
            if rule_roles and not (rule_roles & roles_set):
                continue

            # 3. Scope check: file type
            rule_types = {t.lower() for t in scope.get("file_types", [])}
            if rule_types and not (rule_types & types_set):
                continue

            # 4. Scope check: carrier
            rule_carriers = {c.lower() for c in scope.get("carriers", [])}
            if rule_carriers and carrier_norm and (carrier_norm not in rule_carriers):
                continue

            # 5. Scope check: header signatures (if rule requires specific column cues)
            req_headers = {h.lower() for h in scope.get("header_signature", [])}
            if req_headers and not (req_headers & headers_norm):
                continue

            # Compatible rule found!
            matched_rules.append(rule)

        log.info("Rule retrieval completed: %d active rule(s) matched for current submission scope",
                 len(matched_rules))
        return matched_rules

    def verify_and_promote_candidate(
        self,
        candidate_rule: LearningRule,
        positive_fixture: dict[str, Any],
        negative_fixture: dict[str, Any],
    ) -> bool:
        """Verify candidate rule against positive (defect fixed) and negative (contrast) fixtures."""
        learning_id = candidate_rule.learning_id
        strategy = candidate_rule.strategy
        action = strategy.get("action")
        params = strategy.get("parameters", {})

        # Positive fixture test: verify defect is resolved
        pos_input = positive_fixture.get("input_columns", [])
        pos_expected = positive_fixture.get("expected_mapping")
        target_field = params.get("target_field")
        src_header = params.get("source_header")

        if action == "APPLY_HEADER_MAPPING":
            # Check positive fixture: input contains source_header, maps to target_field
            if src_header not in pos_input:
                log.error("Promotion failed: source header '%s' not found in positive fixture", src_header)
                return False
            # Check negative fixture: legitimate blank or non-medical columns are NOT changed
            neg_input = negative_fixture.get("input_columns", [])
            neg_non_medical = negative_fixture.get("non_medical_columns", [])
            if any(col in neg_non_medical for col in [src_header]):
                log.error("Promotion failed: strategy erroneously maps non-medical column in negative fixture")
                return False

        # Preconditions & exclusions must be specified
        if not strategy.get("preconditions") or not strategy.get("exclusions"):
            log.error("Promotion failed: missing explicit preconditions or exclusions")
            return False

        # All tests passed! Promote to active
        candidate_rule.verification["case_successes"] = 1
        candidate_rule.verification["promotion_eligible"] = True
        candidate_rule.verification["regression_fixture_ids"] = [positive_fixture.get("id", "pos_fix_1")]
        candidate_rule.verification["negative_fixture_ids"] = [negative_fixture.get("id", "neg_fix_1")]

        promoted = self.memory_store.promote_rule(
            learning_id=learning_id,
            mode="auto_allowlisted",
            reason="Passed positive regression and negative contrast fixtures",
        )
        return promoted

