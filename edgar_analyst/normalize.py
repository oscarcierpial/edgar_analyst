"""
Turns the SEC's raw pile of tagged facts into clean annual financial statements.

This is the part that would take you days by hand for a single company.

THE THREE HARD PROBLEMS, AND HOW WE SOLVE THEM
----------------------------------------------
1. WHICH FACTS ARE ANNUAL?
   companyfacts returns every period a company ever tagged -- quarters, halves,
   nine-month stubs, full years, all mixed together. We keep only facts whose
   period is roughly 365 days long and that came from a 10-K.

2. RESTATEMENTS AND DUPLICATES.
   The same fiscal year appears in several filings (this year's 10-K shows two
   prior years as comparatives). Sometimes those values differ because the
   company restated. We keep the MOST RECENTLY FILED version of each period, so
   the history is internally consistent -- but we record when a restatement
   happened, because a restatement is itself a research signal.

3. LINING UP FISCAL YEARS ACROSS COMPANIES.
   Apple's year ends in September, Walmart's in January, most companies' in
   December. We label each year by the calendar year it mostly covers so
   companies are comparable side by side. See CALENDAR_ALIGN_FISCAL_YEARS.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from . import config, taxonomy
from .taxonomy import DURATION, INSTANT, LineItem


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def fiscal_year_label(period_end: date) -> int:
    """Which calendar year does this fiscal year mostly belong to?

    A year ending 2025-01-31 covers mostly calendar 2024, so we call it 2024.
    A year ending 2025-09-27 covers mostly calendar 2025, so we call it 2025.
    """
    if config.CALENDAR_ALIGN_FISCAL_YEARS and period_end.month <= 5:
        return period_end.year - 1
    return period_end.year


def _is_annual_form(form: str) -> bool:
    """True for annual reports, including amendments and the older 10-K405."""
    return form.upper().startswith("10-K")


def _form_tier(form: str) -> int:
    """How much we trust a filing as a source for annual figures. Lower is better.

    The 10-K is always preferred. But a fiscal year's figures can be absent
    from every 10-K and still be sitting in EDGAR: Disney's FY2017 balance
    sheet, for instance, appears only in an 8-K filed in 2019 that recast the
    prior years for the Fox acquisition and the segment reorganization that
    followed. Restricting ourselves to 10-Ks alone silently drops that year --
    and one missing year in the middle of a decade quietly breaks every average
    and growth rate that spans it.

    So we take the 10-K when there is one, a 10-Q comparative column when there
    isn't, and a recast 8-K as a last resort -- and record on the coverage sheet
    which years came from somewhere other than an annual report.
    """
    upper = form.upper()
    if upper.startswith("10-K"):
        return 0
    if upper.startswith("10-Q"):
        return 1
    return 2


def _better_fact(candidate: dict, incumbent: dict | None) -> bool:
    """Should `candidate` replace `incumbent` as the source for a period?

    Preference order: trusted form first, then most recently filed (so a
    restatement in a later 10-K supersedes the original).
    """
    if incumbent is None:
        return True
    candidate_key = (_form_tier(candidate.get("form", "")),)
    incumbent_key = (_form_tier(incumbent.get("form", "")),)
    if candidate_key != incumbent_key:
        return candidate_key < incumbent_key
    return candidate.get("filed", "") > incumbent.get("filed", "")


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass
class CoverageRow:
    """Records how we sourced one line item -- your audit trail."""

    key: str
    label: str
    statement: str
    tag_used: str | None
    years_found: int
    years_missing: list[int] = field(default_factory=list)
    derived: bool = False
    restated_years: list[int] = field(default_factory=list)
    optional: bool = False
    # Tags used to fill years the primary tag didn't cover. Worth eyeballing:
    # two tags for one line item can mean two slightly different definitions.
    supplemental_tags: list[str] = field(default_factory=list)
    # Years whose value came from a 10-Q comparative or a recast 8-K rather
    # than an annual report. Not wrong, but worth knowing.
    non_10k_years: list[int] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.tag_used is None and not self.derived:
            return "OPTIONAL - not reported" if self.optional else "MISSING"
        if self.derived:
            return "DERIVED"
        if self.years_missing:
            # An optional item with gaps is usually just a disclosure that
            # didn't exist yet (operating leases before ASC 842 in 2019) or
            # an activity the company didn't have that year (no acquisitions).
            # Don't flag those as data problems -- it trains you to ignore the
            # sheet that's supposed to catch real ones.
            return "OPTIONAL - partial" if self.optional else "PARTIAL"
        if self.supplemental_tags:
            return "OK (multi-tag)"
        return "OK"


@dataclass
class NormalizedCompany:
    """Everything we know about one company, in standard form."""

    ticker: str
    cik: int
    entity_name: str
    fiscal_years: list[int]
    period_ends: dict[int, date]
    statements: pd.DataFrame  # rows = line item keys, columns = fiscal years
    coverage: list[CoverageRow]
    warnings: list[str] = field(default_factory=list)
    # Set by interim.compute_ttm via attach_ttm() when quarterly filings exist
    # more recent than the last 10-K. Typed loosely to avoid a circular import.
    ttm: object | None = None
    ttm_column: str | None = None
    industry: str = ""  # SEC SIC description, from the filer record

    @property
    def columns(self) -> list:
        """Every column in the model: the fiscal years, plus TTM if present."""
        return list(self.statements.columns)

    def attach_ttm(self, ttm) -> None:
        """Add a trailing-twelve-month column to the right of the statements.

        Once attached it behaves like any other column, so the metrics engine
        computes ratios for it without needing to know it's there.
        """
        self.ttm = ttm
        if ttm is None:
            return
        self.ttm_column = ttm.label
        self.statements[ttm.label] = pd.Series(
            {key: ttm.values.get(key) for key in self.statements.index},
            dtype="float64",
        )
        if ttm.unavailable:
            missing = [
                taxonomy.LINE_ITEMS_BY_KEY[k].label
                for k in ttm.unavailable
                if k in taxonomy.LINE_ITEMS_BY_KEY
                and not taxonomy.LINE_ITEMS_BY_KEY[k].optional
            ]
            if missing:
                self.warnings.append(
                    f"The {ttm.label} column is missing "
                    f"{', '.join(missing[:4])}"
                    f"{' and others' if len(missing) > 4 else ''} — the company "
                    f"doesn't tag those consistently in its 10-Qs. Fiscal year "
                    f"columns are unaffected."
                )

    def value(self, key: str, year: int) -> float | None:
        """Look up one number, returning None instead of NaN when absent."""
        try:
            v = self.statements.at[key, year]
        except KeyError:
            return None
        return None if pd.isna(v) else float(v)

    def series(self, key: str) -> pd.Series:
        """One line item across all years. Missing rows come back as all-NaN."""
        if key in self.statements.index:
            return self.statements.loc[key]
        return pd.Series(index=self.fiscal_years, dtype="float64")


# ---------------------------------------------------------------------------
# the normalizer
# ---------------------------------------------------------------------------

class Normalizer:
    """Builds NormalizedCompany objects out of raw companyfacts JSON."""

    def __init__(self, years: int = config.DEFAULT_YEARS):
        self.years = years

    # -- fact extraction ----------------------------------------------------

    def _facts_for_tag(self, raw: dict, item: LineItem, tag: str) -> list[dict]:
        """Pull the list of raw facts for one tag, or [] if the company never used it."""
        try:
            units = raw["facts"][item.taxonomy][tag]["units"]
        except KeyError:
            return []
        # Most items are in USD. EPS is in USD/shares, share counts in "shares".
        # If our expected unit isn't there, take whatever single unit exists so
        # we don't silently drop a foreign-currency filer.
        if item.unit in units:
            return units[item.unit]
        if len(units) == 1:
            return next(iter(units.values()))
        return []

    def _annual_duration_facts(
        self, facts: list[dict], only_10k: bool = False
    ) -> dict[date, dict]:
        """Keep one full-year fact per period end date, best source wins.

        `only_10k` is used when establishing the fiscal calendar. The 10-K is
        the authoritative statement of when a company's year ends, and an 8-K
        can contain a 365-day period for something else entirely (pro-forma
        figures, or an acquired business's own financial year). We let those
        filings supply VALUES at known year ends, but never let them invent a
        year end.
        """
        best: dict[date, dict] = {}
        for fact in facts:
            if "start" not in fact or "end" not in fact:
                continue
            if only_10k and not _is_annual_form(fact.get("form", "")):
                continue
            start, end = _parse_date(fact["start"]), _parse_date(fact["end"])
            days = (end - start).days
            if not (config.ANNUAL_MIN_DAYS <= days <= config.ANNUAL_MAX_DAYS):
                continue
            if _better_fact(fact, best.get(end)):
                best[end] = fact
        return best

    def _instant_facts(self, facts: list[dict]) -> dict[date, dict]:
        """Keep one balance-sheet snapshot per date, best source wins."""
        best: dict[date, dict] = {}
        for fact in facts:
            if "start" in fact or "end" not in fact:
                continue
            end = _parse_date(fact["end"])
            if _better_fact(fact, best.get(end)):
                best[end] = fact
        return best

    def _restated_years(self, facts: list[dict], is_duration: bool) -> set[date]:
        """Period ends where different filings reported different values.

        Not used to change any number -- just flagged in the coverage sheet,
        because a quiet restatement is worth knowing about.
        """
        by_end: dict[date, set[float]] = defaultdict(set)
        for fact in facts:
            if is_duration:
                if "start" not in fact or "end" not in fact:
                    continue
                start, end = _parse_date(fact["start"]), _parse_date(fact["end"])
                if not (config.ANNUAL_MIN_DAYS <= (end - start).days <= config.ANNUAL_MAX_DAYS):
                    continue
            else:
                if "start" in fact or "end" not in fact:
                    continue
                end = _parse_date(fact["end"])
            if not _is_annual_form(fact.get("form", "")):
                continue
            by_end[end].add(round(float(fact["val"]), 2))
        return {end for end, values in by_end.items() if len(values) > 1}

    # -- fiscal calendar ----------------------------------------------------

    def _build_fiscal_calendar(self, raw: dict) -> tuple[list[int], dict[int, date]]:
        """Work out this company's fiscal year ends from its own filings.

        We look at the tags almost every filer reports (revenue, net income,
        operating cash flow) and collect the period end dates they use.
        """
        anchor_keys = ("revenue", "net_income", "cfo", "operating_income")
        end_date_votes: Counter[date] = Counter()

        for key in anchor_keys:
            item = taxonomy.LINE_ITEMS_BY_KEY[key]
            for tag in item.candidates:
                facts = self._facts_for_tag(raw, item, tag)
                if not facts:
                    continue
                for end in self._annual_duration_facts(facts, only_10k=True):
                    end_date_votes[end] += 1
                break  # first tag the company uses is enough for calendar purposes

        if not end_date_votes:
            return [], {}

        # If two end dates land in the same fiscal year label (which happens
        # when a company changes its year end), keep the better-supported one.
        by_label: dict[int, tuple[int, date]] = {}
        for end, votes in end_date_votes.items():
            label = fiscal_year_label(end)
            current = by_label.get(label)
            if current is None or (votes, end) > current:
                by_label[label] = (votes, end)

        labels = sorted(by_label)[-self.years:]
        period_ends = {label: by_label[label][1] for label in labels}
        return labels, period_ends

    # -- line item extraction ----------------------------------------------

    def _extract_item(
        self,
        raw: dict,
        item: LineItem,
        years: list[int],
        period_ends: dict[int, date],
    ) -> tuple[dict[int, float], CoverageRow]:
        """Find the best tag(s) for one line item and pull its yearly values.

        Two passes, and the second one matters:

        PASS 1 picks the single tag covering the most years. That's the
        "primary" tag and it wins every year it covers.

        PASS 2 fills the remaining gaps from the other candidate tags. This is
        essential for long histories, because companies switch tags over time.
        Apple, for example, tagged revenue as `SalesRevenueNet` through FY2017
        and `RevenueFromContractWithCustomerExcludingAssessedTax` afterward --
        take only the modern tag and you silently lose the early years, which
        then quietly breaks every growth rate and CAGR downstream.

        Any tag used to fill a gap is recorded so the Data Coverage sheet shows
        you exactly where a series stitches two definitions together.
        """
        # Pull each candidate tag's usable facts once.
        per_tag_values: dict[str, dict[int, float]] = {}
        per_tag_restated: dict[str, list[int]] = {}
        per_tag_forms: dict[str, dict[int, str]] = {}

        for tag in item.candidates:
            facts = self._facts_for_tag(raw, item, tag)
            if not facts:
                continue

            selected = (
                self._annual_duration_facts(facts)
                if item.kind == DURATION
                else self._instant_facts(facts)
            )

            values: dict[int, float] = {}
            forms: dict[int, str] = {}
            for year in years:
                target = period_ends[year]
                fact = selected.get(target)
                if fact is None and item.kind == INSTANT:
                    # Balance sheet dates can sit a day or two off the income
                    # statement period end, so allow a little slack.
                    tolerance = config.BALANCE_SHEET_DATE_TOLERANCE_DAYS
                    near = [
                        (abs((end - target).days), end)
                        for end in selected
                        if abs((end - target).days) <= tolerance
                    ]
                    if near:
                        fact = selected[min(near)[1]]
                if fact is not None:
                    values[year] = float(fact["val"])
                    forms[year] = fact.get("form", "")

            if values:
                per_tag_values[tag] = values
                per_tag_forms[tag] = forms
                restated_ends = self._restated_years(facts, item.kind == DURATION)
                per_tag_restated[tag] = sorted(
                    year for year in years if period_ends[year] in restated_ends
                )

        if not per_tag_values:
            return {}, CoverageRow(
                key=item.key, label=item.label, statement=item.statement,
                tag_used=None, years_found=0, years_missing=list(years),
                optional=item.optional,
            )

        # PASS 1 -- best single tag. Candidate order breaks ties, since we only
        # replace on a strictly larger year count.
        primary_tag = max(per_tag_values, key=lambda t: len(per_tag_values[t]))
        merged = dict(per_tag_values[primary_tag])
        merged_forms = dict(per_tag_forms[primary_tag])

        # PASS 2 -- fill gaps in candidate-preference order.
        supplemental: list[str] = []
        for tag in item.candidates:
            if tag == primary_tag or tag not in per_tag_values:
                continue
            filled_any = False
            for year, value in per_tag_values[tag].items():
                if year not in merged:
                    merged[year] = value
                    merged_forms[year] = per_tag_forms[tag].get(year, "")
                    filled_any = True
            if filled_any:
                supplemental.append(tag)

        coverage = CoverageRow(
            key=item.key,
            label=item.label,
            statement=item.statement,
            tag_used=primary_tag,
            years_found=len(merged),
            years_missing=[y for y in years if y not in merged],
            restated_years=per_tag_restated.get(primary_tag, []),
            optional=item.optional,
            supplemental_tags=supplemental,
            non_10k_years=sorted(
                year for year, form in merged_forms.items()
                if form and not _is_annual_form(form)
            ),
        )
        return merged, coverage

    # -- derivations --------------------------------------------------------

    def _derive_missing(
        self, table: pd.DataFrame, coverage_by_key: dict[str, CoverageRow]
    ) -> None:
        """Fill in items that are computable from others.

        Plenty of companies never tag GrossProfit -- they just report revenue
        and cost of revenue and let the reader subtract. We do the subtraction.
        Modifies `table` in place.
        """

        def fill(target: str, computed: pd.Series, note: str) -> None:
            if target not in table.index:
                return
            row = table.loc[target]
            # Only fill years that are actually empty; never overwrite a
            # number the company reported itself.
            missing = row.isna() & computed.notna()
            if not missing.any():
                return
            table.loc[target, missing] = computed[missing]
            cov = coverage_by_key.get(target)
            if cov is not None:
                cov.derived = cov.tag_used is None
                cov.years_found = int(table.loc[target].notna().sum())
                cov.years_missing = [
                    int(y) for y in table.columns[table.loc[target].isna()]
                ]
                cov.label += f" ({note})" if cov.derived else ""

        def row(key: str) -> pd.Series:
            if key in table.index:
                return table.loc[key]
            return pd.Series(index=table.columns, dtype="float64")

        # Gross profit = revenue - cost of revenue
        fill("gross_profit", row("revenue") - row("cogs"), "derived")

        # SG&A = sales & marketing + G&A when the company splits them (Uber,
        # most software companies). If S&M is never reported, G&A alone is
        # the best available figure, but it's labeled as such.
        selling, admin = row("selling_marketing"), row("g_and_a")
        if selling.notna().any():
            fill("sganda", selling + admin, "derived: S&M + G&A")
        else:
            fill("sganda", admin, "G&A only")

        # Operating income = gross profit - operating expenses
        fill("operating_income", row("gross_profit") - row("operating_expenses"),
             "derived")

        # Still no operating income? Fall back to EBIT = pre-tax income +
        # interest expense. Plenty of large companies -- Exxon, Nike, J&J, IBM
        # -- simply have no "operating income" line on their income statement,
        # and without this fallback their ROIC, EBITDA and interest coverage
        # would all come back blank.
        #
        # We skip this when interest expense is large relative to revenue,
        # which is how we avoid producing a nonsense EBIT for banks and
        # insurers: for a lender, interest IS an operating cost, so adding it
        # back is meaningless rather than merely imprecise.
        revenue = row("revenue")
        interest = row("interest_expense").abs()
        interest_is_operating = (interest / revenue.where(revenue > 0)) > 0.15
        # Companies that report interest NET of interest income (Nike, for one)
        # often don't tag it at all. Treating a missing interest line as zero
        # makes EBIT equal pre-tax income, which is very close to right for a
        # company with little debt and clearly better than leaving the row --
        # and ROIC, EBITDA and margins with it -- blank.
        ebit = (row("pretax_income") + interest.fillna(0)).where(~interest_is_operating)
        fill("operating_income", ebit, "derived: pretax + interest")

        # Pre-tax income = net income + tax expense
        fill("pretax_income", row("net_income") + row("tax_expense"), "derived")

        # A company reporting inventory on a FIFO basis with a separate LIFO
        # reserve carries the net figure on its balance sheet. Subtract the
        # reserve so inventory matches what's actually reported -- otherwise
        # inventory days and the cash conversion cycle are overstated.
        inventory_cov = coverage_by_key.get("inventory")
        if inventory_cov is not None and inventory_cov.tag_used == "FIFOInventoryAmount":
            reserve = row("lifo_reserve")
            adjusted = row("inventory") - reserve.fillna(0)
            table.loc["inventory"] = adjusted
            inventory_cov.label += " (FIFO less LIFO reserve)"

        # Total equity including non-controlling interests.
        # Careful about double counting: the `equity` row may already BE the
        # including-NCI figure if that's the only tag the company used, in
        # which case adding NCI on top would overstate it.
        equity_cov = coverage_by_key.get("equity")
        equity_is_parent_only = (
            equity_cov is not None and equity_cov.tag_used == "StockholdersEquity"
        )
        if equity_is_parent_only:
            fill("total_equity", row("equity") + row("minority_interest").fillna(0),
                 "derived")
        else:
            fill("total_equity", row("equity"), "derived")

        # Temporary ("mezzanine") equity: redeemable NCI plus redeemable
        # preferred. Sits between liabilities and equity, so every balance
        # sheet identity below has to include it.
        mezzanine = (row("redeemable_nci").fillna(0)
                     + row("redeemable_preferred").fillna(0))

        # Total liabilities = total assets - all equity claims
        fill(
            "total_liabilities",
            row("total_assets") - row("total_equity") - mezzanine,
            "derived",
        )

        # Reconcile total equity against the balance sheet identity.
        #
        # A company can report BOTH `StockholdersEquity` and
        # `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`
        # with values that don't reconcile, because the two figures were tagged
        # in filings made at different times and one of them predates a
        # restatement. GE in 2021 is the case in point: its tagged
        # including-NCI equity was $33.3bn while parent equity plus NCI came to
        # $41.6bn, and only the latter makes assets equal liabilities plus
        # equity. When the two candidates disagree, keep whichever one actually
        # balances -- that's the arbiter, not tag preference.
        if {"total_equity", "total_assets", "total_liabilities"} <= set(table.index):
            alternative = row("equity") + row("minority_interest").fillna(0)
            residual = (
                row("total_assets")
                - row("total_liabilities")
                - mezzanine
            )
            current_error = (row("total_equity") - residual).abs()
            alternative_error = (alternative - residual).abs()
            prefer_alternative = (
                alternative.notna()
                & current_error.notna()
                & alternative_error.notna()
                & (alternative_error < current_error)
            )
            if prefer_alternative.any():
                table.loc["total_equity", prefer_alternative] = \
                    alternative[prefer_alternative]

    # -- sanity checks ------------------------------------------------------

    @staticmethod
    def _first_10k_year(raw: dict) -> int | None:
        """Fiscal year covered by the company's first 10-K on EDGAR.

        Any year before it comes only from comparatives in later filings --
        i.e. the company wasn't public yet. That's what separates an IPO
        share-count jump from a stock split.
        """
        earliest: tuple[str, str] | None = None  # (filed, accn)
        ends_by_accn: dict[str, date] = {}
        for facts in (raw.get("facts") or {}).values():
            for concept in facts.values():
                for unit_facts in (concept.get("units") or {}).values():
                    for fact in unit_facts:
                        if fact.get("form") != "10-K":
                            continue
                        accn, filed = fact.get("accn", ""), fact.get("filed", "")
                        if filed and (earliest is None or (filed, accn) < earliest):
                            earliest = (filed, accn)
                        try:
                            end = _parse_date(fact["end"])
                        except (KeyError, ValueError):
                            continue
                        if accn not in ends_by_accn or end > ends_by_accn[accn]:
                            ends_by_accn[accn] = end
        if earliest is None or earliest[1] not in ends_by_accn:
            return None
        return fiscal_year_label(ends_by_accn[earliest[1]])

    def _detect_warnings(self, table: pd.DataFrame, years: list[int],
                         coverage: list[CoverageRow],
                         first_10k_year: int | None = None) -> list[str]:
        """Catch the artifacts that silently poison a model.

        The big one is stock splits. When a company splits its stock it
        restates share counts and EPS in the filings it makes AFTERWARD -- but
        the older filings on EDGAR still carry the pre-split numbers, and those
        old filings are the only source for the early years of your history. So
        a 10-year share count series can contain a 4x step change that is not
        real. Left undetected it wrecks EPS growth, per-share metrics, and any
        buyback analysis. We can't fix it automatically without split data the
        SEC doesn't give us here, so we flag it loudly.
        """
        warnings: list[str] = []

        # Detect splits from SHARE COUNTS only, never from EPS. A company's EPS
        # can legitimately double or 7x in a year (that's just a good year), so
        # testing EPS ratios against split factors produces false alarms.
        # Share counts, by contrast, only move a few percent a year from
        # buybacks and issuance -- a clean 2x/4x/10x step in a share count is
        # a split and essentially nothing else.
        common_split_ratios = (2, 3, 4, 5, 7, 8, 10, 20, 40)
        tolerance = 0.10  # ratio must be within 10% of an exact split factor

        # Collect boundaries first so we emit one warning per year rather than
        # a near-identical one for basic and diluted counts.
        boundaries: dict[int, tuple[int, float]] = {}
        for key in ("shares_diluted", "shares_basic"):
            if key not in table.index:
                continue
            series = table.loc[key].dropna()
            if len(series) < 2:
                continue
            for earlier_year, later_year in zip(series.index[:-1], series.index[1:]):
                earlier, later = series[earlier_year], series[later_year]
                if earlier <= 0 or later <= 0:
                    continue
                ratio = later / earlier
                for split in common_split_ratios:
                    forward = abs(ratio - split) <= tolerance * split
                    reverse = abs(ratio - 1 / split) <= tolerance / split
                    if forward or reverse:
                        boundaries.setdefault(int(later_year), (int(earlier_year), ratio))
                        break

        # A share count can also jump ~2-3x at an IPO, when preferred stock
        # converts to common -- Uber went from 479mm to 1,248mm diluted shares
        # in FY2019. Same consequence (per-share history not comparable),
        # different cause, and the cause is what an analyst would repeat.
        # Catch two cases: the step lands at a split ratio but the earlier
        # year predates the first 10-K, or the step is any big increase
        # (>1.5x) across that boundary.
        if first_10k_year is not None:
            for key in ("shares_diluted", "shares_basic"):
                if key not in table.index:
                    continue
                series = table.loc[key].dropna()
                for earlier_year, later_year in zip(series.index[:-1], series.index[1:]):
                    if int(earlier_year) >= first_10k_year or series[earlier_year] <= 0:
                        continue
                    ratio = series[later_year] / series[earlier_year]
                    if ratio > 1.5:
                        boundaries[int(later_year)] = (int(earlier_year), ratio)

        for later_year, (earlier_year, ratio) in sorted(boundaries.items()):
            kind = "forward" if ratio > 1 else "reverse"
            if first_10k_year is not None and earlier_year < first_10k_year \
                    and ratio > 1:
                warnings.append(
                    f"Share count steps ~{ratio:.1f}x between FY{earlier_year} "
                    f"and FY{later_year}, and FY{earlier_year} predates the "
                    f"company's first 10-K (FY{first_10k_year}). That's the IPO "
                    f"-- preferred stock converting to common and new shares "
                    f"sold -- not a stock split. Per-share figures (EPS, share "
                    f"count) are NOT comparable across it; pre-IPO EPS describes "
                    f"a much smaller common share base."
                )
                continue
            warnings.append(
                f"Share count steps ~{ratio:.1f}x between FY{earlier_year} and "
                f"FY{later_year} -- an unadjusted {kind} stock split, not a real "
                f"change. EDGAR never restates already-filed reports for a later "
                f"split, so the early years of this history come from filings on "
                f"the old basis. Per-share figures (EPS, share count) are NOT "
                f"comparable across FY{earlier_year}/FY{later_year}. Everything "
                f"else on the statements is unaffected."
            )

        if "equity" in table.index:
            negative = [int(y) for y in years
                        if pd.notna(table.at["equity", y]) and table.at["equity", y] < 0]
            if negative:
                mezz_years = [
                    y for y in negative
                    if "redeemable_preferred" in table.index
                    and pd.notna(table.at["redeemable_preferred", y])
                    and table.at["redeemable_preferred", y] > abs(table.at["equity", y])
                ]
                cause = (
                    "Here it's a pre-IPO capital structure: the investors' "
                    "capital sits in redeemable preferred (mezzanine), not in "
                    "common equity. ROE is blanked for those years."
                    if mezz_years else
                    "Usually buybacks or accumulated losses. ROE (and ROIC, if "
                    "invested capital is also negative) is blanked in those years."
                )
                warnings.append(
                    f"Shareholders' equity is negative in {', '.join(map(str, negative))}. "
                    + cause
                )

        # The balance sheet identity is the best single check that we mapped
        # the balance sheet correctly. When it fails, some line is being
        # sourced from a filing on a different basis than the others -- most
        # often a restatement -- and anything built on those rows (ROIC,
        # leverage, working capital) is suspect for that year.
        needed = {"total_assets", "total_liabilities", "total_equity"}
        if needed <= set(table.index):
            off_years = []
            for year in years:
                assets = table.at["total_assets", year]
                liabilities = table.at["total_liabilities", year]
                equity_total = table.at["total_equity", year]
                redeemable = 0.0
                for key in ("redeemable_nci", "redeemable_preferred"):
                    if key in table.index and pd.notna(table.at[key, year]):
                        redeemable += table.at[key, year]
                if pd.isna(assets) or pd.isna(liabilities) or pd.isna(equity_total):
                    continue
                if assets == 0:
                    continue
                gap = abs(assets - (liabilities + equity_total + redeemable)) / abs(assets)
                if gap > 0.01:
                    off_years.append((int(year), gap))
            if off_years:
                detail = ", ".join(f"FY{y} ({g:.1%})" for y, g in off_years)
                warnings.append(
                    f"Balance sheet doesn't tie in {detail}. Assets should equal "
                    f"liabilities plus total equity; a gap usually means one line "
                    f"came from a filing made before a restatement. Check those "
                    f"years against the 10-K before using leverage or return "
                    f"metrics for them."
                )

        # Gross margin comparability. Some companies tag a cost of sales that
        # excludes depreciation and amortization, which makes their gross
        # margin structurally higher than a peer that includes it. Kroger's
        # gross margin is ~23% on this basis; it is not the same 23% as a
        # competitor reporting cost of sales inclusive of D&A.
        cogs_cov = next((c for c in coverage if c.key == "cogs"), None)
        if cogs_cov is not None and cogs_cov.tag_used \
                and "ExcludingDepreciation" in cogs_cov.tag_used:
            warnings.append(
                "Cost of sales excludes depreciation and amortization "
                f"(tag: {cogs_cov.tag_used}). Gross profit and gross margin are "
                "therefore higher than they would be on a D&A-inclusive basis, "
                "and are NOT directly comparable to peers who include it. "
                "Operating margin and everything below it are unaffected."
            )

        if "revenue" in table.index:
            gaps = [int(y) for y in years if pd.isna(table.at["revenue", y])]
            if gaps:
                warnings.append(
                    f"No revenue found for {', '.join(map(str, gaps))}. Growth rates "
                    f"and CAGRs touching those years will be blank."
                )

        return warnings

    # -- entry point --------------------------------------------------------

    def normalize(self, raw: dict, ticker: str) -> NormalizedCompany:
        years, period_ends = self._build_fiscal_calendar(raw)
        if not years:
            raise ValueError(
                f"Couldn't find any annual (10-K) data for {ticker}. Foreign "
                f"private issuers file 20-F and won't work here, and very "
                f"recent IPOs may not have filed a 10-K yet."
            )

        values_by_key: dict[str, dict[int, float]] = {}
        coverage: list[CoverageRow] = []
        for item in taxonomy.LINE_ITEMS:
            values, cov = self._extract_item(raw, item, years, period_ends)
            values_by_key[item.key] = values
            coverage.append(cov)

        table = pd.DataFrame(
            {year: {key: vals.get(year) for key, vals in values_by_key.items()}
             for year in years},
            index=[item.key for item in taxonomy.LINE_ITEMS],
            dtype="float64",
        )

        coverage_by_key = {row.key: row for row in coverage}
        self._derive_missing(table, coverage_by_key)

        return NormalizedCompany(
            ticker=ticker.upper(),
            cik=int(raw.get("cik", 0)),
            entity_name=raw.get("entityName", ticker.upper()),
            fiscal_years=years,
            period_ends=period_ends,
            statements=table,
            coverage=coverage,
            warnings=self._detect_warnings(table, years, coverage,
                                           self._first_10k_year(raw)),
        )
