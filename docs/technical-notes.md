# edgar_analyst — technical notes (statement normalizer & TTM)

Pulls any US public company's financials straight from the SEC and produces a
standardized 10-year model in Excel — statements spread, ratios computed, and
an audit trail showing where every number came from.

About 1 second per company. Spreading a decade of financials by hand takes
most of a day, and you'd still have to do the ratios.

Quick start and the overall tool are described in the main [README](../README.md).
This page goes deeper on module 2 (the statement normalizer) and the TTM column.

---

## What you get

Each company produces one workbook with six sheets:

- **Summary** — identity, fiscal calendar, headline stats, and **data warnings**
- **Income Statement / Balance Sheet / Cash Flow** — 10 years, standardized
- **Metrics** — ~45 ratios grouped into Growth, Margins, Returns, Cash Flow,
  Working Capital, Balance Sheet, and Quality Flags
- **Data Coverage** — which XBRL tag fed each line, which years are missing,
  which were restated, and which came from something other than a 10-K

**Read the Data Coverage sheet before you trust a number.** It is the whole
difference between a model you can defend in an interview and one you can't.

The comps workbook puts one row per company with the same definitions applied
to all of them, which is what makes a peer screen meaningful.

---

## The four hard problems, and how this handles them

These are the reasons "just download the XBRL" doesn't work, and most of the
code exists to deal with them.

**1. Companies use different tags for the same thing.** Apple tags revenue as
`RevenueFromContractWithCustomerExcludingAssessedTax`. Walmart uses `Revenues`.
Older filings use `SalesRevenueNet`. `taxonomy.py` keeps an ordered candidate
list per line item, takes the best-covered tag, then fills remaining years from
the others — because companies *switch* tags mid-history, and taking only the
modern tag silently drops the early years, which then breaks every CAGR.

Tag order encodes real judgment. `Revenues` comes before the contract-revenue
tag because Walmart's contract revenue is $642.6bn while its actual total
revenue is $648.1bn — the difference is membership income. Same issue at
Costco, where membership fees are the entire business model.

**2. The same year appears in multiple filings with different values.** This
year's 10-K shows two prior years as comparatives, sometimes restated. We keep
the most recently filed version so the history is internally consistent, and
flag on the coverage sheet which years were restated — a quiet restatement is
itself a research signal.

**3. Fiscal years don't line up.** Apple's ends in September, Walmart's in
January, most in December. Each year is labeled by the calendar year it mostly
covers, so a January year-end retailer sits next to a December filer correctly.
This means a year the company calls "fiscal 2025" may appear here as FY2024 —
deliberately. The real period end date is printed under every column.

**4. Data isn't always where it should be.** Handled automatically:

- **Reorganizations.** XOM now points to a new CIK with no 10-K history, while a
  decade of filings sits on the old one. The program detects this, finds the
  predecessor, and tells you it switched.
- **Recast filings.** Disney's FY2017 balance sheet appears in *no 10-K* — only
  in an 8-K that recast prior years after the Fox acquisition. 10-K first, then
  10-Q comparatives, then recast 8-Ks; the coverage sheet says which was used.
- **Stock splits.** EDGAR never restates already-filed reports for a later
  split, so a 10-year share count can contain a fake 4x step. Detected from
  share counts (never from EPS, which legitimately moves that much) and flagged
  loudly on the Summary sheet.
- **Missing operating income.** Exxon, Nike, J&J and IBM don't report an
  operating income line at all. It's derived as pre-tax income plus interest —
  except for banks, where interest *is* an operating cost and the answer would
  be meaningless, so those are left blank instead of fabricated.

---

## Staying current: the TTM column

A 10-K is annual. By mid-year an annual-only model is six to nine months stale
while the company has filed two or three 10-Qs nobody is reading.

So every workbook carries a **TTM column** on the right of the fiscal years,
shaded a different colour, built as:

    TTM = last full fiscal year + year-to-date now - year-to-date a year ago

