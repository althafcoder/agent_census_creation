"""
Claude Tool-Using Agent Orchestrator (Read -> Inspect -> Commit -> Execute -> Verify).

Architectural role (Copilot-parity closed-loop reasoning backed by deterministic Python execution):
  1. **Interactive Inspection Tools (`inspect_spreadsheet_rows`, `inspect_pdf_text`)**:
     Unlike a single blind pass over truncated snippets, Claude can interactively drill into ANY
     spreadsheet sheet/row range (exact 0-based row & column indices) or ANY PDF page/section
     (rate tables, footers, Benefits Booklets, carrier SBCs, email threads) until 100% certain.
  2. **Structured Tool Commit (`commit_submission_blueprint`)**:
     Uses native Anthropic Tool Use (`block.input`) rather than fragile text-format JSON schemas,
     guaranteeing parsed, validated Python dicts without empty-`{}` truncation failures.
  3. **Deterministic Python Execution**:
     Python executes the committed Blueprint across 100% of rows (20 or 2,000+ employees), runs
     exact Decimal financial reconciliation, builds Marsh/RAPT/SBU workbooks, and writes the
     Missing Information Email.
  4. **Closed-Loop Read-Back Audit (`run_tool_based_workbook_audit`)**:
     Uses native Anthropic Tool Use (`submit_workbook_audit`) to read back the ACTUAL saved
     deliverables and catch any remaining contradiction or missing source fact before release.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from python_sandbox import PythonSandboxSession
from submission_blueprint import (
    BLUEPRINT_SCHEMA,
    SubmissionBlueprint,
    build_submission_digest,
    validate_blueprint_dict,
)
from utils import clean

log = logging.getLogger("census.agent_orchestrator")


# ------------------------------------------------------------ Tool Schemas --
_RUN_PYTHON_TOOL: dict[str, Any] = {
    "name": "run_python",
    "description": (
        "Execute read-only Python data-analysis code (pandas, openpyxl, re, json, Decimal) over 100% of "
        "the uploaded submission files without truncation. Pre-loaded globals:\n"
        "  - FILES: dict[filename -> absolute_path]\n"
        "  - TABLES: dict[filename -> dict[sheet_name -> pd.DataFrame]]\n"
        "  - TEXTS: dict[filename -> extracted_text]\n"
        "  - Helpers: list_files(), sheet_df(file_name, sheet_name=None, header_row=None), "
        "column_summary(file_name, sheet_name=None, header_row=0), search_text(pattern, file_name=None).\n"
        "Use this FIRST to inspect EVERY unique value of Employment Status, Class, Worker Category, "
        "Coverage/Tier, Plan End Date, Decline/Waive Reason, compute exact multi-file set differences, "
        "and cross-check headcounts against RFP/GHQ and invoice totals."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["code"],
        "properties": {
            "code": {
                "type": "string",
                "description": "Python script to execute; use print(...) to display exact results.",
            },
        },
    },
}

_COMMIT_BLUEPRINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(BLUEPRINT_SCHEMA["required"]) + ["agent_clarifications", "excluded_employees"],
    "properties": {
        **BLUEPRINT_SCHEMA["properties"],
        "agent_clarifications": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Concrete, evidence-backed clarification questions for the broker/client discovered "
                "across documents (e.g., SP=Y flag with no spouse row, Benefits Booklet OON OOP anomaly, "
                "missing proposed effective date, Benefits Booklet not replacing official carrier SBC)."
            ),
        },
        "excluded_employees": {
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
            "description": (
                "Exact list (computed via run_python over 100% of rows) of employees who must be excluded "
                "from the active Full-Time medical census: "
                "(a) test/placeholder records (e.g. 'Test Employee'), "
                "(b) confirmed terminated/inactive non-COBRA employees, "
                "(c) Part-Time / Seasonal / Per-Diem / Contractor employees, or "
                "(d) employees appearing only in an older all-employee demographic export who are ABSENT "
                "from the current authoritative Active Full-Time / On-Leave / Part-Time roster AND not billed on the current medical invoice."
            ),
        },
        "status_overrides": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["person_name", "coverage", "reason"],
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
            "description": (
                "Optional exact status/coverage overrides computed via run_python for employees whose "
                "medical status is unambiguous from source columns (e.g. Decline Reason -> WO, Waiting Period -> WP, "
                "Active COBRA participant -> cobra='Y') but could be missed by generic column heuristics."
            ),
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
            "description": (
                "Optional exact (dependent_name -> subscriber_name) household links computed via run_python, "
                "especially for separate Dependent tabs where a spouse or child has a different last name "
                "(e.g. maiden/blended surname) and matches a tier-compatible employee by exact 5-digit ZIP."
            ),
        },
    },
}

INTAKE_AGENT_TOOLS: list[dict[str, Any]] = [
    _RUN_PYTHON_TOOL,
    {
        "name": "inspect_spreadsheet_rows",
        "description": (
            "Read a specific 0-based row slice from an uploaded Excel/CSV spreadsheet sheet, showing exact "
            "0-based column indices ([0]=..., [1]=...). Use this to verify headers, coded values, edge-case "
            "rows (e.g. SP=Y, waived, dependents), and invoice totals/footers."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["file_name", "start_row", "end_row"],
            "properties": {
                "file_name": {"type": "string", "description": "Exact uploaded filename"},
                "sheet_name": {"type": "string", "description": "Optional worksheet name (defaults to first sheet)"},
                "start_row": {"type": "integer", "description": "0-based start row (inclusive)"},
                "end_row": {"type": "integer", "description": "0-based end row (exclusive, max 60 rows per call)"},
            },
        },
    },
    {
        "name": "inspect_pdf_text",
        "description": (
            "Read a character window of any uploaded PDF/DOCX/email document (e.g. to check whether a file "
            "is a carrier SBC vs. an employee Benefits Booklet, inspect rate tables, or verify email facts)."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["file_name", "start_char"],
            "properties": {
                "file_name": {"type": "string", "description": "Exact uploaded filename"},
                "start_char": {"type": "integer", "description": "0-based character offset"},
                "max_chars": {"type": "integer", "description": "Characters to return (default 6000, max 10000)"},
            },
        },
    },
    {
        "name": "commit_submission_blueprint",
        "description": (
            "Commit the complete, verified whole-submission Blueprint once you have inspected all relevant "
            "files and are 100% confident in document roles, prospect metadata, census column mappings, "
            "coded value maps, email facts, plan links, invoice totals, and broker clarifications."
        ),
        "input_schema": _COMMIT_BLUEPRINT_SCHEMA,
    },
]


# ----------------------------------------------------------- Tool Handlers --
def _find_doc(docs: list[Any], file_name: str) -> Optional[Any]:
    target = clean(file_name).lower()
    for d in docs:
        if clean(getattr(d, "name", "")).lower() == target:
            return d
    for d in docs:
        if target and target in clean(getattr(d, "name", "")).lower():
            return d
    return None


def execute_inspection_tool(
    name: str,
    tool_input: dict[str, Any],
    docs: list[Any],
    sandbox: Optional[PythonSandboxSession] = None,
) -> str:
    """Execute a read-only inspection or Python analysis tool deterministically on the loaded RawDocs."""
    if name == "run_python":
        code = str(tool_input.get("code") or "")
        if sandbox is not None:
            return sandbox.run(code)
        with PythonSandboxSession(docs) as ephemeral_sb:
            return ephemeral_sb.run(code)

    if name == "inspect_spreadsheet_rows":
        fn = str(tool_input.get("file_name") or "")
        sn = clean(tool_input.get("sheet_name") or "").lower()
        start_r = max(0, int(tool_input.get("start_row") or 0))
        end_r = min(start_r + 60, max(start_r + 1, int(tool_input.get("end_row") or (start_r + 30))))
        doc = _find_doc(docs, fn)
        if doc is None:
            return f"ERROR: File '{fn}' not found. Available: {[getattr(d, 'name', '') for d in docs]}"
        tables = getattr(doc, "tables", None) or []
        if not tables:
            return f"File '{doc.name}' has no parsed tables."
        tbl = None
        if sn:
            tbl = next((t for t in tables if clean(getattr(t, "name", "")).lower() == sn), None)
        if tbl is None:
            tbl = tables[0]
        df = getattr(tbl, "df", None)
        if df is None or df.empty:
            return f"Sheet '{tbl.name}' in '{doc.name}' is empty."
        end_r = min(end_r, len(df))
        out = [f"FILE: {doc.name} | SHEET: {tbl.name} | TOTAL_ROWS: {len(df)} | SHOWING: {start_r}..{end_r - 1}"]
        for ri in range(start_r, end_r):
            cells = [clean(v) for v in df.iloc[ri].tolist()]
            pairs = [f"[{ci}]={v}" for ci, v in enumerate(cells) if v]
            if pairs:
                out.append(f"Row {ri}: " + " | ".join(pairs[:25]))
        return "\n".join(out) if len(out) > 1 else f"Rows {start_r}..{end_r - 1} are blank."

    if name == "inspect_pdf_text":
        fn = str(tool_input.get("file_name") or "")
        start_c = max(0, int(tool_input.get("start_char") or 0))
        max_c = min(10000, max(500, int(tool_input.get("max_chars") or 6000)))
        doc = _find_doc(docs, fn)
        if doc is None:
            return f"ERROR: File '{fn}' not found. Available: {[getattr(d, 'name', '') for d in docs]}"
        txt = getattr(doc, "text", "") or ""
        chunk = txt[start_c:start_c + max_c]
        return f"FILE: {doc.name} | TOTAL_CHARS: {len(txt)} | SLICE: {start_c}..{start_c + len(chunk)}\n{chunk}"

    return f"ERROR: Unknown tool '{name}'"


def _get_shared_agent_tools() -> list[dict[str, Any]]:
    """Return a unified toolset for live cached runs so Intake and Workbook Auditor share the exact same `tools + system` cache prefix."""
    from workbook_auditor import AUDIT_SCHEMA
    return list(INTAKE_AGENT_TOOLS) + [
        {
            "name": "submit_workbook_audit",
            "description": "Submit the completed Post-Generation Workbook QA Audit report (only used by the Post-Generation QA Auditor).",
            "input_schema": AUDIT_SCHEMA,
        }
    ]
def _agent_loop_enabled() -> bool:
    if os.getenv("CENSUS_DISABLE_AGENT_LOOP", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.getenv("CENSUS_DISABLE_BLUEPRINT", "").strip().lower() in ("1", "true", "yes"):
        return False
    from census_config import ANTHROPIC_API_KEY, LLM_FIRST, USE_LLM_FALLBACK
    return bool(LLM_FIRST and USE_LLM_FALLBACK and ANTHROPIC_API_KEY and not ANTHROPIC_API_KEY.startswith("sk-ant-test"))


def _build_cached_agent_payload(
    base_system: str,
    role_instructions: str,
    messages: list[dict[str, Any]],
    api_key: str,
) -> tuple[Any, list[dict[str, Any]]]:
    """Attach Anthropic ephemeral cache_control breakpoints to system instructions and multi-turn messages.

    Breakpoint 1: Shared governing official instructions in `system` (cached once, reused across all turns and stages).
    Breakpoint 2: Initial submission/snapshot user message (`messages[0]`).
    Breakpoint 3: Latest tool_result user turn (`messages[-1]`) on multi-turn loops so prior tool turns are cached.
    """
    from caching.config import ANTHROPIC_PROMPT_CACHE_ENABLED
    if (
        not ANTHROPIC_PROMPT_CACHE_ENABLED
        or not api_key
        or api_key.startswith(("sk-ant-test", "sk-ant-real-mock"))
        or os.getenv("PYTEST_CURRENT_TEST", "") != ""
    ):
        full_sys = f"{base_system}\n\n{role_instructions}".strip()
        return full_sys, messages

    sys_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": base_system,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if role_instructions.strip():
        sys_blocks.append({
            "type": "text",
            "text": role_instructions.strip(),
        })

    cached_msgs: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        c = msg.get("content")
        if idx == 0 and msg.get("role") == "user" and isinstance(c, str) and len(c) >= 2000:
            cached_msgs.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": c,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            })
        elif idx == len(messages) - 1 and idx >= 2 and msg.get("role") == "user" and isinstance(c, list) and c:
            new_list = [dict(blk) if isinstance(blk, dict) else blk for blk in c]
            if isinstance(new_list[-1], dict):
                new_list[-1] = {**new_list[-1], "cache_control": {"type": "ephemeral"}}
            cached_msgs.append({"role": "user", "content": new_list})
        else:
            cached_msgs.append(msg)

    return sys_blocks, cached_msgs


def run_agent_intake_plan(
    docs: list[Any],
    *,
    max_turns: int = 4,
    bypass_cache: bool = False,
) -> Optional[SubmissionBlueprint]:
    """Run the Claude Tool-Using Intake Agent (with Sandboxed Python Code Interpreter) to inspect all files
    and commit a verified SubmissionBlueprint.

    Returns ``None`` cleanly when LLM/agent loop is disabled or offline so deterministic execution runs seamlessly.
    """
    if not docs or not _agent_loop_enabled():
        return None

    from census_config import ANTHROPIC_API_KEY, ANTHROPIC_EFFORT, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID
    from llm_client import (
        _STAGE_CHECKLISTS,
        get_instruction_doc_name,
        get_official_instructions,
        get_scoped_instructions,
        get_system_prompt,
        set_current_tracking,
    )

    set_current_tracking(file_name="whole_submission", step_name="agent_intake_blueprint")
    doc_name = get_instruction_doc_name()
    use_shared_sys_cache = bool(
        ANTHROPIC_API_KEY
        and not ANTHROPIC_API_KEY.startswith(("sk-ant-test", "sk-ant-real-mock"))
        and os.getenv("PYTEST_CURRENT_TEST", "") == ""
    )
    scoped_rules = _STAGE_CHECKLISTS["blueprint"] if use_shared_sys_cache else get_scoped_instructions("blueprint")
    digest = build_submission_digest(docs)
    cache_key_prompt = f"AGENT_INTAKE_V2_PY\n{doc_name}\n{digest}"
    effective_effort = ANTHROPIC_EFFORT or "high"

    # 1. Local SQLite prompt cache check (0 tokens on repeat runs)
    if not bypass_cache:
        try:
            from caching import get_local_cache
            cached = get_local_cache().get(
                cache_key_prompt, ANTHROPIC_MODEL, step_name="agent_intake_blueprint",
                bypass_cache=bypass_cache, effort=effective_effort, output_schema=_COMMIT_BLUEPRINT_SCHEMA,
            )
            if isinstance(cached, dict) and cached and any(k in cached for k in ("documents", "prospect", "census_schemas")):
                bp = validate_blueprint_dict(cached, docs=docs)
                bp.agent_clarifications = [clean(x) for x in (cached.get("agent_clarifications") or []) if clean(x)]
                bp.excluded_employees = [
                    {"person_name": clean(x.get("person_name", "")), "reason": clean(x.get("reason", ""))}
                    for x in (cached.get("excluded_employees") or [])
                    if isinstance(x, dict) and clean(x.get("person_name", ""))
                ]
                bp.status_overrides = [
                    {
                        "person_name": clean(x.get("person_name", "")),
                        "coverage": clean(x.get("coverage", "")).upper(),
                        "cobra": clean(x.get("cobra", "")).upper(),
                        "reason": clean(x.get("reason", "")),
                    }
                    for x in (cached.get("status_overrides") or [])
                    if isinstance(x, dict) and clean(x.get("person_name", "")) and clean(x.get("coverage", ""))
                ]
                bp.dependent_links = [
                    {
                        "dependent_name": clean(x.get("dependent_name", "")),
                        "subscriber_name": clean(x.get("subscriber_name", "")),
                        "reason": clean(x.get("reason", "")),
                    }
                    for x in (cached.get("dependent_links") or [])
                    if isinstance(x, dict) and clean(x.get("dependent_name", "")) and clean(x.get("subscriber_name", ""))
                ]
                bp.python_census_payload = cached.get("_python_census_payload")
                bp.python_invoices_payload = cached.get("_python_invoices_payload")
                log.info(
                    "⚡ [AGENT INTAKE CACHE HIT] Blueprint restored (%d docs, %d schemas, %d excluded, %d overrides, %d dep links, py_census=%s)",
                    len(bp.documents), len(bp.census_schemas), len(bp.excluded_employees), len(bp.status_overrides), len(bp.dependent_links),
                    bool(bp.python_census_payload),
                )
                return bp
        except Exception as _ce:
            log.debug("Agent intake cache lookup skipped: %s", _ce)

    base_system_prompt = (
        f"{get_system_prompt()}\n\nOFFICIAL INSTRUCTIONS ({doc_name}):\n{get_official_instructions()}"
        if use_shared_sys_cache
        else get_system_prompt()
    )
    role_system_prompt = (
        "You are operating as an interactive Underwriting Intake Agent with a Sandboxed Python Code Interpreter (`run_python`) "
        "and read-only document inspection tools (`inspect_spreadsheet_rows`, `inspect_pdf_text`). "
        "ALWAYS use `run_python` (e.g. `column_summary(...)` and exact pandas set-difference / filter queries) in ONE comprehensive script to inspect 100% of "
        "every census sheet's columns and unique values (Employment Status, Class, Worker Category, Medical Plan End Date, Decline Reason, COBRA) "
        "so you NEVER miss terminated, part-time, inactive, or waived employees on unseen file layouts. "
        "Then call `commit_submission_blueprint` ONCE with complete, Python-verified data."
    )
    user_prompt = f"""Analyze this entire medical benefits submission under {doc_name}.

