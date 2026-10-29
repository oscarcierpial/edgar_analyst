"""
Ties the pieces together: ticker in, NormalizedCompany out.

This exists as its own module because getting from a ticker to a usable set of
financials is not always one hop, and both the command line and the library
entry point need the same logic.

THE REORGANIZATION PROBLEM
--------------------------
The SEC's ticker file maps each ticker to exactly one CIK -- the company that
currently owns the ticker. That's usually what you want. But when a company
reorganizes into a new holding structure, the SEC issues the successor a BRAND
NEW CIK, moves the ticker to it, and leaves the entire filing history behind on
the old CIK.

Exxon is the live example: XOM now points to "ExxonMobil Holdings Corp", which
has filed one 10-Q and no 10-K at all, while a decade of annual reports sits
under "EXXON MOBIL CORP" on the old CIK. Naively trusting the ticker file gives
you nothing for one of the largest companies in the index -- and, worse, would
give you nothing without telling you why.

So when the resolved CIK has no annual data, we search EDGAR for companies with
a similar name that DO file 10-Ks, verify the candidate, and use it -- telling
you in plain terms that we switched.
"""

from __future__ import annotations

import difflib
import re

from .client import EDGARClient, SECError
from .interim import compute_ttm
from .normalize import NormalizedCompany, Normalizer

MAX_PREDECESSOR_CANDIDATES = 8
MIN_NAME_SIMILARITY = 0.55


def _name_similarity(a: str, b: str) -> float:
    """0-1 score for how alike two company names are, ignoring legal noise."""

    def clean(text: str) -> str:
        text = text.lower()
        for token in (",", ".", "/", "&", "'", "-"):
            text = text.replace(token, " ")
        drop = {"inc", "corp", "corporation", "co", "company", "holdings",
                "holding", "group", "plc", "ltd", "the", "new", "class"}
        return " ".join(w for w in text.split() if w not in drop)

    return difflib.SequenceMatcher(None, clean(a), clean(b)).ratio()


def _find_historical_filer(
    client: EDGARClient, cik: int, entity_name: str
) -> tuple[int, str] | None:
    """Look for a predecessor CIK holding this company's 10-K history."""
    candidates = client.search_10k_filers_by_name(entity_name)

    scored: list[tuple[float, int, str]] = []
    for candidate in candidates[:MAX_PREDECESSOR_CANDIDATES]:
        if candidate == cik:
            continue
        try:
            submissions = client.get_submissions(candidate)
        except SECError:
            continue
        name = submissions.get("name", "")
        forms = set(submissions.get("filings", {}).get("recent", {}).get("form", []))
        if not any(form.startswith("10-K") for form in forms):
            continue
        score = _name_similarity(entity_name, name)
        if score >= MIN_NAME_SIMILARITY:
            scored.append((score, candidate, name))

    if not scored:
        return None
    scored.sort(reverse=True)
    _score, best_cik, best_name = scored[0]
    return best_cik, best_name


def _apply_filer_identity(client: EDGARClient, company: NormalizedCompany,
                          cik: int) -> None:
    """Use the SEC's filer record for the company name, not the XBRL one.

    These disagree more often than you'd expect, and the XBRL one is worse.
    VST's companyfacts document calls the entity "Vistra Operations Company
    LLC" -- the debt-issuing subsidiary named as a co-registrant on a combined
    10-K -- while the filer record says "Vistra Corp.", which is the listed
    company you actually own. Same CIK, same filing, but only one of those is
    a name you want on a model you hand to someone.

    Failures here are cosmetic, so they never interrupt a build.
    """
    try:
        submissions = client.get_submissions(cik)
    except SECError:
        return
    name = (submissions.get("name") or "").strip()
    if name:
        company.entity_name = name
    company.industry = (submissions.get("sicDescription") or "").strip()

    former = [entry.get("name", "") for entry in submissions.get("formerNames") or []]

    # TICKER REASSIGNMENT -- the quietest way to analyse the wrong company.
    #
    # When a company is taken private or acquired, its ticker goes back in the
    # pool and can be handed to someone else. Nothing errors: you type PARA
    # expecting Paramount and get a full, internally consistent report on
    # Banzai International, a small software company that picked the symbol up
    # afterwards. Every number is correct and the entire analysis is worthless.
    #
    # There's no field that says "this ticker was reassigned", but a short
    # filing history under the current CIK is the tell, so say so and make the
    # resolved name impossible to miss.
    if len(company.fiscal_years) <= 6:
        company.warnings.insert(0, (
            f"Only {len(company.fiscal_years)} years of 10-K history exist under "
            f"this CIK, and the filer is \"{company.entity_name}\". That means a "
            f"recent listing, a reorganization -- or that this TICKER HAS BEEN "
            f"REASSIGNED to a different company than the one you had in mind. "
            f"Confirm the company name above is who you meant before using any "
            f"of this."
        ))

    # SPAC HERITAGE -- the early "history" belongs to a shell, not a business.
    #
    # A company that merged into a blank-check vehicle inherits that shell's
    # filings. Those years show a few million in trust assets and no operations,
    # so growth rates, margins and every trend spanning the merger are measuring
    # the transition from shell to company, not business performance.
    spac_pattern = re.compile(
        r"(acquisition corp|acquisition co\b|blank check|capital corp|"
        r"& co\.? holdings|holdings corp\b)", re.I)
    shells = [n for n in former if spac_pattern.search(n)]
    if shells:
        company.warnings.insert(0, (
            f"Previously named \"{shells[0]}\" -- this company almost certainly "
            f"came public through a SPAC or blank-check vehicle. The earliest "
            f"years here are the SHELL'S financials, not the operating "
            f"business's, so growth rates and margin trends spanning the merger "
            f"are meaningless. Use only the years after the combination."
        ))


def load_company(
    ticker: str,
    client: EDGARClient | None = None,
    normalizer: Normalizer | None = None,
) -> tuple[NormalizedCompany, list[str]]:
    """Fetch and normalize one company.

    Returns the company plus a list of notes about anything unusual we had to
    do to get the data (currently: falling back to a predecessor CIK).
    """
    client = client or EDGARClient()
    normalizer = normalizer or Normalizer()
    notes: list[str] = []

    cik = client.lookup_cik(ticker)
    raw = client.get_company_facts(cik)

    try:
        company = normalizer.normalize(raw, ticker)
        company.attach_ttm(compute_ttm(raw, company))
        _apply_filer_identity(client, company, cik)
        return company, notes
    except ValueError as original_error:
        # No annual data under the ticker's current CIK. Before giving up, see
        # whether the history lives under a predecessor entity.
        entity_name = raw.get("entityName", ticker)
        found = _find_historical_filer(client, cik, entity_name)
        if found is None:
            raise original_error

        historical_cik, historical_name = found
        historical_raw = client.get_company_facts(historical_cik)
        company = normalizer.normalize(historical_raw, ticker)
        # Annual history comes from the predecessor, but the newest quarter
        # is usually filed by the successor -- pool both so TTM is current.
        company.attach_ttm(compute_ttm([historical_raw, raw], company))
        _apply_filer_identity(client, company, historical_cik)
        notes.append(
            f"{ticker.upper()} resolves to CIK {cik} ({entity_name}), which has no "
            f"10-K history -- this happens after a holding-company "
            f"reorganization. Used predecessor CIK {historical_cik} "
            f"({historical_name}) instead. Verify this is the right entity "
            f"before relying on the numbers."
        )
        company.warnings.insert(0, notes[-1])
        return company, notes
