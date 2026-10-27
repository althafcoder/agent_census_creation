"""Shared dataclasses used across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

Note = tuple[str, bool]          # (text, is_red)


@dataclass
class Person:
    role: str                                  # EE | SP | CH
    first: str = ""
    last: str = ""
    gender: str = ""
    dob: Optional[date] = None
    zip: str = ""
    coverage: str = ""                         # employees only: EE, ES, EC, FAM, WO, WP, NE, RC
    cobra: str = ""
    plan: str = ""
    premium: Optional[float] = None
    sub_id: str = ""
    hire_date: Optional[date] = None
    effective_date: Optional[date] = None
    eligibility_class: str = ""                # raw benefit eligibility classification (authoritative)
    worker_category: str = ""                  # raw worker category (full-time/part-time/temp)
    position_status: str = ""                  # raw position/employment status (e.g. Active)
    waive_reason: str = ""                     # raw medical waive reason (evidence of a genuine waiver)
    job_title: str = ""                        # raw job title / position description
    salary: str = ""                           # raw salary / compensation description
    source: str = ""
    rel_raw: str = ""
    cov_raw: str = ""
    record_id: str = ""                        # Stable unique record ID within the submission
    subscriber_id: str = ""                    # Explicit link to subscriber's record_id
    eligibility_status: str = ""               # ENROLLED, WAITING_PERIOD, WAIVED, NOT_ELIGIBLE, REFUSED
    employee: Optional["Person"] = None        # dependents -> their employee
    dependents: list["Person"] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    invoice_only: bool = False
    is_duplicate: bool = False                 # second occurrence of an employee in the same file
    benefit_roster: bool = False               # employee appears in a benefit-eligibility/plan roster
    row_no: int = 0                            # "Data Row" number assigned at write time
    raw_data: dict = field(default_factory=dict) # Raw extracted values and source locations

    def note(self, text: str, red: bool = False, orange: bool = False) -> None:
        color = "red" if red else ("orange" if orange else "black")
        if not any(t == text for t, _ in self.notes):
            self.notes.append((text, color))

    @property
    def display(self) -> str:
        return f"{self.first} {self.last}".strip() or "(no name)"

    @property
    def relationship(self) -> str:
        return self.role


@dataclass
class InvoiceRecord:
    first: str = ""
    last: str = ""
    raw_name: str = ""                         # exact invoice spelling
    tier: str = ""
    dob: Optional[date] = None
    gender: str = ""
    zip: str = ""
    plan: str = ""                             # normalized plan ID
    raw_plan: str = ""                         # original extracted plan string
    premium: Optional[float] = None            # current monthly premium (recurring)
    current_premium: Optional[float] = None    # recurring monthly premium
    retro_amount: Optional[float] = None       # retroactive adjustments
    retro_code: str = ""                       # ADD, TRM, CHG, etc.
    retro_period: str = ""                     # e.g. "9/01-9/30/2026"
    service_period: str = ""                   # e.g. "10/01/2026 - 10/31/2026"
    total_amount: Optional[float] = None       # line total (current + retro)
    status: str = ""
    sub_id: str = ""
    dep_count: int = 0
    dep_rels: set = field(default_factory=set)
    has_dep_detail: bool = False
    source: str = ""
    raw_data: dict = field(default_factory=dict)


@dataclass
class InvoiceDoc:
    path: str
    records: list[InvoiceRecord]
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    entity: str = "default"
    revised: bool = False
    draft: bool = False
    number: str = ""                            # statement / invoice number found in the document
    doc_date: Optional[date] = None              # statement / invoice date found in the document
    current_charges: Optional[float] = None      # total current monthly charges
    total_adjustments: Optional[float] = None    # total adjustments / retro
    total_due: Optional[float] = None            # total balance due
    retro_adjustments: list[dict] = field(default_factory=list)


@dataclass
class RenewalDoc:
    path: str
    effective: Optional[date] = None
    termination: Optional[date] = None
    version_rank: int = 0                       # Final/Revised higher
    rates: dict = field(default_factory=dict)   # (plan_name, tier) -> premium
    plans: list[str] = field(default_factory=list)
    age_banded: bool = False


@dataclass
class Prospect:
    effective_date: Optional[date] = None
    sic: str = ""
    company: str = ""
    address: str = ""
    city_state_zip: str = ""
    referral: str = ""
    sales_person: str = ""
    state: str = ""
    city: str = ""
    peo_name: str = ""
    is_peo: bool = False
    funding_type: str = ""
    current_carrier: str = ""
    ein: str = ""
    salesforce_id: str = ""
    dba: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    ft_employees: Optional[int] = None          # employer-declared full-time employee count

    @property
    def zip(self) -> str:
        import re
        m = re.search(r"\b(\d{5}(?:-\d{4})?)\b", self.city_state_zip)
        return m.group(1) if m else ""


@dataclass
class Census:
    people: list[Person] = field(default_factory=list)      # employees + dependents in output order
    raw_employee_rows: int = 0
    raw_dependent_rows: int = 0
    cross_file_merges: int = 0
    dependent_merges: int = 0
    duplicate_employees: int = 0
    unassigned_dependents: list[Person] = field(default_factory=list)
    files_used: list[str] = field(default_factory=list)
    census_notes: list[str] = field(default_factory=list)

    @property
    def employees(self) -> list[Person]:
        return [p for p in self.people if p.role == "EE"]

    @property
    def dependents(self) -> list[Person]:
        return [p for p in self.people if p.role != "EE"]


@dataclass
class RowResult:
    inv: Optional[InvoiceRecord] = None
    inv_name: str = ""
    plan: str = ""
    tier: str = ""
    status: str = ""
    inv_premium: Optional[float] = None
    renewal_premium: Optional[float] = None
    match_pct: Optional[float] = None
    match_text: str = ""
    result: str = ""
    result_red: bool = False
    notes: list[Note] = field(default_factory=list)


@dataclass
class Findings:
    """Document-level facts that feed the Summary sheet."""
    notes: list[str] = field(default_factory=list)
    exceptions: list[tuple[str, str, str]] = field(default_factory=list)   # (category, who, detail)
    counters: dict = field(default_factory=dict)
    invoice_period: str = ""
    renewal_effective: Optional[date] = None
    renewal_unmapped_plans: set = field(default_factory=set)
    plan_rates: dict = field(default_factory=dict)       # (plan, tier) -> invoice rate
    renewal_rates: dict = field(default_factory=dict)    # (plan, tier) -> renewal rate
    plan_enrollment: dict = field(default_factory=dict)  # (plan, tier) -> count
    wp_stats: dict = field(default_factory=dict)
    invoice_employee_count: int = 0
    invoice_medical_count: int = 0
    invoices_received: str = ""
    selected_invoice: str = ""
    # Premium reconciliation metrics
    census_matched_premium: Optional[float] = None
    invoice_current_charges: Optional[float] = None
    invoice_only_difference: Optional[float] = None
    invoice_retro_adjustments: Optional[float] = None
    invoice_total_balance_due: Optional[float] = None
    reconciliation_items: list[tuple[str, float]] = field(default_factory=list)
    # Renewal status
    renewal_required: bool = False
    renewal_reason: str = ""
    # Routing, carrier, and email deliverables
    carrier: str = ""
    state: str = ""
    routing_notes: list[str] = field(default_factory=list)
    sbc_notes: list[str] = field(default_factory=list)
    claims_notes: list[str] = field(default_factory=list)
    renewal_notes: list[str] = field(default_factory=list)
    email_text: str = ""
