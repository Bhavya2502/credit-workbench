"""Financial covenant levels, with the sentence each one came from.

Nothing else in the workbench holds a covenant level. `quali.note_signals` can say a
waiver was mentioned and `marts.credit_events` catches an acceleration after the fact;
neither knows what the borrower actually promised.

Two findings from reading the agreements shape this, and both rule out the obvious
parser.

Most ratios in a credit agreement are not covenants. They are incurrence tests -
conditions attached to a disposition, an investment, a restricted payment or the
incurrence of more debt ("at the election of the Initial Borrower... the First Lien
Leverage Ratio would not exceed 2.00:1.00"). Taking every ratio near the words "leverage
ratio" would fill this mart with basket thresholds that look exactly like covenants.

Section boundaries alone are not enough to separate them either. The heading sometimes
sits on its own line with the covenant in sub-paragraphs beneath it, and sometimes runs
straight into the text; anchoring on it gave sections of 23 characters in one agreement
and 118,606 in another.

So the anchor is the obligation itself - "shall not permit the X Ratio ... to be greater
than 4.50:1.00" - which is how a maintenance covenant is written and an incurrence test
is not. Proximity to a financial covenant heading raises confidence rather than deciding
the matter, incurrence phrasing rules a sentence out, and every row keeps the sentence
it came from so a reader can check the machine.

A covenant is also a schedule, not a number: 83% of covenant sections carry more than
one level and half are laid out against dates, so each level is its own row.
"""
from __future__ import annotations

import argparse
import re
import time

import duckdb

from credit_workbench.common.config import R2, motherduck_token

LAKE = "r2://credit-workbench-raw"
EXHIBITS = f"{LAKE}/parquet/sec/narrative/exhibits"
OUT = f"{LAKE}/parquet/derived/covenant_terms"
# A trial run writes here instead, so it can never add rows to the mart an analyst
# reads. It did once: a 600-agreement sample re-read agreements already extracted and,
# because incremental passes write under a per-run filename, left 428 duplicates behind.
OUT_SAMPLE = f"{LAKE}/parquet/derived/covenant_terms_sample"

COVENANT_TYPES = [
    ("first_lien_leverage", r"first[\s-]lien\s+(?:net\s+)?leverage ratio"),
    ("senior_secured_leverage", r"(?:senior\s+)?secured\s+(?:net\s+)?leverage ratio"),
    ("total_net_leverage", r"(?:consolidated\s+)?total\s+net\s+leverage ratio"),
    ("net_leverage", r"(?:consolidated\s+)?net\s+leverage ratio"),
    ("total_leverage", r"(?:consolidated\s+)?(?:total\s+)?leverage ratio"),
    ("debt_to_ebitda", r"(?:debt|indebtedness)\s+to\s+ebitda"),
    ("interest_coverage", r"(?:consolidated\s+)?(?:net\s+)?interest (?:expense )?coverage ratio"),
    ("fixed_charge_coverage", r"fixed charge coverage ratio"),
    ("debt_service_coverage", r"debt service coverage ratio"),
    ("current_ratio", r"current ratio"),
    ("net_worth", r"(?:consolidated\s+)?(?:tangible\s+)?net worth"),
    ("minimum_liquidity", r"minimum liquidity|liquidity\s+(?:shall|of not less)"),
    ("minimum_ebitda", r"minimum\s+(?:consolidated\s+)?ebitda"),
]
COVENANT_RES = [(name, re.compile(p, re.IGNORECASE)) for name, p in COVENANT_TYPES]

# How a standing obligation is phrased. An incurrence test never reads like this.
OBLIGATION_RE = re.compile(
    r"(shall not (?:be permitted to )?(?:permit|suffer or permit|allow|cause)"
    r"|will not (?:permit|allow)|shall (?:at all times )?maintain"
    r"|shall not (?:exceed|be less than)|^permit\b|\bpermit the\b)",
    re.IGNORECASE)

# The obligation clause carries the negation and the comparison sits well away from it
# - "shall not permit the Leverage Ratio ... to be greater than 4.50:1.00" - so the
# comparison word alone fixes the direction. Requiring "not greater than" adjacently
# matched almost nothing.
#
# The minimum test runs first because "at least equal to or greater than" is a floor
# despite containing "greater than".
MIN_RE = re.compile(
    r"(?:at\s+least|no\s+less\s+than|not\s+less\s+than|less\s+than|minimum"
    r"|not\s+fall\s+below|equal\s+to\s+or\s+greater\s+than)",
    re.IGNORECASE)
