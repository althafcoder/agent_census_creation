"""Comprehensive Unit Tests for the Prompt Caching Subsystem."""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from caching import (
    LocalPromptCache,
    cached_extraction,
    get_cache_metrics,
    log_cache_summary,
    prepare_cached_messages,
    prompt_cache_decorator,
    record_anthropic_usage,
)


def test_local_prompt_cache_lifecycle():
    """Test LocalPromptCache store, retrieval, hit counting, and reuse tracking."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache = LocalPromptCache(db_dir=Path(tmp_dir), ttl_hours=24)

        prompt = "Extract employee census records from document..."
        model = "claude-3-5-sonnet-20241022"
        data = [{"name": "Jane Smith", "tier": "EE", "premium": 520.0}]

        # 1. Cache miss on initial query
        assert cache.get(prompt, model, step_name="test_step") is None

        # 2. Store in cache
        cache.set(prompt, model, data, step_name="test_step", tokens_saved_estimate=1200)

        # 3. Cache hit on 2nd query
        cached = cache.get(prompt, model, step_name="test_step")
        assert cached == data, f"Expected cached data match, got: {cached}"

        # 4. Verify reuse count incremented
        stats = cache.get_stats()
        assert stats["total_cached_items"] == 1
        assert stats["total_reuse_count"] == 2  # 1 initial insert + 1 get access
        assert stats["estimated_tokens_saved"] == 1200

        # 5. Verify TTL expiration
        expired_cache = LocalPromptCache(db_dir=Path(tmp_dir), ttl_hours=0.0001)  # ~0.36 seconds
        time.sleep(0.5)
        # Should expire and return None
        assert expired_cache.get(prompt, model, step_name="test_step") is None

    print("[PASS] test_local_prompt_cache_lifecycle passed.")


def test_anthropic_cache_message_builder():
    """Test automatic injection of Anthropic ephemeral cache_control blocks."""
    # Scenario 1: Short prompt below minimum threshold -> remains unchanged
    short_prompt = "Hello, extract this small text."
    msgs, sys_p = prepare_cached_messages(short_prompt, "System instructions")
    assert isinstance(msgs, list)
    assert msgs[0]["content"] == short_prompt
    assert "cache_control" not in str(msgs)

    # Scenario 2: Large prompt with official instructions exceeding 1024 tokens (~4100 chars)
    large_instructions = "OFFICIAL INSTRUCTIONS:\n" + ("Standard Census Underwriting Rule Definition. " * 200)
    document_excerpt = "SOURCE DOCUMENTS:\nEmployee: John Doe, Plan: PPO 500, Tier: EE"
    full_prompt = f"{large_instructions}\n\n{document_excerpt}"

    msgs, sys_p = prepare_cached_messages(full_prompt, "You are a census extraction engine.")
    assert isinstance(msgs, list)
    assert len(msgs) == 1
    content_blocks = msgs[0]["content"]
    assert isinstance(content_blocks, list)
    assert len(content_blocks) == 2

    # Block 1 should have cache_control ephemeral
    assert content_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "OFFICIAL INSTRUCTIONS" in content_blocks[0]["text"]

    # Block 2 should have dynamic document excerpts
    assert "SOURCE DOCUMENTS" in content_blocks[1]["text"]
    assert "cache_control" not in content_blocks[1]

    print("[PASS] test_anthropic_cache_message_builder passed.")


def test_anthropic_usage_telemetry():
    """Test telemetry recorder for Anthropic cache_read and cache_creation."""
    mock_usage_hit = MagicMock()
    mock_usage_hit.input_tokens = 50
    mock_usage_hit.output_tokens = 150
    mock_usage_hit.cache_read_input_tokens = 25000
    mock_usage_hit.cache_creation_input_tokens = 0

    res = record_anthropic_usage(mock_usage_hit, step_name="invoice_extraction", latency_sec=1.1)
    assert res["cache_read_tokens"] == 25000
    assert res["input_tokens"] == 50

    mock_usage_write = MagicMock()
    mock_usage_write.input_tokens = 50
    mock_usage_write.output_tokens = 150
    mock_usage_write.cache_read_input_tokens = 0
    mock_usage_write.cache_creation_input_tokens = 25000

    res_write = record_anthropic_usage(mock_usage_write, step_name="invoice_extraction", latency_sec=2.4)
    assert res_write["cache_creation_tokens"] == 25000

    print("[PASS] test_anthropic_usage_telemetry passed.")


def test_cached_extraction_orchestrator():
    """Test high-level cached_extraction flow and zero-disruption fallback."""
    counter = {"api_calls": 0}

    def fake_api_call():
        counter["api_calls"] += 1
        return {"company_name": "Acme Widgets", "effective_date": "01/01/2026"}

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch("caching.local_cache.PROMPT_CACHE_DIR", Path(tmp_dir)):
            # Reset cache singleton for isolation
            import caching.local_cache as lc_mod
            lc_mod._INSTANCE = LocalPromptCache(db_dir=Path(tmp_dir), ttl_hours=24)

            prompt = "Extract prospect company name and effective date from RFP email."
            model = "claude-3-5-sonnet-20241022"

            # Call 1: Misses local cache -> calls fake_api_call
            res1 = cached_extraction(prompt, model, fallback_fn=fake_api_call, step_name="prospect_test")
            assert res1 == {"company_name": "Acme Widgets", "effective_date": "01/01/2026"}
            assert counter["api_calls"] == 1

            # Call 2: Hits local cache -> returns result without calling fake_api_call
            res2 = cached_extraction(prompt, model, fallback_fn=fake_api_call, step_name="prospect_test")
            assert res2 == res1
            assert counter["api_calls"] == 1, "API call count should remain 1 due to cache reuse!"

    print("[PASS] test_cached_extraction_orchestrator passed.")


def test_cache_metrics_and_summary():
    """Test metrics aggregation and formatted summary logging."""
    metrics = get_cache_metrics()
    assert "total_requests" in metrics
    assert "hit_rate_pct" in metrics
    log_cache_summary()
    print("[PASS] test_cache_metrics_and_summary passed.")


if __name__ == "__main__":
    test_local_prompt_cache_lifecycle()
    test_anthropic_cache_message_builder()
    test_anthropic_usage_telemetry()
    test_cached_extraction_orchestrator()
    test_cache_metrics_and_summary()
    print("\n==========================================")
    print(" ALL PROMPT CACHING TESTS PASSED (100%)   ")
    print("==========================================")
