"""Bank-scoped peer-modeling service.

This module intentionally stops at a deterministic materialization boundary.  A future
LLM worker can produce ``PeerClaimDraft`` values and reuse the same validation and
repository transaction without changing the public storage or response models.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

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
    PeerCorrectionRequest,
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


class PeerModelingService:
    """Validate peer operations and build deterministic materialized projections."""

    def __init__(self, repository: PeerRepository, *, max_card_entries: int = MAX_PEER_CARD_ENTRIES):
        self.repository = repository
        self.max_card_entries = max(1, max_card_entries)

    async def create_peer(self, bank_id: str, payload: PeerCreate) -> Peer:
        peer_id = str(uuid.uuid4())
        try:
            return await self.repository.create_peer(
                bank_id=bank_id,
                peer_id=peer_id,
                external_id=payload.external_id,
                display_name=payload.display_name,
                kind=payload.kind,
                metadata=payload.metadata,
            )
        except PeerConflictError:
            raise
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise PeerConflictError(
                    f"Peer external_id '{payload.external_id}' already exists in bank '{bank_id}'"
                ) from exc
            raise

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
        claims = [claim for claim in await self.get_claims(bank_id, observer_peer_id, target_peer_id) if claim.status == PeerClaimStatus.ACTIVE]
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
        payload: PeerCorrectionRequest,
    ) -> PeerCorrectionResult:
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        draft = payload.claim
        if draft.source_kind not in (PeerSourceKind.MANUAL, PeerSourceKind.MEMORY_UNIT):
            raise PeerValidationError("Corrections must use manual or memory_unit provenance")

        model, claims = await self._load_model_and_claims(bank_id, observer_peer_id, target_peer_id)
        claim_id = str(uuid.uuid4())
        source_ids = list(draft.source_ids)
        source_kind = draft.source_kind
        if source_kind == PeerSourceKind.MEMORY_UNIT:
            await self._validate_memory_sources(bank_id, source_ids)
        else:
            # A manual correction is itself the evidence edge.  The stable synthetic
            # source id keeps provenance relational without pretending a memory exists.
            source_ids = [f"correction:{claim_id}"]

        correction = PeerClaimWrite(
            id=claim_id,
            claim_type=draft.claim_type,
            text=draft.text,
            confidence=draft.confidence,
            origin=PeerClaimOrigin.MANUAL,
            locked=True,
            provenance=payload.note,
            source_kind=source_kind,
            source_ids=source_ids,
        )
        plan = self._build_plan(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model=model,
            claims=claims,
            new_claims=[correction],
            supersede_claim_type=draft.claim_type,
        )
        await self.repository.apply_materialization(plan)
        updated_model = await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        updated_claim = await self.repository.get_claim(bank_id=bank_id, claim_id=claim_id)
        if updated_model is None or updated_claim is None:
            raise PeerNotFoundError("Peer correction was not materialized")
        return PeerCorrectionResult(claim=updated_claim, model=updated_model)

    async def rebuild(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        payload: PeerModelRequest | None = None,
    ) -> PeerModel:
        """Re-materialize a pair from its persisted claims without invoking an LLM."""
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        model, claims = await self._load_model_and_claims(bank_id, observer_peer_id, target_peer_id)
        if payload is not None and payload.claims:
            raise PeerValidationError("Deterministic rebuild does not accept new claims; use model when the worker is enabled")
        plan = self._build_plan(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model=model,
            claims=claims,
            new_claims=[],
            supersede_claim_type=None,
        )
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
    ) -> PeerModel:
        """Materialize supplied evidence claims through the worker-safe domain path."""
        await self.validate_model_request(bank_id, observer_peer_id, target_peer_id, payload)
        model, claims = await self._load_model_and_claims(bank_id, observer_peer_id, target_peer_id)
        existing_derived = {
            (claim.claim_type, claim.text): claim
            for claim in claims
            if claim.origin == PeerClaimOrigin.DERIVED and claim.status == PeerClaimStatus.ACTIVE
        }
        new_claims: list[PeerClaimWrite] = []
        for draft in (payload.claims if payload else []):
            existing = existing_derived.get((draft.claim_type, draft.text))
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
                    text=draft.text,
                    confidence=draft.confidence,
                    origin=PeerClaimOrigin.DERIVED,
                    locked=False,
                    provenance="peer_modeling",
                    source_kind=draft.source_kind,
                    source_ids=draft.source_ids,
                )
            )
        if model is not None and not new_claims:
            return model
        plan = self._build_plan(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            model=model,
            claims=claims,
            new_claims=new_claims,
            supersede_claim_type=None,
        )
        await self.repository.apply_materialization(plan)
        materialized = await self.repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
        )
        if materialized is None:
            raise PeerNotFoundError("Peer model was not materialized")
        return materialized

    async def validate_model_request(
        self,
        bank_id: str,
        observer_peer_id: str,
        target_peer_id: str,
        payload: PeerModelRequest | None,
    ) -> None:
        """Validate the pair/evidence boundary before a future LLM task is queued."""
        await self._ensure_pair(bank_id, observer_peer_id, target_peer_id)
        for draft in (payload.claims if payload else []):
            self._validate_draft_shape(draft)
            if draft.source_kind != PeerSourceKind.MEMORY_UNIT:
                raise PeerValidationError("Modeling claims require memory_unit evidence; use corrections for manual claims")
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
    ) -> tuple[PeerModel | None, list[PeerClaim]]:
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
        return model, claims or []

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
        supersede_claim_type: PeerClaimType | None,
    ) -> PeerMaterializationPlan:
        version = model.version + 1 if model is not None else 1
        model_id = model.id if model is not None else str(uuid.uuid4())
        active_claims = [claim for claim in claims if claim.status == PeerClaimStatus.ACTIVE]
        card_candidates: list[PeerCardEntry] = []
        representation_candidates: list[tuple[PeerClaimType, str, bool, float, str]] = []

        for claim in active_claims:
            if supersede_claim_type == claim.claim_type and not claim.locked:
                continue
            if claim.locked or claim.confidence >= 0.85:
                card_candidates.append(
                    PeerCardEntry(
                        claim_id=claim.id,
                        claim_type=claim.claim_type,
                        text=claim.text,
                        confidence=claim.confidence,
                        locked=claim.locked,
                    )
                )
            representation_candidates.append(
                (claim.claim_type, claim.text, claim.locked, claim.confidence, claim.id)
            )

        for claim in new_claims:
            if claim.locked or claim.confidence >= 0.85:
                card_candidates.append(
                    PeerCardEntry(
                        claim_id=claim.id,
                        claim_type=claim.claim_type,
                        text=claim.text,
                        confidence=claim.confidence,
                        locked=claim.locked,
                    )
                )
            representation_candidates.append(
                (claim.claim_type, claim.text, claim.locked, claim.confidence, claim.id)
            )

        card_candidates.sort(key=self._card_sort_key)
        representation_candidates.sort(key=self._representation_sort_key)
        card_candidates = list({(entry.claim_type, entry.text): entry for entry in reversed(card_candidates)}.values())
        card_candidates.sort(key=self._card_sort_key)
        representation_candidates = list(
            {
                (entry[0], entry[1]): entry
                for entry in reversed(representation_candidates)
            }.values()
        )
        representation_candidates.sort(key=self._representation_sort_key)
        card_entries = card_candidates[: self.max_card_entries]
        representation = self._representation_text(representation_candidates)
        return PeerMaterializationPlan(
            bank_id=bank_id,
            model_id=model_id,
            observer_peer_id=observer_peer_id,
            target_peer_id=target_peer_id,
            version=version,
            supersede_claim_type=supersede_claim_type,
            claims=new_claims,
            card_entries=card_entries,
            representation=representation,
        )

    @staticmethod
    def _card_sort_key(entry: PeerCardEntry) -> tuple[int, int, float, str]:
        return (_CLAIM_TYPE_ORDER[entry.claim_type], 0 if entry.locked else 1, -entry.confidence, entry.claim_id)

    @staticmethod
    def _representation_sort_key(entry: tuple[PeerClaimType, str, bool, float, str]) -> tuple[int, int, float, str]:
        claim_type, _, locked, confidence, claim_id = entry
        return (_CLAIM_TYPE_ORDER[claim_type], 0 if locked else 1, -confidence, claim_id)

    @staticmethod
    def _representation_text(entries: Iterable[tuple[PeerClaimType, str, bool, float, str]]) -> str:
        grouped: dict[PeerClaimType, list[str]] = {claim_type: [] for claim_type in PeerClaimType}
        for claim_type, text, _, _, _ in entries:
            grouped[claim_type].append(text)
        sections = [
            f"{claim_type.value}: " + "; ".join(grouped[claim_type])
            for claim_type in PeerClaimType
            if grouped[claim_type]
        ]
        return "\n".join(sections)


__all__ = ["PeerModelingService"]
