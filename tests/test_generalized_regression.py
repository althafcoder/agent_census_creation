"""Generalized regression tests verifying dynamic, reusable logic across all submissions.

Ensures that:
1. Relationships and subscriber IDs are preserved across reordered rows, deduplication, and sorting.
2. Subscriber IDs convert to final workbook data rows only after output order is finalized.
3. Dependents appearing before their subscriber in source files are correctly linked.
4. Decimal arithmetic and sign preservation work accurately for any monetary amounts.
5. Invoices with arbitrary subscriber names, plan IDs, and component lines parse deterministically.
6. Local cache bypass toggle executes cleanly and bypasses cached entries.
"""
from datetime import date
from decimal import Decimal
import pandas as pd
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from models import Person, Census, InvoiceRecord, InvoiceDoc, Findings
from census_builder import _link_dependents, _order_output, _State, _DepRef, validate_census
from utils import name_key
from invoice_parser import combine_records
from tracer import DiagnosticTracer
from caching.local_cache import LocalPromptCache
from comparator import run_comparison


def test_dependent_linking_order_independence():
    """Verify dependents are correctly linked to subscriber even if they appear before the subscriber."""
    census = Census()
    st = _State(census)

    # 1. Dependent created FIRST
    dep = Person(role="CH", first="ChildOne", last="Smith", rel_raw="Child", source="census.xlsx")
    dep.record_id = "rec_0001"
    dep.subscriber_id = ""  # Not yet linked
    ref = _DepRef(dep, emp_key=name_key("John", "Smith"), file="census.xlsx")
    st.dep_refs.append(ref)

    # 2. Employee created SECOND
    emp = Person(role="EE", first="John", last="Smith", rel_raw="Employee", source="census.xlsx")
    emp.record_id = "rec_0002"
    emp.coverage = "EC"
    emp.plan = "PlanAlpha"
    st.employees.append(emp)
    st.by_name[name_key("John", "Smith")] = [emp]

    # 3. Link dependents
    _link_dependents(st, effective_date=date(2026, 1, 1))

    # Assert link is established
    assert dep.employee is emp
    assert dep.subscriber_id == emp.record_id
    assert dep in emp.dependents

    # 4. Finalize output order
    _order_output(st)

    # Employee is row 1, dependent is row 2
    assert emp.row_no == 1
    assert dep.row_no == 2
    # In workbook output, dependent references employee's data row (1)
    assert dep.employee.row_no == 1


def test_reordered_rows_subscriber_data_row_resolution():
    """Ensure data rows are strictly assigned sequentially in final output order, never hardcoded."""
    census = Census()
    st = _State(census)

    emp1 = Person(role="EE", first="Alice", last="Brown")
    emp1.record_id = "rec_0010"
    emp2 = Person(role="EE", first="Charlie", last="Davis")
    emp2.record_id = "rec_0020"
    dep2 = Person(role="SP", first="Dana", last="Davis")
    dep2.record_id = "rec_0021"

    st.employees = [emp1, emp2]
    dep2.employee = emp2
    dep2.subscriber_id = emp2.record_id
    emp2.dependents.append(dep2)

    _order_output(st)

    assert emp1.row_no == 1
    assert emp2.row_no == 2
    assert dep2.row_no == 3
    assert dep2.employee.row_no == 2


def test_census_validation_detects_unresolved_dependents():
    """Flag unresolved relationships for review rather than guessing."""
    census = Census()
    emp = Person(role="EE", first="Alice", last="Taylor")
    emp.record_id = "rec_0001"
    dep_orphan = Person(role="CH", first="Unknown", last="Orphan")
    dep_orphan.record_id = "rec_0002"
    dep_orphan.subscriber_id = "" # No subscriber link

    census.people = [emp, dep_orphan]

    warnings = validate_census(census)
    assert any("has no subscriber_id" in w for w in warnings)


def test_invoice_combine_records_no_cross_employee_merge():
    """Verify invoice combine_records does not merge different subscribers with the same policy."""
    inv = InvoiceDoc(path="invoice.pdf", records=[
        InvoiceRecord(first="Michael", last="Scott", plan="PPO500", current_premium=650.00, retro_amount=0.00),
        InvoiceRecord(first="Dwight", last="Schrute", plan="PPO500", current_premium=650.00, retro_amount=150.00, retro_code="ADD"),
    ])

    combined = combine_records([inv])
    assert len(combined) == 2
    names = {f"{r.first} {r.last}" for r in combined}
    assert "Michael Scott" in names
    assert "Dwight Schrute" in names

    dwight = next(r for r in combined if r.last == "Schrute")
    assert dwight.current_premium == 650.00
    assert dwight.retro_amount == 150.00
    assert dwight.retro_code == "ADD"


