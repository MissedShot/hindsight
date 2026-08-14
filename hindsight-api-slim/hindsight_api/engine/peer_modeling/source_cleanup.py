"""Transactional invalidation of peer projections backed by changed memories."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hindsight_api.engine.db import DatabaseConnection
from hindsight_api.engine.schema import fq_table

from .models import PeerCardEntry

_SOURCE_CURSOR_RESET = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class PeerSourceInvalidationResult:
    """Counts from one bank-scoped peer-source invalidation."""

    claims_superseded: int = 0
    source_links_deleted: int = 0
    models_updated: int = 0


@dataclass(frozen=True)
class _AffectedClaim:
    claim_id: str
    model_id: str
    representation_line: str


def _row_count(command_tag: str) -> int:
    """Read the affected-row count from PostgreSQL/Oracle command tags."""
    for token in reversed(command_tag.split()):
        try:
            return int(token)
        except ValueError:
            continue
    return 0


def _source_values(source_ids: list[str]) -> list[str]:
    """Normalize polymorphic text ids without assuming a UUID-backed store."""
    return list(dict.fromkeys(value for source_id in source_ids if (value := str(source_id))))


def _text_source_binds(source_values: list[str]) -> list[str]:
    """Prefix text ids so Oracle's generic UUID converter leaves them as text."""
    return [f"~{source_id}" for source_id in source_values]


def _text_source_in_list(*, start: int, count: int) -> str:
    """Build portable text binds; SUBSTR removes the Oracle-conversion guard."""
    return ", ".join(f"SUBSTR(${index}, 2)" for index in range(start, start + count))


