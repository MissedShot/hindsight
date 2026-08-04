from typing import Any

import pytest

from hindsight_client import Hindsight


@pytest.mark.asyncio
async def test_peer_client_builds_encoded_paths_and_payloads(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_request(
        self,
        method: str,
        bank_id: str,
        path: str = "",
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "bank_id": bank_id,
                "path": path,
                "body": body,
                "params": params,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(Hindsight, "_apeer_request", fake_request)
    client = Hindsight(base_url="http://example.invalid")

    await client.acreate_peer(
        "bank/id",
        "alice",
        display_name="Alice",
        metadata={"team": "infra"},
    )
    await client.amodel_peer("bank/id", "observer/id", "target id", claims=[{"text": "fact"}])
    await client.aplan_peer_correction("bank/id", "observer/id", "target id", "not anymore")
    await client.acorrect_peer_model(
        "bank/id",
        "observer/id",
        "target id",
        {"base_model_version": 2, "claims": []},
        note="operator-approved",
    )

    assert calls == [
        {
            "method": "POST",
            "bank_id": "bank/id",
            "path": "",
            "body": {
                "external_id": "alice",
                "display_name": "Alice",
                "kind": "person",
                "metadata": {"team": "infra"},
            },
            "params": None,
        },
        {
            "method": "POST",
            "bank_id": "bank/id",
            "path": "/observer%2Fid/model/target%20id",
            "body": {"claims": [{"text": "fact"}]},
            "params": None,
        },
        {
            "method": "POST",
            "bank_id": "bank/id",
            "path": "/observer%2Fid/corrections/target%20id/plan",
            "body": {"text": "not anymore"},
            "params": None,
        },
        {
            "method": "POST",
            "bank_id": "bank/id",
            "path": "/observer%2Fid/corrections/target%20id",
            "body": {
                "plan": {"base_model_version": 2, "claims": []},
                "note": "operator-approved",
            },
            "params": None,
        },
    ]
