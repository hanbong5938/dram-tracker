"""Fetch and validate the public DRAM Exchange middle-session price."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import store

SOURCE_URL = "https://www.dramexchange.com/"
PRODUCT = store.PRODUCT
FETCH_TIMEOUT_SECONDS = 20
_TAIPEI = timezone(timedelta(hours=8))
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?\Z")
_SESSION_CHANGE_RE = re.compile(r"[+-]?\d+(?:\.\d+)?\s*%\Z")
_TIMESTAMP_RE = re.compile(
    r"^Last\s+Update:\s*"
    r"(?P<date>[A-Za-z]{3,9}\.?\s*\d{1,2}\s+\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
    r"\(GMT(?P<sign>[+-])(?P<offset_hour>\d{1,2})"
    r"(?::?(?P<offset_minute>\d{2}))?\)"
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_HEADER = (
    "Item",
    "Daily High",
    "Daily Low",
    "Session High",
    "Session Low",
    "Session Average",
    "Session Change",
    "History",
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "command",
        "embed",
        "hr",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class CollectionError(RuntimeError):
    """An expected failure while fetching, parsing, or persisting a price."""


class _TargetPageParser(HTMLParser):
    """Extract only the identified DRAM table and its identified timestamp."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: List[str] = []
        self._target_tbody_depth: Optional[int] = None
        self._target_tbody_count = 0
        self._current_row: Optional[List[str]] = None
        self._current_row_depth: Optional[int] = None
        self._current_cell: Optional[List[str]] = None
        self._current_cell_depth: Optional[int] = None
        self.rows: List[List[str]] = []
        self._timestamp_parts: List[str] = []
        self._timestamp_depth: Optional[int] = None
        self.timestamp_texts: List[str] = []

    @staticmethod
    def _attrs_dict(attrs: Sequence[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        return {name.lower(): (value or "") for name, value in attrs}

    @staticmethod
    def _clean(parts: Sequence[str]) -> str:
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = self._attrs_dict(attrs)
        parent_depth = len(self._stack)

        if tag == "tbody" and attributes.get("id") == "tb_NationalDramSpotPrice":
            self._target_tbody_count += 1
            if self._target_tbody_depth is None:
                self._target_tbody_depth = parent_depth + 1

        if (
            self._target_tbody_depth is not None
            and tag == "tr"
            and parent_depth == self._target_tbody_depth
        ):
            if self._current_row is not None:
                raise ValueError("target DRAM table contains an unclosed row")
            self._current_row = []
            self._current_row_depth = parent_depth + 1

        if (
            self._current_row is not None
            and tag in ("td", "th")
            and self._current_row_depth is not None
            and parent_depth == self._current_row_depth
        ):
            if self._current_cell is not None:
                raise ValueError("target DRAM table contains an unclosed cell")
            self._current_cell = []
            self._current_cell_depth = parent_depth + 1

        if tag == "td" and attributes.get("id") == "NationalDramSpotPrice_show_day":
            if self._timestamp_depth is not None:
                raise ValueError("DRAM page contains duplicate target timestamp cells")
            self._timestamp_parts = []
            self._timestamp_depth = parent_depth + 1

        if tag not in _VOID_TAGS:
            self._stack.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)


    def handle_data(self, data: str) -> None:
        if self._timestamp_depth is not None:
            self._timestamp_parts.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        depth = len(self._stack)

        if (
            self._timestamp_depth is not None
            and tag == "td"
            and depth == self._timestamp_depth
        ):
            self.timestamp_texts.append(self._clean(self._timestamp_parts))
            self._timestamp_parts = []
            self._timestamp_depth = None

        if (
            self._current_cell is not None
            and tag in ("td", "th")
            and self._current_cell_depth == depth
        ):
            assert self._current_row is not None
            self._current_row.append(self._clean(self._current_cell))
            self._current_cell = None
            self._current_cell_depth = None

        if (
            self._current_row is not None
            and tag == "tr"
            and self._current_row_depth == depth
        ):
            self.rows.append(self._current_row)
            self._current_row = None
            self._current_row_depth = None

        if self._stack:
            if self._stack[-1] == tag:
                self._stack.pop()
            elif tag in self._stack:
                # HTMLParser is intentionally tolerant.  Discard malformed
                # nested tags so a later valid table cannot inherit stale state.
                index = len(self._stack) - 1 - self._stack[::-1].index(tag)
                del self._stack[index:]

        if tag == "tbody" and self._target_tbody_depth == depth:
            self._target_tbody_depth = None


def _validate_expected_date(expected_date: str) -> str:
    if not isinstance(expected_date, str) or not _ISO_DATE_RE.fullmatch(expected_date):
        raise ValueError("expected_date must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(expected_date)
    except ValueError as exc:
        raise ValueError("expected_date is not a valid calendar date") from exc
    return expected_date


def taipei_today() -> str:
    """Return today's date in the source's +08 calendar, not the host timezone."""
    return datetime.now(timezone.utc).astimezone(_TAIPEI).date().isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _positive_decimal(text: str, field: str) -> Decimal:
    if not _DECIMAL_RE.fullmatch(text):
        raise ValueError("target row %s is not an exact decimal" % field)
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("target row %s is not an exact decimal" % field) from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("target row %s must be finite and greater than zero" % field)
    return number


def _parse_timestamp(text: str, expected_date: str) -> Tuple[str, str, str]:
    match = _TIMESTAMP_RE.match(text)
    if match is None:
        raise ValueError("target DRAM timestamp is missing or malformed")
    suffix = text[match.end() :].strip()
    if suffix and suffix != "<Price Notice>":
        raise ValueError("target DRAM timestamp contains unexpected trailing text")
    sign = match.group("sign")
    offset_hour = int(match.group("offset_hour"))
    offset_minute = int(match.group("offset_minute") or "0")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if sign != "+" or offset_hour != 8 or offset_minute != 0:
        raise ValueError("target DRAM timestamp must use GMT+8")
    if hour != 14 or minute != 40:
        raise ValueError("target DRAM timestamp is not the middle session update at 14:40")

    date_text = re.sub(r"\s+", " ", match.group("date")).strip()
    date_match = re.fullmatch(
        r"(?P<month>[A-Za-z]{3,9})\.?\s*(?P<day>\d{1,2})\s+(?P<year>\d{4})",
        date_text,
    )
    if date_match is None:
        raise ValueError("target DRAM timestamp date is malformed")
    month_name = date_match.group("month").lower()
    month = _MONTHS.get(month_name)
    if month is None:
        raise ValueError("target DRAM timestamp uses an unknown month")
    try:
        source_day = date(
            int(date_match.group("year")), month, int(date_match.group("day"))
        )
    except ValueError as exc:
        raise ValueError("target DRAM timestamp date is not valid") from exc
    observed_date = source_day.isoformat()
    if observed_date != expected_date:
        raise ValueError(
            "target DRAM source date %s does not match expected date %s"
            % (observed_date, expected_date)
        )
    source_updated_at = datetime(
        source_day.year,
        source_day.month,
        source_day.day,
        hour,
        minute,
        tzinfo=_TAIPEI,
    ).isoformat()
    return date_text, observed_date, source_updated_at


def parse_page(html_text: str, expected_date: str) -> Dict[str, Any]:
    """Parse one homepage response into a validated observation dictionary."""
    expected = _validate_expected_date(expected_date)
    if not isinstance(html_text, str) or not html_text:
        raise ValueError("source homepage response is empty")
    if _CONTROL_RE.search(html_text.replace("\n", "").replace("\r", "").replace("\t", "")):
        raise ValueError("source homepage response contains control characters")

    parser = _TargetPageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ValueError("target DRAM table is malformed: %s" % exc) from exc

    if parser._target_tbody_count != 1 or not parser.rows:
        raise ValueError("target DRAM table was not found exactly once")
    if len(parser.timestamp_texts) != 1:
        raise ValueError("target DRAM timestamp was not found exactly once")
    if parser.rows[0] != list(_HEADER):
        raise ValueError("target DRAM table header does not match the expected columns")

    for row in parser.rows[1:]:
        if len(row) != len(_HEADER):
            raise ValueError("target DRAM table contains a row with the wrong number of columns")
    matches = [row for row in parser.rows[1:] if row[0] == PRODUCT]
    if any(row == list(_HEADER) for row in parser.rows[1:]):
        raise ValueError("target DRAM table contains a duplicate header row")
    if len(matches) != 1:
        raise ValueError("target DRAM product row was not found exactly once")
    row = matches[0]

    numbers = {
        "daily high": _positive_decimal(row[1], "daily high"),
        "daily low": _positive_decimal(row[2], "daily low"),
        "session high": _positive_decimal(row[3], "session high"),
        "session low": _positive_decimal(row[4], "session low"),
        "session average": _positive_decimal(row[5], "session average"),
    }
    if numbers["daily high"] < numbers["daily low"]:
        raise ValueError("target DRAM daily high is below daily low")
    if numbers["session high"] < numbers["session low"]:
        raise ValueError("target DRAM session high is below session low")
    if numbers["daily high"] < numbers["session high"]:
        raise ValueError("target DRAM session high exceeds daily high")
    if numbers["daily low"] > numbers["session low"]:
        raise ValueError("target DRAM session low is below daily low")
    if not numbers["session low"] <= numbers["session average"] <= numbers["session high"]:
        raise ValueError("target DRAM session average is outside session bounds")
    if not row[6] or _SESSION_CHANGE_RE.fullmatch(row[6]) is None:
        raise ValueError("target DRAM session change is malformed")

    date_text, observed_date, source_updated_at = _parse_timestamp(
        parser.timestamp_texts[0], expected
    )
    observation_key = "%s|%s|%s" % (observed_date, source_updated_at, PRODUCT)
    return {
        "observation_key": observation_key,
        "product": PRODUCT,
        "source_kind": "official",
        "source_url": SOURCE_URL,
        "date_text": date_text,
        "observed_date": observed_date,
        "date_precision": "day",
        "source_updated_at": source_updated_at,
        "fetched_at": None,
        "value_text": row[5],
        "value_qualifier": "displayed",
        "session": "middle",
        "evidence_note": (
            "DRAM Exchange homepage target table; exact displayed Session Average "
            "for the middle session."
        ),
    }


def fetch_page(
    url: str = SOURCE_URL, *, timeout: float = FETCH_TIMEOUT_SECONDS
) -> str:
    """Fetch the public homepage once, with an identifiable non-browser UA."""
    request = Request(
        url,
        headers={
            "User-Agent": (
                "DRAMTracker/1.0 (personal non-commercial price tracker; "
                "respects source terms)"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise CollectionError("public homepage fetch failed: %s" % exc) from exc
    try:
        return payload.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise CollectionError("public homepage response encoding is invalid") from exc


def collect_once(conn: Any, expected_date: Optional[str] = None) -> Dict[str, Any]:
    """Fetch, validate, and persist one source observation.

    Every attempted collection gets a ``collection_runs`` row, including
    network, date, parser, and duplicate/conflict failures.
    """
    requested_date: Any = expected_date if expected_date is not None else taipei_today()
    run_date = requested_date if isinstance(requested_date, str) else repr(requested_date)
    run_id = store.start_collection_run(conn, run_date)
    stage = "date validation"
    try:
        expected = _validate_expected_date(requested_date)
        stage = "public homepage fetch"
        html_text = fetch_page()
        stage = "target table parsing"
        observation = parse_page(html_text, expected)
        observation["fetched_at"] = _utc_now()
        stage = "observation persistence"
        store.save_observation(conn, observation)
    except Exception as exc:
        message = "%s failed: %s" % (stage, exc)
        try:
            store.finish_collection_run(conn, run_id, status="error", error_text=message)
        except Exception:
            # Preserve the actionable original failure if the logger itself
            # encounters a closed or otherwise broken connection.
            pass
        if isinstance(exc, CollectionError):
            raise CollectionError(message) from exc
        raise CollectionError(message) from exc

    store.finish_collection_run(
        conn,
        run_id,
        status="success",
        observation_key=observation["observation_key"],
    )
    return observation


__all__ = [
    "CollectionError",
    "FETCH_TIMEOUT_SECONDS",
    "PRODUCT",
    "SOURCE_URL",
    "collect_once",
    "fetch_page",
    "parse_page",
    "taipei_today",
]
