"""
Brings the model up to date using quarterly filings.

THE PROBLEM
-----------
A 10-K is an annual document. For most companies the latest one covers the
fiscal year that ended in December, which means that by the middle of the
following year the "latest" figures in an annual-only model are six to nine
months stale. Meanwhile the company has filed two or three 10-Qs that nobody
is reading into the model.

THE FIX: TRAILING TWELVE MONTHS
-------------------------------
The standard construction, and the one used here:

    TTM = last full fiscal year
        + year-to-date this year
        - year-to-date the same period last year

Apple's year ended September 2025 at $416.2bn of revenue. Through three
quarters of fiscal 2026 it had done $364.4bn, against $313.7bn in the same
three quarters a year earlier. So the trailing twelve months to June 2026 is
416.2 + 364.4 - 313.7 = $466.8bn -- twelve months of business ending nine
months after the last annual report.

Why this formula rather than summing four quarters: companies report
year-to-date figures in every 10-Q, but the fourth quarter is never filed as a
10-Q at all (it only appears inside the next 10-K, as the annual figure minus
nine months). Building from YTD figures needs two numbers that always exist
instead of four that don't.

WHAT IS AND ISN'T SAFE HERE
---------------------------
- Income statement and cash flow items are summed over twelve months. Safe.
- Balance sheet items are a snapshot, so TTM takes the LATEST reported balance
  sheet rather than doing any arithmetic. Also safe, and more current than the
  fiscal year end.
- Growth rates against the prior fiscal year are NOT computed for the TTM
  column, because a trailing-twelve-month period overlaps the fiscal year it
  would be compared against. That comparison looks meaningful and isn't.
- Everything here is UNAUDITED. Quarterly figures don't get the same treatment
  as the annual ones, and they're restated more often.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import taxonomy
from .normalize import (
    NormalizedCompany,
    _better_fact,
    _is_annual_form,
    _parse_date,
)
from .taxonomy import DURATION, INSTANT, LineItem

# A prior-year comparative period must be this close to exactly one year before
# the current one. Generous enough for 52/53-week fiscal calendars, which drift
# by up to a week, but not so generous it matches the wrong quarter.
YEAR_MATCH_TOLERANCE_DAYS = 20
# Two periods count as the same length within this many days.
PERIOD_LENGTH_TOLERANCE_DAYS = 12
# A year-to-date period has to begin at the start of the fiscal year.
FY_START_TOLERANCE_DAYS = 12


@dataclass
class TTMResult:
    """Twelve months of results ending after the last fiscal year end."""

    as_of: date                       # end of the most recent reported period
    period_start: date                # twelve months before that
    balance_sheet_date: date | None   # latest balance sheet available
    months_beyond_fiscal_year: int
    values: dict[str, float] = field(default_factory=dict)
    tags_used: dict[str, str] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"TTM {self.as_of.strftime('%b %Y')}"

    @property
    def description(self) -> str:
        return (
            f"Trailing twelve months to {self.as_of.strftime('%b %d, %Y')} "
            f"({self.months_beyond_fiscal_year} months past the last fiscal "
            f"year end). Unaudited, built from 10-Q filings."
        )


def _duration_facts(facts: list[dict]) -> list[dict]:
    """Every fact covering a span of time, best source per (start, end)."""
    best: dict[tuple[str, str], dict] = {}
    for fact in facts:
        if "start" not in fact or "end" not in fact:
            continue
        key = (fact["start"], fact["end"])
        if _better_fact(fact, best.get(key)):
            best[key] = fact
    return list(best.values())


def _instant_facts(facts: list[dict]) -> dict[date, dict]:
    best: dict[date, dict] = {}
    for fact in facts:
        if "start" in fact or "end" not in fact:
            continue
        end = _parse_date(fact["end"])
        if _better_fact(fact, best.get(end)):
            best[end] = fact
    return best


def _facts_for_tag(sources, item: LineItem, tag: str) -> list[dict]:
    """Facts for one tag, pooled across every companyfacts document given.

    Pooling matters after a reorganization. Exxon's annual history sits under
    the old CIK, but the June 2026 quarter was filed by the NEW entity -- so
    reading either one alone gives you a trailing-twelve-month figure that is
    a full quarter staler than what has actually been reported. The dedup in
    _duration_facts and _instant_facts resolves the overlap between them.
    """
    if isinstance(sources, dict):
        sources = [sources]
    pooled: list[dict] = []
    for raw in sources:
        try:
            units = raw["facts"][item.taxonomy][tag]["units"]
        except (KeyError, TypeError):
            continue
        if item.unit in units:
            pooled.extend(units[item.unit])
        elif len(units) == 1:
            pooled.extend(next(iter(units.values())))
    return pooled


def _latest_ytd(facts: list[dict], fiscal_year_end: date) -> dict | None:
    """The most recent year-to-date period that starts a new fiscal year.

    Among periods ending on the latest date we take the LONGEST one, because
    a Q3 filing reports both the discrete quarter and the nine-month
    cumulative figure, and it's the cumulative one we need.
    """
    fy_start = fiscal_year_end + timedelta(days=1)
    candidates = []
    for fact in _duration_facts(facts):
        start, end = _parse_date(fact["start"]), _parse_date(fact["end"])
        if end <= fiscal_year_end:
            continue
        if abs((start - fy_start).days) > FY_START_TOLERANCE_DAYS:
            continue
        candidates.append(((end, (end - start).days), fact))
    if not candidates:
        return None
    return max(candidates)[1]


def _matching_prior_ytd(facts: list[dict], current: dict) -> dict | None:
    """The same year-to-date window one year earlier."""
    current_start = _parse_date(current["start"])
    current_end = _parse_date(current["end"])
    length = (current_end - current_start).days
    target_end = current_end - timedelta(days=365)

    best = None
    best_distance = None
    for fact in _duration_facts(facts):
        start, end = _parse_date(fact["start"]), _parse_date(fact["end"])
        if abs((end - start).days - length) > PERIOD_LENGTH_TOLERANCE_DAYS:
            continue
        distance = abs((end - target_end).days)
        if distance > YEAR_MATCH_TOLERANCE_DAYS:
            continue
        if best_distance is None or distance < best_distance:
            best, best_distance = fact, distance
    return best


def _annual_value(facts: list[dict], fiscal_year_end: date) -> float | None:
    """The full-year figure for the fiscal year ending on this date."""
    for fact in _duration_facts(facts):
        if _parse_date(fact["end"]) != fiscal_year_end:
            continue
        if not _is_annual_form(fact.get("form", "")):
            continue
        days = (_parse_date(fact["end"]) - _parse_date(fact["start"])).days
        if 340 <= days <= 400:
            return float(fact["val"])
    return None


def compute_ttm(sources, company: NormalizedCompany) -> TTMResult | None:
    """Build the trailing-twelve-month column, or None if there's nothing newer.

    `sources` is one companyfacts document, or several to pool (see
    _facts_for_tag). Returns None when the company's most recent filing is
    still the 10-K -- the correct answer right after an annual report, not a
    failure.
    """
    if isinstance(sources, dict):
        sources = [sources]
    if not company.fiscal_years:
        return None
    fiscal_year_end = company.period_ends[company.fiscal_years[-1]]

    # Find how current the data actually is, using the line items almost
    # every filer reports.
    latest_end: date | None = None
    for key in ("revenue", "net_income", "cfo"):
        item = taxonomy.LINE_ITEMS_BY_KEY[key]
        for tag in item.candidates:
            facts = _facts_for_tag(sources, item, tag)
            if not facts:
                continue
            ytd = _latest_ytd(facts, fiscal_year_end)
            if ytd is not None:
                end = _parse_date(ytd["end"])
                if latest_end is None or end > latest_end:
                    latest_end = end
    if latest_end is None:
        return None  # nothing filed since the 10-K

    result = TTMResult(
        as_of=latest_end,
        period_start=latest_end - timedelta(days=364),
        balance_sheet_date=None,
        months_beyond_fiscal_year=round((latest_end - fiscal_year_end).days / 30.44),
    )

    for item in taxonomy.LINE_ITEMS:
        if item.kind == DURATION:
            value, tag = _ttm_for_duration_item(sources, item, fiscal_year_end)
        else:
            value, tag = _latest_for_instant_item(sources, item, fiscal_year_end, result)

        if value is None:
            result.unavailable.append(item.key)
        else:
            result.values[item.key] = value
            result.tags_used[item.key] = tag or ""

    _derive_missing_ttm(result)
    return result


def _ttm_for_duration_item(
    sources, item: LineItem, fiscal_year_end: date
) -> tuple[float | None, str | None]:
    """TTM for one income statement or cash flow line.

    All three terms of the formula must come from the SAME tag. Mixing tags --
    taking the annual figure from one and the quarterly deltas from another --
    can subtract a differently-defined number and produce a total that looks
    plausible and is wrong.
    """
    for tag in item.candidates:
        facts = _facts_for_tag(sources, item, tag)
        if not facts:
            continue
        annual = _annual_value(facts, fiscal_year_end)
        if annual is None:
            continue
        current = _latest_ytd(facts, fiscal_year_end)
        if current is None:
            continue
        prior = _matching_prior_ytd(facts, current)
        if prior is None:
            continue
        return annual + float(current["val"]) - float(prior["val"]), tag
    return None, None


def _latest_for_instant_item(
    sources, item: LineItem, fiscal_year_end: date, result: TTMResult
) -> tuple[float | None, str | None]:
    """Most recent balance sheet value. No arithmetic -- it's a snapshot."""
    for tag in item.candidates:
        facts = _facts_for_tag(sources, item, tag)
        if not facts:
            continue
        instants = _instant_facts(facts)
        newer = [end for end in instants if end > fiscal_year_end]
        if not newer:
            continue
        latest = max(newer)
        if result.balance_sheet_date is None or latest > result.balance_sheet_date:
            result.balance_sheet_date = latest
        return float(instants[latest]["val"]), tag
    return None, None


