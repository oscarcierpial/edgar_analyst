"""
The command line front end -- one run, all four modules.

    python -m edgar_analyst ULTA
    python -m edgar_analyst VST CEG NRG TLN --comps
    python -m edgar_analyst AAPL --no-filings      (skip the slow 10-K diff)

For each ticker you get:

    <TICKER>_<Company>.xlsx     the model: statements, ratios, TTM, red flags,
                                reverse DCF
    <TICKER>_brief.html         the read-first brief: what the price assumes,
                                what to be suspicious of, what changed in the
                                10-K

and for a multi-ticker run, comps.xlsx putting them side by side.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import (config, export, filings, htmlreport, metrics, multiples,
               redflags, valuation)
from .client import EDGARClient, SECError
from .normalize import NormalizedCompany, Normalizer
from .pipeline import load_company
from .prices import PriceClient


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")


def build_one(
    ticker: str,
    client: EDGARClient,
    normalizer: Normalizer,
    output_dir: Path,
    quote=None,
    price_history=None,
    run_filings: bool = True,
    write_csv: bool = False,
    quiet: bool = False,
) -> tuple[NormalizedCompany, dict]:
    """Run every module for one company and write its outputs."""
    if not quiet:
        print(f"  {ticker:<6} working...", end="", flush=True)

    # Module 2 -- the foundation everything else reads.
    company, _notes = load_company(ticker, client, normalizer)
    metric_frame = metrics.compute(company)
    stats = metrics.summary(company, metric_frame)

    # Module 4 -- needs a price, so it may legitimately be skipped.
    valuation_result = None
    market_cap = None
    if quote is not None:
        valuation_result = valuation.evaluate(
            company, metric_frame, price=quote.price,
            price_source=quote.source + (" - STALE, update prices.csv" if quote.is_stale_manual else ""),
            price_as_of=quote.as_of,
        )
        if valuation_result is not None:
            market_cap = valuation_result.market_cap

    # Multiples -- current, and against this company's own history.
    multiples_report = multiples.evaluate(
        company, metric_frame,
        price=quote.price if quote is not None else None,
        history=price_history,
    )

    # Module 3 -- market cap sharpens Altman, but it runs without one.
    redflag_report = redflags.evaluate(company, metric_frame, market_cap)

    # Module 1 -- the slow one: two filings to download the first time.
    comparison = None
    if run_filings:
        try:
            comparison = filings.compare_filings(
                client, company.cik, company.ticker, company.entity_name)
        except (SECError, ValueError, OSError) as exc:
            company.warnings.append(
                f"Filing comparison unavailable: {exc}. Everything else ran.")

    workbook = output_dir / f"{ticker.upper()}_{_slug(company.entity_name)[:40]}.xlsx"
    export.write_company_workbook(
        company, metric_frame, stats, workbook,
        redflag_report=redflag_report, valuation_result=valuation_result,
        multiples_report=multiples_report)

    if write_csv:
        export.write_csvs(company, metric_frame, output_dir / "csv",
                          redflag_report=redflag_report,
                          multiples_report=multiples_report)

    brief = output_dir / f"{ticker.upper()}_brief.html"
    htmlreport.write_report(company, valuation_result, redflag_report,
                            comparison, brief, workbook_name=workbook.name,
                            multiples_report=multiples_report,
                            metric_frame=metric_frame)

    if not quiet:
        pieces = [f"flags {redflag_report.score}"]
        if valuation_result and valuation_result.implied_growth is not None:
            pieces.append(f"implied {valuation_result.implied_growth:.0%}")
        if comparison:
            pieces.append(f"10-K {comparison.overall_change:.0%} changed")
        if company.ttm:
            pieces.append(company.ttm.label)
        print(f"\r  {ticker:<6} {company.entity_name[:34]:<36} "
              f"{' · '.join(pieces)}")
        for warning in company.warnings:
            print(f"         ! {warning}")

    row = {
        "ticker": ticker.upper(),
        "company": company.entity_name,
        "as_of": (company.ttm.as_of.strftime("%b %d, %Y") if company.ttm is not None
                  else company.period_ends[company.fiscal_years[-1]].strftime("%b %d, %Y")),
        **stats,
        "redflag_score": redflag_report.score,
        "redflag_verdict": redflag_report.verdict,
        "implied_growth": valuation_result.implied_growth if valuation_result else None,
        "historical_fcf_cagr": (valuation_result.historical_fcf_cagr
                                if valuation_result else None),
        "ev_to_ebitda": valuation_result.ev_to_ebitda if valuation_result else None,
        "filing_change": comparison.overall_change if comparison else None,
        "ev_ebitda_median": (multiples_report.stats["ev_ebitda"].median
                             if "ev_ebitda" in multiples_report.stats else None),
        "ev_ebitda_pctile": (multiples_report.stats["ev_ebitda"].percentile
                             if "ev_ebitda" in multiples_report.stats else None),
    }
    return company, row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edgar_analyst",
        description="Pull SEC filings and produce a model, a screen and a brief.",
    )
    parser.add_argument("tickers", nargs="+", help="One or more tickers")
    parser.add_argument("--years", type=int, default=config.DEFAULT_YEARS)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_DIR)
    parser.add_argument("--comps", action="store_true",
                        help="Also write comps.xlsx comparing the tickers")
    parser.add_argument("--no-filings", action="store_true",
                        help="Skip the 10-K comparison (much faster first run)")
    parser.add_argument("--csv", action="store_true",
                        help="Also write plain CSVs (no spreadsheet app needed)")
    parser.add_argument("--no-prices", action="store_true",
                        help="Skip valuation even if a price source is available")
    parser.add_argument("--wacc", type=float, default=config.DEFAULT_WACC)
    parser.add_argument("--terminal-growth", type=float,
                        default=config.DEFAULT_TERMINAL_GROWTH)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).expanduser()
    client = EDGARClient(use_cache=not args.no_cache)
    normalizer = Normalizer(years=args.years)

    config.DEFAULT_WACC = args.wacc
    config.DEFAULT_TERMINAL_GROWTH = args.terminal_growth

    tickers = [t.upper() for t in args.tickers]
    if not args.quiet:
        print(f"\nBuilding {len(tickers)} company report(s) -> {output_dir}\n")

    quotes = {}
    price_history = None
    if not args.no_prices:
        price_client = PriceClient()
        quotes = price_client.get_quotes(tickers)
        price_history = price_client.load_price_history(tickers) or None
        if price_history is not None:
            price_client.notes.extend(price_history.notes)
        if not args.quiet:
            for note in price_client.notes:
                print(f"  {note}")
            if price_client.notes:
                print()

    started = time.time()
    comps_rows: list[dict] = []
    failures: list[tuple[str, str]] = []

    for ticker in tickers:
        try:
            _company, row = build_one(
                ticker, client, normalizer, output_dir,
                quote=quotes.get(ticker),
                price_history=price_history,
                run_filings=not args.no_filings,
                write_csv=args.csv,
                quiet=args.quiet,
            )
            comps_rows.append(row)
        except (SECError, ValueError) as exc:
            failures.append((ticker, str(exc)))
            if not args.quiet:
                print(f"\r  {ticker:<6} FAILED: {exc}")

    if args.comps and len(comps_rows) > 1:
        comps_path = output_dir / "comps.xlsx"
        export.write_comps_workbook(comps_rows, comps_path)
        if not args.quiet:
            print(f"\n  Peer comparison -> {comps_path.name}")

    if not args.quiet:
        print(f"\nDone: {len(comps_rows)} succeeded, {len(failures)} failed, "
              f"{time.time() - started:.1f}s.\n")
        if failures:
            print("Failures:")
            for ticker, message in failures:
                print(f"  {ticker}: {message}")
            print()

    return 0 if comps_rows else 1


if __name__ == "__main__":
    sys.exit(main())
