"""Tests for the later rounds over folders another run held: convert, convert --refit and plan.

vidfunnel.turns.held_rounds visits again, after the main pass, the folders
that answered SKIPPED, settles without a lock what the other run has done,
and takes the lock for what is left. These tests run in process without
ffmpeg: each stage's work on an item is a stand-in that notes whether this
run held the item's folder and saves the record; the folder locks are real
(vidfunnel.record.FolderLock, with acquire and release logged). Another run's
lock is a lock file of another machine that beat just now.

The rounds are driven by replacing vidfunnel.turns.wait_for_folders with a
script of steps (what the other run does while this one waits), so no test
depends on the clock or on thread scheduling. The tests of the real wait set
HELD_RETRY_SECONDS to twice DEADLINE_SECONDS and end the wait from another
thread once the waiting line has been written: by a drain, a stop file or an
interrupt. Should that not end it within DEADLINE_SECONDS, the thread notes
it and removes the other run's lock, so the next round finishes the run and
the test fails instead of hanging; a wait that ignores the drain and ends
only at its interval is caught the same way.
"""

VERSION = "20261111"

import _thread
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path

import vidfunnel.interrupt
import vidfunnel.plan
import vidfunnel.record as record_module
import vidfunnel.stopfile
import vidfunnel.turns
from vidfunnel import encode
from vidfunnel.plan import make_options, plan_paths
from vidfunnel.record import cheap_fingerprint, load_record, new_record, save_record, set_status

DEADLINE_SECONDS = 60.0
LAYOUT = (("first", ("harbour",)), ("second", ("meadow", "orchard")), ("third", ("valley",)))
POSITIONS = {"harbour": 1, "meadow": 2, "orchard": 3, "valley": 4}
DONE_WORD = {"convert": "converted", "refit": "refitted", "plan": "remux"}
# The main pass tries a held folder's lock once, and the first round, which follows it at once, once more.
TRIED_TWICE = ["acquiring", "acquiring"]


def lock_file_of(folder):
    return Path(folder) / ".vidfunnel" / "lock"


def foreign_live_lock(folder):
    """A lock of another machine whose holder beat just now (its mtime is the share's now)."""
    path = lock_file_of(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"host": "elsewhere-host", "pid": 4242, "started": started, "container": False,
                                "machine": "elsewhere-machine", "machine_source": "machine-id",
                                "boot": "elsewhere-boot", "flock": False,
                                "heartbeat": {"interval": record_module.HEARTBEAT_SECONDS}}), encoding="utf-8")
    return path


class WatchedStream(io.StringIO):
    """A StringIO whose `waiting` event is set once a waiting line has been written to it."""

    def __init__(self):
        io.StringIO.__init__(self)
        self.waiting = threading.Event()

    def write(self, text):
        written = io.StringIO.write(self, text)
        if text.startswith("waiting for "):
            self.waiting.set()
        return written


