import uuid

import asyncpg
import pytest


async def _expect_fk_violation(conn: asyncpg.Connection, sql: str, *args: object) -> None:
    savepoint = conn.transaction()
    await savepoint.start()
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(sql, *args)
    finally:
        await savepoint.rollback()


@pytest.mark.asyncio
async def test_peer_relationships_reject_cross_bank_references(pg0_db_url: str) -> None:
    bank_a = f"peer-fk-a-{uuid.uuid4().hex}"
    bank_b = f"peer-fk-b-{uuid.uuid4().hex}"
    peer_a_observer = uuid.uuid4()
    peer_a_target = uuid.uuid4()
    peer_b = uuid.uuid4()
    model_a = uuid.uuid4()
    claim_a = uuid.uuid4()
    memory_a = uuid.uuid4()

    conn = await asyncpg.connect(pg0_db_url)
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.executemany("INSERT INTO banks (bank_id) VALUES ($1)", [(bank_a,), (bank_b,)])
        await conn.executemany(
            "INSERT INTO peers (id, bank_id, external_id) VALUES ($1, $2, $3)",
            [
                (peer_a_observer, bank_a, "observer-a"),
                (peer_a_target, bank_a, "target-a"),
                (peer_b, bank_b, "peer-b"),
            ],
        )
        await conn.execute(
            """
            INSERT INTO peer_models (id, bank_id, observer_peer_id, target_peer_id)
            VALUES ($1, $2, $3, $4)
            """,
            model_a,
            bank_a,
            peer_a_observer,
            peer_a_target,
        )
        await conn.execute(
            """
            INSERT INTO peer_model_claims
                (id, bank_id, model_id, observer_peer_id, target_peer_id,
                 claim_type, text, origin, confidence)
            VALUES ($1, $2, $3, $4, $5, 'IDENTITY', 'claim-a', 'manual', 1.0)
            """,
            claim_a,
            bank_a,
            model_a,
            peer_a_observer,
            peer_a_target,
        )
        await conn.execute(
            "INSERT INTO memory_units (id, bank_id, text) VALUES ($1, $2, 'memory-a')",
            memory_a,
            bank_a,
        )

        await _expect_fk_violation(
            conn,
            """
            INSERT INTO peer_models (id, bank_id, observer_peer_id, target_peer_id)
            VALUES ($1, $2, $3, $4)
            """,
            uuid.uuid4(),
            bank_a,
            peer_b,
            peer_a_target,
        )
        await _expect_fk_violation(
            conn,
            """
            INSERT INTO peer_model_claims
                (id, bank_id, model_id, observer_peer_id, target_peer_id,
                 claim_type, text, origin, confidence)
            VALUES ($1, $2, $3, $4, $5, 'IDENTITY', 'cross-bank', 'manual', 1.0)
            """,
            uuid.uuid4(),
            bank_a,
            model_a,
            peer_b,
            peer_a_target,
        )
        await _expect_fk_violation(
            conn,
            """
            INSERT INTO peer_model_claim_sources (bank_id, claim_id, source_kind, source_id)
            VALUES ($1, $2, 'memory_unit', $3)
            """,
            bank_b,
            claim_a,
            str(memory_a),
        )
        await _expect_fk_violation(
            conn,
            """
            INSERT INTO memory_peer_roles (id, bank_id, memory_unit_id, peer_id, role)
            VALUES ($1, $2, $3, $4, 'observer')
            """,
            uuid.uuid4(),
            bank_b,
            memory_a,
            peer_b,
        )
        await _expect_fk_violation(
            conn,
            """
            INSERT INTO memory_peer_roles (id, bank_id, memory_unit_id, peer_id, role)
            VALUES ($1, $2, $3, $4, 'observer')
            """,
            uuid.uuid4(),
            bank_a,
            memory_a,
            peer_b,
        )
    finally:
        await transaction.rollback()
        await conn.close()
