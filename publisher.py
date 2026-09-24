"""Publish the public DRAM chart to a deliberately small Git repository."""

from __future__ import annotations

import contextlib
import fcntl
import errno
import html.parser
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


_ALLOWED_FILES = (".nojekyll", "index.html")
_BRANCH_RE = re.compile(r"^(?![./])(?!.*(?:\.\.|//|@\{|\\|[ ~^:?*\[]))(?!.*[./]$)[A-Za-z0-9._/-]+$")
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_MARKER = ".dram-publisher-workdir"
_MARKER_CONTENT = "DRAM publisher disposable work directory\n"
_LOCK = ".dram-publisher.lock"
_GIT_TIMEOUT = 30.0
_HTTP_TIMEOUT = 3.0
_HTTP_ATTEMPTS = 3


class PublishError(RuntimeError):
    """A safe, actionable failure while preparing or publishing a page."""


def _safe_error(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:1000] or "operation failed"


def _absolute_path(value: object, base: Path, field: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise PublishError(f"{field} must be a non-empty path")
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("empty or invalid path")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        return path.resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise PublishError(f"{field} must be a non-empty valid path") from exc


def _public_url(value: object) -> str:
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise PublishError("public_url must be a URL without control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise PublishError("public_url is invalid") from exc
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PublishError("public_url must not contain credentials, a query, or a fragment")
    host = (parsed.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise PublishError("public_url must use HTTPS (HTTP is allowed only on loopback)")
    if not host or port is not None and not (1 <= port <= 65535):
        raise PublishError("public_url has an invalid host or port")
    return value


def _remote_url(value: object, base: Path) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 for character in value)
    ):
        raise PublishError("remote_url is invalid")
    if _GITHUB_SSH_RE.fullmatch(value):
        owner, repository = _GITHUB_SSH_RE.fullmatch(value).groups()
        if owner in {".", ".."} or repository in {".", ".."}:
            raise PublishError("remote_url is invalid")
        return value
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError) as exc:
        raise PublishError("remote_url is invalid") from exc
    if path.is_symlink():
        raise PublishError("local remote_url must not be a symlink")
    if not path.is_absolute():
        raise PublishError("remote_url must be a GitHub SSH URL or an absolute local path")
    resolved = path.resolve(strict=False)
    if resolved == Path("/") or resolved == base:
        raise PublishError("remote_url does not identify a safe bare repository")
    return str(resolved)


def _validate_config(raw: Mapping[str, Any], base: Optional[Path] = None) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PublishError("publication config must be a JSON object")
    if any(not isinstance(key, str) for key in raw):
        raise PublishError("publication config field names must be strings")
    base = (base or Path.cwd()).resolve()
    allowed = {
        "remote_url", "branch", "public_url", "work_dir", "ssh_key",
        "publication_authorized", "source_kinds",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise PublishError(f"unknown publication config field: {unknown[0]}")
    missing = [name for name in ("remote_url", "public_url", "work_dir", "publication_authorized") if name not in raw]
    if missing:
        raise PublishError(f"missing publication config field: {missing[0]}")
    branch = raw.get("branch", "main")
    unsafe_component = any(
        component.startswith(".") or component.endswith(".lock")
        for component in branch.split("/")
    ) if isinstance(branch, str) else True
    if (
        not isinstance(branch, str)
        or not _BRANCH_RE.fullmatch(branch)
        or branch == "@"
        or unsafe_component
    ):
        raise PublishError("branch is not a safe Git branch name")
    authorized = raw["publication_authorized"]
    if not isinstance(authorized, bool):
        raise PublishError("publication_authorized must be a boolean")
    kinds = raw.get("source_kinds", ["official", "community"])
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(not isinstance(kind, str) or kind not in {"official", "community"} for kind in kinds)
    ):
        raise PublishError("source_kinds must be a non-empty list containing only official/community")
    if len(set(kinds)) != len(kinds):
        raise PublishError("source_kinds must not contain duplicates")

    work_dir = _absolute_path(raw["work_dir"], base, "work_dir")
    if work_dir in {Path("/"), Path.home().resolve(), base}:
        raise PublishError("work_dir must be a dedicated subdirectory")
    ssh_key = raw.get("ssh_key")
    key_path = _absolute_path(ssh_key, base, "ssh_key") if ssh_key is not None else None
    remote = _remote_url(raw["remote_url"], base)
    if key_path is not None and not _GITHUB_SSH_RE.fullmatch(remote):
        raise PublishError("ssh_key is supported only with a GitHub SSH remote")
    if key_path is not None and (key_path.is_symlink() or not key_path.is_file()):
        raise PublishError("ssh_key must be a regular, non-symlink file")
    return {
        "remote_url": remote,
        "branch": branch,
        "public_url": _public_url(raw["public_url"]),
        "work_dir": work_dir,
        "ssh_key": key_path,
        "publication_authorized": authorized,
        "source_kinds": list(kinds),
    }


