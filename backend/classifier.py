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
                     "typeemployeespousechildren", "relationshipemployeespouseorchild", "relationshiptoinsured"],
    "coverage": ["coverage", "coveragetier", "tier", "coveragelevel", "enrollmenttier", "plantier",
                 "coveragetype", "election", "coveragecode", "enrollmentlevel", "benefittier", "ratetier",
                 "householdtier", "medicaltier", "coveragestatus", "coveragelevelvalue", "coveragelevelamount",
                 "medicalcoverage", "healthcoveragelevel", "healthcoverage", "healthcoveragetier",
                 "medicalelection", "medicalelectioneees", "medicalelectioneeesec",
                 "medical1coveragelevel", "medicalcoveragetier", "medical1coveragetier", "medical1tier", "medical1election"],
    "status": ["status", "employeestatus", "memberstatus", "billingstatus", "policystatus",
               "enrollmentstatus", "enrolled", "coveragestatus", "medicalstatus", "medicalenrollment",
               "employeesstatus", "employeesstatusftfulltimeptparttime", "employeestatusftfulltimeptparttime"],
    "cobra": ["cobra", "cobraparticipant", "cobrastatus", "cobraindicator", "cobrayn", "oncobra", "oncobra1yes0no"],
    "plan": ["plan", "planname", "medicalplan", "plancode", "planelected", "medicalplanname", "product",
             "productname", "planenrolled", "currentplan", "planoption", "medicalplancode", "plandescription",
             "currentmedicalplan", "enrolledplan", "medicalplanoption", "healthplan",
             "medical1planname", "medical1plan", "medical1plancode"],
    "plan_type": ["plantype", "benefittype", "lineofcoverage", "producttype", "benefitplantype"],
    "plan_category": ["plancategory", "benefitcategory", "productcategory", "benefitplancategory"],
    "carrier": ["carrier", "provider", "carriercode", "carriername", "insurancecarrier", "vendor",
                "medical1provider", "medical1carrier"],
    "ee_cost": ["employeecost", "eecost", "employeededuction", "eededuction",
                "medical1employeecost", "medical1eecost"],
    "er_cost": ["employercost", "ercost", "employercostamount", "companycost", "employercontribution",
                "medical1employercost", "medical1ercost"],
    "premium": ["premium", "monthlypremium", "totalpremium", "amount", "charge", "billedamount",
                "currentpremium", "rate", "monthlytotalpremium", "totalcharge", "billedpremium",
                "currentcharge", "totalamount", "currentamountdue", "monthlycost", "premiumamount",
                "currentrate", "billedrate", "totalbilled", "totalcost", "cost", "totalamountbilled", "billedcost"],
    "sub_id": ["subscriberid", "memberid", "employeeid", "eeid", "empid", "id", "ssn", "ssnlast4",
               "certificate", "certno", "certificatenumber", "memberno", "subscriberssn", "employeessn",
               "eessn", "subscribernumber", "employeenumber", "empno", "idnumber", "ssn4", "last4ssn",
               "householdid", "hhid", "familyid", "taxid", "employeetaxid", "positionid"],
    "hire_date": ["hiredate", "dateofhire", "doh", "startdate", "originalhiredate"],
    "eligibility_class": ["benefiteligibilityclass", "eligibilityclass", "benefiteligibility",
                          "eligibilitygroup", "benefiteligroup", "medicaleligibilitygroup",
                          "medical1eligibilitygroup", "medicaleligibilityclass"],
    "worker_category": ["workercategory", "employeecategory", "employmentcategory", "employeetype",
                        "employeestatuscategory", "workerstatus", "workertype", "ftpt", "fulltimeparttime"],
    "position_status": ["positionstatus", "employmentstatus", "positionstate"],
    "waive_reason": ["medicalwaivereason", "waivereason", "waiverreason", "medicalwaiver",
                     "waivedreason", "enrollmentwaivereason"],
    "job_title": ["jobtitle", "positiontitle", "jobdescription", "positiondescription", "jobname", "title",
                  "employeesjobtitle", "employeejobtitle"],
    "salary": ["annualsalary", "salary", "annualwages", "wages", "grosssalary", "annualcomp",
               "compensation", "annualcompensation", "basepay", "baseannualsalary",
               "employeesannualpayrate", "employeeannualpayrate", "annualpayrate", "payrate", "annualrate"],
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


