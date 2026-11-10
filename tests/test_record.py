"""Tests for vidfunnel.record: paths, atomic saves, fingerprints, hashes, stages, lock."""

VERSION = "20261110.1"

import contextlib
import datetime
import errno
import hashlib
import io
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest import mock
from pathlib import Path

import vidfunnel.interrupt
import vidfunnel.record
import vidfunnel.record as record_module
from vidfunnel.record import (
    machine_id,
    machine_identity,
    FOLDER_FILE,
    HEARTBEAT_SECONDS,
    HEARTBEAT_STALE_SECONDS,
    PROBE_PREFIX,
    MARKER_ASIDE_PREFIX,
    NEW_LOCK_PREFIX,
    TAKEOVER_MARKER,
    TAKEOVER_MARKER_STALE_SECONDS,
    held_lock_fds,
    LOCK_NAME,
    RECORD_DIR,
    FolderLock,
    LockHeld,
    RecordError,
    in_container,
    pid_alive,
    add_flag,
    add_penalty,
    add_stage,
    cheap_fingerprint,
    fingerprint_differences,
    fingerprint_identical,
    fingerprint_matches,
    iter_records,
    load_folder,
    check_source,
    load_record,
    mtime_difference,
    mtime_is_coarser_view,
    new_record,
    record_path,
    save_folder,
    save_record,
    set_status,
    sha256_file,
)

MIB = 1024 * 1024


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "wb") as stream:
        stream.write(data)
    return path


def flip_byte(path, offset):
    """Change one byte in place, keeping the size, and restore the mtime so only content differs."""
    before = os.stat(str(path))
    with open(str(path), "r+b") as stream:
        stream.seek(offset)
        original = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([original[0] ^ 0xFF]))
    os.utime(str(path), ns=(before.st_atime_ns, before.st_mtime_ns))


class RecordingProgress:
    def __init__(self):
        self.values = []

    def update(self, current):
        self.values.append(current)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="vidfunnel-record-test-")
        self.base = Path(self.holder.name)

    def tearDown(self):
        self.holder.cleanup()


class PathTests(TempDirCase):
    def test_sources_sharing_a_stem_get_separate_records(self):
        avi_source = self.base / "movie.avi"
        mkv_source = self.base / "movie.mkv"
        self.assertEqual(record_path(avi_source), self.base / RECORD_DIR / "movie.avi.json")
        self.assertEqual(record_path(mkv_source), self.base / RECORD_DIR / "movie.mkv.json")
        self.assertNotEqual(record_path(avi_source), record_path(mkv_source))

        write_bytes(avi_source, b"avi content")
        write_bytes(mkv_source, b"mkv content")
        avi_record = new_record(avi_source, self.base)
        mkv_record = new_record(mkv_source, self.base)
        save_record(avi_source, avi_record)
        save_record(mkv_source, mkv_record)
        self.assertEqual(load_record(avi_source)["id"], avi_record["id"])
        self.assertEqual(load_record(mkv_source)["id"], mkv_record["id"])


