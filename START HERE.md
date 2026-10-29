# edgar_analyst — start here

## Every time you use it

1. Finder → **Documents** → **edgar_analyst**
2. Double-click **`2 - RUN (double-click me).command`**
3. Type tickers, press Enter:  `VST CEG NRG TLN`
4. It asks about the 10-K comparison — press Enter for yes
5. The `output` folder opens by itself

## What you get, and what to open first

**`TICKER_brief.html`** — open this first. Double-click, it opens in your
browser. Four sections, in the order you should think about them:

1. What the price assumes (reverse DCF)
2. What to be suspicious of (red flags)
3. What management changed in the 10-K
4. Data warnings — read these before quoting anything

**`TICKER_Company.xlsx`** — the model. Ten years of statements, ~45 ratios, a
TTM column with 2026 data, plus Red Flags and Reverse DCF tabs.

**`comps.xlsx`** — every ticker from that run side by side.

## Prices (automatic now)

The reverse DCF and the multiples need share prices, which SEC filings don't
contain. These now come from **Yahoo Finance automatically** — free, no key,
nothing to set up. Ten years of daily history comes with them (cached once a
day), so the **Multiples** tab can show each multiple against its own history.
Stock splits are handled: old share counts are restated so they match the
split-adjusted prices.

The order the program tries, per ticker:

1. **`prices.csv`, if the row is dated within 5 days** — a price you typed on
   purpose wins (say, to model at a specific close or a deal price). A
   Barchart watchlist CSV export saved as `prices.csv` also works untouched.
2. **Barchart OnDemand API**, only if `barchart_key.txt` holds a key. Optional.
3. **Yahoo Finance** — the default.
4. **An old or undated `prices.csv` row**, only if nothing else answered, and
   flagged as **STALE** in the run output and on the valuation sheet.

Your own files in **`price_history/`** still win over Yahoo history.

Without any price, everything else still runs; only valuation is skipped.

## Reading it well

- **The brief tells you where to look. It doesn't tell you what to think.**
  A triggered flag is a question for the filing. A company building a factory
  fails the reinvestment test and should.
- **Check the company name** at the top of the brief. Tickers get reassigned —
  PARA is no longer Paramount. The program warns when a company has a short
  filing history, which is the tell.
- **Read metrics across years, not down a column.** One year's ROIC says
  almost nothing; ROIC sliding from 22% to 14% over six years is a thesis.
- **The reverse DCF works well for steady cash generators and badly for
  companies mid-build.** It says so when the base is too small or too negative
  to be meaningful — believe it, and use EV/EBITDA instead.

## Things that are correct, not broken

- Banks and insurers show blank operating income, EBITDA and ROIC.
- Foreign companies (20-F filers, most ADRs) fail outright.
- Utilities and IPPs are exempted from the Altman distress flag — they're
  leveraged by design and would otherwise flag every single year.
- A fiscal year ending January 2026 is labelled FY2025 here, because it mostly
  covers 2025. That's what lets a January filer sit next to a December one.

## If something breaks

`403` or `Forbidden` → your name/email in `sec_user_agent.txt` got lost.

Anything else → double-click `1 - SETUP` again; it rebuilds cleanly.

Full technical detail is in `README.md`.
