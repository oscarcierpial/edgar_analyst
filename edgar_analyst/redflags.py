"""
MODULE 3 -- accounting quality and distress screening.

The job here is not to find fraud. It's to rank companies by how much of their
reported profit you should take at face value, fast enough that you can do it
for a whole sector before lunch.

Everything is rules-based and computed from the normalized statements, so the
same definitions apply to every company. Nothing here is a verdict: a triggered
flag is a question to go ask the filing, and plenty of them have innocent
answers. A company building a new plant will fail the capex test and should.

WHAT'S IN HERE
--------------
Two published scores, because they're what people will expect you to know:

  Beneish M-score   Eight ratios comparing this year to last, weighted into one
                    number. Above -1.78 puts a company in the range where
                    manipulators historically clustered. Beneish's own model
                    caught roughly three-quarters of them, which also means it
                    misses a quarter and flags plenty of honest companies.

  Altman Z-score    Bankruptcy risk. Needs market value of equity, so when
                    there's no price we fall back to Altman's Z'' variant,
                    which uses book equity and has its own thresholds. The
                    output always says which one was used -- comparing a Z to
                    a Z'' is meaningless.

Then a set of plain-language checks that matter more in practice than either
score, because they're specific enough to act on: earnings not backed by cash,
receivables outrunning revenue, inventory building, margins sliding,
underinvestment, dilution, leverage, and restatements.

READ THE DETAIL, NOT THE SCORE. The composite at the end is for sorting a list.
The individual flags are what you actually take to the 10-K.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .normalize import NormalizedCompany

# Beneish's threshold. Above this, a company sits in the range where the model's
# manipulators clustered -- not proof of anything.
BENEISH_THRESHOLD = -1.78

# Altman zones. The two variants are NOT comparable to each other.
ALTMAN_Z_SAFE, ALTMAN_Z_DISTRESS = 2.99, 1.81        # classic, needs market cap
ALTMAN_ZZ_SAFE, ALTMAN_ZZ_DISTRESS = 2.60, 1.10      # Z'' variant, book equity

HIGH, MEDIUM, LOW = "High", "Medium", "Low"
_SEVERITY_POINTS = {HIGH: 12, MEDIUM: 7, LOW: 3}


@dataclass
class Flag:
    """One check. `triggered` False still gets reported -- absence is evidence."""

    key: str
    name: str
    category: str
    triggered: bool
    severity: str
    detail: str
    value: float | None = None


@dataclass
class RedFlagReport:
    ticker: str
    flags: list[Flag] = field(default_factory=list)
    beneish_m: float | None = None
    beneish_components: dict[str, float] = field(default_factory=dict)
    altman_z: float | None = None
    altman_variant: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def triggered(self) -> list[Flag]:
        return [f for f in self.flags if f.triggered]

    @property
    def score(self) -> int:
        """0-100, higher = more to explain. For sorting a list, nothing more."""
        points = sum(_SEVERITY_POINTS[f.severity] for f in self.triggered)
        return int(min(100, points))

    @property
    def verdict(self) -> str:
        score = self.score
        if score >= 45:
            return "Heavy scrutiny"
        if score >= 25:
            return "Several questions"
        if score >= 10:
            return "Minor items"
        return "Clean"

    @property
    def altman_zone(self) -> str:
        if self.altman_z is None:
            return ""
        if self.altman_variant == "Z''":
            safe, distress = ALTMAN_ZZ_SAFE, ALTMAN_ZZ_DISTRESS
        else:
            safe, distress = ALTMAN_Z_SAFE, ALTMAN_Z_DISTRESS
        if self.altman_z >= safe:
            return "Safe"
        if self.altman_z <= distress:
            return "Distress"
        return "Grey"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _val(company: NormalizedCompany, key: str, year) -> float | None:
    try:
        value = company.statements.at[key, year]
    except KeyError:
        return None
    return None if pd.isna(value) else float(value)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _recent_years(company: NormalizedCompany, count: int) -> list[int]:
    """The last `count` FISCAL years -- never the TTM column.

    Year-over-year tests need periods that don't overlap. TTM overlaps the
    fiscal year before it, so including it would compare a period with itself.
    """
    return company.fiscal_years[-count:]


def _streak(values: list[float | None], test) -> int:
    """How many of the most recent consecutive years satisfy `test`."""
    streak = 0
    for value in reversed(values):
        if value is None or not test(value):
            break
        streak += 1
    return streak


# ---------------------------------------------------------------------------
# published scores
# ---------------------------------------------------------------------------

def beneish_m_score(company: NormalizedCompany) -> tuple[float | None, dict]:
    """Beneish's eight-variable earnings manipulation model.

    Each variable compares the latest fiscal year to the one before it, so it
    needs two clean consecutive years. Returns (score, components) and (None,
    reason) when the inputs aren't there -- which is common and not a failure.
    """
    years = _recent_years(company, 2)
    if len(years) < 2:
        return None, {"unavailable": "needs two fiscal years"}
    prior, current = years

    def get(key: str, year) -> float | None:
        return _val(company, key, year)

    revenue_t, revenue_p = get("revenue", current), get("revenue", prior)
    if not revenue_t or not revenue_p or revenue_t <= 0 or revenue_p <= 0:
        return None, {"unavailable": "revenue missing"}

    components: dict[str, float] = {}

    # Days sales in receivables index -- receivables growing faster than sales.
    ar_t, ar_p = get("receivables", current), get("receivables", prior)
    dsri = _ratio(_ratio(ar_t, revenue_t), _ratio(ar_p, revenue_p)) if ar_t and ar_p else 1.0
    components["DSRI"] = dsri or 1.0

    # Gross margin index -- above 1 means margins deteriorated.
    gm_t = _ratio(get("gross_profit", current), revenue_t)
    gm_p = _ratio(get("gross_profit", prior), revenue_p)
    gmi = _ratio(gm_p, gm_t) if gm_t and gm_p and gm_t > 0 else 1.0
    components["GMI"] = gmi or 1.0

    # Asset quality index -- growth in assets that aren't PP&E or current.
    def soft_assets(year) -> float | None:
        assets = get("total_assets", year)
        current_assets = get("current_assets", year) or 0
        ppe = get("ppe", year) or 0
        if not assets:
            return None
        return 1 - (current_assets + ppe) / assets

    aq_t, aq_p = soft_assets(current), soft_assets(prior)
    aqi = _ratio(aq_t, aq_p) if aq_t and aq_p and aq_p > 0 else 1.0
    components["AQI"] = aqi or 1.0

    # Sales growth index. Growth isn't manipulation, but it's the setting.
    components["SGI"] = revenue_t / revenue_p

    # Depreciation index -- above 1 means the rate slowed (assets stretched).
    def dep_rate(year) -> float | None:
        depreciation = get("d_and_a", year)
        ppe = get("ppe", year)
        if not depreciation or not ppe:
            return None
        return depreciation / (depreciation + ppe)

    dep_t, dep_p = dep_rate(current), dep_rate(prior)
    depi = _ratio(dep_p, dep_t) if dep_t and dep_p and dep_t > 0 else 1.0
    components["DEPI"] = depi or 1.0

    # SG&A index.
    sga_t = _ratio(get("sganda", current), revenue_t)
    sga_p = _ratio(get("sganda", prior), revenue_p)
    sgai = _ratio(sga_t, sga_p) if sga_t and sga_p and sga_p > 0 else 1.0
    components["SGAI"] = sgai or 1.0

    # Leverage index.
    def leverage(year) -> float | None:
        assets = get("total_assets", year)
        if not assets:
            return None
        current_liabilities = get("current_liabilities", year) or 0
        long_term_debt = get("long_term_debt", year) or 0
        return (current_liabilities + long_term_debt) / assets

    lev_t, lev_p = leverage(current), leverage(prior)
    lvgi = _ratio(lev_t, lev_p) if lev_t and lev_p and lev_p > 0 else 1.0
    components["LVGI"] = lvgi or 1.0

    # Total accruals to total assets -- the heaviest weight in the model, and
    # the most intuitive: profit that never turned into cash.
    net_income = get("net_income", current)
    cfo = get("cfo", current)
    assets_t = get("total_assets", current)
    tata = ((net_income - cfo) / assets_t
            if None not in (net_income, cfo, assets_t) and assets_t else 0.0)
    components["TATA"] = tata

    m = (
        -4.84
        + 0.920 * components["DSRI"]
        + 0.528 * components["GMI"]
        + 0.404 * components["AQI"]
        + 0.892 * components["SGI"]
        + 0.115 * components["DEPI"]
        - 0.172 * components["SGAI"]
        + 4.679 * components["TATA"]
        - 0.327 * components["LVGI"]
    )
    return m, components


def altman_z_score(
    company: NormalizedCompany, market_cap: float | None = None
) -> tuple[float | None, str]:
    """Altman's bankruptcy score. Returns (score, which variant was used).

    The classic Z needs the market value of equity. Without a price we use
    Z'', Altman's revision for non-manufacturers and private firms, which
    substitutes book equity and drops the sales-to-assets term. They are
    different scales with different cut-offs and must never be compared.
    """
    year = company.fiscal_years[-1]

    assets = _val(company, "total_assets", year)
    if not assets:
        return None, ""

    current_assets = _val(company, "current_assets", year)
    current_liabilities = _val(company, "current_liabilities", year)
    working_capital = (
        current_assets - current_liabilities
        if None not in (current_assets, current_liabilities) else None
    )
    retained = _val(company, "retained_earnings", year)
    ebit = _val(company, "operating_income", year)
    liabilities = _val(company, "total_liabilities", year)
    revenue = _val(company, "revenue", year)
    equity = _val(company, "total_equity", year)

    if liabilities in (None, 0):
        return None, ""

    x1 = (working_capital / assets) if working_capital is not None else 0.0
    x2 = (retained / assets) if retained is not None else 0.0
    x3 = (ebit / assets) if ebit is not None else 0.0

    if market_cap:
        x4 = market_cap / liabilities
        x5 = (revenue / assets) if revenue else 0.0
        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
        return z, "Z"

    if equity is None:
        return None, ""
    x4 = equity / liabilities
    z = 6.56 * x1 + 3.26 * x2 + 6.72 * x3 + 1.05 * x4
    return z, "Z''"


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------

def evaluate(
    company: NormalizedCompany,
    metric_frame: pd.DataFrame,
    market_cap: float | None = None,
) -> RedFlagReport:
    """Run every check and return the report."""
    report = RedFlagReport(ticker=company.ticker)
    years = _recent_years(company, 5)
    latest = company.fiscal_years[-1]

    def metric(key: str, year) -> float | None:
        if key not in metric_frame.index:
            return None
        try:
            value = metric_frame.at[key, year]
        except KeyError:
            return None
        return None if pd.isna(value) else float(value)

    def series(key: str) -> list[float | None]:
        return [metric(key, y) for y in years]

    def add(key, name, category, triggered, severity, detail, value=None):
        report.flags.append(Flag(key, name, category, triggered, severity,
                                 detail, value))

    # -- earnings backed by cash -------------------------------------------
    accruals = metric("accruals_ratio", latest)
    add("accruals", "Accruals ratio", "Earnings quality",
        accruals is not None and accruals > 0.10, HIGH,
        f"{accruals:.1%} of average assets" if accruals is not None
        else "not computable",
        accruals)

    conversion = series("fcf_conversion")
    weak_years = _streak(conversion, lambda v: v < 0.80)
    add("fcf_conversion", "Free cash flow vs net income", "Earnings quality",
        weak_years >= 3, HIGH,
        f"below 80% of net income for {weak_years} straight years"
        if weak_years else "converting normally",
        conversion[-1] if conversion else None)

    cfo_ni = metric("cfo_to_net_income", latest)
    add("cfo_ni", "Operating cash flow vs net income", "Earnings quality",
        cfo_ni is not None and cfo_ni < 0.80, MEDIUM,
        f"{cfo_ni:.0%} of net income" if cfo_ni is not None else "not computable",
        cfo_ni)

    # -- revenue quality ----------------------------------------------------
    ar_gap = series("receivables_vs_revenue")
    ar_years = _streak(ar_gap, lambda v: v > 0.05)
    add("receivables", "Receivables vs revenue growth", "Revenue quality",
        ar_years >= 2, HIGH,
        f"receivables outgrew revenue for {ar_years} straight years"
        if ar_years else "in line with revenue",
        ar_gap[-1] if ar_gap else None)

    inv_gap = series("inventory_vs_revenue")
    inv_years = _streak(inv_gap, lambda v: v > 0.05)
    add("inventory", "Inventory vs revenue growth", "Revenue quality",
        inv_years >= 2, MEDIUM,
        f"inventory outgrew revenue for {inv_years} straight years"
        if inv_years else "in line with revenue",
        inv_gap[-1] if inv_gap else None)

    ccc = series("cash_conversion_cycle")
    clean_ccc = [v for v in ccc if v is not None]
    ccc_worse = (len(clean_ccc) >= 3 and clean_ccc[-1] - clean_ccc[0] > 15)
    add("ccc", "Cash conversion cycle", "Revenue quality",
        ccc_worse, MEDIUM,
        f"lengthened {clean_ccc[-1] - clean_ccc[0]:.0f} days over the period"
        if ccc_worse else "stable or improving",
        clean_ccc[-1] if clean_ccc else None)

    # -- margins and investment --------------------------------------------
    gross = [metric("gross_margin", y) for y in years]
    clean_gross = [v for v in gross if v is not None]
    gross_falling = len(clean_gross) >= 3 and (clean_gross[-1] - clean_gross[0]) < -0.03
    add("gross_margin", "Gross margin trend", "Margins",
        gross_falling, MEDIUM,
        f"down {abs(clean_gross[-1] - clean_gross[0]):.1%} over the period"
        if gross_falling else "stable or improving",
        clean_gross[-1] if clean_gross else None)

    operating = [metric("operating_margin", y) for y in years]
    clean_op = [v for v in operating if v is not None]
    op_falling = len(clean_op) >= 3 and (clean_op[-1] - clean_op[0]) < -0.03
    add("operating_margin", "Operating margin trend", "Margins",
        op_falling, MEDIUM,
        f"down {abs(clean_op[-1] - clean_op[0]):.1%} over the period"
        if op_falling else "stable or improving",
        clean_op[-1] if clean_op else None)

    capex_da = series("capex_to_d_and_a")
    under = _streak(capex_da, lambda v: v < 1.0)
    add("underinvestment", "Capex vs depreciation", "Margins",
        under >= 3, MEDIUM,
        f"capex below depreciation for {under} straight years -- the asset "
        f"base is shrinking" if under else "reinvesting at or above depreciation",
        capex_da[-1] if capex_da else None)

    # -- shareholder economics ---------------------------------------------
    sbc = metric("sbc_pct_revenue", latest)
    add("sbc", "Stock-based compensation", "Shareholder economics",
        sbc is not None and sbc > 0.10, MEDIUM,
        f"{sbc:.1%} of revenue" if sbc is not None else "not reported", sbc)

    share_change = series("share_count_change")
    dilution = _streak(share_change, lambda v: v > 0.01)
    add("dilution", "Share count", "Shareholder economics",
        dilution >= 3, MEDIUM,
        f"diluting for {dilution} straight years" if dilution
        else "flat or shrinking",
        share_change[-1] if share_change else None)

    # -- balance sheet ------------------------------------------------------
    leverage = metric("net_debt_to_ebitda", latest)
    add("leverage", "Net debt / EBITDA", "Balance sheet",
        leverage is not None and leverage > 4.0, HIGH,
        f"{leverage:.1f}x" if leverage is not None else "not computable",
        leverage)

    coverage = metric("interest_coverage", latest)
    add("coverage", "Interest coverage", "Balance sheet",
        coverage is not None and coverage < 2.0, HIGH,
        f"EBIT covers interest {coverage:.1f}x" if coverage is not None
        else "no interest expense reported",
        coverage)

    goodwill = metric("goodwill_pct_assets", latest)
    add("goodwill", "Goodwill share of assets", "Balance sheet",
        goodwill is not None and goodwill > 0.40, MEDIUM,
        f"{goodwill:.0%} of total assets -- the story depends on acquisitions "
        f"working" if goodwill is not None and goodwill > 0.40
        else (f"{goodwill:.0%} of assets" if goodwill is not None else "none"),
        goodwill)

    equity = _val(company, "total_equity", latest)
    add("negative_equity", "Shareholders' equity", "Balance sheet",
        equity is not None and equity < 0, LOW,
        "negative" if equity is not None and equity < 0 else "positive", equity)

    # -- filing behaviour ---------------------------------------------------
    restated = sorted({y for row in company.coverage for y in row.restated_years})
    add("restatements", "Restated figures", "Filing behaviour",
        len(restated) >= 2, MEDIUM,
        f"values restated across {len(restated)} year(s): "
        f"{', '.join(map(str, restated[:6]))}" if restated
        else "no restatements detected",
        float(len(restated)))

    # -- published scores ---------------------------------------------------
    report.beneish_m, report.beneish_components = beneish_m_score(company)
    if report.beneish_m is not None:
        sgi = report.beneish_components.get("SGI", 1.0)
        detail = f"{report.beneish_m:.2f} (threshold {BENEISH_THRESHOLD})"
        # The model's single largest input is sales growth, so it flags fast
        # growers as a matter of arithmetic. NVIDIA scores badly on it while
        # growing 65% a year -- that is the model describing growth, not
        # detecting manipulation. Say so rather than let the flag imply more
        # than it knows.
        if sgi > 1.30:
            detail += (
                f" -- but driven by {sgi - 1:.0%} sales growth, which this model "
                f"penalises mechanically. Read it as 'growing fast enough that "
                f"accruals deserve a look', not as a manipulation signal."
            )
        add("beneish", "Beneish M-score", "Published scores",
            report.beneish_m > BENEISH_THRESHOLD, HIGH, detail,
            report.beneish_m)
    else:
        report.notes.append(
            "Beneish M-score not computed: "
            f"{report.beneish_components.get('unavailable', 'inputs missing')}."
        )

    report.altman_z, report.altman_variant = altman_z_score(company, market_cap)
    if report.altman_z is not None:
        zone = report.altman_zone
        # Altman calibrated Z on manufacturers. Businesses that are leveraged
        # by design -- utilities, independent power producers, banks, REITs,
        # airlines -- score in the distress zone permanently without being in
        # any distress at all. Flagging Vistra as distressed every single year
        # teaches you to ignore the flag, which is worse than not having it.
        industry = (company.industry or "").lower()
        structurally_levered = any(word in industry for word in (
            "electric", "gas", "utility", "utilities", "water supply", "pipeline",
            "bank", "credit", "insurance", "real estate", "investment",
            "air transportation", "railroad",
        ))
        if structurally_levered:
            report.notes.append(
                f"Altman's score is calibrated on manufacturers. {company.industry} "
                f"is leveraged by design, so a low score here reflects the business "
                f"model rather than distress. Use the leverage and coverage flags "
                f"instead, and compare against sector peers, not the raw zones."
            )
        add("altman", f"Altman {report.altman_variant}-score", "Published scores",
            zone == "Distress" and not structurally_levered, HIGH,
            f"{report.altman_z:.2f} -- {zone} zone"
            + ("" if market_cap else " (book-equity variant, no price available)")
            + (" [sector is leveraged by design; see notes]"
               if structurally_levered else ""),
            report.altman_z)

    if company.ttm is None:
        report.notes.append(
            "No quarterly data newer than the 10-K, so every flag is as of the "
            "last fiscal year end."
        )
    return report
