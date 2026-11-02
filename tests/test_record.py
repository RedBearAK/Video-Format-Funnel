"""Tests for vidfunnel.record: paths, atomic saves, fingerprints, hashes, stages, lock."""

VERSION = "20261102"

import datetime
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from unittest import mock
from pathlib import Path

from vidfunnel.record import (
    FOLDER_FILE,
    LOCK_NAME,
    RECORD_DIR,
    FolderLock,
    LockHeld,
    RecordError,
    add_flag,
    add_penalty,
    add_stage,
    cheap_fingerprint,
    fingerprint_matches,
    iter_records,
    load_folder,
    load_record,
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


class FolderLockTests(TempDirCase):
    def lock_file(self):
        return self.base / RECORD_DIR / LOCK_NAME

    def test_second_acquisition_raises(self):
        with FolderLock(self.base):
            content = json.loads(self.lock_file().read_text(encoding="utf-8"))
            self.assertEqual(content["pid"], os.getpid())
            self.assertEqual(sorted(content), ["host", "pid", "started"])
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

    def test_lock_removed_after_exception(self):
        with self.assertRaises(RuntimeError):
            with FolderLock(self.base):
                self.assertTrue(self.lock_file().exists())
                raise RuntimeError("boom")
        self.assertFalse(self.lock_file().exists())


if __name__ == "__main__":
    unittest.main()

# End of File #