def _score_row(vals: list[str]) -> int:
    flat = {a for al in FIELD_ALIASES.values() for a in al}
    count = 0
    for v in vals:
        k = norm_key(v)
        knp = norm_key(re.sub(r"\(.*?\)", "", str(v)))
        knb = norm_key(re.sub(r"[\(\[\{].*", "", str(v)))
        kn_lead = norm_key(re.sub(r"^[\)\]\}]+", "", str(v)))
        k_no_poss = norm_key(re.sub(r"['\u2019]s\b", "", str(v)))
        knp_poss = norm_key(re.sub(r"['\u2019]s\b", "", re.sub(r"\(.*?\)", "", str(v))))
        if any(x and x in flat for x in (k, knp, knb, kn_lead, k_no_poss, knp_poss)):
            count += 1
    return count


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
    cand_cols: dict[str, list[tuple[int, str]]] = {}
    wide_cands: dict[tuple[str, str], dict[str, list[tuple[int, str]]]] = {}
    for idx, lab in enumerate(labels):
        k = norm_key(lab)
        k_no_paren = norm_key(re.sub(r"\(.*?\)", "", str(lab)))
        knb = norm_key(re.sub(r"[\(\[\{].*", "", str(lab)))
        kn_lead = norm_key(re.sub(r"^[\)\]\}]+", "", str(lab)))
        k_no_poss = norm_key(re.sub(r"['\u2019]s\b", "", str(lab)))
        k_no_paren_poss = norm_key(re.sub(r"['\u2019]s\b", "", re.sub(r"\(.*?\)", "", str(lab))))
        if not k and not k_no_paren and not knb and not kn_lead:
            continue
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
            if any(x and x in FIELD_ALIASES[canon] for x in (k, k_no_paren, knb, kn_lead, k_no_poss, k_no_paren_poss)):
                cand_cols.setdefault(canon, []).append((idx, str(lab)))
                if canon not in cols:
                    cols[canon] = idx
                elif canon in ("gender", "dep_gender"):
                    prev_lab = str(labels[cols[canon]])
                    if "(" in prev_lab and "(" not in str(lab):
                        cols[canon] = idx
                break
    return Header(best_i, cols, wide, labels, cand_cols, wide_cands)


def body(df: pd.DataFrame, hdr: Header) -> pd.DataFrame:
    return df.iloc[hdr.row_idx + 1:].reset_index(drop=True)


# ------------------------------------------------------- document roles -----
ROLE_FILENAME = [
    ("deliverable", r"(- marsh census|- sbu census|marsh_census_|sbu_census_|rapt_census_)"),
    ("payroll", r"(payroll|wage|earnings)"),
    ("invoice", r"(invoice|inv[_\-\s]|billing|bill[_\-\s]|statement|premium\s*bill)"),
    ("renewal", r"(renewal|renew|rate\s*(sheet|pass|card)|proposal)"),
    ("ghq", r"(ghq|group\s*health\s*questionnaire|questionnaire)"),
    ("rfp", r"(rfp|request\s*for\s*proposal|mail|email|submission|inbox|application)"),
    ("census", r"(census|roster|enroll|eligib|employee|dependent|demographic)"),
    ("plan_design", r"(plan[_\-\s]*design|summary[_\-\s]*of[_\-\s]*benefits?|sbc\b|benefit[_\-\s]*summary|\bplan\b.*(buy\s*up|base)|(buy\s*up|base)\s*plan)"),
]
ROLE_CONTENT = {
    "payroll": [r"payroll register", r"gross wages?", r"gross pay", r"service fee", r"\bfuta\b", r"\bsui\b",
                r"social security", r"medicare", r"wc code", r"workers'? comp", r"associate id", r"paid-in department",
                r"earnings", r"\bcheck ?#", r"file ?#"],
    "invoice": [r"amount due", r"billing period", r"total (premium|due)", r"statement date",
                r"medical insurance", r"premium"],
    "renewal": [r"renewal", r"renewing", r"new rates?", r"rate (sheet|schedule)", r"proposed rates?"],
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


def classify(doc: RawDoc) -> str:
    """Return one of: invoice, renewal, ghq, rfp, census, payroll, unknown."""
    from census_config import LLM_FIRST, USE_LLM_FALLBACK

    # 1. LLM-First semantic classification (Copilot Studio approach)
    if LLM_FIRST and USE_LLM_FALLBACK and (doc.text or "").strip():
        try:
            from llm_client import classify_document
            llm_role = classify_document(doc.text, doc.name)
            if llm_role and llm_role != "unknown":
                # Structural guard: a tabular employee roster (names + DOB header) that is not a
                # payroll register must stay a census, even when it carries salary / WC-class /
                # payroll-name columns that can mislead semantic classification.
                if llm_role in {"payroll", "invoice", "rfp", "plan_design"} and _looks_like_roster(doc):
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
                return llm_role
        except Exception:
            pass

    return "unknown"

