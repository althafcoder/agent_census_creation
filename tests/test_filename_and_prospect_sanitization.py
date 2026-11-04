"""Unit tests for company name cleaning, safe filename sanitization, and invoice address extraction."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from utils import clean_company_name, sanitize_filename
from prospect import build_prospect
from router import extract_prospect_state, determine_routing
from readers import RawDoc


def test_clean_company_name():
    # Test pipe-separated table tokens
    assert clean_company_name("| National Insurance Brokerage of NY Inc | | | | |") == "National Insurance Brokerage of NY Inc"
    assert clean_company_name("Acme Corp | | ") == "Acme Corp"
    assert clean_company_name("   |  Acme Corp  |  ") == "Acme Corp"
    assert clean_company_name("Acme Corp, Inc.") == "Acme Corp, Inc"
    assert clean_company_name("") == ""
    assert clean_company_name(None) == ""


def test_sanitize_filename():
    # Windows reserved characters: < > : " / \ | ? *
    bad_name = '| National Insurance Brokerage of NY Inc | | | |'
    safe = sanitize_filename(bad_name)
    assert "|" not in safe
    assert "/" not in safe
    assert "\\" not in safe
    assert ":" not in safe
    assert "*" not in safe
    assert "?" not in safe
    assert '"' not in safe
    assert "<" not in safe
    assert ">" not in safe
    assert safe == "National_Insurance_Brokerage_of_NY_Inc"

    # Test file creation with sanitized name
    test_file = Path("output") / f"test_{safe}.txt"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("ok", encoding="utf-8")
    assert test_file.exists()
    test_file.unlink()


def test_invoice_address_and_state_extraction(monkeypatch):
    import census_config
    monkeypatch.setattr(census_config, "USE_LLM_FALLBACK", False)
    invoice_text = """
================================================================================
DOCUMENT: National Insurance Invoice.PDF
TOTAL CHARACTERS: 42,091 chars
================================================================================
           NATIONAL INSURANCE BROKERAGE OF NY INC                                   
           DREW MULLER                                                              
           175 OVAL DR                                                              
           ISLANDIA NY 11749-1401                                                   

                Account Summary                 Thank you for your business.        
    """
    doc = RawDoc(path=Path("National Insurance Invoice.PDF"), ext=".pdf", text=invoice_text)
    prospect, has_source, missing = build_prospect([], [], all_docs=[doc])
    
    assert "national insurance brokerage of ny inc" in prospect.company.lower()
    assert "|" not in prospect.company
    assert prospect.state == "NY"
    assert "ISLANDIA" in (prospect.city or prospect.city_state_zip)
    assert "175 OVAL DR" in (prospect.address or "")

    st, conflicting, _ = extract_prospect_state(prospect, [doc])
    assert st == "NY"
    assert not conflicting

    routing = determine_routing(prospect, [doc])
    assert routing.state == "NY"
    assert not routing.is_florida
    assert routing.carrier == "Cigna"
