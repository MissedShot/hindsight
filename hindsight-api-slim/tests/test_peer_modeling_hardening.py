"""Deterministic production invariants for native peer modeling."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine, _get_tiktoken_encoding
from hindsight_api.engine.peer_modeling.bootstrap import _FinalClaim, _validated_final_evidence
from hindsight_api.engine.peer_modeling.models import (
    PeerClaimOrigin,
    PeerClaimType,
    PeerClaimWrite,
    PeerModelRequest,
    PeerPendingMemorySources,
    PeerSourceKind,
)
from hindsight_api.engine.peer_modeling.repository import PeerRepository
from hindsight_api.engine.peer_modeling.service import PeerModelingService
from hindsight_api.engine.retain.types import RetainContentDict
from hindsight_api.engine.transfer.export import export_bank


@dataclass
class _AutoPeerConfig:
    enable_peer_modeling: bool = True
    enable_auto_peer_modeling: bool = True
    peer_model_min_new_facts: int = 1
    peer_model_cooldown_seconds: int = 3600


class _AutoPeerRepository:
    def __init__(self, pending_sources: PeerPendingMemorySources) -> None:
        peer_ids = {"observer-reference": "observer-id", "target-reference": "target-id"}

        async def resolve_peer_id(*, bank_id: str, reference: str) -> str | None:
            del bank_id
            return peer_ids.get(reference)

        self.resolve_peer_id = AsyncMock(side_effect=resolve_peer_id)
        self.get_directional_model = AsyncMock(return_value=None)
        self.get_pending_memory_sources = AsyncMock(return_value=pending_sources)


@dataclass
class _AutoPeerHarness:
    engine: MemoryEngine
    repository: _AutoPeerRepository


def _make_auto_peer_harness(pending_sources: PeerPendingMemorySources) -> _AutoPeerHarness:
    repository = _AutoPeerRepository(pending_sources)
    service = SimpleNamespace(repository=repository)
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._config_resolver = SimpleNamespace(
        resolve_full_config=AsyncMock(return_value=_AutoPeerConfig()),
    )
    engine._peer_modeling_service = AsyncMock(return_value=service)
    engine.submit_async_peer_modeling = AsyncMock()
    return _AutoPeerHarness(engine=engine, repository=repository)


def _auto_peer_contents() -> list[RetainContentDict]:
    contents: list[RetainContentDict] = [
        {
            "content": "Observer discussed the target.",
            "peer_context": {
                "modality": "actual",
                "observer_peer_id": "observer-reference",
                "subject_peer_ids": ["target-reference"],
            },
        }
    ]
    return contents


async def _assert_auto_peer_submission(unit_ids: list[list[str]]) -> None:
    cursor = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    pending_sources = PeerPendingMemorySources(
        source_ids=["persisted-source-id"],
        next_cursor=cursor,
        next_cursor_id="persisted-cursor-id",
    )
    harness = _make_auto_peer_harness(pending_sources)
    request_context = cast(Any, object())

    await harness.engine._submit_auto_peer_modeling(
        bank_id="bank-id",
        contents=_auto_peer_contents(),
        unit_ids=unit_ids,
        request_context=request_context,
    )

    harness.repository.resolve_peer_id.assert_has_awaits(
        [
            call(bank_id="bank-id", reference="observer-reference"),
            call(bank_id="bank-id", reference="target-reference"),
        ]
    )
    harness.repository.get_pending_memory_sources.assert_awaited_once_with(
        bank_id="bank-id",
        observer_peer_id="observer-id",
        target_peer_id="target-id",
    )
    submitted = cast(Any, harness.engine.submit_async_peer_modeling).await_args
    assert submitted.args[:3] == ("bank-id", "observer-id", "target-id")
    assert isinstance(submitted.args[3], PeerModelRequest)
    assert submitted.kwargs == {
        "request_context": request_context,
        "_auto_source_ids": ["persisted-source-id"],
        "_auto_source_cursor": cursor,
        "_auto_source_cursor_id": "persisted-cursor-id",
    }


@pytest.mark.asyncio
async def test_auto_peer_modeling_ignores_empty_retain_unit_ids() -> None:
    await _assert_auto_peer_submission([])


@pytest.mark.asyncio
async def test_auto_peer_modeling_ignores_mismatched_retain_unit_ids() -> None:
    await _assert_auto_peer_submission([["retain-unit-id-1"], ["retain-unit-id-2"]])


def test_representation_respects_configured_token_cap():
    service = PeerModelingService(
        cast(PeerRepository, object()),
        representation_max_tokens=12,
    )
    plan = service._build_plan(
        bank_id="bank",
        observer_peer_id="11111111-1111-1111-1111-111111111111",
        target_peer_id="22222222-2222-2222-2222-222222222222",
        model=None,
        claims=[],
        new_claims=[
            PeerClaimWrite(
                id="33333333-3333-3333-3333-333333333333",
                claim_type=PeerClaimType.ATTRIBUTE,
                text="restores vintage radios during bright summer weekends " * 8,
                confidence=0.7,
                origin=PeerClaimOrigin.DERIVED,
                locked=False,
                provenance="test",
                source_kind=PeerSourceKind.MEMORY_UNIT,
                source_ids=["44444444-4444-4444-4444-444444444444"],
            )
        ],
        supersede_claim_ids=[],
    )

    assert len(_get_tiktoken_encoding().encode(plan.representation)) <= 12


def test_card_selection_rotates_across_available_claim_types() -> None:
    service = PeerModelingService(cast(PeerRepository, object()), max_card_entries=4)
    claim_types = [
        PeerClaimType.IDENTITY,
        PeerClaimType.IDENTITY,
        PeerClaimType.IDENTITY,
        PeerClaimType.IDENTITY,
        PeerClaimType.ATTRIBUTE,
        PeerClaimType.RELATIONSHIP,
        PeerClaimType.INSTRUCTION,
    ]
    plan = service._build_plan(
        bank_id="bank",
        observer_peer_id="11111111-1111-1111-1111-111111111111",
        target_peer_id="22222222-2222-2222-2222-222222222222",
        model=None,
        claims=[],
        new_claims=[
            PeerClaimWrite(
                id=f"33333333-3333-3333-3333-{index:012d}",
                claim_type=claim_type,
                text=f"Synthetic {claim_type.value.lower()} claim {index}",
                confidence=0.95,
                origin=PeerClaimOrigin.DERIVED,
                locked=False,
                provenance="test",
                source_kind=PeerSourceKind.MEMORY_UNIT,
                source_ids=["44444444-4444-4444-4444-444444444444"],
            )
            for index, claim_type in enumerate(claim_types, start=1)
        ],
        supersede_claim_ids=[],
    )

    assert [entry.claim_type for entry in plan.card_entries] == [
        PeerClaimType.IDENTITY,
        PeerClaimType.ATTRIBUTE,
        PeerClaimType.RELATIONSHIP,
        PeerClaimType.INSTRUCTION,
    ]


def test_pattern_source_minimum_is_enforced_after_llm_output():
    direct = _FinalClaim(
        claim_type=PeerClaimType.IDENTITY,
        text="Name: Morgan",
        confidence=0.95,
        source_ids=["source-1"],
        card_eligible=True,
    )
    pattern = _FinalClaim(
        claim_type=PeerClaimType.ATTRIBUTE,
        text="Usually prefers concise reports",
        confidence=0.7,
        source_ids=["source-1"],
        card_eligible=False,
    )

    assert _validated_final_evidence(direct, source_pool={"source-1"}, min_pattern_sources=2) == ["source-1"]
    assert _validated_final_evidence(pattern, source_pool={"source-1"}, min_pattern_sources=2) == []
    pattern.source_ids.append("source-2")
    assert _validated_final_evidence(
        pattern,
        source_pool={"source-1", "source-2"},
        min_pattern_sources=2,
    ) == ["source-1", "source-2"]


class _PeerStateConnection:
    async def fetchval(self, *_args, **_kwargs):
        return True


@pytest.mark.asyncio
async def test_whole_bank_export_rejects_unportable_peer_state():
    with pytest.raises(ValueError, match="does not yet preserve native peer-modeling state"):
        await export_bank(_PeerStateConnection(), "bank-with-peers")


def test_static_openapi_contains_peer_correction_plan_contract():
    spec_path = Path(__file__).parents[2] / "hindsight-docs" / "static" / "openapi.json"
    spec = json.loads(spec_path.read_text())
    path = "/v1/default/banks/{bank_id}/peers/{observer_peer_id}/corrections/{target_peer_id}/plan"

    operation = spec["paths"][path]["post"]
    assert operation["operationId"] == "plan_peer_correction"
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PeerCorrectionRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PeerCorrectionPlan-Output"
    }
