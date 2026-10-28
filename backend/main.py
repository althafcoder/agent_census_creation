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
import hashlib
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_config as C
from caching.config import (
    APP_VERSION,
    BYPASS_LOCAL_CACHE,
    INSTRUCTION_VERSION,
    SCHEMA_VERSION,
    get_instruction_hash,
)
from census_builder import build_census, extract_waiting_period_rule, refine_non_enrolled_statuses
from classifier import classify, detect_header
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
from tracer import DiagnosticTracer
from workbook_writer import (
    save_marsh_workbook,
    save_rapt_workbook,
    save_sbu_workbook,
    save_workbook,
)

log = logging.getLogger("census")


def run_pipeline(
    source: Path | list[Path | str] = C.INPUT_DIR,
    output_dir: Path = C.OUTPUT_DIR,
    bypass_cache: bool = False,
    tracer: DiagnosticTracer | None = None,
    case_id: str | None = None,
    enable_learning: bool = True,
) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    F = Findings()

    if bypass_cache:
        os.environ["BYPASS_LOCAL_CACHE"] = "true"
    tracer = tracer or DiagnosticTracer()

    # Learning Engine initialization
    le_orchestrator = None
    le_ctx = None
    le_snapshot = None
    coverage_ledger = None
    provenance = None
    validator = None

    if enable_learning:
        try:
            from learning_engine import (
                LEARNING_ENGINE_ENABLED,
                LearningEngineOrchestrator,
                ProvenanceTracker,
                SourceCoverageLedger,
                UnderwritingValidator,
            )
            if LEARNING_ENGINE_ENABLED:
                le_orchestrator = LearningEngineOrchestrator()
                le_ctx, le_snapshot = le_orchestrator.initialize_run_context(
                    case_id=case_id,
                    bypass_cache=bypass_cache,
                    output_dir=output_dir,
                )
                coverage_ledger = SourceCoverageLedger(run_id=le_ctx.run_id)
                provenance = ProvenanceTracker(run_id=le_ctx.run_id)
                validator = UnderwritingValidator(instruction_hash=le_snapshot.content_hash)
        except Exception as _le_init_err:
            log.warning("Learning Engine initialization skipped: %s", _le_init_err)

    # 1. review EVERY uploaded document ----------------------------------------------------------
    log.info("[1/7] Reading and classifying documents...")
    docs = read_all(source, dest_dir=output_dir / "extracted_text")
    if not docs:
        raise ValueError(f"No supported files found in {source}")

    if coverage_ledger:
        for d in docs:
            exp_units = [t.name for t in d.tables] or ["main"]
            from learning_engine import DocumentManifest
            manifest = DocumentManifest(
                document_id=d.name,
                filename=d.name,
                content_hash=getattr(d, "file_hash", "") or "",
                roles=[getattr(d, "role", "unknown")],
                access_status=getattr(d, "access_status", "READABLE"),
                expected_units=exp_units,
                visited_units=exp_units,
                extraction_status="COMPLETED",
            )
            coverage_ledger.register_document(manifest)
            for u in exp_units:
                coverage_ledger.record_unit_visit(d.name, u, status="VISITED")
        cov_sum = coverage_ledger.get_coverage_summary()
        log.info(
            "      * [LEARNING ENGINE] SourceCoverageLedger tracked %d files (%d/%d units visited, complete=%s)",
            len(docs),
            cov_sum.get("visited_units", 0),
            cov_sum.get("total_expected_units", 0),
            cov_sum.get("is_complete", True),
        )

    from protected_files import get_current_store, AccessStatus
    access_records = get_current_store()._last_access_records
    F.access_records = access_records
    for name, rec in access_records.items():
        if rec.status == AccessStatus.UNLOCKED_AND_READ:
            applied_pw = getattr(rec, "applied_password", "")
            pw_info = " (Decrypted with provided password)" if applied_pw else ""
            log.info("      * %-42s -> UNLOCKED AND READ%s", name, pw_info)
        elif rec.status in (AccessStatus.PASSWORD_REJECTED, AccessStatus.PASSWORD_NOT_FOUND, AccessStatus.PRESENT_LOCKED):
            log.warning("      * %-42s -> %s", name, rec.status.value)

    input_hashes: dict[str, str] = {}
    for d in docs:
        try:
            input_hashes[d.name] = hashlib.sha256(d.path.read_bytes()).hexdigest()[:12]
        except Exception:
            pass
    inst_hash = get_instruction_hash()
    log.info("Agent Execution: AppVersion=%s, SchemaVersion=%s, InstructionVersion=%s (Hash=%s)",
             APP_VERSION, SCHEMA_VERSION, INSTRUCTION_VERSION, inst_hash)
    log.info("Inputs received: %d files (Hashes: %s)", len(docs), ", ".join(f"{k}:{v}" for k, v in input_hashes.items()))

    roles: dict[str, list] = {
        "census": [], "invoice": [], "renewal": [], "ghq": [], "rfp": [],
        "deliverable": [], "payroll": [], "plan_design": [], "unknown": []
    }
    for d in docs:
        role = classify(d)
        roles.setdefault(role, []).append(d)
        log.info("      * %-42s -> %-11s %s", d.name, role, "(OCR Scanned)" if d.scanned else "(Digital)")

    # Unknown files that contain a census-looking table are still used (never ignore a genuine census file)
    unknown_docs = list(roles.get("unknown", []))
    for unk in unknown_docs:
        has_census_table = False
        for t in unk.tables:
            h = detect_header(t.df)
            if h and ({"first_name", "full_name"} & set(h.columns)) and ({"dob", "relationship", "coverage"} & set(h.columns)):
                has_census_table = True
                break
        if has_census_table:
            roles["census"].append(unk)
            if unk in roles.get("unknown", []):
                roles["unknown"].remove(unk)
            log.info("      * Unknown file %s contains census table -> promoted to census", unk.name)

    # Combine Benefits Enrollment Report (BER) / Election Report into census when census lacks medical plan columns
    census_has_plan = any(
        "plan" in (detect_header(t.df).columns if detect_header(t.df) else {})
        for cd in roles["census"] for t in cd.tables
    )
    if not census_has_plan:
        for inv_doc in roles.get("invoice", []):
            if inv_doc not in roles["census"] and re.search(r"(enroll|benefit.*report|\bber\b|election)", inv_doc.name, re.I):
                roles["census"].append(inv_doc)
                log.info("      * Enrollment report %s also added to census for plan/tier consolidation", inv_doc.name)

    # Conversely, if a Benefits Enrollment Report (BER) was classified as census and no medical carrier invoice exists,
    # include the BER in invoice comparison as an invoice equivalent (Section 11.1)
    if not roles.get("invoice"):
        for cen_doc in roles.get("census", []):
            if re.search(r"(benefit.*enroll.*report|enrollment\s*report|\bber\b)", cen_doc.name, re.I):
                roles["invoice"].append(cen_doc)
                log.info("      * Enrollment report %s also added to invoice as BER equivalent", cen_doc.name)

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
        log.warning("No census / enrollment file was found among the uploads; proceeding to generate missing info deliverable.")
        census = Census()
        meta = {"has_coverage_column": False, "saw_dependent_source": False}
        F.notes.append("No census / enrollment file was found among the uploads. Census comparison workbook was not generated.")
    else:
        census, meta = build_census(roles["census"], effective_date=prospect.effective_date, tracer=tracer)
        if not census.employees:
            fallback_docs = [d for d in docs if d not in roles["census"] and d.tables and d.access_status in ("READABLE", "UNLOCKED_AND_READ")]
            if fallback_docs:
                log.info("      * Primary census yielded 0 employees; attempting fallback consolidation with %s", [d.name for d in fallback_docs])
                census, meta = build_census(roles["census"] + fallback_docs, effective_date=prospect.effective_date, tracer=tracer)
        if not census.employees:
            log.warning("Census files were found but no employee rows could be parsed; proceeding to generate missing info deliverable.")
            F.notes.append("Census files were provided but 0 employee rows could be read from them.")
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
    refine_non_enrolled_statuses(
        census, reference_date=ref_date, waiting_days=wait_days, waiting_rule=wait_rule,
        docs=docs, effective_date=prospect.effective_date, tracer=tracer,
    )

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

    renewal_to_use = renewal if ren_required else None

    # 6. compare + populate premiums -------------------------------------------------------------
    log.info("[6/7] Running comparison and audit reconciliation...")
    results = run_comparison(
        census, invoice_records, renewal_to_use, meta, F, have_invoice,
        renewal_required=ren_required, tracer=tracer,
    )

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

    out_path: Path | None = None
    sbu_path: Path | None = None

    if not census.employees:
        log.info("[7/8] Census is absent or empty; withholding carrier comparison workbooks per Section 25.")
    elif routing.carrier == C.CARRIER_FLORIDA_BLUE or routing.is_florida:
        # Florida Blue -> RAPT Census Comparison Workbook only
        log.info("[7/8] Generating Florida Blue RAPT Census Excel workbook...")
        out_path = output_dir / f"RAPT_Census_{name}_{stamp}.xlsx".replace(" ", "_")
        save_rapt_workbook(C.RAPT_TEMPLATE_PATH, out_path, census, prospect, results, F, audit, tracer=tracer)
        log.info("      * Saved Florida Blue Excel: %s", out_path.name)
    elif routing.carrier == C.CARRIER_CIGNA or (routing.state and routing.state.upper() not in ("FL", "UNKNOWN", "CONFLICTING")):
        # Confirmed Non-Florida (Cigna routing) -> Marsh Primary + SBU Deliverables
        log.info("[7/8] Generating Cigna Marsh PEO Practice Census Comparison workbook...")
        out_path = output_dir / f"Marsh_Census_{name}_{stamp}.xlsx".replace(" ", "_")
        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, out_path, census, prospect, results, F, audit, tracer=tracer)
        log.info("      * Saved Marsh Cigna Excel: %s", out_path.name)

        log.info("      * Generating SBU Census Template deliverable from finalized census...")
        sbu_path = output_dir / f"SBU_Census_{name}_{stamp}.xlsx".replace(" ", "_")
        save_sbu_workbook(C.SBU_TEMPLATE_PATH, sbu_path, census, prospect)
        log.info("      * Saved SBU Cigna Excel: %s", sbu_path.name)
    else:
        # Unknown or conflicting business state -> no carrier workbook per Section 4.2!
        log.info("[7/8] Primary business state unknown/conflicting; withholding carrier workbooks per Section 4.2.")

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

    # Diagnostic Trace deliverable
    trace_summary = tracer.format_summary()
    trace_path = output_dir / f"Diagnostic_Trace_{name}_{stamp}.txt".replace(" ", "_")
    trace_path.write_text(trace_summary, encoding="utf-8")
    log.info("      * Saved Diagnostic Trace: %s", trace_path.name)

    summary = exception_summary(F, census, audit)
    if out_path:
        txt_path = output_dir / out_path.name.replace(".xlsx", "_exceptions.txt")
    else:
        txt_path = output_dir / f"Exceptions_Audit_{name}_{stamp}.txt".replace(" ", "_")
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
    le_status = "PASSED"
    le_val_report = None
    if validator:
        le_val_report = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census, selected_invoices=selected,
            renewal=renewal, ren_required=ren_required, claims_required=claims_required,
            findings=F, results=results, roles=roles,
        )
        le_status = le_val_report.release_status.value
        if le_orchestrator and le_ctx:
            try:
                if not le_ctx.case_id or le_ctx.case_id == "case_default":
                    slug = "".join(ch if ch.isalnum() else "_" for ch in (prospect.company or "case")).strip("_").lower()[:40]
                    if slug:
                        le_ctx.case_id = f"case_{slug}"
                le_orchestrator._ensure_validated_repair_patterns(le_ctx, le_val_report)
                case_audit = le_orchestrator._build_private_case_audit(
                    ctx=le_ctx,
                    docs=docs,
                    coverage_ledger=coverage_ledger,
                    census=census,
                    report=le_val_report,
                    executed_patches=[],
                    attempt=0,
                )
                le_orchestrator.memory_store.record_run_data(le_ctx.case_id, le_ctx.run_id, "case_audit", case_audit)
                le_orchestrator.memory_store.record_run_data(le_ctx.case_id, le_ctx.run_id, "final_validation", le_val_report.to_dict())
                if coverage_ledger:
                    le_orchestrator.memory_store.record_run_data(le_ctx.case_id, le_ctx.run_id, "coverage_ledger", coverage_ledger.to_dict())
                if provenance:
                    le_orchestrator.memory_store.record_run_data(le_ctx.case_id, le_ctx.run_id, "provenance", provenance.to_dict())

                gates = le_val_report.execution_gates or {}
                gate_str = " | ".join(f"{k}={v.get('status', 'PASS')}" for k, v in gates.items())
                pop_sum = case_audit.get("canonical_population_summary", {})
                disp_counts = pop_sum.get("disposition_counts", {})
                b_gate = gates.get("B_gender_resolution", {})
                run_dir = le_orchestrator.memory_store.data_dir / "runs" / le_ctx.case_id / le_ctx.run_id
                le_log = logging.getLogger("census.learning_engine.orchestrator")

                banner_lines = [
                    "=" * 88,
                    f"[LEARNING ENGINE] VERIFICATION SUMMARY | Status={le_status} | Checks={le_val_report.passed_checks}/{le_val_report.total_checks} Passed",
                    f"  * [INSTRUCTION SNAPSHOT] Hash={le_snapshot.content_hash[:12] if le_snapshot else inst_hash} | Case={le_ctx.case_id} | Run={le_ctx.run_id}",
                    "  * [INSTRUCTION & REPAIR RULES APPLIED]:",
                ]
                for chk in le_val_report.checks:
                    st_val = chk.status.value if hasattr(chk.status, "value") else str(chk.status)
                    banner_lines.append(f"      - [{st_val}] {chk.rule_id} (Section {chk.section} - {chk.name}): {chk.reason}")

                banner_lines.append("  * [VALIDATED LEARNING PATTERNS APPLIED]:")
                for vp in le_orchestrator.memory_store.load_validated_patterns():
                    banner_lines.append(
                        f"      - [{vp.get('pattern_state', 'VALIDATED')}] {vp.get('learning_id')} "
                        f"(v{vp.get('version', 1)}, patch={vp.get('patch_version', '2.2.0')}, fp={vp.get('fingerprint', '')})"
                    )

                banner_lines.extend([
                    f"  * [POPULATION & DISPOSITIONS] CanonicalRows={pop_sum.get('total_canonical_rows', 0)} "
                    f"(Employees={pop_sum.get('canonical_employees', 0)}, ActiveMedDeps={pop_sum.get('active_medical_dependents', 0)}) | "
                    f"ExcludedNonMedDeps={pop_sum.get('excluded_non_medical_or_pt_dependents', 0)} | "
                    f"MaskedSlotsIgnored={pop_sum.get('masked_placeholder_slots_ignored', 0)} | Dispositions={disp_counts}",
                    f"  * [GENDER RESOLUTION] Resolved(M/F)={b_gate.get('resolved_m_f_count', 0)}/{b_gate.get('total_canonical_people', 0)} | "
                    f"UnresolvedBlank={b_gate.get('unresolved_missing_count', 0)} | Conflicts={b_gate.get('conflict_count', 0)}",
                    f"  * [EXECUTION GATES A-F] {gate_str}",
                    f"  * [AUDIT ARTIFACTS SAVED] {run_dir}",
                    f"[LEARNING ENGINE] ALL RULES APPLIED BASED ON ACTIVE INSTRUCTIONS -- LEARNING VERIFICATION COMPLETED (Status: {le_status})",
                    "=" * 88,
                ])

                for bline in banner_lines:
                    le_log.info(bline)
                    if le_log.propagate is False:
                        log.info(bline)

                summary = summary + "\n\n" + "\n".join(banner_lines)
                txt_path.write_text(summary, encoding="utf-8")
            except Exception as _le_save_err:
                log.warning("Learning Engine artifact persistence warning: %s", _le_save_err)

    return {
        "output_file": out_path,
        "sbu_output_file": sbu_path,
        "txt_file": txt_path,
        "email_file": email_path,
        "email_text": email_content,
        "trace_file": trace_path,
        "tracer": tracer,
        "tracer_summary": trace_summary,
        "app_version": APP_VERSION,
        "instruction_version": INSTRUCTION_VERSION,
        "instruction_hash": inst_hash,
        "input_hashes": input_hashes,
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
        "census": census,
        "summary": summary,
        "audit": audit,
        "findings": F,
        "classified_docs": doc_roles,
        "extracted_text_dir": str(output_dir / "extracted_text"),
        "learning_engine": {
            "engine_status": le_status,
            "case_id": le_ctx.case_id if le_ctx else None,
            "run_id": le_ctx.run_id if le_ctx else None,
            "instruction_hash": le_snapshot.content_hash if le_snapshot else inst_hash,
            "validation_report": le_val_report.to_dict() if le_val_report else None,
        },
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
    ap.add_argument("--bypass-cache", action="store_true", help="Bypass local SQLite prompt/response cache")
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
        res = run_pipeline(src, Path(a.output), bypass_cache=a.bypass_cache)
        print(f"\nOutput file: {res['output_file']}\n")
        print(res["summary"])
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