def _derive_missing_ttm(result: TTMResult) -> None:
    """Same derivations the annual statements get, applied to the TTM column."""
    values = result.values

    def get(key: str) -> float | None:
        return values.get(key)

    def fill(target: str, computed: float | None) -> None:
        if target not in values and computed is not None:
            values[target] = computed
            result.tags_used[target] = "derived"
            if target in result.unavailable:
                result.unavailable.remove(target)

    revenue, cogs = get("revenue"), get("cogs")
    if revenue is not None and cogs is not None:
        fill("gross_profit", revenue - cogs)

    selling, admin = get("selling_marketing"), get("g_and_a")
    if selling is not None and admin is not None:
        fill("sganda", selling + admin)
    elif admin is not None and selling is None:
        fill("sganda", admin)

    gross, opex = get("gross_profit"), get("operating_expenses")
    if gross is not None and opex is not None:
        fill("operating_income", gross - opex)

    pretax, interest = get("pretax_income"), get("interest_expense")
    if "operating_income" not in values and pretax is not None:
        # Same EBIT fallback as the annual statements, with the same guard
        # against applying it to lenders.
        interest_amount = abs(interest) if interest is not None else 0.0
        if revenue and revenue > 0 and (interest_amount / revenue) <= 0.15:
            fill("operating_income", pretax + interest_amount)

    net_income, tax = get("net_income"), get("tax_expense")
    if net_income is not None and tax is not None:
        fill("pretax_income", net_income + tax)

    equity, minority = get("equity"), get("minority_interest")
    if equity is not None:
        fill("total_equity", equity + (minority or 0.0))

    assets, total_equity = get("total_assets"), get("total_equity")
    if assets is not None and total_equity is not None:
        fill("total_liabilities",
             assets - total_equity - (get("redeemable_nci") or 0.0)
             - (get("redeemable_preferred") or 0.0))
