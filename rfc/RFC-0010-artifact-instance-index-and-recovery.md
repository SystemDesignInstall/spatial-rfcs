# RFC-0010 — Artifact Instance Index and Recovery

- **Status:** draft
- **Author:** Spatial Platform contributor
- **Date:** 2026-10-28
- **Supersedes:** RFC-0006's no-new-migration constraint for the artifact index only; its instance identity and immutability requirements remain binding.
- **Depends on:** RFC-0001, RFC-0002, RFC-0003, RFC-0006, RFC-0007, ADR-008, ADR-009, ADR-010, ADR-014, ADR-015, ADR-020, ADR-028, ADR-035.
- **Protected surfaces touched:** Artifact Format (`core/artifacts/**`), Scene Query (`core/scene/**`), database schema and migrations (`schemas/**`).

## Summary

Allow multiple independently identified artifact instances to reference one
content-addressed payload. Introduce a versioned SQLite migration for the
artifact index, preserve existing UUIDs and CAS hashes, and define an explicit,
auditable recovery path for manifests whose index entries were previously
displaced. A content hash is payload identity; an artifact UUID is instance
identity. Neither is a substitute for the other.

This is a draft, not authorization to implement or run the migration. It must
pass Architecture Review and be ratified under the Constitution before protected
implementation changes are made.

## Motivation

RFC-0006 §6.5 and its acceptance criteria require two captures with identical
bytes and different contexts to produce one CAS payload but two independent
artifact instances, each with its own UUID and provenance. `ArtifactStore::PutInstance`
creates the second manifest, but migration `0001` declares
`artifacts.content_hash TEXT NOT NULL UNIQUE` and `MetadataDb::UpsertArtifact`
uses `INSERT OR REPLACE`. Inserting the second instance deletes the first index
row because of the hash constraint.

This was reproduced against a real project database and CAS on 2026-10-28:
after `Put(payload, A)` followed by `PutInstance(hash, B)`, both manifests
existed, `SceneQuery::ArtifactHash(B)` resolved, and
`SceneQuery::ArtifactHash(A)` returned no value. An older ImageObservation may
still reference A, so feature extraction can fail even though its payload and
manifest are present. Existing `DedupCase2SharedPayloadTwoInstances` asserts
only one index row and does not check the first UUID. The observed behavior
violates RFC-0006's identity contract.

The source-file reuse implemented by the current trajectory importer avoids
calling `PutInstance`, but this does not repair the image-import path or any
previously displaced index records.

## Design

### Identity and lookup invariants

1. `artifact_id` is a unique immutable instance UUID. One content hash may
   have multiple rows with distinct UUIDs, manifests and provenance.
2. A CAS hash continues to identify one immutable byte payload. Existing CAS
   paths and manifest JSON stay compatible; no image bytes are duplicated.
3. `FindArtifactById` resolves every indexed instance independently.
   `FindArtifactsByType` returns every instance. A hash lookup returning one
   instance must have deterministic documented selection; it must not be used
   to assert which observation an instance belongs to.
4. `Put` remains content-deduplicating and returns an existing appropriate
   instance when its documented conditions hold. `PutInstance` publishes a
   distinct instance without deleting or mutating any previous instance.
   Reusing an existing UUID with different immutable metadata is an error.
5. The shared payload must be verified before a new instance or recovery row
   refers to it. A hash mismatch never becomes a successful import.
6. Concurrent calls and restart/retry must not produce a silent replacement,
   duplicate primary publication, lost identity or a partially visible
   observation. The DB transaction and filesystem publication protocol must
   explicitly address their separate failure points.

### Versioned migration

The proposed migration, tentatively `0009`, changes the `artifacts` table from
one row per content hash to one row per instance: retain the primary key on
`artifact_id`, remove the uniqueness constraint on `content_hash`, and retain a
non-unique hash index. Preserve each existing row's UUID, hash, type, schema
version, producer, configuration, timestamps, units and validation status.
The migration must be transactional, verify foreign-key integrity, support an
unchanged legacy project, and leave the database at its prior version if it
fails. Do not edit historical migrations in place.

