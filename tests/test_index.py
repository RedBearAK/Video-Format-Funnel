"""Tests for vidfunnel.index: indexing, queries, removed rows, find by ID and prefix, subcommands."""

VERSION = "20261102"

import argparse
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from vidfunnel.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE
from vidfunnel.index import (
    IndexMissing,
    add_arguments,
    add_find_arguments,
    default_db_path,
    find,
    index_tree,
    open_index,
    query,
    rebuild,
    run,
    run_find,
    upsert_record,
)
from vidfunnel.record import new_record, record_path, save_record, set_status


def quiet_say(message):
    pass


class IndexTestCase(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        base = Path(self.workspace.name)
        self.library = base / "library"
        self.holidays = self.library / "holidays"
        self.birthdays = self.library / "family" / "birthdays"
        self.holidays.mkdir(parents=True)
        self.birthdays.mkdir(parents=True)
        self.db_path = base / "state" / "index.sqlite"
        self.records = {}
        self.add_source(self.holidays / "holiday_beach.avi", "verified", 95, "remux", output="holiday_beach.mp4")
        self.add_source(self.holidays / "holiday_mountain.mkv", "verified", 70, "reencode",
                        output="holiday_mountain.mp4")
        self.add_source(self.birthdays / "birthday_cake.mpg", "quarantined", None, "quarantine")
        self.add_source(self.birthdays / "birthday_party.wmv", "planned", None, "reencode")

    def tearDown(self):
        self.workspace.cleanup()

    def add_source(self, source, status, score, action, output=None, record_id=None):
        source.write_bytes(("dummy video bytes of " + source.name).encode("utf-8"))
        record = new_record(source)
        if record_id is not None:
            record["id"] = record_id
        set_status(record, status)
        record["score"] = score
        record["action"] = action
        record["flags"] = ["aspect-ambiguous"] if action == "reencode" else []
        if output is not None:
            record["output"]["name"] = output
            record["output"]["size"] = 1234
        save_record(source, record)
        self.records[source.name] = record
        return record

    def open(self):
        conn = open_index(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def row_count(self, conn):
        return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]


class TestDatabase(IndexTestCase):

    def test_default_db_path_under_home(self):
        self.assertEqual(default_db_path(home="/home/someone"),
                         Path("/home/someone/.local/state/vidfunnel/index.sqlite"))
        self.assertTrue(str(default_db_path()).endswith(os.path.join(".local", "state", "vidfunnel", "index.sqlite")))
        self.assertNotIn("~", str(default_db_path()))

    def test_open_creates_schema_without_wal(self):
        conn = self.open()
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"records", "meta"})
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "1")

    def test_open_without_create_refuses_missing_file(self):
        with self.assertRaises(IndexMissing):
            open_index(self.db_path, create=False)
        self.assertFalse(self.db_path.exists())

    def test_other_schema_version_is_rebuilt(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        with conn:
            conn.execute("UPDATE meta SET value = 'old' WHERE key = 'schema_version'")
        conn.close()
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            reopened = open_index(self.db_path)
        self.addCleanup(reopened.close)
        self.assertIn("rebuilding", warning.getvalue())
        self.assertEqual(self.row_count(reopened), 0)

    def test_rebuild_empties_and_recreates(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        self.assertEqual(self.row_count(conn), 4)
        rebuild(conn)
        self.assertEqual(self.row_count(conn), 0)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "1")
        self.assertIsNone(conn.execute("SELECT value FROM meta WHERE key = 'last_indexed'").fetchone())
        index_tree(conn, self.library, progress_say=quiet_say)
        self.assertEqual(self.row_count(conn), 4)


class TestIndexTree(IndexTestCase):

    def test_counts_and_row_contents(self):
        conn = self.open()
        messages = []
        counts = index_tree(conn, self.library, progress_say=messages.append)
        self.assertEqual(counts, {"folders": 2, "records": 4, "removed": 0, "errors": 0})
        self.assertIn("'{}'".format(self.library), messages[0])
        beach = self.records["holiday_beach.avi"]
        row = conn.execute("SELECT folder, source_name, record_path, status, score, action, flags, "
                           "output_name, output_size FROM records WHERE id = ?", (beach["id"],)).fetchone()
        self.assertEqual(row, (str(self.holidays), "holiday_beach.avi",
                               str(record_path(self.holidays / "holiday_beach.avi")),
                               "verified", 95, "remux", "[]", "holiday_beach.mp4", 1234))
        self.assertIsNotNone(conn.execute("SELECT value FROM meta WHERE key = 'last_indexed'").fetchone())

    def test_indexing_twice_keeps_one_row_per_record(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        index_tree(conn, self.library, progress_say=quiet_say)
        upsert_record(conn, self.holidays, self.records["holiday_beach.avi"])
        conn.commit()
        self.assertEqual(self.row_count(conn), 4)

    def test_upsert_replaces_changed_values(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        party = self.records["birthday_party.wmv"]
        party["score"] = 88
        set_status(party, "converted")
        save_record(self.birthdays / "birthday_party.wmv", party)
        index_tree(conn, self.library, progress_say=quiet_say)
        self.assertEqual(conn.execute("SELECT status, score FROM records WHERE id = ?", (party["id"],)).fetchone(),
                         ("converted", 88))

    def test_deleted_record_file_is_removed_only_under_its_root(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        record_path(self.birthdays / "birthday_cake.mpg").unlink()
        counts = index_tree(conn, self.holidays, progress_say=quiet_say)
        self.assertEqual(counts["removed"], 0)
        self.assertEqual(self.row_count(conn), 4)
        counts = index_tree(conn, self.library, progress_say=quiet_say)
        self.assertEqual(counts, {"folders": 2, "records": 3, "removed": 1, "errors": 0})
        cake_id = self.records["birthday_cake.mpg"]["id"]
        self.assertIsNone(conn.execute("SELECT id FROM records WHERE id = ?", (cake_id,)).fetchone())

    def test_sibling_folder_with_shared_name_prefix_is_not_under_root(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        sibling = Path(self.workspace.name) / "library_extra"
        sibling.mkdir()
        counts = index_tree(conn, sibling, progress_say=quiet_say)
        self.assertEqual(counts["removed"], 0)
        self.assertEqual(self.row_count(conn), 4)

    def test_unreadable_record_is_reported_and_its_row_kept(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        broken = record_path(self.holidays / "holiday_mountain.mkv")
        broken.write_text("{ not json", encoding="utf-8")
        messages = []
        counts = index_tree(conn, self.library, progress_say=messages.append)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(counts["removed"], 0)
        self.assertEqual(self.row_count(conn), 4)
        self.assertTrue(any("'{}'".format(broken) in message for message in messages))

    def test_missing_root_raises(self):
        conn = self.open()
        with self.assertRaises(FileNotFoundError):
            index_tree(conn, Path(self.workspace.name) / "nowhere", progress_say=quiet_say)


class TestQuery(IndexTestCase):

    def setUp(self):
        IndexTestCase.setUp(self)
        self.conn = self.open()
        index_tree(self.conn, self.library, progress_say=quiet_say)

    def names(self, rows):
        return sorted(row["source_name"] for row in rows)

    def test_by_status(self):
        self.assertEqual(self.names(query(self.conn, status="verified")),
                         ["holiday_beach.avi", "holiday_mountain.mkv"])
        self.assertEqual(self.names(query(self.conn, status=("planned", "quarantined"))),
                         ["birthday_cake.mpg", "birthday_party.wmv"])

    def test_by_score_band(self):
        self.assertEqual(self.names(query(self.conn, min_score=90)), ["holiday_beach.avi"])
        self.assertEqual(self.names(query(self.conn, min_score=60, max_score=89)), ["holiday_mountain.mkv"])
        self.assertEqual(self.names(query(self.conn, max_score=100)), ["holiday_beach.avi", "holiday_mountain.mkv"])

    def test_by_folder_prefix(self):
        self.assertEqual(self.names(query(self.conn, folder_prefix=self.library / "family")),
                         ["birthday_cake.mpg", "birthday_party.wmv"])
        self.assertEqual(len(query(self.conn)), 4)

    def test_row_shape(self):
        row = query(self.conn, min_score=90)[0]
        self.assertEqual(row["source_path"], str(self.holidays / "holiday_beach.avi"))
        self.assertEqual(row["output_path"], str(self.holidays / "holiday_beach.mp4"))
        self.assertEqual(row["flags"], [])
        self.assertTrue(row["from_index"])
        self.assertEqual(row["record"]["id"], self.records["holiday_beach.avi"]["id"])


class TestFind(IndexTestCase):

    def add_shared_prefix_pair(self):
        self.add_source(self.holidays / "holiday_lake.avi", "planned", None, "remux",
                        record_id="abcdef01-1111-4111-8111-111111111111")
        self.add_source(self.birthdays / "birthday_games.avi", "planned", None, "remux",
                        record_id="abcdef01-2222-4222-8222-222222222222")

    def test_full_id(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        beach = self.records["holiday_beach.avi"]
        rows = find(conn, beach["id"], progress_say=quiet_say)
        self.assertEqual([row["id"] for row in rows], [beach["id"]])
        self.assertTrue(rows[0]["from_index"])

    def test_unique_prefix_case_insensitive(self):
        conn = self.open()
        self.add_shared_prefix_pair()
        index_tree(conn, self.library, progress_say=quiet_say)
        rows = find(conn, "ABCDEF01-1111", progress_say=quiet_say)
        self.assertEqual([row["source_name"] for row in rows], ["holiday_lake.avi"])

    def test_ambiguous_prefix_returns_all(self):
        conn = self.open()
        self.add_shared_prefix_pair()
        index_tree(conn, self.library, progress_say=quiet_say)
        rows = find(conn, "abcdef01", progress_say=quiet_say)
        self.assertEqual(sorted(row["source_name"] for row in rows), ["birthday_games.avi", "holiday_lake.avi"])

    def test_fallback_without_database_walks_roots(self):
        self.add_shared_prefix_pair()
        rows = find(None, "abcdef01-2222", roots=(self.library,), progress_say=quiet_say)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["from_index"])
        self.assertEqual(rows[0]["source_path"], str(self.birthdays / "birthday_games.avi"))
        self.assertEqual(rows[0]["record_path"], str(record_path(self.birthdays / "birthday_games.avi")))

    def test_fallback_overlapping_roots_do_not_duplicate(self):
        beach = self.records["holiday_beach.avi"]
        rows = find(None, beach["id"], roots=(self.library, self.holidays), progress_say=quiet_say)
        self.assertEqual(len(rows), 1)

    def test_fallback_when_index_lacks_id(self):
        conn = self.open()
        index_tree(conn, self.holidays, progress_say=quiet_say)
        cake = self.records["birthday_cake.mpg"]
        rows = find(conn, cake["id"], roots=(self.library,), progress_say=quiet_say)
        self.assertEqual([row["id"] for row in rows], [cake["id"]])
        self.assertFalse(rows[0]["from_index"])

    def test_fallback_defaults_to_current_directory(self):
        beach = self.records["holiday_beach.avi"]
        previous = os.getcwd()
        os.chdir(str(self.library))
        self.addCleanup(os.chdir, previous)
        rows = find(None, beach["id"][:13], progress_say=quiet_say)
        self.assertEqual([row["id"] for row in rows], [beach["id"]])

    def test_no_match_and_empty_id(self):
        self.assertEqual(find(None, "ffffffff-none", roots=(self.library,), progress_say=quiet_say), [])
        with self.assertRaises(ValueError):
            find(None, "  ", roots=(self.library,), progress_say=quiet_say)


class TestSubcommands(IndexTestCase):

    def parse(self, adder, argv):
        parser = argparse.ArgumentParser()
        adder(parser)
        return parser.parse_args(argv)

    def run_captured(self, function, args):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = function(args)
        return code, output.getvalue(), errors.getvalue()

    def test_index_subcommand(self):
        args = self.parse(add_arguments, ["--db", str(self.db_path), str(self.holidays), str(self.birthdays)])
        code, output, _errors = self.run_captured(run, args)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("4 records from 2 folders, 0 removed", output)
        self.assertIn("'{}'".format(self.holidays), output)
        self.assertIn("'{}'".format(self.db_path), output)
        args = self.parse(add_arguments, ["--rebuild", "--db", str(self.db_path), str(self.holidays)])
        code, output, _errors = self.run_captured(run, args)
        self.assertEqual(code, EXIT_OK)
        conn = open_index(self.db_path, create=False)
        self.addCleanup(conn.close)
        self.assertEqual(self.row_count(conn), 2)

    def test_index_subcommand_rejects_missing_path(self):
        missing = Path(self.workspace.name) / "nowhere"
        args = self.parse(add_arguments, ["--db", str(self.db_path), str(missing)])
        code, _output, errors = self.run_captured(run, args)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("'{}'".format(missing), errors)
        self.assertFalse(self.db_path.exists())

    def test_find_subcommand_through_index(self):
        args = self.parse(add_arguments, ["--db", str(self.db_path), str(self.library)])
        self.run_captured(run, args)
        beach = self.records["holiday_beach.avi"]
        args = self.parse(add_find_arguments, [beach["id"][:8], "--db", str(self.db_path)])
        code, output, _errors = self.run_captured(run_find, args)
        self.assertEqual(code, EXIT_OK)
        lines = output.splitlines()
        self.assertEqual(lines[0], "id: {}".format(beach["id"]))
        self.assertIn("status: verified", lines)
        self.assertIn("score: 95", lines)
        self.assertIn("action: remux", lines)
        self.assertIn("source: {}".format(self.holidays / "holiday_beach.avi"), lines)
        self.assertIn("output: {}".format(self.holidays / "holiday_beach.mp4"), lines)
        self.assertIn("record: {}".format(record_path(self.holidays / "holiday_beach.avi")), lines)

    def test_find_subcommand_ambiguous_and_none(self):
        self.add_source(self.holidays / "holiday_lake.avi", "planned", None, "remux",
                        record_id="abcdef01-1111-4111-8111-111111111111")
        self.add_source(self.birthdays / "birthday_games.avi", "planned", None, "remux",
                        record_id="abcdef01-2222-4222-8222-222222222222")
        missing_db = Path(self.workspace.name) / "absent.sqlite"
        args = self.parse(add_find_arguments, ["abcdef01", "--db", str(missing_db), "--root", str(self.library)])
        code, output, errors = self.run_captured(run_find, args)
        self.assertEqual(code, EXIT_ERROR)
        self.assertEqual(output.count("id: abcdef01-"), 2)
        self.assertIn("ambiguous", errors)
        self.assertIn("no index at '{}'".format(missing_db), errors)
        self.assertFalse(missing_db.exists())
        args = self.parse(add_find_arguments, ["ffffffff", "--db", str(missing_db), "--root", str(self.library)])
        code, output, errors = self.run_captured(run_find, args)
        self.assertEqual(code, EXIT_ERROR)
        self.assertEqual(output, "")
        self.assertIn("no record matches 'ffffffff'", errors)

    def test_find_notes_stale_index_row(self):
        conn = self.open()
        index_tree(conn, self.library, progress_say=quiet_say)
        conn.close()
        cake = self.records["birthday_cake.mpg"]
        record_path(self.birthdays / "birthday_cake.mpg").unlink()
        args = self.parse(add_find_arguments, [cake["id"], "--db", str(self.db_path)])
        code, output, _errors = self.run_captured(run_find, args)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("note: record file not found", output)


if __name__ == "__main__":
    unittest.main()

# End of File #
