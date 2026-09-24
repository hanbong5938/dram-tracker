"""SQLite persistence for the personal DRAM tracker.

The module deliberately contains no network or presentation code.  It owns the
small schema shared by the collector, history importer, and local renderers.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union
from urllib.parse import urlparse

PRODUCT = "DDR5 16Gb (2Gx8) 4800/5600"

_OBSERVATION_COLUMNS = (
    "observation_key",
    "product",
    "source_kind",
    "source_url",
    "date_text",
    "observed_date",
    "date_precision",
    "source_updated_at",
    "fetched_at",
    "value_text",
    "value_qualifier",
    "session",
    "evidence_note",
)
_REQUIRED_OBSERVATION_FIELDS = (
    "observation_key",
    "product",
    "source_kind",
    "source_url",
    "date_text",
    "observed_date",
    "date_precision",
    "value_text",
    "value_qualifier",
    "evidence_note",
)
_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?\Z")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PUBLICATION_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_PUBLICATION_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_PUBLICATION_TERMINAL_STATUSES = (
    "published",
    "unchanged",
    "pending",
    "error",
    "blocked",
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    observation_key TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('official', 'community')),
    source_url TEXT NOT NULL,
    date_text TEXT NOT NULL,
    observed_date TEXT,
    date_precision TEXT NOT NULL CHECK (date_precision IN ('day', 'approximate')),
    source_updated_at TEXT,
    fetched_at TEXT,
    value_text TEXT NOT NULL,
    value_qualifier TEXT NOT NULL CHECK (value_qualifier IN ('reported', 'approximate', 'displayed')),
    session TEXT CHECK (session IS NULL OR session = 'middle'),
    evidence_note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_render_order
    ON observations (observed_date, source_updated_at, observation_key);
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    expected_date TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'error')),
    observation_key TEXT,
    error_text TEXT
);
CREATE INDEX IF NOT EXISTS collection_runs_started_order
    ON collection_runs (started_at, run_id);
CREATE TABLE IF NOT EXISTS publication_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    collection_run_id INTEGER REFERENCES collection_runs (run_id),
    status TEXT NOT NULL CHECK (
        status IN ('running', 'published', 'unchanged', 'pending', 'error', 'blocked')
    ),
    data_digest TEXT,
    commit_sha TEXT,
    public_url TEXT,
    error_text TEXT
);
CREATE INDEX IF NOT EXISTS publication_runs_started_order
    ON publication_runs (started_at, run_id);
"""


def _text(value: Any, field: str, *, required: bool = True) -> Optional[str]:
    if value is None:
        if required:
            raise ValueError("observation field %s is required" % field)
        return None
    if not isinstance(value, str):
        raise ValueError("observation field %s must be text" % field)
    if not value or _CONTROL_RE.search(value):
        raise ValueError("observation field %s is empty or contains control characters" % field)
    return value


def _validate_iso_date(value: Any, field: str = "observed_date") -> str:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ValueError("%s must be an ISO date (YYYY-MM-DD)" % field)
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("%s is not a valid calendar date" % field) from exc
    return value


