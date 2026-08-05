"""Tracker item C1 — ingest SEC companyfacts.zip into the R2 raw zone.

Runs on a GitHub Actions cloud runner (never on the owner's machine). The runner
downloads the SEC bulk file to its own temporary disk, uploads it to R2 under a
dated key, and exits; the runner's disk is destroyed with the job.

Trigger: .github/workflows/ingest_companyfacts.yml (manual now, nightly cron later).
Requires secrets: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, SEC_USER_AGENT.
"""
import datetime
import tempfile

import boto3
import httpx

from credit_workbench.common.config import R2, sec_user_agent

COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"


def run() -> str:
    r2 = R2.from_env()
    key = f"raw/sec/companyfacts/dt={datetime.date.today():%Y-%m-%d}/companyfacts.zip"
    s3 = boto3.client(
        "s3",
        endpoint_url=r2.endpoint_url,
        aws_access_key_id=r2.access_key_id,
        aws_secret_access_key=r2.secret_access_key,
        region_name="auto",
    )
    headers = {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    with tempfile.TemporaryFile() as tmp:
        with httpx.stream("GET", COMPANYFACTS_URL, headers=headers, timeout=600, follow_redirects=True) as resp:
            resp.raise_for_status()
            size = 0
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                tmp.write(chunk)
                size += len(chunk)
        tmp.seek(0)
        s3.upload_fileobj(tmp, r2.bucket, key)
    print(f"Uploaded {size / 1e9:.2f} GB to r2://{r2.bucket}/{key}")
    return key


if __name__ == "__main__":
    run()
