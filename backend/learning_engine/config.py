"""Configuration for the Learning Engine Agent.

Provides paths, feature toggles, repair budgets, and storage locations
for the evidence-based Learning Engine.
"""
from __future__ import annotations

import os
from pathlib import Path

# Learning Engine Base Paths
BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = BACKEND_DIR.parent

DEFAULT_DATA_DIR = BASE_DIR / "learning_engine_data"
DATA_DIR_ENV = os.getenv("LEARNING_ENGINE_DATA_DIR")
LEARNING_ENGINE_DATA_DIR = Path(DATA_DIR_ENV).resolve() if DATA_DIR_ENV else DEFAULT_DATA_DIR

# Feature Toggles
LEARNING_ENGINE_ENABLED = os.getenv("LEARNING_ENGINE_ENABLED", "true").lower() in ("true", "1", "yes")
AUTO_PROMOTE_NARROW_RULES = os.getenv("LEARNING_ENGINE_AUTO_PROMOTION", "true").lower() in ("true", "1", "yes")

# Repair Budgets
LEARNING_ENGINE_MAX_REPAIR_CYCLES = int(os.getenv("LEARNING_ENGINE_MAX_REPAIR_CYCLES", "3"))
LEARNING_ENGINE_TIME_BUDGET_SECONDS = float(os.getenv("LEARNING_ENGINE_TIME_BUDGET_SECONDS", "120.0"))
LEARNING_ENGINE_MAX_TOKENS = int(os.getenv("LEARNING_ENGINE_MAX_TOKENS", "16000"))

# Retention
CASE_RETENTION_DAYS = int(os.getenv("LEARNING_ENGINE_CASE_RETENTION_DAYS", "30"))

# Subdirectory structure
REGISTRY_DIR = LEARNING_ENGINE_DATA_DIR / "registry"
CANDIDATES_DIR = LEARNING_ENGINE_DATA_DIR / "candidates"
RULE_HISTORY_DIR = LEARNING_ENGINE_DATA_DIR / "rule_history"
RUNS_DIR = LEARNING_ENGINE_DATA_DIR / "runs"
FEEDBACK_DIR = LEARNING_ENGINE_DATA_DIR / "feedback"
REGRESSION_FIXTURES_DIR = LEARNING_ENGINE_DATA_DIR / "regression_fixtures"


def ensure_directories() -> None:
    """Ensure all durable learning storage directories exist."""
    for d in (
        LEARNING_ENGINE_DATA_DIR,
        REGISTRY_DIR,
        CANDIDATES_DIR,
        RULE_HISTORY_DIR,
        RUNS_DIR,
        FEEDBACK_DIR,
        REGRESSION_FIXTURES_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)

