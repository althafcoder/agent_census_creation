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
from email_evidence import extract_email_evidence
from invoice_parser import combine_records, parse_invoice, select_invoices
from models import Census, Findings
from agent_orchestrator import run_agent_intake_plan
from population_sanity import evaluate_population_sanity
from prospect import build_prospect
from submission_blueprint import generate_submission_blueprint, verify_invoice_control_totals
from readers import read_all
from renewal_parser import parse_renewal, select_renewal
from router import (
    build_missing_information_email_parts,
    check_claims_required,
    check_renewal_required,
    determine_routing,
    format_missing_information_email_text,
    write_missing_information_docx,
)
from tracer import DiagnosticTracer
from utils import sanitize_filename
from workbook_auditor import audit_generated_deliverables
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
                EngineStatus,
                RepairPatch,
            )
            if LEARNING_ENGINE_ENABLED:
                from learning_engine.memory_store import MemoryStore
                _is_test_tmp = bool(
                    os.getenv("PYTEST_CURRENT_TEST")
                    and not os.getenv("LEARNING_ENGINE_DATA_DIR")
                    and output_dir.resolve() != C.OUTPUT_DIR.resolve()
                )
                _ms = MemoryStore(data_dir=output_dir / "learning_engine_data") if _is_test_tmp else None
                le_orchestrator = LearningEngineOrchestrator(memory_store=_ms)
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

    # 1.5 Claude Tool-Using Intake Agent & Whole-Submission Blueprint -----------------------------
    # Interactive tool-using agent inspects all files (with on-demand row/PDF drill-down tools)
    # and commits a strictly validated Blueprint via native Tool Use; falls back cleanly to
    # single-pass Blueprint or deterministic execution when offline.
    blueprint = run_agent_intake_plan(docs, bypass_cache=bypass_cache)
    if blueprint is None:
        blueprint = generate_submission_blueprint(docs)

    roles: dict[str, list] = {
        "census": [], "invoice": [], "renewal": [], "ghq": [], "rfp": [],
        "deliverable": [], "payroll": [], "plan_design": [], "unknown": []
    }
    from classifier import is_comparison_output_doc, is_sbc_document, is_wage_document
    for d in docs:
        bp_role = blueprint.role_for(d.name) if blueprint is not None else None
        if is_comparison_output_doc(d):
            role = "deliverable"
        elif is_wage_document(d):
            role = "payroll"
        elif is_sbc_document(d):
            role = "plan_design"
        elif bp_role:
            role = bp_role
            log.info("      * [BLUEPRINT] %-34s -> %-11s", d.name, role)
        else:
            role = classify(d)
        if blueprint is not None and role == "plan_design":
            bp_sbc_ids = blueprint.sbc_plan_ids_for(d.name)
            if bp_sbc_ids:
                setattr(d, "sbc_plan_ids", bp_sbc_ids)
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

    # Pre-consolidation source & header gate: strip any completed comparison deliverable
    # from source roles and warn if a census table lacks relationship/coverage mapping.
    from classifier import is_comparison_output_doc
    for src_role in ("census", "invoice", "renewal"):
        for doc_item in list(roles.get(src_role, [])):
            if is_comparison_output_doc(doc_item):
                roles[src_role].remove(doc_item)
                if doc_item not in roles["deliverable"]:
                    roles["deliverable"].append(doc_item)
                log.warning("      * Pre-consolidation gate: removed deliverable '%s' from %s role", doc_item.name, src_role)
    for cen_doc in roles.get("census", []):
        for tbl in getattr(cen_doc, "tables", []) or []:
            bp_sc_chk = blueprint.census_schema_for(cen_doc.name, tbl.name) if blueprint is not None else None
            hdr = detect_header(tbl.df, blueprint_schema=bp_sc_chk)
            if hdr and ({"first_name", "full_name"} & set(hdr.columns)) and "dob" in hdr.columns:
                missing_hdrs = [k for k in ("relationship", "coverage") if k not in hdr.columns and not hdr.wide_deps]
                if missing_hdrs:
                    log.warning("      * Header-mapping gate: '%s' (%s) missing mapped columns %s", cen_doc.name, tbl.name, missing_hdrs)

    # When dedicated census spreadsheets exist (e.g. Wellfit Census 2026 or Employee Census 3),
    # reserve the multi-benefit enrollment report (BER) strictly for invoice-equivalent comparison (Section 11.1).
    # Never add BER to census builder when dedicated census spreadsheets exist to prevent non-medical inflation.
    dedicated_censuses = [d for d in roles.get("census", []) if re.search(r"(census|roster)", d.name, re.I)]
    if dedicated_censuses:
        for cen_doc in list(roles.get("census", [])):
            if re.search(r"(benefit.*enroll|enrollment(\s*report)?|\bber\b|benefit\s*election)", cen_doc.name, re.I):
                if cen_doc not in roles.setdefault("invoice", []):
                    roles["invoice"].append(cen_doc)
                roles["census"].remove(cen_doc)
                log.info("      * Dedicated census files exist (%s); BER %s reserved for invoice-equivalent comparison",
                         [d.name for d in dedicated_censuses], cen_doc.name)
    elif not roles.get("invoice"):
        for cen_doc in list(roles.get("census", [])):
            if re.search(r"(benefit.*enroll|enrollment(\s*report)?|\bber\b|benefit\s*election)", cen_doc.name, re.I):
                roles.setdefault("invoice", []).append(cen_doc)
                log.info("      * Enrollment report %s also added to invoice as BER equivalent", cen_doc.name)

    # Combine Benefits Enrollment Report (BER) / Election Report into census ONLY when NO dedicated census files exist
    # and census lacks medical plan columns
    if not dedicated_censuses:
        census_has_plan = any(
            ("plan" in (detect_header(t.df).columns if detect_header(t.df) else {})) or
            ("option_code" in (detect_header(t.df).columns if detect_header(t.df) else {}))
            for cd in roles["census"] for t in cd.tables
        )
        if not census_has_plan:
            for inv_doc in roles.get("invoice", []):
                if inv_doc not in roles["census"] and re.search(r"(enroll|benefit.*report|\bber\b|election)", inv_doc.name, re.I):
                    roles["census"].append(inv_doc)
                    log.info("      * Enrollment report %s also added to census for plan/tier consolidation", inv_doc.name)

    # 2. prospect block & routing decision --------------------------------------------------------
    log.info("[2/7] Extracting Prospect Information & Determining Carrier Routing...")
    prospect, has_source, missing = build_prospect(roles["ghq"], roles["rfp"], all_docs=docs, blueprint=blueprint)
    if blueprint is not None:
        blueprint.apply_to_prospect(prospect)
        if prospect.company or prospect.state or prospect.effective_date:
            has_source = True
    
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
                       ("DHR Sales Person", prospect.sales_person)]
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
    anchored_med_plans: set[str] = set()
    used_agent_py_first = False
    py_first_invoices: Optional[list[Any]] = None
    if not roles["census"]:
        log.warning("No census / enrollment file was found among the uploads; proceeding to generate missing info deliverable.")
        census = Census()
        meta = {"has_coverage_column": False, "saw_dependent_source": False}
        F.notes.append("No census / enrollment file was found among the uploads. Census comparison workbook was not generated.")
    else:
        for d in docs:
            d_low = d.name.lower()
            if "sbc" in d_low or "summary of benefits" in d_low or d in roles.get("invoice", []):
                for tok in re.findall(r"\b[a-z]{1,5}\d{3,6}[a-z]\d{3,5}[a-z]?\b", (d.name + " " + (d.text or "")[:4000]), re.I):
                    anchored_med_plans.add(tok.lower())

        # Pillar #1 & #4: Agent-Python-First Mode (Claude + Sandboxed Python is the PRIMARY builder
        # on live runs; deterministic census_builder.py acts strictly as offline/error fallback).
        try:
            from agent_orchestrator import run_agent_python_first_build
            py_first_census, py_first_invoices = run_agent_python_first_build(
                docs,
                blueprint=blueprint,
                prospect=prospect,
                effective_date=prospect.effective_date,
            )
            if py_first_census is not None and py_first_census.canonical_census_employees:
                census = py_first_census
                meta = {
                    "has_coverage_column": any(bool(e.coverage) for e in census.canonical_census_employees),
                    "saw_dependent_source": bool(census.dependents),
                    "company": prospect.company,
                    "builder": "agent_python_first",
                }
                used_agent_py_first = True
                log.info(
                    "🚀 [AGENT PYTHON-FIRST] Primary Census built directly via Python Sandbox: %d EEs, %d Deps",
                    len(census.canonical_census_employees), len(census.dependents),
                )
        except Exception as _pyf_err:
            log.warning("Agent-Python-First primary build skipped (falling back to deterministic census_builder): %s", _pyf_err)

        if not used_agent_py_first:
            census, meta = build_census(
                roles["census"],
                effective_date=prospect.effective_date,
                tracer=tracer,
                anchored_medical_plans=anchored_med_plans,
                blueprint=blueprint,
            )
            meta["company"] = prospect.company
            if not census.employees:
                excluded_fallback = set(id(x) for x in (roles.get("census", []) + roles.get("deliverable", [])))
                fallback_docs = [d for d in docs if id(d) not in excluded_fallback and d.tables and d.access_status in ("READABLE", "UNLOCKED_AND_READ")]
                if fallback_docs:
                    log.info("      * Primary census yielded 0 employees; attempting fallback consolidation with %s", [d.name for d in fallback_docs])
                    census, meta = build_census(
                        roles["census"] + fallback_docs,
                        effective_date=prospect.effective_date,
                        tracer=tracer,
                        anchored_medical_plans=anchored_med_plans,
                    )
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
    if py_first_invoices and any(getattr(i, "records", None) for i in py_first_invoices):
        inv_docs = py_first_invoices
        log.info("🚀 [AGENT PYTHON-FIRST] Using %d Python-verified medical invoice(s)", len(inv_docs))
    else:
        inv_docs = [parse_invoice(d) for d in roles["invoice"]]
    if blueprint is not None:
        blueprint.apply_to_invoices(inv_docs)
    selected, inv_findings, period = select_invoices(inv_docs)
    inv_control_gate = verify_invoice_control_totals(selected)
    if not inv_control_gate["passed"]:
        for _cg_issue in inv_control_gate["issues"]:
            log.warning("⚠️ [INVOICE CONTROL-TOTAL GATE] %s", _cg_issue)
            if _cg_issue not in F.notes:
                F.notes.append(_cg_issue)
            F.exceptions.append(("Invoice control-total mismatch", "Selected Invoice", _cg_issue))
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
    if not used_agent_py_first:
        refine_non_enrolled_statuses(
            census, reference_date=ref_date, waiting_days=wait_days, waiting_rule=wait_rule,
            docs=docs, effective_date=prospect.effective_date, tracer=tracer,
        )

    # 4.2 Authoritative Population Controls (Test/Placeholder rows + Agent-identified excluded/terminated employees)
    # Skip heuristic multi-file exclusion when Agent-Python-First already built the exact canonical population
    # (so email-added employees like new hires are never accidentally stripped by file-membership heuristics).
    if census and census.employees and not used_agent_py_first:
        from utils import name_similarity, split_full_name
        from census_builder import recalculate_row_numbers
        bp_exclusions = list(getattr(blueprint, "excluded_employees", None) or []) if blueprint is not None else []
        bp_excl_pairs = [
            (split_full_name(item["person_name"]), item.get("reason", "Excluded by Intake Agent"))
            for item in bp_exclusions
            if isinstance(item, dict) and item.get("person_name")
        ]
        # Never exclude an employee who is actively billed on the current medical invoice
        inv_name_pairs = [(r.first, r.last) for r in (invoice_records or []) if r.first or r.last]

        # Detect an authoritative FT/PT active-status roster when 2+ census files are uploaded:
        # a single census file that explicitly enumerates BOTH active FT and PT employees covering >=50%
        # of the combined workforce is the authoritative active-employee population.
        all_emps_incl_pt = list(census.employees) + [
            ex for ex in (getattr(census, "excluded_employees", None) or [])
            if ex not in census.employees
        ]
        auth_roster_files: set[str] = set()
        pt_def_files = set(getattr(census, "pt_defining_files", None) or set())
        if len(set(getattr(census, "files_used", None) or [])) >= 2 and pt_def_files and len(all_emps_incl_pt) >= 10:
            for cand_f in pt_def_files:
                f_emps = [e for e in all_emps_incl_pt if cand_f in (getattr(e, "source_files", None) or {e.source})]
                f_pt = sum(1 for e in f_emps if getattr(e, "disposition", "") == "EXCLUDE_PART_TIME" or (e.worker_category or "").upper() == "PT")
                f_ft = len(f_emps) - f_pt
                if f_ft >= 5 and f_pt >= 5 and len(f_emps) >= len(all_emps_incl_pt) * 0.50:
                    auth_roster_files.add(cand_f)

        to_remove_ids: set[int] = set()
        excluded_names_log: list[str] = []
        absent_from_auth_names: list[str] = []
        for e in list(census.employees):
            if getattr(e, "invoice_only", False) or getattr(e, "disposition", "").startswith("EXCLUDE_"):
                continue
            full_low = f"{e.first} {e.last}".strip().lower()
            is_test_row = full_low in ("test employee", "employee test", "sample employee", "test user", "dummy employee")
            matched_reason = "Test/placeholder record" if is_test_row else ""
            if not matched_reason and auth_roster_files:
                e_files = getattr(e, "source_files", None) or {e.source}
                if not (e_files & auth_roster_files):
                    on_active_inv = any(name_similarity((e.first, e.last), ip) >= 0.88 for ip in inv_name_pairs)
                    if not on_active_inv:
                        matched_reason = f"Absent from authoritative active FT/PT roster ({', '.join(sorted(auth_roster_files))}) and not on medical invoice"
                        absent_from_auth_names.append(e.display)
            if not matched_reason and bp_excl_pairs:
                on_active_inv = any(name_similarity((e.first, e.last), ip) >= 0.88 for ip in inv_name_pairs)
                if not on_active_inv:
                    for (ex_f, ex_l), rsn in bp_excl_pairs:
                        if name_similarity((e.first, e.last), (ex_f, ex_l)) >= 0.88:
                            matched_reason = rsn or "Excluded by Intake Agent"
                            break
            if matched_reason:
                e.disposition = "EXCLUDE_TERMINATED" if not is_test_row else "EXCLUDE_PLACEHOLDER"
                e.eligibility_status = e.disposition
                if e not in census.excluded_employees:
                    census.excluded_employees.append(e)
                to_remove_ids.add(id(e))
                for dep in list(getattr(e, "dependents", []) or []):
                    dep.disposition = e.disposition
                    if dep not in census.excluded_dependents:
                        census.excluded_dependents.append(dep)
                    to_remove_ids.add(id(dep))
                excluded_names_log.append(f"{e.display} ({matched_reason[:60]})")
        if to_remove_ids:
            census.people = [p for p in census.people if id(p) not in to_remove_ids]
            recalculate_row_numbers(census)
            log.info(
                "🤖 [AGENT POPULATION CONTROL] Excluded %d non-active/placeholder employee(s): %s",
                len(excluded_names_log), ", ".join(excluded_names_log[:12]),
            )
            if absent_from_auth_names:
                _abs_note = (
                    f"{len(absent_from_auth_names)} employee(s) appeared only in a secondary demographic file and are absent from the "
                    f"authoritative active/part-time roster ({', '.join(sorted(auth_roster_files))}) and current medical billing; "
                    f"excluded pending termination confirmation: {', '.join(absent_from_auth_names)}."
                )
                if _abs_note not in F.notes:
                    F.notes.append(_abs_note)

        # Apply Python-verified status overrides from the Intake Agent Blueprint (e.g. WO/WP/NE/COBRA)
        bp_overrides = list(getattr(blueprint, "status_overrides", None) or []) if blueprint is not None else []
        if bp_overrides:
            ov_applied = 0
            for ov in bp_overrides:
                if not isinstance(ov, dict) or not ov.get("person_name") or not ov.get("coverage"):
                    continue
                ov_f, ov_l = split_full_name(ov["person_name"])
                ov_cov = str(ov.get("coverage") or "").strip().upper()
                ov_cobra = str(ov.get("cobra") or "").strip().upper()
                ov_rsn = str(ov.get("reason") or "Intake Agent Python-verified status")
                for e in census.employees:
                    if getattr(e, "invoice_only", False) or str(getattr(e, "disposition", "") or "").startswith("EXCLUDE_"):
                        continue
                    if name_similarity((e.first, e.last), (ov_f, ov_l)) >= 0.88:
                        # Do not demote an employee who is actively billed on the medical invoice
                        on_active_inv = any(name_similarity((e.first, e.last), ip) >= 0.88 for ip in inv_name_pairs)
                        if on_active_inv and ov_cov in ("WO", "WP", "NE", "RC"):
                            continue
                        # Do not promote a dual-role spouse employee back to FAM/EF if they are already carried as SP under another subscriber
                        is_carried_as_sp = any(
                            d.role in ("SP", "DP") and name_similarity((e.first, e.last), (d.first, d.last)) >= 0.88
                            for other_e in census.employees if other_e is not e
                            for d in (getattr(other_e, "dependents", None) or [])
                        )
                        if is_carried_as_sp and e.coverage == "WO" and ov_cov in ("FAM", "EF", "ES", "EC"):
                            continue
                        if ov_cov in ("EE", "ES", "EC", "FAM", "WO", "WP", "NE", "RC"):
                            e.coverage = ov_cov
                            if ov_cov in ("WO", "WP", "NE", "RC"):
                                e.plan = "NA"
                                e.disposition = "RETAIN_MEDICAL_STATUS"
                                e.eligibility_status = {"WO": "WAIVED", "WP": "WAITING_PERIOD", "NE": "NOT_ELIGIBLE", "RC": "REFUSED"}[ov_cov]
                            else:
                                e.disposition = "INCLUDE_ACTIVE_MEDICAL"
                                e.eligibility_status = "ENROLLED"
                        if ov_cobra in ("Y", "N"):
                            e.cobra = ov_cobra
                        e.note(f"Status set to {e.coverage} ({ov_rsn})", red=False)
                        ov_applied += 1
                        break
            if ov_applied:
                log.info("🤖 [AGENT STATUS OVERRIDES] Applied %d Python-verified status override(s)", ov_applied)

        # Apply Python-verified dependent links from the Intake Agent Blueprint (e.g. different-surname households)
        bp_dep_links = list(getattr(blueprint, "dependent_links", None) or []) if blueprint is not None else []
        if bp_dep_links:
            from census_builder import apply_dependent_links
            dl_applied = apply_dependent_links(census, bp_dep_links)
            if dl_applied:
                log.info("🤖 [AGENT DEPENDENT LINKS] Applied %d Python-verified dependent link(s)", dl_applied)

    # 4.5 Dynamic Population Sanity Gate (Fix #3) --------------------------------------------------
    # Cross-check parsed census totals against the submission's own stated headcount, invoice
    # subscriber count, and internal structural invariants (unassigned-dependent spikes, ratios,
    # demographics collapse). If a critical structural failure is found, attempt one LLM-guided
    # column/value remap pass before proceeding to comparison or workbook generation.
    sanity_report = evaluate_population_sanity(
        census, prospect=prospect, invoice_records=invoice_records, docs=docs, roles=roles,
    )
    if sanity_report.blocked and roles["census"]:
        log.warning(
            "⚠️ [POPULATION SANITY GATE] %d critical anomaly(ies) detected before comparison: %s",
            sanity_report.critical_count,
            "; ".join(i.message for i in sanity_report.issues if i.severity == "critical"),
        )
        try:
            remapped_census, remapped_meta = build_census(
                roles["census"],
                effective_date=prospect.effective_date,
                tracer=tracer,
                anchored_medical_plans=anchored_med_plans,
                force_llm_remap=True,
            )
            remapped_meta["company"] = prospect.company
            refine_non_enrolled_statuses(
                remapped_census, reference_date=ref_date, waiting_days=wait_days, waiting_rule=wait_rule,
                docs=docs, effective_date=prospect.effective_date, tracer=tracer,
            )
            remapped_sanity = evaluate_population_sanity(
                remapped_census, prospect=prospect, invoice_records=invoice_records, docs=docs, roles=roles,
            )
            if remapped_census.employees and remapped_sanity.critical_count < sanity_report.critical_count:
                log.info(
                    "✅ [POPULATION SANITY GATE] Dynamic LLM schema remap resolved anomalies (%d -> %d critical; EEs=%d, Deps=%d).",
                    sanity_report.critical_count, remapped_sanity.critical_count,
                    len(remapped_census.employees), len(remapped_census.dependents),
                )
                census = remapped_census
                meta = remapped_meta
                sanity_report = remapped_sanity
        except Exception as _remap_err:
            log.warning("Population sanity recovery remap skipped: %s", _remap_err)

    # Copilot-style Sanity-Gate Fallback: if deterministic heuristics + remap STILL fail the
    # Population Sanity Gate on an unseen HRIS layout, let Claude build the canonical Census
    # directly via pandas inside PythonSandboxSession (100% row fidelity, zero token truncation).
    if sanity_report.blocked and roles["census"]:
        try:
            from agent_orchestrator import build_canonical_census_via_python_agent
            rescued_census = build_canonical_census_via_python_agent(
                docs,
                sanity_report.issues,
                prospect=prospect,
                effective_date=prospect.effective_date,
            )
            if rescued_census is not None and rescued_census.canonical_census_employees:
                rescued_sanity = evaluate_population_sanity(
                    rescued_census, prospect=prospect, invoice_records=invoice_records, docs=docs, roles=roles,
                )
                if not rescued_sanity.blocked or rescued_sanity.critical_count < sanity_report.critical_count:
                    log.info(
                        "✅ [SANITY-GATE PYTHON RESCUE] Agent-built canonical census resolved anomalies (%d -> %d critical; EEs=%d, Deps=%d).",
                        sanity_report.critical_count, rescued_sanity.critical_count,
                        len(rescued_census.canonical_census_employees), len(rescued_census.dependents),
                    )
                    census = rescued_census
                    sanity_report = rescued_sanity
        except Exception as _py_rescue_err:
            log.warning("Sanity-Gate Python Rescue skipped: %s", _py_rescue_err)

    meta["population_sanity"] = sanity_report.to_dict()
    setattr(census, "population_sanity", sanity_report)
    if sanity_report.blocked:
        for _si in sanity_report.issues:
            if _si.severity == "critical":
                if _si.message not in F.notes:
                    F.notes.append(_si.message)
                F.exceptions.append(("Population sanity gate", "Census Population", _si.message))

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
    known_ren_dt = plan_year_renewal or getattr(prospect, "renewal_date", None)
    ren_required, ren_reason = check_renewal_required(
        prospect, renewal_doc=renewal, docs=docs, proposed_effective=prospect.effective_date,
        known_renewal_date=known_ren_dt,
    )
    F.renewal_reason = ren_reason
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

    renewal_to_use = renewal if (ren_required or (renewal and getattr(renewal, "rates", None))) else None

    # Single-medical-SBC plan name backfill: when neither census nor invoice supplies a plan column
    # and the submission contains exactly one medical SBC / Benefit Highlights document, derive that
    # single medical plan name dynamically and populate enrolled employees & their linked dependents.
    if census and census.employees:
        from census_builder import _is_non_medical
        _NON_MED_DOC_RE = re.compile(
            r"(?:^|[^a-z0-9])(dental|vision|vsp|eyemed|life|ad&?d|std|ltd|accident|critical|hospital|ancillary)(?:$|[^a-z0-9])",
            re.I,
        )
        need_plan = [e for e in census.employees if e.coverage in C.ACTIVE_TIERS and not (e.plan or "").strip()]
        if need_plan and not any((r.plan or "").strip() for r in invoice_records):
            _BOOKLET_RE = re.compile(r"\b(benefits?\s+booklet|benefits?\s+guide|enrollment\s+guide|employee\s+guide)\b", re.I)
            med_sbcs = [
                d for d in (roles.get("plan_design", []) or [])
                if not _NON_MED_DOC_RE.search(d.name or "")
                and not _is_non_medical(d.name, "")
                and not _BOOKLET_RE.search(f"{d.name} {(d.text or '')[:1200]}")
                and not re.search(r"^\s*(?:dental|vision|blue\s*20/20|life\s+insurance)\b", (d.text or "")[:300], re.I)
            ]
            if len(med_sbcs) == 1:
                raw_stem = med_sbcs[0].path.stem
                # Strip leading carrier/prefix token separated by '_' when a multi-word plan name follows (e.g. BCBS_Blue Options PPO 1500 -> Blue Options PPO 1500)
                if "_" in raw_stem:
                    _pfx, _rest = raw_stem.split("_", 1)
                    if len(_pfx.strip()) <= 8 and len(_rest.strip().split()) >= 2:
                        raw_stem = _rest
                sbc_title = re.sub(r"[_\s]+", " ", raw_stem).strip()
                if sbc_title:
                    for e in need_plan:
                        e.plan = sbc_title
                        for dep in getattr(e, "dependents", []) or []:
                            if not (dep.plan or "").strip():
                                dep.plan = sbc_title
                    for r_inv in invoice_records:
                        if not (r_inv.plan or "").strip():
                            r_inv.plan = sbc_title
                    log.info("      * Populated single-medical-SBC plan name '%s' for %d enrolled employee(s)", sbc_title, len(need_plan))

    if selected and len(selected) == 1:
        _sel0 = selected[0]
        if getattr(_sel0, "total_due", None) is not None:
            meta["printed_invoice_total_due"] = float(_sel0.total_due)
        if getattr(_sel0, "non_medical_charges", None) is not None:
            meta["invoice_non_medical_charges"] = float(_sel0.non_medical_charges)
        if getattr(_sel0, "non_medical_breakdown", None):
            meta["invoice_non_medical_breakdown"] = dict(_sel0.non_medical_breakdown)
        if getattr(_sel0, "gross_total_premium", None) is not None:
            meta["invoice_gross_total_premium"] = float(_sel0.gross_total_premium)
    all_non_med_inv = [r for s_inv in selected for r in (getattr(s_inv, "non_medical_records", None) or [])]
    if all_non_med_inv:
        meta["non_medical_invoice_records"] = all_non_med_inv

    # 6. compare + populate premiums -------------------------------------------------------------
    log.info("[6/7] Running comparison and audit reconciliation...")
    email_evs = extract_email_evidence(docs, roles=roles)
    if blueprint is not None:
        bp_evs = blueprint.as_person_evidence()
        seen_ev = {(e.event_type, e.first.lower(), e.last.lower()) for e in email_evs}
        for bev in bp_evs:
            k_ev = (bev.event_type, bev.first.lower(), bev.last.lower())
            if k_ev not in seen_ev:
                email_evs.append(bev)
                seen_ev.add(k_ev)
        meta["blueprint_plan_links"] = list(blueprint.plan_links)
        for _ac in (getattr(blueprint, "agent_clarifications", None) or []):
            if _ac and _ac not in F.notes:
                F.notes.append(_ac)
    meta["email_evidence"] = email_evs
    results = run_comparison(
        census, invoice_records, renewal_to_use, meta, F, have_invoice,
        renewal_required=ren_required, tracer=tracer,
    )

    # 6b. Cross-document DOB discrepancy detection (P0.4) & Invoice Recency check (P1.7 / Section 11.2)
    try:
        from clarifications import dob_discrepancy_items, invoice_recency_items
        dob_items = dob_discrepancy_items(census, roles=roles, docs=docs, invoice_records=invoice_records)
        for ditem in dob_items:
            ev = ditem.evidence or {}
            who_lbl = ev.get("person") or ", ".join(ditem.employees)
            detail_lbl = f"Census {ev.get('census_dob', '')} vs {ev.get('source', 'supporting doc')} {ev.get('other_dob', '')}"
            if ("DOB discrepancies", who_lbl, detail_lbl) not in F.exceptions:
                F.exceptions.append(("DOB discrepancies", who_lbl, detail_lbl))
            if ditem.text not in F.notes:
                F.notes.append(ditem.text)
        # Sync any newly attached dependent/employee DOB notes into results
        for p in census.people:
            rr_p = results.get(id(p))
            if rr_p is not None and p.notes:
                for n_txt, n_col in p.notes:
                    if "DOB Discrepancy" in str(n_txt) and not any(t == n_txt for t, _ in rr_p.notes):
                        rr_p.notes.append((n_txt, True))
        recency_items = invoice_recency_items(selected, prospect=prospect, roles=roles, docs=docs)
        for ritem in recency_items:
            if ritem.text not in F.notes:
                F.notes.append(ritem.text)
            inv_lbl = (ritem.evidence or {}).get("invoice", "Selected Invoice")
            if ("Outdated invoice", inv_lbl, ritem.text) not in F.exceptions:
                F.exceptions.append(("Outdated invoice", inv_lbl, ritem.text))
    except Exception as _post_comp_err:
        log.debug("Post-comparison DOB/recency check skipped: %s", _post_comp_err)

    # Record field-level provenance for every canonical census record (P2.13 / G3)
    if provenance and census:
        try:
            from learning_engine.contracts import FieldStatus, SourceRef
            for idx_p, p in enumerate(census.people):
                if getattr(p, "is_duplicate", False) or getattr(p, "invoice_only", False):
                    continue
                rec_key = p.record_id or f"rec_{idx_p + 1:04d}"
                rd = getattr(p, "raw_data", {}) or {}
                s_ref = SourceRef(
                    document_id=p.source or (rd.get("file") or "census"),
                    unit_id=str(rd.get("table") or "main"),
                    row_index=p.row_no or idx_p + 1,
                )
                for fname_k, fval in (
                    ("first_name", p.first),
                    ("last_name", p.last),
                    ("gender", p.gender),
                    ("dob", f"{p.dob:%Y-%m-%d}" if hasattr(p.dob, "strftime") else str(p.dob or "")),
                    ("zip", p.zip),
                    ("relationship", p.role),
                    ("coverage", p.coverage),
                    ("cobra", p.cobra),
                    ("plan", p.plan),
                ):
                    st_f = FieldStatus.PRESENT_SUPPORTED if fval not in (None, "") else FieldStatus.MISSING_IN_SOURCE
                    provenance.record_field(rec_key, fname_k, fval, fval, status=st_f, source_ref=s_ref)
        except Exception as _prov_err:
            log.debug("Provenance recording skipped: %s", _prov_err)

    # Per the Payroll and Wage Document Exclusion rule (Section 1.2 & Section 29),
    # payroll/wage documents must never drive enrollment deficiencies or cross-document conflict exceptions.
    # Non-employee wage/tax registers are strictly excluded from generating medical deficiency notes.

    # 7. QC audit numbers --------------------------------------------------------------------------
    src_emp = census.raw_employee_rows - census.cross_file_merges
    src_dep = census.raw_dependent_rows - census.dependent_merges
    out_emp = len([p for p in census.people if p.role == "EE" and not p.invoice_only and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")])
    out_dep = len([p for p in census.people if p.role != "EE" and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")])
    pt_excluded = len({id(p) for p in census.people if p.role == "EE" and str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")} | {id(p) for p in getattr(census, "excluded_employees", [])})
    non_med_dep_excluded = len({id(p) for p in census.people if p.role != "EE" and str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")} | {id(p) for p in getattr(census, "excluded_dependents", [])})
    audit = {
        "src_employees": src_emp,
        "src_dependents": src_dep,
        "out_employees": out_emp,
        "out_dependents": out_dep,
        "pt_excluded": pt_excluded,
        "non_med_dep_excluded": non_med_dep_excluded,
        "missing_employees": max(0, src_emp - out_emp - pt_excluded)
    }
    unreconciled_dep = max(0, src_dep - out_dep - non_med_dep_excluded)
    if audit["missing_employees"] or unreconciled_dep:
        F.notes.append("Census totals do not reconcile to the source census files - review required.")
    log.info("      * Audit: Source EEs=%d, Source Deps=%d -> Output EEs=%d, Output Deps=%d (PT Excluded=%d, Non-Med Deps Excluded=%d)",
             src_emp, src_dep, out_emp, out_dep, pt_excluded, non_med_dep_excluded)

    # Claims requirement check
    claims_required, claims_reason = check_claims_required(
        prospect, enrolled_medical_count=F.invoice_medical_count or len([p for p in census.employees if p.coverage in C.ACTIVE_TIERS]), docs=docs
    )
    if claims_required and claims_reason not in F.claims_notes:
        F.claims_notes.append(claims_reason)

    # 7.5 Learning Engine Pre-Release Validation & Closed-Loop Repair ------------------------------
    le_status = "PASSED"
    le_val_report = None
    executed_repair_patches: list[RepairPatch] = []

    if validator:
        import time as _time
        log.info("[7.5] Learning Engine pre-release validation starting...")
        _t_val = _time.time()
        le_val_report = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census, selected_invoices=selected,
            renewal=renewal, ren_required=ren_required, claims_required=claims_required,
            findings=F, results=results, roles=roles,
        )
        le_status = le_val_report.release_status.value
        log.info("[7.5] Learning Engine validation finished in %.1fs (failed_checks=%d, issues=%d)",
                 _time.time() - _t_val, le_val_report.failed_checks, len(le_val_report.issues))

        if le_orchestrator and le_ctx and (le_val_report.failed_checks > 0 or le_val_report.issues):
            log.warning("⚠️ [LEARNING ENGINE] %d validation failure(s) detected. Entering closed-loop self-healing repair...", len(le_val_report.issues))
            working_state = {
                "census": census,
                "population_sanity": sanity_report.to_dict(),
                "invoices": invoice_records,
                "invs": invoice_records,
                "invoice_docs": inv_docs,
                "selected_invoices": selected,
                "results": results,
                "findings": F,
                "meta": meta,
                "prospect": prospect,
                "routing": routing,
                "renewal": renewal,
                "roles": roles,
                "docs": docs,
            }
            repair_engine = le_orchestrator.repair_engine
            repair_cycle = 0

            while repair_cycle < repair_engine.max_cycles and (le_val_report.failed_checks > 0 or le_val_report.issues):
                repair_cycle += 1
                patches = repair_engine.propose_patches(le_val_report, working_state)
                if not patches:
                    break

                pre_missing_census = F.counters.get("missing_census", 0)
                pre_canonical_matched = len([
                    p for p in census.canonical_census_employees
                    if results.get(id(p)) and results[id(p)].inv is not None
                ])
                cycle_txs = []
                for patch in patches:
                    log.info("🔧 [SELF-HEALING] Applying repair patch: %s (%s)", patch.patch_id, patch.action.value)
                    tx = repair_engine.execute_patch(patch, working_state)
                    cycle_txs.append(tx)
                    executed_repair_patches.append(patch)

                # Re-sync modified pipeline objects from repaired working state
                census = working_state.get("census", census)
                selected = working_state.get("selected_invoices", selected)
                invs_recs = working_state.get("invoices") or working_state.get("invs")
                if isinstance(invs_recs, list) and invs_recs and hasattr(invs_recs[0], "first"):
                    invoice_records = invs_recs
                else:
                    invoice_records = combine_records(selected)
                have_invoice = bool(invoice_records)
                renewal_to_use = renewal if (ren_required or (renewal and getattr(renewal, "rates", None))) else None

                # Recalculate QC audit numbers directly from repaired census
                src_emp = census.raw_employee_rows - census.cross_file_merges
                src_dep = census.raw_dependent_rows - census.dependent_merges
                out_emp = len([p for p in census.people if p.role == "EE" and not p.invoice_only and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")])
                out_dep = len([p for p in census.people if p.role != "EE" and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")])
                pt_excluded = len({id(p) for p in census.people if p.role == "EE" and str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")} | {id(p) for p in getattr(census, "excluded_employees", [])})
                non_med_dep_excluded = len({id(p) for p in census.people if p.role != "EE" and str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")} | {id(p) for p in getattr(census, "excluded_dependents", [])})
                audit = {
                    "src_employees": src_emp,
                    "src_dependents": src_dep,
                    "out_employees": out_emp,
                    "out_dependents": out_dep,
                    "pt_excluded": pt_excluded,
                    "non_med_dep_excluded": non_med_dep_excluded,
                    "missing_employees": max(0, src_emp - out_emp - pt_excluded)
                }
                working_state["audit"] = audit

                # Reset comparison exceptions before re-running to prevent accumulation of stale duplicates
                F.exceptions.clear()
                results = run_comparison(
                    census=census,
                    invs=invoice_records,
                    renewal=renewal_to_use,
                    meta=meta,
                    F=F,
                    have_invoice=have_invoice,
                    renewal_required=ren_required,
                    tracer=tracer,
                )
                working_state["results"] = results
                working_state["findings"] = F

                post_missing_census = F.counters.get("missing_census", 0)
                post_canonical_matched = len([
                    p for p in census.canonical_census_employees
                    if results.get(id(p)) and results[id(p)].inv is not None
                ])
                if pre_missing_census > post_missing_census and (pre_missing_census - post_missing_census) > max(0, post_canonical_matched - pre_canonical_matched):
                    log.warning("⚠️ [SELF-HEALING] Repair cycle %d lost %d Invoice-Only subscriber(s) without canonical match gain; rolling back.",
                                repair_cycle, pre_missing_census - post_missing_census)
                    for tx in reversed(cycle_txs):
                        tx.rollback(working_state)
                    census = working_state.get("census", census)
                    results = run_comparison(
                        census=census, invs=invoice_records, renewal=renewal_to_use,
                        meta=meta, F=F, have_invoice=have_invoice, renewal_required=ren_required, tracer=tracer,
                    )
                    working_state["results"] = results
                    working_state["findings"] = F
                    break

                # Refresh claims requirement check after patch
                claims_required, claims_reason = check_claims_required(
                    prospect, enrolled_medical_count=F.invoice_medical_count or len([p for p in census.employees if p.coverage in C.ACTIVE_TIERS]), docs=docs
                )
                if claims_required and claims_reason not in F.claims_notes:
                    F.claims_notes.append(claims_reason)

                re_report = validator.validate_full_pipeline(
                    prospect=prospect, routing=routing, census=census, selected_invoices=selected,
                    renewal=renewal, ren_required=ren_required, claims_required=claims_required,
                    findings=F, results=results, roles=roles,
                )
                re_acc = getattr(re_report, "accuracy", 0.0)
                re_thresh = getattr(re_report, "accuracy_threshold", 0.90)

                if (
                    re_report.failed_checks < le_val_report.failed_checks
                    or len(re_report.issues) < len(le_val_report.issues)
                    or re_acc >= re_thresh
                ):
                    log.info("✅ [SELF-HEALING] Repair cycle %d succeeded! Failures: %d -> %d (Accuracy: %.1f%%, Target: %.1f%%)",
                             repair_cycle, le_val_report.failed_checks, re_report.failed_checks, re_acc * 100, re_thresh * 100)
                    le_val_report = re_report
                    if re_report.failed_checks == 0:
                        le_val_report.release_status = EngineStatus.REPAIRED_AND_PASSED
                        le_status = "REPAIRED_AND_PASSED"
                        break
                    elif re_report.release_status != EngineStatus.BLOCKED_TECHNICAL and re_acc >= re_thresh and re_report.blocked_checks == 0:
                        le_val_report.release_status = EngineStatus.REPAIRED_AND_PASSED
                        le_status = "REPAIRED_AND_PASSED"
                        break
                    else:
                        le_status = re_report.release_status.value
                else:
                    break

            if executed_repair_patches:
                le_orchestrator._persist_verified_learning(
                    executed_repair_patches,
                    le_ctx,
                    validation_report=le_val_report,
                    sanity_passed=sanity_report.passed,
                )

    # 8. write carrier-specific workbook(s) --------------------------------------------------------
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = sanitize_filename(prospect.company or "Prospect", max_length=40)

    out_path: Path | None = None
    sbu_path: Path | None = None

    allow_unsafe_wb = os.getenv("CENSUS_ALLOW_UNSAFE_WORKBOOK", "").strip().lower() in ("1", "true", "yes")
    if not census.employees:
        log.info("[7/8] Census is absent or empty; withholding carrier comparison workbooks per Section 25.")
    elif sanity_report.blocked and not allow_unsafe_wb:
        log.warning(
            "[7/8] Population Sanity Gate BLOCKED workbook generation (%d critical anomaly(ies)); "
            "withholding carrier comparison workbooks to prevent corrupt output.",
            sanity_report.critical_count,
        )
        msg_wb = (
            "Carrier comparison workbook(s) withheld because the Population Sanity Gate detected "
            "unresolved critical count/classification anomalies. Review the source census mapping "
            "and exceptions report before re-running."
        )
        if msg_wb not in F.notes:
            F.notes.append(msg_wb)
    elif routing.carrier == C.CARRIER_FLORIDA_BLUE or routing.is_florida:
        # Florida Blue -> RAPT Census Comparison Workbook only
        log.info("[7/8] Generating Florida Blue RAPT Census Excel workbook...")
        out_path = output_dir / f"RAPT_Census_{name}_{stamp}.xlsx"
        save_rapt_workbook(C.RAPT_TEMPLATE_PATH, out_path, census, prospect, results, F, audit, tracer=tracer)
        log.info("      * Saved Florida Blue Excel: %s", out_path.name)
    elif routing.carrier == C.CARRIER_CIGNA or (routing.state and routing.state.upper() not in ("FL", "UNKNOWN", "CONFLICTING")):
        # Confirmed Non-Florida (Cigna routing) -> Marsh Primary + SBU Deliverables
        log.info("[7/8] Generating Cigna Marsh PEO Practice Census Comparison workbook...")
        out_path = output_dir / f"Marsh_Census_{name}_{stamp}.xlsx"
        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, out_path, census, prospect, results, F, audit, tracer=tracer)
        log.info("      * Saved Marsh Cigna Excel: %s", out_path.name)

        log.info("      * Generating SBU Census Template deliverable from finalized census...")
        sbu_path = output_dir / f"SBU_Census_{name}_{stamp}.xlsx"
        save_sbu_workbook(C.SBU_TEMPLATE_PATH, sbu_path, census, prospect)
        log.info("      * Saved SBU Cigna Excel: %s", sbu_path.name)
    else:
        # Unknown or conflicting business state -> no carrier workbook per Section 4.2!
        log.info("[7/8] Primary business state unknown/conflicting; withholding carrier workbooks per Section 4.2.")

    # Deliverable 2: Missing Information Email ----------------------------------------------------
    log.info("[8/8] Generating ready-to-send Missing Information Email deliverable...")
    if claims_required and claims_reason not in F.claims_notes:
        F.claims_notes.append(claims_reason)

    email_subject, email_sections = build_missing_information_email_parts(
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
    email_content = format_missing_information_email_text(email_subject, email_sections)
    email_path = output_dir / f"Missing_Information_Email_{name}_{stamp}.docx"
    write_missing_information_docx(email_path, email_subject, email_sections)
    F.email_text = email_content
    log.info("      * Saved Missing Information Email: %s", email_path.name)

    # Stage 8b: Post-Generation Workbook Auditor (Deterministic + Scoped LLM QA Review) ----------
    workbook_audit_report = None
    if out_path and out_path.exists():
        try:
            workbook_audit_report = audit_generated_deliverables(
                out_path,
                sbu_path=sbu_path,
                email_text=email_content,
                docs=docs,
                roles=roles,
                blueprint=blueprint,
                prospect=prospect,
                selected_invoices=selected,
                findings=F,
                apply_patches=True,
            )
            # Bounded 1-pass Closed-Loop Self-Repair when the Auditor's run_python check detects
            # concrete population, status, or dependent-linking defects in the generated workbook.
            _rep = getattr(workbook_audit_report, "repair_actions", None) or {}
            _rep_excl = _rep.get("exclude_employees") or []
            _rep_ov = _rep.get("status_overrides") or []
            _rep_dl = _rep.get("dependent_links") or []
            _rep_ff = _rep.get("field_fixes") or []
            if _rep_excl or _rep_ov or _rep_dl or _rep_ff:
                from utils import name_similarity as _rep_sim, split_full_name as _rep_split
                from census_builder import apply_dependent_links as _rep_apply_dl, recalculate_row_numbers as _rep_recalc
                _inv_pairs = [(r.first, r.last) for r in (invoice_records or []) if r.first or r.last]
                _changed = 0

                if _rep_excl:
                    _rm_ids: set[int] = set()
                    for ex in _rep_excl:
                        ef, el = _rep_split(ex["person_name"])
                        for e in list(census.employees):
                            if getattr(e, "invoice_only", False) or str(getattr(e, "disposition", "") or "").startswith("EXCLUDE_"):
                                continue
                            if _rep_sim((e.first, e.last), (ef, el)) >= 0.88:
                                if any(_rep_sim((e.first, e.last), ip) >= 0.88 for ip in _inv_pairs):
                                    continue
                                e.disposition = "EXCLUDE_TERMINATED"
                                e.eligibility_status = "EXCLUDED_TERMINATED"
                                if e not in census.excluded_employees:
                                    census.excluded_employees.append(e)
                                _rm_ids.add(id(e))
                                for dep in list(getattr(e, "dependents", []) or []):
                                    dep.disposition = "EXCLUDE_TERMINATED"
                                    if dep not in census.excluded_dependents:
                                        census.excluded_dependents.append(dep)
                                    _rm_ids.add(id(dep))
                                _changed += 1
                                break
                    if _rm_ids:
                        census.people = [p for p in census.people if id(p) not in _rm_ids]
                        _rep_recalc(census)

                if _rep_ov:
                    for ov in _rep_ov:
                        of, ol = _rep_split(ov["person_name"])
                        ocov = str(ov.get("coverage") or "").strip().upper()
                        ocob = str(ov.get("cobra") or "").strip().upper()
                        orsn = str(ov.get("reason") or "Post-Generation Auditor status fix")
                        for e in census.employees:
                            if getattr(e, "invoice_only", False) or str(getattr(e, "disposition", "") or "").startswith("EXCLUDE_"):
                                continue
                            if _rep_sim((e.first, e.last), (of, ol)) >= 0.88:
                                if ocov in ("WO", "WP", "NE", "RC") and any(_rep_sim((e.first, e.last), ip) >= 0.88 for ip in _inv_pairs):
                                    continue
                                if ocov in ("EE", "ES", "EC", "FAM", "WO", "WP", "NE", "RC"):
                                    e.coverage = ocov
                                    if ocov in ("WO", "WP", "NE", "RC"):
                                        e.plan = "NA"
                                        e.disposition = "RETAIN_MEDICAL_STATUS"
                                        e.eligibility_status = {"WO": "WAIVED", "WP": "WAITING_PERIOD", "NE": "NOT_ELIGIBLE", "RC": "REFUSED"}[ocov]
                                    else:
                                        e.disposition = "INCLUDE_ACTIVE_MEDICAL"
                                        e.eligibility_status = "ENROLLED"
                                if ocob in ("Y", "N"):
                                    e.cobra = ocob
                                e.note(f"Auditor repair: status set to {e.coverage} ({orsn})", red=False)
                                _changed += 1
                                break

                if _rep_dl:
                    _changed += _rep_apply_dl(census, _rep_dl)

                if _rep_ff:
                    from utils import parse_date as _rep_parse_date, std_zip as _rep_std_zip
                    for ff in _rep_ff:
                        ff_f, ff_l = _rep_split(ff["person_name"])
                        for p in census.people:
                            if getattr(p, "invoice_only", False) or str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_"):
                                continue
                            if _rep_sim((p.first, p.last), (ff_f, ff_l)) >= 0.88:
                                patched_any = False
                                if ff.get("gender") in ("M", "F"):
                                    p.gender = ff["gender"]
                                    p.gender_resolution_status = "RESOLVED"
                                    patched_any = True
                                if ff.get("dob"):
                                    d_parsed = _rep_parse_date(ff["dob"])
                                    if d_parsed:
                                        p.dob = d_parsed
                                        patched_any = True
                                if ff.get("zip"):
                                    zv = _rep_std_zip(ff["zip"])
                                    z_str = zv[0] if isinstance(zv, tuple) else str(zv or "")
                                    if z_str:
                                        p.zip = z_str
                                        patched_any = True
                                if ff.get("cobra") in ("Y", "N"):
                                    p.cobra = ff["cobra"]
                                    patched_any = True
                                if patched_any:
                                    _changed += 1
                                break

                if _changed > 0:
                    log.info(
                        "🔧 [AUDITOR SELF-REPAIR] Applying %d repair action(s) (excl=%d, overrides=%d, dep_links=%d) and regenerating deliverables...",
                        _changed, len(_rep_excl), len(_rep_ov), len(_rep_dl),
                    )
                    # Clear invoice-only ghost rows before re-comparing
                    census.people = [p for p in census.people if not getattr(p, "invoice_only", False)]
                    F.exceptions.clear()
                    results = run_comparison(
                        census=census,
                        invs=invoice_records,
                        renewal=renewal_to_use,
                        meta=meta,
                        F=F,
                        have_invoice=have_invoice,
                        renewal_required=ren_required,
                        tracer=tracer,
                    )
                    out_emp = len([p for p in census.people if p.role == "EE" and not p.invoice_only and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")])
                    out_dep = len([p for p in census.people if p.role != "EE" and not str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")])
                    pt_excluded = len({id(p) for p in census.people if p.role == "EE" and str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")} | {id(p) for p in getattr(census, "excluded_employees", [])})
                    non_med_dep_excluded = len({id(p) for p in census.people if p.role != "EE" and str(getattr(p, "disposition", "") or "").startswith("EXCLUDE_")} | {id(p) for p in getattr(census, "excluded_dependents", [])})
                    audit.update({
                        "out_employees": out_emp,
                        "out_dependents": out_dep,
                        "pt_excluded": pt_excluded,
                        "non_med_dep_excluded": non_med_dep_excluded,
                        "missing_employees": max(0, src_emp - out_emp - pt_excluded),
                    })
                    if routing.carrier == C.CARRIER_FLORIDA_BLUE or routing.is_florida:
                        save_rapt_workbook(C.RAPT_TEMPLATE_PATH, out_path, census, prospect, results, F, audit, tracer=tracer)
                    else:
                        save_marsh_workbook(C.MARSH_TEMPLATE_PATH, out_path, census, prospect, results, F, audit, tracer=tracer)
                        if sbu_path:
                            save_sbu_workbook(C.SBU_TEMPLATE_PATH, sbu_path, census, prospect)

                    # Also regenerate the Missing Information Email so repaired counts and clarifications stay in sync
                    email_subject, email_sections = build_missing_information_email_parts(
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
                    email_content = format_missing_information_email_text(email_subject, email_sections)
                    write_missing_information_docx(email_path, email_subject, email_sections)
                    F.email_text = email_content

                    workbook_audit_report = audit_generated_deliverables(
                        out_path,
                        sbu_path=sbu_path,
                        email_text=email_content,
                        docs=docs,
                        roles=roles,
                        blueprint=blueprint,
                        prospect=prospect,
                        selected_invoices=selected,
                        findings=F,
                        apply_patches=True,
                    )
                    workbook_audit_report.repair_pass_applied = True
                    log.info(
                        "🔧 [AUDITOR SELF-REPAIR] Completed! Final census=%d EEs / %d Deps | Audit Status=%s",
                        out_emp, out_dep, workbook_audit_report.overall_status.upper(),
                    )
        except Exception as _aud_err:
            log.warning("Post-Generation Workbook Auditor skipped: %s", _aud_err)

    # Diagnostic Trace deliverable
    trace_summary = tracer.format_summary()
    trace_path = output_dir / f"Diagnostic_Trace_{name}_{stamp}.txt"
    trace_path.write_text(trace_summary, encoding="utf-8")
    log.info("      * Saved Diagnostic Trace: %s", trace_path.name)

    summary = exception_summary(F, census, audit)
    if workbook_audit_report is not None:
        summary = summary + "\n\n" + workbook_audit_report.format_text()
    if out_path:
        txt_path = output_dir / out_path.name.replace(".xlsx", "_exceptions.txt")
    else:
        txt_path = output_dir / f"Exceptions_Audit_{name}_{stamp}.txt"
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
    if not le_val_report and validator:
        le_val_report = validator.validate_full_pipeline(
            prospect=prospect, routing=routing, census=census, selected_invoices=selected,
            renewal=renewal, ren_required=ren_required, claims_required=claims_required,
            findings=F, results=results, roles=roles,
        )
        le_status = le_val_report.release_status.value

    if le_val_report and le_orchestrator and le_ctx:
        try:
            if not le_ctx.case_id or le_ctx.case_id == "case_default":
                slug = "".join(ch if ch.isalnum() else "_" for ch in (prospect.company or "case")).strip("_").lower()[:40]
                if slug:
                    le_ctx.case_id = f"case_{slug}"
            if sanity_report.passed and le_val_report.blocked_checks == 0:
                le_orchestrator._ensure_validated_repair_patterns(le_ctx, le_val_report)
            case_audit = le_orchestrator._build_private_case_audit(
                ctx=le_ctx,
                docs=docs,
                coverage_ledger=coverage_ledger,
                census=census,
                report=le_val_report,
                executed_patches=executed_repair_patches,
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
        "population_sanity": sanity_report.to_dict(),
        "blueprint": blueprint.to_summary_dict() if blueprint is not None else None,
        "invoice_control_gate": inv_control_gate,
        "workbook_audit": workbook_audit_report.to_dict() if workbook_audit_report is not None else None,
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
        f"- Name mismatches: {c.get('name_mismatch', 0)} | Tier mismatches: {c.get('tier_mismatch', 0)}",
        f"- Coverage/structure mismatches: "
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
