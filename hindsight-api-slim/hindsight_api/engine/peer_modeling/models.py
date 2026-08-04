"""Typed domain models for bank-scoped directional peer modeling.

The first slice deliberately keeps model materialization deterministic. Claims supplied by a
caller are validated and persisted with relational source links; a later LLM updater can consume
these same task and claim models without changing the storage contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PeerClaimType(StrEnum):
    """Taxonomy allowed in the compact peer card."""

    IDENTITY = "IDENTITY"
    ATTRIBUTE = "ATTRIBUTE"
    RELATIONSHIP = "RELATIONSHIP"
    INSTRUCTION = "INSTRUCTION"


class PeerClaimStatus(StrEnum):
    """Lifecycle state of an atomic peer claim."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CONTESTED = "contested"
    RETRACTED = "retracted"


class PeerClaimOrigin(StrEnum):
    """How a claim entered the model."""

    DERIVED = "derived"
    MANUAL = "manual"
    IMPORTED = "imported"


class PeerSourceKind(StrEnum):
    """Portable source-link namespaces supported by the first slice."""

    MEMORY_UNIT = "memory_unit"
    MANUAL = "manual"


class PeerModelOperationKind(StrEnum):
    """Deterministic operation modes exposed by the API."""

    MODEL = "model"
    REBUILD = "rebuild"


