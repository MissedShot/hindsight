# Native Peer Modeling for Hindsight v0.8.6

> **Execution boundary:** Implement inside Hindsight only. Do not modify Hermes, its provider, config, plugins, gateway, or memory runtime.

**Goal:** Add a native, bank-scoped peer-modeling subsystem that derives directional peer cards and richer relationship representations from retained memories, exposes them through the Hindsight API, and makes them operable from the Control Plane.

**Architecture:** A bank remains the memory-isolation boundary. Peers live inside a bank; a directional peer model is keyed by `(bank, observer, target)`. Existing facts and observations remain the evidence layer. A background `peer_modeling` operation incrementally produces typed, evidence-linked claims, a compact stable peer card, and a richer representation.

**Initial product scope:** Hindsight backend, REST API, async worker integration, storage/migrations, configuration, SDK/OpenAPI parity where required, and Control Plane UI. No Hermes integration or configuration changes.

---

## Design contract

### Concepts

- **Peer:** A bank-scoped identity such as a person, agent, team, project, or organization.
- **Directional peer model:** What `observer` currently knows about `target`. Self-models use `observer == target`.
- **Peer card:** Compact stable identity projection with only `IDENTITY`, `ATTRIBUTE`, `RELATIONSHIP`, and explicit `INSTRUCTION` entries.
- **Representation:** Richer evidence-grounded summary of preferences, behavior, collaboration patterns, relationship dynamics, contradictions, and temporal change.
- **Claim:** Atomic, typed, versioned derived statement with status, confidence, origin, and evidence links.

### Non-negotiable truth rules

- The card is a derived convenience view, never an independent source of truth.
- Advice, hypotheticals, fiction, roleplay, quotations, and assistant-authored plans must not silently become target biography.
- Behavioral patterns remain in representation, not the card.
- Explicit corrections supersede stale claims; manual corrections are preserved as locked claims.
- Every derived claim must link to existing bank-scoped evidence.
- Directional models never bleed into another observer/target pair or another bank.
- Existing retain/recall/reflect behavior remains unchanged when peer modeling is disabled.

## Proposed storage

1. `peers`
   - Bank-scoped stable identity and metadata.
   - Unique `(bank_id, external_id)`.
2. `memory_peer_roles`
   - Links a memory unit to a peer as `speaker`, `subject`, `observer`, `beneficiary`, or `mentioned`.
   - Records explicit/derived attribution and optional modality/confidence.
3. `peer_models`
   - Unique `(bank_id, observer_peer_id, target_peer_id)` current materialized card, representation, version, watermark, and timestamps.
4. `peer_model_claims`
   - Typed active/superseded/contested claims, origin, confidence, locked/manual status, and temporal validity.
5. `peer_model_claim_sources`
   - Portable relational evidence linkage to memory units/observations.

Use first-class source-link tables rather than hiding provenance in a JSONB blob. Migrations must support PostgreSQL and Oracle via `run_for_dialect`; PostgreSQL backup coverage must include every new table in dependency order.

## API surface

Under `/v1/default/banks/{bank_id}`:

- `POST /peers`
- `GET /peers`
- `GET /peers/{peer_id}`
- `PATCH /peers/{peer_id}`
- `GET /peers/{observer_id}/card?target={target_id}`
- `GET /peers/{observer_id}/representation?target={target_id}`
- `GET /peers/{observer_id}/context?target={target_id}`
- `GET /peers/{observer_id}/claims?target={target_id}`
- `POST /peers/{observer_id}/model?target={target_id}`
- `POST /peers/{observer_id}/rebuild?target={target_id}`
- `POST /peers/{observer_id}/corrections?target={target_id}`

Manual modeling/rebuild calls return an operation ID and use the existing operation-status endpoint.

## Retain attribution contract

Add an optional typed `peer_context` per retained content item:

```json
{
  "session_id": "optional",
  "message_id": "optional",
  "speaker_peer_id": "required for reliable speaker attribution",
  "observer_peer_id": "optional",
  "participant_peer_ids": ["optional"],
  "source_kind": "message"
}
```

The caller supplies speaker identity. LLM extraction may identify subjects/modality only among known peers; ambiguous names remain ordinary entities rather than auto-created peers.

## Modeling pipeline

1. Retain stores facts plus explicit speaker/observer linkage.
2. Fact extraction records target subjects and modality when available.
3. Normal observation consolidation runs unchanged.
4. Consolidation completion identifies affected observer/target pairs and submits at most one active `peer_modeling` task per pair.
5. The task reads new peer-linked raw facts, relevant observations, current claims/card, and locked manual corrections.
6. A structured-output updater proposes create/update/supersede/contest actions.
7. Deterministic validators enforce pair scope, source existence, card taxonomy, modality safety, minimum pattern evidence, and correction precedence.
8. Claims, source links, card, representation, version, and watermark commit atomically.
9. Metrics, progress, operation status, and webhooks expose the run.

When observations are disabled, retain may schedule peer modeling directly. Scheduling is controlled by minimum-new-facts and cooldown settings; row locks and watermarks make retries idempotent without advisory locks.

## Configuration

Proposed bank-configurable fields, defaulting to safe/off where appropriate:

- `enable_peer_modeling`
- `enable_auto_peer_modeling`
- `peer_model_min_new_facts`
- `peer_model_cooldown_seconds`
- `peer_model_max_card_entries`
- `peer_model_min_pattern_sources`
- `peer_model_representation_max_tokens`
- `peer_model_history_max_entries`
- operation-specific peer-model LLM provider/model/key/base URL with fallback to consolidation LLM

Update `config.py`, docs, root `.env.example`, bundled embed env template, and `/version` feature flags.

## Control Plane

Add a first-class **Peers** section to the bank page:

