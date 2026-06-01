from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_compatibility_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    table_columns = {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in inspector.get_table_names()
    }

    statements: list[str] = []

    if "submissions" in table_columns:
        if "source" not in table_columns["submissions"]:
            statements.append("ALTER TABLE submissions ADD COLUMN source VARCHAR(32) DEFAULT 'manual'")
        if "sample_case_id" not in table_columns["submissions"]:
            statements.append("ALTER TABLE submissions ADD COLUMN sample_case_id VARCHAR(128)")
        if "deleted_at" not in table_columns["submissions"]:
            statements.append("ALTER TABLE submissions ADD COLUMN deleted_at TIMESTAMP")

    if "receipts" in table_columns:
        if "storage_backend" not in table_columns["receipts"]:
            statements.append("ALTER TABLE receipts ADD COLUMN storage_backend VARCHAR(32) DEFAULT 'local'")
        if "storage_uri" not in table_columns["receipts"]:
            statements.append("ALTER TABLE receipts ADD COLUMN storage_uri VARCHAR(1000)")
        if "source" not in table_columns["receipts"]:
            statements.append("ALTER TABLE receipts ADD COLUMN source VARCHAR(32) DEFAULT 'manual_upload'")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
