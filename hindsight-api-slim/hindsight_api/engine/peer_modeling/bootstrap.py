"""Historical peer-model bootstrap for an existing memory bank.

The bootstrap is deliberately asynchronous.  It discovers conversation participants,
attributes existing evidence to directional observer -> target pairs, distils durable
claims, and materializes cards while reporting coarse operation progress.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, Field

from hindsight_api.engine.db_utils import acquire_with_retry
from hindsight_api.engine.schema import fq_table

from .models import (
    PeerClaim,
    PeerClaimDelta,
    PeerClaimDraft,
    PeerClaimType,
    PeerCreate,
    PeerModelRequest,
    PeerUpdate,
)

if TYPE_CHECKING:
    from hindsight_api.engine.memory_engine import MemoryEngine
    from hindsight_api.models import RequestContext

logger = logging.getLogger(__name__)

_BATCH_SIZE = 80
_MAX_CONTEXT_SAMPLES = 30
_MAX_METADATA_VALUES = 40
_MAX_SOURCE_IDS_PER_CLAIM = 16
_MAX_CURRENT_CLAIMS = 64
_MAX_CURRENT_SOURCE_IDS_PER_CLAIM = 16
_MAX_INCREMENTAL_CURRENT_CLAIM_TEXT = 4_000
_MAX_INCREMENTAL_SYNTHESIS_USER_BYTES = 128_000
_MAX_EXTRACTION_MESSAGES_BYTES = 128_000


class _DiscoveredPeer(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    kind: str = Field(default="person", min_length=1, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    role: Literal["observer", "participant"] = "participant"


class _DiscoveryResult(BaseModel):
    observer_external_id: str = Field(min_length=1, max_length=255)
    peers: list[_DiscoveredPeer] = Field(default_factory=list, max_length=20)


class _ExtractedClaim(BaseModel):
    target_external_id: str = Field(min_length=1, max_length=255)
    claim_type: PeerClaimType
    text: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    source_ids: list[str] = Field(default_factory=list, min_length=1, max_length=_MAX_SOURCE_IDS_PER_CLAIM)
    card_eligible: bool = False


class _ClaimBatch(BaseModel):
    claims: list[_ExtractedClaim] = Field(default_factory=list, max_length=80)
    ambiguous_count: int = Field(default=0, ge=0)


class _FinalClaim(BaseModel):
    claim_type: PeerClaimType
    text: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    source_ids: list[str] = Field(default_factory=list, min_length=1, max_length=_MAX_SOURCE_IDS_PER_CLAIM)
    card_eligible: bool = False


class _FinalClaims(BaseModel):
    claims: list[_FinalClaim] = Field(default_factory=list, max_length=80)


class _IncrementalFinalClaim(_FinalClaim):
    """Incremental synthesis output, including its typed replacement contract."""

    supersede_claim_ids: list[str] = Field(default_factory=list, max_length=32)


class _IncrementalFinalClaims(BaseModel):
    """Incremental-only synthesis schema; full bootstrap keeps _FinalClaims."""

    claims: list[_IncrementalFinalClaim] = Field(default_factory=list, max_length=80)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean_identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 255:
        return None
    return value


def _normalize_claim(text: str) -> str:
    return " ".join(text.casefold().split())


def _validated_final_evidence(
    claim: _FinalClaim,
    *,
    source_pool: set[str],
    min_pattern_sources: int,
    fail_closed: bool = False,
) -> list[str]:
    """Enforce source-count policy after the LLM response."""
    evidence = list(dict.fromkeys(claim.source_ids))
    if fail_closed and any(source_id not in source_pool for source_id in evidence):
        raise ValueError("distiller returned a source outside the server-derived allowlist")
    if not fail_closed:
        evidence = [source_id for source_id in evidence if source_id in source_pool]
    minimum = 1 if claim.card_eligible else max(1, min_pattern_sources)
    return evidence[:_MAX_SOURCE_IDS_PER_CLAIM] if len(evidence) >= minimum else []


def _candidate_metadata(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    values: Counter[str] = Counter()
    contexts: list[str] = []
    seen_contexts: set[str] = set()
    for row in rows:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        if isinstance(metadata, dict):
            for key in ("user_name", "chat_name", "speaker", "author", "sender", "agent_identity"):
                cleaned = _clean_identity(metadata.get(key))
                if cleaned:
                    values[cleaned] += 1
        context = _clean_identity(row.get("context"))
        if context and context not in seen_contexts and len(contexts) < _MAX_CONTEXT_SAMPLES:
            contexts.append(context)
            seen_contexts.add(context)
    return [value for value, _ in values.most_common(_MAX_METADATA_VALUES)], contexts


def _fallback_discovery(existing: list[Any], metadata_values: list[str], contexts: list[str]) -> _DiscoveryResult:
    observer = next((peer for peer in existing if str(peer.kind) == "agent"), None)
    if observer is None and existing:
        observer = existing[0]

    observer_external_id = observer.external_id if observer is not None else "assistant"
    observer_aliases = [observer_external_id]
    if observer is not None and observer.display_name:
        observer_aliases.append(observer.display_name)

    context_names: list[str] = []
    for context in contexts:
        match = re.search(r"conversation\s+between\s+([^;,\n]+?)\s+and\s+([^;,\n.]+)", context, re.IGNORECASE)
        if match:
            context_names.extend(part.strip() for part in match.groups())

    human_external_id = next(
        (
            value
            for value in metadata_values
            if value.casefold() not in {alias.casefold() for alias in observer_aliases}
        ),
        None,
    )
    human_aliases = [human_external_id] if human_external_id else []
    for name in context_names:
        if name.casefold() not in {alias.casefold() for alias in observer_aliases}:
            human_aliases.append(name)
    human_aliases = list(dict.fromkeys(alias for alias in human_aliases if alias))

    peers = [
        _DiscoveredPeer(
            external_id=observer_external_id,
            display_name=observer.display_name if observer is not None else observer_external_id,
            kind="agent",
            aliases=observer_aliases,
            role="observer",
        )
    ]
    if human_external_id:
        display_name = next(
            (alias for alias in human_aliases if alias.casefold() != human_external_id.casefold()), None
        )
        peers.append(
            _DiscoveredPeer(
                external_id=human_external_id,
                display_name=display_name or human_external_id,
                kind="person",
                aliases=human_aliases,
                role="participant",
            )
        )
    return _DiscoveryResult(observer_external_id=observer_external_id, peers=peers)


async def _discover_peers(
    *,
    llm: Any,
    existing: list[Any],
    metadata_values: list[str],
    contexts: list[str],
) -> _DiscoveryResult:
    fallback = _fallback_discovery(existing, metadata_values, contexts)
    existing_payload = [
        {
            "external_id": peer.external_id,
            "display_name": peer.display_name,
            "kind": str(peer.kind),
            "metadata": peer.metadata,
        }
        for peer in existing
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "Identify the actual participants of one private conversation memory bank. "
                "Do not turn projects, tools, places, fictional characters, quoted roleplay, or people merely mentioned "
                "into peers. Reuse an existing peer external_id whenever it names a participant. Merge handles and names "
                "that clearly identify the same participant into aliases. Mark the assistant/agent whose memory bank this "
                "is as observer. Return only evidence-supported participants; uncertainty means omission."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "existing_peers": existing_payload,
                    "identity_metadata_values": metadata_values,
                    "source_context_samples": contexts,
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        result = await llm.call(
            messages,
            response_format=_DiscoveryResult,
            max_completion_tokens=1800,
            temperature=0.0,
            scope="peer_bootstrap.discovery",
            strict_schema=True,
        )
        if isinstance(result, _DiscoveryResult) and result.peers:
            return result
    except Exception as exc:
        logger.warning(
            "[PEER_BOOTSTRAP] discovery LLM failed; using metadata fallback (error_type=%s)",
            type(exc).__name__,
        )
    return fallback


def _match_existing_peer(discovered: _DiscoveredPeer, existing: list[Any]) -> Any | None:
    names = {discovered.external_id.casefold(), *(alias.casefold() for alias in discovered.aliases)}
    if discovered.display_name:
        names.add(discovered.display_name.casefold())
    for peer in existing:
        peer_names = {peer.external_id.casefold()}
        if peer.display_name:
            peer_names.add(peer.display_name.casefold())
        aliases = peer.metadata.get("aliases", []) if isinstance(peer.metadata, dict) else []
        peer_names.update(alias.casefold() for alias in aliases if isinstance(alias, str))
        if names & peer_names:
            return peer
    return None


async def _upsert_discovered_peers(service: Any, bank_id: str, discovery: _DiscoveryResult) -> tuple[list[Any], int]:
    current = list((await service.repository.list_peers(bank_id=bank_id, limit=1000, offset=0)).items)
    created = 0
    resolved: list[Any] = []
    for discovered in discovery.peers:
        peer = _match_existing_peer(discovered, current)
        aliases = list(dict.fromkeys([discovered.external_id, *discovered.aliases]))
        metadata = {"aliases": aliases, "discovered_by": "peer_bootstrap"}
        if peer is None:
            peer = await service.create_peer(
                bank_id,
                PeerCreate(
                    external_id=discovered.external_id,
                    display_name=discovered.display_name,
                    kind=discovered.kind,
                    metadata=metadata,
                ),
            )
            current.append(peer)
            created += 1
        else:
            merged_metadata = dict(peer.metadata)
            merged_aliases = list(
                dict.fromkeys(
                    [
                        *(merged_metadata.get("aliases") or []),
                        *aliases,
                    ]
                )
            )
            merged_metadata.update({"aliases": merged_aliases, "discovered_by": "peer_bootstrap"})
            peer = await service.update_peer(
                bank_id,
                peer.id,
                PeerUpdate(
                    display_name=peer.display_name or discovered.display_name,
                    kind=peer.kind,
                    metadata=merged_metadata,
                ),
            )
        resolved.append(peer)
    return resolved, created


def _peer_aliases(peer: Any) -> set[str]:
    aliases = {peer.external_id, peer.display_name or ""}
    if isinstance(peer.metadata, dict):
        aliases.update(alias for alias in peer.metadata.get("aliases", []) if isinstance(alias, str))
    return {alias.casefold() for alias in aliases if alias}


def _relevant_rows(rows: list[dict[str, Any]], peer: Any) -> list[dict[str, Any]]:
    aliases = _peer_aliases(peer)
    relevant: list[dict[str, Any]] = []
    for row in rows:
        haystack = f"{row.get('text', '')}\n{row.get('context', '')}".casefold()
        if any(alias in haystack for alias in aliases):
            relevant.append(row)
    return relevant


async def _extract_claim_batch(
    *,
    llm: Any,
    observer: Any,
    peers: list[Any],
    rows: list[dict[str, Any]],
) -> _ClaimBatch:
    allowed = [
        {
            "external_id": peer.external_id,
            "display_name": peer.display_name,
            "aliases": sorted(_peer_aliases(peer)),
        }
        for peer in peers
    ]
    evidence = [{"id": str(row["id"]), "text": row["text"]} for row in rows]
    messages = [
        {
            "role": "system",
            "content": (
                "Extract evidence-grounded directional peer claims from the supplied memories. The observer is the agent "
                "whose memory bank contains the evidence. Targets must be chosen only from allowed_peers. Ignore system "
                "wrappers, assistant-authored hypotheticals, roleplay, temporary task state, and facts merely mentioning a "
                "person without describing them. Prefer durable identity, attributes, relationships, and standing "
                "instructions. Behavioral inference may be returned only when supported by at least two source IDs and must "
                "set card_eligible=false. Direct explicit stable facts may set card_eligible=true. Every source_id must be an "
                "ID present in evidence. Keep claims concise, standalone, and free of timestamps unless time is essential."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "observer": observer.external_id,
                    "allowed_peers": allowed,
                    "evidence": evidence,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    serialized_messages = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    if len(serialized_messages.encode("utf-8")) > _MAX_EXTRACTION_MESSAGES_BYTES:
        raise ValueError("serialized extraction messages payload exceeds the UTF-8 byte bound")
    result = await llm.call(
        messages,
        response_format=_ClaimBatch,
        max_completion_tokens=3000,
        temperature=0.0,
        scope="peer_bootstrap.extract",
        strict_schema=True,
    )
    return result if isinstance(result, _ClaimBatch) else _ClaimBatch.model_validate(result)


async def _synthesize_claims(
    *,
    llm: Any,
    peer: Any,
    proposals: list[_ExtractedClaim],
    current_claims: list[PeerClaim] | None = None,
    max_card_entries: int,
) -> list[_FinalClaim]:
    if not proposals:
        return []
    payload = [proposal.model_dump(mode="json") for proposal in proposals]
    incremental = current_claims is not None
    if not incremental:
        # Keep the historical bootstrap prompt and schema byte-semantically stable;
        # supersession is an incremental-only semantic delta.
        user_content = json.dumps(
            {"target": peer.external_id, "proposals": payload},
            ensure_ascii=False,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "Deduplicate and consolidate claims about exactly one peer. Preserve source IDs from the proposals that "
                    "support each final claim; never invent IDs. Keep direct durable facts card_eligible=true with confidence "
                    ">=0.85 only when clearly supported. Keep behavioral patterns card_eligible=false and confidence <0.85. "
                    "Drop task-specific events, transitory states, weak speculation, prompt text, and synthetic assistant claims. "
                    f"Return at most {max_card_entries} card-eligible claims plus at most 20 representation-only patterns."
                ),
            },
            {"role": "user", "content": user_content},
        ]
        response_format: type[BaseModel] = _FinalClaims
    else:
        bounded_current_claims = (current_claims or [])[:_MAX_CURRENT_CLAIMS]
        current_payload = [
            {
                "id": claim.id,
                "claim_type": claim.claim_type.value,
                "text": claim.text,
                "confidence": claim.confidence,
                "locked": claim.locked,
                "origin": claim.origin.value,
                "source_ids": list(
                    dict.fromkeys(
                        source.source_id for source in claim.sources if source.source_kind.value == "memory_unit"
                    )
                )[:_MAX_CURRENT_SOURCE_IDS_PER_CLAIM],
            }
            for claim in bounded_current_claims
        ]
        user_content = json.dumps(
            {"target": peer.external_id, "proposals": payload, "current_claims": current_payload},
            ensure_ascii=False,
        )
        # These checks deliberately happen after serialization and immediately
        # before the provider call: oversized text is rejected, never truncated.
        if any(len(claim.text) > _MAX_INCREMENTAL_CURRENT_CLAIM_TEXT for claim in bounded_current_claims):
            raise ValueError("incremental current claim text exceeds the synthesis bound")
        if len(user_content.encode("utf-8")) > _MAX_INCREMENTAL_SYNTHESIS_USER_BYTES:
            raise ValueError("incremental synthesis input exceeds the serialized user-content bound")
        messages = [
            {
                "role": "system",
                "content": (
                    "Deduplicate and consolidate claims about exactly one peer. Preserve source IDs from the proposals that "
                    "support each final claim; never invent IDs. Keep direct durable facts card_eligible=true with confidence "
                    ">=0.85 only when clearly supported. Keep behavioral patterns card_eligible=false and confidence <0.85. "
                    "Drop task-specific events, transitory states, weak speculation, prompt text, and synthetic assistant claims. "
                    "Compare proposals with current_claims. A replacement may set supersede_claim_ids only to IDs of current "
                    "active derived, unlocked claims that it semantically replaces; never supersede locked or manual claims. "
                    f"Return at most {max_card_entries} card-eligible claims plus at most 20 representation-only patterns."
                ),
            },
            {"role": "user", "content": user_content},
        ]
        response_format = _IncrementalFinalClaims
    result = await llm.call(
        messages,
        response_format=response_format,
        max_completion_tokens=4000,
        temperature=0.0,
        scope="peer_bootstrap.synthesize",
        strict_schema=True,
    )
    parsed = result if isinstance(result, response_format) else response_format.model_validate(result)
    return cast(list[_FinalClaim], list(parsed.claims))


async def _write_result_metadata(
    memory_engine: "MemoryEngine", operation_id: str | None, payload: dict[str, Any]
) -> None:
    if not operation_id:
        return
    backend = await memory_engine._get_backend()
    async with acquire_with_retry(backend) as conn:
        await conn.execute(
            f"""
            UPDATE {fq_table("async_operations")}
            SET result_metadata = COALESCE(result_metadata, '{{}}'::jsonb) || $2::jsonb,
                updated_at = NOW()
            WHERE operation_id = $1
            """,
            __import__("uuid").UUID(operation_id),
            json.dumps({"peer_bootstrap": payload}),
        )


async def distill_directional_claims(
    *,
    memory_engine: "MemoryEngine",
    service: Any,
    bank_id: str,
    observer: Any,
    target: Any,
    source_ids: list[str],
    request_context: "RequestContext",
) -> list[PeerClaimDraft]:
    """Distil role-attributed evidence using the historical bootstrap semantics."""
    config = await memory_engine._config_resolver.resolve_full_config(bank_id, request_context)
    llm = memory_engine._consolidation_llm_config.with_config(
        config,
        bank_id=bank_id,
        operation="peer_modeling_incremental",
    )
    source_texts = await service.repository.get_memory_texts(bank_id=bank_id, source_ids=source_ids)
    rows = [
        {"id": source_id, "text": source_texts[source_id], "context": ""}
        for source_id in source_ids
        if source_texts.get(source_id, "").strip()
    ]
    if not rows:
        return []
    # This old role-attributed path intentionally does not apply the newer
    # target-alias prefilter; role attribution was its authoritative evidence boundary.
    extracted = await _extract_claim_batch(llm=llm, observer=observer, peers=[target], rows=rows)
    valid_ids = {str(row["id"]) for row in rows}
    proposals: list[_ExtractedClaim] = []
    for claim in extracted.claims:
        if claim.target_external_id.casefold() != target.external_id.casefold():
            continue
        claim.source_ids = [source_id for source_id in claim.source_ids if source_id in valid_ids]
        if claim.source_ids:
            proposals.append(claim)
    final_claims = await _synthesize_claims(
        llm=llm,
        peer=target,
        proposals=proposals,
        max_card_entries=config.peer_model_max_card_entries,
    )
    source_pool = {source_id for proposal in proposals for source_id in proposal.source_ids}
    drafts: list[PeerClaimDraft] = []
    for claim in final_claims:
        evidence = _validated_final_evidence(
            claim,
            source_pool=source_pool,
            min_pattern_sources=config.peer_model_min_pattern_sources,
        )
        if not evidence:
            continue
        confidence = max(0.85, claim.confidence) if claim.card_eligible else min(0.8, claim.confidence)
        drafts.append(
            PeerClaimDraft(
                claim_type=claim.claim_type,
                text=claim.text,
                confidence=confidence,
                source_ids=evidence,
            )
        )
    return drafts


async def distill_directional_claim_delta(
    *,
    memory_engine: "MemoryEngine",
    service: Any,
    bank_id: str,
    observer: Any,
    target: Any,
    source_ids: list[str],
    request_context: "RequestContext",
    current_claims: list[PeerClaim] | None = None,
    source_rows: list[Any] | None = None,
) -> PeerClaimDelta:
    """Reuse bootstrap attribution while keeping refresh evidence immutable."""
    config = await memory_engine._config_resolver.resolve_full_config(bank_id, request_context)
    llm = memory_engine._consolidation_llm_config.with_config(
        config,
        bank_id=bank_id,
        operation="peer_modeling_incremental",
    )
    if source_rows is None:
        source_texts = await service.repository.get_memory_texts(bank_id=bank_id, source_ids=source_ids)
        rows = [
            {"id": source_id, "text": source_texts[source_id], "context": ""}
            for source_id in source_ids
            if source_texts.get(source_id, "").strip()
        ]
    else:
        rows = [
            {"id": row.id, "text": row.text, "context": row.context}
            for row in source_rows
            if row.id in source_ids and row.text.strip()
        ]
    rows = _relevant_rows(rows, target)
    if not rows:
        return PeerClaimDelta()
    extracted = await _extract_claim_batch(llm=llm, observer=observer, peers=[target], rows=rows)
    valid_ids = {str(row["id"]) for row in rows}
    proposals: list[_ExtractedClaim] = []
    for claim in extracted.claims:
        if any(source_id not in valid_ids for source_id in claim.source_ids):
            raise ValueError("distiller returned a source outside the immutable refresh snapshot")
        if claim.target_external_id.casefold() != target.external_id.casefold():
            continue
        claim.source_ids = list(dict.fromkeys(claim.source_ids))
        if claim.source_ids:
            proposals.append(claim)
    final_claims = await _synthesize_claims(
        llm=llm,
        peer=target,
        proposals=proposals,
        current_claims=current_claims,
        max_card_entries=config.peer_model_max_card_entries,
    )
    source_pool = {str(source_id) for source_id in source_ids}
    source_pool.update(
        source.source_id
        for claim in current_claims or []
        if claim.status == "active"
        for source in claim.sources
        if source.source_kind.value == "memory_unit"
    )
    drafts: list[PeerClaimDraft] = []
    supersede_claim_ids: list[str] = []
    for claim in final_claims:
        evidence = _validated_final_evidence(
            claim,
            source_pool=source_pool,
            min_pattern_sources=config.peer_model_min_pattern_sources,
            fail_closed=True,
        )
        if not evidence:
            continue
        confidence = max(0.85, claim.confidence) if claim.card_eligible else min(0.8, claim.confidence)
        drafts.append(
            PeerClaimDraft(
                claim_type=claim.claim_type,
                text=claim.text,
                confidence=confidence,
                source_ids=evidence,
            )
        )
        if isinstance(claim, _IncrementalFinalClaim):
            supersede_claim_ids.extend(claim.supersede_claim_ids)
    return PeerClaimDelta(
        claims=drafts,
        supersede_claim_ids=list(dict.fromkeys(supersede_claim_ids)),
    )


async def run_peer_bootstrap(
    *,
    memory_engine: "MemoryEngine",
    bank_id: str,
    request_context: "RequestContext",
    operation_id: str | None,
) -> dict[str, Any]:
    """Discover peers and build historical directional cards for one bank."""
    started_at = _now_iso()
    config = await memory_engine._config_resolver.resolve_full_config(bank_id, request_context)
    if not config.enable_peer_modeling:
        return {"status": "disabled", "bank_id": bank_id}

    llm = memory_engine._consolidation_llm_config.with_config(config, bank_id=bank_id, operation="peer_bootstrap")
    service = await memory_engine._peer_modeling_service(bank_id, request_context)
    backend = await memory_engine._get_backend()

    await memory_engine._write_operation_progress(operation_id, stage="scanning", processed=0, total=None)
    async with acquire_with_retry(backend) as conn:
        rows_raw = await conn.fetch(
            f"""
            SELECT id, text, context, metadata, fact_type, mentioned_at, updated_at
            FROM {fq_table("memory_units")}
            WHERE bank_id = $1
              AND fact_type = 'observation'
            ORDER BY mentioned_at ASC NULLS LAST, created_at ASC, id ASC
            """,
            bank_id,
        )
        if not rows_raw:
            rows_raw = await conn.fetch(
                f"""
                SELECT id, text, context, metadata, fact_type, mentioned_at, updated_at
                FROM {fq_table("memory_units")}
                WHERE bank_id = $1
                  AND fact_type IN ('world', 'experience')
                ORDER BY mentioned_at ASC NULLS LAST, created_at ASC, id ASC
                """,
                bank_id,
            )
    rows = [dict(row) for row in rows_raw]
    source_versions = {str(row["id"]): row["updated_at"] for row in rows}
    if not rows:
        result = {
            "status": "completed",
            "started_at": started_at,
            "completed_at": _now_iso(),
            "evidence_total": 0,
            "evidence_processed": 0,
            "peers_discovered": 0,
            "peers_created": 0,
            "pairs_completed": 0,
            "claims_materialized": 0,
            "card_entries": 0,
            "ambiguous": 0,
        }
        await memory_engine._write_operation_progress(operation_id, stage="completed", processed=0, total=0)
        await _write_result_metadata(memory_engine, operation_id, result)
        logger.info(
            "[PEER_BOOTSTRAP] bank=%s operation=%s phase=completed evidence=0 peers=0 pairs=0 claims=0 cards=0",
            bank_id,
            operation_id,
        )
        return result
    metadata_values, contexts = _candidate_metadata(rows)
    existing = list((await service.repository.list_peers(bank_id=bank_id, limit=1000, offset=0)).items)

    await memory_engine._write_operation_progress(
        operation_id,
        stage="discovering_peers",
        processed=0,
        total=len(rows),
        detail={"evidence_total": len(rows), "existing_peers": len(existing)},
    )
    logger.info(
        "[PEER_BOOTSTRAP] bank=%s operation=%s phase=discovering_peers evidence=%d", bank_id, operation_id, len(rows)
    )
    discovery = await _discover_peers(
        llm=llm,
        existing=existing,
        metadata_values=metadata_values,
        contexts=contexts,
    )
    peers, peers_created = await _upsert_discovered_peers(service, bank_id, discovery)
    observer = next(
        (peer for peer in peers if peer.external_id.casefold() == discovery.observer_external_id.casefold()),
        peers[0] if peers else None,
    )
    if observer is None:
        raise ValueError("Peer bootstrap could not establish an observer peer")

    targets = peers
    relevant_by_target = {peer.id: _relevant_rows(rows, peer) for peer in targets}
    total_batches = sum((len(items) + _BATCH_SIZE - 1) // _BATCH_SIZE for items in relevant_by_target.values())
    batches_done = 0
    processed_evidence_ids: set[str] = set()
    evidence_processed = 0
    ambiguous_count = 0
    proposals_by_target: dict[str, list[_ExtractedClaim]] = defaultdict(list)

    for peer in targets:
        peer_rows = relevant_by_target[peer.id]
        for start in range(0, len(peer_rows), _BATCH_SIZE):
            batch_rows = peer_rows[start : start + _BATCH_SIZE]
            extracted = await _extract_claim_batch(llm=llm, observer=observer, peers=[peer], rows=batch_rows)
            valid_ids = {str(row["id"]) for row in batch_rows}
            for claim in extracted.claims:
                target = next(
                    (
                        candidate
                        for candidate in targets
                        if candidate.external_id.casefold() == claim.target_external_id.casefold()
                    ),
                    None,
                )
                source_ids = [source_id for source_id in claim.source_ids if source_id in valid_ids]
                if target is None or not source_ids:
                    ambiguous_count += 1
                    continue
                claim.source_ids = list(dict.fromkeys(source_ids))[:_MAX_SOURCE_IDS_PER_CLAIM]
                proposals_by_target[target.id].append(claim)
            ambiguous_count += extracted.ambiguous_count
            batches_done += 1
            processed_evidence_ids.update(str(row["id"]) for row in batch_rows)
            evidence_processed = len(processed_evidence_ids)
            await memory_engine._write_operation_progress(
                operation_id,
                stage="extracting_claims",
                processed=batches_done,
                total=max(total_batches, 1),
                detail={
                    "evidence_processed": evidence_processed,
                    "evidence_total": len(rows),
                    "peers_discovered": len(peers),
                    "claims_proposed": sum(len(value) for value in proposals_by_target.values()),
                    "ambiguous": ambiguous_count,
                },
            )
            logger.info(
                "[PEER_BOOTSTRAP] bank=%s operation=%s phase=extracting_claims batches=%d/%d evidence=%d/%d claims=%d",
                bank_id,
                operation_id,
                batches_done,
                total_batches,
                evidence_processed,
                len(rows),
                sum(len(value) for value in proposals_by_target.values()),
            )

    pairs_completed = 0
    claims_materialized = 0
    card_entries = 0
    for peer in targets:
        proposals = proposals_by_target.get(peer.id, [])
        deduped: dict[tuple[PeerClaimType, str], _ExtractedClaim] = {}
        for proposal in proposals:
            key = (proposal.claim_type, _normalize_claim(proposal.text))
            existing_proposal = deduped.get(key)
            if existing_proposal is None:
                deduped[key] = proposal
            else:
                existing_proposal.source_ids = list(
                    dict.fromkeys([*existing_proposal.source_ids, *proposal.source_ids])
                )[:_MAX_SOURCE_IDS_PER_CLAIM]
                existing_proposal.confidence = max(existing_proposal.confidence, proposal.confidence)
                existing_proposal.card_eligible = existing_proposal.card_eligible or proposal.card_eligible
        final_claims = await _synthesize_claims(
            llm=llm,
            peer=peer,
            proposals=list(deduped.values()),
            max_card_entries=config.peer_model_max_card_entries,
        )
        source_pool = {source_id for proposal in proposals for source_id in proposal.source_ids}
        drafts: list[PeerClaimDraft] = []
        for claim in final_claims:
            source_ids = _validated_final_evidence(
                claim,
                source_pool=source_pool,
                min_pattern_sources=config.peer_model_min_pattern_sources,
            )
            if not source_ids:
                ambiguous_count += 1
                continue
            confidence = max(0.85, claim.confidence) if claim.card_eligible else min(0.8, claim.confidence)
            drafts.append(
                PeerClaimDraft(
                    claim_type=claim.claim_type,
                    text=claim.text,
                    confidence=confidence,
                    source_ids=source_ids,
                )
            )
        if drafts:
            materialize_sources = list(dict.fromkeys(source_id for claim in drafts for source_id in claim.source_ids))
            model = await service.model(
                bank_id,
                observer.id,
                peer.id,
                PeerModelRequest(claims=drafts),
                validate_bank_sources=materialize_sources,
                expected_source_versions={source_id: source_versions[source_id] for source_id in materialize_sources},
                validate_existing_sources=True,
            )
            claims_materialized += len(drafts)
            card_entries += len(model.card.entries)
        pairs_completed += 1
        await memory_engine._write_operation_progress(
            operation_id,
            stage="materializing_cards",
            processed=pairs_completed,
            total=len(targets),
            detail={
                "peers_discovered": len(peers),
                "pairs_completed": pairs_completed,
                "claims_materialized": claims_materialized,
                "card_entries": card_entries,
                "ambiguous": ambiguous_count,
            },
        )

    result = {
        "status": "completed",
        "started_at": started_at,
        "completed_at": _now_iso(),
        "evidence_total": len(rows),
        "evidence_processed": evidence_processed,
        "peers_discovered": len(peers),
        "peers_created": peers_created,
        "observer_peer_id": observer.id,
        "pairs_completed": pairs_completed,
        "claims_materialized": claims_materialized,
        "card_entries": card_entries,
        "ambiguous": ambiguous_count,
    }
    await memory_engine._write_operation_progress(
        operation_id,
        stage="completed",
        processed=len(targets),
        total=len(targets),
        detail={
            "evidence_processed": evidence_processed,
            "evidence_total": len(rows),
            "peers_discovered": len(peers),
            "claims_materialized": claims_materialized,
            "card_entries": card_entries,
            "ambiguous": ambiguous_count,
        },
    )
    await _write_result_metadata(memory_engine, operation_id, result)
    logger.info(
        "[PEER_BOOTSTRAP] bank=%s operation=%s phase=completed evidence=%d peers=%d pairs=%d claims=%d cards=%d ambiguous=%d",
        bank_id,
        operation_id,
        len(rows),
        len(peers),
        pairs_completed,
        claims_materialized,
        card_entries,
        ambiguous_count,
    )
    return result


__all__ = ["distill_directional_claims", "run_peer_bootstrap"]
