"""
Talks to the SEC. Nothing in this file understands accounting -- its only job
is to get raw JSON from EDGAR and cache it on disk.

Keeping the "get the data" code separate from the "understand the data" code is
the single most useful habit in a project like this. When the SEC changes an
endpoint, you fix one file.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from . import config


class SECError(Exception):
    """Raised when we can't get what we need from the SEC."""


class EDGARClient:
    """A small wrapper around the SEC's public data APIs.

    Usage:
        client = EDGARClient()
        cik = client.lookup_cik("AAPL")
        facts = client.get_company_facts(cik)
    """

    def __init__(self, cache_dir: Path | None = None, use_cache: bool = True):
        self.cache_dir = Path(cache_dir or config.CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_cache = use_cache

        # A requests.Session reuses the underlying network connection across
        # calls, which is meaningfully faster when you pull 50 companies.
        if not config.USER_AGENT:
            raise SECError(
                "No name/email set for SEC requests. The SEC blocks anonymous "
                "traffic. Create sec_user_agent.txt in the project folder "
                "containing one line like:  Jane Doe jane@example.com  "
                "(or set the SEC_USER_AGENT environment variable)."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._last_request_time = 0.0
        self._ticker_map: dict[str, int] | None = None

    # -- low level ----------------------------------------------------------

    def _throttle(self) -> None:
        """Wait if we're about to make requests faster than the SEC allows."""
        elapsed = time.time() - self._last_request_time
        if elapsed < config.REQUEST_DELAY_SECONDS:
            time.sleep(config.REQUEST_DELAY_SECONDS - elapsed)
        self._last_request_time = time.time()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Any | None:
        """Return cached JSON if it exists and isn't stale, else None."""
        if not self.use_cache:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > config.CACHE_MAX_AGE_HOURS:
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            # A corrupt cache file should never break a run -- just refetch.
            return None

    def _write_cache(self, key: str, payload: Any) -> None:
        try:
            with self._cache_path(key).open("w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError:
            pass  # caching is a nicety, not a requirement

    def _get_json(self, url: str, cache_key: str) -> Any:
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        self._throttle()
        try:
            response = self.session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise SECError(f"Network problem reaching {url}: {exc}") from exc

        if response.status_code == 403:
            raise SECError(
                "The SEC returned 403 Forbidden. This almost always means your "
                "name/email for the SEC is missing or fake. Put your real name "
                "and email in sec_user_agent.txt (or the SEC_USER_AGENT "
                "environment variable)."
            )
        if response.status_code == 404:
            raise SECError(f"Not found at {url} (the SEC has no data at that address).")
        if response.status_code != 200:
            raise SECError(f"SEC returned HTTP {response.status_code} for {url}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SECError(f"SEC returned something that isn't JSON from {url}") from exc

        self._write_cache(cache_key, payload)
        return payload

    # -- public -------------------------------------------------------------

    def lookup_cik(self, ticker: str) -> int:
        """Turn a ticker like 'AAPL' into the SEC's internal company ID (CIK).

        Every company in EDGAR is identified by a CIK number, not a ticker.
        The SEC publishes one file mapping all tickers to CIKs; we download it
        once and reuse it.
        """
        if self._ticker_map is None:
            raw = self._get_json(config.SEC_TICKER_URL, "company_tickers")
            # The file is shaped {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
            self._ticker_map = {
                entry["ticker"].upper(): int(entry["cik_str"])
                for entry in raw.values()
            }

        key = ticker.strip().upper()
        if key not in self._ticker_map:
            raise SECError(
                f"Ticker '{ticker}' isn't in the SEC's ticker list. Check the "
                f"spelling. Note that foreign filers, funds, and companies that "
                f"file 20-F instead of 10-K may not appear."
            )
        return self._ticker_map[key]

    def get_company_facts(self, cik: int) -> dict:
        """Download every XBRL number a company has ever tagged in a filing.

        This one endpoint is the entire foundation of the program. It returns
        a structure shaped like:

            facts -> "us-gaap" -> "Revenues" -> units -> "USD" -> [ {fact}, ... ]

        where each {fact} has a value, a period (start/end dates), the form it
        came from (10-K, 10-Q), and when it was filed.
        """
        url = config.SEC_COMPANYFACTS_URL.format(cik=cik)
        return self._get_json(url, f"companyfacts_{cik:010d}")

    def search_10k_filers_by_name(self, name: str) -> list[int]:
        """Find CIKs of companies matching a name that actually file 10-Ks.

        Used to recover from corporate reorganizations -- see
        pipeline.load_company for why that's necessary.
        """
        # The search chokes on punctuation and legal suffixes, so strip them.
        cleaned = name.lower()
        for token in (",", ".", "/", "&", "'"):
            cleaned = cleaned.replace(token, " ")
        drop = {"inc", "corp", "corporation", "co", "company", "holdings",
                "holding", "group", "plc", "ltd", "the", "new", "class"}
        words = [w for w in cleaned.split() if w and w not in drop]
        if not words:
            return []

        # Try the most specific query first, then progressively broaden.
        queries = []
        if len(words) >= 2:
            queries.append(" ".join(words[:2]))
        queries.append(words[0])
        # EDGAR's search is a prefix match on the filer name, so a run-together
        # brand fails against a spaced legal name: Exxon's new holding company
        # calls itself "ExxonMobil Holdings Corp", the predecessor is "EXXON
        # MOBIL CORP", and "exxonmobil" matches nothing. A shorter prefix
        # finds it; the name-similarity check in pipeline.py weeds out the
        # unrelated companies a short prefix also brings back.
        if len(words[0]) >= 8:
            queries.append(words[0][:max(5, len(words[0]) // 2)])

        seen: list[int] = []
        for query in queries:
            url = config.SEC_COMPANY_SEARCH_URL.format(name=query.replace(" ", "+"))
            self._throttle()
            try:
                response = self.session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException:
                continue
            if response.status_code != 200:
                continue
            # The Atom feed carries CIKs in <cik> tags for both the single-match
            # and multi-match shapes; names aren't reliably included, so we
            # check candidates against the submissions API afterward.
            for match in re.findall(r"<cik>(\d+)</cik>", response.text):
                value = int(match)
                if value not in seen:
                    seen.append(value)
            if seen:
                break
        return seen

    # -- raw documents (module 1) ------------------------------------------

    def get_raw(self, url: str) -> str:
        """Fetch a filing document as text. Not cached here -- see the text cache.

        Filing documents run to 10-20MB, so callers cache the EXTRACTED TEXT
        rather than the HTML.
        """
        self._throttle()
        try:
            response = self.session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise SECError(f"Couldn't download {url}: {exc}") from exc
        if response.status_code != 200:
            raise SECError(f"SEC returned HTTP {response.status_code} for {url}")
        return response.text

    def read_text_cache(self, key: str) -> str | None:
        path = self.cache_dir / f"{key}.txt"
        if not self.use_cache or not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def write_text_cache(self, key: str, text: str) -> None:
        try:
            (self.cache_dir / f"{key}.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass

    def get_submissions(self, cik: int) -> dict:
        """Filing history: form types, dates, and document URLs.

        Not used by the normalizer, but this is what the filing-diff module
        (module 1) will need to find this year's and last year's 10-K.
        """
        url = config.SEC_SUBMISSIONS_URL.format(cik=cik)
        return self._get_json(url, f"submissions_{cik:010d}")