def _parse_datetime(value: Any, field: str, *, require_utc: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be an ISO timestamp" % field)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("%s must be an ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s must include a timezone offset" % field)
    if require_utc and parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("%s must be UTC" % field)
    return value


def _validate_decimal(value: Any, field: str = "value_text") -> str:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise ValueError("%s must be an exact positive decimal" % field)
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("%s must be an exact positive decimal" % field) from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("%s must be finite and greater than zero" % field)
    return value


def _validate_url(value: Any) -> str:
    text = _text(value, "source_url")
    assert text is not None
    if any(character.isspace() for character in text):
        raise ValueError("source_url must not contain whitespace")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("source_url must be an absolute HTTP(S) URL")
    return text


def _validate_public_url(value: Any) -> str:
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise ValueError("public_url must be non-empty text without control characters")
    if any(character.isspace() for character in value):
        raise ValueError("public_url must not contain whitespace")
    parsed = urlparse(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("public_url must be a valid absolute HTTP(S) URL") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or hostname is None
    ):
        raise ValueError("public_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public_url must not contain credentials")
    return value


def _validate_positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("%s must be a positive integer" % field)
    return value


def _validate_publication_hash(
    value: Optional[str], field: str, pattern: "re.Pattern[str]"
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        if field == "data_digest":
            raise ValueError("data_digest must be a 64-character hexadecimal digest")
        raise ValueError("commit_sha must be a 40- or 64-character hexadecimal hash")
    return value.lower()


def _validate_observation(observation: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise ValueError("observation must be a mapping")
    missing = [name for name in _REQUIRED_OBSERVATION_FIELDS if name not in observation]
    if missing:
        raise ValueError("observation is missing required field(s): %s" % ", ".join(missing))

    result: Dict[str, Any] = {}
    result["observation_key"] = _text(observation.get("observation_key"), "observation_key")
    result["product"] = _text(observation.get("product"), "product")
    if result["product"] != PRODUCT:
        raise ValueError("product must be %r" % PRODUCT)

    source_kind = _text(observation.get("source_kind"), "source_kind")
    if source_kind not in ("official", "community"):
        raise ValueError("source_kind must be 'official' or 'community'")
    result["source_kind"] = source_kind
    result["source_url"] = _validate_url(observation.get("source_url"))
    result["date_text"] = _text(observation.get("date_text"), "date_text")

    date_precision = _text(observation.get("date_precision"), "date_precision")
    if date_precision not in ("day", "approximate"):
        raise ValueError("date_precision must be 'day' or 'approximate'")
    result["date_precision"] = date_precision

    observed_date = observation.get("observed_date")
    if observed_date is None:
        if date_precision != "approximate":
            raise ValueError("observed_date may be null only for approximate dates")
        result["observed_date"] = None
    else:
        result["observed_date"] = _validate_iso_date(observed_date)

    source_updated_at = observation.get("source_updated_at")
    if source_updated_at is not None:
        result["source_updated_at"] = _parse_datetime(source_updated_at, "source_updated_at")
    else:
        result["source_updated_at"] = None

    fetched_at = observation.get("fetched_at")
    if fetched_at is not None:
        result["fetched_at"] = _parse_datetime(fetched_at, "fetched_at", require_utc=True)
    else:
        result["fetched_at"] = None

    result["value_text"] = _validate_decimal(observation.get("value_text"))
    value_qualifier = _text(observation.get("value_qualifier"), "value_qualifier")
    if value_qualifier not in ("reported", "approximate", "displayed"):
        raise ValueError("value_qualifier must be 'reported', 'approximate', or 'displayed'")
    result["value_qualifier"] = value_qualifier

    session = observation.get("session")
    if session is not None:
        session = _text(session, "session")
        if session != "middle":
            raise ValueError("session must be 'middle' or null")
    result["session"] = session
    result["evidence_note"] = _text(observation.get("evidence_note"), "evidence_note")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """Open *path*, configure rows as mappings, and initialize the schema."""
    if isinstance(path, str) and path == ":memory:":
        database = path
    else:
        database_path = Path(path).expanduser()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database = str(database_path)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def save_observation(conn: sqlite3.Connection, observation: Mapping[str, Any]) -> bool:
    """Insert an observation, returning false for an identical existing key.

    A key collision with any differing source or value column is an error.
    ``fetched_at`` is acquisition metadata and may naturally differ on a
    repeated fetch of the same published source observation.  In particular, a
    changed price for an already published source timestamp must never be
    silently replaced.
    """
    values = _validate_observation(observation)
    parameters = tuple(values[column] for column in _OBSERVATION_COLUMNS)
    placeholders = ", ".join("?" for _ in _OBSERVATION_COLUMNS)
    columns = ", ".join(_OBSERVATION_COLUMNS)
    transaction_owned = conn.in_transaction
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO observations (%s) VALUES (%s)" % (columns, placeholders),
            parameters,
        )
        if cursor.rowcount == 1:
            if not transaction_owned:
                conn.commit()
            return True
        existing_row = conn.execute(
            "SELECT %s FROM observations WHERE observation_key = ?" % columns,
            (values["observation_key"],),
        ).fetchone()
        if existing_row is None:
            if not transaction_owned and conn.in_transaction:
                conn.rollback()
            raise RuntimeError("observation insert was ignored but no matching key exists")
        existing = tuple(existing_row[column] for column in _OBSERVATION_COLUMNS)
        comparison_columns = tuple(
            column for column in _OBSERVATION_COLUMNS if column != "fetched_at"
        )
        comparison_existing = tuple(
            existing[_OBSERVATION_COLUMNS.index(column)] for column in comparison_columns
        )
        comparison_parameters = tuple(
            parameters[_OBSERVATION_COLUMNS.index(column)] for column in comparison_columns
        )
        if comparison_existing != comparison_parameters:
            if not transaction_owned and conn.in_transaction:
                conn.rollback()
            differing = [
                column
                for column, old, new in zip(
                    comparison_columns, comparison_existing, comparison_parameters
                )
                if old != new
            ]
            raise ValueError(
                "observation_key conflict for %r; differing field(s): %s"
                % (values["observation_key"], ", ".join(differing))
            )
        if not transaction_owned:
            conn.commit()
        return False
    except Exception:
        if not transaction_owned and conn.in_transaction:
            conn.rollback()
        raise

def observations(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return observations in chronological order suitable for rendering."""
    rows = conn.execute(
        "SELECT %s FROM observations "
        "ORDER BY observed_date ASC, "
        "CASE WHEN source_updated_at IS NULL THEN 0 ELSE 1 END ASC, "
        "source_updated_at ASC, observation_key ASC" % ", ".join(_OBSERVATION_COLUMNS)
    ).fetchall()
    return [dict(row) for row in rows]


def start_collection_run(conn: sqlite3.Connection, expected_date: Optional[str]) -> int:
    """Record the beginning of a collection attempt and return its id."""
    try:
        cursor = conn.execute(
            "INSERT INTO collection_runs (started_at, expected_date, status) VALUES (?, ?, 'running')",
            (_utc_now(), expected_date),
        )
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def finish_collection_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    observation_key: Optional[str] = None,
    error_text: Optional[str] = None,
) -> None:
    """Complete a collection run with success or a user-facing failure."""
    if status not in ("success", "error"):
        raise ValueError("collection run status must be 'success' or 'error'")
    if error_text is not None:
        error_text = str(error_text)
    try:
        conn.execute(
            "UPDATE collection_runs SET finished_at = ?, status = ?, observation_key = ?, error_text = ? "
            "WHERE run_id = ?",
            (_utc_now(), status, observation_key, error_text, run_id),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def start_publication_run(
    conn: sqlite3.Connection,
    *,
    collection_run_id: Optional[int] = None,
    public_url: Optional[str] = None,
) -> int:
    """Record a running publication attempt without taking over a caller transaction."""
    if collection_run_id is not None:
        collection_run_id = _validate_positive_id(
            collection_run_id, "collection_run_id"
        )
        if (
            conn.execute(
                "SELECT 1 FROM collection_runs WHERE run_id = ?",
                (collection_run_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("collection_run_id does not identify a collection run")
    if public_url is not None:
        public_url = _validate_public_url(public_url)

    transaction_owned = conn.in_transaction
    try:
        cursor = conn.execute(
            "INSERT INTO publication_runs "
            "(started_at, collection_run_id, status, public_url) "
            "VALUES (?, ?, 'running', ?)",
            (_utc_now(), collection_run_id, public_url),
        )
        if not transaction_owned:
            conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        if not transaction_owned and conn.in_transaction:
            conn.rollback()
        raise


def finish_publication_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    data_digest: Optional[str] = None,
    commit_sha: Optional[str] = None,
    error_text: Optional[str] = None,
) -> None:
    """Finish one running publication attempt exactly once.

    ``published`` and ``unchanged`` are accepted only with both the content
    digest and commit hash supplied by the publisher that verified the public
    URL.  This function records that result; it does not itself claim or test
    external URL availability.
    """
    run_id = _validate_positive_id(run_id, "run_id")
    if status not in _PUBLICATION_TERMINAL_STATUSES:
        raise ValueError(
            "publication run status must be published, unchanged, pending, error, or blocked"
        )
    data_digest = _validate_publication_hash(
        data_digest, "data_digest", _PUBLICATION_DIGEST_RE
    )
    commit_sha = _validate_publication_hash(
        commit_sha, "commit_sha", _PUBLICATION_COMMIT_RE
    )
    if status in ("published", "unchanged") and (
        data_digest is None or commit_sha is None
    ):
        raise ValueError("%s publication runs require digest and commit hash" % status)
    if error_text is not None and not isinstance(error_text, str):
        raise ValueError("error_text must be text or null")

    transaction_owned = conn.in_transaction
    try:
        existing = conn.execute(
            "SELECT status FROM publication_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is None:
            raise ValueError("run_id does not identify a publication run")
        if existing["status"] != "running":
            raise ValueError("publication run is already finished")
        cursor = conn.execute(
            "UPDATE publication_runs "
            "SET finished_at = ?, status = ?, data_digest = ?, commit_sha = ?, error_text = ? "
            "WHERE run_id = ? AND status = 'running'",
            (_utc_now(), status, data_digest, commit_sha, error_text, run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("publication run is no longer running")
        if not transaction_owned:
            conn.commit()
    except Exception:
        if not transaction_owned and conn.in_transaction:
            conn.rollback()
        raise


def publication_runs(
    conn: sqlite3.Connection, limit: int = 20
) -> List[Dict[str, Any]]:
    """Return the newest publication attempts, independently of collection history."""
    limit = _validate_positive_id(limit, "limit")
    rows = conn.execute(
        "SELECT run_id, started_at, finished_at, collection_run_id, status, "
        "data_digest, commit_sha, public_url, error_text "
        "FROM publication_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def backup_database(conn: sqlite3.Connection, output_path: Union[str, Path]) -> Path:
    """Make a restore-ready online SQLite backup at *output_path*."""
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_row = conn.execute("PRAGMA database_list").fetchall()
    source_file = None
    for row in source_row:
        if row[1] == "main":
            source_file = row[2]
            break
    if source_file and source_file not in ("", ":memory:"):
        try:
            if Path(source_file).expanduser().resolve() == destination:
                raise ValueError("backup output must differ from the live database")
        except OSError:
            pass

    destination_conn = sqlite3.connect(str(destination), timeout=30)
    try:
        conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
    return destination


__all__ = [
    "PRODUCT",
    "backup_database",
    "connect",
    "finish_collection_run",
    "finish_publication_run",
    "observations",
    "publication_runs",
    "save_observation",
    "start_collection_run",
    "start_publication_run",
]
