"""
Dynamic column value-code resolution (Fix #1 - Column Semantics).

Source rosters frequently encode a field with opaque codes instead of words, e.g.
``*Employee (1) or Dependent (0)`` -> values ``1`` / ``0``, or
``Relationship (E=Employee, S=Spouse, C=Child)`` -> values ``E`` / ``S`` / ``C``.

The deterministic normalizers (``std_relationship``, ``std_gender``, ``std_coverage``)
only understand words, so such codes used to fall through as "unrecognised" and every
employee was converted into a dependent.

This module resolves codes WITHOUT hardcoding any carrier/template/column specifics:
  1. Values the normalizer already understands are kept as-is.
  2. A code legend is parsed from the source header text itself
     (``Word (code)`` and ``code = Word`` styles).
  3. Any values still unresolved are sent (header + distinct values only, never the
     full roster) to the LLM, which returns a code -> meaning mapping. Python then
     applies the mapping to every row, preserving 100% tabular fidelity.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ``Employee (1)`` / ``Dependent (0)`` / ``Spouse (S)``
_WORD_THEN_CODE = re.compile(r"([A-Za-z][A-Za-z\s/&\-]*?)\s*[\(\[]\s*([A-Za-z0-9]{1,3})\s*[\)\]]")
# ``E=Employee`` / ``S - Spouse`` / ``C: Child``
_CODE_THEN_WORD = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{1,3})\s*[=:\-]\s*([A-Za-z][A-Za-z\s/&]*?)(?=\s*(?:[,;|/)\]]|\bor\b|\band\b|$))", re.I)
_CONNECTORS = re.compile(r"^(?:\W|\bor\b|\band\b|\bis\b|\bfor\b)+", re.I)


def _clean(v) -> str:
    s = "" if v is None else str(v).strip()
    if s.lower() in ("nan", "none", "nat"):
        return ""
    # Excel often renders integer codes as floats (1.0 / 0.0)
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s


def parse_header_legend(label: str) -> dict[str, str]:
    """Extract a ``{code_lower: meaning}`` legend embedded in a source header label."""
    text = str(label or "").replace("*", " ")
    legend: dict[str, str] = {}
    for word, code in _WORD_THEN_CODE.findall(text):
        meaning = _CONNECTORS.sub("", word).strip()
        meaning = meaning.split()[-1] if meaning and len(meaning.split()) > 3 else meaning
        if meaning and code:
            legend.setdefault(code.lower(), meaning)
    for code, word in _CODE_THEN_WORD.findall(text):
        meaning = word.strip()
        if meaning and code and code.lower() not in legend and len(code) < len(meaning):
            legend[code.lower()] = meaning
    return legend


def _llm_enabled() -> bool:
    if os.getenv("CENSUS_DISABLE_LLM_VALUE_MAP", "").strip().lower() in ("1", "true", "yes"):
        return False
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return bool(key) and not key.startswith("sk-ant-test")


def build_value_map(
    field: str,
    header_label: str,
    values: list,
    normalizer: Callable[[str], str],
    allow_llm: bool = True,
    min_unresolved_share: float = 0.2,
) -> dict[str, str]:
    """Return ``{raw_value_lower: meaning_text}`` for values the normalizer cannot read directly.

    ``meaning_text`` is a word the normalizer understands (e.g. 'Employee', 'Dependent').
    Only codes needing translation are included, so recognised values pass through untouched.
    """
    cleaned = [_clean(v) for v in values]
    non_empty = [v for v in cleaned if v]
    if not non_empty:
        return {}
    distinct = sorted({v for v in non_empty}, key=str.lower)
    unresolved = [v for v in distinct if not normalizer(v)]
    if not unresolved:
        return {}

    vmap: dict[str, str] = {}
    legend = parse_header_legend(header_label)
    for v in unresolved:
        meaning = legend.get(v.lower())
        if meaning and normalizer(meaning):
            vmap[v.lower()] = meaning

    still = [v for v in unresolved if v.lower() not in vmap]
    unresolved_rows = sum(1 for v in non_empty if v in still)
    if (still and allow_llm and _llm_enabled()
            and unresolved_rows / max(1, len(non_empty)) >= min_unresolved_share):
        try:
            from llm_client import infer_column_value_codes
            llm_map = infer_column_value_codes(field, header_label, distinct[:40]) or {}
            for v in still:
                meaning = _clean(llm_map.get(v) or llm_map.get(v.lower()))
                if meaning and normalizer(meaning):
                    vmap[v.lower()] = meaning
        except Exception as exc:  # LLM is a best-effort enhancer, never a hard dependency
            log.warning("LLM value-code inference skipped for '%s': %s", header_label, exc)

    if vmap:
        log.info("Column '%s' (%s): resolved value codes %s", header_label, field, vmap)
    return vmap


def resolve(value, vmap: Optional[dict[str, str]]):
    """Translate a raw cell through a value map (no-op if unmapped)."""
    if not vmap:
        return value
    key = _clean(value).lower()
    return vmap.get(key, value) if key else value


def infer_table_schema(
    df,
    hdr_row: int,
    raw_labels: list[str],
    allowed_fields: list[str],
    file_name: str | None = None,
) -> tuple[dict[str, int], dict[str, dict[str, str]]]:
    """Ask the LLM to remap ambiguous census columns & coded values using header + sample rows.

    Returns ``(columns_override, value_maps_override)``. Safe no-op when LLM is disabled.
    """
    if not _llm_enabled() or df is None or df.empty or hdr_row + 1 >= len(df):
        return {}, {}
    sample_df = df.iloc[hdr_row + 1: min(len(df), hdr_row + 9)]
    sample_rows = [[_clean(v) for v in row] for row in sample_df.values.tolist()]
    try:
        from llm_client import infer_table_column_mapping
        res = infer_table_column_mapping(raw_labels, sample_rows, allowed_fields, file_name=file_name) or {}
    except Exception as exc:
        log.warning("LLM table schema remap skipped: %s", exc)
        return {}, {}

    allowed_set = set(allowed_fields)
    n_cols = df.shape[1]
    cols_out: dict[str, int] = {}
    for fld, idx in (res.get("columns") or {}).items():
        if fld in allowed_set:
            try:
                ci = int(idx)
            except (TypeError, ValueError):
                continue
            if 0 <= ci < n_cols:
                cols_out[fld] = ci

    from utils import std_coverage, std_gender, std_relationship
    norms = {"relationship": std_relationship, "gender": std_gender, "coverage": std_coverage}
    vmaps_out: dict[str, dict[str, str]] = {}
    for fld, raw_map in (res.get("value_maps") or {}).items():
        if fld not in norms or not isinstance(raw_map, dict):
            continue
        norm = norms[fld]
        cleaned_map: dict[str, str] = {}
        for k, meaning in raw_map.items():
            ck = _clean(k).lower()
            cm = _clean(meaning)
            if ck and cm and norm(cm):
                cleaned_map[ck] = cm
        if cleaned_map:
            vmaps_out[fld] = cleaned_map

    if cols_out or vmaps_out:
        log.info("LLM schema remap produced cols=%s value_maps=%s", cols_out, vmaps_out)
    return cols_out, vmaps_out

