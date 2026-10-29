"""
edgar_analyst -- module 2 of the equity research toolkit.

Pulls SEC XBRL data, maps every company's idiosyncratic tags onto one standard
chart of accounts, computes the ratios that matter, and writes an Excel model.

Quick start in Python (rather than the command line):

    from edgar_analyst import build

    company, metric_frame, stats = build("AAPL")
    print(metric_frame.loc["roic"])
"""

from __future__ import annotations

from . import config, export, metrics, taxonomy
from .client import EDGARClient, SECError
from .normalize import NormalizedCompany, Normalizer
from .pipeline import load_company

__all__ = [
    "build",
    "load_company",
    "EDGARClient",
    "SECError",
    "Normalizer",
    "NormalizedCompany",
    "config",
    "export",
    "metrics",
    "taxonomy",
]

__version__ = "0.1.0"


def build(ticker: str, years: int = config.DEFAULT_YEARS):
    """Convenience wrapper: ticker in, (company, metrics, summary stats) out.

    Does not write a file -- use edgar_analyst.export for that, or the command
    line. This is the function the other modules (filing diff, red-flag
    screener, reverse DCF) will call.
    """
    company, _notes = load_company(ticker, EDGARClient(), Normalizer(years=years))
    metric_frame = metrics.compute(company)
    return company, metric_frame, metrics.summary(company, metric_frame)
