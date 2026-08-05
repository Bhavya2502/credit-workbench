"""Environment-based settings.

Secrets are provided by GitHub Actions (repo Settings -> Secrets) at run time.
Nothing here is ever hardcoded or written to disk — see docs/secrets.md.
"""
import os
from dataclasses import dataclass


class MissingSecret(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingSecret(
            f"Run blocked: {name} is not configured. "
            "Add it under repo Settings -> Secrets and variables -> Actions "
            "(see docs/secrets.md)."
        )
    return value


@dataclass(frozen=True)
class R2:
    """Cloudflare R2 (S3-compatible object storage) — the raw data zone."""
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str

    @classmethod
    def from_env(cls) -> "R2":
        return cls(
            account_id=_require("R2_ACCOUNT_ID"),
            access_key_id=_require("R2_ACCESS_KEY_ID"),
            secret_access_key=_require("R2_SECRET_ACCESS_KEY"),
            bucket=os.environ.get("R2_BUCKET", "credit-workbench-raw"),
        )

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def motherduck_token() -> str:
    """MotherDuck (cloud DuckDB warehouse) service token."""
    return _require("MOTHERDUCK_TOKEN")


def sec_user_agent() -> str:
    """SEC fair-access policy requires a descriptive User-Agent with contact email."""
    return _require("SEC_USER_AGENT")  # e.g. "Peaks2Tails credit-workbench bhavya@peaks2tails.com"
