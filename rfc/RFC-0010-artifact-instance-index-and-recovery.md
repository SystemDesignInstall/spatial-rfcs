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
The existing `check_constitution.py` previously checked only the shape of an RFC
reference, not whether it existed or was ratified. A separate implementation
change is proposed to check status; neither that check nor an RFC identifier
alone substitutes for an Architecture Review of the change's scope.

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
   `FindArtifactsByType` returns every instance. `FindArtifactByHash` returns the
   stable primary instance recorded in the payload index; it must not be used
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
7. Garbage collection must preserve a shared CAS payload while any live
   instance, observation or dependency still refers to it, and must not delete
   another instance's manifest as a side effect of removing one instance.

### Versioned migration

The proposed migration, tentatively `0009`, changes the `artifacts` table from
one row per content hash to one row per instance: retain the primary key on
`artifact_id`, remove the uniqueness constraint on `content_hash`, and retain a
non-unique hash index. Add `artifact_payload_index` with one primary instance ID
per content hash, enforced by a hash primary key and a composite foreign key
to the matching `(artifact_id, content_hash)` in `artifacts`. The primary is an
index-selection rule, not a claim that the instance is the earliest capture.
Initialize it from each existing legacy row. Preserve each row's UUID, hash, type, schema
version, producer, configuration, timestamps, units and validation status.
The migration must be transactional, verify foreign-key integrity, support an
unchanged legacy project, and leave the database at its prior version if it
fails. Do not edit historical migrations in place.

`Put` must serialize its check-and-insert in a project/connection-scoped writer
transaction, re-check the payload index after acquiring the writer lock, and
return the recorded primary when it already exists. `PutInstance` inserts an
independent row and does not change the payload index. Replace the destructive
`INSERT OR REPLACE` use for instance creation with conflict-detecting insertion;
validation-status changes use an explicit update. Existing outer host
transactions must be supported without transaction interleaving or a nested
`BEGIN`; lock ownership and savepoints must be reviewed and tested. The
filesystem cannot participate in SQLite's atomic commit, so orphaned files are
reported and handled by the separate crash-recovery protocol.

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
physical measurement. Unknown metadata remains unknown. Store an unrecoverable
nanosecond creation timestamp as SQL `NULL`; update typed read APIs to represent
it as optional rather than reporting epoch zero. A recoverable instance is
inserted without changing an existing primary payload index. If a hash has no
indexed primary, report it for manual handling rather than inventing one.
Report every restored, skipped and conflicted
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
4. Keep only a non-unique instance table and select any row on hash lookup:
   rejected because concurrent `Put` calls can create competing primary
   instances and an unordered lookup makes selection unstable. The dedicated
   payload index separates those concerns.

## Review checkpoints

Architecture Review must inspect the complete legacy-table copy, lock ownership,
outer-transaction behavior, exact mandatory manifest fields for recovery, and
Windows filesystem failure behavior. Conflicts or insufficient provenance are
reported for manual handling; they are not grounds to invent a record. The
prototype under `experimental/` demonstrates the proposed database identity
and explicit recovery mechanics, but does not establish a production-ready
migration or cross-filesystem crash safety.

## Impact

Acceptance must include fresh and upgraded-project tests on Linux and Windows;
two identical images from different contexts with both UUIDs resolvable and
both observations usable in feature extraction; idempotent re-import; payload
and manifest integrity failures; explicit dry-run and repair of a displaced
legacy instance; conflicts and missing data; injected failure/restart at each
publication boundary; and supported concurrency. Update existing tests that
currently assert a single index row for two instances. Run the full C++ matrix,
schema/worker/domain/dependency gates, and Architecture Review after ratification.
