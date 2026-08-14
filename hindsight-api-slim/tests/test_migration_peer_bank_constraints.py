import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.xdist_group("migration-peer-bank-fk-pg0")

_SCRIPT_LOCATION = str(Path(__file__).parent.parent / "hindsight_api" / "alembic")
_PRE_HARDENING_REVISION = "b7e3c1a9d4f2"
_HARDENING_REVISION = "c8f4d2a1e6b9"


def _alembic_cfg(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _SCRIPT_LOCATION)
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("prepend_sys_path", ".")
    cfg.set_main_option("path_separator", "os")
    return cfg


def _constraint_names(db_url: str) -> set[str]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT conname
                        FROM pg_constraint
                        WHERE connamespace = 'public'::regnamespace
                        """
                    )
                )
            }
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def head_db_url() -> str:
    from hindsight_api.pg0 import EmbeddedPostgres

    pg0 = EmbeddedPostgres(name=f"hindsight-peer-bank-fk-{uuid.uuid4().hex}", port=None)
    loop = asyncio.new_event_loop()
    try:
        url = loop.run_until_complete(pg0.ensure_running())
    finally:
        loop.close()
    command.upgrade(_alembic_cfg(url), "heads")
    return url


def test_peer_bank_fk_migration_downgrades_and_reupgrades(head_db_url: str) -> None:
    cfg = _alembic_cfg(head_db_url)

    at_head = _constraint_names(head_db_url)
    assert {"uq_memory_units_bank_id", "fk_pm_obs_bank", "fk_pmcs_claim_bank", "fk_mpr_memory_bank"} <= at_head

    command.downgrade(cfg, _PRE_HARDENING_REVISION)
    downgraded = _constraint_names(head_db_url)
    assert "uq_memory_units_bank_id" not in downgraded
    assert "fk_pm_obs_bank" not in downgraded
    assert {
        "peer_models_observer_peer_id_fkey",
        "peer_model_claim_sources_claim_id_fkey",
        "memory_peer_roles_memory_unit_id_fkey",
    } <= downgraded

    command.upgrade(cfg, _HARDENING_REVISION)
    reupgraded = _constraint_names(head_db_url)
    assert {"uq_memory_units_bank_id", "fk_pm_obs_bank", "fk_pmcs_claim_bank", "fk_mpr_memory_bank"} <= reupgraded


def test_peer_bank_fk_preflight_fails_on_cross_bank_data_before_any_ddl(head_db_url: str) -> None:
    """Cross-bank rows must abort the upgrade before the first DDL statement.

    Oracle commits each DDL statement implicitly; a late ADD CONSTRAINT failure
    would leave the schema half-migrated. The preflight must therefore be the
    first thing that runs and must fail without dropping the old constraints.
    """
    import asyncio

    import asyncpg

    cfg = _alembic_cfg(head_db_url)
    command.downgrade(cfg, _PRE_HARDENING_REVISION)

    bank_a = f"preflight-a-{uuid.uuid4().hex}"
    bank_b = f"preflight-b-{uuid.uuid4().hex}"
    peer_a = uuid.uuid4()
    peer_b = uuid.uuid4()
    model_a = uuid.uuid4()
    claim_a = uuid.uuid4()
    memory_a = uuid.uuid4()

    async def seed() -> None:
        conn = await asyncpg.connect(head_db_url)
        try:
            await conn.executemany("INSERT INTO banks (bank_id) VALUES ($1)", [(bank_a,), (bank_b,)])
            await conn.executemany(
                "INSERT INTO peers (id, bank_id, external_id) VALUES ($1, $2, $3)",
                [
                    (peer_a, bank_a, "peer-a"),
                    (peer_b, bank_b, "peer-b"),
                ],
            )
            await conn.execute(
                "INSERT INTO memory_units (id, bank_id, text) VALUES ($1, $2, 'memory-a')",
                memory_a,
                bank_a,
            )
            # Same-bank model but a cross-bank observer reference: this is the
            # exact corruption the hardening migration must reject up front.
            await conn.execute(
                """
                INSERT INTO peer_models (id, bank_id, observer_peer_id, target_peer_id)
                VALUES ($1, $2, $3, $4)
                """,
                model_a,
                bank_a,
                peer_b,
                peer_a,
            )
            await conn.execute(
                """
                INSERT INTO peer_model_claims
                    (id, bank_id, model_id, observer_peer_id, target_peer_id,
                     claim_type, text, origin, confidence)
                VALUES ($1, $2, $3, $4, $5, 'IDENTITY', 'claim', 'manual', 1.0)
                """,
                claim_a,
                bank_a,
                model_a,
                peer_b,
                peer_a,
            )
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(seed())
    finally:
        loop.close()

    with pytest.raises(RuntimeError, match="Peer bank-scope preflight failed"):
        command.upgrade(cfg, _HARDENING_REVISION)

    # No DDL was applied: the old single-column FKs and revision must survive.
    untouched = _constraint_names(head_db_url)
    assert "peer_models_observer_peer_id_fkey" in untouched
    assert "fk_pm_obs_bank" not in untouched
    assert "uq_memory_units_bank_id" not in untouched

    # The upgrade must be able to retry cleanly after the bad row is removed.
    async def remove_bad_rows() -> None:
        conn = await asyncpg.connect(head_db_url)
        try:
            await conn.execute("DELETE FROM peer_model_claims WHERE bank_id = $1", bank_a)
            await conn.execute("DELETE FROM peer_models WHERE bank_id = $1", bank_a)
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(remove_bad_rows())
    finally:
        loop.close()
    command.upgrade(cfg, _HARDENING_REVISION)
    assert {"uq_memory_units_bank_id", "fk_pm_obs_bank", "fk_pmcs_claim_bank", "fk_mpr_memory_bank"} <= (
        _constraint_names(head_db_url)
    )
