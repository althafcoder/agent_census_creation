"""Census Creation & Invoice Comparison Agent - pipeline entry point.

Usage (from the project root):
    python backend/main.py                      # reads ./input, writes ./output
    python backend/main.py -i input -o output

Programmatic:
    from main import run_pipeline
    result = run_pipeline(Path("input"), Path("output"))
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

<<<<<<< Updated upstream
import census_config as C
from census_builder import build_census
=======
import config as C
from census_builder import build_census, extract_waiting_period_rule, refine_non_enrolled_statuses
>>>>>>> Stashed changes
from classifier import classify
from comparator import run_comparison
from invoice_parser import combine_records, parse_invoice, select_invoices
from models import Findings
from prospect import build_prospect
from readers import read_all
from renewal_parser import parse_renewal, select_renewal
from router import (
    check_claims_required,
    check_renewal_required,
    determine_routing,
    generate_missing_information_email,
)
from workbook_writer import (
    save_marsh_workbook,
    save_rapt_workbook,
    save_sbu_workbook,
    save_workbook,
)

log = logging.getLogger("census")


def run_pipeline(source: Path | list[Path | str] = C.INPUT_DIR, output_dir: Path = C.OUTPUT_DIR) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    F = Findings()

    # 1. review EVERY uploaded document ----------------------------------------------------------
    log.info("[1/7] Reading and classifying documents...")
    docs = read_all(source, dest_dir=output_dir / "extracted_text")
    if not docs:
        raise ValueError(f"No supported files found in {source}")

    roles: dict[str, list] = {"census": [], "invoice": [], "renewal": [], "ghq": [], "rfp": [], "deliverable": [], "payroll": [], "unknown": []}
    for d in docs:
        role = classify(d)
        roles.setdefault(role, []).append(d)
        log.info("      * %-42s -> %-8s %s", d.name, role, "(OCR Scanned)" if d.scanned else "(Digital)")
    # unknown files that contain a census-looking table are still used (never ignore a census file)
    roles["census"] += roles.pop("unknown")

    # 2. prospect block & routing decision --------------------------------------------------------
    log.info("[2/7] Extracting Prospect Information & Determining Carrier Routing...")
    prospect, has_source, missing = build_prospect(roles["ghq"], roles["rfp"], all_docs=docs)
    
    # Apply Mandatory Routing Decision based on Prospect Primary Business State
    routing = determine_routing(prospect, docs=docs)
    F.carrier = routing.carrier
    F.state = routing.state
    F.routing_notes = list(routing.notes)
    
    log.info("      * Routing Decision: State=%s, Carrier=%s, Primary Template=%s",
             routing.state or "(Unknown)", routing.carrier or "(Undetermined)",
             routing.primary_template.name if routing.primary_template else "None")
    for n in routing.notes:
        log.info("        -> %s", n)

    if not roles["ghq"] and not roles["rfp"]:
        if prospect.company or prospect.effective_date:
            _fields = [("Proposed Effective Date", prospect.effective_date), ("SIC / NAICS Code", prospect.sic),
                       ("Company Name", prospect.company), ("Address", prospect.address),
                       ("City, State, Zip", prospect.city_state_zip), ("OneDigital Referral", prospect.referral),
                       ("REI Sales Person", prospect.sales_person)]
            filled = [n for n, v in _fields if v]
            blank = [n for n, v in _fields if not v]
            F.notes.append("No GHQ or RFP was provided. " + ", ".join(filled) +
                           " were populated from the other uploaded documents.")
            if missing:
                F.notes.append("Some prospect information fields were not available in the GHQ or RFP and were "
                               "left blank (" + "; ".join(blank) + ").")
            log.info("      * Populated prospect from census/invoice: Company=%r, EffDate=%r, Addr=%r",
                     prospect.company, prospect.effective_date, prospect.address)
        else:
            F.notes.append("No GHQ or RFP was provided. Prospect information fields were left blank.")
            log.info("      * No GHQ or RFP provided and no prospect details found -> Prospect block left blank")
    elif not has_source:
        F.notes.append("No GHQ or RFP was provided. Prospect information fields were left blank.")
        log.info("      * No GHQ or RFP provided -> Prospect block left blank")
    elif missing:
        F.notes.append("Some prospect information fields were not available in the GHQ or RFP and were left blank.")
        log.info("      * Prospect extracted (partial): Company=%r, EffDate=%r, SIC=%r, Missing=%s",
                 prospect.company, prospect.effective_date, prospect.sic, missing)
    else:
        log.info("      * Prospect extracted (complete): Company=%r, EffDate=%r, SIC=%r, Zip=%r",
                 prospect.company, prospect.effective_date, prospect.sic, prospect.zip)

    # 3. census (employees + dependents from all files) ------------------------------------------
    log.info("[3/7] Building Census: parsing employee & dependent records...")
    if not roles["census"]:
        raise ValueError("No census / enrollment file was found among the uploads.")
    census, meta = build_census(roles["census"], effective_date=prospect.effective_date)
    if not census.employees:
        raise ValueError("Census files were found but no employee rows could be parsed.")
    log.info("      * Parsed %d employee records, %d dependent records", len(census.employees), len(census.dependents))

    if not meta["saw_dependent_source"]:
        F.notes.append("Dependent file not provided (no dependent records found). Tier/dependent structure "
                       "validation was not performed.")
    if not meta["has_coverage_column"]:
        F.notes.append("No coverage/tier column was found in the census; Coverage was left blank.")

    # 4. invoices ----------------------------------------------------------------------------------
    log.info("[4/7] Parsing and selecting invoices...")
    inv_docs = [parse_invoice(d) for d in roles["invoice"]]
    selected, inv_findings, period = select_invoices(inv_docs)
    F.notes += inv_findings
    F.invoice_period = period
    with_detail = sum(1 for i in inv_docs if i.records)
    if inv_docs:
        F.invoices_received = (f"{len(inv_docs)} ({with_detail} with employee-level detail, "
                               f"{len(inv_docs) - with_detail} without employee-level detail)")
    F.selected_invoice = "; ".join(
        " ".join(x for x in [s.path, f"#{s.number}" if s.number else "",
                              f"dated {s.doc_date:%m/%d/%Y}" if s.doc_date else ""] if x)
        for s in selected)
    invoice_records = combine_records(selected)
    have_invoice = bool(invoice_records)
    if not roles["invoice"]:
        F.notes.append("No invoice was provided. Invoice comparison was not performed.")
        log.info("      * No invoice files provided")
    elif not have_invoice:
        F.notes.append("Invoice file(s) were provided but no employee rows could be read from them.")
        log.info("      * Invoices provided but no employee records read")
    else:
        log.info("      * Selected %d invoice(s), combined %d employee records (Period: %s)",
                 len(selected), len(invoice_records), period)

    # Resolve non-enrolled coverage statuses (NE/WP/WO) structurally. The reference date for the
    # waiting-period test is the invoice period start (the as-of date of the submission), falling back
    # to the proposed effective date; the waiting period itself is read from the plan-design
    # documents, never assumed.
    ref_date = (selected[0].period_start if selected else None) or prospect.effective_date
    wait_rule = extract_waiting_period_rule(docs)
    wait_days = wait_rule.get("days") if wait_rule else None
    refine_non_enrolled_statuses(census, reference_date=ref_date, waiting_days=wait_days, waiting_rule=wait_rule)

    # 5. renewal (PEO & 90-day rule check) ---------------------------------------------------------
    log.info("[5/7] Parsing and matching renewals...")
    ren_docs = [parse_renewal(d) for d in roles["renewal"]]
    ps = min((s.period_start for s in selected if s.period_start), default=None)
    pe = max((s.period_end for s in selected if s.period_end), default=None)
    renewal, ren_findings = select_renewal(ren_docs, pe, ps)
    F.notes += ren_findings
    F.renewal_effective = renewal.effective if renewal else None

    # Check renewal requirement under the PEO & 90-day rule
    from datetime import timedelta
    plan_year_renewal = min((d.termination + timedelta(days=1) for d in ren_docs if d.termination), default=None)
    ren_required, ren_reason = check_renewal_required(
        prospect, renewal_doc=renewal, docs=docs, proposed_effective=prospect.effective_date,
        known_renewal_date=plan_year_renewal,
    )
    if ren_required:
        F.renewal_notes.append(ren_reason)
        if not renewal:
            F.notes.append(f"Renewal is required under PEO & 90-day rule ({ren_reason}) but was not provided.")
            log.warning("      * %s", ren_reason)
        else:
            log.info("      * Matched renewal: effective=%s, %d rate rows (%s)", renewal.effective, len(renewal.rates), ren_reason)
    else:
        log.info("      * Renewal check: %s", ren_reason)
        if renewal:
            log.info("      * Renewal document provided voluntarily: effective=%s, %d rate rows", renewal.effective, len(renewal.rates))

    # 6. compare + populate premiums -------------------------------------------------------------
    log.info("[6/7] Running comparison and audit reconciliation...")
    results = run_comparison(census, invoice_records, renewal, meta, F, have_invoice)

    # Cross-document consistency check between census and payroll register(s).
    # Per the Payroll and Wage Document Exclusion rule, a payroll/wage document must never drive
    # enrollment deficiencies, so only non-wage payroll-role documents are used here.
    from classifier import is_wage_document as _is_wage_doc
    payroll_enrollment_docs = [d for d in roles.get("payroll", []) if not _is_wage_doc(d)]
    if payroll_enrollment_docs:
        payroll_names = set()
        for p_doc in payroll_enrollment_docs:
            for t in p_doc.tables:
                df = t.df
                for val in df.values.flatten():
                    s = str(val).strip()
                    if not s or s.lower() == "nan":
                        continue
                    for line in s.split("\n"):
                        line = line.strip()
                        m = re.match(r"^([A-Za-z\s\.\'\-]+),\s+([A-Za-z\s\.\'\-]+)$", line)
                        if m:
                            last, first = m.group(1).strip(), m.group(2).strip()
                            if not any(w in last.lower() for w in ["department", "company", "total", "payroll", "earnings"]):
                                last = " ".join(last.split())
                                first = first.split()[0]
                                payroll_names.add((first.lower(), last.lower()))

        if payroll_names:
            payroll_lasts = {l for _f, l in payroll_names}
            for emp in census.employees:
                if emp.coverage in C.ACTIVE_TIERS and not emp.invoice_only:
                    fn = emp.first.lower().split()[0] if emp.first else ""
                    ln = emp.last.lower().strip() if emp.last else ""
                    if not ln:
                        continue
                    # Match tolerantly: payroll extraction can drop leading characters (e.g. 'ertolotti'
                    # for 'Bertolotti'), so accept a last-name containment match in either direction.
                    match = (fn, ln) in payroll_names
                    if not match and len(ln) >= 5:
                        match = any(len(pl) >= 5 and (ln in pl or pl in ln) for pl in payroll_lasts)
                    if not match:
                        res = results.get(id(emp))
                        plan_disp = (res.plan if res and res.plan else emp.plan) or "Medical"
                        who = f"{emp.first} {emp.last}".strip()
                        detail = f"{who} enrolled in census medical coverage ({plan_disp} ({emp.coverage})) but missing from uploaded payroll register documents."
                        F.exceptions.append(("Cross-document conflicts", who, detail))
                        F.notes.append(f"Cross-document conflicts: {detail}")
                        log.warning("      * [CONFLICT] %s", detail)

    # 7. QC audit numbers --------------------------------------------------------------------------
    src_emp = census.raw_employee_rows - census.cross_file_merges
    src_dep = census.raw_dependent_rows - census.dependent_merges
    out_emp = len([p for p in census.people if p.role == "EE" and not p.invoice_only])
    out_dep = len([p for p in census.people if p.role != "EE"])
    audit = {"src_employees": src_emp, "src_dependents": src_dep, "out_employees": out_emp,
             "out_dependents": out_dep, "missing_employees": max(0, src_emp - out_emp)}
    if audit["missing_employees"] or src_dep != out_dep:
        F.notes.append("Census totals do not reconcile to the source census files - review required.")
    log.info("      * Audit: Source EEs=%d, Source Deps=%d -> Output EEs=%d, Output Deps=%d",
             src_emp, src_dep, out_emp, out_dep)

    # 8. write carrier-specific workbook(s) --------------------------------------------------------
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = (prospect.company or "Prospect").replace("/", "-").strip()[:40]

    sbu_path: Path | None = None

    if routing.carrier == C.CARRIER_FLORIDA_BLUE or routing.is_florida:
        # Florida Blue -> RAPT Census Comparison Workbook only
        log.info("[7/8] Generating Florida Blue RAPT Census Excel workbook...")
        out_path = output_dir / f"RAPT_Census_{name}_{stamp}.xlsx".replace(" ", "_")
        save_rapt_workbook(C.RAPT_TEMPLATE_PATH, out_path, census, prospect, results, F, audit)
        log.info("      * Saved Florida Blue Excel: %s", out_path.name)
    else:
        # Non-Florida (or default Cigna routing) -> Marsh Primary + SBU Deliverables
        log.info("[7/8] Generating Cigna Marsh PEO Practice Census Comparison workbook...")
        out_path = output_dir / f"Marsh_Census_{name}_{stamp}.xlsx".replace(" ", "_")
        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, out_path, census, prospect, results, F, audit)
        log.info("      * Saved Marsh Cigna Excel: %s", out_path.name)

        log.info("      * Generating SBU Census Template deliverable from finalized census...")
        sbu_path = output_dir / f"SBU_Census_{name}_{stamp}.xlsx".replace(" ", "_")
        save_sbu_workbook(C.SBU_TEMPLATE_PATH, sbu_path, census, prospect)
        log.info("      * Saved SBU Cigna Excel: %s", sbu_path.name)

    # Deliverable 2: Missing Information Email ----------------------------------------------------
    log.info("[8/8] Generating ready-to-send Missing Information Email deliverable...")
    claims_required, claims_reason = check_claims_required(
        prospect, enrolled_medical_count=F.invoice_medical_count or len([p for p in census.employees if p.coverage in C.ACTIVE_TIERS]), docs=docs
    )
    if claims_required:
        F.claims_notes.append(claims_reason)

    email_content = generate_missing_information_email(
        prospect=prospect,
        routing=routing,
        findings=F,
        census=census,
        have_census=bool(census.employees),
        have_invoice=have_invoice,
        have_renewal=bool(renewal),
        renewal_required=ren_required,
        claims_required=claims_required,
        ghq_present=bool(roles["ghq"]),
        ghq_partial=bool(missing and roles["ghq"]),
        roles=roles,
        selected_invoices=selected,
        invoice_records=invoice_records,
        results=results,
        docs=docs,
    )
    email_path = output_dir / f"Missing_Information_Email_{name}_{stamp}.txt".replace(" ", "_")
    email_path.write_text(email_content, encoding="utf-8")
    F.email_text = email_content
    log.info("      * Saved Missing Information Email: %s", email_path.name)

    summary = exception_summary(F, census, audit)
    txt_path = output_dir / out_path.name.replace(".xlsx", "_exceptions.txt")
    txt_path.write_text(summary, encoding="utf-8")
    log.info("      * Saved Exceptions Report: %s", txt_path.name)
    log.info("\n" + summary)
    log.info("\n--- MISSING INFORMATION EMAIL ---\n" + email_content)

    doc_roles = []
    for role, d_list in roles.items():
        for d in d_list:
            doc_roles.append({
                "name": d.name,
                "role": role,
                "scanned": getattr(d, "scanned", False),
                "extraction_method": getattr(d, "extraction_method", "Digital Extraction"),
                "char_count": len(getattr(d, "text", "")),
                "table_count": len(getattr(d, "tables", [])),
                "extracted_filename": d.extracted_text_path.name if getattr(d, "extracted_text_path", None) else None,
                "text_snippet": getattr(d, "text", "")[:2000],
            })
    return {
        "output_file": out_path,
        "sbu_output_file": sbu_path,
        "txt_file": txt_path,
        "email_file": email_path,
        "email_text": email_content,
        "carrier": routing.carrier or "Undetermined",
        "state": routing.state or "Unknown",
        "company": prospect.company or "",
        "prospect_name": prospect.company or "",
        "proposed_effective_date": (prospect.effective_date.strftime("%m/%d/%Y") if hasattr(prospect.effective_date, "strftime") else str(prospect.effective_date or "")),
        "sic_naics_code": prospect.sic or "",
        "company_name": prospect.company or "",
        "address": prospect.address or "",
        "city_state_zip": prospect.city_state_zip or "",
        "prospect": {
            "proposed_effective_date": (prospect.effective_date.strftime("%m/%d/%Y") if hasattr(prospect.effective_date, "strftime") else str(prospect.effective_date or "")),
            "sic_naics_code": prospect.sic or "",
            "company_name": prospect.company or "",
            "address": prospect.address or "",
            "city_state_zip": prospect.city_state_zip or "",
            "effective_date": (prospect.effective_date.strftime("%m/%d/%Y") if hasattr(prospect.effective_date, "strftime") else str(prospect.effective_date or "")),
            "sic": prospect.sic or "",
            "company": prospect.company or "",
            "referral": prospect.referral or "",
            "sales_person": prospect.sales_person or "",
        },
        "routing": {
            "carrier": routing.carrier,
            "state": routing.state,
            "is_florida": routing.is_florida,
            "primary_template": routing.primary_template.name if routing.primary_template else "",
            "requires_sbu": routing.requires_sbu,
            "ghq_required": routing.ghq_required,
        },
        "summary": summary,
        "audit": audit,
        "findings": F,
        "classified_docs": doc_roles,
        "extracted_text_dir": str(output_dir / "extracted_text"),
    }



def exception_summary(F: Findings, census, audit: dict) -> str:
    c = F.counters
    lines = [
        "EXCEPTION SUMMARY",
        f"- Census: {audit['out_employees']} employees / {audit['out_dependents']} dependents",
        f"- Missing from invoice: {c.get('missing_invoice', 0)} | Missing from census: {c.get('missing_census', 0)}",
        f"- Tier mismatches: {c.get('tier_mismatch', 0)} | Coverage/structure mismatches: "
        f"{c.get('coverage_mismatch', 0) + c.get('coverage_status_mismatch', 0) + c.get('dependent_mismatch', 0)}",
        f"- Premium discrepancies: {c.get('premium_discrepancy', 0)} | Eligibility conflicts: "
        f"{c.get('eligibility_conflict', 0)}",
        f"- Unassigned dependents: {len(census.unassigned_dependents)} | Duplicate employees: "
        f"{census.duplicate_employees}",
        f"- Most-used-plan rate applied: {c.get('most_used', 0)} | Missing renewal rates: "
        f"{c.get('missing_renewal', 0)}",
    ]
    lines += [f"- {n}" for n in F.notes]
    return "\n".join(lines)


def _cli() -> None:
    # Ensure unbuffered output so terminal logs appear in real time
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Census creation & invoice comparison")
    ap.add_argument("files", nargs="*", default=[], help="Specific file path(s) to process")
    ap.add_argument("-i", "--input", default=str(C.INPUT_DIR), help="Input folder")
    ap.add_argument("-o", "--output", default=str(C.OUTPUT_DIR), help="Output folder")
    ap.add_argument("-q", "--quiet", action="store_true", help="Only show summary and errors (suppress progress logs)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show debug level logs")
    a = ap.parse_args()

    if a.quiet:
        log_level = logging.WARNING
    elif a.verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("census").setLevel(log_level)

    src = [Path(f) for f in a.files] if a.files else Path(a.input)
    try:
        res = run_pipeline(src, Path(a.output))
        print(f"\nOutput file: {res['output_file']}\n")
        print(res["summary"])
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
