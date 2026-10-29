"""
Writes the finished model to an Excel workbook.

The output is deliberately a spreadsheet, not a database. The point of this
module is to hand you the tab you would otherwise have spent two days building
by hand, with the historicals already spread and tied out -- so you can start
where the actual thinking begins: the forecast.

Sheets produced
---------------
  Summary          company identity, fiscal calendar, headline stats
  Income Statement / Balance Sheet / Cash Flow    10 years, standardized
  Metrics          every ratio, grouped
  Data Coverage    which XBRL tag fed each line, and what's missing
  Comps            (multi-company runs only) one row per company
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import metrics as metrics_module
from . import taxonomy
from .metrics import DAYS, MONEY, PCT, RATIO, TIMES, METRIC_SPECS
from .normalize import NormalizedCompany

MILLIONS = 1_000_000

# -- styling ---------------------------------------------------------------
TITLE_FONT = Font(bold=True, size=14)
HEADER_FONT = Font(bold=True, size=10, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
TTM_FILL = PatternFill("solid", fgColor="0E6655")  # distinct: unaudited stub period
GROUP_FONT = Font(bold=True, size=10)
GROUP_FILL = PatternFill("solid", fgColor="D9E2F3")
NOTE_FONT = Font(italic=True, size=9, color="666666")
LABEL_FONT = Font(size=10)
BOTTOM_BORDER = Border(bottom=Side(style="thin", color="9E9E9E"))

FORMATS = {
    MONEY: '#,##0;[Red](#,##0);"-"',
    PCT: '0.0%;[Red](0.0%);"-"',
    TIMES: '0.00"x";[Red](0.00"x");"-"',
    DAYS: '0.0;[Red](0.0);"-"',
    RATIO: '0.00;[Red](0.00);"-"',
}
EPS_FORMAT = '0.00;[Red](0.00);"-"'

# Line items that are per-share or share counts and must not be scaled to
# millions the way dollar figures are.
PER_SHARE_KEYS = {"eps_diluted"}
SHARE_COUNT_KEYS = {"shares_diluted", "shares_basic"}


def _set_widths(ws, label_width: int = 42, value_width: int = 14, columns: int = 12):
    ws.column_dimensions["A"].width = label_width
    for i in range(2, columns + 2):
        ws.column_dimensions[get_column_letter(i)].width = value_width


def _write_title(ws, title: str, subtitle: str = "") -> int:
    """Write the sheet heading. Returns the next free row number."""
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    row = 2
    if subtitle:
        ws["A2"] = subtitle
        ws["A2"].font = NOTE_FONT
        row = 3
    return row + 1


def _column_labels(company: NormalizedCompany) -> list[tuple[str, str]]:
    """(heading, sub-heading) for every column, fiscal years and TTM alike."""
    labels: list[tuple[str, str]] = []
    for col in company.columns:
        if isinstance(col, str):  # the trailing-twelve-month column
            as_of = company.ttm.as_of.strftime("%b %d, %Y") if company.ttm else ""
            labels.append((col, f"{as_of} · unaudited"))
        else:
            end = company.period_ends.get(col)
            labels.append((f"FY{col}", end.strftime("%b %d, %Y") if end else ""))
    return labels


def _write_column_header(ws, row: int, labels: list[tuple[str, str]]) -> int:
    """Header block: column names plus the period each one actually covers."""
    cell = ws.cell(row=row, column=1, value="Period")
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL
    for offset, (heading, _sub) in enumerate(labels):
        cell = ws.cell(row=row, column=2 + offset, value=heading)
        cell.font = HEADER_FONT
        # Tint the TTM column so nobody mistakes an unaudited stub for a
        # filed fiscal year.
        cell.fill = TTM_FILL if heading.startswith("TTM") else HEADER_FILL
        cell.alignment = Alignment(horizontal="right")

    date_row = row + 1
    ws.cell(row=date_row, column=1, value="Period ended").font = NOTE_FONT
    for offset, (_heading, sub) in enumerate(labels):
        cell = ws.cell(row=date_row, column=2 + offset, value=sub)
        cell.font = NOTE_FONT
        cell.alignment = Alignment(horizontal="right")
        cell.border = BOTTOM_BORDER
    return date_row


def _write_year_header(ws, row: int, years: list[int], period_ends: dict) -> int:
    """Header block: fiscal year labels plus the actual period end dates.

    Showing the real end date matters. Two companies both labeled FY2025 can
    have year ends five months apart, and you need to see that before you
    compare their margins.

    Returns the row number of the last header row written.
    """
    cell = ws.cell(row=row, column=1, value="Fiscal Year")
    cell.font = HEADER_FONT
    cell.fill = HEADER_FILL
    for offset, year in enumerate(years):
        cell = ws.cell(row=row, column=2 + offset, value=f"FY{year}")
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="right")

    date_row = row + 1
    cell = ws.cell(row=date_row, column=1, value="Period ended")
    cell.font = NOTE_FONT
    for offset, year in enumerate(years):
        end = period_ends.get(year)
        cell = ws.cell(row=date_row, column=2 + offset,
                       value=end.strftime("%b %d, %Y") if end else None)
        cell.font = NOTE_FONT
        cell.alignment = Alignment(horizontal="right")
        cell.border = BOTTOM_BORDER
    return date_row


# ---------------------------------------------------------------------------
# individual sheets
# ---------------------------------------------------------------------------

def _write_statement(ws, company: NormalizedCompany, statement: str) -> None:
    row = _write_title(
        ws,
        f"{company.entity_name} ({company.ticker}) - {statement}",
        "$ in millions except per-share amounts. Source: SEC XBRL company facts.",
    )
    labels = _column_labels(company)
    header_row = _write_column_header(ws, row, labels)
    row = header_row + 1

    for item in taxonomy.items_for(statement):
        if item.key not in company.statements.index:
            continue
        series = company.statements.loc[item.key]
        if series.isna().all():
            continue  # don't clutter the model with rows the company never reports

        label_cell = ws.cell(row=row, column=1, value=item.label)
        label_cell.font = LABEL_FONT

        for offset, year in enumerate(company.columns):
            value = series.get(year)
            if pd.isna(value):
                out_value = None
            elif item.key in PER_SHARE_KEYS:
                out_value = float(value)
            elif item.key in SHARE_COUNT_KEYS:
                out_value = float(value) / MILLIONS
            else:
                out_value = float(value) / MILLIONS

            cell = ws.cell(row=row, column=2 + offset, value=out_value)
            cell.number_format = (
                EPS_FORMAT if item.key in PER_SHARE_KEYS else FORMATS[MONEY]
            )
        row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)
    _set_widths(ws, columns=len(company.columns))


def _write_metrics(ws, company: NormalizedCompany, frame: pd.DataFrame) -> None:
    row = _write_title(
        ws,
        f"{company.entity_name} ({company.ticker}) - Metrics",
        "Dollar figures in millions. Return and turnover metrics use average "
        "balance sheet values.",
    )
    labels = _column_labels(company)
    header_row = _write_column_header(ws, row, labels)
    row = header_row + 1

    current_group = None
    for spec in METRIC_SPECS:
        if spec.key not in frame.index:
            continue
        series = frame.loc[spec.key]
        if series.isna().all():
            continue

        if spec.group != current_group:
            current_group = spec.group
            group_cell = ws.cell(row=row, column=1, value=spec.group.upper())
            group_cell.font = GROUP_FONT
            group_cell.fill = GROUP_FILL
            for offset in range(len(company.columns)):
                ws.cell(row=row, column=2 + offset).fill = GROUP_FILL
            row += 1

        ws.cell(row=row, column=1, value=spec.label).font = LABEL_FONT

        for offset, year in enumerate(company.columns):
            value = series.get(year)
            if pd.isna(value):
                out_value = None
            elif spec.fmt == MONEY:
                out_value = float(value) / MILLIONS
            else:
                out_value = float(value)
            cell = ws.cell(row=row, column=2 + offset, value=out_value)
            cell.number_format = FORMATS[spec.fmt]

        if spec.note:
            note_col = len(company.columns) + 3
            note_cell = ws.cell(row=row, column=note_col, value=spec.note)
            note_cell.font = NOTE_FONT
        row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)
    _set_widths(ws, columns=len(company.columns))
    ws.column_dimensions[get_column_letter(len(company.columns) + 3)].width = 70


def _write_coverage(ws, company: NormalizedCompany) -> None:
    row = _write_title(
        ws,
        f"{company.entity_name} ({company.ticker}) - Data Coverage",
        "Your audit trail. Check this before trusting any number above. "
        "MISSING rows are where you should add a tag to taxonomy.py.",
    )

    headers = ["Line Item", "Statement", "Status", "XBRL Tag Used",
               "Years Found", "Years Missing", "Restated Years",
               "Not From 10-K", "Gap-Filled From"]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    header_row = row
    row += 1

    warn_fill = PatternFill("solid", fgColor="FCE4D6")
    for cov in company.coverage:
        ws.cell(row=row, column=1, value=cov.label)
        ws.cell(row=row, column=2, value=cov.statement)
        status_cell = ws.cell(row=row, column=3, value=cov.status)
        ws.cell(row=row, column=4, value=cov.tag_used or "")
        ws.cell(row=row, column=5, value=cov.years_found)
        ws.cell(row=row, column=6,
                value=", ".join(str(y) for y in cov.years_missing))
        ws.cell(row=row, column=7,
                value=", ".join(str(y) for y in cov.restated_years))
        ws.cell(row=row, column=8,
                value=", ".join(str(y) for y in cov.non_10k_years))
        ws.cell(row=row, column=9, value=", ".join(cov.supplemental_tags))
        if cov.status in {"MISSING", "PARTIAL"}:
            for col in range(1, len(headers) + 1):
                ws.cell(row=row, column=col).fill = warn_fill
        row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    widths = [44, 18, 22, 58, 13, 24, 18, 20, 58]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _write_summary(ws, company: NormalizedCompany, stats: dict) -> None:
    row = _write_title(
        ws,
        f"{company.entity_name} ({company.ticker})",
        "Standardized from SEC XBRL data. Always check the Data Coverage tab.",
    )

    def kv(label: str, value, fmt: str | None = None) -> None:
        nonlocal row
        ws.cell(row=row, column=1, value=label).font = LABEL_FONT
        cell = ws.cell(row=row, column=2, value=value)
        if fmt:
            cell.number_format = fmt
        row += 1

    kv("CIK", company.cik)
    if company.industry:
        kv("Industry (SEC SIC)", company.industry)
    kv("Fiscal years covered",
       f"FY{company.fiscal_years[0]} - FY{company.fiscal_years[-1]}")
    kv("Most recent period end", company.period_ends[company.fiscal_years[-1]].isoformat())
    kv("Fiscal year end month",
       company.period_ends[company.fiscal_years[-1]].strftime("%B"))
    if company.ttm is not None:
        kv("Latest quarterly data through", company.ttm.as_of.isoformat())
        kv("Months past fiscal year end", company.ttm.months_beyond_fiscal_year)
        kv("TTM column", company.ttm.label + " (unaudited)")
    else:
        kv("Latest quarterly data", "none newer than the 10-K")
    row += 1

    ws.cell(row=row, column=1, value="HEADLINE STATISTICS").font = GROUP_FONT
    ws.cell(row=row, column=1).fill = GROUP_FILL
    ws.cell(row=row, column=2).fill = GROUP_FILL
    row += 1

    latest_revenue = stats.get("latest_revenue")
    kv("Latest revenue ($mm)",
       None if latest_revenue is None else latest_revenue / MILLIONS,
       FORMATS[MONEY])
    kv("Revenue CAGR (full period)", stats.get("revenue_cagr"), FORMATS[PCT])
    kv("Net income CAGR (full period)", stats.get("net_income_cagr"), FORMATS[PCT])
    kv("Gross margin (latest)", stats.get("gross_margin_latest"), FORMATS[PCT])
    kv("Operating margin (latest)", stats.get("operating_margin_latest"), FORMATS[PCT])
    kv("Operating margin (period avg)", stats.get("operating_margin_avg"), FORMATS[PCT])
    kv("FCF margin (latest)", stats.get("fcf_margin_latest"), FORMATS[PCT])
    kv("ROIC (latest)", stats.get("roic_latest"), FORMATS[PCT])
    kv("ROIC (period avg)", stats.get("roic_avg"), FORMATS[PCT])
    kv("FCF / net income (cumulative)", stats.get("fcf_conversion_avg"), FORMATS[PCT])
    kv("Net debt / EBITDA (latest)", stats.get("net_debt_to_ebitda_latest"), FORMATS[TIMES])
    kv("Cash conversion cycle (latest, days)",
       stats.get("cash_conversion_cycle_latest"), FORMATS[DAYS])
    kv("Accruals ratio (latest)", stats.get("accruals_ratio_latest"), FORMATS[PCT])
    kv("Stock comp % of revenue (latest)", stats.get("sbc_pct_revenue_latest"), FORMATS[PCT])

    # -- warnings: read these before you trust anything above ---------------
    row += 1
    ws.cell(row=row, column=1, value="DATA WARNINGS").font = GROUP_FONT
    ws.cell(row=row, column=1).fill = GROUP_FILL
    ws.cell(row=row, column=2).fill = GROUP_FILL
    row += 1

    warn_font = Font(size=10, color="9C0006")
    if company.warnings:
        for warning in company.warnings:
            cell = ws.cell(row=row, column=1, value=f"* {warning}")
            cell.font = warn_font
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
            ws.row_dimensions[row].height = 32
            row += 1
    else:
        ws.cell(row=row, column=1, value="None detected.").font = NOTE_FONT
        row += 1

    row += 1
    missing = [c.label for c in company.coverage if c.status == "MISSING"]
    ws.cell(row=row, column=1, value="Line items with no data:").font = GROUP_FONT
    row += 1
    cell = ws.cell(row=row, column=1,
                   value=", ".join(missing) if missing else "None -- full coverage.")
    cell.font = NOTE_FONT
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 26
    for i in range(3, 7):
        ws.column_dimensions[get_column_letter(i)].width = 18


def _write_redflags(ws, report) -> None:
    """The screener's findings, triggered first."""
    row = _write_title(
        ws, f"{report.ticker} - Accounting Red Flags",
        "Rules-based checks applied identically to every company. A triggered "
        "flag is a question for the filing, not a verdict.",
    )
    ws.cell(row=row, column=1, value="Composite score").font = GROUP_FONT
    ws.cell(row=row, column=2, value=f"{report.score} / 100").font = GROUP_FONT
    row += 1
    ws.cell(row=row, column=1, value="Assessment")
    ws.cell(row=row, column=2, value=report.verdict)
    row += 1
    if report.beneish_m is not None:
        ws.cell(row=row, column=1, value="Beneish M-score")
        ws.cell(row=row, column=2, value=round(report.beneish_m, 2))
        row += 1
    if report.altman_z is not None:
        ws.cell(row=row, column=1, value=f"Altman {report.altman_variant}-score")
        ws.cell(row=row, column=2,
                value=f"{report.altman_z:.2f} ({report.altman_zone})")
        row += 1
    row += 1

    headers = ["", "Flag", "Area", "Severity", "Detail"]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    header_row = row
    row += 1

    trigger_fill = PatternFill("solid", fgColor="FDECEE")
    for flag in sorted(report.flags, key=lambda f: (not f.triggered, f.category)):
        ws.cell(row=row, column=1, value="FLAG" if flag.triggered else "ok")
        ws.cell(row=row, column=2, value=flag.name)
        ws.cell(row=row, column=3, value=flag.category)
        ws.cell(row=row, column=4, value=flag.severity if flag.triggered else "")
        detail = ws.cell(row=row, column=5, value=flag.detail)
        detail.alignment = Alignment(wrap_text=True, vertical="top")
        if flag.triggered:
            for col in range(1, 6):
                ws.cell(row=row, column=col).fill = trigger_fill
        row += 1

    if report.notes:
        row += 1
        ws.cell(row=row, column=1, value="NOTES").font = GROUP_FONT
        row += 1
        for note in report.notes:
            cell = ws.cell(row=row, column=1, value=note)
            cell.font = NOTE_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
            ws.row_dimensions[row].height = 30
            row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    for i, width in enumerate([8, 34, 22, 11, 78], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _write_valuation(ws, result) -> None:
    """Reverse DCF: what the price already assumes."""
    row = _write_title(
        ws, f"{result.ticker} - Reverse DCF",
        "Takes the price as given and solves for the growth it requires. "
        "Assumptions are in config.py and drive the answer.",
    )

    def kv(label, value, fmt=None, bold=False):
        nonlocal row
        cell = ws.cell(row=row, column=1, value=label)
        if bold:
            cell.font = GROUP_FONT
        out = ws.cell(row=row, column=2, value=value)
        if fmt:
            out.number_format = fmt
        if bold:
            out.font = GROUP_FONT
        row += 1

    kv("Share price", result.price, '#,##0.00')
    kv("Price source", f"{result.price_source} {result.price_as_of}".strip())
    kv("Shares outstanding (mm)", result.shares / MILLIONS, FORMATS[MONEY])
    kv("Market capitalisation ($mm)", result.market_cap / MILLIONS, FORMATS[MONEY])
    kv("Net debt ($mm)", result.net_debt / MILLIONS, FORMATS[MONEY])
    kv("Enterprise value ($mm)", result.enterprise_value / MILLIONS, FORMATS[MONEY])
    row += 1
    kv("ASSUMPTIONS", "", bold=True)
    kv("Discount rate (WACC)", result.wacc, FORMATS[PCT])
    kv("Terminal growth", result.terminal_growth, FORMATS[PCT])
    kv("Forecast years", result.forecast_years)
    kv(f"Base free cash flow ({result.base_period}, $mm)",
       result.base_fcf / MILLIONS, FORMATS[MONEY])
    if result.base_vs_average is not None:
        kv("Base vs 5-year average", result.base_vs_average, FORMATS[TIMES])
    row += 1
    kv("THE ANSWER", "", bold=True)
    kv("Implied FCF growth (required)", result.implied_growth, FORMATS[PCT], bold=True)
    kv("Actual FCF growth (10yr)", result.historical_fcf_cagr, FORMATS[PCT])
    kv("Actual revenue growth (10yr)", result.historical_revenue_cagr, FORMATS[PCT])
    if result.verdict:
        kv("Read", result.verdict)
    row += 1
    kv("MULTIPLES", "", bold=True)
    kv("EV / EBITDA", result.ev_to_ebitda, FORMATS[TIMES])
    kv("EV / free cash flow", result.ev_to_fcf, FORMATS[TIMES])
    kv("Free cash flow yield", result.fcf_yield, FORMATS[PCT])
    kv("P / E", result.pe_ratio, FORMATS[TIMES])

    if result.sensitivity is not None:
        row += 2
        ws.cell(row=row, column=1,
                value="SENSITIVITY - implied growth by WACC (rows) and "
                      "terminal growth (columns)").font = GROUP_FONT
        row += 1
        ws.cell(row=row, column=1, value="WACC \\ terminal").font = HEADER_FONT
        ws.cell(row=row, column=1).fill = HEADER_FILL
        for offset, column_name in enumerate(result.sensitivity.columns):
            cell = ws.cell(row=row, column=2 + offset, value=column_name)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
        row += 1
        for index, data_row in result.sensitivity.iterrows():
            ws.cell(row=row, column=1, value=index)
            for offset, value in enumerate(data_row):
                cell = ws.cell(row=row, column=2 + offset,
                               value=None if value is None else float(value))
                cell.number_format = FORMATS[PCT]
            row += 1

    if result.warnings:
        row += 1
        ws.cell(row=row, column=1, value="WARNINGS").font = GROUP_FONT
        row += 1
        for warning in result.warnings:
            cell = ws.cell(row=row, column=1, value=warning)
            cell.font = NOTE_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
            ws.row_dimensions[row].height = 32
            row += 1

    ws.column_dimensions["A"].width = 44
    for i in range(2, 8):
        ws.column_dimensions[get_column_letter(i)].width = 16


def _write_multiples(ws, report) -> None:
    """Current multiples, each year's multiples, and current vs own history."""
    from .multiples import SPEC

    row = _write_title(
        ws, f"{report.ticker} - Valuation Multiples",
        "Historical market cap uses each year's weighted-average diluted "
        "shares. Negative denominators are left blank, not shown negative.",
    )

    current = report.current
    if current and current.market_cap:
        for label, value, fmt in [
            ("Price", current.price, '#,##0.00'),
            ("Shares (mm)", current.shares / MILLIONS, FORMATS[MONEY]),
            ("Market cap ($mm)", current.market_cap / MILLIONS, FORMATS[MONEY]),
            ("Net debt ($mm)", (current.net_debt or 0) / MILLIONS, FORMATS[MONEY]),
            ("Enterprise value ($mm)",
             (current.enterprise_value or 0) / MILLIONS, FORMATS[MONEY]),
        ]:
            ws.cell(row=row, column=1, value=label)
            cell = ws.cell(row=row, column=2, value=value)
            cell.number_format = fmt
            row += 1
        row += 1

    headers = ["Multiple", "Current", "Median", "Low", "High", "Yrs",
               "%ile", "vs Median", "Read"]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    header_row = row
    row += 1

    cheap_fill = PatternFill("solid", fgColor="E8F6EE")
    rich_fill = PatternFill("solid", fgColor="FDECEE")

    for key, label, unit, lower_is_cheaper, judged in SPEC:
        value = current.get(key) if current else None
        stat = report.stats.get(key)
        if value is None and stat is None:
            continue
        fmt = FORMATS[PCT] if unit == "%" else FORMATS[TIMES]

        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value).number_format = fmt
        if stat:
            ws.cell(row=row, column=3, value=stat.median).number_format = fmt
            ws.cell(row=row, column=4, value=stat.low).number_format = fmt
            ws.cell(row=row, column=5, value=stat.high).number_format = fmt
            ws.cell(row=row, column=6, value=stat.observations)
            pct = ws.cell(row=row, column=7, value=stat.percentile)
            pct.number_format = '0'
            ws.cell(row=row, column=8, value=stat.vs_median).number_format = FORMATS[PCT]
            ws.cell(row=row, column=9, value=stat.verdict)
            if (judged and stat.reliable and stat.vs_median is not None
                    and abs(stat.vs_median) >= 0.10):
                rich = (stat.vs_median > 0) if lower_is_cheaper else (stat.vs_median < 0)
                for col in range(1, 10):
                    ws.cell(row=row, column=col).fill = rich_fill if rich else cheap_fill
        row += 1

    # Year-by-year, so the median is auditable rather than asserted.
    if report.history:
        row += 2
        ws.cell(row=row, column=1, value="MULTIPLE BY FISCAL YEAR").font = GROUP_FONT
        row += 1
        ws.cell(row=row, column=1, value="Multiple").font = HEADER_FONT
        ws.cell(row=row, column=1).fill = HEADER_FILL
        for offset, period in enumerate([h.period for h in report.history]):
            cell = ws.cell(row=row, column=2 + offset, value=f"FY{period}")
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
        price_row = row + 1
        ws.cell(row=price_row, column=1, value="Price at year end").font = NOTE_FONT
        for offset, item in enumerate(report.history):
            ws.cell(row=price_row, column=2 + offset,
                    value=item.price).number_format = '#,##0.00'
        row = price_row + 1
        for key, label, unit, _low, _judged in SPEC:
            series = [h.get(key) for h in report.history]
            if all(v is None for v in series):
                continue
            ws.cell(row=row, column=1, value=label)
            for offset, value in enumerate(series):
                cell = ws.cell(row=row, column=2 + offset, value=value)
                cell.number_format = FORMATS[PCT] if unit == "%" else FORMATS[TIMES]
            row += 1

    if report.notes:
        row += 1
        for note in report.notes:
            cell = ws.cell(row=row, column=1, value=note)
            cell.font = NOTE_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
            ws.row_dimensions[row].height = 30
            row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)
    for i, width in enumerate([30, 12, 12, 11, 11, 7, 8, 12, 30], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width


def _write_comps(ws, rows: list[dict]) -> None:
    """One row per company -- the fast peer screen."""
    row = _write_title(
        ws, "Peer Comparison",
        "Same definitions applied to every company. $ in millions. "
        "\"Data Through\" is the latest period behind each row -- a TTM date "
        "means the figures include quarterly filings past the last 10-K.",
    )

    columns = [
        ("ticker", "Ticker", None),
        ("company", "Company", None),
        ("as_of", "Data Through", None),
        ("years_of_data", "Yrs", '0'),
        ("latest_revenue", "Revenue", FORMATS[MONEY]),
        ("revenue_cagr", "Rev CAGR", FORMATS[PCT]),
        ("gross_margin_latest", "Gross Mgn", FORMATS[PCT]),
        ("operating_margin_latest", "Op Mgn", FORMATS[PCT]),
        ("operating_margin_avg", "Op Mgn Avg", FORMATS[PCT]),
        ("fcf_margin_latest", "FCF Mgn", FORMATS[PCT]),
        ("roic_latest", "ROIC", FORMATS[PCT]),
        ("roic_avg", "ROIC Avg", FORMATS[PCT]),
        ("fcf_conversion_avg", "FCF/NI Cum", FORMATS[PCT]),
        ("net_debt_to_ebitda_latest", "ND/EBITDA", FORMATS[TIMES]),
        ("cash_conversion_cycle_latest", "CCC (days)", FORMATS[DAYS]),
        ("sbc_pct_revenue_latest", "SBC % Rev", FORMATS[PCT]),
        ("accruals_ratio_latest", "Accruals", FORMATS[PCT]),
        ("redflag_score", "Flags", '0'),
        ("redflag_verdict", "Assessment", None),
        ("implied_growth", "Implied Gr", FORMATS[PCT]),
        ("historical_fcf_cagr", "Actual Gr", FORMATS[PCT]),
        ("ev_to_ebitda", "EV/EBITDA", FORMATS[TIMES]),
        ("filing_change", "10-K Chg", FORMATS[PCT]),
        ("ev_ebitda_median", "EV/EB Med", FORMATS[TIMES]),
        ("ev_ebitda_pctile", "EV/EB %ile", '0'),
    ]

    for col, (_, header, _fmt) in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    header_row = row
    row += 1

    for entry in rows:
        for col, (key, _header, fmt) in enumerate(columns, start=1):
            value = entry.get(key)
            if key == "latest_revenue" and value is not None:
                value = value / MILLIONS
            cell = ws.cell(row=row, column=col, value=value)
            if fmt:
                cell.number_format = fmt
        row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=3)
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 34
    for i in range(3, len(columns) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 13


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

def write_company_workbook(
    company: NormalizedCompany,
    metric_frame: pd.DataFrame,
    stats: dict,
    path: Path,
    redflag_report=None,
    valuation_result=None,
    multiples_report=None,
) -> Path:
    """Write the full single-company model."""
    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet

    _write_summary(wb.create_sheet("Summary"), company, stats)
    if multiples_report is not None and multiples_report.current is not None:
        _write_multiples(wb.create_sheet("Multiples"), multiples_report)
    if valuation_result is not None:
        _write_valuation(wb.create_sheet("Reverse DCF"), valuation_result)
    if redflag_report is not None:
        _write_redflags(wb.create_sheet("Red Flags"), redflag_report)
    for statement in (taxonomy.INCOME_STATEMENT, taxonomy.BALANCE_SHEET,
                      taxonomy.CASH_FLOW):
        _write_statement(wb.create_sheet(statement), company, statement)
    _write_metrics(wb.create_sheet("Metrics"), company, metric_frame)
    _write_coverage(wb.create_sheet("Data Coverage"), company)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_csvs(company, metric_frame, folder: Path,
               redflag_report=None, multiples_report=None) -> list[Path]:
    """Write the model as plain CSVs.

    Not everyone has Excel, and nothing here needs it: CSV opens in Numbers,
    Google Sheets, LibreOffice, a text editor, or pandas. The .xlsx is nicer to
    read and carries the formatting; the CSVs carry the data anywhere.
    """
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    prefix = company.ticker

    def label_index(frame, labels):
        # Drop rows the company never reports, matching the workbook and the
        # brief. A CSV full of empty "Research & Development" rows invites the
        # reader to wonder what went wrong.
        out = frame.dropna(how="all").copy()
        out.index = [labels.get(key, key) for key in out.index]
        out.index.name = "Line item"
        return out

    statement_labels = {item.key: item.label for item in taxonomy.LINE_ITEMS}
    statements = company.statements.copy()
    # Dollars in millions, matching the workbook, except per-share figures.
    scaled = statements / MILLIONS
    for key in PER_SHARE_KEYS:
        if key in statements.index:
            scaled.loc[key] = statements.loc[key]
    path = folder / f"{prefix}_statements.csv"
    label_index(scaled, statement_labels).to_csv(path, float_format="%.2f")
    written.append(path)

    metric_labels = {spec.key: spec.label for spec in METRIC_SPECS}
    path = folder / f"{prefix}_metrics.csv"
    label_index(metric_frame, metric_labels).to_csv(path, float_format="%.4f")
    written.append(path)

    path = folder / f"{prefix}_coverage.csv"
    pd.DataFrame([{
        "Line item": row.label,
        "Statement": row.statement,
        "Status": row.status,
        "XBRL tag": row.tag_used or "",
        "Years found": row.years_found,
        "Years missing": ", ".join(str(y) for y in row.years_missing),
        "Restated years": ", ".join(str(y) for y in row.restated_years),
    } for row in company.coverage]).to_csv(path, index=False)
    written.append(path)

    if redflag_report is not None:
        path = folder / f"{prefix}_redflags.csv"
        pd.DataFrame([{
            "Triggered": "YES" if flag.triggered else "no",
            "Flag": flag.name,
            "Area": flag.category,
            "Severity": flag.severity if flag.triggered else "",
            "Detail": flag.detail,
        } for flag in redflag_report.flags]).to_csv(path, index=False)
        written.append(path)

    if multiples_report is not None and multiples_report.current is not None:
        from .multiples import SPEC
        rows = []
        for key, label, unit, _low, _judged in SPEC:
            stat = multiples_report.stats.get(key)
            rows.append({
                "Multiple": label,
                "Unit": unit,
                "Current": multiples_report.current.get(key),
                "Median": stat.median if stat else None,
                "Low": stat.low if stat else None,
                "High": stat.high if stat else None,
                "Years": stat.observations if stat else 0,
                "Percentile": stat.percentile if stat else None,
                "vs median": stat.vs_median if stat else None,
                "Read": stat.verdict if stat else "",
            })
        path = folder / f"{prefix}_multiples.csv"
        pd.DataFrame(rows).to_csv(path, index=False, float_format="%.4f")
        written.append(path)

    return written


def write_comps_workbook(rows: list[dict], path: Path) -> Path:
    """Write the peer comparison workbook for a multi-ticker run."""
    wb = Workbook()
    wb.remove(wb.active)
    _write_comps(wb.create_sheet("Comps"), rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
