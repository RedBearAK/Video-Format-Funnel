"""Tests for vidfunnel.stopfile: 'stop-all' and 'resume', the walk up to the root, and what a stop file does."""

VERSION = "20261109"

import contextlib
import io
import json
import os
import shutil
import signal
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import vidfunnel.interrupt
import vidfunnel.stopfile
from vidfunnel import cli
from vidfunnel.ffmpeg import run
from vidfunnel.interrupt import Interrupted, draining, drain_tally, interrupted, run_pool
from vidfunnel.record import RECORD_DIR
from vidfunnel.stopfile import check_item, find_stop, refusal, stop_path


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class StopFileCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="vidfunnel-stopfile-test-")
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name)
        self.library = self.base / "library"
        self.nested = self.library / "Films" / "Old"
        self.nested.mkdir(parents=True)
        self.sibling = self.base / "elsewhere" / "Films"
        self.sibling.mkdir(parents=True)
        for flag in (interrupted, draining):
            flag.clear()
            self.addCleanup(flag.clear)
        drain_tally.reset()
        self.addCleanup(drain_tally.reset)
        vidfunnel.stopfile.reset()
        self.addCleanup(vidfunnel.stopfile.reset)
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1):
            previous = signal.signal(signum, signal.SIG_DFL)
            self.addCleanup(signal.signal, signum, previous)

    def stop_all(self, *arguments):
        return run_main(["stop-all"] + [str(argument) for argument in arguments])


