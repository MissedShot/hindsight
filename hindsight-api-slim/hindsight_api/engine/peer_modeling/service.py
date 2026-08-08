"""Bank-scoped peer-modeling service.

This module intentionally stops at a deterministic materialization boundary.  A future
LLM worker can produce ``PeerClaimDraft`` values and reuse the same validation and
repository transaction without changing the public storage or response models.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import PeerConflictError, PeerNotFoundError, PeerValidationError
from .models import (
    MAX_PEER_CARD_ENTRIES,
    Peer,
    PeerCardEntry,
    PeerClaim,
    PeerClaimDraft,
    PeerClaimOrigin,
    PeerClaimStatus,
    PeerClaimType,
    PeerClaimWrite,
    PeerContext,
    PeerCorrectionApplyRequest,
    PeerCorrectionResult,
    PeerCreate,
    PeerList,
    PeerMaterializationPlan,
    PeerModel,
    PeerModelRequest,
    PeerSourceKind,
    PeerUpdate,
)
from .repository import PeerRepository

_CLAIM_TYPE_ORDER = {
    PeerClaimType.IDENTITY: 0,
    PeerClaimType.ATTRIBUTE: 1,
    PeerClaimType.RELATIONSHIP: 2,
    PeerClaimType.INSTRUCTION: 3,
}


@dataclass(frozen=True)
class _PeerModelState:
    model: PeerModel | None
    claims: list[PeerClaim]


@dataclass(frozen=True)
class _RepresentationCandidate:
    claim_type: PeerClaimType
    text: str
    locked: bool
    confidence: float
    claim_id: str


@dataclass(frozen=True)
class _ProjectionClaim:
    claim_type: PeerClaimType
    text: str
    locked: bool
    confidence: float
    claim_id: str
    created_at: datetime


class PeerModelingService:
    """Validate peer operations and build deterministic materialized projections."""

    def __init__(
        self,
        repository: PeerRepository,
        *,
        max_card_entries: int = MAX_PEER_CARD_ENTRIES,
        representation_max_tokens: int = 1200,
    ):
        self.repository = repository
        self.max_card_entries = max(1, max_card_entries)
        self.representation_max_tokens = max(1, representation_max_tokens)

    async def create_peer(self, bank_id: str, payload: PeerCreate) -> Peer:
        peer_id = str(uuid.uuid4())
        return await self.repository.create_peer(
            bank_id=bank_id,
            peer_id=peer_id,
            external_id=payload.external_id,
            display_name=payload.display_name,
            kind=payload.kind,
            metadata=payload.metadata,
        )

    async def list_peers(self, bank_id: str, *, limit: int, offset: int) -> PeerList:
        return await self.repository.list_peers(bank_id=bank_id, limit=limit, offset=offset)

    async def get_peer(self, bank_id: str, peer_id: str) -> Peer | None:
        return await self.repository.get_peer(bank_id=bank_id, peer_id=peer_id)

    async def update_peer(self, bank_id: str, peer_id: str, payload: PeerUpdate) -> Peer | None:
        return await self.repository.update_peer(
            bank_id=bank_id,
            peer_id=peer_id,
            display_name=payload.display_name,
            kind=payload.kind,
            metadata=payload.metadata,
        )

    async def get_directional_model(self, bank_id: str, observer_peer_id: str, target_peer_id: str) -> PeerModel | None:
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        return await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )

    async def get_claims(self, bank_id: str, observer_peer_id: str, target_peer_id: str) -> list[PeerClaim]:
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        claims = await self.repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        return claims or []

    async def get_context(self, bank_id: str, observer_peer_id: str, target_peer_id: str) -> PeerContext:
        model = await self.get_directional_model(bank_id, observer_peer_id, target_peer_id)
        if model is None:
            raise PeerNotFoundError(
                f"No peer model exists for observer '{observer_peer_id}' and target '{target_peer_id}'"
            )
        claims = [
            claim
            for claim in await self.get_claims(bank_id, observer_peer_id, target_peer_id)
            if claim.status == PeerClaimStatus.ACTIVE
        ]
        return PeerContext(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model_id=model.id,
            version=model.version,
            card=model.card,
            representation=model.representation,
            claims=claims,
        )

    async def apply_correction(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        payload: PeerCorrectionApplyRequest,
    ) -> PeerCorrectionResult:
        """Apply a reviewed semantic plan without broad claim-type replacement."""
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        state = await self._load_model_and_claims(bank_id, observer_peer_id, target_peer_id)
        model, claims = state.model, state.claims
        if model is None:
            raise PeerNotFoundError("Peer model must exist before applying a correction")
        if model.version != payload.plan.base_model_version:
            raise PeerConflictError(
                "Peer model changed after the correction was planned; review a fresh plan before applying it"
            )

        active_claims = {claim.id: claim for claim in claims if claim.status == PeerClaimStatus.ACTIVE}
        supersede_claim_ids = list(dict.fromkeys(payload.plan.supersede_claim_ids))
        unknown_claim_ids = [claim_id for claim_id in supersede_claim_ids if claim_id not in active_claims]
        if unknown_claim_ids:
            raise PeerValidationError(
                "Correction plans may supersede only active claims from the current directional model"
            )

        correction_claims: list[PeerClaimWrite] = []
        correction_ids: list[str] = []
        provenance = payload.note or payload.plan.reason
        for draft in payload.plan.claims:
            claim_id = str(uuid.uuid4())
            correction_ids.append(claim_id)
            correction_claims.append(
                PeerClaimWrite(
                    id=claim_id,
                    claim_type=draft.claim_type,
                    text=draft.text,
                    confidence=draft.confidence,
                    origin=PeerClaimOrigin.MANUAL,
                    locked=True,
                    provenance=provenance,
                    source_kind=PeerSourceKind.MANUAL,
                    source_ids=[f"correction:{claim_id}"],
                )
            )

        plan = self._build_plan(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model=model,
            claims=claims,
            new_claims=correction_claims,
            supersede_claim_ids=supersede_claim_ids,
        )
        await self.repository.apply_materialization(plan)
        updated_model = await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        updated_claims = [
            claim
            for claim_id in correction_ids
            if (claim := await self.repository.get_claim(bank_id=bank_id, claim_id=claim_id)) is not None
        ]
        if updated_model is None or len(updated_claims) != len(correction_ids):
            raise PeerNotFoundError("Peer correction was not materialized")
        return PeerCorrectionResult(
            claims=updated_claims,
            superseded_claim_ids=supersede_claim_ids,
            model=updated_model,
        )

    async def rebuild(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        payload: PeerModelRequest | None = None,
    ) -> PeerModel:
        """Re-materialize a pair from its persisted claims without invoking an LLM."""
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        state = await self._load_model_and_claims(bank_id, observer_peer_id, target_peer_id)
        model, claims = state.model, state.claims
        if payload is not None and payload.claims:
            raise PeerValidationError(
                "Deterministic rebuild does not accept new claims; use model when the worker is enabled"
            )
        plan = self._build_plan(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model=model,
            claims=claims,
            new_claims=[],
            supersede_claim_ids=[],
        )
        if model is not None and not self._plan_needs_apply(plan, model, claims):
            return model
        await self.repository.apply_materialization(plan)
        rebuilt = await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        if rebuilt is None:
            raise PeerNotFoundError("Peer model was not materialized")
        return rebuilt

    async def model(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        payload: PeerModelRequest | None = None,
        *,
        source_cursor: datetime | None = None,
        source_cursor_id: str | None = None,
        validate_pair_sources: bool = False,
    ) -> PeerModel:
        """Materialize supplied evidence claims through the worker-safe domain path."""
        await self.validate_model_request(bank_id, observer_peer_id, target_peer_id, payload)
        state = await self._load_model_and_claims(bank_id, observer_peer_id, target_peer_id)
        model, claims = state.model, state.claims
        existing_derived = self._canonical_derived_claims(claims)
        new_claims: list[PeerClaimWrite] = []
        for draft in payload.claims if payload else []:
            existing = existing_derived.get((draft.claim_type, self._normalize_claim_text(draft.text)))
            existing_sources = {
                source.source_id
                for source in (existing.sources if existing else [])
                if source.source_kind == draft.source_kind
            }
            if existing is not None and set(draft.source_ids).issubset(existing_sources):
                continue
            new_claims.append(
                PeerClaimWrite(
                    id=str(uuid.uuid4()),
                    claim_type=draft.claim_type,
                    # Preserve the first persisted spelling when an exact normalized
                    # duplicate gains new evidence; the repository then reuses its ID.
                    text=existing.text if existing is not None else draft.text,
                    confidence=max(existing.confidence, draft.confidence) if existing is not None else draft.confidence,
                    origin=PeerClaimOrigin.DERIVED,
                    locked=False,
                    provenance="peer_modeling",
                    source_kind=draft.source_kind,
                    source_ids=draft.source_ids,
                )
            )
        plan = self._build_plan(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model=model,
            claims=claims,
            new_claims=new_claims,
            supersede_claim_ids=[],
            source_cursor=source_cursor,
            source_cursor_id=source_cursor_id,
        )
        pair_source_ids = self._pair_memory_source_ids(claims, plan) if validate_pair_sources else None
        if model is not None and not self._plan_needs_apply(plan, model, claims):
            if pair_source_ids is not None:
                await self.repository.validate_pair_memory_sources(
                    bank_id=bank_id,
                    observer_peer_id=observer_peer_id,
                    target_peer_id=target_peer_id,
                    source_ids=pair_source_ids,
                )
            if source_cursor is not None and source_cursor_id is not None:
                await self.repository.advance_source_cursor(
                    bank_id=bank_id,
                    observer_peer_id=observer_peer_id,
                    target_peer_id=target_peer_id,
                    source_cursor=source_cursor,
                    source_cursor_id=source_cursor_id,
                )
            return model
        if pair_source_ids is not None:
            await self.repository.apply_materialization(plan, pair_source_ids=pair_source_ids)
        else:
            await self.repository.apply_materialization(plan)
        materialized = await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        if materialized is None:
            raise PeerNotFoundError("Peer model was not materialized")
        return materialized

    @staticmethod
    def _normalize_claim_text(text: str) -> str:
        return " ".join(text.casefold().split())

    @staticmethod
    def _pair_memory_source_ids(claims: list[PeerClaim], plan: PeerMaterializationPlan) -> list[str]:
        """Collect every memory source contributing to the rebuilt projections."""
        source_ids = {
            source.source_id
            for claim in claims
            if claim.status == PeerClaimStatus.ACTIVE
            for source in claim.sources
            if source.source_kind == PeerSourceKind.MEMORY_UNIT
        }
        source_ids.update(
            source_id
            for claim in plan.claims
            if claim.source_kind == PeerSourceKind.MEMORY_UNIT
            for source_id in claim.source_ids
        )
        return sorted(source_ids)

    @staticmethod
    def _claim_age_key(claim: PeerClaim) -> tuple[datetime, str]:
        return claim.created_at, claim.id

    def _canonical_derived_claims(self, claims: list[PeerClaim]) -> dict[tuple[PeerClaimType, str], PeerClaim]:
        """Choose the deterministic canonical claim for each compactable key."""
        canonical: dict[tuple[PeerClaimType, str], PeerClaim] = {}
        eligible = sorted(
            (
                claim
                for claim in claims
                if claim.status == PeerClaimStatus.ACTIVE
                and claim.origin == PeerClaimOrigin.DERIVED
                and not claim.locked
            ),
            key=self._claim_age_key,
        )
        for claim in eligible:
            canonical.setdefault((claim.claim_type, self._normalize_claim_text(claim.text)), claim)
        return canonical

    async def validate_model_request(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        payload: PeerModelRequest | None,
    ) -> None:
        """Validate the pair/evidence boundary before a future LLM task is queued."""
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        for draft in payload.claims if payload else []:
            self._validate_draft_shape(draft)
            if draft.source_kind != PeerSourceKind.MEMORY_UNIT:
                raise PeerValidationError(
                    "Modeling claims require memory_unit evidence; use corrections for manual claims"
                )
            if draft.source_kind == PeerSourceKind.MEMORY_UNIT:
                await self._validate_memory_sources(bank_id, draft.source_ids)

    async def _ensure_pair(self, bank_id: str, observer_peer_id: str, target_peer_id: str) -> None:
        if not await self.repository.peer_pair_exists(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        ):
            raise PeerNotFoundError(
                f"Observer '{observer_peer_id}' and target '{target_peer_id}' must both belong to bank '{bank_id}'"
            )

    async def _load_model_and_claims(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
    ) -> _PeerModelState:
        model = await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        claims = await self.repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        return _PeerModelState(model=model, claims=claims or [])

    async def _validate_memory_sources(self, bank_id: str, source_ids: list[str]) -> None:
        if not source_ids or not await self.repository.memory_sources_exist(bank_id=bank_id, source_ids=source_ids):
            raise PeerValidationError("Every memory_unit claim must link to an existing memory in the same bank")

    @staticmethod
    def _validate_draft_shape(draft: PeerClaimDraft) -> None:
        if draft.source_kind == PeerSourceKind.MEMORY_UNIT and not draft.source_ids:
            raise PeerValidationError("Derived claims require at least one memory_unit source")
        if draft.source_kind == PeerSourceKind.MANUAL and draft.source_ids:
            raise PeerValidationError("Manual source ids are assigned by the correction service")

    def _build_plan(
        self,
        *,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        model: PeerModel | None,
        claims: list[PeerClaim],
        new_claims: list[PeerClaimWrite],
        supersede_claim_ids: list[str],
        reactivate_claim_ids: list[str] | None = None,
        source_cursor: datetime | None = None,
        source_cursor_id: str | None = None,
    ) -> PeerMaterializationPlan:
        version = model.version + 1 if model is not None else 1
        model_id = model.id if model is not None else str(uuid.uuid4())
        supersede_ids = set(supersede_claim_ids)
        reactivate_ids = set(reactivate_claim_ids or [])
        active_claims = [
            claim
            for claim in claims
            if (claim.status == PeerClaimStatus.ACTIVE and claim.id not in supersede_ids) or claim.id in reactivate_ids
        ]

        compactable: dict[tuple[PeerClaimType, str], list[PeerClaim]] = {}
        for claim in active_claims:
            if claim.status == PeerClaimStatus.ACTIVE and claim.origin == PeerClaimOrigin.DERIVED and not claim.locked:
                compactable.setdefault((claim.claim_type, self._normalize_claim_text(claim.text)), []).append(claim)
        canonical_by_key = {key: min(group, key=self._claim_age_key) for key, group in compactable.items()}
        compact_supersede_ids = {
            claim.id for key, group in compactable.items() for claim in group if claim.id != canonical_by_key[key].id
        }

        incoming_by_key: dict[tuple[PeerClaimType, str], list[PeerClaimWrite]] = {}
        new_claims_by_key: dict[tuple[PeerClaimType, str], PeerClaimWrite] = {}
        standalone_new_claims: list[PeerClaimWrite] = []
        for claim in new_claims:
            key = (claim.claim_type, self._normalize_claim_text(claim.text))
            if claim.origin == PeerClaimOrigin.DERIVED and not claim.locked:
                if key in canonical_by_key:
                    incoming_by_key.setdefault(key, []).append(claim)
                    continue
                previous = new_claims_by_key.get(key)
                if previous is None:
                    new_claims_by_key[key] = claim
                else:
                    new_claims_by_key[key] = previous.model_copy(
                        update={
                            "confidence": max(previous.confidence, claim.confidence),
                            "source_ids": list(dict.fromkeys([*previous.source_ids, *claim.source_ids])),
                        }
                    )
                continue
            standalone_new_claims.append(claim)

        plan_claims: list[PeerClaimWrite] = []
        projection_claims: list[_ProjectionClaim] = []
        for claim in active_claims:
            key = (claim.claim_type, self._normalize_claim_text(claim.text))
            if key not in compactable or claim.origin != PeerClaimOrigin.DERIVED or claim.locked:
                projection_claims.append(self._projection_claim(claim))
                continue
            canonical = canonical_by_key[key]
            if claim.id != canonical.id:
                continue
            group = compactable[key]
            incoming = incoming_by_key.get(key, [])
            max_confidence = max(
                [member.confidence for member in group] + [item.confidence for item in incoming],
                default=claim.confidence,
            )
            all_sources = [(source.source_kind, source.source_id) for member in group for source in member.sources]
            all_sources.extend((item.source_kind, source_id) for item in incoming for source_id in item.source_ids)
            existing_sources = {(source.source_kind, source.source_id) for source in claim.sources}
            source_delta = sorted(
                {source_pair for source_pair in all_sources if source_pair not in existing_sources},
                key=lambda pair: (pair[0].value, pair[1]),
            )
            if len(group) > 1 or incoming:
                plan_claims.extend(
                    self._canonical_writes(
                        claim,
                        confidence=max_confidence,
                        source_delta=source_delta,
                    )
                )
            projection_claims.append(
                _ProjectionClaim(
                    claim_type=claim.claim_type,
                    text=claim.text,
                    locked=claim.locked,
                    confidence=max_confidence,
                    claim_id=claim.id,
                    created_at=claim.created_at,
                )
            )

        for claim in [*new_claims_by_key.values(), *standalone_new_claims]:
            plan_claims.append(claim)
            projection_claims.append(
                _ProjectionClaim(
                    claim_type=claim.claim_type,
                    text=claim.text,
                    locked=claim.locked,
                    confidence=claim.confidence,
                    claim_id=claim.id,
                    created_at=datetime.max.replace(tzinfo=UTC),
                )
            )

        projection_claims = self._dedupe_projection_claims(projection_claims)
        card_candidates = [
            PeerCardEntry(
                claim_id=claim.claim_id,
                claim_type=claim.claim_type,
                text=claim.text,
                confidence=claim.confidence,
                locked=claim.locked,
            )
            for claim in projection_claims
            if claim.locked or claim.confidence >= 0.85
        ]
        card_candidates.sort(key=self._card_sort_key)
        locked_entries = [entry for entry in card_candidates if entry.locked]
        unlocked_entries = [entry for entry in card_candidates if not entry.locked]
        card_entries = locked_entries + unlocked_entries[: max(0, self.max_card_entries - len(locked_entries))]
        representation_candidates = [
            _RepresentationCandidate(
                claim_type=claim.claim_type,
                text=claim.text,
                locked=claim.locked,
                confidence=claim.confidence,
                claim_id=claim.claim_id,
            )
            for claim in projection_claims
        ]
        representation_candidates.sort(key=self._representation_sort_key)
        supersede_ids.update(compact_supersede_ids)
        return PeerMaterializationPlan(
            bank_id=bank_id,
            model_id=model_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            version=version,
            supersede_claim_ids=sorted(supersede_ids),
            reactivate_claim_ids=sorted(reactivate_ids),
            claims=plan_claims,
            card_entries=card_entries,
            representation=self._representation_text(representation_candidates),
            source_cursor=source_cursor,
            source_cursor_id=source_cursor_id,
        )

    @staticmethod
    def _projection_claim(claim: PeerClaim) -> _ProjectionClaim:
        return _ProjectionClaim(
            claim_type=claim.claim_type,
            text=claim.text,
            locked=claim.locked,
            confidence=claim.confidence,
            claim_id=claim.id,
            created_at=claim.created_at,
        )

    @staticmethod
    def _canonical_writes(
        claim: PeerClaim,
        *,
        confidence: float,
        source_delta: list[tuple[PeerSourceKind, str]],
    ) -> list[PeerClaimWrite]:
        by_kind: dict[PeerSourceKind, list[str]] = {}
        for source_kind, source_id in source_delta:
            if source_id not in by_kind.setdefault(source_kind, []):
                by_kind[source_kind].append(source_id)
        if not by_kind:
            source_kind = claim.sources[0].source_kind if claim.sources else PeerSourceKind.MEMORY_UNIT
            by_kind[source_kind] = []
        return [
            PeerClaimWrite(
                id=claim.id,
                claim_type=claim.claim_type,
                text=claim.text,
                confidence=confidence,
                origin=claim.origin,
                locked=claim.locked,
                provenance=claim.provenance,
                source_kind=source_kind,
                source_ids=source_ids,
            )
            for source_kind, source_ids in sorted(by_kind.items(), key=lambda item: item[0].value)
        ]

    def _dedupe_projection_claims(self, claims: list[_ProjectionClaim]) -> list[_ProjectionClaim]:
        """Deduplicate projections while retaining every locked claim."""
        seen: set[tuple[PeerClaimType, str]] = set()
        result: list[_ProjectionClaim] = []
        for claim in sorted(
            claims,
            key=lambda item: [
                0 if item.locked else 1,
                _CLAIM_TYPE_ORDER[item.claim_type],
                -item.confidence,
                item.created_at,
                item.claim_id,
            ],
        ):
            key = (claim.claim_type, self._normalize_claim_text(claim.text))
            if not claim.locked and key in seen:
                continue
            result.append(claim)
            seen.add(key)
        return result

    def _plan_needs_apply(self, plan: PeerMaterializationPlan, model: PeerModel, claims: list[PeerClaim]) -> bool:
        if plan.supersede_claim_ids or plan.reactivate_claim_ids:
            return True
        active_by_id = {claim.id: claim for claim in claims if claim.status == PeerClaimStatus.ACTIVE}
        for write in plan.claims:
            existing = active_by_id.get(write.id)
            if existing is None:
                return True
            if write.confidence > existing.confidence:
                return True
            existing_sources = {(source.source_kind, source.source_id) for source in existing.sources}
            if any((write.source_kind, source_id) not in existing_sources for source_id in write.source_ids):
                return True
        return model.card.entries != plan.card_entries or model.representation != plan.representation

    @staticmethod
    def _card_sort_key(entry: PeerCardEntry) -> list[int | float | str]:
        return [0 if entry.locked else 1, _CLAIM_TYPE_ORDER[entry.claim_type], -entry.confidence, entry.claim_id]

    @staticmethod
    def _representation_sort_key(entry: _RepresentationCandidate) -> list[int | float | str]:
        return [
            0 if entry.locked else 1,
            _CLAIM_TYPE_ORDER[entry.claim_type],
            -entry.confidence,
            entry.claim_id,
        ]

    def _representation_text(self, entries: Iterable[_RepresentationCandidate]) -> str:
        from hindsight_api.engine.memory_engine import _get_tiktoken_encoding

        encoding = _get_tiktoken_encoding()
        packed: list[str] = []
        for entry in entries:
            line = f"{entry.claim_type.value}: {entry.text}"
            candidate = "\n".join([*packed, line])
            if len(encoding.encode(candidate)) <= self.representation_max_tokens:
                packed.append(line)
        return "\n".join(packed)


__all__ = ["PeerModelingService"]