MAX_RE = re.compile(
    r"(?:greater\s+than|more\s+than|exceed|in\s+excess\s+of|maximum)",
    re.IGNORECASE)

# Language that marks a test attached to an action rather than a standing obligation.
INCURRENCE_RE = re.compile(
    r"(after giving (?:pro forma )?effect|in connection with|at the election of"
    r"|on a pro forma basis|pro forma basis|would not exceed|immediately prior to"
    r"|for purposes of (?:determining|calculating)|applicable margin|pricing grid"
    r"|disposition percentage|excess cash flow|permitted acquisition|may (?:make|incur)"
    r"|is permitted to|so long as|net cash proceeds|asset sale|recovery event"
    r"|shall prepay|mandatory prepayment|for the avoidance of doubt"
    r"|alternate base rate|applicable rate)",
    re.IGNORECASE)

RATIO_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,3})?)\s*(?::|\s+to\s+|\s*/\s*)\s*1(?:\.0{1,3})?\b")
MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|mm|bn)?", re.IGNORECASE)

HEADING_RE = re.compile(
    r"^[ \t]*(?:(?:section|article)\s+[\dIVXLC]+(?:\.\d+)*[.\s-]*)?"
    r"(financial covenants?|financial condition covenants?"
    r"|financial performance covenants?)\b",
    re.IGNORECASE | re.MULTILINE)

PERIOD_RE = re.compile(
    r"((?:march|june|september|december)\s+\d{1,2},?\s+20\d\d"
    r"|fiscal (?:quarter|year) ending [^,.;]{0,40}20\d\d|thereafter)",
    re.IGNORECASE)

SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")


# Explicit floor and ceiling language, which means the same thing either way round.
FLOOR_RE = re.compile(
    r"(?:at\s+least|no\s+less\s+than|not\s+less\s+than|not\s+fall\s+below|minimum)",
    re.IGNORECASE)
CEILING_RE = re.compile(
    r"(?:greater\s+than|more\s+than|exceed|in\s+excess\s+of|maximum)",
    re.IGNORECASE)
# Bare "less than" means opposite things depending on the obligation it sits under.
BARE_LESS_RE = re.compile(r"(?<!not )(?<!no )less\s+than", re.IGNORECASE)
NEGATIVE_OBLIGATION_RE = re.compile(
    r"(?:shall|will)\s+not\s+(?:be permitted to\s+)?"
    r"(?:permit|suffer or permit|allow|cause)|^permit\b|\bpermit the\b",
    re.IGNORECASE)


def direction_for(clause: str, at: int, negative_obligation: bool) -> str | None:
    """The comparison closest before a level, which is the one that governs it.

    Taking whichever pattern matched first anywhere in the clause left 630 leverage
    covenants recorded as floors: a stray "not less than $1,000,000" in a proviso
    outranked the "shall not exceed" that actually governed the ratio. A level is
    governed by the comparison immediately preceding it.
    """
    window = clause[max(0, at - 160):at]
    # "equal to or greater than" is a floor, but ends in a ceiling phrase, so it is
    # rewritten before either pattern sees it.
    window = re.sub(r"equal\s+to\s+or\s+greater\s+than", "at least", window,
                    flags=re.IGNORECASE)

    last_floor = max((m.end() for m in FLOOR_RE.finditer(window)), default=-1)
    last_ceiling = max((m.end() for m in CEILING_RE.finditer(window)), default=-1)
    # "Maintain a Leverage Ratio of less than 3.00" is a ceiling; "shall not permit the
    # Ratio to be less than 3.00" is a floor. Identical words, opposite meaning, so the
    # polarity of the obligation settles it.
    last_bare = max((m.end() for m in BARE_LESS_RE.finditer(window)), default=-1)
    if last_bare > max(last_floor, last_ceiling):
        return "min" if negative_obligation else "max"
    if last_floor < 0 and last_ceiling < 0:
        return None
    return "min" if last_floor > last_ceiling else "max"


