"""Install the DRAM tracker as a macOS LaunchAgent.

The calendar entry is interpreted in macOS's system local timezone.  This
module therefore refuses to install unless /etc/localtime identifies
Asia/Seoul; setting a ``TZ`` environment variable does not change launchd's
calendar timezone.

A LaunchAgent cannot collect while the Mac is powered off or asleep at the
scheduled minute.  launchd may run a missed job after wake in some cases, but
it cannot recreate observations for the time the Mac was unavailable.
"""

from __future__ import annotations

import os
import pathlib
import plistlib
import platform
import stat
import subprocess
import tempfile
from typing import Any, Sequence


LABEL = "com.local.dramtracker"
_START_HOUR = 15
_START_MINUTE = 45
_AGENT_RELATIVE_PATH = pathlib.Path("Library") / "LaunchAgents" / f"{LABEL}.plist"
_LAUNCHCTL = "/bin/launchctl"
_READLINK = "/usr/bin/readlink"
_UNATTENDED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class ScheduleError(RuntimeError):
    """An actionable error while inspecting or changing the LaunchAgent."""


_SOURCE_TERMS_MESSAGE = (
    "Scheduling requires explicit source-terms acknowledgement. Re-run with "
    "--accept-source-terms after reviewing the source site's terms. This "
    "acknowledgement does not grant permission or rights to copy or automate "
    "access."
)


def _path(value: os.PathLike[str] | str, name: str) -> pathlib.Path:
    """Return a normalized absolute path without changing the filesystem."""

    try:
        result = pathlib.Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"{name} path is invalid: {value!r}") from exc
    if not result.is_absolute():
        result = pathlib.Path.cwd() / result
    # strict=False keeps plist generation pure for paths that will be created
    # by install, while still making every serialized path absolute.
    return result.resolve(strict=False)


def _agent_path() -> pathlib.Path:
    return pathlib.Path.home() / _AGENT_RELATIVE_PATH


def _is_darwin() -> bool:
    return platform.system() == "Darwin"