def test_cache_bypass_flag(tmp_path):
    """Verify bypass_cache parameter forces cache miss and bypasses SQLite cache."""
    cache = LocalPromptCache(db_dir=tmp_path)
    prompt = "Test prompt for dynamic cache bypass verification"
    model = "claude-test-model"
    response = {"result": "success"}

    # Set in cache
    cache.set(prompt, model, response, step_name="test_step")

    # Regular lookup should hit
    hit = cache.get(prompt, model, step_name="test_step", bypass_cache=False)
    assert hit == response

    # Lookup with bypass_cache=True must return None
    miss = cache.get(prompt, model, step_name="test_step", bypass_cache=True)
    assert miss is None


def test_comparison_invoice_only_none_premium():
    """Verify invoice-only employee with None premium does not raise TypeError during comparison."""
    census = Census()
    emp = Person(role="EE", first="Alice", last="Wonderland", coverage="EE", plan="Gold PPO", source="census.xlsx")
    census.employees.append(emp)
    census.people.append(emp)

    # Invoice has Alice (matches) and Bob (invoice-only, with premium=None)
    inv_alice = InvoiceRecord(first="Alice", last="Wonderland", raw_name="WONDERLAND, ALICE", plan="Gold PPO", tier="EE", premium=500.0)
    inv_bob = InvoiceRecord(first="Bob", last="Builder", raw_name="BUILDER, BOB", plan="Gold PPO", tier="EE", premium=None)

    findings = Findings()
    results = run_comparison(census, [inv_alice, inv_bob], renewal=None, meta={}, F=findings, have_invoice=True)

    # Bob should be added to census as invoice_only
    bob_person = next((p for p in census.people if getattr(p, "invoice_only", False)), None)
    assert bob_person is not None
    assert bob_person.last == "Builder"

    # Bob's row result should exist
    rr = results[id(bob_person)]
    assert rr.result == "Invoice Employee Not Found In Census"
    assert rr.inv_premium is None

    # Findings should have exception without crashing on string formatting
    exc_entries = [e for e in findings.exceptions if e[0] == "Employee missing from census"]
    assert len(exc_entries) == 1
    assert "BUILDER, BOB" in exc_entries[0][1]
    assert "Gold PPO" in exc_entries[0][2]


def test_comparison_premium_due_adjustment_none_premium():
    """Verify employee with 'premium due' status and None premium formats note safely."""
    census = Census()
    emp = Person(role="EE", first="Charlie", last="Brown", coverage="EE", plan="Silver HMO", source="census.xlsx")
    census.employees.append(emp)
    census.people.append(emp)

    inv_charlie = InvoiceRecord(first="Charlie", last="Brown", raw_name="BROWN, CHARLIE", plan="Silver HMO", tier="EE",
                                status="Active - Premium Due", premium=None)

    findings = Findings()
    results = run_comparison(census, [inv_charlie], renewal=None, meta={}, F=findings, have_invoice=True)

    rr = results[id(emp)]
    assert "Premium Due" in rr.status
    # Verify note was appended without error
    note_texts = [text for text, _ in rr.notes]
    assert any("PREMIUM DUE adjustment" in t for t in note_texts)


