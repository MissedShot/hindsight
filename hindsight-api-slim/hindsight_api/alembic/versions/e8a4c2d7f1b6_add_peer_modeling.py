"""Add native bank-scoped peer modeling tables.

Revision ID: e8a4c2d7f1b6
Revises: c7d1e9a4b3f2
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import context, op

from hindsight_api.alembic._dialect import run_for_dialect

revision: str = "e8a4c2d7f1b6"
down_revision: str | Sequence[str] | None = "c7d1e9a4b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _pg_schema_prefix() -> str:
    schema = context.config.get_main_option("target_schema")
    return f'"{schema}".' if schema else ""


def _pg_upgrade() -> None:
    s = _pg_schema_prefix()
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {s}peers (
            id UUID PRIMARY KEY,
            bank_id TEXT NOT NULL REFERENCES {s}banks(bank_id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,
            display_name TEXT,
            kind TEXT NOT NULL DEFAULT 'person',
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_peers_bank_external UNIQUE (bank_id, external_id),
            CONSTRAINT uq_peers_bank_id UNIQUE (bank_id, id)
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS idx_peers_bank_created ON {s}peers (bank_id, created_at)")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {s}peer_models (
            id UUID PRIMARY KEY,
            bank_id TEXT NOT NULL REFERENCES {s}banks(bank_id) ON DELETE CASCADE,
            observer_peer_id UUID NOT NULL REFERENCES {s}peers(id) ON DELETE CASCADE,
            target_peer_id UUID NOT NULL REFERENCES {s}peers(id) ON DELETE CASCADE,
            version INTEGER NOT NULL DEFAULT 1,
            card JSONB NOT NULL DEFAULT '[]'::jsonb,
            representation TEXT NOT NULL DEFAULT '',
            source_cursor TIMESTAMPTZ,
            generation_status TEXT NOT NULL DEFAULT 'ready',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_peer_models_direction UNIQUE (bank_id, observer_peer_id, target_peer_id)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {s}peer_model_claims (
            id UUID PRIMARY KEY,
            bank_id TEXT NOT NULL REFERENCES {s}banks(bank_id) ON DELETE CASCADE,
            model_id UUID NOT NULL REFERENCES {s}peer_models(id) ON DELETE CASCADE,
            observer_peer_id UUID NOT NULL REFERENCES {s}peers(id) ON DELETE CASCADE,
            target_peer_id UUID NOT NULL REFERENCES {s}peers(id) ON DELETE CASCADE,
            claim_type TEXT NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            origin TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            locked BOOLEAN NOT NULL DEFAULT FALSE,
            provenance TEXT,
            valid_from TIMESTAMPTZ,
            valid_until TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_peer_claim_confidence CHECK (confidence >= 0 AND confidence <= 1)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_peer_claims_direction "
        f"ON {s}peer_model_claims (bank_id, observer_peer_id, target_peer_id, status)"
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {s}peer_model_claim_sources (
            bank_id TEXT NOT NULL REFERENCES {s}banks(bank_id) ON DELETE CASCADE,
            claim_id UUID NOT NULL REFERENCES {s}peer_model_claims(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (bank_id, claim_id, source_kind, source_id)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {s}memory_peer_roles (
            id UUID PRIMARY KEY,
            bank_id TEXT NOT NULL REFERENCES {s}banks(bank_id) ON DELETE CASCADE,
            memory_unit_id UUID NOT NULL REFERENCES {s}memory_units(id) ON DELETE CASCADE,
            peer_id UUID NOT NULL REFERENCES {s}peers(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            explicit BOOLEAN NOT NULL DEFAULT TRUE,
            modality TEXT NOT NULL DEFAULT 'actual',
            source_message_id TEXT,
            session_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_memory_peer_role UNIQUE (bank_id, memory_unit_id, peer_id, role)
        )
        """
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS idx_memory_peer_roles_peer "
        f"ON {s}memory_peer_roles (bank_id, peer_id, role, created_at)"
    )


def _pg_downgrade() -> None:
    s = _pg_schema_prefix()
    for table in (
        "memory_peer_roles",
        "peer_model_claim_sources",
        "peer_model_claims",
        "peer_models",
        "peers",
    ):
        op.execute(f"DROP TABLE IF EXISTS {s}{table} CASCADE")


def _oracle_create(sql: str) -> None:
    escaped = sql.replace("'", "''")
    op.execute(
        f"BEGIN EXECUTE IMMEDIATE '{escaped}'; EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF; END;"
    )


def _oracle_drop(table: str) -> None:
    op.execute(
        f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {table} CASCADE CONSTRAINTS'; "
        "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
    )


