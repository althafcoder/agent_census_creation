"""
Sandboxed Python Code Interpreter for the Claude Underwriting Agent.

Gives Claude Copilot-level data-analysis power over 100% of uploaded spreadsheets,
PDFs, and generated Excel workbooks without token truncation or regex blind spots:
  - Pre-loads all parsed RawDoc tables (`TABLES`), extracted texts (`TEXTS`), and
    read-only file paths (`FILES`) into an isolated temporary session directory.
  - Enforces AST-level import/call safety checks (blocks network, shell, destructive
    filesystem operations, and credential/env access).
  - Runs each snippet in an isolated subprocess with a strict wall-clock timeout
    and stdout/stderr size cap.
"""
from __future__ import annotations

import ast
import logging
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("census.python_sandbox")

FORBIDDEN_MODULES: frozenset[str] = frozenset({
    "subprocess", "socket", "urllib", "http", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
    "ctypes", "cffi", "multiprocessing", "concurrent", "threading",
    "shutil", "webbrowser", "importlib", "pty", "fcntl", "signal",
    "winreg", "msvcrt", "anthropic", "openai", "boto3", "paramiko",
    "pickle", "marshal", "shelve", "code", "runpy", "venv", "pip",
})

FORBIDDEN_BUILTINS: frozenset[str] = frozenset({
    "eval", "exec", "compile", "__import__", "breakpoint", "input",
})

FORBIDDEN_ATTR_CALLS: frozenset[str] = frozenset({
    "system", "popen", "spawnl", "spawnv", "execv", "execl",
    "unlink", "rmdir", "removedirs",
    "chmod", "chown", "kill", "fork", "startfile", "rmtree",
    "write_text", "write_bytes",
})

# Attributes that are dangerous ONLY on `os` (not on str/DataFrame/list/set!)
FORBIDDEN_OS_CALLS: frozenset[str] = frozenset({
    "remove", "rename", "replace",
})


def validate_sandbox_code(code: str) -> Optional[str]:
    """AST-validate Python code before sandbox execution.

    Returns ``None`` if safe, or a human-readable error message if blocked.
    """
    if not code or not code.strip():
        return "ERROR: Empty Python code."
    if len(code) > 25_000:
        return "ERROR: Code exceeds 25,000 character limit."
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError on line {exc.lineno}: {exc.msg}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                if root in FORBIDDEN_MODULES:
                    return f"SECURITY BLOCK: Importing '{root}' is not permitted in the data-analysis sandbox."
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_MODULES:
                return f"SECURITY BLOCK: Importing from '{root}' is not permitted in the data-analysis sandbox."
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_BUILTINS:
                    return f"SECURITY BLOCK: Calling '{node.func.id}()' is not permitted in the sandbox."
                if node.func.id == "open":
                    # Block write/append/exclusive modes on open()
                    mode_val = None
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        mode_val = str(node.args[1].value)
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode_val = str(kw.value.value)
                    if mode_val and any(ch in mode_val for ch in ("w", "a", "x", "+")):
                        return f"SECURITY BLOCK: File write mode '{mode_val}' is not permitted in the read-only sandbox."
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_ATTR_CALLS:
                    return f"SECURITY BLOCK: Calling '.{node.func.attr}()' is not permitted in the sandbox."
                if (
                    node.func.attr in FORBIDDEN_OS_CALLS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                ):
                    return f"SECURITY BLOCK: Calling 'os.{node.func.attr}()' is not permitted in the sandbox."
        elif isinstance(node, ast.Attribute):
            if node.attr == "environ":
                return "SECURITY BLOCK: Accessing environment variables is not permitted in the sandbox."
    return None