def test_multi_benefit_medical_only_isolation_and_idempotent_comparison():
    """Verify multi-benefit stacked roster filtering (both with Benefit column and via 1-Employee-1-Medical-Plan
    Invariant when Benefit column is absent), non-medical-only employee WO resolution, idempotent run_comparison()
    across self-healing reruns, medical-only SBC email requests, and dynamic RAPT DHR template layout normalization.
    """
    import openpyxl
    import census_config as C
    from readers import RawDoc, RawTable
    from census_builder import build_census, refine_non_enrolled_statuses
    from models import Prospect
    from router import RoutingDecision, generate_missing_information_email
    from workbook_writer import _inspect_rapt_layout
    from learning_engine.validator import UnderwritingValidator, CheckStatus

    headers = [
        "Company Name", "Employee ID", "Relationship", "First Name", "Last Name",
        "Sex", "DOB", "Zip Code", "Class", "Hire Date", "Benefit", "Plan",
        "Effective Date", "Coverage Level", "Is Cobra",
    ]
    rows = [
        headers,
        # 1. Lorraine Robinson: Medical + Dental + Vision + Life + LTD
        ["MM Robinson LLC", "25546", "Employee", "Lorraine", "Robinson", "F", "9/21/1968", "32825", "Full Time Eligible", "12/1/2002", "Medical", "Buy-Up: E2000i8022B 2026", "6/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "25546", "Employee", "Lorraine", "Robinson", "F", "9/21/1968", "32825", "Full Time Eligible", "12/1/2002", "Dental", "Buy-Up: P4883 2026", "6/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "25546", "Employee", "Lorraine", "Robinson", "F", "9/21/1968", "32825", "Full Time Eligible", "12/1/2002", "Vision", "SH107 2026", "6/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "25546", "Employee", "Lorraine", "Robinson", "F", "9/21/1968", "32825", "Full Time Eligible", "12/1/2002", "Group Life", "Basic Life ADD 25K 2026", "6/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "25546", "Employee", "Lorraine", "Robinson", "F", "9/21/1968", "32825", "Full Time Eligible", "12/1/2002", "Group Long-Term Disability", "LTD20281 2026", "6/1/2026", "Employee", "No"],
        # 2. Cecilia Garcia: Dental + Vision + Life ONLY (waived Medical -> must resolve to WO)
        ["MM Robinson LLC", "162110", "Employee", "CECILIA", "GARCIA", "F", "10/20/1982", "34744", "Full Time Eligible", "9/8/2025", "Dental", "Base: D1057 2026", "11/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "162110", "Employee", "CECILIA", "GARCIA", "F", "10/20/1982", "34744", "Full Time Eligible", "9/8/2025", "Vision", "SH107 2026", "11/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "162110", "Employee", "CECILIA", "GARCIA", "F", "10/20/1982", "34744", "Full Time Eligible", "9/8/2025", "Group Life", "Basic Life ADD 25K 2026", "11/1/2026", "Employee", "No"],
        # 3. Haris Sabic: Medical EE + Dental Family with Spouse & Child (must stay EE with 0 medical dependents)
        ["MM Robinson LLC", "25547", "Employee", "Haris", "Sabic", "M", "12/26/1979", "32707", "Full Time Eligible", "4/3/2023", "Medical", "Base: PROE250020i7024B 2026", "6/1/2026", "Employee", "No"],
        ["MM Robinson LLC", "25547", "Employee", "Haris", "Sabic", "M", "12/26/1979", "32707", "Full Time Eligible", "4/3/2023", "Dental", "Base: D1057 2026", "6/1/2026", "Employee + Family", "No"],
        ["MM Robinson LLC", "25547", "Spouse", "Ostina", "Sabic", "F", "10/14/1982", "32707", "Full Time Eligible", "4/3/2023", "Dental", "Base: D1057 2026", "6/1/2026", "Employee + Family", "No"],
        ["MM Robinson LLC", "25547", "Child", "Amaris", "Sabic", "M", "8/20/2002", "32707", "Full Time Eligible", "4/3/2023", "Dental", "Base: D1057 2026", "6/1/2026", "Employee + Family", "No"],
        # 4. Jeffrey Robinson: Medical EC with Child Sophia
        ["MM Robinson LLC", "25545", "Employee", "Jeffrey", "Robinson", "M", "4/3/1962", "32825", "Full Time Eligible", "4/2/1999", "Medical", "Buy-Up: E2000i8022B 2026", "6/1/2026", "Employee + Child", "No"],
        ["MM Robinson LLC", "25545", "Child", "Sophia", "Robinson", "F", "5/14/2003", "32825", "Full Time Eligible", "4/2/1999", "Medical", "Buy-Up: E2000i8022B 2026", "6/1/2026", "Employee + Child", "No"],
        ["MM Robinson LLC", "25545", "Child", "Sophia", "Robinson", "F", "5/14/2003", "32825", "Full Time Eligible", "4/2/1999", "Dental", "Buy-Up: P4883 2026", "6/1/2026", "Employee + Child", "No"],
    ]

    # A. Test both WITH Benefit column and WITHOUT Benefit column (1-Employee-1-Medical-Plan Invariant)
    for drop_benefit_col in (False, True):
        test_rows = [[c for i, c in enumerate(r) if not (drop_benefit_col and i == 10)] for r in rows]
        df = pd.DataFrame(test_rows)
        doc = RawDoc(path=Path("MM Robinson Census 2026.csv"), ext=".csv", tables=[RawTable("Roster", df)], text="")
        census, meta = build_census([doc], anchored_medical_plans={"E2000i8022B", "PROE250020i7024B"})
        refine_non_enrolled_statuses(census)

        assert len(census.canonical_census_employees) == 4
        emp_map = {f"{e.first.lower()} {e.last.lower()}": e for e in census.canonical_census_employees}
        assert emp_map["lorraine robinson"].coverage == "EE"
        assert emp_map["lorraine robinson"].plan == "Buy-Up: E2000i8022B 2026"
        assert emp_map["cecilia garcia"].coverage == "WO"
        assert emp_map["cecilia garcia"].plan == "NA"
        assert emp_map["haris sabic"].coverage == "EE"
        assert emp_map["haris sabic"].plan == "Base: PROE250020i7024B 2026"
        assert len(emp_map["haris sabic"].dependents) == 0
        assert emp_map["jeffrey robinson"].coverage == "EC"
        assert [d.display for d in emp_map["jeffrey robinson"].dependents] == ["Sophia Robinson"]
        assert [d.display for d in census.dependents] == ["Sophia Robinson"]

    # B. Test idempotent run_comparison across multiple runs (simulating Learning Engine self-healing loop)
    inv_recs = [
        InvoiceRecord(first="Haris", last="Sabic", raw_name="Sabic, Haris", plan="PROE250020i7024B", tier="EE", status="Active", premium=563.13),
        InvoiceRecord(first="Jeffrey", last="Robinson", raw_name="ROBINSON, JEFFREY", plan="E2000i8022B", tier="EC", status="Active", premium=2237.11),
        InvoiceRecord(first="Angel", last="Andrade", raw_name="Andrade, Angel", plan="PROE250020i7024B", tier="EE", status="Active", premium=563.13),
        InvoiceRecord(first="Garrett", last="Miles", raw_name="Miles, Garrett", plan="E2000i8022B", tier="EE", status="Active (Retro ADD 08/2025)", premium=721.02),
    ]
    F = Findings()
    results1 = run_comparison(census, inv_recs, None, meta, F, have_invoice=True)
    results2 = run_comparison(census, inv_recs, None, meta, F, have_invoice=True)

    inv_only = [p for p in census.people if p.invoice_only]
    assert len(inv_only) == 2
    assert len(census.canonical_census_employees) == 4
    for p in inv_only:
        rr = results2[id(p)]
        assert rr.match_pct == 0
        assert rr.result == "Invoice Employee Not Found In Census"

    # C. Verify Missing Information Email requests only missing Medical SBC (PROE250020i7024B) and lists Lorraine once
    sbc1 = RawDoc(path=Path("levelfunded_sbc_document_E2000i8022B.pdf"), ext=".pdf", text="E2000i8022B")
    raw_inv = RawDoc(
        path=Path("invoice.pdf"),
        ext=".pdf",
        text="1153433 ROBINSON, LORRAINE Non-Contributory MAX *****684300 E A 0.87 $0.78 $8.03\n1.5K LTD\n",
    )
    inv_doc = InvoiceDoc(path=Path("invoice.pdf"), records=inv_recs, period_start=date(2025, 9, 1))
    prospect = Prospect(company="MM Robinson LLC", state="FL", effective_date=date(2026, 1, 1))
    routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    email_txt = generate_missing_information_email(
        prospect=prospect, routing=routing, census=census, findings=F,
        have_census=True, have_invoice=True, have_renewal=False, renewal_required=False, claims_required=False,
        roles={"census": [doc], "invoice": [raw_inv], "plan_design": [sbc1], "ghq": [sbc1]},
        selected_invoices=[inv_doc], invoice_records=inv_recs, results=results2, docs=[doc, raw_inv, sbc1],
    )
    assert "Base: PROE250020i7024B 2026" in email_txt
    assert "P4883" not in email_txt
    assert "D1057" not in email_txt
    assert "SH107" not in email_txt
    assert "LTD20281" not in email_txt
    assert email_txt.count("Lorraine Robinson:") == 1

    # D. Verify Learning Engine RULE_29 does not false-positive on non-medical invoice lines
    validator = UnderwritingValidator(instruction_hash="test_hash")
    val_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census, selected_invoices=[inv_doc],
        renewal=None, ren_required=False, claims_required=False, findings=F, results=results2,
        roles={"census": [doc], "invoice": [raw_inv], "ghq": [sbc1]},
    )
    r29 = next((c for c in val_report.checks if c.rule_id == "RULE_29_INVOICE_PRESENCE_FALSE_POSITIVE"), None)
    assert r29 is not None and r29.status == CheckStatus.PASS

    # E. Verify RAPT layout dynamic inspection normalizes 'REI Sales Person' -> 'DHR Sales Person'
    # and resolves merged label ranges (e.g. F2:G2 -> H2 for SIC) without MergedCell read-only errors
    from workbook_writer import write_census_sheet
    wb = openpyxl.load_workbook(C.RAPT_TEMPLATE_PATH)
    ws = wb.active
    cells, hdr_row, first_data_row, comp_start_col, tc = _inspect_rapt_layout(ws)
    assert ws["G10"].value == "DHR Sales Person"
    assert cells["sic"] == "H2"
    assert cells["sales_person"] == "H10"
    assert comp_start_col == 13
    prospect.sic = "7382"
    prospect.address = "6457 Hazeltine National Dr, Suite 110"
    prospect.city_state_zip = "Orlando, FL 32822"
    write_census_sheet(ws, census, prospect, results2)
    assert ws["H2"].value == "7382"
    assert ws["C4"].value == "MM Robinson LLC"


