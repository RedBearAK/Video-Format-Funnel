"""Tests for vidfunnel.turns: a folder's lock taken at its first item, once, and released after its last."""

VERSION = "20261110"

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
from vidfunnel.record import FolderLock
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


if __name__ == "__main__":
    unittest.main()

# End of File #
