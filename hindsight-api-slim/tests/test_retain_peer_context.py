"""Contract tests for explicit peer attribution on retain inputs."""

from typing import cast

import pytest
from pydantic import ValidationError

from hindsight_api.api.http import MemoryItem, RetainPeerContext
from hindsight_api.engine.retain.orchestrator import _build_contents, _build_delta_contents
from hindsight_api.engine.retain.types import RetainContentDict


def test_retain_peer_context_requires_an_attributed_peer() -> None:
    with pytest.raises(ValidationError, match="at least one peer"):
        RetainPeerContext()


def test_retain_peer_context_normalizes_lists_and_rejects_unknown_fields() -> None:
    context = RetainPeerContext(
        observer_peer_id="avery",
        subject_peer_ids=["user", " user ", "agent"],
        modality="quoted",
    )

    assert context.subject_peer_ids == ["user", "agent"]
    assert context.modality == "quoted"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RetainPeerContext(observer_peer_id="avery", inferred_biography=True)  # type: ignore[call-arg]


def test_memory_item_accepts_peer_context_and_retain_preserves_it() -> None:
    peer_context = RetainPeerContext(
        observer_peer_id="avery",
        speaker_peer_id="user",
        subject_peer_ids=["user"],
        source_message_id="fixture:message-42",
        modality="actual",
    )
    item = MemoryItem(
        content="The user explicitly said they collect antique postcards.",
        peer_context=peer_context,
    )
    payload = item.peer_context.model_dump(exclude_none=True) if item.peer_context else None
    assert payload is not None

    content_input = cast(RetainContentDict, {"content": item.content, "peer_context": payload})
    contents = _build_contents([content_input], None)
    assert contents[0].peer_context == payload

    delta_contents, chunk_map = _build_delta_contents(contents, {0: item.content}, [0])
    assert chunk_map == {0: 0}
    assert delta_contents[0].peer_context == payload