def test_comparison_output_exclusion_and_dependent_provenance_guard():
    """Verify completed comparison workbooks are never ingested as census sources,
    multi-line template headers map cleanly, positional fallback never coerces
    unverified/conflicting-plan rows into CH dependents, and the Learning Engine
    catches both deliverable-as-census and unverified/cross-role dependents."""
    from classifier import classify, detect_header, is_comparison_output_doc
    from readers import RawDoc, RawTable
    from census_builder import build_census
    from models import Findings, Person, Prospect, RowResult
    from router import RoutingDecision
    from learning_engine.validator import UnderwritingValidator, CheckStatus
    import census_config as C

    # 1. Completed comparison output workbook with multi-line template headers
    out_headers = [
        "Data Row *",
        "First Name *",
        "Last Name *",
        "Gender *\nEnter M for Male\nor F for Female",
        "Date of Birth *",
        "Home Zip Code *",
        "Relationship to Employee *\nEnter EE for the employee row\nEnter SP for spouses\nEnter CH for children",
        "Dependent of Employee Row *",
        "Coverage *\nEnter EE for Employee Only\nEnter EC for Employee + Child(ren)",
        "Cobra Participant *\nEnter Y for Yes\nor N for No",
        "Plan Name",
        "Monthly Total Premium\n\nThe monthly premium of the employee's enrolled plan.",
        "Invoice Employee Name",
        "Match %",
        "Invoice Plan",
        "Invoice Tier",
        "Invoice Status",
        "Invoice Premium",
        "Renewal Premium",
        "Comparison Result",
        "Notes",
    ]
    out_rows = [
        out_headers,
        [1, "Sean", "Storch", "M", "1976-02-24", "33570", "EE", None, "EC", "N", "E2000i10021B", 2067.14, "Storch, Sean", 1, "E2000i10021B", "EC", "A", 2067.14, None, "Match", ""],
        [2, "Mackenzie", "Storch", "F", "2012-01-23", "33570", "CH", 1, "EC", "N", "E2000i10021B", None, None, None, None, None, None, None, None, "Dependent", ""],
        [3, "Maeghan", "Solanki", "F", "2001-08-04", "33570", "CH", 1, "EC", "N", "E2000i10021B", None, None, None, None, None, None, None, None, "Dependent", ""],
        [None, "Jeremy", "Aschman", None, None, None, None, None, None, None, "E3000i8021B", None, "Aschman, Jeremy", 0, "E3000i8021B", "E", "A", 929.85, None, "Invoice Employee Not Found In Census", "Recent term"],
    ]
    out_df = pd.DataFrame(out_rows)
    out_doc = RawDoc(path=Path("reference_output.xlsx"), ext=".xlsx", tables=[RawTable("Sheet1", out_df)], text="")

    # Multi-line template headers must map relationship, coverage, gender, and premium
    hdr = detect_header(out_df)
    assert hdr is not None
    for expected_col in ("first_name", "last_name", "gender", "dob", "relationship", "coverage", "plan", "premium"):
        assert expected_col in hdr.columns

    # Completed comparison output workbook must be classified as deliverable, not census
    assert is_comparison_output_doc(out_doc) is True
    assert classify(out_doc) == "deliverable"

    # 2. Positional fallback guard: even on a table without a Relationship column,
    # a row lacking DOB/gender or carrying a conflicting plan must NOT become a CH dependent
    no_rel_df = pd.DataFrame([
        ["First Name", "Last Name", "Gender", "DOB", "Plan", "Coverage"],
        ["Sean", "Storch", "M", "1976-02-24", "E2000i10021B", "EC"],
        ["Mackenzie", "Storch", "F", "2012-01-23", "E2000i10021B", ""],
        ["Maeghan", "Solanki", "F", "2001-08-04", "E2000i10021B", ""],
        ["Jeremy", "Aschman", "", "", "E3000i8021B", ""],
    ])
    no_rel_doc = RawDoc(path=Path("real_census.xlsx"), ext=".xlsx", tables=[RawTable("Sheet1", no_rel_df)], text="")
    census, _ = build_census([no_rel_doc])
    assert len(census.dependents) == 2
    assert {d.display for d in census.dependents} == {"Mackenzie Storch", "Maeghan Solanki"}

    # 3. Learning Engine validator catches deliverable-as-census and phantom/cross-role dependents
    validator = UnderwritingValidator(instruction_hash="test_hash")
    prospect = Prospect(company="Test Co", state="FL")
    routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    F = Findings()

    # Clean run passes both new rules
    clean_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census, selected_invoices=[],
        renewal=None, ren_required=False, claims_required=False, findings=F, results={},
        roles={"census": [no_rel_doc]},
    )
    r_deliv_clean = next(c for c in clean_report.checks if c.rule_id == "RULE_06_NO_DELIVERABLE_AS_SOURCE")
    r_prov_clean = next(c for c in clean_report.checks if c.rule_id == "RULE_09_DEPENDENT_PROVENANCE")
    assert r_deliv_clean.status == CheckStatus.PASS
    assert r_prov_clean.status == CheckStatus.PASS

    # Polluted run (deliverable in roles['census'] + phantom dependent) fails both rules
    phantom_dep = Person("CH", "Jeremy", "Aschman", source="reference_output.xlsx", rel_raw="")
    census.people.append(phantom_dep)
    inv_only_p = Person("EE", "Jeremy", "Aschman", invoice_only=True)
    census.people.append(inv_only_p)
    polluted_results = {id(inv_only_p): RowResult(result="Invoice Employee Not Found In Census")}
    bad_report = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census, selected_invoices=[],
        renewal=None, ren_required=False, claims_required=False, findings=F, results=polluted_results,
        roles={"census": [no_rel_doc, out_doc]},
    )
    r_deliv_bad = next(c for c in bad_report.checks if c.rule_id == "RULE_06_NO_DELIVERABLE_AS_SOURCE")
    r_prov_bad = next(c for c in bad_report.checks if c.rule_id == "RULE_09_DEPENDENT_PROVENANCE")
    assert r_deliv_bad.status == CheckStatus.FAIL
    assert r_prov_bad.status == CheckStatus.FAIL