- peer registry/list/create/edit;
- observer/target selector, including self directions;
- peer card and richer representation;
- claims grouped by active/contested/superseded;
- evidence/source count and origin labels;
- version/history diff when available;
- run, refresh, and rebuild actions with operation progress;
- manual correction UI that creates a locked correction claim rather than replacing the whole card.

Use the existing Radix/shadcn visual language. This is an operational data surface: clarity, provenance, and state labels matter more than decorative motion.

## Implementation tasks

### Task 1 — Branch and baseline

- Start a feature branch from the pinned v0.8.6 checkout.
- Preserve the detached tag as the documented base revision.
- Add this plan under `.hermes/plans/`.
- Run only fast baseline checks needed to identify pre-existing failures.

### Task 2 — Peer storage and typed domain models

Likely files:

- Create: `hindsight-api-slim/hindsight_api/engine/peer_modeling/models.py`
- Create: `hindsight-api-slim/hindsight_api/engine/peer_modeling/service.py`
- Create: migration under `hindsight-api-slim/hindsight_api/alembic/versions/`
- Modify: `hindsight-api-slim/hindsight_api/models.py`
- Modify: `hindsight-api-slim/hindsight_api/admin/cli.py`
- Test: focused migration-shape, backup coverage, and peer-engine tests.

Deliver peer CRUD, directional model lookup, claims, source linkage, and deterministic card materialization before adding LLM behavior.

### Task 3 — REST API

Likely files:

- Create: `hindsight-api-slim/hindsight_api/api/peers.py`
- Modify: `hindsight-api-slim/hindsight_api/api/http.py`
- Modify: `hindsight-api-slim/hindsight_api/engine/interface.py`
- Modify: `hindsight-api-slim/hindsight_api/engine/memory_engine.py`
- Test: focused HTTP integration tests for auth, isolation, CRUD, card/context, correction, and operation submission.

Handlers remain thin; all auth and persistence stay in the engine layer.

### Task 4 — Retain attribution

Likely files:

- Modify: `hindsight-api-slim/hindsight_api/engine/retain/types.py`
- Modify: retain request models in `hindsight-api-slim/hindsight_api/api/http.py`
- Modify: `hindsight-api-slim/hindsight_api/engine/retain/orchestrator.py`
- Modify: `hindsight-api-slim/hindsight_api/engine/retain/fact_extraction.py`
- Test: deterministic propagation tests plus one real-LLM/judge attribution test later, not a large provider matrix.

The first implementation may support explicit speaker/observer linkage before model-derived subject classification. Do not block the vertical slice on a large attribution taxonomy.

### Task 5 — Peer modeling operation

Likely files:

- Create: `hindsight-api-slim/hindsight_api/engine/peer_modeling/prompts.py`
- Create: `hindsight-api-slim/hindsight_api/engine/peer_modeling/orchestrator.py`
- Create: `hindsight-api-slim/hindsight_api/engine/peer_modeling/validation.py`
- Modify: `hindsight-api-slim/hindsight_api/engine/memory_engine.py`
- Modify: `hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py`
- Modify: async-operation worker routing and metrics/webhook surfaces.
- Test: one focused idempotency/dedup test, one correction-safety test, one structured-output orchestration test.

Start with a constrained structured-output updater. Do not implement Honcho-style autonomous deduction/induction specialists in the first vertical slice.

### Task 6 — Config and feature discovery

- Add hierarchical config fields and safe defaults.
- Add feature flag exposure.
- Update documentation and synchronized env templates.
- Add focused precedence and env-template sync checks.

### Task 7 — OpenAPI and wrappers

- Generate OpenAPI after API stabilizes.
- Add equivalent Python and TypeScript convenience-wrapper methods.
- Regenerate low-level clients only when required by the repository workflow.
- Run existing coverage/parity checks rather than inventing a broad new suite.

### Task 8 — Control Plane

Likely files:

- Modify: `hindsight-control-plane/src/app/[locale]/banks/[bankId]/page.tsx`
- Create: `hindsight-control-plane/src/components/peers-view.tsx`
- Create: peer proxy routes under `hindsight-control-plane/src/app/api/`
- Modify: `hindsight-control-plane/src/lib/api.ts`
- Modify: feature context and locale dictionaries.
- Test: proxy-route/client smoke coverage, locale-key checks, and production build.

### Task 9 — Integration and focused verification

Required gates, intentionally bounded:

- focused backend peer-modeling tests;
- migration-shape and backup-table guard;
- API integration tests for new endpoints;
- Ruff on changed Python files and `ty` on the new package/API boundary;
- Control Plane tests covering new client/routes plus `npm run build`;
- `git diff --check` and final changed-file review;
- no Hermes files, config, plugins, or runtime changes.

Do not spend time on the full real-LLM matrix, unrelated legacy suites, release machinery, or a live Hermes cutover in this implementation pass.

## Deferred work

- Honcho-style autonomous deduction and induction specialists.
- Cross-bank peers or global peer identity.
- Automatic historical role inference over ambiguous legacy archives.
- Hermes provider integration, auto-context injection, cadence, or migration.
- Production deployment or live-bank migrations.

## Acceptance criteria for the first vertical slice

- A bank can create/list peers.
- A directional `(observer, target)` model can be read independently of all other directions.
- Explicitly linked memory evidence can produce typed claims, a stable card, and a representation through a tracked operation.
- A manual correction supersedes a stale claim and survives later modeling.
- Every claim exposes evidence/provenance.
- Feature-disabled banks behave as before.
- Control Plane displays and operates the peer model without requiring direct API calls.
- PostgreSQL/Oracle migration shape and PostgreSQL backup coverage are accounted for.
- Hermes remains untouched.
