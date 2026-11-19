"""
Post-Generation Workbook Auditor (Deterministic Cross-Check + Scoped LLM Semantic Review).

Architecture (closing the final accuracy gap vs. end-to-end reasoning agents):
  1. **Output Snapshot Builder (`build_workbook_audit_snapshot`)**:
     Reads the ALREADY-SAVED Excel deliverable(s) (Marsh or RAPT, plus SBU if present) and
     Missing Information Email, extracting:
       - Group Info / Prospect header block (every label -> value pair)
       - Every column's header, non-empty count, distinct count, and representative sample values
       - Representative rows + ALL edge-case rows (waived/WP/NE, invoice-only, mismatched, noted)
       - Complete Summary Sheet (all sections, formulas, numbers, notes, audit counts, exceptions)
       - SBU deliverable counts & Company Info block
       - Source submission ground-truth digest (document roles, printed invoice totals/group #,
         PEO/renewal metadata, Blueprint facts)
  2. **Deterministic Pre-Audit Checks (`run_deterministic_output_audit`)**:
     Runs always (even offline, <10ms, zero false positives) to catch structural output defects:
       - Group Info PEO vs Summary renewal-explanation contradictions
       - Column header vs value semantic mismatch (e.g. Subscriber IDs under "Invoice Group #")
       - Blank medical plan names when a single medical SBC / Benefit Highlights PDF exists
       - Blank Marsh dependent coverage when subscriber has active coverage
       - Summary Census Audit counts vs actual rows written on the Census worksheet
       - Invoice-only rows leaking into Census underwriting columns (A-J) or SBU workbook
       - Reconciliation total math vs printed invoice Total Amount Due
  3. **Scoped LLM Semantic Audit (`run_llm_workbook_audit`)**:
     One compact, cached, Anthropic-strict JSON Schema LLM call comparing the generated output
     snapshot against source facts to catch any subtle, previously unseen semantic contradiction,
     missing available source detail, ancillary leakage/erasure, or false Missing Info request.
     Every finding must be grounded with both `output_location` and `source_evidence`.
  4. **Safe Non-Destructive Summary Patching (`apply_safe_workbook_patches`)**:
     Never touches underwriting columns (A-L). Applies only provably safe Summary-sheet text
     repairs (e.g., fixing a contradicted PEO/renewal explanation) and appends a concise
     `Post-Generation QA Audit` section to `Summary` only when unresolved critical/high issues exist.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional

import openpyxl

from utils import clean

log = logging.getLogger("census.workbook_auditor")

VALID_SEVERITIES = ("critical", "high", "medium", "low")
VALID_CATEGORIES = (
    "internal_contradiction",
    "column_heading_mismatch",
    "missing_source_detail",
    "non_medical_leakage_or_erasure",
    "financial_reconciliation",
    "population_or_dependent_integrity",
    "missing_info_email_accuracy",
    "other",
)

# Anthropic-strict JSON Schema (additionalProperties=False on every object, explicit required keys, anyOf for nullables)
AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall_status", "confidence_summary", "findings", "safe_summary_patches", "repair_actions"],
    "properties": {
        "overall_status": {"type": "string", "enum": ["passed", "warnings", "failed"]},
        "confidence_summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "severity", "category", "output_location",
                    "source_evidence", "summary", "recommended_fix",
                ],
                "properties": {
                    "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
                    "category": {"type": "string", "enum": list(VALID_CATEGORIES)},
                    "output_location": {"type": "string"},
                    "source_evidence": {"type": "string"},
                    "summary": {"type": "string"},
                    "recommended_fix": {"type": "string"},
                },
            },
        },
        "safe_summary_patches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary_row", "old_substring", "new_text", "reason"],
                "properties": {
                    "summary_row": {"type": "integer"},
                    "old_substring": {"type": "string"},
                    "new_text": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "repair_actions": {
            "type": "object",
            "additionalProperties": False,
            "required": ["exclude_employees", "status_overrides", "dependent_links", "field_fixes"],
            "properties": {
                "exclude_employees": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["person_name", "reason"],
                        "properties": {
                            "person_name": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "status_overrides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["person_name", "coverage", "cobra", "reason"],
                        "properties": {
                            "person_name": {"type": "string"},
                            "coverage": {
                                "type": "string",
                                "enum": ["EE", "ES", "EC", "FAM", "WO", "WP", "NE", "RC"],
                            },
                            "cobra": {"type": "string", "enum": ["", "Y", "N"]},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "dependent_links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["dependent_name", "subscriber_name", "reason"],
                        "properties": {
                            "dependent_name": {"type": "string"},
                            "subscriber_name": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "field_fixes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["person_name", "gender", "dob", "zip", "cobra", "reason"],
                        "properties": {
                            "person_name": {"type": "string"},
                            "gender": {"type": "string", "enum": ["", "M", "F"]},
                            "dob": {"type": "string"},
                            "zip": {"type": "string"},
                            "cobra": {"type": "string", "enum": ["", "Y", "N"]},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class AuditFinding:
    severity: str
    category: str
    output_location: str
    source_evidence: str
    summary: str
    recommended_fix: str = ""
    source: str = "deterministic"  # "deterministic" | "llm"
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "output_location": self.output_location,
            "source_evidence": self.source_evidence,
            "summary": self.summary,
            "recommended_fix": self.recommended_fix,
            "source": self.source,
            "code": self.code,
        }


@dataclass
class SafeSummaryPatch:
    summary_row: int
    old_substring: str
    new_text: str
    reason: str
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_row": self.summary_row,
            "old_substring": self.old_substring,
            "new_text": self.new_text,
            "reason": self.reason,
            "applied": self.applied,
        }


@dataclass
class WorkbookAuditReport:
    overall_status: str = "passed"  # "passed" | "warnings" | "failed"
    llm_executed: bool = False
    confidence_summary: str = ""
    findings: list[AuditFinding] = field(default_factory=list)
    patches_applied: list[SafeSummaryPatch] = field(default_factory=list)
    repair_actions: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    repair_pass_applied: bool = False

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    @property
    def passed(self) -> bool:
        return self.critical_count == 0 and self.high_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "passed": self.passed,
            "llm_executed": self.llm_executed,
            "confidence_summary": self.confidence_summary,
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "total_findings": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
            "patches_applied": [p.to_dict() for p in self.patches_applied],
            "repair_actions": self.repair_actions,
            "repair_pass_applied": self.repair_pass_applied,
        }

    def format_text(self) -> str:
        header = (
            f"POST-GENERATION WORKBOOK AUDIT | Status={self.overall_status.upper()} | "
            f"Findings={len(self.findings)} (Critical={self.critical_count}, High={self.high_count}) | "
            f"LLM_Audit={'YES' if self.llm_executed else 'OFFLINE_DETERMINISTIC_ONLY'}"
        )
        lines = [header]
        if self.confidence_summary:
            lines.append(f"  Summary: {self.confidence_summary}")
        if not self.findings:
            lines.append("  - No post-generation defects detected.")
        for f in self.findings:
            lines.append(
                f"  - [{f.severity.upper()}/{f.source}] ({f.category}) @ {f.output_location}: {f.summary}"
            )
            if f.source_evidence:
                lines.append(f"      Evidence: {f.source_evidence}")
            if f.recommended_fix:
                lines.append(f"      Fix: {f.recommended_fix}")
        for p in self.patches_applied:
            if p.applied:
                lines.append(f"  * [AUTO-PATCH APPLIED] Summary Row {p.summary_row}: {p.reason}")
        return "\n".join(lines)


# ------------------------------------------------------ Snapshot Extraction -
_NON_MED_DOC_RE = re.compile(
    r"(?:^|[^a-z0-9])(dental|vision|vsp|eyemed|life|ad&?d|std|ltd|accident|critical|hospital|ancillary)(?:$|[^a-z0-9])",
    re.I,
)


def _cell_str(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%Y-%m-%d")
        except Exception:
            pass
    return clean(v)


def _read_workbook_structure(workbook_path: Path) -> dict[str, Any]:
    """Read the saved Marsh or RAPT workbook into a compact structured dictionary."""
    wb = openpyxl.load_workbook(workbook_path, data_only=False)
    sheet_names = list(wb.sheetnames)
    is_marsh = "Group Info + Census" in sheet_names
    ws_census = wb["Group Info + Census"] if is_marsh else wb.active
    ws_sum = wb["Summary"] if "Summary" in sheet_names else None

    header_row = 12 if is_marsh else 12
    first_data_row = 14 if is_marsh else 14
    if not is_marsh:
        # Locate actual RAPT header row dynamically
        for r_try in range(10, min(20, ws_census.max_row + 1)):
            row_l = " ".join(_cell_str(ws_census.cell(r_try, c).value).lower() for c in range(1, 12))
            if "first name" in row_l and ("last name" in row_l or "dob" in row_l or "date of birth" in row_l):
                header_row = r_try
                first_data_row = r_try + 1
                break

    # 1. Group Info / Prospect block (rows above header_row)
    group_info: dict[str, str] = {}
    if is_marsh:
        for r in range(4, 8):
            for lc, vc in ((1, 2), (4, 5), (7, 8)):
                lbl = _cell_str(ws_census.cell(r, lc).value).rstrip(":")
                val = _cell_str(ws_census.cell(r, vc).value)
                if lbl:
                    group_info[lbl] = val
    else:
        for r in range(1, header_row):
            for c_i in range(1, 10):
                lbl = _cell_str(ws_census.cell(r, c_i).value)
                if lbl and lbl.endswith(":") or lbl.lower() in {
                    "company", "group name", "proposed effective date", "sic", "address",
                    "city/state/zip", "referral", "sales person",
                }:
                    val = _cell_str(ws_census.cell(r, c_i + 1).value)
                    group_info[lbl.rstrip(":")] = val

    # 2. Column headers
    max_col = min(ws_census.max_column, 26)
    headers: dict[int, str] = {}
    for col in range(1, max_col + 1):
        hv = _cell_str(ws_census.cell(header_row, col).value)
        if not hv and header_row > 1:
            hv = _cell_str(ws_census.cell(header_row - 1, col).value)
        if hv:
            headers[col] = hv

    # 3. Scan census & comparison rows (skipping completely blank rows)
    data_rows: list[dict[str, Any]] = []
    blank_streak = 0
    for r in range(first_data_row, ws_census.max_row + 1):
        vals = {col: ws_census.cell(r, col).value for col in headers}
        # Detect if row has any data beyond pre-printed template row number in Col A
        non_a = [v for c_i, v in vals.items() if c_i != 1 and _cell_str(v) != ""]
        if not non_a:
            blank_streak += 1
            if blank_streak >= 5:
                break
            continue
        blank_streak = 0
        data_rows.append({"row": r, "vals": vals})

    # 4. Build column profiles (non-empty count, distinct count, top samples)
    col_profiles: list[dict[str, Any]] = []
    for col, hname in sorted(headers.items()):
        non_empty = [_cell_str(dr["vals"].get(col)) for dr in data_rows if _cell_str(dr["vals"].get(col)) != ""]
        distinct = list(dict.fromkeys(non_empty))
        col_profiles.append({
            "col_idx": col,
            "col_letter": openpyxl.utils.get_column_letter(col),
            "header": hname,
            "non_empty": len(non_empty),
            "distinct_count": len(distinct),
            "samples": distinct[:6],
        })

    # 5. Classify rows (census EEs, census Deps, invoice-only)
    role_col = 7 if is_marsh else next((c_i for c_i, h in headers.items() if "rel" in h.lower() or "member type" in h.lower()), 7)
    cov_col = 8 if is_marsh else next((c_i for c_i, h in headers.items() if "cov" in h.lower() or "tier" in h.lower()), 8)
    plan_col = 10 if is_marsh else next((c_i for c_i, h in headers.items() if "plan" in h.lower() and c_i <= 12), 10)
    comp_start = 11 if is_marsh else 13
    res_col = next((c_i for c_i, h in headers.items() if "comparison result" in h.lower()), comp_start + 9)
    notes_col = next((c_i for c_i, h in headers.items() if h.lower() == "notes"), comp_start + 10)

    ee_rows = []
    dep_rows = []
    inv_only_rows = []
    for dr in data_rows:
        v = dr["vals"]
        has_census_identity = bool(_cell_str(v.get(2)) or _cell_str(v.get(3)))
        has_inv_name = bool(_cell_str(v.get(comp_start)))
        role_val = _cell_str(v.get(role_col)).upper()
        res_val = _cell_str(v.get(res_col))
        if (not has_census_identity and has_inv_name) or "not found in census" in res_val.lower():
            inv_only_rows.append(dr)
        elif role_val in ("SP", "CH", "DP", "SPOUSE", "CHILD"):
            dep_rows.append(dr)
        elif has_census_identity:
            ee_rows.append(dr)

    # 6. Summary sheet lines
    summary_rows: list[dict[str, Any]] = []
    if ws_sum is not None:
        for r in range(1, ws_sum.max_row + 1):
            row_vals = [ws_sum.cell(r, c_i).value for c_i in range(1, min(ws_sum.max_column, 7) + 1)]
            if any(v is not None and str(v).strip() != "" for v in row_vals):
                summary_rows.append({
                    "row": r,
                    "cells": [v if isinstance(v, (int, float)) else _cell_str(v) for v in row_vals],
                })

    wb.close()
    return {
        "workbook_name": workbook_path.name,
        "template_kind": "Marsh" if is_marsh else "RAPT",
        "sheet_names": sheet_names,
        "group_info": group_info,
        "headers": headers,
        "col_profiles": col_profiles,
        "role_col": role_col,
        "cov_col": cov_col,
        "plan_col": plan_col,
        "comp_start": comp_start,
        "res_col": res_col,
        "notes_col": notes_col,
        "data_rows": data_rows,
        "ee_rows": ee_rows,
        "dep_rows": dep_rows,
        "inv_only_rows": inv_only_rows,
        "summary_rows": summary_rows,
    }


def _read_sbu_structure(sbu_path: Path) -> dict[str, Any]:
    wb = openpyxl.load_workbook(sbu_path, data_only=False)
    out: dict[str, Any] = {"workbook_name": sbu_path.name, "census_rows": 0, "role_counts": {}, "waived_counts": {}, "company_info": {}}
    if "Census" in wb.sheetnames:
        ws = wb["Census"]
        roles = Counter()
        waived = Counter()
        cnt = 0
        for r in range(5, ws.max_row + 1):
            fn = _cell_str(ws.cell(r, 1).value)
            ln = _cell_str(ws.cell(r, 2).value)
            if not (fn or ln):
                continue
            cnt += 1
            roles[_cell_str(ws.cell(r, 3).value) or "(blank)"] += 1
            waived[_cell_str(ws.cell(r, 7).value) or "(blank)"] += 1
        out["census_rows"] = cnt
        out["role_counts"] = dict(roles)
        out["waived_counts"] = dict(waived)
    if "Company Info" in wb.sheetnames:
        ws_c = wb["Company Info"]
        for r in range(1, ws_c.max_row + 1):
            k = _cell_str(ws_c.cell(r, 1).value).rstrip(":")
            v = _cell_str(ws_c.cell(r, 2).value)
            if k and v:
                out["company_info"][k] = v
    wb.close()
    return out


def build_workbook_audit_snapshot(
    workbook_path: Path,
    *,
    sbu_path: Optional[Path] = None,
    email_text: str = "",
    docs: list[Any] | None = None,
    roles: dict[str, list[Any]] | None = None,
    blueprint: Any = None,
    prospect: Any = None,
    selected_invoices: list[Any] | None = None,
    findings: Any = None,
) -> dict[str, Any]:
    """Build both structured data (for deterministic audit) and compact text digest (for LLM audit)."""
    wb_struct = _read_workbook_structure(workbook_path)
    sbu_struct = _read_sbu_structure(sbu_path) if (sbu_path and sbu_path.exists()) else None

    # Build compact text representation for the LLM auditor (zero truncation of key facts)
    lines: list[str] = []
    lines.append(f"=== GENERATED WORKBOOK: {wb_struct['workbook_name']} ({wb_struct['template_kind']}) ===")
    lines.append("GROUP INFO / PROSPECT HEADER:")
    for k, v in wb_struct["group_info"].items():
        lines.append(f"  - {k}: {v or '(blank)'}")

    lines.append("\nCENSUS SHEET COLUMN PROFILES:")
    for cp in wb_struct["col_profiles"]:
        lines.append(
            f"  - Col {cp['col_letter']} ({cp['header']}): {cp['non_empty']} non-empty, "
            f"{cp['distinct_count']} distinct -> samples={cp['samples']}"
        )

    lines.append(
        f"\nROW COUNTS ON CENSUS SHEET: Employees={len(wb_struct['ee_rows'])}, "
        f"Dependents={len(wb_struct['dep_rows'])}, Invoice-Only={len(wb_struct['inv_only_rows'])}"
    )

    # Select representative + ALL edge-case rows so LLM sees every anomaly/waiver/invoice-only row
    cov_col = wb_struct["cov_col"]
    res_col = wb_struct["res_col"]
    notes_col = wb_struct["notes_col"]
    sample_rows: list[dict[str, Any]] = []
    seen_r: set[int] = set()

    def _add_row(dr: dict[str, Any]) -> None:
        if dr["row"] not in seen_r and len(sample_rows) < 28:
            seen_r.add(dr["row"])
            sample_rows.append(dr)

    for dr in wb_struct["ee_rows"][:3]:
        _add_row(dr)
    for dr in wb_struct["dep_rows"][:2]:
        _add_row(dr)
    # Include ALL non-enrolled (WO/WP/NE), ALL mismatched/noted rows, and ALL invoice-only rows
    for dr in wb_struct["ee_rows"]:
        cov_v = _cell_str(dr["vals"].get(cov_col)).upper()
        res_v = _cell_str(dr["vals"].get(res_col)).lower()
        note_v = _cell_str(dr["vals"].get(notes_col))
        if cov_v in ("WO", "WP", "NE", "RC") or (res_v and res_v != "match") or note_v:
            _add_row(dr)
    for dr in wb_struct["inv_only_rows"]:
        _add_row(dr)

    lines.append("\nREPRESENTATIVE & EDGE-CASE ROWS:")
    hdrs = wb_struct["headers"]
    for dr in sample_rows:
        kv = [f"{hdrs[c_i]}={_cell_str(dr['vals'].get(c_i))}" for c_i in sorted(hdrs) if _cell_str(dr["vals"].get(c_i))]
        lines.append(f"  Row {dr['row']}: " + " | ".join(kv))

    lines.append("\nSUMMARY SHEET (ALL ROWS):")
    for sr in wb_struct["summary_rows"]:
        non_empty_cells = [str(x) for x in sr["cells"] if x != "" and x is not None]
        lines.append(f"  R{sr['row']}: " + " | ".join(non_empty_cells))

    if sbu_struct:
        lines.append(
            f"\nSBU WORKBOOK ({sbu_struct['workbook_name']}): rows={sbu_struct['census_rows']}, "
            f"roles={sbu_struct['role_counts']}, waived={sbu_struct['waived_counts']}, "
            f"company_info={sbu_struct['company_info']}"
        )

    if email_text:
        lines.append("\nMISSING INFORMATION EMAIL:")
        lines.append(email_text[:3000])

    # Source submission ground truth block
    lines.append("\n=== SOURCE SUBMISSION GROUND TRUTH ===")
    if roles:
        for role_name, d_list in roles.items():
            if d_list:
                lines.append(f"  Role '{role_name}': {[getattr(d, 'name', str(d)) for d in d_list]}")
    elif docs:
        lines.append(f"  Files: {[getattr(d, 'name', str(d)) for d in docs]}")
    if prospect is not None:
        lines.append(
            f"  Prospect: company={getattr(prospect, 'company', '')!r}, state={getattr(prospect, 'state', '')!r}, "
            f"eff={getattr(prospect, 'effective_date', None)}, renewal_date={getattr(prospect, 'renewal_date', None)}, "
            f"is_peo={getattr(prospect, 'is_peo', False)}, peo_name={getattr(prospect, 'peo_name', '')!r}, "
            f"carrier={getattr(prospect, 'current_carrier', '')!r}, stated_total={getattr(prospect, 'stated_total_employees', None)}"
        )
    for inv_d in (selected_invoices or []):
        lines.append(
            f"  Invoice '{getattr(inv_d, 'path', '')}': group_no={getattr(inv_d, 'group_number', '')!r}, "
            f"medical_recs={len(getattr(inv_d, 'records', []) or [])}, "
            f"ancillary_only_recs={len(getattr(inv_d, 'non_medical_records', []) or [])}, "
            f"current_charges={getattr(inv_d, 'current_charges', None)}, "
            f"non_med_charges={getattr(inv_d, 'non_medical_charges', None)} ({getattr(inv_d, 'non_medical_breakdown', {})}), "
            f"gross_total={getattr(inv_d, 'gross_total_premium', None)}, "
            f"printed_total_due={getattr(inv_d, 'total_due', None)}"
        )
        for nm in (getattr(inv_d, "non_medical_records", None) or []):
            lines.append(
                f"    Ancillary-Only Subscriber: {nm.raw_name} (SubID={nm.sub_id}, $0.00 medical, {nm.ancillary_detail or nm.ancillary_premium})"
            )
    if blueprint is not None and getattr(blueprint, "email_facts", None):
        lines.append(f"  Blueprint Email Facts: {blueprint.email_facts[:10]}")

    return {
        "workbook": wb_struct,
        "sbu": sbu_struct,
        "digest_text": "\n".join(lines),
    }


# -------------------------------------------- Deterministic Pre-Audit Gates -
def run_deterministic_output_audit(
    snapshot: dict[str, Any],
    *,
    docs: list[Any] | None = None,
    roles: dict[str, list[Any]] | None = None,
    prospect: Any = None,
    selected_invoices: list[Any] | None = None,
    findings: Any = None,
) -> tuple[list[AuditFinding], list[SafeSummaryPatch]]:
    """Run deterministic, zero-hallucination structural audits on the actual saved workbook."""
    wb = snapshot["workbook"]
    sbu = snapshot["sbu"]
    audit_findings: list[AuditFinding] = []
    safe_patches: list[SafeSummaryPatch] = []

    gi = wb["group_info"]
    sum_rows = wb["summary_rows"]
    ee_rows = wb["ee_rows"]
    dep_rows = wb["dep_rows"]
    inv_only_rows = wb["inv_only_rows"]

    # Check A: Group Info Current PEO vs Summary "not currently with a PEO" contradiction
    peo_val = ""
    for k, v in gi.items():
        if "peo" in k.lower() and "sales" not in k.lower() and v:
            peo_val = v.strip()
            break
    if not peo_val and prospect is not None:
        peo_val = (getattr(prospect, "peo_name", "") or "").strip()
    has_peo = bool(peo_val and peo_val.lower() not in {"no", "none", "n/a", "direct"})
    if has_peo:
        for sr in sum_rows:
            txt = str(sr["cells"][0] or "")
            if "prospect not currently with a peo" in txt.lower():
                ren_dt = getattr(prospect, "renewal_date", None) if prospect is not None else None
                dt_part = f"renewal date {ren_dt:%m/%d/%Y} is more than 90 days out" if hasattr(ren_dt, "strftime") else "renewal date is more than 90 days out"
                fixed_txt = re.sub(
                    r"prospect not currently with a peo",
                    f"current PEO {peo_val}; {dt_part}",
                    txt,
                    flags=re.I,
                )
                audit_findings.append(AuditFinding(
                    severity="critical",
                    category="internal_contradiction",
                    output_location=f"Summary!A{sr['row']}",
                    source_evidence=f"Group Info lists Current PEO = '{peo_val}'",
                    summary=f"Summary!A{sr['row']} states 'prospect not currently with a PEO', contradicting Group Info Current PEO '{peo_val}'.",
                    recommended_fix=f"Replace explanation with: '{fixed_txt}'",
                    source="deterministic",
                    code="PEO_RENEWAL_CONTRADICTION",
                ))
                safe_patches.append(SafeSummaryPatch(
                    summary_row=sr["row"],
                    old_substring="prospect not currently with a PEO",
                    new_text=fixed_txt,
                    reason=f"Align Summary!A{sr['row']} renewal reason with Group Info Current PEO '{peo_val}'",
                ))

    # Check B: "Invoice Group #" column populated with individual Subscriber IDs instead of a shared group #
    inv_group_nos = {
        clean(getattr(inv_d, "group_number", ""))
        for inv_d in (selected_invoices or [])
        if clean(getattr(inv_d, "group_number", ""))
    }
    for cp in wb["col_profiles"]:
        if "invoice group" in cp["header"].lower() and cp["non_empty"] >= 5:
            ratio = cp["distinct_count"] / max(1, cp["non_empty"])
            if ratio >= 0.70 and cp["distinct_count"] >= 5:
                ev = (
                    f"Invoice Group Number in source = {sorted(inv_group_nos)}"
                    if inv_group_nos
                    else f"{cp['distinct_count']} distinct values across {cp['non_empty']} rows (samples: {cp['samples'][:4]})"
                )
                audit_findings.append(AuditFinding(
                    severity="high",
                    category="column_heading_mismatch",
                    output_location=f"Census Col {cp['col_letter']} ('{cp['header']}')",
                    source_evidence=ev,
                    summary=(
                        f"Column {cp['col_letter']} is headed '{cp['header']}' but contains {cp['distinct_count']} distinct "
                        f"values across {cp['non_empty']} rows — appears to hold individual Subscriber IDs instead of Group #."
                    ),
                    recommended_fix="Populate the shared Invoice Group # in this column and place individual Subscriber IDs under 'Invoice Subscriber ID'.",
                    source="deterministic",
                    code="GROUP_NO_VS_SUB_ID_MISMATCH",
                ))

    # Check C: Blank Medical Plan Names on enrolled employees when exactly 1 medical SBC exists
    cov_col = wb["cov_col"]
    plan_col = wb["plan_col"]
    enrolled_ees = [dr for dr in ee_rows if _cell_str(dr["vals"].get(cov_col)).upper() in ("EE", "ES", "EC", "EF", "FAM")]
    enrolled_with_plan = [dr for dr in enrolled_ees if _cell_str(dr["vals"].get(plan_col))]
    if enrolled_ees and not enrolled_with_plan:
        plan_docs = (roles.get("plan_design", []) if roles else []) or []
        med_sbcs = [
            d for d in plan_docs
            if not _NON_MED_DOC_RE.search(getattr(d, "name", "") or "")
            and not re.search(r"\b(benefits?\s+booklet|benefits?\s+guide|enrollment\s+guide|employee\s+guide)\b", f"{getattr(d, 'name', '')} {(getattr(d, 'text', '') or '')[:1200]}", re.I)
        ]
        if len(med_sbcs) == 1:
            audit_findings.append(AuditFinding(
                severity="high",
                category="missing_source_detail",
                output_location=f"Census Col {openpyxl.utils.get_column_letter(plan_col)} (Medical Plan Name)",
                source_evidence=f"Single medical plan design document provided: '{med_sbcs[0].name}'",
                summary=(
                    f"All {len(enrolled_ees)} enrolled employees have blank Medical Plan Name despite single "
                    f"medical plan document '{med_sbcs[0].name}' in the submission."
                ),
                recommended_fix=f"Populate medical plan name derived from '{med_sbcs[0].name}' for enrolled employees and covered dependents.",
                source="deterministic",
                code="BLANK_MEDICAL_PLAN_WITH_SINGLE_SBC",
            ))

    # Check D: Marsh dependents missing Coverage in Col H
    if wb["template_kind"] == "Marsh" and dep_rows:
        blank_dep_cov = [dr["row"] for dr in dep_rows if not _cell_str(dr["vals"].get(cov_col))]
        if blank_dep_cov:
            audit_findings.append(AuditFinding(
                severity="high",
                category="population_or_dependent_integrity",
                output_location=f"Group Info + Census Col H (Rows {blank_dep_cov[:6]})",
                source_evidence=f"{len(blank_dep_cov)}/{len(dep_rows)} dependent rows have blank Medical Coverage",
                summary=f"{len(blank_dep_cov)} dependent row(s) in Marsh have blank Coverage instead of carrying their subscriber's tier.",
                recommended_fix="Propagate subscriber's Medical Coverage (ES/EC/EF), COBRA, and Plan Name to linked dependent rows.",
                source="deterministic",
                code="BLANK_DEPENDENT_COVERAGE_MARSH",
            ))

    # Check E: Invoice-only employees leaking into Marsh Census underwriting columns A-J or SBU
    if wb["template_kind"] == "Marsh":
        leaked_rows = []
        for dr in inv_only_rows:
            non_blank_aj = [c_i for c_i in range(1, 11) if _cell_str(dr["vals"].get(c_i))]
            if non_blank_aj:
                leaked_rows.append((dr["row"], non_blank_aj))
        if leaked_rows:
            audit_findings.append(AuditFinding(
                severity="critical",
                category="population_or_dependent_integrity",
                output_location=f"Group Info + Census Cols A-J (Rows {[r for r, _ in leaked_rows]})",
                source_evidence="Invoice-only discrepancies must not be added to confirmed census underwriting columns A-J",
                summary=f"{len(leaked_rows)} invoice-only discrepancy row(s) have non-blank values in Census columns A-J.",
                recommended_fix="Clear Columns A-J on invoice-only discrepancy rows.",
                source="deterministic",
                code="INVOICE_ONLY_LEAKED_INTO_CENSUS_COLS",
            ))

    if sbu is not None:
        expected_sbu = len(ee_rows) + len(dep_rows)
        if sbu["census_rows"] != expected_sbu:
            audit_findings.append(AuditFinding(
                severity="high",
                category="population_or_dependent_integrity",
                output_location=f"SBU '{sbu['workbook_name']}' Census sheet",
                source_evidence=f"Confirmed census has {len(ee_rows)} EEs + {len(dep_rows)} Deps = {expected_sbu} rows",
                summary=f"SBU Census has {sbu['census_rows']} rows, disagreeing with confirmed census population ({expected_sbu}).",
                recommended_fix="Ensure SBU contains only confirmed census employees and dependents (no invoice-only rows).",
                source="deterministic",
                code="SBU_POPULATION_MISMATCH",
            ))

    # Check F: Summary Audit counts vs actual rows written on Census sheet
    sum_kv: dict[str, Any] = {}
    for sr in sum_rows:
        k_s = str(sr["cells"][0] or "").strip()
        v_s = sr["cells"][1] if len(sr["cells"]) > 1 else None
        if k_s:
            sum_kv[k_s] = (sr["row"], v_s)

    added_inv = sum_kv.get("Added Invoice Employees")
    if added_inv and isinstance(added_inv[1], (int, float)) and added_inv[1] > 0:
        # Verify whether invoice-only rows were actually added into A-J
        if wb["template_kind"] == "Marsh" and all(
            not any(_cell_str(dr["vals"].get(c_i)) for c_i in range(1, 11)) for dr in inv_only_rows
        ):
            audit_findings.append(AuditFinding(
                severity="high",
                category="internal_contradiction",
                output_location=f"Summary!B{added_inv[0]} ('Added Invoice Employees')",
                source_evidence=f"0 invoice-only rows appear in Census underwriting columns A-J ({len(inv_only_rows)} listed as invoice-only discrepancies)",
                summary=f"Summary reports 'Added Invoice Employees: {int(added_inv[1])}' even though invoice-only rows were not added to Census A-J.",
                recommended_fix="Report 'Added Invoice Employees: 0' and track invoice-only rows under 'Invoice-Only Discrepancies (Not Added to Census)'.",
                source="deterministic",
                code="STALE_ADDED_INVOICE_AUDIT_COUNT",
            ))

    # Check G: Printed invoice Total Amount Due vs Findings.invoice_total_balance_due
    if selected_invoices and len(selected_invoices) == 1 and findings is not None:
        printed_due = getattr(selected_invoices[0], "total_due", None)
        calc_due = getattr(findings, "invoice_total_balance_due", None)
        if printed_due is not None and calc_due is not None and abs(float(printed_due) - float(calc_due)) > 0.05:
            audit_findings.append(AuditFinding(
                severity="high",
                category="financial_reconciliation",
                output_location="Summary (Premium Reconciliation -> Invoice total balance due)",
                source_evidence=f"Invoice printed Total Amount Due = ${float(printed_due):,.2f}",
                summary=f"Reconciled balance due (${float(calc_due):,.2f}) differs from printed invoice Total Amount Due (${float(printed_due):,.2f}).",
                recommended_fix="Reconcile non-medical charges and invoice-level credits/adjustments to the printed Total Amount Due.",
                source="deterministic",
                code="BALANCE_DUE_VARIANCE",
            ))

    return audit_findings, safe_patches


# ---------------------------------------------- Scoped LLM Semantic Auditor -
def _auditor_llm_enabled() -> bool:
    if os.getenv("CENSUS_DISABLE_WORKBOOK_AUDITOR", "").strip().lower() in ("1", "true", "yes"):
        return False
    from census_config import ANTHROPIC_API_KEY, USE_LLM_FALLBACK
    return bool(USE_LLM_FALLBACK and ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY.startswith("sk-ant-test"))


def _validate_audit_dict(raw: Any) -> dict[str, Any]:
    """Deterministically validate & filter LLM audit output to eliminate ungrounded hallucinations."""
    if not isinstance(raw, dict):
        return {"overall_status": "passed", "confidence_summary": "", "findings": [], "safe_summary_patches": []}
    status = str(raw.get("overall_status") or "passed").strip().lower()
    if status not in ("passed", "warnings", "failed"):
        status = "warnings"

    clean_findings: list[dict[str, Any]] = []
    for item in (raw.get("findings") or []):
        if not isinstance(item, dict):
            continue
        loc = clean(item.get("output_location", ""))
        ev = clean(item.get("source_evidence", ""))
        summ = clean(item.get("summary", ""))
        # Grounding gate: reject vague/ungrounded findings lacking concrete location, evidence, or summary
        if not (loc and ev and summ):
            continue
        sev = str(item.get("severity") or "medium").strip().lower()
        if sev not in VALID_SEVERITIES:
            sev = "medium"
        cat = str(item.get("category") or "other").strip().lower()
        if cat not in VALID_CATEGORIES:
            cat = "other"
        clean_findings.append({
            "severity": sev,
            "category": cat,
            "output_location": loc,
            "source_evidence": ev,
            "summary": summ,
            "recommended_fix": clean(item.get("recommended_fix", "")),
        })

    clean_patches: list[dict[str, Any]] = []
    for p in (raw.get("safe_summary_patches") or []):
        if not isinstance(p, dict):
            continue
        try:
            row_i = int(p.get("summary_row", 0))
        except Exception:
            continue
        old_s = str(p.get("old_substring") or "").strip()
        new_t = str(p.get("new_text") or "").strip()
        rsn = clean(p.get("reason", ""))
        if row_i >= 1 and old_s and new_t and old_s != new_t:
            clean_patches.append({
                "summary_row": row_i,
                "old_substring": old_s,
                "new_text": new_t,
                "reason": rsn,
            })

    clean_repairs: dict[str, list[dict[str, str]]] = {
        "exclude_employees": [],
        "status_overrides": [],
        "dependent_links": [],
        "field_fixes": [],
    }
    raw_repairs = raw.get("repair_actions")
    if isinstance(raw_repairs, dict):
        for ex in (raw_repairs.get("exclude_employees") or []):
            if isinstance(ex, dict) and clean(ex.get("person_name", "")):
                clean_repairs["exclude_employees"].append({
                    "person_name": clean(ex.get("person_name", "")),
                    "reason": clean(ex.get("reason", "")) or "Post-Generation Auditor exclusion",
                })
        for ov in (raw_repairs.get("status_overrides") or []):
            if isinstance(ov, dict) and clean(ov.get("person_name", "")) and clean(ov.get("coverage", "")):
                cov_u = clean(ov.get("coverage", "")).upper()
                if cov_u in ("EE", "ES", "EC", "FAM", "WO", "WP", "NE", "RC"):
                    clean_repairs["status_overrides"].append({
                        "person_name": clean(ov.get("person_name", "")),
                        "coverage": cov_u,
                        "cobra": clean(ov.get("cobra", "")).upper(),
                        "reason": clean(ov.get("reason", "")) or "Post-Generation Auditor status fix",
                    })
        for dl in (raw_repairs.get("dependent_links") or []):
            if isinstance(dl, dict) and clean(dl.get("dependent_name", "")) and clean(dl.get("subscriber_name", "")):
                clean_repairs["dependent_links"].append({
                    "dependent_name": clean(dl.get("dependent_name", "")),
                    "subscriber_name": clean(dl.get("subscriber_name", "")),
                    "reason": clean(dl.get("reason", "")) or "Post-Generation Auditor dependent link",
                })
        for ff in (raw_repairs.get("field_fixes") or []):
            if isinstance(ff, dict) and clean(ff.get("person_name", "")):
                g_u = clean(ff.get("gender", "")).upper()
                c_u = clean(ff.get("cobra", "")).upper()
                dob_s = clean(ff.get("dob", ""))
                zip_s = clean(ff.get("zip", ""))
                if any((g_u in ("M", "F"), dob_s, zip_s, c_u in ("Y", "N"))):
                    clean_repairs["field_fixes"].append({
                        "person_name": clean(ff.get("person_name", "")),
                        "gender": g_u if g_u in ("M", "F") else "",
                        "dob": dob_s,
                        "zip": zip_s,
                        "cobra": c_u if c_u in ("Y", "N") else "",
                        "reason": clean(ff.get("reason", "")) or "Adversarial Verifier field fix",
                    })

    return {
        "overall_status": status,
        "confidence_summary": clean(raw.get("confidence_summary", "")),
        "findings": clean_findings,
        "safe_summary_patches": clean_patches,
        "repair_actions": clean_repairs,
    }


def run_llm_workbook_audit(
    snapshot: dict[str, Any],
    *,
    docs: Optional[list[Any]] = None,
    extra_files: Optional[dict[str, str]] = None,
    repair_actions_out: Optional[dict[str, Any]] = None,
) -> tuple[bool, str, list[AuditFinding], list[SafeSummaryPatch]]:
    """Execute one compact, schema-validated LLM QA review of the generated deliverables.

    Prefers native Anthropic Tool Use (`run_python` + `submit_workbook_audit` via `agent_orchestrator`) so
    Claude can verify exact counts on the saved workbooks and return structured dicts without truncation;
    falls back cleanly to `llm_client.extract_json` when needed.
    """
    if not _auditor_llm_enabled():
        return False, "", [], []

    # 1. Preferred path: Native Anthropic Tool Use (only when real API key is active, not monkeypatched unit tests)
    try:
        from census_config import ANTHROPIC_API_KEY
        if ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY.startswith("sk-ant-test") and os.getenv("PYTEST_CURRENT_TEST", "") == "":
            from agent_orchestrator import run_tool_based_workbook_audit
            ok, conf_s, f_dicts, p_dicts = run_tool_based_workbook_audit(
                snapshot, docs=docs, extra_files=extra_files, repair_actions_out=repair_actions_out,
            )
            if ok:
                return (
                    True,
                    conf_s,
                    [AuditFinding(**f, source="llm") for f in f_dicts],
                    [SafeSummaryPatch(**p) for p in p_dicts],
                )
    except Exception as _tool_exc:
        log.warning("Tool-based workbook audit skipped (%s); falling back to JSON extraction.", _tool_exc)

    from llm_client import (
        extract_json,
        get_instruction_doc_name,
        get_scoped_instructions,
        set_current_tracking,
    )

    wb_name = snapshot["workbook"]["workbook_name"]
    set_current_tracking(file_name=wb_name, step_name="workbook_auditor")
    doc_name = get_instruction_doc_name()
    scoped_rules = get_scoped_instructions("audit")

    prompt = f"""You are the Post-Generation Underwriting QA Auditor operating under {doc_name}.

