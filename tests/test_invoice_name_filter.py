"""Regression tests: invoice text fallback must not turn billing/vendor phrases into employees."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from readers import RawDoc
from invoice_parser import _records_from_text


def _records(text: str):
    doc = RawDoc(path=Path("invoice.txt"), ext=".txt", text=text)
    return _records_from_text(doc)


def test_billing_phrases_are_not_employees():
    text = (
        "                PaychexHRPEO                                  0.00\n"
        "                PEOBenefitsAdministration                                   3,710.77\n"
        "               AutomaticPayment                                              341.11\n"
    )
    assert _records(text) == [], [r.raw_name for r in _records(text)]


def test_real_concatenated_name_is_preserved():
    text = (
        "      FreundlichBeth            October,2026   Oxford Liberty Gold EPO 30/60/1250"
        "            Employee                  $1,398.63\n"
    )
    names = {(r.first, r.last) for r in _records(text)}
    assert ("Beth", "Freundlich") in names, names


def test_explicit_last_first_name_is_preserved():
    names = {(r.first, r.last) for r in _records("Smith, John A  EE  $512.40\n")}
    assert ("John", "Smith") in names, names


if __name__ == "__main__":
    test_billing_phrases_are_not_employees()
    test_real_concatenated_name_is_preserved()
    test_explicit_last_first_name_is_preserved()
    print("--- ALL INVOICE NAME FILTER TESTS PASSED ---")
