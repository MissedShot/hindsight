"""Focused PostgreSQL/API vertical-slice coverage for native peer modeling."""

import json
import logging
import uuid

import pytest

from hindsight_api.api.http import _raise_peer_http_error
from hindsight_api.engine.peer_modeling.errors import PeerValidationError
from hindsight_api.engine.peer_modeling.repository import PeerRepository


@pytest.mark.asyncio
async def test_peer_context_is_rejected_when_peer_modeling_is_disabled(api_client):
    bank_id = f"peer-disabled-{uuid.uuid4()}"
    create_bank = await api_client.put(f"/v1/default/banks/{bank_id}", json={"name": "Peer disabled"})
    assert create_bank.status_code == 200, create_bank.text

    response = await api_client.post(
        f"/v1/default/banks/{bank_id}/memories",
        json={
            "items": [
                {
                    "content": "This attribution must not be silently discarded.",
                    "peer_context": {
                        "observer_peer_id": str(uuid.uuid4()),
                        "subject_peer_ids": [str(uuid.uuid4())],
                    },
                }
            ]
        },
    )
    assert response.status_code == 400, response.text
    assert "peer_context requires peer modeling" in response.text


@pytest.mark.asyncio
async def test_peer_modeling_crud_context_and_manual_correction(api_client, memory):
    test_bank_id = f"peer-test-{uuid.uuid4()}"
    bank_response = await api_client.put(
        f"/v1/default/banks/{test_bank_id}",
        json={"name": "Peer modeling test"},
    )
    assert bank_response.status_code == 200, bank_response.text
    config_response = await api_client.patch(
        f"/v1/default/banks/{test_bank_id}/config",
        json={
            "updates": {
                "enable_peer_modeling": True,
                "enable_auto_peer_modeling": True,
                "peer_model_min_new_facts": 2,
                "peer_model_min_pattern_sources": 2,
                "peer_model_cooldown_seconds": 0,
            }
        },
    )
    assert config_response.status_code == 200, config_response.text

    observer_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/peers",
        json={"external_id": "observer", "display_name": "Observer"},
    )
    target_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/peers",
        json={"external_id": "target", "display_name": "Target"},
    )
    assert observer_response.status_code == 201, observer_response.text
    assert target_response.status_code == 201, target_response.text
    observer_id = observer_response.json()["id"]
    target_id = target_response.json()["id"]

    peers_response = await api_client.get(f"/v1/default/banks/{test_bank_id}/peers")
    assert peers_response.status_code == 200, peers_response.text
    assert peers_response.json()["total"] == 2

    model_response = await api_client.post(f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/model/{target_id}")
    assert model_response.status_code == 202, model_response.text
    operation_id = model_response.json()["operation_id"]
    operation_response = await api_client.get(f"/v1/default/banks/{test_bank_id}/operations/{operation_id}")
    assert operation_response.status_code == 200, operation_response.text
    operation = operation_response.json()
    assert operation["status"] == "completed"
    assert operation["result_metadata"]["peer_model"]["version"] == 1

    correction_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/corrections/{target_id}",
        json={
            "plan": {
                "correction_text": "Target prefers illustrated gardening notes.",
                "base_model_version": 1,
                "claims": [
                    {
                        "claim_type": "IDENTITY",
                        "text": "Target prefers illustrated gardening notes.",
                        "confidence": 1.0,
                    }
                ],
                "supersede_claim_ids": [],
                "reason": "Explicit operator correction",
            },
            "note": "Explicit operator correction",
        },
    )
    assert correction_response.status_code == 200, correction_response.text
    correction = correction_response.json()
    assert correction["claims"][0]["origin"] == "manual"
    assert correction["claims"][0]["locked"] is True
    assert correction["model"]["card"]["entries"][0]["text"] == "Target prefers illustrated gardening notes."

    context_response = await api_client.get(f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/context/{target_id}")
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert context["observer_peer_id"] == observer_id
    assert context["target_peer_id"] == target_id
    assert context["card"]["entries"][0]["locked"] is True
    assert context["claims"][0]["sources"][0]["source_kind"] == "manual"

    retain_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/memories",
        json={
            "items": [
                {
                    "content": "Observer says Target collects antique postcards.",
                    "peer_context": {
                        "observer_peer_id": observer_id,
                        "speaker_peer_id": "observer",
                        "subject_peer_ids": ["target"],
                        "source_message_id": "message-1",
                        "session_id": "session-1",
                    },
                }
            ]
        },
    )
    assert retain_response.status_code == 200, retain_response.text

    backend = await memory._get_backend()
    async with backend.acquire() as conn:
        rows = await conn.fetch(
            "SELECT memory_unit_id, role, modality, source_message_id, session_id "
            "FROM memory_peer_roles WHERE bank_id = $1 ORDER BY role",
            test_bank_id,
        )
    assert {row["role"] for row in rows} == {"observer", "speaker", "subject"}
    assert {row["modality"] for row in rows} == {"actual"}
    assert {row["source_message_id"] for row in rows} == {"message-1"}
    assert {row["session_id"] for row in rows} == {"session-1"}

    auto_context_response = await api_client.get(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/context/{target_id}"
    )
    auto_context = auto_context_response.json()
    assert auto_context["representation"]
    assert [entry["text"] for entry in auto_context["card"]["entries"]] == [
        "Target prefers illustrated gardening notes."
    ]

    source_id = str(next(row["memory_unit_id"] for row in rows if row["role"] == "subject"))
    evidence_model_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/model/{target_id}",
        json={
            "claims": [
                {
                    "claim_type": "ATTRIBUTE",
                    "text": "Target collects antique postcards.",
                    "confidence": 0.7,
                    "source_ids": [source_id],
                }
            ]
        },
    )
    assert evidence_model_response.status_code == 202, evidence_model_response.text
    context_response = await api_client.get(f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/context/{target_id}")
    context = context_response.json()
    assert "Target collects antique postcards." in context["representation"]
    assert [entry["text"] for entry in context["card"]["entries"]] == ["Target prefers illustrated gardening notes."]

    manual_claim_id = correction["claims"][0]["id"]
    unlock_response = await api_client.patch(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/claims/{target_id}/{manual_claim_id}",
        json={"locked": False},
    )
    assert unlock_response.status_code == 200, unlock_response.text
    assert unlock_response.json()["claim"]["locked"] is False

    lock_response = await api_client.patch(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/claims/{target_id}/{manual_claim_id}",
        json={"locked": True},
    )
    assert lock_response.status_code == 200, lock_response.text
    assert lock_response.json()["claim"]["locked"] is True

    retract_response = await api_client.delete(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/claims/{target_id}/{manual_claim_id}"
    )
    assert retract_response.status_code == 200, retract_response.text
    assert retract_response.json()["claim"]["status"] == "retracted"
    assert retract_response.json()["model"]["card"]["entries"] == []

    wrong_bank_delete_response = await api_client.delete(f"/v1/default/banks/not-{test_bank_id}/memories/{source_id}")
    assert wrong_bank_delete_response.status_code == 404, wrong_bank_delete_response.text

    delete_memory_response = await api_client.delete(f"/v1/default/banks/{test_bank_id}/memories/{source_id}")
    assert delete_memory_response.status_code == 200, delete_memory_response.text
    claims_response = await api_client.get(f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/claims/{target_id}")
    derived_claim = next(
        claim for claim in claims_response.json()["items"] if claim["text"] == "Target collects antique postcards."
    )
    assert derived_claim["status"] == "superseded"


class _BootstrapLLM:
    no_incremental_claims = False

    def with_config(self, *_args, **_kwargs):
        return self

    async def call(self, messages, *, response_format, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        if response_format.__name__ == "_DiscoveryResult":
            return response_format.model_validate(
                {
                    "observer_external_id": "Avery",
                    "peers": [
                        {
                            "external_id": "Avery",
                            "display_name": "Avery",
                            "kind": "agent",
                            "aliases": ["Avery"],
                            "role": "observer",
                        },
                        {
                            "external_id": "morgan-fixture",
                            "display_name": "Morgan",
                            "kind": "person",
                            "aliases": ["morgan-fixture", "Morgan"],
                            "role": "participant",
                        },
                    ],
                }
            )
        if response_format.__name__ == "_ClaimBatch":
            if self.no_incremental_claims:
                return response_format.model_validate({"claims": [], "ambiguous_count": 0})
            target = payload["allowed_peers"][0]["external_id"]
            source_ids = [item["id"] for item in payload["evidence"]]
            if target != "morgan-fixture":
                return response_format.model_validate({"claims": [], "ambiguous_count": 0})
            return response_format.model_validate(
                {
                    "claims": [
                        {
                            "target_external_id": target,
                            "claim_type": "IDENTITY",
                            "text": "Name: Morgan",
                            "confidence": 0.95,
                            "source_ids": source_ids[:1],
                            "card_eligible": True,
                        },
                        {
                            "target_external_id": target,
                            "claim_type": "ATTRIBUTE",
                            "text": "Collects antique postcards",
                            "confidence": 0.92,
                            "source_ids": source_ids,
                            "card_eligible": True,
                        },
                    ],
                    "ambiguous_count": 0,
                }
            )
        if response_format.__name__ == "_FinalClaims":
            if self.no_incremental_claims:
                return response_format.model_validate({"claims": []})
            return response_format.model_validate(
                {
                    "claims": [
                        {
                            "claim_type": proposal["claim_type"],
                            "text": proposal["text"],
                            "confidence": proposal["confidence"],
                            "source_ids": proposal["source_ids"],
                            "card_eligible": proposal["card_eligible"],
                        }
                        for proposal in payload["proposals"]
                    ]
                }
            )
        raise AssertionError(f"Unexpected response model: {response_format}")

    async def cleanup(self):
        return None


@pytest.mark.asyncio
async def test_peer_bootstrap_discovers_peers_materializes_cards_and_reports_progress(api_client, memory):
    bank_id = f"peer-bootstrap-{uuid.uuid4()}"
    create_bank = await api_client.put(f"/v1/default/banks/{bank_id}", json={"name": "Bootstrap test"})
    assert create_bank.status_code == 200, create_bank.text

    enable = await api_client.patch(
        f"/v1/default/banks/{bank_id}/config",
        json={"updates": {"enable_peer_modeling": True}},
    )
    assert enable.status_code == 200, enable.text
    assert enable.json()["config"]["enable_auto_peer_modeling"] is True

    observer_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers",
        json={"external_id": "Avery", "display_name": "Avery", "kind": "agent"},
    )
    assert observer_response.status_code == 201, observer_response.text
    observer_id = observer_response.json()["id"]

    evidence_ids = [uuid.uuid4(), uuid.uuid4()]
    backend = await memory._get_backend()
    async with backend.acquire() as conn:
        for evidence_id, text in zip(
            evidence_ids,
            [
                "Morgan is the name of the synthetic participant.",
                "morgan-fixture collects antique postcards.",
            ],
            strict=True,
        ):
            await conn.execute(
                """
                INSERT INTO memory_units (id, bank_id, text, fact_type, context, metadata)
                VALUES ($1, $2, $3, 'observation', $4, $5::jsonb)
                """,
                evidence_id,
                bank_id,
                text,
                "This is a synthetic fixture conversation between Morgan and Avery.",
                json.dumps({"user_name": "morgan-fixture", "chat_name": "morgan-fixture"}),
            )

    memory._consolidation_llm_config = _BootstrapLLM()
    bootstrap = await api_client.post(f"/v1/default/banks/{bank_id}/peers/bootstrap")
    assert bootstrap.status_code == 202, bootstrap.text
    operation_id = bootstrap.json()["operation_id"]

    peers_response = await api_client.get(f"/v1/default/banks/{bank_id}/peers")
    assert peers_response.status_code == 200, peers_response.text
    peers = {peer["external_id"]: peer for peer in peers_response.json()["items"]}
    assert set(peers) == {"Avery", "morgan-fixture"}
    assert peers["morgan-fixture"]["display_name"] == "Morgan"
    assert {"Morgan", "morgan-fixture"} <= set(peers["morgan-fixture"]["metadata"]["aliases"])

    context_response = await api_client.get(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/context/{peers['morgan-fixture']['id']}"
    )
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert {entry["text"] for entry in context["card"]["entries"]} == {
        "Name: Morgan",
        "Collects antique postcards",
    }
    assert "Collects antique postcards" in context["representation"]

    status_response = await api_client.get(f"/v1/default/banks/{bank_id}/operations/{operation_id}")
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()
    assert status["status"] == "completed"
    assert status["progress"]["stage"] == "completed"
    assert status["progress"]["detail"]["evidence_processed"] == 2
    assert status["result_metadata"]["peer_bootstrap"]["peers_created"] == 1
    assert status["result_metadata"]["peer_bootstrap"]["card_entries"] == 2

    config_response = await api_client.patch(
        f"/v1/default/banks/{bank_id}/config",
        json={
            "updates": {
                "peer_model_cooldown_seconds": 0,
                "peer_model_min_new_facts": 8,
                "enable_auto_consolidation": False,
            }
        },
    )
    assert config_response.status_code == 200, config_response.text
    incremental_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/memories",
        json={
            "items": [
                {
                    "content": "Morgan collects antique postcards and likes bright weather.",
                    "peer_context": {
                        "observer_peer_id": observer_id,
                        "speaker_peer_id": peers["morgan-fixture"]["id"],
                        "subject_peer_ids": [peers["morgan-fixture"]["id"]],
                        "source_message_id": "incremental-message-1",
                        "session_id": "incremental-session-1",
                    },
                }
            ]
        },
    )
    assert incremental_response.status_code == 200, incremental_response.text
    first_context_response = await api_client.get(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/context/{peers['morgan-fixture']['id']}"
    )
    assert first_context_response.status_code == 200, first_context_response.text
    assert first_context_response.json()["version"] == context["version"]

    second_incremental_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/memories",
        json={
            "items": [
                {
                    "content": "Morgan also restores vintage radios and catalogs spare parts.",
                    "peer_context": {
                        "observer_peer_id": observer_id,
                        "speaker_peer_id": peers["morgan-fixture"]["id"],
                        "subject_peer_ids": [peers["morgan-fixture"]["id"]],
                        "source_message_id": "incremental-message-2",
                        "session_id": "incremental-session-2",
                    },
                }
            ]
        },
    )
    assert second_incremental_response.status_code == 200, second_incremental_response.text
    claims_response = await api_client.get(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/claims/{peers['morgan-fixture']['id']}"
    )
    assert claims_response.status_code == 200, claims_response.text
    historical_source_ids = {str(evidence_id) for evidence_id in evidence_ids}
    assert any(
        source["source_id"] not in historical_source_ids
        for claim in claims_response.json()["items"]
        for source in claim["sources"]
        if source["source_kind"] == "memory_unit"
    )

    modeled_context_response = await api_client.get(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/context/{peers['morgan-fixture']['id']}"
    )
    version_before_noop = modeled_context_response.json()["version"]
    memory._consolidation_llm_config.no_incremental_claims = True
    for index in range(3, 5):
        noop_response = await api_client.post(
            f"/v1/default/banks/{bank_id}/memories",
            json={
                "items": [
                    {
                        "content": f"Routine travel note {index} with no durable profile change.",
                        "peer_context": {
                            "observer_peer_id": observer_id,
                            "speaker_peer_id": peers["morgan-fixture"]["id"],
                            "subject_peer_ids": [peers["morgan-fixture"]["id"]],
                            "source_message_id": f"incremental-message-{index}",
                            "session_id": f"incremental-session-{index}",
                        },
                    }
                ]
            },
        )
        assert noop_response.status_code == 200, noop_response.text

    noop_context_response = await api_client.get(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/context/{peers['morgan-fixture']['id']}"
    )
    assert noop_context_response.json()["version"] == version_before_noop
    async with backend.acquire() as conn:
        cursor_row = await conn.fetchrow(
            "SELECT source_cursor, source_cursor_id FROM peer_models "
            "WHERE bank_id = $1 AND observer_peer_id = $2 AND target_peer_id = $3",
            bank_id,
            uuid.UUID(observer_id),
            uuid.UUID(peers["morgan-fixture"]["id"]),
        )
        assert cursor_row["source_cursor"] is not None
        assert cursor_row["source_cursor_id"] is not None

        tie_ids = [
            uuid.UUID("ffffffff-ffff-ffff-ffff-fffffffffff0"),
            uuid.UUID("ffffffff-ffff-ffff-ffff-fffffffffff1"),
        ]
        for tie_id in tie_ids:
            await conn.execute(
                """
                INSERT INTO memory_units (id, bank_id, text, fact_type, context, metadata, created_at)
                VALUES ($1, $2, $3, 'world', '', '{}'::jsonb, $4)
                """,
                tie_id,
                bank_id,
                f"same-timestamp evidence {tie_id}",
                cursor_row["source_cursor"],
            )
            for role, peer_id in (
                ("observer", observer_id),
                ("subject", peers["morgan-fixture"]["id"]),
            ):
                await conn.execute(
                    """
                    INSERT INTO memory_peer_roles
                        (id, bank_id, memory_unit_id, peer_id, role, modality)
                    VALUES ($1, $2, $3, $4, $5, 'actual')
                    """,
                    uuid.uuid4(),
                    bank_id,
                    tie_id,
                    uuid.UUID(peer_id),
                    role,
                )

    repository = PeerRepository(backend)
    pending = await repository.get_pending_memory_sources(
        bank_id=bank_id,
        observer_peer_id=observer_id,
        target_peer_id=peers["morgan-fixture"]["id"],
    )
    assert pending.source_ids[-2:] == [str(value) for value in tie_ids]
    assert pending.next_cursor_id == str(tie_ids[-1])
    next_cursor = pending.next_cursor
    next_cursor_id = pending.next_cursor_id
    assert next_cursor is not None
    assert next_cursor_id is not None
    await repository.advance_source_cursor(
        bank_id=bank_id,
        observer_peer_id=observer_id,
        target_peer_id=peers["morgan-fixture"]["id"],
        source_cursor=next_cursor,
        source_cursor_id=next_cursor_id,
    )
    assert not (
        await repository.get_pending_memory_sources(
            bank_id=bank_id,
            observer_peer_id=observer_id,
            target_peer_id=peers["morgan-fixture"]["id"],
        )
    ).source_ids


