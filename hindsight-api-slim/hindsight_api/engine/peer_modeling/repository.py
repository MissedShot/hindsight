"""Database repository for the peer-modeling vertical slice.

The repository owns all SQL and always scopes every read/write by bank_id. It uses the
existing DatabaseBackend/DatabaseConnection abstraction so PostgreSQL and Oracle share
the same query shape and driver rewriting.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from hindsight_api.engine.db import DatabaseBackend, DatabaseConnection
from hindsight_api.engine.schema import fq_table

from .errors import PeerConflictError, PeerValidationError
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
    PeerMemorySource,
    PeerMemoryWindow,
    PeerModel,
    PeerPendingMemorySources,
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
                INSERT INTO {fq_table("peers")}
                    (id, bank_id, external_id, display_name, kind, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (bank_id, external_id) DO NOTHING
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
                FROM {fq_table("peers")}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(peer_id),
            )
            if row is None:
                raise PeerConflictError(f"Peer external_id '{external_id}' already exists in bank '{bank_id}'")
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
                FROM {fq_table("peers")}
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
                FROM {fq_table("peers")}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(peer_id),
            )
            return self._peer_from_row(conn, row) if row else None

    async def resolve_peer_id(self, *, bank_id: str, reference: str) -> str | None:
        """Resolve either an internal UUID or a bank-scoped external_id."""
        try:
            internal_id = _uuid_value(reference)
        except ValueError:
            internal_id = None
        async with self._backend.acquire() as conn:
            if internal_id is not None:
                row = await conn.fetchrow(
                    f"SELECT id FROM {fq_table('peers')} WHERE bank_id = $1 AND (id = $2 OR external_id = $3)",
                    bank_id,
                    internal_id,
                    reference,
                )
            else:
                row = await conn.fetchrow(
                    f"SELECT id FROM {fq_table('peers')} WHERE bank_id = $1 AND external_id = $2",
                    bank_id,
                    reference,
                )
            return str(row["id"]) if row else None

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
                UPDATE {fq_table("peers")}
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
                FROM {fq_table("peers")}
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
                SELECT COUNT(*) FROM {fq_table("peers")}
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
                    SELECT 1 FROM {fq_table("memory_units")}
                    WHERE bank_id = $1 AND id = $2::uuid
                    """,
                    bank_id,
                    _uuid_value(source_id),
                )
                if found is None:
                    return False
        return True

    async def get_memory_texts(self, *, bank_id: str, source_ids: list[str]) -> dict[str, str]:
        """Load source text for deterministic, evidence-backed auto materialization."""
        if not source_ids:
            return {}
        async with self._backend.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, text FROM {fq_table("memory_units")}
                WHERE bank_id = $1
                  AND id IN ({", ".join(f"${index + 2}" for index in range(len(source_ids)))})
                """,
                bank_id,
                *[_uuid_value(source_id) for source_id in source_ids],
            )
        return {str(row["id"]): str(row["text"]) for row in rows}

    async def list_bootstrap_memory_window(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        after_cursor: datetime,
        after_cursor_id: str | None,
        snapshot_at: datetime,
        limit: int = 16,
    ) -> PeerMemoryWindow:
        """Read one immutable, pair-assigned refresh window from the bootstrap corpus.

        The pair arguments deliberately do not become role-table joins: the existing
        directional model is the assignment boundary, while bootstrap's target-bound
        extractor decides whether this bank-level row is relevant to this target.
        """
        del observer_peer_id, target_peer_id
        bounded_limit = min(max(1, limit), 16)
        if after_cursor_id is None:
            cursor_clause = "AND memory.updated_at >= $3"
            parameters: list[Any] = [bank_id, snapshot_at, after_cursor, bounded_limit + 1]
            limit_placeholder = "$4"
        else:
            cursor_clause = "AND (memory.updated_at > $3 OR (memory.updated_at = $3 AND memory.id > $4))"
            parameters = [bank_id, snapshot_at, after_cursor, _uuid_value(after_cursor_id), bounded_limit + 1]
            limit_placeholder = "$5"
        async with self._backend.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT memory.id, memory.text, memory.context, memory.fact_type, memory.updated_at
                FROM {fq_table("memory_units")} memory
                WHERE memory.bank_id = $1
                  AND memory.updated_at <= $2
                  {cursor_clause}
                  AND (
                        (memory.fact_type = 'observation' AND EXISTS (
                            SELECT 1 FROM {fq_table("memory_units")} observations
                            WHERE observations.bank_id = memory.bank_id
                              AND observations.fact_type = 'observation'
                              AND observations.updated_at <= $2
                        ))
                     OR (memory.fact_type IN ('world', 'experience') AND NOT EXISTS (
                            SELECT 1 FROM {fq_table("memory_units")} observations
                            WHERE observations.bank_id = memory.bank_id
                              AND observations.fact_type = 'observation'
                              AND observations.updated_at <= $2
                        ))
                  )
                ORDER BY memory.updated_at ASC, memory.id ASC
                LIMIT {limit_placeholder}
                """,
                *parameters,
            )
        selected = rows[:bounded_limit]
        sources = [
            PeerMemorySource(
                id=str(row["id"]),
                text=str(row["text"] or ""),
                context=str(row["context"] or ""),
                fact_type=str(row["fact_type"]),
                updated_at=row["updated_at"],
            )
            for row in selected
        ]
        last_source = sources[-1] if sources else None
        return PeerMemoryWindow(
            sources=sources,
            next_cursor=last_source.updated_at if last_source else None,
            next_cursor_id=last_source.id if last_source else None,
            has_more=len(rows) > bounded_limit,
        )

    async def list_pair_memory_source_ids(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        created_before: datetime,
        limit: int = 16,
    ) -> list[str]:
        """List a bounded, bank-scoped snapshot of evidence attributed to one pair."""
        bounded_limit = min(max(1, limit), 16)
        async with self._backend.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT memory.id
                FROM {fq_table("memory_units")} memory
                JOIN {fq_table("memory_peer_roles")} observer_role
                  ON observer_role.bank_id = memory.bank_id
                 AND observer_role.memory_unit_id = memory.id
                 AND observer_role.peer_id = $2
                 AND observer_role.role = 'observer'
                 AND observer_role.modality = 'actual'
                JOIN {fq_table("memory_peer_roles")} target_role
                  ON target_role.bank_id = memory.bank_id
                 AND target_role.memory_unit_id = memory.id
                 AND target_role.peer_id = $3
                 AND target_role.role IN ('subject', 'participant')
                 AND target_role.modality = 'actual'
                WHERE memory.bank_id = $1
                  AND memory.created_at < $4
                GROUP BY memory.id, memory.created_at
                ORDER BY memory.created_at DESC, memory.id DESC
                LIMIT $5
                """,
                bank_id,
                _uuid_value(observer_peer_id),
                _uuid_value(target_peer_id),
                created_before,
                bounded_limit,
            )
        return [str(row["id"]) for row in rows]

    async def get_pending_memory_sources(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
    ) -> PeerPendingMemorySources:
        """Return explicitly attributed facts not yet checkpointed for this pair."""
        async with self._backend.acquire() as conn:
            model_row = await self._model_row(conn, bank_id, observer_peer_id, target_peer_id)
            cursor = None
            cursor_id = None
            if model_row is not None:
                cursor = model_row["source_cursor"]
                cursor_id = model_row["source_cursor_id"]
            cursor_clause = ""
            parameters: list[Any] = [
                bank_id,
                _uuid_value(observer_peer_id),
                _uuid_value(target_peer_id),
            ]
            if cursor is not None and cursor_id is not None:
                cursor_clause = " AND (memory.created_at > $4 OR (memory.created_at = $4 AND memory.id > $5))"
                parameters.extend([cursor, _uuid_value(str(cursor_id))])
            elif cursor is not None:
                # Safe compatibility path for a timestamp-only checkpoint: replay
                # the boundary rather than dropping same-timestamp evidence.
                cursor_clause = " AND memory.created_at >= $4"
                parameters.append(cursor)
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT memory.id, memory.created_at
                FROM {fq_table("memory_units")} memory
                JOIN {fq_table("memory_peer_roles")} observer_role
                  ON observer_role.bank_id = memory.bank_id
                 AND observer_role.memory_unit_id = memory.id
                 AND observer_role.peer_id = $2
                 AND observer_role.role = 'observer'
                 AND observer_role.modality = 'actual'
                JOIN {fq_table("memory_peer_roles")} target_role
                  ON target_role.bank_id = memory.bank_id
                 AND target_role.memory_unit_id = memory.id
                 AND target_role.peer_id = $3
                 AND target_role.role IN ('subject', 'participant')
                 AND target_role.modality = 'actual'
                WHERE memory.bank_id = $1{cursor_clause}
                ORDER BY memory.created_at ASC, memory.id ASC
                """,
                *parameters,
            )
        source_ids = [str(row["id"]) for row in rows]
        next_cursor = rows[-1]["created_at"] if rows else cursor
        next_cursor_id = str(rows[-1]["id"]) if rows else (str(cursor_id) if cursor_id is not None else None)
        return PeerPendingMemorySources(
            source_ids=source_ids,
            next_cursor=next_cursor,
            next_cursor_id=next_cursor_id,
        )

    async def advance_source_cursor(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        source_cursor: datetime,
        source_cursor_id: str,
        bank_source_ids: list[str] | None = None,
        expected_source_versions: Mapping[str, datetime] | None = None,
        model_id: str | None = None,
        validate_existing_sources: bool = False,
    ) -> None:
        """Checkpoint processed evidence without changing card version or freshness."""
        async with self._backend.acquire() as conn:
            async with conn.transaction():
                if validate_existing_sources:
                    if model_id is None:
                        raise PeerValidationError("model_id is required to validate existing memory sources")
                    await conn.fetchrow(
                        f"""
                        SELECT id FROM {fq_table("peer_models")}
                        WHERE bank_id = $1 AND id = $2
                        FOR UPDATE
                        """,
                        bank_id,
                        _uuid_value(model_id),
                    )
                    await self._validate_active_model_memory_sources(
                        conn,
                        bank_id=bank_id,
                        model_id=model_id,
                    )
                if bank_source_ids is not None:
                    await self._validate_bank_memory_sources(
                        conn,
                        bank_id=bank_id,
                        source_ids=bank_source_ids,
                        expected_source_versions=expected_source_versions,
                    )
                await self._advance_source_cursor(
                    conn,
                    bank_id=bank_id,
                    observer_peer_id=observer_peer_id,
                    target_peer_id=target_peer_id,
                    source_cursor=source_cursor,
                    source_cursor_id=source_cursor_id,
                )

    async def _advance_source_cursor(
        self,
        conn: DatabaseConnection,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        source_cursor: datetime,
        source_cursor_id: str,
    ) -> None:
        await conn.execute(
            f"""
            UPDATE {fq_table("peer_models")}
            SET source_cursor = $4, source_cursor_id = $5
            WHERE bank_id = $1 AND observer_peer_id = $2 AND target_peer_id = $3
              AND (
                    source_cursor IS NULL
                 OR source_cursor < $4
                 OR (source_cursor = $4 AND (source_cursor_id IS NULL OR source_cursor_id < $5))
              )
            """,
            bank_id,
            _uuid_value(observer_peer_id),
            _uuid_value(target_peer_id),
            source_cursor,
            _uuid_value(source_cursor_id),
        )

    async def list_directional_models(self, *, bank_id: str) -> list[PeerModel]:
        """List already-materialized directional models for one bank."""
        async with self._backend.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, bank_id, observer_peer_id, target_peer_id, version, card,
                       representation, source_cursor, source_cursor_id, created_at, updated_at
                FROM {fq_table("peer_models")}
                WHERE bank_id = $1
                ORDER BY observer_peer_id ASC, target_peer_id ASC
                """,
                bank_id,
            )
            models: list[PeerModel] = []
            for row in rows:
                card_entries = [PeerCardEntry.model_validate(item) for item in _json_list(conn, row["card"])]
                models.append(
                    PeerModel(
                        id=str(row["id"]),
                        bank_id=str(row["bank_id"]),
                        observer_peer_id=str(row["observer_peer_id"]),
                        target_peer_id=str(row["target_peer_id"]),
                        version=int(row["version"]),
                        card=self._card_from_row(row, card_entries),
                        representation=str(row["representation"] or ""),
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        source_cursor=row["source_cursor"],
                        source_cursor_id=str(row["source_cursor_id"]) if row["source_cursor_id"] is not None else None,
                    )
                )
            return models

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
                source_cursor=row["source_cursor"],
                source_cursor_id=str(row["source_cursor_id"]) if row["source_cursor_id"] is not None else None,
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

    async def validate_model_memory_sources(
        self,
        *,
        bank_id: str,
        model_id: str,
        new_source_ids: list[str],
        expected_source_versions: Mapping[str, datetime] | None = None,
    ) -> None:
        """Lock/revalidate all active projection sources plus new refresh evidence."""
        async with self._backend.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow(
                    f"""
                    SELECT id FROM {fq_table("peer_models")}
                    WHERE bank_id = $1 AND id = $2
                    FOR UPDATE
                    """,
                    bank_id,
                    _uuid_value(model_id),
                )
                await self._validate_active_model_memory_sources(
                    conn,
                    bank_id=bank_id,
                    model_id=model_id,
                )
                self._validate_expected_source_versions(
                    new_source_ids,
                    expected_source_versions,
                )
                await self._validate_bank_memory_sources(
                    conn,
                    bank_id=bank_id,
                    source_ids=new_source_ids,
                    expected_source_versions=expected_source_versions,
                )

    async def _validate_active_model_memory_sources(
        self,
        conn: DatabaseConnection,
        *,
        bank_id: str,
        model_id: str,
    ) -> None:
        """Lock every active memory-unit source contributing to a model projection."""
        memory_id_text = (
            "memory.id::text" if getattr(conn, "backend_type", "postgresql") == "postgresql" else "TO_CHAR(memory.id)"
        )
        active_sources = f"""
            SELECT DISTINCT links.source_id
            FROM {fq_table("peer_model_claim_sources")} links
            JOIN {fq_table("peer_model_claims")} claims
              ON claims.bank_id = links.bank_id
             AND claims.id = links.claim_id
            WHERE links.bank_id = $1
              AND claims.model_id = $2
              AND claims.status = 'active'
              AND links.source_kind = 'memory_unit'
        """
        active_rows = await conn.fetch(
            f"""
            WITH active_sources AS ({active_sources})
            SELECT source_id
            FROM active_sources
            ORDER BY source_id
            """,
            bank_id,
            _uuid_value(model_id),
        )
        active_source_ids = {str(row["source_id"]) for row in active_rows}
        if not active_source_ids:
            return
        source_rows = await conn.fetch(
            f"""
            WITH active_sources AS ({active_sources})
            SELECT memory.id
            FROM {fq_table("memory_units")} memory
            JOIN active_sources
              ON {memory_id_text} = active_sources.source_id
            WHERE memory.bank_id = $1
            ORDER BY memory.id
            FOR UPDATE
            """,
            bank_id,
            _uuid_value(model_id),
        )
        if {str(row["id"]) for row in source_rows} != active_source_ids:
            raise PeerValidationError("A memory_unit source is missing from the model projection")

    @staticmethod
    def _validate_expected_source_versions(
        source_ids: list[str],
        expected_source_versions: Mapping[str, datetime] | None,
    ) -> None:
        if set(expected_source_versions or {}) - set(source_ids):
            raise PeerValidationError("Expected source versions must belong to the validated source set")

    async def validate_pair_memory_sources(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        source_ids: list[str],
    ) -> None:
        """Transactionally validate every memory source used by a pair projection."""
        async with self._backend.acquire() as conn:
            async with conn.transaction():
                await conn.fetch(
                    f"""
                    SELECT id FROM {fq_table("peers")}
                    WHERE bank_id = $1 AND id IN ($2, $3)
                    ORDER BY id FOR UPDATE
                    """,
                    bank_id,
                    _uuid_value(observer_peer_id),
                    _uuid_value(target_peer_id),
                )
                await self._validate_pair_memory_sources(
                    conn,
                    bank_id=bank_id,
                    observer_peer_id=observer_peer_id,
                    target_peer_id=target_peer_id,
                    source_ids=source_ids,
                )

    async def apply_materialization(
        self,
        plan: PeerMaterializationPlan,
        *,
        pair_source_ids: list[str] | None = None,
        bank_source_ids: list[str] | None = None,
        expected_source_versions: Mapping[str, datetime] | None = None,
        validate_existing_sources: bool = False,
    ) -> PeerMaterializationResult:
        """Apply claims, source links, card, and representation in one transaction."""
        claims_added = 0
        card_json = json.dumps([entry.model_dump(mode="json") for entry in plan.card_entries])
        async with self._backend.acquire() as conn:
            async with conn.transaction():
                # Lock both peer identities in a stable order. This also serializes
                # first-time materializations where no peer_models row exists yet.
                await conn.fetch(
                    f"""
                    SELECT id FROM {fq_table("peers")}
                    WHERE bank_id = $1 AND id IN ($2, $3)
                    ORDER BY id FOR UPDATE
                    """,
                    plan.bank_id,
                    _uuid_value(plan.observer_peer_id),
                    _uuid_value(plan.target_peer_id),
                )
                if bank_source_ids is not None:
                    await self._validate_bank_memory_sources(
                        conn,
                        bank_id=plan.bank_id,
                        source_ids=bank_source_ids,
                        expected_source_versions=expected_source_versions,
                    )
                if pair_source_ids is not None:
                    await self._validate_pair_memory_sources(
                        conn,
                        bank_id=plan.bank_id,
                        observer_peer_id=plan.observer_peer_id,
                        target_peer_id=plan.target_peer_id,
                        source_ids=pair_source_ids,
                    )
                current_version = await conn.fetchval(
                    f"""
                    SELECT version FROM {fq_table("peer_models")}
                    WHERE bank_id = $1 AND observer_peer_id = $2 AND target_peer_id = $3
                    FOR UPDATE
                    """,
                    plan.bank_id,
                    _uuid_value(plan.observer_peer_id),
                    _uuid_value(plan.target_peer_id),
                )
                if validate_existing_sources:
                    await self._validate_active_model_memory_sources(
                        conn,
                        bank_id=plan.bank_id,
                        model_id=plan.model_id,
                    )
                expected_version = int(current_version or 0) + 1
                if plan.version != expected_version:
                    raise PeerConflictError(
                        f"Peer model changed concurrently (expected version {plan.version}, current version {current_version})"
                    )
                await conn.execute(
                    f"""
                    INSERT INTO {fq_table("peer_models")}
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
                if plan.source_cursor is not None and plan.source_cursor_id is not None:
                    await self._advance_source_cursor(
                        conn,
                        bank_id=plan.bank_id,
                        observer_peer_id=plan.observer_peer_id,
                        target_peer_id=plan.target_peer_id,
                        source_cursor=plan.source_cursor,
                        source_cursor_id=plan.source_cursor_id,
                    )
                if plan.rebuild:
                    await conn.execute(
                        f"""
                        UPDATE {fq_table("peer_model_claims")}
                        SET status = 'superseded', updated_at = NOW()
                        WHERE bank_id = $1 AND model_id = $2
                          AND status = 'active' AND locked = FALSE AND origin = 'derived'
                        """,
                        plan.bank_id,
                        _uuid_value(plan.model_id),
                    )
                # Corrections target exact reviewed claim IDs. The former broad
                # claim-type update allowed one ATTRIBUTE edit to supersede every
                # unrelated attribute in the directional model.
                for claim_id in plan.supersede_claim_ids:
                    await conn.execute(
                        f"""
                        UPDATE {fq_table("peer_model_claims")}
                        SET status = 'superseded', updated_at = NOW()
                        WHERE bank_id = $1 AND model_id = $2 AND id = $3
                          AND status = 'active'
                        """,
                        plan.bank_id,
                        _uuid_value(plan.model_id),
                        _uuid_value(claim_id),
                    )
                for claim_id in plan.reactivate_claim_ids:
                    await conn.execute(
                        f"""
                        UPDATE {fq_table("peer_model_claims")}
                        SET status = 'active', updated_at = NOW()
                        WHERE bank_id = $1 AND model_id = $2 AND id = $3
                          AND status = 'superseded'
                        """,
                        plan.bank_id,
                        _uuid_value(plan.model_id),
                        _uuid_value(claim_id),
                    )
                for claim in plan.claims:
                    # Claim identity is resolved by PeerModelingService while it
                    # builds the plan. Do not repair a transient card/source ID
                    # after the fact by matching claim text here: that leaves the
                    # plan internally inconsistent. The lookup below only
                    # confirms that a plan's canonical ID is already persisted.
                    persisted_id = await conn.fetchval(
                        f"""
                        SELECT id FROM {fq_table("peer_model_claims")}
                        WHERE bank_id = $1 AND model_id = $2 AND id = $3
                          AND status = 'active'
                        """,
                        plan.bank_id,
                        _uuid_value(plan.model_id),
                        _uuid_value(claim.id),
                    )
                    claim_id = str(persisted_id) if persisted_id else claim.id
                    if persisted_id is None:
                        await conn.execute(
                            f"""
                            INSERT INTO {fq_table("peer_model_claims")}
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
                    else:
                        await conn.execute(
                            f"""
                            UPDATE {fq_table("peer_model_claims")}
                            SET confidence = GREATEST(confidence, $4), updated_at = NOW()
                            WHERE bank_id = $1 AND model_id = $2 AND id = $3
                              AND status = 'active' AND locked = FALSE AND origin = 'derived'
                            """,
                            plan.bank_id,
                            _uuid_value(plan.model_id),
                            _uuid_value(claim_id),
                            claim.confidence,
                        )
                    for source_id in claim.source_ids:
                        await conn.execute(
                            f"""
                            INSERT INTO {fq_table("peer_model_claim_sources")}
                                (bank_id, claim_id, source_kind, source_id)
                            VALUES ($1, $2, $3, SUBSTR($4, 2))
                            ON CONFLICT (bank_id, claim_id, source_kind, source_id) DO NOTHING
                            """,
                            plan.bank_id,
                            _uuid_value(claim_id),
                            claim.source_kind.value,
                            f"~{source_id}",
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
                FROM {fq_table("peer_model_claims")}
                WHERE bank_id = $1 AND id = $2
                """,
                bank_id,
                _uuid_value(claim_id),
            )
            if row is None:
                return None
            sources = await self._sources_for_claims(conn, bank_id=bank_id, claim_ids=[claim_id])
            return self._claim_from_row(row, sources.get(claim_id, []))

    async def _validate_pair_memory_sources(
        self,
        conn: DatabaseConnection,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        source_ids: list[str],
    ) -> None:
        """Lock and revalidate an explicit pair projection source set."""
        source_ids = sorted(set(source_ids))
        if not source_ids:
            return
        try:
            source_values = [_uuid_value(source_id) for source_id in source_ids]
        except ValueError as exc:
            raise PeerValidationError("Every memory_unit source must be a valid memory id") from exc

        source_placeholders = ", ".join(f"${index + 2}" for index in range(len(source_values)))
        memory_rows = await conn.fetch(
            f"""
            SELECT id
            FROM {fq_table("memory_units")}
            WHERE bank_id = $1 AND id IN ({source_placeholders})
            ORDER BY id
            FOR UPDATE
            """,
            bank_id,
            *source_values,
        )
        found_memory_ids = {str(row["id"]) for row in memory_rows}
        if found_memory_ids != set(source_ids):
            raise PeerValidationError("A memory_unit source is missing from the bank")

        role_placeholders = ", ".join(f"${index + 3}" for index in range(len(source_values)))
        observer_rows = await conn.fetch(
            f"""
            SELECT memory_unit_id
            FROM {fq_table("memory_peer_roles")}
            WHERE bank_id = $1
              AND peer_id = $2
              AND role = 'observer'
              AND modality = 'actual'
              AND memory_unit_id IN ({role_placeholders})
            ORDER BY memory_unit_id
            FOR UPDATE
            """,
            bank_id,
            _uuid_value(observer_peer_id),
            *source_values,
        )
        found_observer_ids = {str(row["memory_unit_id"]) for row in observer_rows}
        if found_observer_ids != set(source_ids):
            raise PeerValidationError("A memory_unit source is not attributed to the observer pair role")

        target_rows = await conn.fetch(
            f"""
            SELECT memory_unit_id
            FROM {fq_table("memory_peer_roles")}
            WHERE bank_id = $1
              AND peer_id = $2
              AND role IN ('subject', 'participant')
              AND modality = 'actual'
              AND memory_unit_id IN ({role_placeholders})
            ORDER BY memory_unit_id
            FOR UPDATE
            """,
            bank_id,
            _uuid_value(target_peer_id),
            *source_values,
        )
        found_target_ids = {str(row["memory_unit_id"]) for row in target_rows}
        if found_target_ids != set(source_ids):
            raise PeerValidationError("A memory_unit source is not attributed to the target pair role")

    async def validate_bank_memory_sources(
        self,
        *,
        bank_id: str,
        source_ids: list[str],
        expected_source_versions: Mapping[str, datetime] | None = None,
    ) -> None:
        """Transactionally validate refresh evidence belongs to the bank without roles."""
        async with self._backend.acquire() as conn:
            async with conn.transaction():
                await self._validate_bank_memory_sources(
                    conn,
                    bank_id=bank_id,
                    source_ids=source_ids,
                    expected_source_versions=expected_source_versions,
                )

    async def _validate_bank_memory_sources(
        self,
        conn: DatabaseConnection,
        *,
        bank_id: str,
        source_ids: list[str],
        expected_source_versions: Mapping[str, datetime] | None = None,
    ) -> None:
        source_ids = sorted(set(source_ids))
        if not source_ids:
            return
        expected_versions = dict(expected_source_versions or {})
        if set(expected_versions) - set(source_ids):
            raise PeerValidationError("Expected source versions must belong to the validated source set")
        try:
            source_values = [_uuid_value(source_id) for source_id in source_ids]
        except ValueError as exc:
            raise PeerValidationError("Every memory_unit source must be a valid memory id") from exc
        source_placeholders = ", ".join(f"${index + 2}" for index in range(len(source_values)))
        rows = await conn.fetch(
            f"""
            SELECT id, updated_at
            FROM {fq_table("memory_units")}
            WHERE bank_id = $1 AND id IN ({source_placeholders})
            ORDER BY id
            FOR UPDATE
            """,
            bank_id,
            *source_values,
        )
        found_sources = {str(row["id"]): row for row in rows}
        found_source_ids = set(found_sources)
        if found_source_ids != set(source_ids):
            raise PeerValidationError("A memory_unit source is missing from the bank")
        for source_id, expected_updated_at in expected_versions.items():
            if found_sources[source_id]["updated_at"] != expected_updated_at:
                raise PeerValidationError("A refresh memory_unit source changed after the snapshot")

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
                   representation, source_cursor, source_cursor_id, created_at, updated_at
            FROM {fq_table("peer_models")}
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
            FROM {fq_table("peer_model_claims")}
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
            FROM {fq_table("peer_model_claim_sources")}
            WHERE bank_id = $1 AND claim_id IN ({", ".join(f"${index + 2}" for index in range(len(claim_ids)))})
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
