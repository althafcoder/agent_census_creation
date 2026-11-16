"""Golden regression test for the BraceLab LLC / Raleigh Hand Rehabilitation Center submission.

Verifies all 5 client feedback items using 100% dynamic, structural rules:
  1. Dependent eligibility (wide multi-benefit carrier census):
     Dental/vision-only dependents (Guilherme Duarte = Dental/Vision only; Karen Hydar = Vision only)
     must be excluded from medical dependents, resulting in:
       - Alla Duarte -> EC (1 medical child: Madison Lennox Duarte), matching Invoice E1D ($1,260.61)
       - Jeffrey Hydar -> EE (0 medical dependents), matching Invoice E ($1,177.37)
  2. Duplicate employee identification (full middle name matching):
     Census 'Jeffrey Hydar' (Middle Name = 'Joseph') must match Invoice 'Hydar, Jeffrey Joseph'
     at 100% as a single employee (not split into an unbilled census employee + invoice-only employee),
     and 'Courtney Roupe' (Middle Name = 'Elizabeth') must match 'Roupe, Courtney Elizabeth' at 100%.
  3. SBC validation:
     Provided SBCs (EOT6 / K35S, EOSY / E83, EOUJ / K65S linked to EO-UI/RXK65S) must match the 3 active
     medical plans and must NOT be requested as missing in the Missing Information Email.
  4. Renewal extraction (member-level Appendix A & plan mapping):
     Renewal plan mapping (EO-T6/RXK35S -> FC-IX/RXK35S, EO-SY/RXE83 -> FC-HF/RXE83, EO-UI/RXK65S -> FC-I8/RXK65S)
     and member-level renewal rates must be captured for all enrolled employees with 0 missing renewal rates.
  5. Missing-information review:
     Legal-entity mismatch (BraceLab LLC vs RALEIGH HAND REHABILITATION CENTER, INC) and missing SIC code
     must be included in the final Missing Information Email.
"""
from datetime import date
from pathlib import Path
import sys
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from census_builder import build_census, refine_non_enrolled_statuses  # noqa: E402
from comparator import run_comparison  # noqa: E402
from invoice_parser import combine_records, parse_invoice, select_invoices  # noqa: E402
from models import Findings  # noqa: E402
from prospect import build_prospect  # noqa: E402
from readers import RawDoc, RawTable  # noqa: E402
from renewal_parser import parse_renewal, select_renewal  # noqa: E402
from router import determine_routing, generate_missing_information_email  # noqa: E402


