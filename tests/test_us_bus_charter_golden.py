"""Golden regression test suite for the US Bus Charter & Limo, Inc. (DBA US Coachways) submission.

Verifies every item from the client feedback:
1. Census Population & Employee Count:
   - Only the 21 submitted US Bus Charter employees (20 active + 1 COBRA) and 9 dependents are included.
   - Unrelated EXAMPLE sheet employees/dependents (Bob Smith, Jane Doe, Bruce Wayne, Peter Parker, etc.),
     conversion tabs (SAIL, Generic Conv, CRQS Census), and template header rows (*Legal Last Name) are excluded.
   - Kustom Application 24 FT / 3 PT is flagged as a headcount discrepancy rather than added to the census.
2. Invalid Dependent Records:
   - Edward JR Telmany, Mark Telmany, and Michael Yehia are never created as their own dependents;
     only their actual dependents (4, 2, and 1 respectively) are linked.
3. COBRA Status:
   - Maria Composto is retained as COBRA = Y ('Yes') based on Status/Category = COBRA.
4. SIC Code:
   - Extracted as 4142 from the source renewal proposal (never overridden by an LLM guess like 2022).
5. Kim Calabrese Gender:
   - Retained as F (never blanked by lookup-legend columns in conversion tabs).
6. DOB Discrepancies:
   - Flags source DOB discrepancies between census and renewal proposal for Joseph Heap (03/24/1988 vs 03/24/1998)
     and Anthony D'Angelo's dependent child Anthony D'Angelo (08/15/2018 vs 08/15/2008).
7. Invoice Comparison:
   - Matches all 14 billed employees on the March invoice against the valid census population with zero
     missing-from-invoice discrepancies for unrelated employees.
8. Invoice Validity:
   - Flags that the invoice dated 02/10/2026 with coverage 03/01/2026-03/31/2026 is >90 days old and an
     updated invoice is required.
9. Missing Information Email:
   - Contains zero questions for unrelated employees and includes all genuine discrepancies.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from census_builder import build_census  # noqa: E402
from clarifications import collect_clarifications, dob_discrepancy_items, headcount_items, invoice_recency_items  # noqa: E402
from comparator import run_comparison  # noqa: E402
from learning_engine.contracts import CheckStatus, EngineStatus  # noqa: E402
from learning_engine.validator import UnderwritingValidator  # noqa: E402
from models import Findings, InvoiceDoc, InvoiceRecord  # noqa: E402
from prospect import build_prospect  # noqa: E402
from readers import RawDoc, RawTable  # noqa: E402
from router import determine_routing, generate_missing_information_email  # noqa: E402


def _build_us_bus_census_doc() -> RawDoc:
    """Construct the multi-sheet 'UPDATED US Coachways PEO Census .xlsx' with all 5 conflicting sheets."""
    # Sheet 1: SAIL (conversion export with 'Sub' relationship and lookup legend)
    sail_df = pd.DataFrame([
        ["FirstName", "LastName", "Gender", "DOB", "ZipCode", "Relationship", "CoverageType", "COBRA", "EntitySourceID", "CarrierID"],
        ["Kim", "Calabrese", "F", "1971-01-09", "10308", "Sub", "EE", "N", "vensure", "aetna"],
    ])

    # Sheet 2: EXAMPLE (template example rows that polluted the prior run with 24 fake records)
    example_df = pd.DataFrame([
        ["Sequence No", "Relationship to Employee (Employee, Spouse, Child)", "Last Name", "First Name", "Gender",
         "Birth Date (MM/DD/YYYY)", "Home Zip Code", "Active Employee, Dependent, or COBRA",
         "Medical Coverage Tier", "Name of Medical Plan Currently Enrolled in", "Full-Time or Part-Time"],
        ["", "Employee", "Smith", "Bob", "Male", "1965-05-29", "33411", "Active", "Family", "Plan ABC", "Full-Time"],
        ["", "Spouse", "Smith", "Dan", "Male", "1991-08-01", "33411", "Dependent", "Family", "", "Dependent"],
        ["", "Child", "Smith", "Danielle", "Female", "1993-09-02", "33411", "Dependent", "Family", "", "Dependent"],
        ["", "Employee", "Jones", "Lila", "Female", "1978-05-06", "33418", "Active", "No Coverage", "", "Full-Time"],
        ["", "Employee", "Johnson", "Terri", "Female", "1983-11-04", "33435", "Active", "Part-Time", "", "Part-Time"],
        ["", "Employee", "Hernandez", "Janet", "Female", "2000-07-09", "33436", "Active", "Waiting Period", "", "Full-Time"],
        ["", "Employee", "Doe", "Jane", "Female", "1971-09-08", "01133", "Active", "Employee Only", "Plan DEF", "Full-Time"],
        ["", "Employee", "Wayne", "Bruce", "Male", "1989-01-05", "33411", "Active", "Employee + Child(ren)", "Plan ABC", "Full-Time"],
        ["", "Child", "Wayne", "Jack", "Male", "2020-07-10", "33411", "Dependent", "Employee + Child(ren)", "", "Dependent"],
        ["", "Employee", "Parker", "Peter", "Male", "2001-01-12", "93150", "Active", "Employee Only", "Plan DEF", "Full-Time"],
    ])

    # Sheet 3: Group Census (the actual 20 active + 1 COBRA + 9 dependents submitted census)
    group_census_df = pd.DataFrame([
        ["Sequence No", "Relationship to Employee (Employee, Spouse, Child)", "Last Name", "First Name", "Gender",
         "Birth Date (MM/DD/YYYY)", "Home Zip Code", "Active Employee, Dependent, or COBRA",
         "Medical Coverage Tier", "Name of Medical Plan Currently Enrolled in", "Full-Time or Part-Time",
         "If a Dependent, Linked Employee Name", "Work State", "Annual Salary", "Job Title"],
        [1, "Employee", "Calabrese", "Kim", "Female", "1971-01-09", "10308", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 134,203.68", "Sales Support Manager"],
        [2, "Employee", "Composto", "Maria", "Female", "1960-03-24", "08723", "COBRA", "Employee Only", "", "COBRA", "", "NJ", "", ""],
        [3, "Employee", "Cruz", "Sylvia", "Female", "1987-11-25", "10301", "Active", "Waived/Other Coverage", "", "Full Time", "", "NJ", "$ 72,009.60", "Assistant Operations Manager"],
        [4, "Employee", "Daly", "Tracy", "Female", "1991-02-25", "07735", "Active", "Waived/Other Coverage", "", "Full Time", "", "NJ", "$ 55,000.14", "Junior Sales Representative"],
        [5, "Employee", "D'Angelo", "Anthony", "Male", "1975-07-02", "10312", "Active", "Employee + Child(ren)", "", "Full Time", "", "NJ", "$ 96,999.50", "Assistant Operations Manager"],
        [5, "Child", "D'Angelo", "Sophia", "Female", "2016-05-14", "10312", "Dependent", "", "", "Dependent", "D'Angelo, Anthony", "NJ", "", ""],
        [5, "Child", "D'Angelo", "Anthony", "Male", "2018-08-15", "10312", "Dependent", "", "", "Dependent", "D'Angelo, Anthony", "NJ", "", ""],
        [6, "Employee", "D'Angelo", "Brittany", "Female", "1992-11-29", "10312", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 72,342.40", "Sales Support Specialist"],
        [7, "Employee", "DeMarco", "Jodi", "Female", "1975-02-19", "08879", "Active", "Waived/Other Coverage", "", "Full Time", "", "NJ", "53560", "Shuttle Dispatcher"],
        [8, "Employee", "DiNardo", "James", "Male", "1971-05-06", "10312", "Active", "Waived/Other Coverage", "", "Full Time", "", "NJ", "$ 156,000.00", "Sales Agent"],
        [9, "Employee", "Garzone", "Michele", "Female", "1971-12-09", "10314", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 58,500.00", "Sales Agent"],
        [10, "Employee", "Guidetti", "Melissa", "Female", "1988-12-13", "07734", "Active", "Waived/Other Coverage", "", "Full Time", "", "NJ", "$ 74,984.00", "Sales Support Specialist"],
        [11, "Employee", "Heap", "Joseph", "Male", "1988-03-24", "07727", "Active", "Employee Only", "Dental", "Full Time", "", "NJ", "$ 57,083.52", "Operations Analyst"],
        [12, "Employee", "Hens", "Mary", "Female", "1966-12-15", "14225", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 79,976.00", "Sales Agent"],
        [13, "Employee", "Herrera", "Edith", "Female", "1992-02-08", "92108", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 80,000.00", "Sales Agent"],
        [14, "Employee", "Legotte", "Leonard", "Male", "1966-06-13", "07735", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 164,806.46", "Workplace Operations Director"],
        [15, "Employee", "Miller", "Veronica", "Female", "1987-07-03", "07008", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 128,971.44", "Senior Sales Operations Manager"],
        [16, "Employee", "Rosen", "Robert", "Male", "1967-07-07", "43054", "Active", "Employee Only", "", "Full Time", "", "NJ", "$ 95,000.10", "Business Development Representative"],
        [17, "Employee", "Telmany", "Edward JR", "Male", "1975-04-05", "07751", "Active", "Family", "", "Full Time", "", "NJ", "$ 296,106.46", "CEO"],
        [17, "Spouse", "Telmany", "Carolyn", "Female", "1991-09-18", "", "Dependent", "", "", "Dependent", "Telmany, Edward JR", "NJ", "", ""],
        [17, "Child", "Telmany", "Edward JR", "Male", "2007-10-02", "", "Dependent", "", "", "Dependent", "Telmany, Edward JR", "NJ", "", ""],
        [17, "Child", "Telmany", "Anna", "Female", "2023-09-12", "", "Dependent", "", "", "Dependent", "Telmany, Edward JR", "NJ", "", ""],
        [17, "Child", "Telmany", "Marissa", "Female", "2009-11-21", "", "Dependent", "", "", "Dependent", "Telmany, Edward JR", "NJ", "", ""],
        [18, "Employee", "Telmany", "Mark", "Male", "1971-05-07", "33160", "Active", "Family", "", "Full Time", "", "NJ", "$ 296,106.46", ""],
        [18, "Spouse", "Telmany", "Deana", "Female", "1978-10-30", "", "Dependent", "", "", "Dependent", "Telmany, Mark", "NJ", "", ""],
        [18, "Child", "Telmany", "Evy", "Female", "2011-08-07", "", "Dependent", "", "", "Dependent", "Telmany, Mark", "NJ", "", ""],
        [19, "Employee", "Terranova", "Vincent", "Male", "1969-03-14", "10309", "Active", "Employee Only", "", "Full Time", "", "NJ", "132521.74", ""],
        [20, "Employee", "Yehia", "Michael", "Male", "1990-10-14", "10312", "Active", "Employee + Child(ren)", "", "Full Time", "", "NJ", "$ 142,495.86", ""],
        [20, "Child", "Yehia", "Layla", "Female", "2022-03-02", "", "Dependent", "", "", "Dependent", "Yehia, Michael", "NJ", "", ""],
        [21, "Employee", "Crawford", "Ronnie", "Male", "1967-08-10", "34736", "Active", "Waived/Other Coverage", "", "Full Time", "", "NJ", "$ 91,000.00", "Dispatch Operations Manager"],
    ])

    # Sheet 4: Census Template (with repeated *Legal Last Name header row)
    census_template_df = pd.DataFrame([
        ["*Legal Last Name\nInput from medical invoice", "*Legal First Name\nInput from medical invoice", "*Home Zip Code", "*D.O.B.", "*Gender", "Contract Tier", "*Contract Type", "*Coverage Tier", "*Plan Name", "*Cobra"],
        ["*Legal Last Name", "*Legal First Name ", "*Home Zip Code", "*DOB", "*Gender", "Contract Tier", "*Contract Type", "*Coverage Tier", "*Plan Name", "*Cobra"],
        ["Calabrese", "Kim", "10308", "1971-01-09", "Female", "1", "Employee", "EE", "", ""],
    ])

    # Sheet 5: Generic Conv (conversion tab that caused Kim Calabrese gender conflict & Telmany/Yehia self-dependents)
    generic_conv_df = pd.DataFrame([
        ["#", "Name", "M/F", "DOB", "Zip", "Med Tier", "COBRA", "", "Gender", "", "", "Last", "First"],
        [1, "Calabrese, Kim", "F", "1971-01-09", "10308", "EE", "N", "", "Male", "M", "", "Calabrese", "Kim"],
        [2, "Composto, Maria", "F", "1960-03-24", "08723", "EE", "Y", "", "Female", "F", "", "Composto", "Maria"],
        [11, "Heap, Joseph", "", "1988-03-24", "07727", "", "N", "", "Part-Time", "NE", "", "Heap", "Joseph"],
        [17, "Telmany, Edward JR", "", "1975-04-05", "07751", "", "N", "", "COBRA", "Y", "", "Telmany", "Edward JR"],
        [18, "Telmany, Mark", "", "1971-05-07", "33160", "", "N", "", "", "", "", "Telmany", "Mark"],
        [20, "Yehia, Michael", "", "1990-10-14", "10312", "", "N", "", "Employee Only", "EE", "", "Yehia", "Michael"],
    ])

    return RawDoc(
        path=Path("UPDATED US Coachways PEO Census .xlsx"),
        ext=".xlsx",
        tables=[
            RawTable("SAIL", sail_df),
            RawTable("EXAMPLE", example_df),
            RawTable("Group Census", group_census_df),
            RawTable("Census Template", census_template_df),
            RawTable("Generic Conv", generic_conv_df),
        ],
        text="",
    )


_RENEWAL_TEXT = """
Effective Date: 4/1/2026       HOLMDEL, NJ (MONMOUTH)                                  Prepared on 2/26/2026
SIC Code: 4142                 Zip Code: 07733                     for US Bus Charter & Limo Inc DBA US Coachways
Census Details
Employee Name Dependent Relationship Age DOB Gender Home Zip Work Zip Medical Dental Vision Life Disability COBRA
CALABRESE, KIM            -    55 1/9/1971 F 10308 07733 EE -   -  -   -    N
D'ANGELO, ANTHONY         -    50 7/2/1975 M 10312 07733 EC -   -  -   -    N
           D'ANGELO JR, ANTHONY C 17 8/15/2008 M 10312 - -  -   -  -   -    -
           D'ANGELO, SOPHIA C  9  5/14/2016 F 10312 -   -   -   -  -   -    -