class WalkTests(StopFileCase):
    def test_a_stop_file_at_the_library_root_is_found_from_a_nested_folder_only(self):
        code, out, _err = self.stop_all(self.library)
        self.assertEqual(code, 0)
        self.assertIn("wrote '{}' (drain)".format(stop_path(str(self.library))), out)
        path, data = find_stop(self.nested)
        self.assertEqual(path, stop_path(str(self.library)))
        self.assertEqual(data["mode"], "drain")
        self.assertEqual(find_stop(self.sibling), (None, None))
        self.assertEqual(find_stop(self.base), (None, None), "a file below is not one above")

    def test_the_file_holds_who_when_and_how_and_is_written_through_the_atomic_writer(self):
        real = vidfunnel.stopfile._write_json_atomic
        with unittest.mock.patch("vidfunnel.stopfile._write_json_atomic", side_effect=real) as writer:
            self.stop_all(self.library, "--now")
        writer.assert_called_once()
        self.assertEqual(writer.call_args[0][0], stop_path(str(self.library)))
        data = json.loads(Path(stop_path(str(self.library))).read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["host", "mode", "pid", "requested", "user"])
        self.assertEqual(data["mode"], "now")
        self.assertEqual(sorted(os.listdir(str(self.library / RECORD_DIR))), ["stop"], "no temporary file is left")

    def test_an_unreadable_stop_file_counts_as_a_drain(self):
        (self.library / RECORD_DIR).mkdir()
        Path(stop_path(str(self.library))).write_text("{half", encoding="utf-8")
        _path, data = find_stop(self.nested)
        self.assertEqual(data["mode"], "drain")
        self.assertTrue(data["unreadable"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is not available here")
    def test_a_fifo_at_the_stop_path_is_a_drain_and_is_never_opened(self):
        """Opening a FIFO waits for a writer; the check must return at once and name the path."""
        (self.library / RECORD_DIR).mkdir()
        fifo = stop_path(str(self.library))
        os.mkfifo(fifo)
        outcome = []
        errors = io.StringIO()

        def check():
            outcome.append(check_item(self.nested))

        with contextlib.redirect_stderr(errors):
            thread = threading.Thread(target=check, daemon=True)
            thread.start()
            thread.join(1.0)
            hung = thread.is_alive()
            if hung:  # a writer releases the open() that waits for one, so the thread can end
                with open(fifo, "w"):
                    pass
                thread.join(5.0)
        self.assertFalse(hung, "the check waited on the FIFO for more than a second")
        self.assertEqual(outcome, ["drain"])
        self.assertTrue(draining.is_set())
        self.assertIn("'{}' (it could not be read as a regular file, taken as drain)".format(fifo), errors.getvalue())
        message = refusal([str(self.nested)])
        self.assertIn("could not be read as a regular file", message)

    def test_resume_removes_the_file_and_says_when_there_is_none(self):
        self.stop_all(self.library)
        code, out, _err = run_main(["resume", str(self.library)])
        self.assertEqual(code, 0)
        self.assertIn("removed '{}'".format(stop_path(str(self.library))), out)
        self.assertFalse(os.path.exists(stop_path(str(self.library))))
        code, out, _err = run_main(["resume", str(self.library)])
        self.assertEqual(code, 0)
        self.assertIn("no stop file at '{}'".format(stop_path(str(self.library))), out)

    def test_stop_all_refuses_a_path_that_is_not_a_folder(self):
        code, _out, err = self.stop_all(self.base / "missing")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("is not a folder", err)


class RefusalTests(StopFileCase):
    def test_a_stage_under_a_stop_file_refuses_before_taking_any_lock(self):
        self.stop_all(self.library)
        for command in ("plan", "convert", "verify"):
            with self.subTest(command=command):
                code, _out, err = run_main([command, str(self.nested)])
                self.assertEqual(code, cli.EXIT_ERROR)
                self.assertIn("vidfunnel {}: a stop was requested from host".format(command), err)
                self.assertIn("(drain)", err)
                self.assertIn("stop file '{}'".format(stop_path(str(self.library))), err)
                self.assertIn("remove it with: vidfunnel resume '{}' (or delete that file)".format(self.library), err)
                self.assertFalse((self.nested / RECORD_DIR).exists(), "nothing was written under the PATH")
        code, _out, err = run_main(["--yes", "retire", str(self.nested)])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("vidfunnel retire: a stop was requested", err)
        self.assertIsNone(refusal([self.sibling]))


class CheckTests(StopFileCase):
    def test_drain_mode_sets_the_drain_and_announces_it_once(self):
        self.stop_all(self.library)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(check_item(self.nested), "drain")
            self.assertEqual(check_item(self.nested), "drain")
        self.assertTrue(draining.is_set())
        self.assertFalse(interrupted.is_set())
        self.assertEqual(err.getvalue().count("vidfunnel: stop requested from host"), 1, err.getvalue())
        self.assertIn("(drain): finishing the items in progress, starting no more", err.getvalue())

    def test_now_mode_acts_as_ctrl_c(self):
        self.stop_all(self.library, "--now")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                unittest.mock.patch("vidfunnel.ffmpeg.terminate_children") as terminate:
            self.assertEqual(check_item(self.nested), "now")
        terminate.assert_called_once()
        self.assertTrue(interrupted.is_set())
        self.assertIn("(now): stopping now", err.getvalue())

    def test_no_file_no_effect(self):
        self.assertIsNone(check_item(self.sibling))
        self.assertFalse(draining.is_set() or interrupted.is_set())

    def test_a_child_is_not_started_under_a_now_file(self):
        sleeper = shutil.which("true")
        check_item(self.nested)  # this thread's item is in the nested folder
        run([sleeper])
        self.stop_all(self.library, "--now")  # main() forgets the item on its way out; set it again
        vidfunnel.stopfile._current.folder = str(self.nested)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                run([sleeper])
        interrupted.clear()
        vidfunnel.stopfile.reset()
        outcome = []

        def worker():
            check_item(self.sibling)
            run([sleeper])
            vidfunnel.stopfile._current.folder = str(self.nested)
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    run([sleeper])
            except BaseException as error:  # recorded for the assertion below
                outcome.append(error)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(10)
        self.assertEqual([type(error) for error in outcome], [Interrupted])

    def test_the_pool_puts_a_task_back_under_a_drain_file(self):
        done = []
        started = []

        def task(name):
            def body():
                started.append(name)
                return name
            return body

        self.stop_all(self.nested)
        folders = [str(self.sibling), str(self.nested), str(self.sibling)]
        with contextlib.redirect_stderr(io.StringIO()):
            run_pool(1, [task("first"), task("second"), task("third")], lambda index, value: done.append(value),
                     folders=folders)
        self.assertEqual(started, ["first"])
        self.assertEqual(done, ["first"])
        self.assertTrue(draining.is_set())
        self.assertEqual((drain_tally.seen, drain_tally.finished, drain_tally.left), (True, 0, 2))

    def test_the_pool_raises_keyboard_interrupt_under_a_now_file(self):
        self.stop_all(self.nested, "--now")
        started = []
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                run_pool(2, [lambda: started.append(1)] * 3, lambda index, value: None,
                         folders=[str(self.nested)] * 3)
        self.assertEqual(started, [])
        self.assertFalse(interrupted.is_set(), "the pool clears the event once its workers have ended")


if __name__ == "__main__":
    unittest.main()

# End of File #
