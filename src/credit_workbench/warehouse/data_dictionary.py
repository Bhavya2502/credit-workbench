"""Tracker A7 — generate docs/data_dictionary.md from the live warehouse.

Reads information_schema plus the curated notes below, so the dictionary can never
drift from what actually exists. Re-run after any schema change.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import duckdb

from credit_workbench.common.config import motherduck_token

SCHEMA_NOTES = {
    "raw": "As received from the source, every column text. Never edited by hand.",
    "staging": "Typed, deduplicated, point-in-time aware. Built by transforms.",
    "marts": "Analysis-ready: spreads, adjusted figures, ratios, benchmarks, events.",
    "ref": "Reference and master data: entities, identifiers, industry codes, tag dictionary.",
    "quali": "Indexes over the qualitative text corpus (filing sections, audit flags, scores).",
    "events": "Event feeds powering early-warning signals.",
}

TABLE_NOTES = {
    "ref.dim_company": "Entity master, one row per SEC filer — name, SIC, EIN, filer category, incorporation, addresses. Source: bulk submissions (B1).",
    "ref.company_tickers": "One row per listing; a company may have several tickers/exchanges (B1).",
    "ref.former_names": "Name-change history with effective dates — needed to match older filings and news (B1).",
    "ref.filing_index": "Every EDGAR filing: accession, form, dates, 8-K item codes, XBRL flags. Feeds the event feed (H1) and late-filing signals (H3).",
    "ref.xbrl_tag": "Deduplicated XBRL tag dictionary: label, documentation, datatype, debit/credit, custom-extension flag. The key to reading any fact.",
    "raw.fsds_sub": "Filing headers from the Financial Statement Data Sets (C2).",
    "raw.fsds_num": "Face-financial numeric facts (C2).",
    "raw.fsds_pre": "Presentation: which statement and line a fact appears on (C2).",
    "raw.fsn_sub": "Filing headers from the Financial Statement AND Notes sets, incl. public float (C3).",
    "raw.fsn_num": "Every numeric fact including footnote detail — leases, pensions, debt schedules (C3). Basis of the adjustments engine (D1).",
    "raw.fsn_txt": "Full text of every tagged text block: the narrative of each note (C3). Basis of the qualitative corpus (G).",
    "raw.fsn_dim": "Segment/axis dimensions behind each fact — business and geographic segments (F1) and customer concentration (F2).",
    "raw.fsn_pre": "Presentation ordering for notes and statements (C3).",
    "raw.fsn_cal": "Calculation relationships: which tags sum into which (C3). Used to validate mapped subtotals (M2).",
    "raw.fsn_ren": "Rendering metadata: report titles and menu categories (C3).",
    "staging.fsds_num_typed": "fsds_num with dates, integers and values cast to real types.",
    "staging.fsn_num_typed": "fsn_num with dates, integers and values cast to real types.",
}


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    rows = con.execute("""
        SELECT c.table_schema, c.table_name, t.table_type, c.ordinal_position,
               c.column_name, c.data_type
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_catalog = 'credit_workbench'
        ORDER BY c.table_schema, c.table_name, c.ordinal_position""").fetchall()

    counts: dict[str, int] = {}
    for schema, table, *_ in rows:
        key = f"{schema}.{table}"
        if key not in counts:
            try:
                counts[key] = con.execute(f"SELECT count(*) FROM {key}").fetchone()[0]
            except Exception:  # noqa: BLE001
                counts[key] = -1

    out = [
        "# Data dictionary",
        "",
        f"Generated from the live warehouse on {datetime.date.today():%Y-%m-%d} "
        "by `credit_workbench.warehouse.data_dictionary`. Do not edit by hand.",
        "",
    ]
    current_schema = current_table = None
    for schema, table, ttype, _pos, column, dtype in rows:
        if schema != current_schema:
            current_schema = schema
            out += [f"## `{schema}`", "", SCHEMA_NOTES.get(schema, ""), ""]
            current_table = None
        if table != current_table:
            current_table = table
            key = f"{schema}.{table}"
            n = counts.get(key, -1)
            label = "view over R2 parquet" if ttype == "VIEW" else "materialised table"
            out += [
                f"### `{key}` — {label}"
                + (f", {n:,} rows" if n >= 0 else ""),
                "",
                TABLE_NOTES.get(key, ""),
                "",
                "| # | Column | Type |",
                "|---|---|---|",
            ]
        out.append(f"| {_pos} | `{column}` | {dtype} |")
        if column == rows[-1][4] and table == rows[-1][1]:
            out.append("")

    path = Path("docs/data_dictionary.md")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Wrote {path} — {len({(r[0], r[1]) for r in rows})} objects, {len(rows)} columns")


if __name__ == "__main__":
    main()
