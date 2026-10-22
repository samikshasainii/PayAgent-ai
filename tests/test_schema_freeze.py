import json
import sys
from pathlib import Path
import pytest
import sqlalchemy
from sqlalchemy import UniqueConstraint
from src.database import Base
import src.models  # Ensure all models are registered in Base.metadata

SNAPSHOT_PATH = Path(__file__).parent / "schema_snapshot.json"


def build_schema_snapshot() -> dict:
    """
    Constructs a deterministic dictionary representation of the database schema
    from SQLAlchemy Base.metadata, including column types, nullability, keys,
    uniqueness, indexes, foreign keys, enum values, table indexes, and unique constraints.
    """
    snapshot = {}
    for table_name, table in sorted(Base.metadata.tables.items()):
        cols = {}
        for col in sorted(table.columns, key=lambda c: c.name):
            col_dict = {
                "type": str(col.type),
                "nullable": bool(col.nullable),
                "primary_key": bool(col.primary_key),
                "unique": bool(col.unique),
                "index": bool(col.index),
                "foreign_keys": sorted(str(fk.target_fullname) for fk in col.foreign_keys),
            }
            if isinstance(col.type, sqlalchemy.Enum):
                col_dict["enum_values"] = sorted(col.type.enums)
            cols[col.name] = col_dict

        # Table-level named indexes
        indexes = sorted([
            [
                idx.name or "",
                [c.name for c in idx.columns],
                bool(idx.unique),
            ]
            for idx in table.indexes
        ], key=lambda x: x[0])

        # Table-level unique constraints
        unique_constraints = sorted([
            [
                c.name or "",
                [col.name for col in c.columns],
            ]
            for c in table.constraints
            if isinstance(c, UniqueConstraint)
        ], key=lambda x: x[0])

        snapshot[table_name] = {
            "columns": cols,
            "indexes": indexes,
            "unique_constraints": unique_constraints,
        }
    return snapshot


def write_schema_snapshot(path: Path = SNAPSHOT_PATH):
    """Writes the current schema snapshot to the specified JSON path."""
    data = build_schema_snapshot()
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"Schema snapshot written to {path}")


def test_schema_freeze():
    """
    Verifies that the current SQLAlchemy models match the frozen schema snapshot.
    The database schema is frozen as of M3.6 before the first Render deploy.
    Any deliberate change requires updating tests/schema_snapshot.json and
    resetting the hosted database.
    """
    assert SNAPSHOT_PATH.exists(), f"Schema snapshot file not found: {SNAPSHOT_PATH}"

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        expected_snapshot = json.load(f)

    current_snapshot = build_schema_snapshot()

    mismatch_msg = (
        "Schema mismatch detected against frozen snapshot!\n"
        "The database schema is frozen for M3.5+ Render deployment. Any schema change "
        "must be deliberate: update tests/schema_snapshot.json, drop and recreate the hosted DB, "
        "and run 'python -m src.seed --recreate-schema --yes-drop-everything' (see ARCHITECTURE.md §4.4)."
    )

    diffs = []
    current_tables = set(current_snapshot.keys())
    expected_tables = set(expected_snapshot.keys())

    if current_tables != expected_tables:
        diffs.append(f"Tables added: {current_tables - expected_tables}, Tables removed: {expected_tables - current_tables}")

    for table in current_tables & expected_tables:
        curr_t = current_snapshot[table]
        exp_t = expected_snapshot[table]
        if curr_t != exp_t:
            # Check column diffs
            curr_cols = curr_t.get("columns", {})
            exp_cols = exp_t.get("columns", {})
            if curr_cols != exp_cols:
                diffs.append(f"Table '{table}' columns mismatch:\n  Current:  {curr_cols}\n  Expected: {exp_cols}")
            # Check index diffs
            curr_idx = curr_t.get("indexes", [])
            exp_idx = exp_t.get("indexes", [])
            if curr_idx != exp_idx:
                diffs.append(f"Table '{table}' indexes mismatch:\n  Current:  {curr_idx}\n  Expected: {exp_idx}")
            # Check unique constraint diffs
            curr_uc = curr_t.get("unique_constraints", [])
            exp_uc = exp_t.get("unique_constraints", [])
            if curr_uc != exp_uc:
                diffs.append(f"Table '{table}' unique_constraints mismatch:\n  Current:  {curr_uc}\n  Expected: {exp_uc}")

    if diffs:
        pytest.fail(f"{mismatch_msg}\nDifferences:\n" + "\n".join(diffs))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--write":
        write_schema_snapshot()
    else:
        print("Usage: python -m tests.test_schema_freeze --write")