SCOPED GOVERNING RULES:
{scoped_rules}

INSTRUCTIONS:
1. In ONE comprehensive `run_python` call, profile EVERY spreadsheet table (`list_files()`, `column_summary(file_name, sheet_name, header_row)`) and any invoice/renewal tables at once:
   - Inspect ALL distinct values of Employment Status / Position Status, Class / Worker Category, Relationship, Coverage / Tier, Medical Plan, Medical Plan End Date, Decline / Waive Reason, and COBRA.
   - If multiple census files exist, compute exact Python set differences between them (e.g., employees in an older all-employee export who are absent from the authoritative Active FT/PT roster).
   - Cross-check the resulting Active Full-Time employee count against any headcount stated in the RFP / GHQ / submission email and against the medical invoice subscriber count.
2. Pay special attention to:
   - PEO status: "Current PEO: NA" / "N/A" / "None" / "Direct" means `is_peo: false`, `peo_name: ""`.
   - Proposed Effective Date vs. Current Renewal Date: do NOT put Renewal Date into `effective_date` if Proposed Effective Date is unstated.
   - Exact 0-based `header_row` and `column_mappings` for every census sheet (including unusual names like `Class`, `Employment Status`, `Residential Zip Code`, `Enrollee`, coded `1/0` columns).
   - Multi-benefit invoices: extract printed medical current charges, adjustments, and Total Amount Due.
   - `agent_clarifications`: include concrete broker questions grounded in the files (e.g., Benefits Booklet ≠ carrier SBC, SP=Y without spouse row, plan benefit inconsistencies, missing effective date).
   - `excluded_employees`: use `run_python` to compute the EXACT list of employees who must be excluded (test/placeholder rows, Terminated/Inactive non-COBRA employees, Part-Time/Seasonal/Per-Diem/Contractor employees, or employees absent from the authoritative active roster and not on the invoice).
   - `status_overrides`: when `run_python` reveals unambiguous medical status from source columns (e.g. `Medical Decline Reason` -> `WO`, recent hire in waiting period -> `WP`, active `COBRA` participant -> `cobra="Y"`, or ended medical coverage with no active plan -> `WO`), emit exact overrides so zero rows remain blank or misclassified.
   - `dependent_links`: if a separate Dependent tab/file has dependents whose last name differs from their employee (e.g. maiden/blended surname) or lacks a Subscriber column, use `run_python` to match them by exact 5-digit ZIP + tier compatibility (`ES`/`EC`/`EF`/`FAM`) and emit exact `(dependent_name -> subscriber_name)` links.