class RoundsCase(object):
    """The cases, for the stage named by `stage` ('convert', 'refit' or 'plan'); mixed into a TestCase below."""

    stage = None

    def setUp(self):
        self.clean_flags()
        self.addCleanup(self.clean_flags)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "library"
        self.guard = threading.Lock()
        self.events = []
        self.steps = []      # what the other run does at each wait, in order (scripted waits)
        self.clock = [0.0]   # the waits' clock (vidfunnel.turns._clock) in scripted runs; steps move it on
        self.waits = []      # the folder names each wait was given
        self.refused = set()  # folder names whose lock file cannot be created
        self.sources = {}

    def clean_flags(self):
        for flag in (vidfunnel.interrupt.interrupted, vidfunnel.interrupt.draining,
                     vidfunnel.interrupt.stopped_by_file, vidfunnel.interrupt.stopped_by_signal):
            flag.clear()
        vidfunnel.interrupt.drain_tally.reset()
        vidfunnel.stopfile.reset()

    def fresh(self):
        """A new library for the next subtest: the stage's sources and records, nothing logged."""
        self.clean_flags()
        shutil.rmtree(str(self.root), ignore_errors=True)
        self.root.mkdir(parents=True)
        self.events.clear()
        self.steps.clear()
        self.clock[0] = 0.0
        self.waits.clear()
        self.refused.clear()
        self.sources = {}
        for folder_name, stems in LAYOUT:
            folder = self.root / folder_name
            folder.mkdir()
            for stem in stems:
                source = folder / (stem + ".avi")
                source.write_bytes(("source of " + stem + " ").encode() * 64)
                self.sources[stem] = source
                if self.stage == "plan":
                    continue
                record = new_record(source)
                record["output"]["name"] = stem + ".mp4"
                if self.stage == "convert":
                    record["action"] = "remux"
                    set_status(record, "planned")
                else:
                    output = folder / (stem + ".mp4")
                    output.write_bytes(("larger output of " + stem + " ").encode() * 96)
                    record["action"] = "reencode"
                    record["flags"] = ["output-larger"]
                    record["output"]["fingerprint"] = cheap_fingerprint(output)
                    set_status(record, "converted")
                save_record(source, record)

    def folder(self, name):
        return self.root / name

    # -- what the other run does

    def finish_elsewhere(self, stem, status=None):
        """The other run's result for `stem`, written as it would write it (no lock of this process there)."""
        source = self.sources[stem]
        if self.stage == "plan":
            record = new_record(source)
            record["action"] = "remux"
            record["output"]["name"] = stem + ".mp4"
            set_status(record, "planned")
        elif self.stage == "convert":
            record = load_record(source)
            output = source.parent / (stem + ".mp4")
            output.write_bytes(("output of " + stem + " ").encode() * 32)
            record["output"]["fingerprint"] = cheap_fingerprint(output)
            record.setdefault("stages", {})["convert"] = {"outcome": "ok", "host": "elsewhere-host"}
            set_status(record, status or "converted")
        else:
            record = load_record(source)
            record["stages"]["refit"] = {"outcome": "replaced", "host": "elsewhere-host"}
            record["flags"] = ["refitted"]
        save_record(source, record)

    def settled_line(self, stem):
        """The line the stage prints for an item it settles without a lock (the plan line starts this way)."""
        tag = "[{} {}/4] ".format(self.stage, POSITIONS[stem])
        if self.stage == "convert":
            return tag + "skipping '{}.avi': already converted\n".format(stem)
        if self.stage == "refit":
            return tag + "skipping '{}.avi': already refitted\n".format(stem)
        return tag + "skipping '{}.avi': record {} is 'planned' and the source is unchanged".format(
            stem, load_record(self.sources[stem])["id"])

    # -- the run

    def log(self, *event):
        with self.guard:
            self.events.append(event)

    def held_here(self, folder):
        try:
            return json.loads(lock_file_of(folder).read_text(encoding="utf-8")).get("pid") == os.getpid()
        except (OSError, ValueError):
            return False

    def stand_in(self, key):
        """The item's work: note whether this run holds the folder, then save the record as done."""
        key = Path(key)
        self.log("start", key.parent.name, key.stem, self.held_here(key.parent))
        if self.stage == "plan":
            record = new_record(key)
            record["action"] = "remux"
            record["output"]["name"] = key.stem + ".mp4"
            set_status(record, "planned")
        else:
            record = load_record(key)
            stage_name = "refit" if self.stage == "refit" else "convert"
            record.setdefault("stages", {})[stage_name] = {"outcome": "stand-in"}
            if stage_name == "convert":
                set_status(record, "converted")
        save_record(key, record)
        return record

    def scripted_wait(self, folders):
        self.waits.append([Path(folder).name for folder in folders])
        if not self.steps:
            raise AssertionError("the run waited for {} with no step of the other run left".format(self.waits[-1]))
        self.steps.pop(0)(folders)
        return None  # the interval passed

    def patches(self, real_wait=False):
        case = self

        class LoggedLock(record_module.FolderLock):
            def acquire(self):
                name = Path(self.folder).name
                case.log("acquiring", name)
                if name in case.refused:
                    raise PermissionError(13, "Permission denied")
                taken = super().acquire()
                case.log("acquired", name)
                return taken

            def release(self):
                super().release()
                case.log("released", Path(self.folder).name)

        def convert_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            self.stand_in(key)
            return {"outcome": "ok"}

        def refit_stand_in(tools, key, record, options, progress=None, position=None, total=None):
            self.stand_in(key)
            return "refitted", None

        def plan_stand_in(tools, work, found, options, folder_sources, capabilities, progress, stream, position,
                          total, record_lock=None):
            return "remux", self.stand_in(work.key)

        made = [unittest.mock.patch("vidfunnel.turns.FolderLock", LoggedLock),
                unittest.mock.patch("vidfunnel.plan.FolderLock", LoggedLock),
                unittest.mock.patch("vidfunnel.encode.convert_one", side_effect=convert_stand_in),
                unittest.mock.patch("vidfunnel.encode.refit_one", side_effect=refit_stand_in),
                unittest.mock.patch("vidfunnel.plan._plan_entry", side_effect=plan_stand_in),
                unittest.mock.patch("vidfunnel.plan.ffmpeg_capabilities", return_value={}),
                unittest.mock.patch("vidfunnel.plan.decodable_codecs", return_value=set()),
                unittest.mock.patch("vidfunnel.encode.run_tool", side_effect=AssertionError("ffmpeg started")),
                unittest.mock.patch("vidfunnel.record._warn")]
        if real_wait:
            made += [unittest.mock.patch("vidfunnel.turns.HELD_RETRY_SECONDS", 2 * DEADLINE_SECONDS),
                     unittest.mock.patch("vidfunnel.turns.HELD_POLL_SECONDS", 0.01)]
        else:
            made += [unittest.mock.patch("vidfunnel.turns.wait_for_folders", side_effect=self.scripted_wait),
                     unittest.mock.patch("vidfunnel.turns._clock", lambda: self.clock[0])]
        return made

    def run_stage(self, jobs, stream=None, real_wait=False, **values):
        progress = types.SimpleNamespace(stream=stream if stream is not None else WatchedStream(), quiet=True,
                                         verbose=False)
        with contextlib.ExitStack() as stack:
            for patch in self.patches(real_wait):
                stack.enter_context(patch)
            if self.stage == "plan":
                summary = plan_paths(None, [str(self.root)], make_options(jobs=jobs, **values), progress)
            else:
                options = types.SimpleNamespace(par2=None, verbose=False, jobs=jobs, dry_run=False,
                                                refit=self.stage == "refit", **values)
                summary = encode.convert_paths(None, [str(self.root)], options, progress)
        return summary, progress.stream.getvalue()

    # -- what the log says

    def started(self):
        return [event[2] for event in self.events if event[0] == "start"]

    def attempts(self, name):
        return [event[0] for event in self.events if event[1] == name and event[0] in ("acquiring", "acquired")]

    def assert_every_start_held_its_folder(self):
        starts = [event for event in self.events if event[0] == "start"]
        self.assertEqual([event for event in starts if not event[3]], [], "an item ran without its folder's lock")

    def assert_counted_once(self, summary, done, skipped, text):
        self.assertEqual(summary.counts[DONE_WORD[self.stage]], done, text)
        self.assertEqual(summary.counts["skipped"], skipped, text)
        self.assertEqual(sum(summary.counts.values()), 4, (summary.counts, text))

    def assert_no_lock_of_ours(self):
        for name in ("first", "second", "third"):
            self.assertFalse(self.held_here(self.folder(name)), name)

    def skip_lines(self, text, name):
        return text.count("skipping folder '{}': ".format(self.folder(name)))

    def announcements(self, text):
        return [line for line in text.splitlines() if line.startswith("waiting for ")]

    def holder_line(self, name, items):
        """A pattern for the line that names a folder another machine holds, in a wait's announcement."""
        return re.compile(re.escape("  '{}': held by host 'elsewhere-host' pid 4242, last beat ".format(
            self.folder(name))) + r"\d+ s ago \(taken over if it stays silent for \d+ s more, at the first try "
            r"after that\); " + re.escape("{} item(s) of this run left there".format(items)) + "$", re.MULTILINE)

    def taken_line(self, name, words):
        return "folder '{}' taken by this run in a later round: {}\n".format(self.folder(name), words)

    # -- the cases

    def test_a_folder_released_later_is_worked_on_in_a_later_round(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                lock = foreign_live_lock(self.folder("second"))
                self.steps.append(lambda folders: os.unlink(str(lock)))
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 4, 0, text)
                self.assertEqual(sorted(self.started()), ["harbour", "meadow", "orchard", "valley"])
                self.assertLess(self.started().index("valley"), self.started().index("meadow"),
                                "the held folder was not left for a later round")
                self.assert_every_start_held_its_folder()
                self.assertEqual(self.attempts("second"), TRIED_TWICE + ["acquiring", "acquired"])
                self.assertEqual(self.waits, [["second"]])
                self.assertEqual(self.skip_lines(text, "second"), 1, text)
                self.assertEqual(self.announcements(text),
                                 ["waiting for 1 folder(s) other runs hold, trying each again every 60 s:"], text)
                self.assertRegex(text, self.holder_line("second", 2))
                self.assertIn("the wait ends for a folder when its holder finishes or releases it, or when its "
                              "holder's heartbeat has been silent for 3600 s and its lock is taken over; Ctrl+C, "
                              "SIGUSR1 or 'vidfunnel stop-all PATH' ends the whole wait; --no-wait skips such folders "
                              "instead\n", text)
                self.assertIn(self.taken_line("second", "the run that held it has released it"), text)
                self.assertNotIn("still waiting", text)
                self.assertFalse(lock_file_of(self.folder("second")).exists())
                if self.stage == "plan":  # the folder file is written under the lock taken in the round
                    data = json.loads((self.folder("second") / ".vidfunnel" / "folder.json").read_text())
                    self.assertEqual(data["sources"], ["meadow.avi", "orchard.avi"])

    def test_work_the_holder_finished_is_settled_without_the_lock(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                lock = foreign_live_lock(self.folder("second"))
                lock_bytes = lock.read_bytes()

                def holder_finishes(folders):
                    self.finish_elsewhere("meadow")
                    self.finish_elsewhere("orchard")

                self.steps.append(holder_finishes)
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 2, 2, text)
                self.assertEqual(sorted(self.started()), ["harbour", "valley"])
                self.assertEqual(self.attempts("second"), TRIED_TWICE, "the lock was tried after the holder finished")
                self.assertEqual(lock.read_bytes(), lock_bytes)
                self.assertEqual(self.waits, [["second"]])
                for stem in ("meadow", "orchard"):
                    self.assertIn(self.settled_line(stem), text)
                self.assertIn("folder '{}' needs nothing more from this run: the run that held it has dealt with its "
                              "2 item(s)\n".format(self.folder("second")), text)
                self.assertNotIn("taken by this run", text)
                self.assert_no_lock_of_ours()

    def test_part_finished_by_the_holder_is_settled_and_the_rest_done_under_the_lock(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                lock = foreign_live_lock(self.folder("second"))

                def holder_finishes_one_and_leaves(folders):
                    self.finish_elsewhere("meadow")
                    os.unlink(str(lock))

                self.steps.append(holder_finishes_one_and_leaves)
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 3, 1, text)
                self.assertEqual(sorted(self.started()), ["harbour", "orchard", "valley"])
                self.assert_every_start_held_its_folder()
                self.assertIn(self.settled_line("meadow"), text)
                self.assertEqual(self.attempts("second"), TRIED_TWICE + ["acquiring", "acquired"])
                self.assertFalse(lock_file_of(self.folder("second")).exists())

    def test_a_stale_lock_is_taken_over_in_a_later_round(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                lock = foreign_live_lock(self.folder("second"))

                def holder_falls_silent(folders):
                    old = time.time() - 2 * record_module.HEARTBEAT_STALE_SECONDS
                    os.utime(str(lock), (old, old))

                self.steps.append(holder_falls_silent)
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 4, 0, text)
                self.assert_every_start_held_its_folder()
                self.assertIn("warning: taking over lock '{}'".format(lock), text)
                self.assertIn(self.taken_line("second", "the lock of the run that held it was judged dead and taken "
                                              "over (see the warning above)"), text)
                self.assertEqual(self.attempts("second"), TRIED_TWICE + ["acquiring", "acquired"])
                self.assertFalse(lock.exists())

    def test_no_wait_skips_a_held_folder_and_ends(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                lock = foreign_live_lock(self.folder("second"))
                lock_bytes = lock.read_bytes()
                summary, text = self.run_stage(jobs, no_wait=True)
                self.assert_counted_once(summary, 2, 2, text)
                self.assertEqual(self.waits, [])
                self.assertEqual(self.attempts("second"), ["acquiring"])
                self.assertEqual(self.skip_lines(text, "second"), 1, text)
                self.assertNotIn("waiting", text)
                self.assertEqual(lock.read_bytes(), lock_bytes)

    def test_a_folder_whose_lock_cannot_be_created_is_not_tried_again(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                self.refused.add("second")
                lock = foreign_live_lock(self.folder("third"))
                self.steps.append(lambda folders: os.unlink(str(lock)))
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 2, 2, text)
                self.assertEqual(self.attempts("second"), ["acquiring"])
                self.assertEqual(summary.lock_errors, [str(self.folder("second"))])
                self.assertEqual(self.waits, [["third"]])
                self.assertEqual(sorted(self.started()), ["harbour", "valley"])
                self.assertRegex(text, self.holder_line("third", 1))
                self.assertNotIn("  '{}': ".format(self.folder("second")), text)

    def test_the_folders_waited_for_are_named_again_whenever_their_set_changes(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                second = foreign_live_lock(self.folder("second"))
                third = foreign_live_lock(self.folder("third"))
                self.steps.extend([lambda folders: None,
                                   lambda folders: os.unlink(str(third)),
                                   lambda folders: None,
                                   lambda folders: os.unlink(str(second))])
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 4, 0, text)
                self.assertEqual(self.waits, [["second", "third"], ["second", "third"], ["second"], ["second"]])
                self.assertEqual(self.announcements(text),
                                 ["waiting for 2 folder(s) other runs hold, trying each again every 60 s:",
                                  "waiting for 1 folder(s) other runs hold, trying each again every 60 s:"], text)
                first, second_wait = text.split("waiting for 1 folder(s)")
                self.assertRegex(first, self.holder_line("second", 2))
                self.assertRegex(first, self.holder_line("third", 1))
                self.assertRegex(second_wait, self.holder_line("second", 2))
                self.assertNotIn("'{}'".format(self.folder("third")), second_wait.split("taken by this run")[0])
                self.assertEqual(text.count(self.taken_line("third", "the run that held it has released it")), 1)
                self.assertLess(text.index(self.taken_line("third", "the run that held it has released it")),
                                text.index("waiting for 1 folder(s)"))
                self.assertNotIn("still waiting", text, "a status line although the report interval never passed")
                self.assertEqual(self.skip_lines(text, "second"), 1, text)
                self.assertEqual(self.skip_lines(text, "third"), 1, text)

    def test_a_status_line_repeats_at_the_report_interval_only(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.fresh()
                lock = foreign_live_lock(self.folder("second"))

                def later(seconds):
                    def step(folders):
                        self.clock[0] += seconds
                    return step

                self.steps.extend([later(300), later(300), later(300), later(300),
                                   lambda folders: os.unlink(str(lock))])
                summary, text = self.run_stage(jobs)
                self.assert_counted_once(summary, 4, 0, text)
                self.assertEqual(len(self.waits), 5)
                self.assertEqual(len(self.announcements(text)), 1, text)
                status = [line for line in text.splitlines() if line.startswith("still waiting")]
                self.assertEqual(len(status), 2, text)  # at 600 s and 1200 s, not at each of the five tries
                for line, waited in zip(status, (600, 1200)):
                    self.assertRegex(line, "^" + re.escape("still waiting after {} s for 1 folder(s) other runs hold: "
                                                           "'{}' (last beat ".format(waited, self.folder("second")))
                                     + r"\d+ s ago\)$")

    # -- the real wait, ended from another thread once the waiting line is out

    def end_the_wait(self, stream, action, lock, finished):
        """A thread that runs `action` once the waiting line was written and then waits for `finished`.

        Should the line not come, or the run not finish, within the deadline,
        it says so in `self.late` and ends the run another way: a drain, or
        the removal of the other run's `lock`, which lets the next round
        (HELD_RETRY_SECONDS, twice the deadline, later) finish it. So a broken
        wait fails the test instead of hanging it.
        """
        self.late = None

        def watch():
            if not stream.waiting.wait(DEADLINE_SECONDS):
                self.late = "the waiting line never came"
                vidfunnel.interrupt.draining.set()
                return
            action()
            if not finished.wait(DEADLINE_SECONDS):
                self.late = "the run went on waiting after the wait was to end"
                os.unlink(str(lock))

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 4 * DEADLINE_SECONDS)
        return thread

    def ended_line(self, reason, end):
        return "not waiting for 1 folder(s) other runs hold ('{}'): {}; {}\n".format(self.folder("second"), reason, end)

    def check_a_wait_ended_by_a_drain(self, jobs, action, reason):
        self.fresh()
        lock = foreign_live_lock(self.folder("second"))
        lock_bytes = lock.read_bytes()
        stream = WatchedStream()
        finished = threading.Event()
        thread = self.end_the_wait(stream, action, lock, finished)
        try:
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                summary, text = self.run_stage(jobs, stream=stream, real_wait=True)
        finally:
            finished.set()
        thread.join(DEADLINE_SECONDS)
        self.assertIsNone(self.late, stream.getvalue())
        self.assert_counted_once(summary, 2, 2, text)
        self.assertTrue(vidfunnel.interrupt.draining.is_set())
        self.assertTrue(vidfunnel.interrupt.drain_tally.seen)
        self.assertEqual(len(self.announcements(text)), 1, text)
        self.assertRegex(text, self.holder_line("second", 2))
        self.assertIn(self.ended_line(reason, "their 2 item(s) are counted as skipped"), text)
        self.assertEqual(self.attempts("second"), TRIED_TWICE)
        self.assertEqual(lock.read_bytes(), lock_bytes)
        self.assert_no_lock_of_ours()

    def test_the_wait_ends_on_the_drain_signal(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.check_a_wait_ended_by_a_drain(jobs, vidfunnel.interrupt.draining.set,
                                                   "the run is draining (SIGUSR1)")

    def test_the_wait_ends_on_a_drain_stop_file(self):
        def stop_file():
            record_module._write_json_atomic(vidfunnel.stopfile.stop_path(str(self.root)),
                                             {"host": "elsewhere", "requested": None, "mode": "drain"})

        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.check_a_wait_ended_by_a_drain(jobs, stop_file, "a stop file asks the run to drain")

    def check_a_wait_ended_by_an_interrupt(self, action, reason):
        self.fresh()
        lock = foreign_live_lock(self.folder("second"))
        lock_bytes = lock.read_bytes()
        stream = WatchedStream()
        finished = threading.Event()
        thread = self.end_the_wait(stream, action, lock, finished)
        try:
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_stage(1, stream=stream, real_wait=True)
        finally:
            finished.set()
        thread.join(DEADLINE_SECONDS)
        self.assertIsNone(self.late, stream.getvalue())
        self.assertEqual(len(self.announcements(stream.getvalue())), 1, stream.getvalue())
        self.assertIn(self.ended_line(reason, "nothing more is done there"), stream.getvalue())
        self.assertEqual(sorted(self.started()), ["harbour", "valley"])
        self.assertEqual(lock.read_bytes(), lock_bytes)
        self.assert_no_lock_of_ours()

    def test_the_wait_ends_on_ctrl_c(self):
        self.check_a_wait_ended_by_an_interrupt(_thread.interrupt_main, "interrupted (Ctrl+C or a stop signal)")

    def test_the_wait_ends_on_a_now_stop_file(self):
        def stop_file():
            record_module._write_json_atomic(vidfunnel.stopfile.stop_path(str(self.root)),
                                             {"host": "elsewhere", "requested": None, "mode": "now"})

        self.check_a_wait_ended_by_an_interrupt(stop_file, "a stop file asks the run to stop now")
        self.assertTrue(vidfunnel.stopfile.stopped_now)


class ConvertRoundsTest(RoundsCase, unittest.TestCase):
    stage = "convert"

    def test_a_record_the_holder_left_failed_is_settled_with_its_reason(self):
        self.fresh()
        foreign_live_lock(self.folder("second"))

        def holder_ends(folders):
            self.finish_elsewhere("meadow")
            self.finish_elsewhere("orchard", status="failed")

        self.steps.append(holder_ends)
        summary, text = self.run_stage(1)
        self.assert_counted_once(summary, 2, 2, text)
        self.assertIn("[convert 3/4] skipping 'orchard.avi': status is 'failed', not 'planned'\n", text)
        self.assertEqual(self.attempts("second"), TRIED_TWICE)


class RefitRoundsTest(RoundsCase, unittest.TestCase):
    stage = "refit"

    def test_skip_reasons_count_each_record_once(self):
        self.fresh()
        lock = foreign_live_lock(self.folder("second"))

        def holder_refits_one_and_leaves(folders):
            self.finish_elsewhere("meadow")
            os.unlink(str(lock))

        self.steps.append(holder_refits_one_and_leaves)
        summary, text = self.run_stage(1)
        self.assertEqual(summary.skip_reasons, {"already refitted": 1}, text)
        self.assertNotIn("folder locked", text)


class PlanRoundsTest(RoundsCase, unittest.TestCase):
    stage = "plan"

    def test_replan_settles_nothing_and_waits_for_the_lock(self):
        self.fresh()
        lock = foreign_live_lock(self.folder("second"))

        def holder_plans_and_leaves(folders):
            self.finish_elsewhere("meadow")
            self.finish_elsewhere("orchard")

        self.steps.extend([holder_plans_and_leaves, lambda folders: os.unlink(str(lock))])
        summary, text = self.run_stage(1, replan=True)
        self.assert_counted_once(summary, 4, 0, text)
        self.assertEqual(self.waits, [["second"], ["second"]])
        self.assertEqual(sorted(self.started()), ["harbour", "meadow", "orchard", "valley"])


if __name__ == "__main__":
    unittest.main()

# End of File #