The implementation must define atomic and deterministic `Put` behavior under
concurrent calls. If an additional payload-identity index/table or a partial
unique index is necessary to preserve it, include it in the reviewed migration
and test its upgrade and recovery behavior. The SQL above is a data-model
direction, not a complete concurrent publication algorithm.

Moving to the new schema does **not** automatically restore rows that were
already displaced. Migration must not silently read arbitrary files or infer
their contents. No downgrade should silently collapse multiple instances into
one; backup/restore and explicit incompatibility must be documented.

### Existing-project diagnosis and explicit recovery

Provide a read-only, dry-run-by-default audit for observation artifact UUIDs,
index rows, manifests and CAS bytes. Distinguish at least:

- indexed instance with consistent manifest and verified payload;
- referenced UUID missing from the index, but with a matching manifest and
  SHA-256-verified payload, potentially recoverable;
- missing/malformed manifest, missing/corrupt payload, UUID mismatch, conflicting
  metadata or ambiguous evidence, requiring manual handling.

A separate explicitly requested repair may restore only verified missing index
records, preserving the original UUID, content hash and manifest metadata.
It must never rewrite the observation, create a replacement UUID, invent missing
provenance, overwrite a conflicting record, or treat a hash as proof of a
physical measurement. Unknown metadata remains unknown; in particular an
unrecoverable nanosecond creation timestamp must not be fabricated from a
second-resolution manifest. Report every restored, skipped and conflicted
record; make repair transactional and idempotent. Validate UUID/hash syntax and
path boundaries before reading files. No automatic repair on project open.

### Compatibility and rollout

- RFC-0006's existing distinction between same-content payload and distinct
  context-specific instances is preserved; its statement that no new migration
  is necessary is superseded for this artifact-index correction only.
- Old `.spx` projects upgrade through the normal migration runner with backup
  guidance. A repair dry-run is required to discover prior displaced entries.
- Worker protocols, CAS hash calculation, canonical image payloads, Frame IDs,
  Observation IDs and image observations remain unchanged.
- Schema version, error codes, audit output and any public API changes need
  Architecture Review before being frozen. The project must not claim repaired
  data merely because the schema migration passed.

## Alternatives

1. Reuse one UUID for all identical content: rejected because it conflates
   capture-specific provenance and contradicts RFC-0006.
2. Recover a missing UUID by scanning manifests inside every SceneQuery call:
   rejected because it hides index corruption, crosses the storage/read boundary
   and does not provide transactional repair.
3. Store UUID aliases in unrelated JSON or dependency fields: rejected because
   it creates an undocumented second index and fragile consistency rules.
4. Keep a one-hash payload table and add a separate instance table: viable
   alternative if the Architecture Review finds it safer for concurrency and
   long-term normalization. It must meet the same identity, migration,
   compatibility and recovery requirements.

## Open Questions

1. Should payload identity and instances become separate normalized tables in
   this migration, or should `artifacts` remain a single non-unique-hash table
   with a dedicated uniqueness mechanism for `Put`?
2. How should unknown recovered timestamps be represented by existing typed
   metadata APIs without conflating unknown with Unix epoch zero?
3. Which manifest fields are mandatory for automatic recovery, and how should
   conflicting but otherwise readable legacy manifests be escalated?
4. What fault-injection and Windows filesystem guarantees are required to close
   the remaining CAS/SQLite crash-recovery gap?

## Impact

Acceptance must include fresh and upgraded-project tests on Linux and Windows;
two identical images from different contexts with both UUIDs resolvable and
both observations usable in feature extraction; idempotent re-import; payload
and manifest integrity failures; explicit dry-run and repair of a displaced
legacy instance; conflicts and missing data; injected failure/restart at each
publication boundary; and supported concurrency. Update existing tests that
currently assert a single index row for two instances. Run the full C++ matrix,
schema/worker/domain/dependency gates, and Architecture Review after ratification.