3. Call `commit_submission_blueprint` immediately on your second turn once verified (do not split Python inspection into multiple small turns when one script can inspect everything).

SUBMISSION DIGEST ({len(docs)} files):
{digest}
"""

    import anthropic

    headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, default_headers=headers)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    committed_raw: Optional[dict[str, Any]] = None

    t0 = time.perf_counter()
    c_payload_emitted: Optional[dict[str, Any]] = None
    i_payload_emitted: Optional[dict[str, Any]] = None
    try:
        with PythonSandboxSession(docs) as sandbox:
            for turn in range(max_turns):
                is_last_turn = (turn == max_turns - 1)
                sys_payload, msgs_payload = _build_cached_agent_payload(
                    base_system_prompt, role_system_prompt, messages, ANTHROPIC_API_KEY
                )
                active_tools = _get_shared_agent_tools() if use_shared_sys_cache else INTAKE_AGENT_TOOLS
                create_kwargs: dict[str, Any] = {
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 8192,
                    "output_config": {"effort": effective_effort},
                    "system": sys_payload,
                    "tools": active_tools,
                    "messages": msgs_payload,
                }
                # Note: Opus 5.5 rejects tool_choice={"type":"tool"} / "any" with effort; use natural auto + prompt nudge
                t_call = time.perf_counter()
                resp = client.messages.create(**create_kwargs)
                try:
                    from caching import record_anthropic_usage
                    record_anthropic_usage(getattr(resp, "usage", None), step_name="agent_intake_blueprint", latency_sec=time.perf_counter() - t_call)
                except Exception:
                    pass

                try:
                    from core.universal_token_monitor import track_usage
                    track_usage(
                        resp.usage, model=ANTHROPIC_MODEL, poc_name="BENEFITS_UW",
                        file_name="whole_submission", step_name=f"agent_intake_turn_{turn + 1}",
                    )
                except Exception:
                    pass

                assistant_blocks = list(getattr(resp, "content", []) or [])
                tool_uses = [b for b in assistant_blocks if getattr(b, "type", "") == "tool_use"]
                if not tool_uses:
                    # Model stopped without calling commit tool; nudge once on next turn
                    messages.append({"role": "assistant", "content": assistant_blocks})
                    messages.append({
                        "role": "user",
                        "content": "Please call `commit_submission_blueprint` now with the verified submission blueprint.",
                    })
                    continue

                tool_results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    t_name = getattr(tu, "name", "")
                    t_input = getattr(tu, "input", {}) or {}
                    t_id = getattr(tu, "id", "")
                    if t_name == "commit_submission_blueprint" and isinstance(t_input, dict) and t_input:
                        committed_raw = t_input
                        break
                    res_text = execute_inspection_tool(t_name, t_input, docs, sandbox=sandbox)
                    _preview = (str(t_input.get("code", ""))[:100].replace("\n", " ") + "...") if t_name == "run_python" else str(t_input)
                    log.info("🔎 [AGENT TOOL] Turn %d | %s(%s) -> %d chars", turn + 1, t_name, _preview, len(res_text))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": t_id,
                        "content": res_text,
                    })

                if committed_raw is not None:
                    break

                if is_last_turn:
                    break
                if turn >= max_turns - 2:
                    tool_results.append({
                        "type": "text",
                        "text": "Inspection budget is nearly complete. Call `commit_submission_blueprint` on your very next response.",
                    })

                messages.append({"role": "assistant", "content": assistant_blocks})
                messages.append({"role": "user", "content": tool_results})

            c_payload_emitted = sandbox.read_canonical_census()
            i_payload_emitted = sandbox.read_canonical_invoices()

        if not committed_raw:
            log.warning("Agent Intake Loop completed without committing a blueprint; falling back.")
            return None

        bp = validate_blueprint_dict(committed_raw, docs=docs)
        bp.python_census_payload = c_payload_emitted
        bp.python_invoices_payload = i_payload_emitted
        bp.agent_clarifications = [clean(x) for x in (committed_raw.get("agent_clarifications") or []) if clean(x)]
        bp.excluded_employees = [
            {"person_name": clean(x.get("person_name", "")), "reason": clean(x.get("reason", ""))}
            for x in (committed_raw.get("excluded_employees") or [])
            if isinstance(x, dict) and clean(x.get("person_name", ""))
        ]
        bp.status_overrides = [
            {
                "person_name": clean(x.get("person_name", "")),
                "coverage": clean(x.get("coverage", "")).upper(),
                "cobra": clean(x.get("cobra", "")).upper(),
                "reason": clean(x.get("reason", "")),
            }
            for x in (committed_raw.get("status_overrides") or [])
            if isinstance(x, dict) and clean(x.get("person_name", "")) and clean(x.get("coverage", ""))
        ]
        bp.dependent_links = [
            {
                "dependent_name": clean(x.get("dependent_name", "")),
                "subscriber_name": clean(x.get("subscriber_name", "")),
                "reason": clean(x.get("reason", "")),
            }
            for x in (committed_raw.get("dependent_links") or [])
            if isinstance(x, dict) and clean(x.get("dependent_name", "")) and clean(x.get("subscriber_name", ""))
        ]
        elapsed = time.perf_counter() - t0
        log.info(
            "🤖 [AGENT INTAKE] Committed verified Blueprint in %.2fs: %d docs, %d census schemas, %d email facts, %d clarifications, %d excluded, %d overrides, %d dep links",
            elapsed, len(bp.documents), len(bp.census_schemas), len(bp.email_facts),
            len(bp.agent_clarifications), len(bp.excluded_employees), len(bp.status_overrides), len(bp.dependent_links),
        )

        # Cache validated blueprint locally (including any emitted canonical census/invoices payloads)
        try:
            from caching import get_local_cache
            cache_obj = dict(committed_raw)
            if c_payload_emitted:
                cache_obj["_python_census_payload"] = c_payload_emitted
            if i_payload_emitted:
                cache_obj["_python_invoices_payload"] = i_payload_emitted
            get_local_cache().set(
                prompt=cache_key_prompt, model=ANTHROPIC_MODEL, response_data=cache_obj,
                step_name="agent_intake_blueprint", tokens_saved_estimate=len(cache_key_prompt) // 4,
                effort=effective_effort, output_schema=_COMMIT_BLUEPRINT_SCHEMA,
            )
        except Exception:
            pass

        return bp
    except Exception as exc:
        log.warning("Agent Intake Loop skipped (falling back to single-pass blueprint/deterministic): %s", exc)
        return None


# ----------------------------------------- Tool-Based Post-Generation Audit -
def run_tool_based_workbook_audit(
    snapshot: dict[str, Any],
    *,
    docs: Optional[list[Any]] = None,
    extra_files: Optional[dict[str, str]] = None,
    max_turns: int = 4,
    repair_actions_out: Optional[dict[str, Any]] = None,
) -> tuple[bool, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the Post-Generation LLM Workbook Audit via Anthropic Tool Use (`run_python` + `submit_workbook_audit`).

    When ``docs`` or ``extra_files`` (generated Excel paths) are supplied, Claude can run read-only Python
    checks directly over the saved workbooks and source files before calling ``submit_workbook_audit``.
    """
    from census_config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID
    from llm_client import (
        _STAGE_CHECKLISTS,
        get_instruction_doc_name,
        get_official_instructions,
        get_scoped_instructions,
        get_system_prompt,
        set_current_tracking,
    )
    from workbook_auditor import AUDIT_SCHEMA, _validate_audit_dict

    wb_name = snapshot["workbook"]["workbook_name"]
    set_current_tracking(file_name=wb_name, step_name="workbook_auditor")
    doc_name = get_instruction_doc_name()
    use_shared_sys_cache = bool(
        ANTHROPIC_API_KEY
        and not ANTHROPIC_API_KEY.startswith(("sk-ant-test", "sk-ant-real-mock"))
        and os.getenv("PYTEST_CURRENT_TEST", "") == ""
    )
    scoped_rules = _STAGE_CHECKLISTS["audit"] if use_shared_sys_cache else get_scoped_instructions("audit")

    audit_tool = {
        "name": "submit_workbook_audit",
        "description": "Submit the completed Post-Generation Workbook QA Audit report.",
        "input_schema": AUDIT_SCHEMA,
    }
    # Default to a single-turn cached audit over the rich deliverable snapshot unless multi-turn Python audit is explicitly enabled
    multi_turn_py_audit = os.getenv("CENSUS_AUDIT_MULTI_TURN_PY", "0").strip().lower() in ("1", "true", "yes")
    has_sandbox = bool((docs or extra_files) and multi_turn_py_audit)
    audit_tools = [_RUN_PYTHON_TOOL, audit_tool] if has_sandbox else [audit_tool]

    py_hint = (
        "\nYou are operating as an INDEPENDENT ADVERSARIAL VERIFIER with `run_python` (read-only access to the "
        "saved Excel workbook(s) in `FILES` and all raw source files in `TABLES`/`TEXTS`).\n"
        "BEFORE calling `submit_workbook_audit`, run a quick Python verification script over `FILES[wb_name]` vs `TABLES` to check:\n"
        "  1. Any blank `Gender`, `DOB`, or `ZIP` cell in the Excel that IS populated in the source spreadsheet -> emit `repair_actions.field_fixes`.\n"
        "  2. Any employee marked `COBRA = Y` when source has NO explicit COBRA column/value (e.g. Auto Enrollment = Y) -> emit `repair_actions.field_fixes` with `cobra='N'`.\n"
        "  3. Any duplicate child/spouse under the same subscriber, or dropped different-surname dependent -> emit `repair_actions.dependent_links`.\n"
        "  4. Any leaked Terminated/Inactive non-COBRA or Part-Time employee -> emit `repair_actions.exclude_employees`.\n"
        "  5. Any blank or wrong medical coverage status (`WO`/`WP`/`NE`) -> emit `repair_actions.status_overrides`.\n"
        "  6. Verify `Summary` medical premium totals match the printed invoice totals.\n"
        "If you populate any `repair_actions`, the pipeline will automatically repair and regenerate the Excel before delivery!\n"
        if has_sandbox else ""
    )
    prompt = f"""You are the Post-Generation Underwriting QA Auditor operating under {doc_name}.

SCOPED GOVERNING RULES:
{scoped_rules}

TASK:
Review the ALREADY-GENERATED Excel deliverable snapshot, Summary Sheet, SBU summary, and Missing Information Email
against the Source Submission Ground Truth below, then call `submit_workbook_audit`.{py_hint}
Flag ONLY real, concrete, evidence-backed defects across:
1. Internal contradictions (e.g. Group Info Current PEO vs Summary renewal text; stated enrollment vs output lives).
2. Column heading vs value semantics (e.g. individual Subscriber IDs placed under 'Invoice Group #').
3. Missing available source details (e.g. blank plan names with single medical SBC, blank ZIPs/tiers/IDs).
4. Non-medical leakage or ancillary-only erasure (e.g. dental/vision in medical totals, or ancillary-only employee marked 'Not on invoice').
5. Financial reconciliation gaps or unexplained variances.
6. Dependent/population/audit-count integrity (e.g. dependents wiped out, blank dependent coverage, invoice-only in A-J or SBU, leaked terminated or part-time employees).
7. Missing Information Email accuracy (false SBC/renewal requests, fake legal entity mismatch, missed structural flags).

Always provide a concise non-empty `confidence_summary` explaining what you verified.

DELIVERABLE SNAPSHOT & SOURCE GROUND TRUTH:
{snapshot['digest_text']}
"""
    # Local cache lookup
    try:
        from caching import get_local_cache
        cached = get_local_cache().get(
            prompt, ANTHROPIC_MODEL, step_name="workbook_auditor",
            effort="medium", output_schema=AUDIT_SCHEMA,
        )
        if isinstance(cached, dict) and cached.get("confidence_summary"):
            v = _validate_audit_dict(cached)
            if repair_actions_out is not None and isinstance(v.get("repair_actions"), dict):
                repair_actions_out.update(v["repair_actions"])
            return True, v["confidence_summary"], v["findings"], v["safe_summary_patches"]
    except Exception:
        pass

    import anthropic

    headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, default_headers=headers)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": prompt + ("\n\nVerify any suspicious counts with `run_python` if needed, then call `submit_workbook_audit`." if has_sandbox else "\n\nCall `submit_workbook_audit` now.")}
    ]
    submitted_raw: Optional[dict[str, Any]] = None

    base_sys_aud = (
        f"{get_system_prompt()}\n\nOFFICIAL INSTRUCTIONS ({doc_name}):\n{get_official_instructions()}"
        if use_shared_sys_cache
        else get_system_prompt()
    )
    sb_ctx = PythonSandboxSession(docs or [], extra_files=extra_files) if has_sandbox else None
    try:
        turns = max_turns if has_sandbox else 1
        for turn in range(turns):
            sys_payload, msgs_payload = _build_cached_agent_payload(
                base_sys_aud, "", messages, ANTHROPIC_API_KEY
            )
            active_aud_tools = _get_shared_agent_tools() if use_shared_sys_cache else audit_tools
            t_call = time.perf_counter()
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=6144,
                output_config={"effort": "medium"},
                system=sys_payload,
                tools=active_aud_tools,
                messages=msgs_payload,
            )
            try:
                from caching import record_anthropic_usage
                record_anthropic_usage(getattr(resp, "usage", None), step_name="workbook_auditor", latency_sec=time.perf_counter() - t_call)
            except Exception:
                pass
            try:
                from core.universal_token_monitor import track_usage
                track_usage(
                    resp.usage, model=ANTHROPIC_MODEL, poc_name="BENEFITS_UW",
                    file_name=wb_name, step_name="workbook_auditor",
                )
            except Exception:
                pass

            assistant_blocks = list(getattr(resp, "content", []) or [])
            tool_uses = [b for b in assistant_blocks if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                if turn < turns - 1:
                    messages.append({"role": "assistant", "content": assistant_blocks})
                    messages.append({"role": "user", "content": "Call `submit_workbook_audit` now."})
                    continue
                break

            tool_results: list[dict[str, Any]] = []
            # Execute all `run_python` calls first before reading `submit_workbook_audit`
            for tu in tool_uses:
                t_name = getattr(tu, "name", "")
                t_input = getattr(tu, "input", {}) or {}
                t_id = getattr(tu, "id", "")
                if t_name == "run_python" and sb_ctx is not None:
                    res_text = sb_ctx.run(str(t_input.get("code") or ""))
                    log.info("🔎 [AUDIT PYTHON] Turn %d -> %d chars", turn + 1, len(res_text))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": t_id,
                        "content": res_text,
                    })
                elif t_name == "submit_workbook_audit" and isinstance(t_input, dict):
                    submitted_raw = t_input

            if submitted_raw is not None or turn == turns - 1:
                break
            if turn >= turns - 2:
                tool_results.append({
                    "type": "text",
                    "text": "Call `submit_workbook_audit` on your next response.",
                })
            messages.append({"role": "assistant", "content": assistant_blocks})
            messages.append({"role": "user", "content": tool_results})
    finally:
        if sb_ctx is not None:
            sb_ctx.close()

    if submitted_raw is None:
        return False, "", [], []

    validated = _validate_audit_dict(submitted_raw)
    if not validated.get("confidence_summary"):
        validated["confidence_summary"] = (
            "Workbook audit completed with 0 defects." if not validated["findings"]
            else f"Workbook audit flagged {len(validated['findings'])} defect(s)."
        )
    try:
        from caching import get_local_cache
        get_local_cache().set(
            prompt=prompt, model=ANTHROPIC_MODEL, response_data=validated,
            step_name="workbook_auditor", tokens_saved_estimate=len(prompt) // 4,
            effort="medium", output_schema=AUDIT_SCHEMA,
        )
    except Exception:
        pass

    if repair_actions_out is not None and isinstance(validated.get("repair_actions"), dict):
        repair_actions_out.update(validated["repair_actions"])
    return True, validated["confidence_summary"], validated["findings"], validated["safe_summary_patches"]


