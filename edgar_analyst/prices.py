"""
Share prices -- the one input the SEC doesn't provide.

Everything else in this toolkit comes from filings. Valuation doesn't work that
way: you need what the market is paying right now, and that has to come from
somewhere else.

SOURCES, IN ORDER
-----------------
1. `prices.csv`, for any row dated within the last few days -- a price you
   typed on purpose (a close you want to model, a deal price) wins.
2. The Barchart OnDemand API, if a key is configured. Optional.
3. Yahoo Finance's public chart endpoint -- free, no key. This is the default,
   so a fresh clone of the repo values companies out of the box.
4. Any older or undated `prices.csv` row, as a last resort, flagged as stale.

Yahoo also supplies daily price history (cached once a day) for any ticker
that has no history file of its own, which is what lets the multiples sheet
say whether today's multiple is high or low for this company.

The fallback isn't an afterthought. An API key expires, a network blocks an
outbound call, you're on a plane -- and the reverse DCF is the one module that
becomes completely useless without a price. Being able to type six numbers into
a CSV and carry on is worth the twenty lines it costs.

A missing price is never fatal: the company still gets its statements, ratios
and red flags, and only the valuation sheet is skipped, with a note saying why.

SETTING YOUR KEY
----------------
Preferred -- set it in the environment so it never lands in a file you might
share or commit:

    export BARCHART_API_KEY="your-key"

Or put it in config.py as BARCHART_API_KEY. If you do that, don't post the file
anywhere. A key is a credential; treat it like a password.

A note on using a work key: check that personal use is acceptable before you
point this at your employer's account. Rate limits and entitlements on a
corporate key are usually tied to a contract.
"""

from __future__ import annotations

import csv
import os
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

from . import config


@dataclass
class PriceHistory:
    """Daily closes per ticker, for pricing past fiscal year ends.

    Held as a sorted list per ticker rather than a DataFrame: the lookups are
    "closest trading day at or before date X", there are a few thousand rows,
    and bisect beats pandas indexing for both speed and clarity here.
    """

    by_ticker: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Stock splits per ticker, (date, ratio) with ratio = new shares per old
    # (4.0 for a 4-for-1). Price histories from Yahoo and from most site
    # downloads are split-ADJUSTED, but EDGAR share counts for older years
    # are not, so multiples.py needs these to put both on the same basis.
    splits: dict[str, list[tuple[date, float]]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_ticker)

    def tickers(self) -> list[str]:
        return sorted(self.by_ticker)

    def close_near(self, ticker: str, target: date,
                   window_days: int = 10) -> float | None:
        """Close on `target`, or the most recent trading day before it.

        Looking BACKWARD rather than to the nearest day is deliberate: a fiscal
        year ending on a weekend or holiday should be priced at the last day
        the market was actually open, not at a price from the following week
        that already reflects news the year end didn't.
        """
        rows = self.by_ticker.get(ticker.upper())
        if not rows:
            return None
        index = bisect_right(rows, (target, float("inf"))) - 1
        if index < 0:
            return None
        found_date, close = rows[index]
        if (target - found_date).days > window_days:
            return None
        return close


@dataclass
class Quote:
    ticker: str
    price: float
    as_of: str
    source: str          # "prices.csv", "barchart" or "yahoo"
    name: str = ""

    @property
    def age_days(self) -> int | None:
        parsed = PriceClient._parse_date(self.as_of) if self.as_of else None
        return (date.today() - parsed).days if parsed else None

    @property
    def is_stale_manual(self) -> bool:
        """A hand-entered price that's undated or old is worth flagging."""
        if self.source != "prices.csv":
            return False
        age = self.age_days
        return age is None or age > config.MANUAL_PRICE_MAX_AGE_DAYS


