"""Fill the approved RAPT template (columns A-L untouched in structure) and add the Summary sheet."""
from __future__ import annotations

from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import census_config as C
from comparator import tier_from_deps
from models import Census, Findings, Prospect, RowResult

THIN = Side(style="thin", color="FFBFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HDR_FILL = PatternFill("solid", fgColor=C.BLUE_HEADER)
SEC_FILL = PatternFill("solid", fgColor="FFD9E1F2")
GREEN_FILL = PatternFill("solid", fgColor=C.GREEN_HEADER_FILL)


# ================================================================ census ====
def _copy_style(src, dst):
    dst.font, dst.border, dst.fill = copy(src.font), copy(src.border), copy(src.fill)
    dst.alignment, dst.number_format, dst.protection = copy(src.alignment), src.number_format, copy(src.protection)


def _rich_notes(notes: list, base: Font):
    """One cell, several notes - red ones in red font, orange ones in orange font."""
    if not notes:
        return None
    norm = []
    for item in notes:
        t, c = item[0], item[1]
        if c is True or c == "red":
            color = C.RED
        elif c == "orange" or "verify dependent eligibility" in str(t):
            color = C.ORANGE
        else:
            color = "FF000000"
        norm.append((t, color))

    colors = {col for _, col in norm}
    if len(colors) == 1:
        single_color = list(colors)[0]
        return "; ".join(t for t, _ in norm), single_color

    parts = []
    for k, (t, col) in enumerate(norm):
        txt = t + ("; " if k < len(norm) - 1 else "")
        parts.append(TextBlock(InlineFont(rFont=base.name, sz=base.sz, b=(col != "FF000000"), color=col), txt))
    return CellRichText(*parts), None


def _set_notes(cell, notes, base):
    out = _rich_notes(notes, base)
    if out is None:
        return
    val, single_color = out
    cell.value = val
    if single_color and single_color != "FF000000":
        cell.font = Font(name=base.name, sz=base.sz, bold=True, color=single_color)


def write_census_sheet(ws, census: Census, prospect: Prospect, results: dict[int, RowResult]) -> None:
    ws.title = "Census"
    # ---- prospect block (values go to the top-left cell of each merged value range)
    cells = C.PROSPECT_CELLS
    if prospect.effective_date:
        ws[cells["effective_date"]].value = prospect.effective_date
        ws[cells["effective_date"]].number_format = "mm/dd/yyyy"
    for key in ("sic", "company", "address", "city_state_zip", "referral", "sales_person"):
        v = getattr(prospect, key)
        if v:
            ws[cells[key]].value = v

    # ---- comparison headers (after column L only); green fill on header row only
    hdr_src = ws.cell(C.HEADER_ROW, 11)       # K14 style donor (blue header)
    for k, title in enumerate(C.COMPARISON_HEADERS):
        col = C.COMPARISON_START_COL + k
        c = ws.cell(C.HEADER_ROW, col, title)
        _copy_style(hdr_src, c)
        c.fill = GREEN_FILL
        ws.column_dimensions[get_column_letter(col)].width = C.COMPARISON_WIDTHS[k]
    ws.column_dimensions["L"].width = max(ws.column_dimensions["L"].width or 0, 30)

    body_src = {col: ws.cell(C.FIRST_DATA_ROW, col) for col in range(1, 13)}
    base_font = copy(body_src[2].font)
    tc = C.TEMPLATE_COLS

    # ---- clear template body rows (they are empty, but be safe) then write
    row = C.FIRST_DATA_ROW
    n = 0
    for p in census.people:
        n += 1
        p.row_no = n
    for p in census.people:
        r = row
        row += 1
        for col in range(1, 13):
            cell = ws.cell(r, col)
            if not cell.has_style or cell.border.left.style is None:
                _copy_style(body_src[col], cell)
        for col in range(13, 22):
            _copy_style(body_src[12], ws.cell(r, col))
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="center",
                                                  horizontal="left" if col in (13, 14, 19, 20, 21) else "center")

        ws.cell(r, tc["data_row"], p.row_no)
        ws.cell(r, tc["first"], p.first or None)
        ws.cell(r, tc["last"], p.last or None)
        ws.cell(r, tc["gender"], p.gender or None)
        if p.dob:
            ws.cell(r, tc["dob"], p.dob).number_format = "mm/dd/yyyy"
        if p.zip:
            z = ws.cell(r, tc["zip"], p.zip)
            z.number_format = "@"
        ws.cell(r, tc["relationship"], p.role)
        if p.role != "EE" and p.employee is not None:
            ws.cell(r, tc["dep_of"], p.employee.row_no or None)
        ws.cell(r, tc["coverage"], p.coverage or None)
        ws.cell(r, tc["cobra"], p.cobra or None)
        if p.role == "EE":
            ws.cell(r, tc["plan"], p.plan or None)
            if p.premium is not None:
                ws.cell(r, tc["premium"], p.premium).number_format = '#,##0.00'

        rr = results.get(id(p))
        notes = list(p.notes)
        if rr:
            notes = notes + rr.notes
            base = C.COMPARISON_START_COL  # Col M (13)
            ws.cell(r, base, rr.inv_name or None)                                # Col M (13): Invoice Employee Name
            m = ws.cell(r, base + 1, rr.match_text or None)                      # Col N (14): Match %
            if rr.match_text == "100% Match":
                m.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.GREEN)
            elif rr.match_pct is not None:
                colour = C.GREEN if rr.match_pct >= 100 else (C.ORANGE if rr.match_pct >= 80 else C.RED)
                m.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=colour)
            ws.cell(r, base + 2, rr.plan or None)                                # Col O (15): Invoice Plan
            ws.cell(r, base + 3, rr.tier or None)                                # Col P (16): Invoice Tier
            ws.cell(r, base + 4, rr.status or None)                              # Col Q (17): Invoice Status
            if rr.inv_premium is not None:
                ws.cell(r, base + 5, rr.inv_premium).number_format = '#,##0.00'  # Col R (18): Invoice Premium
            if rr.renewal_premium is not None:
                ws.cell(r, base + 6, rr.renewal_premium).number_format = '#,##0.00' # Col S (19): Renewal Premium
            res = ws.cell(r, base + 7, rr.result or None)                        # Col T (20): Comparison Result
            if rr.result == "Match":
                res.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.GREEN)
            elif rr.result in ("Eligibility Conflict", "Missing From Census") or rr.result_red:
                res.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.RED)
        _set_notes(ws.cell(r, C.COMPARISON_START_COL + 8), notes, base_font)     # Col U (21): Notes


