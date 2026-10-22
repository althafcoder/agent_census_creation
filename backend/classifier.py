"""Header detection / field mapping and document-role classification."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from readers import RawDoc, RawTable
from utils import clean, norm_key

# canonical field -> normalised header aliases
FIELD_ALIASES: dict[str, list[str]] = {
    "first_name": ["firstname", "first", "fname", "givenname", "employeefirstname", "eefirstname",
                   "subscriberfirstname", "memberfirstname", "firstnamemi", "legalfirstname",
                   "preferredorchosenfirst", "primaryfirstname", "eefirst", "employeefirst",
                   "payrollnamefirstname", "payrollfirstname", "employeelegalfirstname", "payrollnamefirst"],
    "last_name": ["lastname", "last", "lname", "surname", "familyname", "employeelastname",
                  "eelastname", "subscriberlastname", "memberlastname", "legallastname",
                  "preferredorchosenlast", "primarylastname", "eelast", "employeelast",
                  "payrollnamelastname", "payrolllastname", "employeelegallastname", "payrollnamelast"],
    "full_name": ["name", "fullname", "employeename", "membername", "subscribername", "employee",
                  "eename", "insuredname", "member", "subscriber", "participant", "enrolleename",
                  "lastfirst", "lastnamefirstname", "namelastfirst"],
    "gender": ["gender", "sex", "mf", "gendermf", "gendercode", "genderdescription"],
    "dob": ["dob", "dateofbirth", "birthdate", "birthday", "bdate", "birthdt", "memberdob", "dateofbirthmmddyyyy",
            "employeedateofbirth", "employeedob"],
    "zip": ["zip", "zipcode", "homezipcode", "postalcode", "homezip", "zip5", "residentzip",
            "primaryaddresszipcode", "primaryaddresszip", "residentzipcode", "residentpostalcode",
            "homepostalcode", "homezipcode", "residentpostal", "legalpreferredaddresszippostalcode",
            "zippostalcode", "zippostal", "legalpreferredaddresszip"],
    "relationship": ["relationship", "relationshiptoemployee", "rel", "relation", "relationshiptosubscriber",
                     "membertype", "relcode", "relationshipcode", "depcode", "relationshiptoee",
                     "dependentrelationship", "deprelationship", "reltoemployee", "relationshiptosubscriber", "deprel",
                     "dependentbeneficiaryrelationship", "dependentbeneficiaryrel", "depbeneficiaryrel"],
    "coverage": ["coverage", "coveragetier", "tier", "coveragelevel", "enrollmenttier", "plantier",
                 "coveragetype", "election", "coveragecode", "enrollmentlevel", "benefittier", "ratetier",
                 "householdtier", "medicaltier", "coveragestatus", "coveragelevelvalue", "coveragelevelamount",
                 "medicalcoverage"],
    "status": ["status", "employeestatus", "memberstatus", "billingstatus", "policystatus",
               "enrollmentstatus", "enrolled", "coveragestatus", "medicalstatus", "medicalenrollment"],
    "cobra": ["cobra", "cobraparticipant", "cobrastatus", "cobraindicator"],
    "plan": ["plan", "planname", "medicalplan", "plancode", "planelected", "medicalplanname", "product",
             "productname", "planenrolled", "currentplan", "planoption", "medicalplancode", "plandescription",
             "currentmedicalplan", "enrolledplan", "medicalplanoption"],
    "plan_type": ["plantype", "benefittype", "lineofcoverage", "producttype", "benefitplantype"],
    "plan_category": ["plancategory", "benefitcategory", "productcategory", "benefitplancategory"],
    "carrier": ["carrier", "provider", "carriercode", "carriername", "insurancecarrier", "vendor"],
    "ee_cost": ["employeecost", "eecost", "employeededuction", "eededuction"],
    "er_cost": ["employercost", "ercost", "employercostamount", "companycost", "employercontribution"],
    "premium": ["premium", "monthlypremium", "totalpremium", "amount", "charge", "billedamount",
                "currentpremium", "rate", "monthlytotalpremium", "totalcharge", "billedpremium",
                "currentcharge", "totalamount", "currentamountdue", "monthlycost", "premiumamount",
                "currentrate", "billedrate", "totalbilled", "totalcost", "cost", "totalamountbilled", "billedcost"],
    "sub_id": ["subscriberid", "memberid", "employeeid", "eeid", "empid", "id", "ssn", "ssnlast4",
               "certificate", "certno", "certificatenumber", "memberno", "subscriberssn", "employeessn",
               "eessn", "subscribernumber", "employeenumber", "empno", "idnumber", "ssn4", "last4ssn",
               "householdid", "hhid", "familyid", "taxid", "employeetaxid", "positionid"],
    "hire_date": ["hiredate", "dateofhire", "doh", "startdate", "originalhiredate"],
    "effective_date": ["effective_date", "effectivedate", "effdate", "enrollmenteffectivedate", "enrollmenteffdate",
                       "coverageeffectivedate", "coverageeffdate", "benefiteffectivedate", "benefiteffdate",
                       "enrollmentstartdate"],
    "txn_type": ["type", "transactiontype", "chargetype", "description", "linetype", "adjustmenttype",
                 "billingtype", "activity"],
    # explicit dependent columns (dependent listing files)
    "dep_first": ["dependentfirstname", "depfirstname", "dependentfirst", "depfirst", "dependentfname", "depfname"],
    "dep_last": ["dependentlastname", "deplastname", "dependentlast", "deplast", "dependentlname", "deplname"],
    "dep_full": ["dependentname", "depname", "dependent", "dependentfullname", "dependentbeneficiaryname", "dependentbeneficiary", "depbeneficiaryname"],
    "dep_dob": ["dependentdob", "depdob", "dependentdateofbirth", "depdateofbirth", "dependentbirthdate", "dependentbeneficiarydateofbirth", "dependentbeneficiarydob", "depbeneficiarydob"],
    "dep_gender": ["dependentgender", "depgender", "dependentsex", "depsex", "dependentbeneficiarygender", "depbeneficiarygender", "dependentbeneficiarysex"],
    # employee reference columns inside dependent files
    "emp_first": ["employeefirstname", "subscriberfirstname", "eefirst", "empfirstname", "primaryfirstname"],
    "emp_last": ["employeelastname", "subscriberlastname", "eelast", "emplastname", "primarylastname"],
    "emp_full": ["employeename", "subscribername", "eename", "primaryname", "policyholder", "primarymember",
                 "subscriber", "employee"],
    "address": ["address", "streetaddress", "address1", "street", "primaryaddressline1", "primaryaddress"],
    "city": ["city", "town", "primaryaddresscity", "residentcity"],
    "state": ["state", "st", "primaryaddressstateterritory", "primaryaddressstate", "livedinstate", "residentstate"],
}

# Premium vs amount wording that must NOT be treated as a total premium
_RETRO_WORDS = re.compile(r"(retro|adjust|credit|refund|prior\s*period|reinstat)", re.I)

# wide-format dependents: spouse_first / child1dob ...
_WIDE_DEP = re.compile(r"^(spouse|sp|child|ch|dependent|dep)(\d*)(firstname|first|lastname|last|name|dob|"
                       r"dateofbirth|birthdate|gender|sex)$")
_WIDE_DEP_REV = re.compile(r"^(firstname|first|lastname|last|name|dob|dateofbirth|birthdate|gender|sex)"
                           r"(spouse|sp|child|ch|dependent|dep)(\d*)$")


@dataclass
class Header:
    row_idx: int
    columns: dict[str, int]                        # canonical field -> column index
    wide_deps: dict[tuple[str, str], dict[str, int]]   # (kind, n) -> {field: col}
    labels: list[str]


def _score_row(vals: list[str]) -> int:
    keys = [norm_key(v) for v in vals]
    flat = {a for al in FIELD_ALIASES.values() for a in al}
    return sum(1 for k in keys if k and k in flat)


def detect_header(df: pd.DataFrame, max_scan: int = 40) -> Optional[Header]:
    best_i, best_s = None, 0
    for i in range(min(max_scan, len(df))):
        s = _score_row([clean(v) for v in df.iloc[i].tolist()])
        if s > best_s:
            best_i, best_s = i, s
    if best_i is None or best_s < 2:
        return None
    labels = [clean(v) for v in df.iloc[best_i].tolist()]
    cols: dict[str, int] = {}
    wide: dict[tuple[str, str], dict[str, int]] = {}
    for idx, lab in enumerate(labels):
        k = norm_key(lab)
        if not k:
            continue
        m = _WIDE_DEP.match(k)
        if m and m.group(1) in {"dependent", "dep"} and not m.group(2):
            m = None                      # 'Dependent First Name' is a dependent-listing column, not wide
        r = _WIDE_DEP_REV.match(k) if not m else None
        if r and r.group(2) in {"dependent", "dep"} and not r.group(3):
            r = None
        if m or r:
            kind, n, fld = (m.group(1), m.group(2), m.group(3)) if m else (r.group(2), r.group(3), r.group(1))
            kind = "SP" if kind in {"spouse", "sp"} else "CH"
            fld = {"firstname": "first", "lastname": "last", "dateofbirth": "dob", "birthdate": "dob",
                   "sex": "gender"}.get(fld, fld)
            wide.setdefault((kind, n or "1"), {})[fld] = idx
            continue
        # specific (dep_/emp_) aliases win over generic ones
        for canon in ("dep_first", "dep_last", "dep_dob", "dep_gender", "dep_full",
                      "first_name", "last_name", "gender", "dob", "zip", "relationship", "coverage", "status",
                      "cobra", "plan_type", "plan", "carrier", "premium", "ee_cost", "er_cost",
                      "hire_date", "effective_date", "txn_type", "sub_id", "full_name",
                      "emp_first", "emp_last", "emp_full", "address", "city", "state"):
            if k in FIELD_ALIASES[canon] and canon not in cols:
                cols[canon] = idx
                break
    return Header(best_i, cols, wide, labels)


def body(df: pd.DataFrame, hdr: Header) -> pd.DataFrame:
    return df.iloc[hdr.row_idx + 1:].reset_index(drop=True)


# ------------------------------------------------------- document roles -----
ROLE_FILENAME = [
    ("deliverable", r"(- marsh census|- sbu census|marsh_census_|sbu_census_|rapt_census_)"),
    ("payroll", r"(payroll\s*register|payroll\s*report|payroll\s*journal|wage\s*report|payroll\s*summary)"),
    ("invoice", r"(invoice|inv[_\-\s]|billing|bill[_\-\s]|statement|premium\s*bill)"),
    ("renewal", r"(renewal|renew|rate\s*(sheet|pass|card)|proposal)"),
    ("ghq", r"(ghq|group\s*health\s*questionnaire|questionnaire)"),
    ("rfp", r"(rfp|request\s*for\s*proposal|mail|email|submission|inbox)"),
    ("census", r"(census|roster|enroll|eligib|employee|dependent|demographic|census)"),
]
ROLE_CONTENT = {
    "payroll": [r"payroll register", r"earnings", r"gross pay", r"associate id", r"paid-in department"],
    "invoice": [r"invoice", r"amount due", r"billing period", r"total (premium|due)", r"statement date"],
    "renewal": [r"renewal", r"renewing", r"new rates?", r"rate (sheet|schedule)", r"proposed rates?"],
    "ghq": [r"group health questionnaire", r"ghq", r"prior carrier", r"sic code"],
    "rfp": [r"request for proposal", r"\brfp\b", r"new submission", r"submission", r"key details", r"peo consultant"],
    "census": [r"date of birth|\bdob\b", r"gender|\bsex\b", r"relationship", r"coverage tier|\btier\b"],
}


def classify(doc: RawDoc) -> str:
    """Return one of: invoice, renewal, ghq, rfp, census, unknown."""
    name = doc.path.stem.lower()
    for role, pat in ROLE_FILENAME:
        if re.search(pat, name):
            # a census file named "...enrollment..." could still be an invoice - trust the filename
            return role
    text = (doc.text or "").lower()
    scores = {r: sum(1 for p in pats if re.search(p, text)) for r, pats in ROLE_CONTENT.items()}
    # header-based evidence for census
    for t in doc.tables:
        h = detect_header(t.df)
        if h and {"dob"} & set(h.columns) and ({"first_name", "full_name"} & set(h.columns)):
            scores["census"] += 3
            if "premium" in h.columns and "plan" in h.columns and re.search(r"invoice|bill", text):
                scores["invoice"] += 3
    role, sc = max(scores.items(), key=lambda kv: kv[1])
    if sc >= 2:
        return role

    from census_config import USE_LLM_FALLBACK
    if USE_LLM_FALLBACK and (doc.text or "").strip():
        try:
            from llm_client import classify_document
            llm_role = classify_document(doc.text, doc.name)
            if llm_role and llm_role != "unknown":
                return llm_role
        except Exception:
            pass

    return "unknown"

