"""
Valuation multiples -- what you're paying, and whether that's normal.

TWO HALVES, AND THE SECOND IS THE POINT
---------------------------------------
The first half is arithmetic: price and share count give market cap, add net
debt for enterprise value, divide by earnings, sales, EBITDA, book value and
cash flow. Every terminal does this and nobody is impressed by it.

The second half is the part that actually settles arguments. "Vistra trades at
11.4x EV/EBITDA" is a fact with no content -- 11x is cheap for a software
company and expensive for a utility. What you need is:

    11.4x today, against a five-year median of 14.2x for THIS company
    -- 20% below its own history, in the 15th percentile of that range.

That reframes the question from "is this cheap" (unanswerable in the abstract)
to "why is the market paying less for this business than it usually does"
(answerable, and the answer is your thesis). It's also the honest way to catch
yourself: a stock that looks cheap on an absolute multiple but sits at its own
90th percentile isn't cheap, it's a business whose multiple has re-rated down
permanently and you're anchoring on the wrong history.

Doing that needs a price for each PAST year end, not just today's -- which is
why price_history.csv exists alongside prices.csv.

THE CAVEATS THAT MATTER
-----------------------
- Historical market cap uses each year's WEIGHTED AVERAGE DILUTED share count,
  because that's what filings give per period. A company that bought back
  heavily mid-year will have a slightly overstated historical market cap. The
  direction of the error is knowable; the size is small for most companies and
  material for aggressive repurchasers.
- Multiples on a loss-making year are meaningless, so negative denominators
  produce a blank rather than a negative multiple. A negative P/E is not a
  cheap stock.
- A five-year median off five observations is a weak statistic. The output
  reports how many years it had, and you should distrust anything built on
  fewer than four.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .normalize import NormalizedCompany

# Multiples we compute, with how to read them and whether low = cheap.
# `lower_is_cheaper` drives the "vs history" language so a yield doesn't get
# described backwards.
# key, label, unit, lower_is_cheaper, judged
# `judged` False means the row is shown but gets no cheap/rich read -- it is a
# measure of something other than valuation and labelling it "cheap" is wrong.
SPEC: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("pe",              "P / E",                    "x", True,  True),
    ("pb",              "P / Book",                 "x", True,  True),
    ("ptbv",            "P / Tangible Book",        "x", True,  True),
    ("ps",              "P / Sales",                "x", True,  True),
    ("ev_sales",        "EV / Sales",               "x", True,  True),
    ("ev_ebitda",       "EV / EBITDA",              "x", True,  True),
    ("ev_ebit",         "EV / EBIT",                "x", True,  True),
    ("ev_fcf",          "EV / Free Cash Flow",      "x", True,  True),
    ("ev_ic",           "EV / Invested Capital",    "x", True,  True),
    ("earnings_yield",  "Earnings Yield",           "%", False, True),
    ("fcf_yield",       "FCF Yield (on mkt cap)",   "%", False, True),
    ("fcf_yield_ev",    "FCF Yield (on EV)",        "%", False, True),
    ("dividend_yield",  "Dividend Yield",           "%", False, True),
    ("shareholder_yield", "Shareholder Yield",      "%", False, True),
    # Leverage, not valuation.
    ("net_debt_to_mktcap", "Net Debt / Market Cap", "x", True,  False),
)
SPEC_BY_KEY = {key: (label, unit, low, judged)
               for key, label, unit, low, judged in SPEC}

# Below this many historical observations, "vs history" is noise.
MIN_HISTORY_YEARS = 4


@dataclass
class MultipleSet:
    """Every multiple for one period."""

    period: str
    price: float | None
    shares: float | None
    market_cap: float | None
    net_debt: float | None
    enterprise_value: float | None
    values: dict[str, float | None] = field(default_factory=dict)

    def get(self, key: str) -> float | None:
        return self.values.get(key)


@dataclass
class HistoryStat:
    key: str
    current: float | None
    median: float | None
    low: float | None
    high: float | None
    observations: int
    percentile: float | None          # 0 = cheapest in range, 100 = dearest
    vs_median: float | None           # current / median - 1

    @property
    def reliable(self) -> bool:
        return self.observations >= MIN_HISTORY_YEARS

    @property
    def verdict(self) -> str:
        """Cheap or rich versus this company's own past -- never "above/below".

        For a multiple, higher means dearer. For a YIELD, higher means cheaper.
        Describing both as "above its own history" is true arithmetically and
        backwards in meaning half the time, which is exactly the kind of label
        someone repeats in a meeting without re-deriving. So the verdict states
        the valuation conclusion, not the direction of the number.
        """
        if self.current is None or self.median is None or not self.reliable:
            return ""
        _label, _unit, lower_is_cheaper, judged = SPEC_BY_KEY[self.key]
        if not judged:
            return ""
        gap = self.vs_median or 0.0
        if abs(gap) < 0.10:
            return "in line with its own history"
        rich = gap > 0 if lower_is_cheaper else gap < 0
        return "richer than its own history" if rich else "cheaper than its own history"


@dataclass
class MultiplesReport:
    ticker: str
    current: MultipleSet | None = None
    history: list[MultipleSet] = field(default_factory=list)
    stats: dict[str, HistoryStat] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def has_history(self) -> bool:
        return len(self.history) >= MIN_HISTORY_YEARS


# ---------------------------------------------------------------------------

def _at(frame: pd.DataFrame, key: str, period) -> float | None:
    if key not in frame.index:
        return None
    try:
        value = frame.at[key, period]
    except KeyError:
        return None
    return None if pd.isna(value) else float(value)


def _divide(numerator: float | None, denominator: float | None) -> float | None:
    """Ratio, blank when the denominator is zero or negative.

    A negative denominator is the important case. Dividing a positive market
    cap by negative earnings yields a negative P/E, which looks like a number
    and means nothing -- people have talked themselves into "cheap on 3x" when
    the company lost money. Blank is the honest output.
    """
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def compute_set(
    company: NormalizedCompany,
    metric_frame: pd.DataFrame,
    period,
    price: float | None,
    shares: float | None,
) -> MultipleSet:
    """Every multiple for one period at one price."""
    statements = company.statements
    result = MultipleSet(period=str(period), price=price, shares=shares,
                         market_cap=None, net_debt=None, enterprise_value=None)
    if not price or not shares or shares <= 0:
        return result

    market_cap = price * shares
    net_debt = _at(metric_frame, "net_debt", period)
    if net_debt is None:
        short_term = _at(statements, "short_term_debt", period) or 0.0
        long_term = _at(statements, "long_term_debt", period) or 0.0
        cash = (_at(statements, "cash", period) or 0.0) \
            + (_at(statements, "short_term_investments", period) or 0.0)
        net_debt = short_term + long_term - cash
    enterprise_value = market_cap + net_debt

    result.market_cap = market_cap
    result.net_debt = net_debt
    result.enterprise_value = enterprise_value

    revenue = _at(statements, "revenue", period)
    net_income = _at(statements, "net_income", period)
    equity = _at(statements, "total_equity", period) or _at(statements, "equity", period)
    goodwill = _at(statements, "goodwill", period) or 0.0
    intangibles = _at(statements, "intangibles", period) or 0.0
    ebitda = _at(metric_frame, "ebitda", period)
    ebit = _at(statements, "operating_income", period)
    fcf = _at(metric_frame, "free_cash_flow", period)
    invested_capital = _at(metric_frame, "invested_capital", period)
    dividends = _at(statements, "dividends", period) or 0.0
    buybacks = _at(statements, "buybacks", period) or 0.0

    tangible_book = None
    if equity is not None:
        tangible_book = equity - goodwill - intangibles

    values = {
        "pe": _divide(market_cap, net_income),
        "pb": _divide(market_cap, equity),
        "ptbv": _divide(market_cap, tangible_book),
        "ps": _divide(market_cap, revenue),
        "ev_sales": _divide(enterprise_value, revenue),
        "ev_ebitda": _divide(enterprise_value, ebitda),
        "ev_ebit": _divide(enterprise_value, ebit),
        "ev_fcf": _divide(enterprise_value, fcf),
        "ev_ic": _divide(enterprise_value, invested_capital),
        "earnings_yield": (net_income / market_cap
                           if net_income is not None and market_cap else None),
        "fcf_yield": (fcf / market_cap if fcf is not None and market_cap else None),
        "fcf_yield_ev": (fcf / enterprise_value
                         if fcf is not None and enterprise_value and enterprise_value > 0
                         else None),
        "dividend_yield": (dividends / market_cap if market_cap else None),
        "shareholder_yield": ((dividends + buybacks) / market_cap
                              if market_cap else None),
        "net_debt_to_mktcap": (net_debt / market_cap if market_cap else None),
    }
    result.values = values
    return result


def _percentile(current: float, observations: list[float]) -> float:
    """Where `current` sits within past observations, 0-100."""
    if not observations:
        return 50.0
    below = sum(1 for value in observations if value < current)
    equal = sum(1 for value in observations if value == current)
    return 100.0 * (below + 0.5 * equal) / len(observations)


def _split_consistent_shares(
    company: NormalizedCompany, splits: list, shares_now: float | None,
) -> tuple[dict[int, float], list[str]]:
    """Put every year's diluted share count on today's post-split basis.

    Price histories are split-adjusted; EDGAR share counts are whatever basis
    the filing that last reported that year used. A 10-K restates the years
    it shows for splits up to its filing date, but the older years come from
    older filings and never are. Multiply an adjusted price by an unadjusted
    share count and NVIDIA's 2019 market cap comes out 40x too small.

    We don't know which filing each year came from, so for each year we try
    every multiplier its later splits allow (the product of the last k splits
    after its year end, for each k) and keep the one that best continues the
    adjusted series from the next year -- real share counts drift a few
    percent a year; splits move them by 1.5x or more, so the right choice is
    unambiguous.
    """
    adjusted: dict[int, float] = {}
    notes: list[str] = []
    reference = shares_now
    for year in sorted(company.fiscal_years, reverse=True):
        shares = _at(company.statements, "shares_diluted", year)
        end = company.period_ends.get(year)
        if not shares or shares <= 0 or end is None:
            continue
        later = [ratio for when, ratio in splits if when > end]
        candidates = [1.0]
        product = 1.0
        for ratio in reversed(later):
            product *= ratio
            candidates.append(product)
        if reference and reference > 0:
            multiplier = min(candidates,
                             key=lambda m: abs(math.log(shares * m / reference)))
        else:
            multiplier = 1.0
        adjusted[year] = shares * multiplier
        if abs(multiplier - 1.0) > 1e-9:
            notes.append(f"FY{year} x{multiplier:g}")
        reference = adjusted[year]
    return adjusted, notes


def evaluate(
    company: NormalizedCompany,
    metric_frame: pd.DataFrame,
    price: float | None,
    history=None,
) -> MultiplesReport:
    """Current multiples, plus the same multiples at each past year end.

    `history` is a PriceHistory (see prices.py) or None. Without it you get
    today's multiples and nothing to judge them against.
    """
    report = MultiplesReport(ticker=company.ticker)

    # -- current -----------------------------------------------------------
    current_period = company.ttm_column or company.fiscal_years[-1]
    shares_now = (_at(company.statements, "shares_outstanding", current_period)
                  or _at(company.statements, "shares_diluted", current_period))
    if shares_now is None:
        for column in reversed(company.columns):
            shares_now = (_at(company.statements, "shares_outstanding", column)
                          or _at(company.statements, "shares_diluted", column))
            if shares_now:
                break
    report.current = compute_set(company, metric_frame, current_period,
                                 price, shares_now)
    if not price:
        report.notes.append(
            "No price, so no multiples (Yahoo didn't answer). Add a row to prices.csv."
        )
        return report

    # -- history -----------------------------------------------------------
    if history is None:
        report.notes.append(
            "No price history loaded, so today's multiples have nothing to be "
            "compared against. Download this ticker's price history and save it "
            "as price_history.csv (or into a price_history/ folder) to see "
            "whether the current multiple is high or low FOR THIS COMPANY."
        )
        return report

    splits = (getattr(history, "splits", {}) or {}).get(company.ticker, [])
    adjusted_shares, split_notes = _split_consistent_shares(
        company, splits, shares_now)
    if split_notes:
        report.notes.append(
            "Historical share counts restated for stock splits so they match "
            "the split-adjusted price history (" + ", ".join(split_notes) + "). "
            "Without this, every pre-split market cap is off by the split ratio."
        )

    for year in company.fiscal_years:
        period_end = company.period_ends.get(year)
        if period_end is None:
            continue
        historical_price = history.close_near(company.ticker, period_end)
        if historical_price is None:
            continue
        # Weighted-average diluted shares is what filings report per period.
        # Point-in-time count isn't available for past years.
        shares = adjusted_shares.get(year)
        if not shares:
            continue
        report.history.append(
            compute_set(company, metric_frame, year, historical_price, shares))

    if not report.history:
        report.notes.append(
            f"Price history is loaded but has no prices near {company.ticker}'s "
            f"fiscal year ends. Check the file covers the right dates and ticker."
        )
        return report

    if len(report.history) < MIN_HISTORY_YEARS:
        report.notes.append(
            f"Only {len(report.history)} historical year(s) priced. A median off "
            f"that few points isn't worth much -- treat the comparisons below as "
            f"indicative."
        )

    # -- current vs its own history ---------------------------------------
    for key, _label, _unit, _low, _judged in SPEC:
        past = [set_.get(key) for set_ in report.history]
        past = [value for value in past if value is not None]
        current_value = report.current.get(key)
        if not past:
            continue
        series = pd.Series(past, dtype="float64")
        median = float(series.median())
        report.stats[key] = HistoryStat(
            key=key,
            current=current_value,
            median=median,
            low=float(series.min()),
            high=float(series.max()),
            observations=len(past),
            percentile=(_percentile(current_value, past)
                        if current_value is not None else None),
            vs_median=((current_value / median - 1)
                       if current_value is not None and median else None),
        )

    # Surface the headline comparison as a note, so it's impossible to miss.
    headline = report.stats.get("ev_ebitda") or report.stats.get("pe")
    if headline and headline.current is not None and headline.reliable:
        label = SPEC_BY_KEY[headline.key][0]
        report.notes.append(
            f"{label} is {headline.current:,.1f}x against a "
            f"{headline.observations}-year median of {headline.median:,.1f}x "
            f"({headline.vs_median:+.0%}, {headline.percentile:.0f}th percentile "
            f"of its own range) -- {headline.verdict}."
        )
    return report
