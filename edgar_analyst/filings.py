"""
MODULE 1 -- what changed in this year's 10-K.

THE IDEA
--------
Companies write their 10-K by editing last year's. Most of it is copied
forward verbatim, which means the parts they DID change are, by construction,
the parts management thought needed changing. A new risk factor, a softened
sentence in MD&A, a quietly reworded revenue recognition policy -- each of
those is a deliberate act by people with lawyers.

Almost nobody reads year-over-year. It's three to six hours per company by
hand, and the changes are buried in two hundred pages of identical text. That
is exactly the kind of work worth handing to a program: the tedium is the
barrier to entry, and removing it gives you something most readers don't have.

There is academic support for this being more than tidiness -- Cohen, Malloy
and Nguyen's "Lazy Prices" found that companies whose filings changed
significantly went on to underperform, and argued the market under-reacts
because so few people compare the documents. Treat that as motivation to look,
not as a trading rule.

WHAT THIS DOES
--------------
Fetches the two most recent 10-Ks, splits each into its numbered Items, and
compares them section by section. Output is an HTML report with additions and
deletions highlighted, ordered so the most-changed section comes first.

WHAT IT DOESN'T DO
------------------
It doesn't tell you whether a change matters. A company reorganising its
segments will rewrite half of MD&A for reasons that are pure presentation.
The program can tell you WHERE to read, at a hundred times the speed you could
find it yourself. Deciding what it means is the job.

PARSING CAVEAT
--------------
10-K HTML has no standard structure -- item headings are whatever the filing
agent's template produced. The splitter here handles the common shapes and
reports what it found, but treat a section boundary as approximate, and if a
section looks wrong, read it in the filing before drawing a conclusion.
"""

from __future__ import annotations

import difflib
import re
from html import unescape
from dataclasses import dataclass, field
from datetime import date

from .client import EDGARClient, SECError

# The Items worth comparing, in document order. Item 8 (the financial
# statements) is excluded on purpose: the numbers change every year by
# definition, so diffing them is all noise, and modules 2 and 3 already read
# them properly.
ITEMS: tuple[tuple[str, str], ...] = (
    ("1", "Business"),
    ("1A", "Risk Factors"),
    ("1B", "Unresolved Staff Comments"),
    ("1C", "Cybersecurity"),
    ("2", "Properties"),
    ("3", "Legal Proceedings"),
    ("5", "Market for Common Equity"),
    ("7", "Management's Discussion and Analysis"),
    ("7A", "Market Risk"),
    ("9A", "Controls and Procedures"),
)

# Every numbered item a 10-K can contain, in the order they appear. Used only
# to find section BOUNDARIES -- what gets reported is the ITEMS list above.
ALL_ITEM_NUMBERS: tuple[str, ...] = (
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8",
    "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
)

# How close another item marker has to be for a match to look like a table of
# contents line rather than a real section heading.
TOC_PROXIMITY_CHARS = 400
# A real section has at least this much text in it.
MIN_SECTION_CHARS = 400
# Cap the rendered diff so one rewritten MD&A can't produce a 40MB page.
MAX_CHANGED_BLOCKS_PER_SECTION = 120


@dataclass
class Filing:
    accession: str
    filing_date: str
    report_date: str
    document: str
    form: str = "10-K"

    @property
    def accession_plain(self) -> str:
        return self.accession.replace("-", "")


@dataclass
class SectionDiff:
    item: str
    title: str
    similarity: float           # 1.0 = identical
    words_added: int
    words_removed: int
    blocks: list[tuple[str, str]] = field(default_factory=list)  # (added|removed, text)
    old_chars: int = 0
    new_chars: int = 0
    truncated: bool = False

    @property
    def percent_changed(self) -> float:
        return 1.0 - self.similarity

    @property
    def is_new(self) -> bool:
        return self.old_chars == 0 and self.new_chars > 0

    @property
    def is_removed(self) -> bool:
        return self.new_chars == 0 and self.old_chars > 0


