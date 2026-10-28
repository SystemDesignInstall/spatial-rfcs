#!/usr/bin/env python3
"""RFC-0010 experimental identity/recovery proof; not production migration.

Uses a deliberately reduced SQLite schema, temporary databases, and in-memory
payloads. It does not inspect or modify a Spatial Platform project. Run with
`python experimental/artifact_instance_index_prototype.py`.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def create_legacy(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE artifacts (
            artifact_id BLOB PRIMARY KEY,
            content_hash TEXT NOT NULL UNIQUE,
            producer_json TEXT NOT NULL,
            created_at_ns INTEGER
        );
        PRAGMA user_version = 8;
    """)


def migrate(db: sqlite3.Connection, inject_failure: bool = False) -> None:
    """Prototype only: preserve rows, remove hash uniqueness, add payload index."""
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""CREATE TABLE artifacts_new (
            artifact_id BLOB PRIMARY KEY,
            content_hash TEXT NOT NULL,
            producer_json TEXT NOT NULL,
            created_at_ns INTEGER,
            UNIQUE (artifact_id, content_hash)
        )""")
        db.execute("INSERT INTO artifacts_new SELECT * FROM artifacts")
        if inject_failure:
            raise RuntimeError("simulated migration failure")
        db.execute("DROP TABLE artifacts")
        db.execute("ALTER TABLE artifacts_new RENAME TO artifacts")
        db.execute("CREATE INDEX artifact_hash_index ON artifacts(content_hash)")
        db.execute("""CREATE TABLE artifact_payload_index (
            content_hash TEXT PRIMARY KEY,
            primary_artifact_id BLOB NOT NULL UNIQUE,
            FOREIGN KEY (primary_artifact_id, content_hash)
                REFERENCES artifacts (artifact_id, content_hash)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        )""")
        db.execute("""INSERT INTO artifact_payload_index
                      SELECT content_hash, artifact_id FROM artifacts""")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("foreign-key check failed")
        db.execute("PRAGMA user_version = 9")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def put(db: sqlite3.Connection, payload: bytes, candidate: uuid.UUID) -> bytes:
    digest = hashlib.sha256(payload).hexdigest()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT primary_artifact_id FROM artifact_payload_index WHERE content_hash = ?",
            (digest,),
        ).fetchone()
        if row is None:
            db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                       (candidate.bytes, digest, '{"id":"example"}', 10))
            db.execute("INSERT INTO artifact_payload_index VALUES (?, ?)",
                       (digest, candidate.bytes))
            result = candidate.bytes
        else:
            result = row[0]
        db.execute("COMMIT")
        return result
    except BaseException:
        db.execute("ROLLBACK")
        raise


def put_instance(db: sqlite3.Connection, payload: bytes, instance: uuid.UUID) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM artifact_payload_index WHERE content_hash = ?",
                      (digest,)).fetchone() is None:
            raise ValueError("payload has no indexed primary")
        db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                   (instance.bytes, digest, '{"id":"example"}', 20))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def recover(db: sqlite3.Connection, manifest: dict, payload: bytes,
            *, dry_run: bool = True) -> str:
    """Restore a missing verified instance only if a primary already exists."""
    try:
        instance = uuid.UUID(manifest["artifact_uuid"])
        digest = manifest["content_hash"]
        producer = manifest["producer"]
        if not isinstance(producer, dict) or not producer.get("id"):
            raise ValueError("producer missing")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid hash")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("payload hash mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid evidence") from exc
    producer_json = json.dumps(producer, sort_keys=True, separators=(",", ":"))
    db.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
    try:
        current = db.execute("SELECT content_hash, producer_json FROM artifacts WHERE artifact_id = ?",
                             (instance.bytes,)).fetchone()
        if current is not None:
            if current != (digest, producer_json):
                raise ValueError("conflicting artifact identity")
            result = "already_indexed"
        elif db.execute("SELECT 1 FROM artifact_payload_index WHERE content_hash = ?",
                        (digest,)).fetchone() is None:
            result = "manual_review_missing_primary"
        elif dry_run:
            result = "would_restore"
        else:
            # Nanosecond insertion time is unavailable in this evidence.
            db.execute("INSERT INTO artifacts VALUES (?, ?, ?, NULL)",
                       (instance.bytes, digest, producer_json))
            result = "restored"
        db.execute("COMMIT")
        return result
    except BaseException:
        db.execute("ROLLBACK")
        raise


class ArtifactIndexPrototypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="artifact_index_prototype_")
        self.path = Path(self.temp.name) / "index.db"
        self.db = connect(self.path)
        create_legacy(self.db)
        self.payload = b"same image bytes"
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.a = uuid.uuid4()
        self.b = uuid.uuid4()
        self.producer = {"id": "image-import", "version": "1"}
        self.producer_json = json.dumps(self.producer, sort_keys=True, separators=(",", ":"))

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def legacy_index_collision(self) -> dict:
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?)",
                        (self.a.bytes, self.digest, self.producer_json, 1))
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, ?)",
                        (self.b.bytes, self.digest, self.producer_json, 2))
        self.assertIsNone(self.db.execute("SELECT 1 FROM artifacts WHERE artifact_id = ?",
                                          (self.a.bytes,)).fetchone())
        return {"artifact_uuid": str(self.a), "content_hash": self.digest,
                "producer": self.producer}

    def test_migrate_then_explicitly_restore_displaced_instance(self) -> None:
        manifest = self.legacy_index_collision()
        migrate(self.db)
        self.assertEqual(recover(self.db, manifest, self.payload), "would_restore")
        self.assertIsNone(self.db.execute("SELECT 1 FROM artifacts WHERE artifact_id = ?",
                                          (self.a.bytes,)).fetchone())
        self.assertEqual(recover(self.db, manifest, self.payload, dry_run=False), "restored")
        self.assertEqual(recover(self.db, manifest, self.payload, dry_run=False), "already_indexed")
        rows = self.db.execute("SELECT artifact_id, created_at_ns FROM artifacts ORDER BY artifact_id").fetchall()
        self.assertEqual({r[0] for r in rows}, {self.a.bytes, self.b.bytes})
        self.assertIsNone(dict(rows)[self.a.bytes])
        self.assertEqual(self.db.execute("SELECT primary_artifact_id FROM artifact_payload_index").fetchone()[0],
                         self.b.bytes)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_invalid_payload_and_conflicting_identity_are_not_repaired(self) -> None:
        manifest = self.legacy_index_collision()
        migrate(self.db)
        with self.assertRaises(ValueError):
            recover(self.db, manifest, b"corrupted", dry_run=False)
        conflicting = dict(manifest, artifact_uuid=str(self.b), producer={"id": "other"})
        with self.assertRaises(ValueError):
            recover(self.db, conflicting, self.payload, dry_run=False)
        self.assertEqual(self.db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)

    def test_dry_run_works_on_a_read_only_connection(self) -> None:
        manifest = self.legacy_index_collision()
        migrate(self.db)
        readonly = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                                   timeout=10, isolation_level=None)
        try:
            self.assertEqual(recover(readonly, manifest, self.payload), "would_restore")
            self.assertIsNone(readonly.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ?", (self.a.bytes,)).fetchone())
        finally:
            readonly.close()

    def test_missing_primary_requires_manual_review(self) -> None:
        migrate(self.db)
        manifest = {"artifact_uuid": str(self.a), "content_hash": self.digest,
                    "producer": self.producer}
        self.assertEqual(recover(self.db, manifest, self.payload, dry_run=False),
                         "manual_review_missing_primary")
        self.assertEqual(self.db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)

    def test_migration_failure_rolls_back_schema_and_rows(self) -> None:
        self.legacy_index_collision()
        with self.assertRaises(RuntimeError):
            migrate(self.db, inject_failure=True)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertEqual(self.db.execute("SELECT artifact_id FROM artifacts").fetchone()[0], self.b.bytes)
        self.assertIsNone(self.db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'artifact_payload_index'").fetchone())

    def test_two_instances_remain_resolvable_and_uuid_reuse_fails(self) -> None:
        migrate(self.db)
        self.assertEqual(put(self.db, self.payload, self.a), self.a.bytes)
        put_instance(self.db, self.payload, self.b)
        with self.assertRaises(sqlite3.IntegrityError):
            put_instance(self.db, self.payload, self.a)
        rows = self.db.execute("SELECT artifact_id FROM artifacts WHERE content_hash = ?",
                               (self.digest,)).fetchall()
        self.assertEqual({r[0] for r in rows}, {self.a.bytes, self.b.bytes})
        self.assertEqual(self.db.execute("SELECT primary_artifact_id FROM artifact_payload_index").fetchone()[0],
                         self.a.bytes)

    def test_concurrent_put_selects_one_primary(self) -> None:
        migrate(self.db)
        barrier = threading.Barrier(2)

        def worker(candidate: uuid.UUID) -> bytes:
            db = connect(self.path)
            try:
                barrier.wait(timeout=5)
                return put(db, self.payload, candidate)
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, item) for item in (self.a, self.b)]
            ids = [future.result(timeout=10) for future in futures]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM artifact_payload_index").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
