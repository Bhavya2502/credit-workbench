"""Tracker C2 + C3 — SEC Financial Statement (and Notes) Data Sets into the data lake.

Two SEC dataset families, same shape:

  fsds  Financial Statement Data Sets      quarterly, 2009q1 ->  4 files: sub pre num tag
  fsn   Financial Statement AND Notes      quarterly then monthly, 2009q1 ->
        8 files: sub tag dim ren cal pre num txt   (txt = full footnote text,
        dim = segment/axis dimensions behind segment and concentration analysis)

Archive URLs are discovered from the SEC index pages rather than guessed, so new
months are picked up automatically.

For each archive: original ZIP -> R2 raw zone (audit trail), then every TSV converted
to ZSTD parquet -> R2 parquet zone, partitioned by period. Columns are kept as text in
the raw zone (fidelity first); typing happens in the staging views.

Idempotent: an archive whose parquet already exists is skipped unless --force.
"""
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
import httpx

from credit_workbench.common import r2 as r2util
from credit_workbench.common.config import R2, sec_user_agent

INDEX_PAGES = {
    "fsds": "https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets",
    "fsn": "https://www.sec.gov/data-research/sec-markets-data/financial-statement-notes-data-sets",
}
ZIP_RE = re.compile(r'href="([^"]+\.zip)"', re.IGNORECASE)
PERIOD_RE = re.compile(r"(\d{4})(?:q([1-4])|_(\d{2}))")


def discover(client: httpx.Client, dataset: str) -> list[tuple[str, str]]:
    """Return [(period, url)] sorted by period, e.g. ('2026_06', 'https://...')."""
    resp = client.get(INDEX_PAGES[dataset])
    resp.raise_for_status()
    out: dict[str, str] = {}
    for href in ZIP_RE.findall(resp.text):
        url = href if href.startswith("http") else f"https://www.sec.gov{href}"
        match = PERIOD_RE.search(Path(url).name)
        if not match:
            continue
        year, quarter, month = match.groups()
        out[f"{year}q{quarter}" if quarter else f"{year}_{month}"] = url
    return sorted(out.items())


def process(dataset: str, period: str, url: str, cfg: R2, s3, force: bool) -> dict:
    """Download one archive, mirror it to R2, convert every TSV to parquet."""
    marker = f"parquet/sec/{dataset}/_complete/period={period}/_SUCCESS"
    if not force and r2util.exists(s3, cfg.bucket, marker):
        print(f"  {period}: already loaded, skipping")
        return {"period": period, "skipped": True}

    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(headers=headers, timeout=900, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.content

    raw_key = f"raw/sec/{dataset}/period={period}/{Path(url).name}"
    s3.put_object(Bucket=cfg.bucket, Key=raw_key, Body=payload)

    con = duckdb.connect()
    tables: dict[str, int] = {}
    with TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            members = [m for m in zf.namelist() if m.endswith((".txt", ".tsv"))]
            zf.extractall(tmp, members=members)

        for member in members:
            table = Path(member).stem.lower()          # sub, num, pre, tag, dim, cal, ren, txt
            src = (tmp / member).as_posix()
            dst = tmp / f"{table}.parquet"
            con.execute(
                f"""
                COPY (SELECT * FROM read_csv(
                        '{src}',
                        delim = '\t', header = true, quote = '', escape = '',
                        all_varchar = true, null_padding = true, strict_mode = false))
                TO '{dst.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
            rows = con.execute(
                f"SELECT count(*) FROM read_parquet('{dst.as_posix()}')"
            ).fetchone()[0]
            key = f"parquet/sec/{dataset}/{table}/period={period}/data.parquet"
            size = r2util.upload(s3, dst, cfg.bucket, key)
            tables[table] = rows
            print(f"  {period}/{table:4} {rows:>10,} rows  {size / 1e6:8.1f} MB -> {key}")

    s3.put_object(Bucket=cfg.bucket, Key=marker, Body=b"")
    return {"period": period, "skipped": False, "tables": tables}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["fsds", "fsn"], required=True)
    ap.add_argument("--years", default="", help="e.g. 2024 or 2009-2012; blank = all")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(headers=headers, timeout=300, follow_redirects=True) as client:
        archives = discover(client, args.dataset)

    if args.years:
        lo, _, hi = args.years.partition("-")
        lo, hi = int(lo), int(hi or lo)
        archives = [(p, u) for p, u in archives if lo <= int(p[:4]) <= hi]

    print(f"{args.dataset}: {len(archives)} archive(s) to consider "
          f"({archives[0][0]} .. {archives[-1][0]})" if archives else "nothing to do")
    total_rows = 0
    for period, url in archives:
        result = process(args.dataset, period, url, cfg, s3, args.force)
        total_rows += sum(result.get("tables", {}).values())
    print(f"DONE {args.dataset}: {len(archives)} archives, {total_rows:,} rows written")


if __name__ == "__main__":
    main()
