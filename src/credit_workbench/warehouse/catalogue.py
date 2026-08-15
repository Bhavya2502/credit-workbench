"""A complete inventory of the warehouse, measured rather than remembered.

Written for someone who has to plan a scoring methodology against this database and has
not built it. Names and row counts are the easy half; what actually decides whether an
analysis is right is the grain of each table, the key it joins on, and the handful of
places where a reasonable-looking query silently returns the wrong number.
"""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

# Grain and purpose, stated per object. Anything not listed still appears in the dump
# below, marked so it is obvious the description is missing rather than the table.
DESCRIBED: dict[str, tuple[str, str]] = {
    "staging.facts_pit": (
        "one row per (company, period, tag, unit, filing)",
        "Every consolidated XBRL fact, all vintages. FILTER is_latest OR "
        "is_first_report - without one you get every restatement of the same figure."),
    "marts.facts_dimensioned": (
        "one row per (company, period, tag, dimension hash, filing)",
        "Every dimensioned fact - the note schedules. Same vintage rule applies; a "
        "figure is re-reported by ~3.1 filings."),
    "marts.spread_lines": (
        "one row per (company, period, spread line, basis)",
        "The standardised financial statement spread, 107 lines from 234 tag "
        "alternatives. basis = 'latest' or 'first_reported'."),
    "marts.ratio_values": (
        "one row per (company, fiscal year, ratio, basis)",
        "47 ratios and 7 distress flags. Use basis='first_reported' for anything a "
        "model trains on."),
    "marts.ratio_percentiles": (
        "one row per (company, fiscal year, ratio, peer grain)",
        "Percentile rank within industry. credit_percentile_size is the one to use."),
    "marts.adjustment_inputs": (
        "one row per (company, period end, basis)",
        "146 note tags pivoted into 106 columns: lease ladders, pension, debt "
        "maturities, tax detail, capital structure, receivable quality."),
    "marts.segments": (
        "one row per (company, period, segment, measure)",
        "Business, product and geographic segment detail."),
    "marts.concentration": (
        "one row per (company, period, customer or risk concentration)",
        "Customer and supplier concentration percentages."),
    "marts.debt_instruments": (
        "one row per (company, period, instrument)",
        "Instrument-level debt: face amount, carrying amount, stated rate, basis "
        "spread, maturity, instrument type."),
    "marts.revolver_capacity": (
        "one row per (company, period, facility)",
        "Committed size, drawn and remaining capacity."),
    "marts.covenant_terms": (
        "one row per (document, covenant, direction, level)",
        "Financial covenant levels read from credit agreements, each with the sentence "
        "it came from. Filter confidence='high' for the reliable set."),
    "marts.covenant_headline": (
        "one row per (company, covenant type)",
        "The binding level per company from its most recent agreement, "
        "high-confidence rows only."),
    "marts.credit_events": (
        "one row per (company, event)",
        "8-K derived events: defaults, bankruptcies, auditor changes, delistings."),
    "marts.credit_outcomes": (
        "one row per (company, observation date)",
        "The distress label for modelling. Delisting is deliberately excluded - more "
        "than half of delistings accompany an acquisition, not a failure."),
    "marts.model_dataset": (
        "one row per (company, fiscal year)",
        "Ratios joined to outcomes on a first-reported basis - the modelling table."),
    "marts.facts_by_note": (
        "one row per (fact, note it was presented in)",
        "MANY-TO-MANY. A figure on the balance sheet and again in the debt note "
        "appears twice by design. Pick a note_category before summing money."),
    "marts.schedules_by_note": (
        "one row per (dimensioned fact, note)",
        "The same for the schedules."),
    "marts.legal_entity_detail": (
        "one row per (subsidiary-level fact)",
        "Figures by legal entity with entity_role: parent_only, guarantor, "
        "non_guarantor, vie. The basis of structural subordination analysis."),
    "marts.fair_value_hierarchy": (
        "one row per (fair-value fact, hierarchy level)",
        "Level 1/2/3. Use dimension_count=1 to avoid summing a cross-tabulation."),
    "marts.calc_check": (
        "one row per (filing, calculation arc)",
        "Whether filer-declared subtotals actually add up."),
    "quali.filing_sections": (
        "one row per (company, filing, 10-K item)",
        "1.79m narrative sections, 2009-2026. Item 8 excluded - it is the financial "
        "statements, already held as XBRL."),
    "quali.risk_factors": ("one row per (company, filing)", "Item 1A text."),
    "quali.mdna": ("one row per (company, filing)", "Item 7 text."),
    "quali.note_text": (
        "one row per (filing, text block)",
        "Note text blocks from XBRL - distinct from filing_sections, which is the "
        "10-K body."),
    "quali.note_signals": (
        "one row per (filing, signal)",
        "14 note-derived signals with negation guards. material_weakness is "
        "documented as unusable - it discriminates inversely."),
    "quali.exhibits": (
        "one row per (filing, exhibit document)",
        "EX-10 and EX-4 documents, full text."),
    "quali.debt_agreements": (
        "one row per (filing, agreement document)",
        "Exhibits classified as credit agreements, indentures, amendments."),
    "ref.note_index": (
        "one row per (filing, numbered report)",
        "Every note and statement with the filer's own title, classified into 36 "
        "types. note_category separates note text from detail schedules."),
    "ref.tag_note_map": (
        "one row per (filing, tag, report)",
        "The bridge from a fact to the note it appears in."),
    "ref.tag_catalog": (
        "one row per tag",
        "Every tag with label, usage and whether a mart claims it."),
    "ref.modelled_tags": (
        "one row per (tag, mart)",
        "Which mart claims which tag. Anything absent is genuinely unmodelled."),
    "ref.dimension_index": (
        "one row per (dimension hash, axis, member)",
        "Every axis=member pair across 35,905 axes."),
    "ref.filing_index": (
        "one row per filing",
        "Every EDGAR filing: form, dates, 8-K item codes."),
    "ref.dim_company": ("one row per company", "Entity master: name, SIC, ticker."),
}


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")

    objects = con.execute("""
        SELECT table_schema, table_name, table_type
        FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'main')
        ORDER BY table_schema, table_name""").fetchall()

    print(f"# Credit workbench catalogue — {len(objects)} objects\n")
    current = None
    for schema, name, kind in objects:
        if schema != current:
            current = schema
            print(f"\n## {schema}\n")
        full = f"{schema}.{name}"
        try:
            n = con.execute(f"SELECT count(*) FROM {full}").fetchone()[0]
            rows = f"{n:,}"
        except Exception:  # noqa: BLE001
            rows = "unavailable"
        grain, note = DESCRIBED.get(full, ("", ""))
        marker = "" if grain else "   [no description recorded]"
        print(f"- **{full}** ({'view' if 'VIEW' in kind else 'table'}) — {rows} rows{marker}")
        if grain:
            print(f"  - grain: {grain}")
            print(f"  - {note}")

    print("\n\n# Join keys\n")
    print("- `cik` — company, zero-padded string in some tables, integer in others; "
          "cast before joining")
    print("- `adsh` — accession number, the filing")
    print("- `period_end` / `fy` — the reporting period")
    print("- `dimh` + `period` — joins a dimensioned fact to ref.dimension_index")
    print("- `adsh` + `report` + `period` — joins a fact to ref.note_index")

    print("\n\n# Traps worth knowing before writing a query\n")
    for line in [
        "**Vintages.** staging.facts_pit and marts.facts_dimensioned hold every "
        "restatement. Without is_latest or is_first_report you count a figure about "
        "three times. Models must use first_reported, or they learn from data that did "
        "not exist at the observation date.",
        "**The note bridge is many-to-many.** A figure disclosed on the balance sheet "
        "and again in the debt note is two rows in marts.facts_by_note. That is the "
        "disclosure, not duplication - filter note_category before summing.",
        "**Fair value cross-tabulates.** Filers cross the hierarchy with asset class "
        "and measurement frequency. Sum only dimension_count = 1 or you add the same "
        "money several times.",
        "**Covenants carry confidence.** confidence='high' means the level sat near a "
        "financial covenant heading. Low-confidence rows are kept and marked; leverage "
        "direction errors concentrate there.",
        "**ASC 840 and ASC 842 leases are separate columns** in "
        "marts.adjustment_inputs. They are different measures and splicing them is a "
        "judgement call, not a default.",
        "**material_weakness is unusable** as a distress signal - it discriminates "
        "inversely. Documented, not hidden.",
    ]:
        print(f"- {line}")


if __name__ == "__main__":
    main()
