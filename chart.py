"""Offline HTML chart and CSV export for DRAM price observations."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


OBSERVATION_FIELDS: Tuple[str, ...] = (
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

PUBLIC_OBSERVATION_FIELDS: Tuple[str, ...] = (
    "product",
    "source_kind",
    "source_url",
    "date_text",
    "observed_date",
    "date_precision",
    "source_updated_at",
    "value_text",
    "value_qualifier",
    "session",
)
_PUBLIC_TEMPLATE_REVISION = "public-chart-v2"
_PUBLIC_SOURCE_KINDS = frozenset(("official", "community"))

PRODUCT_LABEL = "DDR5 16Gb (2Gx8) 4800/5600"
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FORMULA_PREFIX_RE = re.compile(r"^\s*[=+\-@]")


def _text(value: Any) -> str:
    """Return a stable string representation for a database field."""

    if value is None:
        return ""
    return str(value)


def _value(row: Any, field: str) -> Any:
    """Read a field from dictionaries, sqlite Rows, or simple row objects."""

    if isinstance(row, Mapping):
        return row.get(field)
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return getattr(row, field, None)


def _row_dict(row: Any) -> Dict[str, Any]:
    return {field: _value(row, field) for field in OBSERVATION_FIELDS}


def _iso_date(value: Any) -> Optional[date]:
    raw = _text(value)
    if not _ISO_DATE_RE.fullmatch(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _is_exact_date(row: Mapping[str, Any]) -> bool:
    return _text(row.get("date_precision")) == "day" and _iso_date(row.get("observed_date")) is not None


def _decimal(value: Any) -> Optional[Decimal]:
    raw = _text(value).strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _float_decimal(value: Decimal) -> Optional[float]:
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rows(conn: Any) -> List[Dict[str, Any]]:
    # Import lazily so chart.py remains importable while the application is being assembled.
    from store import observations

    return [_row_dict(row) for row in observations(conn)]


def _render_sort_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    observed = _iso_date(row.get("observed_date"))
    exact_group = 0 if _is_exact_date(row) else 1
    return (
        exact_group,
        observed.isoformat() if observed is not None else "",
        _text(row.get("date_text")),
        _text(row.get("source_kind")),
        _text(row.get("session")),
        _text(row.get("observation_key")),
        tuple(_text(row.get(field)) for field in OBSERVATION_FIELDS),
    )


def _csv_sort_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    observed = _iso_date(row.get("observed_date"))
    exact_group = 0 if observed is not None and _text(row.get("date_precision")) == "day" else 1
    return (
        exact_group,
        observed.isoformat() if observed is not None else "",
        _text(row.get("date_text")),
        _text(row.get("observation_key")),
        tuple(_text(row.get(field)) for field in OBSERVATION_FIELDS),
    )


def _safe_http_url(value: Any) -> Optional[str]:
    raw = _text(value).strip()
    if not raw or "\r" in raw or "\n" in raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


def _escaped(value: Any, *, quote: bool = False) -> str:
    return html.escape(_text(value), quote=quote)


def _display(value: Any, *, code: bool = False) -> str:
    raw = _text(value)
    if not raw:
        return '<span class="muted">—</span>'
    escaped = _escaped(raw)
    return "<code>" + escaped + "</code>" if code else escaped


def _source_markup(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return '<span class="muted">—</span>'
    safe = _safe_http_url(raw)
    escaped = _escaped(raw, quote=True)
    if safe is None:
        return '<code title="http 또는 https 주소만 링크로 엽니다">' + escaped + "</code>"
    return (
        '<a href="'
        + _escaped(safe, quote=True)
        + '" target="_blank" rel="noopener noreferrer">'
        + _escaped(raw)
        + "</a>"
    )


def _kind_markup(value: Any) -> str:
    raw = _text(value)
    label = {"official": "공식", "community": "커뮤니티"}.get(raw, raw or "미상")
    class_name = "official" if raw == "official" else "community" if raw == "community" else "other"
    return '<span class="kind kind-' + class_name + '">' + _escaped(label) + "</span>"


def _precision_markup(value: Any) -> str:
    raw = _text(value)
    label = {"day": "정확한 날짜", "approximate": "대략적인 날짜"}.get(raw, raw or "미상")
    return _escaped(label) + (" <small>(" + _escaped(raw) + ")</small>" if raw else "")


def _qualifier_markup(value: Any) -> str:
    raw = _text(value)
    label = {
        "reported": "보고값",
        "approximate": "근사값",
        "displayed": "화면 표시값",
    }.get(raw, raw or "미상")
    return _escaped(label) + (" <small>(" + _escaped(raw) + ")</small>" if raw else "")


def _field_markup(field: str, value: Any) -> str:
    if field == "source_url":
        return _source_markup(value)
    if field == "source_kind":
        return _kind_markup(value)
    if field == "date_precision":
        return _precision_markup(value)
    if field == "value_qualifier":
        return _qualifier_markup(value)
    if field == "value_text":
        return _display(value, code=True)
    return _display(value)


_TABLE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("observation_key", "관측 키"),
    ("product", "제품"),
    ("source_kind", "출처 종류"),
    ("source_url", "출처 링크"),
    ("date_text", "원문 날짜"),
    ("observed_date", "관측일"),
    ("date_precision", "날짜 정밀도"),
    ("source_updated_at", "출처 갱신 시각"),
    ("fetched_at", "가져온 시각"),
    ("value_text", "원문 가격"),
    ("value_qualifier", "값 상태"),
    ("session", "세션"),
    ("evidence_note", "근거 메모"),
)


def _observation_table(rows: Sequence[Mapping[str, Any]], table_id: str, caption: str) -> str:
    if not rows:
        return '<p class="empty">이 구분에 해당하는 관측이 없습니다.</p>'
    head = "".join("<th scope=\"col\">" + _escaped(label) + "</th>" for _, label in _TABLE_COLUMNS)
    body_parts: List[str] = []
    for row in rows:
        raw_kind = _text(row.get("source_kind"))
        row_class = "official-row" if raw_kind == "official" else "community-row" if raw_kind == "community" else "other-row"
        cells = "".join(
            "<td>" + _field_markup(field, row.get(field)) + "</td>" for field, _ in _TABLE_COLUMNS
        )
        body_parts.append('<tr class="' + row_class + '">' + cells + "</tr>")
    return (
        '<div class="table-wrap"><table id="'
        + _escaped(table_id, quote=True)
        + '"><caption>'
        + _escaped(caption)
        + "</caption><thead><tr>"
        + head
        + "</tr></thead><tbody>"
        + "".join(body_parts)
        + "</tbody></table></div>"
    )


def _svg_number(value: float) -> str:
    if not math.isfinite(value):
        return "0"
    rendered = format(value, ".4f").rstrip("0").rstrip(".")
    return rendered if rendered and rendered not in {"-0", "-0.0"} else "0"


def _axis_label(value: float) -> str:
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 10000 or magnitude < 0.01:
        return format(value, ".4g")
    return format(value, ".4f").rstrip("0").rstrip(".")


def _tick_dates(start: date, end: date, maximum: int = 6) -> List[date]:
    span = (end - start).days
    if span <= maximum - 1:
        return [start + timedelta(days=index) for index in range(span + 1)]
    ticks: List[date] = []
    for index in range(maximum):
        offset = round(span * index / (maximum - 1))
        candidate = start + timedelta(days=offset)
        if not ticks or candidate != ticks[-1]:
            ticks.append(candidate)
    return ticks


def _chart_svg(points: Sequence[Mapping[str, Any]]) -> str:
    width = 1100
    height = 450
    left = 78
    right = 30
    top = 34
    bottom = 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    title = "정확한 날짜별 가격 차트"
    description = "공식 중간 세션의 연속된 날짜만 선으로 연결하고, 커뮤니티 관측은 별도 기호로 표시합니다."

    if not points:
        return (
            '<svg class="price-chart" viewBox="0 0 1100 450" role="img" '
            'aria-labelledby="chart-title chart-desc"><title id="chart-title">'
            + _escaped(title)
            + '</title><desc id="chart-desc">'
            + _escaped(description)
            + '</desc><rect class="chart-empty" x="30" y="34" width="1040" height="346" rx="16" />'
            '<text class="chart-empty-text" x="550" y="210" text-anchor="middle">'
            '정확한 날짜와 숫자 가격 데이터가 없어 그래프를 그리지 않았습니다.</text></svg>'
        )

    date_values = [point["date"] for point in points]
    value_values = [point["number"] for point in points]
    min_date = min(date_values)
    max_date = max(date_values)
    date_span = (max_date - min_date).days
    domain_start = min_date - timedelta(days=1) if date_span == 0 else min_date
    domain_end = max_date + timedelta(days=1) if date_span == 0 else max_date
    domain_span = max(1, (domain_end - domain_start).days)
    minimum = min(value_values)
    maximum = max(value_values)
    value_span = maximum - minimum
    padding = max(abs(maximum) * 0.1, 1.0) if value_span == 0 else value_span * 0.08
    y_min = minimum - padding
    y_max = maximum + padding
    y_span = y_max - y_min

    def x_for(day_value: date) -> float:
        return left + ((day_value - domain_start).days / domain_span) * plot_width

    def y_for(number: float) -> float:
        return top + ((y_max - number) / y_span) * plot_height

    parts: List[str] = [
        '<svg class="price-chart" viewBox="0 0 1100 450" role="img" aria-labelledby="chart-title chart-desc">',
        '<title id="chart-title">' + _escaped(title) + "</title>",
        '<desc id="chart-desc">' + _escaped(description) + "</desc>",
        '<rect class="chart-bg" x="0" y="0" width="1100" height="450" rx="16" />',
        '<text class="axis-title" x="' + _svg_number(left) + '" y="18">원문 가격 (value_text)</text>',
    ]

    for index in range(5):
        y = top + plot_height * index / 4
        value = y_max - y_span * index / 4
        parts.append(
            '<line class="grid-line" x1="'
            + _svg_number(left)
            + '" x2="'
            + _svg_number(width - right)
            + '" y1="'
            + _svg_number(y)
            + '" y2="'
            + _svg_number(y)
            + '" />'
        )
        parts.append(
            '<text class="y-label" x="'
            + _svg_number(left - 12)
            + '" y="'
            + _svg_number(y + 4)
            + '" text-anchor="end">'
            + _escaped(_axis_label(value))
            + "</text>"
        )

    tick_values = _tick_dates(domain_start, domain_end)
    for tick_index, tick in enumerate(tick_values):
        x = x_for(tick)
        text_anchor = (
            "start"
            if tick_index == 0
            else "end"
            if tick_index == len(tick_values) - 1
            else "middle"
        )
        parts.append(
            '<line class="x-tick" x1="'
            + _svg_number(x)
            + '" x2="'
            + _svg_number(x)
            + '" y1="'
            + _svg_number(top + plot_height)
            + '" y2="'
            + _svg_number(top + plot_height + 6)
            + '" />'
        )
        parts.append(
            '<text class="x-label" x="'
            + _svg_number(x)
            + '" y="'
            + _svg_number(top + plot_height + 25)
            + '" text-anchor="' + text_anchor + '">'
            + _escaped(tick.isoformat())
            + "</text>"
        )
    parts.append(
        '<text class="axis-caption" x="'
        + _svg_number(left + plot_width / 2)
        + '" y="'
        + _svg_number(height - 14)
        + '" text-anchor="middle">관측일 (원문 날짜의 정확한 일자만 배치)</text>'
    )

    # A line is eligible only when both dates have an official middle-session value
    # and the dates are exactly one calendar day apart. Community points never enter
    # this list, and a missing date therefore remains visibly unconnected.
    by_date: Dict[date, List[Mapping[str, Any]]] = {}
    for point in points:
        if point["kind"] == "official" and point["session"] == "middle":
            by_date.setdefault(point["date"], []).append(point)
    official_by_day: Dict[date, Mapping[str, Any]] = {}
    for day_value, day_points in by_date.items():
        official_by_day[day_value] = sorted(
            day_points,
            key=lambda point: (_text(point["key"]), _text(point["raw_value"])),
        )[0]
    sorted_days = sorted(official_by_day)
    for first_day, second_day in zip(sorted_days, sorted_days[1:]):
        if (second_day - first_day).days != 1:
            continue
        first = official_by_day[first_day]
        second = official_by_day[second_day]
        parts.append(
            '<line class="official-line" x1="'
            + _svg_number(x_for(first["date"]))
            + '" y1="'
            + _svg_number(y_for(first["number"]))
            + '" x2="'
            + _svg_number(x_for(second["date"]))
            + '" y2="'
            + _svg_number(y_for(second["number"]))
            + '" />'
        )

    for index, point in enumerate(points):
        x = x_for(point["date"])
        y = y_for(point["number"])
        source_label = "공식" if point["kind"] == "official" else "커뮤니티"
        point_title = (
            source_label
            + " | "
            + point["date"].isoformat()
            + " | 원문 가격 "
            + _text(point["raw_value"])
        )
        title_markup = "<title>" + _escaped(point_title) + "</title>"
        if point["kind"] == "official":
            shape = (
                '<circle class="official-point" cx="'
                + _svg_number(x)
                + '" cy="'
                + _svg_number(y)
                + '" r="6">'
                + title_markup
                + "</circle>"
            )
        else:
            size = 7
            shape = (
                '<path class="community-point" d="M '
                + _svg_number(x)
                + " "
                + _svg_number(y - size)
                + " L "
                + _svg_number(x + size)
                + " "
                + _svg_number(y)
                + " L "
                + _svg_number(x)
                + " "
                + _svg_number(y + size)
                + " L "
                + _svg_number(x - size)
                + " "
                + _svg_number(y)
                + ' Z">'
                + title_markup
                + "</path>"
            )
        parts.append('<g aria-label="' + _escaped(point_title, quote=True) + '">' + shape + "</g>")

    parts.append("</svg>")
    return "".join(parts)


def _latest_official_update(rows: Iterable[Mapping[str, Any]]) -> str:
    candidates: List[Tuple[Tuple[int, str, str], str]] = []
    for row in rows:
        if _text(row.get("source_kind")) != "official":
            continue
        raw = _text(row.get("source_updated_at"))
        if not raw:
            continue
        parsed: Optional[datetime]
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is None:
            key = (0, "", raw)
        else:
            key = (1, parsed.isoformat(), raw)
        candidates.append((key, raw))
    if not candidates:
        return "공식 원문 갱신 시각 미기록"
    return max(candidates, key=lambda item: item[0])[1]


def _latest_fetched_at(rows: Iterable[Mapping[str, Any]]) -> str:
    candidates = [_text(row.get("fetched_at")) for row in rows if _text(row.get("fetched_at"))]
    return max(candidates) if candidates else "가져온 시각 미기록"


def _render_html(rows: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(rows, key=_render_sort_key)
    exact_rows = [row for row in ordered if _is_exact_date(row)]
    approximate_rows = [row for row in ordered if not _is_exact_date(row)]
    points: List[Dict[str, Any]] = []
    for row in exact_rows:
        day_value = _iso_date(row.get("observed_date"))
        parsed = _decimal(row.get("value_text"))
        number = _float_decimal(parsed) if parsed is not None else None
        if day_value is None or parsed is None or number is None:
            continue
        points.append(
            {
                "date": day_value,
                "decimal": parsed,
                "number": number,
                "raw_value": _text(row.get("value_text")),
                "kind": _text(row.get("source_kind")),
                "session": _text(row.get("session")),
                "key": _text(row.get("observation_key")),
            }
        )

    official_count = sum(1 for row in rows if _text(row.get("source_kind")) == "official")
    community_count = sum(1 for row in rows if _text(row.get("source_kind")) == "community")
    latest_update = _latest_official_update(rows)
    latest_fetched = _latest_fetched_at(rows)
    chart_note = (
        "정확한 날짜와 숫자 가격이 있는 관측만 점으로 표시합니다."
        if points
        else "정확한 날짜와 숫자 가격이 있는 관측이 없어 점을 표시하지 않았습니다."
    )

    return """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DRAM 가격 기록</title>
