"""Unit test verifying Anthropic Claude LLM integration and robust parsing."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from llm_client import _clean_json_text, _parse_json_robust, extract_json
import census_config as C

def test_json_parsing():
    # Test 1: Markdown code fences
    raw1 = '```json\n{"role": "census"}\n```'
    assert _parse_json_robust(raw1) == {"role": "census"}, f"Test 1 failed: {raw1}"

    # Test 2: Thinking tags + markdown list
    raw2 = '<thinking>Processing...</thinking>\n```json\n[{"name": "John Doe"}]\n```'
    assert _parse_json_robust(raw2) == [{"name": "John Doe"}], f"Test 2 failed: {raw2}"

    # Test 3: Preamble and postscript commentary
    raw3 = 'Here is the extracted data:\n{"company_name": "Acme LLC"}\nEnd of response.'
    assert _parse_json_robust(raw3) == {"company_name": "Acme LLC"}, f"Test 3 failed: {raw3}"
    print("[PASS] _parse_json_robust tests passed.")

def test_anthropic_call_mock():
    with patch("anthropic.Anthropic") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        mock_resp = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"status": "verified", "model": "claude-3-5-sonnet-20241022"}'
        mock_resp.content = [mock_block]
        mock_instance.messages.create.return_value = mock_resp

        with patch.object(C, "ANTHROPIC_API_KEY", "sk-ant-testkey-123456"):
            with patch.object(C, "ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"):
                # Also reload or test extract_json
                from llm_client import extract_json
                with patch("llm_client.ANTHROPIC_API_KEY", "sk-ant-testkey-123456"):
                    with patch("llm_client.ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"):
                        res = extract_json("Extract prospect data")
                        assert res == {"status": "verified", "model": "claude-3-5-sonnet-20241022"}
                        mock_instance.messages.create.assert_called_once()
                        call_kwargs = mock_instance.messages.create.call_args.kwargs
                        assert call_kwargs["model"] == "claude-3-5-sonnet-20241022"
                        print("[PASS] Anthropic Claude client integration test passed.")

if __name__ == "__main__":
    test_json_parsing()
    test_anthropic_call_mock()
    print("\n--- ALL ANTHROPIC CLAUDE TESTS PASSED ---")
