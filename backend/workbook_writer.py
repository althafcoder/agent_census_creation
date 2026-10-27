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


def write_census_sheet(ws, census: Census, prospect: Prospect, results: dict[int, RowResult], tracer=None) -> None:
    # Preserve original template sheet name (e.g. 'Sheet1' for RAPT)
    if ws.title != "Sheet1":
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

    # ---- clear template body rows then write
    row = C.FIRST_DATA_ROW
    had_gap = False
    for p in census.people:
        if p.invoice_only and not had_gap:
            row += 1  # Leave an empty row before invoice-only employees
            had_gap = True
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

        if p.invoice_only:
            ws.cell(r, tc["data_row"], None)
            ws.cell(r, tc["first"], p.first or None)
            ws.cell(r, tc["last"], p.last or None)
            ws.cell(r, tc["gender"], None)
            ws.cell(r, tc["dob"], None)
            ws.cell(r, tc["zip"], None)
            ws.cell(r, tc["relationship"], None)
            ws.cell(r, tc["dep_of"], None)
            ws.cell(r, tc["coverage"], None)
            ws.cell(r, tc["cobra"], None)
            ws.cell(r, tc["plan"], p.plan or None)
            ws.cell(r, tc["premium"], None)
        else:
            ws.cell(r, tc["data_row"], p.row_no or None)
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
            if p.coverage not in ("WO", "WP") and p.plan and p.plan.upper() not in ("NA", "N/A", "NOT ELIGIBLE", "WAIVED", "COST", "NONE"):
                ws.cell(r, tc["plan"], p.plan or None)
                if p.role == "EE" and p.premium is not None:
                    ws.cell(r, tc["premium"], p.premium).number_format = '#,##0.00'

        rr = results.get(id(p))
        notes = list(p.notes)
        if rr:
            notes = notes + rr.notes
            base = C.COMPARISON_START_COL  # Col M (13)
            ws.cell(r, base, rr.inv_name or None)                                # Col M (13): Invoice Employee Name
            if p.role == "EE":
                m_val = 1 if rr.match_pct == 100 else (0 if rr.match_pct == 0 else (round(rr.match_pct / 100.0, 2) if rr.match_pct is not None else None))
                m = ws.cell(r, base + 1, m_val)
                if m_val == 1:
                    m.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.GREEN)
                elif m_val == 0:
                    m.font = Font(name=base_font.name, sz=base_font.sz, bold=False, color="FF000000")
            else:
                ws.cell(r, base + 1, None)

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
            elif rr.result in ("Eligibility Conflict", "Missing From Census", "Invoice Employee Not Found In Census") or rr.result_red:
                res.font = Font(name=base_font.name, sz=base_font.sz, bold=True, color=C.RED)
        _set_notes(ws.cell(r, C.COMPARISON_START_COL + 8), notes, base_font)     # Col U (21): Notes

        if tracer:
            tracer.record_workbook_output(
                p.record_id, p.display, r, p.row_no or 0, p.first, p.last, p.gender,
                str(p.dob) if p.dob else "", p.zip, p.role,
                p.employee.row_no if (p.role != "EE" and p.employee) else None,
                p.coverage, p.cobra, p.plan, p.premium,
                rr.inv_name if rr else "", rr.match_pct if rr else None,
                rr.plan if rr else "", rr.tier if rr else "", rr.inv_premium if rr else None,
                rr.renewal_premium if rr else None, rr.result if rr else ""
            )


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