# ================================================================ summary ===
class _Sheet:
    def __init__(self, ws):
        self.ws, self.r = ws, 1

    def title(self, text):
        c = self.ws.cell(self.r, 1, text)
        c.font = Font(name="Segoe UI", sz=14, bold=True, color="FF1F3864")
        self.r += 2

    def section(self, text, width=6):
        for col in range(1, width + 1):
            self.ws.cell(self.r, col).fill = SEC_FILL
        c = self.ws.cell(self.r, 1, text)
        c.font = Font(name="Segoe UI", sz=12, bold=True)
        self.r += 1

    def header(self, labels):
        for i, lab in enumerate(labels, 1):
            c = self.ws.cell(self.r, i, lab)
            c.fill, c.border = HDR_FILL, BOX
            c.font = Font(name="Segoe UI", sz=10, bold=True, color="FFFFFFFF")
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        self.r += 1

    def row(self, vals, red=False, fmt=None):
        for i, v in enumerate(vals, 1):
            c = self.ws.cell(self.r, i, v)
            c.border = BOX
            c.font = Font(name="Segoe UI", sz=10, color=C.RED if red else "FF000000")
            c.alignment = Alignment(wrap_text=True, vertical="top", horizontal="left" if i == 1 else "center")
            if fmt and i in fmt and isinstance(v, (int, float)):
                c.number_format = fmt[i]
        self.r += 1

    def line(self, text, red=False):
        c = self.ws.cell(self.r, 1, text)
        c.font = Font(name="Segoe UI", sz=10, color=C.RED if red else "FF000000")
        self.r += 1

    def gap(self):
        self.r += 1


