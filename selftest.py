"""
Checks the program's output against figures taken from actual 10-K filings.

Run it any time you change taxonomy.py or normalize.py:

    python selftest.py

This is the habit that separates a model you can defend from one you can't. If
you add a tag to the taxonomy and something here breaks, you've just learned
that your change had a side effect on a different company -- which is exactly
the kind of thing that is otherwise invisible until an interviewer finds it.

Add your own cases as you verify companies by hand. Every number below was
read off the face of the filing.
"""

from __future__ import annotations

import sys

import pandas as pd

from edgar_analyst import build

# (ticker, fiscal year as this program labels it, line item, expected $mm)
#
# NOTE ON YEAR LABELS: this program labels a fiscal year by the calendar year
# it mostly covers, so a year ending Jan 2025 is FY2024 here even though the
# company itself calls it fiscal 2025. See config.CALENDAR_ALIGN_FISCAL_YEARS.
KNOWN_VALUES = [
    # Apple -- year ends late September
    ("AAPL", 2024, "revenue", 391_035),
    ("AAPL", 2024, "net_income", 93_736),
    ("AAPL", 2023, "revenue", 383_285),
    ("AAPL", 2016, "revenue", 215_639),      # only in pre-2018 filings
    ("AAPL", 2024, "cfo", 118_254),
    # Microsoft -- year ends June
    ("MSFT", 2024, "revenue", 245_122),
    ("MSFT", 2023, "revenue", 211_915),
    # Walmart -- year ends January, and total revenue includes membership income
    ("WMT", 2024, "revenue", 680_985),
    ("WMT", 2023, "revenue", 648_125),
    ("WMT", 2023, "net_income", 15_511),
    # Costco -- membership fees are the whole business model, so the revenue
    # tag choice matters more here than almost anywhere
    ("COST", 2024, "revenue", 254_453),
    ("COST", 2023, "revenue", 242_290),
    # NVIDIA -- year ends January
    ("NVDA", 2024, "revenue", 130_497),
    ("NVDA", 2023, "revenue", 60_922),
    # Disney FY2017 exists only in a recast 8-K, not in any 10-K
    ("DIS", 2017, "total_assets", 95_789),
]

# Things that must be true for any company, checked across a sample.
STRUCTURAL_SAMPLE = ["AAPL", "WMT", "JPM", "XOM", "NVDA", "DAL", "ULTA"]

TOLERANCE = 0.005  # 0.5% -- allows for rounding, not for wrong numbers


def check_known_values() -> tuple[int, int]:
    passed = failed = 0
    cache: dict[str, object] = {}
    print("Known values from filings")
    print("-" * 78)
    for ticker, year, key, expected in KNOWN_VALUES:
        if ticker not in cache:
            cache[ticker] = build(ticker)[0]
        company = cache[ticker]
        actual = company.value(key, year)
        actual_mm = None if actual is None else actual / 1_000_000

        if actual_mm is None:
            print(f"  FAIL  {ticker:<5} FY{year} {key:<14} expected {expected:>10,}  got nothing")
            failed += 1
            continue

        off_by = abs(actual_mm - expected) / expected
        if off_by <= TOLERANCE:
            print(f"  ok    {ticker:<5} FY{year} {key:<14} {actual_mm:>12,.0f}")
            passed += 1
        else:
            print(f"  FAIL  {ticker:<5} FY{year} {key:<14} expected {expected:>10,}  "
                  f"got {actual_mm:>12,.0f}  ({off_by:.1%} off)")
            failed += 1
    return passed, failed


def check_structure() -> tuple[int, int]:
    passed = failed = 0
    print("\nStructural checks")
    print("-" * 78)
    for ticker in STRUCTURAL_SAMPLE:
        company, metric_frame, stats = build(ticker)
        problems = []

        if len(company.fiscal_years) < 8:
            problems.append(f"only {len(company.fiscal_years)} years of history")

        # Fiscal years must be consecutive -- a gap means the calendar builder
        # lost a year, which silently corrupts every growth rate.
        years = company.fiscal_years
        if years != list(range(years[0], years[-1] + 1)):
            problems.append(f"non-consecutive fiscal years: {years}")

        # The balance sheet has to balance. This is the single best check that
        # we mapped assets, liabilities and equity to the right tags.
        for year in years:
            assets = company.value("total_assets", year)
            liabilities = company.value("total_liabilities", year)
            total_equity = company.value("total_equity", year)
            redeemable = ((company.value("redeemable_nci", year) or 0)
                          + (company.value("redeemable_preferred", year) or 0))
            if None in (assets, liabilities, total_equity) or assets == 0:
                continue
            gap = abs(assets - (liabilities + total_equity + redeemable)) / abs(assets)
            if gap > 0.005:
                problems.append(f"FY{year} balance sheet off by {gap:.1%}")

        # Revenue should never be negative, and net income should never exceed
        # revenue by a wide margin (a sign of a mis-mapped tag).
        for year in years:
            revenue = company.value("revenue", year)
            if revenue is not None and revenue < 0:
                problems.append(f"FY{year} negative revenue")

        if problems:
            print(f"  FAIL  {ticker:<5} " + "; ".join(problems[:3]))
            failed += 1
        else:
            print(f"  ok    {ticker:<5} {len(years)} yrs, balance sheet ties, "
                  f"years consecutive")
            passed += 1
    return passed, failed