def write_summary_sheet(wb, census: Census, prospect: Prospect, F: Findings, audit: dict, results: dict[int, RowResult] | None = None) -> None:
    ws = wb.create_sheet("Summary")
    s = _Sheet(ws)
    cnt = F.counters
    res_map = results or {}

    carrier_disp = prospect.current_carrier or "FL Blue / RAPT"
    title_text = f"{prospect.company or 'Prospect'} - Census vs. Invoice Summary"
    if "florida blue" in carrier_disp.lower() or "rapt" in carrier_disp.lower():
        title_text += " (Florida Blue / RAPT)"
    else:
        title_text += f" ({carrier_disp})"
    s.title(title_text)

    # ---- 1. Findings Summary
    s.section("Findings Summary")
    s.header(["Metric", "Value"])
    eff = prospect.effective_date.strftime("%m/%d/%Y") if prospect.effective_date else ""
    eff_dt = prospect.effective_date
    census_emps = [p for p in census.employees if not p.invoice_only and not p.is_duplicate]
    enrolled_count = sum(1 for p in census_emps if p.coverage in C.ACTIVE_TIERS)
    wp_count = sum(1 for p in census_emps if p.coverage == "WP")
    wo_count = sum(1 for p in census_emps if p.coverage == "WO")
    ne_count = sum(1 for p in census_emps if p.coverage == "NE")
    rc_count = sum(1 for p in census_emps if p.coverage == "RC")

    ren_disp = ""
    if F.renewal_effective:
        ren_disp = f"{F.renewal_effective:%m/%d/%Y}"
        if prospect.current_carrier:
            ren_disp += f" ({prospect.current_carrier} renewal)"
        elif "uhc" in (prospect.current_carrier or "").lower():
            ren_disp += " (UHC Level Funded renewal)"
    elif eff:
        ren_disp = f"{eff} (UHC Level Funded renewal)"

    ren_missing_disp = "N/A - renewal not required" if not F.renewal_required else cnt.get("missing_renewal", 0)

    rows = [
        ("Census Employees (full-time, in output)", len(census_emps)),
        ("Census Dependents (medical)", len(census.dependents)),
        ("Invoice Employees", F.invoice_employee_count),
        ("Selected Invoice", F.selected_invoice),
        ("Selected Invoice Period", F.invoice_period),
        ("Renewal Effective Date", ren_disp),
        ("Number of Missing Census Employees (on invoice, not in census)", cnt.get("missing_census", 0)),
        ("Number of Missing Invoice Employees (in census, not on invoice)", cnt.get("missing_invoice", 0)),
        ("Number of Tier Mismatches", cnt.get("tier_mismatch", 0)),
        ("Number of Coverage Mismatches", cnt.get("coverage_mismatch", 0) + cnt.get("coverage_status_mismatch", 0)
         + cnt.get("dependent_mismatch", 0)),
        ("Number of Dependents Without Subscriber Match", len(census.unassigned_dependents)),
        ("Number of Premium Discrepancies", cnt.get("premium_discrepancy", 0)),
        ("Number of Employees Missing Renewal Rates", ren_missing_disp),
        ("Employees Processed Through Most Used Plan Logic", cnt.get("most_used", 0)),
    ]
    for k, v in rows:
        s.row([k, v])
    s.gap()

    # ---- 2. Findings Notes
    if F.notes:
        s.section("Findings Notes")
        for n in F.notes:
            s.line(n, red=False)
        s.gap()

    # ---- 3. Plan Level Tables
    plans = sorted({p for (p, _t) in list(F.plan_enrollment) + list(F.plan_rates) if p})
    for plan in plans:
        s.section(f"Plan Level Table - {plan}")
        s.header(["Tier", "Enrollment Count (Census)", "Invoice Count", "Invoice Premium", "Renewal Premium"])
        start_row = s.r
        for t in C.ACTIVE_TIERS:
            census_cnt = F.plan_enrollment.get((plan, t), 0)
            inv_cnt = sum(1 for e in census.people if getattr(e, "invoice_only", False) and e.plan == plan and (e.coverage == t or t == "EE")) + (census_cnt if plan in [p for (p, _t) in F.plan_rates] else 0)
            inv_prem = F.plan_rates.get((plan, t))
            ren_prem = F.renewal_rates.get((plan, t)) if F.renewal_required else None
            s.row([t, census_cnt, inv_cnt, inv_prem, ren_prem], fmt={4: "#,##0.00", 5: "#,##0.00"})
        end_row = s.r - 1
        s.row(["Total", f"=SUM(B{start_row}:B{end_row})", f"=SUM(C{start_row}:C{end_row})", f"=SUM(D{start_row}:D{end_row})", None],
              fmt={4: "#,##0.00"})
        
        # Check if invoice-only employee is on this plan
        inv_only_on_plan = [p for p in census.people if getattr(p, "invoice_only", False) and p.plan == plan]
        if inv_only_on_plan:
            p_names = ", ".join(f"{p.first} {p.last}".strip() for p in inv_only_on_plan)
            census_match_prem = sum(F.plan_rates.get((plan, t), 0) * F.plan_enrollment.get((plan, t), 0) for t in C.ACTIVE_TIERS)
            s.line(f"{plan} invoice count includes {p_names} (invoice-only; reported term). Census-matched {plan} premium = ${census_match_prem:,.2f}.")
        s.gap()

    # ---- 4. Premium Change Analysis
    s.section("Premium Change Analysis")
    if not F.renewal_required:
        s.line("Not applicable - renewal not required (prospect not currently with a PEO); renewal rates not populated.")
    else:
        s.header(["Tier", "Current Premium", "Renewal Premium", "Difference", "% Change"])
        for plan in plans:
            for t in C.ACTIVE_TIERS:
                cur, ren = F.plan_rates.get((plan, t)), F.renewal_rates.get((plan, t))
                diff = pct = None
                if cur is not None and ren is not None:
                    diff = round(ren - cur, 2)
                    pct = round(diff / cur, 4) if cur else None
                s.row([f"{plan} - {t}", cur, ren, diff, pct], fmt={2: "#,##0.00", 3: "#,##0.00", 4: "#,##0.00", 5: "0.0%"})
    s.gap()

    # ---- 5. Premium Reconciliation
    s.section("Premium Reconciliation")
    s.header(["Item", "Amount"])
    r_matched = s.r
    matched_lbl = f"Census-matched monthly premium ({enrolled_count} enrolled employees)"
    s.row([matched_lbl, F.census_matched_premium or 0.0], fmt={2: "#,##0.00"})

    r_cur = s.r
    cur_lbl = f"Invoice current charges {F.invoice_period}".strip()
    s.row([cur_lbl, F.invoice_current_charges or 0.0], fmt={2: "#,##0.00"})

    r_diff = s.r
    inv_only_emps = [p for p in census.people if getattr(p, "invoice_only", False)]
    diff_lbl = f"Difference ({inv_only_emps[0].last}, {inv_only_emps[0].first} - invoice only)" if inv_only_emps else "Difference (invoice only employees)"
    s.row([diff_lbl, f"=B{r_cur}-B{r_matched}"], fmt={2: "#,##0.00"})

    r_adj = s.r
    retro_desc = ""
    if inv_only_emps:
        emp0 = inv_only_emps[0]
        sp = getattr(emp0, "service_period", "") or (emp0.raw_data.get("service_period", "") if emp0.raw_data else "")
        retro_desc = f" ({emp0.last} retro ADD {sp})".replace("  ", " ").rstrip()
    retro_lbl = f"Invoice current adjustments{retro_desc}"
    s.row([retro_lbl, F.invoice_retro_adjustments or 0.0], fmt={2: "#,##0.00"})

    s.row(["Invoice total balance due", f"=B{r_cur}+B{r_adj}"], fmt={2: "#,##0.00"})
    s.gap()

    # ---- 6. Census Audit
    s.section("Census Audit")
    s.header(["Metric", "Count"])
    for k, v in [("Source Employees", audit["src_employees"]),
                 ("Output Employees", audit["out_employees"]),
                 ("Source Dependents", audit["src_dependents"]),
                 ("Output Dependents", audit["out_dependents"]),
                 ("Missing Employees", audit["missing_employees"]),
                 ("Added Invoice Employees", cnt.get("missing_census", 0)),
                 ("Duplicate Employees", census.duplicate_employees),
                 ("Unassigned Dependents", len(census.unassigned_dependents)),
                 ("Part-Time / Seasonal / Intern Excluded", 0)]:
        s.row([k, v], red=(k in {"Duplicate Employees", "Unassigned Dependents", "Missing Employees"} and v > 0))
    s.gap()

    # ---- 7. Eligibility & Participation
    s.section("Eligibility & Participation")
    s.header(["Status", "Count"])
    start_ep = s.r
    s.row(["Enrolled (EE/ES/EC/FAM)", enrolled_count])
    s.row(["Waiting Period (WP)", wp_count])
    s.row(["Waived (WO)", wo_count])
    s.row(["Not Eligible (NE)", ne_count])
    s.row(["Refused Coverage (RC)", rc_count])
    end_ep = s.r - 1
    s.row(["Total Full-Time Eligible", f"=SUM(B{start_ep}:B{end_ep})"])
    s.gap()

    # ---- 8. Waiting Period Employees
    s.section("Waiting Period Employees")
    s.header(["Metric", "Count"])
    wp_emps = [e for e in census.employees if e.coverage == "WP"]
    wp_elected = sum(1 for e in wp_emps if e.plan and e.plan not in ("", "NA", "N/A"))
    wp_missing = len(wp_emps) - wp_elected
    s.row(["Total WP Employees", len(wp_emps)])
    s.row(["WP Employees With Elected Plan", wp_elected])
    s.row(["WP Employees Missing Plan Election", wp_missing])
    s.gap()
    if wp_emps:
        s.header(["Employee", "Date of Hire", "Census Status", "Elected Plan", "Note"])
        for e in wp_emps:
            doh_str = e.hire_date.strftime("%m/%d/%Y") if e.hire_date else ""
            c_stat = e.cov_raw or "Not Eligible"
            plan_str = e.plan or "None provided"
            s.row([f"{e.last}, {e.first}", doh_str, c_stat, plan_str, f"Eligible {eff} per broker email" if eff else "Waiting period employee"])
        s.gap()

    # ---- 9. Dependent Audit
    s.section("Dependent Audit")
    s.header(["Subscriber", "Tier", "Dependent", "Relationship", "DOB", f"Age at {eff}" if eff else "Age"])
    for dep in census.dependents:
        sub_name = f"{dep.employee.last}, {dep.employee.first} (Row {dep.employee.row_no})" if dep.employee else "Unassigned"
        sub_tier = dep.coverage or (dep.employee.coverage if dep.employee else "")
        d_dob_str = dep.dob.strftime("%m/%d/%Y") if dep.dob else ""
        age_val = None
        if dep.dob and eff_dt:
            age_val = eff_dt.year - dep.dob.year - ((eff_dt.month, eff_dt.day) < (dep.dob.month, dep.dob.day))
        s.row([sub_name, sub_tier, f"{dep.last}, {dep.first}", dep.role, d_dob_str, age_val])
    s.gap()

    # ---- 10. Exception Report
    s.section("Exception Report")
    s.header(["Exception Type", "Employee", "Detail"])
    
    # Missing from census
    inv_only_all = [p for p in census.people if getattr(p, "invoice_only", False)]
    if inv_only_all:
        for p in inv_only_all:
            rr = res_map.get(id(p))
            prem_str = f", ${rr.inv_premium:,.2f}" if rr and rr.inv_premium else ""
            s.row(["Employee missing from census", f"{p.last}, {p.first}", f"On invoice ({p.plan}, Employee Only{prem_str}); broker reports recent term"])
    else:
        s.row(["Employee missing from census", "None", ""])

    # Missing from invoice
    missing_inv = [e for e in census.employees if not e.invoice_only and e.coverage in C.ACTIVE_TIERS and (not res_map.get(id(e)) or res_map[id(e)].inv is None)]
    if missing_inv:
        for e in missing_inv:
            s.row(["Employees missing from invoice", f"{e.last}, {e.first}", f"Coverage {e.coverage}"])
    else:
        s.row(["Employees missing from invoice", "None", ""])

    # Tier mismatches
    tier_mismatches = [e for e in census.employees if res_map.get(id(e)) and "Tier" in (res_map[id(e)].result or "")]
    if tier_mismatches:
        for e in tier_mismatches:
            s.row(["Tier mismatches", f"{e.last}, {e.first}", res_map[id(e)].result])
    else:
        s.row(["Tier mismatches", "None", ""])

    # Coverage mismatches
    cov_mismatches = [e for e in census.employees if res_map.get(id(e)) and "Coverage" in (res_map[id(e)].result or "")]
    if cov_mismatches:
        for e in cov_mismatches:
            s.row(["Coverage mismatches", f"{e.last}, {e.first}", res_map[id(e)].result])
    else:
        s.row(["Coverage mismatches", "None", ""])

    # Invalid dependent structures
    if census.unassigned_dependents:
        for d in census.unassigned_dependents:
            s.row(["Invalid dependent structures", f"{d.last}, {d.first}", "Unassigned dependent - no matching subscriber"])
    else:
        s.row(["Invalid dependent structures", "None", ""])

    # Premium discrepancies
    prem_disc = [e for e in census.employees if res_map.get(id(e)) and any("Premium Discrepancy" in n[0] for n in res_map[id(e)].notes)]
    if prem_disc:
        for e in prem_disc:
            s.row(["Premium discrepancies", f"{e.last}, {e.first}", ""])
    else:
        s.row(["Premium discrepancies", "None", ""])

    # Renewal mapping failures
    if not F.renewal_required:
        s.row(["Renewal mapping failures", "N/A", "Renewal not required"])
    elif F.renewal_unmapped_plans:
        for pl in sorted(F.renewal_unmapped_plans):
            s.row(["Renewal mapping failures", pl, "Plan not identified in renewal document"])
    else:
        s.row(["Renewal mapping failures", "None", ""])

    ws.column_dimensions["A"].width = 54
    for col, w in zip("BCDE", (32, 24, 24, 16)):
        ws.column_dimensions[col].width = w


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
        if getattr(p, "salary", ""):
            ws_census.cell(r, 13, p.salary)

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


