"""
Whole-Submission LLM Blueprint (Scoped Rules + Strict JSON Schema) + Math Control Gates.

Architectural role:
  1. **LLM understands the submission as a whole** (one compact pass over all files,
     email bodies, indexed spreadsheet headers + sample rows, and PDF snippets) guided
     by a scoped instruction slice (``get_scoped_instructions("blueprint")``) and
     enforced by an Anthropic-strict JSON Schema (``BLUEPRINT_SCHEMA``).
  2. **Python executes deterministically** across 100% of rows using the Blueprint's
     column mappings, value-code tables, document roles, email facts, plan links, and
     printed control totals — zero row truncation, zero brittle column-alias whack-a-mole,
     and zero forgotten business invariants.
  3. **Math & Sanity Gates verify** parsed outputs against printed invoice control totals
     and cross-document population totals before deliverables are released.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import re
from typing import Any, Optional

from utils import (
    clean,
    clean_overprint_text,
    normalize_reversed_text,
    parse_date,
    split_full_name,
    std_coverage,
    std_gender,
    std_relationship,
)

log = logging.getLogger("census.blueprint")

VALID_ROLES = (
    "census", "invoice", "renewal", "ghq", "rfp",
    "payroll", "deliverable", "claims", "plan_design", "unknown",
)

CANONICAL_CENSUS_FIELDS = (
    "first_name", "last_name", "full_name", "preferred_first", "middle_name",
    "gender", "dob", "zip", "relationship", "coverage", "status", "cobra",
    "plan_type", "plan", "carrier", "premium", "ee_cost", "er_cost",
    "hire_date", "effective_date", "txn_type", "sub_id",
    "eligibility_class", "worker_category", "position_status",
    "waive_reason", "job_title", "salary",
    "emp_first", "emp_last", "emp_full",
    "dep_first", "dep_last", "dep_full", "dep_dob", "dep_gender",
    "address", "city", "state",
)

# Strict JSON Schema compliant with Anthropic Structured Outputs
# (every object has additionalProperties=False and explicit required keys; no open maps).
BLUEPRINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "documents",
        "prospect",
        "census_schemas",
        "email_facts",
        "plan_links",
        "invoice_control_totals",
    ],
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file_name", "role", "reason", "sbc_plan_ids"],
                "properties": {
                    "file_name": {"type": "string"},
                    "role": {"type": "string", "enum": list(VALID_ROLES)},
                    "reason": {"type": "string"},
                    "sbc_plan_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "prospect": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "company", "dba", "ein", "sic",
                "address", "city", "state", "zip",
                "effective_date", "sales_person", "referral",
                "current_carrier", "is_peo", "peo_name",
                "contact_name", "contact_email", "contact_phone",
                "stated_employees", "renewal_date",
            ],
            "properties": {
                "company": {"type": "string"},
                "dba": {"type": "string"},
                "ein": {"type": "string"},
                "sic": {"type": "string"},
                "address": {"type": "string"},
                "city": {"type": "string"},
                "state": {"type": "string"},
                "zip": {"type": "string"},
                "effective_date": {"type": "string"},
                "sales_person": {"type": "string"},
                "referral": {"type": "string"},
                "current_carrier": {"type": "string"},
                "is_peo": {"type": "boolean"},
                "peo_name": {"type": "string"},
                "contact_name": {"type": "string"},
                "contact_email": {"type": "string"},
                "contact_phone": {"type": "string"},
                "stated_employees": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "renewal_date": {"type": "string"},
            },
        },
        "census_schemas": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "file_name", "sheet_name", "header_row",
                    "is_census_table", "column_mappings", "value_maps",
                ],
                "properties": {
                    "file_name": {"type": "string"},
                    "sheet_name": {"type": "string"},
                    "header_row": {"type": "integer"},
                    "is_census_table": {"type": "boolean"},
                    "column_mappings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["field", "column_index"],
                            "properties": {
                                "field": {"type": "string", "enum": list(CANONICAL_CENSUS_FIELDS)},
                                "column_index": {"type": "integer"},
                            },
                        },
                    },
                    "value_maps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["field", "mappings"],
                            "properties": {
                                "field": {"type": "string", "enum": ["relationship", "gender", "coverage"]},
                                "mappings": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["raw_code", "meaning"],
                                        "properties": {
                                            "raw_code": {"type": "string"},
                                            "meaning": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
        "email_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["person_name", "event_type", "effective_date", "note", "source_file"],
                "properties": {
                    "person_name": {"type": "string"},
                    "event_type": {"type": "string", "enum": ["terminated", "new_hire", "waived", "cobra", "other"]},
                    "effective_date": {"type": "string"},
                    "note": {"type": "string"},
                    "source_file": {"type": "string"},
                },
            },
        },
        "plan_links": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["invoice_plan", "canonical_plan_id", "sbc_file", "evidence"],
                "properties": {
                    "invoice_plan": {"type": "string"},
                    "canonical_plan_id": {"type": "string"},
                    "sbc_file": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "invoice_control_totals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "file_name", "period_start", "period_end",
                    "current_charges", "retro_adjustments", "total_due", "subscriber_count",
                ],
                "properties": {
                    "file_name": {"type": "string"},
                    "period_start": {"type": "string"},
                    "period_end": {"type": "string"},
                    "current_charges": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "retro_adjustments": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "total_due": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "subscriber_count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                },
            },
        },
    },
}


@dataclass
class BlueprintCensusSchema:
    file_name: str
    sheet_name: str
    header_row: int
    is_census_table: bool
    columns: dict[str, int] = field(default_factory=dict)
    value_maps: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class BlueprintPlanLink:
    invoice_plan: str
    canonical_plan_id: str
    sbc_file: str = ""
    evidence: str = ""


@dataclass
class BlueprintInvoiceTotal:
    file_name: str
    period_start: str = ""
    period_end: str = ""
    current_charges: Optional[float] = None
    retro_adjustments: Optional[float] = None
    total_due: Optional[float] = None
    subscriber_count: Optional[int] = None


@dataclass
class SubmissionBlueprint:
    """Validated whole-submission plan generated by the LLM and executed deterministically by Python."""
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)  # file_name_lower -> {file_name, role, reason, sbc_plan_ids}
    prospect: dict[str, Any] = field(default_factory=dict)
    census_schemas: list[BlueprintCensusSchema] = field(default_factory=list)
    email_facts: list[dict[str, Any]] = field(default_factory=list)
    plan_links: list[BlueprintPlanLink] = field(default_factory=list)
    invoice_control_totals: list[BlueprintInvoiceTotal] = field(default_factory=list)
    agent_clarifications: list[str] = field(default_factory=list)
    excluded_employees: list[dict[str, str]] = field(default_factory=list)  # [{person_name, reason}]
    status_overrides: list[dict[str, str]] = field(default_factory=list)    # [{person_name, coverage, cobra, reason}]
    dependent_links: list[dict[str, str]] = field(default_factory=list)     # [{dependent_name, subscriber_name, reason}]
    python_census_payload: Optional[dict[str, Any]] = None
    python_invoices_payload: Optional[dict[str, Any]] = None

    def role_for(self, file_name: str) -> Optional[str]:
        rec = self.documents.get(clean(file_name).lower())
        if not rec:
            return "unknown" if self.documents else None
        r = str(rec.get("role") or "").strip().lower()
        return r if r in VALID_ROLES else ("unknown" if self.documents else None)

    def sbc_plan_ids_for(self, file_name: str) -> list[str]:
        rec = self.documents.get(clean(file_name).lower())
        return list(rec.get("sbc_plan_ids") or []) if rec else []

    def census_schema_for(self, file_name: str, sheet_name: str) -> Optional[BlueprintCensusSchema]:
        fn = clean(file_name).lower()
        sn = clean(sheet_name).lower()
        for sc in self.census_schemas:
            if clean(sc.file_name).lower() == fn and (
                not sn or not clean(sc.sheet_name) or clean(sc.sheet_name).lower() == sn
            ):
                return sc
        # Single-sheet fallback matching by filename alone ONLY when the schema did not specify a different sheet_name
        by_file = [sc for sc in self.census_schemas if clean(sc.file_name).lower() == fn]
        if len(by_file) == 1 and (not sn or not clean(by_file[0].sheet_name)):
            return by_file[0]
        return None

    def as_person_evidence(self) -> list[Any]:
        """Convert blueprint email_facts into ``PersonEvidence`` records for comparator & router."""
        from email_evidence import PersonEvidence
        out: list[PersonEvidence] = []
        for ef in self.email_facts:
            raw_nm = clean(ef.get("person_name"))
            if not raw_nm:
                continue
            first, last = split_full_name(raw_nm)
            if not (first or last):
                continue
            out.append(PersonEvidence(
                raw_name=raw_nm,
                first=first,
                last=last,
                event_type=str(ef.get("event_type") or "other"),
                effective_date=clean(ef.get("effective_date")),
                sentence=clean(ef.get("note")),
                source=clean(ef.get("source_file")) or "submission email",
            ))
        return out

    def apply_to_prospect(self, prospect: Any) -> None:
        """Merge validated Blueprint prospect facts into ``Prospect`` (filling blanks & authoritative flags)."""
        pr = self.prospect or {}
        if not pr or prospect is None:
            return
        str_fields = (
            "company", "dba", "ein", "sic", "address", "city", "state",
            "sales_person", "referral", "current_carrier", "peo_name",
            "contact_name", "contact_email", "contact_phone",
        )
        _NON_PEO = {"", "none", "n/a", "na", "no", "false", "direct", "not applicable", "-"}
        for fld in str_fields:
            val = clean(pr.get(fld))
            if fld == "peo_name" and val.lower() in _NON_PEO:
                continue
            cur_v = clean(getattr(prospect, fld, ""))
            is_placeholder = bool(cur_v and re.fullmatch(r"[_\-\.\s]+", cur_v))
            if val and (not cur_v or is_placeholder):
                setattr(prospect, fld, val)
        if clean(pr.get("zip")) and not clean(getattr(prospect, "zip", "")):
            z = clean(pr.get("zip"))
            if getattr(prospect, "city_state_zip", ""):
                pass
            elif getattr(prospect, "city", "") and getattr(prospect, "state", ""):
                prospect.city_state_zip = f"{prospect.city}, {prospect.state} {z}"
        if not getattr(prospect, "city_state_zip", "") and getattr(prospect, "city", "") and getattr(prospect, "state", ""):
            z = clean(pr.get("zip"))
            prospect.city_state_zip = f"{prospect.city}, {prospect.state} {z}".strip()
        if not getattr(prospect, "effective_date", None) and clean(pr.get("effective_date")):
            dt = parse_date(pr["effective_date"])
            if dt:
                prospect.effective_date = dt
        if not getattr(prospect, "renewal_date", None) and clean(pr.get("renewal_date")):
            rdt = parse_date(pr["renewal_date"])
            if rdt:
                prospect.renewal_date = rdt
        stated = pr.get("stated_employees")
        if isinstance(stated, int) and stated > 0 and not getattr(prospect, "stated_total_employees", None):
            prospect.stated_total_employees = stated
            prospect.stated_total_source = "Submission Blueprint (GHQ/RFP/Email)"
        if pr.get("is_peo") is True and clean(pr.get("peo_name")) and not getattr(prospect, "peo_name", ""):
            prospect.peo_name = clean(pr["peo_name"])

    def apply_to_invoices(self, inv_docs: list[Any]) -> None:
        """Backfill missing billing periods or printed control totals on parsed ``InvoiceDoc``s."""
        by_name = {clean(t.file_name).lower(): t for t in self.invoice_control_totals}
        for inv in inv_docs or []:
            tot = by_name.get(clean(getattr(inv, "path", "")).lower())
            if not tot:
                continue
            if not getattr(inv, "period_start", None) and tot.period_start:
                inv.period_start = parse_date(tot.period_start)
            if not getattr(inv, "period_end", None) and tot.period_end:
                inv.period_end = parse_date(tot.period_end)
            if getattr(inv, "current_charges", None) is None and tot.current_charges is not None:
                inv.current_charges = float(tot.current_charges)
            if getattr(inv, "total_adjustments", None) is None and tot.retro_adjustments is not None:
                inv.total_adjustments = float(tot.retro_adjustments)
            if getattr(inv, "total_due", None) is None and tot.total_due is not None:
                inv.total_due = float(tot.total_due)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "documents_count": len(self.documents),
            "census_schemas_count": len(self.census_schemas),
            "email_facts_count": len(self.email_facts),
            "plan_links_count": len(self.plan_links),
            "invoice_control_totals_count": len(self.invoice_control_totals),
        }


# ----------------------------------------------------------------- Digest ---
def _tail_control_lines(text: str) -> str:
    """Extract printed summary/total lines from anywhere in an invoice/billing PDF."""
    hits: list[str] = []
    for ln in (text or "").splitlines():
        if re.search(
            r"\b(TOTAL\s+FEES|CURRENT\s+CHARGES|TOTAL\s+BALANCE\s+DUE|TOTAL\s+DUE|TOTAL\s+AMOUNT|AMOUNT\s+DUE|SUBTOTAL|RETRO|ADJUSTMENTS)\b",
            ln,
            re.I,
        ) and re.search(r"\d+\.\d{2}", ln):
            cleaned_ln = re.sub(r"\s+", " ", ln).strip()
            if cleaned_ln and cleaned_ln not in hits:
                hits.append(cleaned_ln)
                if len(hits) >= 12:
                    break
    return "\n".join(hits)


def build_submission_digest(docs: list[Any], max_total_chars: int = 42000) -> str:
    """Build a compact, information-dense digest of ALL uploaded files for one unified LLM call.

    - Spreadsheets: explicit 0-based row and column indices for the first 12 non-empty rows per sheet.
    - PDFs/DOCX/Emails: de-reversed & overprint-cleaned headers/bodies + printed invoice totals.
    """
    blocks: list[str] = []
    for idx, d in enumerate(docs or [], 1):
        name = getattr(d, "name", f"doc_{idx}")
        ext = (getattr(d, "ext", "") or "").lower()
        tables = getattr(d, "tables", None) or []
        raw_text = getattr(d, "text", "") or ""
        parts = [f"=== FILE {idx}: {name} (ext={ext}, tables={len(tables)}, chars={len(raw_text)}) ==="]

        if ext in (".xlsx", ".xlsm", ".xls", ".csv") and tables:
            for tbl in tables[:6]:
                df = getattr(tbl, "df", None)
                if df is None or df.empty:
                    continue
                tname = getattr(tbl, "name", "Sheet")
                parts.append(f"  [Sheet: {tname} | rows={len(df)} cols={df.shape[1]}]")
                emitted = 0
                wide_emitted = 0
                for r_i in range(min(len(df), 65)):
                    row_vals = [clean(v) for v in df.iloc[r_i].tolist()]
                    non_empty_cnt = sum(1 for v in row_vals if v)
                    if not non_empty_cnt:
                        continue
                    indexed = ", ".join(f"[{c_i}]={v[:60]}" for c_i, v in enumerate(row_vals[:25]) if v)
                    parts.append(f"    Row {r_i}: {indexed}")
                    emitted += 1
                    if non_empty_cnt >= 4:
                        wide_emitted += 1
                    # Keep scanning past top company-info/instruction banners until >= 6 wide rows (header + sample rows) are seen
                    if wide_emitted >= 6 and emitted >= 12:
                        break
                    if emitted >= 30:
                        break
        else:
            # Repair rotated/reversed text & overprint tokens before showing to Claude
            compact = re.sub(r"[ \t]+", " ", raw_text[:28000])
            norm = clean_overprint_text(normalize_reversed_text(compact))
            is_email_or_rfp = bool(re.search(r"email|rfp|ghq|questionnaire|subject:|from:", (name + " " + norm[:600]), re.I))
            budget = 3500 if is_email_or_rfp else 1800
            snippet = norm[:budget].strip()
            if snippet:
                parts.append(snippet)
            ctrl = _tail_control_lines(clean_overprint_text(raw_text))
            if ctrl:
                parts.append("  [PRINTED TOTAL/SUMMARY LINES]:\n" + ctrl)

        blocks.append("\n".join(parts))

    digest = "\n\n".join(blocks)
    if len(digest) > max_total_chars:
        digest = digest[:max_total_chars] + "\n...[digest truncated to budget]..."
    return digest


# --------------------------------------------------------------- Validation -
_PLACEHOLDERS = {"", "n/a", "na", "none", "null", "unknown", "tbd", "not applicable", "pending"}


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in _PLACEHOLDERS else s


def validate_blueprint_dict(raw: Any, docs: Optional[list[Any]] = None) -> SubmissionBlueprint:
    """Deterministically validate & sanitize raw Blueprint JSON before applying or caching."""
    if not isinstance(raw, dict):
        return SubmissionBlueprint()

    doc_map: dict[str, Any] = {}
    for d in docs or []:
        doc_map[clean(getattr(d, "name", "")).lower()] = d

    # 1. Documents
    validated_docs: dict[str, dict[str, Any]] = {}
    for item in raw.get("documents") or []:
        if not isinstance(item, dict):
            continue
        fn = _clean_str(item.get("file_name"))
        role = _clean_str(item.get("role")).lower()
        if not fn or role not in VALID_ROLES:
            continue
        if doc_map and fn.lower() not in doc_map:
            continue
        sbc_ids = [_clean_str(x) for x in (item.get("sbc_plan_ids") or []) if _clean_str(x)]
        validated_docs[fn.lower()] = {
            "file_name": fn,
            "role": role,
            "reason": _clean_str(item.get("reason")),
            "sbc_plan_ids": sbc_ids,
        }

    # 2. Prospect
    pr_raw = raw.get("prospect") if isinstance(raw.get("prospect"), dict) else {}
    validated_prospect: dict[str, Any] = {}
    for fld in (
        "company", "dba", "ein", "sic", "address", "city", "state", "zip",
        "effective_date", "sales_person", "referral", "current_carrier",
        "peo_name", "contact_name", "contact_email", "contact_phone", "renewal_date",
    ):
        validated_prospect[fld] = _clean_str(pr_raw.get(fld))
    st_code = validated_prospect.get("state", "").upper()
    if len(st_code) == 2 and st_code.isalpha():
        validated_prospect["state"] = st_code
    validated_prospect["is_peo"] = bool(pr_raw.get("is_peo", False))
    se = pr_raw.get("stated_employees")
    try:
        se_int = int(se) if se is not None and not isinstance(se, bool) else None
    except (TypeError, ValueError):
        se_int = None
    validated_prospect["stated_employees"] = se_int if (se_int is not None and 1 <= se_int <= 500000) else None

    # 3. Census schemas (bounds-checked against actual DataFrames + normalizer-verified value maps)
    allowed_fields = set(CANONICAL_CENSUS_FIELDS)
    normalizers = {
        "relationship": std_relationship,
        "gender": std_gender,
        "coverage": std_coverage,
    }
    validated_schemas: list[BlueprintCensusSchema] = []
    for sc in raw.get("census_schemas") or []:
        if not isinstance(sc, dict):
            continue
        fn = _clean_str(sc.get("file_name"))
        sn = _clean_str(sc.get("sheet_name"))
        is_cen_tbl = bool(sc.get("is_census_table", True))
        if not fn:
            continue
        if not is_cen_tbl:
            validated_schemas.append(BlueprintCensusSchema(
                file_name=fn,
                sheet_name=sn,
                header_row=0,
                is_census_table=False,
                columns={},
                value_maps={},
            ))
            continue
        try:
            hdr_row = int(sc.get("header_row", 0))
        except (TypeError, ValueError):
            hdr_row = 0
        if hdr_row < 0:
            hdr_row = 0

        # Find actual table bounds if doc is available
        max_cols = None
        max_rows = None
        doc_obj = doc_map.get(fn.lower())
        if doc_obj is not None:
            for t in getattr(doc_obj, "tables", []) or []:
                if not sn or clean(t.name).lower() == sn.lower():
                    if t.df is not None and not t.df.empty:
                        max_rows, max_cols = t.df.shape
                        break
        if max_rows is not None and hdr_row >= max_rows:
            continue

        cols_dict: dict[str, int] = {}
        for cm in sc.get("column_mappings") or []:
            if not isinstance(cm, dict):
                continue
            fld = _clean_str(cm.get("field"))
            if fld not in allowed_fields:
                continue
            try:
                ci = int(cm.get("column_index"))
            except (TypeError, ValueError):
                continue
            if ci < 0 or (max_cols is not None and ci >= max_cols):
                continue
            cols_dict[fld] = ci

        vmaps: dict[str, dict[str, str]] = {}
        for vm in sc.get("value_maps") or []:
            if not isinstance(vm, dict):
                continue
            fld = _clean_str(vm.get("field"))
            norm = normalizers.get(fld)
            if not norm:
                continue
            cleaned_map: dict[str, str] = {}
            for pair in vm.get("mappings") or []:
                if not isinstance(pair, dict):
                    continue
                rk = clean(pair.get("raw_code"))
                if re.fullmatch(r"-?\d+\.0+", rk):
                    rk = rk.split(".")[0]
                meaning = _clean_str(pair.get("meaning"))
                if fld == "relationship" and meaning:
                    m_low = meaning.lower()
                    if m_low.startswith(("dependent", "dependant")) or (
                        bool(re.search(r"\b(spouse|sp)\b", m_low) and re.search(r"\b(child|children|ch)\b", m_low))
                    ):
                        meaning = "Dependent"
                if rk and meaning and norm(meaning):
                    cleaned_map[rk.lower()] = meaning
            if cleaned_map:
                vmaps[fld] = cleaned_map

        if cols_dict:
            validated_schemas.append(BlueprintCensusSchema(
                file_name=fn,
                sheet_name=sn,
                header_row=hdr_row,
                is_census_table=True,
                columns=cols_dict,
                value_maps=vmaps,
            ))

    # 4. Email facts
    validated_facts: list[dict[str, Any]] = []
    for ef in raw.get("email_facts") or []:
        if not isinstance(ef, dict):
            continue
        pname = _clean_str(ef.get("person_name"))
        ev_type = _clean_str(ef.get("event_type")).lower()
        if not pname or ev_type not in ("terminated", "new_hire", "waived", "cobra", "other"):
            continue
        validated_facts.append({
            "person_name": pname,
            "event_type": ev_type,
            "effective_date": _clean_str(ef.get("effective_date")),
            "note": _clean_str(ef.get("note")),
            "source_file": _clean_str(ef.get("source_file")),
        })

    # 5. Plan links
    validated_links: list[BlueprintPlanLink] = []
    for pl in raw.get("plan_links") or []:
        if not isinstance(pl, dict):
            continue
        inv_p = _clean_str(pl.get("invoice_plan"))
        canon_p = _clean_str(pl.get("canonical_plan_id"))
        if inv_p and canon_p:
            validated_links.append(BlueprintPlanLink(
                invoice_plan=inv_p,
                canonical_plan_id=canon_p,
                sbc_file=_clean_str(pl.get("sbc_file")),
                evidence=_clean_str(pl.get("evidence")),
            ))

    # 6. Invoice control totals
    validated_totals: list[BlueprintInvoiceTotal] = []
    for it in raw.get("invoice_control_totals") or []:
        if not isinstance(it, dict):
            continue
        fn = _clean_str(it.get("file_name"))
        if not fn:
            continue

        def _opt_float(v) -> Optional[float]:
            if v is None or isinstance(v, bool):
                return None
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None

        def _opt_int(v) -> Optional[int]:
            if v is None or isinstance(v, bool):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        validated_totals.append(BlueprintInvoiceTotal(
            file_name=fn,
            period_start=_clean_str(it.get("period_start")),
            period_end=_clean_str(it.get("period_end")),
            current_charges=_opt_float(it.get("current_charges")),
            retro_adjustments=_opt_float(it.get("retro_adjustments")),
            total_due=_opt_float(it.get("total_due")),
            subscriber_count=_opt_int(it.get("subscriber_count")),
        ))

    return SubmissionBlueprint(
        documents=validated_docs,
        prospect=validated_prospect,
        census_schemas=validated_schemas,
        email_facts=validated_facts,
        plan_links=validated_links,
        invoice_control_totals=validated_totals,
    )


# ------------------------------------------------ Math Control-Total Gate ---
def verify_invoice_control_totals(selected_invoices: list[Any], tolerance: float = 1.00) -> dict[str, Any]:
    """Deterministic Math Gate: verify ``sum(parsed subscriber premiums)`` against printed ``current_charges``.

    Returns ``{"passed": bool, "checks": [...], "issues": [...]}``.
    """
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    for inv in selected_invoices or []:
        recs = getattr(inv, "records", None) or []
        printed = getattr(inv, "current_charges", None)
        parsed_sum = round(sum(float(getattr(r, "premium", 0.0) or 0.0) for r in recs), 2)
        item = {
            "invoice": getattr(inv, "path", ""),
            "parsed_count": len(recs),
            "parsed_sum": parsed_sum,
            "printed_current_charges": printed,
            "variance": round(parsed_sum - float(printed), 2) if printed is not None else None,
            "passed": True,
        }
        if printed is not None and float(printed) > 0:
            diff = abs(parsed_sum - float(printed))
            if diff > tolerance:
                item["passed"] = False
                issues.append(
                    f"Invoice Control-Total Math Gate ({item['invoice']}): parsed subscriber premiums "
                    f"${parsed_sum:,.2f} ({len(recs)} rows) differ from printed Current Charges "
                    f"${float(printed):,.2f} by ${diff:,.2f} — verify for dropped or mis-parsed invoice rows."
                )
        checks.append(item)
    return {
        "passed": not bool(issues),
        "checks": checks,
        "issues": issues,
    }


# --------------------------------------------------------------- LLM Caller -
def _blueprint_enabled() -> bool:
    if os.getenv("CENSUS_DISABLE_BLUEPRINT", "").strip().lower() in ("1", "true", "yes"):
        return False
    from census_config import ANTHROPIC_API_KEY, USE_LLM_FALLBACK
    return bool(USE_LLM_FALLBACK and ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY.startswith("sk-ant-test"))


def generate_submission_blueprint(docs: list[Any]) -> Optional[SubmissionBlueprint]:
    """Run one unified Whole-Submission LLM Blueprint call (scoped instructions + strict JSON schema).

    Returns ``None`` cleanly when LLM is disabled/unavailable so deterministic execution continues seamlessly.
    """
    if not docs or not _blueprint_enabled():
        return None

    from llm_client import (
        extract_json,
        get_instruction_doc_name,
        get_scoped_instructions,
        set_current_tracking,
    )

    set_current_tracking(file_name="whole_submission", step_name="submission_blueprint")
    doc_name = get_instruction_doc_name()
    scoped_rules = get_scoped_instructions("blueprint")
    digest = build_submission_digest(docs)

    prompt = f"""You are performing the Whole-Submission Intake & Schema Blueprint pass under {doc_name}.