def covenant_clauses(para: str) -> list[tuple[str, str]]:
    """Split a sentence into one clause per covenant it names.

    A single sentence often carries two covenants - "shall not permit the Leverage
    Ratio to exceed 4.00 to 1.00 and the Fixed Charge Coverage Ratio to be less than
    1.15 to 1.00". Reading direction from the whole sentence gave both covenants the
    same direction and attributed both to whichever name came first, which is how 416
    leverage covenants ended up recorded as minima - the opposite of what a leverage
    covenant means.

    Each clause runs from its covenant name to the next one, so the comparison and the
    level that follow belong to it.
    """
    spans: list[tuple[int, int, str]] = []
    for name, rx in COVENANT_RES:        # ordered most specific first
        for m in rx.finditer(para):
            if any(s < m.end() and m.start() < e for s, e, _ in spans):
                continue                  # already claimed by a more specific name
            spans.append((m.start(), m.end(), name))
    if not spans:
        return []
    spans.sort()
    out = []
    for i, (start, _end, name) in enumerate(spans):
        stop = spans[i + 1][0] if i + 1 < len(spans) else len(para)
        out.append((name, para[start:stop]))
    return out


def extract(text: str) -> list[dict]:
    """Covenant levels stated as standing obligations, one row per level."""
    heading_positions = [m.start() for m in HEADING_RE.finditer(text)]
    rows: list[dict] = []
    cursor = 0

    for raw in SENTENCE_SPLIT.split(text):
        # Position first, while the raw text still matches the document, then collapse
        # the whitespace. Agreements wrap mid-phrase, so "after giving\neffect" and
        # "greater\nthan" are common - matching on the raw text let incurrence language
        # through the filter and lost covenants whose comparison straddled a line break.
        at = text.find(raw[:120], cursor) if raw[:120] else -1
        if at >= 0:
            cursor = at
        para = re.sub(r"\s+", " ", raw).strip()
        if len(para) > 3000 or len(para) < 40:
            continue
        if not OBLIGATION_RE.search(para):
            continue
        if INCURRENCE_RE.search(para):
            continue

        # Closeness to a financial covenant heading is corroboration, not the test.
        nearest = (min((abs(at - h) for h in heading_positions), default=10 ** 9)
                   if at >= 0 else 10 ** 9)
        negative = bool(NEGATIVE_OBLIGATION_RE.search(para))

        for ctype, clause in covenant_clauses(para):
            # (level, direction) pairs: each level takes the comparison governing it.
            found: list[tuple[str, str]] = []
            unit = "ratio"
            for m in RATIO_RE.finditer(clause):
                d = direction_for(clause, m.start(), negative)
                if d:
                    found.append((m.group(1), d))
            if not found and ctype in ("net_worth", "minimum_liquidity",
                                       "minimum_ebitda"):
                for m in MONEY_RE.finditer(clause):
                    d = direction_for(clause, m.start(), negative)
                    if not d:
                        continue
                    amount = float(m.group(1).replace(",", ""))
                    scale = (m.group(2) or "").lower()
                    amount *= 1e6 if scale in ("million", "mm") else (
                        1e9 if scale in ("billion", "bn") else 1)
                    found.append((str(amount), d))
                unit = "usd"
            if not found:
                continue

            seen: dict[str, str] = {}
            for level, d in found:
                seen.setdefault(level, d)
            levels = list(seen)
            periods = [m.group(1) for m in PERIOD_RE.finditer(clause)]

            for i, level in enumerate(levels):
                direction = seen[level]
                rows.append({
                    "covenant_type": ctype,
                    "direction": direction,
                    "level": float(level),
                    "unit": unit,
                    "level_index": i,
                    "is_schedule": len(set(levels)) > 1,
                    "applies_from": periods[i] if i < len(periods) else None,
                    "near_covenant_heading": nearest < 4000,
                    "confidence": "high" if nearest < 4000 else "low",
                    "chars_from_heading": min(nearest, 10 ** 6),
                    "sentence": para[:600],
                })
    return rows


