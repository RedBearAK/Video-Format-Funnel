"""Tests for vidfunnel.retire: every guard, quarantine, interim rename, destroy confirmation, run."""

VERSION = "20261102"

import argparse
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            add_flag(self.beach_record, "keep-larger")
            self.assert_refused("output-larger", self.beach_source, self.beach_record, allow_review=True)
        hasher.assert_not_called()
        self.beach_record["flags"].remove("keep-larger")
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

    def test_keep_larger_flag(self):
        add_flag(self.beach_record, "keep-larger")
        self.assert_refused("output-larger", self.beach_source, self.beach_record)

    def test_output_larger_than_source_without_flag(self):
        bigger = SOURCE_BYTES + b"more"
        write_bytes(self.beach_output, bigger)
        self.beach_record["output"]["sha256"] = sha256_file(self.beach_output)
        self.beach_record["output"]["size"] = len(bigger)
        self.beach_record["action"] = "reencode"
        self.assert_refused("output-larger", self.beach_source, self.beach_record)

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
                                  "members": [self.meeting_source]})

    def test_large_output_hash_shows_progress(self):
        large = b"\0" * (33 * 1024 * 1024)
        large_source = self.holidays / "harbour_night.avi"
        large_output = self.holidays / "harbour_night.mp4"
        record = make_verified(large_source, large_output, "harbour_night.mp4",
                               source_bytes=large + b"source", output_bytes=large)
        out, _err = self.quietly()
        check_eligible(large_source, record)
        self.assertIn("hash 'harbour_night.mp4'", out.getvalue())


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


if __name__ == "__main__":
    unittest.main()

# End of File #