def _build_bracelab_census_doc() -> RawDoc:
    headers = [
        "Company Name", "Member Number", "Last Name", "First Name", "Middle Name",
        "Relationship Desc", "Date of Birth", "Gender", "Status", "Zip", "Date of Hire",
        "Medical Coverage Effective Date", "Medical Plan ID", "Medical Plan Desc", "Medical External Marketing Name",
        "Dental Coverage Effective Date", "Dental Plan ID", "Dental Plan Desc",
        "Vision Coverage Effective Date", "Vision Plan ID", "Vision Plan Desc",
    ]
    rows = [
        headers,
        # 1. Christine Abourisk (EE) + Nolan Abourisk (CH, Medical+Dental) -> EC
        ["RALEIGH HAND REHABILITATION CENTER, INC", "572132484", "ABOURISK", "CHRISTINE", "L", "Employee", "01/04/1990", "F", "Active", "37922", "02/01/2018", "01/01/2026", "EOT6", "NC G CHC + NG 30/60/2000/80 POS 26 EOT6", "CHOICE PLUS EOT6", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "572132484", "ABOURISK", "NOLAN", "", "Child", "02/15/2022", "M", "Active", "37922", "", "01/01/2026", "EOT6", "NC G CHC + NG 30/60/2000/80 POS 26 EOT6", "CHOICE PLUS EOT6", "01/01/2026", "P4878", "Dental Contributory P4878", "", "", ""],
        # 2. Nicholas Chevalley (EE, COBRA) -> EE
        ["RALEIGH HAND REHABILITATION CENTER, INC", "602309684", "CHEVALLEY", "NICHOLAS", "E", "Employee", "02/24/1995", "M", "COBRA", "37919", "10/01/2019", "01/01/2026", "EOSY", "NC S CHC + NG 6000/100 POS HSA 26 EOSY", "CHOICE PLUS EOSY", "", "", "", "", "", ""],
        # 3. Jerry Ditz (EE) + Caroline (SP) + 3 Children -> FAM
        ["RALEIGH HAND REHABILITATION CENTER, INC", "468309836", "DITZ", "JERRY", "J", "Employee", "05/12/1984", "M", "Active", "27609", "08/01/2014", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "468309836", "DITZ", "CAROLINE", "", "Spouse", "10/04/1985", "F", "Active", "27609", "", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "468309836", "DITZ", "ANDREW", "", "Child", "02/06/2014", "M", "Active", "27609", "", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "468309836", "DITZ", "HENRY", "", "Child", "01/26/2016", "M", "Active", "27609", "", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "468309836", "DITZ", "THOMAS", "", "Child", "03/03/2020", "M", "Active", "27609", "", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        # 4. Alla Duarte (EE) + Guilherme Duarte (Spouse, DENTAL/VISION ONLY!) + Madison Lennox Duarte (Child, Medical+Dental) -> MUST BE EC!
        ["RALEIGH HAND REHABILITATION CENTER, INC", "440262467", "DUARTE", "ALLA", "", "Employee", "12/06/1983", "F", "Active", "28411", "01/29/2018", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "440262467", "DUARTE", "GUILHERME", "", "Spouse", "05/12/1985", "M", "Active", "28411", "", "", "", "", "", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "440262467", "DUARTE", "MADISON LENNOX", "", "Child", "01/19/2023", "F", "Active", "28411", "", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "", "", ""],
        # 5. Troy Ektnitphong (EE) -> EE
        ["RALEIGH HAND REHABILITATION CENTER, INC", "362794212", "EKTNITPHONG", "TROY", "S", "Employee", "01/21/1996", "M", "Active", "37771", "01/09/2023", "01/01/2026", "EOSY", "NC S CHC + NG 6000/100 POS HSA 26 EOSY", "CHOICE PLUS EOSY", "", "", "", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        # 6. Jeffrey Hydar (EE, Middle='Joseph') + Karen Hydar (Spouse, VISION ONLY!) -> MUST BE EE & match Hydar, Jeffrey Joseph!
        ["RALEIGH HAND REHABILITATION CENTER, INC", "796540229", "Hydar", "Jeffrey", "Joseph", "Employee", "12/06/1973", "M", "Active", "37934", "07/01/2024", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        ["RALEIGH HAND REHABILITATION CENTER, INC", "796540229", "Hydar", "Karen", "", "Spouse", "10/30/1967", "F", "Active", "37934", "", "", "", "", "", "", "", "", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        # 7. Brian Levine (EE) -> EE
        ["RALEIGH HAND REHABILITATION CENTER, INC", "482036274", "LEVINE", "BRIAN", "T", "Employee", "07/16/1996", "M", "Active", "37919", "01/21/2022", "01/01/2026", "EOSY", "NC S CHC + NG 6000/100 POS HSA 26 EOSY", "CHOICE PLUS EOSY", "01/01/2026", "P4878", "Dental Contributory P4878", "", "", ""],
        # 8. Lauren Parker (EE) -> EE
        ["RALEIGH HAND REHABILITATION CENTER, INC", "698473783", "Parker", "Lauren", "", "Employee", "10/09/1989", "F", "Active", "28607", "07/15/2024", "01/01/2026", "EOSY", "NC S CHC + NG 6000/100 POS HSA 26 EOSY", "CHOICE PLUS EOSY", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
        # 9. Courtney Roupe (EE, Middle='Elizabeth') -> EE & match Roupe, Courtney Elizabeth!
        ["RALEIGH HAND REHABILITATION CENTER, INC", "495858854", "Roupe", "Courtney", "Elizabeth", "Employee", "03/17/1993", "F", "Active", "37772", "02/24/2025", "01/01/2026", "EOUI", "NC P CHC + NG 10/20/1000/100 POS 26 EOUI", "CHOICE PLUS EOUI", "01/01/2026", "P4878", "Dental Contributory P4878", "01/01/2026", "S1006", "Vision 100% Voluntary S1006"],
    ]
    df = pd.DataFrame(rows)
    return RawDoc(
        path=Path("BraceLab_Company_Census_Report_2026 - MembersListReport.xlsx"),
        ext=".xlsx",
        tables=[RawTable("Sheet0", df)],
        text="",
    )


_BRACELAB_INVOICE_TEXT = """
RALEIGH HAND REHABILITATION CENTER, INC
Customer No: 1576396
Invoice No: 515783023142
Invoice Date: 10/13/2026
Bill Group: 1750941
Coverage Period: 11/01/2026 - 11/30/2026
Due Date: 11/01/2026
Summary
Description Employee Count Total Volume (000's) Net Amount
1240091-RALEIGH HAND REHABILITATION CENTER, INC
NC G CHC + NG 30/60/2000/80 POS 26 EOT6
Employee & One Dependent 1 $1,047.21
Subtotal, NC G CHC + NG 30/60/2000/80 POS 26 EOT6 1 $1,047.21
NC P CHC + NG 10/20/1000/100 POS 26 EOUI
Employee & Family 1 $2,940.41
Employee & One Dependent 1 $1,260.61
Employee 2 $1,890.91
Subtotal, NC P CHC + NG 10/20/1000/100 POS 26 EOUI 4 $6,091.93
NC S CHC + NG 6000/100 POS HSA 26 EOSY
Employee 5 $2,714.96
Subtotal, NC S CHC + NG 6000/100 POS HSA 26 EOSY 5 $2,714.96
Subtotal 1240091-RALEIGH HAND REHABILITATION CENTER, INC $10,631.28

Details
Current Detail - 11/01-11/30/2026 Adjustment Detail Totals
Policy No. Name Plan ID Coverage Status Vol (000's) Charge Amount Period Code Amount Total
1240091 ABOURISK, CHRISTINE L Dental Contributory *****8400 E1D A $120.11 $1,175.52
P4878
1240091 ABOURISK, CHRISTINE L NC G CHC + NG *****8400 E1D A $1,047.21
30/60/2000/80 POS 26
EOT6
1240091 ABOURISK, CHRISTINE L Vision 100% Voluntary *****8400 E A $8.20
S1006
1240091 CHEVALLEY, NICHOLAS E NC S CHC + NG *****8400 E C $545.30 $545.30
6000/100 POS HSA 26
EOSY
1240091 DITZ, JERRY J Dental Contributory *****3600 ESC A $179.23 $3,145.29
P4878
1240091 DITZ, JERRY J NC P CHC + NG *****3600 ESC A $2,940.41
10/20/1000/100 POS 26
EOUI
1240091 DITZ, JERRY J Vision 100% Voluntary *****3600 ESC A $25.65
S1006
1240091 DUARTE, ALLA Dental Contributory *****6700 ESC A $179.23 $1,455.39
P4878
1240091 DUARTE, ALLA Vision 100% Voluntary *****6700 ES A $15.55
S1006
1240091 DUARTE, ALLA NC P CHC + NG *****6700 E1D A $1,260.61
10/20/1000/100 POS 26
EOUI
1240091 EKTNITPHONG, TROY S NC S CHC + NG *****1200 E A $537.61 $545.81
6000/100 POS HSA 26
EOSY
1240091 EKTNITPHONG, TROY S Vision 100% Voluntary *****1200 E A $8.20
S1006
1240091 Esposito, Autumn NC S CHC + NG *****3400 E A $503.50 $503.50
6000/100 POS HSA 26
EOSY
1240091 Hydar, Jeffrey Joseph Vision 100% Voluntary *****2900 ESC A $25.65 $1,252.76
S1006
1240091 Hydar, Jeffrey Joseph Dental Contributory *****2900 E A $49.74
P4878
1240091 Hydar, Jeffrey Joseph NC P CHC + NG *****2900 E A $1,177.37
10/20/1000/100 POS 26
EOUI
1240091 LEVINE, BRIAN T NC S CHC + NG *****7400 E A $537.61 $587.35
6000/100 POS HSA 26
EOSY
1240091 LEVINE, BRIAN T Dental Contributory *****7400 E A $49.74
P4878
1240091 Parker, Lauren NC S CHC + NG *****8300 E A $590.94 $648.88
6000/100 POS HSA 26
EOSY
1240091 Parker, Lauren Dental Contributory *****8300 E A $49.74
P4878
1240091 Parker, Lauren Vision 100% Voluntary *****8300 E A $8.20
S1006
1240091 Roupe, Courtney Elizabeth Dental Contributory *****5400 E A $49.74 $771.48
P4878
1240091 Roupe, Courtney Elizabeth NC P CHC + NG *****5400 E A $713.54
10/20/1000/100 POS 26
EOUI
1240091 Roupe, Courtney Elizabeth Vision 100% Voluntary *****5400 E A $8.20
S1006
Total $10,631.28 $0.00 $10,631.28
"""

_BRACELAB_RENEWAL_TEXT = """
Personal Overview
RALEIGHHANDREHABILITATIONCENTER,INC
Groupnumber:1576396
Currentpackage:RENEWAL/NC26B
Renewaldate:01/01/2027

CurrentMedicalPlansRENEWAL/NC26B-presentedfromleantorich
OpenAccessHSA EO-SY/RXE831,2 S $6,000/$12,000 $12,000/$24,000 $6,000/$12,000 $12,000/$24,000 NA/NA CHOICEPLUS
OpenAccessDirect EO-T6/RXK35S1 $2,000/$4,000 $4,000/$8,000 $6,000/$12,000 $12,000/$24,000 $30/$60 CHOICEPLUS
OpenAccessDirect EO-UI/RXK65S1 $1,000/$2,000 $2,000/$4,000 $2,500/$5,000 $5,000/$10,000 $10/$20 CHOICEPLUS
RenewalMedicalPlansRENEWAL/NC27B-presentedfromleantorich
OpenAccessHSA FC-HF/RXE831,2 S $6,000/$12,000 $12,000/$24,000 $6,000/$12,000 $12,000/$24,000 NA/NA CHOICEPLUS
OpenAccessDirect FC-IX/RXK35S1 $2,000/$4,000 $4,000/$8,000 $6,000/$12,000 $12,000/$24,000 $30/$60 CHOICEPLUS
OpenAccessDirect FC-I8/RXK65S1 $1,000/$2,000 $2,000/$4,000 $3,000/$6,000 $5,000/$10,000 $15/$30 CHOICEPLUS

Appendix A: Employee enrollment detail and rates
                                               Current            Renewal                
                                Dep Empl Spo Child                                       
     CoveredEmployee     Age Sex Cov† Status Age Count PlanName Premium PlanName Premium 
     CHRISTINELABOURISK  36  F   C   A      1  EO-T6/RXK35S $1,051.43 FC-IX/RXK35S $1,246.86
     NICHOLASECHEVALLEY  31  M   E   C         EO-SY/RXE83 $556.83 FC-HF/RXE83 $664.36   
     JERRYJDITZ          42  M   F   A  41  3  EO-UI/RXK65S $2,968.76 FC-I8/RXK65S $3,453.57
     ALLADUARTE          43  F   F   A  41  1  EO-UI/RXK65S $1,279.91 FC-I8/RXK65S $1,488.92
     AutumnEsposito      28  F   E   A         EO-SY/RXE83 $522.24 FC-HF/RXE83 $623.09   
     TROYSEKTNITPHONG    30  M   E   A         EO-SY/RXE83 $545.30 FC-HF/RXE83 $650.60   
     JeffreyJHydar       53  M   F   A  59  1  EO-UI/RXK65S $1,230.45 FC-I8/RXK65S $1,431.39
     BRIANTLEVINE        30  M   E   A         EO-SY/RXE83 $545.30 FC-HF/RXE83 $650.60   
     LaurenParker        37  F   E   A         EO-SY/RXE83 $594.78 FC-HF/RXE83 $709.65   
     CourtneyERoupe      33  F   E   A         EO-UI/RXK65S $722.59 FC-I8/RXK65S $840.59 
                                       TotalPremium  $10,017.59         $11,759.63       
CustomerName: RALEIGHHANDREHABILITATIONCENTER,INC State: NC
"""

_BRACELAB_EMAIL_TEXT = """
From: Karen Schneider <Karen.Schneider@DecisionHR.com>
Sent: Friday, November 13, 2026 4:59 PM
Subject: BraceLab, new medical opp
Please run:
Cigna Master
Cigna Captive
Company Legal Entity Name: BraceLab LLC
EIN (Employer Identification Number): 561544502
Company Domain: https://www.bracelab.com
HQ Legal Address: 4700 Homewood Ct, Raleigh, NC 27609, — Suite 112
Current Medical Carrier: United Healthcare
Medical Renewal Date: 01/01/2027
ED: 1/1/27
Current PEO: ADP Run
Broker: PairPEO (20% admin + $30/25 PEPM)
"""


def test_bracelab_all_five_client_feedback_items(monkeypatch):
    import census_config
    monkeypatch.setattr(census_config, "USE_LLM_FALLBACK", False)
    monkeypatch.setattr(census_config, "LLM_FIRST", False)

    census_doc = _build_bracelab_census_doc()
    inv_raw_doc = RawDoc(path=Path("BraceLab_Benefit_Invoice_United Health - invoice_515783023142.pdf"), ext=".pdf", text=_BRACELAB_INVOICE_TEXT)
    ren_raw_doc = RawDoc(path=Path("BraceLab_Medical_Renewal_2027 RENEWALS0120270201PRP000IU922227.pdf"), ext=".pdf", text=_BRACELAB_RENEWAL_TEXT)
    rfp_raw_doc = RawDoc(path=Path("Original_Email.pdf"), ext=".pdf", text=_BRACELAB_EMAIL_TEXT)

    sbc_eot6 = RawDoc(
        path=Path("BraceLab_SBCs__Summary_of_Benefits__SBC_FI_NC_EOT6_K35S_2026-11-13.pdf"),
        ext=".pdf",
        text="Summary of Benefits and Coverage: Choice Plus EOT6 / K35S | Plan Type: POS | Network: $2,000 Individual / $4,000 Family | $30 copay | $60 copay | 20% coinsurance",
    )
    sbc_eosy = RawDoc(
        path=Path("BraceLab_SBCs__Summary_of_Benefits__SBC_FI_NC_EOSY_E83_2026-11-13.pdf"),
        ext=".pdf",
        text="Summary of Benefits and Coverage: Choice Plus EOSY / E83 | Plan Type: POS | Network: $6,000 Individual / $12,000 Family | No Charge",
    )
    sbc_eouj = RawDoc(
        path=Path("BraceLab_SBCs__Summary_of_Benefits__SBC_FI_NC_EOUJ_K65S_2026-11-13.pdf"),
        ext=".pdf",
        text="Summary of Benefits and Coverage: Choice Plus EOUJ / K65S | Plan Type: POS | Network: $1,000 Individual / $2,000 Family | $10 copay | $20 copay | No Charge",
    )

    roles = {
        "census": [census_doc],
        "invoice": [inv_raw_doc],
        "renewal": [ren_raw_doc],
        "rfp": [rfp_raw_doc],
        "plan_design": [sbc_eot6, sbc_eosy, sbc_eouj],
    }
    all_docs = [census_doc, inv_raw_doc, ren_raw_doc, rfp_raw_doc, sbc_eot6, sbc_eosy, sbc_eouj]

    # 1. Prospect & Routing
    prospect, *_ = build_prospect([], [rfp_raw_doc], all_docs)
    assert prospect.company == "BraceLab LLC"
    assert prospect.sic == ""
    routing = determine_routing(prospect, all_docs)

    # 2. Census Build & Issue 1 (Dental/vision-only dependents excluded -> Alla Duarte = EC, Jeffrey Hydar = EE)
    census, meta = build_census([census_doc], effective_date=date(2027, 1, 1))
    refine_non_enrolled_statuses(census, reference_date=date(2026, 11, 30), effective_date=date(2027, 1, 1))

    emp_by_name = {f"{e.first.lower()} {e.last.lower()}": e for e in census.canonical_census_employees}
    assert len(census.canonical_census_employees) == 9

    alla = emp_by_name["alla duarte"]
    assert alla.coverage == "EC", f"Alla Duarte should be EC (only child Madison on medical), got {alla.coverage}"
    assert [d.display.lower() for d in alla.dependents] == ["madison lennox duarte"]

    jeffrey = emp_by_name["jeffrey hydar"]
    assert jeffrey.coverage == "EE", f"Jeffrey Hydar should be EE (spouse Karen is Vision-only), got {jeffrey.coverage}"
    assert len(jeffrey.dependents) == 0

    excluded_dep_names = {d.display.lower() for d in census.excluded_dependents}
    assert "guilherme duarte" in excluded_dep_names
    assert "karen hydar" in excluded_dep_names

    # 3. Invoice & Renewal Parsing (Issue 4)
    inv_doc = parse_invoice(inv_raw_doc)
    selected_invs, _, _ = select_invoices([inv_doc])
    inv_end = selected_invs[0].period_end if selected_invs else None
    inv_start = selected_invs[0].period_start if selected_invs else None
    inv_records = combine_records(selected_invs)
    assert len(inv_records) == 10

    ren_doc = parse_renewal(ren_raw_doc)
    selected_ren, _ = select_renewal([ren_doc], inv_end, inv_start)
    assert selected_ren is not None
    assert selected_ren.effective == date(2027, 1, 1)
    assert selected_ren.plan_mapping == {
        "EO-T6/RXK35S": "FC-IX/RXK35S",
        "EO-SY/RXE83": "FC-HF/RXE83",
        "EO-UI/RXK65S": "FC-I8/RXK65S",
    }

    # 4. Comparison (Issue 2: Jeffrey Hydar matches Hydar, Jeffrey Joseph at 100%; Courtney Roupe matches at 100%; Issue 4: 0 missing renewal rates)
    F = Findings()
    results = run_comparison(census, inv_records, selected_ren, meta, F, have_invoice=True, renewal_required=True)

    # Issue 2 verification: Jeffrey Hydar is matched at 100% (NOT duplicated as invoice-only!)
    rr_jeff = results[id(jeffrey)]
    assert rr_jeff.match_pct == 100
    assert rr_jeff.result == "Match"
    assert rr_jeff.inv_premium == 1177.37
    assert rr_jeff.renewal_premium == 1431.39

    # Alla Duarte matches at 100% with 0 tier mismatch!
    rr_alla = results[id(alla)]
    assert rr_alla.match_pct == 100
    assert rr_alla.result == "Match"
    assert rr_alla.inv_premium == 1260.61
    assert rr_alla.renewal_premium == 1488.92

    # Courtney Roupe matches at 100% (not 62% Name Mismatch)
    courtney = emp_by_name["courtney roupe"]
    rr_courtney = results[id(courtney)]
    assert rr_courtney.match_pct == 100
    assert rr_courtney.result == "Match"
    assert rr_courtney.renewal_premium == 840.59

    # Only 1 invoice-only employee: Autumn Esposito
    inv_only_emps = [p for p in census.people if p.invoice_only]
    assert len(inv_only_emps) == 1
    assert inv_only_emps[0].display == "Autumn Esposito"
    assert results[id(inv_only_emps[0])].renewal_premium == 623.09

    # Issue 4 verification: 0 missing renewal rates and 0 unmapped plans
    assert F.counters.get("missing_renewal", 0) == 0
    assert len(F.renewal_unmapped_plans) == 0
    assert all(results[id(e)].renewal_premium is not None for e in census.canonical_census_employees)

    # 5. Missing Information Email (Issue 3: No SBCs requested; Issue 5: Legal-entity mismatch & missing SIC included)
    email_txt = generate_missing_information_email(
        prospect=prospect,
        routing=routing,
        findings=F,
        census=census,
        have_census=True,
        have_invoice=True,
        have_renewal=True,
        renewal_required=True,
        claims_required=False,
        roles=roles,
        selected_invoices=selected_invs,
        invoice_records=inv_records,
        results=results,
        docs=all_docs,
    )

    # Issue 3: SBCs were already provided, so SBC section must NOT appear
    assert "Please provide complete SBCs" not in email_txt
    assert "EOT6" not in email_txt
    assert "EOUI" not in email_txt
    assert "EOSY" not in email_txt

    # Issue 2: Jeffrey Hydar must NOT be reported as missing from invoice or missing from census
    assert "Jeffrey Hydar" not in email_txt
    assert "Hydar, Jeffrey Joseph" not in email_txt

    # Issue 5: Legal-entity mismatch and missing SIC must be included
    assert "Legal Entity Mismatch" in email_txt
    assert "BraceLab LLC" in email_txt
    assert "RALEIGH HAND REHABILITATION CENTER, INC" in email_txt
    assert "Missing SIC Code" in email_txt
