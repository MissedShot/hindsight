"""Merge the v0.9.1 and native peer-modeling migration heads.

Revision ID: b7e3c1a9d4f2
Revises: c4f7a91b2d38, f9b5d3e1a7c2
Create Date: 2026-08-14 13:59:09 UTC
"""

from collections.abc import Sequence

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "b7e3c1a9d4f2"
down_revision: str | Sequence[str] | None = ("c4f7a91b2d38", "f9b5d3e1a7c2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_upgrade() -> None:
    # Both parent branches own disjoint schema changes; merging only joins their histories.
    pass


def _pg_downgrade() -> None:
    pass


def _oracle_upgrade() -> None:
    pass


def _oracle_downgrade() -> None:
    pass


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_downgrade)
