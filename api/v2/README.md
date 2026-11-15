# API v2 contract · M0

openapi.json is generated from workflow/contracts.py by scripts/generate_workflow_contracts.py. Every operation is marked contract-only and has its target milestone. M0 does not register these routes in webui/server.py.

## Common rules

- Unknown fields are rejected. UUIDs, timezone-aware timestamps, positive state/revision numbers and lowercase SHA-256 hashes are validated.
- GET snapshots expose state_version, allowed_actions and last_event_seq. The frontend does not derive command permissions from its demo state machine.
- POST commands include Idempotency-Key and expected_state_version. HTTP 202 acknowledges storage, not execution completion. Versions and permissions are rechecked transactionally in M2.
- Review confirmation additionally binds route run/request scope, artifact_id, revision, content_hash and input_manifest_id. Saving a new revision invalidates old pending review content; the M3 service persists that change atomically.
- run_seq is a per-run cursor; global_seq is a different global-summary cursor. SSE uses the corresponding sequence as its id. A retained-history gap requires resynchronization.
- Download URLs accept run_id/artifact_id, never an arbitrary filesystem path. Server-side scope, manifest membership and path containment are M1/M3 implementation work.
- Secrets are not task configuration fields on this API. config_ref is a reference, not YAML or credentials. Event/log producers must redact secrets before persisting or emitting payloads.
- Body schema validation does not replace semantic state checks or authorization. Python policies and frontend semantic validators deliberately supplement JSON Schema.

The fixture in fixtures/contract-demo.json is synthetic contract-test data. It is not a real task, an example production config or a backend response captured from the user's workspace.
