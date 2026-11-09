"""Tests for vidfunnel.report: collecting, sorting, redaction, formats, --from and run()."""

VERSION = "20261108"

import argparse
import contextlib
import csv
import io
import json
import os
import random
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock
from pathlib import Path

from vidfunnel.record import new_record, save_record
from vidfunnel.report import (
    HEADER_COMMENT_END,
    HEADER_COMMENT_START,
    HINT_REDACTED,
    HINT_REDACTED_NO_FROM,
    MD_COLUMNS,
    OLD_MD_COLUMNS,
    ReportError,
    ReportUsageError,
    add_arguments,
    band,
    build_header,
    collect,
    parse_report_header,
    render_csv,
    render_json,
    render_markdown,
    render_text,
    rows_from_records,
    run,
    short_ids,
    sort_rows,
)
from vidfunnel.index import index_tree, open_index, rebuild
from vidfunnel.redact import Redactor


def parse(argv):
    parser = argparse.ArgumentParser(prog="vidfunnel report")
    add_arguments(parser)
    args = parser.parse_args(argv)
    args.quiet = True
    args.verbose = False
    return args


FIXED_HEADER = {
    "report_id": "report-identity",
    "generated_at": "2026-11-08T12:00:00Z",
    "tool_version": "20261108",
    "command": "vidfunnel report --format text -- '<root>'",
    "redacted": True,
    "redact_dates": False,
    "records": 4,
    "hint": HINT_REDACTED_NO_FROM,
}


def quadratic_short_ids(rows):
    """The definition of short_ids at 1f0ac04, kept as the reference its result must equal."""
    ids = [str(row.get("id") or "") for row in rows]
    shown = {}
    for record_id in ids:
        length = 8
        while length < len(record_id) and any(
                other != record_id and other[:length] == record_id[:length] for other in ids):
            length += 1
        shown[record_id] = record_id[:length]
    return shown


