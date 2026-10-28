"""Live test with configured Anthropic API key and claude-opus-5-5 model."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from llm_client import extract_json, get_instruction_doc_name, get_instruction_doc_names, get_official_instructions

def test_live_claude():
    print("Testing live Claude extraction with all three official instructions...")
    doc_names = get_instruction_doc_names()
    print("Instruction documents found:", doc_names)
    assert len(doc_names) >= 1, f"Expected at least 1 instruction document, found {len(doc_names)}: {doc_names}"
    instructions = get_official_instructions()
    doc_name = get_instruction_doc_name()
    assert len(instructions) > 50000, f"Expected > 50,000 chars, got {len(instructions)}"

    prompt = f"""You are operating strictly under the official instructions from {doc_name}:

OFFICIAL INSTRUCTIONS ({doc_name}):
{instructions}

SOURCE DOCUMENTS:
Company Name: Acme Industrial Solutions Inc.
Proposed Effective Date: 01/01/2027
SIC Code: 3599
Address: 100 Innovation Way
City, State, Zip: Austin, TX 78701

Extract prospect-level information fields for the Census Template:
Return JSON only:
{{
  "proposed_effective_date": "",
  "sic_naics_code": "",
  "company_name": "",
  "address": "",
  "city": "",
  "state": "",
  "zip": "",
  "city_state_zip": "",
  "onedigital_referral": "",
  "rei_sales_person": ""
}}
"""
    result = extract_json(prompt, max_tokens=16000, effort="high")
    print("Live Claude extraction response:")
    print(result)

    assert isinstance(result, dict), "Result must be a dict"
    assert result.get("company_name") == "Acme Industrial Solutions Inc."
    assert "2027" in str(result.get("proposed_effective_date"))
    print("\n[SUCCESS] LIVE CLAUDE EXTRACTION VERIFIED WITH REAL API KEY!")

if __name__ == "__main__":
    test_live_claude()