# ---------------------------- Sanity-Gate Fallback: Python Canonical Census -
def canonical_json_to_census(
    payload: dict[str, Any],
    *,
    source_files: Optional[list[str]] = None,
) -> Optional[Any]:
    """Convert a Python-emitted `canonical_census.json` payload into a validated `Census` model."""
    if not isinstance(payload, dict) or not isinstance(payload.get("employees"), list):
        return None
    raw_emps = [e for e in payload["employees"] if isinstance(e, dict)]
    if not raw_emps:
        return None

    from census_builder import recalculate_row_numbers, update_candidate_dispositions
    from models import Census, Person
    from utils import parse_date, std_cobra, std_coverage, std_gender, std_relationship, std_zip, to_money

    census = Census()
    census.files_used = list(source_files or [])
    rec_counter = 0

    def _next_id() -> str:
        nonlocal rec_counter
        rec_counter += 1
        return f"rec_{rec_counter:04d}"

    emp_count = 0
    dep_count = 0
    for er in raw_emps:
        first = clean(er.get("first") or er.get("first_name") or "")
        last = clean(er.get("last") or er.get("last_name") or "")
        if not (first or last):
            continue
        cov = std_coverage(er.get("coverage") or er.get("tier") or "") or clean(er.get("coverage") or "").upper()
        if cov not in ("EE", "ES", "EC", "FAM", "EF", "WO", "WP", "NE", "RC"):
            cov = "EE" if clean(er.get("plan") or "") else "WO"
        if cov == "EF":
            cov = "FAM"
        plan = clean(er.get("plan") or "")
        if cov in ("WO", "WP", "NE", "RC") and not plan:
            plan = "NA"
        cobra = std_cobra(er.get("cobra")) or "N"
        raw_ez = er.get("zip")
        if isinstance(raw_ez, str) and raw_ez.strip().isdigit() and len(raw_ez.strip()) == 4:
            raw_ez = raw_ez.strip().zfill(5)
        emp_zip_val = std_zip(raw_ez or "")
        if isinstance(emp_zip_val, tuple):
            emp_zip = emp_zip_val[0] if emp_zip_val[1] else ""
        else:
            emp_zip = str(emp_zip_val or "")
        emp = Person(
            role="EE",
            first=first,
            last=last,
            gender=std_gender(er.get("gender") or ""),
            dob=parse_date(er.get("dob")),
            zip=emp_zip,
            coverage=cov,
            cobra=cobra,
            plan=plan,
            premium=to_money(er.get("premium")),
            sub_id=clean(er.get("sub_id") or ""),
            hire_date=parse_date(er.get("hire_date")),
            source=clean(er.get("source") or (source_files[0] if source_files else "agent_python")),
            rel_raw="EE",
            record_id=_next_id(),
            gender_resolution_status="RESOLVED" if std_gender(er.get("gender") or "") else "MISSING",
        )
        for _en in (er.get("notes") or []):
            if clean(_en):
                emp.note(clean(_en))
        if cov in ("EE", "ES", "EC", "FAM"):
            emp.eligibility_status = "ENROLLED"
            emp.disposition = "INCLUDE_ACTIVE_MEDICAL"
        elif cov == "WP":
            emp.eligibility_status = "WAITING_PERIOD"
            emp.disposition = "RETAIN_MEDICAL_STATUS"
        elif cov == "WO":
            emp.eligibility_status = "WAIVED"
            emp.disposition = "RETAIN_MEDICAL_STATUS"
        elif cov == "NE":
            emp.eligibility_status = "NOT_ELIGIBLE"
            emp.disposition = "RETAIN_MEDICAL_STATUS"
        else:
            emp.eligibility_status = "REFUSED"
            emp.disposition = "RETAIN_MEDICAL_STATUS"

        census.people.append(emp)
        emp_count += 1

        # Attach linked active medical dependents
        for dr in (er.get("dependents") or []):
            if not isinstance(dr, dict):
                continue
            dfirst = clean(dr.get("first") or dr.get("first_name") or "")
            dlast = clean(dr.get("last") or dr.get("last_name") or "")
            if not (dfirst or dlast):
                continue
            drole = std_relationship(dr.get("role") or dr.get("relationship") or "") or "CH"
            if drole not in ("SP", "CH", "DP"):
                drole = "CH"
            raw_dz = dr.get("zip")
            if isinstance(raw_dz, str) and raw_dz.strip().isdigit() and len(raw_dz.strip()) == 4:
                raw_dz = raw_dz.strip().zfill(5)
            dep_zip_val = std_zip(raw_dz or "")
            dep_zip = (dep_zip_val[0] if isinstance(dep_zip_val, tuple) else str(dep_zip_val or "")) or emp.zip
            dep = Person(
                role=drole,
                first=dfirst,
                last=dlast,
                gender=std_gender(dr.get("gender") or ""),
                dob=parse_date(dr.get("dob")),
                zip=dep_zip,
                coverage=emp.coverage,
                cobra=emp.cobra or "N",
                plan=emp.plan,
                source=clean(dr.get("source") or emp.source),
                rel_raw=drole,
                record_id=_next_id(),
                subscriber_id=emp.record_id,
                employee=emp,
                eligibility_status="DEPENDENT",
                disposition="INCLUDE_ACTIVE_MEDICAL",
                gender_resolution_status="RESOLVED" if std_gender(dr.get("gender") or "") else "MISSING",
            )
            emp.dependents.append(dep)
            census.people.append(dep)
            dep_count += 1

    if emp_count == 0:
        return None

    census.raw_employee_rows = emp_count
    census.raw_dependent_rows = dep_count
    for note in (payload.get("notes") or []):
        n_str = clean(note)
        if n_str and n_str not in census.census_notes:
            census.census_notes.append(n_str)
    recalculate_row_numbers(census)
    update_candidate_dispositions(census)
    return census


