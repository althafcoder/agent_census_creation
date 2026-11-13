"""Regression tests for client feedback items (dynamic, zero hardcoding)."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
import sys

import openpyxl
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import census_config as C
from census_builder import _State, _order_output
from comparator import run_comparison
from models import Census, Findings, InvoiceRecord, Person, Prospect
from router import write_missing_information_docx
from utils import std_zip
from workbook_writer import save_rapt_workbook


def test_zip_truncation_strips_plus4():
    assert std_zip("33326-2942") == ("33326", True)
    assert std_zip("333262942") == ("33326", True)
    assert std_zip("02138") == ("02138", True)


def test_census_preserves_original_order():
    st = _State()
    e1 = Person("EE", "Zoe", "Zimmer", coverage="WO", row_no=1)
    e2 = Person("EE", "Amy", "Adams", coverage="EE", plan="Plan A", row_no=2)
    e3 = Person("EE", "Bob", "Baker", coverage="WP", row_no=3)
    st.employees = [e1, e2, e3]
    _order_output(st)
    assert [p.first for p in st.census.people] == ["Zoe", "Amy", "Bob"]


def test_most_used_plan_and_summary_census_plan_name_and_rapt_output(tmp_path: Path):
    # Build synthetic census with 2 matched EEs on Plan Alpha, 1 census-only EE on Unknown Plan, and a DP dependent
    e1 = Person("EE", "Alice", "Smith", gender="F", dob=_dt.date(1985, 1, 10), zip="33150-1234",
                coverage="EE", plan="Census Plan Alpha", row_no=1)
    d1 = Person("DP", "Sam", "Smith", gender="M", dob=_dt.date(1984, 5, 20), zip=std_zip("33150-1234")[0],
                employee=e1, row_no=2)
    e1.dependents = [d1]

    e2 = Person("EE", "Brian", "Jones", gender="M", dob=_dt.date(1982, 3, 15), zip=std_zip("33326-2942")[0],
                coverage="EE", plan="Census Plan Alpha", row_no=3)

    e3 = Person("EE", "Cara", "Lee", gender="F", dob=_dt.date(1990, 7, 1), zip=std_zip("33128-1598")[0],
                coverage="EE", plan="Census Plan Beta", row_no=4)

    census = Census(people=[e1, d1, e2, e3])

    invs = [
        InvoiceRecord(raw_name="Smith, Alice", first="Alice", last="Smith",
                      plan="INV PLAN ALPHA 100", tier="EE", status="Active", premium=750.0),
        InvoiceRecord(raw_name="Jones, Brian", first="Brian", last="Jones",
                      plan="INV PLAN ALPHA 100", tier="EE", status="Active", premium=750.0),
    ]

    findings = Findings()
    results = run_comparison(census, invs, renewal=None, meta={}, F=findings, have_invoice=True, renewal_required=False)

    # Census-only active EE without invoice rate should use Most Used Plan
    rr_cara = results[id(e3)]
    assert rr_cara.used_most_used_plan is True
    assert rr_cara.plan == "INV PLAN ALPHA 100"
    assert rr_cara.inv_premium == 750.0
    assert any("Most used plan used" in n[0] for n in rr_cara.notes)
    assert findings.counters["most_used"] == 1
    assert findings.plan_enrollment[("INV PLAN ALPHA 100", "EE")] == 3
    assert findings.plan_display_names["INV PLAN ALPHA 100"] == "Census Plan Alpha"

    # Write RAPT workbook and verify outputs
    out_xlsx = tmp_path / "rapt_out.xlsx"
    audit = {
        "src_employees": 3,
        "out_employees": 3,
        "src_dependents": 1,
        "out_dependents": 1,
        "missing_employees": 0,
    }
    save_rapt_workbook(C.TEMPLATE_PATH, out_xlsx, census, Prospect(company="Test Co", state="FL"), results, findings, audit)

    wb = openpyxl.load_workbook(out_xlsx)
    ws = wb["Sheet1"]
    # Find DP row -> Col G (7) must be "SP", Col F (6) must be 5 digits
    dp_rows = [r for r in range(15, ws.max_row + 1) if ws.cell(r, 2).value == "Sam"]
    assert len(dp_rows) == 1
    assert ws.cell(dp_rows[0], 7).value == "SP"
    assert ws.cell(dp_rows[0], 6).value == "33150"

    # Find Cara Lee row -> Col O (15) & Col R (18) must be RED font
    cara_rows = [r for r in range(15, ws.max_row + 1) if ws.cell(r, 2).value == "Cara"]
    assert len(cara_rows) == 1
    cr = cara_rows[0]
    assert ws.cell(cr, 15).value == "INV PLAN ALPHA 100"
    assert ws.cell(cr, 15).font.color.rgb.endswith(C.RED)
    assert ws.cell(cr, 18).value == 750.0
    assert ws.cell(cr, 18).font.color.rgb.endswith(C.RED)

    # Summary sheet must title the Plan Level Table with the census plan name
    ws_sum = wb["Summary"]
    summary_texts = [str(ws_sum.cell(r, 1).value or "") for r in range(1, ws_sum.max_row + 1)]
    assert any("Plan Level Table - Census Plan Alpha" in t for t in summary_texts)


def test_missing_information_docx_formatting(tmp_path: Path):
    out_docx = tmp_path / "Missing_Info.docx"
    write_missing_information_docx(
        out_docx,
        "Medical Submission Missing Information - Test Co",
        [("GHQ", ["Group Health Questionnaire (GHQ) is incomplete."]), ("Census Clarification", ["Confirm tier."])],
    )
    assert out_docx.exists()
    doc = Document(str(out_docx))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert any("Subject: Medical Submission Missing Information - Test Co" in t for t in texts)
    assert "GHQ" in texts
    assert "Census Clarification" in texts
    # Subheading runs should be bold
    ghq_para = next(p for p in doc.paragraphs if p.text == "GHQ")
    assert ghq_para.runs[0].bold is True


def test_periodic_prompt_cache_auto_clear(monkeypatch):
    import threading
    import time
    import api as api_mod
    from caching import PROMPT_CACHE_CLEAR_INTERVAL_MINUTES

    assert PROMPT_CACHE_CLEAR_INTERVAL_MINUTES == 15.0

    cleared = []

    class _FakeCache:
        def clear(self):
            cleared.append(True)

    monkeypatch.setattr("caching.get_local_cache", lambda: _FakeCache())

    stop_event = threading.Event()
    t = threading.Thread(target=api_mod._periodic_prompt_cache_clear, args=(0.02, stop_event), daemon=True)
    t.start()
    time.sleep(0.08)
    stop_event.set()
    t.join(timeout=2.0)
    assert len(cleared) >= 1
