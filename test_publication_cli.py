"""Behavioral tests for publication-oriented tracker commands."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect
import publisher
import store
import tracker


_DIGEST = "a" * 64
_COMMIT = "b" * 40


def _config(root: Path, *, authorized: bool = True) -> Path:
    remote = root / "public.git"
    remote.mkdir(exist_ok=True)
    path = root / "publication.json"
    path.write_text(
        json.dumps(
            {
                "remote_url": str(remote),
                "branch": "main",
                "public_url": "http://127.0.0.1:8000/index.html",
                "work_dir": str(root / "publisher-work"),
                "ssh_key": None,
                "publication_authorized": authorized,
                "source_kinds": ["official"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _successful_collection(connection):
    run_id = store.start_collection_run(connection, "2026-09-24")
    store.finish_collection_run(
        connection, run_id, status="success", observation_key="official:2026-09-24"
    )
    return {
        "observed_date": "2026-09-24",
        "value_text": "57.000",
        "observation_key": "official:2026-09-24",
    }


def _failed_collection(connection):
    run_id = store.start_collection_run(connection, "2026-09-24")
    error = RuntimeError("source unavailable")
    store.finish_collection_run(connection, run_id, status="error", error_text=str(error))
    raise error


class PublicationCliTests(unittest.TestCase):
    def test_configure_creates_disabled_real_config_without_remote_contact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "publication.json"
            with mock.patch.object(publisher, "publish") as publish:
                code = tracker.main(
                    [
                        "--db",
                        str(root / "prices.sqlite3"),
                        "configure-publish",
                        "--repository",
                        "owner/site",
                        "--output",
                        str(output),
                    ]
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertFalse(payload["publication_authorized"])
        self.assertIsNone(payload["ssh_key"])
        self.assertEqual(payload["remote_url"], "git@github.com:owner/site.git")
        self.assertEqual(payload["public_url"], "https://owner.github.io/site/")
        publish.assert_not_called()

    def test_public_chart_uses_public_default_and_source_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            public_output = root / "public" / "index.html"
            connection = store.connect(database)
            try:
                for kind, marker in (("official", "OFFICIAL"), ("community", "COMMUNITY")):
                    store.save_observation(
                        connection,
                        {
                            "observation_key": kind + ":2026-09-24",
                            "product": store.PRODUCT,
                            "source_kind": kind,
                            "source_url": "https://example.test/" + marker,
                            "date_text": "2026-09-24",
                            "observed_date": "2026-09-24",
                            "date_precision": "day",
                            "source_updated_at": "2026-09-24T14:40:00+08:00",
                            "fetched_at": "2026-09-24T07:00:00Z",
                            "value_text": "57.000",
                            "value_qualifier": "reported",
                            "session": "middle" if kind == "official" else None,
                            "evidence_note": marker,
                        },
                    )
            finally:
                connection.close()
            with mock.patch.object(tracker, "DEFAULT_PUBLIC_CHART", public_output):
                code = tracker.main(
                    ["--db", str(database), "chart", "--public", "--source-kinds", "official"]
                )
            document = public_output.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn("OFFICIAL", document)
        self.assertNotIn("COMMUNITY", document)

    def test_failed_collection_still_renders_private_and_blocks_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            config = _config(root)
            private_output = root / "private.html"
            public_output = root / "public.html"

            def fail(connection, expected_date):
                return _failed_collection(connection)

            with mock.patch.object(collect, "taipei_today", return_value="2026-09-24"), mock.patch.object(
                collect, "collect_once", side_effect=fail
            ), mock.patch("chart.render", return_value=private_output) as render, mock.patch(
                "chart.render_public"
            ) as render_public, mock.patch.object(publisher, "publish") as publish, mock.patch.object(
                tracker, "DEFAULT_PUBLIC_CHART", public_output
            ):
                code = tracker.main(
                    [
                        "--db",
                        str(database),
                        "run",
                        "--output",
                        str(private_output),
                        "--publish-config",
                        str(config),
                    ]
                )

            connection = store.connect(database)
            try:
                publication = store.publication_runs(connection, 1)[0]
            finally:
                connection.close()

        self.assertEqual(code, 1)
        render.assert_called_once()
        render_public.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(publication["status"], "blocked")
        self.assertIsNotNone(publication["collection_run_id"])

    def test_successful_run_records_pending_and_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            config = _config(root)
            private_output = root / "private.html"
            public_output = root / "public.html"

            def collect_once(connection, expected_date):
                return _successful_collection(connection)

            def render_public(connection, path, *, source_kinds):
                Path(path).write_text("public", encoding="utf-8")
                return Path(path)

            pending = {
                "status": "pending",
                "data_digest": _DIGEST,
                "commit_sha": _COMMIT,
                "public_url": "http://127.0.0.1:8000/index.html",
                "error_text": "not live yet",
            }
            with mock.patch.object(collect, "taipei_today", return_value="2026-09-24"), mock.patch.object(
                collect, "collect_once", side_effect=collect_once
            ), mock.patch("chart.render", return_value=private_output), mock.patch(
                "chart.render_public", side_effect=render_public
            ), mock.patch.object(publisher, "publish", return_value=pending) as publish, mock.patch.object(
                tracker, "DEFAULT_PUBLIC_CHART", public_output
            ):
                code = tracker.main(
                    [
                        "--db",
                        str(database),
                        "run",
                        "--output",
                        str(private_output),
                        "--publish-config",
                        str(config),
                    ]
                )

            connection = store.connect(database)
            try:
                publication = store.publication_runs(connection, 1)[0]
            finally:
                connection.close()

        self.assertEqual(code, 2)
        publish.assert_called_once()
        self.assertEqual(publication["status"], "pending")
        self.assertEqual(publication["data_digest"], _DIGEST)
        self.assertIsNotNone(publication["collection_run_id"])

    def test_public_render_failure_never_calls_publisher_and_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            config = _config(root)
            private_output = root / "private.html"
            public_output = root / "public.html"

            def collect_once(connection, expected_date):
                return _successful_collection(connection)

            with mock.patch.object(collect, "taipei_today", return_value="2026-09-24"), mock.patch.object(
                collect, "collect_once", side_effect=collect_once
            ), mock.patch("chart.render", return_value=private_output), mock.patch(
                "chart.render_public", side_effect=RuntimeError("render failed")
            ), mock.patch.object(publisher, "publish") as publish, mock.patch.object(
                tracker, "DEFAULT_PUBLIC_CHART", public_output
            ):
                code = tracker.main(
                    [
                        "--db",
                        str(database),
                        "run",
                        "--output",
                        str(private_output),
                        "--publish-config",
                        str(config),
                    ]
                )
            connection = store.connect(database)
            try:
                publication = store.publication_runs(connection, 1)[0]
            finally:
                connection.close()

        self.assertEqual(code, 1)
        publish.assert_not_called()
        self.assertEqual(publication["status"], "error")
        self.assertIn("render failed", publication["error_text"])

    def test_operation_lock_skips_duplicate_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            lock_path = database.with_name(database.name + ".operation.lock")
            lock_path.touch()
            with lock_path.open("r+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(collect, "collect_once") as collect_once:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = tracker.main(["--db", str(database), "collect"])

        self.assertEqual(code, 0)
        self.assertIn("건너뜁니다", output.getvalue())
        collect_once.assert_not_called()

    def test_manual_rollback_records_pending_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            config = _config(root)
            result = {
                "status": "pending",
                "data_digest": _DIGEST,
                "commit_sha": _COMMIT,
                "public_url": "http://127.0.0.1:8000/index.html",
                "error_text": "not live yet",
            }
            with mock.patch.object(publisher, "rollback", return_value=result):
                code = tracker.main(
                    [
                        "--db",
                        str(database),
                        "rollback",
                        "--config",
                        str(config),
                        "--commit",
                        "c" * 40,
                    ]
                )
            connection = store.connect(database)
            try:
                publication = store.publication_runs(connection, 1)[0]
            finally:
                connection.close()

        self.assertEqual(code, 2)
        self.assertEqual(publication["status"], "pending")
        self.assertIsNone(publication["collection_run_id"])
        self.assertEqual(publication["commit_sha"], _COMMIT)

    def test_unauthorized_manual_publish_is_blocked_before_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "prices.sqlite3"
            config = _config(root, authorized=False)
            with mock.patch.object(publisher, "publish") as publish:
                code = tracker.main(
                    ["--db", str(database), "publish", "--config", str(config)]
                )
            connection = store.connect(database)
            try:
                publication = store.publication_runs(connection, 1)[0]
            finally:
                connection.close()

        self.assertEqual(code, 1)
        publish.assert_not_called()
        self.assertEqual(publication["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
