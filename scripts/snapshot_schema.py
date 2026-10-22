#!/usr/bin/env python
"""Helper script to regenerate tests/schema_snapshot.json."""
import sys
from pathlib import Path

# Ensure repo root is in python path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_schema_freeze import write_schema_snapshot

if __name__ == "__main__":
    write_schema_snapshot()