def test_raise_peer_http_error_maps_validation_to_400() -> None:
    """PeerValidationError is a domain error, not a ValueError: it must still be a 400."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        _raise_peer_http_error(PeerValidationError("stale pair source"))
    assert exc_info.value.status_code == 400
    assert "stale pair source" in exc_info.value.detail


@pytest.mark.asyncio
async def test_invalid_peer_model_claim_returns_400_not_500(api_client) -> None:
    """A model request whose claim has no memory sources must not become an unhandled 500."""
    bank_id = f"peer-invalid-{uuid.uuid4()}"
    create_bank = await api_client.put(f"/v1/default/banks/{bank_id}", json={"name": "Invalid peer"})
    assert create_bank.status_code == 200, create_bank.text
    config_response = await api_client.patch(
        f"/v1/default/banks/{bank_id}/config",
        json={
            "updates": {
                "enable_peer_modeling": True,
                "peer_model_min_pattern_sources": 1,
                "peer_model_cooldown_seconds": 0,
            }
        },
    )
    assert config_response.status_code == 200, config_response.text

    observer = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers", json={"external_id": "obs", "display_name": "Obs"}
    )
    target = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers", json={"external_id": "tgt", "display_name": "Tgt"}
    )
    assert observer.status_code == 201, observer.text
    assert target.status_code == 201, target.text

    response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers/{observer.json()['id']}/model/{target.json()['id']}",
        json={
            "claims": [
                {
                    "claim_type": "ATTRIBUTE",
                    "text": "Invalid claim without sources.",
                    "source_ids": [],
                }
            ]
        },
    )
    assert response.status_code == 400, response.text


def test_discovery_llm_failure_logs_error_type_not_raw_exception(caplog) -> None:
    """Bootstrap discovery fallback must not leak the provider's exception text into logs."""
    import asyncio

    from hindsight_api.engine.peer_modeling.bootstrap import _discover_peers, _fallback_discovery

    marker = "RAW_PROVIDER_SECRET_7f3a9c"
    metadata_marker = "METADATA_SECRET_1b2e4d"

    class _RaisingProvider:
        async def call(self, *args, **kwargs):
            raise RuntimeError(f"provider exploded: {marker}")

    caplog.set_level(logging.WARNING, logger="hindsight_api.engine.peer_modeling.bootstrap")

    result = asyncio.run(
        _discover_peers(
            llm=_RaisingProvider(),
            existing=[],
            metadata_values=[metadata_marker],
            contexts=[],
        )
    )

    # Fallback must still produce a deterministic metadata-based discovery.
    assert result == _fallback_discovery([], [metadata_marker], [])
    log_output = "\n".join(record.getMessage() for record in caplog.records)
    assert "error_type=RuntimeError" in log_output
    assert marker not in log_output
    assert metadata_marker not in log_output
