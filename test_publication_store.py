"""Focused tests for publication-run persistence and migration."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import store


DIGEST = "a" * 64
COMMIT = "b" * 40


class PublicationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = store.connect(":memory:")

    def tearDown(self) -> None:
        self.connection.close()

    def test_legacy_database_is_migrated_without_changing_collection_history(self) -> None:
        self.connection.close()
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(str(database_path))
            legacy.execute(
                "CREATE TABLE collection_runs ("
                "run_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "started_at TEXT NOT NULL, finished_at TEXT, expected_date TEXT, "
                "status TEXT NOT NULL CHECK (status IN ('running', 'success', 'error')), "
                "observation_key TEXT, error_text TEXT)"
            )
            legacy.execute(
                "INSERT INTO collection_runs "
                "(started_at, finished_at, expected_date, status, observation_key) "
                "VALUES (?, ?, ?, 'success', ?)",
                (
                    "2026-09-23T07:00:00+00:00",
                    "2026-09-23T07:01:00+00:00",
                    "2026-09-23",
                    "official:2026-09-23",
                ),
            )
            legacy.commit()
            legacy.close()

            migrated = store.connect(database_path)
            try:
                collection = migrated.execute(
                    "SELECT status, observation_key FROM collection_runs WHERE run_id = 1"
                ).fetchone()
                self.assertEqual(collection["status"], "success")
                self.assertEqual(collection["observation_key"], "official:2026-09-23")
                self.assertEqual(store.publication_runs(migrated), [])

                run_id = store.start_publication_run(
                    migrated,
                    collection_run_id=1,
                    public_url="https://hanbong5938.github.io/dram-tracker/",
                )
                store.finish_publication_run(migrated, run_id, status="blocked")
                self.assertEqual(store.publication_runs(migrated)[0]["status"], "blocked")
            finally:
                migrated.close()
        self.connection = store.connect(":memory:")

    def test_failed_and_pending_publications_do_not_change_successful_collection(self) -> None:
        collection_run_id = store.start_collection_run(self.connection, "2026-09-24")
        store.finish_collection_run(
            self.connection,
            collection_run_id,
            status="success",
            observation_key="official:2026-09-24",
        )

        failed_id = store.start_publication_run(
            self.connection,
            collection_run_id=collection_run_id,
            public_url="https://example.test/dram/",
        )
        store.finish_publication_run(
            self.connection, failed_id, status="error", error_text="git push failed"
        )
        pending_id = store.start_publication_run(
            self.connection,
            collection_run_id=collection_run_id,
            public_url="https://example.test/dram/",
        )
        store.finish_publication_run(
            self.connection,
            pending_id,
            status="pending",
            data_digest=DIGEST,
            commit_sha=COMMIT,
            error_text="live page has not converged",
        )

        collection = self.connection.execute(
            "SELECT status, observation_key, error_text FROM collection_runs WHERE run_id = ?",
            (collection_run_id,),
        ).fetchone()
        self.assertEqual(dict(collection), {
            "status": "success",
            "observation_key": "official:2026-09-24",
            "error_text": None,
        })
        publications = store.publication_runs(self.connection)
        self.assertEqual([row["status"] for row in publications], ["pending", "error"])
        self.assertTrue(all(row["collection_run_id"] == collection_run_id for row in publications))

    def test_running_run_has_one_valid_terminal_transition(self) -> None:
        run_id = store.start_publication_run(
            self.connection, public_url="https://example.test/dram/"
        )
        with self.assertRaises(ValueError):
            store.finish_publication_run(self.connection, run_id, status="running")
        with self.assertRaises(ValueError):
            store.finish_publication_run(self.connection, run_id, status="published")
        self.assertEqual(store.publication_runs(self.connection)[0]["status"], "running")

        store.finish_publication_run(
            self.connection,
            run_id,
            status="published",
            data_digest=DIGEST.upper(),
            commit_sha=COMMIT.upper(),
        )
        finished = store.publication_runs(self.connection)[0]
        self.assertEqual(finished["status"], "published")
        self.assertEqual(finished["data_digest"], DIGEST)
        self.assertEqual(finished["commit_sha"], COMMIT)
        self.assertIsNotNone(finished["finished_at"])

        with self.assertRaises(ValueError):
            store.finish_publication_run(
                self.connection, run_id, status="error", error_text="conflicting result"
            )
        with self.assertRaises(ValueError):
            store.finish_publication_run(self.connection, 999, status="blocked")
        with self.assertRaises(ValueError):
            store.finish_publication_run(self.connection, 0, status="blocked")

    def test_ids_hashes_links_and_public_urls_are_validated_before_insert(self) -> None:
        invalid_starts = (
            {"collection_run_id": 999},
            {"collection_run_id": True},
            {"public_url": "https://user:secret@example.test/dram/"},
            {"public_url": "file:///tmp/index.html"},
        )
        for arguments in invalid_starts:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    store.start_publication_run(self.connection, **arguments)
        self.assertEqual(store.publication_runs(self.connection), [])

        run_id = store.start_publication_run(self.connection)
        with self.assertRaises(ValueError):
            store.finish_publication_run(
                self.connection,
                run_id,
                status="pending",
                data_digest="not-a-digest",
                commit_sha=COMMIT,
            )
        with self.assertRaises(ValueError):
            store.finish_publication_run(
                self.connection,
                run_id,
                status="pending",
                data_digest=DIGEST,
                commit_sha="short",
            )
        self.assertEqual(store.publication_runs(self.connection)[0]["status"], "running")

    def test_caller_rollback_controls_start_and_finish_atomically(self) -> None:
        self.connection.execute("BEGIN")
        rolled_back_id = store.start_publication_run(
            self.connection, public_url="http://127.0.0.1:8000/index.html"
        )
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(store.publication_runs(self.connection)[0]["run_id"], rolled_back_id)
        self.connection.rollback()
        self.assertEqual(store.publication_runs(self.connection), [])

        run_id = store.start_publication_run(self.connection)
        self.connection.execute("BEGIN")
        store.finish_publication_run(
            self.connection,
            run_id,
            status="pending",
            data_digest=DIGEST,
            commit_sha="c" * 64,
            error_text="awaiting live verification",
        )
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(store.publication_runs(self.connection)[0]["status"], "pending")
        self.connection.rollback()
        restored = store.publication_runs(self.connection)[0]
        self.assertEqual(restored["status"], "running")
        self.assertIsNone(restored["finished_at"])


if __name__ == "__main__":
    unittest.main()
