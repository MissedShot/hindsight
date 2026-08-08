"""Bounded pair-scoped refresh for already-materialized directional peer models."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from .bootstrap import distill_directional_claims
from .models import PeerClaimDraft, PeerModelBase, PeerModelRequest, PeerSourceKind

if TYPE_CHECKING:
    from hindsight_api.engine.memory_engine import MemoryEngine
    from hindsight_api.models import RequestContext


_REFRESH_SOURCE_LIMIT = 16
_REFRESH_SOURCE_TEXT_LIMIT = 4_000
_REFRESH_TOTAL_TEXT_LIMIT = 64_000

_PROMPT_CONTROL_PAYLOAD = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:ignore|disregard)\s+(?:(?:all|any|the|your|previous|prior|of)\s+){0,4}"
    r"(?:safeguards?|safety\s+rules?|rules?|polic(?:y|ies))\s+"
    r"(?:(?!(?:not|never|avoid|without)\b)\w+\s+){0,5}"
    r"(?:exfiltrat(?:e|ing)|reveal(?:s|ed|ing)?|dump(?:s|ed|ing)?|"
    r"disclos(?:e|ed|ing)|leak(?:s|ed|ing)?)\s+"
    r"(?:(?:the|all|any|your|system)\s+){0,2}(?:secrets?|credentials?)(?:\s+.*)?"
    r"|override\s+(?:(?:all|any|the|your|previous|prior)\s+)?"
    r"(?:safety\s+rules?|safeguards?|rules?|polic(?:y|ies))(?:\s+.*)?"
    r"|follow\s+(?:the\s+)?system\s+instructions?\s+(?:to|and)\s+"
    r"(?:exfiltrate|reveal|dump|disclose|leak)\s+(?:the\s+)?(?:secrets?|credentials?)(?:\s+.*)?"
    r")\s*$",
    re.IGNORECASE,
)


class PeerRefreshPairOutcome(PeerModelBase):
    """Outcome for one existing observer-to-target direction."""

    observer_peer_id: str
    target_peer_id: str
    status: Literal["refreshed", "failed"]
    version_before: int
    version_after: int | None = None
    claims_materialized: int = 0
    error: str | None = None


class PeerRefreshResult(PeerModelBase):
    """Typed outcomes for every existing directional model considered."""

    pairs: list[PeerRefreshPairOutcome] = Field(default_factory=list)


Distiller = Callable[..., Awaitable[list[PeerClaimDraft]]]


class _SnapshotRepository:
    """Read-only evidence view handed to the default distiller."""

    def __init__(self, source_ids: tuple[str, ...], source_texts: dict[str, str]) -> None:
        self._source_ids = source_ids
        self._source_texts = MappingProxyType(dict(source_texts))

    async def get_memory_texts(self, *, bank_id: str, source_ids: list[str]) -> MappingProxyType[str, str]:
        if tuple(source_ids) != self._source_ids:
            raise ValueError("distiller requested a source set outside the refresh snapshot")
        return self._source_texts


class _SnapshotService:
    """Tiny service facade preventing the distiller from reaching mutable rows."""

    def __init__(self, repository: _SnapshotRepository) -> None:
        self.repository = repository


def _validated_claims(
    claims: Any,
    *,
    source_ids: tuple[str, ...],
) -> list[PeerClaimDraft]:
    """Reject any distiller output that escapes the pair's role-scoped source pool."""
    if not isinstance(claims, list) or not claims:
        raise ValueError("distiller returned no claims")

    allowed_source_ids = set(source_ids)
    validated: list[PeerClaimDraft] = []
    for claim in claims:
        if not isinstance(claim, PeerClaimDraft):
            raise ValueError("distiller returned an invalid claim")
        if claim.source_kind != PeerSourceKind.MEMORY_UNIT:
            raise ValueError("distiller returned a non-memory claim")
        if not claim.source_ids or any(source_id not in allowed_source_ids for source_id in claim.source_ids):
            raise ValueError("distiller returned a source outside the pair source pool")
        if _is_prompt_control_payload(claim.text):
            raise ValueError("distiller returned a prompt-control claim")
        text = claim.text.strip()
        if not text:
            raise ValueError("distiller returned an empty claim")
        validated.append(claim.model_copy(update={"text": text}))
    return validated


