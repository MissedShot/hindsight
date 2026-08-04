"""Database repository for the peer-modeling vertical slice.

The repository owns all SQL and always scopes every read/write by bank_id. It uses the
existing DatabaseBackend/DatabaseConnection abstraction so PostgreSQL and Oracle share
the same query shape and driver rewriting.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from typing import Any

from hindsight_api.engine.db import DatabaseBackend, DatabaseConnection
from hindsight_api.engine.schema import fq_table

from .errors import PeerNotFoundError
from .models import (
    Peer,
    PeerCardEntry,
    PeerClaim,
    PeerClaimOrigin,
    PeerClaimStatus,
    PeerClaimType,
    PeerList,
    PeerMaterializationPlan,
    PeerMaterializationResult,
    PeerModel,
    PeerSource,
    PeerSourceKind,
)


def _json_object(connection: DatabaseConnection, value: Any) -> dict[str, Any]:
    """Normalize a dynamic JSON metadata column without treating known fields as a dict."""
    parsed = connection.parse_json(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_list(connection: DatabaseConnection, value: Any) -> list[Any]:
    """Normalize a JSON array before validating each known card entry."""
    parsed = connection.parse_json(value)
    return parsed if isinstance(parsed, list) else []


def _uuid_value(value: str) -> uuid.UUID:
    """Bind UUID columns consistently for both asyncpg and Oracle RAW(16)."""
    return uuid.UUID(str(value))


class PeerRepository:
    """Persistence operations for bank-scoped peers and directional models."""

    def __init__(self, backend: DatabaseBackend):
        self._backend = backend

    async def create_peer(
        self,
        *,
        bank_id: str,
        peer_id: str,
        external_id: str,
        display_name: str | None,
        kind: str,
        metadata: dict[str, Any],
    ) -> Peer:
        async with self._backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table('peers')}
                    (id, bank_id, external_id, display_name, kind, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                _uuid_value(peer_id),
                bank_id,
                external_id,
                display_name,
                kind,
                json.dumps(metadata),
            )
            row = await conn.fetchrow(
                f"""
                SELECT id, bank_id, external_id, display_name, kind, metadata, created_at, updated_at
                FROM {fq_table('peers')}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(peer_id),
            )
            if row is None:
                raise PeerNotFoundError(f"Peer '{peer_id}' was not created")
            return self._peer_from_row(conn, row)

    async def list_peers(self, *, bank_id: str, limit: int, offset: int) -> PeerList:
        async with self._backend.acquire() as conn:
            total = int(
                await conn.fetchval(
                    f"SELECT COUNT(*) FROM {fq_table('peers')} WHERE bank_id = $1",
                    bank_id,
                )
                or 0
            )
            rows = await conn.fetch(
                f"""
                SELECT id, bank_id, external_id, display_name, kind, metadata, created_at, updated_at
                FROM {fq_table('peers')}
                WHERE bank_id = $1
                ORDER BY created_at ASC, id ASC
                LIMIT $2 OFFSET $3
                """,
                bank_id,
                limit,
                offset,
            )
            return PeerList(
                items=[self._peer_from_row(conn, row) for row in rows],
                total=total,
                limit=limit,
                offset=offset,
            )

    async def get_peer(self, *, bank_id: str, peer_id: str) -> Peer | None:
        async with self._backend.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT id, bank_id, external_id, display_name, kind, metadata, created_at, updated_at
                FROM {fq_table('peers')}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(peer_id),
            )
            return self._peer_from_row(conn, row) if row else None

    async def update_peer(
        self,
        *,
        bank_id: str,
        peer_id: str,
        display_name: str | None,
        kind: str | None,
        metadata: dict[str, Any] | None,
    ) -> Peer | None:
        async with self._backend.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT id FROM {fq_table('peers')} WHERE bank_id = $1 AND id = $2",
                bank_id,
                _uuid_value(peer_id),
            )
            if row is None:
                return None
            await conn.execute(
                f"""
                UPDATE {fq_table('peers')}
                SET display_name = COALESCE($3, display_name),
                    kind = COALESCE($4, kind),
                    metadata = COALESCE($5::jsonb, metadata),
                    updated_at = NOW()
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(peer_id),
                display_name,
                kind,
                json.dumps(metadata) if metadata is not None else None,
            )
            updated = await conn.fetchrow(
                f"""
                SELECT id, bank_id, external_id, display_name, kind, metadata, created_at, updated_at
                FROM {fq_table('peers')}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(peer_id),
            )
            return self._peer_from_row(conn, updated) if updated else None

    async def peer_pair_exists(self, *, bank_id: str, observer_peer_id: str, target_peer_id: str) -> bool:
        async with self._backend.acquire() as conn:
            count = await conn.fetchval(
                f"""
                SELECT COUNT(*) FROM {fq_table('peers')}
                WHERE bank_id = $1 AND id IN ($2, $3)
                """,
                bank_id,
                _uuid_value(observer_peer_id),
                _uuid_value(target_peer_id),
            )
            return int(count or 0) == (1 if observer_peer_id == target_peer_id else 2)

    async def memory_sources_exist(self, *, bank_id: str, source_ids: list[str]) -> bool:
        """Verify every memory-unit source belongs to this bank before linking it."""
        if not source_ids:
            return True
        async with self._backend.acquire() as conn:
            for source_id in source_ids:
                found = await conn.fetchval(
                    f"""
                    SELECT 1 FROM {fq_table('memory_units')}
                    WHERE bank_id = $1 AND id = $2::uuid
                    """,
                    bank_id,
                    _uuid_value(source_id),
                )
                if found is None:
                    return False
        return True

    async def get_directional_model(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
    ) -> PeerModel | None:
        async with self._backend.acquire() as conn:
            row = await self._model_row(conn, bank_id, observer_peer_id, target_peer_id)
            if row is None:
                return None
            card_entries = [PeerCardEntry.model_validate(item) for item in _json_list(conn, row["card"])]
            card = self._card_from_row(row, card_entries)
            return PeerModel(
                id=str(row["id"]),
                bank_id=str(row["bank_id"]),
                observer_peer_id=str(row["observer_peer_id"]),
                target_peer_id=str(row["target_peer_id"]),
                version=int(row["version"]),
                card=card,
                representation=str(row["representation"] or ""),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    async def get_directional_claims(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
    ) -> list[PeerClaim] | None:
        async with self._backend.acquire() as conn:
            row = await self._model_row(conn, bank_id, observer_peer_id, target_peer_id)
            if row is None:
                return None
            return await self._claims_for_model(conn, bank_id=bank_id, model_id=str(row["id"]))

    async def apply_materialization(self, plan: PeerMaterializationPlan) -> PeerMaterializationResult:
        """Apply claims, source links, card, and representation in one transaction."""
        claims_added = 0
        card_json = json.dumps([entry.model_dump(mode="json") for entry in plan.card_entries])
        async with self._backend.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"""
                    INSERT INTO {fq_table('peer_models')}
                        (id, bank_id, observer_peer_id, target_peer_id, version, card, representation)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                    ON CONFLICT (bank_id, observer_peer_id, target_peer_id) DO UPDATE SET
                        version = EXCLUDED.version,
                        card = EXCLUDED.card,
                        representation = EXCLUDED.representation,
                        updated_at = NOW()
                    """,
                    _uuid_value(plan.model_id),
                    plan.bank_id,
                    _uuid_value(plan.observer_peer_id),
                    _uuid_value(plan.target_peer_id),
                    plan.version,
                    card_json,
                    plan.representation,
                )
                if plan.rebuild:
                    await conn.execute(
                        f"""
                        UPDATE {fq_table('peer_model_claims')}
                        SET status = 'superseded', updated_at = NOW()
                        WHERE bank_id = $1 AND model_id = $2
                          AND status = 'active' AND locked = FALSE AND origin = 'derived'
                        """,
                        plan.bank_id,
                        _uuid_value(plan.model_id),
                    )
                if plan.supersede_claim_type is not None:
                    await conn.execute(
                        f"""
                        UPDATE {fq_table('peer_model_claims')}
                        SET status = 'superseded', updated_at = NOW()
                        WHERE bank_id = $1 AND model_id = $2
                          AND claim_type = $3 AND status = 'active' AND locked = FALSE
                        """,
                        plan.bank_id,
                        _uuid_value(plan.model_id),
                        plan.supersede_claim_type.value,
                    )
                for claim in plan.claims:
                    existing_id = None
                    if claim.origin == PeerClaimOrigin.DERIVED:
                        existing_id = await conn.fetchval(
                            f"""
                            SELECT id FROM {fq_table('peer_model_claims')}
                            WHERE bank_id = $1 AND model_id = $2 AND claim_type = $3
                              AND text = $4 AND origin = $5 AND status = 'active'
                            """,
                            plan.bank_id,
                            _uuid_value(plan.model_id),
                            claim.claim_type.value,
                            claim.text,
                            claim.origin.value,
                        )
                    claim_id = str(existing_id) if existing_id else claim.id
                    if existing_id is None:
                        await conn.execute(
                            f"""
                            INSERT INTO {fq_table('peer_model_claims')}
                                (id, bank_id, model_id, observer_peer_id, target_peer_id,
                                 claim_type, text, status, origin, confidence, locked, provenance)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                            """,
                            _uuid_value(claim.id),
                            plan.bank_id,
                            _uuid_value(plan.model_id),
                            _uuid_value(plan.observer_peer_id),
                            _uuid_value(plan.target_peer_id),
                            claim.claim_type.value,
                            claim.text,
                            claim.status.value,
                            claim.origin.value,
                            claim.confidence,
                            claim.locked,
                            claim.provenance,
                        )
                        claims_added += 1
                    for source_id in claim.source_ids:
                        await conn.execute(
                            f"""
                            INSERT INTO {fq_table('peer_model_claim_sources')}
                                (bank_id, claim_id, source_kind, source_id)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (bank_id, claim_id, source_kind, source_id) DO NOTHING
                            """,
                            plan.bank_id,
                            _uuid_value(claim_id),
                            claim.source_kind.value,
                            source_id,
                        )
        return PeerMaterializationResult(
            model_id=plan.model_id,
            version=plan.version,
            claims_added=claims_added,
            card_entries=len(plan.card_entries),
        )

    async def get_claim(self, *, bank_id: str, claim_id: str) -> PeerClaim | None:
        async with self._backend.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT id, bank_id, model_id, observer_peer_id, target_peer_id,
                       claim_type, text, status, origin, confidence, locked, provenance,
                       valid_from, valid_until, created_at, updated_at
                FROM {fq_table('peer_model_claims')}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(claim_id),
            )
            if row is None:
                return None
            sources = await self._sources_for_claims(conn, bank_id=bank_id, claim_ids=[claim_id])
            return self._claim_from_row(row, sources.get(claim_id, []))

    async def _model_row(
        self,
        conn: DatabaseConnection,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
    ) -> Any:
        return await conn.fetchrow(
            f"""
            SELECT id, bank_id, observer_peer_id, target_peer_id, version, card,
                   representation, created_at, updated_at
            FROM {fq_table('peer_models')}
            WHERE bank_id = $1 AND observer_peer_id = $2 AND target_peer_id = $3
            """,
            bank_id,
            _uuid_value(observer_peer_id),
            _uuid_value(target_peer_id),
            )

    async def _claims_for_model(
        self,
        conn: DatabaseConnection,
        *,
        bank_id: str,
        model_id: str,
    ) -> list[PeerClaim]:
        rows = await conn.fetch(
            f"""
            SELECT id, bank_id, model_id, observer_peer_id, target_peer_id,
                   claim_type, text, status, origin, confidence, locked, provenance,
                   valid_from, valid_until, created_at, updated_at
            FROM {fq_table('peer_model_claims')}
            WHERE bank_id = $1 AND model_id = $2
            ORDER BY locked DESC, created_at ASC, id ASC
            """,
            bank_id,
            _uuid_value(model_id),
        )
        claim_ids = [str(row["id"]) for row in rows]
        sources = await self._sources_for_claims(conn, bank_id=bank_id, claim_ids=claim_ids)
        return [self._claim_from_row(row, sources.get(str(row["id"]), [])) for row in rows]

    async def _sources_for_claims(
        self,
        conn: DatabaseConnection,
        *,
        bank_id: str,
        claim_ids: list[str],
    ) -> dict[str, list[PeerSource]]:
        if not claim_ids:
            return {}
        rows = await conn.fetch(
            f"""
            SELECT claim_id, source_kind, source_id
            FROM {fq_table('peer_model_claim_sources')}
            WHERE bank_id = $1 AND claim_id IN ({', '.join(f'${index + 2}' for index in range(len(claim_ids)))})
            ORDER BY claim_id ASC, source_kind ASC, source_id ASC
            """,
            bank_id,
            *[_uuid_value(claim_id) for claim_id in claim_ids],
        )
        sources_by_claim: defaultdict[str, list[PeerSource]] = defaultdict(list)
        for row in rows:
            sources_by_claim[str(row["claim_id"])].append(
                PeerSource(source_kind=PeerSourceKind(row["source_kind"]), source_id=str(row["source_id"]))
            )
        return dict(sources_by_claim)

    @staticmethod
    def _peer_from_row(conn: DatabaseConnection, row: Any) -> Peer:
        return Peer(
            id=str(row["id"]),
            bank_id=str(row["bank_id"]),
            external_id=str(row["external_id"]),
            display_name=row["display_name"],
            kind=str(row["kind"]),
            metadata=_json_object(conn, row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _claim_from_row(row: Any, sources: list[PeerSource]) -> PeerClaim:
        return PeerClaim(
            id=str(row["id"]),
            bank_id=str(row["bank_id"]),
            model_id=str(row["model_id"]),
            observer_peer_id=str(row["observer_peer_id"]),
            target_peer_id=str(row["target_peer_id"]),
            claim_type=PeerClaimType(row["claim_type"]),
            text=str(row["text"]),
            status=PeerClaimStatus(row["status"]),
            origin=PeerClaimOrigin(row["origin"]),
            confidence=float(row["confidence"]),
            locked=bool(row["locked"]),
            provenance=row["provenance"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sources=sources,
        )

    @staticmethod
    def _card_from_row(row: Any, entries: list[PeerCardEntry]):
        from .models import PeerCard

        return PeerCard(
            model_id=str(row["id"]),
            bank_id=str(row["bank_id"]),
            observer_peer_id=str(row["observer_peer_id"]),
            target_peer_id=str(row["target_peer_id"]),
            version=int(row["version"]),
            entries=entries,
            updated_at=row["updated_at"],
        )
