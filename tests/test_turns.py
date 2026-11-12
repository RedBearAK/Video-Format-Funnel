"""Tests for vidfunnel.turns: a folder's lock taken at its first item, once, and released after its last."""

VERSION = "20261111.1"

import datetime
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import vidfunnel.interrupt
import vidfunnel.record as record_module
import vidfunnel.turns as turns_module
from vidfunnel.record import FolderLock, LockHeld
from vidfunnel.turns import DRAINING, ERROR, HELD, SKIPPED, FolderTurns

DEADLINE_SECONDS = 60.0


def wait_for(condition, deadline=DEADLINE_SECONDS):
    """Poll `condition` until it is true; False only when the generous deadline passes first."""
    ends = time.monotonic() + deadline
    while time.monotonic() < ends:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def foreign_live_lock(folder):
    """A lock of another machine whose holder beat just now (the file's mtime is the share's now)."""
    path = Path(folder) / ".vidfunnel" / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"host": "elsewhere-host", "pid": 4242, "started": started, "container": False,
                                "machine": "elsewhere-machine", "machine_source": "machine-id",
                                "boot": "elsewhere-boot", "flock": False,
                                "heartbeat": {"interval": record_module.HEARTBEAT_SECONDS}}), encoding="utf-8")
    return path


def heartbeat_threads():
    return [thread for thread in threading.enumerate() if thread.name == "vidfunnel-heartbeat"]


class TurnsCase(unittest.TestCase):

    def setUp(self):
        vidfunnel.interrupt.draining.clear()
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        vidfunnel.interrupt.drain_tally.reset()
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lines = []
        self.harbour = self.folder("harbour")
        self.meadow = self.folder("meadow")

    def folder(self, name):
        path = self.root / name
        path.mkdir()
        return path

    def lock_file(self, folder):
        return Path(folder) / ".vidfunnel" / "lock"

    def turns(self, counts, **options):
        made = FolderTurns(counts, self.lines.append, **options)
        self.addCleanup(made.close)
        return made