class ReportTree(unittest.TestCase):
    """Two folders, four records: planned, verified 95, verified 70, quarantined 0.

    Source sizes are the length of b"dummy video bytes for " (22) plus the word:
    beach 27 B, mountain 30 B, meeting 29 B, broken 28 B. Verified outputs are 10 B.
    """

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.base = Path(self.scratch.name).resolve() / "family_archive"
        self.holidays = self.base / "summer_holidays"
        self.work = self.base / "office_work"
        self.holidays.mkdir(parents=True)
        self.work.mkdir(parents=True)
        self.records = {}
        self.make("beach", self.holidays / "beach_holiday.avi", "verified", 95, "remux")
        self.make("mountain", self.holidays / "mountain_hike.mpg", "verified", 70, "reencode")
        self.make("meeting", self.work / "client_meeting.mkv", "planned", None, "reencode")
        self.make("broken", self.work / "garden_party.swf", "quarantined", 0, "quarantine")
        self.records["broken"]["quarantine_reason"] = "swf-vector"
        self.records["mountain"]["flags"] = ["aspect-ambiguous"]
        self.records["beach"]["stages"]["convert"] = {
            "stderr": "[mp4 @ 0x1] error writing '{}' near frame 12\n".format(
                self.holidays / "beach_holiday.mp4"),
            "at": "2026-11-02T10:00:00Z",
        }
        self.save_all()

    def tearDown(self):
        self.scratch.cleanup()

    def make(self, word, source, status, score, action):
        source.write_bytes(b"dummy video bytes for " + word.encode("ascii"))
        record = new_record(source)
        record["status"] = status
        record["score"] = score
        record["action"] = action
        if status == "verified":
            record["output"]["name"] = source.stem + ".mp4"
            record["output"]["size"] = 10
        self.records[word] = record
        record["_source"] = source

    def save_all(self):
        for record in self.records.values():
            source = record.pop("_source")
            save_record(source, record)
            record["_source"] = source

    def short(self, word):
        return self.records[word]["id"][:8]

    def run_report(self, argv):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = run(parse(argv))
        return code, output.getvalue(), errors.getvalue()

    def write_report(self, argv, file_name):
        _code, text, _errors = self.run_report(argv)
        report_file = Path(self.scratch.name) / file_name
        report_file.write_text(text, encoding="utf-8")
        return report_file, text

    def redacted_rows(self, with_logs=False):
        items = collect([str(self.base)])
        redactor = Redactor(roots=[str(self.base)])
        return sort_rows(rows_from_records(items, redactor=redactor, with_logs=with_logs)), redactor

    # collect and sort

    def test_collect_finds_four(self):
        items = collect([str(self.base)])
        self.assertEqual(4, len(items))
        ids = {record["id"] for _folder, record in items}
        self.assertEqual({record["id"] for record in self.records.values()}, ids)

    def test_collect_overlapping_paths_counts_each_record_once(self):
        items = collect([str(self.base), str(self.holidays)])
        self.assertEqual(4, len(items))

    def test_collect_missing_path_raises(self):
        with self.assertRaises(ReportError):
            collect([str(self.base / "not_there")])

    def test_sort_puts_worst_scores_first_then_unscored(self):
        rows = sort_rows(rows_from_records(collect([str(self.base)])))
        expected = [self.records[word]["id"] for word in ("broken", "mountain", "beach", "meeting")]
        self.assertEqual(expected, [row["id"] for row in rows])

    def test_band_edges(self):
        self.assertEqual("ok", band(90))
        self.assertEqual("review", band(89))
        self.assertEqual("review", band(60))
        self.assertEqual("hold", band(59))
        self.assertIsNone(band(None))

    # text, the default format

    def test_text_rendering_of_two_folders(self):
        rows, redactor = self.redacted_rows()
        text = redactor.text(render_text(dict(FIXED_HEADER), rows))
        expected = "\n".join([
            "vidfunnel report",
            "",
            HINT_REDACTED_NO_FROM,
            "",
            "report ID: report-identity",
            "generated at: 2026-11-08T12:00:00Z",
            "tool version: 20261108",
            "invocation: vidfunnel report --format text -- '<root>'",
            "redacted: yes",
            "records: 4",
            "",
            "<root>/o____e_w__k/",
            "   0  hold    quarantined  quarantine  --                {}".format(self.short("broken")),
            "      g____n_p___y.swf   (28 B -> --)",
            "  --  --      planned      reencode    --                {}".format(self.short("meeting")),
            "      c____t_m_____g.mkv   (29 B -> --)",
            "",
            "<root>/s____r_h______s/",
            "  70  review  verified     reencode    aspect-ambiguous  {}".format(self.short("mountain")),
            "      m______n_h__e.mpg  ->  m______n_h__e.mp4   (30 B -> 10 B)",
            "  95  ok      verified     remux       --                {}".format(self.short("beach")),
            "      b___h_h_____y.avi  ->  b___h_h_____y.mp4   (27 B -> 10 B)",
        ]) + "\n"
        self.assertEqual(expected, text)

    def test_text_groups_folders_by_their_worst_row(self):
        # Sorted rows: broken (office), mountain (summer), beach (summer), meeting (office).
        # The office folder holds the worst row, so it comes first and prints once,
        # with meeting under it even though meeting sorts last.
        _code, text, _errors = self.run_report([str(self.base)])
        lines = text.splitlines()
        office = lines.index("<root>/o____e_w__k/")
        summer = lines.index("<root>/s____r_h______s/")
        self.assertLess(office, summer)
        self.assertEqual(1, lines.count("<root>/o____e_w__k/"))
        order = [word for word in ("broken", "meeting", "mountain", "beach")
                 if any(line.endswith(self.short(word)) for line in lines)]
        positions = [next(number for number, line in enumerate(lines) if line.endswith(self.short(word)))
                     for word in order]
        self.assertEqual(sorted(positions), positions)
        self.assertLess(positions[1], summer, "meeting belongs to the office group")

    def test_folders_follow_their_worst_row_not_the_alphabet(self):
        attic = self.base / "aardvark_attic"
        attic.mkdir()
        self.make("attic", attic / "attic_boxes.avi", "verified", 99, "remux")
        self.save_all()
        # Worst rows: office 0, summer 70, attic 99; alphabetically attic comes first.
        expected = ["<root>/o____e_w__k/", "<root>/s____r_h______s/", "<root>/a______k_a___c/"]
        _code, text, _errors = self.run_report([str(self.base)])
        self.assertEqual(expected, [line for line in text.splitlines() if line.startswith("<root>/")])
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        folders = text.split("\n## Folders\n\n", 1)[1].splitlines()
        self.assertEqual(expected, [line[2:].rsplit(": ", 1)[0] for line in folders])

    def test_default_format_is_text_and_md_is_still_md(self):
        code, text, _errors = self.run_report([str(self.base)])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("vidfunnel report\n"))
        self.assertIn("invocation: vidfunnel report --format text -- '<root>'", text)
        self.assertNotIn(HEADER_COMMENT_START, text)
        code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        self.assertEqual(0, code)
        self.assertTrue(text.startswith("# vidfunnel report\n"))

    def test_text_is_redacted_by_default(self):
        code, text, _errors = self.run_report([str(self.base)])
        self.assertEqual(0, code)
        for real in ("beach", "client", "meeting", "summer", "family", "garden", self.scratch.name):
            self.assertNotIn(real, text)
        for word in self.records:
            self.assertIn(self.short(word), text)
            self.assertNotIn(self.records[word]["id"], text)
        self.assertIn(HINT_REDACTED_NO_FROM, text)

    def test_text_hint_does_not_offer_from_but_markdown_does(self):
        _code, text, _errors = self.run_report([str(self.base)])
        self.assertNotIn("--from <this file>", text, "--from refuses a text report")
        self.assertIn("--format md", text.splitlines()[2])
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        self.assertIn("> " + HINT_REDACTED + "\n", text)
        self.assertIn("--from <this file> --reveal", HINT_REDACTED)

    def test_text_keeps_a_name_with_a_newline_on_its_line(self):
        self.records["beach"]["source"]["name"] = "beach\nholiday\r.avi"
        self.records["beach"]["output"]["name"] = "beach\nholiday.mp4"
        self.save_all()
        _code, text, _errors = self.run_report(["--reveal", str(self.base)])
        self.assertIn("      beach holiday .avi  ->  beach holiday.mp4   (27 B -> 10 B)\n", text)
        self.assertNotIn("\nholiday", text)
        self.assertNotIn("\r", text)

    def make_christmas_folders(self):
        """Two real folders whose redacted forms are the same: 'C_______s ####'."""
        early = self.base / "Christmas 1998"
        late = self.base / "Christmas 2001"
        early.mkdir()
        late.mkdir()
        self.make("sledding", early / "sledding_hill.avi", "verified", 80, "remux")
        self.make("presents", late / "presents_opening.avi", "verified", 85, "remux")
        self.save_all()

    def test_two_real_folders_with_one_redacted_form_stay_two_groups(self):
        self.make_christmas_folders()
        _code, text, _errors = self.run_report([str(self.base)])
        lines = text.splitlines()
        heading = "<root>/C_______s ####/"
        self.assertEqual(2, lines.count(heading), text)
        first = lines.index(heading)
        second = lines.index(heading, first + 1)
        self.assertTrue(lines[first + 1].endswith(self.short("sledding")))
        self.assertTrue(lines[second + 1].endswith(self.short("presents")))
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        folders = text.split("\n## Folders\n\n", 1)[1].splitlines()
        self.assertIn("- {}: {}".format(heading, self.short("sledding")), folders)
        self.assertIn("- {}: {}".format(heading, self.short("presents")), folders)
        _code, text, _errors = self.run_report(["--format", "json", str(self.base)])
        self.assertNotIn("_folder_group", text)

    def test_reveal_shows_names(self):
        code, text, _errors = self.run_report(["--reveal", str(self.base)])
        self.assertEqual(0, code)
        self.assertIn(str(self.holidays) + "/\n", text)
        self.assertIn("beach_holiday.avi  ->  beach_holiday.mp4   (27 B -> 10 B)", text)
        self.assertIn("client_meeting.mkv", text)

    def test_text_with_logs(self):
        rows, redactor = self.redacted_rows(with_logs=True)
        header = dict(FIXED_HEADER, with_logs=True)
        lines = redactor.text(render_text(header, rows)).splitlines()
        beach = next(number for number, line in enumerate(lines) if line.endswith(self.short("beach")))
        self.assertEqual([
            "      b___h_h_____y.avi  ->  b___h_h_____y.mp4   (27 B -> 10 B)",
            "      convert stderr:",
            "        [mp4 @ 0x1] error writing '<root>/s____r_h______s/b___h_h_____y.mp4' near frame 12",
        ], lines[beach + 1:beach + 4])
        _code, text, _errors = self.run_report(["--with-logs", str(self.base)])
        self.assertIn("near frame 12", text)
        self.assertNotIn("beach_holiday", text)

    def test_text_with_logs_says_when_there_are_none(self):
        rows, redactor = self.redacted_rows(with_logs=True)
        rows = [row for row in rows if not row.get("logs")]
        text = render_text(dict(FIXED_HEADER, with_logs=True), rows)
        self.assertTrue(text.endswith("\nNo stderr or run log is stored in these records.\n"))

    # Markdown

    def test_markdown_columns_ids_and_folders(self):
        code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        self.assertEqual(0, code)
        self.assertIn("| ID | score | band | status | action | flags | source | size |\n", text)
        table = [line for line in text.splitlines() if line.startswith("| ") and not line.startswith("| ID |")]
        self.assertEqual(4, len(table))
        for line in table:
            self.assertEqual(8, len(line.split(" | ")[0][2:]))
        self.assertIn("| {} | 70 | review | verified | reencode | aspect-ambiguous | m______n_h__e.mpg "
                      "| 30 B -> 10 B |".format(self.short("mountain")), text)
        self.assertIn("| {} | -- | -- | planned | reencode | -- | c____t_m_____g.mkv | 29 B -> -- |".format(
            self.short("meeting")), text)
        folders = text.split("\n## Folders\n\n", 1)[1].splitlines()
        self.assertEqual([
            "- <root>/o____e_w__k/: {}, {}".format(self.short("broken"), self.short("meeting")),
            "- <root>/s____r_h______s/: {}, {}".format(self.short("mountain"), self.short("beach")),
        ], folders)
        for real in ("beach", "summer", self.scratch.name):
            self.assertNotIn(real, text)

    def test_markdown_shows_output_only_when_it_differs(self):
        # An MP4 source is converted under the interim name, renamed by retire (HANDOFF 2.4).
        beach = self.records["beach"]
        beach["output"]["interim_name"] = "beach_holiday.vidfunnel.mp4"
        self.save_all()
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        self.assertIn("| b___h_h_____y.avi -> b___h_h_____y.v_______l.mp4 | 27 B -> 10 B |", text)
        self.assertIn("| m______n_h__e.mpg | 30 B -> 10 B |", text)
        self.assertNotIn("m______n_h__e.mp4", text)
        beach["status"] = "retired"
        beach["stages"]["retire"] = {"renamed_output": True}
        self.save_all()
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        self.assertIn("| b___h_h_____y.avi | 27 B -> 10 B |", text, "after retire the final name is on disk")

    def test_retired_record_whose_rename_failed_shows_the_interim_name(self):
        beach = self.records["beach"]
        beach["output"]["interim_name"] = "beach_holiday.vidfunnel.mp4"
        beach["status"] = "retired"
        beach["stages"]["retire"] = {"renamed_output": False, "rename_error":
                                     "renaming 'beach_holiday.vidfunnel.mp4' to 'beach_holiday.mp4' failed"}
        self.save_all()
        _code, text, _errors = self.run_report(["--reveal", str(self.base)])
        self.assertIn("beach_holiday.avi  ->  beach_holiday.vidfunnel.mp4   (27 B -> 10 B)", text)
        _code, text, _errors = self.run_report(["--format", "json", "--reveal", str(self.base)])
        row = next(row for row in json.loads(text)["rows"] if row["id"] == beach["id"])
        self.assertEqual("beach_holiday.vidfunnel.mp4", row["output_name"])

    def test_markdown_revealed_folders_show_real_paths(self):
        _code, text, _errors = self.run_report(["--format", "md", "--reveal", str(self.base)])
        self.assertIn("- {}/: {}, {}".format(self.holidays, self.short("mountain"), self.short("beach")), text)
        self.assertIn("| beach_holiday.avi | 27 B -> 10 B |", text)

    def test_short_ids_grow_past_eight_only_where_two_rows_share_them(self):
        rows = [{"id": "abcd1234-0000-4000-8000-000000000001"},
                {"id": "abcd1234-0000-4000-8000-000000000002"},
                {"id": "ffee9988-0000-4000-8000-000000000003"}]
        self.assertEqual({"abcd1234-0000-4000-8000-000000000001": "abcd1234-0000-4000-8000-000000000001",
                          "abcd1234-0000-4000-8000-000000000002": "abcd1234-0000-4000-8000-000000000002",
                          "ffee9988-0000-4000-8000-000000000003": "ffee9988"}, short_ids(rows))

    def test_short_ids_equal_the_old_definition_on_shared_prefixes(self):
        rows = [{"id": value} for value in (
            "abcd1234-0000-4000-8000-000000000001", "abcd1234-0000-4000-8000-000000000002",
            "abcd1234-0000-4000-9000-000000000003", "abcd1235-0000-4000-8000-000000000004",
            "abcdef", "abcdefgh", "abcdefghij", "abcdefghijk", "ffee9988-0000", "ffee9988-0000",
            "", "0123456789abcdef", "0123456789abcdeg", "zzzzzzzz-1111")]
        self.assertEqual(quadratic_short_ids(rows), short_ids(rows))

    def test_short_ids_of_ten_thousand_uuids_take_under_three_seconds(self):
        generator = random.Random(20261108)
        rows = [{"id": str(uuid.UUID(int=generator.getrandbits(128), version=4))} for _count in range(10000)]
        started = time.monotonic()
        shown = short_ids(rows)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)
        self.assertEqual(10000, len(set(shown.values())))
        longer = [row for row in rows if shown[row["id"]] != row["id"][:8]]
        everything = [row["id"] for row in rows]
        for row in rows[:200] + longer[:50]:
            prefix = shown[row["id"]]
            self.assertEqual([row["id"]], [other for other in everything if other.startswith(prefix)])

    def test_markdown_round_trips_through_parse(self):
        _code, text, _errors = self.run_report(["--format", "md", "--min-score", "50", str(self.base)])
        stored = parse_report_header(text)
        self.assertEqual("md", stored["format"])
        self.assertEqual(["vidfunnel", "report", "--format", "md", "--min-score", "50", "--", "<root>"],
                         stored["invocation"])
        self.assertEqual({self.short(word) for word in ("beach", "mountain")}, set(stored["rows"]))
        self.assertEqual({"status": "verified", "score": 95}, stored["rows"][self.short("beach")])
        self.assertEqual({self.short("beach"): "<root>/s____r_h______s/",
                          self.short("mountain"): "<root>/s____r_h______s/"}, stored["folders"])

    # JSON and CSV

    def test_json_round_trips_through_parse(self):
        code, text, _errors = self.run_report(["--format", "json", str(self.base)])
        self.assertEqual(0, code)
        data = json.loads(text)
        self.assertEqual({"header", "rows"}, set(data))
        stored = parse_report_header(text)
        self.assertEqual("json", stored["format"])
        self.assertEqual(data["header"]["invocation"], stored["invocation"])
        self.assertEqual(4, len(stored["rows"]))
        self.assertEqual({"status": "planned", "score": None}, stored["rows"][self.records["meeting"]["id"]])
        self.assertNotIn("beach", text)

    def test_csv_has_same_column_count_on_every_line(self):
        code, text, _errors = self.run_report(["--format", "csv", "--with-logs", str(self.base)])
        self.assertEqual(0, code)
        lines = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(5, len(lines))
        widths = {len(line) for line in lines}
        self.assertEqual({len(lines[0])}, widths)
        self.assertEqual("id", lines[0][0])

    def test_csv_cannot_be_read_back(self):
        _code, text, _errors = self.run_report(["--format", "csv", str(self.base)])
        with self.assertRaises(ReportError):
            parse_report_header(text)

    # logs and filters

    def test_markdown_with_logs_redacts_paths_inside_stderr(self):
        _code, text, _errors = self.run_report(["--format", "md", "--with-logs", str(self.base)])
        self.assertIn("near frame 12", text)
        self.assertIn("### {}".format(self.short("beach")), text)
        self.assertIn("<root>/s____r_h______s/b___h_h_____y.mp4", text)
        self.assertNotIn("beach_holiday", text)

    def test_with_logs_revealed_keeps_real_path(self):
        _code, text, _errors = self.run_report(["--with-logs", "--reveal", str(self.base)])
        self.assertIn(str(self.holidays / "beach_holiday.mp4"), text)

    def test_min_score_filters(self):
        _code, text, _errors = self.run_report(["--format", "json", "--min-score", "80", str(self.base)])
        rows = json.loads(text)["rows"]
        self.assertEqual([self.records["beach"]["id"]], [row["id"] for row in rows])

    def test_status_filter(self):
        _code, text, _errors = self.run_report(["--format", "json", "--status", "verified",
                                                "--status", "planned", str(self.base)])
        rows = json.loads(text)["rows"]
        self.assertEqual(3, len(rows))

    # --from

    def test_from_marks_changed_rows(self):
        report_file, text = self.write_report(["--format", "md", str(self.base)], "shared_report.md")
        meeting = self.records["meeting"]
        meeting["status"] = "verified"
        meeting["score"] = 92
        self.save_all()
        code, again, _errors = self.run_report(["--from", str(report_file), "--reveal",
                                                "--format", "json", str(self.base)])
        self.assertEqual(0, code)
        data = json.loads(again)
        self.assertTrue(data["header"]["redacted"] is False)
        self.assertEqual(parse_report_header(text)["header"]["report_id"], data["header"]["from_report"])
        changed = {row["id"]: row["changed_since"] for row in data["rows"]}
        self.assertEqual("status planned -> verified; score -- -> 92", changed[meeting["id"]])
        for word in ("beach", "mountain", "broken"):
            self.assertIsNone(changed[self.records[word]["id"]])
        self.assertIn("client_meeting.mkv", again)

    def old_layout_report(self):
        """A Markdown report in the layout written before 20261108: full IDs and a path column."""
        header = {"report_id": "earlier-report", "invocation": ["vidfunnel", "report", "--format", "md",
                                                                "--", "<root>"], "redacted": True}
        lines = ["# vidfunnel report", "", HEADER_COMMENT_START + json.dumps(header) + HEADER_COMMENT_END, "",
                 "| " + " | ".join(OLD_MD_COLUMNS) + " |", "|---|---|---|---|---|---|---|---|"]
        for word, status, score in (("beach", "verified", 95), ("meeting", "planned", "--")):
            lines.append("| {} | {} | -- | {} | remux | -- | 1 KiB -> 1 KiB (x.mp4) | <root>/x/y.avi |".format(
                self.records[word]["id"], score, status))
        return "\n".join(lines) + "\n"

    def test_from_old_layout_markdown(self):
        stored = parse_report_header(self.old_layout_report())
        self.assertEqual({self.records["beach"]["id"], self.records["meeting"]["id"]}, set(stored["rows"]))
        self.assertEqual({}, stored["folders"])
        report_file = Path(self.scratch.name) / "old_report.md"
        report_file.write_text(self.old_layout_report(), encoding="utf-8")
        self.records["meeting"]["status"] = "failed"
        self.save_all()
        code, again, errors = self.run_report(["--from", str(report_file), "--format", "json", str(self.base)])
        self.assertEqual(0, code, errors)
        data = json.loads(again)
        changed = {row["id"]: row["changed_since"] for row in data["rows"]}
        self.assertEqual("status planned -> failed", changed[self.records["meeting"]["id"]])
        self.assertIsNone(changed[self.records["beach"]["id"]])
        self.assertEqual("earlier-report", data["header"]["from_report"])

    def test_from_text_report_saved_with_its_stderr_is_usage_error(self):
        _code, text, _errors = self.run_report([str(self.base)])
        report_file = Path(self.scratch.name) / "terminal_with_stderr.txt"
        report_file.write_text("vidfunnel report: no PATH given; reading the current directory '/x'\n"
                               "report: reading records under '/x'\n" + text + "report: 4 rows, redacted\n",
                               encoding="utf-8")
        with self.assertRaises(ReportUsageError):
            parse_report_header(report_file.read_text(encoding="utf-8"))
        code, _output, errors = self.run_report(["--from", str(report_file), str(self.base)])
        self.assertEqual(2, code)
        self.assertIn("text report", errors)

    def test_parse_stops_at_the_logs_section(self):
        beach = self.records["beach"]
        beach["stages"]["convert"]["stderr"] = ("## Folders\n\n- /elsewhere/: deadbeef\n\n"
                                                "| " + " | ".join(MD_COLUMNS) + " |\n|---|---|---|---|---|---|---|---|\n"
                                                "| deadbeef | 1 | hold | failed | -- | -- | x.avi | -- |\n")
        self.save_all()
        _code, text, _errors = self.run_report(["--format", "md", "--with-logs", str(self.base)])
        self.assertIn("| deadbeef |", text)
        stored = parse_report_header(text)
        self.assertEqual({self.short(word) for word in self.records}, set(stored["rows"]))
        self.assertNotIn("deadbeef", stored["folders"])
        self.assertEqual(4, len(stored["folders"]))

    def test_parse_rejects_a_table_with_unknown_columns(self):
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        changed = text.replace("| ID | score | band |", "| ID | rating | band |")
        self.assertNotEqual(text, changed)
        with self.assertRaises(ReportError) as caught:
            parse_report_header(changed)
        self.assertIn("unknown columns", str(caught.exception))

    def test_from_says_which_earlier_row_is_not_found(self):
        report_file, text = self.write_report(["--format", "md", str(self.base)], "shared_report.md")
        report_file.write_text(text.replace("| {} |".format(self.short("beach")), "| deadbeef |"),
                               encoding="utf-8")
        args = parse(["--from", str(report_file), "--format", "json", str(self.base)])
        args.quiet = False
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = run(args)
        self.assertEqual(0, code, errors.getvalue())
        self.assertIn("report: record deadbeef from the earlier report was not found now", errors.getvalue())

    def test_from_resolves_an_upper_case_prefix(self):
        self.records["beach"]["id"] = "beadfeed-0000-4000-8000-000000000001"
        self.save_all()
        report_file, text = self.write_report(["--format", "md", str(self.base)], "shared_report.md")
        upper = self.short("beach").upper()
        report_file.write_text(text.replace("| {} |".format(self.short("beach")), "| {} |".format(upper)),
                               encoding="utf-8")
        code, again, errors = self.run_report(["--from", str(report_file), "--format", "json", str(self.base)])
        self.assertEqual(0, code, errors)
        changed = {row["id"]: row["changed_since"] for row in json.loads(again)["rows"]}
        self.assertIsNone(changed[self.records["beach"]["id"]])

    def test_from_text_report_is_usage_error(self):
        report_file, _text = self.write_report([str(self.base)], "terminal_report.txt")
        code, output, errors = self.run_report(["--from", str(report_file), str(self.base)])
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIn("'{}'".format(report_file), errors)
        self.assertIn("text report", errors)
        with self.assertRaises(ReportUsageError):
            parse_report_header(report_file.read_text(encoding="utf-8"))

    def test_from_ambiguous_prefix_is_an_error_naming_both(self):
        self.records["beach"]["id"] = "abcd1234-0000-4000-8000-000000000001"
        self.records["mountain"]["id"] = "abcd1234-0000-4000-8000-000000000002"
        self.save_all()
        _code, text, _errors = self.run_report(["--format", "md", str(self.base)])
        self.assertIn("| abcd1234-0000-4000-8000-000000000001 |", text, "shown IDs grow to stay unique")
        report_file = Path(self.scratch.name) / "edited_report.md"
        report_file.write_text(text.replace("| abcd1234-0000-4000-8000-000000000001 |", "| abcd1234 |"),
                               encoding="utf-8")
        code, _output, errors = self.run_report(["--from", str(report_file), "--format", "json", str(self.base)])
        self.assertEqual(1, code)
        self.assertIn("'abcd1234'", errors)
        self.assertIn("'abcd1234-0000-4000-8000-000000000001'", errors)
        self.assertIn("'abcd1234-0000-4000-8000-000000000002'", errors)

    def test_shown_id_is_unique_among_filtered_out_records_too(self):
        self.records["beach"]["id"] = "abcd1234-0000-4000-8000-000000000001"
        self.records["mountain"]["id"] = "abcd1234-0000-4000-9000-000000000002"
        self.save_all()
        report_file, text = self.write_report(["--format", "md", "--min-score", "80", str(self.base)],
                                              "filtered_report.md")
        self.assertIn("| abcd1234-0000-4000-8 | 95 |", text, "mountain is filtered out but shares 8 characters")
        code, again, errors = self.run_report(["--from", str(report_file), "--format", "json", str(self.base)])
        self.assertEqual(0, code, errors)
        rows = json.loads(again)["rows"]
        self.assertEqual([("abcd1234-0000-4000-8000-000000000001", None)],
                         [(row["id"], row["changed_since"]) for row in rows])

    def test_from_revealed_report_reuses_stored_paths(self):
        report_file, _text = self.write_report(["--reveal", "--format", "json", str(self.base)],
                                               "local_report.json")
        code, again, _errors = self.run_report(["--from", str(report_file)])
        self.assertEqual(0, code)
        self.assertEqual(4, len(json.loads(again)["rows"]))

    def index_report_file(self, argv, file_name):
        """A revealed Markdown report as a run over the index writes it: no PATH in the invocation."""
        _code, text, _errors = self.run_report(argv)
        lines = text.splitlines()
        for number, line in enumerate(lines):
            if line.startswith(HEADER_COMMENT_START):
                header = json.loads(line[len(HEADER_COMMENT_START):-len(HEADER_COMMENT_END)])
                header["invocation"] = header["invocation"][:header["invocation"].index("--")]
                lines[number] = HEADER_COMMENT_START + json.dumps(header) + HEADER_COMMENT_END
        report_file = Path(self.scratch.name) / file_name
        report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_file

    def test_from_index_report_reads_the_index_when_a_folder_is_gone(self):
        db_path = Path(self.scratch.name) / "state" / "index.sqlite"
        db_path.parent.mkdir()
        connection = open_index(db_path)
        index_tree(connection, self.base, progress_say=lambda message: None)
        connection.close()
        report_file = self.index_report_file(["--format", "md", "--reveal", str(self.base)], "index_report.md")
        shutil.rmtree(str(self.work))
        code, again, errors = self.run_report(["--from", str(report_file), "--db", str(db_path),
                                               "--format", "json"])
        self.assertEqual(0, code, errors)
        self.assertEqual({record["id"] for record in self.records.values()},
                         {row["id"] for row in json.loads(again)["rows"]})
        self.assertNotIn("skipping folder", errors)

    def index_into(self, db_path, root, fresh=False):
        db_path.parent.mkdir(exist_ok=True)
        connection = open_index(db_path)
        if fresh:
            rebuild(connection)
        index_tree(connection, root, progress_say=lambda message: None)
        connection.close()

    def test_from_index_report_after_a_folder_moved_reads_the_whole_index(self):
        db_path = Path(self.scratch.name) / "state" / "index.sqlite"
        self.index_into(db_path, self.base)
        report_file, _text = self.write_report(["--db", str(db_path), "--reveal", "--format", "md"],
                                               "index_report.md")
        self.assertNotIn(" -- ", " ".join(parse_report_header(report_file.read_text())["invocation"]))
        self.holidays.rename(self.base / "moved_holidays")
        self.index_into(db_path, self.base, fresh=True)
        code, again, errors = self.run_report(["--from", str(report_file), "--format", "json"])
        self.assertEqual(0, code, errors)
        self.assertEqual({record["id"] for record in self.records.values()},
                         {row["id"] for row in json.loads(again)["rows"]})

    def test_from_index_report_after_the_tree_was_copied_reads_the_whole_index(self):
        db_path = Path(self.scratch.name) / "state" / "index.sqlite"
        self.index_into(db_path, self.base)
        report_file, _text = self.write_report(["--db", str(db_path), "--reveal", "--format", "md"],
                                               "index_report.md")
        copy = Path(self.scratch.name) / "copied_archive"
        shutil.copytree(str(self.base), str(copy))
        self.index_into(db_path, copy, fresh=True)
        code, again, errors = self.run_report(["--from", str(report_file), "--format", "json"])
        self.assertEqual(0, code, errors)
        self.assertEqual({record["id"] for record in self.records.values()},
                         {row["id"] for row in json.loads(again)["rows"]})

    def test_from_index_report_without_index_skips_a_folder_that_is_gone(self):
        report_file = self.index_report_file(["--format", "md", "--reveal", str(self.base)], "index_report.md")
        shutil.rmtree(str(self.work))
        missing_db = Path(self.scratch.name) / "no_state" / "index.sqlite"
        code, again, errors = self.run_report(["--from", str(report_file), "--db", str(missing_db),
                                               "--format", "json"])
        self.assertEqual(0, code, errors)
        self.assertEqual({self.records[word]["id"] for word in ("beach", "mountain")},
                         {row["id"] for row in json.loads(again)["rows"]})
        self.assertEqual(1, errors.count("skipping folder"), errors)
        self.assertIn("skipping folder '{}/' of the earlier report: it no longer exists".format(self.work), errors)

    def test_from_revealed_markdown_without_paths_reads_its_folders(self):
        _code, text, _errors = self.run_report(["--format", "md", "--reveal", "--min-score", "50",
                                                str(self.base)])
        # The same report as a run over the index would write it: no PATH in the invocation.
        lines = text.splitlines()
        for number, line in enumerate(lines):
            if line.startswith(HEADER_COMMENT_START):
                header = json.loads(line[len(HEADER_COMMENT_START):-len(HEADER_COMMENT_END)])
                header["invocation"] = header["invocation"][:header["invocation"].index("--")]
                lines[number] = HEADER_COMMENT_START + json.dumps(header) + HEADER_COMMENT_END
        report_file = Path(self.scratch.name) / "index_report.md"
        report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.make("sunset", self.holidays / "sunset_walk.avi", "verified", 99, "remux")
        self.save_all()
        code, again, errors = self.run_report(["--from", str(report_file), "--no-index", "--format", "json"])
        self.assertEqual(0, code, errors)
        self.assertEqual({self.records[word]["id"] for word in ("beach", "mountain")},
                         {row["id"] for row in json.loads(again)["rows"]})

    def test_redaction_compiles_its_pattern_once_for_all_records(self):
        # Learning and redacting record by record recompiled the pattern per record (quadratic);
        # a 5,000-record redacted report took about 2.5 minutes with nothing printed. The bound
        # also needs the trie-shaped pattern: with a flat alternation of every name these 4,000
        # records took 15 s here, with the trie under 3 s.
        import re as regex_module
        items = []
        for number in range(4000):
            folder = str(self.base / "tree{}".format(number % 40))
            source = Path(folder) / "clip {}.avi".format(number)
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(b"x")
            record = new_record(source)
            record["source"]["folder"] = folder
            record["output"]["name"] = "clip {}.mp4".format(number)
            # The argv goes through Redactor.text() while the record is redacted, which is
            # where the per-record learn-then-redact compiled the pattern again.
            record["stages"]["plan"] = {"argv": ["ffmpeg", "-i", str(source), "-c", "copy",
                                                 str(Path(folder) / "clip {}.mp4".format(number))]}
            items.append((Path(folder), record))
        compiled = []
        real_compile = regex_module.compile

        def counting(pattern, *rest, **options):
            if "(?<![^\\W_])" in pattern:
                compiled.append(pattern)
            return real_compile(pattern, *rest, **options)

        started = time.monotonic()
        with mock.patch("vidfunnel.redact.re.compile", side_effect=counting):
            rows = rows_from_records(items, redactor=Redactor(roots=[str(self.base)]))
        elapsed = time.monotonic() - started
        self.assertEqual(len(rows), 4000)
        self.assertEqual(len(compiled), 1, "one compile with every name, not one per record")
        self.assertLess(elapsed, 10, "4000 records redacted in {:.1f} s".format(elapsed))

    def test_no_path_reads_the_current_directory_and_says_so(self):
        previous = os.getcwd()
        os.chdir(str(self.base))
        self.addCleanup(os.chdir, previous)
        args = parse(["--format", "json"])
        args.quiet = False
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = run(args)
        self.assertEqual(code, 0, errors.getvalue())
        self.assertIn("vidfunnel report: no PATH given; reading the current directory '{}'".format(os.getcwd()),
                      errors.getvalue())
        self.assertEqual(len(json.loads(output.getvalue())["rows"]), 4)

    def test_index_option_reads_the_index_instead_of_the_current_directory(self):
        seen = {}

        def fake_collect(paths, use_index=False, db_path=None, errors=None):
            seen.update(paths=list(paths), use_index=use_index, db_path=db_path)
            return []

        with mock.patch("vidfunnel.report.collect", side_effect=fake_collect):
            code, _output, errors = self.run_report(["--index"])
        self.assertEqual(code, 0, errors)
        self.assertEqual((seen["paths"], seen["use_index"]), ([], True))
        with mock.patch("vidfunnel.report.collect", side_effect=fake_collect):
            self.run_report(["--db", str(self.base / "somewhere.sqlite")])
        self.assertEqual((seen["paths"], seen["use_index"]), ([], True), "--db alone still means the index")

    def test_from_redacted_report_without_paths_or_index_is_usage_error(self):
        report_file, _text = self.write_report(["--format", "md", str(self.base)], "shared_report.md")
        code, _output, errors = self.run_report(["--from", str(report_file), "--no-index"])
        self.assertEqual(2, code)
        self.assertIn("'{}'".format(report_file), errors)

    # header and run

    def test_build_header_has_invocation_and_hint(self):
        args = parse(["--format", "csv", "--status", "verified", str(self.base)])
        header = build_header(args, 3)
        self.assertEqual(["vidfunnel", "report", "--format", "csv", "--status", "verified", "--", "<root>"],
                         header["invocation"])
        self.assertEqual(HINT_REDACTED_NO_FROM, header["hint"], "CSV cannot be read back either")
        self.assertEqual(HINT_REDACTED, build_header(parse(["--format", "json", str(self.base)]), 3)["hint"])
        self.assertTrue(header["redacted"])
        self.assertEqual(3, header["records"])
        self.assertEqual(36, len(header["report_id"]))

    def test_renderers_agree_on_rows(self):
        rows, _redactor = self.redacted_rows()
        header = build_header(parse([str(self.base)]), len(rows))
        self.assertEqual(4, len(json.loads(render_json(header, rows))["rows"]))
        self.assertEqual(5, len(render_csv(header, rows).splitlines()))
        self.assertEqual(4, len(parse_report_header(render_markdown(header, rows))["rows"]))
        self.assertEqual(4, sum(1 for line in render_text(header, rows).splitlines() if line.startswith("  ")
                                and not line.startswith("      ")))

    def test_run_missing_path_returns_error(self):
        code, _text, errors = self.run_report([str(self.base / "not_there")])
        self.assertEqual(1, code)
        self.assertIn("not_there", errors)


if __name__ == "__main__":
    unittest.main()

# End of File #
