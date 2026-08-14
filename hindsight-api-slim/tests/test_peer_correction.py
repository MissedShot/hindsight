"""Regression coverage for semantic, claim-ID-scoped peer corrections."""

import json
import uuid

import pytest


class _CorrectionPlannerLLM:
    def with_config(self, *_args, **_kwargs):
        return self

    async def call(self, messages, *, response_format, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        conflicting = [
            claim
            for claim in payload["active_claims"]
            if "birthday" in claim["text"].lower() or "age is 35" in claim["text"].lower()
        ]
        return response_format.model_validate(
            {
                "claims": [
                    {
                        "claim_type": "IDENTITY",
                        "text": "Target was born on March 14, 1991.",
                        "confidence": 1.0,
                    }
                ],
                "supersede_claim_ids": [claim["id"] for claim in conflicting],
                "reason": "The explicit birth date replaces only the incomplete birthday and stored-age claims.",
            }
        )

    async def cleanup(self):
        return None


@pytest.mark.asyncio
async def test_semantic_correction_supersedes_only_conflicting_claims(api_client, memory):
    bank_id = f"peer-correction-{uuid.uuid4()}"
    create_bank = await api_client.put(f"/v1/default/banks/{bank_id}", json={"name": "Correction test"})
    assert create_bank.status_code == 200, create_bank.text
    enable = await api_client.patch(
        f"/v1/default/banks/{bank_id}/config",
        json={"updates": {"enable_peer_modeling": True}},
    )
    assert enable.status_code == 200, enable.text

    observer_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers",
        json={"external_id": "observer", "display_name": "Observer", "kind": "agent"},
    )
    target_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers",
        json={"external_id": "target", "display_name": "Target", "kind": "person"},
    )
    assert observer_response.status_code == 201, observer_response.text
    assert target_response.status_code == 201, target_response.text
    observer_id = observer_response.json()["id"]
    target_id = target_response.json()["id"]

    empty_model = await api_client.post(f"/v1/default/banks/{bank_id}/peers/{observer_id}/model/{target_id}")
    assert empty_model.status_code == 202, empty_model.text

    evidence_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    initial_claims = [
        ("IDENTITY", "Target's birthday is March 14; the birth year is unknown."),
        ("ATTRIBUTE", "Target collects antique postcards."),
        ("ATTRIBUTE", "Target likes bright dry weather."),
    ]
    backend = await memory._get_backend()
    async with backend.acquire() as conn:
        for evidence_id, (_, text) in zip(evidence_ids, initial_claims, strict=True):
            await conn.execute(
                """
                INSERT INTO memory_units (id, bank_id, text, fact_type, context, metadata)
                VALUES ($1, $2, $3, 'observation', $4, $5::jsonb)
                """,
                evidence_id,
                bank_id,
                text,
                "Private conversation between Observer and Target.",
                json.dumps({"speaker": "target"}),
            )

    model_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/model/{target_id}",
        json={
            "claims": [
                {
                    "claim_type": claim_type,
                    "text": text,
                    "confidence": 0.99,
                    "source_ids": [str(evidence_id)],
                }
                for evidence_id, (claim_type, text) in zip(evidence_ids, initial_claims, strict=True)
            ]
        },
    )
    assert model_response.status_code == 202, model_response.text

    manual_age_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/corrections/{target_id}",
        json={
            "plan": {
                "claims": [
                    {
                        "claim_type": "ATTRIBUTE",
                        "text": "Target birth year is 1991, and age is 35.",
                        "confidence": 1.0,
                    }
                ],
                "supersede_claim_ids": [],
                "reason": "Manual correction that stored a dynamic age.",
                "correction_text": "I was born in 1991 and I am 35.",
                "base_model_version": 2,
            }
        },
    )
    assert manual_age_response.status_code == 200, manual_age_response.text
    assert manual_age_response.json()["claims"][0]["locked"] is True

    memory._consolidation_llm_config = _CorrectionPlannerLLM()
    plan_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/corrections/{target_id}/plan",
        json={"text": "I was born on March 14, 1991, and I am 35 years old."},
    )
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    assert plan["base_model_version"] == 3
    assert plan["claims"] == [
        {
            "claim_type": "IDENTITY",
            "text": "Target was born on March 14, 1991.",
            "confidence": 1.0,
        }
    ]
    assert len(plan["supersede_claim_ids"]) == 2

    apply_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/corrections/{target_id}",
        json={"plan": plan, "note": "Explicit user correction"},
    )
    assert apply_response.status_code == 200, apply_response.text
    result = apply_response.json()
    assert result["superseded_claim_ids"] == plan["supersede_claim_ids"]
    assert result["claims"][0]["origin"] == "manual"
    assert result["claims"][0]["locked"] is True

    claims_response = await api_client.get(f"/v1/default/banks/{bank_id}/peers/{observer_id}/claims/{target_id}")
    assert claims_response.status_code == 200, claims_response.text
    claims = claims_response.json()["items"]
    active_texts = {claim["text"] for claim in claims if claim["status"] == "active"}
    assert "Target was born on March 14, 1991." in active_texts
    assert "Target collects antique postcards." in active_texts
    assert "Target likes bright dry weather." in active_texts
    assert all("age is 35" not in text.lower() for text in active_texts)
    superseded_ids = {claim["id"] for claim in claims if claim["status"] == "superseded"}
    assert superseded_ids == set(plan["supersede_claim_ids"])

    replay_response = await api_client.post(
        f"/v1/default/banks/{bank_id}/peers/{observer_id}/corrections/{target_id}",
        json={"plan": plan, "note": "Stale replay must fail"},
    )
    assert replay_response.status_code == 409, replay_response.text
