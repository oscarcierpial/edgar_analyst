"""
The mapping layer -- the heart of the whole module.

THE PROBLEM THIS SOLVES
-----------------------
Every public company tags its financials in XBRL, but the US-GAAP taxonomy has
thousands of tags and companies pick different ones for the same economic idea.
Apple's revenue is tagged `RevenueFromContractWithCustomerExcludingAssessedTax`.
An older filer might use `SalesRevenueNet`. A conglomerate might use `Revenues`.
All three mean "revenue."

So for each standard line item we keep an ORDERED list of candidate tags. The
normalizer walks the list and takes the first one the company actually reports.
Order matters: the most specific / most modern tag goes first.

This file is where you'll spend your time as you run into more companies. When
a line item comes back empty for some company, look at its filing, find the tag
it used, and add that tag to the right candidate list. The program gets better
every time you do that.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# XBRL facts come in two flavors and you must not mix them up:
#   DURATION -- covers a span of time (revenue for the year). Has start + end.
#   INSTANT  -- a snapshot at one moment (cash on the balance sheet date). End only.
DURATION = "duration"
INSTANT = "instant"

INCOME_STATEMENT = "Income Statement"
BALANCE_SHEET = "Balance Sheet"
CASH_FLOW = "Cash Flow"


@dataclass(frozen=True)
class LineItem:
    """One standardized row in our model."""

    key: str  # short internal name, e.g. "revenue"
    label: str  # what shows up in the spreadsheet
    statement: str  # which sheet it belongs on
    kind: str  # DURATION or INSTANT
    candidates: tuple[str, ...]  # US-GAAP tags to try, best first
    unit: str = "USD"
    taxonomy: str = "us-gaap"
    # Some items are optional -- their absence is normal, not a data problem.
    # (A software company has no inventory; a debt-free company has no interest.)
    optional: bool = False
    notes: str = ""


LINE_ITEMS: tuple[LineItem, ...] = (
    # ---------------------------------------------------------------- income
    LineItem(
        "revenue", "Revenue", INCOME_STATEMENT, DURATION,
        (
            # `Revenues` is the US-GAAP element for TOTAL revenue, so it goes
            # first. This ordering matters more than it looks. Walmart tags
            # net sales as RevenueFromContractWithCustomer... ($642.6bn in the
            # year to Jan 2025) but its income statement total revenues are
            # $648.1bn -- the difference is membership and other income. Put
            # the contract-revenue tag first and you quietly understate the top
            # line of every company that has revenue outside customer
            # contracts: retailers with membership fees, anyone with material
            # interest or royalty income.
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenuesNetOfInterestExpense",  # banks
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            # Last resort only: this one is GROSS of sales taxes collected on
            # customers' behalf, which isn't really the company's revenue.
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
    ),
    LineItem(
        "cogs", "Cost of Revenue", INCOME_STATEMENT, DURATION,
        (
            "CostOfGoodsAndServicesSold",
            "CostOfRevenue",
            "CostOfGoodsSold",
            "CostOfServices",
            # Variants that EXCLUDE depreciation and amortization. Kroger
            # switched to the first of these after FY2017 and stopped using
            # CostOfGoodsSold entirely -- without it, a $147bn grocer has no
            # cost of goods, no gross margin, and no inventory days at all.
            # Note the singular "Service" in the first tag; the plural version
            # is a different, also-used tag.
            #
            # Gross margin from these is NOT comparable to a company that puts
            # D&A in cost of sales -- it will look better. normalize.py raises
            # a warning whenever one of these is the source.
            "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
            "CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization",
            "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
        ),
        optional=True,
    ),
    LineItem(
        "gross_profit", "Gross Profit", INCOME_STATEMENT, DURATION,
        ("GrossProfit",),
        optional=True,
        notes="Derived as Revenue - Cost of Revenue when not tagged directly.",
    ),
    LineItem(
        "rnd", "Research & Development", INCOME_STATEMENT, DURATION,
        ("ResearchAndDevelopmentExpense",
         "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
        optional=True,
    ),
    LineItem(
        "sganda", "Selling, General & Administrative", INCOME_STATEMENT, DURATION,
        ("SellingGeneralAndAdministrativeExpense",),
        optional=True,
        notes=(
            "Derived as Sales & Marketing + G&A when the company reports them "
            "separately. Uber does: mapping SG&A to G&A alone put it at 6% of "
            "revenue against a true ~20%, silently wrong next to any peer "
            "that reports the combined line."
        ),
    ),
    LineItem(
        "selling_marketing", "Sales & Marketing", INCOME_STATEMENT, DURATION,
        ("SellingAndMarketingExpense", "SellingExpense"),
        optional=True,
    ),
    LineItem(
        "g_and_a", "General & Administrative", INCOME_STATEMENT, DURATION,
        ("GeneralAndAdministrativeExpense",),
        optional=True,
    ),
    LineItem(
        "operating_expenses", "Total Operating Expenses", INCOME_STATEMENT, DURATION,
        ("OperatingExpenses", "CostsAndExpenses"),
        optional=True,
    ),
    LineItem(
        "operating_income", "Operating Income", INCOME_STATEMENT, DURATION,
        ("OperatingIncomeLoss",),
        notes="Derived as Gross Profit - Operating Expenses when not tagged.",
    ),
    LineItem(
        "interest_expense", "Interest Expense", INCOME_STATEMENT, DURATION,
        ("InterestExpense",
         "InterestExpenseNonoperating",
         "InterestExpenseDebt",
         "InterestIncomeExpenseNet"),
        optional=True,
    ),
    LineItem(
        "pretax_income", "Pre-Tax Income", INCOME_STATEMENT, DURATION,
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
        ),
        optional=True,
    ),
    LineItem(
        "tax_expense", "Income Tax Expense", INCOME_STATEMENT, DURATION,
        ("IncomeTaxExpenseBenefit",
         "IncomeTaxExpenseBenefitContinuingOperations"),
        optional=True,
    ),
    LineItem(
        "net_income", "Net Income", INCOME_STATEMENT, DURATION,
        ("NetIncomeLoss",
         "ProfitLoss",
         "NetIncomeLossAvailableToCommonStockholdersBasic"),
    ),
    LineItem(
        "eps_diluted", "EPS (Diluted)", INCOME_STATEMENT, DURATION,
        ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
        unit="USD/shares", optional=True,
    ),
    LineItem(
        "shares_diluted", "Diluted Shares Outstanding", INCOME_STATEMENT, DURATION,
        ("WeightedAverageNumberOfDilutedSharesOutstanding",
         "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
        unit="shares", optional=True,
    ),
    LineItem(
        "shares_basic", "Basic Shares Outstanding", INCOME_STATEMENT, DURATION,
        ("WeightedAverageNumberOfSharesOutstandingBasic",),
        unit="shares", optional=True,
    ),

    LineItem(
        "shares_outstanding", "Shares Outstanding (cover page)",
        INCOME_STATEMENT, INSTANT,
        ("EntityCommonStockSharesOutstanding",),
        unit="shares", taxonomy="dei", optional=True,
        notes=(
            "From the 10-K/10-Q cover page -- the most current share count "
            "available, and what market cap should be built from. Diluted "
            "weighted-average shares are an average over the period, which is "
            "the wrong number for a point-in-time market cap."
        ),
    ),

    # --------------------------------------------------------- balance sheet
    LineItem(
        "cash", "Cash & Equivalents", BALANCE_SHEET, INSTANT,
        ("CashAndCashEquivalentsAtCarryingValue",
         "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
         "CashAndDueFromBanks"),
    ),
    LineItem(
        "short_term_investments", "Short-Term Investments", BALANCE_SHEET, INSTANT,
        ("ShortTermInvestments",
         "MarketableSecuritiesCurrent",
         "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
         "OtherShortTermInvestments"),
        optional=True,
    ),
    LineItem(
        "receivables", "Accounts Receivable", BALANCE_SHEET, INSTANT,
        ("AccountsReceivableNetCurrent",
         "ReceivablesNetCurrent",
         "AccountsAndOtherReceivablesNetCurrent",
         "AccountsReceivableGrossCurrent"),
        optional=True,
    ),
    LineItem(
        "inventory", "Inventory", BALANCE_SHEET, INSTANT,
        (
            "InventoryNet",
            # Retailers and apparel companies very often use a finished-goods
            # tag instead, and never touch InventoryNet. Ulta, Lululemon and
            # Nike all do -- which means without these a beauty retailer comes
            # back with no inventory line and no inventory days at all.
            "InventoryFinishedGoodsNetOfReserves",
            "InventoryFinishedGoods",
            "RetailRelatedInventory",
            "InventoryGross",
            # Last resort: grocers on LIFO (Kroger) report FIFO inventory plus
            # a separate reserve. This figure is GROSS of that reserve, so the
            # derivation below subtracts it to get the balance sheet number.
            "FIFOInventoryAmount",
        ),
        optional=True,
    ),
    LineItem(
        "lifo_reserve", "LIFO Reserve", BALANCE_SHEET, INSTANT,
        ("InventoryLIFOReserve",),
        optional=True,
        notes=(
            "Only reported by companies on LIFO. Subtracted from FIFO "
            "inventory to reach the carrying value. Kroger's is ~26% of "
            "inventory, so ignoring it badly overstates inventory days."
        ),
    ),
    LineItem(
        "current_assets", "Total Current Assets", BALANCE_SHEET, INSTANT,
        ("AssetsCurrent",), optional=True,
    ),
    LineItem(
        "ppe", "Property, Plant & Equipment (net)", BALANCE_SHEET, INSTANT,
        ("PropertyPlantAndEquipmentNet",
         "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
        optional=True,
    ),
    LineItem(
        "goodwill", "Goodwill", BALANCE_SHEET, INSTANT,
        ("Goodwill",), optional=True,
    ),
    LineItem(
        "intangibles", "Intangible Assets (ex-Goodwill)", BALANCE_SHEET, INSTANT,
        ("FiniteLivedIntangibleAssetsNet",
         "IntangibleAssetsNetExcludingGoodwill"),
        optional=True,
    ),
    LineItem(
        "total_assets", "Total Assets", BALANCE_SHEET, INSTANT,
        ("Assets",),
    ),
    LineItem(
        "accounts_payable", "Accounts Payable", BALANCE_SHEET, INSTANT,
        ("AccountsPayableCurrent",
         "AccountsPayableAndAccruedLiabilitiesCurrent",
         "AccountsPayableTradeCurrent"),
        optional=True,
    ),
    LineItem(
        "current_liabilities", "Total Current Liabilities", BALANCE_SHEET, INSTANT,
        ("LiabilitiesCurrent",), optional=True,
    ),
    LineItem(
        "short_term_debt", "Short-Term Debt & Current Maturities", BALANCE_SHEET, INSTANT,
        ("LongTermDebtCurrent",
         "DebtCurrent",
         "ShortTermBorrowings",
         "OtherShortTermBorrowings"),
        optional=True,
    ),
    LineItem(
        "long_term_debt", "Long-Term Debt", BALANCE_SHEET, INSTANT,
        ("LongTermDebtNoncurrent",
         "LongTermDebt",
         "LongTermDebtAndCapitalLeaseObligations"),
        optional=True,
    ),
    LineItem(
        "operating_lease_liability", "Operating Lease Liability (non-current)",
        BALANCE_SHEET, INSTANT,
        ("OperatingLeaseLiabilityNoncurrent",),
        optional=True,
        notes="Leases are debt-like. Worth adding to net debt for retailers and airlines.",
    ),
    LineItem(
        "retained_earnings", "Retained Earnings", BALANCE_SHEET, INSTANT,
        ("RetainedEarningsAccumulatedDeficit",),
        optional=True,
        notes=(
            "Cumulative profit never paid out. Altman's Z-score leans on it as "
            "a measure of how much of the balance sheet was self-funded -- a "
            "young or serially loss-making company scores badly here, which is "
            "the point."
        ),
    ),
    LineItem(
        "total_liabilities", "Total Liabilities", BALANCE_SHEET, INSTANT,
        ("Liabilities",), optional=True,
    ),
    LineItem(
        "equity", "Shareholders' Equity (parent)", BALANCE_SHEET, INSTANT,
        ("StockholdersEquity",
         "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        notes="Parent-company equity, excluding non-controlling interests.",
    ),
    LineItem(
        "redeemable_nci", "Redeemable Non-Controlling Interests", BALANCE_SHEET, INSTANT,
        ("RedeemableNoncontrollingInterestEquityCarryingAmount",
         "RedeemableNoncontrollingInterestEquityFairValue"),
        optional=True,
        notes=(
            "'Temporary equity' -- sits between liabilities and equity because "
            "the holder can force the company to buy them out. Walmart carried "
            "billions here during the Flipkart years. Excluded from both sides "
            "of the balance check, it looks like the balance sheet doesn't tie."
        ),
    ),
    LineItem(
        "redeemable_preferred", "Redeemable Preferred (mezzanine)",
        BALANCE_SHEET, INSTANT,
        # Only the parent-attributable tag: the "including NCI" variant would
        # double count anything already in redeemable_nci.
        ("TemporaryEquityCarryingAmountAttributableToParent",),
        optional=True,
        notes=(
            "Pre-IPO companies carry their venture preferred stock here, "
            "between liabilities and equity. Uber's FY2018 balance sheet "
            "missed by $14.2bn without it -- that was the convertible "
            "preferred, which turned into common stock at the 2019 IPO."
        ),
    ),
    LineItem(
        "total_equity", "Total Equity (incl. NCI)", BALANCE_SHEET, INSTANT,
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
        optional=True,
        notes="Derived as parent equity + non-controlling interests when not tagged.",
    ),
    LineItem(
        "minority_interest", "Non-Controlling Interests", BALANCE_SHEET, INSTANT,
        ("MinorityInterest",
         "StockholdersEquityAttributableToNoncontrollingInterest"),
        optional=True,
        notes=(
            "The slice of consolidated subsidiaries owned by someone else. "
            "Exxon carries ~2% of its balance sheet here. It matters twice: "
            "assets = liabilities + parent equity + NCI (not without it), and "
            "operating income includes the NCI's share of profit, so invested "
            "capital must include their capital or ROIC is overstated."
        ),
    ),

    # ------------------------------------------------------------- cash flow
    LineItem(
        "cfo", "Cash from Operations", CASH_FLOW, DURATION,
        ("NetCashProvidedByUsedInOperatingActivities",
         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ),
    LineItem(
        "capex", "Capital Expenditures", CASH_FLOW, DURATION,
        ("PaymentsToAcquirePropertyPlantAndEquipment",
         "PaymentsToAcquireProductiveAssets",
         "PaymentsForCapitalImprovements",
         "PaymentsToAcquireOtherPropertyPlantAndEquipment"),
        optional=True,
        notes="Reported as a positive number (cash paid out).",
    ),
    LineItem(
        "d_and_a", "Depreciation & Amortization", CASH_FLOW, DURATION,
        ("DepreciationDepletionAndAmortization",
         "DepreciationAmortizationAndAccretionNet",
         "DepreciationAndAmortization",
         "Depreciation"),
        optional=True,
    ),
    LineItem(
        "sbc", "Stock-Based Compensation", CASH_FLOW, DURATION,
        ("ShareBasedCompensation",
         "AllocatedShareBasedCompensationExpense"),
        optional=True,
        notes="A real cost. Watch it as a share of revenue and of free cash flow.",
    ),
    LineItem(
        "cfi", "Cash from Investing", CASH_FLOW, DURATION,
        ("NetCashProvidedByUsedInInvestingActivities",
         "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"),
        optional=True,
    ),
    LineItem(
        "cff", "Cash from Financing", CASH_FLOW, DURATION,
        ("NetCashProvidedByUsedInFinancingActivities",
         "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"),
        optional=True,
    ),
    LineItem(
        "buybacks", "Share Repurchases", CASH_FLOW, DURATION,
        ("PaymentsForRepurchaseOfCommonStock",
         "PaymentsForRepurchaseOfEquity"),
        optional=True,
    ),
    LineItem(
        "dividends", "Dividends Paid", CASH_FLOW, DURATION,
        ("PaymentsOfDividendsCommonStock",
         "PaymentsOfDividends",
         "PaymentsOfDistributionsToAffiliates"),
        optional=True,
    ),
    LineItem(
        "acquisitions", "Cash Paid for Acquisitions", CASH_FLOW, DURATION,
        ("PaymentsToAcquireBusinessesNetOfCashAcquired",
         "PaymentsToAcquireBusinessesAndInterestInAffiliatesGross"),
        optional=True,
        notes="Capital allocation: compare cumulative M&A spend to the returns it produced.",
    ),
)


# Quick lookups used elsewhere in the program.
LINE_ITEMS_BY_KEY: dict[str, LineItem] = {item.key: item for item in LINE_ITEMS}


def items_for(statement: str) -> list[LineItem]:
    """All line items belonging to one statement, in display order."""
    return [item for item in LINE_ITEMS if item.statement == statement]