def build_canonical_census_via_python_agent(
    docs: list[Any],
    sanity_issues: list[Any],
    *,
    prospect: Any = None,
    effective_date: Any = None,
    max_turns: int = 6,
) -> Optional[Any]:
    """Copilot-style Sanity-Gate Fallback: when deterministic heuristic parsing trips the Population
    Sanity Gate on an unseen HRIS layout, Claude writes and executes a `pandas` script inside
    `PythonSandboxSession` over 100% of rows and calls `emit_canonical_census(...)` to build a clean
    canonical `Census` directly from Python (zero LLM row truncation).
    """
    if not docs or not _agent_loop_enabled():
        return None

    from census_config import ANTHROPIC_API_KEY, ANTHROPIC_EFFORT, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID
    from llm_client import (
        get_instruction_doc_name,
        get_scoped_instructions,
        get_system_prompt,
        set_current_tracking,
    )

    set_current_tracking(file_name="whole_submission", step_name="sanity_gate_python_census")
    doc_name = get_instruction_doc_name()
    scoped_rules = get_scoped_instructions("blueprint")
    issue_lines = "\n".join(
        f"  - [{getattr(i, 'severity', 'critical').upper()}] {getattr(i, 'message', str(i))}"
        for i in (sanity_issues or [])
    )

    confirm_tool = {
        "name": "confirm_canonical_census",
        "description": (
            "Call ONCE after your `run_python` script has called `emit_canonical_census(...)` and "
            "verified the exact active Full-Time employee and active medical dependent counts."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "employee_count", "dependent_count", "excluded_count"],
            "properties": {
                "summary": {"type": "string"},
                "employee_count": {"type": "integer"},
                "dependent_count": {"type": "integer"},
                "excluded_count": {"type": "integer"},
            },
        },
    }

    system_prompt = (
        get_system_prompt()
        + "\n\nYou are operating in SANITY-GATE RESCUE MODE with a live Sandboxed Python Code Interpreter (`run_python`). "
        "The standard heuristic census parser failed on this submission's file layout. Your job is to inspect the real "
        "DataFrames with `run_python`, write clean `pandas` logic tailored to this exact file, call "
        "`emit_canonical_census(payload)` directly from Python (so 100% of rows are written with zero truncation), "
        "verify the resulting counts, and then call `confirm_canonical_census`."
    )
    user_prompt = f"""The deterministic census parser tripped the Population Sanity Gate on this submission ({doc_name}):

SANITY GATE FAILURES TO FIX:
{issue_lines}

SCOPED RULES:
{scoped_rules}

PROSPECT CONTEXT:
- Company: {getattr(prospect, 'company', '') if prospect else ''}
- Proposed Effective Date: {effective_date or ''}
- Stated Headcount (if any): FT={getattr(prospect, 'ft_employees', None) if prospect else None}, Total={getattr(prospect, 'stated_total_employees', None) if prospect else None}

YOUR TASK (use `run_python` — `FILES`, `TABLES`, `TEXTS`, `list_files()`, `sheet_df()`, `column_summary()`, `emit_canonical_census()` are pre-loaded):
1. Call `run_python` to inspect `list_files()` and `column_summary(...)` on the census spreadsheet(s).
2. Write clean `pandas` code to build the exact active Full-Time (plus active COBRA) medical census:
   - EXCLUDE Terminated/Inactive non-COBRA employees, Part-Time/Seasonal/Contractor employees, and test/placeholder rows.
   - EXCLUDE all Non-Medical benefit rows (Dental, Vision, DPPO, Life, AD&D, STD, LTD, DBL, PFL, Critical Illness, Accident, Hospital Indemnity, MCO Tax).
   - Deduplicate employees across multi-row benefit reports; zero-pad 4-digit US ZIP codes to 5 digits (`str(z).zfill(5)`).
   - Link ONLY active medical dependents to their true subscriber (matching `ES` -> SP, `EC` -> CH, `FAM` -> SP+CH; exclude dependents on `EE`/`WO`/`WP`/`NE` employees).
   - Call `emit_canonical_census({{"employees": [...], "excluded_count": N, "notes": [...]}})` where each employee dict has:
     `first`, `last`, `gender` (`M`/`F`/``), `dob` (`YYYY-MM-DD`), `zip` (5-digit string), `coverage` (`EE`/`ES`/`EC`/`FAM`/`WO`/`WP`/`NE`/`RC`), `plan`, `cobra` (`Y`/`N`), `premium` (float or None), `source`, and `dependents` (list of `{{"role": "SP"|"CH"|"DP", "first", "last", "gender", "dob", "zip"}}`).
3. Print and verify counts in Python, then call `confirm_canonical_census`.
"""

    import anthropic

    headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, default_headers=headers)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    confirmed = False
    t0 = time.perf_counter()

    try:
        with PythonSandboxSession(docs) as sandbox:
            for turn in range(max_turns):
                resp = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=6144,
                    output_config={"effort": ANTHROPIC_EFFORT or "high"},
                    system=system_prompt,
                    tools=[_RUN_PYTHON_TOOL, confirm_tool],
                    messages=messages,
                )
                try:
                    from core.universal_token_monitor import track_usage
                    track_usage(
                        resp.usage, model=ANTHROPIC_MODEL, poc_name="BENEFITS_UW",
                        file_name="whole_submission", step_name=f"sanity_rescue_turn_{turn + 1}",
                    )
                except Exception:
                    pass

                assistant_blocks = list(getattr(resp, "content", []) or [])
                tool_uses = [b for b in assistant_blocks if getattr(b, "type", "") == "tool_use"]
                if not tool_uses:
                    messages.append({"role": "assistant", "content": assistant_blocks})
                    messages.append({"role": "user", "content": "Run `emit_canonical_census(...)` via `run_python` and then call `confirm_canonical_census`."})
                    continue

                tool_results: list[dict[str, Any]] = []
                confirm_tu = None
                # Always execute all `run_python` calls FIRST so parallel tool calls never skip `emit_canonical_census`
                for tu in tool_uses:
                    t_name = getattr(tu, "name", "")
                    t_input = getattr(tu, "input", {}) or {}
                    t_id = getattr(tu, "id", "")
                    if t_name == "run_python":
                        res_text = sandbox.run(str(t_input.get("code") or ""))
                        log.info("🐍 [SANITY RESCUE PY] Turn %d -> %d chars", turn + 1, len(res_text))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": t_id,
                            "content": res_text,
                        })
                    elif t_name == "confirm_canonical_census":
                        confirm_tu = tu

                if confirm_tu is not None:
                    if sandbox.read_canonical_census() is not None:
                        confirmed = True
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": getattr(confirm_tu, "id", ""),
                            "content": "ERROR: canonical_census.json was NOT emitted yet. Call emit_canonical_census(...) inside run_python first, then call confirm_canonical_census.",
                        })

                if confirmed or turn == max_turns - 1:
                    break
                if turn >= max_turns - 2:
                    tool_results.append({
                        "type": "text",
                        "text": "Call `emit_canonical_census(...)` if not yet called, then call `confirm_canonical_census` now.",
                    })
                messages.append({"role": "assistant", "content": assistant_blocks})
                messages.append({"role": "user", "content": tool_results})

            payload = sandbox.read_canonical_census()
            if not payload:
                log.warning("Sanity-Gate Python Rescue did not emit canonical_census.json.")
                return None
            src_names = [getattr(d, "name", "") for d in docs if getattr(d, "name", "")]
            rescued = canonical_json_to_census(payload, source_files=src_names)
            if rescued:
                log.info(
                    "🤖 [SANITY RESCUE] Built canonical Census via Python in %.2fs: %d EEs, %d Deps",
                    time.perf_counter() - t0, len(rescued.employees), len(rescued.dependents),
                )
            return rescued
    except Exception as exc:
        log.warning("Sanity-Gate Python Rescue skipped: %s", exc)
        return None


