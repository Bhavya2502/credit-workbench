"""One-off discovery probe: confirm SEC bulk URL patterns and archive contents.

Runs on a cloud runner with the registered SEC User-Agent. Prints what it finds so
the loaders can be written against the real file layout rather than assumptions.
"""
import io
import zipfile

import httpx

from credit_workbench.common.config import sec_user_agent

CANDIDATES = {
    "submissions_bulk": "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
    "fsds_2026q1": "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q1.zip",
    "fsds_2009q2": "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2009q2.zip",
    "fsn_2026_01": "https://www.sec.gov/files/dera/data/financial-statement-and-notes-data-sets/2026_01_notes.zip",
    "fsn_2020q1": "https://www.sec.gov/files/dera/data/financial-statement-and-notes-data-sets/2020q1_notes.zip",
    "fsn_2009q2": "https://www.sec.gov/files/dera/data/financial-statement-and-notes-data-sets/2009q2_notes.zip",
    "company_tickers": "https://www.sec.gov/files/company_tickers.json",
    "company_tickers_exchange": "https://www.sec.gov/files/company_tickers_exchange.json",
}


def main() -> None:
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(headers=headers, timeout=120, follow_redirects=True) as client:
        for label, url in CANDIDATES.items():
            try:
                resp = client.head(url)
                size = resp.headers.get("content-length", "?")
                mb = f"{int(size) / 1e6:.1f} MB" if size.isdigit() else size
                print(f"[{resp.status_code}] {label:26} {mb:>12}  {url}")
            except Exception as exc:  # noqa: BLE001
                print(f"[ERR] {label:26} {exc}  {url}")

        # Inspect the structure of one small archive of each family
        for label, url in (("FSDS 2026q1",
                            "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q1.zip"),
                           ("FSN 2026_01",
                            "https://www.sec.gov/files/dera/data/financial-statement-and-notes-data-sets/2026_01_notes.zip")):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                zf = zipfile.ZipFile(io.BytesIO(resp.content))
                print(f"\n=== {label} contents ===")
                for info in zf.infolist():
                    print(f"  {info.filename:16} {info.file_size / 1e6:9.2f} MB")
                    if info.filename.endswith((".txt", ".tsv")):
                        with zf.open(info.filename) as fh:
                            header = fh.readline().decode("utf-8", "replace").rstrip("\r\n")
                        print(f"      cols: {header}")
            except Exception as exc:  # noqa: BLE001
                print(f"[ERR] inspecting {label}: {exc}")


if __name__ == "__main__":
    main()
