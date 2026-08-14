"""Regression coverage for peer projections backed by deleted memories."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.engine.memory_engine import fq_table
from hindsight_api.engine.peer_modeling.models import (
    PeerClaimDraft,
    PeerClaimOrigin,
    PeerClaimStatus,
    PeerClaimType,
    PeerCorrectionApplyRequest,
    PeerCorrectionClaimDraft,
    PeerCorrectionPlan,
    PeerCreate,
    PeerModelRequest,
    PeerSourceKind,
)
from hindsight_api.engine.peer_modeling.repository import PeerRepository
from hindsight_api.engine.peer_modeling.service import PeerModelingService
from hindsight_api.engine.peer_modeling.source_cleanup import (
    _source_values,
    _text_source_binds,
    _text_source_in_list,
    invalidate_changed_memory_sources,
)


@pytest.mark.asyncio
async def test_delete_memory_supersedes_derived_claim_and_updates_projection(memory, request_context) -> None:
    """Deleting evidence must atomically remove every materialized peer projection of it."""
    bank_id = f"peer-source-delete-{uuid.uuid4()}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    backend = await memory._get_backend()
    repository = PeerRepository(backend)
    service = PeerModelingService(repository)
    observer = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-observer"))
    target = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-target"))
    source_id = str(uuid.uuid4())
    source_updated_at = datetime.now(UTC)

    try:
        async with backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'world', $4, $4, $4)
                """,
                uuid.UUID(source_id),
                bank_id,
                "Synthetic target uses a blue notebook.",
                source_updated_at,
            )

        initial = await service.model(
            bank_id,
            observer.id,
            target.id,
            PeerModelRequest(
                claims=[
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Uses a blue notebook",
                        confidence=0.95,
                        source_ids=[source_id],
                    )
                ]
            ),
            source_cursor=source_updated_at,
            source_cursor_id=source_id,
            validate_bank_sources=[source_id],
            expected_source_versions={source_id: source_updated_at},
            validate_existing_sources=True,
        )
        corrected = await service.apply_correction(
            bank_id,
            observer.id,
            target.id,
            PeerCorrectionApplyRequest(
                plan=PeerCorrectionPlan(
                    correction_text="Keep this synthetic manual note.",
                    base_model_version=initial.version,
                    claims=[
                        PeerCorrectionClaimDraft(
                            claim_type=PeerClaimType.INSTRUCTION,
                            text="Keeps a synthetic manual note",
                            confidence=1.0,
                        )
                    ],
                    supersede_claim_ids=[],
                    reason="Synthetic regression fixture",
                ),
                note="Synthetic regression fixture",
            ),
        )

        result = await memory.delete_memory_unit(source_id, bank_id=bank_id, request_context=request_context)
        assert result["success"] is True

        claims = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims is not None
        derived = next(claim for claim in claims if claim.origin == PeerClaimOrigin.DERIVED)
        manual = next(claim for claim in claims if claim.origin == PeerClaimOrigin.MANUAL)
        assert derived.status == PeerClaimStatus.SUPERSEDED
        assert derived.sources == []
        assert manual.status == PeerClaimStatus.ACTIVE
        assert manual.locked is True
        assert manual.sources[0].source_kind == PeerSourceKind.MANUAL

        model = await repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert model is not None
        assert model.version == corrected.model.version + 1
        assert [entry.claim_id for entry in model.card.entries] == [manual.id]
        assert "Uses a blue notebook" not in model.representation
        assert "Keeps a synthetic manual note" in model.representation
        assert model.source_cursor == datetime(1970, 1, 1, tzinfo=UTC)
        assert model.source_cursor_id is None
        await repository.validate_model_memory_sources(
            bank_id=bank_id,
            model_id=model.id,
            new_source_ids=[],
            expected_source_versions={},
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_delete_source_supersedes_claim_backed_by_derived_observation(memory, request_context) -> None:
    """Cascading observation cleanup must retire claims sourced by that observation."""
    bank_id = f"peer-observation-cascade-{uuid.uuid4()}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    backend = await memory._get_backend()
    repository = PeerRepository(backend)
    service = PeerModelingService(repository)
    observer = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-cascade-observer"))
    target = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-cascade-target"))
    source_id = str(uuid.uuid4())
    observation_id = str(uuid.uuid4())
    source_updated_at = datetime.now(UTC)
    observation_updated_at = datetime.now(UTC)

    try:
        async with backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'world', $4, $4, $4)
                """,
                uuid.UUID(source_id),
                bank_id,
                "Synthetic source for a derived observation.",
                source_updated_at,
            )
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, source_memory_ids, proof_count,
                     event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'observation', $4, 1, $5, $5, $5)
                """,
                uuid.UUID(observation_id),
                bank_id,
                "Synthetic derived observation.",
                [uuid.UUID(source_id)],
                observation_updated_at,
            )

        await service.model(
            bank_id,
            observer.id,
            target.id,
            PeerModelRequest(
                claims=[
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Has a synthetic observation-backed attribute",
                        confidence=0.95,
                        source_ids=[observation_id],
                    )
                ]
            ),
            source_cursor=observation_updated_at,
            source_cursor_id=observation_id,
            validate_bank_sources=[observation_id],
            expected_source_versions={observation_id: observation_updated_at},
            validate_existing_sources=True,
        )

        result = await memory.delete_memory_unit(source_id, bank_id=bank_id, request_context=request_context)
        assert result["success"] is True

        claims = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims is not None
        assert claims[0].status == PeerClaimStatus.SUPERSEDED
        assert claims[0].sources == []
        assert not await repository.memory_sources_exist(
            bank_id=bank_id,
            source_ids=[source_id, observation_id],
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["clear", "delete", "delete_bank_type"])
async def test_observation_removal_preserves_claim_backed_by_surviving_fact(
    memory, request_context, mutation: str
) -> None:
    """Removing an observation retires its claim without invalidating the surviving fact."""
    bank_id = f"peer-observation-removal-{mutation}-{uuid.uuid4()}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    backend = await memory._get_backend()
    repository = PeerRepository(backend)
    service = PeerModelingService(repository)
    observer = await service.create_peer(bank_id, PeerCreate(external_id=f"synthetic-{mutation}-observer"))
    target = await service.create_peer(bank_id, PeerCreate(external_id=f"synthetic-{mutation}-target"))
    source_id = str(uuid.uuid4())
    observation_id = str(uuid.uuid4())
    source_updated_at = datetime.now(UTC)
    observation_updated_at = datetime.now(UTC)

    try:
        async with backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'experience', $4, $4, $4)
                """,
                uuid.UUID(source_id),
                bank_id,
                "Synthetic surviving source fact.",
                source_updated_at,
            )
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, source_memory_ids, proof_count,
                     event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'observation', $4, 1, $5, $5, $5)
                """,
                uuid.UUID(observation_id),
                bank_id,
                "Synthetic removable observation.",
                [uuid.UUID(source_id)],
                observation_updated_at,
            )

        await service.model(
            bank_id,
            observer.id,
            target.id,
            PeerModelRequest(
                claims=[
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Keeps the surviving synthetic fact",
                        confidence=0.95,
                        source_ids=[source_id],
                    ),
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Uses the removable synthetic observation",
                        confidence=0.95,
                        source_ids=[observation_id],
                    ),
                ]
            ),
            source_cursor=observation_updated_at,
            source_cursor_id=observation_id,
            validate_bank_sources=[source_id, observation_id],
            expected_source_versions={
                source_id: source_updated_at,
                observation_id: observation_updated_at,
            },
            validate_existing_sources=True,
        )

        if mutation == "clear":
            with patch.object(memory, "submit_async_consolidation", new=AsyncMock()):
                result = await memory.clear_observations_for_memory(
                    bank_id,
                    source_id,
                    request_context=request_context,
                )
            assert result["deleted_count"] == 1
        elif mutation == "delete":
            result = await memory.delete_memory_unit(
                observation_id,
                bank_id=bank_id,
                request_context=request_context,
            )
            assert result["success"] is True
        else:
            result = await memory.delete_bank(
                bank_id,
                fact_type="observation",
                request_context=request_context,
            )
            assert result["memory_units_deleted"] == 1

        claims = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims is not None
        by_text = {claim.text: claim for claim in claims}
        surviving = by_text["Keeps the surviving synthetic fact"]
        removed = by_text["Uses the removable synthetic observation"]
        assert surviving.status == PeerClaimStatus.ACTIVE
        assert [source.source_id for source in surviving.sources] == [source_id]
        assert removed.status == PeerClaimStatus.SUPERSEDED
        assert removed.sources == []
        assert await repository.memory_sources_exist(bank_id=bank_id, source_ids=[source_id])
        assert not await repository.memory_sources_exist(bank_id=bank_id, source_ids=[observation_id])
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_delete_memory_detaches_locked_claim_source_without_dropping_projection(memory, request_context) -> None:
    """Locked content survives while its dead memory provenance is detached."""
    bank_id = f"peer-source-locked-{uuid.uuid4()}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    backend = await memory._get_backend()
    repository = PeerRepository(backend)
    service = PeerModelingService(repository)
    observer = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-locked-observer"))
    target = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-locked-target"))
    source_id = str(uuid.uuid4())
    source_updated_at = datetime.now(UTC)

    try:
        async with backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'experience', $4, $4, $4)
                """,
                uuid.UUID(source_id),
                bank_id,
                "Synthetic evidence for a locked claim.",
                source_updated_at,
            )

        initial = await service.model(
            bank_id,
            observer.id,
            target.id,
            PeerModelRequest(
                claims=[
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Keeps a synthetic locked attribute",
                        confidence=0.95,
                        source_ids=[source_id],
                    )
                ]
            ),
            source_cursor=source_updated_at,
            source_cursor_id=source_id,
            validate_bank_sources=[source_id],
            expected_source_versions={source_id: source_updated_at},
            validate_existing_sources=True,
        )
        claims_before = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims_before is not None
        claim_id = claims_before[0].id
        async with backend.acquire() as conn:
            await conn.execute(
                f"UPDATE {fq_table('peer_model_claims')} SET locked = $3 WHERE bank_id = $1 AND id = $2",
                bank_id,
                uuid.UUID(claim_id),
                True,
            )

        result = await memory.delete_memory_unit(source_id, bank_id=bank_id, request_context=request_context)
        assert result["success"] is True

        claims = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims is not None
        locked_claim = claims[0]
        assert locked_claim.id == claim_id
        assert locked_claim.status == PeerClaimStatus.ACTIVE
        assert locked_claim.locked is True
        assert locked_claim.sources == []

        model = await repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert model is not None
        assert model.version == initial.version + 1
        assert [entry.claim_id for entry in model.card.entries] == [claim_id]
        assert "Keeps a synthetic locked attribute" in model.representation
        assert model.source_cursor == datetime(1970, 1, 1, tzinfo=UTC)
        assert model.source_cursor_id is None
        await repository.validate_model_memory_sources(
            bank_id=bank_id,
            model_id=model.id,
            new_source_ids=[],
            expected_source_versions={},
        )
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


@pytest.mark.asyncio
async def test_document_reingest_invalidates_peer_claim_from_outgoing_memory(memory, request_context) -> None:
    """Full-replace retain must not leave peer claims pointing at the old document version."""
    from hindsight_api.engine.retain.fact_storage import handle_document_tracking

    bank_id = f"peer-source-reingest-{uuid.uuid4()}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    backend = await memory._get_backend()
    repository = PeerRepository(backend)
    service = PeerModelingService(repository)
    observer = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-reingest-observer"))
    target = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-reingest-target"))
    document_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    source_updated_at = datetime.now(UTC)

    try:
        async with backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table("documents")}
                    (id, bank_id, original_text, content_hash, created_at, updated_at)
                VALUES ($1, $2, $3, 'synthetic-old-hash', $4, $4)
                """,
                document_id,
                bank_id,
                "Synthetic old document version.",
                source_updated_at,
            )
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, document_id, event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'experience', $4, $5, $5, $5)
                """,
                uuid.UUID(source_id),
                bank_id,
                "Synthetic old document fact.",
                document_id,
                source_updated_at,
            )

        initial = await service.model(
            bank_id,
            observer.id,
            target.id,
            PeerModelRequest(
                claims=[
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Has a synthetic old-document attribute",
                        confidence=0.95,
                        source_ids=[source_id],
                    )
                ]
            ),
            source_cursor=source_updated_at,
            source_cursor_id=source_id,
            validate_bank_sources=[source_id],
            expected_source_versions={source_id: source_updated_at},
            validate_existing_sources=True,
        )

        async with backend.acquire() as conn:
            async with conn.transaction():
                await handle_document_tracking(
                    conn,
                    bank_id=bank_id,
                    document_id=document_id,
                    combined_content="Synthetic replacement document version.",
                    is_first_batch=True,
                    retain_params=None,
                    document_tags=None,
                    ops=backend.ops,
                )

        claims = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims is not None
        derived = next(claim for claim in claims if claim.origin == PeerClaimOrigin.DERIVED)
        assert derived.status == PeerClaimStatus.SUPERSEDED
        assert derived.sources == []

        model = await repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert model is not None
        assert model.version == initial.version + 1
        assert model.card.entries == []
        assert model.representation == ""
        assert model.source_cursor == datetime(1970, 1, 1, tzinfo=UTC)
        assert model.source_cursor_id is None
        assert not await repository.memory_sources_exist(bank_id=bank_id, source_ids=[source_id])
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)


def test_text_source_binds_preserve_uuid_shaped_and_external_ids_for_oracle() -> None:
    """Polymorphic source ids must stay text across Oracle's generic UUID converter."""
    from hindsight_api.engine.db.oracle import _convert_arg, _rewrite_pg_to_oracle

    uuid_shaped = str(uuid.uuid4())
    values = _source_values([uuid_shaped, "external-memory-1", uuid_shaped, ""])
    assert values == [uuid_shaped, "external-memory-1"]

    binds = _text_source_binds(values)
    assert binds == [f"~{uuid_shaped}", "~external-memory-1"]
    assert all(isinstance(_convert_arg(value), str) for value in binds)

    query = (
        f"SELECT source_id FROM peer_model_claim_sources WHERE source_id IN ({_text_source_in_list(start=1, count=2)})"
    )
    rewritten, ignore_dup, returning = _rewrite_pg_to_oracle(query)
    assert "SUBSTR(:1, 2)" in rewritten
    assert "SUBSTR(:2, 2)" in rewritten
    assert ignore_dup is False
    assert returning is None