async def invalidate_changed_memory_sources(
    conn: DatabaseConnection,
    *,
    bank_id: str,
    source_ids: list[str],
) -> PeerSourceInvalidationResult:
    """Invalidate active derived claims after memory evidence changes or disappears.

    Claim-source ids are polymorphic, so they cannot carry a direct foreign key to
    ``memory_units``. Every delete, edit, and invalidation path calls this function in
    the same transaction as the source mutation. Affected unlocked derived claims are
    superseded and filtered from the materialized projection. Stale memory source rows are
    removed from every active claim, including locked/manual claims, so strict projection
    validation cannot be poisoned while their curated content survives.

    The pair cursor resets to the Unix epoch so the normal bounded refresh can
    re-synthesize from all surviving evidence without putting an LLM inside the
    source-mutation transaction.
    """
    source_values = _source_values(source_ids)
    if not source_values:
        return PeerSourceInvalidationResult()
    source_binds = _text_source_binds(source_values)

    source_placeholders = _text_source_in_list(start=2, count=len(source_values))
    affected_query = f"""
        SELECT claims.id AS claim_id, claims.model_id, claims.claim_type, claims.text,
               claims.origin, claims.locked
        FROM {fq_table("peer_model_claim_sources")} links
        JOIN {fq_table("peer_model_claims")} claims
          ON claims.bank_id = links.bank_id
         AND claims.id = links.claim_id
        WHERE links.bank_id = $1
          AND links.source_kind = 'memory_unit'
          AND links.source_id IN ({source_placeholders})
          AND claims.status = 'active'
        ORDER BY claims.model_id ASC, claims.id ASC
    """
    candidate_rows = await conn.fetch(
        affected_query,
        bank_id,
        *source_binds,
    )
    candidate_model_ids = sorted({str(row["model_id"]) for row in candidate_rows})
    if candidate_model_ids:
        await conn.fetch(
            f"""
            SELECT id
            FROM {fq_table("peer_models")}
            WHERE bank_id = $1
              AND id IN ({", ".join(f"${index + 2}" for index in range(len(candidate_model_ids)))})
            ORDER BY id ASC
            FOR UPDATE
            """,
            bank_id,
            *[uuid.UUID(model_id) for model_id in candidate_model_ids],
        )
    rows = await conn.fetch(
        affected_query + " FOR UPDATE",
        bank_id,
        *source_binds,
    )
    affected_by_model: defaultdict[str, list[_AffectedClaim]] = defaultdict(list)
    seen_active_claim_ids: set[str] = set()
    supersede_claim_ids: set[str] = set()
    for row in rows:
        claim_id = str(row["claim_id"])
        if claim_id in seen_active_claim_ids:
            continue
        seen_active_claim_ids.add(claim_id)
        model_id = str(row["model_id"])
        affected_by_model.setdefault(model_id, [])
        if str(row["origin"]) == "derived" and not bool(row["locked"]):
            affected_by_model[model_id].append(
                _AffectedClaim(
                    claim_id=claim_id,
                    model_id=model_id,
                    representation_line=f"{row['claim_type']}: {row['text']}",
                )
            )
            supersede_claim_ids.add(claim_id)

    source_link_tag = await conn.execute(
        f"""
        DELETE FROM {fq_table("peer_model_claim_sources")}
        WHERE bank_id = $1
          AND source_kind = 'memory_unit'
          AND source_id IN ({_text_source_in_list(start=2, count=len(source_values))})
        """,
        bank_id,
        *source_binds,
    )
    if not affected_by_model:
        return PeerSourceInvalidationResult(source_links_deleted=_row_count(source_link_tag))

    affected_claim_ids = sorted(supersede_claim_ids)
    claim_tag = "UPDATE 0"
    if affected_claim_ids:
        claim_placeholders = ", ".join(f"${index + 3}" for index in range(len(affected_claim_ids)))
        claim_tag = await conn.execute(
            f"""
            UPDATE {fq_table("peer_model_claims")}
            SET status = 'superseded', updated_at = NOW()
            WHERE bank_id = $1
              AND status = 'active'
              AND origin = 'derived'
              AND locked = $2
              AND id IN ({claim_placeholders})
            """,
            bank_id,
            False,
            *[uuid.UUID(claim_id) for claim_id in affected_claim_ids],
        )

    models_updated = 0
    for model_id in sorted(affected_by_model):
        model_row = await conn.fetchrow(
            f"""
            SELECT card, representation
            FROM {fq_table("peer_models")}
            WHERE bank_id = $1 AND id = $2
            """,
            bank_id,
            uuid.UUID(model_id),
        )
        if model_row is None:
            continue
        affected_claims = affected_by_model[model_id]
        affected_ids = {claim.claim_id for claim in affected_claims}
        parsed_card: Any = conn.parse_json(model_row["card"])
        card_items = parsed_card if isinstance(parsed_card, list) else []
        card_entries = [PeerCardEntry.model_validate(item) for item in card_items]
        filtered_card = [entry for entry in card_entries if entry.claim_id not in affected_ids]

        surviving_rows = await conn.fetch(
            f"""
            SELECT claim_type, text
            FROM {fq_table("peer_model_claims")}
            WHERE bank_id = $1 AND model_id = $2 AND status = 'active'
            """,
            bank_id,
            uuid.UUID(model_id),
        )
        surviving_lines = {f"{row['claim_type']}: {row['text']}" for row in surviving_rows}
        removable_lines = {
            claim.representation_line for claim in affected_claims if claim.representation_line not in surviving_lines
        }
        representation = "\n".join(
            line for line in str(model_row["representation"] or "").splitlines() if line not in removable_lines
        )
        await conn.execute(
            f"""
            UPDATE {fq_table("peer_models")}
            SET version = version + 1,
                card = $3::jsonb,
                representation = $4,
                source_cursor = $5,
                source_cursor_id = NULL,
                updated_at = NOW()
            WHERE bank_id = $1 AND id = $2
            """,
            bank_id,
            uuid.UUID(model_id),
            json.dumps([entry.model_dump(mode="json") for entry in filtered_card]),
            representation,
            _SOURCE_CURSOR_RESET,
        )
        models_updated += 1

    return PeerSourceInvalidationResult(
        claims_superseded=_row_count(claim_tag),
        source_links_deleted=_row_count(source_link_tag),
        models_updated=models_updated,
    )


__all__ = ["PeerSourceInvalidationResult", "invalidate_changed_memory_sources"]
