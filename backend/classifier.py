"""Header detection / field mapping and document-role classification."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from readers import RawDoc, RawTable
from utils import clean, norm_key

log = logging.getLogger("census.classifier")


# canonical field -> normalised header aliases
FIELD_ALIASES: dict[str, list[str]] = {
    "first_name": ["firstname", "first", "fname", "givenname", "employeefirstname", "eefirstname",
                   "subscriberfirstname", "memberfirstname", "firstnamemi", "legalfirstname",
                   "preferredorchosenfirst", "primaryfirstname", "eefirst", "employeefirst",
                   "payrollnamefirstname", "payrollfirstname", "employeelegalfirstname", "payrollnamefirst",
                   "employeedependentfirstname", "employeeanddependentfirstname", "eeanddepfirstname",
                   "employeelegalnamefirst", "employeeslegalnamefirst", "employeelegalnamefirstname",
                   "employeeslegalnamefirstname", "legalnamefirst", "legalnamefirstname"],
    "preferred_first": ["preferredfirstname", "preferredfirst", "preferredname", "chosenfirst",
                        "chosenfirstname", "nickname", "aliasfirstname", "preferredfname",
                        "preferredorchosenfirst", "preferredchosenfirstname", "preferredchosenfirst",
                        "preferrednamefirstname", "preferredfirstnamemi"],
    "middle_name": ["middlename", "middle", "mname", "mi", "middleinitial", "employeemiddlename"],
    "last_name": ["lastname", "last", "lname", "surname", "familyname", "employeelastname",
                  "eelastname", "subscriberlastname", "memberlastname", "legallastname",
                  "preferredorchosenlast", "primarylastname", "eelast", "employeelast",
                  "payrollnamelastname", "payrolllastname", "employeelegallastname", "payrollnamelast",
                  "employeedependentlastname", "employeeanddependentlastname", "eeanddeplastname",
                  "employeelegalnamelast", "employeeslegalnamelast", "employeelegalnamelastname",
                  "employeeslegalnamelastname", "legalnamelast", "legalnamelastname"],
    "full_name": ["name", "fullname", "employeename", "employeefullname", "membername", "subscribername", "subscriberfullname", "employee",
                  "eename", "eefullname", "empfullname", "primaryfullname", "insuredname", "member", "subscriber", "participant", "enrolleename",
                  "lastfirst", "lastnamefirstname", "namelastfirst"],
    "gender": ["gender", "sex", "mf", "gendermf", "gendercode", "genderdescription", "gendermorf",
               "genderselfid", "employeegender", "employeesex", "eegender", "eesex",
               "subscribergender", "subscribersex", "membergender", "membersex",
               "legalgender", "legalsex", "birthsex", "assignedsex", "biologicalsex"],
    "dob": ["dob", "dateofbirth", "birthdate", "birthday", "bdate", "birthdt", "memberdob", "dateofbirthmmddyyyy",
            "employeedateofbirth", "employeedob"],
    "zip": ["zip", "zipcode", "homezipcode", "postalcode", "homezip", "zip5", "residentzip",
            "primaryaddresszipcode", "primaryaddresszip", "residentzipcode", "residentpostalcode",
            "homepostalcode", "homezipcode", "residentpostal", "legalpreferredaddresszippostalcode",
            "zippostalcode", "zippostal", "legalpreferredaddresszip", "workzipcode", "workzip"],
    "relationship": ["relationship", "relationshiptoemployee", "rel", "relation", "relationshiptosubscriber",
                     "membertype", "relcode", "relationshipcode", "depcode", "relationshiptoee",
                     "dependentrelationship", "deprelationship", "reltoemployee", "relationshiptosubscriber", "deprel",
                     "dependentbeneficiaryrelationship", "dependentbeneficiaryrel", "depbeneficiaryrel",
                     "statusrelationship", "statusrelation", "typeemployeespousechild", "typeeespch",
                     "type", "eetype", "memberrole", "role", "dependenttype", "relationshiptype",
                     "typeemployeespousechildren", "relationshipemployeespouseorchild", "relationshiptoinsured",
                     "employeedependent", "employeeordependent", "employeedep", "employeeanddependent", "eeordep", "eeanddep",
                     "relationshipdesc", "relationshipdescription", "dependentrelationshipdesc", "dependentrelationshipdescription"],
    "coverage": ["coverage", "coveragetier", "tier", "coveragelevel", "enrollmenttier", "plantier",
                 "election", "coveragecode", "enrollmentlevel", "benefittier", "ratetier",
                 "householdtier", "medicaltier", "coveragestatus", "coveragelevelvalue", "coveragelevelamount",
                 "medicalcoverage", "healthcoveragelevel", "healthcoverage", "healthcoveragetier",
                 "medicalelection", "medicalelectioneees", "medicalelectioneeesec",
                 "coveragedetails", "coverageelection", "electedcoverage", "coveragedescription",
                 "tierdescription", "medicalcoveragedetails", "benefitcoverage", "benefitcoveragetier", "coveragename",
                 "medical1coveragelevel", "medicalcoveragetier", "medical1coveragetier", "medical1tier", "medical1election",
                 "medicalcoveragelevel", "currentanticipatedhealthcoveragelevel", "anticipatedhealthcoveragelevel",
                 "currenthealthcoveragelevel", "currentanticipatedcoveragelevel"],
    "status": ["status", "employeestatus", "memberstatus", "billingstatus", "policystatus",
               "enrollmentstatus", "enrolled", "coveragestatus", "medicalstatus", "medicalenrollment",
               "employeesstatus", "employeesstatusftfulltimeptparttime", "employeestatusftfulltimeptparttime"],
    "cobra": ["cobra", "cobraparticipant", "cobrastatus", "cobraindicator", "cobrayn", "oncobra", "oncobra1yes0no"],
    "plan": ["plan", "planname", "medicalplan", "plancode", "planelected", "medicalplanname", "product",
             "productname", "planenrolled", "currentplan", "planoption", "medicalplancode", "plandescription",
             "currentmedicalplan", "enrolledplan", "medicalplanoption", "healthplan", "healthcare", "healthcareplan",
             "planadminname", "adminplanname", "planadministrator", "medicalplandescription", "electedplan",
             "planenrolledname", "enrolledplanname", "benefitplan", "planadmin", "plannameadmin",
             "medical1planname", "medical1plan", "medical1plancode",
             "medicalplanselected", "planselected", "selectedplan", "selectedmedicalplan",
             "coverageoption", "coveragegroupoption", "coveragegroup"],
    "plan_type": ["plantype", "benefittype", "benefit", "benefitline", "benefitcategory",
                  "lineofcoverage", "coverageline", "producttype", "productline",
                  "benefitplantype", "benefitdescription", "lob", "lineofbusiness",
                  "electedbenefit", "coveragecategory", "coveragetype"],
    "plan_category": ["plancategory", "benefitcategory", "productcategory", "benefitplancategory"],
    "carrier": ["carrier", "provider", "carriercode", "carriername", "insurancecarrier", "vendor",
                "medical1provider", "medical1carrier"],
    "ee_cost": ["employeecost", "eecost", "employeededuction", "eededuction",
                "employeecontribution", "eecontribution", "employeedeductions", "eedeductions", "eecontrib",
                "employeerate", "eerate", "employeecontributionamount",
                "medical1employeecost", "medical1eecost", "ghpeemonthlycontribution",
                "eemonthlycontribution", "employeemonthlycontribution", "monthlyemployeecontribution", "monthlyeecontribution"],
    "er_cost": ["employercost", "ercost", "employercostamount", "companycost", "employercontribution",
                "ercontribution", "companycontribution", "ercontrib", "employercontributionamount",
                "medical1employercost", "medical1ercost"],
    "premium": ["premium", "monthlypremium", "totalpremium", "amount", "charge", "billedamount",
                "currentpremium", "rate", "monthlytotalpremium", "totalcharge", "billedpremium",
                "currentcharge", "totalamount", "currentamountdue", "monthlycost", "premiumamount",
                "totalrate", "monthlyrate", "totalmonthlyrate", "premiumrate", "totalpremiumrate", "fullrate",
                "ratetotal", "rateamount", "totalmonthlypremium",
                "currentrate", "billedrate", "totalbilled", "totalcost", "cost", "totalamountbilled", "billedcost",
                "planrate", "medicalrate"],
    "sub_id": ["subscriberid", "memberid", "employeeid", "eeid", "empid", "id", "ssn", "ssnlast4",
               "certificate", "certno", "certificatenumber", "memberno", "subscriberssn", "employeessn",
               "eessn", "subscribernumber", "employeenumber", "empno", "idnumber", "ssn4", "last4ssn",
               "householdid", "hhid", "familyid", "taxid", "employeetaxid", "positionid"],
    "hire_date": ["hiredate", "dateofhire", "doh", "startdate", "originalhiredate",
                  "insperityhiredate", "clienthiredate", "companyhiredate", "employmenthiredate", "peohiredate"],
    "eligibility_class": ["benefiteligibilityclass", "eligibilityclass", "benefiteligibility",
                          "eligibilitygroup", "benefiteligroup", "medicaleligibilitygroup",
                          "medical1eligibilitygroup", "medicaleligibilityclass"],
    "worker_category": ["workercategory", "employeecategory", "employmentcategory", "employeetype",
                        "employeestatuscategory", "workerstatus", "workertype", "ftpt", "fulltimeparttime",
                        "ftorpt", "ftorptstatus", "employeeclassification", "classification"],
    "position_status": ["positionstatus", "employmentstatus", "positionstate"],
    "waive_reason": ["medicalwaivereason", "waivereason", "waiverreason", "medicalwaiver",
                     "waivedreason", "enrollmentwaivereason", "waiver", "waivereasontext", "reasonforwaiving",
                     "waivedcoveragefor", "declinedreason", "declinereason", "reasonfordeclining"],
    "job_title": ["jobtitle", "positiontitle", "jobdescription", "positiondescription", "jobname", "title",
                  "employeesjobtitle", "employeejobtitle"],
    "salary": ["annualsalary", "salary", "annualwages", "wages", "grosssalary", "annualcomp",
               "compensation", "annualcompensation", "basepay", "baseannualsalary",
               "employeesannualpayrate", "employeeannualpayrate", "annualpayrate", "payrate", "annualrate",
               "annualpay"],
    "effective_date": ["effective_date", "effectivedate", "effdate", "enrollmenteffectivedate", "enrollmenteffdate",
                       "coverageeffectivedate", "coverageeffdate", "benefiteffectivedate", "benefiteffdate",
                       "enrollmentstartdate"],
    "txn_type": ["transactiontype", "chargetype", "adjustmenttype", "billingtype", "linetype", "activity"],
    # explicit dependent columns (dependent listing files)
    "dep_first": ["dependentfirstname", "depfirstname", "dependentfirst", "depfirst", "dependentfname", "depfname",
                  "dependentlegalnamefirst", "dependentslegalnamefirst", "dependentlegalnamefirstname",
                  "dependentslegalnamefirstname", "dependentlegalfirstname", "dependentslegalfirstname"],
    "dep_last": ["dependentlastname", "deplastname", "dependentlast", "deplast", "dependentlname", "deplname",
                 "dependentlegalnamelast", "dependentslegalnamelast", "dependentlegalnamelastname",
                 "dependentslegalnamelastname", "dependentlegallastname", "dependentslegallastname"],
    "dep_full": ["dependentname", "depname", "dependent", "dependentfullname", "dependentbeneficiaryname", "dependentbeneficiary", "depbeneficiaryname"],
    "dep_dob": ["dependentdob", "depdob", "dependentdateofbirth", "depdateofbirth", "dependentbirthdate", "dependentbeneficiarydateofbirth", "dependentbeneficiarydob", "depbeneficiarydob"],
    "dep_gender": ["dependentgender", "depgender", "dependentsex", "depsex", "dependentbeneficiarygender",
                   "depbeneficiarygender", "dependentbeneficiarysex", "depbeneficiarysex",
                   "dependentgenderselfid", "depgenderselfid"],
    # employee reference columns inside dependent files
    "emp_first": ["employeefirstname", "subscriberfirstname", "eefirst", "empfirstname", "primaryfirstname"],
    "emp_last": ["employeelastname", "subscriberlastname", "eelast", "emplastname", "primarylastname"],
    "emp_full": ["employeename", "employeefullname", "subscribername", "subscriberfullname", "eename", "eefullname", "empfullname", "primaryname", "primaryfullname", "policyholder", "primarymember",
                 "subscriber", "employee"],
    "address": ["address", "homeaddress1", "homeaddress", "streetaddress", "address1", "street", "primaryaddressline1", "primaryaddress"],
    "city": ["city", "town", "primaryaddresscity", "residentcity"],
    "state": ["state", "st", "primaryaddressstateterritory", "primaryaddressstate", "livedinstate", "residentstate", "workstate", "workst"],
}

# Premium vs amount wording that must NOT be treated as a total premium
_RETRO_WORDS = re.compile(r"(retro|adjust|credit|refund|prior\s*period|reinstat)", re.I)

# wide-format dependents: spouse_first / child1dob / dependent1relationship ...
_WIDE_DEP = re.compile(r"^(spouse|sp|child|ch|dependent|dep)(\d*)(firstname|first|lastname|last|name|dob|"
                       r"dateofbirth|birthdate|gender|sex|relationship|rel)$")
_WIDE_DEP_REV = re.compile(r"^(firstname|first|lastname|last|name|dob|dateofbirth|birthdate|gender|sex|relationship|rel)"
                           r"(spouse|sp|child|ch|dependent|dep)(\d*)$")


@dataclass
class Header:
    row_idx: int
    columns: dict[str, int]                        # canonical field -> column index
    wide_deps: dict[tuple[str, str], dict[str, int]]   # (kind, n) -> {field: col}
    labels: list[str]
    candidate_columns: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    wide_candidate_columns: dict[tuple[str, str], dict[str, list[tuple[int, str]]]] = field(default_factory=dict)


def _header_variants(val: str) -> tuple[str, ...]:
    """Return normalized key variants for a header label, stripping parentheticals,
    possessives, and multi-line template instructions (e.g. 'Coverage *\\nEnter EE...')."""
    s = str(val or "")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    bases = [s]
    if lines and lines[0] != s.strip():
        bases.append(lines[0])
    variants: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for cand in (
            norm_key(base),
            norm_key(re.sub(r"\(.*?\)", "", base)),
            norm_key(re.sub(r"[\(\[\{].*", "", base)),
            norm_key(re.sub(r"^[\)\]\}]+", "", base)),
            norm_key(re.sub(r"['\u2019]s\b", "", base)),
            norm_key(re.sub(r"['\u2019]s\b", "", re.sub(r"\(.*?\)", "", base))),
        ):
            if cand and cand not in seen:
                seen.add(cand)
                variants.append(cand)
    return tuple(variants)


def _score_row(vals: list[str]) -> int:
    flat = {a for al in FIELD_ALIASES.values() for a in al}
    count = 0
    for v in vals:
        if any(x in flat for x in _header_variants(v)):
            count += 1
    return count


def detect_header(df: pd.DataFrame, max_scan: int = 40) -> Optional[Header]:
    best_i, best_s = None, 0
    for i in range(min(max_scan, len(df))):
        s = _score_row(["" if pd.isna(v) else str(v) for v in df.iloc[i].tolist()])
        if s > best_s:
            best_i, best_s = i, s
    if best_i is None or best_s < 2:
        return None
    raw_labels = ["" if pd.isna(v) else str(v) for v in df.iloc[best_i].tolist()]
    labels = [clean(v) for v in df.iloc[best_i].tolist()]
    cols: dict[str, int] = {}
    wide: dict[tuple[str, str], dict[str, int]] = {}
    cand_cols: dict[str, list[tuple[int, str]]] = {}
    wide_cands: dict[tuple[str, str], dict[str, list[tuple[int, str]]]] = {}
    for idx, (lab, raw_lab) in enumerate(zip(labels, raw_labels)):
        variants = _header_variants(raw_lab or lab)
        if not variants:
            continue
        k = variants[0]
        k_no_paren = variants[1] if len(variants) > 1 else ""
        m = _WIDE_DEP.match(k) or (_WIDE_DEP.match(k_no_paren) if k_no_paren else None)
        if m and m.group(1) in {"dependent", "dep"} and not m.group(2):
            m = None                      # 'Dependent First Name' is a dependent-listing column, not wide
        r = (_WIDE_DEP_REV.match(k) or (_WIDE_DEP_REV.match(k_no_paren) if k_no_paren else None)) if not m else None
        if r and r.group(2) in {"dependent", "dep"} and not r.group(3):
            r = None
        if m or r:
            kind, n, fld = (m.group(1), m.group(2), m.group(3)) if m else (r.group(2), r.group(3), r.group(1))
            kind = "SP" if kind in {"spouse", "sp"} else "CH"
            fld = {"firstname": "first", "lastname": "last", "dateofbirth": "dob", "birthdate": "dob",
                   "sex": "gender", "relationship": "rel"}.get(fld, fld)
            slot_key = (kind, n or "1")
            slot_dict = wide.setdefault(slot_key, {})
            if fld not in slot_dict or (fld == "gender" and "(" in str(labels[slot_dict[fld]]) and "(" not in str(lab)):
                slot_dict[fld] = idx
            wide_cands.setdefault(slot_key, {}).setdefault(fld, []).append((idx, str(lab)))
            continue
        # specific (dep_/emp_) aliases win over generic ones
        for canon in ("dep_first", "dep_last", "dep_dob", "dep_gender", "dep_full",
                      "first_name", "last_name", "preferred_first", "middle_name",
                      "gender", "dob", "zip", "relationship", "coverage", "status",
                      "cobra", "plan_type", "plan", "carrier", "premium", "ee_cost", "er_cost",
                      "hire_date", "effective_date", "txn_type", "sub_id", "full_name",
                      "eligibility_class", "worker_category", "position_status",
                      "waive_reason", "job_title", "salary",
                      "emp_first", "emp_last", "emp_full", "address", "city", "state"):
            if any(x in FIELD_ALIASES[canon] for x in variants):
                cand_cols.setdefault(canon, []).append((idx, str(lab)))
                if canon not in cols:
                    cols[canon] = idx
                elif canon in ("gender", "dep_gender"):
                    prev_lab = str(labels[cols[canon]])
                    if "(" in prev_lab and "(" not in str(lab):
                        cols[canon] = idx
                break

    # Handle repeated unnumbered dependent column blocks (e.g., Benefits Enrollment report with 5 wide dependent slots)
    dep_anchors = [idx for idx, _ in (cand_cols.get("dep_last") or cand_cols.get("dep_first") or [])]
    if len(dep_anchors) > 1 and ("full_name" in cols or "first_name" in cols):
        dep_anchors = sorted(dep_anchors)
        for slot_i, start_col in enumerate(dep_anchors):
            end_col = dep_anchors[slot_i + 1] if slot_i + 1 < len(dep_anchors) else len(labels)
            slot_key = ("CH", str(slot_i + 1))
            slot_dict = wide.setdefault(slot_key, {})
            for canon_dep, target_fld in (
                ("dep_last", "last"), ("dep_first", "first"), ("dep_full", "name"),
                ("dep_dob", "dob"), ("dep_gender", "gender"), ("relationship", "rel"),
            ):
                for col_idx, col_lab in cand_cols.get(canon_dep, []):
                    if start_col <= col_idx < end_col:
                        slot_dict[target_fld] = col_idx
                        wide_cands.setdefault(slot_key, {}).setdefault(target_fld, []).append((col_idx, col_lab))
                        break
        for k_rm in ("dep_last", "dep_first", "dep_full", "dep_dob", "dep_gender"):
            cols.pop(k_rm, None)
        if "relationship" in cols and cols["relationship"] >= dep_anchors[0]:
            # Check if there was an earlier relationship column before dep_anchors[0]
            earlier_rel = [idx for idx, _ in cand_cols.get("relationship", []) if idx < dep_anchors[0]]
            if earlier_rel:
                cols["relationship"] = earlier_rel[0]
            else:
                cols.pop("relationship", None)

    # Handle swapped/misaligned 'Employee Name' (holding Last Name) + 'Last' (holding First Name)
    if "full_name" in cols and "last_name" in cols and "first_name" not in cols:
        c_full, c_last = cols["full_name"], cols["last_name"]
        if c_last == c_full + 1 and best_i + 1 < len(df):
            sample_rows = df.iloc[best_i + 1: min(best_i + 25, len(df))]
            v0_list = [clean(v) for v in sample_rows.iloc[:, c_full].tolist() if clean(v)]
            v1_list = [clean(v) for v in sample_rows.iloc[:, c_last].tolist() if clean(v)]
            if v0_list and v1_list and not any("," in v for v in v0_list[:10]):
                cols["last_name"] = c_full
                cols["first_name"] = c_last
                cols.pop("full_name", None)

    # Fallback: if 'Coverage Type' mapped to plan_type and 'coverage' is unmapped, check if values are tier codes
    if "coverage" not in cols and "plan_type" in cols and best_i + 1 < len(df):
        from utils import std_coverage
        c_pt = cols["plan_type"]
        sample_vals = [clean(v) for v in df.iloc[best_i + 1: min(best_i + 30, len(df)), c_pt].tolist() if clean(v)]
        tier_hits = sum(1 for v in sample_vals if std_coverage(v) in ("EE", "ES", "EC", "FAM", "WO", "WP", "NE", "RC"))
        if sample_vals and tier_hits >= max(1, len(sample_vals) // 2):
            cols["coverage"] = c_pt
            cols.pop("plan_type", None)

    return Header(best_i, cols, wide, labels, cand_cols, wide_cands)


def body(df: pd.DataFrame, hdr: Header) -> pd.DataFrame:
    return df.iloc[hdr.row_idx + 1:].reset_index(drop=True)


# ------------------------------------------------------- document roles -----
ROLE_FILENAME = [
    ("deliverable", r"(- marsh census|- sbu census|marsh_census_|sbu_census_|rapt_census_)"),
    ("claims", r"(claims?|large[_\-\s]*claim)"),
    ("payroll", r"(payroll|wage|earnings|(?:^|[_\-\s])schedule[_\-\s]*a\b)"),
    ("invoice", r"(invoice|inv[_\-\s]|billing|bill[_\-\s]|statement|premium\s*bill)"),
    ("renewal", r"(renewal|renew|rate\s*(sheet|pass|card)|proposal|\bmar[_\-\s]*report|medical[_\-\s]*allocation|financial[_\-\s]*stewardship|schedule[_\-\s]*b\b)"),
    ("ghq", r"(ghq|group\s*health\s*questionnaire|questionnaire)"),
    ("rfp", r"(rfp|request\s*for\s*proposal|mail|email|submission|inbox|application)"),
    ("census", r"(census|roster|enroll|eligib|employee|dependent|demographic)"),
    ("plan_design", r"(plan[_\-\s]*design|group[_\-\s]*identification[_\-\s]*key|summary[_\-\s]*of[_\-\s]*benefits?|sbc\b|benefit[_\-\s]*summary|\bplan\b.*(buy\s*up|base)|(buy\s*up|base)\s*plan)"),
]
ROLE_CONTENT = {
    "claims": [r"aggregate claims?", r"specific claims?", r"paid claims?", r"large claims?", r"stop loss"],
    "payroll": [r"payroll register", r"gross wages?", r"gross pay", r"service fee", r"\bfuta\b", r"\bsui\b",
                r"social security", r"medicare", r"wc code", r"workers'? comp", r"associate id", r"paid-in department",
                r"earnings", r"\bcheck ?#", r"file ?#"],
    "invoice": [r"amount due", r"billing period", r"total (premium|due)", r"statement date",
                r"medical insurance", r"premium"],
    "renewal": [r"renewal", r"renewing", r"new rates?", r"rate (sheet|schedule)", r"proposed rates?",
                r"medical allocation report", r"financial stewardship report", r"new monthly.*alloc"],
    "ghq": [r"group health questionnaire", r"ghq", r"prior carrier", r"sic code"],
    "rfp": [r"request for proposal", r"\brfp\b", r"new submission", r"submission", r"key details", r"peo consultant"],
    "census": [r"date of birth|\bdob\b", r"gender|\bsex\b", r"relationship", r"coverage tier|\btier\b"],
    "plan_design": [r"summary of benefits", r"schedule of benefits", r"benefit summary", r"coverage period", r"deductible", r"out-of-pocket", r"coinsurance", r"copayment"],
}


# Wage / payroll content signals used to dynamically disqualify a document from benefit-invoice use.
# Only register-specific tokens are used. Generic terms like "earnings" or "social security" are
# intentionally excluded: they occur incidentally in census and enrollment files (plan names,
# deduction categories) and would cause false payroll classification.
_WAGE_CONTENT = re.compile(
    r"(gross\s*wages?|gross\s*pay|\bservice\s*fee\b|\bfuta\b|\bsui\b|"
    r"\bwc\s*code\b|workers'?\s*comp|paid-?in\s*department|"
    r"\bcheck\s*#|\bfile\s*#)", re.I)
# Genuine benefit-invoice signals: medical plan premium lines.
_BENEFIT_CONTENT = re.compile(
    r"(medical\s*insurance|total\s*premium|premium\s*amount|monthly\s*premium|"
    r"employee\s+details|provider\s+plan|coverage\s+type|plan\s+premium)", re.I)


def is_wage_document(doc: RawDoc) -> bool:
    """True when the document is dominated by payroll-register content and carries no genuine
    medical premium detail. Purely content-driven - no file-name or hardcoded values.

    Uses both an absolute floor and a density ratio so that large census/enrollment files that
    merely mention a few wage-related words are never misclassified as payroll."""
    text = doc.text or ""
    if not text.strip():
        return False
    wage_hits = len(_WAGE_CONTENT.findall(text))
    benefit_hits = len(_BENEFIT_CONTENT.findall(text))
    words = max(len(text.split()), 1)
    wage_density = wage_hits / words * 1000.0        # hits per 1,000 words
    # A genuine payroll register repeats register-specific tokens dozens of times per page:
    # require many hits, a high density, and a clear margin over benefit-premium signals.
    return wage_hits >= 15 and wage_density >= 15.0 and wage_hits > benefit_hits * 2


def _looks_like_roster(doc: RawDoc) -> bool:
    """True when a spreadsheet table has a per-person roster header (first/full name + DOB) with
    at least one populated data row, and the document is not a payroll register or a premium bill."""
    if is_wage_document(doc) or not doc.tables:
        return False
    for t in doc.tables:
        h = detect_header(t.df)
        if not h or "dob" not in h.columns or not ({"first_name", "full_name"} & set(h.columns)):
            continue
        if "premium" in h.columns:
            continue
        try:
            rows = body(t.df, h)
            name_col = h.columns.get("first_name", h.columns.get("full_name"))
            if rows.iloc[:, name_col].astype(str).str.strip().replace({"nan": "", "None": ""}).ne("").any():
                return True
        except Exception:
            return True
    return False


_COMPARISON_OUTPUT_HEADERS = {
    "comparisonresult", "invoiceemployeename", "match", "matchpct",
    "invoiceplan", "invoicetier", "invoicestatus", "invoicepremium", "renewalpremium",
}


def is_comparison_output_doc(doc: RawDoc) -> bool:
    """Dynamically detect completed census-comparison output workbooks (e.g. RAPT / Marsh PEO
    deliverables or reference outputs) so they are never ingested as raw census/invoice inputs."""
    for tbl in getattr(doc, "tables", []) or []:
        df = getattr(tbl, "df", None)
        if df is None or df.empty:
            continue
        for i in range(min(40, len(df))):
            row_keys = {
                v
                for cell in df.iloc[i].tolist()
                for v in _header_variants(clean(cell))
            }
            if len(row_keys & _COMPARISON_OUTPUT_HEADERS) >= 3 and (
                "comparisonresult" in row_keys or "invoiceemployeename" in row_keys
            ):
                return True
    text_low = (getattr(doc, "text", "") or "").lower()
    if "comparison result" in text_low and (
        "invoice employee name" in text_low or "findings summary" in text_low
    ) and ("match %" in text_low or "census vs. invoice summary" in text_low):
        return True
    return False


def classify(doc: RawDoc) -> str:
    """Return one of: invoice, renewal, ghq, rfp, census, payroll, deliverable, unknown."""
    from census_config import LLM_FIRST, USE_LLM_FALLBACK

    if is_comparison_output_doc(doc):
        log.info("Document '%s' has completed comparison-output structure -> classified as deliverable (excluded from source ingestion)", doc.name)
        return "deliverable"

    # 1. LLM-First semantic classification (Copilot Studio approach)
    if LLM_FIRST and USE_LLM_FALLBACK and (doc.text or "").strip():
        try:
            from llm_client import classify_document
            llm_role = classify_document(doc.text, doc.name)
            if llm_role and llm_role != "unknown":
                if llm_role == "census" and re.search(r"\b(claims?|large[_\-\s]*claim)\b", doc.name, re.I):
                    log.info("LLM classified '%s' as census; overridden to claims based on claim document signature", doc.name)
                    return "claims"
                if llm_role == "census" and (
                    re.search(r"(\bmar[_\-\s]*report|medical[_\-\s]*allocation|financial[_\-\s]*stewardship|schedule[_\-\s]*b\b)", doc.name, re.I)
                    or re.search(r"(medical allocation report|financial stewardship report)", doc.text or "", re.I)
                ):
                    log.info("LLM classified '%s' as census; overridden to renewal based on MAR/Stewardship/Schedule B signature", doc.name)
                    return "renewal"
                if re.search(r"(group[_\-\s]*identification[_\-\s]*key|option[_\-\s]*key|plan[_\-\s]*key)", doc.name, re.I):
                    log.info("Document '%s' is an option/plan legend -> overridden to plan_design", doc.name)
                    return "plan_design"
                if re.search(r"(?:^|[_\-\s])schedule[_\-\s]*a\b", doc.name, re.I):
                    log.info("Document '%s' is Schedule A fee/payroll exhibit -> overridden to payroll", doc.name)
                    return "payroll"
                # Structural guard: a tabular employee roster (names + DOB header) that is not a
                # payroll register must stay a census, even when it carries salary / WC-class /
                # payroll-name columns that can mislead semantic classification.
                if llm_role in {"payroll", "invoice", "rfp", "plan_design"} and _looks_like_roster(doc) and not re.search(r"\b(rfp|request\s*for\s*proposal|schedule[_\-\s]*a|group[_\-\s]*identification)\b", doc.name, re.I):
                    log.info("LLM-First classified '%s' as %s; overridden to census (employee roster "
                             "header detected, not a payroll register)", doc.name, llm_role)
                    return "census"
                log.info("LLM-First classified '%s' as %s", doc.name, llm_role)
                return llm_role
        except Exception as exc:
            log.warning("LLM classification failed for %s: %s (falling back to rules)", doc.name, exc)

    # 2. Rule-based / pattern fallback
    # Content-driven wage/payroll detection wins over filename: a document dominated by wage, tax,
    # service-fee, or WC lines is a payroll document and must never be treated as a benefit invoice.
    if is_wage_document(doc):
        return "payroll"

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
                if llm_role == "census" and re.search(r"\b(claims?|large[_\-\s]*claim)\b", doc.name, re.I):
                    log.info("LLM classified '%s' as census; overridden to claims based on claim document signature", doc.name)
                    return "claims"
                return llm_role
        except Exception:
            pass

    return "unknown"