class EnterLeaveTest(TurnsCase):

    def test_the_first_enter_takes_the_lock_and_the_last_leave_releases_it(self):
        turns = self.turns({self.harbour: 2, self.meadow: 1})
        self.assertFalse(self.lock_file(self.harbour).exists(), "a lock was taken before any item started")
        self.assertEqual(turns.enter(self.harbour), HELD)
        self.assertTrue(self.lock_file(self.harbour).exists())
        self.assertFalse(self.lock_file(self.meadow).exists(), "a folder no item has reached was locked")
        content = self.lock_file(self.harbour).read_bytes()
        self.assertEqual(turns.enter(self.harbour), HELD)
        self.assertEqual(self.lock_file(self.harbour).read_bytes(), content, "the second enter took the lock again")
        turns.leave(self.harbour)
        self.assertTrue(self.lock_file(self.harbour).exists(), "released before the folder's last item ended")
        turns.leave(self.harbour)
        self.assertFalse(self.lock_file(self.harbour).exists(), "not released after the folder's last item")
        self.assertEqual(turns.held(), [])
        self.assertEqual(self.lines, [])

    def test_a_folder_entered_after_its_release_is_not_worked_on(self):
        turns = self.turns({self.harbour: 1})
        self.assertEqual(turns.enter(self.harbour), HELD)
        turns.leave(self.harbour)
        self.assertNotEqual(turns.enter(self.harbour), HELD)
        self.assertFalse(self.lock_file(self.harbour).exists())

    def test_leave_without_enter_counts_down_and_takes_no_lock(self):
        turns = self.turns({self.harbour: 2})
        turns.leave(self.harbour)
        self.assertFalse(self.lock_file(self.harbour).exists())
        self.assertEqual(turns.enter(self.harbour), HELD)
        turns.leave(self.harbour)
        self.assertFalse(self.lock_file(self.harbour).exists())

    def test_close_releases_what_a_drain_left_held(self):
        turns = self.turns({self.harbour: 3, self.meadow: 2})
        turns.enter(self.harbour)
        turns.enter(self.meadow)
        turns.leave(self.harbour)
        turns.close()
        self.assertFalse(self.lock_file(self.harbour).exists())
        self.assertFalse(self.lock_file(self.meadow).exists())
        self.assertEqual(turns.held(), [])

    def test_a_held_folder_is_skipped_once_with_its_line(self):
        foreign = foreign_live_lock(self.meadow)
        content = foreign.read_bytes()
        turns = self.turns({self.meadow: 3})
        for _item in range(3):
            self.assertEqual(turns.enter(self.meadow), SKIPPED)
            turns.leave(self.meadow)
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertTrue(self.lines[0].startswith("skipping folder '{}': ".format(self.meadow)), self.lines)
        self.assertIn("elsewhere-host", self.lines[0])
        self.assertEqual(foreign.read_bytes(), content, "the other run's lock was touched")
        self.assertEqual(turns.lock_errors, [])

    def test_a_lock_file_that_cannot_be_created_is_an_error_named_once(self):
        errors = []
        turns = self.turns({self.harbour: 2}, lock_errors=errors)
        with mock.patch.object(FolderLock, "acquire", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(turns.enter(self.harbour), ERROR)
            self.assertEqual(turns.enter(self.harbour), ERROR)
        self.assertEqual(errors, [str(self.harbour)])
        self.assertIs(turns.lock_errors, errors)
        self.assertEqual(self.lines, ["skipping folder '{}': cannot take its lock: [Errno 13] Permission denied".format(
            self.harbour)])

    def test_no_new_lock_once_the_run_drains_but_a_held_folder_stays_held(self):
        turns = self.turns({self.harbour: 2, self.meadow: 1})
        self.assertEqual(turns.enter(self.harbour), HELD)
        vidfunnel.interrupt.draining.set()
        self.assertEqual(turns.enter(self.meadow), DRAINING)
        self.assertFalse(self.lock_file(self.meadow).exists())
        self.assertEqual(turns.enter(self.harbour), HELD, "an item already in a held folder")
        self.assertEqual(self.lines, [])

    def test_on_enter_runs_once_under_the_new_lock(self):
        seen = []

        def check(folder):
            seen.append((Path(folder).name, self.lock_file(folder).exists()))

        turns = self.turns({self.harbour: 2, self.meadow: 1}, on_enter=check)
        turns.enter(self.harbour)
        turns.enter(self.harbour)
        self.assertEqual(seen, [("harbour", True)])

    def test_on_enter_failing_releases_the_folder_and_its_items_do_not_run(self):
        def broken(folder):
            raise RuntimeError("the check broke")

        turns = self.turns({self.harbour: 2}, on_enter=broken)
        with self.assertRaises(RuntimeError):
            turns.enter(self.harbour)
        self.assertFalse(self.lock_file(self.harbour).exists())
        self.assertEqual(turns.enter(self.harbour), ERROR)

    def test_last_runs_under_the_lock_only_for_a_held_folder(self):
        seen = []

        def last(folder):
            seen.append((Path(folder).name, self.lock_file(folder).exists()))

        foreign_live_lock(self.meadow)
        turns = self.turns({self.harbour: 1, self.meadow: 1})
        turns.enter(self.harbour)
        turns.enter(self.meadow)
        turns.leave(self.harbour, last=last)
        turns.leave(self.meadow, last=last)
        self.assertEqual(seen, [("harbour", True)])
        self.assertFalse(self.lock_file(self.harbour).exists())


class AgainTest(TurnsCase):
    """again(): a folder another run held is tried once more in a later round, its skip line not repeated."""

    def test_a_skipped_folder_is_tried_again_and_taken_once_free(self):
        foreign = foreign_live_lock(self.meadow)
        turns = self.turns({self.meadow: 2})
        for _item in range(2):
            self.assertEqual(turns.enter(self.meadow), SKIPPED)
            turns.leave(self.meadow)
        turns.again(self.meadow, 1)
        self.assertEqual(turns.enter(self.meadow), SKIPPED, "still held")
        turns.leave(self.meadow)
        foreign.unlink()
        turns.again(self.meadow, 1)
        self.assertEqual(turns.enter(self.meadow), HELD)
        self.assertTrue(self.lock_file(self.meadow).exists())
        turns.leave(self.meadow)
        self.assertFalse(self.lock_file(self.meadow).exists(), "not released after the round's last item")
        self.assertEqual(len(self.lines), 2, self.lines)
        self.assertTrue(self.lines[0].startswith("skipping folder '{}': ".format(self.meadow)), self.lines)
        self.assertEqual(self.lines[1], "folder '{}' taken by this run in a later round: the run that held it has "
                                        "released it".format(self.meadow))
        self.assertIsNotNone(turns.holder(self.meadow))
        self.assertEqual((turns.holder(self.meadow).host, turns.holder(self.meadow).pid), ("elsewhere-host", 4242))

    def test_a_folder_never_entered_may_be_tried(self):
        turns = self.turns({})
        turns.again(self.harbour, 1)
        self.assertEqual(turns.enter(self.harbour), HELD)
        turns.leave(self.harbour)
        self.assertFalse(self.lock_file(self.harbour).exists())

    def test_a_held_an_errored_or_a_busy_folder_is_not_tried_again(self):
        turns = self.turns({self.harbour: 1, self.meadow: 2})
        self.assertEqual(turns.enter(self.harbour), HELD)
        with self.assertRaises(ValueError):
            turns.again(self.harbour, 1)
        with mock.patch.object(FolderLock, "acquire", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(turns.enter(self.meadow), ERROR)
        turns.leave(self.meadow)
        turns.leave(self.meadow)
        with self.assertRaises(ValueError):
            turns.again(self.meadow, 1)
        foreign_live_lock(self.folder("orchard"))
        busy = self.turns({self.root / "orchard": 2})
        self.assertEqual(busy.enter(self.root / "orchard"), SKIPPED)
        with self.assertRaises(ValueError):  # one item of the folder has not left yet
            busy.again(self.root / "orchard", 1)


class SerialisedEnterTest(TurnsCase):

    def enter_together(self, delay):
        """Eight threads enter one folder at once; FolderLock.acquire slowed by `delay` to widen the window."""
        real_acquire = FolderLock.acquire
        calls = []

        def slow_acquire(lock):
            calls.append(lock.folder)
            time.sleep(delay)
            return real_acquire(lock)

        turns = self.turns({self.harbour: 8})
        answers = []
        start = threading.Barrier(8)

        def item():
            start.wait()
            answers.append(turns.enter(self.harbour))
            turns.leave(self.harbour)

        with mock.patch.object(FolderLock, "acquire", slow_acquire):
            threads = [threading.Thread(target=item) for _number in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(DEADLINE_SECONDS)
        return calls, answers

    def test_threads_of_one_folder_acquire_it_once(self):
        for delay in (0.0, 0.2):
            with self.subTest(delay=delay):
                self.lines.clear()
                calls, answers = self.enter_together(delay)
                self.assertEqual(len(calls), 1, calls)
                self.assertEqual(answers, [HELD] * 8)
                self.assertFalse(any("locked by this run itself" in line for line in self.lines), self.lines)
                self.assertFalse(self.lock_file(self.harbour).exists())


class HeartbeatBetweenFoldersTest(TurnsCase):

    def test_one_heartbeat_thread_at_most_and_none_after_the_last_release(self):
        wait_for(lambda: not heartbeat_threads())
        before = len(heartbeat_threads())
        self.assertEqual(before, 0, "a heartbeat thread from an earlier test is still running")
        folders = [self.folder("orchard"), self.folder("valley"), self.folder("summit")]
        turns = self.turns({folder: 1 for folder in folders})
        most = 0
        for folder in folders:
            self.assertEqual(turns.enter(folder), HELD)
            most = max(most, len(heartbeat_threads()))
            turns.leave(folder)
            most = max(most, len(heartbeat_threads()))
        self.assertLessEqual(most, 1)
        self.assertTrue(wait_for(lambda: not heartbeat_threads()), "the heartbeat thread outlived the last lock")


class NotStartedTest(unittest.TestCase):

    def setUp(self):
        vidfunnel.interrupt.drain_tally.reset()
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)

    def test_moves_a_task_from_finished_to_left(self):
        tally = vidfunnel.interrupt.drain_tally
        tally.finished = 3
        turns_module.not_started(2)
        self.assertEqual((tally.finished, tally.left, tally.seen), (1, 2, True))


class ScriptedTurns(object):
    """What held_rounds asks of a FolderTurns: again() (nothing to reset here) and the holder of each folder."""

    def __init__(self, holders):
        self.holders = holders  # folder name -> LockHeld, changed by the test between rounds
        self.opened = []

    def again(self, folder, count):
        self.opened.append((str(folder), count))

    def holder(self, folder):
        return self.holders.get(str(folder))


def beating_holder(host="elsewhere-host", pid=4242, beat=10.0):
    return LockHeld("locked by host '{}' pid {}".format(host, pid), host=host, pid=pid, age_seconds=60.0,
                    beat_age_seconds=beat)


class HeldRoundsTest(unittest.TestCase):
    """held_rounds alone, with a scripted run, settle and wait: what it announces and when it waits."""

    def setUp(self):
        vidfunnel.interrupt.draining.clear()
        self.addCleanup(vidfunnel.interrupt.draining.clear)
        vidfunnel.interrupt.drain_tally.reset()
        self.addCleanup(vidfunnel.interrupt.drain_tally.reset)
        self.lines = []
        self.clock = [0.0]
        self.waits = []
        self.steps = []
        for name, value in (("_clock", lambda: self.clock[0]), ("wait_for_folders", self.scripted_wait)):
            patch = mock.patch.object(turns_module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def scripted_wait(self, folders):
        self.waits.append(list(folders))
        self.steps.pop(0)()
        return None

    def later(self, seconds):
        def step():
            self.clock[0] += seconds
        return step

    def rounds(self, turns, held, run):
        return turns_module.held_rounds(turns, held, lambda unit: unit[0], lambda unit: unit, run, self.lines.append)

    def test_the_same_units_in_another_order_are_one_announcement_and_status_lines_follow(self):
        units = [("/library/meadow", "orchard"), ("/library/harbour", "pier"), ("/library/meadow", "grove")]
        turns = ScriptedTurns({"/library/meadow": beating_holder(), "/library/harbour": beating_holder()})
        calls = []

        def run(batch):
            calls.append(list(batch))
            if len(calls) > 5:
                return []
            return list(reversed(batch)) if len(calls) % 2 else list(batch)  # back in alternating order

        self.steps.extend([self.later(300)] * 5)
        self.assertEqual(self.rounds(turns, list(units), run), [])
        self.assertEqual(len(self.waits), 5)
        self.assertEqual(self.waits[0], ["/library/harbour", "/library/meadow"], "folders in sorted order")
        announced = [line for line in self.lines if line.startswith("waiting for ")]
        self.assertEqual(announced, ["waiting for 2 folder(s) other runs hold, trying each again every 60 s:"],
                         self.lines)
        folder_lines = [line for line in self.lines if line.startswith("  '")]
        self.assertEqual([line.split("'")[1] for line in folder_lines], ["/library/harbour", "/library/meadow"])
        self.assertIn("; 2 item(s) of this run left there", folder_lines[1])
        status = [line for line in self.lines if line.startswith("still waiting")]
        self.assertEqual(status, [
            "still waiting after {} s for 2 folder(s) other runs hold: '/library/harbour' (last beat 10 s ago), "
            "'/library/meadow' (last beat 10 s ago)".format(waited) for waited in (600, 1200)], self.lines)

    def test_a_folder_whose_holder_changes_is_announced_again(self):
        units = [("/library/meadow", "orchard")]
        turns = ScriptedTurns({"/library/meadow": beating_holder()})
        rounds_left = [3]

        def run(batch):
            rounds_left[0] -= 1
            return list(batch) if rounds_left[0] >= 0 else []

        def new_holder():
            turns.holders["/library/meadow"] = beating_holder(host="faraway-host", pid=5151, beat=20.0)

        self.steps.extend([self.later(60), new_holder, self.later(60)])
        self.assertEqual(self.rounds(turns, list(units), run), [])
        announced = [index for index, line in enumerate(self.lines) if line.startswith("waiting for ")]
        self.assertEqual(len(announced), 2, self.lines)
        self.assertIn("held by host 'elsewhere-host' pid 4242, last beat 10 s ago", self.lines[announced[0] + 1])
        self.assertIn("held by host 'faraway-host' pid 5151, last beat 20 s ago", self.lines[announced[1] + 1])
        self.assertTrue(self.lines[announced[1] + 2].startswith("the wait ends for a folder"), self.lines)


class WaitWordsTest(unittest.TestCase):
    """What a wait says about each folder's holder: what ends the wait for that folder."""

    def setUp(self):
        self.turns = ScriptedTurns({})

    def test_a_pid_running_here_says_what_ends_its_wait(self):
        held = LockHeld("folder '/library/meadow' is locked by host 'harbour-host' pid 777 since 2026-11-11T08:00:00Z "
                        "(7200 s ago; its pid 777 is running here and its holder last beat 12 s ago on the share's "
                        "clock; taken over once that process is gone, or once the lock is 24 h old and its holder has "
                        "been silent for 3600 s); lock file '/library/meadow/.vidfunnel/lock'",
                        host="harbour-host", pid=777, age_seconds=7200.0)
        held.pid_running_here = True
        self.turns.holders["/library/meadow"] = held
        words = turns_module.holder_words(held)
        self.assertEqual(words, "held by host 'harbour-host' pid 777, a process running on this machine (taken over "
                                "once that process is gone; while it runs, not before the lock is 24 h old: "
                                "{})".format(held))
        self.assertNotIn("its heartbeat does not decide", words)
        self.assertNotIn("stays silent", words)
        lines = turns_module.waiting_lines(self.turns, ["/library/meadow"], {"/library/meadow": 3})
        self.assertEqual(lines[1], "  '/library/meadow': {}; 3 item(s) of this run left there".format(words))
        self.assertEqual(lines[2], "the wait ends for a folder when its holder finishes or releases it, or when its "
                                   "lock is taken over as its line says; Ctrl+C, SIGUSR1 or 'vidfunnel stop-all PATH' "
                                   "(PATH at or above these folders) ends the whole wait; --no-wait skips such folders "
                                   "instead")
        self.assertEqual(turns_module.status_line(self.turns, ["/library/meadow"], 600.0),
                         "still waiting after 600 s for 1 folder(s) other runs hold: '/library/meadow' (last beat not "
                         "judged by heartbeat)")

    def test_a_lock_the_heartbeat_does_not_decide_embeds_its_reason(self):
        held = LockHeld("folder '/library/meadow' is locked by host 'harbour-host' pid 777 since 2026-11-11T08:00:00Z "
                        "(600 s ago; written by a run on another host, whose pid cannot be checked from here; taken "
                        "over once 24 h old); lock file '/library/meadow/.vidfunnel/lock'",
                        host="harbour-host", pid=777, age_seconds=600.0)
        self.turns.holders["/library/meadow"] = held
        self.assertEqual(turns_module.holder_words(held),
                         "held by host 'harbour-host' pid 777 (its heartbeat does not decide here: {})".format(held))
        self.assertEqual(turns_module.status_line(self.turns, ["/library/meadow"], 1200.0),
                         "still waiting after 1200 s for 1 folder(s) other runs hold: '/library/meadow' (last beat "
                         "not judged by heartbeat)")

    def test_a_beating_lock_says_when_it_is_taken_over(self):
        self.assertEqual(turns_module.holder_words(beating_holder(beat=600.0)),
                         "held by host 'elsewhere-host' pid 4242, last beat 600 s ago (taken over if it stays silent "
                         "for 3000 s more, at the first try after that)")


if __name__ == "__main__":
    unittest.main()

# End of File #
