"""Turning filing HTML into text, two ways.

`to_text` in `ingest.filing_sections` emits a newline around every `<td>`, which suits
the 10-K narrative: risk factors and MD&A are prose, and a table inside them is
incidental. Proxy statements are the opposite. Everything the Management Risk scorecard
wants as a number - the audit and non-audit fee split, the director table, the summary
compensation table - is an HTML *table*, and a converter that puts every cell on its own
line separates each label from its figure.

Measured on 40 sampled proxies: an audit fee label sat beside its number on the same
line in 35% of documents under the cell-per-line converter and 80% under this one, at no
cost in size (1.00x). So this is not a general improvement to the older converter, which
would gain nothing from it; it is what tabular filings require.

Two details are not optional. Filings pad table cells with zero-width spaces, which
leave a cell looking non-empty and defeat any "is this blank" test, so they are stripped
rather than kept. And a nested row is emitted at its own depth rather than folded into
the cell containing it: filings wrap the real data table in a layout table, so folding
inwards collapsed an entire fee table onto one line.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# Zero-width and soft-hyphen characters used as spacers inside filing tables.
INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

CELL = " | "


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.translate(INVISIBLE).replace("\xa0", " ")).strip()


class RowExtractor(HTMLParser):
    """Strip tags, emitting each table row as one line with cells joined by ' | '."""

    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.depth = 0                     # open <table> count
        self.cells: list[list[str]] = []   # finished cells, one list per open row
        self.buf: list[str] = []           # the cell being read

    def _emit(self, s: str) -> None:
        (self.buf if self.depth else self.out).append(s)

    def _close_cell(self) -> None:
        text = clean("".join(self.buf))
        self.buf = []
        if not self.cells:
            if text:                       # inside a table but outside any row
                self.out.append("\n" + text + "\n")
            return
        self.cells[-1].append(text)

    def _close_row(self) -> None:
        if not self.cells:
            return
        self._close_cell()
        line = CELL.join(c for c in self.cells.pop() if c)
        if line:
            self.out.append("\n" + line + "\n")

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "table":
            self.depth += 1
        elif tag == "tr":
            # One row may be open per nesting level; more means a missing </tr>.
            if len(self.cells) >= max(self.depth, 1):
                self._close_row()
            self.cells.append([])
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in self.BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag == "table":
            while len(self.cells) >= max(self.depth, 1):
                self._close_row()
            self.depth = max(0, self.depth - 1)
        elif tag == "tr":
            self._close_row()
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in self.BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self._emit(data)

    def text(self) -> str:
        while self.cells:
            self._close_row()
        out = "".join(self.out).translate(INVISIBLE).replace("\xa0", " ")
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r" *\| *(\| *)+", CELL, out)     # collapse runs of empty cells
        return re.sub(r"\n\s*\n+", "\n", out).strip()


def to_rows(html: str) -> str:
    """Text with table rows kept on one line each."""
    p = RowExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001  malformed markup: keep whatever parsed
        pass
    return p.text()
