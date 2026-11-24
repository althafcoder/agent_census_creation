"""Regression tests for dynamic postal-grammar state parsing and carrier routing."""
import sys
from pathlib import Path

import pytest

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from models import Prospect
from readers import RawDoc
from router import determine_routing, extract_prospect_state, normalize_state


@pytest.mark.parametrize(
    "street_only",
    [
        "1111 Missouri Ave N",
        "111 Missouri Ave N",
        "500 Virginia St",
        "200 Indiana Blvd",
        "100 N Washington Dr",
        "42 Georgia Ln",
        "777 Delaware Rd",
        "123 Main St NE",
        "456 Oak Ct",
        "1111 Missouri",
    ],
)
def test_street_only_addresses_do_not_fabricate_state(street_only):
    assert normalize_state(street_only, allow_unqualified_full_name=False) is None
    assert normalize_state(street_only, allow_unqualified_full_name=True) is None


@pytest.mark.parametrize(
    "address_text,expected",
    [
        ("1111 Missouri Ave N, Largo, FL 33770", "FL"),
        ("1111 Missouri Ave N.Largo FL", "FL"),
        ("500 Virginia Ave, Tampa, Florida 33602", "FL"),
        ("500 Virginia Ave, Tampa, Florida", "FL"),
        ("200 Indiana Blvd, Austin, TX 78701", "TX"),
        ("100 Washington Dr, Indiana, PA 15701", "PA"),
        ("50 Main St, Delaware, OH 43015", "OH"),
        ("300 Oak Rd, Kansas City, MO 64101", "MO"),
        ("900 Oceanfront, Virginia Beach, VA 23451", "VA"),
        ("400 Pine St, Wyoming, MI 49509", "MI"),
        ("123 Main St, Hartford, CT", "CT"),
        ("123 Main St Hartford CT 06103", "CT"),
        ("ISLANDIA NY 11749-1401", "NY"),
    ],
)
def test_full_addresses_with_state_named_streets_or_cities_resolve_accurately(address_text, expected):
    assert normalize_state(address_text, allow_unqualified_full_name=False) == expected
    assert normalize_state(address_text, allow_unqualified_full_name=True) == expected


@pytest.mark.parametrize(
    "ambiguous_city",
    [
        "Kansas City",
        "Virginia Beach",
        "Colorado Springs",
        "Texas City",
    ],
)
def test_ambiguous_city_names_without_state_return_none(ambiguous_city):
    assert normalize_state(ambiguous_city, allow_unqualified_full_name=True) is None


def test_dedicated_state_field_accepts_full_state_names_and_codes():
    assert normalize_state("FL", allow_unqualified_full_name=True) == "FL"
    assert normalize_state("Florida", allow_unqualified_full_name=True) == "FL"
    assert normalize_state("Missouri", allow_unqualified_full_name=True) == "MO"
    assert normalize_state("New York", allow_unqualified_full_name=True) == "NY"
    # Street-address mode rejects a bare state name with no postal structure (e.g. OCR fragment)
    assert normalize_state("Missouri", allow_unqualified_full_name=False) is None


def test_mcgill_plumbing_florida_street_named_missouri_routes_to_florida_blue():
    prospect = Prospect(
        company="McGill Plumbing, Inc",
        address="1111 Missouri Ave N",
        city="Largo",
        state="FL",
        city_state_zip="Largo, FL 33770",
    )
    ghq = RawDoc(
        path=Path("10-26-26 McGill Plumbing General & Health Questionnaire.pdf"),
        ext=".pdf",
        text="Client Name: McGill Plumbing\nState: FL\nStreet Address: Missouri\nCity & State: Largo Zip 33770\n",
    )
    ghq.role = "ghq"
    rfp = RawDoc(
        path=Path("11-3-26 McGill Plumbing Revised data sheet.pdf"),
        ext=".pdf",
        text="Company Name: McGill Plumbing\nMailing Address: 111 Missouri Ave N\nLocations: 1111 Missouri Ave N.Largo FL\n",
    )
    rfp.role = "rfp"

    state, conflicting, detail = extract_prospect_state(prospect, [ghq, rfp])
    assert state == "FL"
    assert not conflicting
    assert detail == ""

    routing = determine_routing(prospect, [ghq, rfp])
    assert routing.state == "FL"
    assert routing.is_florida
    assert routing.carrier == "Florida Blue"
    assert not routing.state_conflicting
    assert not routing.state_missing


def test_non_florida_street_named_florida_routes_to_cigna():
    prospect = Prospect(
        company="Apex Services LLC",
        address="250 Florida Ave NW",
        city="Washington",
        state="DC",
        city_state_zip="Washington, DC 20001",
    )
    routing = determine_routing(prospect, [])
    assert routing.state == "DC"
    assert not routing.is_florida
    assert routing.carrier == "Cigna"
    assert not routing.state_conflicting


def test_genuine_state_conflict_still_withheld_per_section_4_2():
    prospect = Prospect(
        company="Dual State Corp",
        address="300 Oak Rd, Kansas City, MO 64101",
        state="FL",
        city_state_zip="Largo, FL 33770",
    )
    ghq = RawDoc(
        path=Path("GHQ.pdf"),
        ext=".pdf",
        text="State: FL\n",
    )
    ghq.role = "ghq"
    state, conflicting, detail = extract_prospect_state(prospect, [ghq])
    assert state == ""
    assert conflicting
    assert "FL" in detail and "MO" in detail

    routing = determine_routing(prospect, [ghq])
    assert routing.state_conflicting
    assert routing.carrier == ""