class SaveLoadTests(TempDirCase):
    def test_round_trip(self):
        source = write_bytes(self.base / "clip.avi", b"some bytes")
        record = new_record(source, self.base)
        add_flag(record, "aspect-ambiguous")
        add_penalty(record, "aspect-ambiguous", 30, "SAR missing")
        add_stage(record, "probe", {"codec": "mpeg4", "names": ["café"]})
        set_status(record, "planned")
        save_record(source, record)
        self.assertEqual(load_record(source), record)

    def test_file_format_is_sorted_indented_utf8_with_newline(self):
        source = write_bytes(self.base / "café.avi", b"x")
        record = new_record(source, self.base)
        save_record(source, record)
        raw = record_path(source).read_bytes()
        self.assertTrue(raw.endswith(b"}\n"))
        text = raw.decode("utf-8")
        self.assertIn("café.avi", text)
        self.assertEqual(text, json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def test_missing_record_is_none(self):
        self.assertIsNone(load_record(self.base / "absent.avi"))

    def test_corrupt_record_raises_naming_the_file(self):
        source = self.base / "broken.avi"
        path = record_path(source)
        write_bytes(path, b"{not json")
        with self.assertRaises(RecordError) as caught:
            load_record(source)
        self.assertIn("'{}'".format(path), str(caught.exception))

    def test_save_replaces_whole_file_and_leaves_no_temp(self):
        source = write_bytes(self.base / "clip.avi", b"data")
        first = new_record(source, self.base)
        first["stages"] = {"probe": {"padding": "x" * 5000}}
        save_record(source, first)
        second = new_record(source, self.base)
        save_record(source, second)
        self.assertEqual(load_record(source), second)
        leftovers = sorted(entry.name for entry in (self.base / RECORD_DIR).iterdir())
        self.assertEqual(leftovers, ["clip.avi.json"])

    def test_failed_save_keeps_old_content_and_leaves_no_temp(self):
        source = write_bytes(self.base / "clip.avi", b"data")
        old = new_record(source, self.base)
        save_record(source, old)
        newer = new_record(source, self.base)
        with mock.patch("vidfunnel.record.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                save_record(source, newer)
        self.assertEqual(load_record(source), old)
        leftovers = sorted(entry.name for entry in (self.base / RECORD_DIR).iterdir())
        self.assertEqual(leftovers, ["clip.avi.json"])

    def test_folder_file_round_trip(self):
        self.assertEqual(load_folder(self.base), {})
        data = {"last_scan": "2026-11-02T10:00:00Z", "sources": ["a.avi", "b.mkv"]}
        save_folder(self.base, data)
        self.assertEqual(load_folder(self.base), data)
        self.assertTrue((self.base / RECORD_DIR / FOLDER_FILE).is_file())


class NewRecordTests(TempDirCase):
    def test_shape(self):
        source = write_bytes(self.base / "movie.avi", b"payload")
        record = new_record(source, self.base)
        self.assertEqual(uuid.UUID(record["id"]).version, 4)
        self.assertIsNone(record["status"])
        self.assertIsNone(record["status_at"])
        self.assertEqual(record["source"]["name"], "movie.avi")
        self.assertEqual(record["source"]["folder"], str(self.base.absolute()))
        self.assertEqual(record["source"]["size"], 7)
        self.assertIsNone(record["source"]["sha256"])
        self.assertEqual(record["stages"], {})
        self.assertEqual(record["history"], [])
        self.assertNotEqual(new_record(source, self.base)["id"], record["id"])


class FingerprintTests(TempDirCase):
    def test_small_file_hashes_whole_file(self):
        data = os.urandom(MIB + 12345)
        path = write_bytes(self.base / "small.bin", data)
        fingerprint = cheap_fingerprint(path)
        whole = hashlib.sha256(data).hexdigest()
        self.assertEqual(fingerprint["size"], len(data))
        self.assertEqual(fingerprint["head_sha256"], whole)
        self.assertEqual(fingerprint["tail_sha256"], whole)
        self.assertEqual(fingerprint["mtime_ns"], os.stat(str(path)).st_mtime_ns)

    def test_large_file_hashes_first_and_last_mib(self):
        data = os.urandom(3 * MIB)
        path = write_bytes(self.base / "large.bin", data)
        fingerprint = cheap_fingerprint(path)
        self.assertEqual(fingerprint["size"], 3 * MIB)
        self.assertEqual(fingerprint["head_sha256"], hashlib.sha256(data[:MIB]).hexdigest())
        self.assertEqual(fingerprint["tail_sha256"], hashlib.sha256(data[-MIB:]).hexdigest())
        self.assertTrue(fingerprint_matches(fingerprint, cheap_fingerprint(path)))

    def test_one_byte_change_in_tail_is_detected(self):
        path = write_bytes(self.base / "large.bin", os.urandom(3 * MIB))
        before = cheap_fingerprint(path)
        flip_byte(path, 3 * MIB - 1)
        after = cheap_fingerprint(path)
        self.assertEqual(before["size"], after["size"])
        self.assertEqual(before["mtime_ns"], after["mtime_ns"])
        self.assertEqual(before["head_sha256"], after["head_sha256"])
        self.assertNotEqual(before["tail_sha256"], after["tail_sha256"])
        self.assertFalse(fingerprint_matches(before, after))

    def test_one_byte_change_in_head_is_detected(self):
        path = write_bytes(self.base / "large.bin", os.urandom(3 * MIB))
        before = cheap_fingerprint(path)
        flip_byte(path, 0)
        after = cheap_fingerprint(path)
        self.assertEqual(before["mtime_ns"], after["mtime_ns"])
        self.assertNotEqual(before["head_sha256"], after["head_sha256"])
        self.assertEqual(before["tail_sha256"], after["tail_sha256"])
        self.assertFalse(fingerprint_matches(before, after))

    def test_one_byte_change_in_small_file_is_detected(self):
        path = write_bytes(self.base / "small.bin", os.urandom(1000))
        before = cheap_fingerprint(path)
        flip_byte(path, 500)
        self.assertFalse(fingerprint_matches(before, cheap_fingerprint(path)))

    def test_matches_rejects_missing(self):
        path = write_bytes(self.base / "small.bin", b"abc")
        fingerprint = cheap_fingerprint(path)
        self.assertFalse(fingerprint_matches(None, fingerprint))
        self.assertFalse(fingerprint_matches(fingerprint, {}))


# The time one file shows on the planning host, and what other mounts can show for it.
PLANNED_MTIME_NS = 1700000001987654321
SECOND_NS = 1000 * 1000 * 1000
STEPS = (("100 ns", 100), ("1 microsecond", 1000), ("1 ms", 1000 * 1000), ("10 ms", 10 * 1000 * 1000),
         ("1 s", SECOND_NS), ("2 s", 2 * SECOND_NS))


def truncated(value, step):
    return value - value % step


# (case, recorded time, seen time): one is the other truncated to a coarser step, in both directions.
COARSER_VIEWS = ([("seen truncated to " + name, PLANNED_MTIME_NS, truncated(PLANNED_MTIME_NS, step))
                  for name, step in STEPS]
                 + [("recorded truncated to " + name, truncated(PLANNED_MTIME_NS, step), PLANNED_MTIME_NS)
                    for name, step in STEPS])
# (case, recorded time, seen time): differences that no truncation explains.
OTHER_TIMES = (
    ("one second later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + SECOND_NS),
    ("one and a half seconds later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * SECOND_NS // 2),
    ("rounded up to the second", PLANNED_MTIME_NS, truncated(PLANNED_MTIME_NS, SECOND_NS) + SECOND_NS),
    ("three days later", PLANNED_MTIME_NS, PLANNED_MTIME_NS + 3 * 86400 * SECOND_NS),
)


class FingerprintTimeTests(TempDirCase):
    """Content decides first; then the time, accepted only as a coarser view; anything else by a full read."""

    def planned(self, word, recorded_ns=PLANNED_MTIME_NS):
        """A 3 MiB file fingerprinted at recorded_ns; (path, fingerprint, full SHA-256)."""
        path = write_bytes(self.base / "{}.bin".format(word), os.urandom(3 * MIB))
        os.utime(str(path), ns=(recorded_ns, recorded_ns))
        return path, cheap_fingerprint(path), sha256_file(path)

    def check(self, path, recorded, sha):
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            result = check_source([(path, recorded)], sha, path.name, quiet=True)
        return result, hasher

    def test_a_coarser_view_in_either_direction_is_the_same_time(self):
        self.assertTrue(mtime_is_coarser_view(PLANNED_MTIME_NS, PLANNED_MTIME_NS))
        for case, recorded_ns, seen_ns in COARSER_VIEWS:
            with self.subTest(case=case):
                self.assertTrue(mtime_is_coarser_view(recorded_ns, seen_ns))
        for case, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=case):
                self.assertFalse(mtime_is_coarser_view(recorded_ns, seen_ns))
        for value in (None, "1700000001987654321", 1.7e18, True, float("nan")):
            with self.subTest(value=value):
                self.assertFalse(mtime_is_coarser_view(value, PLANNED_MTIME_NS))

    def test_a_coarser_view_is_unchanged_without_a_read(self):
        for case, recorded_ns, seen_ns in COARSER_VIEWS:
            with self.subTest(case=case):
                path, recorded, sha = self.planned("view_" + case.replace(" ", "_"), recorded_ns)
                os.utime(str(path), ns=(seen_ns, seen_ns))
                result, hasher = self.check(path, recorded, sha)
                hasher.assert_not_called()
                self.assertIsNone(result.reason)
                self.assertEqual(result.notes, [{"name": path.name, "recorded": recorded_ns, "seen": seen_ns}])
                self.assertEqual(result.stage_notes(), {"source_mtime_differs": result.notes,
                                                        "source_sha256_checked": False})

    def test_the_same_time_reads_nothing_and_notes_nothing(self):
        path, recorded, sha = self.planned("unmoved")
        result, hasher = self.check(path, recorded, sha)
        hasher.assert_not_called()
        self.assertIsNone(result.reason)
        self.assertEqual(result.stage_notes(), {})

    def test_another_time_with_the_same_content_is_settled_by_one_read(self):
        for case, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=case):
                path, recorded, sha = self.planned(case.replace(" ", "_"), recorded_ns)
                os.utime(str(path), ns=(seen_ns, seen_ns))
                result, hasher = self.check(path, recorded, sha)
                self.assertEqual(hasher.call_count, 1)
                self.assertIsNone(result.reason)
                self.assertTrue(result.sha256_checked)
                self.assertEqual(result.sha256, sha)

    def test_another_time_with_a_changed_middle_is_changed(self):
        for case, recorded_ns, seen_ns in OTHER_TIMES:
            with self.subTest(case=case):
                path, recorded, sha = self.planned("middle_" + case.replace(" ", "_"), recorded_ns)
                flip_byte(path, int(1.5 * MIB))
                os.utime(str(path), ns=(seen_ns, seen_ns))
                self.assertTrue(fingerprint_matches(recorded, cheap_fingerprint(path)))
                result, hasher = self.check(path, recorded, sha)
                self.assertEqual(hasher.call_count, 1)
                self.assertEqual(result.reason, "SHA-256 of '{}' is {}, the record says {}".format(
                    path.name, sha256_file(path), sha))

    def test_another_time_without_a_recorded_hash_is_changed_without_a_read(self):
        path, recorded, _sha = self.planned("unhashed")
        os.utime(str(path), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        result, hasher = self.check(path, recorded, None)
        hasher.assert_not_called()
        self.assertIn("no source SHA-256", result.reason)

    def test_an_unusable_recorded_time_is_settled_by_a_read(self):
        path, recorded, sha = self.planned("edited_time")
        for value in (None, "yesterday", 1.7e18, True):
            with self.subTest(value=value):
                edited = dict(recorded, mtime_ns=value)
                result, hasher = self.check(path, edited, sha)
                self.assertTrue(fingerprint_matches(edited, cheap_fingerprint(path)))
                self.assertIsNone(result.reason)
                self.assertTrue(result.sha256_checked)

    def test_a_second_check_of_the_same_file_reuses_the_read_until_it_is_touched(self):
        path, recorded, sha = self.planned("checked_twice")
        os.utime(str(path), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        self.assertEqual(self.check(path, recorded, sha)[1].call_count, 1)
        result, hasher = self.check(path, recorded, sha)
        hasher.assert_not_called()
        self.assertIsNone(result.reason)
        self.assertTrue(result.sha256_checked)
        flip_byte(path, int(1.5 * MIB))    # same size, ends and time; the inode change time moves
        result, hasher = self.check(path, recorded, sha)
        self.assertEqual(hasher.call_count, 1)
        self.assertIn("SHA-256 of", result.reason)

    def test_a_file_written_while_it_is_read_is_changed(self):
        path, recorded, sha = self.planned("written_meanwhile")
        os.utime(str(path), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        real = record_module.full_sha256

        def read_then_touch(*arguments, **options):
            digest = real(*arguments, **options)
            os.utime(str(path), ns=(PLANNED_MTIME_NS + 2 * SECOND_NS, PLANNED_MTIME_NS + 2 * SECOND_NS))
            return digest

        with mock.patch("vidfunnel.record.full_sha256", side_effect=read_then_touch):
            result = check_source([(path, recorded)], sha, path.name, quiet=True)
        self.assertEqual(result.reason, "'{}' changed while it was being read".format(path.name))

    def test_a_vob_group_is_read_as_its_members_in_order(self):
        first, first_print, _ = self.planned("first_member")
        second, second_print, _ = self.planned("second_member")
        joined = hashlib.sha256(first.read_bytes() + second.read_bytes()).hexdigest()
        os.utime(str(second), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        with mock.patch("vidfunnel.record.full_sha256", wraps=record_module.full_sha256) as hasher:
            result = check_source([(first, first_print), (second, second_print)], joined, "group", quiet=True)
        self.assertIsNone(result.reason)
        self.assertEqual(hasher.call_args[0][0], (os.path.abspath(str(first)), os.path.abspath(str(second))))
        self.assertEqual([note["name"] for note in result.notes], [second.name])

    def test_a_large_read_is_announced_with_live_progress(self):
        path, recorded, sha = self.planned("announced")
        os.utime(str(path), ns=(PLANNED_MTIME_NS + SECOND_NS, PLANNED_MTIME_NS + SECOND_NS))
        stream = io.StringIO()
        with mock.patch.object(record_module, "FULL_HASH_PROGRESS_BYTES", MIB):
            check_source([(path, recorded)], sha, path.name, stream=stream)
        text = stream.getvalue()
        self.assertIn("  reading '{}' in full (3.0 MiB): its modification time is neither the recorded one nor a "
                      "coarser view of it; comparing its SHA-256 with the record's".format(path.name), text)
        self.assertIn("[sha256] 3.0 MiB / 3.0 MiB  100.0 %", text)
        stream = io.StringIO()
        os.utime(str(path), ns=(PLANNED_MTIME_NS + 2 * SECOND_NS, PLANNED_MTIME_NS + 2 * SECOND_NS))
        check_source([(path, recorded)], sha, path.name, stream=stream)
        self.assertNotIn("[sha256]", stream.getvalue())

    def test_each_content_field_is_named_when_it_differs(self):
        path, recorded, _sha = self.planned("fields")
        flip_byte(path, 0)
        self.assertEqual(fingerprint_differences(recorded, cheap_fingerprint(path)), ["first MiB differs"])
        flip_byte(path, 0)
        flip_byte(path, 3 * MIB - 1)
        self.assertEqual(fingerprint_differences(recorded, cheap_fingerprint(path)), ["last MiB differs"])
        flip_byte(path, 3 * MIB - 1)
        with open(str(path), "ab") as handle:
            handle.write(b"appended")
        seen = cheap_fingerprint(path)
        self.assertFalse(fingerprint_matches(recorded, seen))
        self.assertEqual(fingerprint_differences(recorded, seen),
                         ["size {} bytes recorded, {} now".format(3 * MIB, 3 * MIB + 8), "last MiB differs"])

    def test_a_record_written_before_the_rule_reads_the_same(self):
        # The fingerprint shape is unchanged: the four fields, mtime_ns stored as before.
        path, recorded, _sha = self.planned("older")
        self.assertEqual(sorted(recorded), ["head_sha256", "mtime_ns", "size", "tail_sha256"])
        older = json.loads(json.dumps(recorded))
        self.assertTrue(fingerprint_matches(older, cheap_fingerprint(path)))

    def test_a_missing_or_malformed_fingerprint_is_named(self):
        path, recorded, _sha = self.planned("malformed")
        self.assertEqual(fingerprint_differences(None, recorded), ["no fingerprint recorded"])
        self.assertEqual(fingerprint_differences(["size"], recorded), ["no fingerprint recorded"])
        self.assertEqual(fingerprint_differences(recorded, None), ["no fingerprint taken"])
        self.assertFalse(fingerprint_matches(["size"], recorded))
        self.assertFalse(fingerprint_matches(dict(recorded, head_sha256=None), recorded))
        self.assertTrue(fingerprint_identical(recorded, cheap_fingerprint(path)))
        self.assertIsNone(mtime_difference(recorded, cheap_fingerprint(path)))


class Sha256Tests(TempDirCase):
    def test_matches_hashlib_with_progress(self):
        data = os.urandom(20 * MIB + 7)
        path = write_bytes(self.base / "content.bin", data)
        progress = RecordingProgress()
        self.assertEqual(sha256_file(path, progress), hashlib.sha256(data).hexdigest())
        self.assertEqual(progress.values, [8 * MIB, 16 * MIB, len(data)])

    def test_matches_hashlib_without_progress(self):
        data = os.urandom(5000)
        path = write_bytes(self.base / "content.bin", data)
        self.assertEqual(sha256_file(path), hashlib.sha256(data).hexdigest())


class StageAndStatusTests(TempDirCase):
    def make_record(self):
        source = write_bytes(self.base / "movie.avi", b"payload")
        return new_record(source, self.base)

    def test_add_stage_refuses_second_write(self):
        record = self.make_record()
        add_stage(record, "plan", {"argv": ["ffmpeg", "-i", "movie.avi"]})
        self.assertIn("at", record["stages"]["plan"])
        with self.assertRaises(ValueError):
            add_stage(record, "plan", {"argv": ["other"]})
        self.assertEqual(record["stages"]["plan"]["argv"], ["ffmpeg", "-i", "movie.avi"])

    def test_add_stage_rejects_unknown_stage(self):
        with self.assertRaises(ValueError):
            add_stage(self.make_record(), "encode", {})

    def test_set_status_rejects_unknown(self):
        record = self.make_record()
        with self.assertRaises(ValueError):
            set_status(record, "done")
        self.assertIsNone(record["status"])
        set_status(record, "verified")
        self.assertEqual(record["status"], "verified")
        self.assertTrue(record["status_at"].endswith("Z"))

    def test_add_flag_deduplicates_in_order(self):
        record = self.make_record()
        for flag in ("aspect-ambiguous", "vfr", "aspect-ambiguous"):
            add_flag(record, flag)
        self.assertEqual(record["flags"], ["aspect-ambiguous", "vfr"])


class IterRecordsTests(TempDirCase):
    def test_walks_nested_tree(self):
        upper = self.base / "shows"
        lower = self.base / "shows" / "season" / "disc"
        sources = [
            write_bytes(upper / "pilot.avi", b"one"),
            write_bytes(upper / "pilot.mkv", b"two"),
            write_bytes(lower / "finale.mpg", b"three"),
        ]
        expected = {}
        for source in sources:
            record = new_record(source, source.parent)
            save_record(source, record)
            expected[record["id"]] = source.parent
        save_folder(upper, {"sources": ["pilot.avi", "pilot.mkv"]})
        with FolderLock(lower):
            found = list(iter_records(self.base))
        self.assertEqual(len(found), 3)
        self.assertEqual({record["id"]: folder for folder, record in found}, expected)

    def test_unreadable_record_raises_or_reports(self):
        source = write_bytes(self.base / "good.avi", b"one")
        save_record(source, new_record(source, self.base))
        write_bytes(self.base / RECORD_DIR / "bad.avi.json", b"[1, 2")
        with self.assertRaises(RecordError):
            list(iter_records(self.base))
        problems = []
        found = list(iter_records(self.base, on_error=lambda path, error: problems.append(path.name)))
        self.assertEqual(len(found), 1)
        self.assertEqual(problems, ["bad.avi.json"])


def race_for_lock(folder, barrier, results, answered):
    """One racer of test_racers_for_a_dead_lock_get_it_once: wait at the barrier, try the lock, and hold it
    until every racer has answered, so a racer that is slow to try finds it held, not released again."""
    barrier.wait()
    try:
        with FolderLock(folder, warn=lambda message: None):
            results.put("ACQUIRED")
            answered.wait(120)
    except LockHeld:
        results.put("HELD")
    except BaseException as error:
        results.put("ERROR {!r}".format(error))


class FolderLockTests(TempDirCase):
    def lock_file(self):
        return self.base / RECORD_DIR / LOCK_NAME

    def test_second_acquisition_raises(self):
        with FolderLock(self.base):
            content = json.loads(self.lock_file().read_text(encoding="utf-8"))
            self.assertEqual(content["pid"], os.getpid())
            self.assertEqual(sorted(content),
                             ["boot", "container", "device", "flock", "heartbeat", "host", "machine",
                              "machine_source", "pid", "started"])
            self.assertEqual(content["heartbeat"], {"interval": HEARTBEAT_SECONDS})
            self.assertEqual((content["machine"], content["machine_source"]), machine_identity())
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base):
                    pass
            self.assertEqual(caught.exception.pid, os.getpid())
            self.assertTrue(self.lock_file().exists())
        self.assertFalse(self.lock_file().exists())

    def test_stale_lock_is_taken_over_with_warning(self):
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
        stale = {"host": "otherhost", "pid": 4242, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ")}
        write_bytes(self.lock_file(), json.dumps(stale).encode("utf-8"))
        warnings = []
        with FolderLock(self.base, warn=warnings.append):
            content = json.loads(self.lock_file().read_text(encoding="utf-8"))
            self.assertEqual(content["pid"], os.getpid())
        self.assertEqual(len(warnings), 1)
        self.assertIn("'otherhost'", warnings[0])
        self.assertIn("4242", warnings[0])
        self.assertFalse(self.lock_file().exists())

    def test_fresh_foreign_lock_is_not_taken_over(self):
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=23)
        foreign = {"host": "otherhost", "pid": 4242, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ")}
        write_bytes(self.lock_file(), json.dumps(foreign).encode("utf-8"))
        with self.assertRaises(LockHeld) as caught:
            with FolderLock(self.base, warn=self.fail):
                pass
        self.assertEqual(caught.exception.host, "otherhost")
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), foreign)

    def write_lock(self, host, pid, hours_ago, container=None, machine="this"):
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
        content = {"host": host, "pid": pid, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if container is not None:
            content["container"] = container
        if machine == "this":
            machine = machine_id()
        if machine is not None:
            content["machine"] = machine
        write_bytes(self.lock_file(), json.dumps(content).encode("utf-8"))
        return content

    def finished_pid(self):
        """The pid of a child that has exited and been reaped, so no process holds it (barring reuse)."""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def test_machine_id_falls_back_to_the_hardware_address_and_rejects_a_random_node(self):
        # macOS has neither /etc/machine-id nor /proc; CI there found every pid unjudgeable.
        absent = (str(self.base / "no-machine-id"), str(self.base / "no-boot-id"))
        with mock.patch("vidfunnel.record.MACHINE_ID_FILES", absent), \
                mock.patch("vidfunnel.record.uuid.getnode", return_value=0x001122334455):
            self.assertEqual(machine_id(), "node-001122334455")
        with mock.patch("vidfunnel.record.MACHINE_ID_FILES", absent), \
                mock.patch("vidfunnel.record.uuid.getnode", return_value=0x011122334455):
            self.assertIsNone(machine_id(), "the multicast bit marks getnode's random fallback")
        self.assertIsNotNone(machine_id(), "this machine has an identity")

    def test_machine_identity_names_its_source(self):
        machine_file = self.base / "machine-id"
        boot_file = self.base / "boot-id"
        files = (str(machine_file), str(boot_file))
        boot_file.write_text("boot-value\n")
        with mock.patch("vidfunnel.record.MACHINE_ID_FILES", files):
            self.assertEqual(machine_identity(), ("boot-value", "boot-id"))
            machine_file.write_text("\n")
            self.assertEqual(machine_identity(), ("boot-value", "boot-id"), "an empty machine-id is skipped")
            machine_file.write_text("machine-value\n")
            self.assertEqual(machine_identity(), ("machine-value", "machine-id"))
        absent = (str(self.base / "no-machine-id"), str(self.base / "no-boot-id"))
        with mock.patch("vidfunnel.record.MACHINE_ID_FILES", absent), \
                mock.patch("vidfunnel.record.uuid.getnode", return_value=0x001122334455):
            self.assertEqual(machine_identity(), ("node-001122334455", "hardware"))
        with mock.patch("vidfunnel.record.MACHINE_ID_FILES", absent), \
                mock.patch("vidfunnel.record.uuid.getnode", return_value=0x011122334455):
            self.assertEqual(machine_identity(), (None, None))

    def test_null_lock_file_is_judged_by_age_and_never_loops(self):
        # A lock file holding JSON null once sent acquire() into an endless loop (the base crashed on it).
        script = ("import sys; from vidfunnel.record import FolderLock, LockHeld\n"
                  "try:\n"
                  "    with FolderLock(sys.argv[1], warn=lambda line: print('WARN', line)):\n"
                  "        print('ACQUIRED')\n"
                  "except LockHeld as held:\n"
                  "    print('HELD', held)\n")
        write_bytes(self.lock_file(), b"null")
        result = subprocess.run([sys.executable, "-c", script, str(self.base)], capture_output=True,
                                universal_newlines=True, timeout=20, env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HELD", result.stdout, "a fresh unreadable lock is respected by age")
        old = (datetime.datetime.now() - datetime.timedelta(hours=25)).timestamp()
        os.utime(str(self.lock_file()), (old, old))
        result = subprocess.run([sys.executable, "-c", script, str(self.base)], capture_output=True,
                                universal_newlines=True, timeout=20, env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACQUIRED", result.stdout, "an old unreadable lock is taken over")
        self.assertIn("stale lock", result.stdout)

    def test_dead_pid_from_another_machine_with_this_host_name_is_not_judged(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False, machine="another-machine-id")
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as held:
                FolderLock(self.base, warn=lambda line: None).acquire()
        self.assertIn("another machine with this host name", str(held.exception))
        self.write_lock(socket.gethostname(), dead, 0.5, container=False, machine=None)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld):
                FolderLock(self.base, warn=lambda line: None).acquire()

    def test_takeover_flock_is_taken_on_a_regular_file(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        opened = []
        real_open = os.open

        def recording_open(path, flags, *rest):
            opened.append(str(path))
            return real_open(path, flags, *rest)

        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("vidfunnel.record.os.open", side_effect=recording_open):
            with FolderLock(self.base, warn=lambda line: None):
                pass
        takeover = str(self.lock_file().parent / "lock.takeover")
        self.assertIn(takeover, opened, "the flock is taken on the regular takeover file")
        marker = str(self.lock_file().parent / TAKEOVER_MARKER)
        new_lock = [number for number, path in enumerate(opened) if os.path.basename(path).startswith(NEW_LOCK_PREFIX)]
        self.assertEqual(len(new_lock), 1, opened)
        self.assertLess(opened.index(takeover), opened.index(marker), "the guard is held before the marker is created")
        self.assertLess(opened.index(marker), new_lock[0], "the marker is created before our new lock file")
        self.assertTrue(Path(takeover).exists())
        self.assertFalse(Path(marker).exists())

    def test_dead_holder_on_this_host_is_taken_over_at_once(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                content = json.loads(self.lock_file().read_text(encoding="utf-8"))
                self.assertEqual(content["pid"], os.getpid())
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pid {}".format(dead), warnings[0])
        self.assertIn("no longer runs", warnings[0])
        self.assertRegex(warnings[0], r"lock written 18[0-9][0-9] s ago")
        self.assertFalse(self.lock_file().exists())

    def test_dead_holder_in_a_lock_without_the_container_field_is_taken_over(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.1)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                pass
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pid {}".format(dead), warnings[0])

    def live_pid(self):
        """The pid of a running child that is not this process; it is stopped at cleanup."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child.pid

    def test_live_holder_on_this_host_is_respected(self):
        live = self.live_pid()
        held = self.write_lock(socket.gethostname(), live, 0.5, container=False)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("its pid {} is running here".format(live), str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)

    def test_live_holder_on_this_host_is_taken_over_once_stale(self):
        """A live pid cannot lengthen the wait: after 24 h it is taken for a reused pid."""
        live = self.live_pid()
        self.write_lock(socket.gethostname(), live, 25, container=False)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                content = json.loads(self.lock_file().read_text(encoding="utf-8"))
                self.assertEqual(content["pid"], os.getpid())
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("stale lock", warnings[0])
        self.assertIn("pid {}".format(live), warnings[0])
        self.assertIn("reused", warnings[0])
        self.assertNotIn("its pid {} is running here".format(live), warnings[0])

    def test_our_own_pid_counts_as_unknown(self):
        """A lock naming this process (a container's PID 1, a reused pid) is judged by age alone."""
        held = self.write_lock(socket.gethostname(), os.getpid(), 0.5, container=False)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("this process's own", str(caught.exception))
        self.assertNotIn("is running here", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)
        self.write_lock(socket.gethostname(), os.getpid(), 25, container=False)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                pass
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("stale lock", warnings[0])

    def test_pids_os_kill_cannot_take_are_unknown(self):
        for pid in (99999999999999, 2 ** 31, 10 ** 30, "4242", 0, -5, True, None, 1.5):
            with self.subTest(pid=pid):
                self.assertIsNone(pid_alive(pid))

    def test_an_oversized_pid_in_a_lock_falls_back_to_the_age_rule(self):
        for pid in (99999999999999, 2 ** 31, 10 ** 30, "4242"):
            with self.subTest(pid=pid), mock.patch("vidfunnel.record.in_container", return_value=False):
                held = self.write_lock(socket.gethostname(), pid, 0.5, container=False)
                with self.assertRaises(LockHeld) as caught:
                    with FolderLock(self.base, warn=self.fail):
                        pass
                self.assertIn("cannot be checked", str(caught.exception))
                self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)
                self.write_lock(socket.gethostname(), pid, 25, container=False)
                warnings = []
                with FolderLock(self.base, warn=warnings.append):
                    pass
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("stale lock", warnings[0])

    def test_racers_for_a_dead_lock_get_it_once(self):
        """Four processes take over the same dead holder's lock at one instant, 20 times; one gets it each time."""
        context = multiprocessing.get_context("spawn")
        racers = 4
        for round_number in range(20):
            with self.subTest(round=round_number):
                self.write_lock(socket.gethostname(), self.finished_pid(), 0.5, container=in_container())
                barrier = context.Barrier(racers)
                results = context.Queue()
                answered = context.Event()
                processes = [context.Process(target=race_for_lock, args=(str(self.base), barrier, results, answered))
                             for _number in range(racers)]
                for process in processes:
                    process.start()
                try:
                    outcomes = sorted(results.get(timeout=120) for _number in range(racers))
                finally:
                    answered.set()
                for process in processes:
                    process.join(120)
                self.assertEqual(outcomes, ["ACQUIRED"] + ["HELD"] * (racers - 1))
                self.assertFalse(self.lock_file().exists())

    def test_a_lock_put_in_place_just_after_our_rename_names_the_winner(self):
        """Another run's file replaces ours right after our rename: LockHeld naming it, its file left alone."""
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        winner = {"host": "winnerhost", "pid": 777, "started": "2026-11-07T12:00:00Z", "container": False}
        steps = []

        def replaced_after_the_rename(step):
            steps.append(step)
            if step == "renamed":
                staged = self.lock_file().parent / "winner-staged"
                write_bytes(staged, json.dumps(winner).encode("utf-8"))
                os.replace(str(staged), str(self.lock_file()))

        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("vidfunnel.record._takeover_step", side_effect=replaced_after_the_rename):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("renamed", steps)
        self.assertEqual((caught.exception.host, caught.exception.pid), ("winnerhost", 777))
        self.assertIn("got it first", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), winner)
        self.assertEqual(sorted(os.listdir(str(self.lock_file().parent))), ["lock", "lock.takeover"])
        with held_lock_fds() as descriptors:
            self.assertEqual(descriptors, ())

    @unittest.skipIf(sys.platform == "win32", "flock is POSIX")
    def test_a_takeover_is_judged_under_the_directory_flock(self):
        import fcntl
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        real_alive = pid_alive
        seen = []

        def probe_flock(pid):
            handle = os.open(str(self.base / RECORD_DIR / "lock.takeover"), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
            finally:
                os.close(handle)
            return real_alive(pid)

        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("vidfunnel.record.pid_alive", side_effect=probe_flock):
            with FolderLock(self.base, warn=lambda message: None):
                pass
        self.assertEqual(seen, ["held"])

    def test_a_file_system_without_directory_flock_still_takes_over(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("fcntl.flock", side_effect=OSError(95, "Operation not supported")):
            with FolderLock(self.base, warn=warnings.append):
                content = json.loads(self.lock_file().read_text(encoding="utf-8"))
                self.assertEqual(content["pid"], os.getpid())
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no longer runs", warnings[0])

    def test_a_lock_replaced_after_it_was_judged_is_not_unlinked(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        winner = {"host": "winnerhost", "pid": 778, "started": "2026-11-07T12:00:00Z", "container": False}
        real_alive = pid_alive

        def judged_then_replaced(pid):
            result = real_alive(pid)
            self.lock_file().unlink()
            write_bytes(self.lock_file(), json.dumps(winner).encode("utf-8"))
            return result

        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("vidfunnel.record.pid_alive", side_effect=judged_then_replaced):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertEqual(caught.exception.pid, 778)
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), winner)

    def test_holder_we_may_not_signal_counts_as_alive(self):
        held = self.write_lock(socket.gethostname(), 4242, 0.5, container=False)
        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("vidfunnel.record.os.kill", side_effect=PermissionError("not ours")):
            with self.assertRaises(LockHeld):
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)

    def test_other_host_with_a_dead_looking_pid_is_respected_until_stale(self):
        dead = self.finished_pid()
        held = self.write_lock("otherhost", dead, 23, container=False)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
            self.assertIn("another host name", str(caught.exception))
            self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)
            self.write_lock("otherhost", dead, 25, container=False)
            warnings = []
            with FolderLock(self.base, warn=warnings.append):
                pass
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("stale lock", warnings[0])

    def test_container_lock_with_our_host_name_is_judged_by_age(self):
        dead = self.finished_pid()
        held = self.write_lock(socket.gethostname(), dead, 1, container=True)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("held by a container run", str(caught.exception))
        self.assertIn("a container run on host", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)

    def test_container_marker_is_written_and_read(self):
        markers = []

        def exists(path):
            markers.append(path)
            return path == "/run/.containerenv"

        with mock.patch("vidfunnel.record.os.path.exists", side_effect=exists):
            with FolderLock(self.base):
                content = json.loads(self.lock_file().read_text(encoding="utf-8"))
        self.assertIs(content["container"], True)
        self.assertIn("/run/.containerenv", markers)
        with mock.patch("vidfunnel.record.os.path.exists", return_value=False):
            with FolderLock(self.base):
                content = json.loads(self.lock_file().read_text(encoding="utf-8"))
        self.assertIs(content["container"], False)
        held = self.write_lock("0123abcdef45", 1, 2, container=True)
        with self.assertRaises(LockHeld) as caught:
            with FolderLock(self.base, warn=self.fail):
                pass
        self.assertIn("held by a container run", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)

    def test_lock_removed_after_exception(self):
        with self.assertRaises(RuntimeError):
            with FolderLock(self.base):
                self.assertTrue(self.lock_file().exists())
                raise RuntimeError("boom")
        self.assertFalse(self.lock_file().exists())



# A lock holder in a process of its own. argv: folder, mode ("plain", "container", "old", "child"),
# and for "child" the stand-in to run through vidfunnel.ffmpeg.run. It prints ACQUIRED once it holds the lock.
HOLDER_SCRIPT = """
import sys, time
from unittest import mock
import vidfunnel.ffmpeg
import vidfunnel.record
from vidfunnel.record import FolderLock
folder, mode = sys.argv[1], sys.argv[2]
patches = []
if mode == "container":
    patches.append(mock.patch("vidfunnel.record.in_container", return_value=True))
if mode == "old":
    patches.append(mock.patch("vidfunnel.record.now_iso", return_value="2000-01-01T00:00:00Z"))
for patch in patches:
    patch.start()
with FolderLock(folder, warn=lambda line: print("WARN", line, flush=True)):
    print("ACQUIRED", flush=True)
    if mode == "child":
        vidfunnel.ffmpeg.run([sys.argv[3]], check=False)
    time.sleep(60)
"""


@unittest.skipIf(not hasattr(os, "fork") or not Path("/proc/sys/kernel/random/boot_id").exists(),
                 "the flock rule needs a POSIX system with a kernel boot id (Linux)")
class FlockHolderTests(TempDirCase):
    """The holder keeps flock(LOCK_EX) on its lock file for the run; a judge on the same kernel tests it."""

    def lock_file(self):
        return self.base / RECORD_DIR / LOCK_NAME

    def start_holder(self, mode, *extra):
        script = self.base / "holder.py"
        script.write_text(HOLDER_SCRIPT, encoding="utf-8")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        holder = subprocess.Popen([sys.executable, str(script), str(self.base), mode] + list(extra),
                                  stdout=subprocess.PIPE, universal_newlines=True, env=environment)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(self.kill_quietly, holder.pid)
        self.assertEqual(holder.stdout.readline(), "ACQUIRED\n")
        return holder

    @staticmethod
    def kill_quietly(pid):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    @staticmethod
    def has_ended(pid):
        """True once `pid` has exited. An orphan is reparented to PID 1 (or a subreaper); when that is this
        test process ('podman run ... sh -c "... && python3 -m unittest"': ash execs its last command), the
        exited orphan stays a zombie that os.kill(pid, 0) still finds until it is reaped here."""
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return not pid_alive(pid)
        return reaped == pid

    def judge(self, warnings):
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                content = json.loads(self.lock_file().read_text(encoding="utf-8"))
                self.assertEqual(content["pid"], os.getpid())

    def test_the_lock_records_its_flock_boot_and_device(self):
        holder = self.start_holder("plain")
        content = json.loads(self.lock_file().read_text(encoding="utf-8"))
        self.assertEqual(content["pid"], holder.pid)
        self.assertIs(content["flock"], True)
        self.assertEqual(content["boot"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        self.assertEqual(content["device"], os.stat(str(self.lock_file())).st_dev)

    def test_a_held_flock_is_respected_even_past_24_hours(self):
        """The holder's pid rule would take a 24 h old lock for a reused pid; a held flock is never taken."""
        holder = self.start_holder("old")
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertEqual(caught.exception.pid, holder.pid)
        self.assertIn("still holds the lock file's flock", str(caught.exception))
        self.assertIn("whatever its age", str(caught.exception))
        self.assertGreater(caught.exception.age_seconds, 30 * 3600)

    def test_a_killed_container_holder_is_taken_over_at_once(self):
        """A fresh lock from a container run (pid not checkable here) is free as soon as its holder is dead."""
        holder = self.start_holder("container")
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld):
                with FolderLock(self.base, warn=self.fail):
                    pass
        holder.kill()
        holder.wait()
        warnings = []
        self.judge(warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("a container run on host", warnings[0])
        self.assertIn("pid {}, has exited".format(holder.pid), warnings[0])
        self.assertIn("nothing holds the lock file's flock", warnings[0])
        self.assertFalse(self.lock_file().exists())

    def write_flock_lock(self, pid, hours_ago, container, **fields):
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
        content = {"host": socket.gethostname(), "pid": pid, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "container": container, "machine": machine_id(), "flock": True,
                   "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
        write_bytes(self.lock_file(), b"{}")
        content["device"] = os.stat(str(self.lock_file())).st_dev
        content.update(fields)
        write_bytes(self.lock_file(), json.dumps(content).encode("utf-8"))
        return content

    def test_another_boot_or_device_falls_back_to_the_age_rule(self):
        """Nothing holds this file's flock; a judge that tested it would take the lock over at once."""
        # Another boot id on this machine would be the reboot rule (RebootRuleTests); here the lock names
        # another machine.
        cases = (({"boot": "another-boot", "machine": "another-machine"}, "another machine with this host name"),
                 ({"boot": None}, "held by a container run"), ({"device": 1}, "held by a container run"))
        for fields, reason in cases:
            with self.subTest(fields=fields), mock.patch("vidfunnel.record.in_container", return_value=False):
                held = self.write_flock_lock(1, 2, True, **fields)
                with self.assertRaises(LockHeld) as caught:
                    with FolderLock(self.base, warn=self.fail):
                        pass
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)
                self.write_flock_lock(1, 25, True, **fields)
                warnings = []
                with FolderLock(self.base, warn=warnings.append):
                    pass
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("stale lock", warnings[0])

    def test_flock_false_falls_back_to_the_pid_rule(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.write_flock_lock(child.pid, 0.5, False, flock=False)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("its pid {} is running here".format(child.pid), str(caught.exception))
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        self.write_flock_lock(finished.pid, 0.5, False, flock=False)
        warnings = []
        self.judge(warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no longer runs", warnings[0])

    def test_a_file_system_refusing_flock_writes_flock_false_and_still_locks(self):
        import fcntl
        real_flock = fcntl.flock

        def refuse_exclusive(handle, operation):
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                raise OSError(95, "Operation not supported")
            return real_flock(handle, operation)

        with mock.patch("vidfunnel.record.fcntl.flock", side_effect=refuse_exclusive):
            lock = FolderLock(self.base).acquire()
        try:
            content = json.loads(self.lock_file().read_text(encoding="utf-8"))
            self.assertIs(content["flock"], False)
            script = ("import sys; from vidfunnel.record import FolderLock, LockHeld\n"
                      "try:\n"
                      "    FolderLock(sys.argv[1], warn=lambda line: print('WARN', line)).acquire()\n"
                      "    print('ACQUIRED')\n"
                      "except LockHeld as held:\n"
                      "    print('HELD', held)\n")
            result = subprocess.run([sys.executable, "-c", script, str(self.base)], capture_output=True,
                                    universal_newlines=True, timeout=20,
                                    env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
            self.assertIn("HELD", result.stdout, result.stderr)
            self.assertIn("its pid {} is running here".format(os.getpid()), result.stdout)
        finally:
            lock.release()
        self.assertFalse(self.lock_file().exists())

    def test_release_unlinks_the_file_before_it_closes_the_descriptor(self):
        lock = FolderLock(self.base).acquire()
        handle = lock.handle
        steps = []
        real_unlink, real_close = os.unlink, os.close

        def unlink(path, *rest, **options):
            if str(path) == str(self.lock_file()):
                steps.append("unlink")
            return real_unlink(path, *rest, **options)

        def close(descriptor):
            if descriptor == handle:
                steps.append("close")
            return real_close(descriptor)

        with mock.patch("vidfunnel.record.os.unlink", side_effect=unlink), \
                mock.patch("vidfunnel.record.os.close", side_effect=close):
            lock.release()
        self.assertEqual(steps, ["unlink", "close"])
        self.assertFalse(self.lock_file().exists())
        with self.assertRaises(OSError):
            os.fstat(handle)

    def test_a_judge_in_the_holding_process_does_not_open_the_lock_file(self):
        """On NFS, closing any descriptor of a file can drop the process's locks on it."""
        with FolderLock(self.base):
            opened = []
            real_open, real_os_open = open, os.open

            def recording_open(path, *rest, **options):
                stream = real_open(path, *rest, **options)
                opened.append(str(path))
                return stream

            def recording_os_open(path, *rest):
                handle = real_os_open(path, *rest)
                opened.append(str(path))
                return handle

            with mock.patch("builtins.open", side_effect=recording_open), \
                    mock.patch("vidfunnel.record.os.open", side_effect=recording_os_open):
                with self.assertRaises(LockHeld) as caught:
                    FolderLock(self.base).acquire()
            self.assertIn("this run itself", str(caught.exception))
            self.assertNotIn(str(self.lock_file()), opened, "the lock file was opened (only its O_EXCL create may fail)")
            self.assertTrue(self.lock_file().exists())

    def test_children_inherit_every_held_lock_descriptor(self):
        """plan --jobs holds several folder locks at once; every child gets all of them."""
        from vidfunnel.ffmpeg import run
        other = self.base / "other"
        check = "import os, sys\nfor number in sys.argv[1:]:\n    os.fstat(int(number))\n"
        with FolderLock(self.base) as first, FolderLock(other) as second:
            # Taken inside the block: release() sets handle to None.
            released = first.handle
            completed = run([sys.executable, "-c", check, str(first.handle), str(second.handle)], check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsInstance(released, int)
        completed = run([sys.executable, "-c", check, str(released)], check=False)
        self.assertNotEqual(completed.returncode, 0, "a released lock's descriptor is passed no more")
        self.assertIn("Bad file descriptor", completed.stderr)

    def test_release_reads_the_lock_through_its_own_descriptor(self):
        """release() never opens the lock path (on NFS a close of any descriptor can drop the flock)."""
        import builtins
        lock = FolderLock(self.base, warn=self.fail)
        lock.acquire()
        lock_path = str(lock.path)
        real_open = builtins.open
        real_os_open = os.open
        opened = []

        def refuse_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and os.fspath(file) == lock_path:
                opened.append(file)
                raise PermissionError("the test refuses to open '{}'".format(lock_path))
            return real_open(file, *args, **kwargs)

        def refuse_os_open(path, *args, **kwargs):
            if os.fspath(path) == lock_path:
                opened.append(path)
                raise PermissionError("the test refuses to open '{}'".format(lock_path))
            return real_os_open(path, *args, **kwargs)

        with mock.patch("builtins.open", refuse_open), mock.patch("os.open", refuse_os_open):
            lock.release()
        self.assertEqual(opened, [], "release() opened the lock path")
        self.assertFalse(os.path.exists(lock_path), "release() left the lock file in place")

    def test_an_orphaned_child_keeps_the_lock_until_it_exits(self):
        """The holder is killed while its 'ffmpeg' runs; the lock stays held until that child ends."""
        marker = self.base / "child.pid"
        stand_in = self.base / "ffmpeg-stand-in"
        stand_in.write_text("#!/bin/sh\necho $$ > '{}'\nexec sleep 60\n".format(marker), encoding="utf-8")
        os.chmod(str(stand_in), 0o755)
        holder = self.start_holder("child", str(stand_in))
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=10)
        while not (marker.exists() and marker.read_text().strip()):
            self.assertLess(datetime.datetime.now(), deadline, "the stand-in never started")
            time.sleep(0.02)
        child_pid = int(marker.read_text().strip())
        self.addCleanup(self.kill_quietly, child_pid)
        holder.kill()
        holder.wait()
        self.assertFalse(pid_alive(holder.pid))
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("still holds the lock file's flock", str(caught.exception))
        os.kill(child_pid, 9)
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=10)
        while not self.has_ended(child_pid):
            self.assertLess(datetime.datetime.now(), deadline, "the stand-in did not end")
            time.sleep(0.02)
        warnings = []
        self.judge(warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pid {}, has exited".format(holder.pid), warnings[0])

    def test_a_held_flock_wins_over_a_silent_beat(self):
        """Same machine: a busy flock means alive, however long ago the lock last beat."""
        holder = self.start_holder("plain")
        self.assertIn("heartbeat", json.loads(self.lock_file().read_text(encoding="utf-8")))
        silent = time.time_ns() - 2 * 3600 * 10 ** 9
        os.utime(str(self.lock_file()), ns=(silent, silent))
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertEqual(caught.exception.pid, holder.pid)
        self.assertIn("still holds the lock file's flock", str(caught.exception))

    def test_a_free_flock_wins_over_a_fresh_beat(self):
        """Same machine: a free flock means every holder has exited, though the lock beat a moment ago."""
        self.write_flock_lock(1, 0.1, True, heartbeat={"interval": HEARTBEAT_SECONDS})
        warnings = []
        self.judge(warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pid 1, has exited", warnings[0])


@unittest.skipIf(machine_identity()[1] != "machine-id" or not Path("/proc/sys/kernel/random/boot_id").exists(),
                 "the reboot rule needs /etc/machine-id and a kernel boot id")
class RebootRuleTests(TempDirCase):
    """A lock written on this machine under an earlier boot id is taken over at once."""

    OTHER_BOOT = "0d6e2f4a-1b3c-4d5e-8f70-123456789abc"

    def lock_file(self):
        return self.base / RECORD_DIR / LOCK_NAME

    def live_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child.pid

    def write_lock(self, pid, seconds_ago, **fields):
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
        content = {"host": socket.gethostname(), "pid": pid, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "container": False, "machine": machine_id(), "machine_source": "machine-id",
                   "boot": self.OTHER_BOOT, "flock": False, "device": 1}
        content.update(fields)
        content = {key: value for key, value in content.items() if value is not None}
        write_bytes(self.lock_file(), json.dumps(content).encode("utf-8"))
        return content

    def test_a_lock_from_before_the_last_reboot_is_taken_over_at_once(self):
        live = self.live_pid()
        for flock in (False, True):
            with self.subTest(flock=flock):
                self.write_lock(live, 10, flock=flock)
                warnings = []
                with mock.patch("vidfunnel.record.in_container", return_value=False):
                    with FolderLock(self.base, warn=warnings.append):
                        content = json.loads(self.lock_file().read_text(encoding="utf-8"))
                        self.assertEqual(content["pid"], os.getpid())
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("taking over lock '{}'".format(self.lock_file()), warnings[0])
                self.assertIn("host '{}' pid {}".format(socket.gethostname(), live), warnings[0])
                self.assertIn("ran before this machine's last reboot", warnings[0])
                self.assertRegex(warnings[0], r"lock written 1[0-9] s ago")
                self.assertFalse(self.lock_file().exists())

    def test_a_container_lock_from_before_the_last_reboot_is_taken_over_too(self):
        self.write_lock(1, 10, host="0123abcdef45", container=True)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                pass
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("a container run on host '0123abcdef45' pid 1", warnings[0])
        self.assertIn("ran before this machine's last reboot", warnings[0])

    def assert_not_the_reboot_rule(self, **fields):
        """A fresh lock with a live pid stays held; at 25 h it is a stale lock, never the reboot message."""
        live = self.live_pid()
        held = self.write_lock(live, 10, **fields)
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertNotIn("reboot", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)
        self.write_lock(live, 25 * 3600, **fields)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                pass
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("stale lock", warnings[0])
        self.assertNotIn("reboot", warnings[0])

    def test_a_machine_from_the_boot_id_fallback_proves_nothing(self):
        self.assert_not_the_reboot_rule(machine_source="boot-id", boot=machine_id())

    def test_a_lock_without_machine_source_whose_machine_is_its_boot_proves_nothing(self):
        self.assert_not_the_reboot_rule(machine_source=None, boot=machine_id())

    def test_machine_source_decides_when_present(self):
        """machine_source 'boot-id' wins over a machine that differs from the boot id."""
        self.assert_not_the_reboot_rule(machine_source="boot-id")

    def test_a_lock_without_machine_source_whose_machine_differs_from_its_boot_is_judged(self):
        self.write_lock(self.live_pid(), 10, machine_source=None)
        warnings = []
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with FolderLock(self.base, warn=warnings.append):
                pass
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("ran before this machine's last reboot", warnings[0])

    def test_another_machine_with_another_boot_id_waits_for_the_age_rule(self):
        self.assert_not_the_reboot_rule(machine="another-machine")

    def test_our_boot_id_is_not_a_reboot(self):
        """Same machine, same boot, flock false, live pid: the pid rule, as before."""
        live = self.live_pid()
        self.write_lock(live, 10, boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertIn("its pid {} is running here".format(live), str(caught.exception))

    def test_a_lock_without_a_boot_id_is_not_a_reboot(self):
        self.assert_not_the_reboot_rule(boot=None)


def heartbeat_threads():
    return [thread for thread in threading.enumerate() if thread.name == "vidfunnel-heartbeat" and thread.is_alive()]


class ShiftedClock:
    """This process's wall clock moved by `shift_seconds`, for vidfunnel.record's 'time' and 'datetime' names."""

    def __init__(self, shift_seconds):
        shift = shift_seconds

        class ShiftedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.now(tz) + datetime.timedelta(seconds=shift)

        self.time = types.SimpleNamespace(time=lambda: time.time() + shift,
                                          time_ns=lambda: time.time_ns() + int(shift * 10 ** 9),
                                          monotonic=time.monotonic, sleep=time.sleep)
        self.datetime = types.SimpleNamespace(datetime=ShiftedDateTime, timezone=datetime.timezone,
                                              timedelta=datetime.timedelta)

    def patches(self):
        return (mock.patch.object(vidfunnel.record, "time", self.time),
                mock.patch.object(vidfunnel.record, "datetime", self.datetime))


class HeartbeatTests(TempDirCase):
    """The holder's beat, the judge's 60-minute rule on the share's clock, and a holder that loses its lock."""

    def setUp(self):
        # The folder goes last, after the cleanups that release locks in it (tearDown runs before them).
        self.holder = tempfile.TemporaryDirectory(prefix="vidfunnel-record-test-")
        self.base = Path(self.holder.name)
        self.addCleanup(self.holder.cleanup)
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)

    def tearDown(self):
        pass

    def lock_file(self):
        return self.base / RECORD_DIR / LOCK_NAME

    def fast_beats(self, interval=0.02):
        """Beat every `interval` seconds for the rest of this test (locks taken from now on).

        First waits for the heartbeat thread of an earlier lock to end: one
        started with the default constants sleeps up to a whole default poll
        (1 s) before it sees a new lock, so a lock taken meanwhile would not
        beat at the patched interval.
        """
        self.no_heartbeat_thread()
        for name, value in (("HEARTBEAT_SECONDS", interval), ("HEARTBEAT_POLL_SECONDS", min(interval, 0.01))):
            patch = mock.patch.object(vidfunnel.record, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def wait_until(self, predicate, seconds, message):
        deadline = time.monotonic() + seconds
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.005)

    def write_other_lock(self, beat_seconds_ago, started_hours_ago=1.0, heartbeat=True):
        """A lock written on another machine, its file's mtime (its last beat) `beat_seconds_ago`."""
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=started_hours_ago)
        content = {"host": "otherhost", "pid": 4242, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "container": False, "machine": "another-machine", "machine_source": "machine-id",
                   "boot": "another-boot", "flock": True, "device": 1}
        if heartbeat:
            content["heartbeat"] = {"interval": HEARTBEAT_SECONDS}
        write_bytes(self.lock_file(), json.dumps(content).encode("utf-8"))
        beat = time.time_ns() - int(beat_seconds_ago * 10 ** 9)
        os.utime(str(self.lock_file()), ns=(beat, beat))
        return content

    def judge(self, warnings=None):
        """Take the lock (returned, released at cleanup) or raise LockHeld."""
        with mock.patch("vidfunnel.record.in_container", return_value=False):
            lock = FolderLock(self.base, warn=(warnings.append if warnings is not None else self.fail)).acquire()
        self.addCleanup(lock.release)
        return lock

    def assert_held(self, content):
        with self.assertRaises(LockHeld) as caught:
            self.judge()
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), content)
        return str(caught.exception)

    def assert_taken(self):
        warnings = []
        lock = self.judge(warnings)
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8"))["pid"], os.getpid())
        self.assertEqual(len(warnings), 1, warnings)
        lock.release()
        return warnings[0]

    def probes_left(self):
        return [name for name in os.listdir(str(self.base / RECORD_DIR)) if name.startswith(PROBE_PREFIX)]

    def test_a_lock_without_the_heartbeat_field_keeps_the_24_hour_rule(self):
        """A lock from an older vidfunnel: neither a silent mtime nor the 60-minute rule takes it."""
        held = self.write_other_lock(2 * 3600, started_hours_ago=2, heartbeat=False)
        message = self.assert_held(held)
        self.assertIn("taken over once 24 h old", message)
        self.assertNotIn("beat", message)
        held = self.write_other_lock(23.5 * 3600, started_hours_ago=23.5, heartbeat=False)
        self.assert_held(held)
        self.write_other_lock(24.5 * 3600, started_hours_ago=24.5, heartbeat=False)
        self.assertIn("stale lock", self.assert_taken())
        self.assertEqual(self.probes_left(), [])

    def test_the_beat_advances_the_mtime_and_keeps_the_inode_and_content(self):
        self.fast_beats()
        lock = self.judge()
        original = self.lock_file().read_bytes()
        before = os.stat(str(self.lock_file()))
        os.utime(str(self.lock_file()), ns=(10 ** 9, 10 ** 9))
        self.wait_until(lambda: os.stat(str(self.lock_file())).st_mtime_ns > 10 ** 9, 30,
                        "no beat advanced the lock file's mtime")
        first = lock.beat.count
        self.wait_until(lambda: lock.beat.count >= first + 20, 30, "the holder stopped beating")
        after = os.stat(str(self.lock_file()))
        self.assertEqual((after.st_dev, after.st_ino, after.st_size), (before.st_dev, before.st_ino, before.st_size))
        self.assertEqual(self.lock_file().read_bytes(), original)
        self.assertEqual(json.loads(original.decode("utf-8")), lock.content)

    def test_a_reader_never_sees_partial_json_while_the_holder_beats(self):
        self.fast_beats(0.001)
        lock = self.judge()
        reads = 0
        deadline = time.monotonic() + 60
        while lock.beat.count < 300:
            self.assertLess(time.monotonic(), deadline, "fewer than 300 beats in 60 s")
            self.assertEqual(json.loads(self.lock_file().read_bytes().decode("utf-8")), lock.content)
            reads += 1
        self.assertGreater(reads, 0)

    def test_another_machines_recent_beat_is_held_and_its_age_named(self):
        held = self.write_other_lock(600)
        message = self.assert_held(held)
        self.assertRegex(message, r"its holder last beat 60[0-9] s ago on the share's clock")
        self.assertIn("taken over once it has been silent for 3600 s", message)
        self.assertEqual(self.probes_left(), [])

    def test_another_machines_silent_lock_is_taken_over(self):
        self.write_other_lock(2 * 3600, started_hours_ago=3)
        warning = self.assert_taken()
        self.assertIn("taking over lock '{}'".format(self.lock_file()), warning)
        self.assertIn("host 'otherhost' pid 4242", warning)
        self.assertRegex(warning, r"its holder last beat 720[0-9] s ago on the share's clock, more than 3600 s")
        self.assertEqual(self.probes_left(), [])

    def test_a_beat_exactly_at_the_threshold_is_held_and_just_past_it_is_taken(self):
        for past_ns, held in ((0, True), (10 ** 6, False)):
            with self.subTest(past_ns=past_ns):
                content = self.write_other_lock(10)
                beat = os.stat(str(self.lock_file())).st_mtime_ns
                share_now = beat + HEARTBEAT_STALE_SECONDS * 10 ** 9 + past_ns
                with mock.patch("vidfunnel.record._share_clock_ns", return_value=share_now):
                    if held:
                        self.assertIn("last beat 3600 s ago", self.assert_held(content))
                    else:
                        self.assertIn("more than 3600 s", self.assert_taken())

    def test_record_and_folder_saves_refuse_once_the_lock_is_taken_and_not_before(self):
        from vidfunnel.record import LockLost, lock_problem, save_folder
        source = self.base / "clip.avi"
        save_record(source, {"id": "before"})  # no lock of this folder held here: nothing to lose
        lock = self.judge()
        lock.warn = lambda line: None
        save_record(source, {"id": "held"})
        self.assertEqual(load_record(source), {"id": "held"})
        record_bytes = record_path(source).read_bytes()
        os.unlink(str(self.lock_file()))
        write_bytes(self.lock_file(), json.dumps({"host": "hostb", "pid": 4242}).encode("utf-8"))
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            with self.assertRaises(LockLost) as caught:
                save_record(source, {"id": "after"})
            with self.assertRaises(LockLost):
                save_folder(self.base, {"sources": []})
        self.assertIn("the lock of folder '{}' was taken over by another run".format(self.base), str(caught.exception))
        self.assertEqual(record_path(source).read_bytes(), record_bytes)
        self.assertFalse((self.base / RECORD_DIR / FOLDER_FILE).exists())
        self.assertTrue(vidfunnel.interrupt.draining.is_set())
        self.assertEqual(len(lines), 1, lines)
        self.assertIsNotNone(lock_problem(self.base))

    def test_a_lock_that_cannot_be_checked_refuses_a_save_without_draining(self):
        from vidfunnel.record import LockLost
        source = self.base / "clip.avi"
        lock = self.judge()
        real_stat = os.stat

        def unreachable(path, *rest, **values):
            if os.fspath(path) == str(self.lock_file()):
                raise OSError(errno.EIO, "Input/output error")
            return real_stat(path, *rest, **values)

        with mock.patch("vidfunnel.record.os.stat", side_effect=unreachable):
            with self.assertRaises(LockLost) as caught:
                save_record(source, {"id": "unchecked"})
        self.assertIn("the lock of folder '{}' could not be checked".format(self.base), str(caught.exception))
        self.assertFalse(record_path(source).exists())
        self.assertFalse(vidfunnel.interrupt.draining.is_set())
        save_record(source, {"id": "checked"})
        lock.release()

    def test_a_beat_a_little_ahead_of_the_share_is_judged_by_it_and_further_ahead_by_the_24_hour_rule(self):
        ahead = vidfunnel.record.HEARTBEAT_AHEAD_SECONDS
        for seconds_ahead, by_beat in ((ahead - 1, True), (ahead + 1, False)):
            with self.subTest(seconds_ahead=seconds_ahead):
                content = self.write_other_lock(10)
                beat = os.stat(str(self.lock_file())).st_mtime_ns
                with mock.patch("vidfunnel.record._share_clock_ns", return_value=beat - seconds_ahead * 10 ** 9):
                    message = self.assert_held(content)
                if by_beat:
                    self.assertIn("its holder last beat 0 s ago on the share's clock", message)
                else:
                    self.assertIn("its last beat is {} s ahead of the share's clock, so the 24 h rule applies".format(
                        seconds_ahead), message)

    def test_a_dead_lock_stamped_in_the_shares_future_is_taken_over_at_24_hours(self):
        future = 400 * 24 * 3600
        held = self.write_other_lock(-future, started_hours_ago=23)
        message = self.assert_held(held)
        self.assertIn("ahead of the share's clock, so the 24 h rule applies", message)
        self.write_other_lock(-future, started_hours_ago=25)
        warning = self.assert_taken()
        self.assertIn("stale lock", warning)
        self.assertIn("ahead of the share's clock, so the 24 h rule decided", warning)
        self.assertIn("25.0 h ago on this machine's clock", warning)

    def test_the_share_clock_decides_whatever_this_machines_clock_says(self):
        for shift_hours in (5, -5):
            with self.subTest(shift_hours=shift_hours):
                clock = ShiftedClock(shift_hours * 3600)
                fresh = self.write_other_lock(60)
                with clock.patches()[0], clock.patches()[1]:
                    self.assertIn("last beat 6", self.assert_held(fresh))
                self.write_other_lock(2 * 3600)
                with clock.patches()[0], clock.patches()[1]:
                    self.assertIn("more than 3600 s", self.assert_taken())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root creates files in a read-only folder")
    def test_without_a_probe_file_the_24_hour_rule_applies_to_the_last_beat_and_says_so(self):
        held = self.write_other_lock(2 * 3600, started_hours_ago=30)
        records = self.base / RECORD_DIR
        os.chmod(str(records), 0o555)
        self.addCleanup(os.chmod, str(records), 0o755)
        message = self.assert_held(held)
        self.assertIn("no probe file can be created in '{}'".format(records), message)
        self.assertIn("the 24 h rule applies to its last beat", message)
        self.assertEqual(self.probes_left(), [])
        os.chmod(str(records), 0o755)
        self.write_other_lock(25 * 3600, started_hours_ago=30)
        with mock.patch("vidfunnel.record._share_clock_ns", side_effect=PermissionError(13, "Permission denied")):
            warning = self.assert_taken()
        self.assertIn("stale lock", warning)
        self.assertIn("so the 24 h rule decided on the younger of its start and its last beat, 25.0 h ago on this "
                      "machine's clock", warning)
        self.assertNotIn("30.0 h", warning)

    def test_the_probe_file_is_removed_when_the_judgement_raises(self):
        held = self.write_other_lock(600)
        with mock.patch("vidfunnel.record.os.fstat", side_effect=RuntimeError("simulated failure")):
            with self.assertRaises(RuntimeError):
                self.judge()
        self.assertEqual(self.probes_left(), [])
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), held)

    def test_a_live_pid_here_keeps_a_silent_lock_until_the_24_hour_rule(self):
        """Same machine, flock not testable: a running pid holds a silent lock, as before the heartbeat."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        for hours, held in ((2, True), (25, False)):
            with self.subTest(hours=hours):
                started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
                content = {"host": socket.gethostname(), "pid": child.pid, "container": False,
                           "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "machine": machine_id(),
                           "flock": False, "heartbeat": {"interval": HEARTBEAT_SECONDS}}
                write_bytes(self.lock_file(), json.dumps(content).encode("utf-8"))
                silent = time.time_ns() - hours * 3600 * 10 ** 9
                os.utime(str(self.lock_file()), ns=(silent, silent))
                if held:
                    message = self.assert_held(content)
                    self.assertIn("its pid {} is running here, although its holder last beat".format(child.pid),
                                  message)
                else:
                    warning = self.assert_taken()
                    self.assertIn("more than 3600 s; its pid is running here, taken for a reused one", warning)

    def test_a_holder_running_past_24_hours_with_a_recent_beat_is_held(self):
        held = self.write_other_lock(300, started_hours_ago=30)
        message = self.assert_held(held)
        self.assertIn("last beat 30", message)

    def test_a_beat_between_the_judgement_and_the_takeover_refuses_the_takeover(self):
        held = self.write_other_lock(2 * 3600)
        identity = os.stat(str(self.lock_file()))
        real_clock = vidfunnel.record._share_clock_ns

        def clock_then_beat(directory):
            share_now = real_clock(directory)
            handle = os.open(str(self.lock_file()), os.O_RDWR)
            try:
                os.pwrite(handle, self.lock_file().read_bytes()[:1], 0)
                os.fsync(handle)
            finally:
                os.close(handle)
            return share_now

        with mock.patch("vidfunnel.record._share_clock_ns", side_effect=clock_then_beat):
            message = self.assert_held(held)
        self.assertIn("its holder beat while the lock was being judged; not taken over", message)
        after = os.stat(str(self.lock_file()))
        self.assertEqual((after.st_dev, after.st_ino), (identity.st_dev, identity.st_ino))
        self.assertGreater(after.st_mtime_ns, identity.st_mtime_ns)

    def test_a_holder_whose_lock_is_taken_drains_says_so_and_leaves_the_new_lock(self):
        self.fast_beats()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            self.wait_until(lambda: lock.beat.count >= 2, 30, "the holder never beat")
            taken = self.replace_lock()
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "the holder did not drain")
            self.wait_until(lambda: lines, 30, "the lost lock was not reported")  # printed after the drain
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("lost the lock of folder '{}'".format(self.base), lines[0])
        self.assertIn("host 'winnerhost' holds it now", lines[0])
        self.assertEqual(vidfunnel.interrupt.drain_tally.lost_locks, [str(self.base)])
        beats = lock.beat.count
        with vidfunnel.record._held_locks_guard:
            self.assertNotIn(lock.beat, vidfunnel.record._beats)
        time.sleep(0.2)  # ten intervals: no further beat
        self.assertEqual(lock.beat.count, beats)
        warnings = []
        lock.warn = warnings.append
        lock.release()
        self.assertEqual(self.lock_file().read_bytes(), taken)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("was taken over by another run; left in place", warnings[0])

    def stepping_clock(self):
        """vidfunnel.record's 'time' with wall and monotonic offsets the test steps, for the rest of this test.

        Beats are made rare (an hour apart) and polls fast, so only the wake check can explain an early beat.
        """
        self.no_heartbeat_thread()  # a thread from an earlier lock would sleep through a whole default poll
        clock = types.SimpleNamespace(wall_offset=0.0, mono_offset=0.0, polls=0)
        clock.time = lambda: time.time() + clock.wall_offset
        clock.time_ns = lambda: time.time_ns() + int(clock.wall_offset * 10 ** 9)
        clock.monotonic = lambda: time.monotonic() + clock.mono_offset

        def counted_sleep(seconds):
            clock.polls += 1  # the loop has read both clocks for this round before it sleeps
            time.sleep(seconds)

        clock.sleep = counted_sleep
        for name, value in (("time", clock), ("HEARTBEAT_SECONDS", 3600), ("HEARTBEAT_POLL_SECONDS", 0.01)):
            patch = mock.patch.object(vidfunnel.record, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return clock

    WINNER = {"host": "winnerhost", "pid": 999, "started": "2026-11-09T12:00:00Z", "heartbeat": {"interval": 300}}

    def replace_lock(self):
        """Put another run's lock at the lock path in one rename, so no look at the path finds it missing.

        A takeover by this version renames its file over the lock path in the
        same way. An older vidfunnel unlinks the lock and then creates its own,
        so the path is missing for a moment; a beat polling during that moment
        says 'gone' and cannot name the new holder. Tests of that sequence
        unlink and write in two steps on purpose (test_a_holder_whose_lock_is_gone_...).
        """
        staged = self.base / RECORD_DIR / "replacement-lock.tmp"
        write_bytes(staged, json.dumps(self.WINNER).encode("utf-8"))
        os.replace(str(staged), str(self.lock_file()))
        return self.lock_file().read_bytes()

    def test_a_holder_whose_lock_is_gone_before_the_new_one_appears_drains_and_leaves_the_new_lock(self):
        """A takeover's unlink seen before its create: the run drains, writes nothing, and keeps off the new lock."""
        from vidfunnel.record import LockLost, lock_problem
        self.fast_beats()
        source = self.base / "clip.avi"
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            self.wait_until(lambda: lock.beat.count >= 2, 30, "the holder never beat")
            os.unlink(str(self.lock_file()))
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "the holder did not drain")
            self.wait_until(lambda: lines, 30, "the lost lock was not reported")
            write_bytes(self.lock_file(), json.dumps(self.WINNER).encode("utf-8"))
            taken = self.lock_file().read_bytes()
            self.assertIn("was taken over by another run", lock_problem(self.base))
            with self.assertRaises(LockLost):
                save_record(source, {"id": "after"})
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("lost the lock of folder '{}': its lock file is gone".format(self.base), lines[0])
        self.assertNotIn("holds it now", lines[0])
        self.assertEqual(vidfunnel.interrupt.drain_tally.lost_locks, [str(self.base)])
        self.assertFalse(record_path(source).exists())
        beats = lock.beat.count
        time.sleep(0.2)  # ten intervals: no further beat
        self.assertEqual(lock.beat.count, beats)
        warnings = []
        lock.warn = warnings.append
        lock.release()
        self.assertEqual(self.lock_file().read_bytes(), taken)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("was taken over by another run; left in place", warnings[0])

    def test_a_release_that_finds_the_lock_path_missing_leaves_the_lock_created_just_after(self):
        """No beat has looked yet; release's own look finds the path missing, and the new lock appears at once."""
        lock = self.judge()  # the default interval: no beat during this test
        os.unlink(str(self.lock_file()))
        real_stat = os.stat
        created = []

        def stat_then_create(path, *rest, **values):
            try:
                return real_stat(path, *rest, **values)
            finally:
                if os.fspath(path) == str(self.lock_file()) and not created:
                    created.append(write_bytes(self.lock_file(), json.dumps(self.WINNER).encode("utf-8")))

        warnings = []
        lock.warn = warnings.append
        with mock.patch("vidfunnel.record.os.stat", side_effect=stat_then_create):
            lock.release()
        self.assertEqual(len(created), 1, "release never looked at the lock path")
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), self.WINNER)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("no longer holds this run's entry; left in place", warnings[0])

    def test_after_a_suspend_a_taken_lock_is_found_at_the_next_poll(self):
        clock = self.stepping_clock()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            taken = self.replace_lock()
            self.wait_for_polls(clock, 20)  # twenty polls without a wake: the hour-long interval keeps it unchecked
            self.assertFalse(vidfunnel.interrupt.draining.is_set())
            self.assertEqual(lines, [])
            clock.wall_offset += 3600  # the wall clock counted an hour of sleep, the monotonic clock did not
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "the wake did not check the lock")
            self.wait_until(lambda: lines, 30, "the lost lock was not reported")  # printed after the drain
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("lost the lock of folder '{}'".format(self.base), lines[0])
        self.assertIn("host 'winnerhost' holds it now", lines[0])
        lock.warn = lambda line: None
        lock.release()
        self.assertEqual(self.lock_file().read_bytes(), taken)

    def test_after_a_suspend_an_intact_lock_beats_once_at_once(self):
        clock = self.stepping_clock()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            os.utime(str(self.lock_file()), ns=(10 ** 9, 10 ** 9))
            self.wait_for_polls(clock, 1)  # the loop has read the clocks before the step, so it sees the step
            clock.wall_offset += 3600
            self.wait_until(lambda: lock.beat.count >= 1, 30, "the wake did not beat the lock")
            self.wait_for_polls(clock, 20)
        self.assertGreater(os.stat(str(self.lock_file())).st_mtime_ns, 10 ** 9)
        self.assertEqual(lock.beat.count, 1)
        self.assertEqual(lines, [])
        self.assertFalse(vidfunnel.interrupt.draining.is_set())

    def test_a_small_forward_step_or_a_backward_step_of_the_wall_clock_beats_nothing(self):
        clock = self.stepping_clock()
        lock = self.judge()
        os.utime(str(self.lock_file()), ns=(10 ** 9, 10 ** 9))
        for step in (vidfunnel.record.HEARTBEAT_WAKE_GAP_SECONDS - 5, -3600):
            with self.subTest(step=step):
                clock.wall_offset += step
                self.wait_for_polls(clock, 20)
                self.assertEqual(os.stat(str(self.lock_file())).st_mtime_ns, 10 ** 9)
                self.assertEqual(lock.beat.count, 0)

    def test_a_forward_step_just_over_the_wake_gap_beats_at_once(self):
        clock = self.stepping_clock()
        lock = self.judge()
        os.utime(str(self.lock_file()), ns=(10 ** 9, 10 ** 9))
        self.wait_for_polls(clock, 1)  # the loop has read the clocks before the step, so it sees the step
        clock.wall_offset += vidfunnel.record.HEARTBEAT_WAKE_GAP_SECONDS + 1
        self.wait_until(lambda: lock.beat.count == 1, 30, "a step just over the wake gap did not beat")
        self.assertGreater(os.stat(str(self.lock_file())).st_mtime_ns, 10 ** 9)

    def test_a_thread_stopped_long_enough_for_a_takeover_checks_its_lock_at_once(self):
        """SIGSTOP: both clocks count the stop, so the hour-long interval itself makes the lock due."""
        clock = self.stepping_clock()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            lock.warn = lambda line: None
            self.replace_lock()
            clock.mono_offset += HEARTBEAT_STALE_SECONDS + 60
            clock.wall_offset += HEARTBEAT_STALE_SECONDS + 60
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "the late poll did not check the lock")
            self.wait_until(lambda: lines, 30, "the lost lock was not reported")  # printed after the drain
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("lost the lock of folder '{}'".format(self.base), lines[0])

    def wait_for_polls(self, clock, polls):
        """Wait until the heartbeat loop of a stepping_clock() has slept `polls` more times."""
        target = clock.polls + polls
        self.wait_until(lambda: clock.polls >= target, 30, "the heartbeat loop did not poll {} times".format(polls))

    def no_heartbeat_thread(self):
        self.wait_until(lambda: vidfunnel.record._beat_thread is None and not heartbeat_threads(), 30,
                        "a heartbeat thread outlived an earlier test")

    def failing_writes(self, failures):
        """os.pwrite that raises EIO for its first `failures` calls, then writes."""
        real_pwrite = os.pwrite
        calls = []

        def pwrite(handle, data, offset):
            calls.append(handle)
            if len(calls) <= failures:
                raise OSError(errno.EIO, "Input/output error")
            return real_pwrite(handle, data, offset)

        patch = mock.patch("vidfunnel.record.os.pwrite", side_effect=pwrite)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_a_beat_survives_a_stderr_that_cannot_be_written(self):
        class GoneReader:
            def write(self, text):
                raise OSError(errno.EIO, "Input/output error")

            def flush(self):
                raise OSError(errno.EIO, "Input/output error")

        for stderr in (GoneReader(), None):
            with self.subTest(stderr=stderr):
                self.fast_beats()
                calls = self.failing_writes(3)
                with mock.patch.object(sys, "stderr", stderr):
                    lock = self.judge()
                    self.wait_until(lambda: lock.beat.count >= 3, 30, "beats stopped after a failed warning")
                    self.assertGreaterEqual(len(calls), 6)
                    self.assertTrue(heartbeat_threads())
                    lock.release()

    def test_a_lost_lock_drains_when_stderr_cannot_be_written(self):
        for stderr in (types.SimpleNamespace(write=mock.Mock(side_effect=OSError(errno.EPIPE, "Broken pipe")),
                                             flush=mock.Mock()), None):
            with self.subTest(stderr=stderr):
                vidfunnel.interrupt.draining.clear()
                vidfunnel.interrupt.drain_tally.reset()
                self.fast_beats()
                with mock.patch.object(sys, "stderr", stderr):
                    lock = self.judge()
                    lock.warn = lambda line: None
                    self.replace_lock()
                    self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "the lost lock did not drain")
                    lock.release()
                self.lock_file().unlink()

    def test_a_lost_lock_drains_when_its_warning_fails(self):
        self.fast_beats()
        with mock.patch("vidfunnel.record._warn", side_effect=RuntimeError("the warning failed")):
            lock = self.judge()
            lock.warn = lambda line: None
            self.replace_lock()
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "a failed warning kept the run from draining")
        self.assertEqual(vidfunnel.interrupt.drain_tally.lost_locks, [str(self.base)])

    def test_a_replacing_lock_nested_too_deep_still_drains_and_says_so(self):
        self.fast_beats()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            lock.warn = lambda line: None
            os.unlink(str(self.lock_file()))
            write_bytes(self.lock_file(), b"[" * 200000 + b"]" * 200000)
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "the lost lock did not drain")
            self.wait_until(lambda: lines, 30, "the lost lock was not reported")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("lost the lock of folder '{}'".format(self.base), lines[0])
        self.assertNotIn("holds it now", lines[0])

    def test_an_exception_in_a_beat_is_reported_once_and_the_beats_go_on(self):
        self.fast_beats()
        real_state = vidfunnel.record._lock_state
        calls = []

        def broken_then_real(beat):
            calls.append(beat)
            if len(calls) <= 3:
                raise ValueError("an unexpected failure")
            return real_state(beat)

        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append), \
                mock.patch("vidfunnel.record._lock_state", side_effect=broken_then_real):
            lock = self.judge()
            self.wait_until(lambda: lock.beat.count >= 3, 30, "the beats did not go on after the exception")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("the heartbeat failed (ValueError: an unexpected failure)", lines[0])

    def test_after_the_thread_dies_the_next_lock_starts_a_new_one(self):
        self.no_heartbeat_thread()
        self.fast_beats()
        with mock.patch("vidfunnel.record._beat_until_none_left", side_effect=RuntimeError("the thread died")), \
                mock.patch.object(threading, "excepthook", lambda arguments: None):
            first = FolderLock(self.base / "first", warn=self.fail).acquire()
            self.addCleanup(first.release)
            self.wait_until(lambda: not heartbeat_threads(), 30, "the thread did not die")
        self.assertIsNone(vidfunnel.record._beat_thread)
        self.assertEqual(first.beat.count, 0)
        second = FolderLock(self.base / "second", warn=self.fail).acquire()
        self.addCleanup(second.release)
        self.wait_until(lambda: first.beat.count >= 2 and second.beat.count >= 2, 30,
                        "no new thread beats the locks")

    def test_a_thread_that_cannot_start_refuses_the_lock_and_leaves_no_file(self):
        class RefusedThread(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")

        self.no_heartbeat_thread()
        refusing = types.SimpleNamespace(Thread=RefusedThread, current_thread=threading.current_thread)
        with mock.patch.object(vidfunnel.record, "threading", refusing):
            with self.assertRaises(LockHeld) as caught:
                FolderLock(self.base, warn=self.fail).acquire()
        self.assertIn("cannot start the heartbeat thread its lock needs (can't start new thread)",
                      str(caught.exception))
        self.assertFalse(self.lock_file().exists())
        with held_lock_fds() as descriptors:
            self.assertEqual(descriptors, ())
        with vidfunnel.record._held_locks_guard:
            self.assertEqual(vidfunnel.record._beats, [])
        lock = self.judge()
        self.assertTrue(heartbeat_threads())
        lock.release()

    def test_beats_come_no_more_often_than_the_interval(self):
        """Each beat falls due an interval after the one before, and none is written before it is due.

        Read inside the write, on the heartbeat thread, beat.due is when the
        next beat falls due (this round's clock reading plus the interval).
        A slow machine can only spread the beats further apart; it cannot
        make the test fail.
        """
        interval = 0.05
        self.fast_beats(interval)
        observed = []  # (when a beat was written, when the next one falls due), both on the monotonic clock
        with mock.patch.object(vidfunnel.record, "HEARTBEAT_POLL_SECONDS", 0.002):
            real_pwrite = os.pwrite

            def recording(handle, data, offset):
                with vidfunnel.record._held_locks_guard:
                    due = [beat.due for beat in vidfunnel.record._beats if beat.handle == handle]
                observed.append((time.monotonic(), due[0] if due else None))
                return real_pwrite(handle, data, offset)

            with mock.patch("vidfunnel.record.os.pwrite", side_effect=recording):
                lock = self.judge()
                self.wait_until(lambda: len(observed) >= 6, 30, "the holder hardly beat")
                lock.release()
        beats = observed[:6]
        self.assertNotIn(None, [due for _written, due in beats], beats)
        for (written, due), (next_written, next_due) in zip(beats, beats[1:]):
            self.assertGreaterEqual(next_due - due, interval, "a beat fell due less than an interval after the one "
                                    "before: {}".format(beats))
            self.assertGreaterEqual(next_written, due, "a beat was written before it was due: {}".format(beats))

    def test_release_during_a_beat_leaves_the_descriptor_to_the_beat(self):
        self.fast_beats()
        in_beat = threading.Event()
        go_on = threading.Event()
        real_pwrite = os.pwrite

        finished = threading.Event()

        def held_up(handle, data, offset):
            in_beat.set()
            go_on.wait(60)
            try:
                return real_pwrite(handle, data, offset)
            finally:
                finished.set()

        with mock.patch("vidfunnel.record.os.pwrite", side_effect=held_up):
            lock = self.judge()
            handle = lock.handle
            self.assertTrue(in_beat.wait(30), "no beat began")
            lock.release()
            self.assertFalse(finished.is_set(), "release waited for the beat in flight")
            self.assertFalse(self.lock_file().exists())
            os.fstat(handle)  # still open: the beat in flight uses it
            go_on.set()
            self.wait_until(lambda: self.descriptor_closed(handle), 30, "the beat did not close the descriptor")

    @staticmethod
    def descriptor_closed(handle):
        try:
            os.fstat(handle)
        except OSError:
            return True
        return False

    def test_a_beat_seeing_our_own_release_does_not_drain(self):
        lock = self.judge()
        beat = lock.beat
        lock.release()
        vidfunnel.record._lose_lock(beat, "its lock file is gone")
        self.assertFalse(vidfunnel.interrupt.draining.is_set())
        self.assertFalse(beat.lost)

    def test_a_released_or_lost_lock_is_neither_checked_nor_beaten(self):
        lock = self.judge()
        with mock.patch("vidfunnel.record._lock_state", side_effect=AssertionError("checked")) as checked, \
                mock.patch("vidfunnel.record.os.pwrite", side_effect=AssertionError("written")) as written:
            lock.beat.lost = True
            vidfunnel.record._beat_once(lock.beat)
            lock.beat.lost = False
            beat = lock.beat
            lock.release()
            vidfunnel.record._beat_once(beat)
        checked.assert_not_called()
        written.assert_not_called()

    def test_every_beat_is_fsynced(self):
        self.fast_beats()
        real_fsync = os.fsync
        synced = []

        def counting(handle):
            synced.append(handle)
            return real_fsync(handle)

        with mock.patch("vidfunnel.record.os.fsync", side_effect=counting):
            lock = self.judge()
            self.wait_until(lambda: lock.beat.count >= 3, 30, "the holder never beat")
            handle = lock.handle
            count = lock.beat.count
            lock.release()
        self.assertGreaterEqual(synced.count(handle), count)

    def test_a_stale_descriptor_means_the_lock_was_taken(self):
        self.fast_beats()
        lines = []
        real_fstat = os.fstat
        lock = self.judge()
        lock.warn = lambda line: None
        handle = lock.handle

        def stale(descriptor):
            if descriptor == handle:
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_fstat(descriptor)

        with mock.patch("vidfunnel.record._warn", side_effect=lines.append), \
                mock.patch("vidfunnel.record.os.fstat", side_effect=stale):
            self.wait_until(vidfunnel.interrupt.draining.is_set, 30, "a stale descriptor did not drain")
            self.wait_until(lambda: lines, 30, "the lost lock was not reported")  # printed after the drain
        self.assertIn("its lock file was removed on the server", lines[0])

    def test_a_record_folder_that_is_gone_is_a_failed_beat_not_a_lost_lock(self):
        self.fast_beats()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            lock = self.judge()
            records = self.base / RECORD_DIR
            away = self.base / "away"
            os.rename(str(records), str(away))
            self.wait_until(lambda: lines, 30, "the failed beat was not reported")
            beats = lock.beat.count
            time.sleep(0.1)  # five intervals
            self.assertFalse(vidfunnel.interrupt.draining.is_set())
            os.rename(str(away), str(records))
            self.wait_until(lambda: lock.beat.count > beats, 30, "the beats did not resume")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("cannot reach '{}'".format(records), lines[0])

    def test_one_thread_beats_every_lock_and_ends_after_the_last_release(self):
        self.no_heartbeat_thread()
        asleep = threading.Event()
        wake = threading.Event()

        def held_sleep(seconds):  # the thread's poll lasts until the test lets it go on
            asleep.set()
            wake.wait(60)

        held = types.SimpleNamespace(time=time.time, time_ns=time.time_ns, monotonic=time.monotonic, sleep=held_sleep)
        with mock.patch.object(vidfunnel.record, "time", held):
            first = FolderLock(self.base / "first", warn=self.fail).acquire()
            self.addCleanup(first.release)
            self.assertEqual(len(heartbeat_threads()), 1)
            second = FolderLock(self.base / "second", warn=self.fail).acquire()
            self.addCleanup(second.release)
            self.assertEqual(len(heartbeat_threads()), 1)
            self.assertTrue(heartbeat_threads()[0].daemon)
            self.assertTrue(asleep.wait(30), "the heartbeat thread never polled")
            first.release()
            second.release()
            self.assertTrue(heartbeat_threads(), "release waited for the heartbeat thread's poll to end")
            wake.set()
            self.wait_until(lambda: not heartbeat_threads(), 30, "the heartbeat thread outlived the last lock")

    def test_a_failed_beat_is_reported_once_and_the_thread_carries_on(self):
        self.fast_beats()
        lines = []
        with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
            failing = mock.patch("vidfunnel.record.os.pwrite", side_effect=OSError(errno.EIO, "Input/output error"))
            refused = failing.start()
            lock = self.judge()
            self.wait_until(lambda: refused.call_count >= 5, 30, "the beat was not retried")
            failing.stop()
            self.wait_until(lambda: lock.beat.count >= 2, 30, "the thread did not beat again after the failures")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("lock '{}': cannot write the heartbeat".format(self.lock_file()), lines[0])
        self.assertIn("Input/output error", lines[0])
        self.assertFalse(vidfunnel.interrupt.draining.is_set())

    @unittest.skipIf(sys.platform == "win32", "flock is POSIX")
    def test_the_descriptor_children_inherit_is_unchanged_by_the_beats(self):
        from vidfunnel.ffmpeg import run
        self.fast_beats()
        lock = self.judge()
        inode = os.fstat(lock.handle).st_ino
        self.wait_until(lambda: lock.beat.count >= 5, 30, "the holder never beat")
        with held_lock_fds() as descriptors:
            self.assertIn(lock.handle, descriptors)
        child = ("import fcntl, os, sys\n"
                 "print(os.fstat(int(sys.argv[1])).st_ino)\n"
                 "test = os.open(sys.argv[2], os.O_RDONLY)\n"
                 "try:\n"
                 "    fcntl.flock(test, fcntl.LOCK_SH | fcntl.LOCK_NB)\n"
                 "    print('free')\n"
                 "except BlockingIOError:\n"
                 "    print('held')\n")
        completed = run([sys.executable, "-c", child, str(lock.handle), str(self.lock_file())], check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.split(), [str(inode), "held"])
        self.assertEqual(os.stat(str(self.lock_file())).st_ino, inode)


# The steps of a takeover at which vidfunnel.record._takeover_step is called, in order (FolderLock._replace).
TAKEOVER_STEPS = ("marked", "rechecked", "created", "written", "checked", "renamed", "published", "unmarking")

# Why a taker that loses a race may be refused; any other LockHeld in a race is a finding. A taker that judges
# only after the winner has finished finds a live lock that beat just now.
RACE_REFUSALS = ("a takeover is in progress", "got it first", "this run itself", "its holder last beat")


def another_machine_patches(host=None, machine="taker"):
    """Patches under which this process takes locks as a run on another machine of the share would.

    Neither the takeover flock nor the lock's own flock reaches across
    machines: the guard does nothing and the boot id and machine are not
    those of the dead lock. Takers given different `machine` names are
    different machines to each other too.
    """
    patches = [mock.patch.object(FolderLock, "_takeover_guard", lambda self: contextlib.nullcontext()),
               mock.patch("vidfunnel.record.boot_id", return_value="{}-boot".format(machine)),
               mock.patch("vidfunnel.record.machine_identity",
                          return_value=("{}-machine".format(machine), "machine-id")),
               mock.patch("vidfunnel.record.in_container", return_value=False)]
    if host is not None:
        patches.append(mock.patch("vidfunnel.record.socket.gethostname", return_value=host))
    return patches


def takeover_race_worker(name, folder, slow_steps, start, finish, released, results):
    """One taker of TakeoverTests.test_two_taker_processes_on_one_dead_lock_get_it_once, as machine `name`.

    For each round: wait at `start`, try the lock (slowed at slow_steps[round],
    if any), report, wait at `finish`, release a lock it won, wait at `released`.
    """
    import time as clock
    from vidfunnel.record import FolderLock as Lock, LockHeld as Held
    current = {"step": None}

    def pause(step):
        if step == current["step"]:
            clock.sleep(0.002)

    for patch in (another_machine_patches(host="{}host".format(name), machine=name)
                  + [mock.patch("vidfunnel.record._takeover_step", side_effect=pause)]):
        patch.start()
    for slow_step in slow_steps:
        current["step"] = slow_step
        start.wait(120)
        lock = None
        try:
            lock = Lock(folder, warn=lambda line: None).acquire()
            results.put(("ACQUIRED", os.getpid(), ""))
        except Held as held:
            results.put(("HELD", os.getpid(), str(held)))
        except BaseException as error:
            results.put(("ERROR", os.getpid(), repr(error)))
        finish.wait(120)
        if lock is not None:
            lock.release()
        released.wait(120)


# A taker in a process of its own, as a run on another machine. argv: folder, the step to slow down ("none" for
# none). It prints ACQUIRED once it holds the lock, then holds it until its stdin closes and releases it.
TAKER_SCRIPT = """
import contextlib, os, sys, time, uuid
from pathlib import Path
from unittest import mock
from vidfunnel.record import FolderLock
folder, slow_step = sys.argv[1], sys.argv[2]
lock_path = str(Path(folder) / ".vidfunnel" / "lock")
real = {name: getattr(os, name) for name in ("unlink", "remove", "rename", "replace")}
taking = [True]

def pause(step):
    if step == slow_step:
        time.sleep(0.2)

def let_the_holder_look():
    # A file 'look-<hex>' in the folder asks the holder's poll to look at its lock now; it removes the file once
    # it has. Until then this taker waits, so the holder looks at the state the lock path is in at this moment.
    flag = os.path.join(folder, "look-" + uuid.uuid4().hex)
    open(flag, "w").close()
    deadline = time.monotonic() + 60
    while os.path.exists(flag) and time.monotonic() < deadline:
        time.sleep(0.001)

def removing(name):
    def hooked(path, *rest, **options):
        real[name](path, *rest, **options)
        if taking[0] and os.fspath(path) == lock_path:  # a takeover never does this: the path would be empty
            let_the_holder_look()
    return hooked

def moving(name):
    def hooked(source, target, *rest, **options):
        if taking[0] and os.fspath(target) == lock_path:  # whatever came before the rename must not empty the path
            let_the_holder_look()
        real[name](source, target, *rest, **options)
        if taking[0] and os.fspath(source) == lock_path:  # moved away: the path would be empty
            let_the_holder_look()
    return hooked

for patch in (mock.patch.object(FolderLock, "_takeover_guard", lambda self: contextlib.nullcontext()),
              mock.patch("vidfunnel.record.boot_id", return_value="taker-boot"),
              mock.patch("vidfunnel.record.machine_identity", return_value=("taker-machine", "machine-id")),
              mock.patch("vidfunnel.record.in_container", return_value=False),
              mock.patch("vidfunnel.record.socket.gethostname", return_value="takerhost"),
              mock.patch("vidfunnel.record._takeover_step", side_effect=pause),
              mock.patch("vidfunnel.record.os.unlink", side_effect=removing("unlink")),
              mock.patch("vidfunnel.record.os.remove", side_effect=removing("remove")),
              mock.patch("vidfunnel.record.os.rename", side_effect=moving("rename")),
              mock.patch("vidfunnel.record.os.replace", side_effect=moving("replace"))):
    patch.start()
lock = FolderLock(folder, warn=lambda line: None).acquire()
taking[0] = False
print("ACQUIRED", flush=True)
sys.stdin.read()
lock.release()
"""

# A holder that beats every 0.05 s. argv: folder. It prints ACQUIRED once it holds the lock.
FAST_HOLDER_SCRIPT = """
import sys, time
from unittest import mock
import vidfunnel.record
mock.patch.object(vidfunnel.record, "HEARTBEAT_SECONDS", 0.05).start()
mock.patch.object(vidfunnel.record, "HEARTBEAT_POLL_SECONDS", 0.01).start()
with vidfunnel.record.FolderLock(sys.argv[1], warn=lambda line: None):
    print("ACQUIRED", flush=True)
    time.sleep(60)
"""

# A taker on this machine that stops once it has written its takeover marker, until it is killed. argv: folder.
MARKED_TAKER_SCRIPT = """
import sys
from unittest import mock
from vidfunnel.record import FolderLock

def stop_once_marked(step):
    if step == "marked":
        print("MARKED", flush=True)
        sys.stdin.read()

mock.patch("vidfunnel.record._takeover_step", side_effect=stop_once_marked).start()
FolderLock(sys.argv[1], warn=lambda line: None).acquire()
"""


class TakeoverTests(unittest.TestCase):
    """A lock judged dead is replaced under the marker 'lock.taking' by a rename (FolderLock._replace)."""

    def setUp(self):
        # The folder goes last, after the cleanups that release locks in it (tearDown runs before them).
        self.holder = tempfile.TemporaryDirectory(prefix="vidfunnel-takeover-test-")
        self.base = Path(self.holder.name)
        self.addCleanup(self.holder.cleanup)
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        (self.base / RECORD_DIR).mkdir()

    def lock_file(self):
        return self.base / RECORD_DIR / LOCK_NAME

    def marker_file(self):
        return self.base / RECORD_DIR / TAKEOVER_MARKER

    def names(self):
        return sorted(os.listdir(str(self.base / RECORD_DIR)))

    def wait_until(self, predicate, seconds, message):
        deadline = time.monotonic() + seconds
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.002)

    def apply(self, patches):
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write_dead_lock(self, number=0, beat_seconds_ago=2 * 3600, started_hours_ago=3.0):
        """A heartbeat lock of another machine whose beat is silent, put at the lock path in one rename."""
        started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=started_hours_ago)
        content = {"host": "deadhost", "pid": 4242 + number, "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "container": False, "machine": "dead-machine", "machine_source": "machine-id",
                   "boot": "dead-boot", "flock": True, "device": 1, "heartbeat": {"interval": HEARTBEAT_SECONDS}}
        staged = self.base / RECORD_DIR / "dead-staged"
        write_bytes(staged, json.dumps(content).encode("utf-8"))
        beat = time.time_ns() - int(beat_seconds_ago * 10 ** 9)
        os.utime(str(staged), ns=(beat, beat))
        os.replace(str(staged), str(self.lock_file()))
        return content

    def write_marker(self, seconds_ago, host="markerhost", pid=5150, **fields):
        content = {"host": host, "pid": pid, "token": uuid.uuid4().hex, "started": "2026-11-10T00:00:00Z"}
        content.update(fields)
        write_bytes(self.marker_file(), json.dumps(content).encode("utf-8"))
        stamp = time.time_ns() - int(seconds_ago * 10 ** 9)
        os.utime(str(self.marker_file()), ns=(stamp, stamp))
        return content

    def snapshot(self, path):
        status = os.stat(str(path))
        return status.st_dev, status.st_ino, Path(path).read_bytes()

    def take(self, warnings=None, warn=None):
        """Acquire the lock or raise LockHeld; released at cleanup, also when acquire raised after taking it."""
        if warn is None:
            warn = warnings.append if warnings is not None else lambda line: None
        lock = FolderLock(self.base, warn=warn)
        self.addCleanup(lock.release)
        return lock.acquire()

    def assert_ours(self, lock):
        there = os.stat(str(self.lock_file()))
        ours = os.fstat(lock.handle)
        self.assertEqual((there.st_dev, there.st_ino), (ours.st_dev, ours.st_ino))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), lock.content)

    def assert_nothing_of_ours(self):
        with held_lock_fds() as descriptors:
            self.assertEqual(descriptors, ())
        with vidfunnel.record._held_locks_guard:
            self.assertNotIn(os.path.abspath(str(self.base)), vidfunnel.record._folder_beats)
            self.assertEqual([beat for beat in vidfunnel.record._beats if beat.path == str(self.lock_file())], [])

    # ------------------------------------------------------------ two takers

    def race_threads(self, rounds, slow_step=None):
        """Two threads take over one dead lock per round; one is slowed at `slow_step`; return the outcomes."""
        def pause(step):
            if step == slow_step and threading.current_thread().name == "slow-taker":
                time.sleep(0.002)

        with contextlib.ExitStack() as stack:
            for patch in another_machine_patches() + [mock.patch("vidfunnel.record._takeover_step", side_effect=pause)]:
                stack.enter_context(patch)
            self.race_rounds(rounds, slow_step)

    def race_rounds(self, rounds, slow_step):
        real_clock = vidfunnel.record._share_clock_ns
        for round_number in range(rounds):
            self.write_dead_lock(round_number)
            judged = threading.Barrier(2, timeout=60)
            waited = set()

            def clock_after_both_judged(directory):
                # Both takers have read the dead lock before either goes on, so they meet at the marker.
                now = real_clock(directory)
                if threading.current_thread().name not in waited:
                    waited.add(threading.current_thread().name)
                    judged.wait()
                return now

            outcomes = {}

            def taker():
                try:
                    outcomes[threading.current_thread().name] = ("ACQUIRED",
                                                                 FolderLock(self.base, warn=lambda line: None).acquire())
                except LockHeld as held:
                    outcomes[threading.current_thread().name] = ("HELD", str(held))
                except BaseException as error:
                    outcomes[threading.current_thread().name] = ("ERROR", repr(error))

            with mock.patch("vidfunnel.record._share_clock_ns", side_effect=clock_after_both_judged):
                threads = [threading.Thread(target=taker, name=name) for name in ("slow-taker", "plain-taker")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(120)
            for kind, value in outcomes.values():
                if kind == "ACQUIRED":
                    self.addCleanup(value.release)  # a failed round leaves no lock to later tests
            kinds = sorted(kind for kind, _value in outcomes.values())
            self.assertEqual(kinds, ["ACQUIRED", "HELD"], (round_number, slow_step, outcomes))
            winner = [value for kind, value in outcomes.values() if kind == "ACQUIRED"][0]
            loser = [value for kind, value in outcomes.values() if kind == "HELD"][0]
            self.assertTrue(any(reason in loser for reason in RACE_REFUSALS), loser)
            self.assert_ours(winner)
            self.assertEqual(self.names(), [LOCK_NAME], (round_number, slow_step))
            winner.release()
            self.assertEqual(self.names(), [])

    def test_two_taker_threads_on_one_dead_lock_get_it_once(self):
        self.race_threads(300)

    def test_two_taker_threads_get_it_once_with_one_slowed_at_each_step(self):
        for step in TAKEOVER_STEPS:
            with self.subTest(step=step):
                self.race_threads(25, slow_step=step)

    def test_two_taker_processes_on_one_dead_lock_get_it_once(self):
        """Two processes as two machines (each its own boot id and machine); one is slowed at each step in turn."""
        context = multiprocessing.get_context("spawn")
        start, finish, released = (context.Barrier(3) for _number in range(3))
        results = context.Queue()
        slow = [None] * 200 + [step for step in TAKEOVER_STEPS for _number in range(25)]
        workers = [context.Process(target=takeover_race_worker,
                                   args=(name, str(self.base), steps, start, finish, released, results))
                   for name, steps in (("slowtaker", slow), ("plaintaker", [None] * len(slow)))]
        for worker in workers:
            worker.start()

        def stop_workers():
            # After the last round the workers leave on their own; a failed round leaves them at a barrier.
            for worker in workers:
                worker.join(30)
            if any(worker.is_alive() for worker in workers):
                for barrier in (start, finish, released):
                    barrier.abort()
            for worker in workers:
                worker.join(10)
                if worker.is_alive():
                    worker.kill()
                    worker.join(10)

        self.addCleanup(stop_workers)
        for round_number, slow_step in enumerate(slow):
            self.write_dead_lock(round_number)
            start.wait(120)
            outcomes = [results.get(timeout=120) for _number in range(2)]
            self.assertEqual(sorted(kind for kind, _pid, _words in outcomes), ["ACQUIRED", "HELD"],
                             (round_number, slow_step, outcomes))
            winner = [pid for kind, pid, _words in outcomes if kind == "ACQUIRED"][0]
            loser = [words for kind, _pid, words in outcomes if kind == "HELD"][0]
            self.assertTrue(any(reason in loser for reason in RACE_REFUSALS), loser)
            self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8"))["pid"], winner)
            self.assertEqual(self.names(), [LOCK_NAME], (round_number, slow_step))
            finish.wait(120)
            released.wait(120)
            self.assertEqual(self.names(), [], (round_number, slow_step))

    # ------------------------------------------------------------ the lock path is never empty

    def test_an_observer_never_finds_the_lock_path_missing_or_incomplete(self):
        looks = [0]
        problems = []
        stop = threading.Event()

        def observe():
            while not stop.is_set():
                try:
                    with open(str(self.lock_file()), "rb") as stream:
                        data = stream.read()
                except FileNotFoundError:
                    problems.append("missing")
                    continue
                try:
                    json.loads(data.decode("utf-8"))
                except ValueError:
                    problems.append(data)
                looks[0] += 1

        def slowed(step):
            time.sleep(0.001)
            if step in ("checked", "renamed"):  # the observer looks just before and just after the rename
                seen = looks[0]
                self.wait_until(lambda: looks[0] > seen + 1, 30, "the observer stopped looking")

        real = {name: getattr(os, name) for name in ("unlink", "remove", "rename", "replace")}
        taking = threading.Event()  # set while the taker acquires: the test's own renames and releases are not hooked

        def let_the_observer_look():
            seen = looks[0] + len(problems)
            self.wait_until(lambda: looks[0] + len(problems) > seen + 1, 30, "the observer stopped looking")

        def removing(name):
            def hooked(path, *rest, **options):
                real[name](path, *rest, **options)
                if taking.is_set() and os.fspath(path) == str(self.lock_file()):  # the path would be empty now
                    let_the_observer_look()
            return hooked

        def moving(name):
            def hooked(source, target, *rest, **options):
                if taking.is_set() and os.fspath(target) == str(self.lock_file()):
                    let_the_observer_look()  # whatever came before the rename must not have emptied the path
                real[name](source, target, *rest, **options)
                if taking.is_set() and os.fspath(source) == str(self.lock_file()):  # moved away: the path is empty
                    let_the_observer_look()
            return hooked

        self.apply(another_machine_patches() + [mock.patch("vidfunnel.record._takeover_step", side_effect=slowed)]
                   + [mock.patch("vidfunnel.record.os." + name, side_effect=removing(name)) for name in ("unlink", "remove")]
                   + [mock.patch("vidfunnel.record.os." + name, side_effect=moving(name)) for name in ("rename", "replace")])
        self.write_dead_lock()
        observer = threading.Thread(target=observe)
        observer.start()
        try:
            previous = None
            for round_number in range(200):
                if previous is not None:
                    self.write_dead_lock(round_number)  # one rename over our lock: the path stays named
                    previous.release()  # finds the path names another file and leaves it
                previous = FolderLock(self.base, warn=lambda line: None)
                self.addCleanup(previous.release)
                taking.set()
                try:
                    previous.acquire()
                finally:
                    taking.clear()
                self.assert_ours(previous)
        finally:
            stop.set()
            observer.join(30)
        previous.release()  # after the observer has stopped: release empties the path
        self.assertEqual(problems, [])
        self.assertGreater(looks[0], 400)

    def test_a_replaced_holder_names_the_new_host_whatever_step_the_taker_is_slow_at(self):
        from vidfunnel.record import lock_problem
        script = self.base / "taker.py"
        script.write_text(TAKER_SCRIPT, encoding="utf-8")
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        for step in ("none",) + TAKEOVER_STEPS:
            with self.subTest(step=step):
                vidfunnel.interrupt.draining.clear()
                vidfunnel.interrupt.drain_tally.reset()
                lock = FolderLock(self.base, warn=lambda line: None)
                self.addCleanup(lock.release)
                lock.acquire()
                silent = time.time_ns() - 2 * 3600 * 10 ** 9
                os.utime(str(self.lock_file()), ns=(silent, silent))  # the holder froze two hours ago
                states = []
                stop = threading.Event()

                def poll(beat=lock.beat):
                    def look():
                        state = vidfunnel.record._lock_state(beat)
                        if not states or states[-1] != state:  # each change, not each look
                            states.append(state)

                    while not stop.is_set():
                        look()
                        for name in os.listdir(str(self.base)):
                            if name.startswith("look-"):  # the taker waits until this look is done
                                look()
                                os.unlink(str(self.base / name))

                poller = threading.Thread(target=poll)
                poller.start()
                taker = subprocess.Popen([sys.executable, str(script), str(self.base), step], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, universal_newlines=True, env=environment)
                self.addCleanup(taker.wait)
                self.addCleanup(taker.kill)
                try:
                    self.assertEqual(taker.stdout.readline(), "ACQUIRED\n")
                    self.wait_until(lambda: states and states[-1] is not None, 30, "the poll never saw the takeover")
                finally:
                    stop.set()
                    poller.join(30)
                self.assertEqual(set(states) - {None, ("lost", "its lock file is another run's now")}, set())
                self.assertIsNone(states[0])
                lines = []
                with mock.patch("vidfunnel.record._warn", side_effect=lines.append):
                    problem = lock_problem(self.base)
                self.assertIn("was taken over by another run", problem)
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("its lock file is another run's now; host 'takerhost' holds it now", lines[0])
                self.assertNotIn("gone", lines[0])
                lock.release()
                taker.stdin.close()
                self.assertEqual(taker.wait(30), 0)
                taker.stdout.close()
                self.assertEqual(self.names(), [])

    # ------------------------------------------------------------ the marker

    def test_a_fresh_marker_is_a_takeover_in_progress(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        self.write_marker(10)
        lock_before = self.snapshot(self.lock_file())
        marker_before = self.snapshot(self.marker_file())
        with self.assertRaises(LockHeld) as caught:
            self.take()
        message = str(caught.exception)
        self.assertIn("is being taken over by host 'markerhost' pid 5150: a takeover is in progress", message)
        self.assertRegex(message, r"its marker '.*lock\.taking' was written 1[0-9] s ago on the share's clock")
        self.assertEqual((caught.exception.host, caught.exception.pid), ("markerhost", 5150))
        self.assertEqual(self.snapshot(self.lock_file()), lock_before)
        self.assertEqual(self.snapshot(self.marker_file()), marker_before)
        self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])
        self.assert_nothing_of_ours()

    def test_a_stale_marker_is_cleared_whatever_this_machines_clock_says(self):
        self.apply(another_machine_patches())
        for shift_hours in (0, 5, -5):
            with self.subTest(shift_hours=shift_hours):
                self.write_dead_lock()
                self.write_marker(TAKEOVER_MARKER_STALE_SECONDS + 60)
                clock = ShiftedClock(shift_hours * 3600)
                warnings = []
                with clock.patches()[0], clock.patches()[1]:
                    lock = self.take(warnings)
                self.assert_ours(lock)
                self.assertEqual(len(warnings), 2, warnings)
                self.assertIn("cleared the takeover marker '{}' of host 'markerhost' pid 5150: written 36".format(
                    self.marker_file()), warnings[0])
                self.assertIn("s ago on the share's clock, more than 300 s", warnings[0])
                self.assertIn("taking over lock", warnings[1])
                self.assertEqual(self.names(), [LOCK_NAME])
                lock.release()

    def test_a_marker_exactly_at_the_threshold_is_held_and_just_past_it_is_cleared(self):
        self.apply(another_machine_patches())
        for past_ns, cleared in ((0, False), (10 ** 6, True)):
            with self.subTest(past_ns=past_ns):
                self.write_dead_lock()
                self.write_marker(0)
                stamp = os.stat(str(self.marker_file())).st_mtime_ns
                share_now = stamp + TAKEOVER_MARKER_STALE_SECONDS * 10 ** 9 + past_ns
                warnings = []
                with mock.patch("vidfunnel.record._share_clock_ns", return_value=share_now):
                    if cleared:
                        lock = self.take(warnings)
                        self.assertIn("written 300 s ago on the share's clock, more than 300 s", warnings[0])
                        self.assertEqual(self.names(), [LOCK_NAME])
                        lock.release()
                    else:
                        with self.assertRaises(LockHeld) as caught:
                            self.take(warnings)
                        self.assertIn("was written 300 s ago on the share's clock", str(caught.exception))
                        self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])
                        os.unlink(str(self.marker_file()))

    def test_without_a_probe_file_a_marker_is_judged_on_this_machines_clock(self):
        self.apply(another_machine_patches())
        refused = mock.patch("vidfunnel.record._share_clock_ns", side_effect=PermissionError(13, "Permission denied"))
        self.apply([refused])
        # Without the share's clock the lock itself is judged by the 24 h rule: it is 30 h old and silent as long.
        self.write_dead_lock(beat_seconds_ago=30 * 3600, started_hours_ago=30)
        self.write_marker(TAKEOVER_MARKER_STALE_SECONDS + 60)
        behind = ShiftedClock(-600)  # this machine's clock is ten minutes behind: the marker looks young
        with behind.patches()[0], behind.patches()[1]:
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("on this machine's clock (no probe file could be created)", str(caught.exception))
        self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])
        warnings = []
        lock = self.take(warnings)
        self.assertIn("cleared the takeover marker", warnings[0])
        self.assertIn("ago on this machine's clock (no probe file could be created), more than 300 s", warnings[0])
        self.assert_ours(lock)
        self.assertEqual(self.names(), [LOCK_NAME])

    def two_takers(self, first, second):
        """Run first() on a thread named 'first' and second() on one named 'second'; their outcomes by name."""
        outcomes = {}

        def run(work):
            try:
                outcomes[threading.current_thread().name] = ("ACQUIRED", work())
            except LockHeld as held:
                outcomes[threading.current_thread().name] = ("HELD", str(held))
            except BaseException as error:
                outcomes[threading.current_thread().name] = ("ERROR", repr(error))

        threads = [threading.Thread(target=run, args=(work,), name=name)
                   for work, name in ((first, "first"), (second, "second"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return outcomes

    def stale_marker_race(self, ordering):
        """Two takers judge one stale marker; `ordering` patches who moves it when; returns their outcomes."""
        self.apply(another_machine_patches())
        self.write_dead_lock()
        self.write_marker(TAKEOVER_MARKER_STALE_SECONDS + 60)
        both_judged = threading.Barrier(2, timeout=30)
        real_judge = FolderLock._judge_marker
        judged_once = set()

        def judge_then_meet(lock, path):
            found = real_judge(lock, path)
            if threading.current_thread().name not in judged_once:
                judged_once.add(threading.current_thread().name)
                both_judged.wait()
            return found

        self.apply([mock.patch.object(FolderLock, "_judge_marker", judge_then_meet)] + ordering)
        def acquire():
            return FolderLock(self.base, warn=lambda line: None).acquire()

        outcomes = self.two_takers(acquire, acquire)
        for kind, value in outcomes.values():
            if kind == "ACQUIRED":
                self.addCleanup(value.release)
        return outcomes

    def test_two_takers_clearing_one_stale_marker_one_proceeds(self):
        """The first moves the marker aside and the second's rename finds nothing; whichever creates the fresh marker
        first proceeds, the other is refused."""
        first_moved = threading.Event()
        second_tried = threading.Event()
        real_move = FolderLock._move_marker_aside

        def move(lock, *arguments):
            if threading.current_thread().name == "second":
                self.assertTrue(first_moved.wait(30))
                try:
                    return real_move(lock, *arguments)
                finally:
                    second_tried.set()
            return real_move(lock, *arguments)

        def step(name):
            if name == "moved-aside" and threading.current_thread().name == "first":
                first_moved.set()
                self.assertTrue(second_tried.wait(30))

        outcomes = self.stale_marker_race([mock.patch.object(FolderLock, "_move_marker_aside", move),
                                           mock.patch("vidfunnel.record._takeover_step", side_effect=step)])
        self.assertEqual(sorted(kind for kind, _value in outcomes.values()), ["ACQUIRED", "HELD"], outcomes)
        winner = [value for kind, value in outcomes.values() if kind == "ACQUIRED"][0]
        loser = [value for kind, value in outcomes.values() if kind == "HELD"][0]
        self.assertTrue(any(reason in loser for reason in RACE_REFUSALS), outcomes)
        self.assert_ours(winner)
        self.assertEqual(self.names(), [LOCK_NAME])

    def test_a_fresh_marker_moved_aside_by_a_late_clearer_stops_both(self):
        """The rare case: the second clearer moves the first's new marker; neither touches the lock."""
        first_marked = threading.Event()
        second_done = threading.Event()
        real_move = FolderLock._move_marker_aside

        def move(lock, *arguments):
            if threading.current_thread().name == "second":
                self.assertTrue(first_marked.wait(30))
                try:
                    return real_move(lock, *arguments)
                finally:
                    second_done.set()
            return real_move(lock, *arguments)

        def step(name):
            if name == "marked" and threading.current_thread().name == "first":
                first_marked.set()
                self.assertTrue(second_done.wait(30))

        outcomes = self.stale_marker_race([mock.patch.object(FolderLock, "_move_marker_aside", move),
                                           mock.patch("vidfunnel.record._takeover_step", side_effect=step)])
        self.assertEqual(outcomes["second"][0], "HELD", outcomes)
        self.assertIn("was replaced while this run cleared it as stale", outcomes["second"][1])
        self.assertEqual(outcomes["first"][0], "HELD", outcomes)
        self.assertIn("is no longer at its path", outcomes["first"][1])
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8"))["host"], "deadhost")
        left = [name for name in self.names() if name.startswith(MARKER_ASIDE_PREFIX)]
        self.assertEqual(len(left), 1, self.names())
        self.assertIn(left[0], outcomes["second"][1])
        self.assertEqual(self.names(), sorted([LOCK_NAME] + left))

    def test_a_moved_aside_marker_swept_away_before_it_is_read_lets_the_takeover_go_on(self):
        """Another taker's sweep removes our moved-aside stale marker between our stat and our read of it."""
        self.apply(another_machine_patches())
        self.write_dead_lock()
        self.write_marker(TAKEOVER_MARKER_STALE_SECONDS + 60)
        real_read = vidfunnel.record._read_json
        swept = []

        def swept_first(path):
            if os.path.basename(os.fspath(path)).startswith(MARKER_ASIDE_PREFIX):
                os.unlink(os.fspath(path))
                swept.append(path)
            return real_read(path)

        warnings = []
        with mock.patch("vidfunnel.record._read_json", side_effect=swept_first):
            lock = self.take(warnings)
        self.assertEqual(len(swept), 1)
        self.assert_ours(lock)
        self.assertEqual(self.names(), [LOCK_NAME])
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("taking over lock", warnings[0])

    def test_a_marker_replaced_before_the_rename_stops_the_taker(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        lock_before = self.snapshot(self.lock_file())
        foreign = {"host": "foreignhost", "pid": 31337, "token": uuid.uuid4().hex, "started": "2026-11-10T00:00:00Z"}

        def replace_marker(step):
            if step == "written":
                staged = self.base / RECORD_DIR / "foreign-staged"
                write_bytes(staged, json.dumps(foreign).encode("utf-8"))
                os.replace(str(staged), str(self.marker_file()))

        with mock.patch("vidfunnel.record._takeover_step", side_effect=replace_marker):
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("this run's takeover marker '{}' is no longer at its path".format(self.marker_file()),
                      str(caught.exception))
        self.assertEqual(self.snapshot(self.lock_file()), lock_before)
        self.assertEqual(json.loads(self.marker_file().read_text(encoding="utf-8")), foreign)
        self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])
        self.assert_nothing_of_ours()

    def test_a_beat_while_the_taker_holds_the_marker_refuses_and_the_holder_beats_on(self):
        script = self.base / "holder.py"
        script.write_text(FAST_HOLDER_SCRIPT, encoding="utf-8")
        holder = subprocess.Popen([sys.executable, str(script), str(self.base)], stdout=subprocess.PIPE,
                                  universal_newlines=True,
                                  env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline(), "ACQUIRED\n")
        before = os.stat(str(self.lock_file()))
        content = self.lock_file().read_bytes()
        read_at = []

        def share_clock_two_hours_on(directory):
            read_at.append(os.stat(str(self.lock_file())).st_mtime_ns)
            return read_at[-1] + 2 * 3600 * 10 ** 9  # the judge takes the holder for silent

        def beat_lands(step):
            if step == "marked":
                self.wait_until(lambda: os.stat(str(self.lock_file())).st_mtime_ns > read_at[0], 30,
                                "the holder did not beat")

        self.apply(another_machine_patches())
        with mock.patch("vidfunnel.record._share_clock_ns", side_effect=share_clock_two_hours_on), \
                mock.patch("vidfunnel.record._takeover_step", side_effect=beat_lands):
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("its holder beat while the lock was being judged; not taken over", str(caught.exception))
        self.assertEqual(self.names(), [LOCK_NAME])
        after = os.stat(str(self.lock_file()))
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(self.lock_file().read_bytes(), content)
        beaten = after.st_mtime_ns
        self.wait_until(lambda: os.stat(str(self.lock_file())).st_mtime_ns > beaten, 30, "the holder stopped beating")
        self.assertIsNone(holder.poll())
        self.assert_nothing_of_ours()

    # ------------------------------------------------------------ failures

    def test_a_failure_at_any_step_leaves_the_lock_and_no_file_of_ours(self):
        self.apply(another_machine_patches())
        real_open, real_write, real_fsync, real_replace = os.open, os.write, os.fsync, os.replace
        failure = OSError(errno.EIO, "Input/output error")
        cases = ("marker create", "marker write", "marker fsync", "temporary create", "temporary write",
                 "temporary fsync", "rename", "interrupt")
        for case in cases:
            with self.subTest(case=case):
                self.write_dead_lock()
                lock_before = self.snapshot(self.lock_file())
                kinds = {}
                opened = []

                def kind_of(path):
                    name = os.path.basename(os.fspath(path))
                    if name == TAKEOVER_MARKER:
                        return "marker"
                    if name.startswith(NEW_LOCK_PREFIX):
                        return "temporary"
                    return None

                def failing_open(path, flags, *rest):
                    kind = kind_of(path)
                    if case == "{} create".format(kind):
                        raise failure
                    handle = real_open(path, flags, *rest)
                    if kind is not None:
                        kinds[handle] = kind
                        opened.append(handle)
                    return handle

                def failing_write(handle, data):
                    if case == "{} write".format(kinds.get(handle)):
                        raise failure
                    return real_write(handle, data)

                def failing_fsync(handle):
                    if case == "{} fsync".format(kinds.get(handle)):
                        raise failure
                    return real_fsync(handle)

                def failing_replace(source, target):
                    if case == "rename" and kind_of(source) == "temporary":
                        raise failure
                    return real_replace(source, target)

                def interrupted(step):
                    if case == "interrupt" and step == "written":
                        raise KeyboardInterrupt

                patches = [mock.patch("vidfunnel.record.os.open", side_effect=failing_open),
                           mock.patch("vidfunnel.record.os.write", side_effect=failing_write),
                           mock.patch("vidfunnel.record.os.fsync", side_effect=failing_fsync),
                           mock.patch("vidfunnel.record.os.replace", side_effect=failing_replace),
                           mock.patch("vidfunnel.record._takeover_step", side_effect=interrupted)]
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    expected = KeyboardInterrupt if case == "interrupt" else OSError
                    with self.assertRaises(expected) as caught:
                        self.take()
                if expected is OSError:
                    self.assertIs(caught.exception, failure)
                self.assertEqual(self.snapshot(self.lock_file()), lock_before)
                self.assertEqual(self.names(), [LOCK_NAME])
                self.assert_nothing_of_ours()
                for handle in opened:
                    with self.assertRaises(OSError, msg="descriptor {} left open".format(kinds[handle])):
                        os.fstat(handle)

    def test_a_marker_that_cannot_be_removed_is_left_with_a_warning_and_the_lock_is_ours(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        real_unlink = os.unlink

        def refusing_unlink(path, *rest, **options):
            if os.fspath(path) == str(self.marker_file()):
                raise OSError(errno.EIO, "Input/output error")
            return real_unlink(path, *rest, **options)

        warnings = []
        with mock.patch("vidfunnel.record.os.unlink", side_effect=refusing_unlink):
            lock = self.take(warnings)
        self.assert_ours(lock)
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("cannot remove the takeover marker '{}': [Errno 5] Input/output error; a marker older than "
                      "300 s is cleared by the next takeover".format(self.marker_file()), warnings[0])
        self.assertIn("taking over lock", warnings[1])
        self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])

    def test_a_temporary_lock_removed_before_the_rename_is_lock_held(self):
        """Another run took a stalled taker's file for a leftover: the rename finds nothing and changes nothing."""
        self.apply(another_machine_patches())
        self.write_dead_lock()
        lock_before = self.snapshot(self.lock_file())

        def remove_ours(step):
            if step == "checked":
                for name in self.names():
                    if name.startswith(NEW_LOCK_PREFIX):
                        os.unlink(str(self.base / RECORD_DIR / name))

        with mock.patch("vidfunnel.record._takeover_step", side_effect=remove_ours):
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("was removed before it could be put in place", str(caught.exception))
        self.assertEqual(self.snapshot(self.lock_file()), lock_before)
        self.assertEqual(self.names(), [LOCK_NAME])
        self.assert_nothing_of_ours()

    # ------------------------------------------------------------ after a takeover

    @unittest.skipIf(not Path("/proc/sys/kernel/random/boot_id").exists(), "the flock rule needs a kernel boot id")
    def test_a_lock_taken_over_behaves_like_any_other(self):
        from vidfunnel.ffmpeg import run
        from vidfunnel.record import lock_problem
        self.apply([mock.patch("vidfunnel.record.in_container", return_value=False),
                    mock.patch.object(vidfunnel.record, "HEARTBEAT_SECONDS", 0.02),
                    mock.patch.object(vidfunnel.record, "HEARTBEAT_POLL_SECONDS", 0.01)])
        self.write_dead_lock()
        warnings = []
        lock = self.take(warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assert_ours(lock)
        content = json.loads(self.lock_file().read_text(encoding="utf-8"))
        self.assertIs(content["flock"], True)
        self.assertEqual(content["device"], os.stat(str(self.lock_file())).st_dev)
        self.assertEqual(content["boot"], Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        with held_lock_fds() as descriptors:
            self.assertEqual(descriptors, (lock.handle,))
        self.assertIsNone(lock_problem(self.base))
        stamp = os.stat(str(self.lock_file())).st_mtime_ns
        self.wait_until(lambda: lock.beat.count >= 3, 30, "the new holder does not beat")
        self.wait_until(lambda: os.stat(str(self.lock_file())).st_mtime_ns > stamp, 30, "no beat moved the mtime")
        judge = ("import sys; from unittest import mock; from vidfunnel.record import FolderLock, LockHeld\n"
                 "mock.patch('vidfunnel.record.in_container', return_value=False).start()\n"
                 "try:\n"
                 "    FolderLock(sys.argv[1], warn=lambda line: None).acquire()\n"
                 "    print('ACQUIRED')\n"
                 "except LockHeld as held:\n"
                 "    print('HELD', held)\n")
        completed = subprocess.run([sys.executable, "-c", judge, str(self.base)], capture_output=True,
                                   universal_newlines=True, timeout=60,
                                   env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
        self.assertIn("HELD", completed.stdout, completed.stderr)
        self.assertIn("still holds the lock file's flock", completed.stdout)
        child = ("import fcntl, os, sys\n"
                 "print(os.fstat(int(sys.argv[1])).st_ino)\n")
        completed = run([sys.executable, "-c", child, str(lock.handle)], check=False)
        self.assertEqual(completed.stdout.split(), [str(os.stat(str(self.lock_file())).st_ino)], completed.stderr)
        self.assertIsNone(lock_problem(self.base))
        lock.release()
        self.assertEqual(self.names(), [vidfunnel.record.TAKEOVER_FILE])
        self.assert_nothing_of_ours()

    def test_leftovers_older_than_the_threshold_are_removed_and_never_read_as_records(self):
        from vidfunnel.plan import recorded_claims
        from vidfunnel.report import _folder_records
        self.apply(another_machine_patches())
        source = write_bytes(self.base / "clip.avi", b"video")
        save_record(source, new_record(source, self.base))
        leftovers = {}
        for prefix in (NEW_LOCK_PREFIX, MARKER_ASIDE_PREFIX):
            for age in (TAKEOVER_MARKER_STALE_SECONDS + 120, TAKEOVER_MARKER_STALE_SECONDS - 120):
                name = prefix + uuid.uuid4().hex
                path = write_bytes(self.base / RECORD_DIR / name, json.dumps({"host": "goner"}).encode("utf-8"))
                stamp = time.time_ns() - age * 10 ** 9
                os.utime(str(path), ns=(stamp, stamp))
                leftovers[name] = age
        young = sorted(name for name, age in leftovers.items() if age < TAKEOVER_MARKER_STALE_SECONDS)

        def records_seen():
            return ([record["id"] for _folder, record in iter_records(self.base)],
                    [record["id"] for record in _folder_records(self.base)],
                    recorded_claims(self.base, "clip.mp4", "other.avi"))

        expected = ([load_record(source)["id"]], [load_record(source)["id"]], [])
        self.assertEqual(records_seen(), expected)
        self.write_dead_lock()
        lock = self.take()
        self.assertEqual(self.names(), sorted([LOCK_NAME, "clip.avi.json"] + young))
        self.assertEqual(records_seen(), expected)
        lock.release()

    def test_the_sweep_keeps_records_and_files_whose_names_only_begin_like_a_leftover(self):
        self.apply(another_machine_patches())
        sources = [write_bytes(self.base / name, b"video") for name in ("lock.new.trailer.mkv", "lock.aside.story.avi")]
        for source in sources:
            save_record(source, new_record(source, self.base))
        ids = [load_record(source)["id"] for source in sources]
        hexes = uuid.uuid4().hex
        # uuid4().hex is 32 lowercase hex digits; a name with one uppercase digit is not one the takeover writes.
        kept_names = ["lock.new.notes", NEW_LOCK_PREFIX + hexes[:31], MARKER_ASIDE_PREFIX + hexes + "0",
                      NEW_LOCK_PREFIX + "A" + hexes[1:]]
        for name in kept_names:
            write_bytes(self.base / RECORD_DIR / name, b"not a leftover")
        removed_names = [NEW_LOCK_PREFIX + uuid.uuid4().hex, MARKER_ASIDE_PREFIX + uuid.uuid4().hex]
        for name in removed_names:
            write_bytes(self.base / RECORD_DIR / name, json.dumps({"host": "goner"}).encode("utf-8"))
        stamp = time.time_ns() - (TAKEOVER_MARKER_STALE_SECONDS + 120) * 10 ** 9
        for name in os.listdir(str(self.base / RECORD_DIR)):
            os.utime(str(self.base / RECORD_DIR / name), ns=(stamp, stamp))
        self.write_dead_lock()
        lock = self.take()
        record_names = ["lock.new.trailer.mkv.json", "lock.aside.story.avi.json"]
        self.assertEqual(self.names(), sorted([LOCK_NAME] + record_names + kept_names))
        self.assertEqual([load_record(source)["id"] for source in sources], ids)
        lock.release()

    # ------------------------------------------------------------ a dead taker on this machine

    @unittest.skipIf(vidfunnel.record.boot_id() is None, "the rule needs a kernel boot id")
    def test_a_marker_left_by_a_killed_taker_on_this_machine_is_cleared_at_once(self):
        self.write_dead_lock()
        script = self.base / "marked_taker.py"
        script.write_text(MARKED_TAKER_SCRIPT, encoding="utf-8")
        child = subprocess.Popen([sys.executable, str(script), str(self.base)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, universal_newlines=True,
                                 env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)))
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline(), "MARKED\n")
        marker = json.loads(self.marker_file().read_text(encoding="utf-8"))
        machine, source = machine_identity()
        self.assertEqual({key: marker.get(key) for key in ("pid", "machine", "machine_source", "boot", "device", "guard")},
                         {"pid": child.pid, "machine": machine, "machine_source": source,
                          "boot": vidfunnel.record.boot_id(), "device": os.stat(str(self.marker_file())).st_dev,
                          "guard": "file"})
        child.kill()
        child.wait(30)
        warnings = []
        lock = self.take(warnings)
        self.assert_ours(lock)
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("cleared the takeover marker '{}' of host '{}' pid {}: written ".format(
            self.marker_file(), socket.gethostname(), child.pid), warnings[0])
        self.assertIn("by a takeover on this machine since its last boot, which has ended (nothing here holds the "
                      "takeover flock)", warnings[0])
        self.assertIn("taking over lock", warnings[1])
        self.assertEqual(self.names(), sorted([LOCK_NAME, vidfunnel.record.TAKEOVER_FILE]))

    @unittest.skipIf(vidfunnel.record.boot_id() is None, "the rule needs a kernel boot id")
    def test_a_fresh_marker_is_cleared_at_once_only_when_its_takeover_has_ended_here(self):
        machine, source = machine_identity()
        ours = {"machine": machine, "machine_source": source, "boot": vidfunnel.record.boot_id(),
                "device": os.stat(str(self.base / RECORD_DIR)).st_dev, "guard": "file"}
        refused = mock.patch("vidfunnel.record.fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available"))
        cases = (("another machine", dict(ours, machine="other-machine"), None),
                 ("another boot", dict(ours, boot="other-boot"), None),
                 ("another device", dict(ours, device=ours["device"] + 1), None),
                 ("the directory's flock", dict(ours, guard="directory"), None),
                 ("no takeover flock could be taken here", ours, refused),
                 ("a marker of vidfunnel 20261110", {}, None))
        for name, fields, patch in cases:
            with self.subTest(case=name):
                self.write_dead_lock()
                lock_before = self.snapshot(self.lock_file())
                marker_before = self.write_marker(10, **fields)
                with patch if patch is not None else contextlib.nullcontext():
                    with self.assertRaises(LockHeld) as caught:
                        self.take()
                self.assertIn("is being taken over by host 'markerhost' pid 5150: a takeover is in progress",
                              str(caught.exception))
                self.assertEqual(json.loads(self.marker_file().read_text(encoding="utf-8")), marker_before)
                self.assertEqual(self.snapshot(self.lock_file()), lock_before)
                self.assertEqual(self.names(), sorted([LOCK_NAME, vidfunnel.record.TAKEOVER_FILE, TAKEOVER_MARKER]))
                self.assert_nothing_of_ours()
                os.unlink(str(self.marker_file()))
        with self.subTest(case="every field ours, and the takeover flock free"):
            self.write_dead_lock()
            self.write_marker(10, **ours)
            warnings = []
            lock = self.take(warnings)
            self.assert_ours(lock)
            self.assertIn("which has ended", warnings[0])
            self.assertEqual(self.names(), sorted([LOCK_NAME, vidfunnel.record.TAKEOVER_FILE]))

    # ------------------------------------------------------------ markers that name nobody

    ODD_MARKERS = ("empty", "null", "nested", "symlink", "fifo")

    def write_odd_marker(self, kind, seconds_ago):
        """Put at the marker path something that names nobody, with its own mtime `seconds_ago`; return its lstat.

        An empty file, JSON null, JSON nested too deeply for the parser, a
        dangling symlink, or a FIFO.
        """
        path = str(self.marker_file())
        if kind == "symlink":
            os.symlink(str(self.base / "nowhere"), path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            write_bytes(self.marker_file(), {"empty": b"", "null": b"null", "nested": b"[" * 200000}[kind])
        stamp = time.time_ns() - int(seconds_ago * 10 ** 9)
        os.utime(path, ns=(stamp, stamp), follow_symlinks=False)
        return os.lstat(path)

    def test_a_fresh_marker_that_names_nobody_is_held_and_says_so(self):
        self.apply(another_machine_patches())
        for kind in self.ODD_MARKERS:
            with self.subTest(kind=kind):
                self.write_dead_lock()
                lock_before = self.snapshot(self.lock_file())
                marker_before = self.write_odd_marker(kind, 10)
                with self.assertRaises(LockHeld) as caught:
                    self.take()
                message = str(caught.exception)
                self.assertIn("is being taken over by another run whose marker is empty or unreadable: a takeover is "
                              "in progress", message)
                self.assertNotIn("None", message)
                self.assertEqual((caught.exception.host, caught.exception.pid), (None, None))
                after = os.lstat(str(self.marker_file()))
                self.assertEqual((after.st_ino, after.st_mode), (marker_before.st_ino, marker_before.st_mode))
                self.assertEqual(self.snapshot(self.lock_file()), lock_before)
                self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])
                self.assert_nothing_of_ours()
                os.unlink(str(self.marker_file()))

    def test_a_stale_marker_that_names_nobody_is_cleared_and_the_first_attempt_takes_the_lock(self):
        self.apply(another_machine_patches())
        for kind in self.ODD_MARKERS:
            with self.subTest(kind=kind):
                self.write_dead_lock()
                self.write_odd_marker(kind, TAKEOVER_MARKER_STALE_SECONDS + 60)
                warnings = []
                lock = self.take(warnings)
                self.assert_ours(lock)
                self.assertEqual(self.names(), [LOCK_NAME])  # no moved-aside file is left
                self.assertEqual(len(warnings), 2, warnings)
                self.assertIn("cleared the takeover marker '{}' of another run whose marker is empty or unreadable: "
                              "written 36".format(self.marker_file()), warnings[0])
                self.assertIn("taking over lock", warnings[1])
                lock.release()

    def test_a_lock_file_nested_too_deeply_is_judged_like_any_unreadable_lock(self):
        """Held until its mtime is 24 h old on this machine's clock, then taken over with a warning."""
        self.apply(another_machine_patches())
        write_bytes(self.lock_file(), b"[" * 200000)
        with self.assertRaises(LockHeld) as caught:
            self.take()
        self.assertIn("taken over once 24 h old", str(caught.exception))
        self.assertEqual(self.names(), [LOCK_NAME])
        self.assert_nothing_of_ours()
        old = time.time_ns() - 25 * 3600 * 10 ** 9
        os.utime(str(self.lock_file()), ns=(old, old))
        warnings = []
        lock = self.take(warnings)
        self.assert_ours(lock)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("taking over stale lock '{}'".format(self.lock_file()), warnings[0])
        self.assertEqual(self.names(), [LOCK_NAME])

    # ------------------------------------------------------------ after the rename

    def test_an_interrupt_as_the_rename_returns_leaves_no_lock_of_ours(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        real_replace, real_open = os.replace, os.open
        temporaries = []

        def tracking_open(path, flags, *rest):
            handle = real_open(path, flags, *rest)
            if os.path.basename(os.fspath(path)).startswith(NEW_LOCK_PREFIX):
                temporaries.append(handle)
            return handle

        def replace_then_interrupt(source, target, *rest, **options):
            real_replace(source, target, *rest, **options)
            if os.path.basename(os.fspath(source)).startswith(NEW_LOCK_PREFIX):
                raise KeyboardInterrupt  # as a signal handler's exception arrives once the call has returned

        with mock.patch("vidfunnel.record.os.replace", side_effect=replace_then_interrupt), \
                mock.patch("vidfunnel.record.os.open", side_effect=tracking_open):
            with self.assertRaises(KeyboardInterrupt):
                self.take()
        self.assertEqual(self.names(), [])  # the dead lock was replaced, and our file went from the lock path
        self.assert_nothing_of_ours()
        self.assertEqual(len(temporaries), 1)
        with self.assertRaises(OSError):
            os.fstat(temporaries[0])

    def stat_failing_after_the_rename(self, failure, times=None):
        """Patches under which os.stat of the lock path raises `failure` once the taker has renamed its file, the
        first `times` times (None: every time); returns (patches, the stats that raised)."""
        real_stat = os.stat
        renamed = threading.Event()
        tried = []

        def stat(path, *rest, **options):
            if (renamed.is_set() and os.fspath(path) == str(self.lock_file())
                    and (times is None or len(tried) < times)):
                tried.append(path)
                raise failure
            return real_stat(path, *rest, **options)

        def step(name):
            if name == "renamed":
                renamed.set()

        return [mock.patch("vidfunnel.record.os.stat", side_effect=stat),
                mock.patch("vidfunnel.record._takeover_step", side_effect=step)], tried

    def test_a_lock_path_that_cannot_be_checked_after_the_rename_is_neither_kept_nor_removed(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        patches, tried = self.stat_failing_after_the_rename(OSError(errno.EIO, "Input/output error"))
        lock = FolderLock(self.base, warn=lambda line: None)
        self.addCleanup(lock.release)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(OSError) as caught:
                lock.acquire()
        self.assertEqual(caught.exception.errno, errno.EIO)
        content = json.loads(self.lock_file().read_text(encoding="utf-8"))
        self.assertEqual((content["host"], content["pid"]), (socket.gethostname(), os.getpid()))  # left in place
        self.assertEqual(self.names(), [LOCK_NAME])
        # The check after the rename and the cleanup's check, each tried PATH_CHECK_ATTEMPTS times.
        self.assertEqual(len(tried), 2 * vidfunnel.record.PATH_CHECK_ATTEMPTS)
        self.assertIsNone(lock.content)
        self.assertIsNone(lock.handle)
        self.assert_nothing_of_ours()

    def test_a_lock_path_check_that_fails_once_after_the_rename_is_tried_again(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        patches, tried = self.stat_failing_after_the_rename(OSError(errno.ESTALE, "Stale file handle"), times=1)
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            lock = self.take()
        self.assertEqual(len(tried), 1)
        self.assert_ours(lock)
        with held_lock_fds() as descriptors:
            self.assertEqual(descriptors, (lock.handle,))

    # ------------------------------------------------------------ errors that must not undo a taken lock

    def assert_taken_and_held(self, lock):
        self.assert_ours(lock)
        with held_lock_fds() as descriptors:
            self.assertEqual(descriptors, (lock.handle,))
        with vidfunnel.record._held_locks_guard:
            self.assertIs(vidfunnel.record._folder_beats.get(os.path.abspath(str(self.base))), lock.beat)
            self.assertIn(lock.beat, vidfunnel.record._beats)
        self.assertEqual(self.names(), [LOCK_NAME])

    def test_a_marker_whose_close_fails_leaves_the_lock_taken_and_held(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        real_open, real_close = os.open, os.close
        markers = []
        failed = []

        def tracking_open(path, flags, *rest):
            handle = real_open(path, flags, *rest)
            if os.path.basename(os.fspath(path)) == TAKEOVER_MARKER:
                markers.append(handle)
            return handle

        def failing_close(handle):
            real_close(handle)  # as on Linux: the descriptor is freed even when close reports an error
            if handle in markers:
                markers.remove(handle)
                failed.append(handle)
                raise OSError(errno.EIO, "Input/output error")

        with mock.patch("vidfunnel.record.os.open", side_effect=tracking_open), \
                mock.patch("vidfunnel.record.os.close", side_effect=failing_close):
            lock = self.take()
        self.assertEqual(len(failed), 1)
        self.assert_taken_and_held(lock)

    def test_a_takeover_warning_that_cannot_be_written_leaves_the_lock_taken_and_held(self):
        self.apply(another_machine_patches())
        self.write_dead_lock()
        written = []

        def full_stream(line):
            written.append(line)
            raise OSError(errno.ENOSPC, "No space left on device")

        lock = self.take(warn=full_stream)
        self.assertEqual(len(written), 1, written)
        self.assertIn("taking over lock", written[0])
        self.assert_taken_and_held(lock)

    @unittest.skipIf(vidfunnel.record.fcntl is None, "needs fcntl")
    def test_a_takeover_where_flock_is_refused_takes_the_lock_without_one(self):
        """_take_flock answers False (it never raises): the new lock says flock: false and is ours all the same."""
        self.apply(another_machine_patches())
        self.write_dead_lock()
        warnings = []
        with mock.patch("vidfunnel.record.fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available")) as flock:
            lock = self.take(warnings)
        self.assertTrue(flock.called)
        self.assertIs(lock.content["flock"], False)
        self.assertIs(json.loads(self.lock_file().read_text(encoding="utf-8"))["flock"], False)
        self.assert_taken_and_held(lock)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("taking over lock", warnings[0])

    # ------------------------------------------------------------ each check of the takeover on its own

    def test_a_lock_that_changes_before_the_check_under_the_marker_is_refused_though_it_changes_back(self):
        """Only the check right after the marker is created (step B) sees this: by the last check before the
        rename the lock's mtime is the judged one again."""
        self.apply(another_machine_patches())
        self.write_dead_lock()
        judged = os.stat(str(self.lock_file()))
        lock_before = self.snapshot(self.lock_file())

        def beat_then_undo(step):
            if step == "marked":
                os.utime(str(self.lock_file()), ns=(judged.st_atime_ns, judged.st_mtime_ns + 10 ** 9))
            elif step == "created":
                os.utime(str(self.lock_file()), ns=(judged.st_atime_ns, judged.st_mtime_ns))

        with mock.patch("vidfunnel.record._takeover_step", side_effect=beat_then_undo):
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("its holder beat while the lock was being judged; not taken over", str(caught.exception))
        self.assertEqual(self.snapshot(self.lock_file()), lock_before)
        self.assertEqual(self.names(), [LOCK_NAME])
        self.assert_nothing_of_ours()

    def test_a_beat_between_the_first_and_the_last_check_refuses_the_takeover(self):
        self.apply(another_machine_patches())
        for at_step in ("rechecked", "created", "written"):
            with self.subTest(step=at_step):
                self.write_dead_lock()
                before = os.stat(str(self.lock_file()))
                content = self.lock_file().read_bytes()

                def beat(step, at_step=at_step):
                    if step == at_step:  # what the holder's heartbeat does: its first byte again, then fsync
                        handle = os.open(str(self.lock_file()), os.O_WRONLY)
                        try:
                            os.pwrite(handle, content[:1], 0)
                            os.fsync(handle)
                        finally:
                            os.close(handle)

                with mock.patch("vidfunnel.record._takeover_step", side_effect=beat):
                    with self.assertRaises(LockHeld) as caught:
                        self.take()
                self.assertIn("its holder beat while the lock was being judged; not taken over", str(caught.exception))
                after = os.stat(str(self.lock_file()))
                self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
                self.assertGreater(after.st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(self.lock_file().read_bytes(), content)
                self.assertEqual(self.names(), [LOCK_NAME])
                self.assert_nothing_of_ours()

    def test_a_second_stale_marker_after_one_was_cleared_is_held(self):
        """A run clears a stale marker once per attempt; another stale one found at its next create is held."""
        self.apply(another_machine_patches())
        self.write_dead_lock()
        self.write_marker(TAKEOVER_MARKER_STALE_SECONDS + 60)
        lock_before = self.snapshot(self.lock_file())
        second = []

        def another_stale_marker(step):
            if step == "moved-aside" and not second:
                second.append(self.write_marker(TAKEOVER_MARKER_STALE_SECONDS + 60, host="secondhost", pid=6160))

        with mock.patch("vidfunnel.record._takeover_step", side_effect=another_stale_marker):
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("is being taken over by host 'secondhost' pid 6160: a takeover is in progress",
                      str(caught.exception))
        self.assertEqual(json.loads(self.marker_file().read_text(encoding="utf-8")), second[0])
        self.assertEqual(self.snapshot(self.lock_file()), lock_before)
        self.assertEqual(self.names(), [LOCK_NAME, TAKEOVER_MARKER])
        self.assert_nothing_of_ours()

    def test_the_sweep_of_leftovers_goes_by_the_shares_clock_not_this_machines(self):
        self.apply(another_machine_patches())
        # (this machine's clock ahead in hours, the leftover's age on the share's clock, swept)
        for shift_hours, written_ago, swept in ((1, TAKEOVER_MARKER_STALE_SECONDS - 200, False),
                                                (-1, TAKEOVER_MARKER_STALE_SECONDS + 100, True)):
            with self.subTest(shift_hours=shift_hours):
                self.write_dead_lock()
                name = NEW_LOCK_PREFIX + uuid.uuid4().hex
                leftover = write_bytes(self.base / RECORD_DIR / name, json.dumps({"host": "goner"}).encode("utf-8"))
                stamp = time.time_ns() - written_ago * 10 ** 9
                os.utime(str(leftover), ns=(stamp, stamp))
                clock = ShiftedClock(shift_hours * 3600)
                with clock.patches()[0], clock.patches()[1]:
                    lock = self.take()
                self.assert_ours(lock)
                self.assertEqual(self.names(), [LOCK_NAME] if swept else sorted([LOCK_NAME, name]))
                lock.release()
                if not swept:
                    os.unlink(str(leftover))

    # ------------------------------------------------------------ an older vidfunnel's takeover

    OLD_TAKER = {"host": "oldhost", "pid": 2020, "started": "2026-11-01T00:00:00Z", "container": False}

    def old_style_takeover(self, create=True):
        """What a vidfunnel before 20261110 does once it has judged the lock dead: unlink, then O_EXCL create."""
        try:
            os.unlink(str(self.lock_file()))
        except FileNotFoundError:
            pass
        if create:
            handle = os.open(str(self.lock_file()), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(self.OLD_TAKER))

    def take_with_old_taker_at(self, at_step, create=True):
        def old_taker(step):
            if step == at_step:
                self.old_style_takeover(create)

        self.apply(another_machine_patches())
        self.write_dead_lock()
        with mock.patch("vidfunnel.record._takeover_step", side_effect=old_taker):
            return self.take()

    def test_an_old_style_takeover_while_we_hold_the_marker_wins_before_our_recheck(self):
        with self.assertRaises(LockHeld) as caught:
            self.take_with_old_taker_at("marked")
        self.assertEqual(caught.exception.host, "oldhost")
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), self.OLD_TAKER)
        self.assertEqual(self.names(), [LOCK_NAME])
        self.assert_nothing_of_ours()

    def test_an_old_style_unlink_at_our_last_check_is_lock_held_and_the_path_stays_free(self):
        def old_taker(step):
            if step == "written":
                self.old_style_takeover(create=False)

        self.apply(another_machine_patches())
        self.write_dead_lock()
        with mock.patch("vidfunnel.record._takeover_step", side_effect=old_taker):
            with self.assertRaises(LockHeld) as caught:
                self.take()
        self.assertIn("its lock file '{}' was removed by another run while this run was taking it over".format(
            self.lock_file()), str(caught.exception))
        self.assertEqual(self.names(), [])
        self.assert_nothing_of_ours()

    def test_an_old_style_unlink_after_our_last_check_lets_our_rename_create_the_lock(self):
        lock = self.take_with_old_taker_at("checked", create=False)
        self.assert_ours(lock)
        with self.assertRaises(FileExistsError):  # the older taker's create that follows its unlink
            os.open(str(self.lock_file()), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        self.assertEqual(self.names(), [LOCK_NAME])

    def test_an_old_style_takeover_after_our_last_check_is_replaced_by_our_rename(self):
        """The window that remains: the older taker's new lock is renamed over and it learns of that only
        through a heartbeat of its own (vidfunnel 20261109.4 and later)."""
        lock = self.take_with_old_taker_at("checked")
        self.assert_ours(lock)
        self.assertEqual(self.names(), [LOCK_NAME])

    def test_an_old_style_takeover_just_after_our_rename_is_lock_held_and_left_alone(self):
        with self.assertRaises(LockHeld) as caught:
            self.take_with_old_taker_at("renamed")
        self.assertEqual(caught.exception.host, "oldhost")
        self.assertIn("got it first", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), self.OLD_TAKER)
        self.assertEqual(self.names(), [LOCK_NAME])
        self.assert_nothing_of_ours()


class BandTests(unittest.TestCase):
    def test_band_edges(self):
        from vidfunnel.record import band
        self.assertEqual(band(100), "ok")
        self.assertEqual(band(90), "ok")
        self.assertEqual(band(89), "review")
        self.assertEqual(band(60), "review")
        self.assertEqual(band(59), "hold")
        self.assertEqual(band(0), "hold")
        self.assertIsNone(band(None))
        self.assertIsNone(band(True))

if __name__ == "__main__":
    unittest.main()

# End of File #