<style>
:root{color-scheme:light;--ink:#1f2937;--muted:#64748b;--line:#dbe3ee;--paper:#fff;--wash:#f3f7fb;--blue:#1769aa;--blue-soft:#d9edff;--orange:#c56a1b;--orange-soft:#fff0dc;--shadow:0 12px 34px rgba(30,64,100,.09)}
*{box-sizing:border-box}html{background:var(--wash)}body{margin:0;color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}main{max-width:1240px;margin:0 auto;padding:30px 20px 64px}.hero,.card,.panel{background:var(--paper);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}.hero{padding:34px 38px;margin-bottom:20px;background:linear-gradient(135deg,#fff 0%,#f7fbff 100%)}.kicker{color:var(--blue);font-size:13px;font-weight:700;letter-spacing:.08em;margin:0 0 6px}.hero h1{font-size:clamp(28px,4vw,44px);line-height:1.2;margin:0 0 12px}.hero p{margin:6px 0}.product{font-weight:700}.notice{margin-top:20px;padding:13px 16px;border-left:4px solid var(--orange);background:#fff8ef;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:20px 0}.card{padding:18px 20px}.card .label{color:var(--muted);font-size:13px;margin:0 0 5px}.card .value{font-size:17px;font-weight:700;margin:0;overflow-wrap:anywhere}.panel{padding:26px 28px;margin:20px 0}.panel h2{font-size:23px;line-height:1.3;margin:0 0 8px}.panel h3{font-size:18px;margin:24px 0 5px}.section-intro{color:var(--muted);margin:0 0 18px}.unit-note{background:#f7fafc;border:1px solid var(--line);padding:15px 17px;border-radius:10px}.chart-wrap{border:1px solid var(--line);border-radius:14px;padding:10px;overflow:hidden;background:#fbfdff}.price-chart{display:block;width:100%;height:auto;min-height:260px}.chart-bg{fill:#fbfdff}.chart-empty{fill:#f3f7fb;stroke:#dbe3ee}.chart-empty-text{fill:#64748b;font-size:16px}.axis-title,.axis-caption,.y-label,.x-label{fill:#64748b;font-size:12px}.axis-caption{font-size:13px}.grid-line{stroke:#dbe3ee;stroke-width:1}.x-tick{stroke:#94a3b8;stroke-width:1}.official-line{stroke:#1769aa;stroke-width:3;fill:none;stroke-linecap:round}.official-point{fill:#1769aa;stroke:#fff;stroke-width:2}.community-point{fill:#c56a1b;stroke:#fff;stroke-width:2}.legend{display:flex;flex-wrap:wrap;gap:14px 24px;margin:13px 0 0;color:#475569}.legend-item{display:inline-flex;align-items:center;gap:7px}.legend-circle{width:12px;height:12px;border-radius:50%;background:var(--blue);display:inline-block;border:2px solid #fff;box-shadow:0 0 0 1px var(--blue)}.legend-diamond{width:11px;height:11px;background:var(--orange);display:inline-block;transform:rotate(45deg);border:1px solid #fff;box-shadow:0 0 0 1px var(--orange)}.legend-line{width:23px;height:0;border-top:3px solid var(--blue);display:inline-block}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;min-width:1220px;border-collapse:collapse;background:#fff;font-size:13px}caption{text-align:left;padding:13px 15px;background:#f7fafc;font-weight:700;border-bottom:1px solid var(--line)}th,td{padding:10px 11px;text-align:left;vertical-align:top;border-bottom:1px solid #e8eef5}th{white-space:nowrap;background:#f7fafc;color:#475569;font-size:12px}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#f8fbff}td code{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}a{color:#125a96;text-decoration:underline;text-underline-offset:2px;overflow-wrap:anywhere}.kind{font-weight:700}.kind-official{color:var(--blue)}.kind-community{color:var(--orange)}.muted,.empty{color:var(--muted)}small{color:var(--muted);white-space:nowrap}.empty{padding:18px 0;margin:0}.rule-list{margin:8px 0 0;padding-left:22px}.footer{color:var(--muted);font-size:13px;margin-top:25px}@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.hero{padding:27px 24px}.panel{padding:22px 18px}}@media(max-width:520px){main{padding:14px 10px 38px}.grid{grid-template-columns:1fr}.hero{padding:23px 18px;border-radius:14px}.panel{border-radius:14px}.chart-wrap{padding:3px}.price-chart{min-height:210px}.legend{gap:11px 16px}}
</style>
</head>
<body>
<main>
<header class="hero">
<p class="kicker">개인 가격 기록 · 오프라인 보고서</p>
<h1>DRAM 가격 기록</h1>
<p class="product">대상 품목: <strong>DDR5 16Gb (2Gx8) 4800/5600</strong></p>
<p>원문에서 확인한 가격을 날짜·세션·출처와 함께 보존한 개인용 기록입니다.</p>
<div class="notice">이 자료는 가격과 출처를 정리한 기록이며 매수·매도·투자 조언, 수익 보장 또는 투자 신호가 아닙니다. 숫자는 원문 표기 그대로 보존하며 통화 단위도 원문을 따릅니다.</div>
</header>
<section class="grid" aria-label="기록 요약">
<div class="card"><p class="label">공식 원문 마지막 갱신</p><p class="value">__LATEST_UPDATE__</p></div>
<div class="card"><p class="label">관측 수</p><p class="value">__OBSERVATION_COUNT__건</p></div>
<div class="card"><p class="label">정확한 날짜 그래프 점</p><p class="value">__POINT_COUNT__개</p></div>
<div class="card"><p class="label">마지막 가져온 시각</p><p class="value">__LATEST_FETCHED__</p></div>
</section>
<section class="panel" aria-labelledby="unit-heading">
<h2 id="unit-heading">단위와 기록 범위</h2>
<div class="unit-note"><strong>Gb와 GB를 혼동하지 마세요.</strong> 16Gb는 기가비트(gigabit)이고, 8비트가 1바이트이므로 16Gb 칩의 용량은 약 2GB(기가바이트)입니다. 이 보고서는 제품 식별자의 <strong>16Gb</strong>를 임의로 GB로 바꾸지 않으며, 데이터베이스의 <code>value_text</code> 원문 가격을 그대로 보여줍니다.</div>
<p class="section-intro">공식 __OFFICIAL_COUNT__건 · 커뮤니티 __COMMUNITY_COUNT__건 · 전체 __OBSERVATION_COUNT__건. 아래 표는 저장된 계약 필드를 빠짐없이 보여줍니다.</p>
</section>
<section class="panel" aria-labelledby="chart-heading">
<h2 id="chart-heading">정확한 날짜 가격 차트</h2>
<p class="section-intro">__CHART_NOTE__ 날짜가 불연속이면 선을 생략합니다. 선은 <strong>공식 출처의 middle 세션</strong>이며, 서로 정확히 하루 차이인 관측끼리만 연결합니다.</p>
<div class="chart-wrap">__SVG__</div>
<div class="legend" aria-label="차트 범례"><span class="legend-item"><i class="legend-circle" aria-hidden="true"></i>공식 관측</span><span class="legend-item"><i class="legend-diamond" aria-hidden="true"></i>커뮤니티 관측</span><span class="legend-item"><i class="legend-line" aria-hidden="true"></i>연속된 공식 middle 세션</span></div>
</section>
<section class="panel" aria-labelledby="exact-heading">
<h2 id="exact-heading">정확한 날짜 관측</h2>
<p class="section-intro">그래프에 배치할 수 있는 날짜 정밀도가 <code>day</code>인 자료입니다. 원문 가격이 숫자로 해석되지 않는 행도 출처 표에 그대로 남깁니다.</p>
__EXACT_TABLE__
</section>
<section class="panel" aria-labelledby="approx-heading">
<h2 id="approx-heading">날짜가 불명확한 관측 — 그래프에 배치하지 않음</h2>
<p class="section-intro">대략적인 날짜나 날짜가 없는 자료는 임의의 x좌표를 만들지 않고 이 구역에 따로 표시합니다.</p>
__APPROX_TABLE__
</section>
<p class="footer">이 페이지는 외부 스타일시트·스크립트·이미지를 사용하지 않는 단일 HTML 파일입니다. 링크를 클릭할 때만 원문 사이트로 이동합니다.</p>
</main>
</body>
</html>
""".replace("__LATEST_UPDATE__", _escaped(latest_update)).replace(
        "__OBSERVATION_COUNT__", str(len(rows))
    ).replace("__POINT_COUNT__", str(len(points))).replace("__LATEST_FETCHED__", _escaped(latest_fetched)).replace(
        "__OFFICIAL_COUNT__", str(official_count)
    ).replace("__COMMUNITY_COUNT__", str(community_count)).replace("__CHART_NOTE__", _escaped(chart_note)).replace(
        "__SVG__", _chart_svg(points)
    ).replace("__EXACT_TABLE__", _observation_table(exact_rows, "exact-observations", "정확한 날짜 관측 원자료")).replace(
        "__APPROX_TABLE__", _observation_table(approximate_rows, "approximate-observations", "날짜가 불명확한 관측 원자료")
    )


def _selected_public_rows(
    conn: Any, source_kinds: Sequence[str]
) -> List[Dict[str, Any]]:
    if isinstance(source_kinds, (str, bytes)):
        raise ValueError("source_kinds must be a sequence of source group names")
    selected = tuple(source_kinds)
    invalid = set(selected).difference(_PUBLIC_SOURCE_KINDS)
    if invalid:
        raise ValueError("unsupported public source kind(s): %s" % ", ".join(sorted(invalid)))
    allowed = set(selected)
    return [row for row in _rows(conn) if _text(row.get("source_kind")) in allowed]


def _public_canonical_rows(rows: Iterable[Mapping[str, Any]]) -> List[List[str]]:
    canonical = [
        [_text(row.get(field)) for field in PUBLIC_OBSERVATION_FIELDS]
        for row in rows
    ]
    canonical.sort()
    return canonical


def _public_digest_for_rows(rows: Iterable[Mapping[str, Any]]) -> str:
    payload = {
        "template_revision": _PUBLIC_TEMPLATE_REVISION,
        "fields": list(PUBLIC_OBSERVATION_FIELDS),
        "observations": _public_canonical_rows(rows),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def public_digest(
    conn: Any, *, source_kinds: Sequence[str] = ("official", "community")
) -> str:
    """Return the stable digest of fields authorized for the public report.

    Acquisition metadata, internal keys and evidence notes are deliberately
    excluded.  The public template revision is included so a material display
    contract change causes publication even when observations are unchanged.
    """

    return _public_digest_for_rows(_selected_public_rows(conn, source_kinds))


_PUBLIC_TABLE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("product", "제품"),
    ("source_kind", "출처 종류"),
    ("source_url", "출처"),
    ("date_text", "원문 날짜"),
    ("observed_date", "관측일"),
    ("date_precision", "날짜 정밀도"),
    ("source_updated_at", "출처 관측 시각"),
    ("value_text", "원문 가격"),
    ("value_qualifier", "값 상태"),
    ("session", "세션"),
)


def _public_source_markup(value: Any) -> str:
    safe = _safe_http_url(value)
    if safe is None:
        return '<span class="muted">공개 가능한 출처 링크 없음</span>'
    return (
        '<a href="'
        + _escaped(safe, quote=True)
        + '" target="_blank" rel="noopener noreferrer">원문 보기</a>'
    )


def _public_field_markup(field: str, value: Any) -> str:
    if field == "source_url":
        return _public_source_markup(value)
    if field == "source_kind":
        return _kind_markup(value)
    if field == "date_precision":
        return _precision_markup(value)
    if field == "value_qualifier":
        return _qualifier_markup(value)
    if field == "value_text":
        return _display(value, code=True)
    return _display(value)


def _public_table(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">선택한 공개 출처 그룹에 해당하는 관측이 없습니다.</p>'
    head = "".join(
        '<th scope="col">' + _escaped(label) + "</th>"
        for _, label in _PUBLIC_TABLE_COLUMNS
    )
    body: List[str] = []
    for row in rows:
        cells = "".join(
            "<td>" + _public_field_markup(field, row.get(field)) + "</td>"
            for field, _ in _PUBLIC_TABLE_COLUMNS
        )
        body.append("<tr>" + cells + "</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _public_points(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_exact_date(row):
            continue
        day_value = _iso_date(row.get("observed_date"))
        parsed = _decimal(row.get("value_text"))
        number = _float_decimal(parsed) if parsed is not None else None
        if day_value is None or parsed is None or number is None:
            continue
        public_key = "\x1f".join(
            _text(row.get(field)) for field in PUBLIC_OBSERVATION_FIELDS
        )
        points.append(
            {
                "date": day_value,
                "decimal": parsed,
                "number": number,
                "raw_value": _text(row.get("value_text")),
                "kind": _text(row.get("source_kind")),
                "session": _text(row.get("session")),
                "key": public_key,
            }
        )
    return points


def _latest_public_official_timestamp(
    rows: Iterable[Mapping[str, Any]]
) -> Optional[str]:
    candidates: List[Tuple[datetime, str]] = []
    for row in rows:
        if _text(row.get("source_kind")) != "official":
            continue
        raw = _text(row.get("source_updated_at"))
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                continue
            candidates.append((parsed.astimezone(timezone.utc), raw))
        except (TypeError, ValueError, OverflowError):
            continue
    return max(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else None


def _generated_timestamp(value: Any) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("generated_at must be an ISO timestamp") from exc
    else:
        raise TypeError("generated_at must be a datetime, ISO timestamp, or None")
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _render_public_html(
    rows: Sequence[Mapping[str, Any]], generated_at: Any
) -> str:
    ordered = sorted(
        rows,
        key=lambda row: (
            not _is_exact_date(row),
            _text(row.get("observed_date")),
            tuple(_text(row.get(field)) for field in PUBLIC_OBSERVATION_FIELDS),
        ),
    )
    points = _public_points(ordered)
    digest = _public_digest_for_rows(ordered)
    generated = _generated_timestamp(generated_at)
    latest_official = _latest_public_official_timestamp(ordered)
    latest_value = latest_official or ""
    latest_absolute = latest_official or "공식 출처 관측 시각 없음"
    official_state = (
        "공식 출처의 마지막 관측을 기준으로 합니다."
        if latest_official
        else "선택한 공개 자료에 유효한 공식 출처 관측 시각이 없습니다."
    )
    exact_rows = [row for row in ordered if _is_exact_date(row)]
    approximate_rows = [row for row in ordered if not _is_exact_date(row)]

    template = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="dram-data-digest" content="@@DIGEST@@">
<meta name="dram-generated-at" content="@@GENERATED@@">
<meta name="dram-source-updated-at" content="@@LATEST_META@@">
<title>DRAM 가격 관측</title>
<style>
:root{color-scheme:light;--ink:#172033;--muted:#607089;--line:#d9e1ec;--paper:#fff;--wash:#f4f7fb;--blue:#1769aa;--orange:#b85f16;--shadow:0 10px 28px rgba(30,55,90,.08)}
*{box-sizing:border-box}html{background:var(--wash)}body{margin:0;color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}main{max-width:1200px;margin:auto;padding:24px 16px 56px}.hero,.panel,.card{background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.hero{padding:28px 32px}.hero h1{font-size:clamp(27px,5vw,42px);line-height:1.2;margin:0 0 10px}.hero p{margin:7px 0}.meta-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:16px 0}.card{padding:17px}.label{color:var(--muted);font-size:13px;margin:0 0 5px}.value{font-weight:700;margin:0;overflow-wrap:anywhere}.panel{margin-top:16px;padding:22px}.panel h2{margin:0 0 8px;font-size:21px}.section-intro{color:var(--muted);margin:0 0 16px}.chart-wrap{overflow-x:auto}.price-chart{display:block;width:100%;min-width:650px;height:auto}.chart-bg{fill:#fbfdff;stroke:var(--line)}.chart-empty{fill:#f7f9fc;stroke:var(--line)}.chart-empty-text,.axis-caption,.axis-title,.x-label,.y-label{fill:var(--muted)}.chart-empty-text{font-size:15px}.axis-title,.axis-caption{font-size:12px}.x-label,.y-label{font-size:11px}.grid-line{stroke:#dfe6ef;stroke-width:1}.x-tick{stroke:#8795a8}.official-line{stroke:var(--blue);stroke-width:3}.official-point{fill:var(--blue);stroke:#fff;stroke-width:2}.community-point{fill:var(--orange);stroke:#fff;stroke-width:2}.legend{display:flex;flex-wrap:wrap;gap:15px;margin-top:10px;color:var(--muted)}.legend i{display:inline-block;margin-right:6px;vertical-align:middle}.legend-circle{width:11px;height:11px;border-radius:50%;background:var(--blue)}.legend-diamond{width:10px;height:10px;background:var(--orange);transform:rotate(45deg)}.legend-line{width:24px;border-top:3px solid var(--blue)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{border-collapse:collapse;width:100%;min-width:1040px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f6f9fc;white-space:nowrap}td{overflow-wrap:anywhere}a{color:#075c9c}.muted,.empty{color:var(--muted)}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.footer{color:var(--muted);font-size:13px;margin:18px 4px}
@media(max-width:680px){main{padding:12px 10px 40px}.hero{padding:22px 18px}.panel{padding:17px 14px}.meta-grid{grid-template-columns:1fr}.price-chart{min-width:600px}}
</style>
</head>
<body>
<main>
<header class="hero">
<h1>DRAM 가격 관측</h1>
<p>선택한 출처의 가격 관측 기록입니다. 숫자와 단위는 원문 표기를 따릅니다.</p>
<p><strong>대상 품목: @@PRODUCT_LABEL@@</strong></p>
<p>제품명 원문의 16Gb는 칩의 비트 용량으로 약 2GB이며, 16GB 메모리 모듈을 뜻하지 않습니다.</p>
<p>가격 기록은 투자 조언이나 거래 신호가 아닙니다.</p>
</header>
<section class="meta-grid" aria-label="페이지 및 자료 시각">
<div class="card"><p class="label">공식 출처 관측 시각</p><p class="value"><time id="source-time" data-source-updated-at="@@LATEST_DATA@@">@@LATEST_TEXT@@</time></p><p id="freshness" class="label" aria-live="polite">@@OFFICIAL_STATE@@</p><noscript><p class="label">절대 시각: @@LATEST_NOSCRIPT@@</p></noscript></div>
<div class="card"><p class="label">페이지 생성 시각</p><p class="value"><time datetime="@@GENERATED_TIME@@">@@GENERATED_TEXT@@</time></p></div>
</section>
<section class="panel" aria-labelledby="chart-heading">
<h2 id="chart-heading">날짜별 가격</h2>
<p class="section-intro">정확한 날짜의 숫자 가격만 배치하며, 날짜 사이의 공백과 대략적인 날짜는 임의로 메우지 않습니다.</p>
<div class="chart-wrap">@@SVG@@</div>
<div class="legend" aria-label="차트 범례"><span><i class="legend-circle" aria-hidden="true"></i>공식</span><span><i class="legend-diamond" aria-hidden="true"></i>커뮤니티</span><span><i class="legend-line" aria-hidden="true"></i>하루 간격의 공식 middle 세션</span></div>
</section>
<section class="panel" aria-labelledby="exact-heading"><h2 id="exact-heading">정확한 날짜 관측</h2>@@EXACT_TABLE@@</section>
<section class="panel" aria-labelledby="approx-heading"><h2 id="approx-heading">대략적인 날짜 관측</h2><p class="section-intro">날짜가 불명확한 자료는 차트에 임의 배치하지 않습니다.</p>@@APPROX_TABLE@@</section>
<p class="footer">외부 스크립트·스타일시트·이미지 없이 이 파일만으로 표시됩니다.</p>
</main>
<script>
(function(){
  "use strict";
  var time=document.getElementById("source-time");
  var output=document.getElementById("freshness");
  function updateFreshness(){
    var raw=time.getAttribute("data-source-updated-at");
    if(!raw){output.textContent="유효한 공식 출처 관측 시각이 없습니다.";return;}
    var observed=new Date(raw);
    if(Number.isNaN(observed.getTime())){output.textContent="공식 출처 관측 시각을 해석할 수 없습니다.";return;}
    var age=Date.now()-observed.getTime();
    if(age < -60000){output.textContent="공식 출처 관측 시각이 현재 기기 시각보다 미래입니다.";return;}
    if(age < 0){age=0;}
    var hours=Math.floor(age/3600000);
    if(hours>48){output.textContent="이 공식 출처 관측은 "+hours+"시간 전으로 오래되었습니다.";return;}
    if(hours>=1){output.textContent="이 공식 출처 관측은 "+hours+"시간 전입니다.";return;}
    output.textContent="이 공식 출처 관측은 "+Math.floor(age/60000)+"분 전입니다.";
  }
  updateFreshness();
  window.setInterval(updateFreshness,60000);
}());
</script>
</body>
</html>
"""
    replacements = {
        "DIGEST": digest,
        "PRODUCT_LABEL": _escaped(PRODUCT_LABEL),
        "GENERATED": _escaped(generated, quote=True),
        "LATEST_META": _escaped(latest_value, quote=True),
        "LATEST_DATA": _escaped(latest_value, quote=True),
        "LATEST_TEXT": _escaped(latest_absolute),
        "OFFICIAL_STATE": _escaped(official_state),
        "LATEST_NOSCRIPT": _escaped(latest_absolute),
        "GENERATED_TIME": _escaped(generated, quote=True),
        "GENERATED_TEXT": _escaped(generated),
        "SVG": _chart_svg(points),
        "EXACT_TABLE": _public_table(exact_rows),
        "APPROX_TABLE": _public_table(approximate_rows),
    }
    return re.sub(
        r"@@([A-Z_]+)@@", lambda match: replacements[match.group(1)], template
    )


def render_public(
    conn: Any,
    output_path: Path,
    *,
    source_kinds: Sequence[str] = ("official", "community"),
    generated_at: Any = None,
) -> Path:
    """Render authorized source groups as a self-contained public HTML page."""

    rows = _selected_public_rows(conn, source_kinds)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_render_public_html(rows, generated_at))
    return path


def render(conn: Any, output_path: Path) -> Path:
    """Render all observations to a self-contained, offline Korean HTML report."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_render_html(_rows(conn)))
    return path


def _csv_field(field: str, value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    # Keep a genuine decimal exactly intact, including a leading minus sign. A
    # malformed value that resembles a spreadsheet formula is protected instead.
    if field == "value_text" and _DECIMAL_RE.fullmatch(raw.strip()):
        return raw
    if _FORMULA_PREFIX_RE.match(raw):
        return "'" + raw
    return raw


def export_csv(conn: Any, output_path: Path) -> Path:
    """Export every observation field in a stable order without changing decimals."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(_rows(conn), key=_csv_sort_key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OBSERVATION_FIELDS)
        for row in rows:
            writer.writerow([_csv_field(field, row.get(field)) for field in OBSERVATION_FIELDS])
    return path


__all__ = [
    "OBSERVATION_FIELDS",
    "PUBLIC_OBSERVATION_FIELDS",
    "export_csv",
    "public_digest",
    "render",
    "render_public",
]
