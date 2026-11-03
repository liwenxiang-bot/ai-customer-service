"""Helpers that keep Alembic migrations idempotent.

0001 builds tables with ``Base.metadata.create_all`` from the *current* models, so on an empty
database every later "add column / add index" migration finds its object already present.
These helpers make those steps no-ops when the object exists, so ``alembic upgrade head`` works
on both fresh and long-lived databases.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op


def add_column_if_missing(table: str, column: sa.Column) -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def create_index_if_missing(name: str, table: str, columns: list[str], **kw) -> None:
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, **kw)