SCOPED GOVERNING RULES:
{scoped_rules}

TASK:
Inspect ALL files in this submission together and output a single strict JSON Blueprint that Python will
execute deterministically across 100% of rows:
1. "documents": Classify EVERY uploaded file into one of {list(VALID_ROLES)}. For SBC / plan_design files,
   list any plan IDs or plan codes covered by that SBC in "sbc_plan_ids".
2. "prospect": Extract company legal name, DBA, EIN, SIC/NAICS, HQ address/city/state/zip, target effective
   date, sales person, referring broker, current medical carrier, is_peo (true ONLY if currently with a PEO),
   peo_name, client contact name/email/phone, employer-stated total employee count, and current renewal date.
   Leave truly absent fields as empty string "" (or null for stated_employees). Never invent data.
3. "census_schemas": For every spreadsheet sheet that is an employee/dependent census table, return:
   - exact 0-based "header_row" index (matching the "Row N:" labels below),
   - exact 0-based "column_mappings" mapping canonical fields to column indices (matching "[col]=" labels),
   - "value_maps" for any coded columns (relationship, gender, coverage — e.g. 1->Employee, 0->Dependent).
4. "email_facts": Extract every person-level fact stated in client/broker emails (terminations with effective
   dates, new hires, coverage notes, exclusions), even when phrased casually in natural language.
