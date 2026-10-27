"""Structured logging and telemetry tracker for Prompt Caching.

Tracks cache hits, cache misses, token savings, reuse frequency, and latency metrics.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

# Dedicated logger for caching
log = logging.getLogger("census.caching")
log.setLevel(logging.INFO)

# Ensure console streaming handler is attached so messages always appear in the terminal
if not any(isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) == sys.stdout for h in log.handlers):
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setLevel(logging.INFO)
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(_sh)

# Thread-safe metrics registry
_METRICS_LOCK = threading.Lock()
_METRICS: dict[str, Any] = {
    "total_requests": 0,
    "local_hits": 0,
    "anthropic_cache_reads": 0,
    "anthropic_cache_creations": 0,
    "cache_misses": 0,
    "total_cached_tokens_read": 0,
    "total_tokens_saved_estimate": 0,
    "total_latency_saved_seconds": 0.0,
    "start_time": time.time(),
}


def log_local_hit(
    step_name: str,
    prompt_hash: str,
    access_count: int,
    tokens_saved_estimate: int,
    latency_ms: float = 0.5,
) -> None:
    """Log an exact local cache hit (zero API calls, zero tokens consumed)."""
    with _METRICS_LOCK:
        _METRICS["total_requests"] += 1
        _METRICS["local_hits"] += 1
        _METRICS["total_tokens_saved_estimate"] += tokens_saved_estimate
        # Estimated network roundtrip saved ~2.5s per extraction
        _METRICS["total_latency_saved_seconds"] += max(2.5, (latency_ms / 1000.0))

    short_hash = prompt_hash[:10]
    log.info(
        "[CACHE HIT: LOCAL] Step: %-22s | Key: %s... | Reused: %dx | Saved: ~%d tokens | Time: %.1fms",
        step_name,
        short_hash,
        access_count,
        tokens_saved_estimate,
        latency_ms,
    )


def log_anthropic_hit(
    step_name: str,
    cache_read_tokens: int,
    input_tokens: int,
    output_tokens: int,
    latency_sec: float,
) -> None:
    """Log an Anthropic API server-side prompt cache read (90% token cost reduction, lower latency)."""
    with _METRICS_LOCK:
        _METRICS["total_requests"] += 1
        _METRICS["anthropic_cache_reads"] += 1
        _METRICS["total_cached_tokens_read"] += cache_read_tokens
        # 90% discount on cache read tokens counts toward effective token savings
        tokens_saved = int(cache_read_tokens * 0.90)
        _METRICS["total_tokens_saved_estimate"] += tokens_saved

    log.info(
        "[CACHE HIT: ANTHROPIC] Step: %-22s | Read From Cache: %d tokens (90%% savings) | Uncached Input: %d | Output: %d | Time: %.2fs",
        step_name,
        cache_read_tokens,
        input_tokens,
        output_tokens,
        latency_sec,
    )


def log_anthropic_write(
    step_name: str,
    cache_creation_tokens: int,
    input_tokens: int,
    output_tokens: int,
    latency_sec: float,
) -> None:
    """Log an Anthropic API server-side prompt cache creation (primed for subsequent reuse)."""
    with _METRICS_LOCK:
        _METRICS["total_requests"] += 1
        _METRICS["anthropic_cache_creations"] += 1

    log.info(
        "[CACHE WRITE: ANTHROPIC] Step: %-22s | Primed Cache: %d tokens | Uncached Input: %d | Output: %d | Time: %.2fs",
        step_name,
        cache_creation_tokens,
        input_tokens,
        output_tokens,
        latency_sec,
    )


def log_cache_miss(
    step_name: str,
    input_tokens: int,
    output_tokens: int,
    latency_sec: float,
) -> None:
    """Log a prompt cache miss (standard invocation without cache match)."""
    with _METRICS_LOCK:
        _METRICS["total_requests"] += 1
        _METRICS["cache_misses"] += 1

    log.info(
        "[CACHE MISS] Step: %-22s | Input: %d tokens | Output: %d tokens | Time: %.2fs",
        step_name,
        input_tokens,
        output_tokens,
        latency_sec,
    )


def get_cache_metrics() -> dict[str, Any]:
    """Retrieve snapshot of current caching metrics."""
    with _METRICS_LOCK:
        metrics = dict(_METRICS)
    
    total = metrics["total_requests"]
    hits = metrics["local_hits"] + metrics["anthropic_cache_reads"]
    hit_rate = (hits / total * 100.0) if total > 0 else 0.0
    metrics["hit_rate_pct"] = round(hit_rate, 1)
    metrics["uptime_seconds"] = round(time.time() - metrics["start_time"], 1)
    return metrics


def log_cache_summary() -> None:
    """Print a clean visual summary of prompt caching performance to the logs."""
    m = get_cache_metrics()
    log.info("================================================================================")
    log.info("                         PROMPT CACHE EFFICIENCY REPORT                         ")
    log.info("================================================================================")
    log.info(" Total Extraction Requests:        %d", m["total_requests"])
    log.info(" Local Exact Cache Hits:           %d", m["local_hits"])
    log.info(" Anthropic Ephemeral Cache Reads:  %d", m["anthropic_cache_reads"])
    log.info(" Anthropic Cache Writes (Primed):  %d", m["anthropic_cache_creations"])
    log.info(" Cache Misses (Full Cost):         %d", m["cache_misses"])
    log.info(" Combined Cache Hit Rate:          %.1f%%", m["hit_rate_pct"])
    log.info(" Estimated Tokens Saved:           ~%s tokens", f"{m['total_tokens_saved_estimate']:,}")
    log.info(" Estimated Latency Saved:          ~%.1f seconds", m["total_latency_saved_seconds"])
    log.info("================================================================================")
