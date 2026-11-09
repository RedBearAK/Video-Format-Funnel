"""Tests for vidfunnel.record: paths, atomic saves, fingerprints, hashes, stages, lock."""

VERSION = "20261109"

import datetime
import hashlib
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock
from pathlib import Path

from vidfunnel.record import (
    machine_id,
    machine_identity,
    FOLDER_FILE,
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


def race_for_lock(folder, barrier, results):
    """One racer of test_racers_for_a_dead_lock_get_it_once: wait at the barrier, try the lock, hold it 0.5 s."""
    import time
    barrier.wait()
    try:
        with FolderLock(folder, warn=lambda message: None):
            results.put("ACQUIRED")
            time.sleep(0.5)
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
                             ["boot", "container", "device", "flock", "host", "machine", "machine_source", "pid",
                              "started"])
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
        last_create = len(opened) - 1 - opened[::-1].index(str(self.lock_file()))
        self.assertLess(opened.index(takeover), last_create, "the guard is held before our lock file is created")
        self.assertTrue(Path(takeover).exists())

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
                processes = [context.Process(target=race_for_lock, args=(str(self.base), barrier, results))
                             for _number in range(racers)]
                for process in processes:
                    process.start()
                outcomes = sorted(results.get(timeout=60) for _number in range(racers))
                for process in processes:
                    process.join(60)
                self.assertEqual(outcomes, ["ACQUIRED"] + ["HELD"] * (racers - 1))
                self.assertFalse(self.lock_file().exists())

    def test_losing_the_create_after_a_takeover_names_the_winner(self):
        dead = self.finished_pid()
        self.write_lock(socket.gethostname(), dead, 0.5, container=False)
        winner = {"host": "winnerhost", "pid": 777, "started": "2026-11-07T12:00:00Z", "container": False}
        real_open = os.open
        calls = []

        def racing_open(path, flags, *args):
            if path != str(self.lock_file()):
                return real_open(path, flags, *args)
            calls.append(path)
            if len(calls) == 2:
                write_bytes(path, json.dumps(winner).encode("utf-8"))
            return real_open(path, flags, *args)

        with mock.patch("vidfunnel.record.in_container", return_value=False), \
                mock.patch("vidfunnel.record.os.open", side_effect=racing_open):
            with self.assertRaises(LockHeld) as caught:
                with FolderLock(self.base, warn=self.fail):
                    pass
        self.assertEqual(len(calls), 2)
        self.assertEqual((caught.exception.host, caught.exception.pid), ("winnerhost", 777))
        self.assertIn("got it first", str(caught.exception))
        self.assertEqual(json.loads(self.lock_file().read_text(encoding="utf-8")), winner)

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
        while pid_alive(child_pid):
            self.assertLess(datetime.datetime.now(), deadline, "the stand-in did not end")
            time.sleep(0.02)
        warnings = []
        self.judge(warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("pid {}, has exited".format(holder.pid), warnings[0])


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
