"""Discovery probe: read SEC index pages to learn the real bulk-file URLs and layouts.

Runs on a cloud runner with the registered SEC User-Agent. Prints findings so loaders
are written against the actual structure rather than guessed patterns.
"""
import io
import re
import zipfile

import httpx

from credit_workbench.common.config import sec_user_agent

INDEX_PAGES = {
    "FSDS": "https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets",
    "FSN": "https://www.sec.gov/data-research/sec-markets-data/financial-statement-notes-data-sets",
}
ZIP_RE = re.compile(r'href="([^"]+\.zip)"', re.IGNORECASE)


def show_archive(client: httpx.Client, label: str, url: str) -> None:
    resp = client.get(url)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    print(f"\n=== {label}: {url} ({len(resp.content) / 1e6:.1f} MB) ===")
    for info in zf.infolist():
        print(f"  {info.filename:16} {info.file_size / 1e6:9.2f} MB uncompressed")
        if info.filename.endswith((".txt", ".tsv")):
            with zf.open(info.filename) as fh:
                header = fh.readline().decode("utf-8", "replace").rstrip("\r\n")
            print(f"      COLS: {header}")


def main() -> None:
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    found: dict[str, list[str]] = {}
    with httpx.Client(headers=headers, timeout=300, follow_redirects=True) as client:
        for label, page in INDEX_PAGES.items():
            resp = client.get(page)
            links = sorted({
                link if link.startswith("http") else f"https://www.sec.gov{link}"
                for link in ZIP_RE.findall(resp.text)
            })
            found[label] = links
            print(f"\n##### {label}: {len(links)} zip links on {page} (HTTP {resp.status_code})")
            for link in links[:3]:
                print(f"  oldest: {link}")
            for link in links[-4:]:
                print(f"  newest: {link}")

        if found.get("FSDS"):
            show_archive(client, "FSDS newest", found["FSDS"][-1])
        if found.get("FSN"):
            show_archive(client, "FSN newest", found["FSN"][-1])


if __name__ == "__main__":
    main()