@dataclass
class FilingComparison:
    ticker: str
    company_name: str
    current: Filing
    previous: Filing
    sections: list[SectionDiff] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def overall_change(self) -> float:
        """Share of text changed, weighted by section length."""
        total = sum(s.new_chars for s in self.sections)
        if not total:
            return 0.0
        return sum(s.percent_changed * s.new_chars for s in self.sections) / total

    @property
    def most_changed(self) -> list[SectionDiff]:
        return sorted(self.sections, key=lambda s: -s.percent_changed)


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def annual_filings(client: EDGARClient, cik: int, limit: int = 6) -> list[Filing]:
    """The company's most recent 10-K filings, newest first, one per year.

    AMENDMENTS ARE EXCLUDED, and that matters more than it sounds. A 10-K/A is
    usually a short document covering one part -- AES filed one of 20,000
    characters against a real 10-K of 992,000. Compare this year's full report
    against last year's amendment and every section comes back "100% changed",
    which looks like a dramatic rewrite and is pure artefact.

    We also keep only one filing per fiscal year, preferring the original, so
    an amended year can't appear twice.
    """
    submissions = client.get_submissions(cik)
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])

    by_period: dict[str, Filing] = {}
    order: list[str] = []
    for index, form in enumerate(forms):
        upper = form.upper()
        # Exact match only: "10-K" and the older "10-K405", never "10-K/A".
        if not (upper == "10-K" or upper.startswith("10-K405")):
            continue
        document = recent.get("primaryDocument", [""] * len(forms))[index]
        if not document:
            continue
        report_date = recent.get("reportDate", [""] * len(forms))[index]
        filing = Filing(
            accession=recent.get("accessionNumber", [""] * len(forms))[index],
            filing_date=recent.get("filingDate", [""] * len(forms))[index],
            report_date=report_date,
            document=document,
            form=form,
        )
        key = report_date or filing.accession
        if key not in by_period:
            by_period[key] = filing
            order.append(key)
        if len(order) >= limit:
            break
    return [by_period[key] for key in order]


def html_to_text(html: str) -> str:
    """Flatten filing HTML to plain text.

    Uses lxml when available because these documents run to 13MB and the pure
    Python parsers are painfully slow at that size; falls back to regex so a
    missing library degrades the output rather than breaking the feature.
    """
    try:
        import lxml.html

        tree = lxml.html.fromstring(html)
        for bad in tree.xpath("//script | //style"):
            bad.getparent().remove(bad)
        text = tree.text_content()
    except Exception:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#8217;", "'"))

    # Decode HTML entities, TWICE. Filing agents routinely double-escape, so
    # the raw document contains "&amp;#160;" which one pass turns into the
    # literal text "&#160;" rather than a space. Left undecoded, a heading
    # reads "Item&#160;1." and no amount of \s* in a regex will match it --
    # which silently reduces the whole module to finding one section out of
    # nine. Loop until it stops changing, capped so a pathological document
    # can't spin.
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded

    # Zero-width characters are used for layout inside filing tables and sit
    # between the word "Item" and its number.
    for invisible in ("​", "‌", "‍", "﻿", "­"):
        text = text.replace(invisible, "")

    text = text.replace("\xa0", " ").replace("’", "'").replace("“", '"')
    text = text.replace("”", '"').replace("—", "--").replace("–", "-")
    # Collapse runs of whitespace but keep paragraph breaks, which are the unit
    # the diff works in.
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def fetch_filing_text(client: EDGARClient, cik: int, filing: Filing) -> str:
    """Download one filing and return its plain text, cached after extraction.

    The cache holds the EXTRACTED TEXT, not the HTML -- about a tenth the size,
    and re-running a comparison then costs nothing.
    """
    cache_key = f"filingtext_{cik:010d}_{filing.accession_plain}"
    cached = client.read_text_cache(cache_key)
    if cached is not None:
        return cached

    url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
           f"{filing.accession_plain}/{filing.document}")
    html = client.get_raw(url)
    text = html_to_text(html)
    client.write_text_cache(cache_key, text)
    return text


# ---------------------------------------------------------------------------
# splitting into Items
# ---------------------------------------------------------------------------

def _item_matches(text: str, number: str) -> list[int]:
    """Every plausible position of an 'Item <number>' heading."""
    # Require a word boundary after the number so "Item 1" doesn't match
    # "Item 1A", and allow the punctuation filing agents use between the
    # number and the title.
    pattern = re.compile(
        rf"item\s*{re.escape(number)}(?![0-9A-Za-z])\s*[\.\:\-–—]?\s*",
        re.IGNORECASE,
    )
    return [m.start() for m in pattern.finditer(text)]


def _looks_like_toc(text: str, position: int, all_positions: list[int]) -> bool:
    """A heading inside the table of contents has another one right behind it.

    In the body, sections are thousands of characters apart. In the contents
    they're on consecutive lines, so proximity is a reliable tell and doesn't
    depend on recognising the word "Contents" in whatever language or layout
    the filing agent used.
    """
    for other in all_positions:
        if position < other <= position + TOC_PROXIMITY_CHARS:
            return True
    return False