def write_summary_sheet(wb, census: Census, prospect: Prospect, F: Findings, audit: dict) -> None:
    ws = wb.create_sheet("Summary")
    s = _Sheet(ws)
    cnt = F.counters
    s.title("Census Comparison Summary")

    # ---- Findings Summary
    s.section("Findings Summary")
    s.header(["Metric", "Value"])
    eff = prospect.effective_date.strftime("%m/%d/%Y") if prospect.effective_date else ""
    census_emps = [p for p in census.employees if not p.invoice_only and not p.is_duplicate]
    enrolled = sum(1 for p in census_emps if p.coverage in C.ACTIVE_TIERS)
    by_status = {}
    for p in census_emps:
        if p.coverage and p.coverage not in C.ACTIVE_TIERS:
            by_status[p.coverage] = by_status.get(p.coverage, 0) + 1
    rows = [
        ("Prospect Name", prospect.company),
        ("Effective Date", eff),
        ("Census Employees", audit["out_employees"]),
        ("   Enrolled in Medical", enrolled),
    ]
    rows += [(f"   Waived ({code})" if code == "WO" else f"   Not Enrolled ({code})", n) for code, n in sorted(by_status.items())]
    rows += [
        ("Census Dependents", audit["out_dependents"]),
        ("Invoice Employees (all benefit lines)", F.invoice_employee_count),
        ("Invoice Employees with Medical", F.invoice_medical_count),
        ("Invoices Received", F.invoices_received),
        ("Selected Invoice", F.selected_invoice),
        ("Selected Invoice Period", F.invoice_period),
        ("Renewal Effective Date", F.renewal_effective.strftime("%m/%d/%Y") if F.renewal_effective else ""),
        ("Number of Missing Census Employees", cnt.get("missing_census", 0)),
        ("Number of Missing Invoice Employees", cnt.get("missing_invoice", 0)),
        ("Number of Tier Mismatches", cnt.get("tier_mismatch", 0)),
        ("Number of Coverage Mismatches", cnt.get("coverage_mismatch", 0) + cnt.get("coverage_status_mismatch", 0)
         + cnt.get("dependent_mismatch", 0)),
        ("Number of Eligibility Conflicts", cnt.get("eligibility_conflict", 0)),
        ("Number of Dependents Without Subscriber Match", len(census.unassigned_dependents)),
        ("Number of Premium Discrepancies", cnt.get("premium_discrepancy", 0)),
        ("Number of Employees Using Most Used Plan Logic", cnt.get("most_used", 0)),
        ("Number of Employees Missing Renewal Rates", cnt.get("missing_renewal", 0)),
    ]
    for k, v in rows:
        s.row([k, v])
    s.gap()
    if F.notes:
        s.section("Document-Level Findings")
        for n in F.notes:
            s.line(n, red=False)
        s.gap()

    # ---- Plan level tables + premium change
    plans = sorted({p for (p, _t) in list(F.plan_enrollment) + list(F.plan_rates)})
    tot_cur = tot_ren = 0.0
    tot_enrolled = 0
    for plan in plans:
        s.section(f"Plan: {plan or '(Plan name not specified)'}")
        s.header(["Tier", "Enrollment Count", "Invoice Premium", "Renewal Premium"])
        plan_total = 0
        for t in C.ACTIVE_TIERS:
            n = F.plan_enrollment.get((plan, t), 0)
            cur, ren = F.plan_rates.get((plan, t)), F.renewal_rates.get((plan, t))
            plan_total += n
            tot_cur += n * (cur or 0)
            tot_ren += n * (ren or 0)
            s.row([t, n, cur, ren], fmt={3: "#,##0.00", 4: "#,##0.00"})
        s.row(["Total", plan_total, None, None])
        tot_enrolled += plan_total
        s.gap()
        s.header(["Tier", "Current Premium", "Renewal Premium", "Difference", "% Change"])
        for t in C.ACTIVE_TIERS:
            cur, ren = F.plan_rates.get((plan, t)), F.renewal_rates.get((plan, t))
            diff = pct = None
            if cur is not None and ren is not None:
                diff = round(ren - cur, 2)
                pct = round(diff / cur, 4) if cur else None
            s.row([t, cur, ren, diff, pct], fmt={2: "#,##0.00", 3: "#,##0.00", 4: "#,##0.00", 5: "0.0%"})
        s.gap()

    if plans:
        s.section("Total Monthly Medical Premium (census-enrolled employees)")
        s.header(["Enrolled Employees", "Current Monthly", "Renewal Monthly", "Difference", "% Change"])
        diff = round(tot_ren - tot_cur, 2) if tot_ren else None
        pct = round(diff / tot_cur, 4) if (diff is not None and tot_cur) else None
        s.row([tot_enrolled, round(tot_cur, 2), round(tot_ren, 2) if tot_ren else None, diff, pct],
              fmt={2: "#,##0.00", 3: "#,##0.00", 4: "#,##0.00", 5: "0.0%"})
        s.gap()

    # ---- Census audit
    s.section("Census Audit")
    s.header(["Metric", "Count"])
    for k, v in [("Source Employees", audit["src_employees"]), ("Output Employees", audit["out_employees"]),
                 ("Source Dependents", audit["src_dependents"]), ("Output Dependents", audit["out_dependents"]),
                 ("Missing Employees", audit["missing_employees"]),
                 ("Added Invoice Employees", cnt.get("missing_census", 0)),
                 ("Duplicate Employees", census.duplicate_employees),
                 ("Unassigned Dependents", len(census.unassigned_dependents))]:
        s.row([k, v], red=(k in {"Duplicate Employees", "Unassigned Dependents", "Missing Employees"} and v > 0))
    s.gap()

    # ---- Dependent audit summary
    s.section("Dependent Audit Summary")
    s.header(["Metric", "Count"])
    for k, v in [("Employee Count from Source Census", audit["src_employees"]),
                 ("Dependent Count from Source Census", audit["src_dependents"]),
                 ("Employee Count in Final Census", audit["out_employees"]),
                 ("Dependent Count in Final Census", audit["out_dependents"]),
                 ("Unassigned Dependents", len(census.unassigned_dependents)),
                 ("Missing Subscriber Records", len(census.unassigned_dependents))]:
        s.row([k, v])
    s.gap()

    # ---- Waiting period
    s.section("Waiting Period Employees")
    s.header(["Metric", "Count"])
    wp = F.wp_stats
    wp_total = sum(1 for e in census.employees if e.coverage == "WP")
    for k, v in [("Total WP Employees", wp_total),
                 ("WP Employees With Elected Plan", wp.get("elected", 0)),
                 ("WP Employees Using Most Used Plan Logic", wp.get("most_used", 0)),
                 ("WP Employees Missing Plan Election", wp.get("missing_election", 0))]:
        s.row([k, v])
    s.gap()

    # ---- Renewal mapping failures + exceptions
    if F.renewal_unmapped_plans:
        s.section("Renewal Plan Match Not Identified")
        for pl in sorted(F.renewal_unmapped_plans):
            s.line(f"Enrolled plan with no confident renewal match: {pl}", red=True)
        s.gap()

    s.section("Exception Report")
    cats = ["Employees missing from invoice", "Employees missing from census", "Tier mismatches",
            "Coverage mismatches", "Invalid dependent structures", "Premium discrepancies",
            "Renewal mapping failures", "Eligibility conflicts", "Duplicate employees",
            "Cross-document conflicts"]
    if not F.exceptions:
        s.line("No exceptions identified.")
    for cat in cats:
        items = [e for e in F.exceptions if e[0] == cat]
        if not items:
            continue
        s.header([f"{cat} ({len(items)})", "Employee", "Detail"])
        for _, who, detail in items:
            s.row(["", who, detail], red=True)
        s.gap()

    ws.column_dimensions["A"].width = 52
    for col, w in zip("BCDE", (34, 22, 22, 14)):
        ws.column_dimensions[col].width = w


