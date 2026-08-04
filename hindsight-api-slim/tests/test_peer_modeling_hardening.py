"""Deterministic production invariants for native peer modeling."""

import json
from pathlib import Path
from typing import cast

import pytest

from hindsight_api.engine.memory_engine import _get_tiktoken_encoding
from hindsight_api.engine.peer_modeling.bootstrap import _FinalClaim, _validated_final_evidence
from hindsight_api.engine.peer_modeling.models import (
    PeerClaimOrigin,
    PeerClaimType,
    PeerClaimWrite,
    PeerSourceKind,
)
from hindsight_api.engine.peer_modeling.repository import PeerRepository
from hindsight_api.engine.peer_modeling.service import PeerModelingService
from hindsight_api.engine.transfer.export import export_bank


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
