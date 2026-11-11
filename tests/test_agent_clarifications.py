"""Regression tests for agent clarification detection (Missing Information Email completeness).

Covers four defects observed in a live run:
  1. Non-standard waiver codes (e.g. 'QW' qualified waiver) left coverage blank.
  2. Invalid coverage values (e.g. '-1') left coverage blank and were not asked.
  3. Payroll employees absent from the census were not asked.
  4. GHQ/RFP declared full-time headcount vs census count mismatch was not asked.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from clarifications import (collect_clarifications, headcount_items, inferred_coverage_items,  # noqa: E402
                            missing_from_email, payroll_not_on_census_items, unresolved_coverage_items)
from models import Census, Person, Prospect  # noqa: E402
from prospect import _FT_LABEL_CELL_RE, _grid_label_value  # noqa: E402
from utils import coverage_code_inferred, std_coverage  # noqa: E402


def _emp(first, last, coverage="", cov_raw="", header="Health Coverage Level", inferred_from=""):
    p = Person("EE", first, last, coverage=coverage, cov_raw=cov_raw)
    p.raw_data = {"coverage_header": header}
    if inferred_from:
        p.raw_data["coverage_inferred_from"] = inferred_from
    return p


def _census():
    return Census(people=[
        _emp("Josh", "Adams", cov_raw="-1"),
        _emp("Damion", "Davidson", cov_raw="-1"),
        _emp("Kenneth", "Matrai", coverage="WO", cov_raw="QW", inferred_from="QW"),
        _emp("Richard", "St Jacques", coverage="EE", cov_raw="EE"),
        _emp("Tony", "Kappa", coverage="EE", cov_raw="EE"),
        _emp("Brian", "Weber", coverage="WO", cov_raw="QW", inferred_from="QW"),
    ])


def test_qualified_waiver_codes_map_to_waived():
    for code in ("QW", "qw", "Q-W", "SW"):
        assert std_coverage(code) == "WO", code
        assert coverage_code_inferred(code)
    # approved codes are not 'inferred'
    assert not coverage_code_inferred("WO")
    assert not coverage_code_inferred("EE")
    # invalid numeric code stays unresolved
    assert std_coverage("-1") == ""


def test_invalid_coverage_grouped_and_asked():
    items = unresolved_coverage_items(_census())
    assert len(items) == 1
    it = items[0]
    assert it.employees == ["Josh Adams", "Damion Davidson"]
    assert "Health Coverage Level is -1 (not a valid option)" in it.text
    assert "waiting period with hire date, waived, or not eligible" in it.text


def test_blank_coverage_asked():
    c = Census(people=[_emp("Jane", "Doe", cov_raw="")])
    items = unresolved_coverage_items(c)
    assert items and "left blank" in items[0].text


def test_inferred_waiver_confirmation():
    items = inferred_coverage_items(_census())
    assert len(items) == 1
    assert set(items[0].employees) == {"Kenneth Matrai", "Brian Weber"}
    assert "'QW'" in items[0].text and "Waived (WO)" in items[0].text


_LABOR_DIST = """RunDate 10/27/26 04:00PM                   PeriodStart-EndDate 10/11/26- 10/24/26                  Page 1of 24
WOODBURY,CANDACE (ID:1085)
WOODBURY,CANDACE 36.7700 735.40
ST.JACQUES,RICHARD (ID:1078)
KOPPASR.,TONYL(ID:128)
WEBER,BRIANJ(ID:1004)
MANES,DANIEL (ID:1083)
MATRAI,KENNETHM(ID:1038)
"""


def test_payroll_employees_missing_from_census():
    doc = SimpleNamespace(name="LaborDist_11226.pdf", text=_LABOR_DIST, tables=[])
    items = payroll_not_on_census_items(_census(), roles={"payroll": [doc]}, docs=[doc])
    assert len(items) == 1
    assert items[0].employees == ["Candace Woodbury", "Daniel Manes"]
    assert "10/11/2026-10/24/2026 payroll but not on the census" in items[0].text
    assert "DOB, gender, ZIP, medical election" in items[0].text


def test_payroll_employee_billed_on_invoice_not_duplicated():
    doc = SimpleNamespace(name="LaborDist.pdf", text=_LABOR_DIST, tables=[])
    inv = [SimpleNamespace(first="Daniel", last="Manes")]
    items = payroll_not_on_census_items(_census(), roles={"payroll": [doc]}, docs=[doc], invoice_records=inv)
    assert items[0].employees == ["Candace Woodbury"]


def test_headcount_mismatch():
    pr = Prospect(ft_employees=24, ft_employees_source="Company Info GHQ signed.docx")
    c = Census(people=[_emp(f"F{i}", f"L{i}", coverage="EE") for i in range(21)])
    items = headcount_items(pr, c)
    assert items and items[0].text.startswith("Headcount: the GHQ shows 24 full-time employees, but the census lists 21.")
    assert headcount_items(Prospect(ft_employees=21), c) == []


def test_ft_count_from_grid_layout():
    text = ("Federal Tax ID Number \"FEIN\": |  | State Incorporated: |  | SIC Code: | # of FT employees: |  | # of PT employees: | \n"
            "48-1239404 |  | KS |  |  | 24 |  | 0 | \n")
    doc = SimpleNamespace(name="EE Census with dep info.xlsx", text=text)
    assert _grid_label_value(_FT_LABEL_CELL_RE, [doc]) == (24, "EE Census with dep info.xlsx")


def test_omission_check_flags_missing_items():
    doc = SimpleNamespace(name="LaborDist.pdf", text=_LABOR_DIST, tables=[])
    items = collect_clarifications(Prospect(ft_employees=24), _census(), roles={"payroll": [doc]}, docs=[doc])
    cats = {i.category for i in items}
    assert cats == {"UNRESOLVED_COVERAGE", "INFERRED_COVERAGE", "PAYROLL_NOT_ON_CENSUS", "HEADCOUNT_MISMATCH"}
    assert missing_from_email(items, "Coverage tier mismatch for Richard St Jacques")
    full_email = "\n".join(i.text for i in items)
    assert missing_from_email(items, full_email) == []

