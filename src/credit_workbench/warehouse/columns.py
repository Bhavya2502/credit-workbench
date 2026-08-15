"""Column lists for the tables a new session will actually query."""
from __future__ import annotations

import duckdb

from credit_workbench.common.config import motherduck_token

WANTED = [
    "staging.facts_pit", "marts.spread_lines", "marts.ratio_values",
    "marts.ratio_percentiles", "marts.adjustment_inputs", "marts.segments",
    "marts.debt_instruments", "marts.revolver_capacity", "marts.covenant_terms",
    "marts.covenant_headline", "marts.credit_events", "marts.credit_outcomes",
    "marts.model_dataset", "marts.facts_by_note", "marts.legal_entity_detail",
    "marts.fair_value_hierarchy", "marts.facts_dimensioned",
    "quali.filing_sections", "quali.debt_agreements", "quali.note_signals",
    "ref.note_index", "ref.tag_note_map", "ref.tag_catalog", "ref.dim_company",
    "ref.modelled_tags", "ref.dimension_index",
]


def main() -> None:
    con = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    for name in WANTED:
        schema, table = name.split(".")
        cols = con.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position""",
            [schema, table]).fetchall()
        if not cols:
            print(f"\n### {name}\n  (not found)")
            continue
        print(f"\n### {name}")
        print("  " + ", ".join(c for c, _ in cols))


if __name__ == "__main__":
    main()