class PeerModelBase(BaseModel):
    """Base model with strict known fields and timezone-safe timestamps."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("created_at", "updated_at", "valid_from", "valid_until", mode="before", check_fields=False)
    @classmethod
    def ensure_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class PeerCreate(PeerModelBase):
    """Request to create one bank-scoped peer identity."""

    external_id: str = Field(min_length=1, max_length=512)
    display_name: str | None = Field(default=None, max_length=512)
    kind: str = Field(default="person", min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PeerUpdate(PeerModelBase):
    """Mutable peer fields; external_id remains the stable identity key."""

    display_name: str | None = Field(default=None, max_length=512)
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    metadata: dict[str, Any] | None = None


class Peer(PeerModelBase):
    """A peer identity scoped to exactly one bank."""

    id: str
    bank_id: str
    external_id: str
    display_name: str | None
    kind: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PeerSource(PeerModelBase):
    """One relational provenance edge for a claim."""

    source_kind: PeerSourceKind
    source_id: str


class PeerClaimDraft(PeerModelBase):
    """Caller-supplied claim proposal before origin/status are assigned by the engine."""

    claim_type: PeerClaimType
    text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    source_kind: PeerSourceKind = PeerSourceKind.MEMORY_UNIT
    source_ids: list[str] = Field(default_factory=list, max_length=64)


class PeerModelTask(PeerModelBase):
    """Serialized task payload crossing the async-operation worker boundary."""

    type: Literal["peer_modeling"] = "peer_modeling"
    operation_id: str
    bank_id: str
    observer_peer_id: str
    target_peer_id: str
    operation_kind: PeerModelOperationKind
    claims: list[PeerClaimDraft] = Field(default_factory=list, max_length=256)
    tenant_id: str | None = None
    api_key_id: str | None = None


class PeerClaim(PeerModelBase):
    """Persisted atomic claim with explicit lifecycle and provenance."""

    id: str
    bank_id: str
    model_id: str
    observer_peer_id: str
    target_peer_id: str
    claim_type: PeerClaimType
    text: str
    status: PeerClaimStatus
    origin: PeerClaimOrigin
    confidence: float
    locked: bool
    provenance: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime
    updated_at: datetime
    sources: list[PeerSource] = Field(default_factory=list)


class PeerCardEntry(PeerModelBase):
    """Typed compact-card projection of a claim."""

    claim_id: str
    claim_type: PeerClaimType
    text: str
    confidence: float
    locked: bool


class PeerCard(PeerModelBase):
    """Stable, compact projection limited to the four allowed claim types."""

    model_id: str
    bank_id: str
    observer_peer_id: str
    target_peer_id: str
    version: int
    entries: list[PeerCardEntry] = Field(default_factory=list)
    updated_at: datetime


class PeerRepresentation(PeerModelBase):
    """Richer deterministic representation for the directional pair."""

    model_id: str
    bank_id: str
    observer_peer_id: str
    target_peer_id: str
    version: int
    text: str
    updated_at: datetime


class PeerContext(PeerModelBase):
    """Typed context projection used by callers before a future retain seam exists."""

    bank_id: str
    observer_peer_id: str
    target_peer_id: str
    model_id: str
    version: int
    card: PeerCard
    representation: str
    claims: list[PeerClaim] = Field(default_factory=list)


class PeerModel(PeerModelBase):
    """Materialized directional model without untyped card payloads."""

    id: str
    bank_id: str
    observer_peer_id: str
    target_peer_id: str
    version: int
    card: PeerCard
    representation: str
    created_at: datetime
    updated_at: datetime


class PeerList(PeerModelBase):
    """Paginated peer collection."""

    items: list[Peer] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PeerClaims(PeerModelBase):
    """Directional claim collection."""

    observer_peer_id: str
    target_peer_id: str
    items: list[PeerClaim] = Field(default_factory=list)


class PeerModelRequest(PeerModelBase):
    """Request body for model/rebuild; claims are optional supplied evidence proposals."""

    claims: list[PeerClaimDraft] = Field(default_factory=list, max_length=256)


class PeerCorrectionRequest(PeerModelBase):
    """Natural-language correction to interpret against the current directional model."""

    text: str = Field(min_length=1, max_length=4000)


class PeerCorrectionClaimDraft(PeerModelBase):
    """One stable claim proposed by the semantic correction planner."""

    claim_type: PeerClaimType
    text: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class PeerCorrectionPlanDraft(PeerModelBase):
    """Strict structured output produced by the correction-planning LLM."""

    claims: list[PeerCorrectionClaimDraft] = Field(default_factory=list, min_length=1, max_length=16)
    supersede_claim_ids: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=4000)


class PeerCorrectionPlan(PeerCorrectionPlanDraft):
    """Version-bound correction plan shown to the caller before any mutation."""

    correction_text: str = Field(min_length=1, max_length=4000)
    base_model_version: int = Field(ge=1)


class PeerCorrectionApplyRequest(PeerModelBase):
    """Explicit request to apply a previously reviewed semantic correction plan."""

    plan: PeerCorrectionPlan
    note: str | None = Field(default=None, max_length=4000)


class PeerCorrectionResult(PeerModelBase):
    """Atomic targeted correction result and its rematerialized directional model."""

    claims: list[PeerClaim] = Field(default_factory=list)
    superseded_claim_ids: list[str] = Field(default_factory=list)
    model: PeerModel


class PeerOperation(PeerModelBase):
    """Stable async-operation acknowledgement."""

    operation_id: str
    deduplicated: bool = False


class PeerMaterializationResult(PeerModelBase):
    """Deterministic task result persisted in operation metadata."""

    model_id: str
    version: int
    claims_added: int
    card_entries: int


class PeerPendingMemorySources(PeerModelBase):
    """Exact incremental evidence window and its composite checkpoint."""

    source_ids: list[str] = Field(default_factory=list)
    next_cursor: datetime | None = None
    next_cursor_id: str | None = None


class PeerClaimWrite(PeerModelBase):
    """Validated claim row to be inserted by one atomic materialization."""

    id: str
    claim_type: PeerClaimType
    text: str
    status: PeerClaimStatus = PeerClaimStatus.ACTIVE
    origin: PeerClaimOrigin = PeerClaimOrigin.DERIVED
    confidence: float = Field(ge=0.0, le=1.0)
    locked: bool = False
    provenance: str | None = None
    source_kind: PeerSourceKind
    source_ids: list[str] = Field(default_factory=list)


class PeerMaterializationPlan(PeerModelBase):
    """Complete typed write-set for a directional model transaction."""

    bank_id: str
    model_id: str
    observer_peer_id: str
    target_peer_id: str
    version: int
    rebuild: bool = False
    supersede_claim_ids: list[str] = Field(default_factory=list)
    reactivate_claim_ids: list[str] = Field(default_factory=list)
    claims: list[PeerClaimWrite] = Field(default_factory=list)
    card_entries: list[PeerCardEntry] = Field(default_factory=list)
    representation: str
    source_cursor: datetime | None = None
    source_cursor_id: str | None = None


class PeerOperationMetadata(PeerModelBase):
    """Known result-metadata fields for a peer-modeling operation."""

    peer_model: PeerMaterializationResult


MAX_PEER_CARD_ENTRIES = 12
