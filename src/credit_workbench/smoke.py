"""Environment self-test. Run by CI on every push to prove the cloud toolchain works."""
import sys


def main() -> None:
    print(f"python {sys.version.split()[0]}")
    import boto3
    import duckdb
    import httpx
    import pandas
    import pyarrow

    for mod in (duckdb, pandas, pyarrow, httpx, boto3):
        print(f"{mod.__name__} {getattr(mod, '__version__', '?')}")
    con = duckdb.connect()
    assert con.sql("SELECT 40 + 2").fetchone()[0] == 42
    print("Environment OK")


if __name__ == "__main__":
    main()
