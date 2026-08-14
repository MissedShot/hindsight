"""Bounded pair-scoped refresh for already-materialized directional peer models."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from .bootstrap import distill_directional_claim_delta
from .models import (
    PeerClaimDelta,
    PeerClaimDraft,
    PeerModelBase,
    PeerModelRequest,
    PeerSourceKind,
)

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
    status: Literal["refreshed", "unchanged", "failed"]
    version_before: int
    version_after: int | None = None
    claims_materialized: int = 0
    cursor_advanced: bool = False
    has_more: bool = False
    error: str | None = None


class PeerRefreshResult(PeerModelBase):
    """Typed outcomes for every existing directional model considered."""

    status: Literal["completed", "partial", "failed", "unchanged"] = "unchanged"
    pairs: list[PeerRefreshPairOutcome] = Field(default_factory=list)


Distiller = Callable[..., Awaitable[PeerClaimDelta]]
LegacyDistiller = Callable[..., Awaitable[list[PeerClaimDraft]]]


class _SnapshotRepository:
    """Read-only evidence view handed to injected distillers."""

    def __init__(self, source_ids: tuple[str, ...], source_texts: dict[str, str]) -> None:
        self._source_ids = source_ids
        self._source_texts = MappingProxyType(dict(source_texts))

    async def get_memory_texts(self, *, bank_id: str, source_ids: list[str]) -> MappingProxyType[str, str]:
        if tuple(source_ids) != self._source_ids:
            raise ValueError("distiller requested a source set outside the refresh snapshot")
        return self._source_texts


class _SnapshotService:
    """Tiny service facade preventing injected distillers from mutable-row reads."""

    def __init__(self, repository: _SnapshotRepository) -> None:
        self.repository = repository


def _validated_claims(
    claims: Any,
    *,
    source_ids: tuple[str, ...],
) -> list[PeerClaimDraft]:
    """Reject distiller output outside the immutable new/current source pool."""
    if not isinstance(claims, list):
        raise ValueError("distiller returned an invalid claim list")
    allowed_source_ids = set(source_ids)
    validated: list[PeerClaimDraft] = []
    for claim in claims:
        if not isinstance(claim, PeerClaimDraft):
            raise ValueError("distiller returned an invalid claim")
        if claim.source_kind != PeerSourceKind.MEMORY_UNIT:
            raise ValueError("distiller returned a non-memory claim")
        if not claim.source_ids or any(source_id not in allowed_source_ids for source_id in claim.source_ids):
            raise ValueError("distiller returned a source outside the refresh source pool")
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


def _result_status(outcomes: list[PeerRefreshPairOutcome]) -> Literal["completed", "partial", "failed", "unchanged"]:
    """Classify orchestration truthfully, including bounded follow-up windows."""
    if not outcomes:
        return "unchanged"
    failures = sum(outcome.status == "failed" for outcome in outcomes)
    if failures == len(outcomes):
        return "failed"
    if failures or any(outcome.has_more and outcome.status != "failed" for outcome in outcomes):
        return "partial"
    if all(outcome.status == "unchanged" for outcome in outcomes):
        return "unchanged"
    return "completed"


async def refresh_existing_peer_models(
    *,
    memory_engine: "MemoryEngine",
    bank_id: str,
    request_context: "RequestContext",
    snapshot_at: datetime | None = None,
    distill_async: Distiller | LegacyDistiller | None = None,
    operation_id: str | None = None,
) -> PeerRefreshResult:
    """Refresh each existing model from one bounded immutable bootstrap window."""
    service = await memory_engine._peer_modeling_service(bank_id, request_context)
    repository = service.repository
    models = await repository.list_directional_models(bank_id=bank_id)
    if not models:
        await memory_engine._write_operation_progress(operation_id, stage="completed", processed=0, total=0)
        return PeerRefreshResult(status="unchanged")

    snapshot = snapshot_at or datetime.now(UTC)
    distiller = distill_async or distill_directional_claim_delta
    total_models = len(models)
    await memory_engine._write_operation_progress(operation_id, stage="refreshing", processed=0, total=total_models)
    outcomes: list[PeerRefreshPairOutcome] = []
    for processed_models, model in enumerate(models, start=1):
        try:
            observer = await repository.get_peer(bank_id=bank_id, peer_id=model.observer_peer_id)
            target = await repository.get_peer(bank_id=bank_id, peer_id=model.target_peer_id)
            if observer is None or target is None:
                raise ValueError("directional model references a missing peer")

            cursor = model.source_cursor or model.updated_at
            window = await repository.list_bootstrap_memory_window(
                bank_id=bank_id,
                observer_peer_id=model.observer_peer_id,
                target_peer_id=model.target_peer_id,
                after_cursor=cursor,
                after_cursor_id=model.source_cursor_id,
                snapshot_at=snapshot,
                limit=_REFRESH_SOURCE_LIMIT,
            )
            if not window.sources:
                await repository.validate_model_memory_sources(
                    bank_id=bank_id,
                    model_id=model.id,
                    new_source_ids=[],
                    expected_source_versions={},
                )
                outcomes.append(
                    PeerRefreshPairOutcome(
                        observer_peer_id=model.observer_peer_id,
                        target_peer_id=model.target_peer_id,
                        status="unchanged",
                        version_before=model.version,
                        version_after=model.version,
                    )
                )
                continue

            source_texts: dict[str, str] = {}
            total_source_text_length = 0
            for source in window.sources:
                if not source.text.strip():
                    raise ValueError("pair source text is missing or empty")
                if len(source.text) > _REFRESH_SOURCE_TEXT_LIMIT:
                    raise ValueError("pair source text exceeds the refresh bound")
                total_source_text_length += len(source.text)
                source_texts[source.id] = source.text
            if total_source_text_length > _REFRESH_TOTAL_TEXT_LIMIT:
                raise ValueError("pair source text exceeds the total refresh bound")

            all_current_claims = [
                claim
                for claim in (
                    await repository.get_directional_claims(
                        bank_id=bank_id,
                        observer_peer_id=model.observer_peer_id,
                        target_peer_id=model.target_peer_id,
                    )
                    or []
                )
                if claim.status.value == "active"
            ]
            current_claims = all_current_claims[:64]
            # Keep the semantic-delta prompt and its server-derived old-source
            # allowlist bounded even when a legacy model accumulated many links.
            current_claims = [claim.model_copy(update={"sources": claim.sources[:16]}) for claim in current_claims]
            current_source_ids = [
                source.source_id
                for claim in all_current_claims
                for source in claim.sources
                if source.source_kind == PeerSourceKind.MEMORY_UNIT
            ]
            source_pool = tuple(dict.fromkeys([*(source.id for source in window.sources), *current_source_ids]))
            expected_source_versions = {source.id: source.updated_at for source in window.sources}
            snapshot_service = _SnapshotService(
                _SnapshotRepository(tuple(source.id for source in window.sources), source_texts)
            )
            distiller_kwargs: dict[str, Any] = {
                "memory_engine": memory_engine,
                "service": snapshot_service,
                "bank_id": bank_id,
                "observer": observer,
                "target": target,
                "source_ids": [source.id for source in window.sources],
                "source_rows": list(window.sources),
                "current_claims": current_claims,
                "request_context": request_context,
            }
            try:
                signature = inspect.signature(distiller)
            except (TypeError, ValueError):
                signature = None
            if signature is not None and not any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
            ):
                distiller_kwargs = {
                    name: value for name, value in distiller_kwargs.items() if name in signature.parameters
                }
            raw_delta = await distiller(**distiller_kwargs)
            delta = raw_delta if isinstance(raw_delta, PeerClaimDelta) else PeerClaimDelta(claims=raw_delta)
            claims = _validated_claims(delta.claims, source_ids=source_pool)
            if window.next_cursor is None or window.next_cursor_id is None:
                raise ValueError("refresh window did not return a composite checkpoint")
            updated = await service.model(
                bank_id,
                model.observer_peer_id,
                model.target_peer_id,
                PeerModelRequest(claims=claims),
                source_cursor=window.next_cursor,
                source_cursor_id=window.next_cursor_id,
                validate_bank_sources=[source.id for source in window.sources],
                expected_source_versions=expected_source_versions,
                supersede_claim_ids=delta.supersede_claim_ids,
                validate_existing_sources=True,
            )
            changed = updated.version != model.version
            outcomes.append(
                PeerRefreshPairOutcome(
                    observer_peer_id=model.observer_peer_id,
                    target_peer_id=model.target_peer_id,
                    status="refreshed" if changed else "unchanged",
                    version_before=model.version,
                    version_after=updated.version,
                    claims_materialized=len(claims),
                    cursor_advanced=True,
                    has_more=window.has_more,
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
            has_pending_window = any(outcome.has_more and outcome.status != "failed" for outcome in outcomes)
            result_status = _result_status(outcomes)
            if processed_models != total_models or has_pending_window:
                progress_stage = "refreshing"
            elif result_status in {"failed", "partial"}:
                progress_stage = result_status
            else:
                progress_stage = "completed"
            await memory_engine._write_operation_progress(
                operation_id,
                stage=progress_stage,
                processed=processed_models,
                total=total_models,
            )
    return PeerRefreshResult(status=_result_status(outcomes), pairs=outcomes)


__all__ = ["PeerRefreshPairOutcome", "PeerRefreshResult", "refresh_existing_peer_models"]