# ================================================================ Marsh & SBU ===
def write_marsh_sheet(ws, census: Census, prospect: Prospect, results: dict[int, RowResult]) -> None:
    from datetime import datetime
    # 1. Group info block (yellow fields in rows 4-7)
    today_str = datetime.now().strftime("%m/%d/%Y")
    ws["B4"].value = today_str
    if prospect.effective_date:
        ws["E4"].value = prospect.effective_date.strftime("%m/%d/%Y")
    if prospect.sales_person:
        ws["H4"].value = prospect.sales_person
    if prospect.company:
        ws["B5"].value = prospect.company
    ws["E5"].value = len([e for e in census.employees if not e.invoice_only and not e.is_duplicate])
    if prospect.current_carrier:
        ws["H5"].value = prospect.current_carrier
    if prospect.address:
        ws["B6"].value = prospect.address
    if prospect.city_state_zip:
        ws["E6"].value = prospect.city_state_zip
    if prospect.peo_name:
        ws["H6"].value = prospect.peo_name
    if prospect.sic:
        ws["B7"].value = prospect.sic
    if prospect.salesforce_id:
        ws["E7"].value = prospect.salesforce_id
    if prospect.referral:
        ws["H7"].value = prospect.referral

    # 2. Comparison headers at row 12 starting Column 15 (Column O)
    hdr_src = ws.cell(12, 10)  # Col J style donor
    for k, title in enumerate(C.COMPARISON_HEADERS):
        col = C.MARSH_COMPARISON_START_COL + k
        c = ws.cell(C.MARSH_HEADER_ROW, col, title)
        _copy_style(hdr_src, c)
        c.fill = GREEN_FILL
        ws.column_dimensions[get_column_letter(col)].width = C.COMPARISON_WIDTHS[k]

    body_src = {col: ws.cell(C.MARSH_FIRST_DATA_ROW, col) for col in range(1, 11)}
    base_font = copy(body_src[2].font)

    # 3. Populate census rows starting at row 14
    row = C.MARSH_FIRST_DATA_ROW
    n = 0
    for p in census.people:
        n += 1
        p.row_no = n

    for p in census.people:
        r = row
        row += 1
        for col in range(1, 11):
            cell = ws.cell(r, col)
            if not cell.has_style or cell.border.left.style is None:
                _copy_style(body_src[col], cell)
        for col in range(C.MARSH_COMPARISON_START_COL, C.MARSH_COMPARISON_START_COL + len(C.COMPARISON_HEADERS)):
            _copy_style(body_src[10], ws.cell(r, col))
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="center",
                                                  horizontal="left" if col in (15, 16, 21, 22, 23) else "center")

        ws.cell(r, 1, p.row_no)
        ws.cell(r, 2, p.first or None)
        ws.cell(r, 3, p.last or None)
        ws.cell(r, 4, p.gender or None)
        if p.dob:
            ws.cell(r, 5, p.dob).number_format = "mm/dd/yyyy"
        if p.zip:
            z = ws.cell(r, 6, p.zip)
            z.number_format = "@"

        # Member Type: EE/SP/CH/DP
        rel = p.role
        if rel == "EE":
            m_type = "EE"
        elif rel == "SP":
            m_type = "SP"
        elif rel == "DP":
            m_type = "DP"
        else:
            m_type = "CH"
        ws.cell(r, 7, m_type)

        # Medical Coverage: EE/ES/EC/EF/WO/WP/OC/RC/NE (FAM -> EF)
        cov = (p.coverage or "").strip().upper()
        if cov in ("FAM", "FAMILY"):
            cov = "EF"
        ws.cell(r, 8, cov or None)
        ws.cell(r, 9, p.cobra or ("N" if p.role in ("SP", "CH") or cov == "WO" else "N"))
        plan = p.plan or ("NA" if cov == "WO" else None)
        ws.cell(r, 10, plan)

        # Comparison columns
        rr = results.get(id(p))
        notes = list(p.notes)
        if rr:
            notes = notes + rr.notes
            base = C.MARSH_COMPARISON_START_COL  # Col O (15)
            ws.cell(r, base, rr.inv_name or None)
            m = ws.cell(r, base + 1, rr.match_text or None)
            if rr.match_text == "100% Match":
                m.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.GREEN)
            elif rr.match_pct is not None:
                colour = C.GREEN if rr.match_pct >= 100 else (C.ORANGE if rr.match_pct >= 80 else C.RED)
                m.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=colour)
            ws.cell(r, base + 2, rr.plan or None)
            ws.cell(r, base + 3, rr.tier or None)
            ws.cell(r, base + 4, rr.status or None)
            if rr.inv_premium is not None:
                ws.cell(r, base + 5, rr.inv_premium).number_format = '#,##0.00'
            if rr.renewal_premium is not None:
                ws.cell(r, base + 6, rr.renewal_premium).number_format = '#,##0.00'
            res = ws.cell(r, base + 7, rr.result or None)
            if rr.result == "Match":
                res.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.GREEN)
            elif rr.result in ("Eligibility Conflict", "Missing From Census") or rr.result_red:
                res.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.RED)
        _set_notes(ws.cell(r, C.MARSH_COMPARISON_START_COL + 8), notes, base_font)

    # Clear pre-populated row numbers in unused rows
    for r in range(row, 520):
        if ws.cell(r, 1).value is not None:
            ws.cell(r, 1).value = None


