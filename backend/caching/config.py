"""Configuration settings for Prompt Caching in the Census Comparison pipeline."""
from __future__ import annotations

import os
from pathlib import Path

# Base paths
BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = BACKEND_DIR.parent

# Cache storage directory
DEFAULT_CACHE_DIR = BACKEND_DIR / "cache_storage"
PROMPT_CACHE_DIR = Path(os.getenv("PROMPT_CACHE_DIR", str(DEFAULT_CACHE_DIR)))

# Enable / Disable prompt caching globally
PROMPT_CACHE_ENABLED = os.getenv("PROMPT_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")

# Local cache TTL (in hours; 0 or negative means infinite retention)
PROMPT_CACHE_TTL_HOURS = float(os.getenv("PROMPT_CACHE_TTL_HOURS", "72"))

# Anthropic API Server-Side Prompt Caching (ephemeral 5-minute sliding cache)
ANTHROPIC_PROMPT_CACHE_ENABLED = os.getenv("ANTHROPIC_PROMPT_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")

# Minimum token count threshold to apply Anthropic cache_control block (default 1024)
ANTHROPIC_MIN_CACHE_TOKENS = int(os.getenv("ANTHROPIC_MIN_CACHE_TOKENS", "1024"))

# Verbose logging of cache statistics and token savings
LOG_CACHE_METRICS = os.getenv("LOG_CACHE_METRICS", "true").lower() in ("true", "1", "yes")


# Version constants for cache invalidation
APP_VERSION = "2.1.0"
SCHEMA_VERSION = "2026.1"
PARSER_VERSION = "2.1.0"
INSTRUCTION_VERSION = "unified_master_v1"

# Bypass local cache toggle
BYPASS_LOCAL_CACHE = os.getenv("BYPASS_LOCAL_CACHE", "false").lower() in ("true", "1", "yes")


def get_instruction_hash() -> str:
    """Return SHA-256 fingerprint of official instruction documents for cache invalidation."""
    import hashlib
    instructions_dir = BACKEND_DIR / "instructions"
    paths = sorted(list(instructions_dir.glob("*.docx")) + list(instructions_dir.glob("*.txt")))
    if not paths:
        return INSTRUCTION_VERSION
    h = hashlib.sha256()
    for p in paths:
        if not p.name.startswith("~$") and p.exists():
            try:
                h.update(p.read_bytes())
            except Exception:
                pass
    return h.hexdigest()[:16]


def get_cache_settings() -> dict:
    """Return dictionary representation of current caching settings."""
    return {
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "instruction_version": INSTRUCTION_VERSION,
        "instruction_hash": get_instruction_hash(),
        "bypass_local_cache": BYPASS_LOCAL_CACHE,
        "prompt_cache_enabled": PROMPT_CACHE_ENABLED,
        "prompt_cache_dir": str(PROMPT_CACHE_DIR),
        "prompt_cache_ttl_hours": PROMPT_CACHE_TTL_HOURS,
        "anthropic_prompt_cache_enabled": ANTHROPIC_PROMPT_CACHE_ENABLED,
        "anthropic_min_cache_tokens": ANTHROPIC_MIN_CACHE_TOKENS,
        "log_cache_metrics": LOG_CACHE_METRICS,
    }