def _oracle_upgrade() -> None:
    _oracle_create(
        "CREATE TABLE peers ("
        "id RAW(16) PRIMARY KEY, bank_id VARCHAR2(4000) NOT NULL, external_id VARCHAR2(512) NOT NULL, "
        "display_name VARCHAR2(512), kind VARCHAR2(64) DEFAULT 'person' NOT NULL, metadata CLOB DEFAULT '{}' NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "CONSTRAINT uq_peers_bank_external UNIQUE (bank_id, external_id), "
        "CONSTRAINT uq_peers_bank_id UNIQUE (bank_id, id), "
        "CONSTRAINT fk_peers_bank FOREIGN KEY (bank_id) REFERENCES banks(bank_id) ON DELETE CASCADE)"
    )
    _oracle_create(
        "CREATE TABLE peer_models ("
        "id RAW(16) PRIMARY KEY, bank_id VARCHAR2(4000) NOT NULL, observer_peer_id RAW(16) NOT NULL, "
        "target_peer_id RAW(16) NOT NULL, version NUMBER(10) DEFAULT 1 NOT NULL, card CLOB DEFAULT '[]' NOT NULL, "
        "representation CLOB DEFAULT '' NOT NULL, source_cursor TIMESTAMP WITH TIME ZONE, "
        "generation_status VARCHAR2(32) DEFAULT 'ready' NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "CONSTRAINT uq_peer_models_direction UNIQUE (bank_id, observer_peer_id, target_peer_id), "
        "CONSTRAINT fk_pm_bank FOREIGN KEY (bank_id) REFERENCES banks(bank_id) ON DELETE CASCADE, "
        "CONSTRAINT fk_pm_observer FOREIGN KEY (observer_peer_id) REFERENCES peers(id) ON DELETE CASCADE, "
        "CONSTRAINT fk_pm_target FOREIGN KEY (target_peer_id) REFERENCES peers(id) ON DELETE CASCADE)"
    )
    _oracle_create(
        "CREATE TABLE peer_model_claims ("
        "id RAW(16) PRIMARY KEY, bank_id VARCHAR2(4000) NOT NULL, model_id RAW(16) NOT NULL, "
        "observer_peer_id RAW(16) NOT NULL, target_peer_id RAW(16) NOT NULL, claim_type VARCHAR2(32) NOT NULL, "
        "text CLOB NOT NULL, status VARCHAR2(32) DEFAULT 'active' NOT NULL, origin VARCHAR2(32) NOT NULL, "
        "confidence BINARY_DOUBLE NOT NULL, locked NUMBER(1) DEFAULT 0 NOT NULL, provenance CLOB, "
        "valid_from TIMESTAMP WITH TIME ZONE, valid_until TIMESTAMP WITH TIME ZONE, "
        "created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "CONSTRAINT fk_pmc_bank FOREIGN KEY (bank_id) REFERENCES banks(bank_id) ON DELETE CASCADE, "
        "CONSTRAINT fk_pmc_model FOREIGN KEY (model_id) REFERENCES peer_models(id) ON DELETE CASCADE)"
    )
    _oracle_create(
        "CREATE TABLE peer_model_claim_sources ("
        "bank_id VARCHAR2(4000) NOT NULL, claim_id RAW(16) NOT NULL, source_kind VARCHAR2(32) NOT NULL, "
        "source_id VARCHAR2(1024) NOT NULL, created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "CONSTRAINT pk_peer_claim_sources PRIMARY KEY (bank_id, claim_id, source_kind, source_id), "
        "CONSTRAINT fk_pmcs_claim FOREIGN KEY (claim_id) REFERENCES peer_model_claims(id) ON DELETE CASCADE)"
    )
    _oracle_create(
        "CREATE TABLE memory_peer_roles ("
        "id RAW(16) PRIMARY KEY, bank_id VARCHAR2(4000) NOT NULL, memory_unit_id RAW(16) NOT NULL, "
        "peer_id RAW(16) NOT NULL, role VARCHAR2(32) NOT NULL, explicit NUMBER(1) DEFAULT 1 NOT NULL, "
        "modality VARCHAR2(32) DEFAULT 'actual' NOT NULL, source_message_id VARCHAR2(1024), "
        "session_id VARCHAR2(1024), created_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL, "
        "CONSTRAINT uq_memory_peer_role UNIQUE (bank_id, memory_unit_id, peer_id, role), "
        "CONSTRAINT fk_mpr_memory FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE, "
        "CONSTRAINT fk_mpr_peer FOREIGN KEY (peer_id) REFERENCES peers(id) ON DELETE CASCADE)"
    )


def _oracle_downgrade() -> None:
    for table in (
        "memory_peer_roles",
        "peer_model_claim_sources",
        "peer_model_claims",
        "peer_models",
        "peers",
    ):
        _oracle_drop(table)


def upgrade() -> None:
    run_for_dialect(pg=_pg_upgrade, oracle=_oracle_upgrade)


def downgrade() -> None:
    run_for_dialect(pg=_pg_downgrade, oracle=_oracle_downgrade)
