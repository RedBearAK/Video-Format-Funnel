"""Tests for vidfunnel.retire: every guard, quarantine, interim rename, destroy confirmation, run."""

VERSION = "20261110"

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vidfunnel.record as record_module
from vidfunnel import retire
from vidfunnel.record import (
    FolderLock,
    add_flag,
    cheap_fingerprint,
    load_record,
    new_record,
    record_path,
    save_record,
    set_status,
    sha256_file,
)
from vidfunnel.retire import (
    GUARDS,
    RetireRefused,
    add_arguments,
    check_eligible,
    confirm_destroy,
    quarantine_destination,
    retire_one,
    run,
)

SOURCE_BYTES = b"legacy video payload " * 200        # 4200 bytes
OUTPUT_BYTES = b"converted mp4 payload " * 100       # 2200 bytes, smaller than the source
MIB = 1024 * 1024
LARGE_SOURCE = bytes(range(256)) * (3 * MIB // 256)  # 3 MiB: a middle that the first and last MiB do not cover
SECOND_NS = 1000 * 1000 * 1000

# The time a source shows on the planning host, and what other mounts can show for it.
PLANNED_MTIME_NS = 1700000001987654321
DAYS_LATER_NS = PLANNED_MTIME_NS + 3 * 86400 * SECOND_NS
STEPS = (("hundred_nanoseconds", 100), ("microsecond", 1000), ("millisecond", 1000 * 1000),
         ("ten_milliseconds", 10 * 1000 * 1000), ("second", SECOND_NS), ("two_seconds", 2 * SECOND_NS))


def truncated(value, step):
    return value - value % step


# (word, recorded time, seen time): one is the other truncated to a coarser step, in both directions.
COARSER_VIEWS = ([("seen_" + word, PLANNED_MTIME_NS, truncated(PLANNED_MTIME_NS, step)) for word, step in STEPS]
                 + [("recorded_" + word, truncated(PLANNED_MTIME_NS, step), PLANNED_MTIME_NS) for word, step in STEPS])
# (word, recorded time, seen time): differences that no truncation explains.
OTHER_TIMES = (
    ("second_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + SECOND_NS),
    ("second_and_half_later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * SECOND_NS // 2),
    ("days_later", PLANNED_MTIME_NS, DAYS_LATER_NS),
)


class TerminalInput(io.StringIO):
    """stdin that claims to be a terminal."""

    def isatty(self):
        return True


class PipeInput(io.StringIO):
    """stdin that is not a terminal and must never be read."""

    def isatty(self):
        return False

    def readline(self, *ignored):
        raise AssertionError("stdin was read")


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "wb") as stream:
        stream.write(data)
    return path


def make_verified(source, output_on_disk, final_name, interim_name=None, score=95, flags=(),
                  source_bytes=SOURCE_BYTES, output_bytes=OUTPUT_BYTES):
    """Write a source and its output, and a 'verified' record for them as plan/convert/verify would."""
    write_bytes(source, source_bytes)
    write_bytes(output_on_disk, output_bytes)
    record = new_record(source)
    set_status(record, "planned")
    record["output"]["name"] = final_name
    record["output"]["interim_name"] = interim_name
    record["output"]["sha256"] = sha256_file(output_on_disk)
    record["output"]["size"] = os.stat(str(output_on_disk)).st_size
    record["score"] = score
    for flag in flags:
        add_flag(record, flag)
    set_status(record, "verified")
    save_record(source, record)
    return record


def snapshot(base):
    """Every file under base (relative path -> bytes), excluding the record directories' lock-free JSON."""
    files = {}
    for current, _dirs, names in os.walk(str(base)):
        for name in names:
            full = Path(current) / name
            files[str(full.relative_to(base))] = full.read_bytes()
    return files


class RetireCase(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="vidfunnel-retire-test-")
        self.root = Path(self.holder.name) / "library"
        self.holidays = self.root / "holidays"
        self.meetings = self.root / "meetings"
        self.beach_source = self.holidays / "holiday_beach.avi"
        self.beach_output = self.holidays / "holiday_beach.mp4"
        self.meeting_source = self.meetings / "client_meeting.mp4"
        self.meeting_interim = self.meetings / "client_meeting.vidfunnel.mp4"
        self.beach_record = make_verified(self.beach_source, self.beach_output, "holiday_beach.mp4")
        self.meeting_record = make_verified(self.meeting_source, self.meeting_interim, "client_meeting.mp4",
                                            interim_name="client_meeting.vidfunnel.mp4")
        self.quarantine = self.root / ".vidfunnel-retired"

    def tearDown(self):
        self.holder.cleanup()

    def quietly(self):
        """Patch stdout and stderr; returns (stdout, stderr) StringIO objects."""
        out = io.StringIO()
        err = io.StringIO()
        out_patch = mock.patch.object(sys, "stdout", out)
        err_patch = mock.patch.object(sys, "stderr", err)
        out_patch.start()
        err_patch.start()
        self.addCleanup(out_patch.stop)
        self.addCleanup(err_patch.stop)
        return out, err

    def assert_refused(self, guard, source, record, **options):
        before = snapshot(self.root)
        with self.assertRaises(RetireRefused) as caught:
            retire_one(source, record, self.root, **options)
        self.assertEqual(caught.exception.reason, guard, caught.exception.detail)
        self.assertEqual(snapshot(self.root), before, "files changed although retire refused")
        return caught.exception


class GuardTests(RetireCase):
    def test_guard_codes_are_the_published_order(self):
        self.assertEqual(GUARDS, ("no-record", "status-not-verified", "source-changed", "output-missing",
                                  "score-hold", "score-review", "output-larger", "output-hash-mismatch",
                                  "interim-rename-blocked", "quarantine-exists"))

    def test_cheap_guards_run_before_the_output_hash(self):
        write_bytes(self.beach_output, b"tampered output")
        self.beach_record["score"] = 75
        with mock.patch.object(retire, "sha256_file") as hasher:
            self.assert_refused("score-review", self.beach_source, self.beach_record)
            add_flag(self.beach_record, "output-larger")
            self.assert_refused("output-larger", self.beach_source, self.beach_record, allow_review=True)
        hasher.assert_not_called()
        self.beach_record["flags"].remove("output-larger")
        self.assert_refused("output-hash-mismatch", self.beach_source, self.beach_record, allow_review=True)

    def test_no_record(self):
        self.assert_refused("no-record", self.beach_source, None)

    def test_record_of_another_file_is_no_record(self):
        self.assert_refused("no-record", self.beach_source, self.meeting_record)

    def test_output_name_with_a_directory_part_is_no_record(self):
        self.beach_record["output"]["name"] = "../holiday_beach.mp4"
        self.assert_refused("no-record", self.beach_source, self.beach_record)

    def test_status_not_verified(self):
        set_status(self.beach_record, "converted")
        self.assert_refused("status-not-verified", self.beach_source, self.beach_record)

    def test_source_modified_after_planning(self):
        with open(str(self.beach_source), "ab") as stream:
            stream.write(b"x")
        self.assert_refused("source-changed", self.beach_source, self.beach_record)

    def test_source_missing(self):
        self.beach_source.unlink()
        self.assert_refused("source-changed", self.beach_source, self.beach_record)

    def test_output_missing(self):
        self.beach_output.unlink()
        self.assert_refused("output-missing", self.beach_source, self.beach_record)

    def test_output_hash_mismatch_same_size(self):
        data = bytearray(OUTPUT_BYTES)
        data[100] ^= 0xFF
        write_bytes(self.beach_output, bytes(data))
        self.assert_refused("output-hash-mismatch", self.beach_source, self.beach_record)

    def test_output_hash_mismatch_other_size(self):
        write_bytes(self.beach_output, OUTPUT_BYTES + b"tail")
        self.assert_refused("output-hash-mismatch", self.beach_source, self.beach_record)

    def test_record_without_output_hash(self):
        self.beach_record["output"]["sha256"] = None
        self.assert_refused("output-hash-mismatch", self.beach_source, self.beach_record)

    def test_score_hold(self):
        self.beach_record["score"] = 59
        self.assert_refused("score-hold", self.beach_source, self.beach_record)
        self.assert_refused("score-hold", self.beach_source, self.beach_record, allow_review=True)

    def test_missing_score_is_hold(self):
        self.beach_record["score"] = None
        self.assert_refused("score-hold", self.beach_source, self.beach_record)

    def test_score_review_needs_allow_review(self):
        self.beach_record["score"] = 75
        self.assert_refused("score-review", self.beach_source, self.beach_record)
        stage = retire_one(self.beach_source, self.beach_record, self.root, allow_review=True)
        self.assertEqual(stage["band"], "review")
        self.assertFalse(self.beach_source.exists())
        self.assertTrue((self.quarantine / "holidays" / "holiday_beach.avi").is_file())

    def test_output_larger_flag_is_refused_without_allow_larger(self):
        add_flag(self.beach_record, "output-larger")
        refusal = self.assert_refused("output-larger", self.beach_source, self.beach_record)
        self.assertIn("--allow-larger", refusal.detail)

    def test_output_larger_flag_is_retired_with_allow_larger(self):
        add_flag(self.beach_record, "output-larger")
        retire_one(self.beach_source, self.beach_record, self.root, allow_larger=True)
        self.assertFalse(self.beach_source.exists())
        self.assertTrue((self.quarantine / "holidays" / "holiday_beach.avi").is_file())
        saved = load_record(self.beach_source)
        self.assertEqual(saved["status"], "retired")
        self.assertIn("allowed-larger", saved["flags"])

    def test_output_larger_than_source_without_flag(self):
        bigger = SOURCE_BYTES + b"more"
        write_bytes(self.beach_output, bigger)
        self.beach_record["output"]["sha256"] = sha256_file(self.beach_output)
        self.beach_record["output"]["size"] = len(bigger)
        self.beach_record["action"] = "reencode"
        refusal = self.assert_refused("output-larger", self.beach_source, self.beach_record)
        self.assertIn("'holiday_beach.mp4' ({} bytes)".format(len(bigger)), refusal.detail)
        self.assertIn("({} bytes)".format(len(SOURCE_BYTES)), refusal.detail)
        self.assertIn("--allow-larger", refusal.detail)
        retire_one(self.beach_source, self.beach_record, self.root, allow_larger=True)
        self.assertFalse(self.beach_source.exists())
        self.assertIn("allowed-larger", load_record(self.beach_source)["flags"])

    def test_smaller_output_gets_no_allowed_larger_flag(self):
        retire_one(self.beach_source, self.beach_record, self.root, allow_larger=True)
        self.assertNotIn("allowed-larger", load_record(self.beach_source)["flags"])

    def test_remux_larger_than_source_is_eligible(self):
        bigger = SOURCE_BYTES + b"more"
        write_bytes(self.beach_output, bigger)
        self.beach_record["output"]["sha256"] = sha256_file(self.beach_output)
        self.beach_record["output"]["size"] = len(bigger)
        self.beach_record["action"] = "remux"
        result = check_eligible(self.beach_source, self.beach_record)
        self.assertEqual(result["band"], "ok")

    def test_interim_rename_blocked(self):
        footage_source = self.meetings / "client_footage.m4v"
        footage_interim = self.meetings / "client_footage.vidfunnel.mp4"
        footage_record = make_verified(footage_source, footage_interim, "client_footage.mp4",
                                       interim_name="client_footage.vidfunnel.mp4")
        write_bytes(self.meetings / "client_footage.mp4", b"someone else's file")
        self.assert_refused("interim-rename-blocked", footage_source, footage_record)

    def test_quarantine_exists(self):
        write_bytes(self.quarantine / "holidays" / "holiday_beach.avi", b"an older original")
        self.assert_refused("quarantine-exists", self.beach_source, self.beach_record)

    def test_check_eligible_returns_paths(self):
        result = check_eligible(self.meeting_source, self.meeting_record)
        self.assertEqual(result, {"output_path": self.meeting_interim,
                                  "final_output_path": self.meeting_source,
                                  "band": "ok", "sha256_verified": True,
                                  "members": [self.meeting_source], "larger": False, "sidecars": [],
                                  "source_mtime_differs": [], "source_sha256_checked": False})

    def test_large_output_hash_shows_progress(self):
        large = b"\0" * (33 * 1024 * 1024)
        large_source = self.holidays / "harbour_night.avi"
        large_output = self.holidays / "harbour_night.mp4"
        record = make_verified(large_source, large_output, "harbour_night.mp4",
                               source_bytes=large + b"source", output_bytes=large)
        out, _err = self.quietly()
        check_eligible(large_source, record)
        self.assertIn("hash 'harbour_night.mp4'", out.getvalue())


class SourceTimeTests(RetireCase):
    """Content first; a coarser view of the recorded time reads nothing; any other time is settled by a read."""

    def setUp(self):
        RetireCase.setUp(self)
        self.out, _err = self.quietly()

    def film(self, word, recorded_ns=PLANNED_MTIME_NS, recorded_hash=True):
        """A verified 3 MiB source '<word>_film.avi' fingerprinted at recorded_ns; (source, record)."""
        source = self.holidays / "{}_film.avi".format(word)
        output = self.holidays / "{}_film.mp4".format(word)
        record = make_verified(source, output, output.name, source_bytes=LARGE_SOURCE)
        os.utime(str(source), ns=(recorded_ns, recorded_ns))
        record["source"]["fingerprint"] = cheap_fingerprint(source)
        record["source"]["sha256"] = sha256_file(source) if recorded_hash else None
        save_record(source, record)
        return source, record

    def changed_middle(self, source, seen_ns):
        content = bytearray(LARGE_SOURCE)
        content[len(content) // 2] ^= 0xFF
        source.write_bytes(bytes(content))
        os.utime(str(source), ns=(seen_ns, seen_ns))
        return bytes(content)

    def retire_watching_the_hash(self, source, record):
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            stage = retire_one(source, record, self.root)
        return stage, hasher

    def test_a_coarser_view_of_the_time_retires_without_a_read(self):
        for word, recorded_ns, seen_ns in COARSER_VIEWS:
            with self.subTest(case=word):
                source, record = self.film(word, recorded_ns)
                os.utime(str(source), ns=(seen_ns, seen_ns))
                stage, hasher = self.retire_watching_the_hash(source, record)
                hasher.assert_not_called()
                self.assertFalse(source.exists())
                self.assertTrue((self.quarantine / "holidays" / source.name).is_file())
                self.assertEqual(stage["source_mtime_differs"],
                                 [{"name": source.name, "recorded": recorded_ns, "seen": seen_ns}])
                self.assertIs(stage["source_sha256_checked"], False)
                self.assertEqual(load_record(source)["status"], "retired")

    def test_another_time_with_the_same_content_is_read_once_and_retired(self):
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                source, record = self.film(word, recorded_ns)
                os.utime(str(source), ns=(seen_ns, seen_ns))
                stage, hasher = self.retire_watching_the_hash(source, record)
                self.assertEqual(hasher.call_count, 1)
                self.assertEqual(hasher.call_args[0][0], (os.path.abspath(str(source)),))
                self.assertIs(stage["source_sha256_checked"], True)
                self.assertEqual(stage["source_mtime_differs"],
                                 [{"name": source.name, "recorded": recorded_ns, "seen": seen_ns}])
                self.assertTrue((self.quarantine / "holidays" / source.name).is_file())

    def test_another_time_with_a_changed_middle_is_refused(self):
        for word, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=word):
                source, record = self.film("edited_" + word, recorded_ns)
                content = self.changed_middle(source, seen_ns)
                seen = cheap_fingerprint(source)
                for key in ("size", "head_sha256", "tail_sha256"):
                    self.assertEqual(seen[key], record["source"]["fingerprint"][key], key)
                record_text = json.dumps(record, sort_keys=True)
                record_file = source.parent / ".vidfunnel" / (source.name + ".json")
                record_bytes = record_file.read_bytes()
                with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
                    refusal = self.assert_refused("source-changed", source, record)
                self.assertEqual(hasher.call_count, 1)
                self.assertEqual(refusal.detail, (
                    "SHA-256 of '{}' is {}, the record says {}; the output '{}' was made from the earlier content; "
                    "to convert the source as it is now, move '{}' away and run: vidfunnel plan '{}'").format(
                        source.name, hashlib.sha256(content).hexdigest(), record["source"]["sha256"],
                        record["output"]["name"], record["output"]["name"], source))
                self.assertTrue(source.is_file())
                self.assertEqual(json.dumps(record, sort_keys=True), record_text)
                self.assertEqual(record_file.read_bytes(), record_bytes)

    def test_a_changed_middle_is_refused_under_destroy_and_an_unchanged_one_read_once(self):
        edited, _record = self.film("edited_destroyed")
        self.changed_middle(edited, PLANNED_MTIME_NS + SECOND_NS)
        kept, _record = self.film("kept_destroyed")
        os.utime(str(kept), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["--destroy-originals-now", str(self.holidays)])
        args.quiet = False
        args.verbose = False
        args.log = None
        args.ffmpeg = None
        args.yes = True
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            run(args)
        self.assertTrue(edited.is_file())
        self.assertIn("'{}' refused (source-changed): SHA-256 of '{}'".format(edited.name, edited.name),
                      self.out.getvalue())
        self.assertFalse(kept.exists())
        # Each source is checked for the confirmation list and again before it is unlinked; one read each.
        self.assertEqual(sorted(call[0][0][0] for call in hasher.call_args_list),
                         sorted([os.path.abspath(str(edited)), os.path.abspath(str(kept))]))

    def test_another_time_without_a_recorded_source_hash_is_refused(self):
        source, record = self.film("unhashed", recorded_hash=False)
        os.utime(str(source), ns=(DAYS_LATER_NS, DAYS_LATER_NS))
        refusal = self.assert_refused("source-changed", source, record)
        self.assertIn("no source SHA-256", refusal.detail)

    def test_an_unusable_recorded_time_reads_the_whole_source(self):
        source, record = self.film("edited_time")
        record["source"]["fingerprint"]["mtime_ns"] = "yesterday"
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            eligibility = check_eligible(source, record)
        self.assertEqual(hasher.call_count, 1)
        self.assertTrue(eligibility["source_sha256_checked"])

    def test_the_same_time_reads_nothing_and_notes_nothing(self):
        source, record = self.film("unmoved")
        stage, hasher = self.retire_watching_the_hash(source, record)
        hasher.assert_not_called()
        self.assertNotIn("source_mtime_differs", stage)
        self.assertNotIn("source_sha256_checked", stage)

    def test_a_full_read_is_announced(self):
        source, record = self.film("announced")
        os.utime(str(source), ns=(DAYS_LATER_NS, DAYS_LATER_NS))
        retire_one(source, record, self.root)
        self.assertIn("  reading '{}' in full (3.0 MiB): its modification time is neither the recorded one nor a "
                      "coarser view of it; comparing its SHA-256 with the record's".format(source.name),
                      self.out.getvalue())


class QuarantineDestinationTests(RetireCase):
    def test_default_location_keeps_relative_path(self):
        self.assertEqual(quarantine_destination(self.beach_source, self.root),
                         self.quarantine / "holidays" / "holiday_beach.avi")

    def test_custom_location(self):
        custom = Path(self.holder.name) / "retired_elsewhere"
        self.assertEqual(quarantine_destination(self.beach_source, self.root, custom),
                         custom / "holidays" / "holiday_beach.avi")

    def test_source_outside_root(self):
        with self.assertRaises(ValueError):
            quarantine_destination(self.beach_source, self.meetings)

    def test_source_inside_quarantine(self):
        with self.assertRaises(ValueError):
            quarantine_destination(self.quarantine / "holidays" / "holiday_beach.avi", self.root)


class RetireOneTests(RetireCase):
    def test_happy_path(self):
        output_before = self.beach_output.read_bytes()
        stage = retire_one(self.beach_source, self.beach_record, self.root)
        destination = self.quarantine / "holidays" / "holiday_beach.avi"
        self.assertFalse(self.beach_source.exists())
        self.assertEqual(destination.read_bytes(), SOURCE_BYTES)
        self.assertEqual(self.beach_output.read_bytes(), output_before)
        saved = load_record(self.beach_source)
        self.assertTrue(record_path(self.beach_source).is_file())
        self.assertEqual(saved["status"], "retired")
        self.assertEqual(saved["stages"]["retire"]["mode"], "quarantine")
        self.assertEqual(saved["stages"]["retire"]["destination"], str(destination))
        self.assertEqual(saved["stages"]["retire"]["method"], "rename")
        self.assertFalse(saved["stages"]["retire"]["renamed_output"])
        self.assertEqual(saved["stages"]["retire"]["score"], 95)
        self.assertEqual(saved["stages"]["retire"]["band"], "ok")
        self.assertEqual(stage["mode"], "quarantine")
        self.assertIn("at", stage)

    def test_interim_rename(self):
        stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        self.assertTrue(stage["renamed_output"])
        self.assertEqual((self.quarantine / "meetings" / "client_meeting.mp4").read_bytes(), SOURCE_BYTES)
        self.assertFalse(self.meeting_interim.exists())
        self.assertEqual(self.meeting_source.read_bytes(), OUTPUT_BYTES)
        saved = load_record(self.meeting_source)
        self.assertEqual(saved["status"], "retired")
        self.assertEqual(saved["output"]["name"], "client_meeting.mp4")
        self.assertEqual(saved["output"]["interim_name"], "client_meeting.vidfunnel.mp4")

    def add_interim_par2(self):
        names = ["client_meeting.vidfunnel.mp4.par2", "client_meeting.vidfunnel.mp4.vol000+200.par2"]
        for name in names:
            write_bytes(self.meetings / name, b"fake par2 for the interim name")
        return names

    @unittest.skipIf(shutil.which("par2") is None, "par2 is not on PATH; the recreate path is not run")
    def test_interim_par2_recreated_for_final_name(self):
        old_names = self.add_interim_par2()
        self.meeting_record["stages"]["plan"] = {"par2": 5}
        out, _err = self.quietly()
        stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        for name in old_names:
            self.assertFalse((self.meetings / name).exists(), name)
        self.assertTrue((self.meetings / "client_meeting.mp4.par2").is_file())
        self.assertIn("client_meeting.mp4.par2", stage["par2_recreated"])
        for name in stage["par2_recreated"]:
            self.assertTrue((self.meetings / name).is_file(), name)
            self.assertTrue(name.startswith("client_meeting.mp4."), name)
        saved = load_record(self.meeting_source)
        self.assertNotIn("par2-stale", saved["flags"])
        self.assertEqual(saved["stages"]["retire"]["par2_recreated"], stage["par2_recreated"])
        self.assertEqual(self.meeting_source.read_bytes(), OUTPUT_BYTES)

    def test_interim_par2_kept_and_flagged_without_par2(self):
        old_names = self.add_interim_par2()
        self.quietly()
        with mock.patch.object(retire.shutil, "which", return_value=None):
            stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        for name in old_names:
            self.assertTrue((self.meetings / name).is_file(), name)
        self.assertEqual(stage["par2_recreated"], [])
        self.assertEqual(stage["par2_stale"], old_names)
        self.assertIn("not on PATH", stage["par2_error"])
        saved = load_record(self.meeting_source)
        self.assertIn("par2-stale", saved["flags"])
        self.assertEqual(saved["status"], "retired")

    def test_existing_final_name_par2_is_not_overwritten(self):
        old_names = self.add_interim_par2()
        users_par2 = write_bytes(self.meetings / "client_meeting.mp4.par2", b"the user's own par2")
        self.quietly()
        stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        self.assertEqual(users_par2.read_bytes(), b"the user's own par2")
        for name in old_names:
            self.assertTrue((self.meetings / name).is_file(), name)
        self.assertIn("already exist", stage["par2_error"])
        self.assertIn("par2-stale", load_record(self.meeting_source)["flags"])

    def test_no_par2_fields_without_interim_par2(self):
        stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        self.assertNotIn("par2_recreated", stage)

    def test_custom_quarantine_dir(self):
        custom = Path(self.holder.name) / "retired_elsewhere"
        stage = retire_one(self.beach_source, self.beach_record, self.root, quarantine_dir=custom)
        self.assertEqual((custom / "holidays" / "holiday_beach.avi").read_bytes(), SOURCE_BYTES)
        self.assertFalse(self.quarantine.exists())
        self.assertEqual(stage["destination"], str(custom / "holidays" / "holiday_beach.avi"))

    def test_copy_across_filesystems(self):
        out, _err = self.quietly()
        with mock.patch.object(retire, "_same_filesystem", return_value=False):
            stage = retire_one(self.beach_source, self.beach_record, self.root)
        destination = self.quarantine / "holidays" / "holiday_beach.avi"
        self.assertEqual(stage["method"], "copy")
        self.assertEqual(destination.read_bytes(), SOURCE_BYTES)
        self.assertFalse(self.beach_source.exists())
        self.assertEqual(os.listdir(str(destination.parent)), ["holiday_beach.avi"])
        self.assertIn("another filesystem", out.getvalue())

    def test_destroy_unlinks_and_keeps_output(self):
        stage = retire_one(self.beach_source, self.beach_record, self.root, destroy=True)
        self.assertEqual(stage["mode"], "destroy")
        self.assertIsNone(stage["destination"])
        self.assertFalse(self.beach_source.exists())
        self.assertFalse(self.quarantine.exists())
        self.assertEqual(self.beach_output.read_bytes(), OUTPUT_BYTES)
        self.assertEqual(load_record(self.beach_source)["status"], "retired")

    def test_second_retire_is_refused(self):
        retire_one(self.beach_source, self.beach_record, self.root)
        self.assert_refused("status-not-verified", self.beach_source, load_record(self.beach_source))


class Par2RedundancyTests(unittest.TestCase):
    def test_convert_value_overrides_plan_value(self):
        record = {"stages": {"plan": {"par2": 5}, "convert": {"par2_redundancy": 20}}}
        self.assertEqual(retire._par2_redundancy(record), 20)

    def test_plan_value_when_convert_has_none(self):
        record = {"stages": {"plan": {"par2": 5}, "convert": {"par2_redundancy": None}}}
        self.assertEqual(retire._par2_redundancy(record), 5)

    def test_default_without_either(self):
        self.assertEqual(retire._par2_redundancy({"stages": {}}), retire.PAR2_DEFAULT_REDUNDANCY)


class Par2RecreateRedundancyTests(RetireCase):
    def test_recreated_par2_uses_and_records_convert_redundancy(self):
        for name in ("client_meeting.vidfunnel.mp4.par2", "client_meeting.vidfunnel.mp4.vol000+200.par2"):
            write_bytes(self.meetings / name, b"fake par2 for the interim name")
        self.meeting_record["stages"]["plan"] = {"par2": 5}
        self.meeting_record["stages"]["convert"] = {"par2_redundancy": 20}
        used = []

        def fake_write_par2(output_path, redundancy, options):
            used.append(redundancy)
            return [write_bytes(Path(str(output_path) + ".par2"), b"new par2")]

        from vidfunnel import encode
        with mock.patch.object(retire.shutil, "which", return_value="/usr/bin/par2"), \
                mock.patch.object(encode, "write_par2", side_effect=fake_write_par2):
            stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        self.assertEqual(used, [20])
        self.assertEqual(stage["par2_redundancy"], 20)
        self.assertEqual(load_record(self.meeting_source)["stages"]["retire"]["par2_redundancy"], 20)


class VobGroupTests(RetireCase):
    """A 'vob-group' record (plan's shape): every member is checked and retired, not only the first."""

    def setUp(self):
        RetireCase.setUp(self)
        self.dvd = self.root / "dvd_rip"
        self.first_member = write_bytes(self.dvd / "VTS_01_1.VOB", b"first vob payload " * 300)
        self.second_member = write_bytes(self.dvd / "VTS_01_2.VOB", b"second vob payload " * 300)
        self.group_output = write_bytes(self.dvd / "VTS_01.mp4", OUTPUT_BYTES)
        record = new_record(self.first_member)
        record["source"].update(
            kind="vob-group",
            members=[{"name": path.name, "fingerprint": cheap_fingerprint(path)}
                     for path in (self.first_member, self.second_member)],
            size=self.first_member.stat().st_size + self.second_member.stat().st_size)
        set_status(record, "planned")
        record["action"] = "reencode"
        record["output"]["name"] = "VTS_01.mp4"
        record["output"]["sha256"] = sha256_file(self.group_output)
        record["output"]["size"] = self.group_output.stat().st_size
        record["score"] = 95
        set_status(record, "verified")
        save_record(self.first_member, record)
        self.group_record = record

    def test_every_member_is_quarantined(self):
        stage = retire_one(self.first_member, self.group_record, self.root)
        first_target = self.quarantine / "dvd_rip" / "VTS_01_1.VOB"
        second_target = self.quarantine / "dvd_rip" / "VTS_01_2.VOB"
        self.assertFalse(self.first_member.exists())
        self.assertFalse(self.second_member.exists())
        self.assertEqual(first_target.read_bytes(), b"first vob payload " * 300)
        self.assertEqual(second_target.read_bytes(), b"second vob payload " * 300)
        self.assertEqual(stage["destinations"], [str(first_target), str(second_target)])
        self.assertEqual(stage["destination"], str(first_target))
        saved = load_record(self.first_member)
        self.assertEqual(saved["status"], "retired")
        self.assertEqual(saved["stages"]["retire"]["destinations"], [str(first_target), str(second_target)])
        self.assertEqual(self.group_output.read_bytes(), OUTPUT_BYTES)

    def test_every_member_is_destroyed(self):
        stage = retire_one(self.first_member, self.group_record, self.root, destroy=True)
        self.assertFalse(self.first_member.exists())
        self.assertFalse(self.second_member.exists())
        self.assertEqual(stage["destinations"], [None, None])

    def test_a_member_time_days_away_hashes_the_members_in_order(self):
        concatenated = self.first_member.read_bytes() + self.second_member.read_bytes()
        self.group_record["source"]["sha256"] = hashlib.sha256(concatenated).hexdigest()
        os.utime(str(self.second_member), ns=(DAYS_LATER_NS, DAYS_LATER_NS))
        self.quietly()
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            stage = retire_one(self.first_member, self.group_record, self.root)
        self.assertEqual(hasher.call_count, 1)
        self.assertEqual(hasher.call_args[0][0], (os.path.abspath(str(self.first_member)),
                                                  os.path.abspath(str(self.second_member))))
        self.assertTrue(stage["source_sha256_checked"])
        self.assertEqual([entry["name"] for entry in stage["source_mtime_differs"]], ["VTS_01_2.VOB"])
        self.assertFalse(self.second_member.exists())

    def test_a_member_time_days_away_with_another_group_hash_is_refused(self):
        reversed_order = self.second_member.read_bytes() + self.first_member.read_bytes()
        self.group_record["source"]["sha256"] = hashlib.sha256(reversed_order).hexdigest()
        os.utime(str(self.second_member), ns=(DAYS_LATER_NS, DAYS_LATER_NS))
        self.quietly()
        refusal = self.assert_refused("source-changed", self.first_member, self.group_record)
        self.assertIn("SHA-256 of 'VTS_01_1.VOB'", refusal.detail)

    def test_changed_second_member_is_refused(self):
        write_bytes(self.second_member, b"edited second vob " * 300)
        refusal = self.assert_refused("source-changed", self.first_member, self.group_record)
        self.assertIn("VTS_01_2.VOB", refusal.detail)

    def test_missing_second_member_is_refused(self):
        os.unlink(str(self.second_member))
        refusal = self.assert_refused("source-changed", self.first_member, self.group_record)
        self.assertIn("VTS_01_2.VOB", refusal.detail)

    def test_output_larger_compares_with_the_group_size(self):
        larger_than_first = b"x" * (self.first_member.stat().st_size + 100)
        write_bytes(self.group_output, larger_than_first)
        self.group_record["output"]["sha256"] = sha256_file(self.group_output)
        self.group_record["output"]["size"] = len(larger_than_first)
        stage = retire_one(self.first_member, self.group_record, self.root)
        self.assertEqual(len(stage["destinations"]), 2)
        self.assertNotIn("allowed-larger", load_record(self.first_member)["flags"])

    def test_output_larger_than_the_group_needs_allow_larger(self):
        group_size = self.first_member.stat().st_size + self.second_member.stat().st_size
        larger_than_group = b"x" * (group_size + 100)
        write_bytes(self.group_output, larger_than_group)
        self.group_record["output"]["sha256"] = sha256_file(self.group_output)
        self.group_record["output"]["size"] = len(larger_than_group)
        refusal = self.assert_refused("output-larger", self.first_member, self.group_record)
        self.assertIn("({} bytes)".format(group_size), refusal.detail)
        stage = retire_one(self.first_member, self.group_record, self.root, allow_larger=True)
        self.assertEqual(len(stage["destinations"]), 2)
        self.assertIn("allowed-larger", load_record(self.first_member)["flags"])

    def test_second_member_in_quarantine_moves_nothing(self):
        write_bytes(self.quarantine / "dvd_rip" / "VTS_01_2.VOB", b"already here")
        self.assert_refused("quarantine-exists", self.first_member, self.group_record)

    def test_failure_after_first_member_is_recorded_as_partial(self):
        real_replace = os.replace
        calls = []

        def failing_replace(source, destination):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("simulated failure on the second member")
            return real_replace(source, destination)

        with mock.patch.object(retire.os, "replace", side_effect=failing_replace):
            with self.assertRaises(OSError) as caught:
                retire_one(self.first_member, self.group_record, self.root)
        self.assertIn("1 of 2", str(caught.exception))
        self.assertFalse(self.first_member.exists())
        self.assertTrue(self.second_member.is_file())
        saved = load_record(self.first_member)
        self.assertEqual(saved["status"], "verified")
        self.assertEqual([entry["name"] for entry in saved["stages"]["retire"]["partial"]], ["VTS_01_1.VOB"])
        self.assertEqual(saved["stages"]["retire"]["failed"], "VTS_01_2.VOB")
        self.assert_refused("status-not-verified", self.first_member, saved)

    def test_destroy_confirmation_lists_every_member(self):
        self.beach_record["score"] = 40
        save_record(self.beach_source, self.beach_record)
        self.meeting_record["score"] = 40
        save_record(self.meeting_source, self.meeting_record)
        self.quietly()
        with mock.patch.object(retire, "confirm_destroy", return_value=False) as confirm:
            run(RunTests.namespace(self, "--destroy-originals-now", str(self.root), yes=True))
        listed = confirm.call_args[0][0]
        self.assertEqual(listed, [str(self.first_member), str(self.second_member)])
        self.assertTrue(self.first_member.is_file())
        self.assertTrue(self.second_member.is_file())


class MarkedOutputTests(RetireCase):
    """plan --mark-outputs: the output and its sidecars carry interim names until retire renames them all."""

    def setUp(self):
        RetireCase.setUp(self)
        self.party_source = self.holidays / "garden_party.avi"
        self.party_interim = self.holidays / "garden_party.vidfunnel.mp4"
        self.party_record = make_verified(self.party_source, self.party_interim, "garden_party.mp4",
                                          interim_name="garden_party.vidfunnel.mp4")
        self.party_record["output"]["sidecars"] = ["garden_party.srt", "garden_party.sup"]
        self.party_record["output"]["interim_sidecars"] = ["garden_party.vidfunnel.srt", "garden_party.vidfunnel.sup"]
        save_record(self.party_source, self.party_record)
        write_bytes(self.holidays / "garden_party.vidfunnel.srt", b"1\n00:00:01,000 --> 00:00:02,000\nhello\n")
        write_bytes(self.holidays / "garden_party.vidfunnel.sup", b"PG fake bitmap subtitles")

    def test_output_and_sidecars_get_their_final_names(self):
        stage = retire_one(self.party_source, self.party_record, self.root)
        self.assertTrue(stage["renamed_output"])
        self.assertEqual(stage["renamed_sidecars"], ["garden_party.srt", "garden_party.sup"])
        self.assertNotIn("sidecar_rename_errors", stage)
        self.assertEqual((self.holidays / "garden_party.mp4").read_bytes(), OUTPUT_BYTES)
        self.assertIn(b"hello", (self.holidays / "garden_party.srt").read_bytes())
        self.assertEqual((self.holidays / "garden_party.sup").read_bytes(), b"PG fake bitmap subtitles")
        for name in ("garden_party.vidfunnel.mp4", "garden_party.vidfunnel.srt", "garden_party.vidfunnel.sup",
                     "garden_party.avi"):
            self.assertFalse((self.holidays / name).exists(), name)
        saved = load_record(self.party_source)
        self.assertEqual(saved["status"], "retired")
        self.assertEqual(saved["output"]["name"], "garden_party.mp4")
        self.assertEqual(saved["output"]["sidecars"], ["garden_party.srt", "garden_party.sup"])
        self.assertEqual(saved["stages"]["retire"]["renamed_sidecars"], ["garden_party.srt", "garden_party.sup"])

    def test_a_record_without_interim_sidecars_renames_none(self):
        stage = retire_one(self.meeting_source, self.meeting_record, self.root)
        self.assertTrue(stage["renamed_output"])
        self.assertNotIn("renamed_sidecars", stage)

    def test_a_foreign_file_at_a_final_sidecar_name_blocks_before_anything_moves(self):
        write_bytes(self.holidays / "garden_party.srt", b"the user's own subtitles")
        refusal = self.assert_refused("interim-rename-blocked", self.party_source, self.party_record)
        self.assertIn("'garden_party.vidfunnel.srt'", refusal.detail)
        self.assertIn("'garden_party.srt'", refusal.detail)

    def test_a_sidecar_that_was_never_written_is_passed_over(self):
        os.unlink(str(self.holidays / "garden_party.vidfunnel.sup"))
        stage = retire_one(self.party_source, self.party_record, self.root)
        self.assertEqual(stage["renamed_sidecars"], ["garden_party.srt"])
        self.assertFalse((self.holidays / "garden_party.sup").exists())

    def test_unusable_sidecar_lists_are_refused_as_no_record(self):
        for interim in (["garden_party.vidfunnel.srt"], "garden_party.vidfunnel.srt",
                        ["../garden_party.vidfunnel.srt", "garden_party.vidfunnel.sup"]):
            with self.subTest(interim=interim):
                record = dict(self.party_record, output=dict(self.party_record["output"], interim_sidecars=interim))
                self.assert_refused("no-record", self.party_source, record)

    def test_sidecars_keep_their_interim_names_when_the_output_rename_fails(self):
        real_replace = os.replace

        def failing_replace(source, destination):
            if str(destination).endswith("garden_party.mp4"):
                raise OSError("simulated rename failure")
            return real_replace(source, destination)

        with mock.patch("vidfunnel.retire.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                retire_one(self.party_source, self.party_record, self.root)
        self.assertTrue((self.holidays / "garden_party.vidfunnel.mp4").exists())
        self.assertTrue((self.holidays / "garden_party.vidfunnel.srt").exists())
        self.assertFalse((self.holidays / "garden_party.srt").exists())
        stage = load_record(self.party_source)["stages"]["retire"]
        self.assertFalse(stage["renamed_output"])
        self.assertEqual(stage["renamed_sidecars"], [])


class ConfirmDestroyTests(unittest.TestCase):
    def test_lists_every_file_and_accepts_only_the_word(self):
        out = io.StringIO()
        accepted = confirm_destroy(["/library/holiday_beach.avi", "/library/garden_party.avi"],
                                   stdin=io.StringIO("destroy\n"), stdout=out)
        self.assertTrue(accepted)
        self.assertIn("'/library/holiday_beach.avi'", out.getvalue())
        self.assertIn("'/library/garden_party.avi'", out.getvalue())
        for answer in ("no\n", "yes\n", "DESTROY\n", "\n", ""):
            self.assertFalse(confirm_destroy(["/library/holiday_beach.avi"], stdin=io.StringIO(answer),
                                             stdout=io.StringIO()), answer)

    def test_yes_does_not_read_stdin(self):
        out = io.StringIO()
        self.assertTrue(confirm_destroy(["/library/holiday_beach.avi"], stdin=PipeInput(), stdout=out,
                                        yes=True))
        self.assertIn("'/library/holiday_beach.avi'", out.getvalue())


class RunTests(RetireCase):
    def namespace(self, *arguments, **global_options):
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(list(arguments))
        args.quiet = global_options.get("quiet", False)
        args.verbose = False
        args.log = None
        args.ffmpeg = None
        args.yes = global_options.get("yes", False)
        return args

    def test_run_quarantines_every_eligible_source(self):
        from vidfunnel.cli import EXIT_OK
        out, _err = self.quietly()
        result = run(self.namespace(str(self.root)))
        self.assertEqual(result, EXIT_OK)
        self.assertTrue((self.quarantine / "holidays" / "holiday_beach.avi").is_file())
        self.assertTrue((self.quarantine / "meetings" / "client_meeting.mp4").is_file())
        self.assertEqual(self.meeting_source.read_bytes(), OUTPUT_BYTES)
        text = out.getvalue()
        self.assertIn("[retire 1/2] 'holidays/holiday_beach.avi' -> quarantine", text)
        self.assertIn("retire: 2 retired", text)

    def test_run_counts_refusals_and_skips(self):
        from vidfunnel.cli import EXIT_OK
        self.beach_record["score"] = 75
        save_record(self.beach_source, self.beach_record)
        set_status(self.meeting_record, "converted")
        save_record(self.meeting_source, self.meeting_record)
        before = snapshot(self.root)
        out, _err = self.quietly()
        self.assertEqual(run(self.namespace(str(self.root))), EXIT_OK)
        self.assertIn("'holidays/holiday_beach.avi' refused (score-review)", out.getvalue())
        self.assertIn("0 retired, 1 skipped, 1 refused score-review", out.getvalue())
        self.assertEqual(snapshot(self.root), before)
        out.truncate(0)
        self.assertEqual(run(self.namespace("--allow-review", str(self.root))), EXIT_OK)
        self.assertFalse(self.beach_source.exists())
        self.assertEqual(load_record(self.meeting_source)["status"], "converted")

    def test_a_record_left_source_changed_by_an_older_convert_is_not_retired(self):
        # Before 20261109.3 convert set this status and flag; such records stay readable and are skipped.
        from vidfunnel.cli import EXIT_OK
        set_status(self.beach_record, "source-changed")
        add_flag(self.beach_record, "source-changed")
        save_record(self.beach_source, self.beach_record)
        self.assert_refused("status-not-verified", self.beach_source, self.beach_record)
        before = snapshot(self.root / "holidays")
        out, _err = self.quietly()
        self.assertEqual(run(self.namespace(str(self.root))), EXIT_OK)
        self.assertIn("1 retired, 1 skipped", out.getvalue())
        self.assertEqual(snapshot(self.root / "holidays"), before)

    def test_run_allow_larger_option(self):
        from vidfunnel.cli import EXIT_OK
        add_flag(self.beach_record, "output-larger")
        save_record(self.beach_source, self.beach_record)
        out, _err = self.quietly()
        self.assertEqual(run(self.namespace(str(self.beach_source))), EXIT_OK)
        self.assertIn("refused (output-larger)", out.getvalue())
        self.assertTrue(self.beach_source.is_file())
        self.assertEqual(run(self.namespace("--allow-larger", str(self.beach_source))), EXIT_OK)
        self.assertFalse(self.beach_source.exists())
        self.assertIn("allowed-larger", load_record(self.beach_source)["flags"])

    def test_run_single_file_path(self):
        out, _err = self.quietly()
        run(self.namespace(str(self.beach_source)))
        self.assertTrue((self.holidays / ".vidfunnel-retired" / "holiday_beach.avi").is_file())
        self.assertTrue(self.meeting_source.is_file())

    def test_run_custom_quarantine(self):
        custom = Path(self.holder.name) / "retired_elsewhere"
        out, _err = self.quietly()
        run(self.namespace("--quarantine", str(custom), str(self.root)))
        self.assertTrue((custom / "holidays" / "holiday_beach.avi").is_file())
        self.assertTrue((custom / "meetings" / "client_meeting.mp4").is_file())

    def test_run_skips_a_locked_folder(self):
        from vidfunnel.cli import EXIT_ERROR
        out, err = self.quietly()
        with FolderLock(self.holidays):
            result = run(self.namespace(str(self.root)))
        self.assertEqual(result, EXIT_ERROR)
        self.assertTrue(self.beach_source.is_file())
        self.assertEqual(load_record(self.beach_source)["status"], "verified")
        self.assertFalse(self.meeting_interim.exists())
        self.assertIn("locked", err.getvalue())
        self.assertIn("1 retired", out.getvalue())
        self.assertIn("1 skipped (locked)", out.getvalue())

    def take_over(self, folder):
        """Host B takes `folder` over: its lock file is replaced under this run."""
        lock_file = folder / ".vidfunnel" / "lock"
        os.unlink(str(lock_file))
        lock_file.write_text(json.dumps({"host": "hostb", "pid": 4242, "started": "2026-11-09T12:00:00Z",
                                         "heartbeat": {"interval": 300}}), encoding="utf-8")

    def assert_nothing_retired_after_the_takeover(self, before, records, result, out, err):
        from vidfunnel.cli import EXIT_ERROR
        self.assertEqual(result, EXIT_ERROR)
        after = snapshot(self.root)
        for name in list(after):
            if name.endswith(os.path.join(".vidfunnel", "lock")):
                del after[name]
                before.pop(name, None)
        self.assertEqual(after, before, "a source was moved or deleted, or a record saved, after the takeover")
        self.assertTrue(self.beach_source.is_file())
        self.assertTrue(self.meeting_source.is_file())
        self.assertEqual({path: path.read_bytes() for path in records}, records)
        self.assertIn("2 skipped (lock taken)", out.getvalue())
        self.assertIn("was taken over by another run", err.getvalue())

    def lost_lock_setup(self):
        import vidfunnel.interrupt
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        patch = mock.patch("vidfunnel.record._warn")
        patch.start()
        self.addCleanup(patch.stop)
        records = [record_module.record_path(self.beach_source), record_module.record_path(self.meeting_source)]
        return snapshot(self.root), {path: path.read_bytes() for path in records}

    def test_run_moves_nothing_after_the_folder_lock_was_taken(self):
        before, records = self.lost_lock_setup()
        real_fresh = retire._fresh_record

        def taken_then_read(candidate):
            self.take_over(candidate.folder)
            return real_fresh(candidate)

        out, err = self.quietly()
        with mock.patch.object(retire, "_fresh_record", side_effect=taken_then_read), \
                mock.patch.object(retire, "drain_before_next", return_value=False):
            result = run(self.namespace(str(self.root)))
        self.assert_nothing_retired_after_the_takeover(before, records, result, out, err)

    def test_destroy_deletes_nothing_after_the_folder_lock_was_taken(self):
        before, records = self.lost_lock_setup()

        def confirmed_then_taken(*arguments, **values):
            self.take_over(self.holidays)
            self.take_over(self.meetings)
            return True

        out, err = self.quietly()
        with mock.patch.object(retire, "confirm_destroy", side_effect=confirmed_then_taken), \
                mock.patch.object(retire, "drain_before_next", return_value=False):
            result = run(self.namespace("--destroy-originals-now", str(self.root), yes=True))
        self.assert_nothing_retired_after_the_takeover(before, records, result, out, err)

    def test_run_skips_every_folder_when_no_heartbeat_thread_can_start(self):
        """A pids limit: the lock cannot beat, so it is not taken; each folder is skipped, nothing is moved."""
        import threading
        import time
        from vidfunnel.cli import EXIT_ERROR

        class RefusedThread(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")

        deadline = time.monotonic() + 5
        while record_module._beat_thread is not None:
            self.assertLess(time.monotonic(), deadline, "a heartbeat thread outlived an earlier test")
            time.sleep(0.01)
        refusing = mock.patch.object(record_module, "threading",
                                     mock.Mock(Thread=RefusedThread, current_thread=threading.current_thread))
        before = snapshot(self.root)
        out, err = self.quietly()
        with refusing:
            result = run(self.namespace(str(self.root)))
        self.assertEqual(result, EXIT_ERROR)
        self.assertEqual(snapshot(self.root), before, "files changed although no folder was locked")
        self.assertIn("cannot start the heartbeat thread its lock needs (can't start new thread)", err.getvalue())
        self.assertIn("2 skipped (locked)", out.getvalue())
        for folder in (self.holidays, self.meetings):
            self.assertFalse((folder / ".vidfunnel" / "lock").exists())

    def test_run_missing_path_is_an_error(self):
        from vidfunnel.cli import EXIT_ERROR
        self.quietly()
        self.assertEqual(run(self.namespace(str(self.root / "no_such_folder"))), EXIT_ERROR)

    def test_destroy_answered_no_unlinks_nothing(self):
        before = snapshot(self.root)
        self.quietly()
        with mock.patch.object(sys, "stdin", TerminalInput("no\n")):
            run(self.namespace("--destroy-originals-now", str(self.root)))
        self.assertEqual(snapshot(self.root), before)

    def test_destroy_answered_destroy_unlinks(self):
        out, _err = self.quietly()
        with mock.patch.object(sys, "stdin", TerminalInput("destroy\n")):
            run(self.namespace("--destroy-originals-now", str(self.root)))
        self.assertFalse(self.beach_source.exists())
        self.assertFalse(self.meeting_interim.exists())
        self.assertEqual(self.meeting_source.read_bytes(), OUTPUT_BYTES)
        self.assertEqual(self.beach_output.read_bytes(), OUTPUT_BYTES)
        self.assertFalse(self.quarantine.exists())
        self.assertIn(str(self.beach_source), out.getvalue())
        self.assertEqual(load_record(self.beach_source)["stages"]["retire"]["mode"], "destroy")

    def test_destroy_with_yes_does_not_read_stdin(self):
        self.quietly()
        with mock.patch.object(sys, "stdin", PipeInput()):
            run(self.namespace("--destroy-originals-now", str(self.root), yes=True))
        self.assertFalse(self.beach_source.exists())
        self.assertFalse(self.meeting_interim.exists())

    def test_destroy_honours_allow_larger(self):
        add_flag(self.beach_record, "output-larger")
        save_record(self.beach_source, self.beach_record)
        out, _err = self.quietly()
        with mock.patch.object(sys, "stdin", PipeInput()):
            run(self.namespace("--destroy-originals-now", str(self.beach_source), yes=True))
        self.assertIn("refused (output-larger)", out.getvalue())
        self.assertTrue(self.beach_source.is_file())
        with mock.patch.object(sys, "stdin", PipeInput()):
            run(self.namespace("--destroy-originals-now", "--allow-larger", str(self.beach_source), yes=True))
        self.assertFalse(self.beach_source.exists())
        self.assertIn("allowed-larger", load_record(self.beach_source)["flags"])

    def test_destroy_without_terminal_or_yes_refuses(self):
        from vidfunnel.cli import EXIT_USAGE
        before = snapshot(self.root)
        _out, err = self.quietly()
        with mock.patch.object(sys, "stdin", PipeInput()):
            result = run(self.namespace("--destroy-originals-now", str(self.root)))
        self.assertEqual(result, EXIT_USAGE)
        self.assertIn("--yes", err.getvalue())
        self.assertEqual(snapshot(self.root), before)

    def test_destroy_skips_refused_sources(self):
        self.beach_record["score"] = 40
        save_record(self.beach_source, self.beach_record)
        out, _err = self.quietly()
        with mock.patch.object(sys, "stdin", TerminalInput("destroy\n")):
            run(self.namespace("--destroy-originals-now", str(self.root)))
        self.assertTrue(self.beach_source.is_file())
        self.assertFalse(self.meeting_interim.exists())
        listing = out.getvalue().split("Type 'destroy'")[0]
        self.assertNotIn(str(self.beach_source), listing.split("permanently delete")[1])


class StopCase(unittest.TestCase):
    """Clean drain flags, stop-file state and signal handlers around each test."""

    def setUp(self):
        import vidfunnel.interrupt
        import vidfunnel.stopfile
        for flag in (vidfunnel.interrupt.interrupted, vidfunnel.interrupt.draining):
            flag.clear()
            self.addCleanup(flag.clear)
        vidfunnel.interrupt.drain_tally.reset()
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        vidfunnel.stopfile.reset()
        self.addCleanup(vidfunnel.stopfile.reset)
        self.holder = tempfile.TemporaryDirectory(prefix="vidfunnel-retire-stop-test-")
        self.addCleanup(self.holder.cleanup)
        self.root = Path(self.holder.name) / "library"

    def tally(self):
        import vidfunnel.interrupt
        tally = vidfunnel.interrupt.drain_tally
        return tally.seen, tally.finished, tally.left


class RetireDrainTests(StopCase):
    """retire's loops ask for a drain before each folder and each source (vidfunnel.interrupt.drain_before_next)."""

    LAYOUT = (("first", "harbour"), ("second", "meadow"), ("second", "orchard"), ("third", "valley"))

    def setUp(self):
        StopCase.setUp(self)
        for folder, stem in self.LAYOUT:
            make_verified(self.root / folder / (stem + ".avi"), self.root / folder / (stem + ".mp4"), stem + ".mp4")

    def namespace(self, *arguments):
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(list(arguments) + [str(self.root)])
        args.quiet, args.verbose, args.yes = True, False, True
        return args

    def retire_drained_in(self, draining_stem, *arguments):
        """Run retire with _retire_candidate replaced; the drain begins in `draining_stem`'s item.

        Returns (stems started, folders locked)."""
        import vidfunnel.interrupt
        started = []
        locked = []
        real_lock = retire.FolderLock

        def lock(folder, *rest, **options):
            locked.append(Path(folder).name)
            return real_lock(folder, *rest, **options)

        def retire_candidate(candidate, *rest, **options):
            started.append(Path(candidate.name).stem)
            if started[-1] == draining_stem:
                vidfunnel.interrupt.draining.set()

        with mock.patch.object(retire, "FolderLock", side_effect=lock), \
                mock.patch.object(retire, "_retire_candidate", side_effect=retire_candidate), \
                mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
            run(self.namespace(*arguments))
        return started, locked

    def test_a_drain_in_the_last_source_of_a_folder_takes_no_further_folder_lock(self):
        self.assertEqual(self.retire_drained_in("harbour"), (["harbour"], ["first"]))
        self.assertEqual(self.tally(), (True, 1, 3))

    def test_a_drain_in_a_source_starts_no_further_source_of_its_folder(self):
        self.assertEqual(self.retire_drained_in("meadow"), (["harbour", "meadow"], ["first", "second"]))
        self.assertEqual(self.tally(), (True, 1, 2))

    def test_a_drain_while_destroying_destroys_no_further_source(self):
        started, locked = self.retire_drained_in("meadow", "--destroy-originals-now")
        self.assertEqual(started, ["harbour", "meadow"])
        self.assertEqual(locked, ["first", "second", "third"], "destroy takes every lock before it checks")
        self.assertEqual(self.tally(), (True, 1, 2))

    def test_a_drain_during_the_checks_before_destroy_destroys_nothing(self):
        import vidfunnel.interrupt
        checked = []
        real_check = retire.check_eligible

        def check(source, *rest, **options):
            checked.append(Path(source).stem)
            if checked[-1] == "meadow":
                vidfunnel.interrupt.draining.set()
            return real_check(source, *rest, **options)

        with mock.patch.object(retire, "check_eligible", side_effect=check), \
                mock.patch.object(retire, "confirm_destroy", return_value=True) as confirm:
            started, _locked = self.retire_drained_in(None, "--destroy-originals-now")
        self.assertEqual(checked, ["harbour", "meadow"])
        self.assertEqual(started, [])
        confirm.assert_not_called()
        self.assertEqual(self.tally(), (True, 0, 4), "two never checked, two checked and not destroyed")

    def test_a_now_stop_file_written_during_a_source_does_not_cut_it_short(self):
        """The PAR2 redo runs after the original has moved; only the next source sees the stop file."""
        import vidfunnel.stopfile
        from vidfunnel.record import _write_json_atomic
        finished = []

        def retire_candidate(candidate, *rest, **options):
            _write_json_atomic(vidfunnel.stopfile.stop_path(str(self.root)),
                               {"host": "elsewhere", "requested": None, "mode": "now"})
            vidfunnel.stopfile.raise_if_stopped_now("par2")  # what write_par2 does before par2 starts
            finished.append(Path(candidate.name).stem)

        with mock.patch.object(retire, "_retire_candidate", side_effect=retire_candidate), \
                mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                run(self.namespace())
        self.assertEqual(finished, ["harbour"])
        self.assertTrue(vidfunnel.stopfile.stopped_now)


class RetireLockFileErrorTests(StopCase):
    """A folder whose lock file cannot be created is skipped with its reason, in both modes."""

    LAYOUT = RetireDrainTests.LAYOUT
    setUp = RetireDrainTests.setUp
    namespace = RetireDrainTests.namespace

    def test_a_folder_whose_lock_file_cannot_be_created_is_skipped_and_the_next_retired(self):
        for arguments in ((), ("--destroy-originals-now",)):
            with self.subTest(arguments=arguments):
                started = []
                real_lock = retire.FolderLock

                def lock(folder, *rest, **options):
                    made = real_lock(folder, *rest, **options)
                    if Path(folder).name == "first":
                        def refuse():
                            raise PermissionError(13, "Permission denied", str(made.path))
                        made.acquire = refuse
                    return made

                def retire_candidate(candidate, *rest, **options):
                    started.append(Path(candidate.name).stem)

                errors = io.StringIO()
                with mock.patch.object(retire, "FolderLock", side_effect=lock), \
                        mock.patch.object(retire, "_retire_candidate", side_effect=retire_candidate), \
                        mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", errors):
                    code = run(self.namespace(*arguments))
                self.assertEqual(started, ["meadow", "orchard", "valley"], errors.getvalue())
                self.assertIn("retire: skipping 1 file(s): cannot take the lock of folder '{}': [Errno 13] "
                              "Permission denied".format(self.root / "first"), errors.getvalue())
                self.assertEqual(code, 1)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOP_STEMS = ("harbour", "meadow", "orchard")


@unittest.skipIf(not hasattr(os, "killpg"), "no process groups here; retire stop integration tests skipped")
class RetireStopIntegrationTests(StopCase):
    """'vidfunnel retire' in a process of its own, stopped while its first source's PAR2 is being redone.

    A stand-in par2 on PATH sleeps 2 s before it writes the index file, so
    the first source is still in progress when the signal or stop file comes.
    """

    def setUp(self):
        StopCase.setUp(self)
        self.records = {}
        for stem in STOP_STEMS:
            source = self.root / (stem + ".mp4")
            interim = self.root / (stem + ".vidfunnel.mp4")
            self.records[stem] = make_verified(source, interim, stem + ".mp4", interim_name=interim.name)
            write_bytes(self.root / (interim.name + ".par2"), b"par2 for the interim name")
        fake_bin = Path(self.holder.name) / "bin"
        fake_bin.mkdir()
        stand_in = fake_bin / "par2"
        stand_in.write_text("#!/bin/sh\nwhile [ \"$1\" != \"--\" ]; do shift; done\nsleep 2\n: > \"$2\"\n",
                            encoding="utf-8")
        os.chmod(str(stand_in), 0o755)
        self.environment = dict(os.environ, PYTHONPATH=REPO_ROOT,
                                PATH="{}:{}".format(fake_bin, os.environ.get("PATH", "")))
        self.before = snapshot(self.root)

    def run_stopped(self, act):
        import signal
        import subprocess
        import time
        process = subprocess.Popen([sys.executable, "-m", "vidfunnel", "retire", str(self.root)], cwd=REPO_ROOT,
                                   env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, universal_newlines=True, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))
        lines = []
        for line in process.stdout:
            lines.append(line)
            if "[retire 1/3]" in line:
                break
        else:
            process.wait(30)
            self.fail("'[retire 1/3]' never appeared; output:\n{}".format("".join(lines)))
        time.sleep(0.5)
        act(process)
        rest, errors = process.communicate(timeout=60)
        return process.returncode, "".join(lines) + rest, errors

    def stop_all(self, *options):
        import subprocess
        result = subprocess.run([sys.executable, "-m", "vidfunnel", "stop-all"] + list(options) + [str(self.root)],
                                cwd=REPO_ROOT, env=self.environment, capture_output=True, universal_newlines=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_first_retired_only(self, output, errors):
        statuses = {stem: load_record(self.root / (stem + ".mp4"))["status"] for stem in STOP_STEMS}
        self.assertEqual(statuses, {"harbour": "retired", "meadow": "verified", "orchard": "verified"},
                         "stdout:\n{}\nstderr:\n{}".format(output, errors))
        retired = load_record(self.root / "harbour.mp4")["stages"]["retire"]
        self.assertEqual(retired["par2_recreated"], ["harbour.mp4.par2"])
        after = snapshot(self.root)
        for stem in ("meadow", "orchard"):
            for name in (stem + ".mp4", stem + ".vidfunnel.mp4", stem + ".vidfunnel.mp4.par2"):
                self.assertEqual(after.get(name), self.before[name], name)
        self.assertNotIn(os.path.join(".vidfunnel", "lock"), after)

    def test_retire_drained_by_sigusr1(self):
        import signal
        code, output, errors = self.run_stopped(lambda process: os.kill(process.pid, signal.SIGUSR1))
        self.assertEqual(code, 0, errors)
        self.assertIn("vidfunnel: drained: finished 1 in progress, started no more (2 not started)", errors)
        self.assert_first_retired_only(output, errors)

    def test_retire_drained_by_a_stop_file(self):
        code, output, errors = self.run_stopped(lambda process: self.stop_all())
        self.assertEqual(code, 0, errors)
        self.assertIn("(drain): finishing the items in progress, starting no more", errors)
        # The run sees the file only when it is about to start the next source, so none counts as in progress.
        self.assertIn("vidfunnel: drained: finished 0 in progress, started no more (2 not started)", errors)
        self.assert_first_retired_only(output, errors)

    def test_retire_stopped_by_a_now_stop_file_finishes_the_source_in_progress(self):
        code, output, errors = self.run_stopped(lambda process: self.stop_all("--now"))
        self.assertEqual(code, 1, errors)
        self.assertIn("(now): stopping now", errors)
        self.assertIn("vidfunnel: stopped (a stop file asked for it)", errors)
        self.assert_first_retired_only(output, errors)


if __name__ == "__main__":
    unittest.main()

# End of File #
