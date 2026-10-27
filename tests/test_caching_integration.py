"""Integration test verifying extract_json transparent caching in backend/llm_client.py."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

import census_config as C
from caching import LocalPromptCache, get_cache_metrics, log_cache_summary
from llm_client import extract_json


def test_extract_json_caching_end_to_end():
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Isolate SQLite cache to temp directory
        import caching.local_cache as lc_mod
        test_cache = LocalPromptCache(db_dir=Path(tmp_dir), ttl_hours=24)
        lc_mod._INSTANCE = test_cache

        with patch("anthropic.Anthropic") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            mock_resp = MagicMock()
            mock_block = MagicMock()
            mock_block.type = "text"
            mock_block.text = '{"company_name": "Cached Prospect LLC", "zip": "90210"}'
            mock_resp.content = [mock_block]
            mock_resp.usage = MagicMock()
            mock_resp.usage.input_tokens = 25000
            mock_resp.usage.output_tokens = 120
            mock_resp.usage.cache_read_input_tokens = 0
            mock_resp.usage.cache_creation_input_tokens = 25000
            mock_instance.messages.create.return_value = mock_resp

            # Use a realistic live key pattern to trigger caching logic
            api_key = "sk-ant-api03-live-test-key-for-caching"
            with patch("llm_client.ANTHROPIC_API_KEY", api_key):
                with patch("llm_client.ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"):
                    prompt = "OFFICIAL INSTRUCTIONS:\n" + ("Standard Rule Definition. " * 300) + "\n\nSOURCE DOCUMENTS:\nCompany: Cached Prospect LLC"

                    # Run 1: First call -> should call API
                    res1 = extract_json(prompt)
                    assert res1 == {"company_name": "Cached Prospect LLC", "zip": "90210"}
                    assert mock_instance.messages.create.call_count == 1

                    # Run 2: Second call with same prompt -> should hit local cache without invoking API!
                    res2 = extract_json(prompt)
                    assert res2 == res1
                    assert mock_instance.messages.create.call_count == 1, "API must NOT be called on 2nd invocation (served from cache)!"

                    # Run 3: bypass_cache=True -> should invoke API again
                    res3 = extract_json(prompt, bypass_cache=True)
                    assert res3 == res1
                    assert mock_instance.messages.create.call_count == 2, "API must be called when bypass_cache=True!"

    print("[PASS] test_extract_json_caching_end_to_end passed.")


if __name__ == "__main__":
    test_extract_json_caching_end_to_end()
    print("\n--- ALL END-TO-END CACHING INTEGRATION TESTS PASSED ---")