# ------------------- Pillar #1 & #4: Agent-Python-First Census + Invoices ---
def _agent_python_first_enabled() -> bool:
    # By default ("auto" / "0"), the unified Intake Agent Blueprint + deterministic census_builder
    # runs first (zero extra LLM loop, 100% deterministic audit/dependent/zip fidelity), and
    # Python-Sandbox census building runs automatically via Sanity-Gate Rescue only if needed.
    # Set CENSUS_AGENT_PYTHON_FIRST=force (or 1 with CENSUS_FORCE_AGENT_PY_IN_TEST=1) to force the separate loop.
    mode = os.getenv("CENSUS_AGENT_PYTHON_FIRST", "auto").strip().lower()
    if mode in ("0", "false", "no", "auto", ""):
        return False
    if os.getenv("PYTEST_CURRENT_TEST", "") != "" and os.getenv("CENSUS_FORCE_AGENT_PY_IN_TEST", "") != "1":
        return False
    return _agent_loop_enabled()


def canonical_json_to_invoices(payload: dict[str, Any]) -> list[Any]:
    """Convert Python-emitted `canonical_invoices.json` into validated `InvoiceDoc` objects."""
    if not isinstance(payload, dict) or not isinstance(payload.get("invoices"), list):
        return []
    from models import InvoiceDoc, InvoiceRecord
    from utils import parse_date, split_full_name, std_coverage, to_money

    out_docs: list[InvoiceDoc] = []
    for inv in payload["invoices"]:
        if not isinstance(inv, dict):
            continue
        raw_recs = inv.get("records") or []
        recs: list[InvoiceRecord] = []
        for r in raw_recs:
            if not isinstance(r, dict):
                continue
            first = clean(r.get("first") or "")
            last = clean(r.get("last") or "")
            raw_name = clean(r.get("raw_name") or f"{first} {last}".strip())
            if not (first or last) and raw_name:
                first, last = split_full_name(raw_name)
            if not (first or last):
                continue
            prem = to_money(r.get("premium"))
            tier = std_coverage(r.get("tier") or "") or clean(r.get("tier") or "").upper()
            recs.append(InvoiceRecord(
                first=first,
                last=last,
                raw_name=raw_name,
                tier=tier,
                plan=clean(r.get("plan") or ""),
                raw_plan=clean(r.get("raw_plan") or r.get("plan") or ""),
                premium=prem,
                sub_id=clean(r.get("sub_id") or ""),
                group_no=clean(r.get("group_no") or inv.get("group_number") or ""),
                source=clean(inv.get("file_name") or "agent_python_invoice"),
            ))
        if not recs:
            continue
        out_docs.append(InvoiceDoc(
            path=clean(inv.get("file_name") or "agent_python_invoice"),
            records=recs,
            period_start=parse_date(inv.get("period_start")),
            period_end=parse_date(inv.get("period_end")),
            group_number=clean(inv.get("group_number") or ""),
            current_charges=to_money(inv.get("current_charges")),
            total_adjustments=to_money(inv.get("total_adjustments")),
            total_due=to_money(inv.get("total_due")),
        ))
    return out_docs