def split_items(text: str) -> dict[str, str]:
    """Split filing text into {item number: section text}.

    Boundaries are found using EVERY 10-K item, not just the ones we report on.
    That distinction matters more than it sounds: Item 8 is the financial
    statements and we deliberately don't diff it, but if it isn't used as a
    boundary then Item 7A ("Market Risk", normally two pages) runs straight
    through all of it and comes out as the largest section in the filing.
    Reporting is a separate decision from parsing.
    """
    per_item: dict[str, list[int]] = {}
    every_position: list[int] = []
    for number in ALL_ITEM_NUMBERS:
        positions = _item_matches(text, number)
        if positions:
            per_item[number] = positions
            every_position.extend(positions)
    every_position.sort()

    # Choose one position per item, walking the items in their canonical order
    # and requiring each to come after the last one chosen. Filings are written
    # in order, so a heading that appears before the previous section is a
    # cross-reference ("as discussed in Item 1A"), not a heading -- and letting
    # one through scrambles every boundary after it.
    chosen: list[tuple[str, int]] = []
    last_position = -1
    for number in ALL_ITEM_NUMBERS:
        positions = per_item.get(number, [])
        if not positions:
            continue
        forward = [p for p in positions if p > last_position]
        if not forward:
            continue
        body = [p for p in forward
                if not _looks_like_toc(text, p, every_position)]
        # First real heading after the previous section; if every candidate
        # looks like a contents line (true for one-line sections such as
        # "Item 1B. None", whose neighbour sits right behind it), take the
        # last one, which is the body rather than the contents.
        position = body[0] if body else forward[-1]
        chosen.append((number, position))
        last_position = position

    sections: dict[str, str] = {}
    for index, (number, start) in enumerate(chosen):
        end = chosen[index + 1][1] if index + 1 < len(chosen) else len(text)
        body = text[start:end].strip()
        if len(body) >= MIN_SECTION_CHARS:
            sections[number] = body
    return sections


# ---------------------------------------------------------------------------
# diffing
# ---------------------------------------------------------------------------

def _blocks(section: str) -> list[str]:
    """Split a section into comparable blocks -- paragraphs, then sentences.

    Paragraphs are the right unit: they survive reflowing, and a changed
    sentence shows up as one changed paragraph rather than a scatter of word
    edits you can't read.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]
    if len(paragraphs) <= 3:
        # Some filings come through as one continuous run of text, so fall
        # back to sentence boundaries.
        paragraphs = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", section)
                      if s.strip()]
    return [p for p in paragraphs if len(p) > 25]


def diff_section(item: str, title: str, old: str, new: str) -> SectionDiff:
    old_words = old.split()
    new_words = new.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    similarity = matcher.ratio()

    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1

    # Readable block-level diff for the report.
    old_blocks, new_blocks = _blocks(old), _blocks(new)
    block_matcher = difflib.SequenceMatcher(None, old_blocks, new_blocks,
                                            autojunk=False)
    blocks: list[tuple[str, str]] = []
    truncated = False
    for tag, i1, i2, j1, j2 in block_matcher.get_opcodes():
        if tag == "equal":
            continue
        if len(blocks) >= MAX_CHANGED_BLOCKS_PER_SECTION:
            truncated = True
            break
        for block in old_blocks[i1:i2]:
            blocks.append(("removed", block))
        for block in new_blocks[j1:j2]:
            blocks.append(("added", block))

    return SectionDiff(
        item=item, title=title, similarity=similarity,
        words_added=added, words_removed=removed, blocks=blocks,
        old_chars=len(old), new_chars=len(new), truncated=truncated,
    )


def compare_filings(
    client: EDGARClient, cik: int, ticker: str, company_name: str
) -> FilingComparison | None:
    """Compare the two most recent 10-Ks. None when there aren't two."""
    filings = annual_filings(client, cik, limit=4)
    if len(filings) < 2:
        return None

    current, previous = filings[0], filings[1]
    try:
        new_text = fetch_filing_text(client, cik, current)
        old_text = fetch_filing_text(client, cik, previous)
    except SECError:
        return None

    new_sections = split_items(new_text)
    old_sections = split_items(old_text)

    # Last line of defence against comparing against a stub. If the previous
    # document is a fraction of the size of the current one, the comparison
    # will read as a total rewrite and mean nothing.
    if old_text and len(old_text) < 0.3 * len(new_text):
        return FilingComparison(
            ticker=ticker, company_name=company_name,
            current=current, previous=previous,
            notes=[
                f"Skipped: the previous filing ({previous.report_date}) is only "
                f"{len(old_text):,} characters against {len(new_text):,} in the "
                f"current one, so it isn't a comparable full annual report. "
                f"Comparing them would report every section as rewritten."
            ],
        )

    comparison = FilingComparison(
        ticker=ticker, company_name=company_name,
        current=current, previous=previous,
    )

    titles = dict(ITEMS)
    for number, title in ITEMS:
        old_body = old_sections.get(number, "")
        new_body = new_sections.get(number, "")
        if not old_body and not new_body:
            continue
        comparison.sections.append(
            diff_section(number, title, old_body, new_body))

    found_new = set(new_sections)
    missing = [f"Item {n} ({titles[n]})" for n, _ in ITEMS if n not in found_new]
    if missing:
        comparison.notes.append(
            "Couldn't locate " + ", ".join(missing) + " in the current filing. "
            "10-K headings aren't standardised, so this usually means an unusual "
            "layout rather than a missing section."
        )
    return comparison
