"""Focused scheduler/worker wiring tests for automatic peer refresh."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api.api.http import OperationProgress, OperationResponse, OperationStatusResponse
from hindsight_api.engine.consolidation.consolidator import _trigger_peer_model_refreshes
from hindsight_api.engine.memory_engine import MemoryEngine
from hindsight_api.engine.peer_modeling.refresh import PeerRefreshPairOutcome, PeerRefreshResult

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class _Config:
    enable_peer_modeling = True
    enable_auto_peer_modeling = True
    peer_model_min_new_facts = 2
    peer_model_cooldown_seconds = 300


class _Repository:
    def __init__(self, models: list[object]) -> None:
        self.models = models
        self.list_directional_models = AsyncMock(return_value=models)


class _SchedulerEngine:
    def __init__(self, models: list[object], config: object = _Config()) -> None:
        repository = _Repository(models)
        self.repository = repository
        self._config_resolver = SimpleNamespace(resolve_full_config=AsyncMock(return_value=config))
        self._peer_modeling_service = AsyncMock(return_value=SimpleNamespace(repository=repository))
        self._latest_operation_attempt = AsyncMock(return_value=None)
        self.submit_async_peer_model_refresh = AsyncMock(return_value={"operation_id": "refresh-op"})


def _model(*, age_seconds: int) -> SimpleNamespace:
    return SimpleNamespace(updated_at=NOW - timedelta(seconds=age_seconds))


class _LookupConnection:
    def __init__(self, rows: list[tuple[str, str, str, datetime, datetime | None]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, query: str, *args: object) -> datetime | None:
        self.calls.append((query, args))
        bank_id, task_type = args
        timestamps = [
            updated_at or created_at
            for row_bank_id, row_task_type, _status, created_at, updated_at in self.rows
            if row_bank_id == bank_id and row_task_type == task_type
        ]
        return max(timestamps, default=None)


class _LookupAcquire:
    def __init__(self, connection: _LookupConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _LookupConnection:
        return self.connection

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False


class _LookupBackend:
    _wraps_backend = True

    def __init__(self, connection: _LookupConnection) -> None:
        self.connection = connection

    def acquire(self) -> _LookupAcquire:
        return _LookupAcquire(self.connection)


@pytest.mark.asyncio
async def test_latest_operation_attempt_uses_one_scoped_aggregate_query() -> None:
    retry_created_at = NOW - timedelta(seconds=600)
    connection = _LookupConnection(
        [
            ("bank-a", "peer_model_refresh", "completed", retry_created_at, None),
            ("bank-a", "peer_model_refresh", "failed", retry_created_at, NOW - timedelta(seconds=30)),
            ("bank-a", "peer_model_refresh", "cancelled", NOW - timedelta(seconds=60), None),
            ("bank-a", "other-task", "processing", NOW, None),
            ("bank-b", "peer_model_refresh", "pending", NOW, None),
        ]
    )
    engine = MemoryEngine.__new__(MemoryEngine)
    request_context = object()
    engine._authenticate_tenant = AsyncMock()
    engine._get_backend = AsyncMock(return_value=_LookupBackend(connection))

    latest = await MemoryEngine._latest_operation_attempt(
        engine,
        "bank-a",
        task_type="peer_model_refresh",
        request_context=request_context,
    )

    assert latest == NOW - timedelta(seconds=30)
    engine._authenticate_tenant.assert_awaited_once_with(request_context)
    assert len(connection.calls) == 1
    query, args = connection.calls[0]
    normalized_query = " ".join(query.split()).lower()
    assert "select max(coalesce(updated_at, created_at))" in normalized_query
    assert "async_operations" in normalized_query
    assert "where bank_id = $1 and operation_type = $2" in normalized_query
    assert "status" not in normalized_query
    assert "count(" not in normalized_query
    assert "limit" not in normalized_query
    assert args == ("bank-a", "peer_model_refresh")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enable_peer_modeling", "enable_auto_peer_modeling"),
    [(False, True), (True, False)],
)
async def test_scheduler_requires_both_peer_flags(enable_peer_modeling: bool, enable_auto_peer_modeling: bool) -> None:
    config = SimpleNamespace(
        enable_peer_modeling=enable_peer_modeling,
        enable_auto_peer_modeling=enable_auto_peer_modeling,
        peer_model_min_new_facts=2,
        peer_model_cooldown_seconds=0,
    )
    engine = _SchedulerEngine([_model(age_seconds=3600)], config)

    scheduled = await _trigger_peer_model_refreshes(
        engine,
        "bank",
        object(),
        new_facts=2,
        final_round=True,
        now=NOW,
    )

    assert scheduled is False
    engine._peer_modeling_service.assert_not_awaited()
    engine.submit_async_peer_model_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_skips_no_models_and_below_threshold() -> None:
    no_models = _SchedulerEngine([])
    assert (
        await _trigger_peer_model_refreshes(
            no_models,
            "bank",
            object(),
            new_facts=2,
            final_round=True,
            now=NOW,
        )
        is False
    )
    no_models._peer_modeling_service.assert_awaited_once()
    no_models.submit_async_peer_model_refresh.assert_not_awaited()

    below_threshold = _SchedulerEngine([_model(age_seconds=3600)])
    assert (
        await _trigger_peer_model_refreshes(
            below_threshold,
            "bank",
            object(),
            new_facts=1,
            final_round=True,
            now=NOW,
        )
        is False
    )
    below_threshold._peer_modeling_service.assert_not_awaited()
    below_threshold.submit_async_peer_model_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_skips_cards_inside_cooldown_and_refreshes_stale_bank_once() -> None:
    fresh = _SchedulerEngine([_model(age_seconds=299)])
    assert (
        await _trigger_peer_model_refreshes(
            fresh,
            "bank",
            object(),
            new_facts=2,
            final_round=True,
            now=NOW,
        )
        is False
    )
    fresh.submit_async_peer_model_refresh.assert_not_awaited()

    stale = _SchedulerEngine([_model(age_seconds=301), _model(age_seconds=60)])
    context = object()
    scheduled = await _trigger_peer_model_refreshes(
        stale,
        "bank",
        context,
        new_facts=2,
        final_round=True,
        now=NOW,
    )

    assert scheduled is True
    stale.submit_async_peer_model_refresh.assert_awaited_once_with(
        bank_id="bank",
        request_context=context,
    )


@pytest.mark.asyncio
async def test_scheduler_failed_refresh_attempt_is_cooled_down_then_expires() -> None:
    engine = _SchedulerEngine([_model(age_seconds=3600)])
    failed_at = NOW - timedelta(seconds=301)
    engine._latest_operation_attempt.side_effect = [failed_at, NOW, NOW]
    context = object()

    await _trigger_peer_model_refreshes(
        engine,
        "bank",
        context,
        new_facts=2,
        final_round=True,
        now=NOW,
    )
    assert engine.submit_async_peer_model_refresh.await_count == 1

    await _trigger_peer_model_refreshes(
        engine,
        "bank",
        context,
        new_facts=2,
        final_round=True,
        now=NOW + timedelta(seconds=100),
    )
    assert engine.submit_async_peer_model_refresh.await_count == 1

    await _trigger_peer_model_refreshes(
        engine,
        "bank",
        context,
        new_facts=2,
        final_round=True,
        now=NOW + timedelta(seconds=301),
    )

    assert engine.submit_async_peer_model_refresh.await_count == 2
    assert engine._latest_operation_attempt.await_count == 3
    engine._latest_operation_attempt.assert_awaited_with(
        "bank",
        task_type="peer_model_refresh",
        request_context=context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "pending", "processing"])
async def test_scheduler_cools_down_latest_refresh_attempt_across_statuses(status: str) -> None:
    engine = _SchedulerEngine([_model(age_seconds=3600)])
    latest_submission = NOW - timedelta(seconds=30)
    engine._latest_operation_attempt.return_value = latest_submission
    context = object()

    scheduled = await _trigger_peer_model_refreshes(
        engine,
        "bank",
        context,
        new_facts=2,
        final_round=True,
        now=NOW,
    )

    assert scheduled is False
    engine.submit_async_peer_model_refresh.assert_not_awaited()
    engine._latest_operation_attempt.assert_awaited_once_with(
        "bank",
        task_type="peer_model_refresh",
        request_context=context,
    )


@pytest.mark.asyncio
async def test_scheduler_uses_latest_attempt_timestamp_and_bank_tenant_scope() -> None:
    engine = _SchedulerEngine([_model(age_seconds=3600)])
    context = SimpleNamespace(tenant_id="tenant-a", api_key_id="key-a")
    engine._latest_operation_attempt.return_value = NOW - timedelta(seconds=30)

    scheduled = await _trigger_peer_model_refreshes(
        engine,
        "bank-a",
        context,
        new_facts=2,
        final_round=True,
        now=NOW,
    )

    assert scheduled is False
    engine._latest_operation_attempt.assert_awaited_once_with(
        "bank-a",
        task_type="peer_model_refresh",
        request_context=context,
    )
    engine.submit_async_peer_model_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_is_final_round_only() -> None:
    engine = _SchedulerEngine([_model(age_seconds=3600)])

    scheduled = await _trigger_peer_model_refreshes(
        engine,
        "bank",
        object(),
        new_facts=2,
        final_round=False,
        now=NOW,
    )

    assert scheduled is False
    engine._peer_modeling_service.assert_not_awaited()
    engine.submit_async_peer_model_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_treats_active_dedupe_as_no_new_submission() -> None:
    engine = _SchedulerEngine([_model(age_seconds=3600)])
    engine.submit_async_peer_model_refresh = AsyncMock(
        return_value={"operation_id": "existing-refresh", "deduplicated": True}
    )

    scheduled = await _trigger_peer_model_refreshes(
        engine,
        "bank",
        object(),
        new_facts=2,
        final_round=True,
        now=NOW,
    )

    assert scheduled is False
    engine.submit_async_peer_model_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_refresh_uses_active_bank_dedupe() -> None:
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._authenticate_tenant = AsyncMock()
    engine._submit_async_operation = AsyncMock(return_value={"operation_id": "existing-refresh", "deduplicated": True})

    result = await MemoryEngine.submit_async_peer_model_refresh(engine, "bank", request_context=object())

    assert result == {"operation_id": "existing-refresh", "deduplicated": True}
    engine._submit_async_operation.assert_awaited_once()
    assert engine._submit_async_operation.await_args is not None
    submit_kwargs = engine._submit_async_operation.await_args.kwargs
    assert submit_kwargs["bank_id"] == "bank"
    assert submit_kwargs["operation_type"] == "peer_model_refresh"
    assert submit_kwargs["task_type"] == "peer_model_refresh"
    assert submit_kwargs["task_payload"] == {}
    assert submit_kwargs["dedupe_by_bank"] is True
    assert submit_kwargs["dedupe_processing"] is True

    queued_progress = submit_kwargs["result_metadata"]["progress"]
    progress = OperationProgress.model_validate(queued_progress)
    assert progress.stage == "queued"
    assert progress.processed == 0
    assert progress.total is None
    assert datetime.fromisoformat(progress.at).tzinfo == UTC

    operation = OperationResponse.model_validate(
        {
            "id": "existing-refresh",
            "task_type": "peer_model_refresh",
            "items_count": 0,
            "created_at": progress.at,
            "status": "pending",
            "error_message": None,
            "progress": queued_progress,
        }
    )
    status = OperationStatusResponse.model_validate(
        {
            "operation_id": "existing-refresh",
            "status": "pending",
            "operation_type": "peer_model_refresh",
            "created_at": progress.at,
            "updated_at": progress.at,
            "progress": queued_progress,
        }
    )
    assert operation.progress == progress
    assert status.progress == progress


@pytest.mark.asyncio
async def test_worker_writes_result_and_pair_failure_metadata() -> None:
    result = PeerRefreshResult(
        pairs=[
            PeerRefreshPairOutcome(
                observer_peer_id="observer",
                target_peer_id="target",
                status="failed",
                version_before=3,
                error="RuntimeError",
            )
        ]
    )
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._write_peer_refresh_metadata = AsyncMock()
    refresh = AsyncMock(return_value=result)

    with patch(
        "hindsight_api.engine.peer_modeling.refresh.refresh_existing_peer_models",
        new=refresh,
    ):
        returned = await MemoryEngine._handle_peer_model_refresh(
            engine,
            {"bank_id": "bank", "operation_id": "op", "_tenant_id": "tenant", "_api_key_id": "key"},
        )

    assert returned == result.model_dump(mode="json")
    assert refresh.await_args is not None
    assert refresh.await_args.kwargs["operation_id"] == "op"
    engine._write_peer_refresh_metadata.assert_awaited_once_with(
        "op", {"status": "completed", **result.model_dump(mode="json")}
    )


@pytest.mark.asyncio
async def test_worker_writes_failure_metadata_before_propagating() -> None:
    engine = MemoryEngine.__new__(MemoryEngine)
    engine._write_peer_refresh_metadata = AsyncMock()

    with patch(
        "hindsight_api.engine.peer_modeling.refresh.refresh_existing_peer_models",
        new=AsyncMock(side_effect=RuntimeError("refresh failed")),
    ):
        with pytest.raises(RuntimeError, match="refresh failed"):
            await MemoryEngine._handle_peer_model_refresh(
                engine,
                {"bank_id": "bank", "operation_id": "op"},
            )

    engine._write_peer_refresh_metadata.assert_awaited_once_with("op", {"status": "failed", "error": "RuntimeError"})