def run_agent_python_first_build(
    docs: list[Any],
    *,
    blueprint: Any = None,
    prospect: Any = None,
    effective_date: Any = None,
    max_turns: int = 8,
    bypass_cache: bool = False,
) -> tuple[Optional[Any], Optional[list[Any]]]:
    """Pillar #1 & #4 (Agent-Python-First Mode): Claude + Sandboxed Python builds the canonical
    `Census` (and optionally Python-verified `InvoiceDoc`s) directly from 100% of rows using
    retrieved Experiential Procedural Recipes (Pillar #2).

    Returns ``(census, invoice_docs)`` where either element may be ``None`` if not emitted
    (falling back cleanly to deterministic engines).
    """
    if not docs:
        return None, None
    src_names = [getattr(d, "name", "") for d in docs if getattr(d, "name", "")]
    if blueprint is not None and getattr(blueprint, "python_census_payload", None):
        c_obj = canonical_json_to_census(blueprint.python_census_payload, source_files=src_names)
        i_objs = canonical_json_to_invoices(blueprint.python_invoices_payload) if getattr(blueprint, "python_invoices_payload", None) else None
        if c_obj is not None:
            log.info(
                "🚀 [UNIFIED AGENT BUILD] Using canonical census emitted during Intake Loop (%d EEs, %d Deps, 0 extra LLM calls)",
                len(c_obj.employees), len(c_obj.dependents),
            )
            return c_obj, (i_objs or None)
    if not _agent_python_first_enabled():
        return None, None

    from census_config import ANTHROPIC_API_KEY, ANTHROPIC_EFFORT, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID
    from llm_client import (
        get_instruction_doc_name,
        get_scoped_instructions,
        get_system_prompt,
        set_current_tracking,
    )
    from recipe_memory import format_recipes_for_prompt, retrieve_relevant_recipes

    set_current_tracking(file_name="whole_submission", step_name="agent_python_first_build")
    doc_name = get_instruction_doc_name()
    scoped_rules = get_scoped_instructions("blueprint")
    digest = build_submission_digest(docs)
    recipes = retrieve_relevant_recipes(docs, max_recipes=5)
    recipes_block = format_recipes_for_prompt(recipes)
    effective_effort = ANTHROPIC_EFFORT or "high"
    cache_key_prompt = f"AGENT_PY_FIRST_V2\n{doc_name}\n{digest}\n{recipes_block}"
    src_names = [getattr(d, "name", "") for d in docs if getattr(d, "name", "")]

    # 1. Local SQLite cache check (0 tokens on repeat runs)
    if not bypass_cache:
        try:
            from caching import get_local_cache
            cached = get_local_cache().get(
                cache_key_prompt, ANTHROPIC_MODEL, step_name="agent_python_first_build",
                bypass_cache=bypass_cache, effort=effective_effort,
            )
            if isinstance(cached, dict) and cached.get("census_payload"):
                c_obj = canonical_json_to_census(cached["census_payload"], source_files=src_names)
                i_objs = canonical_json_to_invoices(cached["invoices_payload"]) if cached.get("invoices_payload") else None
                if c_obj is not None:
                    log.info(
                        "⚡ [AGENT PYTHON-FIRST CACHE HIT] Restored %d EEs / %d Deps (invoices=%d)",
                        len(c_obj.employees), len(c_obj.dependents), len(i_objs or []),
                    )
                    return c_obj, (i_objs or None)
        except Exception as _ce:
            log.debug("Agent Python-First cache lookup skipped: %s", _ce)

    confirm_tool = {
        "name": "confirm_agent_build",
        "description": (
            "Call ONCE after your `run_python` script has called `emit_canonical_census(...)` "
            "(and optionally `emit_canonical_invoices(...)` if medical invoices are present) and verified all counts."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "employee_count", "dependent_count", "excluded_count", "invoice_subscriber_count"],
            "properties": {
                "summary": {"type": "string"},
                "employee_count": {"type": "integer"},
                "dependent_count": {"type": "integer"},
                "excluded_count": {"type": "integer"},
                "invoice_subscriber_count": {"type": "integer"},
            },
        },
    }

    system_prompt = (
        get_system_prompt()
        + "\n\nYou are the PRIMARY Underwriting Data Builder operating in Agent-Python-First Mode with a live "
        "Sandboxed Python Code Interpreter (`run_python`). Inspect 100% of the uploaded files in Python, write clean "
        "`pandas` logic that faithfully implements the governing instructions and learned procedural recipes, call "
        "`emit_canonical_census(payload)` (and `emit_canonical_invoices(payload)` when medical invoices are present), "
        "verify every count and total in Python, and then call `confirm_agent_build`."
    )
    user_prompt = f"""Build the canonical medical census (and medical invoice records if invoices are present) for this submission under {doc_name}.

SCOPED GOVERNING RULES:
{scoped_rules}

{recipes_block}

PROSPECT CONTEXT:
- Company: {getattr(prospect, 'company', '') if prospect else ''}
- Proposed Effective Date: {effective_date or ''}
- Blueprint Summary: {blueprint.to_summary_dict() if (blueprint and hasattr(blueprint, 'to_summary_dict')) else 'None'}

INSTRUCTIONS (pre-loaded in `run_python`: `FILES`, `TABLES`, `TEXTS`, `list_files()`, `sheet_df()`, `column_summary()`, `search_text()`, `emit_canonical_census()`, `emit_canonical_invoices()`):
1. Profile EVERY census spreadsheet with `run_python` (`list_files()`, `column_summary(...)`).
2. Write clean `pandas` code to build the exact active Full-Time (plus active COBRA) medical census:
   - Exclude Terminated/Inactive non-COBRA employees, Part-Time/Seasonal/Contractor employees, and template example rows (e.g. John/Jane/Johnny Doe, Test Employee).
   - Exclude ALL non-medical benefit lines (Dental, Vision, DPPO, DHMO, Life, AD&D, STD, LTD, DBL, PFL, Critical Illness, Accident, Hospital Indemnity, MCO Tax).
   - Keep Employee Gender, DOB, and zero-padded 5-digit ZIP (`str(int(z)).zfill(5)`) intact for EVERY employee.
   - Set `cobra='Y'` ONLY when an explicit COBRA column or 'COBRA' text says so (never from 'Auto Enrollment = Y').
   - Link ONLY active medical dependents to their true subscriber (`ES`->SP, `EC`->CH, `FAM`->SP+CH; exclude dependents on `EE`/`WO`/`WP`/`NE` employees; link different-surname dependents on separate tabs via exact 5-digit ZIP + tier compatibility; deduplicate same-name children under the same subscriber).
   - AUTHORITATIVE SUBMISSION EMAIL ADDITIONS/TERMINATIONS: Inspect all email/PDF text in `TEXTS` (`search_text(...)`). If a broker or client email explicitly asks to ADD an employee (and/or spouse/dependents) not yet in the Excel census (providing name, DOB, tier, etc.), INCLUDE them directly in `emit_canonical_census(...)` with all demographics stated in the email (leaving any unstated Gender/ZIP blank so they are flagged for confirmation) and note `"Added per submission email"`. Conversely, exclude any employee confirmed terminated in the email (unless COBRA).
   - Call `emit_canonical_census({{"employees": [...], "excluded_count": N, "notes": [...]}})`.
3. If medical invoice spreadsheet(s) or structured PDF billing text exist in `TABLES`/`TEXTS`, extract ONLY active medical subscriber rows (excluding Dental/Vision/Life/WC/Payroll), verify in Python that `sum(premiums)` reconciles to the printed medical subtotal, and call `emit_canonical_invoices({{"invoices": [...]}})`. (If invoice PDF is scanned/unstructured, skip `emit_canonical_invoices` so the dedicated PDF invoice parser handles it.)
4. Print final verification counts in Python and call `confirm_agent_build`.

SUBMISSION DIGEST ({len(docs)} files):
{digest}
"""

    import anthropic

    headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, default_headers=headers)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    confirmed = False
    t0 = time.perf_counter()

    try:
        with PythonSandboxSession(docs) as sandbox:
            for turn in range(max_turns):
                resp = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=8192,
                    output_config={"effort": effective_effort},
                    system=system_prompt,
                    tools=[_RUN_PYTHON_TOOL, confirm_tool],
                    messages=messages,
                )
                try:
                    from core.universal_token_monitor import track_usage
                    track_usage(
                        resp.usage, model=ANTHROPIC_MODEL, poc_name="BENEFITS_UW",
                        file_name="whole_submission", step_name=f"agent_py_first_turn_{turn + 1}",
                    )
                except Exception:
                    pass

                assistant_blocks = list(getattr(resp, "content", []) or [])
                tool_uses = [b for b in assistant_blocks if getattr(b, "type", "") == "tool_use"]
                if not tool_uses:
                    messages.append({"role": "assistant", "content": assistant_blocks})
                    messages.append({"role": "user", "content": "Call `emit_canonical_census(...)` via `run_python` and then `confirm_agent_build`."})
                    continue

                tool_results: list[dict[str, Any]] = []
                confirm_tu = None
                # Always execute all `run_python` calls FIRST so parallel tool calls never skip `emit_canonical_census`
                for tu in tool_uses:
                    t_name = getattr(tu, "name", "")
                    t_input = getattr(tu, "input", {}) or {}
                    t_id = getattr(tu, "id", "")
                    if t_name == "run_python":
                        res_text = sandbox.run(str(t_input.get("code") or ""))
                        log.info("🐍 [AGENT PYTHON-FIRST] Turn %d -> %d chars", turn + 1, len(res_text))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": t_id,
                            "content": res_text,
                        })
                    elif t_name == "confirm_agent_build":
                        confirm_tu = tu

                if confirm_tu is not None:
                    if sandbox.read_canonical_census() is not None:
                        confirmed = True
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": getattr(confirm_tu, "id", ""),
                            "content": "ERROR: canonical_census.json has NOT been emitted yet! Call emit_canonical_census(...) inside run_python first, then call confirm_agent_build.",
                        })

                if confirmed or turn == max_turns - 1:
                    break
                if turn >= max_turns - 2:
                    tool_results.append({
                        "type": "text",
                        "text": "Call `emit_canonical_census(...)` now if not yet emitted, then call `confirm_agent_build`.",
                    })
                messages.append({"role": "assistant", "content": assistant_blocks})
                messages.append({"role": "user", "content": tool_results})

            c_payload = sandbox.read_canonical_census()
            i_payload = sandbox.read_canonical_invoices()
            if c_payload is None:
                log.warning("⚠️ [AGENT PYTHON-FIRST] Completed without emitting canonical_census.json; falling back to deterministic parser.")
            c_obj = canonical_json_to_census(c_payload, source_files=src_names) if c_payload else None
            i_objs = canonical_json_to_invoices(i_payload) if i_payload else None

            if c_obj is not None:
                log.info(
                    "🤖 [AGENT PYTHON-FIRST] Built primary Census in %.2fs: %d EEs, %d Deps (invoices=%d)",
                    time.perf_counter() - t0, len(c_obj.employees), len(c_obj.dependents), len(i_objs or []),
                )
                try:
                    from caching import get_local_cache
                    get_local_cache().set(
                        prompt=cache_key_prompt,
                        model=ANTHROPIC_MODEL,
                        response_data={"census_payload": c_payload, "invoices_payload": i_payload},
                        step_name="agent_python_first_build",
                        tokens_saved_estimate=len(cache_key_prompt) // 4,
                        effort=effective_effort,
                    )
                except Exception:
                    pass
            return c_obj, (i_objs or None)
    except Exception as exc:
        log.warning("Agent-Python-First build skipped (falling back to deterministic parser): %s", exc)
        return None, None