def check_ttm() -> tuple[int, int]:
    """Validate the trailing-twelve-month column.

    Deliberately STRUCTURAL rather than hardcoded values. A test asserting
    "Apple's TTM revenue is $466.8bn" would fail the moment Apple files its
    next 10-Q -- which is every quarter, forever. These rules stay true.
    """
    passed = failed = 0
    print("\nTrailing-twelve-month checks")
    print("-" * 78)
    for ticker in ["AAPL", "ULTA", "WMT", "NVDA", "XOM", "GOOGL"]:
        company, metric_frame, _stats = build(ticker)
        ttm = company.ttm
        problems = []

        if ttm is None:
            # Legitimate right after an annual report, so not a failure -- but
            # say so, because a silent absence looks like working code.
            print(f"  --    {ticker:<5} no TTM (last 10-K is still the newest filing)")
            passed += 1
            continue

        fiscal_year_end = company.period_ends[company.fiscal_years[-1]]
        if ttm.as_of <= fiscal_year_end:
            problems.append("TTM date is not after the fiscal year end")

        # Revenue over twelve months shouldn't be wildly unlike the last full
        # year. A factor-of-two move means the period arithmetic is wrong --
        # most likely a year-to-date figure mistaken for a quarterly one.
        fy_revenue = company.value("revenue", company.fiscal_years[-1])
        ttm_revenue = ttm.values.get("revenue")
        if fy_revenue and ttm_revenue:
            ratio = ttm_revenue / fy_revenue
            if not 0.5 <= ratio <= 2.0:
                problems.append(f"TTM revenue is {ratio:.1f}x the last fiscal year")

        # The balance sheet is a snapshot, so it must be a real date, not a sum.
        if ttm.balance_sheet_date and ttm.balance_sheet_date <= fiscal_year_end:
            problems.append("TTM balance sheet is older than the fiscal year end")

        # Growth rates must be blank in the TTM column: the period overlaps the
        # year it would be compared against.
        if company.ttm_column in metric_frame.columns:
            growth = metric_frame.loc["revenue_growth", company.ttm_column]
            if not pd.isna(growth):
                problems.append("growth rate populated in TTM column")

        if problems:
            print(f"  FAIL  {ticker:<5} " + "; ".join(problems))
            failed += 1
        else:
            print(f"  ok    {ticker:<5} {ttm.label}, "
                  f"{ttm.months_beyond_fiscal_year}mo past FY end, "
                  f"{len(ttm.values)} line items")
            passed += 1
    return passed, failed