def connect() -> duckdb.DuckDBPyConnection:
    cfg = R2.from_env()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        CREATE OR REPLACE SECRET r2_lake (
            TYPE R2, KEY_ID '{cfg.access_key_id}', SECRET '{cfg.secret_access_key}',
            ACCOUNT_ID '{cfg.account_id}', REGION 'auto')""")
    # Leaves headroom for the Python side, which holds a batch of agreement text and
    # the rows extracted from it. At 9GB DuckDB and Python together took the runner
    # down once the corpus reached 40,596 agreements.
    con.execute("SET memory_limit = '5GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = '/tmp/duckdb'")
    return con


COLUMNS = """
        cik VARCHAR, adsh VARCHAR, form VARCHAR, filing_date VARCHAR,
        exhibit_number VARCHAR, file_name VARCHAR, doc_kind VARCHAR, covenant_type VARCHAR,
        direction VARCHAR, level DOUBLE, unit VARCHAR, level_index INTEGER,
        is_schedule BOOLEAN, applies_from VARCHAR, near_covenant_heading BOOLEAN,
        confidence VARCHAR, chars_from_heading BIGINT, sentence VARCHAR"""


def clear_output(con: duckdb.DuckDBPyConnection, prefix: str) -> None:
    """Delete an output prefix so a rebuild replaces rather than accumulates.

    DuckDB's OVERWRITE_OR_IGNORE permits writing into a populated directory; it does not
    empty it. Without this, every rebuild layers another copy on top of the last.
    """
    from credit_workbench.common import r2 as r2util
    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    key_prefix = prefix.replace(f"r2://{cfg.bucket}/", "")
    paginator = s3.get_paginator("list_objects_v2")
    keys = [{"Key": o["Key"]}
            for page in paginator.paginate(Bucket=cfg.bucket, Prefix=key_prefix)
            for o in page.get("Contents", [])]
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=cfg.bucket, Delete={"Objects": keys[i:i + 1000]})
    print(f"cleared {len(keys):,} existing objects under {key_prefix}")


def build(sample: int = 0, rebuild_all: bool = False, reset: bool = False) -> None:
    """Read the agreements a batch at a time, writing each batch out before the next.

    An earlier version pulled every agreement's text into one table and accumulated all
    the extracted rows in Python. That fitted at 12,164 agreements and took the runner
    down at 40,596: the text alone is roughly eight gigabytes. Only the keys are held
    now, the text arrives one batch at a time, and each batch is written before the
    next is read.
    """
    con = connect()
    out = OUT_SAMPLE if sample else OUT
    if reset or sample:
        clear_output(con, out)

    # Only read agreements that have not been read before. The mart already covers
    # 2019-2026, so a rerun after a backfill should cost the new agreements and not the
    # whole corpus again - which matters when compute is the binding constraint.
    already: set[str] = set()
    if not sample and not rebuild_all:
        try:
            already = {r[0] for r in con.execute(
                f"SELECT DISTINCT adsh FROM read_parquet('{out}/*/*.parquet', "
                f"hive_partitioning = true, union_by_name = true)").fetchall()}
            print(f"{len(already):,} agreements already extracted, and will be skipped")
        except duckdb.IOException:
            print("no existing covenant output; reading the whole corpus")

    con.execute(f"""
        CREATE OR REPLACE TABLE done (adsh VARCHAR)""")
    if already:
        con.executemany("INSERT INTO done VALUES (?)", [(a,) for a in already])

    # Keyed on file name, not exhibit number. The number is derived from the file name,
    # so two files in one filing - ex10d1.htm and ex10-1.htm - both resolve to "10.1";
    # joining on it fanned out and processed those documents twice, leaving 2,016
    # duplicate levels in a build that was otherwise clean.
    con.execute(f"""
        CREATE OR REPLACE TABLE keys AS
        SELECT DISTINCT ON (e.adsh, e.file_name) e.adsh, e.file_name
        FROM read_parquet('{EXHIBITS}/*/*.parquet', hive_partitioning = true,
                          union_by_name = true) e
        LEFT JOIN done d ON d.adsh = e.adsh
        WHERE e.doc_kind IN ('credit_agreement', 'amendment', 'note_purchase')
          AND d.adsh IS NULL
        {f'ORDER BY hash(e.adsh) LIMIT {sample}' if sample else ''}""")
    con.execute("""
        CREATE OR REPLACE TABLE keys_n AS
        SELECT adsh, file_name, row_number() OVER (ORDER BY adsh, file_name) AS rn
        FROM keys""")
    total = con.execute("SELECT count(*) FROM keys_n").fetchone()[0]
    print(f"{total:,} agreements to read")

    con.execute(f"CREATE OR REPLACE TABLE terms ({COLUMNS})")
    written = 0
    batch_size = 400

    for lo in range(0, total, batch_size):
        batch = con.execute(f"""
            SELECT DISTINCT ON (e.adsh, e.file_name)
                   e.cik, e.adsh, e.form, e.filing_date, e.exhibit_number, e.file_name,
                   e.doc_kind, e.text
            FROM read_parquet('{EXHIBITS}/*/*.parquet', hive_partitioning = true,
                              union_by_name = true) e
            JOIN keys_n k ON k.adsh = e.adsh AND k.file_name = e.file_name
            WHERE k.rn > {lo} AND k.rn <= {lo + batch_size}
              AND e.doc_kind IN ('credit_agreement', 'amendment', 'note_purchase')
        """).fetchall()

        rows = []
        for cik, adsh, form, filed, exhibit, fname, kind, text in batch:
            for row in extract(text or ""):
                rows.append((str(cik), str(adsh), form, str(filed), exhibit, fname, kind,
                             row["covenant_type"], row["direction"], row["level"],
                             row["unit"], row["level_index"], row["is_schedule"],
                             row["applies_from"], row["near_covenant_heading"],
                             row["confidence"], row["chars_from_heading"],
                             row["sentence"]))
        if rows:
            con.executemany(
                "INSERT INTO terms VALUES (" + ", ".join("?" * 18) + ")", rows)
            written += len(rows)
        del batch, rows
        if lo % 4000 == 0:
            print(f"  {min(lo + batch_size, total):,}/{total:,} agreements, "
                  f"{written:,} covenant rows")

    if not written:
        raise SystemExit("no covenant terms extracted - refusing to write an empty mart")

    # A distinct filename per run, so an incremental pass adds to the partitions rather
    # than replacing what earlier passes wrote.
    stamp = f"cov_{int(time.time())}"
    con.execute(f"""
        COPY (SELECT *, CAST(substr(filing_date, 1, 4) AS INTEGER) AS filing_year
              FROM terms)
        TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (filing_year),
                    OVERWRITE_OR_IGNORE, FILENAME_PATTERN '{stamp}_{{i}}')""")
    print(f"DONE: {written:,} covenant levels from {total:,} agreements")


def register() -> None:
    md = duckdb.connect(f"md:credit_workbench?motherduck_token={motherduck_token()}")
    md.execute("DROP VIEW IF EXISTS marts.covenant_terms")
    md.execute(f"""
        CREATE VIEW marts.covenant_terms AS SELECT * FROM read_parquet(
            '{OUT}/*/*.parquet', hive_partitioning = true, union_by_name = true)""")
    rows, agreements, companies = md.execute("""
        SELECT count(*), count(DISTINCT adsh), count(DISTINCT cik)
        FROM marts.covenant_terms""").fetchone()
    print(f"view  marts.covenant_terms  {rows:,} levels, {agreements:,} agreements, "
          f"{companies:,} companies")

    # The headline figure a lender wants: the binding level per company and covenant,
    # taken from the most recent agreement and, where a schedule steps down, the
    # tightest level in it.
    md.execute("DROP VIEW IF EXISTS marts.covenant_headline")
    md.execute("""
        CREATE VIEW marts.covenant_headline AS
        SELECT cik, covenant_type, direction, filing_date, adsh,
               CASE WHEN direction = 'max' THEN min(level) ELSE max(level) END AS binding_level,
               count(*) AS levels_in_schedule,
               any_value(sentence) AS evidence
        FROM marts.covenant_terms
        WHERE confidence = 'high'
        GROUP BY cik, covenant_type, direction, filing_date, adsh
        QUALIFY row_number() OVER (
            PARTITION BY cik, covenant_type ORDER BY filing_date DESC) = 1""")
    n = md.execute("SELECT count(*) FROM marts.covenant_headline").fetchone()[0]
    print(f"view  marts.covenant_headline  {n:,} company-covenant levels")

    for row in md.execute("""
        SELECT covenant_type, direction, count(*) AS levels,
               count(DISTINCT cik) AS companies, round(median(level), 2) AS median_level
        FROM marts.covenant_terms WHERE near_covenant_heading
        GROUP BY 1, 2 ORDER BY levels DESC LIMIT 14""").fetchall():
        print(f"  {row[0]:<26} {row[1]:<4} {row[2]:>7,} levels  "
              f"{row[3]:>5,} companies  median {row[4]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--rebuild-all", action="store_true",
                    help="re-read every agreement instead of only the new ones")
    ap.add_argument("--reset", action="store_true",
                    help="clear the output prefix first, so a rebuild replaces it")
    args = ap.parse_args()
    if args.register:
        register()
    else:
        build(args.sample, args.rebuild_all, args.reset)


if __name__ == "__main__":
    main()
