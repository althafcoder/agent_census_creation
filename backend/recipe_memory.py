"""
Experiential Procedural Recipe Memory (Pillar #2 — True Self-Learning for the Python Sandbox).

Stores compact, 100% PII-free underwriting data-engineering recipes learned across real carrier
and HRIS export layouts (never client names, employee names, or PHI). On each submission,
`retrieve_relevant_recipes(docs)` matches the uploaded files' headers, sheet names, and text
signals against stored recipes and injects the top relevant procedures into Claude's `run_python`
prompt so the agent solves known structural patterns on the first try.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("census.recipe_memory")

_DEFAULT_RECIPE_FILE = (
    Path(__file__).resolve().parent.parent
    / "learning_engine_data"
    / "recipes"
    / "procedural_recipes.json"
)

# Built-in, 100% PII-free general HRIS/carrier procedural recipes
BUILTIN_RECIPES: list[dict[str, Any]] = [
    {
        "id": "horizontal_ee_dep_multi_benefit",
        "title": "Horizontal EE-*/DEP-* Multi-Benefit Report",
        "triggers": ["ee-first", "dep-first", "dep-relation", "benefit plan description", "dbl", "pfl", "dppo", "auto enrollment"],
        "guidance": (
            "When a census has horizontal EE-* and DEP-* columns across multiple benefit lines per person: "
            "(1) Filter out ALL non-medical rows (Dental, Vision, DPPO, DHMO, Life, AD&D, STD, LTD, DBL, PFL, "
            "Critical Illness, Accident, Hospital Indemnity, MCO Tax) BEFORE extracting medical plans/tiers/dependents; "
            "(2) Always read EE-Gender and EE-Birth Date for the employee even on rows that also contain DEP-* columns; "
            "(3) Never treat 'Auto Enrollment = Y' or 'Status = A/Y' as COBRA = Y (COBRA requires an explicit COBRA column or 'COBRA' text); "
            "(4) Continuation rows with blank EE-* belong to the employee directly above; "
            "(5) Deduplicate same-name, same-role children under the same subscriber even if one row has a copied parent DOB (keep the child DOB and note the conflict)."
        ),
    },
    {
        "id": "hris_terminated_and_class_filter",
        "title": "HRIS Full-History Export (Terminated & Part-Time Exclusion)",
        "triggers": ["employment status", "termination date", "terminated", "part-time", "class", "medical decline reason", "eenav"],
        "guidance": (
            "When an HRIS export contains historical employees: "
            "(1) Run value_counts() on Employment Status and Class/Worker Category; "
            "(2) Exclude ALL Terminated/Inactive/Separated/Retired employees and their dependents UNLESS COBRA = Y; "
            "(3) Exclude ALL Part-Time / Seasonal / Per-Diem / <30 hrs employees and their dependents; "
            "(4) Map employees with a populated Medical Decline/Waive Reason (and no active medical plan) to coverage='WO'."
        ),
    },
    {
        "id": "multi_file_authoritative_roster_diff",
        "title": "Multi-File Census Reconciliation (Authoritative FT/PT Roster Set Difference)",
        "triggers": ["full time", "part time", "not benefits eligible", "on leave", "active benefits eligible"],
        "guidance": (
            "When 2+ census spreadsheets are uploaded and one explicitly separates Active Full-Time / On-Leave / Part-Time tabs: "
            "(1) Treat the explicit FT/PT-defining file as the authoritative active workforce; "
            "(2) Merge demographics (DOB, Gender, ZIP) from any secondary all-employee export using case-insensitive normalized names (handling ALL-CAPS and middle names); "
            "(3) Compute the exact Python set difference: exclude any employee appearing ONLY in the older secondary file who is absent from all tabs of the authoritative roster AND not billed on the current medical invoice."
        ),
    },
    {
        "id": "separate_dependent_tab_zip_tier",
        "title": "Separate Dependent Tab Without Subscriber Column",
        "triggers": ["dependant census", "dependent census", "relationship to employee", "home zipcode"],
        "guidance": (
            "When dependents are on a separate worksheet without a Subscriber Name or Subscriber ID column: "
            "(1) Link by exact shared surname + 5-digit ZIP (preferring employees whose medical tier admits that dependent role); "
            "(2) For different-surname spouses/children (maiden/blended names), match by exact 5-digit residential ZIP to the unique employee whose medical tier admits that role (SP/DP -> ES/FAM/EF; CH -> EC/FAM/EF); "
            "(3) Never link a dependent to an EE-only employee when a tier-compatible employee shares the exact same ZIP."
        ),
    },
    {
        "id": "template_example_and_ft_only_ghq",
        "title": "Template Example Rows & FT-Only Census vs. Total Payroll GHQ",
        "triggers": ["john doe", "jane doe", "johnny doe", "plan abc", "ghq", "part-time"],
        "guidance": (
            "Always exclude template placeholder/example rows (e.g., John Doe / Jane Doe / Johnny Doe / Plan ABC / Test Employee). "
            "When the GHQ lists Total Payroll = FT + PT (e.g. 64 total = 4 FT + 60 PT) and the census only lists the 4 FT employees, "
            "do NOT treat the omitted PT staff as missing census records — retain the 4 active FT employees and note the GHQ FT/PT split."
        ),
    },
    {
        "id": "multi_division_invoice_and_credits",
        "title": "Multi-Division Carrier Invoice & Retro/Term Credit Reconciliation",
        "triggers": ["division", "term", "retro", "credit", "adjustment", "amount due", "current charges", "sub total", "dental", "vision"],
        "guidance": (
            "When parsing medical invoices: "
            "(1) Separate medical charges from dental/vision/life/ancillary sections and exclude Workers' Comp or payroll registers; "
            "(2) Capture printed current medical charges, retro/TERM credits, and Total Amount Due separately; "
            "(3) Verify in Python that the sum of extracted active medical subscriber premiums reconciles to the printed medical subtotal; "
            "(4) Preserve zero-padded 5-digit US ZIP codes (str(int(z)).zfill(5) for 4-digit NJ/CT/MA ZIPs)."
        ),
    },
    {
        "id": "email_requested_census_additions",
        "title": "Authoritative Submission Email Employee/Dependent Additions & Terminations",
        "triggers": ["add", "added", "spouse", "dob", "email", "please add", "moving off", "not on the census"],
        "guidance": (
            "Always inspect submission email text in `TEXTS` alongside the Excel census. "
            "When a broker or client email explicitly asks to ADD an employee (and/or spouse/dependents) who is not yet listed on the Excel census "
            "(providing name, DOB, coverage tier, or plan), include that employee and their dependents directly in `emit_canonical_census(...)` "
            "with all demographics stated in the email (leaving unstated Gender/ZIP blank so they are flagged for confirmation) and note 'Added per submission email'."
        ),
    },
]


def _recipe_path(custom_path: Optional[Path] = None) -> Path:
    return custom_path or _DEFAULT_RECIPE_FILE


def load_recipes(custom_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Load built-in + persisted procedural recipes (deduplicated by `id`)."""
    by_id: dict[str, dict[str, Any]] = {r["id"]: dict(r) for r in BUILTIN_RECIPES}
    p = _recipe_path(custom_path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("id") and item.get("guidance"):
                        by_id[str(item["id"])] = item
        except Exception as exc:
            log.debug("Could not load custom recipes from %s: %s", p, exc)
    return list(by_id.values())


def record_new_recipe(
    recipe_id: str,
    title: str,
    triggers: list[str],
    guidance: str,
    *,
    custom_path: Optional[Path] = None,
) -> bool:
    """Persist a new PII-free procedural recipe to disk so future runs benefit automatically."""
    rid = re.sub(r"[^a-z0-9_]+", "_", str(recipe_id or "").strip().lower()).strip("_")
    if not rid or not guidance or not guidance.strip():
        return False
    # Basic PII guard: refuse recipes containing SSNs or emails
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b|\b[\w.+-]+@[\w-]+\.[\w.-]+\b", guidance):
        log.warning("Refused to persist recipe '%s' due to potential PII pattern.", rid)
        return False
    p = _recipe_path(custom_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        by_id = {str(r.get("id")): r for r in existing if isinstance(r, dict) and r.get("id")}
        by_id[rid] = {
            "id": rid,
            "title": str(title or rid).strip(),
            "triggers": [str(t).strip().lower() for t in (triggers or []) if str(t).strip()],
            "guidance": str(guidance).strip(),
        }
        p.write_text(json.dumps(list(by_id.values()), indent=2), encoding="utf-8")
        log.info("🧠 [RECIPE MEMORY] Persisted procedural recipe '%s'", rid)
        return True
    except Exception as exc:
        log.debug("Failed persisting recipe '%s': %s", rid, exc)
        return False


def retrieve_relevant_recipes(
    docs: list[Any],
    *,
    max_recipes: int = 5,
    custom_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Score and return the top relevant procedural recipes for the uploaded submission."""
    recipes = load_recipes(custom_path=custom_path)
    if not docs:
        return recipes[:max_recipes]

    # Build a compact lowercase corpus of filenames, sheet names, header rows, and text snippets
    corpus_parts: list[str] = []
    for d in docs:
        corpus_parts.append(str(getattr(d, "name", "") or "").lower())
        txt = str(getattr(d, "text", "") or "")
        if txt:
            corpus_parts.append(txt[:4000].lower())
        for tbl in (getattr(d, "tables", None) or []):
            corpus_parts.append(str(getattr(tbl, "name", "") or "").lower())
            df = getattr(tbl, "df", None)
            if df is not None and not df.empty:
                head_rows = df.head(6).values.flatten().tolist()
                corpus_parts.append(" ".join(str(x) for x in head_rows[:120] if x is not None and str(x).lower() != "nan").lower())
    corpus = " ".join(corpus_parts)

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, rec in enumerate(recipes):
        trigs = rec.get("triggers") or []
        score = sum(1 for t in trigs if t and str(t).lower() in corpus)
        scored.append((score, -idx, rec))
    scored.sort(reverse=True)
    selected = [r for sc, _, r in scored if sc > 0][:max_recipes]
    if len(selected) < min(3, max_recipes):
        for _, _, r in scored:
            if r not in selected:
                selected.append(r)
            if len(selected) >= min(3, max_recipes):
                break
    return selected


def format_recipes_for_prompt(recipes: list[dict[str, Any]]) -> str:
    """Format retrieved procedural recipes into a compact prompt section."""
    if not recipes:
        return ""
    lines = ["LEARNED PROCEDURAL RECIPES (apply whenever matching structure appears):"]
    for i, r in enumerate(recipes, 1):
        lines.append(f"{i}. [{r.get('title', r.get('id', ''))}] {r.get('guidance', '')}")
    return "\n".join(lines)
