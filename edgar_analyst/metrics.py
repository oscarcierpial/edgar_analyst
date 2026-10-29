"""
Everything you'd normally build by hand in the "ratios" tab of a model.

Each metric is defined once, in one place, so every company you look at is
computed the same way. That consistency is the actual product here -- it's what
lets you compare 40 names without wondering whether you treated leases
differently in the third one.

WHERE JUDGMENT CREEPS IN
------------------------
Several of these require a choice, and reasonable analysts choose differently.
Wherever that's true there's a comment saying what this file assumes so you can
change it deliberately rather than by accident:
  * Invested capital is debt + equity - cash and short-term investments.
  * Balance sheet items are AVERAGED (beginning + ending) / 2 for return and
    turnover metrics, because the numerator covers a period and the
    denominator is a snapshot.
  * Operating leases are NOT in net debt by default. For retailers, airlines
    and restaurants you should probably turn that on -- see INCLUDE_LEASES.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .normalize import NormalizedCompany


# Set True to treat non-current operating lease liabilities as debt.
INCLUDE_LEASES_IN_NET_DEBT = False

# Used for NOPAT when a company's own effective tax rate is unusable
# (negative pre-tax income, or a one-off that makes the rate meaningless).
FALLBACK_TAX_RATE = 0.21

# Formatting hints the Excel exporter reads.
PCT, TIMES, DAYS, MONEY, RATIO = "pct", "times", "days", "money", "ratio"


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    group: str
    fmt: str
    note: str = ""


METRIC_SPECS: tuple[MetricSpec, ...] = (
    # growth
    MetricSpec("revenue_growth", "Revenue Growth", "Growth", PCT),
    MetricSpec("gross_profit_growth", "Gross Profit Growth", "Growth", PCT),
    MetricSpec("operating_income_growth", "Operating Income Growth", "Growth", PCT),
    MetricSpec("net_income_growth", "Net Income Growth", "Growth", PCT),
    MetricSpec("fcf_growth", "Free Cash Flow Growth", "Growth", PCT),
    MetricSpec("eps_growth", "Diluted EPS Growth", "Growth", PCT),
    MetricSpec("share_count_change", "Diluted Share Count Change", "Growth", PCT,
               "Negative = buying back stock. Compare to buyback spend."),

    # margins
    MetricSpec("gross_margin", "Gross Margin", "Margins", PCT),
    MetricSpec("operating_margin", "Operating Margin", "Margins", PCT),
    MetricSpec("ebitda_margin", "EBITDA Margin", "Margins", PCT),
    MetricSpec("net_margin", "Net Margin", "Margins", PCT),
    MetricSpec("fcf_margin", "Free Cash Flow Margin", "Margins", PCT),
    MetricSpec("rnd_pct_revenue", "R&D % of Revenue", "Margins", PCT),
    MetricSpec("sganda_pct_revenue", "SG&A % of Revenue", "Margins", PCT),

    # returns
    MetricSpec("roic", "ROIC (NOPAT / avg invested capital)", "Returns", PCT,
               "The number that decides whether growth creates or destroys value."),
    MetricSpec("roe", "ROE", "Returns", PCT),
    MetricSpec("roa", "ROA", "Returns", PCT),
    MetricSpec("asset_turnover", "Asset Turnover", "Returns", TIMES),
    MetricSpec("invested_capital", "Invested Capital", "Returns", MONEY),
    MetricSpec("nopat", "NOPAT", "Returns", MONEY),
    MetricSpec("effective_tax_rate", "Effective Tax Rate", "Returns", PCT),

    # cash
    MetricSpec("ebitda", "EBITDA", "Cash Flow", MONEY),
    MetricSpec("free_cash_flow", "Free Cash Flow (CFO - capex)", "Cash Flow", MONEY),
    MetricSpec("fcf_conversion", "FCF / Net Income", "Cash Flow", PCT,
               "Below ~80% for years running is where accounting questions start."),
    MetricSpec("cfo_to_net_income", "CFO / Net Income", "Cash Flow", PCT),
    MetricSpec("capex_pct_revenue", "Capex % of Revenue", "Cash Flow", PCT),
    MetricSpec("capex_to_d_and_a", "Capex / D&A", "Cash Flow", TIMES,
               "Persistently below 1.0x means the asset base is shrinking."),
    MetricSpec("sbc_pct_revenue", "Stock Comp % of Revenue", "Cash Flow", PCT),
    MetricSpec("sbc_pct_fcf", "Stock Comp % of Free Cash Flow", "Cash Flow", PCT),
    MetricSpec("shareholder_return", "Buybacks + Dividends", "Cash Flow", MONEY),
    MetricSpec("payout_of_fcf", "Buybacks + Dividends % of FCF", "Cash Flow", PCT),

    # working capital
    MetricSpec("dso", "Days Sales Outstanding", "Working Capital", DAYS),
    MetricSpec("dio", "Days Inventory Outstanding", "Working Capital", DAYS),
    MetricSpec("dpo", "Days Payable Outstanding", "Working Capital", DAYS),
    MetricSpec("cash_conversion_cycle", "Cash Conversion Cycle", "Working Capital", DAYS,
               "DSO + DIO - DPO. Rising = cash is getting trapped in the business."),

    # balance sheet
    MetricSpec("total_debt", "Total Debt", "Balance Sheet", MONEY),
    MetricSpec("net_debt", "Net Debt", "Balance Sheet", MONEY),
    MetricSpec("net_debt_to_ebitda", "Net Debt / EBITDA", "Balance Sheet", TIMES),
    MetricSpec("interest_coverage", "EBIT / Interest Expense", "Balance Sheet", TIMES),
    MetricSpec("current_ratio", "Current Ratio", "Balance Sheet", TIMES),
    MetricSpec("debt_to_equity", "Debt / Equity", "Balance Sheet", TIMES),
    MetricSpec("goodwill_pct_assets", "Goodwill % of Assets", "Balance Sheet", PCT,
               "High and rising means the story depends on acquisitions working."),

    # quality flags -- the early-warning section
    MetricSpec("accruals_ratio", "Accruals Ratio ((NI - CFO) / avg assets)",
               "Quality Flags", PCT,
               "Sloan's ratio. High positive = earnings not backed by cash."),
    MetricSpec("receivables_vs_revenue", "AR Growth less Revenue Growth",
               "Quality Flags", PCT,
               "Positive for multiple years is a classic revenue-quality warning."),
    MetricSpec("inventory_vs_revenue", "Inventory Growth less Revenue Growth",
               "Quality Flags", PCT,
               "Positive means inventory is building faster than sales."),
    MetricSpec("net_income_less_fcf", "Net Income less Free Cash Flow",
               "Quality Flags", MONEY),
)

METRIC_SPECS_BY_KEY = {spec.key: spec for spec in METRIC_SPECS}


# ---------------------------------------------------------------------------
# safe arithmetic
# ---------------------------------------------------------------------------
# Real filings have zeros and blanks everywhere. Dividing by them should
# produce an empty cell, not a crash and not "inf".

def _safe_div(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denom = denominator.replace(0, np.nan)
    return numerator / denom


def _growth(series: pd.Series) -> pd.Series:
    """Year-over-year percentage change. Meaningless if the base is negative,
    so those years come back blank rather than misleading."""
    prior = series.shift(1)
    prior = prior.where(prior > 0)
    return (series - prior) / prior


def _not_reported(company, key: str) -> bool:
    """True when the company never tagged this line item at all.

    Distinguishes "this company has no receivables" from "we couldn't find
    the tag" -- the two look identical in the data and mean opposite things.
    """
    for row in company.coverage:
        if row.key == key:
            return row.tag_used is None and not row.derived
    return True


def _average_balance(series: pd.Series) -> pd.Series:
    """(beginning + ending) / 2, falling back to ending for the first year."""
    return ((series + series.shift(1)) / 2).fillna(series)


# ---------------------------------------------------------------------------

def compute(company: NormalizedCompany) -> pd.DataFrame:
    """Return a DataFrame of every metric: rows = metric keys, cols = years."""
    s = company.series  # shorthand

    revenue = s("revenue")
    cogs = s("cogs")
    gross_profit = s("gross_profit")
    operating_income = s("operating_income")
    net_income = s("net_income")
    pretax = s("pretax_income")
    tax = s("tax_expense")
    d_and_a = s("d_and_a")
    cfo = s("cfo")
    capex = s("capex").fillna(0)
    sbc = s("sbc")
    interest = s("interest_expense").abs()

    cash = s("cash").fillna(0)
    sti = s("short_term_investments").fillna(0)
    receivables = s("receivables")
    inventory = s("inventory")
    payables = s("accounts_payable")
    total_assets = s("total_assets")
    equity = s("equity")
    st_debt = s("short_term_debt").fillna(0)
    lt_debt = s("long_term_debt").fillna(0)
    leases = s("operating_lease_liability").fillna(0)

    out: dict[str, pd.Series] = {}

    # -- cash flow basics ---------------------------------------------------
    free_cash_flow = cfo - capex
    ebitda = operating_income + d_and_a.fillna(0)
    out["free_cash_flow"] = free_cash_flow
    out["ebitda"] = ebitda

    # -- growth -------------------------------------------------------------
    out["revenue_growth"] = _growth(revenue)
    out["gross_profit_growth"] = _growth(gross_profit)
    out["operating_income_growth"] = _growth(operating_income)
    out["net_income_growth"] = _growth(net_income)
    out["fcf_growth"] = _growth(free_cash_flow)
    out["eps_growth"] = _growth(s("eps_diluted"))
    shares = s("shares_diluted")
    out["share_count_change"] = shares.pct_change()

    # -- margins ------------------------------------------------------------
    out["gross_margin"] = _safe_div(gross_profit, revenue)
    out["operating_margin"] = _safe_div(operating_income, revenue)
    out["ebitda_margin"] = _safe_div(ebitda, revenue)
    out["net_margin"] = _safe_div(net_income, revenue)
    out["fcf_margin"] = _safe_div(free_cash_flow, revenue)
    out["rnd_pct_revenue"] = _safe_div(s("rnd"), revenue)
    out["sganda_pct_revenue"] = _safe_div(s("sganda"), revenue)

    # -- returns ------------------------------------------------------------
    # Effective tax rate, sanity-bounded. A rate outside 0-50% is usually a
    # one-off (valuation allowance release, tax reform) and would distort NOPAT.
    eff_tax = _safe_div(tax, pretax)
    eff_tax_clean = eff_tax.where((eff_tax >= 0) & (eff_tax <= 0.5))
    out["effective_tax_rate"] = eff_tax

    nopat = operating_income * (1 - eff_tax_clean.fillna(FALLBACK_TAX_RATE))
    out["nopat"] = nopat

    total_debt = st_debt + lt_debt + (leases if INCLUDE_LEASES_IN_NET_DEBT else 0)
    # Use TOTAL equity, including non-controlling interests and redeemable
    # (temporary) equity. Operating income is struck before the NCI's share of
    # profit is carved out, so leaving their capital out of the denominator
    # overstates ROIC for any company with material consolidated subsidiaries
    # it doesn't wholly own.
    total_equity = s("total_equity")
    total_equity = total_equity.fillna(equity + s("minority_interest").fillna(0))
    invested_capital = (
        total_debt + total_equity + s("redeemable_nci").fillna(0)
        + s("redeemable_preferred").fillna(0) - cash - sti
    )
    out["invested_capital"] = invested_capital
    # A return on negative capital is not a return: -$1bn on -$7bn of equity
    # prints as +14% ROE. Blank those years (pre-IPO Uber, heavy-buyback
    # names) rather than show a number with the wrong sign of meaning.
    avg_ic = _average_balance(invested_capital)
    avg_equity = _average_balance(equity)
    out["roic"] = _safe_div(nopat, avg_ic.where(avg_ic > 0))
    out["roe"] = _safe_div(net_income, avg_equity.where(avg_equity > 0))
    out["roa"] = _safe_div(net_income, _average_balance(total_assets))
    out["asset_turnover"] = _safe_div(revenue, _average_balance(total_assets))

    # -- cash quality -------------------------------------------------------
    # Conversion ratios need a meaningful denominator. Caterpillar earned
    # -$59mm on $38bn of revenue in 2016; dividing $4.5bn of free cash flow by
    # that produces -77x, which is arithmetic, not information. Blank out any
    # year where net income is negligible next to revenue.
    meaningful_ni = net_income.abs() > (revenue.abs() * 0.01)
    out["fcf_conversion"] = _safe_div(free_cash_flow, net_income).where(meaningful_ni)
    out["cfo_to_net_income"] = _safe_div(cfo, net_income).where(meaningful_ni)
    out["capex_pct_revenue"] = _safe_div(capex, revenue)
    out["capex_to_d_and_a"] = _safe_div(capex, d_and_a)
    out["sbc_pct_revenue"] = _safe_div(sbc, revenue)
    out["sbc_pct_fcf"] = _safe_div(sbc, free_cash_flow)
    shareholder_return = s("buybacks").fillna(0) + s("dividends").fillna(0)
    out["shareholder_return"] = shareholder_return.where(shareholder_return != 0)
    out["payout_of_fcf"] = _safe_div(shareholder_return, free_cash_flow)

    # -- working capital ----------------------------------------------------
    out["dso"] = _safe_div(_average_balance(receivables), revenue) * 365
    out["dio"] = _safe_div(_average_balance(inventory), cogs) * 365
    out["dpo"] = _safe_div(_average_balance(payables), cogs) * 365
    # The cash conversion cycle has to distinguish two very different reasons a
    # component is blank:
    #
    #   - The company genuinely has none of it. Dollar General sells for cash
    #     and carries no receivables, so its DSO is really zero and its cycle
    #     is inventory days less payable days. Treating that as unknown throws
    #     away a real number.
    #   - We failed to map the tag. Then the cycle is simply unknown, and
    #     filling in a zero silently understates it. Kroger without inventory
    #     days would report a 5-day cycle -- for a grocer holding six weeks of
    #     food on the shelf.
    #
    # The coverage record tells us which case we're in, so use it.
    def term(series: pd.Series, line_item: str) -> pd.Series:
        if _not_reported(company, line_item):
            return series.fillna(0)
        return series  # NaN propagates, blanking the cycle, which is correct

    out["cash_conversion_cycle"] = (
        term(out["dso"], "receivables")
        + term(out["dio"], "inventory")
        - term(out["dpo"], "accounts_payable")
    )

    # -- balance sheet ------------------------------------------------------
    out["total_debt"] = total_debt.where(total_debt != 0)
    net_debt = total_debt - cash - sti
    out["net_debt"] = net_debt
    out["net_debt_to_ebitda"] = _safe_div(net_debt, ebitda)
    out["interest_coverage"] = _safe_div(operating_income, interest)
    out["current_ratio"] = _safe_div(s("current_assets"), s("current_liabilities"))
    out["debt_to_equity"] = _safe_div(total_debt, equity)
    out["goodwill_pct_assets"] = _safe_div(s("goodwill"), total_assets)

    # -- quality flags ------------------------------------------------------
    out["accruals_ratio"] = _safe_div(net_income - cfo, _average_balance(total_assets))
    out["receivables_vs_revenue"] = _growth(receivables) - out["revenue_growth"]
    out["inventory_vs_revenue"] = _growth(inventory) - out["revenue_growth"]
    out["net_income_less_fcf"] = net_income - free_cash_flow

    frame = pd.DataFrame(
        {key: out[key] for key in METRIC_SPECS_BY_KEY if key in out}
    ).T
    frame = frame.reindex([spec.key for spec in METRIC_SPECS])
    frame = frame.reindex(columns=list(company.statements.columns))

    # Blank the growth rows in the TTM column. A trailing-twelve-month period
    # overlaps the fiscal year it would be measured against -- for a company
    # two quarters into its year, half the "growth" is the period compared
    # against itself. The number would look meaningful and mislead.
    if company.ttm_column is not None and company.ttm_column in frame.columns:
        overlapping = [
            spec.key for spec in METRIC_SPECS
            if spec.group == "Growth"
            or spec.key in ("receivables_vs_revenue", "inventory_vs_revenue")
        ]
        rows = [key for key in overlapping if key in frame.index]
        frame.loc[rows, company.ttm_column] = np.nan

    return frame


def summary(company: NormalizedCompany, metric_frame: pd.DataFrame) -> dict[str, float]:
    """A handful of full-period numbers for the comps sheet.

    CAGRs use the first and last year available, which is the right way to
    summarize a decade without letting one weird year dominate.
    """

    def cagr(key: str) -> float | None:
        series = company.series(key).dropna()
        if len(series) < 2:
            return None
        first, last = series.iloc[0], series.iloc[-1]
        if first <= 0 or last <= 0:
            return None
        years = len(series) - 1
        return (last / first) ** (1 / years) - 1

    def latest(frame_key: str) -> float | None:
        if frame_key not in metric_frame.index:
            return None
        series = metric_frame.loc[frame_key].dropna()
        return float(series.iloc[-1]) if len(series) else None

    def mean_of(frame_key: str) -> float | None:
        if frame_key not in metric_frame.index:
            return None
        series = metric_frame.loc[frame_key].dropna()
        return float(series.mean()) if len(series) else None

    def cumulative_ratio(numerator_key: str, denominator_key: str) -> float | None:
        """Sum the numerator and denominator over the whole period, then divide.

        This is the right way to summarize a conversion ratio across a decade.
        Averaging the yearly ratios instead lets one bad year -- a company that
        earned almost nothing but still generated cash -- produce a number like
        -600%, which says nothing about the business. Summing first also
        naturally handles the case of heavy investment years followed by
        harvest years, which is exactly the pattern you want to see through.
        """
        numerator = metric_frame.loc[numerator_key].dropna() \
            if numerator_key in metric_frame.index else company.series(numerator_key).dropna()
        denominator = company.series(denominator_key).dropna()
        shared = numerator.index.intersection(denominator.index)
        if len(shared) < 2:
            return None
        total_denominator = float(denominator[shared].sum())
        if total_denominator <= 0:
            return None
        return float(numerator[shared].sum()) / total_denominator

    return {
        "years_of_data": len(company.fiscal_years),
        "revenue_cagr": cagr("revenue"),
        "net_income_cagr": cagr("net_income"),
        "latest_revenue": company.series("revenue").dropna().iloc[-1]
        if company.series("revenue").notna().any() else None,
        "gross_margin_latest": latest("gross_margin"),
        "operating_margin_latest": latest("operating_margin"),
        "operating_margin_avg": mean_of("operating_margin"),
        "fcf_margin_latest": latest("fcf_margin"),
        "roic_latest": latest("roic"),
        "roic_avg": mean_of("roic"),
        # Cumulative, not averaged -- see cumulative_ratio above.
        "fcf_conversion_avg": cumulative_ratio("free_cash_flow", "net_income"),
        "net_debt_to_ebitda_latest": latest("net_debt_to_ebitda"),
        "accruals_ratio_latest": latest("accruals_ratio"),
        "sbc_pct_revenue_latest": latest("sbc_pct_revenue"),
        "cash_conversion_cycle_latest": latest("cash_conversion_cycle"),
    }