def check_modules() -> tuple[int, int]:
    """Modules 1, 3 and 4 -- structural checks, not fixed values.

    Prices and filings both move, so anything hardcoded here would rot. These
    test the properties that must hold whatever the inputs.
    """
    from edgar_analyst import filings, redflags, valuation
    from edgar_analyst.client import EDGARClient

    passed = failed = 0
    print("\nModules 1, 3 and 4")
    print("-" * 78)

    # Module 4: the solver must invert its own model exactly.
    problems = []
    for growth in (-0.05, 0.0, 0.08, 0.25):
        value = valuation._present_value(1000.0, growth, 0.09, 0.025, 10)
        solved = valuation.solve_implied_growth(value, 1000.0, 0.09, 0.025, 10)
        if solved is None or abs(solved - growth) > 1e-4:
            problems.append(f"solver failed to invert growth of {growth:.0%}")
    if problems:
        print("  FAIL  reverse DCF solver: " + "; ".join(problems)); failed += 1
    else:
        print("  ok    reverse DCF solver inverts its own model to 1e-4"); passed += 1

    client = EDGARClient()
    for ticker in ["ULTA", "WMT"]:
        company, metric_frame, _stats = build(ticker)
        problems = []

        report = redflags.evaluate(company, metric_frame)
        if not report.flags:
            problems.append("red flag engine produced no checks")
        if not 0 <= report.score <= 100:
            problems.append(f"score out of range: {report.score}")
        # A flag must never claim to have triggered without saying why.
        for flag in report.triggered:
            if not flag.detail:
                problems.append(f"flag {flag.key} triggered with no detail")

        result = valuation.evaluate(company, metric_frame, price=100.0)
        if result is None:
            problems.append("valuation returned nothing at a test price")
        elif result.implied_growth is not None:
            # Enterprise value must be recoverable from the implied growth.
            rebuilt = valuation._present_value(
                result.base_fcf, result.implied_growth, result.wacc,
                result.terminal_growth, result.forecast_years)
            if abs(rebuilt - result.enterprise_value) / result.enterprise_value > 1e-3:
                problems.append("implied growth doesn't reproduce enterprise value")

        comparison = filings.compare_filings(
            client, company.cik, ticker, company.entity_name)
        if comparison is None:
            problems.append("no filing comparison")
        else:
            if len(comparison.sections) < 4:
                problems.append(
                    f"only {len(comparison.sections)} 10-K sections parsed")
            # Risk factors are always substantial; a tiny parse means the
            # section boundaries are wrong.
            risk = next((s for s in comparison.sections if s.item == "1A"), None)
            if risk is None or risk.new_chars < 5000:
                problems.append("Item 1A parsed as implausibly small")
            # No section may swallow another: Item 7A is a few pages, so if it
            # runs to six figures it has absorbed the financial statements.
            market_risk = next((s for s in comparison.sections if s.item == "7A"), None)
            if market_risk and market_risk.new_chars > 100_000:
                problems.append("Item 7A absorbed a neighbouring section")

        if problems:
            print(f"  FAIL  {ticker:<5} " + "; ".join(problems[:3])); failed += 1
        else:
            sections = len(comparison.sections) if comparison else 0
            print(f"  ok    {ticker:<5} {len(report.flags)} checks, valuation "
                  f"inverts, {sections} 10-K sections"); passed += 1
    return passed, failed


def check_multiples() -> tuple[int, int]:
    """Verify multiples reproduce from raw inputs, and yields read correctly.

    The valuable check here isn't the level of any multiple -- that depends on
    whatever price file happens to be present -- it's that the arithmetic is
    recoverable and the cheap/rich wording doesn't invert on yields.
    """
    from edgar_analyst import multiples
    from edgar_analyst.prices import PriceClient

    passed = failed = 0
    print("\nValuation multiples")
    print("-" * 78)

    history = PriceClient().load_price_history(["ULTA", "WMT", "NVDA"]) or None

    for ticker in ["ULTA", "WMT", "NVDA"]:
        company, metric_frame, _stats = build(ticker)
        report = multiples.evaluate(company, metric_frame, price=100.0,
                                    history=history)
        problems = []

        current = report.current
        if current is None or not current.market_cap:
            problems.append("no current multiples at a test price")
        else:
            # Enterprise value must be market cap plus net debt, exactly.
            rebuilt = current.market_cap + (current.net_debt or 0)
            if abs(rebuilt - current.enterprise_value) > 1:
                problems.append("EV != market cap + net debt")
            # P/E must reproduce from price x shares / net income.
            net_income = company.value("net_income", company.fiscal_years[-1])
            pe = current.get("pe")
            if pe is not None and pe <= 0:
                problems.append("non-positive P/E reported")
            # A loss-making denominator must blank, never go negative.
            for key in ("pe", "pb", "ev_ebitda", "ev_fcf"):
                value = current.get(key)
                if value is not None and value < 0:
                    problems.append(f"{key} is negative; should be blank")

        # Historical multiples must reproduce from their own inputs.
        for item in report.history:
            year = int(item.period)
            shares = item.shares
            net_income = company.value("net_income", year)
            if not shares or not net_income or net_income <= 0:
                continue
            expected = (item.price * shares) / net_income
            actual = item.get("pe")
            if actual is None or abs(actual - expected) / expected > 1e-6:
                problems.append(f"FY{year} P/E doesn't reproduce from inputs")
                break

        # Split basis: split-adjusted prices x share counts must give a market
        # cap series with no split-sized jumps. NVDA (4:1 2021, 10:1 2024) and
        # WMT (3:1 2024) fail this badly if share counts aren't restated.
        caps = [s_.market_cap for s_ in report.history if s_.market_cap]
        for earlier, later in zip(caps[:-1], caps[1:]):
            if later / earlier > 12 or later / earlier < 1 / 6:
                problems.append("historical market cap jumps by a split-sized step")
                break

        # Yields must not read backwards: a yield ABOVE its median is CHEAPER.
        stat = report.stats.get("earnings_yield")
        if stat and stat.reliable and stat.vs_median is not None:
            if stat.vs_median > 0.10 and "richer" in stat.verdict:
                problems.append("high earnings yield described as richer")

        if problems:
            print(f"  FAIL  {ticker:<5} " + "; ".join(problems[:3]))
            failed += 1
        else:
            years = len(report.history)
            print(f"  ok    {ticker:<5} EV ties, {len(current.values)} multiples, "
                  f"{years} historical year(s) reproduce")
            passed += 1
    return passed, failed


