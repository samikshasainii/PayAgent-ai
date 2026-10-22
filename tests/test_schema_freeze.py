import json
from pathlib import Path
import pytest
from src.database import Base
import src.models  # Ensure all models are registered in Base.metadata

SNAPSHOT_PATH = Path(__file__).parent / "schema_snapshot.json"


def test_schema_freeze():
    """
    Verifies that the current SQLAlchemy models match the frozen schema snapshot.
    The database schema is frozen as of M3.6 before the first Render deploy.
    Any deliberate change requires updating tests/schema_snapshot.json and
    resetting the hosted database.
    """
    assert SNAPSHOT_PATH.exists(), f"Schema snapshot file not found: {SNAPSHOT_PATH}"

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        raw_expected = json.load(f)

    # Normalize to tuple-based rows for exact comparison
    expected_snapshot = {
        table: [tuple(col) for col in cols]
        for table, cols in raw_expected.items()
    }

    current_metadata = {
        table_name: sorted(
            [(c.name, str(c.type), c.nullable) for c in table.columns],
            key=lambda x: x[0]
        )
        for table_name, table in sorted(Base.metadata.tables.items())
    }

    mismatch_msg = (
        "Schema mismatch detected against frozen snapshot!\n"
        "The database schema is frozen for M3.5+ Render deployment. Any schema change "
        "must be deliberate: update tests/schema_snapshot.json, drop and recreate the hosted DB, "
        "and run 'python -m src.seed --reset' (see ARCHITECTURE.md §4.4)."
    )

    diffs = []
    current_tables = set(current_metadata.keys())
    expected_tables = set(expected_snapshot.keys())

    if current_tables != expected_tables:
        diffs.append(f"Tables added: {current_tables - expected_tables}, Tables removed: {expected_tables - current_tables}")

    for table in current_tables & expected_tables:
        curr_cols = current_metadata[table]
        exp_cols = expected_snapshot[table]
        if curr_cols != exp_cols:
            diffs.append(f"Table '{table}' columns mismatch:\n  Current:  {curr_cols}\n  Expected: {exp_cols}")

    if diffs:
        pytest.fail(f"{mismatch_msg}\nDifferences:\n" + "\n".join(diffs))