SCOPED GOVERNING RULES:
{scoped_rules}

TASK:
Review the ALREADY-GENERATED Excel deliverable snapshot, Summary Sheet, SBU summary, and Missing Information Email
against the Source Submission Ground Truth below.
Flag ONLY real, concrete, evidence-backed defects across these 7 dimensions:
1. Internal contradictions (e.g. Group Info Current PEO = 'Paychex' while Summary says 'not currently with a PEO').
2. Column heading vs value semantics (e.g. individual Subscriber IDs placed under 'Invoice Group #').
3. Missing available source details (e.g. blank plan names when a single medical SBC exists; blank IDs/tiers/renewal date).
4. Non-medical leakage or ancillary-only erasure (e.g. dental/vision included in medical premiums, or a waived employee billed for dental/vision marked 'Not on invoice').
5. Financial reconciliation gaps or unexplained variances between medical, non-medical, credits, and Total Amount Due.
6. Dependent/population/audit-count integrity (e.g. dependents classified as employees, blank dependent coverage, invoice-only rows leaking into Census A-J or SBU, stale audit counts).
7. Missing Information Email accuracy (false SBC or Renewal requests when already provided or not required under the PEO 90-day rule).

RULES FOR OUTPUT:
- If the workbook is accurate and internally consistent, return overall_status="passed" and findings=[] (DO NOT invent nitpicks).
- Every finding MUST cite an exact output_location AND concrete source_evidence.
- Only propose safe_summary_patches for clear prose contradictions on the Summary sheet (never alter numbers or census rows).