def write_sbu_sheets(wb, census: Census, prospect: Prospect) -> None:
    ws_census = wb["Census"]
    body_src = {col: ws_census.cell(C.SBU_FIRST_DATA_ROW, col) for col in range(1, 14)}
    row = C.SBU_FIRST_DATA_ROW

    for p in census.people:
        r = row
        row += 1
        for col in range(1, 14):
            cell = ws_census.cell(r, col)
            if not cell.has_style or cell.border.left.style is None:
                _copy_style(body_src[col], cell)

        ws_census.cell(r, 1, p.first or None)
        ws_census.cell(r, 2, p.last or None)

        # Type: Employee, Spouse, Partner, Child
        if p.role == "EE":
            t_val = "Employee"
        elif p.role == "SP":
            t_val = "Spouse"
        elif p.role == "DP":
            t_val = "Partner"
        else:
            t_val = "Child"
        ws_census.cell(r, 3, t_val)

        if p.dob:
            ws_census.cell(r, 4, p.dob).number_format = "mm/dd/yyyy"

        # Biological Sex: M / F
        ws_census.cell(r, 5, p.gender or None)

        # Home Zip Code
        if p.zip:
            z = ws_census.cell(r, 6, p.zip[:5])
            z.number_format = "@"

        # Waived: Y / N
        if p.role == "EE":
            is_w = p.coverage in ("WO", "RC", "NE") or "waiv" in (p.cov_raw or "").lower()
            ws_census.cell(r, 7, "Y" if is_w else "N")
        else:
            sub = p.employee
            is_w = (sub and sub.coverage in ("WO", "RC", "NE")) if sub else False
            ws_census.cell(r, 7, "Y" if is_w else "N")

        if p.hire_date:
            ws_census.cell(r, 12, p.hire_date).number_format = "mm/dd/yyyy"

    # Company Info sheet - map supported prospect values onto the template's existing label cells
    if "Company Info" in wb.sheetnames:
        ws_co = wb["Company Info"]
        values = _company_info_values(prospect, census)
        _fill_labeled_fields(ws_co, values)


