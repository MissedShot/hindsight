"""Retain-time persistence for explicit peer attribution."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from hindsight_api.engine.schema import fq_table

from .errors import PeerValidationError

if TYPE_CHECKING:
    from hindsight_api.engine.db import DatabaseConnection
    from hindsight_api.engine.retain.types import RetainContent

_ALLOWED_MODALITIES = {"actual", "hypothetical", "fictional", "quoted"}


def _peer_refs(context: dict[str, Any]) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for role, field in (("observer", "observer_peer_id"), ("speaker", "speaker_peer_id")):
        value = context.get(field)
        if isinstance(value, str) and value.strip():
            refs.append((role, value.strip()))
    subjects = context.get("subject_peer_ids")
    if isinstance(subjects, list):
        refs.extend(("subject", value.strip()) for value in subjects if isinstance(value, str) and value.strip())
    participants = context.get("participant_peer_ids")
    if isinstance(participants, list):
        refs.extend(
            ("participant", value.strip()) for value in participants if isinstance(value, str) and value.strip()
        )
    return list(dict.fromkeys(refs))


async def _resolve_peer_id(conn: "DatabaseConnection", bank_id: str, reference: str) -> uuid.UUID:
    try:
        peer_uuid = uuid.UUID(reference)
    except ValueError:
        peer_uuid = None
    if peer_uuid is not None:
        value = await conn.fetchval(
            f"SELECT id FROM {fq_table('peers')} WHERE bank_id = $1 AND (id = $2 OR external_id = $3)",
            bank_id,
            peer_uuid,
            reference,
        )
    else:
        value = await conn.fetchval(
            f"SELECT id FROM {fq_table('peers')} WHERE bank_id = $1 AND external_id = $2",
            bank_id,
            reference,
        )
    if value is None:
        raise PeerValidationError(f"Peer reference '{reference}' does not exist in bank '{bank_id}'")
    return uuid.UUID(str(value))


async def persist_memory_peer_roles(
    conn: "DatabaseConnection",
    bank_id: str,
    contents: list["RetainContent"],
    result_unit_ids: list[list[str]],
) -> int:
    """Persist explicit content attribution for every produced memory unit."""
    inserted = 0
    resolved: dict[str, uuid.UUID] = {}
    for content, unit_ids in zip(contents, result_unit_ids, strict=True):
        context = content.peer_context
        if not context or not unit_ids:
            continue
        modality = str(context.get("modality", "actual"))
        if modality not in _ALLOWED_MODALITIES:
            raise PeerValidationError(f"Unsupported peer attribution modality '{modality}'")
        refs = _peer_refs(context)
        for _, reference in refs:
            if reference not in resolved:
                resolved[reference] = await _resolve_peer_id(conn, bank_id, reference)
        for unit_id in unit_ids:
            memory_uuid = uuid.UUID(str(unit_id))
            for role, reference in refs:
                await conn.execute(
                    f"""
                    INSERT INTO {fq_table("memory_peer_roles")}
                        (id, bank_id, memory_unit_id, peer_id, role, explicit, modality,
                         source_message_id, session_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    uuid.uuid4(),
                    bank_id,
                    memory_uuid,
                    resolved[reference],
                    role,
                    True,
                    modality,
                    context.get("source_message_id"),
                    context.get("session_id"),
                )
                inserted += 1
    return inserted