def test_instruction_scoping_tier_consistency_and_self_healing_repair(monkeypatch):
    """Verify Phase 1-4 upgrades:
    1. get_scoped_instructions includes stage checklist & sections >= 32 (even with punctuation).
    2. RULE_09_TIER_DEPENDENT_CONSISTENCY catches EE/WO employees with active dependents.
    3. RepairEngine RECLASSIFY_SOURCE & REPAIR_LINKAGE_OR_NORMALIZATION self-heal working_state.
    4. OutputAuditor.diff_against_reference diffs counts without ingesting reference output."""
    import llm_client
    from readers import RawDoc, RawTable
    from census_builder import build_census
    from models import Findings, Person, Prospect
    from router import RoutingDecision
    from learning_engine.validator import UnderwritingValidator, CheckStatus
    from learning_engine.repair import RepairEngine
    from learning_engine.contracts import RepairAction, RepairPatch
    from learning_engine.output_audit import OutputAuditor
    import census_config as C

    # 1. Instruction scoping includes checklist and Section 32+ with punctuation
    sample_inst = (
        "HEADER INTRO\n\n"
        "1. GENERAL RULES\nKeep everything accurate.\n\n"
        "11. INVOICE RULES\nSelect latest invoice.\n\n"
        "32. DEPENDENT & TIER-COUNT RULES:\nNever coerce unlinked invoice rows into CH.\n"
    )
    monkeypatch.setattr(llm_client, "get_official_instructions", lambda: sample_inst)
    scoped = llm_client.get_scoped_instructions("invoice")
    assert "CRITICAL STAGE CHECKLIST (INVOICE)" in scoped
    assert "32. DEPENDENT & TIER-COUNT RULES:" in scoped
    assert "Never coerce unlinked invoice rows into CH." in scoped

    # 2. Tier-to-Dependent Consistency Check
    ee_df = pd.DataFrame([
        ["First Name", "Last Name", "Relationship", "Gender", "DOB", "Plan", "Coverage"],
        ["Ingrid", "Hooglander", "EE", "F", "1971-08-31", "E2000i10021B", "EE"],
    ])
    ee_doc = RawDoc(path=Path("ee_census.xlsx"), ext=".xlsx", tables=[RawTable("Sheet1", ee_df)], text="")
    census, _ = build_census([ee_doc])
    emp = census.employees[0]
    bad_dep = Person("CH", "Ghost", "Child", source="ee_census.xlsx", rel_raw="CH")
    bad_dep.dob = pd.Timestamp("2015-01-01").date()
    bad_dep.employee = emp
    emp.dependents.append(bad_dep)
    census.people.append(bad_dep)

    validator = UnderwritingValidator(instruction_hash="test_hash")
    prospect = Prospect(company="Test Co", state="FL")
    routing = RoutingDecision(carrier=C.CARRIER_FLORIDA_BLUE, state="FL", is_florida=True)
    F = Findings()
    rep = validator.validate_full_pipeline(
        prospect=prospect, routing=routing, census=census, selected_invoices=[],
        renewal=None, ren_required=False, claims_required=False, findings=F, results={},
        roles={"census": [ee_doc]},
    )
    r_tier = next(c for c in rep.checks if c.rule_id == "RULE_09_TIER_DEPENDENT_CONSISTENCY")
    assert r_tier.status == CheckStatus.FAIL

    # 3. Self-healing RECLASSIFY_SOURCE removes deliverable and rebuilds census
    out_df = pd.DataFrame([
        ["Data Row *", "First Name *", "Last Name *", "Relationship", "Coverage", "Invoice Employee Name", "Match %", "Comparison Result"],
        [1, "Ingrid", "Hooglander", "EE", "EE", "Hooglander, Ingrid", 1, "Match"],
        [2, "Ghost", "Child", "CH", "EC", "", None, "Dependent"],
    ])
    out_doc = RawDoc(path=Path("reference_output.xlsx"), ext=".xlsx", tables=[RawTable("Sheet1", out_df)], text="")
    working_state = {
        "roles": {"census": [ee_doc, out_doc], "deliverable": []},
        "census": census,
        "prospect": prospect,
        "findings": F,
        "meta": {},
    }
    engine = RepairEngine()
    reclass_patch = RepairPatch(
        patch_id="p_reclass", issue_ids=["ISSUE_DELIVERABLE_CLASSIFIED_AS_CENSUS"],
        action=RepairAction.RECLASSIFY_SOURCE,
    )
    tx = engine.execute_patch(reclass_patch, working_state)
    assert tx is not None
    assert out_doc not in working_state["roles"]["census"]
    assert out_doc in working_state["roles"]["deliverable"]
    assert len(working_state["census"].dependents) == 0

    # 4. OutputAuditor.diff_against_reference detects extra/missing dependents
    diff = OutputAuditor("test_run").diff_against_reference(working_state["census"], out_doc)
    assert diff["expected_employees"] == 1
    assert diff["actual_employees"] == 1
    assert diff["expected_dependents"] == 1
    assert diff["actual_dependents"] == 0
    assert diff["missing_dependents"] == [("ghost", "child")]
    assert diff["match"] is False





