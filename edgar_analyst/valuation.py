"""
MODULE 4 -- reverse DCF: what the current price already assumes.

A normal DCF asks "what is this worth?", which mostly measures your own
assumptions back at you. Run it forward and you can produce any answer you
like by nudging growth a point.

A reverse DCF asks the useful question instead: taking today's price as given,
what growth does the market already require? Then you compare that number to
what the company has actually done. You are no longer arguing about value in
the abstract -- you're deciding whether one specific, falsifiable expectation
is too high or too low.

That reframing is the whole point. "I think Vistra is cheap" is an opinion.
"At $X the market needs 11% free cash flow growth for a decade from a business
that has compounded at 4%" is an argument, and the person across the table can
tell you exactly where they disagree.

HOW IT WORKS
------------
Enterprise value = market cap + net debt. We discount a growing free cash flow
stream for `forecast_years`, add a terminal value at `terminal_growth`, and
solve for the growth rate that makes the present value equal today's
enterprise value. Bisection, because present value falls monotonically as the
required growth rises -- no optimiser needed, and the answer is exact to the
tolerance we set.

WHERE THE JUDGMENT SITS
-----------------------
Three assumptions drive everything, and they live in config.py so you can see
and defend them rather than discover them later:

  WACC (default 9%)             the discount rate
  terminal growth (2.5%)        perpetual growth after the forecast window
  forecast years (10)           how long the explicit period runs

None of those are facts. A different analyst would pick differently and get a
different implied growth, which is why the output includes a sensitivity grid
instead of one number pretending to be precise. If the conclusion flips inside
a plausible range of assumptions, you don't have a thesis yet.

THE BASE MATTERS MOST
---------------------
Implied growth is measured off a starting free cash flow. If that base year is
unusually good or bad the answer is distorted, so the result reports which
period it used and how that compares to the multi-year average. A company
whose base year is double its normal cash generation will look like the market
expects nothing from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import config
from .normalize import NormalizedCompany

# Search bounds for implied growth. Outside this the answer isn't meaningful
# anyway -- a business cannot compound at 100% for a decade.
MIN_GROWTH, MAX_GROWTH = -0.50, 1.00
SOLVER_TOLERANCE = 1e-7
SOLVER_MAX_ITERATIONS = 200


@dataclass
class ValuationResult:
    ticker: str
    price: float
    price_source: str
    price_as_of: str
    shares: float
    market_cap: float
    net_debt: float
    enterprise_value: float

    base_fcf: float
    base_period: str
    base_vs_average: float | None       # base FCF ÷ average FCF of last 5 years

    wacc: float
    terminal_growth: float
    forecast_years: int

    implied_growth: float | None
    implied_note: str = ""

    # What the company has actually done, for comparison.
    historical_fcf_cagr: float | None = None
    historical_revenue_cagr: float | None = None

    # Multiples, because they're the quickest sanity check on everything above.
    ev_to_ebitda: float | None = None
    ev_to_fcf: float | None = None
    fcf_yield: float | None = None
    pe_ratio: float | None = None

    sensitivity: pd.DataFrame | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """How the market's requirement compares to the company's track record."""
        if self.implied_growth is None or self.historical_fcf_cagr is None:
            return ""
        gap = self.implied_growth - self.historical_fcf_cagr
        if gap > 0.05:
            return "Price requires materially FASTER growth than history"
        if gap < -0.05:
            return "Price requires SLOWER growth than history"
        return "Price roughly in line with historical growth"


def _present_value(fcf0: float, growth: float, wacc: float,
                   terminal_growth: float, years: int) -> float:
    """PV of a growing cash flow stream plus its terminal value."""
    if wacc <= terminal_growth:
        raise ValueError("WACC must exceed terminal growth, or value is infinite.")
    total = 0.0
    cash_flow = fcf0
    for year in range(1, years + 1):
        cash_flow = fcf0 * (1 + growth) ** year
        total += cash_flow / (1 + wacc) ** year
    terminal = cash_flow * (1 + terminal_growth) / (wacc - terminal_growth)
    total += terminal / (1 + wacc) ** years
    return total


def solve_implied_growth(target_value: float, fcf0: float, wacc: float,
                         terminal_growth: float, years: int) -> float | None:
    """Growth rate that makes the DCF equal `target_value`.

    Present value increases monotonically with growth, so bisection is exact
    and can't land on a local minimum the way an optimiser might.
    """
    if fcf0 <= 0 or target_value <= 0:
        return None
    try:
        low_value = _present_value(fcf0, MIN_GROWTH, wacc, terminal_growth, years)
        high_value = _present_value(fcf0, MAX_GROWTH, wacc, terminal_growth, years)
    except ValueError:
        return None
    if not (low_value <= target_value <= high_value):
        return None  # the price implies something outside the searchable range

    low, high = MIN_GROWTH, MAX_GROWTH
    for _ in range(SOLVER_MAX_ITERATIONS):
        middle = (low + high) / 2
        value = _present_value(fcf0, middle, wacc, terminal_growth, years)
        if abs(value - target_value) / target_value < SOLVER_TOLERANCE:
            return middle
        if value < target_value:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _latest(company: NormalizedCompany, key: str) -> float | None:
    """Most recent value for a line item, preferring the TTM column."""
    for column in reversed(company.columns):
        try:
            value = company.statements.at[key, column]
        except KeyError:
            continue
        if not pd.isna(value):
            return float(value)
    return None


def _cagr(series: pd.Series) -> float | None:
    clean = series.dropna()
    if len(clean) < 3:
        return None
    first, last = float(clean.iloc[0]), float(clean.iloc[-1])
    if first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / (len(clean) - 1)) - 1


def evaluate(
    company: NormalizedCompany,
    metric_frame: pd.DataFrame,
    price: float,
    shares: float | None = None,
    price_source: str = "",
    price_as_of: str = "",
    wacc: float = config.DEFAULT_WACC,
    terminal_growth: float = config.DEFAULT_TERMINAL_GROWTH,
    forecast_years: int = config.DEFAULT_FORECAST_YEARS,
) -> ValuationResult | None:
    """Reverse-engineer the growth the price implies. None if inputs are missing."""
    warnings: list[str] = []

    # Share count for market cap. The cover-page count is a point-in-time
    # figure, which is what you want; diluted weighted-average shares are an
    # average over the period and would misstate market cap.
    if shares is None:
        shares = _latest(company, "shares_outstanding")
        if shares is None:
            shares = _latest(company, "shares_diluted")
            if shares is not None:
                warnings.append(
                    "No cover-page share count available, so market cap uses "
                    "weighted-average diluted shares -- a period average rather "
                    "than a point-in-time count. Market cap is approximate."
                )
    if not shares or shares <= 0:
        return None

    market_cap = price * shares

    # Net debt comes from the metrics frame, which is where it is defined --
    # short-term debt plus long-term debt less cash and short-term investments,
    # with the lease treatment set in metrics.py. Recomputing it here from the
    # raw statements would risk a second, quietly different definition of the
    # same quantity, and enterprise value is exactly the wrong place to have
    # one: for a leveraged business the debt is most of the value.
    net_debt = None
    if "net_debt" in metric_frame.index:
        for column in reversed(company.columns):
            try:
                value = metric_frame.at["net_debt", column]
            except KeyError:
                continue
            if not pd.isna(value):
                net_debt = float(value)
                break
    if net_debt is None:
        short_term = _latest(company, "short_term_debt") or 0.0
        long_term = _latest(company, "long_term_debt") or 0.0
        cash = (_latest(company, "cash") or 0.0) \
            + (_latest(company, "short_term_investments") or 0.0)
        net_debt = short_term + long_term - cash
        warnings.append(
            "Net debt was rebuilt from the balance sheet because the metrics "
            "frame had none. Check the debt lines on the Balance Sheet tab."
        )
    enterprise_value = market_cap + net_debt

    # Base free cash flow, preferring the trailing twelve months.
    base_period = company.ttm_column if company.ttm_column else company.fiscal_years[-1]
    base_fcf = None
    if "free_cash_flow" in metric_frame.index:
        try:
            value = metric_frame.at["free_cash_flow", base_period]
            base_fcf = None if pd.isna(value) else float(value)
        except KeyError:
            base_fcf = None
    if base_fcf is None:
        for column in reversed(company.columns):
            try:
                value = metric_frame.at["free_cash_flow", column]
            except KeyError:
                continue
            if not pd.isna(value):
                base_fcf, base_period = float(value), column
                break
    if base_fcf is None:
        return None

    # How normal is the base year? This is the single biggest distortion in a
    # reverse DCF and the easiest to miss.
    fcf_series = metric_frame.loc["free_cash_flow", company.fiscal_years].dropna() \
        if "free_cash_flow" in metric_frame.index else pd.Series(dtype=float)
    recent_average = float(fcf_series.tail(5).mean()) if len(fcf_series) else None
    base_vs_average = (base_fcf / recent_average
                       if recent_average and recent_average > 0 else None)
    if base_vs_average is not None and (base_vs_average > 1.5 or base_vs_average < 0.6):
        warnings.append(
            f"Base free cash flow is {base_vs_average:.1f}x the five-year average. "
            f"Implied growth is measured off that base, so an unusually "
            f"{'strong' if base_vs_average > 1 else 'weak'} period makes the "
            f"market look like it expects "
            f"{'less' if base_vs_average > 1 else 'more'} than it does. Consider "
            f"re-running against a normalised figure."
        )

    # A reverse DCF is only as meaningful as its base. Two situations make the
    # implied growth arithmetically correct and analytically worthless, and
    # both are common in capital-intensive businesses:
    #
    #   1. The base is tiny next to enterprise value. Constellation's trailing
    #      free cash flow is $0.3bn against a $129bn enterprise value, so the
    #      model has to find 48% compound growth to bridge the gap -- a number
    #      that says nothing about the business and everything about dividing
    #      by a small denominator.
    #   2. Free cash flow has usually been negative. A company part-way through
    #      building generating capacity isn't valued on current cash generation
    #      at all, so solving for the growth in it answers the wrong question.
    #
    # Say so rather than print a confident percentage.
    if base_fcf > 0 and enterprise_value > 0:
        yield_on_ev = base_fcf / enterprise_value
        if yield_on_ev < 0.01:
            warnings.append(
                f"Base free cash flow is only {yield_on_ev:.2%} of enterprise "
                f"value, so implied growth is extremely sensitive to it -- a 10% "
                f"change in the base moves the answer by several percentage "
                f"points. Treat the number below as a rough magnitude at best, "
                f"and prefer EV/EBITDA for this company."
            )
    negative_years = int((fcf_series < 0).sum()) if len(fcf_series) else 0
    if len(fcf_series) >= 4 and negative_years >= len(fcf_series) / 2:
        warnings.append(
            f"Free cash flow was negative in {negative_years} of the last "
            f"{len(fcf_series)} fiscal years. A company in a heavy build phase "
            f"isn't valued on current cash generation, so a free-cash-flow "
            f"reverse DCF is the wrong lens here. Look at what the capacity "
            f"being built is expected to earn instead."
        )

    if base_fcf <= 0:
        warnings.append(
            "Free cash flow is negative, so there is no growth rate that makes a "
            "discounted cash flow equal the price. The market is valuing "
            "something other than current cash generation -- future profitability, "
            "assets, or optionality. A reverse DCF can't speak to that."
        )
        implied, note = None, "negative base free cash flow"
    else:
        implied = solve_implied_growth(enterprise_value, base_fcf, wacc,
                                       terminal_growth, forecast_years)
        note = "" if implied is not None else (
            f"The price implies growth outside the searchable range "
            f"({MIN_GROWTH:.0%} to {MAX_GROWTH:.0%}). Usually means the base "
            f"cash flow is tiny relative to the enterprise value."
        )

    result = ValuationResult(
        ticker=company.ticker,
        price=price,
        price_source=price_source,
        price_as_of=price_as_of,
        shares=shares,
        market_cap=market_cap,
        net_debt=net_debt,
        enterprise_value=enterprise_value,
        base_fcf=base_fcf,
        base_period=str(base_period),
        base_vs_average=base_vs_average,
        wacc=wacc,
        terminal_growth=terminal_growth,
        forecast_years=forecast_years,
        implied_growth=implied,
        implied_note=note,
        warnings=warnings,
    )

    # What the business has actually delivered.
    if "free_cash_flow" in metric_frame.index:
        result.historical_fcf_cagr = _cagr(
            metric_frame.loc["free_cash_flow", company.fiscal_years])
    result.historical_revenue_cagr = _cagr(
        company.statements.loc["revenue", company.fiscal_years])

    # Multiples -- the fastest cross-check on everything above.
    ebitda = None
    if "ebitda" in metric_frame.index:
        try:
            value = metric_frame.at["ebitda", base_period]
            ebitda = None if pd.isna(value) else float(value)
        except KeyError:
            ebitda = None
    if ebitda and ebitda > 0:
        result.ev_to_ebitda = enterprise_value / ebitda
    if base_fcf > 0:
        result.ev_to_fcf = enterprise_value / base_fcf
        result.fcf_yield = base_fcf / market_cap
    net_income = None
    try:
        net_income = company.statements.at["net_income", base_period]
        net_income = None if pd.isna(net_income) else float(net_income)
    except KeyError:
        net_income = None
    if net_income and net_income > 0:
        result.pe_ratio = market_cap / net_income

    result.sensitivity = _sensitivity_grid(
        enterprise_value, base_fcf, wacc, terminal_growth, forecast_years)
    return result


def _sensitivity_grid(enterprise_value: float, base_fcf: float, wacc: float,
                      terminal_growth: float, years: int) -> pd.DataFrame | None:
    """Implied growth across a range of discount and terminal-growth rates.

    One implied growth number looks far more precise than it is. The grid shows
    how much of the conclusion is the market's expectation and how much is your
    own choice of discount rate -- and if the answer flips inside this range,
    the thesis rests on the assumption rather than the company.
    """
    if base_fcf <= 0:
        return None
    waccs = [round(wacc + step, 4) for step in (-0.02, -0.01, 0, 0.01, 0.02)]
    terminals = [round(terminal_growth + step, 4) for step in (-0.01, -0.005, 0, 0.005, 0.01)]

    data = {}
    for terminal in terminals:
        column = []
        for rate in waccs:
            if rate <= terminal:
                column.append(None)
                continue
            column.append(solve_implied_growth(
                enterprise_value, base_fcf, rate, terminal, years))
        data[f"{terminal:.1%}"] = column
    frame = pd.DataFrame(data, index=[f"{rate:.1%}" for rate in waccs])
    frame.index.name = "WACC \\ terminal growth"
    return frame
