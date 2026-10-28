from pathlib import Path
import sys
import pytest

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from protected_files import (
    AccessStatus,
    CandidatePassword,
    TransientPasswordStore,
    clean_password_candidate,
    detect_file_encryption,
    discover_passwords_from_text,
    execute_access_preflight,
    unlock_office_file,
)
from readers import read_all

BW_DIR = Path(r"C:\Users\c1755\Documents\copilot_new_agent\Census_input-files\Proctected Files\FW__Company__BW_Cyber_LLC\FW__Company__BW_Cyber_LLC")


def test_clean_password_candidate():
    assert clean_password_candidate('  "BW Cyber"  ') == "BW Cyber"
    assert clean_password_candidate("BW Cyber\nTrueCompare 15% admin.") == "BW Cyber"
    assert clean_password_candidate("BW Cyber.") == "BW Cyber"
    assert clean_password_candidate("Password123") == "Password123"


def test_discover_passwords_from_text():
    sample_email = """
    FW: Company: BW Cyber LLC
    From: David Campbell <David.Campbell@decisionhr.com>
    10 attachments (2 MB)
    Employee Census.xlsx; YearToDateReport 2 - pw protected EXCEL.xlsx

    Password: BW Cyber
    TrueCompare 15% admin.
    1/1 effective. 4/1 renewal.
    """
    cands = discover_passwords_from_text(sample_email, source_name="email.pdf")
    assert len(cands) >= 1
    assert cands[0].value == "BW Cyber"


@pytest.mark.skipif(not BW_DIR.exists(), reason="BW Cyber test files folder not found")
def test_detect_file_encryption():
    emp_census = BW_DIR / "Employee Census.xlsx"
    dep_info = BW_DIR / "Dependent Information.xlsx"

    assert detect_file_encryption(emp_census) == (True, "office_crypto")
    assert detect_file_encryption(dep_info) == (False, "none")


@pytest.mark.skipif(not BW_DIR.exists(), reason="BW Cyber test files folder not found")
def test_unlock_office_file():
    emp_census = BW_DIR / "Employee Census.xlsx"

    # Wrong password
    ok, tmp, err = unlock_office_file(emp_census, "WrongPassword")
    assert not ok
    assert err == AccessStatus.PASSWORD_REJECTED.value

    # Correct password
    ok, tmp, err = unlock_office_file(emp_census, "BW Cyber")
    assert ok
    assert tmp is not None and tmp.exists()
    assert tmp.stat().st_size > 0


@pytest.mark.skipif(not BW_DIR.exists(), reason="BW Cyber test files folder not found")
def test_execute_access_preflight_end_to_end():
    docs, access_records = execute_access_preflight(BW_DIR)

    assert "Employee Census.xlsx" in access_records
    assert access_records["Employee Census.xlsx"].status == AccessStatus.UNLOCKED_AND_READ
    assert access_records["Employee Census.xlsx"].applied_password == "BW Cyber"

    assert "YearToDateReport 2 - pw protected EXCEL.xlsx" in access_records
    assert access_records["YearToDateReport 2 - pw protected EXCEL.xlsx"].status == AccessStatus.UNLOCKED_AND_READ
    assert access_records["YearToDateReport 2 - pw protected EXCEL.xlsx"].applied_password == "BW Cyber"

    census_doc = next((d for d in docs if d.name == "Employee Census.xlsx"), None)
    assert census_doc is not None
    assert census_doc.decrypted is True
    assert "BW Cyber" in census_doc.extraction_method
    assert len(census_doc.tables) >= 1
    assert census_doc.tables[0].df.shape[0] >= 9
