import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import openpyxl
import pandas as pd
from classifier import FIELD_ALIASES, detect_header
from readers import RawDoc, RawTable
from census_builder import build_census

# Apply alias additions
FIELD_ALIASES["last_name"].extend(["legallastname", "preferredorchosenlast", "primarylastname"])
FIELD_ALIASES["first_name"].extend(["legalfirstname", "preferredorchosenfirst", "primaryfirstname"])
FIELD_ALIASES["zip"].extend(["primaryaddresszipcode", "primaryaddresszip", "residentpostalcode", "postalcode", "zipcode"])
FIELD_ALIASES["address"].extend(["primaryaddressline1", "primaryaddress", "addressline1"])
FIELD_ALIASES["city"].extend(["primaryaddresscity", "residentcity"])
FIELD_ALIASES["state"].extend(["primaryaddressstateterritory", "primaryaddressstate", "livedinstate", "workedinstate"])
FIELD_ALIASES["coverage"].extend(["coveragelevelvalue", "coveragelevelamount", "coveragelevel", "medicalcoverage", "medicaltier"])
FIELD_ALIASES["relationship"].extend(["dependentrelationship", "deprelationship", "reltoemployee"])
FIELD_ALIASES["sub_id"].extend(["taxid", "employeetaxid", "positionid"])
FIELD_ALIASES["plan_type"] = ["plantype", "plancategory", "benefitcategory", "benefittype"]

p = Path(r"C:\Users\c1755\Documents\copilot_new_agent\SLM_Waste_and_Recycling_Services_Inc\SLM_Waste_and_Recycling_Services_Inc")

docs = []
for fn in ["SLM employee census.xlsx", "SLM Employee and Dependent Enrollments.xlsx"]:
    wb = openpyxl.load_workbook(p / fn, data_only=True)
    ws = wb.active
    data = [list(r) for r in ws.iter_rows(values_only=True)]
    df = pd.DataFrame(data).dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    d = RawDoc(p / fn, ".xlsx", [RawTable(ws.title, df)])
    docs.append(d)

census, meta = build_census(docs)
print("=== CENSUS BUILD RESULT ===")
print("Employees count:", len(census.employees))
print("Dependents count:", len(census.dependents))
print("Meta:", meta)
print("\nFirst 10 people in census:")
for idx, person in enumerate(census.people[:15], 1):
    print(f"  {idx}: Name={person.first} {person.last} | Role={person.role} | Cov={person.coverage} | Plan={person.plan} | Zip={person.zip}")
