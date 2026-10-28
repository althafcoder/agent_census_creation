"""Unit test verifying Anthropic Claude LLM integration and robust parsing."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from llm_client import (
    DETERMINISTIC_SYSTEM_PROMPT,
    PROSPECT_OUTPUT_SCHEMA,
    RECONCILIATION_OUTPUT_SCHEMA,
    _clean_json_text,
    _parse_json_robust,
    _validate_census_rows,
    _validate_invoice_rows,
    _validate_prospect_dict,
    _validate_reconciliation_output,
    _validate_renewal_rates,
    extract_json,
    reconcile_records,
    validate_structured_output,
)
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
                        assert call_kwargs.get("extra_body", {}).get("temperature") == 1.0 or call_kwargs.get("temperature") == 1.0
                        print("[PASS] Anthropic Claude client integration test passed.")


def test_opus_5_5_deterministic_effort_config():
    """Verify Opus 5.5 omits temperature, sets max_tokens=16000, output_config={'effort': 'high'}, and uses deterministic system rules."""
    with patch("anthropic.Anthropic") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        mock_resp = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"status": "deterministic", "model": "claude-opus-5-5"}'
        mock_resp.content = [mock_block]
        mock_instance.messages.create.return_value = mock_resp

        with patch("llm_client.ANTHROPIC_API_KEY", "sk-ant-testkey-123456"):
            with patch("llm_client.ANTHROPIC_MODEL", "claude-opus-5-5"):
                res = extract_json("Process the supplied document data.")
                assert res == {"status": "deterministic", "model": "claude-opus-5-5"}
                mock_instance.messages.create.assert_called_once()
                call_kwargs = mock_instance.messages.create.call_args.kwargs
                assert call_kwargs["model"] == "claude-opus-5-5"
                assert call_kwargs["max_tokens"] == 16000
                assert call_kwargs["output_config"] == {"effort": "high"}
                assert "temperature" not in call_kwargs
                assert "extra_body" not in call_kwargs
                assert "You are a deterministic document processing agent." in str(call_kwargs["system"])
                assert "1. Extract only information supported by source documents." in str(call_kwargs["system"])
                assert "7. Return only the requested output structure." in str(call_kwargs["system"])
                print("[PASS] Opus 5.5 deterministic effort configuration test passed.")


def test_structured_outputs_and_deterministic_validation():
    """Verify structured output schema enforcement and deterministic post-extraction validation."""
    with patch("anthropic.Anthropic") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        mock_resp = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"company_name": "Acme Corp", "sic_naics_code": "N/A", "state": "FL"}'
        mock_resp.content = [mock_block]
        mock_instance.messages.create.return_value = mock_resp

        with patch("llm_client.ANTHROPIC_API_KEY", "sk-ant-testkey-123456"):
            with patch("llm_client.ANTHROPIC_MODEL", "claude-opus-5-5"):
                res = extract_json(
                    "Process the supplied document data.",
                    max_tokens=16000,
                    effort="high",
                    output_schema=PROSPECT_OUTPUT_SCHEMA,
                    use_structured_output=True,
                )
                call_kwargs = mock_instance.messages.create.call_args.kwargs
                assert "temperature" not in call_kwargs
                assert call_kwargs["output_config"]["effort"] == "high"
                assert call_kwargs["output_config"]["format"] == {
                    "type": "json_schema",
                    "schema": PROSPECT_OUTPUT_SCHEMA,
                }
                validated = _validate_prospect_dict(res)
                assert validated["company_name"] == "Acme Corp"
                # Invented placeholder "N/A" must be deterministically stripped to ""
                assert validated["sic_naics_code"] == ""
                assert validated["state"] == "FL"
                print("[PASS] Structured output and deterministic validation test passed.")


def test_complex_reconciliation_high_effort_conflict_flagging():
    """Verify complex reconciliation starts with effort='high', omits temperature, and flags conflicts without silent resolution."""
    with patch("anthropic.Anthropic") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        mock_resp = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = (
            '{"matched_records": [{"census_name": "John Smith", "invoice_name": "SMITH, JOHN", '
            '"census_tier": "Employee Only", "invoice_tier": "Family", "plan": "PPO 500", '
            '"invoice_premium": "$1,250.00", "status": "Active", "conflicts": [], "ambiguous": false}], '
            '"unmatched_census": ["Jane Doe"], "unmatched_invoice": [], "flagged_conflicts": []}'
        )
        mock_resp.content = [mock_block]
        mock_instance.messages.create.return_value = mock_resp

        with patch("llm_client.ANTHROPIC_API_KEY", "sk-ant-testkey-123456"):
            with patch("llm_client.ANTHROPIC_MODEL", "claude-opus-5-5"):
                rec = reconcile_records(
                    census_records=[{"first_name": "John", "last_name": "Smith", "coverage": "EE"}],
                    invoice_records=[{"name": "SMITH, JOHN", "tier": "FAM", "plan": "PPO 500", "premium": 1250.0}],
                )
                call_kwargs = mock_instance.messages.create.call_args.kwargs
                assert call_kwargs["model"] == "claude-opus-5-5"
                assert call_kwargs["max_tokens"] == 16000
                assert call_kwargs["output_config"]["effort"] == "high"
                assert "temperature" not in call_kwargs

                # Verify deterministic conflict flagging: EE vs FAM must never be silently resolved
                assert len(rec["matched_records"]) == 1
                m = rec["matched_records"][0]
                assert m["census_tier"] == "EE"
                assert m["invoice_tier"] == "FAM"
                assert m["ambiguous"] is True
                assert any("Tier conflict" in c for c in m["conflicts"])
                assert any("Tier conflict" in c for c in rec["flagged_conflicts"])
                print("[PASS] Complex reconciliation high-effort & conflict flagging test passed.")


if __name__ == "__main__":
    test_json_parsing()
    test_anthropic_call_mock()
    test_opus_5_5_deterministic_effort_config()
    test_structured_outputs_and_deterministic_validation()
    test_complex_reconciliation_high_effort_conflict_flagging()
    print("\n--- ALL ANTHROPIC CLAUDE TESTS PASSED ---")
