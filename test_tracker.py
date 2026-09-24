"""Regression tests for the DRAM tracker parser and persistence boundary."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from typing import Dict, Iterable, List

import collect
import history
import store


# This keeps the source's target timestamp/table relationship, including the
# void elements which occur in the live markup.  The unrelated table and
# timestamp ensure the parser does not merely select the first plausible text.
HTML_FIXTURE = dedent(
    """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <link rel="stylesheet" href="/site.css" />
      </head>
      <body>
        <table id="unrelated-table">
          <tbody>
            <tr><td class="updated">Last Update: Sep 20 2026 14:40 (GMT+8)</td></tr>
          </tbody>
        </table>
        <div class="unrelated-update">Last Update: Sep 20 2026 14:40 (GMT+8)</div>

        <table id="target-update">
          <tbody>
            <tr>
              <td id="NationalDramSpotPrice_show_day">Last Update: Sep. 21 2026 14:40 (GMT+8)</td>
            </tr>
          </tbody>
        </table>

        <table id="national-dram">
          <tbody id="tb_NationalDramSpotPrice">
            <tr>
              <th>Item</th>
              <th>Daily High</th>
              <th>Daily Low</th>
              <th>Session High</th>
              <th>Session Low</th>
              <th>Session Average</th>
              <th>Session Change</th>
              <th>History</th>
            </tr>
            <tr>
              <td>DDR5 16Gb (2Gx8) 4800/5600</td>
              <td><img src="/chip.png">57.500<br></td>
              <td>56.800<br /></td>
              <td><input type="hidden" value="chip" />57.300</td>
              <td>56.900<img src="/chip.svg" /></td>
              <td>57.167<br/> </td>
              <td>+1.2 %</td>
              <td><img src="/history.png" /></td>
            </tr>
          </tbody>
        </table>
      </body>
    </html>
    """
).strip()

DUPLICATE_PRODUCT_ROW = dedent(
    """
    <tr>
      <td>DDR5 16Gb (2Gx8) 4800/5600</td>
      <td>57.500</td><td>56.800</td><td>57.300</td>
      <td>56.900</td><td>57.167</td><td>+1.2 %</td><td>history</td>
    </tr>
    """
).strip()


def _with_duplicate_product(html: str) -> str:
    target_start = html.index('<tbody id="tb_NationalDramSpotPrice">')
    target_end = html.index("</tbody>", target_start)
    return html[:target_end] + DUPLICATE_PRODUCT_ROW + html[target_end:]


def _seed_rows() -> List[Dict[str, str]]:
    with Path(__file__).with_name("seed.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Iterable[Dict[str, str]]) -> None:
    rows = list(rows)
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class ParsePageTests(unittest.TestCase):
    def test_target_table_handles_void_elements_and_distractions(self) -> None:
        observation = collect.parse_page(HTML_FIXTURE, "2026-09-21")

        self.assertEqual(observation["value_text"], "57.167")
        self.assertEqual(observation["observed_date"], "2026-09-21")
        self.assertEqual(observation["source_updated_at"], "2026-09-21T14:40:00+08:00")
        self.assertEqual(observation["session"], "middle")

    def test_wrong_date_and_session_are_rejected(self) -> None:
        with self.subTest("source date differs from expected date"):
            with self.assertRaises(ValueError):
                collect.parse_page(HTML_FIXTURE, "2026-09-20")

        with self.subTest("timestamp is not the middle session"):
            wrong_session = HTML_FIXTURE.replace(
                "Sep. 21 2026 14:40", "Sep. 21 2026 13:40", 1
            )
            with self.assertRaises(ValueError):
                collect.parse_page(wrong_session, "2026-09-21")

    def test_duplicate_product_missing_target_and_invalid_prices_are_rejected(self) -> None:
        cases = {
            "duplicate product": _with_duplicate_product(HTML_FIXTURE),
            "missing target tbody": HTML_FIXTURE.replace(
                'id="tb_NationalDramSpotPrice"', 'id="not-the-target"', 1
            ),
            "nonfinite price": HTML_FIXTURE.replace("57.500", "NaN", 1),
            "negative price": HTML_FIXTURE.replace("56.800", "-1", 1),
        }
        for label, html in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    collect.parse_page(html, "2026-09-21")


class StoreAndHistoryTests(unittest.TestCase):
    def test_seed_import_is_idempotent_and_preserves_approximate_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = store.connect(Path(directory) / "prices.sqlite3")
            try:
                seed = Path(__file__).with_name("seed.csv")
                self.assertEqual(history.import_history(connection, seed), 40)
                self.assertEqual(history.import_history(connection, seed), 0)

                observations = store.observations(connection)
                self.assertEqual(len(observations), 40)
                approximate = [
                    row for row in observations if row["date_precision"] == "approximate"
                ]
                self.assertEqual(len(approximate), 4)
                self.assertTrue(all(row["observed_date"] is None for row in approximate))
            finally:
                connection.close()

    def test_conflicting_import_rolls_back_the_entire_batch(self) -> None:
        rows = _seed_rows()
        first_new = dict(rows[1])
        conflicting = dict(rows[0])
        existing = dict(conflicting)
        existing["value_text"] = "999"
        for field in ("observed_date", "source_updated_at", "fetched_at", "session"):
            existing[field] = existing[field] or None

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            csv_path = directory_path / "conflict.csv"
            _write_csv(csv_path, [first_new, conflicting])
            connection = store.connect(directory_path / "prices.sqlite3")
            try:
                self.assertTrue(store.save_observation(connection, existing))
                with self.assertRaises(ValueError):
                    history.import_history(connection, csv_path)

                observations = store.observations(connection)
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["observation_key"], existing["observation_key"])
                self.assertEqual(observations[0]["value_text"], "999")
            finally:
                connection.close()

    def test_official_duplicate_ignores_fetched_at_but_rejects_changed_value(self) -> None:
        observation = collect.parse_page(HTML_FIXTURE, "2026-09-21")
        observation["fetched_at"] = "2026-09-21T06:40:00+00:00"

        with tempfile.TemporaryDirectory() as directory:
            connection = store.connect(Path(directory) / "prices.sqlite3")
            try:
                self.assertTrue(store.save_observation(connection, observation))

                duplicate = dict(observation)
                duplicate["fetched_at"] = "2026-09-21T06:41:00+00:00"
                self.assertFalse(store.save_observation(connection, duplicate))
                self.assertEqual(len(store.observations(connection)), 1)

                changed = dict(observation)
                changed["value_text"] = "57.168"
                with self.assertRaises(ValueError):
                    store.save_observation(connection, changed)
                self.assertEqual(store.observations(connection)[0]["value_text"], "57.167")
            finally:
                connection.close()

    def test_backup_opened_as_restored_database_retains_observable_prices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            source = store.connect(directory_path / "prices.sqlite3")
            try:
                history.import_history(source, Path(__file__).with_name("seed.csv"))
                expected = [
                    (row["observation_key"], row["value_text"])
                    for row in store.observations(source)
                ]
                backup_path = store.backup_database(source, directory_path / "backup.sqlite3")
            finally:
                source.close()

            restored = store.connect(backup_path)
            try:
                actual = [
                    (row["observation_key"], row["value_text"])
                    for row in store.observations(restored)
                ]
                self.assertEqual(actual, expected)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