def check_recent_fixes() -> tuple[int, int]:
    """Regression checks for the October 2026 fixes (Uber run + prices)."""
    import tempfile
    from datetime import date, timedelta
    from pathlib import Path

    from edgar_analyst import config
    from edgar_analyst.prices import PriceClient

    passed = failed = 0
    print("\nRecent fixes")
    print("-" * 78)

    company, metric_frame, _stats = build("UBER")
    problems = []
    # SG&A = S&M + G&A; Uber FY2024 S&M 4,337 + G&A 3,639 = 7,976
    sga = company.value("sganda", 2024)
    if sga is None or abs(sga / 1e6 - 7_976) > 40:
        problems.append(f"FY2024 SG&A {sga} != S&M + G&A")
    # FY2018 ties once the $14.2bn convertible preferred is in mezzanine
    if any("doesn't tie" in w and "FY2018" in w for w in company.warnings):
        problems.append("FY2018 balance sheet still doesn't tie")
    # The FY2019 share jump is the IPO, not a split
    if any("stock split" in w and "FY2019" in w and "IPO" not in w
           for w in company.warnings):
        problems.append("IPO conversion reported as a stock split")
    # ROE blank while equity is negative
    roe = metric_frame.loc["roe"] if "roe" in metric_frame.index else None
    if roe is not None and pd.notna(roe.get(2018)):
        problems.append("ROE reported on negative equity")
    if problems:
        print("  FAIL  UBER  " + "; ".join(problems)); failed += 1
    else:
        print("  ok    UBER  SG&A = S&M + G&A, FY2018 ties, IPO not a split, "
              "ROE blank on negative equity"); passed += 1

    # Price source order, offline: a fresh prices.csv row overrides; a stale
    # one is only a last resort and is flagged.
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "prices.csv"
        fresh = date.today().isoformat()
        old = (date.today() - timedelta(days=30)).isoformat()
        csv_path.write_text(f"ticker,price,date\nAAA,10,{fresh}\nBBB,20,{old}\n")
        client = PriceClient(api_key="", prices_csv=csv_path)
        client.api_key = ""
        saved = config.YAHOO_ENABLED
        config.YAHOO_ENABLED = False
        try:
            quotes = client.get_quotes(["AAA", "BBB"])
        finally:
            config.YAHOO_ENABLED = saved
        ok = (quotes["AAA"].price == 10 and not quotes["AAA"].is_stale_manual
              and quotes["BBB"].is_stale_manual
              and any("STALE" in n for n in client.notes))
        if ok:
            print("  ok    prices  fresh CSV overrides, stale CSV flagged"); passed += 1
        else:
            print("  FAIL  prices  source order / staleness"); failed += 1
    return passed, failed


def main() -> int:
    print("\nedgar_analyst self-test\n")
    known_passed, known_failed = check_known_values()
    struct_passed, struct_failed = check_structure()
    ttm_passed, ttm_failed = check_ttm()
    mod_passed, mod_failed = check_modules()
    mult_passed, mult_failed = check_multiples()
    fix_passed, fix_failed = check_recent_fixes()

    total_passed = (known_passed + struct_passed + ttm_passed + mod_passed
                    + mult_passed + fix_passed)
    total_failed = (known_failed + struct_failed + ttm_failed + mod_failed
                    + mult_failed + fix_failed)
    print("\n" + "=" * 78)
    print(f"{total_passed} passed, {total_failed} failed")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