def _company_info_values(prospect: Prospect, census: Census) -> dict:
    """Dynamic label -> value map for the Company Info sheet (labels matched, not cell refs)."""
    return {
        "legalcompanyname": prospect.company,
        "dbaifapplicable": prospect.dba,
        "dba": prospect.dba,
        "eintaxid": prospect.ein,
        "ein": prospect.ein,
        "siccode": prospect.sic,
        "maincompanyaddress": prospect.address,
        "citystatezip": prospect.city_state_zip,
        "address": prospect.address,
        "contactname": prospect.contact_name,
        "contactemail": prospect.contact_email,
        "contactphone": prospect.contact_phone,
        "desiredeffectivedate": prospect.effective_date.strftime("%m/%d/%Y") if prospect.effective_date else "",
        "numberoffulltimeemployees": (prospect.ft_employees if prospect.ft_employees else
                                      len([p for p in census.employees if not getattr(p, "invoice_only", False)])),
    }


def _fill_labeled_fields(ws, values: dict) -> None:
    """Populate column B of a label sheet by matching the normalized label text in column A.
    Preserves template structure/formatting and leaves genuinely unsupported fields blank."""
    from utils import norm_key
    for row in range(1, ws.max_row + 1):
        label = ws.cell(row, 1).value
        if label is None:
            continue
        key = norm_key(str(label).rstrip(":"))
        if key in values and values[key] not in (None, ""):
            ws.cell(row, 2).value = values[key]


def save_rapt_workbook(template: Path, out: Path, census, prospect, results, F, audit):
    wb = openpyxl.load_workbook(template)
    write_census_sheet(wb.active, census, prospect, results)
    write_summary_sheet(wb, census, prospect, F, audit)
    wb.save(out)
    return out


def save_marsh_workbook(template: Path, out: Path, census, prospect, results, F, audit):
    wb = openpyxl.load_workbook(template)
    ws = wb["Group Info + Census"] if "Group Info + Census" in wb.sheetnames else wb.active
    write_marsh_sheet(ws, census, prospect, results)
    write_summary_sheet(wb, census, prospect, F, audit)
    wb.save(out)
    return out


def save_sbu_workbook(template: Path, out: Path, census, prospect):
    wb = openpyxl.load_workbook(template)
    write_sbu_sheets(wb, census, prospect)
    wb.save(out)
    return out


def save_workbook(template: Path, out: Path, census, prospect, results, F, audit, carrier: str = ""):
    t_name = str(template).lower()
    if carrier == C.CARRIER_CIGNA or "marsh" in t_name:
        return save_marsh_workbook(template, out, census, prospect, results, F, audit)
    else:
        return save_rapt_workbook(template, out, census, prospect, results, F, audit)