def _is_prompt_control_payload(text: str) -> bool:
    """Reject obvious imperative/meta-prompt payloads without banning ordinary claims."""
    normalized = " ".join(re.sub(r"[^\w\s]+", " ", text).split())
    return bool(_PROMPT_CONTROL_PAYLOAD.match(normalized))


async def refresh_existing_peer_models(
    *,
    memory_engine: "MemoryEngine",
    bank_id: str,
    request_context: "RequestContext",
    snapshot_at: datetime | None = None,
    distill_async: Distiller | None = None,
    operation_id: str | None = None,
) -> PeerRefreshResult:
    """Refresh existing directional models sequentially from bounded pair evidence."""
    service = await memory_engine._peer_modeling_service(bank_id, request_context)
    repository = service.repository
    models = await repository.list_directional_models(bank_id=bank_id)
    if not models:
        await memory_engine._write_operation_progress(operation_id, stage="completed", processed=0, total=0)
        return PeerRefreshResult()

    snapshot = snapshot_at or datetime.now(UTC)
    distiller = distill_async or distill_directional_claims
    total_models = len(models)
    await memory_engine._write_operation_progress(operation_id, stage="refreshing", processed=0, total=total_models)
    outcomes: list[PeerRefreshPairOutcome] = []
    for processed_models, model in enumerate(models, start=1):
        try:
            observer = await repository.get_peer(bank_id=bank_id, peer_id=model.observer_peer_id)
            target = await repository.get_peer(bank_id=bank_id, peer_id=model.target_peer_id)
            if observer is None or target is None:
                raise ValueError("directional model references a missing peer")

            source_ids = await repository.list_pair_memory_source_ids(
                bank_id=bank_id,
                observer_peer_id=model.observer_peer_id,
                target_peer_id=model.target_peer_id,
                created_before=snapshot,
                limit=_REFRESH_SOURCE_LIMIT,
            )
            if not source_ids:
                raise ValueError("pair has no memory-unit evidence")
            source_pool = tuple(source_ids[:_REFRESH_SOURCE_LIMIT])
            source_texts = await repository.get_memory_texts(bank_id=bank_id, source_ids=list(source_pool))
            total_source_text_length = 0
            for source_id in source_pool:
                source_text = source_texts.get(source_id)
                if not isinstance(source_text, str) or not source_text.strip():
                    raise ValueError("pair source text is missing or empty")
                if len(source_text) > _REFRESH_SOURCE_TEXT_LIMIT:
                    raise ValueError("pair source text exceeds the refresh bound")
                total_source_text_length += len(source_text)
            if total_source_text_length > _REFRESH_TOTAL_TEXT_LIMIT:
                raise ValueError("pair source text exceeds the total refresh bound")

            snapshot_service = _SnapshotService(
                _SnapshotRepository(
                    source_pool,
                    {source_id: source_texts[source_id] for source_id in source_pool},
                )
            )
            claims = _validated_claims(
                await distiller(
                    memory_engine=memory_engine,
                    service=snapshot_service,
                    bank_id=bank_id,
                    observer=observer,
                    target=target,
                    source_ids=list(source_pool),
                    request_context=request_context,
                ),
                source_ids=source_pool,
            )
            updated = await service.model(
                bank_id,
                model.observer_peer_id,
                model.target_peer_id,
                PeerModelRequest(claims=claims),
                validate_pair_sources=True,
            )
            outcomes.append(
                PeerRefreshPairOutcome(
                    observer_peer_id=model.observer_peer_id,
                    target_peer_id=model.target_peer_id,
                    status="refreshed",
                    version_before=model.version,
                    version_after=updated.version,
                    claims_materialized=len(claims),
                )
            )
        except Exception as exc:
            outcomes.append(
                PeerRefreshPairOutcome(
                    observer_peer_id=model.observer_peer_id,
                    target_peer_id=model.target_peer_id,
                    status="failed",
                    version_before=model.version,
                    error=type(exc).__name__,
                )
            )
        finally:
            await memory_engine._write_operation_progress(
                operation_id,
                stage="completed" if processed_models == total_models else "refreshing",
                processed=processed_models,
                total=total_models,
            )
    return PeerRefreshResult(pairs=outcomes)


__all__ = ["PeerRefreshPairOutcome", "PeerRefreshResult", "refresh_existing_peer_models"]