5. "plan_links": Link any invoice billing plan codes (e.g. 0006-PPO) to canonical medical plan IDs (e.g. P621CHC)
   and their matching SBC filenames when cross-document evidence connects them.
6. "invoice_control_totals": For every medical invoice, extract printed billing period dates, Current Charges,
   Retro/Adjustments, Total Due, and subscriber count if printed.

Return ONLY a single valid JSON object with exact top-level keys:
{{"documents": [...], "prospect": {{...}}, "census_schemas": [...], "email_facts": [...], "plan_links": [...], "invoice_control_totals": [...]}}

SUBMISSION DIGEST ({len(docs)} files):
{digest}
"""
    try:
        raw = extract_json(
            prompt,
            max_tokens=8192,
            effort="high",
            output_schema=BLUEPRINT_SCHEMA,
            use_structured_output=True,
            validator_fn=lambda d: d if (isinstance(d, dict) and any(k in d for k in ("documents", "prospect", "census_schemas"))) else {},
        )
        if not isinstance(raw, dict) or not raw:
            log.warning("Submission Blueprint returned empty JSON; falling back to deterministic pipeline.")
            return None
        bp = validate_blueprint_dict(raw, docs=docs)
        log.info(
            "🧭 [SUBMISSION BLUEPRINT] Generated unified plan: %d docs, %d census schemas, %d email facts, %d plan links, %d invoice control totals",
            len(bp.documents), len(bp.census_schemas), len(bp.email_facts),
            len(bp.plan_links), len(bp.invoice_control_totals),
        )
        return bp
    except Exception as exc:
        log.warning("Submission Blueprint generation skipped (falling back to deterministic pipeline): %s", exc)
        return None
