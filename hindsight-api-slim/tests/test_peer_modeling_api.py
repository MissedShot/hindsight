"""Focused PostgreSQL/API vertical-slice coverage for native peer modeling."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_peer_modeling_crud_context_and_manual_correction(api_client):
    test_bank_id = f"peer-test-{uuid.uuid4()}"
    bank_response = await api_client.put(
        f"/v1/default/banks/{test_bank_id}",
        json={"name": "Peer modeling test"},
    )
    assert bank_response.status_code == 200, bank_response.text
    config_response = await api_client.patch(
        f"/v1/default/banks/{test_bank_id}/config",
        json={"updates": {"enable_peer_modeling": True}},
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

    model_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/model/{target_id}"
    )
    assert model_response.status_code == 200, model_response.text
    assert model_response.json()["card"]["entries"] == []

    correction_response = await api_client.post(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/corrections/{target_id}",
        json={
            "claim": {
                "claim_type": "IDENTITY",
                "text": "Target prefers illustrated gardening notes.",
                "confidence": 1.0,
                "source_kind": "manual",
            },
            "note": "Explicit operator correction",
        },
    )
    assert correction_response.status_code == 200, correction_response.text
    correction = correction_response.json()
    assert correction["claim"]["origin"] == "manual"
    assert correction["claim"]["locked"] is True
    assert correction["model"]["card"]["entries"][0]["text"] == "Target prefers illustrated gardening notes."

    context_response = await api_client.get(
        f"/v1/default/banks/{test_bank_id}/peers/{observer_id}/context/{target_id}"
    )
    assert context_response.status_code == 200, context_response.text
    context = context_response.json()
    assert context["observer_peer_id"] == observer_id
    assert context["target_peer_id"] == target_id
    assert context["card"]["entries"][0]["locked"] is True
    assert context["claims"][0]["sources"][0]["source_kind"] == "manual"
