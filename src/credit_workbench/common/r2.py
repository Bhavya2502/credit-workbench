"""Cloudflare R2 helpers (the raw + parquet data lake)."""
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from credit_workbench.common.config import R2


def client(cfg: R2 | None = None):
    cfg = cfg or R2.from_env()
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


def exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload(s3, path, bucket: str, key: str) -> int:
    s3.upload_file(str(path), bucket, key)
    return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
