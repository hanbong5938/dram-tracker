"""Regression tests for safe public Git publication."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import sys
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

import publisher


DIGEST_ONE = "1" * 64
DIGEST_TWO = "2" * 64


def _page(digest: str, generated: str) -> bytes:
    return (
        "<!doctype html><html><head>"
        f'<meta content="{digest}" name="dram-data-digest">'
        f'<meta name="generated-at" content="{generated}">'
        "</head><body>public</body></html>\n"
    ).encode("utf-8")


def _git(path: Path, *args: str, input_bytes: Optional[bytes] = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(path), *args], input=input_bytes,
        capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout


class _PageHandler(BaseHTTPRequestHandler):
    page = b""

    def do_GET(self) -> None:
        body = type(self).page
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _PageServer:
    def __enter__(self) -> "_PageServer":
        _PageHandler.page = b""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/index.html"

    def serve(self, page: bytes) -> None:
        _PageHandler.page = page


class PublisherTests(unittest.TestCase):
    def _setup(self, root: Path, url: str) -> tuple[Path, Dict[str, object]]:
        remote = root / "public.git"
        remote.mkdir()
        _git(remote, "init", "--bare", "--quiet")
        config: Dict[str, object] = {
            "remote_url": str(remote),
            "branch": "main",
            "public_url": url,
            "work_dir": root / "private" / "publisher-work",
            "publication_authorized": True,
            "source_kinds": ["official", "community"],
        }
        return remote, config

    def _candidate(self, root: Path, name: str, page: bytes) -> Path:
        path = root / name
        path.write_bytes(page)
        return path

    def _remote_head(self, remote: Path) -> str:
        return _git(remote, "rev-parse", "refs/heads/main").decode().strip()

    def _remote_page(self, remote: Path, revision: str = "refs/heads/main") -> bytes:
        return _git(remote, "show", f"{revision}:index.html")

    def test_first_push_pending_then_same_digest_confirms_without_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _PageServer() as server:
            root = Path(directory)
            remote, config = self._setup(root, server.url)
            first_page = _page(DIGEST_ONE, "2026-09-24T01:00:00Z")
            first = publisher.publish(self._candidate(root, "first.html", first_page), config)

            self.assertEqual(first["status"], "pending")
            self.assertEqual(first["data_digest"], DIGEST_ONE)
            self.assertEqual(first["commit_sha"], self._remote_head(remote))
            self.assertIn("error_text", first)

            server.serve(first_page)
            regenerated = _page(DIGEST_ONE, "2026-09-24T02:00:00Z")
            second = publisher.publish(self._candidate(root, "second.html", regenerated), config)

            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(second["commit_sha"], first["commit_sha"])
            self.assertEqual(self._remote_page(remote), first_page)
            self.assertNotIn("error_text", second)

    def test_live_mismatch_is_never_reported_as_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _PageServer() as server:
            root = Path(directory)
            remote, config = self._setup(root, server.url)
            server.serve(b"stale")

            result = publisher.publish(
                self._candidate(root, "candidate.html", _page(DIGEST_ONE, "one")), config
            )

            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["commit_sha"], self._remote_head(remote))

    def test_remote_extra_file_prevents_push_and_retains_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _PageServer() as server:
            root = Path(directory)
            remote, config = self._setup(root, server.url)
            seed = root / "seed"
            seed.mkdir()
            _git(seed, "init", "--quiet")
            _git(seed, "config", "user.name", "Test")
            _git(seed, "config", "user.email", "test@localhost")
            (seed / "index.html").write_bytes(_page(DIGEST_ONE, "one"))
            (seed / ".nojekyll").write_bytes(b"")
            (seed / "secret.txt").write_text("private", encoding="utf-8")
            _git(seed, "add", "--", "index.html", ".nojekyll", "secret.txt")
            _git(seed, "commit", "--quiet", "-m", "unsafe tree")
            _git(seed, "push", "--quiet", str(remote), "HEAD:refs/heads/main")
            original = self._remote_head(remote)

            with self.assertRaises(publisher.PublishError):
                publisher.publish(
                    self._candidate(root, "candidate.html", _page(DIGEST_TWO, "two")), config
                )

            self.assertEqual(self._remote_head(remote), original)
            self.assertEqual(_git(remote, "show", f"{original}:secret.txt"), b"private")

    def test_rollback_creates_new_commit_with_ancestor_values_and_verifies_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _PageServer() as server:
            root = Path(directory)
            remote, config = self._setup(root, server.url)
            page_one = _page(DIGEST_ONE, "one")
            page_two = _page(DIGEST_TWO, "two")
            first = publisher.publish(self._candidate(root, "one.html", page_one), config)
            second = publisher.publish(self._candidate(root, "two.html", page_two), config)
            server.serve(page_one)

            result = publisher.rollback(config, first["commit_sha"])

            self.assertEqual(result["status"], "published")
            self.assertEqual(result["data_digest"], DIGEST_ONE)
            self.assertNotEqual(result["commit_sha"], first["commit_sha"])
            self.assertNotEqual(result["commit_sha"], second["commit_sha"])
            self.assertEqual(self._remote_page(remote), page_one)
            parents = _git(remote, "show", "-s", "--format=%P", result["commit_sha"]).decode().split()
            self.assertEqual(parents, [second["commit_sha"]])

    def test_config_resolution_authorization_and_hostile_revision_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "public.git"
            remote.mkdir()
            _git(remote, "init", "--bare", "--quiet")
            config_path = root / "publication.json"
            config_path.write_text(
                json.dumps({
                    "remote_url": str(remote),
                    "public_url": "http://127.0.0.1:8000/index.html",
                    "work_dir": "data/publisher-work",
                    "publication_authorized": False,
                    "source_kinds": ["official"],
                }),
                encoding="utf-8",
            )
            config = publisher.load_config(config_path)

            self.assertEqual(config["work_dir"], (root / "data" / "publisher-work").resolve())
            self.assertEqual(config["branch"], "main")
            with self.assertRaises(publisher.PublishError):
                publisher.publish(
                    self._candidate(root, "candidate.html", _page(DIGEST_ONE, "one")),
                    config,
                )
            authorized = dict(config, publication_authorized=True)
            with self.assertRaisesRegex(publisher.PublishError, "40-character"):
                publisher.rollback(authorized, "--help")

    def test_candidate_and_remote_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _PageServer() as server:
            root = Path(directory)
            remote, config = self._setup(root, server.url)
            real = self._candidate(root, "real.html", _page(DIGEST_ONE, "one"))
            linked = root / "linked.html"
            linked.symlink_to(real)
            with self.assertRaises(publisher.PublishError):
                publisher.publish(linked, config)

            remote_link = root / "remote-link.git"
            remote_link.symlink_to(remote, target_is_directory=True)
            hostile = dict(config, remote_url=str(remote_link))
            with self.assertRaises(publisher.PublishError):
                publisher.publish(real, hostile)

    def test_workspace_lock_releases_after_crash_and_refuses_live_contention(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _PageServer() as server:
            root = Path(directory)
            _remote, config = self._setup(root, server.url)
            candidate = self._candidate(root, "candidate.html", _page(DIGEST_ONE, "one"))
            child = root / "child.py"
            child.write_text(
                """import json
import os
import pathlib
import publisher
import sys
import time

config = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
mode = sys.argv[3]
if mode == "crash":
    publisher._live_matches = lambda url, expected: os._exit(77)
else:
    def hold(url, expected):
        pathlib.Path(sys.argv[4]).write_text("ready", encoding="utf-8")
        time.sleep(30)
        return False
    publisher._live_matches = hold
publisher.publish(pathlib.Path(sys.argv[1]), config)
""",
                encoding="utf-8",
            )
            config_path = root / "child-config.json"
            config_path.write_text(json.dumps(config, default=str), encoding="utf-8")
            environment = dict(os.environ)
            module_dir = str(Path(publisher.__file__).resolve().parent)
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (module_dir, environment.get("PYTHONPATH", "")) if part
            )

            crashed = subprocess.run(
                [sys.executable, str(child), str(candidate), str(config_path), "crash"],
                capture_output=True,
                env=environment,
                timeout=15,
                check=False,
            )
            self.assertEqual(crashed.returncode, 77, crashed.stderr.decode("utf-8", "replace"))
            restarted = publisher.publish(candidate, config)
            self.assertEqual(restarted["status"], "pending")

            ready = root / "holder-ready"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(child),
                    str(candidate),
                    str(config_path),
                    "hold",
                    str(ready),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "lock-holding child did not reach live verification")
                with self.assertRaisesRegex(
                    publisher.PublishError, "already running"
                ):
                    publisher.publish(candidate, config)
            finally:
                holder.terminate()
                _stdout, stderr = holder.communicate(timeout=5)
                if holder.returncode is None:
                    holder.kill()
                    holder.wait(timeout=5)
                self.assertIn(holder.returncode, {-15, 0}, stderr.decode("utf-8", "replace"))

            after_contention = publisher.publish(candidate, config)
            self.assertEqual(after_contention["status"], "pending")


if __name__ == "__main__":
    unittest.main()
