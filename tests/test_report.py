"""Tests for vidfunnel.report: collecting, sorting, redaction, formats, --from and run()."""

VERSION = "20261102"

import argparse
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from vidfunnel.record import new_record, save_record
from vidfunnel.report import (
    HINT_REDACTED,
    MD_COLUMNS,
    ReportError,
    add_arguments,
    band,
    build_header,
    collect,
    parse_report_header,
    render_csv,
    render_json,
    render_markdown,
    rows_from_records,
    run,
    sort_rows,
)
from vidfunnel.redact import Redactor


def parse(argv):
    parser = argparse.ArgumentParser(prog="vidfunnel report")
    add_arguments(parser)
    args = parser.parse_args(argv)
    args.quiet = True
    args.verbose = False
    return args


class ReportTree(unittest.TestCase):
    """Two folders, four records: planned, verified 95, verified 70, quarantined 0."""

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

    def run_report(self, argv):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = run(parse(argv))
        return code, output.getvalue(), errors.getvalue()

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

    # Markdown and redaction

    def test_markdown_is_redacted_by_default(self):
        code, text, _errors = self.run_report([str(self.base)])
        self.assertEqual(0, code)
        for real in ("beach", "client", "meeting", "summer", "family", "garden", self.scratch.name):
            self.assertNotIn(real, text)
        for record in self.records.values():
            self.assertIn(record["id"], text)
        self.assertIn(HINT_REDACTED, text)
        self.assertIn("<root>/s______h_______/b_____h______.avi", text)
        self.assertIn("| " + " | ".join(MD_COLUMNS) + " |", text)

    def test_reveal_shows_names(self):
        code, text, _errors = self.run_report(["--reveal", str(self.base)])
        self.assertEqual(0, code)
        self.assertIn(str(self.holidays / "beach_holiday.avi"), text)
        self.assertIn("client_meeting.mkv", text)

    def test_markdown_shows_human_sizes(self):
        _code, text, _errors = self.run_report(["--reveal", str(self.base)])
        self.assertIn("-> 10 B (beach_holiday.mp4)", text)

    def test_markdown_round_trips_through_parse(self):
        _code, text, _errors = self.run_report(["--min-score", "50", str(self.base)])
        stored = parse_report_header(text)
        self.assertEqual("md", stored["format"])
        self.assertEqual(["vidfunnel", "report", "--format", "md", "--min-score", "50", "--", "<root>"],
                         stored["invocation"])
        self.assertEqual({self.records[word]["id"] for word in ("beach", "mountain")},
                         set(stored["rows"]))
        self.assertEqual({"status": "verified", "score": 95}, stored["rows"][self.records["beach"]["id"]])

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

    def test_with_logs_redacts_paths_inside_stderr(self):
        _code, text, _errors = self.run_report(["--with-logs", str(self.base)])
        self.assertIn("near frame 12", text)
        self.assertIn("<root>/s______h_______/b_____h______.mp4", text)
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
        _code, text, _errors = self.run_report([str(self.base)])
        report_file = Path(self.scratch.name) / "shared_report.md"
        report_file.write_text(text, encoding="utf-8")
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

    def test_from_revealed_report_reuses_stored_paths(self):
        _code, text, _errors = self.run_report(["--reveal", "--format", "json", str(self.base)])
        report_file = Path(self.scratch.name) / "local_report.json"
        report_file.write_text(text, encoding="utf-8")
        code, again, _errors = self.run_report(["--from", str(report_file)])
        self.assertEqual(0, code)
        self.assertEqual(4, len(json.loads(again)["rows"]))

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
        _code, text, _errors = self.run_report([str(self.base)])
        report_file = Path(self.scratch.name) / "shared_report.md"
        report_file.write_text(text, encoding="utf-8")
        code, _output, errors = self.run_report(["--from", str(report_file), "--no-index"])
        self.assertEqual(2, code)
        self.assertIn("'{}'".format(report_file), errors)

    # header and run

    def test_build_header_has_invocation_and_hint(self):
        args = parse(["--format", "csv", "--status", "verified", str(self.base)])
        header = build_header(args, 3)
        self.assertEqual(["vidfunnel", "report", "--format", "csv", "--status", "verified", "--", "<root>"],
                         header["invocation"])
        self.assertEqual(HINT_REDACTED, header["hint"])
        self.assertTrue(header["redacted"])
        self.assertEqual(3, header["records"])
        self.assertEqual(36, len(header["report_id"]))

    def test_renderers_agree_on_rows(self):
        items = collect([str(self.base)])
        redactor = Redactor(roots=[str(self.base)])
        rows = sort_rows(rows_from_records(items, redactor=redactor))
        header = build_header(parse([str(self.base)]), len(rows))
        self.assertEqual(4, len(json.loads(render_json(header, rows))["rows"]))
        self.assertEqual(5, len(render_csv(header, rows).splitlines()))
        self.assertEqual(4, len(parse_report_header(render_markdown(header, rows))["rows"]))

    def test_run_through_namespace_returns_ok(self):
        from vidfunnel.cli import EXIT_OK
        code, text, _errors = self.run_report([str(self.base)])
        self.assertEqual(EXIT_OK, code)
        self.assertTrue(text.startswith("# vidfunnel report"))

    def test_run_missing_path_returns_error(self):
        code, _text, errors = self.run_report([str(self.base / "not_there")])
        self.assertEqual(1, code)
        self.assertIn("not_there", errors)


if __name__ == "__main__":
    unittest.main()

# End of File #
