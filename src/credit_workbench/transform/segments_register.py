"""Argument-free entry point for the segment/concentration registration (F1, F2).

The batched build writes parquet; this publishes the views over it. Kept separate so
it can be run through any workflow that takes a bare module path, which matters when
one runner queue is unavailable.
"""
from credit_workbench.transform.segments import register

if __name__ == "__main__":
    register()
