"""Compile the complete schema for PostgreSQL without claiming a live DB test."""
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from kkr_scout.database import metadata


def test_all_schema_tables_compile_for_postgresql():
    from kkr_scout import inputs, metrics
    statements = []
    engine = create_mock_engine("postgresql+psycopg://", lambda sql, *args, **kwargs: statements.append(str(sql.compile(dialect=postgresql.dialect()))))
    metadata.create_all(engine)
    inputs.inputs_metadata.create_all(engine)
    expected = {table.name for table in metadata.tables.values()} | {table.name for table in inputs.inputs_metadata.tables.values()}
    created = {table for table in expected if any(f"CREATE TABLE {table} " in statement or f"CREATE TABLE {table}\n" in statement for statement in statements)}
    assert created == expected
    assert not any("PRAGMA" in statement or "AUTOINCREMENT" in statement for statement in statements)
