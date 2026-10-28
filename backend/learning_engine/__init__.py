"""Learning Engine Agent Package.

Provides evidence-based continuous learning, multi-stage underwriting validation,
bounded repair, and procedural memory retrieval for the Census Comparison pipeline.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import (
    LEARNING_ENGINE_ENABLED,
    LEARNING_ENGINE_DATA_DIR,
    LEARNING_ENGINE_MAX_REPAIR_CYCLES,
)


def ensure_learning_logger() -> logging.Logger:
    """Ensure census.learning_engine logs to both console/backend stdout and learning_engine_data/learning_engine.log."""
    le_logger = logging.getLogger("census.learning_engine")
    le_logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Persistent file handler in learning_engine_data/learning_engine.log
    try:
        LEARNING_ENGINE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LEARNING_ENGINE_DATA_DIR / "learning_engine.log"
        has_file_handler = any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", "").lower() == str(log_file.resolve()).lower()
            for h in le_logger.handlers
        )
        if not has_file_handler:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.INFO)
            fh.setFormatter(fmt)
            le_logger.addHandler(fh)
    except Exception:
        pass

    # 2. Ensure census parent logger has a console handler when invoked outside CLI/API
    census_logger = logging.getLogger("census")
    census_logger.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in census_logger.handlers) or any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root_logger.handlers
    )
    if not has_console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter(fmt="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
        census_logger.addHandler(sh)

    return le_logger


ensure_learning_logger()
from .contracts import (
    CheckResult,
    CheckStatus,
    DocumentManifest,
    EarliestStage,
    EngineStatus,
    FieldEvidence,
    FieldStatus,
    Issue,
    IssueSeverity,
    IssueType,
    LearningRule,
    RepairAction,
    RepairPatch,
    RuleStatus,
    RunContext,
    SourceRef,
    ValidationReport,
)
from .instruction_registry import (
    InstructionSnapshot,
    RuleSpec,
    build_instruction_registry,
    resolve_instruction_snapshot,
    verify_instruction_snapshot,
)
from .memory_store import MemoryStore
from .orchestrator import LearningEngineOrchestrator
from .output_audit import OutputAuditor
from .provenance import ProvenanceTracker, sanitize_secrets
from .repair import RepairEngine, RepairTransaction
from .retrieval import RuleRetriever
from .source_coverage import SourceCoverageLedger, TableChunk, chunk_dataframe, chunk_text
from .validator import UnderwritingValidator

__all__ = [
    "LEARNING_ENGINE_ENABLED",
    "LEARNING_ENGINE_DATA_DIR",
    "LEARNING_ENGINE_MAX_REPAIR_CYCLES",
    "CheckResult",
    "CheckStatus",
    "DocumentManifest",
    "EarliestStage",
    "EngineStatus",
    "FieldEvidence",
    "FieldStatus",
    "Issue",
    "IssueSeverity",
    "IssueType",
    "LearningRule",
    "RepairAction",
    "RepairPatch",
    "RuleStatus",
    "RunContext",
    "SourceRef",
    "ValidationReport",
    "InstructionSnapshot",
    "RuleSpec",
    "build_instruction_registry",
    "resolve_instruction_snapshot",
    "verify_instruction_snapshot",
    "MemoryStore",
    "LearningEngineOrchestrator",
    "OutputAuditor",
    "ProvenanceTracker",
    "sanitize_secrets",
    "RepairEngine",
    "RepairTransaction",
    "RuleRetriever",
    "SourceCoverageLedger",
    "TableChunk",
    "chunk_dataframe",
    "chunk_text",
    "UnderwritingValidator",
]

