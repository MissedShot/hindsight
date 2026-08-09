"""RED coverage for bounded incremental bootstrap refresh semantics."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.peer_modeling.bootstrap import (
    _ClaimBatch,
    _extract_claim_batch,
    _ExtractedClaim,
    _FinalClaim,
    _FinalClaims,
    _IncrementalFinalClaim,
    _IncrementalFinalClaims,
    _synthesize_claims,
    distill_directional_claims,
)
from hindsight_api.engine.peer_modeling.models import (
    Peer,
    PeerCard,
    PeerClaim,
    PeerClaimDelta,
    PeerClaimDraft,
    PeerClaimOrigin,
    PeerClaimStatus,
    PeerClaimType,
    PeerModel,
    PeerSource,
    PeerSourceKind,
)
from hindsight_api.engine.peer_modeling.refresh import refresh_existing_peer_models
from hindsight_api.engine.peer_modeling.repository import PeerRepository
from hindsight_api.engine.peer_modeling.service import PeerModelingService
from hindsight_api.worker.exceptions import DeferOperation

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
OBSERVER_ID = "observer"
TARGET_A_ID = "target-a"
TARGET_B_ID = "target-b"


def _peer(peer_id: str, external_id: str) -> Peer:
    return Peer(
        id=peer_id,
        bank_id="bank",
        external_id=external_id,
        display_name=external_id.title(),
        kind="person",
        metadata={},
        created_at=NOW - timedelta(days=3),
        updated_at=NOW - timedelta(days=2),
    )


def _model(observer_id: str, target_id: str, *, version: int = 3) -> PeerModel:
    updated_at = NOW - timedelta(days=1)
    card = PeerCard(
        model_id=f"model-{observer_id}-{target_id}",
        bank_id="bank",
        observer_peer_id=observer_id,
        target_peer_id=target_id,
        version=version,
        entries=[],
        updated_at=updated_at,
    )
    return PeerModel(
        id=card.model_id,
        bank_id="bank",
        observer_peer_id=observer_id,
        target_peer_id=target_id,
        version=version,
        card=card,
        representation="old representation",
        created_at=updated_at - timedelta(days=1),
        updated_at=updated_at,
    )


def _claim(source_id: str, text: str = "new claim") -> PeerClaimDraft:
    return PeerClaimDraft(
        claim_type=PeerClaimType.ATTRIBUTE,
        text=text,
        confidence=0.9,
        source_ids=[source_id],
    )


def _memory_source(source_id: str, updated_at: datetime, text: str, context: str = "") -> Any:
    return SimpleNamespace(id=source_id, updated_at=updated_at, text=text, context=context, fact_type="observation")


def _serialized_extraction_user_content(text: str) -> str:
    return json.dumps(
        {
            "observer": "observer",
            "allowed_peers": [
                {
                    "external_id": "target-a",
                    "display_name": "Target-A",
                    "aliases": ["target-a"],
                }
            ],
            "evidence": [{"id": "source", "text": text}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _current_claim(text: str, claim_id: str = "current") -> Any:
    return SimpleNamespace(
        id=claim_id,
        claim_type=PeerClaimType.ATTRIBUTE,
        text=text,
        confidence=0.8,
        locked=False,
        origin=PeerClaimOrigin.DERIVED,
        sources=[],
    )


class _WindowRepository:
    def __init__(self, models: list[PeerModel], windows: dict[tuple[str, str], list[Any]]) -> None:
        self.models = models
        self.windows = windows
        self.peers = {
            OBSERVER_ID: _peer(OBSERVER_ID, "observer"),
            TARGET_A_ID: _peer(TARGET_A_ID, "target-a"),
            TARGET_B_ID: _peer(TARGET_B_ID, "target-b"),
        }
        self.claims: dict[tuple[str, str], list[PeerClaim]] = {}
        self.model_calls: list[dict[str, Any]] = []
        self.advance_calls: list[dict[str, Any]] = []
        self.revalidate_calls: list[dict[str, Any]] = []
        self.window_calls: list[dict[str, Any]] = []
        self.memory_text_calls = 0
        self.missing_old_sources = False

    async def list_directional_models(self, *, bank_id: str) -> list[PeerModel]:
        assert bank_id == "bank"
        return list(self.models)

    async def get_peer(self, *, bank_id: str, peer_id: str) -> Peer | None:
        assert bank_id == "bank"
        return self.peers.get(peer_id)

    async def list_bootstrap_memory_window(self, **kwargs: Any) -> Any:
        self.window_calls.append(kwargs)
        pair = (kwargs["observer_peer_id"], kwargs["target_peer_id"])
        rows = self.windows.get(pair, [])
        selected = rows[: kwargs["limit"]]
        return SimpleNamespace(
            sources=selected,
            next_cursor=selected[-1].updated_at if selected else None,
            next_cursor_id=selected[-1].id if selected else None,
            has_more=len(rows) > len(selected),
        )

    async def get_directional_claims(
        self, *, bank_id: str, observer_peer_id: str, target_peer_id: str
    ) -> list[PeerClaim]:
        assert bank_id == "bank"
        return list(self.claims.get((observer_peer_id, target_peer_id), []))

    async def get_memory_texts(self, *, bank_id: str, source_ids: list[str]) -> dict[str, str]:
        self.memory_text_calls += 1
        raise AssertionError("mutable refetch")

    async def validate_model_memory_sources(
        self,
        *,
        bank_id: str,
        model_id: str,
        new_source_ids: list[str] | None = None,
        expected_source_versions: dict[str, datetime] | None = None,
    ) -> None:
        self.revalidate_calls.append(
            {
                "bank_id": bank_id,
                "model_id": model_id,
                "new_source_ids": new_source_ids,
                "expected_source_versions": expected_source_versions,
            }
        )
        if self.missing_old_sources:
            raise ValueError("A memory_unit source is missing from the model projection")

    async def validate_bank_memory_sources(self, **kwargs: Any) -> None:
        self.advance_calls.append(kwargs)


class _WindowService:
    def __init__(self, repository: _WindowRepository) -> None:
        self.repository = repository

    async def model(
        self, bank_id: str, observer_peer_id: str, target_peer_id: str, payload: Any, **kwargs: Any
    ) -> PeerModel:
        assert bank_id == "bank"
        self.repository.model_calls.append(
            {
                "observer_peer_id": observer_peer_id,
                "target_peer_id": target_peer_id,
                "claims": payload.claims,
                **kwargs,
            }
        )
        for index, model in enumerate(self.repository.models):
            if model.observer_peer_id == observer_peer_id and model.target_peer_id == target_peer_id:
                update: dict[str, Any] = {}
                if payload.claims or kwargs.get("supersede_claim_ids"):
                    update["version"] = model.version + 1
                if kwargs.get("source_cursor") is not None and kwargs.get("source_cursor_id") is not None:
                    update["source_cursor"] = kwargs["source_cursor"]
                    update["source_cursor_id"] = kwargs["source_cursor_id"]
                if update:
                    updated = model.model_copy(update=update)
                    self.repository.models[index] = updated
                    return updated
                return model
        raise AssertionError("unknown pair")


class _ReadbackFailureRepository(_WindowRepository):
    def __init__(self, models: list[PeerModel], windows: dict[tuple[str, str], list[Any]]) -> None:
        super().__init__(models, windows)
        self.plan: Any = None

    async def peer_pair_exists(self, **_kwargs: Any) -> bool:
        return True

    async def get_directional_model(self, **_kwargs: Any) -> PeerModel:
        if self.plan is not None:
            raise RuntimeError("post-commit readback unavailable")
        return self.models[0]

    async def memory_sources_exist(self, **_kwargs: Any) -> bool:
        return True

    async def apply_materialization(self, plan: Any, **_kwargs: Any) -> Any:
        self.plan = plan
        return SimpleNamespace(model_id=plan.model_id, version=plan.version)


class _WindowEngine:
    def __init__(self, service: _WindowService) -> None:
        self._peer_modeling_service = AsyncMock(return_value=service)
        self._write_operation_progress = AsyncMock()
        self._write_peer_refresh_metadata = AsyncMock()
        self._parse_peer_refresh_snapshot_at = MemoryEngine._parse_peer_refresh_snapshot_at


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return self.rows


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False


class _Backend:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_repository_selects_bootstrap_corpus_without_roles_and_with_composite_bounds() -> None:
    cursor = NOW - timedelta(hours=2)
    rows = [
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "text": "one",
            "context": "ctx",
            "fact_type": "observation",
            "updated_at": cursor + timedelta(minutes=1),
        },
    ]
    connection = _Connection(rows)
    repository = PeerRepository(cast(Any, _Backend(connection)))

    window = await repository.list_bootstrap_memory_window(
        bank_id="bank",
        observer_peer_id=OBSERVER_ID,
        target_peer_id=TARGET_A_ID,
        after_cursor=cursor,
        after_cursor_id="99999999-9999-9999-9999-999999999999",
        snapshot_at=NOW,
        limit=16,
    )

    assert [source.id for source in window.sources] == [rows[0]["id"]]
    assert window.next_cursor == rows[0]["updated_at"]
    assert window.next_cursor_id == rows[0]["id"]
    query, args = connection.calls[0]
    normalized = " ".join(query.split()).lower()
    assert "memory_peer_roles" not in normalized
    assert "updated_at <= $2" in normalized
    assert "memory.updated_at > $3" in normalized
    assert "memory.updated_at = $3 and memory.id > $4" in normalized
    assert "order by memory.updated_at asc, memory.id asc" in normalized
    assert "limit $5" in normalized
    assert "fact_type = 'observation'" in normalized
    assert "fact_type in ('world', 'experience')" in normalized
    assert args[1] == NOW
    assert args[2] == cursor
    assert args[4] == 17


@pytest.mark.asyncio
async def test_future_observation_does_not_suppress_pre_cutoff_fallback_row() -> None:
    cursor = NOW - timedelta(hours=2)
    fallback = {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "text": "pre-cutoff world fact",
        "context": "ctx",
        "fact_type": "world",
        "updated_at": cursor + timedelta(minutes=1),
    }
    future_observation = {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "text": "future observation",
        "context": "ctx",
        "fact_type": "observation",
        "updated_at": NOW + timedelta(minutes=1),
    }

    class _FallbackConnection(_Connection):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.calls.append((query, args))
            snapshot_at = args[1]
            cutoff_applied_to_observations = "observations.updated_at <= $2" in query
            has_observation = any(
                row["fact_type"] == "observation"
                and (not cutoff_applied_to_observations or row["updated_at"] <= snapshot_at)
                for row in (fallback, future_observation)
            )
            return [] if has_observation else [fallback]

    connection = _FallbackConnection([])
    repository = PeerRepository(cast(Any, _Backend(connection)))

    window = await repository.list_bootstrap_memory_window(
        bank_id="bank",
        observer_peer_id=OBSERVER_ID,
        target_peer_id=TARGET_A_ID,
        after_cursor=cursor,
        after_cursor_id="99999999-9999-9999-9999-999999999999",
        snapshot_at=NOW,
        limit=16,
    )

    assert [source.id for source in window.sources] == [fallback["id"]]
    query, _args = connection.calls[0]
    assert query.count("observations.updated_at <= $2") == 2


@pytest.mark.asyncio
async def test_legacy_null_cursor_starts_at_model_updated_at_without_full_scan() -> None:
    cursor = NOW - timedelta(days=1)
    connection = _Connection([])
    repository = PeerRepository(cast(Any, _Backend(connection)))

    await repository.list_bootstrap_memory_window(
        bank_id="bank",
        observer_peer_id=OBSERVER_ID,
        target_peer_id=TARGET_A_ID,
        after_cursor=cursor,
        after_cursor_id=None,
        snapshot_at=NOW,
        limit=16,
    )

    query, args = connection.calls[0]
    normalized = " ".join(query.split()).lower()
    assert "memory.updated_at >= $3" in normalized
    assert "memory.updated_at > $3" not in normalized
    assert args[2] == cursor
    assert "order by memory.updated_at asc, memory.id asc" in normalized


@pytest.mark.asyncio
async def test_zero_role_rows_and_new_observation_are_processed_as_a_pair() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("new-observation", NOW - timedelta(hours=1), "Target A prefers tea")
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})
    service = _WindowService(repository)
    distiller = AsyncMock(return_value=[_claim(source.id)])

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=distiller,
    )

    assert result.pairs[0].status == "refreshed"
    assert distiller.await_count == 1
    assert "validate_pair_sources" not in repository.model_calls[0]
    assert repository.model_calls[0]["validate_bank_sources"] == [source.id]
    assert repository.model_calls[0]["source_cursor"] == source.updated_at
    assert repository.model_calls[0]["source_cursor_id"] == source.id


@pytest.mark.asyncio
async def test_each_pair_gets_separate_bank_window_call_and_target_result() -> None:
    models = [_model(OBSERVER_ID, TARGET_A_ID), _model(OBSERVER_ID, TARGET_B_ID)]
    source_a = _memory_source("source-a", NOW - timedelta(hours=2), "Target A detail")
    source_b = _memory_source("source-b", NOW - timedelta(hours=1), "Target B detail")
    repository = _WindowRepository(
        models,
        {
            (OBSERVER_ID, TARGET_A_ID): [source_a],
            (OBSERVER_ID, TARGET_B_ID): [source_b],
        },
    )
    service = _WindowService(repository)

    async def distill(**kwargs: Any) -> list[PeerClaimDraft]:
        target_id = kwargs["target"].id
        source_id = kwargs["source_ids"][0]
        assert kwargs["service"].repository is not repository
        return [_claim(source_id, f"claim for {target_id}")]

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=distill,
    )

    assert [pair.status for pair in result.pairs] == ["refreshed", "refreshed"]
    assert [(call["target_peer_id"], call["claims"][0].source_ids) for call in repository.model_calls] == [
        (TARGET_A_ID, [source_a.id]),
        (TARGET_B_ID, [source_b.id]),
    ]


@pytest.mark.asyncio
async def test_irrelevant_zero_claim_window_advances_cursor_without_version_bump() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("irrelevant", NOW - timedelta(hours=1), "Unrelated text")
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})
    service = _WindowService(repository)

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=AsyncMock(return_value=[]),
    )

    assert result.pairs[0].status == "unchanged"
    assert result.pairs[0].version_after == model.version
    assert repository.model_calls[0]["claims"] == []
    assert repository.model_calls[0]["source_cursor_id"] == source.id


@pytest.mark.asyncio
async def test_no_pending_rows_is_unchanged_without_llm_or_persistence() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    repository = _WindowRepository([model], {})
    service = _WindowService(repository)
    distiller = AsyncMock()

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=distiller,
    )

    assert result.pairs[0].status == "unchanged"
    assert result.pairs[0].version_after == model.version
    assert distiller.await_count == 0
    assert repository.model_calls == []
    assert len(repository.revalidate_calls) == 1
    assert repository.revalidate_calls[0]["new_source_ids"] == []


@pytest.mark.asyncio
async def test_old_projection_sources_beyond_prompt_caps_are_revalidated() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    new_source = _memory_source("new-source", NOW - timedelta(hours=1), "Target A detail")
    old_claims: list[PeerClaim] = []
    for index in range(65):
        old_claims.append(
            PeerClaim(
                id=f"claim-{index}",
                bank_id="bank",
                model_id=model.id,
                observer_peer_id=OBSERVER_ID,
                target_peer_id=TARGET_A_ID,
                claim_type=PeerClaimType.ATTRIBUTE,
                text=f"old claim {index}",
                status=PeerClaimStatus.ACTIVE,
                origin=PeerClaimOrigin.DERIVED,
                confidence=0.8,
                locked=False,
                provenance="peer_modeling",
                valid_from=None,
                valid_until=None,
                created_at=NOW - timedelta(days=2),
                updated_at=NOW - timedelta(days=1),
                sources=[PeerSource(source_kind=PeerSourceKind.MEMORY_UNIT, source_id=f"old-{index}")],
            )
        )
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [new_source]})
    repository.claims[(OBSERVER_ID, TARGET_A_ID)] = old_claims
    service = _WindowService(repository)
    captured_current_claims: list[Any] = []

    async def distill(**kwargs: Any) -> list[PeerClaimDraft]:
        captured_current_claims.extend(kwargs["current_claims"])
        return [_claim(new_source.id)]

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=distill,
    )

    assert result.pairs[0].status == "refreshed"
    assert len(captured_current_claims) == 64
    assert all(len(claim.sources) <= 16 for claim in captured_current_claims)
    assert repository.model_calls[0]["validate_bank_sources"] == [new_source.id]
    assert repository.revalidate_calls == []
    assert repository.model_calls[0]["validate_existing_sources"] is True


@pytest.mark.asyncio
async def test_empty_window_missing_old_source_fails_without_writes() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    repository = _WindowRepository([model], {})
    repository.missing_old_sources = True
    service = _WindowService(repository)

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=AsyncMock(),
    )

    assert result.pairs[0].status == "failed"
    assert result.pairs[0].error == "ValueError"
    assert repository.model_calls == []
    assert len(repository.revalidate_calls) == 1
    assert repository.revalidate_calls[0]["new_source_ids"] == []


@pytest.mark.asyncio
async def test_real_seventeen_source_window_is_partial_and_defers_same_operation() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    sources = [
        _memory_source(
            f"source-{index}",
            NOW - timedelta(hours=17 - index),
            f"Target A detail {index}",
        )
        for index in range(17)
    ]
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): sources})
    engine = _WindowEngine(_WindowService(repository))
    operation_id = "operation-17-source"
    task = {
        "bank_id": "bank",
        "operation_id": operation_id,
        "_tenant_id": "tenant",
        "_api_key_id": "key",
        "snapshot_at": NOW.isoformat(),
    }
    distiller = AsyncMock(return_value=[_claim(sources[0].id)])

    with patch(
        "hindsight_api.engine.peer_modeling.refresh.distill_directional_claim_delta",
        new=distiller,
    ):
        with pytest.raises(DeferOperation):
            await MemoryEngine._handle_peer_model_refresh(cast(MemoryEngine, engine), task)

    assert len(repository.window_calls) == 1
    assert repository.window_calls[0]["limit"] == 16
    assert repository.window_calls[0]["snapshot_at"] == NOW
    assert distiller.await_args is not None
    assert len(distiller.await_args.kwargs["source_ids"]) == 16
    assert engine._write_peer_refresh_metadata.await_count == 1
    metadata_call = engine._write_peer_refresh_metadata.await_args
    assert metadata_call is not None
    assert metadata_call.args[0] == operation_id
    assert metadata_call.args[1]["status"] == "partial"
    assert metadata_call.args[1]["pairs"][0]["has_more"] is True
    assert metadata_call.args[1]["pairs"][0]["status"] == "refreshed"
    assert all(call.args[0] == operation_id for call in engine._write_operation_progress.await_args_list)
    assert engine._write_operation_progress.await_args_list[-1].kwargs["stage"] == "refreshing"
    assert repository.models[0].version == model.version + 1
    assert repository.models[0].source_cursor_id == sources[15].id


@pytest.mark.asyncio
async def test_real_seventeen_source_no_delta_window_is_partial_and_defers_same_operation() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    sources = [
        _memory_source(
            f"source-{index}",
            NOW - timedelta(hours=17 - index),
            f"Target A detail {index}",
        )
        for index in range(17)
    ]
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): sources})
    engine = _WindowEngine(_WindowService(repository))
    operation_id = "operation-17-source-no-delta"
    task = {
        "bank_id": "bank",
        "operation_id": operation_id,
        "_tenant_id": "tenant",
        "_api_key_id": "key",
        "snapshot_at": NOW.isoformat(),
    }
    distiller = AsyncMock(return_value=PeerClaimDelta())

    with patch(
        "hindsight_api.engine.peer_modeling.refresh.distill_directional_claim_delta",
        new=distiller,
    ):
        with pytest.raises(DeferOperation):
            await MemoryEngine._handle_peer_model_refresh(cast(MemoryEngine, engine), task)

    assert len(repository.window_calls) == 1
    assert repository.window_calls[0]["limit"] == 16
    assert repository.window_calls[0]["snapshot_at"] == NOW
    assert distiller.await_args is not None
    assert len(distiller.await_args.kwargs["source_ids"]) == 16
    assert distiller.await_args.kwargs["source_ids"] == [source.id for source in sources[:16]]
    assert engine._write_peer_refresh_metadata.await_count == 1
    metadata_call = engine._write_peer_refresh_metadata.await_args
    assert metadata_call is not None
    assert metadata_call.args[0] == operation_id
    assert metadata_call.args[1]["status"] == "partial"
    assert metadata_call.args[1]["pairs"][0]["status"] == "unchanged"
    assert metadata_call.args[1]["pairs"][0]["has_more"] is True
    assert metadata_call.args[1]["pairs"][0]["cursor_advanced"] is True
    assert all(call.args[0] == operation_id for call in engine._write_operation_progress.await_args_list)
    assert engine._write_operation_progress.await_args_list[-1].kwargs["stage"] == "refreshing"
    assert repository.models[0].version == model.version
    assert repository.models[0].source_cursor == sources[15].updated_at
    assert repository.models[0].source_cursor_id == sources[15].id


@pytest.mark.asyncio
async def test_incremental_oversized_current_claim_is_rejected_before_provider_call() -> None:
    llm = SimpleNamespace(call=AsyncMock())

    with pytest.raises(ValueError, match="current claim text"):
        await _synthesize_claims(
            llm=llm,
            peer=_peer(TARGET_A_ID, "target-a"),
            proposals=[
                _ExtractedClaim(
                    target_external_id="target-a",
                    claim_type=PeerClaimType.ATTRIBUTE,
                    text="new proposal",
                    source_ids=["new-source"],
                )
            ],
            current_claims=[_current_claim("x" * 4_001)],
            max_card_entries=12,
        )

    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_incremental_oversized_serialized_current_claims_are_rejected_before_provider_call() -> None:
    llm = SimpleNamespace(call=AsyncMock())

    with pytest.raises(ValueError, match="serialized user-content"):
        await _synthesize_claims(
            llm=llm,
            peer=_peer(TARGET_A_ID, "target-a"),
            proposals=[
                _ExtractedClaim(
                    target_external_id="target-a",
                    claim_type=PeerClaimType.ATTRIBUTE,
                    text="new proposal",
                    source_ids=["new-source"],
                )
            ],
            current_claims=[_current_claim("x" * 4_000, str(index)) for index in range(64)],
            max_card_entries=12,
        )

    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_rejects_complete_messages_when_user_content_is_under_bound() -> None:
    text = "x" * 127_500
    assert len(_serialized_extraction_user_content(text).encode("utf-8")) < 128_000
    llm = SimpleNamespace(call=AsyncMock())

    with pytest.raises(ValueError, match="serialized extraction messages payload"):
        await _extract_claim_batch(
            llm=llm,
            observer=_peer(OBSERVER_ID, "observer"),
            peers=[_peer(TARGET_A_ID, "target-a")],
            rows=[{"id": "source", "text": text}],
        )

    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_rejects_multibyte_complete_messages_overflow_before_provider_call() -> None:
    text = "😀" * 31_900
    assert len(_serialized_extraction_user_content(text).encode("utf-8")) < 128_000
    llm = SimpleNamespace(call=AsyncMock())

    with pytest.raises(ValueError, match="serialized extraction messages payload"):
        await _extract_claim_batch(
            llm=llm,
            observer=_peer(OBSERVER_ID, "observer"),
            peers=[_peer(TARGET_A_ID, "target-a")],
            rows=[{"id": "source", "text": text}],
        )

    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_calls_provider_when_complete_messages_are_under_bound() -> None:
    text = "😀" * 31_000
    assert len(_serialized_extraction_user_content(text).encode("utf-8")) < 128_000
    llm = SimpleNamespace(call=AsyncMock(return_value=_ClaimBatch()))

    result = await _extract_claim_batch(
        llm=llm,
        observer=_peer(OBSERVER_ID, "observer"),
        peers=[_peer(TARGET_A_ID, "target-a")],
        rows=[{"id": "source", "text": text}],
    )

    assert result == _ClaimBatch()
    llm.call.assert_awaited_once()
    messages = llm.call.await_args.args[0]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 128_000


@pytest.mark.asyncio
async def test_extraction_rejects_oversized_utf8_payload_before_provider_call() -> None:
    llm = SimpleNamespace(call=AsyncMock())
    rows = [{"id": f"source-{index}", "text": "😀" * 4_000} for index in range(16)]

    with pytest.raises(ValueError, match="serialized extraction messages payload"):
        await _extract_claim_batch(
            llm=llm,
            observer=_peer(OBSERVER_ID, "observer"),
            peers=[_peer(TARGET_A_ID, "target-a")],
            rows=rows,
        )

    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_synthesis_keeps_original_schema_prompt_without_incremental_contract() -> None:
    captured: dict[str, Any] = {}

    async def llm_call(messages: list[dict[str, str]], response_format: Any, **_kwargs: Any) -> _FinalClaims:
        captured["messages"] = messages
        captured["response_format"] = response_format
        return _FinalClaims(
            claims=[
                _FinalClaim(
                    claim_type=PeerClaimType.ATTRIBUTE,
                    text="authoritative claim",
                    source_ids=["source"],
                    card_eligible=True,
                )
            ]
        )

    result = await _synthesize_claims(
        llm=SimpleNamespace(call=llm_call),
        peer=_peer(TARGET_A_ID, "target-a"),
        proposals=[
            _ExtractedClaim(
                target_external_id="target-a",
                claim_type=PeerClaimType.ATTRIBUTE,
                text="proposal",
                source_ids=["source"],
            )
        ],
        max_card_entries=12,
    )

    assert result[0].text == "authoritative claim"
    assert captured["response_format"] is _FinalClaims
    assert "current_claims" not in captured["messages"][1]["content"]
    assert "supersede_claim_ids" not in _FinalClaim.model_fields


@pytest.mark.asyncio
async def test_legacy_distiller_processes_role_scoped_evidence_without_target_alias_prefilter() -> None:
    config = SimpleNamespace(peer_model_max_card_entries=12, peer_model_min_pattern_sources=2)
    llm_calls: list[Any] = []

    async def llm_call(messages: list[dict[str, str]], response_format: Any, **_kwargs: Any) -> Any:
        llm_calls.append((messages, response_format))
        if response_format is _ClaimBatch:
            return _ClaimBatch(
                claims=[
                    _ExtractedClaim(
                        target_external_id="target-a",
                        claim_type=PeerClaimType.ATTRIBUTE,
                        text="role-scoped evidence",
                        source_ids=["source"],
                    )
                ]
            )
        return _FinalClaims(
            claims=[
                _FinalClaim(
                    claim_type=PeerClaimType.ATTRIBUTE,
                    text="role-scoped evidence",
                    source_ids=["source"],
                    card_eligible=True,
                )
            ]
        )

    engine = SimpleNamespace(
        _config_resolver=SimpleNamespace(resolve_full_config=AsyncMock(return_value=config)),
        _consolidation_llm_config=SimpleNamespace(with_config=lambda *_args, **_kwargs: SimpleNamespace(call=llm_call)),
    )
    service = SimpleNamespace(
        repository=SimpleNamespace(get_memory_texts=AsyncMock(return_value={"source": "no alias here"}))
    )

    drafts = await distill_directional_claims(
        memory_engine=engine,
        service=service,
        bank_id="bank",
        observer=_peer(OBSERVER_ID, "observer"),
        target=_peer(TARGET_A_ID, "target-a"),
        source_ids=["source"],
        request_context=object(),
    )

    assert [draft.text for draft in drafts] == ["role-scoped evidence"]
    assert [response_format for _messages, response_format in llm_calls] == [_ClaimBatch, _FinalClaims]


@pytest.mark.asyncio
async def test_current_claims_are_sent_to_synthesis_and_semantic_replacement_names_only_eligible_claim() -> None:
    captured: list[tuple[str, Any]] = []

    async def llm_call(messages: list[dict[str, str]], response_format: Any, **_kwargs: Any) -> _IncrementalFinalClaims:
        captured.append((messages[1]["content"], response_format))
        return _IncrementalFinalClaims(
            claims=[
                _IncrementalFinalClaim(
                    claim_type=PeerClaimType.ATTRIBUTE,
                    text="Updated target preference",
                    confidence=0.95,
                    source_ids=["new-source"],
                    card_eligible=True,
                    supersede_claim_ids=["derived-old"],
                )
            ]
        )

    current_derived = PeerClaim(
        id="derived-old",
        bank_id="bank",
        model_id="model",
        observer_peer_id=OBSERVER_ID,
        target_peer_id=TARGET_A_ID,
        claim_type=PeerClaimType.ATTRIBUTE,
        text="Old target preference",
        status=PeerClaimStatus.ACTIVE,
        origin=PeerClaimOrigin.DERIVED,
        confidence=0.8,
        locked=False,
        provenance="peer_modeling",
        valid_from=None,
        valid_until=None,
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(days=2),
        sources=[PeerSource(source_kind=PeerSourceKind.MEMORY_UNIT, source_id="old-source")],
    )
    current_manual = current_derived.model_copy(
        update={
            "id": "manual-locked",
            "text": "Manual target preference",
            "origin": PeerClaimOrigin.MANUAL,
            "locked": True,
        }
    )

    result = await _synthesize_claims(
        llm=SimpleNamespace(call=llm_call),
        peer=_peer(TARGET_A_ID, "target-a"),
        proposals=[
            _ExtractedClaim(
                target_external_id="target-a",
                claim_type=PeerClaimType.ATTRIBUTE,
                text="Target preference",
                confidence=0.9,
                source_ids=["new-source"],
                card_eligible=True,
            )
        ],
        current_claims=[current_derived, current_manual],
        max_card_entries=12,
    )

    incremental_result = cast(list[_IncrementalFinalClaim], result)
    assert incremental_result[0].supersede_claim_ids == ["derived-old"]
    assert captured[0][1] is _IncrementalFinalClaims
    assert '"id": "derived-old"' in captured[0][0]
    assert '"id": "manual-locked"' in captured[0][0]


@pytest.mark.asyncio
async def test_returned_source_outside_new_and_current_allowlist_fails_before_apply() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("new-source", NOW - timedelta(hours=1), "Target A detail")
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})
    service = _WindowService(repository)

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=AsyncMock(return_value=[_claim(source.id), _claim("not-allowed")]),
    )

    assert result.pairs[0].status == "failed"
    assert result.pairs[0].error == "ValueError"
    assert repository.model_calls == []


@pytest.mark.asyncio
async def test_pair_failure_continues_and_all_failures_are_not_completed() -> None:
    models = [_model(OBSERVER_ID, TARGET_A_ID), _model(OBSERVER_ID, TARGET_B_ID)]
    source_a = _memory_source("source-a", NOW - timedelta(hours=2), "Target A detail")
    source_b = _memory_source("source-b", NOW - timedelta(hours=1), "Target B detail")
    repository = _WindowRepository(
        models,
        {(OBSERVER_ID, TARGET_A_ID): [source_a], (OBSERVER_ID, TARGET_B_ID): [source_b]},
    )
    service = _WindowService(repository)
    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=AsyncMock(side_effect=[RuntimeError("first"), RuntimeError("second")]),
    )

    assert [pair.status for pair in result.pairs] == ["failed", "failed"]
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_materialization_exception_does_not_advance_refresh_cursor() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("new-source", NOW - timedelta(hours=1), "Target A detail")
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})

    class FailingService(_WindowService):
        async def model(self, *args: Any, **kwargs: Any) -> PeerModel:
            raise RuntimeError("materialization")

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(FailingService(repository)),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=AsyncMock(return_value=[_claim(source.id)]),
    )

    assert result.pairs[0].status == "failed"
    assert repository.model_calls == []
    assert repository.models[0].version == model.version


@pytest.mark.asyncio
async def test_committed_refresh_uses_planned_existing_model_when_readback_fails() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("new-source", NOW - timedelta(hours=1), "Target A detail")
    repository = _ReadbackFailureRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})
    service = PeerModelingService(cast(PeerRepository, repository))

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(cast(Any, service)),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=AsyncMock(return_value=[_claim(source.id)]),
    )

    assert result.status == "completed"
    assert result.pairs[0].status == "refreshed"
    assert result.pairs[0].version_after == model.version + 1
    assert result.pairs[0].cursor_advanced is True
    assert repository.plan is not None
    assert repository.plan.version == model.version + 1
    assert repository.plan.source_cursor == source.updated_at


@pytest.mark.asyncio
async def test_synthesis_can_use_snapshot_rows_without_mutable_refetch() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("source", NOW - timedelta(hours=1), "Target A detail")
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})
    service = _WindowService(repository)

    async def distill(**kwargs: Any) -> list[PeerClaimDraft]:
        rows = kwargs["source_rows"]
        assert rows[0].text == "Target A detail"
        return [_claim(source.id)]

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=distill,
    )

    assert result.pairs[0].status == "refreshed"
    assert repository.memory_text_calls == 0


@pytest.mark.asyncio
async def test_legacy_distiller_receives_only_the_declared_old_keyword_shape() -> None:
    model = _model(OBSERVER_ID, TARGET_A_ID)
    source = _memory_source("source", NOW - timedelta(hours=1), "Target A detail")
    repository = _WindowRepository([model], {(OBSERVER_ID, TARGET_A_ID): [source]})
    service = _WindowService(repository)
    calls: list[dict[str, Any]] = []

    async def legacy_distiller(
        *,
        memory_engine: Any,
        service: Any,
        bank_id: str,
        observer: Peer,
        target: Peer,
        source_ids: list[str],
        request_context: Any,
    ) -> list[PeerClaimDraft]:
        calls.append(
            {
                "memory_engine": memory_engine,
                "service": service,
                "bank_id": bank_id,
                "observer": observer,
                "target": target,
                "source_ids": source_ids,
                "request_context": request_context,
            }
        )
        return [_claim(source.id)]

    result = await refresh_existing_peer_models(
        memory_engine=_WindowEngine(service),
        bank_id="bank",
        request_context=object(),
        snapshot_at=NOW,
        distill_async=legacy_distiller,
    )

    assert result.pairs[0].status == "refreshed"
    assert calls[0]["source_ids"] == [source.id]