@pytest.mark.asyncio
async def test_non_uuid_source_id_reaches_invalidation_query() -> None:
    """External-store text ids must not be silently discarded before cleanup."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.execute.return_value = "DELETE 3"

    result = await invalidate_changed_memory_sources(
        conn,
        bank_id="synthetic-bank",
        source_ids=["external-memory-1"],
    )

    assert result.claims_superseded == 0
    assert result.source_links_deleted == 3
    conn.execute.assert_awaited_once()
    first_fetch_args = conn.fetch.await_args_list[0].args
    assert first_fetch_args[-1] == "~external-memory-1"
    delete_args = conn.execute.await_args.args
    assert delete_args[-1] == "~external-memory-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["edit", "invalidate"])
async def test_curation_invalidates_peer_projection_for_changed_source(memory, request_context, mutation: str) -> None:
    """Editing or archiving evidence must invalidate claims derived from its old state."""
    bank_id = f"peer-source-curation-{uuid.uuid4()}"
    await memory.get_bank_profile(bank_id=bank_id, request_context=request_context)
    backend = await memory._get_backend()
    repository = PeerRepository(backend)
    service = PeerModelingService(repository)
    observer = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-curation-observer"))
    target = await service.create_peer(bank_id, PeerCreate(external_id="synthetic-curation-target"))
    source_id = str(uuid.uuid4())
    source_updated_at = datetime.now(UTC)

    try:
        async with backend.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {fq_table("memory_units")}
                    (id, bank_id, text, fact_type, event_date, created_at, updated_at)
                VALUES ($1, $2, $3, 'experience', $4, $4, $4)
                """,
                uuid.UUID(source_id),
                bank_id,
                "Synthetic curation source fact.",
                source_updated_at,
            )

        initial = await service.model(
            bank_id,
            observer.id,
            target.id,
            PeerModelRequest(
                claims=[
                    PeerClaimDraft(
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="Has a synthetic curation attribute",
                        confidence=0.95,
                        source_ids=[source_id],
                    )
                ]
            ),
            source_cursor=source_updated_at,
            source_cursor_id=source_id,
            validate_bank_sources=[source_id],
            expected_source_versions={source_id: source_updated_at},
            validate_existing_sources=True,
        )

        if mutation == "edit":
            result = await memory.update_memory_unit(
                bank_id,
                source_id,
                text="Synthetic revised curation source fact.",
                request_context=request_context,
            )
        else:
            result = await memory.update_memory_unit(
                bank_id,
                source_id,
                state="invalidated",
                reason="Synthetic invalidation reason.",
                request_context=request_context,
            )
        assert result is not None

        claims = await repository.get_directional_claims(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert claims is not None
        derived = next(claim for claim in claims if claim.origin == PeerClaimOrigin.DERIVED)
        assert derived.status == PeerClaimStatus.SUPERSEDED
        assert derived.sources == []

        model = await repository.get_directional_model(
            bank_id=bank_id,
            observer_peer_id=observer.id,
            target_peer_id=target.id,
        )
        assert model is not None
        assert model.version == initial.version + 1
        assert model.card.entries == []
        assert model.representation == ""
        assert model.source_cursor == datetime(1970, 1, 1, tzinfo=UTC)
        assert model.source_cursor_id is None
        source_exists = await repository.memory_sources_exist(bank_id=bank_id, source_ids=[source_id])
        assert source_exists is (mutation == "edit")
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)
