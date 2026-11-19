"""Regression: rosters that encode employee/dependent with opaque codes (e.g. 'Employee (1) or Dependent (0)').

Root cause fixed: coded relationship values ('1'/'0') were unrecognised, so every employee became a
dependent (CH) and the census exploded into false 'unassigned dependent' exceptions.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
FIXTURE = ROOT / "tests" / "fixtures" / "coded_relationship_census.xlsx"

from column_semantics import build_value_map, parse_header_legend  # noqa: E402
from utils import std_relationship  # noqa: E402


@pytest.mark.parametrize("label,expected", [
    ("*Employee (1) or Dependent (0)", {"1": "Employee", "0": "Dependent"}),
    ("Relationship (E=Employee, S=Spouse, C=Child)", {"e": "Employee", "s": "Spouse", "c": "Child"}),
    ("Member Type: 1 = Subscriber; 2 = Spouse; 3 = Child", {"1": "Subscriber", "2": "Spouse", "3": "Child"}),
])
def test_header_legend_parsing(label, expected):
    legend = parse_header_legend(label)
    for k, v in expected.items():
        assert legend.get(k, "").lower() == v.lower(), legend


def test_value_map_translates_codes_without_llm():
    vm = build_value_map("relationship", "*Employee (1) or Dependent (0)", [1, 0, 1, "1.0", None],
                         std_relationship, allow_llm=False)
    assert std_relationship(vm["1"]) == "EE"
    assert std_relationship(vm["0"]) in ("CH", "SP", "DP")


def test_recognised_values_pass_through():
    assert build_value_map("relationship", "Relationship", ["Employee", "Spouse", "Child"],
                           std_relationship, allow_llm=False) == {}


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture missing")
def test_coded_census_builds_correct_population(monkeypatch):
    monkeypatch.setenv("CENSUS_DISABLE_LLM_VALUE_MAP", "1")
    from readers import read_file
    from census_builder import build_census
    doc = read_file(FIXTURE)
    census, _ = build_census([doc])
    emps = [p for p in census.people if p.role == "EE"]
    deps = [p for p in census.people if p.role in ("SP", "CH", "DP")]
    assert len(emps) == 42, f"expected 42 employees, got {len(emps)}"
    assert len(deps) == 26, f"expected 26 dependents, got {len(deps)}"
    assert not census.unassigned_dependents
    names = {(p.first.lower(), p.last.lower()) for p in emps}
    for n in [("kathleen", "barraza"), ("carson", "blair"), ("mason", "bowdy"), ("stephen", "johnston")]:
        assert n in names
    assert all(p.dob for p in census.people), "every source record has a DOB"

    fam = {(e.first, e.last): sorted((d.role, d.first) for d in e.dependents) for e in emps if e.dependents}
    # positional (stacked) ownership beats same-surname household members
    assert fam[("Carson", "Blair")] == [("CH", "Brooks"), ("CH", "Caden"), ("SP", "Angela")]
    assert fam[("Amanda", "Lommason-Foltz")] == [("CH", "Cobie"), ("CH", "Parker")]
    assert fam[("Tyler", "Shockley")] == [("CH", "Benjamin")]
    # generic 'Dependent' resolved to SP/CH from coverage tier + age
    assert fam[("Stephen", "Johnston")] == [("SP", "Simone")]
    assert ("SP", "Stevie") in fam[("Thomas", "Dallefeld")]
    assert sum(1 for d in deps if d.role == "SP") == 6