def canonical_json_to_renewal(payload: dict[str, Any]) -> Optional[Any]:
    """Convert Python-emitted `canonical_renewal.json` into a validated `RenewalDoc`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rates"), list):
        return None
    from models import RenewalDoc
    from utils import parse_date, std_coverage, to_money

    rates: dict[tuple[str, str], float] = {}
    plans: list[str] = []
    for r in payload["rates"]:
        if not isinstance(r, dict):
            continue
        plan = clean(r.get("plan") or "")
        tier = std_coverage(r.get("tier") or "") or clean(r.get("tier") or "").upper()
        if tier == "EF":
            tier = "FAM"
        rate_val = to_money(r.get("rate"))
        if not plan or not tier or rate_val is None or rate_val <= 0:
            continue
        rates[(plan, tier)] = float(rate_val)
        if plan not in plans:
            plans.append(plan)
    if not rates:
        return None
    return RenewalDoc(
        path=clean(payload.get("file_name") or "agent_python_renewal"),
        effective=parse_date(payload.get("effective")),
        rates=rates,
        plans=plans,
    )


def run_agent_auditor_repair_build(
    docs: list[Any],
    audit_findings: list[Any],
    *,
    prospect: Any = None,
    effective_date: Any = None,
    max_turns: int = 5,
) -> tuple[Optional[Any], Optional[list[Any]], Optional[Any]]:
    """Compulsory-only closed-loop repair: triggered ONLY when `workbook_auditor.py` returns
    `overall_status == 'failed'` (Critical/High findings) that simple field patches could not fix.

    Uses the live Python sandbox (`run_python`) to inspect 100% of raw files, resolve every
    reported Critical/High finding at the root, and emit repaired canonical census, invoice,
    and/or renewal payloads in 1 bounded session (zero extra API calls on clean runs).
    """
    if not docs or not audit_findings or not _agent_loop_enabled():
        return None, None, None

    from census_config import ANTHROPIC_API_KEY, ANTHROPIC_EFFORT, ANTHROPIC_MODEL, ANTHROPIC_WORKSPACE_ID
    from llm_client import (
        get_instruction_doc_name,
        get_scoped_instructions,
        get_system_prompt,
        set_current_tracking,
    )

    set_current_tracking(file_name="whole_submission", step_name="auditor_python_repair")
    doc_name = get_instruction_doc_name()
    scoped_rules = get_scoped_instructions("blueprint")
    digest = build_submission_digest(docs)
    src_names = [getattr(d, "name", "") for d in docs if getattr(d, "name", "")]

    finding_lines = []
    for f in audit_findings[:15]:
        sev = getattr(f, "severity", "high")
        cat = getattr(f, "category", "")
        loc = getattr(f, "location", "")
        msg = getattr(f, "message", "")
        fix = getattr(f, "suggested_fix", "")
        finding_lines.append(f"- [{str(sev).upper()}] ({cat}) @ {loc}: {msg}" + (f" | Fix: {fix}" if fix else ""))
    findings_block = "\n".join(finding_lines)

    confirm_tool = {
        "name": "confirm_audit_repair",
        "description": (
            "Call ONCE after `run_python` has emitted the repaired canonical JSON(s) "
            "(`emit_canonical_census`, and if needed `emit_canonical_invoices` / `emit_canonical_renewal`) "
            "resolving all Critical/High audit findings."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "emitted_census", "emitted_invoices", "emitted_renewal"],
            "properties": {
                "summary": {"type": "string"},
                "emitted_census": {"type": "boolean"},
                "emitted_invoices": {"type": "boolean"},
                "emitted_renewal": {"type": "boolean"},
            },
        },
    }

    system_prompt = (
        get_system_prompt()
        + "\n\nYou are executing a COMPULSORY CLOSED-LOOP AUDIT REPAIR inside a live Sandboxed Python "
        "Code Interpreter (`run_python`). The post-generation auditor flagged Critical/High defects "
        "in the generated deliverables. Inspect the raw files directly in Python, fix EVERY reported "
        "defect at its source (template rows, COBRA enrollees, invalid ZIPs left blank, member-level "
        "medical invoice/BER reconciliation, and renewal rate tables), emit the corrected canonical "
        "payloads, verify every total in Python, and call `confirm_audit_repair`."
    )
    user_prompt = f"""Repair the Critical/High post-generation audit findings below for this submission under {doc_name}.

AUDIT FINDINGS TO RESOLVE:
{findings_block}

PROSPECT CONTEXT:
- Company: {getattr(prospect, 'company', '') if prospect else ''}
- Proposed Effective Date: {effective_date or ''}

SANDBOX HELPERS:
- `FILES`, `TABLES`, `TEXTS`, `list_files()`, `sheet_df()`, `column_summary()`, `search_text()`
- `emit_canonical_census({{"employees": [...], "excluded_count": N, "notes": [...]}})`
- `emit_canonical_invoices({{"invoices": [{{"file_name": ..., "period_start": ..., "period_end": ..., "current_charges": ..., "records": [...]}}]}})`
- `emit_canonical_renewal({{"file_name": ..., "effective": "YYYY-MM-DD", "rates": [{{"plan": ..., "tier": "EE|ES|EC|FAM", "rate": 123.45}}]}})`

RULES:
1. Exclude all unnumbered template/example rows and instructional prompt rows.
2. Preserve all COBRA enrollees as primary subscribers (`cobra="Y"`, `coverage="EE"`, blank DOB/ZIP if not in source).
3. Never truncate invalid ZIPs — leave invalid ZIPs blank (`""`).
4. If an invoice PDF is lump-sum (no member rows), check ALL spreadsheets in `TABLES` for a member-level medical premium sheet whose sum reconciles to the invoice medical total, and emit it via `emit_canonical_invoices(...)`.
5. If renewal is required and a renewal rate table exists in `TABLES`/`TEXTS`, emit all medical plan/tier renewal rates via `emit_canonical_renewal(...)`.

SCOPED GOVERNING RULES:
{scoped_rules}

SUBMISSION DIGEST ({len(docs)} files):
{digest}
"""

    import anthropic

    headers = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, default_headers=headers)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    effective_effort = ANTHROPIC_EFFORT or "medium"
    t0 = time.perf_counter()

    try:
        with PythonSandboxSession(docs) as sandbox:
            for turn in range(max_turns):
                resp = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=8192,
                    output_config={"effort": effective_effort},
                    system=system_prompt,
                    tools=[_RUN_PYTHON_TOOL, confirm_tool],
                    messages=messages,
                )
                try:
                    from core.universal_token_monitor import track_usage
                    track_usage(
                        resp.usage, model=ANTHROPIC_MODEL, poc_name="BENEFITS_UW",
                        file_name="whole_submission", step_name=f"auditor_py_repair_turn_{turn + 1}",
                    )
                except Exception:
                    pass

                assistant_blocks = list(getattr(resp, "content", []) or [])
                tool_uses = [b for b in assistant_blocks if getattr(b, "type", "") == "tool_use"]
                if not tool_uses:
                    messages.append({"role": "assistant", "content": assistant_blocks})
                    messages.append({"role": "user", "content": "Emit the repaired canonical JSON(s) via `run_python` and call `confirm_audit_repair`."})
                    continue

                tool_results: list[dict[str, Any]] = []
                confirmed = False
                for tu in tool_uses:
                    t_name = getattr(tu, "name", "")
                    t_input = getattr(tu, "input", {}) or {}
                    t_id = getattr(tu, "id", "")
                    if t_name == "run_python":
                        res_text = sandbox.run(str(t_input.get("code") or ""))
                        log.info("🔧 [AUDITOR PYTHON REPAIR] Turn %d -> %d chars", turn + 1, len(res_text))
                        tool_results.append({"type": "tool_result", "tool_use_id": t_id, "content": res_text})
                    elif t_name == "confirm_audit_repair":
                        confirmed = True
                        tool_results.append({"type": "tool_result", "tool_use_id": t_id, "content": "Confirmed."})

                if confirmed or turn == max_turns - 1:
                    break
                messages.append({"role": "assistant", "content": assistant_blocks})
                messages.append({"role": "user", "content": tool_results})

            c_payload = sandbox.read_canonical_census()
            i_payload = sandbox.read_canonical_invoices()
            r_payload = sandbox.read_canonical_renewal()
            c_obj = canonical_json_to_census(c_payload, source_files=src_names) if c_payload else None
            i_objs = canonical_json_to_invoices(i_payload) if i_payload else None
            r_obj = canonical_json_to_renewal(r_payload) if r_payload else None
            log.info(
                "🔧 [AUDITOR PYTHON REPAIR] Completed in %.2fs (census=%s, invoices=%d, renewal=%s)",
                time.perf_counter() - t0,
                bool(c_obj),
                len(i_objs or []),
                bool(r_obj),
            )
            return c_obj, (i_objs or None), r_obj
    except Exception as exc:
        log.warning("Auditor Python-Sandbox repair skipped: %s", exc)
        return None, None, None
