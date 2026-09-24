"""Import the captured community price history into the SQLite store."""

from __future__ import annotations

import csv
import datetime as _datetime
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Union


CSV_COLUMNS = (
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

PRODUCT = "DDR5 16Gb (2Gx8) 4800/5600"
DEFAULT_SOURCE_URL = "https://www.fmkorea.com/10360993528"

_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}$"
)
_UTC_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]00:00)$"
)


class HistoryImportError(ValueError):
    """Raised when a history CSV is malformed before import starts."""


def _required_text(value: Optional[str], field: str, row_number: int) -> str:
    if value is None or not value.strip():
        raise HistoryImportError(
            "row {}: {} is required".format(row_number, field)
        )
    return value


def _optional_text(value: Optional[str]) -> Optional[str]:
    if value is None or value == "" or not value.strip():
        return None
    return value


def _validate_date(value: Optional[str], field: str, row_number: int) -> Optional[str]:
    value = _optional_text(value)
    if value is None:
        return None
    if not _DATE_RE.fullmatch(value):
        raise HistoryImportError(
            "row {}: {} must be an ISO date".format(row_number, field)
        )
    try:
        _datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise HistoryImportError(
            "row {}: {} is not a valid date".format(row_number, field)
        ) from exc
    return value


def _validate_offset_datetime(
    value: Optional[str], field: str, row_number: int
) -> Optional[str]:
    value = _optional_text(value)
    if value is None:
        return None
    if not _OFFSET_DATETIME_RE.fullmatch(value):
        raise HistoryImportError(
            "row {}: {} must be an ISO datetime with an offset".format(
                row_number, field
            )
        )
    try:
        parsed = _datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise HistoryImportError(
            "row {}: {} is not a valid datetime".format(row_number, field)
        ) from exc
    if parsed.utcoffset() is None:
        raise HistoryImportError(
            "row {}: {} must include a UTC offset".format(row_number, field)
        )
    return value


def _validate_utc_datetime(
    value: Optional[str], field: str, row_number: int
) -> Optional[str]:
    value = _optional_text(value)
    if value is None:
        return None
    if not _UTC_DATETIME_RE.fullmatch(value):
        raise HistoryImportError(
            "row {}: {} must be an ISO UTC datetime".format(row_number, field)
        )
    parsed_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _datetime.datetime.fromisoformat(parsed_value)
    except ValueError as exc:
        raise HistoryImportError(
            "row {}: {} is not a valid datetime".format(row_number, field)
        ) from exc
    if parsed.utcoffset() != _datetime.timedelta(0):
        raise HistoryImportError(
            "row {}: {} must be UTC".format(row_number, field)
        )
    return value


def _validate_value(value: Optional[str], row_number: int) -> str:
    value = _required_text(value, "value_text", row_number)
    if not _DECIMAL_RE.fullmatch(value):
        raise HistoryImportError(
            "row {}: value_text must be a decimal".format(row_number)
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise HistoryImportError(
            "row {}: value_text must be a decimal".format(row_number)
        ) from exc
    if not parsed.is_finite():
        raise HistoryImportError(
            "row {}: value_text must be finite".format(row_number)
        )
    return value


def _validate_row(raw: Dict[str, Optional[str]], row_number: int) -> Dict[str, Optional[str]]:
    required = {
        "observation_key": _required_text(
            raw.get("observation_key"), "observation_key", row_number
        ),
        "product": _required_text(raw.get("product"), "product", row_number),
        "source_kind": _required_text(
            raw.get("source_kind"), "source_kind", row_number
        ),
        "source_url": _required_text(raw.get("source_url"), "source_url", row_number),
        "date_text": _required_text(raw.get("date_text"), "date_text", row_number),
        "date_precision": _required_text(
            raw.get("date_precision"), "date_precision", row_number
        ),
        "value_qualifier": _required_text(
            raw.get("value_qualifier"), "value_qualifier", row_number
        ),
        "evidence_note": _required_text(
            raw.get("evidence_note"), "evidence_note", row_number
        ),
    }

    if required["product"] != PRODUCT:
        raise HistoryImportError(
            "row {}: product does not match {}".format(row_number, PRODUCT)
        )
    if required["source_kind"] not in {"official", "community"}:
        raise HistoryImportError(
            "row {}: source_kind must be official or community".format(row_number)
        )
    if required["date_precision"] not in {"day", "approximate"}:
        raise HistoryImportError(
            "row {}: date_precision must be day or approximate".format(row_number)
        )
    if required["value_qualifier"] not in {
        "reported",
        "approximate",
        "displayed",
    }:
        raise HistoryImportError(
            "row {}: invalid value_qualifier".format(row_number)
        )

    observed_date = _validate_date(raw.get("observed_date"), "observed_date", row_number)
    if required["date_precision"] == "day" and observed_date is None:
        raise HistoryImportError(
            "row {}: day precision requires observed_date".format(row_number)
        )
    if required["date_precision"] == "approximate" and observed_date is not None:
        raise HistoryImportError(
            "row {}: approximate precision cannot have observed_date".format(row_number)
        )

    source_updated_at = _validate_offset_datetime(
        raw.get("source_updated_at"), "source_updated_at", row_number
    )
    fetched_at = _validate_utc_datetime(raw.get("fetched_at"), "fetched_at", row_number)
    session = _optional_text(raw.get("session"))
    if session is not None and session != "middle":
        raise HistoryImportError("row {}: invalid session".format(row_number))

    return {
        "observation_key": required["observation_key"],
        "product": required["product"],
        "source_kind": required["source_kind"],
        "source_url": required["source_url"],
        "date_text": required["date_text"],
        "observed_date": observed_date,
        "date_precision": required["date_precision"],
        "source_updated_at": source_updated_at,
        "fetched_at": fetched_at,
        "value_text": _validate_value(raw.get("value_text"), row_number),
        "value_qualifier": required["value_qualifier"],
        "session": session,
        "evidence_note": required["evidence_note"],
    }


def _read_rows(path: Path) -> List[Dict[str, Optional[str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise HistoryImportError("history CSV has no header")
            if len(fieldnames) != len(set(fieldnames)):
                raise HistoryImportError("history CSV has duplicate columns")
            if set(fieldnames) != set(CSV_COLUMNS):
                raise HistoryImportError(
                    "history CSV columns must be: {}".format(", ".join(CSV_COLUMNS))
                )

            rows: List[Dict[str, Optional[str]]] = []
            for row_number, raw in enumerate(reader, start=2):
                if None in raw:
                    raise HistoryImportError(
                        "row {} has more fields than the header".format(row_number)
                    )
                rows.append(_validate_row(raw, row_number))
            return rows
    except csv.Error as exc:
        raise HistoryImportError("invalid CSV: {}".format(exc)) from exc


def import_history(conn, csv_path: Optional[Union[str, Path]] = None) -> int:
    """Validate and atomically import all rows from a captured history CSV.

    The CSV is fully read and validated before a savepoint is opened.  Each
    insert then goes through ``store.save_observation`` so duplicate rows are
    harmless while conflicting existing observations remain errors.
    """

    path = Path(csv_path) if csv_path is not None else Path(__file__).with_name("seed.csv")
    rows = _read_rows(path)
    if not rows:
        return 0

    import store

    conn.execute("SAVEPOINT history_import")
    try:
        inserted = 0
        for row in rows:
            if store.save_observation(conn, row):
                inserted += 1
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT history_import")
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT history_import")
    return inserted
