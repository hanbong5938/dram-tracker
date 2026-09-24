"""Focused regressions for the public chart publication boundary."""

from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import chart
import store


_GENERATED_AT = datetime(2026, 9, 24, 6, 45, tzinfo=timezone.utc)


def _observation(kind: str, marker: str, **changes: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "observation_key": "private-key-" + marker,
        "product": store.PRODUCT,
        "source_kind": kind,
        "source_url": "https://example.test/source/" + marker,
        "date_text": "2026-09-23",
        "observed_date": "2026-09-23",
        "date_precision": "day",
        "source_updated_at": "2026-09-23T14:40:00+08:00",
        "fetched_at": "2026-09-23T07:00:00Z",
        "value_text": "57.167",
        "value_qualifier": "reported",
        "session": "middle",
        "evidence_note": "private-note-" + marker,
    }
    row.update(changes)
    return row


def _connection(*rows: Dict[str, Any]):
    connection = store.connect(":memory:")
    for row in rows:
        store.save_observation(connection, row)
    return connection


def _html_digest(document: str) -> str:
    matches = re.findall(
        r'<meta name="dram-data-digest" content="([0-9a-f]{64})">', document
    )
    if len(matches) != 1:
        raise AssertionError("public document must contain exactly one data digest")
    return matches[0]


class PublicChartTests(unittest.TestCase):
    def test_public_render_does_not_leak_private_fields_and_private_render_still_has_them(self) -> None:
        row = _observation(
            "official",
            "official-visible",
            observation_key="SECRET-OBSERVATION-KEY",
            fetched_at="2099-01-02T03:04:05Z",
            evidence_note="SECRET INTERNAL EVIDENCE /Users/private/archive.csv",
        )
        connection = _connection(row)
        try:
            with tempfile.TemporaryDirectory() as directory:
                public_path = Path(directory) / "public.html"
                private_path = Path(directory) / "private.html"
                chart.render_public(connection, public_path, generated_at=_GENERATED_AT)
                chart.render(connection, private_path)
                public_html = public_path.read_text(encoding="utf-8")
                private_html = private_path.read_text(encoding="utf-8")
        finally:
            connection.close()

        for private_marker in (
            "SECRET-OBSERVATION-KEY",
            "2099-01-02T03:04:05Z",
            "SECRET INTERNAL EVIDENCE",
            "/Users/private/archive.csv",
        ):
            self.assertNotIn(private_marker, public_html)
            self.assertIn(private_marker, private_html)
        self.assertNotIn("observation_key", public_html)
        self.assertNotIn("evidence_note", public_html)
        self.assertNotIn("fetched_at", public_html)
        self.assertNotIn(".csv", public_html.lower())
        _html_digest(public_html)

    def test_digest_ignores_acquisition_metadata_and_generation_time_but_tracks_public_data(self) -> None:
        first = _connection(_observation("official", "same"))
        second = _connection(
            _observation(
                "official",
                "same",
                observation_key="different-private-key",
                fetched_at="2026-09-24T12:34:56Z",
                evidence_note="different private evidence",
            )
        )
        changed = _connection(_observation("official", "same", value_text="58.000"))
        try:
            first_digest = chart.public_digest(first)
            self.assertEqual(first_digest, chart.public_digest(second))
            self.assertNotEqual(first_digest, chart.public_digest(changed))
            with tempfile.TemporaryDirectory() as directory:
                first_path = Path(directory) / "first.html"
                second_path = Path(directory) / "second.html"
                chart.render_public(first, first_path, generated_at="2026-09-24T00:00:00Z")
                chart.render_public(second, second_path, generated_at="2026-09-25T00:00:00Z")
                self.assertEqual(
                    _html_digest(first_path.read_text(encoding="utf-8")),
                    _html_digest(second_path.read_text(encoding="utf-8")),
                )
        finally:
            first.close()
            second.close()
            changed.close()

    def test_source_group_selection_is_shared_by_digest_chart_table_and_freshness(self) -> None:
        official = _observation(
            "official",
            "OFFICIAL-GROUP-MARKER",
            source_updated_at="2026-09-23T14:40:00+08:00",
            value_text="57.100",
        )
        community = _observation(
            "community",
            "COMMUNITY-GROUP-MARKER",
            observed_date="2026-09-22",
            date_text="2026-09-22",
            source_updated_at="2026-09-24T18:00:00+08:00",
            value_text="56.200",
            session=None,
        )
        connection = _connection(official, community)
        official_digest = chart.public_digest(connection, source_kinds=("official",))
        community_digest = chart.public_digest(connection, source_kinds=("community",))
        try:
            with tempfile.TemporaryDirectory() as directory:
                official_path = Path(directory) / "official.html"
                community_path = Path(directory) / "community.html"
                chart.render_public(
                    connection,
                    official_path,
                    source_kinds=("official",),
                    generated_at=_GENERATED_AT,
                )
                chart.render_public(
                    connection,
                    community_path,
                    source_kinds=("community",),
                    generated_at=_GENERATED_AT,
                )
                official_html = official_path.read_text(encoding="utf-8")
                community_html = community_path.read_text(encoding="utf-8")
        finally:
            connection.close()

        self.assertIn("OFFICIAL-GROUP-MARKER", official_html)
        self.assertNotIn("COMMUNITY-GROUP-MARKER", official_html)
        self.assertIn("COMMUNITY-GROUP-MARKER", community_html)
        self.assertNotIn("OFFICIAL-GROUP-MARKER", community_html)
        official_svg = official_html[
            official_html.index("<svg") : official_html.index("</svg>") + len("</svg>")
        ]
        community_svg = community_html[
            community_html.index("<svg") : community_html.index("</svg>") + len("</svg>")
        ]
        self.assertIn("57.100", official_svg)
        self.assertNotIn("56.200", official_svg)
        self.assertIn("56.200", community_svg)
        self.assertNotIn("57.100", community_svg)
        self.assertIn('data-source-updated-at="2026-09-23T14:40:00+08:00"', official_html)
        self.assertIn('data-source-updated-at=""', community_html)
        self.assertEqual(_html_digest(official_html), official_digest)
        self.assertEqual(_html_digest(community_html), community_digest)


if __name__ == "__main__":
    unittest.main()