D'ANGELO, BRITTANY        -    33 11/29/1992 F 10312 07733 EE - -  -   -    N
GARZONE, MICHELE          -    54 12/9/1971 F 10314 07733 EE -  -  -   -    N
HEAP JR, JOSEPH           -    28 3/24/1998 M 07727 07733 EE -  -  -   -    N
HENS, MARY                -    59 12/15/1966 F 14225 07733 EE - -  -   -    N
HERRERA, EDITH            -    34 2/8/1992 F 92108 07733 EE -   -  -   -    N
LEGOTTE, LEONARD          -    59 6/13/1966 M 07735 07733 EE -  -  -   -    N
MILLER, VERONICA          -    38 7/3/1987 F 07008 07733 EE -   -  -   -    N
ROSEN, ROBERT             -    58 7/7/1967 M 43054 07733 EE -   -  -   -    N
TELMANY, EDWARD           -    50 4/5/1975 M 08857 07733 EF -   -  -   -    N
           TELMANY, CAROLYN S  34 9/18/1991 F 08857 -   -   -   -  -   -    -
           TELMANY JR, EDWARD C 18 10/2/2007 M 08857 -  -   -   -  -   -    -
           TELMANY, MARISSA C  16 11/21/2009 F 08857 -  -   -   -  -   -    -
           TELMANY, ANNA  C    2  9/12/2023 F 08857 -   -   -   -  -   -    -
