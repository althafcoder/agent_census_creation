"""Anthropic Server-Side Prompt Caching Adapter.

Formats messages and system prompts to leverage Anthropic's ephemeral prompt caching
(up to 90% cost discount on cache reads and up to 80% latency reduction).
Parses usage metadata (cache_creation_input_tokens, cache_read_input_tokens) and logs telemetry.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .config import ANTHROPIC_MIN_CACHE_TOKENS, ANTHROPIC_PROMPT_CACHE_ENABLED
from .logger import log_anthropic_hit, log_anthropic_write, log_cache_miss

log = logging.getLogger("census.caching.anthropic")

# Common delimiters separating static official instructions from dynamic document text
_SPLIT_PATTERNS = [
    r"\n\n(?=SOURCE DOCUMENTS:)",
    r"\n\n(?=INVOICE DOCUMENT EXCERPTS:)",
    r"\n\n(?=RENEWAL DOCUMENT EXCERPTS:)",
    r"\n\n(?=CENSUS TEXT EXCERPTS:)",
    r"\n\n(?=Filename:)",
    r"\n\n(?=Document Text:)",
    r"\n\n(?=Extract prospect-level)",
    r"\n\n(?=Extract each subscriber)",
    r"\n\n(?=Extract medical renewal)",
    r"\n\n(?=Extract all employee)",
]


def prepare_cached_messages(
    prompt: str,
    system_prompt: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | str]:
    """Prepare messages and system parameters with Anthropic ephemeral cache_control blocks.

    If Anthropic prompt caching is disabled or the prompt is below the token threshold,
    returns standard un-cached payload structures.
    """
    if not ANTHROPIC_PROMPT_CACHE_ENABLED:
        return [{"role": "user", "content": prompt}], system_prompt

    # Rough heuristic: 1 token ~= 4 characters
    est_tokens = len(prompt) // 4
    if est_tokens < ANTHROPIC_MIN_CACHE_TOKENS:
        # Prompt is too small for Anthropic prompt caching (minimum 1024 tokens)
        return [{"role": "user", "content": prompt}], system_prompt

    # Look for boundary between large static instructions and dynamic file text
    split_index = -1
    for pat in _SPLIT_PATTERNS:
        match = re.search(pat, prompt)
        if match:
            split_index = match.start()
            break

    # If we found a clear static header block that exceeds minimum tokens
    if split_index > 0:
        static_part = prompt[:split_index].strip()
        dynamic_part = prompt[split_index:].strip()

        # If static instructions block is large enough to cache
        if (len(static_part) // 4) >= ANTHROPIC_MIN_CACHE_TOKENS:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": static_part,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": dynamic_part,
                        },
                    ],
                }
            ]
            return messages, system_prompt

    # Alternatively, if system_prompt itself contains large instructions
    if system_prompt and (len(system_prompt) // 4) >= ANTHROPIC_MIN_CACHE_TOKENS:
        system_blocks = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        return [{"role": "user", "content": prompt}], system_blocks

    # If prompt cannot be cleanly split, mark the entire prompt if within limits
    # (Anthropic allows caching single user message)
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]
    return messages, system_prompt


def record_anthropic_usage(
    usage: Any,
    step_name: str,
    latency_sec: float,
) -> dict[str, int]:
    """Inspect Anthropic response usage and record cache hits, writes, or misses.

    Returns dict of token metrics.
    """
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}

    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0

    if cache_read > 0:
        log_anthropic_hit(
            step_name=step_name,
            cache_read_tokens=cache_read,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_sec=latency_sec,
        )
    elif cache_creation > 0:
        log_anthropic_write(
            step_name=step_name,
            cache_creation_tokens=cache_creation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_sec=latency_sec,
        )
    else:
        log_cache_miss(
            step_name=step_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_sec=latency_sec,
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }
