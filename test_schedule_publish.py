"""Regression tests for safe publication-aware LaunchAgent scheduling."""

from __future__ import annotations

import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import schedule


class SchedulePublicationTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        python = root / "Python Bin" / "python3"
        tracker = root / "Tracker App" / "tracker.py"
        database = root / "Private Data" / "dram.sqlite3"
        output = root / "Public Pages" / "index.html"
        agent = root / "Home Folder" / "Library" / "LaunchAgents" / f"{schedule.LABEL}.plist"
        python.parent.mkdir(parents=True)
        tracker.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o700)
        tracker.write_text("# tracker\n", encoding="utf-8")
        return python, tracker, database, output, agent

    def _config(
        self,
        root: Path,
        *,
        authorized: bool = True,
        github: bool = False,
        ssh_key: Path | None = None,
    ) -> Path:
        config = root / "Config Folder" / "publication.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        remote = "git@github.com:owner/repository.git" if github else str(root / "public.git")
        payload: dict[str, object] = {
            "remote_url": remote,
            "branch": "main",
            "public_url": "https://owner.github.io/repository/",
            "work_dir": str(root / "Publisher Work"),
            "publication_authorized": authorized,
            "source_kinds": ["official", "community"],
        }
        if ssh_key is not None:
            payload["ssh_key"] = str(ssh_key)
        config.write_text(json.dumps(payload), encoding="utf-8")
        return config

    def _install_patches(self, root: Path, agent: Path):
        return (
            mock.patch.object(schedule, "_is_darwin", return_value=True),
            mock.patch.object(schedule, "_require_timezone"),
            mock.patch.object(schedule, "_agent_path", return_value=agent),
            mock.patch.object(schedule.pathlib.Path, "home", return_value=root / "Home Folder"),
        )

    def test_make_plist_keeps_html_and_plist_paths_distinct_with_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, agent = self._paths(root)
            config = root / "Config Folder" / "publication config.json"

            with mock.patch.object(
                schedule.pathlib.Path, "home", return_value=root / "Home Folder"
            ):
                manifest = plistlib.loads(
                    schedule.make_plist(
                        python,
                        tracker,
                        database,
                        output,
                        publish_config=config,
                    )
                )

            argv = manifest["ProgramArguments"]
            self.assertEqual(argv[:5], [str(python), str(tracker), "--db", str(database), "run"])
            self.assertEqual(argv[5:7], ["--output", str(output)])
            self.assertEqual(argv[7:9], ["--publish-config", str(config)])
            self.assertEqual(Path(argv[6]).suffix, ".html")
            self.assertNotEqual(Path(argv[6]), agent)
            self.assertEqual(manifest["StartCalendarInterval"], {"Hour": 15, "Minute": 45})
            self.assertEqual(
                manifest["EnvironmentVariables"],
                {
                    "HOME": str(root / "Home Folder"),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
            serialized = plistlib.dumps(manifest)
            self.assertNotIn(b"PRIVATE KEY", serialized)
            self.assertNotIn(b"credentials", serialized)

    def test_make_plist_without_publication_keeps_collection_only_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, _ = self._paths(root)
            with mock.patch.object(
                schedule.pathlib.Path, "home", return_value=root / "Home Folder"
            ):
                manifest = plistlib.loads(
                    schedule.make_plist(python, tracker, database, output)
                )

            self.assertEqual(
                manifest["ProgramArguments"],
                [
                    str(python),
                    str(tracker),
                    "--db",
                    str(database),
                    "run",
                    "--output",
                    str(output),
                ],
            )

    def test_unauthorized_config_leaves_existing_service_and_plist_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, agent = self._paths(root)
            config = self._config(root, authorized=False)
            agent.parent.mkdir(parents=True)
            original = plistlib.dumps({"Label": schedule.LABEL, "Sentinel": "old"})
            agent.write_bytes(original)
            patches = self._install_patches(root, agent)

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                schedule, "_bootout_owned"
            ) as bootout, mock.patch.object(schedule, "_write_atomically") as write, mock.patch.object(
                schedule, "_launchctl"
            ) as launchctl:
                with self.assertRaisesRegex(schedule.ScheduleError, "publication_authorized"):
                    schedule.install(
                        python,
                        tracker,
                        database,
                        output,
                        True,
                        publish_config=config,
                    )

            self.assertEqual(agent.read_bytes(), original)
            bootout.assert_not_called()
            write.assert_not_called()
            launchctl.assert_not_called()

    def test_github_config_without_dedicated_key_is_rejected_before_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, agent = self._paths(root)
            config = self._config(root, github=True)
            patches = self._install_patches(root, agent)

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                schedule, "_write_atomically"
            ) as write, mock.patch.object(schedule, "_launchctl") as launchctl:
                with self.assertRaisesRegex(schedule.ScheduleError, "dedicated ssh_key"):
                    schedule.install(
                        python,
                        tracker,
                        database,
                        output,
                        True,
                        publish_config=config,
                    )

            self.assertFalse(database.parent.exists())
            self.assertFalse(output.parent.exists())
            write.assert_not_called()
            launchctl.assert_not_called()

    def test_broad_access_github_key_is_rejected_before_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, agent = self._paths(root)
            key = root / "Keys" / "publisher"
            key.parent.mkdir()
            key.write_text("test key material", encoding="utf-8")
            key.chmod(0o640)
            config = self._config(root, github=True, ssh_key=key)
            patches = self._install_patches(root, agent)

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                schedule, "_bootout_owned"
            ) as bootout, mock.patch.object(schedule, "_write_atomically") as write, mock.patch.object(
                schedule, "_launchctl"
            ) as launchctl:
                with self.assertRaisesRegex(schedule.ScheduleError, "unsafe permissions"):
                    schedule.install(
                        python,
                        tracker,
                        database,
                        output,
                        True,
                        publish_config=config,
                    )

            bootout.assert_not_called()
            write.assert_not_called()
            launchctl.assert_not_called()

    def test_install_executes_only_after_secure_github_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, agent = self._paths(root)
            key = root / "Keys" / "publisher"
            key.parent.mkdir()
            key.write_text("test key material", encoding="utf-8")
            key.chmod(0o600)
            config = self._config(root, github=True, ssh_key=key)
            patches = self._install_patches(root, agent)
            written: list[tuple[Path, bytes]] = []

            def capture_write(path: Path, payload: bytes) -> None:
                written.append((path, payload))

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                schedule, "_write_atomically", side_effect=capture_write
            ), mock.patch.object(schedule, "_launchctl") as launchctl:
                result = schedule.install(
                    python,
                    tracker,
                    database,
                    output,
                    True,
                    publish_config=config,
                )

            self.assertEqual(result, agent)
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0][0], agent)
            manifest = plistlib.loads(written[0][1])
            self.assertEqual(
                manifest["ProgramArguments"][-2:],
                ["--publish-config", str(config)],
            )
            launchctl.assert_called_once_with(
                ["bootstrap", f"gui/{os.getuid()}", str(agent)], "bootstrap"
            )

    def test_local_only_schedule_does_not_require_ssh_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            python, tracker, database, output, agent = self._paths(root)
            config = self._config(root)
            patches = self._install_patches(root, agent)

            with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                schedule, "_write_atomically"
            ) as write, mock.patch.object(schedule, "_launchctl"):
                schedule.install(
                    python,
                    tracker,
                    database,
                    output,
                    True,
                    publish_config=config,
                )

            write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