class PriceClient:
    """Fetches share prices, with a manual fallback."""

    def __init__(self, api_key: str | None = None, prices_csv: Path | None = None):
        # Three places, in order of preference. The key file matters most in
        # practice: this program is launched by double-clicking, and an
        # environment variable exported in a Terminal window does not reach a
        # double-clicked script. Telling someone to "just export it" would mean
        # the feature silently never works for the way they actually run this.
        self.api_key = (
            api_key
            or os.environ.get("BARCHART_API_KEY")
            or self._read_key_file()
            or getattr(config, "BARCHART_API_KEY", "")
        ).strip()
        self.prices_csv = Path(prices_csv or config.PRICES_CSV)
        self.session = requests.Session()
        self.notes: list[str] = []

    @staticmethod
    def _read_key_file() -> str:
        """Read the key from barchart_key.txt next to the program, if present.

        Kept out of config.py on purpose: config.py is the file you'd send
        someone if they asked how this works, and a credential shouldn't ride
        along with it. This file is in .gitignore for the same reason.
        """
        path = config.PROJECT_ROOT / "barchart_key.txt"
        try:
            if path.exists():
                # Ignore blank lines and anything commented out, so the file
                # can carry its own instructions.
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except OSError:
            pass
        return ""

    # -- Barchart ----------------------------------------------------------

    def _fetch_barchart(self, tickers: list[str]) -> dict[str, Quote]:
        if not self.api_key:
            return {}
        quotes: dict[str, Quote] = {}
        # The API takes a comma-separated list, so one call covers a whole
        # peer group. Chunked to stay clear of URL length limits.
        for start in range(0, len(tickers), 25):
            batch = tickers[start:start + 25]
            try:
                response = self.session.get(
                    config.BARCHART_QUOTE_URL,
                    params={"apikey": self.api_key, "symbols": ",".join(batch)},
                    timeout=30,
                )
            except requests.RequestException as exc:
                self.notes.append(f"Barchart request failed ({exc}).")
                break

            if response.status_code == 401 or response.status_code == 403:
                self.notes.append(
                    "Barchart rejected the API key (HTTP "
                    f"{response.status_code}). Check BARCHART_API_KEY."
                )
                break
            if response.status_code != 200:
                self.notes.append(f"Barchart returned HTTP {response.status_code}.")
                break

            try:
                payload = response.json()
            except ValueError:
                self.notes.append("Barchart returned a response that isn't JSON.")
                break

            status = payload.get("status", {})
            if status.get("code") not in (200, None):
                self.notes.append(
                    f"Barchart error {status.get('code')}: {status.get('message')}"
                )
                break

            for row in payload.get("results") or []:
                symbol = str(row.get("symbol", "")).upper()
                price = row.get("lastPrice", row.get("close"))
                if not symbol or price in (None, ""):
                    continue
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                quotes[symbol] = Quote(
                    ticker=symbol,
                    price=price,
                    as_of=str(row.get("tradeTimestamp") or row.get("serverTimestamp")
                              or date.today().isoformat())[:10],
                    source="barchart",
                    name=str(row.get("name", "")),
                )
        return quotes

    # -- manual ------------------------------------------------------------

    # Column names we accept for each field. The first list is what this
    # program writes; the rest are what you get from a Barchart watchlist,
    # portfolio or screener export, so one of those files can be saved as
    # prices.csv and used as-is without editing a single cell.
    TICKER_COLUMNS = ("ticker", "symbol", "sym")
    PRICE_COLUMNS = ("price", "last", "last price", "lastprice", "last trade",
                     "close", "prev close", "previous close", "settlement")
    DATE_COLUMNS = ("date", "trade time", "last trade time", "time",
                    "exchange date", "timestamp")
    # For history files the close is what we want, never the last/intraday.
    HISTORY_PRICE_COLUMNS = ("close", "close price", "adj close",
                             "adjusted close", "last", "price", "settlement")

    @classmethod
    def _pick(cls, row: dict, names: tuple[str, ...]) -> str:
        for name in names:
            if name in row and str(row[name]).strip():
                return str(row[name]).strip()
        return ""

    def _read_csv(self) -> dict[str, Quote]:
        """Read prices.csv -- either the template, or a Barchart CSV export.

        Barchart Premier includes watchlist and screener downloads but no API,
        so the realistic path is: export from the site, save it here, run. That
        only works if we accept their column names rather than insisting on
        ours, which costs nothing and removes the only manual step.
        """
        if not self.prices_csv.exists():
            return {}
        quotes: dict[str, Quote] = {}
        try:
            with self.prices_csv.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    lowered = {str(k).strip().lower(): v
                               for k, v in row.items() if k}
                    ticker = self._pick(lowered, self.TICKER_COLUMNS).upper()
                    # Barchart exports append a footer line such as
                    # "Downloaded from Barchart.com as of ..." -- it parses as
                    # a row with a junk ticker, so drop anything that isn't a
                    # plausible symbol.
                    if not ticker or len(ticker) > 6 or " " in ticker:
                        continue
                    raw_price = (self._pick(lowered, self.PRICE_COLUMNS)
                                 .replace("$", "").replace(",", "").lstrip("+"))
                    if not ticker or not raw_price:
                        continue
                    try:
                        price = float(raw_price)
                    except ValueError:
                        continue
                    quotes[ticker] = Quote(
                        ticker=ticker,
                        price=price,
                        as_of=self._pick(lowered, self.DATE_COLUMNS)[:10],
                        source="prices.csv",
                    )
        except OSError as exc:
            self.notes.append(f"Couldn't read {self.prices_csv.name}: {exc}")
        return quotes

    def write_csv_template(self, tickers: list[str]) -> Path:
        """Create a prices.csv skeleton so there's something obvious to fill in."""
        self.prices_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.prices_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ticker", "price", "date"])
            for ticker in tickers:
                writer.writerow([ticker.upper(), "", date.today().isoformat()])
        return self.prices_csv

    # -- public ------------------------------------------------------------

    # -- price history -----------------------------------------------------

    DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y",
                    "%Y/%m/%d", "%b %d, %Y", "%d-%b-%Y", "%m-%d-%Y")

    @classmethod
    def _parse_date(cls, text: str) -> date | None:
        """Parse a date in whatever format the export happened to use.

        Worth the loop: date format is the single most common reason a
        perfectly good history file silently loads zero rows, and the failure
        is invisible -- you just get no historical multiples and no error.
        """
        text = (text or "").strip()
        if not text:
            return None
        # Trim a time component if one came along.
        text = text.split("T")[0].split(" ")[0] if len(text) > 10 else text
        for fmt in cls.DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    def _read_history_file(self, path: Path,
                           default_ticker: str | None = None) -> dict[str, list]:
        rows_by_ticker: dict[str, list[tuple[date, float]]] = {}
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    lowered = {str(k).strip().lower(): v
                               for k, v in row.items() if k}
                    ticker = (self._pick(lowered, self.TICKER_COLUMNS).upper()
                              or (default_ticker or "").upper())
                    if not ticker or len(ticker) > 6 or " " in ticker:
                        continue
                    when = self._parse_date(self._pick(lowered, self.DATE_COLUMNS))
                    if when is None:
                        continue
                    raw = (self._pick(lowered, self.HISTORY_PRICE_COLUMNS)
                           .replace("$", "").replace(",", ""))
                    try:
                        close = float(raw)
                    except ValueError:
                        continue
                    rows_by_ticker.setdefault(ticker, []).append((when, close))
        except OSError as exc:
            self.notes.append(f"Couldn't read {path.name}: {exc}")
        return rows_by_ticker

    def load_price_history(self, tickers: list[str] | None = None) -> PriceHistory:
        """Load daily closes from price_history.csv and/or price_history/.

        Any of `tickers` with no local history file is filled from Yahoo (if
        enabled). Local files always win -- they're there on purpose.

        Never raises and never blocks a run: no history simply means the
        multiples have nothing to be compared against, which the report says
        plainly rather than quietly omitting.
        """
        history = PriceHistory()
        combined: dict[str, list[tuple[date, float]]] = {}

        single = Path(config.PRICE_HISTORY_CSV)
        if single.exists():
            for ticker, rows in self._read_history_file(single).items():
                combined.setdefault(ticker, []).extend(rows)

        folder = Path(config.PRICE_HISTORY_DIR)
        if folder.is_dir():
            for path in sorted(folder.glob("*.csv")):
                # A per-symbol export usually has no symbol column, so the
                # filename is the ticker: VST.csv, or vst_daily_history.csv.
                stem = path.stem.upper().split("_")[0].split("-")[0]
                for ticker, rows in self._read_history_file(path, stem).items():
                    combined.setdefault(ticker, []).extend(rows)

        if tickers and config.YAHOO_ENABLED:
            from_yahoo = []
            for ticker in (t.upper() for t in tickers):
                rows, splits = self._yahoo_history(ticker)
                # Split events are wanted even when the prices come from a
                # local file: site downloads are split-adjusted too.
                if splits:
                    history.splits[ticker] = splits
                if ticker in combined or not rows:
                    continue
                combined[ticker] = rows
                from_yahoo.append(ticker)
            if from_yahoo:
                history.notes.append(
                    f"Yahoo Finance supplied price history for: {', '.join(from_yahoo)}.")

        for ticker, rows in combined.items():
            # Sort by date and keep one close per day (last wins), so a file
            # appended to twice doesn't skew a median.
            deduped: dict[date, float] = {}
            for when, close in rows:
                deduped[when] = close
            history.by_ticker[ticker] = sorted(deduped.items())

        if history:
            spans = []
            for ticker, rows in history.by_ticker.items():
                spans.append(f"{ticker} {rows[0][0]:%Y-%m}..{rows[-1][0]:%Y-%m}")
            history.notes.append(
                f"Price history loaded for {len(history.by_ticker)} ticker(s): "
                + ", ".join(spans[:8])
                + (" ..." if len(spans) > 8 else "")
            )
        return history

    def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        """Prices for these tickers, best source first (see module docstring).

        Never raises. Tickers with no price simply aren't in the result, and
        the caller skips valuation for those.
        """
        wanted = [t.upper() for t in tickers]
        quotes: dict[str, Quote] = {}

        # 1. Fresh hand-entered prices are deliberate overrides.
        manual = self._read_csv()
        fresh = {t: q for t, q in manual.items()
                 if t in wanted and not q.is_stale_manual}
        quotes.update(fresh)
        if fresh:
            self.notes.append(
                f"{self.prices_csv.name} supplied {len(fresh)} price(s): "
                f"{', '.join(sorted(fresh))}."
            )

        # 2. Barchart, if a key is configured.
        missing = [t for t in wanted if t not in quotes]
        if missing:
            barchart = self._fetch_barchart(missing)
            quotes.update(barchart)
            if barchart:
                self.notes.append(
                    f"Barchart supplied {len(barchart)} of {len(missing)} prices.")

        # 3. Yahoo -- the free default.
        missing = [t for t in wanted if t not in quotes]
        if missing and config.YAHOO_ENABLED:
            yahoo = self._fetch_yahoo(missing)
            quotes.update(yahoo)
            if yahoo:
                self.notes.append(
                    f"Yahoo Finance supplied {len(yahoo)} of {len(missing)} prices.")

        # 4. Stale hand-entered prices, loudly, rather than no valuation at all.
        missing = [t for t in wanted if t not in quotes]
        stale = [t for t in missing if t in manual]
        for ticker in stale:
            quotes[ticker] = manual[ticker]
            as_of = manual[ticker].as_of or "no date"
            self.notes.append(
                f"WARNING: {ticker} is using a STALE hand-entered price from "
                f"{self.prices_csv.name} ({as_of}). No live source answered. "
                f"Update the row or delete it before trusting the valuation."
            )

        still_missing = [t for t in wanted if t not in quotes]
        if still_missing:
            self.notes.append(
                f"No price for: {', '.join(still_missing)}. Valuation skipped "
                f"for those; everything else still runs."
            )
        return quotes

    # -- Yahoo -------------------------------------------------------------

    @staticmethod
    def _yahoo_symbol(ticker: str) -> str:
        # Share classes: SEC/Barchart write BRK.B, Yahoo writes BRK-B.
        return ticker.upper().replace(".", "-").replace("/", "-")

    def _yahoo_chart(self, ticker: str, range_: str) -> dict | None:
        try:
            response = self.session.get(
                config.YAHOO_CHART_URL.format(symbol=self._yahoo_symbol(ticker)),
                params={"range": range_, "interval": "1d", "events": "split"},
                headers={"User-Agent": config.YAHOO_USER_AGENT},
                timeout=20,
            )
        except requests.RequestException as exc:
            self.notes.append(f"Yahoo request failed for {ticker} ({exc}).")
            return None
        if response.status_code != 200:
            return None
        try:
            result = (response.json().get("chart") or {}).get("result") or []
        except ValueError:
            return None
        return result[0] if result else None

    def _fetch_yahoo(self, tickers: list[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for ticker in tickers:
            chart = self._yahoo_chart(ticker, "5d")
            if not chart:
                continue
            meta = chart.get("meta") or {}
            price = meta.get("regularMarketPrice")
            if price in (None, "") or meta.get("currency") not in (None, "USD"):
                # A non-USD listing would be valued against USD financials.
                continue
            when = meta.get("regularMarketTime")
            as_of = (datetime.fromtimestamp(when).date().isoformat()
                     if when else date.today().isoformat())
            quotes[ticker] = Quote(
                ticker=ticker, price=float(price), as_of=as_of, source="yahoo",
                name=str(meta.get("longName") or meta.get("shortName") or ""),
            )
        return quotes

    def _yahoo_history(self, ticker: str
                       ) -> tuple[list[tuple[date, float]], list[tuple[date, float]]]:
        """Ten years of split-adjusted daily closes, plus the split events.

        Cached for a day in the cache folder: <TICKER>.csv and
        <TICKER>_splits.csv.
        """
        folder = Path(config.YAHOO_HISTORY_DIR)
        path = folder / f"{ticker.upper()}.csv"
        split_path = folder / f"{ticker.upper()}_splits.csv"
        if path.exists() and split_path.exists() and \
                date.fromtimestamp(path.stat().st_mtime) == date.today():
            rows = self._read_history_file(path, ticker).get(ticker.upper(), [])
            splits = []
            with split_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    splits.append((date.fromisoformat(row["date"]), float(row["ratio"])))
            return rows, splits

        chart = self._yahoo_chart(ticker, "10y")
        if not chart or (chart.get("meta") or {}).get("currency") not in (None, "USD"):
            return [], []
        stamps = chart.get("timestamp") or []
        closes = ((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        rows = [(datetime.fromtimestamp(ts).date(), float(close))
                for ts, close in zip(stamps, closes) if close is not None]
        splits = []
        for event in ((chart.get("events") or {}).get("splits") or {}).values():
            try:
                ratio = float(event["numerator"]) / float(event["denominator"])
                splits.append((datetime.fromtimestamp(event["date"]).date(), ratio))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        splits.sort()
        if rows:
            folder.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "close"])
                writer.writerows((d.isoformat(), c) for d, c in rows)
            with split_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "ratio"])
                writer.writerows((d.isoformat(), r) for d, r in splits)
        return rows, splits