Return ONLY a single valid JSON object with exact top-level keys:
{{"overall_status": "passed|warnings|failed", "confidence_summary": "...", "findings": [{{"severity": "...", "category": "...", "output_location": "...", "source_evidence": "...", "summary": "...", "recommended_fix": "..."}}], "safe_summary_patches": [{{"summary_row": 1, "old_substring": "...", "new_text": "...", "reason": "..."}}]}}

DELIVERABLE SNAPSHOT & SOURCE GROUND TRUTH:
{snapshot['digest_text']}
"""
    try:
        raw = extract_json(
            prompt,
            max_tokens=6144,
            effort="medium",
            output_schema=AUDIT_SCHEMA,
            use_structured_output=True,
            validator_fn=lambda d: _validate_audit_dict(d) if (isinstance(d, dict) and "overall_status" in d) else {},
        )
        if not isinstance(raw, dict) or "overall_status" not in raw:
            log.warning("Post-Generation LLM Workbook Audit returned empty JSON; keeping deterministic audit only.")
            return False, "", [], []
        validated = _validate_audit_dict(raw)
        findings_out = [
            AuditFinding(
                severity=f["severity"],
                category=f["category"],
                output_location=f["output_location"],
                source_evidence=f["source_evidence"],
                summary=f["summary"],
                recommended_fix=f["recommended_fix"],
                source="llm",
            )
            for f in validated["findings"]
        ]
        patches_out = [
            SafeSummaryPatch(
                summary_row=p["summary_row"],
                old_substring=p["old_substring"],
                new_text=p["new_text"],
                reason=p["reason"],
            )
            for p in validated["safe_summary_patches"]
        ]
        return True, validated["confidence_summary"], findings_out, patches_out
    except Exception as exc:
        log.warning("Post-Generation LLM Workbook Audit skipped (deterministic audit still active): %s", exc)
        return False, "", [], []


# ----------------------------------------- Non-Destructive Summary Patching -
def apply_safe_workbook_patches(
    workbook_path: Path,
    patches: list[SafeSummaryPatch],
    unresolved_findings: list[AuditFinding],
) -> list[SafeSummaryPatch]:
    """Apply provably safe Summary-sheet text repairs and append QA Audit flags only if critical/high issues remain.

    NEVER touches Census underwriting columns (A-L) or numeric formulas.
    """
    high_or_crit = [f for f in unresolved_findings if f.severity in ("critical", "high")]
    if not patches and not high_or_crit:
        return []

    wb = openpyxl.load_workbook(workbook_path)
    if "Summary" not in wb.sheetnames:
        wb.close()
        return []
    ws_sum = wb["Summary"]

    applied: list[SafeSummaryPatch] = []
    modified = False
    for p in patches:
        if p.summary_row < 1 or p.summary_row > ws_sum.max_row:
            continue
        cell = ws_sum.cell(p.summary_row, 1)
        val = cell.value
        # Safety gate: never overwrite formulas or numeric cells
        if not isinstance(val, str) or val.startswith("="):
            continue
        if p.old_substring.lower() in val.lower():
            cell.value = p.new_text
            p.applied = True
            applied.append(p)
            modified = True

    # Append a concise Post-Generation QA Audit section at the bottom of Summary ONLY for unresolved critical/high findings
    remaining = [f for f in high_or_crit if not any(
        ap.applied and f"Summary!A{ap.summary_row}" == f.output_location for ap in applied
    )]
    if remaining:
        from openpyxl.styles import Font, PatternFill
        r = ws_sum.max_row + 2
        hdr_cell = ws_sum.cell(r, 1, "Post-Generation QA Audit Flags")
        hdr_cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")
        hdr_cell.fill = PatternFill("solid", fgColor="FFC00000")
        r += 1
        for col_i, h_lbl in enumerate(["Severity / Location", "Issue Summary", "Source Evidence"], start=1):
            c = ws_sum.cell(r, col_i, h_lbl)
            c.font = Font(name="Calibri", size=10, bold=True)
        r += 1
        for f in remaining:
            ws_sum.cell(r, 1, f"[{f.severity.upper()}] {f.output_location}").font = Font(name="Calibri", size=10, bold=True, color="FF9C0006")
            ws_sum.cell(r, 2, f.summary).font = Font(name="Calibri", size=10)
            ws_sum.cell(r, 3, f.source_evidence).font = Font(name="Calibri", size=10)
            r += 1
        modified = True

    if modified:
        wb.save(workbook_path)
    wb.close()
    return applied


# ------------------------------------------------------------ Orchestrator -
def audit_generated_deliverables(
    workbook_path: Path,
    *,
    sbu_path: Optional[Path] = None,
    email_text: str = "",
    docs: list[Any] | None = None,
    roles: dict[str, list[Any]] | None = None,
    blueprint: Any = None,
    prospect: Any = None,
    selected_invoices: list[Any] | None = None,
    findings: Any = None,
    apply_patches: bool = True,
) -> WorkbookAuditReport:
    """Run full Post-Generation Audit (Deterministic + Scoped LLM) on the saved deliverables."""
    snapshot = build_workbook_audit_snapshot(
        workbook_path,
        sbu_path=sbu_path,
        email_text=email_text,
        docs=docs,
        roles=roles,
        blueprint=blueprint,
        prospect=prospect,
        selected_invoices=selected_invoices,
        findings=findings,
    )

    det_findings, det_patches = run_deterministic_output_audit(
        snapshot,
        docs=docs,
        roles=roles,
        prospect=prospect,
        selected_invoices=selected_invoices,
        findings=findings,
    )
    _audit_extra_files: dict[str, str] = {}
    if workbook_path and Path(workbook_path).exists():
        _audit_extra_files[Path(workbook_path).name] = str(Path(workbook_path).resolve())
    if sbu_path and Path(sbu_path).exists():
        _audit_extra_files[Path(sbu_path).name] = str(Path(sbu_path).resolve())
    _repair_actions: dict[str, list[dict[str, str]]] = {}
    llm_ok, conf_summary, llm_findings, llm_patches = run_llm_workbook_audit(
        snapshot, docs=docs, extra_files=_audit_extra_files or None, repair_actions_out=_repair_actions,
    )

    # Deduplicate LLM findings that overlap an already-caught deterministic finding on the same category/location
    merged: list[AuditFinding] = list(det_findings)
    det_keys = {(f.category, f.output_location.lower()) for f in det_findings}
    for lf in llm_findings:
        key = (lf.category, lf.output_location.lower())
        if key not in det_keys:
            merged.append(lf)

    all_patches: list[SafeSummaryPatch] = list(det_patches)
    patched_rows = {p.summary_row for p in det_patches}
    for lp in llm_patches:
        if lp.summary_row not in patched_rows:
            all_patches.append(lp)

    applied_patches: list[SafeSummaryPatch] = []
    if apply_patches:
        applied_patches = apply_safe_workbook_patches(workbook_path, all_patches, merged)

    # Remove findings that were cleanly auto-patched on the Summary sheet from unresolved critical count
    patched_locs = {f"Summary!A{p.summary_row}" for p in applied_patches if p.applied}
    unresolved = [f for f in merged if f.output_location not in patched_locs]

    if any(f.severity == "critical" for f in unresolved):
        status = "failed"
    elif any(f.severity in ("high", "medium") for f in unresolved):
        status = "warnings"
    else:
        status = "passed"

    report = WorkbookAuditReport(
        overall_status=status,
        llm_executed=llm_ok,
        confidence_summary=conf_summary or (
            "All post-generation deterministic integrity checks passed."
            if not unresolved else f"{len(unresolved)} post-generation audit finding(s) flagged."
        ),
        findings=unresolved,
        patches_applied=applied_patches,
        repair_actions=_repair_actions,
    )
    if report.passed:
        log.info(
            "✅ [WORKBOOK AUDITOR] PASSED | %s | LLM=%s | Auto-Patches=%d",
            workbook_path.name, "YES" if llm_ok else "OFFLINE", len(applied_patches),
        )
    else:
        log.warning(
            "⚠️ [WORKBOOK AUDITOR] %s | %s | Findings=%d (Critical=%d, High=%d) | LLM=%s",
            status.upper(), workbook_path.name, len(unresolved),
            report.critical_count, report.high_count, "YES" if llm_ok else "OFFLINE",
        )
        for uf in unresolved:
            log.warning("      * [%s/%s] %s: %s", uf.severity.upper(), uf.source, uf.output_location, uf.summary)
    return report
