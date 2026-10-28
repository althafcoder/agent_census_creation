"""Persistent Learning Memory Store and Atomic Serialization.

Provides atomic, thread-safe JSON persistence for:
- Active and rejected rule registries with index and version tracking
- Candidate rules and versioned rule history
- Case-scoped run manifests, coverage, validation checkpoints, and diagnoses
- Quarantine of malformed records
- Zero-leak credential / prospect fact isolation
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from .config import (
    CANDIDATES_DIR,
    FEEDBACK_DIR,
    LEARNING_ENGINE_DATA_DIR,
    REGRESSION_FIXTURES_DIR,
    REGISTRY_DIR,
    RULE_HISTORY_DIR,
    RUNS_DIR,
    ensure_directories,
)
from .contracts import (
    LearningRule,
    RuleStatus,
    ValidationReport,
    serialize_safe,
)

log = logging.getLogger("census.learning_engine.memory_store")


class MemoryStore:
    """Thread-safe persistent store for learning engine rules, runs, and artifacts."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or LEARNING_ENGINE_DATA_DIR
        self._lock = threading.Lock()
        ensure_directories()
        self._active_registry_path = self.data_dir / "registry" / "active_rules.json"
        self._validated_patterns_path = self.data_dir / "registry" / "validated_patterns.json"
        self._rejected_registry_path = self.data_dir / "registry" / "rejected_rules.json"
        self._index_path = self.data_dir / "registry" / "index.json"
        self._ensure_registry_files()

    def _ensure_registry_files(self) -> None:
        """Create empty initial registry files if they do not exist."""
        with self._lock:
            if not self._active_registry_path.exists():
                self._atomic_write(self._active_registry_path, [])
            if not self._validated_patterns_path.exists():
                self._atomic_write(self._validated_patterns_path, [])
            if not self._rejected_registry_path.exists():
                self._atomic_write(self._rejected_registry_path, [])
            if not self._index_path.exists():
                self._atomic_write(self._index_path, {
                    "active_rule_ids": [],
                    "validated_pattern_fingerprints": [],
                    "rejected_rule_ids": [],
                    "retired_rule_ids": [],
                    "candidate_rule_ids": [],
                    "last_updated": None,
                })

    def _atomic_write(self, target_path: Path, data: Any) -> None:
        """Write JSON atomically via temporary file and replace."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(serialize_safe(data), indent=2)

        # Write to temp file in same directory for atomic rename
        temp_fd, temp_path_str = tempfile.mkstemp(
            dir=str(target_path.parent),
            prefix=".tmp_atomic_",
            suffix=".json",
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(temp_path_str, str(target_path))
        except Exception:
            if os.path.exists(temp_path_str):
                try:
                    os.remove(temp_path_str)
                except Exception:
                    pass
            raise

    def load_active_rules(self) -> list[dict[str, Any]]:
        """Load and return active rules registry."""
        with self._lock:
            if not self._active_registry_path.exists():
                return []
            try:
                content = self._active_registry_path.read_text(encoding="utf-8")
                return json.loads(content)
            except Exception as e:
                log.error("Failed loading active rules; quarantining corrupt file: %s", e)
                self._quarantine_corrupt(self._active_registry_path)
                return []

    def _quarantine_corrupt(self, file_path: Path) -> None:
        """Move corrupt file to quarantine folder to avoid crash and data loss."""
        quarantine_dir = self.data_dir / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = quarantine_dir / f"{file_path.name}.corrupt_{stamp}"
        shutil.move(str(file_path), str(target))
        log.warning("Corrupt file %s quarantined to %s", file_path.name, target)

    def save_candidate_rule(self, rule: LearningRule) -> Path:
        """Save a new or updated candidate learning rule, deduplicated by fingerprint."""
        with self._lock:
            fp = rule.fingerprint or rule.learning_id
            rule.fingerprint = fp
            cand_path = self.data_dir / "candidates" / f"{rule.learning_id}.json"

            # Check if an existing candidate/active rule with the same fingerprint exists
            existing_dict = None
            if cand_path.exists():
                try:
                    existing_dict = json.loads(cand_path.read_text(encoding="utf-8"))
                except Exception:
                    existing_dict = None

            now_iso = datetime.now().isoformat()
            if existing_dict and (existing_dict.get("fingerprint") == fp or existing_dict.get("learning_id") == rule.learning_id):
                # Keep version stable when patch_version and strategy are unchanged
                if existing_dict.get("patch_version") == rule.patch_version and existing_dict.get("strategy") == rule.strategy:
                    rule.version = int(existing_dict.get("version", 1))
                else:
                    rule.version = int(existing_dict.get("version", 1)) + 1
                rule.created_at = existing_dict.get("created_at") or now_iso
            else:
                if not rule.created_at:
                    rule.created_at = now_iso

            if rule.status not in (RuleStatus.ACTIVE, RuleStatus.VALIDATED, RuleStatus.REJECTED):
                rule.status = RuleStatus.CANDIDATE
                rule.pattern_state = "CANDIDATE"
            rule.updated_at = now_iso

            self._atomic_write(cand_path, rule.to_dict())

            # Also save to version history
            hist_dir = self.data_dir / "rule_history" / rule.learning_id
            hist_path = hist_dir / f"v{rule.version}.json"
            self._atomic_write(hist_path, rule.to_dict())

            # Update index
            self._update_index_candidate(rule.learning_id)
            return cand_path

    def promote_rule(self, learning_id: str, mode: str, reason: str) -> bool:
        """Promote candidate rule to active/VALIDATED status after passing verification, deduplicated by fingerprint."""
        with self._lock:
            cand_path = self.data_dir / "candidates" / f"{learning_id}.json"
            if not cand_path.exists():
                log.error("Candidate rule not found for promotion: %s", learning_id)
                return False

            rule_dict = json.loads(cand_path.read_text(encoding="utf-8"))
            fp = rule_dict.get("fingerprint") or learning_id
            rule_dict["fingerprint"] = fp
            rule_dict["status"] = "active"
            rule_dict["pattern_state"] = "VALIDATED"
            rule_dict["updated_at"] = datetime.now().isoformat()

            # Load existing active_rules.json
            active_list = []
            if self._active_registry_path.exists():
                try:
                    active_list = json.loads(self._active_registry_path.read_text(encoding="utf-8"))
                except Exception:
                    active_list = []

            existing_active = next(
                (r for r in active_list if r.get("learning_id") == learning_id or r.get("fingerprint") == fp),
                None,
            )
            if existing_active:
                # Preserve stable version if patch_version and strategy match
                if existing_active.get("patch_version") == rule_dict.get("patch_version") and existing_active.get("strategy") == rule_dict.get("strategy"):
                    rule_dict["version"] = int(existing_active.get("version", 1))
                rule_dict["created_at"] = existing_active.get("created_at") or rule_dict.get("created_at")
                prev_succ = int(existing_active.get("verification", {}).get("case_successes", 0))
                rule_dict.setdefault("verification", {})["case_successes"] = max(
                    prev_succ, int(rule_dict.get("verification", {}).get("case_successes", 1))
                )

            rule_dict["promotion"] = {
                "mode": mode,
                "decision_ref": f"verified_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "reason": reason,
            }

            # Deduplicate by both learning_id and fingerprint
            active_list = [
                r for r in active_list
                if r.get("learning_id") != learning_id and r.get("fingerprint") != fp
            ]
            active_list.append(rule_dict)
            self._atomic_write(self._active_registry_path, active_list)
            self._atomic_write(self._validated_patterns_path, active_list)

            # Update candidate file & version history
            self._atomic_write(cand_path, rule_dict)
            hist_dir = self.data_dir / "rule_history" / learning_id
            hist_path = hist_dir / f"v{rule_dict.get('version', 1)}.json"
            self._atomic_write(hist_path, rule_dict)

            # Update index
            self._update_index_promotion(learning_id, fp)
            log.info("Successfully promoted learning rule '%s' (fingerprint=%s) to VALIDATED/active (%s)", learning_id, fp, reason)
            return True

    def load_validated_patterns(self) -> list[dict[str, Any]]:
        """Return all validated reusable patterns."""
        return self.load_active_rules()

    def record_run_data(
        self,
        case_id: str,
        run_id: str,
        data_name: str,
        content: Any,
    ) -> Path:
        """Record case-scoped run diagnostic or checkpoint data."""
        with self._lock:
            run_dir = self.data_dir / "runs" / case_id / run_id
            target_path = run_dir / f"{data_name}.json"
            self._atomic_write(target_path, content)
            return target_path

    def _update_index_candidate(self, learning_id: str) -> None:
        idx = {}
        if self._index_path.exists():
            try:
                idx = json.loads(self._index_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        cands = set(idx.get("candidate_rule_ids", []))
        cands.add(learning_id)
        idx["candidate_rule_ids"] = sorted(list(cands))
        idx["last_updated"] = datetime.now().isoformat()
        self._atomic_write(self._index_path, idx)

    def _update_index_promotion(self, learning_id: str, fingerprint: str = "") -> None:
        idx = {}
        if self._index_path.exists():
            try:
                idx = json.loads(self._index_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        actives = set(idx.get("active_rule_ids", []))
        actives.add(learning_id)
        idx["active_rule_ids"] = sorted(list(actives))
        fps = set(idx.get("validated_pattern_fingerprints", []))
        if fingerprint:
            fps.add(fingerprint)
        idx["validated_pattern_fingerprints"] = sorted(list(fps))
        idx["last_updated"] = datetime.now().isoformat()
        self._atomic_write(self._index_path, idx)