Apple's year ended September 2025 at $416.2bn. Through three quarters of fiscal
2026 it had done $364.4bn against $313.7bn a year earlier, so trailing twelve
months to June 2026 is $466.8bn — twelve months of business ending nine months
after the last annual report.

Why not just add up four quarters: the fourth quarter is never filed as a 10-Q.
It exists only inside the next 10-K, as the annual figure minus nine months.
Building from year-to-date figures uses two numbers that always exist instead
of four that don't.

What to keep in mind:

- **It's unaudited.** Quarterly figures get less scrutiny and are restated more
  often. The column is shaded to keep that visible.
- **Balance sheet items are the latest snapshot**, not a sum — and more current
  than the fiscal year end.
- **Growth rates are deliberately blank in the TTM column.** A trailing twelve
  months overlaps the fiscal year it would be compared against; for a company
  two quarters into its year, half the "growth" is the period measured against
  itself.
- **Some line items won't have one.** Companies tag less in 10-Qs than in
  10-Ks. Stock comp is a common casualty. The fiscal year columns are
  unaffected, and the Summary tab lists what's missing.
- **No TTM at all** means the 10-K is still the newest filing — normal for a
  month or two after the annual report, not a failure.

For companies that reorganized, quarterly and annual data can live under
different CIKs (Exxon's annual history is on the old entity, its June 2026
quarter on the new one). Both are pooled, or the column would be a quarter
stale.

---

## Verifying it

```bash
python selftest.py
```

Checks output against figures read off actual filings, and enforces structural
rules — fiscal years consecutive, no negative revenue, and the balance sheet
actually balancing. Across a 50-company sample the identity holds for 498 of
499 company-years; the one exception is detected and flagged as a warning in
that company's own workbook rather than passed off as clean.

Run this after any change to `taxonomy.py` or `normalize.py`. Add your own
cases as you verify companies by hand — that's how the taxonomy improves.

---

## Known limitations

- **Banks and insurers.** The line items don't fit them. Revenue, assets and
  equity come through fine, but operating income, EBITDA, working capital and
  ROIC are meaningless or blank. They need their own template — net interest
  income, provisions, tier 1 capital, book value per share.
- **Foreign private issuers.** Companies filing 20-F instead of 10-K (BABA,
  most ADRs) aren't supported. The SEC's XBRL data for them is inconsistent.
- **Per-share figures across splits.** Flagged on the statements, not
  rewritten. (The multiples sheet does restate share counts, using split
  history from Yahoo, so historical market caps are on one basis.)
- **Segment data.** Not included. The `companyfacts` endpoint returns only
  consolidated figures, so segment-level analysis needs the filings themselves.
- **Market data is from Yahoo Finance's unofficial chart endpoint.** Free and
  keyless, but unsupported; `prices.csv` is the manual override if it ever
  stops answering.

---

## The code

```
edgar_analyst/
  config.py      Settings. Cache behavior, fiscal year convention, DCF assumptions.
  client.py      Talks to the SEC. Caching, rate limiting, error handling.
  taxonomy.py    Tag -> standard line item mapping. You'll edit this most.
  normalize.py   Raw facts -> clean annual statements. The hard part.
  metrics.py     Every ratio, defined once.
  export.py      Excel output and formatting.
  pipeline.py    Ticker -> company, including the reorganization fallback.
  cli.py         Command line interface.
selftest.py      Verification against real filings.
```

**When a line item comes back empty for some company:** open its filing, find
the tag it used, add it to the right candidate list in `taxonomy.py`, rerun
`selftest.py` to confirm you didn't break anything else. The program gets
better every time you do this. That loop is most of the ongoing work.

---

## One caution

This gets the mechanical work out of the way. It doesn't do the analysis, and
the numbers are only as good as what companies tagged — which is why the
coverage sheet exists and why you should spot-check a few line items against
the actual 10-K the first time you look at any company. Fast wrong numbers are
worse than slow right ones.
