"""
Settings for the whole program. Everything you might want to change lives here.

Beginner note: a "config" module is just a file of constants that other files
import. It means you change a setting in one place instead of hunting for it in
five different files.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# SEC requirements
# ---------------------------------------------------------------------------
# The SEC requires every automated request to identify who is making it.
# If you don't send this, they will block you.
# They also ask for no more than 10 requests per second -- client.py enforces
# a slower rate than that so we stay well inside the line.
#
# Your name and email are NOT stored in this file, so the code can be shared
# publicly. They're read from (first match wins):
#   1. the SEC_USER_AGENT environment variable, e.g.
#        export SEC_USER_AGENT="Jane Doe jane@example.com"
#   2. a file called sec_user_agent.txt in the project folder (git-ignored;
#      the SETUP launcher creates it for you)
USER_AGENT_FILE = PROJECT_ROOT / "sec_user_agent.txt"


def _load_user_agent() -> str:
    env = os.environ.get("SEC_USER_AGENT", "").strip()
    if env:
        return env
    try:
        for line in USER_AGENT_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


USER_AGENT = _load_user_agent()

SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_SEARCH_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={name}"
    "&type=10-K&dateb=&owner=include&count=40&output=atom"
)
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Seconds to wait between SEC requests. 0.15 = ~6 requests/second.
REQUEST_DELAY_SECONDS = 0.15
REQUEST_TIMEOUT_SECONDS = 60

# ---------------------------------------------------------------------------
# Where files go
# ---------------------------------------------------------------------------
# Downloaded SEC data is cached so that re-running the program is instant and
# doesn't hammer the SEC. Delete this folder to force a fresh download.
CACHE_DIR = PROJECT_ROOT / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output"

# How long a cached file is considered fresh. Companies file quarterly, so a
# day is plenty during normal work.
CACHE_MAX_AGE_HOURS = 24

# ---------------------------------------------------------------------------
# Analysis defaults
# ---------------------------------------------------------------------------
DEFAULT_YEARS = 10  # how many fiscal years of history to pull

# ---------------------------------------------------------------------------
# Share prices (module 4 only)
# ---------------------------------------------------------------------------
# SEC filings contain no prices, so the reverse DCF needs an outside source.
# PREFER THE ENVIRONMENT over writing a key here -- a key is a credential:
#     export BARCHART_API_KEY="your-key"
# Anything set in the environment overrides this value.
BARCHART_API_KEY = ""
BARCHART_QUOTE_URL = "https://marketdata.websol.barchart.com/getQuote.json"

# Free default source: Yahoo Finance's public chart endpoint. No key, no
# account, so anyone who clones this repo gets a working valuation. Unofficial
# and unsupported -- if it ever stops answering, the run carries on with
# whatever Barchart / prices.csv provide. Set to False to never call it.
YAHOO_ENABLED = True
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo rejects requests without a browser-like User-Agent.
YAHOO_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
# Daily history pulled from Yahoo is cached here and refreshed once a day.
YAHOO_HISTORY_DIR = CACHE_DIR / "yahoo_history"

# A hand-entered price this many days old (or undated) stops overriding the
# live sources and becomes a last resort, with a warning.
MANUAL_PRICE_MAX_AGE_DAYS = 5

# Hand-entered prices. Columns: ticker,price[,date]
PRICES_CSV = PROJECT_ROOT / "prices.csv"

# PRICE HISTORY -- what makes "is 11x cheap?" answerable.
# One spot price gives you a multiple; a history tells you whether that
# multiple is high or low FOR THIS COMPANY, which is the only version of the
# question that has an answer.
#
# Two accepted shapes, whichever your data source produces:
#   price_history.csv      one file, needs a symbol/ticker column
#   price_history/         a folder of per-ticker files (VST.csv, CEG.csv...),
#                          ticker taken from the filename when there's no
#                          symbol column -- which is how most "download this
#                          symbol's history" buttons export.
# Either way only a date and a close are needed; extra columns are ignored.
PRICE_HISTORY_CSV = PROJECT_ROOT / "price_history.csv"
PRICE_HISTORY_DIR = PROJECT_ROOT / "price_history"

# ---------------------------------------------------------------------------
# Reverse DCF assumptions (module 4)
# ---------------------------------------------------------------------------
# These are the judgment calls a reverse DCF rests on. Changing them changes
# the answer, so they live here where you can see and defend them.
DEFAULT_WACC = 0.09              # discount rate
DEFAULT_TERMINAL_GROWTH = 0.025  # perpetual growth after the forecast period
DEFAULT_FORECAST_YEARS = 10

# A fiscal year is labeled by the calendar year it mostly covers. A year ending
# in Jan-May is labeled as the PRIOR calendar year, so a retailer with a
# January year-end lines up against a normal December filer. This can differ
# from the company's own "FY2025" label -- that's intentional, because
# comparing economics across companies matters more than matching their
# marketing. Set to False to just use the calendar year of the end date.
CALENDAR_ALIGN_FISCAL_YEARS = True

# A duration fact counts as "annual" if its period is this many days long.
# Fiscal years are 52 or 53 weeks, so this window catches both.
ANNUAL_MIN_DAYS = 340
ANNUAL_MAX_DAYS = 400

# When matching a balance sheet date to a fiscal year end, allow this much
# slack (some companies report the balance sheet a day or two off).
BALANCE_SHEET_DATE_TOLERANCE_DAYS = 10
