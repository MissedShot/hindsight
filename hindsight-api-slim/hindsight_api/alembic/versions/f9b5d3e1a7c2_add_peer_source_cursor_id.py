"""Add composite peer-model source cursor tie-breaker.

Revision ID: f9b5d3e1a7c2
Revises: e8a4c2d7f1b6
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "f9b5d3e1a7c2"
down_revision: str | Sequence[str] | None = "e8a4c2d7f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_schema_prefix() -> str:
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_upgrade() -> None:
    op.execute(f"ALTER TABLE {_pg_schema_prefix()}peer_models ADD COLUMN source_cursor_id UUID")


def _pg_downgrade() -> None:
    op.execute(f"ALTER TABLE {_pg_schema_prefix()}peer_models DROP COLUMN source_cursor_id")


def _oracle_upgrade() -> None:
    op.execute("ALTER TABLE peer_models ADD (source_cursor_id RAW(16))")


def _oracle_downgrade() -> None:
    op.execute("ALTER TABLE peer_models DROP COLUMN source_cursor_id")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_downgrade)
