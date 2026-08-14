"""Enforce bank-scoped foreign keys for native peer modeling.

Revision ID: c8f4d2a1e6b9
Revises: b7e3c1a9d4f2
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import context, op
from sqlalchemy import text

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "c8f4d2a1e6b9"
down_revision: str | Sequence[str] | None = "b7e3c1a9d4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_schema_prefix() -> str:
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_preflight() -> None:
    """Fail before any DDL when existing rows already violate bank scoping.

    Oracle commits each DDL statement implicitly, so a failed ADD CONSTRAINT
    late in the sequence would leave earlier steps applied. PostgreSQL is
    transactional, but the preflight keeps both dialects on the same path and
    reports the actual offending rows instead of a bare constraint violation.
    """
    s = _pg_schema_prefix()
    checks = (
        (
            "peer_models observer",
            f"""
            SELECT pm.id
            FROM {s}peer_models pm
            LEFT JOIN {s}peers p ON p.id = pm.observer_peer_id
            WHERE pm.observer_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pm.bank_id)
            LIMIT 1
            """,
        ),
        (
            "peer_models target",
            f"""
            SELECT pm.id
            FROM {s}peer_models pm
            LEFT JOIN {s}peers p ON p.id = pm.target_peer_id
            WHERE pm.target_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pm.bank_id)
            LIMIT 1
            """,
        ),
        (
            "peer_model_claims model",
            f"""
            SELECT pmc.id
            FROM {s}peer_model_claims pmc
            LEFT JOIN {s}peer_models pm ON pm.id = pmc.model_id
            WHERE pmc.model_id IS NOT NULL AND (pm.id IS NULL OR pm.bank_id <> pmc.bank_id)
            LIMIT 1
            """,
        ),
        (
            "peer_model_claims observer",
            f"""
            SELECT pmc.id
            FROM {s}peer_model_claims pmc
            LEFT JOIN {s}peers p ON p.id = pmc.observer_peer_id
            WHERE pmc.observer_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pmc.bank_id)
            LIMIT 1
            """,
        ),
        (
            "peer_model_claims target",
            f"""
            SELECT pmc.id
            FROM {s}peer_model_claims pmc
            LEFT JOIN {s}peers p ON p.id = pmc.target_peer_id
            WHERE pmc.target_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pmc.bank_id)
            LIMIT 1
            """,
        ),
        (
            "peer_model_claim_sources claim",
            f"""
            SELECT pmcs.claim_id, pmcs.source_id
            FROM {s}peer_model_claim_sources pmcs
            LEFT JOIN {s}peer_model_claims pmc ON pmc.id = pmcs.claim_id
            WHERE pmcs.claim_id IS NOT NULL AND (pmc.id IS NULL OR pmc.bank_id <> pmcs.bank_id)
            LIMIT 1
            """,
        ),
        (
            "memory_peer_roles memory",
            f"""
            SELECT mpr.id
            FROM {s}memory_peer_roles mpr
            LEFT JOIN {s}memory_units mu ON mu.id = mpr.memory_unit_id
            WHERE mpr.memory_unit_id IS NOT NULL AND (mu.id IS NULL OR mu.bank_id <> mpr.bank_id)
            LIMIT 1
            """,
        ),
        (
            "memory_peer_roles peer",
            f"""
            SELECT mpr.id
            FROM {s}memory_peer_roles mpr
            LEFT JOIN {s}peers p ON p.id = mpr.peer_id
            WHERE mpr.peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> mpr.bank_id)
            LIMIT 1
            """,
        ),
    )
    for label, query in checks:
        conn = op.get_bind()
        bad = conn.execute(text(query)).first()
        if bad is not None:
            raise RuntimeError(
                f"Peer bank-scope preflight failed: cross-bank/missing {label} row id={bad[0]}; "
                "repair or remove the row before upgrading"
            )


def _pg_upgrade() -> None:
    s = _pg_schema_prefix()

    _pg_preflight()

    # Composite parent keys let every relationship carry the tenant boundary
    # into the database constraint rather than relying only on application SQL.
    op.execute(f"ALTER TABLE {s}memory_units ADD CONSTRAINT uq_memory_units_bank_id UNIQUE (bank_id, id)")
    op.execute(f"ALTER TABLE {s}peer_models ADD CONSTRAINT uq_peer_models_bank_id UNIQUE (bank_id, id)")
    op.execute(f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT uq_peer_model_claims_bank_id UNIQUE (bank_id, id)")

    for table, constraint in (
        ("peer_models", "peer_models_observer_peer_id_fkey"),
        ("peer_models", "peer_models_target_peer_id_fkey"),
        ("peer_model_claims", "peer_model_claims_model_id_fkey"),
        ("peer_model_claims", "peer_model_claims_observer_peer_id_fkey"),
        ("peer_model_claims", "peer_model_claims_target_peer_id_fkey"),
        ("peer_model_claim_sources", "peer_model_claim_sources_claim_id_fkey"),
        ("memory_peer_roles", "memory_peer_roles_memory_unit_id_fkey"),
        ("memory_peer_roles", "memory_peer_roles_peer_id_fkey"),
    ):
        op.execute(f"ALTER TABLE {s}{table} DROP CONSTRAINT {constraint}")

    op.execute(
        f"ALTER TABLE {s}peer_models ADD CONSTRAINT fk_pm_obs_bank "
        f"FOREIGN KEY (bank_id, observer_peer_id) REFERENCES {s}peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_models ADD CONSTRAINT fk_pm_tgt_bank "
        f"FOREIGN KEY (bank_id, target_peer_id) REFERENCES {s}peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT fk_pmc_model_bank "
        f"FOREIGN KEY (bank_id, model_id) REFERENCES {s}peer_models(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT fk_pmc_obs_bank "
        f"FOREIGN KEY (bank_id, observer_peer_id) REFERENCES {s}peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT fk_pmc_tgt_bank "
        f"FOREIGN KEY (bank_id, target_peer_id) REFERENCES {s}peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claim_sources ADD CONSTRAINT fk_pmcs_claim_bank "
        f"FOREIGN KEY (bank_id, claim_id) REFERENCES {s}peer_model_claims(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}memory_peer_roles ADD CONSTRAINT fk_mpr_memory_bank "
        f"FOREIGN KEY (bank_id, memory_unit_id) REFERENCES {s}memory_units(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}memory_peer_roles ADD CONSTRAINT fk_mpr_peer_bank "
        f"FOREIGN KEY (bank_id, peer_id) REFERENCES {s}peers(bank_id, id) ON DELETE CASCADE"
    )


def _pg_downgrade() -> None:
    s = _pg_schema_prefix()

    for table, constraint in (
        ("peer_models", "fk_pm_obs_bank"),
        ("peer_models", "fk_pm_tgt_bank"),
        ("peer_model_claims", "fk_pmc_model_bank"),
        ("peer_model_claims", "fk_pmc_obs_bank"),
        ("peer_model_claims", "fk_pmc_tgt_bank"),
        ("peer_model_claim_sources", "fk_pmcs_claim_bank"),
        ("memory_peer_roles", "fk_mpr_memory_bank"),
        ("memory_peer_roles", "fk_mpr_peer_bank"),
    ):
        op.execute(f"ALTER TABLE {s}{table} DROP CONSTRAINT {constraint}")

    op.execute(
        f"ALTER TABLE {s}peer_models ADD CONSTRAINT peer_models_observer_peer_id_fkey "
        f"FOREIGN KEY (observer_peer_id) REFERENCES {s}peers(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_models ADD CONSTRAINT peer_models_target_peer_id_fkey "
        f"FOREIGN KEY (target_peer_id) REFERENCES {s}peers(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT peer_model_claims_model_id_fkey "
        f"FOREIGN KEY (model_id) REFERENCES {s}peer_models(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT peer_model_claims_observer_peer_id_fkey "
        f"FOREIGN KEY (observer_peer_id) REFERENCES {s}peers(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claims ADD CONSTRAINT peer_model_claims_target_peer_id_fkey "
        f"FOREIGN KEY (target_peer_id) REFERENCES {s}peers(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}peer_model_claim_sources ADD CONSTRAINT peer_model_claim_sources_claim_id_fkey "
        f"FOREIGN KEY (claim_id) REFERENCES {s}peer_model_claims(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}memory_peer_roles ADD CONSTRAINT memory_peer_roles_memory_unit_id_fkey "
        f"FOREIGN KEY (memory_unit_id) REFERENCES {s}memory_units(id) ON DELETE CASCADE"
    )
    op.execute(
        f"ALTER TABLE {s}memory_peer_roles ADD CONSTRAINT memory_peer_roles_peer_id_fkey "
        f"FOREIGN KEY (peer_id) REFERENCES {s}peers(id) ON DELETE CASCADE"
    )

    op.execute(f"ALTER TABLE {s}peer_model_claims DROP CONSTRAINT uq_peer_model_claims_bank_id")
    op.execute(f"ALTER TABLE {s}peer_models DROP CONSTRAINT uq_peer_models_bank_id")
    op.execute(f"ALTER TABLE {s}memory_units DROP CONSTRAINT uq_memory_units_bank_id")


def _oracle_preflight() -> None:
    """Fail before the first Oracle DDL statement.

    Oracle has no transactional DDL: every ALTER commits immediately. The
    preflight therefore runs first, before any constraint is added or dropped,
    so an invalid cross-bank row can never leave the schema half-migrated.
    """
    checks = (
        (
            "peer_models observer",
            """
            SELECT pm.id
            FROM peer_models pm
            LEFT JOIN peers p ON p.id = pm.observer_peer_id
            WHERE pm.observer_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pm.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "peer_models target",
            """
            SELECT pm.id
            FROM peer_models pm
            LEFT JOIN peers p ON p.id = pm.target_peer_id
            WHERE pm.target_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pm.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "peer_model_claims model",
            """
            SELECT pmc.id
            FROM peer_model_claims pmc
            LEFT JOIN peer_models pm ON pm.id = pmc.model_id
            WHERE pmc.model_id IS NOT NULL AND (pm.id IS NULL OR pm.bank_id <> pmc.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "peer_model_claims observer",
            """
            SELECT pmc.id
            FROM peer_model_claims pmc
            LEFT JOIN peers p ON p.id = pmc.observer_peer_id
            WHERE pmc.observer_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pmc.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "peer_model_claims target",
            """
            SELECT pmc.id
            FROM peer_model_claims pmc
            LEFT JOIN peers p ON p.id = pmc.target_peer_id
            WHERE pmc.target_peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> pmc.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "peer_model_claim_sources claim",
            """
            SELECT pmcs.claim_id, pmcs.source_id
            FROM peer_model_claim_sources pmcs
            LEFT JOIN peer_model_claims pmc ON pmc.id = pmcs.claim_id
            WHERE pmcs.claim_id IS NOT NULL AND (pmc.id IS NULL OR pmc.bank_id <> pmcs.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "memory_peer_roles memory",
            """
            SELECT mpr.id
            FROM memory_peer_roles mpr
            LEFT JOIN memory_units mu ON mu.id = mpr.memory_unit_id
            WHERE mpr.memory_unit_id IS NOT NULL AND (mu.id IS NULL OR mu.bank_id <> mpr.bank_id)
            AND ROWNUM = 1
            """,
        ),
        (
            "memory_peer_roles peer",
            """
            SELECT mpr.id
            FROM memory_peer_roles mpr
            LEFT JOIN peers p ON p.id = mpr.peer_id
            WHERE mpr.peer_id IS NOT NULL AND (p.id IS NULL OR p.bank_id <> mpr.bank_id)
            AND ROWNUM = 1
            """,
        ),
    )
    for label, query in checks:
        conn = op.get_bind()
        bad = conn.execute(text(query)).first()
        if bad is not None:
            raise RuntimeError(
                f"Peer bank-scope preflight failed: cross-bank/missing {label} row id={bad[0]}; "
                "repair or remove the row before upgrading"
            )


def _oracle_upgrade() -> None:
    _oracle_preflight()

    op.execute("ALTER TABLE memory_units ADD CONSTRAINT uq_mu_bank_id UNIQUE (bank_id, id)")
    op.execute("ALTER TABLE peer_models ADD CONSTRAINT uq_pm_bank_id UNIQUE (bank_id, id)")
    op.execute("ALTER TABLE peer_model_claims ADD CONSTRAINT uq_pmc_bank_id UNIQUE (bank_id, id)")

    for table, constraint in (
        ("peer_models", "fk_pm_observer"),
        ("peer_models", "fk_pm_target"),
        ("peer_model_claims", "fk_pmc_model"),
        ("peer_model_claim_sources", "fk_pmcs_claim"),
        ("memory_peer_roles", "fk_mpr_memory"),
        ("memory_peer_roles", "fk_mpr_peer"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")

    # Oracle e8 omitted direct bank FKs on these two tables; add them for
    # PostgreSQL/Oracle schema parity.
    op.execute(
        "ALTER TABLE peer_model_claim_sources ADD CONSTRAINT fk_pmcs_bank "
        "FOREIGN KEY (bank_id) REFERENCES banks(bank_id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory_peer_roles ADD CONSTRAINT fk_mpr_bank "
        "FOREIGN KEY (bank_id) REFERENCES banks(bank_id) ON DELETE CASCADE"
    )

    op.execute(
        "ALTER TABLE peer_models ADD CONSTRAINT fk_pm_obs_bank "
        "FOREIGN KEY (bank_id, observer_peer_id) REFERENCES peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_models ADD CONSTRAINT fk_pm_tgt_bank "
        "FOREIGN KEY (bank_id, target_peer_id) REFERENCES peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_model_claims ADD CONSTRAINT fk_pmc_model_bank "
        "FOREIGN KEY (bank_id, model_id) REFERENCES peer_models(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_model_claims ADD CONSTRAINT fk_pmc_obs_bank "
        "FOREIGN KEY (bank_id, observer_peer_id) REFERENCES peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_model_claims ADD CONSTRAINT fk_pmc_tgt_bank "
        "FOREIGN KEY (bank_id, target_peer_id) REFERENCES peers(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_model_claim_sources ADD CONSTRAINT fk_pmcs_claim_bank "
        "FOREIGN KEY (bank_id, claim_id) REFERENCES peer_model_claims(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory_peer_roles ADD CONSTRAINT fk_mpr_memory_bank "
        "FOREIGN KEY (bank_id, memory_unit_id) REFERENCES memory_units(bank_id, id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory_peer_roles ADD CONSTRAINT fk_mpr_peer_bank "
        "FOREIGN KEY (bank_id, peer_id) REFERENCES peers(bank_id, id) ON DELETE CASCADE"
    )


def _oracle_downgrade() -> None:
    for table, constraint in (
        ("peer_models", "fk_pm_obs_bank"),
        ("peer_models", "fk_pm_tgt_bank"),
        ("peer_model_claims", "fk_pmc_model_bank"),
        ("peer_model_claims", "fk_pmc_obs_bank"),
        ("peer_model_claims", "fk_pmc_tgt_bank"),
        ("peer_model_claim_sources", "fk_pmcs_claim_bank"),
        ("memory_peer_roles", "fk_mpr_memory_bank"),
        ("memory_peer_roles", "fk_mpr_peer_bank"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")

    op.execute("ALTER TABLE peer_model_claim_sources DROP CONSTRAINT fk_pmcs_bank")
    op.execute("ALTER TABLE memory_peer_roles DROP CONSTRAINT fk_mpr_bank")

    op.execute(
        "ALTER TABLE peer_models ADD CONSTRAINT fk_pm_observer "
        "FOREIGN KEY (observer_peer_id) REFERENCES peers(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_models ADD CONSTRAINT fk_pm_target "
        "FOREIGN KEY (target_peer_id) REFERENCES peers(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_model_claims ADD CONSTRAINT fk_pmc_model "
        "FOREIGN KEY (model_id) REFERENCES peer_models(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE peer_model_claim_sources ADD CONSTRAINT fk_pmcs_claim "
        "FOREIGN KEY (claim_id) REFERENCES peer_model_claims(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory_peer_roles ADD CONSTRAINT fk_mpr_memory "
        "FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE memory_peer_roles ADD CONSTRAINT fk_mpr_peer "
        "FOREIGN KEY (peer_id) REFERENCES peers(id) ON DELETE CASCADE"
    )

    op.execute("ALTER TABLE peer_model_claims DROP CONSTRAINT uq_pmc_bank_id")
    op.execute("ALTER TABLE peer_models DROP CONSTRAINT uq_pm_bank_id")
    op.execute("ALTER TABLE memory_units DROP CONSTRAINT uq_mu_bank_id")


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_downgrade)