def _timezone_name() -> tuple[str | None, str | None]:
    """Read the system timezone link, returning (detected_name, detail)."""

    try:
        completed = subprocess.run(
            [_READLINK, "/etc/localtime"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return None, f"could not run readlink: {exc}"

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    candidates = [stdout]
    # On systems where /etc/localtime is not a symlink, resolve() can still
    # provide a canonical target.  It is only accepted when it names the
    # Asia/Seoul zone, never merely because the process TZ happens to match.
    try:
        candidates.append(str(pathlib.Path("/etc/localtime").resolve(strict=True)))
    except OSError:
        pass

    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        marker = "/zoneinfo/"
        if marker in normalized:
            zone = normalized.rsplit(marker, 1)[1]
            if zone == "Asia/Seoul":
                return zone, None
        if normalized == "Asia/Seoul" or normalized.endswith("/Asia/Seoul"):
            return "Asia/Seoul", None

    detail = stderr or stdout or f"readlink exited with status {completed.returncode}"
    return None, detail


def _require_timezone() -> None:
    detected, detail = _timezone_name()
    if detected == "Asia/Seoul":
        return
    suffix = f" Detected: {detail}." if detail else ""
    raise ScheduleError(
        "The Mac system timezone must be Asia/Seoul before installing this "
        "LaunchAgent. Set it in System Settings > General > Date & Time and "
        f"retry.{suffix} launchd follows the system local timezone; a TZ "
        "environment variable does not control StartCalendarInterval."
    )


def _require_python(python_path: pathlib.Path) -> None:
    if not python_path.is_file():
        raise ScheduleError(
            f"Python executable does not exist: {python_path}. "
            "Pass the absolute path to the Python interpreter used by tracker.py."
        )
    if not os.access(python_path, os.X_OK):
        raise ScheduleError(f"Python path is not executable: {python_path}")


def _require_tracker(tracker_path: pathlib.Path) -> None:
    if not tracker_path.is_file():
        raise ScheduleError(f"Tracker script does not exist: {tracker_path}")


def _program_arguments(
    python_path: pathlib.Path,
    tracker_path: pathlib.Path,
    db_path: pathlib.Path,
    output_path: pathlib.Path,
    publish_config: pathlib.Path | None,
) -> list[str]:
    arguments = [
        str(python_path),
        str(tracker_path),
        "--db",
        str(db_path),
        "run",
        "--output",
        str(output_path),
    ]
    if publish_config is not None:
        arguments.extend(["--publish-config", str(publish_config)])
    return arguments


def make_plist(
    python_path: os.PathLike[str] | str,
    tracker_path: os.PathLike[str] | str,
    db_path: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    *,
    publish_config: os.PathLike[str] | str | None = None,
) -> bytes:
    """Return LaunchAgent XML for the daily 15:45 Asia/Seoul collection.

    The function has no filesystem or process side effects.  It normalizes all
    paths to absolute strings and preserves spaces as individual
    ``ProgramArguments`` entries.  :func:`install` validates executable and
    publication configuration paths before writing anything.
    """

    python_abs = _path(python_path, "Python executable")
    tracker_abs = _path(tracker_path, "Tracker script")
    db_abs = _path(db_path, "Database")
    output_abs = _path(output_path, "Output")
    publish_config_abs = (
        _path(publish_config, "Publication config")
        if publish_config is not None
        else None
    )

    payload: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": _program_arguments(
            python_abs, tracker_abs, db_abs, output_abs, publish_config_abs
        ),
        "EnvironmentVariables": {
            "HOME": str(pathlib.Path.home()),
            "PATH": _UNATTENDED_PATH,
        },
        "StartCalendarInterval": {"Hour": _START_HOUR, "Minute": _START_MINUTE},
        "StandardOutPath": str(db_abs.parent / "dramtracker.stdout.log"),
        "StandardErrorPath": str(db_abs.parent / "dramtracker.stderr.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _diagnostic(result: subprocess.CompletedProcess[str], action: str) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"exit status {result.returncode}"
    return f"launchctl {action} failed: {detail}"


def _launchctl(args: Sequence[str], action: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [_LAUNCHCTL, *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ScheduleError(
            "launchctl was not found at /bin/launchctl; no LaunchAgent was changed."
        ) from exc
    except OSError as exc:
        raise ScheduleError(f"Could not run launchctl {action}: {exc}") from exc
    if result.returncode != 0:
        raise ScheduleError(_diagnostic(result, action))
    return result


def _bootout_not_loaded(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(
        phrase in text
        for phrase in (
            "no such process",
            "could not find service",
            "service is not loaded",
            "service not found",
            "does not exist",
            "not found",
        )
    )


def _bootout_owned(agent_path: pathlib.Path, *, tolerate_not_loaded: bool) -> None:
    target = f"gui/{os.getuid()}"
    try:
        result = subprocess.run(
            [_LAUNCHCTL, "bootout", target, str(agent_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ScheduleError(
            "launchctl was not found at /bin/launchctl; no LaunchAgent was changed."
        ) from exc
    except OSError as exc:
        raise ScheduleError(f"Could not run launchctl bootout: {exc}") from exc

    if result.returncode == 0:
        return
    if tolerate_not_loaded and _bootout_not_loaded(result):
        return
    raise ScheduleError(_diagnostic(result, "bootout"))


def _existing_owned_bytes(agent_path: pathlib.Path) -> bytes | None:
    if not agent_path.exists() and not agent_path.is_symlink():
        return None
    if agent_path.is_symlink():
        raise ScheduleError(
            f"Refusing to follow symlink at {agent_path}; remove it manually after inspection."
        )
    if not agent_path.is_file():
        raise ScheduleError(f"Refusing to replace non-file LaunchAgent path: {agent_path}")
    try:
        previous = agent_path.read_bytes()
        parsed = plistlib.loads(previous)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ScheduleError(
            f"Existing LaunchAgent is not a readable plist: {agent_path}; it was left unchanged."
        ) from exc
    if parsed.get("Label") != LABEL:
        raise ScheduleError(
            f"Existing plist at {agent_path} is not owned by {LABEL}; it was left unchanged."
        )
    return previous


def _write_atomically(agent_path: pathlib.Path, payload: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{LABEL}.", suffix=".tmp", dir=str(agent_path.parent)
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, agent_path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ScheduleError(f"Could not write LaunchAgent {agent_path}: {exc}") from exc


def _preflight_publication_config(
    publish_config: pathlib.Path,
) -> None:
    """Validate unattended publication without performing a remote operation."""

    try:
        import publisher
    except ImportError as exc:
        raise ScheduleError(f"Publication support is unavailable: {exc}") from exc

    try:
        config = publisher.load_config(publish_config)
    except publisher.PublishError as exc:
        raise ScheduleError(f"Publication configuration is invalid: {exc}") from exc

    if not config["publication_authorized"]:
        raise ScheduleError(
            "Publication scheduling is blocked because publication_authorized "
            "is false; the existing LaunchAgent was left unchanged."
        )

    remote_url = config["remote_url"]
    if not remote_url.startswith("git@github.com:"):
        return

    key_path = config["ssh_key"]
    if key_path is None:
        raise ScheduleError(
            "GitHub publication scheduling requires a dedicated ssh_key; "
            "the existing LaunchAgent was left unchanged."
        )
    try:
        mode = stat.S_IMODE(key_path.stat().st_mode)
    except OSError as exc:
        raise ScheduleError(
            f"Could not inspect dedicated GitHub SSH key {key_path}: {exc}; "
            "the existing LaunchAgent was left unchanged."
        ) from exc
    if mode & 0o077:
        raise ScheduleError(
            f"Dedicated GitHub SSH key {key_path} has unsafe permissions "
            f"{mode:04o}; remove all group/other permissions before scheduling. "
            "The existing LaunchAgent was left unchanged."
        )


def install(
    python_path: os.PathLike[str] | str,
    tracker_path: os.PathLike[str] | str,
    db_path: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    accept_source_terms: bool = False,
    *,
    publish_config: os.PathLike[str] | str | None = None,
) -> pathlib.Path:
    """Install and bootstrap the daily LaunchAgent, without starting it now.

    The Mac must be configured to Asia/Seoul because launchd uses the system
    local timezone, not ``TZ``.  A sleeping or powered-off Mac can miss the
    scheduled minute, so this schedule is not a substitute for continuous
    availability or historical backfilling.
    """

    if not _is_darwin():
        raise ScheduleError(
            "schedule install is supported only on macOS (Darwin); no LaunchAgent was changed."
        )
    if not accept_source_terms:
        raise ScheduleError(_SOURCE_TERMS_MESSAGE)

    python_abs = _path(python_path, "Python executable")
    tracker_abs = _path(tracker_path, "Tracker script")
    db_abs = _path(db_path, "Database")
    output_abs = _path(output_path, "Output")
    publish_config_abs = (
        _path(publish_config, "Publication config")
        if publish_config is not None
        else None
    )

    # Complete every validation before unloading a service, replacing a plist,
    # or creating installation directories.
    _require_timezone()
    _require_python(python_abs)
    _require_tracker(tracker_abs)
    if publish_config_abs is not None:
        _preflight_publication_config(publish_config_abs)

    agent_path = _agent_path()
    payload = make_plist(
        python_abs,
        tracker_abs,
        db_abs,
        output_abs,
        publish_config=publish_config_abs,
    )
    previous = _existing_owned_bytes(agent_path)

    try:
        db_abs.parent.mkdir(parents=True, exist_ok=True)
        output_abs.parent.mkdir(parents=True, exist_ok=True)
        agent_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScheduleError(f"Could not create scheduler directories: {exc}") from exc

    # Booting out only the fixed, label-owned plist makes repeated installs
    # idempotent while never touching an unrelated service.
    if previous is not None:
        _bootout_owned(agent_path, tolerate_not_loaded=True)

    try:
        _write_atomically(agent_path, payload)
        _launchctl(
            ["bootstrap", f"gui/{os.getuid()}", str(agent_path)],
            "bootstrap",
        )
    except ScheduleError:
        # Preserve an existing owned configuration if bootstrap of the new
        # configuration fails.  Never remove or restore an unrelated path.
        try:
            if previous is None:
                if agent_path.is_file() and not agent_path.is_symlink():
                    agent_path.unlink()
            else:
                _write_atomically(agent_path, previous)
        except (OSError, ScheduleError):
            pass
        raise

    return agent_path


def uninstall() -> None:
    """Boot out and remove only this module's own LaunchAgent plist.

    If the plist is absent, there is nothing owned to unload and the function
    returns without asking launchctl to touch a service by label.  A Mac that
    sleeps or is powered off can miss scheduled collection; uninstalling does
    not recover those observations.
    """

    if not _is_darwin():
        raise ScheduleError(
            "schedule uninstall is supported only on macOS (Darwin); no files or services were changed."
        )

    agent_path = _agent_path()
    previous = _existing_owned_bytes(agent_path)
    if previous is None:
        return

    _bootout_owned(agent_path, tolerate_not_loaded=True)
    try:
        agent_path.unlink()
    except OSError as exc:
        raise ScheduleError(f"Could not remove owned LaunchAgent {agent_path}: {exc}") from exc
