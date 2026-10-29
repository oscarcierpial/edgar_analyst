"""
The one-page brief: everything modules 1, 3 and 4 found, in reading order.

The Excel workbook is the model -- ten years of statements you'll build a
forecast on top of. This is the other half: what you read BEFORE opening it,
to decide whether the company is worth the afternoon.

Order is deliberate and it is not the order the modules were built in:

  1. What the price assumes     the only question that decides anything
  2. What to be suspicious of   what would break the thesis
  3. What management changed    what they thought needed saying differently
  4. Data warnings              what not to trust in any of the above

Self-contained HTML with no external anything, so it opens offline and can be
sent to someone without breaking.
"""

from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
from pathlib import Path

STYLE = """
:root{--ink:#14181f;--muted:#5d6673;--line:#dfe3ea;--bg:#ffffff;
--add:#0a6b3d;--addbg:#e8f6ee;--del:#8a1c2b;--delbg:#fdecee;--accent:#1f3864}
*{box-sizing:border-box}
body{margin:0;background:#f4f6f9;color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:3px solid var(--accent);padding-bottom:16px;margin-bottom:8px}
h1{margin:0 0 4px;font-size:26px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px}
section{background:var(--bg);border:1px solid var(--line);border-radius:8px;
padding:22px 24px;margin:20px 0}
h2{margin:0 0 4px;font-size:18px;letter-spacing:-.01em}
h2 .n{color:var(--muted);font-weight:600;margin-right:8px}
.lede{color:var(--muted);font-size:13.5px;margin:0 0 16px}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;
text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--line);
padding:6px 8px}
td{padding:7px 8px;border-bottom:1px solid #f0f2f5;vertical-align:top}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:1fr auto;gap:2px 16px;margin:10px 0}
.kv div{padding:5px 0;border-bottom:1px solid #f0f2f5}
.kv .v{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.callout{border-left:3px solid var(--accent);background:#f7f9fc;padding:12px 14px;
margin:14px 0;font-size:14px;border-radius:0 4px 4px 0}
.warn{border-left-color:#b45309;background:#fffbeb}
.sev{display:inline-block;font-size:11px;font-weight:700;padding:2px 7px;
border-radius:3px;text-transform:uppercase;letter-spacing:.03em}
.sev.High{background:#fdecee;color:var(--del)}
.sev.Medium{background:#fff4e5;color:#92400e}
.sev.Low{background:#eef2f7;color:var(--muted)}
.blk{padding:9px 12px;margin:7px 0;border-radius:4px;font-size:13.5px;
white-space:pre-wrap;word-wrap:break-word}
.blk.added{background:var(--addbg);border-left:3px solid var(--add)}
.blk.removed{background:var(--delbg);border-left:3px solid var(--del);
text-decoration:line-through;text-decoration-color:#d7a1a9}
.tag{font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.tag.added{color:var(--add)} .tag.removed{color:var(--del)}
details{margin:10px 0;border:1px solid var(--line);border-radius:6px}
summary{cursor:pointer;padding:11px 14px;font-weight:600;font-size:14px}
summary:hover{background:#f7f9fc}
.body{padding:4px 14px 14px}
.muted{color:var(--muted)}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.scroll table{min-width:640px;font-size:13px}
td.grp{background:var(--line);font-weight:700;font-size:11px;
text-transform:uppercase;letter-spacing:.04em}
.foot{color:var(--muted);font-size:12.5px;margin-top:28px;text-align:center}
@media(max-width:600px){.wrap{padding:18px 12px 60px}h1{font-size:21px}}
"""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _pct(value, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _money(value) -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    if absolute >= 1e9:
        return f"${value / 1e9:,.2f}bn"
    if absolute >= 1e6:
        return f"${value / 1e6:,.0f}mm"
    return f"${value:,.0f}"


def _valuation_section(result) -> str:
    if result is None:
        return (
            "<section><h2><span class='n'>1</span>What the price assumes</h2>"
            "<p class='lede'>Skipped — no share price available (Yahoo didn't "
            "answer for this ticker). Add a row to prices.csv, then re-run.</p></section>"
        )

    rows = [
        ("Share price", f"${result.price:,.2f}"
         + (f" <span class='muted'>({_esc(result.price_source)}"
            + (f", {_esc(result.price_as_of)}" if result.price_as_of else "") + ")</span>"
            if result.price_source else "")),
        ("Market capitalisation", _money(result.market_cap)),
        ("Net debt", _money(result.net_debt)),
        ("Enterprise value", _money(result.enterprise_value)),
        (f"Free cash flow ({_esc(result.base_period)})", _money(result.base_fcf)),
    ]
    body = "".join(f"<div>{label}</div><div class='v'>{value}</div>"
                   for label, value in rows)

    implied = _pct(result.implied_growth)
    historical = _pct(result.historical_fcf_cagr)
    verdict = result.verdict

    headline = (
        f"<div class='callout'><strong>At ${result.price:,.2f}, the market "
        f"requires free cash flow to compound at {implied} a year for "
        f"{result.forecast_years} years</strong> (then {_pct(result.terminal_growth)} "
        f"forever, discounted at {_pct(result.wacc)}). "
        f"Over the last decade it actually compounded at {historical}."
        + (f"<br><br>{_esc(verdict)}." if verdict else "")
        + "</div>"
    )
    if result.implied_growth is None:
        headline = (f"<div class='callout warn'>{_esc(result.implied_note)}</div>")

    multiples = "".join(
        f"<div>{label}</div><div class='v'>{value}</div>" for label, value in [
            ("EV / EBITDA", "—" if result.ev_to_ebitda is None
             else f"{result.ev_to_ebitda:,.1f}x"),
            ("EV / free cash flow", "—" if result.ev_to_fcf is None
             else f"{result.ev_to_fcf:,.1f}x"),
            ("Free cash flow yield", _pct(result.fcf_yield)),
            ("P / E", "—" if result.pe_ratio is None else f"{result.pe_ratio:,.1f}x"),
            ("Revenue CAGR (10yr actual)", _pct(result.historical_revenue_cagr)),
        ])

    sensitivity = ""
    if result.sensitivity is not None:
        frame = result.sensitivity
        header = "".join(f"<th class='num'>{_esc(c)}</th>" for c in frame.columns)
        body_rows = ""
        for index, row in frame.iterrows():
            cells = "".join(
                f"<td class='num'>{'—' if v is None else f'{v * 100:.1f}%'}</td>"
                for v in row)
            body_rows += f"<tr><td><strong>{_esc(index)}</strong></td>{cells}</tr>"
        sensitivity = (
            "<details><summary>How much of that is my assumptions?</summary>"
            "<div class='body'><p class='lede'>Implied growth at different "
            "discount rates (rows) and terminal growth rates (columns). If the "
            "conclusion flips inside this range, the thesis rests on the "
            "assumption rather than the company.</p>"
            f"<table><tr><th>WACC</th>{header}</tr>{body_rows}</table></div></details>"
        )

    warnings = "".join(
        f"<div class='callout warn'>{_esc(w)}</div>" for w in result.warnings)

    return (
        "<section><h2><span class='n'>1</span>What the price assumes</h2>"
        "<p class='lede'>A reverse discounted cash flow. Rather than guessing a "
        "value, it takes today's price as given and solves for the growth the "
        "market is already paying for — so the question becomes whether that "
        "specific expectation is too high or too low.</p>"
        f"{headline}<div class='kv'>{body}</div>"
        f"<h3 style='font-size:14px;margin:18px 0 0'>Multiples</h3>"
        f"<div class='kv'>{multiples}</div>{sensitivity}{warnings}</section>"
    )


def _multiples_section(report) -> str:
    """What you're paying, and whether that's normal for this company."""
    if report is None or report.current is None or not report.current.market_cap:
        return ""
    from .multiples import SPEC

    rows = ""
    for key, label, unit, lower_is_cheaper, judged in SPEC:
        value = report.current.get(key)
        stat = report.stats.get(key)
        if value is None and stat is None:
            continue
        shown = ("—" if value is None
                 else (_pct(value) if unit == "%" else f"{value:,.1f}x"))
        if stat and stat.median is not None and stat.reliable:
            median = (_pct(stat.median) if unit == "%" else f"{stat.median:,.1f}x")
            gap = "" if stat.vs_median is None else f"{stat.vs_median:+.0%}"
            pctile = "" if stat.percentile is None else f"{stat.percentile:.0f}th"
            read = _esc(stat.verdict)
        else:
            median = gap = pctile = "—"
            read = "<span class='muted'>no usable history</span>"
        rows += (f"<tr><td>{_esc(label)}</td><td class='num'>{shown}</td>"
                 f"<td class='num'>{median}</td><td class='num'>{gap}</td>"
                 f"<td class='num'>{pctile}</td><td>{read}</td></tr>")

    notes = "".join(f"<div class='callout'>{_esc(n)}</div>" for n in report.notes)

    return (
        "<section><h2><span class='n'>2</span>What you're paying</h2>"
        "<p class='lede'>A multiple on its own says nothing — 11x is cheap for "
        "software and dear for a utility. What matters is the multiple against "
        "this company's own history, which turns \"is it cheap?\" into \"why is "
        "the market paying less than it usually does?\"</p>"
        f"{notes}"
        f"<table><tr><th>Multiple</th><th class='num'>Now</th>"
        f"<th class='num'>Own median</th><th class='num'>vs median</th>"
        f"<th class='num'>Percentile</th><th>Read</th></tr>{rows}</table>"
        "</section>"
    )


def _redflag_section(report) -> str:
    if report is None:
        return ""
    triggered = report.triggered
    if triggered:
        rows = "".join(
            f"<tr><td><span class='sev {f.severity}'>{f.severity}</span></td>"
            f"<td><strong>{_esc(f.name)}</strong><br>"
            f"<span class='muted'>{_esc(f.detail)}</span></td>"
            f"<td class='muted'>{_esc(f.category)}</td></tr>"
            for f in triggered)
        table = (f"<table><tr><th>Severity</th><th>Flag</th><th>Area</th></tr>"
                 f"{rows}</table>")
    else:
        table = "<p class='muted'>Nothing triggered.</p>"

    passed = [f for f in report.flags if not f.triggered]
    passed_html = ""
    if passed:
        items = "".join(
            f"<tr><td>{_esc(f.name)}</td><td class='muted'>{_esc(f.detail)}</td></tr>"
            for f in passed)
        passed_html = (
            f"<details><summary>{len(passed)} checks that didn't trigger</summary>"
            f"<div class='body'><table>{items}</table></div></details>")

    scores = ""
    if report.beneish_m is not None or report.altman_z is not None:
        parts = []
        if report.beneish_m is not None:
            parts.append(f"<div>Beneish M-score</div>"
                         f"<div class='v'>{report.beneish_m:.2f}</div>")
        if report.altman_z is not None:
            parts.append(f"<div>Altman {_esc(report.altman_variant)}-score</div>"
                         f"<div class='v'>{report.altman_z:.2f} "
                         f"({_esc(report.altman_zone)})</div>")
        scores = f"<div class='kv'>{''.join(parts)}</div>"

    notes = "".join(f"<div class='callout warn'>{_esc(n)}</div>"
                    for n in report.notes)

    return (
        "<section><h2><span class='n'>3</span>What to be suspicious of</h2>"
        "<p class='lede'>Rules-based checks applied identically to every "
        "company. A triggered flag is a question to take to the filing, not a "
        "verdict — a company building a factory will fail the reinvestment "
        "test and should.</p>"
        f"<div class='callout'><strong>{_esc(report.verdict)}</strong> — "
        f"{len(triggered)} of {len(report.flags)} checks triggered "
        f"(score {report.score}/100).</div>"
        f"{scores}{table}{passed_html}{notes}</section>"
    )


def _filing_section(comparison) -> str:
    if comparison is None:
        return (
            "<section><h2><span class='n'>4</span>What management changed</h2>"
            "<p class='lede'>Skipped — fewer than two 10-K filings available to "
            "compare.</p></section>"
        )

    rows = "".join(
        f"<tr><td><strong>Item {_esc(s.item)}</strong> {_esc(s.title)}</td>"
        f"<td class='num'>{s.percent_changed * 100:.0f}%</td>"
        f"<td class='num'>+{s.words_added:,}</td>"
        f"<td class='num'>−{s.words_removed:,}</td></tr>"
        for s in comparison.most_changed)

    details = ""
    for section in comparison.most_changed:
        if not section.blocks:
            continue
        blocks = ""
        for kind, text in section.blocks:
            label = "Added" if kind == "added" else "Removed"
            blocks += (f"<div class='blk {kind}'>"
                       f"<span class='tag {kind}'>{label}</span><br>"
                       f"{_esc(text)}</div>")
        if section.truncated:
            blocks += ("<p class='muted'>Diff truncated — this section changed "
                       "too extensively to render in full. Read it in the "
                       "filing.</p>")
        details += (
            f"<details><summary>Item {_esc(section.item)} — "
            f"{_esc(section.title)} ({section.percent_changed * 100:.0f}% changed)"
            f"</summary><div class='body'>{blocks}</div></details>")

    notes = "".join(f"<div class='callout warn'>{_esc(n)}</div>"
                    for n in comparison.notes)

    return (
        "<section><h2><span class='n'>4</span>What management changed</h2>"
        "<p class='lede'>Companies write each 10-K by editing the last one, so "
        "whatever they changed is what they decided needed changing. Sections "
        "are ordered most-changed first.</p>"
        f"<div class='callout'>Comparing the year to "
        f"<strong>{_esc(comparison.current.report_date)}</strong> "
        f"(filed {_esc(comparison.current.filing_date)}) against the year to "
        f"<strong>{_esc(comparison.previous.report_date)}</strong>. "
        f"Overall, {comparison.overall_change * 100:.0f}% of the compared text "
        f"changed.</div>"
        f"<table><tr><th>Section</th><th class='num'>Changed</th>"
        f"<th class='num'>Words added</th><th class='num'>Words cut</th></tr>"
        f"{rows}</table>{details}{notes}</section>"
    )


def _column_labels(company) -> list[tuple[str, str]]:
    """(heading, sub-heading) per column — fiscal years plus TTM."""
    labels: list[tuple[str, str]] = []
    for column in company.columns:
        if isinstance(column, str):          # the trailing-twelve-month column
            as_of = (company.ttm.as_of.strftime("%b %d, %Y")
                     if company.ttm is not None else "")
            labels.append((column, f"{as_of} · unaudited"))
        else:
            end = company.period_ends.get(column)
            labels.append((f"FY{column}",
                           end.strftime("%b %d, %Y") if end else ""))
    return labels


def _model_section(company, metric_frame) -> str:
    """The full model as readable tables, so a spreadsheet app is optional.

    The .xlsx is the right format for BUILDING on -- a forecast, your own
    columns. But reading it shouldn't require owning Excel, and for most
    questions reading is all you need. These are the same numbers, in the
    browser, collapsed so they don't bury the analysis above.
    """
    from . import taxonomy
    from .metrics import DAYS, METRIC_SPECS, MONEY, PCT, RATIO, TIMES

    columns = company.columns
    labels = _column_labels(company)
    header = "".join(f"<th class='num'>{_esc(head)}</th>" for head, _sub in labels)

    def money(value):
        if value is None or pd.isna(value):
            return "—"
        return f"{value / 1_000_000:,.0f}"

    blocks = ""
    for statement in (taxonomy.INCOME_STATEMENT, taxonomy.BALANCE_SHEET,
                      taxonomy.CASH_FLOW):
        rows = ""
        for item in taxonomy.items_for(statement):
            if item.key not in company.statements.index:
                continue
            series = company.statements.loc[item.key]
            if series.isna().all():
                continue
            cells = ""
            for column in columns:
                value = series.get(column)
                if item.key == "eps_diluted":
                    shown = "—" if pd.isna(value) else f"{value:,.2f}"
                else:
                    shown = money(value)
                cells += f"<td class='num'>{shown}</td>"
            rows += f"<tr><td>{_esc(item.label)}</td>{cells}</tr>"
        if rows:
            blocks += (
                f"<details><summary>{_esc(statement)}</summary>"
                f"<div class='body'><div class='scroll'><table>"
                f"<tr><th>$ in millions</th>{header}</tr>{rows}"
                f"</table></div></div></details>")

    # Metrics, grouped the same way as the workbook.
    rows = ""
    current_group = None
    for spec in METRIC_SPECS:
        if spec.key not in metric_frame.index:
            continue
        series = metric_frame.loc[spec.key]
        if series.isna().all():
            continue
        if spec.group != current_group:
            current_group = spec.group
            rows += (f"<tr><td colspan='{len(columns) + 1}' "
                     f"class='grp'>{_esc(spec.group)}</td></tr>")
        cells = ""
        for column in columns:
            value = series.get(column)
            if value is None or pd.isna(value):
                shown = "—"
            elif spec.fmt == PCT:
                shown = f"{value * 100:,.1f}%"
            elif spec.fmt == TIMES:
                shown = f"{value:,.2f}x"
            elif spec.fmt == DAYS:
                shown = f"{value:,.0f}"
            elif spec.fmt == MONEY:
                shown = money(value)
            else:
                shown = f"{value:,.2f}"
            cells += f"<td class='num'>{shown}</td>"
        rows += f"<tr><td>{_esc(spec.label)}</td>{cells}</tr>"
    if rows:
        blocks += (f"<details><summary>Metrics</summary><div class='body'>"
                   f"<div class='scroll'><table><tr><th>Metric</th>{header}</tr>"
                   f"{rows}</table></div></div></details>")

    # Coverage -- the audit trail, which is the part worth checking first.
    coverage_rows = ""
    for row in company.coverage:
        if row.status.startswith("OPTIONAL") or row.status == "OK":
            continue
        coverage_rows += (
            f"<tr><td>{_esc(row.label)}</td><td>{_esc(row.status)}</td>"
            f"<td class='muted'>{_esc(row.tag_used or '')}</td>"
            f"<td class='muted'>{_esc(', '.join(str(y) for y in row.years_missing))}</td></tr>")
    if coverage_rows:
        blocks += (
            "<details><summary>Data coverage — lines that aren't clean</summary>"
            "<div class='body'><p class='lede'>Everything not listed here mapped "
            "cleanly to a single tag across every year.</p>"
            "<div class='scroll'><table><tr><th>Line</th><th>Status</th>"
            f"<th>Tag used</th><th>Years missing</th></tr>{coverage_rows}"
            "</table></div></div></details>")

    return (
        "<section><h2><span class='n'>5</span>The model</h2>"
        "<p class='lede'>The same figures as the spreadsheet, readable without "
        "one. Use the .xlsx when you want to build a forecast on top; use this "
        "when you just need to look.</p>"
        f"{blocks}</section>"
    )


def _warnings_section(company) -> str:
    if not company.warnings:
        return ""
    items = "".join(f"<div class='callout warn'>{_esc(w)}</div>"
                    for w in company.warnings)
    return (
        "<section><h2><span class='n'>6</span>Data warnings</h2>"
        "<p class='lede'>Things that affect how much of the above you should "
        "trust. Read these before quoting any figure.</p>"
        f"{items}</section>"
    )


def write_report(
    company,
    valuation_result,
    redflag_report,
    filing_comparison,
    path: Path,
    workbook_name: str = "",
    multiples_report=None,
    metric_frame=None,
) -> Path:
    """Render the brief for one company."""
    ttm = (f" · data through {company.ttm.as_of.strftime('%b %d, %Y')}"
           if company.ttm is not None else "")
    industry = f" · {_esc(company.industry)}" if company.industry else ""

    parts = [
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_esc(company.ticker)} — {_esc(company.entity_name)}</title>",
        f"<style>{STYLE}</style></head><body><div class='wrap'>",
        f"<header><h1>{_esc(company.entity_name)} "
        f"<span class='muted'>({_esc(company.ticker)})</span></h1>"
        f"<div class='sub'>CIK {company.cik}{industry}{ttm}</div></header>",
        _valuation_section(valuation_result),
        _multiples_section(multiples_report),
        _redflag_section(redflag_report),
        _filing_section(filing_comparison),
        _model_section(company, metric_frame) if metric_frame is not None else "",
        _warnings_section(company),
        f"<p class='foot'>Generated {datetime.now():%d %b %Y %H:%M} from SEC "
        f"filings"
        + (f" · model: {_esc(workbook_name)}" if workbook_name else "")
        + "<br>Every figure traces to a filing. Nothing here is advice, and "
        "the judgment is still yours.</p>",
        "</div></body></html>",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")
    return path