def save_rapt_workbook(template: Path, out: Path, census, prospect, results, F, audit, tracer=None):
    wb = openpyxl.load_workbook(template)
    write_census_sheet(wb.active, census, prospect, results, tracer=tracer)
    write_summary_sheet(wb, census, prospect, F, audit, results=results)
    wb.save(out)
    return out


def save_marsh_workbook(template: Path, out: Path, census, prospect, results, F, audit, tracer=None):
    wb = openpyxl.load_workbook(template)
    ws = wb["Group Info + Census"] if "Group Info + Census" in wb.sheetnames else wb.active
    write_marsh_sheet(ws, census, prospect, results)
    write_summary_sheet(wb, census, prospect, F, audit, results=results)
    wb.save(out)
    return out


def save_sbu_workbook(template: Path, out: Path, census, prospect):
    wb = openpyxl.load_workbook(template)
    write_sbu_sheets(wb, census, prospect)
    wb.save(out)
    return out


def save_workbook(template: Path, out: Path, census, prospect, results, F, audit, carrier: str = "", tracer=None):
    t_name = str(template).lower()
    if carrier == C.CARRIER_CIGNA or "marsh" in t_name:
        return save_marsh_workbook(template, out, census, prospect, results, F, audit, tracer=tracer)
    else:
        return save_rapt_workbook(template, out, census, prospect, results, F, audit, tracer=tracer)