def load_config(path: os.PathLike[str] | str) -> Dict[str, Any]:
    """Load and validate a publication JSON file, resolving relative paths beside it."""

    try:
        config_path = Path(path).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError) as exc:
        raise PublishError("publication config path is invalid") from exc
    if not config_path.is_file() or config_path.is_symlink():
        raise PublishError(f"publication config is not a regular file: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"could not read publication config: {_safe_error(exc)}") from exc
    return _validate_config(raw, config_path.parent)


def _require_authorized(config: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = _validate_config(config)
    if not normalized["publication_authorized"]:
        raise PublishError(
            "publication_authorized must be true; this acknowledgement does not grant legal rights to publish data"
        )
    return normalized


def _git_env(config: Mapping[str, Any]) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    })
    key = config.get("ssh_key")
    command = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    if key is not None:
        command += f" -o IdentitiesOnly=yes -i {shlex.quote(str(key))}"
    env["GIT_SSH_COMMAND"] = command
    return env


def _git(repo: Path, config: Mapping[str, Any], args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            timeout=_GIT_TIMEOUT, env=_git_env(config), check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"git operation failed: {_safe_error(exc)}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise PublishError(f"git operation failed: {_safe_error(detail)}")
    return result


def _check_local_remote(config: Mapping[str, Any]) -> None:
    remote = config["remote_url"]
    if _GITHUB_SSH_RE.fullmatch(remote):
        return
    path = Path(remote)
    if path.is_symlink() or not path.is_dir():
        raise PublishError("local remote_url must be an existing bare repository directory")
    result = _git(path, config, ["rev-parse", "--is-bare-repository"])
    if result.stdout.strip() != "true":
        raise PublishError("local remote_url must identify a bare Git repository")


@contextlib.contextmanager
def _workspace(config: Mapping[str, Any]) -> Iterator[Path]:
    root = config["work_dir"]
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise PublishError("work_dir must be a real directory, not a file or symlink")
    if not root.exists():
        try:
            root.mkdir(parents=True, mode=0o700)
            (root / _MARKER).write_text(_MARKER_CONTENT, encoding="utf-8")
        except OSError as exc:
            raise PublishError(f"could not create work_dir: {_safe_error(exc)}") from exc
    marker = root / _MARKER
    try:
        if marker.is_symlink() or marker.read_text(encoding="utf-8") != _MARKER_CONTENT:
            raise PublishError("work_dir is not owned by this publisher")
    except OSError as exc:
        raise PublishError("work_dir is not owned by this publisher") from exc

    lock = root / _LOCK
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            str(lock),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise PublishError("another publication operation is already running") from exc
    except PublishError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise PublishError(f"could not lock work_dir: {_safe_error(exc)}") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="operation-", dir=str(root)) as directory:
            yield Path(directory)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class _DigestParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[Optional[str]] = []

    def handle_starttag(self, tag: str, attrs: list[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "meta":
            return
        values = {name.lower(): value for name, value in attrs}
        if (values.get("name") or "").lower() == "dram-data-digest":
            self.values.append(values.get("content"))

    handle_startendtag = handle_starttag


def _digest(page: bytes) -> str:
    try:
        text = page.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublishError("index.html must be valid UTF-8") from exc
    parser = _DigestParser()
    parser.feed(text)
    parser.close()
    if len(parser.values) != 1:
        raise PublishError("index.html must contain exactly one dram-data-digest meta element")
    digest = parser.values[0]
    if digest is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise PublishError("dram-data-digest content must be 64 lowercase hexadecimal characters")
    return digest


def _tree_entries(repo: Path, config: Mapping[str, Any], revision: str) -> Tuple[str, ...]:
    result = _git(repo, config, ["ls-tree", "-r", "-z", revision])
    entries = []
    for raw in result.stdout.split("\x00"):
        if not raw:
            continue
        try:
            metadata, name = raw.split("\t", 1)
            mode, kind, _object = metadata.split(" ", 2)
        except ValueError as exc:
            raise PublishError("remote repository has an invalid tree") from exc
        if mode == "120000" or kind != "blob" or name not in _ALLOWED_FILES:
            raise PublishError("remote tree contains an extra file, directory, or symlink")
        entries.append(name)
    if tuple(sorted(entries)) != _ALLOWED_FILES:
        raise PublishError("remote tree must contain exactly index.html and .nojekyll")
    return tuple(sorted(entries))


def _show_bytes(repo: Path, config: Mapping[str, Any], revision: str, name: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{revision}:{name}"], capture_output=True,
            timeout=_GIT_TIMEOUT, env=_git_env(config), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"git operation failed: {_safe_error(exc)}") from exc
    if result.returncode:
        raise PublishError("could not read the expected public page from Git")
    return result.stdout


def _prepare_repository(repo: Path, config: Mapping[str, Any]) -> Optional[str]:
    _check_local_remote(config)
    _git(repo, config, ["init", "--quiet"])
    _git(repo, config, ["config", "user.name", "DRAM Tracker Publisher"])
    _git(repo, config, ["config", "user.email", "dram-tracker@localhost"])
    _git(repo, config, ["remote", "add", "origin", config["remote_url"]])
    ref = f"refs/heads/{config['branch']}"
    advertised = _git(repo, config, ["ls-remote", "--heads", "origin", ref])
    lines = [line for line in advertised.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise PublishError("remote branch advertisement is ambiguous")
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != ref or not _REVISION_RE.fullmatch(fields[0]):
        raise PublishError("remote branch advertisement is invalid")
    _git(repo, config, ["fetch", "--quiet", "--no-tags", "origin", f"{ref}:refs/remotes/origin/{config['branch']}"])
    _git(repo, config, ["checkout", "--quiet", "-B", config["branch"], f"refs/remotes/origin/{config['branch']}"])
    head = _git(repo, config, ["rev-parse", "HEAD"]).stdout.strip()
    if head.lower() != fields[0].lower():
        raise PublishError("remote branch changed while it was being fetched")
    _tree_entries(repo, config, "HEAD")
    return head


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, original: str):
        super().__init__()
        parsed = urlsplit(original)
        self._origin = (parsed.scheme, parsed.hostname, parsed.port)

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validated = _public_url(newurl)
        parsed = urlsplit(validated)
        if (parsed.scheme, parsed.hostname, parsed.port) != self._origin:
            raise urllib.error.URLError("cross-origin redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, validated)


def _live_matches(url: str, expected: bytes) -> bool:
    opener = urllib.request.build_opener(_SafeRedirect(url))
    for attempt in range(_HTTP_ATTEMPTS):
        request = urllib.request.Request(
            url, headers={"Accept": "text/html", "Cache-Control": "no-cache", "Pragma": "no-cache"}
        )
        try:
            with opener.open(request, timeout=_HTTP_TIMEOUT) as response:
                if response.status == 200 and response.read(len(expected) + 1) == expected:
                    return True
        except (OSError, urllib.error.URLError, ValueError):
            pass
        if attempt + 1 < _HTTP_ATTEMPTS:
            time.sleep(0.25)
    return False


def _result(status: str, digest: str, commit: str, config: Mapping[str, Any], error: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": status, "data_digest": digest, "commit_sha": commit,
        "public_url": config["public_url"],
    }
    if error is not None:
        result["error_text"] = error
    return result


def _commit_and_push(
    repo: Path,
    config: Mapping[str, Any],
    page: bytes,
    message: str,
    old_head: Optional[str],
    nojekyll: bytes = b"",
) -> str:
    try:
        (repo / "index.html").write_bytes(page)
        (repo / ".nojekyll").write_bytes(nojekyll)
    except OSError as exc:
        raise PublishError(f"could not prepare publication files: {_safe_error(exc)}") from exc
    _git(repo, config, ["add", "--", "index.html", ".nojekyll"])
    staged = _git(repo, config, ["diff", "--cached", "--name-only"]).stdout.splitlines()
    if tuple(sorted(staged)) != _ALLOWED_FILES and old_head is None:
        raise PublishError("initial publication did not stage exactly the two public files")
    if any(name not in _ALLOWED_FILES for name in staged):
        raise PublishError("publication attempted to change a non-public file")
    _git(repo, config, ["commit", "--quiet", "-m", message])
    commit = _git(repo, config, ["rev-parse", "HEAD"]).stdout.strip()
    destination = f"HEAD:refs/heads/{config['branch']}"
    push = _git(repo, config, ["push", "--porcelain", "origin", destination], check=False)
    if push.returncode:
        raise PublishError("remote branch changed; publication was not pushed")
    return commit


def publish(candidate_path: os.PathLike[str] | str, config: Mapping[str, Any]) -> Dict[str, Any]:
    """Publish a generated page and confirm the public URL contains its exact bytes.

    A successful push whose URL has not converged is returned as ``pending``;
    ``published`` always means the fetched page exactly matched the committed page.
    """

    normalized = _require_authorized(config)
    try:
        candidate = Path(candidate_path)
    except (TypeError, ValueError) as exc:
        raise PublishError("candidate_path is invalid") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise PublishError("candidate_path must be a regular, non-symlink file")
    try:
        page = candidate.read_bytes()
    except OSError as exc:
        raise PublishError(f"could not read candidate page: {_safe_error(exc)}") from exc
    digest = _digest(page)

    with _workspace(normalized) as operation:
        repo = operation / "repo"
        repo.mkdir()
        old_head = _prepare_repository(repo, normalized)
        if old_head is not None:
            remote_page = _show_bytes(repo, normalized, "HEAD", "index.html")
            remote_digest = _digest(remote_page)
            if remote_digest == digest:
                if _live_matches(normalized["public_url"], remote_page):
                    return _result("unchanged", remote_digest, old_head, normalized)
                return _result("pending", remote_digest, old_head, normalized, "public URL does not yet match the remote page")
        commit = _commit_and_push(repo, normalized, page, f"Publish DRAM data {digest}", old_head)
        if _live_matches(normalized["public_url"], page):
            return _result("published", digest, commit, normalized)
        return _result("pending", digest, commit, normalized, "public URL does not yet match the pushed page")


def rollback(config: Mapping[str, Any], commit_sha: str) -> Dict[str, Any]:
    """Restore an allowlisted ancestor as a new non-force commit and verify it live."""

    normalized = _require_authorized(config)
    if not isinstance(commit_sha, str) or not _REVISION_RE.fullmatch(commit_sha):
        raise PublishError("commit_sha must be a full 40-character hexadecimal Git object ID")
    with _workspace(normalized) as operation:
        repo = operation / "repo"
        repo.mkdir()
        old_head = _prepare_repository(repo, normalized)
        if old_head is None:
            raise PublishError("cannot roll back an empty remote branch")
        target_result = _git(repo, normalized, ["rev-parse", "--verify", f"{commit_sha}^{{commit}}"], check=False)
        target = target_result.stdout.strip()
        if target_result.returncode or target.lower() != commit_sha.lower():
            raise PublishError("rollback target is not a commit in the current branch history")
        ancestor = _git(repo, normalized, ["merge-base", "--is-ancestor", target, "HEAD"], check=False)
        if ancestor.returncode != 0 or target == old_head:
            raise PublishError("rollback target must be a strict ancestor of the current branch")
        _tree_entries(repo, normalized, target)
        page = _show_bytes(repo, normalized, target, "index.html")
        nojekyll = _show_bytes(repo, normalized, target, ".nojekyll")
        digest = _digest(page)
        current_tree = _git(repo, normalized, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
        target_tree = _git(repo, normalized, ["rev-parse", f"{target}^{{tree}}"]).stdout.strip()
        if current_tree == target_tree:
            raise PublishError("rollback target has the same public tree as the current branch")
        _git(repo, normalized, ["checkout", target, "--", "index.html", ".nojekyll"])
        commit = _commit_and_push(
            repo, normalized, page, f"Rollback DRAM publication to {target}", old_head, nojekyll
        )
        if _live_matches(normalized["public_url"], page):
            return _result("published", digest, commit, normalized)
        return _result("pending", digest, commit, normalized, "public URL does not yet match the rollback page")