TELMANY, MARK             -    54 5/7/1971 M 10309 07733 EF -   -  -   -    N
           TELMANY, DEANA S    47 10/30/1978 F 10309 -  -   -   -  -   -    -
           TELMANY, EVY   C    14 8/7/2011 F 10309 -    -   -   -  -   -    -
TERRANOVA, VINCENT        -    57 3/14/1969 M 10309 07733 EE -  -  -   -    N
YEHIA, MICHAEL            -    35 10/14/1990 M 10312 07733 EC - -  -   -    N
           YEHIA, LAYLA   C    4  3/2/2022 F 10312 -    -   -   -  -   -    -
"""

_KUSTOM_APP_TEXT = """
PEO Request for Proposal
--- [INTERACTIVE FORM FIELDS] ---
Client Legal Name: US Bus Charter & Limo, Inc.
DBA: US Coachways Inc.
Client Company Contact: Lenny Legotte
Title: General Manager
Please attach a list with all location addresses: 960 Holmdel Rd. Holmdel, NJ 07733 Suite 1203
Phone: 800-359-5991 ext. 301
Email: accounting@uscoachwaysinc.com
FT Employees: 24
PT Employees: 3
Contract Employees: 0
FEIN: 26-0156156
Nature of Business: Traverl
"""

_ORIGINAL_EMAIL_TEXT = """
From: Tim Tyler <Tim.Tyler@DecisionHR.com>
Sent: Thursday, November 12, 2026 11:43 AM
Subject: New Opp - US Bus Charter & Limo, Inc. - RUSH!!!
This is from my partner KustomHR – broker commission 25% of admin. Effective date 1/1.
"""


def _build_march_invoice_doc() -> InvoiceDoc:
    records = [
        InvoiceRecord(first="KIM", last="CALABRESE", raw_name="CALABRESE, KIM", tier="EE", plan="FrP1500i80LX21B", premium=866.09, status="Active"),
        InvoiceRecord(first="ANTHONY", last="D'ANGELO", raw_name="D'ANGELO, ANTHONY", tier="EC", plan="FrP1500i80LX21B", premium=1622.37, status="Active"),
        InvoiceRecord(first="BRITTANY", last="D'ANGELO", raw_name="D'ANGELO, BRITTANY", tier="EE", plan="FrP3500i80LX21B", premium=735.61, status="Active"),
        InvoiceRecord(first="MICHELE", last="GARZONE", raw_name="GARZONE, MICHELE", tier="EE", plan="FrP1500i80LX21B", premium=866.09, status="Active"),
        InvoiceRecord(first="JOSEPH", last="HEAP", raw_name="HEAP, JOSEPH JR", tier="EE", plan="FrP3500i80LX21B", premium=735.61, status="Active"),
        InvoiceRecord(first="MARY", last="HENS", raw_name="HENS, MARY", tier="EE", plan="FrP500i100LX21B", premium=1085.32, status="Active"),
        InvoiceRecord(first="EDITH", last="HERRERA", raw_name="HERRERA, EDITH", tier="EE", plan="FrP1500i80LX21B", premium=866.09, status="Active"),
        InvoiceRecord(first="LEONARD", last="LEGOTTE", raw_name="LEGOTTE, LEONARD J", tier="EE", plan="FrP500i100LX21B", premium=1085.32, status="Active"),
        InvoiceRecord(first="VERONICA", last="MILLER", raw_name="MILLER, VERONICA", tier="EE", plan="FrP1500i80LX21B", premium=866.09, status="Active"),
        InvoiceRecord(first="ROBERT", last="ROSEN", raw_name="ROSEN, ROBERT J", tier="EE", plan="FrP1500i80LX21B", premium=866.09, status="Active"),
        InvoiceRecord(first="EDWARD", last="TELMANY", raw_name="TELMANY, EDWARD", tier="FAM", plan="FrP500i100LX21B", premium=3489.65, status="Active"),
        InvoiceRecord(first="MARK", last="TELMANY", raw_name="TELMANY, MARK", tier="FAM", plan="FrP500i100LX21B", premium=3489.65, status="Active"),
        InvoiceRecord(first="VINCENT", last="TERRANOVA", raw_name="TERRANOVA, VINCENT", tier="EE", plan="FrP1500i80LX21B", premium=866.09, status="Active"),
        InvoiceRecord(first="MICHAEL", last="YEHIA", raw_name="YEHIA, MICHAEL", tier="EC", plan="FrP1500i80LX21B", premium=1622.37, status="Active"),
    ]
    return InvoiceDoc(
        path="MED- MARCH INVOICE.pdf",
        records=records,
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        doc_date=date(2026, 2, 10),
        number="512259157962",
    )


def test_us_bus_charter_golden_feedback(monkeypatch):
    """Verify all 10 client feedback items on US Bus Charter & Limo, Inc."""
    monkeypatch.setattr("census_config.USE_LLM_FALLBACK", False)

    census_doc = _build_us_bus_census_doc()
    kustom_doc = RawDoc(path=Path("Kustom Application.pdf"), ext=".pdf", text=_KUSTOM_APP_TEXT)
    renewal_doc = RawDoc(path=Path("US Bus 2026 Renewal Proposal.pdf"), ext=".pdf", text=_RENEWAL_TEXT)
    email_doc = RawDoc(path=Path("Original_Email.pdf"), ext=".pdf", text=_ORIGINAL_EMAIL_TEXT)
    all_docs = [census_doc, kustom_doc, renewal_doc, email_doc]

    # 1. Prospect & SIC Code (4142 from renewal proposal, never 2022)
    prospect, _, _ = build_prospect([kustom_doc], [email_doc], all_docs=all_docs)
    if not prospect.effective_date:
        prospect.effective_date = date(2027, 1, 1)
    assert prospect.sic == "4142"
    assert prospect.ft_employees == 24
    assert prospect.pt_employees == 3
    assert "Kustom Application" in prospect.ft_employees_source

    # 2. Census Population & Employee Count (20 active + 1 COBRA = 21 employees, 9 dependents)
    census, meta = build_census([census_doc], effective_date=prospect.effective_date)
    emps = census.canonical_census_employees
    deps = census.dependents
    assert len(emps) == 21
    assert len(deps) == 9

    emp_names = {f"{e.first} {e.last}" for e in emps}
    for fake_name in ("Bob Smith", "Jane Doe", "Bruce Wayne", "Peter Parker", "Lila Jones", "Terri Johnson", "Janet Hernandez"):
        assert fake_name not in emp_names
    assert not any("*" in (p.first or "") or "*" in (p.last or "") for p in census.people)

    # 3. COBRA Status (Maria Composto = Y, 20 active employees = N)
    composto = next(e for e in emps if e.last == "Composto")
    assert composto.cobra == "Y"
    cobra_emps = [e for e in emps if e.cobra == "Y"]
    active_emps = [e for e in emps if e.cobra != "Y"]
    assert len(cobra_emps) == 1
    assert len(active_emps) == 20

    # 4. Kim Calabrese Gender = F
    calabrese = next(e for e in emps if e.last == "Calabrese")
    assert calabrese.gender == "F"

    # 5. Invalid Dependent Records (Edward Telmany, Mark Telmany, Michael Yehia NOT their own dependents)
    edward = next(e for e in emps if e.last == "Telmany" and "Edward" in e.first)
    mark = next(e for e in emps if e.last == "Telmany" and e.first == "Mark")
    yehia = next(e for e in emps if e.last == "Yehia")

    assert len(edward.dependents) == 4
    assert [(d.first, d.role, d.dob) for d in edward.dependents] == [
        ("Carolyn", "SP", date(1991, 9, 18)),
        ("Edward JR", "CH", date(2007, 10, 2)),
        ("Anna", "CH", date(2023, 9, 12)),
        ("Marissa", "CH", date(2009, 11, 21)),
    ]
    assert not any(d.dob == edward.dob for d in edward.dependents)

    assert len(mark.dependents) == 2
    assert [(d.first, d.role) for d in mark.dependents] == [("Deana", "SP"), ("Evy", "CH")]
    assert not any(d.first == "Mark" for d in mark.dependents)

    assert len(yehia.dependents) == 1
    assert [(d.first, d.role) for d in yehia.dependents] == [("Layla", "CH")]
    assert not any(d.first == "Michael" for d in yehia.dependents)

    # 6. Joseph Heap retains EE medical coverage (not turned into WO by 'Dental' in plan column)
    heap = next(e for e in emps if e.last == "Heap")
    assert heap.coverage == "EE"

    # 7. Invoice Comparison against March Invoice (14 billed employees match, 0 fake missing-from-invoice)
    inv_doc = _build_march_invoice_doc()
    F = Findings()
    results = run_comparison(census, inv_doc.records, None, meta, F, have_invoice=True, renewal_required=False)
    matched = [e for e in emps if results[id(e)].inv is not None]
    missing_inv = [e for e in emps if results[id(e)].result == "Not On Invoice"]
    assert len(matched) == 14
    assert [e.display for e in missing_inv] == ["Maria Composto"]

    # 8. DOB Discrepancies (Joseph Heap 1988 vs 1998, Anthony D'Angelo dependent 2018 vs 2008)
    roles = {"census": [census_doc], "renewal": [renewal_doc], "ghq": [kustom_doc], "rfp": [email_doc], "invoice": []}
    dob_items = dob_discrepancy_items(census, roles=roles, docs=all_docs, invoice_records=inv_doc.records)
    assert len(dob_items) == 2
    dob_texts = "\n".join(i.text for i in dob_items)
    assert "Joseph Heap" in dob_texts and "03/24/1988" in dob_texts and "03/24/1998" in dob_texts
    assert "Anthony D'Angelo (dependent of Anthony D'Angelo)" in dob_texts and "08/15/2018" in dob_texts and "08/15/2008" in dob_texts

    # 9. Invoice Validity / Recency (02/10/2026 invoice with 03/01/2026-03/31/2026 coverage flagged)
    rec_items = invoice_recency_items([inv_doc], prospect=prospect, roles=roles, docs=all_docs)
    assert len(rec_items) == 1
    assert "02/10/2026" in rec_items[0].text
    assert "03/01/2026–03/31/2026" in rec_items[0].text
    assert "updated" in rec_items[0].text.lower()

    # 10. Headcount Discrepancy & Missing Information Email
    hc_items = headcount_items(prospect, census)
    assert len(hc_items) == 1
    assert "24 full-time employees (and 3 part-time)" in hc_items[0].text
    assert "20 active employees + 1 COBRA participant (21 total)" in hc_items[0].text

    routing = determine_routing(prospect, docs=all_docs)
    assert routing.state == "NJ"
    assert routing.carrier == "Cigna"
    email_text = generate_missing_information_email(
        prospect=prospect,
        routing=routing,
        findings=F,
        census=census,
        have_census=True,
        have_invoice=True,
        have_renewal=True,
        renewal_required=False,
        claims_required=False,
        roles=roles,
        selected_invoices=[inv_doc],
        invoice_records=inv_doc.records,
        results=results,
        docs=all_docs,
    )
    for unrelated in ("Bob Smith", "Jane Doe", "Bruce Wayne", "Peter Parker", "Legal Last Name", "Lila Jones", "Terri Johnson", "Janet Hernandez"):
        assert unrelated not in email_text
    assert "Joseph Heap" in email_text
    assert "Anthony D'Angelo (dependent of Anthony D'Angelo)" in email_text
    assert "02/10/2026" in email_text and "03/01/2026–03/31/2026" in email_text
    assert "20 active employees + 1 COBRA participant" in email_text

    # 11. Validator passes 100% of checks with EngineStatus.PASSED
    F.email_text = email_text
    validator = UnderwritingValidator(instruction_hash="us_bus_golden")
    val_report = validator.validate_full_pipeline(
        prospect=prospect,
        routing=routing,
        census=census,
        selected_invoices=[inv_doc],
        renewal=None,
        ren_required=False,
        claims_required=False,
        findings=F,
        results=results,
        roles=roles,
    )
    failed = [c for c in val_report.checks if c.status != CheckStatus.PASS]
    assert failed == [], f"Unexpected failed checks: {[(c.rule_id, c.reason) for c in failed]}"
    assert val_report.release_status == EngineStatus.PASSED

    print("\n=== US BUS CHARTER GOLDEN TEST CASE VERIFICATION ===")
    print(f"1. Routing & SIC       : State={routing.state}, Carrier={routing.carrier}, SIC={prospect.sic}")
    print(f"2. Census Population   : {len(emps)} employees ({len(active_emps)} active + {len(cobra_emps)} COBRA), {len(deps)} dependents")
    print(f"3. Maria Composto COBRA: {composto.cobra}")
    print(f"4. Kim Calabrese Gender: {calabrese.gender}")
    print(f"5. Edward Telmany Deps : {[(d.display, d.role, str(d.dob)) for d in edward.dependents]}")
    print(f"   Mark Telmany Deps   : {[(d.display, d.role, str(d.dob)) for d in mark.dependents]}")
    print(f"   Michael Yehia Deps  : {[(d.display, d.role, str(d.dob)) for d in yehia.dependents]}")
    print(f"6. Invoice Comparison  : {len(matched)}/{len(inv_doc.records)} billed employees matched; Missing from invoice: {[e.display for e in missing_inv]}")
    print("7. DOB Discrepancies   :")
    for item in dob_items:
        print(f"   * {item.text}")
    print("8. Invoice Validity    :")
    for item in rec_items:
        print(f"   * {item.text}")
    print("9. Headcount Mismatch  :")
    for item in hc_items:
        print(f"   * {item.text}")
    print(f"10. Validator Status   : {val_report.release_status.value} ({val_report.passed_checks}/{val_report.total_checks} checks passed)")
    print("\n--- MISSING INFORMATION EMAIL ---")
    print(email_text)


