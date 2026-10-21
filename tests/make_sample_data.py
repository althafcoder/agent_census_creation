"""Creates a small synthetic submission in ./input so the pipeline can be smoke-tested."""
from pathlib import Path
import sys
import docx
import pandas as pd

out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "input")
out.mkdir(parents=True, exist_ok=True)

# GHQ (Word)
d = docx.Document()
d.add_paragraph("Group Health Questionnaire")
t = d.add_table(rows=0, cols=2)
for k, v in [("Company Name", "Acme Widgets LLC"), ("Proposed Effective Date", "01/01/2027"),
             ("SIC Code", "3499"), ("Address", "12 Main Street"), ("City", "Latham"), ("State", "NY"),
             ("Zip", "12110-1418"), ("OneDigital Referral", "Yes"), ("REI Sales Person", "Jane Rep")]:
    r = t.add_row().cells; r[0].text, r[1].text = k, v
d.save(out / "GHQ_Acme.docx")

# Employee census + dependents on a second tab
emp = pd.DataFrame([
    ["E001", "John", "Smith", "Male", "03/15/1980", "12110", "Employee Only", "Gold PPO", "N"],
    ["E002", "Mary", "Jones", "Female", "07/22/1985", "12205", "Employee + Spouse", "Gold PPO", "N"],
    ["E003", "Robert", "Brown", "M", "11/02/1975", "12110", "Family", "Silver HMO", "N"],
    ["E004", "Linda", "Davis", "F", "01/30/1990", "12208", "Employee + Child", "Gold PPO", "N"],
    ["E005", "Paul", "Green", "M", "05/05/1992", "12110", "Waiting Period", "", "N"],
    ["E006", "Susan", "White", "F", "09/09/1988", "12110", "Waived", "", "N"],
    ["E007", "Tom", "Black", "M", "04/04/1983", "1210", "Employee Only", "Silver HMO", "N"],
    ["E008", "Karen", "Gray", "F", "06/06/1979", "12110", "Employee + Spouse", "Gold PPO", "N"],
    ["E009", "Mike", "Stone", "M", "02/02/1986", "12110", "Employee Only", "Gold PPO", "N"],
    ["E001", "John", "Smith", "Male", "03/15/1980", "12110", "Employee Only", "Gold PPO", "N"],   # dup
], columns=["Employee ID", "First Name", "Last Name", "Gender", "DOB", "Zip", "Coverage Tier", "Medical Plan", "COBRA"])
dep = pd.DataFrame([
    ["E002", "Peter", "Jones", "M", "02/02/1984", "Spouse"],
    ["E003", "Ann", "Brown", "F", "03/03/1978", "Spouse"],
    ["E003", "Tim", "Brown", "M", "04/04/2010", "Child"],
    ["E004", "Zoe", "Davis", "F", "05/05/2015", "Child"],
    ["E008", "Gary", "Gray", "M", "08/08/1978", "Spouse"],
    ["E008", "Lily", "Gray", "F", "08/08/2012", "Child"],     # ES but has a child -> structure flag
    ["E999", "Nobody", "Orphan", "F", "01/01/2000", "Child"],  # unassigned
], columns=["Subscriber ID", "Dependent First Name", "Dependent Last Name", "Gender", "Date of Birth", "Relationship"])
with pd.ExcelWriter(out / "Acme_Employee_Census.xlsx") as w:
    emp.to_excel(w, sheet_name="Employees", index=False)
    dep.to_excel(w, sheet_name="Dependents", index=False)

# Current invoice (Oct 2026) - xlsx with a title block above the table
rows = [["Acme Widgets LLC - Premium Invoice"], ["Billing Period: 10/01/2026 - 10/31/2026"], [],
        ["Member Name", "Date of Birth", "Gender", "Zip", "Tier", "Plan", "Premium", "Status"],
        ["SMITH, JOHN A", "03/15/1980", "M", "12110", "EE", "Gold PPO", 500.00, "Active"],
        ["Jones, Mary", "07/22/1985", "F", "12205", "ES", "Gold PPO", 950.00, "Active"],
        ["Brown Robert", "11/02/1975", "M", "12110", "ES", "Silver HMO", 800.00, "Active"],   # tier mismatch (census FAM)
        ["Davis, Lynda", "01/30/1990", "F", "12208", "EC", "Gold PPO", 800.00, "Active"],   # fuzzy name
        ["White, Susan", "09/09/1988", "F", "12110", "EE", "Gold PPO", 500.00, "Active"],   # WO but on invoice
        ["Black, Tom", "04/04/1983", "M", "12110", "EE", "Silver HMO", 450.00, "Active"],
        ["Newhire, Nina", "10/10/1995", "F", "12110", "EE", "Gold PPO", 500.00, "Active"],  # invoice only
        ["Black, Tom", "", "", "", "EE", "Silver HMO", -450.00, "Retro adjustment"],
        ["Total", "", "", "", "", "", 3700.00, ""]]
pd.DataFrame(rows).to_excel(out / "Invoice_Oct2026.xlsx", header=False, index=False)

# Older superseded invoice (Sep 2026)
rows[1] = ["Billing Period: 09/01/2026 - 09/30/2026"]
pd.DataFrame(rows).to_excel(out / "Invoice_Sep2026.xlsx", header=False, index=False)

# Renewal (future period) - wide layout
ren = [["Renewal Rates - FINAL"], ["Renewal Effective Date: 01/01/2027"], [],
       ["Plan", "EE", "ES", "EC", "FAM"],
       ["Gold PPO", 550, 1045, 880, 1500],
       ["Silver HMO", 495, 880, 740, 1300]]
pd.DataFrame(ren).to_excel(out / "Renewal_2027_Final.xlsx", header=False, index=False)
print("sample data ->", out)
