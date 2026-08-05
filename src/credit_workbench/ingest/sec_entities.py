"""Tracker B1 — entity master from the SEC bulk submissions archive.

One pass over submissions.zip (~1.5 GB, one JSON per CIK) produces four parquet
datasets in R2, capturing every field the SEC publishes about a filer:

  companies     one row per CIK: name, SIC + description, EIN, entity type, filer
                category, fiscal year end, state of incorporation, both addresses,
                phone, websites, owner org, insider-transaction flags
  tickers       one row per listing (a company can have several)
  former_names  name-change history with effective dates
  filings       the complete filing index: accession, form, dates, 8-K item codes,
                XBRL flags, primary document  (this is what powers H1/H3 later)

Raw fidelity: every value is written as text; typing happens in the staging views.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from credit_workbench.common import r2 as r2util
from credit_workbench.common.config import R2, sec_user_agent

SUBMISSIONS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"

COMPANY_FIELDS = [
    "cik", "name", "entity_type", "sic", "sic_description", "ein", "description",
    "category", "fiscal_year_end", "state_of_incorporation",
    "state_of_incorporation_description", "phone", "website", "investor_website",
    "owner_org", "insider_transaction_for_owner_exists",
    "insider_transaction_for_issuer_exists", "flags",
    "business_street1", "business_street2", "business_city", "business_state",
    "business_zip", "business_country",
    "mailing_street1", "mailing_street2", "mailing_city", "mailing_state",
    "mailing_zip", "mailing_country",
]
FILING_FIELDS = [
    "cik", "accession_number", "filing_date", "report_date", "acceptance_datetime",
    "act", "form", "file_number", "film_number", "items", "core_type", "size",
    "is_xbrl", "is_inline_xbrl", "primary_document", "primary_doc_description",
]
FILING_KEYS = [
    "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "act",
    "form", "fileNumber", "filmNumber", "items", "core_type", "size", "isXBRL",
    "isInlineXBRL", "primaryDocument", "primaryDocDescription",
]
BATCH = 2_000_000


def _s(value) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _addr(addresses: dict, kind: str, key: str) -> str | None:
    return _s((addresses.get(kind) or {}).get(key))


def _company_row(doc: dict) -> list:
    addresses = doc.get("addresses") or {}
    return [
        _s(doc.get("cik")), _s(doc.get("name")), _s(doc.get("entityType")),
        _s(doc.get("sic")), _s(doc.get("sicDescription")), _s(doc.get("ein")),
        _s(doc.get("description")), _s(doc.get("category")),
        _s(doc.get("fiscalYearEnd")), _s(doc.get("stateOfIncorporation")),
        _s(doc.get("stateOfIncorporationDescription")), _s(doc.get("phone")),
        _s(doc.get("website")), _s(doc.get("investorWebsite")), _s(doc.get("ownerOrg")),
        _s(doc.get("insiderTransactionForOwnerExists")),
        _s(doc.get("insiderTransactionForIssuerExists")),
        _s(",".join(doc.get("flags") or []) if isinstance(doc.get("flags"), list)
           else doc.get("flags")),
        _addr(addresses, "business", "street1"), _addr(addresses, "business", "street2"),
        _addr(addresses, "business", "city"), _addr(addresses, "business", "stateOrCountry"),
        _addr(addresses, "business", "zipCode"),
        _addr(addresses, "business", "stateOrCountryDescription"),
        _addr(addresses, "mailing", "street1"), _addr(addresses, "mailing", "street2"),
        _addr(addresses, "mailing", "city"), _addr(addresses, "mailing", "stateOrCountry"),
        _addr(addresses, "mailing", "zipCode"),
        _addr(addresses, "mailing", "stateOrCountryDescription"),
    ]


def _filing_rows(cik: str, block: dict) -> list[list]:
    accessions = block.get("accessionNumber") or []
    columns = [block.get(key) or [] for key in FILING_KEYS]
    rows = []
    for i in range(len(accessions)):
        row = [cik]
        for col in columns:
            row.append(_s(col[i]) if i < len(col) else None)
        rows.append(row)
    return rows


def main() -> None:
    cfg = R2.from_env()
    s3 = r2util.client(cfg)
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}

    with TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        archive = tmp / "submissions.zip"
        print("Downloading submissions.zip ...")
        with httpx.stream("GET", SUBMISSIONS_URL, headers=headers, timeout=1800,
                          follow_redirects=True) as resp:
            resp.raise_for_status()
            with archive.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
        print(f"Downloaded {archive.stat().st_size / 1e9:.2f} GB")
        s3.upload_file(str(archive), cfg.bucket, "raw/sec/submissions/submissions.zip")

        companies: list[list] = []
        tickers: list[list] = []
        former: list[list] = []
        filing_schema = pa.schema([(name, pa.string()) for name in FILING_FIELDS])
        filings_path = tmp / "filings.parquet"
        writer = pq.ParquetWriter(filings_path, filing_schema, compression="zstd")
        pending: list[list] = []
        n_files = n_filings = 0

        def flush() -> None:
            nonlocal pending
            if pending:
                cols = list(zip(*pending))
                writer.write_table(pa.table(
                    {name: pa.array(col, type=pa.string())
                     for name, col in zip(FILING_FIELDS, cols)},
                    schema=filing_schema))
                pending = []

        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.endswith(".json"):
                    continue
                with zf.open(info) as fh:
                    doc = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
                n_files += 1

                if "-submissions-" in name:            # older filing pages
                    cik = _s(name.split("CIK")[1][:10].lstrip("0")) or "0"
                    rows = _filing_rows(cik, doc)
                else:                                   # main entity file
                    cik = _s(doc.get("cik"))
                    companies.append(_company_row(doc))
                    for ticker, exch in zip(doc.get("tickers") or [],
                                            (doc.get("exchanges") or []) + [None] * 9):
                        tickers.append([cik, _s(ticker), _s(exch)])
                    for entry in doc.get("formerNames") or []:
                        former.append([cik, _s(entry.get("name")),
                                       _s(entry.get("from")), _s(entry.get("to"))])
                    rows = _filing_rows(cik, (doc.get("filings") or {}).get("recent") or {})

                pending.extend(rows)
                n_filings += len(rows)
                if len(pending) >= BATCH:
                    flush()
                if n_files % 200_000 == 0:
                    print(f"  parsed {n_files:,} json files, {n_filings:,} filings, "
                          f"{len(companies):,} companies")

        flush()
        writer.close()

        def write_upload(rows: list[list], fields: list[str], table: str) -> None:
            path = tmp / f"{table}.parquet"
            cols = list(zip(*rows)) if rows else [[] for _ in fields]
            pq.write_table(
                pa.table({name: pa.array(col, type=pa.string())
                          for name, col in zip(fields, cols)}),
                path, compression="zstd")
            key = f"parquet/sec/entity/{table}/data.parquet"
            size = r2util.upload(s3, path, cfg.bucket, key)
            print(f"  {table:13} {len(rows):>10,} rows  {size / 1e6:8.1f} MB -> {key}")

        write_upload(companies, COMPANY_FIELDS, "companies")
        write_upload(tickers, ["cik", "ticker", "exchange"], "tickers")
        write_upload(former, ["cik", "former_name", "name_from", "name_to"], "former_names")
        key = "parquet/sec/entity/filings/data.parquet"
        size = r2util.upload(s3, filings_path, cfg.bucket, key)
        print(f"  {'filings':13} {n_filings:>10,} rows  {size / 1e6:8.1f} MB -> {key}")
        print(f"DONE: {n_files:,} json files parsed, {len(companies):,} companies, "
              f"{n_filings:,} filings")


if __name__ == "__main__":
    main()