_RUNNER_TEMPLATE = r'''
import collections
import csv
import datetime
import difflib
import io
import json
import math
import os
import pickle
import re
import statistics
from decimal import Decimal
from pathlib import Path

import openpyxl
import pandas as pd

with open("context.pkl", "rb") as _f:
    _CTX = pickle.load(_f)

FILES = _CTX["files"]       # {filename: absolute_path}
TABLES = _CTX["tables"]     # {filename: {sheet_name: pd.DataFrame}}
TEXTS = _CTX["texts"]       # {filename: extracted_text}


def _match_file(file_name: str, container: dict):
    if file_name in container:
        return file_name
    low = str(file_name or "").strip().lower()
    for k in container:
        if k.lower() == low:
            return k
    for k in container:
        if low and low in k.lower():
            return k
    raise KeyError(f"File '{file_name}' not found. Available: {list(container.keys())}")


def list_files():
    """Print summary of all uploaded files, worksheet shapes, and text lengths."""
    for fn in sorted(set(list(FILES.keys()) + list(TABLES.keys()) + list(TEXTS.keys()))):
        tmap = TABLES.get(fn, {})
        tlen = len(TEXTS.get(fn, "") or "")
        sheets = ", ".join(f"{sn}({df.shape[0]}x{df.shape[1]})" for sn, df in tmap.items())
        print(f"- {fn} | sheets=[{sheets}] | text_chars={tlen}")


def sheet_df(file_name: str, sheet_name: str = None, header_row: int = None) -> pd.DataFrame:
    """Return a DataFrame for an uploaded spreadsheet sheet.

    If header_row (0-based) is provided, promotes that row to column names and returns rows below it.
    """
    fn = _match_file(file_name, TABLES)
    tmap = TABLES[fn]
    if not tmap:
        raise ValueError(f"No parsed tables in '{fn}'")
    if sheet_name is None:
        df = next(iter(tmap.values())).copy()
    else:
        low = sheet_name.strip().lower()
        key = next((k for k in tmap if k.lower() == low), None)
        if key is None:
            key = next((k for k in tmap if low in k.lower()), None)
        if key is None:
            raise KeyError(f"Sheet '{sheet_name}' not found in '{fn}'. Available: {list(tmap.keys())}")
        df = tmap[key].copy()
    if header_row is not None and 0 <= header_row < len(df):
        raw_cols = [str(v).strip() if pd.notna(v) else f"col_{i}" for i, v in enumerate(df.iloc[header_row].tolist())]
        # Deduplicate column names cleanly
        seen = {}
        cols = []
        for c in raw_cols:
            if c in seen:
                seen[c] += 1
                cols.append(f"{c}_{seen[c]}")
            else:
                seen[c] = 0
                cols.append(c)
        df = df.iloc[header_row + 1:].reset_index(drop=True)
        df.columns = cols
    return df


def column_summary(file_name: str, sheet_name: str = None, header_row: int = 0, top_n: int = 12):
    """Print every column's non-null count and top distinct values (Copilot-style profiling)."""
    df = sheet_df(file_name, sheet_name=sheet_name, header_row=header_row)
    print(f"=== {file_name} | sheet={sheet_name or '(first)'} | rows={len(df)} | cols={len(df.columns)} ===")
    for ci, col in enumerate(df.columns):
        ser = df[col].dropna().astype(str).str.strip()
        ser = ser[ser != ""]
        vc = ser.value_counts().head(top_n).to_dict()
        print(f"  [{ci}] {col!r} (non-empty={len(ser)}, unique={ser.nunique()}): {vc}")


def search_text(pattern: str, file_name: str = None, max_hits: int = 15):
    """Regex search across extracted text of one or all documents."""
    targets = [_match_file(file_name, TEXTS)] if file_name else sorted(TEXTS.keys())
    rx = re.compile(pattern, re.I)
    hits = 0
    for fn in targets:
        txt = TEXTS.get(fn) or ""
        for ln_no, line in enumerate(txt.splitlines(), 1):
            if rx.search(line):
                print(f"[{fn}:L{ln_no}] {line[:220]}")
                hits += 1
                if hits >= max_hits:
                    return
    if hits == 0:
        print(f"No matches for /{pattern}/")


def emit_canonical_census(payload):
    """Write a canonical census JSON payload (from Python) to the session's output slot.

    Enforces universal pre-commit structural invariants (no header/blank-name rows,
    automatic ES->SP and FAM adult->SP role resolution for generic dependents, and
    subscriber tier inheritance) before accepting the payload.
    """
    if isinstance(payload, list):
        payload = {"employees": payload, "excluded_count": 0, "notes": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("employees"), list):
        raise ValueError("emit_canonical_census expects a dict with an 'employees' list (or a list of employees).")

    _BAD_HEADER_TOKENS = {
        "firstname", "lastname", "fullname", "employeename", "censusname", "censusrow",
        "payrollname", "code", "description", "headcount", "annualwages", "weeklygross",
        "total", "subtotal", "grandtotal", "required", "none", "nan", "null",
    }
    _VALID_COV = {"EE", "ES", "EC", "FAM", "EF", "WO", "WP", "NE", "RC"}

    def _parse_yr(d_val):
        if not d_val:
            return None
        s = str(d_val).strip()
        m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
        return int(m.group(1)) if m else None

    cleaned_emps = []
    for idx, emp in enumerate(payload["employees"]):
        if not isinstance(emp, dict):
            continue
        ef = str(emp.get("first") or emp.get("first_name") or "").strip()
        el = str(emp.get("last") or emp.get("last_name") or "").strip()
        ef_k = re.sub(r"[^a-z0-9]", "", ef.lower())
        el_k = re.sub(r"[^a-z0-9]", "", el.lower())
        if not ef_k or not el_k or ef_k in _BAD_HEADER_TOKENS or el_k in _BAD_HEADER_TOKENS:
            raise ValueError(
                f"VALIDATION ERROR in emit_canonical_census at employee[{idx}] ({ef!r} {el!r}): "
                f"Blank, missing, or table-header/metadata name token detected. Filter out header/legend/blank rows before calling emit_canonical_census."
            )
        cov = str(emp.get("coverage") or emp.get("tier") or "").strip().upper()
        if cov == "EF":
            cov = "FAM"
        emp["first"] = ef
        emp["last"] = el
        emp["coverage"] = cov if cov in _VALID_COV else ("EE" if emp.get("plan") else "WO")
        emp["cobra"] = "Y" if str(emp.get("cobra") or "").strip().upper() in ("Y", "YES", "TRUE", "1") else "N"

        raw_deps = [d for d in (emp.get("dependents") or []) if isinstance(d, dict)]
        valid_deps = []
        for d_idx, dep in enumerate(raw_deps):
            df = str(dep.get("first") or dep.get("first_name") or "").strip()
            dl = str(dep.get("last") or dep.get("last_name") or "").strip()
            df_k = re.sub(r"[^a-z0-9]", "", df.lower())
            dl_k = re.sub(r"[^a-z0-9]", "", dl.lower())
            if not df_k or not dl_k or df_k in _BAD_HEADER_TOKENS or dl_k in _BAD_HEADER_TOKENS:
                raise ValueError(
                    f"VALIDATION ERROR in emit_canonical_census at employee[{idx}] dependent[{d_idx}] ({df!r} {dl!r}): "
                    f"Blank or table-header dependent name detected."
                )
            drole = str(dep.get("role") or dep.get("relationship") or "").strip().upper()
            dep["first"] = df
            dep["last"] = dl
            dep["role"] = drole
            dep["coverage"] = emp["coverage"]
            dep["cobra"] = emp["cobra"]
            valid_deps.append(dep)

        # Universal Tier <-> Dependent Role Invariant:
        # 1. If subscriber is ES and has 1 dependent, that dependent must be SP (or DP).
        if emp["coverage"] == "ES" and len(valid_deps) == 1 and valid_deps[0]["role"] not in ("SP", "DP"):
            d_yr = _parse_yr(valid_deps[0].get("dob"))
            if d_yr is None or (2026 - d_yr) >= 18:
                valid_deps[0]["role"] = "SP"
        # 2. If subscriber is FAM and has no SP/DP yet, promote the adult closest in age to the employee to SP.
        elif emp["coverage"] == "FAM" and valid_deps and not any(d["role"] in ("SP", "DP") for d in valid_deps):
            e_yr = _parse_yr(emp.get("dob"))
            adult_deps = [d for d in valid_deps if (_parse_yr(d.get("dob")) is not None and (2026 - _parse_yr(d.get("dob"))) >= 19)]
            if adult_deps:
                best_sp = min(adult_deps, key=lambda d: abs(_parse_yr(d.get("dob")) - (e_yr or _parse_yr(d.get("dob")))))
                if e_yr is None or abs(_parse_yr(best_sp.get("dob")) - e_yr) <= 22:
                    best_sp["role"] = "SP"
        for d in valid_deps:
            if d["role"] not in ("SP", "CH", "DP"):
                d["role"] = "CH"
        emp["dependents"] = valid_deps
        cleaned_emps.append(emp)

    payload["employees"] = cleaned_emps

    def _default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return str(o)
    with open("canonical_census.json", "w", encoding="utf-8") as _out:
        json.dump(payload, _out, default=_default, ensure_ascii=False)
    emps = payload["employees"]
    deps = sum(len(e.get("dependents") or []) for e in emps if isinstance(e, dict))
    sp_cnt = sum(1 for e in emps for d in (e.get("dependents") or []) if d.get("role") in ("SP", "DP"))
    ch_cnt = deps - sp_cnt
    cov_counts = collections.Counter(e.get("coverage", "") for e in emps)
    print(
        f"EMITTED canonical_census.json: {len(emps)} employees, {deps} dependents (SP={sp_cnt}, CH={ch_cnt}), "
        f"tiers={dict(cov_counts)}, excluded_count={payload.get('excluded_count', 0)}"
    )


def emit_canonical_invoices(payload):
    """Write Python-verified canonical medical invoice data to the session's output slot.

    Expected shape:
      {
        "invoices": [
          {
            "file_name": "Oct_Invoice.pdf",
            "period_start": "2026-10-01", "period_end": "2026-10-31",
            "group_number": "14179816", "current_charges": 19511.64,
            "total_adjustments": 0.0, "total_due": 19511.64,
            "records": [
              {"first": "Jane", "last": "Doe", "raw_name": "DOE, JANE", "plan": "AETNA EPO",
               "tier": "EE", "premium": 650.00, "sub_id": "12345", "group_no": "001"}
            ]
          }
        ]
      }
    """
    if isinstance(payload, list):
        payload = {"invoices": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("invoices"), list):
        raise ValueError("emit_canonical_invoices expects a dict with an 'invoices' list.")
    def _default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return str(o)
    with open("canonical_invoices.json", "w", encoding="utf-8") as _out:
        json.dump(payload, _out, default=_default, ensure_ascii=False)
    invs = payload["invoices"]
    recs = sum(len(inv.get("records") or []) for inv in invs if isinstance(inv, dict))
    print(f"EMITTED canonical_invoices.json: {len(invs)} invoice(s), {recs} medical subscriber record(s)")


with open("agent_script.py", "r", encoding="utf-8") as _sf:
    _AGENT_CODE = _sf.read()

exec(compile(_AGENT_CODE, "agent_script.py", "exec"))
'''


