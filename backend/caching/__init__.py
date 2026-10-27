"""Prompt Caching Subsystem for Census Comparison Project.

Provides a unified two-tier caching architecture:
  1. Local Persistent SQLite Cache: Instant exact-match reuse (0 tokens, <1ms latency).
  2. Anthropic Server-Side Ephemeral Cache: Anthropic cache_control blocks for instructions (90% savings).
  3. Structured Logging & Telemetry: Real-time hit/miss and token efficiency tracking.
"""
from __future__ import annotations

from .adapter import cached_extraction, prompt_cache_decorator
from .anthropic_cache import prepare_cached_messages, record_anthropic_usage
from .config import (
    ANTHROPIC_MIN_CACHE_TOKENS,
    ANTHROPIC_PROMPT_CACHE_ENABLED,
    PROMPT_CACHE_DIR,
    PROMPT_CACHE_ENABLED,
    PROMPT_CACHE_TTL_HOURS,
    get_cache_settings,
)
from .local_cache import LocalPromptCache, get_local_cache
from .logger import (
    get_cache_metrics,
    log_anthropic_hit,
    log_anthropic_write,
    log_cache_miss,
    log_cache_summary,
    log_local_hit,
)

__all__ = [
    # Config
    "PROMPT_CACHE_ENABLED",
    "PROMPT_CACHE_DIR",
    "PROMPT_CACHE_TTL_HOURS",
    "ANTHROPIC_PROMPT_CACHE_ENABLED",
    "ANTHROPIC_MIN_CACHE_TOKENS",
    "get_cache_settings",
    # Local cache
    "LocalPromptCache",
    "get_local_cache",
    # Anthropic adapter
    "prepare_cached_messages",
    "record_anthropic_usage",
    # Orchestrator & Decorator
    "cached_extraction",
    "prompt_cache_decorator",
    # Telemetry & Logging
    "log_local_hit",
    "log_anthropic_hit",
    "log_anthropic_write",
    "log_cache_miss",
    "log_cache_summary",
    "get_cache_metrics",
]