class PythonSandboxSession:
    """Isolated, read-only Python data-analysis session for a single submission run."""

    def __init__(
        self,
        docs: list[Any],
        *,
        extra_files: Optional[dict[str, str]] = None,
        timeout_seconds: float = 15.0,
        max_output_chars: int = 12_000,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self._tmpdir = tempfile.mkdtemp(prefix="census_py_sandbox_")
        self._closed = False

        files_map: dict[str, str] = {}
        tables_map: dict[str, dict[str, Any]] = {}
        texts_map: dict[str, str] = {}

        for d in docs or []:
            name = str(getattr(d, "name", "") or Path(str(getattr(d, "path", ""))).name)
            if not name:
                continue
            raw_path = getattr(d, "working_path", None) or getattr(d, "path", None)
            if raw_path and os.path.exists(str(raw_path)):
                files_map[name] = str(Path(str(raw_path)).resolve())
            tmap: dict[str, Any] = {}
            for tbl in (getattr(d, "tables", None) or []):
                df = getattr(tbl, "df", None)
                tname = str(getattr(tbl, "name", "") or f"Sheet_{len(tmap) + 1}")
                if df is not None:
                    tmap[tname] = df
            if tmap:
                tables_map[name] = tmap
            txt = getattr(d, "text", None) or ""
            if txt:
                texts_map[name] = txt

        for k, v in (extra_files or {}).items():
            if v and os.path.exists(str(v)):
                abs_v = str(Path(str(v)).resolve())
                files_map[str(k)] = abs_v
                # Also pre-load extra .xlsx/.xls workbooks (e.g. generated Marsh/RAPT/SBU deliverables)
                # into TABLES so `sheet_df(wb_name)` works seamlessly in Post-Generation Audit!
                if abs_v.lower().endswith((".xlsx", ".xls")):
                    try:
                        import openpyxl as _oxl
                        import pandas as _pd
                        _wb = _oxl.load_workbook(abs_v, data_only=True)
                        _etmap: dict[str, Any] = {}
                        for _sn in _wb.sheetnames:
                            _ws = _wb[_sn]
                            _rows = list(_ws.iter_rows(values_only=True))
                            _etmap[_sn] = _pd.DataFrame(_rows)
                        if _etmap:
                            tables_map[str(k)] = _etmap
                        _wb.close()
                    except Exception as _ex_err:
                        log.debug("Could not pre-load extra workbook '%s' into TABLES: %s", k, _ex_err)

        ctx_path = os.path.join(self._tmpdir, "context.pkl")
        with open(ctx_path, "wb") as f:
            pickle.dump({"files": files_map, "tables": tables_map, "texts": texts_map}, f)

        runner_path = os.path.join(self._tmpdir, "runner.py")
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(_RUNNER_TEMPLATE)

    def run(self, code: str) -> str:
        """Validate and execute Python code in the isolated subprocess sandbox."""
        if self._closed:
            return "ERROR: Sandbox session is already closed."
        err = validate_sandbox_code(code)
        if err:
            return err

        script_path = os.path.join(self._tmpdir, "agent_script.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Strip sensitive API keys from child environment
        safe_env = {
            k: v for k, v in os.environ.items()
            if not any(secret in k.upper() for secret in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL"))
        }
        safe_env["PYTHONIOENCODING"] = "utf-8"

        try:
            proc = subprocess.run(
                [sys.executable, "runner.py"],
                cwd=self._tmpdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=safe_env,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            parts = []
            if stdout.strip():
                parts.append(stdout.rstrip())
            if stderr.strip():
                parts.append(f"[STDERR]\n{stderr.rstrip()}")
            if not parts:
                parts.append("(no output printed — use print(...) to display results)")
            combined = "\n".join(parts)
            if len(combined) > self.max_output_chars:
                combined = combined[:self.max_output_chars] + f"\n... [truncated {len(combined) - self.max_output_chars} chars]"
            if proc.returncode != 0:
                combined = f"[EXIT CODE {proc.returncode}]\n{combined}"
            return combined
        except subprocess.TimeoutExpired:
            return f"ERROR: Python execution timed out after {self.timeout_seconds:.0f}s. Simplify the script and avoid heavy loops."
        except Exception as exc:
            return f"ERROR: Sandbox execution failed: {exc}"

    def read_canonical_census(self) -> Optional[dict[str, Any]]:
        """Read back the JSON emitted via `emit_canonical_census(...)` inside this session, if any."""
        p = os.path.join(self._tmpdir, "canonical_census.json")
        if not os.path.exists(p):
            return None
        try:
            import json
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("Failed reading canonical_census.json from sandbox: %s", exc)
            return None

    def read_canonical_invoices(self) -> Optional[dict[str, Any]]:
        """Read back the JSON emitted via `emit_canonical_invoices(...)` inside this session, if any."""
        p = os.path.join(self._tmpdir, "canonical_invoices.json")
        if not os.path.exists(p):
            return None
        try:
            import json
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("Failed reading canonical_invoices.json from sandbox: %s", exc)
            return None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "PythonSandboxSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
